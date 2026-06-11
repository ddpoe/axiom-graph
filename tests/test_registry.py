"""Tests for axiom_graph.registry — viz project registry helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph import registry


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch, tmp_path):
    """Redirect the registry away from the user's real ~/.axiom_graph/."""
    fake_dir = tmp_path / ".axiom_graph"
    monkeypatch.setattr(registry, "REGISTRY_DIR", fake_dir)
    monkeypatch.setattr(registry, "REGISTRY_PATH", fake_dir / "projects.json")


def _init_axiom_toml(root: Path, project_id: str) -> None:
    """Create a minimal axiom-graph.toml so project_display_name resolves project_id."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "axiom-graph.toml").write_text(
        f'[axiom_graph]\nproject_id = "{project_id}"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# upsert_registry
# ---------------------------------------------------------------------------


def test_upsert_registry_adds_then_updates_in_place(tmp_path: Path) -> None:
    """First call adds a new entry; second call refreshes name without duplicating."""
    project = tmp_path / "proj"
    _init_axiom_toml(project, "myproj")

    first = registry.upsert_registry(project)
    assert len(first) == 1
    assert first[0]["path"] == str(project.resolve())
    assert first[0]["name"] == "myproj"
    original_added = first[0]["added"]

    # Change the project_id on disk; a second upsert should refresh the name only.
    _init_axiom_toml(project, "myproj-renamed")
    second = registry.upsert_registry(project)

    assert len(second) == 1, "second upsert must update in place, not duplicate"
    assert second[0]["name"] == "myproj-renamed"
    assert second[0]["added"] == original_added, "added timestamp preserved on update"


# ---------------------------------------------------------------------------
# project_display_name
# ---------------------------------------------------------------------------


def test_project_display_name_primary_clone(tmp_path: Path) -> None:
    """Primary clone (.git is a directory) returns project_id unchanged."""
    project = tmp_path / "primary"
    _init_axiom_toml(project, "myproj")
    (project / ".git").mkdir()  # primary clone marker

    assert registry.project_display_name(project) == "myproj"


def test_project_display_name_worktree_appends_marker(tmp_path: Path) -> None:
    """Worktree (.git is a file) appends [wt: <dirname>] to disambiguate."""
    worktree = tmp_path / "wt-feature-x"
    _init_axiom_toml(worktree, "myproj")
    # Git worktrees have .git as a file containing `gitdir: <path>`.
    (worktree / ".git").write_text("gitdir: /some/path/to/.git/worktrees/wt-feature-x\n")

    assert registry.project_display_name(worktree) == "myproj [wt: wt-feature-x]"


# ---------------------------------------------------------------------------
# prune_registry
# ---------------------------------------------------------------------------


def test_prune_registry_drops_dead_paths_and_persists(tmp_path: Path) -> None:
    """Entries whose paths no longer exist are removed from the list and from disk."""
    live = tmp_path / "live"
    live.mkdir()
    dead = tmp_path / "dead"
    dead.mkdir()

    registry.upsert_registry(live)
    registry.upsert_registry(dead)
    assert len(registry.load_registry()) == 2

    dead.rmdir()  # simulate `git worktree remove` / rm -rf

    pruned = registry.prune_registry()
    assert len(pruned) == 1
    assert pruned[0]["path"] == str(live.resolve())

    # Verify the on-disk file was rewritten (next load reflects the prune).
    on_disk = json.loads(registry.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert len(on_disk["projects"]) == 1
    assert on_disk["projects"][0]["path"] == str(live.resolve())


def test_prune_registry_no_op_when_all_paths_exist(tmp_path: Path) -> None:
    """All-live registry is returned unchanged and the file is not rewritten."""
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    registry.upsert_registry(a)
    registry.upsert_registry(b)

    file_mtime_before = registry.REGISTRY_PATH.stat().st_mtime_ns

    pruned = registry.prune_registry()
    assert len(pruned) == 2

    file_mtime_after = registry.REGISTRY_PATH.stat().st_mtime_ns
    assert file_mtime_before == file_mtime_after, "file should not be rewritten when nothing pruned"
