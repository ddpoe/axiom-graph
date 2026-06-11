"""Tests for MCP tool enhancements batch: reorder, tags, doc meta, batch modes."""

from __future__ import annotations

import json
from pathlib import Path


from axiom_graph.index import db
from axiom_graph.models import AxiomNode
from axiom_graph.docjson import parse as json_doc_scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_doc(project_root: Path, slug: str, title: str, sections: list[dict], tags: list[str] | None = None) -> Path:
    """Write a DocJSON file, scan it, and index it."""
    docs_dir = project_root / "docs"
    docs_dir.mkdir(exist_ok=True)
    doc_path = docs_dir / f"{slug}.json"
    data: dict = {"title": title, "sections": sections}
    if tags is not None:
        data["tags"] = tags
    doc_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Scan and index
    db_path = project_root / ".axiom_graph" / "graph.db"
    project_id = project_root.name
    nodes, edges, doc_recs, sec_recs = json_doc_scanner.scan_single_json_doc(doc_path, project_root, project_id)
    for node in nodes:
        db.upsert_node(db_path, node)
    for edge in edges:
        db.upsert_edge(db_path, edge)
    with db._connect(db_path) as conn:
        for rec in doc_recs:
            db.upsert_doc(conn, rec)
        for rec in sec_recs:
            db.upsert_doc_section(conn, rec)
    return doc_path


def _upsert_code_node(db_path: Path, node_id: str, **kwargs) -> None:
    """Insert a minimal code node for batch-mode testing."""
    defaults = dict(
        id=node_id,
        node_type="atomic_process",
        title=node_id.split("::")[-1],
        location="mod.py",
        source="ast",
        code_hash="hash_aaa",
        level_0=node_id.split("::")[-1],
        level_1=node_id.split("::")[-1],
    )
    defaults.update(kwargs)
    db.upsert_node(db_path, AxiomNode(**defaults), discovery_only=False)


# ---------------------------------------------------------------------------
# WS1: update_section — reorder (after param)
# ---------------------------------------------------------------------------


class TestUpdateSectionReorder:
    """Reorder sections via axiom_graph_update_section with ``after`` param."""

    def test_reorder_top_level(self, mini_project: Path) -> None:
        """Move a top-level section after a sibling."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
                {"id": "setup", "heading": "Setup", "content": "B"},
                {"id": "usage", "heading": "Usage", "content": "C"},
            ],
        )

        sec_id = f"{pid}::docs.guide::intro"
        result = axiom_graph_update_section(str(mini_project), sec_id, after="setup")
        assert "after" in result.lower() or "reorder" in result.lower() or "Updated" in result

        # Verify JSON on disk
        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        ids = [s["id"] for s in data["sections"]]
        assert ids == ["setup", "intro", "usage"]

    def test_reorder_nested(self, mini_project: Path) -> None:
        """Move a nested section after a sibling within the same parent."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "arch",
            "Architecture",
            [
                {
                    "id": "database",
                    "heading": "Database",
                    "content": "DB overview",
                    "sections": [
                        {"id": "tables", "heading": "Tables", "content": "T"},
                        {"id": "indexes", "heading": "Indexes", "content": "I"},
                        {"id": "migrations", "heading": "Migrations", "content": "M"},
                    ],
                },
            ],
        )

        sec_id = f"{pid}::docs.arch::database.tables"
        result = axiom_graph_update_section(str(mini_project), sec_id, after="migrations")
        assert "Updated" in result

        data = json.loads((mini_project / "docs" / "arch.json").read_text())
        child_ids = [s["id"] for s in data["sections"][0]["sections"]]
        assert child_ids == ["indexes", "migrations", "tables"]

    def test_reorder_after_self_is_noop(self, mini_project: Path) -> None:
        """Reordering a section after itself is a no-op."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
                {"id": "setup", "heading": "Setup", "content": "B"},
            ],
        )

        sec_id = f"{pid}::docs.guide::intro"
        result = axiom_graph_update_section(str(mini_project), sec_id, after="intro")
        # Should not error — either a no-op message or Updated
        assert "ERROR" not in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        ids = [s["id"] for s in data["sections"]]
        # Order should be unchanged since "intro" is already before itself conceptually
        # Actually after=self means put intro after intro, which is the same position
        assert ids[0] == "intro"

    def test_reorder_cross_parent_errors(self, mini_project: Path) -> None:
        """Referencing a sibling under a different parent is an error."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "arch",
            "Architecture",
            [
                {
                    "id": "database",
                    "heading": "Database",
                    "sections": [
                        {"id": "tables", "heading": "Tables", "content": "T"},
                    ],
                },
                {"id": "api", "heading": "API", "content": "Endpoints"},
            ],
        )

        # Try to reorder database.tables after "api" which is a top-level sibling, not a child of database
        sec_id = f"{pid}::docs.arch::database.tables"
        result = axiom_graph_update_section(str(mini_project), sec_id, after="api")
        assert "ERROR" in result

    def test_reorder_combined_with_content(self, mini_project: Path) -> None:
        """Reorder + content update in the same call."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
                {"id": "setup", "heading": "Setup", "content": "B"},
                {"id": "usage", "heading": "Usage", "content": "C"},
            ],
        )

        sec_id = f"{pid}::docs.guide::intro"
        result = axiom_graph_update_section(str(mini_project), sec_id, after="usage", content="Updated intro")
        assert "Updated" in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        ids = [s["id"] for s in data["sections"]]
        assert ids == ["setup", "usage", "intro"]
        # Verify content was also updated
        intro_sec = next(s for s in data["sections"] if s["id"] == "intro")
        assert intro_sec["content"] == "Updated intro"


# ---------------------------------------------------------------------------
# WS1: update_section — tags param
# ---------------------------------------------------------------------------


class TestUpdateSectionTags:
    """Section-level tags via axiom_graph_update_section."""

    def test_set_tags(self, mini_project: Path) -> None:
        """Setting tags writes them to the section in the JSON file."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
            ],
        )

        sec_id = f"{pid}::docs.guide::intro"
        result = axiom_graph_update_section(str(mini_project), sec_id, tags=["important", "reviewed"])
        assert "Updated" in result
        assert "tags" in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        assert data["sections"][0]["tags"] == ["important", "reviewed"]

    def test_clear_tags(self, mini_project: Path) -> None:
        """Passing an empty list clears tags."""
        from axiom_graph.mcp_server import axiom_graph_update_section

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A", "tags": ["old-tag"]},
            ],
        )

        sec_id = f"{pid}::docs.guide::intro"
        result = axiom_graph_update_section(str(mini_project), sec_id, tags=[])
        assert "Updated" in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        assert data["sections"][0]["tags"] == []


# ---------------------------------------------------------------------------
# WS2: axiom_graph_update_doc_meta
# ---------------------------------------------------------------------------


class TestUpdateDocMeta:
    """Doc-level metadata updates."""

    def test_update_title(self, mini_project: Path) -> None:
        """Updating title patches the JSON and re-indexes."""
        from axiom_graph.mcp_server import axiom_graph_update_doc_meta

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Old Title",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
            ],
        )

        doc_id = f"{pid}::docs.guide"
        result = axiom_graph_update_doc_meta(str(mini_project), doc_id, title="New Title")
        assert "Updated" in result or "title" in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        assert data["title"] == "New Title"

        # Verify DB was re-indexed with new title
        db_path = mini_project / ".axiom_graph" / "graph.db"
        node = db.get_node(db_path, doc_id)
        assert node is not None
        assert node.title == "New Title"

    def test_update_tags(self, mini_project: Path) -> None:
        """Updating doc-level tags patches the JSON."""
        from axiom_graph.mcp_server import axiom_graph_update_doc_meta

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
            ],
        )

        doc_id = f"{pid}::docs.guide"
        result = axiom_graph_update_doc_meta(str(mini_project), doc_id, tags=["architecture", "v2"])
        assert "Updated" in result

        data = json.loads((mini_project / "docs" / "guide.json").read_text())
        assert data["tags"] == ["architecture", "v2"]

    def test_empty_title_errors(self, mini_project: Path) -> None:
        """Setting title to empty string is an error."""
        from axiom_graph.mcp_server import axiom_graph_update_doc_meta

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
            ],
        )

        doc_id = f"{pid}::docs.guide"
        result = axiom_graph_update_doc_meta(str(mini_project), doc_id, title="")
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# WS3: Batch axiom_graph_source
# ---------------------------------------------------------------------------


class TestBatchSource:
    """Batch mode for axiom_graph_source."""

    def test_batch_multiple_nodes(self, mini_project: Path) -> None:
        """node_ids returns combined results with delimiters."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        # Write a real source file so axiom_graph_source can read it
        mod_file = mini_project / "mod.py"
        mod_file.write_text("def func_a():\n    pass\n\ndef func_b():\n    pass\n")

        _upsert_code_node(db_path, "test::mod::func_a", level_3_location="mod.py#L1-L2")
        _upsert_code_node(db_path, "test::mod::func_b", level_3_location="mod.py#L4-L5")

        result = axiom_graph_source(
            str(mini_project),
            node_id="ignored",
            node_ids=["test::mod::func_a", "test::mod::func_b"],
        )
        assert "func_a" in result
        assert "func_b" in result

    def test_batch_invalid_id_in_list(self, mini_project: Path) -> None:
        """Invalid IDs produce per-ID errors without aborting."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        mod_file = mini_project / "mod.py"
        mod_file.write_text("def func_a():\n    pass\n")
        _upsert_code_node(db_path, "test::mod::func_a", level_3_location="mod.py#L1-L2")

        result = axiom_graph_source(
            str(mini_project),
            node_id="ignored",
            node_ids=["test::mod::func_a", "test::mod::nonexistent"],
        )
        assert "func_a" in result
        assert "ERROR" in result  # error for nonexistent
        assert "nonexistent" in result

    def test_batch_empty_list_errors(self, mini_project: Path) -> None:
        """Empty node_ids list returns an error."""
        from axiom_graph.mcp_server import axiom_graph_source

        result = axiom_graph_source(str(mini_project), node_id="ignored", node_ids=[])
        assert "ERROR" in result

    def test_single_node_id_backward_compat(self, mini_project: Path) -> None:
        """Existing single node_id param works as before."""
        from axiom_graph.mcp_server import axiom_graph_source

        db_path = mini_project / ".axiom_graph" / "graph.db"
        mod_file = mini_project / "mod.py"
        mod_file.write_text("def func_a():\n    pass\n")
        _upsert_code_node(db_path, "test::mod::func_a", level_3_location="mod.py#L1-L2")

        result = axiom_graph_source(str(mini_project), node_id="test::mod::func_a")
        assert "func_a" in result
        assert "ERROR" not in result


# ---------------------------------------------------------------------------
# WS3: Batch axiom_graph_graph
# ---------------------------------------------------------------------------


class TestBatchGraph:
    """Batch mode for axiom_graph_graph."""

    def test_batch_multiple_nodes(self, mini_project: Path) -> None:
        """node_ids returns combined graph results."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_code_node(db_path, "test::mod::func_a")
        _upsert_code_node(db_path, "test::mod::func_b")

        result = axiom_graph_graph(
            str(mini_project),
            node_id="ignored",
            node_ids=["test::mod::func_a", "test::mod::func_b"],
        )
        assert "func_a" in result
        assert "func_b" in result

    def test_batch_invalid_id(self, mini_project: Path) -> None:
        """Invalid ID in batch produces per-ID error."""
        from axiom_graph.mcp_server import axiom_graph_graph

        db_path = mini_project / ".axiom_graph" / "graph.db"
        _upsert_code_node(db_path, "test::mod::func_a")

        result = axiom_graph_graph(
            str(mini_project),
            node_id="ignored",
            node_ids=["test::mod::func_a", "test::mod::ghost"],
        )
        assert "func_a" in result
        assert "ERROR" in result
        assert "ghost" in result


# ---------------------------------------------------------------------------
# WS3: Batch axiom_graph_read_doc
# ---------------------------------------------------------------------------


class TestBatchReadDoc:
    """Batch mode for axiom_graph_read_doc."""

    def test_batch_multiple_docs(self, mini_project: Path) -> None:
        """doc_ids returns combined doc content."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "Guide intro"},
            ],
        )
        _write_doc(
            mini_project,
            "reference",
            "Reference",
            [
                {"id": "api", "heading": "API", "content": "API docs"},
            ],
        )

        result = axiom_graph_read_doc(
            str(mini_project),
            doc_id="ignored",
            doc_ids=[f"{pid}::docs.guide", f"{pid}::docs.reference"],
        )
        assert "Guide" in result
        assert "Reference" in result

    def test_batch_invalid_doc_id(self, mini_project: Path) -> None:
        """Invalid doc ID in batch produces per-ID error."""
        from axiom_graph.mcp_server import axiom_graph_read_doc

        pid = mini_project.name
        _write_doc(
            mini_project,
            "guide",
            "Guide",
            [
                {"id": "intro", "heading": "Intro", "content": "A"},
            ],
        )

        result = axiom_graph_read_doc(
            str(mini_project),
            doc_id="ignored",
            doc_ids=[f"{pid}::docs.guide", f"{pid}::docs.nonexistent"],
        )
        assert "Guide" in result
        assert "ERROR" in result
        assert "nonexistent" in result
