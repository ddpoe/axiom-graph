"""Behavioural tests for baselining test functions at their first scan.

A test function that validates code with any past content change is
LINKED_STALE unless it carries a verification newer than that change.  A
test indexed for the first time is baseline-verified by the build, so it
starts VERIFIED; a later change to its target flags it as usual.  All
scenarios enter through ``axiom_graph.lifecycle.api``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from axiom_annotations import workflow

from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import (
    build_index,
    checkout_db,
    compute_check_summary,
    compute_report,
    mark_clean_nodes,
)

CODE_ID = "proj::src.mod::foo"
TEST_ID = "proj::tests.test_mod::test_foo"
OTHER_TEST_ID = "proj::tests.test_other::test_foo_again"

_TEST_SRC = "from src.mod import foo\n\n\ndef test_foo():\n    assert foo() is not None\n"
_OTHER_TEST_SRC = "from src.mod import foo\n\n\ndef test_foo_again():\n    assert foo() is not None\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project whose function ``foo`` already has a content-change history row."""
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    (tmp_path / "docs").mkdir()
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    _edit_foo(tmp_path, 1)
    build_index(dbp, tmp_path)
    assert db.get_latest_code_change_times(dbp, [CODE_ID]), "fixture: foo needs a content-change history row"
    return tmp_path


def _edit_foo(root: Path, value: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "mod.py", f"def foo():\n    return {value}\n")


def _status(root: Path, node_id: str) -> tuple[str, str, list[str]]:
    summary = compute_check_summary(_db_path(str(root)), root)
    assert summary is not None
    return summary.statuses[node_id]


def _persisted(root: Path, node_id: str) -> tuple[str, str]:
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0], row[1]


def _baseline_rows(root: Path, node_id: str) -> list[dict]:
    """Verification history rows written with op ``scan_baseline``."""
    rows = db.get_history(_db_path(str(root)), node_id, limit=100)
    return [
        r
        for r in rows
        if r["change_type"] == "AGENT_VERIFIED"
        and r["meta"]
        and json.loads(r["meta"]).get("verification_op") == "scan_baseline"
    ]


def _verification(root: Path, node_id: str) -> dict | None:
    return db.get_verification(_db_path(str(root)), node_id)


@workflow(
    purpose="A test first indexed after its target already changed comes out VERIFIED after the build and the next "
    "check, with exactly one scan_baseline verification event that report lists; a new non-test function is not "
    "baselined"
)
def test_new_test_for_already_changed_code_is_verified_and_reported(project: Path) -> None:
    dbp = _db_path(str(project))
    _write(project / "tests" / "test_mod.py", _TEST_SRC)
    _write(project / "src" / "other.py", "def bar():\n    return 1\n")

    result = build_index(dbp, project)

    assert result.tests_baselined == [TEST_ID]
    assert _persisted(project, TEST_ID) == ("VERIFIED", "VERIFIED")
    own, link, _via = _status(project, TEST_ID)
    assert (own, link) == ("VERIFIED", "VERIFIED")
    assert len(_baseline_rows(project, TEST_ID)) == 1
    assert _verification(project, TEST_ID)["verified_by"] == "agent:scan-baseline"
    assert _verification(project, "proj::src.other::bar") is None

    report = compute_report(dbp)
    assert TEST_ID in {r["node_id"] for r in report.verifications}


@workflow(
    purpose="A test added in the same build that first observes its target's edit is VERIFIED after that build and "
    "after the next check"
)
def test_new_test_landing_with_its_code_change_is_verified(project: Path) -> None:
    dbp = _db_path(str(project))
    # Verify foo so its next edit is recorded as a new content change by the build below.
    mark_clean_nodes(dbp, project, [CODE_ID], reason="reviewed", verified_by="human")
    changes_before = db.get_latest_code_change_times(dbp, [CODE_ID])
    _edit_foo(project, 2)
    _write(project / "tests" / "test_mod.py", _TEST_SRC)

    build_index(dbp, project)

    assert db.get_latest_code_change_times(dbp, [CODE_ID]) != changes_before, "the build must record foo's edit"
    assert _status(project, TEST_ID)[:2] == ("VERIFIED", "VERIFIED")
    assert _status(project, TEST_ID)[:2] == ("VERIFIED", "VERIFIED")
    assert len(_baseline_rows(project, TEST_ID)) == 1


@workflow(
    purpose="A later change to a baselined test's target flags it LINKED_STALE; the later build re-stamps only the "
    "tests it baselined itself; a real verification replaces the baseline provenance"
)
def test_later_target_change_restales_and_restamp_is_keyed_to_the_build(project: Path) -> None:
    dbp = _db_path(str(project))
    _write(project / "tests" / "test_mod.py", _TEST_SRC)
    build_index(dbp, project)
    first_stamp = _verification(project, TEST_ID)["verified_at"]
    # Verify foo so its next edit is recorded as a new content change.
    mark_clean_nodes(dbp, project, [CODE_ID], reason="reviewed", verified_by="human")

    _edit_foo(project, 3)
    _write(project / "tests" / "test_other.py", _OTHER_TEST_SRC)
    second = build_index(dbp, project)

    assert second.tests_baselined == [OTHER_TEST_ID]
    assert _verification(project, TEST_ID)["verified_at"] == first_stamp
    _own, link, via = _status(project, TEST_ID)
    assert link == "LINKED_STALE"
    assert CODE_ID in via
    assert _status(project, OTHER_TEST_ID)[1] == "VERIFIED"

    mark_clean_nodes(dbp, project, [TEST_ID], reason="re-ran the test", verified_by="human")
    assert _verification(project, TEST_ID)["verified_by"] == "human"
    assert _status(project, TEST_ID)[1] == "VERIFIED"


def _forget_verification(root: Path, node_id: str) -> None:
    """Make *node_id* look like a test indexed before tests were baselined."""
    with sqlite3.connect(_db_path(str(root))) as conn:
        conn.execute("DELETE FROM node_verification WHERE node_id = ?", (node_id,))
        conn.execute("DELETE FROM node_history WHERE node_id = ? AND change_type = 'AGENT_VERIFIED'", (node_id,))


@pytest.mark.parametrize("change", ["rebuild_unchanged", "edit_body", "move"])
@workflow(
    purpose="An existing never-verified LINKED_STALE test stays LINKED_STALE, with no baseline written, when it is "
    "rebuilt unchanged, its body is edited, or it is moved to another module and the rename is detected"
)
def test_existing_stale_test_is_never_baselined(project: Path, change: str) -> None:
    dbp = _db_path(str(project))
    _write(project / "tests" / "test_mod.py", _TEST_SRC)
    build_index(dbp, project)
    _forget_verification(project, TEST_ID)
    _edit_foo(project, 4)
    build_index(dbp, project)
    assert _status(project, TEST_ID)[1] == "LINKED_STALE"

    time.sleep(0.02)
    expected_id = TEST_ID
    if change == "rebuild_unchanged":
        (project / "tests" / "test_mod.py").touch()
    elif change == "edit_body":
        _write(project / "tests" / "test_mod.py", _TEST_SRC.replace("is not None", "is not None  # still"))
    else:
        (project / "tests" / "test_mod.py").rename(project / "tests" / "test_moved.py")
        expected_id = "proj::tests.test_moved::test_foo"

    result = build_index(dbp, project)

    if change == "move":
        with sqlite3.connect(dbp) as conn:
            renamed = conn.execute("SELECT 1 FROM node_renames WHERE old_id = ? AND new_id = ?", (TEST_ID, expected_id))
            assert renamed.fetchone(), "fixture: the rename must be detected"
    assert result.tests_baselined == []
    assert _verification(project, expected_id) is None
    assert _baseline_rows(project, expected_id) == []
    assert _status(project, expected_id)[1] == "LINKED_STALE"


@workflow(
    purpose="A copied index built in a second checkout with fresh mtimes baselines only the test that is new there"
)
def test_worktree_copy_baselines_only_its_new_test(project: Path, tmp_path_factory, monkeypatch) -> None:
    monkeypatch.setattr("axiom_graph.registry.upsert_registry", lambda _root: [])
    _write(project / "tests" / "test_mod.py", _TEST_SRC)
    build_index(_db_path(str(project)), project)

    worktree = tmp_path_factory.mktemp("worktree")
    for name in ("axiom-graph.toml", "src", "tests", "docs"):
        src = project / name
        if src.is_dir():
            shutil.copytree(src, worktree / name, copy_function=shutil.copy)
        else:
            shutil.copy(src, worktree / name)
    _write(worktree / "tests" / "test_other.py", _OTHER_TEST_SRC)
    assert checkout_db(_db_path(str(project)), worktree).copied

    result = build_index(_db_path(str(worktree)), worktree)

    assert result.tests_baselined == [OTHER_TEST_ID]
    assert len(_baseline_rows(worktree, TEST_ID)) == 1  # the one copied from the source index


@workflow(
    purpose="A build into an empty index writes no scan baseline for the tests it indexes; a later build still "
    "baselines a newly added test"
)
def test_first_build_writes_no_baselines(tmp_path: Path) -> None:
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    (tmp_path / "docs").mkdir()
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    _write(tmp_path / "tests" / "test_mod.py", _TEST_SRC)
    dbp = _db_path(str(tmp_path))

    first = build_index(dbp, tmp_path)

    assert first.tests_baselined == []
    assert _verification(tmp_path, TEST_ID) is None
    assert _baseline_rows(tmp_path, TEST_ID) == []
    assert _status(tmp_path, TEST_ID)[:2] == ("VERIFIED", "VERIFIED")

    _write(tmp_path / "tests" / "test_other.py", _OTHER_TEST_SRC)
    second = build_index(dbp, tmp_path)

    assert second.tests_baselined == [OTHER_TEST_ID]
    assert len(_baseline_rows(tmp_path, OTHER_TEST_ID)) == 1
