"""Axiom-graph DB: node history rows + since-cutoff resolution.

Covers the ``node_history`` table (``get_history``,
``get_agent_verified_nodes``, ``get_history_since``,
``resolve_since_cutoff``, ``list_reference_points``,
``filter_history_rows``, ``build_node_types_map``,
``insert_history_row``, ``get_latest_code_change_times``).
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timedelta
from pathlib import Path

from axiom_annotations import Step, task

from axiom_graph.db._core import (
    _HISTORY_ROW_LIMIT,
    _connect,
    _now_utc,
)


def get_history(db_path: Path, node_id: str, limit: int = 10) -> list[dict]:
    """Return history rows newest-first, up to limit (max 100).

    Each dict has keys: id, node_id, scanned_at, change_type, git_sha, meta, preserved.
    """
    limit = min(limit, _HISTORY_ROW_LIMIT)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, node_id, scanned_at, change_type, git_sha, meta, preserved
            FROM node_history
            WHERE node_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (node_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent_verified_nodes(db_path: Path) -> list[dict]:
    """Return all nodes whose most-recent non-checkpoint history row is AGENT_VERIFIED.

    Each dict: {node_id, scanned_at, code_hash, desc_hash, meta}
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT h.node_id, h.scanned_at, h.meta,
                   n.code_hash, n.desc_hash
            FROM node_history h
            JOIN nodes n ON n.id = h.node_id
            WHERE h.change_type = 'AGENT_VERIFIED'
              AND h.id = (
                  SELECT MAX(h2.id)
                  FROM node_history h2
                  WHERE h2.node_id = h.node_id
                    AND h2.change_type != 'CHECKPOINT'
              )
            """,
        ).fetchall()
        return [dict(r) for r in rows]


def get_history_since(
    db_path: Path,
    since_timestamp: str | None = None,
    since_sha: str | None = None,
    until_timestamp: str | None = None,
) -> list[dict]:
    """Return all history rows after a reference point, newest-first.

    Delegates to ``resolve_since_cutoff`` for reference point resolution.
    See that function's docstring for the full resolution order.

    When *until_timestamp* is provided, only rows with
    ``scanned_at <= until_timestamp`` are included, creating a bounded
    time window for range queries.

    Returns every ``node_history`` row with ``scanned_at > cutoff``
    (and ``scanned_at <= until_timestamp`` when given), ordered
    newest-first.  Each dict has keys: id, node_id, scanned_at,
    change_type, git_sha, meta, preserved.
    """
    cutoff, _ = resolve_since_cutoff(
        db_path,
        since_timestamp=since_timestamp,
        since_sha=since_sha,
    )

    with _connect(db_path) as conn:
        if cutoff is None and until_timestamp is None:
            # No reference point at all — return everything
            rows = conn.execute(
                """
                SELECT id, node_id, scanned_at, change_type, git_sha, meta, preserved
                FROM node_history
                ORDER BY id DESC
                """,
            ).fetchall()
        elif cutoff is None:
            # No lower bound, but upper bound
            rows = conn.execute(
                """
                SELECT id, node_id, scanned_at, change_type, git_sha, meta, preserved
                FROM node_history
                WHERE scanned_at <= ?
                ORDER BY id DESC
                """,
                (until_timestamp,),
            ).fetchall()
        elif until_timestamp is None:
            rows = conn.execute(
                """
                SELECT id, node_id, scanned_at, change_type, git_sha, meta, preserved
                FROM node_history
                WHERE scanned_at > ?
                ORDER BY id DESC
                """,
                (cutoff,),
            ).fetchall()
        else:
            # Both bounds — range query
            rows = conn.execute(
                """
                SELECT id, node_id, scanned_at, change_type, git_sha, meta, preserved
                FROM node_history
                WHERE scanned_at > ? AND scanned_at <= ?
                ORDER BY id DESC
                """,
                (cutoff, until_timestamp),
            ).fetchall()

        return [dict(r) for r in rows]


@task(
    purpose="Resolve the reference point for a since query",
    inputs="db_path, optional since_sha (git SHA prefix), optional since_timestamp (ISO-8601)",
    outputs="(cutoff_timestamp, resolved_git_sha) tuple — either may be None",
)
def resolve_since_cutoff(
    db_path: Path,
    since_timestamp: str | None = None,
    since_sha: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the reference point for a "since" query.

    Resolution order:

    1. *since_sha* → match a CHECKPOINT by git_sha prefix.
    2. *since_sha* → match any history row by git_sha prefix (picks up
       INITIAL, content events, etc. — works after ``axiom-graph init``).
    3. *since_timestamp* → use directly.
    4. Fallback (only when no *since_sha* was given) → most recent CHECKPOINT.
    5. Fallback (only when no *since_sha* was given) → most recent history row
       with a git_sha.

    An explicit *since_sha* that matches no history row returns
    ``(None, None)`` — it fails loud rather than borrowing a different
    baseline, so callers can report "not in index" instead of a count
    against the wrong reference point.

    CHECKPOINTs are preferred when available (they represent explicit
    reference points), but any git SHA in history works as a cutoff.
    This ensures the "since" filter works immediately after
    ``axiom-graph init`` without requiring a manual checkpoint.

    Returns
    -------
    tuple[str | None, str | None]
        ``(cutoff_timestamp, resolved_git_sha)``.  Either or both may be
        ``None`` when no reference could be resolved.
    """
    with _connect(db_path) as conn:
        if since_sha:
            口 = Step(
                step_num=1,
                name="Match CHECKPOINT by SHA prefix",
                purpose="Prefer explicit reference points — their timestamps have intentional meaning",
            )
            row = conn.execute(
                """
                SELECT scanned_at, git_sha FROM node_history
                WHERE change_type = 'CHECKPOINT' AND git_sha LIKE ? || '%'
                ORDER BY id DESC LIMIT 1
                """,
                (since_sha,),
            ).fetchone()
            if row:
                return row["scanned_at"], row["git_sha"]

            口 = Step(
                step_num=2,
                name="Match earliest build batch by SHA prefix",
                purpose="Find the first build that recorded this SHA, then set the cutoff "
                "to the END of that batch so the entire init/build batch is excluded "
                "and only subsequent changes are visible",
                critical="The 2-second batch window prevents swallowing later BECAME_* events "
                "that share the same SHA — too large a window hides real transitions",
            )
            row = conn.execute(
                """
                SELECT scanned_at, git_sha, change_type FROM node_history
                WHERE git_sha LIKE ? || '%'
                ORDER BY id ASC LIMIT 1
                """,
                (since_sha,),
            ).fetchone()
            if row:
                # Find the end of this build batch: the contiguous block of
                # rows with the same change_type and git_sha written within
                # ~2s of the first row.  This scopes to e.g. just the INITIAL
                # rows from init, without swallowing later BECAME_* events
                # that happen to carry the same SHA.
                first_ts = datetime.fromisoformat(row["scanned_at"])
                window_end = (first_ts + timedelta(seconds=2)).isoformat()
                batch_end = conn.execute(
                    """
                    SELECT MAX(scanned_at) as batch_end FROM node_history
                    WHERE git_sha = ?
                      AND change_type = ?
                      AND scanned_at >= ? AND scanned_at <= ?
                    """,
                    (row["git_sha"], row["change_type"], row["scanned_at"], window_end),
                ).fetchone()
                cutoff = batch_end["batch_end"] if batch_end else row["scanned_at"]
                return cutoff, row["git_sha"]

            # Explicit SHA requested but not found in node_history — fail loud.
            # Do NOT fall through to the no-arg checkpoint/any-SHA fallback
            # (steps 4-5): answering "changed since X" against a different
            # baseline is the silent lie this guards against.
            return None, None

        if since_timestamp:
            口 = Step(step_num=3, name="Use timestamp directly", purpose="Caller provided an explicit ISO-8601 cutoff")
            return since_timestamp, None

        口 = Step(
            step_num=4,
            name="Fallback: most recent CHECKPOINT",
            purpose="Default no-args resolution — find the last named reference point",
        )
        row = conn.execute(
            """
            SELECT scanned_at, git_sha FROM node_history
            WHERE change_type = 'CHECKPOINT'
            ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()
        if row:
            return row["scanned_at"], row["git_sha"]

        口 = Step(
            step_num=5,
            name="Fallback: most recent row with any git SHA",
            purpose="Last resort — use the most recent indexed event that carries a commit SHA",
        )
        row = conn.execute(
            """
            SELECT scanned_at, git_sha FROM node_history
            WHERE git_sha IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()
        if row:
            return row["scanned_at"], row["git_sha"]

        return None, None


def get_index_head_sha(db_path: Path) -> str | None:
    """Return the git SHA the index was most recently built at.

    The ``git_sha`` on the most recent ``node_history`` row — i.e. the commit
    the live index currently reflects.  Used to report how far the index lags
    the working-tree HEAD.  ``None`` when no history row carries a SHA.
    """
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT git_sha FROM node_history
            WHERE git_sha IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()
    return row["git_sha"] if row else None


def get_indexed_shas(db_path: Path) -> set[str]:
    """Return the set of distinct git SHAs present in ``node_history``.

    A commit is a valid ``since`` reference point iff it appears here.  The
    commit picker uses this to mark which commits can actually be resolved
    (the rest are faded out).
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT git_sha FROM node_history WHERE git_sha IS NOT NULL",
        ).fetchall()
    return {r["git_sha"] for r in rows}


def list_reference_points(db_path: Path) -> list[dict]:
    """Return available reference points for ``axiom-graph report --since-sha``.

    Returns a list of dicts with keys: git_sha, scanned_at, type
    ("checkpoint" or "build"), message (checkpoint message or None),
    and row_count (number of history rows with that SHA).
    """
    with _connect(db_path) as conn:
        # Checkpoints first
        checkpoints = conn.execute(
            """
            SELECT git_sha, MIN(scanned_at) as scanned_at, meta,
                   COUNT(*) as row_count
            FROM node_history
            WHERE change_type = 'CHECKPOINT' AND git_sha IS NOT NULL
            GROUP BY git_sha
            ORDER BY MIN(id) DESC
            """,
        ).fetchall()

        seen_shas: set[str] = set()
        results: list[dict] = []
        for row in checkpoints:
            sha = row["git_sha"]
            seen_shas.add(sha)
            message = None
            if row["meta"]:
                try:
                    message = json.loads(row["meta"]).get("message")
                except Exception:
                    pass
            results.append(
                {
                    "git_sha": sha,
                    "scanned_at": row["scanned_at"],
                    "type": "checkpoint",
                    "message": message,
                    "row_count": row["row_count"],
                }
            )

        # Distinct build SHAs (not already listed as checkpoints)
        builds = conn.execute(
            """
            SELECT git_sha, MIN(scanned_at) as first_seen,
                   MAX(scanned_at) as last_seen, COUNT(*) as row_count
            FROM node_history
            WHERE git_sha IS NOT NULL AND change_type != 'CHECKPOINT'
            GROUP BY git_sha
            ORDER BY MIN(id) DESC
            """,
        ).fetchall()

        for row in builds:
            sha = row["git_sha"]
            if sha in seen_shas:
                continue
            results.append(
                {
                    "git_sha": sha,
                    "scanned_at": row["first_seen"],
                    "type": "build",
                    "message": None,
                    "row_count": row["row_count"],
                }
            )

        return results


def filter_history_rows(
    rows: list[dict],
    change_type_pattern: str | None = None,
    node_pattern: str | None = None,
    node_type: str | None = None,
    node_types_map: dict[str, str] | None = None,
) -> list[dict]:
    """Filter history rows using glob patterns.

    Args:
        rows: History rows from get_history_since().
        change_type_pattern: Glob pattern matched against change_type
            (e.g. ``*STALE*``, ``LINK_*``, ``AGENT_*``).
        node_pattern: Glob pattern matched against node_id
            (e.g. ``axiom_graph::axiom_graph.viz.*``).
        node_type: Exact node type to keep (e.g. ``atomic_process``).
            Requires node_types_map.
        node_types_map: Dict mapping node_id → node_type. Built by
            the caller from ``query_nodes()`` when node_type filtering
            is requested.
    """
    filtered = rows
    if change_type_pattern:
        filtered = [r for r in filtered if fnmatch.fnmatch(r["change_type"], change_type_pattern)]
    if node_pattern:
        filtered = [r for r in filtered if fnmatch.fnmatch(r["node_id"], node_pattern)]
    if node_type and node_types_map is not None:
        filtered = [r for r in filtered if node_types_map.get(r["node_id"]) == node_type]
    return filtered


def build_node_types_map(db_path: Path) -> dict[str, str]:
    """Return a dict mapping node_id → node_type for all nodes."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT id, node_type FROM nodes").fetchall()
        return {r["id"]: r["node_type"] for r in rows}


def insert_history_row(
    db_path: Path,
    node_id: str,
    change_type: str,
    git_sha: str | None = None,
    meta: str | None = None,
    preserved: bool = False,
) -> None:
    """Insert a single history row directly (used by mark-clean and checkpoint)."""
    scanned_at = _now_utc()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, scanned_at, change_type, git_sha, meta, 1 if preserved else 0),
        )


def get_latest_code_change_times(db_path: Path, node_ids: list[str]) -> dict[str, str]:
    """Return the latest CONTENT_ONLY/CONTENT_AND_DESC scanned_at per node.

    Used by the LINKED_STALE verification filter: if a node was marked clean
    (verified_at) after all its via nodes' last code change, the LINKED_STALE
    signal should be suppressed.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Node IDs to look up.

    Returns:
        Dict mapping node_id to its latest scanned_at timestamp string.
        Nodes with no matching history rows are omitted.
    """
    if not node_ids:
        return {}
    with _connect(db_path) as conn:
        placeholders = ",".join("?" for _ in node_ids)
        rows = conn.execute(
            f"""
            SELECT node_id, MAX(scanned_at) AS latest_scanned_at
            FROM node_history
            WHERE node_id IN ({placeholders})
              AND change_type IN ('CONTENT_ONLY', 'CONTENT_AND_DESC', 'BECAME_CONTENT_UPDATED')
            GROUP BY node_id
            """,
            list(node_ids),
        ).fetchall()
        return {r["node_id"]: r["latest_scanned_at"] for r in rows}


__all__ = [
    "get_history",
    "get_agent_verified_nodes",
    "get_history_since",
    "resolve_since_cutoff",
    "get_index_head_sha",
    "get_indexed_shas",
    "list_reference_points",
    "filter_history_rows",
    "build_node_types_map",
    "insert_history_row",
    "get_latest_code_change_times",
]
