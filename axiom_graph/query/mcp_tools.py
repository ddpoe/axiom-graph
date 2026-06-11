"""Query MCP wire surface.

Thin wrappers re-exporting the query behavioural API for the MCP tool
registry in :mod:`axiom_graph.mcp.server`.  Each wrapper preserves the
public docstring and signature and forwards to
:mod:`axiom_graph.query.api`.  The ``_timed_tool`` decorator is applied
at registration time in ``mcp.server`` (matching cycle 1's docjson
template + ``workflows.mcp_tools`` precedent) so it composes cleanly
with the symmetric four-domain registration block.

Per ADR-019, this module's allowed imports are:
``axiom_graph.query.api``, ``axiom_graph.config``,
``axiom_graph.index.paths`` (for ``require_db``), and the standard
library.  Nothing else (no direct ``db.*`` / ``index.*`` (other than
``paths``) / ``sqlite3``).
"""

from __future__ import annotations

import logging
from pathlib import Path

import axiom_graph.query.api as _api
from axiom_graph.index.paths import require_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sql
# ---------------------------------------------------------------------------


def axiom_graph_sql(project_root: str, query: str, max_results: int = 50) -> str:
    """Run a read-only SQL query against the axiom-graph index for debugging.

    Returns results as a formatted table.  Only SELECT statements are allowed.
    Use this for diagnostic queries against nodes, node_history, edges, tags,
    node_verification, docs, and doc_sections tables.

    Args:
        project_root: Absolute path to the indexed project.
        query: A SELECT SQL statement to execute.
        max_results: Maximum rows to return (default 50, max 500).
    """
    db_path = require_db(project_root)
    return _api.run_sql(db_path, query, max_results)


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def axiom_graph_render(
    project_root: str,
    level: int,
    node_id: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    """Render nodes from the index at a given detail level.

    Passing ``node_id`` renders that single node; cap and offset are ignored.
    Omitting ``node_id`` renders all nodes capped at ``max_results`` (default
    60) with a ``[N of M nodes -- level N]`` count header.  Use ``offset`` to
    page through results.

    At levels 1 and 2, non-VERIFIED staleness badges are appended inline --
    e.g. ``[CONTENT_UPDATED]`` or ``[LINKED_STALE]``.  VERIFIED
    nodes receive no annotation (happy-path is noise-free).

    Args:
        project_root: Absolute path to the indexed project.
        level: 0 = ids only, 1 = id + summary (+ staleness badge), 2 = full
            detail (+ staleness badge on header line), 3 = step markers.
        node_id: If provided, render only this specific node (cap/offset
            ignored).
        max_results: Maximum nodes returned when ``node_id`` is omitted
            (default 60).
        offset: Starting index for pagination (default 0).
    """
    db_path = require_db(project_root)
    result = _api.fetch_render_data(
        db_path,
        level,
        node_id=node_id,
        max_results=max_results,
        offset=offset,
        with_badges=True,
    )
    if result.not_found is not None:
        return f"ERROR: Node '{result.not_found}' not found."
    if result.invalid_level is not None:
        return f"ERROR: level must be 0, 1, 2, or 3 (got {result.invalid_level})."
    if result.header:
        return f"{result.header}\n{result.body}"
    return result.body


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


# Friendly node_type aliases (MCP-only; CLI passes raw values)
_LIST_ALIASES = {"function": "atomic_process", "module": "composite_process"}


def axiom_graph_list(
    project_root: str,
    node_type: str | None = None,
    tag: str | None = None,
    parent_id: str | None = None,
    location: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    """List nodes, optionally filtered by node_type, tag, parent_id, or location.

    **To list all functions in a file**, pass ``location`` -- e.g.
    ``location="snakemake_gen.py"`` returns every node whose source
    location contains that substring.  This collapses the common two-step
    pattern (search for node ID -> list children) into a single call.
    Combine with ``node_type`` to restrict to functions only.

    **To list all functions inside a module by ID**, pass ``parent_id`` -- e.g.
    ``parent_id="pm_mvp::pm.method"`` returns every function defined in
    that module via one-hop ``composes`` edges. This is faster and more
    precise than ``axiom_graph_graph`` for simple child enumeration.

    Output always starts with a ``[N of M results]`` header line.
    Results are capped at ``max_results`` (default 60) to prevent accidental
    full-project dumps.  Raise ``max_results`` explicitly if you need more.

    Args:
        project_root: Absolute path to the indexed project.
        node_type: e.g. ``atomic_process`` (functions/methods) or
            ``composite_process`` (modules).  Aliases ``"function"`` and
            ``"module"`` are also accepted.
        tag: Only return nodes with this tag.
        parent_id: If provided, return only the direct children of this node
            (one-hop ``composes`` edges). Useful for listing all functions in
            a module without reading the file. Tags are ignored when parent_id
            is set.
        location: File path substring filter.  Any node whose
            ``level_3_location`` contains this string is included.
        max_results: Maximum rows returned (default 60).  The count header
            always shows total before the cap.
        offset: Starting index for pagination (default 0).
    """
    if node_type in _LIST_ALIASES:
        node_type = _LIST_ALIASES[node_type]

    db_path = require_db(project_root)
    nodes = _api.list_nodes(
        db_path,
        node_type=node_type,
        tag=tag,
        parent_id=parent_id,
        location=location,
    )
    total = len(nodes)
    capped = nodes[offset : offset + max_results]
    header = f"[{len(capped)} of {total} results]"
    if total > offset + max_results:
        header += f"  (cap={max_results}; pass offset={offset + max_results} for next page)"
    # Local import to avoid Rule 1 (presentation may not import db);
    # ``renderers`` is presentation-side formatting only.
    from axiom_graph.renderers import agent

    body = agent.render_level_1(capped)
    return f"{header}\n{body}"


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def axiom_graph_graph(
    project_root: str,
    node_id: str,
    direction: str = "out",
    depth: int = 1,
    max_results: int = 40,
    node_ids: list[str] | None = None,
    offset: int = 0,
) -> str:
    """Traverse and render the edge graph for a node.

    **To find all callers or construction sites of a symbol**, use
    ``direction="in"`` -- this returns every node that calls or depends on
    the given node.  The recommended pattern for a usage search is:
    ``axiom_graph_search(symbol_name)`` -> get the node ID from the results ->
    ``axiom_graph_graph(node_id, direction="in")`` to see callers, then follow
    with ``grep_search`` for literal call sites that may not be indexed.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: ID of the starting node.
        direction: ``"out"`` -- nodes this node depends on.
            ``"in"`` -- nodes that depend on / call this node.
            ``"both"`` -- full neighbourhood.
        depth: Number of hops to traverse.
        max_results: Maximum number of edges to return (default 40).  When
            the traversal exceeds this cap, output is truncated with a hint
            to narrow the query.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored and graph results are returned
            for all listed IDs with per-ID delimiters.
        offset: Number of edges to skip (default 0).
    """
    valid_directions = {"out", "in", "both"}
    if direction not in valid_directions:
        return f"ERROR: direction must be one of {sorted(valid_directions)}, got '{direction}'"

    if node_ids is not None:
        if not node_ids:
            return "ERROR: node_ids list is empty"
        parts: list[str] = []
        for nid in node_ids:
            try:
                result = axiom_graph_graph(
                    project_root,
                    node_id=nid,
                    direction=direction,
                    depth=depth,
                    max_results=max_results,
                    offset=offset,
                )
            except Exception as exc:
                result = f"ERROR ({nid}): {exc}"
            parts.append(result)
        return "\n\n---\n\n".join(parts)

    logger.debug("axiom_graph_graph: node_id=%s, direction=%s, depth=%d", node_id, direction, depth)

    db_path = require_db(project_root)
    result = _api.fetch_graph(
        db_path,
        node_id,
        direction=direction,
        depth=depth,
        max_results=max_results,
        offset=offset,
        with_locations=True,
    )
    if result.not_found:
        return f"ERROR: Node '{node_id}' not found."

    header = f"[{result.shown} of {result.total_edges} edges]"
    if result.truncated:
        header += "  (use depth=1 or a more specific node_id to narrow)"
    return f"{header}\n{result.rendered}"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


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
    _embedder_thread=None,
) -> str:
    """Full-text search over node level_1 and level_2 fields.

    Returns one line per match: ``{node_id}  {level_1 summary}``.
    A header line always reports how many results were returned and the total
    number of matches before the cap, so you can tell whether narrowing the
    query would help.

    **Finding all usages of a symbol**

    Prefer this tool over ``axiom_graph_list`` for answering "where is X used?".
    The efficient pattern is:

    1. ``axiom_graph_search(project_root, "SymbolName")`` -- locate the node ID.
    2. ``axiom_graph_graph(project_root, node_id, direction="in")`` -- find callers
       and dependents from the index.
    3. ``grep_search`` for literal call sites (construction, imports) that
       may not be captured as edges in the index.

    This is significantly cheaper in context than calling ``axiom_graph_list``
    (which dumps every node) and more targeted than reading full files.

    **How the query is interpreted**

    In ``keyword`` mode (default), the search runs through three stages,
    stopping as soon as any stage returns results:

    1. **FTS (ranked)** -- SQLite FTS5 full-text index, BM25-ranked.  A single
       word finds every node containing that word, ordered by relevance.
       A quoted phrase (``"scan module"``) requires both words adjacent.
       A prefix (``scan*``) matches any word starting with those characters.
       Multiple unquoted words (``scan import``) require *all* words to be
       present -- if nothing contains every term, this stage returns nothing
       and the next stage runs.

    2. **LIKE-AND fallback** -- substring scan, all tokens must appear
       somewhere in the text.  Same AND semantics as FTS but no ranking.
       Fires when FTS returns nothing (e.g. query used unsupported syntax).

    3. **LIKE-OR fallback** -- substring scan, *any* token matches.  Broad
       and low-confidence; capped at 10 results regardless of ``max_results``.
       If this stage fires it usually means the query terms are too common or
       unrelated -- a more specific single term will return better-ranked
       results from stage 1.

    In ``semantic`` mode, the query is converted to an embedding vector and
    matched against stored node embeddings via cosine similarity. This finds
    conceptually related nodes even when the exact words differ. Requires
    embeddings to have been generated during ``axiom_graph_build``. Falls back to
    keyword mode if embeddings are unavailable.

    Args:
        project_root: Absolute path to the indexed project.
        query: Search string.  A single specific term or a quoted phrase
            returns ranked results from the FTS index.  A prefix ending in
            ``*`` (e.g. ``resolv*``) matches all words sharing that root.
            Multiple space-separated terms require all of them to appear in
            the same node.
        level: ``1`` -- search level_1 (one-line summaries) only; faster and
            less noisy when you already know the function/module name.
            ``2`` -- search level_2 (full docstrings) only; useful when
            looking for behaviour described in prose rather than in a name.
            ``None`` (default) -- search both fields.
        max_results: Maximum number of nodes to return (default 20).  The
            header line shows the total match count so you can raise this
            when needed.  LIKE-OR results are always capped at 10 regardless
            of this value.
        node_type: Restrict results to a single node type:
            ``"atomic_process"`` for functions/methods,
            ``"composite_process"`` for modules, or omit to search all types.
        mode: Search mode: ``"keyword"`` (default) uses FTS5 full-text
            matching. ``"semantic"`` uses embedding-based vector similarity
            search (deprecated as of 2.1.0; slated for removal in 3.0 --
            use keyword search).
        scope: Filter results by source: ``"code"`` returns only code nodes,
            ``"docs"`` returns only doc section nodes, ``"all"`` (default)
            returns both.
        tag: Only return nodes with this tag.
        offset: Number of leading matches to skip before returning results
            (default 0).  Pair with ``max_results`` to page through a large
            result set.
    """
    logger.debug(
        "axiom_graph_search: query=%r, mode=%s, max_results=%d, offset=%d",
        query,
        mode,
        max_results,
        offset,
    )
    logger.debug("axiom_graph_search: resolving DB path for %s", project_root)
    db_path = require_db(project_root)
    logger.debug("axiom_graph_search: DB path resolved to %s", db_path)
    return _api.search_nodes(
        db_path,
        query,
        level=level,
        max_results=max_results,
        node_type=node_type,
        mode=mode,
        scope=scope,
        tag=tag,
        offset=offset,
        embedder_thread=_embedder_thread,
    )


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


def axiom_graph_source(
    project_root: str,
    node_id: str,
    node_ids: list[str] | None = None,
    max_chars: int = 40_000,
) -> str:
    """Return the raw source body of a node, looked up by ID.

    Uses the ``level_3_location`` stored in the index to locate the file and
    line range, then returns those source lines directly.  This removes the
    need to manually read files after a ``axiom_graph_search`` -- the full flow
    becomes ``axiom_graph_search`` -> ``axiom_graph_source``.

    For module-level nodes (no line range stored) the entire file is returned.

    Args:
        project_root: Absolute path to the indexed project.
        node_id: Full node ID, e.g. ``myproject::pm.cli::some_function``.
        node_ids: Optional list of node IDs for batch operation.  When
            provided, ``node_id`` is ignored and results are returned for
            all listed IDs with per-ID delimiters.  Invalid IDs produce
            per-ID error messages without aborting the entire call.
        max_chars: Maximum total characters in the response (default
            40 000).  High enough that single-node calls are never
            truncated, but prevents batch calls from blowing up context.
            When the limit is hit mid-batch, remaining node IDs are listed
            so you can fetch them in a follow-up call.  Pass ``None`` to
            disable the limit entirely.
    """
    if node_ids is not None:
        if not node_ids:
            return "ERROR: node_ids list is empty"
        parts: list[str] = []
        char_count = 0
        separator = "\n\n---\n\n"
        omitted: list[str] = []
        for i, nid in enumerate(node_ids):
            try:
                result = axiom_graph_source(project_root, node_id=nid, max_chars=None)
            except Exception as exc:
                result = f"ERROR ({nid}): {exc}"
            chunk_cost = len(result) + (len(separator) if parts else 0)
            if max_chars is not None and char_count + chunk_cost > max_chars and parts:
                omitted = list(node_ids[i:])
                break
            parts.append(result)
            char_count += chunk_cost
        response = separator.join(parts)
        if omitted:
            response += (
                f"\n\n[TRUNCATED — {len(omitted)} of {len(node_ids)} nodes omitted "
                f"to stay within {max_chars} char limit]\n"
                f"Omitted IDs: {', '.join(omitted)}\n"
                f"Re-call with these IDs, or pass max_chars=None for the full response."
            )
        return response

    db_path = require_db(project_root)
    root = Path(project_root).resolve()
    result = _api.fetch_source(db_path, root, node_id)
    if result.not_found:
        return f"ERROR: Node '{node_id}' not found."
    if result.no_location:
        return f"ERROR: Node '{node_id}' has no location recorded."
    if result.file_missing:
        # File path was relative to root; reconstruct the absolute for the message.
        loc = result.location
        if "#L" in loc:
            file_part = loc.split("#L", 1)[0]
        else:
            file_part = loc
        return f"ERROR: Source file not found: {root / file_part}"
    return result.text


# ---------------------------------------------------------------------------
# list_tags
# ---------------------------------------------------------------------------


def axiom_graph_list_tags(project_root: str) -> str:
    """List all distinct tags in the index with node counts.

    Returns a table of tags and how many nodes bear each tag. Useful for
    discovering available tags before filtering with ``axiom_graph_list`` or
    ``axiom_graph_search``.

    Args:
        project_root: Absolute path to the indexed project.
    """
    db_path = require_db(project_root)
    tags = _api.list_tags(db_path)
    if not tags:
        return "No tags found in index."
    lines = [f"[{len(tags)} tags]"]
    for tag_name, count in tags:
        lines.append(f"  {tag_name}: {count} node(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# list_undocumented
# ---------------------------------------------------------------------------


_UNDOC_ALIASES = {"function": "atomic_process", "module": "composite_process"}


def axiom_graph_list_undocumented(
    project_root: str,
    node_type: str | None = None,
    max_results: int = 60,
    offset: int = 0,
) -> str:
    """List all nodes that have no inbound 'documents' edge.

    These nodes exist in the index but are not referenced by any doc section.
    Output always starts with a ``[N of M undocumented nodes]`` count header.
    Results are capped at ``max_results`` (default 60); use ``offset`` to page.

    Args:
        project_root: Absolute path to the indexed project.
        node_type: If provided, restrict to nodes of this type.  Accepts
            ``"function"`` (``atomic_process``) and ``"module"``
            (``composite_process``) aliases.
        max_results: Maximum rows returned (default 60).
        offset: Starting index for pagination (default 0).
    """
    if node_type in _UNDOC_ALIASES:
        node_type = _UNDOC_ALIASES[node_type]

    db_path = require_db(project_root)
    nodes = _api.list_undocumented(db_path, node_type=node_type)
    if not nodes:
        suffix = f" of type '{node_type}'" if node_type else ""
        return f"All nodes{suffix} are documented."
    total = len(nodes)
    capped = nodes[offset : offset + max_results]
    header = f"[{len(capped)} of {total} undocumented nodes]"
    if total > offset + max_results:
        header += f"  (cap={max_results}; pass offset={offset + max_results} for next page)"
    from axiom_graph.renderers import agent

    return f"{header}\n{agent.render_level_1(capped)}"


# ---------------------------------------------------------------------------
# drift_query (D-3: relocated from lifecycle/mcp_tools.py)
# ---------------------------------------------------------------------------


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
    """Filtered/grouped/paginated projection over the persisted staleness inventory.

    This is the read-only companion to ``axiom_graph_check``.  ``check``
    answers "how much drift is there?" in one summary line; this answers
    "what are the specific drifted nodes (or distribution / IDs)?" with
    pagination so large drift volumes don't overflow the tool-result
    cap.

    Args:
        project_root: Absolute path to the indexed project.
        filter: Status filter.  ``None`` (all own + link problem
            statuses), ``"staleness"`` (own + LINKED_STALE only),
            ``"links"`` (LINKED_STALE + BROKEN_LINK), ``"all"`` (every
            own + link problem status), or any individual status name
            (``CONTENT_UPDATED``, ``DESC_UPDATED``, ``RENAMED``,
            ``NOT_FOUND``, ``LINKED_STALE``, ``BROKEN_LINK``).  The ``DOC_SECTION_LONG``
            advisory is not addressable here -- it lives on a different
            table and is summarised by ``axiom_graph_check``.
        location_glob: fnmatch-style path glob (``**`` for recursive)
            applied to ``nodes.level_3_location`` (falling back to
            ``location``).  E.g. ``"axiom_graph/viz/**"``.  Filters
            BEFORE grouping; grouped counts reflect the post-filter
            slice.
        group_by: Optional grouping axis.  One of:

            - ``None`` -- flat row list (paginated).
            - ``"status"`` -- group by ``own/link`` status pair.
            - ``"location_prefix"`` -- group by 2-component path prefix
              (``axiom_graph/viz``, ``axiom_graph/index``, ...).
            - ``"feature"`` -- group by inbound ``documents``-edge
              feature ancestor (``docs.features.{X}`` in the doc tree).
              Nodes with no inbound ``documents`` edge bucket as
              ``(undocumented)``; never silently dropped.

        format: Projection.  ``None`` (the default) resolves to
            ``"full"`` when ungrouped and ``"counts"`` when ``group_by``
            is set -- so an aggregate call returns a compact distribution
            rather than dumping every full row.  Explicit values:

            - ``"full"`` -- ``id, own_status, link_status, location, via``.
            - ``"ids"`` -- newline-delimited IDs (or ``{group, ids}`` per
              group when ``group_by`` is set).
            - ``"counts"`` -- ``{group, count}`` per group.  Only valid
              with ``group_by``.

        page: Zero-indexed page number (``page * limit`` -> OFFSET).
            Applies to ``full`` and ``ids`` on both the flat and grouped
            paths; grouped pagination is over the flat (pre-group) row set
            ordered by id, then re-grouped within the page, so groups may
            span page boundaries.  ``counts`` is a bounded distribution and
            is never paginated.
        limit: Page size (default 100).
        include_frozen: When ``False`` (the default), rows under docs
            tagged in ``config.staleness.frozen_tags`` are excluded
            from output, except BROKEN_LINK rows which are retained
            with a ``[frozen-source]`` postfix on ``format='full'``.
            When ``True`` all rows are returned, with a ``[frozen]``
            postfix on frozen rows in ``format='full'``.  Markers
            never appear on ``format='ids'`` or ``format='counts'``.

    Returns:
        Newline-delimited textual representation of the projection.
        The ``full`` and ``ids`` projections are prefixed with a
        ``[N of M drifted nodes]`` count header (with a
        ``(pass page=<next> for next page)`` hint when more rows remain),
        matching the sibling paginated tools.  Shapes:

        - flat full: count header, ``#`` column header, then
          ``id  own/link  location  via=...`` per line.
        - flat ids: count header, then bare node IDs.
        - grouped counts: ``group  count`` per line (no header; unpaginated).
        - grouped ids: count header, then ``[group]`` + indented IDs.
        - grouped full: count header, ``#`` column header, then
          ``[group]`` + indented full rows.

        Returns ``"(no matches)"`` when the post-filter slice is empty,
        and ``"(page out of range)"`` when ``page`` paginates past the
        end of an otherwise non-empty slice (both the flat and grouped
        ``full``/``ids`` paths).

    Raises:
        ValueError: invalid ``filter``, ``group_by``, ``format``, or
            ``format='counts'`` without ``group_by``.
    """
    db_path = require_db(project_root)
    root = Path(project_root).resolve()
    return _api.compute_drift_query(
        db_path,
        root,
        filter=filter,
        location_glob=location_glob,
        group_by=group_by,
        format=format,
        page=page,
        limit=limit,
        include_frozen=include_frozen,
    )
