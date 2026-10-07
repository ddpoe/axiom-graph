"""Test that axiom_graph_write_doc uses axiom-graph.toml project_id, not directory name."""

from __future__ import annotations

import json
from pathlib import Path

from axiom_graph.index import db
from axiom_graph.docjson.api import axiom_graph_write_doc


def test_write_doc_uses_toml_project_id(tmp_path: Path) -> None:
    """axiom_graph_write_doc should use axiom-graph.toml project_id, not directory name."""
    # Setup: axiom-graph.toml with a custom project_id
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "custom_proj"\n')
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)

    doc = {
        "title": "Test Doc",
        "id": "test-doc",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Hello"}],
    }

    result = axiom_graph_write_doc(str(tmp_path), json.dumps(doc))

    assert "Wrote" in result
    # The doc node should use the toml project_id, not the tmp dir name
    nodes = db.all_nodes(db_path)
    assert len(nodes) >= 1
    assert all(n.id.startswith("custom_proj::") for n in nodes), (
        f"Expected 'custom_proj::' prefix, got {[n.id for n in nodes]}"
    )


def test_write_doc_falls_back_to_dir_name(tmp_path: Path) -> None:
    """Without axiom-graph.toml, axiom_graph_write_doc should fall back to directory name."""
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)

    doc = {
        "title": "Fallback Doc",
        "id": "fallback-doc",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Hello"}],
    }

    result = axiom_graph_write_doc(str(tmp_path), json.dumps(doc))

    assert "Wrote" in result
    nodes = db.all_nodes(db_path)
    assert len(nodes) >= 1
    assert all(n.id.startswith(f"{tmp_path.name}::") for n in nodes), (
        f"Expected '{tmp_path.name}::' prefix, got {[n.id for n in nodes]}"
    )


def test_write_doc_rejects_node_id_form(tmp_path: Path) -> None:
    """axiom_graph_write_doc should reject node-id-form 'id' with a helpful error.

    Node-id form ('project::docs/path/X') breaks on Windows (NTFS reserves
    '::') and is silently malformed on POSIX. The fix is to point the
    caller at the path-slug shape ('pev/instances/X').
    """
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)

    doc = {
        "title": "Bad Doc",
        "id": "axiom_graph::docs/pev/instances/pev-instance-2026-04-28-foo",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Hello"}],
    }

    result = axiom_graph_write_doc(str(tmp_path), json.dumps(doc))

    assert result.startswith("ERROR:"), f"Expected ERROR, got: {result}"
    assert "node-id" in result.lower(), f"Error should explain node-id vs path-slug; got: {result}"
    assert "path-slug" in result, f"Error should point at path-slug as the right input shape; got: {result}"


def test_write_doc_accepts_path_slug_with_subdirs(tmp_path: Path) -> None:
    """Path-slug form with forward-slash subdirs is the supported shape."""
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)

    doc = {
        "title": "Subdir Doc",
        "id": "pev/instances/my-instance",
        "sections": [{"id": "intro", "heading": "Introduction", "content": "Hi"}],
    }

    result = axiom_graph_write_doc(str(tmp_path), json.dumps(doc))

    assert "Wrote" in result, f"Expected success, got: {result}"
    written = tmp_path / "docs" / "pev" / "instances" / "my-instance.docjson"
    assert written.exists(), f"File not written at expected subdir path: {written}"


def test_write_doc_rejects_filesystem_illegal_chars(tmp_path: Path) -> None:
    """Other NTFS-reserved characters in the id should be rejected with a clear error."""
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)

    doc = {
        "title": "Bad Chars",
        "id": "foo<bar|baz",
        "sections": [{"id": "intro", "heading": "x", "content": "y"}],
    }

    result = axiom_graph_write_doc(str(tmp_path), json.dumps(doc))

    assert result.startswith("ERROR:"), f"Expected ERROR, got: {result}"
    assert "invalid in filenames" in result, f"Error should name the problem; got: {result}"


def test_write_doc_without_toml_uses_the_id_the_index_stores(tmp_path: Path) -> None:
    """With no axiom-graph.toml, a doc written in a folder named differently from the project gets the index's id.

    A later build keeps every node, the new doc included, under that id.
    """
    from axiom_graph.index import builder

    root = tmp_path / "worktree-folder"
    (root / "docs").mkdir(parents=True)
    (root / "mod.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
    builder.build(root, project_id="fresh", discovery_only=False)

    doc = {"title": "Cycle", "id": "pev/cycles/c-1", "sections": [{"id": "intro", "heading": "Intro"}]}
    result = axiom_graph_write_doc(str(root), json.dumps(doc))

    assert "fresh::docs/pev/cycles/c-1" in result, result
    builder.build(root, discovery_only=True)
    ids = {n.id for n in db.all_nodes(root / ".axiom_graph" / "graph.db")}
    assert "fresh::docs/pev/cycles/c-1" in ids
    assert "fresh::mod::fn" in ids
    assert not [i for i in ids if not i.startswith("fresh::")], ids


def test_export_and_viz_without_toml_use_the_id_the_index_stores(tmp_path: Path) -> None:
    """With no axiom-graph.toml, export and the viz server take the index's id, not the folder name."""
    from click.testing import CliRunner

    from axiom_graph.cli import main
    from axiom_graph.index import builder
    from axiom_graph.viz import server

    root = tmp_path / "worktree-folder"
    root.mkdir()
    (root / "mod.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
    builder.build(root, project_id="fresh", discovery_only=False)

    result = CliRunner().invoke(main, ["export", str(root)])
    assert result.exit_code == 0, result.output
    exported = json.loads((root / ".axiom_graph" / "index.json").read_text(encoding="utf-8"))
    assert exported["project_id"] == "fresh"

    with server._switch_lock:
        previous = server._PROJECT_ROOT
        server._apply_project(root)
        try:
            assert server._PROJECT_ID == "fresh"
        finally:
            if previous is not None:
                server._apply_project(previous)
