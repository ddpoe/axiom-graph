"""axiom_graph.db — top-level package re-exporting the DB surface.

Phase 4 directory restructure: the top-level ``axiom_graph.db`` name is
the canonical import path for database operations.  Implementation is
split across seven submodules:

- ``_core``       — schema + connection + serdes helpers
- ``nodes``       — node CRUD + verification
- ``edges``       — edge CRUD + ID migration
- ``docs``        — doc/doc_section CRUD + FTS search
- ``history``     — history rows + reference points
- ``staleness``   — staleness persistence + computed queries
- ``embeddings``  — sqlite-vec embedding I/O + semantic search

``axiom_graph.index.db`` is a back-compat shim that re-exports from here.
Callers should prefer ``from axiom_graph.db import X`` going forward.
"""

from __future__ import annotations

from axiom_graph.db._core import *  # noqa: F401,F403
from axiom_graph.db.docs import *  # noqa: F401,F403
from axiom_graph.db.edges import *  # noqa: F401,F403
from axiom_graph.db.embeddings import *  # noqa: F401,F403
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
    _load_sqlite_vec,
    _node_to_row,
    _now_utc,
    _row_to_edge,
    _row_to_node,
    _steps_to_json,
    _vec_connect,
    _vec_to_bytes,
)
from axiom_graph.db.edges import _migrate_edges  # noqa: F401
from axiom_graph.db.nodes import _get_node_hashes_conn  # noqa: F401
