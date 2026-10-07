"""Link-target resolution reads the re-export relation per module, with the whole read's answers.

The build's resolver no longer reads every ``depends_on`` row: it reads the
modules its walks reach, and asks whether any module outside the rescans
holds a named re-export marker only once a counted link stays unresolved,
stopping at the first holder.  These tests hold it to the whole-read
resolver's warnings, retargeted links and stored rows, and count its work.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from axiom_graph.db import _core
from axiom_graph.index import builder
from axiom_graph.models import make_edge
from axiom_graph.scanners import module_scanner
from tests.fixtures import whole_read_oracles

_NAMES = module_scanner.REEXPORT_NAMES_KEY

# Stored meta of the marker holder ``p::lib``, as raw column text.  The
# escaped key is a named marker to a JSON parser; the rest are not.
_HOLDER_META = {
    "named": json.dumps({_NAMES: {"a": "b"}}),
    "escaped key": '{"reexport\\u005fnames": {"a": "b"}}',
    "not json": "{not json",
    "json list": "[1, 2]",
    "json number": "5",
    "names a list": json.dumps({_NAMES: ["a"]}),
    "names empty": json.dumps({_NAMES: {}}),
    "names a string": json.dumps({_NAMES: "a"}),
    "star only": json.dumps({module_scanner.REEXPORT_STAR_KEY: "star"}),
    "empty text": "",
    "none": None,
}

_STEP = "p::w::step-1"
_TEST = "p::t::test_x"
# Rescan shapes: no build; nothing that holds a marker; the named holder
# ``p::pkg`` but not ``p::lib``; both holders.
_RESCANS = {
    "outside a build": None,
    "holders not rescanned": {"p::w", _STEP, "p::t", _TEST},
    "lib not rescanned": {"p::w", _STEP, "p::t", _TEST, "p::pkg"},
    "holders rescanned": {"p::w", _STEP, "p::t", _TEST, "p::pkg", "p::lib"},
}


def _node(conn: sqlite3.Connection, node_id: str) -> None:
    conn.execute(
        "INSERT INTO nodes (id, node_type, title, location, source, code_hash, level_0, level_1, updated_at) "
        "VALUES (?, 'atomic_process', ?, 'm.py', 'python', 'h', ?, ?, '2026-01-01')",
        (node_id, node_id, node_id, node_id),
    )


def _depends_on(conn: sqlite3.Connection, src: str, dst: str, meta: str | None) -> None:
    conn.execute(
        "INSERT INTO edges (id, edge_type, from_id, to_id, meta) VALUES (?, 'depends_on', ?, ?, ?)",
        (f"depends_on:{src}->{dst}", src, dst, meta),
    )


def _index(path: Path, holder_meta: str | None) -> Path:
    """An index where ``p::pkg`` re-exports ``real`` as ``alias`` and ``p::lib`` holds *holder_meta*."""
    _core.init_db(path)
    conn = sqlite3.connect(path)
    for node_id in ("p::impl::real", _STEP, _TEST):
        _node(conn, node_id)
    _depends_on(conn, "p::pkg", "p::impl", json.dumps({_NAMES: {"alias": "real"}}))
    _depends_on(conn, "p::lib", "p::dep", holder_meta)
    _depends_on(conn, "p::plain", "p::dep", None)
    conn.commit()
    conn.close()
    return path


def _links() -> list:
    """Links of every resolution outcome: resolved, unresolved, chained, live, and not a link."""
    return [
        make_edge("delegates_to", _STEP, "p::pkg::alias"),
        make_edge("validates", _TEST, "p::pkg::alias"),
        make_edge("delegates_to", _STEP, "p::lib::missing"),
        make_edge("validates", _TEST, "p::plain::gone"),
        make_edge("delegates_to", _STEP, "p::lib::other", meta={module_scanner.UNSPELLED_CHAIN_KEY: True}),
        make_edge("delegates_to", _STEP, "p::impl::real"),
        make_edge("calls", _STEP, "p::nowhere::f"),
    ]


def _resolve(resolver, path: Path, rescanned, live: bool) -> tuple:
    """Run *resolver* and return everything it produces: counts, warnings, links and stored edges."""
    edges, warnings = _links(), []
    live_ids = {"p::impl::real", _STEP, _TEST} if live else None
    counts = resolver(path, edges, warnings, live_ids=live_ids, rescanned_ids=rescanned)
    conn = sqlite3.connect(path)
    stored = conn.execute("SELECT rowid, * FROM edges ORDER BY rowid").fetchall()
    conn.close()
    return counts, warnings, [(e.id, e.edge_type, e.from_id, e.to_id, e.meta) for e in edges], stored


@pytest.mark.parametrize("live", [False, True], ids=["index lookup", "live set"])
@pytest.mark.parametrize("rescan", list(_RESCANS))
@pytest.mark.parametrize("holder", list(_HOLDER_META))
def test_resolution_matches_the_whole_relation_read(tmp_path: Path, holder: str, rescan: str, live: bool) -> None:
    """Warnings, retargeted links, counts and stored rows equal those of the whole-read resolver."""
    meta = _HOLDER_META[holder]
    rescanned = _RESCANS[rescan]
    scoped = _resolve(builder._resolve_delegate_targets, _index(tmp_path / "a.db", meta), rescanned, live)
    whole = _resolve(whole_read_oracles.resolve_delegate_targets, _index(tmp_path / "b.db", meta), rescanned, live)
    assert scoped == whole
    assert scoped[0]["delegates_to"] == 1 and scoped[0]["validates"] == 1


@pytest.mark.parametrize(
    ("holder", "warns"),
    [("named", False), ("escaped key", False), ("not json", True), ("names empty", True), ("star only", True)],
)
def test_only_a_parsed_named_marker_outside_the_rescans_silences_the_warning(
    tmp_path: Path, holder: str, warns: bool
) -> None:
    """With ``p::pkg`` rescanned, ``p::lib``'s meta decides the warning, judged by the JSON parser."""
    path = _index(tmp_path / "w.db", _HOLDER_META[holder])
    _, warnings, _, _ = _resolve(builder._resolve_delegate_targets, path, _RESCANS["lib not rescanned"], True)
    assert bool(warnings) is warns
    assert all("UPDATE nodes SET file_mtime = NULL" in w for w in warnings)


@contextmanager
def _vm_steps(monkeypatch: pytest.MonkeyPatch):
    """Count the SQLite VM steps of every connection opened in the block."""
    steps = {"n": 0}
    real = sqlite3.connect

    def tick() -> int:
        steps["n"] += 1
        return 0

    def counting(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_progress_handler(tick, 1)
        return conn

    monkeypatch.setattr(sqlite3, "connect", counting)
    try:
        yield steps
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)


def _wide_index(path: Path, others: int, guessed_holds_marker: bool) -> Path:
    """An index of *others* unrelated modules, each with a plain and a named ``depends_on`` row."""
    _core.init_db(path)
    conn = sqlite3.connect(path)
    _node(conn, _STEP)
    _depends_on(conn, "p::a", "p::a_plain", None)
    if guessed_holds_marker:
        _depends_on(conn, "p::a", "p::z_named", json.dumps({_NAMES: {"x": "y"}}))
    for i in range(others):
        _depends_on(conn, f"p::o{i:05d}", "p::a_plain", None)
        _depends_on(conn, f"p::o{i:05d}", "p::z_named", json.dumps({_NAMES: {"x": "y"}}))
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize("guessed_holds_marker", [True, False], ids=["guessed module holds one", "found by scan"])
def test_the_marker_question_does_not_grow_with_unrelated_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, guessed_holds_marker: bool
) -> None:
    """An unresolved link takes the same VM steps beside 10 or 2,000 other modules; the whole read does not."""
    counts: dict[tuple[str, int], int] = {}
    for others in (10, 2000):
        for name, resolver in (
            ("scoped", builder._resolve_delegate_targets),
            ("whole", whole_read_oracles.resolve_delegate_targets),
        ):
            path = _wide_index(tmp_path / f"{name}{others}.db", others, guessed_holds_marker)
            edges, warnings = [make_edge("delegates_to", _STEP, "p::a::missing")], []
            with _vm_steps(monkeypatch) as steps:
                resolver(path, edges, warnings, live_ids={_STEP}, rescanned_ids={"p::w", _STEP})
            assert warnings == []
            counts[name, others] = steps["n"]
    assert counts["scoped", 10] == counts["scoped", 2000]
    assert counts["whole", 2000] > counts["whole", 10]
