"""A refresh that stops part-way: the next plain ``check`` still stores what ``check --full`` stores.

A pass commits its own-phase write (live hashes, file fingerprints, own
transitions) before its link phase runs.  These tests stop a refresh at each
point between those writes and the refresh's last one, then run a plain
check and compare every stored status and live hash with a full recompute.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.index import db, refresh, staleness
from axiom_graph.lifecycle.api import build_index, compute_check_summary, mark_clean_nodes
from tests.fixtures.full_recompute import assert_matches_full_recompute

_TOML = '[axiom_graph]\nproject_id = "demo"\n'
_F_ID = "demo::pkg.mod::f"
_SECTION_ID = "demo::docs/guide::f"


class _Stop(Exception):
    """The injected failure."""


def _project(root: Path) -> Path:
    """A doc section documents ``f`` in ``pkg/mod.py``; one unrelated module."""
    (root / "axiom-graph.toml").write_text(_TOML, encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "other.py").write_text("def h():\n    return 0\n", encoding="utf-8")
    docs_dir = root / "docs"
    docs_dir.mkdir()
    doc = {
        "title": "Guide",
        "tags": [],
        "sections": [{"id": "f", "heading": "f", "content": "f returns one.", "links": [{"node_id": _F_ID}]}],
    }
    (docs_dir / "guide.docjson").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    result = CliRunner().invoke(cli, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root / ".axiom_graph" / "graph.db"


def _edit(root: Path, value: int) -> None:
    (root / "pkg" / "mod.py").write_text(f"def f():\n    return {value}\n", encoding="utf-8")


def _settled_on_b(root: Path) -> Path:
    """f edited A -> B and built, the section verified against f=B, two checks."""
    db_path = _project(root)
    _edit(root, 2)
    build_index(db_path, root)
    mark_clean_nodes(db_path, root, [_SECTION_ID], "reviewed", verified_by="agent")
    compute_check_summary(db_path, root)
    compute_check_summary(db_path, root)
    return db_path


def _link_status(db_path: Path, node_id: str) -> str:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()[0]


def _open_passes(db_path: Path) -> list:
    with db._connect(db_path) as conn:
        return db.read_open_passes_conn(conn)


def _raise(*_args, **_kwargs):
    raise _Stop


def _stop_after_link_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the recorded pass finish, then stop before the refresh's watermark write."""
    real = refresh.record_staleness_pass

    def _then_stop(*args, **kwargs):
        real(*args, **kwargs)
        raise _Stop

    monkeypatch.setattr(refresh, "record_staleness_pass", _then_stop)


def _stop_after_own_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staleness, "_link_phase", _raise)


def _stop_inside_link_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(staleness, "_get_linked_stale_ids", _raise)


@workflow(
    purpose="A check that stops inside its link phase after storing the edited file's fingerprint leaves the next "
    "plain check recomputing the doc section that file's edit moves: the section reads LINKED_STALE, as check "
    "--full stores it"
)
def test_check_stopped_in_its_link_phase_is_recovered_by_the_next_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    口 = Step(step_num=1, name="Verified against B", purpose="edit f to B, build, verify the section, check twice")
    db_path = _settled_on_b(tmp_path)
    assert _link_status(db_path, _SECTION_ID) == "VERIFIED"

    口 = Step(step_num=2, name="Edit to C, check stops", purpose="the check raises inside its link phase")
    _edit(tmp_path, 3)
    with monkeypatch.context() as m:
        _stop_inside_link_phase(m)
        with pytest.raises(_Stop):
            compute_check_summary(db_path, tmp_path)
    assert len(_open_passes(db_path)) == 1

    口 = Step(step_num=3, name="Next check", purpose="recovers the stopped pass and stores what check --full stores")
    cs = compute_check_summary(db_path, tmp_path)
    assert cs.refresh.mode == "incremental"
    assert _link_status(db_path, _SECTION_ID) == "LINKED_STALE"
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


@pytest.mark.parametrize(
    "stop",
    [_stop_after_own_write, _stop_inside_link_phase, _stop_after_link_write],
    ids=["after-own-write", "inside-link-phase", "after-link-write"],
)
def test_check_stopped_at_any_point_leaves_the_next_check_matching_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop
) -> None:
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)
    with monkeypatch.context() as m:
        stop(m)
        with pytest.raises(_Stop):
            compute_check_summary(db_path, tmp_path)
    compute_check_summary(db_path, tmp_path)
    assert _link_status(db_path, _SECTION_ID) == "LINKED_STALE"
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


def test_full_check_stopped_part_way_makes_the_next_check_run_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)
    with monkeypatch.context() as m:
        _stop_after_own_write(m)
        with pytest.raises(_Stop):
            compute_check_summary(db_path, tmp_path, full=True)
    assert [p.full for p in _open_passes(db_path)] == [True]
    assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


def test_build_stopped_between_its_passes_leaves_the_next_check_matching_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)
    with monkeypatch.context() as m:
        m.setattr(db, "restamp_verifications", _raise)
        with pytest.raises(_Stop):
            build_index(db_path, tmp_path)
    compute_check_summary(db_path, tmp_path)
    assert _link_status(db_path, _SECTION_ID) == "LINKED_STALE"
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


def test_write_tool_refresh_stopped_in_its_link_phase_leaves_the_next_check_matching_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _settled_on_b(tmp_path)
    # Edited without a check: the write tool's cone is the first to re-hash it.
    _edit(tmp_path, 3)
    with monkeypatch.context() as m:
        _stop_inside_link_phase(m)
        with pytest.raises(_Stop):
            refresh.refresh_after_write(db_path, tmp_path, [_SECTION_ID])
    assert len(_open_passes(db_path)) == 1
    compute_check_summary(db_path, tmp_path)
    assert _link_status(db_path, _SECTION_ID) == "LINKED_STALE"
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


def test_open_pass_entry_rides_on_writes_the_pass_makes_anyway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The entry is recorded and removed inside transactions that already wrote, so no commit is added."""
    db_path = _settled_on_b(tmp_path)
    calls: list[tuple[str, bool]] = []
    for name in ("open_pass_conn", "close_passes_conn"):
        real = getattr(db, name)

        def _spy(conn, *args, _real=real, _name=name, **kwargs):
            calls.append((_name, conn.in_transaction))
            return _real(conn, *args, **kwargs)

        monkeypatch.setattr(db, name, _spy)

    _edit(tmp_path, 3)
    compute_check_summary(db_path, tmp_path)
    compute_check_summary(db_path, tmp_path, full=True)
    assert _open_passes(db_path) == []

    # A write tool's cone re-hashes the moved file and carries its seeds in
    # the one carried entry, which stays whatever its link write writes; the
    # next check re-checks those seeds and removes it in its closing write.
    _edit(tmp_path, 4)
    refresh.refresh_after_write(db_path, tmp_path, [_SECTION_ID])
    assert len(_open_passes(db_path)) == 1
    compute_check_summary(db_path, tmp_path)
    assert _open_passes(db_path) == []
    assert [name for name, _ in calls] == ["open_pass_conn", "close_passes_conn"] * 2 + [
        "open_pass_conn",
        "close_passes_conn",
    ]
    assert all(in_txn for _name, in_txn in calls), calls
    assert_matches_full_recompute(db_path, tmp_path)


def test_idle_and_unconsuming_refreshes_record_no_open_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _settled_on_b(tmp_path)
    opened: list[str] = []
    real = db.open_pass_conn
    monkeypatch.setattr(db, "open_pass_conn", lambda conn, token, **kw: (opened.append(token), real(conn, token, **kw)))
    compute_check_summary(db_path, tmp_path)
    mark_clean_nodes(db_path, tmp_path, [_F_ID], "reviewed", verified_by="agent")
    assert opened == []


def test_refresh_logs_its_mode_and_a_closing_line(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)
    with caplog.at_level(logging.INFO, logger="axiom_graph.index.refresh"):
        compute_check_summary(db_path, tmp_path)
        compute_check_summary(db_path, tmp_path, full=True)
    lines = [r.getMessage() for r in caplog.records if r.name == "axiom_graph.index.refresh"]
    assert any(m.startswith("staleness refresh: incremental (") for m in lines), lines
    assert any(m.startswith("staleness refresh: full (requested)") for m in lines), lines
    assert any(m.startswith("staleness refresh: incremental done in ") and "re-hashed" in m for m in lines), lines
    assert sum(" done in " in m for m in lines) == 2


@workflow(
    purpose="Twenty write-tool refreshes that each finish leave one carried entry, sized by the distinct nodes they "
    "refreshed rather than by the number of writes; the next check re-checks those nodes, says so, reports no "
    "stopped pass, and stores what check --full stores"
)
def test_finished_cones_carry_one_entry_to_the_next_check(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)
    refresh.refresh_after_write(db_path, tmp_path, [_SECTION_ID])
    first = _open_passes(db_path)
    assert len(first) == 1 and first[0].carried and not first[0].full
    for value in range(4, 24):
        _edit(tmp_path, value)
        refresh.refresh_after_write(db_path, tmp_path, [_SECTION_ID])
    entries = _open_passes(db_path)
    assert len(entries) == 1
    assert entries[0].carried and not entries[0].full
    assert entries[0].ids == first[0].ids

    with caplog.at_level(logging.INFO, logger="axiom_graph.index.refresh"):
        compute_check_summary(db_path, tmp_path)
    lines = [r.getMessage() for r in caplog.records if r.name == "axiom_graph.index.refresh"]
    assert any(f"re-checking {len(first[0].ids)} node(s) tools refreshed since the last check" in m for m in lines)
    assert not any("stopped part-way" in m for m in lines), lines
    assert _open_passes(db_path) == []
    assert _link_status(db_path, _SECTION_ID) == "LINKED_STALE"
    assert_matches_full_recompute(db_path, tmp_path)


def test_carried_entry_merges_distinct_nodes_and_turns_full_past_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cones merge into one entry under a new key; a stopped pass keeps its own entry; past the cap it turns full."""
    db_path = _project(tmp_path)
    # Init's own doc write (the seeded policy doc) may already have left a
    # carried entry; its nodes merge in like any other cone's.
    already = {i for e in _open_passes(db_path) if e.carried for i in e.ids}
    with db._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        db.open_pass_conn(conn, "cone-1", full=False, ids={"x", "y"}, carried=True)
        db.open_pass_conn(conn, "stopped", full=False, ids={"q"})
        db.open_pass_conn(conn, "cone-2", full=False, ids={"y", "z"}, carried=True)
    entries = {e.token: e for e in _open_passes(db_path)}
    assert set(entries) == {"stopped", "cone-2"}
    assert entries["cone-2"].carried and entries["cone-2"].ids == {"x", "y", "z"} | already
    assert not entries["stopped"].carried and entries["stopped"].ids == {"q"}

    # A refresh that read the entry before the second cone merged into it
    # removes nothing of it.
    with db._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        db.close_passes_conn(conn, ["cone-1"])
    assert {e.token for e in _open_passes(db_path)} == {"stopped", "cone-2"}

    monkeypatch.setattr("axiom_graph.db.files.CARRIED_PASS_MAX_IDS", 3)
    with db._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        db.open_pass_conn(conn, "cone-3", full=False, ids={"w"}, carried=True)
    carried = [e for e in _open_passes(db_path) if e.carried]
    assert [(e.token, e.full, e.ids) for e in carried] == [("cone-3", True, frozenset())]


def test_recovery_with_no_node_left_removes_its_entries_holding_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entries are read and rewritten inside a write transaction, so one recorded meanwhile is not lost."""
    db_path = _settled_on_b(tmp_path)
    with db._connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        db.open_pass_conn(conn, "gone", full=False, ids={"demo::pkg.gone::g"}, carried=True)
    seen: list[bool] = []
    real = db.close_passes_conn

    def _spy(conn, tokens):
        seen.append(conn.in_transaction)
        return real(conn, tokens)

    monkeypatch.setattr(db, "close_passes_conn", _spy)
    cs = compute_check_summary(db_path, tmp_path)
    assert cs.refresh.mode == "idle"
    assert seen == [True]
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)


def test_failed_envelope_pass_makes_the_next_check_run_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The check that swallows the envelope failure finishes; the next one runs in full and matches a full recompute."""
    db_path = _settled_on_b(tmp_path)
    _edit(tmp_path, 3)

    def _walk_fails(*_args, **_kwargs):
        raise RuntimeError("closure walk failed")

    with monkeypatch.context() as m, caplog.at_level(logging.WARNING, logger="axiom_graph.index.staleness"):
        m.setattr(staleness, "warm_delegates_closures", _walk_fails)
        compute_check_summary(db_path, tmp_path)
    warnings = [r.getMessage() for r in caplog.records if r.name == "axiom_graph.index.staleness"]
    assert any("the next check re-runs every node in full" in w for w in warnings), warnings
    assert [(p.full, p.reason) for p in _open_passes(db_path)] == [(True, db.OPEN_PASS_ENVELOPE_FAILED)]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="axiom_graph.index.refresh"):
        assert compute_check_summary(db_path, tmp_path).refresh.mode == "full"
    lines = [r.getMessage() for r in caplog.records if r.name == "axiom_graph.index.refresh"]
    assert "staleness refresh: full (an earlier pass's envelope check failed)" in lines, lines
    assert not any("stopped part-way" in m for m in lines), lines
    assert _open_passes(db_path) == []
    assert_matches_full_recompute(db_path, tmp_path)
