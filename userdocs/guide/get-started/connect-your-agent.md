<!-- generated from axiom_graph::docs.consumer.get-started.connect-your-agent @ 168300d7c649; do not edit -->

# Connect Your Agent

## Connect Your Agent

axiom-graph is agent-native first. The Model Context Protocol (MCP) server is the primary way to use it: once your coding agent (Claude Code, Cursor, Copilot, Continue, or anything that speaks MCP) is connected, every `axiom_graph_*` tool appears in the agent's palette and the agent can query the codebase mesh directly. The human [CLI](use-the-cli.md) and [visualizer](../viz.md) are the secondary path.

Why lead with MCP? Because the whole point of axiom-graph is **context reduction**. Instead of an agent reading a whole file to find one function, or loading an entire design doc to answer one question, it asks the mesh for exactly the context it needs: one search hit, one function by line range, one doc section, or a one-hop traversal to the linked nodes. The agent spends its context budget on the answer, not on the haystack.

This page walks the full onboarding path: install, build the index, register the server in your client, then run an agent through its first intent-scoped retrieval. It closes with the current tool surface and the maintenance tools that keep the mesh trustworthy as code drifts.

## Install

Install axiom-graph into the virtualenv your project uses:

```bash
pip install axiom-graph
```

That single package ships both the indexer and the MCP server. There is no separate server install.

> **Note on extras:** older guides mentioned a `[semantic]` extra for embedding-based search. Semantic search is deprecated as of 2.1.0 and removed in 3.0 (ADR-020) — do not install it. Keyword search (FTS5) is the supported search path and needs no extra.

## Build the Index First

The MCP server reads a SQLite index at `.axiom_graph/graph.db`. If that file does not exist, every tool call fails with a "missing index" error. Build it once before connecting:

```bash
axiom-graph init .     # one-time: scaffold config + first index
axiom-graph build .    # incremental rebuild after that
```

The first build scans every file and can take 30+ seconds on a large codebase; subsequent builds are incremental (an mtime check skips unchanged files). The agent can also call `axiom_graph_build` itself once the server is connected, but having a baseline index in place means the very first tool call already returns useful results.

See [Use the CLI](use-the-cli.md) for the full `init` / `build` surface and [Configuration](configuration.md) for what lives in the config file.

## Configure Claude Code

Create a `.mcp.json` file in your project root:

```json
{
  "mcpServers": {
    "axiom-graph": {
      "type": "stdio",
      "command": "/path/to/your/venv/bin/python",
      "args": ["-m", "axiom_graph.mcp_server"],
      "env": {
        "AXIOM_GRAPH_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Point `command` at the Python executable **inside the virtualenv where axiom-graph is installed**. Using `python -m axiom_graph.mcp_server` rather than the bare `axiom-graph-mcp` console script guarantees the right virtualenv is used regardless of PATH.

Restart Claude Code. All `axiom_graph_*` tools then appear in the agent's tool palette. Every tool takes `project_root` as its first argument — the absolute path to your indexed project.

## Configure Cursor and Other MCP Clients

The server uses **stdio transport**: it reads JSON-RPC requests from stdin and writes responses to stdout, while all logging goes to stderr so it never corrupts the protocol stream. Any MCP-capable client can drive it.

Two equivalent entry points exist:

| Entry point | When to use |
|---|---|
| `python -m axiom_graph.mcp_server` | Most reliable; pins the virtualenv. Recommended. |
| `axiom-graph-mcp` | Console script installed by pip; works when axiom-graph is on PATH. |

For **Cursor**, add an `axiom-graph` entry to its MCP server settings using the same `command` / `args` shape as the Claude Code example above. For **VS Code** extensions that support MCP (Copilot, Continue, etc.), add it to `.vscode/settings.json`:

```json
{
  "mcp.servers": {
    "axiom-graph": {
      "command": "/path/to/your/venv/bin/python",
      "args": ["-m", "axiom_graph.mcp_server"],
      "env": { "AXIOM_GRAPH_LOG_LEVEL": "INFO" }
    }
  }
}
```

The pattern is identical across clients — point at the venv Python and use the module entry point. Only the JSON key differs (`mcpServers` vs `mcp.servers`); check your client's docs for the exact spelling.

To sanity-check the server outside any client, run `axiom-graph-mcp` in a terminal: it starts and waits for JSON-RPC on stdin. Press Ctrl+C to stop.

## Retrieve an Intent-Scoped Bundle

Once connected, the agent's core move is to pull just the part of the mesh sized to its intent, never the whole codebase. The canonical loop is **search → source / read_doc / graph**, and each step is a deliberate context saving.

**1. Locate the node.** Full-text search returns one ranked line per hit — node ID plus a one-line summary, with `@ path#L10-L45` line ranges for function nodes — instead of a pile of file contents:

```
axiom_graph_search(project_root, "compute_staleness")
```

**2. Read exactly what you need from that node:**

| Tool | Returns | Context saved |
|---|---|---|
| `axiom_graph_source` | The function body by stored line range | The rest of the file never loads |
| `axiom_graph_read_doc` (with `section=`) | One DocJSON section as Markdown | The rest of the document never loads |
| `axiom_graph_graph` | Just the linked neighbours (one hop) | No grep across the tree |

```
# Function body only — no file-path parsing, no offset arithmetic
axiom_graph_source(project_root, "myproject::index.staleness::compute_staleness")

# One doc section, not the whole doc
axiom_graph_read_doc(project_root, "myproject::docs.architecture", section="data-model")

# Traverse to exactly the linked nodes
axiom_graph_graph(project_root, node_id, direction="out")
```

This is the typed mesh paying off: the same nodes-and-edges that power drift detection are what the agent reads for context. An edge is intent-typed (a doc `documents` a function; a test `validates` it), so traversal lands on semantically related nodes, not just textual neighbours.

**A note on the graph's shape.** axiom-graph is an orientation and staleness tool, *not* a full call graph. The scanner emits `depends_on` edges from imports and `composes` edges from module-to-definition, but it does **not** index every function call site. So `axiom_graph_graph(direction="in")` finds importers and dependents, not literal callers — for "where is this called?" fall back to your editor's grep. Used for what it is good at — orientation, import dependency, doc coverage, and staleness — the mesh is the cheapest map of the codebase an agent can hold.

For more usage patterns, see [the reporting pipeline example](../examples/reporting-pipeline.md) and the agent that consumes the mesh to do real work in [PEV](../pev/overview.md).

## The Tool Surface

The server exposes its tools grouped by concern. Every tool takes `project_root` first and returns a text response (even on error — failures come back as `ERROR:` strings, not dropped connections). This list reflects the current surface.

**Query (read the mesh):**

| Tool | Purpose |
|---|---|
| `axiom_graph_search` | 3-stage full-text search (FTS5 → LIKE-AND → LIKE-OR) over node and doc-section text |
| `axiom_graph_source` | Raw source body of a node by line range |
| `axiom_graph_read_doc` | Render a DocJSON doc (or one `section`) to Markdown |
| `axiom_graph_graph` | Traverse `depends_on` / `composes` edges (`in` / `out` / `both`) |
| `axiom_graph_list` | Filtered node listing by type, tag, `parent_id`, or `location` |
| `axiom_graph_render` | Multi-level detail render (level_0–level_3) with inline staleness badges |
| `axiom_graph_sql` | Read-only SQL against the index |
| `axiom_graph_list_tags` / `axiom_graph_list_undocumented` | Tag enumeration; code with no linked doc |
| `axiom_graph_workflow_list` / `axiom_graph_workflow_detail` | The semantic layer: axiom-annotation workflow purpose, inputs/outputs, step ordering |

**Lifecycle and staleness:**

| Tool | Purpose |
|---|---|
| `axiom_graph_build` | Rebuild the index (discovery-only; safe to re-run) |
| `axiom_graph_check` | One-line staleness headline (own_status + link_status counts) |
| `axiom_graph_drift_query` | Filtered / grouped / paginated per-node drift detail |
| `axiom_graph_diff` | What changed in a node since a baseline commit |
| `axiom_graph_report` | Impact report since a checkpoint, SHA, or timestamp |
| `axiom_graph_history` / `axiom_graph_list_reference_points` | Change timeline; available baselines |
| `axiom_graph_mark_clean` | Record agent verification (clears promotable own_status) |
| `axiom_graph_apply_rename` / `axiom_graph_revert_rename` | Manually weld / un-weld a missed rename |
| `axiom_graph_checkout` | Isolated read-only DB snapshot |
| `axiom_graph_purge_node` | Remove a node from the index |

**Doc editing:** `axiom_graph_write_doc`, `axiom_graph_update_section`, `axiom_graph_patch_section` (append / prepend / unique-match replace), `axiom_graph_add_section`, `axiom_graph_delete_section`, `axiom_graph_delete_doc`, `axiom_graph_add_link`, `axiom_graph_delete_link`, `axiom_graph_update_doc_meta`, plus `axiom_graph_render_site` to republish the consumer site.

Docs at this layer are themselves DocJSON nodes — a section is its own node — so an agent can read, patch, and re-link documentation at section granularity, the same atomic unit it reads.

> `axiom_graph_report` is an MCP tool, not a CLI subcommand. The `report` view is reached through the agent (or the visualizer), not `axiom-graph report`.

## Keeping the Mesh Honest

Staleness is not a side feature — it is the engine that keeps the mesh worth trusting. When code drifts away from the docs and tests that link to it, the agent needs to *know*, then act. These tools turn drift into a workflow.

**Find the drift.** `axiom_graph_check` gives the headline (`own: N CONTENT_UPDATED … · link: N LINKED_STALE … · N VERIFIED`). For the specific nodes — filtered by status, scoped by path glob, grouped by feature, paginated so big drift volumes stay under the result cap — use `axiom_graph_drift_query`:

```
# Every LINKED_STALE node under one subtree, as bare IDs
axiom_graph_drift_query(project_root, filter="LINKED_STALE",
                        location_glob="src/viz/**", format="ids")
```

**Inspect a change.** `axiom_graph_diff` shows what changed in a node since its verified baseline — a line-scoped source diff for code, a section-level diff for docs — so the agent reviews only the delta, not the whole file again.

**Survive renames.** axiom-graph auto-matches renames by scoped similarity, but when a rename falls below threshold the old node goes `NOT_FOUND` and the renamed code is indexed fresh. `axiom_graph_apply_rename(old_id, new_id)` welds them — migrating the old node's history, verification, and edges onto the new identity — and `axiom_graph_revert_rename(new_id)` un-welds it (idempotent). Because consumer docs link *through* a dev-doc proxy rather than at raw symbols, those links ride the rename and stay intact.

**Isolate reads.** `axiom_graph_checkout` produces a read-only DB snapshot, useful for parallel analysis while a build runs elsewhere.

**Clear it once fixed.** `axiom_graph_mark_clean` records that an agent re-verified a node, promoting drifted-but-now-correct `own_status` back to VERIFIED. `LINKED_STALE` is not mark-cleanable by fiat — the prose or test actually has to be updated.

This loop is also why these docs stay honest. Published consumer pages are DocJSON nodes linked through a dev-doc proxy to the code; because `consumer` is a transitive tag, a consumer page inherits `LINKED_STALE` when the code beneath it drifts. That inherited staleness is the signal to update the page; mark-clean / verification clears it; `render-site` republishes. This very site is built that way — axiom-graph dogfoods its own staleness engine. See [staleness](../concepts/staleness.md) and [the docs-honesty loop](../examples/docs-honesty-loop.md) for the full story, and [the mesh](../concepts/the-mesh.md) for how the typed edges underpin all of it.

## Observability and Troubleshooting

**Log level.** `AXIOM_GRAPH_LOG_LEVEL` (default `INFO`) controls verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. At `INFO` the server already logs every tool call with its duration; `DEBUG` adds SQL, parser internals, and edge-resolution detail. Logs always go to stderr (stdout is reserved for the protocol). Set `AXIOM_GRAPH_LOG_FILE` to *also* write to a rotating file (5 MB × 2 backups), then `tail -f` it to watch an agent's search → source → graph sequence in real time:

```json
{
  "env": {
    "AXIOM_GRAPH_LOG_LEVEL": "DEBUG",
    "AXIOM_GRAPH_LOG_FILE": "/tmp/axiom-graph-mcp.log"
  }
}
```

**Common issues:**

| Symptom | Cause and fix |
|---|---|
| No tools appear | `command` is not the venv Python where axiom-graph is installed. Verify with `python -c "import axiom_graph"`. |
| Tool calls fail immediately | No `.axiom_graph/graph.db`. Run `axiom-graph build .` first. |
| "database is locked" | Two *writers* contend (e.g. two builds). The DB runs in WAL mode, so unlimited readers are fine — just serialize writes (one build at a time). |
| Slow first response | First build/scan, or a large project. Subsequent builds are incremental. |

**Error shape.** When a tool fails (bad node ID, parse error, validation rejection) it returns a structured JSON-RPC error with a stable code and a human-readable message, logs the cause to stderr, and **keeps the connection up**. A single bad call never poisons a long agent session — only catastrophic failures (DB corruption, OOM) bring the server down. Agents that retry should match on the error code, not the message text.
