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
from pathlib import Path

from axiom_annotations import Step, task

from axiom_graph.models import AxiomNode

from axiom_graph.db._core import (
    _HISTORY_ROW_LIMIT,
    _connect,
    _derive_change_type,
    _node_to_row,
    _now_utc,
    _row_to_node,
    _steps_to_json,
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
    verified_at = _now_utc()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO node_verification
                (node_id, status, verified_at, verified_by, reason, code_hash_at, desc_hash_at)
            VALUES (?, 'VERIFIED', ?, ?, ?, ?, ?)
            """,
            (node_id, verified_at, verified_by, reason, code_hash_at, desc_hash_at),
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

    Args:
        db_path: Path to the axiom-graph SQLite database.
        node_id: The node to update.
        code_hash: Current code/body hash from the file on disk.
        desc_hash: Current desc/heading hash from the file on disk.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET code_hash = ?, desc_hash = ?, own_status = 'VERIFIED' WHERE id = ?",
            (code_hash, desc_hash, node_id),
        )


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
            (e.g. ``{"actor": "agent:pev-auditor", "reason": "..."}``).
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
    "update_node_baseline",
    "get_verification",
    "get_all_verifications",
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
    "count_parentless_step_nodes_by_location_conn",
    # Deletes
    "delete_nodes_by_location",
    "delete_node_by_id",
]
