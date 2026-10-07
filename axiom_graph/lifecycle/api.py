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
    ``anchor_file_on_disk``     -- is a file-level node's file still on disk
    ``file_unparseable``        -- is a node's file on disk but not parsing
    ``select_purgeable_not_found`` -- split NOT_FOUND nodes into purge / keep
    ``fetch_history``           -- node history rows + total count
    ``list_reference_points``   -- list available SHAs/checkpoints
    ``compute_report``          -- impact report since a reference point
    ``render_report_text``      -- shared summary / condensed / full report text
    ``checkout_db``             -- VACUUM INTO copy of the index DB
    ``carry_forward_verifications`` -- copy a merged worktree's verifications back
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

import ast
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from axiom_annotations import AutoStep, Step, task, workflow

from axiom_graph.config import AxiomGraphConfig, db_path_for
from axiom_graph.index import builder, db
from axiom_graph.index.doc_stamps import RAW_DOCJSON_EDIT, adopt_drifted_stamps
from axiom_graph.index.git_utils import _run_git, read_file_at_baseline
from axiom_graph.index.link_maintenance import LinkPatchResult, link_rewrite_warnings
from axiom_graph.index.mark_clean import (
    VERIFICATION_OP_MARK_CLEAN,
    VERIFICATION_OP_REVERIFY,
    VERIFIED_BY_SCAN_BASELINE,
    VERIFIED_BY_TOOL_STAMP,
)
from axiom_graph.scanners.node_hashing import scan_blob_at_location
from axiom_graph.scanners.source_roots import resolve_source_roots
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
        files_skipped_mtime: Code files the build left unparsed because their
            content and mtime both match the last parse (a file with no
            parse record yet: because its mtime matches its scan mtime).
            The name predates the content check.
        docs_skipped_mtime: Markdown and DocJSON files skipped by the same
            rule.  Counted apart from ``files_skipped_mtime`` because doc
            files are walked by their own scanners; without it there is no
            observable evidence that a doc file was left unread.
        tests_baselined: Test functions this build indexed for the first
            time and baseline-verified (op ``scan_baseline``).
        stamp_verified: DocJSON sections verified in full because they carry
            a tool-write stamp that matches their text and every linked code
            node (op ``tool_stamp``).
        stamp_text_verified: DocJSON sections whose tool-write stamp matches
            their text but only some of their linked code: the text and those
            links were verified, every other link keeps its status.
        raw_docjson_edits: DocJSON sections this build recorded as edited
            outside the doc tools.  ``warnings`` carries the one summary.
        staleness_total: Nodes in the index after the build.
        staleness_stale: Nodes not VERIFIED in either dimension, counted
            over the same statuses as ``check`` (frozen-doc sections
            excluded, LINKED_STALE included).
        check: The staleness counts the build stored, counted the way
            :func:`compute_check_summary` counts them (frozen-doc sections
            excluded, LINKED_STALE included), so a build reports the numbers
            ``check`` would.  ``None`` when the index has no nodes.
        annotation_findings: Every current annotation finding after the
            ``[validation]`` config, each with a ``new`` flag (not in the
            findings store before this build).
        annotation_findings_new: How many of them are new.
        annotation_findings_resolved: How many stored findings are gone.
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
    check: CheckSummary | None = None
    annotation_findings: list = field(default_factory=list)
    docs_skipped_mtime: int = 0
    tests_baselined: list[str] = field(default_factory=list)
    stamp_verified: list[str] = field(default_factory=list)
    stamp_text_verified: list[str] = field(default_factory=list)
    raw_docjson_edits: list[str] = field(default_factory=list)
    annotation_findings_new: int = 0
    annotation_findings_resolved: int = 0


@dataclass
class AnnotationFindingsResult:
    """Result of :func:`read_annotation_findings`.

    Attributes:
        findings: Every current annotation finding after the
            ``[validation]`` config, sorted by file and line, in the
            ``rule_id/severity/module/function/line/message`` shape plus
            ``new: bool`` (not in the findings store before this run).
        new: How many findings are new.
        resolved: How many stored findings are gone.
        files_rescanned: Files edited since the last build, rescanned in
            memory for this read.
    """

    findings: list[dict] = field(default_factory=list)
    new: int = 0
    resolved: int = 0
    files_rescanned: int = 0


@dataclass
class CheckSummary:
    """Result of :func:`compute_check_summary`.

    The counts come from aggregate queries over the stored statuses.  The
    per-node views are read on first access, so a caller that prints only
    the summary line never loads every node:

    - ``statuses``: every node (frozen-doc sections filtered as the counts
      are), ``{node_id: (own, link, via)}``, in node-table order;
    - ``problem_statuses``: only the nodes not VERIFIED in a dimension;
    - ``ordered_ids``: every node id, in node-table order (``check --all``).

    Attributes:
        own_counts: Own-status counts.
        link_counts: Link-status counts.
        clean_count: Nodes VERIFIED in both dimensions.
        doc_quality_count: DOC_SECTION_LONG advisories.
        all_clean: Every counted node is VERIFIED in both dimensions.
        own_present: The own statuses that occur among the counted nodes.
        link_present: The link statuses that occur among the counted nodes.
        structure: Files whose last re-hash found structure the index lacks:
            location -> ``{"new": [...], "missing": [...]}``.
        refresh: What the refresh behind this summary did (``None`` when
            the statuses came from elsewhere, e.g. a build's pass).
    """

    own_counts: dict[str, int]
    link_counts: dict[str, int]
    clean_count: int
    doc_quality_count: int
    all_clean: bool
    own_present: set[str] = field(default_factory=set)
    link_present: set[str] = field(default_factory=set)
    structure: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    refresh: object | None = field(default=None, repr=False)
    _statuses: dict[str, tuple[str, str, list[str]]] | None = field(default=None, repr=False)
    _problems: dict[str, tuple[str, str, list[str]]] | None = field(default=None, repr=False)
    _ordered_ids: list[str] | None = field(default=None, repr=False)
    _loader: Callable[[bool], tuple[list[str], dict[str, tuple[str, str, list[str]]]]] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def statuses(self) -> dict[str, tuple[str, str, list[str]]]:
        """Every counted node's ``(own, link, via)``, read on first access."""
        if self._statuses is None:
            ids, statuses = self._loader(False) if self._loader is not None else ([], {})
            self._ordered_ids, self._statuses = ids, statuses
        return self._statuses

    @property
    def problem_statuses(self) -> dict[str, tuple[str, str, list[str]]]:
        """The counted nodes not VERIFIED in a dimension, read on first access."""
        if self._problems is None:
            if self._statuses is not None:
                self._problems = {
                    nid: trip for nid, trip in self._statuses.items() if trip[0] != VERIFIED or trip[1] != VERIFIED
                }
            else:
                self._problems = self._loader(True)[1] if self._loader is not None else {}
        return self._problems

    @property
    def ordered_ids(self) -> list[str]:
        """Every node id in node-table order (frozen-doc sections included)."""
        if self._ordered_ids is None:
            if self._loader is not None and self._statuses is None:
                _ = self.statuses  # the loader returns the order with the statuses
            else:
                self._ordered_ids = list(self.statuses)
        return self._ordered_ids or []

    def summary_line(self) -> str:
        """Return the one-line count summary ``check`` and ``build`` print.

        Returns:
            ``own: N CONTENT_UPDATED / N DESC_UPDATED / N RENAMED /
            N NOT_FOUND · link: N LINKED_STALE / N BROKEN_LINK · N VERIFIED``.
        """
        own, link = self.own_counts, self.link_counts
        return (
            f"own: {own.get(CONTENT_UPDATED, 0)} CONTENT_UPDATED / "
            f"{own.get(DESC_UPDATED, 0)} DESC_UPDATED / "
            f"{own.get(RENAMED, 0)} RENAMED / "
            f"{own.get(NOT_FOUND, 0)} NOT_FOUND · "
            f"link: {link.get(LINKED_STALE, 0)} LINKED_STALE / "
            f"{link.get(BROKEN_LINK, 0)} BROKEN_LINK · "
            f"{self.clean_count} VERIFIED"
        )


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
    - ``cleared``: nodes this call verified whose stored LINKED_STALE
      cleared: the source and cascade members that were LINKED_STALE
      after the entry refresh and are not after the closing recompute.
    - ``own_change_kept``: cascade members whose own content or docstring
      had changed and was not reviewed: only their links were verified
      (a link-only verification), so their own status stays CONTENT_UPDATED
      / DESC_UPDATED.  They are in ``verified`` and, once their link clears,
      in ``cleared``.
    - ``settled``: other nodes (frozen docs excluded) whose LINKED_STALE
      left in the closing recompute, e.g. aggregates that settled by
      composite inheritance.  Never listed as cleared.
    - ``skipped``: nodes attributed partly to the source but still
      outstanding via other root offenders — left LINKED_STALE, mapped
      to those offender IDs.  Offenders already reverified (themselves
      or through a ``composes`` ancestor) are not listed; the map shrinks
      as the composition is completed.
    - ``marked_clean_offenders``: the offenders named in ``skipped`` that
      were verified since their last change by an operation other than
      reverify (e.g. ``mark_clean``) — the reason they still block.
      Informational only; it never changes what is cleared or skipped.
    - ``before_linked_stale`` / ``after_linked_stale``: the LINKED_STALE
      counts ``check`` shows (frozen docs excluded, their envelopes too)
      after the entry refresh and after the closing recompute.
    """

    source_id: str
    not_found: bool = False
    verified: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    own_change_kept: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    marked_clean_offenders: list[str] = field(default_factory=list)
    before_linked_stale: int = 0
    after_linked_stale: int = 0


@dataclass
class ReverifyBatchResult:
    """Result of :func:`reverify_nodes` — one report for a batch of sources.

    - ``sources``: the requested sources that exist and were verified, in
      request order (duplicates dropped).
    - ``not_found``: requested IDs with no node — reported, not fatal.
    - ``verified``: nodes that received verification rows in this call
      (the sources first, then the cascade-cleared dependents).
    - ``cleared`` / ``own_change_kept`` / ``settled`` / ``skipped`` /
      ``marked_clean_offenders`` / ``before_linked_stale`` /
      ``after_linked_stale``: as on :class:`ReverifyResult`, computed once
      for the whole batch.  A skipped dependent's offenders are only those
      outside the batch.
    """

    sources: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    own_change_kept: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    marked_clean_offenders: list[str] = field(default_factory=list)
    before_linked_stale: int = 0
    after_linked_stale: int = 0


#: One-line action a presentation surface appends to reverify's skip
#: block.  Defined here so every surface renders the same string; the
#: layout of the block stays with the surface.  It names the action and
#: deliberately does not repeat the offender IDs printed above it.
REVERIFY_SKIP_HINT = (
    "Reverify each offender listed above — a dependent clears once every offender behind it is reverified."
)

#: Per-offender note for a skipped dependent's offender that was marked
#: clean but not reverified.  Format with ``offender=<node id>``.
REVERIFY_MARKED_CLEAN_NOTE = (
    "{offender} was marked clean, not reverified — mark_clean does not settle an offender "
    "for its dependents; reverify {offender} to clear them."
)

#: One line standing in for several :data:`REVERIFY_MARKED_CLEAN_NOTE`
#: lines in a compact report.  Format with ``count=<int>`` and
#: ``offenders=<rendered id list>``.
REVERIFY_MARKED_CLEAN_COMBINED_NOTE = (
    "{count} offenders named above were marked clean, not reverified — mark_clean does not settle an "
    "offender for its dependents; reverify them to clear their dependents: {offenders}."
)

#: Line a surface prints when nothing cleared but dependents rooted at the
#: source were skipped (so "nothing rooted here" would be false).
REVERIFY_NOTHING_CLEARED_SKIPPED = (
    "Nothing cleared — every dependent rooted at this node is also stale via another offender (see Skipped)."
)

#: Line a surface prints when nothing cleared and nothing was skipped.
REVERIFY_NOTHING_TO_CLEAR = "Nothing to clear — no LINKED_STALE rooted at this node."

#: Batch counterparts of the two "nothing cleared" lines above.
REVERIFY_BATCH_NOTHING_CLEARED_SKIPPED = (
    "Nothing cleared — every dependent rooted at these sources is also stale via an offender "
    "outside the batch (see Skipped)."
)
REVERIFY_BATCH_NOTHING_TO_CLEAR = "Nothing to clear — no LINKED_STALE rooted at these sources."


#: :attr:`PurgeResult.reason` for a file-level node refused because its file is
#: on disk and its NOT_FOUND is inherited from NOT_FOUND nodes in that file.
PURGE_REFUSED_INHERITED = "inherited_not_found"

#: Line a surface prints for a :data:`PURGE_REFUSED_INHERITED` refusal.
PURGE_INHERITED_HINT = (
    "its file is still on disk, so its NOT_FOUND is inherited from the NOT_FOUND nodes in that file; "
    "purge those instead if they were really removed, and it clears on the next check"
)

#: :attr:`PurgeResult.reason` for a node refused because its file is on disk
#: but does not parse (see :func:`file_unparseable`): its NOT_FOUND says the
#: file could not be read, not that the node is gone.
PURGE_REFUSED_UNPARSEABLE = "file_unparseable"

#: Line a surface prints for a :data:`PURGE_REFUSED_UNPARSEABLE` refusal.
PURGE_UNPARSEABLE_HINT = (
    "its file is on disk but does not parse, so its nodes read NOT_FOUND without being gone; "
    "fix the file and re-run check, and do not purge its nodes"
)


@dataclass
class PurgeResult:
    """Result of :func:`purge_nodes` for a single node."""

    node_id: str
    purged: bool
    reason: str | None = None  # error reason, when purged is False
    #: For a :data:`PURGE_REFUSED_INHERITED` refusal: the NOT_FOUND nodes in
    #: the same file, which are the ones to purge if they were really
    #: removed.  Empty for every other result.
    deleted_children: list[str] = field(default_factory=list)


#: Per-operation cache of :func:`file_unparseable` verdicts, keyed by
#: ``(file path, is a document node)``, so each file is parsed once.
ParseVerdicts = dict[tuple[str, bool], bool]


@dataclass
class NotFoundSelection:
    """The index's NOT_FOUND nodes split for ``purge --all-not-found``.

    Built by :func:`select_purgeable_not_found`.
    """

    #: Nodes to purge, sorted by id: a parent sorts before its children, so a
    #: doc is purged before its sections.
    to_purge: list[str] = field(default_factory=list)
    #: File-level nodes whose file is on disk and parses: their NOT_FOUND is
    #: inherited from removed children, so they are kept.  Sorted by id.
    inherited: list[str] = field(default_factory=list)
    #: Each file on disk that does not parse, mapped to its NOT_FOUND node
    #: ids (sorted): all of them are kept.
    unparseable: dict[str, list[str]] = field(default_factory=dict)
    #: The parse verdicts the selection made.  Pass them to
    #: :func:`purge_nodes` so the purge does not parse the files again.
    parse_verdicts: ParseVerdicts = field(default_factory=dict)


@dataclass
class RenameApplyResult:
    """Result of :func:`apply_rename`."""

    applied: bool
    old_id: str
    new_id: str
    reason: str | None = None  # refusal reason when applied is False
    #: DocJSON files whose links still name ``old_id``: their write locks
    #: were busy when the rename rewrote links.
    links_not_patched: list[str] = field(default_factory=list)
    #: DocJSON files the link rewrite could not read or parse, so could not
    #: check for links to ``old_id`` (any unparseable doc file in the tree).
    links_unreadable: list[str] = field(default_factory=list)


@dataclass
class RenameRevertResult:
    """Result of :func:`revert_rename`."""

    reverted: bool
    new_id: str
    old_id: str | None = None
    reason: str | None = None  # refusal reason when reverted is False
    #: DocJSON files whose links still name ``new_id``: their write locks
    #: were busy when the revert rewrote links.
    links_not_patched: list[str] = field(default_factory=list)
    #: DocJSON files the link rewrite could not read or parse, so could not
    #: check for links to ``new_id`` (any unparseable doc file in the tree).
    links_unreadable: list[str] = field(default_factory=list)


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
    ``resolution`` records what the report was measured against, so
    every format can print its reference header (also on empty windows).
    """

    summary: dict[str, int]
    content_changes: dict[str, list[dict]]
    staleness_transitions: list[dict]
    link_changes: list[dict]
    verifications: list[dict]
    human_verified_ids: set[str]
    raw_docjson_edits: list[dict] = field(default_factory=list)
    no_rows: bool = False
    no_matches: bool = False
    resolution: db.Resolution | None = None


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
# build
# ---------------------------------------------------------------------------


def build_index(
    db_path: Path,
    root: Path,
    *,
    project_id: str | None = None,
    discovery_only: bool = True,
    verbose: bool = False,
) -> BuildSummary:
    """Run an axiom-graph build and compute persistent staleness.

    Shared orchestration for the CLI ``axiom-graph build`` command and
    the ``axiom_graph_build`` MCP tool.  Returns a typed
    :class:`BuildSummary`; presentation layers format it.

    After the builder returns, staleness is brought up to date by the
    scoped refresh ``check`` uses
    (:func:`~axiom_graph.index.refresh.refresh_staleness`), seeded with
    what the build changed: the nodes at the files it parsed (re-hashed
    whatever their fingerprint says), both ends of every edge those files
    emitted or lost, and the new, renamed, deleted and build-verified ids,
    widened to their neighbourhood; a full rescan (``discovery_only`` off)
    runs the full pass.  Between the own write and the link phase the
    verifications this build wrote are re-stamped so they postdate the
    code changes the refresh recorded, so the ``LINKED_STALE`` a change
    causes on linked doc sections and tests is stored in the same run.
    The returned counts are read from the stored statuses with the rule
    ``check`` applies.

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

    Returns:
        :class:`BuildSummary` with file/node/edge counts, warnings, and
        staleness counters.
    """

    # The whole build runs on one connection (every ``_connect`` block the
    # builder and the refresh open reuses it and still commits at its own
    # end), when the index already exists and is the one the builder writes.
    # A first build creates the DB inside the builder, so it opens its own.
    def _key(path: Path) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    if Path(db_path).exists() and _key(db_path) == _key(db_path_for(Path(root).resolve())):
        with db.operation_connection(db_path):
            return _build_index(db_path, root, project_id=project_id, discovery_only=discovery_only)
    return _build_index(db_path, root, project_id=project_id, discovery_only=discovery_only)


def _build_index(db_path: Path, root: Path, *, project_id: str | None, discovery_only: bool) -> BuildSummary:
    """The body of :func:`build_index` (the builder, the refresh and the counts)."""
    summary = builder.build(
        root,
        project_id=project_id,
        discovery_only=discovery_only,
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
        tests_baselined=list(summary.get("scan_baselined_ids") or []),
        stamp_verified=list(summary.get("stamp_verified_ids") or []),
        stamp_text_verified=list(summary.get("stamp_text_verified_ids") or []),
        raw_docjson_edits=list(summary.get("raw_docjson_edit_ids") or []),
        annotation_findings_new=summary.get("annotation_findings_new", 0) or 0,
        annotation_findings_resolved=summary.get("annotation_findings_resolved", 0) or 0,
    )

    # Compute, record transition events, and persist staleness: the scoped
    # refresh over what this build changed (the full pass for a full rescan).
    if db_path.exists():
        from axiom_graph.index.refresh import refresh_staleness  # noqa: PLC0415

        config = AxiomGraphConfig.load(root)
        restamped = False

        def _restamp_build_verifications() -> None:
            nonlocal restamped
            restamped = True
            # Second touch of the verifications this build wrote.  Staleness
            # records this build's code changes only in the first pass, after
            # the builder returned, so those rows postdate the builder's
            # verifications.  Re-stamping makes the build's own verifications
            # newer than every change it observed -- before the second pass,
            # so that pass does not count them stale.  Keyed by the ids this
            # build returned and limited to rows still carrying the build's
            # provenance -- never selected by provenance alone, which would
            # re-stamp every earlier build's rows and silently clear later
            # code changes.
            db.restamp_verifications(db_path, list(summary.get("scan_baselined_ids") or []), VERIFIED_BY_SCAN_BASELINE)
            db.restamp_verifications(db_path, list(summary.get("stamp_verified_ids") or []), VERIFIED_BY_TOOL_STAMP)

        with db._connect(db_path) as conn:
            node_count = db.count_nodes_conn(conn)
        refreshed = None
        if node_count:
            renamed = set(summary.get("renamed_new_ids") or [])
            # Seeds: the nodes at the files the build parsed (re-hashed whatever
            # their fingerprint says), both ends of every edge those files
            # emitted or lost, and the new, renamed, deleted and build-verified
            # ids; the walk adds every other file whose bytes moved.
            seeds = (
                set(summary.get("staleness_seed_ids") or [])
                | set(summary.get("deleted_ids") or [])
                | renamed
                | set(summary.get("scan_baselined_ids") or [])
                | set(summary.get("stamp_verified_ids") or [])
                # Never re-stamped: their verified_at must not move.
                | set(summary.get("stamp_text_verified_ids") or [])
            )
            refreshed = refresh_staleness(
                db_path,
                root,
                transitive_tags=config.staleness.transitive_tags,
                frozen_tags=config.staleness.frozen_tags,
                full=not discovery_only,
                seed_node_ids=seeds,
                seed_locations=summary.get("changed_locations") or (),
                renamed_ids=renamed,
                between_passes=_restamp_build_verifications,
                walked=summary.get("discovery_observed"),
            )
        if not restamped:
            # No pass ran (nothing in scope): the verifications are re-stamped
            # all the same.
            _restamp_build_verifications()
        if node_count:
            # Counted from the stored statuses, the rule check applies.
            result.staleness_total = node_count
            result.check = _summary_from_store(db_path, root, config, include_frozen=False, refresh=refreshed)
            result.staleness_stale = sum(result.check.link_counts.values()) - result.check.clean_count

    return result


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@workflow(
    purpose="Bring the stored statuses up to date and count them: adopt valid tool-write stamps on own-drifted doc "
    "sections, then the incremental refresh (discovery walk, journal past the watermark, scoped pass), or the full "
    "pass on request, with no watermark or on a scheme-stamp mismatch; the summary comes from aggregate queries "
    "(frozen docs, envelopes included, left out) and per-node rows load only when a caller reads them",
    inputs="db_path, project root, include_frozen, full",
    outputs="CheckSummary (counts, present statuses, structural changes, lazy per-node views), or None when empty",
)
@db.connection_scope
def compute_check_summary(
    db_path: Path,
    root: Path,
    include_frozen: bool = False,
    *,
    full: bool = False,
) -> CheckSummary | None:
    """Refresh the stored statuses and compute the data behind the one-line summary.

    Shared by CLI ``axiom-graph check`` and MCP ``axiom_graph_check``.
    Returns ``None`` when the index has no nodes (callers print
    ``(no nodes in index)``).  First, DocJSON sections stored own-drifted
    whose tool-write stamp is valid are adopted
    (:func:`~axiom_graph.index.doc_stamps.adopt_drifted_stamps`), so a
    merged tool write an earlier build indexed without adopting reads
    VERIFIED in this same check; a hand edit fails its stamp and stays
    flagged.  The statuses are then brought up to date by
    :func:`~axiom_graph.index.refresh.refresh_staleness`, seeded with the
    adopted sections: incrementally
    (only the files whose bytes moved and the nodes the journal names
    since the last check, widened to their neighbourhood), or in full under
    *full*, on the first check of an index (no watermark) and after an
    upgrade that changed the hashing scheme, the staleness rules or the
    staleness config.  Either way the stored values are what a full
    recompute stores, and a change this check detects first is
    linked-stale in the statuses it stores and counts.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        include_frozen: When ``False`` (the default), the rows of a doc
            carrying any tag listed in ``config.staleness.frozen_tags``,
            its sections and the doc's own node (the envelope) alike, are
            filtered out of the summary (their own-status and
            LINKED_STALE / VERIFIED counts), EXCEPT a row whose link
            status is BROKEN_LINK: it is kept in ``statuses`` and counted
            under BROKEN_LINK only (its own status is not counted), the
            same rule ``drift_query`` applies.  When ``True`` they
            participate in counts unchanged.  No-op when ``frozen_tags``
            is empty.
        full: Recompute every node from a re-hash of every file
            (``check --full``).  The stamp adoption runs either way.

    Returns:
        :class:`CheckSummary` (or ``None`` when empty).
    """
    from axiom_graph.index.refresh import refresh_staleness  # noqa: PLC0415

    口 = Step(step_num=1, name="Empty index", purpose="Return None when the index holds no node")
    with db._connect(db_path) as conn:
        if not db.count_nodes_conn(conn):
            return None

    config = AxiomGraphConfig.load(root)
    口 = AutoStep(step_num=2, name="Adopt valid stamps on drifted doc sections")
    adopted = adopt_drifted_stamps(db_path, root)

    def _restamp_adopted() -> None:
        # As the build does: the refresh records the code changes it finds
        # after the adoption wrote its verifications, so the full ones are
        # re-stamped between the passes.  Text-only ones keep their time.
        db.restamp_verifications(db_path, adopted.verified, VERIFIED_BY_TOOL_STAMP)

    口 = AutoStep(step_num=3, name="Refresh the stored statuses")
    refreshed = refresh_staleness(
        db_path,
        root,
        transitive_tags=config.staleness.transitive_tags,
        frozen_tags=config.staleness.frozen_tags,
        full=full,
        seed_node_ids=[*adopted.verified, *adopted.text_verified],
        between_passes=_restamp_adopted if adopted.verified else None,
    )

    口 = Step(
        step_num=4,
        name="Count the stored statuses",
        purpose="Aggregate queries over the stored own and link statuses with the frozen-doc rule; per-node rows are "
        "left to the summary's lazy views",
    )
    return _summary_from_store(db_path, root, config, include_frozen=include_frozen, refresh=refreshed)


def _summary_from_store(
    db_path: Path,
    root: Path,
    config: AxiomGraphConfig,
    *,
    include_frozen: bool,
    refresh=None,
) -> CheckSummary:
    """Count the stored statuses into a :class:`CheckSummary`, the rule :func:`_summarize_statuses` applies.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        config: The project config (frozen and transitive tags).
        include_frozen: Count frozen-doc nodes (sections and envelopes) too.
        refresh: The refresh behind the stored statuses, if any.

    Returns:
        The summary, with lazy per-node views.
    """
    frozen_rows = {} if include_frozen else db.get_frozen_rows(db_path, config.staleness.frozen_tags)
    with db._connect(db_path) as conn:
        pairs = db.count_status_pairs_conn(conn)
    frozen = set(frozen_rows)
    frozen_broken = 0
    frozen_broken_own: set[str] = set()
    for own, link in frozen_rows.values():
        pairs[(own, link)] = pairs.get((own, link), 0) - 1
        if link == BROKEN_LINK:
            frozen_broken += 1
            frozen_broken_own.add(own)

    own_counts: dict[str, int] = {CONTENT_UPDATED: 0, DESC_UPDATED: 0, RENAMED: 0, NOT_FOUND: 0, VERIFIED: 0}
    link_counts: dict[str, int] = {LINKED_STALE: 0, BROKEN_LINK: 0, VERIFIED: 0}
    own_present: set[str] = set(frozen_broken_own)
    link_present: set[str] = {BROKEN_LINK} if frozen_broken else set()
    for (own, link), n in pairs.items():
        if n <= 0:
            continue
        own_counts[own] = own_counts.get(own, 0) + n
        link_counts[link] = link_counts.get(link, 0) + n
        own_present.add(own)
        link_present.add(link)
    link_counts[BROKEN_LINK] += frozen_broken
    clean_count = max(pairs.get((VERIFIED, VERIFIED), 0), 0)
    all_clean = own_present <= {VERIFIED} and link_present <= {VERIFIED}

    def _load(problems_only: bool) -> tuple[list[str], dict[str, tuple[str, str, list[str]]]]:
        from axiom_graph.index.staleness import linked_stale_vias  # noqa: PLC0415

        with db._connect(db_path) as conn:
            rows = db.get_ordered_staleness_conn(conn, problems_only=problems_only)
        ordered = [nid for nid, _own, _link in rows]
        kept = [(nid, own, link) for nid, own, link in rows if nid not in frozen or link == BROKEN_LINK]
        vias = linked_stale_vias(
            db_path,
            root,
            [nid for nid, _own, link in kept if link in (LINKED_STALE, BROKEN_LINK)],
            transitive_tags=config.staleness.transitive_tags,
            frozen_tags=config.staleness.frozen_tags,
        )
        return ordered, {nid: (own, link, vias.get(nid, [])) for nid, own, link in kept}

    return CheckSummary(
        own_counts=own_counts,
        link_counts=link_counts,
        clean_count=clean_count,
        doc_quality_count=len(db.get_long_sections(db_path)),
        all_clean=all_clean,
        own_present=own_present,
        link_present=link_present,
        structure=dict(getattr(refresh, "structure", {}) or {}),
        refresh=refresh,
        _loader=_load,
    )


def structure_lines(structure: dict[str, dict[str, list[str]]]) -> list[str]:
    """Render the structural changes a refresh found, one line per file.

    A file whose last re-hash found functions or sections the index lacks,
    or lost indexed functions, needs a ``build`` to bring the index's
    structure up to date; staleness alone never adds or removes a node.

    Args:
        structure: Location -> ``{"new": [...], "missing": [...]}``.

    Returns:
        Lines such as ``utils.py has 2 new functions — run build``, sorted by file.
    """
    lines: list[str] = []
    for loc in sorted(structure):
        entry = structure[loc] or {}
        noun = "section" if loc.endswith((".docjson", ".json", ".md")) else "function"
        parts: list[str] = []
        new = len(entry.get("new") or [])
        missing = len(entry.get("missing") or [])
        if new:
            parts.append(f"{new} new {noun}{'' if new == 1 else 's'}")
        if missing:
            parts.append(f"{missing} indexed {noun}{'' if missing == 1 else 's'} no longer found")
        if parts:
            lines.append(f"{loc} has {' and '.join(parts)} — run build")
    return lines


# ---------------------------------------------------------------------------
# read tools: refresh_before_read
# ---------------------------------------------------------------------------


def behind_line(behind: int) -> str:
    """Return the ``"off"`` warning for *behind* files.

    Args:
        behind: Tracked files whose stat moved since the index last read them.

    Returns:
        ``index is behind for N files — run `check```.
    """
    return f"index is behind for {behind} file{'' if behind == 1 else 's'} — run `check`"


@dataclass
class ReadRefresh:
    """What a read did to the stored statuses before it answered (:func:`refresh_before_read`).

    Attributes:
        mode: The ``refresh_before_read`` mode that ran.
        statuses: ``{node_id: (own, link)}`` as stored after the refresh, for
            the nodes a node-naming read names (empty for a whole-repo read).
        behind: Under ``"off"``, the tracked files whose stat moved since the
            index last read them.
        structure: Files whose last re-hash found structure the index lacks:
            every such file for a whole-repo read, the named nodes' files for
            a node-naming read.
        refresh: The :class:`~axiom_graph.index.refresh.RefreshResult`, when
            a refresh ran.
    """

    mode: str
    statuses: dict[str, tuple[str, str]] = field(default_factory=dict)
    behind: int = 0
    structure: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    refresh: object | None = field(default=None, repr=False)

    def flags(self, node_id: str) -> list[str]:
        """Return the node's statuses that are not VERIFIED, own first."""
        own, link = self.statuses.get(node_id, (VERIFIED, VERIFIED))
        return [st for st in (own, link) if st and st != VERIFIED]

    def tag(self, node_id: str) -> str:
        """Return ``"  [STATUS, ...]"`` for a node not VERIFIED, else ``""`` (the outline's format)."""
        flags = self.flags(node_id)
        return f"  [{', '.join(flags)}]" if flags else ""

    def tags(self) -> dict[str, str]:
        """Return :meth:`tag` for every named node that has one."""
        return {nid: self.tag(nid) for nid in self.statuses if self.flags(nid)}

    def notes(self) -> list[str]:
        """Return the lines a read appends: the behind warning, then the structural lines, each in brackets."""
        lines = [behind_line(self.behind)] if self.behind else []
        lines.extend(structure_lines(self.structure))
        return [f"[{line}]" for line in lines]

    def narrowed(self, node_ids: Iterable[str], locations: Iterable[str]) -> ReadRefresh:
        """Return the view one read of a batch shows: this refresh, cut down to the nodes that read names.

        A batch read refreshes the union of every entry's nodes once; each
        entry is then rendered from its own narrowed view, so its text is the
        text a single read of that entry would give.

        Args:
            node_ids: The nodes the entry names.
            locations: Their files, as stored on the nodes.

        Returns:
            A :class:`ReadRefresh` with the same mode, behind count and refresh,
            the named nodes' statuses, and the structural changes in their files.
        """
        keep = set(locations)
        return ReadRefresh(
            mode=self.mode,
            statuses={nid: self.statuses[nid] for nid in dict.fromkeys(node_ids) if nid in self.statuses},
            behind=self.behind,
            structure={loc: s for loc, s in self.structure.items() if loc in keep},
            refresh=self.refresh,
        )


@workflow(
    purpose="Bring the statuses a read shows up to date the way the configured refresh_before_read mode says: the "
    "cone of the named nodes (a node-naming read) or the incremental check (a whole-repo read) under changed-files, "
    "a check first under check, nothing under off but a stat probe that counts the files the index is behind",
    inputs="db_path, project root, the node ids the read names (None for a whole-repo read), an optional mode",
    outputs="ReadRefresh: the mode, the named nodes' stored statuses, the behind count, structural changes",
)
@db.connection_scope
def refresh_before_read(
    db_path: Path,
    root: Path,
    node_ids: Iterable[str] | None = None,
    *,
    mode: str | None = None,
) -> ReadRefresh:
    """Refresh what a read shows, then return the statuses it shows (one connection).

    A node-naming read (``read_doc``, ``graph``, ``search``, ``source``, a
    viz neighbourhood) passes the ids it names: under ``"changed-files"``
    only the files those statuses read are looked at, with the journal rows
    naming them, and the journal watermark does not move.  A whole-repo
    read (``drift_query``, viz) passes ``None`` and gets the incremental
    check.  ``"check"`` runs the check's refresh whatever the read;
    ``"off"`` refreshes nothing and counts, by stat alone, the tracked files
    that moved since the index last read them.  Below schema v5 nothing is
    refreshed (the index has no per-file records until its first build).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        node_ids: The nodes the read names, or ``None`` for a whole-repo read.
        mode: Overrides ``[axiom_graph.staleness] refresh_before_read``.

    Returns:
        :class:`ReadRefresh`.
    """
    from axiom_graph.index.file_state import stat_behind  # noqa: PLC0415
    from axiom_graph.index.refresh import refresh_staleness  # noqa: PLC0415

    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    mode = mode or config.staleness.refresh_before_read
    named = None if node_ids is None else list(dict.fromkeys(node_ids))

    口 = Step(
        step_num=1,
        name="Refresh by mode",
        purpose="off: stat probe only; check, or a whole-repo read: the incremental check's refresh; a node-naming "
        "read: the cone of the named nodes, leaving the watermark",
    )
    with db._connect(db_path) as conn:
        v5 = db.pairs_ready(conn)
    refreshed = None
    behind = 0
    if v5 and mode == "off":
        with db._connect(db_path) as conn:
            records = db.get_file_records_conn(conn)
        behind = stat_behind(root, {loc: (r.mtime, r.size) for loc, r in records.items()})
    elif v5:
        cone = mode != "check" and named is not None
        refreshed = refresh_staleness(
            db_path,
            root,
            transitive_tags=config.staleness.transitive_tags,
            frozen_tags=config.staleness.frozen_tags,
            discover=not cone,
            # Discovery treats seeds as changed, so only the cone gets the named nodes.
            seed_node_ids=(named or ()) if cone else (),
            seeds_changed=False,
        )

    口 = Step(
        step_num=2,
        name="Read the shown statuses",
        purpose="The named nodes' stored own and link statuses and the structural changes in their files, batched",
    )
    with db._connect(db_path) as conn:
        structure = refreshed.structure if refreshed is not None else db.get_file_structures_conn(conn)
        statuses = db.get_staleness_for_conn(conn, named) if named else {}
        if named is not None and structure:
            locations = db.locations_of_conn(conn, named)
            structure = {loc: s for loc, s in structure.items() if loc in locations}
    return ReadRefresh(mode=mode, statuses=statuses, behind=behind, structure=dict(structure), refresh=refreshed)


@task(
    purpose=(
        "Read the current annotation findings for check: stored rows for files unchanged since the last build, "
        "in-memory rescans for edited ones, B4 resolved against the index, each finding flagged new or not"
    ),
    inputs="db_path, project root",
    outputs="AnnotationFindingsResult: current findings with new flags, new and resolved counts, files rescanned",
    critical=(
        "Never writes the findings store; only build does.  Walks exactly the files build walks.  A file the "
        "caller's discovery walk read is rescanned when its bytes differ from the bytes build last parsed (by the "
        "scan mtime while it has no parse record); any other file when its on-disk mtime differs from the indexed "
        "nodes.file_mtime"
    ),
)
def read_annotation_findings(
    db_path: Path,
    root: Path,
    observed: dict | None = None,
) -> AnnotationFindingsResult:
    """Compute check's annotation findings from the findings store.

    Runs pending schema migrations first, so an index from before the store
    upgrades on its first ``check``.  Walks the file set ``build`` walks
    (base skip dirs plus ``exclude_dirs``; ``js_paths`` when tree-sitter is
    available).  A file that has not changed since the last build
    contributes its stored rows; any other file is rescanned in memory.
    "Changed" is decided from *observed* (the discovery walk ``check`` just
    made, so no file is read twice) where it covers the file: its bytes
    against the bytes build last parsed, or the scan mtime while the file
    has no parse record.  A file it does not cover (a new file) is decided
    by its mtime.  B4 is resolved against the index with those rescans
    overlaid, the ``[validation]`` config is applied, and each finding is
    compared with the store as it stands.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        observed: Location -> :class:`~axiom_graph.index.file_state.FileObservation`
            from the caller's discovery walk (``CheckSummary.refresh.observed``).

    Returns:
        :class:`AnnotationFindingsResult`.
    """
    from axiom_graph.index import annotation_findings  # noqa: PLC0415
    from axiom_graph.index.file_state import file_unchanged_since, files_to_parse  # noqa: PLC0415

    db.run_migrations(db_path)
    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = builder.resolve_project_id(root, db_path, config=config)
    skip_dirs = builder._BASE_SKIP_DIRS | frozenset(config.scan.exclude_dirs)
    stored_mtimes = db.get_all_file_mtimes(db_path)
    js_available = bool(config.scan.js_paths) and annotation_findings.js_scanning_available()

    seen = {loc: obs for loc, obs in (observed or {}).items() if not obs.missing}
    changed: set[str] = set()
    if seen:
        with db._connect(db_path) as conn:
            records = db.get_file_records_conn(conn, list(seen))
        parsed = {loc: r.parsed_fp for loc, r in records.items() if r.parsed_fp is not None}
        changed = files_to_parse(seen, parsed, stored_mtimes, has_record=set(parsed))

    walked: set[str] = set()
    edited_py: list[Path] = []
    edited_js: list[Path] = []
    for files, edited in (
        (builder._iter_python_files(root, skip_dirs), edited_py),
        (builder._iter_js_files(root, config.scan.js_paths, skip_dirs) if js_available else (), edited_js),
    ):
        for path in files:
            rel = path.relative_to(root).as_posix()
            walked.add(rel)
            if rel in seen:
                if rel in changed:
                    edited.append(path)
            elif not file_unchanged_since(stored_mtimes.get(rel), path.stat().st_mtime):
                edited.append(path)

    # The build's import roots, so a rescanned file links (and B4 resolves)
    # exactly as the build would link it.
    source_roots = resolve_source_roots(root, config.scan.source_roots, skip_dirs)
    rescanned, nodes, edges = annotation_findings.rescan_in_memory(
        root, project_id, edited_py, edited_js, source_roots=source_roots
    )
    stored = db.read_annotation_store(db_path)
    hidden = (
        set()
        if js_available or not config.scan.js_paths
        else {f for f in stored.files if f.endswith((".js", ".jsx", ".ts", ".tsx"))}
    )
    # B4 asks only about the targets it resolves: the live set and the
    # re-export relation are read for those, on this connection.
    live_ids = annotation_findings.live_node_lookup(db_path, root, nodes, walked=walked)
    with db._connect(db_path) as conn, live_ids.reading_on(conn):
        star, named = annotation_findings.overlaid_reexport_relation(conn, nodes, edges)
        outcome = annotation_findings.compute_findings(
            stored,
            walked=walked,
            rescanned=rescanned,
            live_ids=live_ids,
            star=star,
            named=named,
            is_rule_enabled=config.validation.is_enabled,
            hidden=hidden,
        )
    return AnnotationFindingsResult(
        findings=outcome.findings,
        new=outcome.new,
        resolved=outcome.resolved,
        files_rescanned=len(edited_py) + len(edited_js),
    )


@dataclass
class CheckReport:
    """Result of :func:`load_check_report`: what ``axiom-graph check`` prints.

    Attributes:
        summary: The check summary, its per-node view already loaded.
        findings: The annotation findings, or ``None`` when reading them
            failed.
        findings_error: The error reading the findings raised, if any; the
            caller reports it after printing the statuses.
    """

    summary: CheckSummary
    findings: AnnotationFindingsResult | None = None
    findings_error: Exception | None = None


@workflow(
    purpose="Load what the check command prints on one connection: refresh and count the stored statuses, load the "
    "per-node view the output shows (every node, or the problem nodes) with its vias, and read the annotation "
    "findings",
    inputs="db_path, project root, full, every_node",
    outputs="CheckReport (summary with its per-node view loaded, annotation findings or the error reading them), or "
    "None when the index is empty",
)
@db.connection_scope
def load_check_report(
    db_path: Path,
    root: Path,
    *,
    full: bool = False,
    every_node: bool = False,
) -> CheckReport | None:
    """Refresh, count and read everything ``axiom-graph check`` prints, in one connection scope.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        full: Recompute every node from a re-hash of every file.
        every_node: Load every node's statuses (``--all`` or JSON output)
            rather than only the problem nodes'.

    Returns:
        :class:`CheckReport`, or ``None`` when the index holds no node.
    """
    口 = AutoStep(step_num=1, name="Refresh and count the stored statuses")
    cs = compute_check_summary(db_path, root, full=full)
    if cs is None:
        return None

    口 = Step(
        step_num=2,
        name="Load the per-node view",
        purpose="Every node's statuses in node-table order, or only the problem nodes', each LINKED_STALE row with "
        "its vias",
    )
    if every_node:
        _ = cs.statuses
        _ = cs.ordered_ids
    else:
        _ = cs.problem_statuses

    try:
        口 = AutoStep(step_num=3, name="Read the annotation findings")
        findings = read_annotation_findings(db_path, root, observed=getattr(cs.refresh, "observed", None))
    except Exception as exc:  # noqa: BLE001 -- reported by the caller after the statuses
        return CheckReport(summary=cs, findings_error=exc)
    return CheckReport(summary=cs, findings=findings)


@dataclass
class GraphView:
    """Result of :func:`read_graph_view`: the whole graph a viz view shows.

    Attributes:
        nodes: Every node, in node-table order (tags populated when asked).
        edges: Every edge, when asked; else empty.
        staleness: ``{node_id: (own, link)}`` as stored after the refresh.
        verifications: Every node's latest verification.
        behind: Tracked files the index is behind (``"off"`` refresh mode).
    """

    nodes: list
    edges: list
    staleness: dict
    verifications: dict
    behind: int = 0


@workflow(
    purpose="Read the whole graph a viz view shows on one connection: refresh the statuses the configured way (or "
    "as the check does), then every node, optionally every edge and the nodes' tags, the stored statuses and "
    "verifications",
    inputs="db_path, project root (None: no refresh), refresh mode, whether to read edges and tags",
    outputs="GraphView",
)
@db.connection_scope
def read_graph_view(
    db_path: Path,
    root: Path | None,
    *,
    mode: str | None = None,
    edges: bool = True,
    tags: bool = True,
) -> GraphView:
    """Refresh and read every node, edge, status and verification a whole-graph viz view shows.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory, or ``None`` to skip the refresh.
        mode: The refresh mode (``refresh_before_read``'s *mode*).
        edges: Read every edge.
        tags: Populate each node's tags.

    Returns:
        :class:`GraphView`.
    """
    rr = None
    if root is not None:
        口 = AutoStep(step_num=1, name="Refresh the shown statuses")
        rr = refresh_before_read(db_path, root, mode=mode)

    口 = Step(step_num=2, name="Read the graph", purpose="Every node, edge, tag, stored status and verification")
    nodes = db.all_nodes(db_path)
    all_edges = db.all_edges(db_path) if edges else []
    if tags:
        with db._connect(db_path) as conn:
            tag_map = db.get_tags_bulk_conn(conn, [n.id for n in nodes])
        for n in nodes:
            n.tags = tag_map.get(n.id, [])
    staleness = db.get_all_staleness(db_path)
    verifications = db.get_all_verifications(db_path)
    return GraphView(
        nodes=nodes,
        edges=all_edges,
        staleness=staleness,
        verifications=verifications,
        behind=rr.behind if rr is not None else 0,
    )


@workflow(
    purpose="Read a node's neighbourhood on one connection: the edges within depth hops, their nodes (tags "
    "included), and those nodes' statuses after the cone refresh",
    inputs="db_path, project root (None: no statuses), node id, direction, depth",
    outputs="(edges, nodes, {node_id: (own, link)})",
)
@db.connection_scope
def read_neighborhood(
    db_path: Path,
    root: Path | None,
    node_id: str,
    *,
    direction: str = "both",
    depth: int = 1,
) -> tuple[list, list, dict[str, tuple[str, str]]]:
    """Read the ego graph of *node_id* and refresh the statuses it shows.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory, or ``None`` for no statuses.
        node_id: The centre node.
        direction: ``"in"``, ``"out"`` or ``"both"``.
        depth: Hops to walk.

    Returns:
        ``(edges, nodes, statuses)``.
    """
    口 = Step(step_num=1, name="Read the ego graph", purpose="The edges within depth hops and their nodes, batched")
    edges = db.query_edges(db_path, node_id, direction=direction, depth=depth)
    node_ids: set[str] = {node_id}
    for e in edges:
        node_ids.add(e.from_id)
        node_ids.add(e.to_id)
    with db._connect(db_path) as conn:
        nodes = list(db.get_nodes_conn(conn, node_ids).values())

    if root is None:
        return edges, nodes, {}
    口 = AutoStep(step_num=2, name="Refresh the cone of the shown nodes")
    rr = refresh_before_read(db_path, root, [n.id for n in nodes])
    return edges, nodes, {n.id: rr.statuses.get(n.id, (VERIFIED, VERIFIED)) for n in nodes}


@workflow(
    purpose="Verify nodes for the viz: read them in one batch, refuse the missing ones and the ones with no code "
    "hash, and send the rest through mark_clean_nodes (verification, dependency pairs, closing refresh), all on "
    "one connection",
    inputs="db_path, project root, node ids, reason, verified_by",
    outputs="{node_id: 'ok' | 'not_found' | 'no_code_hash'}",
)
@db.connection_scope
def verify_nodes_checked(
    db_path: Path,
    root: Path,
    node_ids: list[str],
    reason: str,
    *,
    verified_by: str,
) -> dict[str, str]:
    """Verify the nodes of *node_ids* that exist and have a code hash, through :func:`mark_clean_nodes`.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        node_ids: The ids to verify, in order; a duplicate is verified again.
        reason: Free-form reason recorded in the history meta.
        verified_by: Verifier identifier.

    Returns:
        Each id's outcome: ``"ok"`` (verified), ``"not_found"`` or
        ``"no_code_hash"`` (refused, nothing written for it).
    """
    口 = Step(
        step_num=1, name="Check the nodes", purpose="One batched read; refuse missing nodes and nodes with no code hash"
    )
    with db._connect(db_path) as conn:
        nodes = db.get_nodes_conn(conn, node_ids)
    outcome: dict[str, str] = {}
    accepted: list[str] = []
    for nid in node_ids:
        node = nodes.get(nid)
        if node is None:
            outcome[nid] = "not_found"
        elif not node.code_hash:
            outcome[nid] = "no_code_hash"
        else:
            outcome[nid] = "ok"
            accepted.append(nid)

    if accepted:
        口 = Step(
            step_num=2,
            name="Verify through the mark_clean funnel",
            purpose="Send the accepted nodes through mark_clean_nodes: verification, dependency pairs and the closing "
            "refresh",
        )
        mark_clean_nodes(db_path, root, accepted, reason, verified_by=verified_by)
    return outcome


def _summarize_statuses(
    db_path: Path,
    root: Path,
    statuses: dict[str, tuple[str, str, list[str]]],
    include_frozen: bool = False,
) -> CheckSummary:
    """Count computed statuses into a :class:`CheckSummary`.

    The one counting rule behind ``check`` and the counts a build reports.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory (its config names the frozen tags).
        statuses: Computed ``{node_id: (own, link, via)}`` statuses.
        include_frozen: Count frozen-doc sections too; see
            :func:`compute_check_summary`.

    Returns:
        :class:`CheckSummary` over *statuses*.
    """
    config = AxiomGraphConfig.load(root)

    # When include_frozen=False, drop frozen-doc rows (sections and the doc
    # envelopes) from the statuses dict before counting, so the summary
    # numbers describe only the non-frozen surface — except a frozen row
    # whose link status is BROKEN_LINK, which stays and is counted under
    # BROKEN_LINK only, so the summary agrees with drift_query.  Skip the
    # resolution work entirely when frozen_tags is empty (O(1) hot path
    # preserved).
    frozen_broken_ids: set[str] = set()
    if not include_frozen and config.staleness.frozen_tags:
        frozen_ids = set(db.get_frozen_rows(db_path, config.staleness.frozen_tags))
        if frozen_ids:
            frozen_broken_ids = {nid for nid in frozen_ids if nid in statuses and statuses[nid][1] == BROKEN_LINK}
            statuses = {
                nid: trip for nid, trip in statuses.items() if nid not in frozen_ids or nid in frozen_broken_ids
            }

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
    for nid, (own, link, _via) in statuses.items():
        if nid in frozen_broken_ids:
            link_counts[BROKEN_LINK] += 1
            continue
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
        own_present={own for own, _link, _via in statuses.values()},
        link_present={link for _own, link, _via in statuses.values()},
        _statuses=statuses,
    )


# ---------------------------------------------------------------------------
# mark_clean
# ---------------------------------------------------------------------------


@db.connection_scope
def mark_clean_nodes(
    db_path: Path,
    root: Path,
    node_ids: list[str],
    reason: str,
    *,
    verified_by: str,
    verification_op: str = VERIFICATION_OP_MARK_CLEAN,
    pairs_from_index: bool = False,
    refresh: bool = True,
) -> MarkCleanResult:
    """Record AGENT_VERIFIED / MANUAL_VERIFIED for one or more nodes, then refresh the statuses they can move.

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
        pairs_from_index: Record each node's dependency pairs at the
            index's live hashes rather than the files on disk.  Set by
            :func:`reverify_nodes` for its cascade, so a dependent cleared
            there never absorbs an edit to another dependency that no build
            has recorded yet.
        refresh: End with one scoped refresh of the statuses the
            verifications can move (:func:`~axiom_graph.index.refresh.refresh_after_write`),
            so they are stored now rather than at the next ``check``.
            :func:`reverify_nodes` passes ``False`` and refreshes once,
            after its cascade.

    Each verification records its node's dependency pairs; every file a
    pair needs is hashed once for the whole call.

    Returns:
        :class:`MarkCleanResult` with marked vs not_found IDs plus the
        aggregate-honesty classification (``inherited`` / ``mixed``).
    """
    from axiom_graph.index.mark_clean import PairRecorder, mark_node_clean
    from axiom_graph.index.staleness import (
        _get_linked_stale_ids,
        classify_inherited_link,
        composes_map_conn,
        expand_composes_subtree,
    )

    # One connection and one transaction for the whole call; every node's
    # reads and writes go through it.
    inherited: dict[str, list[str]] = {}
    mixed: dict[str, list[str]] = {}
    marked: list[str] = []
    not_found: list[str] = []
    recorder = PairRecorder(root, from_index=pairs_from_index)
    with db._connect(db_path) as conn:
        # The lookup indexes the per-file and per-link reads below use, on an
        # index no build has run init_db on since they were added.
        db.ensure_file_state_conn(conn)

        # Classify aggregate targets BEFORE marking: marking writes
        # verified_at, which clears each target's own signal from the live
        # stale map and would misclassify own-signal nodes as inherited-only.
        # Ordinary (childless) targets skip the stale-map computation, and the
        # map is computed for the parents' subtrees only.
        children_map = composes_map_conn(conn, node_ids, upward=False)
        parents = [nid for nid in dict.fromkeys(node_ids) if nid in children_map]
        if parents:
            config = AxiomGraphConfig.load(root)
            subtree_scope = {d for nid in parents for d in expand_composes_subtree(db_path, nid, children_map)}
            stale_map = _get_linked_stale_ids(
                db_path,
                transitive_tags=config.staleness.transitive_tags,
                frozen_tags=config.staleness.frozen_tags,
                scope=subtree_scope | set(parents),
            )
            batch = set(node_ids)
            for nid in parents:
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

        nodes = db.get_nodes_conn(conn, node_ids)
        recorder.prepare(conn, list(nodes.values()))
        for nid in node_ids:
            node = nodes.get(nid)
            if node is None:
                not_found.append(nid)
                continue
            mark_node_clean(
                db_path,
                root,
                node,
                reason,
                verified_by,
                verification_op=verification_op,
                pairs=recorder,
                conn=conn,
            )
            marked.append(nid)

    # Classifications only apply to nodes that were actually marked.
    for nid in not_found:
        inherited.pop(nid, None)
        mixed.pop(nid, None)

    if refresh and marked:
        from axiom_graph.index.refresh import refresh_after_write  # noqa: PLC0415

        refresh_after_write(db_path, root, marked)

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
    to it does not invalidate its dependents".  The single-source form of
    :func:`reverify_nodes`, which it delegates to so the two paths cannot
    drift.  The flow:

    0. Bring the stored statuses up to date with the refresh ``check``
       runs, and count LINKED_STALE as ``check`` does (frozen docs and
       their envelopes excluded): the "before" count.
    1. Expand the source to its full ``composes`` subtree (composite
       sources match staleness rooted at any of their parts).
    2. Compute the live stale map with the same transitive/frozen tag
       configuration as ``check``.
    3. Resolve every stale entry's via chain to its leaf root offenders
       (:func:`axiom_graph.index.staleness.resolve_root_offenders`).
    4. Narrow each root set to the offenders that are still outstanding:
       offenders already reverified since their last change — themselves
       or through a reverify of a ``composes`` ancestor — drop out
       (:func:`axiom_graph.index.staleness.already_reverified_offenders`).
       **Reverifies compose** — reverifying every offender behind a
       dependent reaches the same end state as marking that dependent
       clean directly, so the last reverify in the series clears it.
    5. Select nodes whose outstanding offenders all fall within the
       source set; nodes still outstanding via *other* offenders are
       skipped and reported (under-clearing is acceptable,
       over-clearing is not).  Outstanding offenders that were only
       marked clean since their last change are named in
       ``marked_clean_offenders`` so the caller knows to reverify them.
    6. Clear with verification rows, the only clearing mechanism; cascade
       rows carry ``[reverify:<source>]`` provenance in reason/history.  A
       selected dependent whose own status is not VERIFIED after the entry
       refresh (or whose file no longer matches the index) gets a
       link-only verification: its links are verified, its snapshot and
       baseline are not, so its own change stays flagged and is reported
       in ``own_change_kept``.  Every other dependent is verified in full,
       as ``mark_clean`` does.
    7. Finish with the shared staleness recompute (the same single-writer
       path ``check`` uses) and count LINKED_STALE again the way ``check``
       does: the "after" count.  ``cleared`` lists only nodes this call
       verified; aggregates that settled by inheritance are ``settled``.

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
    batch = reverify_nodes(db_path, root, [source_node_id], reason, verified_by=verified_by)
    if batch.not_found:
        return ReverifyResult(source_id=source_node_id, not_found=True)
    return ReverifyResult(
        source_id=source_node_id,
        verified=batch.verified,
        cleared=batch.cleared,
        own_change_kept=batch.own_change_kept,
        settled=batch.settled,
        skipped=batch.skipped,
        marked_clean_offenders=batch.marked_clean_offenders,
        before_linked_stale=batch.before_linked_stale,
        after_linked_stale=batch.after_linked_stale,
    )


@db.connection_scope
def reverify_nodes(
    db_path: Path,
    root: Path,
    source_node_ids: list[str],
    reason: str,
    *,
    verified_by: str,
) -> ReverifyBatchResult:
    """Verify every source in *source_node_ids* and clear the LINKED_STALE they caused.

    The same assertion as one :func:`reverify_node` call per source, made
    once: the union of every found source's ``composes`` subtree is ONE
    source set.  A dependent whose outstanding root offenders all fall in
    that union clears in this call whatever the order the sources were
    listed in; only dependents held by an offender outside the batch are
    skipped.  Outstanding-offender narrowing (including composite-ancestor
    settlement) is the same as the single form, and one scoped refresh
    of the statuses the batch can move runs once, after the cascade.

    A cleared dependent's verification reason names the batch source(s)
    that held it: ``[reverify:<id>]`` for one, ``[reverify:<a>, <b>]``
    for several.  Unknown IDs are reported in ``not_found`` and do not
    stop the rest.

    The call starts with the refresh ``check`` runs (before selection and
    before any write; there is none between the source writes and the
    cascade), so an edit on disk that no build has recorded counts: an
    unbuilt edit to another dependency makes it an outstanding offender
    (the dependent is skipped), and an unbuilt edit to the dependent itself
    makes its write link-only.  The before and after LINKED_STALE counts
    are the ones ``check`` shows (frozen docs, envelopes included, left
    out).

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        source_node_ids: The nodes the caller verified.  Order is kept;
            duplicates are dropped.
        reason: Free-form reason shared by every verification row.
        verified_by: Verifier identifier (``"human"``, ``"agent"``, ...).
            Required keyword.

    Returns:
        :class:`ReverifyBatchResult` — one report for the whole batch.
    """
    from axiom_graph.index.refresh import expand_scope
    from axiom_graph.index.staleness import (
        _get_linked_stale_ids,
        already_reverified_offenders,
        composes_ancestors,
        composes_map_conn,
        expand_composes_subtree,
        marked_clean_not_reverified_offenders,
        resolve_root_offenders,
    )

    config = AxiomGraphConfig.load(root)
    with db._connect(db_path) as conn:
        found = db.get_nodes_conn(conn, list(dict.fromkeys(source_node_ids)))
        sources: list[str] = [nid for nid in dict.fromkeys(source_node_ids) if nid in found]
        not_found: list[str] = [nid for nid in dict.fromkeys(source_node_ids) if nid not in found]
        if not sources:
            return ReverifyBatchResult(not_found=not_found)

    # Entry refresh: the statuses check would store, counted the way check
    # counts them (the "before" count).  Before selection and any write.
    entry = compute_check_summary(db_path, root)
    before_count = entry.link_counts.get(LINKED_STALE, 0) if entry is not None else 0
    with db._connect(db_path) as conn:
        before_ids = {
            nid for nid, _own, link in db.get_ordered_staleness_conn(conn, problems_only=True) if link == LINKED_STALE
        }

        # Attribution: one source set, the union of every source's subtree.
        # The per-source subtrees are kept to name each cleared dependent's
        # holder(s) in its provenance.
        down_map = composes_map_conn(conn, sources, upward=False)
        subtrees = {sid: {sid} | expand_composes_subtree(db_path, sid, down_map) for sid in sources}
        source_set = set().union(*subtrees.values())

        # Every node a root in the source set can reach: its dependents, by
        # the same widening the scoped refresh uses.  A stale entry outside it
        # cannot be rooted at a source.
        dependents = expand_scope(db_path, conn, source_set, config.staleness.transitive_tags)

    # Live stale map with check-parity configuration, for the dependents and
    # every node their via chains pass through (a chain's intermediate is
    # judged too, so each entry resolves to the same roots as on the full map).
    stale_map: dict[str, list[str]] = {}
    evaluated: set[str] = set()
    frontier = set(dependents)
    while frontier:
        found_map = _get_linked_stale_ids(
            db_path,
            transitive_tags=config.staleness.transitive_tags,
            frozen_tags=config.staleness.frozen_tags,
            scope=frontier,
        )
        stale_map.update(found_map)
        evaluated |= frontier
        frontier = {via for vias in found_map.values() for via in vias} - evaluated
    roots_map = resolve_root_offenders(stale_map)

    # Reverifies compose: an offender reverified since its own last change
    # — directly, or through a reverify of any composes ancestor (module,
    # doc, parent section) — is no longer outstanding, so a series of
    # reverifies adds up.  Read once for every root offender in play and
    # its ancestors, then decided by the pure primitive.  Computed before
    # any write below, so this call never discounts its own sources
    # mid-flight (they are in source_set).
    all_roots = {rid for roots in roots_map.values() for rid in roots}
    with db._connect(db_path) as conn:
        up_map = composes_map_conn(conn, all_roots, upward=True)
    ancestors = composes_ancestors(all_roots, up_map)
    lookup_ids = all_roots | {aid for ids in ancestors.values() for aid in ids}
    latest_change_ids, verification_ops = db.get_verification_ordering_rows(db_path, sorted(lookup_ids))
    already_reverified = already_reverified_offenders(
        all_roots,
        latest_change_ids=latest_change_ids,
        verification_ops=verification_ops,
        ancestors=ancestors,
    )

    named = set(sources)
    selected: dict[str, tuple[str, ...]] = {}
    skipped: dict[str, list[str]] = {}
    for nid, roots in sorted(roots_map.items()):
        if nid in named:
            # Sources are verified below regardless of their own staleness.
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
            selected[nid] = tuple(sid for sid in sources if root_set & subtrees[sid])
        else:
            skipped[nid] = sorted(outstanding - source_set)

    # Why each skipped dependent still blocks: name the outstanding
    # offenders that were verified, but by mark_clean rather than reverify.
    # Informational only — computed after selection, never feeds it.
    marked_clean = marked_clean_not_reverified_offenders(
        {oid for offs in skipped.values() for oid in offs},
        latest_change_ids=latest_change_ids,
        verification_ops=verification_ops,
        ancestors=ancestors,
    )

    # ADR boundary: verification rows via the mark_clean machinery remain
    # the ONLY clearing mechanism.  Sources first (plain reason, marked as
    # reverify-written so each becomes a term in the composition), then the
    # cascade set with reverify-of-source provenance, one write per
    # distinct holder group.
    mark_clean_nodes(
        db_path,
        root,
        sources,
        reason,
        verified_by=verified_by,
        verification_op=VERIFICATION_OP_REVERIFY,
        refresh=False,
    )
    groups: dict[tuple[str, ...], list[str]] = {}
    for nid, holders in selected.items():
        groups.setdefault(holders, []).append(nid)
    kept: list[str] = []
    for holders, members in groups.items():
        tag = f"[reverify:{', '.join(holders)}]"
        cascade_reason = f"{tag} {reason}" if reason else tag
        # No per-call refresh (D-16): a refresh between the sources and the
        # cascade would re-read a sibling's unbuilt edit into the index, and
        # the cascade's pairs would then absorb it.
        kept.extend(_write_reverify_cascade(db_path, root, members, cascade_reason, verified_by=verified_by))

    # Shared recompute, once for the whole batch — same single-writer
    # record_staleness path check uses — counted the way check counts.
    cs = compute_check_summary(db_path, root)
    after_count = cs.link_counts.get(LINKED_STALE, 0) if cs is not None else 0
    with db._connect(db_path) as conn:
        after_ids = {
            nid for nid, _own, link in db.get_ordered_staleness_conn(conn, problems_only=True) if link == LINKED_STALE
        }

    verified = [*sources, *selected]
    left = before_ids - after_ids
    frozen = set(db.get_frozen_rows(db_path, config.staleness.frozen_tags))
    return ReverifyBatchResult(
        sources=sources,
        not_found=not_found,
        verified=verified,
        cleared=sorted(nid for nid in verified if nid in left),
        own_change_kept=sorted(kept),
        settled=sorted(left - set(verified) - frozen),
        skipped=skipped,
        marked_clean_offenders=sorted(marked_clean),
        before_linked_stale=before_count,
        after_linked_stale=after_count,
    )


def _own_drifted(row: dict | None, disk: tuple[str | None, str | None]) -> bool:
    """Whether a cascade member's own content changed and nobody reviewed it.

    Args:
        row: The member's stored row (:func:`axiom_graph.db.get_live_rows_conn`).
        disk: Its hashes on disk now.

    Returns:
        ``True`` when its stored own status is not VERIFIED, or the disk no
        longer holds the hashes the last refresh stored for it.
    """
    if row is None:
        return False
    if row["own_status"] != VERIFIED:
        return True
    live = row["live_code_hash"]
    if not live or live == db.MISSING_LIVE_HASH:
        return False
    return (disk[0], disk[1]) != (live, row["live_desc_hash"])


def _write_reverify_cascade(
    db_path: Path,
    root: Path,
    members: list[str],
    reason: str,
    *,
    verified_by: str,
) -> list[str]:
    """Verify one holder group of a reverify cascade; return the members verified link-only.

    One connection and one :class:`~axiom_graph.index.mark_clean.PairRecorder`
    reading the index's hashes, so a member never absorbs an edit no build
    has recorded.  A member whose own content changed and was not reviewed
    (:func:`_own_drifted`) gets a link-only verification
    (:func:`~axiom_graph.index.mark_clean.verify_links_conn`); every other
    member is verified in full, as ``mark_clean`` does.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        members: The selected dependents of one holder group.
        reason: The ``[reverify:<source>]`` reason.
        verified_by: Verifier identifier.

    Returns:
        The members verified link-only (their own change kept).
    """
    from axiom_graph.index.mark_clean import PairRecorder, mark_node_clean, verify_links_conn  # noqa: PLC0415

    recorder = PairRecorder(root, from_index=True)
    kept: list[str] = []
    with db._connect(db_path) as conn:
        db.ensure_file_state_conn(conn)
        nodes = db.get_nodes_conn(conn, members)
        rows = db.get_live_rows_conn(conn, list(nodes))
        recorder.prepare(conn, list(nodes.values()))
        for nid in members:
            node = nodes.get(nid)
            if node is None:
                continue
            if _own_drifted(rows.get(nid), recorder.current_hashes(conn, node)):
                verify_links_conn(conn, recorder, node, reason=reason, verified_by=verified_by)
                kept.append(nid)
                continue
            mark_node_clean(db_path, root, node, reason, verified_by, pairs=recorder, conn=conn)
    return kept


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def anchor_file_on_disk(root: Path, subtype: str | None, location: str | None) -> bool:
    """Return True when *subtype* is a file-level node whose file still exists.

    A file-level node (module, DocJSON doc, config) whose file is on disk is
    not gone: a ``NOT_FOUND`` on it is inherited through composite
    inheritance, from a removed child or from children a file that no longer
    parses does not yield (:func:`file_unparseable` tells the two apart).
    :func:`purge_nodes` refuses such a node and
    ``axiom-graph purge --all-not-found`` keeps it, both through this check.

    Args:
        root: Project root, for resolving *location* on disk.
        subtype: The node's subtype.
        location: The node's location (a project-relative path, optionally
            with a ``#L`` fragment).

    Returns:
        True for a file-level subtype whose file exists under *root*.
    """
    from axiom_graph.index.staleness import _ANCHOR_SUBTYPES

    path = (location or "").split("#", 1)[0]
    return subtype in _ANCHOR_SUBTYPES and bool(path) and (root / path).exists()


#: Subtypes of the nodes a document file (DocJSON or Markdown) yields.
_DOC_SUBTYPES = frozenset({"docjson", "docjson_doc", "docjson_section"})


def file_unparseable(root: Path, node_id: str, subtype: str | None, location: str | None) -> bool:
    """Return True when a node's file is on disk but does not parse.

    Every node of such a file can read ``NOT_FOUND`` although it is not
    gone, so :func:`purge_nodes` refuses them and ``axiom-graph purge
    --all-not-found`` keeps them.  The file is parsed the way the
    staleness pass reads it:

    - a document node's file (DocJSON or Markdown) through the document
      scanner, which yields at least the document node for any file it can
      read;
    - a ``.py`` file through ``ast.parse``;
    - a JS/TS file through tree-sitter.  tree-sitter reads past a syntax
      error and still yields the rest of the file, but drops the function
      the error is in, so a file whose parse tree has an error counts.
      Without tree-sitter installed a JS/TS file counts too: its nodes
      cannot be read, so none of them can be shown to be gone.

    A file that parses with some or all of its functions removed is not
    unparseable: the removed ones are really gone.  No other kind of file
    is ever unparseable.  Why a file failed is logged at debug level.

    Args:
        root: Project root, for resolving *location* on disk.
        node_id: The node's id; its project-id prefix scopes a document scan.
        subtype: The node's subtype.
        location: The node's location (a project-relative path, optionally
            with a ``#L`` fragment).

    Returns:
        True when the file exists under *root* and fails to parse.
    """
    path = (location or "").split("#", 1)[0]
    abs_path = root / path
    if not path or not abs_path.is_file():
        return False
    if subtype in _DOC_SUBTYPES:
        # The private scanner entry is deliberate: it is the reader the
        # staleness pass itself uses for a document file (DocJSON and Markdown,
        # through the open parse cache), and its empty result is what made the
        # file's nodes NOT_FOUND.  The public scan_blob_at_location scans a copy
        # mirrored into a temp dir, a second way of reading the file.
        from axiom_graph.scanners.node_hashing import _scan_docjson_sections  # noqa: PLC0415

        if _scan_docjson_sections(abs_path, root, node_id.split("::", 1)[0]):
            return False
        logger.debug("purge: document file %s does not scan", path)
        return True
    if abs_path.suffix == ".py":
        try:
            ast.parse(abs_path.read_text(encoding="utf-8", errors="replace"), filename=str(abs_path))
        except (SyntaxError, ValueError, OSError) as exc:
            logger.debug("purge: %s does not parse: %s", path, exc)
            return True
        return False
    from axiom_graph.scanners import js_scanner  # noqa: PLC0415

    if abs_path.suffix in js_scanner.JS_TS_EXTENSIONS:
        if not js_scanner.HAS_TREE_SITTER:
            logger.debug("purge: %s cannot be parsed: tree-sitter is not installed", path)
            return True
        try:
            broken = js_scanner.file_has_parse_errors(abs_path)
        except OSError as exc:
            logger.debug("purge: %s cannot be read: %s", path, exc)
            return True
        if broken:
            logger.debug("purge: %s parses with errors", path)
        return broken
    return False


def _cached_unparseable(
    verdicts: ParseVerdicts, root: Path, node_id: str, subtype: str | None, location: str | None
) -> bool:
    """:func:`file_unparseable`, parsing the file only when *verdicts* has no verdict for it yet.

    Args:
        verdicts: The operation's cache; a new verdict is stored in it.
        root: Project root.
        node_id: The node's id.
        subtype: The node's subtype.
        location: The node's location.

    Returns:
        The file's verdict.
    """
    key = ((location or "").split("#", 1)[0], subtype in _DOC_SUBTYPES)
    if key not in verdicts:
        verdicts[key] = file_unparseable(root, node_id, subtype, location)
    return verdicts[key]


def select_purgeable_not_found(db_path: Path, root: Path) -> NotFoundSelection:
    """Split the index's NOT_FOUND nodes into those to purge and those to keep.

    Every node whose file is on disk but does not parse
    (:func:`file_unparseable`) is kept: it reads NOT_FOUND only because the
    file could not be read, and it comes back once the file is fixed and
    check re-runs.  Such nodes are grouped by file, so a preview can name
    each file once.  A file-level node (module, DocJSON doc, config file)
    whose file is still on disk and parses is NOT_FOUND only because
    composite inheritance passed up a removed child's status
    (:func:`anchor_file_on_disk`); the node itself is not gone, so it is
    kept.  It clears on the next check once the child is purged.  These are
    the checks :func:`purge_nodes` refuses such nodes with.  Each file is
    parsed once; pass the result's ``parse_verdicts`` to :func:`purge_nodes`
    so the purge does not parse it again.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root, for resolving node locations on disk.

    Returns:
        The :class:`NotFoundSelection`.
    """
    ids = sorted(nid for group in db.query_drift_ids_by_status(db_path, filter=NOT_FOUND) for nid in group["ids"])
    with db._connect(db_path) as conn:
        nodes = db.get_nodes_conn(conn, ids)
    selection = NotFoundSelection()
    for nid in ids:
        node = nodes.get(nid)
        if node is None:
            selection.to_purge.append(nid)
            continue
        if _cached_unparseable(selection.parse_verdicts, root, nid, node.subtype, node.location):
            selection.unparseable.setdefault((node.location or "").split("#", 1)[0], []).append(nid)
        elif anchor_file_on_disk(root, node.subtype, node.location):
            selection.inherited.append(nid)
        else:
            selection.to_purge.append(nid)
    return selection


def _live_anchor_refusal(conn: sqlite3.Connection, node_id: str, location: str, *, unparseable: bool) -> PurgeResult:
    """Build the refusal for a NOT_FOUND file-level node whose file is on disk.

    When the file does not parse (*unparseable*, from
    :func:`file_unparseable`), its nodes are live, so the refusal is
    :data:`PURGE_REFUSED_UNPARSEABLE` and lists none of them.  Otherwise the
    NOT_FOUND is inherited from the NOT_FOUND nodes stored for the same
    file, leaving out file-level nodes and the pass-through kinds staleness
    never hashes (steps, external packages, entities):
    :data:`PURGE_REFUSED_INHERITED`, listing them.

    Args:
        conn: Open SQLite connection.
        node_id: The refused file-level node.
        location: Its location (a project-relative path).
        unparseable: Whether its file does not parse.

    Returns:
        The refusal :class:`PurgeResult` for *node_id*.
    """
    from axiom_graph.index.staleness import _ANCHOR_SUBTYPES
    from axiom_graph.scanners.node_hashing import _PASSTHROUGH_SUBTYPES

    if unparseable:
        return PurgeResult(node_id=node_id, purged=False, reason=PURGE_REFUSED_UNPARSEABLE)
    path = location.split("#", 1)[0]
    rows = conn.execute(
        "SELECT id, node_type, subtype FROM nodes WHERE id != ? AND own_status = 'NOT_FOUND'"
        " AND (location = ? OR substr(location, 1, ?) = ?)",
        (node_id, path, len(path) + 1, f"{path}#"),
    ).fetchall()
    not_found = sorted(
        r["id"]
        for r in rows
        if r["subtype"] not in _ANCHOR_SUBTYPES
        and r["subtype"] not in _PASSTHROUGH_SUBTYPES
        and r["node_type"] != "entity"
    )
    return PurgeResult(node_id=node_id, purged=False, reason=PURGE_REFUSED_INHERITED, deleted_children=not_found)


def purge_nodes(
    db_path: Path,
    root: Path,
    node_ids: list[str],
    reason: str,
    *,
    actor: str,
    parse_verdicts: ParseVerdicts | None = None,
) -> list[PurgeResult]:
    """Purge one or more NOT_FOUND nodes from the index.

    Doc nodes are cascade-deleted via ``delete_doc_by_id`` (sections too);
    code/other nodes via ``delete_node_by_id``.  A preserved DELETED
    history row is recorded with *actor* and the supplied reason.

    A file-level node (module, DocJSON doc, config) whose file is still on
    disk is refused even when it reads ``NOT_FOUND``, since purging it would
    delete a live node's history and verification.  Usually the status is
    inherited from removed children: the refusal carries
    :data:`PURGE_REFUSED_INHERITED` and lists the NOT_FOUND nodes in the
    same file, the ones to purge if they were really removed.  Any node
    whose file is on disk but does not parse (:func:`file_unparseable`) is
    refused with :data:`PURGE_REFUSED_UNPARSEABLE` and nothing listed: the
    file-level node and every function or section in it read NOT_FOUND
    only because the file could not be read, so none of them is gone.

    Each file is parsed at most once per call, and every parse happens
    before the first delete, so none runs while the write transaction is
    open.

    Purging a file-level anchor (whose file is gone) also clears the stored
    ``file_mtime`` of every remaining row at its location, so if the file
    comes back the next build rescans it and re-creates the anchor — even
    when the file's other rows carry stamps from an older version.  The
    staleness fast pass never promotes those rows, so the rescan cannot
    launder them.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root, for checking whether a file-level node's file
            is on disk.
        node_ids: Node IDs to purge.
        reason: Free-form reason recorded in the history meta.
        actor: Who is purging, recorded as the history meta's ``actor``
            (``"agent"`` from the MCP tool, ``"human"`` from the CLI).
        parse_verdicts: Parse verdicts the operation already made, e.g.
            :attr:`NotFoundSelection.parse_verdicts`; a file with a verdict
            is not parsed again.  New verdicts are added to it.

    Returns:
        One :class:`PurgeResult` per input node, in input order.
    """
    from axiom_graph.index.staleness import _ANCHOR_SUBTYPES

    verdicts: ParseVerdicts = {} if parse_verdicts is None else parse_verdicts
    with db._connect(db_path) as conn:
        candidates = [
            conn.execute(
                "SELECT id, subtype, location FROM nodes WHERE id = ? AND own_status = ?", (nid, NOT_FOUND)
            ).fetchone()
            for nid in dict.fromkeys(node_ids)
        ]
    for row in candidates:
        if row is not None:
            _cached_unparseable(verdicts, root, row["id"], row["subtype"], row["location"])

    results: list[PurgeResult] = []
    with db._connect(db_path) as conn:
        for nid in node_ids:
            row = conn.execute(
                "SELECT id, node_type, subtype, location, own_status FROM nodes WHERE id = ?",
                (nid,),
            ).fetchone()
            if row is None:
                results.append(PurgeResult(node_id=nid, purged=False, reason="not_found_in_index"))
                continue
            status = row["own_status"]
            if status != "NOT_FOUND":
                results.append(PurgeResult(node_id=nid, purged=False, reason=f"status_{status}"))
                continue
            unparseable = _cached_unparseable(verdicts, root, nid, row["subtype"], row["location"])
            if anchor_file_on_disk(root, row["subtype"], row["location"]):
                results.append(_live_anchor_refusal(conn, nid, row["location"], unparseable=unparseable))
                continue
            if unparseable:
                results.append(PurgeResult(node_id=nid, purged=False, reason=PURGE_REFUSED_UNPARSEABLE))
                continue
            reason_meta = {"actor": actor, "reason": reason}
            is_doc = conn.execute("SELECT 1 FROM docs WHERE id = ?", (nid,)).fetchone() is not None
            if is_doc:
                db.delete_doc_by_id(conn, nid, reason_meta=reason_meta)
            else:
                db.delete_node_by_id(conn, nid, reason_meta=reason_meta)
            if row["subtype"] in _ANCHOR_SUBTYPES and row["location"]:
                db.clear_location_file_mtime_conn(conn, row["location"])
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
    *new_id* via :func:`db.record_code_rename_conn`, and *new_id*'s ``own_status``
    is forced to ``RENAMED`` (sticky, consistent with the auto-apply path), in
    one transaction: a failure part-way leaves the index as it was.  DocJSON
    links naming *old_id* are then patched on disk.

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
        # Read before the first write, so no lock is held while git runs.
        git_sha = _git_sha(root)
        db.record_code_rename_conn(conn, old_id, new_id, new_location)
        _force_renamed_status_conn(conn, new_id, manual=True, now=db._now_utc(), git_sha=git_sha)

    patched = _patch_renamed_links(root, old_id, new_id)
    return RenameApplyResult(
        True, old_id, new_id, links_not_patched=patched.not_patched, links_unreadable=patched.unreadable
    )


def revert_rename(
    db_path: Path,
    root: Path,
    new_id: str,
) -> RenameRevertResult:
    """Un-weld a previously applied rename via symmetric migrate-back (US-6).

    Looks up the ``node_renames`` mapping for *new_id*, re-runs the migration
    in reverse (``record_code_rename_conn(new_id -> old_id)``) -- no inverse-patch
    storage is kept -- then restores *old_id* as the live identity, detaches
    *new_id* as a fresh node, and clears the ``node_renames`` rows for the pair
    so the round-trip leaves no residual mapping.  The index changes are one
    transaction: a failure part-way leaves the index as it was.  DocJSON links
    naming *new_id* are then patched back on disk.

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
        # Read before the first write, so no lock is held while git runs.
        git_sha = _git_sha(root)

        # Symmetric migrate-back: history/verification/edges return to old_id.
        db.record_code_rename_conn(conn, new_id, old_id, old_loc)

        now = db._now_utc()
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

    patched = _patch_renamed_links(root, new_id, old_id)
    return RenameRevertResult(
        True, new_id, old_id=old_id, links_not_patched=patched.not_patched, links_unreadable=patched.unreadable
    )


def _patch_renamed_links(root: Path, old_id: str, new_id: str) -> LinkPatchResult:
    """Point DocJSON links naming *old_id* at *new_id* on disk, after the rename is committed.

    Returns:
        The rewrite's result: ``not_patched`` files still name *old_id*
        (locks busy); ``unreadable`` files could not be checked.
    """
    from axiom_graph.index.link_maintenance import patch_doc_links_batch  # noqa: PLC0415

    return patch_doc_links_batch(root, {old_id: new_id})


def link_rewrite_note(unreadable: list[str], not_patched: list[str], old_ref: str, new_ref: str) -> list[str]:
    """The warning lines for a rename's link rewrite: files it could not rewrite apart from files it could not check.

    Args:
        unreadable: Files the rewrite could not read or parse.
        not_patched: Files that still link *old_ref* (write locks busy).
        old_ref: The renamed-away id.
        new_ref: The id those links should point at.

    Returns:
        Zero, one or two lines without a ``WARNING:`` prefix.
    """
    return link_rewrite_warnings(unreadable, not_patched, old_ref, new_ref)


def _force_renamed_status_conn(
    conn: sqlite3.Connection, new_id: str, *, manual: bool, now: str, git_sha: str | None
) -> None:
    """Persist ``own_status = RENAMED`` on *new_id* with a transition event.

    Mirrors the auto-apply path's sticky overlay: the persisted ``RENAMED`` is
    preserved across subsequent builds (cleared only by ``mark_clean`` or a
    genuine ``NOT_FOUND``).

    Args:
        conn: Open connection; the caller owns the transaction.
        new_id: Node to mark ``RENAMED``.
        manual: Whether this came from the manual ``apply_rename`` escape hatch
            (recorded in the history meta).
        now: Timestamp of the history row.
        git_sha: Commit recorded on the history row.
    """
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

    with db._connect(db_path) as conn:
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

#: Re-exported so presentation layers (which may not import the db) can
#: type-check and catch the resolver's result / error.
Resolution = db.Resolution
UnresolvedReferenceError = db.UnresolvedReferenceError

#: ``detail`` levels accepted by :func:`render_report_text`.
REPORT_DETAILS = ("summary", "condensed", "full")

#: How many ``via`` containers the condensed staleness table lists before
#: folding the rest into ``+N others``.
CONDENSED_TOP_VIA = 10

_CONTENT_TYPES = {"INITIAL", "CONTENT_ONLY", "DESC_ONLY", "CONTENT_AND_DESC"}
_STALENESS_TYPES = {
    BECAME_CONTENT_UPDATED,
    BECAME_DESC_UPDATED,
    BECAME_NOT_FOUND,
    BECAME_RENAMED,
    BECAME_LINKED_STALE,
    BECAME_BROKEN_LINK,
    LINK_BECAME_VERIFIED,
    BECAME_VERIFIED,
}
_LINK_TYPES = {"LINK_ADDED", "LINK_REMOVED"}
_VERIFY_TYPES = {"AGENT_VERIFIED", "MANUAL_VERIFIED"}
# Recorded once per section edited outside the doc tools.  Reported in its
# own bucket; never a staleness input or a diff baseline.
_RAW_EDIT_TYPES = {RAW_DOCJSON_EDIT}
_VIA_TYPES = (BECAME_LINKED_STALE, LINK_BECAME_VERIFIED)


def _empty_summary() -> dict[str, int]:
    return {
        "nodes_changed": 0,
        "became_stale": 0,
        "became_clean": 0,
        "verified": 0,
        "agent_only": 0,
        "links_modified": 0,
        "raw_docjson_edits": 0,
    }


def compute_report(
    db_path: Path,
    *,
    since_sha: str | None = None,
    since_timestamp: str | None = None,
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
    exclude_node_pattern: str | list[str] | None = None,
    project_root: Path | None = None,
) -> ReportData:
    """Classify history rows since a reference point into a report payload.

    Shared by CLI ``axiom-graph report`` and MCP ``axiom_graph_report``.
    Returns a :class:`ReportData` carrying the resolved reference point,
    summary counters and the per-bucket lists; presentation layers format
    it with :func:`render_report_text` / :func:`report_to_dict`.

    Args:
        db_path: Path to the axiom-graph DB.
        since_sha: Git SHA prefix.  A SHA the index never recorded resolves
            to its git commit time (needs *project_root*).
        since_timestamp: ISO-8601 datetime cutoff.
        change_type_pattern: Glob pattern for change types.
        node_pattern: Glob pattern for node IDs.
        node_type: Filter to nodes of this type.
        exclude_node_pattern: Glob (or list of globs) for node IDs to drop
            from every bucket and from the headline counts.
        project_root: Repo root for the git commit-time fallback.

    Returns:
        :class:`ReportData`.  ``no_rows`` is True when no history exists
        after the reference point.  ``no_matches`` is True when the
        filter dropped every row.  ``resolution`` is always set.

    Raises:
        UnresolvedReferenceError: When an explicit *since_sha* matches
            neither the index nor git (or is ambiguous / too short).
    """
    resolution = db.resolve_since_cutoff(
        db_path,
        since_timestamp=since_timestamp,
        since_sha=since_sha,
        project_root=project_root,
    )
    rows = db.get_history_for_resolution(db_path, resolution)

    def _empty(**flags: bool) -> ReportData:
        return ReportData(
            summary=_empty_summary(),
            content_changes={},
            staleness_transitions=[],
            link_changes=[],
            verifications=[],
            human_verified_ids=set(),
            resolution=resolution,
            **flags,
        )

    if not rows:
        return _empty(no_rows=True)

    has_filter = change_type_pattern or node_pattern or node_type or db.normalize_patterns(exclude_node_pattern)
    if has_filter:
        nt_map = db.build_node_types_map(db_path) if node_type else None
        rows = db.filter_history_rows(
            rows,
            change_type_pattern=change_type_pattern,
            node_pattern=node_pattern,
            node_type=node_type,
            node_types_map=nt_map,
            exclude_node_pattern=exclude_node_pattern,
        )
        if not rows:
            return _empty(no_matches=True)

    content_changes: dict[str, list[dict]] = {}
    staleness_transitions: list[dict] = []
    link_changes: list[dict] = []
    verifications: list[dict] = []
    raw_docjson_edits: list[dict] = []

    for row in rows:
        ct = row["change_type"]
        if ct in _CONTENT_TYPES:
            content_changes.setdefault(row["node_id"], []).append(row)
        elif ct in _STALENESS_TYPES:
            staleness_transitions.append(row)
        elif ct in _LINK_TYPES:
            link_changes.append(row)
        elif ct in _VERIFY_TYPES:
            verifications.append(row)
        elif ct in _RAW_EDIT_TYPES:
            raw_docjson_edits.append(row)

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
        "raw_docjson_edits": len({r["node_id"] for r in raw_docjson_edits}),
    }

    return ReportData(
        summary=summary,
        content_changes=content_changes,
        staleness_transitions=staleness_transitions,
        link_changes=link_changes,
        verifications=verifications,
        human_verified_ids=human_verified_ids,
        resolution=resolution,
        raw_docjson_edits=raw_docjson_edits,
    )


# --- reference header -------------------------------------------------------


def format_reference(resolution: Resolution | None) -> str:
    """Return the one-line ``reference: ...`` header for a report.

    Names the resolved SHA (12 chars), the cutoff time and how the
    reference was resolved, so a report always says what it was measured
    against.

    Args:
        resolution: The report's :class:`Resolution` (``None`` is treated
            as "no reference").

    Returns:
        The header line (no trailing newline).
    """
    if resolution is None or resolution.source == db.SOURCE_NONE:
        return "reference: none — whole history (no checkpoint or SHA-bearing history row)"
    sha = resolution.sha[:12] if resolution.sha else "(no sha)"
    cutoff = resolution.cutoff
    source = resolution.source
    if source == db.SOURCE_CHECKPOINT:
        return f"reference: {sha} — checkpoint at {cutoff}"
    if source == db.SOURCE_BUILD:
        return f"reference: {sha} — end of its first indexed build at {cutoff}"
    if source == db.SOURCE_GIT:
        return f"reference: {sha} — git commit time {cutoff} (SHA not in index)"
    if source == db.SOURCE_TIMESTAMP:
        return f"reference: timestamp {cutoff}"
    if source == db.SOURCE_DEFAULT_CHECKPOINT:
        return f"reference: {sha} — latest checkpoint at {cutoff} (default; no reference given)"
    if source == db.SOURCE_DEFAULT_BUILD:
        return f"reference: {sha} — latest SHA-bearing history row at {cutoff} (default; no checkpoint exists)"
    return f"reference: unresolved — since_sha '{resolution.requested_sha}' {resolution.reason}"


def reference_to_dict(resolution: Resolution | None) -> dict:
    """Return the JSON ``reference`` object for a report.

    Args:
        resolution: The report's :class:`Resolution`.

    Returns:
        Dict with ``sha``, ``cutoff``, ``source``, ``requested_sha`` and the
        human-readable ``description`` (the text header).
    """
    if resolution is None:
        return {
            "sha": None,
            "cutoff": None,
            "source": db.SOURCE_NONE,
            "requested_sha": None,
            "description": format_reference(None),
        }
    return {
        "sha": resolution.sha,
        "cutoff": resolution.cutoff,
        "source": resolution.source,
        "requested_sha": resolution.requested_sha,
        "description": format_reference(resolution),
    }


def _row_to_json(row: dict) -> dict:
    out = {k: row.get(k) for k in ("node_id", "scanned_at", "change_type", "git_sha")}
    if row.get("meta"):
        try:
            out["meta"] = json.loads(row["meta"])
        except Exception as exc:
            logger.debug("report: could not parse meta JSON for %s: %s", row.get("node_id"), exc)
            out["meta"] = row["meta"]
    return out


def report_to_dict(data: ReportData) -> dict:
    """Return the JSON payload for a report: raw rows plus the reference.

    Args:
        data: Result of :func:`compute_report`.

    Returns:
        Dict with ``reference``, ``summary``, ``content_changes``,
        ``staleness_transitions``, ``link_changes`` and ``verifications``.
    """
    return {
        "reference": reference_to_dict(data.resolution),
        "summary": data.summary,
        "content_changes": {nid: [_row_to_json(r) for r in evts] for nid, evts in data.content_changes.items()},
        "staleness_transitions": [_row_to_json(r) for r in data.staleness_transitions],
        "link_changes": [_row_to_json(r) for r in data.link_changes],
        "verifications": [_row_to_json(r) for r in data.verifications],
        "raw_docjson_edits": [_row_to_json(r) for r in data.raw_docjson_edits],
    }


# --- condensed aggregation --------------------------------------------------


def report_container(node_id: str) -> str:
    """Return a node's container: the first two ``::`` segments of its id."""
    return "::".join(node_id.split("::")[:2])


def _meta(row: dict) -> dict:
    raw = row.get("meta")
    if not raw:
        return {}
    try:
        m = json.loads(raw)
    except Exception as exc:
        logger.debug("report: could not parse meta JSON for %s: %s", row.get("node_id"), exc)
        return {}
    return m if isinstance(m, dict) else {}


def _actor(row: dict) -> str | None:
    actor = _meta(row).get("actor")
    return actor if isinstance(actor, str) and actor else None


def is_actor_row(row: dict) -> bool:
    """True for a hand-made change: ``meta.actor`` set and not ``system`` / ``build:*``."""
    actor = _actor(row)
    return bool(actor) and actor != "system" and not actor.startswith("build:")


@dataclass
class CondensedStalenessType:
    """One change type in the condensed staleness table."""

    change_type: str
    rows: int
    nodes: int
    top_via: list[tuple[str, int]] = field(default_factory=list)
    other_via: int = 0


@dataclass
class CondensedReport:
    """Structured condensed view of a :class:`ReportData`.

    Attributes:
        content: ``(container, node_count, {change_type: rows})`` per
            container, sorted by container.
        content_actor_rows: Actor-authored content rows (printed verbatim).
        staleness: Per change type counts (+ top ``via`` containers for
            ``BECAME_LINKED_STALE`` / ``LINK_BECAME_VERIFIED``).
        staleness_actor_rows: Actor-authored staleness rows (verbatim).
        link_actor_rows: Actor-authored link rows (verbatim).
        link_system: ``(actor, change_type, target_container, count)`` for
            non-actor link rows, collapsed.
        verified_direct: ``(container, node_count, {change_type: rows})``
            for containers whose own nodes were verified.
        verified_cascade_only: ``(container, node_count)`` for containers
            that appear only through cascade retirement
            (``LINK_BECAME_VERIFIED``).
        verification_actor_rows: Actor-authored verification rows (verbatim).
        agent_only: Agent-verified nodes without a manual verification.
    """

    content: list[tuple[str, int, dict[str, int]]] = field(default_factory=list)
    content_actor_rows: list[dict] = field(default_factory=list)
    staleness: list[CondensedStalenessType] = field(default_factory=list)
    staleness_actor_rows: list[dict] = field(default_factory=list)
    link_actor_rows: list[dict] = field(default_factory=list)
    link_system: list[tuple[str, str, str, int]] = field(default_factory=list)
    verified_direct: list[tuple[str, int, dict[str, int]]] = field(default_factory=list)
    verified_cascade_only: list[tuple[str, int]] = field(default_factory=list)
    verification_actor_rows: list[dict] = field(default_factory=list)
    agent_only: int = 0


def _group_by_container(rows: list[dict]) -> list[tuple[str, int, dict[str, int]]]:
    nodes: dict[str, set[str]] = {}
    types: dict[str, Counter] = {}
    for r in rows:
        c = report_container(r["node_id"])
        nodes.setdefault(c, set()).add(r["node_id"])
        types.setdefault(c, Counter())[r["change_type"]] += 1
    return [(c, len(nodes[c]), dict(sorted(types[c].items()))) for c in sorted(nodes)]


def condense_report(data: ReportData) -> CondensedReport:
    """Aggregate a report into the fixed condensed shape.

    Actor-authored rows are kept verbatim; everything else rolls up by
    container (first two ``::`` segments of the node id).

    Args:
        data: Result of :func:`compute_report`.

    Returns:
        A :class:`CondensedReport`.
    """
    out = CondensedReport(agent_only=data.summary.get("agent_only", 0))

    content_rows = [r for evts in data.content_changes.values() for r in evts]
    out.content_actor_rows = [r for r in content_rows if is_actor_row(r)]
    out.content = _group_by_container([r for r in content_rows if not is_actor_row(r)])

    out.staleness_actor_rows = [r for r in data.staleness_transitions if is_actor_row(r)]
    by_type: dict[str, list[dict]] = {}
    for r in data.staleness_transitions:
        if not is_actor_row(r):
            by_type.setdefault(r["change_type"], []).append(r)
    for ct in sorted(by_type):
        rows = by_type[ct]
        entry = CondensedStalenessType(change_type=ct, rows=len(rows), nodes=len({r["node_id"] for r in rows}))
        if ct in _VIA_TYPES:
            via = Counter(
                report_container(_meta(r)["linked_node"])
                for r in rows
                if isinstance(_meta(r).get("linked_node"), str) and _meta(r)["linked_node"]
            )
            ranked = sorted(via.items(), key=lambda kv: (-kv[1], kv[0]))
            entry.top_via = ranked[:CONDENSED_TOP_VIA]
            entry.other_via = len(ranked) - len(entry.top_via)
        out.staleness.append(entry)

    out.link_actor_rows = [r for r in data.link_changes if is_actor_row(r)]
    system_counts: Counter = Counter()
    for r in data.link_changes:
        if is_actor_row(r):
            continue
        target = _meta(r).get("target") or ""
        system_counts[(_actor(r) or "system", r["change_type"], report_container(target) if target else "?")] += 1
    out.link_system = [(a, ct, c, n) for (a, ct, c), n in sorted(system_counts.items())]

    out.verification_actor_rows = [r for r in data.verifications if is_actor_row(r)]
    direct_rows = [r for r in data.verifications if not is_actor_row(r)]
    out.verified_direct = _group_by_container(direct_rows)
    direct_containers = {report_container(r["node_id"]) for r in data.verifications}
    cascade_nodes: dict[str, set[str]] = {}
    for r in data.staleness_transitions:
        if r["change_type"] == LINK_BECAME_VERIFIED:
            c = report_container(r["node_id"])
            if c not in direct_containers:
                cascade_nodes.setdefault(c, set()).add(r["node_id"])
    out.verified_cascade_only = [(c, len(cascade_nodes[c])) for c in sorted(cascade_nodes)]
    return out


# --- text rendering ---------------------------------------------------------


def _actor_tag(row: dict) -> str:
    actor = _actor(row)
    return f"  [{actor}]" if actor else ""


def _staleness_line(r: dict) -> str:
    m = _meta(r)
    parts = []
    if m.get("from"):
        parts.append(f"was {m['from']}")
    if m.get("linked_node"):
        parts.append(f"via {m['linked_node']}")
    meta_str = f"  ({', '.join(parts)})" if parts else ""
    return f"  {r['node_id']}  {r['change_type']}{meta_str}{_actor_tag(r)}"


def _link_line(r: dict) -> str:
    target = _meta(r).get("target", "")
    arrow = "→" if r["change_type"] == "LINK_ADDED" else "✕"
    return f"  {r['node_id']}  {arrow} {target}{_actor_tag(r)}"


def _verification_line(r: dict, human_verified_ids: set[str]) -> str:
    flag = " ⚠ agent-only" if (r["change_type"] == "AGENT_VERIFIED" and r["node_id"] not in human_verified_ids) else ""
    return f"  {r['node_id']}  {r['change_type']}{flag}{_actor_tag(r)}"


def _summary_line(summary: dict[str, int]) -> str:
    line = (
        f"{summary['nodes_changed']} nodes changed, "
        f"{summary['became_stale']} became stale, "
        f"{summary['verified']} verified ({summary['agent_only']} agent-only), "
        f"{summary['links_modified']} links modified"
    )
    if summary.get("raw_docjson_edits"):
        line += f", {summary['raw_docjson_edits']} raw DocJSON edits"
    return line


def _render_raw_docjson_edits(data: ReportData, lines: list[str], *, condensed: bool = False) -> None:
    if not data.raw_docjson_edits:
        return
    if not condensed:
        _section(lines, "RAW DOCJSON EDITS")
        lines.extend(
            f"  {r['node_id']}  RAW_DOCJSON_EDIT  (edited outside the doc tools){_actor_tag(r)}"
            for r in data.raw_docjson_edits
        )
        return
    _section(lines, "RAW DOCJSON EDITS (by container; edited outside the doc tools)")
    per_container: dict[str, set[str]] = {}
    for r in data.raw_docjson_edits:
        per_container.setdefault(report_container(r["node_id"]), set()).add(r["node_id"])
    lines.extend(f"  {c}  {_count(len(ids), 'section')}" for c, ids in sorted(per_container.items()))


def _section(lines: list[str], title: str) -> None:
    lines.append(f"\n{title}")
    lines.append("-" * 40)


def _counts(types: dict[str, int]) -> str:
    return ", ".join(f"{ct} {n}" for ct, n in types.items())


def _render_full(data: ReportData, lines: list[str]) -> None:
    if data.content_changes:
        _section(lines, "CONTENT CHANGES")
        for node_id, evts in sorted(data.content_changes.items()):
            types = ", ".join(sorted({e["change_type"] for e in evts}))
            lines.append(f"  {node_id}  [{types}]")
    if data.staleness_transitions:
        _section(lines, "STALENESS TRANSITIONS")
        lines.extend(_staleness_line(r) for r in data.staleness_transitions)
    if data.link_changes:
        _section(lines, "LINK CHANGES")
        lines.extend(_link_line(r) for r in data.link_changes)
    if data.verifications:
        _section(lines, "VERIFICATION ACTIVITY")
        lines.extend(_verification_line(r, data.human_verified_ids) for r in data.verifications)


def _count(n: int, noun: str) -> str:
    """Return ``"1 node"`` / ``"3 nodes"`` — *noun* is the singular form."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _render_condensed(data: ReportData, lines: list[str]) -> None:
    c = condense_report(data)
    if c.content or c.content_actor_rows:
        _section(lines, "CONTENT CHANGES (by container)")
        for container, n_nodes, types in c.content:
            lines.append(f"  {container}  {_count(n_nodes, 'node')}  [{_counts(types)}]")
        lines.extend(f"  {r['node_id']}  [{r['change_type']}]{_actor_tag(r)}" for r in c.content_actor_rows)
    if c.staleness or c.staleness_actor_rows:
        _section(lines, "STALENESS TRANSITIONS (by type)")
        for entry in c.staleness:
            lines.append(f"  {entry.change_type}  {_count(entry.rows, 'row')}, {_count(entry.nodes, 'node')}")
            if entry.top_via:
                lines.append("    via:")
                lines.extend(f"      {container}  {n}" for container, n in entry.top_via)
                if entry.other_via:
                    lines.append(f"      +{entry.other_via} others")
        lines.extend(_staleness_line(r) for r in c.staleness_actor_rows)
    if c.link_actor_rows or c.link_system:
        _section(lines, "LINK CHANGES")
        lines.extend(_link_line(r) for r in c.link_actor_rows)
        for actor, ct, container, n in c.link_system:
            what = "purges" if ct == "LINK_REMOVED" else "links added"
            lines.append(f"  [{actor}] {n} {what}  → {container}")
    if c.verified_direct or c.verified_cascade_only or c.verification_actor_rows:
        _section(lines, "VERIFICATION ACTIVITY")
        lines.append(f"  {_count(data.summary['verified'], 'node')} verified ({c.agent_only} agent-only)")
        if c.verified_direct:
            lines.append("  verified directly:")
            ranked = sorted(c.verified_direct, key=lambda e: (-e[1], e[0]))
            top = ranked[:CONDENSED_TOP_VIA]
            lines.extend(f"    {container}  {_count(n, 'node')}  [{_counts(types)}]" for container, n, types in top)
            if len(ranked) > len(top):
                rest = ranked[len(top) :]
                lines.append(
                    f"    +{len(rest)} other containers ({_count(sum(n for _, n, _ in rest), 'node')}; "
                    "the full report lists every row)"
                )
        lines.extend(_verification_line(r, data.human_verified_ids) for r in c.verification_actor_rows)
        if c.verified_cascade_only:
            lines.append("  cascade retirement only (LINK_BECAME_VERIFIED, no direct verification):")
            lines.extend(f"    {container}  {_count(n, 'node')}" for container, n in c.verified_cascade_only)


def render_report_text(data: ReportData, detail: str = "full") -> str:
    """Render a report as text — the one renderer CLI and MCP share.

    Every level starts with the ``reference:`` header (also on an empty
    window), then the headline counts.

    Args:
        data: Result of :func:`compute_report`.
        detail: ``summary`` (header + headline), ``condensed`` (aggregated
            by container; actor-authored rows verbatim) or ``full`` (one
            line per row — the CLI ``text`` format).

    Returns:
        The rendered report (no trailing newline).

    Raises:
        ValueError: On an unknown *detail*.
    """
    if detail not in REPORT_DETAILS:
        raise ValueError(f"detail must be one of {', '.join(REPORT_DETAILS)}; got {detail!r}")
    lines = [format_reference(data.resolution)]
    if data.no_rows:
        lines.append("No history events found after the reference point.")
        return "\n".join(lines)
    if data.no_matches:
        lines.append("No history events match the given filters.")
        return "\n".join(lines)
    headline = _summary_line(data.summary)
    lines.append(headline)
    if detail == "summary":
        return "\n".join(lines)
    lines.append("=" * len(headline))
    if detail == "full":
        _render_full(data, lines)
    else:
        _render_condensed(data, lines)
    _render_raw_docjson_edits(data, lines, condensed=detail == "condensed")
    return "\n".join(lines)


def cap_report_text(text: str, max_chars: int | None, detail: str | None = None) -> str:
    """Cap rendered report text at *max_chars*, cutting at a line boundary.

    Under the cap the text is returned unchanged.  Over it, whole lines are
    kept while they (plus the footer) fit, and a footer names how many
    lines were dropped and how to narrow the report.  If not even the first
    line fits, that line is hard-cut so the caller still gets the
    reference header's start.

    Args:
        text: Rendered report.
        max_chars: Character budget for the whole response, footer
            included; ``None`` disables the cap.
        detail: The detail level *text* was rendered at.  The footer
            only suggests ``detail="condensed"`` when the report is
            not already condensed.

    Returns:
        The (possibly truncated) text.
    """
    if max_chars is None or len(text) <= max_chars:
        return text
    lines = text.split("\n")

    narrow = "" if detail == "condensed" else 'detail="condensed", '

    def _footer(dropped: int) -> str:
        return (
            f"\n\n[report truncated: {dropped} more line(s) dropped to stay within {max_chars} chars. "
            f"Narrow it with {narrow}exclude_node_pattern=..., node_pattern / change_type_pattern, "
            "or run the CLI (axiom-graph report <root> --format condensed > report.txt); "
            "pass max_chars=None for the full response.]"
        )

    budget = max_chars - len(_footer(len(lines)))
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if not kept:
        kept = [lines[0][: max(budget, 80)] + "…"]
        return "\n".join(kept) + _footer(len(lines) - 1)
    return "\n".join(kept) + _footer(len(lines) - len(kept))


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
# carry-forward (the return trip of checkout)
# ---------------------------------------------------------------------------


class CarryForwardRefusedError(Exception):
    """:func:`carry_forward_verifications` refused before writing anything.

    Raised when the worktree index is missing, is this index, or cannot be
    compared with it (schema below v5, a different schema version, or a
    different project id).
    """


#: Report label of each reason a stale node is not carried.
CARRY_FORWARD_REASON_LABELS: dict[str, str] = {
    "not_verified": "not verified in the worktree",
    "content_differs": "content differs",
    "link_absent": "a link is absent in the worktree",
    "linked_node_differs": "a linked node differs",
    "structural": "NOT_FOUND, RENAMED or BROKEN_LINK here",
}

#: Report label of each reason a link of a partly carried node stays open.
CARRY_FORWARD_HELD_LABELS: dict[str, str] = {
    "no_receipt": "no receipt in the worktree",
    "link_absent": "absent in the worktree",
    "linked_node_differs": "differs",
}


@dataclass
class CarryForwardResult:
    """Result of :func:`carry_forward_verifications`.

    Attributes:
        dry_run: Nothing was written.
        worktree_db_path: The worktree index read.
        branch: The worktree's branch, or ``None`` (detached, or no git).
        sha: The worktree's HEAD SHA, or ``None``.
        carried: Nodes whose worktree verification was copied in full (on a
            dry run: would be copied), in stored node order.
        partial: Nodes carried in part (on a dry run: would be), in stored
            node order: the worktree verified exactly this content, and its
            own-text verification and/or the receipts of some links came
            across (see :attr:`partial_detail`).
        partial_detail: Node id -> ``(text, receipts, held)`` for each
            :attr:`partial` node: whether its own text carried, the link
            targets whose receipts carried, and ``(target, reason)`` for each
            link holding it stale here that stays open (reasons are the keys
            of :data:`CARRY_FORWARD_HELD_LABELS`).
        not_carried: Reason -> ``[(node_id, detail), ...]`` for the stale
            nodes that keep their status; ``detail`` names the blocking link
            target for the link reasons.  Reasons are the keys of
            :data:`CARRY_FORWARD_REASON_LABELS`; empty reasons are absent.
        follows: Nodes stale only through what they inherit (a module, an
            envelope whose composed children are stale) or a transitive
            doc-to-doc link; they settle when those do.
        carried_still_stale: Nodes carried in full that the recompute still
            finds not VERIFIED, in ``carried`` order: held from a target's own
            status until it settles (a section's transitive doc-to-doc link to
            a stale section, or an envelope's annotated function or delegated
            task that is itself stale).  Always empty on a dry run.
        partial_settled: Nodes carried in part that the recompute finds
            VERIFIED in both dimensions, in ``partial`` order.  Always empty
            on a dry run.
        before_stale: Nodes not VERIFIED in either dimension when the call
            started (after refreshing this index's statuses; on a dry run,
            as this index's last build or check stored them).
        after_stale: The same count after the carry and its recompute;
            ``None`` on a dry run.
        summary_line: The ``check`` summary line after the carry (on a dry
            run: counted from the stored statuses, without a refresh).
    """

    dry_run: bool
    worktree_db_path: Path
    branch: str | None
    sha: str | None
    carried: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    partial_detail: dict[str, tuple[bool, list[str], list[tuple[str, str]]]] = field(default_factory=dict)
    not_carried: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    follows: list[str] = field(default_factory=list)
    carried_still_stale: list[str] = field(default_factory=list)
    partial_settled: list[str] = field(default_factory=list)
    before_stale: int = 0
    after_stale: int | None = None
    summary_line: str | None = None

    @property
    def not_carried_count(self) -> int:
        """Number of stale nodes not carried (``follows`` excluded)."""
        return sum(len(rows) for rows in self.not_carried.values())


def _carry_reason(source: str, provenance) -> str:
    """The reason a carry stores: the source, then the worktree's latest verification's verifier and reason."""
    reason = f"[carry_forward:{source}] verified by {provenance.verified_by}"
    return f"{reason}: {provenance.reason}" if provenance.reason else reason


def _carried_from(branch: str | None, sha: str | None, provenance) -> dict[str, str | None]:
    """A carry's provenance meta: branch, SHA and the worktree's latest verification of the node."""
    return {
        "branch": branch,
        "sha": sha,
        "verified_by": provenance.verified_by,
        "verified_at": provenance.verified_at,
        "verification_op": provenance.op,
    }


def _resolve_worktree_db(worktree: Path) -> tuple[Path, Path]:
    """Return ``(worktree_db, worktree_root)`` for a worktree directory or a path to its index file."""
    path = Path(worktree).resolve()
    if path.is_dir():
        return db_path_for(path), path
    # A path to the index file: <root>/.axiom_graph/graph.db.
    return path, path.parent.parent


@workflow(
    purpose="Carry a merged worktree's verifications into this index: refuse indexes that cannot be compared, refresh "
    "this index (not on a dry run, which writes nothing), judge every stale node against the worktree index "
    "read-only, copy each qualifying verification in full with its recorded pairs or in part (own text and/or the "
    "receipts that match here), settle the links with one recompute, and note what is still stale",
    inputs="this index's db path and root, the worktree directory or its index file, dry_run flag",
    outputs="CarryForwardResult: nodes carried in full and in part, the reason each other stale node keeps its "
    "status, counts before and after, the carried nodes still stale",
)
@db.connection_scope
def carry_forward_verifications(
    db_path: Path,
    root: Path,
    worktree: Path,
    *,
    dry_run: bool = False,
) -> CarryForwardResult:
    """Copy the verifications a merged worktree made into this index.

    Run from the checkout a worktree was merged into, after its build.  For
    every node not VERIFIED here, the worktree's verification is copied in
    full when the worktree verified exactly what this index now holds
    (:mod:`axiom_graph.index.carry_forward` has the rule): the node is
    VERIFIED there with a verification record, its own content is the same
    in both indexes, and every link a verification settles (by recorded
    pair or by time) exists there with each linked node at the version the
    worktree verified it against.  Code, test and doc nodes alike.

    A node that fails that rule but that the worktree verified at this same
    content (own VERIFIED there, same own content) is carried one dimension
    at a time: its own-text verification when its own status drifted here,
    and the worktree's receipt for each link holding it stale here whose
    linked node is at the version the receipt names.  Every other link
    keeps this index's state.

    Each node carried in full gets one verification record, written at its
    hashes here on this index's clock, with every pair the worktree
    recorded; a node carried in part gets a text verification and/or the
    carried receipts, leaving ``verified_at`` and its other receipts as they
    were.  Either way one preserved history row with op ``carry_forward``
    names the branch, the SHA and the worktree's latest verification of the
    node (a doc tool's write included).  The worktree's history is not
    imported.  One recompute (the ``check`` refresh) then settles the
    links through the pair rule; for an envelope the rule covers every
    annotated function and delegated task.  Two holds come from a target's
    own status instead: an envelope stays stale while an annotated function
    or delegated task is itself stale, and a section through a transitive
    doc-to-doc link to a stale section (the one link outside the rule).  A
    carried node held either way stays stale until that target settles, and
    :attr:`CarryForwardResult.carried_still_stale` lists it.

    The worktree index is opened read-only, once.  An index there that is
    behind its files only makes fewer nodes carry: its stored hashes are
    older, so they do not match.

    Args:
        db_path: This index.
        root: This checkout's root.
        worktree: The worktree directory, or its ``.axiom_graph/graph.db``.
        dry_run: Judge every stale node and write nothing to either index.
            This index's statuses are not refreshed: the dry run reads them
            as this index's last build or check stored them, so run it after
            this checkout's build.

    Returns:
        :class:`CarryForwardResult`.

    Raises:
        CarryForwardRefusedError: The worktree index is missing or is this
            index, or the two indexes differ in schema version or project id,
            or either is below schema v5 (no recorded pairs).
    """
    from axiom_graph.index.carry_forward import CARRY, CARRY_PARTIAL, FOLLOWS, index_identity_conn, plan_carry_conn
    from axiom_graph.index.git_utils import get_git_branch, get_git_sha
    from axiom_graph.index.mark_clean import carry_partial_verification_conn, carry_verification_conn
    from axiom_graph.index.staleness import _get_linked_stale_ids

    口 = Step(
        step_num=1,
        name="Refuse indexes that cannot be compared",
        purpose="The worktree index must exist, be another file, and match this index's schema version (v5 or later) "
        "and project id; checked before anything is written",
    )
    wt_db, wt_root = _resolve_worktree_db(worktree)
    if not wt_db.is_file():
        raise CarryForwardRefusedError(f"no axiom-graph index at {wt_db}; build the worktree first")
    if os.path.normcase(str(wt_db)) == os.path.normcase(str(Path(db_path).resolve())):
        raise CarryForwardRefusedError(f"{wt_db} is this checkout's own index")
    wt_conn = db.open_connection(wt_db, read_only=True)
    try:
        wt_version, wt_project = index_identity_conn(wt_conn)
        with db._connect(db_path) as conn:
            version, project = index_identity_conn(conn)
        if version != wt_version:
            raise CarryForwardRefusedError(
                f"schema versions differ: this index is v{version}, the worktree index is v{wt_version}; "
                "build both with the same axiom-graph version"
            )
        if version < db.PAIRS_SCHEMA_VERSION:
            raise CarryForwardRefusedError(
                f"both indexes are at schema v{version}; carry-forward needs v{db.PAIRS_SCHEMA_VERSION} "
                "(recorded pairs): run a build in each"
            )
        if project != wt_project:
            raise CarryForwardRefusedError(
                f"project ids differ: this index is {project!r}, the worktree index is {wt_project!r}"
            )

        if not dry_run:
            # A dry run writes nothing, so it skips this refresh (which saves
            # statuses, live hashes, history rows and the watermark) and
            # judges the statuses this index's last build or check stored.
            口 = AutoStep(step_num=2, name="Refresh this index's statuses")
            compute_check_summary(db_path, root)

        口 = Step(
            step_num=3,
            name="List this index's stale nodes",
            purpose="The nodes not VERIFIED and the vias holding each LINKED_STALE one, read from the stored statuses: "
            "just refreshed, or on a dry run as this index's last build or check stored them",
        )
        config = AxiomGraphConfig.load(root)
        with db._connect(db_path) as conn:
            stale = db.get_ordered_staleness_conn(conn, problems_only=True)
        linked = {nid for nid, _own, link in stale if link == LINKED_STALE}
        vias = (
            _get_linked_stale_ids(
                db_path,
                transitive_tags=config.staleness.transitive_tags,
                frozen_tags=config.staleness.frozen_tags,
                scope=linked,
            )
            if linked
            else {}
        )

        with db._connect(db_path) as conn:
            口 = AutoStep(step_num=4, name="Judge every stale node against the worktree")
            plan = plan_carry_conn(conn, wt_conn, stale, vias)
    finally:
        wt_conn.close()

    branch = get_git_branch(wt_root)
    sha = get_git_sha(wt_root)
    result = CarryForwardResult(
        dry_run=dry_run, worktree_db_path=wt_db, branch=branch, sha=sha, before_stale=len(stale)
    )
    for part in plan.partial:
        result.partial_detail[part.node_id] = (part.text, sorted(part.receipts), list(part.held))
    for nid, (verdict, detail) in plan.verdicts.items():
        if verdict == CARRY:
            result.carried.append(nid)
        elif verdict == CARRY_PARTIAL:
            result.partial.append(nid)
        elif verdict == FOLLOWS:
            result.follows.append(nid)
        else:
            result.not_carried.setdefault(verdict, []).append((nid, detail))
    if dry_run:
        # Counted from the stored statuses, without the refresh check runs.
        result.summary_line = _summary_from_store(db_path, root, config, include_frozen=False).summary_line()
        return result

    口 = Step(
        step_num=5,
        name="Copy the qualifying verifications",
        purpose="Full carries: one verification record per node at its hashes here, with every pair the worktree "
        "recorded.  Partial carries: the own-text verification and/or the receipts that match here, verified_at and "
        "other receipts untouched.  Each with one carry_forward history row naming branch, SHA and the worktree's "
        "latest verification; one transaction",
    )
    source = f"{branch or wt_root.name}@{sha[:12] if sha else 'unknown'}"
    with db._connect(db_path) as conn:
        for row in plan.carry:
            口 = AutoStep(step_num=5.1, name="Copy one verification in full")
            carry_verification_conn(
                conn,
                row.node_id,
                row.code_hash,
                row.desc_hash,
                verified_by=row.verified_by,
                reason=_carry_reason(source, row.provenance),
                pairs=row.pairs,
                carried_from=_carried_from(branch, sha, row.provenance),
            )
        for part in plan.partial:
            口 = AutoStep(step_num=5.2, name="Copy one verification in part")
            carry_partial_verification_conn(
                conn,
                part.node_id,
                part.code_hash,
                part.desc_hash,
                text=part.text,
                receipts=part.receipts,
                open_targets=part.open_targets,
                has_row=part.has_row,
                verified_by=part.verified_by,
                reason=_carry_reason(source, part.provenance),
                carried_from=_carried_from(branch, sha, part.provenance),
            )

    口 = AutoStep(step_num=6, name="Settle the links with one recompute")
    cs = compute_check_summary(db_path, root)

    口 = Step(
        step_num=7,
        name="Count what is still stale",
        purpose="The stale count after the carry; the nodes carried in full still not VERIFIED (held from a target's "
        "own status: a transitive doc-to-doc link, or an envelope's annotated function or delegated task still "
        "stale, until it settles); the nodes carried in part that are now VERIFIED",
    )
    result.summary_line = cs.summary_line() if cs is not None else None
    with db._connect(db_path) as conn:
        after = db.get_ordered_staleness_conn(conn, problems_only=True)
    result.after_stale = len(after)
    still_stale = {nid for nid, _own, _link in after}
    result.carried_still_stale = [nid for nid in result.carried if nid in still_stale]
    result.partial_settled = [nid for nid in result.partial if nid not in still_stale]
    return result


def render_carry_forward_report(result: CarryForwardResult, *, list_nodes: bool = False) -> str:
    """Render a :class:`CarryForwardResult` as the text the CLI command and the MCP tool print.

    A dry run also lists the nodes it would carry, in full and in part,
    without *list_nodes*, so the preview names what the real run writes
    without listing every stale node.

    Args:
        result: The carry's result.
        list_nodes: Add one line per node, grouped by verdict.

    Returns:
        A few summary lines (counts, how many carried nodes stay stale when
        any do, reasons, the check summary line), then the would-carry lists
        on a dry run, or every per-node group when *list_nodes* is set.
    """
    source = f"{result.branch or '(no branch)'} @ {result.sha[:12] if result.sha else 'unknown'}"
    n = len(result.carried)
    m = len(result.partial)
    counted = f"{n} verification(s) in full and {m} in part" if m else f"{n} verification(s)"
    if result.dry_run:
        lines = [
            f"Dry run: would carry {counted} from {source}; nothing written.",
            f"Stale here: {result.before_stale} (statuses as this index's last build or check stored them).",
        ]
    else:
        lines = [
            f"Carried {counted} from {source}.",
            f"Stale here: {result.before_stale} before -> {result.after_stale} after.",
        ]
    if result.carried_still_stale:
        lines.append(
            f"{len(result.carried_still_stale)} carried node(s) stay stale until nodes they depend on settle "
            "(a doc-to-doc link, or an annotated function or delegated task still stale)."
        )
    if m and not result.dry_run:
        lines.append(
            f"{len(result.partial_settled)} of the {m} carried in part now read VERIFIED; the rest stay stale "
            "through what did not carry (a link the worktree did not verify at this version) or until nodes they "
            "depend on settle."
        )
    if result.not_carried:
        counts = ", ".join(
            f"{len(result.not_carried[reason])} {label}"
            for reason, label in CARRY_FORWARD_REASON_LABELS.items()
            if reason in result.not_carried
        )
        lines.append(f"Not carried ({result.not_carried_count}): {counts}.")
    if result.follows:
        lines.append(f"Stale only through their children or linked sections ({len(result.follows)}).")
    if result.summary_line:
        lines.append(result.summary_line)
    heading = "Would carry" if result.dry_run else "Carried"
    carry_groups: list[tuple[str, list[str]]] = [
        (heading, result.carried),
        (f"{heading} in part", [_partial_line(nid, result.partial_detail.get(nid)) for nid in result.partial]),
    ]
    if result.dry_run and not list_nodes:
        _append_groups(lines, carry_groups)
    if list_nodes:
        groups = [
            *carry_groups,
            ("Carried, still stale until nodes they depend on settle", result.carried_still_stale),
        ]
        for reason, label in CARRY_FORWARD_REASON_LABELS.items():
            rows = result.not_carried.get(reason, [])
            groups.append(
                (f"Not carried, {label}", [f"{nid} (via {detail})" if detail else nid for nid, detail in rows])
            )
        groups.append(("Stale only through their children or linked sections", result.follows))
        _append_groups(lines, groups)
    return "\n".join(lines)


def _partial_line(nid: str, detail: tuple[bool, list[str], list[tuple[str, str]]] | None) -> str:
    """One partly carried node: what came across (text, receipts) and which links stay open, and why."""
    if detail is None:
        return nid
    text, receipts, held = detail
    parts = []
    if text:
        parts.append("own text")
    if receipts:
        parts.append(f"receipts: {', '.join(receipts)}")
    if held:
        parts.append(
            "open: "
            + ", ".join(f"{target} ({CARRY_FORWARD_HELD_LABELS.get(reason, reason)})" for target, reason in held)
        )
    return f"{nid} ({'; '.join(parts)})" if parts else nid


def _append_groups(lines: list[str], groups: list[tuple[str, list[str]]]) -> None:
    """Append each non-empty ``(title, members)`` group as a titled, counted bullet list."""
    for title, members in groups:
        if members:
            lines.append("")
            lines.append(f"{title} ({len(members)}):")
            lines.extend(f"- {member}" for member in members)


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

# Per status dimension: the transitions that open a stale stretch, and the rows
# that close one.  A verification closes both; a status that heals by itself
# closes only its own dimension (a reverify cascade writes LINK_BECAME_VERIFIED).
_STALE_STRETCH_DIMENSIONS = (
    (
        frozenset({BECAME_CONTENT_UPDATED, BECAME_DESC_UPDATED}),
        frozenset({"AGENT_VERIFIED", "MANUAL_VERIFIED", BECAME_VERIFIED}),
    ),
    (
        frozenset({BECAME_LINKED_STALE}),
        frozenset({"AGENT_VERIFIED", "MANUAL_VERIFIED", LINK_BECAME_VERIFIED}),
    ),
)


def _open_stretch_start(rows: list[dict], opens: frozenset[str], closes: frozenset[str]) -> int | None:
    """Return the index of the first row of the stretch still open in one dimension, or ``None``.

    Args:
        rows: The node's history rows, newest first.
        opens: Change types that open a stale stretch in the dimension.
        closes: Change types that close one.

    Returns:
        The index of the oldest opening row newer than the newest closing
        row, or ``None`` when the dimension has no open stretch.
    """
    start: int | None = None
    for index, row in enumerate(rows):
        if row["change_type"] in closes:
            break
        if row["change_type"] in opens:
            start = index
    return start


def _open_stale_stretch_start(rows: list[dict]) -> int | None:
    """Return the index of the row that began the node's open stale stretch, or ``None``.

    With an open stretch in both dimensions (own and link) it is the start
    of the older stretch: the larger index, since *rows* are newest first.

    Args:
        rows: The node's history rows, newest first.

    Returns:
        The index into *rows*, or ``None`` when the node is in no stale stretch.
    """
    starts = [_open_stretch_start(rows, opens, closes) for opens, closes in _STALE_STRETCH_DIMENSIONS]
    open_starts = [start for start in starts if start is not None]
    return max(open_starts) if open_starts else None


#: The most commits of a file the diff fallback looks through for one that holds the node.
_BASELINE_WALK_LIMIT = 20


def _commit_holding_node(
    project_root: Path,
    node,
    git_path: str,
    candidate_ids: list[str],
    baseline_hashes: set[tuple[str | None, str | None]],
    skip_sha: str,
) -> tuple[str, str | None, str, str] | None:
    """Return the newest commit whose copy of the node is the text the node went stale from.

    Looks through the newest :data:`_BASELINE_WALK_LIMIT` commits of the
    node's file (following renames), skipping *skip_sha* (already found not
    to hold the node).  A commit qualifies when the indexer's scan of its copy
    holds the node with a ``(code_hash, desc_hash)`` pair in
    *baseline_hashes*, so a commit that already holds the edit never does.

    Args:
        project_root: Root of the git repository.
        node: The indexed node being diffed.
        git_path: The node's current repo-relative file path.
        candidate_ids: Ids to find the node by (see :func:`_baseline_candidate_ids`).
        baseline_hashes: The hash pairs the node's own status compares
            against: its stored hashes, and its latest verification's.
        skip_sha: A commit not to look at again.

    Returns:
        ``(sha, path_at_sha, node_text, commit_date)`` or ``None`` when no such
        commit is found.
    """
    root = Path(project_root)
    project_id = node.id.split("::")[0]
    out = _run_git(["log", "--follow", f"-n{_BASELINE_WALK_LIMIT}", "--format=%H %cI", "--", git_path], root)
    for line in (out or "").splitlines():
        sha, _, commit_date = line.partition(" ")
        if not sha or sha == skip_sha:
            continue
        at_commit = read_file_at_baseline(root, sha, git_path)
        if at_commit.status != "found":
            continue
        scan = scan_blob_at_location(at_commit.content or "", root, git_path, project_id)
        scanned = next((scan.nodes[cid] for cid in candidate_ids if cid in scan.nodes), None)
        if scanned is None or (scanned.code_hash, scanned.desc_hash) not in baseline_hashes:
            continue
        status, text = _locate_node_in_content(at_commit.content or "", node, project_root, git_path, candidate_ids)
        if status == "found":
            return sha, at_commit.path, text, commit_date
    return None


def _default_baseline_row(rows: list[dict]) -> tuple[dict | None, str | None, str | None]:
    """Pick the history row a diff compares against when no baseline is given.

    A node in a stale stretch -- own (``BECAME_CONTENT_UPDATED`` /
    ``BECAME_DESC_UPDATED`` with no verification or ``BECAME_VERIFIED``
    after it) or link (``BECAME_LINKED_STALE`` with no verification or
    ``LINK_BECAME_VERIFIED`` after it) -- diffs against the newest row with a
    git SHA recorded *before* the older of its open stretches began, so a
    checkpoint stamped while the node was already stale -- a commit that
    already holds the change -- is never the baseline.  Any other node diffs
    against its newest verified or checkpoint row with a SHA, else its oldest
    row with one.

    Args:
        rows: The node's history rows, newest first.

    Returns:
        ``(row, reason, None)`` naming the chosen row and why, or
        ``(None, None, failure)`` when no row qualifies.
    """
    stretch_start = _open_stale_stretch_start(rows)
    if stretch_start is not None:
        went_stale = rows[stretch_start]
        status = went_stale["change_type"].removeprefix("BECAME_")
        for row in rows[stretch_start + 1 :]:
            if row.get("git_sha"):
                return row, f"last commit recorded before the node went {status} at {went_stale['scanned_at']}", None
        return (
            None,
            None,
            f"Node went {status} at {went_stale['scanned_at']} and no earlier history entry has a git SHA",
        )
    for row in rows:
        if row.get("git_sha") and row["change_type"] in _BASELINE_CHANGE_TYPES:
            return row, f"newest {row['change_type']} entry with a git SHA", None
    for row in reversed(rows):
        if row.get("git_sha"):
            return row, "oldest history entry with a git SHA", None
    return None, None, "No history entry with a git SHA"


def _parse_level3(level_3_location: str | None) -> tuple[str | None, int | None, int | None]:
    """Parse ``level_3_location`` into ``(file_path, start_line, end_line)``.

    The file path is everything before the first ``#``.  Only an
    ``L<n>[-L<m>]`` fragment is a line range; any other fragment (a Markdown
    section's slug, as in ``docs/notes.md#alpha``) names a position in the
    file and carries no lines.

    Returns ``(None, None, None)`` when *level_3_location* is falsy.
    """
    if not level_3_location:
        return None, None, None
    file_part, _, fragment = level_3_location.partition("#")
    if not file_part:
        return None, None, None
    m = re.fullmatch(r"L(\d+)(?:-L?(\d+))?", fragment)
    if not m:
        return file_part, None, None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return file_part, start, end


def _slice_lines(content: str, start: int | None, end: int | None) -> str:
    """Return the line-range slice of *content* (1-based, inclusive)."""
    if start is None:
        return content
    lines = content.splitlines()
    return "\n".join(lines[start - 1 : end])


_POSITION_UNRESOLVED = "node_position_unresolved"


def _split_node_id(node_id: str) -> tuple[str, str | None]:
    """Split a node id into its file prefix and its within-file part.

    Ids are ``{project}::{module dotpath or doc id}::{within-file part}``; the
    within-file part may itself contain ``::`` (step nodes such as
    ``fn::step-3``) or ``@workflow`` (envelopes).

    Args:
        node_id: The node id to split.

    Returns:
        ``(file_prefix, within_file)``; ``within_file`` is ``None`` for a
        file-level id (module, doc envelope).
    """
    parts = node_id.split("::")
    if len(parts) <= 2:
        return node_id, None
    return "::".join(parts[:2]), "::".join(parts[2:])


def _prior_node_ids(db_path: Path, node_id: str) -> list[str]:
    """Return every id *node_id* was renamed from, transitively, newest first.

    Walks the ``node_renames`` ledger backwards (``new_id`` -> ``old_id``),
    breadth first, newest rename first at each hop.  Cycle-safe.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        node_id: The node's current id.

    Returns:
        Prior ids, nearest rename first; empty when the node was never renamed.
    """
    chain: list[str] = []
    seen = {node_id}
    frontier = [node_id]
    with db._connect(db_path) as conn:
        while frontier:
            current = frontier.pop(0)
            rows = conn.execute(
                "SELECT old_id FROM node_renames WHERE new_id = ? ORDER BY renamed_at DESC",
                (current,),
            ).fetchall()
            for row in rows:
                old_id = row["old_id"]
                if old_id not in seen:
                    seen.add(old_id)
                    chain.append(old_id)
                    frontier.append(old_id)
    return chain


def _baseline_candidate_ids(node_id: str, prior_ids: list[str]) -> list[str]:
    """Return the ids to look for in the baseline scan, in priority order.

    The node's current id first, then each prior id rebased onto the current
    file prefix by its within-file part: the baseline is scanned at the node's
    current path, so a file move is already absorbed and only the within-file
    part of an old id can differ.

    Args:
        node_id: The node's current id.
        prior_ids: Ids from :func:`_prior_node_ids`, nearest first.

    Returns:
        De-duplicated candidate ids.
    """
    prefix, _ = _split_node_id(node_id)
    candidates = [node_id]
    for old_id in prior_ids:
        _, within = _split_node_id(old_id)
        if within is None:
            continue
        rebased = f"{prefix}::{within}"
        if rebased not in candidates:
            candidates.append(rebased)
    return candidates


def _section_doc_id(node) -> str | None:
    """Return the id of the doc a section node belongs to, or ``None`` for a non-section.

    A DocJSON section (subtype ``docjson_section``) belongs to the doc its id
    prefix names.  A Markdown H2 section is an ``atomic_process`` carrying
    the ``docjson`` subtype it shares with every Markdown node, and attaches
    to its document with :data:`doc_ids.MARKDOWN_SECTION_SEP`.

    Args:
        node: The indexed node.

    Returns:
        The owning doc's id, or ``None`` when *node* is not a doc section.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    subtype = getattr(node, "subtype", None)
    if subtype == "docjson_section":
        return _split_node_id(node.id)[0]
    if subtype == "docjson" and node.node_type == "atomic_process" and doc_ids.MARKDOWN_SECTION_SEP in node.id:
        return node.id.partition(doc_ids.MARKDOWN_SECTION_SEP)[0]
    return None


def _locate_node_in_content(
    content: str,
    node,
    project_root: Path,
    location: str,
    candidate_ids: list[str],
) -> tuple[str, str]:
    """Find the node in one side's file content by identity and return its text.

    * A DocJSON section is found by its id in the scanned doc and rendered as
      its own heading and content (not its subsections).  A Markdown H2
      section is found and rendered the same way, as its heading and body.
    * A node with no line range (module, doc envelope) is the whole file.
    * A ranged code node is found by id in a scan of the content with the
      indexer's own scanner, and sliced at the range that scan reports.

    Args:
        content: The file content for this side.
        node: The indexed node being diffed.
        project_root: Project root (config source for the scan).
        location: The node's current repo-relative file path.
        candidate_ids: Ids to try, in order (see :func:`_baseline_candidate_ids`).

    Returns:
        ``(status, value)``: ``("found", text)``, ``("absent", "")`` when a
        clean scan does not contain the node, or ``("unresolved", reason)``.
    """
    section_doc_id = _section_doc_id(node)
    is_section = section_doc_id is not None
    _, start, _ = _parse_level3(node.level_3_location)
    if not is_section and start is None:
        return "found", content

    project_id = node.id.split("::")[0]
    scan = scan_blob_at_location(content, Path(project_root), location, project_id)
    if scan.error is not None:
        return "unresolved", scan.error

    if is_section:
        prefix = section_doc_id
        if prefix not in scan.nodes:
            return "unresolved", f"the file scans as a different doc than '{prefix}'"

    for cid in candidate_ids:
        sn = scan.nodes.get(cid)
        if sn is None:
            continue
        if is_section:
            return "found", f"{sn.title}\n\n{sn.level_2 or ''}"
        _, sn_start, sn_end = _parse_level3(sn.level_3_location)
        if sn_start is None:
            return "unresolved", f"the scan reports no line range for '{cid}'"
        return "found", _slice_lines(content, sn_start, sn_end)

    if scan.partial:
        return "unresolved", "the file parsed with errors and the node was not found in it"
    return "absent", ""


@task(
    purpose="Return old vs new source for a code node relative to a baseline commit",
    inputs="db_path, project_root, node_id, optional baseline_sha",
    outputs="dict with old_content, new_content, path, baseline_path, baseline_sha, baseline_date, "
    "baseline_reason, commit context",
)
def get_node_diff(
    db_path: Path,
    project_root: Path,
    node_id: str,
    baseline_sha: str | None = None,
) -> dict:
    """Return old vs new source for *node_id* relative to a baseline.

    **Baseline resolution** (when *baseline_sha* is ``None``):

    1. A node in a stale stretch -- a ``BECAME_CONTENT_UPDATED`` /
       ``BECAME_DESC_UPDATED`` row with no verification or
       ``BECAME_VERIFIED`` after it, or a ``BECAME_LINKED_STALE`` row with
       no verification or ``LINK_BECAME_VERIFIED`` after it -- diffs
       against the newest row with a ``git_sha`` recorded *before* the
       stretch began.  A checkpoint
       stamped while the node was already stale holds the change, so it is
       never the baseline.  No such row is a ``no_baseline`` error.  A row
       records git HEAD even when the node was not committed yet, so when the
       node is absent at that commit the baseline is the newest of the
       file's last 20 commits whose copy of the node hashes to the text it
       went stale from (its stored hashes or its latest verification's), and
       ``baseline_reason`` says so; ``baseline_date`` is then that commit's
       date.  A commit that already holds the edit never qualifies.  With
       none, the result is ``no_baseline`` with the reason, never an all-new
       or an unchanged diff.
    2. Any other node: the newest verified/checkpoint row with a non-NULL
       ``git_sha``, else the *oldest* row that has one (typically the
       ``INITIAL`` scan row).

    ``baseline_reason`` names the rule that picked the baseline.  When
    *baseline_sha* is provided it is used directly -- no history lookup --
    and ``baseline_reason`` is ``"given"``.

    **Renames.** The old side is read from wherever the file lived at the
    baseline: a file renamed or moved since then (committed, or staged in the
    index) diffs against its old path, via
    :func:`axiom_graph.index.git_utils.read_file_at_baseline`.  A file that did
    not exist at the baseline has an empty old side; a file whose old path git
    cannot resolve is an error, never an empty diff.

    **Locating the node.** Each side is cut at the node's own position *in
    that side's content*, found by identity -- never at the indexed line
    range, which goes wrong as soon as lines above the node change.  A code
    node (Python, JS/TS) is found by id in a scan of the content with the
    indexer's own scanner, run as if the content lived at the node's current
    path; the baseline side falls back to the node's prior ids from the
    rename ledger, rebased onto the current file.  A DocJSON section is found
    by its dot-path and diffs as its own heading and content, without its
    subsections; a Markdown H2 section is found by its slug and diffs as its
    own heading and body.  A node with no line range (module, doc envelope) diffs as
    the whole file.  A node absent from a cleanly scanned side has an empty
    side (e.g. a function added since the baseline).  When a side cannot be
    scanned -- it does not parse, the scanner is not installed, the file kind
    has no identity scan, or a partial parse lacks the node -- the result is
    a ``node_position_unresolved`` error, never a mis-sliced diff.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Root of the git repository the node's path is relative to.
        node_id: The node to diff.
        baseline_sha: Commit to diff against; resolved from history when ``None``.

    Returns:
        On success: ``{old_content, new_content, path, baseline_path,
        baseline_sha, baseline_date, baseline_reason, commit_subject,
        commit_author, commit_date}`` -- ``path`` is the node's current file and
        ``baseline_path`` the file it was read from at the baseline
        (``None`` when the file is new since then).  ``baseline_date`` is
        the chosen history row's time, or the commit's date when the
        stale-node fallback picked the baseline.
        On failure: ``{error, reason}`` with ``error`` either
        ``"no_baseline"`` (no node, location, baseline, or git failed;
        includes a stale node with no SHA row from before it went stale, or
        that none of its file's last 20 commits holds as it was before then),
        ``"baseline_path_unresolved"`` (the file is missing at the baseline
        and git cannot tell whether it was renamed) or
        ``"node_position_unresolved"`` (the node's position in the baseline
        or current file cannot be determined; ``reason`` names the side and
        the cause).
    """
    口 = Step(
        step_num=1,
        name="Look up node and parse location",
        purpose="Get the node's current source file path from level_3_location (its line range is not used to cut the diff)",
    )
    node = db.get_node(db_path, node_id)
    if node is None:
        return {"error": "no_baseline", "reason": f"Node not found: {node_id}"}

    file_path, _, _ = _parse_level3(node.level_3_location)
    if file_path is None:
        file_path = node.location
    if not file_path:
        return {"error": "no_baseline", "reason": "Node has no source location"}

    口 = Step(
        step_num=2,
        name="Resolve baseline SHA",
        purpose=(
            "Find the git commit to diff against -- for a node in a stale stretch, the last SHA row before it "
            "went stale; otherwise prefer verified/checkpoint, fall back to oldest SHA"
        ),
        critical="A checkpoint stamped while the node was already stale is never the baseline: it holds the change",
    )
    baseline_date: str | None = None
    baseline_reason = "given"
    went_stale: dict | None = None

    if baseline_sha is not None:
        rows = db.get_history(db_path, node_id, limit=100)
        for row in rows:
            if row.get("git_sha") == baseline_sha:
                baseline_date = row["scanned_at"]
                break
    else:
        rows = db.get_history(db_path, node_id, limit=100)
        baseline_row, baseline_reason, failure = _default_baseline_row(rows)
        if baseline_row is None:
            return {"error": "no_baseline", "reason": failure}
        baseline_sha = baseline_row["git_sha"]
        baseline_date = baseline_row["scanned_at"]
        stretch_start = _open_stale_stretch_start(rows)
        if stretch_start is not None:
            went_stale = rows[stretch_start]

    口 = Step(
        step_num=3,
        name="Retrieve old content at its baseline path",
        purpose=(
            "Read the file at the baseline commit, following a rename or move since then "
            "(committed or staged), so a moved file diffs against its old path"
        ),
        outputs="old file content and baseline_path; empty content and None when the file is new",
        critical=(
            "Three outcomes stay distinct: found (real diff), absent (new file, empty old side, or a stale "
            "node's Step 5 fallback), "
            "unresolved (git error or skipped rename detection -> error, never an empty diff). "
            "An uncommitted rename is followed only once staged; an untracked path reads as new"
        ),
    )
    git_path = file_path.replace("\\", "/")
    baseline_file = read_file_at_baseline(Path(project_root), baseline_sha, git_path)
    if baseline_file.status == "error":
        code = "baseline_path_unresolved" if baseline_file.unresolved else "no_baseline"
        return {"error": code, "reason": baseline_file.reason}
    old_file_content = baseline_file.content or ""
    baseline_path = baseline_file.path

    口 = Step(
        step_num=4,
        name="Locate the node by identity on both sides",
        purpose=(
            "Read the current file from disk, find the node in the baseline and current content by its id "
            "(re-scanned with the indexer's scanner at the node's current path; DocJSON sections by dot-path, "
            "Markdown sections by slug; "
            "the baseline falls back to the node's prior ids from the rename ledger) and cut each side at its own position"
        ),
        outputs="old_content and new_content: each side's own text for the node; empty where the node is absent",
        critical=(
            "Never slice at the indexed line range. Three outcomes per side stay distinct: found (that side's text), "
            "absent (clean scan without the node -> empty side), unresolved (parse failure, scanner unavailable, "
            "no identity scan for the file kind -> node_position_unresolved error, never a mis-sliced diff)"
        ),
    )
    src_file = Path(project_root) / file_path
    if not src_file.exists():
        return {"error": "no_baseline", "reason": f"Source file not found: {file_path}"}

    new_file_content = src_file.read_text(encoding="utf-8", errors="replace")

    new_status, new_value = _locate_node_in_content(new_file_content, node, project_root, git_path, [node_id])
    if new_status == "unresolved":
        return {"error": _POSITION_UNRESOLVED, "reason": f"current file {git_path}: {new_value}"}
    new_content = new_value

    candidates = _baseline_candidate_ids(node_id, _prior_node_ids(db_path, node_id))
    old_status = "absent"
    if baseline_file.status == "absent":
        old_content = ""
    else:
        old_status, old_value = _locate_node_in_content(old_file_content, node, project_root, git_path, candidates)
        if old_status == "unresolved":
            return {
                "error": _POSITION_UNRESOLVED,
                "reason": f"baseline file {baseline_path} at {baseline_sha}: {old_value}",
            }
        old_content = old_value

    口 = Step(
        step_num=5,
        name="Fall back to a commit that holds the node",
        purpose=(
            "When a stale node is absent at its default baseline (it was indexed before it was committed), "
            "diff against the newest commit whose copy of the node hashes to the text it went stale from"
        ),
        critical=(
            "A node that existed before its edit never diffs with an all-new old side, and a commit that already "
            "holds the edit is never its baseline: when no commit qualifies the result is no_baseline with the reason"
        ),
    )
    if went_stale is not None and old_status == "absent":
        recorded_sha = baseline_sha
        stale_status = went_stale["change_type"].removeprefix("BECAME_")
        stale_at = went_stale["scanned_at"]
        baseline_hashes = {(node.code_hash, node.desc_hash)}
        verification = db.get_verification(db_path, node_id)
        if verification:
            baseline_hashes.add((verification.get("code_hash_at"), verification.get("desc_hash_at")))
        holding = _commit_holding_node(project_root, node, git_path, candidates, baseline_hashes, recorded_sha)
        if holding is None:
            return {
                "error": "no_baseline",
                "reason": (
                    f"the node did not exist at {recorded_sha}, the last commit recorded before it went "
                    f"{stale_status} at {stale_at}, and none of the last {_BASELINE_WALK_LIMIT} commits of "
                    f"{git_path} holds it as it was before then"
                ),
            }
        baseline_sha, baseline_path, old_content, baseline_date = holding
        baseline_reason = (
            f"newest commit that holds the node as it was before it went {stale_status} at {stale_at} "
            f"(the last commit recorded before then, {recorded_sha}, does not hold the node)"
        )
        logger.info(
            "diff %s: %s lacks the node; baseline %s holds its pre-stale text", node_id, recorded_sha, baseline_sha
        )

    口 = Step(
        step_num=6, name="Get commit context", purpose="Retrieve commit subject, author, and date for the baseline SHA"
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
        "path": git_path,
        "baseline_path": baseline_path,
        "baseline_sha": baseline_sha,
        "baseline_date": baseline_date,
        "baseline_reason": baseline_reason,
        "commit_subject": commit_subject,
        "commit_author": commit_author,
        "commit_date": commit_date,
    }


NODE_DIFF_BATCH_DELIMITER = "\n\n---\n\n"


@task(
    purpose="Build the node diff report shared by the MCP diff tool and the diff command: "
    "get_node_diff's sides plus a +N / -M line summary",
    inputs="db_path, project_root, node_id, optional baseline_sha, summary_only",
    outputs="report dict (node_id, baseline, paths, contents or line counts, summary), or get_node_diff's error dict",
)
def node_diff_report(
    db_path: Path,
    project_root: Path,
    node_id: str,
    baseline_sha: str | None = None,
    summary_only: bool = False,
) -> dict:
    """Return the diff report a diff surface prints for one node.

    Wraps :func:`get_node_diff` and adds the ``+N / -M lines in body``
    summary (lines present on one side and not the other).  The MCP
    ``axiom_graph_diff`` tool and the ``axiom-graph diff`` command both
    print this report, so they always agree.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Root of the git repository.
        node_id: The node to diff.
        baseline_sha: Commit to diff against; resolved from history when ``None``.
        summary_only: Omit ``old_content`` / ``new_content`` and include
            ``lines_added`` / ``lines_removed`` instead.

    Returns:
        The ``{error, reason}`` dict from :func:`get_node_diff` unchanged on
        failure; otherwise ``{node_id, baseline_sha, baseline_date,
        baseline_reason, path, baseline_path, old_content, new_content,
        summary}`` (or, with
        *summary_only*, the same without the contents plus
        ``lines_added`` / ``lines_removed``).
    """
    result = get_node_diff(db_path, project_root, node_id, baseline_sha=baseline_sha)
    if "error" in result:
        return result

    old_lines = result["old_content"].splitlines()
    new_lines = result["new_content"].splitlines()
    added = sum(1 for ln in new_lines if ln not in old_lines)
    removed = sum(1 for ln in old_lines if ln not in new_lines)
    summary = f"+{added} / -{removed} lines in body"

    report = {
        "node_id": node_id,
        "baseline_sha": result["baseline_sha"],
        "baseline_date": result["baseline_date"],
        "baseline_reason": result["baseline_reason"],
        "path": result["path"],
        "baseline_path": result["baseline_path"],
    }
    if summary_only:
        report.update({"summary": summary, "lines_added": added, "lines_removed": removed})
    else:
        report.update({"old_content": result["old_content"], "new_content": result["new_content"], "summary": summary})
    return report


def format_node_diff_report(report: dict) -> str:
    """Render a :func:`node_diff_report` result as the text a diff surface prints.

    An error report is compact JSON; a diff is JSON indented by two spaces.

    Args:
        report: A dict returned by :func:`node_diff_report`.

    Returns:
        The JSON text.
    """
    if "error" in report:
        return json.dumps(report)
    return json.dumps(report, indent=2)


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

    Both document classes share ``documents`` and ``sections``; a mapping's
    ``kind`` says which one it is (``doc`` / ``section`` for DocJSON,
    ``markdown_doc`` / ``markdown_section`` for Markdown).  One list rather
    than two per class keeps ``as_mapping`` a single map — the on-disk
    ``links[]`` rewrite makes exactly one pass over the tree and has to be
    able to rewrite a reference to either class in it.

    Attributes:
        project_id: Project ID prefix.
        docs_roots: Configured docs roots that exist on disk.
        documents: One old -> new mapping per document envelope, both classes.
        sections: One old -> new mapping per section node, both classes.
        collisions: Duplicate groups in the *projected* ID set, across both
            document classes.  Non-empty means execute mode is unreachable.
        current_collisions: Duplicate groups in the current DocJSON ID set —
            the overlaps that already exist today.
        markdown_shadowed: Repo-relative paths of Markdown files whose
            *current* identity is already claimed by another document, so
            there is no distinct node to move.  Advisory, never blocking:
            these overlaps are the defect the new derivation removes, and
            refusing on them would leave such a project permanently
            unmigratable.  Each file gets its own identity at the next build.
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
    markdown_shadowed: list[str] = field(default_factory=list)
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
        files_not_patched: DocJSON files whose links the rewrite could not
            patch -- unreadable, or locked by another writer past the
            deadline.
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
    files_not_patched: list[str] = field(default_factory=list)
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

    In a git repository both buckets cover tracked files only (``git
    ls-files``): ignored files, untracked files and nested checkouts are
    never read, so nothing the sweep writes is beyond ``git checkout``.
    Outside a repository the whole tree is walked.

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
    tracked = _tracked_paths(root)
    if tracked is not None:
        doc_files = [f for f in doc_files if f.rel_path in tracked]
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
    for path in _prose_candidate_files(root, tracked, skip_dirs):
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


def _tracked_paths(root: Path) -> set[str] | None:
    """Return the repo-relative paths git tracks under *root*.

    Args:
        root: Absolute project root.

    Returns:
        Repo-relative POSIX paths from ``git ls-files``, or ``None`` when git
        cannot answer -- a missing binary, a directory that is not a
        repository, or a failing command.  Ignored files, untracked files and
        nested checkouts are never in the set, so every path in it is one
        ``git checkout`` can restore.
    """
    raw = _run_git(["ls-files", "-z"], root)
    if raw is None:
        return None
    return {p for p in raw.split("\0") if p}


def _prose_candidate_files(root: Path, tracked: set[str] | None, skip_dirs: set[str]) -> list[Path]:
    """Return the files the prose scan reads outside DocJSON content.

    In a repository the candidates are exactly the tracked files, so the
    sweep never reads or writes anything ``git checkout`` cannot restore.
    Outside one, the whole tree is walked so the preview can still report;
    the sweep writes nothing there, because :func:`_uncommitted_paths`
    cannot answer either.

    Args:
        root: Absolute project root.
        tracked: Tracked repo-relative paths, or ``None`` when git cannot answer.
        skip_dirs: Directory names never scanned.

    Returns:
        Absolute paths of candidate files.
    """
    if tracked is not None:
        return [
            root / rel
            for rel in sorted(tracked)
            if not skip_dirs.intersection(rel.split("/")[:-1]) and (root / rel).is_file()
        ]
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        candidates.extend(Path(dirpath) / name for name in filenames)
    return candidates


@dataclass(frozen=True)
class ProseRewrite:
    """One textual doc-ID reference the sweep rewrote.

    Attributes:
        file_path: Repo-relative path of the file holding the reference.
        line: 1-based line number.
        old_text: The reference exactly as it was written.
        new_text: The reference as it was rewritten.
        in_docjson_content: Whether the reference sits in DocJSON section
            prose, as opposed to anywhere else in the repository.
    """

    file_path: str
    line: int
    old_text: str
    new_text: str
    in_docjson_content: bool


@dataclass
class ProseSweepResult:
    """Outcome of a doc-ID prose sweep.

    Attributes:
        dry_run: Whether the sweep wrote anything.
        rewrites: Every reference rewritten, in scan order.
        files_rewritten: Repo-relative paths actually written to, sorted.
        skipped_uncommitted: Repo-relative paths left alone because they
            carry uncommitted changes, sorted.  Their references are counted
            nowhere else -- a skipped file is unswept, not swept-and-reverted.
        unresolved: References naming no document on disk.  Left exactly as
            written: there is no target to rewrite them to.
        report: A markdown review report of what the sweep did to DocJSON
            section prose.
    """

    dry_run: bool
    rewrites: list[ProseRewrite] = field(default_factory=list)
    files_rewritten: list[str] = field(default_factory=list)
    skipped_uncommitted: list[str] = field(default_factory=list)
    unresolved: list[ProseReference] = field(default_factory=list)
    report: str = ""

    @property
    def references_rewritten(self) -> int:
        """Number of references actually rewritten."""
        return len(self.rewrites)


def _uncommitted_paths(root: Path) -> set[str] | None:
    """Return project-relative paths carrying uncommitted changes.

    ``git status`` reports paths from the repository root, which is not the
    project root when the project sits inside a larger repository, so each
    path is re-based onto the project root and anything outside it dropped.
    ``-z`` output is used so non-ASCII and space-containing names arrive
    unquoted.

    Args:
        root: Absolute project root.

    Returns:
        Project-relative POSIX paths with staged, unstaged, or untracked
        changes, or ``None`` when git cannot answer -- a missing binary, a
        directory that is not a repository, or a failing command.  ``None``
        is not an empty set: a caller that cannot tell which files are
        committed must treat every file as unsafe to rewrite.
    """
    prefix = _run_git(["rev-parse", "--show-prefix"], root)
    raw = _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    if prefix is None or raw is None:
        return None
    prefix = prefix.strip()
    entries = raw.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" or entry[1] in "RC":
            # A rename or copy is followed by its origin path as its own entry.
            if i < len(entries) and entries[i]:
                paths.append(entries[i])
            i += 1
    return {p[len(prefix) :] for p in paths if p.startswith(prefix)}


def _rewrite_reference(text: str, doc_id: str, new_doc_id: str) -> str:
    """Return *text* with its document half replaced.

    A document ID is a prefix of every one of its section IDs, so only the
    leading ``doc_id`` may move; the ``::`` section path after it is carried
    across untouched.

    Args:
        text: The reference exactly as written.
        doc_id: The document envelope the reference resolved to.
        new_doc_id: That envelope's new identity.

    Returns:
        The rewritten reference.
    """
    return new_doc_id + text[len(doc_id) :]


def _render_sweep_report(result: ProseSweepResult, project_id: str) -> str:
    """Render the DocJSON-content review report for a sweep.

    The sweep touches prose no test can verify, so what it did to DocJSON
    section content is reviewed by a person.  Every rewrite in that bucket is
    listed with its file, line, and both forms.

    Entries are distinct: a line naming the same reference more than once --
    common in DocJSON, where a whole section's content is one JSON line --
    is listed once with a ``(×N)`` count, and the header counts entries, not
    occurrences.  ``result.rewrites`` itself keeps every occurrence.

    Args:
        result: The populated sweep result, minus its report.
        project_id: Project ID prefix, for the header.

    Returns:
        A markdown report.
    """
    distinct = Counter(result.rewrites)
    in_docjson = [r for r in distinct if r.in_docjson_content]
    elsewhere = [r for r in distinct if not r.in_docjson_content]
    unresolved = Counter(result.unresolved)
    mode = "DRY RUN — nothing was written" if result.dry_run else "applied"

    def _times(count: int) -> str:
        return f"  (×{count})" if count > 1 else ""

    lines = [
        f"# Doc-ID prose sweep — project '{project_id}' ({mode})",
        "",
        f"- DocJSON section content : {len(in_docjson)} reference(s) in "
        f"{len({r.file_path for r in in_docjson})} file(s)",
        f"- Elsewhere in the repo   : {len(elsewhere)} reference(s) in {len({r.file_path for r in elsewhere})} file(s)",
        f"- Skipped (uncommitted)   : {len(result.skipped_uncommitted)} file(s)",
        f"- Left as written         : {len(unresolved)} reference(s) naming no document",
        "",
    ]

    lines.append("## DocJSON section content")
    lines.append("")
    if not in_docjson:
        lines.append("No DocJSON section prose was rewritten.")
        lines.append("")
    else:
        for path in sorted({r.file_path for r in in_docjson}):
            lines.append(f"### {path}")
            lines.append("")
            for rewrite in [r for r in in_docjson if r.file_path == path]:
                lines.append(f"- L{rewrite.line}  `{rewrite.old_text}`{_times(distinct[rewrite])}")
                lines.append(f"  -> `{rewrite.new_text}`")
            lines.append("")

    if result.skipped_uncommitted:
        lines.append("## Skipped — uncommitted changes")
        lines.append("")
        lines.append("These files were left untouched so the sweep stays one `git checkout` from undo.")
        lines.append("")
        lines.extend(f"- {path}" for path in result.skipped_uncommitted)
        lines.append("")

    if unresolved:
        lines.append("## Left as written — no such document")
        lines.append("")
        lines.append("Placeholders and examples naming no document on disk. There is nothing to rewrite them to.")
        lines.append("")
        lines.extend(f"- {ref.file_path}:{ref.line}  `{ref.text}`{_times(count)}" for ref, count in unresolved.items())
        lines.append("")

    return "\n".join(lines)


@workflow(
    purpose="Rewrite every stale textual doc-ID reference that resolves to a real document, skipping files with uncommitted changes and reporting what it did to DocJSON prose",
    inputs="project root, old -> new document id mapping, dry_run flag",
    outputs="ProseSweepResult — rewrites, files written, files skipped, unresolved references, review report",
)
def sweep_doc_id_prose(
    root: Path,
    mapping: dict[str, str],
    *,
    dry_run: bool = False,
) -> ProseSweepResult:
    """Apply a doc-ID migration's mapping to the prose the migration cannot reach.

    The migration rewrites ``links[].node_id`` entries; every other written
    mention of a doc ID -- in DocJSON section content, in skills, templates,
    READMEs, and docstrings -- is inert text that goes stale the moment the
    identities move.  This is the write half of the scan the preview already
    reports.

    Two guardrails, both behavioural:

    * only **tracked** files are read or written, and a file carrying
      **uncommitted changes** is skipped and named, so the whole sweep stays
      one ``git checkout`` away from undo and can never destroy work in
      progress or touch ignored output and nested worktrees.  When git
      cannot answer at all, nothing is rewritten;
    * what the sweep did to DocJSON section prose is rendered as a review
      report, because no test can tell a good prose rewrite from a bad one.

    References that resolve to no document on disk are reported and left
    alone.  That is a property of the data rather than a policy: a
    placeholder has no target identity, so there is nothing to rewrite it to.

    Args:
        root: Absolute project root.
        mapping: Old -> new document envelope IDs, as
            :meth:`DocIdMigrationPlan.as_mapping` returns them.
        dry_run: When ``True``, compute and report without writing.

    Returns:
        A :class:`ProseSweepResult`.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    口 = Step(
        step_num=1,
        name="Scan the tree for textual references",
        purpose="Reuse the scan the preview reports from rather than matching a second time",
    )
    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = builder.resolve_project_id(root, config=config)
    documents = doc_ids.classify_doc_files(
        doc_ids.enumerate_doc_files(root, config.scan.docs_dirs, config.scan.docs_extensions)
    ).documents
    known_doc_ids = {doc_ids.current_doc_id(project_id, f) for f in documents}
    in_content, elsewhere = scan_doc_id_prose_references(
        root,
        project_id,
        documents,
        known_doc_ids,
        exclude_dirs=tuple(config.scan.exclude_dirs or ()),
    )

    口 = Step(
        step_num=2,
        name="Turn resolved references into rewrites",
        purpose="Only the document half of a reference moves; a section path rides across untouched",
    )
    candidates: list[ProseRewrite] = []
    unresolved: list[ProseReference] = []
    for ref, in_docjson in [(r, True) for r in in_content] + [(r, False) for r in elsewhere]:
        new_doc_id = mapping.get(ref.doc_id) if ref.doc_id else None
        if new_doc_id is None:
            unresolved.append(ref)
            continue
        candidates.append(
            ProseRewrite(
                file_path=ref.file_path,
                line=ref.line,
                old_text=ref.text,
                new_text=_rewrite_reference(ref.text, ref.doc_id, new_doc_id),
                in_docjson_content=in_docjson,
            )
        )

    口 = Step(
        step_num=3,
        name="Exclude files carrying uncommitted changes",
        purpose="A file that is not committed cannot be reverted, so it is never rewritten",
    )
    dirty = _uncommitted_paths(root)
    by_file: dict[str, list[ProseRewrite]] = {}
    for rewrite in candidates:
        by_file.setdefault(rewrite.file_path, []).append(rewrite)
    skipped = sorted(path for path in by_file if dirty is None or path in dirty)
    writable = {path: rewrites for path, rewrites in by_file.items() if path not in set(skipped)}

    口 = Step(
        step_num=4,
        name="Apply the rewrites, one pass per file",
        purpose="Rewrite the longest reference on a line first so a document id never eats its own section id",
    )
    applied: list[ProseRewrite] = []
    files_written: list[str] = []
    for path, rewrites in writable.items():
        if dry_run:
            applied.extend(rewrites)
            continue
        target = root / path
        try:
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError) as exc:
            # A file that cannot be read is not rewritten, so it must not be
            # counted as rewritten either: the report is the artifact a person
            # reads to decide whether to trust the run.
            logger.warning("prose sweep could not read %s: %s", path, exc)
            continue
        for rewrite in sorted(rewrites, key=lambda r: len(r.old_text), reverse=True):
            index = rewrite.line - 1
            if 0 <= index < len(lines):
                lines[index] = lines[index].replace(rewrite.old_text, rewrite.new_text)
        target.write_text("".join(lines), encoding="utf-8")
        applied.extend(rewrites)
        files_written.append(path)

    口 = Step(
        step_num=5,
        name="Render the review report",
        purpose="What the sweep did to DocJSON prose is reviewed by a person, not by a test",
    )
    result = ProseSweepResult(
        dry_run=dry_run,
        rewrites=applied,
        files_rewritten=sorted(files_written),
        skipped_uncommitted=skipped,
        unresolved=unresolved,
    )
    result.report = _render_sweep_report(result, project_id)
    return result


@task(
    purpose="Report whether the index holds doc ids under a namespace this code no longer derives",
    inputs="db_path, project root",
    outputs="DocIdReconciliation — clear / unmigrated / no_index, with the stale ids",
)
def check_doc_id_reconciliation(db_path: Path, root: Path):
    """Return the doc-namespace verdict for a project, writing nothing.

    The predicate the build gates on, exposed for the CLI and for anyone who
    wants to ask before running a build.  A ``no_index`` verdict is not a
    weaker ``clear``: a project with nothing indexed yet has nothing to
    reconcile, and must build normally.

    Args:
        db_path: Path to the axiom-graph DB.  May not exist.
        root: Absolute project root.

    Returns:
        A :class:`~axiom_graph.index.doc_ids.DocIdReconciliation`.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = builder.resolve_project_id(root, db_path, config=config)
    stored = set(db.all_doc_ids(db_path)) if Path(db_path).exists() else set()
    stored -= set(doc_ids.malformed_doc_ids(stored, project_id))
    return doc_ids.reconcile_doc_namespace(
        stored,
        project_id,
        doc_ids.classify_doc_files(
            doc_ids.enumerate_doc_files(root, config.scan.docs_dirs, config.scan.docs_extensions)
        ).documents,
        doc_ids.enumerate_markdown_files(root, config.scan.docs_dirs),
    )


def _resolve_markdown_shadowing(
    markdown,
    docjson_old_ids: set[str],
) -> tuple[list, list, list[str]]:
    """Drop Markdown mappings whose *current* identity belongs to someone else.

    Under the retired derivation a Markdown document is keyed on its filename
    stem alone, so ``a/notes.md`` and ``b/notes.md`` derive one identity, and
    ``x.md`` derives the same identity as ``x.json``.  Only one node row
    exists for such a group -- whichever file the build upserted last -- so
    only one mapping has anything to move.  The rest are dropped here rather
    than left in the plan, where their duplicate ``old_id`` would silently
    overwrite a live entry in the single old -> new map that drives both the
    rekey and the on-disk link rewrite.

    Who wins matches who won the build: DocJSON always (its scanner runs
    after the Markdown one within a root), and among Markdown files the
    last-enumerated, since enumeration and the scan walk share a sort order.

    Args:
        markdown: The ``DocIdProjection`` from ``project_markdown_doc_ids``.
        docjson_old_ids: Every current DocJSON document envelope ID.

    Returns:
        ``(documents, sections, shadowed_paths)`` -- the surviving Markdown
        mappings and the repo-relative paths that were dropped, sorted.
    """
    winner_by_old_id: dict[str, str] = {}
    shadowed: set[str] = set()
    for mapping in markdown.documents:
        if mapping.old_id in docjson_old_ids:
            shadowed.add(mapping.file_path)
            continue
        previous = winner_by_old_id.get(mapping.old_id)
        if previous is not None:
            shadowed.add(previous)
        winner_by_old_id[mapping.old_id] = mapping.file_path

    kept_paths = set(winner_by_old_id.values())
    documents = [m for m in markdown.documents if m.file_path in kept_paths]
    sections = [m for m in markdown.sections if m.file_path in kept_paths]
    return documents, sections, sorted(shadowed)


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

    Every ``*.md`` under a docs root is planned against too, with its H2
    slugs as section identities.  Markdown needs no classification step --
    the Markdown scanner admits every file it walks -- and its mappings land
    in the same ``documents`` / ``sections`` lists so the on-disk link
    rewrite stays one pass over one map.

    Args:
        db_path: Path to the axiom-graph DB.  Not read — accepted so plan and
            execute share one signature shape.
        root: Absolute project root.

    Returns:
        A :class:`DocIdMigrationPlan`.  ``blocked`` is ``True`` when two
        *documents* share one doc ID — in the projected set, which would
        recreate the overwrite this migration exists to remove, or in the
        current DocJSON set, where there is no coherent identity to move.
        A Markdown file whose current identity is already taken is reported
        in ``markdown_shadowed`` rather than blocking; see the attribute.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = builder.resolve_project_id(root, db_path, config=config)

    scan = doc_ids.classify_doc_files(
        doc_ids.enumerate_doc_files(root, config.scan.docs_dirs, config.scan.docs_extensions)
    )
    documents = scan.documents
    projection = doc_ids.project_doc_ids(project_id, documents)
    known_doc_ids = {m.old_id for m in projection.documents}

    markdown_files = doc_ids.enumerate_markdown_files(root, config.scan.docs_dirs)
    markdown = doc_ids.project_markdown_doc_ids(project_id, markdown_files)
    md_documents, md_sections, md_shadowed = _resolve_markdown_shadowing(markdown, known_doc_ids)

    in_content, elsewhere = scan_doc_id_prose_references(
        root,
        project_id,
        documents,
        known_doc_ids,
        exclude_dirs=tuple(config.scan.exclude_dirs or ()),
    )

    new_id_sources = dict(projection.new_id_sources)
    for new_id, sources in markdown.new_id_sources.items():
        new_id_sources.setdefault(new_id, []).extend(sources)

    return DocIdMigrationPlan(
        project_id=project_id,
        docs_roots=[entry for entry, _abs in doc_ids.resolve_docs_roots(root, config.scan.docs_dirs)],
        documents=projection.documents + md_documents,
        sections=projection.sections + md_sections,
        collisions=doc_ids.find_collisions(new_id_sources),
        current_collisions=doc_ids.find_collisions(doc_ids.current_doc_id_index(project_id, documents)),
        markdown_shadowed=md_shadowed,
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

    Both document classes migrate.  A Markdown document is a ``nodes`` row
    with ``#slug`` children and no ``docs`` record, so its rekey is told to
    walk sections by ``#``; DocJSON keeps ``::``.  Both classes' mappings
    ride in one map, so the on-disk link rewrite stays a single pass however
    many classes the tree holds.

    What survives: the rename ledger, node history, verification for
    documents *and* sections, graph edges, and ``links[].node_id``
    references on disk.  What does not: prose references to doc IDs (they
    are reported by :func:`plan_doc_id_migration`, never rewritten) and
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

    # Nothing to do: no old identity in the mapping is still in the index.
    # Checked here, ahead of the backup, so a second run is *reported* as a
    # no-op rather than merely being a harmless one.  Without this the rekey
    # finds no rows and clones nothing, yet the run still copies the database
    # and announces N documents migrated -- which reads as a second migration
    # having happened and is the reason anyone worries about re-running it.
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT id FROM nodes").fetchall()
    if not ({r["id"] for r in rows} & set(mapping)):
        return DocIdMigrationResult(executed=False, reason="already_migrated", plan=fresh)

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
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    file_paths = {m.old_id: m.file_path for m in fresh.documents}
    # A document's class decides how its sections hang off it: DocJSON uses a
    # ``::`` dot-path, Markdown a ``#`` slug.  Rekeying a Markdown document
    # with the DocJSON separator moves the envelope and strands every section.
    section_seps = {
        m.old_id: (doc_ids.MARKDOWN_SECTION_SEP if m.kind == "markdown_doc" else doc_ids.DOCJSON_SECTION_SEP)
        for m in fresh.documents
    }
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
                sep = section_seps.get(old_id, doc_ids.DOCJSON_SECTION_SEP)
                sections_migrated += db.rekey_doc_identity(conn, old_id, new_id, file_path, section_sep=sep).sections
                db.record_doc_rename_conn(conn, old_id, new_id, file_path, section_sep=sep)
                db.delete_doc_by_id(
                    conn,
                    old_id,
                    reason_meta={"actor": "doc-id-migration", "reason": f"renamed to {new_id}"},
                    section_sep=sep,
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
    patched = patch_doc_links_batch(root, mapping)

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
        files_patched=patched.files_patched,
        doc_files_read=patched.files_read,
        files_not_patched=patched.unreadable + patched.not_patched,
        plan=fresh,
    )


# ---------------------------------------------------------------------------
# DocJSON extension rename -- ``doc-ids rename-extension``
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtensionRename:
    """One document the rename command converts.

    Attributes:
        old_path: Project-relative path of the ``.json`` document.
        new_path: Project-relative path it is renamed to.
    """

    old_path: str
    new_path: str


@dataclass
class ExtensionRenamePlan:
    """What ``doc-ids rename-extension`` would do, and why it may refuse.

    Attributes:
        renames: Git-tracked DocJSON documents to rename, in path order.
        untracked: Documents git does not track.  Listed, never renamed.
        data_files: ``.json`` files under a docs root that are not documents
            (or cannot be read).  Never renamed.
        collisions: Documents whose ``.docjson`` target already exists.
        dirty: Documents to rename that carry uncommitted changes.
        not_git: The project is not a git repository (or git cannot answer).
        gate_blocked: The index still holds the retired doc-ID namespace,
            so ``doc-ids execute`` must run first.
        target_not_scanned: ``.docjson`` is not in the configured
            ``docs_extensions``, so a renamed document would drop out of
            the index.
    """

    renames: list[ExtensionRename] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    data_files: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)
    not_git: bool = False
    gate_blocked: bool = False
    target_not_scanned: bool = False

    @property
    def refusals(self) -> list[str]:
        """Every reason execute would refuse, one line each; empty when it may run."""
        out: list[str] = []
        if self.target_not_scanned:
            out.append(
                "`.docjson` is not in [axiom_graph.scan] docs_extensions: renamed documents would "
                "not be scanned and the re-index would delete them; add `.docjson` first"
            )
        if self.not_git:
            out.append("not a git repository: the rename runs through `git mv` so git sees renames")
        if self.gate_blocked:
            out.append("the index still holds the retired doc-id namespace: run `axiom-graph doc-ids execute` first")
        if self.dirty:
            out.append(f"uncommitted changes in {len(self.dirty)} file(s) it would rename: {', '.join(self.dirty)}")
        if self.collisions:
            out.append(f"the target already exists for {len(self.collisions)} file(s): {', '.join(self.collisions)}")
        return out


@dataclass
class ExtensionRenameResult:
    """Outcome of ``doc-ids rename-extension --execute``.

    Attributes:
        plan: The plan execute ran (or refused) against.
        executed: Whether any rename ran.
        renamed: The renames performed, in order.
        refused: The plan's refusals when execute refused; empty otherwise.
        build: The re-index run after the renames, when there was one.
    """

    plan: ExtensionRenamePlan
    executed: bool = False
    renamed: list[ExtensionRename] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    build: BuildSummary | None = None


@task(
    purpose="Plan converting every git-tracked DocJSON document under the docs roots from .json to .docjson, writing nothing",
    inputs="db_path, project root",
    outputs="ExtensionRenamePlan — renames plus untracked, data, collision, dirty-file and gate findings",
)
def plan_docjson_extension_rename(db_path: Path, root: Path) -> ExtensionRenamePlan:
    """Return what ``doc-ids rename-extension`` would rename, writing nothing.

    Only ``.json`` files that classify as DocJSON *documents* are candidates;
    ordinary JSON data under a docs root is listed and never touched.  A
    document is renamed only when git tracks it, its ``.docjson`` target does
    not exist, and it carries no uncommitted change.  The doc-ID
    reconciliation gate is checked against the index too: a tree whose index
    still needs ``doc-ids execute`` must be migrated first, because a build
    there refuses.

    Args:
        db_path: Path to the axiom-graph DB.  Read for the gate only; a
            missing DB means there is nothing to reconcile.
        root: Absolute project root.

    Returns:
        An :class:`ExtensionRenamePlan`.
    """
    from axiom_graph.index import doc_ids  # noqa: PLC0415

    root = Path(root).resolve()
    config = AxiomGraphConfig.load(root)
    project_id = builder.resolve_project_id(root, db_path, config=config)
    source_ext = doc_ids.DOCJSON_EXTENSIONS[-1]
    target_ext = doc_ids.DOCJSON_EXTENSIONS[0]

    plan = ExtensionRenamePlan(target_not_scanned=target_ext not in config.scan.docs_extensions)
    scan = doc_ids.classify_doc_files(doc_ids.enumerate_doc_files(root, config.scan.docs_dirs, [source_ext]))
    plan.data_files = sorted(scan.non_documents + scan.unreadable)

    tracked = _tracked_paths(root)
    uncommitted = _uncommitted_paths(root)
    plan.not_git = tracked is None or uncommitted is None
    for doc_file in scan.documents:
        target = doc_file.path.with_name(doc_ids.strip_docjson_extension(doc_file.path.name) + target_ext)
        new_rel = doc_ids.strip_docjson_extension(doc_file.rel_path) + target_ext
        if target.exists():
            plan.collisions.append(doc_file.rel_path)
        elif tracked is not None and doc_file.rel_path not in tracked:
            plan.untracked.append(doc_file.rel_path)
        else:
            plan.renames.append(ExtensionRename(old_path=doc_file.rel_path, new_path=new_rel))
    plan.dirty = sorted(r.old_path for r in plan.renames if uncommitted and r.old_path in uncommitted)

    if Path(db_path).exists():
        all_documents = doc_ids.classify_doc_files(
            doc_ids.enumerate_doc_files(root, config.scan.docs_dirs, config.scan.docs_extensions)
        ).documents
        verdict = doc_ids.reconcile_doc_namespace(
            set(db.all_doc_ids(db_path)),
            project_id,
            all_documents,
            doc_ids.enumerate_markdown_files(root, config.scan.docs_dirs),
        )
        plan.gate_blocked = verdict.blocked
    return plan


@workflow(
    purpose="Convert every git-tracked DocJSON document from .json to .docjson with git mv, then re-index, refusing before any rename when the plan has a blocker",
    inputs="db_path, project root",
    outputs="ExtensionRenameResult — renames performed or the refusals, plus the re-index summary",
)
def execute_docjson_extension_rename(db_path: Path, root: Path) -> ExtensionRenameResult:
    """Rename every planned document to ``.docjson`` with ``git mv`` and re-index.

    The plan is computed fresh and every refusal -- ``.docjson`` missing
    from ``docs_extensions``, not a git repository, an index that still
    needs ``doc-ids execute``, uncommitted changes in a file
    it would rename, an existing target -- is checked before the first
    ``git mv``, so a refused run renames nothing.  The renames go through
    ``git mv`` so git records them as renames.  A doc ID never carries its
    extension, so every document keeps its identity, history and
    verification; the closing build moves the index's file paths to the new
    names.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Absolute project root.

    Returns:
        An :class:`ExtensionRenameResult`.

    Raises:
        RuntimeError: When a ``git mv`` fails partway; the renames already
            made are listed in the message and remain staged.
    """
    root = Path(root).resolve()
    口 = AutoStep(step_num=1, name="Plan the rename")
    plan = plan_docjson_extension_rename(db_path, root)
    result = ExtensionRenameResult(plan=plan)

    口 = Step(
        step_num=2,
        name="Refuse on any blocker",
        purpose="Check every refusal before the first git mv so a refused run renames nothing",
    )
    if plan.refusals:
        result.refused = plan.refusals
        for reason in result.refused:
            logger.warning("doc-ids rename-extension refused: %s", reason)
        return result
    if not plan.renames:
        logger.info("doc-ids rename-extension: no tracked .json documents to rename")
        return result

    口 = Step(
        step_num=3,
        name="Rename with git mv",
        purpose="Rename each planned document so git records a rename, never a delete plus add",
    )
    for rename in plan.renames:
        try:
            subprocess.run(
                ["git", "mv", "--", rename.old_path, rename.new_path],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            done = ", ".join(r.old_path for r in result.renamed) or "none"
            detail = getattr(exc, "stderr", "") or str(exc)
            logger.error("doc-ids rename-extension: git mv %s failed (already renamed: %s)", rename.old_path, done)
            raise RuntimeError(f"git mv {rename.old_path} failed: {detail.strip()} (already renamed: {done})") from exc
        logger.info("doc-ids rename-extension: renamed %s -> %s", rename.old_path, rename.new_path)
        result.renamed.append(rename)
    result.executed = True

    口 = Step(
        step_num=4,
        name="Re-index so file paths match disk",
        purpose="Rebuild so the index's file paths name the renamed files; every doc id is unchanged",
    )
    logger.info("doc-ids rename-extension: re-indexing after %d rename(s)", len(result.renamed))
    result.build = build_index(db_path, root)
    return result
