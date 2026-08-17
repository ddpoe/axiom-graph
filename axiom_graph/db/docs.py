"""Axiom-graph DB: doc metadata, doc-section node reads, renames, FTS search.

Per ADR-021 the DocJSON storage model is envelope-pattern nodes: every
section is a first-class ``nodes`` row (``subtype='docjson_section'``,
heading in ``level_1``, full content in ``level_2``, render order /
heading level in ``doc_position`` / ``doc_level``) and the DocJSON file
is the lone composite envelope (``subtype='docjson_doc'``).  There is no
``doc_sections`` table and no shadow-row sync machinery.

This module covers the thin ``docs`` metadata table (``upsert_doc``,
``list_docs``, ``get_doc_ids_by_filepath``, ``get_all_doc_file_paths``),
node-backed section reads that preserve the legacy row-dict shape
(``get_doc_sections``, ``list_all_doc_sections``, ``get_long_sections``,
``query_doc_sections_by_tags``, ``get_section_doc_id_map``,
``get_tagged_doc_doc_edges``), doc lifecycle (``delete_doc_by_id``,
``record_doc_rename``, ``record_code_rename``, ``move_doc``), and the
FTS5 node_fts search (``fts_search``, ``index_doc_sections_fts``,
``list_tags``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
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

    口 = Step(
        step_num=3,
        name="Delete doc metadata record",
        purpose="Remove the docs table entry (sections are nodes rows, already deleted above)",
    )
    conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))


# ---------------------------------------------------------------------------
# Renames (doc + code)
# ---------------------------------------------------------------------------


def _doc_section_suffixes(conn: sqlite3.Connection, old_id: str) -> list[str]:
    """Return every section suffix currently attached to *old_id*.

    A section's identity can survive in more tables than one: an unverified
    section has ``nodes`` and ``edges`` rows, a purged-then-re-added one may
    have only history, and a verified one carries a ``node_verification``
    row.  A rename that enumerates from a single table silently strands the
    rest, so the union of all four is taken.

    Args:
        conn: Open SQLite connection.
        old_id: The document envelope ID being renamed from.

    Returns:
        Section suffixes (the part after ``{old_id}::``), sorted.
    """
    prefix = old_id + "::"
    like = prefix + "%"
    suffixes: set[str] = set()
    queries = (
        ("SELECT id AS nid FROM nodes WHERE id LIKE ?", (like,)),
        ("SELECT DISTINCT node_id AS nid FROM node_history WHERE node_id LIKE ?", (like,)),
        ("SELECT node_id AS nid FROM node_verification WHERE node_id LIKE ?", (like,)),
        ("SELECT DISTINCT from_id AS nid FROM edges WHERE from_id LIKE ?", (like,)),
        ("SELECT DISTINCT to_id AS nid FROM edges WHERE to_id LIKE ?", (like,)),
    )
    for sql, params in queries:
        for row in conn.execute(sql, params).fetchall():
            nid = row["nid"]
            if isinstance(nid, str) and nid.startswith(prefix):
                suffixes.add(nid[len(prefix) :])
    return sorted(suffixes)


@task(
    purpose="Migrate a doc's ledger, history, verification, and edges from an old id to a new one, covering sections as well as the envelope",
    inputs="open connection, old_id, new_id, file_path",
    outputs="None (side effect: rename ledger written, history/verification/edges moved)",
)
def record_doc_rename_conn(
    conn: sqlite3.Connection,
    old_id: str,
    new_id: str,
    file_path: str,
) -> None:
    """Migrate one document's identity on an already-open connection.

    Takes a connection so a bulk rename can run as one transaction: a
    failure partway through then rolls the whole batch back instead of
    leaving a half-migrated index.

    Section verification is migrated alongside the envelope's.  Sections are
    the overwhelming majority of a document's verified nodes, and
    ``node_verification.node_id`` is a foreign key onto ``nodes(id)`` with
    ``PRAGMA foreign_keys=ON`` -- so the new ``nodes`` rows must already
    exist (see :func:`rekey_doc_identity`) or the update is silently
    dropped by ``OR IGNORE``.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        old_id: The old doc node ID being renamed from.
        new_id: The new doc node ID being renamed to.
        file_path: File path recorded in the ``node_renames`` ledger.
    """
    now = _now_utc()
    口 = Step(
        step_num=1,
        name="Record the envelope rename and migrate its rows",
        purpose="Write the ledger entry, then move the envelope's history, verification, and edges",
    )
    conn.execute(
        "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
        (old_id, new_id, now, file_path),
    )
    conn.execute("UPDATE node_history SET node_id = ? WHERE node_id = ?", (new_id, old_id))
    conn.execute(
        "UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?",
        (new_id, old_id),
    )
    _migrate_edges(conn, old_id, new_id)

    口 = Step(
        step_num=2,
        name="Migrate every section identity",
        purpose="Move each section's ledger entry, history, verification, and edges to the new envelope",
    )
    for suffix in _doc_section_suffixes(conn, old_id):
        old_sec_id = old_id + "::" + suffix
        new_sec_id = new_id + "::" + suffix
        口 = Step(
            step_num=2.1,
            name="Migrate one section",
            purpose="Move a single section's ledger entry, history, verification, and edges",
        )
        conn.execute(
            "INSERT OR IGNORE INTO node_renames (old_id, new_id, renamed_at, file_path) VALUES (?, ?, ?, ?)",
            (old_sec_id, new_sec_id, now, file_path),
        )
        conn.execute("UPDATE node_history SET node_id = ? WHERE node_id = ?", (new_sec_id, old_sec_id))
        conn.execute(
            "UPDATE OR IGNORE node_verification SET node_id = ? WHERE node_id = ?",
            (new_sec_id, old_sec_id),
        )
        _migrate_edges(conn, old_sec_id, new_sec_id)


@dataclass(frozen=True)
class RekeyCounts:
    """Node rows materialised under a new document identity.

    The two are counted apart rather than summed because a document's
    envelope row and its section rows can go missing independently: an
    index holding section rows but no envelope row would make
    ``sections = total - 1`` undercount by one.

    Attributes:
        envelope: Whether the document envelope's ``nodes`` row was cloned.
        sections: Number of section ``nodes`` rows cloned.
    """

    envelope: bool = False
    sections: int = 0


@task(
    purpose="Materialise a document's node, doc, tag, and FTS rows under a new id so the new identity exists before dependent rows move onto it",
    inputs="open connection, old_id, new_id, optional new file_path",
    outputs="RekeyCounts — whether the envelope row was cloned, and how many section rows were",
)
def rekey_doc_identity(
    conn: sqlite3.Connection,
    old_id: str,
    new_id: str,
    new_file_path: str | None = None,
) -> RekeyCounts:
    """Clone a document's identity rows onto *new_id*, leaving the old ones.

    ``node_verification.node_id`` is a foreign key onto ``nodes(id)``, so a
    rename cannot move verification onto an identity that does not exist
    yet.  Materialising the new rows first is what makes verification --
    and every other dependent row -- survive the move.  The old rows are
    left in place for the caller to retire.

    Args:
        conn: Open SQLite connection (caller owns the transaction).
        old_id: The document envelope ID being renamed from.
        new_id: The document envelope ID being renamed to.
        new_file_path: File path for the new ``docs`` row.  Defaults to the
            old row's path -- a doc-ID migration moves the identity, not the
            file.

    Returns:
        A :class:`RekeyCounts` describing what was actually cloned.
    """
    pairs = [(old_id, new_id, False)]
    pairs.extend((old_id + "::" + s, new_id + "::" + s, True) for s in _doc_section_suffixes(conn, old_id))

    envelope_cloned = False
    sections_cloned = 0
    for old_node_id, new_node_id, is_section in pairs:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (old_node_id,)).fetchone()
        if row is None:
            continue
        columns = list(row.keys())
        values = [new_node_id if col == "id" else row[col] for col in columns]
        placeholders = ",".join("?" * len(columns))
        conn.execute(
            f"INSERT OR REPLACE INTO nodes ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
        if is_section:
            sections_cloned += 1
        else:
            envelope_cloned = True
        conn.execute(
            "INSERT OR IGNORE INTO tags (node_id, tag) SELECT ?, tag FROM tags WHERE node_id = ?",
            (new_node_id, old_node_id),
        )
        conn.execute("DELETE FROM node_fts WHERE id = ?", (new_node_id,))
        conn.execute(
            "INSERT INTO node_fts (id, level_1, level_2) SELECT ?, level_1, level_2 FROM node_fts WHERE id = ?",
            (new_node_id, old_node_id),
        )

    doc_row = conn.execute("SELECT * FROM docs WHERE id = ?", (old_id,)).fetchone()
    if doc_row is not None:
        columns = list(doc_row.keys())
        values = []
        for col in columns:
            if col == "id":
                values.append(new_id)
            elif col == "file_path" and new_file_path is not None:
                values.append(new_file_path)
            else:
                values.append(doc_row[col])
        placeholders = ",".join("?" * len(columns))
        conn.execute(
            f"INSERT OR REPLACE INTO docs ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )
    return RekeyCounts(envelope=envelope_cloned, sections=sections_cloned)


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
            to update link references from old_id to new_id, including
            references to its sections.
    """
    with _connect(db_path) as conn:
        record_doc_rename_conn(conn, old_id, new_id, file_path)

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


# Subtype / source families for DocJSON section nodes.  ``docjson`` is the
# pre-ADR-021 legacy subtype (kept in read filters so tools stay usable on a
# not-yet-migrated DB); ``docjson_section`` is the current one.  Source
# scoping keeps markdown doc nodes (source='doc_scanner', legacy subtype
# 'docjson') out of DocJSON-section queries.
_SECTION_SUBTYPES = ("docjson", "docjson_section")
_DOCJSON_SOURCES = ("docjson", "json_doc_scanner")


def _section_filter_sql(alias: str = "") -> str:
    """SQL predicate selecting DocJSON section node rows.

    Args:
        alias: Optional table alias prefix (e.g. ``"n."``).
    """
    return (
        f"{alias}node_type = 'atomic_process' "
        f"AND {alias}subtype IN ('docjson', 'docjson_section') "
        f"AND {alias}source IN ('docjson', 'json_doc_scanner')"
    )


_SECTION_FILTER_SQL = _section_filter_sql()


def split_section_id(section_id: str) -> tuple[str, str]:
    """Split a full section ID into ``(doc_id, dot_path)``.

    Section IDs are ``{doc_id}::{dot_path}`` where ``doc_id`` itself
    contains exactly one ``::`` (``proj::docs.x``) and the dot-path never
    does — so the split is the last ``::``.
    """
    doc_id, _, dot_path = section_id.rpartition("::")
    return doc_id, dot_path


def _like_escape(text: str) -> str:
    r"""Escape LIKE wildcards so *text* matches literally (ESCAPE '\\')."""
    return text.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _section_tags_json(conn: sqlite3.Connection, section_ids: list[str]) -> dict[str, str]:
    """Return ``{section_id: json_tags_string}`` for sections that have tags."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(section_ids), 500):
        chunk = section_ids[i : i + 500]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT node_id, tag FROM tags WHERE node_id IN ({ph}) ORDER BY tag",
            chunk,
        ).fetchall():
            out.setdefault(r["node_id"], []).append(r["tag"])
    return {sid: json.dumps(tags) for sid, tags in out.items()}


def _section_node_to_dict(row: sqlite3.Row | dict, tags_json: str | None) -> dict:
    """Map a section node row to the legacy ``doc_sections`` dict shape.

    Keys: ``id``, ``doc_id``, ``heading``, ``level``, ``tags`` (JSON string
    or None), ``content``, ``desc_hash``, ``parent_id``, ``depth``,
    ``position``, ``updated_at``.  ``doc_id`` / ``parent_id`` / ``depth``
    are derived from the ID's dot-path; ``position`` / ``level`` come from
    the denormalized ``doc_position`` / ``doc_level`` columns.
    """
    d = dict(row)
    sec_id = d["id"]
    doc_id, dot_path = split_section_id(sec_id)
    if "." in dot_path:
        parent_id: str | None = f"{doc_id}::{dot_path.rsplit('.', 1)[0]}"
    else:
        parent_id = None
    return {
        "id": sec_id,
        "doc_id": doc_id,
        "heading": d.get("level_1") or "",
        "level": d.get("doc_level") if d.get("doc_level") is not None else 2,
        "tags": tags_json,
        "content": d.get("level_2") or "",
        "desc_hash": d.get("desc_hash"),
        "parent_id": parent_id,
        "depth": dot_path.count("."),
        "position": d.get("doc_position") if d.get("doc_position") is not None else 0,
        "updated_at": d.get("updated_at"),
    }


def _depth_first(sections: list[dict]) -> list[dict]:
    """Order section dicts in depth-first document order.

    Siblings sort by ``position`` (then id for stability); each parent is
    immediately followed by its subtree.  Sections whose parent is missing
    from the set are treated as roots (defensive).
    """
    by_parent: dict[str | None, list[dict]] = {}
    ids = {s["id"] for s in sections}
    for s in sections:
        parent = s["parent_id"] if s["parent_id"] in ids else None
        by_parent.setdefault(parent, []).append(s)
    for siblings in by_parent.values():
        siblings.sort(key=lambda s: (s["position"], s["id"]))

    out: list[dict] = []

    def _walk(parent: str | None) -> None:
        for sec in by_parent.get(parent, []):
            out.append(sec)
            _walk(sec["id"])

    _walk(None)
    return out


def _query_section_dicts(conn: sqlite3.Connection, where: str, params: list) -> list[dict]:
    """Fetch section nodes matching *where*, mapped to legacy dict shape."""
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE {_SECTION_FILTER_SQL} AND {where}",
        params,
    ).fetchall()
    tag_map = _section_tags_json(conn, [r["id"] for r in rows])
    return [_section_node_to_dict(r, tag_map.get(r["id"])) for r in rows]


def get_doc_sections(db_path: Path, doc_id: str) -> list[dict]:
    """Return section dicts for a doc, in depth-first document order.

    Sections are ``nodes`` rows (``subtype='docjson_section'``); the
    returned dicts preserve the legacy ``doc_sections`` row shape.
    """
    with _connect(db_path) as conn:
        pattern = _like_escape(doc_id) + "::%"
        secs = _query_section_dicts(conn, r"id LIKE ? ESCAPE '\'", [pattern])
    # LIKE with escaped wildcards is exact, but keep a defensive prefix check.
    secs = [s for s in secs if s["doc_id"] == doc_id]
    return _depth_first(secs)


def list_docs(db_path: Path) -> list[dict]:
    """Return all rows from the docs table."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM docs ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def list_all_doc_sections(db_path: Path) -> list[dict]:
    """Return all section dicts ordered by doc_id, then depth-first."""
    with _connect(db_path) as conn:
        secs = _query_section_dicts(conn, "1=1", [])
    by_doc: dict[str, list[dict]] = {}
    for s in secs:
        by_doc.setdefault(s["doc_id"], []).append(s)
    out: list[dict] = []
    for doc_id in sorted(by_doc):
        out.extend(_depth_first(by_doc[doc_id]))
    return out


# DOC_SECTION_LONG content-length threshold (single source of truth).
# Consumed by :func:`get_long_sections` AND the ``DOC_SECTION_LONG``
# arm of :func:`axiom_graph.db.staleness.query_drift_rows` — keeping
# this in one place means a threshold change propagates atomically.
DOC_SECTION_LONG_THRESHOLD = 2000


def get_long_sections(db_path: Path, threshold: int = DOC_SECTION_LONG_THRESHOLD) -> list[dict]:
    """Return doc sections whose content exceeds *threshold* chars, longest first.

    Section content lives in ``nodes.level_2`` (full, untruncated) — the
    same column the ``DOC_SECTION_LONG`` arm of ``query_drift_rows``
    filters on, so ``check`` and ``drift_query`` agree by construction.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT id, level_1 AS heading, LENGTH(level_2) AS chars
            FROM nodes
            WHERE {_SECTION_FILTER_SQL}
              AND LENGTH(level_2) > ?
            ORDER BY LENGTH(level_2) DESC
            """,
            (threshold,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        doc_id, _ = split_section_id(r["id"])
        out.append({"id": r["id"], "doc_id": doc_id, "heading": r["heading"], "chars": r["chars"]})
    return out


def query_doc_sections_by_tags(
    db_path: Path,
    tags: list[str],
    *,
    match_all: bool = False,
) -> list[dict]:
    """Return doc sections whose section-level tags overlap with *tags*.

    When *match_all* is ``True``, only sections tagged with **every**
    requested tag are returned.  Otherwise any overlap is sufficient.

    Each returned dict has the legacy ``doc_sections`` row shape plus
    ``doc_title`` (from the parent ``docs`` row).
    """
    if not tags:
        return []
    with _connect(db_path) as conn:
        placeholders = ",".join("?" * len(tags))
        threshold = len(tags) if match_all else 1
        rows = conn.execute(
            f"""
            SELECT n.*
            FROM nodes n
            JOIN tags t ON t.node_id = n.id
            WHERE {_section_filter_sql("n.")}
              AND t.tag IN ({placeholders})
            GROUP BY n.id
            HAVING COUNT(DISTINCT t.tag) >= ?
            """,
            [*tags, threshold],
        ).fetchall()
        tag_map = _section_tags_json(conn, [r["id"] for r in rows])
        doc_titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM docs").fetchall()}
    out: list[dict] = []
    for r in rows:
        sec = _section_node_to_dict(r, tag_map.get(r["id"]))
        sec["doc_title"] = doc_titles.get(sec["doc_id"], "")
        out.append(sec)
    out.sort(key=lambda s: (s["doc_id"], s["position"], s["id"]))
    return out


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
            f"""
            SELECT
                e.from_id AS source_section_id,
                e.to_id   AS target_section_id
            FROM edges e
            JOIN nodes s_src ON s_src.id = e.from_id AND {_section_filter_sql("s_src.")}
            JOIN nodes s_tgt ON s_tgt.id = e.to_id AND {_section_filter_sql("s_tgt.")}
            WHERE e.edge_type = 'documents'
            """
        ).fetchall()
        doc_tag_rows = conn.execute("SELECT id, tags FROM docs WHERE tags IS NOT NULL").fetchall()

    # Filter in Python: docs.tags is a JSON array string; check overlap with
    # the requested tags set.  The tag check is at the document level — the
    # source section's owning doc (derived from the section ID) must carry
    # at least one matching tag.
    tag_set = set(tags)
    tagged_doc_ids: set[str] = set()
    for r in doc_tag_rows:
        try:
            doc_tags = json.loads(r["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            doc_tags = []
        if tag_set & set(doc_tags):
            tagged_doc_ids.add(r["id"])

    result: list[dict] = []
    for r in rows:
        src_doc_id, _ = split_section_id(r["source_section_id"])
        if src_doc_id in tagged_doc_ids:
            result.append(
                {
                    "source_section_id": r["source_section_id"],
                    "target_section_id": r["target_section_id"],
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
        rows = conn.execute(f"SELECT id FROM nodes WHERE {_SECTION_FILTER_SQL}").fetchall()

    out: dict[str, str] = {}
    for r in rows:
        doc_id, _ = split_section_id(r["id"])
        if doc_ids is None or doc_id in doc_ids:
            out[r["id"]] = doc_id
    return out


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
    """Re-sync node_fts entries for every DocJSON section node.

    Sections are first-class ``nodes`` rows, so this is a pure FTS refresh
    from the canonical ``level_1`` / ``level_2`` columns.  It never creates
    node rows — a purged section therefore stays purged (the legacy
    resurrection branch that re-inserted nodes from orphaned
    ``doc_sections`` rows is retired with that table).

    The refresh is deliberately issued as one bulk ``DELETE`` plus one
    ``executemany`` rather than a statement pair per section.  ``node_fts`` is
    an FTS5 virtual table, and FTS5 cannot carry a secondary index on a
    column, so ``DELETE FROM node_fts WHERE id = ?`` has no index to use and
    degrades to a full scan of the FTS table.  Issuing one such delete per
    section makes the pass quadratic in the number of sections and turns it
    into the dominant cost of a build; the bulk form scans once.

    Args:
        db_path: Path to the axiom-graph DB file.

    Returns:
        Number of doc sections indexed.
    """
    with _connect(db_path) as conn:
        sections = conn.execute(f"SELECT id, level_1, level_2 FROM nodes WHERE {_SECTION_FILTER_SQL}").fetchall()
        # Subquery rather than an ``IN (?, ?, ...)`` parameter list so the
        # statement stays within SQLITE_MAX_VARIABLE_NUMBER at any doc count.
        conn.execute(f"DELETE FROM node_fts WHERE id IN (SELECT id FROM nodes WHERE {_SECTION_FILTER_SQL})")
        conn.executemany(
            "INSERT INTO node_fts (id, level_1, level_2) VALUES (?, ?, ?)",
            [(sec["id"], sec["level_1"] or "", sec["level_2"] or "") for sec in sections],
        )
    return len(sections)


__all__ = [
    # Doc ID helpers
    "get_doc_ids_by_filepath",
    # Doc delete
    "delete_doc_by_id",
    # Renames
    "record_doc_rename",
    "record_doc_rename_conn",
    "rekey_doc_identity",
    "RekeyCounts",
    "record_code_rename",
    # Upserts + reads
    "upsert_doc",
    "get_doc_sections",
    "list_docs",
    "list_all_doc_sections",
    "get_long_sections",
    "DOC_SECTION_LONG_THRESHOLD",
    "split_section_id",
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
