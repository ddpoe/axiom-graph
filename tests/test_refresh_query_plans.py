"""The refresh's edge walks read an index sized by the ids they ask about, on an index that holds the scan indexes."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from axiom_graph.cli import main as cli
from axiom_graph.db._core import _SCAN_INDEX_NAMES
from axiom_graph.index import db
from axiom_graph.index.refresh import _edges_sql


def _indexed(root: Path) -> Path:
    (root / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "demo"\n', encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "docs").mkdir()
    doc = {"title": "G", "tags": [], "sections": [{"id": "f", "heading": "f", "content": "x", "links": []}]}
    (root / "docs" / "g.docjson").write_text(json.dumps(doc), encoding="utf-8")
    assert CliRunner().invoke(cli, ["init", str(root)]).exit_code == 0
    return root / ".axiom_graph" / "graph.db"


def _plan(db_path: Path, sql: str, params: tuple) -> list[str]:
    with db._connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert set(_SCAN_INDEX_NAMES) <= names
        return [r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}", params)]


def test_edges_into_ids_search_the_to_id_index(tmp_path: Path) -> None:
    db_path = _indexed(tmp_path)
    for types in (("composes",), ("documents", "validates", "annotates")):
        plan = _plan(db_path, _edges_sql(len(types), 2, into=True), (*types, "a", "b"))
        assert any("idx_edges_to" in step and "to_id=?" in step for step in plan), plan
        assert not any(step.startswith("SCAN") for step in plan), plan


def test_edges_out_of_ids_search_by_from_id(tmp_path: Path) -> None:
    db_path = _indexed(tmp_path)
    plan = _plan(db_path, _edges_sql(1, 2, into=False), ("annotates", "a", "b"))
    assert any("from_id=?" in step for step in plan), plan
    assert not any(step.startswith("SCAN") for step in plan), plan
