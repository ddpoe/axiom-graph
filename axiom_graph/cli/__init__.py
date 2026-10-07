"""Axiom-graph CLI -- click-based command-line interface.

Phase 4 (ADR-005): split from monolithic ``axiom_graph/cli.py`` into a
subpackage:

- ``cli.__init__`` -- click group ``main``, UTF-8 stdout shim, and command
  registration (imports sibling modules for their side-effects).
- ``cli._core`` -- shared helpers (``_db_path``, ``_require_db``,
  ``_echo_build_summary``).
- ``cli.indexing`` -- init/build/check/mark-clean/purge/checkout/carry-forward/link commands.
- ``cli.rendering`` -- render/render-site/export/viz commands.
- ``cli.inspection`` -- list/graph/report/diff commands plus the ``history`` group.
- ``cli.workflows`` -- the ``workflows`` group (``workflows export``).

Commands
--------
axiom-graph init <project_root> [--id <project_id>]
axiom-graph build <project_root> [--id <project_id>]
axiom-graph render --level <0|1|2|steps> [--id <node_id>] <project_root>
axiom-graph list [--type <node_type>] [--tag <tag>] <project_root>
axiom-graph graph <node_id> [--direction in|out] [--depth N] <project_root>
axiom-graph check <project_root>
axiom-graph export <project_root>
axiom-graph workflows export [<id> ...] <project_root> [--file PATH ...] [--format html|json] [-o OUTPUT]
axiom-graph link <from_node_id> --edge-type <type> --to <to_node_id> <project_root>
axiom-graph mark-clean <node_id> <project_root> [--reason TEXT]
axiom-graph purge [<node_id> ...] <project_root> [--all-not-found] [--reason TEXT] [--yes]
axiom-graph report <project_root> [--since-sha SHA] [--since DATETIME] [--format text|json|condensed] [--exclude-node GLOB ...]
axiom-graph diff <node_id> [<node_id> ...] <project_root> [--baseline SHA] [--summary]
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


class _AxiomGraphGroup(click.Group):
    """Root command group that turns expected refusals into clean CLI errors.

    A refusal the user is meant to act on -- the doc-id namespace gate's
    :class:`~axiom_graph.index.doc_ids.DocIdNamespaceError` and the project-id
    gate's :class:`~axiom_graph.config.ProjectIdMismatchError` -- is re-raised
    as a :class:`click.ClickException`, so every command that can reach it
    prints ``Error: <message>`` and exits 1 instead of a traceback.
    """

    def invoke(self, ctx: click.Context):
        """Invoke the subcommand, translating expected refusals.

        Args:
            ctx: The click context for this invocation.

        Returns:
            Whatever the invoked subcommand returns.

        Raises:
            click.ClickException: When the subcommand raised a
                ``DocIdNamespaceError`` or a ``ProjectIdMismatchError``; its
                message is passed through.
        """
        from axiom_graph.config import ProjectIdMismatchError  # noqa: PLC0415
        from axiom_graph.index.doc_ids import DocIdNamespaceError  # noqa: PLC0415

        try:
            return super().invoke(ctx)
        except (DocIdNamespaceError, ProjectIdMismatchError) as exc:
            logger.info("refused: %s", exc)
            raise click.ClickException(str(exc)) from exc


@click.group(cls=_AxiomGraphGroup)
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
    for _name in ("httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Command registration: importing each submodule registers its commands on
# ``main`` via the ``@main.command`` / ``@main.group`` decorators.
# ---------------------------------------------------------------------------

from axiom_graph.cli import indexing as _indexing  # noqa: E402, F401
from axiom_graph.cli import rendering as _rendering  # noqa: E402, F401
from axiom_graph.cli import inspection as _inspection  # noqa: E402, F401

# ``cli.migration`` follows the ADR-019 import rules from day one (it never
# imports ``axiom_graph.cli``), so its group is attached here rather than by
# a ``@main.group`` decorator inside the module.
from axiom_graph.cli.migration import (  # noqa: E402
    doc_ids_group,
    cmd_doc_ids_preview,
    cmd_doc_ids_execute,
)

main.add_command(doc_ids_group)

from axiom_graph.cli.stamps import stamps_group  # noqa: E402

main.add_command(stamps_group)

from axiom_graph.cli.workflows import cmd_workflows_export, workflows_group  # noqa: E402

main.add_command(workflows_group)

# Re-export individual command callables so tests and direct importers can
# reach them as ``from axiom_graph.cli import cmd_build``.
from axiom_graph.cli.indexing import (  # noqa: E402, F401
    cmd_init,
    cmd_build,
    cmd_checkout,
    cmd_carry_forward,
    cmd_check,
    cmd_mark_clean,
    cmd_purge,
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
    cmd_diff,
    history_group,
    cmd_history_checkpoint,
    cmd_history_agent_verified,
)


__all__ = [
    "main",
    "cmd_init",
    "cmd_build",
    "cmd_checkout",
    "cmd_carry_forward",
    "cmd_check",
    "cmd_mark_clean",
    "cmd_purge",
    "cmd_link",
    "rename_group",
    "cmd_rename_apply",
    "cmd_rename_revert",
    "doc_ids_group",
    "cmd_doc_ids_preview",
    "cmd_doc_ids_execute",
    "cmd_render",
    "cmd_viz",
    "cmd_export",
    "cmd_render_site",
    "cmd_list",
    "cmd_graph",
    "cmd_report",
    "cmd_diff",
    "history_group",
    "cmd_history_checkpoint",
    "cmd_history_agent_verified",
    "workflows_group",
    "cmd_workflows_export",
]
