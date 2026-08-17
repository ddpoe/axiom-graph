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
import os
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from axiom_annotations import task, workflow, Step

from axiom_graph.config import AxiomGraphConfig, db_path_for
from axiom_graph.index import builder, db
from axiom_graph.index.mark_clean import (
    VERIFICATION_OP_MARK_CLEAN,
    VERIFICATION_OP_REVERIFY,
)
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
    """Result of :func:`build_index`.

    Attributes:
        files_skipped_mtime: Code files the mtime fast-pass skipped.
        docs_skipped_mtime: Markdown and DocJSON files the mtime fast-pass
            skipped.  Counted apart from ``files_skipped_mtime`` because
            doc files are walked by their own scanners; without it there is
            no observable evidence that a doc file was left unread.
    """

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
    docs_skipped_mtime: int = 0


@dataclass
class CheckSummary:
    """Result of :func:`compute_check_summary`."""

    own_counts: dict[str, int]
    link_counts: dict[str, int]
    clean_count: int
    doc_quality_count: int
    all_clean: bool
    statuses: dict[str, tuple[str, str, list[str]]]


@dataclass
class MarkCleanResult:
    """Result of :func:`mark_clean_nodes`.

    ``inherited`` and ``mixed`` carry the aggregate-honesty
    classification (empty for ordinary nodes — the happy-path shape is
    unchanged):

    - ``inherited``: node ID -> stale descendant IDs, for targets whose
      LINKED_STALE is *inherited* from their ``composes`` subtree with
      no own stale signal.  Marking them wrote a verification row but
      has no direct effect — the next recompute re-derives the parent's
      link_status from those descendants.
    - ``mixed``: node ID -> stale descendant IDs, for targets that had
      an own stale signal (genuinely cleared) AND stale descendants
      (the inherited portion survives the recompute).
    """

    marked: list[str]
    not_found: list[str]
    inherited: dict[str, list[str]] = field(default_factory=dict)
    mixed: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ReverifyResult:
    """Result of :func:`reverify_node`.

    - ``verified``: nodes that received verification rows in this call
      (the source first, then cascade-cleared dependents with
      reverify-of-source provenance).
    - ``cleared``: nodes whose persisted LINKED_STALE cleared during
      this operation (superset of the cascade set — includes aggregates
      cleared purely by composite inheritance in the final recompute).
    - ``skipped``: nodes attributed partly to the source but still
      outstanding via other root offenders — left LINKED_STALE, mapped
      to those offender IDs.  Offenders already reverified are not
      listed; the map shrinks as the composition is completed.
    - ``before_linked_stale`` / ``after_linked_stale``: persisted
      LINKED_STALE counts at entry and after the recompute.
    """

    source_id: str
    not_found: bool = False
    verified: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    before_linked_stale: int = 0
    after_linked_stale: int = 0


#: One-line action a presentation surface appends to reverify's skip
#: block.  Defined here so every surface renders the same string; the
#: layout of the block stays with the surface.  It names the action and
#: deliberately does not repeat the offender IDs printed above it.
REVERIFY_SKIP_HINT = (
    "Reverify each offender listed above — a dependent clears once every offender behind it is reverified."
)


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
        docs_skipped_mtime=summary.get("docs_skipped_mtime", 0) or 0,
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

    return CheckSummary(
        own_counts=own_counts,
        link_counts=link_counts,
        clean_count=clean_count,
        doc_quality_count=doc_quality_count,
        all_clean=all_clean,
        statuses=statuses,
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
    verification_op: str = VERIFICATION_OP_MARK_CLEAN,
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
        verification_op: Which operation is writing these verifications,
            recorded as provenance in each history row's ``meta``
            payload.  Defaults to
            :data:`~axiom_graph.index.mark_clean.VERIFICATION_OP_MARK_CLEAN`;
            :func:`reverify_node` sets it for the source it names.

    Returns:
        :class:`MarkCleanResult` with marked vs not_found IDs plus the
        aggregate-honesty classification (``inherited`` / ``mixed``).
    """
    from axiom_graph.index.mark_clean import mark_node_clean
    from axiom_graph.index.staleness import (
        _composes_children_map,
        _get_linked_stale_ids,
        classify_inherited_link,
    )

    # Classify aggregate targets BEFORE marking: marking writes
    # verified_at, which clears each target's own signal from the live
    # stale map and would misclassify own-signal nodes as inherited-only.
    # Ordinary (childless) targets skip the stale-map computation.
    inherited: dict[str, list[str]] = {}
    mixed: dict[str, list[str]] = {}
    children_map = _composes_children_map(db_path)
    if any(nid in children_map for nid in node_ids):
        config = AxiomGraphConfig.load(root)
        stale_map = _get_linked_stale_ids(
            db_path,
            transitive_tags=config.staleness.transitive_tags,
            frozen_tags=config.staleness.frozen_tags,
        )
        batch = set(node_ids)
        for nid in node_ids:
            if nid not in children_map:
                continue
            # Other batch members with own signals are genuinely cleared
            # by this very call — exclude them from the hint so the
            # report only names descendants that will remain stale.
            has_own, stale_desc = classify_inherited_link(
                db_path,
                nid,
                stale_map,
                children_map=children_map,
                exclude=batch - {nid},
            )
            if not stale_desc:
                continue
            if has_own:
                mixed[nid] = stale_desc
            else:
                inherited[nid] = stale_desc

    marked: list[str] = []
    not_found: list[str] = []
    for nid in node_ids:
        node = db.get_node(db_path, nid)
        if node is None:
            not_found.append(nid)
            continue
        mark_node_clean(db_path, root, node, reason, verified_by, verification_op=verification_op)
        marked.append(nid)

    # Classifications only apply to nodes that were actually marked.
    for nid in not_found:
        inherited.pop(nid, None)
        mixed.pop(nid, None)

    return MarkCleanResult(marked=marked, not_found=not_found, inherited=inherited, mixed=mixed)


def reverify_node(
    db_path: Path,
    root: Path,
    source_node_id: str,
    reason: str,
    *,
    verified_by: str,
) -> ReverifyResult:
    """Verify *source_node_id* and clear the LINKED_STALE it caused.

    One-operation scoped clear: asserts "I verified the source; my change
    to it does not invalidate its dependents".  The flow:

    1. Expand the source to its full ``composes`` subtree (composite
       sources match staleness rooted at any of their parts).
    2. Compute the live stale map with the same transitive/frozen tag
       configuration as ``check``.
    3. Resolve every stale entry's via chain to its leaf root offenders
       (:func:`axiom_graph.index.staleness.resolve_root_offenders`).
    4. Narrow each root set to the offenders that are still outstanding:
       offenders already reverified since their last change drop out
       (:func:`axiom_graph.index.staleness.already_reverified_offenders`).
       **Reverifies compose** — reverifying every offender behind a
       dependent reaches the same end state as marking that dependent
       clean directly, so the last reverify in the series clears it.
    5. Select nodes whose outstanding offenders all fall within the
       source set; nodes still outstanding via *other* offenders are
       skipped and reported (under-clearing is acceptable,
       over-clearing is not).
    6. Clear via :func:`mark_clean_nodes` — verification rows remain the
       only clearing mechanism; cascade rows carry
       ``[reverify:<source>]`` provenance in reason/history.
    7. Finish with the shared staleness recompute (the same single-writer
       path ``check`` uses), so aggregates cleared by composite
       inheritance are visible in this call's own report.

    The source itself is always marked verified — it is the explicit,
    named target of the operation — even when there is nothing to clear
    (idempotent "nothing to clear" reports are success, not errors).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        source_node_id: The node the caller verified (any node type).
        reason: Free-form reason recorded in history/verification rows.
        verified_by: Verifier identifier (``"human"``, ``"agent"``, ...).
            Required keyword.

    Returns:
        :class:`ReverifyResult` with verified/cleared/skipped node IDs
        and before/after LINKED_STALE counts.
    """
    from axiom_graph.index.staleness import (
        _get_linked_stale_ids,
        already_reverified_offenders,
        expand_composes_subtree,
        resolve_root_offenders,
    )

    node = db.get_node(db_path, source_node_id)
    if node is None:
        return ReverifyResult(source_id=source_node_id, not_found=True)

    # Persisted LINKED_STALE surface at entry (the "before" set).
    persisted = db.get_all_staleness(db_path)
    before_ids = {nid for nid, (_own, link) in persisted.items() if link == LINKED_STALE}

    # Live stale map with check-parity configuration.
    config = AxiomGraphConfig.load(root)
    stale_map = _get_linked_stale_ids(
        db_path,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
    )

    # Attribution: which stale nodes root entirely at the source?
    source_set = {source_node_id} | expand_composes_subtree(db_path, source_node_id)
    roots_map = resolve_root_offenders(stale_map)

    # Reverifies compose: an offender reverified since its own last change
    # is no longer outstanding, so a series of reverifies adds up.  Read
    # once for every root offender in play, then decided by the pure
    # primitive.  Computed before any write below, so this call never
    # discounts its own source mid-flight (the source is in source_set).
    all_roots = {rid for roots in roots_map.values() for rid in roots}
    latest_change_ids, verification_ops = db.get_verification_ordering_rows(db_path, sorted(all_roots))
    already_reverified = already_reverified_offenders(
        all_roots,
        latest_change_ids=latest_change_ids,
        verification_ops=verification_ops,
    )

    selected: list[str] = []
    skipped: dict[str, list[str]] = {}
    for nid, roots in sorted(roots_map.items()):
        if nid == source_node_id:
            # The source is verified below regardless of its own staleness.
            continue
        root_set = set(roots)
        if not root_set or not (root_set & source_set):
            # Unattributable (empty root set) or rooted entirely at other
            # offenders — untouched, conservatively.  Evaluated on the RAW
            # root set: narrowing here would make a dependent whose
            # remaining offenders lie outside the source set look
            # unrelated, silently dropping it from the skip report
            # instead of reporting what is still outstanding.
            continue
        outstanding = root_set - already_reverified
        if outstanding <= source_set:
            selected.append(nid)
        else:
            skipped[nid] = sorted(outstanding - source_set)

    # ADR boundary: verification rows via the mark_clean machinery remain
    # the ONLY clearing mechanism.  Source first (plain reason, marked as
    # reverify-written so it becomes a term in the composition), then the
    # cascade set with reverify-of-source provenance.
    mark_clean_nodes(
        db_path,
        root,
        [source_node_id],
        reason,
        verified_by=verified_by,
        verification_op=VERIFICATION_OP_REVERIFY,
    )
    if selected:
        cascade_reason = f"[reverify:{source_node_id}] {reason}" if reason else f"[reverify:{source_node_id}]"
        mark_clean_nodes(db_path, root, selected, cascade_reason, verified_by=verified_by)

    # Shared recompute — same single-writer record_staleness path check
    # uses.  include_frozen=True keeps sticky frozen sections in the
    # after-surface so they never spuriously appear "cleared".
    cs = compute_check_summary(db_path, root, include_frozen=True)
    after_ids: set[str] = set()
    if cs is not None:
        after_ids = {nid for nid, (_own, link, _via) in cs.statuses.items() if link == LINKED_STALE}

    return ReverifyResult(
        source_id=source_node_id,
        verified=[source_node_id, *selected],
        cleared=sorted(before_ids - after_ids),
        skipped=skipped,
        before_linked_stale=len(before_ids),
        after_linked_stale=len(after_ids),
    )


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


# ---------------------------------------------------------------------------
# Net "changed since" diff (Option A) — true state-diff vs a baseline commit
# ---------------------------------------------------------------------------

# Map the four-field ``_derive_change_type`` verdicts to the public net kinds.
_CHANGE_TYPE_TO_KIND = {
    "CONTENT_ONLY": "content",
    "DESC_ONLY": "desc",
    "CONTENT_AND_DESC": "content+desc",
}


@dataclass
class NetDiffResult:
    """The net state-diff of the index's built state vs a baseline commit.

    Attributes:
        change_kinds: ``{node_id: [kind, ...]}`` for every node whose built
            state differs from the baseline. Kinds: ``added``, ``content``,
            ``desc``, ``content+desc``, ``renamed``. ``deleted`` is carried by
            the endpoint's ghost-synthesis path, not here.
        node_ids: Sorted list of the changed node ids (the keys of
            ``change_kinds``).
        baseline_sha: The baseline commit the diff was computed against.
        git_calls: Number of git invocations made (instrumentation for the
            O(changed files) acceptance criterion).
    """

    change_kinds: dict[str, list[str]] = field(default_factory=dict)
    node_ids: list[str] = field(default_factory=list)
    baseline_sha: str | None = None
    git_calls: int = 0


def compute_net_diff(
    db_path: Path,
    project_root: Path,
    baseline_sha: str,
    current_sha: str,
) -> NetDiffResult:
    """Compute the net state-diff of the built index vs *baseline_sha*.

    Replaces the event-log replay in the "changed since" endpoint with a true
    net diff, so an edit-then-revert cancels to zero and each changed node is
    labelled by kind. Two stages, both O(changed files):

    **Stage 1 — file membership.** One ``git diff --name-status -M`` call
    (the keystone :func:`get_name_status_changes`) yields the full A/M/D/R set.
    Modified + renamed (new-side) + added paths map to candidate nodes via
    ``nodes.location``. Coarse-but-correct — over-includes by design.

    **Stage 2 — per-node precision + kind.** For each candidate:

    * If the node's path is in the keystone **A** (added) set, label it
      ``added`` and skip the classifier (D-4): an added node's baseline blob is
      empty-string, so ``_derive_change_type`` would mislabel it ``content``.
    * Renamed nodes (path is a rename new-side) are labelled ``renamed``.
    * Otherwise compare the **baseline-blob** ``(code_hash, desc_hash)``
      (re-derived from ``git show <baseline_sha>:<old_path>`` via the same
      hashing dispatch) against the **DB's stored** ``(code_hash, desc_hash)``
      (the built state — D-2, *not* the on-disk file). Feed both to
      :func:`_derive_change_type`; ``None`` drops the node (this is where
      edit-then-revert cancels).

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Absolute path to the project root (git repo).
        baseline_sha: The "old" side commit to diff against.
        current_sha: The index's built SHA (the "new" side anchor). Used for
            the single name-status call; the per-node current hashes come from
            the DB, not this commit's blobs.

    Returns:
        A :class:`NetDiffResult`. Empty when either SHA is falsy.
    """
    from axiom_graph.index import git_utils  # noqa: PLC0415
    from axiom_graph.scanners.node_hashing import node_hashes_for_blob  # noqa: PLC0415

    result = NetDiffResult(baseline_sha=baseline_sha)
    if not baseline_sha or not current_sha:
        return result

    # --- Stage 1: file membership (one git call) ---
    changes = git_utils.get_name_status_changes(project_root, baseline_sha, current_sha)
    result.git_calls += 1

    # Reverse rename map: new_path -> old_path (baseline blob lives at old_path).
    new_to_old: dict[str, str] = {new: old for old, new in changes.renamed.items()}
    renamed_new_paths = set(changes.renamed.values())

    # Paths whose nodes are candidates: modified in place, renamed (new side),
    # or added. Deleted paths are handled by the endpoint's ghost path.
    candidate_paths = changes.modified | renamed_new_paths | changes.added
    if not candidate_paths:
        return result

    # Group the built nodes by their current (POSIX) location.
    nodes_by_location: dict[str, list] = {}
    for node in db.all_nodes(db_path):
        loc = (node.location or "").replace("\\", "/")
        if loc in candidate_paths:
            nodes_by_location.setdefault(loc, []).append(node)

    # --- Stage 2: per-node precision + kind ---
    for path, nodes in nodes_by_location.items():
        is_added = path in changes.added
        is_renamed = path in renamed_new_paths

        if is_added:
            # D-4: the A set is the only clean add signal. Branch BEFORE the
            # classifier — an added node's baseline blob is "" (not None), so
            # the classifier would mislabel it `content`.
            for node in nodes:
                result.change_kinds.setdefault(node.id, []).append("added")
            continue

        if is_renamed:
            # Renamed nodes are labelled `renamed` (not delete+add). The
            # name-status -M call already detected the file rename.
            for node in nodes:
                result.change_kinds.setdefault(node.id, []).append("renamed")
            continue

        # Modified in place: hash the baseline blob and compare to STORED hashes.
        old_path = new_to_old.get(path, path)
        blob = git_utils.get_old_body(project_root, baseline_sha, old_path, None, None)
        result.git_calls += 1
        if blob is None:
            # No clean baseline blob (e.g. config / envelope with no span, or
            # an unreachable blob) — exclude from net membership.
            continue
        baseline_hashes = node_hashes_for_blob(blob, nodes, project_root, path)
        # Also hash the CURRENT on-disk file through the same dispatch so we can
        # tell a genuinely-new node (present now, absent at baseline) apart from
        # a node the hashing dispatch legitimately skips (module composites,
        # pass-through subtypes) — those must NOT be mislabelled `added`.
        current_dispatch = node_hashes_for_blob(
            (project_root / path).read_text(encoding="utf-8", errors="replace")
            if (project_root / path).exists()
            else blob,
            nodes,
            project_root,
            path,
        )
        result.git_calls += 0  # local file read, not a git call
        for node in nodes:
            base = baseline_hashes.get(node.id)
            if base is None:
                # Node absent from the baseline blob. Only label `added` when
                # the dispatch DOES produce a current hash for it (a real new
                # node within a modified file). If the dispatch skips it too
                # (module composite / pass-through), it has no own hash story —
                # drop it (its constituent functions carry the signal).
                if node.id in current_dispatch:
                    result.change_kinds.setdefault(node.id, []).append("added")
                continue
            base_code, base_desc = base
            change_type = db._derive_change_type(base_code, base_desc, node.code_hash or "", node.desc_hash)
            kind = _CHANGE_TYPE_TO_KIND.get(change_type)
            if kind is None:
                # None -> unchanged (revert cancels here); INITIAL -> handled
                # by the added branch above. Drop everything else.
                continue
            result.change_kinds.setdefault(node.id, []).append(kind)

    result.node_ids = sorted(result.change_kinds.keys())
    return result


def recover_deleted_source(
    project_root: Path,
    location: str,
    *,
    preserved_sha: str | None,
    preserved_level_3: str | None,
    baseline_sha: str | None,
) -> str | None:
    """Recover the baseline source text of a deleted ghost node.

    Deleted ghosts are purged from the index, so they cannot route through
    :func:`get_node_diff` / ``get_node_source`` (both 404 on a missing node).
    This reads the old source directly from git.

    Two tiers (D-5):

    * **Exact-span** — when the DELETED history row preserved a SHA and a
      ``level_3_location`` span (ghosts deleted after this ships):
      ``git show <preserved_sha>:<location>`` sliced to the preserved span.
    * **Legacy whole-file fallback** — when span/SHA are absent (ghosts deleted
      before this ships): ``git show <baseline_sha>:<location>`` whole file.

    Never raises on a missing span/SHA; returns ``None`` only when the blob is
    genuinely unreachable.

    Args:
        project_root: Absolute path to the project root (git repo).
        location: Repo-relative path of the deleted node's file.
        preserved_sha: SHA preserved in the DELETED meta, or ``None`` (legacy).
        preserved_level_3: ``level_3_location`` span preserved in the DELETED
            meta, or ``None`` (legacy).
        baseline_sha: The net-diff's baseline commit, used for the legacy
            whole-file fallback.

    Returns:
        The recovered baseline source text, or ``None`` if unreachable.
    """
    from axiom_graph.index import git_utils  # noqa: PLC0415

    if not location:
        return None

    # Exact-span tier.
    if preserved_sha and preserved_level_3:
        _file, start, end = _parse_level3(preserved_level_3)
        body = git_utils.get_old_body(project_root, preserved_sha, location, start, end)
        if body is not None:
            return body
        # Fall through to whole-file if the exact-span blob is unreachable.

    # Legacy whole-file fallback.
    if baseline_sha:
        return git_utils.get_old_body(project_root, baseline_sha, location, None, None)

    # Last resort: try the preserved SHA whole-file if we have one.
    if preserved_sha:
        return git_utils.get_old_body(project_root, preserved_sha, location, None, None)

    return None


# ---------------------------------------------------------------------------
# Doc-ID migration (plan / preview / execute)
# ---------------------------------------------------------------------------


#: Directories never scanned for prose doc-ID references.
_PROSE_SKIP_DIRS = frozenset(
    {
        ".git",
        ".axiom_graph",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
    }
)

#: Extensions treated as binary and skipped by the prose scan.
_PROSE_SKIP_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".whl",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".pyc",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".mp4",
        ".webm",
        ".mp3",
        ".onnx",
        ".bin",
    }
)

#: Largest file the prose scan will read.
_PROSE_MAX_BYTES = 2_000_000

#: The prose scan never counts a reference the migration rewrites.  On disk
#: those are ``links[].node_id`` entries, one per line in DocJSON.
_LINK_LINE_PREFIX = '"node_id"'


@dataclass(frozen=True)
class ProseReference:
    """One textual doc-ID reference the migration will not rewrite.

    Attributes:
        file_path: Repo-relative path of the file containing the reference.
        line: 1-based line number.
        text: The matched reference, exactly as written.
        doc_id: The document envelope the reference resolves to, or ``None``
            when the reference does not match any document on disk.
        is_section_reference: ``True`` when the reference names a section
            rather than the document envelope.
    """

    file_path: str
    line: int
    text: str
    doc_id: str | None
    is_section_reference: bool


@dataclass
class DocIdMigrationPlan:
    """Everything a doc-ID migration would do, computed without writing.

    Attributes:
        project_id: Project ID prefix.
        docs_roots: Configured docs roots that exist on disk.
        documents: One old -> new mapping per document envelope.
        sections: One old -> new mapping per section node.
        collisions: Duplicate groups in the *projected* ID set.  Non-empty
            means execute mode is unreachable.
        current_collisions: Duplicate groups in the current ID set — the
            overlaps that already exist today.
        dotted_filenames: Repo-relative paths with extra dots in the stem.
        prose_in_docjson_content: References inside DocJSON section content.
        prose_elsewhere: References in every other file in the repository.
        unreadable: Files under a docs root that could not be read or
            parsed, plus documents that yielded no section identity.
            Ordinary JSON data files are *not* listed — they are not
            documents and nothing about them is a finding.
        revert_supported: Whether a per-document revert path exists.  Always
            ``False`` — rollback is restoring the backup.
    """

    project_id: str
    docs_roots: list[str] = field(default_factory=list)
    documents: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    collisions: list = field(default_factory=list)
    current_collisions: list = field(default_factory=list)
    dotted_filenames: list[str] = field(default_factory=list)
    prose_in_docjson_content: list[ProseReference] = field(default_factory=list)
    prose_elsewhere: list[ProseReference] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    revert_supported: bool = False

    @property
    def document_count(self) -> int:
        """Number of document envelopes the migration would move."""
        return len(self.documents)

    @property
    def section_count(self) -> int:
        """Number of section nodes the migration would move."""
        return len(self.sections)

    @property
    def total_nodes(self) -> int:
        """Total node identities the migration would move."""
        return len(self.documents) + len(self.sections)

    @property
    def blocked(self) -> bool:
        """Whether a doc-ID collision blocks execution.

        Both classes count.  A duplicate in the *projected* set would create
        the very overwrite the migration exists to prevent; a duplicate in
        the *current* set means the index already holds one identity for two
        files, so there is no coherent identity to move.
        """
        return bool(self.collisions or self.current_collisions)

    @property
    def blocking_collisions(self) -> list:
        """Every collision group that makes execute mode unreachable."""
        return list(self.collisions) + list(self.current_collisions)

    def as_mapping(self) -> dict[str, str]:
        """Return the document-level old -> new mapping."""
        return {m.old_id: m.new_id for m in self.documents}


@dataclass
class DocIdMigrationResult:
    """Outcome of an executed doc-ID migration.

    Attributes:
        executed: Whether the index was written.
        reason: Machine-readable refusal / abort reason when not executed.
        backup_path: Where the pre-write database copy was written.
        documents_migrated: Document envelopes moved.
        sections_migrated: Section nodes moved.
        files_patched: DocJSON files whose links were rewritten on disk.
        doc_files_read: DocJSON files opened by the link rewrite — one pass
            over the tree, not one pass per rename.
        aborted_at: The document ID the batch failed on, when it aborted.
        error: The failure text, when it aborted.
        restored_from_backup: Whether the DB was restored after an abort.
        plan: The plan the run gated on.
    """

    executed: bool
    reason: str | None = None
    backup_path: Path | None = None
    documents_migrated: int = 0
    sections_migrated: int = 0
    files_patched: int = 0
    doc_files_read: int = 0
    aborted_at: str | None = None
    error: str | None = None
    restored_from_backup: bool = False
    plan: DocIdMigrationPlan | None = None


def _prose_pattern(project_id: str) -> re.Pattern[str]:
    """Return the regex matching a maximal doc-ID reference for *project_id*.

    Matches the longest ``{project_id}::docs.<path>[::<section>]`` token, so a
    reference to a section is never miscounted as a reference to its document
    envelope — a document ID is a prefix of every one of its section IDs.

    Args:
        project_id: Project ID prefix.

    Returns:
        A compiled pattern.
    """
    segment = r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*"
    return re.compile(rf"{re.escape(project_id)}::docs\.{segment}(?:::{segment})?")


def _classify_reference(text: str, known_doc_ids: set[str]) -> tuple[str | None, bool]:
    """Resolve a matched reference to its owning document envelope.

    Args:
        text: The matched reference token.
        known_doc_ids: Every document envelope ID derived from disk.

    Returns:
        ``(doc_id, is_section_reference)``.  ``doc_id`` is ``None`` when the
        reference resolves to no document on disk.
    """
    parts = text.split("::")
    if len(parts) >= 3:
        envelope = "::".join(parts[:2])
        return (envelope if envelope in known_doc_ids else None), True
    return (text if text in known_doc_ids else None), False


def _scan_text_for_references(
    rel_path: str,
    text: str,
    pattern: re.Pattern[str],
    known_doc_ids: set[str],
    *,
    skip_link_lines: bool,
) -> list[ProseReference]:
    """Collect doc-ID references from a file's text, one record per match.

    Args:
        rel_path: Repo-relative path, used in the returned records.
        text: Full file text.
        pattern: Compiled reference pattern.
        known_doc_ids: Every document envelope ID derived from disk.
        skip_link_lines: When ``True``, lines carrying a ``links[].node_id``
            entry are ignored — those references *are* rewritten, so counting
            them in a not-patched report would be a lie.

    Returns:
        One :class:`ProseReference` per match, in file order.
    """
    out: list[ProseReference] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if skip_link_lines and line.lstrip().startswith(_LINK_LINE_PREFIX):
            continue
        for match in pattern.finditer(line):
            doc_id, is_section = _classify_reference(match.group(0), known_doc_ids)
            out.append(
                ProseReference(
                    file_path=rel_path,
                    line=lineno,
                    text=match.group(0),
                    doc_id=doc_id,
                    is_section_reference=is_section,
                )
            )
    return out


@task(
    purpose="Enumerate textual doc-ID references the migration will not rewrite, split into DocJSON content and everything else",
    inputs="project root, project_id, enumerated DocJSON files, known doc ids, excluded dirs",
    outputs="(in_docjson_content, elsewhere) — ProseReference lists with file and line",
)
def scan_doc_id_prose_references(
    root: Path,
    project_id: str,
    doc_files: list,
    known_doc_ids: set[str],
    *,
    exclude_dirs: tuple[str, ...] = (),
) -> tuple[list[ProseReference], list[ProseReference]]:
    """Return doc-ID references in DocJSON prose and in the rest of the repo.

    Two buckets, deliberately distinguishable: references inside DocJSON
    section content are data axiom-graph owns; references anywhere else are
    not.  Neither bucket is rewritten by the migration — ``links[].node_id``
    entries, which *are* rewritten, are excluded from the DocJSON bucket.

    Args:
        root: Absolute project root.
        project_id: Project ID prefix.
        doc_files: ``DocFile`` records for the DocJSON *documents* under the
            docs roots.  Anything else under a docs root is scanned as an
            ordinary repository file, not as DocJSON content.
        known_doc_ids: Every document envelope ID derived from disk.
        exclude_dirs: Additional directory names to skip.

    Returns:
        ``(in_docjson_content, elsewhere)``.
    """
    pattern = _prose_pattern(project_id)
    docjson_paths = {f.path.resolve() for f in doc_files}

    in_content: list[ProseReference] = []
    for doc_file in doc_files:
        try:
            text = doc_file.path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file
            continue
        in_content.extend(
            _scan_text_for_references(
                doc_file.rel_path,
                text,
                pattern,
                known_doc_ids,
                skip_link_lines=True,
            )
        )

    skip_dirs = _PROSE_SKIP_DIRS | set(exclude_dirs)
    needle = f"{project_id}::docs."
    elsewhere: list[ProseReference] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in _PROSE_SKIP_SUFFIXES:
                continue
            try:
                if path.resolve() in docjson_paths:
                    continue
                if path.stat().st_size > _PROSE_MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:  # pragma: no cover - unreadable file
                continue
            if needle not in text:
                continue
            rel = path.relative_to(root).as_posix()
            elsewhere.extend(_scan_text_for_references(rel, text, pattern, known_doc_ids, skip_link_lines=False))

    return in_content, elsewhere


@task(
    purpose="Compute the complete doc-ID migration plan without writing anything",
    inputs="db_path, project root",
    outputs="DocIdMigrationPlan — projection, collision verdict, advisories, prose buckets",
)
def plan_doc_id_migration(db_path: Path, root: Path) -> DocIdMigrationPlan:
    """Project every doc-ID move and gate it, writing nothing.

    The projection enumerates DocJSON files **from disk** rather than from the
    ``docs`` table: table rows only cover whatever the last build happened to
    walk, and the build's mtime fast-pass means that is not the whole tree.

    Every ``*.json`` under a docs root is enumerated, then classified.  Only
    DocJSON documents are planned against: ordinary JSON data files beside
    the docs never become doc nodes, so they get no projected ID, no
    collision candidacy, no advisory, and no say in the gate.  Files that
    cannot be read or parsed have no identity either, but are reported in
    ``unreadable`` alongside documents that yielded no sections.

    Args:
        db_path: Path to the axiom-graph DB.  Not read — accepted so plan and
            execute share one signature shape.
        root: Absolute project root.

    Returns:
        A :class:`DocIdMigrationPlan`.  ``blocked`` is ``True`` when two
        *documents* share one doc ID — in the projected set, which would
        recreate the overwrite this migration exists to remove, or in the
        current set, where there is no coherent identity to move.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = config.project_id or root.name

    scan = doc_ids.classify_doc_files(doc_ids.enumerate_doc_files(root, config.scan.docs_dirs))
    documents = scan.documents
    projection = doc_ids.project_doc_ids(project_id, documents)
    known_doc_ids = {m.old_id for m in projection.documents}

    in_content, elsewhere = scan_doc_id_prose_references(
        root,
        project_id,
        documents,
        known_doc_ids,
        exclude_dirs=tuple(config.scan.exclude_dirs or ()),
    )

    return DocIdMigrationPlan(
        project_id=project_id,
        docs_roots=[entry for entry, _abs in doc_ids.resolve_docs_roots(root, config.scan.docs_dirs)],
        documents=projection.documents,
        sections=projection.sections,
        collisions=doc_ids.find_collisions(projection.new_id_sources),
        current_collisions=doc_ids.find_collisions(doc_ids.current_doc_id_index(project_id, documents)),
        dotted_filenames=doc_ids.dotted_filenames(documents),
        prose_in_docjson_content=in_content,
        prose_elsewhere=elsewhere,
        unreadable=sorted(set(scan.unreadable) | set(projection.unreadable)),
    )


def _order_doc_renames(mapping: dict[str, str]) -> list[str] | None:
    """Order document renames so no rename lands on a live identity.

    The collision gate proves the *destination* set is injective; it does not
    prove any arbitrary order is safe.  A projected new ID can equal some
    other document's current old ID, so that document has to move out of the
    way first.

    Args:
        mapping: Old -> new document IDs.

    Returns:
        Old IDs in a safe order, or ``None`` when the constraints form a
        cycle (two documents trading identities) and no order exists.
    """
    old_ids = set(mapping)
    # ``blockers[a]`` holds the documents that must move before ``a`` does.
    blockers: dict[str, set[str]] = {old: set() for old in mapping}
    for old, new in mapping.items():
        if new in old_ids and new != old:
            blockers[old].add(new)

    ordered: list[str] = []
    remaining = dict(blockers)
    while remaining:
        ready = sorted(old for old, deps in remaining.items() if not deps)
        if not ready:
            return None
        for old in ready:
            ordered.append(old)
            del remaining[old]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def _backup_db(db_path: Path) -> Path:
    """Copy the index to a timestamped sibling and return its path.

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        Path to the backup copy.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = db_path.parent / f"{db_path.name}.pre-doc-id-migration-{stamp}"
    db.vacuum_into(db_path, backup)
    return backup


@workflow(
    purpose="Migrate every document and section doc id in a project, gating on collisions and backing the index up before any write",
    inputs="db_path, project root, optional precomputed plan",
    outputs="DocIdMigrationResult — counts, backup path, and abort detail",
)
def execute_doc_id_migration(
    db_path: Path,
    root: Path,
    *,
    plan: DocIdMigrationPlan | None = None,
) -> DocIdMigrationResult:
    """Migrate every doc and section identity in *root*.  Irreversible.

    The collision gate is re-run immediately before writing, so a tree that
    grew a duplicate since the preview is refused rather than half-migrated.
    A timestamped copy of the database is taken before the first write and
    reported back; the whole batch then runs in one transaction, and any
    failure rolls it back and restores the copy rather than leaving a
    partially-migrated index.

    What survives: the rename ledger, node history, verification for
    documents *and* sections, graph edges, and ``links[].node_id``
    references on disk.  What does not: prose references to doc IDs (they
    are reported by :func:`plan_doc_id_migration`, never rewritten), and
    reversibility -- there is no per-document revert path.

    A rebuild is deliberately not run afterwards.  Under a derivation that
    still produces the old IDs, a rebuild re-creates the identities this
    call retired.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Absolute project root.
        plan: A previously computed plan.  Ignored for gating -- the gate
            always re-runs -- and accepted only to avoid recomputing the
            prose scan for the report.

    Returns:
        A :class:`DocIdMigrationResult`.
    """
    from axiom_graph.index.link_maintenance import patch_doc_links_batch  # noqa: PLC0415

    root = Path(root).resolve()

    口 = Step(
        step_num=1,
        name="Re-plan and gate",
        purpose="Recompute the projection from disk and refuse when any two projected ids collide",
        critical="Execute mode is unreachable while a collision exists — the gate is re-run here, not trusted from the preview",
    )
    fresh = plan_doc_id_migration(db_path, root)
    if fresh.blocked:
        return DocIdMigrationResult(executed=False, reason="collision", plan=fresh)
    mapping = fresh.as_mapping()
    if not mapping:
        return DocIdMigrationResult(executed=False, reason="no_documents", plan=fresh)
    order = _order_doc_renames(mapping)
    if order is None:
        return DocIdMigrationResult(executed=False, reason="rename_cycle", plan=fresh)

    口 = Step(
        step_num=2,
        name="Back the index up",
        purpose="Take a timestamped copy of the database before the first write and report where it went",
    )
    backup_path = _backup_db(db_path)

    口 = Step(
        step_num=3,
        name="Migrate every identity in one transaction",
        purpose="Materialise the new rows, move history/verification/edges, retire the old rows; abort the whole run on the first failure",
    )
    file_paths = {m.old_id: m.file_path for m in fresh.documents}
    documents_migrated = 0
    sections_migrated = 0
    old_id = order[0]
    try:
        with db._connect(db_path) as conn:
            for old_id in order:
                口 = Step(
                    step_num=3.1,
                    name="Migrate one document identity",
                    purpose="Clone the new rows, move history/verification/edges onto them, retire the old rows",
                )
                new_id = mapping[old_id]
                file_path = file_paths.get(old_id, "")
                sections_migrated += db.rekey_doc_identity(conn, old_id, new_id, file_path).sections
                db.record_doc_rename_conn(conn, old_id, new_id, file_path)
                db.delete_doc_by_id(
                    conn,
                    old_id,
                    reason_meta={"actor": "doc-id-migration", "reason": f"renamed to {new_id}"},
                )
                documents_migrated += 1
    except Exception as exc:
        logger.error("doc-id migration aborted at %s: %s", old_id, exc)
        restored = False
        try:
            shutil.copy2(backup_path, db_path)
            restored = True
        except OSError:  # pragma: no cover - restore failure
            logger.error("could not restore %s from %s", db_path, backup_path)
        return DocIdMigrationResult(
            executed=False,
            reason="aborted",
            backup_path=backup_path,
            aborted_at=old_id,
            error=str(exc),
            restored_from_backup=restored,
            plan=fresh,
        )

    口 = Step(
        step_num=4,
        name="Rewrite on-disk links in a single pass",
        purpose="Apply the whole map to every DocJSON links[].node_id in one walk of the doc tree",
    )
    files_patched, files_read = patch_doc_links_batch(root, mapping)

    口 = Step(
        step_num=5,
        name="Report",
        purpose="Return counts, the backup path, and the plan the run gated on",
    )
    return DocIdMigrationResult(
        executed=True,
        backup_path=backup_path,
        documents_migrated=documents_migrated,
        sections_migrated=sections_migrated,
        files_patched=files_patched,
        doc_files_read=files_read,
        plan=fresh,
    )
