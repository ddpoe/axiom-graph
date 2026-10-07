"""Shared staleness engine — single writer for all staleness computation.

Every consumer reaches the one recorded pass, :func:`record_staleness_pass`,
directly or through :func:`record_staleness` /
:func:`record_staleness_settled` and the scoped refresh
(:mod:`axiom_graph.index.refresh`), or reads the persisted status columns.
No other module computes or persists staleness independently.

Each rule exists once.  Every pass takes an optional scope: with none it
covers every node (the reference ``check --full`` runs); with one it decides
only that set, which the scoped refresh closes under every dependency the
rules read, so the stored values equal a full recompute's.

``compute_staleness()`` remains a pure computation (returns dict, no side
effects).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from axiom_graph.index import db
from axiom_graph.index.dependency_set import (
    DependencyGraph,
    LazyDependencyGraph,
    LiveView,
    dependency_set,
    dependency_targets,
    delegates_closure,
    load_dependency_graph,
    pair_matches,
    warm_delegates_closures,
)
from axiom_graph.index import file_state as _file_state
from axiom_graph.index.file_state import FileObservation, file_unchanged_since
from axiom_graph.index.mark_clean import VERIFICATION_OP_REVERIFY
from axiom_graph.index.git_utils import get_git_sha
from axiom_graph.index.status import (
    VERIFIED,
    CONTENT_UPDATED,
    DESC_UPDATED,
    RENAMED,
    NOT_FOUND,
    LINKED_STALE,
    BROKEN_LINK,
    BECAME_CONTENT_UPDATED,
    BECAME_DESC_UPDATED,
    BECAME_NOT_FOUND,
    BECAME_RENAMED,
    BECAME_VERIFIED,
    BECAME_LINKED_STALE,
    BECAME_BROKEN_LINK,
    LINK_BECAME_VERIFIED,
    OWN_SEVERITY as _STATUS_OWN_SEVERITY,
    LINK_SEVERITY as _STATUS_LINK_SEVERITY,
)
from axiom_annotations import workflow, task, Step, AutoStep

logger = logging.getLogger(__name__)

# File extensions whose module nodes are hashed from raw decoded bytes
# (tree-sitter scanner: ``read_bytes().decode(...)``), NOT from universal-newline
# ``read_text``.  The content gate must read these the SAME way so its hash is
# comparable to the stored ``code_hash``.  This set must exactly match the
# extensions the builder actually discovers and scans (``.js/.jsx/.ts/.tsx``;
# see ``_iter_js_files`` at builder.py:941 and the scan dispatch at
# builder.py:1140) — any extra extension here is unreachable, since no anchor
# code_hash is ever produced for a file the scanner never visits.
_JS_TS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})

# Subtypes that mark a single file-level anchor node carrying a whole-file
# ``code_hash``: Python / JS-TS modules (``module``), DocJSON composites
# (``docjson``), and config files (``config``).  Selecting the anchor by this
# set avoids any ``subtype is None`` test.
_ANCHOR_SUBTYPES = frozenset({"module", "docjson", "docjson_doc", "config"})


def _file_anchor(loc_nodes: list):
    """Return a location's file-level anchor: the node whose ``code_hash`` fingerprints the whole file.

    Args:
        loc_nodes: Every node whose location resolves to the file.

    Returns:
        The first node with a subtype in :data:`_ANCHOR_SUBTYPES` and a
        non-empty ``code_hash``, or ``None`` when there is none.
    """
    for n in loc_nodes:
        if getattr(n, "subtype", None) in _ANCHOR_SUBTYPES and getattr(n, "code_hash", None):
            return n
    return None


def _file_fingerprint(abs_path: "Path") -> str | None:
    """Return the whole-file hash an anchor's ``code_hash`` is comparable to.

    The file is read the SAME way its scanner hashes it (read-mode parity is
    load-bearing): JS/TS via ``read_bytes().decode`` (no newline normalization),
    everything else via universal-newline ``read_text``.

    Args:
        abs_path: Absolute path to the file on disk.

    Returns:
        ``hash16`` of the file's text, or ``None`` when it cannot be read.
    """
    return _file_state.file_fingerprint(abs_path)


def _fingerprint_vouches(anchor, content_fp: str | None, last_hashed: str | None) -> bool:
    """Whether the file's fingerprint lets the fast pass vouch for its nodes.

    Two fingerprints must both describe the file as it is now.  The anchor's
    ``code_hash`` is the file as it was scanned.  The last-hashed fingerprint
    is the file as the last staleness pass that re-hashed it read it, which
    is when every node's live hash was taken; ``None`` when no pass has.  A
    file that moved away from the scanned content and came back to it
    matches the first and not the second, so its nodes take the ladder and
    their live hashes are taken again.  The reset marker ``''`` a
    verification leaves on the anchor never matches, so the anchor takes
    the ladder once.  Requiring both only removes fast passes compared with
    the scanned fingerprint alone.

    Args:
        anchor: The location's file-level anchor (:func:`_file_anchor`), or ``None``.
        content_fp: The file's current fingerprint (:func:`_file_fingerprint`).
        last_hashed: The anchor's last-hashed fingerprint, or ``None``.

    Returns:
        ``True`` when the anchor exists and the file matches its scanned
        fingerprint and, when one is stored, its last-hashed fingerprint.
    """
    if anchor is None or content_fp is None or content_fp != anchor.code_hash:
        return False
    return last_hashed is None or last_hashed == content_fp


def _file_content_matches_anchor(abs_path: "Path", loc_nodes: list) -> bool:
    """Whether the file's current bytes match its indexed file-level anchor.

    Backs the staleness step-2 mtime fast-pass: even when mtime says
    "unchanged", confirm the content against the anchor node's whole-file
    ``code_hash`` before blanket-verifying (see :func:`_file_fingerprint` for
    the read-mode parity rule).

    Args:
        abs_path: Absolute path to the file on disk.
        loc_nodes: Every node whose location resolves to ``abs_path``.

    Returns:
        ``True`` only when a file-level anchor with a non-empty ``code_hash``
        exists AND a freshly computed ``hash16`` of the file equals it.
        ``False`` when no anchor is found, its ``code_hash`` is empty, or the
        hashes differ — callers must then fall through to the per-node ladder
        rather than blanket-verify.
    """
    anchor = _file_anchor(loc_nodes)
    if anchor is None:
        return False
    return _fingerprint_vouches(anchor, _file_fingerprint(abs_path), None)


def _location_persisted_verified(loc_nodes: list, persisted_own: Mapping[str, str | None]) -> bool:
    """Whether every non-anchor node at a location is persisted VERIFIED.

    Backs the staleness step-2 mtime fast pass: blanket-verifying a
    location is only a re-confirmation, so it is allowed only when the
    location's nodes are already VERIFIED.  File-level anchors (subtype in
    :data:`_ANCHOR_SUBTYPES`) are exempt — a freshly re-created anchor has
    no meaningful persisted status, and the content gate checks it anyway.

    Args:
        loc_nodes: Every node whose location resolves to the file.
        persisted_own: Node ID -> persisted ``own_status`` (missing or
            ``None`` counts as not VERIFIED).

    Returns:
        ``True`` when no non-anchor node at the location is persisted with
        anything other than ``VERIFIED``.
    """
    for n in loc_nodes:
        if getattr(n, "subtype", None) in _ANCHOR_SUBTYPES:
            continue
        if persisted_own.get(n.id) != VERIFIED:
            return False
    return True


# ---------------------------------------------------------------------------
# Severity ladders (two-column split)
# ---------------------------------------------------------------------------

# Own-content dimension: tracks whether this node's own content has changed.
_OWN_SEVERITY: dict[str, int] = _STATUS_OWN_SEVERITY

_OWN_SEVERITY_TO_STATUS = [VERIFIED, DESC_UPDATED, CONTENT_UPDATED, RENAMED, NOT_FOUND]

# Link dimension: tracks whether dependencies this node points at are stale.
_LINK_SEVERITY: dict[str, int] = _STATUS_LINK_SEVERITY

_LINK_SEVERITY_TO_STATUS = [VERIFIED, LINKED_STALE, BROKEN_LINK]


# ---------------------------------------------------------------------------
# Transition-event helpers
# ---------------------------------------------------------------------------

# Statuses considered "good" — transitions between them are not recorded.
_GOOD_STATUSES = {VERIFIED}


def _transition_change_type(
    old: tuple[str, str],
    new: tuple[str, str],
) -> list[str]:
    """Map a staleness transition to its ``BECAME_*`` change type(s).

    Each dimension is diffed independently, and 0-2 events are returned.

    Returns an empty list when no transition event should be recorded.
    """
    events: list[str] = []
    old_own, old_link = old
    new_own, new_link = new
    # Own dimension
    own_event = _own_transition(old_own, new_own)
    if own_event:
        events.append(own_event)
    # Link dimension
    link_event = _link_transition(old_link, new_link)
    if link_event:
        events.append(link_event)
    return events


def _own_transition(old: str, new: str) -> str | None:
    """Map an own-status transition to a BECAME_* event string."""
    if old == new:
        return None
    if old in _GOOD_STATUSES and new in _GOOD_STATUSES:
        return None
    # Stale -> VERIFIED: hashes realigned (promotion or re-verification)
    if new == VERIFIED and old not in _GOOD_STATUSES:
        return BECAME_VERIFIED
    _map = {
        CONTENT_UPDATED: BECAME_CONTENT_UPDATED,
        DESC_UPDATED: BECAME_DESC_UPDATED,
        RENAMED: BECAME_RENAMED,
        NOT_FOUND: BECAME_NOT_FOUND,
    }
    return _map.get(new)


def _link_transition(old: str, new: str) -> str | None:
    """Map a link-status transition to a BECAME_* event string."""
    if old == new:
        return None
    if old in _GOOD_STATUSES and new in _GOOD_STATUSES:
        return None
    _map = {
        LINKED_STALE: BECAME_LINKED_STALE,
        BROKEN_LINK: BECAME_BROKEN_LINK,
    }
    if new in _map:
        return _map[new]
    # Link dimension: stale -> VERIFIED
    if new in _GOOD_STATUSES and old not in _GOOD_STATUSES:
        return LINK_BECAME_VERIFIED
    return None


# _get_linked_stale_map is retired — its functionality is subsumed by the
# dict[str, list[str]] return value of _get_linked_stale_ids.  The via data
# now flows through compute_staleness as the third tuple element.


# ---------------------------------------------------------------------------
# Broken-link detection
# ---------------------------------------------------------------------------


# One definition, shared with the incremental refresh's dangling-link lookup
# (``db.unflagged_dangling_sources_conn``), so the two cannot drift apart.
_BROKEN_LINK_EDGE_TYPES = db.BROKEN_LINK_EDGE_TYPES


def find_broken_links(db_path: Path, scope: Collection[str] | None = None) -> dict[str, str]:
    """Find edges whose to_id has no matching node in the index.

    Covers the user-facing link types — ``documents``, ``validates``, and
    ``delegates_to``.  A workflow step whose delegate target names nothing
    is the same class of defect as a doc link pointing at a deleted
    function: navigation dead-ends and consumers print an identifier that
    resolves to no source.

    A finding on a step node is attributed to the envelope that composes
    it.  The envelope is the node a maintainer acts on, and it already
    receives the transitive link signal that walks through its steps;
    step nodes otherwise carry no staleness dimensions, since staleness
    computation assigns their subtype a blanket VERIFIED.

    A step no ``workflow`` / ``task`` composes — a leftover from an edit
    that renumbered or removed the enclosing function, which nothing
    retires — keeps the finding on itself.  That is deliberate: its own id
    is the only identifier that names the leftover, whereas charging the
    surrounding module would flag a live node for a step no scan emits.
    The status is stable there, because the BROKEN_LINK overlay in
    :func:`record_staleness` outranks the blanket VERIFIED and is
    reapplied on every recompute for as long as the edge dangles.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        scope: Only edges leaving these nodes (an evaluation set closed
            under ``composes`` descendants, so a step's envelope is in it
            whenever the step is); ``None`` for every edge.

    Returns:
        Dict mapping source node ID to the dangling target node ID (the
        smallest one when a source has several).
    """
    sql = _broken_link_rows_sql()
    with db._connect(db_path) as conn:
        if scope is None:
            rows = conn.execute(sql, _BROKEN_LINK_EDGE_TYPES).fetchall()
        else:
            rows = []
            ids = sorted(set(scope))
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                rows.extend(
                    conn.execute(
                        f"{sql} AND e.from_id IN ({','.join('?' * len(chunk))})", (*_BROKEN_LINK_EDGE_TYPES, *chunk)
                    ).fetchall()
                )
    result: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: (r["from_id"], r["to_id"])):
        result.setdefault(r["from_id"], r["to_id"])
    return result


def _broken_link_rows_sql() -> str:
    """Return the query of every dangling link, ``(from_id, to_id)`` with a step attributed to its envelope.

    Its parameters are :data:`_BROKEN_LINK_EDGE_TYPES`; a caller may append
    ``AND`` filters on ``e``.
    """
    placeholders = ", ".join("?" * len(_BROKEN_LINK_EDGE_TYPES))
    return f"""
            SELECT
                {db.charged_source_sql()} AS from_id,
                e.to_id AS to_id
            FROM edges e
            LEFT JOIN nodes src ON src.id = e.from_id
            LEFT JOIN nodes n ON n.id = e.to_id
            WHERE e.edge_type IN ({placeholders})
              AND n.id IS NULL
            """


def count_broken_link_sources(db_path: Path) -> int:
    """Return how many nodes :func:`find_broken_links` names over the whole index (one query).

    The number a build reports as ``broken links``: every source, after
    step-to-envelope attribution, of a link whose target has no node.

    Args:
        db_path: Path to the axiom-graph SQLite database.

    Returns:
        The count.
    """
    with db._connect(db_path) as conn:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT from_id) FROM ({_broken_link_rows_sql()})",  # noqa: S608 - fixed text
            _BROKEN_LINK_EDGE_TYPES,
        ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# record_staleness — unified write point for staleness + transition events
# ---------------------------------------------------------------------------


@task(
    purpose="Compute staleness in one settled pass and persist it: own statuses, live hashes and own transition "
    "rows first, then the link statuses and link transition rows read with those rows already written; only rows "
    "whose values changed are written",
    inputs="db_path, project_root, list of AxiomNode objects",
    outputs="dict[str, tuple[str, str, list[str]]] — three-column statuses (own_status, link_status, via)",
)
def record_staleness(
    db_path: Path,
    project_root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    renamed_ids: set[str] | None = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Compute staleness and record transition events: the single write point.

    One settled pass (:func:`record_staleness_pass`): the own phase's
    statuses, live hashes and transition rows are written before the link
    phase runs, so a change this pass detects is linked-stale in the same
    call.  Every node in *nodes* gets a status.  On a schema-v5 index the
    hashes computed for each re-hashed node are stored as its live hashes
    (``live_code_hash`` / ``live_desc_hash``; a node its file no longer
    holds stores the missing marker), and each re-hashed file's
    fingerprint is stored in its per-file record, which the next pass's
    fast pass compares the file with.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        nodes: AxiomNode objects to compute and persist.
        transitive_tags: Doc-level tags for transitive LINKED_STALE.
        frozen_tags: Doc-level tags whose sections are immune to LINKED_STALE.
        renamed_ids: New node IDs that received a migrated history/edges from
            a rename *this build*.  Their own_status is forced to ``RENAMED``.
            ``RENAMED`` is also *sticky*: a node whose persisted own_status is
            already ``RENAMED`` keeps it (until ``mark_clean`` or until the
            node is genuinely lost -> ``NOT_FOUND``), since there is no hash
            signal that re-derives "this was renamed".

    Returns:
        ``{node_id: (own_status, link_status, via)}`` for every node in *nodes*.
    """
    return record_staleness_pass(
        db_path,
        project_root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        renamed_ids=renamed_ids,
    ).statuses


@task(
    purpose=(
        "Record staleness in one settled pass, with the caller's between-passes hook run after the own phase's "
        "rows are written and before the link phase reads them"
    ),
    inputs="db_path, project_root, list of AxiomNode objects, optional between-passes callback",
    outputs="dict[str, tuple[str, str, list[str]]] — the statuses the pass stored",
)
def record_staleness_settled(
    db_path: Path,
    project_root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    renamed_ids: set[str] | None = None,
    between_passes: Callable[[], None] | None = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Record staleness so a change detected now is linked-stale now, in one pass.

    The ``documents`` and ``validates`` links read a code node's change
    from its node_history rows.  The pass writes the own phase's
    ``BECAME_*`` rows before the link phase reads them, so the
    ``LINKED_STALE`` a change causes on the doc sections that document it
    and the tests that validate it is stored by the same call, and the
    stored statuses are what the next pass would compute.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        nodes: AxiomNode objects to compute and persist.
        transitive_tags: Doc-level tags for transitive LINKED_STALE.
        frozen_tags: Doc-level tags whose sections are immune to LINKED_STALE.
        renamed_ids: Node ids renamed by this build; see :func:`record_staleness`.
        between_passes: Called after the own phase is written and before
            the link phase runs.  A build passes its re-stamp of the
            verifications it wrote.

    Returns:
        ``{node_id: (own_status, link_status, via)}``.
    """
    return record_staleness_pass(
        db_path,
        project_root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        renamed_ids=renamed_ids,
        between_passes=between_passes,
    ).statuses


@dataclass
class PassResult:
    """What one recorded staleness pass computed and wrote.

    Attributes:
        statuses: ``{node_id: (own, link, via)}`` for every node passed in.
        rows_written: How many node rows the pass updated.
        files_hashed: The files the pass re-hashed (the per-node ladder).
        structure: Per re-hashed file, the names its parse found that the
            index lacks (``"new"``) and the indexed nodes it could not find
            (``"missing"``).
        observed: Location -> what the pass read of each file it re-hashed
            (missing files included).
        history_ids: The ids of the ``node_history`` rows the pass wrote.
        open_pass: The token of the open-pass entry the own-phase write
            recorded, or ``None`` (none recorded, or a cone's seeds merged
            into the carried entry, which the next discovery refresh
            removes); the caller removes it in its last write transaction.
    """

    statuses: dict[str, tuple[str, str, list[str]]] = field(default_factory=dict)
    rows_written: int = 0
    files_hashed: set[str] = field(default_factory=set)
    structure: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    observed: dict[str, FileObservation] = field(default_factory=dict)
    history_ids: list[int] = field(default_factory=list)
    open_pass: str | None = None


@workflow(
    purpose="Run one settled staleness pass over a scope or every node: the own phase (re-hash what the gate or the caller names, derive the rest), write own statuses, live hashes, file records and own transitions, run the "
    "between-passes hook, then the link phase reading those rows, and write link statuses and link transitions; "
    "only changed rows are written",
    inputs="db_path, project_root, the nodes to evaluate, staleness config, rehash choice, scope, observations",
    outputs="PassResult: statuses, rows written, files re-hashed, structure found, files observed",
)
def record_staleness_pass(
    db_path: Path,
    project_root: Path,
    nodes: list,
    *,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    renamed_ids: set[str] | None = None,
    between_passes: Callable[[], None] | None = None,
    rehash: object = None,
    scope: Collection[str] | None = None,
    observed: Mapping[str, FileObservation] | None = None,
    open_pass: db.OpenPass | None = None,
    prior_fingerprints: Mapping[str, str | None] | None = None,
) -> PassResult:
    """Run one settled staleness pass over *nodes* and persist what changed.

    The order (one pass, no second round):

    1. the own phase: re-hash the files it must, derive the rest from the
       stored live hashes, inherit and promote own statuses, overlay RENAMED;
    2. write the own statuses that changed, the live hashes, the per-file
       records of the re-hashed files and the own transition rows;
    3. run *between_passes*;
    4. the link phase, which reads the rows step 2 wrote;
    5. write the link statuses that changed and the link transition rows.

    With *scope* the passes evaluate only that set, which must be closed
    under ``composes`` descendants and hold every node whose status the
    change can move (the scoped refresh computes it); the rules are the
    same code, so the stored values equal a full recompute's.

    The own-phase write commits before the link phase runs, so a pass that
    stops between the two (an exception, an interrupt, a busy timeout) has
    stored file fingerprints and live hashes the next discovery would take
    as already seen.  With *open_pass*, the own-phase write records the pass
    as open in the same transaction whenever it stores such an input (a
    whole file's fingerprint, or an own status with no journal row); the
    next discovery refresh that finds the entry recovers it.  A cone's entry
    is *carried*: its seeds merge into the one carried entry, which the
    next discovery refresh re-checks whether or not the cone finished.

    When the in-memory envelope pass (Pass A'/B') fails, the pass still
    finishes and stores its other statuses, and the link-phase write
    records an open full entry no caller removes, so the next discovery
    refresh runs in full and stores the envelope flags the failure missed.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        nodes: The nodes to evaluate and persist (every node, or the scope).
        transitive_tags: Doc-level tags for transitive LINKED_STALE.
        frozen_tags: Doc-level tags whose sections are immune to LINKED_STALE.
        renamed_ids: Node ids renamed by this build (forced RENAMED).
        between_passes: Hook run between the own write and the link phase.
        rehash: ``None`` for the fast-pass gate per file, :data:`REHASH_ALL`
            to re-hash every file, or the set of files to re-hash (every
            other file's nodes are derived from their stored live hashes).
        scope: The evaluation set, or ``None`` for every node.
        observed: The discovery walk's observations, reused for fingerprints.
        open_pass: The entry to record when the own-phase write stores an
            input the next discovery would not find again, or ``None``.  A
            carried entry merges into the carried entry and is never named
            in :attr:`PassResult.open_pass`; any other is, for the caller.
        prior_fingerprints: Location -> the fingerprint the caller read from
            the file's record before the pass; a whole-file re-hash that
            stores the same fingerprint hides nothing and does not open the
            pass.  ``None`` counts every whole-file re-hash as new.

    Returns:
        :class:`PassResult`.
    """
    started = time.perf_counter()
    result = PassResult()
    requested = sorted({n.id for n in nodes})
    口 = Step(
        step_num=1,
        name="Own phase",
        purpose="Re-hash the files the gate or the caller names, derive every other node's hashes from its stored "
        "live hashes, inherit and promote own statuses",
    )
    ph = _own_phase(db_path, project_root, nodes, rehash=rehash, observed=observed)
    result.files_hashed = set(ph.laddered)
    result.structure = dict(ph.structure)
    result.observed = dict(ph.hashed_files)

    own_inh = _inherit_own(ph, db_path, scope)
    own_final = _promote(own_inh, nodes, ph.hashes, db_path, scope)
    for nid in requested:
        own_final.setdefault(nid, VERIFIED)

    git_sha_cache: list[str | None] = []

    def _git_sha() -> str | None:
        if not git_sha_cache:
            git_sha_cache.append(get_git_sha(project_root))
        return git_sha_cache[0]

    renamed = renamed_ids or set()
    口 = Step(
        step_num=2,
        name="Write the own phase",
        purpose="Overlay RENAMED; write the own statuses and live hashes that changed, the own transition rows, "
        "and the fingerprints of the files re-hashed whole; record the pass as open when the write stores an input "
        "the next discovery would not find again (a cone merges its seeds into the one carried entry, which the next "
        "discovery refresh re-checks and removes)",
        critical="The open-pass entry is written in this transaction, never in one of its own, so the mark costs no "
        "commit; it is written only after another write in the transaction",
    )
    with db._connect(db_path) as conn:
        old = db.get_live_rows_conn(conn, requested)
        write_live = db.pairs_ready(conn)
        consumed = False
        for nid in requested:
            own = own_final[nid]
            prior = old.get(nid)
            if own == NOT_FOUND or prior is None:
                continue
            if nid in renamed or prior["own_status"] == RENAMED:
                own_final[nid] = RENAMED
        scanned_at = db._now_utc()
        for nid in requested:
            prior = old.get(nid)
            if prior is None:
                continue
            old_own, old_link = prior["own_status"], prior["link_status"]
            new_own = own_final[nid]
            event = _own_transition(old_own, new_own)
            if event:
                meta: dict = {"from_own": old_own, "from_link": old_link}
                if event == BECAME_VERIFIED and nid in ph.realigned:
                    meta[db.REALIGNED_META_KEY] = True
                cur = conn.execute(
                    "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (nid, scanned_at, event, _git_sha(), json.dumps(meta)),
                )
                result.history_ids.append(int(cur.lastrowid))
            elif new_own != old_own:
                consumed = True
            live = ph.live_writes.get(nid) if write_live else None
            live_changed = live is not None and (live[0], live[1]) != (
                prior["live_code_hash"],
                prior["live_desc_hash"],
            )
            if live_changed:
                conn.execute(
                    "UPDATE nodes SET own_status = ?, live_code_hash = ?, live_desc_hash = ? WHERE id = ?",
                    (new_own, live[0], live[1], nid),
                )
                result.rows_written += 1
            elif new_own != old_own:
                conn.execute("UPDATE nodes SET own_status = ? WHERE id = ?", (new_own, nid))
                result.rows_written += 1
        if write_live and ph.hashed_files:
            # A file's fingerprint may vouch for its nodes' live hashes only
            # when this pass re-hashed every one of them; a pass over part of
            # a file forgets the fingerprint instead, so the next pass
            # re-hashes the whole file.
            db.ensure_file_state_conn(conn)
            passed = {n.id for n in nodes}
            held = db.get_hashed_ids_at_conn(conn, ph.hashed_files)
            whole = {loc: obs for loc, obs in ph.hashed_files.items() if held.get(loc, set()) <= passed}
            db.record_hashed_files_conn(conn, {loc: obs.as_record() for loc, obs in whole.items()})
            db.record_file_structure_conn(conn, {loc: ph.structure.get(loc) for loc in whole})
            db.clear_hashed_fingerprints_conn(conn, [loc for loc in ph.hashed_files if loc not in whole])
            consumed = consumed or any(
                prior_fingerprints is None
                or prior_fingerprints.get(loc)
                != (obs.fingerprint if obs.fingerprint is not None else db.MISSING_FILE_FP)
                for loc, obs in whole.items()
            )
        if open_pass is not None and write_live and consumed:
            # A recorded fingerprint (or an own status no journal row names)
            # hides this pass's input from the next discovery until the link
            # phase is stored too: record the pass as open until then (a
            # cone carries its seeds to the next discovery refresh instead).
            db.open_pass_conn(conn, open_pass.token, full=open_pass.full, ids=open_pass.ids, carried=open_pass.carried)
            result.open_pass = None if open_pass.carried else open_pass.token
    logger.debug(
        "staleness pass: own phase written (%d nodes, %d files re-hashed, %.3fs)",
        len(requested),
        len(result.files_hashed),
        time.perf_counter() - started,
    )

    口 = Step(
        step_num=3,
        name="Between passes",
        purpose="Run the caller's hook after the own rows are written and before the link phase reads them",
    )
    if between_passes is not None:
        between_passes()

    口 = Step(
        step_num=4,
        name="Link phase",
        purpose="Link statuses read with this pass's own rows already written, composite inheritance, broken links",
    )
    envelope_failures: list[str] = []
    link, via_map = _link_phase(
        db_path,
        ph,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        scope=scope,
        failures=envelope_failures,
    )
    merged = {nid: (own_inh.get(nid, VERIFIED), link.get(nid, VERIFIED)) for nid in set(own_inh) | set(link)}
    apply_composite_inheritance(merged, db_path, scope=scope, frozen_tags=frozen_tags)
    link_final = {nid: lk for nid, (_o, lk) in merged.items()}
    for node_id in find_broken_links(db_path, scope=scope):
        if node_id in link_final or node_id in own_final:
            current = link_final.get(node_id, VERIFIED)
            if _LINK_SEVERITY.get(BROKEN_LINK, 0) > _LINK_SEVERITY.get(current, 0):
                link_final[node_id] = BROKEN_LINK

    口 = Step(
        step_num=5,
        name="Write the link phase",
        purpose="Write the link statuses that changed and the link transition rows; when the envelope pass failed, "
        "record an open full entry for the next discovery refresh",
    )
    with db._connect(db_path) as conn:
        scanned_at = db._now_utc()
        link_wrote = False
        for nid in requested:
            prior = old.get(nid)
            if prior is None:
                continue
            old_own, old_link = prior["own_status"], prior["link_status"]
            new_link = link_final.get(nid, VERIFIED)
            via = via_map.get(nid, [])
            event = _link_transition(old_link, new_link)
            if event:
                meta = {"from_own": old_own, "from_link": old_link}
                if event == BECAME_LINKED_STALE and via:
                    meta["linked_node"] = via[0]
                cur = conn.execute(
                    "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                    "VALUES (?, ?, ?, ?, ?, 0)",
                    (nid, scanned_at, event, _git_sha(), json.dumps(meta)),
                )
                result.history_ids.append(int(cur.lastrowid))
                link_wrote = True
            if new_link != old_link:
                conn.execute("UPDATE nodes SET link_status = ? WHERE id = ?", (new_link, nid))
                result.rows_written += 1
                link_wrote = True
            result.statuses[nid] = (own_final[nid], new_link, via)
        if envelope_failures and write_live:
            # The envelope pass failed, so an envelope flag it would have set
            # may be missing, and no later scoped pass is bound to revisit it:
            # record an open full entry no caller removes, so the next
            # discovery refresh runs in full.  A failure path only: the lock
            # is taken here when the link write wrote nothing.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            db.open_pass_conn(conn, uuid.uuid4().hex, full=True, ids=(), reason=db.OPEN_PASS_ENVELOPE_FAILED)
    logger.debug(
        "staleness pass: link phase written (%d rows written, %.3fs)",
        result.rows_written,
        time.perf_counter() - started,
    )
    return result


# ---------------------------------------------------------------------------
# Composite inheritance
# ---------------------------------------------------------------------------


@task(
    purpose="Propagate worst-severity child staleness up to each composite_process node via bottom-up traversal, over every parent or only the parents in a scope; a section of a frozen doc is never raised",
    inputs="Mutable statuses dict (node_id → status), db_path, optional scope closed under composes descendants, frozen doc tags",
    outputs="Updated statuses dict with composite nodes assigned their worst child status",
)
def apply_composite_inheritance(
    statuses: dict[str, tuple[str, str]],
    db_path: Path,
    scope: Collection[str] | None = None,
    frozen_tags: list[str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Assign each composite_process node the worst-severity child status.

    Inheritance runs independently for each dimension using
    ``_OWN_SEVERITY`` and ``_LINK_SEVERITY``.

    A DocJSON section of a doc carrying a frozen tag is never raised: neither
    the atomic-parent cap (a child's own change lifting the parent to
    LINKED_STALE) nor worst-child link inheritance applies to it, so its
    LINKED_STALE comes only from what the frozen carry block keeps (an
    inherited LINKED_STALE there would otherwise become permanent).  The doc
    envelope is not a frozen section and still aggregates its subtree.  The
    frozen parents are found among the parents loaded in Step 4, by id, so a
    scoped pass and the full pass decide them alike.

    Loads the ``composes`` edges, topologically sorts them (leaves first),
    then walks bottom-up so that multi-level composites propagate
    correctly.  With *scope* only the edges leaving its members are read;
    the scope is closed under ``composes`` descendants, so every child of a
    parent in it is in *statuses* with the value the full pass computes.

    Parameters:
        statuses: Mutable dict mapping node_id to status.  Modified in-place.
        db_path: Path to the axiom-graph SQLite DB.
        scope: Only parents in this set; ``None`` for every parent.
        frozen_tags: ``config.staleness.frozen_tags``: sections of docs
            carrying any of them keep their status.

    Returns:
        The updated statuses dict (same object, modified in-place).
    """

    口 = Step(
        step_num=1,
        name="Load composes edges",
        purpose="Fetch the composes edges (all of them, or those leaving the scope); early-exit if none exist",
        inputs="db_path, scope",
        outputs="composes_edges list of (parent_id, child_id) tuples",
    )
    with db._connect(db_path) as conn:
        if scope is None:
            composes_edges = [
                (r["from_id"], r["to_id"])
                for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes' ORDER BY rowid")
            ]
        else:
            composes_edges = []
            ids = sorted(set(scope))
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                composes_edges.extend(
                    (r["from_id"], r["to_id"])
                    for r in conn.execute(
                        "SELECT from_id, to_id FROM edges WHERE edge_type = 'composes' "
                        f"AND from_id IN ({','.join('?' * len(chunk))}) ORDER BY rowid",
                        chunk,
                    )
                )

    if not composes_edges:
        return statuses

    口 = Step(
        step_num=2,
        name="Build adjacency maps",
        purpose="Build forward (parent->children) and reverse (child->parents) maps plus all_parents set from composes edges",
        outputs="children dict, parents dict, all_parents set",
    )
    children: dict[str, list[str]] = {}
    parents: dict[str, list[str]] = {}
    all_parents: set[str] = set()

    for parent_id, child_id in composes_edges:
        children.setdefault(parent_id, []).append(child_id)
        parents.setdefault(child_id, []).append(parent_id)
        all_parents.add(parent_id)

    口 = Step(
        step_num=3,
        name="Topological sort (Kahn's, leaves-first)",
        purpose="Compute bottom-up traversal order so nested composites resolve before their parents",
        outputs="topo_order list",
        critical="Cycles in composes edges would stall here",
    )
    in_degree = {p: 0 for p in all_parents}
    for parent_id in all_parents:
        for child_id in children.get(parent_id, []):
            if child_id in all_parents:
                in_degree[parent_id] += 1

    queue = [p for p, deg in in_degree.items() if deg == 0]
    topo_order: list[str] = []
    while queue:
        node = queue.pop(0)
        topo_order.append(node)
        for parent_id in parents.get(node, []):
            if parent_id in in_degree:
                in_degree[parent_id] -= 1
                if in_degree[parent_id] == 0:
                    queue.append(parent_id)

    口 = Step(
        step_num=4,
        name="Load node types for inheritance mode",
        purpose="Fetch node_type and subtype for each parent so atomic_process parents get LINKED_STALE cap and docjson_doc envelopes aggregate over their subtree; mark the parents that are sections of a frozen doc",
        outputs="node_types dict, subtypes dict, frozen parent set",
    )
    node_types: dict[str, str] = {}
    subtypes: dict[str, str | None] = {}
    with db._connect(db_path) as conn:
        parent_ids = sorted(all_parents)
        for start in range(0, len(parent_ids), 500):
            chunk = parent_ids[start : start + 500]
            for r in conn.execute(
                f"SELECT id, node_type, subtype FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk
            ):
                node_types[r["id"]] = r["node_type"]
                subtypes[r["id"]] = r["subtype"]
    frozen_parents: set[str] = set()
    if frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, frozen_tags)
        frozen_parents = {
            p
            for p in all_parents
            if subtypes.get(p) in ("docjson", "docjson_section") and db.split_section_id(p)[0] in frozen_doc_ids
        }

    口 = Step(
        step_num=5,
        name="Propagate worst child severity per dimension",
        purpose="Walk topo order assigning each parent the worst-child status per dimension independently; a frozen "
        "section parent keeps its own status",
        inputs="topo_order, children map, statuses dict, node_types, subtypes, frozen parents",
        outputs="statuses dict updated in-place",
        critical="Missing children default to VERIFIED",
    )

    _propagate_two_column(statuses, topo_order, children, node_types, subtypes, frozen=frozen_parents)

    return statuses


def _subtree_ids(root: str, children: dict[str, list[str]]) -> set[str]:
    """Return the transitive ``composes`` closure below *root* (root excluded).

    Cycle-safe: a visited set guards against malformed edge data (including
    self-loops), so traversal always terminates.

    Args:
        root: Node ID to start from.
        children: Forward adjacency map (parent -> child IDs).

    Returns:
        Set of every node ID reachable from *root* via composes edges.
    """
    seen: set[str] = set()
    stack = list(children.get(root, []))
    while stack:
        nid = stack.pop()
        if nid in seen or nid == root:
            continue
        seen.add(nid)
        stack.extend(children.get(nid, []))
    return seen


def _propagate_two_column(
    statuses: dict[str, tuple[str, str]],
    topo_order: list[str],
    children: dict[str, list[str]],
    node_types: dict[str, str],
    subtypes: dict[str, str | None] | None = None,
    frozen: Collection[str] = (),
) -> None:
    """Two-column composite inheritance: worst per dimension independently.

    DocJSON envelopes (``subtype='docjson_doc'``) aggregate over their whole
    ``composes`` subtree rather than direct children only: mid-tree sections
    are atomic and do not relay child own-status upward the way composite
    parents do, so the envelope must look at every descendant itself.
    Parents in *frozen* (sections of a frozen doc) keep the status they have.
    """
    subtypes = subtypes or {}
    for composite_id in topo_order:
        if composite_id in frozen:
            statuses.setdefault(composite_id, (VERIFIED, VERIFIED))
            continue
        # Start from the composite's current status so upstream passes
        # (e.g. annotates/delegates_to LINKED_STALE) are never regressed
        # when all children are VERIFIED.
        cur_own, cur_link = statuses.get(composite_id, (VERIFIED, VERIFIED))
        worst_own = _OWN_SEVERITY.get(cur_own, 0)
        worst_link = _LINK_SEVERITY.get(cur_link, 0)
        if subtypes.get(composite_id) == "docjson_doc":
            scope: list[str] | set[str] = _subtree_ids(composite_id, children)
        else:
            scope = children.get(composite_id, [])
        for child_id in scope:
            c_own, c_link = statuses.get(child_id, (VERIFIED, VERIFIED))
            worst_own = max(worst_own, _OWN_SEVERITY.get(c_own, 0))
            worst_link = max(worst_link, _LINK_SEVERITY.get(c_link, 0))

        ntype = node_types.get(composite_id)
        if ntype == "atomic_process":
            # ADR-010: atomic_process parents get LINKED_STALE cap for own,
            # but link dimension still inherits worst-child link.
            cur_own, cur_link = statuses.get(composite_id, (VERIFIED, VERIFIED))
            cur_own_sev = _OWN_SEVERITY.get(cur_own, 0)
            if worst_own > 0 or worst_link > 0:
                # If any child has issues, set link to at least LINKED_STALE
                effective_link = max(worst_link, _LINK_SEVERITY[LINKED_STALE])
            else:
                effective_link = max(_LINK_SEVERITY.get(cur_link, 0), worst_link)
            statuses[composite_id] = (
                _OWN_SEVERITY_TO_STATUS[max(cur_own_sev, 0)],  # preserve own
                _LINK_SEVERITY_TO_STATUS[effective_link],
            )
        else:
            statuses[composite_id] = (
                _OWN_SEVERITY_TO_STATUS[worst_own],
                _LINK_SEVERITY_TO_STATUS[worst_link],
            )


# ---------------------------------------------------------------------------
# Shared staleness computation — the single writer
# ---------------------------------------------------------------------------


def _via_settled_by(changed_at: str | None, verified_at: str) -> bool:
    """Whether a verification at *verified_at* already saw a via's change.

    Args:
        changed_at: Change time the via was admitted under, or ``None``
            when unknown.
        verified_at: The dependent's verification time (same clock as
            *changed_at*).

    Returns:
        ``True`` only when the change time is known and not newer than the
        verification.  An unknown change time is never treated as settled.
    """
    return bool(changed_at) and changed_at <= verified_at


def db_live_view(conn) -> LiveView:
    """Return the DB-only live view: stored live hashes, else baselines, NOT_FOUND by stored status.

    What the readers outside a staleness pass (``drift_query``,
    ``mark_clean``'s classification, ``reverify``) compare recorded pairs
    against: the index as of the last build or check, never the disk.

    Args:
        conn: Open connection to the index.

    Returns:
        The :class:`~axiom_graph.index.dependency_set.LiveView`.
    """
    hashes, not_found = db.load_live_view_conn(conn)
    return LiveView(hashes=hashes.get, missing=not_found.__contains__)


class _StoredLive:
    """Stored live hashes read per node on demand, for a pass that judges only a few nodes.

    Args:
        db_path: Path to the axiom-graph DB.
        raw_missing: ``True`` (the engine) reads "missing" from the missing
            marker the last re-hash stored, the own status before
            inheritance; ``False`` (a DB-only reader) reads it from the
            stored own status, as :func:`db_live_view` does.
        conn: Read through this open connection instead of opening one per
            batch on *db_path* (a caller holding a read-only connection to
            another index).
    """

    def __init__(self, db_path: Path | None, *, raw_missing: bool, conn=None) -> None:
        self._db_path = db_path
        self._raw = raw_missing
        self._conn = conn
        self._rows: dict[str, dict | None] = {}

    def prefetch(self, node_ids: Iterable[str]) -> None:
        """Load the rows of *node_ids* not loaded yet, in batches."""
        need = [nid for nid in dict.fromkeys(node_ids) if nid not in self._rows]
        if not need:
            return
        if self._conn is not None:
            rows = db.get_live_rows_conn(self._conn, need)
        else:
            with db._connect(self._db_path) as conn:
                rows = db.get_live_rows_conn(conn, need)
        for nid in need:
            self._rows[nid] = rows.get(nid)

    def _row(self, node_id: str) -> dict | None:
        if node_id not in self._rows:
            self.prefetch([node_id])
        return self._rows[node_id]

    def hashes(self, node_id: str) -> tuple[str | None, str | None] | None:
        row = self._row(node_id)
        if row is None:
            return None
        live = row["live_code_hash"]
        if live and live != db.MISSING_LIVE_HASH:
            return live, row["live_desc_hash"]
        return (row["code_hash"], row["desc_hash"]) if row["code_hash"] else None

    def missing(self, node_id: str) -> bool:
        row = self._row(node_id)
        if row is None:
            return False
        if self._raw:
            return row["live_code_hash"] == db.MISSING_LIVE_HASH
        return row["own_status"] == NOT_FOUND

    def view(self) -> LiveView:
        """Return this reader as a :class:`LiveView`."""
        return LiveView(hashes=self.hashes, missing=self.missing, prefetch=self.prefetch)


def _engine_live_view(
    db_path: Path,
    fresh_hashes: Mapping[str, tuple[str | None, str | None]],
    own_now: Mapping[str, str],
    scoped: bool = False,
) -> LiveView:
    """Return the staleness engine's live view: this pass's hashes first, then the stored ones.

    A node whose file this pass re-hashed reads at the hash just computed,
    and a node it derived reads at its stored live hash.  Any other node
    reads its stored live hash, else its baseline.  NOT_FOUND is this
    pass's verdict where the pass evaluated the node; outside a scoped
    pass's evaluation set it is the missing marker the node's last re-hash
    stored (the same verdict, without the parse), and in a full pass the
    stored status of a node the pass did not evaluate.  The stored view is
    read only if a pair lookup needs it.

    Args:
        db_path: Path to the axiom-graph DB.
        fresh_hashes: Node id -> hashes this pass computed or derived.
        own_now: Node id -> own status this pass has decided so far.
        scoped: The pass evaluates a scope, so nodes outside it are read
            per node (raw missing marker) rather than through a whole-index load.

    Returns:
        The :class:`~axiom_graph.index.dependency_set.LiveView`.
    """
    stored: list[LiveView] = []

    def _stored() -> LiveView:
        if not stored:
            if scoped:
                stored.append(_StoredLive(db_path, raw_missing=True).view())
            else:
                with db._connect(db_path) as conn:
                    stored.append(db_live_view(conn))
        return stored[0]

    def _hashes(node_id: str) -> tuple[str | None, str | None] | None:
        fresh = fresh_hashes.get(node_id)
        return fresh if fresh is not None else _stored().hashes(node_id)

    def _missing(node_id: str) -> bool:
        own = own_now.get(node_id)
        return own == NOT_FOUND if own is not None else _stored().missing(node_id)

    def _prefetch(node_ids: Iterable[str]) -> None:
        if scoped:
            pending = [nid for nid in node_ids if nid not in fresh_hashes or nid not in own_now]
            view = _stored()
            if view.prefetch is not None:
                view.prefetch(pending)

    return LiveView(hashes=_hashes, missing=_missing, prefetch=_prefetch)


def _pair_lookups(
    db_path: Path,
    live_view: LiveView | None,
) -> tuple[dict[str, dict[str, tuple[str, str | None]]], DependencyGraph | None, LiveView | None]:
    """Load every recorded pair, plus the links and live view needed to judge them.

    Args:
        db_path: Path to the axiom-graph DB.
        live_view: The caller's live view, or ``None`` for the DB-only one.

    Returns:
        ``(pairs, graph, view)``.  ``pairs`` is empty, and ``graph`` and
        ``view`` are ``None``, on an index below schema v5 or one that holds
        no pair -- the clock rule alone applies then.
    """
    with db._connect(db_path) as conn:
        return _pair_lookups_conn(conn, db_path, live_view, None)


def _pair_lookups_conn(
    conn,
    db_path: Path,
    live_view: LiveView | None,
    dependents: Collection[str] | None,
) -> tuple[dict[str, dict[str, tuple[str, str | None]]], DependencyGraph | None, LiveView | None]:
    """:func:`_pair_lookups` on an open connection, for every dependent or only *dependents*.

    For a scope the pairs, links and live hashes are read per node through
    the indexes (:class:`~axiom_graph.index.dependency_set.LazyDependencyGraph`),
    so the cost follows the scope; the graph reads through *conn*, which
    must stay open while it is used.

    Args:
        conn: Open connection.
        db_path: Path to the axiom-graph DB.
        live_view: The caller's live view, or ``None`` for the DB-only one.
        dependents: The dependents to judge, or ``None`` for every one.

    Returns:
        ``(pairs, graph, view)`` as :func:`_pair_lookups`.
    """
    if not db.pairs_ready(conn):
        return {}, None, None
    if dependents is None:
        pairs = db.get_all_verification_targets_conn(conn)
        if not pairs:
            return {}, None, None
        graph: DependencyGraph = load_dependency_graph(conn)
        view = live_view if live_view is not None else db_live_view(conn)
        return pairs, graph, view
    pairs = db.get_verification_targets_for_conn(conn, dependents)
    if not pairs:
        return {}, None, None
    lazy = LazyDependencyGraph(conn)
    lazy.prefetch(pairs)
    view = live_view if live_view is not None else _StoredLive(db_path, raw_missing=False).view()
    targets = {t for recorded in pairs.values() for t in recorded}
    lazy.prefetch(targets)
    if view.prefetch is not None:
        view.prefetch(dependency_targets(lazy, pairs) | targets)
    return pairs, lazy, view


def _forward_tagged_closure(
    db_path: Path,
    sections: Collection[str],
    transitive_tags: list[str] | None,
) -> set[str]:
    """Return every section reachable from *sections* along tagged doc-to-doc links (Pass 3 targets).

    Pass 3 decides a section from its targets' membership in the stale map
    before inheritance, so a scoped pass evaluates the whole forward chain
    rather than reading a target's stored link status (which may be
    inherited from its children).

    Args:
        db_path: Path to the axiom-graph DB.
        sections: The scope.
        transitive_tags: The doc tags that opt in to transitive propagation.

    Returns:
        The reachable sections, the scope itself excluded.
    """
    if not transitive_tags or not sections:
        return set()
    start = set(sections)
    seen: set[str] = set()
    frontier = sorted(start)
    with db._connect(db_path) as conn:
        while frontier:
            edges = db.get_tagged_doc_doc_edges_conn(conn, transitive_tags, source_ids=frontier)
            nxt = {e["target_section_id"] for e in edges} - seen - start
            seen |= nxt
            frontier = sorted(nxt)
    return seen


def _get_linked_stale_ids(
    db_path: Path,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    realigned_now: frozenset[str] | set[str] = frozenset(),
    live_view: LiveView | None = None,
    scope: Collection[str] | None = None,
) -> dict[str, list[str]]:
    """Return node IDs that are LINKED_STALE, with via chains.

    Returns a dict mapping each stale node ID to a list of the node IDs
    that *caused* the staleness (the "via" chain).  For direct
    doc-to-code or test-to-code staleness the via list contains the
    code node ID.  For transitive doc-to-doc staleness the via list
    contains the intermediate doc section ID that is itself stale.

    Pass 1 (direct): Collects doc-to-code and test-to-code signals,
    identical to the previous ``set[str]`` logic but populating a dict.

    Pass P (recorded pairs): A verification records, for each dependency
    target, the live hash of the target it saw (a *pair*).  For every pair
    whose dependency still exists -- the link, or the task still in the
    envelope's ``delegates_to`` closure, under the same dependent and
    target filters as Pass 1 / A / B -- the dependent is admitted with that
    via when the recorded hash differs from the target's live hash,
    whatever the change rows say.  That catches a revert, whose change the
    effective-change fold cancels, and a second edit, which writes no new
    change row.  Frozen sections are skipped as in Pass 1; a target with no
    live hash (NOT_FOUND, or no code hash) is left to the other passes,
    except under an open receipt (a pin), which never matches: a pinned via
    is admitted and kept whatever its target's live hash.

    Pass 2 (verification filter, per via): For a dependent with a
    verification (``verified_at``), a via with a recorded pair is settled
    exactly when the pair matches the target's live hash.  A via without
    one keeps the clock rule: kept only when its change -- the change time
    the via was admitted under by Pass 1 / A / B -- is newer than that
    verification.  The dependent is dropped when no via remains.  A via
    with no known change time and no pair is kept, never pruned on that
    basis.  Dependents with no verification keep every via.  Runs *before*
    transitive propagation so that verified direct nodes do not cascade
    false positives into consumers.

    Pass 3 (transitive): When *transitive_tags* is non-empty, loads
    doc-to-doc ``documents`` edges (via ``get_tagged_doc_doc_edges``)
    and runs a fixed-point loop: if a doc section's target is already
    in the stale dict, the source section is added with
    ``via=[target]``.  A visited set prevents cycles.

    When *frozen_tags* is non-empty, sections under any doc carrying a
    matching tag are skipped at insertion (Pass 1 doc-to-code) and never
    receive transitive propagation (Pass 3).  Test-to-code rows (Pass 1)
    and annotates / delegates_to passes are unaffected — the freeze is
    doc-section-scoped.  Empty *frozen_tags* (the default) is O(1)
    overhead — no SQL is issued.

    Every pass admits its rows in a fixed order (by dependent, then via),
    so a node's via list is the same whether the pass covered every node
    or a scope.

    Args:
        db_path: Path to the axiom-graph DB.
        transitive_tags: Doc-level tags that opt in to transitive
            propagation.  ``None`` or empty means Pass 3 is skipped.
        frozen_tags: Doc-level tags that opt OUT of LINKED_STALE signal.
            ``None`` or empty means no doc is treated as frozen.
        realigned_now: Nodes the current staleness pass found back at
            their stored baseline, before the history row recording it is
            written.  Their open change no longer counts as a change time
            in Pass 1 / A / B (see
            :func:`axiom_graph.db.history.effective_change_rows_conn`), so
            a round trip clears on the build that completes it.
        live_view: Where Pass P and Pass 2 read a target's live hash.  The
            staleness engine passes the hashes it just computed; ``None``
            (every DB-only reader) reads the stored live hashes, else the
            baselines (:func:`db_live_view`).
        scope: Decide only these nodes (and the sections their tagged
            doc-to-doc links reach, which Pass 3 reads).  ``None`` decides
            every node: the reference every scoped call must equal.  The
            returned map holds entries for evaluated nodes only.

    Change times in every pass come from the shared effective-change rule:
    a change that ended back at the baseline it was measured against does
    not count, while one accepted by ``mark_clean`` at a different hash
    still does.

    Returns:
        Dict mapping stale node ID to list of causing node IDs.
    """
    stale_map: dict[str, list[str]] = {}
    eval_ids: set[str] | None = None
    if scope is not None:
        scope_set = set(scope)
        eval_ids = scope_set | _forward_tagged_closure(db_path, scope_set, transitive_tags)

    # -- Resolve frozen sections (once, only when frozen_tags is non-empty) --
    # frozen_section_ids is the set of doc_section IDs whose parent doc
    # carries a frozen tag.  When frozen_tags is empty/None this is O(1):
    # no SQL is issued by either helper.
    frozen_section_ids: set[str] = set()
    if frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, frozen_tags)
        if frozen_doc_ids:
            if eval_ids is None:
                section_to_doc = db.get_section_doc_id_map(db_path, frozen_doc_ids)
                frozen_section_ids = set(section_to_doc.keys())
            else:
                frozen_section_ids = {sid for sid in eval_ids if db.split_section_id(sid)[0] in frozen_doc_ids}

    # Change time each (dependent, via) pair was admitted under, as carried
    # by the admitting pass's own row.  Pass A admits DESC_ONLY changes that
    # a code-only lookup would miss, so Pass 2 must compare against the time
    # the via was actually admitted with — never a narrower change set.
    via_change_times: dict[tuple[str, str], str] = {}

    def _admit(dependent_id: str, via_id: str, changed_at: str | None) -> None:
        vias = stale_map.setdefault(dependent_id, [])
        if via_id not in vias:
            vias.append(via_id)
        if changed_at:
            key = (dependent_id, via_id)
            prior = via_change_times.get(key)
            if prior is None or changed_at > prior:
                via_change_times[key] = changed_at

    # -- Pass 1: direct doc-to-code and test-to-code staleness ----------
    for row in db.get_stale_doc_sections(db_path, realigned_now=realigned_now, section_ids=eval_ids):
        sid = row["section_id"]
        # Frozen-tag skip: sections under a frozen doc never receive
        # LINKED_STALE signal at Pass 1 entry.
        if sid in frozen_section_ids:
            continue
        _admit(sid, row["code_node_id"], row.get("code_changed_at"))

    for row in db.get_stale_tests(db_path, realigned_now=realigned_now, test_ids=eval_ids):
        _admit(row["test_node_id"], row["code_node_id"], row.get("code_changed_at"))

    # -- Pass A: annotates-envelope staleness (widened with DESC_ONLY) --
    # For every envelope X with outbound `annotates` → Y, if Y's code OR
    # docstring drifted after X was last updated, flag X as LINKED_STALE.
    for row in db.get_stale_annotated_nodes(db_path, realigned_now=realigned_now, envelope_ids=eval_ids):
        _admit(row["envelope_id"], row["target_id"], row.get("change_at"))

    # -- Pass B: delegates_to transitive staleness (code-only, cycle-guarded) --
    # Walks composes → autostep → delegates_to → annotates_rev transitively.
    # DESC_ONLY is excluded (Pass A catches it on the task's own envelope).
    for row in db.get_stale_workflow_envelopes_via_delegates(
        db_path, realigned_now=realigned_now, envelope_ids=eval_ids
    ):
        _admit(row["envelope_id"], row["via_task_id"], row.get("change_at"))

    with db._connect(db_path) as conn:
        # -- Pass P: recorded pairs ------------------------------------------
        # A pair whose dependency still exists and whose recorded hash differs
        # from the target's live hash admits its dependent, whatever the change
        # rows say.  One query for the pairs; the comparison is in memory.
        pairs, graph, view = _pair_lookups_conn(conn, db_path, live_view, eval_ids)
        dep_sets: dict[str, dict[str, frozenset[str]]] = {}
        live_cache: dict[str, tuple[str | None, str | None] | None] = {}

        def _deps(node_id: str) -> dict[str, frozenset[str]]:
            if node_id not in dep_sets:
                dep_sets[node_id] = dependency_set(graph, node_id) if graph is not None else {}
            return dep_sets[node_id]

        def _live(target_id: str) -> tuple[str | None, str | None] | None:
            if target_id not in live_cache:
                live_cache[target_id] = view.value(graph, target_id) if view is not None and graph is not None else None
            return live_cache[target_id]

        def _pair_verdict(node_id: str, target_id: str) -> bool | None:
            """``True`` when the pair matches, ``False`` when it differs, ``None`` when no pair decides.

            An open receipt (a pin) never matches, whether or not the target
            has a live hash: the offender it holds open stays open until a
            verification of the links replaces it.
            """
            recorded = pairs.get(node_id, {}).get(target_id)
            if recorded is None:
                return None
            kinds = _deps(node_id).get(target_id)
            if kinds is None:
                return None
            if recorded[0] == db.OPEN_RECEIPT_HASH:
                return False
            live = _live(target_id)
            if live is None:
                return None
            return pair_matches(kinds, recorded, live)

        for node_id in sorted(pairs):
            if node_id in frozen_section_ids:
                continue
            for target_id in sorted(pairs[node_id]):
                if _pair_verdict(node_id, target_id) is False:
                    _admit(node_id, target_id, None)

        # -- Pass 2: verification filter (per via) --------------------------
        # A verification settles every change it saw.  A via with a recorded
        # pair is settled exactly when the pair matches the live hash; any
        # other via keeps the clock rule: kept only when changed AFTER the
        # dependent's verified_at.  The dependent is dropped when no via
        # remains.  Passes A / B already admit a via only when its change
        # post-dates the envelope's updated_at, so for an unpaired via this
        # yields "changed after max(updated_at, verified_at)".  Runs BEFORE
        # transitive propagation so that verified direct nodes do not cascade
        # false LINKED_STALE into downstream consumers.
        if stale_map:
            verifications = (
                db.get_all_verifications_conn(conn)
                if eval_ids is None
                else db.get_verifications_for_conn(conn, list(stale_map))
            )
            to_remove: list[str] = []
            for node_id, via_list in stale_map.items():
                v = verifications.get(node_id)
                verified_at = v.get("verified_at") if v else None
                if not verified_at:
                    continue
                remaining: list[str] = []
                for via_id in via_list:
                    verdict = _pair_verdict(node_id, via_id)
                    if verdict is None:
                        if not _via_settled_by(via_change_times.get((node_id, via_id)), verified_at):
                            remaining.append(via_id)
                    elif not verdict:
                        remaining.append(via_id)
                if remaining:
                    via_list[:] = remaining
                else:
                    to_remove.append(node_id)

            for node_id in to_remove:
                del stale_map[node_id]

    # -- Pass 3: transitive doc-to-doc propagation ----------------------
    if transitive_tags:
        edges = db.get_tagged_doc_doc_edges(db_path, transitive_tags, source_ids=eval_ids)
        # Build adjacency: target_section_id -> [source_section_ids]
        # If a source links to a target that is stale, the source becomes stale.
        target_to_sources: dict[str, list[str]] = {}
        for edge in edges:
            tgt = edge["target_section_id"]
            src = edge["source_section_id"]
            target_to_sources.setdefault(tgt, []).append(src)
        ordered_targets = sorted(target_to_sources)

        # Fixed-point loop with visited-edge guard for cycle safety.
        # We track visited (src, target) edge pairs rather than just
        # source nodes, because a single source may link to multiple
        # stale targets and each edge should contribute a via entry.
        changed = True
        visited_edges: set[tuple[str, str]] = set()
        while changed:
            changed = False
            for target_id in ordered_targets:
                if target_id not in stale_map:
                    continue
                for src_id in sorted(target_to_sources[target_id]):
                    # Frozen-tag skip: a frozen source section never
                    # receives LINKED_STALE signal via Pass 3 propagation.
                    if src_id in frozen_section_ids:
                        continue
                    edge_key = (src_id, target_id)
                    if edge_key in visited_edges:
                        continue
                    visited_edges.add(edge_key)
                    if src_id not in stale_map:
                        stale_map[src_id] = [target_id]
                        changed = True
                    else:
                        if target_id not in stale_map[src_id]:
                            stale_map[src_id].append(target_id)

    return stale_map


# ---------------------------------------------------------------------------
# Reusable staleness-attribution helpers (mark_clean honesty + reverify)
# ---------------------------------------------------------------------------


def _composes_children_map(db_path: Path) -> dict[str, list[str]]:
    """Load the forward ``composes`` adjacency map (parent -> child IDs).

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        Dict mapping each composes parent to its direct child IDs.
    """
    children: dict[str, list[str]] = {}
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'").fetchall()
    for r in rows:
        children.setdefault(r["from_id"], []).append(r["to_id"])
    return children


def composes_map_conn(conn, node_ids: Iterable[str], *, upward: bool) -> dict[str, list[str]]:
    """Load the part of the forward ``composes`` map that a walk from *node_ids* reads.

    Every ``composes`` edge on the downward closure of *node_ids* (their
    subtrees), or with *upward* on the upward closure (their ancestor
    chains), in the shape of :func:`_composes_children_map`.  Read through
    the ``edges`` indexes in batches, so the cost follows the closure rather
    than the graph.  :func:`expand_composes_subtree` (downward) and
    :func:`composes_ancestors` (upward) give the same answers for *node_ids*
    over this map as over the whole one.

    Args:
        conn: Open connection.
        node_ids: Where the walk starts.
        upward: Walk to the parents rather than the children.

    Returns:
        Parent id -> child ids, children in table order.
    """
    children: dict[str, list[str]] = {}
    key, other = ("to_id", "from_id") if upward else ("from_id", "to_id")
    seen: set[str] = set()
    frontier = set(node_ids)
    while frontier:
        seen |= frontier
        ordered = sorted(frontier)
        nxt: set[str] = set()
        for start in range(0, len(ordered), 500):
            chunk = ordered[start : start + 500]
            rows = conn.execute(
                f"SELECT from_id, to_id FROM edges WHERE edge_type = 'composes' "
                f"AND {key} IN ({','.join('?' * len(chunk))}) ORDER BY rowid",
                chunk,
            )
            for r in rows:
                children.setdefault(r["from_id"], []).append(r["to_id"])
                if r[other] not in seen:
                    nxt.add(r[other])
        frontier = nxt
    return children


def expand_composes_subtree(
    db_path: Path,
    node_id: str,
    children_map: dict[str, list[str]] | None = None,
) -> set[str]:
    """Return every descendant of *node_id* via ``composes`` edges.

    Expands a composite source (doc envelope, module, or section with
    child sections) to its full descendant set — leaves AND intermediate
    composites.  Intermediates are included because a staleness root can
    itself be an aggregate (e.g. an edited section that has child
    sections).  Cycle-guarded; a leaf node returns an empty set.

    Node_type-agnostic: any node with outbound ``composes`` edges is
    treated as an aggregate, whatever its node_type.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: Node to expand.  Not included in the returned set.
        children_map: Optional pre-loaded forward composes adjacency map
            (from :func:`_composes_children_map`); loaded on demand when
            omitted.

    Returns:
        Set of descendant node IDs (excluding *node_id* itself).
    """
    if children_map is None:
        children_map = _composes_children_map(db_path)
    return _subtree_ids(node_id, children_map)


def resolve_root_offenders(stale_map: dict[str, list[str]]) -> dict[str, list[str]]:
    """Resolve each live stale-map entry to its leaf root offenders.

    Takes the live stale map from :func:`_get_linked_stale_ids`
    (``{stale_id: [via_ids]}``) and follows via entries that are
    themselves in the map until it reaches nodes that are NOT in the map
    — the leaf root offenders whose change originally caused the
    staleness.  Transitive doc-to-doc chains therefore resolve past
    their one-hop vias back to the originating node.

    Pure function over the passed map — issues no SQL and changes no
    pass semantics.  Cycle-safe: via cycles are collapsed by a visited
    set; a chain that terminates in a pure cycle with no external root
    contributes no roots (callers should treat an empty root list as
    unattributable and act conservatively).

    Args:
        stale_map: Live LINKED_STALE map (stale node ID -> via IDs).

    Returns:
        Dict mapping each stale node ID to its sorted list of root
        offender IDs.
    """
    result: dict[str, list[str]] = {}
    for nid in stale_map:
        roots: set[str] = set()
        visited: set[str] = set()
        stack = [nid]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            for via_id in stale_map.get(cur, []):
                if via_id in stale_map:
                    if via_id not in visited:
                        stack.append(via_id)
                else:
                    roots.add(via_id)
        result[nid] = sorted(roots)
    return result


def composes_ancestors(
    node_ids: Iterable[str],
    children_map: Mapping[str, list[str]],
) -> dict[str, set[str]]:
    """Return every ``composes`` ancestor of each node in *node_ids*.

    Walks the reverse of the forward ``composes`` adjacency map (from
    :func:`_composes_children_map`) upward through the full chain —
    module -> function, doc envelope -> section, section -> child
    section.  Treated as a DAG with possible cycles: a visited set stops
    the walk, and a node is never reported as its own ancestor.

    Pure function — issues no SQL.

    Args:
        node_ids: Nodes whose ancestors to collect.
        children_map: Forward composes adjacency map (parent -> children).

    Returns:
        Dict mapping each node in *node_ids* to its ancestor IDs (empty
        set for a node with no composes parent).
    """
    parents: dict[str, list[str]] = {}
    for parent, kids in children_map.items():
        for kid in kids:
            parents.setdefault(kid, []).append(parent)

    result: dict[str, set[str]] = {}
    for nid in node_ids:
        seen: set[str] = set()
        stack = list(parents.get(nid, ()))
        while stack:
            cur = stack.pop()
            if cur in seen or cur == nid:
                continue
            seen.add(cur)
            stack.extend(parents.get(cur, ()))
        result[nid] = seen
    return result


def _has_verification_after(
    node_id: str,
    change_id: int,
    *,
    verification_ops: Mapping[str, list[tuple[int, str | None]]],
    ancestors: Mapping[str, Iterable[str]] | None,
    reverify: bool,
) -> bool:
    """Whether *node_id* or a composes ancestor has a qualifying verification row.

    Args:
        node_id: The offender.
        change_id: ``node_history.id`` of the offender's latest change.
        verification_ops: Node ID -> ``(history_id, verification_op)`` pairs.
        ancestors: Optional offender ID -> its composes ancestors.
        reverify: ``True`` to count only rows written by reverify;
            ``False`` to count only rows written by any other operation.

    Returns:
        ``True`` when a matching row newer than *change_id* exists.
    """
    holders = [node_id, *((ancestors or {}).get(node_id, ()))]
    for holder in holders:
        for history_id, op in verification_ops.get(holder, ()):
            if history_id <= change_id:
                continue
            if (op == VERIFICATION_OP_REVERIFY) == reverify:
                return True
    return False


def already_reverified_offenders(
    offender_ids: Iterable[str],
    *,
    latest_change_ids: Mapping[str, int],
    verification_ops: Mapping[str, list[tuple[int, str | None]]],
    ancestors: Mapping[str, Iterable[str]] | None = None,
) -> set[str]:
    """Return the offenders that have already been reverified.

    An offender counts as already-reverified when it — or any of its
    ``composes`` ancestors listed in *ancestors* — carries a verification
    row satisfying both:

    1. **Operation check** — the row records
       :data:`~axiom_graph.index.mark_clean.VERIFICATION_OP_REVERIFY` as
       the operation that wrote it.  Rows recording any other operation
       value, and rows written before that provenance existed (op
       ``None``), are not counted.
    2. **Ordering check** — that row is *newer* than the offender's most
       recent content-bearing change, compared by ``node_history`` row
       id (monotonic, so no clock reading is involved).  A later change
       re-opens the offender.

    Reverifying a composite (module, doc, parent section) therefore
    counts as reverifying each of its descendants; the marker itself is
    still written only on the named source — this is read-time only.  An
    ancestor's row is compared against the *offender's* own latest change,
    so changing the offender after the ancestor was reverified re-opens it.

    Conservative default in every ambiguous case: an offender with no
    qualifying verification row, or with no change row at all — hence no
    reference point to order against — is **not** reported as
    already-reverified, whatever its ancestors carry.

    Pure function over the passed maps — issues no SQL.  The api layer
    supplies them (see
    :func:`axiom_graph.db.history.get_verification_ordering_rows` and
    :func:`composes_ancestors`).

    Args:
        offender_ids: Root offender IDs to classify.
        latest_change_ids: Offender ID -> ``node_history.id`` of its most
            recent content-bearing change row.
        verification_ops: Node ID -> its ``(history_id,
            verification_op)`` pairs, for the offenders and their
            ancestors.
        ancestors: Optional offender ID -> its ``composes`` ancestor IDs.
            Omitted means only the offender's own rows count.

    Returns:
        The subset of *offender_ids* that has already been reverified.
    """
    result: set[str] = set()
    for nid in offender_ids:
        change_id = latest_change_ids.get(nid)
        if change_id is None:
            continue
        if _has_verification_after(
            nid, change_id, verification_ops=verification_ops, ancestors=ancestors, reverify=True
        ):
            result.add(nid)
    return result


def marked_clean_not_reverified_offenders(
    offender_ids: Iterable[str],
    *,
    latest_change_ids: Mapping[str, int],
    verification_ops: Mapping[str, list[tuple[int, str | None]]],
    ancestors: Mapping[str, Iterable[str]] | None = None,
) -> set[str]:
    """Return the offenders verified since their last change, but not by reverify.

    Informational only — used to explain why reverify skipped a
    dependent.  An offender qualifies when it (or a ``composes`` ancestor
    in *ancestors*) carries a verification row newer than its latest
    change that was written by an operation other than reverify (e.g.
    ``mark_clean``, which is a single-node claim and deliberately does not
    settle an offender for its dependents), and it is not already
    reverified.  An offender with no change row never qualifies.

    Args:
        offender_ids: Outstanding offender IDs to classify.
        latest_change_ids: Offender ID -> latest change ``node_history.id``.
        verification_ops: Node ID -> its ``(history_id, verification_op)``
            pairs, for the offenders and their ancestors.
        ancestors: Optional offender ID -> its ``composes`` ancestor IDs.

    Returns:
        The subset of *offender_ids* that was marked clean but not
        reverified since its last change.
    """
    reverified = already_reverified_offenders(
        offender_ids,
        latest_change_ids=latest_change_ids,
        verification_ops=verification_ops,
        ancestors=ancestors,
    )
    result: set[str] = set()
    for nid in offender_ids:
        change_id = latest_change_ids.get(nid)
        if change_id is None or nid in reverified:
            continue
        if _has_verification_after(
            nid, change_id, verification_ops=verification_ops, ancestors=ancestors, reverify=False
        ):
            result.add(nid)
    return result


def classify_inherited_link(
    db_path: Path,
    node_id: str,
    stale_map: dict[str, list[str]],
    children_map: dict[str, list[str]] | None = None,
    exclude: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """Classify whether a node's LINKED_STALE is inherited from descendants.

    A node's LINKED_STALE is *inherited* when the node has no own stale
    source (it is absent from the live stale map) while descendants in
    its ``composes`` subtree do.  Marking such a node clean has no
    direct effect: the next recompute re-derives the parent's
    link_status from its children.

    Keys on outbound ``composes`` edges only — never on node_type — so
    doc envelopes (composite_process) and section nodes with child
    sections (atomic_process) classify identically.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: Node to classify.
        stale_map: Live LINKED_STALE map from
            :func:`_get_linked_stale_ids` (computed with the same
            transitive/frozen tag config as ``check``).
        children_map: Optional pre-loaded forward composes adjacency map.
        exclude: Descendant IDs to omit from the stale-descendant hint
            (e.g. nodes being cleared in the same batch).

    Returns:
        Tuple ``(has_own_signal, stale_descendants)``.  *has_own_signal*
        is True when the node itself is in the stale map (mark_clean
        genuinely clears that portion).  *stale_descendants* lists
        subtree members that ARE in the stale map — the actionable
        nodes an honest report should name.
    """
    has_own_signal = node_id in stale_map
    descendants = expand_composes_subtree(db_path, node_id, children_map)
    excluded = exclude or set()
    stale_descendants = sorted(d for d in descendants if d in stale_map and d not in excluded)
    return has_own_signal, stale_descendants


#: Version of the staleness rules: what the passes conclude from given hashes,
#: history, verifications and links.  Bump it with any change to a rule; the
#: staleness scheme stamp then makes the next ``check`` recompute every node
#: once.
STALENESS_RULES = "2"

#: ``rehash`` value of a pass that re-hashes every file (``check --full``).
REHASH_ALL = "all"

_HASHED_SUBTYPES_FOR_OWN = ("docjson", "docjson_doc", "workflow", "task")


@dataclass
class OwnPhase:
    """The own phase of one staleness pass.

    Attributes:
        own: Node id -> own status before inheritance and promotion
            (composites with no hash of their own are left out: they inherit).
        link_preset: Node id -> link status fixed before the link phase (step views).
        hashes: Node id -> the hashes the pass compared: re-hashed now, or the
            stored live hashes of a node whose file it did not re-hash.
        live_writes: Node id -> the live hashes to persist (re-hashed nodes,
            the missing marker for nodes the re-hash could not find, and each
            re-hashed file's anchor fingerprint).
        realigned: Nodes found back at their baseline while persisted stale.
        hashed_files: Location -> what the re-hash read (also missing files).
        laddered: Locations re-hashed node by node.
        structure: Location -> ``{"new": [...], "missing": [...]}`` for a
            re-hashed file whose parse found names the index lacks, or lost
            indexed nodes.
    """

    own: dict[str, str] = field(default_factory=dict)
    link_preset: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    live_writes: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    realigned: set[str] = field(default_factory=set)
    hashed_files: dict[str, FileObservation] = field(default_factory=dict)
    laddered: set[str] = field(default_factory=set)
    structure: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _derived_own(
    node_type: str, subtype: str | None, code_hash: str | None, desc_hash: str | None, live_code, live_desc
):
    """Decide a node's own status from its stored live hashes, without a parse.

    The same comparison the per-node ladder makes, on the hashes the last
    re-hash of the node's file stored: identical bytes give identical
    hashes, so this is what a re-hash would conclude.

    Args:
        node_type: The node's type.
        subtype: The node's subtype.
        code_hash: The baseline code hash.
        desc_hash: The baseline desc hash.
        live_code: Stored live code hash (``None`` / ``''`` read as the baseline,
            the missing marker as NOT_FOUND).
        live_desc: Stored live desc hash.

    Returns:
        ``(status, hashes)``; status ``None`` for a composite with no hash of
        its own (it inherits), hashes ``None`` when there are none to compare.
    """
    if subtype == "external_package" or node_type == "entity" or subtype in ("step", "autostep"):
        return VERIFIED, None
    if not code_hash:
        return VERIFIED, None
    if node_type == "composite_process" and subtype not in _HASHED_SUBTYPES_FOR_OWN:
        return None, None
    if live_code == db.MISSING_LIVE_HASH:
        return NOT_FOUND, None
    cur = (live_code, live_desc) if live_code else (code_hash, desc_hash)
    return _compare_own(cur, code_hash, desc_hash), cur


def _compare_own(cur: tuple[str | None, str | None], code_hash: str | None, desc_hash: str | None) -> str:
    """Own status of a node found at *cur* against its baseline."""
    cur_code, cur_desc = cur
    if cur_code != code_hash and cur_desc == desc_hash:
        return CONTENT_UPDATED
    if cur_code == code_hash and cur_desc != desc_hash:
        return DESC_UPDATED
    if cur_code != code_hash and cur_desc != desc_hash:
        return CONTENT_UPDATED
    return VERIFIED


@workflow(
    purpose="Own phase of a staleness pass: per file, re-hash it node by node, or derive its nodes' own statuses "
    "from the live hashes its last re-hash stored when its bytes are the bytes that re-hash read",
    inputs="db_path, project_root, the nodes to evaluate, which files to re-hash (gate / all / a set), discovery observations",
    outputs="OwnPhase: own statuses before inheritance, the compared hashes, the live hashes and file records to persist, realigned nodes, structural differences",
)
def _own_phase(
    db_path: Path,
    project_root: Path,
    nodes: list,
    *,
    rehash: object = None,
    observed: Mapping[str, FileObservation] | None = None,
) -> OwnPhase:
    """Compute own statuses for *nodes*, re-hashing only the files that need it.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        nodes: The nodes to evaluate.
        rehash: ``None`` for the fast-pass gate per file, :data:`REHASH_ALL`
            to re-hash every file, or the set of files to re-hash.
        observed: Discovery observations to reuse (location -> observation).

    Returns:
        :class:`OwnPhase`.
    """
    ph = OwnPhase()

    口 = Step(
        step_num=1,
        name="Categorize nodes",
        purpose="Separate external_package/entity and step views (always VERIFIED) from project nodes; group by file location",
        outputs="own statuses of the pass-through nodes, location_map",
    )
    location_map: dict[str, list] = defaultdict(list)
    step_ids_at: dict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if getattr(node, "subtype", None) == "external_package":
            ph.own[node.id] = VERIFIED
        elif node.node_type == "entity":
            ph.own[node.id] = VERIFIED
        elif getattr(node, "subtype", None) in ("step", "autostep"):
            # Step nodes are views into their enclosing function.  They
            # carry no staleness dimension — the function's own_status
            # and the envelope's link_status (via `annotates`) together
            # cover every semantic change.
            ph.own[node.id] = VERIFIED
            ph.link_preset[node.id] = VERIFIED
            if node.location:
                step_ids_at[node.location].add(node.id)
        elif node.location:
            location_map[node.location].append(node)
    if not location_map:
        return ph

    口 = Step(
        step_num=2,
        name="Choose re-hash or derive, per file",
        purpose="Batch-load what the choice reads; a file is re-hashed when the caller says so, or (the gate) when its "
        "bytes differ from the bytes its last re-hash read or it holds a node whose baseline a verification reset; a "
        "file with no last-hashed fingerprint falls back to the scan-time anchor fingerprint, the scan mtime and "
        "every node persisted VERIFIED",
        inputs="location_map, rehash, the per-file records, the reset markers, the stored live hashes",
        outputs="for each file: re-hash, derive, or (fallback only) confirm VERIFIED",
        critical="Deriving is truthful only because the same bytes give the same hashes: a node is derived only when "
        "its file's fingerprint equals the one its stored live hashes were taken at, and no baseline moved since "
        "(a verification's reset marker sends the file to the ladder; a build parse clears the file's fingerprint)",
    )
    ids = [n.id for loc_nodes in location_map.values() for n in loc_nodes]
    with db._connect(db_path) as conn:
        v5 = db.pairs_ready(conn)
        rows = db.get_live_rows_conn(conn, ids)
        gate = rehash is None
        records = db.get_file_records_conn(conn, list(location_map)) if gate and v5 else {}
        reset_locs = db.get_reset_locations_conn(conn, list(location_map)) if gate else set()
    persisted_own = {nid: r["own_status"] for nid, r in rows.items()}
    fallback: dict[str, dict] = {}

    def _fallback_maps() -> dict[str, dict]:
        if not fallback:
            fallback["mtimes"] = db.get_all_file_mtimes(db_path)
            fallback["last"] = db.get_last_hashed_fingerprints(db_path, _ANCHOR_SUBTYPES)
            fallback["reset"] = db.get_unhashed_node_ids(db_path)
        return fallback

    from axiom_graph.scanners.node_hashing import current_node_hashes_for_file  # noqa: PLC0415

    for location, loc_nodes in location_map.items():
        abs_path = project_root / location
        obs = observed.get(location) if observed is not None else None
        exists = obs.mtime is not None if obs is not None else abs_path.exists()
        if not exists:
            for n in loc_nodes:
                ph.own[n.id] = NOT_FOUND
                if v5 and getattr(n, "code_hash", None):
                    ph.live_writes[n.id] = (db.MISSING_LIVE_HASH, None)
            ph.hashed_files[location] = obs if obs is not None else FileObservation(None, None, None)
            continue

        if rehash == REHASH_ALL:
            ladder = True
        elif not gate:
            ladder = location in rehash  # type: ignore[operator]
        else:
            record = records.get(location)
            if record is not None and record.hashed_fp is not None:
                fp = obs.fingerprint if obs is not None else _file_fingerprint(abs_path)
                ladder = fp != record.hashed_fp or location in reset_locs
            else:
                # No fingerprint from a re-hash yet: the rule before per-file
                # records existed.  It confirms VERIFIED (it never derives).
                maps = _fallback_maps()
                anchor = _file_anchor(loc_nodes)
                if (
                    file_unchanged_since(maps["mtimes"].get(location), abs_path.stat().st_mtime)
                    and _location_persisted_verified(loc_nodes, persisted_own)
                    and not any(n.id in maps["reset"] for n in loc_nodes)
                    and anchor is not None
                    and _fingerprint_vouches(anchor, _file_fingerprint(abs_path), maps["last"].get(anchor.id))
                ):
                    for n in loc_nodes:
                        ph.own[n.id] = VERIFIED
                    continue
                ladder = True

        if not ladder:
            口 = Step(
                step_num=2.1,
                name="Derive from stored live hashes",
                purpose="Compare each node's stored live hashes with its baseline, the comparison the ladder makes",
            )
            for n in loc_nodes:
                row = rows.get(n.id) or {}
                status, cur = _derived_own(
                    n.node_type,
                    getattr(n, "subtype", None),
                    n.code_hash,
                    n.desc_hash,
                    row.get("live_code_hash"),
                    row.get("live_desc_hash"),
                )
                if status is None:
                    continue
                ph.own[n.id] = status
                if cur is not None:
                    ph.hashes[n.id] = cur
                    if status == VERIFIED and persisted_own.get(n.id) in (CONTENT_UPDATED, DESC_UPDATED, NOT_FOUND):
                        ph.realigned.add(n.id)
            continue

        口 = Step(
            step_num=2.2,
            name="Re-hash the file node by node",
            purpose="Fingerprint the file, then parse it once and compare every node's fresh hashes with its baseline; "
            "keep the fresh hashes as live hashes, the missing marker for nodes not found, and the names the parse "
            "found that the index lacks",
        )
        # The fingerprint is taken before the hashing, so a write in between
        # makes the next pass re-hash again rather than vouch for a stale
        # live hash.
        if obs is not None and obs.fingerprint is not None:
            observation = obs
        else:
            st = abs_path.stat()
            observation = FileObservation(_file_fingerprint(abs_path), st.st_mtime, st.st_size)
        ph.hashed_files[location] = observation
        ph.laddered.add(location)
        anchor = _file_anchor(loc_nodes)
        if anchor is not None and observation.fingerprint is not None:
            ph.live_writes[anchor.id] = (observation.fingerprint, None)

        unindexed: list[str] = []
        file_hashes = current_node_hashes_for_file(
            abs_path,
            loc_nodes,
            project_root,
            unindexed_out=unindexed,
            indexed_ids={n.id for n in loc_nodes} | step_ids_at.get(location, set()),
        )
        missing_here: list[str] = []
        for n in loc_nodes:
            subtype = getattr(n, "subtype", None)
            if subtype == "external_package":
                ph.own[n.id] = VERIFIED
                continue
            if not getattr(n, "code_hash", None):
                ph.own[n.id] = VERIFIED
                continue

            # Composite_process nodes other than docjson / workflow / task do
            # not have a directly derivable on-disk hash -- their own_status
            # is inherited from their children.  Leave own_status unset.
            if n.node_type == "composite_process" and subtype not in _HASHED_SUBTYPES_FOR_OWN:
                continue

            hashes = file_hashes.get(n.id)
            if hashes is None:
                ph.own[n.id] = NOT_FOUND
                if v5:
                    ph.live_writes[n.id] = (db.MISSING_LIVE_HASH, None)
                if n.node_type == "atomic_process":
                    missing_here.append(n.id)
                continue

            ph.hashes[n.id] = hashes
            ph.live_writes[n.id] = hashes
            status = _compare_own(hashes, n.code_hash, n.desc_hash)
            ph.own[n.id] = status
            if status == VERIFIED and persisted_own.get(n.id) in (CONTENT_UPDATED, DESC_UPDATED, NOT_FOUND):
                ph.realigned.add(n.id)
        if unindexed or missing_here:
            ph.structure[location] = {"new": sorted(unindexed), "missing": sorted(missing_here)}
    return ph


def _inherit_own(ph: OwnPhase, db_path: Path, scope: Collection[str] | None) -> dict[str, str]:
    """Own statuses after composite inheritance (the own dimension does not read the link one).

    Args:
        ph: The own phase.
        db_path: Path to the axiom-graph DB.
        scope: The evaluation set, or ``None``.

    Returns:
        Node id -> own status after inheritance, before promotion.
    """
    merged = {nid: (ph.own.get(nid, VERIFIED), VERIFIED) for nid in set(ph.own) | set(ph.link_preset)}
    apply_composite_inheritance(merged, db_path, scope=scope)
    return {nid: own for nid, (own, _link) in merged.items()}


def _promote(
    own: Mapping[str, str],
    nodes: list,
    hashes: Mapping[str, tuple[str | None, str | None]],
    db_path: Path,
    scope: Collection[str] | None,
) -> dict[str, str]:
    """Promote CONTENT_UPDATED / DESC_UPDATED to VERIFIED where the verification snapshot matches.

    Args:
        own: Own statuses after inheritance.
        nodes: The evaluated nodes (their baselines).
        hashes: The hashes the pass compared.
        db_path: Path to the axiom-graph DB.
        scope: The evaluation set, or ``None``.

    Returns:
        A new dict of own statuses after promotion.
    """
    out = dict(own)
    candidates = [nid for nid, status in own.items() if status in (CONTENT_UPDATED, DESC_UPDATED)]
    if not candidates:
        return out
    if scope is None:
        verifications = db.get_all_verifications(db_path)
    else:
        with db._connect(db_path) as conn:
            verifications = db.get_verifications_for_conn(conn, candidates)
    node_map = {n.id: n for n in nodes}
    for node_id in candidates:
        v = verifications.get(node_id)
        n = node_map.get(node_id)
        if not v or not n or not n.code_hash:
            continue
        cur = hashes.get(node_id)
        check_code = cur[0] if cur else n.code_hash
        check_desc = cur[1] if cur else n.desc_hash
        code_match = v.get("code_hash_at") == check_code
        desc_match = v.get("desc_hash_at") == check_desc
        if code_match and desc_match:
            out[node_id] = VERIFIED
        elif not code_match:
            out[node_id] = CONTENT_UPDATED
        else:
            out[node_id] = DESC_UPDATED
    return out


_ANNOTATE_STALE_OWN = frozenset({CONTENT_UPDATED, DESC_UPDATED, NOT_FOUND})
_CODE_STALE_OWN = frozenset({CONTENT_UPDATED, NOT_FOUND})


def _link_phase(
    db_path: Path,
    ph: OwnPhase,
    *,
    transitive_tags: list[str] | None,
    frozen_tags: list[str] | None,
    scope: Collection[str] | None,
    failures: list[str] | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Link phase of a staleness pass: LINKED_STALE before inheritance, with its vias.

    A failure of the in-memory envelope pass (Pass A'/B') is logged and the
    phase returns what the other passes found.

    Args:
        db_path: Path to the axiom-graph DB.
        ph: The own phase (own statuses before inheritance, compared hashes, realigned nodes).
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.
        scope: The evaluation set, or ``None`` for every node.
        failures: When given, the envelope pass's error is appended to it,
            for a caller that records the failure for the next check.

    Returns:
        ``(link_statuses, via_map)`` before composite inheritance.
    """
    own = ph.own
    link = dict(ph.link_preset)
    live_view = _engine_live_view(db_path, ph.hashes, own, scoped=scope is not None)
    linked_stale_map = _get_linked_stale_ids(
        db_path,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        realigned_now=ph.realigned,
        live_view=live_view,
        scope=scope,
    )
    via_map: dict[str, list[str]] = {}
    for node_id, via_list in linked_stale_map.items():
        if node_id in own or node_id in link:
            link[node_id] = LINKED_STALE
            via_map[node_id] = via_list

    # ADR-018 sticky LINKED_STALE invariant: when frozen_tags is non-empty
    # the propagation skip prevents NEW LINKED_STALE signal from being
    # recorded on frozen-doc sections, but freezing must NOT silently clear
    # existing LINKED_STALE.  The main passes skip frozen sections at Pass 1
    # / Pass 3, so this block applies the evidence Pass 2 reads itself: a
    # carried LINKED_STALE is re-asserted unless the section
    # carries a verification row newer (by node_history id) than its latest
    # BECAME_LINKED_STALE row — any verification row when it has none — in
    # which case it falls through to VERIFIED and composite inheritance
    # clears its doc envelope once no child stays stale.  A doc tool's
    # text-only verification is not such a row: it says nothing about the
    # section's links, so it never ends a carried LINKED_STALE.  Freezing
    # alone never clears it either.  A carried LINKED_STALE also needs a
    # cause: the passes run over the carried sections without the frozen
    # skip (Pass 2 included), and a section they find no via for falls
    # through to VERIFIED, so a status no current link explains (one an
    # earlier rule stored) is not kept forever.  With the skip off, Pass 3
    # also reads frozen targets: a carried section linking a frozen section
    # whose code changed keeps its cause, though that target never shows
    # LINKED_STALE itself.  The via is not recorded: the section stays carried.
    if frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, frozen_tags)
        if frozen_doc_ids:
            if scope is None:
                frozen_sections = set(db.get_section_doc_id_map(db_path, frozen_doc_ids))
            else:
                frozen_sections = {sid for sid in scope if db.split_section_id(sid)[0] in frozen_doc_ids}
            to_check = sorted(sid for sid in frozen_sections if sid not in linked_stale_map)
            if to_check:
                with db._connect(db_path) as conn:
                    prior_rows = db.get_live_rows_conn(conn, to_check)
                carried = [sid for sid in to_check if (prior_rows.get(sid) or {}).get("link_status") == LINKED_STALE]
                became_stale_ids = db.get_latest_history_ids(db_path, carried, (BECAME_LINKED_STALE,))
                # A doc edit's text-only verification is not evidence about
                # links, so it never ends a carried LINKED_STALE.
                verified_ids = db.get_latest_history_ids(
                    db_path, carried, ("AGENT_VERIFIED", "MANUAL_VERIFIED"), skip_text_only=True
                )
                unverified = [
                    sid
                    for sid in carried
                    if verified_ids.get(sid) is None or verified_ids[sid] <= became_stale_ids.get(sid, 0)
                ]
                caused = (
                    _get_linked_stale_ids(
                        db_path,
                        transitive_tags=transitive_tags,
                        realigned_now=ph.realigned,
                        live_view=live_view,
                        scope=unverified,
                    )
                    if unverified
                    else {}
                )
                for sid in unverified:
                    if caused.get(sid):
                        link[sid] = LINKED_STALE
                        via_map.setdefault(sid, [])

    # Pass A' / B' — in-memory annotates and delegates walks over the own
    # statuses this pass JUST computed, so an envelope whose annotated target
    # or delegated task is drifting right now is flagged even before its
    # change row exists.
    try:
        with db._connect(db_path) as conn:
            if scope is None:
                graph: DependencyGraph = load_dependency_graph(conn)
                envelope_ids = sorted({src for sources in graph.annotates_rev.values() for src in sources})
            else:
                graph = LazyDependencyGraph(conn)
                graph.annotates_out.prefetch(scope)  # type: ignore[attr-defined]
                envelope_ids = sorted(env for env in set(scope) if graph.annotates_out.get(env))
            # The closures are walked once, a level at a time over a lazy
            # graph, and every target the two loops read an own status for
            # outside the evaluation set is loaded in one batched read, so
            # the reads do not grow with the number of envelopes.
            warm_delegates_closures(graph, envelope_ids)
            closures = {
                env_id: delegates_closure(
                    env_id, graph.composes_out, graph.delegates_out, graph.annotates_rev, graph.subtype_of
                )
                for env_id in envelope_ids
            }
            outside: dict[str, str] = {}

            def _load_outside(node_ids: Collection[str]) -> None:
                rows = db.get_live_rows_conn(conn, sorted(node_ids))
                for node_id in node_ids:
                    row = rows.get(node_id)
                    status = None
                    if row is not None:
                        status, _cur = _derived_own(
                            row["node_type"],
                            row["subtype"],
                            row["code_hash"],
                            row["desc_hash"],
                            row["live_code_hash"],
                            row["live_desc_hash"],
                        )
                    outside[node_id] = status or VERIFIED

            if scope is not None:
                asked = {t for env_id in envelope_ids for t in graph.annotates_out.get(env_id, ())}
                asked.update(t for tasks in closures.values() for t in tasks)
                pending = {t for t in asked if t not in own}
                if pending:
                    _load_outside(pending)

            def _own_of(node_id: str) -> str:
                if node_id in own:
                    return own[node_id]
                if scope is None:
                    return VERIFIED
                if node_id not in outside:
                    _load_outside([node_id])
                return outside[node_id]

            for env_id in envelope_ids:
                for target_id in sorted(graph.annotates_out.get(env_id, ())):
                    if _own_of(target_id) not in _ANNOTATE_STALE_OWN:
                        continue
                    link[env_id] = LINKED_STALE
                    via_map.setdefault(env_id, [])
                    if target_id not in via_map[env_id]:
                        via_map[env_id].append(target_id)
            for env_id in envelope_ids:
                for task_id in closures[env_id]:
                    if _own_of(task_id) in _CODE_STALE_OWN:
                        link[env_id] = LINKED_STALE
                        via_map.setdefault(env_id, [])
                        if task_id not in via_map[env_id]:
                            via_map[env_id].append(task_id)
    except Exception as exc:
        if failures is not None:
            failures.append(str(exc))
            consequence = "the next check re-runs every node in full to set them"
        else:
            consequence = "this result may miss their flags"
        logger.warning(
            "in-memory annotates/delegates_to staleness pass failed; envelopes may not reflect drift, and %s. "
            "Error: %s",
            consequence,
            exc,
        )
    return link, via_map


def linked_stale_vias(
    db_path: Path,
    project_root: Path,
    node_ids: Collection[str],
    *,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
) -> dict[str, list[str]]:
    """Return the vias a staleness pass lists for *node_ids*, from the stored live hashes (no parse).

    The link phase of a pass scoped to *node_ids*, over own statuses derived
    from the live hashes the last re-hash stored: the same rules and the
    same inputs as the pass that stored the statuses, so each via list is
    the one ``check --full`` would list now.  The cost follows *node_ids*.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        node_ids: The nodes to attribute (typically the LINKED_STALE ones shown).
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.

    Returns:
        ``{node_id: via list}`` for the nodes that have one.
    """
    found = _scoped_via_map(db_path, project_root, node_ids, transitive_tags=transitive_tags, frozen_tags=frozen_tags)
    return {nid: via for nid, via in found.items() if via}


def _scoped_via_map(
    db_path: Path,
    project_root: Path,
    node_ids: Collection[str],
    *,
    transitive_tags: list[str] | None,
    frozen_tags: list[str] | None,
) -> dict[str, list[str]]:
    """The link phase of a pass scoped to *node_ids*, from the stored live hashes (no parse).

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        node_ids: The nodes to attribute.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.

    Returns:
        ``{node_id: via list}`` for every LINKED_STALE node among *node_ids*,
        an empty list included (a frozen section's carried status).
    """
    ids = sorted(set(node_ids))
    if not ids:
        return {}
    nodes = []
    with db._connect(db_path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            nodes.extend(
                db._row_to_node(r)
                for r in conn.execute(f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(chunk))})", chunk)
            )
    if not nodes:
        return {}
    ph = _own_phase(db_path, project_root, nodes, rehash=frozenset())
    _link, via_map = _link_phase(
        db_path, ph, transitive_tags=transitive_tags, frozen_tags=frozen_tags, scope={n.id for n in nodes}
    )
    wanted = set(ids)
    return {nid: list(via) for nid, via in via_map.items() if nid in wanted}


@dataclass
class CurrentOffenders:
    """A node's open offenders now: what a doc write's ``addresses=`` may name, and what each name refreshes.

    Attributes:
        vias: Direct vias, as ``drift_query``'s ``via=`` lists them.
        roots: Root offenders, as ``root=`` lists them (a chain's leaves).
        receipt_targets: The offenders in the node's dependency set: naming
            one refreshes the node's receipt for it.  Every other offender is
            reached through a doc-to-doc chain (Pass 3) and clears when its
            via does.
        carried: A frozen-doc section carrying LINKED_STALE with no via.
    """

    vias: list[str]
    roots: list[str]
    receipt_targets: frozenset[str]
    carried: bool = False

    @property
    def names(self) -> list[str]:
        """Every id ``addresses=`` accepts for the node: its vias and its roots."""
        return sorted(set(self.vias) | set(self.roots))


def current_offenders(
    db_path: Path,
    project_root: Path,
    node_ids: Collection[str],
    *,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
) -> dict[str, CurrentOffenders]:
    """Return the open offenders of *node_ids*, each a receipt target or a chain via, from the stored live hashes.

    One scoped link phase over the batch (:func:`linked_stale_vias`' rules
    and inputs), then one per hop of a doc-to-doc chain over the chain vias
    not evaluated yet, so each root is the one ``drift_query``'s ``root=``
    names.  Callers bring the nodes' cone current first
    (:func:`axiom_graph.index.refresh.refresh_before_write`), so these are
    the offenders ``check --full`` would list.  The cost follows the batch
    and its chains.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        node_ids: The nodes (doc sections a write names).
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.

    Returns:
        ``{node_id: CurrentOffenders}`` for the nodes with an offender or a
        carried status; the others are omitted.
    """
    wanted = set(node_ids)
    if not wanted:
        return {}
    stale_map: dict[str, list[str]] = {}
    carried: set[str] = set()
    dep_sets: dict[str, dict[str, frozenset[str]]] = {}
    evaluated: set[str] = set()
    frontier = set(wanted)
    with db._connect(db_path) as conn:
        graph = LazyDependencyGraph(conn)
        while frontier:
            found = _scoped_via_map(
                db_path, project_root, frontier, transitive_tags=transitive_tags, frozen_tags=frozen_tags
            )
            evaluated |= frontier
            for nid, vias in found.items():
                if vias:
                    stale_map[nid] = vias
                elif nid in wanted:
                    carried.add(nid)
            graph.prefetch(sorted(frontier))
            chain_vias: set[str] = set()
            for nid in sorted(frontier):
                dep_sets[nid] = dependency_set(graph, nid)
                chain_vias.update(v for v in stale_map.get(nid, ()) if v not in dep_sets[nid])
            frontier = chain_vias - evaluated
    roots = resolve_root_offenders(stale_map)
    out: dict[str, CurrentOffenders] = {}
    for nid in sorted(wanted):
        if nid not in stale_map and nid not in carried:
            continue
        vias = stale_map.get(nid, [])
        node_roots = roots.get(nid, [])
        deps = dep_sets.get(nid, {})
        out[nid] = CurrentOffenders(
            vias=list(vias),
            roots=list(node_roots),
            receipt_targets=frozenset(t for t in (*vias, *node_roots) if t in deps),
            carried=nid in carried,
        )
    return out


@workflow(
    purpose="Compute per-node staleness without writing: the own phase (re-hash or derive per file), the link phase, composite inheritance and verification promotion — over every node, or a scope",
    inputs="db_path, project_root, the nodes to evaluate, optional scope and re-hash choice",
    outputs="Dict mapping node_id → (own_status, link_status, via)",
)
def compute_staleness(
    db_path: Path,
    project_root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    realigned_out: set[str] | None = None,
    live_hashes_out: dict[str, tuple[str | None, str | None]] | None = None,
    *,
    scope: Collection[str] | None = None,
    rehash: object = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Hash-based staleness, without writing anything.

    Returns three-column statuses:
    ``{node_id: (own_status, link_status, via_list)}``.

    The *via_list* is a list of node IDs that caused the LINKED_STALE
    signal.  It is empty for nodes that are not LINKED_STALE.

    Own-status values (content dimension):
        ``CONTENT_UPDATED`` — code body / prose body changed.
        ``DESC_UPDATED``    — docstring / heading changed.
        ``NOT_FOUND``       — file or node no longer exists.
        ``VERIFIED``        — unchanged or explicitly verified.

    Link-status values (dependency dimension):
        ``LINKED_STALE``    — a node this one documents or validates changed.
        ``BROKEN_LINK``     — edge points at a non-existent node.
        ``VERIFIED``        — no dependency issues.

    The two dimensions are orthogonal: a node can be CONTENT_UPDATED AND
    LINKED_STALE simultaneously.

    Args:
        db_path: Path to the axiom-graph DB.
        project_root: Project root directory.
        nodes: Every node to evaluate.
        transitive_tags: Doc tags opting in to doc-to-doc propagation.
        frozen_tags: Doc tags whose sections receive no new LINKED_STALE.
        realigned_out: When given, receives the IDs of nodes whose
            persisted own status was stale and whose current hashes equal
            their stored baseline again (a round trip back to the
            baseline).  The same set is fed to the LINKED_STALE passes so
            the round trip stops counting as a change in this pass.
        live_hashes_out: When given, receives the live hashes a recorded
            pass would persist: the fresh hashes of every re-hashed node,
            the missing marker for a node its file no longer holds, and
            each re-hashed file's anchor fingerprint.
        scope: Evaluate only this set (closed under composes descendants);
            ``None`` evaluates every node in *nodes*.
        rehash: ``None`` for the fast-pass gate per file, :data:`REHASH_ALL`,
            or the set of files to re-hash.
    """
    t0 = time.monotonic()
    logger.info("compute_staleness: start (%d nodes)", len(nodes))

    口 = AutoStep(step_num=1, name="Own phase")
    ph = _own_phase(db_path, project_root, nodes, rehash=rehash)
    if realigned_out is not None:
        realigned_out.update(ph.realigned)
    if live_hashes_out is not None:
        live_hashes_out.update(ph.live_writes)

    口 = Step(
        step_num=2,
        name="Secondary staleness (LINKED_STALE)",
        purpose="Set link_status to LINKED_STALE when a hash a node's verification recorded for a dependency differs "
        "from that dependency's live hash, or, for a dependency with no recorded hash, when it changed after the "
        "verification; carry frozen LINKED_STALE while a cause remains (a frozen section gains none, here or by "
        "inheritance); flag envelopes whose annotated target or delegated task drifts now",
        outputs="link statuses and vias before inheritance",
        critical="Reads every dependency's live hash as the own phase left it: re-hashed when its file's "
        "fingerprint moved since that file's last re-hash, derived from the stored live hashes otherwise; a file "
        "with no last-hashed fingerprint keeps the earlier gate (scan mtime, every node persisted VERIFIED, no "
        "reset marker, anchor fingerprint), which can confirm its nodes VERIFIED without a re-hash, while the "
        "refresh always re-hashes such a file; with a scope only the nodes in it are evaluated",
    )
    link, via_map = _link_phase(db_path, ph, transitive_tags=transitive_tags, frozen_tags=frozen_tags, scope=scope)

    merged: dict[str, tuple[str, str]] = {}
    for nid in set(ph.own) | set(link):
        merged[nid] = (ph.own.get(nid, VERIFIED), link.get(nid, VERIFIED))
    口 = AutoStep(step_num=3, name="Composite inheritance")
    apply_composite_inheritance(merged, db_path, scope=scope, frozen_tags=frozen_tags)

    口 = Step(
        step_num=4,
        name="Verification promotion",
        purpose="Promote CONTENT_UPDATED/DESC_UPDATED to VERIFIED if verification snapshot matches current hashes",
        critical="Only CONTENT_UPDATED and DESC_UPDATED are promotable; LINKED_STALE and NOT_FOUND are not. "
        "Both code_hash AND desc_hash must match the verification snapshot for promotion.",
    )
    own_final = _promote({nid: o for nid, (o, _l) in merged.items()}, nodes, ph.hashes, db_path, scope)

    result: dict[str, tuple[str, str, list[str]]] = {}
    for nid, (_o, lk) in merged.items():
        result[nid] = (own_final.get(nid, VERIFIED), lk, via_map.get(nid, []))

    elapsed = time.monotonic() - t0
    stale = sum(1 for own, link_s, _via in result.values() if own != VERIFIED or link_s != VERIFIED)
    logger.info("compute_staleness: done (%.3fs, %d stale of %d)", elapsed, stale, len(result))
    return result
