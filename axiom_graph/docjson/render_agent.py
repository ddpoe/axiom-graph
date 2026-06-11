"""Agent-facing doc rendering.

Phase 4 (ADR-005): extracted from ``axiom_graph/mcp/_helpers.py``.

Renders DocJSON documents as Markdown for consumption by MCP agents / tools.
Unlike the consumer renderer (``docjson.render_consumer``) which produces clean
output for human-facing static sites, this renderer includes section ID
annotations (``<!-- id: ... -->``) and linked-node lists so agents can
programmatically reference sections and follow cross-references.
"""

from __future__ import annotations

from pathlib import Path

from axiom_graph.index import db


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
    lines: list[str] = [f"# {title}", ""]

    for sec in sections:
        heading = sec["heading"]
        content = sec.get("content") or ""
        level = sec.get("level") or 2
        prefix = "#" * max(2, min(level, 6))
        sec_raw_id = sec.get("id", "")
        id_annotation = f"  <!-- id: {sec_raw_id} -->" if sec_raw_id else ""
        lines.append(f"{prefix} {heading}{id_annotation}")
        lines.append("")
        if content:
            lines.append(content)
            lines.append("")

        # Collect linked code nodes (edges: section -> node, excluding -> doc)
        sec_id = sec["id"]
        linked: list[tuple[str, str, str]] = []
        with db._connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT e.to_id, n.level_1, n.level_3_location
                FROM edges e
                LEFT JOIN nodes n ON n.id = e.to_id
                WHERE e.from_id = ?
                  AND e.edge_type = 'documents'
                  AND e.to_id != ?
                ORDER BY e.to_id
                """,
                (sec_id, doc_id),
            ).fetchall()
            linked = [(r["to_id"], r["level_1"] or "", r["level_3_location"] or "") for r in rows]

        if linked:
            lines.append("**Linked nodes:**")
            for nid, summary, loc in linked:
                entry = f"- `{nid}`"
                if summary:
                    entry += f" — {summary}"
                if loc and "#L" in loc:
                    entry += f"  @ {loc}"
                lines.append(entry)
            lines.append("")

    return "\n".join(lines)


def _render_doc_toc(title: str, sections: list[dict]) -> str:
    """Render a table-of-contents for a large doc instead of full content.

    Args:
        title: Document title.
        sections: List of section dicts from the DB.

    Returns:
        Rendered TOC string.
    """
    total_chars = sum(len(s.get("content") or "") for s in sections)
    lines = [
        f"# {title}",
        "",
        f"This document has {len(sections)} sections ({total_chars} chars total). "
        "Use section='slug' to read a specific section.",
        "",
        "| Section | Chars |",
        "|---------|-------|",
    ]
    for sec in sections:
        slug = sec.get("id", "").split("::")[-1]
        chars = len(sec.get("content") or "")
        lines.append(f"| {slug} | {chars} |")
    return "\n".join(lines)
