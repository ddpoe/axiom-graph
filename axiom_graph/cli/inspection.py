"""Inspection commands: list, graph, report, history group."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from axiom_graph.cli._core import _require_db, _row_for_json
from axiom_graph.index import db
from axiom_graph.lifecycle import api as lifecycle_api
from axiom_graph.query import api as query_api
from axiom_graph.renderers import agent
from axiom_annotations import Step, workflow

from axiom_graph.cli import main

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# axiom-graph list
# ---------------------------------------------------------------------------


@main.command("list")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--type", "node_type", default=None, help="Filter by node type.")
@click.option("--tag", default=None, help="Filter by tag.")
def cmd_list(project_root: str, node_type: str | None, tag: str | None) -> None:
    """List nodes, optionally filtered by type or tag."""
    root = Path(project_root).resolve()
    path = _require_db(root)
    nodes = query_api.list_nodes(path, node_type=node_type, tag=tag)
    click.echo(agent.render_level_1(nodes))


# ---------------------------------------------------------------------------
# axiom-graph graph
# ---------------------------------------------------------------------------


@main.command("graph")
@click.argument("node_id")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--direction",
    default="out",
    type=click.Choice(["in", "out", "both"]),
    show_default=True,
)
@click.option("--depth", default=1, show_default=True, type=int)
def cmd_graph(node_id: str, project_root: str, direction: str, depth: int) -> None:
    """Show the edge graph for NODE_ID."""
    root = Path(project_root).resolve()
    path = _require_db(root)
    # Use a generous max_results so the CLI shows the full edge list (no
    # truncation hint) -- preserves the cycle-2 cmd_graph byte-identity:
    # CLI does not paginate / cap.
    result = query_api.fetch_graph(
        path,
        node_id,
        direction=direction,
        depth=depth,
        max_results=10**9,
        offset=0,
    )
    if result.not_found:
        raise click.ClickException(f"Node '{node_id}' not found.")
    click.echo(result.rendered)


# ---------------------------------------------------------------------------
# axiom-graph history
# ---------------------------------------------------------------------------


@main.group("history")
def history_group() -> None:
    """Manage and inspect node change history."""


@history_group.command("checkpoint")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--node-types",
    default="atomic_process,composite_process",
    show_default=True,
    help="Comma-separated node types to apply to.",
)
@click.option("--message", "-m", default=None, help="Optional message stored in checkpoint meta.")
def cmd_history_checkpoint(
    project_root: str,
    node_types: str,
    message: str | None,
) -> None:
    """Insert a CHECKPOINT semantic marker on qualifying nodes.

    Records the current timestamp and HEAD git SHA as a preserved
    CHECKPOINT row on each matching node.  No history rows are deleted;
    the 100-row hard cap in upsert_node_conn is the only pruning
    mechanism.
    """
    root = Path(project_root).resolve()
    path = _require_db(root)

    # Capture git SHA if available
    git_sha: str | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
        git_sha = result.stdout.strip()[:12]
    except subprocess.TimeoutExpired:
        logger.warning("git rev-parse HEAD timed out")
    except Exception as exc:
        logger.debug("git rev-parse HEAD failed (expected if not a git repo): %s", exc)

    sha_label = f"git:{git_sha}" if git_sha else "no git sha"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    click.echo(f"Checkpoint set: {sha_label} ({today})")

    target_types = [t.strip() for t in node_types.split(",") if t.strip()]
    nodes = db.query_nodes(path)
    matched = [n for n in nodes if n.node_type in target_types]

    meta: str | None = None
    if message:
        meta = json.dumps({"message": message})

    for node in matched:
        db.insert_history_row(
            path,
            node_id=node.id,
            change_type="CHECKPOINT",
            git_sha=git_sha,
            meta=meta,
            preserved=True,
        )

    click.echo(f"Inserted CHECKPOINT on {len(matched)} nodes.")


@history_group.command("agent-verified")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
def cmd_history_agent_verified(project_root: str) -> None:
    """List nodes whose most-recent history row is AGENT_VERIFIED (pre-push gate)."""
    root = Path(project_root).resolve()
    path = _require_db(root)
    rows = db.get_agent_verified_nodes(path)

    if not rows:
        click.echo("No agent-verified nodes pending human review.")
        return

    click.echo("AGENT-VERIFIED NODES (not yet human-reviewed)")
    click.echo("-" * 50)
    for r in rows:
        reason = ""
        if r.get("meta"):
            try:
                reason = json.loads(r["meta"]).get("reason", "")
            except Exception:
                pass
        ts = r["scanned_at"][:10]
        reason_part = f'  "{reason}"' if reason else ""
        click.echo(f"{r['node_id']:<60}  verified {ts}{reason_part}")

    click.echo(f"\n{len(rows)} node(s) pending human review.")


# ---------------------------------------------------------------------------
# axiom-graph report
# ---------------------------------------------------------------------------


@main.command("report")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--since-sha", default=None, help="Git SHA (prefix) of a checkpoint to start from.")
@click.option("--since", "since_timestamp", default=None, help="ISO-8601 datetime cutoff.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--change-type",
    "change_type_pattern",
    default=None,
    help="Glob pattern for change types (e.g. *STALE*, LINK_*, AGENT_*).",
)
@click.option(
    "--node", "node_pattern", default=None, help="Glob pattern for node IDs (e.g. axiom_graph::axiom_graph.viz.*)."
)
@click.option(
    "--node-type",
    "node_type_filter",
    default=None,
    type=click.Choice(["atomic_process", "composite_process", "entity"], case_sensitive=False),
    help="Filter to nodes of this type.",
)
@click.option(
    "--list-refs", is_flag=True, default=False, help="List available reference points (SHAs/checkpoints) and exit."
)
@workflow(
    purpose="Generate an impact report from node history since a reference point",
    inputs="project_root path, since_sha/since_timestamp/output_format options",
    outputs="Grouped history report printed to stdout",
)
def cmd_report(
    project_root: str,
    since_sha: str | None,
    since_timestamp: str | None,
    output_format: str,
    change_type_pattern: str | None,
    node_pattern: str | None,
    node_type_filter: str | None,
    list_refs: bool,
) -> None:
    """Impact report: what changed since a checkpoint, SHA, or datetime.

    Resolution order: --since-sha (checkpoint with matching SHA), then
    --since (datetime), then the most recent checkpoint.  If no reference
    point exists, all history is included.
    """
    root = Path(project_root).resolve()
    path = _require_db(root)

    if list_refs:
        refs = db.list_reference_points(path)
        if not refs:
            click.echo("No reference points found.")
            return
        click.echo(f"{len(refs)} reference point(s):\n")
        for ref in refs:
            sha_short = ref["git_sha"][:12] if ref["git_sha"] else "?"
            ts_date = ref["scanned_at"][:10]
            msg = f'  "{ref["message"]}"' if ref.get("message") else ""
            click.echo(f"  {sha_short}  {ref['type']:<12} {ts_date}  ({ref['row_count']} rows){msg}")
        return

    口 = Step(
        step_num=1, name="Load history rows", purpose="Resolve reference point and query all history rows after it"
    )
    data = lifecycle_api.compute_report(
        path,
        since_sha=since_sha,
        since_timestamp=since_timestamp,
        change_type_pattern=change_type_pattern,
        node_pattern=node_pattern,
        node_type=node_type_filter,
    )

    if data.no_rows:
        click.echo("No history events found after the reference point.")
        return

    口 = Step(
        step_num=2,
        name="Classify events",
        purpose="Bucket history rows into content changes, staleness transitions, link changes, and verifications",
    )
    if data.no_matches:
        click.echo("No history events match the given filters.")
        return

    content_changes = data.content_changes
    staleness_transitions = data.staleness_transitions
    link_changes = data.link_changes
    verifications = data.verifications
    human_verified_ids = data.human_verified_ids

    口 = Step(
        step_num=3,
        name="Compute summary counters",
        purpose="Count nodes changed, became stale, verified, links modified",
    )
    summary = data.summary

    口 = Step(step_num=4, name="Format and output", purpose="Render report in requested format (text or JSON)")

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "summary": summary,
                    "content_changes": {nid: [_row_for_json(r) for r in evts] for nid, evts in content_changes.items()},
                    "staleness_transitions": [_row_for_json(r) for r in staleness_transitions],
                    "link_changes": [_row_for_json(r) for r in link_changes],
                    "verifications": [_row_for_json(r) for r in verifications],
                },
                indent=2,
            )
        )
        return

    # --- Text output ---
    summary_line = (
        f"{summary['nodes_changed']} nodes changed, "
        f"{summary['became_stale']} became stale, "
        f"{summary['verified']} verified ({summary['agent_only']} agent-only), "
        f"{summary['links_modified']} links modified"
    )
    click.echo(summary_line)
    click.echo("=" * len(summary_line))

    if content_changes:
        click.echo("\nCONTENT CHANGES")
        click.echo("-" * 40)
        for node_id, evts in sorted(content_changes.items()):
            types = ", ".join(sorted({e["change_type"] for e in evts}))
            click.echo(f"  {node_id}  [{types}]")

    if staleness_transitions:
        click.echo("\nSTALENESS TRANSITIONS")
        click.echo("-" * 40)
        for r in staleness_transitions:
            meta_str = ""
            if r.get("meta"):
                try:
                    m = json.loads(r["meta"])
                    parts = []
                    if m.get("from"):
                        parts.append(f"was {m['from']}")
                    if m.get("linked_node"):
                        parts.append(f"via {m['linked_node']}")
                    meta_str = f"  ({', '.join(parts)})" if parts else ""
                except Exception:
                    pass
            click.echo(f"  {r['node_id']}  {r['change_type']}{meta_str}")

    if link_changes:
        click.echo("\nLINK CHANGES")
        click.echo("-" * 40)
        for r in link_changes:
            target = ""
            actor = ""
            if r.get("meta"):
                try:
                    m = json.loads(r["meta"])
                    target = m.get("target", "")
                    actor = m.get("actor", "")
                except Exception:
                    pass
            arrow = "→" if r["change_type"] == "LINK_ADDED" else "✕"
            actor_tag = f"  [{actor}]" if actor else ""
            click.echo(f"  {r['node_id']}  {arrow} {target}{actor_tag}")

    if verifications:
        click.echo("\nVERIFICATION ACTIVITY")
        click.echo("-" * 40)
        for r in verifications:
            flag = (
                " ⚠ agent-only"
                if (r["change_type"] == "AGENT_VERIFIED" and r["node_id"] not in human_verified_ids)
                else ""
            )
            click.echo(f"  {r['node_id']}  {r['change_type']}{flag}")


__all__ = [
    "cmd_list",
    "cmd_graph",
    "cmd_report",
    "history_group",
    "cmd_history_checkpoint",
    "cmd_history_agent_verified",
]
