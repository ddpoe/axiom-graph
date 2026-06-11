"""Shared helpers for MCP tool implementations.

After ADR-019 cycle 3 the only inhabitant of this module is the
``_timed_tool`` wall-clock-logging decorator applied at registration
time in ``axiom_graph.mcp.server``.  All DB-path helpers, the semantic
search handler, and the staleness/mark-clean/file-rescan helpers live in
their respective bounded contexts:

- ``axiom_graph.index.paths.db_path`` / ``require_db`` -- canonical DB path.
- ``axiom_graph.query.api`` -- search (incl. semantic), render, list,
  graph, source, sql, drift_query, list_tags, list_undocumented.
- ``axiom_graph.lifecycle.api`` -- build, check, mark_clean, purge,
  history, report, diff, render_site, checkout.
"""

from __future__ import annotations

import functools
import logging
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool timing decorator
# ---------------------------------------------------------------------------


def _timed_tool(fn):
    """Decorator that logs start/done with wall-clock duration for MCP tools.

    Args:
        fn: The tool function to wrap.

    Returns:
        Wrapped function with timing logging.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        name = fn.__name__
        logger.info("tool %s: start", name)
        t0 = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            elapsed = time.monotonic() - t0
            logger.info("tool %s: done (%.3fs)", name, elapsed)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.warning("tool %s: error (%.3fs): %s", name, elapsed, exc)
            raise

    return wrapper
