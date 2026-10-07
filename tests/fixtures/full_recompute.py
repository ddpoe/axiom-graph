"""What ``check --full`` stores, read from a copy of an index.

Tests whose expectation follows a write tool's scoped refresh compare the
stored rows with a full recompute run on a copy, so a refresh that disagrees
with ``check --full`` fails the test rather than the test pinning the
refresh's answer.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

# A node's live value: its stored live hashes, or its baseline when none is
# stored (NULL / '' read as "live equals the baseline").
_COLUMNS = (
    "own_status, link_status, "
    "CASE WHEN COALESCE(live_code_hash, '') != '' THEN live_code_hash ELSE code_hash END, "
    "CASE WHEN COALESCE(live_code_hash, '') != '' THEN live_desc_hash ELSE desc_hash END"
)


def stored_rows(db_path: Path, node_ids: Iterable[str] | None = None) -> dict[str, tuple]:
    """Return ``{node_id: (own, link, live_code_hash, live_desc_hash)}`` as stored.

    Args:
        db_path: Path to the index.
        node_ids: Limit to these nodes; every node when ``None``.

    Returns:
        The stored status and live-hash columns per node.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"SELECT id, {_COLUMNS} FROM nodes").fetchall()
    wanted = None if node_ids is None else set(node_ids)
    return {r[0]: tuple(r[1:]) for r in rows if wanted is None or r[0] in wanted}


def full_recompute_rows(db_path: Path, root: Path, node_ids: Iterable[str] | None = None) -> dict[str, tuple]:
    """Return what ``check --full`` stores, run on a copy of *db_path* (the index itself is untouched).

    Args:
        db_path: Path to the index.
        root: Project root.
        node_ids: Limit to these nodes; every node when ``None``.

    Returns:
        The status and live-hash columns per node after the full recompute.
    """
    from axiom_graph.lifecycle.api import compute_check_summary  # noqa: PLC0415

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / "graph.db"
        with closing(sqlite3.connect(db_path)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst)
        compute_check_summary(copy, root, full=True)
        return stored_rows(copy, node_ids)


def assert_matches_full_recompute(db_path: Path, root: Path, node_ids: Iterable[str] | None = None) -> None:
    """Assert the stored rows equal what ``check --full`` stores (status and live hashes).

    Args:
        db_path: Path to the index.
        root: Project root.
        node_ids: Limit the comparison to these nodes; every node when ``None``.
    """
    ids = None if node_ids is None else list(node_ids)
    stored = stored_rows(db_path, ids)
    full = full_recompute_rows(db_path, root, ids)
    differing = {
        nid: (stored.get(nid), full.get(nid)) for nid in set(stored) | set(full) if stored.get(nid) != full.get(nid)
    }
    assert not differing, f"stored != check --full: {differing}"
