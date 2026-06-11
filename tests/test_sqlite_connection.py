"""Tests for SQLite connection resilience.

Covers:
- WAL mode enabled on every connection
- timeout=5 so concurrent writers wait instead of failing
- foreign_keys=ON
- Rollback on exception
- MCP tools raise exceptions (not return error strings) on unexpected failures
- MCP input validation still returns ERROR strings (not exceptions)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from axiom_graph.index import db
from axiom_graph.models import AxiomNode
from axiom_graph import mcp_server


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_node(node_id: str, code_hash: str = "abc123") -> AxiomNode:
    """Create a minimal AxiomNode for testing."""
    parts = node_id.rsplit("::", 1)
    return AxiomNode(
        id=node_id,
        title=parts[-1],
        node_type="atomic_process",
        location="test_file.py",
        source="ast",
        code_hash=code_hash,
        desc_hash="desc_hash_placeholder",
        level_0=node_id,
        level_1=f"{parts[-1]} — test node",
    )


# ---------------------------------------------------------------------------
# Connection layer tests
# ---------------------------------------------------------------------------


def test_connect_enables_wal_mode(db_path: Path) -> None:
    """_connect() sets journal_mode=WAL on every connection."""
    with db._connect(db_path) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"


def test_connect_uses_timeout(db_path: Path) -> None:
    """_connect() passes timeout=5 so a second writer waits instead of failing immediately."""
    blocker = sqlite3.connect(str(db_path), timeout=0)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN EXCLUSIVE")

    def release():
        time.sleep(0.3)
        blocker.rollback()
        blocker.close()

    threading.Thread(target=release, daemon=True).start()

    with db._connect(db_path) as conn:
        conn.execute("SELECT count(*) FROM nodes")


def test_connect_enables_foreign_keys(db_path: Path) -> None:
    """_connect() sets PRAGMA foreign_keys=ON."""
    with db._connect(db_path) as conn:
        row = conn.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


def test_connect_rollback_on_exception(db_path: Path) -> None:
    """_connect() rolls back the transaction when an exception is raised."""
    node = _make_node("pkg::mod::func_rollback", code_hash="aaa")
    db.upsert_node(db_path, node)

    with pytest.raises(RuntimeError):
        with db._connect(db_path) as conn:
            conn.execute(
                "UPDATE nodes SET code_hash = ? WHERE id = ?",
                ("zzz", "pkg::mod::func_rollback"),
            )
            raise RuntimeError("force rollback")

    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT code_hash FROM nodes WHERE id = ?",
            ("pkg::mod::func_rollback",),
        ).fetchone()
        assert row["code_hash"] == "aaa"


# ---------------------------------------------------------------------------
# MCP structured error tests
# ---------------------------------------------------------------------------


def test_mcp_tool_raises_on_unexpected_error(tmp_path: Path) -> None:
    """MCP tools re-raise unexpected exceptions instead of returning error strings."""
    fake_root = str(tmp_path / "nonexistent")
    with pytest.raises((FileNotFoundError, OSError)):
        mcp_server.axiom_graph_build(fake_root)


def test_mcp_tool_input_validation_returns_error_string(mini_project: Path) -> None:
    """Input validation errors (bad node_id) still return ERROR strings."""
    result = mcp_server.axiom_graph_render(str(mini_project), level=1, node_id="nonexistent::node")
    assert result.startswith("ERROR:")
