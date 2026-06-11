"""Shared fixtures for axiom_graph tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from axiom_graph.index import db


@pytest.fixture
def mini_project(tmp_path: Path) -> Path:
    """Return a tmp directory initialised with an axiom-graph DB.

    Callers write their own .py files into it and call build() or
    scan_module() directly.
    """
    ag_dir = tmp_path / ".axiom_graph"
    ag_dir.mkdir()
    db.init_db(ag_dir / "graph.db")
    return tmp_path


@pytest.fixture
def db_path(mini_project: Path) -> Path:
    return mini_project / ".axiom_graph" / "graph.db"


@pytest.fixture
def git_project(tmp_path: Path) -> Path:
    """Temp directory with a real git repo + axiom-graph DB.

    Has an initial commit so HEAD exists.  E2E tests write .py files,
    commit, and call builder/staleness functions against real git.
    """
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    (tmp_path / ".gitkeep").touch()
    subprocess.run(
        ["git", "add", "."],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    ag_dir = tmp_path / ".axiom_graph"
    ag_dir.mkdir()
    db.init_db(ag_dir / "graph.db")
    return tmp_path


@pytest.fixture
def git_db_path(git_project: Path) -> Path:
    return git_project / ".axiom_graph" / "graph.db"
