"""Axiom-graph ontology — node types, edge types, and validation helpers.

Loads ontology.yaml from the same directory as this module.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Load YAML
# ---------------------------------------------------------------------------

_YAML_PATH = Path(__file__).parent / "ontology.yaml"

with _YAML_PATH.open("r", encoding="utf-8") as _f:
    ONTOLOGY: dict[str, Any] = yaml.safe_load(_f)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class NodeType(str, enum.Enum):
    atomic_process = "atomic_process"
    composite_process = "composite_process"
    entity = "entity"


class EdgeType(str, enum.Enum):
    consumes = "consumes"
    produces = "produces"
    composes = "composes"
    delegates_to = "delegates_to"
    depends_on = "depends_on"
    constrains = "constrains"
    validates = "validates"
    documents = "documents"
    annotates = "annotates"
    supersedes = "supersedes"


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

# Pre-build a lookup: edge_type -> (frozenset of valid from types, frozenset of valid to types)
_EDGE_RULES: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
for _etype, _spec in ONTOLOGY.get("edge_types", {}).items():
    _EDGE_RULES[_etype] = (
        frozenset(_spec.get("from", [])),
        frozenset(_spec.get("to", [])),
    )


def valid_edge(edge_type: str, from_node_type: str, to_node_type: str) -> bool:
    """Return True if this edge_type is permitted between the two node types."""
    if edge_type not in _EDGE_RULES:
        return False
    valid_from, valid_to = _EDGE_RULES[edge_type]
    return from_node_type in valid_from and to_node_type in valid_to


def validate_edge(edge_type: str, from_node_type: str, to_node_type: str) -> str | None:
    """Return an error message string if the edge is invalid, else None."""
    if not valid_edge(edge_type, from_node_type, to_node_type):
        rules = _EDGE_RULES.get(edge_type)
        if rules is None:
            return f"Unknown edge_type '{edge_type}'"
        valid_from, valid_to = rules
        return (
            f"Edge '{edge_type}' from '{from_node_type}' to '{to_node_type}' "
            f"is not permitted. Allowed: from={sorted(valid_from)}, to={sorted(valid_to)}"
        )
    return None
