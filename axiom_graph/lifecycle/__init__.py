"""Lifecycle bounded context.

Per ADR-019 (cycle 2), this domain owns the orchestration for build,
check, mark_clean, history, report, diff, purge, render_site, and
checkout operations.

Public modules:
    ``axiom_graph.lifecycle.api``       -- behavioural API (DB-owning)
    ``axiom_graph.lifecycle.mcp_tools`` -- MCP wire surface (no DB access)
"""
