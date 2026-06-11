"""Tests for doc-node subtype='docjson' / subtype='docjson' distinction.

Tier 1: plain pytest — internal scanner behaviour. These tests exercise
json_doc_scanner and doc_scanner directly without going through the full build
pipeline.
"""

from __future__ import annotations

import json

from axiom_graph.scanners import doc_scanner
from axiom_graph.docjson import parse as json_doc_scanner


# ---------------------------------------------------------------------------
# JSON doc scanner — subtype tests
# ---------------------------------------------------------------------------


def test_json_doc_file_node_has_subtype_file(tmp_path):
    """JSON scanner sets subtype='docjson' on the file-level document node."""
    doc = {
        "title": "My Doc",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Some content."}],
    }
    f = tmp_path / "docs" / "mydoc.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    file_node = next(n for n in nodes if n.id == "proj::docs.mydoc")
    assert file_node.subtype == "docjson"


def test_json_doc_section_node_has_subtype_section(tmp_path):
    """JSON scanner sets subtype='docjson' on each section node."""
    doc = {
        "title": "My Doc",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Some content."}],
    }
    f = tmp_path / "docs" / "mydoc.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    section_node = next(n for n in nodes if n.id == "proj::docs.mydoc::intro")
    assert section_node.subtype == "docjson"


def test_json_doc_section_desc_hash_changes_with_content(tmp_path):
    """Two sections with different content produce different desc_hash values."""
    doc = {
        "title": "My Doc",
        "sections": [
            {"id": "sec1", "heading": "Section 1", "content": "First content."},
            {"id": "sec2", "heading": "Section 2", "content": "Completely different content."},
        ],
    }
    f = tmp_path / "docs" / "mydoc.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    sec1 = next(n for n in nodes if n.id == "proj::docs.mydoc::sec1")
    sec2 = next(n for n in nodes if n.id == "proj::docs.mydoc::sec2")
    assert sec1.desc_hash != sec2.desc_hash


def test_json_doc_no_sections_only_file_node(tmp_path):
    """A JSON doc with an empty sections list produces exactly one node (the file node)."""
    doc = {"title": "Empty Doc", "sections": []}
    f = tmp_path / "docs" / "empty.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))

    nodes, _, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    assert len(nodes) == 1
    assert nodes[0].subtype == "docjson"


# ---------------------------------------------------------------------------
# Markdown doc scanner — subtype tests
# ---------------------------------------------------------------------------


def test_markdown_file_node_has_subtype_file(tmp_path):
    """Markdown scanner sets subtype='docjson' on the file-level document node."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "readme.md").write_text("# My Project\n\nThis is the readme.\n\n## Installation\n\nRun pip install.\n")

    nodes, _, _ = doc_scanner.scan_docs(docs_dir, tmp_path, "proj")

    # File node has no '#' in the ID; section nodes do
    file_node = next(n for n in nodes if "#" not in n.id)
    assert file_node.subtype == "docjson"


def test_markdown_section_node_has_subtype_section(tmp_path):
    """Markdown scanner sets subtype='docjson' on H2 section nodes."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "readme.md").write_text(
        "# My Project\n\nIntro paragraph here.\n\n## Installation\n\nRun pip install.\n"
    )

    nodes, _, _ = doc_scanner.scan_docs(docs_dir, tmp_path, "proj")

    section_nodes = [n for n in nodes if "#" in n.id]
    assert len(section_nodes) == 1
    assert section_nodes[0].subtype == "docjson"


def test_markdown_section_id_uses_hash_separator(tmp_path):
    """Section node IDs use '#' to separate the file ID from the heading slug."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text("# Guide\n\nIntro text.\n\n## Getting Started\n\nDo this first.\n")

    nodes, _, _ = doc_scanner.scan_docs(docs_dir, tmp_path, "proj")

    section_nodes = [n for n in nodes if "#" in n.id]
    assert len(section_nodes) == 1
    assert section_nodes[0].id == "proj::docs.guide#getting-started"


# ---------------------------------------------------------------------------
# composes edges: doc file → section
# ---------------------------------------------------------------------------


def test_json_doc_emits_composes_edges_from_file_to_sections(tmp_path):
    """JSON scanner emits a composes edge from the file node to each section node."""
    doc = {
        "title": "My Doc",
        "sections": [
            {"id": "intro", "heading": "Introduction", "content": "Intro text."},
            {"id": "usage", "heading": "Usage", "content": "How to use."},
        ],
    }
    f = tmp_path / "docs" / "mydoc.json"
    f.parent.mkdir()
    f.write_text(json.dumps(doc))

    _, edges, _, _ = json_doc_scanner.scan_single_json_doc(f, tmp_path, "proj")

    composes_edges = [(e.from_id, e.to_id) for e in edges if e.edge_type == "composes"]
    assert ("proj::docs.mydoc", "proj::docs.mydoc::intro") in composes_edges
    assert ("proj::docs.mydoc", "proj::docs.mydoc::usage") in composes_edges


def test_markdown_doc_emits_composes_edges_from_file_to_sections(tmp_path):
    """Markdown scanner emits a composes edge from the file node to each section node."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.md").write_text(
        "# Guide\n\nIntro.\n\n## Installation\n\nRun pip install.\n\n## Usage\n\nRun the tool.\n"
    )

    _, edges, _ = doc_scanner.scan_docs(docs_dir, tmp_path, "proj")

    composes_edges = [(e.from_id, e.to_id) for e in edges if e.edge_type == "composes"]
    assert ("proj::docs.guide", "proj::docs.guide#installation") in composes_edges
    assert ("proj::docs.guide", "proj::docs.guide#usage") in composes_edges
