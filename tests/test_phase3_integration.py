"""Phase 3 Tier 3 integration tests.

End-to-end tests that exercise the full build + check + workflow_detail
path without the legacy ``.dflow/workflow.db`` cross-DB reader.

Mapped to the Phase 3 pitch test-plan (cycle manifest
``axiom_graph::docs.pev.cycles.pev-2026-04-21-phase3-axiom-annotations``).
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.workflows.api import workflow_detail, workflow_list
from axiom_graph.index import builder, db


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build(project_root: Path, discovery_only: bool = False) -> None:
    """Run axiom-graph build with embeddings skipped."""
    os.environ["AXIOM_GRAPH_SKIP_EMBEDDINGS"] = "1"
    builder.build(project_root, project_id="proj", discovery_only=discovery_only)


def _status(db_path: Path, node_id: str) -> tuple[str, str]:
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert row is not None, f"node {node_id} not found"
        return row["own_status"], row["link_status"]


def _edges(db_path: Path, edge_type: str) -> list[tuple[str, str]]:
    with db._connect(db_path) as conn:
        rows = conn.execute("SELECT from_id, to_id FROM edges WHERE edge_type = ?", (edge_type,)).fetchall()
        return [(r["from_id"], r["to_id"]) for r in rows]


@workflow(purpose="Tier 3 integration: build + check + workflow_detail end-to-end")
def test_end_to_end_build_check_workflow_detail(tmp_path):
    """Exercise the full Phase 3 pipeline against a fresh fixture project.

    Two modules, one ``@workflow``, one ``@task``, three steps total (with
    one AutoStep that delegates to the task).  Verify graph shape, Pass A'
    + Pass B' staleness behaviour, and that ``workflow_detail`` returns a
    populated result without touching ``.dflow/workflow.db``.
    """
    口 = Step(
        step_num=1,
        name="Write fixture",
        purpose="Build a minimal two-module project with one workflow and one task",
    )
    _write(
        tmp_path / "tasks.py",
        """
from axiom_annotations import task, Step

@task(purpose="inner task with two steps")
def inner():
    口 = Step(step_num=1, name="sub-a", purpose="inner step a")
    口 = Step(step_num=2, name="sub-b", purpose="inner step b")
""".lstrip(),
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, AutoStep
from tasks import inner

@workflow(purpose="outer workflow delegates to inner task")
def outer():
    '''Initial outer docstring.'''
    口 = AutoStep(step_num=1, name="call-inner")
    inner()
""".lstrip(),
    )

    口 = Step(
        step_num=2,
        name="Initial build",
        purpose="Run axiom-graph build (full, not discovery-only) to populate hashes",
    )
    _build(tmp_path, discovery_only=False)
    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    assert ag_db.exists(), "axiom-graph DB should be created by build"

    口 = Step(
        step_num=3,
        name="Assert graph shape",
        purpose="Both envelopes exist; three step nodes exist; composes/annotates/delegates_to edges present",
    )
    env_outer = "proj::pipe::outer@workflow"
    env_inner = "proj::tasks::inner@workflow"
    func_outer = "proj::pipe::outer"
    func_inner = "proj::tasks::inner"
    step_outer_1 = "proj::pipe::outer::step-1"
    step_inner_1 = "proj::tasks::inner::step-1"
    step_inner_2 = "proj::tasks::inner::step-2"

    all_ids = {n.id for n in db.all_nodes(ag_db)}
    for nid in (
        env_outer,
        env_inner,
        func_outer,
        func_inner,
        step_outer_1,
        step_inner_1,
        step_inner_2,
    ):
        assert nid in all_ids, f"expected node {nid} in index"

    composes = set(_edges(ag_db, "composes"))
    assert (env_outer, step_outer_1) in composes
    assert (env_inner, step_inner_1) in composes
    assert (env_inner, step_inner_2) in composes

    annotates = set(_edges(ag_db, "annotates"))
    assert (env_outer, func_outer) in annotates
    assert (env_inner, func_inner) in annotates

    delegates = set(_edges(ag_db, "delegates_to"))
    assert (step_outer_1, func_inner) in delegates

    口 = Step(
        step_num=4,
        name="Baseline staleness",
        purpose="Run record_staleness to establish a VERIFIED baseline for both envelopes",
    )
    from axiom_graph.index import staleness as stalemod

    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    口 = Step(
        step_num=5,
        name="Edit task body",
        purpose="Change inner()'s runtime behaviour (content, not just docstring)",
    )
    _write(
        tmp_path / "tasks.py",
        """
from axiom_annotations import task, Step

@task(purpose="inner task with two steps")
def inner():
    口 = Step(step_num=1, name="sub-a", purpose="inner step a")
    口 = Step(step_num=2, name="sub-b", purpose="inner step b")
    return 99
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass: history row from first pass is now visible to SQL Pass A.
    nodes = db.all_nodes(ag_db)
    result = stalemod.record_staleness(ag_db, tmp_path, nodes)

    口 = Step(
        step_num=6,
        name="Assert Pass B' flipped outer envelope",
        purpose="outer envelope LINKED_STALE via inner task function (transitive through AutoStep)",
    )
    _, outer_link = _status(ag_db, env_outer)
    assert outer_link == "LINKED_STALE", (
        f"outer envelope should be LINKED_STALE after task body change, got {outer_link}"
    )
    # via list should reference the inner task function (reached via Pass B' walk).
    outer_via = result.get(env_outer, (None, None, []))[2]
    assert func_inner in outer_via, f"via hint should include inner task {func_inner!r}, got {outer_via!r}"

    口 = Step(
        step_num=7,
        name="Edit workflow docstring only",
        purpose="Pure DESC change on outer() — tests Pass A' DESC_ONLY propagation",
    )
    _write(
        tmp_path / "pipe.py",
        """
from axiom_annotations import workflow, AutoStep
from tasks import inner

@workflow(purpose="outer workflow delegates to inner task")
def outer():
    '''Completely revised outer docstring — pure DESC change.'''
    口 = AutoStep(step_num=1, name="call-inner")
    inner()
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    nodes = db.all_nodes(ag_db)
    result2 = stalemod.record_staleness(ag_db, tmp_path, nodes)

    口 = Step(
        step_num=8,
        name="Assert Pass A' flipped outer envelope via outer function",
        purpose="outer envelope LINKED_STALE with via hint = outer function (DESC_UPDATED)",
    )
    _, outer_link2 = _status(ag_db, env_outer)
    assert outer_link2 == "LINKED_STALE"
    outer_via2 = result2.get(env_outer, (None, None, []))[2]
    assert func_outer in outer_via2, (
        f"via hint should include outer function {func_outer!r} after docstring change, got {outer_via2!r}"
    )

    口 = Step(
        step_num=9,
        name="workflow_detail returns populated result",
        purpose="workflow_detail reads only axiom-graph DB; .dflow/workflow.db must not exist",
    )
    assert not (tmp_path / ".dflow" / "workflow.db").exists(), (
        "fixture must not contain .dflow/workflow.db — scanner should not read or create it"
    )

    detail = workflow_detail(tmp_path, env_outer)
    assert detail is not None, "workflow_detail should not be None for a known envelope"
    assert detail.role == "workflow"
    # Expanded step sequence: outer AutoStep "1" first, then inner steps renumbered as 1.1 / 1.2.
    rendered = [s.step_num for s in detail.steps]
    assert rendered[0] == "1", f"first rendered step should be outer AutoStep 1, got {rendered!r}"
    assert "1.1" in rendered and "1.2" in rendered, f"expected expanded minor steps 1.1 and 1.2, got {rendered!r}"
    # AutoStep delegates to inner task by name.
    auto = next(s for s in detail.steps if s.step_num == "1")
    assert auto.is_auto is True
    assert auto.delegates_to_name == "inner"
    # Confirm no .dflow/ directory was created as a side-effect.
    assert not (tmp_path / ".dflow").exists(), "scanner must not create .dflow/ directory as a side-effect"


@workflow(purpose="Tier 3 integration: cross-DB reader fully removed")
def test_cross_db_reader_removed(tmp_path):
    """Verify the build + API path no longer depends on .dflow/workflow.db.

    Fixture has no ``.dflow/`` directory at all.  Build must succeed,
    ``delegates_to`` edges must be present (AST-derived), and both public
    API entrypoints must return non-None results.  Also asserts the
    functional scope of the removal: ``api.py`` and ``mcp/dflow.py``
    contain zero ``workflow.db`` references.
    """
    口 = Step(
        step_num=1,
        name="Write fixture with no .dflow/",
        purpose="Single-module project with a workflow that AutoStep-delegates to a task",
    )
    _write(
        tmp_path / "worker.py",
        """
from axiom_annotations import task

@task(purpose="worker task")
def work():
    return "done"
""".lstrip(),
    )
    _write(
        tmp_path / "pipeline.py",
        """
from axiom_annotations import workflow, AutoStep
from worker import work

@workflow(purpose="pipeline delegates to work")
def pipeline():
    口 = AutoStep(step_num=1, name="call-work")
    work()
""".lstrip(),
    )
    assert not (tmp_path / ".dflow").exists(), "fixture precondition: no .dflow/ directory"

    口 = Step(
        step_num=2,
        name="Build succeeds with no .dflow/",
        purpose="axiom-graph build must not warn or fail when workflow.db is absent",
    )
    _build(tmp_path, discovery_only=False)
    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    assert ag_db.exists()
    nodes = db.all_nodes(ag_db)
    assert len(nodes) > 0, "build must produce a non-empty index"

    口 = Step(
        step_num=3,
        name="delegates_to edges derived from AST",
        purpose="AutoStep-to-task delegation must be captured without reading workflow.db",
    )
    delegates = set(_edges(ag_db, "delegates_to"))
    step_id = "proj::pipeline::pipeline::step-1"
    task_id = "proj::worker::work"
    assert (step_id, task_id) in delegates, f"expected delegates_to edge {step_id!r} -> {task_id!r}, got {delegates!r}"

    口 = Step(
        step_num=4,
        name="Public APIs return populated results",
        purpose="workflow_detail and workflow_list both work without a .dflow/ directory",
    )
    rows = workflow_list(tmp_path)
    assert rows, "workflow_list should return at least the pipeline + work envelopes"
    names = {r.name for r in rows}
    assert "pipeline" in names and "work" in names

    detail = workflow_detail(tmp_path, "proj::pipeline::pipeline@workflow")
    assert detail is not None
    assert detail.role == "workflow"
    assert any(s.step_num == "1" for s in detail.steps)

    口 = Step(
        step_num=5,
        name=".dflow/ directory not created",
        purpose="Scanner must not create a .dflow/ directory as a side-effect",
    )
    assert not (tmp_path / ".dflow").exists()

    口 = Step(
        step_num=6,
        name="workflows/api.py and workflows/mcp_tools.py contain zero workflow.db references",
        purpose="Functional scope of cross-DB reader removal for this cycle's targets",
    )
    repo_root = Path(__file__).resolve().parent.parent
    api_py = repo_root / "axiom_graph" / "workflows" / "api.py"
    mcp_dflow_py = repo_root / "axiom_graph" / "workflows" / "mcp_tools.py"
    assert api_py.exists(), f"expected {api_py} to exist"
    assert mcp_dflow_py.exists(), f"expected {mcp_dflow_py} to exist"
    api_text = api_py.read_text(encoding="utf-8")
    mcp_text = mcp_dflow_py.read_text(encoding="utf-8")
    assert "workflow.db" not in api_text, (
        "axiom_graph/workflows/api.py must contain zero 'workflow.db' references after Phase 3"
    )
    assert "workflow.db" not in mcp_text, (
        "axiom_graph/workflows/mcp_tools.py must contain zero 'workflow.db' references after Phase 3"
    )
