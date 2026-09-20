"""Versioned schema migrations for the axiom-graph SQLite DB.

axiom-graph carries no ORM — the schema is raw DDL (``_SCHEMA_SQL``) and
versioning rides on SQLite's built-in ``PRAGMA user_version`` (a 32-bit
integer in the database header that travels with the file).

Mechanism:

- The package carries :data:`CURRENT_SCHEMA_VERSION`.  Pre-versioning
  legacy DBs have never set ``user_version`` and therefore read as ``0``.
  A fresh ``init_db`` creates the current schema directly and stamps
  ``user_version = CURRENT_SCHEMA_VERSION`` so a brand-new DB is never
  mistaken for one needing migration.
- :data:`MIGRATIONS` is an ordered registry keyed by **target** version.
  :func:`run_migrations` reads the DB's ``user_version`` and applies, in
  sequence, every step whose target exceeds it, bumping ``user_version``
  after each step **inside the same transaction** (``user_version`` writes
  are transactional in SQLite), so a crash mid-step rolls back cleanly and
  the step re-runs on the next invocation.
- Before applying anything the runner snapshots the DB via ``VACUUM INTO``
  to ``{db_name}.pre-v{N}.bak`` next to the DB file, so a failed upgrade
  is recoverable.
- Opening a DB whose ``user_version`` is **greater** than
  :data:`CURRENT_SCHEMA_VERSION` (written by a newer package, then
  downgraded) raises :class:`SchemaVersionError` instead of corrupting it.

The runner is invoked at the top of ``build`` (right after ``init_db``),
so the entire user-facing upgrade is ``pip install -U axiom-graph`` +
their normal ``build``.

Registered steps:

- **v1** — fold the legacy ``doc_sections`` table into ``nodes`` and retire
  the two-table model (ADR-021).
- **v2** — re-sync every DocJSON envelope's ``tags`` rows from the
  ``docs.tags`` JSON the DB already holds, repairing indexes whose envelope
  tags drifted while tag resync was gated on the node's stored text.

Preservation contract (ADR-021 migration amendment): migration steps never
modify ``node_history``, ``node_verification``, or ``node_renames``.
Preservation is structural — node IDs are stable, so those tables keep
pointing at the right rows.  The v1 step performs one **read-only** query
against ``node_history`` (preserved DELETED tombstone check) to avoid
resurrecting purged sections; it writes nothing there.  The v2 step reads
``docs`` and ``nodes`` and writes only ``tags``, so it disturbs neither
staleness nor verification state.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Callable

from axiom_graph.db._core import vacuum_into

logger = logging.getLogger(__name__)


#: Schema version written by the current package.  Bump when registering a
#: new migration step.
CURRENT_SCHEMA_VERSION = 2


class SchemaVersionError(RuntimeError):
    """The DB was written by a newer axiom-graph than the running package."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def get_user_version(conn: sqlite3.Connection) -> int:
    """Return the DB's ``PRAGMA user_version`` (0 for legacy DBs)."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


# ---------------------------------------------------------------------------
# Migration step v1 — legacy two-table model -> envelope-pattern nodes
# ---------------------------------------------------------------------------


def _migrate_v1_legacy_to_envelope(conn: sqlite3.Connection) -> None:
    """Fold ``doc_sections`` into ``nodes`` and retire the two-table model.

    Per ADR-021:

    - Adds the ``doc_position`` / ``doc_level`` columns to ``nodes``.
    - Copies every ``doc_sections`` row into an equivalent ``nodes`` row,
      **preserving IDs** (``doc_sections.id`` == node id).  Existing shadow
      rows are updated in place (staleness baseline ``code_hash`` and the
      persisted ``own_status`` / ``link_status`` are untouched); rows with
      no node are inserted — unless the ID carries a preserved ``DELETED``
      tombstone in ``node_history``, in which case the orphan stays dead
      (this is the purge-resurrection bug class the ADR eliminates).
    - Emits ``composes`` edges for the parent_id hierarchy (envelope ->
      top-level section, parent section -> child section) with a
      self-loop guard.
    - Copies section tags from the ``doc_sections.tags`` JSON column into
      the polymorphic ``tags`` table.
    - Renames subtypes: DocJSON sections become ``docjson_section``, the
      DocJSON file envelope becomes ``docjson_doc``.  Markdown doc nodes
      (source ``doc_scanner``) keep their legacy ``docjson`` subtype.
    - Re-syncs ``node_fts`` for every section node from the (now
      canonical) ``nodes`` content.
    - Drops the ``doc_sections`` table.  The thin ``docs`` metadata table
      (file provenance + doc tags) is kept.

    Never writes ``node_history`` / ``node_verification`` /
    ``node_renames``.
    """
    # 1. Column additions (guarded — idempotent under crash-retry).
    if not _column_exists(conn, "nodes", "doc_position"):
        conn.execute("ALTER TABLE nodes ADD COLUMN doc_position INTEGER")
    if not _column_exists(conn, "nodes", "doc_level"):
        conn.execute("ALTER TABLE nodes ADD COLUMN doc_level INTEGER")

    section_ids: list[str] = []

    if _table_exists(conn, "doc_sections"):
        doc_paths: dict[str, str] = {}
        if _table_exists(conn, "docs"):
            doc_paths = {r[0]: r[1] for r in conn.execute("SELECT id, file_path FROM docs").fetchall()}

        rows = conn.execute(
            "SELECT id, doc_id, heading, level, tags, content, desc_hash, "
            "parent_id, depth, position, updated_at FROM doc_sections"
        ).fetchall()

        for row in rows:
            sec_id = row["id"]
            doc_id = row["doc_id"]
            heading = row["heading"] or ""
            content = row["content"] or ""

            node_exists = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (sec_id,)).fetchone() is not None

            if not node_exists:
                # Orphan canonical row.  A preserved DELETED tombstone means
                # the section was deliberately purged — do NOT resurrect it.
                # (Read-only history check; nothing is written to history.)
                tombstoned = (
                    conn.execute(
                        "SELECT 1 FROM node_history WHERE node_id = ? AND change_type = 'DELETED' AND preserved = 1",
                        (sec_id,),
                    ).fetchone()
                    is not None
                )
                if tombstoned:
                    logger.info("migration v1: skipping tombstoned orphan section %s", sec_id)
                    continue
                # The ``docs`` row is the only trustworthy source of a doc's
                # file path.  Reconstructing one from the id is not possible:
                # the id's dots stand for both directory separators and dots
                # in filenames, so the mapping back to a path is ambiguous.
                location = doc_paths.get(doc_id) or ""
                conn.execute(
                    """
                    INSERT INTO nodes
                        (id, node_type, subtype, title, location, status, source,
                         code_hash, desc_hash, level_0, level_1, level_2,
                         level_3_location, doc_position, doc_level, updated_at)
                    VALUES (?, 'atomic_process', 'docjson_section', ?, ?, 'active', 'docjson',
                            ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sec_id,
                        heading,
                        location,
                        row["desc_hash"] or "",
                        row["desc_hash"],
                        heading,
                        heading,
                        content,
                        location,
                        row["position"],
                        row["level"],
                        row["updated_at"],
                    ),
                )
            else:
                # Existing (shadow) row: relocate content + section metadata.
                # code_hash (staleness baseline), own_status, link_status,
                # location, and level_3_location are deliberately untouched.
                conn.execute(
                    """
                    UPDATE nodes SET
                        node_type = 'atomic_process',
                        subtype = 'docjson_section',
                        title = ?, level_1 = ?, level_2 = ?,
                        desc_hash = ?, updated_at = ?,
                        doc_position = ?, doc_level = ?
                    WHERE id = ?
                    """,
                    (
                        heading,
                        heading,
                        content,
                        row["desc_hash"],
                        row["updated_at"],
                        row["position"],
                        row["level"],
                        sec_id,
                    ),
                )

            section_ids.append(sec_id)

            # composes hierarchy: parent section (nested) or doc envelope.
            parent_id = row["parent_id"] or doc_id
            if parent_id and parent_id != sec_id:  # self-loop guard
                conn.execute(
                    "INSERT OR IGNORE INTO edges (id, edge_type, from_id, to_id, weight) VALUES (?, 'composes', ?, ?, 1.0)",
                    (f"{parent_id}::composes::{sec_id}", parent_id, sec_id),
                )

            # Section tags -> polymorphic tags table.
            raw_tags = row["tags"]
            if raw_tags:
                try:
                    for tag in json.loads(raw_tags):
                        conn.execute(
                            "INSERT OR IGNORE INTO tags (node_id, tag) VALUES (?, ?)",
                            (sec_id, str(tag)),
                        )
                except (json.JSONDecodeError, TypeError):
                    pass

    # 3. Subtype renames.  Scoped by source so markdown doc nodes
    #    (source='doc_scanner', legacy subtype 'docjson') are untouched.
    conn.execute(
        "UPDATE nodes SET subtype = 'docjson_section' "
        "WHERE node_type = 'atomic_process' AND subtype = 'docjson' "
        "  AND source IN ('docjson', 'json_doc_scanner')"
    )
    conn.execute(
        "UPDATE nodes SET subtype = 'docjson_doc' "
        "WHERE node_type = 'composite_process' AND subtype = 'docjson' "
        "  AND source IN ('docjson', 'json_doc_scanner')"
    )

    # 4. FTS resync for the migrated sections (content is now canonical on
    #    the nodes row).
    for sec_id in section_ids:
        node_row = conn.execute("SELECT level_1, level_2 FROM nodes WHERE id = ?", (sec_id,)).fetchone()
        if node_row is None:
            continue
        conn.execute("DELETE FROM node_fts WHERE id = ?", (sec_id,))
        conn.execute(
            "INSERT INTO node_fts (id, level_1, level_2) VALUES (?, ?, ?)",
            (sec_id, node_row["level_1"] or "", node_row["level_2"] or ""),
        )

    # 5. Drop the legacy table.
    conn.execute("DROP TABLE IF EXISTS doc_sections")


# ---------------------------------------------------------------------------
# Migration step v2 — re-sync DocJSON envelope tag rows from ``docs.tags``
# ---------------------------------------------------------------------------


def _migrate_v2_resync_doc_envelope_tags(conn: sqlite3.Connection) -> None:
    """Bring every DocJSON envelope's ``tags`` rows into agreement with ``docs.tags``.

    A document's tags are stored twice: the ``docs.tags`` JSON column, which
    the writer rewrites from the file on every save, and ``tags`` rows on the
    document's envelope node, which is what every reader (tag search, tag
    filters, ``query_nodes(tag=...)``, the visualiser) actually queries.  The
    two could drift apart, because the node upsert used to gate its tag
    rewrite on the node's stored text having changed — and an envelope's
    stored text is only the first 4000 characters of its file, so a tag edit
    below that mark never reached the rows.  A document nobody edits again is
    never rescanned, so fixing the gate alone would leave the already-drifted
    rows unreachable.

    This step repairs them from data the database already holds: no file is
    read, nothing is rescanned, and no staleness, history, or verification
    state is touched.  It is idempotent — a second run finds every set already
    in agreement and writes nothing.

    Scoped by **node**: only ``docs`` rows that still have a ``docjson_doc``
    node are rewritten, so a stale ``docs`` row cannot leave orphan tag rows
    behind.  A NULL or unparseable ``docs.tags`` is tolerated — NULL means
    "no tags" and is honoured as such; unparseable JSON leaves the document's
    rows untouched rather than aborting the transaction.

    Never writes ``node_history`` / ``node_verification`` / ``node_renames``.

    Args:
        conn: Open connection inside the runner's migration transaction.
    """
    if not _table_exists(conn, "docs"):
        return

    rows = conn.execute(
        """
        SELECT d.id AS id, d.tags AS tags
        FROM docs d
        JOIN nodes n ON n.id = d.id
        WHERE n.subtype = 'docjson_doc'
        """
    ).fetchall()

    for row in rows:
        doc_id = row["id"]
        raw = row["tags"]
        desired: list[str] = []
        if raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("migration v2: unparseable docs.tags for %s — leaving its tag rows alone", doc_id)
                continue
            if not isinstance(parsed, list):
                logger.warning("migration v2: docs.tags for %s is not a list — leaving its tag rows alone", doc_id)
                continue
            desired = [str(tag) for tag in parsed]

        stored = {r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE node_id = ?", (doc_id,)).fetchall()}
        if stored == set(desired):
            continue
        conn.execute("DELETE FROM tags WHERE node_id = ?", (doc_id,))
        for tag in desired:
            conn.execute("INSERT OR IGNORE INTO tags (node_id, tag) VALUES (?, ?)", (doc_id, tag))
        logger.info("migration v2: re-synced tag rows for %s", doc_id)


#: Ordered registry of migration steps, keyed by **target** version.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1_legacy_to_envelope,
    2: _migrate_v2_resync_doc_envelope_tags,
}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_migrations(db_path: Path) -> list[int]:
    """Bring *db_path* up to :data:`CURRENT_SCHEMA_VERSION`.

    Invoked at the top of ``build`` (after ``init_db``).  No-op when the DB
    is already current.  Each pending step runs in its own transaction and
    stamps ``user_version`` inside that transaction, so a crash mid-step
    rolls back and the step retries on the next run.

    Args:
        db_path: Path to the axiom-graph SQLite database.

    Returns:
        The list of migration target versions applied this run (empty when
        the DB was already current or does not exist).

    Raises:
        SchemaVersionError: When the DB's ``user_version`` exceeds
            :data:`CURRENT_SCHEMA_VERSION` (DB written by a newer package).
    """
    if not db_path.exists():
        return []

    # Peek at the version with a short-lived read connection.
    peek = sqlite3.connect(db_path, timeout=5)
    try:
        version = get_user_version(peek)
    finally:
        peek.close()

    if version > CURRENT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database {db_path} has schema version {version}, but this "
            f"axiom-graph supports at most {CURRENT_SCHEMA_VERSION}. "
            f"It was written by a newer axiom-graph — upgrade the package "
            f"(pip install -U axiom-graph) instead of downgrading."
        )

    pending = sorted(v for v in MIGRATIONS if v > version)
    if not pending:
        return []

    # Pre-migration backup (VACUUM INTO refuses an existing target — clear a
    # stale backup from an earlier interrupted attempt first).
    backup = db_path.with_name(f"{db_path.name}.pre-v{pending[-1]}.bak")
    if backup.exists():
        backup.unlink()
    vacuum_into(db_path, backup)
    logger.info("migrations: backed up %s -> %s", db_path.name, backup.name)

    applied: list[int] = []
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        for target in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[target](conn)
                # user_version participates in the transaction — a rollback
                # reverts the stamp along with the data changes.
                conn.execute(f"PRAGMA user_version = {int(target):d}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            applied.append(target)
            logger.info("migrations: applied schema migration -> v%d", target)
    finally:
        conn.close()

    return applied


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIGRATIONS",
    "SchemaVersionError",
    "get_user_version",
    "run_migrations",
]
