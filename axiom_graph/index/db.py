"""Back-compat shim: ``axiom_graph.index.db`` re-exports ``axiom_graph.db``.

Phase 4 directory restructure moved the database layer to the top-level
``axiom_graph.db`` package.  This module remains as a shim so legacy
callers (``from axiom_graph.index.db import X``) continue to work.  Per
ADR decision D-A8, the shim is load-bearing — removal is deferred to a
future cycle.
"""

from __future__ import annotations

# Re-export ``sqlite3`` and ``time`` as module attributes so legacy tests that
# do ``from axiom_graph.index import db as db_mod`` then patch
# ``db_mod.sqlite3.connect`` or ``db_mod.time.monotonic`` continue to work.
# The _connect implementation that uses these symbols actually lives in
# ``axiom_graph.db._core`` — patches on these shim attributes are re-exported
# via the module reference below so the _core module also sees them.
from axiom_graph.db import _core as _db_core  # noqa: F401

sqlite3 = _db_core.sqlite3
time = _db_core.time

from axiom_graph.db import *  # noqa: F401,F403,E402

# Private helpers (star import skips names starting with underscore).
from axiom_graph.db import (  # noqa: F401
    _connect,
    _vec_connect,
    _load_sqlite_vec,
    _vec_to_bytes,
    _now_utc,
    _node_to_row,
    _row_to_node,
    _edge_to_row,
    _row_to_edge,
    _steps_to_json,
    _json_to_steps,
    _derive_change_type,
    _get_node_hashes_conn,
    _migrate_edges,
)
