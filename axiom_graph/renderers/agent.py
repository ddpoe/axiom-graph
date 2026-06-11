"""Cortex agent renderer — formats AxiomNode / AxiomEdge data as plain text.

All functions return str. Designed for CLI output and MCP tool responses.

Functions
---------
render_level_0(nodes)   — one line per node: ``{id}``
render_level_1(nodes)   — one line per node: ``{id}  {level_1}  @ location`` (functions only)
render_level_2(nodes)   — full level_2 block per node
render_steps(nodes)     — numbered step list for nodes that have level_steps
render_graph(node, edges, direction) — ASCII tree of connected edges
"""

from __future__ import annotations

from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Level renderers
# ---------------------------------------------------------------------------


def render_level_0(nodes: list[AxiomNode]) -> str:
    """One line per node: just the id."""
    if not nodes:
        return "(no nodes)"
    return "\n".join(n.id for n in nodes)


def render_level_1(nodes: list[AxiomNode]) -> str:
    """One line per node: ``{id}  {level_1}  @ {location}`` (location only for function nodes)."""
    if not nodes:
        return "(no nodes)"
    # Align level_1 by padding id to the longest id length
    max_id = max(len(n.id) for n in nodes)
    lines = []
    for n in nodes:
        line = f"{n.id:<{max_id}}  {n.level_1}"
        # Only append location for function-level nodes (line range present, e.g. #L10-L45)
        if n.level_3_location and "#L" in n.level_3_location:
            line += f"  @ {n.level_3_location}"
        lines.append(line)
    return "\n".join(lines)


def render_level_2(nodes: list[AxiomNode]) -> str:
    """Full detail block per node: id, level_1 header, then level_2 body."""
    if not nodes:
        return "(no nodes)"
    parts = []
    for n in nodes:
        header = f"=== {n.id} ==="
        subheader = f"  {n.level_1}"
        if n.level_2:
            body = _indent(n.level_2, "  ")
        else:
            body = "  (no detail)"
        if n.level_3_location:
            body += f"\n  @ {n.level_3_location}"
        parts.append("\n".join([header, subheader, body]))
    return "\n\n".join(parts)


def render_steps(nodes: list[AxiomNode]) -> str:
    """Numbered step list for nodes that have level_steps set."""
    nodes_with_steps = [n for n in nodes if n.level_steps]
    if not nodes_with_steps:
        return "(no nodes with step markers)"
    parts = []
    for n in nodes_with_steps:
        lines = [f"=== {n.id} ==="]
        for step in n.level_steps:  # type: ignore[union-attr]
            prefix = f"  [{step.step_num}] {step.name}"
            lines.append(prefix)
            lines.append(f"       purpose : {step.purpose}")
            if step.inputs:
                lines.append(f"       inputs  : {step.inputs}")
            if step.outputs:
                lines.append(f"       outputs : {step.outputs}")
            if step.critical:
                lines.append(f"       CRITICAL: {step.critical}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Graph renderer
# ---------------------------------------------------------------------------


def render_graph(
    node: AxiomNode,
    edges: list[AxiomEdge],
    direction: str = "out",
    node_lookup: dict[str, AxiomNode] | None = None,
) -> str:
    """ASCII tree showing edge_type and connected node ids.

    For depth > 1, renders a proper BFS tree rather than a flat list of
    multi-hop edges, so the traversal path is visually clear.

    Parameters
    ----------
    node:
        The root node of the traversal.
    edges:
        Edges returned by ``db.query_edges()``.
    direction:
        ``"out"``, ``"in"``, or ``"both"``.
    node_lookup:
        Optional mapping of node_id → AxiomNode.  When provided, function-
        level nodes (those with ``#L`` in their location) get a
        ``@ path#L10-L45`` suffix on every tree line.
    """

    def _fmt(nid: str) -> str:
        """Return ``nid  @ location`` for function nodes when lookup is set."""
        if node_lookup and nid in node_lookup:
            loc = node_lookup[nid].level_3_location
            if loc and "#L" in loc:
                return f"{nid}  @ {loc}"
        return nid

    root_loc = ""
    if node.level_3_location and "#L" in node.level_3_location:
        root_loc = f"  @ {node.level_3_location}"
    lines = [f"[{node.node_type}] {node.id}{root_loc}"]

    if not edges:
        lines.append("  (no edges)")
        return "\n".join(lines)

    # Build adjacency maps for tree rendering
    # out_adj: from_id -> list[(edge_type, to_id)]
    # in_adj:  to_id   -> list[(edge_type, from_id)]
    out_adj: dict[str, list[tuple[str, str]]] = {}
    in_adj: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        out_adj.setdefault(e.from_id, []).append((e.edge_type, e.to_id))
        in_adj.setdefault(e.to_id, []).append((e.edge_type, e.from_id))

    def _render_tree_out(current_id: str, visited: set[str], indent: str) -> list[str]:
        result = []
        for etype, child_id in out_adj.get(current_id, []):
            result.append(f"{indent}--[{etype}]--> {_fmt(child_id)}")
            if child_id not in visited:
                visited.add(child_id)
                result.extend(_render_tree_out(child_id, visited, indent + "    "))
        return result

    def _render_tree_in(current_id: str, visited: set[str], indent: str) -> list[str]:
        result = []
        for etype, parent_id in in_adj.get(current_id, []):
            result.append(f"{indent}<--[{etype}]-- {_fmt(parent_id)}")
            if parent_id not in visited:
                visited.add(parent_id)
                result.extend(_render_tree_in(parent_id, visited, indent + "    "))
        return result

    root_id = node.id

    if direction in ("out", "both"):
        out_lines = _render_tree_out(root_id, {root_id}, "  ")
        if out_lines:
            lines.append("  outgoing:")
            lines.extend(out_lines)

    if direction in ("in", "both"):
        in_lines = _render_tree_in(root_id, {root_id}, "  ")
        if in_lines:
            lines.append("  incoming:")
            lines.extend(in_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
