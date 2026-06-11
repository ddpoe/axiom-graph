"""Shared helpers for axiom-graph CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from axiom_graph.index.paths import db_path as _db_path_canonical


def _db_path(project_root: Path) -> Path:
    """CLI-side wrapper around :func:`axiom_graph.index.paths.db_path`."""
    return _db_path_canonical(project_root)


def _require_db(project_root: Path) -> Path:
    """CLI-side require_db that raises ``ClickException`` (not FileNotFoundError).

    Wraps :func:`axiom_graph.index.paths.db_path` so the missing-DB error
    becomes a CLI-friendly ``click.ClickException`` with a remediation hint.
    """
    path = _db_path(project_root)
    if not path.exists():
        raise click.ClickException(f"No index found at {path}. Run `axiom-graph init {project_root}` first.")
    return path


def _echo_build_summary(summary) -> None:
    """Print the standard build/init summary.

    Accepts either the raw dict shape returned by ``builder.build`` or a
    :class:`axiom_graph.lifecycle.api.BuildSummary` dataclass; both expose
    the same field names so the f-strings work either way.

    Args:
        summary: Build summary (dict or :class:`BuildSummary`).
    """
    # Adapt dataclass -> dict-like access without forcing a specific type.
    if hasattr(summary, "files_scanned") and not isinstance(summary, dict):
        warnings = list(summary.warnings)
        files_scanned = summary.files_scanned
        files_skipped = summary.files_skipped_mtime
        nodes_written = summary.nodes_written
        nodes_skipped = summary.nodes_skipped
        nodes_renamed = summary.nodes_renamed
        edges_written = summary.edges_written
        edges_skipped = summary.edges_skipped
        annotation_findings = list(summary.annotation_findings)
    else:
        warnings = summary["warnings"]
        files_scanned = summary.get("files_scanned", "?")
        files_skipped = summary.get("files_skipped_mtime", 0)
        nodes_written = summary["nodes_written"]
        nodes_skipped = summary["nodes_skipped"]
        nodes_renamed = summary.get("nodes_renamed", 0)
        edges_written = summary["edges_written"]
        edges_skipped = summary["edges_skipped"]
        annotation_findings = summary.get("annotation_findings") or []

    click.echo(
        f"  files scanned : {files_scanned}\n"
        f"  files skipped : {files_skipped} (mtime unchanged)\n"
        f"  nodes written : {nodes_written}\n"
        f"  nodes skipped : {nodes_skipped}\n"
        f"  nodes renamed : {nodes_renamed}\n"
        f"  edges written : {edges_written}\n"
        f"  edges skipped : {edges_skipped}"
    )
    if warnings:
        click.echo(f"  warnings ({len(warnings)}):")
        for w in warnings:
            click.echo(f"    ! {w}")
    if annotation_findings:
        click.echo(f"  Annotation findings ({len(annotation_findings)}):")
        for f in annotation_findings:
            click.echo(f"    ! [{f['rule_id']}] {f['module']}:{f['line']} {f['function']} — {f['message']}")
    click.echo("Done.")


def _row_for_json(row: dict) -> dict:
    """Prepare a history row dict for JSON serialization."""
    out = {k: row[k] for k in ("node_id", "scanned_at", "change_type", "git_sha")}
    if row.get("meta"):
        try:
            out["meta"] = json.loads(row["meta"])
        except Exception:
            out["meta"] = row["meta"]
    return out


__all__ = ["_db_path", "_require_db", "_echo_build_summary", "_row_for_json"]
