"""Public Python API for the query bounded context.

Per ADR-019 (cycle 3), the query domain owns every read-only inventory
operation against the axiom-graph index: full-text search,
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
    ``search_nodes``         -- keyword search over the index
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

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from axiom_graph.config import AxiomGraphConfig
from axiom_graph.index import db
from axiom_graph.index.builder import rescan_file_if_needed
from axiom_graph.index.status import BROKEN_LINK, LINKED_STALE, VERIFIED
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
# search
# ---------------------------------------------------------------------------


def search_nodes(
    db_path: Path,
    query: str,
    *,
    level: int | None = None,
    max_results: int = 20,
    node_type: str | None = None,
    scope: str = "all",
    tag: str | None = None,
    offset: int = 0,
    root: Path | None = None,
) -> str:
    """Run a keyword search over the axiom-graph index.

    Returns the formatted text result (the wire wrapper passes it through
    unchanged).  A three-stage fallback chain runs (FTS5 -> LIKE-AND ->
    LIKE-OR).  Header labels for each stage are part of the wire contract.

    With *root*, the statuses of the shown nodes are refreshed first
    (:func:`~axiom_graph.lifecycle.api.refresh_before_read`); a shown node
    that is not VERIFIED ends its line with ``  [STATUS, ...]`` and the
    read's notes follow after a blank line.  The call runs on one connection.

    Args:
        db_path: Path to the axiom-graph DB file.
        query: Search string.
        level: ``1`` searches level_1 only; ``2`` searches level_2 only;
            ``None`` (default) searches both.
        max_results: Maximum results to return.
        node_type: Optional node-type filter (raw value, no aliases).
        scope: ``"code"`` / ``"docs"`` / ``"all"``.
        tag: Optional tag filter.
        offset: Number of results to skip (default 0).
        root: Project root.  When given, the shown nodes are refreshed and
            tagged; when ``None`` the listing carries no statuses.

    Returns:
        Newline-delimited formatted search results.
    """
    logger.debug(
        "search_nodes: query=%r, max_results=%d, offset=%d",
        query,
        max_results,
        offset,
    )

    with _one_connection(db_path):
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
        rr = _refresh_shown(db_path, root, [n.id for n in nodes])
    result = agent.render_level_1(nodes, badges=rr.tags() if rr else None)
    mode_labels = {
        "fts": "fts ranked",
        "like_and": "LIKE-AND fallback",
        "like_or": "LIKE-OR fallback (broad, low-confidence)",
    }
    label = mode_labels.get(search_mode, search_mode)
    shown = len(nodes)
    header = f"[{shown} of {total} results -- {label}]"
    text = f"{header}\n{result}" if result != "(no nodes)" else header
    return _with_notes(text, rr)


def _one_connection(db_path: Path):
    """Return the operation scope for *db_path* (one connection per read), or a no-op when the DB is missing.

    A missing DB keeps the error its first query raised before (the scope
    would create the file).
    """
    if Path(db_path).exists():
        return db.operation_connection(db_path)
    return contextlib.nullcontext()


def _refresh_shown(db_path: Path, root: Path | None, node_ids: list[str]):
    """Refresh what a node-naming read shows (``refresh_before_read``), or nothing when *root* is ``None``.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root, or ``None`` for a caller that shows no statuses.
        node_ids: The nodes the read names.

    Returns:
        The :class:`~axiom_graph.lifecycle.api.ReadRefresh`, or ``None``.
    """
    if root is None:
        return None
    from axiom_graph.lifecycle.api import refresh_before_read  # noqa: PLC0415

    return refresh_before_read(db_path, root, node_ids)


def _with_notes(text: str, rr) -> str:
    """Append a read's notes (behind / structural lines) after a blank line, when it has any."""
    notes = rr.notes() if rr is not None else []
    return "\n".join([text, "", *notes]) if notes else text


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
    root: Path | None = None,
) -> GraphResult:
    """Traverse and render the edge graph for a node.

    Shared orchestration for ``axiom-graph graph`` and ``axiom_graph_graph``.
    Returns a :class:`GraphResult`; the CLI raises ``ClickException`` on
    not_found, the MCP wire wrapper formats both not_found and the
    optional truncation hint into a string.

    The call runs on one connection.  With *root*, the statuses of the
    root and of every node the shown edges name are refreshed first
    (:func:`~axiom_graph.lifecycle.api.refresh_before_read`); each line
    naming a node that is not VERIFIED ends with ``  [STATUS, ...]`` and the
    read's notes follow after a blank line.

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
        root: Project root.  When given, the shown nodes are refreshed and
            tagged; when ``None`` (the CLI) the tree carries no statuses.

    Returns:
        :class:`GraphResult`.
    """
    with _one_connection(db_path):
        sl = _graph_slice(db_path, node_id, direction, depth, max_results, offset)
        if sl is None:
            return GraphResult(rendered="", shown=0, total_edges=0, truncated=False, not_found=True)
        node_lookup = None
        if with_locations:
            # Location lookup for all nodes appearing in the traversal, one batched read.
            with db._connect(db_path) as conn:
                node_lookup = db.get_nodes_conn(conn, sl.all_ids)
        rr = _refresh_shown(db_path, root, sl.all_ids)
    return _render_graph_slice(sl, direction, node_lookup, rr)


@dataclass
class BatchItem:
    """One entry of a batch read (:func:`fetch_graph_batch`, :func:`fetch_source_batch`).

    Attributes:
        node_id: The id the entry asked for.
        result: The entry's :class:`GraphResult` or :class:`NodeSource`, when it was read.
        error: The exception reading the entry raised, when it failed on its own.
    """

    node_id: str
    result: GraphResult | NodeSource | None = None
    error: Exception | None = None


@dataclass
class _GraphSlice:
    """What :func:`fetch_graph` read for one node before the refresh: the node, its edge slice, the ids it names."""

    node: object
    edges: list
    shown: int
    total_edges: int
    truncated: bool
    all_ids: list[str]


def _graph_slice(
    db_path: Path, node_id: str, direction: str, depth: int, max_results: int, offset: int
) -> _GraphSlice | None:
    """Read a node and the slice of its edges a graph read shows, or ``None`` when the node is unknown."""
    node = db.get_node(db_path, node_id)
    if node is None:
        return None
    edges = db.query_edges(db_path, node_id, direction=direction, depth=depth)

    total_edges = len(edges)
    edges = edges[offset:]
    truncated = len(edges) > max_results
    if truncated:
        edges = edges[:max_results]

    # Every node the slice names: the root, then each edge's endpoints.
    all_ids = list(dict.fromkeys([node_id, *(nid for e in edges for nid in (e.from_id, e.to_id))]))
    return _GraphSlice(
        node=node, edges=edges, shown=len(edges), total_edges=total_edges, truncated=truncated, all_ids=all_ids
    )


def _render_graph_slice(sl: _GraphSlice, direction: str, node_lookup, rr) -> GraphResult:
    """Render a graph slice with the statuses and notes of *rr* (``None``: no statuses)."""
    rendered = agent.render_graph(
        sl.node, sl.edges, direction=direction, node_lookup=node_lookup, badges=rr.tags() if rr else None
    )
    return GraphResult(
        rendered=_with_notes(rendered, rr),
        shown=sl.shown,
        total_edges=sl.total_edges,
        truncated=sl.truncated,
    )


def _narrowed(rr, node_ids: list[str], lookup: dict):
    """Cut a batch's shared refresh down to one entry's nodes and their files (``None`` stays ``None``)."""
    if rr is None:
        return None
    return rr.narrowed(node_ids, {lookup[nid].location for nid in node_ids if nid in lookup})


def fetch_graph_batch(
    db_path: Path,
    node_ids: list[str],
    *,
    direction: str = "out",
    depth: int = 1,
    max_results: int = 40,
    offset: int = 0,
    with_locations: bool = False,
    root: Path | None = None,
) -> list[BatchItem]:
    """Traverse the edge graph for several nodes on one connection with one refresh.

    Each node's slice is read first (an exception there fails only that
    entry); then one batched node lookup and one refresh cover every node
    any slice names; then each entry is rendered from a view narrowed to its
    own nodes, so its text equals what :func:`fetch_graph` returns for it.
    An exception in the shared lookup or refresh fails the whole call.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: The starting nodes, in output order.
        direction: ``"out"`` / ``"in"`` / ``"both"``.
        depth: Number of hops to traverse.
        max_results: Maximum number of edges in each slice.
        offset: Number of edges to skip in each slice.
        with_locations: Pass each slice's node lookup to the renderer.
        root: Project root.  When given, the shown nodes are refreshed and tagged.

    Returns:
        One :class:`BatchItem` per id, in order.
    """
    items = [BatchItem(node_id=nid) for nid in node_ids]
    slices: dict[int, _GraphSlice] = {}
    lookup: dict = {}
    rr = None
    with _one_connection(db_path):
        for i, item in enumerate(items):
            try:
                sl = _graph_slice(db_path, item.node_id, direction, depth, max_results, offset)
            except Exception as exc:  # noqa: BLE001 -- one entry's failure is reported on that entry
                item.error = exc
                continue
            if sl is None:
                item.result = GraphResult(rendered="", shown=0, total_edges=0, truncated=False, not_found=True)
            else:
                slices[i] = sl
        shown = list(dict.fromkeys(nid for sl in slices.values() for nid in sl.all_ids))
        if shown:
            with db._connect(db_path) as conn:
                lookup = db.get_nodes_conn(conn, shown)
            rr = _refresh_shown(db_path, root, shown)
    for i, sl in slices.items():
        try:
            node_lookup = {nid: lookup[nid] for nid in sl.all_ids if nid in lookup} if with_locations else None
            items[i].result = _render_graph_slice(sl, direction, node_lookup, _narrowed(rr, sl.all_ids, lookup))
        except Exception as exc:  # noqa: BLE001 -- one entry's failure is reported on that entry
            items[i].error = exc
    return items


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

    The call runs on one connection.  After the file rescan, the statuses of
    the node (and of the children a truncated module lists) are refreshed
    (:func:`~axiom_graph.lifecycle.api.refresh_before_read`); the ``# id``
    header line and each child entry naming a node that is not VERIFIED end
    with ``  [STATUS, ...]``, and the read's notes follow after a blank line.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root (used to resolve relative paths in
            ``level_3_location``).
        node_id: Full node ID.

    Returns:
        :class:`NodeSource` with the rendered body text plus diagnostic
        flags for not-found / no-location / file-missing.
    """
    with _one_connection(db_path):
        prep = _source_slice(db_path, root, node_id)
        if isinstance(prep, NodeSource):
            return prep
        rr = _refresh_shown(db_path, root, prep.shown_ids)
    return _render_source_slice(prep, rr)


def fetch_source_batch(db_path: Path, root: Path, node_ids: list[str]) -> list[BatchItem]:
    """Return the source bodies of several nodes on one connection with one refresh.

    Each node is read first (rescan, location, file lines, a truncated
    module's children; an exception there fails only that entry); then one
    batched node lookup and one refresh cover every node any entry names;
    then each entry is rendered from a view narrowed to its own nodes, so its
    text equals what :func:`fetch_source` returns for it.  An exception in
    the shared lookup or refresh fails the whole call.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root (resolves the relative paths in ``level_3_location``).
        node_ids: Full node ids, in output order.

    Returns:
        One :class:`BatchItem` per id, in order.
    """
    items = [BatchItem(node_id=nid) for nid in node_ids]
    preps: dict[int, _SourceSlice] = {}
    lookup: dict = {}
    rr = None
    with _one_connection(db_path):
        for i, item in enumerate(items):
            try:
                prep = _source_slice(db_path, root, item.node_id)
            except Exception as exc:  # noqa: BLE001 -- one entry's failure is reported on that entry
                item.error = exc
                continue
            if isinstance(prep, NodeSource):
                item.result = prep
            else:
                preps[i] = prep
        shown = list(dict.fromkeys(nid for prep in preps.values() for nid in prep.shown_ids))
        if shown:
            with db._connect(db_path) as conn:
                lookup = db.get_nodes_conn(conn, shown)
            rr = _refresh_shown(db_path, root, shown)
    for i, prep in preps.items():
        try:
            items[i].result = _render_source_slice(prep, _narrowed(rr, prep.shown_ids, lookup))
        except Exception as exc:  # noqa: BLE001 -- one entry's failure is reported on that entry
            items[i].error = exc
    return items


@dataclass
class _SourceSlice:
    """What :func:`fetch_source` read for one node before the refresh."""

    node_id: str
    loc: str
    lines: list[str]
    start: int | None
    end: int | None
    children: list | None
    shown_ids: list[str]


def _source_slice(db_path: Path, root: Path, node_id: str) -> _SourceSlice | NodeSource:
    """Read a node's file slice (rescanning its file first), or the :class:`NodeSource` that reports why not."""
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
    children = None
    shown_ids = [node_id]
    if start is None and node.node_type == "composite_process" and len(all_lines) > 200:
        children = db.query_children(db_path, node_id)
        shown_ids = [node_id, *(c.id for c in children)]
    return _SourceSlice(
        node_id=node_id, loc=loc, lines=all_lines, start=start, end=end, children=children, shown_ids=shown_ids
    )


def _render_source_slice(prep: _SourceSlice, rr) -> NodeSource:
    """Render a source slice with the statuses and notes of *rr*: the body, or a truncated module's TOC."""
    node_id, loc, all_lines = prep.node_id, prep.loc, prep.lines
    if prep.children is not None:
        preview = "\n".join(all_lines[:50])
        child_lines = []
        for child in prep.children:
            cloc = child.level_3_location or ""
            entry = f"- {child.id}"
            if cloc:
                entry += f"  @ {cloc}"
            child_lines.append(entry + rr.tag(child.id))
        parts = [
            f"# {node_id}  @ {loc}{rr.tag(node_id)}",
            "",
            f"Module has {len(all_lines)} lines and {len(prep.children)} functions. Showing first 50 lines.",
            "Use axiom_graph_source on a specific function for the full body.",
            "",
            preview,
        ]
        if child_lines:
            parts.append("")
            parts.append("Children (use axiom_graph_source with these IDs):")
            parts.extend(child_lines)
        return NodeSource(text=_with_notes("\n".join(parts), rr), location=loc)

    body = "\n".join(all_lines[prep.start - 1 : prep.end] if prep.start is not None else all_lines)
    return NodeSource(text=_with_notes(f"# {node_id}  @ {loc}{rr.tag(node_id)}\n\n{body}", rr), location=loc)


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
    conn = db.open_connection(db_path, read_only=True)
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


def node_tags(db_path: Path, node_ids: list[str]) -> dict[str, list[str]]:
    """Return the tags of the nodes in *node_ids* that have any.

    Reads in batches, on the caller's operation connection when one is open.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: The node ids.

    Returns:
        ``{node_id: [tag, ...]}``, tags in row order.
    """
    if not node_ids:
        return {}
    with db._connect(db_path) as conn:
        return db.get_tags_bulk_conn(conn, list(node_ids))


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

# Offender ids shown per via=/root= list before "(+N more)".
_OFFENDER_DISPLAY_CAP = 10


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
    """Refresh the stored statuses (``refresh_before_read``), then project them; see :func:`_drift_projection`.

    One connection per call.  Under ``"changed-files"`` (the default) the
    incremental check runs first, so the listing is current; under
    ``"off"`` the stored statuses are listed and a trailing
    ``[index is behind for N files — run `check`]`` line says how many
    tracked files moved since the index last read them; ``"check"`` runs
    the check first.  Files whose last re-hash found structure the index
    lacks add one ``[<file> has ... — run build]`` line each.  These lines
    follow the projection after a blank line and start with ``[``, so a
    consumer that skips lines starting with ``#`` or ``[`` skips them.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory.
        filter: Status filter.
        location_glob: Path glob over node locations.
        group_by: ``None`` / ``"status"`` / ``"location_prefix"`` / ``"feature"`` / ``"node_kind"``.
        format: ``None`` / ``"full"`` / ``"ids"`` / ``"counts"``.
        page: Zero-indexed page number.
        limit: Page size.
        include_frozen: Include frozen-doc rows.

    Returns:
        Newline-delimited text projection, then any notes.

    Raises:
        ValueError: As :func:`_drift_projection`, before anything is refreshed.
    """
    notes: list[str] = []
    kwargs = {
        "filter": filter,
        "location_glob": location_glob,
        "group_by": group_by,
        "format": format,
        "page": page,
        "limit": limit,
        "include_frozen": include_frozen,
    }
    if not Path(db_path).exists():
        # Argument errors still surface first; the projection opens nothing before them.
        return _drift_projection(db_path, root, notes_out=notes, **kwargs)
    with db.operation_connection(db_path):
        text = _drift_projection(db_path, root, notes_out=notes, **kwargs)
    return "\n".join([text, "", *notes]) if notes else text


def _drift_projection(
    db_path: Path,
    root: Path,
    *,
    notes_out: list[str],
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
    emits a ``#`` comment-line column header once, after the count
    header, and labels each row's fields:
    ``id=<node_id>  <own>/<link>  loc=<location>  via=...  root=...``.

    ``via`` on a LINKED_STALE row lists its direct offenders from the
    computed stale map (``_get_linked_stale_ids``), and ``root`` its leaf
    root offenders (``resolve_root_offenders``, the resolver ``reverify``
    uses) when they differ from ``via``.  Rows absent from the map
    (frozen, composite-inherited) fall back to the persisted-status proxy
    ``via``.  Each list shows at most 10 ids, then ``(+N more)``.
    Doc-quality advisory rows (``filter='all'`` / ``'doc_quality'``)
    carry a ``[DOC_SECTION_LONG]`` label.  Pagination
    is over the post-frozen-filter set ordered by id, so the count header
    is accurate even when ``frozen_tags`` drops rows; grouped output
    re-groups the page slice (groups may span page boundaries).
    ``format='counts'`` is a bounded distribution: unpaginated, no header.
    Empty-result sentinels (``(no matches)``, ``(page out of range)``)
    apply to flat and grouped ``full``/``ids`` alike.  Consumers parsing
    the output should skip lines starting with ``#`` or ``[``.

    Frozen-tag handling (controlled by ``config.staleness.frozen_tags``):
    when ``include_frozen`` is ``False`` (the default), the rows of a doc
    carrying a frozen tag, its doc node (the envelope) included, are
    dropped from the output, EXCEPT rows
    whose link status is BROKEN_LINK, which are retained in every format
    (``full``, ``ids`` and ``counts``, flat or grouped) — with a
    ``[frozen-source]`` postfix on ``format='full'`` — so the counts and
    the listed rows always agree.  When ``include_frozen=True`` all
    rows are returned and frozen-doc rows get a ``[frozen]`` postfix
    on ``format='full'``.  Markers never appear on ``format='ids'``
    or ``format='counts'``.  No effect when ``frozen_tags`` is empty.

    Args:
        db_path: Path to the axiom-graph DB.
        root: Project root directory (used to load ``frozen_tags`` from
            ``axiom-graph.toml``).
        filter: Status filter.
        location_glob: Path glob over node locations: ``*`` / ``?``
            stay within one path segment, ``**`` crosses directories,
            ``[!x]`` and ``{a,b}`` are supported.  Matches the whole
            location or its path part before a ``#Lx-Ly`` fragment.
        group_by: ``None`` / ``"status"`` / ``"location_prefix"`` (path
            prefix, ``#...`` line suffix dropped) / ``"feature"`` /
            ``"node_kind"`` (``code`` / ``test`` / ``doc``; ``test`` uses
            ``scan.test_paths`` from the project config).
        format: ``None`` (conditional default -> ``full`` flat / ``counts``
            grouped) / ``"full"`` / ``"ids"`` / ``"counts"``.
        page: Zero-indexed page number (``full``/``ids`` only).
        limit: Page size (``full``/``ids`` only).
        include_frozen: When ``True``, include the rows of frozen docs
            (sections and the doc node itself) in output with
            ``[frozen]`` marker on ``format='full'``.

    Returns:
        Newline-delimited text projection.

    Raises:
        ValueError: Invalid ``filter`` (including ``'VERIFIED'``),
            malformed ``location_glob``, invalid ``group_by`` or
            ``format``, or ``format='counts'`` without ``group_by``.
    """
    _FULL_HEADER = (
        "# id=<node_id>  <own>/<link>  loc=<location>  via=<direct offenders>  root=<root offenders, when different>"
    )
    # Conditional default (single source of truth for both wrappers): an
    # unspecified format means a flat row list when ungrouped, but the
    # compact distribution when grouped -- so an aggregate call never dumps
    # full rows for the whole inventory.
    if format is None:
        format = "counts" if group_by is not None else "full"
    if format not in ("full", "ids", "counts"):
        raise ValueError(f"format must be one of full|ids|counts, got {format!r}")
    if group_by is not None and group_by not in ("status", "location_prefix", "feature", "node_kind"):
        raise ValueError(f"group_by must be one of None|status|location_prefix|feature|node_kind, got {group_by!r}")
    if format == "counts" and group_by is None:
        raise ValueError("format='counts' requires group_by to be set")
    if page < 0:
        raise ValueError("page must be >= 0")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    # Validate filter and glob early so the error is surface-level.
    from axiom_graph.db import staleness as st

    st.parse_drift_filter(filter)  # raises ValueError on bad filter
    if location_glob is not None:
        st._glob_to_regex(location_glob)  # raises ValueError on a malformed glob

    # Arguments are valid: bring the stored statuses up to date (or count how
    # far behind they are) before listing them.
    from axiom_graph.lifecycle.api import refresh_before_read  # noqa: PLC0415

    notes_out.extend(refresh_before_read(db_path, root).notes())

    # ------------------------------------------------------------------
    # Frozen-tag resolution (O(1) when frozen_tags is empty).
    # ------------------------------------------------------------------
    # Every node of a frozen doc, its envelope included: the one frozen-row
    # set check and the build counts use.
    config = AxiomGraphConfig.load(root)
    frozen_rows = db.get_frozen_rows(db_path, config.staleness.frozen_tags)
    frozen_section_ids: set[str] = set(frozen_rows)

    def _row_is_frozen(row_id: str) -> bool:
        return row_id in frozen_section_ids

    # Frozen ids the default filter must still drop: every frozen row
    # whose persisted link status is not BROKEN_LINK.  The grouped
    # counts/ids projections carry ids only, so they read it from here;
    # _filter_row applies the same rule to full rows.
    dropped_frozen_ids: set[str] = set()
    if frozen_section_ids and not include_frozen:
        dropped_frozen_ids = {nid for nid, (_own, link) in frozen_rows.items() if link != BROKEN_LINK}

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

    # ------------------------------------------------------------------
    # Offender attribution (format='full' only).  LINKED_STALE rows get
    # their direct offenders (``via``) and leaf root offenders (``root``)
    # from the computed stale map -- the same map and resolver
    # ``reverify`` uses -- rather than the persisted-own_status proxy.
    # Rows absent from the map (frozen, composite-inherited) keep the
    # proxy ``via``.  Computed lazily, once, only when a page row needs it.
    # ------------------------------------------------------------------
    attribution: dict[str, tuple[list[str], list[str]]] = {}

    def _attribute(page_rows: list[dict]) -> None:
        if not any(r["link_status"] == LINKED_STALE for r in page_rows):
            return
        from axiom_graph.index.staleness import _get_linked_stale_ids, resolve_root_offenders

        # Evaluate the page's LINKED_STALE rows, then the vias they name, hop
        # by hop, until no via is left unevaluated: the map holds the page's
        # via chains and nothing else, and each entry is the one the whole
        # stale map holds (a scoped pass lists the same vias).
        stale_map: dict[str, list[str]] = {}
        evaluated: set[str] = set()
        frontier = {r["id"] for r in page_rows if r["link_status"] == LINKED_STALE}
        while frontier:
            part = _get_linked_stale_ids(
                db_path,
                transitive_tags=config.staleness.transitive_tags,
                frozen_tags=config.staleness.frozen_tags,
                scope=frontier,
            )
            evaluated |= frontier
            found = {nid: vias for nid, vias in part.items() if nid in frontier}
            stale_map.update(found)
            frontier = {v for vias in found.values() for v in vias} - evaluated
        roots_map = resolve_root_offenders(stale_map)
        for r in page_rows:
            if r["link_status"] == LINKED_STALE and r["id"] in stale_map:
                attribution[r["id"]] = (list(stale_map[r["id"]]), roots_map.get(r["id"], []))

    def _offender_list(ids: list[str]) -> str:
        shown = ",".join(ids[:_OFFENDER_DISPLAY_CAP])
        extra = len(ids) - _OFFENDER_DISPLAY_CAP
        return f"{shown} (+{extra} more)" if extra > 0 else shown

    def _full_row_line(r: dict, marker: str, indent: str = "") -> str:
        via, roots = attribution.get(r["id"], (r["via"], []))
        via_part = f"  via={_offender_list(via)}" if via else ""
        root_part = f"  root={_offender_list(roots)}" if roots and sorted(roots) != sorted(via) else ""
        loc = r["location"] or "(no-location)"
        advisory = " [DOC_SECTION_LONG]" if r.get("long_section") else ""
        return (
            f"{indent}id={r['id']}  {r['own_status']}/{r['link_status']}  loc={loc}"
            f"{via_part}{root_part}{advisory}{marker}"
        )

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
        _attribute([r for r, _ in page_rows])
        lines = [header, _FULL_HEADER]
        for r, marker in page_rows:
            lines.append(_full_row_line(r, marker))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Grouped
    # ------------------------------------------------------------------
    # One table per (projection, axis), so every format -- and the frozen
    # recount below -- dispatches the same axis the same way.
    grouped_fns = {
        "counts": {
            "status": st.query_drift_counts_by_status,
            "location_prefix": st.query_drift_counts_by_location_prefix,
            "feature": st.query_drift_counts_by_feature,
            "node_kind": st.query_drift_counts_by_node_kind,
        },
        "ids": {
            "status": st.query_drift_ids_by_status,
            "location_prefix": st.query_drift_ids_by_location_prefix,
            "feature": st.query_drift_ids_by_feature,
            "node_kind": st.query_drift_ids_by_node_kind,
        },
        "full": {
            "status": st.query_drift_full_by_status,
            "location_prefix": st.query_drift_full_by_location_prefix,
            "feature": st.query_drift_full_by_feature,
            "node_kind": st.query_drift_full_by_node_kind,
        },
    }

    def _grouped(projection: str) -> list[dict]:
        """Run the ``projection`` (counts / ids / full) helper for ``group_by``."""
        fn = grouped_fns[projection][group_by]
        if group_by == "node_kind":
            return fn(db_path, filter=filter, location_glob=location_glob, test_paths=config.scan.test_paths)
        return fn(db_path, filter=filter, location_glob=location_glob)

    if format == "counts":
        buckets = _grouped("counts")
        # When frozen-tag filtering is active and there are frozen rows in
        # the underlying universe, we need to recompute counts from the
        # ids variant because we can't filter pre-aggregated counts.
        # Skip entirely when include_frozen=True — the original counts
        # query is already correct (no rows are dropped).
        if frozen_section_ids and not include_frozen:
            id_buckets = _grouped("ids")
            # Same retention rule as _filter_row: a frozen BROKEN_LINK row
            # stays counted, every other frozen row is dropped.
            buckets = []
            for b in id_buckets:
                kept_ids = [nid for nid in b["ids"] if nid not in dropped_frozen_ids]
                if kept_ids:
                    buckets.append({"group": b["group"], "count": len(kept_ids)})
        if not buckets:
            return "(no matches)"
        return "\n".join(f"{b['group']}  {b['count']}" for b in buckets)

    if format == "ids":
        buckets = _grouped("ids")
        # Flatten to (group, id), dropping frozen non-BROKEN_LINK ids (ids
        # carry no marker).
        items: list[tuple[str, object]] = []
        for b in buckets:
            for nid in b["ids"]:
                if nid in dropped_frozen_ids:
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
    buckets = _grouped("full")
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
    _attribute([r for _, (r, _m) in page_items])
    out_lines = [_page_header(len(page_items), total), _FULL_HEADER]
    for group, members in _regroup(page_items):
        out_lines.append(f"[{group}]")
        for r, marker in members:
            out_lines.append(_full_row_line(r, marker, indent="  "))
    return "\n".join(out_lines)
