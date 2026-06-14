<!-- generated from axiom_graph::docs.consumer.viz @ 51341425220d; do not edit -->

# The Viz Dashboard

## A human window onto the mesh

The viz dashboard is a local web app for browsing your project's [mesh](concepts/the-mesh.md) by eye. It is a convenience layer, not the main interface: agents read the mesh through the [MCP server](get-started/connect-your-agent.md) and most maintenance happens at the [CLI](get-started/use-the-cli.md). The dashboard exists for the moments a human wants to *see* the graph - to spot where staleness is spreading, trace how a function connects to its docs, or edit a DocJSON section in a rich editor.

It reads the same indexed database the rest of axiom-graph reads (`.axiom_graph/graph.db`), so nothing it shows is computed specially for the UI. The graph, the staleness rings, the node details - all of it is the persisted mesh, rendered. Run `axiom-graph build` at least once before launching so there is an index to read.

## Launch it

Start the dashboard from a terminal, pointing at a project root:

```bash
axiom-graph viz /path/to/your/project
```

The server starts on port 8080 and opens your browser automatically. Override the port or suppress the browser as needed:

```bash
axiom-graph viz /path/to/your/project --port 9090
axiom-graph viz /path/to/your/project --no-browser
```

Then visit `http://127.0.0.1:8080` (or your chosen port). The dashboard requires the viz extras:

```bash
pip install axiom-graph[viz]
```

It is a local, single-user tool - no authentication, no remote deployment. Launching auto-registers the project in `~/.axiom_graph/projects.json`, which is what makes the project switcher (below) work across sessions and across git worktrees of the same repo.

## The five tabs

The header has five tabs, each a different view of the same mesh:

| Tab | What it is for |
|---|---|
| **Graph** | Interactive dependency graph (Cytoscape.js). Best for understanding how components relate. |
| **List** | Sortable, filterable table of every indexed node, with a source preview panel. Best for triaging staleness or browsing the full inventory. |
| **Docs** | Full DocJSON document manager: folder tree, section editor, link picker, Mermaid diagrams. |
| **Workflows** | The annotated orchestration highways - `@workflow` / `@task` definitions with their named step sequences. |
| **Tests** | Test functions with tier badges and the code nodes each one validates. |

The active tab is remembered for the session. The Graph and List views share one set of sidebar filters, so a filter set in one carries into the other.

## Graph and List: two views, shared filters

**Graph view** renders nodes and intent-typed edges (`composes`, `delegates_to`, `depends_on`, `validates`, `documents`, `consumes`) as a node-link diagram. Nodes are colored by subtype - function (green), module (blue), docjson (orange), test (red), entity (purple), external package (grey) - and config nodes pulled from `.claude/` get their own distinct color so agent-config files stand out. Staleness shows as a colored border ring, so drift is visible at a glance rather than something you have to query for.

Five layouts are available (Force is the default; also Hierarchy, BFS, Grid, Concentric). Click a node to enter **focus mode** - a neighborhood-depth slider (1-4 hops) narrows the view to a local ego-network instead of the whole graph, which is the context-reduction idea applied visually: see exactly the connected nodes, not everything. Edge-type toggles and a "hide isolated nodes" checkbox trim the rest. For very large projects the dashboard starts in List view and offers to load the full graph on demand.

**List view** is the same nodes as a table (Name, Type, Location, Staleness, Tags) with sortable columns, a column-visibility menu, and five grouping modes (Type, Subtype, Module/File, Staleness, Tag). The shared sidebar filters by node type, subtype, staleness status, and tag, plus visibility toggles for private (`_`) functions and test files (both hidden by default). Markdown config files render an inline preview. Click a row to open the **source preview**: code nodes show in a Monaco editor with the relevant lines highlighted; a **diff toggle** compares the current source against a previous commit. Checkboxes drive bulk operations - select several nodes and "Verify Selected" to mark them reviewed, or "View in Graph" to jump to them in focus mode.

## The detail drawer

Clicking a node in either Graph or List opens a detail drawer with five tabs:

- **Overview** - type, subtype, staleness status, ID, location, tags, one-line summary. If the node is stale, a *Staleness Cause* block says why (content changed, description changed, a linked node went stale, or a broken link). A *Mark Verified* button records that you reviewed it.
- **Docs** - the node's description (Level 1) and detailed documentation (Level 2).
- **API** - interface info, docstring, and the node's annotated step sequence if it has one. *View Source* jumps to the source panel.
- **Relationships** - inbound and outbound edges grouped by type, plus test-coverage links. Click any to navigate there.
- **History** - the change log with commit SHAs and dates; click a SHA for a diff of that commit against the current source.

The Staleness Cause block is worth dwelling on: it is the same drift signal the rest of the system runs on, made legible (drift is a [read on the mesh](concepts/staleness.md)), and *Mark Verified* is how you tell the mesh a node is fine again. The drawer also shows verification attribution (who verified, when, and any reason).

## Editing docs in the browser

The Docs tab is a full manager for the DocJSON files in your `docs/` directory. A folder tree on the left organizes documents; a filter panel narrows by tag, search term, or path. Because each [DocJSON](concepts/docjson.md) section is its own node, the editor works at section granularity:

- **Edit a section** in a rich-text editor (headings, bold, lists, tables, code blocks); changes save back to the file on disk.
- **Manage sections** - add, reorder by dragging, rename headings and slugs inline, delete. A table-of-contents sidebar tracks scroll position.
- **Attach links** - a link picker searches for nodes and attaches them as provenance links, the typed edges that bind a section to the code it documents.
- **Render Mermaid** diagrams from section content, with a dedicated Monaco diagram editor and live preview.
- **Raw mode** toggles between the rich editor and the underlying JSON.

This is the same editing surface used to maintain the docs you are reading - the [docs-honesty loop](concepts/staleness.md) in practice.

## Workflows and tests

**Workflows** surfaces the semantic layer - the annotated [orchestration highways](concepts/the-mesh.md). If your code uses `@workflow` / `@task` decorators, the tab lists every discovered workflow and task; selecting one shows its step sequence with step numbers, names, purposes, inputs, outputs, and critical-path flags. Filters narrow by module or show only items with steps, critical steps, or linked nodes. A Monaco viewer highlights the step lines in source. This is deliberately not a full call graph - it is the intentional highways through your orchestration, with their step names attached.

**Tests** lists every indexed test function with a tier badge (T1 unit, T2 integration, T3 end-to-end) and shows which code nodes each test `validates`. Filter by module, tier, or step annotations; selecting a test opens its source with relevant lines highlighted, and fixture relationships appear when available.

## Search, time travel, and project switching

**Search** - the header search bar does keyword full-text matching, filtering both Graph and List to the matching nodes at once.

**Changed Since** - the sidebar narrows the display to nodes that changed after a reference point. Quick presets cover *Last Checkpoint*, *Last Commit*, and *24h*; for finer control, *Browse...* opens a commit picker that lists recent commits - pick one to filter since that point, or check two to define a range. "Changed" is a true **net diff** of the current index against that point: a node you edited and then reverted within the window cancels out and won't show up. Each changed row carries a **change-kind badge** - *added*, *content*, *descriptor*, *content+descriptor*, *renamed*, or *deleted* - and a kind filter lets you narrow to just one kind (e.g. only renames). The net kinds are on by default; a *link* toggle is shown but disabled (links/tags net-membership is deferred). Deleted nodes appear as **ghost nodes**: dimmed, struck-through rows synthesized from preserved history, so a node disappearing is itself visible rather than silent - and their pre-deletion source is recovered from git so you can still inspect what was removed. Both the since-query and those ghost rows are reads against the [history log](concepts/history.md).

**Check and Rescan** - two header buttons. *Check* recomputes staleness for all nodes and writes it back, without re-indexing. *Rescan* runs a full `axiom-graph build` to pick up new or changed files. (Staleness shown in the UI reflects the last build or check, not live disk state - *Check* is how you refresh it on demand.)

**Project switching** - the project name in the top-left is a dropdown. Switch between registered projects without restarting the server, or register a new one by entering its path. Projects auto-register the first time you launch `axiom-graph viz` against them - including separate git worktrees of the same repo, which register independently - so the switcher fills in as you work.
