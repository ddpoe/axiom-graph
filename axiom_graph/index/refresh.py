"""Scoped staleness refresh: recompute only what a change can move.

One primitive, :func:`refresh_staleness`, keeps the stored statuses current
at a cost that follows the change.  It finds the changed nodes (files whose
bytes differ from the bytes their last re-hash read, ``node_history`` rows
past a watermark, or the nodes a caller names), widens them to every node
whose status the change can move (:func:`expand_scope`), and runs the one
recorded staleness pass (:func:`~axiom_graph.index.staleness.record_staleness_pass`)
over that scope.  The pass rules are the same code a full recompute runs,
given a smaller input, so the stored values equal a full recompute's
(``check --full``).

Three ways in:

* **full** -- every node, every file re-hashed: ``check --full``, the first
  pass after an upgrade (no scheme stamp), and any pass whose scheme stamp
  differs from the running package's.
* **incremental** -- the discovery walk over every tracked file plus the
  journal: ``check`` and ``build``.  Moves the journal watermark.
* **cone** -- only the files the named nodes' statuses read, grown until
  every file the scope depends on has been looked at: the write tools and the
  node-naming read tools.  Never moves the watermark, so the next ``check``
  still sees every journal row.

A refresh never adds or removes a node or an edge; it reports structure it
finds (functions or sections the index lacks) for ``build`` to add.

A pass commits its own-phase write before its link phase runs.  A pass that
stops between the two leaves an open-pass entry (``index_meta``); the next
discovery refresh that finds one recovers it before anything else: the
recorded seeds join its changed set, or it runs in full when the open pass
was full.  Each refresh logs one INFO line when its mode is decided and one
when it returns.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import AutoStep, Step, task, workflow

from axiom_graph.index import db
from axiom_graph.index.dependency_set import LazyDependencyGraph, dependency_targets
from axiom_graph.index.file_state import FileObservation, files_to_rehash, observe_file, observe_files
from axiom_graph.index.staleness import REHASH_ALL, STALENESS_RULES, record_staleness_pass
from axiom_graph.models import hash16

if TYPE_CHECKING:
    from axiom_graph.config import AxiomGraphConfig

logger = logging.getLogger(__name__)

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_CONE = "cone"
MODE_LEGACY = "legacy"
MODE_IDLE = "idle"

_DEPENDENCY_EDGE_TYPES = ("documents", "validates", "annotates")
_ENVELOPE_SUBTYPES = ("workflow", "task")


@dataclass
class RefreshResult:
    """What one refresh did.

    Attributes:
        mode: ``full``, ``incremental``, ``cone``, ``idle`` (nothing changed)
            or ``legacy`` (an index below schema v5: a full pass, no records).
        statuses: ``{node_id: (own, link, via)}`` for the nodes the pass evaluated.
        files_observed: How many files the discovery walk (or the cone) read.
        files_hashed: The files re-hashed node by node.
        scope_size: How many nodes the pass evaluated.
        rows_written: How many node rows changed.
        structure: Every tracked file whose last re-hash found structure the
            index lacks: location -> ``{"new": [...], "missing": [...]}``.
        observed: Location -> what this refresh read of each file (the
            discovery walk, the cone, or a full pass's re-hash), for callers
            that decide per file from the same read (``check``'s annotation
            findings).  Empty below schema v5.
    """

    mode: str
    statuses: dict[str, tuple[str, str, list[str]]] = field(default_factory=dict)
    files_observed: int = 0
    files_hashed: set[str] = field(default_factory=set)
    scope_size: int = 0
    rows_written: int = 0
    structure: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    observed: dict[str, FileObservation] = field(default_factory=dict)


def scheme_stamp(transitive_tags: Iterable[str] | None, frozen_tags: Iterable[str] | None) -> str:
    """Return the stamp of everything that decides a stored status besides the inputs themselves.

    The hashing scheme, the staleness rules and the staleness config.  A
    stored stamp that differs (or none) makes the next refresh run in full.

    Args:
        transitive_tags: Configured transitive tags.
        frozen_tags: Configured frozen tags.

    Returns:
        The stamp string.
    """
    from axiom_graph.scanners.node_hashing import HASHING_SCHEME  # noqa: PLC0415

    config = hash16(json.dumps({"transitive": sorted(transitive_tags or []), "frozen": sorted(frozen_tags or [])}))
    return f"h{HASHING_SCHEME}.r{STALENESS_RULES}.c{config}"


# ---------------------------------------------------------------------------
# Graph walks (indexed, per node)
# ---------------------------------------------------------------------------


def _chunks(ids: Iterable[str], size: int = 500):
    ordered = sorted(set(ids))
    for start in range(0, len(ordered), size):
        yield ordered[start : start + size]


def _edges_sql(n_types: int, n_ids: int, *, into: bool) -> str:
    """Return the statement :func:`_edges` runs for *n_types* edge types and a chunk of *n_ids* ids.

    An into-lookup writes its type filter as ``+edge_type``: the covering
    ``(edge_type, from_id, to_id)`` scan index would otherwise win the plan
    on the type alone and walk every edge of the type, where the
    ``(to_id, edge_type)`` index reads only the edges into the chunk.
    """
    types = ",".join("?" * n_types)
    ids = ",".join("?" * n_ids)
    if into:
        return f"SELECT from_id, to_id FROM edges WHERE +edge_type IN ({types}) AND to_id IN ({ids})"
    return f"SELECT from_id, to_id FROM edges WHERE edge_type IN ({types}) AND from_id IN ({ids})"


def _edges(conn, ids: Iterable[str], edge_types: Collection[str], *, into: bool) -> list[tuple[str, str]]:
    """Return ``(from_id, to_id)`` of the edges of *edge_types* into (or out of) *ids*."""
    types = list(edge_types)
    out: list[tuple[str, str]] = []
    for chunk in _chunks(ids):
        out.extend((r[0], r[1]) for r in conn.execute(_edges_sql(len(types), len(chunk), into=into), (*types, *chunk)))
    return out


def _closure(conn, start: Iterable[str], *, upward: bool) -> set[str]:
    """Every ``composes`` ancestor (upward) or descendant of *start*, *start* excluded."""
    seen: set[str] = set()
    frontier = set(start)
    origin = set(start)
    while frontier:
        nxt: set[str] = set()
        for frm, to in _edges(conn, frontier, ("composes",), into=upward):
            other = frm if upward else to
            if other not in seen and other not in origin:
                nxt.add(other)
        seen |= nxt
        frontier = nxt
    return seen


def _reverse_delegates(conn, tasks: Iterable[str]) -> set[str]:
    """Every envelope whose ``delegates_to`` closure reaches one of *tasks*.

    The forward walk goes envelope -> its autosteps -> each autostep's task
    -> the envelopes annotating that task -> on.  This walks it backwards:
    task -> the autosteps delegating to it -> their envelopes, and from each
    envelope the tasks it annotates, which other envelopes may reach.
    """
    envelopes: set[str] = set()
    seen_tasks: set[str] = set()
    frontier = set(tasks)
    while frontier:
        seen_tasks |= frontier
        autosteps = {frm for frm, _to in _edges(conn, frontier, ("delegates_to",), into=True)}
        envs = {frm for frm, _to in _edges(conn, autosteps, ("composes",), into=True)} if autosteps else set()
        new_envs = envs - envelopes
        envelopes |= new_envs
        annotated = {to for _frm, to in _edges(conn, new_envs, ("annotates",), into=False)} if new_envs else set()
        frontier = annotated - seen_tasks
    return envelopes


def _reverse_tagged(conn, targets: set[str], transitive_tags: list[str] | None) -> set[str]:
    """Every section that reaches one of *targets* along tagged doc-to-doc links (Pass 3 sources)."""
    if not transitive_tags:
        return set()
    seen: set[str] = set()
    frontier = set(targets)
    while frontier:
        edges = db.get_tagged_doc_doc_edges_conn(conn, transitive_tags, target_ids=frontier)
        nxt = {e["source_section_id"] for e in edges} - seen - targets
        seen |= nxt
        frontier = nxt
    return seen


@workflow(
    purpose="Widen a set of changed nodes to every node whose stored status the change can move, closed under "
    "composes descendants so composite inheritance reads evaluated children only",
    inputs="open connection, the changed node ids, transitive tags",
    outputs="The evaluation set (existing node ids)",
)
def expand_scope(db_path: Path, conn, changed: Iterable[str], transitive_tags: list[str] | None) -> set[str]:
    """Return the evaluation set for *changed*.

    Args:
        db_path: Unused: every read goes through *conn*.  Kept because a
            caller outside the refresh (``lifecycle.api``) passes it.
        conn: Open connection.
        changed: Nodes whose own inputs changed (files re-hashed, journal
            rows, a caller's verifications or links).  Ids with no node row
            (deleted nodes) still widen the scope through the links that
            name them.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.

    Returns:
        Node ids that exist, closed under ``composes`` descendants.
    """
    口 = Step(
        step_num=1,
        name="Own-dimension reach",
        purpose="The changed nodes and their composes ancestors, whose inherited own status and change rows move with them",
    )
    own_moved = set(changed)
    own_moved |= _closure(conn, own_moved, upward=True)

    口 = Step(
        step_num=2,
        name="Direct dependents",
        purpose="Nodes that document, validate or annotate a moved node, and envelopes whose delegates_to closure reaches one",
    )
    dependents = {frm for frm, _to in _edges(conn, own_moved, _DEPENDENCY_EDGE_TYPES, into=True)}
    dependents |= _reverse_delegates(conn, own_moved)

    口 = Step(
        step_num=3,
        name="Transitive doc-to-doc dependents",
        purpose="Sections whose tagged doc-to-doc links reach a node whose LINKED_STALE membership can move",
    )
    dependents |= _reverse_tagged(conn, dependents | own_moved, transitive_tags)

    口 = Step(
        step_num=4,
        name="Close the scope",
        purpose="Add the composes ancestors of every node in scope (their inherited link status moves) and every "
        "composes descendant (inheritance reads them), then keep the ids that exist",
    )
    scope = own_moved | dependents
    scope |= _closure(conn, scope, upward=True)
    scope |= _closure(conn, scope, upward=False)
    existing: set[str] = set()
    for chunk in _chunks(scope):
        existing.update(
            r[0] for r in conn.execute(f"SELECT id FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        )
    return existing


def _forward_tagged(conn, sections: set[str], transitive_tags: list[str] | None) -> set[str]:
    """Every section reachable from *sections* along tagged doc-to-doc links."""
    if not transitive_tags:
        return set()
    seen: set[str] = set()
    frontier = set(sections)
    while frontier:
        edges = db.get_tagged_doc_doc_edges_conn(conn, transitive_tags, source_ids=frontier)
        nxt = {e["target_section_id"] for e in edges} - seen - sections
        seen |= nxt
        frontier = nxt
    return seen


def dependency_reach(conn, scope: set[str], transitive_tags: list[str] | None) -> set[str]:
    """Return every node the statuses of *scope* read.

    The scope, the sections its tagged doc-to-doc links reach, and every
    dependency target (digest members included) of both.  Targets with no
    node row (deleted, purged) are kept: a journal row may name them.

    Args:
        conn: Open connection.
        scope: The nodes whose statuses are wanted.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.

    Returns:
        Node ids.
    """
    reach = set(scope) | _forward_tagged(conn, set(scope), transitive_tags)
    graph = LazyDependencyGraph(conn)
    graph.prefetch(reach)
    reach |= dependency_targets(graph, reach)
    return reach


def dependency_locations(conn, scope: set[str], transitive_tags: list[str] | None) -> set[str]:
    """Return every file the statuses of *scope* read.

    The scope's own files, the files of every dependency target (digest
    members included) of the scope and of the sections its tagged
    doc-to-doc links reach, and those sections' files.

    Args:
        conn: Open connection.
        scope: The evaluation set.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.

    Returns:
        Project-relative locations.
    """
    return _locations_of(conn, dependency_reach(conn, scope, transitive_tags))


def _record_unmoved_stats(conn, observed: dict[str, FileObservation], moved: set[str], records: dict) -> None:
    """Store the stat of each file read whose bytes still match its last re-hash, when the stat moved.

    Args:
        conn: Open connection.
        observed: What this refresh read of each file.
        moved: The files it re-hashes (their records are written by the pass).
        records: Location -> the stored :class:`~axiom_graph.db.files.FileRecord` of the files read.
    """
    stats = {
        loc: (obs.mtime, obs.size)
        for loc, obs in observed.items()
        if loc not in moved
        and not obs.missing
        and loc in records
        and records[loc].hashed_fp == obs.fingerprint
        and (records[loc].mtime, records[loc].size) != (obs.mtime, obs.size)
    }
    db.record_file_stats_conn(conn, stats)


def _locations_of(conn, node_ids: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for chunk in _chunks(node_ids):
        out.update(
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT location FROM nodes WHERE location != '' AND node_type != 'entity' "
                f"AND COALESCE(subtype, '') != 'external_package' AND id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
        )
    return out


def _ids_at(conn, locations: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for chunk in _chunks(locations):
        out.update(
            r[0] for r in conn.execute(f"SELECT id FROM nodes WHERE location IN ({','.join('?' * len(chunk))})", chunk)
        )
    return out


def _nodes(conn, ids: Iterable[str]) -> list:
    out = []
    for chunk in _chunks(ids):
        out.extend(
            db._row_to_node(r)
            for r in conn.execute(f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        )
    return out


def tracked_locations_conn(conn) -> list[str]:
    """Return every file the index holds a node for (the discovery walk's set).

    Args:
        conn: Open connection.

    Returns:
        Sorted project-relative locations.
    """
    return sorted(db.distinct_locations_conn(conn, tracked_only=True))


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------


@workflow(
    purpose="Keep stored statuses current at the cost of the change: find the changed nodes (discovery walk and "
    "journal, or the files a cone of named nodes reads), widen them to their neighbourhood, and run the one recorded "
    "staleness pass over it; run in full instead on a scheme-stamp mismatch, with no watermark, or when asked",
    inputs="db_path, project_root, staleness config, mode switches, seed nodes and files",
    outputs="RefreshResult: mode, statuses of the evaluated scope, files read and re-hashed, structure found",
)
@db.connection_scope
def refresh_staleness(
    db_path: Path,
    project_root: Path,
    *,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    full: bool = False,
    discover: bool = True,
    seed_node_ids: Iterable[str] = (),
    seed_locations: Iterable[str] = (),
    seeds_changed: bool = True,
    renamed_ids: set[str] | None = None,
    between_passes: Callable[[], None] | None = None,
    walked: Mapping[str, FileObservation] | None = None,
) -> RefreshResult:
    """Bring the stored statuses up to date with the disk and the journal.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.
        full: Recompute every node from a re-hash of every file.
        discover: ``True`` walks every tracked file and reads the whole
            journal, then moves the watermark (``check``, ``build``);
            ``False`` looks only at the files the seed nodes' statuses read
            (the cone) and the journal rows naming them, and leaves the
            watermark alone (write and read tools).
        seed_node_ids: Nodes whose inputs the caller changed or wants shown.
        seed_locations: Files the caller wants re-hashed whatever their
            fingerprint says (a build passes the files it parsed).
        seeds_changed: The caller changed the seed nodes' inputs (a write
            tool's verifications), so their neighbourhood is recomputed even
            when no file moved; ``False`` (a read tool) recomputes only what
            a moved file or a journal row reaches.
        renamed_ids: Node ids renamed by this build (forced RENAMED).
        between_passes: Hook run between the own write and the link phase.
        walked: What the caller's own walk just read (a build's discovery
            walk), location -> observation; the discovery walk reuses it
            and reads only the tracked files it does not cover.

    Returns:
        :class:`RefreshResult`.
    """
    project_root = Path(project_root).resolve()
    seeds = set(seed_node_ids)
    forced = set(seed_locations)

    口 = Step(
        step_num=1,
        name="Choose the mode",
        purpose="Below schema v5 run the legacy full pass; run in full on request, with no stored scheme stamp or a "
        "different one, or (discovery) with no watermark; otherwise incremental (discovery) or cone",
    )
    started = time.perf_counter()
    stamp = scheme_stamp(transitive_tags, frozen_tags)
    with db._connect(db_path) as conn:
        v5 = db.pairs_ready(conn)
        if v5:
            db.ensure_file_state_conn(conn)
        stored_stamp = db.get_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY) if v5 else None
        watermark_raw = db.get_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY) if v5 else None
        # A discovery refresh takes in every open entry (passes that stopped
        # part-way, and the seeds the cones carried); a cone leaves them to
        # the next one (its own scope reads the stored inputs).
        absorbed = db.read_open_passes_conn(conn) if v5 and discover else []
    if not v5:
        logger.info("staleness refresh: %s (index below schema v5)", MODE_LEGACY)
        with db._connect(db_path) as conn:
            nodes = _nodes(conn, [r[0] for r in conn.execute("SELECT id FROM nodes")])
        passed = record_staleness_pass(
            db_path,
            project_root,
            nodes,
            transitive_tags=transitive_tags,
            frozen_tags=frozen_tags,
            renamed_ids=renamed_ids,
            between_passes=between_passes,
        )
        return _logged(
            RefreshResult(
                mode=MODE_LEGACY,
                statuses=passed.statuses,
                files_hashed=passed.files_hashed,
                scope_size=len(nodes),
                rows_written=passed.rows_written,
            ),
            started,
        )
    cold = stored_stamp != stamp or watermark_raw is None
    stopped = [p for p in absorbed if not p.carried]
    carried_ids = {nid for p in absorbed if p.carried for nid in p.ids}
    recover_full = any(p.full for p in absorbed)
    run_full = full or (cold and discover) or recover_full

    if run_full:
        reason = (
            "requested"
            if full
            else "an earlier pass stopped part-way"
            if any(p.full and not p.reason for p in stopped)
            else "an earlier pass's envelope check failed"
            if any(p.full and p.reason == db.OPEN_PASS_ENVELOPE_FAILED for p in stopped)
            else f"tools refreshed more than {db.CARRIED_PASS_MAX_IDS} nodes since the last check"
            if recover_full
            else "no scheme stamp"
            if stored_stamp is None
            else "scheme stamp mismatch"
            if stored_stamp != stamp
            else "no watermark"
        )
        logger.info("staleness refresh: %s (%s)", MODE_FULL, reason)
        口 = AutoStep(step_num=2, name="Full pass")
        refreshed = _full_refresh(
            db_path,
            project_root,
            stamp,
            transitive_tags,
            frozen_tags,
            renamed_ids,
            between_passes,
            absorbed=[p.token for p in absorbed],
        )
        return _logged(refreshed, started)

    watermark = int(watermark_raw or 0)
    with db._connect(db_path) as conn:
        if discover:
            口 = Step(
                step_num=3,
                name="Discovery walk and journal",
                purpose="Fingerprint every tracked file and compare it with the fingerprint its last re-hash read; read "
                "the journal rows past the watermark",
                outputs="the files to re-hash, the changed nodes, the last journal id read",
            )
            locations = tracked_locations_conn(conn)
            if walked:
                observed = {
                    loc: walked[loc] if loc in walked else observe_file(project_root, loc)
                    for loc in dict.fromkeys(locations)
                }
            else:
                observed = observe_files(project_root, locations)
            records = db.get_file_records_conn(conn)
            reset = db.get_reset_locations_conn(conn)
            rehash = files_to_rehash(observed, {loc: r.hashed_fp for loc, r in records.items()}, reset_locations=reset)
            rehash |= forced & set(observed)
            _record_unmoved_stats(conn, observed, rehash, records)
            journal, last_id = db.read_journal_conn(conn, watermark)
            deletion_mark_raw = db.get_index_meta_conn(conn, db.DELETION_MARK_META_KEY)
            last_deletion = db.max_deletion_log_id_conn(conn)
            dangling = db.unflagged_dangling_sources_conn(
                conn, None if deletion_mark_raw is None else int(deletion_mark_raw), last_deletion
            )
            recovered = {nid for p in absorbed for nid in p.ids}
            changed = journal | seeds | _ids_at(conn, rehash) | dangling | recovered
            if not changed:
                # Nothing to recompute: the watermark moves only when a row
                # past it was read (an idle check writes nothing).
                if str(last_id) != watermark_raw:
                    db.set_index_meta_conn(conn, db.STALENESS_WATERMARK_META_KEY, str(last_id))
                _store_deletion_mark(conn, deletion_mark_raw, last_deletion)
                structure = db.get_file_structures_conn(conn)
                logger.info("staleness refresh: %s (no file moved and no journal row past the watermark)", MODE_IDLE)
                return _logged(
                    RefreshResult(mode=MODE_IDLE, files_observed=len(observed), structure=structure, observed=observed),
                    started,
                )
            scope = expand_scope(db_path, conn, changed, transitive_tags)
            mode = MODE_INCREMENTAL
            logger.info(
                "staleness refresh: %s (%d files moved, %d journal rows' nodes%s%s)",
                mode,
                len(rehash),
                len(journal),
                f", re-checking {len(carried_ids)} node(s) tools refreshed since the last check"
                if any(p.carried for p in absorbed)
                else "",
                f", recovering {len(stopped)} pass(es) that stopped part-way" if stopped else "",
            )
        else:
            口 = Step(
                step_num=4,
                name="Cone of the named nodes",
                purpose="Grow the scope from the seed nodes: read the files the scope's statuses depend on, re-hash those "
                "whose bytes moved, take in journal rows naming them, and repeat until no new file is needed",
                outputs="the files to re-hash, the evaluation set",
                critical="Every file a scoped status reads is looked at, so no stored value comes from a file that "
                "changed unseen; the watermark is never moved",
            )
            observed = {}
            rehash = set()
            seen_records: dict = {}
            shown = seeds | _closure(conn, seeds, upward=False)
            changed = (set(seeds) if seeds_changed else set()) | db.read_journal_for_conn(conn, watermark, shown)
            if not seeds_changed:
                # A read takes in the journal rows of everything its statuses
                # read, deleted targets included, so a writer that left only a
                # journal row cannot leave a shown status behind.
                changed |= db.read_journal_for_conn(conn, watermark, dependency_reach(conn, shown, transitive_tags))
            scope = expand_scope(db_path, conn, changed, transitive_tags) if changed else set()
            needed = dependency_locations(conn, scope | shown, transitive_tags) | forced
            reset_all = None
            while True:
                new = needed - set(observed)
                if not new:
                    break
                fresh = observe_files(project_root, new)
                observed.update(fresh)
                records = db.get_file_records_conn(conn, new)
                seen_records.update(records)
                if reset_all is None:
                    reset_all = db.get_reset_locations_conn(conn)
                moved = files_to_rehash(
                    fresh, {loc: r.hashed_fp for loc, r in records.items()}, reset_locations=reset_all
                )
                moved |= forced & set(fresh)
                if cold:
                    # No full pass has stamped this index's hashes yet: the cone
                    # re-hashes every file it reads rather than derive from them.
                    moved |= set(fresh)
                if not moved:
                    break
                rehash |= moved
                grown = _ids_at(conn, moved)
                grown |= db.read_journal_for_conn(conn, watermark, grown)
                if grown <= changed:
                    break
                changed |= grown
                scope = expand_scope(db_path, conn, changed, transitive_tags)
                needed = dependency_locations(conn, scope, transitive_tags)
            _record_unmoved_stats(conn, observed, rehash, seen_records)
            mode = MODE_CONE
            last_id = None
            deletion_mark_raw = last_deletion = None
            logger.info("staleness refresh: %s (%d named nodes, %d files moved)", mode, len(seeds), len(rehash))
        nodes = _nodes(conn, scope)

    if not nodes:
        with db._connect(db_path) as conn:
            if absorbed:
                # The recovered seeds name no node left: nothing to recompute.
                # No write precedes the removal here, so take the write lock
                # first: an entry another pass records meanwhile is kept (a
                # recovery-only path, never a normal refresh's).
                conn.execute("BEGIN IMMEDIATE")
                db.close_passes_conn(conn, [p.token for p in absorbed])
            structure = db.get_file_structures_conn(conn)
        return _logged(
            RefreshResult(mode=MODE_IDLE, files_observed=len(observed), structure=structure, observed=observed),
            started,
        )

    口 = AutoStep(step_num=5, name="Recorded pass over the scope")
    passed = record_staleness_pass(
        db_path,
        project_root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        renamed_ids=renamed_ids,
        between_passes=between_passes,
        rehash=rehash,
        scope={n.id for n in nodes},
        observed=observed,
        open_pass=db.OpenPass(uuid.uuid4().hex, False, frozenset(changed), carried=mode == MODE_CONE),
        prior_fingerprints={loc: r.hashed_fp for loc, r in (records if discover else seen_records).items()},
    )
    口 = Step(
        step_num=6,
        name="Close the refresh",
        purpose="Discovery: move the watermark (past this pass's own journal rows when no other writer's row sits "
        "among them) and the deletion mark, and remove this pass's open entry and the entries it recovered, in one "
        "transaction",
    )
    with db._connect(db_path) as conn:
        if last_id is not None:
            db.set_index_meta_conn(
                conn, db.STALENESS_WATERMARK_META_KEY, str(_watermark_after(conn, last_id, passed.history_ids))
            )
        if last_deletion is not None:
            _store_deletion_mark(conn, deletion_mark_raw, last_deletion)
        if last_id is not None:
            # A cone writes nothing here: it carried its seeds into the
            # carried entry, which the next discovery refresh re-checks.
            closing = [p.token for p in absorbed] + ([passed.open_pass] if passed.open_pass else [])
            if closing:
                db.close_passes_conn(conn, closing)
        structure = db.get_file_structures_conn(conn)
    return _logged(
        RefreshResult(
            mode=mode,
            statuses=passed.statuses,
            files_observed=len(observed),
            files_hashed=passed.files_hashed,
            scope_size=len(nodes),
            rows_written=passed.rows_written,
            structure=structure,
            observed={**observed, **passed.observed},
        ),
        started,
    )


def _logged(result: RefreshResult, started: float) -> RefreshResult:
    """Log the one INFO line a refresh ends with, and return *result*."""
    logger.info(
        "staleness refresh: %s done in %.3fs (%d files observed, %d re-hashed, scope %d nodes, %d rows written)",
        result.mode,
        time.perf_counter() - started,
        result.files_observed,
        len(result.files_hashed),
        result.scope_size,
        result.rows_written,
    )
    return result


def _watermark_after(conn, last_id: int, own_ids: list[int]) -> int:
    """Return the watermark a discovery refresh stores after its pass.

    The last journal id it read, or past the pass's own ``BECAME_*`` rows
    when every non-inert row between them is one of the pass's own: those
    rows record transitions the pass computed over a scope closed under
    everything they can move, so reading them again recomputes nothing.  A
    row another writer added among them keeps the watermark where it was.

    Args:
        conn: Open connection.
        last_id: The last journal id the refresh read before its pass.
        own_ids: The ids of the history rows the pass wrote.

    Returns:
        The watermark.
    """
    mine = [i for i in own_ids if i > last_id]
    if not mine:
        return last_id
    top = max(mine)
    if db.journal_rows_between_conn(conn, last_id, top) == len(mine):
        return top
    return last_id


@task(
    purpose="Recompute every node from a re-hash of every file in one recorded pass, then record the scheme stamp "
    "and move the watermark to the last journal row",
    inputs="db_path, project_root, scheme stamp, staleness config, renamed ids, between-passes hook",
    outputs="RefreshResult in full mode",
)
def _full_refresh(
    db_path: Path,
    project_root: Path,
    stamp: str,
    transitive_tags: list[str] | None,
    frozen_tags: list[str] | None,
    renamed_ids: set[str] | None,
    between_passes: Callable[[], None] | None,
    *,
    absorbed: Collection[str] = (),
) -> RefreshResult:
    """Recompute every node from a re-hash of every file, then record the stamp and the watermark.

    Removes, with the watermark, its own open-pass entry and the *absorbed*
    entries (passes that stopped part-way and the cones' carried seeds,
    which a full pass covers).
    """
    with db._connect(db_path) as conn:
        last_id = db.max_history_id_conn(conn)
        deletion_mark_raw = db.get_index_meta_conn(conn, db.DELETION_MARK_META_KEY)
        last_deletion = db.max_deletion_log_id_conn(conn)
        nodes = _nodes(conn, [r[0] for r in conn.execute("SELECT id FROM nodes")])
    passed = record_staleness_pass(
        db_path,
        project_root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        renamed_ids=renamed_ids,
        between_passes=between_passes,
        rehash=REHASH_ALL,
        open_pass=db.OpenPass(uuid.uuid4().hex, True, frozenset()),
    )
    with db._connect(db_path) as conn:
        db.set_index_meta_conn(conn, db.STALENESS_SCHEME_META_KEY, stamp)
        db.set_index_meta_conn(
            conn, db.STALENESS_WATERMARK_META_KEY, str(_watermark_after(conn, last_id, passed.history_ids))
        )
        _store_deletion_mark(conn, deletion_mark_raw, last_deletion)
        closing = [*absorbed, *([passed.open_pass] if passed.open_pass else [])]
        if closing:
            db.close_passes_conn(conn, closing)
        structure = db.get_file_structures_conn(conn)
    return RefreshResult(
        mode=MODE_FULL,
        statuses=passed.statuses,
        files_observed=len(passed.files_hashed),
        files_hashed=passed.files_hashed,
        scope_size=len(nodes),
        rows_written=passed.rows_written,
        structure=structure,
        observed=passed.observed,
    )


def _store_deletion_mark(conn, stored_raw: str | None, last_deletion: int) -> None:
    """Record that a discovery refresh consumed the deletion log through *last_deletion*.

    Writes only when the mark moves (or was never stored), and drops the
    consumed log rows in the same transaction.

    Args:
        conn: Open connection (caller owns the transaction).
        stored_raw: The mark as read before the refresh, or ``None``.
        last_deletion: The newest log id the refresh read.
    """
    if stored_raw == str(last_deletion):
        return
    db.set_index_meta_conn(conn, db.DELETION_MARK_META_KEY, str(last_deletion))
    db.drop_deletion_log_through_conn(conn, last_deletion)


def refresh_after_write(
    db_path: Path,
    project_root: Path,
    node_ids: Iterable[str],
    *,
    locations: Iterable[str] = (),
    config: AxiomGraphConfig | None = None,
) -> RefreshResult:
    """The one closing refresh of a write tool: the cone of the nodes it wrote.

    Recomputes the statuses the write can move (the written nodes, ids it
    deleted, and their neighbourhood) from the files they read, with the
    project's staleness config.  Never moves the journal watermark.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        node_ids: Nodes the write verified, re-indexed, linked or deleted.
        locations: Files the write parsed, re-hashed whatever their fingerprint says.
        config: The project config the write already loaded; loaded here when ``None``.

    Returns:
        :class:`RefreshResult`.
    """
    if config is None:
        from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

        config = AxiomGraphConfig.load(Path(project_root))
    return refresh_staleness(
        db_path,
        project_root,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
        discover=False,
        seed_node_ids=node_ids,
        seed_locations=locations,
        seeds_changed=True,
    )


def refresh_before_write(
    db_path: Path,
    project_root: Path,
    node_ids: Iterable[str],
    *,
    config: AxiomGraphConfig | None = None,
) -> RefreshResult | None:
    """The pre-write refresh of a doc write that reads open offenders: the cone of the nodes it names, as a read.

    Brings the named nodes' stored statuses up to date with the disk and the
    journal (a moved file in their cone is re-hashed, a journal row naming
    what they read is taken in), so the offenders the write validates, pins
    and stamps against are the ones ``check --full`` would list.  Recomputes
    only what a moved file or a journal row reaches, and never moves the
    journal watermark.  Does nothing below schema v5 (no receipts to keep).

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        node_ids: The sections the write verifies or names.
        config: The project config the write already loaded; loaded here when ``None``.

    Returns:
        :class:`RefreshResult`, or ``None`` when nothing was refreshed.
    """
    ids = set(node_ids)
    if not ids:
        return None
    with db._connect(db_path) as conn:
        if not db.pairs_ready(conn):
            return None
    if config is None:
        from axiom_graph.config import AxiomGraphConfig  # noqa: PLC0415

        config = AxiomGraphConfig.load(Path(project_root))
    return refresh_staleness(
        db_path,
        project_root,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
        discover=False,
        seed_node_ids=ids,
        seeds_changed=False,
    )


def observation_records(observed: dict[str, FileObservation]) -> dict[str, tuple]:
    """Return ``{location: (fingerprint, mtime, size)}`` for the record writers."""
    return {loc: obs.as_record() for loc, obs in observed.items()}


__all__ = [
    "MODE_CONE",
    "MODE_FULL",
    "MODE_IDLE",
    "MODE_INCREMENTAL",
    "MODE_LEGACY",
    "RefreshResult",
    "dependency_locations",
    "dependency_reach",
    "expand_scope",
    "observation_records",
    "refresh_after_write",
    "refresh_staleness",
    "scheme_stamp",
    "tracked_locations_conn",
]
