"""Lifecycle MCP wire surface.

Thin wrappers re-exporting the lifecycle behavioural API for the MCP
tool registry in ``axiom_graph.mcp.server``.  Each wrapper preserves
the public docstring and signature and forwards to
``axiom_graph.lifecycle.api``.  The ``_timed_tool`` decorator is
applied at registration time in ``mcp.server`` (matching cycle 1's
docjson template + ``workflows.mcp_tools`` precedent) so it composes
cleanly with the symmetric four-domain registration block.

Per ADR-019 (cycle 3), this module's allowed imports are:
``axiom_graph.lifecycle.api``, ``axiom_graph.config``,
``axiom_graph.index.paths`` (for ``require_db``), and the standard
library.  Nothing else (no direct ``db.*`` / ``index.*`` other than
``paths`` / ``sqlite3``).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import axiom_graph.lifecycle.api as _api  # noqa: F401
from axiom_graph.index.paths import require_db as _require_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def axiom_graph_build(project_root: str, verbose: bool = False, _embedder_thread=None) -> str:
    """Run axiom-graph build (discovery-only) for a project.

    Only nodes that have never been indexed are inserted, preserving
    ``CONTENT_UPDATED`` / ``NOT_FOUND`` signals.  Edges are updated
    in all cases.

    A full rebuild (which resets baselines and clears staleness) is
    intentionally not available through this tool -- use the CLI
    ``axiom-graph init`` after you have resolved and verified all stale
    nodes.

    To purge individual NOT_FOUND nodes, use ``axiom_graph_purge_node``.
    For bulk purge of all NOT_FOUND nodes, use the CLI
    ``axiom-graph build --purge``.

    Args:
        project_root: Absolute path to the project to index.
        verbose: When ``True``, include all warning details (ontology
            violations, scanner errors, etc.) in the output.  Default
            ``False`` shows only the warning count.
    """
    root = Path(project_root).resolve()
    db_path = _require_db(project_root)
    summary = _api.build_index(
        db_path,
        root,
        discovery_only=True,
        verbose=verbose,
        embedder_thread=_embedder_thread,
    )

    lines: list[str] = []
    num_warnings = len(summary.warnings)
    lines.append(
        f"axiom-graph build complete (discovery-only)\n"
        f"  files scanned   : {summary.files_scanned}\n"
        f"  files skipped   : {summary.files_skipped_mtime} (mtime unchanged)\n"
        f"  nodes added     : {summary.nodes_written}\n"
        f"  nodes unchanged : {summary.nodes_skipped}\n"
        f"  nodes renamed   : {summary.nodes_renamed}\n"
        f"  edges updated   : {summary.edges_written}\n"
        f"  edges unchanged : {summary.edges_skipped}\n"
        f"  broken links    : {summary.broken_links_flagged}\n"
        f"  warnings        : {num_warnings}"
    )
    if num_warnings > 0 and verbose:
        lines.append("")
        for w in summary.warnings:
            lines.append(f"  ! {w}")
    if summary.staleness_total:
        lines.append(f"  staleness      : {summary.staleness_total} nodes updated ({summary.staleness_stale} stale)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


def axiom_graph_checkout(project_root: str, worktree_path: str) -> str:
    """Copy the axiom-graph DB into a worktree via VACUUM INTO.

    Produces an atomic, consistent snapshot of the source index --
    safe regardless of WAL state or concurrent writes. The target
    directory must exist; it does not need to be a git worktree.

    If the target DB already exists, returns a skip warning. Delete
    the target DB manually to force a fresh copy.

    Args:
        project_root: Absolute path to the source project (must have
            .axiom_graph/graph.db).
        worktree_path: Absolute path to the target directory. A .axiom_graph/
            subdirectory will be created if needed.
    """
    source_db = _require_db(project_root)
    target_dir = Path(worktree_path)
    if not target_dir.is_dir():
        return f"ERROR: target directory does not exist: {worktree_path}"
    result = _api.checkout_db(source_db, target_dir, force=False)
    if not result.copied:
        return f"DB already exists at {result.target_db_path}, skipping -- delete manually to force refresh."
    return f"Copied axiom-graph DB to {result.target_db_path}"


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def axiom_graph_check(project_root: str, include_frozen: bool = False) -> str:
    """Report per-node staleness / confidence status (one-line summary).

    Returns a single summary line covering both dimensions of node
    health, with optional ``(all nodes VERIFIED)`` / ``(no doc-quality
    advisories)`` trailers when applicable:

        ``own: 3 CONTENT_UPDATED / 1 DESC_UPDATED / 0 NOT_FOUND ·
         link: 5 LINKED_STALE / 0 BROKEN_LINK · 42 VERIFIED · 1 DOC_SECTION_LONG``

    For per-node detail, paginated lists, filtered slices, or grouped
    aggregates use ``axiom_graph_drift_query``.

    Args:
        project_root: Absolute path to the indexed project.
        include_frozen: When ``False`` (the default), sections under
            docs tagged in ``config.staleness.frozen_tags`` are
            excluded from the summary counts.  When ``True`` they are
            included.  No-op when ``frozen_tags`` is empty.

    Note:
        ``verbose`` and ``filter`` parameters were removed in the
        2026-05 drift_query cycle.  Calling with those keyword
        arguments raises ``TypeError`` -- migrate to
        ``axiom_graph_drift_query`` instead.
    """
    root = Path(project_root).resolve()
    db_path = _require_db(project_root)
    cs = _api.compute_check_summary(db_path, root, include_frozen=include_frozen)

    if cs is None:
        return "(no nodes in index)"

    summary = (
        f"own: {cs.own_counts['CONTENT_UPDATED']} CONTENT_UPDATED / "
        f"{cs.own_counts['DESC_UPDATED']} DESC_UPDATED / "
        f"{cs.own_counts.get('RENAMED', 0)} RENAMED / "
        f"{cs.own_counts['NOT_FOUND']} NOT_FOUND · "
        f"link: {cs.link_counts['LINKED_STALE']} LINKED_STALE / "
        f"{cs.link_counts['BROKEN_LINK']} BROKEN_LINK · "
        f"{cs.clean_count} VERIFIED"
    )
    if cs.doc_quality_count:
        summary += f" · {cs.doc_quality_count} DOC_SECTION_LONG"
    if cs.invariant_violations:
        summary += f" · {cs.invariant_violations} INVARIANT_VIOLATION"

    if cs.all_clean and not cs.doc_quality_count and not cs.invariant_violations:
        return summary + "\n(all nodes VERIFIED)"
    return summary


# ---------------------------------------------------------------------------
# history (presentation lives here -- _format_history_for_node not in api)
# ---------------------------------------------------------------------------


def _format_history_for_node(
    project_root: str,
    node_id: str,
    max_results: int = 10,
    offset: int = 0,
) -> str:
    """Format history output for a single node (shared by single and batch modes).

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to inspect.
        max_results: Max history entries to return.
        offset: Number of entries to skip.
    """
    logger.debug(
        "axiom_graph_history: node_id=%s, max_results=%d, offset=%d",
        node_id,
        max_results,
        offset,
    )

    db_path = _require_db(project_root)
    result = _api.fetch_history(db_path, node_id, max_results=max_results, offset=offset)
    if not result.rows and result.total == 0:
        return f"No history found for '{node_id}'."

    rows = result.rows
    total = result.total
    shown = len(rows)
    header = f"[{shown} of {total} entries]"
    if total > offset + shown:
        header += f"  (pass offset={offset + shown} for next page)"
    sep = "─" * max(len(header), 60)
    lines = [f"history for {node_id}", header, sep]

    # Stale window annotation -- walk newest-first
    desc_seen = False
    stale_window_open = False
    now = datetime.now(timezone.utc)

    for row in rows:
        ct = row.change_type
        ts = row.scanned_at[:10]

        # Human-readable description
        if ct == "INITIAL":
            desc = "first scan"
        elif ct == "CONTENT_ONLY":
            desc = "content changed, description not updated"
        elif ct == "DESC_ONLY":
            desc = "description updated (content unchanged)"
        elif ct == "CONTENT_AND_DESC":
            desc = "content and description both changed"
        elif ct == "AGENT_VERIFIED":
            meta_blob = row.meta
            reason = ""
            if meta_blob:
                try:
                    reason = json.loads(meta_blob).get("reason", "")
                except Exception as exc:
                    logger.debug("failed to parse AGENT_VERIFIED meta: %s", exc)
            desc = f'agent verified — "{reason}"' if reason else "agent verified"
        elif ct == "MANUAL_VERIFIED":
            meta_blob = row.meta
            reason = ""
            if meta_blob:
                try:
                    reason = json.loads(meta_blob).get("reason", "")
                except Exception as exc:
                    logger.debug("failed to parse MANUAL_VERIFIED meta: %s", exc)
            desc = f'manually verified — "{reason}"' if reason else "manually verified"
        elif ct == "LINK_ADDED":
            meta_blob = row.meta
            target = ""
            if meta_blob:
                try:
                    target = json.loads(meta_blob).get("target", "")
                except Exception as exc:
                    logger.debug("failed to parse LINK_ADDED meta: %s", exc)
            desc = f"link added → {target}" if target else "link added"
        elif ct == "LINK_REMOVED":
            meta_blob = row.meta
            target = ""
            if meta_blob:
                try:
                    target = json.loads(meta_blob).get("target", "")
                except Exception as exc:
                    logger.debug("failed to parse LINK_REMOVED meta: %s", exc)
            desc = f"link removed → {target}" if target else "link removed"
        elif ct == "CHECKPOINT":
            git = f"git:{row.git_sha}" if row.git_sha else "no git sha"
            desc = f"{git}  (earlier history: git log --follow)"
        elif ct == "BECAME_CONTENT_UPDATED":
            meta_blob = row.meta
            from_status = ""
            if meta_blob:
                try:
                    from_status = json.loads(meta_blob).get("from", "")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            desc = f"became CONTENT_UPDATED (was {from_status})" if from_status else "became CONTENT_UPDATED"
        elif ct == "BECAME_DESC_UPDATED":
            meta_blob = row.meta
            from_status = ""
            if meta_blob:
                try:
                    from_status = json.loads(meta_blob).get("from", "")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            desc = f"became DESC_UPDATED (was {from_status})" if from_status else "became DESC_UPDATED"
        elif ct == "BECAME_LINKED_STALE":
            meta_blob = row.meta
            from_status, linked = "", ""
            if meta_blob:
                try:
                    m = json.loads(meta_blob)
                    from_status = m.get("from", "")
                    linked = m.get("linked_node", "")
                except Exception as exc:
                    logger.debug("failed to parse BECAME_LINKED_STALE meta: %s", exc)
            parts = ["became LINKED_STALE"]
            if linked:
                parts.append(f"via {linked}")
            if from_status:
                parts.append(f"(was {from_status})")
            desc = " ".join(parts)
        elif ct == "BECAME_BROKEN_LINK":
            meta_blob = row.meta
            from_status, linked = "", ""
            if meta_blob:
                try:
                    m = json.loads(meta_blob)
                    from_status = m.get("from", "")
                    linked = m.get("linked_node", "")
                except Exception as exc:
                    logger.debug("failed to parse BECAME_BROKEN_LINK meta: %s", exc)
            parts = ["became BROKEN_LINK"]
            if linked:
                parts.append(f"via {linked}")
            if from_status:
                parts.append(f"(was {from_status})")
            desc = " ".join(parts)
        elif ct == "BECAME_NOT_FOUND":
            meta_blob = row.meta
            from_status = ""
            if meta_blob:
                try:
                    from_status = json.loads(meta_blob).get("from", "")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            desc = f"became NOT_FOUND (was {from_status})" if from_status else "became NOT_FOUND"
        elif ct == "BECAME_RENAMED":
            meta_blob = row.meta
            from_status, old_id = "", ""
            if meta_blob:
                try:
                    m = json.loads(meta_blob)
                    from_status = m.get("from", "")
                    old_id = m.get("old_id", "")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            parts = ["became RENAMED"]
            if old_id:
                parts.append(f"from {old_id}")
            if from_status:
                parts.append(f"(was {from_status})")
            desc = " ".join(parts)
        elif ct == "RENAME_SCORING_SKIPPED":
            meta_blob = row.meta
            reason, candidates = "", None
            if meta_blob:
                try:
                    m = json.loads(meta_blob)
                    reason = m.get("reason", "")
                    candidates = m.get("candidates")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            detail = []
            if candidates is not None:
                detail.append(f"{candidates} candidate(s)")
            if reason:
                detail.append(f"reason={reason}")
            suffix = f" ({', '.join(detail)})" if detail else ""
            desc = f"rename scoring skipped — possible undetected rename{suffix}"
        elif ct in ("BECAME_VERIFIED", "LINK_BECAME_VERIFIED"):
            meta_blob = row.meta
            from_status = ""
            if meta_blob:
                try:
                    from_status = json.loads(meta_blob).get("from", "")
                except Exception as exc:
                    logger.debug("failed to parse %s meta: %s", ct, exc)
            label = "LINK_VERIFIED" if ct == "LINK_BECAME_VERIFIED" else "VERIFIED"
            desc = f"became {label} (was {from_status})" if from_status else f"became {label}"
        else:
            desc = ct

        # Stale window annotations
        annotation = ""
        if ct == "DESC_ONLY" or ct == "CONTENT_AND_DESC":
            desc_seen = True
            stale_window_open = False
        elif ct == "CONTENT_ONLY" and not desc_seen:
            if not stale_window_open:
                try:
                    row_date = datetime.fromisoformat(row.scanned_at)
                    if row_date.tzinfo is None:
                        row_date = row_date.replace(tzinfo=timezone.utc)
                    age_days = (now - row_date).days
                    annotation = f"  ← stale window opened ({age_days} days)"
                except Exception as exc:
                    logger.debug("failed to parse stale window date: %s", exc)
                    annotation = "  ← stale window opened"
                stale_window_open = True
            else:
                annotation = "  (stale window ongoing)"

        line = f"{ts}  {ct:<16}  {desc}{annotation}"
        lines.append(line)

    has_checkpoint = any(r.change_type == "CHECKPOINT" for r in rows)
    remaining = total - (offset + shown)
    if remaining > 0:
        if has_checkpoint:
            lines.append(f"... {remaining} more entries above checkpoint")
        else:
            lines.append(f"... {remaining} more entries (no checkpoint -- full history in axiom-graph DB)")

    return "\n".join(lines)


def axiom_graph_history(
    project_root: str,
    node_id: str,
    max_results: int = 10,
    offset: int = 0,
    node_ids: list[str] | None = None,
    limit: int | None = None,
) -> str:
    """Show the change history for a single node.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to inspect.
        max_results: Number of history entries to return (default 10, max 100).
        offset: Number of entries to skip (default 0).
        node_ids: Optional list of node IDs for batch operation. When
            provided, ``node_id`` is ignored and history is returned for
            all listed IDs with per-ID delimiters.
        limit: Deprecated alias for max_results.
    """
    if limit is not None and max_results == 10:
        max_results = limit
    max_results = min(max_results, 100)

    if node_ids is not None:
        if not node_ids:
            return "ERROR: node_ids list is empty"
        parts: list[str] = []
        for nid in node_ids:
            try:
                result = _format_history_for_node(
                    project_root,
                    nid,
                    max_results=max_results,
                    offset=offset,
                )
            except Exception as exc:
                result = f"ERROR ({nid}): {exc}"
            parts.append(result)
        return "\n\n---\n\n".join(parts)

    return _format_history_for_node(
        project_root,
        node_id,
        max_results=max_results,
        offset=offset,
    )


def axiom_graph_list_reference_points(project_root: str) -> str:
    """List available reference points for ``axiom_graph_report(since_sha=...)``.

    Call this **before** ``axiom_graph_report`` to discover valid SHA values.
    Returns checkpoints (explicit markers) and build SHAs (from indexed
    commits), newest first.  Each entry shows the short SHA, timestamp,
    type, row count, and checkpoint message (if any).

    Args:
        project_root: Absolute path to the indexed project.
    """
    db_path = _require_db(project_root)
    refs = _api.list_reference_points(db_path)

    if not refs:
        return "No reference points found. Run `axiom-graph build` or `axiom-graph history checkpoint` first."

    lines: list[str] = [f"[{len(refs)} reference point(s)]", ""]
    for ref in refs:
        sha_short = ref.git_sha[:12] if ref.git_sha else "?"
        ts_date = ref.scanned_at[:10]
        msg = f'  "{ref.message}"' if ref.message else ""
        lines.append(f"  {sha_short}  {ref.type:<12} {ts_date}  ({ref.row_count} rows){msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def axiom_graph_report(
    project_root: str,
    since_sha: str | None = None,
    since_timestamp: str | None = None,
    verbose: bool = False,
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
) -> str:
    """Impact report: what changed since a checkpoint, SHA, or datetime.

    Summarises content changes, staleness transitions, link modifications,
    and verification activity recorded in ``node_history``.

    **Reference point resolution** (first match wins):

    1. ``since_sha`` -- match a CHECKPOINT by git_sha prefix.
    2. ``since_sha`` -- match any history row by git_sha prefix.
    3. ``since_timestamp`` -- ISO-8601 datetime used directly as cutoff.
    4. Neither given -- most recent CHECKPOINT, then most recent history
       row with a git_sha.
    5. No reference found -- report over entire history.

    Always starts with a one-line summary.

    Args:
        project_root: Absolute path to the indexed project.
        since_sha: Git SHA prefix. Matched against history rows
            (CHECKPOINTs checked first, then any row).
        since_timestamp: ISO-8601 datetime cutoff (e.g.
            ``2026-03-18T00:00:00``).
        verbose: When False (default), return only the summary line.  When
            True, include the full categorised breakdown.
        change_type_pattern: Glob pattern to filter change types (e.g.
            ``*STALE*``, ``LINK_*``, ``AGENT_*``, ``INITIAL``).
        node_pattern: Glob pattern to filter node IDs (e.g.
            ``axiom_graph::axiom_graph.viz.*``).
        node_type: Filter to nodes of this type. One of
            ``atomic_process``, ``composite_process``, or ``entity``.
    """
    db_path = _require_db(project_root)
    data = _api.compute_report(
        db_path,
        since_sha=since_sha,
        since_timestamp=since_timestamp,
        change_type_pattern=change_type_pattern,
        node_pattern=node_pattern,
        node_type=node_type,
    )

    if data.no_rows:
        return "No history events found after the reference point."
    if data.no_matches:
        return "No history events match the given filters."

    summary_text = (
        f"{data.summary['nodes_changed']} nodes changed, "
        f"{data.summary['became_stale']} became stale, "
        f"{data.summary['verified']} verified ({data.summary['agent_only']} agent-only), "
        f"{data.summary['links_modified']} links modified"
    )

    if not verbose:
        return summary_text

    lines = [summary_text, "=" * len(summary_text)]

    if data.content_changes:
        lines.append("\nCONTENT CHANGES")
        lines.append("-" * 40)
        for nid in sorted(data.content_changes):
            types = ", ".join(sorted({e["change_type"] for e in data.content_changes[nid]}))
            lines.append(f"  {nid}  [{types}]")

    if data.staleness_transitions:
        lines.append("\nSTALENESS TRANSITIONS")
        lines.append("-" * 40)
        for r in data.staleness_transitions:
            ct = r["change_type"]
            meta_parts: list[str] = []
            if r.get("meta"):
                try:
                    m = json.loads(r["meta"])
                    if m.get("from"):
                        meta_parts.append(f"was {m['from']}")
                    if m.get("linked_node"):
                        meta_parts.append(f"via {m['linked_node']}")
                except Exception as exc:
                    logger.debug("failed to parse staleness meta: %s", exc)
            suffix = f"  ({', '.join(meta_parts)})" if meta_parts else ""
            lines.append(f"  {r['node_id']}  {ct}{suffix}")

    if data.link_changes:
        lines.append("\nLINK CHANGES")
        lines.append("-" * 40)
        for r in data.link_changes:
            target = ""
            if r.get("meta"):
                try:
                    target = json.loads(r["meta"]).get("target", "")
                except Exception as exc:
                    logger.debug("failed to parse link meta: %s", exc)
            arrow = "→" if r["change_type"] == "LINK_ADDED" else "✕"
            lines.append(f"  {r['node_id']}  {arrow} {target}")

    if data.verifications:
        lines.append("\nVERIFICATION ACTIVITY")
        lines.append("-" * 40)
        for r in data.verifications:
            ct = r["change_type"]
            flag = ""
            if ct == "AGENT_VERIFIED" and r["node_id"] not in data.human_verified_ids:
                flag = " ⚠ agent-only"
            lines.append(f"  {r['node_id']}  {ct}{flag}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def axiom_graph_diff(
    project_root: str,
    node_id: str,
    baseline_sha: str | None = None,
    node_ids: list[str] | None = None,
    summary_only: bool = False,
) -> str:
    """Show what changed in a node since a baseline commit.

    **Baseline resolution** (when *baseline_sha* is omitted):

    1. Most recent ``AGENT_VERIFIED``, ``MANUAL_VERIFIED``, or ``CHECKPOINT``
       history row with a non-NULL ``git_sha``.
    2. Fallback: oldest history row with a ``git_sha`` (typically the
       ``INITIAL`` scan -- gives "diff since first indexed").

    Pass *baseline_sha* explicitly to diff against a specific commit.

    When ``summary_only`` is ``False`` (default), the response is
    JSON-formatted text with keys: ``node_id``, ``baseline_sha``,
    ``baseline_date``, ``old_content``, ``new_content``, ``summary``.

    When ``summary_only`` is ``True``, ``old_content`` and ``new_content``
    are omitted and ``lines_added`` / ``lines_removed`` integers are
    included instead.  Use this for triage before deep-diving into
    individual nodes.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to diff.
        baseline_sha: Optional git SHA to diff against.  If omitted,
            auto-resolves from the node's history.
        node_ids: Optional list of node IDs for batch operation. When
            provided, ``node_id`` is ignored and diffs are returned for
            all listed IDs with per-ID delimiters.
        summary_only: If ``True``, return only metadata and line-count
            stats, omitting the full source content.  Useful for batch
            triage of many nodes without blowing up context.
    """
    if node_ids is not None:
        if not node_ids:
            return "ERROR: node_ids list is empty"
        parts: list[str] = []
        for nid in node_ids:
            try:
                result = axiom_graph_diff(
                    project_root,
                    nid,
                    baseline_sha=baseline_sha,
                    summary_only=summary_only,
                )
            except Exception as exc:
                result = f"ERROR ({nid}): {exc}"
            parts.append(result)
        return "\n\n---\n\n".join(parts)

    db_path = _require_db(project_root)
    root = Path(project_root).resolve()
    result = _api.get_node_diff(db_path, root, node_id, baseline_sha=baseline_sha)

    if "error" in result:
        return json.dumps(result)

    old_lines = result["old_content"].splitlines()
    new_lines = result["new_content"].splitlines()
    added = sum(1 for ln in new_lines if ln not in old_lines)
    removed = sum(1 for ln in old_lines if ln not in new_lines)
    summary = f"+{added} / -{removed} lines in body"

    if summary_only:
        output = {
            "node_id": node_id,
            "baseline_sha": result["baseline_sha"],
            "baseline_date": result["baseline_date"],
            "summary": summary,
            "lines_added": added,
            "lines_removed": removed,
        }
    else:
        output = {
            "node_id": node_id,
            "baseline_sha": result["baseline_sha"],
            "baseline_date": result["baseline_date"],
            "old_content": result["old_content"],
            "new_content": result["new_content"],
            "summary": summary,
        }
    return json.dumps(output, indent=2)


# ---------------------------------------------------------------------------
# mark_clean
# ---------------------------------------------------------------------------


def axiom_graph_mark_clean(
    project_root: str,
    node_id: str,
    reason: str,
    verified_by: str = "agent",
    node_ids: list[str] | None = None,
) -> str:
    """Mark one or more CONTENT_UPDATED nodes as agent-verified.

    Records an AGENT_VERIFIED history row per node. Nodes appear in
    ``axiom-graph history agent-verified`` for pre-push human review. Use only
    when you have read the current code and documentation and confirmed
    they are consistent.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Single node to mark clean (used when node_ids is omitted).
        reason: Brief explanation of why the documentation is still accurate.
        verified_by: Identifier for the verifier. Defaults to ``'agent'``;
            pass the model name for traceability, e.g.
            ``'agent:claude-sonnet-4-6'``.
        node_ids: Optional list of node IDs for batch operation. When
            provided, all listed nodes are marked clean with the shared
            reason and verified_by. ``node_id`` is ignored in this case.
    """
    logger.debug(
        "axiom_graph_mark_clean: node_id=%s, batch=%s",
        node_id,
        len(node_ids) if node_ids else "no",
    )

    db_path = _require_db(project_root)
    root = Path(project_root).resolve()

    if node_ids is not None:
        result = _api.mark_clean_nodes(db_path, root, node_ids, reason, verified_by=verified_by)
        parts = [f"Marked {len(result.marked)} nodes as AGENT_VERIFIED.\nReason: {reason}"]
        if result.marked:
            parts.append("\nNodes:\n" + "\n".join(f"- {nid}" for nid in result.marked))
        if result.not_found:
            parts.append(
                f"\n\nNot found ({len(result.not_found)}):\n" + "\n".join(f"- {nid}" for nid in result.not_found)
            )
        return "".join(parts)

    # Single-node mode
    result = _api.mark_clean_nodes(db_path, root, [node_id], reason, verified_by=verified_by)
    if result.not_found:
        return f"ERROR: Node '{node_id}' not found."
    return f"Marked '{node_id}' as AGENT_VERIFIED.\nReason: {reason}"


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def axiom_graph_purge_node(
    project_root: str,
    node_id: str,
    reason: str,
    node_ids: list[str] | None = None,
) -> str:
    """Purge one or more NOT_FOUND nodes from the index.

    Only nodes with ``own_status = 'NOT_FOUND'`` can be purged.  Doc nodes
    are cascade-deleted via ``delete_doc_by_id`` (removing sections too);
    code/other nodes use ``delete_node_by_id``.  A preserved DELETED history
    row is written with the supplied reason.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Full node ID to purge, e.g.
            ``myproject::mod.helpers::old_func``.
        reason: Human-readable reason for the purge (stored in history meta).
        node_ids: Optional list of node IDs for batch operation. When
            provided, ``node_id`` is ignored and all listed nodes are purged
            with the shared reason. Per-item errors do not abort remaining items.
    """
    if node_ids is not None:
        if not node_ids:
            return "ERROR: node_ids list is empty"
        results: list[str] = []
        for nid in node_ids:
            try:
                result = axiom_graph_purge_node(project_root, nid, reason)
            except Exception as exc:
                result = f"ERROR ({nid}): {exc}"
            results.append(result)
        return "\n\n---\n\n".join(results)

    db_path = _require_db(project_root)
    purge_results = _api.purge_nodes(db_path, [node_id], reason)
    pr = purge_results[0]
    if pr.purged:
        return f"Purged node: {node_id} (reason: {reason})"
    if pr.reason == "not_found_in_index":
        return f"ERROR: node not found in index: {node_id}"
    if pr.reason and pr.reason.startswith("status_"):
        status = pr.reason.removeprefix("status_")
        return f"ERROR: Node {node_id} has status {status}, not NOT_FOUND. Only NOT_FOUND nodes can be purged."
    return f"ERROR: failed to purge {node_id} ({pr.reason})"


# ---------------------------------------------------------------------------
# apply_rename / revert_rename
# ---------------------------------------------------------------------------


def axiom_graph_apply_rename(
    project_root: str,
    old_id: str,
    new_id: str,
) -> str:
    """Manually weld a rename the automatic matcher missed.

    Escape hatch for a real rename that fell below the similarity threshold:
    the old node became ``NOT_FOUND`` and the renamed node was indexed as a
    fresh node.  Restricted to the ``(NOT_FOUND old, newly-created new)``
    safety contract -- it refuses to weld two pre-existing identities.

    On success it migrates the old node's history, verification, and edges to
    ``new_id`` and marks ``new_id`` with ``own_status = RENAMED``.

    Args:
        project_root: Absolute path to the indexed project.
        old_id: The ``NOT_FOUND`` node being renamed *from*.
        new_id: The newly-created live node being renamed *to*.
    """
    db_path = _require_db(project_root)
    root = Path(project_root).resolve()
    result = _api.apply_rename(db_path, root, old_id, new_id)
    if result.applied:
        return f"Applied rename: {old_id} -> {new_id} (new node marked RENAMED)"
    return (
        f"ERROR: refused to apply rename {old_id} -> {new_id} "
        f"({result.reason}). Contract requires a NOT_FOUND old node and a "
        f"newly-created live new node not already involved in a rename."
    )


def axiom_graph_revert_rename(
    project_root: str,
    new_id: str,
) -> str:
    """Un-weld a previously applied rename, restoring the prior identity.

    Re-runs the recorded migration in reverse: the renamed node's history,
    verification, and edges move back to the original ID, which is restored as
    the live identity while ``new_id`` is detached as a fresh node.

    Args:
        project_root: Absolute path to the indexed project.
        new_id: The current (renamed-to) identity to revert.
    """
    db_path = _require_db(project_root)
    root = Path(project_root).resolve()
    result = _api.revert_rename(db_path, root, new_id)
    if result.reverted:
        return f"Reverted rename: restored {result.old_id} (detached {new_id})"
    return f"ERROR: cannot revert {new_id} ({result.reason}). No recorded rename for this node."


# ---------------------------------------------------------------------------
# render_site
# ---------------------------------------------------------------------------


def axiom_graph_render_site(
    project_root: str,
    build: bool = False,
    nav_path: str | None = None,
    output_dir: str | None = None,
    targets: list[str] | None = None,
) -> str:
    """Render consumer documentation site from DocJSON sources.

    Runs the same core pipeline as the ``axiom-graph render-site`` CLI command.
    With no ``nav_path``/``output_dir``, renders every configured render target
    (``[[axiom_graph.site.targets]]``) -- or the subset named in *targets* --
    in its declared flavor (plain GFM or Sphinx/MyST).  When no targets are
    configured an implicit ``guide`` (sphinx -> ``userdocs/guide``) target is
    synthesised.

    ``nav_path``/``output_dir`` are single-target ad-hoc overrides that bypass
    the target list and render one nav-driven Sphinx subtree.

    Args:
        project_root: Absolute path to the indexed project.
        build: If True, also run ``sphinx-build`` after generating files
            (sphinx-format targets only).
        nav_path: Path to site-nav.yml ad-hoc override.  Defaults to
            ``{project_root}/site-nav.yml``.
        output_dir: Directory for the generated MyST pages ad-hoc override.
            Defaults to ``{project_root}/userdocs/guide``.
        targets: Optional list of target names to render; others are skipped.

    Returns:
        Text summary listing pages rendered, warnings, and output path(s).
    """
    root = Path(project_root).resolve()
    _require_db(str(root))

    # Ad-hoc single-target override: nav_path / output_dir bypass the target list.
    if nav_path is not None or output_dir is not None:
        result = _api.render_site(
            root,
            nav_path=Path(nav_path) if nav_path else None,
            output_dir=Path(output_dir) if output_dir else None,
            run_sphinx_build=build,
        )
        lines: list[str] = [
            f"Consumer site rendered: {result.pages_rendered} page(s)",
            f"  output: {result.output_dir}",
        ]
        if result.warnings:
            lines.append(f"  warnings: {len(result.warnings)}")
            for w in result.warnings:
                lines.append(f"    ! {w}")
        return "\n".join(lines)

    # Multi-target path.
    results = _api.render_targets(root, only=list(targets) if targets else None, run_sphinx_build=build)
    lines = ["Render targets:"]
    for r in results:
        if r.skipped:
            lines.append(f"  [{r.name}] skipped")
            continue
        lines.append(f"  [{r.name}] {r.format} -> {r.output} : {r.pages_rendered} page(s)")
        for w in r.warnings:
            lines.append(f"    ! {w}")
    return "\n".join(lines)
