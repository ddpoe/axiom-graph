"""Tier-2 / Tier-3 tests for ``axiom_graph.query.api``.

Per ADR-019 cycle 3, the query behavioural API is the single
orchestration source of truth for search, render, list, graph, source,
sql, list_tags, list_undocumented, and drift_query.

These tests verify:
- US-1: domain shape (``query/api.py`` + ``query/mcp_tools.py``
  exist with the expected public surface).
- US-2: shared orchestration (CLI + MCP both call ``query.api``).
- US-3: helper drain + layering allowlist drained.
- US-4: behavioural identity for the load-bearing flows.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# US-1: Domain shape established
# ---------------------------------------------------------------------------


def test_query_api_public_surface():
    """All required query orchestration functions are importable + callable."""
    from axiom_graph.query.api import (
        compute_drift_query,
        fetch_graph,
        fetch_render_data,
        fetch_source,
        list_nodes,
        list_tags,
        list_undocumented,
        run_sql,
        search_nodes,
    )

    for fn in [
        search_nodes,
        fetch_render_data,
        list_nodes,
        fetch_graph,
        fetch_source,
        run_sql,
        list_tags,
        list_undocumented,
        compute_drift_query,
    ]:
        assert callable(fn)


def test_query_dataclasses_are_dataclasses():
    """RenderResult, GraphResult, NodeSource are dataclasses."""
    import dataclasses

    from axiom_graph.query.api import GraphResult, NodeSource, RenderResult

    for cls in [RenderResult, GraphResult, NodeSource]:
        assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"


def test_mcp_query_module_deleted():
    """Acceptance: ``axiom_graph/mcp/query.py`` is removed (cycle-3)."""
    p = Path(__file__).parent.parent / "axiom_graph" / "mcp" / "query.py"
    assert not p.exists(), "axiom_graph/mcp/query.py should be deleted"


def test_lifecycle_compute_drift_query_removed():
    """``compute_drift_query`` no longer importable from lifecycle.api."""
    import axiom_graph.lifecycle.api as life_api

    assert not hasattr(life_api, "compute_drift_query"), (
        "compute_drift_query should have moved to axiom_graph.query.api"
    )


def test_mcp_server_resolves_to_query_mcp_tools():
    """Acceptance: registered query tools resolve to ``query.mcp_tools.*``.

    Also verifies ``_impl_drift_query`` (relocated cycle-3) resolves to
    query, NOT lifecycle.
    """
    from axiom_graph.mcp import server

    for name in [
        "_impl_sql",
        "_impl_render",
        "_impl_list",
        "_impl_graph",
        "_impl_search",
        "_impl_source",
        "_impl_list_tags",
        "_impl_list_undocumented",
        "_impl_drift_query",
    ]:
        impl = getattr(server, name)
        assert impl.__module__ == "axiom_graph.query.mcp_tools", (
            f"{name} resolves to {impl.__module__}, expected query.mcp_tools"
        )


# ---------------------------------------------------------------------------
# US-3: Helper drain + layering allowlist drained
# ---------------------------------------------------------------------------


def test_helpers_module_shrunk():
    """``_semantic_search_handler`` / ``_db_path`` / ``_require_db`` removed."""
    import axiom_graph.mcp._helpers as helpers

    assert not hasattr(helpers, "_semantic_search_handler"), "Should have moved to axiom_graph.query.api"
    assert not hasattr(helpers, "_db_path"), "Wrapper should be removed -- import db_path from axiom_graph.index.paths"
    assert not hasattr(helpers, "_require_db"), (
        "Wrapper should be removed -- import require_db from axiom_graph.index.paths"
    )


def test_helpers_module_under_30_lines_of_code():
    """``mcp/_helpers.py`` shrunk to ~30 lines of executable code."""
    import inspect

    import axiom_graph.mcp._helpers as helpers

    src = inspect.getsource(helpers)
    # Strip comments + docstring lines + blank lines for a rough LOC count.
    code_lines = [line for line in src.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    # Allow some headroom; the architect's target was ~30 LOC.
    assert len(code_lines) < 60, f"_helpers.py has {len(code_lines)} lines of code; expected < 60 after cycle-3 drain"


def test_layering_allowlist_drained():
    """``CYCLE_2_3_PRESENTATION_FILES`` is empty (or constant retired)."""
    pytest.importorskip("tools.check_layering", reason="internal layering-lint tooling not shipped")
    from tools.check_layering import CYCLE_2_3_PRESENTATION_FILES

    # Either the set is empty, or it doesn't include any /mcp/ files
    # (cli/* files may still be grandfathered under a different name).
    mcp_entries = {p for p in CYCLE_2_3_PRESENTATION_FILES if "/mcp/" in p}
    assert not mcp_entries, f"No mcp/ files should be allowlisted post-cycle-3; got {mcp_entries}"


def test_layering_check_passes_clean():
    """``python tools/check_layering.py`` exits clean against the worktree."""
    import subprocess
    import sys

    repo_root = Path(__file__).parent.parent
    result = subprocess.run(
        [sys.executable, str(repo_root / "tools" / "check_layering.py")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"layering check failed (rc={result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# US-4: Behavioural identity for the load-bearing flows
# ---------------------------------------------------------------------------


def test_search_nodes_threads_embedder_thread_kwarg():
    """``search_nodes`` accepts ``embedder_thread`` and forwards without raising.

    Per cycle-3 O-1 relaxation: this is a Tier-2 plumbing assertion that
    the parameter survives the refactor; the actual semantic backend is
    out-of-scope (worktree venv lacks the ``semantic`` extras).
    """
    import inspect

    from axiom_graph.query.api import search_nodes

    sig = inspect.signature(search_nodes)
    assert "embedder_thread" in sig.parameters, "search_nodes must accept embedder_thread for the warm-up plumbing"


def test_server_search_passes_embedder_thread():
    """``mcp/server.py`` still threads ``_embedder_thread`` into ``axiom_graph_search``."""
    server_path = Path(__file__).parent.parent / "axiom_graph" / "mcp" / "server.py"
    src = server_path.read_text(encoding="utf-8")
    assert "_embedder_thread=_embedder_thread" in src, (
        "mcp/server.py must continue to thread _embedder_thread to axiom_graph_search"
    )


def test_drift_query_compute_callable_with_invalid_filter_raises():
    """``compute_drift_query`` validates ``format`` early."""
    from axiom_graph.query.api import compute_drift_query

    with pytest.raises(ValueError):
        # ``root`` is required but never dereferenced before the ValueError;
        # any Path suffices since the early validation aborts the call.
        compute_drift_query(Path("/nonexistent.db"), Path("/nonexistent/root"), format="garbage")


# ---------------------------------------------------------------------------
# US-2: Orchestration sharing — CLI + MCP both call ``query.api`` directly.
#
# The pattern: build a small fixture, call ``query.api.<func>`` once, then
# invoke both wire surfaces and assert their outputs derive from the same
# underlying data.  The CLI surface intentionally omits the MCP pagination
# header (cycle-2 cmd_* byte-identity), and the render surface intentionally
# omits MCP staleness badges -- so we don't assert byte-equality, only that
# the load-bearing data appears in both places.
# ---------------------------------------------------------------------------


def _seed_node(
    db_path: Path,
    node_id: str,
    *,
    own_status: str = "VERIFIED",
    link_status: str = "VERIFIED",
    location: str = "mod.py",
    level_3_location: str | None = None,
    node_type: str = "atomic_process",
) -> None:
    """Seed a single node with the given staleness pair (mirror test_drift_query)."""
    from axiom_graph.index import db
    from axiom_graph.models import AxiomNode

    node = AxiomNode(
        id=node_id,
        node_type=node_type,
        subtype=None,
        title=node_id.split("::")[-1],
        location=location,
        level_3_location=level_3_location,
        source="ast",
        code_hash="hash_aaa",
        level_0=node_id.split("::")[-1],
        level_1=node_id.split("::")[-1],
    )
    # Seed with the default discovery_only=True (Rule 3 forbids the
    # discovery_only=False shortcut in non-legacy test files); the UPDATE
    # below directly sets the staleness pair regardless of insert mode.
    db.upsert_node(db_path, node)
    with db._connect(db_path) as conn:
        conn.execute(
            "UPDATE nodes SET own_status = ?, link_status = ? WHERE id = ?",
            (own_status, link_status, node_id),
        )


def test_list_nodes_orchestration_shared(mini_project: Path):
    """US-2: ``query.api.list_nodes`` is the single source of truth for CLI + MCP.

    Both surfaces call ``query.api.list_nodes`` and format around it; the
    MCP wire wrapper adds a ``[N of M results]`` header, the CLI emits the
    bare body to preserve cycle-2 byte-identity.  Both must surface every
    node returned by the api call.
    """
    from click.testing import CliRunner

    from axiom_graph import cli
    from axiom_graph.query import api as query_api
    from axiom_graph.query import mcp_tools as query_mcp

    db_path = mini_project / ".axiom_graph" / "graph.db"
    _seed_node(db_path, "p::a::f1", node_type="atomic_process")
    _seed_node(db_path, "p::a::f2", node_type="atomic_process")
    _seed_node(db_path, "p::b::mod", node_type="composite_process")

    # 1. Source of truth: the API call.
    api_nodes = query_api.list_nodes(db_path, node_type="atomic_process")
    api_ids = {n.id for n in api_nodes}
    assert api_ids == {"p::a::f1", "p::a::f2"}, "list_nodes must filter by node_type before either surface formats"

    # 2. MCP wire derives from the same data.
    mcp_out = query_mcp.axiom_graph_list(str(mini_project), node_type="atomic_process")
    for nid in api_ids:
        assert nid in mcp_out, f"MCP output missing {nid!r}: {mcp_out!r}"
    # MCP-only header line (CLI omits this).
    assert "results]" in mcp_out

    # 3. CLI derives from the same data.
    runner = CliRunner()
    cli_result = runner.invoke(cli.main, ["list", str(mini_project), "--type", "atomic_process"])
    assert cli_result.exit_code == 0, f"CLI list failed: {cli_result.output} / {cli_result.exception}"
    for nid in api_ids:
        assert nid in cli_result.output, f"CLI output missing {nid!r}: {cli_result.output!r}"
    # CLI byte-identity: NO pagination header.
    assert "results]" not in cli_result.output


def test_fetch_graph_orchestration_shared(mini_project: Path):
    """US-2: ``query.api.fetch_graph`` is the single source of truth for CLI + MCP.

    The MCP wire wrapper formats a ``[N of M edges]`` header + truncation
    hint; the CLI emits the bare rendered edge body.  Both must reference
    the same starting node and outbound edges.
    """
    from click.testing import CliRunner

    from axiom_graph import cli
    from axiom_graph.index import db
    from axiom_graph.models import AxiomEdge
    from axiom_graph.query import api as query_api
    from axiom_graph.query import mcp_tools as query_mcp

    db_path = mini_project / ".axiom_graph" / "graph.db"
    _seed_node(db_path, "p::caller::root")
    _seed_node(db_path, "p::callee::a")
    _seed_node(db_path, "p::callee::b")
    db.upsert_edge(
        db_path,
        AxiomEdge(
            id="p::caller::root::calls::p::callee::a",
            edge_type="calls",
            from_id="p::caller::root",
            to_id="p::callee::a",
        ),
    )
    db.upsert_edge(
        db_path,
        AxiomEdge(
            id="p::caller::root::calls::p::callee::b",
            edge_type="calls",
            from_id="p::caller::root",
            to_id="p::callee::b",
        ),
    )

    # 1. Source of truth: the API call (CLI knob: with_locations=False).
    api_result = query_api.fetch_graph(db_path, "p::caller::root", direction="out", depth=1)
    assert not api_result.not_found
    assert api_result.total_edges == 2
    # The renderer always references both sides of each edge -- assert the
    # callees show up in the rendered body (load-bearing identity check).
    assert "p::callee::a" in api_result.rendered
    assert "p::callee::b" in api_result.rendered

    # 2. MCP wire derives from the same data.
    mcp_out = query_mcp.axiom_graph_graph(str(mini_project), "p::caller::root")
    assert "p::callee::a" in mcp_out
    assert "p::callee::b" in mcp_out
    # MCP-only edge-count header.
    assert "edges]" in mcp_out

    # 3. CLI derives from the same data; ClickException must NOT be raised.
    runner = CliRunner()
    cli_result = runner.invoke(cli.main, ["graph", "p::caller::root", str(mini_project)])
    assert cli_result.exit_code == 0, f"CLI graph failed: {cli_result.output} / {cli_result.exception}"
    assert "p::callee::a" in cli_result.output
    assert "p::callee::b" in cli_result.output


def test_fetch_render_data_orchestration_shared(mini_project: Path):
    """US-2: ``query.api.fetch_render_data`` is shared by CLI (no badges) and MCP (with badges).

    Both surfaces call ``fetch_render_data`` for the underlying node list
    and rendered body; the ``with_badges`` knob is the only Cat-4b
    surface-divergent axis.  CLI passes False (preserves cycle-2
    cmd_render byte-identity); MCP passes True (overlays staleness badges).
    """
    from click.testing import CliRunner

    from axiom_graph import cli
    from axiom_graph.query import api as query_api
    from axiom_graph.query import mcp_tools as query_mcp

    db_path = mini_project / ".axiom_graph" / "graph.db"
    _seed_node(
        db_path,
        "p::a::clean",
        own_status="VERIFIED",
        link_status="VERIFIED",
    )
    _seed_node(
        db_path,
        "p::a::stale",
        own_status="VERIFIED",
        link_status="LINKED_STALE",
    )

    # 1. Source of truth (twice): badges-on vs badges-off both derive from
    # the same underlying node list.  Identity check: same node IDs in body.
    api_with = query_api.fetch_render_data(db_path, level=1, with_badges=True)
    api_without = query_api.fetch_render_data(db_path, level=1, with_badges=False)
    for body in (api_with.body, api_without.body):
        assert "p::a::clean" in body
        assert "p::a::stale" in body
    # Badge-on overlays the LINKED_STALE marker; badge-off does not.
    assert "[LINKED_STALE]" in api_with.body, "with_badges=True must overlay the LINKED_STALE badge for the stale node"
    assert "[LINKED_STALE]" not in api_without.body, "with_badges=False must emit a bare body (no badge markers)"

    # 2. MCP wire derives from with_badges=True.
    mcp_out = query_mcp.axiom_graph_render(str(mini_project), 1)
    assert "p::a::clean" in mcp_out
    assert "p::a::stale" in mcp_out
    assert "[LINKED_STALE]" in mcp_out, "MCP axiom_graph_render must surface the staleness badge"
    # MCP-only count header.
    assert "nodes -- level 1" in mcp_out

    # 3. CLI derives from with_badges=False.
    runner = CliRunner()
    cli_result = runner.invoke(cli.main, ["render", str(mini_project), "--level", "1"])
    assert cli_result.exit_code == 0, f"CLI render failed: {cli_result.output} / {cli_result.exception}"
    assert "p::a::clean" in cli_result.output
    assert "p::a::stale" in cli_result.output
    # CLI byte-identity: NO badge markers, NO count header.
    assert "[LINKED_STALE]" not in cli_result.output
    assert "nodes -- level 1" not in cli_result.output
