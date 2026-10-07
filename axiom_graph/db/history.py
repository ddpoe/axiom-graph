"""Axiom-graph DB: node history rows + since-cutoff resolution.

Covers the ``node_history`` table (``get_history``,
``get_agent_verified_nodes``, ``get_history_since``,
``resolve_since_cutoff``, ``list_reference_points``,
``filter_history_rows``, ``build_node_types_map``, ``get_history_for_resolution``,
``insert_history_row``, ``get_latest_code_change_times``,
``get_verification_ordering_rows``) and the shared effective-change rule
(``effective_change_rows_conn`` / ``get_effective_change_rows``) every
change-time reader routes through.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
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


def latest_git_shas_conn(conn, node_ids, window: int = 50) -> dict[str, str]:
    """Return each node's newest ``git_sha`` among its *window* newest history rows, in one query.

    The batched form of reading :func:`get_history` (``limit=window``) per
    node and taking the first row that carries a SHA.  Nodes with no such
    row are absent from the result.

    Args:
        conn: Open connection.
        node_ids: The nodes to look up.
        window: How many of each node's newest rows to search.

    Returns:
        ``{node_id: git_sha}``.
    """
    ids = sorted(set(node_ids))
    window = min(window, _HISTORY_ROW_LIMIT)
    out: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            "SELECT node_id, git_sha FROM ("
            "  SELECT node_id, git_sha, ROW_NUMBER() OVER (PARTITION BY node_id ORDER BY id DESC) AS rn"
            f"  FROM node_history WHERE node_id IN ({','.join('?' * len(chunk))})"
            ") WHERE rn <= ? AND git_sha IS NOT NULL AND git_sha != '' ORDER BY node_id, rn",
            (*chunk, window),
        ).fetchall()
        for node_id, sha in rows:
            out.setdefault(node_id, sha)
    return out


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


#: Minimum length of an explicit SHA reference.  Shorter prefixes are too
#: likely to collide, so they are unresolved rather than guessed.
MIN_SHA_PREFIX = 4

#: ``Resolution.source`` values — how a reference point was found.
SOURCE_CHECKPOINT = "checkpoint"
SOURCE_BUILD = "build"
SOURCE_GIT = "git-commit-time"
SOURCE_TIMESTAMP = "timestamp"
SOURCE_DEFAULT_CHECKPOINT = "default-checkpoint"
SOURCE_DEFAULT_BUILD = "default-build"
SOURCE_NONE = "none"
SOURCE_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class Resolution:
    """How a "since" reference point was resolved.

    Attributes:
        cutoff: ISO-8601 cutoff; rows with ``scanned_at > cutoff`` are in the
            window.  ``None`` for ``none`` (whole history) and ``unresolved``.
        sha: The resolved git SHA — the stored SHA for index matches, the
            full commit SHA for a git resolution, ``None`` otherwise.
        source: One of the ``SOURCE_*`` constants.
        requested_sha: The SHA the caller asked for, if any.
        reason: Why an explicit reference could not be resolved
            (only set when ``source`` is ``unresolved``).
    """

    cutoff: str | None
    sha: str | None
    source: str
    requested_sha: str | None = None
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        """True unless an explicitly requested reference could not be found."""
        return self.source != SOURCE_UNRESOLVED


class UnresolvedReferenceError(ValueError):
    """An explicitly requested reference SHA could not be resolved.

    Raised by :func:`get_history_since` / :func:`get_history_for_resolution`
    so an explicit miss can never silently widen to the whole history.

    Attributes:
        resolution: The unresolved :class:`Resolution`.
    """

    def __init__(self, resolution: Resolution) -> None:
        self.resolution = resolution
        super().__init__(f"since_sha '{resolution.requested_sha}' {resolution.reason}")


_HISTORY_COLUMNS = "id, node_id, scanned_at, change_type, git_sha, meta, preserved"


def get_history_for_resolution(
    db_path: Path,
    resolution: Resolution,
    until_timestamp: str | None = None,
) -> list[dict]:
    """Return history rows inside the window a :class:`Resolution` defines.

    Rows with ``scanned_at > resolution.cutoff`` (and ``<= until_timestamp``
    when given), newest-first.  The whole table (up to *until_timestamp*)
    is returned only when ``resolution.cutoff`` is ``None`` on a resolved
    reference — i.e. source ``none``: nothing was asked for and there was
    nothing to default to.

    Args:
        db_path: Path to the axiom-graph DB.
        resolution: Result of :func:`resolve_since_cutoff`.
        until_timestamp: Optional inclusive upper bound.

    Returns:
        History row dicts with keys id, node_id, scanned_at, change_type,
        git_sha, meta, preserved.

    Raises:
        UnresolvedReferenceError: When *resolution* is unresolved.
    """
    if not resolution.resolved:
        raise UnresolvedReferenceError(resolution)

    clauses: list[str] = []
    params: list[str] = []
    if resolution.cutoff is not None:
        clauses.append("scanned_at > ?")
        params.append(resolution.cutoff)
    if until_timestamp is not None:
        clauses.append("scanned_at <= ?")
        params.append(until_timestamp)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {_HISTORY_COLUMNS} FROM node_history {where} ORDER BY id DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def get_history_since(
    db_path: Path,
    since_timestamp: str | None = None,
    since_sha: str | None = None,
    until_timestamp: str | None = None,
    project_root: Path | None = None,
) -> list[dict]:
    """Return all history rows after a reference point, newest-first.

    Delegates to ``resolve_since_cutoff`` for reference point resolution.
    See that function's docstring for the full resolution order.

    When *until_timestamp* is provided, only rows with
    ``scanned_at <= until_timestamp`` are included, creating a bounded
    time window for range queries.

    Args:
        db_path: Path to the axiom-graph DB.
        since_timestamp: ISO-8601 cutoff.
        since_sha: Git SHA (prefix) reference.
        until_timestamp: Optional inclusive upper bound.
        project_root: Repo root for the git commit-time fallback.  Without
            it an explicit SHA must be found in the index.

    Returns:
        History row dicts (id, node_id, scanned_at, change_type, git_sha,
        meta, preserved), newest-first.

    Raises:
        UnresolvedReferenceError: When an explicit *since_sha* resolves to
            nothing — with or without *until_timestamp*.  The whole-history
            answer is reachable only when no reference was asked for.
    """
    resolution = resolve_since_cutoff(
        db_path,
        since_timestamp=since_timestamp,
        since_sha=since_sha,
        project_root=project_root,
    )
    return get_history_for_resolution(db_path, resolution, until_timestamp=until_timestamp)


# Symmetric prefix match: the shorter of (stored, requested) must be a
# prefix of the longer.  The stored side must be long enough to mean
# something — an empty or 1-3 char git_sha would otherwise match anything.
_SHA_PREFIX_MATCH = (
    f"git_sha IS NOT NULL AND length(git_sha) >= {MIN_SHA_PREFIX} "
    "AND (lower(substr(git_sha, 1, length(:sha))) = :sha "
    "OR substr(:sha, 1, length(git_sha)) = lower(git_sha))"
)


@task(
    purpose="Resolve the reference point for a since query",
    inputs="db_path, optional since_sha (git SHA prefix), optional since_timestamp (ISO-8601), "
    "optional project_root (enables the git commit-time fallback)",
    outputs="Resolution(cutoff, sha, source) — source says how it resolved, or 'unresolved' "
    "for an explicit SHA neither the index nor git can resolve",
)
def resolve_since_cutoff(
    db_path: Path,
    since_timestamp: str | None = None,
    since_sha: str | None = None,
    project_root: Path | None = None,
) -> Resolution:
    """Resolve the reference point for a "since" query.

    Resolution order:

    1. *since_sha* → match a CHECKPOINT by git_sha prefix.
    2. *since_sha* → match any history row by git_sha prefix (picks up
       INITIAL, content events, etc. — works after ``axiom-graph init``).
    3. *since_sha* unknown to the index → ask git (needs *project_root*);
       the cutoff is that commit's committer time in UTC.
    4. *since_timestamp* → use directly.
    5. Fallback (only when no *since_sha* was given) → most recent CHECKPOINT.
    6. Fallback (only when no *since_sha* was given) → most recent history row
       with a git_sha.  Nothing at all → source ``none`` (whole history).

    Prefix matching is symmetric — a 40-char SHA matches a legacy 12-char
    checkpoint and a 12-char prefix matches a 40-char build row.  An
    explicit SHA shorter than :data:`MIN_SHA_PREFIX`, or one neither the
    index nor git can resolve (git ambiguity counts as unresolvable),
    yields an ``unresolved`` :class:`Resolution`.  It never borrows a
    different baseline; :func:`get_history_since` turns it into an error.

    Args:
        db_path: Path to the axiom-graph DB.
        since_timestamp: ISO-8601 cutoff.
        since_sha: Git SHA (prefix) reference.
        project_root: Repo root for the git fallback.  Pass it explicitly;
            it is never derived from *db_path*.

    Returns:
        A :class:`Resolution`.  This function never raises for an
        unresolvable reference.
    """
    with _connect(db_path) as conn:
        if since_sha:
            sha_key = since_sha.strip().lower()
            if len(sha_key) < MIN_SHA_PREFIX:
                return Resolution(
                    None,
                    None,
                    SOURCE_UNRESOLVED,
                    requested_sha=since_sha,
                    reason=f"is shorter than the {MIN_SHA_PREFIX}-character minimum for a SHA reference",
                )

            口 = Step(
                step_num=1,
                name="Match CHECKPOINT by SHA prefix",
                purpose="Prefer explicit reference points — their timestamps have intentional meaning",
            )
            row = conn.execute(
                f"""
                SELECT scanned_at, git_sha FROM node_history
                WHERE change_type = 'CHECKPOINT' AND {_SHA_PREFIX_MATCH}
                ORDER BY id DESC LIMIT 1
                """,
                {"sha": sha_key},
            ).fetchone()
            if row:
                return Resolution(row["scanned_at"], row["git_sha"], SOURCE_CHECKPOINT, requested_sha=since_sha)

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
                f"""
                SELECT scanned_at, git_sha, change_type FROM node_history
                WHERE {_SHA_PREFIX_MATCH}
                ORDER BY id ASC LIMIT 1
                """,
                {"sha": sha_key},
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
                return Resolution(cutoff, row["git_sha"], SOURCE_BUILD, requested_sha=since_sha)

            口 = Step(
                step_num=3,
                name="Fall back to the git commit time",
                purpose="A SHA git knows but the index never recorded resolves to its committer time, "
                "on the same UTC axis history rows are ordered on",
                critical="Explicit SHA only, and only with an explicit project_root. Unknown or ambiguous "
                "to git → unresolved; never fall through to the no-argument defaults (steps 5-6)",
            )
            if any(c not in "0123456789abcdef" for c in sha_key):
                # git is only asked about hex SHAs, so a ref name gets its own reason.
                reason = (
                    "is not in node_history and is not a hex SHA prefix "
                    "(since_sha takes a commit SHA, not a ref name such as HEAD or a branch)"
                )
            elif project_root is not None:
                from axiom_graph.index.git_utils import resolve_commit

                commit = resolve_commit(Path(project_root), sha_key)
                if commit is not None:
                    full_sha, commit_ts = commit
                    return Resolution(commit_ts, full_sha, SOURCE_GIT, requested_sha=since_sha)
                reason = "is not in node_history and is not a commit in this repository"
            else:
                reason = "is not in node_history (no project root was given for a git lookup)"
            return Resolution(None, None, SOURCE_UNRESOLVED, requested_sha=since_sha, reason=reason)

        if since_timestamp:
            口 = Step(step_num=4, name="Use timestamp directly", purpose="Caller provided an explicit ISO-8601 cutoff")
            return Resolution(since_timestamp, None, SOURCE_TIMESTAMP)

        口 = Step(
            step_num=5,
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
            return Resolution(row["scanned_at"], row["git_sha"], SOURCE_DEFAULT_CHECKPOINT)

        口 = Step(
            step_num=6,
            name="Fallback: most recent row with any git SHA",
            purpose="Last resort — use the most recent indexed event that carries a commit SHA; "
            "with none, the window is the whole history",
        )
        row = conn.execute(
            """
            SELECT scanned_at, git_sha FROM node_history
            WHERE git_sha IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
        ).fetchone()
        if row:
            return Resolution(row["scanned_at"], row["git_sha"], SOURCE_DEFAULT_BUILD)

        return Resolution(None, None, SOURCE_NONE)


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
    exclude_node_pattern: str | list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """Filter history rows using glob patterns.

    Positive filters run first; *exclude_node_pattern* then drops every row
    whose ``node_id`` matches any of its globs.  Exclusion looks only at a
    row's own ``node_id`` — a kept row whose ``meta.linked_node`` points at
    an excluded node is untouched.

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
        exclude_node_pattern: Glob (or list of globs) matched against
            node_id; matching rows are removed (e.g. ``proj::docs/pev/cycles/x*``).
    """
    filtered = rows
    if change_type_pattern:
        filtered = [r for r in filtered if fnmatch.fnmatch(r["change_type"], change_type_pattern)]
    if node_pattern:
        filtered = [r for r in filtered if fnmatch.fnmatch(r["node_id"], node_pattern)]
    if node_type and node_types_map is not None:
        filtered = [r for r in filtered if node_types_map.get(r["node_id"]) == node_type]
    excludes = normalize_patterns(exclude_node_pattern)
    if excludes:
        filtered = [r for r in filtered if not any(fnmatch.fnmatch(r["node_id"], p) for p in excludes)]
    return filtered


def normalize_patterns(patterns: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Return *patterns* as a list of non-empty globs (accepts a str or a list)."""
    if not patterns:
        return []
    if isinstance(patterns, str):
        patterns = [patterns]
    return [p for p in patterns if p]


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
    with _connect(db_path) as conn:
        insert_history_row_conn(conn, node_id, change_type, git_sha=git_sha, meta=meta, preserved=preserved)


def insert_history_row_conn(
    conn,
    node_id: str,
    change_type: str,
    git_sha: str | None = None,
    meta: str | None = None,
    preserved: bool = False,
) -> None:
    """Insert a single history row on an open connection (see :func:`insert_history_row`).

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The node the row is about.
        change_type: The row's ``change_type``.
        git_sha: HEAD sha, when known.
        meta: JSON payload.
        preserved: Keep the row through history pruning.
    """
    conn.execute(
        """
        INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (node_id, _now_utc(), change_type, git_sha, meta, 1 if preserved else 0),
    )


# ---------------------------------------------------------------------------
# Effective change time — the one rule every change-time reader shares
# ---------------------------------------------------------------------------

#: Change rows a code-only reader counts (Pass 1, Pass B, the Pass 2 lookup).
CODE_CHANGE_TYPES: tuple[str, ...] = ("CONTENT_ONLY", "CONTENT_AND_DESC", "BECAME_CONTENT_UPDATED")
#: Change rows the widened (code + docstring) readers count (Pass A, reverify).
CODE_AND_DESC_CHANGE_TYPES: tuple[str, ...] = (*CODE_CHANGE_TYPES, "DESC_ONLY")

#: ``node_history.meta`` key on a ``BECAME_VERIFIED`` row: ``True`` when the
#: node's current hashes equal its stored baseline again (the change it
#: recovers from was a round trip).  Written by
#: :func:`axiom_graph.index.staleness.record_staleness` and
#: :func:`record_realigned_if_stale`.
REALIGNED_META_KEY = "realigned"

# Upsert rows that rewrite the stored hashes (the baseline) outright.
_BASELINE_WRITE_TYPES = frozenset({"INITIAL", "CONTENT_ONLY", "DESC_ONLY", "CONTENT_AND_DESC"})
_VERIFICATION_TYPES = frozenset({"AGENT_VERIFIED", "MANUAL_VERIFIED"})
_SCANNER_CHANGE_TYPE = "BECAME_CONTENT_UPDATED"
_REALIGN_TYPE = "BECAME_VERIFIED"
_RULE_ROW_TYPES = tuple(sorted(_BASELINE_WRITE_TYPES | _VERIFICATION_TYPES | {_SCANNER_CHANGE_TYPE, _REALIGN_TYPE}))


def _meta_flag(raw: str | None, key: str) -> bool | None:
    """Read a boolean flag from a ``node_history.meta`` JSON payload.

    Args:
        raw: The stored ``meta`` text, or ``None``.
        key: Flag to read.

    Returns:
        The flag's boolean value, or ``None`` when the payload is missing,
        unparsable, or does not carry the key.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw).get(key)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


def _fold_effective_change(rows: list, change_types: frozenset[str], realigned_now: bool) -> tuple[int, str] | None:
    """Apply the effective-change rule to one node's rule rows (ascending id).

    A scanner ``BECAME_CONTENT_UPDATED`` row stays *open* — measured against
    the baseline in force when it was written — until either the baseline
    moves to a different hash (the change is then committed and keeps
    counting) or the node's hashes return to that same baseline (the change
    is cancelled).  Upsert change rows rewrite the baseline themselves, so
    they are committed as soon as they are written.

    A verification row (``mark_clean`` / reverify / build baselines) is a
    baseline reset and commits the open change.  Evidence of a return to
    the baseline is recorded at write time, as a ``BECAME_VERIFIED`` row
    flagged :data:`REALIGNED_META_KEY` (written ahead of the verification
    row when a reset finds the hashes already back at the baseline).
    Legacy rows carry no flag and take the conservative default: an
    unflagged ``BECAME_VERIFIED`` cancels nothing, so the change keeps
    counting exactly as before.

    Args:
        rows: The node's rule rows (``id``, ``change_type``, ``scanned_at``,
            ``meta``) in ascending id order.
        change_types: Change rows this reader's width counts.
        realigned_now: The caller has just observed the node back at its
            baseline, ahead of the ``BECAME_VERIFIED`` row that records it.

    Returns:
        ``(history_id, scanned_at)`` of the latest change that still counts,
        or ``None`` when no change counts.
    """
    committed: tuple[int, str] | None = None
    open_change: tuple[int, str] | None = None
    for r in rows:
        ct = r["change_type"]
        if ct == _SCANNER_CHANGE_TYPE:
            if ct in change_types:
                open_change = (r["id"], r["scanned_at"])
        elif ct in _BASELINE_WRITE_TYPES:
            if open_change is not None:
                committed = open_change
                open_change = None
            if ct in change_types:
                committed = (r["id"], r["scanned_at"])
        elif ct in _VERIFICATION_TYPES:
            if open_change is not None:
                committed = open_change
                open_change = None
        elif ct == _REALIGN_TYPE:
            if _meta_flag(r["meta"], REALIGNED_META_KEY) is True:
                open_change = None
    if realigned_now:
        open_change = None
    return open_change if open_change is not None else committed


def effective_change_rows_conn(
    conn,
    node_ids: list[str] | None = None,
    *,
    include_desc: bool = False,
    realigned_now: frozenset[str] | set[str] = frozenset(),
) -> dict[str, tuple[int, str]]:
    """Return each node's latest change row that still counts as a change.

    The single source of "when did this node last change" for every
    staleness reader (Pass 1 / A / B, the Pass 2 lookup and reverify's
    ordering).  A change that ended back at the baseline it was measured
    against — a round trip or an edit then revert, with no baseline reset
    at a different hash in between — no longer counts; the node then
    reports its previous change that still counts, if any.  A change that
    a ``mark_clean`` / reverify accepted at a different hash keeps
    counting.  See :func:`_fold_effective_change` for the rule.

    Args:
        conn: Open DB connection.
        node_ids: Nodes to resolve; ``None`` resolves every node that has a
            change row.
        include_desc: Count ``DESC_ONLY`` rows too (the widened readers).
        realigned_now: Nodes the caller has observed back at their baseline
            in the current pass, before their ``BECAME_VERIFIED`` row is
            written — their open change is cancelled now rather than one
            check later.

    Returns:
        Dict mapping node_id to ``(history_id, scanned_at)``.  Nodes with no
        change that still counts are omitted.
    """
    change_types = frozenset(CODE_AND_DESC_CHANGE_TYPES if include_desc else CODE_CHANGE_TYPES)
    change_ph = ",".join("?" * len(change_types))
    rule_ph = ",".join("?" * len(_RULE_ROW_TYPES))
    if node_ids is None:
        chunks: list[list[str] | None] = [None]
    else:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return {}
        chunks = [ids[i : i + 500] for i in range(0, len(ids), 500)]

    by_node: dict[str, list] = {}
    for chunk in chunks:
        if chunk is None:
            scope_sql = f"SELECT DISTINCT node_id FROM node_history WHERE change_type IN ({change_ph})"
            scope_args: list = list(change_types)
        else:
            scope_sql = ",".join("?" * len(chunk))
            scope_args = list(chunk)
        rows = conn.execute(
            f"""
            SELECT node_id, id, change_type, scanned_at, meta
            FROM node_history
            WHERE node_id IN ({scope_sql})
              AND change_type IN ({rule_ph})
            ORDER BY node_id, id
            """,
            [*scope_args, *_RULE_ROW_TYPES],
        ).fetchall()
        for r in rows:
            by_node.setdefault(r["node_id"], []).append(r)

    result: dict[str, tuple[int, str]] = {}
    for nid, rows in by_node.items():
        eff = _fold_effective_change(rows, change_types, nid in realigned_now)
        if eff is not None:
            result[nid] = eff
    return result


def get_effective_change_rows(
    db_path: Path,
    node_ids: list[str] | None = None,
    *,
    include_desc: bool = False,
    realigned_now: frozenset[str] | set[str] = frozenset(),
) -> dict[str, tuple[int, str]]:
    """Connection-owning wrapper around :func:`effective_change_rows_conn`.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Nodes to resolve; ``None`` resolves every changed node.
        include_desc: Count ``DESC_ONLY`` rows too.
        realigned_now: Nodes observed back at their baseline this pass.

    Returns:
        Dict mapping node_id to ``(history_id, scanned_at)`` of its latest
        change that still counts.
    """
    with _connect(db_path) as conn:
        return effective_change_rows_conn(conn, node_ids, include_desc=include_desc, realigned_now=realigned_now)


def record_realigned_if_stale(db_path: Path, node_id: str, git_sha: str | None = None) -> bool:
    """Record that a stale node is back at its stored baseline hashes.

    Called by a baseline reset that finds the node's current hashes equal to
    the stored baseline while its persisted own status is still stale (an
    edit reverted before any build saw the revert).  Writes the flagged
    ``BECAME_VERIFIED`` row the scanner would have written, so the open
    change is cancelled rather than committed by the reset that follows.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: The node being reset.
        git_sha: HEAD sha recorded on the row, when known.

    Returns:
        ``True`` when a row was written.
    """
    with _connect(db_path) as conn:
        return record_realigned_if_stale_conn(conn, node_id, git_sha)


def record_realigned_if_stale_conn(conn, node_id: str, git_sha: str | None = None) -> bool:
    """:func:`record_realigned_if_stale` on an open connection.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The node being reset.
        git_sha: HEAD sha recorded on the row, when known.

    Returns:
        ``True`` when a row was written.
    """
    row = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None or row["own_status"] not in ("CONTENT_UPDATED", "DESC_UPDATED", "NOT_FOUND"):
        return False
    conn.execute(
        """
        INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            node_id,
            _now_utc(),
            _REALIGN_TYPE,
            git_sha,
            json.dumps({"from_own": row["own_status"], REALIGNED_META_KEY: True}),
        ),
    )
    return True


#: What a partial verification row's meta holds: a ``verifies`` key, whatever
#: its value (a doc edit verifies a section's text, not its links; a partial
#: carry verifies the text and/or named receipts only).  ``mark_clean`` writes
#: the key with ``json.dumps``' default separators -- keep the two in step.
_TEXT_ONLY_META_LIKE = '%"verifies": "%'


def get_latest_history_ids(
    db_path: Path, node_ids: list[str], change_types: tuple[str, ...], *, skip_text_only: bool = False
) -> dict[str, int]:
    """Return the ``node_history.id`` of each node's latest row of the given types.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Nodes to look up.
        change_types: ``change_type`` values to consider.
        skip_text_only: Leave out partial verification rows (meta carrying
            ``verifies``: a doc edit's text-only row, a partial carry): they
            are not evidence about all of a node's links.

    Returns:
        Dict mapping node_id to its latest matching history id.  Nodes with
        no matching row are omitted.
    """
    ids = list(dict.fromkeys(node_ids))
    if not ids or not change_types:
        return {}
    type_ph = ",".join("?" * len(change_types))
    text_filter = " AND COALESCE(meta, '') NOT LIKE ?" if skip_text_only else ""
    text_params = [_TEXT_ONLY_META_LIKE] if skip_text_only else []
    out: dict[str, int] = {}
    with _connect(db_path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            rows = conn.execute(
                f"""
                SELECT node_id, MAX(id) AS latest_id
                FROM node_history
                WHERE node_id IN ({",".join("?" * len(chunk))})
                  AND change_type IN ({type_ph}){text_filter}
                GROUP BY node_id
                """,
                [*chunk, *change_types, *text_params],
            ).fetchall()
            out.update({r["node_id"]: r["latest_id"] for r in rows})
    return out


def get_latest_code_change_times(db_path: Path, node_ids: list[str]) -> dict[str, str]:
    """Return the time of each node's latest code change that still counts.

    Code width (``CONTENT_ONLY`` / ``CONTENT_AND_DESC`` /
    ``BECAME_CONTENT_UPDATED``), resolved by
    :func:`effective_change_rows_conn`, so a change that ended back at its
    baseline is not reported.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Node IDs to look up.

    Returns:
        Dict mapping node_id to its effective change's scanned_at timestamp
        string.  Nodes with no change that still counts are omitted.
    """
    if not node_ids:
        return {}
    eff = get_effective_change_rows(db_path, list(node_ids))
    return {nid: at for nid, (_hid, at) in eff.items()}


def get_verification_ordering_rows(
    db_path: Path,
    node_ids: list[str],
) -> tuple[dict[str, int], dict[str, list[tuple[int, str | None]]]]:
    """Return per-node history rows for ordering verifications against changes.

    Two batched lookups over ``node_history``, both carrying the table's
    monotonic ``id`` so callers can order rows without reading a clock:

    - **latest content-bearing change that still counts** per node, over
      the widest change set the staleness passes use (``CONTENT_ONLY`` /
      ``DESC_ONLY`` / ``CONTENT_AND_DESC`` / ``BECAME_CONTENT_UPDATED``) —
      the same set as ``get_stale_annotated_nodes``, so a docstring-only
      drift that can root a dependent is not missed — resolved by
      :func:`effective_change_rows_conn`, so a round trip back to the
      baseline does not count.
    - **every verification row** (``AGENT_VERIFIED`` /
      ``MANUAL_VERIFIED``) per node, each paired with the operation
      recorded in its ``meta`` payload under ``verification_op`` (written
      by :func:`axiom_graph.index.mark_clean.mark_node_clean`).  Rows
      written before that provenance existed carry ``None``.  This
      module reports what is stored; deciding what a given operation
      value means belongs to the caller.  Partial verification rows
      (meta carrying ``verifies``: a doc edit's text-only row, a partial
      carry) are left out: they verify a node's text or named receipts,
      not everything it was checked against.

    Nodes with no matching row are omitted from the respective dict.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Node IDs to look up.

    Returns:
        ``(latest_change_ids, verification_ops)`` — the first maps
        node_id to the ``node_history.id`` of its latest content-bearing
        change; the second maps node_id to its ``(history_id,
        verification_op)`` pairs in ascending id order.
    """
    if not node_ids:
        return {}, {}
    ids = list(node_ids)
    placeholders = ",".join("?" for _ in ids)
    with _connect(db_path) as conn:
        effective = effective_change_rows_conn(conn, ids, include_desc=True)
        verify_rows = conn.execute(
            f"""
            SELECT node_id, id, meta
            FROM node_history
            WHERE node_id IN ({placeholders})
              AND change_type IN ('AGENT_VERIFIED', 'MANUAL_VERIFIED')
            ORDER BY id
            """,
            ids,
        ).fetchall()

    latest_change_ids = {nid: hid for nid, (hid, _at) in effective.items()}
    verification_ops: dict[str, list[tuple[int, str | None]]] = {}
    for r in verify_rows:
        meta: dict = {}
        if r["meta"]:
            try:
                parsed = json.loads(r["meta"])
                meta = parsed if isinstance(parsed, dict) else {}
            except Exception:
                meta = {}
        if meta.get("verifies") is not None:
            continue
        op: str | None = meta.get("verification_op")
        verification_ops.setdefault(r["node_id"], []).append((r["id"], op))
    return latest_change_ids, verification_ops


def latest_verification_rows_conn(conn, node_ids) -> dict[str, dict]:
    """Return each node's latest verification history row, read in batches.

    The latest ``AGENT_VERIFIED`` / ``MANUAL_VERIFIED`` row by id, whatever it
    verified (a full verification, a doc edit's text, a partial carry): the
    node's most recent verification, which the verification record alone does
    not name after a text-only write.

    Args:
        conn: Open connection to the index (read-only is enough).
        node_ids: The nodes.

    Returns:
        Node id -> ``{"id", "change_type", "scanned_at", "meta"}`` with
        ``meta`` parsed (``{}`` when absent or unreadable).  Nodes with no
        verification row are omitted.
    """
    ids = list(dict.fromkeys(node_ids))
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"""
            SELECT h.node_id, h.id, h.change_type, h.scanned_at, h.meta
            FROM node_history h
            JOIN (
                SELECT node_id, MAX(id) AS latest_id
                FROM node_history
                WHERE node_id IN ({",".join("?" * len(chunk))})
                  AND change_type IN ('AGENT_VERIFIED', 'MANUAL_VERIFIED')
                GROUP BY node_id
            ) latest ON latest.latest_id = h.id
            """,
            chunk,
        ).fetchall()
        for node_id, hid, change_type, scanned_at, raw in rows:
            try:
                meta = json.loads(raw) if raw else {}
            except ValueError:
                meta = {}
            out[node_id] = {
                "id": hid,
                "change_type": change_type,
                "scanned_at": scanned_at,
                "meta": meta if isinstance(meta, dict) else {},
            }
    return out


__all__ = [
    "latest_verification_rows_conn",
    "insert_history_row_conn",
    "record_realigned_if_stale_conn",
    "get_history",
    "latest_git_shas_conn",
    "get_agent_verified_nodes",
    "get_history_since",
    "get_history_for_resolution",
    "resolve_since_cutoff",
    "Resolution",
    "UnresolvedReferenceError",
    "MIN_SHA_PREFIX",
    "SOURCE_CHECKPOINT",
    "SOURCE_BUILD",
    "SOURCE_GIT",
    "SOURCE_TIMESTAMP",
    "SOURCE_DEFAULT_CHECKPOINT",
    "SOURCE_DEFAULT_BUILD",
    "SOURCE_NONE",
    "SOURCE_UNRESOLVED",
    "get_index_head_sha",
    "get_indexed_shas",
    "list_reference_points",
    "filter_history_rows",
    "normalize_patterns",
    "build_node_types_map",
    "insert_history_row",
    "CODE_CHANGE_TYPES",
    "CODE_AND_DESC_CHANGE_TYPES",
    "REALIGNED_META_KEY",
    "record_realigned_if_stale",
    "get_latest_history_ids",
    "effective_change_rows_conn",
    "get_effective_change_rows",
    "get_latest_code_change_times",
    "get_verification_ordering_rows",
]
