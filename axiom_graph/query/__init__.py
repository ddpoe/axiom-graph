"""Query bounded context (ADR-019 cycle 3).

Read-only inventory queries over the axiom-graph index: search, render,
list, graph traversal, source-fetch, SQL, drift query, tag listing, and
undocumented-node listing.

The behavioural API is in :mod:`axiom_graph.query.api`; the MCP wire
surface is in :mod:`axiom_graph.query.mcp_tools`; both the CLI
(``axiom_graph.cli.inspection``, ``axiom_graph.cli.rendering``) and
MCP wire layer call ``axiom_graph.query.api`` directly so a single
orchestration function is the source of truth per operation.
"""
