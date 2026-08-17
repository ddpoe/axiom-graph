"""Doc-ID migration commands: ``axiom-graph doc-ids preview|execute``.

Presentation only.  Every behavioural decision -- projection, collision
gate, backup, batch, abort -- lives in
:mod:`axiom_graph.lifecycle.api`; this module formats the typed results it
returns and owns nothing else.  Registered on the root ``main`` group from
``axiom_graph.cli.__init__`` so this file needs no import of the CLI
package itself.
"""

from __future__ import annotations

from pathlib import Path

import click

from axiom_graph.config import db_path_for
from axiom_graph.lifecycle.api import execute_doc_id_migration, plan_doc_id_migration


_NO_REVERT_NOTICE = (
    "IRREVERSIBLE: there is no per-document revert path. Rolling back means "
    "restoring the database backup taken before the run."
)


def _format_plan(plan, *, limit: int) -> list[str]:
    """Render a migration plan as report lines.

    Args:
        plan: A :class:`~axiom_graph.lifecycle.api.DocIdMigrationPlan`.
        limit: Maximum old -> new rows shown per kind.  ``0`` shows all.

    Returns:
        Report lines, in order.
    """
    out: list[str] = []
    out.append("=" * 78)
    out.append(f"Doc-ID migration preview — project '{plan.project_id}'")
    out.append("=" * 78)
    out.append(f"Docs roots      : {', '.join(plan.docs_roots) or '(none)'}")
    out.append(f"Documents       : {plan.document_count}")
    out.append(f"Sections        : {plan.section_count}")
    out.append(f"Node identities : {plan.total_nodes}")
    out.append("")

    out.append("-- Collision verdict " + "-" * 57)
    if plan.blocked:
        out.append(f"BLOCKED — {len(plan.blocking_collisions)} doc id(s) have more than one source file.")
        for label, groups in (("projected", plan.collisions), ("current", plan.current_collisions)):
            for collision in groups:
                out.append(f"  [{label}/{collision.kind}] {collision.doc_id}")
                for src in collision.sources:
                    out.append(f"      {src}")
        out.append("Execute mode is unreachable while a collision exists.")
    else:
        out.append("CLEAR — every current and projected doc id has exactly one source file.")
    out.append("")

    out.append("-- Projected identities " + "-" * 54)
    for label, rows in (("documents", plan.documents), ("sections", plan.sections)):
        shown = rows if limit == 0 else rows[:limit]
        out.append(f"[{label}: {len(rows)}]")
        for mapping in shown:
            out.append(f"  {mapping.old_id}")
            out.append(f"    -> {mapping.new_id}")
        if len(rows) > len(shown):
            out.append(f"  ... {len(rows) - len(shown)} more (use --limit 0 for the full map)")
        out.append("")

    out.append("-- Not patched by this migration " + "-" * 45)
    out.append(
        "These references are enumerated, never rewritten. Fix them by hand "
        "after a migration, or they go stale silently."
    )
    for label, bucket in (
        ("DocJSON section content", plan.prose_in_docjson_content),
        ("elsewhere in the repository", plan.prose_elsewhere),
    ):
        out.append(f"[{label}: {len(bucket)} reference(s)]")
        shown = bucket if limit == 0 else bucket[:limit]
        for ref in shown:
            kind = "section" if ref.is_section_reference else "document"
            resolved = ref.doc_id or "(unresolved)"
            out.append(f"  {ref.file_path}:{ref.line}: {ref.text}  [{kind} ref -> {resolved}]")
        if len(bucket) > len(shown):
            out.append(f"  ... {len(bucket) - len(shown)} more (use --limit 0 for the full list)")
        out.append("")

    if plan.dotted_filenames:
        out.append("-- Dotted filenames (advisory) " + "-" * 47)
        for name in plan.dotted_filenames:
            out.append(f"  {name}")
        out.append("")

    if plan.unreadable:
        out.append("-- Unreadable / section-less DocJSON " + "-" * 41)
        for name in plan.unreadable:
            out.append(f"  {name}")
        out.append("")

    out.append("-- What a run preserves " + "-" * 54)
    out.append("  preserved : rename ledger, node history, verification (documents AND sections),")
    out.append("              graph edges, and links[].node_id references on disk")
    out.append("  NOT done  : prose references above; they are reported only")
    out.append(f"  revert    : {'available' if plan.revert_supported else 'NOT available'}")
    out.append(f"  {_NO_REVERT_NOTICE}")
    out.append("")
    out.append("Preview mode wrote nothing: no database mutation, no file mutation.")
    return out


def _format_result(result) -> list[str]:
    """Render an execution result as report lines.

    Args:
        result: A :class:`~axiom_graph.lifecycle.api.DocIdMigrationResult`.

    Returns:
        Report lines, in order.
    """
    out: list[str] = []
    if result.backup_path is not None:
        out.append(f"Database backup: {result.backup_path}")
    if result.executed:
        out.append(f"Migrated {result.documents_migrated} document(s) and {result.sections_migrated} section(s).")
        out.append(
            f"DocJSON files with rewritten links: {result.files_patched} (read {result.doc_files_read} file(s))."
        )
        out.append(_NO_REVERT_NOTICE)
        if result.plan is not None:
            not_patched = len(result.plan.prose_in_docjson_content) + len(result.plan.prose_elsewhere)
            out.append(f"Prose references NOT rewritten: {not_patched}. Re-run preview for the file:line list.")
        return out

    out.append(f"Nothing was migrated ({result.reason}).")
    if result.aborted_at:
        out.append(f"Aborted at: {result.aborted_at}")
    if result.error:
        out.append(f"Error: {result.error}")
    if result.restored_from_backup:
        out.append("The index was restored from the backup — no partial migration remains.")
    if result.plan is not None and result.plan.blocked:
        out.append("Refused: doc ids collide.")
        for collision in result.plan.blocking_collisions:
            out.append(f"  {collision.doc_id}  [{collision.kind}]: {', '.join(collision.sources)}")
    return out


@click.group("doc-ids")
def doc_ids_group() -> None:
    """Preview or execute a doc-ID namespace migration.

    Doc node ids today flatten every configured docs root into one ``docs.``
    namespace and rewrite ``/`` to ``.``.  Neither transform is injective, so
    two files can silently resolve to one identity.  This command projects
    what every document *and* section id would become under per-root
    namespacing, refuses to run if any two would collide, and reports the
    prose references it will not rewrite.

    Two modes:

    \b
      preview  read-only. Writes nothing at all — no database mutation and
               no file mutation. Run this first, always.
      execute  writes. Backs the database up first, then migrates in one
               transaction, aborting the whole run on the first failure.

    \b
    EXECUTE IS IRREVERSIBLE. There is no per-document revert path.
    Rolling back means restoring the backup the run echoes.
    """


@doc_ids_group.command("preview")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--limit", default=20, show_default=True, help="Rows shown per section; 0 shows everything.")
def cmd_doc_ids_preview(project_root: str, limit: int) -> None:
    """Show what a doc-ID migration would do, writing nothing.

    Reports the projected old -> new id for every document and every section,
    the collision verdict, the prose references that will not be rewritten
    (with file and line), and the absence of a revert path.

    \b
    Example:
        axiom-graph doc-ids preview .
        axiom-graph doc-ids preview . --limit 0 > migration-preview.txt
    """
    root = Path(project_root).resolve()
    plan = plan_doc_id_migration(db_path_for(root), root)
    for line in _format_plan(plan, limit=limit):
        click.echo(line)


@doc_ids_group.command("execute")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def cmd_doc_ids_execute(project_root: str, yes: bool) -> None:
    """Migrate every doc and section id in PROJECT_ROOT. IRREVERSIBLE.

    Re-runs the collision gate immediately before writing and refuses when
    any two projected ids collide.  Copies the database to a timestamped
    backup, echoes that path, then migrates in one transaction; a failure
    partway through aborts the whole run and restores the backup rather than
    leaving a partially-migrated index.

    \b
    There is NO per-document revert path. Rolling back means restoring the
    backup this command prints. Run ``doc-ids preview`` first.
    """
    root = Path(project_root).resolve()
    db_path = db_path_for(root)
    if not db_path.exists():
        raise click.ClickException(f"No index at {db_path}. Run `axiom-graph build {root}` first.")
    if not yes:
        click.confirm(
            f"Migrate every doc and section id under {root}? {_NO_REVERT_NOTICE}",
            abort=True,
        )
    result = execute_doc_id_migration(db_path, root)
    for line in _format_result(result):
        click.echo(line)
    if not result.executed:
        raise SystemExit(1)


__all__ = [
    "doc_ids_group",
    "cmd_doc_ids_preview",
    "cmd_doc_ids_execute",
]
