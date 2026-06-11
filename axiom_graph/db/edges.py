"""Axiom-graph DB: edge CRUD + ID migration helpers."""

from __future__ import annotations

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


def get_outbound_documents_targets_conn(
    conn: sqlite3.Connection,
    from_id: str,
) -> set[str]:
    """Return the set of ``to_id`` for outbound ``documents`` edges from ``from_id``.

    Scoped strictly to ``edge_type='documents'`` — other edge types are not
    returned.  Used by the build-time reconciliation pass to detect orphan
    documents edges whose targets are no longer in a section's JSON ``links``.

    Args:
        conn: Open SQLite connection (caller manages transaction).
        from_id: Source node ID (typically a doc section node).

    Returns:
        Set of target node IDs.  Empty set when no matching edges exist.
    """
    rows = conn.execute(
        "SELECT to_id FROM edges WHERE from_id = ? AND edge_type = 'documents'",
        (from_id,),
    ).fetchall()
    return {r["to_id"] for r in rows}


def delete_documents_edge_conn(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    actor: str = "build:reconcile",
) -> bool:
    """Delete a specific outbound ``documents`` edge and emit LINK_REMOVED history.

    Co-locates the DELETE and the history-row emit so they share one
    transaction — callers always get atomic semantics.  Restricted to
    ``edge_type='documents'`` by construction (composes/validates etc.
    cannot be deleted via this primitive).

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
    edge_id = f"{from_id}::documents::{to_id}"
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
                    "edge_type": "documents",
                    "source": from_id,
                    "target": to_id,
                    "actor": actor,
                }
            ),
            0,
        ),
    )
    return True


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


__all__ = [
    "upsert_edge",
    "upsert_edge_conn",
    "query_edges",
    "all_edges",
    "get_outbound_documents_targets_conn",
    "delete_documents_edge_conn",
]
