# MCP Tools

The axiom-graph MCP server exposes 33 tools to AI agents over the Model Context
Protocol, grouped below by purpose. Pick a group in the sidebar, or jump
straight to a tool from the tables.

## Search & navigate

Explore the graph — search, read source, list, and traverse.

| Tool | Description |
| --- | --- |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_search` | Full-text search over node level_1 and level_2 fields. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_source` | Return the raw source body of a node, looked up by ID. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_list` | List nodes, filtered by type, tag, parent, or location. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_graph` | Traverse and render the edge graph for a node. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_render` | Render nodes from the index at a given detail level. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_list_tags` | List all distinct tags in the index with node counts. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_list_undocumented` | List nodes with no inbound `documents` edge. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_list_reference_points` | List reference points for `report(since_sha=…)`. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_sql` | Run a read-only SQL query against the index. |

## Edit docs & links

Read and write DocJSON documents, sections, and doc→code links.

| Tool | Description |
| --- | --- |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_read_doc` | Read a DocJSON document as Markdown. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_write_doc` | Write a DocJSON file and register it in the index. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_update_section` | Update a section's content, heading, or ID. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_patch_section` | Append, prepend, or unique-match replace within a section. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_add_section` | Add a new section to an existing document. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_delete_section` | Delete a section and its nested children. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_delete_doc` | Delete an entire document and all DB artifacts. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_update_doc_meta` | Update a document's title or tags. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_add_link` | Add link(s) from a doc section to code node(s). |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_delete_link` | Remove link(s) from a doc section to code node(s). |

## Staleness & lifecycle

Inspect and resolve documentation staleness.

| Tool | Description |
| --- | --- |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_check` | Per-node staleness / confidence summary. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_drift_query` | Filtered/grouped projection over the staleness inventory. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_mark_clean` | Mark promotable own-status drift (CONTENT_UPDATED / DESC_UPDATED / RENAMED) as agent-verified. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_purge_node` | Purge NOT_FOUND nodes from the index. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_apply_rename` | Weld a NOT_FOUND node to its renamed replacement. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_revert_rename` | Undo a rename weld (idempotent). |

## History & impact

Track what changed and where.

| Tool | Description |
| --- | --- |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_history` | Show the change history for a single node. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_diff` | Show what changed in a node since a baseline commit. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_report` | Impact report since a checkpoint, SHA, or datetime. |

## Build & workflows

Build the index, snapshot it, render the site, and inspect axiom-annotation workflows.

| Tool | Description |
| --- | --- |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_build` | Run axiom-graph build (discovery-only) for a project. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_checkout` | Copy the DB into a worktree via `VACUUM INTO`. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_render_site` | Render the consumer documentation site from DocJSON. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_workflow_list` | List axiom-annotation workflow and task functions. |
| {py:func}`~axiom_graph.mcp.server.axiom_graph_workflow_detail` | Show ordered steps for an axiom-annotation workflow or task. |

```{toctree}
:hidden:

search
docs
staleness
history
build
```
