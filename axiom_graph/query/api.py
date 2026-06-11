"""Public Python API for the query bounded context.

Per ADR-019 (cycle 3), the query domain owns every read-only inventory
operation against the axiom-graph index: full-text and semantic search,
node rendering at multiple detail levels, node listing with filters,
edge traversal, raw source fetch, SQL passthrough, drift inventory
projection, tag listing, and undocumented-node listing.

This module is the single canonical home for those operations; the MCP
wire surface (:mod:`axiom_graph.query.mcp_tools`) is a thin layer that
forwards calls.  The CLI (:mod:`axiom_graph.cli.inspection`,
:mod:`axiom_graph.cli.rendering`) also calls this module directly so a
single orchestration function is the source of truth for each Cat 4
operation.

Public surface:
    ``search_nodes``         -- keyword / semantic search over the index
    ``fetch_render_data``    -- nodes + optional staleness for renderers
    ``list_nodes``           -- typed/tagged/filtered node listing
    ``fetch_graph``          -- edge traversal from a node
    ``fetch_source``         -- raw source body of a node by ID
    ``run_sql``              -- read-only SQL passthrough
    ``list_tags``            -- distinct tag listing with node counts
    ``list_undocumented``    -- nodes with no inbound documents edge
    ``compute_drift_query``  -- filtered/grouped/paginated drift inventory

Layering invariants (per ADR-019; enforced by ``tools/check_layering.py``):
    Allowed imports: ``axiom_graph.config``, ``axiom_graph.index.*``,
    ``axiom_graph.renderers``, ``axiom_graph.db.*`` (for
    drift_query staleness queries), and stdlib.  Never
    ``axiom_graph.mcp.*``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import db
from axiom_graph.index.builder import rescan_file_if_needed
from axiom_graph.index.status import BROKEN_LINK, VERIFIED
from axiom_graph.renderers import agent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed result dataclasses (mirrors cycle-2 D-1: typed dataclasses)
# ---------------------------------------------------------------------------


@dataclass
class RenderResult:
    """Result of :func:`fetch_render_data`.

    Carries the rendered text body plus an optional count header so the
    MCP wire wrapper can prepend a ``[N of M nodes -- level N]`` line
    while the CLI omits it.

    Attributes:
        body: Rendered string body (badges already applied if requested).
        header: Optional ``[N of M ...]`` count header (None for single-node
            renders).
        not_found: ID of the node that was not found, when applicable.
        invalid_level: Set to the level value when an invalid level was
            requested (so wire layer formats the error message itself).
    """

    body: str
    header: str | None = None
    not_found: str | None = None
    invalid_level: int | None = None


@dataclass
class GraphResult:
    """Result of :func:`fetch_graph`.

    Attributes:
        rendered: Rendered edge body (without header).
        shown: Number of edges in the rendered slice.
        total_edges: Total edges before offset/cap.
        truncated: True when offset + cap < total_edges.
        not_found: True when the starting node could not be located.
    """

    rendered: str
    shown: int
    total_edges: int
    truncated: bool
    not_found: bool = False


@dataclass
class NodeSource:
    """Result of :func:`fetch_source`.

    Attributes:
        text: The rendered source body (with file/line header banner).
        not_found: True when the node ID does not exist.
        no_location: True when the node has no recorded location.
        file_missing: True when the source file is missing on disk.
        location: The original ``level_3_location`` (or empty string).
    """

    text: str
    not_found: bool = False
    no_location: bool = False
    file_missing: bool = False
    location: str = ""


# ---------------------------------------------------------------------------
# Semantic search handler (formerly mcp/_helpers._semantic_search_handler)
# ---------------------------------------------------------------------------


def _semantic_search_handler(
    db_path: Path,
    query: str,
    max_results: int = 20,
    node_type: str | None = None,
    scope: str = "all",
    embedder_thread=None,
) -> str:
    """Handle semantic search mode for :func:`search_nodes`.

    Embeds the query, performs vector similarity search, and formats results.
    Falls back to keyword search if embeddings are unavailable.

    Args:
        db_path: Path to the axiom-graph DB file.
        query: The natural-language search query.
        max_results: Maximum results to return.
        node_type: Optional node type filter.
        scope: Scope filter ('code', 'docs', 'all').
        embedder_thread: The embedder warm-up thread to join.

    Returns:
        Formatted search results string.
    """
    try:
        if embedder_thread is not None:
            logger.debug("semantic: waiting for embedder warm-up thread")
            t0 = time.monotonic()
            embedder_thread.join(timeout=30)
            join_elapsed = time.monotonic() - t0
            if join_elapsed > 0.1:
                logger.info("semantic: embedder warm-up wait %.2fs", join_elapsed)

        from axiom_graph.index.embeddings import get_embedder

        logger.debug("semantic: loading embedder model")
        embedder = get_embedder()
        logger.debug("semantic: embedder loaded, embedding query")
        t1 = time.monotonic()
        query_vec = embedder([query])[0]
        logger.debug("semantic: query embedded in %.3fs", time.monotonic() - t1)

        logger.debug("semantic: querying vector index")
        t2 = time.monotonic()
        nodes, total = db.semantic_search(
            db_path,
            query_vec,
            max_results=max_results,
            node_type=node_type,
            scope=scope if scope != "all" else None,
        )
        logger.debug("semantic: vec search in %.3fs (%d results)", time.monotonic() - t2, total)

        if not nodes:
            # Fall back to keyword search if semantic returns nothing
            nodes, search_mode, total = db.fts_search(
                db_path,
                query,
                max_results=max_results,
                node_type=node_type,
                scope=scope if scope != "all" else None,
            )
            result = agent.render_level_1(nodes)
            shown = len(nodes)
            header = f"[{shown} of {total} results -- semantic->keyword fallback]"
            return f"{header}\n{result}" if result != "(no nodes)" else header

        result = agent.render_level_1(nodes)
        shown = len(nodes)
        header = f"[{shown} of {total} results -- semantic]"
        return f"{header}\n{result}" if result != "(no nodes)" else header
    except Exception as exc:
        logger.warning("Semantic search failed, falling back to keyword: %s", exc)
        nodes, search_mode, total = db.fts_search(
            db_path,
            query,
            max_results=max_results,
            node_type=node_type,
            scope=scope if scope != "all" else None,
        )
        result = agent.render_level_1(nodes)
        shown = len(nodes)
        mode_labels = {
            "fts": "fts ranked",
            "like_and": "LIKE-AND fallback",
            "like_or": "LIKE-OR fallback (broad, low-confidence)",
        }
        label = mode_labels.get(search_mode, search_mode)
        header = f"[{shown} of {total} results -- {label} (semantic unavailable)]"
        return f"{header}\n{result}" if result != "(no nodes)" else header


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search_nodes(
    db_path: Path,
    query: str,
    *,
    level: int | None = None,
    max_results: int = 20,
    node_type: str | None = None,
    mode: str = "keyword",
    scope: str = "all",
    tag: str | None = None,
    offset: int = 0,
    embedder_thread=None,
) -> str:
    """Run a keyword or semantic search over the axiom-graph index.

    Returns the formatted text result (the wire wrapper passes it through
    unchanged).  In ``"keyword"`` mode a three-stage fallback chain runs
    (FTS5 -> LIKE-AND -> LIKE-OR); in ``"semantic"`` mode the query is
    embedded and matched via vector similarity, with graceful fallback
    to keyword when embeddings are unavailable.  Header labels for each
    stage are part of the wire contract.

    Args:
        db_path: Path to the axiom-graph DB file.
        query: Search string.
        level: ``1`` searches level_1 only; ``2`` searches level_2 only;
            ``None`` (default) searches both.
        max_results: Maximum results to return.
        node_type: Optional node-type filter (raw value, no aliases).
        mode: ``"keyword"`` (default) or ``"semantic"``.
        scope: ``"code"`` / ``"docs"`` / ``"all"``.
        tag: Optional tag filter.
        offset: Number of results to skip (default 0).
        embedder_thread: The embedder warm-up thread to join (semantic only).

    Returns:
        Newline-delimited formatted search results.
    """
    logger.debug(
        "search_nodes: query=%r, mode=%s, max_results=%d, offset=%d",
        query,
        mode,
        max_results,
        offset,
    )

    if mode == "semantic":
        logger.debug("search_nodes: entering semantic search mode")
        return _semantic_search_handler(
            db_path,
            query,
            max_results=max_results,
            node_type=node_type,
            scope=scope,
            embedder_thread=embedder_thread,
        )

    # Default: keyword (FTS) mode
    logger.debug("search_nodes: entering keyword search mode")
    fetch_limit = max_results + offset
    nodes, search_mode, total = db.fts_search(
        db_path,
        query,
        level=level,
        max_results=fetch_limit,
        node_type=node_type,
        scope=scope if scope != "all" else None,
        tag=tag,
    )
    nodes = nodes[offset:]
    if len(nodes) > max_results:
        nodes = nodes[:max_results]
    result = agent.render_level_1(nodes)
    mode_labels = {
        "fts": "fts ranked",
        "like_and": "LIKE-AND fallback",
        "like_or": "LIKE-OR fallback (broad, low-confidence)",
    }
    label = mode_labels.get(search_mode, search_mode)
    shown = len(nodes)
    header = f"[{shown} of {total} results -- {label}]"
    return f"{header}\n{result}" if result != "(no nodes)" else header


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def fetch_render_data(
    db_path: Path,
    level: int,
    *,
    node_id: str | None = None,
    node_type: str | None = None,
    max_results: int = 60,
    offset: int = 0,
    with_badges: bool = False,
) -> RenderResult:
    """Render nodes from the index at a given detail level.

    Shared orchestration for the CLI ``axiom-graph render`` command and
    the ``axiom_graph_render`` MCP tool.  ``with_badges=True`` overlays
    staleness badges (used by MCP); the CLI passes ``False`` (preserves
    cycle-2 cmd_render byte-identity).  ``node_type`` filter is supplied
    by the CLI's ``--type`` flag (Cat-4b surface-divergent knob, mirrors
    ``lifecycle.api.compute_check_summary``); MCP passes ``None``.

    Args:
        db_path: Path to the axiom-graph DB.
        level: 0 (ids only), 1 (id + summary), 2 (full detail), 3 (steps).
        node_id: If provided, render only this node (cap/offset ignored).
        node_type: Optional node-type filter (e.g. ``"atomic_process"``).
            When set, ``db.query_nodes(node_type=...)`` replaces the
            unfiltered ``db.all_nodes`` fetch.  CLI-only knob; MCP
            ``axiom_graph_render`` does not expose it.
        max_results: Maximum nodes returned when ``node_id`` is omitted.
        offset: Starting index for pagination.
        with_badges: When True, overlay staleness badges from
            ``db.get_all_staleness``.

    Returns:
        :class:`RenderResult` carrying body text + optional header.
    """
    if node_id:
        node = db.get_node(db_path, node_id)
        if node is None:
            return RenderResult(body="", not_found=node_id)
        nodes = [node]
        header: str | None = None
    else:
        if node_type is not None:
            all_nodes_list = db.query_nodes(db_path, node_type=node_type)
        else:
            all_nodes_list = db.all_nodes(db_path)
        total = len(all_nodes_list)
        nodes = all_nodes_list[offset : offset + max_results]
        header = f"[{len(nodes)} of {total} nodes -- level {level}]"
        if total > offset + max_results:
            header += f"  (cap={max_results}; pass offset={offset + max_results} for next page)"

    if with_badges:
        staleness = db.get_all_staleness(db_path)

        def _badge(nid: str) -> str:
            pair = staleness.get(nid, (VERIFIED, VERIFIED))
            own, link = pair if isinstance(pair, tuple) else (pair, "VERIFIED")
            badges = []
            if own != VERIFIED:
                badges.append(own)
            if link != VERIFIED:
                badges.append(link)
            return f"  [{'+'.join(badges)}]" if badges else ""
    else:

        def _badge(nid: str) -> str:  # noqa: ARG001
            return ""

    if level == 0:
        body = agent.render_level_0(nodes)
    elif level == 1:
        if not nodes:
            body = "(no nodes)"
        else:
            raw_lines = agent.render_level_1(nodes).splitlines()
            badged_lines = []
            for n, line in zip(nodes, raw_lines):
                line += _badge(n.id)
                badged_lines.append(line)
            body = "\n".join(badged_lines)
    elif level == 2:
        if not nodes:
            body = "(no nodes)"
        else:
            parts = []
            for n in nodes:
                block_lines = agent.render_level_2([n]).splitlines()
                b = _badge(n.id)
                if b and block_lines:
                    block_lines[0] = block_lines[0] + b
                parts.append("\n".join(block_lines))
            body = "\n\n".join(parts)
    elif level == 3:
        body = agent.render_steps(nodes)
    else:
        return RenderResult(body="", invalid_level=level)

    return RenderResult(body=body, header=header)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def list_nodes(
    db_path: Path,
    *,
    node_type: str | None = None,
    tag: str | None = None,
    parent_id: str | None = None,
    location: str | None = None,
) -> list:
    """List nodes from the index with the standard filter set.

    Shared orchestration for ``axiom-graph list`` and ``axiom_graph_list``.
    Returns the *filtered* node list -- presentation layers paginate and
    format.  No node-type alias mapping happens here (CLI passes raw
    values; MCP wire wrapper applies aliases before calling).

    Args:
        db_path: Path to the axiom-graph DB.
        node_type: Optional node-type filter (raw value).
        tag: Optional tag filter.
        parent_id: When set, returns one-hop ``composes`` children of this
            node; ``tag`` is ignored.
        location: Substring filter on ``level_3_location``.

    Returns:
        List of :class:`AxiomNode` matching the filters.
    """
    if parent_id is not None:
        nodes = db.query_children(db_path, parent_id)
        if node_type:
            nodes = [n for n in nodes if n.node_type == node_type]
    else:
        nodes = db.query_nodes(db_path, node_type=node_type, tag=tag)

    if location:
        nodes = [n for n in nodes if n.level_3_location and location in n.level_3_location]

    return nodes


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------


def fetch_graph(
    db_path: Path,
    node_id: str,
    *,
    direction: str = "out",
    depth: int = 1,
    max_results: int = 40,
    offset: int = 0,
    with_locations: bool = False,
) -> GraphResult:
    """Traverse and render the edge graph for a node.

    Shared orchestration for ``axiom-graph graph`` and ``axiom_graph_graph``.
    Returns a :class:`GraphResult`; the CLI raises ``ClickException`` on
    not_found, the MCP wire wrapper formats both not_found and the
    optional truncation hint into a string.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: ID of the starting node.
        direction: ``"out"`` / ``"in"`` / ``"both"``.
        depth: Number of hops to traverse.
        max_results: Maximum number of edges in the slice.
        offset: Number of edges to skip.
        with_locations: When True, look up each node in the traversal and
            pass the lookup table to the renderer so function-level edges
            display ``@ path#L10-L45`` suffixes.  MCP wire passes True;
            the CLI passes False (preserves byte-identity with cycle-2
            ``cmd_graph`` output, which never showed locations).

    Returns:
        :class:`GraphResult`.
    """
    node = db.get_node(db_path, node_id)
    if node is None:
        return GraphResult(rendered="", shown=0, total_edges=0, truncated=False, not_found=True)
    edges = db.query_edges(db_path, node_id, direction=direction, depth=depth)

    total_edges = len(edges)
    edges = edges[offset:]
    truncated = len(edges) > max_results
    if truncated:
        edges = edges[:max_results]
    shown = len(edges)

    if with_locations:
        # Build location lookup for all nodes appearing in the traversal
        all_ids: set[str] = {node_id}
        for e in edges:
            all_ids.add(e.from_id)
            all_ids.add(e.to_id)
        node_lookup = {}
        for nid in all_ids:
            n = db.get_node(db_path, nid)
            if n is not None:
                node_lookup[nid] = n
        rendered = agent.render_graph(node, edges, direction=direction, node_lookup=node_lookup)
    else:
        rendered = agent.render_graph(node, edges, direction=direction)

    return GraphResult(
        rendered=rendered,
        shown=shown,
        total_edges=total_edges,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


def fetch_source(
    db_path: Path,
    root: Path,
    node_id: str,
) -> NodeSource:
    """Return the raw source body of a node, looked up by ID.

    Uses ``level_3_location`` to locate the file and line range, then
    reads the slice from disk.  For module-level nodes (no line range),
    the entire file is returned, with a child-table truncation for very
    large composite_process nodes.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root (used to resolve relative paths in
            ``level_3_location``).
        node_id: Full node ID.

    Returns:
        :class:`NodeSource` with the rendered body text plus diagnostic
        flags for not-found / no-location / file-missing.
    """
    node = db.get_node(db_path, node_id)
    if node is None:
        return NodeSource(text="", not_found=True)

    if rescan_file_if_needed(db_path, root, node):
        node = db.get_node(db_path, node_id) or node

    loc = node.level_3_location
    if not loc:
        return NodeSource(text="", no_location=True, location="")

    # Parse "path/to/file.py#L10-L45" or "path/to/file.py"
    if "#L" in loc:
        file_part, line_part = loc.split("#L", 1)
        line_part = line_part.replace("L", "")
        if "-" in line_part:
            start_str, end_str = line_part.split("-", 1)
            start, end = int(start_str), int(end_str)
        else:
            start = end = int(line_part)
    else:
        file_part = loc
        start = end = None

    src_file = root / file_part
    if not src_file.exists():
        return NodeSource(text="", file_missing=True, location=loc)

    all_lines = src_file.read_text(encoding="utf-8").splitlines()

    # Module-level truncation: large composite_process nodes get a TOC
    if start is None and node.node_type == "composite_process" and len(all_lines) > 200:
        preview = "\n".join(all_lines[:50])
        children = db.query_children(db_path, node_id)
        child_lines = []
        for child in children:
            cloc = child.level_3_location or ""
            entry = f"- {child.id}"
            if cloc:
                entry += f"  @ {cloc}"
            child_lines.append(entry)
        parts = [
            f"# {node_id}  @ {loc}",
            "",
            f"Module has {len(all_lines)} lines and {len(children)} functions. Showing first 50 lines.",
            "Use axiom_graph_source on a specific function for the full body.",
            "",
            preview,
        ]
        if child_lines:
            parts.append("")
            parts.append("Children (use axiom_graph_source with these IDs):")
            parts.extend(child_lines)
        return NodeSource(text="\n".join(parts), location=loc)

    body = "\n".join(all_lines[start - 1 : end] if start is not None else all_lines)
    return NodeSource(text=f"# {node_id}  @ {loc}\n\n{body}", location=loc)


# ---------------------------------------------------------------------------
# sql
# ---------------------------------------------------------------------------


def run_sql(db_path: Path, query: str, max_results: int = 50) -> str:
    """Run a read-only SQL query against the axiom-graph index.

    Only ``SELECT`` statements are accepted.  Results are formatted as
    an aligned table with a ``[N of M+ rows]`` header.  String values
    longer than 80 chars are ellipsised; results are capped at
    ``max_results + 1`` rows internally so the count header can flag
    truncation.

    Args:
        db_path: Path to the axiom-graph DB.
        query: A SELECT SQL statement to execute.
        max_results: Maximum rows to return (default 50, max 500).

    Returns:
        Formatted table string, or an ``ERROR: ...`` sentinel on
        non-SELECT input.
    """
    logger.debug("run_sql: query=%r, max_results=%d", query[:80], max_results)

    stripped = query.strip().rstrip(";")
    if not stripped.upper().startswith("SELECT"):
        return "ERROR: Only SELECT queries are allowed."
    max_results = min(max_results, 500)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(stripped).fetchmany(max_results + 1)
        if not rows:
            return "[0 of 0 rows]\n(no rows)"
        total_fetched = len(rows)
        truncated = total_fetched > max_results
        rows = rows[:max_results]
        shown = len(rows)
        cols = rows[0].keys()
        widths = {c: len(c) for c in cols}
        str_rows: list[dict[str, str]] = []
        for r in rows:
            sr = {}
            for c in cols:
                val = str(r[c]) if r[c] is not None else "NULL"
                if len(val) > 80:
                    val = val[:77] + "..."
                sr[c] = val
                widths[c] = max(widths[c], len(val))
            str_rows.append(sr)
        total_label = f"{total_fetched}+" if truncated else str(shown)
        count_header = f"[{shown} of {total_label} rows]"
        col_header = "  ".join(c.ljust(widths[c]) for c in cols)
        sep = "  ".join("-" * widths[c] for c in cols)
        lines = [count_header, col_header, sep]
        for sr in str_rows:
            lines.append("  ".join(sr[c].ljust(widths[c]) for c in cols))
        if truncated:
            lines.append(f"\n... truncated at {max_results} rows")
        return "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# list_tags
# ---------------------------------------------------------------------------


def list_tags(db_path: Path) -> list[tuple[str, int]]:
    """List all distinct tags in the index with node counts.

    Returns the raw ``(tag_name, count)`` pairs as ordered by
    :func:`db.list_tags`.  Presentation layers format.

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        List of ``(tag_name, count)`` tuples (possibly empty).
    """
    return db.list_tags(db_path)


# ---------------------------------------------------------------------------
# list_undocumented
# ---------------------------------------------------------------------------


def list_undocumented(
    db_path: Path,
    *,
    node_type: str | None = None,
) -> list:
    """List nodes that have no inbound ``documents`` edge.

    Returns the raw node list (no pagination).  Presentation layers
    paginate and format.

    Args:
        db_path: Path to the axiom-graph DB.
        node_type: Optional node-type filter (raw value, no aliases).

    Returns:
        List of :class:`AxiomNode`.
    """
    return db.get_undocumented_nodes(db_path, node_type=node_type)


# ---------------------------------------------------------------------------
# drift_query (D-3: verbatim move from lifecycle/api.py)
# ---------------------------------------------------------------------------


def compute_drift_query(
    db_path: Path,
    root: Path,
    *,
    filter: str | None = None,
    location_glob: str | None = None,
    group_by: str | None = None,
    format: str | None = None,
    page: int = 0,
    limit: int = 100,
    include_frozen: bool = False,
) -> str:
    """Filtered/grouped/paginated projection over the staleness inventory.

    Returns the formatted text result; callers (MCP wrapper today) pass
    it through unchanged.

    ``format`` defaults to ``None``, which resolves to ``"full"`` when
    ungrouped and ``"counts"`` when ``group_by`` is set, so an aggregate
    call returns the compact distribution rather than every full row.

    The paginated projections (``full`` and ``ids``, flat or grouped) are
    prefixed with a ``[N of M drifted nodes]`` count header (plus a
    ``(pass page=<next> for next page)`` hint when more rows remain),
    matching the sibling paginated tools.  ``format='full'`` additionally
    emits the comment-line column header (``# node_id  status_pair
    (own/link)  location  via``) once, after the count header.  Pagination
    is over the post-frozen-filter set ordered by id, so the count header
    is accurate even when ``frozen_tags`` drops rows; grouped output
    re-groups the page slice (groups may span page boundaries).
    ``format='counts'`` is a bounded distribution: unpaginated, no header.
    Empty-result sentinels (``(no matches)``, ``(page out of range)``)
    apply to flat and grouped ``full``/``ids`` alike.  Consumers parsing
    the output should skip lines starting with ``#`` or ``[``.

    Frozen-tag handling (controlled by ``config.staleness.frozen_tags``):
    when ``include_frozen`` is ``False`` (the default), rows whose owning
    doc carries a frozen tag are dropped from the output, EXCEPT
    BROKEN_LINK rows, which are retained with a ``[frozen-source]``
    postfix on ``format='full'``.  When ``include_frozen=True`` all
    rows are returned and frozen-doc rows get a ``[frozen]`` postfix
    on ``format='full'``.  Markers never appear on ``format='ids'``
    or ``format='counts'``.  No effect when ``frozen_tags`` is empty.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory (used to load ``frozen_tags`` from
            ``axiom-graph.toml``).
        filter: Status filter.
        location_glob: fnmatch-style path glob.
        group_by: ``None`` / ``"status"`` / ``"location_prefix"`` / ``"feature"``.
        format: ``None`` (conditional default -> ``full`` flat / ``counts``
            grouped) / ``"full"`` / ``"ids"`` / ``"counts"``.
        page: Zero-indexed page number (``full``/``ids`` only).
        limit: Page size (``full``/``ids`` only).
        include_frozen: When ``True``, include frozen-doc rows in
            output with ``[frozen]`` marker on ``format='full'``.

    Returns:
        Newline-delimited text projection.

    Raises:
        ValueError: Invalid ``filter``, ``group_by``, ``format``, or
            ``format='counts'`` without ``group_by``.
    """
    _FULL_HEADER = "# node_id  status_pair (own/link)  location  via"
    # Conditional default (single source of truth for both wrappers): an
    # unspecified format means a flat row list when ungrouped, but the
    # compact distribution when grouped -- so an aggregate call never dumps
    # full rows for the whole inventory.
    if format is None:
        format = "counts" if group_by is not None else "full"
    if format not in ("full", "ids", "counts"):
        raise ValueError(f"format must be one of full|ids|counts, got {format!r}")
    if group_by is not None and group_by not in ("status", "location_prefix", "feature"):
        raise ValueError(f"group_by must be one of None|status|location_prefix|feature, got {group_by!r}")
    if format == "counts" and group_by is None:
        raise ValueError("format='counts' requires group_by to be set")
    if page < 0:
        raise ValueError("page must be >= 0")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    # Validate filter early so the error is surface-level.
    from axiom_graph.db import staleness as st

    st.parse_drift_filter(filter)  # raises ValueError on bad filter

    # ------------------------------------------------------------------
    # Frozen-tag resolution (O(1) when frozen_tags is empty).
    # ------------------------------------------------------------------
    frozen_section_ids: set[str] = set()
    config = AxiomGraphConfig.load(root)
    if config.staleness.frozen_tags:
        frozen_doc_ids = db.get_doc_ids_with_tags(db_path, config.staleness.frozen_tags)
        if frozen_doc_ids:
            section_to_doc = db.get_section_doc_id_map(db_path, frozen_doc_ids)
            frozen_section_ids = set(section_to_doc.keys())

    def _row_is_frozen(row_id: str) -> bool:
        return row_id in frozen_section_ids

    def _filter_row(row: dict) -> tuple[bool, str]:
        """Decide whether to keep *row* and return (keep, marker).

        Returns (True, "") for non-frozen rows.  Returns (True, " [frozen]")
        for frozen rows when include_frozen is True.  Returns (True,
        " [frozen-source]") for frozen-doc BROKEN_LINK rows when
        include_frozen is False.  Returns (False, "") for frozen-doc
        non-BROKEN_LINK rows when include_frozen is False.
        """
        if not _row_is_frozen(row["id"]):
            return True, ""
        if include_frozen:
            return True, " [frozen]"
        if row["link_status"] == BROKEN_LINK:
            return True, " [frozen-source]"
        return False, ""

    # ------------------------------------------------------------------
    # Pagination helpers (shared by flat + grouped full/ids).
    #
    # Pagination is applied in this layer over the post-frozen-filter set
    # so the [N of M] header total is accurate even when frozen_tags drops
    # rows (a plain SQL COUNT would over-report).  Rows are globally
    # ordered by id, then sliced; grouped output re-groups the slice.
    # ``counts`` is a bounded distribution and is never paginated.
    # ------------------------------------------------------------------
    offset = max(0, page) * max(1, limit)

    def _page_header(shown: int, total: int) -> str:
        """``[N of M drifted nodes]`` (+ next-page hint), like sibling tools."""
        head = f"[{shown} of {total} drifted nodes]"
        if offset + limit < total:
            head += f"  (pass page={page + 1} for next page)"
        return head

    def _regroup(pairs: list[tuple[str, object]]) -> list[tuple[str, list]]:
        """Re-group ``(group, payload)`` pairs into alphabetically ordered
        buckets, preserving each payload's incoming (id) order."""
        grouped: dict[str, list] = {}
        for group, payload in pairs:
            grouped.setdefault(group, []).append(payload)
        return sorted(grouped.items())

    def _full_row_line(r: dict, marker: str, indent: str = "") -> str:
        via_part = "  via=" + ",".join(r["via"][:3]) if r["via"] else ""
        loc = r["location"] or "(no-location)"
        return f"{indent}{r['id']}  {r['own_status']}/{r['link_status']}  {loc}{via_part}{marker}"

    # ------------------------------------------------------------------
    # Flat (no group_by)
    # ------------------------------------------------------------------
    if group_by is None:
        rows = st.query_drift_rows(db_path, filter=filter, location_glob=location_glob)
        # Apply frozen-tag filter (always — _filter_row is a no-op when
        # frozen_section_ids is empty).  query_drift_rows already orders by id.
        kept: list[tuple[dict, str]] = []
        for r in rows:
            keep, marker = _filter_row(r)
            if keep:
                kept.append((r, marker))
        total = len(kept)
        if total == 0:
            return "(no matches)"
        if offset >= total:
            return "(page out of range)"
        page_rows = kept[offset : offset + limit]
        header = _page_header(len(page_rows), total)

        if format == "ids":
            # No markers in ids format.
            return "\n".join([header, *(r["id"] for r, _ in page_rows)])

        # format == "full"
        lines = [header, _FULL_HEADER]
        for r, marker in page_rows:
            lines.append(_full_row_line(r, marker))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Grouped
    # ------------------------------------------------------------------
    if format == "counts":
        if group_by == "status":
            buckets = st.query_drift_counts_by_status(db_path, filter=filter, location_glob=location_glob)
        elif group_by == "location_prefix":
            buckets = st.query_drift_counts_by_location_prefix(db_path, filter=filter, location_glob=location_glob)
        else:  # feature
            buckets = st.query_drift_counts_by_feature(db_path, filter=filter, location_glob=location_glob)
        # When frozen-tag filtering is active and there are frozen rows in
        # the underlying universe, we need to recompute counts from the
        # ids variant because we can't filter pre-aggregated counts.
        # Skip entirely when include_frozen=True — the original counts
        # query is already correct (no rows are dropped).
        if frozen_section_ids and not include_frozen:
            if group_by == "status":
                id_buckets = st.query_drift_ids_by_status(db_path, filter=filter, location_glob=location_glob)
            elif group_by == "location_prefix":
                id_buckets = st.query_drift_ids_by_location_prefix(db_path, filter=filter, location_glob=location_glob)
            else:
                id_buckets = st.query_drift_ids_by_feature(db_path, filter=filter, location_glob=location_glob)
            # Filter ids per bucket; link_status unknown here so
            # BROKEN_LINK retention can't be exercised in counts.  That's
            # acceptable since markers don't show in counts.
            buckets = []
            for b in id_buckets:
                kept_ids = [nid for nid in b["ids"] if nid not in frozen_section_ids]
                if kept_ids:
                    buckets.append({"group": b["group"], "count": len(kept_ids)})
        if not buckets:
            return "(no matches)"
        return "\n".join(f"{b['group']}  {b['count']}" for b in buckets)

    if format == "ids":
        if group_by == "status":
            buckets = st.query_drift_ids_by_status(db_path, filter=filter, location_glob=location_glob)
        elif group_by == "location_prefix":
            buckets = st.query_drift_ids_by_location_prefix(db_path, filter=filter, location_glob=location_glob)
        else:
            buckets = st.query_drift_ids_by_feature(db_path, filter=filter, location_glob=location_glob)
        # Flatten to (group, id), dropping frozen ids (ids carry no marker).
        items: list[tuple[str, object]] = []
        drop_frozen = bool(frozen_section_ids) and not include_frozen
        for b in buckets:
            for nid in b["ids"]:
                if drop_frozen and nid in frozen_section_ids:
                    continue
                items.append((b["group"], nid))
        items.sort(key=lambda t: t[1])  # global id order
        total = len(items)
        if total == 0:
            return "(no matches)"
        if offset >= total:
            return "(page out of range)"
        page_items = items[offset : offset + limit]
        out_lines = [_page_header(len(page_items), total)]
        for group, ids in _regroup(page_items):
            out_lines.append(f"[{group}]")
            for nid in ids:
                out_lines.append(f"  {nid}")
        return "\n".join(out_lines)

    # format == "full"
    if group_by == "status":
        buckets = st.query_drift_full_by_status(db_path, filter=filter, location_glob=location_glob)
    elif group_by == "location_prefix":
        buckets = st.query_drift_full_by_location_prefix(db_path, filter=filter, location_glob=location_glob)
    else:
        buckets = st.query_drift_full_by_feature(db_path, filter=filter, location_glob=location_glob)
    # Flatten to (group, (row, marker)), applying the frozen-tag filter
    # (_filter_row is a no-op when frozen_section_ids is empty and retains
    # frozen BROKEN_LINK rows with a [frozen-source] marker).
    items = []
    for b in buckets:
        for r in b["rows"]:
            keep, marker = _filter_row(r)
            if keep:
                items.append((b["group"], (r, marker)))
    items.sort(key=lambda t: t[1][0]["id"])  # global id order
    total = len(items)
    if total == 0:
        return "(no matches)"
    if offset >= total:
        return "(page out of range)"
    page_items = items[offset : offset + limit]
    out_lines = [_page_header(len(page_items), total), _FULL_HEADER]
    for group, members in _regroup(page_items):
        out_lines.append(f"[{group}]")
        for r, marker in members:
            out_lines.append(_full_row_line(r, marker, indent="  "))
    return "\n".join(out_lines)
