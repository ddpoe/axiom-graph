"""Axiom-graph CLI -- click-based command-line interface.

Phase 4 (ADR-005): split from monolithic ``axiom_graph/cli.py`` into a
subpackage:

- ``cli.__init__`` -- click group ``main``, UTF-8 stdout shim, and command
  registration (imports sibling modules for their side-effects).
- ``cli._core`` -- shared helpers (``_db_path``, ``_require_db``,
  ``_echo_build_summary``, ``_row_for_json``).
- ``cli.indexing`` -- init/build/check/mark-clean/checkout/link commands.
- ``cli.rendering`` -- render/render-site/export/viz commands.
- ``cli.inspection`` -- list/graph/report commands plus the ``history`` group.

Commands
--------
axiom-graph init <project_root> [--id <project_id>]
axiom-graph build <project_root> [--id <project_id>]
axiom-graph render --level <0|1|2|steps> [--id <node_id>] <project_root>
axiom-graph list [--type <node_type>] [--tag <tag>] <project_root>
axiom-graph graph <node_id> [--direction in|out] [--depth N] <project_root>
axiom-graph check <project_root>
axiom-graph export <project_root>
axiom-graph link <from_node_id> --edge-type <type> --to <to_node_id> <project_root>
axiom-graph mark-clean <node_id> <project_root> [--reason TEXT]
axiom-graph report <project_root> [--since-sha SHA] [--since DATETIME] [--format text|json]
axiom-graph history checkpoint <project_root> [OPTIONS]
axiom-graph history agent-verified <project_root>
"""

from __future__ import annotations

import logging
import os
import sys

import click

logger = logging.getLogger(__name__)

# Ensure UTF-8 output on Windows terminals that default to cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
def main() -> None:
    """Axiom-graph -- project knowledge indexer for AI agents."""
    level_name = os.environ.get("AXIOM_GRAPH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Suppress noisy third-party loggers
    for _name in ("httpx", "httpcore", "fastembed", "onnxruntime"):
        logging.getLogger(_name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Command registration: importing each submodule registers its commands on
# ``main`` via the ``@main.command`` / ``@main.group`` decorators.
# ---------------------------------------------------------------------------

from axiom_graph.cli import indexing as _indexing  # noqa: E402, F401
from axiom_graph.cli import rendering as _rendering  # noqa: E402, F401
from axiom_graph.cli import inspection as _inspection  # noqa: E402, F401

# Re-export individual command callables so tests and direct importers can
# reach them as ``from axiom_graph.cli import cmd_build``.
from axiom_graph.cli.indexing import (  # noqa: E402, F401
    cmd_init,
    cmd_build,
    cmd_checkout,
    cmd_check,
    cmd_mark_clean,
    cmd_link,
    rename_group,
    cmd_rename_apply,
    cmd_rename_revert,
)
from axiom_graph.cli.rendering import (  # noqa: E402, F401
    cmd_render,
    cmd_viz,
    cmd_export,
    cmd_render_site,
)
from axiom_graph.cli.inspection import (  # noqa: E402, F401
    cmd_list,
    cmd_graph,
    cmd_report,
    history_group,
    cmd_history_checkpoint,
    cmd_history_agent_verified,
)


__all__ = [
    "main",
    "cmd_init",
    "cmd_build",
    "cmd_checkout",
    "cmd_check",
    "cmd_mark_clean",
    "cmd_link",
    "rename_group",
    "cmd_rename_apply",
    "cmd_rename_revert",
    "cmd_render",
    "cmd_viz",
    "cmd_export",
    "cmd_render_site",
    "cmd_list",
    "cmd_graph",
    "cmd_report",
    "history_group",
    "cmd_history_checkpoint",
    "cmd_history_agent_verified",
]
