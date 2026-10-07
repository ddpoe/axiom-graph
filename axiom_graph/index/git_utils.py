"""Git helpers shared across axiom-graph index modules."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
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


def get_git_branch(project_root: Path) -> str | None:
    """Return the checked-out branch name, or None (detached HEAD, or not a git repo)."""
    out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_root, timeout=5)
    name = out.strip() if out else ""
    return name if name and name != "HEAD" else None


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


def resolve_commit(project_root: Path, sha_prefix: str) -> tuple[str, str] | None:
    """Resolve a SHA prefix to one commit and its committer time.

    Asks git to expand *sha_prefix* to a single commit
    (``rev-parse --verify --quiet <prefix>^{commit}``), then reads that
    commit's committer date (``show -s --format=%cI``).  The time is
    converted to UTC and rendered the way ``node_history.scanned_at`` is
    written (``datetime.isoformat()`` on an aware UTC value, i.e. a
    ``+00:00`` suffix), so it orders correctly against history rows that
    are compared as strings.

    Args:
        project_root: Repo root to run git in.
        sha_prefix: A full or abbreviated commit SHA.

    Returns:
        ``(full_sha, utc_timestamp)``, or ``None`` when the prefix is
        unknown to git, ambiguous, not a commit, or git is unavailable /
        the directory is not a repository.
    """
    from datetime import datetime, timezone

    if not sha_prefix or project_root is None:
        return None
    # Only hex SHAs: a ref name ("HEAD", a branch) is not a SHA reference.
    if any(c not in "0123456789abcdefABCDEF" for c in sha_prefix):
        return None
    raw_sha = _run_git(["rev-parse", "--verify", "--quiet", f"{sha_prefix}^{{commit}}"], project_root)
    if raw_sha is None:
        return None
    full_sha = raw_sha.strip()
    if not full_sha:
        return None
    raw_time = _run_git(["show", "-s", "--format=%cI", full_sha], project_root)
    if raw_time is None:
        return None
    try:
        committed = datetime.fromisoformat(raw_time.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        logger.debug("resolve_commit: could not parse commit time %r for %s: %s", raw_time, full_sha, exc)
        return None
    if committed.tzinfo is None:
        committed = committed.replace(tzinfo=timezone.utc)
    return full_sha, committed.astimezone(timezone.utc).isoformat()


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


@dataclass
class NameStatusChanges:
    """Full per-file change set from ``git diff --name-status -M``.

    Attributes:
        added: Repo-relative paths added between the two commits (``A`` lines).
        modified: Repo-relative paths modified in place (``M`` lines).
        deleted: Repo-relative paths deleted (``D`` lines).
        renamed: Mapping ``{old_path: new_path}`` for renamed files (``R`` lines).
    """

    added: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)
    deleted: set[str] = field(default_factory=set)
    renamed: dict[str, str] = field(default_factory=dict)


def _parse_name_status_changes(raw: str) -> NameStatusChanges:
    """Parse ``git diff --name-status -M`` output into a full change set.

    Unlike :func:`_parse_name_status_renames` — which keeps only ``R`` lines —
    this retains **every** status code: ``A`` (added), ``M`` (modified),
    ``D`` (deleted), and ``R`` (renamed). Copy (``C``) lines are treated like
    renames (old->new). Paths are POSIX-normalised.

    Args:
        raw: Raw stdout from ``git diff --name-status -M``.

    Returns:
        A :class:`NameStatusChanges` with the four buckets populated.
    """
    changes = NameStatusChanges()
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        code = status[0] if status else ""
        if code == "A" and len(parts) >= 2:
            changes.added.add(parts[1].replace("\\", "/"))
        elif code == "M" and len(parts) >= 2:
            changes.modified.add(parts[1].replace("\\", "/"))
        elif code == "D" and len(parts) >= 2:
            changes.deleted.add(parts[1].replace("\\", "/"))
        elif code in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
            changes.renamed[old_path.replace("\\", "/")] = new_path.replace("\\", "/")
        # else: T (type change) / U (unmerged) / blank — ignore.
    return changes


def get_name_status_changes(
    project_root: Path,
    baseline_sha: str,
    current_sha: str,
) -> NameStatusChanges:
    """Return the full A/M/D/R change set between two commits in one git call.

    Runs ``git diff --name-status -M <baseline_sha>..<current_sha>`` exactly
    once and classifies every changed path. This is the keystone primitive for
    the net "changed since" diff: it is O(changed files), giving revert-cancel,
    rename, delete, and add detection from a single invocation.

    Args:
        project_root: Repo root to run git in.
        baseline_sha: The baseline commit (the "old" side of the diff).
        current_sha: The current commit (the "new" side — typically the
            index's last-built SHA).

    Returns:
        A :class:`NameStatusChanges`. Returns an empty change set when git is
        unavailable or either SHA is unknown to git.
    """
    if not baseline_sha or not current_sha:
        return NameStatusChanges()
    raw = _run_git(
        ["diff", "--name-status", "-M", f"{baseline_sha}..{current_sha}"],
        project_root,
    )
    if raw is None:
        return NameStatusChanges()
    return _parse_name_status_changes(raw)


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


#: ``git show <rev>:<path>`` stderr fragments meaning "no such path at <rev>".
_PATH_MISSING_MARKERS = ("does not exist", "exists on disk")

#: ``git diff`` stderr fragment printed when the rename matrix exceeded
#: ``diff.renameLimit`` and inexact rename detection did not run.
_RENAME_SKIPPED_MARKER = "rename detection was skipped"


@dataclass
class BaselineFile:
    """A file's content at a baseline revision, located across renames.

    Exactly one of three outcomes, named by ``status``:

    - ``"found"``: the file existed at the baseline; ``path`` is where it
      lived then (the current path, or the path it was renamed from) and
      ``content`` is its text.
    - ``"absent"``: the file did not exist at the baseline under any path git
      can pair with it -- a new file.
    - ``"error"``: the baseline could not be read or the old path could not
      be resolved; ``reason`` says why.  Never to be shown as an empty diff.

    Attributes:
        status: ``"found"``, ``"absent"`` or ``"error"``.
        path: Repo-relative POSIX path at the baseline (``found`` only).
        content: File text at the baseline (``found`` only).
        reason: Human-readable failure (``error`` only).
        unresolved: ``True`` when the error is that git could not tell
            whether the file was renamed (as opposed to git itself failing).
    """

    status: str
    path: str | None = None
    content: str | None = None
    reason: str | None = None
    unresolved: bool = False


def _git_capture(args: list[str], repo_root: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run git in *repo_root*, capturing UTF-8 text; exceptions propagate."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


def _renamed_to_old(raw_z: str) -> dict[str, str]:
    """Parse ``git diff --name-status -z -M`` output into ``{new_path: old_path}``.

    ``-z`` output is NUL-separated with no path quoting: a status token,
    then one path (``A``/``M``/``D``/``T``/``U``) or two paths (``R``/``C``).
    """
    tokens = raw_z.split("\0")
    pairs: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if status[0] in ("R", "C") and i + 2 < len(tokens):
            pairs[tokens[i + 2].replace("\\", "/")] = tokens[i + 1].replace("\\", "/")
            i += 3
        else:
            i += 2
    return pairs


def read_file_at_baseline(repo_root: Path, rev: str, path: str, timeout: int = 10) -> BaselineFile:
    """Read a file as it was at *rev*, following a rename or move since then.

    1. ``git show <rev>:<path>`` -- the file still lives where it did.
    2. If git says the path is missing at *rev*, run
       ``git diff --name-status -z -M <rev>`` over the **whole** tree (a
       pathspec naming only the new path hides the deleted old path from
       rename detection) and look for a rename whose new path is *path*.
       ``git diff <rev>`` compares *rev* with the working tree, so committed
       renames and renames staged in the index (``git mv``, or ``git add``
       of the new path) are both followed.  A file at an untracked path is
       invisible to git's pairing and reads as new.
    3. A rename found -> read the old path at *rev*.  None found -> the
       file is new (``absent``).  Git failing, or reporting that it skipped
       inexact rename detection (``diff.renameLimit``), -> ``error``: the old
       path is unknown, which must not be shown as an all-new file.

    Args:
        repo_root: Root of the git repository the path is relative to.
        rev: Baseline revision (commit SHA, ref, or tree-ish).
        path: Current repo-relative path of the file.
        timeout: Per-git-call timeout in seconds.

    Returns:
        A :class:`BaselineFile` naming one of the three outcomes.
    """
    git_path = path.replace("\\", "/")
    try:
        shown = _git_capture(["show", f"{rev}:{git_path}"], repo_root, timeout)
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out for %s:%s", rev, git_path)
        return BaselineFile("error", reason="git show timed out")
    except Exception as exc:
        logger.warning("git show error: %s", exc)
        return BaselineFile("error", reason=f"git error: {exc}")
    if shown.returncode == 0:
        return BaselineFile("found", path=git_path, content=shown.stdout)
    stderr = shown.stderr.strip()
    if not any(marker in stderr for marker in _PATH_MISSING_MARKERS):
        return BaselineFile("error", reason=f"git show failed: {stderr}")

    try:
        diffed = _git_capture(["diff", "--name-status", "-z", "-M", rev, "--"], repo_root, timeout)
    except subprocess.TimeoutExpired:
        logger.warning("git diff rename lookup timed out for %s", rev)
        return BaselineFile(
            "error",
            reason=f"rename lookup for {git_path} since {rev} timed out",
            unresolved=True,
        )
    except Exception as exc:
        logger.warning("git diff rename lookup error: %s", exc)
        return BaselineFile("error", reason=f"rename lookup for {git_path} failed: {exc}", unresolved=True)
    if diffed.returncode != 0:
        return BaselineFile(
            "error",
            reason=f"rename lookup for {git_path} since {rev} failed: {diffed.stderr.strip()}",
            unresolved=True,
        )

    old_path = _renamed_to_old(diffed.stdout).get(git_path)
    if old_path is None:
        if _RENAME_SKIPPED_MARKER in diffed.stderr:
            return BaselineFile(
                "error",
                reason=(
                    f"cannot tell whether {git_path} was renamed since {rev}: git skipped "
                    "inexact rename detection (too many files; raise diff.renameLimit)"
                ),
                unresolved=True,
            )
        return BaselineFile("absent")

    try:
        old_shown = _git_capture(["show", f"{rev}:{old_path}"], repo_root, timeout)
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out for %s:%s", rev, old_path)
        return BaselineFile("error", reason="git show timed out")
    except Exception as exc:
        logger.warning("git show error: %s", exc)
        return BaselineFile("error", reason=f"git error: {exc}")
    if old_shown.returncode != 0:
        return BaselineFile(
            "error",
            reason=f"{git_path} was renamed from {old_path}, but reading it at {rev} failed: {old_shown.stderr.strip()}",
            unresolved=True,
        )
    return BaselineFile("found", path=old_path, content=old_shown.stdout)


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


def count_commits_ahead(project_root: Path, base_sha: str) -> int | None:
    """Return how many commits HEAD is ahead of *base_sha* (``base..HEAD``).

    Used to surface how far the index lags the working tree (``index is N
    commits behind HEAD``).  Best-effort hint only: returns ``None`` when git
    is unavailable or *base_sha* is unknown to git (e.g. a different branch).
    False-negatives are acceptable — the caller treats ``None`` as "unknown".
    """
    if not base_sha:
        return None
    raw = _run_git(["rev-list", "--count", f"{base_sha}..HEAD"], project_root)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None
