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


def axiom_graph_build(project_root: str, verbose: bool = False) -> str:
    """Re-index the project: add new nodes and refresh edges after code changes.

    Only nodes that have never been indexed are inserted, preserving
    ``CONTENT_UPDATED`` / ``NOT_FOUND`` signals.  Edges are updated
    in all cases.

    The project id resolves from ``axiom-graph.toml``, then the id the
    index stores, then the directory name.  When it differs from the stored
    id the build is refused and the tool returns ``ERROR: ...`` naming both.

    A full rebuild (which resets baselines and clears staleness) is
    intentionally not available through this tool -- use the CLI
    ``axiom-graph init`` after you have resolved and verified all stale
    nodes.

    To purge individual NOT_FOUND nodes, use ``axiom_graph_purge_node``.
    For bulk purge of all NOT_FOUND nodes, use the CLI
    ``axiom-graph purge --all-not-found``.

    Args:
        project_root: Absolute path to the project to index.
        verbose: When ``True``, include all warning details (ontology
            violations, scanner errors, etc.) in the output.  Default
            ``False`` shows only the warning count.
    """
    from axiom_graph.config import ProjectIdMismatchError  # noqa: PLC0415

    root = Path(project_root).resolve()
    db_path = _require_db(project_root)
    try:
        summary = _api.build_index(
            db_path,
            root,
            discovery_only=True,
            verbose=verbose,
        )
    except ProjectIdMismatchError as exc:
        return f"ERROR: {exc}"

    lines: list[str] = []
    num_warnings = len(summary.warnings)
    lines.append(
        f"axiom-graph build complete (discovery-only)\n"
        f"  files scanned   : {summary.files_scanned} (Python)\n"
        f"  files skipped   : {summary.files_skipped_mtime} (Python, content and mtime unchanged)\n"
        f"  docs skipped    : {summary.docs_skipped_mtime} (markdown + DocJSON, content and mtime unchanged)\n"
        f"  nodes added     : {summary.nodes_written}\n"
        f"  nodes unchanged : {summary.nodes_skipped}\n"
        f"  nodes renamed   : {summary.nodes_renamed}\n"
        f"  edges updated   : {summary.edges_written}\n"
        f"  edges unchanged : {summary.edges_skipped}\n"
        f"  broken links    : {summary.broken_links_flagged}\n"
        f"  warnings        : {num_warnings}"
    )
    # The unresolved-import line names its own fix, so it shows without verbose.
    shown = summary.warnings if verbose else [w for w in summary.warnings if "source_roots" in w]
    if shown:
        lines.append("")
        for w in shown:
            lines.append(f"  ! {w}")
    if summary.check is not None:
        lines.append(f"  staleness       : {summary.check.summary_line()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------


def axiom_graph_checkout(project_root: str, worktree_path: str) -> str:
    """Copy the index into a git worktree as a consistent snapshot.

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
# carry_forward
# ---------------------------------------------------------------------------


def axiom_graph_carry_forward(
    project_root: str,
    worktree_path: str,
    dry_run: bool = False,
    list_nodes: bool = False,
) -> str:
    """Copy a merged worktree's verifications into this index.

    The return trip of ``axiom_graph_checkout``.  Run on the checkout the
    worktree was merged into, after its build (and after a build and check
    in the worktree, or fewer nodes carry).  The worktree index is opened
    read-only.  A node stale here takes the worktree's verification in full,
    with the versions of the linked nodes it was checked against, when the
    worktree verified exactly what this checkout holds: VERIFIED there with
    a verification record, the same content in both indexes, and every link
    a verification settles present there with each linked node at the
    verified version (for an envelope, every annotated function and
    delegated task too).  Code, test and doc nodes alike.  A node two
    branches changed matches neither, so it stays stale.

    A node the worktree verified at the same content that is still flagged
    there, or here, for something else is carried one dimension at a time:
    its own-text verification, and the worktree's receipt for each link
    holding it stale here whose linked node is at the version the receipt
    names.  Every other link keeps its state here.

    Each carried node gets one history row with op ``carry_forward`` whose
    reason names the branch, the SHA and the worktree's latest verification
    of the node (a doc tool's write included); the worktree's history is
    not imported.  One recompute then settles the links.  The report gives
    the counts carried in full and in part, the stale count before and
    after, how many carried nodes stay stale while a node they depend on is
    itself stale here (a doc-to-doc link, or an envelope's annotated
    function or delegated task), and why each other stale node was not
    carried (not verified in the worktree, content differs, a link is absent
    in the worktree, a linked node differs, or NOT_FOUND / RENAMED /
    BROKEN_LINK).

    Refused with ``ERROR: ...``, writing nothing, when the worktree index is
    missing or the two indexes differ in schema version or project id.

    Args:
        project_root: Absolute path to the checkout the worktree was merged into.
        worktree_path: Absolute path to the worktree directory, or to its
            ``.axiom_graph/graph.db``.
        dry_run: Judge every stale node and write nothing; the report lists
            the nodes it would carry, in full and in part.  Reads the
            statuses as this checkout's last build or check stored them, so
            run it after the build.
        list_nodes: List every stale node by verdict.
    """
    db_path = _require_db(project_root)
    root = Path(project_root).resolve()
    try:
        result = _api.carry_forward_verifications(db_path, root, Path(worktree_path), dry_run=dry_run)
    except _api.CarryForwardRefusedError as exc:
        return f"ERROR: carry-forward refused: {exc}"
    return _api.render_carry_forward_report(result, list_nodes=list_nodes)


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def axiom_graph_check(project_root: str, include_frozen: bool = False, full: bool = False) -> str:
    """Summarise staleness in one line: counts per status, no node list.

    Returns a single summary line covering both dimensions of node
    health, with optional ``(all nodes VERIFIED)`` / ``(no doc-quality
    advisories)`` trailers when applicable:

        ``own: 3 CONTENT_UPDATED / 1 DESC_UPDATED / 0 NOT_FOUND ·
         link: 5 LINKED_STALE / 0 BROKEN_LINK · 42 VERIFIED · 1 DOC_SECTION_LONG``

    Files that hold functions or sections the index lacks, or that lost
    ones the index holds, add one line each, e.g. ``utils.py has 2 new
    functions — run build`` or ``utils.py has 1 indexed function no
    longer found — run build``.

    For per-node detail, paginated lists, filtered slices, or grouped
    aggregates use ``axiom_graph_drift_query``.

    Args:
        project_root: Absolute path to the indexed project.
        include_frozen: When ``False`` (the default), sections under
            docs tagged in ``config.staleness.frozen_tags`` are
            excluded from the summary counts.  When ``True`` they are
            included.  No-op when ``frozen_tags`` is empty.
        full: When ``False`` (the default), only what changed since the
            last check is recomputed.  When ``True`` every node is
            recomputed from a re-hash of every file.

    Note:
        ``verbose`` and ``filter`` parameters were removed in the
        2026-05 drift_query cycle.  Calling with those keyword
        arguments raises ``TypeError`` -- migrate to
        ``axiom_graph_drift_query`` instead.
    """
    root = Path(project_root).resolve()
    db_path = _require_db(project_root)
    cs = _api.compute_check_summary(db_path, root, include_frozen=include_frozen, full=full)

    if cs is None:
        return "(no nodes in index)"

    summary = cs.summary_line()
    if cs.doc_quality_count:
        summary += f" · {cs.doc_quality_count} DOC_SECTION_LONG"

    if cs.all_clean and not cs.doc_quality_count:
        summary += "\n(all nodes VERIFIED)"
    lines = _api.structure_lines(cs.structure)
    if lines:
        summary += "\n" + "\n".join(lines)
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
        elif ct == "RAW_DOCJSON_EDIT":
            desc = "raw DocJSON edit — edited outside the doc tools; re-apply with a doc tool or accept it"
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
    """List the checkpoints and SHAs that report(since_sha=...) can start from.

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
    verbose: bool | None = None,
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
    detail: str | None = None,
    exclude_node_pattern: str | list[str] | None = None,
    max_chars: int | None = 40_000,
) -> str:
    """Impact report: what changed since a checkpoint, SHA, or datetime.

    Summarises content changes, staleness transitions, link modifications,
    and verification activity recorded in ``node_history``.

    **Reference point resolution** (first match wins):

    1. ``since_sha`` -- match a CHECKPOINT by git_sha prefix (the match is
       symmetric: a full 40-char SHA matches a 12-char checkpoint).
    2. ``since_sha`` -- match any history row by git_sha prefix.
    3. ``since_sha`` -- not in the index but a commit git knows: its git
       commit time is the cutoff (the header says so).
    4. ``since_timestamp`` -- ISO-8601 datetime used directly as cutoff.
    5. Neither given -- most recent CHECKPOINT, then most recent history
       row with a git_sha; with neither, the whole history.

    An explicit ``since_sha`` that neither the index nor git can resolve
    (unknown, ambiguous, or shorter than 4 characters) returns an
    ``ERROR: ...`` string -- never a report over a different window.

    Every response starts with a ``reference:`` line naming the SHA,
    cutoff time and how it was resolved, then the one-line headline.

    Args:
        project_root: Absolute path to the indexed project.
        since_sha: Git SHA prefix (4+ characters).
        since_timestamp: ISO-8601 datetime cutoff (e.g.
            ``2026-03-18T00:00:00``).
        verbose: Deprecated -- use ``detail``.  ``True`` maps to
            ``detail="full"``; ignored when ``detail`` is given.
        change_type_pattern: Glob pattern to filter change types (e.g.
            ``*STALE*``, ``LINK_*``, ``AGENT_*``, ``INITIAL``).
        node_pattern: Glob pattern to filter node IDs (e.g.
            ``axiom_graph::axiom_graph.viz.*``).
        node_type: Filter to nodes of this type. One of
            ``atomic_process``, ``composite_process``, or ``entity``.
        detail: ``"summary"`` (default: reference + headline),
            ``"condensed"`` (aggregated by container, hand-made changes
            verbatim) or ``"full"`` (one line per history row).
        exclude_node_pattern: Glob (or list of globs) for node IDs to leave
            out of every section and the headline counts -- e.g. the cycle
            manifest written during the window: ``"{doc_id}*"``.
        max_chars: Maximum characters in the response (default 40 000).
            Longer output is cut at a line boundary with a footer saying
            how many lines were dropped.  Pass ``None`` for no cap.
    """
    if detail is None:
        detail = "full" if verbose else "summary"
    if detail not in _api.REPORT_DETAILS:
        return f"ERROR: detail must be one of {', '.join(_api.REPORT_DETAILS)}; got {detail!r}"
    db_path = _require_db(project_root)
    try:
        data = _api.compute_report(
            db_path,
            since_sha=since_sha,
            since_timestamp=since_timestamp,
            change_type_pattern=change_type_pattern,
            node_pattern=node_pattern,
            node_type=node_type,
            exclude_node_pattern=exclude_node_pattern,
            project_root=Path(project_root).resolve(),
        )
    except _api.UnresolvedReferenceError as exc:
        return f"ERROR: {exc}"
    return _api.cap_report_text(_api.render_report_text(data, detail), max_chars, detail)


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

    1. A node that went stale (CONTENT_UPDATED, DESC_UPDATED or
       LINKED_STALE) and has not been verified since: the newest history
       row with a ``git_sha`` from *before* it went stale.  A checkpoint
       taken while it was already stale is skipped, since that commit
       already holds the change.  No such row is a ``no_baseline`` error.
    2. Any other node: the most recent ``AGENT_VERIFIED``,
       ``MANUAL_VERIFIED``, or ``CHECKPOINT`` history row with a non-NULL
       ``git_sha``; else the oldest history row with a ``git_sha``
       (typically the ``INITIAL`` scan -- gives "diff since first indexed").

    ``baseline_reason`` in the response says which rule picked the
    baseline.  Pass *baseline_sha* explicitly to diff against a specific
    commit (``baseline_reason`` is then ``"given"``).

    Renames are followed: a file renamed or moved since the baseline
    (committed, or staged with ``git mv`` / ``git add``) diffs against its
    old path, reported as ``baseline_path`` beside the current ``path``.
    ``baseline_path`` is ``null`` when the file is new since the baseline.
    When the file is missing at the baseline and git cannot tell whether it
    was renamed, the response is an error
    (``"error": "baseline_path_unresolved"``), never an all-new diff.

    The node is found by identity in each side's file, never cut at its
    indexed line range: code is re-scanned with the indexer's scanner (the
    baseline falls back to the node's prior ids from rename history), and a
    DocJSON section diffs as its own heading and content.  A node new since
    the baseline has an empty ``old_content``.  When the node's position in
    either side cannot be determined (e.g. the baseline file does not parse)
    the response is ``{"error": "node_position_unresolved", "reason": ...}``,
    never a diff cut from the wrong lines.

    When ``summary_only`` is ``False`` (default), the response is
    JSON-formatted text with keys: ``node_id``, ``baseline_sha``,
    ``baseline_date``, ``baseline_reason``, ``path``, ``baseline_path``,
    ``old_content``, ``new_content``, ``summary``.

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
        return _api.NODE_DIFF_BATCH_DELIMITER.join(parts)

    db_path = _require_db(project_root)
    root = Path(project_root).resolve()
    report = _api.node_diff_report(db_path, root, node_id, baseline_sha=baseline_sha, summary_only=summary_only)
    return _api.format_node_diff_report(report)


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
    """Mark nodes verified after reviewing them, clearing their drift.

    Records an AGENT_VERIFIED history row per node. Nodes appear in
    ``axiom-graph history agent-verified`` for pre-push human review. Use only
    when you have read the current code and documentation and confirmed
    they are consistent.

    It records a verification for the node it names: LINKED_STALE on that node
    clears when each linked node is at the version the verification recorded.
    A link it recorded no version for (an older verification from before
    versions were recorded, or a link added since) falls back to the time
    rule: it clears when the verification is newer than the linked node's
    last change.

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
        if result.inherited:
            parts.append(
                f"\n\nInherited LINKED_STALE — no direct effect ({len(result.inherited)}):\n"
                + "\n".join(
                    f"- {nid} (stale descendants: {', '.join(descs)})" for nid, descs in result.inherited.items()
                )
                + "\nMark the stale descendants clean to clear these aggregates."
            )
        if result.mixed:
            parts.append(
                f"\n\nOwn signal cleared; inherited LINKED_STALE remains ({len(result.mixed)}):\n"
                + "\n".join(f"- {nid} (stale descendants: {', '.join(descs)})" for nid, descs in result.mixed.items())
            )
        if result.not_found:
            parts.append(
                f"\n\nNot found ({len(result.not_found)}):\n" + "\n".join(f"- {nid}" for nid in result.not_found)
            )
        return "".join(parts)

    # Single-node mode
    result = _api.mark_clean_nodes(db_path, root, [node_id], reason, verified_by=verified_by)
    if result.not_found:
        return f"ERROR: Node '{node_id}' not found."
    if node_id in result.inherited:
        descs = result.inherited[node_id]
        return (
            f"Marked '{node_id}' as AGENT_VERIFIED, but its LINKED_STALE is "
            f"inherited — no direct effect.\n"
            f"LINKED_STALE is inherited from stale descendants:\n"
            + "\n".join(f"- {d}" for d in descs)
            + "\nMark those descendants clean (or reverify the node they depend on) to clear it.\n"
            f"Reason: {reason}"
        )
    if node_id in result.mixed:
        descs = result.mixed[node_id]
        return (
            f"Marked '{node_id}' as AGENT_VERIFIED.\n"
            f"Own stale signal cleared; LINKED_STALE inherited from stale descendants remains:\n"
            + "\n".join(f"- {d}" for d in descs)
            + f"\nReason: {reason}"
        )
    return f"Marked '{node_id}' as AGENT_VERIFIED.\nReason: {reason}"


#: How many skipped dependents the default reverify report lists before a
#: ``+N more`` line; ``verbose=True`` lists every one.
REVERIFY_SKIPPED_CAP = 20

#: How many outstanding offenders the default report names per skipped
#: dependent, and in the combined marked-clean note, before ``+N more``.
REVERIFY_OFFENDERS_CAP = 3

#: How many unknown batch ids the default report lists under ``Not found``
#: before a ``+N more`` line.
REVERIFY_NOT_FOUND_CAP = 10

#: The phrase naming the scope of reverify's before/after counts.
REVERIFY_COUNT_SCOPE = "as check counts it, frozen docs excluded"


def axiom_graph_reverify(
    project_root: str,
    node_id: str,
    reason: str,
    verified_by: str = "agent",
    node_ids: list[str] | None = None,
    verbose: bool = False,
) -> str:
    """Verify a node and clear the LINKED_STALE it caused, in one operation.

    Assertion semantics: "I verified this node; my change to it does not
    invalidate its dependents."  Composite sources (doc envelopes,
    modules, sections with child sections) are expanded to their full
    subtree; transitive doc-to-doc chains are resolved back to their
    root offender before clearing.

    The call starts with the refresh ``check`` runs, so edits on disk that
    no build has recorded count.  Skip rule: a dependent that is also stale
    via *other* root offenders (an unbuilt edit included) is conservatively
    left LINKED_STALE and reported as skipped — clear it by reverifying the
    other offenders (or an explicit mark_clean).

    A cleared dependent whose own content or docstring changed and was not
    reviewed gets a link-only verification: its LINKED_STALE clears, its
    own status stays CONTENT_UPDATED / DESC_UPDATED, and the report counts
    it under "own changes kept".  Other dependents are verified in full and
    carry ``[reverify:<source>]`` provenance in their history rows.

    The operation finishes with a staleness recompute.  The before and
    after LINKED_STALE counts are the ones ``check`` shows (frozen docs
    excluded, their doc nodes too).  The default report is compact: it
    leads with how many LINKED_STALE nodes the call cleared, gives counts,
    and lists only the skipped dependents with what still holds them, each
    list capped with a ``+N more`` line (skipped dependents, the offenders
    per dependent, unknown batch ids); marked-clean notes cover only the
    offenders shown.  ``verbose=True`` lists everything and adds the source,
    cleared and own-change-kept lists and how many aggregates settled by
    inheritance; "cleared" lists only nodes this call verified.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node you verified (the staleness root).
        reason: Brief explanation of why dependents remain accurate.
        verified_by: Identifier for the verifier.  Defaults to
            ``'agent'``; pass the model name for traceability.
        node_ids: Optional list of sources for batch operation.  When
            provided, ``node_id`` is ignored: the union of every source's
            subtree is treated as one source set, the staleness recompute
            runs once, and one report covers the batch (unknown IDs are
            listed under ``Not found``, not fatal).
        verbose: List every source, cleared node, own-change-kept node and
            skipped dependent.
    """
    logger.debug(
        "axiom_graph_reverify: node_id=%s, batch=%s",
        node_id,
        len(node_ids) if node_ids else "no",
    )

    db_path = _require_db(project_root)
    root = Path(project_root).resolve()

    if node_ids is not None:
        batch = _api.reverify_nodes(db_path, root, node_ids, reason, verified_by=verified_by)
        return _format_reverify(batch, reason, verbose=verbose)

    result = _api.reverify_node(db_path, root, node_id, reason, verified_by=verified_by)
    if result.not_found:
        return f"ERROR: Node '{node_id}' not found."
    return _format_reverify(result, reason, verbose=verbose, single=node_id)


def _id_block(title: str, ids: list[str]) -> str:
    return f"\n{title} ({len(ids)}):\n" + "\n".join(f"- {nid}" for nid in ids)


def _format_reverify(result, reason: str, *, verbose: bool, single: str | None = None) -> str:
    """Render a reverify report, single source or batch.

    Args:
        result: A :class:`~axiom_graph.lifecycle.api.ReverifyResult` (with
            *single*) or :class:`~axiom_graph.lifecycle.api.ReverifyBatchResult`.
        reason: The shared reason, echoed on the last line.
        verbose: Add the source, cleared and own-change-kept lists, the
            settled count, and every skipped dependent.
        single: The source id of a single-source call.

    Returns:
        Plain-text report whose first line leads with the cleared count.
    """
    sources = [single] if single is not None else list(result.sources)
    cascade_count = len(result.verified) - len(sources)
    cleared, kept, skipped = list(result.cleared), list(result.own_change_kept), dict(result.skipped)
    if single is not None:
        what = f"Reverified '{single}' — source verified"
        nothing = (_api.REVERIFY_NOTHING_CLEARED_SKIPPED, _api.REVERIFY_NOTHING_TO_CLEAR)
        skip_title = "Skipped — also stale via other offenders"
    else:
        what = f"Reverified {len(sources)} source(s)"
        nothing = (_api.REVERIFY_BATCH_NOTHING_CLEARED_SKIPPED, _api.REVERIFY_BATCH_NOTHING_TO_CLEAR)
        skip_title = "Skipped — also stale via offenders outside the batch"
    parts = [
        f"Cleared {len(cleared)} LINKED_STALE node(s). "
        + what
        + (f", {cascade_count} dependent(s) cascade-verified." if cascade_count else "."),
    ]
    if sources:
        parts.append(
            f"Sources: {len(sources)} · cleared: {len(cleared)} · skipped: {len(skipped)} · "
            f"own changes kept: {len(kept)}"
        )
        parts.append(
            f"LINKED_STALE before: {result.before_linked_stale} -> after: {result.after_linked_stale} "
            f"({REVERIFY_COUNT_SCOPE})"
        )
        if not cleared:
            parts.append(f"\n{nothing[0] if skipped else nothing[1]}")
        if kept and not verbose:
            parts.append(
                f"{len(kept)} cleared dependent(s) kept their own change: review them (verbose=true lists them)."
            )
        if verbose:
            parts.append(_id_block("Sources", sources))
            if cleared:
                parts.append(_id_block("Cleared", cleared))
            if kept:
                parts.append(_id_block("Own change kept — links verified, own change still to review", kept))
            if result.settled:
                parts.append(f"Settled by inheritance (not listed as cleared): {len(result.settled)}")
    if skipped:
        rows = sorted(skipped.items())
        shown = rows if verbose else rows[:REVERIFY_SKIPPED_CAP]
        named: set[str] = set()
        lines = []
        for nid, offs in shown:
            offs_shown = offs if verbose else offs[:REVERIFY_OFFENDERS_CAP]
            named.update(offs_shown)
            lines.append(f"- {nid} (other offenders: {_capped_ids(offs_shown, len(offs))})")
        if len(rows) > len(shown):
            lines.append(f"(+{len(rows) - len(shown)} more; pass verbose=true for all)")
        parts.append(f"\n{skip_title} ({len(rows)}):\n" + "\n".join(lines) + f"\n{_api.REVERIFY_SKIP_HINT}")
        parts.extend(_marked_clean_notes(result.marked_clean_offenders, named, verbose=verbose))
    not_found = list(getattr(result, "not_found", []) or []) if single is None else []
    if not_found:
        nf_shown = not_found if verbose else not_found[:REVERIFY_NOT_FOUND_CAP]
        block = f"\nNot found ({len(not_found)}):\n" + "\n".join(f"- {nid}" for nid in nf_shown)
        if len(not_found) > len(nf_shown):
            block += f"\n(+{len(not_found) - len(nf_shown)} more; pass verbose=true for all)"
        parts.append(block)
    parts.append(f"Reason: {reason}")
    return "\n".join(parts)


def _capped_ids(shown: list[str], total: int) -> str:
    """Join *shown* ids, adding ``+N more`` when *total* exceeds them."""
    more = total - len(shown)
    return ", ".join(shown) + (f" +{more} more" if more > 0 else "")


def _marked_clean_notes(marked: list[str], named: set[str], *, verbose: bool) -> list[str]:
    """Notes for skipped dependents' offenders that were marked clean, not reverified.

    Verbose mode prints one note per offender.  The compact report covers
    only the offenders its shown rows name, so the notes never grow with
    the hidden rows, and several collapse into one line.

    Args:
        marked: Every marked-clean offender behind a skipped dependent.
        named: The offenders the report's shown skip rows name.
        verbose: Print one note per offender in *marked*.

    Returns:
        The note lines, possibly empty.
    """
    if verbose:
        return [_api.REVERIFY_MARKED_CLEAN_NOTE.format(offender=o) for o in marked]
    shown = [o for o in marked if o in named]
    if len(shown) <= 1:
        return [_api.REVERIFY_MARKED_CLEAN_NOTE.format(offender=o) for o in shown]
    listed = _capped_ids(shown[:REVERIFY_OFFENDERS_CAP], len(shown))
    return [_api.REVERIFY_MARKED_CLEAN_COMBINED_NOTE.format(count=len(shown), offenders=listed)]


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def axiom_graph_purge_node(
    project_root: str,
    node_id: str,
    reason: str,
    node_ids: list[str] | None = None,
) -> str:
    """Remove NOT_FOUND nodes (deleted code or docs) from the index.

    Only nodes with ``own_status = 'NOT_FOUND'`` can be purged.  A module,
    DocJSON doc or config node whose file is still on disk is refused: its
    NOT_FOUND is inherited from the NOT_FOUND nodes in that file, which the
    error lists to purge if they were really removed.  Any node of a Python,
    DocJSON or JS/TS file that is on disk but does not parse (for JS/TS, a
    file tree-sitter parses with errors), a function or section
    as well as the module or doc, is refused with an error saying the file
    does not parse; it lists nothing (fix the file and re-run check, do not
    purge its nodes).  Doc nodes are
    cascade-deleted via ``delete_doc_by_id`` (removing sections too);
    code/other nodes use ``delete_node_by_id``.  A preserved DELETED history
    row is written with actor ``agent`` and the supplied reason.

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
    purge_results = _api.purge_nodes(db_path, Path(project_root).resolve(), [node_id], reason, actor="agent")
    pr = purge_results[0]
    if pr.purged:
        return f"Purged node: {node_id} (reason: {reason})"
    if pr.reason == "not_found_in_index":
        return f"ERROR: node not found in index: {node_id}"
    if pr.reason == _api.PURGE_REFUSED_INHERITED:
        children = "".join(f"\n- {c}" for c in pr.deleted_children)
        return f"ERROR: Not purged: {node_id} -- {_api.PURGE_INHERITED_HINT}.{children}"
    if pr.reason == _api.PURGE_REFUSED_UNPARSEABLE:
        return f"ERROR: Not purged: {node_id} -- {_api.PURGE_UNPARSEABLE_HINT}."
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
    """Record a rename the automatic matcher missed, keeping the node's history.

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
        return f"Applied rename: {old_id} -> {new_id} (new node marked RENAMED)" + _unpatched_links_note(
            result, old_id, new_id
        )
    return (
        f"ERROR: refused to apply rename {old_id} -> {new_id} "
        f"({result.reason}). Contract requires a NOT_FOUND old node and a "
        f"newly-created live new node not already involved in a rename."
    )


def _unpatched_links_note(result: _api.RenameApplyResult | _api.RenameRevertResult, old_id: str, new_id: str) -> str:
    """The lines naming DocJSON files a rename could not re-point or could not check, or ``""``."""
    lines = _api.link_rewrite_note(result.links_unreadable, result.links_not_patched, old_id, new_id)
    return "".join(f"\nWARNING: {line}" for line in lines)


def axiom_graph_revert_rename(
    project_root: str,
    new_id: str,
) -> str:
    """Undo an applied rename, restoring the node's prior identity.

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
        return f"Reverted rename: restored {result.old_id} (detached {new_id})" + _unpatched_links_note(
            result, new_id, result.old_id or ""
        )
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
    """Render the configured consumer doc targets (Sphinx pages, README) from docs.

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
