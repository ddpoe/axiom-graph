"""Shim for backward compat — real implementation moved to ``axiom_graph.mcp.server``.

The ``axiom-graph-mcp`` console script (see ``pyproject.toml``) points at
``axiom_graph.mcp_server:run``, so this shim keeps that entry point valid
after the Phase 4 directory restructure.  Star re-export preserves the
existing ``from axiom_graph.mcp_server import axiom_graph_*`` import paths
used throughout the test suite.

``run`` is redefined locally (not re-exported) so that tests which do
``patch.object(axiom_graph.mcp_server, "mcp", ...)`` continue to see the
patched ``mcp`` reference — the real ``run()`` in ``mcp.server`` closes
over its own module-level ``mcp`` and would ignore the patch otherwise.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from axiom_graph.mcp.server import *  # noqa: F401,F403
from axiom_graph.mcp.server import mcp  # noqa: F401
from axiom_graph.mcp._helpers import _timed_tool  # noqa: F401


def run() -> None:
    """Start the MCP server (stdio transport).

    Body duplicated from ``axiom_graph.mcp.server.run`` so module-level
    ``patch.object(axiom_graph.mcp_server, "mcp", ...)`` affects the
    mcp reference used here.
    """
    level_name = os.environ.get("AXIOM_GRAPH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format=fmt,
    )
    log_file = os.environ.get("AXIOM_GRAPH_LOG_FILE")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=2,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(file_handler)
    mcp.run(transport="stdio")


__all__ = ["run", "mcp"]


if __name__ == "__main__":
    run()
