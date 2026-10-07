"""``axiom-graph stamps`` -- DocJSON tool-write stamp commands.

Thin presentation over :func:`axiom_graph.docjson.api.axiom_graph_accept_doc_edits`;
follows the ADR-019 import rules (docjson api, click, stdlib only).
"""

from __future__ import annotations

import click

from axiom_graph.docjson.api import axiom_graph_accept_doc_edits


@click.group("stamps")
def stamps_group() -> None:
    """DocJSON tool-write stamps: review and accept raw DocJSON edits."""


@stamps_group.command("accept")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.argument("section_ids", nargs=-1)
@click.option("--all", "all_flagged", is_flag=True, help="Accept every section flagged as a raw DocJSON edit.")
@click.option("--list", "list_only", is_flag=True, help="List the flagged sections and change nothing.")
def cmd_stamps_accept(project_root: str, section_ids: tuple[str, ...], all_flagged: bool, list_only: bool) -> None:
    """Accept hand-edited DocJSON sections: stamp them and verify their text.

    \b
    Example:
        axiom-graph stamps accept . --list
        axiom-graph stamps accept . proj::docs/spec::overview
        axiom-graph stamps accept . --all
    """
    out = axiom_graph_accept_doc_edits(
        project_root,
        section_ids=list(section_ids) or None,
        all_flagged=all_flagged,
        dry_run=list_only,
        verified_by="human",
    )
    click.echo(out)
    if out.startswith("ERROR:"):
        raise SystemExit(1)
