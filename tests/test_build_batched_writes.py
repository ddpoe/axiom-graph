"""The build's batched writes store what one write per item stored, with reads that do not grow per item."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom_graph.index import builder, db
from axiom_graph.index.paths import db_path as _db_path
from axiom_graph.lifecycle.api import build_index

_MOD = "def f():\n    return 1\n\n\ndef g():\n    return f() + 1\n"


def _project(root: Path, tests: int) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(_MOD, encoding="utf-8")
    (root / "pkg" / "use.py").write_text(
        "from pkg.mod import f, g\n\n\ndef h():\n    return f() + g()\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    body = "from pkg.mod import f\n" + "".join(f"\n\ndef test_{i}():\n    assert f() == 1\n" for i in range(tests))
    (root / "tests" / "test_many.py").write_text(body, encoding="utf-8")
    return root


def _edges(root: Path) -> list[tuple]:
    with db._connect(_db_path(str(root))) as conn:
        return [
            tuple(r)
            for r in conn.execute("SELECT rowid, id, edge_type, from_id, to_id, weight, meta FROM edges ORDER BY rowid")
        ]


def _per_edge(conn: sqlite3.Connection, edges) -> int:
    """The reference: one upsert, with its own existence read, per edge."""
    return sum(1 for edge in edges if db.upsert_edge_conn(conn, edge))


def _build_twice(root: Path) -> list[tuple]:
    first = builder.build(root)
    after_first = _edges(root)
    (root / "pkg" / "use.py").write_text("from pkg.mod import g\n\n\ndef h():\n    return g() * 2\n", encoding="utf-8")
    second = builder.build(root)
    counts = [(s["edges_written"], s["edges_skipped"]) for s in (first, second)]
    return [counts, after_first, _edges(root)]


def test_batched_edge_write_stores_what_one_upsert_per_edge_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows, rowids and the written / skipped counts match the per-edge write, on a first and an edit build."""
    calls: list[str] = []
    real_single = db.upsert_edge_conn

    def counting_single(conn, edge):
        calls.append(edge.id)
        return real_single(conn, edge)

    monkeypatch.setattr(db, "upsert_edge_conn", counting_single)
    batched = _build_twice(_project(tmp_path / "batched" / "proj", tests=2))
    assert calls == [], "the batched build writes no edge one at a time"

    monkeypatch.setattr(db, "upsert_edges_conn", _per_edge)
    reference = _build_twice(_project(tmp_path / "reference" / "proj", tests=2))
    assert calls, "the reference build writes every edge one at a time"

    assert batched == reference
    assert batched[1], "the project has edges"


@contextlib.contextmanager
def _traced(statements: list[str]):
    real = db._connect

    @contextlib.contextmanager
    def connect(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(db, "_connect", connect)
        yield


def _baseline_selects(root: Path, tests: int) -> tuple[int, int]:
    """Re-run the scan baseline for every test of a built project; return (SELECTs issued, tests baselined)."""
    _project(root, tests)
    dbp = _db_path(str(root))
    build_index(dbp, root)
    with db._connect(dbp) as conn:
        conn.execute("DELETE FROM node_verification")
        nodes = [
            SimpleNamespace(id=r[0], location=r[1], subtype="test")
            for r in conn.execute(
                "SELECT id, location FROM nodes WHERE id LIKE '%::test_%' AND node_type = 'atomic_process'"
            )
        ]
    assert len(nodes) == tests
    statements: list[str] = []
    with _traced(statements):
        written = builder._baseline_new_tests(dbp, nodes, (), None, root)
    return sum(1 for s in statements if s.lstrip().upper().startswith("SELECT")), len(written)


def test_scan_baseline_reads_do_not_grow_per_test(tmp_path: Path) -> None:
    """A first build's test baseline reads once per batch: ten times the tests, the same number of SELECTs."""
    few_selects, few = _baseline_selects(tmp_path / "few", tests=3)
    many_selects, many = _baseline_selects(tmp_path / "many", tests=30)
    assert (few, many) == (3, 30)
    assert many_selects == few_selects
