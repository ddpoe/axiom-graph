"""Tests for rename-aware edge migration (ADR-013 Layer 3).

Tier 1 -- plain pytest:
    Edge migration in record_code_rename and record_doc_rename.

Tier 2 -- @workflow(purpose=...):
    DocJSON file patching, no broken links after rename.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.index import db
from axiom_graph.index.staleness import find_broken_links
from axiom_graph.models import AxiomEdge, AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _upsert_code_node(db_path: Path, node_id: str, code_hash: str = "hash_abc", location: str = "mod.py") -> None:
    """Insert a minimal code node."""
    parts = node_id.split("::")
    name = parts[-1]
    node = AxiomNode(
        id=node_id,
        node_type="atomic_process",
        title=name,
        location=location,
        source="ast",
        code_hash=code_hash,
        level_0=name,
        level_1=name,
    )
    db.upsert_node(db_path, node, discovery_only=False)


def _upsert_doc_node(db_path: Path, node_id: str, location: str = "docs/test.json") -> None:
    """Insert a minimal doc section node."""
    parts = node_id.split("::")
    name = parts[-1]
    node = AxiomNode(
        id=node_id,
        node_type="atomic_process",
        subtype="docjson",
        title=name,
        location=location,
        source="json_doc_scanner",
        code_hash="hash_doc",
        level_0=name,
        level_1=name,
    )
    db.upsert_node(db_path, node, discovery_only=False)


def _insert_edge(db_path: Path, edge_type: str, from_id: str, to_id: str) -> None:
    """Insert an edge."""
    edge = AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
    )
    db.upsert_edge(db_path, edge)


def _get_edges(db_path: Path, edge_type: str | None = None) -> list[dict]:
    """Get all edges, optionally filtered by type."""
    with db._connect(db_path) as conn:
        if edge_type:
            rows = conn.execute("SELECT * FROM edges WHERE edge_type = ?", (edge_type,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM edges").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tier 1 -- Edge migration in record_code_rename
# ---------------------------------------------------------------------------


def test_record_code_rename_migrates_to_id_edges(mini_project, db_path):
    """record_code_rename should update edges where old_id is the to_id (target)."""
    _upsert_code_node(db_path, "proj::mod::func_old", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::func_new", code_hash="hash_abc")
    _upsert_doc_node(db_path, "proj::docs.test::s1")

    # Doc section documents the old function
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_old")

    db.record_code_rename(db_path, "proj::mod::func_old", "proj::mod::func_new", "mod.py")

    edges = _get_edges(db_path, "documents")
    assert len(edges) == 1
    assert edges[0]["to_id"] == "proj::mod::func_new"


def test_record_code_rename_migrates_from_id_edges(mini_project, db_path):
    """record_code_rename should update edges where old_id is the from_id (source)."""
    _upsert_code_node(db_path, "proj::mod::func_old", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::func_new", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::other_func")

    # Old function depends on another
    _insert_edge(db_path, "depends_on", "proj::mod::func_old", "proj::mod::other_func")

    db.record_code_rename(db_path, "proj::mod::func_old", "proj::mod::func_new", "mod.py")

    with db._connect(db_path) as conn:
        edges = conn.execute("SELECT * FROM edges WHERE edge_type = 'depends_on'").fetchall()
    assert len(edges) == 1
    assert edges[0]["from_id"] == "proj::mod::func_new"


def test_record_doc_rename_migrates_edges(mini_project, db_path):
    """record_doc_rename should update edges for both parent and section nodes."""
    _upsert_doc_node(db_path, "proj::docs.old::s1", location="docs/old.json")
    _upsert_code_node(db_path, "proj::mod::func_a")

    # Section documents a code node
    _insert_edge(db_path, "documents", "proj::docs.old::s1", "proj::mod::func_a")
    # Composes edge: parent -> section
    _insert_edge(db_path, "composes", "proj::docs.old", "proj::docs.old::s1")

    db.record_doc_rename(db_path, "proj::docs.old", "proj::docs.new", "docs/new.json")

    edges = _get_edges(db_path)
    docs_edges = [e for e in edges if e["edge_type"] == "documents"]
    composes_edges = [e for e in edges if e["edge_type"] == "composes"]

    # documents edge: from_id should be updated to new section ID
    assert len(docs_edges) == 1
    assert docs_edges[0]["from_id"] == "proj::docs.new::s1"

    # composes edge: both from_id and to_id should be updated
    assert len(composes_edges) == 1
    assert composes_edges[0]["from_id"] == "proj::docs.new"
    assert composes_edges[0]["to_id"] == "proj::docs.new::s1"


# ---------------------------------------------------------------------------
# Tier 2 -- DocJSON file patching
# ---------------------------------------------------------------------------


@workflow(
    purpose="Verify that record_code_rename patches DocJSON files on disk to update link node_ids",
)
def test_code_rename_patches_docjson_links(mini_project, db_path):
    """When a code node is renamed, DocJSON files referencing the old ID should be patched."""
    # Create DocJSON file with a link to the old function
    docs_dir = mini_project / "docs"
    docs_dir.mkdir(exist_ok=True)
    doc_content = {
        "title": "Test Doc",
        "sections": [
            {
                "id": "s1",
                "heading": "Section 1",
                "content": "Describes func_old.",
                "links": [{"node_id": "proj::mod::func_old", "type": "documents"}],
            }
        ],
    }
    (docs_dir / "test.json").write_text(json.dumps(doc_content, indent=2))

    _upsert_code_node(db_path, "proj::mod::func_old", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::func_new", code_hash="hash_abc")
    _upsert_doc_node(db_path, "proj::docs.test::s1")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_old")

    db.record_code_rename(
        db_path,
        "proj::mod::func_old",
        "proj::mod::func_new",
        "mod.py",
        project_root=mini_project,
    )

    # Read the patched file
    patched = json.loads((docs_dir / "test.json").read_text())
    link_ids = [link["node_id"] for sec in patched["sections"] for link in (sec.get("links") or [])]
    assert "proj::mod::func_new" in link_ids
    assert "proj::mod::func_old" not in link_ids


@workflow(
    purpose="Verify that no broken links remain after a code rename with edge migration",
)
def test_no_broken_links_after_rename(mini_project, db_path):
    """After rename with edge migration, find_broken_links should return empty."""
    _upsert_code_node(db_path, "proj::mod::func_old", code_hash="hash_abc")
    _upsert_code_node(db_path, "proj::mod::func_new", code_hash="hash_abc")
    _upsert_doc_node(db_path, "proj::docs.test::s1")
    _insert_edge(db_path, "documents", "proj::docs.test::s1", "proj::mod::func_old")

    # Before rename: broken link exists (old node will be purged)
    # After rename: edge should point to new node
    db.record_code_rename(db_path, "proj::mod::func_old", "proj::mod::func_new", "mod.py")

    broken = find_broken_links(db_path)
    # func_new exists, so no broken link
    assert "proj::docs.test::s1" not in broken
