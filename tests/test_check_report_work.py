"""What ``check`` and the dependency walks cost, and that the cheaper paths answer the same.

``axiom-graph check`` loads its whole report (statuses, vias, annotation
findings) through one api entry on one connection, and its query count does
not grow with the number of LINKED_STALE rows it lists.  A verification that
records a pair against a module walks the module's ``composes`` tree a level
at a time, so its query count does not grow with the module's size.  Work is
counted, not timed; stored rows are compared with ``check --full``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow
from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.index import db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import (
    build_index,
    compute_check_summary,
    load_check_report,
    mark_clean_nodes,
    read_annotation_findings,
)
from tests.fixtures.full_recompute import assert_matches_full_recompute

_FN = "proj::src.mod::f"
_MODULE = "proj::src.big"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _doc(root: Path, sections: list[dict], *, tags: list[str] | None = None, name: str = "spec") -> None:
    _write(
        root / "docs" / f"{name}.json", json.dumps({"title": name.title(), "tags": tags or [], "sections": sections})
    )


def _section(sid: str, *links: str) -> dict:
    return {"id": sid, "heading": sid.upper(), "content": f"{sid} text.", "links": [{"node_id": n} for n in links]}


class _Work:
    """Counts connections opened and SELECTs run while installed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.connections = 0
        self.selects = 0
        real_connect = sqlite3.connect

        def counting_connect(*args, **kwargs):
            self.connections += 1
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(self._count)
            return conn

        monkeypatch.setattr(sqlite3, "connect", counting_connect)

    def _count(self, sql: str) -> None:
        self.selects += sql.lstrip().upper().startswith("SELECT")


def _stale_sections_project(root: Path, sections: int) -> Path:
    """*sections* sections document ``f``; all verified, then ``f`` changes, so each is LINKED_STALE."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    _write(root / "src" / "mod.py", "def f():\n    return 1\n")
    _doc(root, [_section(f"s{i}", _FN) for i in range(sections)])
    dbp = _db_path(str(root))
    build_index(dbp, root)
    mark_clean_nodes(dbp, root, [f"proj::docs/spec::s{i}" for i in range(sections)], "ok", verified_by="human")
    time.sleep(0.05)
    _write(root / "src" / "mod.py", "def f():\n    return 2\n")
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    return dbp


def _cli_check_work(root: Path, sections: int, monkeypatch: pytest.MonkeyPatch) -> tuple[_Work, str]:
    _stale_sections_project(root, sections)
    with monkeypatch.context() as m:
        work = _Work(m)
        result = CliRunner().invoke(cli, ["check", str(root)])
    assert result.exit_code == 0, result.output
    assert result.output.count("LINKED_STALE  via") == sections, result.output
    return work, result.output


@workflow(
    purpose="The check command loads its report on one connection, and lists 5 or 50 LINKED_STALE sections with "
    "their vias in the same number of queries"
)
def test_cli_check_opens_one_connection_whatever_its_linked_stale_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    口 = Step(step_num=1, name="Five stale sections", purpose="Count the CLI check's connections and queries")
    five, _ = _cli_check_work(tmp_path / "five", 5, monkeypatch)
    口 = Step(step_num=2, name="Fifty stale sections", purpose="The same work, ten times the rows")
    fifty, _ = _cli_check_work(tmp_path / "fifty", 50, monkeypatch)
    口 = Step(step_num=3, name="Compare", purpose="One connection each; the query count does not grow with the rows")
    assert five.connections == 1, five.connections
    assert fifty.connections == 1, fifty.connections
    assert fifty.selects == five.selects, (five.selects, fifty.selects)


@pytest.mark.parametrize("every_node", [False, True])
def test_the_check_report_equals_what_the_separate_reads_return(tmp_path: Path, every_node: bool) -> None:
    dbp = _stale_sections_project(tmp_path, 3)
    report = load_check_report(dbp, tmp_path, every_node=every_node)
    separate = compute_check_summary(dbp, tmp_path)
    findings = read_annotation_findings(dbp, tmp_path, observed=getattr(separate.refresh, "observed", None))
    if every_node:
        assert report.summary.statuses == separate.statuses
        assert report.summary.ordered_ids == separate.ordered_ids
    assert report.summary.problem_statuses == separate.problem_statuses
    assert report.summary.summary_line() == separate.summary_line()
    assert report.findings_error is None
    assert report.findings.findings == findings.findings
    assert_matches_full_recompute(dbp, tmp_path)


def test_check_json_output_lists_the_statuses_the_separate_reads_return(tmp_path: Path) -> None:
    dbp = _stale_sections_project(tmp_path, 3)
    result = CliRunner().invoke(cli, ["check", str(tmp_path), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    separate = compute_check_summary(dbp, tmp_path)
    assert list(payload["statuses"]) == list(separate.statuses)
    for nid, (own, link, via) in separate.statuses.items():
        assert payload["statuses"][nid]["own_status"] == own
        assert payload["statuses"][nid]["link_status"] == link
        assert payload["statuses"][nid].get("linked_via", []) == (via if link == "LINKED_STALE" else [])


def _module_mark_clean_work(root: Path, functions: int, monkeypatch: pytest.MonkeyPatch) -> _Work:
    """mark_clean of a section linking a module of *functions* functions and a two-method class."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    body = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(functions))
    body += "class C:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n"
    _write(root / "src" / "big.py", body)
    _doc(root, [_section("m", _MODULE)])
    dbp = _db_path(str(root))
    build_index(dbp, root)
    with monkeypatch.context() as m:
        work = _Work(m)
        result = mark_clean_nodes(dbp, root, ["proj::docs/spec::m"], "ok", verified_by="human")
    assert result.marked == ["proj::docs/spec::m"]
    assert_matches_full_recompute(dbp, root)
    return work


@workflow(
    purpose="Verifying a section that links a module reads the module's members a walk level at a time: the same "
    "number of queries for 3 functions as for 30"
)
def test_mark_clean_of_a_module_link_reads_its_members_by_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    口 = Step(step_num=1, name="Small module", purpose="Verify a section linking a module of 3 functions")
    small = _module_mark_clean_work(tmp_path / "small", 3, monkeypatch)
    口 = Step(step_num=2, name="Large module", purpose="Verify a section linking a module of 30 functions")
    large = _module_mark_clean_work(tmp_path / "large", 30, monkeypatch)
    口 = Step(step_num=3, name="Compare", purpose="The query count follows the tree's depth, not its size")
    assert large.selects == small.selects, (small.selects, large.selects)
    assert large.connections == small.connections == 1


def test_renaming_a_function_leaves_the_steps_of_a_sibling_its_name_matches_alone(tmp_path: Path) -> None:
    """``_`` in an id is a character, not a wildcard: renaming ``do_it`` leaves ``doxit``'s steps alone."""
    dbp = tmp_path / "graph.db"
    db.init_db(dbp)
    conn = sqlite3.connect(dbp)
    conn.row_factory = sqlite3.Row
    ids = ["m::do_it", "m::do_it::step-1", "m::doxit", "m::doxit::step-1", "m::Do_It::step-1"]
    required = [r[1] for r in conn.execute("PRAGMA table_info(nodes)") if r[3] and r[4] is None and r[1] != "id"]
    for nid in ids:
        values = {col: "atomic_process" if col == "node_type" else "m.py" for col in required}
        conn.execute(
            f"INSERT INTO nodes (id, {', '.join(values)}) VALUES (?{', ?' * len(values)})", (nid, *values.values())
        )
    db.record_code_rename_conn(conn, "m::do_it", "m::done", "m.py")
    remaining = {r[0] for r in conn.execute("SELECT id FROM nodes")}
    renamed = {(r[0], r[1]) for r in conn.execute("SELECT old_id, new_id FROM node_renames")}
    conn.close()
    assert remaining == {"m::do_it", "m::done::step-1", "m::doxit", "m::doxit::step-1", "m::Do_It::step-1"}
    assert renamed == {("m::do_it", "m::done"), ("m::do_it::step-1", "m::done::step-1")}


class _Recorder:
    """Stands in for a connection, recording what it is asked to run."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.calls.append((sql, params))
        return self.conn.execute(sql, params)


def test_the_frozen_doc_section_read_plans_no_scan_of_nodes(tmp_path: Path) -> None:
    _write(
        tmp_path / "axiom-graph.toml",
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.staleness]\nfrozen_tags = ["frozen"]\n',
    )
    _write(tmp_path / "src" / "__init__.py", "")
    _write(tmp_path / "src" / "mod.py", "def f():\n    return 1\n")
    _doc(tmp_path, [_section(f"s{i}", _FN) for i in range(3)], tags=["frozen"], name="old")
    _doc(tmp_path, [_section("t", _FN)], name="live")
    dbp = _db_path(str(tmp_path))
    build_index(dbp, tmp_path)
    frozen = db.get_doc_ids_with_tags(dbp, ["frozen"])
    assert frozen == {"proj::docs/old"}
    with db._connect(dbp) as conn:
        recorder = _Recorder(conn)
        rows = db.get_section_statuses_under_docs_conn(recorder, frozen)
        ((sql, params),) = recorder.calls
        plan = [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    assert set(rows) == {f"proj::docs/old::s{i}" for i in range(3)}
    assert not [p for p in plan if p.startswith("SCAN") and "json_each" not in p and not p.startswith("SCAN d")], plan
