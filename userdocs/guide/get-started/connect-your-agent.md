<!-- generated from axiom_graph::docs/consumer/get-started/connect-your-agent @ 0ff4c84fa946; do not edit -->

# Connect Your Agent

## Connect Your Agent

axiom-graph includes an MCP (Model Context Protocol) server. Once your coding agent is connected, it gets `axiom_graph_*` tools that search the index, return one function or one doc section at a time, follow the links between code and docs, and find and fix docs that no longer match the code. Claude Code, Cursor, VS Code and any other MCP client can use it. The [CLI](use-the-cli.md) and the [dashboard](../viz.md) work on the same index.

This page covers setup (install, build the index, add the server to your client), how an agent reads through the tools, the full tool list, and troubleshooting.

## Install

Install axiom-graph in a virtualenv:

```bash
pip install axiom-graph
```

The package includes the MCP server. Your client config will point at this virtualenv's Python.

## Build the Index

The server reads the index in `.axiom_graph/graph.db` under your project. Create it from the project root before you connect:

```bash
axiom-graph init .
```

Run `init` once. On a project that already has an index it asks for confirmation, then deletes the index and its history and starts over.

After code changes, `axiom-graph build .` adds new code and docs, refreshes links, and flags docs whose code changed. The agent can run the same build with `axiom_graph_build`, but only once an index exists. Until then every tool call fails with `No index at <project>/.axiom_graph/graph.db`. See [Use the CLI](use-the-cli.md) for both commands.

You don't need a build to see drift. Read tools such as `axiom_graph_search`, `axiom_graph_read_doc` and `axiom_graph_drift_query` bring the statuses they show up to date with the files on disk before they answer. They don't add new code to the index: when a file has functions the index lacks, the reply ends with a line such as `[utils.py has 2 new functions — run build]`. The `refresh_before_read` setting in [Configuration](configuration.md) controls this.

## Configure Claude Code

Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "axiom-graph": {
      "type": "stdio",
      "command": "/path/to/your/venv/bin/python",
      "args": ["-m", "axiom_graph.mcp_server"]
    }
  }
}
```

Set `command` to the Python inside the virtualenv where you installed axiom-graph. On Windows that is `<venv>\Scripts\python.exe`; in JSON, double the backslashes or use forward slashes. Running the module with that Python means the server always comes from that virtualenv, whatever is on `PATH`.

Restart Claude Code and approve the server when it asks. Run `/mcp` to check that `axiom-graph` is connected; its tools then appear in the agent's tool list.

## Configure Cursor and Other MCP Clients

The server talks over stdio, so any MCP client can start it. Add the same entry (`command`, `args`, and optionally `env`) to the client's config:

| Client | Config file | Key that holds the servers |
|---|---|---|
| Cursor | `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project | `mcpServers` |
| VS Code (Copilot agent mode) | `.vscode/mcp.json` | `servers` |

Other clients take the same entry; check their docs for the file and key.

The package also installs an `axiom-graph-mcp` command that starts the same server. It works as `command` when the virtualenv's scripts directory is on the client's `PATH`. Running it in a terminal is a quick check that the server starts: it waits for input until you press Ctrl+C.

## How Your Agent Reads the Project

The agent finds a node with `axiom_graph_search`, then reads only that node. Search returns one line per hit: the node id, a summary, and where it lives, with a line range for functions.

```
axiom_graph_search(project_root, "invoice")
```

```
[2 of 2 results -- fts ranked]
myproject::billing.invoices::issue_invoice  issue_invoice(order) — Create an invoice for an order.  @ billing/invoices.py#L40-L72
myproject::docs/billing::invoices           Invoices  @ docs/billing.docjson
```

With an id, the agent reads one thing:

| Call | Returns |
|---|---|
| `axiom_graph_source(project_root, node_id)` | The node's source: a function's lines, or a whole module |
| `axiom_graph_read_doc(project_root, doc_id, outline=True)` | A doc's section tree: ids, headings and sizes |
| `axiom_graph_read_doc(project_root, section_ids=[...])` | Those sections and their subsections, as Markdown |
| `axiom_graph_graph(project_root, node_id, direction="in")` | The node's neighbours: its module, the doc sections that describe it, the tests that call it |

The graph records imports, which module holds which function, doc links and test calls. It does not record every call site, so to find all callers of a function, grep for it.

For a longer walk-through, see [the reporting pipeline example](../examples/reporting-pipeline.md).

## MCP Tools

Every tool except `axiom_graph_guide` takes `project_root`, the absolute path of the indexed project, as its first argument. Every tool returns plain text. A failed call returns an error message instead of a result, and the server keeps running.

When a client connects, the server sends the agent short usage instructions: the tool families below, and patterns such as reading a doc's outline before its sections and passing many ids in one call. You don't need to describe the tools in your `CLAUDE.md`. Subagents don't receive these instructions, so tell them to call `axiom_graph_guide` first; it returns the instructions plus one line per tool.

Navigate:

| Tool | What it does |
|---|---|
| `axiom_graph_search` | Full-text search over code and doc sections; `scope="code"` or `scope="docs"` narrows it |
| `axiom_graph_source` | The source of one node |
| `axiom_graph_graph` | A node's incoming and outgoing edges (`direction="in"`, `"out"` or `"both"`) |
| `axiom_graph_list` | List nodes, filtered by type, tag, parent or file location |
| `axiom_graph_list_tags` | Every tag in the index, with node counts |
| `axiom_graph_list_undocumented` | Nodes that no doc section links to |
| `axiom_graph_render` | One node, or a page of nodes, as text at detail level 0 to 3 |
| `axiom_graph_sql` | A read-only SQL query against the index |
| `axiom_graph_workflow_list` | Workflows, tasks and state machines, with their purpose and location |
| `axiom_graph_workflow_detail` | The ordered steps of one workflow or task |
| `axiom_graph_workflow_export` | Write the workflows you name (`workflow_ids`) or every workflow in some `files`, with the code their steps reach, to one shareable HTML page; `format="json"` writes a JSON bundle |

Docs:

| Tool | What it does |
|---|---|
| `axiom_graph_read_doc` | A doc as Markdown: whole, one section, chosen `section_ids`, or its outline |
| `axiom_graph_write_doc` | Create a DocJSON doc, or replace one whole; `expected_hash` refuses the overwrite if the doc changed since you read it |
| `axiom_graph_clone_doc` | Copy a doc to a new one; `set_sections` replaces named sections' content and `omit_sections` drops sections |
| `axiom_graph_update_section` | Replace a section's content, heading or id; `addresses=[...]` names the changed nodes the edit answers, `edits=[...]` batches many edits |
| `axiom_graph_patch_section` | Append to, prepend to, or replace one exact match in a section; takes `addresses=[...]` and `edits=[...]` the same way |
| `axiom_graph_add_section` | Add sections to an existing doc |
| `axiom_graph_delete_section` | Delete a section and its subsections |
| `axiom_graph_delete_doc` | Delete a doc and its file |
| `axiom_graph_update_doc_meta` | Change a doc's title or tags |
| `axiom_graph_add_link` | Link a doc section to the code it describes, so drift flags it |
| `axiom_graph_delete_link` | Remove links from a doc section |
| `axiom_graph_accept_doc_edits` | Keep sections edited outside the tools and verify their text |
| `axiom_graph_render_site` | Render the configured doc targets (Sphinx pages, README) |

Staleness:

| Tool | What it does |
|---|---|
| `axiom_graph_check` | One line of counts per status. It rechecks only what changed since the last check; `full=True` rechecks every file |
| `axiom_graph_drift_query` | The stale nodes with status and cause; filter by status and path, page, and group with `group_by="status"`, `"location_prefix"`, `"feature"` or `"node_kind"` (code, test, doc) |
| `axiom_graph_mark_clean` | Mark nodes verified after review, clearing their drift |
| `axiom_graph_reverify` | Verify a changed node and clear the `LINKED_STALE` it caused |
| `axiom_graph_purge_node` | Remove `NOT_FOUND` nodes (deleted code or docs) from the index |
| `axiom_graph_diff` | What changed in a node since it was last verified, or since a commit |
| `axiom_graph_history` | A node's change history |
| `axiom_graph_report` | What changed across the project since a checkpoint, commit or time |
| `axiom_graph_list_reference_points` | The checkpoints and commits `axiom_graph_report` can start from |
| `axiom_graph_apply_rename` | Record a rename the automatic matcher missed, keeping the node's history |
| `axiom_graph_revert_rename` | Undo an applied rename |

Index:

| Tool | What it does |
|---|---|
| `axiom_graph_build` | Re-index after code changes: add new nodes and refresh edges |
| `axiom_graph_checkout` | Copy the index into another directory, such as a git worktree |
| `axiom_graph_carry_forward` | After merging a worktree, copy the verifications made there (`worktree_path`) into this index; `dry_run=True` shows what would carry |
| `axiom_graph_guide` | The connect-time instructions plus one line per tool |
| `axiom_graph_info` | Project facts (id, docs roots, extensions) and the project's agent policy |

## Keep Docs Current

When code changes, the doc sections linked to it are marked `LINKED_STALE`. [Staleness](../concepts/staleness.md) explains every status. An agent finds and clears them with these tools.

Count drift with `axiom_graph_check`:

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 3 LINKED_STALE / 0 BROKEN_LINK · 912 VERIFIED
```

List the stale nodes with `axiom_graph_drift_query`, which filters by status and path, groups, and pages:

```
axiom_graph_drift_query(project_root, filter="LINKED_STALE", location_glob="docs/**", format="ids")
```

See what changed with `axiom_graph_diff(project_root, node_id)`. For a stale node it returns the old and new text since the last commit recorded before the node went stale (or, if that commit doesn't hold the node yet, the newest commit that holds it as it was before the change), so you see the change that flagged it. Pass `baseline_sha` to compare with any other commit; `baseline_reason` says which baseline was used.

Then clear each stale section one of three ways:

- If the doc is wrong, fix it with `axiom_graph_update_section` or `axiom_graph_patch_section`, and list in `addresses` the changed nodes the edit answers (the `via` nodes `drift_query` shows for the section):

  ```
  axiom_graph_update_section(project_root, "myproject::docs/billing::invoices", content="...",
                             addresses=["myproject::billing.invoices::issue_invoice"])
  ```

  An edit without `addresses` verifies the text but leaves the section `LINKED_STALE`. The section clears once every node it was flagged through is named; until then the reply ends with `still LINKED_STALE via: ...`.
- If the doc is still right, call `axiom_graph_mark_clean` on the section with a `reason`. This clears it, whatever flagged it.
- If you changed the code and the docs that describe it are still right, call `axiom_graph_reverify` on the code node. It clears the `LINKED_STALE` that node caused in one call.

Running `check` again does not clear `LINKED_STALE`, and neither does editing the doc file directly. `axiom_graph_accept_doc_edits` keeps a direct edit and verifies its text; a `LINKED_STALE` section stays stale until you clear it one of the three ways above. For a worked example, see [the docs-honesty loop](../examples/docs-honesty-loop.md).

## Troubleshooting

The server logs to stderr, because stdout carries the protocol. At the default level it logs each tool call and how long it took. For more, set environment variables in the server's `env` block:

```json
"env": {
  "AXIOM_GRAPH_LOG_LEVEL": "DEBUG",
  "AXIOM_GRAPH_LOG_FILE": "/tmp/axiom-graph-mcp.log"
}
```

`DEBUG` adds each SQL statement. `AXIOM_GRAPH_LOG_FILE` copies the log to that file, rotated at 5 MB with two backups, so you can `tail -f` it while the agent works. [Configuration](configuration.md) lists both variables.

| Symptom | Fix |
|---|---|
| No `axiom_graph_*` tools appear | `command` is not the Python where axiom-graph is installed. Check with `/path/to/your/venv/bin/python -c "import axiom_graph"`. |
| The client shows the server as failed or "Connection closed" | The server exited on start. Run the configured command by hand, for example `/path/to/your/venv/bin/python -m axiom_graph.mcp_server`, and read the error it prints. |
| Calls fail with `No index at .../.axiom_graph/graph.db` | Run `axiom-graph init .` in the project. |
| `database is locked` | Two processes wrote to the index at once, for example two builds. Run one build at a time. |
| Tools behave like the old version after an upgrade | The server keeps running the code it started with. Restart it; in Claude Code, use `/mcp`. |
