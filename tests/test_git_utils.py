"""Tests for git_utils name-status helpers.

Tier 1 -- plain pytest: the bulk name-status parser (A/M/D/R, empty diff,
rename pair) and the public ``get_name_status_changes`` helper against a real
temporary git repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from axiom_annotations import workflow

from axiom_graph.index.git_utils import (
    NameStatusChanges,
    _parse_name_status_changes,
    get_name_status_changes,
    resolve_commit,
)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parse_name_status_changes_classifies_each_code():
    raw = "A\tnew.py\nM\tmod.py\nD\tgone.py\nR100\told.py\trenamed.py\n"
    changes = _parse_name_status_changes(raw)
    assert changes.added == {"new.py"}
    assert changes.modified == {"mod.py"}
    assert changes.deleted == {"gone.py"}
    assert changes.renamed == {"old.py": "renamed.py"}


def test_parse_name_status_changes_empty_diff():
    changes = _parse_name_status_changes("")
    assert changes == NameStatusChanges()
    assert not changes.added and not changes.modified
    assert not changes.deleted and not changes.renamed


def test_parse_name_status_changes_normalises_backslashes():
    raw = "A\tdir\\sub\\new.py\n"
    changes = _parse_name_status_changes(raw)
    assert changes.added == {"dir/sub/new.py"}


def test_parse_name_status_changes_added_line_is_in_added_set():
    """The A set is the clean signal for the `added` kind (D-4)."""
    raw = "A\tbrand_new.py\nM\texisting.py\n"
    changes = _parse_name_status_changes(raw)
    assert "brand_new.py" in changes.added
    assert "brand_new.py" not in changes.modified


# ---------------------------------------------------------------------------
# Public helper against a real repo
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def test_get_name_status_changes_real_repo(repo: Path):
    baseline = _git(["rev-parse", "HEAD"], repo)
    # modify a.py, add c.py, delete b.py
    (repo / "a.py").write_text("def a():\n    return 99\n", encoding="utf-8")
    (repo / "c.py").write_text("def c():\n    return 3\n", encoding="utf-8")
    (repo / "b.py").unlink()
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "change"], repo)
    current = _git(["rev-parse", "HEAD"], repo)

    changes = get_name_status_changes(repo, baseline, current)
    assert "a.py" in changes.modified
    assert "c.py" in changes.added
    assert "b.py" in changes.deleted


def test_get_name_status_changes_detects_rename(repo: Path):
    baseline = _git(["rev-parse", "HEAD"], repo)
    _git(["mv", "a.py", "a_renamed.py"], repo)
    _git(["commit", "-m", "rename"], repo)
    current = _git(["rev-parse", "HEAD"], repo)

    changes = get_name_status_changes(repo, baseline, current)
    assert changes.renamed.get("a.py") == "a_renamed.py"


def test_get_name_status_changes_empty_for_blank_sha(repo: Path):
    assert get_name_status_changes(repo, "", "HEAD") == NameStatusChanges()


# ---------------------------------------------------------------------------
# resolve_commit — SHA prefix -> (full SHA, UTC committer time)
# ---------------------------------------------------------------------------


def _commit_with_date(repo: Path, message: str, date: str) -> str:
    env = {**os.environ, "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message], cwd=repo, env=env, capture_output=True, check=True
    )
    return _git(["rev-parse", "HEAD"], repo)


@workflow(
    purpose="A known commit prefix resolves to the full SHA and its committer time in UTC, shaped like "
    "a history row timestamp so string comparison orders it correctly; unknown, ref-name and "
    "out-of-repo inputs resolve to nothing"
)
def test_resolve_commit_returns_full_sha_and_utc_time(repo: Path, tmp_path: Path):
    full = _commit_with_date(repo, "offset commit", "2026-03-04T10:30:00+05:30")

    resolved = resolve_commit(repo, full[:7])

    assert resolved is not None
    sha, ts = resolved
    assert sha == full
    assert ts == "2026-03-04T05:00:00+00:00"
    # A history row written later in the same second sorts after the commit time.
    assert ts < "2026-03-04T05:00:00.123456+00:00"
    assert "2026-03-04T04:59:59.999999+00:00" < ts

    assert resolve_commit(repo, "0123456789abcdef0123456789abcdef01234567") is None
    assert resolve_commit(repo, "HEAD") is None
    assert resolve_commit(tmp_path / "missing", full[:7]) is None


def test_resolve_commit_ambiguous_prefix_is_none(repo: Path):
    lines = []
    for i in range(1500):
        msg = f"c{i}\n"
        lines.append("commit refs/heads/many")
        lines.append(f"committer t <t@t.com> {1700000000 + i} +0000")
        lines.append(f"data {len(msg)}")
        lines.append(msg)
    stream = "\n".join(lines) + "\n"
    subprocess.run(["git", "fast-import", "--quiet"], cwd=repo, input=stream.encode("utf-8"), check=True)
    shas = _git(["rev-list", "refs/heads/many"], repo).split()
    counts: dict[str, int] = {}
    for s in shas:
        counts[s[:4]] = counts.get(s[:4], 0) + 1
    shared = [p for p, n in counts.items() if n > 1]
    if not shared:
        pytest.skip("no shared 4-char commit prefix in this sample")

    assert resolve_commit(repo, shared[0]) is None
