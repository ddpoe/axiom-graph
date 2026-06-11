"""Subsystem tests for builder validates-edge filtering.

Tier 2: @workflow(purpose=...) — meaningful subsystem behaviour, not a
stakeholder narrative.
"""

from __future__ import annotations

from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.index import builder, db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _validates_edges(db_path: Path):
    return [e for e in db.all_edges(db_path) if e.edge_type == "validates"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@workflow(purpose="Verify that the builder writes validates edges to the DB when both nodes exist")
def test_builder_writes_validates_edge_when_target_exists(mini_project, db_path):
    """Full build of a two-file project: one production module, one test file.
    After build the validates edge must appear in the edges table.
    """
    _write(mini_project / "mymod.py", "def target_func(): pass\n")
    _write(
        mini_project / "test_foo.py",
        """\
from mymod import target_func

def test_calls_target():
    target_func()
""",
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)

    edges = _validates_edges(db_path)
    assert any(
        e.from_id == "proj::test_foo::test_calls_target" and e.to_id == "proj::mymod::target_func" for e in edges
    ), f"Expected validates edge not found. edges={[e.id for e in edges]}"


@workflow(purpose="Verify that the builder silently skips validates edges whose target is not an indexed node")
def test_builder_silently_skips_unresolved_validates_target(mini_project, db_path):
    """Test file calls a name that resolves to a module binding but not an indexed function.
    The builder must skip the edge without raising a warning.
    """
    # mymod exports a constant, not a function — won't be in the node index
    _write(mini_project / "mymod.py", "MY_CONST = 42\n")
    _write(
        mini_project / "test_foo.py",
        """\
from mymod import MY_CONST

def test_uses_const():
    assert MY_CONST == 42
""",
    )

    result = builder.build(mini_project, project_id="proj", discovery_only=False)

    assert not _validates_edges(db_path), "No validates edges expected for non-function targets"
    assert not any("validates" in w for w in result["warnings"]), (
        "Builder must not warn on unresolved validates targets"
    )


@workflow(purpose="Verify that existing depends_on edges are unaffected by the validates edge changes")
def test_builder_depends_on_edges_unaffected(mini_project, db_path):
    """Ensures the name_map enrichment (tuple instead of str) does not break
    the existing depends_on edges between modules.
    """
    _write(mini_project / "mymod.py", "def func(): pass\n")
    _write(
        mini_project / "consumer.py",
        """\
from mymod import func

def do_work():
    func()
""",
    )

    builder.build(mini_project, project_id="proj", discovery_only=False)

    depends = [e for e in db.all_edges(db_path) if e.edge_type == "depends_on"]
    assert any(e.from_id == "proj::consumer" and e.to_id == "proj::mymod" for e in depends), (
        "Module-level depends_on edge must still be emitted"
    )
