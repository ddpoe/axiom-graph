"""Indexing-phase commands: init, build, check, mark-clean, checkout, link."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import click

from axiom_graph.cli._core import _db_path, _echo_build_summary, _require_db
from axiom_graph.index import builder, db
from axiom_graph.index.status import (
    BROKEN_LINK,
    CONTENT_UPDATED,
    DESC_UPDATED,
    LINK_PROBLEM_STATUSES,
    LINKED_STALE,
    NOT_FOUND,
    OWN_PROBLEM_STATUSES,
    RENAMED,
    VERIFIED,
)
from axiom_graph.lifecycle import api as lifecycle_api
from axiom_graph.models import AxiomEdge
from axiom_graph.ontology import validate_edge
from axiom_annotations import Step, workflow

from axiom_graph.cli import main

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# axiom-graph init
# ---------------------------------------------------------------------------


@main.command("init")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--id", "project_id", default=None, help="Project ID prefix (default: directory name)")
def cmd_init(project_root: str, project_id: str | None) -> None:
    """Initialise (or re-initialise) the axiom-graph index for PROJECT_ROOT.

    Creates .axiom_graph/graph.db, runs a full scan, and establishes
    code_hash/desc_hash baselines for every node.  This is the safe
    first-run command.

    If the database already exists, prompts for confirmation before
    wiping and rebuilding all baselines.
    """
    root = Path(project_root).resolve()
    db_path = _db_path(root)

    if db_path.exists():
        click.confirm(
            f"Index already exists at {db_path}. "
            "Re-initialising will reset all baselines and clear staleness signals. Continue?",
            abort=True,
        )
        db_path.unlink()
        click.echo("Existing index removed.")

    click.echo(f"Initialising index for {root} ...")
    summary = lifecycle_api.build_index(
        db_path,
        root,
        project_id=project_id,
        discovery_only=False,
    )
    _echo_build_summary(summary)

    if summary.staleness_total:
        click.echo(f"  staleness     : {summary.staleness_total} nodes updated ({summary.staleness_stale} stale)")


# ---------------------------------------------------------------------------
# axiom-graph build
# ---------------------------------------------------------------------------


@main.command("build")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--id", "project_id", default=None, help="Project ID prefix (default: directory name)")
@click.option("--purge", is_flag=True, default=False, help="Remove NOT_FOUND nodes after build")
def cmd_build(project_root: str, project_id: str | None, purge: bool) -> None:
    """Scan PROJECT_ROOT and add newly-discovered nodes/edges.

    Always runs in discovery-only mode: only new nodes are inserted,
    existing ones are untouched (staleness signals preserved).  Edges
    are always updated.

    To reset all baselines, use `axiom-graph init` instead.
    """
    root = Path(project_root).resolve()
    path = _db_path(root)
    click.echo(f"Building index for {root} (discovery-only) ...")
    summary = lifecycle_api.build_index(
        path,
        root,
        project_id=project_id,
        discovery_only=True,
    )
    _echo_build_summary(summary)

    if summary.staleness_total:
        click.echo(f"  staleness     : {summary.staleness_total} nodes updated ({summary.staleness_stale} stale)")

    # Purge NOT_FOUND nodes if requested
    if purge:
        if not path.exists():
            click.echo("  purge skipped : no database found")
        else:
            warnings: list[str] = []
            purged = builder._purge_stale_entries(path, root, warnings)
            click.echo(f"  purged        : {purged} node(s) removed")
            for w in warnings:
                click.echo(f"  ! purge: {w}")


@main.command("checkout")
@click.argument("worktree_path", type=click.Path(file_okay=False))
@click.option(
    "-p", "--project-root", type=click.Path(exists=True, file_okay=False), default=".", help="Source project root."
)
@click.option("--force", is_flag=True, default=False, help="Overwrite existing DB in target.")
def cmd_checkout(worktree_path: str, project_root: str, force: bool) -> None:
    """Copy the axiom-graph DB into WORKTREE_PATH via VACUUM INTO."""
    source_db = _require_db(Path(project_root).resolve())
    target_dir = Path(worktree_path)
    if not target_dir.is_dir():
        raise click.BadParameter(f"Target directory does not exist: {worktree_path}", param_hint="WORKTREE_PATH")
    result = lifecycle_api.checkout_db(source_db, target_dir, force=force)
    if not result.copied:
        click.echo(f"DB already exists at {result.target_db_path}, skipping — delete manually or use --force.")
        return
    click.echo(f"Copied axiom-graph DB to {result.target_db_path}")


# ---------------------------------------------------------------------------
# axiom-graph check
# ---------------------------------------------------------------------------


@main.command("check")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--all", "show_all", is_flag=True, default=False, help="Show all nodes including VERIFIED (default: problems only)."
)
@click.option(
    "--fail-on",
    type=click.Choice(["none", "stale", "unverified", "any"], case_sensitive=False),
    default="none",
    show_default=True,
    help="Exit 1 if matching problem nodes remain after verification promotion.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"], case_sensitive=False),
    default="text",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--strict-annotations",
    "strict_annotations",
    is_flag=True,
    default=False,
    help="Exit 1 if any annotation finding surfaced (composable with --fail-on=stale).",
)
@workflow(
    purpose="Compute per-node staleness via shared engine and report results",
    inputs="project_root path, show_all/fail_on/output_format options",
    outputs="Per-node staleness statuses printed to stdout; exit code 1 on failure",
)
def cmd_check(
    project_root: str, show_all: bool, fail_on: str, output_format: str, strict_annotations: bool = False
) -> None:
    """Report per-node staleness / confidence status (two-dimensional).

    By default only non-VERIFIED nodes are shown. Pass --all for a full inventory.
    A summary line is always printed first.
    """
    from axiom_graph.config import AxiomGraphConfig

    口 = Step(step_num=1, name="Load index", purpose="Resolve project root, open DB, and load all indexed nodes")
    root = Path(project_root).resolve()
    path = _require_db(root)
    nodes = db.all_nodes(path)

    if not nodes:
        click.echo("(no nodes in index)")
        return

    口 = Step(
        step_num=2,
        name="Compute staleness via shared engine and persist",
        purpose="Delegate to lifecycle.api.compute_check_summary which computes, records transition events, and persists in one transaction",
    )
    config = AxiomGraphConfig.load(root)
    cs = lifecycle_api.compute_check_summary(path, root)
    statuses = cs.statuses

    口 = Step(
        step_num=3,
        name="Summarize and output",
        purpose="Count statuses per dimension, format as text or JSON, and print summary line plus problem nodes",
    )
    _OWN_PROBLEM = OWN_PROBLEM_STATUSES
    _LINK_PROBLEM = LINK_PROBLEM_STATUSES
    own_counts = cs.own_counts
    link_counts = cs.link_counts
    clean_count = cs.clean_count

    if output_format == "json":
        serialized: dict[str, dict] = {}
        for nid, (own, link, via) in statuses.items():
            entry: dict = {"own_status": own, "link_status": link}
            if link == LINKED_STALE and via:
                entry["linked_via"] = via
                entry["linked_via_count"] = len(via)
            serialized[nid] = entry
        summary = {
            "own": {
                k: own_counts.get(k, 0)
                for k in (
                    "CONTENT_UPDATED",
                    "DESC_UPDATED",
                    "RENAMED",
                    "NOT_FOUND",
                    "VERIFIED",
                )
            },
            "link": {
                k: link_counts.get(k, 0)
                for k in (
                    "LINKED_STALE",
                    "BROKEN_LINK",
                    "VERIFIED",
                )
            },
            "clean": clean_count,
        }
        # JSON emission deferred until after annotation findings are collected
        # so `annotation_findings` can be included as a new top-level key.
        _json_payload = {"statuses": serialized, "summary": summary}
    else:
        _json_payload = None
        click.echo(
            f"own: {own_counts['CONTENT_UPDATED']} CONTENT_UPDATED / "
            f"{own_counts['DESC_UPDATED']} DESC_UPDATED / "
            f"{own_counts.get('RENAMED', 0)} RENAMED / "
            f"{own_counts['NOT_FOUND']} NOT_FOUND · "
            f"link: {link_counts['LINKED_STALE']} LINKED_STALE / "
            f"{link_counts['BROKEN_LINK']} BROKEN_LINK · "
            f"{clean_count} VERIFIED"
        )

        def _is_problem(own: str, link: str) -> bool:
            return own in _OWN_PROBLEM or link in _LINK_PROBLEM

        rows_to_show = (
            nodes if show_all else [n for n in nodes if _is_problem(*statuses.get(n.id, (VERIFIED, VERIFIED, []))[:2])]
        )
        if not rows_to_show:
            click.echo("(all nodes VERIFIED)")
        else:
            col_w = max(len(n.id) for n in rows_to_show)
            click.echo("")
            click.echo(f"{'NODE':<{col_w}}  OWN_STATUS       LINK_STATUS")
            click.echo("-" * (col_w + 40))
            for node in rows_to_show:
                own, link, via = statuses.get(node.id, (VERIFIED, VERIFIED, []))
                via_suffix = ""
                if link == LINKED_STALE and via:
                    via_suffix = f"  via {via[0]}"
                    if len(via) > 1:
                        via_suffix += f" (+{len(via) - 1} more)"
                click.echo(f"{node.id:<{col_w}}  {own:<16} {link}{via_suffix}")

    # ------------------------------------------------------------------
    # Annotation findings: re-run a lightweight scan to surface rules.
    # Findings are ephemeral (not persisted in the DB) — they are computed
    # on demand here so `check` reports live violations.
    # ------------------------------------------------------------------
    annotation_findings: list = []
    try:
        from axiom_graph.scanners import module_scanner as _mod_scanner

        _findings: list = []
        _autosteps: list = []
        guard = lambda rid: config.validation.is_enabled(rid)  # noqa: E731
        for py_file in root.rglob("*.py"):
            parts = set(py_file.relative_to(root).parts)
            if parts & {
                ".axiom_graph",
                ".git",
                "__pycache__",
                ".venv",
                "venv",
                "node_modules",
                ".tox",
                "dist",
                "build",
            }:
                continue
            try:
                _mod_scanner.scan_module(
                    py_file,
                    root,
                    config.project_id or root.name,
                    findings_out=_findings,
                    autosteps_out=_autosteps,
                    is_rule_enabled=guard,
                )
            except Exception:  # pragma: no cover
                continue
        # B4 deferred resolution
        from axiom_graph.workflows.validation import validate_autostep_targets as _vat

        envelope_ids = {n.id for n in nodes if n.node_type == "composite_process"}
        _findings.extend(_vat(_autosteps, envelope_node_ids=envelope_ids, is_rule_enabled=guard))
        annotation_findings = [f.to_dict() for f in _findings]
    except Exception as exc:  # pragma: no cover
        logger.debug("check: annotation scan failed: %s", exc)

    if output_format == "json" and _json_payload is not None:
        _json_payload["annotation_findings"] = annotation_findings
        click.echo(json.dumps(_json_payload))
    elif output_format == "text":
        if annotation_findings:
            click.echo("")
            click.echo(f"Annotation findings ({len(annotation_findings)}):")
            for f in annotation_findings:
                click.echo(f"  ! [{f['rule_id']}] {f['module']}:{f['line']} {f['function']} — {f['message']}")

    口 = Step(
        step_num=4,
        name="Gate exit code",
        purpose="Exit 1 if --strict-annotations is set and any annotation findings exist, or if --fail-on threshold is exceeded",
    )
    if strict_annotations and annotation_findings:
        raise SystemExit(1)
    if fail_on != "none":
        all_own = {own for own, _link, _via in statuses.values()}
        all_link = {link for _own, link, _via in statuses.values()}
        fail = False
        if fail_on == "stale" and (
            (all_own & {CONTENT_UPDATED, DESC_UPDATED, RENAMED, NOT_FOUND}) or (all_link & {LINKED_STALE, BROKEN_LINK})
        ):
            fail = True
        elif fail_on == "unverified" and (all_own - {VERIFIED}):
            fail = True
        elif fail_on == "any" and ((all_own - {VERIFIED}) or (all_link - {VERIFIED})):
            fail = True
        if fail:
            raise SystemExit(1)


# ---------------------------------------------------------------------------
# axiom-graph mark-clean
# ---------------------------------------------------------------------------


@main.command("mark-clean")
@click.argument("node_id")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--reason", default="", help="Why the documentation is still accurate.")
def cmd_mark_clean(node_id: str, project_root: str, reason: str) -> None:
    """Mark NODE_ID as manually verified clean (MANUAL_VERIFIED)."""
    root = Path(project_root).resolve()
    path = _require_db(root)
    result = lifecycle_api.mark_clean_nodes(path, root, [node_id], reason, verified_by="human")
    if result.not_found:
        raise click.ClickException(f"Node '{node_id}' not found.")
    click.echo(f"Marked '{node_id}' as MANUAL_VERIFIED.")
    if reason:
        click.echo(f"Reason: {reason}")


# ---------------------------------------------------------------------------
# axiom-graph link
# ---------------------------------------------------------------------------


@main.command("link")
@click.argument("from_node_id")
@click.option(
    "--edge-type",
    "edge_type",
    required=True,
    help="Edge type from the ontology (validates, documents, constrains, supersedes, ...).",
)
@click.option("--to", "to_node_id", required=True, help="Target node ID.")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
def cmd_link(from_node_id: str, edge_type: str, to_node_id: str, project_root: str) -> None:
    """Add a typed edge from FROM_NODE_ID to another node.

    The edge type must be a valid ontology edge and the from/to node types must
    satisfy the ontology rules -- an error is raised otherwise.

    Example (test validates production code)\\::

        axiom-graph link pm_mvp::tests.test_db::test_get_engine \\
            --edge-type validates \\
            --to pm_mvp::pm.database::get_engine .
    """
    root = Path(project_root).resolve()
    path = _require_db(root)

    from_node = db.get_node(path, from_node_id)
    if from_node is None:
        raise click.ClickException(f"Source node '{from_node_id}' not found.")

    to_node = db.get_node(path, to_node_id)
    if to_node is None:
        raise click.ClickException(f"Target node '{to_node_id}' not found.")

    error = validate_edge(edge_type, from_node.node_type, to_node.node_type)
    if error:
        raise click.ClickException(f"Ontology violation: {error}")

    edge = AxiomEdge(
        id=f"{from_node_id}::{edge_type}::{to_node_id}",
        edge_type=edge_type,
        from_id=from_node_id,
        to_id=to_node_id,
    )
    written = db.upsert_edge(path, edge)
    verb = "Added" if written else "Already exists"
    click.echo(f"{verb}: {from_node_id} --{edge_type}--> {to_node_id}")


# ---------------------------------------------------------------------------
# axiom-graph rename (apply / revert)
# ---------------------------------------------------------------------------


@main.group("rename")
def rename_group() -> None:
    """Manually weld or un-weld a code-node rename.

    The automatic scoped-similarity matcher applies confident renames at build
    time.  These commands are the manual escape hatch (``apply``) for a real
    rename that fell below the similarity threshold, and the round-trip
    ``revert`` to undo a weld.
    """


@rename_group.command("apply")
@click.argument("old_id")
@click.argument("new_id")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
def cmd_rename_apply(old_id: str, new_id: str, project_root: str) -> None:
    """Weld OLD_ID -> NEW_ID, migrating history/edges and marking NEW_ID RENAMED.

    Restricted to the ``(NOT_FOUND old, newly-created new)`` safety contract:
    OLD_ID must be an existing NOT_FOUND node and NEW_ID must be a newly-created
    live node not already involved in a rename.  Anything else is refused.

    Example\\::

        axiom-graph rename apply proj::old.mod::func proj::new.mod::func .
    """
    root = Path(project_root).resolve()
    path = _require_db(root)
    result = lifecycle_api.apply_rename(path, root, old_id, new_id)
    if not result.applied:
        raise click.ClickException(
            f"Refused to apply rename {old_id} -> {new_id} ({result.reason}). "
            "Contract requires a NOT_FOUND old node and a newly-created live new node."
        )
    click.echo(f"Applied rename: {old_id} -> {new_id} (new node marked RENAMED).")


@rename_group.command("revert")
@click.argument("new_id")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
def cmd_rename_revert(new_id: str, project_root: str) -> None:
    """Un-weld NEW_ID, restoring the prior identity as the live node.

    Re-runs the recorded migration in reverse: history/edges move back to the
    original ID, which becomes live again while NEW_ID is detached as fresh.

    Example\\::

        axiom-graph rename revert proj::new.mod::func .
    """
    root = Path(project_root).resolve()
    path = _require_db(root)
    result = lifecycle_api.revert_rename(path, root, new_id)
    if not result.reverted:
        raise click.ClickException(f"Cannot revert {new_id} ({result.reason}). No recorded rename for this node.")
    click.echo(f"Reverted rename: restored {result.old_id} (detached {new_id}).")


__all__ = [
    "cmd_init",
    "cmd_build",
    "cmd_checkout",
    "cmd_check",
    "cmd_mark_clean",
    "cmd_link",
    "rename_group",
    "cmd_rename_apply",
    "cmd_rename_revert",
]
