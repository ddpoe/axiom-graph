"""Shared staleness engine — single writer for all staleness computation.

All consumers (CLI cmd_check, MCP axiom_graph_check/axiom_graph_render, Viz /api/check
and /api/all) call ``record_staleness()`` (the unified write point) or read
the persisted ``staleness`` column.  No other module computes or persists
staleness independently.

``compute_staleness()`` remains a pure computation (returns dict, no side
effects).  ``record_staleness()`` wraps it with transition-event recording
and staleness persistence in a single transaction.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

from axiom_graph.index import db
from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.index.git_utils import get_git_sha
from axiom_graph.models import hash16
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
_ANCHOR_SUBTYPES = frozenset({"module", "docjson", "config"})


def _file_content_matches_anchor(abs_path: "Path", loc_nodes: list) -> bool:
    """Whether the file's current bytes match its indexed file-level anchor.

    Backs the staleness step-2 mtime fast-pass: even when mtime says
    "unchanged", confirm the content against the anchor node's whole-file
    ``code_hash`` before blanket-verifying.

    The file is read the SAME way its scanner hashes it (read-mode parity is
    load-bearing): JS/TS via ``read_bytes().decode`` (no newline normalization),
    everything else via universal-newline ``read_text``.

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
    anchor = None
    for n in loc_nodes:
        if getattr(n, "subtype", None) in _ANCHOR_SUBTYPES and getattr(n, "code_hash", None):
            anchor = n
            break
    if anchor is None:
        return False

    try:
        if abs_path.suffix in _JS_TS_EXTENSIONS:
            current_text = abs_path.read_bytes().decode("utf-8", errors="replace")
        else:
            current_text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("content gate: failed to read %s: %s", abs_path, exc)
        return False

    return hash16(current_text) == anchor.code_hash


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


def find_broken_links(db_path: Path) -> dict[str, str]:
    """Find edges whose to_id has no matching node in the index.

    Only checks ``documents`` and ``validates`` edge types (user-facing
    link types).  Returns a dict mapping from_id to the first dangling
    to_id found for that source node.

    Args:
        db_path: Path to the axiom-graph SQLite database.

    Returns:
        Dict mapping source node ID to the dangling target node ID.
    """
    with db._connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.from_id, e.to_id
            FROM edges e
            LEFT JOIN nodes n ON n.id = e.to_id
            WHERE e.edge_type IN ('documents', 'validates')
              AND n.id IS NULL
            """
        ).fetchall()
    result: dict[str, str] = {}
    for r in rows:
        result.setdefault(r["from_id"], r["to_id"])
    return result


# ---------------------------------------------------------------------------
# record_staleness — unified write point for staleness + transition events
# ---------------------------------------------------------------------------


@task(
    purpose="Compute staleness, detect transition events, and persist in one transaction",
    inputs="db_path, project_root, list of AxiomNode objects",
    outputs="dict[str, tuple[str, str]] — two-column statuses (own_status, link_status)",
)
def record_staleness(
    db_path: Path,
    project_root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    renamed_ids: set[str] | None = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Compute staleness and record transition events in one transaction.

    This is the single write point that all callers (CLI, MCP, viz) use.
    Returns ``dict[str, tuple[str, str, list[str]]]`` -- three-column
    statuses (own_status, link_status, via_list).

    Args:
        renamed_ids: New node IDs that received a migrated history/edges from
            a rename *this build*.  Their own_status is forced to ``RENAMED``.
            ``RENAMED`` is also *sticky*: a node whose persisted own_status is
            already ``RENAMED`` keeps it (until ``mark_clean`` or until the
            node is genuinely lost -> ``NOT_FOUND``), since there is no hash
            signal that re-derives "this was renamed".
    """
    requested_ids = {n.id for n in nodes}

    # Pure computation — returns three-column tuples.
    new_statuses = compute_staleness(
        db_path,
        project_root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
    )

    # Post-processing: overlay BROKEN_LINK on link dimension.
    broken_links = find_broken_links(db_path)
    for node_id in broken_links:
        if node_id in new_statuses:
            own, link, via = new_statuses[node_id]
            if _LINK_SEVERITY.get(BROKEN_LINK, 0) > _LINK_SEVERITY.get(link, 0):
                new_statuses[node_id] = (own, BROKEN_LINK, via)

    git_sha = get_git_sha(project_root)
    scanned_at = db._now_utc()

    with db._connect(db_path) as conn:
        # --- read old two-column staleness ---
        old_rows = conn.execute("SELECT id, own_status, link_status FROM nodes").fetchall()
        old_statuses = {r["id"]: (r["own_status"], r["link_status"]) for r in old_rows}

        # RENAMED overlay (D-1): force RENAMED for nodes renamed THIS build, and
        # preserve a persisted RENAMED (sticky -- there is no hash signal that
        # re-derives it; cleared only by mark_clean or a genuine NOT_FOUND).
        _renamed = renamed_ids or set()
        for nid in list(new_statuses.keys()):
            own, link, via = new_statuses[nid]
            if own == NOT_FOUND:
                continue
            if nid in _renamed or old_statuses.get(nid, (None, None))[0] == RENAMED:
                new_statuses[nid] = (RENAMED, link, via)

        # --- diff and insert transition events (requested nodes only) ---
        for node_id, (new_own, new_link, via) in new_statuses.items():
            if node_id not in requested_ids:
                continue
            old_pair = old_statuses.get(node_id, (VERIFIED, VERIFIED))
            new_pair = (new_own, new_link)
            events = _transition_change_type(old_pair, new_pair)
            if not events:
                continue
            for change_type in events:
                meta: dict = {"from_own": old_pair[0], "from_link": old_pair[1]}
                if change_type == BECAME_LINKED_STALE and via:
                    meta["linked_node"] = via[0]
                conn.execute(
                    """
                    INSERT INTO node_history
                        (node_id, scanned_at, change_type, git_sha, meta, preserved)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (node_id, scanned_at, change_type, git_sha, json.dumps(meta)),
                )

        # --- persist new staleness (requested nodes only) ---
        for node_id, (own, link, _via) in new_statuses.items():
            if node_id not in requested_ids:
                continue
            conn.execute(
                "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
                (own, link, node_id),
            )

    return new_statuses


# ---------------------------------------------------------------------------
# Composite inheritance
# ---------------------------------------------------------------------------


@task(
    purpose="Propagate worst-severity child staleness up to each composite_process node via bottom-up traversal",
    inputs="Mutable statuses dict (node_id → status) and db_path",
    outputs="Updated statuses dict with composite nodes assigned their worst child status",
)
def apply_composite_inheritance(
    statuses: dict[str, tuple[str, str]],
    db_path: Path,
) -> dict[str, tuple[str, str]]:
    """Assign each composite_process node the worst-severity child status.

    Inheritance runs independently for each dimension using
    ``_OWN_SEVERITY`` and ``_LINK_SEVERITY``.

    Loads all ``composes`` edges from the DB, topologically sorts them
    (leaves first), then walks bottom-up so that multi-level composites
    propagate correctly.

    Parameters:
        statuses: Mutable dict mapping node_id to status.  Modified in-place.
        db_path: Path to the axiom-graph SQLite DB.

    Returns:
        The updated statuses dict (same object, modified in-place).
    """

    口 = Step(
        step_num=1,
        name="Load composes edges",
        purpose="Fetch all edges from DB and filter to composes type; early-exit if none exist",
        inputs="db_path",
        outputs="composes_edges list of (parent_id, child_id) tuples",
    )
    all_edges = db.all_edges(db_path)
    composes_edges = [(e.from_id, e.to_id) for e in all_edges if e.edge_type == "composes"]

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
        purpose="Fetch node_type for each parent so atomic_process parents get LINKED_STALE cap",
        outputs="node_types dict",
    )
    node_types: dict[str, str] = {}
    with db._connect(db_path) as conn:
        placeholders = ",".join("?" * len(all_parents))
        rows = conn.execute(
            f"SELECT id, node_type FROM nodes WHERE id IN ({placeholders})",
            list(all_parents),
        ).fetchall()
        node_types = {r["id"]: r["node_type"] for r in rows}

    口 = Step(
        step_num=5,
        name="Propagate worst child severity per dimension",
        purpose="Walk topo order assigning each parent the worst-child status per dimension independently",
        inputs="topo_order, children map, statuses dict, node_types",
        outputs="statuses dict updated in-place",
        critical="Missing children default to VERIFIED",
    )

    _propagate_two_column(statuses, topo_order, children, node_types)

    return statuses


def _propagate_two_column(
    statuses: dict[str, tuple[str, str]],
    topo_order: list[str],
    children: dict[str, list[str]],
    node_types: dict[str, str],
) -> None:
    """Two-column composite inheritance: worst per dimension independently."""
    for composite_id in topo_order:
        # Start from the composite's current status so upstream passes
        # (e.g. annotates/delegates_to LINKED_STALE) are never regressed
        # when all children are VERIFIED.
        cur_own, cur_link = statuses.get(composite_id, (VERIFIED, VERIFIED))
        worst_own = _OWN_SEVERITY.get(cur_own, 0)
        worst_link = _LINK_SEVERITY.get(cur_link, 0)
        for child_id in children.get(composite_id, []):
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


def _get_linked_stale_ids(
    db_path: Path,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
) -> dict[str, list[str]]:
    """Return node IDs that are LINKED_STALE, with via chains.

    Returns a dict mapping each stale node ID to a list of the node IDs
    that *caused* the staleness (the "via" chain).  For direct
    doc-to-code or test-to-code staleness the via list contains the
    code node ID.  For transitive doc-to-doc staleness the via list
    contains the intermediate doc section ID that is itself stale.

    Pass 1 (direct): Collects doc-to-code and test-to-code signals,
    identical to the previous ``set[str]`` logic but populating a dict.

    Pass 2 (verification filter): Removes nodes whose verification
    (verified_at) is newer than all their via nodes' latest code
    changes.  Runs *before* transitive propagation so that verified
    direct nodes do not cascade false positives into consumers.

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

    Args:
        db_path: Path to the axiom-graph DB.
        transitive_tags: Doc-level tags that opt in to transitive
            propagation.  ``None`` or empty means Pass 3 is skipped.
        frozen_tags: Doc-level tags that opt OUT of LINKED_STALE signal.
            ``None`` or empty means no doc is treated as frozen.

    Returns:
        Dict mapping stale node ID to list of causing node IDs.
    """
    stale_map: dict[str, list[str]] = {}

    # -- Resolve frozen sections (once, only when frozen_tags is non-empty) --
    # frozen_section_ids is the set of doc_section IDs whose parent doc
    # carries a frozen tag.  When frozen_tags is empty/None this is O(1):
    # no SQL is issued by either helper.
    frozen_section_ids: set[str] = set()
    if frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, frozen_tags)
        if frozen_doc_ids:
            section_to_doc = db.get_section_doc_id_map(db_path, frozen_doc_ids)
            frozen_section_ids = set(section_to_doc.keys())

    # -- Pass 1: direct doc-to-code and test-to-code staleness ----------
    for row in db.get_stale_doc_sections(db_path):
        sid = row["section_id"]
        # Frozen-tag skip: sections under a frozen doc never receive
        # LINKED_STALE signal at Pass 1 entry.
        if sid in frozen_section_ids:
            continue
        code_id = row["code_node_id"]
        stale_map.setdefault(sid, [])
        if code_id not in stale_map[sid]:
            stale_map[sid].append(code_id)

    for row in db.get_stale_tests(db_path):
        tid = row["test_node_id"]
        code_id = row["code_node_id"]
        stale_map.setdefault(tid, [])
        if code_id not in stale_map[tid]:
            stale_map[tid].append(code_id)

    # -- Pass A: annotates-envelope staleness (widened with DESC_ONLY) --
    # For every envelope X with outbound `annotates` → Y, if Y's code OR
    # docstring drifted after X was last updated, flag X as LINKED_STALE.
    for row in db.get_stale_annotated_nodes(db_path):
        env_id = row["envelope_id"]
        target_id = row["target_id"]
        stale_map.setdefault(env_id, [])
        if target_id not in stale_map[env_id]:
            stale_map[env_id].append(target_id)

    # -- Pass B: delegates_to transitive staleness (code-only, cycle-guarded) --
    # Walks composes → autostep → delegates_to → annotates_rev transitively.
    # DESC_ONLY is excluded (Pass A catches it on the task's own envelope).
    for row in db.get_stale_workflow_envelopes_via_delegates(db_path):
        env_id = row["envelope_id"]
        via_id = row["via_task_id"]
        stale_map.setdefault(env_id, [])
        if via_id not in stale_map[env_id]:
            stale_map[env_id].append(via_id)

    # -- Pass 2: verification filter ------------------------------------
    # Remove nodes whose verification (verified_at) is newer than all
    # their via nodes' latest code changes.  This runs BEFORE transitive
    # propagation so that a verified direct node does not cascade false
    # LINKED_STALE into downstream consumers.
    if stale_map:
        verifications = db.get_all_verifications(db_path)
        all_via_ids: set[str] = set()
        for via_list in stale_map.values():
            all_via_ids.update(via_list)
        change_times = db.get_latest_code_change_times(db_path, list(all_via_ids))

        to_remove: list[str] = []
        for node_id, via_list in stale_map.items():
            v = verifications.get(node_id)
            if not v:
                continue
            verified_at = v.get("verified_at")
            if not verified_at:
                continue
            # Check if verified_at is newer than ALL via nodes' latest change.
            all_resolved = True
            for via_id in via_list:
                via_changed = change_times.get(via_id)
                if via_changed and via_changed > verified_at:
                    all_resolved = False
                    break
            if all_resolved:
                to_remove.append(node_id)

        for node_id in to_remove:
            del stale_map[node_id]

    # -- Pass 3: transitive doc-to-doc propagation ----------------------
    if transitive_tags:
        edges = db.get_tagged_doc_doc_edges(db_path, transitive_tags)
        # Build adjacency: target_section_id -> [source_section_ids]
        # If a source links to a target that is stale, the source becomes stale.
        target_to_sources: dict[str, list[str]] = {}
        for edge in edges:
            tgt = edge["target_section_id"]
            src = edge["source_section_id"]
            target_to_sources.setdefault(tgt, []).append(src)

        # Fixed-point loop with visited-edge guard for cycle safety.
        # We track visited (src, target) edge pairs rather than just
        # source nodes, because a single source may link to multiple
        # stale targets and each edge should contribute a via entry.
        changed = True
        visited_edges: set[tuple[str, str]] = set()
        while changed:
            changed = False
            for target_id, source_ids in target_to_sources.items():
                if target_id not in stale_map:
                    continue
                for src_id in source_ids:
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


@workflow(
    purpose="Compute per-node staleness via file re-parsing — single authoritative writer",
    inputs="db_path, project_root, list of all indexed nodes",
    outputs="Dict mapping node_id → staleness status",
)
def compute_staleness(
    db_path: Path,
    project_root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Full hash-based staleness check — the single writer.

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
    """
    t0 = time.monotonic()
    logger.info("compute_staleness: start (%d nodes)", len(nodes))

    口 = Step(
        step_num=1,
        name="Categorize nodes",
        purpose="Separate external_package/entity (always VERIFIED) from project nodes; group by file location",
        outputs="own_statuses dict, location_map",
    )
    # Internal working dicts — single strings, assembled into tuples at the end.
    own_statuses: dict[str, str] = {}
    link_statuses: dict[str, str] = {}
    # Track current recomputed hashes per node_id for verification promotion.
    current_node_hashes: dict[str, tuple[str | None, str | None]] = {}
    location_map: dict[str, list] = defaultdict(list)

    for node in nodes:
        if getattr(node, "subtype", None) == "external_package":
            own_statuses[node.id] = VERIFIED
        elif node.node_type == "entity":
            own_statuses[node.id] = VERIFIED
        elif getattr(node, "subtype", None) in ("step", "autostep"):
            # Step nodes are views into their enclosing function.  They
            # carry no staleness dimension — the function's own_status
            # and the envelope's link_status (via `annotates`) together
            # cover every semantic change.  See
            # axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations
            # for the full rationale.
            own_statuses[node.id] = VERIFIED
            link_statuses[node.id] = VERIFIED
        else:
            if node.location:
                location_map[node.location].append(node)

    口 = Step(
        step_num=2,
        name="Per-file staleness detection",
        purpose="For each file location: check existence -> NOT_FOUND; CONTENT-GATED mtime fast-pass -> VERIFIED only when a whole-file fingerprint matches the file-level anchor's code_hash; else (mtime changed, or anchor missing/mismatched) re-parse and compare hashes per node",
        inputs="location_map, project_root, stored mtimes from DB",
        outputs="own_statuses updated for atomic_process nodes; composite_process nodes left unset for inheritance",
        critical="Mtime trust is no longer blind — the content gate confirms the file's bytes against the anchor's whole-file code_hash before blanket-VERIFY. The gate relies on that anchor code_hash being present and on read-mode parity (JS/TS read as raw decoded bytes, everything else via universal-newline read_text); a missing/empty anchor hash falls through to the per-node ladder rather than over-trusting mtime",
    )
    for location, loc_nodes in location_map.items():
        abs_path = project_root / location
        if not abs_path.exists():
            for n in loc_nodes:
                own_statuses[n.id] = NOT_FOUND
            continue

        stored_mtime = db.get_file_mtime(db_path, location)
        current_mtime = abs_path.stat().st_mtime

        # Mtime fast-pass (exact comparison — no slop; see cycle
        # pev-2026-06-11-staleness-content-hash-gate decision D-B1).  Even
        # when mtime says "unchanged", confirm the file's content against
        # its file-level anchor's whole-file hash before blanket-verifying,
        # so a byte change with a preserved/rolled-back mtime is still
        # caught.  Anchor missing / empty hash / mismatch -> fall through to
        # the per-node ladder (never blanket-VERIFY without a real compare).
        if file_unchanged_since(stored_mtime, current_mtime) and _file_content_matches_anchor(abs_path, loc_nodes):
            for n in loc_nodes:
                own_statuses[n.id] = VERIFIED
            continue

        # File has changed — delegate to the consolidated primitive,
        # which parses the AST (or DocJSON) once and looks up every
        # node from this file in a single batch.  See
        # ``axiom_graph.scanners.node_hashing`` for the dispatch ladder.
        # Nodes absent from ``file_hashes`` could not be located on
        # disk -> NOT_FOUND.
        from axiom_graph.scanners.node_hashing import current_node_hashes_for_file  # noqa: PLC0415

        file_hashes = current_node_hashes_for_file(abs_path, loc_nodes, project_root)

        for n in loc_nodes:
            subtype = getattr(n, "subtype", None)
            if subtype == "external_package":
                own_statuses[n.id] = VERIFIED
                continue
            if not getattr(n, "code_hash", None):
                own_statuses[n.id] = VERIFIED
                continue

            # Composite_process nodes other than docjson / workflow /
            # task do not have a directly derivable on-disk hash --
            # their own_status is inherited from their children in
            # step 4 (apply_composite_inheritance).  Leave own_status
            # unset for them.
            if n.node_type == "composite_process" and subtype not in (
                "docjson",
                "workflow",
                "task",
            ):
                continue

            hashes = file_hashes.get(n.id)
            if hashes is None:
                own_statuses[n.id] = NOT_FOUND
                continue

            cur_code, cur_desc = hashes
            # Record current hashes for step 5 verification promotion.
            current_node_hashes[n.id] = (cur_code, cur_desc)

            # Status comparison.  desc_hash equality counts both as
            # "both None" and "both equal".
            if cur_code != n.code_hash and cur_desc == n.desc_hash:
                own_statuses[n.id] = CONTENT_UPDATED
            elif cur_code == n.code_hash and cur_desc != n.desc_hash:
                own_statuses[n.id] = DESC_UPDATED
            elif cur_code != n.code_hash and cur_desc != n.desc_hash:
                own_statuses[n.id] = CONTENT_UPDATED
            else:
                own_statuses[n.id] = VERIFIED

    口 = Step(
        step_num=3,
        name="Secondary staleness (LINKED_STALE)",
        purpose="Set link_status to LINKED_STALE when a node's dependency has changed",
        outputs="link_statuses updated for doc sections and tests with edge-based staleness",
    )
    linked_stale_map = _get_linked_stale_ids(
        db_path,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
    )
    via_map: dict[str, list[str]] = {}
    for node_id, via_list in linked_stale_map.items():
        if node_id in own_statuses or node_id in link_statuses:
            link_statuses[node_id] = LINKED_STALE
            via_map[node_id] = via_list

    # ADR-018 sticky LINKED_STALE invariant: when frozen_tags is non-empty
    # the propagation skip prevents NEW LINKED_STALE signal from being
    # recorded on frozen-doc sections, but it must NOT silently clear
    # existing LINKED_STALE.  Only mark_clean (via Pass 2 verification
    # filter) is allowed to clear LINKED_STALE.  Carry forward any prior
    # LINKED_STALE status that the DB already holds for frozen sections
    # not present in ``linked_stale_map``.  Cost is bounded by the number
    # of frozen-doc sections — typically small.  O(1) when frozen_tags
    # is empty (no SQL).
    if frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, frozen_tags)
        if frozen_doc_ids:
            section_to_doc = db.get_section_doc_id_map(db_path, frozen_doc_ids)
            frozen_section_ids_for_compute = set(section_to_doc.keys())
            # Only query for frozen sections that are NOT already in
            # linked_stale_map (those would have been recorded fresh).
            to_check = [sid for sid in frozen_section_ids_for_compute if sid not in linked_stale_map]
            if to_check:
                with db._connect(db_path) as conn:
                    placeholders = ",".join("?" * len(to_check))
                    prior_rows = conn.execute(
                        f"SELECT id, link_status FROM nodes WHERE id IN ({placeholders})",
                        to_check,
                    ).fetchall()
                for r in prior_rows:
                    if r["link_status"] == LINKED_STALE:
                        link_statuses[r["id"]] = LINKED_STALE
                        # Preserve via_map empty list; we don't know the
                        # original via chain, but the LINKED_STALE signal
                        # itself is what matters for the invariant.
                        via_map.setdefault(r["id"], [])

    # Pass A' — in-memory annotates walk.  The SQL Pass A reads node_history,
    # which lags behind by one record_staleness call (the BECAME_* row is
    # inserted AFTER compute_staleness returns).  This pass uses the
    # own_statuses we JUST computed to flip envelopes whose annotated target
    # is drifting right now.  DESC_ONLY equivalents (DESC_UPDATED) are
    # included; Pass B-equivalents (CONTENT-only, transitive) are also
    # handled inline via an edge walk.
    try:
        annotates_rev: dict[str, list[str]] = {}
        composes_out: dict[str, list[str]] = {}
        delegates_out: dict[str, str] = {}
        subtype_map: dict[str, str | None] = {}
        with db._connect(db_path) as conn:
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'annotates'"):
                annotates_rev.setdefault(r["to_id"], []).append(r["from_id"])
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'composes'"):
                composes_out.setdefault(r["from_id"], []).append(r["to_id"])
            for r in conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = 'delegates_to'"):
                delegates_out[r["from_id"]] = r["to_id"]
            for r in conn.execute("SELECT id, subtype FROM nodes"):
                subtype_map[r["id"]] = r["subtype"]

        _ANNOTATE_STALE_OWN = {CONTENT_UPDATED, DESC_UPDATED, NOT_FOUND}
        _CODE_STALE_OWN = {CONTENT_UPDATED, NOT_FOUND}

        # Pass A' direct: envelope annotates stale target.
        for target_id, envs in annotates_rev.items():
            target_own = own_statuses.get(target_id, VERIFIED)
            if target_own not in _ANNOTATE_STALE_OWN:
                continue
            for env_id in envs:
                link_statuses[env_id] = LINKED_STALE
                via_map.setdefault(env_id, [])
                if target_id not in via_map[env_id]:
                    via_map[env_id].append(target_id)

        # Pass B' transitive: walk composes→autostep→delegates_to→annotates_rev.
        def _reachable_tasks(envelope_id: str) -> list[str]:
            visited_tasks: set[str] = set()
            env_frontier: list[str] = [envelope_id]
            visited_envs: set[str] = {envelope_id}
            depth = 0
            while env_frontier and depth < 32:
                next_envs: list[str] = []
                for env in env_frontier:
                    for step_id in composes_out.get(env, []):
                        if subtype_map.get(step_id) != "autostep":
                            continue
                        tgt_task = delegates_out.get(step_id)
                        if not tgt_task or tgt_task in visited_tasks:
                            continue
                        visited_tasks.add(tgt_task)
                        for tgt_env in annotates_rev.get(tgt_task, []):
                            if tgt_env in visited_envs:
                                continue
                            visited_envs.add(tgt_env)
                            next_envs.append(tgt_env)
                env_frontier = next_envs
                depth += 1
            return list(visited_tasks)

        # Enumerate envelopes from the edge map we already loaded.
        envelope_ids = {src for sources in annotates_rev.values() for src in sources}
        for env_id in envelope_ids:
            for task_id in _reachable_tasks(env_id):
                if own_statuses.get(task_id, VERIFIED) in _CODE_STALE_OWN:
                    link_statuses[env_id] = LINKED_STALE
                    via_map.setdefault(env_id, [])
                    if task_id not in via_map[env_id]:
                        via_map[env_id].append(task_id)
    except Exception as exc:
        logger.warning(
            "in-memory annotates/delegates_to staleness pass failed; envelopes may not reflect drift until next full rebuild. Error: %s",
            exc,
        )

    # Merge into two-column tuples for inheritance, then split back.
    all_ids = set(own_statuses) | set(link_statuses)
    merged: dict[str, tuple[str, str]] = {}
    for nid in all_ids:
        merged[nid] = (own_statuses.get(nid, VERIFIED), link_statuses.get(nid, VERIFIED))
    口 = AutoStep(step_num=4, name="Composite inheritance")
    apply_composite_inheritance(merged, db_path)
    # Unpack back
    for nid, (o, l) in merged.items():
        own_statuses[nid] = o
        link_statuses[nid] = l

    口 = Step(
        step_num=5,
        name="Verification promotion",
        purpose="Promote CONTENT_UPDATED/DESC_UPDATED to VERIFIED if verification snapshot matches current hashes",
        critical="Only CONTENT_UPDATED and DESC_UPDATED are promotable; LINKED_STALE and NOT_FOUND are not. "
        "Both code_hash AND desc_hash must match the verification snapshot for promotion.",
    )
    verifications = db.get_all_verifications(db_path)
    node_map = {n.id: n for n in nodes}
    for node_id, status in list(own_statuses.items()):
        if status in (CONTENT_UPDATED, DESC_UPDATED):
            v = verifications.get(node_id)
            n = node_map.get(node_id)
            if not v or not n or not n.code_hash:
                continue
            cur = current_node_hashes.get(node_id)
            check_code = cur[0] if cur else n.code_hash
            check_desc = cur[1] if cur else n.desc_hash
            code_match = v.get("code_hash_at") == check_code
            desc_match = v.get("desc_hash_at") == check_desc
            if code_match and desc_match:
                own_statuses[node_id] = VERIFIED
            elif not code_match:
                own_statuses[node_id] = CONTENT_UPDATED
            else:
                own_statuses[node_id] = DESC_UPDATED

    # Assemble final three-column result (own, link, via).
    result: dict[str, tuple[str, str, list[str]]] = {}
    all_final_ids = set(own_statuses) | set(link_statuses)
    for nid in all_final_ids:
        result[nid] = (
            own_statuses.get(nid, VERIFIED),
            link_statuses.get(nid, VERIFIED),
            via_map.get(nid, []),
        )

    elapsed = time.monotonic() - t0
    stale = sum(1 for own, link, _via in result.values() if own != VERIFIED or link != VERIFIED)
    logger.info("compute_staleness: done (%.3fs, %d stale of %d)", elapsed, stale, len(result))
    return result
