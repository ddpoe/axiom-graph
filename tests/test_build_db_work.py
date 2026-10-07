"""Database work of build and check: connections, history reads, no-op writes.

Work counts, never wall clock: the number of connections an operation opens,
the SQLite VM steps a history read takes, and the write statements an
operation issues when nothing changed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from axiom_annotations import workflow

from axiom_graph.config import db_path_for
from axiom_graph.db import _core
from axiom_graph.db.edges import upsert_edge_conn, upsert_edges_conn
from axiom_graph.models import AxiomEdge
from axiom_graph.db.docs import distinct_locations_conn, get_section_doc_id_map, get_section_statuses_under_docs_conn
from axiom_graph.db.files import deleted_node_ids_since_conn, max_deletion_log_id_conn, read_journal_conn
from axiom_graph.db.nodes import get_liveness_rows_conn
from axiom_graph.db.staleness import unflagged_dangling_sources_conn
from axiom_graph.index import annotation_findings, builder
from axiom_graph.index.staleness import count_broken_link_sources, find_broken_links
from axiom_graph.scanners import module_scanner
from axiom_graph.lifecycle.api import build_index, compute_check_summary
from tests.fixtures import whole_read_oracles
from tests.fixtures.full_recompute import assert_matches_full_recompute

_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _project(root: Path) -> Path:
    """Write a small project: two modules, a workflow, a test and a doc linking a function."""
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "proj"\n', encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "from axiom_annotations import AutoStep, workflow\n\n\n"
        "def f():\n    return 1\n\n\n"
        "def g():\n    return f() + 1\n\n\n"
        '@workflow(purpose="Run f")\n'
        "def run():\n"
        '    口 = AutoStep(step_num=1, name="Call f")\n'
        "    return f()\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_mod.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    docs = root / "docs"
    docs.mkdir()
    (docs / "spec.docjson").write_text(
        json.dumps(
            {
                "title": "Spec",
                "tags": [],
                "sections": [
                    {"id": "s", "heading": "S", "content": "About f.", "links": [{"node_id": "proj::pkg.mod::f"}]}
                ],
            }
        ),
        encoding="utf-8",
    )
    return db_path_for(root)


def _settled(root: Path) -> Path:
    """Build the project until a build parses nothing, then check once."""
    db_path = _project(root)
    for _ in range(3):
        if build_index(db_path, root).files_scanned == 0:
            break
    compute_check_summary(db_path, root)
    return db_path


@contextmanager
def _counting(monkeypatch: pytest.MonkeyPatch):
    """Count the connections opened and record every statement run on them."""
    seen: dict = {"connections": 0, "statements": []}
    real = sqlite3.connect

    def counting(*args, **kwargs):
        seen["connections"] += 1
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen["statements"].append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting)
    yield seen
    monkeypatch.setattr(sqlite3, "connect", real)


def _writes(statements: list[str]) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith(_WRITE_PREFIXES)]


def _vm_steps(conn: sqlite3.Connection, run) -> int:
    """Return how many SQLite VM steps *run(conn)* takes."""
    steps = {"n": 0}

    def tick() -> int:
        steps["n"] += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        run(conn)
    finally:
        conn.set_progress_handler(None, 1)
    return steps["n"]


def _history_db(path: Path, older_rows: int) -> sqlite3.Connection:
    """An index whose history holds *older_rows* rows, then a mark, then three new rows."""
    _core.init_db(path)
    conn = sqlite3.connect(path)
    rows = [(f"proj::m::n{i}", "2026-01-01", "CONTENT_ONLY" if i % 3 else "DELETED") for i in range(older_rows)]
    conn.executemany("INSERT INTO node_history (node_id, scanned_at, change_type) VALUES (?, ?, ?)", rows)
    conn.execute("INSERT INTO node_history (node_id, scanned_at, change_type) VALUES ('mark', 'x', 'CHECKPOINT')")
    conn.executemany(
        "INSERT INTO node_history (node_id, scanned_at, change_type) VALUES (?, ?, ?)",
        [("proj::m::a", "y", "DELETED"), ("proj::m::a", "y", "DELETED"), ("proj::m::b", "y", "CONTENT_ONLY")],
    )
    conn.commit()
    return conn


def test_history_reads_past_a_mark_do_not_grow_with_older_history(tmp_path: Path) -> None:
    """The deleted-ids and journal reads take the same VM steps over 10 or 3,000 older history rows."""
    counts = {}
    for older in (10, 3000):
        conn = _history_db(tmp_path / f"h{older}.db", older)
        mark = conn.execute("SELECT id FROM node_history WHERE node_id = 'mark'").fetchone()[0]
        assert deleted_node_ids_since_conn(conn, mark) == {"proj::m::a"}
        assert read_journal_conn(conn, mark) == ({"proj::m::a", "proj::m::b"}, mark + 3)
        counts[older] = (
            _vm_steps(conn, lambda c, m=mark: deleted_node_ids_since_conn(c, m)),
            _vm_steps(conn, lambda c, m=mark: read_journal_conn(c, m)),
        )
        conn.close()
    assert counts[10] == counts[3000]


@workflow(purpose="A build that finds nothing changed runs on one connection and leaves the findings store unwritten")
def test_a_no_change_build_opens_one_connection_and_rewrites_no_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _settled(tmp_path)
    before = build_index(db_path, tmp_path)
    with _counting(monkeypatch) as seen:
        after = build_index(db_path, tmp_path)
    assert after.files_scanned == 0
    assert seen["connections"] == 1
    assert not [s for s in _writes(seen["statements"]) if "annotation_findings" in s]
    assert after.annotation_findings == before.annotation_findings
    assert (after.annotation_findings_new, after.annotation_findings_resolved) == (0, 0)
    assert_matches_full_recompute(db_path, tmp_path)


@workflow(purpose="An idle check runs on one connection and writes nothing")
def test_an_idle_check_opens_one_connection_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _settled(tmp_path)
    with _counting(monkeypatch) as seen:
        summary = compute_check_summary(db_path, tmp_path)
    assert summary.refresh.mode == "idle"
    assert seen["connections"] == 1
    assert _writes(seen["statements"]) == []
    assert_matches_full_recompute(db_path, tmp_path)


@workflow(purpose="An edit build still runs on one connection and stores what a full recompute stores")
def test_an_edit_build_opens_one_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _settled(tmp_path)
    mod = tmp_path / "pkg" / "mod.py"
    mod.write_text(mod.read_text(encoding="utf-8").replace("return 1", "return 2"), encoding="utf-8")
    with _counting(monkeypatch) as seen:
        summary = build_index(db_path, tmp_path)
    assert summary.files_scanned == 1
    assert seen["connections"] == 1
    assert_matches_full_recompute(db_path, tmp_path)


def _raw_node(conn: sqlite3.Connection, node_id: str, location: str = "m.py", **cols: str) -> None:
    """Insert a bare ``nodes`` row (an ``atomic_process`` unless *cols* say otherwise)."""
    row = {
        "id": node_id,
        "node_type": "atomic_process",
        "title": node_id,
        "location": location,
        "source": "python",
        "code_hash": "h",
        "level_0": node_id,
        "level_1": node_id,
        "updated_at": "2026-01-01",
        **cols,
    }
    names = ", ".join(row)
    conn.execute(f"INSERT INTO nodes ({names}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _raw_edge(conn: sqlite3.Connection, edge_type: str, from_id: str, to_id: str) -> None:
    conn.execute(
        "INSERT INTO edges (id, edge_type, from_id, to_id) VALUES (?, ?, ?, ?)",
        (f"{edge_type}:{from_id}->{to_id}", edge_type, from_id, to_id),
    )


def _index(path: Path) -> sqlite3.Connection:
    _core.init_db(path)
    return sqlite3.connect(path)


@pytest.mark.parametrize("removal", ["delete", "re-key"])
def test_dangling_sources_since_the_mark_match_the_full_scan(tmp_path: Path, removal: str) -> None:
    """A link left dangling by any row removal after the mark is found from the deletion log alone."""
    conn = _index(tmp_path / "g.db")
    _raw_node(conn, "p::docs/a::s", subtype="docjson_section")
    _raw_node(conn, "p::m::t")
    _raw_edge(conn, "documents", "p::docs/a::s", "p::m::t")
    mark = max_deletion_log_id_conn(conn)
    if removal == "delete":
        conn.execute("DELETE FROM nodes WHERE id = 'p::m::t'")
    else:
        conn.execute("UPDATE nodes SET id = 'p::m::t2' WHERE id = 'p::m::t'")
    last = max_deletion_log_id_conn(conn)
    assert last > mark
    assert (
        unflagged_dangling_sources_conn(conn, mark, last) == unflagged_dangling_sources_conn(conn) == {"p::docs/a::s"}
    )
    assert unflagged_dangling_sources_conn(conn, last, last) == set()


def test_the_dangling_lookup_does_not_grow_with_unrelated_links(tmp_path: Path) -> None:
    """Reading the deletions since the mark takes the same VM steps beside 10 or 2,000 intact links."""
    counts = {}
    for links in (10, 2000):
        conn = _index(tmp_path / f"d{links}.db")
        _raw_node(conn, "p::m::t")
        for i in range(links):
            _raw_node(conn, f"p::docs/a::s{i}", subtype="docjson_section")
            _raw_edge(conn, "documents", f"p::docs/a::s{i}", "p::m::t")
        _raw_node(conn, "p::m::gone")
        _raw_edge(conn, "documents", "p::docs/a::s0", "p::m::gone")
        mark = max_deletion_log_id_conn(conn)
        conn.execute("DELETE FROM nodes WHERE id = 'p::m::gone'")
        last = max_deletion_log_id_conn(conn)
        assert unflagged_dangling_sources_conn(conn, mark, last) == {"p::docs/a::s0"}
        counts[links] = _vm_steps(conn, lambda c, m=mark, x=last: unflagged_dangling_sources_conn(c, m, x))
        conn.close()
    assert counts[10] == counts[2000]


def test_scoped_index_reads_match_their_whole_table_queries(tmp_path: Path) -> None:
    """Frozen-doc sections, file locations and the broken-link count equal what the whole-table queries return."""
    path = tmp_path / "s.db"
    conn = _index(path)
    for sid in ("p::docs/a::s", "p::docs/a::s.t", "p::docs/a2::s", "p::docs/b::s"):
        _raw_node(conn, sid, location=sid.split("::")[1] + ".docjson", subtype="docjson_section", source="docjson")
    _raw_node(conn, "p::docs/a::other", location="docs/a.docjson", subtype="docjson_doc", source="docjson")
    _raw_node(conn, "p::ent", location="b.py", node_type="entity")
    _raw_node(conn, "p::pkg", location="c.py", subtype="external_package")
    _raw_node(conn, "p::blank", location="")
    _raw_node(conn, "p::w", location="w.py", subtype="workflow")
    _raw_node(conn, "p::w::step-1", location="w.py", subtype="step")
    _raw_edge(conn, "composes", "p::w", "p::w::step-1")
    _raw_edge(conn, "delegates_to", "p::w::step-1", "p::missing1")
    _raw_edge(conn, "documents", "p::docs/a::s", "p::missing2")
    _raw_edge(conn, "documents", "p::docs/a::s", "p::missing3")
    _raw_edge(conn, "validates", "p::blank", "p::missing4")
    conn.commit()
    frozen = {"p::docs/a", "p::docs/none"}
    expected = {sid for sid, doc in get_section_doc_id_map(path).items() if doc in frozen}
    assert set(get_section_statuses_under_docs_conn(conn, frozen)) == expected == {"p::docs/a::s", "p::docs/a::s.t"}
    every = sorted(r[0] for r in conn.execute("SELECT DISTINCT location FROM nodes"))
    assert distinct_locations_conn(conn) == every
    assert distinct_locations_conn(conn, tracked_only=True) == [loc for loc in every if loc not in ("", "b.py", "c.py")]
    assert count_broken_link_sources(path) == len(find_broken_links(path)) == 3
    conn.close()


@workflow(purpose="An idle check reads no node or link table in full beyond the one status-pair count")
def test_an_idle_check_plans_no_full_scan_of_nodes_or_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = _settled(tmp_path)
    with _counting(monkeypatch) as seen:
        assert compute_check_summary(db_path, tmp_path).refresh.mode == "idle"
    conn = sqlite3.connect(db_path)
    scans = []
    for sql in dict.fromkeys(seen["statements"]):
        head = sql.lstrip().upper()
        if not head.startswith(("SELECT", "WITH")) or not any(t in sql for t in (" nodes", " edges")):
            continue
        if head.startswith("SELECT COUNT(*) FROM NODES"):
            continue
        for line in (r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)):
            # Allowed: the doc-id list and deletion-log rows driving a lookup
            # (``d``), the loose location scan (``locs``), the per-file and
            # per-doc tables, the partial index of reset rows, and the one
            # covering status-pair count.
            if not line.startswith("SCAN ") or line == "SCAN d" or line.startswith(("SCAN d ", "SCAN locs")):
                continue
            allowed = ("idx_nodes_status_pair", "idx_nodes_reset")
            if not any(a in line for a in allowed) and not line.startswith(("SCAN file_state", "SCAN docs")):
                scans.append((line, sql[:120]))
    conn.close()
    assert scans == []


@workflow(purpose="A node row removed with no journal row still flags its linking section at the next check")
def test_a_node_row_removed_without_a_journal_row_is_flagged_by_the_next_check(tmp_path: Path) -> None:
    db_path = _settled(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM nodes WHERE id = 'proj::pkg.mod::f'")
    conn.commit()
    conn.close()
    summary = compute_check_summary(db_path, tmp_path)
    assert summary.link_counts["BROKEN_LINK"] >= 1
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT link_status FROM nodes WHERE id = 'proj::docs/spec::s'").fetchone()[0]
    conn.close()
    assert status == "BROKEN_LINK"
    assert_matches_full_recompute(db_path, tmp_path)


_FLOW = (
    "from axiom_annotations import AutoStep, workflow\n\n\n"
    "def target():\n    return 1\n\n\n"
    '@workflow(purpose="Run the target")\n'
    "def flow():\n"
    '    口 = AutoStep(step_num=1, name="Call target")\n'
    "    return target()\n"
)


@workflow(purpose="A delegate target removed with no journal row flags the composing workflow at the next check")
@pytest.mark.parametrize("deletion_mark", ["stored", "not stored yet"])
def test_a_removed_delegate_target_flags_its_workflow_at_the_next_check(tmp_path: Path, deletion_mark: str) -> None:
    db_path = _project(tmp_path)
    (tmp_path / "pkg" / "flow.py").write_text(_FLOW, encoding="utf-8")
    for _ in range(3):
        if build_index(db_path, tmp_path).files_scanned == 0:
            break
    compute_check_summary(db_path, tmp_path)
    target = "proj::pkg.flow::target"
    conn = sqlite3.connect(db_path)
    envelopes = {
        r[0]
        for r in conn.execute(
            "SELECT c.from_id FROM edges c JOIN edges d ON d.from_id = c.to_id "
            "WHERE c.edge_type = 'composes' AND d.edge_type = 'delegates_to' AND d.to_id = ?",
            (target,),
        )
    }
    assert len(envelopes) == 1
    assert not conn.execute(
        "SELECT 1 FROM edges WHERE to_id = ? AND edge_type IN ('documents', 'validates', 'annotates')", (target,)
    ).fetchone()
    conn.execute("DELETE FROM nodes WHERE id = ?", (target,))
    if deletion_mark == "not stored yet":
        conn.execute("DELETE FROM index_meta WHERE key = 'staleness_deletion_mark'")
    conn.commit()
    conn.close()
    compute_check_summary(db_path, tmp_path)
    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (envelopes.pop(),)).fetchone()[0]
    conn.close()
    assert status == "BROKEN_LINK"
    assert_matches_full_recompute(db_path, tmp_path)


def test_dangling_sources_charge_a_step_link_as_the_broken_link_rule_does(tmp_path: Path) -> None:
    """A step's dangling delegate link names its envelope (or the step when nothing composes it), as find_broken_links does."""
    path = tmp_path / "w.db"
    conn = _index(path)
    _raw_node(conn, "p::m::t")
    _raw_node(conn, "p::w", location="w.py", subtype="workflow")
    _raw_node(conn, "p::w::step-1", location="w.py", subtype="autostep")
    _raw_edge(conn, "composes", "p::w", "p::w::step-1")
    _raw_edge(conn, "delegates_to", "p::w::step-1", "p::m::t")
    _raw_node(conn, "p::x::step-1", location="x.py", subtype="step")
    _raw_edge(conn, "delegates_to", "p::x::step-1", "p::m::t")
    _raw_node(conn, "p::e", location="e.py", subtype="task")
    _raw_edge(conn, "annotates", "p::e", "p::m::t")
    mark = max_deletion_log_id_conn(conn)
    conn.execute("DELETE FROM nodes WHERE id = 'p::m::t'")
    conn.commit()
    last = max_deletion_log_id_conn(conn)
    expected = set(find_broken_links(path)) | {"p::e"}
    assert expected == {"p::w", "p::x::step-1", "p::e"}
    assert unflagged_dangling_sources_conn(conn, mark, last) == unflagged_dangling_sources_conn(conn) == expected
    conn.execute("UPDATE nodes SET link_status = 'BROKEN_LINK' WHERE id IN ('p::w', 'p::x::step-1')")
    assert unflagged_dangling_sources_conn(conn, mark, last) == unflagged_dangling_sources_conn(conn) == {"p::e"}
    conn.close()


def test_the_batched_edge_upsert_writes_what_one_at_a_time_writes(tmp_path: Path) -> None:
    """Edges upserted together store the same rows, in the same order, as one upsert per edge, and count new ids once."""
    edges = [
        AxiomEdge("a::documents::t1", "documents", "a", "t1", meta={"k": 1}),
        AxiomEdge("a::documents::t0", "documents", "a", "t0", weight=2.0),
        AxiomEdge("a::documents::t2", "documents", "a", "t2"),
        AxiomEdge("a::documents::t2", "documents", "a", "t2", weight=3.0),
    ]
    stored, new = [], []
    for name in ("each", "batch"):
        conn = _index(tmp_path / f"{name}.db")
        _raw_edge(conn, "documents", "a", "t0")
        conn.execute("UPDATE edges SET id = 'a::documents::t0'")
        if name == "each":
            new.append(sum(upsert_edge_conn(conn, e) for e in edges))
        else:
            new.append(upsert_edges_conn(conn, edges))
        stored.append(conn.execute("SELECT rowid, * FROM edges ORDER BY rowid").fetchall())
        conn.close()
    assert stored[0] == stored[1]
    assert new == [2, 2]


def _live_index(path: Path, root: Path) -> sqlite3.Connection:
    """An index covering each case of the live-node rule; ``kept.py`` and ``parsed.py`` exist, ``gone.py`` does not."""
    (root / "kept.py").write_text("", encoding="utf-8")
    (root / "parsed.py").write_text("", encoding="utf-8")
    conn = _index(path)
    _raw_node(conn, "p::kept::a", location="kept.py")
    _raw_node(conn, "p::kept::lost", location="kept.py", own_status="NOT_FOUND")
    _raw_node(conn, "p::kept::a@workflow", location="kept.py", node_type="composite_process")
    _raw_node(conn, "p::gone::b", location="gone.py")
    _raw_node(conn, "p::parsed::old", location="parsed.py")
    _raw_node(conn, "p::ext", location="external", node_type="entity")
    _raw_node(conn, "p::blank", location="")
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def test_the_live_node_lookup_answers_as_the_whole_index_set(tmp_path: Path) -> None:
    """Scoped live-node answers equal the set built from every row, and keep the pre-build view of changed files."""
    path = tmp_path / "l.db"
    conn = _live_index(path, tmp_path)
    scanned = [SimpleNamespace(id="p::parsed::new", node_type="atomic_process", location="parsed.py")]
    walked = {"kept.py", "parsed.py"}
    rows = [tuple(r) for r in conn.execute("SELECT id, node_type, location, own_status FROM nodes")]
    whole = whole_read_oracles.live_node_types(tmp_path, scanned, rows, walked=walked)
    touched = {"parsed.py", "gone.py"}
    lookup = builder.LiveNodeLookup(
        path,
        tmp_path,
        scanned,
        walked=walked,
        snapshot=get_liveness_rows_conn(conn, node_ids=["p::parsed::new"], locations=touched),
        snapshot_ids=["p::parsed::new"],
        touched=touched,
    )
    # After the snapshot the build rewrites its changed files: a row lands in one, another leaves.
    _raw_node(conn, "p::parsed::added", location="parsed.py")
    conn.execute("DELETE FROM nodes WHERE id = 'p::parsed::old'")
    conn.commit()
    asked = [r[0] for r in rows] + ["p::parsed::new", "p::parsed::added", "p::nowhere"]
    with lookup.reading_on(conn):
        lookup.prefetch(asked[:3])
        assert {i: lookup.get(i) for i in asked} == {i: whole.get(i) for i in asked}
        assert [i for i in asked if i in lookup] == [i for i in asked if i in whole]
    conn.close()


def _depends_on(conn: sqlite3.Connection, src: str, dst: str, meta: dict | None) -> None:
    conn.execute(
        "INSERT INTO edges (id, edge_type, from_id, to_id, meta) VALUES (?, 'depends_on', ?, ?, ?)",
        (f"depends_on:{src}->{dst}", src, dst, json.dumps(meta) if meta is not None else None),
    )


def test_the_reexport_relation_read_per_module_matches_the_whole_read(tmp_path: Path) -> None:
    """Star and named re-exports read per module equal the relation read from every marker at once."""
    conn = _index(tmp_path / "r.db")
    conn.row_factory = sqlite3.Row
    star_key, names_key = module_scanner.REEXPORT_STAR_KEY, module_scanner.REEXPORT_NAMES_KEY
    _depends_on(conn, "p::pkg", "p::pkg.b", {star_key: True})
    _depends_on(conn, "p::pkg", "p::pkg.a", {star_key: True, names_key: {"x": "y"}})
    _depends_on(conn, "p::pkg.a", "p::pkg.c", {names_key: {"z": "z"}})
    _depends_on(conn, "p::other", "p::pkg", None)
    star, named = whole_read_oracles.read_reexport_relation(conn)
    reader = builder.ReexportRelationReader(conn)
    reader.prefetch(["p::pkg"])
    for module in ("p::pkg", "p::pkg.a", "p::other", "p::absent"):
        assert reader.star.get(module, ()) == star.get(module, ())
        assert reader.named.get(module, ()) == named.get(module, ())
    conn.close()


def test_b4_reads_do_not_grow_with_unrelated_nodes_and_links(tmp_path: Path) -> None:
    """Resolving B4 on the scoped live set and relation takes the same VM steps beside 10 or 2,000 other modules."""
    (tmp_path / "m.py").write_text("", encoding="utf-8")
    records = [
        {"module": "p.m", "function": "run", "line": 1, "step_num": 1, "target_name": name, "target_node_id": target}
        | {"has_next_call": True}
        for name, target in (("f", "p::m::f"), ("g", "p::pkg::g"), ("h", "p::m::h"))
    ]
    counts, results = {}, {}
    for others in (10, 2000):
        path = tmp_path / f"b{others}.db"
        conn = _index(path)
        _raw_node(conn, "p::m::f")
        _raw_node(conn, "p::m::f@workflow", node_type="composite_process")
        _raw_node(conn, "p::impl::g")
        _depends_on(conn, "p::pkg", "p::impl", {module_scanner.REEXPORT_STAR_KEY: True})
        for i in range(others):
            _raw_node(conn, f"p::o{i}::x")
            _depends_on(conn, f"p::o{i}", "p::impl", {module_scanner.REEXPORT_STAR_KEY: True})
        conn.commit()
        conn.row_factory = sqlite3.Row

        def resolve(c: sqlite3.Connection, p: Path = path) -> list[dict]:
            live = builder.LiveNodeLookup(p, tmp_path, [], walked={"m.py"})
            relation = builder.ReexportRelationReader(c)
            with live.reading_on(c):
                return annotation_findings.resolve_b4(records, live, relation.star, relation.named)

        results[others] = resolve(conn)
        counts[others] = _vm_steps(conn, resolve)
        conn.close()
    assert [f["message"] for f in results[10]] == [f["message"] for f in results[2000]]
    assert len(results[10]) == 2  # g resolves through the re-export to an undecorated node; h names nothing
    assert counts[10] == counts[2000]


@workflow(purpose="A no-change build reads no node or link table in full beyond the accepted per-file and status reads")
def test_a_no_change_build_plans_no_full_scan_of_nodes_or_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _settled(tmp_path)
    with _counting(monkeypatch) as seen:
        assert build_index(db_path, tmp_path).files_scanned == 0
    conn = sqlite3.connect(db_path)
    allowed_indexes = (
        "idx_nodes_status_pair",
        "idx_nodes_reset",
        "idx_nodes_file_mtime",
        "idx_nodes_level_2_length",
        "idx_nodes_doc_envelopes",
    )
    allowed_tables = ("SCAN file_state", "SCAN docs", "SCAN annotation_findings", "SCAN sqlite_master")
    scans = []
    for sql in dict.fromkeys(seen["statements"]):
        head = sql.lstrip().upper()
        if not head.startswith(("SELECT", "WITH")) or not any(t in sql for t in (" nodes", " edges")):
            continue
        for line in (r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)):
            if not line.startswith("SCAN ") or line == "SCAN d" or line.startswith(("SCAN d ", "SCAN locs")):
                continue
            if not any(a in line for a in allowed_indexes) and not line.startswith(allowed_tables):
                scans.append((line, sql[:120]))
    conn.close()
    assert scans == []
    assert_matches_full_recompute(db_path, tmp_path, ["proj::docs/spec::s", "proj::tests.test_mod::test_f"])
