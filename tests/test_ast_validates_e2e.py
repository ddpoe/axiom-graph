"""E2E test: AST call graph validates edges with no manual annotation.

Tier 3: @workflow + Step() — stakeholder-readable user story. Demonstrates
the full feature from source files on disk → axiom-graph build → queryable validates
edges, with no covers= or dFlow test annotations anywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from axiom_annotations import workflow, Step

from axiom_graph.index import builder, db

FIXTURES = Path(__file__).parent / "fixtures" / "ast_validates_no_annotation"


@workflow(
    purpose=(
        "Verify that axiom-graph build auto-generates validates edges from the test "
        "call graph with no manual annotation — no covers=, no dFlow decorators "
        "on the test, just plain imports and function calls."
    ),
)
def test_ast_validates_edges_no_annotation_required(mini_project, db_path):
    口 = Step(
        step_num=1,
        name="Write production module",
        purpose="Create a .py file with two indexed functions",
        outputs="mymod.py with process_data() and validate_schema()",
    )
    shutil.copy(FIXTURES / "mymod.py", mini_project / "mymod.py")

    口 = Step(
        step_num=2,
        name="Write test file with no annotation",
        purpose="Create a test_*.py that imports and calls production functions — no covers=, no @workflow",
        inputs="mymod.py production functions",
        outputs="test_mymod.py with two plain pytest test functions",
    )
    shutil.copy(FIXTURES / "test_mymod.py", mini_project / "test_mymod.py")

    口 = Step(
        step_num=3,
        name="Run axiom-graph build",
        purpose="Scan both files, upsert all nodes and edges including auto-generated validates edges",
        outputs="Populated axiom-graph DB with validates edges",
    )
    result = builder.build(mini_project, project_id="proj", discovery_only=False)
    assert not result["warnings"], f"Build warnings: {result['warnings']}"

    口 = Step(
        step_num=4,
        name="Assert validates edges exist",
        purpose="Confirm each test function has a validates edge to the production function it calls",
        inputs="axiom-graph DB",
        outputs="Two validates edges: test_process_data → process_data, test_validate_schema → validate_schema",
    )
    validates = [e for e in db.all_edges(db_path) if e.edge_type == "validates"]
    to_ids = {e.to_id for e in validates}

    assert "proj::mymod::process_data" in to_ids, (
        "test_process_data_returns_input must have a validates edge to process_data"
    )
    assert "proj::mymod::validate_schema" in to_ids, (
        "test_validate_schema_returns_true must have a validates edge to validate_schema"
    )

    口 = Step(
        step_num=5,
        name="Assert edge from_ids are correct test functions",
        purpose="Confirm edges originate from the right test functions, not from the module or some other node",
    )
    from_ids = {e.from_id for e in validates}
    assert "proj::test_mymod::test_process_data_returns_input" in from_ids
    assert "proj::test_mymod::test_validate_schema_returns_true" in from_ids
