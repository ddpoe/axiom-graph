"""Dependency sets and module digests -- one definition shared by every pair reader and writer.

A node's *dependency set* is every target the LINKED_STALE passes can name as
its via, each with the kinds of link that name it:

- ``documents`` -- a doc section's link targets (Pass 1), except atomic
  DocJSON nodes, which only Pass 3 propagates through;
- ``validates`` -- an atomic test's targets (Pass 1), same exclusion;
- ``annotates`` -- an envelope's annotated targets (Pass A), compared on code
  and docstring;
- ``delegates`` -- the tasks a workflow / task envelope reaches through its
  ``delegates_to`` closure (Pass B).

A verification records one *pair* per target (the target's live hash at that
moment), and the staleness passes compare it with the target's live hash.
The recording funnels, the schema-v5 backfill and Pass P all read the set
from here, so they can never disagree about which links a pair covers.

A composite target that has no hash of its own (a module) pairs by a digest
of its members' code hashes (:func:`digest_members`, :func:`module_digest`).

This module is pure apart from :func:`load_dependency_graph`, which reads an
open connection.  It must not import the DB package at module level:
``axiom_graph.db.staleness`` imports :func:`delegates_closure` from here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from axiom_graph.models import hash16

#: Link kinds a dependency can carry.
DOCUMENTS = "documents"
VALIDATES = "validates"
ANNOTATES = "annotates"
DELEGATES = "delegates"

#: Composite subtypes the staleness engine hashes from disk.  Any other
#: composite (a module, a config file) has no hash of its own and pairs by
#: the digest of its members.
HASHED_COMPOSITE_SUBTYPES = frozenset({"docjson", "docjson_doc", "workflow", "task"})
#: Pass-through step views: never hashed, never a digest member.
STEP_SUBTYPES = frozenset({"step", "autostep"})

_SECTION_SUBTYPES = frozenset({"docjson", "docjson_section"})
_SECTION_SOURCES = frozenset({"docjson", "json_doc_scanner"})
_ENVELOPE_SUBTYPES = frozenset({"workflow", "task"})
_CLOSURE_MAX_DEPTH = 32
_GRAPH_EDGE_TYPES = ("documents", "validates", "annotates", "composes", "delegates_to")

#: ``(code_hash, desc_hash)`` of a node.
Hashes = tuple[str | None, str | None]


@dataclass(frozen=True)
class NodeKind:
    """The identity columns the dependency filters read.

    Attributes:
        node_type: ``atomic_process`` / ``composite_process`` / ``entity``.
        subtype: Node subtype, or ``None``.
        source: Scanner that produced the node, or ``None``.
    """

    node_type: str
    subtype: str | None
    source: str | None


def is_doc_section(kind: NodeKind) -> bool:
    """Whether *kind* is a DocJSON section (the Pass 1 ``documents`` dependent filter)."""
    return kind.node_type == "atomic_process" and kind.subtype in _SECTION_SUBTYPES and kind.source in _SECTION_SOURCES


def is_excluded_link_target(kind: NodeKind) -> bool:
    """Whether a ``documents`` / ``validates`` target is an atomic DocJSON node, which Pass 1 skips."""
    return kind.node_type == "atomic_process" and (kind.subtype or "") in _SECTION_SUBTYPES


def is_envelope(kind: NodeKind) -> bool:
    """Whether *kind* is a workflow / task envelope (the Pass B dependent filter)."""
    return kind.node_type == "composite_process" and kind.subtype in _ENVELOPE_SUBTYPES


def is_digest_target(node_type: str | None, subtype: str | None) -> bool:
    """Whether a target pairs by the digest of its members rather than a hash of its own.

    Args:
        node_type: The target's node type.
        subtype: The target's subtype.

    Returns:
        ``True`` for a composite the staleness engine never hashes (a module,
        a config file).
    """
    return node_type == "composite_process" and subtype not in HASHED_COMPOSITE_SUBTYPES


def warm_delegates_closures(graph: object, envelope_ids: Iterable[str]) -> None:
    """Load what the ``delegates_to`` closures of *envelope_ids* read from a lazy graph, a level at a time.

    A no-op unless *graph* reads on demand (its ``kinds``, ``composes_out``,
    ``delegates_out`` and ``annotates_rev`` offer ``prefetch``).  The walk is
    the one :func:`delegates_closure` makes, from every envelope at once:
    per level, one batched read each of the envelopes' ``composes``
    children, their identities, the autosteps' ``delegates_to`` targets and
    the tasks' annotating envelopes.  So a closure over a lazy graph costs
    O(depth) batched reads, however many envelopes, steps and tasks it
    meets.  Values are not changed, only how many reads load them.

    Args:
        graph: The links (a :class:`DependencyGraph`).
        envelope_ids: The envelopes whose closures will be walked.
    """
    kinds = getattr(graph, "kinds", None)
    composes_out = getattr(graph, "composes_out", None)
    delegates_out = getattr(graph, "delegates_out", None)
    annotates_rev = getattr(graph, "annotates_rev", None)
    loaders = [getattr(m, "prefetch", None) for m in (composes_out, kinds, delegates_out, annotates_rev)]
    if any(loader is None for loader in loaders):
        return
    composes_prefetch, kinds_prefetch, delegates_prefetch, annotates_prefetch = loaders
    frontier = list(dict.fromkeys(envelope_ids))
    seen_envs: set[str] = set(frontier)
    seen_tasks: set[str] = set()
    depth = 0
    while frontier and depth < _CLOSURE_MAX_DEPTH:
        composes_prefetch(frontier)
        steps = list(dict.fromkeys(s for env in frontier for s in composes_out.get(env, ())))
        kinds_prefetch(steps)
        autosteps = [s for s in steps if graph.subtype_of(s) == "autostep"]  # type: ignore[attr-defined]
        delegates_prefetch(autosteps)
        tasks = [t for t in dict.fromkeys(delegates_out.get(s) for s in autosteps) if t and t not in seen_tasks]
        seen_tasks.update(tasks)
        annotates_prefetch(tasks)
        frontier = [e for e in dict.fromkeys(e for t in tasks for e in annotates_rev.get(t, ())) if e not in seen_envs]
        seen_envs.update(frontier)
        depth += 1


def delegates_closure(
    envelope_id: str,
    composes_out: Mapping[str, list[str]],
    delegates_out: Mapping[str, str],
    annotates_rev: Mapping[str, list[str]],
    subtype_of: Callable[[str], str | None],
) -> list[str]:
    """Return the tasks an envelope reaches through its ``delegates_to`` closure.

    Walks ``composes`` (envelope -> autostep) -> ``delegates_to`` (autostep ->
    task) -> ``annotates`` reversed (task -> the task's own envelope), and on
    from that envelope.  Visited-task and visited-envelope sets make it
    cycle-safe, and the walk stops after a fixed depth.

    Args:
        envelope_id: The envelope to expand.
        composes_out: Forward ``composes`` adjacency.
        delegates_out: Autostep -> its one ``delegates_to`` target.
        annotates_rev: Annotated node -> the envelopes that annotate it.
        subtype_of: Node id -> subtype lookup.

    Returns:
        The reachable task ids, in discovery order.
    """
    owner = getattr(subtype_of, "__self__", None)
    if owner is not None:
        warm_delegates_closures(owner, [envelope_id])
    tasks: list[str] = []
    visited_tasks: set[str] = set()
    visited_envs: set[str] = {envelope_id}
    frontier = [envelope_id]
    depth = 0
    while frontier and depth < _CLOSURE_MAX_DEPTH:
        next_envs: list[str] = []
        for env in frontier:
            for step_id in composes_out.get(env, ()):
                if subtype_of(step_id) != "autostep":
                    continue
                task_id = delegates_out.get(step_id)
                if not task_id or task_id in visited_tasks:
                    continue
                visited_tasks.add(task_id)
                tasks.append(task_id)
                for task_env in annotates_rev.get(task_id, ()):
                    if task_env in visited_envs:
                        continue
                    visited_envs.add(task_env)
                    next_envs.append(task_env)
        frontier = next_envs
        depth += 1
    return tasks


@dataclass
class DependencyGraph:
    """The node identities and links the dependency filters read, loaded once.

    Attributes:
        kinds: Node id -> :class:`NodeKind` for every indexed node.
        documents_out: Source -> ``documents`` targets.
        validates_out: Source -> ``validates`` targets.
        annotates_out: Source -> ``annotates`` targets.
        annotates_rev: Target -> the sources that annotate it.
        composes_out: Parent -> ``composes`` children.
        delegates_out: Autostep -> its ``delegates_to`` target.
    """

    kinds: dict[str, NodeKind] = field(default_factory=dict)
    documents_out: dict[str, list[str]] = field(default_factory=dict)
    validates_out: dict[str, list[str]] = field(default_factory=dict)
    annotates_out: dict[str, list[str]] = field(default_factory=dict)
    annotates_rev: dict[str, list[str]] = field(default_factory=dict)
    composes_out: dict[str, list[str]] = field(default_factory=dict)
    delegates_out: dict[str, str] = field(default_factory=dict)

    def subtype_of(self, node_id: str) -> str | None:
        """Return the subtype of *node_id*, or ``None`` when unknown."""
        kind = self.kinds.get(node_id)
        return kind.subtype if kind else None


def load_dependency_graph(conn: sqlite3.Connection) -> DependencyGraph:
    """Load every node identity and dependency link in two queries.

    Args:
        conn: Open connection whose ``row_factory`` yields mapping rows.

    Returns:
        The loaded :class:`DependencyGraph`.
    """
    graph = DependencyGraph()
    for r in conn.execute("SELECT id, node_type, subtype, source FROM nodes"):
        graph.kinds[r["id"]] = NodeKind(r["node_type"], r["subtype"], r["source"])
    placeholders = ",".join("?" * len(_GRAPH_EDGE_TYPES))
    rows = conn.execute(
        f"SELECT edge_type, from_id, to_id FROM edges WHERE edge_type IN ({placeholders})",
        _GRAPH_EDGE_TYPES,
    )
    for r in rows:
        et, src, dst = r["edge_type"], r["from_id"], r["to_id"]
        if et == "documents":
            graph.documents_out.setdefault(src, []).append(dst)
        elif et == "validates":
            graph.validates_out.setdefault(src, []).append(dst)
        elif et == "annotates":
            graph.annotates_out.setdefault(src, []).append(dst)
            graph.annotates_rev.setdefault(dst, []).append(src)
        elif et == "composes":
            graph.composes_out.setdefault(src, []).append(dst)
        else:
            # An autostep has at most one delegates_to edge by construction.
            graph.delegates_out[src] = dst
    return graph


def dependency_set(graph: DependencyGraph, node_id: str) -> dict[str, frozenset[str]]:
    """Return every target the staleness passes can name as *node_id*'s via.

    Mirrors the passes' filters exactly: ``documents`` only from a doc
    section, ``validates`` only from an atomic test (both skipping atomic
    DocJSON targets), ``annotates`` from any node, and the ``delegates_to``
    closure only from a workflow / task envelope.  A target with no node row
    is left out: it has no hash to pair against (BROKEN_LINK covers it).

    Args:
        graph: The loaded links.
        node_id: The dependent.

    Returns:
        Target id -> the link kinds that name it.
    """
    kind = graph.kinds.get(node_id)
    if kind is None:
        return {}
    out: dict[str, set[str]] = {}
    prefetch = getattr(graph.kinds, "prefetch", None)
    if prefetch is not None:
        # A lazy graph: load every linked target's identity in one query, not one per target.
        linked = list(graph.annotates_out.get(node_id, ()))
        if is_doc_section(kind):
            linked += graph.documents_out.get(node_id, ())
        if kind.node_type == "atomic_process" and kind.subtype == "test":
            linked += graph.validates_out.get(node_id, ())
        prefetch(linked)

    def _add(target: str, link_kind: str) -> None:
        out.setdefault(target, set()).add(link_kind)

    if is_doc_section(kind):
        for target in graph.documents_out.get(node_id, ()):
            tk = graph.kinds.get(target)
            if tk is not None and not is_excluded_link_target(tk):
                _add(target, DOCUMENTS)
    if kind.node_type == "atomic_process" and kind.subtype == "test":
        for target in graph.validates_out.get(node_id, ()):
            tk = graph.kinds.get(target)
            if tk is not None and not is_excluded_link_target(tk):
                _add(target, VALIDATES)
    for target in graph.annotates_out.get(node_id, ()):
        if target in graph.kinds:
            _add(target, ANNOTATES)
    if is_envelope(kind):
        for target in delegates_closure(
            node_id, graph.composes_out, graph.delegates_out, graph.annotates_rev, graph.subtype_of
        ):
            if target in graph.kinds:
                _add(target, DELEGATES)
    return {target: frozenset(kinds) for target, kinds in out.items()}


def _prefetch_composes_trees(graph: DependencyGraph, root_ids: Iterable[str]) -> None:
    """Load a lazy graph's ``composes`` trees under *root_ids* one walk level at a time.

    A no-op on a fully loaded graph.  On a lazy one, each level costs one
    batched read of its ``composes`` children and one of their identities,
    for every root at once, so walking the trees reads O(depth) batches,
    not one row per member.  Values are not changed, only how many reads
    load them.

    Args:
        graph: The links.
        root_ids: The roots of the walks.
    """
    composes_prefetch = getattr(graph.composes_out, "prefetch", None)
    kinds_prefetch = getattr(graph.kinds, "prefetch", None)
    if composes_prefetch is None or kinds_prefetch is None:
        return
    level = list(dict.fromkeys(root_ids))
    seen: set[str] = set(level)
    while level:
        composes_prefetch(level)
        children = [
            c for c in dict.fromkeys(c for nid in level for c in graph.composes_out.get(nid, ())) if c not in seen
        ]
        seen.update(children)
        if children:
            kinds_prefetch(children)
        level = children


def warm_dependency_sets(graph: DependencyGraph, node_ids: Iterable[str]) -> None:
    """Load what :func:`dependency_set` reads for *node_ids* from a lazy graph, in a few batched reads.

    A no-op on a fully loaded graph.  On a lazy one: the nodes' identities
    and outbound links, their linked targets' identities, and the
    ``delegates_to`` closures of the envelopes among them, each in batches
    for every node at once.  Values are not changed, only how many reads
    load them.

    Args:
        graph: The links.
        node_ids: The dependents about to be judged.
    """
    prefetch = getattr(graph, "prefetch", None)
    kinds_prefetch = getattr(graph.kinds, "prefetch", None)
    if prefetch is None or kinds_prefetch is None:
        return
    ids = list(dict.fromkeys(node_ids))
    prefetch(ids)
    linked: list[str] = []
    envelopes: list[str] = []
    for node_id in ids:
        kind = graph.kinds.get(node_id)
        if kind is None:
            continue
        linked.extend(graph.annotates_out.get(node_id, ()))
        if is_doc_section(kind):
            linked.extend(graph.documents_out.get(node_id, ()))
        if kind.node_type == "atomic_process" and kind.subtype == "test":
            linked.extend(graph.validates_out.get(node_id, ()))
        if is_envelope(kind):
            envelopes.append(node_id)
    kinds_prefetch(linked)
    warm_delegates_closures(graph, envelopes)


def digest_members(graph: DependencyGraph, target_id: str) -> list[str]:
    """Return the members a digest target's pair hash is computed over.

    Every atomic descendant through ``composes`` -- functions and tests,
    nested ones included -- except the pass-through step views.  Cycle-safe.
    Whether a member currently has a live hash is the caller's lookup to
    decide (a NOT_FOUND member is left out of the digest).

    Args:
        graph: The loaded links.
        target_id: The digest target (see :func:`is_digest_target`).

    Returns:
        Member ids in walk order.
    """
    _prefetch_composes_trees(graph, [target_id])
    members: list[str] = []
    seen: set[str] = {target_id}
    stack = list(graph.composes_out.get(target_id, ()))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        kind = graph.kinds.get(nid)
        if kind is not None and kind.node_type == "atomic_process" and kind.subtype not in STEP_SUBTYPES:
            members.append(nid)
        stack.extend(graph.composes_out.get(nid, ()))
    return members


def module_digest(code_hashes: Iterable[str]) -> str:
    """Hash a digest target's member code hashes, ids left out, so a pure rename keeps it.

    Args:
        code_hashes: The members' live code hashes.

    Returns:
        ``hash16`` of the sorted hashes, newline-joined.
    """
    return hash16("\n".join(sorted(code_hashes)))


def live_value(
    graph: DependencyGraph,
    target_id: str,
    hashes: Callable[[str], Hashes | None],
    missing: Callable[[str], bool],
) -> Hashes | None:
    """Return a target's live ``(code, desc)`` as pairs record and compare it.

    The one rule both sides use.  A target that is missing (NOT_FOUND) or has
    no code hash has no live value.  A digest target's value is
    :func:`module_digest` over the members that have one, with no desc; it
    has no live value when no member has one (its file is gone, or it holds
    no function).  A digest target's own status is never consulted: it is
    inherited from its members, so a module reads NOT_FOUND whenever one
    member does, while the engine decides its live value before inheritance
    runs.

    Args:
        graph: The loaded links.
        target_id: The dependency target.
        hashes: Node id -> its live hashes, or ``None`` when it has none.
        missing: Node id -> whether it is NOT_FOUND.

    Returns:
        The live hashes, or ``None`` when the target has no live value.
    """
    kind = graph.kinds.get(target_id)
    if kind is None:
        return None
    if is_digest_target(kind.node_type, kind.subtype):
        codes: list[str] = []
        for member in digest_members(graph, target_id):
            if missing(member):
                continue
            h = hashes(member)
            if h is not None and h[0]:
                codes.append(h[0])
        if not codes:
            return None
        return module_digest(codes), None
    if missing(target_id):
        return None
    h = hashes(target_id)
    if h is None or not h[0]:
        return None
    return h


@dataclass(frozen=True)
class LiveView:
    """Where a pair reader or writer looks up a node's live hashes.

    Attributes:
        hashes: Node id -> its live ``(code, desc)``, or ``None`` when it has
            none.
        missing: Node id -> whether it is NOT_FOUND.
        prefetch: Optional batch loader a lazy view offers, so a reader can
            load many nodes in one query before judging them.
    """

    hashes: Callable[[str], Hashes | None]
    missing: Callable[[str], bool]
    prefetch: Callable[[Iterable[str]], None] | None = None

    def value(self, graph: DependencyGraph, target_id: str) -> Hashes | None:
        """Return *target_id*'s live value under this view (see :func:`live_value`)."""
        return live_value(graph, target_id, self.hashes, self.missing)


def pair_hashes(kinds: frozenset[str], live: Hashes) -> Hashes:
    """Return the ``(code, desc)`` a verification records for one target.

    Args:
        kinds: The link kinds naming the target.
        live: The target's live value.

    Returns:
        The code hash, plus the desc hash only when an ``annotates`` link
        names the target (Pass A counts docstring changes; no other pass does).
    """
    return live[0], (live[1] if ANNOTATES in kinds else None)


def pair_matches(kinds: frozenset[str], recorded: Hashes, live: Hashes) -> bool:
    """Whether a recorded pair still matches the target's live value.

    Args:
        kinds: The link kinds naming the target now.
        recorded: The pair's ``(code_hash, desc_hash)``.
        live: The target's live value.

    Returns:
        ``True`` when the code hash matches and, for an ``annotates`` link,
        the desc hash matches too.
    """
    if recorded[0] != live[0]:
        return False
    if ANNOTATES in kinds:
        return recorded[1] == live[1]
    return True


_ABSENT = object()


class _ReadThrough:
    """A read-only mapping that loads entries in batches on first access.

    Supports what the dependency filters use: ``get(key, default)``,
    ``in`` and ``[key]``.  :meth:`prefetch` loads many keys in one query.

    Args:
        loader: ``keys -> {key: value}`` for the keys that have a value.
    """

    def __init__(self, loader: Callable[[list[str]], dict]) -> None:
        self._loader = loader
        self._cache: dict = {}

    def prefetch(self, keys: Iterable[str]) -> None:
        """Load every key not loaded yet, in batches."""
        missing = [k for k in dict.fromkeys(keys) if k not in self._cache]
        for start in range(0, len(missing), 500):
            chunk = missing[start : start + 500]
            loaded = self._loader(chunk)
            for k in chunk:
                self._cache[k] = loaded.get(k, _ABSENT)

    def get(self, key, default=None):
        """Return the value for *key*, or *default* when it has none."""
        if key not in self._cache:
            self.prefetch([key])
        value = self._cache[key]
        return default if value is _ABSENT else value

    def __contains__(self, key) -> bool:
        return self.get(key, _ABSENT) is not _ABSENT

    def __getitem__(self, key):
        value = self.get(key, _ABSENT)
        if value is _ABSENT:
            raise KeyError(key)
        return value


class LazyDependencyGraph(DependencyGraph):
    """A :class:`DependencyGraph` that reads only the nodes and links it is asked about.

    The same interface and the same values as :func:`load_dependency_graph`
    (edge lists keep table order), loaded per key through the ``edges``
    from / to indexes, so a caller that judges a few nodes never loads the
    whole graph.  It reads through *conn*, which must stay open while the
    graph is used.

    Args:
        conn: Open connection whose ``row_factory`` yields mapping rows.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:  # noqa: D107
        super().__init__()
        self._conn = conn
        self.kinds = _ReadThrough(self._load_kinds)  # type: ignore[assignment]
        self.documents_out = _ReadThrough(lambda ids: self._load_edges(ids, "documents", "from_id"))  # type: ignore[assignment]
        self.validates_out = _ReadThrough(lambda ids: self._load_edges(ids, "validates", "from_id"))  # type: ignore[assignment]
        self.annotates_out = _ReadThrough(lambda ids: self._load_edges(ids, "annotates", "from_id"))  # type: ignore[assignment]
        self.annotates_rev = _ReadThrough(lambda ids: self._load_edges(ids, "annotates", "to_id"))  # type: ignore[assignment]
        self.composes_out = _ReadThrough(lambda ids: self._load_edges(ids, "composes", "from_id"))  # type: ignore[assignment]
        self.delegates_out = _ReadThrough(self._load_delegates)  # type: ignore[assignment]

    def _load_kinds(self, ids: list[str]) -> dict[str, NodeKind]:
        rows = self._conn.execute(
            f"SELECT id, node_type, subtype, source FROM nodes WHERE id IN ({','.join('?' * len(ids))})", ids
        )
        return {r["id"]: NodeKind(r["node_type"], r["subtype"], r["source"]) for r in rows}

    def _load_edges(self, ids: list[str], edge_type: str, key_column: str) -> dict[str, list[str]]:
        other = "to_id" if key_column == "from_id" else "from_id"
        rows = self._conn.execute(
            f"SELECT {key_column} AS k, {other} AS v FROM edges WHERE edge_type = ? "
            f"AND {key_column} IN ({','.join('?' * len(ids))}) ORDER BY rowid",
            (edge_type, *ids),
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["k"], []).append(r["v"])
        return out

    def _load_delegates(self, ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, targets in self._load_edges(ids, "delegates_to", "from_id").items():
            # Same rule as the full load: the last row wins.
            out[key] = targets[-1]
        return out

    def rebind(self, conn: sqlite3.Connection) -> None:
        """Read what is not loaded yet through *conn* (a caller whose connection changed); loaded entries stay."""
        self._conn = conn

    def prefetch(self, node_ids: Iterable[str]) -> None:
        """Load the identity and every outbound dependency link of *node_ids* in a few queries."""
        ids = list(dict.fromkeys(node_ids))
        self.kinds.prefetch(ids)  # type: ignore[attr-defined]
        for mapping in (self.documents_out, self.validates_out, self.annotates_out, self.composes_out):
            mapping.prefetch(ids)  # type: ignore[attr-defined]


def dependency_targets(graph: DependencyGraph, node_ids: Iterable[str]) -> set[str]:
    """Return every node the statuses of *node_ids* read: dependency targets and digest members.

    Args:
        graph: The links.
        node_ids: The dependents.

    Returns:
        Target ids, plus the members of every digest target among them.
    """
    ids = list(dict.fromkeys(node_ids))
    warm_dependency_sets(graph, ids)
    out: set[str] = set()
    digests: list[str] = []
    for node_id in ids:
        for target in dependency_set(graph, node_id):
            out.add(target)
            kind = graph.kinds.get(target)
            if kind is not None and is_digest_target(kind.node_type, kind.subtype):
                digests.append(target)
    _prefetch_composes_trees(graph, digests)
    for target in dict.fromkeys(digests):
        out.update(digest_members(graph, target))
    return out


__all__ = [
    "ANNOTATES",
    "LazyDependencyGraph",
    "dependency_targets",
    "DELEGATES",
    "DOCUMENTS",
    "VALIDATES",
    "HASHED_COMPOSITE_SUBTYPES",
    "STEP_SUBTYPES",
    "DependencyGraph",
    "Hashes",
    "LiveView",
    "NodeKind",
    "delegates_closure",
    "dependency_set",
    "digest_members",
    "is_digest_target",
    "is_doc_section",
    "is_envelope",
    "is_excluded_link_target",
    "live_value",
    "load_dependency_graph",
    "module_digest",
    "pair_hashes",
    "pair_matches",
    "warm_delegates_closures",
    "warm_dependency_sets",
]
