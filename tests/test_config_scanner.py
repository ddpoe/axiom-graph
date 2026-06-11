"""Tests for config_scanner: .claude/ config files → AxiomNode objects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph.scanners.config_scanner import scan_config_dir, _dotpath


# ---------------------------------------------------------------------------
# Unit: _dotpath
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel, expected",
    [
        (".claude/settings.local.json", "claude.settings-local"),
        (".claude/skills/axiom-annotations-markers/SKILL.md", "claude.skills.axiom-annotations-markers.SKILL"),
        (".claude/hooks.yaml", "claude.hooks"),
        (".claude/settings.json", "claude.settings"),
    ],
)
def test_dotpath(rel: str, expected: str):
    assert _dotpath(rel) == expected


# ---------------------------------------------------------------------------
# Integration: scan_config_dir
# ---------------------------------------------------------------------------


def test_scan_json_file(tmp_path: Path):
    """A JSON config file produces a composite_process node with subtype=config."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    settings = {"permissions": {"allow": ["Bash"]}}
    (config_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    nodes, edges, skipped = scan_config_dir(
        config_dir,
        tmp_path,
        "test_proj",
        prefix="config",
    )

    assert skipped == 0
    assert len(nodes) == 1
    node = nodes[0]
    assert node.id == "test_proj::config.claude.settings"
    assert node.node_type == "composite_process"
    assert node.subtype == "config"
    assert node.source == "config_scanner"
    assert node.title == "settings.json"
    assert node.code_hash  # non-empty hash


def test_scan_markdown_skill(tmp_path: Path):
    """A SKILL.md produces a composite_process node (no section parsing)."""
    skill_dir = tmp_path / ".claude" / "skills" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\n---\n# My Skill\n\nDoes things.\n",
        encoding="utf-8",
    )

    nodes, edges, skipped = scan_config_dir(
        tmp_path / ".claude",
        tmp_path,
        "test_proj",
        prefix="config",
    )

    assert len(nodes) == 1
    node = nodes[0]
    assert node.id == "test_proj::config.claude.skills.my-skill.SKILL"
    assert node.node_type == "composite_process"
    assert node.subtype == "config"


def test_scan_mtime_fast_pass(tmp_path: Path):
    """Files with unchanged mtime are skipped."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    f = config_dir / "settings.json"
    f.write_text("{}", encoding="utf-8")
    mtime = f.stat().st_mtime

    # Simulate stored mtime matching current
    stored = {".claude/settings.json": mtime}
    nodes, edges, skipped = scan_config_dir(
        config_dir,
        tmp_path,
        "test_proj",
        prefix="config",
        stored_mtimes=stored,
    )

    assert skipped == 1
    assert len(nodes) == 0


def test_scan_nonexistent_dir(tmp_path: Path):
    """Scanning a nonexistent directory returns empty results."""
    nodes, edges, skipped = scan_config_dir(
        tmp_path / ".claude",
        tmp_path,
        "test_proj",
    )
    assert nodes == []
    assert edges == []
    assert skipped == 0


def test_scan_ignores_unsupported_extensions(tmp_path: Path):
    """Files with unsupported extensions are not scanned."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / "binary.dat").write_bytes(b"\x00\x01\x02")
    (config_dir / "notes.txt").write_text("hello", encoding="utf-8")

    nodes, edges, skipped = scan_config_dir(
        config_dir,
        tmp_path,
        "test_proj",
    )
    assert len(nodes) == 0


def test_scan_multiple_files(tmp_path: Path):
    """Multiple config files in nested dirs all produce nodes."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text("{}", encoding="utf-8")
    skill_dir = config_dir / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Foo", encoding="utf-8")

    nodes, edges, skipped = scan_config_dir(
        config_dir,
        tmp_path,
        "test_proj",
    )
    assert len(nodes) == 2
    ids = {n.id for n in nodes}
    assert "test_proj::config.claude.settings" in ids
    assert "test_proj::config.claude.skills.foo.SKILL" in ids


def test_hash_changes_on_content_change(tmp_path: Path):
    """Changing file content produces a different hash."""
    config_dir = tmp_path / ".claude"
    config_dir.mkdir()
    f = config_dir / "settings.json"

    f.write_text('{"a": 1}', encoding="utf-8")
    nodes1, _, _ = scan_config_dir(config_dir, tmp_path, "test_proj")
    hash1 = nodes1[0].code_hash

    f.write_text('{"a": 2}', encoding="utf-8")
    nodes2, _, _ = scan_config_dir(config_dir, tmp_path, "test_proj")
    hash2 = nodes2[0].code_hash

    assert hash1 != hash2
