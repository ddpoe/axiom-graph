"""Tests for WS4: axiom_graph_purge_node, axiom_graph_build purge removal, CLI --purge.

Covers:
- axiom_graph_purge_node happy path (NOT_FOUND code node)
- axiom_graph_purge_node refuses non-NOT_FOUND node
- axiom_graph_purge_node on doc node
- axiom_graph_purge_node on missing node
- axiom_graph_build no longer accepts purge param
- CLI cmd_build accepts --purge flag
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path


from axiom_graph.index import builder, db


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


# ---------------------------------------------------------------------------
# Test: axiom_graph_purge_node happy path — NOT_FOUND code node
# ---------------------------------------------------------------------------


def test_purge_not_found_code_node(mini_project: Path, db_path: Path):
    """Purging a NOT_FOUND code node removes it and records reason in history."""
    from axiom_graph.mcp_server import axiom_graph_purge_node

    # Create and index a Python file
    py_file = mini_project / "example.py"
    py_file.write_text("def hello():\n    '''Say hello.'''\n    pass\n", encoding="utf-8")
    _build_full(mini_project)

    # Find the indexed node
    nodes = db.all_nodes(db_path)
    example_nodes = [n for n in nodes if "example" in n.id]
    assert len(example_nodes) > 0
    node_id = example_nodes[0].id

    # Delete the file so staleness marks it NOT_FOUND
    py_file.unlink()
    from axiom_graph.index.staleness import record_staleness

    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    # Verify own_status is NOT_FOUND
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row["own_status"] == "NOT_FOUND"

    # Purge the node
    result = axiom_graph_purge_node(str(mini_project), node_id=node_id, reason="file was deleted")
    assert "Purged node:" in result
    assert node_id in result
    assert "file was deleted" in result

    # Node should be gone
    assert db.get_node(db_path, node_id) is None

    # History should have a preserved DELETED row with the reason
    with db._connect(db_path) as conn:
        history = conn.execute(
            "SELECT * FROM node_history WHERE node_id = ? AND change_type = 'DELETED' AND preserved = 1",
            (node_id,),
        ).fetchall()
    assert len(history) >= 1
    meta = json.loads(history[-1]["meta"])
    assert meta["actor"] == "agent:pev-auditor"
    assert meta["reason"] == "file was deleted"


# ---------------------------------------------------------------------------
# Test: axiom_graph_purge_node refuses non-NOT_FOUND node
# ---------------------------------------------------------------------------


def test_purge_refuses_verified_node(mini_project: Path, db_path: Path):
    """Purging a VERIFIED node returns an error."""
    from axiom_graph.mcp_server import axiom_graph_purge_node

    py_file = mini_project / "example.py"
    py_file.write_text("def hello():\n    '''Say hello.'''\n    pass\n", encoding="utf-8")
    _build_full(mini_project)

    nodes = db.all_nodes(db_path)
    example_nodes = [n for n in nodes if "example" in n.id]
    assert len(example_nodes) > 0
    node_id = example_nodes[0].id

    result = axiom_graph_purge_node(str(mini_project), node_id=node_id, reason="no reason")
    assert "ERROR" in result
    assert "NOT_FOUND" in result
    assert "VERIFIED" in result

    # Node should still exist
    assert db.get_node(db_path, node_id) is not None


# ---------------------------------------------------------------------------
# Test: axiom_graph_purge_node on doc node
# ---------------------------------------------------------------------------


def test_purge_not_found_doc_node(mini_project: Path, db_path: Path):
    """Purging a NOT_FOUND doc node removes it and its sections."""
    from axiom_graph.mcp_server import axiom_graph_purge_node

    docs_dir = mini_project / "docs"
    _write_doc(
        docs_dir,
        "arch.json",
        "Architecture",
        [
            {"id": "overview", "heading": "Overview", "content": "An overview."},
        ],
    )
    _build_full(mini_project)

    doc_id = "proj::docs.arch"
    assert db.get_node(db_path, doc_id) is not None

    # Delete the doc file so it becomes NOT_FOUND
    (docs_dir / "arch.json").unlink()
    from axiom_graph.index.staleness import record_staleness

    nodes = db.all_nodes(db_path)
    record_staleness(db_path, mini_project, nodes)

    # Verify the doc node is NOT_FOUND
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (doc_id,)).fetchone()
    assert row["own_status"] == "NOT_FOUND"

    result = axiom_graph_purge_node(str(mini_project), node_id=doc_id, reason="doc removed")
    assert "Purged node:" in result

    # Doc node and section should be gone
    assert db.get_node(db_path, doc_id) is None
    assert db.get_node(db_path, "proj::docs.arch::overview") is None

    # Verify DELETED history rows carry the actor and reason from reason_meta
    with db._connect(db_path) as conn:
        hist_rows = conn.execute(
            "SELECT meta FROM node_history WHERE change_type = 'DELETED' AND preserved = 1 AND node_id IN (?, ?)",
            (doc_id, "proj::docs.arch::overview"),
        ).fetchall()
    assert len(hist_rows) >= 2, f"Expected at least 2 DELETED history rows, got {len(hist_rows)}"
    for hrow in hist_rows:
        meta = json.loads(hrow["meta"])
        assert meta["actor"] == "agent:pev-auditor", f"Expected actor 'agent:pev-auditor', got {meta.get('actor')}"
        assert meta["reason"] == "doc removed", f"Expected reason 'doc removed', got {meta.get('reason')}"


# ---------------------------------------------------------------------------
# Test: axiom_graph_purge_node on missing node
# ---------------------------------------------------------------------------


def test_purge_missing_node(mini_project: Path, db_path: Path):
    """Purging a node that doesn't exist returns an error."""
    from axiom_graph.mcp_server import axiom_graph_purge_node

    result = axiom_graph_purge_node(str(mini_project), node_id="proj::nonexistent", reason="cleanup")
    assert "ERROR" in result
    assert "not found" in result


# ---------------------------------------------------------------------------
# Test: axiom_graph_build no longer accepts purge param
# ---------------------------------------------------------------------------


def test_build_no_purge_param():
    """axiom_graph_build should not accept a purge parameter."""
    from axiom_graph.mcp_server import axiom_graph_build

    sig = inspect.signature(axiom_graph_build)
    assert "purge" not in sig.parameters


# ---------------------------------------------------------------------------
# Test: CLI cmd_build accepts --purge flag
# ---------------------------------------------------------------------------


def test_cli_build_has_purge_flag():
    """CLI cmd_build should have a --purge click option."""
    from axiom_graph.cli import cmd_build

    # Check the click params
    param_names = [p.name for p in cmd_build.params]
    assert "purge" in param_names
