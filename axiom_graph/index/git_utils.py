"""Git helpers shared across axiom-graph index modules."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def get_git_sha(project_root: Path) -> str | None:
    """Return the current HEAD commit SHA, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception as exc:
        logger.debug("get_git_sha failed (expected if not a git repo): %s", exc)
    return None


def _run_git(args: list[str], project_root: Path, timeout: int = 10) -> str | None:
    """Run a git command, returning stdout on success or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return result.stdout
        logger.debug("git %s failed: %s", args[0], result.stderr.strip())
    except Exception as exc:
        logger.debug("git %s error: %s", args[0], exc)
    return None


def _parse_name_status_renames(raw: str) -> dict[str, str]:
    """Parse ``git diff --name-status -M`` output into old_path -> new_path."""
    pairs: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line[0] != "R":
            # Rename status is ``Rxxx`` (e.g. ``R100``); skip A/M/D/C lines.
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            pairs[old_path.replace("\\", "/")] = new_path.replace("\\", "/")
    return pairs


def get_rename_pairs(project_root: Path, since_sha: str | None) -> dict[str, str]:
    """Return git file-rename pairs as ``{old_path: new_path}``.

    Composes two diffs so a function that moved via a committed file-rename
    **and** an uncommitted one resolves to a single old->new path mapping:

    1. Committed: ``git diff --name-status -M <since_sha>..HEAD`` (skipped when
       *since_sha* is ``None`` -- e.g. first build).
    2. Working tree: ``git diff --name-status -M HEAD``.

    Args:
        project_root: Repo root to run git in.
        since_sha: Baseline commit for the committed diff, or ``None``.

    Returns:
        Mapping from a node's *old* repo-relative path to its *current* path.
        Returns an empty dict when git is unavailable.
    """
    committed: dict[str, str] = {}
    if since_sha:
        raw = _run_git(["diff", "--name-status", "-M", f"{since_sha}..HEAD"], project_root)
        if raw is not None:
            committed = _parse_name_status_renames(raw)

    raw_wt = _run_git(["diff", "--name-status", "-M", "HEAD"], project_root)
    working: dict[str, str] = _parse_name_status_renames(raw_wt) if raw_wt is not None else {}

    # Compose: committed old->mid, working mid->new  ==>  old->new.
    composed: dict[str, str] = dict(committed)
    for old_path, new_path in committed.items():
        if new_path in working:
            composed[old_path] = working[new_path]
    for old_path, new_path in working.items():
        composed.setdefault(old_path, new_path)
    return composed


def get_old_body(
    project_root: Path,
    git_sha: str,
    old_path: str,
    start_line: int | None,
    end_line: int | None,
) -> str | None:
    """Return the body text of a node at a past commit, sliced to its lines.

    Mirrors the ``get_node_diff`` retrieval path: ``git show <sha>:<path>``
    then slice to ``[start_line, end_line]`` (1-based, inclusive).

    Args:
        project_root: Repo root.
        git_sha: Commit to read the old file from.
        old_path: Repo-relative path of the file at that commit.
        start_line: 1-based first line of the node (or ``None`` for whole file).
        end_line: 1-based last line of the node (inclusive).

    Returns:
        The sliced body text, or ``None`` if the blob is unreachable.
    """
    git_path = old_path.replace("\\", "/")
    raw = _run_git(["show", f"{git_sha}:{git_path}"], project_root)
    if raw is None:
        return None
    if start_line is None or end_line is None:
        return raw
    lines = raw.splitlines(keepends=True)
    return "".join(lines[max(start_line - 1, 0) : end_line])
