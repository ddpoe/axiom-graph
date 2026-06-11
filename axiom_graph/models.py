"""Axiom-graph data models — pure dataclasses + their canonical factory / hash helpers.

``hash16`` is the canonical content-addressing function used for every node's
``code_hash`` and ``desc_hash`` field.  ``make_edge`` is the canonical
``AxiomEdge`` factory.  Both live next to the dataclasses they produce / hash
because that's where readers look first when asking "how do I make one of
these?" or "what hash do these fields use?".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def hash16(text: str) -> str:
    """Return the first 16 hex chars of the SHA-256 of *text*."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass
class StepMarker:
    step_num: float | int
    name: str
    purpose: str
    inputs: str | None = None
    outputs: str | None = None
    critical: str | None = None


@dataclass
class AxiomNode:
    id: str  # "{project_id}::{dotpath}::{name}"
    node_type: str  # NodeType value
    title: str
    location: str  # repo-relative path
    source: str  # "ast" | "doc_scanner" | "manual"
    code_hash: str  # SHA-256[:16] of code (excl. docstring)
    level_0: str
    level_1: str
    subtype: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = "active"  # "active" | "deprecated" | "proposed"
    level_2: str | None = None
    level_3_location: str | None = None  # "path/file.py#L10-L45"
    level_steps: list[StepMarker] | None = None
    desc_hash: str | None = None  # SHA-256[:16] of docstring / description only
    file_mtime: float | None = None  # stored on module/doc nodes; None on function nodes
    dflow_meta: dict | None = None


@dataclass
class AxiomEdge:
    id: str  # "{from_id}::{edge_type}::{to_id}"
    edge_type: str
    from_id: str
    to_id: str
    weight: float = 1.0
    meta: dict | None = None


@dataclass
class AxiomIndex:
    axiom_graph_version: str
    project_id: str
    project_root: str
    built_at: str  # ISO 8601
    nodes: list[AxiomNode]
    edges: list[AxiomEdge]


def make_edge(
    edge_type: str,
    from_id: str,
    to_id: str,
    *,
    meta: dict | None = None,
) -> AxiomEdge:
    """Create an ``AxiomEdge`` with a deterministic id.

    Args:
        edge_type: Edge ontology type (e.g. ``"composes"``, ``"delegates_to"``).
        from_id: Source node id.
        to_id: Target node id.
        meta: Optional edge-level metadata dict.  Persisted as JSON in the
            ``edges.meta`` column.  Used by xstate transitions to record
            ``event`` / ``via`` / ``delay`` / ``internal`` per edge.

    Returns:
        AxiomEdge with deterministic id ``"{from_id}::{edge_type}::{to_id}"``.
    """
    return AxiomEdge(
        id=f"{from_id}::{edge_type}::{to_id}",
        edge_type=edge_type,
        from_id=from_id,
        to_id=to_id,
        meta=meta,
    )
