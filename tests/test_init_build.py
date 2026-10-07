"""Tests for the axiom-graph init / build split.

Covers the builder.build() entry point with discovery_only=True vs False,
project ID resolution, exclude_dirs, and the purge pass.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from axiom_annotations import Step, workflow

from axiom_graph.cli import main as cli
from axiom_graph.config import ProjectIdMismatchError
from axiom_graph.docjson.api import axiom_graph_update_section
from axiom_graph.project.api import DEFAULT_POLICY_SECTIONS, project_facts
from axiom_graph.index import builder, db
from axiom_graph.lifecycle.mcp_tools import axiom_graph_build as mcp_build


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


# ===================================================================
# The project id is stored, and every mismatch is refused
# ===================================================================


def _read_toml(project_root: Path) -> dict:
    """Parse *project_root*'s ``axiom-graph.toml``."""
    try:
        import tomllib  # noqa: PLC0415
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # noqa: PLC0415

    return tomllib.loads((project_root / "axiom-graph.toml").read_text(encoding="utf-8"))


def _node_ids(project_root: Path) -> set[str]:
    """Every node id in *project_root*'s index."""
    return {n.id for n in db.all_nodes(project_root / ".axiom_graph" / "graph.db")}


@workflow(
    purpose=(
        "Verify a project initialised with `init --id` keeps that id: init records it in "
        "axiom-graph.toml and the index, and a later `build` with no --id indexes nothing "
        "under the directory name"
    ),
)
def test_init_id_is_kept_by_later_builds(tmp_path):
    root = tmp_path / "folder_name"
    root.mkdir()
    (root / "mod.py").write_text(_SIMPLE_MODULE)

    result = CliRunner().invoke(cli, ["init", str(root), "--id", "demo"])
    assert result.exit_code == 0, result.output
    assert _read_toml(root)["axiom_graph"]["project_id"] == "demo"
    assert db.get_index_meta(root / ".axiom_graph" / "graph.db", "project_id") == "demo"

    (root / "mod.py").write_text(_MODIFIED_MODULE)
    result = CliRunner().invoke(cli, ["build", str(root)])
    assert result.exit_code == 0, result.output

    ids = _node_ids(root)
    assert "demo::mod::greet" in ids
    assert not [i for i in ids if i.startswith("folder_name::")], ids


@pytest.mark.parametrize(
    "existing",
    [
        None,
        '# my project\n[axiom_graph]\ndb_path = ".axiom_graph/graph.db"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs"]\n',
        '# my project\n[axiom_graph.scan]\ndocs_dirs = ["docs"]\n\n[tool.other]\nkey = 1\n',
    ],
    ids=["no-toml", "table-without-id", "no-axiom-graph-table"],
)
def test_init_id_writes_project_id_and_keeps_the_rest_of_the_toml(tmp_path, existing):
    """``init --id`` adds ``project_id`` under ``[axiom_graph]`` and leaves every other key as it was."""
    if existing is not None:
        (tmp_path / "axiom-graph.toml").write_text(existing, encoding="utf-8")
    before = _read_toml(tmp_path) if existing is not None else {}

    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", "demo"])
    assert result.exit_code == 0, result.output

    after = _read_toml(tmp_path)
    assert after["axiom_graph"].pop("project_id") == "demo"
    if not after["axiom_graph"]:
        after.pop("axiom_graph")
    assert after == before
    if existing is not None:
        assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8").startswith("# my project\n")


@workflow(
    purpose=(
        "Verify `init --id` refuses, before its prompt and before touching the index, when "
        "axiom-graph.toml already holds a different project id, naming both ids"
    ),
)
def test_init_id_refuses_a_different_toml_project_id(tmp_path):
    toml = '[axiom_graph]\nproject_id = "kept"\n'
    (tmp_path / "axiom-graph.toml").write_text(toml, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(tmp_path, discovery_only=False)
    ids_before = _node_ids(tmp_path)

    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", "other"], input="y\n")

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), f"the refusal escaped as {result.exception!r}"
    assert result.stderr.startswith("Error: "), result.stderr
    assert "'kept'" in result.stderr and "'other'" in result.stderr
    assert "axiom-graph.toml" in result.stderr
    assert "Continue?" not in result.output, "the refusal comes before the re-initialise prompt"
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == toml
    assert _node_ids(tmp_path) == ids_before


@workflow(
    purpose=(
        "Verify `build` refuses an id that differs from the one the index stores -- whether it "
        "came from --id or from axiom-graph.toml -- with a clean error naming both ids, how to "
        "fix it, and that renaming is not supported, and indexes nothing"
    ),
)
@pytest.mark.parametrize("source", ["flag", "toml"])
def test_build_refuses_a_project_id_the_index_was_not_built_with(tmp_path, source):
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(tmp_path, project_id="stored", discovery_only=False)
    (tmp_path / "extra.py").write_text(_EXTRA_MODULE)
    ids_before = _node_ids(tmp_path)

    args = ["build", str(tmp_path)]
    if source == "flag":
        args += ["--id", "other"]
    else:
        (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "other"\n', encoding="utf-8")
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), f"the refusal escaped as {result.exception!r}"
    message = result.stderr
    assert message.startswith("Error: "), message
    assert "'stored'" in message and "'other'" in message
    assert 'project_id = "stored"' in message, "the fix names the id to put back"
    assert "--id stored" in message
    assert "not supported" in message
    assert _node_ids(tmp_path) == ids_before


def test_build_resolves_the_stored_id_before_the_directory_name(tmp_path):
    """With no --id and no axiom-graph.toml, a build keeps the id the index stores."""
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(tmp_path, project_id="stored", discovery_only=False)
    (tmp_path / "extra.py").write_text(_EXTRA_MODULE)

    builder.build(tmp_path, discovery_only=True)

    ids = _node_ids(tmp_path)
    assert "stored::extra::multiply" in ids
    assert not [i for i in ids if i.startswith(f"{tmp_path.name}::")], ids


def _legacy_index(project_root: Path, project_id: str) -> Path:
    """Index *project_root* under *project_id*, then strip the stored id, as an index from before ids were stored."""
    (project_root / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(project_root, project_id=project_id, discovery_only=False)
    db_path_ = project_root / ".axiom_graph" / "graph.db"
    conn = sqlite3.connect(db_path_)
    conn.execute("DROP TABLE index_meta")
    conn.commit()
    conn.close()
    assert db.get_index_meta(db_path_, "project_id") is None
    return db_path_


def test_index_without_a_stored_id_keeps_the_prefix_its_nodes_share(tmp_path):
    """An index from before ids were stored keeps its nodes' one prefix rather than the directory name."""
    db_path_ = _legacy_index(tmp_path, "demo")
    (tmp_path / "extra.py").write_text(_EXTRA_MODULE)

    builder.build(tmp_path, discovery_only=True)

    assert db.get_index_meta(db_path_, "project_id") == "demo"
    ids = _node_ids(tmp_path)
    assert "demo::extra::multiply" in ids
    assert not [i for i in ids if not i.startswith("demo::")], ids


@pytest.mark.parametrize("source", ["flag", "toml"])
def test_index_without_a_stored_id_refuses_a_different_id_and_writes_nothing(tmp_path, source):
    """The prefix an unstamped index's nodes share counts as its id: a different --id or toml id is refused."""
    db_path_ = _legacy_index(tmp_path, "demo")
    (tmp_path / "extra.py").write_text(_EXTRA_MODULE)
    ids_before = _node_ids(tmp_path)
    explicit_id = "other" if source == "flag" else None
    if source == "toml":
        (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "other"\n', encoding="utf-8")

    with pytest.raises(ProjectIdMismatchError) as exc:
        builder.build(tmp_path, project_id=explicit_id, discovery_only=True)

    assert "'demo'" in str(exc.value) and "'other'" in str(exc.value)
    assert _node_ids(tmp_path) == ids_before
    assert db.get_index_meta(db_path_, "project_id") is None


def test_index_with_mixed_prefixes_and_no_stored_id_adopts_the_resolved_id(tmp_path):
    """An unstamped index whose nodes already carry two prefixes has no id to keep; the build's id is recorded."""
    db_path_ = _legacy_index(tmp_path, "demo")
    conn = sqlite3.connect(db_path_)
    conn.execute("UPDATE nodes SET id = 'stray::mod::add' WHERE id = 'demo::mod::add'")
    conn.commit()
    conn.close()

    builder.build(tmp_path, project_id="fresh", discovery_only=True)

    assert db.get_index_meta(db_path_, "project_id") == "fresh"


def test_empty_project_id_counts_as_unset(tmp_path):
    """``project_id = ""`` in the toml, or an empty --id, falls through to the directory name."""
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = ""\n', encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)

    builder.build(tmp_path, project_id="", discovery_only=False)

    ids = _node_ids(tmp_path)
    assert f"{tmp_path.name}::mod::greet" in ids
    assert not [i for i in ids if i.startswith("::")], ids

    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", "demo"], input="y\n")
    assert result.exit_code == 0, result.output
    assert _read_toml(tmp_path)["axiom_graph"]["project_id"] == "demo"
    assert "demo::mod::greet" in _node_ids(tmp_path)


def test_init_with_an_empty_id_counts_it_as_unset(tmp_path):
    """``init --id ""`` falls through to the directory name, which is the id recorded in the toml."""
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)

    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", ""])

    assert result.exit_code == 0, result.output
    assert f"{tmp_path.name}::mod::greet" in _node_ids(tmp_path)
    assert _read_toml(tmp_path) == {"axiom_graph": {"project_id": tmp_path.name}}


@workflow(
    purpose=(
        "Verify `init` without --id re-initialises an index under the id it already stores, and "
        "records that id in axiom-graph.toml, instead of re-namespacing it to the directory name"
    ),
)
def test_init_without_id_keeps_the_stored_id(tmp_path):
    root = tmp_path / "folder_name"
    root.mkdir()
    (root / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(root, project_id="demo", discovery_only=False)

    result = CliRunner().invoke(cli, ["init", str(root)], input="y\n")

    assert result.exit_code == 0, result.output
    ids = _node_ids(root)
    assert "demo::mod::greet" in ids
    assert not [i for i in ids if i.startswith("folder_name::")], ids
    assert _read_toml(root)["axiom_graph"]["project_id"] == "demo"


def test_init_id_keeps_crlf_line_endings_in_the_toml(tmp_path):
    """Adding ``project_id`` to a CRLF toml keeps every line CRLF."""
    toml_path = tmp_path / "axiom-graph.toml"
    toml_path.write_bytes(b'[axiom_graph]\r\ndb_path = ".axiom_graph/graph.db"\r\n\r\n[axiom_graph.scan]\r\n')

    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", "demo"])

    assert result.exit_code == 0, result.output
    raw = toml_path.read_bytes()
    assert b'project_id = "demo"\r\n' in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), raw


def test_init_leaves_the_toml_alone_when_the_old_index_cannot_be_removed(tmp_path, monkeypatch):
    """A failed delete of the old index (e.g. a locked file) happens before the toml is written."""
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(tmp_path, project_id="t", discovery_only=False)

    def _locked(self, *args, **kwargs):
        raise PermissionError(f"locked: {self}")

    monkeypatch.setattr(Path, "unlink", _locked)
    result = CliRunner().invoke(cli, ["init", str(tmp_path), "--id", "t"], input="y\n")

    assert isinstance(result.exception, PermissionError), result.output
    assert not (tmp_path / "axiom-graph.toml").exists()


def test_mcp_build_reports_a_project_id_mismatch_as_an_error_string(tmp_path):
    """The MCP build tool answers a mismatch with an ``ERROR:`` line, not an exception."""
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    builder.build(tmp_path, project_id="stored", discovery_only=False)
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "other"\n', encoding="utf-8")

    out = mcp_build(str(tmp_path))

    assert out.startswith("ERROR: "), out
    assert "'stored'" in out and "'other'" in out


# ===================================================================
# Linked staleness stored by the build that detects the change
# ===================================================================

_LINKED_TOML = '[axiom_graph]\nproject_id = "demo"\n\n[axiom_graph.staleness]\nfrozen_tags = ["adr"]\n'
_F_ID = "demo::pkg.mod::f"
_GUIDE_SECTION_ID = "demo::docs/guide::f"
_ADR_SECTION_ID = "demo::docs/adr::f"
_TEST_F_ID = "demo::tests.test_mod::test_f"


def _write_doc(path: Path, *, title: str, content: str, tags: list[str] | None = None) -> None:
    """Write a one-section DocJSON document whose section links ``f``."""
    doc = {
        "title": title,
        "tags": tags or [],
        "sections": [{"id": "f", "heading": "f", "content": content, "links": [{"node_id": _F_ID}]}],
    }
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _linked_project(root: Path) -> None:
    """Create a project where a doc section documents ``f`` and a test validates it.

    A second, frozen-tagged document links ``f`` too, so the frozen filter
    has a section to act on.
    """
    (root / "axiom-graph.toml").write_text(_LINKED_TOML, encoding="utf-8")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_mod.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8"
    )
    docs_dir = root / "docs"
    docs_dir.mkdir()
    _write_doc(docs_dir / "guide.docjson", title="Guide", content="f returns one.")
    _write_doc(docs_dir / "adr.docjson", title="ADR", content="We chose f.", tags=["adr"])


def _stored_statuses(root: Path) -> dict[str, tuple[str, str]]:
    """Read the persisted (own_status, link_status) pairs without recomputing."""
    return db.get_all_staleness(root / ".axiom_graph" / "graph.db")


def _check_line(root: Path) -> str:
    """Return the one-line summary ``axiom-graph check`` prints first."""
    result = CliRunner().invoke(cli, ["check", str(root)])
    assert result.exit_code == 0, result.output
    return next(line for line in result.output.splitlines() if line.startswith("own: "))


@workflow(
    purpose="A build stores the LINKED_STALE that the code change it detects causes on the doc section "
    "documenting the code and the test validating it, without a check in between",
)
def test_build_stores_linked_stale_for_the_change_it_detects(tmp_path: Path) -> None:
    _linked_project(tmp_path)
    runner = CliRunner()
    init = runner.invoke(cli, ["init", str(tmp_path)])
    assert init.exit_code == 0, init.output
    stored = _stored_statuses(tmp_path)
    assert stored[_GUIDE_SECTION_ID] == ("VERIFIED", "VERIFIED")
    assert stored[_TEST_F_ID] == ("VERIFIED", "VERIFIED")

    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    build = runner.invoke(cli, ["build", str(tmp_path)])
    assert build.exit_code == 0, build.output

    stored = _stored_statuses(tmp_path)
    assert stored[_F_ID][0] == "CONTENT_UPDATED"
    assert stored[_GUIDE_SECTION_ID][1] == "LINKED_STALE"
    assert stored[_TEST_F_ID][1] == "LINKED_STALE"

    _check_line(tmp_path)
    assert _stored_statuses(tmp_path) == stored


def test_build_counts_a_test_added_with_the_code_change_as_verified(tmp_path: Path) -> None:
    """A test first indexed by the build that sees its target change is not linked-stale."""
    _linked_project(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(tmp_path)]).exit_code == 0

    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    (tmp_path / "tests" / "test_more.py").write_text(
        "from pkg.mod import f\n\n\ndef test_f_again():\n    assert f() == 2\n", encoding="utf-8"
    )
    build = runner.invoke(cli, ["build", str(tmp_path)])
    assert build.exit_code == 0, build.output

    new_test = "demo::tests.test_more::test_f_again"
    assert _stored_statuses(tmp_path)[new_test] == ("VERIFIED", "VERIFIED")
    assert _stored_statuses(tmp_path)[_TEST_F_ID][1] == "LINKED_STALE"
    _check_line(tmp_path)  # run check only for its recompute; the stored row must survive it
    assert _stored_statuses(tmp_path)[new_test] == ("VERIFIED", "VERIFIED")


@workflow(
    purpose="The staleness counts axiom-graph build prints are the counts axiom-graph check reports: "
    "frozen-doc sections excluded, LINKED_STALE included",
)
def test_build_prints_the_counts_check_reports(tmp_path: Path) -> None:
    _linked_project(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", str(tmp_path)]).exit_code == 0

    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    _write_doc(tmp_path / "docs" / "adr.docjson", title="ADR", content="We chose f, edited by hand.", tags=["adr"])
    build = runner.invoke(cli, ["build", str(tmp_path)])
    assert build.exit_code == 0, build.output

    assert _stored_statuses(tmp_path)[_ADR_SECTION_ID] != ("VERIFIED", "VERIFIED")
    check_line = _check_line(tmp_path)
    assert "0 LINKED_STALE" not in check_line
    assert f"  staleness     : {check_line}" in build.output.splitlines()

    from axiom_graph.lifecycle.mcp_tools import axiom_graph_build

    assert f"  staleness       : {check_line}" in axiom_graph_build(str(tmp_path)).splitlines()


# ===================================================================
# Linked staleness stored by the check that detects the change
# ===================================================================


def test_check_stores_linked_stale_for_the_change_it_detects(tmp_path: Path) -> None:
    """One check after a code edit, with no build, stores the LINKED_STALE the edit causes.

    The doc section documenting the edited function and the test validating
    it are LINKED_STALE after the first check, and a second check stores the
    same statuses.
    """
    _linked_project(tmp_path)
    assert CliRunner().invoke(cli, ["init", str(tmp_path)]).exit_code == 0

    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    check_line = _check_line(tmp_path)

    stored = _stored_statuses(tmp_path)
    assert stored[_F_ID][0] == "CONTENT_UPDATED"
    assert stored[_GUIDE_SECTION_ID][1] == "LINKED_STALE"
    assert stored[_TEST_F_ID][1] == "LINKED_STALE"
    assert "0 LINKED_STALE" not in check_line

    assert _check_line(tmp_path) == check_line
    assert _stored_statuses(tmp_path) == stored


def test_check_summary_counts_linked_stale_for_the_change_it_detects(tmp_path: Path) -> None:
    """The shared check summary (CLI and MCP) counts the edit's LINKED_STALE on its first run."""
    from axiom_graph.lifecycle.api import compute_check_summary

    _linked_project(tmp_path)
    assert CliRunner().invoke(cli, ["init", str(tmp_path)]).exit_code == 0
    db_path = tmp_path / ".axiom_graph" / "graph.db"

    (tmp_path / "pkg" / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    first = compute_check_summary(db_path, tmp_path)

    assert first is not None
    assert first.statuses[_GUIDE_SECTION_ID][1] == "LINKED_STALE"
    assert first.statuses[_TEST_F_ID][1] == "LINKED_STALE"
    stored = _stored_statuses(tmp_path)
    assert stored[_GUIDE_SECTION_ID][1] == "LINKED_STALE"
    assert stored[_TEST_F_ID][1] == "LINKED_STALE"

    second = compute_check_summary(db_path, tmp_path)
    assert second is not None
    assert second.link_counts == first.link_counts
    assert _stored_statuses(tmp_path) == stored


@pytest.mark.parametrize(
    ("toml_id", "stored_id", "expected"),
    [("from_toml", "stored", "from_toml"), (None, "stored", "stored"), (None, None, "folder_name")],
    ids=["toml-wins", "stored-id", "directory-name"],
)
def test_resolve_project_id_prefers_toml_then_stored_id_then_directory(tmp_path, toml_id, stored_id, expected):
    """Commands other than build resolve the toml's id, then the index's stored id, then the directory name."""
    root = tmp_path / "folder_name"
    root.mkdir()
    if stored_id is not None:
        (root / "mod.py").write_text(_SIMPLE_MODULE)
        builder.build(root, project_id=stored_id, discovery_only=False)
    if toml_id is not None:
        (root / "axiom-graph.toml").write_text(f'[axiom_graph]\nproject_id = "{toml_id}"\n', encoding="utf-8")

    assert builder.resolve_project_id(root) == expected
    if stored_id is None:
        assert not (root / ".axiom_graph").exists(), "resolving never creates an index"


def test_init_without_id_records_the_directory_name_in_the_toml(tmp_path):
    """A plain ``init`` on a project with no toml records the id it indexed under, so later commands agree on it."""
    root = tmp_path / "folder_name"
    root.mkdir()
    (root / "mod.py").write_text(_SIMPLE_MODULE)

    result = CliRunner().invoke(cli, ["init", str(root)])

    assert result.exit_code == 0, result.output
    assert _read_toml(root) == {"axiom_graph": {"project_id": "folder_name"}}
    assert "folder_name::mod::greet" in _node_ids(root)


_CUSTOM_TOML = (
    '[axiom_graph]\nproject_id = "kept"\n\n'
    '[axiom_graph.scan]\nexclude_dirs = ["vendor"]\ndocs_dirs = ["docs"]\n\n'
    "[axiom_graph.thresholds]\nmax_function_lines = 120\n\n"
    "[tool.other]\nkey = 1\n"
)


def _index_state(root: Path) -> tuple[set[str], list[tuple], list[tuple]]:
    """Node ids, history rows and verification rows outside the policy doc: what a rebuild would replace."""
    conn = sqlite3.connect(root / ".axiom_graph" / "graph.db")
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE id NOT LIKE '%agent-policy%'")}
        history = conn.execute(
            "SELECT id, node_id, change_type FROM node_history WHERE node_id NOT LIKE '%agent-policy%' ORDER BY id"
        ).fetchall()
        verified = conn.execute(
            "SELECT node_id, verified_at, verified_by FROM node_verification "
            "WHERE node_id NOT LIKE '%agent-policy%' ORDER BY node_id"
        ).fetchall()
    finally:
        conn.close()
    return ids, history, verified


def _built_with_a_marker(root: Path, *, init: bool = False) -> tuple[set[str], list[tuple], list[tuple]]:
    """Index *root* (``build``, or ``init`` to seed the policy), mark a node clean and return the index state."""
    if init:
        result = CliRunner().invoke(cli, ["init", str(root)])
        assert result.exit_code == 0, result.output
    else:
        builder.build(root, discovery_only=False)
    marked = CliRunner().invoke(
        cli, ["mark-clean", f"{_read_toml(root)['axiom_graph']['project_id']}::mod::greet", str(root)]
    )
    assert marked.exit_code == 0, marked.output
    state = _index_state(root)
    assert any(change == "MANUAL_VERIFIED" for _id, _node, change in state[1]), state[1]
    return state


@workflow(
    purpose=(
        "Verify `init --settings` lists each axiom-graph.toml setting that differs from the defaults with its "
        "default and asks; yes or --yes resets the file keeping the project id and says a build applies it, no "
        "or no terminal leaves it byte-for-byte, and the index is untouched every time"
    ),
)
@pytest.mark.parametrize(
    ("terminal", "args", "answer", "resets"),
    [
        (True, [], "y\n", True),
        (False, ["--yes"], "", True),
        (True, [], "n\n", False),
        (False, [], "", False),
    ],
    ids=["yes", "--yes", "no", "no-terminal"],
)
def test_init_settings_offers_to_reset_a_toml_that_differs_from_the_defaults(
    tmp_path, monkeypatch, terminal, args, answer, resets
):
    口 = Step(step_num=1, name="Build a project with a customised toml", purpose="Record the index state first")
    monkeypatch.setattr("axiom_graph.cli.indexing._stdin_is_a_terminal", lambda: terminal)
    (tmp_path / "axiom-graph.toml").write_text(_CUSTOM_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    state = _built_with_a_marker(tmp_path)

    口 = Step(step_num=2, name="Run init --settings", purpose="It lists the differing settings, then asks")
    result = CliRunner().invoke(cli, ["init", "--settings", *args, str(tmp_path)], input=answer)

    assert result.exit_code == 0, result.output
    assert 'axiom_graph.scan.exclude_dirs = ["vendor"]  (default: [])' in result.output
    assert "axiom_graph.thresholds.max_function_lines = 120  (default: 80)" in result.output
    assert "tool.other.key = 1  (default: none, the key is dropped)" in result.output
    assert "axiom_graph.scan.docs_dirs" not in result.output, "a setting at its default is not a change"
    assert "Index already exists" not in result.output

    口 = Step(step_num=3, name="Check the outcome", purpose="Only a yes resets the toml; the index never changes")
    if args:
        assert "Reset axiom-graph.toml" not in result.output, "--yes does not ask"
    elif terminal:
        assert 'project_id stays "kept"' in result.output
    else:
        assert "Not resetting axiom-graph.toml: no terminal to ask" in result.output
    if resets:
        assert _read_toml(tmp_path) == {"axiom_graph": {"project_id": "kept"}}
        assert "axiom-graph build" in result.output
    else:
        assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == _CUSTOM_TOML
    assert _index_state(tmp_path) == state


_TWO_ROOT_TOML = (
    '[axiom_graph]\nproject_id = "kept"\n\n'
    '[axiom_graph.scan]\nexclude_dirs = ["vendor"]\ndocs_dirs = ["handbook", "docs"]\n\n'
    "[axiom_graph.thresholds]\nmax_function_lines = 120\n"
)
_POLICY_DOC_ID = "kept::handbook/agent-policy"


def _policy_doc_ids(root: Path) -> list[str]:
    """The ids of the docs in *root*'s index tagged ``agent-policy``."""
    conn = sqlite3.connect(root / ".axiom_graph" / "graph.db")
    try:
        rows = conn.execute("SELECT id, tags FROM docs").fetchall()
    finally:
        conn.close()
    return sorted(doc_id for doc_id, tags in rows if "agent-policy" in json.loads(tags or "[]"))


@workflow(
    purpose=(
        "Plain init resets only the index: it never asks about a customised axiom-graph.toml, seeds the "
        "shipped agent policy as an indexed, verified doc in the primary docs root, says on re-init what "
        "it resets and keeps, and keeps an edited policy doc across the re-init"
    ),
)
def test_plain_init_seeds_the_policy_and_keeps_the_toml_and_an_edited_policy(tmp_path):
    口 = Step(
        step_num=1,
        name="Initialise a project with a customised toml",
        purpose="No toml question; the toml is unchanged; the last line names the policy doc and the reset flags",
    )
    root = tmp_path / "proj"
    root.mkdir()
    (root / "axiom-graph.toml").write_text(_TWO_ROOT_TOML, encoding="utf-8")
    (root / "mod.py").write_text(_SIMPLE_MODULE)

    result = CliRunner().invoke(cli, ["init", str(root)])

    assert result.exit_code == 0, result.output
    assert "differ from the defaults" not in result.output
    assert "Reset axiom-graph.toml" not in result.output
    assert (root / "axiom-graph.toml").read_text(encoding="utf-8") == _TWO_ROOT_TOML
    last = result.output.strip().splitlines()[-1]
    assert "handbook/agent-policy.docjson" in last
    assert "init --policy" in last
    assert "init --settings" in last

    口 = Step(
        step_num=2,
        name="Check the seeded doc",
        purpose="In the primary root with the primary extension, tagged, indexed, verified, and what info renders",
    )
    assert (root / "handbook" / "agent-policy.docjson").is_file()
    assert not (root / "docs").exists()
    assert _policy_doc_ids(root) == [_POLICY_DOC_ID]
    statuses = db.get_all_staleness(root / ".axiom_graph" / "graph.db")
    sections = {nid: s for nid, s in statuses.items() if nid.startswith(f"{_POLICY_DOC_ID}::")}
    assert sections, "the seeded doc's sections are indexed"
    assert all(own == "VERIFIED" for own, _link in sections.values()), sections
    facts = project_facts(root)
    assert facts.policy.doc_id == _POLICY_DOC_ID

    口 = Step(
        step_num=3,
        name="Edit the policy and decline a re-init",
        purpose="The confirm says what init resets and keeps and names the reset flags; no changes nothing",
    )
    edited = axiom_graph_update_section(
        str(root), f"{_POLICY_DOC_ID}::start-here", content="Our own rule: read the house style first."
    )
    assert not edited.startswith("ERROR"), edited
    ids_before = _node_ids(root)

    declined = CliRunner().invoke(cli, ["init", str(root)], input="n\n")

    assert declined.exit_code == 1, declined.output
    confirm = declined.output
    assert "resets the index" in confirm
    assert "keeps the axiom-graph.toml settings and the agent-policy doc" in confirm
    for flag in ("axiom-graph init --settings", "axiom-graph init --policy", "axiom-graph init --all"):
        assert flag in confirm, flag
    assert (root / "axiom-graph.toml").read_text(encoding="utf-8") == _TWO_ROOT_TOML
    assert _node_ids(root) == ids_before

    口 = Step(
        step_num=4,
        name="Confirm the re-init",
        purpose="The edited policy survives, no second policy doc appears, and the toml is untouched",
    )
    again = CliRunner().invoke(cli, ["init", str(root)], input="y\n")

    assert again.exit_code == 0, again.output
    assert "Agent policy written" not in again.output
    assert _policy_doc_ids(root) == [_POLICY_DOC_ID]
    assert "read the house style first" in (root / "handbook" / "agent-policy.docjson").read_text(encoding="utf-8")
    assert any(s.content == "Our own rule: read the house style first." for s in project_facts(root).policy.sections)
    assert (root / "axiom-graph.toml").read_text(encoding="utf-8") == _TWO_ROOT_TOML


@pytest.mark.parametrize(
    ("toml", "note"),
    [
        (
            '[axiom_graph]\nproject_id = "p"\n',
            "docs/agent-policy.docjson exists and is not tagged agent-policy.",
        ),
        (
            '[axiom_graph]\nproject_id = "p"\n\n[axiom_graph.scan]\ndocs_dirs = []\n',
            "axiom-graph.toml configures no docs_dirs to write it in.",
        ),
    ],
    ids=["untagged-file", "no-docs-dirs"],
)
def test_init_skips_the_policy_seed_with_a_note(tmp_path, toml, note):
    """Init writes no policy over an untagged file at the target, nor without a docs root, and says why."""
    (tmp_path / "axiom-graph.toml").write_text(toml, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    docs = tmp_path / "docs"
    docs.mkdir()
    untagged = docs / "agent-policy.docjson"
    untagged.write_text(
        json.dumps({"title": "Ours", "sections": [{"id": "a", "heading": "A", "content": "Mine."}]}),
        encoding="utf-8",
    )
    before = untagged.read_text(encoding="utf-8")

    result = CliRunner().invoke(cli, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output.strip().splitlines()[-1] == f"No agent policy written: {note}"
    assert untagged.read_text(encoding="utf-8") == before
    assert _policy_doc_ids(tmp_path) == []


def test_init_settings_has_nothing_to_reset_when_the_toml_holds_only_defaults(tmp_path):
    """A toml whose settings all equal the defaults has nothing to reset: --settings says so and asks nothing."""
    toml = '[axiom_graph]\nproject_id = "kept"\n\n[axiom_graph.scan]\ndocs_dirs = ["docs"]\n'
    (tmp_path / "axiom-graph.toml").write_text(toml, encoding="utf-8")

    result = CliRunner().invoke(cli, ["init", "--settings", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "nothing to reset" in result.output
    assert "Reset axiom-graph.toml" not in result.output
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == toml
    assert not (tmp_path / ".axiom_graph").exists(), "--settings never creates an index"


# ===================================================================
# init --policy / --settings together / --all
# ===================================================================

_DEFAULT_TEXT = DEFAULT_POLICY_SECTIONS[1][2]
_EDIT = "Our own rule: read the house style first."


def _edit_policy(root: Path, doc_id: str) -> None:
    """Edit the policy doc's first section through the doc write path."""
    edited = axiom_graph_update_section(str(root), f"{doc_id}::start-here", content=_EDIT)
    assert not edited.startswith("ERROR"), edited


@workflow(
    purpose=(
        "Verify `init --policy` on a project indexed before plain init seeded a policy writes the shipped "
        "default as an indexed doc that info shows, with no delete prompt and the rest of the index intact"
    ),
)
def test_init_policy_writes_the_default_when_the_project_has_none(tmp_path):
    口 = Step(step_num=1, name="Index with build alone", purpose="build never seeds, so there is no policy doc")
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "p"\n', encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    state = _built_with_a_marker(tmp_path)
    assert project_facts(tmp_path).policy.is_default

    口 = Step(step_num=2, name="Run init --policy", purpose="It writes the default without asking anything")
    result = CliRunner().invoke(cli, ["init", "--policy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Index already exists" not in result.output
    assert "docs/agent-policy.docjson" in result.output

    口 = Step(step_num=3, name="Check the doc and the index", purpose="info shows the doc; nothing else changed")
    policy = project_facts(tmp_path).policy
    assert policy.doc_id == "p::docs/agent-policy"
    assert any(s.content == _DEFAULT_TEXT for s in policy.sections)
    assert _index_state(tmp_path) == state


@workflow(
    purpose=(
        "Verify `init --policy` names the existing policy doc and asks; yes or --yes restores the shipped default "
        "under the same id, verified, no or no terminal keeps the edit, and the index is untouched every time"
    ),
)
@pytest.mark.parametrize(
    ("terminal", "args", "answer", "restores"),
    [
        (True, [], "y\n", True),
        (False, ["--yes"], "", True),
        (True, [], "n\n", False),
        (False, [], "", False),
    ],
    ids=["yes", "--yes", "no", "no-terminal"],
)
def test_init_policy_asks_before_overwriting_the_policy_doc(tmp_path, monkeypatch, terminal, args, answer, restores):
    monkeypatch.setattr("axiom_graph.cli.indexing._stdin_is_a_terminal", lambda: terminal)
    (tmp_path / "axiom-graph.toml").write_text('[axiom_graph]\nproject_id = "p"\n', encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    state = _built_with_a_marker(tmp_path, init=True)
    _edit_policy(tmp_path, "p::docs/agent-policy")

    result = CliRunner().invoke(cli, ["init", "--policy", *args, str(tmp_path)], input=answer)

    assert result.exit_code == 0, result.output
    assert "Index already exists" not in result.output
    if not args:
        assert "p::docs/agent-policy" in result.output, "the prompt names the doc"
    contents = [s.content for s in project_facts(tmp_path).policy.sections]
    assert project_facts(tmp_path).policy.doc_id == "p::docs/agent-policy"
    if restores:
        assert _DEFAULT_TEXT in contents and _EDIT not in contents
        statuses = db.get_all_staleness(tmp_path / ".axiom_graph" / "graph.db")
        policy_rows = [s for nid, s in statuses.items() if nid.startswith("p::docs/agent-policy")]
        assert policy_rows and all(own == "VERIFIED" for own, _link in policy_rows), policy_rows
    else:
        assert _EDIT in contents
    assert _policy_doc_ids(tmp_path) == ["p::docs/agent-policy"]
    assert _index_state(tmp_path) == state


@workflow(
    purpose=(
        "Verify `init --settings --policy --yes` resets axiom-graph.toml and the policy doc with one --yes, never "
        "prompts to delete the index, and leaves the index intact"
    ),
)
def test_init_settings_and_policy_with_yes_reset_both_and_leave_the_index(tmp_path):
    (tmp_path / "axiom-graph.toml").write_text(_CUSTOM_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    state = _built_with_a_marker(tmp_path, init=True)
    _edit_policy(tmp_path, "kept::docs/agent-policy")

    result = CliRunner().invoke(cli, ["init", "--settings", "--policy", "--yes", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Index already exists" not in result.output
    assert _read_toml(tmp_path) == {"axiom_graph": {"project_id": "kept"}}
    assert any(s.content == _DEFAULT_TEXT for s in project_facts(tmp_path).policy.sections)
    assert _index_state(tmp_path) == state


@workflow(
    purpose=(
        "Verify `init --settings --policy` without an index fails before either prompt, so the --settings half "
        "never runs: the toml is unchanged and no index is created"
    ),
)
def test_init_settings_and_policy_without_an_index_change_nothing(tmp_path):
    (tmp_path / "axiom-graph.toml").write_text(_CUSTOM_TOML, encoding="utf-8")

    result = CliRunner().invoke(cli, ["init", "--settings", "--policy", "--yes", str(tmp_path)])

    assert result.exit_code != 0
    assert "axiom-graph init" in result.output
    assert "differ from the defaults" not in result.output
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == _CUSTOM_TOML
    assert not (tmp_path / ".axiom_graph").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--yes"],
        ["--policy", "--id", "other"],
        ["--settings", "--id", "other"],
        ["--all", "--yes"],
        ["--all", "--policy"],
    ],
    ids=["yes-alone", "policy-id", "settings-id", "all-yes", "all-policy"],
)
def test_init_refuses_flag_combinations_before_any_prompt(tmp_path, args):
    """--yes without a reset flag, --id with one, and --all with --yes or a reset flag are usage errors."""
    (tmp_path / "axiom-graph.toml").write_text(_CUSTOM_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    state = _built_with_a_marker(tmp_path)

    result = CliRunner().invoke(cli, ["init", *args, str(tmp_path)], input="y\ny\ny\n")

    assert result.exit_code == 2, result.output
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == _CUSTOM_TOML
    assert _index_state(tmp_path) == state


@workflow(
    purpose=(
        "Verify `init --all` lists the index, toml and policy resets in one confirm; declining changes nothing, "
        "and confirming leaves default settings keeping the project id, the shipped policy under the same doc "
        "id, and an index rebuilt with the default config"
    ),
)
def test_init_all_resets_the_index_the_toml_and_the_policy_behind_one_confirm(tmp_path):
    口 = Step(
        step_num=1,
        name="Customise a project",
        purpose="A toml excluding vendor/, an edited policy doc and a verification marker",
    )
    (tmp_path / "axiom-graph.toml").write_text(_CUSTOM_TOML, encoding="utf-8")
    (tmp_path / "mod.py").write_text(_SIMPLE_MODULE)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "lib.py").write_text("def vendored():\n    return 1\n", encoding="utf-8")
    state = _built_with_a_marker(tmp_path, init=True)
    _edit_policy(tmp_path, "kept::docs/agent-policy")
    assert not any("vendor" in nid for nid in state[0])
    policy_file = (tmp_path / "docs" / "agent-policy.docjson").read_text(encoding="utf-8")

    口 = Step(step_num=2, name="Decline the confirm", purpose="It lists all three resets; no changes nothing")
    declined = CliRunner().invoke(cli, ["init", "--all", str(tmp_path)], input="n\n")

    assert declined.exit_code == 1, declined.output
    assert "DELETED and rebuilt" in declined.output
    assert 'axiom_graph.scan.exclude_dirs = ["vendor"]  (default: [])' in declined.output
    assert "kept::docs/agent-policy (docs/agent-policy.docjson) is overwritten" in declined.output
    assert (tmp_path / "axiom-graph.toml").read_text(encoding="utf-8") == _CUSTOM_TOML
    assert (tmp_path / "docs" / "agent-policy.docjson").read_text(encoding="utf-8") == policy_file
    assert _index_state(tmp_path) == state

    口 = Step(step_num=3, name="Confirm", purpose="Default settings, the shipped policy and a rebuilt index")
    result = CliRunner().invoke(cli, ["init", "--all", str(tmp_path)], input="y\n")

    assert result.exit_code == 0, result.output
    assert _read_toml(tmp_path) == {"axiom_graph": {"project_id": "kept"}}
    policy = project_facts(tmp_path).policy
    assert policy.doc_id == "kept::docs/agent-policy"
    assert any(s.content == _DEFAULT_TEXT for s in policy.sections)
    assert _policy_doc_ids(tmp_path) == ["kept::docs/agent-policy"]
    ids, history, _verified = _index_state(tmp_path)
    assert any("vendor" in nid for nid in ids), "the default config no longer excludes vendor/"
    assert not any(change == "MANUAL_VERIFIED" for _id, _node, change in history)
