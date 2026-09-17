"""Structural invariants an index must satisfy after a build.

Each invariant is a helper that returns the violations it finds, so it can
be pointed at any built index, plus a test that asserts a freshly built
index has none.  Add new invariants as helper + test pairs beside these.
"""

from __future__ import annotations

import os
from pathlib import Path

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db
from axiom_graph.index.staleness import find_broken_links
from axiom_graph.index.status import BROKEN_LINK, LINKED_STALE, NOT_FOUND, VERIFIED
from axiom_graph.lifecycle.api import build_index, compute_check_summary
from axiom_graph.models import make_edge


# ---------------------------------------------------------------------------
# Invariant helpers
# ---------------------------------------------------------------------------


def autostep_delegate_cardinality_violations(db_path: Path) -> dict[str, list[str]]:
    """Return AutoStep sources holding more than one outbound delegate link.

    An AutoStep delegates to at most one target — the function its next call
    resolves to.  The scoping to AutoStep sources is load-bearing: a state
    machine's states legitimately delegate to one target per transition.

    Args:
        db_path: Path to the index to inspect.

    Returns:
        Mapping of source node ID to its delegate targets, containing only
        sources with two or more.  Empty when the invariant holds.
    """
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.from_id, e.to_id FROM edges e "
            "JOIN nodes n ON n.id = e.from_id "
            "WHERE e.edge_type = 'delegates_to' AND n.subtype = 'autostep'"
        ).fetchall()
    by_source: dict[str, list[str]] = {}
    for row in rows:
        by_source.setdefault(row["from_id"], []).append(row["to_id"])
    return {source: targets for source, targets in by_source.items() if len(targets) > 1}


def _is_external_stub_id(node_id: str, shadowing_modules: set[str]) -> bool:
    """Return whether *node_id* is a scanner-minted external package stub.

    An external stub id is exactly ``{project_id}::external::{package}`` —
    three segments, the middle one literal.  Matching that shape rather
    than the substring ``::external::`` keeps the exclusion from swallowing
    deeper ids that merely contain a segment called ``external``, such as a
    step node under a function of that name.

    The three-segment shape alone is still ambiguous: a project holding a
    top-level ``external.py`` mints ``{project_id}::external::{function}``
    for each of its functions, which is character-for-character an external
    stub id.  Nothing in the id distinguishes them, so the module node is
    consulted instead — *shadowing_modules* holds the ids of module nodes
    named ``external``, and a project that has one gets no exclusion at
    all, which errs toward reporting rather than hiding.

    Args:
        node_id: The edge target being classified.
        shadowing_modules: Ids of module nodes whose dotpath is ``external``
            (i.e. ``{project_id}::external``), as returned by the index.

    Returns:
        True when the id should be treated as an external package stub.
    """
    parts = node_id.split("::")
    if len(parts) != 3 or parts[1] != "external":
        return False
    return f"{parts[0]}::external" not in shadowing_modules


def dangling_edge_target_violations(db_path: Path) -> list[tuple[str, str, str]]:
    """Return every edge whose target names no node in the index.

    An edge is a claim about two nodes.  A target that names nothing is a
    claim the index cannot honour: navigation dead-ends, traversal stops,
    and consumers print an identifier that resolves to no source.  The
    check is deliberately written over edge targets in general rather than
    one edge type, so an edge type added later inherits it.

    External package stubs are excluded.  Scanners mint those ids for
    third-party imports and the node behind one is a deliberate placeholder,
    so its absence from a partial index is not a broken claim.

    Args:
        db_path: Path to the index to inspect.

    Returns:
        Sorted list of ``(edge_type, from_id, to_id)`` triples, one per
        dangling edge.  Empty when the invariant holds.
    """
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT e.edge_type, e.from_id, e.to_id FROM edges e LEFT JOIN nodes n ON n.id = e.to_id WHERE n.id IS NULL"
        ).fetchall()
        shadowing_modules = {
            row["id"] for row in conn.execute("SELECT id FROM nodes WHERE subtype = 'module' AND id LIKE '%::external'")
        }
    return sorted(
        (row["edge_type"], row["from_id"], row["to_id"])
        for row in rows
        if not _is_external_stub_id(row["to_id"], shadowing_modules)
    )


# ---------------------------------------------------------------------------
# Fixture family — one multi-file package modelling real module topology
# ---------------------------------------------------------------------------
#
# Every resolution shape the index has to survive lives in one package so the
# cases below share a single topology instead of each inventing its own:
#
#   pkgapp/__init__.py        package that defines nothing
#   pkgapp/runner.py          tasks reached by attribute call
#   pkgapp/impl/core.py       task reached only through two star hops
#   pkgapp/impl/__init__.py   star aggregator
#   pkgapp/shim.py            star back-compat shim
#   pkgapp/orchestrator.py    `from pkg import submodule` + relative form
#   pkgapp/deferred.py        import inside the function body, attribute call
#   pkgapp/plain.py           no annotations, deferred import its only reference
#   pkgapp/service.py         workflow in a class body, delegating to a
#                             sibling method and to a method on a collaborator
#   reexp/__init__.py         defines nothing; re-exports by name, by alias
#                             (a function and a class), by relative star and
#                             by absolute import, and opens two-hop chains
#                             (named->star, named->named)
#   reexp/sub/__init__.py     re-exports a different ``func`` of its own
#   test_reexp_calls.py       one test per call shape through the re-exports
#   reexp_flow.py             one AutoStep per call shape through the re-exports
#
# ``_write_unresolvable_module`` is deliberately separate: the family above is
# fully resolvable, and the broken-link case needs a target nothing defines.
# ``_write_reexport_negatives`` is separate for the same reason: its steps
# name what the re-exports must not resolve, so they dangle by design.

_FAMILY_FILES: dict[str, str] = {
    "pkgapp/__init__.py": '"""Package that defines nothing of its own."""\n',
    "pkgapp/runner.py": (
        '"""Tasks a workflow reaches by attribute call."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Build the image")\n'
        "def build_image():\n"
        '    return "image"\n\n\n'
        '@task(purpose="Tear the stack down")\n'
        "def teardown():\n"
        '    return "gone"\n'
    ),
    "pkgapp/impl/core.py": (
        '"""Definitions reached only through the star-export layers."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Persist one record")\n'
        "def persist_record():\n"
        "    return 1\n"
    ),
    "pkgapp/impl/__init__.py": (
        '"""Aggregator that re-exports its submodule."""\n\nfrom pkgapp.impl.core import *  # noqa: F403\n'
    ),
    "pkgapp/shim.py": ('"""Back-compat shim that defines nothing."""\n\nfrom pkgapp.impl import *  # noqa: F403\n'),
    "pkgapp/orchestrator.py": (
        '"""Workflow whose steps leave the file."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n"
        "from pkgapp import runner\n"
        "from . import shim\n\n\n"
        '@workflow(purpose="Run the cross-module pipeline")\n'
        "def pipeline():\n"
        '    口 = AutoStep(step_num=1, name="Build the image")\n'
        "    runner.build_image()\n"
        '    口 = AutoStep(step_num=2, name="Persist the record")\n'
        "    shim.persist_record()\n"
    ),
    "pkgapp/deferred.py": (
        '"""Workflow whose receiver is imported inside the function body."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n\n"
        '@workflow(purpose="Tear the stack down")\n'
        "def deploy():\n"
        "    from pkgapp import runner\n\n"
        '    口 = AutoStep(step_num=1, name="Tear the stack down")\n'
        "    runner.teardown()\n"
    ),
    "pkgapp/plain.py": (
        '"""Module with no annotations whose only reference is deferred."""\n\n\n'
        "def helper():\n"
        "    from pkgapp import runner\n\n"
        "    return runner.build_image()\n"
    ),
    "pkgapp/service.py": (
        '"""Workflow declared in a class body."""\n\n'
        "from axiom_annotations import AutoStep, task, workflow\n\n\n"
        '@task(purpose="Emit the report")\n'
        "def emit_report():\n"
        '    return "report"\n\n\n'
        "class Service:\n"
        '    """One request handler."""\n\n'
        '    @workflow(purpose="Serve one request")\n'
        "    def handle(self):\n"
        '        口 = AutoStep(step_num=1, name="Emit the report")\n'
        "        emit_report()\n"
        '        口 = AutoStep(step_num=2, name="Record the outcome")\n'
        "        self.record_outcome()\n"
        '        口 = AutoStep(step_num=3, name="Flush the sink")\n'
        "        self.sink.flush()\n\n"
        '    @task(purpose="Record one outcome")\n'
        "    def record_outcome(self):\n"
        "        return None\n"
    ),
    "reexp/__init__.py": (
        '"""Package that defines nothing and re-exports what its modules define."""\n\n'
        "from reexp.absimpl import abs_func\n\n"
        "from .api import hop_star\n"
        "from .impl import LIMIT, Thing, func\n"
        "from .impl import Thing as ThingAlias\n"
        "from .impl import real_name as aliased\n"
        "from .mid import hop_named\n"
        "from .stars import *  # noqa: F403\n"
    ),
    "reexp/impl.py": (
        '"""Definitions the package re-exports by name."""\n\n'
        "from axiom_annotations import task\n\n"
        "LIMIT = 5\n\n\n"
        '@task(purpose="Do the thing")\n'
        "def func():\n"
        "    return 1\n\n\n"
        '@task(purpose="Do the thing under its own name")\n'
        "def real_name():\n"
        "    return 2\n\n\n"
        "class Thing:\n"
        '    """A class the package re-exports."""\n\n'
        '    @task(purpose="Run the member")\n'
        "    def method(self):\n"
        "        return 3\n"
    ),
    "reexp/stars.py": (
        '"""Definitions the package re-exports by star."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Reached through a relative star re-export")\n'
        "def star_func():\n"
        "    return 4\n"
    ),
    "reexp/api.py": '"""Star layer over the deep module."""\n\nfrom .deep import *  # noqa: F403\n',
    "reexp/mid.py": '"""Named layer over the deep module."""\n\nfrom .deep import hop_named\n',
    "reexp/deep.py": (
        '"""Definitions two re-export hops away from the package."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Reached through a named hop then a star hop")\n'
        "def hop_star():\n"
        "    return 5\n\n\n"
        '@task(purpose="Reached through two named hops")\n'
        "def hop_named():\n"
        "    return 6\n"
    ),
    "reexp/absimpl.py": (
        '"""Definition the package re-exports by absolute import."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Reached through an absolute named re-export")\n'
        "def abs_func():\n"
        "    return 7\n"
    ),
    "reexp/sub/__init__.py": '"""Subpackage re-exporting a func of its own."""\n\nfrom .leaf import func\n',
    "reexp/sub/leaf.py": (
        '"""Definitions of the subpackage."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="A func that is not the package func")\n'
        "def func():\n"
        "    return 8\n"
    ),
    "test_reexp_calls.py": (
        '"""Tests reaching the package functions through its re-exports."""\n\n'
        "from reexp import LIMIT, Thing, ThingAlias, abs_func, aliased, func, hop_named, hop_star, star_func\n\n\n"
        "def test_named():\n"
        "    func()\n\n\n"
        "def test_body_import():\n"
        "    from reexp import func as body_func\n\n"
        "    body_func()\n\n\n"
        "def test_aliased():\n"
        "    aliased()\n\n\n"
        "def test_star():\n"
        "    star_func()\n\n\n"
        "def test_named_then_star():\n"
        "    hop_star()\n\n\n"
        "def test_named_then_named():\n"
        "    hop_named()\n\n\n"
        "def test_absolute():\n"
        "    abs_func()\n\n\n"
        "def test_module_attribute():\n"
        "    import reexp\n\n"
        "    reexp.func()\n\n\n"
        "def test_member():\n"
        "    Thing.method(None)\n\n\n"
        "def test_aliased_member():\n"
        "    ThingAlias.method(None)\n\n\n"
        "def test_class():\n"
        "    Thing()\n\n\n"
        "def test_constant():\n"
        "    LIMIT.bit_length()\n\n\n"
        "def test_chained():\n"
        "    import reexp\n\n"
        "    reexp.sub.func()\n\n\n"
        "def test_chained_through_star():\n"
        "    import reexp\n\n"
        "    reexp.stars.star_func()\n\n\n"
        "def test_dotted_twin():\n"
        "    import reexp.sub\n\n"
        "    reexp.sub.func()\n"
    ),
    "reexp_flow.py": (
        '"""Workflows whose steps reach the package functions through its re-exports."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n"
        "import reexp\n"
        "from reexp import Thing, ThingAlias, abs_func, aliased, func, hop_named, hop_star, star_func\n\n\n"
        '@workflow(purpose="Reach every function the package re-exports")\n'
        "def run_all():\n"
        '    口 = AutoStep(step_num=1, name="Named")\n'
        "    func()\n"
        '    口 = AutoStep(step_num=2, name="Aliased")\n'
        "    aliased()\n"
        '    口 = AutoStep(step_num=3, name="Relative star")\n'
        "    star_func()\n"
        '    口 = AutoStep(step_num=4, name="Named then star")\n'
        "    hop_star()\n"
        '    口 = AutoStep(step_num=5, name="Named then named")\n'
        "    hop_named()\n"
        '    口 = AutoStep(step_num=6, name="Absolute named")\n'
        "    abs_func()\n"
        '    口 = AutoStep(step_num=7, name="Module attribute")\n'
        "    reexp.func()\n"
        '    口 = AutoStep(step_num=8, name="Member of a re-exported class")\n'
        "    Thing.method(None)\n"
        '    口 = AutoStep(step_num=9, name="Member of a class re-exported under an alias")\n'
        "    ThingAlias.method(None)\n"
        '    口 = AutoStep(step_num=10, name="Chained through a star re-export")\n'
        "    reexp.stars.star_func()\n\n\n"
        '@workflow(purpose="Reach functions through imports made in the body")\n'
        "def run_deferred():\n"
        "    import reexp.sub\n"
        "    from reexp import func as body_func\n\n"
        '    口 = AutoStep(step_num=1, name="Import in the body")\n'
        "    body_func()\n"
        '    口 = AutoStep(step_num=2, name="Spelled dotted import")\n'
        "    reexp.sub.func()\n"
    ),
}

_UNRESOLVABLE_MODULE = (
    '"""Workflow whose delegate target is defined nowhere."""\n\n'
    "from axiom_annotations import AutoStep, workflow\n\n"
    "from pkgapp import runner\n\n\n"
    '@workflow(purpose="Call a name the package does not define")\n'
    "def orphan():\n"
    '    口 = AutoStep(step_num=1, name="Call a helper that does not exist")\n'
    "    runner.vanished_helper()\n"
)


_REEXPORT_NEGATIVES_MODULE = (
    '"""Workflow whose steps name what the re-exports must not resolve."""\n\n'
    "from axiom_annotations import AutoStep, workflow\n\n"
    "import reexp\n"
    "from reexp import Thing\n\n\n"
    '@workflow(purpose="Reach a re-exported class and a call chained past the package")\n'
    "def run_negatives():\n"
    '    口 = AutoStep(step_num=1, name="Instantiate a re-exported class")\n'
    "    Thing()\n"
    '    口 = AutoStep(step_num=2, name="Call chained past the package")\n'
    "    reexp.sub.func()\n"
)


def _write_files(project_root: Path, files: dict[str, str]) -> None:
    """Write ``{relative path: source}`` into *project_root*, creating directories."""
    for rel_path, source in files.items():
        target = project_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")


def _write_fixture_family(project_root: Path) -> None:
    """Write the multi-file fixture package into *project_root*."""
    _write_files(project_root, _FAMILY_FILES)


def _write_unresolvable_module(project_root: Path) -> None:
    """Add a module whose delegate target no file in the family defines."""
    (project_root / "pkgapp" / "unresolvable.py").write_text(_UNRESOLVABLE_MODULE, encoding="utf-8")


def _write_reexport_negatives(project_root: Path) -> None:
    """Add a workflow whose steps name what the family's re-exports must not resolve."""
    (project_root / "reexp_negatives.py").write_text(_REEXPORT_NEGATIVES_MODULE, encoding="utf-8")


def _rewrite(path: Path, transform) -> None:
    """Rewrite *path* through *transform* and move its mtime past the stored one.

    An incremental build skips a file whose mtime has not advanced, and a
    write inside one test can land on the mtime the previous build stored.

    Args:
        path: File to rewrite.
        transform: Callable taking the current source and returning the new one.
    """
    path.write_text(transform(path.read_text(encoding="utf-8")), encoding="utf-8")
    later = path.stat().st_mtime + 10
    os.utime(path, (later, later))


# ---------------------------------------------------------------------------
# Index navigation helpers — every expectation is read back from the build
# ---------------------------------------------------------------------------


def _node_id(db_path: Path, location: str, title: str) -> str:
    """Return the id of the node the build minted for *title* in *location*."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM nodes WHERE location = ? AND title = ?",
            (location, title),
        ).fetchall()
    assert len(rows) == 1, f"expected one node for {title!r} in {location}, got {[r['id'] for r in rows]}"
    return rows[0]["id"]


def _module_node_id(db_path: Path, location: str) -> str:
    """Return the id of the module node the build minted for *location*."""
    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM nodes WHERE location = ? AND subtype = 'module'",
            (location,),
        ).fetchone()
    assert row is not None, f"build did not mint a module node for {location}"
    return row["id"]


def _persisted_link_status(db_path: Path, node_id: str) -> str | None:
    """Return the ``link_status`` column stored on *node_id*, or ``None``.

    Reads the persisted column rather than any in-memory computation, so a
    caller can tell whether a status a build wrote actually survived the
    staleness recompute that follows it.

    Args:
        db_path: Path to the axiom-graph SQLite database.
        node_id: Node whose stored link status is wanted.

    Returns:
        The stored ``link_status`` string, or ``None`` when no such node row
        exists.
    """
    with db._connect(db_path) as conn:
        row = conn.execute("SELECT link_status FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return None if row is None else row["link_status"]


def _delegate_target(db_path: Path, step_source_id: str) -> str | None:
    """Return the single delegate target held by *step_source_id*, if any."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT to_id FROM edges WHERE edge_type = 'delegates_to' AND from_id = ?",
            (step_source_id,),
        ).fetchall()
    assert len(rows) <= 1, f"{step_source_id} holds several delegate links: {[r['to_id'] for r in rows]}"
    return rows[0]["to_id"] if rows else None


def _step_id(func_node_id: str, step_num: str) -> str:
    """Return the id of a step node inside *func_node_id*."""
    return f"{func_node_id}::step-{step_num}"


def _has_edge(db_path: Path, edge_type: str, from_id: str, to_id: str) -> bool:
    """Return whether the index holds *edge_type* between the two nodes."""
    with db._connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM edges WHERE edge_type = ? AND from_id = ? AND to_id = ?",
            (edge_type, from_id, to_id),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Tier 2 — invariants over a built index
# ---------------------------------------------------------------------------


def _workflow_module(step_name: str, next_call: str) -> str:
    """Return a module with one workflow whose AutoStep is followed by *next_call*."""
    return (
        '"""Module."""\n\n'
        "from axiom_annotations import AutoStep, task, workflow\n\n\n"
        '@task(purpose="Do the first thing")\n'
        "def alpha():\n"
        "    return 1\n\n\n"
        '@task(purpose="Do the second thing")\n'
        "def beta():\n"
        "    return 2\n\n\n"
        '@workflow(purpose="Run it")\n'
        f"def {step_name}():\n"
        '    口 = AutoStep(step_num=1, name="Delegate")\n'
        f"    {next_call}\n"
    )


@workflow(
    purpose="No AutoStep in a built index holds more than one outbound delegate link, including after its call is retargeted",
)
def test_no_autostep_holds_more_than_one_delegate_link(mini_project, db_path):
    (mini_project / "stable.py").write_text(_workflow_module("run_stable", "alpha()"), encoding="utf-8")
    (mini_project / "moving.py").write_text(_workflow_module("run_moving", "alpha()"), encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    (mini_project / "moving.py").write_text(_workflow_module("run_moving", "beta()"), encoding="utf-8")
    builder.build(mini_project, project_id="proj", discovery_only=False)

    with db._connect(db_path) as conn:
        delegate_count = conn.execute("SELECT COUNT(*) AS c FROM edges WHERE edge_type = 'delegates_to'").fetchone()[
            "c"
        ]
    assert delegate_count >= 2, "fixture should have produced delegate links to check"

    violations = autostep_delegate_cardinality_violations(db_path)
    assert violations == {}, f"AutoStep sources with several delegate links: {violations}"


@workflow(
    purpose="No edge in a built index points at an identifier the same build did not mint as a node",
)
def test_no_edge_targets_a_node_the_build_did_not_mint(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    with db._connect(db_path) as conn:
        edge_count = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
    assert edge_count > 0, "fixture family should have produced edges to check"

    violations = dangling_edge_target_violations(db_path)
    assert violations == [], f"edges whose target names no node: {violations}"


# ---------------------------------------------------------------------------
# Tier 2 / Tier 3 — delegate target resolution across module boundaries
# ---------------------------------------------------------------------------


@workflow(
    purpose="An AutoStep calling a method on a module imported via `from package import submodule` delegates to the node defining that method",
)
def test_attribute_call_on_an_imported_submodule_resolves_to_the_defining_node(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")
    build_image_id = _node_id(db_path, "pkgapp/runner.py", "build_image")

    assert _delegate_target(db_path, _step_id(pipeline_id, "1")) == build_image_id


@workflow(
    purpose="An AutoStep whose target is reached through star-export shim layers delegates to the defining node, not to a shim that defines nothing",
)
def test_target_behind_star_export_shims_resolves_to_the_defining_node(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")
    persist_record_id = _node_id(db_path, "pkgapp/impl/core.py", "persist_record")

    assert _delegate_target(db_path, _step_id(pipeline_id, "2")) == persist_record_id


@workflow(
    purpose="An AutoStep in a method calling a sibling method through self delegates to that method's node",
)
def test_self_call_in_a_method_resolves_to_the_sibling_method(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    handle_id = _node_id(db_path, "pkgapp/service.py", "Service.handle")
    record_outcome_id = _node_id(db_path, "pkgapp/service.py", "Service.record_outcome")

    assert _delegate_target(db_path, _step_id(handle_id, "2")) == record_outcome_id


@workflow(
    purpose="A module whose only reference to another module is an import inside a function body depends on it, with no annotations anywhere",
)
def test_import_inside_a_function_body_is_a_module_dependency(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    plain_module_id = _module_node_id(db_path, "pkgapp/plain.py")
    runner_module_id = _module_node_id(db_path, "pkgapp/runner.py")
    orchestrator_module_id = _module_node_id(db_path, "pkgapp/orchestrator.py")

    assert _has_edge(db_path, "depends_on", plain_module_id, runner_module_id)
    # A module-level import of the same module is unaffected.
    assert _has_edge(db_path, "depends_on", orchestrator_module_id, runner_module_id)


@workflow(
    purpose="A workflow whose receiver is imported inside its own function body and then called by attribute delegates to the node defining the called function",
)
def test_receiver_imported_inside_the_function_body_resolves_by_attribute_call(mini_project, db_path):
    口 = Step(
        step_num=1,
        name="Write the fixture package",
        purpose="Lay down a package whose workflow imports its receiver inside the function body and calls it by attribute",
    )
    _write_fixture_family(mini_project)

    口 = Step(
        step_num=2,
        name="Build the index",
        purpose="Scan the package so nodes and edges are minted for every module",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=3,
        name="Read back the nodes the build minted",
        purpose="Take the workflow and the called function from the index rather than naming them",
    )
    deploy_id = _node_id(db_path, "pkgapp/deferred.py", "deploy")
    teardown_id = _node_id(db_path, "pkgapp/runner.py", "teardown")

    口 = Step(
        step_num=4,
        name="Assert the step delegates to the defining node",
        purpose="Confirm the deferred import and the attribute call resolve together",
    )
    assert _delegate_target(db_path, _step_id(deploy_id, "1")) == teardown_id

    口 = Step(
        step_num=5,
        name="Assert the deferred import is also a dependency",
        purpose="Confirm the same import feeds the module dependency graph",
    )
    deferred_module_id = _module_node_id(db_path, "pkgapp/deferred.py")
    runner_module_id = _module_node_id(db_path, "pkgapp/runner.py")
    assert _has_edge(db_path, "depends_on", deferred_module_id, runner_module_id)


@workflow(
    purpose="A delegate target no file defines is reported as a broken link, while a target that resolves produces no finding",
)
def test_unresolvable_delegate_target_is_reported_as_a_broken_link(mini_project, db_path):
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    pipeline_id = _node_id(db_path, "pkgapp/orchestrator.py", "pipeline")

    broken = find_broken_links(db_path)

    assert f"{orphan_id}@workflow" in broken, f"unresolvable delegate target not reported: {broken}"
    assert f"{pipeline_id}@workflow" not in broken, "a delegate target that resolves must produce no finding"


@workflow(
    purpose="A broken delegate link is persisted on the envelope by the build and still reported by the check that follows it",
)
def test_broken_delegate_link_survives_the_build_and_the_following_check(mini_project, db_path):
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)

    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    orphan_envelope = f"{_node_id(db_path, 'pkgapp/unresolvable.py', 'orphan')}@workflow"
    pipeline_envelope = f"{_node_id(db_path, 'pkgapp/orchestrator.py', 'pipeline')}@workflow"

    assert _persisted_link_status(db_path, orphan_envelope) == BROKEN_LINK, (
        "the build did not leave BROKEN_LINK persisted on the envelope of the unresolvable delegate target"
    )

    summary = compute_check_summary(db_path, mini_project)

    assert summary is not None
    assert summary.link_counts[BROKEN_LINK] >= 1, (
        f"check did not report the unresolvable delegate target: {summary.link_counts}"
    )
    assert _persisted_link_status(db_path, orphan_envelope) == BROKEN_LINK, (
        "the staleness recompute behind check overwrote the envelope's BROKEN_LINK"
    )
    assert _persisted_link_status(db_path, pipeline_envelope) != BROKEN_LINK, (
        "an envelope whose delegate targets all resolve must not be flagged"
    )


def test_a_step_no_envelope_composes_keeps_the_broken_link_finding_on_itself(mini_project, db_path):
    """A step with no composing envelope holds its own finding, and it persists.

    Envelope attribution needs a ``composes`` edge from a workflow or task.
    A step left in the index by an earlier edit has none, so the finding
    falls back to the step id.  It must still land somewhere that keeps it:
    the step id is the only identifier that describes the leftover, and a
    status written there survives the recompute because the overlay outranks
    the blanket VERIFIED that steps are assigned.
    """
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    step_id = _step_id(orphan_id, "1")
    with db._connect(db_path) as conn:
        conn.execute("DELETE FROM edges WHERE edge_type = 'composes' AND to_id = ?", (step_id,))

    assert find_broken_links(db_path).get(step_id) is not None, (
        "with no composing envelope the finding must fall back to the step itself"
    )

    compute_check_summary(db_path, mini_project)

    assert _persisted_link_status(db_path, step_id) == BROKEN_LINK
    assert _persisted_link_status(db_path, f"{orphan_id}@workflow") != BROKEN_LINK, (
        "an envelope that no longer composes the step must not keep its finding"
    )


@workflow(
    purpose="Retargeting an AutoStep's cross-module call leaves exactly one delegate link, pointing at the new target",
)
def test_retargeting_a_cross_module_autostep_leaves_one_delegate_link(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    deferred = mini_project / "pkgapp" / "deferred.py"
    deferred.write_text(
        deferred.read_text(encoding="utf-8").replace("runner.teardown()", "runner.build_image()"),
        encoding="utf-8",
    )
    builder.build(mini_project, project_id="proj", discovery_only=False)

    deploy_id = _node_id(db_path, "pkgapp/deferred.py", "deploy")
    build_image_id = _node_id(db_path, "pkgapp/runner.py", "build_image")

    assert _delegate_target(db_path, _step_id(deploy_id, "1")) == build_image_id
    assert dangling_edge_target_violations(db_path) == []


# ---------------------------------------------------------------------------
# Tier 2 / Tier 3 — links through package re-exports
# ---------------------------------------------------------------------------
#
# One row per call shape: the family test making the call, the AutoStep
# making it, and where the called function is defined.

_REEXPORT_SHAPES: list[tuple[str, str, tuple[str, str], str, str]] = [
    ("named", "test_named", ("run_all", "1"), "reexp/impl.py", "func"),
    ("import in the body", "test_body_import", ("run_deferred", "1"), "reexp/impl.py", "func"),
    ("aliased", "test_aliased", ("run_all", "2"), "reexp/impl.py", "real_name"),
    ("relative star", "test_star", ("run_all", "3"), "reexp/stars.py", "star_func"),
    ("named then star", "test_named_then_star", ("run_all", "4"), "reexp/deep.py", "hop_star"),
    ("named then named", "test_named_then_named", ("run_all", "5"), "reexp/deep.py", "hop_named"),
    ("absolute named", "test_absolute", ("run_all", "6"), "reexp/absimpl.py", "abs_func"),
    ("module attribute", "test_module_attribute", ("run_all", "7"), "reexp/impl.py", "func"),
    ("member of a class", "test_member", ("run_all", "8"), "reexp/impl.py", "Thing.method"),
    ("member of an aliased class", "test_aliased_member", ("run_all", "9"), "reexp/impl.py", "Thing.method"),
    ("spelled dotted import", "test_dotted_twin", ("run_deferred", "2"), "reexp/sub/leaf.py", "func"),
]

_REEXPORT_DEFINING_FILES = (
    "reexp/impl.py",
    "reexp/stars.py",
    "reexp/deep.py",
    "reexp/absimpl.py",
    "reexp/sub/leaf.py",
)


def _validated_targets(db_path: Path, test_id: str) -> list[str]:
    """Return the sorted targets of every validates link held by *test_id*."""
    with db._connect(db_path) as conn:
        rows = conn.execute(
            "SELECT to_id FROM edges WHERE edge_type = 'validates' AND from_id = ?",
            (test_id,),
        ).fetchall()
    return sorted(row["to_id"] for row in rows)


@workflow(
    purpose="Tests reaching a function through a package's named, aliased, star, absolute and two-hop re-exports validate the defining function, and editing it makes them LINKED_STALE",
)
def test_tests_reaching_a_function_through_package_reexports_link_to_it_and_go_stale_when_it_changes(
    mini_project, db_path
):
    口 = Step(
        step_num=1,
        name="Build the fixture family",
        purpose="Index a package that only re-exports, beside one test per call shape",
    )
    _write_fixture_family(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    compute_check_summary(db_path, mini_project)

    口 = Step(
        step_num=2,
        name="Assert every test validates the defining function",
        purpose=(
            "Each call shape reaches the function through the re-exports, not the package that names it, "
            "and starts out VERIFIED"
        ),
    )
    for shape, test_title, _flow, location, title in _REEXPORT_SHAPES:
        test_id = _node_id(db_path, "test_reexp_calls.py", test_title)
        target_id = _node_id(db_path, location, title)
        assert _has_edge(db_path, "validates", test_id, target_id), (
            f"{shape}: {test_id} does not validate {target_id}; it validates {_validated_targets(db_path, test_id)}"
        )
        assert _persisted_link_status(db_path, test_id) == VERIFIED, f"{shape}: {test_id} is not VERIFIED yet"

    口 = Step(
        step_num=3,
        name="Edit every defining function and build again",
        purpose="Change the bodies behind the re-exports and let an incremental build see them",
    )
    for location in _REEXPORT_DEFINING_FILES:
        _rewrite(mini_project / location, lambda source: source.replace("return ", "return 100 + "))
    build_index(db_path, mini_project, project_id="proj")
    compute_check_summary(db_path, mini_project)

    口 = Step(
        step_num=4,
        name="Assert every test is LINKED_STALE",
        purpose="The restored links carry the change to the tests that exercise it",
    )
    for shape, test_title, *_ in _REEXPORT_SHAPES:
        test_id = _node_id(db_path, "test_reexp_calls.py", test_title)
        assert _persisted_link_status(db_path, test_id) == LINKED_STALE, f"{shape}: {test_id} did not go stale"

    口 = Step(
        step_num=5,
        name="Assert the tests with no link are not flagged",
        purpose="A re-exported class or constant is not linked, so editing the file that defines it flags none of its tests",
    )
    for test_title in ("test_class", "test_constant"):
        test_id = _node_id(db_path, "test_reexp_calls.py", test_title)
        assert _persisted_link_status(db_path, test_id) == VERIFIED, f"{test_title} was flagged by the edit"


@workflow(
    purpose="AutoSteps reaching a function through a package's named, aliased, star, absolute and two-hop re-exports, or by a call chained past the package into a module it star-exports, delegate to the defining function, and none is a broken link",
)
def test_autosteps_reaching_a_function_through_package_reexports_delegate_to_it(mini_project, db_path):
    _write_fixture_family(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    for shape, _test, (flow_title, step_num), location, title in _REEXPORT_SHAPES:
        step_id = _step_id(_node_id(db_path, "reexp_flow.py", flow_title), step_num)
        assert _delegate_target(db_path, step_id) == _node_id(db_path, location, title), shape

    # A chained call names the package rather than the module it calls into;
    # the package's star re-export of that module still reaches the definition.
    chained_step_id = _step_id(_node_id(db_path, "reexp_flow.py", "run_all"), "10")
    assert _delegate_target(db_path, chained_step_id) == _node_id(db_path, "reexp/stars.py", "star_func")

    broken = find_broken_links(db_path)
    for flow_title in ("run_all", "run_deferred"):
        envelope = f"{_node_id(db_path, 'reexp_flow.py', flow_title)}@workflow"
        assert envelope not in broken, f"{envelope} reports a broken link: {broken[envelope]}"


@workflow(
    purpose="A re-exported class or constant gains no validates link, nor does a test's call chained past a package (even into a module the package star-exports), and an AutoStep's chained call is never linked to the function the package re-exports under the same name",
)
def test_reexports_add_no_link_to_a_class_or_through_an_unspelled_chain(mini_project, db_path):
    _write_fixture_family(mini_project)
    _write_reexport_negatives(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    for test_title in ("test_class", "test_constant", "test_chained", "test_chained_through_star"):
        test_id = _node_id(db_path, "test_reexp_calls.py", test_title)
        assert _validated_targets(db_path, test_id) == [], f"{test_title} gained a validates link"

    # The AutoSteps keep the targets the scanner emitted.
    package_id = _module_node_id(db_path, "reexp/__init__.py")
    negatives_id = _node_id(db_path, "reexp_negatives.py", "run_negatives")
    assert _delegate_target(db_path, _step_id(negatives_id, "1")) == f"{package_id}::Thing"
    assert _delegate_target(db_path, _step_id(negatives_id, "2")) == f"{package_id}::func"

    # The spelled twin of the chained call still resolves through the subpackage.
    twin_id = _node_id(db_path, "test_reexp_calls.py", "test_dotted_twin")
    assert _validated_targets(db_path, twin_id) == [_node_id(db_path, "reexp/sub/leaf.py", "func")]


_ADDED_TEST_MODULE = (
    '"""A test file added after the first build."""\n\n'
    "from pkgapp.runner import build_image\n"
    "from reexp import func\n\n\n"
    "def test_calls_both_targets():\n"
    "    build_image()\n"
    "    func()\n"
)


@workflow(
    purpose="A test added to an index whose targets are unchanged validates them on the build that adds it, directly and through a package re-export, and goes LINKED_STALE when they change",
)
def test_a_test_added_beside_unchanged_targets_links_to_them_on_the_build_that_adds_it(mini_project, db_path):
    口 = Step(step_num=1, name="Build the fixture family", purpose="A full build stores every file's mtime")
    _write_fixture_family(mini_project)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    口 = Step(
        step_num=2,
        name="Add a test file and build again",
        purpose="Only the new file is rescanned; every file it calls into is skipped by mtime",
    )
    (mini_project / "test_added_later.py").write_text(_ADDED_TEST_MODULE, encoding="utf-8")
    summary = build_index(db_path, mini_project, project_id="proj")
    assert summary.files_scanned == 1, f"expected only the new file rescanned, got {summary.files_scanned}"

    口 = Step(
        step_num=3,
        name="Assert both validates links exist",
        purpose="The direct target and the re-exported one are both linked on this build",
    )
    test_id = _node_id(db_path, "test_added_later.py", "test_calls_both_targets")
    build_image_id = _node_id(db_path, "pkgapp/runner.py", "build_image")
    func_id = _node_id(db_path, "reexp/impl.py", "func")
    assert _validated_targets(db_path, test_id) == sorted([build_image_id, func_id])

    口 = Step(step_num=4, name="Edit both targets and build", purpose="Change the functions the new test calls")
    for location in ("pkgapp/runner.py", "reexp/impl.py"):
        _rewrite(mini_project / location, lambda source: source.replace("return ", "return 100 + "))
    build_index(db_path, mini_project, project_id="proj")
    compute_check_summary(db_path, mini_project)

    口 = Step(step_num=5, name="Assert the new test is LINKED_STALE", purpose="Its links carry the change")
    assert _persisted_link_status(db_path, test_id) == LINKED_STALE


_MOVE_BEFORE: dict[str, str] = {
    "mv/__init__.py": (
        '"""Package that still defines its function."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Defined in the package itself")\n'
        "def func():\n"
        "    return 1\n"
    ),
    "mv2.py": (
        '"""Module a later edit turns into a package."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Defined in a plain module")\n'
        "def func2():\n"
        "    return 2\n"
    ),
    "test_mv.py": (
        '"""Test calling both functions through their package."""\n\n'
        "from mv import func\n"
        "from mv2 import func2\n\n\n"
        "def test_moved():\n"
        "    func()\n"
        "    func2()\n"
    ),
    "mv_flow.py": (
        '"""Workflow calling both functions through their package."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n"
        "from mv import func\n"
        "from mv2 import func2\n\n\n"
        '@workflow(purpose="Call both functions")\n'
        "def run_moved():\n"
        '    口 = AutoStep(step_num=1, name="Call the function moved within its package")\n'
        "    func()\n"
        '    口 = AutoStep(step_num=2, name="Call the function whose module became a package")\n'
        "    func2()\n"
    ),
}

# The moved bodies change as well, so rename detection has nothing to carry
# the old links across on: the links the callers end with come from link
# resolution on the build that performs the move.
_MOVE_AFTER: dict[str, str] = {
    "mv/__init__.py": '"""Back-compat re-export of the moved function."""\n\nfrom .impl import func\n',
    "mv/impl.py": (
        '"""Where the function lives now."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Defined in the package itself")\n'
        "def func():\n"
        "    total = 10\n"
        "    return total + 1\n"
    ),
    "mv2/__init__.py": '"""Back-compat re-export of the moved function."""\n\nfrom .impl import func2\n',
    "mv2/impl.py": (
        '"""Where the function lives now."""\n\n'
        "from axiom_annotations import task\n\n\n"
        '@task(purpose="Defined in a plain module")\n'
        "def func2():\n"
        "    total = 20\n"
        "    return total + 2\n"
    ),
}


# Callers added on a build after the move.  That build leaves the package
# alone, so its index entry for the old function is a NOT_FOUND ghost in a
# file that still exists.
_MOVE_LATER_CALLERS: dict[str, str] = {
    "test_mv_later.py": (
        '"""Test added after the move, calling the function through its package."""\n\n'
        "from mv import func\n\n\n"
        "def test_moved_later():\n"
        "    func()\n"
    ),
    "mv_flow_later.py": (
        '"""Workflow added after the move, calling the function through its package."""\n\n'
        "from axiom_annotations import AutoStep, workflow\n\n"
        "from mv import func\n\n\n"
        '@workflow(purpose="Call the moved function")\n'
        "def run_moved_later():\n"
        '    口 = AutoStep(step_num=1, name="Call the function moved within its package")\n'
        "    func()\n"
    ),
}


@workflow(
    purpose="Moving a function behind a package re-export, or turning its module into a package, leaves its callers linked to the moved function and not to the ghost of the old one, whether they are rescanned in the same build as the move or added on a later build",
)
def test_moving_a_function_behind_a_reexport_keeps_its_callers_linked_to_the_moved_function(mini_project, db_path):
    _write_files(mini_project, _MOVE_BEFORE)
    build_index(db_path, mini_project, project_id="proj", discovery_only=False)

    (mini_project / "mv2.py").unlink()
    _write_files(mini_project, _MOVE_AFTER)
    for caller in ("test_mv.py", "mv_flow.py"):
        _rewrite(mini_project / caller, lambda source: source + "\n")
    build_index(db_path, mini_project, project_id="proj")

    moved = [_node_id(db_path, "mv/impl.py", "func"), _node_id(db_path, "mv2/impl.py", "func2")]
    validated = _validated_targets(db_path, _node_id(db_path, "test_mv.py", "test_moved"))
    for target in moved:
        assert target in validated, f"the caller test lost its link to {target}: {validated}"

    flow_id = _node_id(db_path, "mv_flow.py", "run_moved")
    assert _delegate_target(db_path, _step_id(flow_id, "1")) == moved[0]
    assert _delegate_target(db_path, _step_id(flow_id, "2")) == moved[1]

    ghost_id = f"{_module_node_id(db_path, 'mv/__init__.py')}::func"
    with db._connect(db_path) as conn:
        ghost = conn.execute("SELECT own_status FROM nodes WHERE id = ?", (ghost_id,)).fetchone()
    assert ghost is not None and ghost["own_status"] == NOT_FOUND, f"expected {ghost_id} to linger as NOT_FOUND"

    _write_files(mini_project, _MOVE_LATER_CALLERS)
    summary = build_index(db_path, mini_project, project_id="proj")
    assert summary.files_scanned == 2, f"expected only the two new callers rescanned, the package skipped: {summary}"

    later_flow_id = _node_id(db_path, "mv_flow_later.py", "run_moved_later")
    assert _delegate_target(db_path, _step_id(later_flow_id, "1")) == moved[0]
    later_test_id = _node_id(db_path, "test_mv_later.py", "test_moved_later")
    assert _validated_targets(db_path, later_test_id) == [moved[0]]


_UPGRADED_INDEX_TEST_MODULE = (
    '"""A test added to an index built before named re-export markers."""\n\n'
    "from reexp import func\n\n\n"
    "def test_calls_the_package_func():\n"
    "    func()\n"
)


def _missing_marker_warnings(summary) -> list[str]:
    """Return the build warnings reporting that named re-export markers are missing."""
    return [warning for warning in summary.warnings if "named re-export markers" in warning]


@workflow(
    purpose="A full build whose unresolved links are genuine misses guessed against modules it rescanned raises no missing-markers warning; an index built before named re-export markers warns, with the recipe, on an incremental build whose new test reaches a package the build did not rescan, even when the build rescans another module with a named import, and goes quiet once that package is rescanned and the link resolves",
)
def test_an_index_without_named_markers_warns_until_the_package_behind_a_link_is_rescanned(mini_project, db_path):
    口 = Step(
        step_num=1,
        name="Build the family, which raises no warning, then drop its named markers",
        purpose=(
            "The full build rescans every module, so its genuine misses (a class, a constant) are guessed against "
            "rescanned modules and leave the recipe nothing to fix; dropping the markers afterwards reproduces an "
            "index built before they existed"
        ),
    )
    _write_fixture_family(mini_project)
    summary = build_index(db_path, mini_project, project_id="proj", discovery_only=False)
    assert _missing_marker_warnings(summary) == [], summary.warnings
    with db._connect(db_path) as conn:
        conn.execute(
            "UPDATE edges SET meta = json_remove(meta, '$.reexport_names') "
            "WHERE edge_type = 'depends_on' AND meta IS NOT NULL"
        )

    口 = Step(
        step_num=2,
        name="Add a test and touch an unrelated module, then build",
        purpose=(
            "The new test reaches func through the package, which this build does not rescan; the touched "
            "module has a named import of its own, so the build writes named markers"
        ),
    )
    (mini_project / "test_after_upgrade.py").write_text(_UPGRADED_INDEX_TEST_MODULE, encoding="utf-8")
    unrelated = mini_project / "reexp" / "mid.py"
    _rewrite(unrelated, lambda source: source + "\n")
    summary = build_index(db_path, mini_project, project_id="proj")
    assert summary.files_scanned == 2, f"expected the new test and the touched module rescanned: {summary}"

    口 = Step(
        step_num=3,
        name="Assert the build warns with the recipe",
        purpose="The unresolved link was guessed against a package whose markers predate this build",
    )
    warnings = _missing_marker_warnings(summary)
    assert len(warnings) == 1, f"expected one missing-markers warning, got {summary.warnings}"
    assert "0 delegate link(s) and 1 validates link(s)" in warnings[0], warnings[0]
    assert "UPDATE nodes SET file_mtime = NULL" in warnings[0] and "axiom-graph build" in warnings[0], warnings[0]
    test_id = _node_id(db_path, "test_after_upgrade.py", "test_calls_the_package_func")
    assert _validated_targets(db_path, test_id) == []

    口 = Step(
        step_num=4,
        name="Rescan the package beside the test, then build",
        purpose=(
            "The package's markers are written again; the unrelated module is rescanned too, so every named "
            "marker the index holds comes from this build's rescans"
        ),
    )
    for path in (mini_project / "reexp" / "__init__.py", mini_project / "test_after_upgrade.py", unrelated):
        _rewrite(path, lambda source: source + "\n")
    summary = build_index(db_path, mini_project, project_id="proj")

    口 = Step(
        step_num=5,
        name="Assert the link resolves and the warning is gone",
        purpose="Nothing is left unresolved for the recipe to fix",
    )
    assert _missing_marker_warnings(summary) == [], summary.warnings
    assert _validated_targets(db_path, test_id) == [_node_id(db_path, "reexp/impl.py", "func")]


# ---------------------------------------------------------------------------
# Tier 1 — re-export closure walk internals
# ---------------------------------------------------------------------------


def _star_hops(relation: dict[str, list[str]]):
    """Adapt ``{module: [star sources]}`` to the walk's hop callable."""
    return lambda module, symbol: [(builder._STAR_HOP, source, symbol) for source in relation.get(module, [])]


def test_reexport_walk_terminates_on_a_cycle_and_respects_the_depth_bound():
    """Mutual re-exports terminate, and a chain longer than the bound is not followed."""
    cycle = {"a": ["b"], "b": ["a"]}
    assert builder.resolve_symbol_through_reexports("a", "thing", lambda _: False, _star_hops(cycle)) is None

    chain = {f"m{i}": [f"m{i + 1}"] for i in range(6)}
    defined = {"m5::thing"}
    assert (
        builder.resolve_symbol_through_reexports("m0", "thing", lambda n: n in defined, _star_hops(chain), max_depth=2)
        is None
    )
    assert (
        builder.resolve_symbol_through_reexports("m0", "thing", lambda n: n in defined, _star_hops(chain), max_depth=5)
        == "m5::thing"
    )


def test_reexport_walk_prefers_the_nearest_source_then_the_smallest_id():
    """A name exported by several sources resolves the same way on every build."""
    relation = {"root": ["zeta", "alpha"], "alpha": ["deep"]}
    defined = {"zeta::thing", "alpha::thing", "deep::thing"}

    assert (
        builder.resolve_symbol_through_reexports("root", "thing", lambda n: n in defined, _star_hops(relation))
        == "alpha::thing"
    )

    nearest_only = {"deep::thing"}
    assert (
        builder.resolve_symbol_through_reexports("root", "thing", lambda n: n in nearest_only, _star_hops(relation))
        == "deep::thing"
    )


def test_reexport_walk_prefers_a_named_binding_and_follows_its_original_name():
    """At one depth a named binding outranks a star source, and a named hop may rename the symbol."""
    hops = {("root", "alias"): [(builder._STAR_HOP, "alpha", "alias"), (builder._NAMED_HOP, "zeta", "real")]}
    defined = {"alpha::alias", "zeta::real"}

    assert (
        builder.resolve_symbol_through_reexports(
            "root", "alias", lambda n: n in defined, lambda module, symbol: hops.get((module, symbol), [])
        )
        == "zeta::real"
    )


def test_resolution_warns_only_when_unresolved_targets_meet_an_index_without_named_markers(mini_project, db_path):
    """An index holding no named re-export markers cannot resolve names, and must say how to fix it."""
    _write_fixture_family(mini_project)
    _write_unresolvable_module(mini_project)
    builder.build(mini_project, project_id="proj", discovery_only=False)

    orphan_id = _node_id(db_path, "pkgapp/unresolvable.py", "orphan")
    missing_target = f"{_module_node_id(db_path, 'pkgapp/runner.py')}::vanished_helper"
    unresolved = [
        make_edge("delegates_to", _step_id(orphan_id, "1"), missing_target),
        make_edge("validates", _node_id(db_path, "test_reexp_calls.py", "test_named"), missing_target),
    ]

    with_markers: list[str] = []
    builder._resolve_delegate_targets(db_path, list(unresolved), with_markers)
    assert with_markers == [], f"an index holding named markers must not warn: {with_markers}"

    # An index built before named markers existed carries at most the star
    # form, which is what dropping the named key reproduces.
    with db._connect(db_path) as conn:
        conn.execute(
            "UPDATE edges SET meta = json_remove(meta, '$.reexport_names') "
            "WHERE edge_type = 'depends_on' AND meta IS NOT NULL"
        )

    without_markers: list[str] = []
    builder._resolve_delegate_targets(db_path, list(unresolved), without_markers)

    assert len(without_markers) == 1, f"expected one warning, got {without_markers}"
    warning = without_markers[0]
    assert "1 delegate link(s)" in warning and "1 validates link(s)" in warning, f"warning must count both: {warning}"
    assert "UPDATE nodes SET file_mtime = NULL" in warning and "axiom-graph build" in warning, (
        f"warning must name the non-destructive remedy: {warning}"
    )
    assert "no star re-exports" not in warning, f"warning must not guess why markers are missing: {warning}"

    # No unresolved target, same marker-less index — nothing to warn about.
    resolvable_target = _node_id(db_path, "pkgapp/runner.py", "build_image")
    quiet: list[str] = []
    builder._resolve_delegate_targets(
        db_path, [make_edge("delegates_to", _step_id(orphan_id, "1"), resolvable_target)], quiet
    )
    assert quiet == [], f"a resolved target must not warn: {quiet}"


def test_external_stub_exclusion_is_structural_and_yields_to_a_module_named_external():
    """The exclusion covers stub ids only, and steps aside for a real ``external.py``."""
    assert _is_external_stub_id("proj::external::requests", set())
    # Deeper ids merely containing an ``external`` segment stay in the net.
    assert not _is_external_stub_id("proj::pkg.mod::external::step-1", set())
    assert not _is_external_stub_id("proj::pkg.external::helper", set())
    # A project with a top-level external.py mints ids of the stub's exact
    # shape, so the module node decides.
    assert not _is_external_stub_id("proj::external::helper", {"proj::external"})
