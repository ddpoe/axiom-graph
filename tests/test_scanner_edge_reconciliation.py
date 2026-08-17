"""Lifecycle of scanner-derived ``delegates_to`` edges across rebuilds.

Source files are the source of truth for the workflow delegate links the
scanners derive from them.  An edge's identity includes its target, so
retargeting a step's next call mints a new edge row — the build must retire
the row the source no longer justifies, for every source it actually walked
this build, and leave every other edge alone.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
from axiom_annotations import Step, workflow

from axiom_graph.db import edges as db_edges
from axiom_graph.index import builder, db
from axiom_graph.models import AxiomEdge
from axiom_graph.scanners.xstate_scanner import HAS_TREE_SITTER

STEP_ID = "proj::mod::run_pipeline::step-1"
FIRST_HELPER = "proj::mod::first_helper"
SECOND_HELPER = "proj::mod::second_helper"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pipeline_source(next_call: str) -> str:
    """Return a module whose single AutoStep is followed by *next_call*."""
    return (
        '"""Pipeline module."""\n\n'
        "from axiom_annotations import AutoStep, task, workflow\n\n\n"
        '@task(purpose="Do the first thing")\n'
        "def first_helper():\n"
        "    return 1\n\n\n"
        '@task(purpose="Do the second thing")\n'
        "def second_helper():\n"
        "    return 2\n\n\n"
        '@workflow(purpose="Run the pipeline")\n'
        "def run_pipeline():\n"
        '    口 = AutoStep(step_num=1, name="Delegate to a helper")\n'
        f"    {next_call}\n"
    )


def _write_pipeline(project_root: Path, next_call: str) -> Path:
    """Write the pipeline module into *project_root* and return its path."""
    src = project_root / "mod.py"
    src.write_text(_pipeline_source(next_call), encoding="utf-8")
    return src


def _outbound_delegates(db_path: Path, from_id: str) -> set[str]:
    """Return the set of ``delegates_to`` targets for *from_id*."""
    with db._connect(db_path) as conn:
        return db_edges.get_outbound_edge_targets_conn(conn, from_id, "delegates_to")


def _inject_edge(db_path: Path, from_id: str, edge_type: str, to_id: str) -> None:
    """Insert an edge row directly, simulating one left behind by an earlier build."""
    with db._connect(db_path) as conn:
        db_edges.upsert_edge_conn(
            conn,
            AxiomEdge(
                id=f"{from_id}::{edge_type}::{to_id}",
                edge_type=edge_type,
                from_id=from_id,
                to_id=to_id,
            ),
        )


def _shift_mtimes_forward(root: Path, seconds: float = 5.0) -> None:
    """Move every source file's mtime forward without touching a byte."""
    future = time.time() + seconds
    for path in root.rglob("*"):
        if path.is_file() and ".axiom_graph" not in path.parts:
            os.utime(path, (future, future))


def _history_rows_for(db_path: Path, node_id: str, change_type: str) -> list[dict]:
    """Return history rows matching *change_type* for the given node."""
    rows = db.get_history(db_path, node_id, limit=100)
    return [r for r in rows if r["change_type"] == change_type]


def _delegate_notices(stats: dict) -> list[str]:
    """Return the build warnings that talk about leftover delegate links."""
    return [w for w in stats["warnings"] if "delegate link" in w]


# ---------------------------------------------------------------------------
# Tier 3 — user-story-level e2e scenario
# ---------------------------------------------------------------------------


@workflow(
    purpose="Retargeting a step's delegate call and rebuilding leaves exactly one delegate link, pointing at the new target",
)
def test_retargeted_step_keeps_only_its_current_delegate(mini_project, db_path):
    口 = Step(step_num=1, name="Write a workflow whose AutoStep calls the first helper", purpose="setup")
    _write_pipeline(mini_project, "first_helper()")

    口 = Step(step_num=2, name="Initial build links the step to that helper", purpose="baseline state")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert _outbound_delegates(db_path, STEP_ID) == {FIRST_HELPER}

    口 = Step(step_num=3, name="Retarget the step at the second helper", purpose="simulate a refactor")
    _write_pipeline(mini_project, "second_helper()")

    口 = Step(step_num=4, name="Rebuild retires the superseded link", purpose="exercise the reconciler")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    surviving = _outbound_delegates(db_path, STEP_ID)
    assert surviving == {SECOND_HELPER}, f"expected only the current target, got: {surviving}"

    口 = Step(step_num=5, name="The removal is recorded in the step's history", purpose="verify history emission")
    removals = [json.loads(r["meta"] or "{}") for r in _history_rows_for(db_path, STEP_ID, "LINK_REMOVED")]
    matching = [
        m
        for m in removals
        if m.get("target") == FIRST_HELPER
        and m.get("edge_type") == "delegates_to"
        and m.get("actor") == "build:reconcile"
    ]
    assert matching, f"expected a LINK_REMOVED row for the superseded target; got: {removals}"


# ---------------------------------------------------------------------------
# Tier 2 — subsystem tests
# ---------------------------------------------------------------------------


@workflow(
    purpose="A walked step that resolves no delegate at all has its previous delegate link removed",
)
def test_step_that_resolves_no_delegate_loses_its_link(mini_project, db_path):
    """A walked source intending nothing is reconciled to nothing, not left alone."""
    _write_pipeline(mini_project, "first_helper()")
    builder.build(mini_project, project_id="proj", discovery_only=False)
    assert _outbound_delegates(db_path, STEP_ID) == {FIRST_HELPER}

    # The step survives (same step_num) but its next call no longer resolves
    # to an indexed function, so this build intends no delegate edge at all.
    _write_pipeline(mini_project, 'print("no delegation here")')
    builder.build(mini_project, project_id="proj", discovery_only=False)

    assert _outbound_delegates(db_path, STEP_ID) == set()


@workflow(
    purpose="A source the build skipped via the mtime fast-pass keeps its delegate links untouched",
)
def test_delegate_links_survive_on_a_source_the_build_did_not_walk(mini_project, db_path):
    """Sources absent from a build's output are out of scope, never treated as intending nothing."""
    _write_pipeline(mini_project, "first_helper()")
    builder.build(mini_project, project_id="proj", discovery_only=True)
    assert _outbound_delegates(db_path, STEP_ID) == {FIRST_HELPER}

    _inject_edge(db_path, STEP_ID, "delegates_to", SECOND_HELPER)

    # Rebuild WITHOUT touching the file — the mtime fast-pass skips it, so the
    # step contributes nothing to this build's output and is out of scope.
    stats = builder.build(mini_project, project_id="proj", discovery_only=True)
    assert stats["files_skipped_mtime"] > 0, "the fast-pass must actually skip the file for this to mean anything"

    surviving = _outbound_delegates(db_path, STEP_ID)
    assert surviving == {FIRST_HELPER, SECOND_HELPER}, (
        f"links on an unwalked source were reconciled away. Links now: {surviving}"
    )


@workflow(
    purpose="Reconciliation is scoped to delegate links; other edge types leaving the same source survive",
)
def test_non_delegate_edges_on_a_reconciled_source_are_preserved(mini_project, db_path):
    _write_pipeline(mini_project, "first_helper()")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    for edge_type, target in (("validates", FIRST_HELPER), ("composes", SECOND_HELPER)):
        _inject_edge(db_path, STEP_ID, edge_type, target)

    builder.build(mini_project, project_id="proj", discovery_only=False)

    assert _outbound_delegates(db_path, STEP_ID) == {FIRST_HELPER}
    with db._connect(db_path) as conn:
        survivors = conn.execute(
            "SELECT edge_type, to_id FROM edges WHERE from_id = ? AND edge_type IN ('validates', 'composes')",
            (STEP_ID,),
        ).fetchall()
    pairs = {(r["edge_type"], r["to_id"]) for r in survivors}
    assert ("validates", FIRST_HELPER) in pairs
    assert ("composes", SECOND_HELPER) in pairs


@pytest.mark.skipif(not HAS_TREE_SITTER, reason="tree-sitter not installed (pip install axiom-graph[js])")
@workflow(
    purpose="A state that legitimately delegates to several transition targets keeps every one of them across a rebuild",
)
def test_fan_out_delegate_links_all_survive_a_rebuild(mini_project, db_path):
    (mini_project / "axiom-graph.toml").write_text(
        '[axiom_graph]\nproject_id = "proj"\n\n[axiom_graph.scan]\njs_paths = ["web/**/*.ts"]\n',
        encoding="utf-8",
    )
    web = mini_project / "web"
    web.mkdir()
    (web / "machine.ts").write_text(
        "import { createMachine } from 'xstate';\n"
        "\n"
        "export const m = createMachine({\n"
        "  id: 'lights',\n"
        "  initial: 'green',\n"
        "  states: {\n"
        "    green: { on: { TIMER: 'yellow', PANIC: 'red', RESET: 'green' } },\n"
        "    yellow: {},\n"
        "    red: {},\n"
        "  },\n"
        "});\n",
        encoding="utf-8",
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)

    with db._connect(db_path) as conn:
        state_ids = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE subtype = 'state'").fetchall()]
    fan_out = {sid: _outbound_delegates(db_path, sid) for sid in state_ids}
    fan_out = {sid: targets for sid, targets in fan_out.items() if len(targets) > 1}
    assert fan_out, "expected at least one state with several transition targets"

    builder.build(mini_project, project_id="proj", discovery_only=False)

    for sid, targets in fan_out.items():
        assert _outbound_delegates(db_path, sid) == targets, f"fan-out collapsed on {sid}"


@workflow(
    purpose="A build whose scanner-edge reconciliation failed leaves the files it read out of the fast-pass so the next build retries",
)
def test_failed_reconciliation_leaves_the_files_it_read_unstamped(mini_project, monkeypatch):
    _write_pipeline(mini_project, "first_helper()")
    builder.build(mini_project, project_id="proj", discovery_only=True)
    _shift_mtimes_forward(mini_project)

    def _locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(builder.db, "get_edge_source_ids_conn", _locked)
    failed = builder.build(mini_project, project_id="proj", discovery_only=True)
    assert any("scanner-edge reconciliation failed" in w for w in failed["warnings"])
    assert failed["file_mtimes_stamped"] == 0

    monkeypatch.undo()
    retried = builder.build(mini_project, project_id="proj", discovery_only=True)
    assert retried["files_skipped_mtime"] == 0, "the failed pass must be retried, not skipped past"
    assert retried["file_mtimes_stamped"] > 0


@workflow(
    purpose="A build reports leftover delegate links in one line, and says nothing at all when there are none",
)
def test_build_reports_leftover_delegate_links_only_when_some_remain(mini_project, db_path):
    _write_pipeline(mini_project, "first_helper()")
    clean = builder.build(mini_project, project_id="proj", discovery_only=True)
    assert _delegate_notices(clean) == []
    assert clean["surplus_delegate_edges"] == 0

    # A leftover on a source this build cannot reach — the mtime fast-pass
    # skips the file, so the reconciler never gets to it.
    _inject_edge(db_path, STEP_ID, "delegates_to", SECOND_HELPER)

    noticed = builder.build(mini_project, project_id="proj", discovery_only=True)
    assert noticed["surplus_delegate_edges"] == 1
    notices = _delegate_notices(noticed)
    assert len(notices) == 1, f"expected exactly one line about leftovers, got: {notices}"
    assert "1" in notices[0]
