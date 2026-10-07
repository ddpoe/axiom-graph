"""Tests for purging NOT_FOUND nodes: the MCP tool, the CLI, and build's --purge flag.

Covers:
- axiom_graph_purge_node happy path (NOT_FOUND code node)
- axiom_graph_purge_node refuses non-NOT_FOUND node
- axiom_graph_purge_node on doc node
- axiom_graph_purge_node on missing node
- purge refuses a file-level node whose file is still on disk (removed children, or a file
  that no longer parses)
- each surface records its own actor on the DELETED history row
- axiom_graph_build no longer accepts purge param; its docs point bulk purges at purge
- CLI cmd_build accepts --purge flag
- CLI ``axiom-graph purge``: named ids, --all-not-found, and argument validation
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest
from axiom_annotations import workflow

from axiom_graph.index import builder, db
from axiom_graph.lifecycle import api as lifecycle_api


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
    assert meta["actor"] == "agent"
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

    doc_id = "proj::docs/arch"
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
    assert db.get_node(db_path, "proj::docs/arch::overview") is None

    # Verify DELETED history rows carry the actor and reason from reason_meta
    with db._connect(db_path) as conn:
        hist_rows = conn.execute(
            "SELECT meta FROM node_history WHERE change_type = 'DELETED' AND preserved = 1 AND node_id IN (?, ?)",
            (doc_id, "proj::docs/arch::overview"),
        ).fetchall()
    assert len(hist_rows) >= 2, f"Expected at least 2 DELETED history rows, got {len(hist_rows)}"
    for hrow in hist_rows:
        meta = json.loads(hrow["meta"])
        assert meta["actor"] == "agent", f"Expected actor 'agent', got {meta.get('actor')}"
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


# ---------------------------------------------------------------------------
# CLI: axiom-graph purge
# ---------------------------------------------------------------------------


_KEPT_WITH_DROPPED = "def keep():\n    '''Stay put.'''\n\n\ndef dropped():\n    '''Removed later.'''\n"


def _project_with_ghosts(mini_project: Path) -> tuple[str, str]:
    """Index a project, then delete a module, a doc and one function so their nodes go NOT_FOUND.

    ``kept.py`` survives with ``dropped`` removed from it, so its module node
    is NOT_FOUND only by inheriting its removed child's status.

    Returns:
        ``(ghost_function_id, live_function_id)`` -- a function whose file was
        deleted, and one whose file is still on disk.
    """
    from axiom_graph.index.staleness import record_staleness

    (mini_project / "gone.py").write_text("def hello():\n    '''Say hello.'''\n", encoding="utf-8")
    (mini_project / "kept.py").write_text(_KEPT_WITH_DROPPED, encoding="utf-8")
    _write_doc(
        mini_project / "docs",
        "arch.json",
        "Architecture",
        [{"id": "overview", "heading": "Overview", "content": "An overview."}],
    )
    _build_full(mini_project)
    db_path = mini_project / ".axiom_graph" / "graph.db"
    ids = {n.id for n in db.all_nodes(db_path)}
    ghost = next(i for i in ids if i.endswith("::hello"))
    live = next(i for i in ids if i.endswith("::keep"))

    (mini_project / "kept.py").write_text("def keep():\n    '''Stay put.'''\n", encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=True)
    # Deleted after the build, so the files' nodes are still indexed.
    (mini_project / "gone.py").unlink()
    (mini_project / "docs" / "arch.json").unlink()
    record_staleness(db_path, mini_project, db.all_nodes(db_path))
    return ghost, live


def _own_status(db_path: Path, node_id: str) -> str | None:
    """Return the persisted own_status of *node_id*, or None when it is not indexed."""
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["own_status"] if row else None


def test_cli_purge_removes_named_not_found_nodes_and_refuses_the_rest(mini_project: Path, db_path: Path):
    """Named NOT_FOUND nodes are purged as the human with the reason recorded; live nodes are refused, kept, and fail the run.

    A module whose file is still on disk is refused even though it reads
    NOT_FOUND, and the output names the removed child to purge instead.
    """
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    ghost, live = _project_with_ghosts(mini_project)
    kept_module = live.removesuffix("::keep")
    dropped = f"{kept_module}::dropped"
    assert _own_status(db_path, ghost) == "NOT_FOUND"
    assert _own_status(db_path, kept_module) == "NOT_FOUND"

    result = CliRunner().invoke(
        cli, ["purge", ghost, live, kept_module, str(mini_project), "--reason", "module deleted"]
    )

    assert result.exit_code == 1, result.output
    assert db.get_node(db_path, ghost) is None
    assert db.get_node(db_path, live) is not None
    assert db.get_node(db_path, kept_module) is not None
    assert f"Purged: {ghost}" in result.output
    assert live in result.output and "VERIFIED" in result.output
    lines = result.output.splitlines()
    refusal = next(i for i, line in enumerate(lines) if line.startswith(f"Not purged: {kept_module} "))
    assert "inherited" in lines[refusal]
    assert lines[refusal + 1] == f"  - {dropped}"
    with db._connect(db_path) as conn:
        meta = conn.execute(
            "SELECT meta FROM node_history WHERE node_id = ? AND change_type = 'DELETED' AND preserved = 1",
            (ghost,),
        ).fetchone()["meta"]
    assert json.loads(meta)["actor"] == "human"
    assert json.loads(meta)["reason"] == "module deleted"


def test_cli_purge_all_not_found_confirms_then_purges_only_not_found_nodes(mini_project: Path, db_path: Path):
    """--all-not-found asks first, then removes every gone node (docs with their sections) and nothing else.

    A file-level node whose file is still on disk is NOT_FOUND only by
    inheriting a removed child's status; it is kept, and clears on the next
    check once the child is gone.
    """
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    ghost, live = _project_with_ghosts(mini_project)
    doc_id = "proj::docs/arch"
    dropped = live.replace("::keep", "::dropped")
    kept_module = live.removesuffix("::keep")
    for nid in (ghost, doc_id, dropped):
        assert _own_status(db_path, nid) == "NOT_FOUND", nid

    declined = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project)], input="n\n")
    assert declined.exit_code != 0
    assert db.get_node(db_path, ghost) is not None, "declining the prompt purges nothing"

    result = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project), "--yes"])

    assert result.exit_code == 0, result.output
    for gone in (ghost, doc_id, f"{doc_id}::overview", dropped):
        assert db.get_node(db_path, gone) is None, gone
    assert db.get_node(db_path, live) is not None
    assert db.get_node(db_path, kept_module) is not None, "a module whose file exists is never purged"

    lifecycle_api.compute_check_summary(db_path, mini_project)
    again = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project), "--yes"])
    assert again.exit_code == 0
    assert "No NOT_FOUND nodes" in again.output


def test_cli_purge_needs_exactly_one_of_node_ids_or_all_not_found(mini_project: Path):
    """Naming no node, or naming nodes alongside --all-not-found, is a usage error."""
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    neither = CliRunner().invoke(cli, ["purge", str(mini_project)])
    both = CliRunner().invoke(cli, ["purge", "proj::x", str(mini_project), "--all-not-found"])

    assert neither.exit_code == 2, neither.output
    assert both.exit_code == 2, both.output


# ---------------------------------------------------------------------------
# Shared purge rules: live file-level nodes, actor, bulk-purge pointers
# ---------------------------------------------------------------------------


def _write_module(root: Path, with_g: bool) -> None:
    """Write ``pkg/mod.py`` with ``f``, plus ``g`` when *with_g*."""
    pkg = root / "pkg"
    pkg.mkdir(exist_ok=True)
    source = "def f():\n    '''Stay put.'''\n"
    if with_g:
        source += "\n\ndef g():\n    '''Removed later.'''\n"
    (pkg / "mod.py").write_text(source, encoding="utf-8")


def _index_then_break(root: Path, db_path: Path, rel_path: str, broken: str) -> None:
    """Index ``pkg/mod.py`` (``f``, ``g``) and ``docs/arch.json`` (sections ``f``, ``g``), then break one and check.

    *broken* is saved over *rel_path*, so every node in that file reads
    NOT_FOUND while the file stays on disk.
    """
    _write_module(root, with_g=True)
    sections = [{"id": "f", "heading": "F", "content": "Stays."}, {"id": "g", "heading": "G", "content": "Stays."}]
    _write_doc(root / "docs", "arch.json", "Architecture", sections)
    lifecycle_api.build_index(db_path, root, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    (root / rel_path).write_text(broken, encoding="utf-8")
    lifecycle_api.compute_check_summary(db_path, root)


def _history_rows(db_path: Path, node_id: str) -> list[tuple]:
    """Return every history row of *node_id*, oldest first."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, change_type, meta FROM node_history WHERE node_id = ? ORDER BY id",
            (node_id,),
        ).fetchall()
    return [tuple(r) for r in rows]


@workflow(
    purpose=(
        "Purge refuses a module whose file is still on disk: its NOT_FOUND is inherited from a "
        "removed function, so the module keeps its row, history and verification, the refusal "
        "names the removed function to purge instead, and once that function is purged the "
        "module is VERIFIED again"
    ),
)
def test_purge_refuses_a_module_whose_file_is_on_disk(mini_project: Path, db_path: Path):
    from axiom_graph.mcp_server import axiom_graph_purge_node

    module, removed = "proj::pkg.mod", "proj::pkg.mod::g"
    _write_module(mini_project, with_g=True)
    lifecycle_api.build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    lifecycle_api.mark_clean_nodes(db_path, mini_project, [module], "reviewed", verified_by="human")
    time.sleep(0.05)
    _write_module(mini_project, with_g=False)
    lifecycle_api.build_index(db_path, mini_project, project_id="proj")
    assert _own_status(db_path, removed) == "NOT_FOUND"
    assert _own_status(db_path, module) == "NOT_FOUND"
    history_before = _history_rows(db_path, module)
    verification_before = db.get_verification(db_path, module)
    assert verification_before is not None

    [refused] = lifecycle_api.purge_nodes(db_path, mini_project, [module], "cleanup", actor="agent")
    tool_output = axiom_graph_purge_node(str(mini_project), node_id=module, reason="cleanup")

    assert not refused.purged
    assert refused.reason == lifecycle_api.PURGE_REFUSED_INHERITED
    assert refused.deleted_children == [removed]
    assert tool_output.startswith("ERROR") and "inherited" in tool_output and removed in tool_output
    assert "if they were really removed" in tool_output
    assert db.get_node(db_path, module) is not None
    assert _history_rows(db_path, module) == history_before
    assert db.get_verification(db_path, module) == verification_before

    assert lifecycle_api.purge_nodes(db_path, mini_project, [removed], "cleanup", actor="agent")[0].purged
    lifecycle_api.compute_check_summary(db_path, mini_project)
    assert _own_status(db_path, module) == "VERIFIED"


@workflow(
    purpose=(
        "Purge refuses a DocJSON doc whose file is still on disk whatever its stored status: the "
        "API and the MCP tool keep the doc and its sections, while a doc whose file is gone is purged"
    ),
)
def test_purge_refuses_a_docjson_doc_whose_file_is_on_disk(mini_project: Path, db_path: Path):
    """A rebuild drops a removed section instead of leaving it NOT_FOUND, so the status is stored directly."""
    from axiom_graph.mcp_server import axiom_graph_purge_node

    doc, section = "proj::docs/arch", "proj::docs/arch::f"
    _write_doc(mini_project / "docs", "arch.json", "Architecture", [{"id": "f", "heading": "F", "content": "Stays."}])
    _write_doc(mini_project / "docs", "gone.json", "Gone", [{"id": "f", "heading": "F", "content": "Goes."}])
    lifecycle_api.build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    (mini_project / "docs" / "gone.json").unlink()
    with db._connect(db_path) as conn:
        conn.execute("UPDATE nodes SET own_status = 'NOT_FOUND' WHERE id IN (?, 'proj::docs/gone')", (doc,))

    live, gone = lifecycle_api.purge_nodes(db_path, mini_project, [doc, "proj::docs/gone"], "cleanup", actor="agent")
    tool_output = axiom_graph_purge_node(str(mini_project), node_id=doc, reason="cleanup")

    assert (live.purged, live.reason) == (False, lifecycle_api.PURGE_REFUSED_INHERITED)
    assert tool_output.startswith("ERROR") and "inherited" in tool_output
    assert db.get_node(db_path, doc) is not None
    assert db.get_node(db_path, section) is not None
    assert gone.purged
    assert db.get_node(db_path, "proj::docs/gone") is None


@workflow(
    purpose=(
        "Purge refuses a module or DocJSON doc whose file is on disk but no longer parses: every "
        "node in it reads NOT_FOUND, so the API, the MCP tool and the CLI say the file likely does "
        "not parse, name no children to purge, and remove nothing"
    ),
)
@pytest.mark.parametrize(
    ("rel_path", "broken", "anchor", "children"),
    [
        ("pkg/mod.py", "def f(:\n", "proj::pkg.mod", ["proj::pkg.mod::f", "proj::pkg.mod::g"]),
        (
            "docs/arch.json",
            '{"title": "A", "sections": [',
            "proj::docs/arch",
            ["proj::docs/arch::f", "proj::docs/arch::g"],
        ),
    ],
    ids=["python-module", "docjson-doc"],
)
def test_purge_refuses_a_file_level_node_whose_file_does_not_parse(
    mini_project: Path, db_path: Path, rel_path: str, broken: str, anchor: str, children: list[str]
):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli
    from axiom_graph.mcp_server import axiom_graph_purge_node

    _index_then_break(mini_project, db_path, rel_path, broken)
    for nid in (anchor, *children):
        assert _own_status(db_path, nid) == "NOT_FOUND", nid

    [refused] = lifecycle_api.purge_nodes(db_path, mini_project, [anchor], "cleanup", actor="agent")
    tool_output = axiom_graph_purge_node(str(mini_project), node_id=anchor, reason="cleanup")
    cli_result = CliRunner().invoke(cli, ["purge", anchor, str(mini_project)])

    assert not refused.purged
    assert refused.reason == lifecycle_api.PURGE_REFUSED_UNPARSEABLE
    assert refused.deleted_children == []
    for output in (tool_output, cli_result.output):
        assert "does not parse" in output
        assert not any(child in output for child in children)
    assert tool_output.startswith("ERROR")
    assert cli_result.exit_code == 1
    for nid in (anchor, *children):
        assert db.get_node(db_path, nid) is not None, nid


@workflow(
    purpose=(
        "Purge refuses a function or section in a file that is on disk but does not parse: the API, "
        "the MCP tool and the CLI refuse it as file_unparseable, say to fix the file, and remove nothing"
    ),
)
@pytest.mark.parametrize(
    ("rel_path", "broken", "target"),
    [
        ("pkg/mod.py", "def f(:\n", "proj::pkg.mod::g"),
        ("docs/arch.json", '{"title": "A", "sections": [', "proj::docs/arch::g"),
    ],
    ids=["python-function", "docjson-section"],
)
def test_purge_refuses_a_node_in_a_file_that_does_not_parse(
    mini_project: Path, db_path: Path, rel_path: str, broken: str, target: str
):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli
    from axiom_graph.mcp_server import axiom_graph_purge_node

    _index_then_break(mini_project, db_path, rel_path, broken)
    assert _own_status(db_path, target) == "NOT_FOUND"
    history_before = _history_rows(db_path, target)

    [refused] = lifecycle_api.purge_nodes(db_path, mini_project, [target], "cleanup", actor="agent")
    tool_output = axiom_graph_purge_node(str(mini_project), node_id=target, reason="cleanup")
    cli_result = CliRunner().invoke(cli, ["purge", target, str(mini_project)])

    assert (refused.purged, refused.reason) == (False, lifecycle_api.PURGE_REFUSED_UNPARSEABLE)
    assert tool_output.startswith("ERROR")
    assert cli_result.exit_code == 1, cli_result.output
    for output in (tool_output, cli_result.output):
        assert "does not parse" in output and "fix the file" in output
    assert db.get_node(db_path, target) is not None
    assert _history_rows(db_path, target) == history_before


@workflow(
    purpose=(
        "purge --all-not-found keeps every node of a file that is on disk but does not parse: the "
        "preview names the file once, to fix and re-run check, lists none of its nodes, and still "
        "purges the nodes that are really gone"
    ),
)
def test_purge_all_not_found_keeps_every_node_of_a_file_that_does_not_parse(mini_project: Path, db_path: Path):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    module, ghost = "proj::pkg.mod", "proj::gone::hello"
    functions = [f"{module}::f", f"{module}::g"]
    (mini_project / "gone.py").write_text("def hello():\n    '''Say hello.'''\n", encoding="utf-8")
    _index_then_break(mini_project, db_path, "pkg/mod.py", "def f(:\n")
    (mini_project / "gone.py").unlink()
    lifecycle_api.compute_check_summary(db_path, mini_project)
    for nid in (module, *functions, ghost):
        assert _own_status(db_path, nid) == "NOT_FOUND", nid

    result = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project), "--yes"])

    assert result.exit_code == 0, result.output
    for nid in (module, *functions):
        assert db.get_node(db_path, nid) is not None, nid
    assert db.get_node(db_path, ghost) is None
    assert module not in result.output, "no node of the unparseable file is listed"
    [kept] = [line for line in result.output.splitlines() if "pkg/mod.py" in line]
    assert "does not parse" in kept and "re-run check" in kept


@workflow(
    purpose=(
        "A function really removed from a file stays purgeable: it is refused while the file does "
        "not parse and purged by purge --all-not-found once the file parses again, even when every "
        "function in the file was removed"
    ),
)
@pytest.mark.parametrize(
    ("fixed", "removed"),
    [
        ("def f():\n    '''Stay put.'''\n", ["proj::pkg.mod::g"]),
        ("X = 1\n", ["proj::pkg.mod::f", "proj::pkg.mod::g"]),
    ],
    ids=["one-removed", "every-function-removed"],
)
def test_a_function_removed_from_a_file_that_parses_again_is_purgeable(
    mini_project: Path, db_path: Path, fixed: str, removed: list[str]
):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli

    _index_then_break(mini_project, db_path, "pkg/mod.py", "def f(:\n")
    while_broken = lifecycle_api.purge_nodes(db_path, mini_project, removed, "cleanup", actor="agent")
    assert [r.reason for r in while_broken] == [lifecycle_api.PURGE_REFUSED_UNPARSEABLE] * len(removed)

    time.sleep(0.05)
    (mini_project / "pkg" / "mod.py").write_text(fixed, encoding="utf-8")
    lifecycle_api.compute_check_summary(db_path, mini_project)
    for nid in removed:
        assert _own_status(db_path, nid) == "NOT_FOUND", nid
    result = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project), "--yes"])

    assert result.exit_code == 0, result.output
    for nid in removed:
        assert db.get_node(db_path, nid) is None, nid
    assert db.get_node(db_path, "proj::pkg.mod") is not None, "the module's file is on disk"


@workflow(
    purpose=(
        "A named purge of the module of a file that still parses but lost every function is "
        "refused as inherited, listing the removed functions to purge, not as file_unparseable"
    ),
)
def test_purge_of_the_module_of_a_parsing_file_with_every_function_removed_lists_them(
    mini_project: Path, db_path: Path
):
    removed = ["proj::pkg.mod::f", "proj::pkg.mod::g"]
    _write_module(mini_project, with_g=True)
    lifecycle_api.build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    (mini_project / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    lifecycle_api.compute_check_summary(db_path, mini_project)
    assert _own_status(db_path, "proj::pkg.mod") == "NOT_FOUND"

    [refused] = lifecycle_api.purge_nodes(db_path, mini_project, ["proj::pkg.mod"], "cleanup", actor="agent")

    assert (refused.purged, refused.reason) == (False, lifecycle_api.PURGE_REFUSED_INHERITED)
    assert refused.deleted_children == removed


@workflow(
    purpose=(
        "Purging several NOT_FOUND nodes of one file parses that file once per operation: once "
        "for a named purge, and once for purge --all-not-found's selection and purge together"
    ),
)
@pytest.mark.parametrize("bulk", [False, True], ids=["named", "all-not-found"])
def test_purge_parses_each_file_once_per_operation(
    mini_project: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch, bulk: bool
):
    import ast

    functions = ["f", "g", "h", "k"]
    removed = [f"proj::pkg.mod::{name}" for name in functions[1:]]
    pkg = mini_project / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("".join(f"def {n}():\n    '''{n}.'''\n\n\n" for n in functions), encoding="utf-8")
    lifecycle_api.build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    time.sleep(0.05)
    (pkg / "mod.py").write_text("def f():\n    '''f.'''\n", encoding="utf-8")
    lifecycle_api.compute_check_summary(db_path, mini_project)
    parses: list[str] = []
    real_parse = ast.parse
    monkeypatch.setattr(ast, "parse", lambda *a, **kw: parses.append(kw.get("filename", "")) or real_parse(*a, **kw))

    if bulk:
        selection = lifecycle_api.select_purgeable_not_found(db_path, mini_project)
        assert selection.to_purge == removed and selection.inherited == ["proj::pkg.mod"]
        targets, verdicts = selection.to_purge, selection.parse_verdicts
    else:
        targets, verdicts = ["proj::pkg.mod", *removed], None
    results = lifecycle_api.purge_nodes(
        db_path, mini_project, targets, "cleanup", actor="human", parse_verdicts=verdicts
    )

    assert [r.node_id for r in results if r.purged] == removed
    assert len(parses) == 1, parses


_TS_SOURCE = (
    "export function a(): number {\n  return 1;\n}\n\n"
    "export function b(): number {\n  return 2;\n}\n\n"
    "export function c(): number {\n  return 3;\n}\n"
)


@workflow(
    purpose=(
        "A JS/TS file that tree-sitter parses with errors counts as unparseable: the half-edited "
        "function it drops reads NOT_FOUND, but purge --all-not-found keeps every node of the file "
        "and a named purge of that function is refused as file_unparseable; without tree-sitter "
        "the file counts as unparseable too"
    ),
)
def test_purge_keeps_every_node_of_a_ts_file_with_parse_errors(
    mini_project: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from click.testing import CliRunner

    from axiom_graph.cli import main as cli
    from axiom_graph.scanners import js_scanner

    if not js_scanner.HAS_TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    (mini_project / "axiom-graph.toml").write_text('[axiom_graph.scan]\njs_paths = ["web/*.ts"]\n', encoding="utf-8")
    web = mini_project / "web"
    web.mkdir()
    (web / "app.ts").write_text(_TS_SOURCE, encoding="utf-8")
    lifecycle_api.build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    file_nodes = sorted(n.id for n in db.all_nodes(db_path) if (n.location or "").startswith("web/app.ts"))
    [half_edited] = [nid for nid in file_nodes if nid.endswith("::b")]
    time.sleep(0.05)
    (web / "app.ts").write_text(_TS_SOURCE.replace("b(): number {", "b( {"), encoding="utf-8")
    lifecycle_api.compute_check_summary(db_path, mini_project)
    assert _own_status(db_path, half_edited) == "NOT_FOUND"

    bulk = CliRunner().invoke(cli, ["purge", "--all-not-found", str(mini_project), "--yes"])
    [refused] = lifecycle_api.purge_nodes(db_path, mini_project, [half_edited], "cleanup", actor="agent")

    assert bulk.exit_code == 0, bulk.output
    assert "web/app.ts" in bulk.output and "does not parse" in bulk.output
    for nid in file_nodes:
        assert db.get_node(db_path, nid) is not None, nid
    assert (refused.purged, refused.reason) == (False, lifecycle_api.PURGE_REFUSED_UNPARSEABLE)
    (web / "app.ts").write_text(_TS_SOURCE, encoding="utf-8")
    assert not lifecycle_api.file_unparseable(mini_project, half_edited, None, "web/app.ts")
    monkeypatch.setattr(js_scanner, "HAS_TREE_SITTER", False)
    assert lifecycle_api.file_unparseable(mini_project, half_edited, None, "web/app.ts")


def test_purge_nodes_requires_an_actor():
    """purge_nodes has no default actor: every caller names who is purging."""
    actor = inspect.signature(lifecycle_api.purge_nodes).parameters["actor"]

    assert actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert actor.default is inspect.Parameter.empty


def test_build_docs_point_bulk_purges_at_purge_all_not_found():
    """The build tool's docs send bulk purges to ``purge --all-not-found``; build --purge says what it really does."""
    from axiom_graph.cli import cmd_build
    from axiom_graph.lifecycle import mcp_tools
    from axiom_graph.mcp import server

    for build_tool in (mcp_tools.axiom_graph_build, server.axiom_graph_build):
        doc = inspect.getdoc(build_tool) or ""
        assert "axiom-graph purge --all-not-found" in doc
        assert "build --purge" not in doc
    purge_flag = next(p for p in cmd_build.params if p.name == "purge")
    assert "deleted-file pass" in (purge_flag.help or "")
