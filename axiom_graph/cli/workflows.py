"""``axiom-graph workflows`` -- workflow and task commands.

Thin presentation over :func:`axiom_graph.workflows.api.write_workflow_export`,
the same export path the MCP ``axiom_graph_workflow_export`` tool uses.  Like
``cli.stamps`` it never imports ``main``: the group is attached to it in
``cli/__init__.py``.  Nothing here needs the viz extra.
"""

from __future__ import annotations

from pathlib import Path

import click

from axiom_graph.workflows.api import EXPORT_FORMATS, WorkflowExportError, write_workflow_export


@click.group("workflows")
def workflows_group() -> None:
    """Workflows and tasks: export them with their code."""


@workflows_group.command("export")
@click.argument("ids", nargs=-1)
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--file",
    "files",
    multiple=True,
    help="Export every workflow and task defined in this source file (repeatable).",
)
@click.option(
    "--format",
    "export_format",
    type=click.Choice(EXPORT_FORMATS),
    default="html",
    show_default=True,
    help="html writes the self-contained page; json writes the bundle.",
)
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(dir_okay=False),
    default=None,
    help="File to write. Default: workflow-export.html (or .json) in the current directory.",
)
def cmd_workflows_export(
    ids: tuple[str, ...],
    project_root: str,
    files: tuple[str, ...],
    export_format: str,
    output: str | None,
) -> None:
    """Export workflows and every source file they reach to one shareable file.

    IDS are workflow or task names, or their node ids. Workflows and tasks
    can be mixed. --file adds every workflow and task in a source file. An
    id or file that matches nothing is an error, and nothing is written.

    \b
    Example:
        axiom-graph workflows export run_pipeline .
        axiom-graph workflows export --file src/pipeline.py -o pipeline.html .
        axiom-graph workflows export build_report prep_rows --format json .
    """
    root = Path(project_root).resolve()

    # A --file path that exists from here is the caller's path; anything else
    # is taken as project-relative, the form the index stores.
    selected_files = [str(Path(f).resolve()) if Path(f).exists() else f for f in files]
    out_path = Path(output) if output else Path(f"workflow-export.{export_format}")
    try:
        summary = write_workflow_export(
            root,
            out_path,
            workflow_ids=ids,
            files=selected_files,
            format=export_format,
        )
    except WorkflowExportError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Wrote {summary.path}: {summary.label}")
