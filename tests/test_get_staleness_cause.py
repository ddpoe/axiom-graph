"""Regression test for ``viz/nodes.py::get_staleness_cause`` correctness.

The pre-cycle implementation walked the AST with ``ast.walk`` and matched
on the bare short name (``node.title.split(".")[-1]``) -- so calling
``get_staleness_cause`` on ``TestB.test_foo`` would return the
``current_code_hash`` of whichever sibling method ``ast.walk`` reached
last.  After the consolidation, the same code path delegates to
:func:`axiom_graph.scanners.node_hashing.current_node_hash` which keys on
the qualified name.

This test reproduces the user-visible bug end-to-end: it builds two
sibling classes, edits one, then calls ``get_staleness_cause`` on the
*unchanged* sibling and asserts the reported ``current_code_hash``
matches that sibling's actual hash (not the edited one's).

Skipped when ``fastapi`` is not installed -- the ``viz`` server is an
optional extra (``[viz]``).

Tests use ``builder.build()`` for index seeding (per ADR-019 layering
lint -- direct ``db.upsert_node(... discovery_only=False)`` calls are
forbidden in new test files).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# viz/nodes.py imports fastapi at module load -- skip the file entirely when
# the optional `viz` extra is not installed in the dev environment.
pytest.importorskip("fastapi")

from axiom_graph.index import builder, db  # noqa: E402
from axiom_graph.index.staleness import compute_staleness, record_staleness  # noqa: E402
from axiom_graph.scanners.node_hashing import current_node_hash  # noqa: E402


def _bump_mtime(file_path: Path, delta: float = 10.0) -> None:
    stat = file_path.stat()
    os.utime(file_path, (stat.st_atime + delta, stat.st_mtime + delta))


def _find_node_by_suffix(db_path: Path, id_suffix: str):
    for n in db.all_nodes(db_path):
        if n.id.endswith(id_suffix):
            return n
    raise AssertionError(f"No node ending in {id_suffix!r}")


def test_get_staleness_cause_reports_correct_hash_for_sibling_method(
    mini_project: Path,
) -> None:
    """``get_staleness_cause`` must report the *correct* sibling's hash.

    Pre-fix bug: editing ``TestA.test_foo`` and then opening the
    staleness cause on either method showed the *last-walker-wins* hash
    of whichever sibling ``ast.walk`` reached last in the file.  Often
    this returned the wrong sibling's hash, making the displayed cause
    incoherent.

    Post-fix: the route delegates to ``current_node_hash`` which keys
    on the qualified name (``TestB.test_foo``), so the displayed
    ``current_code_hash`` matches the right sibling.
    """
    db_path = mini_project / ".axiom_graph" / "graph.db"
    src_dir = mini_project / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    py_file = src_dir / "mod.py"
    py_file.write_text(
        "class TestA:\n"
        "    def test_foo(self):\n"
        "        return 1\n"
        "\n"
        "class TestB:\n"
        "    def test_foo(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)
    a_node = _find_node_by_suffix(db_path, "::TestA.test_foo")
    b_node = _find_node_by_suffix(db_path, "::TestB.test_foo")

    # Both currently match disk -- record_staleness baselines them VERIFIED.
    record_staleness(db_path, mini_project, [a_node, b_node])

    # Edit only TestA -- TestB stays unchanged.
    py_file.write_text(
        "class TestA:\n"
        "    def test_foo(self):\n"
        "        return 999  # changed\n"
        "\n"
        "class TestB:\n"
        "    def test_foo(self):\n"
        "        return 2\n",
        encoding="utf-8",
    )
    _bump_mtime(py_file)

    # Recompute staleness so the DB reflects A=CONTENT_UPDATED, B=VERIFIED.
    a_node = _find_node_by_suffix(db_path, "::TestA.test_foo")
    b_node = _find_node_by_suffix(db_path, "::TestB.test_foo")
    statuses = compute_staleness(db_path, mini_project, [a_node, b_node])
    assert statuses[a_node.id][0] == "CONTENT_UPDATED"
    assert statuses[b_node.id][0] == "VERIFIED"

    # Persist statuses so get_staleness_cause sees them.
    record_staleness(db_path, mini_project, [a_node, b_node])

    # Wire up viz.server module globals so get_staleness_cause can read
    # the DB and the project root.
    from axiom_graph.viz import server
    from axiom_graph.viz.server import _apply_project

    _apply_project(mini_project)
    server._PROJECT_ROOT = mini_project
    server._DB_PATH = db_path

    from axiom_graph.viz.nodes import get_staleness_cause

    # Headline assertion: opening staleness-cause on TestA reports
    # the *correct* current hash for TestA (post-edit).
    a_cur_code_expected, _ = current_node_hash(db.get_node(db_path, a_node.id), mini_project)
    a_cause = get_staleness_cause(a_node.id)
    assert a_cause["details"]["current_code_hash"] == a_cur_code_expected, (
        "get_staleness_cause must report TestA's actual current hash"
    )

    # Now corrupt TestB's stored baseline so its details block is
    # populated (gated on own_status in (CONTENT_UPDATED, DESC_UPDATED)).
    # We do this via mark_node_clean with a *synthetic* mismatch -- but
    # since we cannot upsert directly, we mutate the in-memory node and
    # re-run compute_staleness with a stale baseline by recording the
    # edited file *before* TestB.
    #
    # Simplest path: edit TestB *too* so its own_status flips to
    # CONTENT_UPDATED.  Then read the cause and assert the displayed
    # hash equals TestB's (post-edit) hash, NOT TestA's hash.
    py_file.write_text(
        "class TestA:\n"
        "    def test_foo(self):\n"
        "        return 999  # changed\n"
        "\n"
        "class TestB:\n"
        "    def test_foo(self):\n"
        "        return 1234  # also changed -- but a DIFFERENT body\n",
        encoding="utf-8",
    )
    _bump_mtime(py_file)
    a_node = _find_node_by_suffix(db_path, "::TestA.test_foo")
    b_node = _find_node_by_suffix(db_path, "::TestB.test_foo")
    statuses_after = compute_staleness(db_path, mini_project, [a_node, b_node])
    assert statuses_after[a_node.id][0] == "CONTENT_UPDATED"
    assert statuses_after[b_node.id][0] == "CONTENT_UPDATED"
    record_staleness(db_path, mini_project, [a_node, b_node])

    # Now both are CU; the details block should populate for each.
    b_real_node = db.get_node(db_path, b_node.id)
    a_real_node = db.get_node(db_path, a_node.id)
    b_expected_cur, _ = current_node_hash(b_real_node, mini_project)
    a_expected_cur, _ = current_node_hash(a_real_node, mini_project)

    # Sanity: distinct hashes for the two siblings.
    assert a_expected_cur != b_expected_cur, "primitive regressed: sibling methods share a hash"

    b_cause = get_staleness_cause(b_node.id)
    assert b_cause["details"]["current_code_hash"] == b_expected_cur, (
        "get_staleness_cause on TestB.test_foo must report TestB's hash, not whichever sibling ast.walk reached last."
    )
    assert b_cause["details"]["current_code_hash"] != a_expected_cur, (
        "Pre-fix bug: TestB's reported current_code_hash equals TestA's "
        "(last-walker-wins).  This is the regression we are fixing."
    )
