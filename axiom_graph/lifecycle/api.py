"""Public Python API for the lifecycle bounded context.

Per ADR-019 (cycle 2), the lifecycle domain owns every behavioural
primitive that drives the index lifecycle: build, staleness check,
verification (mark_clean), purge, history fetch, reference-point
listing, impact reporting, node diffing, DB checkout, and consumer
site rendering.

This module is the single canonical home for those operations; the
MCP wire surface (``axiom_graph.lifecycle.mcp_tools``) is a thin
layer that forwards calls.  The CLI (``axiom_graph.cli.indexing``,
``axiom_graph.cli.inspection``) also calls this module directly so a
single orchestration function is the source of truth for each Cat 4
operation.

Public surface:
    ``build_index``             -- discovery-only build + staleness compute
    ``compute_check_summary``   -- compute one-line staleness summary data
    ``mark_clean_nodes``        -- mark CONTENT_UPDATED nodes as verified
    ``purge_nodes``             -- remove NOT_FOUND nodes from the index
    ``fetch_history``           -- node history rows + total count
    ``list_reference_points``   -- list available SHAs/checkpoints
    ``compute_report``          -- impact report since a reference point
    ``checkout_db``             -- VACUUM INTO copy of the index DB
    ``render_site``             -- consumer-site renderer wrapper
    ``get_node_diff``           -- old vs new source for a code node

Per ADR-019 (cycle 3), ``compute_drift_query`` -- the read-only
inventory projection -- moved to :mod:`axiom_graph.query.api`.  Import
it from there.

Layering invariants (per ADR-019; enforced by ``tools/check_layering.py``):
    Allowed imports: ``axiom_graph.config``, ``axiom_graph.index.*``,
    ``axiom_graph.docjson.render_consumer`` (for render_site),
    ``axiom_graph.registry``, and stdlib.  Never ``axiom_graph.mcp.*``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from axiom_annotations import task, Step

from axiom_graph.config import AxiomGraphConfig, db_path_for
from axiom_graph.index import builder, db
from axiom_graph.index.staleness import record_staleness
from axiom_graph.index.status import (
    BECAME_BROKEN_LINK,
    BECAME_CONTENT_UPDATED,
    BECAME_DESC_UPDATED,
    BECAME_LINKED_STALE,
    BECAME_NOT_FOUND,
    BECAME_RENAMED,
    BECAME_VERIFIED,
    BROKEN_LINK,
    CONTENT_UPDATED,
    DESC_UPDATED,
    LINK_BECAME_VERIFIED,
    LINKED_STALE,
    NOT_FOUND,
    RENAMED,
    VERIFIED,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed result dataclasses (D-1: typed dataclasses, stable contract)
# ---------------------------------------------------------------------------


@dataclass
class BuildSummary:
    """Result of :func:`build_index`."""

    files_scanned: int
    files_skipped_mtime: int
    nodes_written: int
    nodes_skipped: int
    nodes_renamed: int
    edges_written: int
    edges_skipped: int
    broken_links_flagged: int
    warnings: list[str] = field(default_factory=list)
    staleness_total: int = 0
    staleness_stale: int = 0
    annotation_findings: list = field(default_factory=list)


@dataclass
class CheckSummary:
    """Result of :func:`compute_check_summary`."""

    own_counts: dict[str, int]
    link_counts: dict[str, int]
    clean_count: int
    doc_quality_count: int
    all_clean: bool
    statuses: dict[str, tuple[str, str, list[str]]]
    # Count of docjson shadow rows whose four invariant fields disagree
    # with the canonical doc_sections row (cycle pev-2026-05-15).
    # Always 0 on a freshly-built DB.
    invariant_violations: int = 0
    invariant_violation_ids: list[str] = field(default_factory=list)


@dataclass
class MarkCleanResult:
    """Result of :func:`mark_clean_nodes`."""

    marked: list[str]
    not_found: list[str]


@dataclass
class PurgeResult:
    """Result of :func:`purge_nodes` for a single node."""

    node_id: str
    purged: bool
    reason: str | None = None  # error reason, when purged is False


@dataclass
class RenameApplyResult:
    """Result of :func:`apply_rename`."""

    applied: bool
    old_id: str
    new_id: str
    reason: str | None = None  # refusal reason when applied is False


@dataclass
class RenameRevertResult:
    """Result of :func:`revert_rename`."""

    reverted: bool
    new_id: str
    old_id: str | None = None
    reason: str | None = None  # refusal reason when reverted is False


@dataclass
class HistoryRow:
    """Single row from :func:`fetch_history`.

    Mirrors the legacy dict shape returned by ``db.get_history`` so
    presentation code can iterate without re-mapping fields.
    """

    node_id: str
    change_type: str
    scanned_at: str
    git_sha: str | None
    meta: str | None


@dataclass
class HistoryResult:
    """Paginated result from :func:`fetch_history`."""

    rows: list[HistoryRow]
    total: int


@dataclass
class ReferencePoint:
    """One entry from :func:`list_reference_points`."""

    git_sha: str | None
    type: str
    scanned_at: str
    row_count: int
    message: str | None = None


@dataclass
class ReportData:
    """Result of :func:`compute_report`.

    Carries already-classified rows + summary counters so both CLI
    text/JSON and MCP text formatters can operate on the same payload.
    """

    summary: dict[str, int]
    content_changes: dict[str, list[dict]]
    staleness_transitions: list[dict]
    link_changes: list[dict]
    verifications: list[dict]
    human_verified_ids: set[str]
    no_rows: bool = False
    no_matches: bool = False


@dataclass
class CheckoutResult:
    """Result of :func:`checkout_db`."""

    target_db_path: Path
    copied: bool
    skipped_reason: str | None = None  # populated when copied=False


@dataclass
class RenderSiteResult:
    """Result of :func:`render_site`."""

    pages_rendered: int
    output_dir: Path
    warnings: list[str]


# ---------------------------------------------------------------------------
# Internal staleness helper (formerly mcp/_helpers._compute_staleness_for_nodes)
# ---------------------------------------------------------------------------


def _compute_staleness_for_nodes(
    db_path: Path,
    root: Path,
    nodes: list,
    transitive_tags: list[str] | None = None,
    frozen_tags: list[str] | None = None,
    renamed_ids: set[str] | None = None,
) -> dict[str, tuple[str, str, list[str]]]:
    """Thin wrapper -- delegates to record_staleness.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        nodes: List of AxiomNode objects.
        transitive_tags: Doc-level tags for transitive LINKED_STALE propagation.
        frozen_tags: Doc-level tags whose sections are immune to LINKED_STALE
            signal (Pass 1 + Pass 3 skip).

    Returns:
        Dict mapping node_id to (own_status, link_status, via_list) tuples.
    """
    return record_staleness(
        db_path,
        root,
        nodes,
        transitive_tags=transitive_tags,
        frozen_tags=frozen_tags,
        renamed_ids=renamed_ids,
    )


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build_index(
    db_path: Path,
    root: Path,
    *,
    project_id: str | None = None,
    discovery_only: bool = True,
    verbose: bool = False,
    embedder_thread=None,
) -> BuildSummary:
    """Run an axiom-graph build and compute persistent staleness.

    Shared orchestration for the CLI ``axiom-graph build`` command and
    the ``axiom_graph_build`` MCP tool.  Returns a typed
    :class:`BuildSummary`; presentation layers format it.

    Args:
        db_path: Path to the axiom-graph DB.  May not yet exist (the
            builder creates it).
        root: Project root directory.
        project_id: Optional project id prefix override.
        discovery_only: When ``True`` (default), only newly-discovered
            nodes are inserted; existing nodes are untouched.  When
            ``False``, runs a full re-scan (CLI ``init`` path).
        verbose: Reserved for caller-side formatting; the builder always
            populates ``BuildSummary.warnings``.
        embedder_thread: Optional embedder warm-up thread to pass through
            to the underlying builder.

    Returns:
        :class:`BuildSummary` with file/node/edge counts, warnings, and
        staleness counters.
    """
    summary = builder.build(
        root,
        project_id=project_id,
        discovery_only=discovery_only,
        embedder_thread=embedder_thread,
    )

    result = BuildSummary(
        files_scanned=summary.get("files_scanned", 0) or 0,
        files_skipped_mtime=summary.get("files_skipped_mtime", 0) or 0,
        nodes_written=summary.get("nodes_written", 0) or 0,
        nodes_skipped=summary.get("nodes_skipped", 0) or 0,
        nodes_renamed=summary.get("nodes_renamed", 0) or 0,
        edges_written=summary.get("edges_written", 0) or 0,
        edges_skipped=summary.get("edges_skipped", 0) or 0,
        broken_links_flagged=summary.get("broken_links_flagged", 0) or 0,
        warnings=list(summary.get("warnings", [])),
        annotation_findings=list(summary.get("annotation_findings", []) or []),
    )

    # Compute, record transition events, and persist staleness.
    if db_path.exists():
        config = AxiomGraphConfig.load(root)
        nodes = db.all_nodes(db_path)
        if nodes:
            statuses = _compute_staleness_for_nodes(
                db_path,
                root,
                nodes,
                transitive_tags=config.staleness.transitive_tags,
                frozen_tags=config.staleness.frozen_tags,
                renamed_ids=set(summary.get("renamed_new_ids", []) or []),
            )
            result.staleness_total = len(statuses)
            result.staleness_stale = sum(
                1 for own, link, _via in statuses.values() if own != "VERIFIED" or link != "VERIFIED"
            )

    return result


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def compute_check_summary(
    db_path: Path,
    root: Path,
    include_frozen: bool = False,
) -> CheckSummary | None:
    """Compute the data backing the one-line staleness summary.

    Shared by CLI ``axiom-graph check`` and MCP ``axiom_graph_check``.
    Returns ``None`` when the index has no nodes (callers print
    ``(no nodes in index)``).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        include_frozen: When ``False`` (the default), rows whose owning
            doc carries any tag listed in ``config.staleness.frozen_tags``
            are filtered out of the summary entirely (both the
            VERIFIED count and any LINKED_STALE / BROKEN_LINK rows).
            When ``True`` they participate in counts unchanged.  No-op
            when ``frozen_tags`` is empty.

    Returns:
        :class:`CheckSummary` (or ``None`` when empty).
    """
    nodes = db.all_nodes(db_path)
    if not nodes:
        return None

    config = AxiomGraphConfig.load(root)
    statuses = _compute_staleness_for_nodes(
        db_path,
        root,
        nodes,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
    )

    # When include_frozen=False, drop frozen-doc section rows from the
    # statuses dict before counting.  The propagation skip in
    # _get_linked_stale_ids already prevents these rows from being
    # LINKED_STALE; this additional filter removes them from the
    # VERIFIED count too, so the summary numbers describe only the
    # non-frozen surface.  Skip the resolution work entirely when
    # frozen_tags is empty (O(1) hot path preserved).
    if not include_frozen and config.staleness.frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, config.staleness.frozen_tags)
        if frozen_doc_ids:
            section_to_doc = db.get_section_doc_id_map(db_path, frozen_doc_ids)
            frozen_section_ids = set(section_to_doc.keys())
            statuses = {nid: trip for nid, trip in statuses.items() if nid not in frozen_section_ids}

    own_counts: dict[str, int] = {
        CONTENT_UPDATED: 0,
        DESC_UPDATED: 0,
        RENAMED: 0,
        NOT_FOUND: 0,
        VERIFIED: 0,
    }
    link_counts: dict[str, int] = {
        LINKED_STALE: 0,
        BROKEN_LINK: 0,
        VERIFIED: 0,
    }
    for own, link, _via in statuses.values():
        own_counts[own] = own_counts.get(own, 0) + 1
        link_counts[link] = link_counts.get(link, 0) + 1

    clean_count = sum(1 for own, link, _via in statuses.values() if own == VERIFIED and link == VERIFIED)

    long_sections = db.get_long_sections(db_path)
    doc_quality_count = len(long_sections)

    all_clean = all(own == VERIFIED and link == VERIFIED for own, link, _via in statuses.values())

    # DocJSON shadow-row invariant scan (cycle pev-2026-05-15).
    invariant_violation_ids = db.find_docjson_shadow_invariant_violations(db_path)

    return CheckSummary(
        own_counts=own_counts,
        link_counts=link_counts,
        clean_count=clean_count,
        doc_quality_count=doc_quality_count,
        all_clean=all_clean,
        statuses=statuses,
        invariant_violations=len(invariant_violation_ids),
        invariant_violation_ids=invariant_violation_ids,
    )


# ---------------------------------------------------------------------------
# mark_clean
# ---------------------------------------------------------------------------


def mark_clean_nodes(
    db_path: Path,
    root: Path,
    node_ids: list[str],
    reason: str,
    *,
    verified_by: str,
) -> MarkCleanResult:
    """Record AGENT_VERIFIED / MANUAL_VERIFIED for one or more nodes.

    Shared by CLI ``axiom-graph mark-clean`` (with
    ``verified_by="human"`` and a single-element list) and MCP
    ``axiom_graph_mark_clean`` (single or batch, default
    ``verified_by="agent"``).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        node_ids: Node IDs to verify.  Order is preserved.
        reason: Free-form reason recorded in the history meta.
        verified_by: Verifier identifier (``"human"``, ``"agent"``,
            ``"agent:claude-sonnet-4-6"``, ...).  Required keyword.

    Returns:
        :class:`MarkCleanResult` with marked vs not_found IDs.
    """
    from axiom_graph.index.mark_clean import mark_node_clean

    marked: list[str] = []
    not_found: list[str] = []
    for nid in node_ids:
        node = db.get_node(db_path, nid)
        if node is None:
            not_found.append(nid)
            continue
        mark_node_clean(db_path, root, node, reason, verified_by)
        marked.append(nid)

    return MarkCleanResult(marked=marked, not_found=not_found)


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def purge_nodes(
    db_path: Path,
    node_ids: list[str],
    reason: str,
) -> list[PurgeResult]:
    """Purge one or more NOT_FOUND nodes from the index.

    Doc nodes are cascade-deleted via ``delete_doc_by_id`` (sections too);
    code/other nodes via ``delete_node_by_id``.  A preserved DELETED
    history row is recorded with the supplied reason.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Node IDs to purge.
        reason: Free-form reason recorded in the history meta.

    Returns:
        One :class:`PurgeResult` per input node, in input order.
    """
    results: list[PurgeResult] = []
    with db._connect(db_path) as conn:
        for nid in node_ids:
            row = conn.execute(
                "SELECT id, node_type, own_status FROM nodes WHERE id = ?",
                (nid,),
            ).fetchone()
            if row is None:
                results.append(PurgeResult(node_id=nid, purged=False, reason="not_found_in_index"))
                continue
            status = row["own_status"]
            if status != "NOT_FOUND":
                results.append(PurgeResult(node_id=nid, purged=False, reason=f"status_{status}"))
                continue
            reason_meta = {"actor": "agent:pev-auditor", "reason": reason}
            is_doc = conn.execute("SELECT 1 FROM docs WHERE id = ?", (nid,)).fetchone() is not None
            if is_doc:
                db.delete_doc_by_id(conn, nid, reason_meta=reason_meta)
            else:
                db.delete_node_by_id(conn, nid, reason_meta=reason_meta)
            results.append(PurgeResult(node_id=nid, purged=True))
    return results


# ---------------------------------------------------------------------------
# apply_rename / revert_rename (manual escape hatch + round-trip)
# ---------------------------------------------------------------------------


def apply_rename(
    db_path: Path,
    root: Path,
    old_id: str,
    new_id: str,
) -> RenameApplyResult:
    """Manually weld a rename the automatic matcher missed (US-5 escape hatch).

    Restricted to the ``(NOT_FOUND old, newly-created new)`` safety contract:
    the call is refused unless *old_id* is an existing ``NOT_FOUND`` node and
    *new_id* is an existing live node that has never been a rename source or
    target.  This structurally prevents welding two pre-existing identities.

    On success the old node's history, verification, and edges are migrated to
    *new_id* via :func:`db.record_code_rename`, and *new_id*'s ``own_status``
    is forced to ``RENAMED`` (sticky, consistent with the auto-apply path).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        old_id: The ``NOT_FOUND`` node being renamed *from*.
        new_id: The newly-created live node being renamed *to*.

    Returns:
        :class:`RenameApplyResult`.  ``applied`` is ``False`` with a ``reason``
        when the safety contract is violated.
    """
    if old_id == new_id:
        return RenameApplyResult(False, old_id, new_id, reason="same_id")

    with db._connect(db_path) as conn:
        old_row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (old_id,)).fetchone()
        new_row = conn.execute("SELECT own_status, location FROM nodes WHERE id = ?", (new_id,)).fetchone()
        if old_row is None:
            return RenameApplyResult(False, old_id, new_id, reason="old_not_in_index")
        if new_row is None:
            return RenameApplyResult(False, old_id, new_id, reason="new_not_in_index")
        if old_row["own_status"] != NOT_FOUND:
            return RenameApplyResult(False, old_id, new_id, reason=f"old_status_{old_row['own_status']}")
        if new_row["own_status"] == NOT_FOUND:
            return RenameApplyResult(False, old_id, new_id, reason="new_not_live")
        # "newly-created new": never already a rename target, and old never
        # already renamed away -- prevents a double-weld onto a baseline node.
        if conn.execute("SELECT 1 FROM node_renames WHERE new_id = ?", (new_id,)).fetchone():
            return RenameApplyResult(False, old_id, new_id, reason="new_already_renamed")
        if conn.execute("SELECT 1 FROM node_renames WHERE old_id = ?", (old_id,)).fetchone():
            return RenameApplyResult(False, old_id, new_id, reason="old_already_renamed")
        new_location = new_row["location"] or ""

    db.record_code_rename(db_path, old_id, new_id, new_location, root)
    _force_renamed_status(db_path, root, new_id, manual=True)
    return RenameApplyResult(True, old_id, new_id)


def revert_rename(
    db_path: Path,
    root: Path,
    new_id: str,
) -> RenameRevertResult:
    """Un-weld a previously applied rename via symmetric migrate-back (US-6).

    Looks up the ``node_renames`` mapping for *new_id*, re-runs the migration
    in reverse (``record_code_rename(new_id -> old_id)``) -- no inverse-patch
    storage is kept -- then restores *old_id* as the live identity, detaches
    *new_id* as a fresh node, and clears the ``node_renames`` rows for the pair
    so the round-trip leaves no residual mapping.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        new_id: The current (renamed-to) identity to revert.

    Returns:
        :class:`RenameRevertResult`.  ``reverted`` is ``False`` with a
        ``reason`` when *new_id* has no recorded rename.
    """
    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT old_id, file_path FROM node_renames WHERE new_id = ? ORDER BY renamed_at DESC LIMIT 1",
            (new_id,),
        ).fetchone()
        if row is None:
            return RenameRevertResult(False, new_id, reason="no_rename_record")
        old_id = row["old_id"]
        old_loc = row["file_path"] or ""
        if not old_loc:
            new_row = conn.execute("SELECT location FROM nodes WHERE id = ?", (new_id,)).fetchone()
            old_loc = (new_row["location"] if new_row else "") or ""

    # Symmetric migrate-back: history/verification/edges return to old_id.
    db.record_code_rename(db_path, new_id, old_id, old_loc, root)

    now = db._now_utc()
    git_sha = _git_sha(root)
    with db._connect(db_path) as conn:
        # Fully un-weld: drop both the forward and the just-inserted reverse rows.
        conn.execute(
            "DELETE FROM node_renames WHERE (old_id = ? AND new_id = ?) OR (old_id = ? AND new_id = ?)",
            (old_id, new_id, new_id, old_id),
        )
        # Restore old as the live identity.
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (old_id,)).fetchone():
            conn.execute(
                "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
                (VERIFIED, VERIFIED, old_id),
            )
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (old_id, now, BECAME_VERIFIED, git_sha, json.dumps({"reverted_from": new_id})),
            )
        # Detach new as a fresh node (its migrated history moved back to old).
        if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (new_id,)).fetchone():
            conn.execute(
                "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
                (VERIFIED, VERIFIED, new_id),
            )
    return RenameRevertResult(True, new_id, old_id=old_id)


def _force_renamed_status(db_path: Path, root: Path, new_id: str, *, manual: bool) -> None:
    """Persist ``own_status = RENAMED`` on *new_id* with a transition event.

    Mirrors the auto-apply path's sticky overlay: the persisted ``RENAMED`` is
    preserved across subsequent builds (cleared only by ``mark_clean`` or a
    genuine ``NOT_FOUND``).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        new_id: Node to mark ``RENAMED``.
        manual: Whether this came from the manual ``apply_rename`` escape hatch
            (recorded in the history meta).
    """
    now = db._now_utc()
    git_sha = _git_sha(root)
    with db._connect(db_path) as conn:
        prev_row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (new_id,)).fetchone()
        prev = prev_row["own_status"] if prev_row else VERIFIED
        conn.execute("UPDATE nodes SET own_status = ? WHERE id = ?", (RENAMED, new_id))
        if prev != RENAMED:
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (new_id, now, BECAME_RENAMED, git_sha, json.dumps({"from_own": prev, "manual": manual})),
            )


def _git_sha(root: Path) -> str | None:
    """Return HEAD SHA for *root*, or ``None`` when git is unavailable."""
    from axiom_graph.index.git_utils import get_git_sha  # noqa: PLC0415

    return get_git_sha(root)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def fetch_history(
    db_path: Path,
    node_id: str,
    *,
    max_results: int = 10,
    offset: int = 0,
) -> HistoryResult:
    """Fetch paginated history rows for a single node.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: The node to inspect.
        max_results: Page size.
        offset: Number of entries to skip.

    Returns:
        :class:`HistoryResult` with the requested page and a total
        row count for the node.
    """
    fetch_limit = max_results + offset
    raw_rows = db.get_history(db_path, node_id, limit=fetch_limit)
    if not raw_rows:
        return HistoryResult(rows=[], total=0)

    with sqlite3.connect(db_path, timeout=5) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM node_history WHERE node_id = ?",
            (node_id,),
        ).fetchone()["c"]

    sliced = raw_rows[offset:]
    rows = [
        HistoryRow(
            node_id=r["node_id"],
            change_type=r["change_type"],
            scanned_at=r["scanned_at"],
            git_sha=r.get("git_sha"),
            meta=r.get("meta"),
        )
        for r in sliced
    ]
    return HistoryResult(rows=rows, total=total)


def list_reference_points(db_path: Path) -> list[ReferencePoint]:
    """List available reference points (CHECKPOINT + build SHAs).

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        List of :class:`ReferencePoint`, newest first.
    """
    refs = db.list_reference_points(db_path)
    return [
        ReferencePoint(
            git_sha=r["git_sha"],
            type=r["type"],
            scanned_at=r["scanned_at"],
            row_count=r["row_count"],
            message=r.get("message"),
        )
        for r in refs
    ]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def compute_report(
    db_path: Path,
    *,
    since_sha: str | None = None,
    since_timestamp: str | None = None,
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
) -> ReportData:
    """Classify history rows since a reference point into a report payload.

    Shared by CLI ``axiom-graph report`` and MCP ``axiom_graph_report``.
    Returns a :class:`ReportData` carrying summary counters and the
    per-bucket lists; presentation layers format it (text vs JSON for
    CLI, single text blob for MCP).

    Args:
        db_path: Path to the axiom-graph DB.
        since_sha: Git SHA prefix.
        since_timestamp: ISO-8601 datetime cutoff.
        change_type_pattern: Glob pattern for change types.
        node_pattern: Glob pattern for node IDs.
        node_type: Filter to nodes of this type.

    Returns:
        :class:`ReportData`.  ``no_rows`` is True when no history exists
        after the reference point.  ``no_matches`` is True when the
        filter dropped every row.
    """
    rows = db.get_history_since(
        db_path,
        since_timestamp=since_timestamp,
        since_sha=since_sha,
    )

    if not rows:
        return ReportData(
            summary={
                "nodes_changed": 0,
                "became_stale": 0,
                "became_clean": 0,
                "verified": 0,
                "agent_only": 0,
                "links_modified": 0,
            },
            content_changes={},
            staleness_transitions=[],
            link_changes=[],
            verifications=[],
            human_verified_ids=set(),
            no_rows=True,
        )

    has_filter = change_type_pattern or node_pattern or node_type
    if has_filter:
        nt_map = db.build_node_types_map(db_path) if node_type else None
        rows = db.filter_history_rows(
            rows,
            change_type_pattern=change_type_pattern,
            node_pattern=node_pattern,
            node_type=node_type,
            node_types_map=nt_map,
        )
        if not rows:
            return ReportData(
                summary={
                    "nodes_changed": 0,
                    "became_stale": 0,
                    "became_clean": 0,
                    "verified": 0,
                    "agent_only": 0,
                    "links_modified": 0,
                },
                content_changes={},
                staleness_transitions=[],
                link_changes=[],
                verifications=[],
                human_verified_ids=set(),
                no_matches=True,
            )

    content_types = {"INITIAL", "CONTENT_ONLY", "DESC_ONLY", "CONTENT_AND_DESC"}
    staleness_types = {
        BECAME_CONTENT_UPDATED,
        BECAME_DESC_UPDATED,
        BECAME_NOT_FOUND,
        BECAME_RENAMED,
        BECAME_LINKED_STALE,
        BECAME_BROKEN_LINK,
        LINK_BECAME_VERIFIED,
        BECAME_VERIFIED,
    }
    link_types = {"LINK_ADDED", "LINK_REMOVED"}
    verify_types = {"AGENT_VERIFIED", "MANUAL_VERIFIED"}

    content_changes: dict[str, list[dict]] = {}
    staleness_transitions: list[dict] = []
    link_changes: list[dict] = []
    verifications: list[dict] = []

    for row in rows:
        ct = row["change_type"]
        if ct in content_types:
            content_changes.setdefault(row["node_id"], []).append(row)
        elif ct in staleness_types:
            staleness_transitions.append(row)
        elif ct in link_types:
            link_changes.append(row)
        elif ct in verify_types:
            verifications.append(row)

    became_stale = [r for r in staleness_transitions if r["change_type"] != BECAME_VERIFIED]
    became_clean = [r for r in staleness_transitions if r["change_type"] == BECAME_VERIFIED]
    agent_only_ids = {r["node_id"] for r in verifications if r["change_type"] == "AGENT_VERIFIED"}
    human_verified_ids = {r["node_id"] for r in verifications if r["change_type"] == "MANUAL_VERIFIED"}
    agent_only_count = len(agent_only_ids - human_verified_ids)

    summary = {
        "nodes_changed": len(content_changes),
        "became_stale": len(set(r["node_id"] for r in became_stale)),
        "became_clean": len(set(r["node_id"] for r in became_clean)),
        "verified": len(agent_only_ids | human_verified_ids),
        "agent_only": agent_only_count,
        "links_modified": len(link_changes),
    }

    return ReportData(
        summary=summary,
        content_changes=content_changes,
        staleness_transitions=staleness_transitions,
        link_changes=link_changes,
        verifications=verifications,
        human_verified_ids=human_verified_ids,
    )


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


def checkout_db(
    source_db_path: Path,
    worktree_path: Path,
    *,
    force: bool = False,
) -> CheckoutResult:
    """Copy the axiom-graph DB into ``worktree_path`` via VACUUM INTO.

    Args:
        source_db_path: Path to the source ``.axiom_graph/graph.db``.
        worktree_path: Target directory.  A ``.axiom_graph/`` subdir
            will be created (by ``db_path_for``) if needed.
        force: When ``True``, an existing target DB is unlinked first.
            When ``False`` (default), the operation is skipped.

    Returns:
        :class:`CheckoutResult` with the target path and whether a copy
        actually happened.
    """
    target_dir = Path(worktree_path).resolve()
    target_db = db_path_for(target_dir)
    if target_db.exists():
        if force:
            target_db.unlink()
        else:
            return CheckoutResult(
                target_db_path=target_db,
                copied=False,
                skipped_reason="exists",
            )
    db.vacuum_into(source_db_path, target_db)
    from axiom_graph.registry import upsert_registry

    upsert_registry(target_dir)
    return CheckoutResult(target_db_path=target_db, copied=True)


# ---------------------------------------------------------------------------
# render_site
# ---------------------------------------------------------------------------


def render_site(
    root: Path,
    *,
    nav_path: Path | None = None,
    output_dir: Path | None = None,
    run_sphinx_build: bool = False,
) -> RenderSiteResult:
    """Render the consumer documentation site from DocJSON sources.

    Thin wrapper over ``axiom_graph.docjson.render_consumer.build_site``;
    presented as part of the lifecycle api so the MCP wire layer can
    follow the symmetric four-domain template.

    Args:
        root: Project root directory.
        nav_path: Path to ``site-nav.yml`` (default ``{root}/site-nav.yml``).
        output_dir: Output directory for the MyST pages (default
            ``{root}/userdocs/guide``).
        run_sphinx_build: When ``True``, also run ``sphinx-build``.

    Returns:
        :class:`RenderSiteResult`.
    """
    from axiom_graph.docjson.render_consumer import build_site

    result = build_site(
        root,
        nav_path=nav_path,
        output_dir=output_dir,
        run_sphinx_build=run_sphinx_build,
    )
    return RenderSiteResult(
        pages_rendered=result.pages_rendered,
        output_dir=result.output_dir,
        warnings=list(result.warnings),
    )


def render_targets(
    root: Path,
    *,
    only: list[str] | None = None,
    run_sphinx_build: bool = False,
):
    """Render every configured render target (or a named subset).

    Thin wrapper over
    :func:`axiom_graph.docjson.render_consumer.render_targets`.  Resolves
    ``[[axiom_graph.site.targets]]`` (or an implicit ``guide`` target when
    none are configured) and renders each, returning one result per target.

    Args:
        root: Project root directory.
        only: Optional list of target names to render; others are skipped.
        run_sphinx_build: When ``True``, run ``sphinx-build`` for sphinx
            targets.

    Returns:
        List of
        :class:`axiom_graph.docjson.render_consumer.RenderTargetResult`.
    """
    from axiom_graph.docjson.render_consumer import render_targets as _render_targets

    return _render_targets(root, only=only, run_sphinx_build=run_sphinx_build)


# ---------------------------------------------------------------------------
# Node diff (moved from axiom_graph/diff.py per ADR-019, cycle 2)
# ---------------------------------------------------------------------------


# Change types that represent a verified/checkpoint baseline
_BASELINE_CHANGE_TYPES = frozenset(
    {
        "AGENT_VERIFIED",
        "MANUAL_VERIFIED",
        "CHECKPOINT",
    }
)


def _parse_level3(level_3_location: str | None) -> tuple[str | None, int | None, int | None]:
    """Parse ``level_3_location`` into ``(file_path, start_line, end_line)``.

    Returns ``(None, None, None)`` when *level_3_location* is falsy.
    """
    if not level_3_location:
        return None, None, None
    m = re.match(r"^(.+?)(?:#L(\d+)(?:-L?(\d+))?)?$", level_3_location)
    if not m:
        return None, None, None
    file_part = m.group(1)
    start = int(m.group(2)) if m.group(2) else None
    end = int(m.group(3)) if m.group(3) else start
    return file_part, start, end


def _slice_lines(content: str, start: int | None, end: int | None) -> str:
    """Return the line-range slice of *content* (1-based, inclusive)."""
    if start is None:
        return content
    lines = content.splitlines()
    return "\n".join(lines[start - 1 : end])


@task(
    purpose="Return old vs new source for a code node relative to a baseline commit",
    inputs="db_path, project_root, node_id, optional baseline_sha",
    outputs="dict with old_content, new_content, baseline_sha, baseline_date, commit context",
)
def get_node_diff(
    db_path: Path,
    project_root: Path,
    node_id: str,
    baseline_sha: str | None = None,
) -> dict:
    """Return old vs new source for *node_id* relative to a baseline.

    **Baseline resolution** (when *baseline_sha* is ``None``):

    1. Walk ``node_history`` newest-first for a verified/checkpoint row with
       a non-NULL ``git_sha``.
    2. Fallback: use the *oldest* row that has a ``git_sha`` (typically the
       ``INITIAL`` scan row).

    When *baseline_sha* is provided it is used directly -- no history lookup.

    Returns
    -------
    dict
        On success: ``{old_content, new_content, baseline_sha, baseline_date}``.
        On failure: ``{error: "no_baseline", reason: "..."}``.
    """
    口 = Step(
        step_num=1,
        name="Look up node and parse location",
        purpose="Get the node's source file path and line range from level_3_location",
    )
    node = db.get_node(db_path, node_id)
    if node is None:
        return {"error": "no_baseline", "reason": f"Node not found: {node_id}"}

    file_path, start, end = _parse_level3(node.level_3_location)
    if file_path is None:
        file_path = node.location
    if not file_path:
        return {"error": "no_baseline", "reason": "Node has no source location"}

    口 = Step(
        step_num=2,
        name="Resolve baseline SHA",
        purpose="Find the git commit to diff against -- prefer verified/checkpoint, fall back to oldest SHA",
    )
    baseline_date: str | None = None

    if baseline_sha is not None:
        rows = db.get_history(db_path, node_id, limit=100)
        for row in rows:
            if row.get("git_sha") == baseline_sha:
                baseline_date = row["scanned_at"]
                break
    else:
        rows = db.get_history(db_path, node_id, limit=100)
        verified_row = None
        any_sha_row = None
        for row in rows:
            if row.get("git_sha"):
                if any_sha_row is None:
                    any_sha_row = row
                if verified_row is None and row["change_type"] in _BASELINE_CHANGE_TYPES:
                    verified_row = row
        oldest_sha_row = None
        for row in reversed(rows):
            if row.get("git_sha"):
                oldest_sha_row = row
                break

        baseline_row = verified_row or oldest_sha_row or any_sha_row
        if baseline_row is None:
            return {
                "error": "no_baseline",
                "reason": "No history entry with a git SHA",
            }
        baseline_sha = baseline_row["git_sha"]
        baseline_date = baseline_row["scanned_at"]

    口 = Step(
        step_num=3,
        name="Retrieve old content via git show",
        purpose="Get the file content at the baseline commit and slice to node's line range",
    )
    git_path = file_path.replace("\\", "/")
    try:
        result = subprocess.run(
            ["git", "show", f"{baseline_sha}:{git_path}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        if result.returncode != 0:
            if "does not exist" in result.stderr or "exists on disk" in result.stderr:
                old_file_content = ""
            else:
                stderr = result.stderr.strip()
                return {"error": "no_baseline", "reason": f"git show failed: {stderr}"}
        else:
            old_file_content = result.stdout
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out for %s:%s", baseline_sha, git_path)
        return {"error": "no_baseline", "reason": "git show timed out"}
    except Exception as exc:
        logger.warning("git show error: %s", exc)
        return {"error": "no_baseline", "reason": f"git error: {exc}"}

    口 = Step(
        step_num=4,
        name="Read current content and slice both to line range",
        purpose="Read the current file from disk and slice both old and new to the node's line range",
        critical="Line range from level_3_location is based on last build -- if code was added/removed above the node, the slice may be off until the next build",
    )
    src_file = Path(project_root) / file_path
    if not src_file.exists():
        return {"error": "no_baseline", "reason": f"Source file not found: {file_path}"}

    new_file_content = src_file.read_text(encoding="utf-8", errors="replace")

    old_content = _slice_lines(old_file_content, start, end)
    new_content = _slice_lines(new_file_content, start, end)

    口 = Step(
        step_num=5, name="Get commit context", purpose="Retrieve commit subject, author, and date for the baseline SHA"
    )
    commit_subject: str | None = None
    commit_author: str | None = None
    commit_date: str | None = None
    try:
        log_result = subprocess.run(
            ["git", "log", "-1", "--format=%s%n%an%n%aI", baseline_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(project_root),
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        if log_result.returncode == 0:
            log_lines = log_result.stdout.strip().splitlines()
            if len(log_lines) >= 1:
                commit_subject = log_lines[0]
            if len(log_lines) >= 2:
                commit_author = log_lines[1]
            if len(log_lines) >= 3:
                commit_date = log_lines[2]
    except subprocess.TimeoutExpired:
        logger.warning("git log timed out for %s", baseline_sha)
    except Exception as exc:
        logger.warning("git log error for commit context: %s", exc)

    return {
        "old_content": old_content,
        "new_content": new_content,
        "baseline_sha": baseline_sha,
        "baseline_date": baseline_date,
        "commit_subject": commit_subject,
        "commit_author": commit_author,
        "commit_date": commit_date,
    }
