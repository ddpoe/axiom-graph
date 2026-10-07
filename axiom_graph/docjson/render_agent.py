"""Agent-facing doc rendering.

Phase 4 (ADR-005): extracted from ``axiom_graph/mcp/_helpers.py``.

Renders DocJSON documents as Markdown for consumption by MCP agents / tools.
Unlike the consumer renderer (``docjson.render_consumer``) which produces clean
output for human-facing static sites, this renderer includes section ID
annotations (``<!-- id: ... -->``) and linked-node lists so agents can
programmatically reference sections and follow cross-references.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from axiom_graph.index import db

#: Marker pair the generated linked-nodes footer is wrapped in.  The write
#: path strips a block between these markers from incoming section content.
LINKED_NODES_OPEN = "<!-- axiom:linked-nodes -->"
LINKED_NODES_CLOSE = "<!-- /axiom:linked-nodes -->"


def _render_doc_header(title: str) -> str:
    """Render the ``# title`` line that opens a rendered doc.

    Args:
        title: Document title.

    Returns:
        The header text; joined to the section blocks with ``"\n"``.
    """
    return "\n".join([f"# {title}", ""])


def _render_doc_meta(doc_id: str, tags: list[str], file_path: str, status: str = "") -> str:
    """Render the generated comment line naming a doc's id, tags and file.

    It follows the ``# title`` line of a ``read_doc`` render, above every
    section, so it is never part of a section's content.

    Args:
        doc_id: The doc node ID.
        tags: The doc's tags, in stored order.
        file_path: The doc's file, relative to the project root.
        status: The doc envelope's ``"  [STATUS, ...]"`` tag when it is not
            VERIFIED, after the comment; ``""`` leaves the line unchanged.

    Returns:
        The comment line plus a trailing newline; joined to the section blocks
        with ``"\n"``.
    """
    return f"<!-- doc: {doc_id}  tags: {', '.join(tags) if tags else '(none)'}  file: {file_path} -->{status}\n"


#: A linked node as the footer lists it: ``(node id, level_1 summary, level_3_location)``.
LinkedNode = tuple[str, str, str]


def linked_nodes(db_path: "Path", section_docs: dict[str, str]) -> dict[str, list[LinkedNode]]:
    """Return the nodes each section links (its ``documents`` edges, the doc-level edge excluded), batched.

    Args:
        db_path: Path to the axiom-graph DB.
        section_docs: Section id -> the id of the doc it belongs to.

    Returns:
        Section id -> its linked nodes ordered by node id, as
        :func:`_render_section_block` lists them.
    """
    out: dict[str, list[LinkedNode]] = {sid: [] for sid in section_docs}
    ids = list(section_docs)
    with db._connect(db_path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            rows = conn.execute(
                f"""
                SELECT e.from_id, e.to_id, n.level_1, n.level_3_location
                FROM edges e
                LEFT JOIN nodes n ON n.id = e.to_id
                WHERE e.edge_type = 'documents'
                  AND e.from_id IN ({",".join("?" * len(chunk))})
                ORDER BY e.from_id, e.to_id
                """,
                chunk,
            ).fetchall()
            for r in rows:
                if r["to_id"] != section_docs[r["from_id"]]:
                    out[r["from_id"]].append((r["to_id"], r["level_1"] or "", r["level_3_location"] or ""))
    return out


def _render_section_block(
    db_path: "Path",
    doc_id: str,
    sec: dict,
    *,
    linked: list[LinkedNode] | None = None,
    tag: Callable[[str], str] | None = None,
) -> str:
    """Render one section: heading with id annotation, content, linked-nodes footer.

    The linked-nodes footer is wrapped in ``LINKED_NODES_OPEN`` /
    ``LINKED_NODES_CLOSE`` so a copy pasted back into content is recognisable
    as generated.

    Args:
        db_path: Path to the axiom-graph DB.
        doc_id: The doc node ID (its doc-level edge is not a linked node).
        sec: Section dict from the DB.
        linked: The section's linked nodes (:func:`linked_nodes`); read
            from the index when ``None``.
        tag: Node id -> its ``"  [STATUS, ...]"`` tag, or ``""``: appended
            to the heading line (after the id annotation) and to each
            linked-node entry.  ``None`` tags nothing.

    Returns:
        The section's Markdown, ending with a blank line.
    """
    heading = sec["heading"]
    content = sec.get("content") or ""
    level = sec.get("level") or 2
    prefix = "#" * max(2, min(level, 6))
    sec_raw_id = sec.get("id", "")
    id_annotation = f"  <!-- id: {sec_raw_id} -->" if sec_raw_id else ""
    status = tag(sec["id"]) if tag is not None else ""
    lines: list[str] = [f"{prefix} {heading}{id_annotation}{status}", ""]
    if content:
        lines.append(content)
        lines.append("")

    if linked is None:
        # Collect linked code nodes (edges: section -> node, excluding -> doc)
        linked = linked_nodes(db_path, {sec["id"]: doc_id})[sec["id"]]

    if linked:
        lines.append(LINKED_NODES_OPEN)
        lines.append("**Linked nodes:**")
        for nid, summary, loc in linked:
            entry = f"- `{nid}`"
            if summary:
                entry += f" — {summary}"
            if loc and "#L" in loc:
                entry += f"  @ {loc}"
            if tag is not None:
                entry += tag(nid)
            lines.append(entry)
        lines.append(LINKED_NODES_CLOSE)
        lines.append("")

    return "\n".join(lines)


def _render_doc_markdown(
    db_path: "Path",
    doc_id: str,
    title: str,
    sections: list[dict],
) -> str:
    """Render a doc + its sections as a Markdown string.

    Args:
        db_path: Path to the axiom-graph DB.
        doc_id: The doc node ID.
        title: Document title.
        sections: List of section dicts from the DB.

    Returns:
        Rendered Markdown string.
    """
    parts = [_render_doc_header(title)]
    linked = linked_nodes(db_path, {sec["id"]: doc_id for sec in sections})
    parts.extend(_render_section_block(db_path, doc_id, sec, linked=linked[sec["id"]]) for sec in sections)
    return "\n".join(parts)
