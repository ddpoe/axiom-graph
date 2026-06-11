"""File-state primitives shared by the scanners and the staleness engine.

This module houses the single source of truth for the *mtime fast-pass* —
the cheap "has this file changed since we last indexed it?" check that
every build/scan/staleness path performs before doing real work.

Historically the comparison was hand-rolled at seven call sites with
divergent slop handling (only the staleness site carried a ``+ 0.01``
tolerance, undocumented).  :func:`file_unchanged_since` consolidates them
onto one primitive with an explicit, named ``slop`` parameter so any
tolerance is a deliberate, self-documenting decision rather than an
accidental literal.
"""

from __future__ import annotations


def file_unchanged_since(
    stored_mtime: float | None,
    current_mtime: float,
    *,
    slop: float = 0.0,
) -> bool:
    """Return whether a file looks unchanged since it was last indexed.

    This is the mtime fast-pass: a cheap timestamp comparison used to skip
    re-scanning files whose modification time has not advanced past the
    value recorded at index time.

    Args:
        stored_mtime: The file modification time recorded in the index, or
            ``None`` if the file has never been indexed.
        current_mtime: The file's current modification time on disk.
        slop: Tolerance, in seconds, added to ``stored_mtime`` before the
            comparison.  Defaults to ``0.0`` (exact comparison — a file is
            "unchanged" only when its current mtime is at or below the
            stored value).  Callers that need a tolerance must pass it
            explicitly with a justifying comment; never bury a tolerance
            as an inline literal.

    Returns:
        ``False`` when ``stored_mtime`` is ``None`` (never indexed — always
        treat as changed).  Otherwise ``True`` when
        ``current_mtime <= stored_mtime + slop``, else ``False``.
    """
    if stored_mtime is None:
        return False
    return current_mtime <= stored_mtime + slop
