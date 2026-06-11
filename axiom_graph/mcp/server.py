"""axiom-graph MCP server -- FastMCP wrapper around the axiom-graph index.

Tool implementations live in domain-specific sub-modules, grouped by concern:
    axiom_graph.query.mcp_tools       -- read-only index query tools
    axiom_graph.docjson.mcp_tools     -- document manipulation tools
    axiom_graph.lifecycle.mcp_tools   -- build, staleness, verification, history tools
    axiom_graph.workflows.mcp_tools   -- workflow/task envelope inspection tools

This file contains only:
    - The FastMCP app instance
    - Tool registration (``@mcp.tool()`` wrappers)
    - The ``run()`` entry point

Each registration is a thin ``functools.wraps``-style passthrough to the
matching ``<domain>.mcp_tools`` function, decorated with ``_timed_tool``.
The MCP-client-visible docstrings live on the registered wrappers
themselves (FastMCP reads them at registration time) and are kept
byte-identical to the per-domain implementation docstrings.

Entry point registered in pyproject.toml as:
    axiom-graph-mcp = "axiom_graph.mcp_server:run"
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP


def _copy_doc(impl):
    """Decorator that copies ``__doc__`` from ``impl`` onto the wrapper.

    Unlike :func:`functools.wraps`, this does NOT set ``__wrapped__`` --
    so :mod:`inspect.signature` continues to introspect the wrapper's
    signature, not the impl's.  This matters for ``axiom_graph_search``
    where the impl exposes a private ``_embedder_thread`` parameter that
    FastMCP rejects (FastMCP forbids leading-underscore parameters).
    The wrapper deliberately omits ``_embedder_thread`` from its public
    signature and supplies the runtime value internally.
    """

    def decorator(wrapper):
        wrapper.__doc__ = impl.__doc__
        wrapper.__module__ = impl.__module__
        return wrapper

    return decorator


logger = logging.getLogger(__name__)

mcp = FastMCP("axiom-graph")


# ---------------------------------------------------------------------------
# Eager embedder warm-up (background thread)
# ---------------------------------------------------------------------------


def _warm_embedder() -> None:
    """Pre-load the embedding model so the first semantic search doesn't hang.

    Honors ``AXIOM_GRAPH_SKIP_EMBEDDINGS=1`` (same flag the build path checks
    in ``axiom_graph/index/builder.py``) so users who never opt into semantic
    search don't pay startup latency -- or hang on a degraded HuggingFace
    cache -- for a deprecated feature (ADR-020).
    """
    if os.environ.get("AXIOM_GRAPH_SKIP_EMBEDDINGS", "").strip() == "1":
        logger.info("MCP server: skipping embedder warm-up (AXIOM_GRAPH_SKIP_EMBEDDINGS=1)")
        return
    try:
        from axiom_graph.index.embeddings import get_embedder, is_available

        if is_available():
            get_embedder()
            logger.info("Embedding model pre-loaded")
    except Exception as exc:
        logger.debug("Embedder warm-up failed: %s", exc)


_embedder_thread = threading.Thread(target=_warm_embedder, daemon=True)
_embedder_thread.start()


# ---------------------------------------------------------------------------
# Import tool implementations from sub-modules
# ---------------------------------------------------------------------------

from axiom_graph.mcp._helpers import _timed_tool  # noqa: E402

# -- Query tools --
from axiom_graph.query.mcp_tools import (  # noqa: E402
    axiom_graph_sql as _impl_sql,
    axiom_graph_render as _impl_render,
    axiom_graph_list as _impl_list,
    axiom_graph_graph as _impl_graph,
    axiom_graph_search as _impl_search,
    axiom_graph_source as _impl_source,
    axiom_graph_list_tags as _impl_list_tags,
    axiom_graph_list_undocumented as _impl_list_undocumented,
    axiom_graph_drift_query as _impl_drift_query,
)

# -- Doc tools --
from axiom_graph.docjson.mcp_tools import (  # noqa: E402
    axiom_graph_write_doc as _impl_write_doc,
    axiom_graph_read_doc as _impl_read_doc,
    axiom_graph_update_section as _impl_update_section,
    axiom_graph_patch_section as _impl_patch_section,
    axiom_graph_add_section as _impl_add_section,
    axiom_graph_delete_section as _impl_delete_section,
    axiom_graph_add_link as _impl_add_link,
    axiom_graph_delete_link as _impl_delete_link,
    axiom_graph_delete_doc as _impl_delete_doc,
    axiom_graph_update_doc_meta as _impl_update_doc_meta,
)

# -- Lifecycle tools --
from axiom_graph.lifecycle.mcp_tools import (  # noqa: E402
    axiom_graph_build as _impl_build,
    axiom_graph_checkout as _impl_checkout,
    axiom_graph_check as _impl_check,
    axiom_graph_history as _impl_history,
    axiom_graph_list_reference_points as _impl_list_reference_points,
    axiom_graph_report as _impl_report,
    axiom_graph_diff as _impl_diff,
    axiom_graph_mark_clean as _impl_mark_clean,
    axiom_graph_purge_node as _impl_purge_node,
    axiom_graph_apply_rename as _impl_apply_rename,
    axiom_graph_revert_rename as _impl_revert_rename,
    axiom_graph_render_site as _impl_render_site,
)

# -- Workflow tools --
from axiom_graph.workflows.mcp_tools import (  # noqa: E402
    axiom_graph_workflow_list as _impl_workflow_list,
    axiom_graph_workflow_detail as _impl_workflow_detail,
)


# ---------------------------------------------------------------------------
# Register tools with FastMCP
#
# Each tool is wrapped with @mcp.tool() and @_timed_tool. The implementation
# lives in the sub-module; the wrapper here just delegates. This keeps tool
# metadata (docstrings, type hints) on the implementation functions where
# FastMCP reads them via the wrapper's functools.wraps.
# ---------------------------------------------------------------------------

# -- Query tools --


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_sql)
def axiom_graph_sql(project_root: str, query: str, max_results: int = 50, max_rows: int | None = None) -> str:
    effective = max_rows if max_rows is not None else max_results
    return _impl_sql(project_root, query, effective)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_render)
def axiom_graph_render(
    project_root: str,
    level: int,
    node_id: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_render(project_root, level, node_id, max_results, offset)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_list)
def axiom_graph_list(
    project_root: str,
    node_type: str | None = None,
    tag: str | None = None,
    parent_id: str | None = None,
    location: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_list(project_root, node_type, tag, parent_id, location, max_results, offset)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_graph)
def axiom_graph_graph(
    project_root: str,
    node_id: str = "",
    direction: str = "out",
    depth: int = 1,
    max_results: int = 40,
    node_ids: list[str] | None = None,
    offset: int = 0,
) -> str:
    return _impl_graph(project_root, node_id, direction, depth, max_results, node_ids, offset=offset)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_search)
def axiom_graph_search(
    project_root: str,
    query: str,
    level: int | None = None,
    max_results: int = 20,
    node_type: str | None = None,
    mode: str = "keyword",
    scope: str = "all",
    tag: str | None = None,
    offset: int = 0,
) -> str:
    return _impl_search(
        project_root,
        query,
        level,
        max_results,
        node_type,
        mode,
        scope,
        tag,
        offset=offset,
        _embedder_thread=_embedder_thread,
    )


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_source)
def axiom_graph_source(
    project_root: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
    max_chars: int = 40_000,
) -> str:
    return _impl_source(project_root, node_id, node_ids, max_chars)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_list_tags)
def axiom_graph_list_tags(project_root: str) -> str:
    return _impl_list_tags(project_root)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_list_undocumented)
def axiom_graph_list_undocumented(
    project_root: str,
    node_type: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    return _impl_list_undocumented(project_root, node_type, max_results, offset)


# -- Doc tools --


@mcp.tool()
@_timed_tool
def axiom_graph_write_doc(project_root: str, doc_json: str | dict) -> str:
    """Write a DocJSON documentation file and register it in the index.

    Accepts a JSON string or dict describing a documentation document.  The
    file is written under the project's primary docs directory (the first
    entry of ``[axiom_graph.scan].docs_dirs`` in ``axiom-graph.toml``,
    falling back to ``docs/``) and immediately indexed.

    **The 'id' field is a path-slug filename hint, NOT the canonical node id.**
    The canonical node id is derived later by the indexer from the file's
    path. Common mistake: passing the indexed node id back as input.

        # ✅ correct — path-slug, supports subdirs
        {"id": "pev/instances/my-instance", ...}

        # ❌ wrong — node-id form (rejected with an error)
        {"id": "axiom_graph::docs.pev.instances.my-instance", ...}

    The 'id' field is stripped from the JSON before writing.

    Args:
        project_root: Absolute path to the indexed project.
        doc_json: JSON string or dict with keys: ``title``, ``sections``
            (required) and optionally ``tags``.

    Returns:
        Summary: sections written, links registered, and any unknown node_ids.
        Or an ``ERROR: ...`` string when validation fails.
    """
    return _impl_write_doc(project_root, doc_json)


@mcp.tool()
@_timed_tool
def axiom_graph_read_doc(
    project_root: str,
    doc_id: str = "",
    section: str | None = None,
    doc_ids: list[str] | None = None,
) -> str:
    """Read a DocJSON document as Markdown.

    Each section heading is annotated with its full section ID in an HTML
    comment (e.g. ``<!-- id: myproject::docs.architecture::overview -->``),
    so you can pass that ID directly to ``axiom_graph_update_section`` or
    ``axiom_graph_add_link`` without a separate lookup step.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
            Pass ``"list"`` to see all available doc IDs.
        section: Optional short slug to read a single section, e.g.
            ``"problem"`` or ``"architecture"``.  The slug is matched against
            the tail of each section ID (the part after the last ``::``).
            If multiple sections match, all are returned with a disambiguation
            header listing the full section IDs so you can re-call with the
            exact slug or switch to the full section ID in
            ``axiom_graph_update_section``.  Omit to read the full document.
        doc_ids: Optional list of doc IDs for batch operation.  When provided,
            ``doc_id`` is ignored and results are returned for all listed IDs
            with per-ID delimiters.
    """
    return _impl_read_doc(project_root, doc_id, section, doc_ids)


@mcp.tool()
@_timed_tool
def axiom_graph_update_section(
    project_root: str,
    section_id: str,
    content: str | None = None,
    heading: str | None = None,
    new_id: str | None = None,
    after: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Update a single section's content, heading, or ID in a DocJSON file.

    Loads the section's JSON file, replaces the specified fields, writes the
    file back, and re-indexes.  The caller provides plain markdown content --
    no need to construct full DocJSON.

    Supports dot-path section IDs for nested sections (e.g.
    ``database-layer.tables``).

    When ``new_id`` is provided, the section is renamed.  Child section IDs
    are cascaded (dot-path prefix replacement).  The new ID must be slug-safe
    (lowercase alphanumeric plus hyphens) and must not collide with an
    existing sibling.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer`` or
            ``myproject::docs.architecture::database-layer.tables``.
        content: New markdown content for the section.  If omitted, content
            is left unchanged.
        heading: New heading for the section.  If omitted, heading is left
            unchanged.
        new_id: New short slug for the section ID (e.g. ``"rest-api"``).
            Must be slug-safe.  Children are cascaded.
        after: Optional short sibling ID to reorder after.  Moves the target
            section to the position immediately after the named sibling within
            the same parent.  Within-parent only -- referencing a sibling
            under a different parent returns an error.
        tags: Optional list of tag strings to set on the section.  Pass an
            empty list to clear tags.  Tags are written to the section object
            in the JSON file and picked up by re-indexing.
    """
    return _impl_update_section(project_root, section_id, content, heading, new_id, after, tags)


@mcp.tool()
@_timed_tool
def axiom_graph_patch_section(
    project_root: str,
    section_id: str,
    new_string: str,
    anchor: str | None = None,
    old_string: str | None = None,
) -> str:
    """Partially edit a section's content (append / prepend / unique-match replace).

    A lightweight companion to ``axiom_graph_update_section``: instead of
    whole-replacing the section content, mutate only a slice of it.  The final
    on-disk content, ``desc_hash``, staleness, and re-indexing are identical to
    the equivalent whole-replace -- this is purely an input-ergonomics
    optimisation (cheaper edits, no read-modify-write clobber risk for
    append-mostly sections like ledgers and friction logs).

    Exactly one of ``anchor`` / ``old_string`` must be supplied:

    - **append** (``anchor="$"``) -- concatenate ``new_string`` at the section
      end.  No need to know the existing content.
    - **prepend** (``anchor="^"``) -- concatenate ``new_string`` at the section
      start.  No need to know the existing content.
    - **replace** (``old_string=...``) -- ``Edit``-style unique-substring
      replacement of ``old_string`` with ``new_string``.  Missing or non-unique
      match is a hard error and the section is left unchanged.

    The ``^`` / ``$`` mnemonics line up with regex anchors but are out-of-band
    parameters, never embedded in ``new_string`` -- a section body containing
    ``$VAR``, ``$x^2$``, or ``Ctrl-^`` round-trips untouched.  Append / prepend
    insert exactly one ``\\n`` separator at the join (skipped when the leading
    side already ends with ``\\n``); into an empty section they just set the
    content.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID (dot-path notation supported for
            nested sections), e.g. ``myproject::docs.architecture::api.errors``.
        new_string: Content to add (append / prepend) or replacement string
            (replace).  Inserted verbatim -- never scanned for anchor sentinels.
            Mirrors ``Edit``'s ``old_string`` / ``new_string`` pair.
        anchor: ``"$"`` to append at the end, ``"^"`` to prepend at the start.
            Mutually exclusive with ``old_string``.
        old_string: Replace-mode target; must match exactly once within the
            section's current content.  Mutually exclusive with ``anchor``.
    """
    return _impl_patch_section(project_root, section_id, new_string, anchor, old_string)


@mcp.tool()
@_timed_tool
def axiom_graph_add_section(
    project_root: str,
    doc_id: str,
    section_id: str,
    heading: str,
    content: str | None = None,
    parent_id: str | None = None,
    after: str | None = None,
) -> str:
    """Add a new section to an existing DocJSON document.

    Appends a section to the end of the document (or after a specified
    sibling).  When ``parent_id`` is given, the section is added as a child
    of that parent section instead of at the top level.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
        section_id: Short slug for the new section (e.g. ``"new-section"``).
            Must be slug-safe (lowercase alphanumeric plus hyphens, no dots).
        heading: Heading text for the new section.
        content: Optional markdown content for the new section.
        parent_id: Optional dot-path of the parent section to nest under.
            If omitted, the section is added at the top level.
        after: Optional short sibling ID to insert after.  If omitted, the
            section is appended at the end.
    """
    return _impl_add_section(project_root, doc_id, section_id, heading, content, parent_id, after)


@mcp.tool()
@_timed_tool
def axiom_graph_delete_section(project_root: str, section_id: str) -> str:
    """Delete a section (and all nested children) from a DocJSON document.

    This is a destructive operation. The section is removed from the JSON
    file on disk, and all corresponding DB rows (nodes, edges, doc_sections)
    are cleaned up.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer`` or
            ``myproject::docs.architecture::database-layer.tables``.
    """
    return _impl_delete_section(project_root, section_id)


@mcp.tool()
@_timed_tool
def axiom_graph_add_link(
    project_root: str,
    section_id: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Add link(s) from a doc section to code node(s).

    Loads the section's JSON file, appends the link(s), writes the file back,
    and re-indexes once.  When ``node_ids`` is provided, all links are added
    in a single pass with one re-index -- much faster than calling this tool
    N times.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer``.
        node_id: The code node ID to link to (single-link mode).
        node_ids: List of code node IDs to link to (batch mode).
            When provided, ``node_id`` is ignored.
    """
    return _impl_add_link(project_root, section_id, node_id, node_ids)


@mcp.tool()
@_timed_tool
def axiom_graph_delete_link(
    project_root: str,
    section_id: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Remove link(s) from a doc section to code node(s).

    This is a destructive operation. The ``documents`` edge(s) are removed.
    Other links and content in the section are untouched.  When ``node_ids``
    is provided, all matching links are removed in a single pass with one
    re-index.

    Args:
        project_root: Absolute path to the indexed project.
        section_id: Full qualified section ID, e.g.
            ``myproject::docs.architecture::database-layer``.
        node_id: The code node ID to unlink (single-link mode).
        node_ids: List of code node IDs to unlink (batch mode).
            When provided, ``node_id`` is ignored.
    """
    return _impl_delete_link(project_root, section_id, node_id, node_ids)


@mcp.tool()
@_timed_tool
def axiom_graph_delete_doc(project_root: str, doc_id: str) -> str:
    """Delete an entire DocJSON document, its JSON file, and all DB artifacts.

    This is a destructive operation. The JSON file is deleted from disk, and
    all DB rows (nodes, edges, sections, tags, FTS, history) are removed.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
    """
    return _impl_delete_doc(project_root, doc_id)


@mcp.tool()
@_timed_tool
def axiom_graph_update_doc_meta(
    project_root: str,
    doc_id: str,
    title: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """Update a document's title or tags without rewriting the entire document.

    Patches the top-level fields in the JSON file and re-indexes. Sections
    and their content are untouched.

    Args:
        project_root: Absolute path to the indexed project.
        doc_id: Full doc node ID, e.g. ``myproject::docs.architecture``.
        title: New title for the document. Must be non-empty if provided.
        tags: New list of tags for the document. Pass an empty list to clear.
    """
    return _impl_update_doc_meta(project_root, doc_id, title, tags)


# -- Lifecycle tools --


@mcp.tool()
@_timed_tool
def axiom_graph_build(project_root: str, verbose: bool = False) -> str:
    """Run axiom-graph build (discovery-only) for a project.

    Only nodes that have never been indexed are inserted, preserving
    ``CONTENT_UPDATED`` / ``NOT_FOUND`` signals.  Edges are updated
    in all cases.

    Args:
        project_root: Absolute path to the project to index.
        verbose: When ``True``, include all warning details.  Default
            ``False`` shows only the warning count.
    """
    return _impl_build(project_root, verbose, _embedder_thread=_embedder_thread)


@mcp.tool()
@_timed_tool
def axiom_graph_checkout(project_root: str, worktree_path: str) -> str:
    """Copy the axiom-graph DB into a worktree via VACUUM INTO.

    Produces an atomic, consistent snapshot of the source index --
    safe regardless of WAL state or concurrent writes.

    Args:
        project_root: Absolute path to the source project (must have
            .axiom_graph/graph.db).
        worktree_path: Absolute path to the target directory.
    """
    return _impl_checkout(project_root, worktree_path)


@mcp.tool()
@_timed_tool
def axiom_graph_check(project_root: str, include_frozen: bool = False) -> str:
    """Report per-node staleness / confidence status (one-line summary).

    Returns a single summary line covering both dimensions of node
    health, with optional ``(all nodes VERIFIED)`` / DOC_SECTION_LONG
    counts when applicable.

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
        2026-05 drift_query cycle.  Calling with those keyword arguments
        raises ``TypeError`` -- migrate to ``axiom_graph_drift_query``.
    """
    return _impl_check(project_root, include_frozen=include_frozen)


@mcp.tool()
@_timed_tool
@_copy_doc(_impl_drift_query)
def axiom_graph_drift_query(
    project_root: str,
    filter: str | None = None,
    location_glob: str | None = None,
    group_by: str | None = None,
    format: str | None = None,
    page: int = 0,
    limit: int = 100,
    include_frozen: bool = False,
) -> str:
    return _impl_drift_query(
        project_root,
        filter=filter,
        location_glob=location_glob,
        group_by=group_by,
        format=format,
        page=page,
        limit=limit,
        include_frozen=include_frozen,
    )


@mcp.tool()
@_timed_tool
def axiom_graph_history(
    project_root: str,
    node_id: str = "",
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
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
        limit: Deprecated alias for max_results.
    """
    return _impl_history(project_root, node_id, max_results, offset, node_ids, limit)


@mcp.tool()
@_timed_tool
def axiom_graph_list_reference_points(project_root: str) -> str:
    """List available reference points for ``axiom_graph_report(since_sha=...)``.

    Call this **before** ``axiom_graph_report`` to discover valid SHA values.

    Args:
        project_root: Absolute path to the indexed project.
    """
    return _impl_list_reference_points(project_root)


@mcp.tool()
@_timed_tool
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

    Args:
        project_root: Absolute path to the indexed project.
        since_sha: Git SHA prefix.
        since_timestamp: ISO-8601 datetime cutoff.
        verbose: When False (default), return only the summary line.
        change_type_pattern: Glob pattern to filter change types.
        node_pattern: Glob pattern to filter node IDs.
        node_type: Filter to nodes of this type.
    """
    return _impl_report(
        project_root,
        since_sha,
        since_timestamp,
        verbose,
        change_type_pattern,
        node_pattern,
        node_type,
    )


@mcp.tool()
@_timed_tool
def axiom_graph_diff(
    project_root: str,
    node_id: str = "",
    baseline_sha: str | None = None,
    node_ids: list[str] | None = None,
    summary_only: bool = False,
) -> str:
    """Show what changed in a node since a baseline commit.

    When ``summary_only`` is ``False`` (default), returns JSON with keys:
    ``node_id``, ``baseline_sha``, ``baseline_date``, ``old_content``,
    ``new_content``, ``summary``.

    When ``summary_only`` is ``True``, omits ``old_content`` and
    ``new_content``, adds ``lines_added`` and ``lines_removed`` integers.
    Use for triage before deep-diving into individual nodes.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: The node to diff.
        baseline_sha: Optional git SHA to diff against.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
        summary_only: Return only metadata and line-count stats.
    """
    return _impl_diff(project_root, node_id, baseline_sha, node_ids, summary_only)


@mcp.tool()
@_timed_tool
def axiom_graph_mark_clean(
    project_root: str,
    reason: str,
    node_id: str = "",
    verified_by: str = "agent",
    node_ids: list[str] | None = None,
) -> str:
    """Mark nodes as agent-verified, clearing promotable own-status drift.

    Records an AGENT_VERIFIED history row per node; on the next check, any node
    whose content still matches is promoted from CONTENT_UPDATED, DESC_UPDATED,
    or RENAMED back to VERIFIED.  Does not clear LINKED_STALE by fiat.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Single node to mark clean (used when node_ids is omitted).
        reason: Brief explanation of why the documentation is still accurate.
        verified_by: Identifier for the verifier. Defaults to ``'agent'``.
        node_ids: Optional list of node IDs for batch operation.
    """
    return _impl_mark_clean(project_root, node_id, reason, verified_by, node_ids)


@mcp.tool()
@_timed_tool
def axiom_graph_purge_node(
    project_root: str,
    reason: str,
    node_id: str = "",
    node_ids: list[str] | None = None,
) -> str:
    """Purge one or more NOT_FOUND nodes from the index.

    Only nodes with ``own_status = 'NOT_FOUND'`` can be purged.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Full node ID to purge.
        reason: Human-readable reason for the purge.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored.
    """
    return _impl_purge_node(project_root, node_id, reason, node_ids)


@mcp.tool()
@_timed_tool
def axiom_graph_apply_rename(
    project_root: str,
    old_id: str,
    new_id: str,
) -> str:
    """Manually weld a rename the automatic matcher missed.

    Escape hatch for a real rename that fell below the similarity threshold.
    Restricted to the ``(NOT_FOUND old, newly-created new)`` safety contract.
    On success it migrates history/verification/edges to ``new_id`` and marks
    it ``RENAMED``.

    Args:
        project_root: Absolute path to the indexed project.
        old_id: The ``NOT_FOUND`` node being renamed from.
        new_id: The newly-created live node being renamed to.
    """
    return _impl_apply_rename(project_root, old_id, new_id)


@mcp.tool()
@_timed_tool
def axiom_graph_revert_rename(
    project_root: str,
    new_id: str,
) -> str:
    """Un-weld a previously applied rename, restoring the prior identity.

    Re-runs the recorded migration in reverse: history/verification/edges move
    back to the original ID, which is restored as the live identity while
    ``new_id`` is detached as a fresh node.

    Args:
        project_root: Absolute path to the indexed project.
        new_id: The current (renamed-to) identity to revert.
    """
    return _impl_revert_rename(project_root, new_id)


@mcp.tool()
@_timed_tool
def axiom_graph_render_site(
    project_root: str,
    build: bool = False,
    nav_path: str | None = None,
    output_dir: str | None = None,
    targets: list[str] | None = None,
) -> str:
    """Render consumer documentation site from DocJSON sources.

    With no ``nav_path``/``output_dir``, renders every configured render target
    (``[[axiom_graph.site.targets]]``) -- or the subset named in *targets* --
    in its declared flavor (plain GFM or Sphinx/MyST).  When no targets are
    configured an implicit ``guide`` (sphinx -> ``userdocs/guide``) target is
    synthesised.  Renders each doc to clean Markdown (no agent annotations,
    internal doc-id links stripped) with a provenance stamp.

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
    """
    return _impl_render_site(project_root, build, nav_path, output_dir, targets)


# -- Workflow tools --


@mcp.tool()
@_timed_tool
def axiom_graph_workflow_list(
    project_root: str,
    module: str | None = None,
    role: str | None = None,
    scope: str = "production",
    has_steps: bool = False,
    max_results: int = 30,
    offset: int = 0,
) -> str:
    """List workflow and task functions.

    Returns one line per function: name, decorator role, purpose summary,
    file location, and the corresponding axiom-graph node ID (when resolved).

    Args:
        project_root: Absolute path to the indexed project.
        module: File path substring filter.
        role: Filter by decorator type: ``"workflow"`` or ``"task"``.
        scope: ``"production"`` (default), ``"tests"``, or ``"all"``.
        has_steps: When True, only return functions with Step markers.
        max_results: Maximum rows returned (default 30).
        offset: Starting index for pagination (default 0).
    """
    return _impl_workflow_list(project_root, module, role, scope, has_steps, max_results, offset)


@mcp.tool()
@_timed_tool
def axiom_graph_workflow_detail(
    project_root: str,
    workflow_id: str,
    verbose: bool = False,
) -> str:
    """Show ordered steps for a single workflow or task function.

    ``workflow_id`` is the function name (e.g. ``"run_pipeline"``) or the
    axiom-graph node ID from ``axiom_graph_workflow_list``.

    Args:
        project_root: Absolute path to the indexed project.
        workflow_id: Function name or axiom-graph node ID.
        verbose: When ``True``, include purpose, inputs, outputs, and
            critical fields.
    """
    return _impl_workflow_detail(project_root, workflow_id, verbose)


# ---------------------------------------------------------------------------
# Backward-compatible re-exports for helpers used in tests
# ---------------------------------------------------------------------------

from axiom_graph.mcp._helpers import (  # noqa: E402, F811
    _timed_tool,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Start the MCP server (stdio transport).

    Configures logging to stderr (MCP uses stdio for transport, so all
    logging MUST go to stderr to avoid corrupting the protocol stream).

    Env vars:
        AXIOM_GRAPH_LOG_LEVEL: Override the default INFO level (DEBUG, INFO,
            WARNING, ERROR).
        AXIOM_GRAPH_LOG_FILE: If set, also log to this file via a
            RotatingFileHandler (5 MB per file, 2 backups = 15 MB max).
    """
    level_name = os.environ.get("AXIOM_GRAPH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=fmt,
    )
    log_file = os.environ.get("AXIOM_GRAPH_LOG_FILE")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(file_handler)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run()
