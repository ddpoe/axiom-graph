"""Cortex doc scanner — Markdown files → AxiomNode + AxiomEdge objects.

Entry point:
    scan_docs(docs_dir, project_root, project_id)
        -> tuple[list[AxiomNode], list[AxiomEdge]]

Uses markdown-it-py for tokenisation. No external runtime dependencies beyond
pyyaml and markdown-it-py (already declared in pyproject.toml).
"""

from __future__ import annotations

import re
from pathlib import Path

from markdown_it import MarkdownIt

from axiom_annotations import task

from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.models import AxiomEdge, AxiomNode, hash16, make_edge


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@task(
    purpose="Walk docs_dir for .md/.markdown files, apply mtime fast-pass, create composite_process nodes with whole-file hashing and atomic_process section nodes",
    inputs="docs_dir, project_root, project_id, optional stored_mtimes",
    outputs="Tuple of (nodes, edges, files_skipped)",
)
def scan_docs(
    docs_dir: Path,
    project_root: Path,
    project_id: str,
    stored_mtimes: dict[str, float] | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge], int]:
    """Walk docs_dir for .md files and return (nodes, edges, files_skipped).

    For each file: one document node for the file and one child document node
    per H2 section.

    Parameters
    ----------
    stored_mtimes:
        ``{rel_path: mtime}`` map from the DB.  Files whose current mtime
        is <= the stored value are skipped entirely.
    """
    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    files_skipped = 0

    if not docs_dir.exists():
        return nodes, edges, 0

    for md_file in sorted(docs_dir.rglob("*.md")):
        # mtime fast-pass
        if stored_mtimes:
            rel = md_file.relative_to(project_root).as_posix()
            stored = stored_mtimes.get(rel)
            if file_unchanged_since(stored, md_file.stat().st_mtime):
                files_skipped += 1
                continue
        try:
            _scan_file(md_file, project_root, project_id, nodes, edges)
        except Exception:
            # Don't let a single bad file crash the build
            pass

    return nodes, edges, files_skipped


# ---------------------------------------------------------------------------
# Per-file scanner
# ---------------------------------------------------------------------------

_md = MarkdownIt()


def _scan_file(
    md_file: Path,
    project_root: Path,
    project_id: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
) -> None:
    text = md_file.read_text(encoding="utf-8", errors="replace")
    rel_path = md_file.relative_to(project_root).as_posix()
    stem = md_file.stem  # e.g. "architecture"
    file_hash = hash16(text)
    file_mtime = md_file.stat().st_mtime

    # Tokenise
    tokens = _md.parse(text)

    # Extract structure: H1, H2, inline text, paragraphs
    sections = _extract_sections(tokens, text)

    # Collect locally so dedup operates only within this file
    local_nodes: list[AxiomNode] = []
    local_edges: list[AxiomEdge] = []

    # ---------------------------------------------------------------------------
    # File-level document node
    # ---------------------------------------------------------------------------
    file_node_id = f"{project_id}::docs.{stem}"
    h1_title = sections["h1"] or stem
    first_para = sections["first_para"] or ""

    file_node = AxiomNode(
        id=file_node_id,
        node_type="composite_process",
        subtype="docjson",
        title=h1_title,
        location=rel_path,
        source="doc_scanner",
        code_hash=file_hash,
        level_0=h1_title,
        level_1=_first_sentence(first_para) if first_para else h1_title,
        level_2=text[:4000] if text else None,
        level_3_location=rel_path,
        desc_hash=file_hash,
        file_mtime=file_mtime,
    )
    local_nodes.append(file_node)

    # ---------------------------------------------------------------------------
    # H2 section nodes + decision detection
    # ---------------------------------------------------------------------------
    for section in sections["h2_sections"]:
        heading = section["heading"]
        body = section["body"]
        slug = _slugify(heading)
        section_id = f"{file_node_id}#{slug}"
        section_body_text = body.strip()

        section_node = AxiomNode(
            id=section_id,
            node_type="atomic_process",
            subtype="docjson",
            title=heading,
            location=rel_path,
            source="doc_scanner",
            code_hash=hash16(section_body_text),
            level_0=heading,
            level_1=_first_sentence(section_body_text) or heading,
            level_2=section_body_text[:4000] if section_body_text else None,
            level_3_location=f"{rel_path}#{slug}",
            desc_hash=hash16(heading),
            file_mtime=file_mtime,
        )
        local_nodes.append(section_node)

        # composes edge: file → section (structural containment, ontologically valid)
        local_edges.append(make_edge("composes", file_node_id, section_id))

    # Deduplicate within this file (a decision can appear on multiple lines)
    seen_ids: set[str] = set()
    for n in local_nodes:
        if n.id not in seen_ids:
            seen_ids.add(n.id)
            nodes.append(n)

    seen_edge_ids: set[str] = set()
    for e in local_edges:
        if e.id not in seen_edge_ids:
            seen_edge_ids.add(e.id)
            edges.append(e)


# ---------------------------------------------------------------------------
# Token-based section extractor
# ---------------------------------------------------------------------------


def _extract_sections(tokens: list, full_text: str) -> dict:
    """Extract H1, first paragraph, and list of H2 sections from token stream."""
    result: dict = {
        "h1": None,
        "first_para": None,
        "h2_sections": [],
    }

    lines = full_text.splitlines()
    first_para_found = False

    # Walk tokens linearly
    i = 0
    while i < len(tokens):
        tok = tokens[i]

        # H1 heading
        if tok.type == "heading_open" and tok.tag == "h1":
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                result["h1"] = tokens[i + 1].content.strip()

        # First paragraph (level_1 of file node)
        elif tok.type == "paragraph_open" and not first_para_found:
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                content = tokens[i + 1].content.strip()
                # Skip very short lines or blockquote-style lines
                if len(content) > 20 and not content.startswith(">"):
                    result["first_para"] = content
                    first_para_found = True

        i += 1

    # Extract H2 sections by splitting on ## headings in raw text
    result["h2_sections"] = _split_h2_sections(full_text)
    return result


def _split_h2_sections(text: str) -> list[dict]:
    """Split markdown text on ## headings, return list of {heading, body}."""
    sections = []
    # Use regex to find ## headings (not inside code blocks)
    # Remove fenced code blocks first to avoid false matches inside them
    clean = re.sub(r"```.*?```", lambda m: "\n" * m.group().count("\n"), text, flags=re.DOTALL)
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(clean))

    for idx, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append({"heading": heading, "body": body})

    return sections


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert a heading string to a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _first_sentence(text: str) -> str:
    """Return first sentence of text (up to first '.', '!', '?', or newline)."""
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        # Skip markdown formatting lines
        if not line or line.startswith("#") or line.startswith("|") or line.startswith("```"):
            continue
        # Strip bold/italic markers for cleaner output
        line = re.sub(r"\*\*?|__?", "", line)
        m = re.search(r"[.!?]", line)
        if m:
            return line[: m.start() + 1]
        return line
    return text.strip()[:120]
