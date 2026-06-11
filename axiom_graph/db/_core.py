"""Axiom-graph DB core: schema, connection, and (de)serialisation helpers.

Shared plumbing used by every other ``axiom_graph.db`` submodule.  Contains:

- Schema DDL (``_SCHEMA_SQL``) and ``init_db``
- Connection helpers (``_connect``, ``_vec_connect``, ``_load_sqlite_vec``,
  ``_vec_to_bytes``, ``_now_utc``, ``vacuum_into``)
- Row <-> dataclass serdes (``_node_to_row``, ``_row_to_node``,
  ``_edge_to_row``, ``_row_to_edge``, ``_steps_to_json``,
  ``_json_to_steps``, ``_derive_change_type``)
"""

from __future__ import annotations

import contextlib as _contextlib
import json
import logging
import os
import sqlite3
import struct
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from axiom_graph.models import AxiomEdge, AxiomNode, StepMarker

# Logger name is kept as ``axiom_graph.index.db`` for backward compatibility:
# existing tests and observability integrations key on this name to capture
# the slow-connect warnings emitted by ``_connect``.
logger = logging.getLogger("axiom_graph.index.db")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HISTORY_ROW_LIMIT = 100  # max rows kept per node in DB (verified rows exempt)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- nodes.updated_at has DIFFERENT SEMANTICS depending on subtype:
--   * code rows: staleness baseline. Preserved across rescans by
--     upsert_node(discovery_only=True) so node_history.scanned_at can be
--     diffed against it to detect drift. Bumped only by a discovery_only=False
--     full build when the row's content actually changed.
--   * subtype='docjson' shadow rows: NOT authoritative. The canonical column
--     is doc_sections.updated_at, which is bumped on every section write.
--     The shadow's updated_at is frozen at initial-index time because the
--     same discovery_only=True path also runs for these rows. Staleness
--     queries that need a doc-section's last-edit timestamp MUST read
--     doc_sections.updated_at, not nodes.updated_at.
CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    node_type        TEXT NOT NULL,
    subtype          TEXT,
    title            TEXT NOT NULL,
    location         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    source           TEXT NOT NULL,
    code_hash        TEXT NOT NULL,
    desc_hash        TEXT,
    file_mtime       REAL,
    level_0          TEXT NOT NULL,
    level_1          TEXT NOT NULL,
    level_2          TEXT,
    level_3_location TEXT,
    level_steps      TEXT,
    dflow_meta       TEXT,
    staleness        TEXT NOT NULL DEFAULT 'VERIFIED',
    own_status       TEXT NOT NULL DEFAULT 'VERIFIED',
    link_status      TEXT NOT NULL DEFAULT 'VERIFIED',
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    edge_type   TEXT NOT NULL,
    from_id     TEXT NOT NULL,
    to_id       TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    meta        TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    node_id TEXT NOT NULL,
    tag     TEXT NOT NULL,
    PRIMARY KEY (node_id, tag)
);

CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    id,
    level_1,
    level_2
);

CREATE TABLE IF NOT EXISTS node_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      TEXT    NOT NULL,
    scanned_at   TEXT    NOT NULL,
    change_type  TEXT    NOT NULL,
    git_sha      TEXT,
    meta         TEXT,
    preserved    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_history_node_id ON node_history (node_id, id DESC);

CREATE TABLE IF NOT EXISTS docs (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    tags        TEXT,
    file_path   TEXT NOT NULL,
    desc_hash   TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS doc_sections (
    id          TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL REFERENCES docs(id),
    heading     TEXT NOT NULL,
    level       INTEGER NOT NULL DEFAULT 2,
    tags        TEXT,
    content     TEXT,
    desc_hash   TEXT,
    parent_id   TEXT,
    depth       INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_verification (
    node_id       TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'VERIFIED',
    verified_at   TEXT NOT NULL,
    verified_by   TEXT NOT NULL,
    reason        TEXT,
    code_hash_at  TEXT NOT NULL,
    desc_hash_at  TEXT
);

CREATE TABLE IF NOT EXISTS node_renames (
    old_id      TEXT NOT NULL,
    new_id      TEXT NOT NULL,
    renamed_at  TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    PRIMARY KEY (old_id, new_id)
);
"""

_FTS_TRIGGERS_SQL = ""  # FTS is synced manually in upsert_node


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


@_contextlib.contextmanager
def _connect(db_path: Path):
    """Yield a SQLite connection that commits on success and always closes.

    Logs a WARNING when lock acquisition exceeds 100ms.  When
    AXIOM_GRAPH_LOG_LEVEL=DEBUG, enables SQLite trace callback to log each
    SQL statement (truncated to 200 chars).
    """
    t0 = time.monotonic()
    conn = sqlite3.connect(db_path, timeout=5)
    elapsed_ms = (time.monotonic() - t0) * 1000
    if elapsed_ms > 100:
        logger.warning("slow SQLite connect: %.0fms for %s", elapsed_ms, db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Query tracing at DEBUG level
    if os.environ.get("AXIOM_GRAPH_LOG_LEVEL", "").upper() == "DEBUG":

        def _trace_callback(statement: str) -> None:
            logger.debug("SQL: %s", statement[:200])

        conn.set_trace_callback(_trace_callback)

    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sqlite-vec extension loading
# ---------------------------------------------------------------------------


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into an open connection.

    Raises:
        OSError: If sqlite-vec is not installed or cannot be loaded.
    """
    import sqlite_vec  # type: ignore[import-untyped]

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)


def _vec_connect(db_path: Path):
    """Yield a SQLite connection with sqlite-vec loaded.

    Same as _connect but also loads the sqlite-vec extension.

    Raises:
        OSError: If sqlite-vec is not available.
    """
    import contextlib

    t0 = time.monotonic()
    conn = sqlite3.connect(db_path, timeout=5)
    elapsed_ms = (time.monotonic() - t0) * 1000
    if elapsed_ms > 100:
        logger.warning("slow SQLite connect: %.0fms for %s", elapsed_ms, db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _load_sqlite_vec(conn)

    @contextlib.contextmanager
    def _ctx():
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    return _ctx()


def _vec_to_bytes(vec: list[float]) -> bytes:
    """Serialize a float vector to little-endian bytes for sqlite-vec."""
    return struct.pack(f"<{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Single source of truth for timestamp formatting so ISO string comparisons
    in queries are always consistent across all DB writes.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _steps_to_json(steps: list[StepMarker] | None) -> str | None:
    if steps is None:
        return None
    return json.dumps([asdict(s) for s in steps])


def _json_to_steps(raw: str | None) -> list[StepMarker] | None:
    if not raw:
        return None
    data = json.loads(raw)
    return [StepMarker(**s) for s in data]


def _node_to_row(node: AxiomNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "node_type": node.node_type,
        "subtype": node.subtype,
        "title": node.title,
        "location": node.location,
        "status": node.status,
        "source": node.source,
        "code_hash": node.code_hash,
        "desc_hash": node.desc_hash,
        "file_mtime": node.file_mtime,
        "level_0": node.level_0,
        "level_1": node.level_1,
        "level_2": node.level_2,
        "level_3_location": node.level_3_location,
        "level_steps": _steps_to_json(node.level_steps),
        "dflow_meta": json.dumps(node.dflow_meta) if node.dflow_meta else None,
        "updated_at": _now_utc(),
    }


def _row_to_node(row: sqlite3.Row) -> AxiomNode:
    d = dict(row)
    return AxiomNode(
        id=d["id"],
        node_type=d["node_type"],
        subtype=d["subtype"],
        title=d["title"],
        location=d["location"],
        status=d["status"],
        source=d["source"],
        code_hash=d["code_hash"],
        desc_hash=d.get("desc_hash"),
        file_mtime=d.get("file_mtime"),
        level_0=d["level_0"],
        level_1=d["level_1"],
        level_2=d["level_2"],
        level_3_location=d["level_3_location"],
        level_steps=_json_to_steps(d.get("level_steps")),
        dflow_meta=json.loads(d["dflow_meta"]) if d.get("dflow_meta") else None,
        tags=[],  # populated separately if needed
    )


def _edge_to_row(edge: AxiomEdge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "edge_type": edge.edge_type,
        "from_id": edge.from_id,
        "to_id": edge.to_id,
        "weight": edge.weight,
        "meta": json.dumps(edge.meta) if edge.meta else None,
    }


def _row_to_edge(row: sqlite3.Row) -> AxiomEdge:
    d = dict(row)
    return AxiomEdge(
        id=d["id"],
        edge_type=d["edge_type"],
        from_id=d["from_id"],
        to_id=d["to_id"],
        weight=d["weight"],
        meta=json.loads(d["meta"]) if d.get("meta") else None,
    )


# ---------------------------------------------------------------------------
# History change_type derivation
# ---------------------------------------------------------------------------


def _derive_change_type(
    old_code: str | None,
    old_desc: str | None,
    new_code: str,
    new_desc: str | None,
) -> str | None:
    """Return the change_type string, or None if nothing changed (caller skips write)."""
    if old_code is None:
        return "INITIAL"
    code_changed = old_code != new_code
    desc_changed = old_desc != new_desc
    if code_changed and not desc_changed:
        return "CONTENT_ONLY"
    if not code_changed and desc_changed:
        return "DESC_ONLY"
    if code_changed and desc_changed:
        return "CONTENT_AND_DESC"
    return None  # unchanged


# ---------------------------------------------------------------------------
# Public schema API
# ---------------------------------------------------------------------------


def init_db(db_path: Path) -> None:
    """Create the schema if it does not already exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA_SQL)


def vacuum_into(source: Path, target: Path) -> None:
    """Copy an axiom-graph DB via VACUUM INTO (atomic, WAL-safe).

    Args:
        source: Path to the source .axiom_graph/graph.db file.
        target: Path where the copy should be written.

    Raises:
        ValueError: If either path contains a single quote.
        FileNotFoundError: If the source DB does not exist.
    """
    for p in (source, target):
        if "'" in str(p):
            raise ValueError(f"Path contains single quote, cannot use VACUUM INTO: {p}")
    if not source.exists():
        raise FileNotFoundError(f"Source DB not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _connect(source) as conn:
        conn.execute(f"VACUUM INTO '{target}'")


__all__ = [
    # Schema / DDL
    "_SCHEMA_SQL",
    "_FTS_TRIGGERS_SQL",
    "_HISTORY_ROW_LIMIT",
    "init_db",
    "vacuum_into",
    # Connections
    "_connect",
    "_vec_connect",
    "_load_sqlite_vec",
    "_vec_to_bytes",
    "_now_utc",
    # Serdes
    "_steps_to_json",
    "_json_to_steps",
    "_node_to_row",
    "_row_to_node",
    "_edge_to_row",
    "_row_to_edge",
    "_derive_change_type",
]
