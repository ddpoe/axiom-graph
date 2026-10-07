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

from axiom_graph.index import doc_ids
from axiom_graph.index.file_state import file_unchanged_since
from axiom_graph.index.walk import TreeListing, suffix_matcher
from axiom_graph.models import AxiomEdge, AxiomNode, hash16, make_edge


_MD_NAMES = suffix_matcher([".md"])


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
    docs_root_entry: str | None = None,
    parse_filter=None,
    listing: TreeListing | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge], int]:
    """Walk docs_dir for .md files and return (nodes, edges, files_skipped).

    ``parse_filter``, when given, takes each walked file's absolute path and
    decides whether it is parsed (``build``'s discovery walk); it replaces
    the ``stored_mtimes`` fast-pass.

    For each file: one document node for the file and one child document node
    per H2 section.

    Parameters
    ----------
    stored_mtimes:
        ``{rel_path: mtime}`` map from the DB.  Files whose current mtime
        is <= the stored value are skipped entirely.
    docs_root_entry:
        ``docs_dir`` exactly as configured in ``docs_dirs``.  Supplies the
        namespace half of every derived doc ID; resolved from ``docs_dir``
        when omitted.
    listing:
        The operation's shared directory listing; a fresh one when omitted.
    """
    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    files_skipped = 0

    if not docs_dir.exists():
        return nodes, edges, 0

    listing = listing if listing is not None else TreeListing()
    for md_file in sorted(listing.matches(docs_dir, _MD_NAMES)):
        if parse_filter is not None:
            if not parse_filter(md_file):
                files_skipped += 1
                continue
        # mtime fast-pass
        elif stored_mtimes:
            rel = md_file.relative_to(project_root).as_posix()
            stored = stored_mtimes.get(rel)
            if file_unchanged_since(stored, md_file.stat().st_mtime):
                files_skipped += 1
                continue
        try:
            _scan_file(md_file, project_root, project_id, nodes, edges, docs_dir, docs_root_entry)
        except Exception:
            # Don't let a single bad file crash the build
            pass

    return nodes, edges, files_skipped


# ---------------------------------------------------------------------------
# Per-file scanner
# ---------------------------------------------------------------------------

_md = MarkdownIt()


def scan_markdown_file(
    md_file: Path,
    project_root: Path,
    project_id: str,
    docs_dir: Path | None = None,
) -> tuple[list[AxiomNode], list[AxiomEdge]]:
    """Scan one Markdown file into its document node and H2 section nodes.

    The same scan :func:`scan_docs` runs per file, so the ids and hashes match
    what the build indexed.  The staleness hasher calls it to re-derive a
    Markdown file's hashes.

    Args:
        md_file: Absolute path of the ``.md`` file.
        project_root: Project root.
        project_id: Project id prefix for the node ids.
        docs_dir: The configured docs root containing the file; resolved from
            the file's path when omitted.

    Returns:
        ``(nodes, edges)`` for the file.
    """
    nodes: list[AxiomNode] = []
    edges: list[AxiomEdge] = []
    _scan_file(md_file, project_root, project_id, nodes, edges, docs_dir)
    return nodes, edges


def _scan_file(
    md_file: Path,
    project_root: Path,
    project_id: str,
    nodes: list[AxiomNode],
    edges: list[AxiomEdge],
    docs_dir: Path | None = None,
    docs_root_entry: str | None = None,
) -> None:
    # Sample the mtime BEFORE reading the bytes — see scan_module for the
    # rationale (stamping an mtime newer than the bytes we indexed makes the
    # next build skip the file permanently).
    file_mtime = md_file.stat().st_mtime
    text = md_file.read_text(encoding="utf-8", errors="replace")
    rel_path = md_file.relative_to(project_root).as_posix()
    stem = md_file.stem  # e.g. "architecture"
    file_hash = hash16(text)

    # Tokenise
    tokens = _md.parse(text)

    # Extract structure: H1, H2, inline text, paragraphs
    sections = _extract_sections(tokens, text)

    # Collect locally so dedup operates only within this file
    local_nodes: list[AxiomNode] = []
    local_edges: list[AxiomEdge] = []

    # ---------------------------------------------------------------------------
    # File-level document node
    #
    # The identity derives from the file's path within its containing docs
    # root, not from its stem: two ``notes.md`` files in different folders are
    # two documents, and the ``.md`` suffix is retained so a Markdown file
    # cannot converge with a DocJSON file of the same name.
    # ---------------------------------------------------------------------------
    root_entry, rel_to_root = doc_ids.resolve_doc_root(md_file, project_root, docs_dir, docs_root_entry)
    file_node_id = doc_ids.derive_markdown_doc_id(project_id, root_entry, rel_to_root)
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
        section_id = f"{file_node_id}{doc_ids.MARKDOWN_SECTION_SEP}{slug}"
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


def markdown_section_slugs(text: str) -> list[str]:
    """Return the slug of every H2 section in *text*, in document order.

    The section identities a build would create for this file, without
    building anything.  Exported so the doc-ID migration can enumerate a
    Markdown document's section nodes from disk through the same split and
    the same slug rule the scanner uses — a second implementation is how a
    migration comes to move an identity the scanner never created.

    Args:
        text: Full Markdown source.

    Returns:
        Slugs in document order.  May contain duplicates when two headings
        slugify identically; the scanner de-duplicates within a file.
    """
    return [_slugify(section["heading"]) for section in _split_h2_sections(text)]


def _split_h2_sections(text: str) -> list[dict]:
    """Split markdown text on ## headings, return list of {heading, body}."""
    sections = []
    # Use regex to find ## headings (not inside code blocks).  Each fenced
    # code block is blanked to spaces of the same length, newlines kept, so
    # a match offset in the masked copy is the same offset in *text*.
    clean = re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group()), text, flags=re.DOTALL)
    pattern = re.compile(r"^## (.+)$", re.MULTILINE)
    matches = list(pattern.finditer(clean))

    for idx, match in enumerate(matches):
        heading = text[match.start(1) : match.end(1)].strip()
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
