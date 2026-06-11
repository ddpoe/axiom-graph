<!-- generated from axiom_graph::docs.consumer.get-started.use-the-cli @ d8df31693484; do not edit -->

# Use the CLI

## Overview

The `axiom-graph` command-line tool is the human path into the mesh. It indexes your project into a typed graph of nodes and intent-typed edges, then lets you explore that graph, check it for documentation drift, and publish a docs site from it.

If you are wiring up an AI agent, the MCP server is the primary surface and most of these commands have a tool equivalent there ([connect your agent](connect-your-agent.md)). This page is for a person driving the tool directly from a terminal.

Every command takes a `PROJECT_ROOT` argument (commonly `.` for the current directory) and reads or writes the index at `.axiom_graph/graph.db` inside that root. The general shape is:

```bash
axiom-graph <command> [flags] PROJECT_ROOT
```

The rest of this page walks the lifecycle you will actually drive: install, init, build, explore (`list`, `render`, `graph`), check for drift, resolve it (`mark-clean`, the rename family), inspect change history, export, and launch the dashboard.

## Install

`axiom-graph` requires Python 3.10 or later. Install it with pip:

```bash
pip install axiom-graph
```

This gives you the core CLI (`axiom-graph`) and the MCP server (`axiom-graph-mcp`). Two optional extras are available:

| Extra | What it adds |
|---|---|
| `viz` | The visualization dashboard (FastAPI + Uvicorn) |
| `js` | JavaScript / TypeScript indexing (tree-sitter) |

Install extras with bracket syntax:

```bash
pip install axiom-graph[viz]
pip install "axiom-graph[viz,js]"
```

> The `semantic` (embeddings) extra is deprecated as of 2.1.0 and slated for removal in 3.0. Semantic search is no longer part of the recommended workflow.

## Index your project: init and build

Run `init` once to create the index. It scans every source file, discovers functions, classes, and modules as nodes, wires up the edges between them, and records the baseline hashes that drift detection compares against later:

```bash
axiom-graph init .
```

The index lands at `.axiom_graph/graph.db` inside your project. If a database already exists, `init` prompts for confirmation before wiping and rebuilding from scratch. Use `--id <prefix>` to set a custom project-ID prefix (it defaults to the directory name).

After the first build, use `build` for incremental updates. It runs in discovery-only mode: new nodes are inserted and edges are refreshed, but existing nodes keep their verification baselines, so your hard-won [staleness](../concepts/staleness.md) signals are preserved. `build` also runs annotation validation and the rename matcher (covered under resolve drift, below) as part of the same pass.

```bash
axiom-graph build .
```

When source files are deleted, their nodes are marked `NOT_FOUND` rather than removed. Add `--purge` to delete those dead nodes once you are confident the removal is intentional:

```bash
axiom-graph build --purge .
```

To reset every baseline and start over, run `init` again rather than `build`.

## Explore the index: list, render, graph

Indexing pays off when you can pull exactly the context you need instead of grepping or reading whole files. Three commands cover most exploration.

**`list`** enumerates nodes, with optional filters by type or tag:

```bash
axiom-graph list .
axiom-graph list --type function .
axiom-graph list --tag consumer .
```

**`render`** prints node detail at a chosen verbosity. Level 0 is IDs only, 1 is the one-line summary, 2 is the full body, and `steps` renders the annotated orchestration highways for workflow nodes (see [annotations](../concepts/annotations.md)). Pass `--id` to render a single node, or `--type` to filter:

```bash
axiom-graph render --level 1 .
axiom-graph render --level 2 --id axiom_graph::axiom_graph.cli::cmd_init .
```

**`graph`** traces the typed edges out of (or into) a node, so you can follow a dependency chain straight to the linked nodes instead of searching for call sites. `--direction` is `out` (dependencies), `in` (callers), or `both`; `--depth` controls hops:

```bash
axiom-graph graph axiom_graph::axiom_graph.cli::cmd_init .
axiom-graph graph axiom_graph::axiom_graph.cli::cmd_init --direction both --depth 2 .
```

Most edges are discovered automatically, but you can add one the scanners can't infer — say, linking a doc node to the code it describes — with `axiom-graph link FROM_ID EDGE_TYPE TO_ID` (doc→code links are more often declared in a DocJSON section's `links` array and picked up on `build`, so reach for `link` only for the occasional manual edge).

Because nodes and edges are the same mesh that drift detection reads, traversing here is reading the very structure that keeps the docs honest.

## Check for drift

`check` is how you ask whether documentation has fallen behind code. It re-runs the staleness engine across the whole project and prints a one-line summary across two dimensions — a node's own content vs. its verified baseline (`own_status`), and the health of the dependencies it links to (`link_status`):

```bash
axiom-graph check .
```

The summary looks like:

```
own: 2 CONTENT_UPDATED / 1 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 3 LINKED_STALE / 0 BROKEN_LINK · 41 VERIFIED
```

The statuses you will see most often:

| Status | Dimension | Meaning |
|---|---|---|
| `VERIFIED` | both | Matches the verified baseline; nothing to do. |
| `CONTENT_UPDATED` | own | Body changed since last verification. Clear with `mark-clean`. |
| `DESC_UPDATED` | own | Heading or docstring changed. Clear with `mark-clean`. |
| `RENAMED` | own | Identity moved via a rename. Clear with `mark-clean`. |
| `NOT_FOUND` | own | File or node no longer on disk. Cannot be marked clean. |
| `LINKED_STALE` | link | A documented/validated dependency changed after this node — update the prose or test. |
| `BROKEN_LINK` | link | An outbound edge points at a node that no longer exists. |

`LINKED_STALE` is the signal at the heart of the docs honesty loop: when code drifts, the dev docs that document it go `LINKED_STALE`, and because `consumer` is a transitive tag, the user-facing pages built from them inherit that staleness too. Editing the prose alone won't clear it — only re-verifying the section with `mark-clean` does, so review what changed, fix the prose if needed, then mark it clean.

Useful flags:

```bash
axiom-graph check --all .                 # include VERIFIED nodes
axiom-graph check --format json .         # machine-readable, per-node detail
axiom-graph check --fail-on stale .       # exit 1 if anything is stale
axiom-graph check --strict-annotations .  # exit 1 on annotation findings
```

`--fail-on` accepts `none`, `stale` (own-status drift plus `LINKED_STALE` / `BROKEN_LINK` / `NOT_FOUND`), `unverified` (any non-`VERIFIED` own_status), or `any`. This is the gate you wire into CI and pre-push hooks. For per-node detail beyond the headline, the `drift_query` MCP tool returns paginated, filtered, and grouped rows.

For the conceptual model behind these statuses, see [staleness](../concepts/staleness.md).

## Resolve drift: mark-clean and renames

Once `check` flags drift, two families of commands resolve it.

**`mark-clean`** records that a node's documentation is still accurate after a change, resetting its baseline so it reads `VERIFIED` again. It applies to own-status drift — `CONTENT_UPDATED`, `DESC_UPDATED`, and `RENAMED`:

```bash
axiom-graph mark-clean axiom_graph::axiom_graph.cli::cmd_init . --reason "Whitespace only; behavior unchanged"
```

The `--reason` is stored with the verification record so a later reviewer (human or agent) understands why the change was waved through. The same verification snapshot also clears `LINKED_STALE` on the section you mark — in fact `mark-clean` is the *only* thing that does (editing the prose alone won't), so review the linked prose/test against what changed before you mark it. `BROKEN_LINK` is the exception: repair or remove the dangling link rather than marking it clean.

**The rename family** handles symbols that moved. During `build`, a scoped-similarity matcher detects most renames automatically and flags the new node `RENAMED`. When a rename falls below the similarity threshold and the matcher misses it, weld it by hand:

```bash
axiom-graph rename apply OLD_ID NEW_ID .
```

The old ID must be an existing `NOT_FOUND` node and the new ID a freshly created live node. On success, the old node's history, verification records, and edges migrate to the new ID, which is marked `RENAMED`. To undo a weld and restore the original identity, run:

```bash
axiom-graph rename revert NEW_ID .
```

This re-runs the migration in reverse and removes both rename rows so re-reverting is safe. Keeping identity stable across renames is also why consumer pages link through dev-doc proxies rather than raw symbols — the link survives the rename.

## Inspect change history

Every content change is recorded in a per-node history log, tagged with its git SHA. (For the concept — the append-only table that drift, diffs, and time travel all read — see [history](../concepts/history.md).) Two commands surface that log from the terminal.

**`history checkpoint`** drops a semantic marker into the history of qualifying nodes — a "known-good" reference point you can later diff or report against. It does not prune; it just marks:

```bash
axiom-graph history checkpoint .
axiom-graph history checkpoint --message "v2.1 release baseline" .
```

By default it marks code nodes (`atomic_process,composite_process`); override with `--node-types`.

**`history agent-verified`** lists nodes an agent marked verified that are still awaiting human review. It is the human sign-off queue — pair it with the CI gate when certain docs need an explicit person, not just an agent, to approve:

```bash
axiom-graph history agent-verified .
```

The impact report that summarizes everything that changed since a checkpoint (or a SHA, or a date) is available both as the `axiom-graph report` CLI subcommand and as the `axiom_graph_report` MCP tool. Agents typically generate it; see [connect your agent](connect-your-agent.md). For a worked end-to-end change-and-report flow, see the [reporting pipeline example](../examples/reporting-pipeline.md).

## Export the index

To get the full index out as portable JSON — for an external tool, a custom report, or archiving — use `export`:

```bash
axiom-graph export .
```

This writes the complete index to `.axiom_graph/index.json` next to the database. It takes no flags.

## Launch the dashboard

For a visual view of the mesh — nodes, edges, staleness status, and the documentation-to-code relationships — launch the dashboard:

```bash
axiom-graph viz .
```

This starts a local web server on port 8080 and opens your browser. Change the port or suppress the browser as needed:

```bash
axiom-graph viz --port 9090 --no-browser .
```

The dashboard requires the `viz` extra (`pip install axiom-graph[viz]`). For a tour of what it offers — including the diff view for drifted nodes — see [the visualization guide](../viz.md).

## Next steps

- **Wire up an agent** — most of these commands have an MCP tool equivalent, and that is the primary integration surface ([connect your agent](connect-your-agent.md)).
- **Tune behavior** — staleness rules, transitive tags, viz, and site settings live in configuration ([configuration](configuration.md)).
- **Understand the signals** — the conceptual model behind `check` ([staleness](../concepts/staleness.md)).
- **Publish docs honestly** — how this very site is rendered from DocJSON and kept honest by the mesh ([docs honesty loop](../examples/docs-honesty-loop.md)).
