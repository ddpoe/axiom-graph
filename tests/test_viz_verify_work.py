"""The viz verify routes go through the mark_clean funnel on one connection.

``POST /api/nodes/{id}/verify`` and ``POST /api/nodes/bulk-verify`` refuse a
missing node or one with no code hash, then verify the rest through
``mark_clean_nodes``: the stored statuses equal ``check --full`` and the
request opens one connection however many nodes it verifies.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom_graph.index.paths import db_path as _db_path  # noqa: E402
from axiom_graph.lifecycle.api import build_index, compute_check_summary  # noqa: E402
from axiom_graph.viz import server  # noqa: E402
from tests.fixtures.full_recompute import assert_matches_full_recompute, stored_rows  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(root: Path, functions: int) -> tuple[Path, list[str]]:
    """*functions* functions a section documents; each then changes, so each is CONTENT_UPDATED."""
    _write(root / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    _write(root / "src" / "__init__.py", "")
    ids = [f"proj::src.mod::f{i}" for i in range(functions)]
    _write(root / "src" / "mod.py", "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(functions)))
    section = {"id": "s", "heading": "S", "content": "S text.", "links": [{"node_id": n} for n in ids]}
    _write(root / "docs" / "spec.json", json.dumps({"title": "Spec", "sections": [section]}))
    dbp = _db_path(str(root))
    build_index(dbp, root)
    time.sleep(0.05)
    _write(root / "src" / "mod.py", "".join(f"def f{i}():\n    return {i + 10}\n\n\n" for i in range(functions)))
    build_index(dbp, root)
    compute_check_summary(dbp, root)
    return dbp, ids


def _client(root: Path) -> TestClient:
    server._PROJECT_ROOT = root
    server._DB_PATH = root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = []
    server._EXCLUDE_DIRS = []
    return TestClient(server.app)


def _counting_connections(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    opened = [0]
    real_connect = sqlite3.connect

    def counting_connect(*args, **kwargs):
        opened[0] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    return opened


def _bulk_verify(root: Path, functions: int, monkeypatch: pytest.MonkeyPatch) -> int:
    dbp, ids = _project(root, functions)
    client = _client(root)
    with monkeypatch.context() as m:
        opened = _counting_connections(m)
        response = client.post(
            "/api/nodes/bulk-verify",
            json={"node_ids": [*ids, "proj::src.mod::nope"], "reason": "reviewed", "verified_by": "human"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "results": [
            *({"node_id": nid, "ok": True} for nid in ids),
            {"node_id": "proj::src.mod::nope", "ok": False, "error": "Node not found"},
        ]
    }
    assert all(stored_rows(dbp, ids)[nid][0] == "VERIFIED" for nid in ids)
    assert_matches_full_recompute(dbp, root)
    return opened[0]


@workflow(
    purpose="Bulk verify in the viz verifies 3 or 12 nodes on one connection, refuses a missing node, and stores what "
    "check --full stores"
)
def test_bulk_verify_opens_one_connection_whatever_its_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    口 = Step(step_num=1, name="Three nodes", purpose="Bulk-verify three changed functions and a missing id")
    three = _bulk_verify(tmp_path / "three", 3, monkeypatch)
    口 = Step(step_num=2, name="Twelve nodes", purpose="The same with twelve")
    twelve = _bulk_verify(tmp_path / "twelve", 12, monkeypatch)
    口 = Step(step_num=3, name="Compare", purpose="One connection each")
    assert three == twelve == 1


@workflow(purpose="Verify in the viz verifies one node on one connection, and answers 404 for a missing node")
def test_verify_opens_one_connection_and_refuses_a_missing_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbp, ids = _project(tmp_path, 2)
    client = _client(tmp_path)
    with monkeypatch.context() as m:
        opened = _counting_connections(m)
        response = client.post(f"/api/nodes/{ids[0]}/verify", json={"reason": "reviewed", "verified_by": "human"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "node_id": ids[0], "verified_by": "human"}
    assert opened[0] == 1
    assert stored_rows(dbp, [ids[0]])[ids[0]][0] == "VERIFIED"
    assert stored_rows(dbp, [ids[1]])[ids[1]][0] == "CONTENT_UPDATED"
    assert_matches_full_recompute(dbp, tmp_path)
    missing = client.post("/api/nodes/proj::src.mod::nope/verify", json={"reason": "r", "verified_by": "human"})
    assert missing.status_code == 404
