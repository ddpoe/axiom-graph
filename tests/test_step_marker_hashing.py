"""Step / AutoStep markers hash as description, not code.

A function's ``code_hash`` excludes its ``Step(...)`` / ``AutoStep(...)``
marker statements; their text is folded into ``desc_hash`` alongside the
docstring.  A marker renumber therefore reads as DESC_UPDATED on the
function and leaves ``validates`` tests and ``documents`` sections alone.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.index import builder, db, staleness
from axiom_graph.scanners.module_scanner import scan_module
from axiom_graph.scanners.node_hashing import _walk_python_functions

_ORIGINAL = '''
from axiom_annotations import Step


def run(items):
    """Process the items."""
    口 = Step(step_num=1, name="Load", purpose="Load the items")
    total = 0
    for item in items:
        口 = Step(step_num=2, name="Add", purpose="Add one item")
        total += item
    with open("x") as fh:
        口 = Step(step_num=3, name="Write", purpose="Write the total")
        fh.write(str(total))
    return total
'''


def _status(db_path: Path, node_id: str) -> tuple[str, str]:
    """Return the persisted ``(own_status, link_status)`` of *node_id*."""
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, f"node {node_id} not found"
    return row["own_status"], row["link_status"]


def _record(db_path: Path, project_root: Path) -> None:
    """Compute and persist staleness for every indexed node."""
    staleness.record_staleness(db_path, project_root, db.all_nodes(db_path))


def _hashes(source: str) -> tuple[str, str | None]:
    """Return ``(code_hash, desc_hash)`` of ``run`` via the scanner path."""
    tree = ast.parse(textwrap.dedent(source))
    return _walk_python_functions(tree)["run"]


def test_marker_renumber_keeps_code_hash_and_changes_desc_hash():
    renumbered = _ORIGINAL.replace("step_num=2", "step_num=5").replace("step_num=3", "step_num=6")
    code_a, desc_a = _hashes(_ORIGINAL)
    code_b, desc_b = _hashes(renumbered)
    assert code_a == code_b
    assert desc_a != desc_b


def test_non_marker_statement_change_changes_code_hash():
    edited = _ORIGINAL.replace("total = 0", "total = 1")
    code_a, desc_a = _hashes(_ORIGINAL)
    code_b, desc_b = _hashes(edited)
    assert code_a != code_b
    assert desc_a == desc_b


def test_nested_markers_are_stripped_from_code_and_folded_into_desc():
    # The loop and ``with`` markers are the only ones edited here.
    edited = _ORIGINAL.replace('"Add one item"', '"Add the next item"').replace(
        '"Write the total"', '"Persist the total"'
    )
    code_a, desc_a = _hashes(_ORIGINAL)
    code_b, desc_b = _hashes(edited)
    assert code_a == code_b
    assert desc_a != desc_b


def test_scanner_and_node_hashing_paths_agree(tmp_path: Path):
    src = tmp_path / "mod.py"
    src.write_text(textwrap.dedent(_ORIGINAL), encoding="utf-8")
    nodes, _ = scan_module(src, tmp_path, "proj")
    run_node = next(n for n in nodes if n.id == "proj::mod::run")
    assert (run_node.code_hash, run_node.desc_hash) == _hashes(_ORIGINAL)
    # level_2 stays the docstring; marker text only feeds the hash.
    assert run_node.level_2 == "Process the items."


@workflow(
    purpose="Renumbering a function's step markers flags only the function's own "
    "description (DESC_UPDATED); the test that validates it stays VERIFIED"
)
def test_marker_renumber_leaves_validating_test_verified(mini_project: Path, db_path: Path):
    project_root = mini_project
    pkg = project_root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    mod = pkg / "mod.py"
    mod.write_text(textwrap.dedent(_ORIGINAL), encoding="utf-8")
    tests_dir = project_root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import run\n\n\ndef test_run():\n    assert run([]) == 0\n",
        encoding="utf-8",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)
    _record(db_path, project_root)

    run_id = "proj::pkg.mod::run"
    test_id = "proj::tests.test_mod::test_run"
    edges = db.all_edges(db_path)
    assert any(e.edge_type == "validates" and e.from_id == test_id and e.to_id == run_id for e in edges)

    renumbered = textwrap.dedent(_ORIGINAL).replace("step_num=2", "step_num=5").replace("step_num=3", "step_num=6")
    mod.write_text(renumbered, encoding="utf-8")
    builder.build(project_root, project_id="proj")
    _record(db_path, project_root)
    _record(db_path, project_root)

    assert _status(db_path, run_id)[0] == "DESC_UPDATED"
    assert _status(db_path, test_id)[1] == "VERIFIED"

    # Control: a real code edit to the same function does reach the test.
    mod.write_text(renumbered.replace("total = 0", "total = 1"), encoding="utf-8")
    builder.build(project_root, project_id="proj")
    _record(db_path, project_root)
    _record(db_path, project_root)

    assert _status(db_path, run_id)[0] == "CONTENT_UPDATED"
    assert _status(db_path, test_id)[1] == "LINKED_STALE"
