"""Rendering commands: render, render-site, export, viz."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import click

from axiom_graph.cli._core import _require_db
from axiom_graph.index import db
from axiom_graph.models import AxiomIndex
from axiom_graph.query import api as query_api

from axiom_graph.cli import main


# ---------------------------------------------------------------------------
# axiom-graph render
# ---------------------------------------------------------------------------


@main.command("render")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--level",
    required=True,
    type=click.Choice(["0", "1", "2", "steps"]),
    help="Output detail level.",
)
@click.option("--id", "node_id", default=None, help="Render a single node by ID.")
@click.option("--type", "node_type", default=None, help="Filter by node type.")
def cmd_render(project_root: str, level: str, node_id: str | None, node_type: str | None) -> None:
    """Render nodes at the specified detail level."""
    root = Path(project_root).resolve()
    path = _require_db(root)

    # Map CLI's "steps" sentinel to the shared api's integer level=3 axis.
    level_int = 3 if level == "steps" else int(level)

    # Use a generous max_results so the CLI shows the full node list (no
    # truncation, no cap header) -- preserves the cycle-2 cmd_render
    # byte-identity: CLI emits a bare body (no header, no badges).
    result = query_api.fetch_render_data(
        path,
        level_int,
        node_id=node_id,
        node_type=node_type,
        max_results=10**9,
        offset=0,
        with_badges=False,
    )
    if result.not_found is not None:
        raise click.ClickException(f"Node '{result.not_found}' not found.")
    click.echo(result.body)


# ---------------------------------------------------------------------------
# axiom-graph viz / axiom-graph export  (visualization + raw export)
# ---------------------------------------------------------------------------


@main.command("viz")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--port", default=8080, show_default=True, help="Port to listen on.")
@click.option("--no-browser", is_flag=True, help="Don't open a browser tab automatically.")
def cmd_viz(project_root: str, port: int, no_browser: bool) -> None:
    """Launch the axiom-graph visualization dashboard.

    Requires the viz extras:  pip install axiom-graph[viz]
    """
    try:
        from axiom_graph.viz.server import run_server  # noqa: PLC0415
    except ImportError:
        raise click.ClickException("Viz dependencies not installed. Run:  pip install axiom-graph[viz]")

    root = Path(project_root).resolve()
    _require_db(root)
    click.echo(f"Axiom-graph Viz -> http://127.0.0.1:{port}  (Ctrl+C to stop)")
    run_server(root, port=port, open_browser=not no_browser)


@main.command("export")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
def cmd_export(project_root: str) -> None:
    """Export the full index to .axiom_graph/index.json."""
    root = Path(project_root).resolve()
    path = _require_db(root)

    nodes = db.all_nodes(path)
    edges = db.all_edges(path)

    index = AxiomIndex(
        axiom_graph_version="0.1.0",
        project_id=root.name,
        project_root=str(root),
        built_at=datetime.now(timezone.utc).isoformat(),
        nodes=nodes,
        edges=edges,
    )

    out_path = root / ".axiom_graph" / "index.json"
    out_path.write_text(
        json.dumps(dataclasses.asdict(index), indent=2, default=str),
        encoding="utf-8",
    )
    click.echo(f"Exported {len(nodes)} nodes and {len(edges)} edges to {out_path.relative_to(root)}")


@main.command("render-site")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--nav", "nav_path", default=None, type=click.Path(), help="Path to site-nav.yml (default: site-nav.yml)")
@click.option(
    "--output", "output_dir", default=None, type=click.Path(), help="Output directory (default: userdocs/guide/)"
)
@click.option(
    "--target",
    "targets",
    multiple=True,
    help="Render only the named target(s) from [[axiom_graph.site.targets]] (repeatable).",
)
@click.option(
    "--build", "run_build", is_flag=True, default=False, help="Run sphinx-build after generating the MyST pages"
)
def cmd_render_site(
    project_root: str,
    nav_path: str | None,
    output_dir: str | None,
    targets: tuple[str, ...],
    run_build: bool,
) -> None:
    """Render consumer documentation site from DocJSON sources.

    With no ``--nav``/``--output``, renders every configured render target
    (``[[axiom_graph.site.targets]]``), or the subset named by ``--target``
    (repeatable).  When no targets are configured an implicit ``guide``
    (sphinx -> ``userdocs/guide``) target is synthesised.

    ``--nav``/``--output`` are single-target ad-hoc overrides that bypass the
    target list entirely and render one nav-driven Sphinx subtree.
    """
    from axiom_graph.docjson.render_consumer import build_site, render_targets

    root = Path(project_root).resolve()
    _require_db(root)

    # Ad-hoc single-target override: --nav / --output bypass the target list.
    if nav_path is not None or output_dir is not None:
        nav = Path(nav_path) if nav_path else None
        out = Path(output_dir) if output_dir else None
        click.echo(f"Building consumer site for {root} ...")
        result = build_site(root, nav_path=nav, output_dir=out, run_sphinx_build=run_build)
        click.echo(f"  pages rendered : {result.pages_rendered}")
        click.echo(f"  output dir     : {result.output_dir}")
        if result.warnings:
            click.echo(f"  warnings       : {len(result.warnings)}")
            for w in result.warnings:
                click.echo(f"    ! {w}")
        else:
            click.echo("  warnings       : 0")
        return

    # Multi-target path.
    only = list(targets) if targets else None
    click.echo(f"Rendering targets for {root} ...")
    results = render_targets(root, only=only, run_sphinx_build=run_build)
    for r in results:
        if r.skipped:
            click.echo(f"  [{r.name}] skipped")
            continue
        click.echo(f"  [{r.name}] {r.format} -> {r.output} : {r.pages_rendered} page(s)")
        for w in r.warnings:
            click.echo(f"    ! {w}")


__all__ = ["cmd_render", "cmd_viz", "cmd_export", "cmd_render_site"]
