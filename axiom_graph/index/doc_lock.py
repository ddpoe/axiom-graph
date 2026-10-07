"""Per-file write lock for DocJSON documents.

Every docjson write tool does read-whole-file, mutate, write-whole-file,
re-index.  Two writers interleaving that sequence on one file lose one
writer's edit.  :func:`doc_write_lock` serialises writers per doc file.

The lock must exclude other *processes* (several MCP servers or CLI runs
can share one project) and other *threads* of the same process (the MCP
server runs tool calls on worker threads).  OS advisory locks alone do not
give both: POSIX ``flock`` is held per open file description and Windows
``msvcrt.locking`` per handle, so their behaviour between threads differs.
The lock therefore pairs an in-process ``threading.Lock`` per lockfile with
an OS lock on the lockfile itself.  The OS releases its lock when a process
dies, so a crash never leaves a permanent lock behind.

Lockfiles live under the project's git-ignored ``.axiom_graph/locks``
directory, keyed by a hash of the doc's path, so they never dirty the docs
tree.
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

#: Seconds a writer waits for a doc's lock before giving up.
DEFAULT_LOCK_TIMEOUT = 10.0

_POLL_INTERVAL = 0.05

_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


class DocLockTimeout(Exception):
    """Raised when a doc's write lock is not acquired within the timeout."""


def lock_path_for(project_root: Path, doc_file: Path) -> Path:
    """Return the lockfile path guarding *doc_file*.

    Args:
        project_root: Absolute project root.
        doc_file: The DocJSON file being written.

    Returns:
        ``{project_root}/.axiom_graph/locks/{hash}.lock``.  Paths are compared
        case-insensitively on Windows, where the filesystem is.
    """
    key = Path(doc_file).resolve().as_posix()
    if sys.platform == "win32":
        key = key.lower()
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return Path(project_root) / ".axiom_graph" / "locks" / f"{digest}.lock"


def _thread_lock_for(key: str) -> threading.Lock:
    with _registry_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _thread_locks[key] = lock
        return lock


def _try_os_lock(fh: IO[bytes]) -> bool:
    """Try once to take an exclusive OS lock on *fh*; return whether it was taken."""
    try:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl  # noqa: PLC0415

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _os_unlock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def doc_write_lock(project_root: Path, doc_file: Path, timeout: float | None = None) -> Iterator[Path]:
    """Hold the exclusive write lock for *doc_file* for the ``with`` body.

    Not re-entrant: a holder that asks for the same doc's lock again waits
    for itself and times out.

    Args:
        project_root: Absolute project root (hosts ``.axiom_graph/locks``).
        doc_file: The DocJSON file about to be read and rewritten.
        timeout: Seconds to wait; defaults to :data:`DEFAULT_LOCK_TIMEOUT`.

    Yields:
        The lockfile path.

    Raises:
        DocLockTimeout: When another thread or process holds the lock for
            longer than *timeout*.
    """
    wait = DEFAULT_LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + wait
    lock_file = lock_path_for(project_root, doc_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(str(lock_file))
    if not thread_lock.acquire(timeout=max(wait, 0)):
        raise DocLockTimeout(f"timed out after {wait:g}s waiting for the write lock on {Path(doc_file).name}")
    try:
        with open(lock_file, "a+b") as fh:
            while not _try_os_lock(fh):
                if time.monotonic() >= deadline:
                    raise DocLockTimeout(
                        f"timed out after {wait:g}s waiting for the write lock on {Path(doc_file).name} "
                        f"(held by another process)"
                    )
                time.sleep(_POLL_INTERVAL)
            try:
                yield lock_file
            finally:
                _os_unlock(fh)
    finally:
        thread_lock.release()


__all__ = ["DEFAULT_LOCK_TIMEOUT", "DocLockTimeout", "doc_write_lock", "lock_path_for"]
