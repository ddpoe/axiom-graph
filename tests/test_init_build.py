"""Tests for the axiom-graph init / build split.

Covers the builder.build() entry point with discovery_only=True vs False,
project ID resolution, exclude_dirs, and the purge pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_annotations import Step, workflow

from axiom_graph.index import builder, db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_MODULE = '''\
"""A simple module."""


def greet(name: str) -> str:
    """Return a greeting."""
    return f"Hello, {name}"


def add(a: int, b: int) -> int:
    """Return the sum."""
    return a + b
'''

_EXTRA_MODULE = '''\
"""Extra module added after initial build."""


def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b
'''

_MODIFIED_MODULE = '''\
"""A simple module (modified)."""


def greet(name: str) -> str:
    """Return a greeting."""
    return f"Hi, {name}!"


def add(a: int, b: int) -> int:
    """Return the sum."""
    return a + b
'''

_TEST_MODULE = '''\
"""Tests for simple module."""

from simple import greet, add


def test_greet():
    assert greet("world") == "Hello, world"


def test_add():
    assert add(1, 2) == 3
'''


@pytest.fixture
def bare_project(tmp_path: Path) -> Path:
    """Return a tmp dir with NO .axiom_graph dir — suitable for init tests."""
    return tmp_path


# ===================================================================
# Tier 1 — Plain pytest (internal logic)
# ===================================================================


def test_build_creates_axiom_graph_dir_and_db(bare_project):
    """build() on a bare directory creates .axiom_graph/graph.db."""
    (bare_project / "hello.py").write_text(_SIMPLE_MODULE)

    builder.build(bare_project, project_id="t", discovery_only=False)

    db_path_ = bare_project / ".axiom_graph" / "graph.db"
    assert db_path_.exists()
    nodes = db.all_nodes(db_path_)
    assert len(nodes) > 0


def test_build_discovery_only_skips_existing_nodes(mini_project, db_path):
    """discovery_only=True skips nodes that already exist in the DB."""
    (mini_project / "mod.py").write_text(_SIMPLE_MODULE)

    r1 = builder.build(mini_project, project_id="t", discovery_only=False)
    assert r1["nodes_written"] > 0

    # Modify the module so code_hash would differ
    (mini_project / "mod.py").write_text(_MODIFIED_MODULE)

    r2 = builder.build(mini_project, project_id="t", discovery_only=True)
    # Existing nodes must be skipped, not rewritten
    assert r2["nodes_skipped"] >= r1["nodes_written"]


def test_build_full_resets_existing_nodes(mini_project, db_path):
    """discovery_only=False rewrites all nodes, resetting baselines."""
    (mini_project / "mod.py").write_text(_SIMPLE_MODULE)

    r1 = builder.build(mini_project, project_id="t", discovery_only=False)
    original_hash = db.get_node_hashes(db_path, "t::mod::greet")[0]

    # Modify module
    (mini_project / "mod.py").write_text(_MODIFIED_MODULE)

    r2 = builder.build(mini_project, project_id="t", discovery_only=False)
    assert r2["nodes_written"] > 0

    new_hash = db.get_node_hashes(db_path, "t::mod::greet")[0]
    assert new_hash != original_hash, "Full rebuild should reset code_hash baseline"


def test_build_project_id_fallback_chain(tmp_path):
    """Project ID: explicit > axiom-graph.toml > directory name."""
    (tmp_path / "a.py").write_text(_SIMPLE_MODULE)

    # Fallback to directory name
    builder.build(tmp_path, project_id=None, discovery_only=False)
    db_path_ = tmp_path / ".axiom_graph" / "graph.db"
    nodes = db.all_nodes(db_path_)
    assert any(n.id.startswith(f"{tmp_path.name}::") for n in nodes)

    # Reset DB and build with axiom-graph.toml
    db_path_.unlink()
    toml = '[axiom_graph]\nproject_id = "from_toml"\n'
    (tmp_path / "axiom-graph.toml").write_text(toml)
    builder.build(tmp_path, project_id=None, discovery_only=False)
    nodes = db.all_nodes(db_path_)
    assert any(n.id.startswith("from_toml::") for n in nodes)

    # Reset DB and build with explicit --id (overrides toml)
    db_path_.unlink()
    builder.build(tmp_path, project_id="explicit", discovery_only=False)
    nodes = db.all_nodes(db_path_)
    assert any(n.id.startswith("explicit::") for n in nodes)


def test_build_respects_exclude_dirs(tmp_path):
    """Files in excluded directories are not indexed."""
    (tmp_path / "good.py").write_text(_SIMPLE_MODULE)
    skip_dir = tmp_path / "vendor"
    skip_dir.mkdir()
    (skip_dir / "bad.py").write_text(_EXTRA_MODULE)

    toml = '[axiom_graph]\nproject_id = "t"\n\n[axiom_graph.scan]\nexclude_dirs = ["vendor"]\n'
    (tmp_path / "axiom-graph.toml").write_text(toml)

    builder.build(tmp_path, project_id="t", discovery_only=False)
    db_path_ = tmp_path / ".axiom_graph" / "graph.db"
    node_ids = {n.id for n in db.all_nodes(db_path_)}

    assert any("good" in nid for nid in node_ids)
    assert not any("bad" in nid or "vendor" in nid for nid in node_ids)


def test_build_purges_deleted_file_nodes(mini_project, db_path):
    """After removing a .py file, rebuild purges its nodes from the DB."""
    (mini_project / "keep.py").write_text(_SIMPLE_MODULE)
    (mini_project / "remove_me.py").write_text(_EXTRA_MODULE)

    builder.build(mini_project, project_id="t", discovery_only=False)
    assert db.get_node(db_path, "t::remove_me::multiply") is not None

    # Delete the file and rebuild
    (mini_project / "remove_me.py").unlink()
    r = builder.build(mini_project, project_id="t", discovery_only=True)

    assert r["nodes_purged"] > 0
    assert db.get_node(db_path, "t::remove_me::multiply") is None


def test_iter_python_files_skips_base_dirs(tmp_path):
    """_iter_python_files skips .git, __pycache__, .venv, etc."""
    (tmp_path / "good.py").write_text("x = 1\n")
    for d in [".git", "__pycache__", ".venv", "node_modules"]:
        p = tmp_path / d
        p.mkdir()
        (p / "bad.py").write_text("x = 1\n")

    files = list(builder._iter_python_files(tmp_path))
    names = [f.name for f in files]
    assert "good.py" in names
    assert "bad.py" not in names


def test_build_edges_always_updated_in_discovery_mode(mini_project, db_path):
    """Even in discovery_only=True, edges are refreshed."""
    (mini_project / "mod.py").write_text(_SIMPLE_MODULE)
    (mini_project / "test_mod.py").write_text(_TEST_MODULE)

    # Initial full build
    builder.build(mini_project, project_id="t", discovery_only=False)
    edges_before = db.all_edges(db_path)

    # Touch a file to force re-scan (mtime guard would skip unchanged files)
    mod_file = mini_project / "mod.py"
    mod_file.write_text(mod_file.read_text())

    # Discovery-only rebuild — edges should still be written for re-scanned files
    r = builder.build(mini_project, project_id="t", discovery_only=True)
    assert r["edges_written"] + r["edges_skipped"] > 0
    edges_after = db.all_edges(db_path)
    assert len(edges_after) >= len(edges_before)


# ===================================================================
# Tier 2 — @workflow(purpose=...) (subsystem tests)
# ===================================================================


@workflow(
    purpose=(
        "Verify the semantic contract between init (discovery_only=False) "
        "and build (discovery_only=True): init resets all baselines while "
        "build preserves staleness signals on existing nodes."
    ),
)
def test_init_build_separation(mini_project, db_path):
    """Init resets baselines; build preserves them."""
    (mini_project / "mod.py").write_text(_SIMPLE_MODULE)

    # Simulate init: full baseline
    builder.build(mini_project, project_id="t", discovery_only=False)
    hash_after_init = db.get_node_hashes(db_path, "t::mod::greet")[0]

    # Modify the file (code_hash would change if re-scanned)
    (mini_project / "mod.py").write_text(_MODIFIED_MODULE)

    # Simulate build: discovery-only — existing node NOT updated
    builder.build(mini_project, project_id="t", discovery_only=True)
    hash_after_build = db.get_node_hashes(db_path, "t::mod::greet")[0]
    assert hash_after_build == hash_after_init, "build (discovery_only) must NOT reset the code_hash baseline"

    # Simulate re-init: full reset — existing node IS updated
    builder.build(mini_project, project_id="t", discovery_only=False)
    hash_after_reinit = db.get_node_hashes(db_path, "t::mod::greet")[0]
    assert hash_after_reinit != hash_after_init, "init (full) must reset the code_hash baseline"


@workflow(
    purpose=(
        "Verify that the build summary dict accurately reflects "
        "nodes_written, nodes_skipped, edges_written, edges_skipped, "
        "and nodes_purged across add / modify / delete cycles."
    ),
)
def test_build_summary_counts_accurate(mini_project, db_path):
    """Summary counts are accurate across add/modify/delete."""
    (mini_project / "mod.py").write_text(_SIMPLE_MODULE)

    # First build: everything is new
    r1 = builder.build(mini_project, project_id="t", discovery_only=False)
    assert r1["nodes_written"] > 0
    assert r1["nodes_purged"] == 0

    # Second build (discovery-only, no changes): all skipped via mtime
    r2 = builder.build(mini_project, project_id="t", discovery_only=True)
    assert r2["files_skipped_mtime"] > 0  # mtime guard skips unchanged files
    assert r2["nodes_written"] == 0  # no new nodes

    # Add a new file, discovery build
    (mini_project / "extra.py").write_text(_EXTRA_MODULE)
    r3 = builder.build(mini_project, project_id="t", discovery_only=True)
    assert r3["nodes_written"] > 0  # new file picked up

    # Delete the extra file, rebuild
    (mini_project / "extra.py").unlink()
    r4 = builder.build(mini_project, project_id="t", discovery_only=True)
    assert r4["nodes_purged"] > 0


@workflow(
    purpose=(
        "Verify that axiom-graph.toml configuration is respected: "
        "project_id, exclude_dirs, and test_paths are all applied "
        "during a build."
    ),
)
def test_build_with_axiom_graph_toml_config(tmp_path):
    """axiom-graph.toml project_id, exclude_dirs, and test_paths are applied."""
    toml = '[axiom_graph]\nproject_id = "myproj"\n\n[axiom_graph.scan]\nexclude_dirs = ["scratch"]\ntest_paths = ["tests/"]\n'
    (tmp_path / "axiom-graph.toml").write_text(toml)
    (tmp_path / "core.py").write_text(_SIMPLE_MODULE)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "ignored.py").write_text(_EXTRA_MODULE)

    builder.build(tmp_path, discovery_only=False)
    db_path_ = tmp_path / ".axiom_graph" / "graph.db"
    node_ids = {n.id for n in db.all_nodes(db_path_)}

    # project_id from toml
    assert all(nid.startswith("myproj::") for nid in node_ids)
    # excluded dir
    assert not any("scratch" in nid or "ignored" in nid for nid in node_ids)


# ===================================================================
# Tier 3 — @workflow(purpose=...) + Step() (E2E user story)
# ===================================================================


@workflow(
    purpose=(
        "Full init → build lifecycle: initialise a project, verify baselines, "
        "add a file via discovery build, modify code without resetting baselines, "
        "delete a file and verify purge. Narrates the complete user workflow."
    ),
)
def test_init_then_build_lifecycle(tmp_path):
    """E2E: init → verify → add file via build → modify (no reset) → delete → purge."""

    口 = Step(
        step_num=1,
        name="Initialise the project",
        purpose="Run a full init on a project with one module",
        outputs=".axiom_graph/graph.db with nodes for greet() and add()",
    )
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    r_init = builder.build(tmp_path, project_id="lc", discovery_only=False)
    db_path_ = tmp_path / ".axiom_graph" / "graph.db"

    assert db_path_.exists()
    assert r_init["nodes_written"] > 0
    assert not r_init["warnings"]

    口 = Step(
        step_num=2,
        name="Record baselines",
        purpose="Capture the code_hash for greet() so we can verify it later",
        outputs="Baseline hash for greet()",
    )
    baseline_hash = db.get_node_hashes(db_path_, "lc::mod::greet")[0]
    assert baseline_hash is not None

    口 = Step(
        step_num=3,
        name="Add a new file and run discovery build",
        purpose="Simulate day-to-day development: new file should be indexed without resetting existing baselines",
        inputs="extra.py with multiply()",
        outputs="multiply() indexed; greet() baseline unchanged",
    )
    (tmp_path / "extra.py").write_text(_EXTRA_MODULE)
    r_build = builder.build(tmp_path, project_id="lc", discovery_only=True)

    assert r_build["nodes_written"] > 0, "New file nodes should be written"
    assert db.get_node(db_path_, "lc::extra::multiply") is not None

    hash_after_build = db.get_node_hashes(db_path_, "lc::mod::greet")[0]
    assert hash_after_build == baseline_hash, "Discovery build must NOT reset existing baselines"

    口 = Step(
        step_num=4,
        name="Modify existing code and discovery build",
        purpose="Change greet() implementation — discovery build should NOT update the hash",
        inputs="Modified mod.py",
        outputs="greet() code_hash unchanged (staleness signal preserved)",
    )
    (tmp_path / "mod.py").write_text(_MODIFIED_MODULE)
    builder.build(tmp_path, project_id="lc", discovery_only=True)

    hash_after_modify = db.get_node_hashes(db_path_, "lc::mod::greet")[0]
    assert hash_after_modify == baseline_hash, "Discovery build must preserve staleness signal on modified node"

    口 = Step(
        step_num=5,
        name="Delete a file and rebuild",
        purpose="Remove extra.py — rebuild should purge its nodes from the DB",
        outputs="multiply() node purged from DB",
    )
    (tmp_path / "extra.py").unlink()
    r_purge = builder.build(tmp_path, project_id="lc", discovery_only=True)

    assert r_purge["nodes_purged"] > 0, "Purge pass should remove nodes for deleted file"
    assert db.get_node(db_path_, "lc::extra::multiply") is None, "Deleted file's nodes must be purged"

    口 = Step(
        step_num=6,
        name="Re-init resets everything",
        purpose="Full re-init after code was modified should update the baseline hash",
        outputs="greet() code_hash updated to reflect modified source",
    )
    builder.build(tmp_path, project_id="lc", discovery_only=False)
    hash_after_reinit = db.get_node_hashes(db_path_, "lc::mod::greet")[0]
    assert hash_after_reinit != baseline_hash, "Re-init must reset baseline to current code"
