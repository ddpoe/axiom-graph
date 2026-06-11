# CLI Reference

The `axiom-graph` command-line interface, grouped by purpose. Pick a group in
the sidebar, or jump straight to a command from the tables below.

## Indexing

Build and maintain the graph.

| Command | Description |
| --- | --- |
| <a href="indexing.html#axiom-graph-init"><code>init</code></a> | Initialise (or re-initialise) the index for a project. |
| <a href="indexing.html#axiom-graph-build"><code>build</code></a> | Scan `PROJECT_ROOT` and add newly-discovered nodes/edges. |
| <a href="indexing.html#axiom-graph-check"><code>check</code></a> | Report per-node staleness / confidence status. |
| <a href="indexing.html#axiom-graph-mark-clean"><code>mark-clean</code></a> | Mark a node as manually verified clean. |
| <a href="indexing.html#axiom-graph-link"><code>link</code></a> | Add a typed edge between two nodes. |
| <a href="indexing.html#axiom-graph-checkout"><code>checkout</code></a> | Copy the DB into a worktree via `VACUUM INTO`. |

## Inspection

Query the graph.

| Command | Description |
| --- | --- |
| <a href="inspection.html#axiom-graph-list"><code>list</code></a> | List nodes, optionally filtered by type or tag. |
| <a href="inspection.html#axiom-graph-graph"><code>graph</code></a> | Show the edge graph for a node. |
| <a href="inspection.html#axiom-graph-report"><code>report</code></a> | Impact report: what changed since a checkpoint or SHA. |
| <a href="inspection.html#axiom-graph-history"><code>history</code></a> | Manage and inspect node change history. |

## Rendering & site

Produce output.

| Command | Description |
| --- | --- |
| <a href="rendering.html#axiom-graph-render"><code>render</code></a> | Render nodes at a given detail level. |
| <a href="rendering.html#axiom-graph-render-site"><code>render-site</code></a> | Render the consumer documentation site from DocJSON. |
| <a href="rendering.html#axiom-graph-export"><code>export</code></a> | Export the full index to `index.json`. |
| <a href="rendering.html#axiom-graph-viz"><code>viz</code></a> | Launch the visualization dashboard. |

```{toctree}
:hidden:

indexing
inspection
rendering
```
