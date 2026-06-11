"""Shared mark_clean logic -- compute current hashes and reset baselines.

All three mark_clean entry points (MCP, CLI, viz server) delegate to
``mark_node_clean`` so the hashing, verification snapshot, and baseline
update logic is in one place.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from axiom_annotations import task

from axiom_graph.index import db
from axiom_graph.scanners.node_hashing import current_node_hash

if TYPE_CHECKING:
    from axiom_graph.models import AxiomNode

logger = logging.getLogger(__name__)


def compute_current_hashes(
    node: "AxiomNode",
    project_root: Path,
) -> tuple[str | None, str | None]:
    """Compute current (code_hash, desc_hash) for a node from the file on disk.

    Thin wrapper around
    :func:`axiom_graph.scanners.node_hashing.current_node_hash` -- the
    consolidated primitive used by both ``mark_clean`` and
    ``compute_staleness`` step 2.  Centralising the dispatch table
    eliminates the qualified-name vs short-name disagreement that
    caused chronic ``CONTENT_UPDATED`` churn for sibling-class tests
    and ``@workflow`` / ``@task`` envelopes.  See
    :mod:`axiom_graph.scanners.node_hashing` for the dispatch ladder
    and rationale.

    Falls back to stored DB hashes if the file cannot be parsed or the
    node is not found in the file.

    Args:
        node: The AxiomNode to compute hashes for.
        project_root: Absolute path to the project root.

    Returns:
        Tuple of (code_hash, desc_hash) representing the current file
        state.  Either element may be ``None`` (envelopes always return
        ``desc_hash=None``; functions without docstrings have no
        ``desc_hash``).
    """
    return current_node_hash(node, project_root)


@task(
    purpose="Record verification history, compute current hashes, write verification snapshot, and reset baselines",
    inputs="db_path, project_root, AxiomNode, reason string, verified_by identifier",
    outputs="None — side effects: history row, verification snapshot, and baseline reset written to DB",
)
def mark_node_clean(
    db_path: Path,
    project_root: Path,
    node: "AxiomNode",
    reason: str,
    verified_by: str,
) -> None:
    """Record verification and reset baseline hashes for one node.

    This is the shared logic for all mark_clean entry points. It:
    1. Inserts a history row (AGENT_VERIFIED or MANUAL_VERIFIED).
    2. Computes current hashes from the file on disk.
    3. Writes a verification snapshot with those hashes.
    4. Resets the baseline code_hash/desc_hash on the nodes table.

    It deliberately does NOT advance ``file_mtime``.  That column is the
    builder's scan-skip cache; advancing it here would make the next build
    skip the file and freeze the node's scan-derived summary (``level_1`` /
    ``level_2``).  See :func:`axiom_graph.db.nodes.update_node_baseline`.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        project_root: Absolute path to the project root.
        node: The AxiomNode to mark clean.
        reason: Brief explanation for the verification.
        verified_by: Identifier (e.g. ``'human'``, ``'agent:model'``).
    """
    change_type = "AGENT_VERIFIED" if verified_by.startswith("agent") else "MANUAL_VERIFIED"
    meta = json.dumps({"reason": reason}) if reason else None
    db.insert_history_row(
        db_path,
        node_id=node.id,
        change_type=change_type,
        meta=meta,
        preserved=True,
    )

    cur_code, cur_desc = compute_current_hashes(node, project_root)

    db.upsert_verification(
        db_path,
        node_id=node.id,
        verified_by=verified_by,
        code_hash_at=cur_code,
        desc_hash_at=cur_desc,
        reason=reason or None,
    )

    # Reset baseline hashes so the next compute_staleness re-parses the file,
    # finds baseline == current, and resolves to VERIFIED.  file_mtime is
    # intentionally left untouched -- see update_node_baseline.
    db.update_node_baseline(
        db_path,
        node_id=node.id,
        code_hash=cur_code,
        desc_hash=cur_desc,
    )
