"""Phase 3 annotation-staleness tests.

Covers Pass A (annotates with DESC_ONLY) and Pass B (delegates_to transitive,
cycle-guarded, DESC_ONLY excluded).  Mapped to US-3 and US-4.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom_annotations import workflow

from axiom_graph.index import builder, db


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build(project_root: Path, discovery_only: bool = True) -> None:
    os.environ["AXIOM_GRAPH_SKIP_EMBEDDINGS"] = "1"
    builder.build(project_root, project_id="proj", discovery_only=discovery_only)


def _status(db_path: Path, node_id: str) -> tuple[str, str]:
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT own_status, link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
        assert row is not None, f"node {node_id} not found"
        return row["own_status"], row["link_status"]


@workflow(purpose="Pass A fires on function body change (CONTENT_ONLY)")
def test_pass_a_content_change(tmp_path):
    _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow

@workflow(purpose="build")
def F():
    return 1
""".lstrip(),
    )
    _build(tmp_path, discovery_only=False)
    # Run record_staleness to populate statuses.
    from axiom_graph.index import staleness as stalemod

    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Edit body.
    _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow

@workflow(purpose="build")
def F():
    return 42
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass picks up the history row the first pass wrote.
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    func_own, func_link = _status(ag_db, "proj::m::F")
    env_own, env_link = _status(ag_db, "proj::m::F@workflow")
    assert func_own == "CONTENT_UPDATED"
    assert env_link == "LINKED_STALE"


@workflow(purpose="Pass A fires on pure docstring change (DESC_ONLY)")
def test_pass_a_desc_change(tmp_path):
    _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow

@workflow(purpose="build")
def F():
    '''Original doc.'''
    return 1
""".lstrip(),
    )
    _build(tmp_path, discovery_only=False)
    from axiom_graph.index import staleness as stalemod

    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    _write(
        tmp_path / "m.py",
        """
from axiom_annotations import workflow

@workflow(purpose="build")
def F():
    '''Changed docstring entirely.'''
    return 1
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass picks up the history row the first pass wrote.
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    func_own, _ = _status(ag_db, "proj::m::F")
    env_own, env_link = _status(ag_db, "proj::m::F@workflow")
    assert func_own == "DESC_UPDATED"
    assert env_own == "VERIFIED"
    assert env_link == "LINKED_STALE"


@workflow(purpose="Pass B transitive: deep task change flips all delegating envelopes")
def test_pass_b_transitive_code_change(tmp_path):
    _write(
        tmp_path / "leaf.py",
        """
from axiom_annotations import task

@task(purpose="leaf")
def T2():
    return 1
""".lstrip(),
    )
    _write(
        tmp_path / "mid.py",
        """
from axiom_annotations import task, AutoStep
from leaf import T2

@task(purpose="mid")
def T1():
    口 = AutoStep(step_num=1, name="x")
    T2()
""".lstrip(),
    )
    _write(
        tmp_path / "top.py",
        """
from axiom_annotations import workflow, AutoStep
from mid import T1

@workflow(purpose="top")
def W1():
    口 = AutoStep(step_num=1, name="y")
    T1()
""".lstrip(),
    )
    _build(tmp_path, discovery_only=False)
    from axiom_graph.index import staleness as stalemod

    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    # Edit T2's body only.
    _write(
        tmp_path / "leaf.py",
        """
from axiom_annotations import task

@task(purpose="leaf")
def T2():
    return 999
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass picks up the history row the first pass wrote.
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    _, w1_link = _status(ag_db, "proj::top::W1@workflow")
    _, t1_link = _status(ag_db, "proj::mid::T1@workflow")
    _, t2_link = _status(ag_db, "proj::leaf::T2@workflow")
    assert t2_link == "LINKED_STALE", "Pass A should flip T2's envelope"
    assert t1_link == "LINKED_STALE", "Pass B should flip T1's envelope (via T2)"
    assert w1_link == "LINKED_STALE", "Pass B transitive should flip W1's envelope"


@workflow(purpose="Pass B excludes DESC_ONLY on tasks")
def test_pass_b_excludes_desc_only(tmp_path):
    _write(
        tmp_path / "leaf.py",
        """
from axiom_annotations import task

@task(purpose="leaf")
def T2():
    '''Doc A.'''
    return 1
""".lstrip(),
    )
    _write(
        tmp_path / "top.py",
        """
from axiom_annotations import workflow, AutoStep
from leaf import T2

@workflow(purpose="top")
def W1():
    口 = AutoStep(step_num=1, name="x")
    T2()
""".lstrip(),
    )
    _build(tmp_path, discovery_only=False)
    from axiom_graph.index import staleness as stalemod

    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    # Edit only T2's docstring.
    _write(
        tmp_path / "leaf.py",
        """
from axiom_annotations import task

@task(purpose="leaf")
def T2():
    '''Doc B — completely different prose.'''
    return 1
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass picks up the history row the first pass wrote.
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    _, t2_link = _status(ag_db, "proj::leaf::T2@workflow")
    _, w1_link = _status(ag_db, "proj::top::W1@workflow")
    assert t2_link == "LINKED_STALE", "Pass A should fire for DESC_ONLY on T2's envelope"
    assert w1_link == "VERIFIED", "Pass B must NOT propagate DESC_ONLY"


@workflow(purpose="Pass B cycle guard terminates in finite time")
def test_pass_b_cycle_guard(tmp_path):
    _write(
        tmp_path / "a.py",
        """
from axiom_annotations import task, AutoStep
from b import B

@task(purpose="A")
def A():
    口 = AutoStep(step_num=1, name="to-b")
    B()
""".lstrip(),
    )
    _write(
        tmp_path / "b.py",
        """
from axiom_annotations import task, AutoStep
from a import A

@task(purpose="B")
def B():
    口 = AutoStep(step_num=1, name="back-to-a")
    A()
""".lstrip(),
    )
    _build(tmp_path, discovery_only=False)
    from axiom_graph.index import staleness as stalemod

    ag_db = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    # Edit A's body.
    _write(
        tmp_path / "a.py",
        """
from axiom_annotations import task, AutoStep
from b import B

@task(purpose="A")
def A():
    口 = AutoStep(step_num=1, name="to-b")
    B()
    return 42
""".lstrip(),
    )
    _build(tmp_path)
    nodes = db.all_nodes(ag_db)
    # Must terminate and not raise.
    stalemod.record_staleness(ag_db, tmp_path, nodes)
    # Second pass picks up the history row the first pass wrote.
    nodes = db.all_nodes(ag_db)
    stalemod.record_staleness(ag_db, tmp_path, nodes)

    _, a_link = _status(ag_db, "proj::a::A@workflow")
    _, b_link = _status(ag_db, "proj::b::B@workflow")
    assert a_link == "LINKED_STALE"  # Pass A on A itself
    assert b_link == "LINKED_STALE"  # Pass B via delegates_to -> A
