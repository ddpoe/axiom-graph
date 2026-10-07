"""Axiom-graph DB: node CRUD + per-node verification.

Covers nodes table reads/writes (``upsert_node``, ``get_node``,
``query_nodes``, ``all_nodes``, hash lookups, baseline updates,
single-node and multi-node deletes, children/undocumented queries) and
the ``node_verification`` table (``upsert_verification``,
``get_verification``, ``get_all_verifications``,
``update_node_baseline``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from axiom_annotations import Step, task

from axiom_graph.models import AxiomNode

from axiom_graph.db._core import (
    MISSING_LIVE_HASH,
    OPEN_RECEIPT_HASH,
    _HISTORY_ROW_LIMIT,
    _connect,
    _derive_change_type,
    _node_to_row,
    _now_utc,
    _row_to_node,
    _steps_to_json,
    pairs_ready,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hash lookups
# ---------------------------------------------------------------------------


def get_code_hash(db_path: Path, node_id: str) -> str | None:
    """Return the stored code_hash for a node, or None if not found."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT code_hash FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row["code_hash"] if row else None


# Keep old name as an alias for callers that haven't migrated yet
get_source_hash = get_code_hash


def get_node_hashes(db_path: Path, node_id: str) -> tuple[str | None, str | None]:
    """Return (code_hash, desc_hash) for a node, or (None, None) if not found."""
    with _connect(db_path) as conn:
        return _get_node_hashes_conn(conn, node_id)


def _get_node_hashes_conn(conn: sqlite3.Connection, node_id: str) -> tuple[str | None, str | None]:
    """Return (code_hash, desc_hash) using an existing connection."""
    row = conn.execute("SELECT code_hash, desc_hash FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None, None
    return row["code_hash"], row["desc_hash"]


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------


def upsert_verification(
    db_path: Path,
    node_id: str,
    verified_by: str,
    code_hash_at: str,
    desc_hash_at: str | None = None,
    reason: str | None = None,
) -> None:
    """Insert or replace a verification row for a node.

    ``verified_by`` should be ``'human'`` or ``'agent:{model}'``.
    ``code_hash_at`` and ``desc_hash_at`` snapshot the node's hashes at
    verification time.  Both must still match on subsequent staleness
    checks for the node to remain VERIFIED.
    """
    with _connect(db_path) as conn:
        upsert_verification_conn(conn, node_id, verified_by, code_hash_at, desc_hash_at, reason)


def upsert_verification_conn(
    conn: sqlite3.Connection,
    node_id: str,
    verified_by: str,
    code_hash_at: str,
    desc_hash_at: str | None = None,
    reason: str | None = None,
) -> None:
    """Insert or replace a verification row on an open connection.

    See :func:`upsert_verification`.  The REPLACE deletes the node's old
    pairs through the foreign-key cascade; a writer that records pairs
    writes them next, in the same transaction
    (:func:`replace_verification_targets_conn`).

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node.
        verified_by: Provenance (``'human'`` / ``'agent:{model}'``).
        code_hash_at: The node's code hash at verification time.
        desc_hash_at: The node's desc hash at verification time.
        reason: Free-form reason, or ``None``.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO node_verification
            (node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at)
        VALUES (?, 'VERIFIED', ?, ?, ?, ?, ?)
        """,
        (node_id, _now_utc(), verified_by, reason, code_hash_at, desc_hash_at),
    )


def update_node_baseline(
    db_path: Path,
    node_id: str,
    code_hash: str,
    desc_hash: str | None = None,
) -> None:
    """Reset the baseline hashes on the nodes table after verification.

    Called by mark_clean writers so that the next ``compute_staleness`` run
    sees baseline == current and resolves to VERIFIED directly: the file is
    re-parsed, the freshly computed hashes match the baseline written here,
    and the verification snapshot backstops the result via Step 5 promotion.

    Deliberately does NOT touch ``file_mtime``.  ``file_mtime`` is the
    builder's scan-skip cache (advanced only by a full scan in
    :func:`upsert_node_conn`), not a verification baseline.  Writing the
    current on-disk mtime here would make the builder's mtime fast-pass treat
    the file as already scanned and skip it on every later build, freezing the
    node's scan-derived fields (``level_1`` / ``level_2`` / line ranges / tags)
    and masking genuinely-stale siblings in the same file via the per-location
    ``MAX(file_mtime)`` lookup.  Leaving it untouched lets the next build
    re-scan the file and regenerate those fields.

    The node's live hashes are cleared with the baseline (``live_code_hash``
    set to ``''``, the reset marker), so readers take the new baseline as its
    live value until the next staleness pass re-hashes the file, and that
    pass may not fast-pass the file.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        node_id: The node to update.
        code_hash: Current code/body hash from the file on disk.
        desc_hash: Current desc/heading hash from the file on disk.
    """
    with _connect(db_path) as conn:
        update_node_baseline_conn(conn, node_id, code_hash, desc_hash)


def update_node_baseline_conn(
    conn: sqlite3.Connection,
    node_id: str,
    code_hash: str,
    desc_hash: str | None = None,
) -> None:
    """Reset a node's baseline hashes on an open connection.

    See :func:`update_node_baseline`.  On a schema-v5 index the live hashes
    are cleared too: ``live_code_hash = ''`` reads as "live equals the
    baseline" and marks the node as reset since its file was last re-hashed
    (:func:`get_unhashed_node_ids`).  A module or config anchor is the
    exception: its ``live_code_hash`` carries the fingerprint of the bytes
    its file's last re-hash read, not a node hash (no reset marker applies
    to it), so it keeps that fingerprint from the file's record, which is
    what a re-hash of the file (``check --full``) stores.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The node to update.
        code_hash: The new code/body baseline.
        desc_hash: The new desc/heading baseline.
    """
    if pairs_ready(conn):
        conn.execute(
            "UPDATE nodes SET code_hash = ?, desc_hash = ?, own_status = 'VERIFIED', "
            "live_code_hash = CASE WHEN node_type = 'composite_process' "
            "AND COALESCE(subtype, '') IN ('module', 'config') "
            "THEN COALESCE((SELECT f.hashed_fp FROM file_state f WHERE f.location = nodes.location), '') "
            "ELSE '' END, live_desc_hash = NULL WHERE id = ?",
            (code_hash, desc_hash, node_id),
        )
    else:
        conn.execute(
            "UPDATE nodes SET code_hash = ?, desc_hash = ?, own_status = 'VERIFIED' WHERE id = ?",
            (code_hash, desc_hash, node_id),
        )


# ---------------------------------------------------------------------------
# Live hashes and verification pairs (schema v5)
# ---------------------------------------------------------------------------


def load_live_view_conn(
    conn: sqlite3.Connection,
) -> tuple[dict[str, tuple[str | None, str | None]], set[str]]:
    """Return every node's live hashes as the index last saw them, plus the NOT_FOUND ids.

    The DB-only view the pair readers compare against: a node's stored live
    pair when its ``live_code_hash`` is set, else its baseline pair (the
    live pair is read together, so a NULL live desc hash stays a real
    value).  A node with no code hash is left out.  On an index below
    schema v5 every node reads at its baseline.

    Args:
        conn: Open SQLite connection.

    Returns:
        ``(hashes, not_found)``: node id -> ``(code_hash, desc_hash)``, and
        the ids whose stored ``own_status`` is ``NOT_FOUND``.
    """
    hashes: dict[str, tuple[str | None, str | None]] = {}
    not_found: set[str] = set()
    if pairs_ready(conn):
        rows = conn.execute("SELECT id, own_status, code_hash, desc_hash, live_code_hash, live_desc_hash FROM nodes")
        for r in rows:
            if r["own_status"] == "NOT_FOUND":
                not_found.add(r["id"])
            if r["live_code_hash"] and r["live_code_hash"] != MISSING_LIVE_HASH:
                hashes[r["id"]] = (r["live_code_hash"], r["live_desc_hash"])
            elif r["code_hash"]:
                hashes[r["id"]] = (r["code_hash"], r["desc_hash"])
    else:
        for r in conn.execute("SELECT id, own_status, code_hash, desc_hash FROM nodes"):
            if r["own_status"] == "NOT_FOUND":
                not_found.add(r["id"])
            if r["code_hash"]:
                hashes[r["id"]] = (r["code_hash"], r["desc_hash"])
    return hashes, not_found


def get_live_rows_conn(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, dict]:
    """Return what the staleness engine reads of each node to judge it without a parse.

    Args:
        conn: Open SQLite connection.
        node_ids: The nodes.

    Returns:
        Node id -> ``{node_type, subtype, location, own_status, link_status,
        code_hash, desc_hash, live_code_hash, live_desc_hash}`` (the live
        columns read as ``None`` below schema v5).
    """
    ids = list(dict.fromkeys(node_ids))
    v5 = pairs_ready(conn)
    live_cols = "live_code_hash, live_desc_hash" if v5 else "NULL AS live_code_hash, NULL AS live_desc_hash"
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for r in conn.execute(
            "SELECT id, node_type, subtype, location, own_status, link_status, code_hash, desc_hash, "
            f"{live_cols} FROM nodes WHERE id IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            out[r["id"]] = dict(r)
    return out


def index_has_nodes_conn(conn: sqlite3.Connection) -> bool:
    """Return whether the index holds any node row (one index step, not a count)."""
    return conn.execute("SELECT MAX(rowid) FROM nodes").fetchone()[0] is not None


def get_liveness_rows_conn(
    conn: sqlite3.Connection,
    *,
    node_ids: Iterable[str] = (),
    locations: Iterable[str] = (),
) -> dict[str, tuple[str, str | None, str | None]]:
    """Return what the live-node rule reads of the given nodes and of every node at the given files.

    Both reads are batched point lookups (``id`` primary key, the
    ``location`` index), so the cost follows the ids and files asked about,
    not the size of the index.

    Args:
        conn: Open SQLite connection.
        node_ids: Node ids to read.
        locations: Stored ``location`` values whose every node is read.

    Returns:
        Node id -> ``(node_type, location, own_status)`` for each row found;
        an id with no row is absent.
    """
    out: dict[str, tuple[str, str | None, str | None]] = {}
    for column, values in (("id", list(dict.fromkeys(node_ids))), ("location", list(dict.fromkeys(locations)))):
        for start in range(0, len(values), 500):
            chunk = values[start : start + 500]
            for r in conn.execute(
                f"SELECT id, node_type, location, own_status FROM nodes WHERE {column} IN ({','.join('?' * len(chunk))})",  # noqa: S608 - fixed column, placeholders only
                chunk,
            ):
                out[r["id"]] = (r["node_type"], r["location"], r["own_status"])
    return out


def get_reset_locations_conn(conn: sqlite3.Connection, locations: Iterable[str] | None = None) -> set[str]:
    """Return the files holding a node whose baseline a verification reset since its last re-hash.

    Args:
        conn: Open SQLite connection.
        locations: Only these files; ``None`` for every file.

    Returns:
        The locations (empty below schema v5).
    """
    if not pairs_ready(conn):
        return set()
    sql = """
        SELECT DISTINCT location FROM nodes
        WHERE live_code_hash = '' AND code_hash != ''
          AND ((node_type = 'atomic_process' AND COALESCE(subtype, '') NOT IN ('step', 'autostep'))
               OR (node_type = 'composite_process' AND subtype IN ('docjson', 'docjson_doc', 'workflow', 'task')))
    """
    if locations is None:
        return {r[0] for r in conn.execute(sql)}
    locs = list(dict.fromkeys(locations))
    out: set[str] = set()
    for start in range(0, len(locs), 500):
        chunk = locs[start : start + 500]
        out.update(r[0] for r in conn.execute(f"{sql} AND location IN ({','.join('?' * len(chunk))})", chunk))
    return out


def get_verifications_for_conn(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, dict]:
    """Return the verification rows of *node_ids* (as :func:`get_all_verifications`, for a few nodes).

    Args:
        conn: Open SQLite connection.
        node_ids: The nodes.

    Returns:
        Node id -> its verification row; nodes without one are omitted.
    """
    ids = list(dict.fromkeys(node_ids))
    out: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for r in conn.execute(
            "SELECT node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at "
            f"FROM node_verification WHERE node_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            out[r["node_id"]] = dict(r)
    return out


def get_verification_targets_for_conn(
    conn: sqlite3.Connection, node_ids: Iterable[str]
) -> dict[str, dict[str, tuple[str, str | None]]]:
    """Return the recorded pairs of *node_ids* (as :func:`get_all_verification_targets_conn`, for a few nodes).

    Args:
        conn: Open SQLite connection to a schema-v5 index.
        node_ids: The verified nodes.

    Returns:
        Verified node id -> target id -> recorded ``(code_hash, desc_hash)``.
    """
    ids = list(dict.fromkeys(node_ids))
    pairs: dict[str, dict[str, tuple[str, str | None]]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for r in conn.execute(
            "SELECT node_id, target_id, code_hash, desc_hash FROM node_verification_targets "
            f"WHERE node_id IN ({','.join('?' * len(chunk))})",
            chunk,
        ):
            pairs.setdefault(r["node_id"], {})[r["target_id"]] = (r["code_hash"], r["desc_hash"])
    return pairs


def get_unhashed_node_ids(db_path: Path) -> set[str]:
    """Return the nodes whose baseline a verification reset since a pass last re-hashed them.

    A verification resets a node's baseline to the node's current hash and
    marks its live hash ``''``; the next staleness pass that re-hashes the
    file stores a real one.  Until then the file-level anchor may still
    fingerprint an older version of the file than the node's baseline (a
    file reverted to that version would match it), so the engine's mtime
    fast pass must not vouch for the location.  Empty below schema v5.

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        Ids of hashed node kinds (atomic nodes other than step views, and
        DocJSON / workflow / task composites) carrying the reset marker.
    """
    with _connect(db_path) as conn:
        if not pairs_ready(conn):
            return set()
        rows = conn.execute(
            """
            SELECT id FROM nodes
            WHERE live_code_hash = '' AND code_hash != ''
              AND ((node_type = 'atomic_process' AND COALESCE(subtype, '') NOT IN ('step', 'autostep'))
                   OR (node_type = 'composite_process' AND subtype IN ('docjson', 'docjson_doc', 'workflow', 'task')))
            """
        ).fetchall()
    return {r["id"] for r in rows}


def get_last_hashed_fingerprints(db_path: Path, anchor_subtypes: Iterable[str]) -> dict[str, str]:
    """Return the whole-file fingerprint each file-level anchor was last re-hashed at.

    A staleness pass that re-hashes a file stores the hash of the file
    content it read as the live hash of the file's anchor node (the module,
    DocJSON or config node whose ``code_hash`` fingerprints the whole file
    as it was scanned).  A verification that reset the anchor's own
    baseline leaves the reset marker ``''`` there instead.  An anchor no
    pass has re-hashed has no entry.  Empty below schema v5.

    Args:
        db_path: Path to the axiom-graph DB.
        anchor_subtypes: The subtypes that mark a file-level anchor.

    Returns:
        Anchor id -> its stored live code hash (``''`` for the reset marker).
    """
    subtypes = sorted(set(anchor_subtypes))
    if not subtypes:
        return {}
    with _connect(db_path) as conn:
        if not pairs_ready(conn):
            return {}
        placeholders = ",".join("?" * len(subtypes))
        rows = conn.execute(
            f"SELECT id, live_code_hash FROM nodes WHERE live_code_hash IS NOT NULL AND subtype IN ({placeholders})",
            subtypes,
        ).fetchall()
    return {r["id"]: r["live_code_hash"] for r in rows}


def get_all_verification_targets_conn(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, tuple[str, str | None]]]:
    """Return every recorded pair, grouped by the verified node (one query).

    Args:
        conn: Open SQLite connection to a schema-v5 index.

    Returns:
        Verified node id -> target id -> the ``(code_hash, desc_hash)`` its
        verification recorded.
    """
    pairs: dict[str, dict[str, tuple[str, str | None]]] = {}
    for r in conn.execute("SELECT node_id, target_id, code_hash, desc_hash FROM node_verification_targets"):
        pairs.setdefault(r["node_id"], {})[r["target_id"]] = (r["code_hash"], r["desc_hash"])
    return pairs


def get_verification_targets(db_path: Path, node_id: str) -> dict[str, tuple[str, str | None]]:
    """Return the pairs one node's verification recorded.

    Args:
        db_path: Path to the axiom-graph DB.
        node_id: The verified node.

    Returns:
        Target id -> recorded ``(code_hash, desc_hash)``; empty when the node
        has no pairs or the index is below schema v5.
    """
    with _connect(db_path) as conn:
        if not pairs_ready(conn):
            return {}
        rows = conn.execute(
            "SELECT target_id, code_hash, desc_hash FROM node_verification_targets WHERE node_id = ?",
            (node_id,),
        ).fetchall()
    return {r["target_id"]: (r["code_hash"], r["desc_hash"]) for r in rows}


def replace_verification_targets_conn(
    conn: sqlite3.Connection,
    node_id: str,
    pairs: dict[str, tuple[str, str | None]],
) -> None:
    """Replace a verified node's pair set inside the caller's transaction.

    Called right after the node's verification row is written, so the row
    and its pairs commit together.  The old set is deleted explicitly rather
    than relying on a REPLACE of the verification row to cascade.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node; its ``node_verification`` row must exist.
        pairs: Target id -> ``(code_hash, desc_hash)`` to record.
    """
    conn.execute("DELETE FROM node_verification_targets WHERE node_id = ?", (node_id,))
    if pairs:
        conn.executemany(
            "INSERT INTO node_verification_targets (node_id, target_id, code_hash, desc_hash) VALUES (?, ?, ?, ?)",
            [(node_id, target, code, desc) for target, (code, desc) in pairs.items()],
        )


def update_verification_snapshot_conn(
    conn: sqlite3.Connection,
    node_id: str,
    code_hash_at: str | None,
    desc_hash_at: str | None,
) -> bool:
    """Update only the snapshot hashes of a node's verification row (a text-only verification).

    ``verified_at``, ``verified_by``, ``reason`` and the recorded pairs keep
    describing the last verification of the node's links.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node.
        code_hash_at: The node's code hash now.
        desc_hash_at: The node's desc hash now.

    Returns:
        Whether the node had a verification row to update.
    """
    cur = conn.execute(
        "UPDATE node_verification SET code_hash_at = ?, desc_hash_at = ? WHERE node_id = ?",
        (code_hash_at, desc_hash_at, node_id),
    )
    return cur.rowcount > 0


def touch_verification_conn(
    conn: sqlite3.Connection,
    node_id: str,
    verified_by: str,
    reason: str | None,
) -> bool:
    """Move a verification row's time, verifier and reason to now, keeping its snapshot hashes.

    What a link-only verification writes on an existing row: ``verified_at``,
    ``verified_by`` and ``reason`` describe the latest verification of the
    node's links, while ``code_hash_at`` / ``desc_hash_at`` keep describing
    the last verification of its own content.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node.
        verified_by: Provenance (``'human'`` / ``'agent:{model}'``).
        reason: Free-form reason, or ``None``.

    Returns:
        Whether the node had a verification row to update.
    """
    cur = conn.execute(
        "UPDATE node_verification SET verified_at = ?, verified_by = ?, reason = ? WHERE node_id = ?",
        (_now_utc(), verified_by, reason, node_id),
    )
    return cur.rowcount > 0


def refresh_verification_targets_conn(
    conn: sqlite3.Connection,
    node_id: str,
    pairs: dict[str, tuple[str, str | None]],
) -> None:
    """Replace the recorded pairs of the named targets only; every other pair of the node is kept.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node; its ``node_verification`` row must exist.
        pairs: Target id -> ``(code_hash, desc_hash)`` to record.
    """
    if not pairs:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO node_verification_targets (node_id, target_id, code_hash, desc_hash) VALUES (?, ?, ?, ?)",
        [(node_id, target, code, desc) for target, (code, desc) in sorted(pairs.items())],
    )


def pin_verification_targets_conn(conn: sqlite3.Connection, node_id: str, target_ids) -> None:
    """Record an open receipt (:data:`OPEN_RECEIPT_HASH`) for each target that holds no pair yet.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_id: The verified node; its ``node_verification`` row must exist.
        target_ids: The open offenders to keep open.
    """
    ids = sorted(set(target_ids))
    if not ids:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO node_verification_targets (node_id, target_id, code_hash, desc_hash) "
        "VALUES (?, ?, ?, NULL)",
        [(node_id, target, OPEN_RECEIPT_HASH) for target in ids],
    )


def rekey_verification_targets_conn(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Point every pair recorded against *old_id* at *new_id*, hashes unchanged.

    The one target-rekey helper every rename path calls.  A verified node
    that already holds a pair for *new_id* keeps it, and its leftover pair
    for *old_id* is dropped.  A no-op below schema v5.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        old_id: The target's id before the rename.
        new_id: The target's id after it.
    """
    if old_id == new_id or not pairs_ready(conn):
        return
    conn.execute(
        "UPDATE OR IGNORE node_verification_targets SET target_id = ? WHERE target_id = ?",
        (new_id, old_id),
    )
    conn.execute("DELETE FROM node_verification_targets WHERE target_id = ?", (old_id,))


def write_baseline_verifications_conn(
    conn: sqlite3.Connection,
    node_ids: list[str],
    *,
    verified_by: str,
    verification_op: str,
    reason: str,
    git_sha: str | None = None,
    pairs_for: Callable[[sqlite3.Connection, str], dict[str, tuple[str, str | None]] | None] | None = None,
) -> list[str]:
    """Verify freshly indexed nodes against the hashes the index just stored.

    For each id, writes one ``node_verification`` row whose snapshot hashes
    are the node row's current ``code_hash`` / ``desc_hash`` (no file is
    re-parsed) and one preserved ``AGENT_VERIFIED`` history row whose
    ``meta`` carries *reason* and ``verification_op``.  An id that already
    has a verification row, or has no node row, is skipped: this helper
    never overwrites a real verification.

    When *pairs_for* is given, each written verification also records its
    pairs (one per dependency target) in the same transaction.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        node_ids: Node ids to baseline, in order; duplicates are written once.
        verified_by: Provenance recorded on the verification row.
        verification_op: Operation recorded in the history row's ``meta``.
        reason: Reason recorded on both rows.
        git_sha: HEAD sha recorded on the history row, when known.
        pairs_for: ``(conn, node_id) -> pairs`` giving the pairs to record
            for a node (``None`` records none), e.g.
            :meth:`axiom_graph.index.mark_clean.PairRecorder.pairs_for`.

    Returns:
        The ids that received a verification row.
    """
    written: list[str] = []
    now = _now_utc()
    meta = json.dumps({"reason": reason, "verification_op": verification_op})
    ids = list(dict.fromkeys(node_ids))
    # Both lookups read once per chunk of ids.  The loop's own writes touch
    # neither: no node row changes, and an id's verification row is written
    # only after its own check (the ids are distinct).
    hashes: dict[str, tuple[str | None, str | None]] = {}
    verified: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(f"SELECT id, code_hash, desc_hash FROM nodes WHERE id IN ({marks})", chunk):
            hashes[r[0]] = (r[1], r[2])
        verified.update(
            r[0] for r in conn.execute(f"SELECT node_id FROM node_verification WHERE node_id IN ({marks})", chunk)
        )
    for node_id in ids:
        code_hash, desc_hash = hashes.get(node_id, (None, None))
        if code_hash is None:
            continue
        if node_id in verified:
            continue
        conn.execute(
            """
            INSERT INTO node_verification
                (node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at)
            VALUES (?, 'VERIFIED', ?, ?, ?, ?, ?)
            """,
            (node_id, now, verified_by, reason, code_hash, desc_hash),
        )
        node_pairs = pairs_for(conn, node_id) if pairs_for is not None else None
        if node_pairs is not None:
            replace_verification_targets_conn(conn, node_id, node_pairs)
        conn.execute(
            """
            INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
            VALUES (?, ?, 'AGENT_VERIFIED', ?, ?, 1)
            """,
            (node_id, now, git_sha, meta),
        )
        written.append(node_id)
    return written


def restamp_verifications(db_path: Path, node_ids: list[str], verified_by: str) -> int:
    """Move ``verified_at`` to now for the given ids, if still carrying *verified_by*.

    Keyed by the explicit id list first: a row is touched only when its id
    is listed **and** its ``verified_by`` still equals *verified_by*, so a
    real verification that replaced the row in between is never rewritten
    and rows written by earlier builds are never selected.  History is not
    touched.

    Args:
        db_path: Path to the axiom-graph DB.
        node_ids: Ids this operation wrote verification rows for.
        verified_by: The provenance those rows were written with.

    Returns:
        Number of rows re-stamped.
    """
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        return 0
    now = _now_utc()
    total = 0
    with _connect(db_path) as conn:
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            cur = conn.execute(
                f"UPDATE node_verification SET verified_at = ? "
                f"WHERE verified_by = ? AND node_id IN ({','.join('?' * len(chunk))})",
                [now, verified_by, *chunk],
            )
            total += cur.rowcount
    return total


def get_verification(db_path: Path, node_id: str) -> dict | None:
    """Return the verification row for a single node, or None if absent."""
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at
            FROM node_verification
            WHERE node_id = ?
            """,
            (node_id,),
        ).fetchone()
        return dict(row) if row else None


@task(
    purpose="Load all verification snapshots (code_hash_at, desc_hash_at) keyed by node_id for promotion checks",
    inputs="db_path",
    outputs="Dict mapping node_id to verification row (status, verified_at, code_hash_at, desc_hash_at, etc.)",
)
def get_all_verifications(db_path: Path) -> dict[str, dict]:
    """Return all verification rows as a dict keyed by node_id (single query)."""
    with _connect(db_path) as conn:
        return get_all_verifications_conn(conn)


def get_all_verifications_conn(conn: sqlite3.Connection) -> dict[str, dict]:
    """Return all verification rows as a dict keyed by node_id (single query), on an open connection.

    Args:
        conn: Open SQLite connection.

    Returns:
        node_id -> verification row as a dict.
    """
    rows = conn.execute(
        """
        SELECT node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at
        FROM node_verification
        """
    ).fetchall()
    return {row["node_id"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# Upsert node
# ---------------------------------------------------------------------------


def upsert_node(
    db_path: Path,
    node: AxiomNode,
    discovery_only: bool = True,
    git_sha: str | None = None,
) -> bool:
    """Insert or replace a node. Returns True if a content change occurred.

    Thin wrapper around :func:`upsert_node_conn` that opens its own connection.
    Prefer ``upsert_node_conn`` when batching multiple upserts.
    """
    with _connect(db_path) as conn:
        return upsert_node_conn(conn, node, discovery_only=discovery_only, git_sha=git_sha)


@task(
    purpose="Compare code_hash/desc_hash against stored values, skip unchanged nodes, write history row on change; in discovery_only mode preserve staleness baseline while refreshing structural metadata",
    inputs="conn (open SQLite connection), AxiomNode, discovery_only flag",
    outputs="True if a content change occurred (new node or hash changed), False otherwise",
)
def upsert_node_conn(
    conn: sqlite3.Connection,
    node: AxiomNode,
    discovery_only: bool = True,
    git_sha: str | None = None,
) -> bool:
    """Insert or replace a node using an existing connection.

    Returns True if a content change occurred.

    When ``discovery_only=True``, existing nodes preserve their ``code_hash``,
    ``desc_hash``, and ``updated_at`` (staleness baseline stays intact) but
    structural metadata (location, line numbers, dflow_meta, source, title,
    node_type, subtype) is still refreshed.  FTS is re-synced only when the
    node's ``level_1`` or ``level_2`` text has actually changed, avoiding
    unnecessary DELETE+INSERT churn on no-op builds.  Tags are re-synced on a
    stored-vs-incoming **set comparison** instead, because stored text is not
    a reliable proxy for tag change: a DocJSON envelope's ``level_2`` holds
    only the first 4000 characters of its file, and a tag-only section edit
    changes no section text at all.
    """
    口 = Step(
        step_num=1,
        name="Compare hashes against stored values",
        purpose="Fetch existing code_hash/desc_hash to determine if node is new, changed, or unchanged; for an existing node in "
        "discovery_only mode, refresh structural metadata, resync tags on a set comparison, and resync FTS on text change",
        critical="In discovery_only mode, existing code_hash/desc_hash are preserved — this is the core staleness invariant. "
        "Breaking this (e.g. overwriting hashes) silently resets the staleness baseline for all existing nodes. "
        "Tag resync is decided independently, by set comparison: stored text is not a proxy for tag change, and gating "
        "tags on it silently strands the tag rows of any document whose tags sit past the stored 4000-character prefix.",
    )
    old_code, old_desc = _get_node_hashes_conn(conn, node.id)
    if discovery_only and old_code is not None:
        # Node exists — preserve staleness baseline (code_hash, desc_hash,
        # updated_at stay unchanged) but refresh structural metadata that
        # drifts when lines are added/removed elsewhere in the file.

        # Check if level_1/level_2 differ BEFORE the UPDATE overwrites them.
        stored = conn.execute("SELECT level_1, level_2 FROM nodes WHERE id = ?", (node.id,)).fetchone()
        text_changed = (
            stored is None or stored["level_1"] != node.level_1 or (stored["level_2"] or "") != (node.level_2 or "")
        )

        conn.execute(
            """
            UPDATE nodes SET
                location = ?, level_3_location = ?, level_steps = ?,
                level_0 = ?, level_1 = ?, level_2 = ?,
                dflow_meta = ?, source = ?,
                title = ?, node_type = ?, subtype = ?,
                doc_position = ?, doc_level = ?
            WHERE id = ?
            """,
            (
                node.location,
                node.level_3_location,
                _steps_to_json(node.level_steps),
                node.level_0,
                node.level_1,
                node.level_2 or "",
                json.dumps(node.dflow_meta) if node.dflow_meta else None,
                node.source,
                node.title,
                node.node_type,
                node.subtype,
                node.doc_position,
                node.doc_level,
                node.id,
            ),
        )
        # DocJSON section carve-out (ADR-021): sections are first-class
        # nodes whose level_1/level_2 mirror the CURRENT file content, so
        # desc_hash (the content-mirror hash) and updated_at (last-edit
        # timestamp) must advance with them.  Only code_hash stays behind
        # as the staleness baseline — the comparator's CONTENT_UPDATED
        # signal comes from code_hash vs current content hash.  This is a
        # content-mirror rule about section text; it says nothing about tags.
        if node.subtype == "docjson_section" and text_changed:
            conn.execute(
                "UPDATE nodes SET desc_hash = ?, updated_at = ? WHERE id = ?",
                (node.desc_hash, _now_utc(), node.id),
            )

        # Tags resync on a stored-vs-incoming set comparison, for EVERY node.
        # Text change is not a usable proxy: a DocJSON envelope's level_2 is
        # only the first 4000 characters of its file, so a tag edit further
        # down the file leaves the stored text byte-identical while the tags
        # differ — and a tag-only section edit never changes section text at
        # all.  The comparison runs in both directions, so a removed tag
        # loses its row as surely as an added one gains one.
        stored_tags = {r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (node.id,)).fetchall()}
        if stored_tags != set(node.tags or []):
            conn.execute("DELETE FROM tags WHERE node_id = ?", (node.id,))
            for tag in node.tags or []:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (node_id, tag) VALUES (?, ?)",
                    (node.id, tag),
                )
        if text_changed:
            conn.execute("DELETE FROM node_fts WHERE id = ?", (node.id,))
            conn.execute(
                "INSERT INTO node_fts (id, level_1, level_2) VALUES (?, ?, ?)",
                (node.id, node.level_1, node.level_2 or ""),
            )
        return False  # no content change — staleness preserved

    口 = Step(
        step_num=2,
        name="Upsert node row with full hash reset",
        purpose="Derive change_type, INSERT OR REPLACE node row, sync tags and FTS",
    )
    change_type = _derive_change_type(old_code, old_desc, node.code_hash, node.desc_hash)
    if change_type is None:
        return False  # unchanged — skip write

    row = _node_to_row(node)
    scanned_at = _now_utc()

    conn.execute(
        """
        INSERT OR REPLACE INTO nodes
            (id, node_type, subtype, title, location, status, source,
             code_hash, desc_hash, file_mtime,
             level_0, level_1, level_2,
             level_3_location, level_steps, dflow_meta,
             doc_position, doc_level, updated_at)
        VALUES
            (:id, :node_type, :subtype, :title, :location, :status, :source,
             :code_hash, :desc_hash, :file_mtime,
             :level_0, :level_1, :level_2,
             :level_3_location, :level_steps, :dflow_meta,
             :doc_position, :doc_level, :updated_at)
        """,
        row,
    )
    # sync tags: delete old, insert new
    conn.execute("DELETE FROM tags WHERE node_id = ?", (node.id,))
    for tag in node.tags or []:
        conn.execute(
            "INSERT OR IGNORE INTO tags (node_id, tag) VALUES (?, ?)",
            (node.id, tag),
        )
    # sync FTS: delete old entry (if any) then insert fresh
    conn.execute("DELETE FROM node_fts WHERE id = ?", (node.id,))
    conn.execute(
        "INSERT INTO node_fts (id, level_1, level_2) VALUES (?, ?, ?)",
        (node.id, node.level_1, node.level_2 or ""),
    )

    口 = Step(
        step_num=3,
        name="Record history row on change",
        purpose="Insert node_history row with change_type and prune old non-preserved rows",
    )
    # Insert history row
    conn.execute(
        """
        INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
        VALUES (?, ?, ?, ?, NULL, 0)
        """,
        (node.id, scanned_at, change_type, git_sha),
    )

    # Prune ordinary rows (preserved=0) exceeding the limit
    conn.execute(
        """
        DELETE FROM node_history
        WHERE node_id = ?
          AND preserved = 0
          AND id NOT IN (
              SELECT id FROM node_history
              WHERE node_id = ? AND preserved = 0
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (node.id, node.id, _HISTORY_ROW_LIMIT),
    )

    return True


# ---------------------------------------------------------------------------
# Simple reads
# ---------------------------------------------------------------------------


def get_node(db_path: Path, node_id: str) -> AxiomNode | None:
    """Return a single node by id, with tags populated."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        node = _row_to_node(row)
        tags = conn.execute("SELECT tag FROM tags WHERE node_id = ?", (node_id,)).fetchall()
        node.tags = [t["tag"] for t in tags]
        return node


def get_nodes_conn(conn: sqlite3.Connection, node_ids: Iterable[str]) -> dict[str, AxiomNode]:
    """Return the nodes of *node_ids* that exist, with tags populated, in batched reads.

    Args:
        conn: Open SQLite connection.
        node_ids: The ids to read; duplicates are read once.

    Returns:
        Node id -> :class:`AxiomNode` (as :func:`get_node` returns it).
    """
    ids = list(dict.fromkeys(node_ids))
    out: dict[str, AxiomNode] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM nodes WHERE id IN ({marks})", chunk):
            out[row["id"]] = _row_to_node(row)
        for t in conn.execute(f"SELECT node_id, tag FROM tags WHERE node_id IN ({marks}) ORDER BY rowid", chunk):
            if t["node_id"] in out:
                out[t["node_id"]].tags.append(t["tag"])
    return out


def query_nodes(
    db_path: Path,
    node_type: str | None = None,
    tag: str | None = None,
) -> list[AxiomNode]:
    """Return nodes filtered by node_type and/or tag."""
    with _connect(db_path) as conn:
        if tag:
            rows = conn.execute(
                """
                SELECT n.* FROM nodes n
                JOIN tags t ON t.node_id = n.id
                WHERE t.tag = ?
                  AND (? IS NULL OR n.node_type = ?)
                """,
                (tag, node_type, node_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE (? IS NULL OR node_type = ?)",
                (node_type, node_type),
            ).fetchall()
        return [_row_to_node(r) for r in rows]


def query_children(
    db_path: Path,
    parent_id: str,
) -> list[AxiomNode]:
    """Return all nodes directly composed by *parent_id* (one-hop composes edges)."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.* FROM nodes n
            JOIN edges e ON e.to_id = n.id
            WHERE e.from_id = ? AND e.edge_type = 'composes'
            """,
            (parent_id,),
        ).fetchall()
        return [_row_to_node(r) for r in rows]


def all_nodes(db_path: Path) -> list[AxiomNode]:
    """Return every node in the DB."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM nodes").fetchall()
        return [_row_to_node(r) for r in rows]


def get_undocumented_nodes(
    db_path: Path,
    node_type: str | None = None,
) -> list[AxiomNode]:
    """Return nodes that have no inbound 'documents' edge.

    An "undocumented" node is one where no doc-section node has a
    ``documents`` edge pointing at it.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.* FROM nodes n
            WHERE NOT EXISTS (
                SELECT 1 FROM edges e
                WHERE e.to_id = n.id AND e.edge_type = 'documents'
            )
            AND (? IS NULL OR n.node_type = ?)
            AND NOT (n.node_type = 'atomic_process' AND COALESCE(n.subtype, '') IN ('docjson', 'docjson_section'))
            """,
            (node_type, node_type),
        ).fetchall()
        return [_row_to_node(r) for r in rows]


# ---------------------------------------------------------------------------
# Workflow step rows
# ---------------------------------------------------------------------------

# Every marker kind that is stored as a workflow step row.  Keyed on
# ``subtype`` alone, never on ``source``, so a step emitted by a future
# JS/TS or state-machine scanner is read by the same queries as one the
# Python AST scanner emitted.
STEP_NODE_SUBTYPES: tuple[str, ...] = ("step", "autostep")

# SQLite's default host-parameter ceiling is 999; stay well inside it.
_LOCATION_CHUNK = 400


def get_step_node_ids_by_location_conn(
    conn: sqlite3.Connection,
    locations: set[str] | frozenset[str],
) -> dict[str, set[str]]:
    """Return the stored step/autostep node IDs at each of *locations*.

    A step row's ``location`` is the file that *declares* its marker —
    cross-module delegation never moves it — so this is the read that lets a
    build diff the step rows it has recorded for a file against the ones that
    file justifies today.

    Args:
        conn: Open SQLite connection (caller manages the transaction).
        locations: Repo-relative file paths to read.  Read in chunks, so an
            arbitrarily large set is safe.

    Returns:
        Mapping of file location to the set of step/autostep node IDs stored
        there.  Locations holding no step rows are absent from the mapping,
        never present with an empty set.
    """
    result: dict[str, set[str]] = {}
    ordered = list(locations)
    subtype_ph = ",".join("?" * len(STEP_NODE_SUBTYPES))
    for start in range(0, len(ordered), _LOCATION_CHUNK):
        chunk = ordered[start : start + _LOCATION_CHUNK]
        loc_ph = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT id, location FROM nodes "  # noqa: S608 - placeholders only
            f"WHERE subtype IN ({subtype_ph}) AND location IN ({loc_ph})",
            (*STEP_NODE_SUBTYPES, *chunk),
        ).fetchall()
        for row in rows:
            result.setdefault(row["location"], set()).add(row["id"])
    return result


def get_nodes_at_locations_conn(
    conn: sqlite3.Connection,
    locations: Iterable[str],
    node_types: Iterable[str] = (),
) -> list[AxiomNode]:
    """Return the nodes stored at *locations*, in node-table order (indexed by location).

    Args:
        conn: Open connection.
        locations: Project-relative file paths, as stored.
        node_types: Keep only these node types; every type when empty.

    Returns:
        The nodes.
    """
    locs = sorted(set(locations))
    types = list(node_types)
    type_sql = f" AND node_type IN ({','.join('?' * len(types))})" if types else ""
    out: list[AxiomNode] = []
    for start in range(0, len(locs), 500):
        chunk = locs[start : start + 500]
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE location IN ({','.join('?' * len(chunk))}){type_sql} ORDER BY rowid",
            (*chunk, *types),
        ).fetchall()
        out.extend(_row_to_node(r) for r in rows)
    return out


def node_ids_at_locations_conn(conn: sqlite3.Connection, locations: Iterable[str]) -> set[str]:
    """Return the ids of the nodes stored at *locations* (indexed by location).

    Args:
        conn: Open connection.
        locations: Project-relative file paths, as stored.

    Returns:
        Node ids.
    """
    locs = sorted(set(locations))
    out: set[str] = set()
    for start in range(0, len(locs), 500):
        chunk = locs[start : start + 500]
        out.update(
            r[0] for r in conn.execute(f"SELECT id FROM nodes WHERE location IN ({','.join('?' * len(chunk))})", chunk)
        )
    return out


def count_parentless_step_nodes_by_location_conn(conn: sqlite3.Connection) -> dict[str, int]:
    """Return, per file, how many step rows have no enclosing workflow left.

    A step row whose declaring function was deleted or moved loses its parent
    envelope, and the ``composes`` edge cascades away with it — leaving a row
    no envelope can enumerate.  Counting those is a single query needing no
    scan, which is what makes it usable as a build-time signal.

    It is a proxy, not a census: a step row whose envelope is still alive (a
    workflow whose markers were renumbered, say) has a ``composes`` parent and
    is invisible here.

    Args:
        conn: Open SQLite connection (caller manages the transaction).

    Returns:
        Mapping of file location to the number of parentless step/autostep
        rows stored there.  Files with none are absent from the mapping.
    """
    subtype_ph = ",".join("?" * len(STEP_NODE_SUBTYPES))
    rows = conn.execute(
        f"SELECT n.location AS location, COUNT(*) AS n FROM nodes n "  # noqa: S608 - placeholders only
        f"WHERE n.subtype IN ({subtype_ph}) "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM edges e WHERE e.to_id = n.id AND e.edge_type = 'composes'"
        ") "
        "GROUP BY n.location",
        STEP_NODE_SUBTYPES,
    ).fetchall()
    return {row["location"]: row["n"] for row in rows}


# ---------------------------------------------------------------------------
# Deletes
# ---------------------------------------------------------------------------


@task(
    purpose="Cascade-delete all nodes at a given file location, removing associated edges (keeping inbound documents edges from surviving sources), tags, FTS, history, and verification rows",
    inputs="conn (open SQLite connection), location file path, optional git_sha",
    outputs="Number of nodes deleted",
)
def delete_nodes_by_location(conn: sqlite3.Connection, location: str, git_sha: str | None = None) -> int:
    """Cascade-delete all nodes at a given file location.

    Removes associated edges, tags, FTS entries, history, and verification rows.
    Inserts a preserved DELETED history row per node so the since filter can
    surface ghost nodes for deleted files.

    Inbound ``documents`` edges from surviving sources are kept (flag-don't-drop):
    the source file still declares the link, so the edge stays for
    ``find_broken_links()`` to flag the source BROKEN_LINK on the next check —
    the same state a from-scratch build computes. Kept edges get no LINK_REMOVED
    history. All other edges (outbound, scanner-derived inbound, both-ends-deleted)
    are deleted as before.

    Takes an open connection so it can be batched in a transaction.
    Returns the number of nodes deleted.

    Args:
        conn: Open SQLite connection (so the delete can be batched in a txn).
        location: Repo-relative file path whose nodes to cascade-delete.
        git_sha: The index/build SHA at deletion time. Written into the
            DELETED-history ``git_sha`` column **and** preserved in the meta
            JSON (alongside each node's ``level_3_location`` span) so a deleted
            ghost's baseline source can be recovered later via ``git show``.
            ``None`` (the default) preserves the legacy behaviour (no SHA, no
            span) for callers that do not supply one — those ghosts fall back
            to whole-file recovery.

    Returns:
        The number of nodes deleted (unchanged contract).
    """
    口 = Step(
        step_num=1,
        name="Snapshot nodes and edges as DELETED history",
        purpose="Collect nodes at location, insert preserved DELETED and LINK_REMOVED history rows",
        critical="LINK_REMOVED history is attached to the surviving node (not the deleted one) so it persists after the cascade delete",
    )
    nodes = conn.execute(
        "SELECT id, node_type, subtype, title, location, level_3_location FROM nodes WHERE location = ?",
        (location,),
    ).fetchall()

    if not nodes:
        return 0

    node_ids = [r["id"] for r in nodes]
    ph = ",".join("?" * len(node_ids))

    now = _now_utc()

    # Snapshot each node as a preserved DELETED history row
    for row in nodes:
        tags = [t["tag"] for t in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (row["id"],)).fetchall()]
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                now,
                "DELETED",
                git_sha,
                json.dumps(
                    {
                        "title": row["title"],
                        "node_type": row["node_type"],
                        "subtype": row["subtype"],
                        "location": row["location"],
                        "level_3_location": row["level_3_location"],
                        "git_sha": git_sha,
                        "tags": tags,
                        "actor": "system",
                    }
                ),
                1,
            ),
        )

    # Record LINK_REMOVED history for edges being deleted.  Inbound
    # ``documents`` edges from surviving sources are excluded: they are kept
    # (not deleted), so recording LINK_REMOVED for them would be a lie.
    edges_to_remove = conn.execute(
        f"""
        SELECT edge_type, from_id, to_id FROM edges
        WHERE (from_id IN ({ph}) OR to_id IN ({ph}))
          AND NOT (edge_type = 'documents' AND to_id IN ({ph}) AND from_id NOT IN ({ph}))
        """,
        node_ids * 4,
    ).fetchall()
    for edge_row in edges_to_remove:
        # Attach the history row to the surviving node when possible,
        # so it is not wiped by the non-preserved cleanup below.
        surviving_id = edge_row["to_id"] if edge_row["from_id"] in node_ids else edge_row["from_id"]
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                surviving_id,
                now,
                "LINK_REMOVED",
                None,
                json.dumps(
                    {
                        "edge_type": edge_row["edge_type"],
                        "source": edge_row["from_id"],
                        "target": edge_row["to_id"],
                        "actor": "system",
                    }
                ),
                1,
            ),
        )

    口 = Step(
        step_num=2,
        name="Cascade-delete node records and references",
        purpose="Remove tags, FTS, non-preserved history, verification, edges, and node rows",
        critical="Inbound documents edges from surviving sources are kept so find_broken_links() flags the source on the next check (flag-don't-drop)",
    )
    conn.execute(f"DELETE FROM tags WHERE node_id IN ({ph})", node_ids)
    conn.execute(f"DELETE FROM node_fts WHERE id IN ({ph})", node_ids)
    conn.execute(f"DELETE FROM node_history WHERE node_id IN ({ph}) AND preserved = 0", node_ids)
    conn.execute(f"DELETE FROM node_verification WHERE node_id IN ({ph})", node_ids)
    conn.execute(
        f"""
        DELETE FROM edges
        WHERE (from_id IN ({ph}) OR to_id IN ({ph}))
          AND NOT (edge_type = 'documents' AND to_id IN ({ph}) AND from_id NOT IN ({ph}))
        """,
        node_ids * 4,
    )
    conn.execute(f"DELETE FROM nodes WHERE id IN ({ph})", node_ids)

    return len(node_ids)


def clear_location_file_mtime_conn(conn: sqlite3.Connection, location: str) -> int:
    """Clear the stored ``file_mtime`` of every row at *location*.

    Drops the location out of the builder's scan-skip cache, so the next
    build rescans the file whatever stamps its rows carried.

    Args:
        conn: Open DB connection (caller commits).
        location: Project-relative file path.

    Returns:
        Number of rows whose stored mtime was cleared.
    """
    cur = conn.execute(
        "UPDATE nodes SET file_mtime = NULL WHERE location = ? AND file_mtime IS NOT NULL",
        (location,),
    )
    return cur.rowcount


def delete_node_by_id(
    conn: sqlite3.Connection,
    node_id: str,
    reason_meta: dict | None = None,
) -> None:
    """Cascade-delete a single node by its ID.

    Same cascade as ``delete_nodes_by_location`` but targeted at one node:
    inserts a preserved DELETED history row, records LINK_REMOVED for edges,
    then deletes tags, FTS, non-preserved history, verification, edges, and
    the node itself.

    Inbound ``documents`` edges from other (surviving) sources are kept with
    no LINK_REMOVED history (flag-don't-drop) so ``find_broken_links()`` flags
    the source BROKEN_LINK on the next check.

    Args:
        conn: Open SQLite connection (caller manages the transaction).
        node_id: The full node ID to delete.
        reason_meta: Optional dict merged into the DELETED history row's meta
            (e.g. ``{"actor": "agent", "reason": "..."}``).
            Defaults to ``{"actor": "system"}`` when not provided.
    """
    row = conn.execute(
        "SELECT id, node_type, subtype, title, location FROM nodes WHERE id = ?",
        (node_id,),
    ).fetchone()
    if row is None:
        return

    now = _now_utc()

    # Build meta for DELETED history row
    tags = [t["tag"] for t in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (node_id,)).fetchall()]
    meta = {
        "title": row["title"],
        "node_type": row["node_type"],
        "subtype": row["subtype"],
        "location": row["location"],
        "tags": tags,
        "actor": "system",
    }
    if reason_meta:
        meta.update(reason_meta)

    conn.execute(
        "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) VALUES (?, ?, ?, ?, ?, ?)",
        (node_id, now, "DELETED", None, json.dumps(meta), 1),
    )

    # Record LINK_REMOVED history for edges being deleted.  Inbound
    # ``documents`` edges from surviving sources are excluded: they are kept
    # (not deleted), so recording LINK_REMOVED for them would be a lie.
    edges_to_remove = conn.execute(
        """
        SELECT edge_type, from_id, to_id FROM edges
        WHERE (from_id = ? OR to_id = ?)
          AND NOT (edge_type = 'documents' AND to_id = ? AND from_id != ?)
        """,
        (node_id, node_id, node_id, node_id),
    ).fetchall()
    for edge_row in edges_to_remove:
        surviving_id = edge_row["to_id"] if edge_row["from_id"] == node_id else edge_row["from_id"]
        conn.execute(
            "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) VALUES (?, ?, ?, ?, ?, ?)",
            (
                surviving_id,
                now,
                "LINK_REMOVED",
                None,
                json.dumps(
                    {
                        "edge_type": edge_row["edge_type"],
                        "source": edge_row["from_id"],
                        "target": edge_row["to_id"],
                        "actor": reason_meta.get("actor", "system") if reason_meta else "system",
                    }
                ),
                1,
            ),
        )

    conn.execute("DELETE FROM tags WHERE node_id = ?", (node_id,))
    conn.execute("DELETE FROM node_fts WHERE id = ?", (node_id,))
    conn.execute("DELETE FROM node_history WHERE node_id = ? AND preserved = 0", (node_id,))
    conn.execute("DELETE FROM node_verification WHERE node_id = ?", (node_id,))
    conn.execute(
        """
        DELETE FROM edges
        WHERE (from_id = ? OR to_id = ?)
          AND NOT (edge_type = 'documents' AND to_id = ? AND from_id != ?)
        """,
        (node_id, node_id, node_id, node_id),
    )
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))


__all__ = [
    # Hash lookups
    "get_code_hash",
    "get_source_hash",
    "get_node_hashes",
    # Verification
    "upsert_verification",
    "upsert_verification_conn",
    "update_node_baseline",
    "update_node_baseline_conn",
    "write_baseline_verifications_conn",
    "restamp_verifications",
    "get_verification",
    "get_all_verifications",
    "get_all_verifications_conn",
    # Live hashes and verification pairs
    "load_live_view_conn",
    "get_unhashed_node_ids",
    "get_live_rows_conn",
    "get_liveness_rows_conn",
    "index_has_nodes_conn",
    "get_nodes_conn",
    "get_reset_locations_conn",
    "get_verifications_for_conn",
    "get_verification_targets_for_conn",
    "get_last_hashed_fingerprints",
    "get_all_verification_targets_conn",
    "get_verification_targets",
    "replace_verification_targets_conn",
    "update_verification_snapshot_conn",
    "touch_verification_conn",
    "refresh_verification_targets_conn",
    "pin_verification_targets_conn",
    "rekey_verification_targets_conn",
    # Upsert
    "upsert_node",
    "upsert_node_conn",
    # Reads
    "get_node",
    "query_nodes",
    "query_children",
    "all_nodes",
    "get_undocumented_nodes",
    # Workflow step rows
    "STEP_NODE_SUBTYPES",
    "get_step_node_ids_by_location_conn",
    "get_nodes_at_locations_conn",
    "node_ids_at_locations_conn",
    "count_parentless_step_nodes_by_location_conn",
    # Deletes
    "delete_nodes_by_location",
    "delete_node_by_id",
    "clear_location_file_mtime_conn",
]
