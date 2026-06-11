"""Tests for axiom_graph_checkout (vacuum_into, MCP tool, CLI command).

Covers:
- vacuum_into produces a valid, readable DB snapshot
- Skip-if-exists behavior (MCP and CLI)
- --force overwrite (CLI only)
- Source DB missing raises appropriate error
- Target directory missing returns error (MCP) / rejected by Click (CLI)
- Single-quote path rejection (ValueError)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from axiom_graph import mcp_server, registry
from axiom_graph.cli import main
from axiom_graph.index import db


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path_factory):
    """Redirect the registry away from ~/.axiom_graph/ so checkout side effects don't pollute it."""
    fake_dir = tmp_path_factory.mktemp("registry") / ".axiom_graph"
    monkeypatch.setattr(registry, "REGISTRY_DIR", fake_dir)
    monkeypatch.setattr(registry, "REGISTRY_PATH", fake_dir / "projects.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_source_db(tmp_path: Path) -> Path:
    """Create a minimal axiom-graph DB at tmp_path/.axiom_graph/graph.db."""
    db_path = tmp_path / ".axiom_graph" / "graph.db"
    db.init_db(db_path)
    # Insert a canary row so we can verify the copy is readable.
    with db._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO nodes "
            "(id, title, node_type, location, source, code_hash, desc_hash, level_0, level_1, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            ("test::canary", "canary", "atomic_process", "test.py", "ast", "h1", "d1", "canary", "canary"),
        )
    return db_path


# ---------------------------------------------------------------------------
# vacuum_into unit tests
# ---------------------------------------------------------------------------


class TestVacuumInto:
    def test_happy_path(self, tmp_path: Path) -> None:
        """vacuum_into produces a valid DB with all source data."""
        source = _init_source_db(tmp_path / "src")
        target = tmp_path / "dst" / ".axiom_graph" / "graph.db"

        db.vacuum_into(source, target)

        assert target.exists()
        conn = sqlite3.connect(target)
        row = conn.execute("SELECT id FROM nodes WHERE id = 'test::canary'").fetchone()
        conn.close()
        assert row is not None

    def test_single_quote_rejection(self, tmp_path: Path) -> None:
        """Paths containing single quotes are rejected with ValueError."""
        source = _init_source_db(tmp_path / "src")
        bad_target = tmp_path / "it's" / "axiom_graph.db"

        with pytest.raises(ValueError, match="single quote"):
            db.vacuum_into(source, bad_target)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """vacuum_into creates intermediate directories for the target."""
        source = _init_source_db(tmp_path / "src")
        target = tmp_path / "deep" / "nested" / ".axiom_graph" / "graph.db"

        db.vacuum_into(source, target)

        assert target.exists()


# ---------------------------------------------------------------------------
# MCP tool tests
# ---------------------------------------------------------------------------


class TestMcpCheckout:
    def test_happy_path(self, tmp_path: Path) -> None:
        """MCP tool copies DB and returns success message."""
        _init_source_db(tmp_path / "src")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = mcp_server.axiom_graph_checkout(
            project_root=str(tmp_path / "src"),
            worktree_path=str(worktree),
        )

        assert "Copied axiom-graph DB to" in result
        assert (worktree / ".axiom_graph" / "graph.db").exists()

    def test_skip_if_exists(self, tmp_path: Path) -> None:
        """MCP tool skips when target DB already exists."""
        _init_source_db(tmp_path / "src")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # First copy
        mcp_server.axiom_graph_checkout(str(tmp_path / "src"), str(worktree))
        # Second copy — should skip
        result = mcp_server.axiom_graph_checkout(str(tmp_path / "src"), str(worktree))

        assert "already exists" in result
        assert "skipping" in result

    def test_target_dir_missing(self, tmp_path: Path) -> None:
        """MCP tool returns ERROR when target directory does not exist."""
        _init_source_db(tmp_path / "src")

        result = mcp_server.axiom_graph_checkout(
            project_root=str(tmp_path / "src"),
            worktree_path=str(tmp_path / "nonexistent"),
        )

        assert result.startswith("ERROR:")

    def test_source_db_missing(self, tmp_path: Path) -> None:
        """MCP tool raises FileNotFoundError when source DB is missing."""
        worktree = tmp_path / "wt"
        worktree.mkdir()

        with pytest.raises(FileNotFoundError):
            mcp_server.axiom_graph_checkout(
                project_root=str(tmp_path / "nosrc"),
                worktree_path=str(worktree),
            )


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


class TestCliCheckout:
    def test_happy_path(self, tmp_path: Path) -> None:
        """CLI copies DB and prints success."""
        _init_source_db(tmp_path / "src")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "checkout",
                str(worktree),
                "--project-root",
                str(tmp_path / "src"),
            ],
        )

        assert result.exit_code == 0
        assert "Copied axiom-graph DB to" in result.output

    def test_skip_if_exists(self, tmp_path: Path) -> None:
        """CLI skips without --force when target exists."""
        _init_source_db(tmp_path / "src")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CliRunner()
        runner.invoke(main, ["checkout", str(worktree), "-p", str(tmp_path / "src")])

        result = runner.invoke(main, ["checkout", str(worktree), "-p", str(tmp_path / "src")])

        assert result.exit_code == 0
        assert "already exists" in result.output

    def test_force_overwrite(self, tmp_path: Path) -> None:
        """CLI --force deletes existing DB and copies fresh."""
        _init_source_db(tmp_path / "src")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        runner = CliRunner()
        runner.invoke(main, ["checkout", str(worktree), "-p", str(tmp_path / "src")])

        result = runner.invoke(
            main,
            [
                "checkout",
                str(worktree),
                "-p",
                str(tmp_path / "src"),
                "--force",
            ],
        )

        assert result.exit_code == 0
        assert "Copied axiom-graph DB to" in result.output


# ---------------------------------------------------------------------------
# Registry side-effect: both checkout surfaces auto-register the worktree.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["mcp", "cli"])
def test_checkout_registers_worktree_in_viz_registry(tmp_path: Path, surface: str) -> None:
    """Both MCP and CLI checkout add the worktree path to the project registry."""
    _init_source_db(tmp_path / "src")
    worktree = tmp_path / "wt"
    worktree.mkdir()

    if surface == "mcp":
        result = mcp_server.axiom_graph_checkout(
            project_root=str(tmp_path / "src"),
            worktree_path=str(worktree),
        )
        assert "Copied axiom-graph DB to" in result
    else:  # cli
        runner = CliRunner()
        cli_result = runner.invoke(
            main,
            ["checkout", str(worktree), "-p", str(tmp_path / "src")],
        )
        assert cli_result.exit_code == 0

    entries = registry.load_registry()
    assert len(entries) == 1, f"expected one registry entry, got {entries}"
    assert entries[0]["path"] == str(worktree.resolve())
