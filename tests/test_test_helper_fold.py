"""Behavioural tests for which nodes in a Python test file own ``validates`` edges.

Only the tests pytest collects own them.  Helpers, fixtures and nested defs
are test support: their calls fold into every collected test that reaches
them (directly, through a helper chain, or by naming a fixture as a
parameter), or into the test module's node when no test does.  Existing
indexes heal on their next build: a schema migration forces test files to be
rescanned and the builder retires the edges a file no longer intends.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.db import migrations
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes
from axiom_graph.models import AxiomEdge, make_edge
from axiom_graph.scanners.module_scanner import scan_module

_PROD_SRC = """\
def direct(): pass
def via_worker(): pass
def via_chain(): pass
def via_fixture(): pass
def via_method_helper(): pass
def only_unused(): pass
"""

_TEST_SRC = """\
import pytest

from prod import direct, via_worker, via_chain, via_fixture, via_method_helper, only_unused


def _outer_helper():
    return _inner_helper()


def _inner_helper():
    via_chain()
    return _outer_helper()  # a cycle the fold must survive


@pytest.fixture
def made():
    return via_fixture()


def _unused_helper():
    only_unused()


def test_reaches_everything(made):
    direct()

    def worker():
        via_worker()

    worker()
    _outer_helper()


class TestGroup:
    def _setup_thing(self):
        via_method_helper()

    def test_method(self):
        self._setup_thing()
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _validates(edges: list[AxiomEdge]) -> set[tuple[str, str]]:
    return {(e.from_id.rsplit("::", 1)[-1], e.to_id.rsplit("::", 1)[-1]) for e in edges if e.edge_type == "validates"}


@workflow(
    purpose="Scanning a test file gives validates edges only to collected tests, folding nested workers, helper "
    "chains, fixtures named as parameters and same-class self helpers into them, and leaves an unused helper's "
    "target on the test module's node"
)
def test_only_collected_tests_own_validates(tmp_path: Path) -> None:
    _write(tmp_path / "prod.py", _PROD_SRC)
    test_file = _write(tmp_path / "test_fold.py", _TEST_SRC)

    nodes, edges = scan_module(test_file, tmp_path, "proj")

    subtypes = {n.id.rsplit("::", 1)[-1]: n.subtype for n in nodes if n.node_type == "atomic_process"}
    tests = {name for name, subtype in subtypes.items() if subtype == "test"}
    assert tests == {"test_reaches_everything", "TestGroup.test_method"}
    for support in (
        "_outer_helper",
        "_inner_helper",
        "made",
        "_unused_helper",
        "test_reaches_everything.worker",
        "TestGroup._setup_thing",
    ):
        assert subtypes[support] == "function", support

    assert _validates(edges) == {
        ("test_reaches_everything", "direct"),
        ("test_reaches_everything", "via_worker"),
        ("test_reaches_everything", "via_chain"),
        ("test_reaches_everything", "via_fixture"),
        ("TestGroup.test_method", "via_method_helper"),
        ("test_fold", "only_unused"),
    }


# ---------------------------------------------------------------------------
# Build-level behaviour
# ---------------------------------------------------------------------------

PROD_ID = "proj::src.prod::compute"
TEST_ID = "proj::tests.test_prod::test_compute"
HELPER_ID = "proj::tests.test_prod::_run_compute"

_HELPER_TEST_SRC = """\
from src.prod import compute


def _run_compute():
    def inner():
        return compute()

    return inner()


def test_compute():
    assert _run_compute() is not None
"""


def _set_compute(root: Path, value: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "prod.py", f"def compute():\n    return {value}\n")


def _helper_project(root: Path) -> Path:
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    _set_compute(root, 0)
    _write(root / "tests" / "test_prod.py", _HELPER_TEST_SRC)
    return _db_path(str(root))


def _persisted_link(dbp: Path, node_id: str) -> str:
    with sqlite3.connect(dbp) as conn:
        row = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0]


def _validates_from(dbp: Path, from_id: str) -> set[str]:
    return {e.to_id for e in db.all_edges(dbp) if e.edge_type == "validates" and e.from_id == from_id}


@workflow(
    purpose="A maintainer changes a production function that a test reaches only through a same-file helper; the "
    "collected test is flagged LINKED_STALE via that function, and no helper or parent is flagged"
)
def test_change_behind_a_helper_flags_the_calling_test(tmp_path: Path) -> None:
    dbp = _helper_project(tmp_path)

    口 = Step(
        step_num=1,
        name="Build and verify the test",
        purpose="The test owns the helper's validates edge and starts VERIFIED",
    )
    build_index(dbp, tmp_path)
    assert _validates_from(dbp, TEST_ID) == {PROD_ID}
    assert _validates_from(dbp, HELPER_ID) == set()
    mark_clean_nodes(dbp, tmp_path, [TEST_ID], reason="reviewed", verified_by="human")
    assert _persisted_link(dbp, TEST_ID) == "VERIFIED"

    口 = Step(
        step_num=2,
        name="Change the production function",
        purpose="Edit the function only the helper calls, then build and check",
    )
    _set_compute(tmp_path, 1)
    build_index(dbp, tmp_path)
    summary = compute_check_summary(dbp, tmp_path)
    assert summary is not None

    口 = Step(
        step_num=3,
        name="Assert where the drift landed",
        purpose="The collected test is LINKED_STALE via the function; the helper and its nested worker are not, "
        "and nothing is LINKED_STALE without a via",
    )
    own, link, via = summary.statuses[TEST_ID]
    assert link == "LINKED_STALE" and PROD_ID in via
    assert _persisted_link(dbp, TEST_ID) == "LINKED_STALE"
    for support in (HELPER_ID, f"{HELPER_ID}.inner"):
        assert summary.statuses[support][1] == "VERIFIED", support
    stale = {
        node_id: node_via for node_id, (_o, status, node_via) in summary.statuses.items() if status == "LINKED_STALE"
    }
    # The test module is a composite that inherits its child test's status; every
    # function-level row that is stale is the collected test.
    assert set(stale) - {"proj::tests.test_prod"} == {TEST_ID}, stale


def _snapshot(dbp: Path, table: str) -> list[tuple]:
    with sqlite3.connect(dbp) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def _make_pre_upgrade(dbp: Path) -> None:
    """Put the index back in the shape the previous scanner and schema left it in."""
    with db._connect(dbp) as conn:
        db.upsert_edge_conn(conn, make_edge("validates", HELPER_ID, PROD_ID))
        db.upsert_edge_conn(conn, make_edge("validates", f"{HELPER_ID}.inner", PROD_ID))
        conn.execute("DELETE FROM edges WHERE edge_type = 'validates' AND from_id = ?", (TEST_ID,))
        conn.execute("UPDATE nodes SET subtype = 'test' WHERE id IN (?, ?)", (HELPER_ID, f"{HELPER_ID}.inner"))
        conn.execute("PRAGMA user_version = 2")


@workflow(
    purpose="Upgrading an index whose test helpers own validates edges: the next normal build rescans test files, "
    "drops the helper edges, adds the folded ones, leaves history and verification untouched, and a second build "
    "changes nothing"
)
def test_upgrade_build_retires_helper_owned_validates(tmp_path: Path) -> None:
    dbp = _helper_project(tmp_path)
    build_index(dbp, tmp_path)
    mark_clean_nodes(dbp, tmp_path, [TEST_ID], reason="reviewed", verified_by="human")
    _make_pre_upgrade(dbp)
    assert _validates_from(dbp, HELPER_ID) == {PROD_ID}
    assert _validates_from(dbp, TEST_ID) == set()
    history = _snapshot(dbp, "node_history")
    verification = _snapshot(dbp, "node_verification")

    applied = migrations.run_migrations(dbp)
    assert applied == list(range(3, migrations.CURRENT_SCHEMA_VERSION + 1))
    assert _snapshot(dbp, "node_history") == history
    assert _snapshot(dbp, "node_verification") == verification
    with sqlite3.connect(dbp) as conn:
        test_file_mtimes = conn.execute("SELECT file_mtime FROM nodes WHERE location = 'tests/test_prod.py'").fetchall()
    assert test_file_mtimes and all(r[0] is None for r in test_file_mtimes), "test files are forced to rescan"

    build_index(dbp, tmp_path)
    assert _validates_from(dbp, HELPER_ID) == set()
    assert _validates_from(dbp, f"{HELPER_ID}.inner") == set()
    assert _validates_from(dbp, TEST_ID) == {PROD_ID}
    with sqlite3.connect(dbp) as conn:
        helper_subtype = conn.execute("SELECT subtype FROM nodes WHERE id = ?", (HELPER_ID,)).fetchone()[0]
    assert helper_subtype == "function"
    assert _snapshot(dbp, "node_verification") == verification

    edges_after = {e.id for e in db.all_edges(dbp)}
    history_after = _snapshot(dbp, "node_history")
    assert migrations.run_migrations(dbp) == []
    build_index(dbp, tmp_path)
    assert {e.id for e in db.all_edges(dbp)} == edges_after
    assert _snapshot(dbp, "node_history") == history_after


def test_reexport_resolved_validates_survive_rebuilds(tmp_path: Path) -> None:
    """A validates link resolved through a package re-export is intended, so reconciling never deletes it."""
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(tmp_path / "pkg" / "__init__.py", "from pkg.impl import func\n")
    _write(tmp_path / "pkg" / "impl.py", "def func():\n    return 1\n")
    test_file = _write(tmp_path / "tests" / "test_pkg.py", "from pkg import func\n\n\ndef test_func():\n    func()\n")
    dbp = _db_path(str(tmp_path))
    test_id = "proj::tests.test_pkg::test_func"

    for _ in range(3):
        build_index(dbp, tmp_path)
        assert _validates_from(dbp, test_id) == {"proj::pkg.impl::func"}
        time.sleep(0.02)
        test_file.write_text(test_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
