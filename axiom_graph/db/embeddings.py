"""Axiom-graph DB: sqlite-vec embedding storage and semantic search.

Distinct from ``axiom_graph/index/embeddings.py`` (the model abstraction
layer).  This module owns the I/O for the ``vec_embeddings`` virtual
table and the companion ``embedding_hashes`` table used for skip
detection during build.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

from axiom_graph.models import AxiomNode

from axiom_graph.db._core import (
    _connect,
    _row_to_node,
    _vec_connect,
    _vec_to_bytes,
)

logger = logging.getLogger(__name__)


def init_embeddings(db_path: Path, dim: int) -> bool:
    """Create the vec_embeddings virtual table and embedding_hashes table.

    Uses sqlite-vec to create a virtual table for vector similarity search.
    Also creates a companion table to track content hashes for skip-detection.

    Args:
        db_path: Path to the axiom-graph DB file.
        dim: Embedding dimension (e.g., 384).

    Returns:
        True if tables were created successfully, False if sqlite-vec is unavailable.
    """
    warnings.warn(
        "axiom-graph semantic indexing is deprecated and scheduled for removal in 3.0. "
        "The vec_embeddings table will continue to be created for now but is no longer "
        "a supported feature. See ADR-020.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        with _vec_connect(db_path) as conn:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings "
                f"USING vec0(node_id TEXT PRIMARY KEY, embedding float[{dim}])"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS embedding_hashes (  node_id TEXT PRIMARY KEY,  content_hash TEXT NOT NULL)"
            )
        return True
    except Exception as exc:
        logger.warning("Failed to initialize embeddings table: %s", exc)
        return False


def upsert_embedding(
    db_path: Path,
    node_id: str,
    vec: list[float],
    content_hash: str,
) -> bool:
    """Store or update an embedding vector for a node.

    Skips the write if the stored content_hash matches (no recomputation needed).

    Args:
        db_path: Path to the axiom-graph DB file.
        node_id: The node ID to associate the embedding with.
        vec: The embedding vector as a list of floats.
        content_hash: Hash of the text that was embedded (for skip detection).

    Returns:
        True if the embedding was written, False if skipped (unchanged).
    """
    try:
        existing_hash = get_embedding_hash(db_path, node_id)
        if existing_hash == content_hash:
            return False

        vec_bytes = _vec_to_bytes(vec)
        with _vec_connect(db_path) as conn:
            # Delete existing entry if any
            conn.execute("DELETE FROM vec_embeddings WHERE node_id = ?", (node_id,))
            conn.execute(
                "INSERT INTO vec_embeddings (node_id, embedding) VALUES (?, ?)",
                (node_id, vec_bytes),
            )
            conn.execute(
                "INSERT OR REPLACE INTO embedding_hashes (node_id, content_hash) VALUES (?, ?)",
                (node_id, content_hash),
            )
        return True
    except Exception as exc:
        logger.warning("Failed to upsert embedding for %s: %s", node_id, exc)
        return False


def upsert_embeddings_batch(
    db_path: Path,
    items: list[tuple[str, list[float], str]],
) -> int:
    """Store or update embedding vectors for multiple nodes in a single transaction.

    Args:
        db_path: Path to the axiom-graph DB file.
        items: List of (node_id, vector, content_hash) tuples.

    Returns:
        Number of embeddings written.
    """
    if not items:
        return 0
    written = 0
    try:
        with _vec_connect(db_path) as conn:
            for node_id, vec, content_hash in items:
                vec_bytes = _vec_to_bytes(vec)
                conn.execute("DELETE FROM vec_embeddings WHERE node_id = ?", (node_id,))
                conn.execute(
                    "INSERT INTO vec_embeddings (node_id, embedding) VALUES (?, ?)",
                    (node_id, vec_bytes),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO embedding_hashes (node_id, content_hash) VALUES (?, ?)",
                    (node_id, content_hash),
                )
                written += 1
    except Exception as exc:
        logger.warning("Failed to batch upsert embeddings: %s", exc)
    return written


def get_embedding_hash(db_path: Path, node_id: str) -> str | None:
    """Return the stored content hash for a node's embedding.

    Args:
        db_path: Path to the axiom-graph DB file.
        node_id: The node ID to look up.

    Returns:
        The content hash string, or None if no embedding exists.
    """
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT content_hash FROM embedding_hashes WHERE node_id = ?",
                (node_id,),
            ).fetchone()
            return row["content_hash"] if row else None
    except Exception:
        return None


def get_all_embedding_hashes(db_path: Path) -> dict[str, str]:
    """Return all stored embedding content hashes as {node_id: content_hash}.

    Single bulk query replaces per-node get_embedding_hash() calls during build.

    Args:
        db_path: Path to the axiom-graph DB file.

    Returns:
        Dict mapping node_id to content_hash. Empty dict on error.
    """
    try:
        with _connect(db_path) as conn:
            rows = conn.execute("SELECT node_id, content_hash FROM embedding_hashes").fetchall()
            return {r["node_id"]: r["content_hash"] for r in rows}
    except Exception:
        return {}


def semantic_search(
    db_path: Path,
    query_vec: list[float],
    max_results: int = 20,
    node_type: str | None = None,
    scope: str | None = None,
) -> tuple[list[AxiomNode], int]:
    """Find nodes by vector similarity using sqlite-vec.

    Args:
        db_path: Path to the axiom-graph DB file.
        query_vec: The query embedding vector.
        max_results: Maximum number of results to return.
        node_type: Optional node type filter.
        scope: Optional scope filter ('code', 'docs', or 'all'/None).

    Returns:
        (nodes, total_found) tuple. Returns ([], 0) if vec table is unavailable.
    """
    try:
        vec_bytes = _vec_to_bytes(query_vec)
        # Fetch more than max_results to allow post-filtering
        fetch_limit = max_results * 3

        with _vec_connect(db_path) as conn:
            rows = conn.execute(
                "SELECT node_id, distance FROM vec_embeddings WHERE embedding MATCH ? AND k = ?",
                (vec_bytes, fetch_limit),
            ).fetchall()

            if not rows:
                return [], 0

            node_ids = [r["node_id"] for r in rows]
            placeholders = ",".join("?" * len(node_ids))

            # Build filters
            filters = []
            params: list[str] = list(node_ids)

            if node_type:
                filters.append("node_type = ?")
                params.append(node_type)

            if scope == "code":
                filters.append("source NOT IN ('docjson', 'doc_scanner', 'json_doc_scanner')")
            elif scope == "docs":
                filters.append("source IN ('docjson', 'doc_scanner', 'json_doc_scanner')")

            where_extra = ""
            if filters:
                where_extra = " AND " + " AND ".join(filters)

            node_rows = conn.execute(
                f"SELECT * FROM nodes WHERE id IN ({placeholders}){where_extra}",
                params,
            ).fetchall()

            # Preserve the distance-based ordering from vec_embeddings
            node_map = {dict(r)["id"]: _row_to_node(r) for r in node_rows}
            ordered = [node_map[nid] for nid in node_ids if nid in node_map]

            total = len(ordered)
            return ordered[:max_results], total
    except Exception as exc:
        logger.debug("semantic_search unavailable: %s", exc)
        return [], 0


__all__ = [
    "init_embeddings",
    "upsert_embedding",
    "upsert_embeddings_batch",
    "get_embedding_hash",
    "get_all_embedding_hashes",
    "semantic_search",
]
