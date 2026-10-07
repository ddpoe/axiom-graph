"""Behavioural tests for the effective change time of a linked node.

A node's change only counts against its tests and doc sections while it
still stands: a hash that flips and then returns to the baseline it was
measured against (a stale hashing seam, a branch switch and back, an edit
then revert) is no change at all.  A change that a verification accepted
at a different hash keeps counting.  Scenarios enter through
``axiom_graph.lifecycle.api``; the primitive's rule is covered directly
at the bottom.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.db.history import _fold_effective_change, get_effective_change_rows
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes

CODE_ID = "proj::src.mod::foo"
TEST_ID = "proj::tests.test_mod::test_foo"
SECTION_ID = "proj::docs/spec::overview"

_TEST_SRC = "from src.mod import foo\n\n\ndef test_foo():\n    assert foo() is not None\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _set_foo(root: Path, value: int) -> None:
    time.sleep(0.02)
    _write(root / "src" / "mod.py", f"def foo():\n    return {value}\n")


def _persisted(root: Path, node_id: str) -> tuple[str, str]:
    with sqlite3.connect(_db_path(str(root))) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    assert row is not None, node_id
    return row[0], row[1]


def _history_types(root: Path, node_id: str) -> list[str]:
    with sqlite3.connect(_db_path(str(root))) as conn:
        rows = conn.execute("SELECT change_type FROM node_history WHERE node_id = ? ORDER BY id", (node_id,)).fetchall()
    return [r[0] for r in rows]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A function, a test that calls it and a doc section that links it, all VERIFIED."""
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def foo():\n    return 0\n")
    _write(tmp_path / "tests" / "test_mod.py", _TEST_SRC)
    _write(
        tmp_path / "docs" / "spec.json",
        json.dumps(
            {
                "title": "Spec",
                "sections": [
                    {"id": "overview", "heading": "Overview", "content": "Foo.", "links": [{"node_id": CODE_ID}]}
                ],
            }
        ),
    )
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    mark_clean_nodes(dbp, tmp_path, [TEST_ID, SECTION_ID], reason="reviewed", verified_by="human")
    assert _persisted(tmp_path, TEST_ID) == ("VERIFIED", "VERIFIED")
    assert _persisted(tmp_path, SECTION_ID)[1] == "VERIFIED"
    return tmp_path


@pytest.mark.parametrize(
    "check_between",
    [
        pytest.param(False, id="no-check-between"),
        pytest.param(True, id="check-between-flags-then-clears"),
    ],
)
@workflow(
    purpose="A function whose hash disagrees with its baseline for one build and agrees again on the next leaves its "
    "test and doc section VERIFIED after the following check, and is never named as an offender; the build that "
    "sees the hash disagree flags them LINKED_STALE (a check in between agrees), and the realign alone clears that "
    "without any verification"
)
def test_hash_round_trip_leaves_dependents_verified(
    project: Path, monkeypatch: pytest.MonkeyPatch, check_between: bool
) -> None:
    from axiom_graph.index import file_state
    from axiom_graph.scanners import node_hashing

    dbp = _db_path(str(project))
    real = node_hashing.current_node_hashes_for_file

    口 = Step(
        step_num=1,
        name="Build while the hashing seam disagrees",
        purpose="One build computes a different hash for the unchanged function, so it is recorded CONTENT_UPDATED",
    )

    def _disagreeing(abs_path, loc_nodes, project_root, **kwargs):
        hashes = real(abs_path, loc_nodes, project_root, **kwargs)
        if CODE_ID in hashes:
            code, desc = hashes[CODE_ID]
            hashes[CODE_ID] = (f"x{code}", desc)
        return hashes

    # Every file takes the per-node hash ladder: no file ever matches the fingerprint its last re-hash read.
    fingerprints = itertools.count()
    monkeypatch.setattr(file_state, "file_fingerprint", lambda _path: f"unmatched-{next(fingerprints)}")
    monkeypatch.setattr(node_hashing, "current_node_hashes_for_file", _disagreeing)
    build_index(dbp, project)
    assert _persisted(project, CODE_ID)[0] == "CONTENT_UPDATED"
    for dependent in (TEST_ID, SECTION_ID):
        assert _persisted(project, dependent)[1] == "LINKED_STALE", dependent
    if check_between:
        flagged = compute_check_summary(dbp, project)
        assert flagged is not None
        for dependent in (TEST_ID, SECTION_ID):
            _own, link, via = flagged.statuses[dependent]
            assert link == "LINKED_STALE" and via == [CODE_ID], dependent
            assert _persisted(project, dependent)[1] == "LINKED_STALE", dependent

    口 = Step(
        step_num=2,
        name="Build once the seam agrees again",
        purpose="The function's hash is back at its baseline; the realigning build itself must not flag dependents",
    )
    monkeypatch.setattr(node_hashing, "current_node_hashes_for_file", real)
    build_index(dbp, project)

    口 = Step(
        step_num=3,
        name="Assert dependents and the offender list",
        purpose="Persisted link status is VERIFIED after the realigning build and the following check, the function "
        "is no offender, and the history keeps the LINKED_STALE the disagreeing build recorded",
    )
    for dependent in (TEST_ID, SECTION_ID):
        assert _persisted(project, dependent)[1] == "VERIFIED", dependent
    assert _persisted(project, CODE_ID)[0] == "VERIFIED"
    summary = compute_check_summary(dbp, project)
    assert summary is not None
    for dependent in (TEST_ID, SECTION_ID):
        _own, link, via = summary.statuses[dependent]
        assert link == "VERIFIED" and CODE_ID not in via, dependent
        assert _persisted(project, dependent)[1] == "VERIFIED", dependent
        assert "BECAME_LINKED_STALE" in _history_types(project, dependent), dependent
    assert get_effective_change_rows(dbp, [CODE_ID]) == {}


def _edit_sticks(root: Path, dbp: Path) -> None:
    _set_foo(root, 1)
    build_index(dbp, root)
    build_index(dbp, root)


def _edit_then_revert(root: Path, dbp: Path) -> None:
    _set_foo(root, 1)
    build_index(dbp, root)
    _set_foo(root, 0)
    build_index(dbp, root)


def _edit_mark_clean_then_revert(root: Path, dbp: Path) -> None:
    _set_foo(root, 1)
    build_index(dbp, root)
    mark_clean_nodes(dbp, root, [CODE_ID], reason="reviewed the edit", verified_by="human")
    _set_foo(root, 0)
    build_index(dbp, root)


def _edit_revert_before_build_then_mark_clean(root: Path, dbp: Path) -> None:
    _set_foo(root, 1)
    build_index(dbp, root)
    _set_foo(root, 0)
    mark_clean_nodes(dbp, root, [CODE_ID], reason="reverted", verified_by="human")
    build_index(dbp, root)


@pytest.mark.parametrize(
    ("scenario", "expect_stale"),
    [
        pytest.param(_edit_sticks, True, id="edit-sticks"),
        pytest.param(_edit_then_revert, False, id="edit-then-revert"),
        pytest.param(_edit_mark_clean_then_revert, False, id="mark-clean-at-new-hash-then-revert"),
        pytest.param(_edit_revert_before_build_then_mark_clean, False, id="revert-seen-first-by-mark-clean"),
    ],
)
@workflow(
    purpose="Only net change counts: an edit that sticks flags the test and doc section via the function; an edit "
    "reverted back to the baseline does not; an edit accepted by mark_clean and then reverted to the version the "
    "test and section were checked against does not either, since what they recorded matches the code again"
)
def test_only_net_change_flags_dependents(project: Path, scenario, expect_stale: bool) -> None:
    dbp = _db_path(str(project))
    scenario(project, dbp)
    summary = compute_check_summary(dbp, project)
    assert summary is not None
    for dependent in (TEST_ID, SECTION_ID):
        _own, link, via = summary.statuses[dependent]
        assert _persisted(project, dependent)[1] == link
        if expect_stale:
            assert link == "LINKED_STALE" and via == [CODE_ID], dependent
        else:
            assert link == "VERIFIED", dependent


# ---------------------------------------------------------------------------
# The rule itself (pure)
# ---------------------------------------------------------------------------

_WIDTH = frozenset({"CONTENT_ONLY", "CONTENT_AND_DESC", "BECAME_CONTENT_UPDATED"})


def _rows(*specs: tuple[str, str | None]) -> list[dict]:
    return [
        {"id": i, "change_type": ct, "scanned_at": f"t{i:02d}", "meta": meta} for i, (ct, meta) in enumerate(specs, 1)
    ]


_REALIGNED = json.dumps({"realigned": True})


@pytest.mark.parametrize(
    ("rows", "realigned_now", "expected"),
    [
        pytest.param(_rows(("BECAME_CONTENT_UPDATED", None)), False, (1, "t01"), id="open-change-counts"),
        pytest.param(
            _rows(("BECAME_CONTENT_UPDATED", None), ("BECAME_VERIFIED", _REALIGNED)), False, None, id="realign-cancels"
        ),
        pytest.param(
            _rows(("BECAME_CONTENT_UPDATED", None), ("BECAME_VERIFIED", None)),
            False,
            (1, "t01"),
            id="legacy-unflagged-verified-still-counts",
        ),
        pytest.param(_rows(("BECAME_CONTENT_UPDATED", None)), True, None, id="realigned-this-pass"),
        pytest.param(
            _rows(
                ("BECAME_CONTENT_UPDATED", None),
                ("AGENT_VERIFIED", None),
                ("BECAME_CONTENT_UPDATED", None),
                ("BECAME_VERIFIED", _REALIGNED),
            ),
            False,
            (1, "t01"),
            id="cancelled-round-trip-falls-back-to-accepted-change",
        ),
        pytest.param(
            _rows(("CONTENT_ONLY", None), ("BECAME_VERIFIED", _REALIGNED)),
            False,
            (1, "t01"),
            id="upsert-change-moves-baseline-and-counts",
        ),
        pytest.param(
            _rows(("BECAME_CONTENT_UPDATED", None), ("INITIAL", None), ("BECAME_VERIFIED", _REALIGNED)),
            False,
            (1, "t01"),
            id="baseline-rewrite-commits-open-change",
        ),
    ],
)
def test_effective_change_rule(rows: list[dict], realigned_now: bool, expected) -> None:
    """The fold returns the latest change that still counts, or ``None``."""
    assert _fold_effective_change(rows, _WIDTH, realigned_now) == expected
