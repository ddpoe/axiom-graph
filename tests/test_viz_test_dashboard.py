"""Subsystem tests for the viz dashboard endpoints (graph.db-sourced).

Tier 2: @workflow(purpose=...) — meaningful subsystem behaviour, not a
stakeholder narrative. These verify the rewritten v2.0 viz endpoints source
their data from axiom-graph envelopes / step nodes (graph.db) and preserve
the frontend-facing JSON shapes.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow
from fastapi.testclient import TestClient

from axiom_graph.index import builder
from axiom_graph.viz import server


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _setup_server(
    project_root: Path,
    *,
    test_paths: list[str] | None = None,
) -> TestClient:
    """Point the viz server module state at a project and return a test client."""
    server._PROJECT_ROOT = project_root
    server._DB_PATH = project_root / ".axiom_graph" / "graph.db"
    server._DFLOW_DB_PATH = None
    server._TEST_PATHS = test_paths or []
    server._EXCLUDE_DIRS = []
    return TestClient(server.app)


def _build_project_with_test(mini_project: Path, db_path: Path) -> str:
    """Write a production module + plain test file, build, return the test node ID."""
    _write(mini_project / "mymod.py", "def target_func(): pass\n")
    _write(
        mini_project / "tests" / "test_mymod.py",
        """\
from mymod import target_func

def test_calls_target():
    \"\"\"Calls target_func to exercise it.\"\"\"
    target_func()
""",
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)
    return "proj::tests.test_mymod::test_calls_target"


def _build_project_with_decorated_workflow(mini_project: Path, db_path: Path) -> tuple[str, str]:
    """Write a module with a @workflow envelope + step. Returns (func_id, envelope_id)."""
    _write(
        mini_project / "mymod.py",
        """\
from axiom_annotations import workflow, Step


@workflow(purpose="demo workflow")
def run_demo():
    '''Demo.'''
    _ = Step(step_num=1, name='step one', purpose='do thing')
""",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    func_id = "proj::mymod::run_demo"
    envelope_id = f"{func_id}@workflow"
    return func_id, envelope_id


@workflow(purpose="GET /api/tests returns axiom-graph-native test items with tier, validates, result fields")
def test_unified_tests_endpoint(mini_project, db_path):
    """Build a project with a test, then hit /api/tests and verify the
    response shape matches the axiom-graph-native format.
    """
    test_node_id = _build_project_with_test(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get("/api/tests")
    assert resp.status_code == 200

    data = resp.json()
    assert data["available"] is True
    assert len(data["tests"]) >= 1

    test_item = next((t for t in data["tests"] if t["cortex_id"] == test_node_id), None)
    assert test_item is not None, f"Test {test_node_id} not in response"

    assert test_item["name"] == "test_calls_target"
    assert test_item["tier"] == "T1"  # no @workflow envelope on the test
    assert "validates_count" in test_item
    assert "validates" in test_item
    assert isinstance(test_item["validates"], list)
    assert "result" in test_item
    assert "module" in test_item


@workflow(purpose="GET /api/tests returns validates edges for tests that call production code")
def test_unified_endpoint_includes_validates_edges(mini_project, db_path):
    """A test that calls a production function should have validates edges
    populated in the /api/tests response.
    """
    test_node_id = _build_project_with_test(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get("/api/tests")
    data = resp.json()
    test_item = next((t for t in data["tests"] if t["cortex_id"] == test_node_id), None)
    assert test_item is not None

    assert test_item["validates_count"] >= 1
    assert any("target_func" in v for v in test_item["validates"])


@workflow(purpose="GET /api/test-detail/{cortex_id} returns detail for an axiom-graph string ID")
def test_detail_by_cortex_id(mini_project, db_path):
    """The detail endpoint must accept an axiom-graph node ID and return
    test metadata including validates edges and fixtures.
    """
    test_node_id = _build_project_with_test(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get(f"/api/test-detail/{test_node_id}")
    assert resp.status_code == 200

    data = resp.json()
    assert data["name"] == "test_calls_target"
    assert data["cortex_id"] == test_node_id
    assert "validates" in data
    assert "tier" in data
    assert "location" in data


@workflow(purpose="GET /api/t1_tests returns empty list (deprecated endpoint)")
def test_t1_tests_deprecated(mini_project, db_path):
    """The deprecated /api/t1_tests endpoint should return an empty list."""
    _build_project_with_test(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get("/api/t1_tests")
    assert resp.status_code == 200
    assert resp.json()["t1_tests"] == []


@workflow(purpose="GET /api/workflows returns envelope data sourced from graph.db with no .dflow/workflow.db")
def test_workflows_endpoint_sources_from_graph_db(mini_project, db_path):
    """The /api/workflows endpoint must return envelope data from graph.db
    without any .dflow/workflow.db file present. Proves US-5 acceptance:
    "every viz endpoint that used to read .dflow/workflow.db now reads
    envelopes and step nodes from graph.db".
    """
    func_id, envelope_id = _build_project_with_decorated_workflow(mini_project, db_path)
    assert not (mini_project / ".dflow" / "workflow.db").exists()
    client = _setup_server(mini_project)

    resp = client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    names = [w["name"] for w in data["workflows"]]
    assert "run_demo" in names, f"expected run_demo in {names}"

    wf = next(w for w in data["workflows"] if w["name"] == "run_demo")
    # Envelope id is used as workflow id; cortex_node_id points at annotated func.
    assert wf["id"] == envelope_id
    assert wf["cortex_node_id"] == func_id
    assert wf["purpose"] == "demo workflow"
    assert wf["step_count"] >= 1


@workflow(purpose="GET /api/workflow/{envelope_id}/steps returns steps from graph.db")
def test_workflow_steps_from_envelope_id(mini_project, db_path):
    """Walk the composes edge to retrieve the steps list."""
    _, envelope_id = _build_project_with_decorated_workflow(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get(f"/api/workflow/{envelope_id}/steps")
    assert resp.status_code == 200
    data = resp.json()
    assert data["func"]["name"] == "run_demo"
    assert data["func"]["purpose"] == "demo workflow"
    assert len(data["steps"]) >= 1
    step = data["steps"][0]
    assert step["step_number"] == "1"
    assert step["name"] == "step one"


@workflow(purpose="Tier classification is T2 when envelope annotates a test with no steps")
def test_tier_classification_t2_from_envelope(mini_project, db_path):
    """A test function decorated with @workflow(purpose=...) and no Step() calls
    should be classified as T2 — sourced from graph.db envelopes.
    """
    _write(mini_project / "mymod.py", "def target_func(): pass\n")
    _write(
        mini_project / "tests" / "test_mymod.py",
        """\
from axiom_annotations import workflow
from mymod import target_func


@workflow(purpose="exercise target_func")
def test_calls_target():
    \"\"\"Calls target_func to exercise it.\"\"\"
    target_func()
""",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    test_node_id = "proj::tests.test_mymod::test_calls_target"

    client = _setup_server(mini_project)
    resp = client.get("/api/tests")
    data = resp.json()
    test_item = next((t for t in data["tests"] if t["cortex_id"] == test_node_id), None)
    assert test_item is not None, f"expected {test_node_id} in {[t['cortex_id'] for t in data['tests']]}"
    assert test_item["tier"] == "T2"
    assert test_item["has_workflow"] is True
    assert test_item["step_count"] == 0


@workflow(purpose="Tier classification is T3 when envelope annotates a test with step markers")
def test_tier_classification_t3_from_envelope(mini_project, db_path):
    """A test function with @workflow + Step() calls should be classified as T3."""
    _write(mini_project / "mymod.py", "def target_func(): pass\n")
    _write(
        mini_project / "tests" / "test_mymod.py",
        """\
from axiom_annotations import workflow, Step
from mymod import target_func


@workflow(purpose="exercise target_func")
def test_calls_target():
    \"\"\"Walks one step.\"\"\"
    _ = Step(step_num=1, name='call target', purpose='run it')
    target_func()
""",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)
    test_node_id = "proj::tests.test_mymod::test_calls_target"

    client = _setup_server(mini_project)
    resp = client.get("/api/tests")
    data = resp.json()
    test_item = next((t for t in data["tests"] if t["cortex_id"] == test_node_id), None)
    assert test_item is not None
    assert test_item["tier"] == "T3"
    assert test_item["step_count"] >= 1


@workflow(purpose="/api/tests excludes module-level nodes and all items have non-null line_start")
def test_no_module_nodes_in_tests_endpoint(mini_project, db_path):
    """Module-level composite_process nodes should not appear in /api/tests.
    Every returned test item must have a non-null line_start.
    """
    _build_project_with_test(mini_project, db_path)
    client = _setup_server(mini_project)

    resp = client.get("/api/tests")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True

    for item in data["tests"]:
        assert item["line_start"] is not None, (
            f"Test {item['cortex_id']} has null line_start — module-level node leaked into /api/tests"
        )
        assert "::" in item["cortex_id"].split("::", 1)[-1], (
            f"Test {item['cortex_id']} appears to be a module-level node"
        )


@workflow(purpose="/api/source returns raw file content for a path under the project root")
def test_api_source_returns_file_content(tmp_path):
    """A project-relative path resolves to its file body and a line count."""
    _write(tmp_path / "pkg" / "mod.py", "def foo():\n    return 1\n")
    client = _setup_server(tmp_path)

    resp = client.get("/api/source", params={"path": "pkg/mod.py"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "def foo()" in body["content"]
    assert body["lines"] == 3


def test_api_source_rejects_path_traversal(tmp_path):
    """The traversal guard must block paths that resolve outside the project root."""
    client = _setup_server(tmp_path)

    resp = client.get("/api/source", params={"path": "../../etc/passwd"})

    assert resp.status_code == 403
