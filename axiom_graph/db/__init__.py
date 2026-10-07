"""axiom_graph.db — top-level package re-exporting the DB surface.

Phase 4 directory restructure: the top-level ``axiom_graph.db`` name is
the canonical import path for database operations.  Implementation is
split across eight submodules:

- ``_core``       — schema + connection + serdes helpers
- ``migrations``  — versioned schema migrations (PRAGMA user_version)
- ``nodes``       — node CRUD + verification
- ``edges``       — edge CRUD + ID migration
- ``files``       — per-file records and staleness stamps
- ``findings``    — annotation findings store (per-file findings, AutoSteps)
- ``docs``        — doc metadata + doc-section node reads + FTS search
- ``history``     — history rows + reference points
- ``staleness``   — staleness persistence + computed queries

``axiom_graph.index.db`` is a back-compat shim that re-exports from here.
Callers should prefer ``from axiom_graph.db import X`` going forward.
"""

from __future__ import annotations

from axiom_graph.db._core import *  # noqa: F401,F403
from axiom_graph.db.migrations import *  # noqa: F401,F403
from axiom_graph.db.docs import *  # noqa: F401,F403
from axiom_graph.db.edges import *  # noqa: F401,F403
from axiom_graph.db.files import *  # noqa: F401,F403
from axiom_graph.db.findings import *  # noqa: F401,F403
from axiom_graph.db.history import *  # noqa: F401,F403
from axiom_graph.db.nodes import *  # noqa: F401,F403
from axiom_graph.db.staleness import *  # noqa: F401,F403

# Re-export private helpers that callers import by name (star-export skips
# names beginning with underscore).
from axiom_graph.db._core import (  # noqa: F401
    _connect,
    _derive_change_type,
    _edge_to_row,
    _json_to_steps,
    _node_to_row,
    _now_utc,
    _row_to_edge,
    _row_to_node,
    _steps_to_json,
    open_connection,
)
from axiom_graph.db.edges import _migrate_edges  # noqa: F401
from axiom_graph.db.nodes import _get_node_hashes_conn  # noqa: F401
