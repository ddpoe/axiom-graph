"""Indexing-phase commands: init, build, check, mark-clean, purge, checkout, carry-forward, link."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from axiom_graph.cli._core import _db_path, _echo_annotation_findings, _echo_build_summary, _require_db
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
from axiom_graph.project import api as project_api
from axiom_graph.models import AxiomEdge
from axiom_graph.ontology import validate_edge
from axiom_annotations import AutoStep, Step, workflow

from axiom_graph.cli import main

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# axiom-graph init
# ---------------------------------------------------------------------------


@main.command("init")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--id",
    "project_id",
    default=None,
    help=(
        "Project ID prefix, recorded in axiom-graph.toml "
        "(default: the toml's project_id, else the existing index's id, else the directory name)"
    ),
)
@click.option(
    "--policy",
    is_flag=True,
    default=False,
    help=(
        "Reset only the agent-policy doc: write axiom-graph's shipped default, asking before it overwrites "
        "an existing one. Needs an index; never touches it."
    ),
)
@click.option(
    "--settings",
    is_flag=True,
    default=False,
    help=(
        "Reset only axiom-graph.toml to the defaults, keeping project_id, after listing what changes and "
        "asking. Never touches the index; `axiom-graph build` applies the new settings."
    ),
)
@click.option(
    "--all",
    "reset_all",
    is_flag=True,
    default=False,
    help=(
        "Reset everything behind one confirm: the index, axiom-graph.toml (to the defaults, keeping "
        "project_id) and the agent-policy doc (to the shipped default)."
    ),
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help=(
        "Answer yes to the --policy and --settings prompts. It never skips the confirm before the "
        "index is deleted, so it is an error without --policy or --settings."
    ),
)
@workflow(
    purpose=(
        "Reset the index: create or re-create it from a full scan under a project id recorded in "
        "axiom-graph.toml, then seed the agent-policy doc when the project has none.  --policy and "
        "--settings instead reset only the policy doc or the toml; --all resets all three"
    ),
    inputs="project_root path, optional --id, --policy, --settings, --all, --yes",
    outputs=(
        "A fresh index, axiom-graph.toml holding project_id (its other settings untouched unless --all), "
        "the shipped agent policy as an editable doc when none existed (restored when --all), and the build "
        "summary on stdout; with --policy / --settings, only that doc or the toml changes"
    ),
)
def cmd_init(
    project_root: str,
    project_id: str | None,
    policy: bool,
    settings: bool,
    reset_all: bool,
    yes: bool,
) -> None:
    """Initialise (or re-initialise) the axiom-graph index for PROJECT_ROOT.

    Creates .axiom_graph/graph.db, runs a full scan, and establishes
    code_hash/desc_hash baselines for every node.  This is the safe
    first-run command.

    If the database already exists, init asks before deleting it.  It resets
    only the index: every baseline, staleness signal, verification record and
    the change history.  It keeps the ``axiom-graph.toml`` settings and the
    agent-policy doc.  After the build, when no doc carries the
    ``agent-policy`` tag, it writes axiom-graph's shipped agent policy as an
    editable doc in the primary docs root; an existing policy doc is never
    overwritten.

    The reset flags reset one thing each and never touch the index:
    ``--policy`` writes the shipped policy (asking before it overwrites the
    project's policy doc; it needs an index), and ``--settings`` resets
    ``axiom-graph.toml`` to the defaults, keeping ``project_id`` (it lists the
    settings that change and asks).  Together they run policy first, then
    settings.  ``--yes`` answers their prompts; without a terminal and without
    ``--yes`` the answer is no.  ``--all`` resets the index, the toml and the
    policy doc behind one confirm that ``--yes`` never skips.

    The project id resolves like ``build``'s: ``--id``, then the toml's
    ``project_id``, then the id of the index being replaced, then the
    directory name (an empty id counts as unset).  The id is recorded as
    ``project_id`` in ``axiom-graph.toml`` when the toml does not hold it yet
    (the file is created, or the key added under ``[axiom_graph]``), so every
    later command, in a worktree or a fresh clone too, uses the same id.  When
    the toml already holds a different ``project_id``, ``--id`` is refused
    before any prompt and nothing changes.
    """
    from axiom_graph.config import (  # noqa: PLC0415
        ConfigError,
        check_toml_project_id,
        reset_toml_to_defaults,
        toml_settings_off_default,
        write_toml_project_id,
    )

    口 = Step(
        step_num=1,
        name="Check the flags",
        purpose="Refuse a flag combination init cannot honour, and --policy without an index",
        critical="Checked before any prompt, so a combined call never half-applies",
    )
    root = Path(project_root).resolve()
    db_path = _db_path(root)
    _check_init_flags(db_path, project_id=project_id, policy=policy, settings=settings, reset_all=reset_all, yes=yes)

    if policy or settings:
        口 = Step(
            step_num=2,
            name="Run the reset-only flags",
            purpose="--policy, then --settings, each with its own prompt that --yes answers",
            critical="Never resolves the id, deletes, rebuilds or re-initialises the index",
        )
        if policy:
            _reset_policy(root, yes=yes)
        if settings:
            _reset_settings(root, db_path, yes=yes)
        return

    口 = Step(
        step_num=3,
        name="Resolve the project id",
        purpose="Take --id, else the toml's id, else the id of the index being replaced, else the directory name",
        critical="An --id that differs from the toml's project_id is refused before any prompt",
    )
    project_id = project_id or None
    if project_id is not None:
        check_toml_project_id(root, project_id)
    else:
        project_id = builder.resolve_project_id(root, db_path)

    口 = Step(
        step_num=4,
        name="Delete the existing index",
        purpose="Confirm, saying what is reset and what is kept (--all: listing all three resets), then remove "
        "the old database",
        critical="Declining changes nothing; --yes never answers this confirm",
    )
    off_default = toml_settings_off_default(root) if reset_all else []
    if reset_all:
        _confirm_full_reset(root, db_path, off_default, project_id)
    elif db_path.exists():
        click.confirm(
            f"Index already exists at {db_path}. "
            "Re-initialising resets the index: it DELETES this database and rebuilds it from scratch, "
            "so every baseline and staleness signal is reset, and every verification record and the "
            "whole change history are lost. It keeps the axiom-graph.toml settings and the agent-policy "
            "doc; to reset those too, run `axiom-graph init --settings`, `axiom-graph init --policy` or "
            "`axiom-graph init --all`. Continue?",
            abort=True,
        )
    if db_path.exists():
        db_path.unlink()
        click.echo("Existing index removed.")

    if reset_all and off_default:
        口 = Step(
            step_num=5,
            name="Reset the toml",
            purpose="--all: rewrite axiom-graph.toml to the defaults, keeping the resolved id, before the rebuild",
        )
        reset_toml_to_defaults(root, project_id)
        click.echo("axiom-graph.toml reset to the defaults.")

    口 = Step(
        step_num=6,
        name="Record the project id",
        purpose="Write project_id into axiom-graph.toml when it does not hold it yet",
    )
    try:
        if write_toml_project_id(root, project_id):
            click.echo(f'Recorded project_id = "{project_id}" in axiom-graph.toml.')
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    口 = Step(step_num=7, name="Build the index", purpose="Run a full scan under the project id and print the summary")
    click.echo(f"Initialising index for {root} ...")
    summary = lifecycle_api.build_index(
        db_path,
        root,
        project_id=project_id,
        discovery_only=False,
    )
    _echo_build_summary(summary)

    try:
        if reset_all:
            口 = AutoStep(step_num=8, name="Restore the agent policy")
            written = project_api.restore_policy(root)
        else:
            口 = AutoStep(step_num=9, name="Seed the agent policy")
            written = project_api.seed_policy(root)
    except project_api.PolicyWriteError as exc:
        raise click.ClickException(f"The agent policy was not written: {exc}") from exc
    _echo_policy_seed(root, written)


def _check_init_flags(
    db_path: Path,
    *,
    project_id: str | None,
    policy: bool,
    settings: bool,
    reset_all: bool,
    yes: bool,
) -> None:
    """Refuse the init flag combinations that cannot be honoured, before any prompt.

    Args:
        db_path: The index file.
        project_id: ``--id``'s value.
        policy: ``--policy``.
        settings: ``--settings``.
        reset_all: ``--all``.
        yes: ``--yes``.

    Raises:
        click.UsageError: ``--id`` with a reset flag; ``--all`` with
            ``--policy`` / ``--settings`` / ``--yes``; ``--yes`` without a
            reset flag.
        click.ClickException: ``--policy`` with no index.
    """
    reset_only = policy or settings
    if reset_all and reset_only:
        raise click.UsageError("--all already resets the toml and the policy; use it without --policy or --settings.")
    if reset_all and yes:
        raise click.UsageError("--yes never skips the confirm before the index is deleted, so --all takes no --yes.")
    if yes and not reset_only:
        raise click.UsageError(
            "--yes answers only the --policy and --settings prompts; it never skips the confirm before the "
            "index is deleted."
        )
    if reset_only and project_id:
        raise click.UsageError("--id sets the id of a new index; --policy and --settings never touch the index.")
    if policy and not db_path.exists():
        raise click.ClickException(
            f"No index at {db_path}: --policy writes through the index. Run `axiom-graph init` first; "
            "it writes the agent policy."
        )


def _ask(question: str, *, yes: bool, refused: str) -> bool:
    """Ask a reset flag's yes/no question, answered by --yes and refused without a terminal.

    Args:
        question: The question, for ``click.confirm``.
        yes: ``--yes``: answer yes without asking.
        refused: What to print when there is no terminal to ask.

    Returns:
        The answer; no without a terminal (``refused`` is printed).
    """
    if yes:
        return True
    if not _stdin_is_a_terminal():
        click.echo(refused)
        return False
    return click.confirm(question, default=False)


def _reset_policy(root: Path, *, yes: bool) -> None:
    """Run ``init --policy``: write the shipped policy, asking before an overwrite.

    Args:
        root: The project root (its index exists).
        yes: ``--yes``.

    Raises:
        click.ClickException: The write path refused the doc.
    """
    existing = project_api.find_policy_doc(root)
    if existing is not None:
        shown = f"{existing.doc_id} ({project_api.relative_to_root(root, existing.file)})"
        if not _ask(
            f"Overwrite the agent-policy doc {shown} with axiom-graph's shipped default?",
            yes=yes,
            refused=f"Not overwriting {shown}: no terminal to ask. Run it from a terminal, or pass --yes.",
        ):
            click.echo(f"Left {shown} as it is.")
            return
    try:
        written = project_api.restore_policy(root)
    except project_api.PolicyWriteError as exc:
        raise click.ClickException(f"The agent policy was not written: {exc}") from exc
    _echo_policy_seed(root, written)


def _reset_settings(root: Path, db_path: Path, *, yes: bool) -> None:
    """Run ``init --settings``: list the toml settings off their defaults and reset them on a yes.

    Args:
        root: The project root.
        db_path: The index file (read for the id to keep; never written).
        yes: ``--yes``.
    """
    from axiom_graph.config import reset_toml_to_defaults, toml_settings_off_default  # noqa: PLC0415

    off_default = toml_settings_off_default(root)
    if not off_default:
        if (root / "axiom-graph.toml").exists():
            click.echo("axiom-graph.toml holds only the defaults: nothing to reset.")
        else:
            click.echo("No axiom-graph.toml: the settings are the defaults, nothing to reset.")
        return
    project_id = builder.resolve_project_id(root, db_path)
    if _ask_toml_reset(off_default, project_id, yes=yes):
        reset_toml_to_defaults(root, project_id)
        click.echo("axiom-graph.toml reset to the defaults. Run `axiom-graph build` to apply them.")


def _confirm_full_reset(
    root: Path, db_path: Path, off_default: list[tuple[str, object, object]], project_id: str
) -> None:
    """Ask ``init --all``'s one confirm, listing the index, toml and policy resets.

    No prompt when there is nothing but a fresh index to build.  Declining,
    or no terminal, aborts init with nothing changed.

    Args:
        root: The project root.
        db_path: The index file.
        off_default: The toml settings the reset changes.
        project_id: The id the toml keeps.
    """
    plan = project_api.plan_policy_reset(root)
    if not db_path.exists() and not off_default and plan.existing is None and not plan.target_exists:
        return
    lines = ["init --all resets:"]
    if db_path.exists():
        lines.append(
            f"  the index at {db_path}: DELETED and rebuilt, losing every baseline, staleness signal, "
            "verification record and the whole change history"
        )
    else:
        lines.append("  the index: built from a full scan")
    if off_default:
        lines.append(f'  axiom-graph.toml: reset to the defaults (project_id stays "{project_id}"):')
        lines.extend(f"    {line}" for line in _off_default_lines(off_default))
    else:
        lines.append("  axiom-graph.toml: already at the defaults")
    lines.append(f"  the agent policy: {_policy_reset_line(root, plan)}")
    click.echo("\n".join(lines))
    click.confirm("Continue?", abort=True)


def _policy_reset_line(root: Path, plan: project_api.PolicyResetPlan) -> str:
    """Describe what ``init --all`` does to the agent policy, for its confirm.

    Args:
        root: The project root.
        plan: :func:`axiom_graph.project.api.plan_policy_reset`'s result.

    Returns:
        One line.
    """
    target = project_api.relative_to_root(root, plan.target) if plan.target is not None else None
    if plan.existing is not None:
        shown = f"{plan.existing.doc_id} ({project_api.relative_to_root(root, plan.existing.file)})"
        if plan.in_place:
            return f"{shown} is overwritten with the shipped default"
        return (
            f"{shown} is left on disk unindexed (the default settings drop its docs root); "
            f"the shipped default is written to {target}"
        )
    if target is None:
        return "nothing is written (the default settings configure no docs root)"
    if plan.target_exists:
        return f"{target} is overwritten with the shipped default if it is tagged agent-policy, else left as it is"
    return f"the shipped default is written to {target}"


def _echo_policy_seed(root: Path, seeded: project_api.PolicyWrite) -> None:
    """Echo what a policy seed or restore did: one line, or nothing when a policy doc was kept.

    Args:
        root: The project root.
        seeded: :func:`axiom_graph.project.api.seed_policy`'s or
            :func:`~axiom_graph.project.api.restore_policy`'s result.
    """
    if seeded.written and seeded.file is not None:
        click.echo(
            f"Agent policy written to {project_api.relative_to_root(root, seeded.file)}: edit it to change "
            "the rules. Reset it with `axiom-graph init --policy`, the toml with `axiom-graph init --settings`."
        )
    elif seeded.note:
        click.echo(f"No agent policy written: {seeded.note}")


def _stdin_is_a_terminal() -> bool:
    """Whether init can ask a question: standard input is an interactive terminal."""
    return sys.stdin is not None and sys.stdin.isatty()


def _off_default_lines(off_default: list[tuple[str, object, object]]) -> list[str]:
    """Return one ``key = value  (default: ...)`` line per toml setting off its default.

    Args:
        off_default: :func:`axiom_graph.config.toml_settings_off_default`'s triples.

    Returns:
        The lines, unindented.
    """
    from axiom_graph.config import NO_DEFAULT  # noqa: PLC0415

    lines = []
    for key, value, default in off_default:
        shown = (
            "none, the key is dropped"
            if default is NO_DEFAULT
            else json.dumps(default, default=str, ensure_ascii=False)
        )
        lines.append(f"{key} = {json.dumps(value, default=str, ensure_ascii=False)}  (default: {shown})")
    return lines


def _ask_toml_reset(off_default: list[tuple[str, object, object]], project_id: str, *, yes: bool = False) -> bool:
    """List the toml settings that differ from the defaults and ask whether to reset them.

    Args:
        off_default: :func:`axiom_graph.config.toml_settings_off_default`'s triples.
        project_id: The id a reset keeps.
        yes: ``--yes``: list them and answer yes without asking.

    Returns:
        ``True`` when the answer is yes.  ``False`` when there is nothing
        to list, the answer is no, or there is no terminal to ask and no
        ``--yes`` (the list is still shown).  Ctrl-C at the question aborts.
    """
    if not off_default:
        return False
    click.echo("axiom-graph.toml has settings that differ from the defaults:")
    for line in _off_default_lines(off_default):
        click.echo(f"  {line}")
    return _ask(
        f'Reset axiom-graph.toml to the defaults? project_id stays "{project_id}".',
        yes=yes,
        refused="Not resetting axiom-graph.toml: no terminal to ask. Run it from a terminal, or pass --yes.",
    )


# ---------------------------------------------------------------------------
# axiom-graph build
# ---------------------------------------------------------------------------


@main.command("build")
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--id",
    "project_id",
    default=None,
    help=(
        "Project ID prefix; must match the id the index stores "
        "(default: axiom-graph.toml's project_id, else the stored id, else directory name)"
    ),
)
@click.option(
    "--purge",
    is_flag=True,
    default=False,
    help=(
        "Re-run the deleted-file pass after the build (every build already runs it). "
        "To remove NOT_FOUND nodes, use `axiom-graph purge --all-not-found`."
    ),
)
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

    # Re-run the deleted-file pass (every build already ran it) if requested
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


@main.command("carry-forward")
@click.argument("worktree_path", type=click.Path(exists=True))
@click.option(
    "-p",
    "--project-root",
    type=click.Path(exists=True, file_okay=False),
    default=".",
    help="The checkout the worktree was merged into.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Judge every stale node and write nothing; prints the summary and the nodes it would carry, in full and in "
    "part. Reads the statuses as this checkout's last build or check stored them, so run it after the build.",
)
@click.option("--list", "list_nodes", is_flag=True, default=False, help="List every stale node by verdict.")
def cmd_carry_forward(worktree_path: str, project_root: str, dry_run: bool, list_nodes: bool) -> None:
    """Copy the verifications a merged worktree made into this checkout's index.

    Run from the checkout WORKTREE_PATH was merged into, after its build.
    WORKTREE_PATH is the worktree directory or its .axiom_graph/graph.db,
    opened read-only.  A node stale here takes the worktree's verification
    in full, with the versions it was checked against, when the worktree
    verified exactly what this checkout holds: VERIFIED there, the same
    content, and every link a verification settles present there with each
    linked node at the verified version (for an envelope, every annotated
    function and delegated task too).

    A node the worktree verified at the same content that is still flagged
    there, or here, for something else is carried one dimension at a time:
    its own-text verification, and the worktree's receipt for each link
    whose linked node is at the version the receipt names.  Every other
    link keeps its state here.  The report counts full and partial carries
    and says why each other stale node keeps its status.  A carried node
    stays stale while a node it depends on is itself stale here (a
    doc-to-doc link, or an envelope's annotated function or delegated task)
    until that node settles; the report counts these.

    Refused, with nothing written, when the two indexes differ in schema
    version or project id.
    """
    root = Path(project_root).resolve()
    path = _require_db(root)
    try:
        result = lifecycle_api.carry_forward_verifications(path, root, Path(worktree_path), dry_run=dry_run)
    except lifecycle_api.CarryForwardRefusedError as exc:
        raise click.ClickException(f"carry-forward refused: {exc}") from exc
    click.echo(lifecycle_api.render_carry_forward_report(result, list_nodes=list_nodes))


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
@click.option(
    "--full",
    "full",
    is_flag=True,
    default=False,
    help="Recompute every node from a re-hash of every file, instead of only what changed since the last check.",
)
@workflow(
    purpose="Bring per-node staleness up to date via the shared refresh and report results",
    inputs="project_root path, show_all/fail_on/output_format/full options",
    outputs="Per-node staleness statuses printed to stdout; exit code 1 on failure",
)
def cmd_check(
    project_root: str,
    show_all: bool,
    fail_on: str,
    output_format: str,
    strict_annotations: bool = False,
    full: bool = False,
) -> None:
    """Report per-node staleness / confidence status (two-dimensional).

    By default only non-VERIFIED nodes are shown. Pass --all for a full inventory.
    A summary line is always printed first.  Only what changed since the
    last check is recomputed; --full recomputes every node.
    """
    口 = Step(step_num=1, name="Resolve index", purpose="Resolve project root and the index path")
    root = Path(project_root).resolve()
    path = _require_db(root)

    口 = AutoStep(step_num=2, name="Load the check report")
    report = lifecycle_api.load_check_report(path, root, full=full, every_node=show_all or output_format == "json")
    if report is None:
        click.echo("(no nodes in index)")
        return
    cs = report.summary

    口 = Step(
        step_num=3,
        name="Summarize and output",
        purpose="Format as text or JSON and print the summary line plus problem nodes (every node with --all or "
        "JSON) and the structural changes the refresh found",
    )
    _OWN_PROBLEM = OWN_PROBLEM_STATUSES
    _LINK_PROBLEM = LINK_PROBLEM_STATUSES
    own_counts = cs.own_counts
    link_counts = cs.link_counts
    clean_count = cs.clean_count
    structure_lines = lifecycle_api.structure_lines(cs.structure)

    if output_format == "json":
        statuses = cs.statuses
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
        _json_payload = {
            "statuses": serialized,
            "summary": summary,
            "structural_changes": {loc: cs.structure[loc] for loc in sorted(cs.structure)},
        }
    else:
        _json_payload = None
        click.echo(cs.summary_line())

        def _is_problem(own: str, link: str) -> bool:
            return own in _OWN_PROBLEM or link in _LINK_PROBLEM

        if show_all:
            statuses = cs.statuses
            rows_to_show = list(cs.ordered_ids)
        else:
            statuses = cs.problem_statuses
            rows_to_show = [nid for nid, (own, link, _via) in statuses.items() if _is_problem(own, link)]
        if not rows_to_show:
            click.echo("(all nodes VERIFIED)")
        else:
            col_w = max(len(nid) for nid in rows_to_show)
            click.echo("")
            click.echo(f"{'NODE':<{col_w}}  OWN_STATUS       LINK_STATUS")
            click.echo("-" * (col_w + 40))
            for nid in rows_to_show:
                own, link, via = statuses.get(nid, (VERIFIED, VERIFIED, []))
                via_suffix = ""
                if link == LINKED_STALE and via:
                    via_suffix = f"  via {via[0]}"
                    if len(via) > 1:
                        via_suffix += f" (+{len(via) - 1} more)"
                click.echo(f"{nid:<{col_w}}  {own:<16} {link}{via_suffix}")
        if structure_lines:
            click.echo("")
            for line in structure_lines:
                click.echo(line)

    口 = Step(
        step_num=4,
        name="Report annotation findings",
        purpose="The findings the report read, or the error reading them raised",
    )
    if report.findings_error is not None:
        exc = report.findings_error
        raise click.ClickException(f"could not read annotation findings: {exc}") from exc
    af = report.findings
    annotation_findings = af.findings

    if output_format == "json" and _json_payload is not None:
        _json_payload["annotation_findings"] = annotation_findings
        click.echo(json.dumps(_json_payload))
    elif output_format == "text" and (annotation_findings or af.resolved):
        click.echo("")
        _echo_annotation_findings(annotation_findings, af.new, af.resolved, show_all=show_all)

    口 = Step(
        step_num=5,
        name="Gate exit code",
        purpose="Exit 1 if --strict-annotations is set and any annotation findings exist, or if --fail-on threshold is exceeded",
    )
    if strict_annotations and annotation_findings:
        raise SystemExit(1)
    if fail_on != "none":
        all_own = cs.own_present
        all_link = cs.link_present
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
    if node_id in result.inherited:
        click.echo("Warning: LINKED_STALE on this node is inherited — marking it clean has no direct effect.")
        click.echo("Stale descendants to mark clean:")
        for desc in result.inherited[node_id]:
            click.echo(f"  - {desc}")
    elif node_id in result.mixed:
        click.echo("Own stale signal cleared; LINKED_STALE inherited from stale descendants remains:")
        for desc in result.mixed[node_id]:
            click.echo(f"  - {desc}")
    if reason:
        click.echo(f"Reason: {reason}")


# ---------------------------------------------------------------------------
# axiom-graph purge
# ---------------------------------------------------------------------------


@main.command("purge")
@click.argument("node_ids", nargs=-1)
@click.argument("project_root", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--all-not-found",
    "all_not_found",
    is_flag=True,
    default=False,
    help="Purge every node whose own_status is NOT_FOUND instead of naming them.",
)
@click.option(
    "--reason",
    default="removed with axiom-graph purge",
    show_default=True,
    help="Why the nodes are being removed (recorded in their history).",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the --all-not-found confirmation prompt.")
def cmd_purge(node_ids: tuple[str, ...], project_root: str, all_not_found: bool, reason: str, yes: bool) -> None:
    """Remove NOT_FOUND nodes (deleted code or docs) from the index.

    Name the nodes as NODE_IDS, or pass --all-not-found to purge every node
    whose own_status is NOT_FOUND.  Only NOT_FOUND nodes are ever purged: a
    named node with any other status is refused and left in place, and the
    command exits 1.  A module, DocJSON doc or config node whose file is
    still on disk is refused (or, with --all-not-found, kept): its NOT_FOUND
    is inherited from the NOT_FOUND nodes in that file, so purge those
    instead if they were really removed.  Every node of a Python, DocJSON
    or JS/TS file that is on disk but does not parse (for JS/TS, one
    tree-sitter parses with errors) is refused too (or, with
    --all-not-found, kept, and the file named once): it reads NOT_FOUND only
    because the file could not be read, so fix the file and re-run check.
    A purged doc takes its sections with it.  Each purge records a DELETED history
    row carrying actor ``human`` and the reason.  Statuses are the ones the
    last build or check stored.

    A purged node can no longer be welded to its successor with
    ``axiom-graph rename apply``, so record any missed rename first.

    \b
    Example:
        axiom-graph purge proj::old.mod::func .
        axiom-graph purge --all-not-found . --yes
    """
    if bool(node_ids) == all_not_found:
        raise click.UsageError("Name the nodes to purge, or pass --all-not-found (not both).")
    root = Path(project_root).resolve()
    path = _require_db(root)

    if all_not_found:
        selection = lifecycle_api.select_purgeable_not_found(path, root)
        targets, parse_verdicts = selection.to_purge, selection.parse_verdicts
        for file_path, kept in selection.unparseable.items():
            click.echo(
                f"Kept {len(kept)} NOT_FOUND node(s) in {file_path}: it is on disk but does not parse; "
                "fix the file and re-run check"
            )
        for nid in selection.inherited:
            click.echo(f"Kept: {nid} ({lifecycle_api.PURGE_INHERITED_HINT})")
        if not targets:
            click.echo("No NOT_FOUND nodes to purge.")
            return
        click.echo(f"{len(targets)} NOT_FOUND node(s):")
        for nid in targets:
            click.echo(f"  - {nid}")
        if not yes:
            click.confirm(f"Purge these {len(targets)} node(s)?", abort=True)
    else:
        targets, parse_verdicts = list(node_ids), None

    results = lifecycle_api.purge_nodes(path, root, targets, reason, actor="human", parse_verdicts=parse_verdicts)
    purged = 0
    refused = 0
    for pr in results:
        if pr.purged:
            purged += 1
            click.echo(f"Purged: {pr.node_id}")
        elif pr.reason == lifecycle_api.PURGE_REFUSED_INHERITED:
            refused += 1
            click.echo(f"Not purged: {pr.node_id} ({lifecycle_api.PURGE_INHERITED_HINT})")
            for child in pr.deleted_children:
                click.echo(f"  - {child}")
        elif pr.reason == lifecycle_api.PURGE_REFUSED_UNPARSEABLE:
            refused += 1
            click.echo(f"Not purged: {pr.node_id} ({lifecycle_api.PURGE_UNPARSEABLE_HINT})")
        elif pr.reason == "not_found_in_index" and all_not_found:
            # Sorted order puts a parent before its children, so a doc's
            # sections already went with the doc; nothing is left to purge.
            purged += 1
            click.echo(f"Purged: {pr.node_id} (removed with its parent)")
        elif pr.reason == "not_found_in_index":
            refused += 1
            click.echo(f"Not purged: {pr.node_id} (not in the index)")
        else:
            refused += 1
            status = (pr.reason or "").removeprefix("status_")
            click.echo(f"Not purged: {pr.node_id} (status {status}; only NOT_FOUND nodes can be purged)")
    click.echo(f"Purged {purged} of {len(results)} node(s). Reason: {reason}")
    if refused:
        raise SystemExit(1)


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
    _echo_unpatched_links(result.links_unreadable, result.links_not_patched, old_id, new_id)


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
    _echo_unpatched_links(result.links_unreadable, result.links_not_patched, new_id, result.old_id or "")


def _echo_unpatched_links(unreadable: list[str], not_patched: list[str], old_id: str, new_id: str) -> None:
    """Warn about DocJSON files a rename could not re-point (locks busy) or could not check (unreadable)."""
    for line in lifecycle_api.link_rewrite_note(unreadable, not_patched, old_id, new_id):
        click.echo(f"WARNING: {line}", err=True)


__all__ = [
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
]
