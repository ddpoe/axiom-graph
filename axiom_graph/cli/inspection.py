"""Inspection commands: list, graph, report, history group."""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from axiom_graph.cli._core import _require_db
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
        git_sha = result.stdout.strip() or None
    except subprocess.TimeoutExpired:
        logger.warning("git rev-parse HEAD timed out")
    except Exception as exc:
        logger.debug("git rev-parse HEAD failed (expected if not a git repo): %s", exc)

    sha_label = f"git:{git_sha[:12]}" if git_sha else "no git sha"
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
@click.option(
    "--since-sha",
    default=None,
    help="Git SHA (prefix, 4+ chars) to start from: a checkpoint, a build row, or any commit git knows.",
)
@click.option("--since", "since_timestamp", default=None, help="ISO-8601 datetime cutoff.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "condensed"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format: text (one line per event), condensed (aggregated by container), or json.",
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
    "--exclude-node",
    "exclude_node_patterns",
    multiple=True,
    help="Glob pattern for node IDs to leave out (repeatable), e.g. a cycle manifest: '{doc_id}*'.",
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
    inputs="project_root path, since_sha/since_timestamp, filters (incl. repeatable --exclude-node), output_format",
    outputs="Report printed to stdout, headed by the resolved reference; non-zero exit on an unresolvable --since-sha",
)
def cmd_report(
    project_root: str,
    since_sha: str | None,
    since_timestamp: str | None,
    output_format: str,
    change_type_pattern: str | None,
    node_pattern: str | None,
    exclude_node_patterns: tuple[str, ...],
    node_type_filter: str | None,
    list_refs: bool,
) -> None:
    """Impact report: what changed since a checkpoint, SHA, or datetime.

    Resolution order: --since-sha (a checkpoint, then a build row with a
    matching SHA, then the commit's git commit time), then --since
    (datetime), then the most recent checkpoint, then the most recent
    SHA-bearing history row.  Only when none of those exist — and no
    reference was given — is the whole history reported.  A --since-sha
    that neither the index nor git can resolve (unknown, ambiguous, or
    shorter than 4 characters) is an error.  Every format starts with
    the reference it was measured against.
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
        step_num=1,
        name="Resolve reference and load classified history",
        purpose="Resolve the reference point (index, then git commit time) and classify the rows after it",
        critical="An unresolvable explicit --since-sha must exit non-zero, never report over all history",
    )
    try:
        data = lifecycle_api.compute_report(
            path,
            since_sha=since_sha,
            since_timestamp=since_timestamp,
            change_type_pattern=change_type_pattern,
            node_pattern=node_pattern,
            node_type=node_type_filter,
            exclude_node_pattern=list(exclude_node_patterns),
            project_root=root,
        )
    except lifecycle_api.UnresolvedReferenceError as exc:
        raise click.ClickException(str(exc)) from exc

    口 = Step(step_num=2, name="Format and output", purpose="Render the report as text, condensed text, or JSON")
    if output_format == "json":
        click.echo(json.dumps(lifecycle_api.report_to_dict(data), indent=2))
        return
    detail = "condensed" if output_format == "condensed" else "full"
    click.echo(lifecycle_api.render_report_text(data, detail))


@main.command("diff")
@click.argument("node_ids", nargs=-1, required=True)
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--baseline",
    "baseline_sha",
    default=None,
    help="Commit or rev expression to diff against (e.g. abc1234 or HEAD~3). "
    "Default: for a node gone stale, the last recorded commit before it went stale; "
    "otherwise its last verified/checkpoint commit, else its oldest indexed commit.",
)
@click.option(
    "--summary",
    "summary_only",
    is_flag=True,
    default=False,
    help="Print only metadata and the +N / -M line counts, not the old and new source.",
)
@workflow(
    purpose="Print what changed in one or more nodes since a baseline commit, as the axiom_graph_diff MCP tool does",
    inputs="node ids, project_root, optional --baseline, --summary flag",
    outputs="One JSON report per node separated by '---'; exit 1 if any node returned an error",
)
def cmd_diff(node_ids: tuple[str, ...], project_root: str, baseline_sha: str | None, summary_only: bool) -> None:
    """Show what changed in nodes since a baseline commit.

    Prints, for each of NODE_IDS, the same JSON the ``axiom_graph_diff``
    MCP tool returns; several nodes are separated by a ``---`` line.  The
    node is found by identity in the baseline and current file, not at its
    indexed line range.  A node that cannot be diffed prints its
    ``{"error", "reason"}`` JSON in its place and the rest still run; the
    command then exits 1.

    \b
    Example:
        axiom-graph diff proj::pkg.mod::func . --baseline HEAD~3 --summary
    """
    root = Path(project_root).resolve()
    path = _require_db(root)

    口 = Step(step_num=1, name="Diff each node", purpose="Build each node's diff report through the lifecycle API")
    reports = [
        lifecycle_api.node_diff_report(path, root, nid, baseline_sha=baseline_sha, summary_only=summary_only)
        for nid in node_ids
    ]

    口 = Step(
        step_num=2,
        name="Print and set the exit code",
        purpose="Print each report as the MCP tool formats it, joined by its batch delimiter; exit 1 if any errored",
    )
    click.echo(lifecycle_api.NODE_DIFF_BATCH_DELIMITER.join(lifecycle_api.format_node_diff_report(r) for r in reports))
    if any("error" in r for r in reports):
        raise SystemExit(1)


__all__ = [
    "cmd_list",
    "cmd_graph",
    "cmd_report",
    "cmd_diff",
    "history_group",
    "cmd_history_checkpoint",
    "cmd_history_agent_verified",
]
