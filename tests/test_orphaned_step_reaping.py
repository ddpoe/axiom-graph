"""Lifecycle of workflow step rows across rebuilds.

A source file is the source of truth for the step markers the scanners derive
from it.  A marker that is renumbered, removed, or carried off to another
module leaves a row behind that nothing else can reach: a step row never
becomes ``NOT_FOUND``, so purge refuses it, and its enclosing workflow node
has usually died with the function that held it, taking the ``composes`` edge
away.  For every file a build walks, the step rows stored there are
reconciled against the markers that file declares — and every file the build
did not walk is left completely alone.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.lifecycle import api as lifecycle_api

PIPELINE = "proj::pipeline::run_pipeline"
PIPELINE_ENVELOPE = f"{PIPELINE}@workflow"
REPORT = "proj::pipeline::run_report"
NEIGHBOUR = "proj::neighbour::run_neighbour"
PIPELINE_FILE = "pipeline.py"
NEIGHBOUR_FILE = "neighbour.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _workflow_block(func_name: str, purpose: str, phases: Sequence[str]) -> str:
    """Return a decorated workflow whose steps are numbered 1..len(phases)."""
    lines = [f'@workflow(purpose="{purpose}")', f"def {func_name}():"]
    if not phases:
        lines.append('    return "nothing to do"')
    for step_num, phase in enumerate(phases, start=1):
        lines.append(f'    口 = Step(step_num={step_num}, name="{phase}", purpose="the {phase} phase")')
        lines.append(f'    print("{phase}")')
    return "\n".join(lines)


def _write_module(project_root: Path, *blocks: str, name: str = PIPELINE_FILE) -> Path:
    """Write a module made of *blocks* into *project_root* and return its path."""
    src = project_root / name
    body = '"""Pipeline module."""\n\nfrom axiom_annotations import Step, workflow\n\n\n'
    body += "\n\n\n".join(blocks) + "\n"
    src.write_text(body, encoding="utf-8")
    return src


def _write_pipeline(project_root: Path, phases: Sequence[str]) -> Path:
    """Write a module holding one workflow with the given phases."""
    return _write_module(project_root, _workflow_block("run_pipeline", "Run the pipeline", phases))


def _write_neighbour(project_root: Path, phases: Sequence[str] = ("Open", "Close")) -> Path:
    """Write a second annotated module, so a build can walk one file and skip another.

    A build that walks nothing reconciles nothing, whatever its scope
    predicate is.  Every claim about what a *partial* walk leaves alone needs
    a second file to be the part that was walked.
    """
    return _write_module(
        project_root,
        _workflow_block("run_neighbour", "Run the neighbour", phases),
        name=NEIGHBOUR_FILE,
    )


def _build(project_root: Path, *, discovery_only: bool = True):
    """Run a build the way the CLI and MCP surfaces run one."""
    return lifecycle_api.build_index(
        project_root / ".axiom_graph" / "graph.db",
        project_root,
        project_id="proj",
        discovery_only=discovery_only,
    )


def _step_rows(db_path: Path, location: str | None = None) -> dict[str, tuple]:
    """Return stored step rows keyed by node id, with their scanned fields.

    Args:
        db_path: Path to the index database.
        location: Optional file to restrict the read to.  Omit for every step
            row in the index.
    """
    sql = "SELECT id, title, location, level_1, level_3_location FROM nodes WHERE subtype IN ('step', 'autostep')"
    params: tuple = ()
    if location is not None:
        sql += " AND location = ?"
        params = (location,)
    with db._connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return {r["id"]: (r["title"], r["location"], r["level_1"], r["level_3_location"]) for r in rows}


def _step_ids(db_path: Path) -> set[str]:
    """Return the ids of every stored step row."""
    return set(_step_rows(db_path))


def _parentless_step_ids(db_path: Path) -> set[str]:
    """Return the step rows that no surviving workflow node composes."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT n.id FROM nodes n WHERE n.subtype IN ('step', 'autostep') "
            "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.to_id = n.id AND e.edge_type = 'composes')"
        ).fetchall()
    return {r["id"] for r in rows}


def _drop_enclosing_workflow(db_path: Path, envelope_id: str) -> None:
    """Delete a workflow node, the way losing its function deletes it.

    The cascade takes the ``composes`` edges with it, leaving the step rows
    stored at that file with no parent to be enumerated through — the state
    an index carries after a function is renamed or moved away.
    """
    with db._connect(db_path) as conn:
        db.delete_node_by_id(conn, envelope_id)


def _hold_below_the_index_mtime(path: Path) -> None:
    """Rewind a file's mtime so the next build's fast pass skips it."""
    past = time.time() - 3600
    os.utime(path, (past, past))


def _touch(path: Path) -> None:
    """Advance a file's mtime without changing a byte of its content."""
    future = time.time() + 5
    os.utime(path, (future, future))


def _reap_notices(summary) -> list[str]:
    """Return the build warnings reporting step rows this build removed."""
    return [w for w in summary.warnings if "no longer declares" in w]


def _leftover_notices(summary) -> list[str]:
    """Return the build warnings reporting orphaned step rows still in the index."""
    return [w for w in summary.warnings if "orphaned workflow step" in w]


def _leave_orphaned_rows(project_root: Path, db_path: Path) -> Path:
    """Leave step rows in the index that no source and no workflow justifies.

    Reproduces the residue an index carries from before it reconciled step
    rows: the markers are gone from the file and the workflow node that held
    them is gone from the index, but the rows are still there — and the file's
    mtime is old enough that the fast pass will skip it.
    """
    src = _write_pipeline(project_root, ["Collect", "Transform", "Emit"])
    _build(project_root, discovery_only=True)
    _drop_enclosing_workflow(db_path, PIPELINE_ENVELOPE)
    _write_module(project_root, '@workflow(purpose="Do nothing in particular")\ndef unrelated():\n    return 0')
    _hold_below_the_index_mtime(src)
    return src


# ---------------------------------------------------------------------------
# Tier 3 — user-story-level e2e scenarios
# ---------------------------------------------------------------------------


@workflow(
    purpose="A file the build skipped via the mtime fast-pass keeps every one of its step rows, unmodified",
)
def test_step_rows_survive_on_a_file_the_build_did_not_walk(mini_project, db_path):
    口 = Step(step_num=1, name="Index two annotated modules", purpose="rows a later build could destroy")
    _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    _write_neighbour(mini_project)
    _build(mini_project, discovery_only=True)
    before = _step_rows(db_path, PIPELINE_FILE)
    assert set(before) == {f"{PIPELINE}::step-{n}" for n in (1, 2, 3)}

    口 = Step(step_num=2, name="Touch only the neighbour, then rebuild", purpose="one file walked, one skipped")
    _touch(mini_project / NEIGHBOUR_FILE)
    summary = _build(mini_project, discovery_only=True)

    口 = Step(step_num=3, name="Confirm the build really was a partial walk", purpose="a zero-file walk proves nothing")
    assert summary.files_scanned > 0, "the build must walk something, or the reaper never reaches its query"
    assert summary.files_skipped_mtime > 0, "the fast pass must actually skip the other file"
    assert set(_step_rows(db_path, NEIGHBOUR_FILE)) == {f"{NEIGHBOUR}::step-{n}" for n in (1, 2)}

    口 = Step(step_num=4, name="The skipped file keeps every step row", purpose="out of scope is not ∅")
    after = _step_rows(db_path, PIPELINE_FILE)
    assert after == before, (
        f"step rows on a file this build never opened were reconciled away. Missing: {sorted(set(before) - set(after))}"
    )


@workflow(
    purpose="Renumbering a workflow's steps leaves exactly the new numbering in the index and none of the old",
)
def test_renumbered_workflow_keeps_only_its_current_step_numbers(mini_project, db_path):
    口 = Step(step_num=1, name="Index a four-phase workflow", purpose="baseline numbering")
    _write_pipeline(mini_project, ["Collect", "Validate", "Transform", "Emit"])
    _build(mini_project, discovery_only=False)
    assert _step_ids(db_path) == {f"{PIPELINE}::step-{n}" for n in (1, 2, 3, 4)}

    口 = Step(step_num=2, name="Drop a phase and renumber the rest", purpose="simulate the refactor")
    _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    _build(mini_project, discovery_only=False)

    口 = Step(step_num=3, name="The index holds the new numbering only", purpose="the old number is gone, not buried")
    surviving = _step_ids(db_path)
    assert surviving == {f"{PIPELINE}::step-{n}" for n in (1, 2, 3)}, (
        f"expected exactly the new numbering; leftovers: {sorted(surviving - {f'{PIPELINE}::step-{n}' for n in (1, 2, 3)})}"
    )


@workflow(
    purpose="Step rows whose enclosing workflow node is gone are still reaped, through the file that declared them",
)
def test_step_rows_with_no_enclosing_workflow_are_reaped(mini_project, db_path):
    口 = Step(step_num=1, name="Index a workflow's steps", purpose="baseline")
    _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    _build(mini_project, discovery_only=False)

    口 = Step(step_num=2, name="Lose the workflow node the way a moved function loses it", purpose="orphan the rows")
    _drop_enclosing_workflow(db_path, PIPELINE_ENVELOPE)
    assert _parentless_step_ids(db_path) == {f"{PIPELINE}::step-{n}" for n in (1, 2, 3)}

    口 = Step(step_num=3, name="Take the function out of the source too", purpose="nothing declares the rows now")
    _write_module(mini_project, '@workflow(purpose="Do nothing in particular")\ndef unrelated():\n    return 0')

    口 = Step(step_num=4, name="Rebuild reaches them through their file", purpose="the parentless majority case")
    _build(mini_project, discovery_only=False)
    assert _step_ids(db_path) == set()


@workflow(
    purpose="Touching a file that carries orphaned step rows, with no edit at all, clears them on the next build",
)
def test_touching_a_carrier_file_clears_its_orphaned_step_rows(mini_project, db_path):
    口 = Step(step_num=1, name="Leave orphaned rows on a file the fast pass skips", purpose="the upgrade residue")
    src = _leave_orphaned_rows(mini_project, db_path)
    skipped = _build(mini_project, discovery_only=True)
    assert _step_ids(db_path) == {f"{PIPELINE}::step-{n}" for n in (1, 2, 3)}
    assert len(_leftover_notices(skipped)) == 1

    口 = Step(step_num=2, name="Touch the file — no content change", purpose="the remedy the notice advertises")
    _touch(src)

    口 = Step(step_num=3, name="Rebuild clears the rows and the notice", purpose="the remedy works end to end")
    cleared = _build(mini_project, discovery_only=True)
    assert _step_ids(db_path) == set()
    assert _leftover_notices(cleared) == []


# ---------------------------------------------------------------------------
# Tier 2 — subsystem tests
# ---------------------------------------------------------------------------


@workflow(
    purpose="A file whose scanner raised during the build keeps all of its step rows",
)
def test_step_rows_survive_when_the_scanner_raises(mini_project, db_path, monkeypatch):
    """A file that could not be parsed contributes no nodes, so it is out of scope — not read as ∅."""
    src = _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    _write_neighbour(mini_project)
    _build(mini_project, discovery_only=True)
    before = _step_rows(db_path, PIPELINE_FILE)
    _touch(src)
    _touch(mini_project / NEIGHBOUR_FILE)

    scan_module = builder.module_scanner.scan_module

    def _unparseable(path, *args, **kwargs):
        """Raise for the pipeline module only, so the build still walks the neighbour."""
        if Path(path).name == PIPELINE_FILE:
            raise SyntaxError("cannot parse")
        return scan_module(path, *args, **kwargs)

    monkeypatch.setattr(builder.module_scanner, "scan_module", _unparseable)
    summary = _build(mini_project, discovery_only=True)

    assert any("module_scanner failed" in w for w in summary.warnings)
    assert summary.files_scanned > 0, "the neighbour must be walked, or the reaper never reaches its query"
    assert _step_rows(db_path, PIPELINE_FILE) == before


@workflow(
    purpose="Removing one workflow's markers leaves every other workflow in the same file untouched",
)
def test_live_step_rows_in_the_same_file_survive(mini_project, db_path):
    _write_module(
        mini_project,
        _workflow_block("run_pipeline", "Run the pipeline", ["Collect", "Transform", "Emit"]),
        _workflow_block("run_report", "Run the report", ["Gather", "Render"]),
    )
    _build(mini_project, discovery_only=False)

    _write_module(
        mini_project,
        _workflow_block("run_pipeline", "Run the pipeline", []),
        _workflow_block("run_report", "Run the report", ["Gather", "Render"]),
    )
    _build(mini_project, discovery_only=False)

    assert _step_ids(db_path) == {f"{REPORT}::step-{n}" for n in (1, 2)}


@workflow(
    purpose="A build reports reaped step rows and leftover orphans in one line each, and says nothing when there are none",
)
def test_build_reports_reaped_and_leftover_step_rows_only_when_there_are_some(mini_project, db_path):
    _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    clean = _build(mini_project, discovery_only=False)
    assert _reap_notices(clean) == []
    assert _leftover_notices(clean) == []

    _write_pipeline(mini_project, ["Collect"])
    reaped = _build(mini_project, discovery_only=False)
    notices = _reap_notices(reaped)
    assert len(notices) == 1, f"expected exactly one line about reaped rows, got: {notices}"
    assert "2" in notices[0]
    assert _leftover_notices(reaped) == [], "nothing is left over — the rows were reaped, not stranded"


@workflow(
    purpose="The leftover notice names the rows orphaned, names the files carrying them, and gives the remedy",
)
def test_the_leftover_notice_says_what_is_wrong_and_what_to_do(mini_project, db_path):
    _leave_orphaned_rows(mini_project, db_path)
    summary = _build(mini_project, discovery_only=True)

    notices = _leftover_notices(summary)
    assert len(notices) == 1, f"expected exactly one line about leftovers, got: {notices}"
    notice = notices[0]
    assert "orphaned" in notice
    assert PIPELINE_FILE in notice, f"the notice must name the file to touch: {notice}"
    assert "touch" in notice and "modification time" in notice, f"the notice must state the remedy: {notice}"


# ---------------------------------------------------------------------------
# Tier 1 — internal guard
# ---------------------------------------------------------------------------


def test_failed_reaping_leaves_the_files_it_read_unstamped(mini_project, monkeypatch):
    """A build that could not finish reaping must be retried, not skipped past."""
    _write_pipeline(mini_project, ["Collect", "Transform", "Emit"])
    _build(mini_project, discovery_only=True)
    _touch(mini_project / PIPELINE_FILE)

    def _locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(builder.db, "get_step_node_ids_by_location_conn", _locked)
    failed = _build(mini_project, discovery_only=True)
    assert any("orphaned step reaping failed" in w for w in failed.warnings)

    monkeypatch.undo()
    retried = _build(mini_project, discovery_only=True)
    assert retried.files_skipped_mtime == 0, "the failed pass must be retried, not skipped past"
