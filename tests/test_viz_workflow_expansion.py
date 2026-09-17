"""Subsystem tests for AutoStep expansion in the viz workflow endpoints.

Tier 2 for the endpoint contract, Tier 1 for the narrower shape assertions.
These verify that ``/api/workflow/{id}/steps`` can return the transitive
AutoStep tree when asked, and that the default response is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow
from fastapi.testclient import TestClient

from axiom_graph.index import builder
from axiom_graph.viz import server
from axiom_graph.workflows.api import workflow_detail


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _setup_server(project_root: Path) -> TestClient:
    """Point the viz server module state at a project and return a test client."""
    server._PROJECT_ROOT = project_root
    server._DB_PATH = project_root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = []
    server._EXCLUDE_DIRS = []
    return TestClient(server.app)


def _build_delegating_workflow(project_root: Path) -> str:
    """Write a workflow whose AutoStep delegates to a task with its own steps.

    Returns the envelope node ID of the outer workflow.
    """
    _write(
        project_root / "tasks.py",
        """\
from axiom_annotations import task, Step


@task(purpose="Assemble the protein table")
def build_proteins():
    '''Inner task.'''
    _ = Step(step_num=1, name='locate cache', purpose='find the upstream cache')
    _ = Step(step_num=2, name='derive grid', purpose='derive the common dose grid')
""",
    )
    _write(
        project_root / "pipe.py",
        """\
from axiom_annotations import workflow, Step, AutoStep

from tasks import build_proteins


@workflow(purpose="Build plot data")
def build_plot_data():
    '''Outer workflow.'''
    _ = Step(step_num=1, name='load inputs', purpose='read the source frame')
    _ = AutoStep(step_num=2, name='bootstrap over the protein set')
    build_proteins()
    _ = Step(step_num=3, name='assemble', purpose='assemble the payload')
""",
    )
    builder.build(project_root, project_id="proj", discovery_only=False)
    return "proj::pipe::build_plot_data@workflow"


@workflow(purpose="Verify the workflow steps endpoint returns the transitive AutoStep tree when expand is requested")
def test_workflow_steps_expand_returns_nested_tree(tmp_path):
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get(f"/api/workflow/{envelope_id}/steps", params={"expand": "true"})

    assert resp.status_code == 200
    steps = resp.json()["steps"]
    assert [s["step_number"] for s in steps] == ["1", "2", "2.1", "2.2", "3"]
    assert [s["depth"] for s in steps] == [0, 0, 1, 1, 0]


def test_workflow_steps_default_returns_only_direct_children(tmp_path):
    """Without the expand flag the payload stays single-hop."""
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get(f"/api/workflow/{envelope_id}/steps")

    assert resp.status_code == 200
    steps = resp.json()["steps"]
    assert [s["step_number"] for s in steps] == ["1", "2", "3"]
    assert all("depth" not in s for s in steps)


def test_expanded_autostep_inherits_purpose_from_delegate_target(tmp_path):
    """An AutoStep carries no purpose of its own; expansion resolves the target's."""
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get(f"/api/workflow/{envelope_id}/steps", params={"expand": "true"})

    steps = {s["step_number"]: s for s in resp.json()["steps"]}
    auto = steps["2"]
    assert auto["is_auto"] is True
    assert auto["purpose"] == "Assemble the protein table"
    assert steps["1"]["purpose"] == "read the source frame"


def test_expanded_step_location_is_the_step_markers_own_file(tmp_path):
    """Steps pulled in from a delegate target report that target's file.

    The envelope lives in ``pipe.py``; steps 2.1 and 2.2 are declared inside
    ``build_proteins`` over in ``tasks.py``.  ``line`` indexes into
    ``location``, so a consumer pairing the line with the envelope's module
    resolves to an unrelated line in the wrong file.
    """
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get(f"/api/workflow/{envelope_id}/steps", params={"expand": "true"})

    steps = {s["step_number"]: s for s in resp.json()["steps"]}
    assert steps["1"]["location"] == "pipe.py"
    assert steps["2"]["location"] == "pipe.py"
    assert steps["2.1"]["location"] == "tasks.py"
    assert steps["2.2"]["location"] == "tasks.py"


def test_unexpanded_steps_also_carry_location(tmp_path):
    """The field is part of the base step shape, not an expansion-only extra."""
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    resp = client.get(f"/api/workflow/{envelope_id}/steps")

    steps = resp.json()["steps"]
    assert [s["location"] for s in steps] == ["pipe.py", "pipe.py", "pipe.py"]


@workflow(
    purpose="Every surface that builds a step payload names the same delegate target and inherits the same intent"
)
def test_all_step_surfaces_report_one_answer_for_a_delegating_step(tmp_path):
    envelope_id = _build_delegating_workflow(tmp_path)
    client = _setup_server(tmp_path)

    detail = workflow_detail(tmp_path, envelope_id)
    structured = next(row for row in detail.steps if row.step_num == "2")
    expanded = {
        s["step_number"]: s
        for s in client.get(f"/api/workflow/{envelope_id}/steps", params={"expand": "true"}).json()["steps"]
    }["2"]
    flat = {s["step_number"]: s for s in client.get(f"/api/workflow/{envelope_id}/steps").json()["steps"]}["2"]

    assert structured.target is not None
    assert structured.target.id == expanded["target"]["id"] == flat["target"]["id"]
    for field_name in ("purpose", "inputs", "outputs", "critical"):
        inherited = getattr(structured, field_name)
        assert inherited == (expanded[field_name] or "") == (flat[field_name] or "")
    assert structured.purpose == "Assemble the protein table"
