"""Axiom-graph DB: edge CRUD + ID migration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

import json
import sqlite3
from pathlib import Path

from axiom_annotations import task

from axiom_graph.models import AxiomEdge

from axiom_graph.db._core import (
    _connect,
    _edge_to_row,
    _now_utc,
    _row_to_edge,
)


def upsert_edge(db_path: Path, edge: AxiomEdge) -> bool:
    """Insert or replace an edge. Returns True if a write occurred.

    Thin wrapper around :func:`upsert_edge_conn` that opens its own connection.
    """
    with _connect(db_path) as conn:
        return upsert_edge_conn(conn, edge)


@task(
    purpose="Insert or replace an edge row; returns True if the edge is new, False if it replaced an existing one",
    inputs="conn (open SQLite connection), AxiomEdge",
    outputs="True if new write, False if replaced existing",
)
def upsert_edge_conn(conn: sqlite3.Connection, edge: AxiomEdge) -> bool:
    """Insert or replace an edge using an existing connection."""
    row = _edge_to_row(edge)
    existing = conn.execute("SELECT id FROM edges WHERE id = ?", (edge.id,)).fetchone()
    conn.execute(
        """
        INSERT OR REPLACE INTO edges (id, edge_type, from_id, to_id, weight, meta)
        VALUES (:id, :edge_type, :from_id, :to_id, :weight, :meta)
        """,
        row,
    )
    return existing is None  # True = new write, False = replaced existing


def upsert_edges_conn(conn: sqlite3.Connection, edges: Iterable[AxiomEdge]) -> int:
    """Insert or replace *edges* in order, reading which ids exist once per chunk of ids.

    The rows written are those of :func:`upsert_edge_conn` called on each
    edge in turn; only the existence reads are batched.

    Args:
        conn: Open connection.
        edges: The edges to write.

    Returns:
        How many distinct edge ids the index did not hold before the call.
    """
    rows = [_edge_to_row(edge) for edge in edges]
    if not rows:
        return 0
    ids = list(dict.fromkeys(row["id"] for row in rows))
    existing: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        existing.update(
            r[0] for r in conn.execute(f"SELECT id FROM edges WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO edges (id, edge_type, from_id, to_id, weight, meta)
        VALUES (:id, :edge_type, :from_id, :to_id, :weight, :meta)
        """,
        rows,
    )
    return len(ids) - len(existing)


def query_edges(
    db_path: Path,
    node_id: str,
    direction: str = "out",
    depth: int = 1,
) -> list[AxiomEdge]:
    """Return edges connected to node_id up to `depth` hops.

    direction="out"  → edges where from_id == node_id (or reachable from it)
    direction="in"   → edges where to_id == node_id (or reaching it)
    direction="both" → either direction
    """
    with _connect(db_path) as conn:
        visited_nodes: set[str] = {node_id}
        frontier: set[str] = {node_id}
        collected: list[AxiomEdge] = []

        for _ in range(depth):
            if not frontier:
                break
            placeholders = ",".join("?" * len(frontier))
            frontier_list = list(frontier)
            next_frontier: set[str] = set()

            if direction in ("out", "both"):
                rows = conn.execute(
                    f"SELECT * FROM edges WHERE from_id IN ({placeholders})",
                    frontier_list,
                ).fetchall()
                for r in rows:
                    e = _row_to_edge(r)
                    collected.append(e)
                    if e.to_id not in visited_nodes:
                        next_frontier.add(e.to_id)
                        visited_nodes.add(e.to_id)

            if direction in ("in", "both"):
                rows = conn.execute(
                    f"SELECT * FROM edges WHERE to_id IN ({placeholders})",
                    frontier_list,
                ).fetchall()
                for r in rows:
                    e = _row_to_edge(r)
                    collected.append(e)
                    if e.from_id not in visited_nodes:
                        next_frontier.add(e.from_id)
                        visited_nodes.add(e.from_id)

            frontier = next_frontier

        # deduplicate by edge id
        seen: set[str] = set()
        result: list[AxiomEdge] = []
        for e in collected:
            if e.id not in seen:
                seen.add(e.id)
                result.append(e)
        return result


def all_edges(db_path: Path) -> list[AxiomEdge]:
    """Return every edge in the DB."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM edges").fetchall()
        return [_row_to_edge(r) for r in rows]


def get_outbound_edge_targets_conn(
    conn: sqlite3.Connection,
    from_id: str,
    edge_type: str,
) -> set[str]:
    """Return the set of ``to_id`` for outbound edges of one type from ``from_id``.

    Scoped strictly to the requested ``edge_type`` — edges of any other type
    are not returned.  Used by the build-time reconciliation passes to diff a
    source's stored edge set against the set its file justifies today.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        from_id: Source node ID.
        edge_type: The single edge type to read (e.g. ``"documents"``,
            ``"delegates_to"``).

    Returns:
        Set of target node IDs.  Empty set when no matching edges exist.
    """
    rows = conn.execute(
        "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = ?",
        (from_id, edge_type),
    ).fetchall()
    return {r["to_id"] for r in rows}


def get_edges_from_conn(
    conn: sqlite3.Connection,
    edge_type: str,
    from_ids: Iterable[str],
) -> list[tuple[str, str, str | None]]:
    """Return the ``(from_id, to_id, meta)`` rows of one edge type leaving the given nodes.

    Batched lookups through the ``from_id`` index, so the cost follows the
    nodes asked about, not the number of edges of the type.

    Args:
        conn: Open SQLite connection.
        edge_type: The single edge type to read.
        from_ids: Source node ids.

    Returns:
        The rows, *meta* as its stored JSON text (``None`` when absent).
    """
    ids = sorted(set(from_ids))
    out: list[tuple[str, str, str | None]] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        out.extend(
            (r["from_id"], r["to_id"], r["meta"])
            for r in conn.execute(
                "SELECT from_id, to_id, meta FROM edges WHERE edge_type = ? "
                f"AND from_id IN ({','.join('?' * len(chunk))})",  # noqa: S608 - placeholders only
                (edge_type, *chunk),
            )
        )
    return out


def iter_edges_of_type_conn(conn: sqlite3.Connection, edge_type: str) -> Iterator[tuple[str, str, str | None]]:
    """Yield the ``(from_id, to_id, meta)`` rows of one edge type, in ``(from_id, to_id)`` order.

    The rows come off the ``(edge_type, from_id, to_id)`` index with no
    sort, one step of the cursor per row, so a caller that stops early has
    read only the rows it consumed, not every edge of the type.  Close the
    generator, or let it go, to release the read.

    Args:
        conn: Open SQLite connection.
        edge_type: The single edge type to read.

    Yields:
        One row per edge, *meta* as its stored JSON text (``None`` when absent).
    """
    cursor = conn.execute(
        "SELECT from_id, to_id, meta FROM edges WHERE edge_type = ? ORDER BY from_id, to_id",
        (edge_type,),
    )
    try:
        for row in cursor:
            yield row[0], row[1], row[2]
    finally:
        cursor.close()


def get_edge_source_ids_conn(
    conn: sqlite3.Connection,
    edge_type: str,
    among: Iterable[str] | None = None,
) -> set[str]:
    """Return every node ID that has at least one outbound edge of ``edge_type``.

    One bulk read that lets a reconciliation pass narrow its candidate
    sources to those that actually hold a stored edge of the type, instead of
    querying once per node the build walked.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        edge_type: The single edge type to read.
        among: Only these sources, looked up in batches through the
            ``from_id`` index; ``None`` for every source.

    Returns:
        Set of source node IDs.  Empty set when no such edges exist.
    """
    if among is None:
        rows = conn.execute(
            "SELECT DISTINCT from_id FROM edges WHERE edge_type = ?",
            (edge_type,),
        ).fetchall()
        return {r["from_id"] for r in rows}
    ids = sorted(set(among))
    out: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        out.update(
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT from_id FROM edges WHERE from_id IN ({','.join('?' * len(chunk))}) "  # noqa: S608
                "AND edge_type = ?",
                (*chunk, edge_type),
            )
        )
    return out


def delete_edge_conn(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    edge_type: str,
    actor: str = "build:reconcile",
) -> bool:
    """Delete one specific outbound edge and emit LINK_REMOVED history.

    Co-locates the DELETE and the history-row emit so they share one
    transaction — callers always get atomic semantics.  Only the edge of the
    requested ``edge_type`` between the two nodes is removed; edges of other
    types between the same pair are untouched by construction.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        from_id: Source node ID of the edge to delete.
        to_id: Target node ID of the edge to delete.
        edge_type: Type of the edge to delete (e.g. ``"documents"``,
            ``"delegates_to"``).
        actor: Value written into the history meta's ``actor`` field.
            Defaults to ``"build:reconcile"`` for the build-time path;
            other callers (tool path) pass ``"agent"``.

    Returns:
        True if an edge row was deleted, False if no matching edge existed.
    """
    edge_id = f"{from_id}::{edge_type}::{to_id}"
    cursor = conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
    if cursor.rowcount == 0:
        return False

    conn.execute(
        """
        INSERT INTO node_history (node_id, scanned_at, change_type, git_sha, meta, preserved)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            from_id,
            _now_utc(),
            "LINK_REMOVED",
            None,
            json.dumps(
                {
                    "edge_type": edge_type,
                    "source": from_id,
                    "target": to_id,
                    "actor": actor,
                }
            ),
            0,
        ),
    )
    return True


def get_outbound_documents_targets_conn(
    conn: sqlite3.Connection,
    from_id: str,
) -> set[str]:
    """Return the set of ``to_id`` for outbound ``documents`` edges from ``from_id``.

    Named entry point for the documents path — a thin wrapper over
    :func:`get_outbound_edge_targets_conn` pinned to ``documents``.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        from_id: Source node ID (typically a doc section node).

    Returns:
        Set of target node IDs.  Empty set when no matching edges exist.
    """
    return get_outbound_edge_targets_conn(conn, from_id, "documents")


def delete_documents_edge_conn(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    actor: str = "build:reconcile",
) -> bool:
    """Delete a specific outbound ``documents`` edge and emit LINK_REMOVED history.

    Named entry point for the documents path — a thin wrapper over
    :func:`delete_edge_conn` pinned to ``documents``, so a documents edge can
    never be deleted with another type's history row.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        from_id: Source node ID of the edge to delete.
        to_id: Target node ID of the edge to delete.
        actor: Value written into the history meta's ``actor`` field.
            Defaults to ``"build:reconcile"`` for the build-time path;
            other callers (tool path) pass ``"agent"``.

    Returns:
        True if an edge row was deleted, False if no matching edge existed.
    """
    return delete_edge_conn(conn, from_id, to_id, "documents", actor=actor)


def _migrate_edges(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Migrate all edge references from old_id to new_id within a transaction.

    Updates both from_id and to_id columns, and regenerates the edge ID
    to reflect the new node ID.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        old_id: The old node ID to replace.
        new_id: The new node ID to replace with.
    """
    # Update edges where old_id is the target (to_id)
    rows = conn.execute(
        "SELECT id, edge_type, from_id, to_id, weight, meta FROM edges WHERE to_id = ?",
        (old_id,),
    ).fetchall()
    for r in rows:
        new_edge_id = f"{r['from_id']}::{r['edge_type']}::{new_id}"
        conn.execute("DELETE FROM edges WHERE id = ?", (r["id"],))
        conn.execute(
            "INSERT OR REPLACE INTO edges (id, edge_type, from_id, to_id, weight, meta) VALUES (?, ?, ?, ?, ?, ?)",
            (new_edge_id, r["edge_type"], r["from_id"], new_id, r["weight"], r["meta"]),
        )

    # Update edges where old_id is the source (from_id)
    rows = conn.execute(
        "SELECT id, edge_type, from_id, to_id, weight, meta FROM edges WHERE from_id = ?",
        (old_id,),
    ).fetchall()
    for r in rows:
        new_edge_id = f"{new_id}::{r['edge_type']}::{r['to_id']}"
        conn.execute("DELETE FROM edges WHERE id = ?", (r["id"],))
        conn.execute(
            "INSERT OR REPLACE INTO edges (id, edge_type, from_id, to_id, weight, meta) VALUES (?, ?, ?, ?, ?, ?)",
            (new_edge_id, r["edge_type"], new_id, r["to_id"], r["weight"], r["meta"]),
        )


def edge_sources_into_conn(
    conn: sqlite3.Connection,
    target_ids: Iterable[str],
    edge_types: Iterable[str],
) -> set[str]:
    """Return the sources of the edges of *edge_types* into *target_ids* (indexed by ``to_id``).

    Args:
        conn: Open connection.
        target_ids: Edge targets; ids with no node row are fine.
        edge_types: The edge types to follow.

    Returns:
        Source node ids.
    """
    ids = sorted(set(target_ids))
    types = list(edge_types)
    out: set[str] = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        out.update(
            r[0]
            for r in conn.execute(
                f"SELECT from_id FROM edges WHERE to_id IN ({','.join('?' * len(chunk))}) "
                f"AND edge_type IN ({','.join('?' * len(types))})",
                (*chunk, *types),
            )
        )
    return out


__all__ = [
    "edge_sources_into_conn",
    "get_edges_from_conn",
    "iter_edges_of_type_conn",
    "upsert_edge",
    "upsert_edge_conn",
    "upsert_edges_conn",
    "query_edges",
    "all_edges",
    "get_outbound_edge_targets_conn",
    "get_edge_source_ids_conn",
    "delete_edge_conn",
    "get_outbound_documents_targets_conn",
    "delete_documents_edge_conn",
]
