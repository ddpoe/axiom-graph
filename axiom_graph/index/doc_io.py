"""Shared write-safety primitives for DocJSON files: multi-doc locking and atomic saves.

Every path that rewrites a DocJSON file uses these two helpers, so a bulk or
multi-file write never gets weaker protection than a single-section write:

- :func:`lock_docs` holds the write lock (:func:`~axiom_graph.index.doc_lock.doc_write_lock`)
  of every doc a call writes, taken in sorted lockfile order under one
  deadline.  It is re-entrant per thread: a lock this thread already holds
  (an outer :func:`lock_docs` on the same doc) is not taken again, so a
  write that patches other docs while holding its own never waits for itself.
- :func:`save_doc_json` writes a doc dict with the one DocJSON serialisation,
  through a temp file in the same directory, ``fsync`` and ``os.replace``, so
  a failed write leaves the old file, never a truncated one.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from axiom_graph.index import doc_lock

#: Attempts at ``os.replace`` on Windows, where a reader holding the target
#: open (an editor, an indexer) makes the swap fail with ``PermissionError``.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF = 0.05

_held = threading.local()


def _held_keys() -> set[str]:
    """The lockfile keys this thread holds through :func:`lock_docs`."""
    keys = getattr(_held, "keys", None)
    if keys is None:
        keys = set()
        _held.keys = keys
    return keys


@contextmanager
def lock_docs(project_root: Path, doc_files: Iterable[Path], timeout: float | None = None) -> Iterator[None]:
    """Hold the write lock of every doc in *doc_files* for the ``with`` body.

    Files are deduplicated by their lockfile (so two spellings of one path
    take one lock) and locked in sorted lockfile order, so two multi-doc
    writers cannot deadlock.  The whole acquisition has one deadline, not one
    timeout per lock.  On timeout every lock taken so far is released before
    :class:`~axiom_graph.index.doc_lock.DocLockTimeout` propagates.  Locks
    this thread already holds through an outer ``lock_docs`` are skipped
    (re-entrant per thread) and stay held when the inner block exits.

    Args:
        project_root: Absolute project root (hosts ``.axiom_graph/locks``).
        doc_files: The DocJSON files about to be read and rewritten.
        timeout: Seconds for the whole acquisition; defaults to
            :data:`axiom_graph.index.doc_lock.DEFAULT_LOCK_TIMEOUT` (read at
            call time).

    Yields:
        Nothing; the locks are held while the body runs.

    Raises:
        DocLockTimeout: When the locks are not all taken before the deadline;
            nothing is held afterwards.
    """
    root = Path(project_root)
    wait = doc_lock.DEFAULT_LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + wait
    held = _held_keys()
    wanted: dict[str, Path] = {}
    for f in doc_files:
        key = str(doc_lock.lock_path_for(root, Path(f)))
        if key not in held:
            wanted.setdefault(key, Path(f))
    with ExitStack() as stack:
        for key in sorted(wanted):
            try:
                stack.enter_context(
                    doc_lock.doc_write_lock(root, wanted[key], timeout=max(deadline - time.monotonic(), 0.0))
                )
            except doc_lock.DocLockTimeout as exc:
                held_by = " (held by another process)" if "held by another process" in str(exc) else ""
                raise doc_lock.DocLockTimeout(
                    f"timed out after {wait:g}s waiting for the write lock on {wanted[key].name}{held_by}"
                ) from None
            held.add(key)
            stack.callback(held.discard, key)
        yield


def dumps_doc_json(data: dict) -> str:
    """Return the one DocJSON text serialisation: 2-space indent, UTF-8 kept, no trailing newline.

    Args:
        data: The DocJSON dict.

    Returns:
        The file text.
    """
    return json.dumps(data, indent=2, ensure_ascii=False)


def save_doc_json(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically: temp file in the same directory, ``fsync``, ``os.replace``.

    A failure part-way leaves *path* as it was (old content, never
    truncated) and removes the temp file.  The text is
    :func:`dumps_doc_json`, written as :meth:`pathlib.Path.write_text`
    writes text (UTF-8, platform newlines).  The replaced file keeps the
    permission bits *path* had; a new file gets the ones ``open`` would give
    it (``0o666`` less the umask).  On Windows a ``PermissionError``
    from the swap (another process has the file open) is retried a few
    times before it propagates.

    Args:
        path: Target DocJSON file; its directory must exist.
        data: The DocJSON dict.

    Raises:
        OSError: The write or the swap failed; *path* is unchanged.
    """
    path = Path(path)
    text = dumps_doc_json(data)
    try:
        existing_mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        existing_mode = None
    tmp_path = str(path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp")
    # 0o666 through the umask, as a plain open() would create the file
    # (mkstemp's 0o600 would leave every saved doc owner-only).
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
    try:
        # The file object owns fd from here, so any failure below (the chmod
        # included) closes it before the temp file is removed.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if existing_mode is not None and os.name != "nt":
                os.chmod(tmp_path, existing_mode)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        attempts = _REPLACE_ATTEMPTS if sys.platform == "win32" else 1
        for attempt in range(attempts):
            try:
                os.replace(tmp_path, str(path))
                break
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = ["dumps_doc_json", "lock_docs", "save_doc_json"]
