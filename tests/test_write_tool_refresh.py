"""Write tools: the work they do, and the statuses they leave behind.

The write tools scale with what they touch, not with the size of the index:
one connection per call, one parse per file, no whole-graph load.  These
tests count that work rather than time it.  Every index writer either
refreshes the statuses it can move, or leaves the next incremental ``check``
what it needs to; either way the stored rows then equal ``check --full``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import pytest

from axiom_graph.db import _core
from axiom_graph.db.migrations import run_migrations
from axiom_graph.docjson.api import (
    axiom_graph_accept_doc_edits,
    axiom_graph_add_link,
    axiom_graph_delete_doc,
    axiom_graph_delete_link,
    axiom_graph_delete_section,
    axiom_graph_update_doc_meta,
    axiom_graph_update_section,
)
from axiom_graph.index import db, dependency_set, staleness
from axiom_graph.index import mark_clean as mark_clean_module
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.index.refresh import refresh_staleness
from axiom_graph.lifecycle import api as lifecycle_api
from axiom_graph.lifecycle.api import (
    apply_rename,
    build_index,
    checkout_db,
    compute_check_summary,
    mark_clean_nodes,
    purge_nodes,
    reverify_nodes,
    revert_rename,
)
from tests.fixtures.full_recompute import assert_matches_full_recompute

N = 6
DOC_ID = "proj::docs/spec"
PARENT_ID = f"{DOC_ID}::parent"
CHILD_ID = f"{DOC_ID}::parent.child"
_LOOKUP_STRUCTURES = {"idx_nodes_location", "idx_edges_to", "idx_edges_from", "file_state"}


def _fn(i: int) -> str:
    return f"proj::src.m{i}::f{i}"


def _section(i: int) -> str:
    return f"{DOC_ID}::s{i}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _edit(path: Path, old: str, new: str) -> None:
    time.sleep(0.02)
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _build_and_check(root: Path) -> None:
    dbp = _db_path(str(root))
    build_index(dbp, root)
    compute_check_summary(dbp, root)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """N one-function modules and a doc whose sections link them (one under a parent section); built and checked."""
    _write(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(tmp_path / "src" / "__init__.py", "")
    for i in range(N):
        _write(tmp_path / "src" / f"m{i}.py", f"def f{i}():\n    return {i}\n")
    sections: list[dict] = [
        {"id": f"s{i}", "heading": f"S{i}", "content": f"Documents f{i}.", "links": [{"node_id": _fn(i)}]}
        for i in range(N)
    ]
    sections.append(
        {
            "id": "parent",
            "heading": "Parent",
            "content": "Parent.",
            "sections": [{"id": "child", "heading": "Child", "content": "Child.", "links": [{"node_id": _fn(0)}]}],
        }
    )
    _write(tmp_path / "docs" / "spec.json", json.dumps({"title": "Spec", "sections": sections}))
    _build_and_check(tmp_path)
    return tmp_path


@contextmanager
def _counting_connections(monkeypatch: pytest.MonkeyPatch):
    """Count every SQLite connection opened inside the block."""
    opened = {"n": 0}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        opened["n"] += 1
        return real(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", counting)
        yield opened


# ---------------------------------------------------------------------------
# Work counts (Tier 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("refresh", [False, True])
def test_mark_clean_opens_one_connection_whatever_the_batch_size(
    project: Path, monkeypatch: pytest.MonkeyPatch, refresh: bool
) -> None:
    """One connection per call, closing refresh included, for one node and for a batch with a parent."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    _build_and_check(project)
    batch = [_section(i) for i in range(N)] + [_fn(i) for i in range(N)] + [PARENT_ID]
    counts = []
    for node_ids in ([_section(1)], batch):
        with _counting_connections(monkeypatch) as opened:
            mark_clean_nodes(dbp, project, node_ids, reason="reviewed", verified_by="human", refresh=refresh)
        counts.append(opened["n"])
    assert counts == [1, 1]


def test_reverify_opens_one_connection(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reverify (sources, cascade, closing check) runs on one connection."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    _build_and_check(project)
    with _counting_connections(monkeypatch) as opened:
        reverify_nodes(dbp, project, [_fn(0)], reason="reviewed", verified_by="human")
    assert opened["n"] == 1


@pytest.mark.parametrize("mode", ["incremental", "cone", "full"])
def test_refresh_staleness_opens_one_connection(project: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """The refresh (discovery walk, cone or full pass) runs on one connection."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    build_index(dbp, project)
    _edit(project / "src" / "m0.py", "return 10", "return 11")
    with _counting_connections(monkeypatch) as opened:
        result = refresh_staleness(
            dbp,
            project,
            full=mode == "full",
            discover=mode != "cone",
            seed_node_ids=[_section(0)] if mode == "cone" else (),
        )
    assert result.mode == mode
    assert opened["n"] == 1


def test_mark_clean_parses_each_file_once(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch of 2N nodes over N+1 files parses N+1 files: own hashes and pair targets share one parse."""
    dbp = _db_path(str(project))
    recorders: list = []
    real_init = mark_clean_module.PairRecorder.__init__

    def capturing_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        recorders.append(self)

    monkeypatch.setattr(mark_clean_module.PairRecorder, "__init__", capturing_init)
    batch = [_section(i) for i in range(N)] + [_fn(i) for i in range(N)]
    mark_clean_nodes(dbp, project, batch, reason="reviewed", verified_by="human", refresh=False)
    assert len(recorders) == 1
    assert recorders[0].files_parsed == N + 1


def test_mark_clean_and_reverify_load_no_whole_graph(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoped write paths read the neighbourhood they touch, never the whole graph or every pair."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    _build_and_check(project)

    def whole_graph(*_args, **_kwargs):
        raise AssertionError("whole-graph load on a scoped write path")

    monkeypatch.setattr(dependency_set, "load_dependency_graph", whole_graph)
    monkeypatch.setattr(staleness, "load_dependency_graph", whole_graph)
    monkeypatch.setattr(staleness, "_composes_children_map", whole_graph)
    monkeypatch.setattr(staleness.db, "get_all_verification_targets_conn", whole_graph)
    mark_clean_nodes(dbp, project, [PARENT_ID, _section(1)], reason="reviewed", verified_by="human")
    reverify_nodes(dbp, project, [_fn(0)], reason="reviewed", verified_by="human")


@pytest.mark.parametrize("entry", ["build", "check", "mark_clean"])
def test_an_upgraded_index_gains_the_lookup_indexes_on_its_first_operation(project: Path, entry: str) -> None:
    """A v4 index brought to v5 by the migrations gets the lookup indexes and file records at its first build, check or mark_clean."""
    dbp = _db_path(str(project))
    with db._connect(dbp) as conn:
        for name in ("idx_nodes_location", "idx_edges_to", "idx_edges_from"):
            conn.execute(f"DROP INDEX {name}")
        # A v4 index never has the v5-only scan indexes; one filters on a column dropped below.
        for name in _core._SCAN_INDEX_NAMES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("DROP TABLE file_state")
        conn.execute("DROP TABLE node_verification_targets")
        conn.execute("ALTER TABLE nodes DROP COLUMN live_code_hash")
        conn.execute("ALTER TABLE nodes DROP COLUMN live_desc_hash")
        conn.execute("PRAGMA user_version = 4")
    run_migrations(dbp)
    with db._connect(dbp) as conn:
        before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "idx_nodes_location" not in before

    if entry == "build":
        build_index(dbp, project)
    elif entry == "check":
        compute_check_summary(dbp, project)
    else:
        mark_clean_nodes(dbp, project, [_section(0)], reason="reviewed", verified_by="human")

    with db._connect(dbp) as conn:
        after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert after >= _LOOKUP_STRUCTURES


# ---------------------------------------------------------------------------
# Writer audit (Tier 1): every index writer leaves rows check --full agrees with
# ---------------------------------------------------------------------------


def _rename_f4(root: Path) -> None:
    _write(
        root / "src" / "m4.py",
        "def h4():\n    total = 0\n    for k in range(3):\n        total += k\n    return total\n",
    )
    _build_and_check(root)
    result = apply_rename(_db_path(str(root)), root, _fn(4), "proj::src.m4::h4")
    assert result.applied, result.reason


def _purge_m5(root: Path) -> None:
    _write(
        root / "src" / "m5.py",
        "def other5():\n    total = 0\n    for k in range(7):\n        total += k * 3\n    return total\n",
    )
    _build_and_check(root)
    results = purge_nodes(_db_path(str(root)), root, [_fn(5)], reason="removed", actor="human")
    assert all(r.purged for r in results), results


def _revert_rename_f4(root: Path) -> None:
    _rename_f4(root)
    result = revert_rename(_db_path(str(root)), root, "proj::src.m4::h4")
    assert result.reverted, result.reason


def _revert_rename_f4_with_a_dependent(root: Path) -> None:
    """Revert a rename whose new id a second section has come to document."""
    _rename_f4(root)
    reply = axiom_graph_add_link(str(root), section_id=_section(5), node_id="proj::src.m4::h4")
    assert not reply.startswith("ERROR"), reply
    _build_and_check(root)
    result = revert_rename(_db_path(str(root)), root, "proj::src.m4::h4")
    assert result.reverted, result.reason


def _accept_raw_edit_s1(root: Path) -> None:
    """Edit section s1 by hand, build, then accept the raw edit."""
    _edit(root / "docs" / "spec.json", "Documents f1.", "Documents f1, edited by hand.")
    build_index(_db_path(str(root)), root)
    reply = axiom_graph_accept_doc_edits(str(root), section_ids=[_section(1)], verified_by="human")
    assert not reply.startswith("ERROR"), reply


# writer -> (the write, whether it refreshes the statuses it moves itself)
_WRITERS: dict[str, tuple[Callable[[Path], object], bool]] = {
    "accept_doc_edits": (_accept_raw_edit_s1, True),
    "add_link": (lambda r: axiom_graph_add_link(str(r), section_id=_section(1), node_id=_fn(0)), True),
    "delete_link": (lambda r: axiom_graph_delete_link(str(r), _section(0), node_id=_fn(0)), True),
    "update_section": (lambda r: axiom_graph_update_section(str(r), CHILD_ID, content="Edited."), True),
    "delete_section": (lambda r: axiom_graph_delete_section(str(r), _section(0)), True),
    "update_doc_meta": (lambda r: axiom_graph_update_doc_meta(str(r), DOC_ID, tags=["spec"]), True),
    "mark_clean": (
        lambda r: mark_clean_nodes(_db_path(str(r)), r, [_section(0)], reason="reviewed", verified_by="human"),
        True,
    ),
    "reverify": (
        lambda r: reverify_nodes(_db_path(str(r)), r, [_fn(0)], reason="reviewed", verified_by="human"),
        True,
    ),
    "delete_doc": (lambda r: axiom_graph_delete_doc(str(r), DOC_ID), False),
    "purge_node": (_purge_m5, False),
    "apply_rename": (_rename_f4, False),
    "revert_rename": (_revert_rename_f4, False),
    "revert_rename_with_a_dependent": (_revert_rename_f4_with_a_dependent, False),
}


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_index_writer_leaves_rows_check_full_agrees_with(project: Path, writer: str) -> None:
    """After any writer, the stored rows equal check --full: at once for a self-refreshing writer, else after an incremental check."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    _build_and_check(project)
    write, refreshes = _WRITERS[writer]
    write(project)
    if refreshes:
        assert_matches_full_recompute(dbp, project)
    compute_check_summary(dbp, project)
    assert_matches_full_recompute(dbp, project)


def test_a_checked_out_index_reaches_the_engine_through_discovery(project: Path, tmp_path_factory) -> None:
    """A copied index keeps its watermark and stamp; the copy's first check re-hashes the files that differ there."""
    dbp = _db_path(str(project))
    _edit(project / "src" / "m0.py", "return 0", "return 10")
    _build_and_check(project)
    worktree = tmp_path_factory.mktemp("worktree")
    shutil.copytree(project, worktree, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".axiom_graph"))
    _edit(worktree / "src" / "m1.py", "return 1", "return 11")
    result = checkout_db(dbp, worktree)
    assert result.copied
    copy = _db_path(str(worktree))
    summary = compute_check_summary(copy, worktree)
    assert summary is not None
    assert_matches_full_recompute(copy, worktree)


# ---------------------------------------------------------------------------
# Renames are all-or-nothing (Tier 1)
# ---------------------------------------------------------------------------


_INDEX_TABLES = {
    "nodes": "SELECT id, own_status, link_status FROM nodes",
    "node_history": "SELECT id, node_id, change_type, meta FROM node_history",
    "node_verification": "SELECT node_id, verified_at, verified_by FROM node_verification",
    "node_verification_targets": "SELECT node_id, target_id, code_hash, desc_hash FROM node_verification_targets",
    "edges": "SELECT from_id, to_id, edge_type FROM edges",
    "node_renames": "SELECT old_id, new_id FROM node_renames",
}


def _index_rows(dbp: Path) -> dict[str, list[tuple]]:
    with db._connect(dbp) as conn:
        return {table: sorted(tuple(r) for r in conn.execute(sql)) for table, sql in _INDEX_TABLES.items()}


class _Injected(RuntimeError):
    pass


def _rename_f4_on_disk(root: Path) -> None:
    _write(
        root / "src" / "m4.py",
        "def h4():\n    total = 0\n    for k in range(3):\n        total += k\n    return total\n",
    )
    _build_and_check(root)


def test_apply_rename_failing_part_way_applies_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the history, verification and edges have moved leaves the index and the docs untouched."""
    dbp = _db_path(str(project))
    _rename_f4_on_disk(project)
    before = _index_rows(dbp)
    doc_before = (project / "docs" / "spec.json").read_text(encoding="utf-8")
    real = lifecycle_api._force_renamed_status_conn

    def write_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise _Injected

    monkeypatch.setattr(lifecycle_api, "_force_renamed_status_conn", write_then_fail)
    with pytest.raises(_Injected):
        apply_rename(dbp, project, _fn(4), "proj::src.m4::h4")
    assert _index_rows(dbp) == before
    assert (project / "docs" / "spec.json").read_text(encoding="utf-8") == doc_before

    monkeypatch.undo()
    assert apply_rename(dbp, project, _fn(4), "proj::src.m4::h4").applied
    assert ("proj::src.m4::f4", "proj::src.m4::h4") in _index_rows(dbp)["node_renames"]


def test_revert_rename_failing_part_way_applies_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the migrate-back has run leaves the applied rename, its index rows and the docs as they were."""
    dbp = _db_path(str(project))
    _rename_f4(project)
    before = _index_rows(dbp)
    doc_before = (project / "docs" / "spec.json").read_text(encoding="utf-8")
    real = lifecycle_api.db.record_code_rename_conn

    def migrate_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise _Injected

    monkeypatch.setattr(lifecycle_api.db, "record_code_rename_conn", migrate_then_fail)
    with pytest.raises(_Injected):
        revert_rename(dbp, project, "proj::src.m4::h4")
    assert _index_rows(dbp) == before
    assert (project / "docs" / "spec.json").read_text(encoding="utf-8") == doc_before

    monkeypatch.undo()
    assert revert_rename(dbp, project, "proj::src.m4::h4").reverted
    assert _index_rows(dbp)["node_renames"] == []


# ---------------------------------------------------------------------------
# The operation's connection keeps every block's commit boundary (Tier 1)
# ---------------------------------------------------------------------------


def _meta(dbp: Path, key: str) -> str | None:
    peek = sqlite3.connect(dbp)
    try:
        row = peek.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    finally:
        peek.close()
    return row[0] if row else None


def test_a_block_in_an_operation_commits_at_its_own_end(project: Path) -> None:
    """Inside an operation, a block's write is visible to another connection as soon as the block ends."""
    dbp = _db_path(str(project))
    with db.operation_connection(dbp) as op:
        with db._connect(dbp) as conn:
            assert conn is op
            db.set_index_meta_conn(conn, "scope-test", "1")
        assert _meta(dbp, "scope-test") == "1"
        assert not op.in_transaction


def test_a_block_opened_over_uncommitted_writes_gets_its_own_connection(project: Path) -> None:
    """A block opened while the operation's connection holds uncommitted writes neither sees nor commits them."""
    dbp = _db_path(str(project))
    with pytest.raises(_Injected):
        with db.operation_connection(dbp):
            with db._connect(dbp) as outer:
                db.set_index_meta_conn(outer, "scope-test", "uncommitted")
                with db._connect(dbp) as inner:
                    assert inner is not outer
                    assert db.get_index_meta_conn(inner, "scope-test") is None
                raise _Injected
    assert _meta(dbp, "scope-test") is None


class _SelectCount:
    """Counts the SELECTs run on every connection opened while installed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.selects = 0
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(self._count)
            return conn

        monkeypatch.setattr(sqlite3, "connect", counting_connect)

    def _count(self, sql: str) -> None:
        self.selects += sql.lstrip().upper().startswith("SELECT")


def _stale_envelopes_check_work(root: Path, envelopes: int, monkeypatch: pytest.MonkeyPatch) -> int:
    """Make *envelopes* workflows that delegate to one task LINKED_STALE, edit the task again, count the check."""
    flows = [
        'from axiom_annotations import AutoStep, task, workflow\n\n\n@task(purpose="Do it")\ndef t():\n    return 1\n'
    ]
    for i in range(envelopes):
        flows.append(
            f'\n\n@workflow(purpose="Flow {i}")\ndef w{i}():\n    口 = AutoStep(step_num=1, name="Do")\n    return t()\n'
        )
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    _write(root / "src" / "flows.py", "".join(flows))
    dbp = _db_path(str(root))
    build_index(dbp, root)
    _edit(root / "src" / "flows.py", "return 1", "return 2")
    _build_and_check(root)
    _edit(root / "src" / "flows.py", "return 2", "return 3")
    with monkeypatch.context() as m:
        work = _SelectCount(m)
        compute_check_summary(dbp, root)
    with db._connect(dbp) as conn:
        stale = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE node_type = 'composite_process' AND subtype = 'workflow' "
            "AND link_status = 'LINKED_STALE'"
        ).fetchone()[0]
    assert stale == envelopes, stale
    assert_matches_full_recompute(dbp, root)
    return work.selects


def test_check_reads_the_same_whatever_its_linked_stale_envelope_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An incremental check over 5 or 50 LINKED_STALE workflow envelopes runs the same number of queries."""
    five = _stale_envelopes_check_work(tmp_path / "five", 5, monkeypatch)
    fifty = _stale_envelopes_check_work(tmp_path / "fifty", 50, monkeypatch)
    assert fifty == five, (five, fifty)
