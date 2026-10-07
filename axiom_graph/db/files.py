"""Axiom-graph DB: per-file records and the staleness stamps in ``index_meta``.

The scoped staleness refresh decides which files to re-hash by comparing each
file's whole-file fingerprint with the per-file record (``file_state``), and
reads the ``node_history`` rows past a watermark as its journal.  The table
and its indexes are created ``IF NOT EXISTS`` (see
:data:`axiom_graph.db._core._FILE_STATE_DDL`); every reader here tolerates
their absence on an index :func:`axiom_graph.db._core.init_db` has not
touched since they were added.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from axiom_graph.db._core import _DELETION_LOG_DDL, _FILE_STATE_DDL, MISSING_FILE_FP, ensure_scan_indexes_conn

#: ``index_meta`` key: the scheme stamp the stored statuses were computed under.
STALENESS_SCHEME_META_KEY = "staleness_scheme"
#: ``index_meta`` key: the last ``node_history.id`` a check's refresh has read.
STALENESS_WATERMARK_META_KEY = "staleness_watermark"
#: ``index_meta`` key: the scopes the next discovery refresh re-checks (a
#: JSON list of :class:`OpenPass` entries), of two kinds.
#:
#: - A discovery or full pass records itself in the transaction of its
#:   own-phase write and removes itself in its last write transaction, so
#:   its entry outlives only a pass that stopped part-way.
#: - A cone (a write or read tool's scoped refresh) has no last write of its
#:   own to remove an entry in without adding a commit, so it merges its seed
#:   nodes into the one *carried* entry instead.  That entry holds the nodes
#:   the tools refreshed since the last check, whether or not their cone
#:   finished; the next discovery refresh re-checks them and removes it.
STALENESS_OPEN_PASSES_META_KEY = "staleness_open_passes"
#: An open pass whose seed set is larger than this is recorded as full: its
#: recovery is one full pass rather than a pass over the recorded seeds.
OPEN_PASS_MAX_IDS = 2000
#: The carried entry turns full once it holds more distinct nodes than this.
#: A scoped re-check over the carried nodes is always correct, so the cap
#: only bounds the entry's size: every later cone that stores an input reads
#: and rewrites the entry under the write lock.  At this size the entry is
#: about two megabytes at most, and a union this large is a large part of
#: most indexes, where one full pass costs about what the scoped re-check
#: would; a batch of a few thousand nodes stays scoped.
CARRIED_PASS_MAX_IDS = 20000
#: :attr:`OpenPass.reason` of the full entry a staleness pass records when its
#: envelope check failed: the pass finished, but an envelope flag may be
#: missing, so the next discovery refresh runs in full.
OPEN_PASS_ENVELOPE_FAILED = "envelope-check-failed"

#: ``node_history.change_type`` values that never change a staleness input,
#: so the journal skips them (a checkpoint writes one per node).
INERT_JOURNAL_TYPES: tuple[str, ...] = ("CHECKPOINT", "RENAME_SCORING_SKIPPED")


@dataclass(frozen=True)
class FileRecord:
    """One tracked file's stored observation.

    Attributes:
        parsed_fp: Fingerprint of the content the last build parsed, or ``None``.
        hashed_fp: Fingerprint of the content the last staleness re-hash read
            (``MISSING_FILE_FP`` when the file was gone), or ``None``.
        mtime: Modification time at the last observation, or ``None``.
        size: Size in bytes at the last observation, or ``None``.
    """

    parsed_fp: str | None
    hashed_fp: str | None
    mtime: float | None
    size: int | None


def file_state_ready(conn: sqlite3.Connection) -> bool:
    """Whether the index holds the ``file_state`` table.

    Args:
        conn: Open connection.

    Returns:
        ``True`` once ``init_db`` (or :func:`ensure_file_state_conn`) created it.
    """
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'file_state'").fetchone()
    return row is not None


def ensure_file_state_conn(conn: sqlite3.Connection) -> None:
    """Create the per-file record table and the lookup indexes if they are missing.

    The same ``IF NOT EXISTS`` statements ``init_db`` runs, for a staleness
    pass that reaches an index ``init_db`` has not touched since they were
    added.

    Args:
        conn: Open connection (caller owns the transaction).
    """
    for statement in (*_FILE_STATE_DDL, *_DELETION_LOG_DDL):
        conn.execute(statement)
    ensure_scan_indexes_conn(conn)


def max_deletion_log_id_conn(conn: sqlite3.Connection) -> int:
    """Return the newest id ever given to a ``node_deletion_log`` row, ``0`` when none was or the log is absent.

    The ``AUTOINCREMENT`` high-water mark (``sqlite_sequence``), not the
    largest id still in the table: a refresh drops the rows it consumed, and
    the mark it stored must still read as current afterwards, so the next
    refresh finds it unmoved and writes nothing.

    Args:
        conn: Open connection.

    Returns:
        The id (one ``sqlite_sequence`` row and a search of the integer primary key).
    """
    try:
        row = conn.execute(
            "SELECT MAX("
            "COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'node_deletion_log'), 0), "
            "COALESCE((SELECT MAX(id) FROM node_deletion_log), 0))"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def drop_deletion_log_through_conn(conn: sqlite3.Connection, last_id: int) -> None:
    """Drop the ``node_deletion_log`` rows a refresh consumed (ids up to *last_id*).

    Args:
        conn: Open connection (caller owns the transaction).
        last_id: The mark the refresh just stored.
    """
    conn.execute("DELETE FROM node_deletion_log WHERE id <= ?", (last_id,))


def get_file_records_conn(
    conn: sqlite3.Connection,
    locations: Iterable[str] | None = None,
) -> dict[str, FileRecord]:
    """Return the stored record of each file (all of them when *locations* is ``None``).

    Args:
        conn: Open connection.
        locations: Files to read, or ``None`` for every record.

    Returns:
        Location -> :class:`FileRecord`; empty when the table is absent.
    """
    if not file_state_ready(conn):
        return {}
    out: dict[str, FileRecord] = {}
    if locations is None:
        rows = conn.execute("SELECT location, parsed_fp, hashed_fp, mtime, size FROM file_state").fetchall()
    else:
        locs = list(dict.fromkeys(locations))
        rows = []
        for start in range(0, len(locs), 500):
            chunk = locs[start : start + 500]
            rows.extend(
                conn.execute(
                    "SELECT location, parsed_fp, hashed_fp, mtime, size FROM file_state "
                    f"WHERE location IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
            )
    for r in rows:
        out[r[0]] = FileRecord(parsed_fp=r[1], hashed_fp=r[2], mtime=r[3], size=r[4])
    return out


def record_hashed_files_conn(
    conn: sqlite3.Connection,
    observed: Mapping[str, tuple[str | None, float | None, int | None]],
) -> None:
    """Store the fingerprint a staleness re-hash read for each file, with its stat.

    Args:
        conn: Open connection (caller owns the transaction).
        observed: Location -> ``(fingerprint, mtime, size)``; a ``None``
            fingerprint is stored as ``MISSING_FILE_FP``.
    """
    if not observed:
        return
    conn.executemany(
        "INSERT INTO file_state (location, hashed_fp, mtime, size) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(location) DO UPDATE SET hashed_fp = excluded.hashed_fp, mtime = excluded.mtime, "
        "size = excluded.size",
        [(loc, fp if fp is not None else MISSING_FILE_FP, mtime, size) for loc, (fp, mtime, size) in observed.items()],
    )


def record_file_stats_conn(
    conn: sqlite3.Connection,
    stats: Mapping[str, tuple[float | None, int | None]],
) -> None:
    """Store a file's stat alone, for a file read again whose bytes still match its last re-hash.

    The ``"off"`` read probe compares stats with the record, so a file only
    touched (same bytes, new mtime) stops counting as behind once a pass has
    read it.  Fingerprints are left alone.

    Args:
        conn: Open connection (caller owns the transaction).
        stats: Location -> ``(mtime, size)`` as read now.
    """
    if not stats or not file_state_ready(conn):
        return
    conn.executemany(
        "UPDATE file_state SET mtime = ?, size = ? WHERE location = ?",
        [(mtime, size, loc) for loc, (mtime, size) in stats.items()],
    )


def get_hashed_ids_at_conn(conn: sqlite3.Connection, locations: Iterable[str]) -> dict[str, set[str]]:
    """Return, per file, the ids of the nodes a re-hash of it judges (step views and externals excluded).

    Args:
        conn: Open connection.
        locations: The files.

    Returns:
        Location -> node ids.
    """
    locs = list(dict.fromkeys(locations))
    out: dict[str, set[str]] = {}
    for start in range(0, len(locs), 500):
        chunk = locs[start : start + 500]
        for nid, loc in conn.execute(
            "SELECT id, location FROM nodes WHERE location IN ("
            + ",".join("?" * len(chunk))
            + ") AND COALESCE(subtype, '') NOT IN ('step', 'autostep', 'external_package') AND node_type != 'entity'",
            chunk,
        ):
            out.setdefault(loc, set()).add(nid)
    return out


def record_parsed_files_conn(
    conn: sqlite3.Connection,
    parsed: Mapping[str, tuple[str | None, float | None, int | None]],
) -> None:
    """Store the fingerprint of the content a build parsed, and clear the file's last-hashed one.

    A parse can insert or move nodes, so the next staleness pass must
    re-hash the file before any hash it took earlier vouches for a node.

    Args:
        conn: Open connection (caller owns the transaction).
        parsed: Location -> ``(fingerprint, mtime, size)`` taken before the parse.
    """
    if not parsed:
        return
    conn.executemany(
        "INSERT INTO file_state (location, parsed_fp, hashed_fp, mtime, size) VALUES (?, ?, NULL, ?, ?) "
        "ON CONFLICT(location) DO UPDATE SET parsed_fp = excluded.parsed_fp, hashed_fp = NULL, "
        "mtime = excluded.mtime, size = excluded.size, structure = NULL",
        [(loc, fp, mtime, size) for loc, (fp, mtime, size) in parsed.items()],
    )


def seed_file_records_conn(
    conn: sqlite3.Connection,
    seen: Mapping[str, tuple[str | None, float | None, int | None]],
) -> None:
    """Give a walked file that has no parse record yet its parsed fingerprint (the upgrade seed).

    A record a staleness pass created (no parsed fingerprint) gains one;
    a record that already has one, and every other column, is left alone.

    Args:
        conn: Open connection (caller owns the transaction).
        seen: Location -> ``(fingerprint, mtime, size)``.
    """
    if not seen:
        return
    conn.executemany(
        "INSERT INTO file_state (location, parsed_fp, mtime, size) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(location) DO UPDATE SET parsed_fp = excluded.parsed_fp WHERE file_state.parsed_fp IS NULL",
        [(loc, fp, mtime, size) for loc, (fp, mtime, size) in seen.items()],
    )


def record_file_structure_conn(
    conn: sqlite3.Connection,
    structure: Mapping[str, Mapping[str, list[str]] | None],
) -> None:
    """Store, per re-hashed file, what its parse found that the index lacks (``None`` clears it).

    Args:
        conn: Open connection (caller owns the transaction).
        structure: Location -> ``{"new": [...], "missing": [...]}``, or ``None``.
    """
    import json  # noqa: PLC0415

    rows = [
        (json.dumps({"new": list(s.get("new", [])), "missing": list(s.get("missing", []))}) if s else None, loc)
        for loc, s in structure.items()
    ]
    if rows:
        conn.executemany("UPDATE file_state SET structure = ? WHERE location = ?", rows)


def get_file_structures_conn(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    """Return every file whose last re-hash found structure the index lacks.

    A ``missing`` id with no row in ``nodes`` (purged since that re-hash) is
    left out, since it no longer names an indexed function, and a file left
    with nothing to report is omitted.  The stored record is not rewritten:
    the file's next re-hash replaces it.

    Args:
        conn: Open connection.

    Returns:
        Location -> ``{"new": [...], "missing": [...]}``; empty when the table is absent.
    """
    import json  # noqa: PLC0415

    if not file_state_ready(conn):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for loc, raw in conn.execute("SELECT location, structure FROM file_state WHERE structure IS NOT NULL"):
        try:
            out[loc] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    named = list(dict.fromkeys(nid for entry in out.values() for nid in entry.get("missing") or []))
    if not named:
        return out
    live: set[str] = set()
    for start in range(0, len(named), 500):
        chunk = named[start : start + 500]
        live.update(
            r[0]
            for r in conn.execute(
                f"SELECT id FROM nodes WHERE id IN ({','.join('?' * len(chunk))})",  # noqa: S608
                chunk,
            )
        )
    for loc in list(out):
        entry = out[loc]
        entry["missing"] = [nid for nid in entry.get("missing") or [] if nid in live]
        if not entry["missing"] and not entry.get("new"):
            del out[loc]
    return out


def clear_hashed_fingerprints_conn(conn: sqlite3.Connection, locations: Iterable[str]) -> None:
    """Forget the last-hashed fingerprint of each file, so the next pass re-hashes it.

    Args:
        conn: Open connection (caller owns the transaction).
        locations: Files to clear.
    """
    if not file_state_ready(conn):
        return
    conn.executemany("UPDATE file_state SET hashed_fp = NULL WHERE location = ?", [(loc,) for loc in locations])


def forget_parsed_files_conn(conn: sqlite3.Connection, locations: Iterable[str]) -> None:
    """Forget what the last re-hash read of files a build just parsed: their fingerprint and structure.

    A parse can insert or move nodes, so the next staleness pass re-hashes
    each file before any earlier hash vouches for its nodes, and a
    structural difference the parse just indexed is not reported again.

    Args:
        conn: Open connection (caller owns the transaction).
        locations: The parsed files.
    """
    if not file_state_ready(conn):
        return
    conn.executemany(
        "UPDATE file_state SET hashed_fp = NULL, structure = NULL WHERE location = ?", [(loc,) for loc in locations]
    )


def clear_file_records_conn(conn: sqlite3.Connection) -> int:
    """Drop every stored scan record, so the next build parses every file (baselines kept).

    The per-file records go, and so do the scan mtimes (``nodes.file_mtime``),
    as the v3 / v4 migrations reset them: a file with neither is parsed
    whatever its content, where a file that only lost its record would be
    judged by its scan mtime (the upgrade seed).  The path a release that
    changes what the scanners emit takes.

    Args:
        conn: Open connection (caller owns the transaction).

    Returns:
        The number of records dropped.
    """
    conn.execute("UPDATE nodes SET file_mtime = NULL WHERE file_mtime IS NOT NULL")
    if not file_state_ready(conn):
        return 0
    return conn.execute("DELETE FROM file_state").rowcount


def delete_file_records_conn(conn: sqlite3.Connection, locations: Iterable[str]) -> None:
    """Drop the records of files the index no longer tracks.

    Args:
        conn: Open connection (caller owns the transaction).
        locations: Files to drop.
    """
    if not file_state_ready(conn):
        return
    conn.executemany("DELETE FROM file_state WHERE location = ?", [(loc,) for loc in locations])


def get_index_meta_conn(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the ``index_meta`` value under *key*, or ``None`` (also when the table is absent).

    Args:
        conn: Open connection.
        key: The key.

    Returns:
        The stored value or ``None``.
    """
    try:
        row = conn.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def set_index_meta_conn(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Store *value* under *key* in ``index_meta`` inside the caller's transaction.

    Args:
        conn: Open connection (caller owns the transaction).
        key: The key.
        value: The value.
    """
    conn.execute(
        "INSERT INTO index_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def delete_index_meta_conn(conn: sqlite3.Connection, key: str) -> None:
    """Remove *key* from ``index_meta`` (no-op when absent).

    Args:
        conn: Open connection (caller owns the transaction).
        key: The key.
    """
    try:
        conn.execute("DELETE FROM index_meta WHERE key = ?", (key,))
    except sqlite3.OperationalError:
        return


def max_history_id_conn(conn: sqlite3.Connection) -> int:
    """Return the largest ``node_history.id`` (0 for an empty table).

    Args:
        conn: Open connection.

    Returns:
        The id.
    """
    row = conn.execute("SELECT MAX(id) FROM node_history").fetchone()
    return int(row[0] or 0)


def deleted_node_ids_since_conn(conn: sqlite3.Connection, after_id: int) -> set[str]:
    """Return the nodes a ``DELETED`` history row past *after_id* names (the nodes deleted since).

    Reads only the rows past *after_id* (the rowid range); the ids are
    deduplicated here, because a ``DISTINCT`` makes SQLite walk the whole
    history through its ``node_id`` index instead.

    Args:
        conn: Open connection.
        after_id: A ``node_history.id`` read before the deletions.

    Returns:
        Node ids.
    """
    rows = conn.execute(
        "SELECT node_id FROM node_history WHERE id > ? AND change_type = 'DELETED'",
        (after_id,),
    ).fetchall()
    return {r[0] for r in rows}


def read_journal_conn(conn: sqlite3.Connection, after_id: int) -> tuple[set[str], int]:
    """Return the nodes named by ``node_history`` rows past *after_id*, and the last id read.

    Rows of :data:`INERT_JOURNAL_TYPES` are skipped (their id still counts as
    read).  Only the rows past *after_id* are read (the rowid range): the ids
    are deduplicated here, not with ``DISTINCT``, which would walk the whole
    history through its ``node_id`` index.

    Args:
        conn: Open connection.
        after_id: The watermark.

    Returns:
        ``(node_ids, last_id)``; ``last_id`` is *after_id* when no row is newer.
    """
    last = conn.execute("SELECT MAX(id) FROM node_history WHERE id > ?", (after_id,)).fetchone()[0]
    if last is None:
        return set(), after_id
    placeholders = ",".join("?" * len(INERT_JOURNAL_TYPES))
    rows = conn.execute(
        f"SELECT node_id FROM node_history WHERE id > ? AND id <= ? AND change_type NOT IN ({placeholders})",
        (after_id, last, *INERT_JOURNAL_TYPES),
    ).fetchall()
    return {r[0] for r in rows}, int(last)


def read_journal_for_conn(conn: sqlite3.Connection, after_id: int, node_ids: Iterable[str]) -> set[str]:
    """Return which of *node_ids* have journal rows past *after_id*; the watermark is not moved.

    Args:
        conn: Open connection.
        after_id: The watermark.
        node_ids: The nodes a partial refresh shows or depends on.

    Returns:
        The subset of *node_ids* named by a non-inert row past the watermark.
    """
    ids = list(dict.fromkeys(node_ids))
    out: set[str] = set()
    placeholders = ",".join("?" * len(INERT_JOURNAL_TYPES))
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        rows = conn.execute(
            f"SELECT DISTINCT node_id FROM node_history WHERE node_id IN ({','.join('?' * len(chunk))}) "
            f"AND id > ? AND change_type NOT IN ({placeholders})",
            (*chunk, after_id, *INERT_JOURNAL_TYPES),
        ).fetchall()
        out.update(r[0] for r in rows)
    return out


@dataclass(frozen=True)
class OpenPass:
    """One scope the next discovery refresh re-checks.

    A pass that committed its own-phase write and not yet its last write, or
    (*carried*) the merged seeds of the cones since the last check.

    Attributes:
        token: The entry's key in the list (a carried entry takes a new key
            on every merge, so a refresh that read it removes it only when
            no cone merged into it since).
        full: The scope is every node (its re-check is a full pass).
        ids: The seed nodes (the re-check re-runs their neighbourhood);
            empty when *full*.
        carried: The entry is the cones' merged seeds, not a pass that
            stopped part-way.
        reason: Why a pass that finished left the entry
            (:data:`OPEN_PASS_ENVELOPE_FAILED`); empty for a pass that
            stopped part-way and for the carried entry.
    """

    token: str
    full: bool
    ids: frozenset[str]
    carried: bool = False
    reason: str = ""


def read_open_passes_conn(conn: sqlite3.Connection) -> list[OpenPass]:
    """Return the staleness passes recorded as open (none on a clean index).

    An entry that does not parse reads as one open full pass, so a damaged
    value costs one full pass and never a skipped recovery.

    Args:
        conn: Open connection.

    Returns:
        The open passes, oldest first.
    """
    raw = get_index_meta_conn(conn, STALENESS_OPEN_PASSES_META_KEY)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
        return [
            OpenPass(
                str(e["token"]),
                bool(e["full"]),
                frozenset(e.get("ids") or ()),
                bool(e.get("carried")),
                str(e.get("reason") or ""),
            )
            for e in entries
        ]
    except (ValueError, TypeError, KeyError):
        return [OpenPass("unreadable", True, frozenset())]


def _write_open_passes_conn(conn: sqlite3.Connection, entries: list[OpenPass]) -> None:
    if not entries:
        delete_index_meta_conn(conn, STALENESS_OPEN_PASSES_META_KEY)
        return
    value = json.dumps(
        [
            {
                "token": e.token,
                "full": e.full,
                "ids": sorted(e.ids),
                **({"carried": True} if e.carried else {}),
                **({"reason": e.reason} if e.reason else {}),
            }
            for e in entries
        ]
    )
    set_index_meta_conn(conn, STALENESS_OPEN_PASSES_META_KEY, value)


def open_pass_conn(
    conn: sqlite3.Connection,
    token: str,
    *,
    full: bool,
    ids: Collection[str],
    carried: bool = False,
    reason: str = "",
) -> None:
    """Record a staleness pass as open, in the transaction of its own-phase write.

    Call it after the transaction's first write, so the read-modify-write
    holds the write lock.

    Args:
        conn: Open connection (caller owns the transaction).
        token: The pass's key.
        full: The pass covers every node.
        ids: The nodes the pass was seeded with (ignored when *full*); more
            than :data:`OPEN_PASS_MAX_IDS` records the pass as full.
        carried: The pass is a cone: merge *ids* into the one carried entry,
            which takes *token* as its new key, instead of adding an entry.
            The merged entry turns full past :data:`CARRIED_PASS_MAX_IDS`
            distinct nodes.
        reason: Why a pass that finished leaves the entry open
            (:data:`OPEN_PASS_ENVELOPE_FAILED`); ignored when *carried*.
    """
    entries = read_open_passes_conn(conn)
    if carried:
        prior = [e for e in entries if e.carried]
        entries = [e for e in entries if not e.carried]
        full = full or any(e.full for e in prior)
        merged = frozenset() if full else frozenset(ids).union(*(e.ids for e in prior))
        full = full or len(merged) > CARRIED_PASS_MAX_IDS
        entries.append(OpenPass(token, full, frozenset() if full else merged, True))
    else:
        full = full or len(ids) > OPEN_PASS_MAX_IDS
        entries = [e for e in entries if e.token != token]
        entries.append(OpenPass(token, full, frozenset() if full else frozenset(ids), reason=reason))
    _write_open_passes_conn(conn, entries)


def close_passes_conn(conn: sqlite3.Connection, tokens: Collection[str]) -> None:
    """Remove the named passes from the open list, in a pass's last write transaction.

    Writes nothing when none of *tokens* is recorded.  Call it after the
    transaction's first write, so the read-modify-write holds the write lock.

    Args:
        conn: Open connection (caller owns the transaction).
        tokens: The pass's own key and the keys of the passes it recovered.
    """
    entries = read_open_passes_conn(conn)
    kept = [e for e in entries if e.token not in tokens]
    if len(kept) != len(entries):
        _write_open_passes_conn(conn, kept)


def journal_rows_between_conn(conn: sqlite3.Connection, after_id: int, through_id: int) -> int:
    """Count the non-inert ``node_history`` rows with ``after_id < id <= through_id`` (a rowid range).

    Args:
        conn: Open connection.
        after_id: Lower bound, exclusive.
        through_id: Upper bound, inclusive.

    Returns:
        The count.
    """
    placeholders = ",".join("?" * len(INERT_JOURNAL_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) FROM node_history WHERE id > ? AND id <= ? AND change_type NOT IN ({placeholders})",
        (after_id, through_id, *INERT_JOURNAL_TYPES),
    ).fetchone()
    return int(row[0])


__all__ = [
    "STALENESS_SCHEME_META_KEY",
    "STALENESS_WATERMARK_META_KEY",
    "STALENESS_OPEN_PASSES_META_KEY",
    "OPEN_PASS_MAX_IDS",
    "CARRIED_PASS_MAX_IDS",
    "OPEN_PASS_ENVELOPE_FAILED",
    "OpenPass",
    "read_open_passes_conn",
    "open_pass_conn",
    "close_passes_conn",
    "journal_rows_between_conn",
    "INERT_JOURNAL_TYPES",
    "FileRecord",
    "file_state_ready",
    "ensure_file_state_conn",
    "max_deletion_log_id_conn",
    "drop_deletion_log_through_conn",
    "get_file_records_conn",
    "record_hashed_files_conn",
    "record_file_stats_conn",
    "get_hashed_ids_at_conn",
    "record_parsed_files_conn",
    "seed_file_records_conn",
    "record_file_structure_conn",
    "get_file_structures_conn",
    "clear_hashed_fingerprints_conn",
    "clear_file_records_conn",
    "forget_parsed_files_conn",
    "delete_file_records_conn",
    "get_index_meta_conn",
    "set_index_meta_conn",
    "delete_index_meta_conn",
    "max_history_id_conn",
    "deleted_node_ids_since_conn",
    "read_journal_conn",
    "read_journal_for_conn",
]
