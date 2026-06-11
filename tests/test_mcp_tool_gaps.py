"""Tests for PEV cycle: MCP tool gaps (delete ops, tag discovery, purge).

Covers:
- axiom_graph_delete_section (flat + nested children)
- axiom_graph_delete_doc (file + DB cleanup)
- axiom_graph_delete_link (valid + missing edge)
- axiom_graph_list_tags (returns correct counts)
- axiom_graph_search with tag filter
- axiom_graph_build with purge parameter
"""

from __future__ import annotations

import json
from pathlib import Path


from axiom_graph.index import builder, db
from axiom_graph.mcp_server import (
    axiom_graph_delete_doc,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_list_tags,
    axiom_graph_search,
)
from axiom_graph.docjson import parse as json_doc_scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_doc(docs_dir: Path, filename: str, title: str, sections: list[dict]) -> Path:
    """Write a DocJSON file and return its path."""
    docs_dir.mkdir(exist_ok=True)
    doc_path = docs_dir / filename
    doc_path.write_text(
        json.dumps({"title": title, "sections": sections}, indent=2),
        encoding="utf-8",
    )
    return doc_path


def _build_full(project_root: Path, project_id: str = "proj") -> dict:
    return builder.build(project_root, project_id=project_id, discovery_only=False)


def _index_doc(db_path: Path, json_file: Path, root: Path, project_id: str = "proj"):
    """Index a single doc file into the DB."""
    nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(json_file, root, project_id)
    for node in nodes:
        db.upsert_node(db_path, node)
    for edge in edges:
        db.upsert_edge(db_path, edge)
    with db._connect(db_path) as conn:
        for rec in doc_recs:
            db.upsert_doc(conn, rec)
        for rec in sec_recs:
            db.upsert_doc_section(conn, rec)


NESTED_DOC_SECTIONS = [
    {
        "id": "database-layer",
        "heading": "Database Layer",
        "content": "Overview of the DB.",
        "sections": [
            {"id": "tables", "heading": "Tables", "content": "Table details."},
            {"id": "migrations", "heading": "Migrations", "content": "Migration info."},
        ],
    },
    {"id": "api-layer", "heading": "API Layer", "content": "REST endpoints."},
]


# ---------------------------------------------------------------------------
# Test: axiom_graph_delete_section (US-1) — flat section
# ---------------------------------------------------------------------------


def test_delete_section_flat(mini_project: Path, db_path: Path):
    """Deleting a flat (top-level, no children) section removes it from JSON and DB."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC_SECTIONS)
    _build_full(mini_project)

    # Verify section exists before delete
    node = db.get_node(db_path, "proj::docs.arch::api-layer")
    assert node is not None

    result = axiom_graph_delete_section(str(mini_project), "proj::docs.arch::api-layer")
    assert "Deleted" in result
    assert "api-layer" in result

    # Section node should be gone from DB
    node = db.get_node(db_path, "proj::docs.arch::api-layer")
    assert node is None

    # Section should be gone from JSON file
    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    section_ids = [s["id"] for s in data["sections"]]
    assert "api-layer" not in section_ids
    # Other sections should remain
    assert "database-layer" in section_ids


# ---------------------------------------------------------------------------
# Test: axiom_graph_delete_section (US-1) — nested with children
# ---------------------------------------------------------------------------


def test_delete_section_nested_with_children(mini_project: Path, db_path: Path):
    """Deleting a parent section recursively removes it and all children."""
    docs_dir = mini_project / "docs"
    _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC_SECTIONS)
    _build_full(mini_project)

    # Verify children exist
    assert db.get_node(db_path, "proj::docs.arch::database-layer") is not None
    assert db.get_node(db_path, "proj::docs.arch::database-layer.tables") is not None
    assert db.get_node(db_path, "proj::docs.arch::database-layer.migrations") is not None

    result = axiom_graph_delete_section(str(mini_project), "proj::docs.arch::database-layer")
    assert "Deleted" in result

    # Parent and children should all be gone
    assert db.get_node(db_path, "proj::docs.arch::database-layer") is None
    assert db.get_node(db_path, "proj::docs.arch::database-layer.tables") is None
    assert db.get_node(db_path, "proj::docs.arch::database-layer.migrations") is None

    # JSON should not have the section
    data = json.loads((docs_dir / "arch.json").read_text(encoding="utf-8"))
    section_ids = [s["id"] for s in data["sections"]]
    assert "database-layer" not in section_ids
    # api-layer should remain
    assert "api-layer" in section_ids


# ---------------------------------------------------------------------------
# Test: axiom_graph_delete_doc (US-2)
# ---------------------------------------------------------------------------


def test_delete_doc(mini_project: Path, db_path: Path):
    """Deleting a doc removes the JSON file and all DB artifacts."""
    docs_dir = mini_project / "docs"
    json_file = _write_doc(docs_dir, "arch.json", "Architecture", NESTED_DOC_SECTIONS)
    _build_full(mini_project)

    assert json_file.exists()
    assert db.get_node(db_path, "proj::docs.arch") is not None

    result = axiom_graph_delete_doc(str(mini_project), "proj::docs.arch")
    assert "Deleted" in result

    # File should be gone
    assert not json_file.exists()

    # DB should be clean
    assert db.get_node(db_path, "proj::docs.arch") is None
    assert db.get_node(db_path, "proj::docs.arch::database-layer") is None


# ---------------------------------------------------------------------------
# Test: axiom_graph_delete_link (US-2) — valid and missing
# ---------------------------------------------------------------------------


def test_delete_link_valid(mini_project: Path, db_path: Path):
    """Deleting an existing link removes the documents edge."""
    docs_dir = mini_project / "docs"
    # Write a doc with a link
    sections = [
        {
            "id": "overview",
            "heading": "Overview",
            "content": "Some content.",
            "links": [{"node_id": "proj::some.module"}],
        }
    ]
    _write_doc(docs_dir, "guide.json", "Guide", sections)
    _build_full(mini_project)

    result = axiom_graph_delete_link(str(mini_project), "proj::docs.guide::overview", "proj::some.module")
    assert "Removed" in result

    # Link should be gone from JSON
    data = json.loads((docs_dir / "guide.json").read_text(encoding="utf-8"))
    links = data["sections"][0].get("links", [])
    assert len(links) == 0


def test_delete_link_not_found(mini_project: Path, db_path: Path):
    """Deleting a non-existent link returns a not-found message."""
    docs_dir = mini_project / "docs"
    sections = [{"id": "overview", "heading": "Overview", "content": "Some content."}]
    _write_doc(docs_dir, "guide.json", "Guide", sections)
    _build_full(mini_project)

    result = axiom_graph_delete_link(str(mini_project), "proj::docs.guide::overview", "proj::nonexistent")
    assert "no matching links" in result.lower()


# ---------------------------------------------------------------------------
# Test: axiom_graph_list_tags (US-4)
# ---------------------------------------------------------------------------


def test_list_tags(mini_project: Path, db_path: Path):
    """list_tags returns distinct tags with counts."""
    docs_dir = mini_project / "docs"
    # Write a doc with tags on sections
    sections = [
        {"id": "sec-a", "heading": "A", "content": "aaa", "tags": ["tag-one", "tag-two"]},
        {"id": "sec-b", "heading": "B", "content": "bbb", "tags": ["tag-one"]},
    ]
    _write_doc(docs_dir, "tagged.json", "Tagged", sections)
    _build_full(mini_project)

    result = axiom_graph_list_tags(str(mini_project))
    # Should contain tag names and counts
    assert "tag-one" in result
    assert "tag-two" in result
    # Verify actual counts: tag-one on both sections, tag-two on one
    assert "tag-one: 2 node(s)" in result
    assert "tag-two: 1 node(s)" in result


# ---------------------------------------------------------------------------
# Test: axiom_graph_search with tag filter (US-4)
# ---------------------------------------------------------------------------


def test_search_with_tag_filter(mini_project: Path, db_path: Path):
    """Search with tag parameter filters results to tagged nodes only."""
    docs_dir = mini_project / "docs"
    sections = [
        {"id": "sec-a", "heading": "Alpha Feature", "content": "alpha details", "tags": ["feature"]},
        {"id": "sec-b", "heading": "Beta Feature", "content": "beta details", "tags": ["deprecated"]},
    ]
    _write_doc(docs_dir, "features.json", "Features", sections)
    _build_full(mini_project)

    # Search for "Feature" with tag filter
    result = axiom_graph_search(str(mini_project), "Feature", tag="feature")
    assert "Alpha" in result or "sec-a" in result
    # Beta should be filtered out (it has tag "deprecated", not "feature")
    assert "sec-b" not in result

    # Empty result case: tag that no node has
    result_empty = axiom_graph_search(str(mini_project), "Feature", tag="nonexistent-tag")
    assert "0 of 0" in result_empty


# ---------------------------------------------------------------------------
# Test: axiom_graph_build purge (US-3)
# ---------------------------------------------------------------------------


def test_build_no_purge_param():
    """axiom_graph_build no longer accepts a purge parameter (moved to CLI)."""
    import inspect
    from axiom_graph.mcp_server import axiom_graph_build as mcp_build

    sig = inspect.signature(mcp_build)
    assert "purge" not in sig.parameters


# ---------------------------------------------------------------------------
# Test: _cleanup_old_section_rows leaves no orphans (Issue #4 regression)
# ---------------------------------------------------------------------------


def test_cleanup_old_section_rows_no_orphans(mini_project: Path, db_path: Path):
    """Deleting a section cleans functional index tables but preserves node_history audit trail."""
    docs_dir = mini_project / "docs"
    sections = [
        {"id": "sec-keep", "heading": "Keep", "content": "stays", "tags": ["keep-tag"]},
        {"id": "sec-remove", "heading": "Remove", "content": "goes away", "tags": ["remove-tag"]},
    ]
    _write_doc(docs_dir, "cleanup.json", "Cleanup", sections)
    _build_full(mini_project)

    sec_id = "proj::docs.cleanup::sec-remove"

    # Verify section node exists and has rows in auxiliary tables
    assert db.get_node(db_path, sec_id) is not None
    with db._connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tags WHERE node_id = ?", (sec_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM node_fts WHERE id = ?", (sec_id,)).fetchone()[0] > 0
        history_before = conn.execute("SELECT COUNT(*) FROM node_history WHERE node_id = ?", (sec_id,)).fetchone()[0]

    # Delete the section via the MCP tool
    result = axiom_graph_delete_section(str(mini_project), sec_id)
    assert "Deleted" in result

    # Node should be gone
    assert db.get_node(db_path, sec_id) is None

    # Functional index tables should be cleaned (no orphaned rows)
    with db._connect(db_path) as conn:
        orphan_tags = conn.execute("SELECT COUNT(*) FROM tags WHERE node_id = ?", (sec_id,)).fetchone()[0]
        orphan_fts = conn.execute("SELECT COUNT(*) FROM node_fts WHERE id = ?", (sec_id,)).fetchone()[0]
        orphan_verification = conn.execute(
            "SELECT COUNT(*) FROM node_verification WHERE node_id = ?", (sec_id,)
        ).fetchone()[0]

        # node_history is an audit trail and must NOT be deleted during cleanup
        history_after = conn.execute("SELECT COUNT(*) FROM node_history WHERE node_id = ?", (sec_id,)).fetchone()[0]

    assert orphan_tags == 0, f"Orphaned tags rows: {orphan_tags}"
    assert orphan_fts == 0, f"Orphaned node_fts rows: {orphan_fts}"
    assert orphan_verification == 0, f"Orphaned node_verification rows: {orphan_verification}"
    assert history_after >= history_before, (
        f"node_history rows should be preserved as audit trail, but went from {history_before} to {history_after}"
    )
