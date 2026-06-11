"""Tier-2 / Tier-3 tests for ``axiom_graph.lifecycle.api``.

Per ADR-019 cycle 2, the lifecycle behavioural API is the single
orchestration source of truth for build, check, mark_clean, history,
report, diff, purge, render_site, and checkout.  These tests verify
the api functions exist and produce data consistent with the MCP
wrappers and CLI command output.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from axiom_graph.lifecycle.api import (
    BuildSummary,
    CheckSummary,
    CheckoutResult,
    HistoryResult,
    MarkCleanResult,
    PurgeResult,
    ReferencePoint,
    ReportData,
    RenderSiteResult,
    build_index,
    checkout_db,
    compute_check_summary,
    compute_report,
    fetch_history,
    get_node_diff,
    list_reference_points,
    mark_clean_nodes,
    purge_nodes,
    render_site,
)


# ---------------------------------------------------------------------------
# US-1: Domain shape established
# ---------------------------------------------------------------------------


def test_lifecycle_api_public_surface():
    """All required orchestration functions are importable and callable."""
    for fn in [
        build_index,
        compute_check_summary,
        mark_clean_nodes,
        purge_nodes,
        fetch_history,
        list_reference_points,
        compute_report,
        checkout_db,
        render_site,
        get_node_diff,
    ]:
        assert callable(fn)


def test_dataclasses_are_dataclasses():
    """Each typed result is a dataclass with stable field names."""
    import dataclasses

    for cls in [
        BuildSummary,
        CheckSummary,
        MarkCleanResult,
        PurgeResult,
        HistoryResult,
        ReferencePoint,
        ReportData,
        CheckoutResult,
        RenderSiteResult,
    ]:
        assert dataclasses.is_dataclass(cls), f"{cls.__name__} is not a dataclass"


def test_diff_module_deleted():
    """Acceptance: ``axiom_graph/diff.py`` is removed from the working tree."""
    diff_path = Path(__file__).parent.parent / "axiom_graph" / "diff.py"
    assert not diff_path.exists(), "axiom_graph/diff.py should be deleted"


def test_mcp_lifecycle_module_deleted():
    """Acceptance: ``axiom_graph/mcp/lifecycle.py`` is removed."""
    p = Path(__file__).parent.parent / "axiom_graph" / "mcp" / "lifecycle.py"
    assert not p.exists(), "axiom_graph/mcp/lifecycle.py should be deleted"


def test_mcp_server_resolves_to_lifecycle_mcp_tools():
    """Acceptance: registered lifecycle tools resolve to lifecycle.mcp_tools.*"""
    from axiom_graph.mcp import server

    # The server module aliases its imported impls as `_impl_<name>`.
    # Per ADR-019 cycle 3, ``_impl_drift_query`` moved from lifecycle to
    # ``axiom_graph.query.mcp_tools`` (read-only inventory query); the
    # lifecycle-resolution assertion below excludes it.
    for name in [
        "_impl_build",
        "_impl_check",
        "_impl_mark_clean",
        "_impl_history",
        "_impl_report",
        "_impl_diff",
        "_impl_checkout",
        "_impl_render_site",
        "_impl_purge_node",
        "_impl_list_reference_points",
    ]:
        impl = getattr(server, name)
        assert impl.__module__ == "axiom_graph.lifecycle.mcp_tools", (
            f"{name} resolves to {impl.__module__}, expected lifecycle.mcp_tools"
        )

    # drift_query now resolves to query.mcp_tools per cycle-3 D-3
    assert server._impl_drift_query.__module__ == "axiom_graph.query.mcp_tools"


# ---------------------------------------------------------------------------
# US-3: Lint allowlist shrunk
# ---------------------------------------------------------------------------


def test_layering_allowlist_no_longer_grandfathers_lifecycle():
    """``CYCLE_2_3_PRESENTATION_FILES`` no longer references ``mcp/lifecycle.py``."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
    try:
        import importlib

        check_layering = importlib.import_module("check_layering")
        importlib.reload(check_layering)
        assert "axiom_graph/mcp/lifecycle.py" not in check_layering.CYCLE_2_3_PRESENTATION_FILES
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# US-2: Shared orchestration — verified_by argument is plumbed (Tier 2)
# ---------------------------------------------------------------------------


def test_mark_clean_nodes_signature_requires_verified_by():
    """``mark_clean_nodes`` takes an explicit ``verified_by`` keyword (no default)."""
    sig = inspect.signature(mark_clean_nodes)
    params = sig.parameters
    assert "verified_by" in params
    # Must be keyword-only (per architect's contract: explicit per surface)
    assert params["verified_by"].kind == inspect.Parameter.KEYWORD_ONLY
    # No default value -- caller must specify
    assert params["verified_by"].default is inspect.Parameter.empty
    # node_ids takes a list
    assert "node_ids" in params


def test_build_index_signature_takes_embedder_thread():
    """``build_index`` accepts an embedder_thread keyword for test injection."""
    sig = inspect.signature(build_index)
    params = sig.parameters
    assert "embedder_thread" in params
    assert params["embedder_thread"].kind == inspect.Parameter.KEYWORD_ONLY


def test_compute_check_summary_returns_typed_summary():
    """``compute_check_summary`` returns CheckSummary or None."""
    sig = inspect.signature(compute_check_summary)
    params = sig.parameters
    assert list(params)[:2] == ["db_path", "root"]


def test_compute_report_returns_report_data():
    """``compute_report`` accepts the architect-specified keyword params."""
    sig = inspect.signature(compute_report)
    params = sig.parameters
    for kw in ("since_sha", "since_timestamp", "change_type_pattern", "node_pattern", "node_type"):
        assert kw in params, f"compute_report missing keyword {kw!r}"
        assert params[kw].kind == inspect.Parameter.KEYWORD_ONLY
