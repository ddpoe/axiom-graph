"""Axiom-graph DB: doc/doc_section CRUD, renames, and FTS search.

Covers the ``docs`` and ``doc_sections`` tables (``upsert_doc``,
``upsert_doc_section``, ``get_doc_sections``, ``list_docs``,
``list_all_doc_sections``, ``get_long_sections``,
``query_doc_sections_by_tags``, ``get_doc_ids_by_filepath``,
``delete_doc_by_id``, ``record_doc_rename``, ``record_code_rename``,
``move_doc``, ``get_all_doc_file_paths``, ``get_all_node_locations``,
``get_tagged_doc_doc_edges``), plus the FTS5 node_fts search
(``fts_search``, ``index_doc_sections_fts``, ``list_tags``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from axiom_annotations import Step, task

from axiom_graph.models import AxiomNode

from axiom_graph.db._core import (
    _connect,
    _now_utc,
    _row_to_node,
)
from axiom_graph.db.edges import _migrate_edges

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Doc ID + file_path helpers
# ---------------------------------------------------------------------------


def get_doc_ids_by_filepath(db_path: Path, file_path: str) -> list[str]:
    """Return all doc IDs that reference a given file_path."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT id FROM docs WHERE file_path = ?", (file_path,)).fetchall()
        return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Doc delete
# ---------------------------------------------------------------------------


@task(
    purpose="Cascade-delete a doc and all related rows (sections, nodes, edges, tags, FTS, history) from the index, keeping inbound documents edges from surviving sources",
    inputs="conn (open SQLite connection), doc_id",
    outputs="None (side effect: all rows referencing doc_id removed, except kept inbound documents edges)",
)
def delete_doc_by_id(
    conn: sqlite3.Connection,
    doc_id: str,
    reason_meta: dict | None = None,
) -> None:
    """Delete a doc and all related rows (sections, nodes, edges, tags, FTS, history).

    Inserts preserved DELETED history rows so ghost nodes survive in the
    since filter.  Takes an open connection so it can be batched in a
    transaction.

    Inbound ``documents`` edges from surviving sources (other docs' sections
    that link to this doc or its sections) are kept with no LINK_REMOVED
    history (flag-don't-drop) so ``find_broken_links()`` flags the source
    BROKEN_LINK on the next check.

    Args:
        conn: Open SQLite connection (caller manages the transaction).
        doc_id: The full doc node ID to delete.
        reason_meta: Optional dict merged into the DELETED history row's meta
            (e.g. ``{"actor": "agent:pev-auditor", "reason": "..."}``).
            Defaults to ``{"actor": "system"}`` when not provided.
    """
    口 = Step(
        step_num=1,
        name="Snapshot nodes as DELETED history rows",
        purpose="Collect doc + section nodes, insert preserved DELETED and LINK_REMOVED history rows",
    )
    # Collect all node rows for this doc (parent + sections)
    nodes = conn.execute(
        "SELECT id, node_type, subtype, title, location FROM nodes WHERE id = ? OR id LIKE ?",
        (doc_id, doc_id + "::%"),
    ).fetchall()

    if nodes:
        node_ids = [r["id"] for r in nodes]
        ph = ",".join("?" * len(node_ids))

        now = _now_utc()

        # Snapshot each node as a preserved DELETED history row
        for row in nodes:
            tags = [t["tag"] for t in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (row["id"],)).fetchall()]
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
                (
                    row["id"],
                    now,
                    "DELETED",
                    None,
                    json.dumps(meta),
                    1,
                ),
            )

        # Record LINK_REMOVED history for edges being deleted.  Inbound
        # ``documents`` edges from surviving sources are excluded: they are
        # kept (not deleted), so recording LINK_REMOVED for them would be a lie.
        edges_to_remove = conn.execute(
            f"""
            SELECT edge_type, from_id, to_id FROM edges
            WHERE (from_id IN ({ph}) OR to_id IN ({ph}))
              AND NOT (edge_type = 'documents' AND to_id IN ({ph}) AND from_id NOT IN ({ph}))
            """,
            node_ids * 4,
        ).fetchall()
        for edge_row in edges_to_remove:
            conn.execute(
                "INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    edge_row["from_id"],
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
                    0,
                ),
            )

        口 = Step(
            step_num=2,
            name="Cascade-delete tags, FTS, verification, edges",
            purpose="Remove dependent rows from tags, FTS, non-preserved history, verification, and edges tables",
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

    口 = Step(step_num=3, name="Delete doc and section records", purpose="Remove doc_sections and docs table entries")
    conn.execute("DELETE FROM doc_sections WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))


# ---------------------------------------------------------------------------
# Renames (doc + code)
# ---------------------------------------------------------------------------


def record_doc_rename(
    db_path: Path,
    old_id: str,
    new_id: str,
    file_path: str,
    project_root: Path | None = None,
) -> None:
    """Record a doc ID rename and migrate history/verification/edges to the new ID.

    Call this BEFORE deleting the old doc rows so history can be migrated.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        old_id: The old doc node ID being renamed from.
        new_id: The new doc node ID being renamed to.
        file_path: File path for the node_renames record.
        project_root: If provided, DocJSON files on disk will be patched
            to update link references from old_id to new_id.
    """
    now = _now_utc()
    with _connect(db_path) as conn:
        # Record the rename
        conn.execute(
            "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
            (old_id, new_id, now, file_path),
        )
        # Migrate history rows: old parent + old sections -> new equivalents
        # Parent node: old_id -> new_id
        conn.execute(
            "UPDATE node_history SET node_id = ? WHERE node_id = ?",
            (new_id, old_id),
        )
        # Section nodes: old_id::sec -> new_id::sec
        old_prefix = old_id + "::"
        rows = conn.execute(
            "SELECT DISTINCT node_id FROM node_history WHERE node_id LIKE ?",
            (old_prefix + "%",),
        ).fetchall()
        for r in rows:
            old_sec_id = r["node_id"]
            suffix = old_sec_id[len(old_prefix) :]
            new_sec_id = new_id + "::" + suffix
            conn.execute(
                "UPDATE node_history SET node_id = ? WHERE node_id = ?",
                (new_sec_id, old_sec_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
                (old_sec_id, new_sec_id, now, file_path),
            )

        # Migrate verification (if any)
        conn.execute(
            "UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?",
            (new_id, old_id),
        )

        # Migrate edges: parent node
        _migrate_edges(conn, old_id, new_id)
        # Migrate edges: section nodes (old_id::sec -> new_id::sec)
        sec_edges = conn.execute(
            "SELECT DISTINCT from_id, to_id FROM edges WHERE from_id LIKE ? OR to_id LIKE ?",
            (old_prefix + "%", old_prefix + "%"),
        ).fetchall()
        migrated_sec_ids: set[str] = set()
        for row in sec_edges:
            for col_id in (row["from_id"], row["to_id"]):
                if col_id.startswith(old_prefix) and col_id not in migrated_sec_ids:
                    suffix = col_id[len(old_prefix) :]
                    new_sec_id = new_id + "::" + suffix
                    _migrate_edges(conn, col_id, new_sec_id)
                    migrated_sec_ids.add(col_id)

    # Patch DocJSON files on disk if project_root is provided
    if project_root is not None:
        from axiom_graph.index.link_maintenance import patch_doc_links  # noqa: PLC0415

        patch_doc_links(project_root, db_path, old_id, new_id)


@task(
    purpose="Record a code node rename: insert into node_renames table, migrate history rows and verification snapshot from old node ID to new node ID",
    inputs="db_path, old_id, new_id, file_path",
    outputs="None (side effect: node_renames row inserted, node_history and node_verification rows migrated)",
)
def record_code_rename(
    db_path: Path,
    old_id: str,
    new_id: str,
    file_path: str,
    project_root: Path | None = None,
) -> None:
    """Record a code node rename and migrate history/verification/edges to the new ID.

    Used by hash-similarity rename detection: when a function disappears from
    one module but an identical ``code_hash`` appears in another, this migrates
    the old node's history, verification, and edges to the new node ID and
    records the mapping in ``node_renames``.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        old_id: The old node ID being renamed from.
        new_id: The new node ID being renamed to.
        file_path: File path for the node_renames record.
        project_root: If provided, DocJSON files on disk will be patched
            to update link references from old_id to new_id.
    """
    口 = Step(
        step_num=1,
        name="Migrate history, verification, and edges to new node ID",
        purpose="Record rename, then UPDATE history/verification rows and edge references from old_id to new_id",
    )
    now = _now_utc()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
            (old_id, new_id, now, file_path),
        )
        # Migrate history rows
        conn.execute(
            "UPDATE node_history SET node_id = ? WHERE node_id = ?",
            (new_id, old_id),
        )
        # Migrate verification (if any)
        conn.execute(
            "UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?",
            (new_id, old_id),
        )
        # Clean up the old verification row if the UPDATE created a conflict
        conn.execute(
            "DELETE FROM node_verification WHERE node_id = ?",
            (old_id,),
        )
        # Migrate edges: update to_id and from_id references
        _migrate_edges(conn, old_id, new_id)

        # Cascade rename to the envelope + step children this function owns.
        # Envelope ID: ``{func_id}@workflow``.  Step IDs: ``{func_id}::step-*``.
        # We also update the envelope/step nodes' code_hash/subtype-neutral
        # metadata via a straight UPDATE: the IDs change, everything else
        # stays referentially intact.
        old_env = f"{old_id}@workflow"
        new_env = f"{new_id}@workflow"
        env_exists = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (old_env,)).fetchone()
        if env_exists is not None:
            conn.execute("UPDATE nodes SET id = ? WHERE id = ?", (new_env, old_env))
            conn.execute(
                "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
                (old_env, new_env, now, file_path),
            )
            conn.execute("UPDATE node_history SET node_id = ? WHERE node_id = ?", (new_env, old_env))
            conn.execute(
                "UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?",
                (new_env, old_env),
            )
            conn.execute("DELETE FROM node_verification WHERE node_id = ?", (old_env,))
            _migrate_edges(conn, old_env, new_env)

        step_rows = conn.execute(
            "SELECT id FROM nodes WHERE id LIKE ?",
            (f"{old_id}::step-%",),
        ).fetchall()
        for srow in step_rows:
            old_step = srow["id"]
            new_step = new_id + old_step[len(old_id) :]
            conn.execute("UPDATE nodes SET id = ? WHERE id = ?", (new_step, old_step))
            conn.execute(
                "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
                (old_step, new_step, now, file_path),
            )
            conn.execute("UPDATE node_history SET node_id = ? WHERE node_id = ?", (new_step, old_step))
            _migrate_edges(conn, old_step, new_step)

    口 = Step(
        step_num=2,
        name="Patch DocJSON link references on disk",
        purpose="Walk DocJSON files and replace old_id with new_id in links arrays",
    )
    # Patch DocJSON files on disk if project_root is provided
    if project_root is not None:
        from axiom_graph.index.link_maintenance import patch_doc_links  # noqa: PLC0415

        patch_doc_links(project_root, db_path, old_id, new_id)


# ---------------------------------------------------------------------------
# Doc / doc-section upserts + reads
# ---------------------------------------------------------------------------


def upsert_doc(conn: sqlite3.Connection, doc: dict) -> None:
    """Insert or replace a doc record. Takes an open connection."""
    conn.execute(
        """
        INSERT OR REPLACE INTO docs (id, title, tags, file_path, desc_hash, updated_at)
        VALUES (:id, :title, :tags, :file_path, :desc_hash, :updated_at)
        """,
        doc,
    )


def _sync_docjson_shadow(conn: sqlite3.Connection, sec_id: str) -> None:
    """Sync the ``nodes`` shadow row from the canonical ``doc_sections`` row.

    For every row in ``nodes`` with ``subtype='docjson'`` and id ``sec_id``,
    the four-field invariant is::

        nodes.updated_at  == doc_sections.updated_at
        nodes.desc_hash   == doc_sections.desc_hash
        nodes.level_1     == doc_sections.heading
        nodes.level_2     == doc_sections.content

    The ``desc_hash`` column holds ``hash16(content)``, not a description
    hash — the name is a historical artefact of the ``nodes`` schema.
    The DocJSON scanner mirrors this by emitting ``content_hash`` for
    section atomic nodes' ``desc_hash``, so the staleness comparator's
    two sides agree.  See cycle ``pev-instance-2026-05-16-docjson-section-desc-updated-phantom-cascade``.

    This helper is the single canonical writer of those four columns for
    docjson shadow rows.  It is idempotent and a silent no-op when the
    canonical ``doc_sections`` row is absent (e.g. cold-build ordering,
    or a stray shadow row).  It does NOT create the shadow row — shadow
    creation stays in :func:`index_doc_sections_fts`.

    The helper deliberately does NOT touch ``code_hash``, ``link_status``,
    ``own_status``, or any verification columns; staleness mechanics
    remain governed by their existing writers (per ADR-018 LINKED_STALE
    stickiness).

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        sec_id: Full doc-section ID (matches ``doc_sections.id``).
    """
    row = conn.execute(
        "SELECT heading, content, desc_hash, updated_at FROM doc_sections WHERE id = ?",
        (sec_id,),
    ).fetchone()
    if row is None:
        return
    conn.execute(
        """
        UPDATE nodes
        SET level_1 = ?, level_2 = ?, desc_hash = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            row["heading"] or "",
            row["content"] or "",
            row["desc_hash"],
            row["updated_at"],
            sec_id,
        ),
    )


def upsert_doc_section(conn: sqlite3.Connection, sec: dict) -> None:
    """Insert or replace a doc_section record. Takes an open connection.

    After persisting the canonical row, syncs the matching ``nodes``
    shadow row (when present) via :func:`_sync_docjson_shadow` so the
    four-field invariant holds for every write path.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO doc_sections
            (id, doc_id, heading, level, tags, content, desc_hash, parent_id, depth, position, updated_at)
        VALUES
            (:id, :doc_id, :heading, :level, :tags, :content, :desc_hash, :parent_id, :depth, :position, :updated_at)
        """,
        sec,
    )
    _sync_docjson_shadow(conn, sec["id"])


def find_docjson_shadow_invariant_violations(db_path: Path) -> list[str]:
    """Return IDs of ``subtype='docjson'`` shadow rows that drift from canonical.

    Scans every ``nodes`` row with ``subtype='docjson'``, joins to
    ``doc_sections`` on id, and surfaces rows where any of the four
    invariant fields disagrees.  Rows without a canonical match are
    classified as orphan shadows and are NOT reported here (different
    bug category).

    Returns:
        Sorted list of offending node IDs.  Empty when the invariant
        holds DB-wide.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT n.id
            FROM nodes n
            JOIN doc_sections d ON d.id = n.id
            WHERE n.subtype = 'docjson'
              AND (
                  COALESCE(n.level_1, '') != COALESCE(d.heading, '')
               OR COALESCE(n.level_2, '') != COALESCE(d.content, '')
               OR COALESCE(n.desc_hash, '') != COALESCE(d.desc_hash, '')
               OR COALESCE(n.updated_at, '') != COALESCE(d.updated_at, '')
              )
            ORDER BY n.id
            """,
        ).fetchall()
        return [r["id"] for r in rows]


def get_doc_sections(db_path: Path, doc_id: str) -> list[dict]:
    """Return all doc_section rows for a given doc_id, ordered by position."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM doc_sections WHERE doc_id = ? ORDER BY position",
            (doc_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_docs(db_path: Path) -> list[dict]:
    """Return all rows from the docs table."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM docs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def list_all_doc_sections(db_path: Path) -> list[dict]:
    """Return all rows from doc_sections ordered by doc_id, position."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM doc_sections ORDER BY doc_id, position").fetchall()
        return [dict(r) for r in rows]


# DOC_SECTION_LONG content-length threshold (single source of truth).
# Consumed by :func:`get_long_sections` AND the ``DOC_SECTION_LONG``
# arm of :func:`axiom_graph.db.staleness.query_drift_rows` — keeping
# this in one place means a threshold change propagates atomically.
DOC_SECTION_LONG_THRESHOLD = 2000


def get_long_sections(db_path: Path, threshold: int = DOC_SECTION_LONG_THRESHOLD) -> list[dict]:
    """Return doc sections whose content exceeds *threshold* chars, longest first."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, doc_id, heading, LENGTH(content) AS chars
            FROM doc_sections
            WHERE LENGTH(content) > ?
            ORDER BY LENGTH(content) DESC
            """,
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]


def query_doc_sections_by_tags(
    db_path: Path,
    tags: list[str],
    *,
    match_all: bool = False,
) -> list[dict]:
    """Return doc_sections whose section-level tags overlap with *tags*.

    When *match_all* is ``True``, only sections tagged with **every**
    requested tag are returned.  Otherwise any overlap is sufficient.

    Each returned dict has all ``doc_sections`` columns plus
    ``doc_title`` (from the parent ``docs`` row).
    """
    if not tags:
        return []
    with _connect(db_path) as conn:
        placeholders = ",".join("?" * len(tags))
        threshold = len(tags) if match_all else 1
        rows = conn.execute(
            f"""
            SELECT ds.*, d.title AS doc_title
            FROM doc_sections ds
            JOIN docs d ON ds.doc_id = d.id
            JOIN tags t ON t.node_id = ds.id
            WHERE t.tag IN ({placeholders})
            GROUP BY ds.id
            HAVING COUNT(DISTINCT t.tag) >= ?
            ORDER BY d.id, ds.position
            """,
            [*tags, threshold],
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tagged doc-to-doc edges
# ---------------------------------------------------------------------------


def get_tagged_doc_doc_edges(db_path: Path, tags: list[str]) -> list[dict]:
    """Return doc-to-doc ``documents`` edges where the source doc has a matching tag.

    Used by the transitive LINKED_STALE propagation pass.  Only returns
    edges where the *target* is a docjson section (i.e. doc-to-doc links),
    which is the inverse of ``get_stale_doc_sections`` (which filters them
    out).

    The tag check is at the **document** level: the source section's parent
    doc must carry at least one tag from *tags*.

    Args:
        db_path: Path to the axiom-graph DB.
        tags: List of tag strings to match against ``docs.tags`` JSON array.

    Returns:
        List of dicts with ``source_section_id`` and ``target_section_id``.
    """
    if not tags:
        return []

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                e.from_id AS source_section_id,
                e.to_id   AS target_section_id,
                d.tags    AS doc_tags
            FROM edges e
            JOIN doc_sections s_src ON s_src.id = e.from_id
            JOIN docs d             ON d.id = s_src.doc_id
            JOIN doc_sections s_tgt ON s_tgt.id = e.to_id
            WHERE e.edge_type = 'documents'
              AND d.tags IS NOT NULL
            """
        ).fetchall()

    # Filter in Python: docs.tags is a JSON array string; check overlap with
    # the requested tags set.  This avoids building dynamic SQL with IN clauses.
    tag_set = set(tags)
    result: list[dict] = []
    for r in rows:
        row_dict = dict(r)
        try:
            doc_tags = json.loads(row_dict.get("doc_tags", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            doc_tags = []
        if tag_set & set(doc_tags):
            result.append(
                {
                    "source_section_id": row_dict["source_section_id"],
                    "target_section_id": row_dict["target_section_id"],
                }
            )
    return result


def get_doc_ids_with_tags(db_path: Path, tags: list[str]) -> set[str]:
    """Return the set of doc IDs whose ``docs.tags`` JSON array intersects *tags*.

    Used by the frozen-tags filter on the staleness engine — sections under
    a doc carrying any of these tags are immune to LINKED_STALE propagation
    (Pass 1 + Pass 3).  Empty *tags* short-circuits to the empty set without
    opening a connection.

    The tag check is at the **document** level (matches the
    ``transitive_tags`` contract).

    Args:
        db_path: Path to the axiom-graph DB.
        tags: List of tag strings to match against ``docs.tags``.

    Returns:
        Set of matching doc IDs.  Empty when *tags* is empty or no doc carries
        a matching tag.
    """
    if not tags:
        return set()

    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, tags FROM docs WHERE tags IS NOT NULL",
        ).fetchall()

    tag_set = set(tags)
    result: set[str] = set()
    for r in rows:
        row_dict = dict(r)
        try:
            doc_tags = json.loads(row_dict.get("tags", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            doc_tags = []
        if tag_set & set(doc_tags):
            result.add(row_dict["id"])
    return result


def get_section_doc_id_map(db_path: Path, doc_ids: set[str] | None = None) -> dict[str, str]:
    """Return a mapping of ``section_id -> doc_id`` for sections under *doc_ids*.

    Used by the frozen-tags filter to look up the owning doc of an
    arbitrary section ID.  When *doc_ids* is ``None`` returns the full
    mapping; when an empty set returns an empty dict without opening a
    connection.

    Args:
        db_path: Path to the axiom-graph DB.
        doc_ids: Optional set of doc IDs to restrict the mapping to.

    Returns:
        Dict mapping section ID to owning doc ID.
    """
    if doc_ids is not None and not doc_ids:
        return {}

    with _connect(db_path) as conn:
        if doc_ids is None:
            rows = conn.execute(
                "SELECT id, doc_id FROM doc_sections",
            ).fetchall()
        else:
            placeholders = ",".join("?" * len(doc_ids))
            rows = conn.execute(
                f"SELECT id, doc_id FROM doc_sections WHERE doc_id IN ({placeholders})",
                list(doc_ids),
            ).fetchall()

    return {r["id"]: r["doc_id"] for r in rows}


# ---------------------------------------------------------------------------
# Move / rename / location helpers
# ---------------------------------------------------------------------------


def move_doc(db_path: Path, old_doc_id: str, new_doc_id: str, new_file_path: str) -> None:
    """Transactional move: migrate history then delete old doc rows.

    1. ``record_doc_rename`` migrates history/verification to the new ID.
    2. ``delete_doc_by_id`` cascading-deletes the old nodes, edges, tags, FTS, history.
    """
    record_doc_rename(db_path, old_doc_id, new_doc_id, new_file_path)
    with _connect(db_path) as conn:
        delete_doc_by_id(conn, old_doc_id)


def get_all_doc_file_paths(db_path: Path) -> list[str]:
    """Return distinct ``file_path`` values from the docs table."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT file_path FROM docs").fetchall()
        return [r["file_path"] for r in rows]


def get_all_node_locations(db_path: Path) -> list[str]:
    """Return distinct ``location`` values from the nodes table."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT location FROM nodes").fetchall()
        return [r["location"] for r in rows]


# ---------------------------------------------------------------------------
# Tags + FTS search
# ---------------------------------------------------------------------------


def list_tags(db_path: Path) -> list[tuple[str, int]]:
    """Return all distinct tags with node counts, ordered alphabetically.

    Returns:
        List of (tag, count) tuples.
    """
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT tag, COUNT(*) as cnt FROM tags GROUP BY tag ORDER BY tag").fetchall()
        return [(r["tag"], r["cnt"]) for r in rows]


def fts_search(
    db_path: Path,
    query: str,
    level: int | None = None,
    max_results: int = 20,
    node_type: str | None = None,
    scope: str | None = None,
    tag: str | None = None,
) -> tuple[list[AxiomNode], str, int]:
    """Full-text search over level_1 and level_2.

    First tries FTS5 for ranked exact/prefix matching.  If FTS5 returns
    nothing (or raises due to a non-FTS5 query syntax), falls back to a
    two-stage LIKE scan:
      Stage 1 — AND: all tokens must appear (precise).
      Stage 2 — OR: any token must appear (broad, last resort, capped at 10).

    Parameters
    ----------
    max_results:
        Maximum number of nodes returned.  ``like_or`` results are further
        capped at ``min(max_results, 10)`` because that mode is low-confidence.
    node_type:
        If given, only nodes of this type are returned (e.g. ``atomic_process``).
    scope:
        Filter results by source: ``'code'`` excludes docjson nodes,
        ``'docs'`` includes only docjson nodes, ``'all'`` or ``None`` includes
        everything.

    Returns
    -------
    (nodes, mode, total_found)
        ``mode`` is one of ``"fts"``, ``"like_and"``, ``"like_or"``.
        ``total_found`` is the count before the ``max_results`` cap was applied.
    """
    type_filter = " AND node_type = ?" if node_type else ""
    type_params: list[str] = [node_type] if node_type else []

    # Scope filtering
    scope_filter = ""
    if scope == "code":
        scope_filter = " AND source NOT IN ('docjson', 'doc_scanner', 'json_doc_scanner')"
    elif scope == "docs":
        scope_filter = " AND source IN ('docjson', 'doc_scanner', 'json_doc_scanner')"

    # Tag post-filter helper
    def _apply_tag_filter(conn, nodes: list[AxiomNode]) -> list[AxiomNode]:
        if not tag:
            return nodes
        tagged_ids = {r["node_id"] for r in conn.execute("SELECT node_id FROM tags WHERE tag = ?", (tag,)).fetchall()}
        return [n for n in nodes if n.id in tagged_ids]

    logger.debug("fts_search: acquiring DB connection for %s", db_path)
    with _connect(db_path) as conn:
        logger.debug("fts_search: connected, preparing query")
        if level == 1:
            fts_query = f"level_1 : {query}"
        elif level == 2:
            fts_query = f"level_2 : {query}"
        else:
            fts_query = query

        ids: list[str] = []
        try:
            logger.debug("fts_search: executing FTS MATCH for %r", fts_query)
            fts_rows = conn.execute(
                "SELECT id FROM node_fts WHERE node_fts MATCH ? ORDER BY rank",
                (fts_query,),
            ).fetchall()
            ids = [r["id"] for r in fts_rows]
            logger.debug("fts_search: FTS returned %d ids", len(ids))
        except Exception:
            logger.debug("fts_search: FTS failed, falling back to LIKE")
            pass  # fall through to LIKE fallback

        if ids:
            placeholders = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM nodes WHERE id IN ({placeholders}){type_filter}{scope_filter}",
                ids + type_params,
            ).fetchall()
            nodes = _apply_tag_filter(conn, [_row_to_node(r) for r in rows])
            total = len(nodes)
            return nodes[:max_results], "fts", total

        tokens = [t for t in query.split() if t]
        if not tokens:
            return [], "fts", 0

        # Stage 1 — AND: all tokens must appear
        and_clauses = " AND ".join("(level_1 LIKE ? OR level_2 LIKE ?)" for _ in tokens)
        and_params = [f"%{t}%" for t in tokens for _ in (0, 1)]
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {and_clauses}{type_filter}{scope_filter}",
            and_params + type_params,
        ).fetchall()
        if rows:
            nodes = _apply_tag_filter(conn, [_row_to_node(r) for r in rows])
            total = len(nodes)
            if nodes or tag:
                return nodes[:max_results], "like_and", total

        # Stage 2 — OR: any token must appear.
        # Capped more aggressively than other modes: a broad OR across many
        # common tokens is low-confidence and returning hundreds of rows adds
        # noise rather than signal.
        # When tag filter is active, skip LIKE-OR fallback — return empty instead.
        if tag:
            return [], "fts", 0
        or_cap = min(max_results, 10)
        or_clauses = " OR ".join("(level_1 LIKE ? OR level_2 LIKE ?)" for _ in tokens)
        or_params = [f"%{t}%" for t in tokens for _ in (0, 1)]
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {or_clauses}{type_filter}{scope_filter}",
            or_params + type_params,
        ).fetchall()
        total = len(rows)
        return [_row_to_node(r) for r in rows[:or_cap]], "like_or", total


def index_doc_sections_fts(db_path: Path) -> int:
    """Index all doc sections into the node_fts table for full-text search.

    Creates synthetic entries in node_fts using the doc section ID as the id,
    the heading as level_1, and the content as level_2. Also creates corresponding
    entries in the nodes table so the FTS results can be joined.

    Args:
        db_path: Path to the axiom-graph DB file.

    Returns:
        Number of doc sections indexed.
    """
    count = 0
    with _connect(db_path) as conn:
        sections = conn.execute(
            "SELECT id, doc_id, heading, content, level, tags, desc_hash, "
            "parent_id, depth, position, updated_at FROM doc_sections"
        ).fetchall()

        for sec in sections:
            sec_id = sec["id"]
            heading = sec["heading"] or ""
            content = sec["content"] or ""

            # Ensure a shadow row exists in nodes so FTS results can join.
            # The invariant-bearing fields (level_1/level_2/desc_hash/
            # updated_at) are written unconditionally by the helper below
            # — the INSERT here only handles cold-build first-creation.
            existing = conn.execute("SELECT id FROM nodes WHERE id = ?", (sec_id,)).fetchone()

            if not existing:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO nodes
                        (id, node_type, subtype, title, location, status, source,
                         code_hash, desc_hash, level_0, level_1, level_2,
                         level_3_location, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sec_id,
                        "atomic_process",
                        "docjson",
                        heading,
                        f"docs/{sec['doc_id'].split('::')[-1]}.json",
                        "active",
                        "docjson",
                        sec["desc_hash"] or "",
                        sec["desc_hash"],
                        heading,
                        heading,
                        content,
                        None,
                        sec["updated_at"],
                    ),
                )

            # Sync the four-field invariant unconditionally (covers both
            # the first-insert case and the resync-existing case).
            _sync_docjson_shadow(conn, sec_id)

            # Sync FTS: delete old entry (if any) then insert fresh
            conn.execute("DELETE FROM node_fts WHERE id = ?", (sec_id,))
            conn.execute(
                "INSERT INTO node_fts (id, level_1, level_2) VALUES (?, ?, ?)",
                (sec_id, heading, content),
            )
            count += 1

    return count


__all__ = [
    # Doc ID helpers
    "get_doc_ids_by_filepath",
    # Doc delete
    "delete_doc_by_id",
    # Renames
    "record_doc_rename",
    "record_code_rename",
    # Upserts + reads
    "upsert_doc",
    "upsert_doc_section",
    "get_doc_sections",
    "list_docs",
    "list_all_doc_sections",
    "get_long_sections",
    "DOC_SECTION_LONG_THRESHOLD",
    "find_docjson_shadow_invariant_violations",
    "query_doc_sections_by_tags",
    "get_tagged_doc_doc_edges",
    "get_doc_ids_with_tags",
    "get_section_doc_id_map",
    # Move / location
    "move_doc",
    "get_all_doc_file_paths",
    "get_all_node_locations",
    # Tags + FTS
    "list_tags",
    "fts_search",
    "index_doc_sections_fts",
]
