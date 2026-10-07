"""Tests for ``axiom-graph export``, the whole-index JSON dump."""

from __future__ import annotations

import json
from pathlib import Path

from axiom_annotations import workflow
from click.testing import CliRunner

from axiom_graph import __version__
from axiom_graph.cli import main
from axiom_graph.index import builder


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _export(project_root: Path) -> None:
    """Run ``axiom-graph export`` on *project_root* and assert it succeeded."""
    result = CliRunner().invoke(main, ["export", str(project_root)])
    assert result.exit_code == 0, result.output


@workflow(
    purpose="The index export records the project's id and the running version, and is written beside the index database",
)
def test_index_export_records_project_id_version_and_db_location(tmp_path):
    configured = tmp_path / "configured"
    _write(configured / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\ndb_path = "store/graph.db"\n')
    _write(configured / "mod.py", "def f():\n    return 1\n")
    builder.build(configured, discovery_only=False)

    _export(configured)

    exported = json.loads((configured / "store" / "index.json").read_text(encoding="utf-8"))
    assert exported["project_id"] == "proj"
    assert exported["axiom_graph_version"] == __version__
    assert any(node["id"].startswith("proj::") for node in exported["nodes"])
    assert not (configured / ".axiom_graph" / "index.json").exists()

    # With no toml, the id is the one the index was built with, not the folder name.
    bare = tmp_path / "bare"
    _write(bare / "mod.py", "def f():\n    return 1\n")
    builder.build(bare, project_id="other", discovery_only=False)

    _export(bare)

    assert json.loads((bare / ".axiom_graph" / "index.json").read_text(encoding="utf-8"))["project_id"] == "other"
