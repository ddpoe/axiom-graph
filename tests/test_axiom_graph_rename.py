"""Rename-validation tests for the cortex -> axiom_graph migration.

Covers US-1 through US-7 per the Architect's Test Plan.  These are
Tier 2 / Tier 3 tests -- they assert the rename is complete on every
user-visible surface (import, CLI, MCP tool names, config, DocJSON prefix).
"""

from __future__ import annotations

import json
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

# Project root resolved from this test file.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# US-1: Clean import surface
# ---------------------------------------------------------------------------


def test_us1_axiom_graph_imports():
    """axiom_graph is the canonical import name."""
    import axiom_graph  # noqa: F401

    # Should have a reachable submodule
    from axiom_graph import cli  # noqa: F401
    from axiom_graph import config  # noqa: F401


def test_us1_cortex_import_fails():
    """`import cortex` raises ModuleNotFoundError (no compat shim)."""
    # Use subprocess to avoid polluting the test runner's sys.modules.
    result = subprocess.run(
        [sys.executable, "-c", "import cortex"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr or "No module named" in result.stderr


# ---------------------------------------------------------------------------
# US-3: MCP tools renamed
# ---------------------------------------------------------------------------


def test_us3_mcp_tools_all_axiom_graph_prefixed():
    """FastMCP registers tools all named axiom_graph_*; zero cortex_*."""
    import axiom_graph.mcp_server as ms

    # FastMCP exposes registered tools via _tool_manager
    tm = ms.mcp._tool_manager  # type: ignore[attr-defined]
    tools = list(tm._tools.keys())
    assert tools, "Expected at least one registered tool"
    assert all(t.startswith("axiom_graph_") for t in tools), (
        f"Non-axiom_graph tool names found: {[t for t in tools if not t.startswith('axiom_graph_')]}"
    )
    assert not any(t.startswith("cortex_") for t in tools)
    # The pitch expected ~33 tools; actual is 29 (documented in builder deviations).
    assert len(tools) >= 29


# ---------------------------------------------------------------------------
# US-4: CLI renamed
# ---------------------------------------------------------------------------


def test_us4_axiom_graph_cli_help():
    """`poetry run axiom-graph --help` exits 0."""
    # Use sys.executable + module invocation for reliability; entry point is
    # axiom_graph.cli:main.
    result = subprocess.run(
        [sys.executable, "-m", "axiom_graph.cli", "--help"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"


# ---------------------------------------------------------------------------
# US-5: Docs reflect rename (consumer MCP setup doc)
# ---------------------------------------------------------------------------


def test_us5_consumer_mcp_setup_references_axiom_graph():
    """docs/consumer/mcp-setup.json references axiom_graph.mcp_server, not cortex.*."""
    p = PROJECT_ROOT / "docs" / "consumer" / "mcp-setup.json"
    if not p.exists():
        pytest.skip(f"{p} not present in this repo snapshot")
    text = p.read_text(encoding="utf-8")
    assert "axiom_graph.mcp_server" in text or "axiom-graph-mcp" in text
    assert "cortex.mcp_server" not in text
    assert "cortex-mcp" not in text


# ---------------------------------------------------------------------------
# US-6: Config rename
# ---------------------------------------------------------------------------


def test_us6_axiom_graph_toml_loaded(tmp_path: Path):
    """AxiomGraphConfig.load() reads axiom-graph.toml with [axiom_graph] header."""
    from axiom_graph.config import AxiomGraphConfig

    (tmp_path / "axiom-graph.toml").write_text(
        """
[axiom_graph]
project_id = "foo"

[axiom_graph.scan]
docs_dirs = ["mydocs"]
""",
        encoding="utf-8",
    )
    cfg = AxiomGraphConfig.load(tmp_path)
    assert cfg.project_id == "foo"
    assert cfg.scan.docs_dirs == ["mydocs"]
    assert cfg.db_path == ".axiom_graph/graph.db"


def test_us6_no_cortex_toml_fallback(tmp_path: Path):
    """Empty tempdir -> defaults. Tempdir with ONLY cortex.toml -> still defaults.

    Verifies the hard-rename: AxiomGraphConfig.load() MUST NOT fall back to a
    legacy cortex.toml file when axiom-graph.toml is missing.  The lingering
    cortex.toml is simply ignored.
    """
    from axiom_graph.config import AxiomGraphConfig

    cfg = AxiomGraphConfig.load(tmp_path)
    # Defaults
    assert cfg.project_id is None
    assert cfg.db_path == ".axiom_graph/graph.db"

    # Now drop a legacy cortex.toml with an old [cortex] header.
    # It MUST be ignored -- no fallback logic reads this file.
    legacy_name = "co" + "rtex.toml"  # constructed to avoid the rename sweep
    legacy_header = "[co" + "rtex]"
    (tmp_path / legacy_name).write_text(
        f"""
{legacy_header}
project_id = "should_be_ignored"
""",
        encoding="utf-8",
    )
    cfg2 = AxiomGraphConfig.load(tmp_path)
    assert cfg2.project_id is None, f"{legacy_name} must not be read as a fallback"
    assert cfg2.db_path == ".axiom_graph/graph.db"


# ---------------------------------------------------------------------------
# US-7: DocJSON prefix sweep
# ---------------------------------------------------------------------------


def test_us7_no_cortex_prefix_in_docs():
    """Zero `cortex::` occurrences remain in docs/**/*.json."""
    docs = PROJECT_ROOT / "docs"
    offenders = []
    for p in docs.rglob("*.json"):
        text = p.read_text(encoding="utf-8")
        if "cortex::" in text:
            offenders.append(str(p.relative_to(PROJECT_ROOT)))
    assert not offenders, f"DocJSON files still contain cortex:: prefix: {offenders}"


def test_us7_adr_005_sections_all_axiom_graph_prefixed():
    """In ADR-005 (axiom-unification), every section id/parent uses axiom_graph::."""
    # Search for any JSON doc that looks like the axiom-unification ADR.
    # Supports both the flat layout (docs/adrs.005-axiom-unification.json) and
    # a nested adrs/ directory layout.
    candidates = list((PROJECT_ROOT / "docs").rglob("*005-axiom-unification*.json"))
    if not candidates:
        candidates = list((PROJECT_ROOT / "docs").rglob("*005*axiom-unification*.json"))
    if not candidates:
        pytest.skip("ADR-005 (axiom-unification) not present in this snapshot")
    p = candidates[0]
    data = json.loads(p.read_text(encoding="utf-8"))

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("id", "parent", "parent_id"):
                    if isinstance(v, str) and v.startswith("cortex::"):
                        return True
                if walk(v):
                    return True
        elif isinstance(obj, list):
            for x in obj:
                if walk(x):
                    return True
        return False

    assert not walk(data), f"{p} still has cortex:: IDs"


# ---------------------------------------------------------------------------
# US-7 (Tier 3): Rebuilt DB has all nodes under axiom_graph::
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_us7_rebuilt_db_axiom_graph_prefix_only():
    """After `axiom-graph build .`, every node.id starts with axiom_graph::.

    This is a Tier-3 smoke test.  Skipped when .axiom_graph/graph.db is absent
    (i.e. when run before the final rebuild step).
    """
    db_path = PROJECT_ROOT / ".axiom_graph" / "graph.db"
    if not db_path.exists():
        pytest.skip(".axiom_graph/graph.db not yet built")

    con = sqlite3.connect(str(db_path))
    try:
        total = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        bad = con.execute("SELECT COUNT(*) FROM nodes WHERE id NOT LIKE 'axiom_graph::%'").fetchone()[0]
    finally:
        con.close()
    assert bad == 0, f"{bad} of {total} nodes still have non-axiom_graph prefix"
    # Rough sanity — rebuild should produce hundreds of nodes.
    assert total > 100, f"Only {total} nodes — rebuild incomplete?"
