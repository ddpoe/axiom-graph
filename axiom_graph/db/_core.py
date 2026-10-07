"""Axiom-graph DB core: schema, connection, and (de)serialisation helpers.

Shared plumbing used by every other ``axiom_graph.db`` submodule.  Contains:

- Schema DDL (``_SCHEMA_SQL``) and ``init_db``
- Connection helpers (``_connect``, ``_now_utc``, ``vacuum_into``)
- Row <-> dataclass serdes (``_node_to_row``, ``_row_to_node``,
  ``_edge_to_row``, ``_row_to_edge``, ``_steps_to_json``,
  ``_json_to_steps``, ``_derive_change_type``)
"""

from __future__ import annotations

import contextlib as _contextlib
import contextvars as _contextvars
import functools
import inspect
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
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
--   * subtype='docjson_section' rows: last-edit timestamp, bumped whenever
--     the section's heading/content actually changes (any write path).
--     The section's own staleness baseline lives in code_hash only;
--     desc_hash/level_1/level_2 always mirror the current file content
--     (ADR-021 envelope model — sections are first-class nodes).
-- doc_position / doc_level are DocJSON section metadata (subtype=
-- 'docjson_section'): sibling-scoped render order and heading level.
-- NULL for every other row.
-- nodes.file_mtime holds THE FILE'S ON-DISK MODIFICATION TIME AS OBSERVED
-- WHEN THIS BUILD LAST SCANNED THE FILE'S BYTES.  It is not a wall-clock
-- scan timestamp, and it is not a claim that the staleness baseline agrees
-- with those bytes (that is code_hash plus the content gate).  It is the
-- builder's scan-skip cache: advancing it promises the next build may
-- safely skip the file entirely, so ONLY a full per-file index pass --
-- nodes AND edges AND doc/section records -- may advance it.  Partial
-- refreshers (mark_clean, rescan_file_if_needed) leave it alone.
-- Stored on file-level rows (module / doc / config / section); NULL on
-- function rows.
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
    doc_position     INTEGER,
    doc_level        INTEGER,
    updated_at       TEXT NOT NULL,
    live_code_hash   TEXT,
    live_desc_hash   TEXT
);
-- live_code_hash / live_desc_hash (schema v5): the node's hashes as the last
-- staleness pass that re-hashed its file found them, which may differ from
-- the code_hash / desc_hash baseline.  NULL means "not re-hashed since the
-- row was written" and '' means "baseline reset by a verification since
-- the last re-hash" (the staleness fast pass then re-hashes the file);
-- either way readers take the baseline pair instead (keyed on
-- live_code_hash: a NULL live_desc_hash is a real value when the code hash
-- is set).  Every baseline write leaves them equal to the new baseline, or
-- NULL.  An existing DB gains the columns in migration v5, so nothing in
-- this script may reference them.

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

-- Thin doc-level metadata (file provenance + doc tags).  Section content
-- lives in ``nodes`` (subtype='docjson_section') per ADR-021 — there is
-- deliberately no doc_sections table.
CREATE TABLE IF NOT EXISTS docs (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    tags        TEXT,
    file_path   TEXT NOT NULL,
    desc_hash   TEXT,
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

-- Index-wide key/value facts, e.g. ``project_id``: the project id the
-- index was built with.  Created by init_db on every build (IF NOT
-- EXISTS), so an index from before this table existed gains it on its
-- next build with no versioned migration.
CREATE TABLE IF NOT EXISTS index_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

-- Annotation findings store (schema v4).  Written only by build; read by
-- build and check.  ``kind`` selects what a row holds:
--   * 'finding'  -- one raw scanner finding of ``file`` (rules A1-C1),
--                   unfiltered by the [validation] config;
--   * 'autostep' -- one AutoStep record of ``file``, as JSON in ``record``,
--                   kept raw so B4 is resolved against the index when read;
--   * 'b4'       -- one B4 finding as the last build resolved it (the
--                   previous set, so B4 can be new or resolved without its
--                   own file changing);
--   * 'dotted'   -- one dotted DocJSON filename the last build saw
--                   (``file`` is the path; no rule columns).
-- ``file`` is the project-relative POSIX path.  A build replaces the
-- 'finding'/'autostep' rows of every file it scanned and drops the rows of
-- files it no longer walks; it replaces 'b4' and 'dotted' whole.  The DDL
-- is ``_ANNOTATION_FINDINGS_DDL`` below, shared with migration v4.
"""

#: DDL of the annotation findings store, one statement per entry so the v4
#: migration can run it inside its own transaction (``executescript`` would
#: commit early).  Appended to :data:`_SCHEMA_SQL` for fresh DBs.
_ANNOTATION_FINDINGS_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS annotation_findings (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind      TEXT NOT NULL,
    file      TEXT NOT NULL,
    rule_id   TEXT,
    severity  TEXT,
    function  TEXT,
    line      INTEGER,
    message   TEXT,
    record    TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_annotation_findings_kind_file ON annotation_findings (kind, file)",
)

_SCHEMA_SQL += "".join(f"\n{statement};\n" for statement in _ANNOTATION_FINDINGS_DDL)

#: DDL of the verification pairs table (schema v5): one row per dependency
#: target a verification saw, holding that target's live hash at the time.
#: ``desc_hash`` is set only for targets an ``annotates`` link names.  The
#: rows live and die with their verification row (``ON DELETE CASCADE``) and
#: follow its id (``ON UPDATE CASCADE``).  One statement per entry so the v5
#: migration can run it inside its own transaction; appended to
#: :data:`_SCHEMA_SQL` for fresh DBs (safe to re-run on a v4 DB: it touches
#: no ``nodes`` column).
_VERIFICATION_TARGETS_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS node_verification_targets (
    node_id    TEXT NOT NULL REFERENCES node_verification(node_id) ON DELETE CASCADE ON UPDATE CASCADE,
    target_id  TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    desc_hash  TEXT,
    PRIMARY KEY (node_id, target_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_verification_targets_target ON node_verification_targets (target_id)",
)

_SCHEMA_SQL += "".join(f"\n{statement};\n" for statement in _VERIFICATION_TARGETS_DDL)

#: DDL of the per-file record and the lookup indexes the scoped staleness
#: refresh reads through.  ``file_state`` holds, per tracked file: the
#: whole-file fingerprint of the content the last build parsed
#: (``parsed_fp``), the fingerprint of the content the last staleness
#: re-hash read (``hashed_fp``; :data:`MISSING_FILE_FP` when the file was
#: gone), and the stat it was last observed at.  Additive objects with no
#: data to carry, created ``IF NOT EXISTS`` like ``index_meta``, so no
#: versioned migration step; none references a schema-v5 column, so the
#: script stays safe to re-run on any older DB.  One statement per entry.
_FILE_STATE_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS file_state (
    location   TEXT PRIMARY KEY,
    parsed_fp  TEXT,
    hashed_fp  TEXT,
    mtime      REAL,
    size       INTEGER,
    structure  TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_nodes_location ON nodes (location)",
    "CREATE INDEX IF NOT EXISTS idx_edges_to ON edges (to_id, edge_type)",
    "CREATE INDEX IF NOT EXISTS idx_edges_from ON edges (from_id, edge_type)",
)

_SCHEMA_SQL += "".join(f"\n{statement};\n" for statement in _FILE_STATE_DDL)

#: DDL of the node deletion log: one row per node id that left the ``nodes``
#: table, by a ``DELETE`` or by an ``UPDATE`` of its id (a rename re-key),
#: written by the two triggers below whatever code path removed the row.
#: The discovery refresh reads the rows past its mark (``index_meta`` key
#: :data:`DELETION_MARK_META_KEY`) to find the links a deletion left
#: dangling, instead of scanning every link; it drops the rows it consumed.
#: ``AUTOINCREMENT`` keeps ids rising after that drop, so a mark is never
#: overtaken by a reused id.  References only ``nodes.id``, so the script
#: stays safe to re-run on any older DB; a code version that predates the
#: log leaves its rows unread and is otherwise unaffected.  A migration that
#: rebuilds ``nodes`` must re-create the triggers.  One statement per entry.
_DELETION_LOG_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS node_deletion_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id  TEXT NOT NULL
)""",
    """CREATE TRIGGER IF NOT EXISTS trg_nodes_deletion_log AFTER DELETE ON nodes
BEGIN
    INSERT INTO node_deletion_log (node_id) VALUES (OLD.id);
END""",
    """CREATE TRIGGER IF NOT EXISTS trg_nodes_rekey_log AFTER UPDATE OF id ON nodes
WHEN OLD.id != NEW.id
BEGIN
    INSERT INTO node_deletion_log (node_id) VALUES (OLD.id);
END""",
)

_SCHEMA_SQL += "".join(f"\n{statement};\n" for statement in _DELETION_LOG_DDL)

#: ``index_meta`` key: the last ``node_deletion_log.id`` a discovery refresh consumed.
DELETION_MARK_META_KEY = "staleness_deletion_mark"

#: DDL of the indexes the build's and the check's whole-index reads go
#: through, so each reads an index sized by what it asks about (or a
#: narrow covering index) instead of the ``nodes`` / ``edges`` table.
#: Several name schema-v5 columns (``own_status``, ``link_status``,
#: ``live_code_hash``), which an older DB gains only in migration v5, so
#: these are not in :data:`_SCHEMA_SQL`: ``init_db`` runs them on a v5 DB
#: and :func:`axiom_graph.db.files.ensure_file_state_conn` on the upgrade
#: path.  One statement per entry.
_SCAN_INDEX_DDL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_edges_type_from_to ON edges (edge_type, from_id, to_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_file_mtime ON nodes (location, file_mtime) WHERE file_mtime IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_nodes_reset ON nodes (location) WHERE live_code_hash = ''",
    "CREATE INDEX IF NOT EXISTS idx_nodes_level_2_length ON nodes (LENGTH(level_2))",
    "CREATE INDEX IF NOT EXISTS idx_nodes_subtype_location ON nodes (subtype, location, id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_status_pair ON nodes (own_status, link_status)",
    # Markdown doc envelopes (``all_doc_ids``): a read of the docs, not of every node.
    "CREATE INDEX IF NOT EXISTS idx_nodes_doc_envelopes ON nodes (id) "
    "WHERE node_type = 'composite_process' AND source = 'doc_scanner'",
)

#: Names of the :data:`_SCAN_INDEX_DDL` indexes (one presence probe instead of re-running the DDL).
_SCAN_INDEX_NAMES: tuple[str, ...] = tuple(s.split(" IF NOT EXISTS ", 1)[1].split(" ", 1)[0] for s in _SCAN_INDEX_DDL)


def ensure_scan_indexes_conn(conn: sqlite3.Connection) -> None:
    """Create the :data:`_SCAN_INDEX_DDL` indexes on a schema-v5 DB that lacks one.

    One ``sqlite_master`` probe when they all exist; below v5 (no
    ``own_status`` column yet) nothing is created.

    Args:
        conn: Open connection (caller owns the transaction).
    """
    ph = ",".join("?" * len(_SCAN_INDEX_NAMES))
    row = conn.execute(
        f"SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name IN ({ph})",  # noqa: S608 - placeholders only
        _SCAN_INDEX_NAMES,
    ).fetchone()
    if int(row[0]) == len(_SCAN_INDEX_NAMES) or not pairs_ready(conn):
        return
    for statement in _SCAN_INDEX_DDL:
        conn.execute(statement)


#: ``file_state.hashed_fp`` of a file that was missing when it was last re-hashed.
MISSING_FILE_FP = "!missing"

#: ``nodes.live_code_hash`` of a node the last re-hash of its file could not find.
MISSING_LIVE_HASH = "!"

#: ``node_verification_targets.code_hash`` of an open receipt (a pin).  A doc
#: write that has to create a section's verification row records one for each
#: open offender that holds no receipt: no live hash ever equals it, so the new
#: row's time cannot settle that offender, and it stays open until a
#: verification of the link (``addresses=``, ``mark_clean``, ``reverify``)
#: records its real hash.
OPEN_RECEIPT_HASH = "!open"

#: First schema version whose index stores verification pairs and live hashes.
PAIRS_SCHEMA_VERSION = 5


def pairs_ready(conn: sqlite3.Connection) -> bool:
    """Whether the index stores verification pairs and live hashes (schema v5+).

    An index still at v4 (a package upgrade before its first build) has
    neither, and every pair reader and writer then behaves exactly as before
    pairs existed: the clock rule alone.

    Args:
        conn: Open connection to the index.

    Returns:
        ``True`` once the DB's ``user_version`` is at least
        :data:`PAIRS_SCHEMA_VERSION`.
    """
    row = conn.execute("PRAGMA user_version").fetchone()
    return bool(row) and int(row[0]) >= PAIRS_SCHEMA_VERSION


_FTS_TRIGGERS_SQL = ""  # FTS is synced manually in upsert_node


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _open(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with the pragmas every ``_connect`` block runs under.

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
    return conn


@dataclass(frozen=True)
class _Scope:
    """The connection one api operation runs on (see :func:`operation_connection`)."""

    key: str
    conn: sqlite3.Connection
    thread: int


_SCOPE: _contextvars.ContextVar[_Scope | None] = _contextvars.ContextVar("axiom_graph_db_scope", default=None)


def _scope_key(db_path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(db_path)))


def _active_scope(db_path: Path | str) -> _Scope | None:
    """Return the open operation scope for *db_path* on this thread, if any."""
    scope = _SCOPE.get()
    if scope is None or scope.thread != threading.get_ident() or scope.key != _scope_key(db_path):
        return None
    return scope


@_contextlib.contextmanager
def _connect(db_path: Path):
    """Yield a SQLite connection that commits on success and always closes.

    Inside an operation scope (:func:`operation_connection`) for the same
    DB on the same thread, the block runs on the operation's connection
    when no transaction is open on it: the block commits its own writes at
    its own end, exactly where a connection of its own would have, and the
    connection stays open for the rest of the operation.  A block opened
    while the operation's connection has uncommitted writes opens a
    connection of its own, as it always has, so it neither sees those
    writes nor commits them.

    Logs a WARNING when lock acquisition exceeds 100ms.  When
    AXIOM_GRAPH_LOG_LEVEL=DEBUG, enables SQLite trace callback to log each
    SQL statement (truncated to 200 chars).
    """
    scope = _active_scope(db_path)
    if scope is not None and not scope.conn.in_transaction:
        shared = scope.conn
        try:
            yield shared
            shared.commit()
        except BaseException:
            shared.rollback()
            raise
        return

    conn = _open(db_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@_contextlib.contextmanager
def operation_connection(db_path: Path | str):
    """Run an api operation on one connection, opened here and closed when the operation ends.

    Every :func:`_connect` block the operation opens on *db_path* (on this
    thread) reuses this connection while no transaction is open on it, and
    commits at its own end as before; the scope adds no transaction and holds
    no lock between blocks.  Nested scopes for the same DB join the outer one.

    Args:
        db_path: Path to the axiom-graph DB.

    Yields:
        The operation's connection.  Statements run on it outside a
        ``_connect`` block commit when the scope ends.
    """
    scope = _active_scope(db_path)
    if scope is not None:
        yield scope.conn
        return
    conn = _open(db_path)
    token = _SCOPE.set(_Scope(_scope_key(db_path), conn, threading.get_ident()))
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        _SCOPE.reset(token)
        conn.close()


def connection_scope(func):
    """Decorate an api entry point that takes a ``db_path`` argument: it runs in :func:`operation_connection`.

    Args:
        func: The entry point.  Its ``db_path`` parameter (positional or
            keyword) names the DB.

    Returns:
        The wrapped function.
    """
    position = list(inspect.signature(func).parameters).index("db_path")

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        db_path = args[position] if len(args) > position else kwargs["db_path"]
        with operation_connection(db_path):
            return func(*args, **kwargs)

    return wrapper


def open_connection(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection the caller closes: WAL journal, ``sqlite3.Row`` rows, 5 s busy timeout.

    For callers that keep a connection across statements they manage
    themselves (the viz request handlers) or need a read-only one (the SQL
    tool).  Everything else uses :func:`_connect`, which commits and closes.

    Args:
        db_path: Path to the axiom-graph DB.
        read_only: Open through SQLite's ``mode=ro`` URI, so no statement can
            write.  The journal mode is left as the file records it: setting
            it is a write, which a DB not yet in WAL mode (a fresh
            ``VACUUM INTO`` copy) refuses on a read-only connection.

    Returns:
        The open connection.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(str(db_path), timeout=5)
    try:
        if not read_only:
            conn.execute("PRAGMA journal_mode=WAL")
    except BaseException:
        conn.close()
        raise
    conn.row_factory = sqlite3.Row
    return conn


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
        "doc_position": node.doc_position,
        "doc_level": node.doc_level,
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
        doc_position=d.get("doc_position"),
        doc_level=d.get("doc_level"),
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
    """Create the schema if it does not already exist.

    A **fresh** DB (no ``nodes`` table yet) is created on the current
    envelope schema and stamped with ``PRAGMA user_version =
    CURRENT_SCHEMA_VERSION`` so it is never mistaken for a legacy DB
    needing migration.  An **existing** DB keeps its stored
    ``user_version`` untouched — the migration runner
    (:func:`axiom_graph.db.migrations.run_migrations`) is responsible for
    upgrading legacy DBs (which read as version 0).
    """
    from axiom_graph.db.migrations import CURRENT_SCHEMA_VERSION  # noqa: PLC0415

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        is_fresh = (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nodes'").fetchone() is None
        )
        conn.executescript(_SCHEMA_SQL)
        if is_fresh:
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION:d}")
        ensure_scan_indexes_conn(conn)


def get_index_meta(db_path: Path, key: str) -> str | None:
    """Return the index-wide value stored under *key*, or ``None``.

    Reads the ``index_meta`` table.  An index that predates the table (it
    is added by :func:`init_db` on the next build) reads as holding nothing.

    Args:
        db_path: Path to the axiom-graph DB.
        key: The fact to read, e.g. ``"project_id"``.

    Returns:
        The stored value, or ``None`` when the key or the table is absent.
    """
    with _connect(db_path) as conn:
        try:
            row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None
    return row["value"] if row else None


def single_node_id_prefix(db_path: Path) -> str | None:
    """Return the project prefix every node id shares, or ``None``.

    One query: the distinct ``<prefix>`` of ``<prefix>::...`` node ids,
    stopped at two.  Used to recognise the project id of an index built
    before the id was stored.

    Args:
        db_path: Path to the axiom-graph DB.

    Returns:
        The one prefix, or ``None`` when the index has no nodes, its nodes
        carry more than one prefix, or it has no ``nodes`` table.
    """
    with _connect(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT DISTINCT substr(id, 1, instr(id, '::') - 1) AS prefix "
                "FROM nodes WHERE instr(id, '::') > 1 LIMIT 2"
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    return rows[0]["prefix"] if len(rows) == 1 else None


def set_index_meta(db_path: Path, key: str, value: str) -> None:
    """Store *value* under *key* in the ``index_meta`` table, replacing any old value.

    Args:
        db_path: Path to the axiom-graph DB (schema already initialised).
        key: The fact to write, e.g. ``"project_id"``.
        value: The value to store.
    """
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO index_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


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
    "_ANNOTATION_FINDINGS_DDL",
    "_VERIFICATION_TARGETS_DDL",
    "_FILE_STATE_DDL",
    "_DELETION_LOG_DDL",
    "_SCAN_INDEX_DDL",
    "DELETION_MARK_META_KEY",
    "ensure_scan_indexes_conn",
    "MISSING_FILE_FP",
    "MISSING_LIVE_HASH",
    "OPEN_RECEIPT_HASH",
    "PAIRS_SCHEMA_VERSION",
    "pairs_ready",
    "_FTS_TRIGGERS_SQL",
    "_HISTORY_ROW_LIMIT",
    "init_db",
    "get_index_meta",
    "set_index_meta",
    "single_node_id_prefix",
    "vacuum_into",
    # Connections
    "_connect",
    "operation_connection",
    "connection_scope",
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
