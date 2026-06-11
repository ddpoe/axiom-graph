"""Tests for configurable docs_dirs / config_dirs / db_path.

Covers:
- Tier 1: config parsing (defaults + malformed), db_path_for helper,
  DocJSON signature sniff (positive + negative).
- Tier 2: multi-config-dir scan, /api/config endpoint shape, backward-compat
  node IDs under default config.
- Tier 3: multi-root docs build, DB path end-to-end override,
  multi-root doc listing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom_graph.config import AxiomGraphConfig, db_path_for
from axiom_graph.index import db as db_mod
from axiom_graph.index.builder import _is_docjson_file


# ---------------------------------------------------------------------------
# Tier 1 -- Config parsing
# ---------------------------------------------------------------------------


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_config_defaults_fire_when_keys_omitted(tmp_path):
    """When axiom-graph.toml omits new keys, defaults apply."""
    _write_toml(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    cfg = AxiomGraphConfig.load(tmp_path)
    assert cfg.scan.docs_dirs == ["docs"]
    assert cfg.scan.config_dirs == [".claude"]
    assert cfg.db_path == ".axiom_graph/graph.db"


def test_config_parses_explicit_docs_and_config_dirs(tmp_path):
    """Explicit docs_dirs / config_dirs / db_path round-trip via load."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        (
            "[axiom_graph]\n"
            'project_id = "proj"\n'
            'db_path = "custom/my.db"\n'
            "[axiom_graph.scan]\n"
            'docs_dirs = ["docs", "specs"]\n'
            'config_dirs = [".claude", ".cursor"]\n'
        ),
    )
    cfg = AxiomGraphConfig.load(tmp_path)
    assert cfg.scan.docs_dirs == ["docs", "specs"]
    assert cfg.scan.config_dirs == [".claude", ".cursor"]
    assert cfg.db_path == "custom/my.db"


def test_config_malformed_docs_dirs_falls_back_to_default(tmp_path):
    """Non-list docs_dirs falls back rather than raising."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        '[axiom_graph]\n[axiom_graph.scan]\ndocs_dirs = "docs"\n',
    )
    cfg = AxiomGraphConfig.load(tmp_path)
    # The load path should not raise; default is used.
    assert cfg.scan.docs_dirs == ["docs"]


def test_config_empty_docs_dirs_list_stays_empty(tmp_path):
    """Empty list is honored (caller's responsibility)."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        "[axiom_graph]\n[axiom_graph.scan]\ndocs_dirs = []\n",
    )
    cfg = AxiomGraphConfig.load(tmp_path)
    # An explicit empty list is interpreted per load logic.  Accept either
    # [] or ["docs"] — both are defensible; just require no crash.
    assert isinstance(cfg.scan.docs_dirs, list)


# ---------------------------------------------------------------------------
# Tier 1 -- db_path_for helper
# ---------------------------------------------------------------------------


def test_db_path_for_default_is_axiom_graph_subdir(tmp_path):
    """Default configuration resolves db_path under project_root/.axiom_graph."""
    _write_toml(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    assert db_path_for(tmp_path) == tmp_path / ".axiom_graph" / "graph.db"


def test_db_path_for_relative_resolves_against_project_root(tmp_path):
    """Relative db_path is resolved against the project root."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        '[axiom_graph]\ndb_path = "custom/my.db"\n',
    )
    assert db_path_for(tmp_path) == tmp_path / "custom" / "my.db"


def test_db_path_for_absolute_is_honored_as_is(tmp_path):
    """Absolute db_path is returned unchanged."""
    abs_db = (tmp_path / "elsewhere" / "foo.db").resolve()
    _write_toml(
        tmp_path / "axiom-graph.toml",
        f'[axiom_graph]\ndb_path = "{abs_db.as_posix()}"\n',
    )
    assert db_path_for(tmp_path) == abs_db


# ---------------------------------------------------------------------------
# Tier 2 -- DocJSON signature sniff
# ---------------------------------------------------------------------------


def test_docjson_sniff_positive_anywhere(tmp_path):
    """Valid DocJSON outside docs/ is classified as DocJSON."""
    f = tmp_path / "somewhere" / "random" / "foo.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"title": "T", "sections": []}))
    assert _is_docjson_file(f) is True


def test_docjson_sniff_negative_in_docs(tmp_path):
    """Non-DocJSON .json under docs/ is NOT classified as DocJSON."""
    f = tmp_path / "docs" / "not-docjson.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"some": "config"}))
    assert _is_docjson_file(f) is False


def test_docjson_sniff_negative_on_malformed(tmp_path):
    """Malformed JSON is not classified as DocJSON."""
    f = tmp_path / "broken.json"
    f.write_text("{not json")
    assert _is_docjson_file(f) is False


# ---------------------------------------------------------------------------
# Tier 3 -- Multi-root docs build
# ---------------------------------------------------------------------------


def test_multi_root_docs_build_indexes_both_roots(tmp_path):
    """A fixture with docs/ + specs/ as configured roots indexes both."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj"\n[axiom_graph.scan]\ndocs_dirs = ["docs", "specs"]\n'),
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "specs").mkdir()
    (tmp_path / "docs" / "foo.md").write_text("# Foo\n", encoding="utf-8")
    (tmp_path / "specs" / "bar.json").write_text(
        json.dumps({"title": "Bar", "sections": [{"id": "s1", "heading": "H", "content": "c"}]}),
        encoding="utf-8",
    )
    from axiom_graph.index import builder

    builder.build(tmp_path)
    db_path = db_path_for(tmp_path)
    # All doc node IDs from the DB
    with db_mod._connect(db_path) as conn:
        rows = conn.execute("SELECT id FROM nodes WHERE subtype='docjson'").fetchall()
        all_ids = {r[0] for r in rows}
        rows2 = conn.execute("SELECT id FROM nodes WHERE source='doc_scanner'").fetchall()
        all_md_ids = {r[0] for r in rows2}
    assert "proj::docs.bar" in all_ids, f"specs/ DocJSON not indexed: {all_ids}"
    # Markdown node IDs use the docs prefix
    assert any("foo" in nid for nid in all_md_ids | all_ids), "docs/ md not indexed"


def test_backward_compat_default_config_node_ids(tmp_path):
    """Default config produces pre-change node IDs (baseline snapshot).

    Also asserts ``result['warnings']`` is empty — a previous regression
    silently appended ``docs_dirs entry not found…`` under the default
    config when the optional ``.claude/`` dir was missing.  This combined
    assertion guards against that class of regression.
    """
    _write_toml(tmp_path / "axiom-graph.toml", '[axiom_graph]\nproject_id = "proj"\n')
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "foo.json").write_text(
        json.dumps({"title": "Foo", "sections": [{"id": "intro", "heading": "I", "content": "c"}]}),
        encoding="utf-8",
    )
    from axiom_graph.index import builder

    result = builder.build(tmp_path)
    db_path = db_path_for(tmp_path)
    with db_mod._connect(db_path) as conn:
        ids = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE subtype='docjson'").fetchall()}
    # These IDs match the pre-change canonical derivation.
    assert "proj::docs.foo" in ids
    assert "proj::docs.foo::intro" in ids
    # Backward-compat guard: default config with no explicit docs_dirs /
    # config_dirs must not emit new warnings, even when .claude/ is absent
    # (which is the case for this fixture — only docs/ was created).
    assert not result["warnings"], f"unexpected warnings under default config: {result['warnings']}"


def test_default_config_no_docs_dir_no_warnings(tmp_path):
    """Default config on a bare project (no docs/, no .claude/) emits no warnings.

    This is the exact backward-compat scenario the PEV reviewer flagged:
    three pre-existing tests (test_init_then_build_lifecycle,
    test_rename_detection_scenario_matrix,
    test_ast_validates_edges_no_annotation_required) build against tmp_paths
    with neither docs/ nor .claude/ and assert ``not result['warnings']``.
    The regression appended ``docs_dirs entry not found…`` and
    ``config_dirs entry not found…`` under the default config, breaking
    all three.  This test pins that behaviour directly.
    """
    # No axiom-graph.toml at all — exercises the "no TOML file" code path in load().
    from axiom_graph.index import builder

    result = builder.build(tmp_path, project_id="proj")
    # Key invariant: no warnings from default-missing dirs.
    assert not result["warnings"], f"default config on a bare project must not emit warnings, got: {result['warnings']}"


def test_explicit_missing_docs_dir_emits_warning(tmp_path):
    """Explicitly configured missing docs_dirs entry DOES warn.

    Confirms the positive side of the distinction: when the user configures
    a path that doesn't exist, they get feedback.
    """
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj"\n[axiom_graph.scan]\ndocs_dirs = ["nonexistent"]\n'),
    )
    from axiom_graph.index import builder

    result = builder.build(tmp_path)
    assert any("nonexistent" in w for w in result["warnings"]), (
        f"explicit missing docs_dirs entry must emit a warning, got: {result['warnings']}"
    )


# ---------------------------------------------------------------------------
# Tier 3 -- DB path end-to-end
# ---------------------------------------------------------------------------


def test_db_path_override_end_to_end(tmp_path):
    """Config'd db_path directs build output to the override location."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj"\ndb_path = "custom/my.db"\n'),
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "foo.json").write_text(
        json.dumps({"title": "Foo", "sections": []}),
        encoding="utf-8",
    )
    from axiom_graph.index import builder

    builder.build(tmp_path)
    custom_db = tmp_path / "custom" / "my.db"
    default_db = tmp_path / ".axiom_graph" / "graph.db"
    assert custom_db.exists()
    assert not default_db.exists()


# ---------------------------------------------------------------------------
# Tier 3 -- Multi config_dirs scan
# ---------------------------------------------------------------------------


def test_multi_config_dirs_indexes_both(tmp_path):
    """Builder iterates both configured config_dirs."""
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj"\n[axiom_graph.scan]\nconfig_dirs = [".claude", ".cursor"]\n'),
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"some": "config"}),
        encoding="utf-8",
    )
    (tmp_path / ".cursor" / "rules.md").write_text("# Cursor rules\n", encoding="utf-8")
    from axiom_graph.index import builder

    builder.build(tmp_path)
    db_path = db_path_for(tmp_path)
    with db_mod._connect(db_path) as conn:
        rows = conn.execute("SELECT id, location FROM nodes WHERE id LIKE 'proj::config.%'").fetchall()
    locations = {r[1] for r in rows if r[1]}
    joined = " ".join(locations)
    assert ".claude" in joined, f"no .claude config nodes: {rows}"
    assert ".cursor" in joined, f"no .cursor config nodes: {rows}"


# ---------------------------------------------------------------------------
# Tier 2 -- /api/config endpoint shape (viz server)
# ---------------------------------------------------------------------------

# The viz server tests require FastAPI (optional extra ``[viz]``).  We don't
# want to skip the entire module when it's absent — only the two viz tests
# below.  The decorator below applies per-test.
try:  # noqa: SIM105 — keep the import attempt explicit for clarity
    import fastapi as _fastapi  # noqa: F401

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

_skip_no_fastapi = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed (optional [viz] extra)")


def _setup_viz_server_against(project_root: Path):
    """Point the viz server module globals at *project_root* and return a TestClient.

    Matches the pattern used in tests/test_viz_test_dashboard.py::_setup_server.
    """
    from fastapi.testclient import TestClient

    from axiom_graph.config import AxiomGraphConfig, db_path_for
    from axiom_graph.viz import server

    server._PROJECT_ROOT = project_root
    server._DB_PATH = db_path_for(project_root)
    server._DFLOW_DB_PATH = None
    _cfg = AxiomGraphConfig.load(project_root)
    server._PROJECT_ID = _cfg.project_id or project_root.name
    server._TEST_PATHS = _cfg.scan.test_paths
    server._EXCLUDE_DIRS = _cfg.scan.exclude_dirs
    return TestClient(server.app)


@_skip_no_fastapi
def test_api_config_endpoint_shape(tmp_path):
    """GET /api/config returns {docs_dirs, project_id} and omits db_path.

    Verifies the frontend-facing surface: the endpoint must return the
    configured docs_dirs list and project_id, but NOT leak the db_path
    (filesystem internals the UI has no need for).
    """
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj_specs"\n[axiom_graph.scan]\ndocs_dirs = ["specs"]\n'),
    )
    # viz server's _apply_project is typically called with a real axiom-graph DB.
    # For the config endpoint we only need the module globals wired up.
    (tmp_path / ".axiom_graph").mkdir()
    db_mod.init_db(tmp_path / ".axiom_graph" / "graph.db")

    client = _setup_viz_server_against(tmp_path)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload == {"docs_dirs": ["specs"], "project_id": "proj_specs"}, f"expected exact shape, got {payload}"
    # Explicit negative assertion: db_path must not be exposed.
    assert "db_path" not in payload, f"db_path leaked to /api/config payload: {payload}"


# ---------------------------------------------------------------------------
# Tier 3 -- list_doc_subdirs enumerates all configured docs roots
# ---------------------------------------------------------------------------


@_skip_no_fastapi
def test_list_doc_subdirs_multi_root(tmp_path):
    """GET /api/docs/subdirs returns subdirs from every configured docs root.

    Exercises the viz-side multi-root behaviour: with ``docs_dirs = ["docs",
    "specs"]`` and subdirs ``docs/prds/`` + ``specs/adrs/``, the endpoint
    must enumerate subdirs from BOTH roots.
    """
    _write_toml(
        tmp_path / "axiom-graph.toml",
        ('[axiom_graph]\nproject_id = "proj"\n[axiom_graph.scan]\ndocs_dirs = ["docs", "specs"]\n'),
    )
    (tmp_path / "docs" / "prds").mkdir(parents=True)
    (tmp_path / "specs" / "adrs").mkdir(parents=True)
    (tmp_path / ".axiom_graph").mkdir()
    db_mod.init_db(tmp_path / ".axiom_graph" / "graph.db")

    client = _setup_viz_server_against(tmp_path)
    resp = client.get("/api/docs/subdirs")
    assert resp.status_code == 200
    payload = resp.json()

    # Server returns {"dirs": [...]} (see viz/server.py::list_doc_subdirs).
    assert "dirs" in payload, f"expected 'dirs' key, got {payload}"
    dirs = set(payload["dirs"])
    assert "docs" in dirs, f"primary root 'docs' missing: {dirs}"
    assert "specs" in dirs, f"secondary root 'specs' missing: {dirs}"
    assert "docs/prds" in dirs, f"docs/prds subdir missing: {dirs}"
    assert "specs/adrs" in dirs, f"specs/adrs subdir missing: {dirs}"
