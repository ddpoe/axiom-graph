"""Shared helpers for axiom-graph CLI commands."""

from __future__ import annotations

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


def _echo_annotation_findings(
    findings: list[dict], new: int, resolved: int, *, show_all: bool = False, indent: str = ""
) -> None:
    """Print the annotation-findings count line and the findings worth listing.

    Prints ``Annotation findings: N (X new, Y resolved)`` followed by the
    new findings only, or by every current finding when *show_all* is set.
    Prints nothing when there are no current findings and none resolved.

    Args:
        findings: Current findings, each carrying a ``new`` flag.
        new: How many are new.
        resolved: How many stored findings are gone.
        show_all: List every current finding, not only the new ones.
        indent: Prefix for the count line; findings get two more spaces.
    """
    if not findings and not resolved:
        return
    click.echo(f"{indent}Annotation findings: {len(findings)} ({new} new, {resolved} resolved)")
    for f in findings:
        if show_all or f.get("new"):
            click.echo(f"{indent}  ! [{f['rule_id']}] {f['module']}:{f['line']} {f['function']} — {f['message']}")


def _echo_build_summary(summary) -> None:
    """Print the standard build/init summary.

    Accepts either the raw dict shape returned by ``builder.build`` or a
    :class:`axiom_graph.lifecycle.api.BuildSummary` dataclass; both expose
    the same field names so the f-strings work either way.

    A :class:`BuildSummary` that carries staleness counts ends the report
    with a ``staleness`` line holding the same count summary ``check``
    prints (frozen-doc sections excluded, LINKED_STALE included).

    Args:
        summary: Build summary (dict or :class:`BuildSummary`).
    """
    check = None
    # Adapt dataclass -> dict-like access without forcing a specific type.
    if hasattr(summary, "files_scanned") and not isinstance(summary, dict):
        check = getattr(summary, "check", None)
        warnings = list(summary.warnings)
        files_scanned = summary.files_scanned
        files_skipped = summary.files_skipped_mtime
        docs_skipped = summary.docs_skipped_mtime
        nodes_written = summary.nodes_written
        nodes_skipped = summary.nodes_skipped
        nodes_renamed = summary.nodes_renamed
        edges_written = summary.edges_written
        edges_skipped = summary.edges_skipped
        annotation_findings = list(summary.annotation_findings)
        findings_new = summary.annotation_findings_new
        findings_resolved = summary.annotation_findings_resolved
    else:
        warnings = summary["warnings"]
        files_scanned = summary.get("files_scanned", "?")
        files_skipped = summary.get("files_skipped_mtime", 0)
        docs_skipped = summary.get("docs_skipped_mtime", 0)
        nodes_written = summary["nodes_written"]
        nodes_skipped = summary["nodes_skipped"]
        nodes_renamed = summary.get("nodes_renamed", 0)
        edges_written = summary["edges_written"]
        edges_skipped = summary["edges_skipped"]
        annotation_findings = summary.get("annotation_findings") or []
        findings_new = summary.get("annotation_findings_new", 0)
        findings_resolved = summary.get("annotation_findings_resolved", 0)

    click.echo(
        f"  files scanned : {files_scanned} (Python)\n"
        f"  files skipped : {files_skipped} (Python, content and mtime unchanged)\n"
        f"  docs skipped  : {docs_skipped} (markdown + DocJSON, content and mtime unchanged)\n"
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
    _echo_annotation_findings(annotation_findings, findings_new, findings_resolved, indent="  ")
    click.echo("Done.")
    if check is not None:
        click.echo(f"  staleness     : {check.summary_line()}")


__all__ = ["_db_path", "_require_db", "_echo_annotation_findings", "_echo_build_summary"]
