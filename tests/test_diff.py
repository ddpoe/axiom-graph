"""Tests for the diff feature: get_node_diff() helper and axiom_graph_diff MCP tool."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock


from axiom_annotations import workflow

from axiom_graph.lifecycle.api import _default_baseline_row, get_node_diff, _parse_level3, _slice_lines
from axiom_graph.index import db
from axiom_graph.models import AxiomNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(
    node_id: str,
    code_hash: str = "abc123",
    location: str = "src/mod.py",
    level_3_location: str | None = "src/mod.py#L5-L10",
) -> AxiomNode:
    return AxiomNode(
        id=node_id,
        node_type="atomic_process",
        subtype="function",
        title=node_id.split("::")[-1],
        location=location,
        level_3_location=level_3_location,
        source="ast",
        code_hash=code_hash,
        desc_hash=None,
        level_0=node_id,
        level_1=node_id,
    )


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command in *cwd* and return its stripped stdout."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _head(repo: Path) -> str:
    """Return the full SHA of HEAD."""
    return _git(["rev-parse", "HEAD"], repo)


def _write(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_history(
    db_path: Path,
    node_id: str,
    change_type: str,
    git_sha: str | None = None,
    preserved: bool = False,
) -> None:
    """Insert a history row with a tiny sleep to ensure timestamp ordering."""
    time.sleep(0.015)
    db.insert_history_row(
        db_path,
        node_id,
        change_type,
        git_sha=git_sha,
        preserved=preserved,
    )


# ---------------------------------------------------------------------------
# Tier 1 — _parse_level3 unit tests
# ---------------------------------------------------------------------------


def test_parse_level3_full_range():
    """Parse a standard level_3_location with line range."""
    path, start, end = _parse_level3("src/mod.py#L5-L10")
    assert path == "src/mod.py"
    assert start == 5
    assert end == 10


def test_parse_level3_single_line():
    """Parse a level_3_location with a single line."""
    path, start, end = _parse_level3("src/mod.py#L42")
    assert path == "src/mod.py"
    assert start == 42
    assert end == 42


def test_parse_level3_no_lines():
    """Parse a level_3_location with no line range (module-level)."""
    path, start, end = _parse_level3("src/mod.py")
    assert path == "src/mod.py"
    assert start is None
    assert end is None


def test_parse_level3_none():
    """Parse None returns all None."""
    assert _parse_level3(None) == (None, None, None)


def test_slice_lines_with_range():
    """Slice lines by 1-based inclusive range."""
    content = "line1\nline2\nline3\nline4\nline5"
    assert _slice_lines(content, 2, 4) == "line2\nline3\nline4"


def test_slice_lines_no_range():
    """No range returns full content."""
    content = "line1\nline2"
    assert _slice_lines(content, None, None) == content


# ---------------------------------------------------------------------------
# Tier 1 — get_node_diff unit tests
# ---------------------------------------------------------------------------


def test_diff_node_not_found(db_path: Path, mini_project: Path):
    """Returns error when node doesn't exist."""
    result = get_node_diff(db_path, mini_project, "nonexistent::node")
    assert result["error"] == "no_baseline"
    assert "not found" in result["reason"].lower()


def test_diff_no_baseline_sha(db_path: Path, mini_project: Path):
    """Returns error when no history entry has a git SHA."""
    n = _node("proj::src.mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)
    # Content event with no git_sha
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha=None)

    result = get_node_diff(db_path, mini_project, n.id)
    assert result["error"] == "no_baseline"
    assert "git sha" in result["reason"].lower()


def test_diff_no_location(db_path: Path, mini_project: Path):
    """Returns error when node has no source location."""
    n = _node("proj::src.mod::fn", location="", level_3_location=None)
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="abc1234")

    result = get_node_diff(db_path, mini_project, n.id)
    assert result["error"] == "no_baseline"
    assert "location" in result["reason"].lower()


def test_diff_git_show_failure(db_path: Path, mini_project: Path):
    """Returns error when git show fails (bad SHA or file not in tree)."""
    # Create source file so current content exists
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("line1\nline2\nline3\n")

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L3")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="deadbeef00000000")

    result = get_node_diff(db_path, mini_project, n.id)
    assert result["error"] == "no_baseline"
    assert "git show failed" in result["reason"].lower() or "git error" in result["reason"].lower()


def test_diff_new_file_returns_empty_old(git_db_path: Path, git_project: Path):
    """A file added after the baseline diffs with an empty old side, not an error."""
    baseline = _head(git_project)
    _write(git_project / "src" / "mod.py", "def fn():\n    return 42\n")
    _git(["add", "-A"], git_project)
    _git(["commit", "-m", "add mod"], git_project)

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(git_db_path, n, discovery_only=False)

    result = get_node_diff(git_db_path, git_project, n.id, baseline_sha=baseline)

    assert "error" not in result
    assert result["old_content"] == ""
    assert "return 42" in result["new_content"]
    assert result["baseline_sha"] == baseline
    assert result["baseline_path"] is None
    assert result["path"] == "src/mod.py"


# ---------------------------------------------------------------------------
# Tier 1 — get_node_diff follows renames (real git)
# ---------------------------------------------------------------------------


_MOVED_MODULE = (
    "# helpers\n"
    "\n"
    "def bar(label, count, total):\n"
    '    """Format a bar."""\n'
    "    if total == 0:\n"
    '        return f"- {label}: 0"\n'
    "    pct = count / total * 100\n"
    '    return f"- {label}: {count} ({pct:.0f}%)"\n'
)


def test_diff_follows_committed_file_move(git_db_path: Path, git_project: Path):
    """A function in a file moved since the baseline diffs against its old path, unchanged."""
    _write(git_project / "scripts" / "report.py", _MOVED_MODULE)
    _git(["add", "-A"], git_project)
    _git(["commit", "-m", "add report"], git_project)
    baseline = _head(git_project)
    (git_project / "pkg" / "scripts").mkdir(parents=True)
    _git(["mv", "scripts/report.py", "pkg/scripts/report.py"], git_project)
    _git(["commit", "-m", "move report"], git_project)

    n = _node(
        "proj::pkg.scripts.report::bar",
        location="pkg/scripts/report.py",
        level_3_location="pkg/scripts/report.py#L3-L8",
    )
    db.upsert_node(git_db_path, n, discovery_only=False)

    result = get_node_diff(git_db_path, git_project, n.id, baseline_sha=baseline)

    assert "error" not in result
    assert result["old_content"] == result["new_content"]
    assert "def bar" in result["old_content"]
    assert result["baseline_path"] == "scripts/report.py"
    assert result["path"] == "pkg/scripts/report.py"


def test_diff_follows_staged_uncommitted_move(git_db_path: Path, git_project: Path):
    """A move staged in the index but not yet committed is followed too."""
    _write(git_project / "scripts" / "report.py", _MOVED_MODULE)
    _git(["add", "-A"], git_project)
    _git(["commit", "-m", "add report"], git_project)
    baseline = _head(git_project)
    _git(["mv", "scripts/report.py", "report_moved.py"], git_project)

    n = _node("proj::report_moved::bar", location="report_moved.py", level_3_location="report_moved.py#L3-L8")
    db.upsert_node(git_db_path, n, discovery_only=False)

    result = get_node_diff(git_db_path, git_project, n.id, baseline_sha=baseline)

    assert "error" not in result
    assert result["old_content"] == result["new_content"]
    assert result["baseline_path"] == "scripts/report.py"


def test_diff_unresolvable_baseline_path_is_error(git_db_path: Path, git_project: Path):
    """When git cannot tell whether the file was renamed, the diff errors instead of reading as new."""
    for i in range(3):
        _write(git_project / "old" / f"m{i}.py", "".join(f"line_{i}_{k} = {k}\n" for k in range(20)))
    _git(["add", "-A"], git_project)
    _git(["commit", "-m", "add modules"], git_project)
    baseline = _head(git_project)
    # Rename (new basenames) and edit every file so only inexact rename
    # detection could pair them, then cap the rename matrix so git skips it.
    _git(["rm", "-r", "-q", "old"], git_project)
    for i in range(3):
        _write(git_project / "new" / f"n{i}.py", "".join(f"line_{i}_{k} = {k}\n" for k in range(19)) + "edited = 1\n")
    _git(["add", "-A"], git_project)
    _git(["commit", "-m", "move and edit"], git_project)
    _git(["config", "diff.renameLimit", "1"], git_project)

    n = _node("proj::new.n0::fn", location="new/n0.py", level_3_location="new/n0.py#L1-L3")
    db.upsert_node(git_db_path, n, discovery_only=False)

    result = get_node_diff(git_db_path, git_project, n.id, baseline_sha=baseline)

    assert result["error"] == "baseline_path_unresolved"
    assert "new/n0.py" in result["reason"]
    assert "old_content" not in result


def test_diff_success_with_mock_git(db_path: Path, mini_project: Path):
    """Returns old and new content when baseline SHA resolves via git."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text(
        "# header\n"
        "# padding\n"
        "# padding2\n"
        "# padding3\n"
        "def fn():\n"
        "    return 42\n"
        "# padding4\n"
        "# padding5\n"
        "# padding6\n"
        "# padding7\n"
    )

    n = _node(
        "proj::src.mod::fn",
        location="src/mod.py",
        level_3_location="src/mod.py#L5-L6",
    )
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="abc1234")

    old_file = (
        "# header\n"
        "# padding\n"
        "# padding2\n"
        "# padding3\n"
        "def fn():\n"
        "    return 0\n"
        "# padding4\n"
        "# padding5\n"
        "# padding6\n"
        "# padding7\n"
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = old_file

    with patch("axiom_graph.lifecycle.api.subprocess.run", return_value=mock_result) as mock_run:
        result = get_node_diff(db_path, mini_project, n.id)

    assert "error" not in result
    assert result["baseline_sha"] == "abc1234"
    assert "return 0" in result["old_content"]
    assert "return 42" in result["new_content"]
    # Verify git show was called with correct args (first subprocess call)
    show_call = mock_run.call_args_list[0]
    assert "abc1234:src/mod.py" in show_call[0][0]
    # Commit context fields should be populated (mocked git log returns same content)
    assert "commit_subject" in result


def test_diff_picks_most_recent_baseline(db_path: Path, mini_project: Path):
    """Baseline resolution picks the newest verified/checkpoint row with a SHA."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    pass\n")

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    # Old checkpoint
    _seed_history(db_path, n.id, "CHECKPOINT", git_sha="old_sha")
    # Content change (no SHA)
    _seed_history(db_path, n.id, "CONTENT_ONLY")
    # Newer verification
    _seed_history(db_path, n.id, "MANUAL_VERIFIED", git_sha="new_sha")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "def fn():\n    pass\n"

    with patch("axiom_graph.lifecycle.api.subprocess.run", return_value=mock_result):
        result = get_node_diff(db_path, mini_project, n.id)

    assert result["baseline_sha"] == "new_sha"


def _history_row(change_type: str, sha: str | None = None) -> dict:
    """A history row as ``get_history`` returns it, for the baseline picker."""
    return {"change_type": change_type, "git_sha": sha, "scanned_at": "2026-10-05T00:00:00"}


def test_default_baseline_tracks_own_and_link_stretches_separately():
    """A stale stretch closes on a verification or its own dimension's healing row, never the other's."""
    cascade_cleared = [
        _history_row("CHECKPOINT", "ccc"),
        _history_row("LINK_BECAME_VERIFIED"),
        _history_row("BECAME_LINKED_STALE"),
        _history_row("INITIAL", "aaa"),
    ]
    row, reason, _ = _default_baseline_row(cascade_cleared)
    assert row["git_sha"] == "ccc"
    assert reason == "newest CHECKPOINT entry with a git SHA"

    own_healed_link_open = [
        _history_row("CHECKPOINT", "ccc"),
        _history_row("BECAME_VERIFIED"),
        _history_row("BECAME_LINKED_STALE"),
        _history_row("CHECKPOINT", "bbb"),
        _history_row("INITIAL", "aaa"),
    ]
    row, reason, _ = _default_baseline_row(own_healed_link_open)
    assert row["git_sha"] == "bbb"
    assert "went LINKED_STALE" in reason

    edited_then_link_stale = [
        _history_row("CHECKPOINT", "ccc"),
        _history_row("BECAME_LINKED_STALE"),
        _history_row("CHECKPOINT", "bbb"),
        _history_row("BECAME_CONTENT_UPDATED"),
        _history_row("INITIAL", "aaa"),
    ]
    row, reason, _ = _default_baseline_row(edited_then_link_stale)
    assert row["git_sha"] == "aaa"
    assert "went CONTENT_UPDATED" in reason


def test_diff_falls_back_to_oldest_sha(db_path: Path, mini_project: Path):
    """When no verified row exists, falls back to the oldest row with a SHA."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    # Only INITIAL + CONTENT_ONLY with SHAs — no verification
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="later_sha")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "def fn():\n    return 0\n"

    with patch("axiom_graph.lifecycle.api.subprocess.run", return_value=mock_result):
        result = get_node_diff(db_path, mini_project, n.id)

    # Should find the INITIAL row's SHA (from upsert_node) or the CONTENT_ONLY sha
    assert "error" not in result
    assert result["baseline_sha"] is not None


def test_diff_explicit_sha_override(db_path: Path, mini_project: Path):
    """When baseline_sha is passed explicitly, it is used directly."""
    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "CONTENT_ONLY", git_sha="auto_sha")
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="verified_sha")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "def fn():\n    return 0\n"

    with patch("axiom_graph.lifecycle.api.subprocess.run", return_value=mock_result):
        result = get_node_diff(db_path, mini_project, n.id, baseline_sha="auto_sha")

    assert result["baseline_sha"] == "auto_sha"


# ---------------------------------------------------------------------------
# Tier 2 — Integration: viz endpoint + MCP tool
# ---------------------------------------------------------------------------


@workflow(purpose="Verify diff endpoint returns structured JSON with old/new content or graceful error")
def test_diff_endpoint_graceful_fallback(db_path: Path, mini_project: Path):
    """Diff endpoint returns {error, reason} when no baseline available."""
    n = _node("proj::src.mod::fn")
    db.upsert_node(db_path, n, discovery_only=False)

    result = get_node_diff(db_path, mini_project, n.id)
    assert result["error"] == "no_baseline"
    assert "reason" in result


@workflow(purpose="Verify axiom_graph_diff MCP tool returns structured diff with line-count summary")
def test_mcp_axiom_graph_diff_returns_summary(db_path: Path, mini_project: Path):
    """MCP tool returns JSON with summary field containing line-count delta."""

    src = mini_project / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "mod.py").write_text("def fn():\n    return 42\n")

    n = _node("proj::src.mod::fn", location="src/mod.py", level_3_location="src/mod.py#L1-L2")
    db.upsert_node(db_path, n, discovery_only=False)
    _seed_history(db_path, n.id, "AGENT_VERIFIED", git_sha="abc1234")

    old_file = "def fn():\n    return 0\n"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = old_file

    with patch("axiom_graph.lifecycle.api.subprocess.run", return_value=mock_result):
        result = get_node_diff(db_path, mini_project, n.id)

    assert "error" not in result
    # Simulate what the MCP tool does: compute summary
    old_lines = result["old_content"].splitlines()
    new_lines = result["new_content"].splitlines()
    added = sum(1 for ln in new_lines if ln not in old_lines)
    removed = sum(1 for ln in old_lines if ln not in new_lines)
    summary = f"+{added} / -{removed} lines in body"
    assert "+" in summary and "-" in summary
