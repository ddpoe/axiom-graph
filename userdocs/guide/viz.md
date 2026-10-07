<!-- generated from axiom_graph::docs/consumer/viz @ 6ad4a8c3b485; do not edit -->

# The viz dashboard

## Overview

The viz dashboard is a local web app for looking at your project's index: a graph of its nodes and edges, a sortable list, a DocJSON editor, and views of your annotated workflows and tests. It reads the same index as the [CLI](get-started/use-the-cli.md) and the [MCP server](get-started/connect-your-agent.md), and shows each node's drift status (see [staleness](concepts/staleness.md)), re-checking the files you changed when it loads. Nodes and edges are explained in [the mesh](concepts/the-mesh.md).

## Launch it

Install the `viz` extra, then start the dashboard on an indexed project:

```bash
pip install "axiom-graph[viz]"
axiom-graph viz /path/to/project
```

The server runs at `http://127.0.0.1:8080` and opens a browser tab. `--port` changes the port and `--no-browser` skips the tab:

```bash
axiom-graph viz /path/to/project --port 9090 --no-browser
```

Stop it with Ctrl+C. If the project has no index yet, `viz` stops with `No index found at ...`; index it first (see [use the CLI](get-started/use-the-cli.md)).

The dashboard is for one person on one machine: it listens only on `127.0.0.1` and has no login. The page loads its graph, editor and diagram libraries from public CDNs, so the browser needs internet access.

## The five tabs

| Tab | Use it to |
|---|---|
| **Graph** | see how nodes connect and where drift is |
| **List** | sort, filter and verify nodes, and read their source |
| **Workflows** | read `@workflow` and `@task` functions step by step |
| **Tests** | see test functions, how they are annotated, and what they validate |
| **Docs** | browse and edit DocJSON docs |

The dashboard reopens on the tab you last used in that browser session.

## Graph view and sidebar filters

The sidebar filters apply to both Graph and List; the other tabs have their own filter panels.

- **Staleness**: the summary at the top counts nodes per drift status. Click a count, or pick a status below it, to show only those nodes.
- **Tags**: search for tags and pick them; a node must carry every selected tag.
- **Subtypes** and **Node Types**: untick an entry to hide it.
- **Changed Since**: see [Search and Changed Since](#search-and-changed-since).

In the graph, a node's color shows its subtype and its ring shows its drift status. These controls apply to the graph only:

- **Edge Types**: one checkbox per edge type in your index, plus **Hide isolated nodes** (on by default).
- **Layout**: Force (the default), Hierarchy, BFS, Grid or Concentric.
- **Neighborhood depth** (1 to 4): how far the **Expand N hops** button in the detail drawer reaches when it adds a node's neighbors to the view.

Click a node to center it and open the [detail drawer](#the-detail-drawer). Edges that a workflow step follows are labelled with the step number.

A project with more than 400 nodes opens in List view. **Load full graph anyway** draws the whole graph; otherwise, selecting a node draws only its neighborhood.

## List view

The list shows the filtered nodes as a table with Name, Type, Location, Staleness and Tags columns. Click a header to sort, use **Columns** to hide columns, and use **Group By** to group rows by Module / File (the default), Type, Subtype, Staleness or Tag. Private (`_`) functions and files under your `test_paths` stay hidden until you tick them under **Visibility**.

Click a row to open it in the side panel:

- Code shows the whole file with the node's lines highlighted. **Focus** shows only the node's lines.
- A DocJSON section shows rendered, with a breadcrumb, **Up** and **Down** to move to the parent or first child section, **Full Doc** for the whole doc, and **Edit in Docs** to open it in the Docs tab.
- A Markdown file from your config folders, such as `.claude/`, shows rendered.

The **···** button beside a name opens the detail drawer, and the location link opens the file in VS Code.

To work through drift from the list:

- Click a stale status badge to see why the node is stale.
- Click ✓ on a stale row to verify it, with an optional reason. This is the same as `axiom-graph mark-clean`.
- Tick rows and click **Verify Selected** to verify them together.
- **Run Check** does the same as the header's **Check** button (see [Refreshing and switching projects](#refreshing-and-switching-projects)).
- **View in Graph** draws the ticked rows, or every listed row if none are ticked, in the Graph tab.

## The detail drawer

Clicking a node in the graph, or **···** in the list, opens a drawer with five tabs:

- **Overview**: type, drift status, id and location (with copy buttons), tags and summary. For a stale node it says why, links to the stale nodes behind the status, and offers **Mark Verified** with an optional reason (the same as `axiom-graph mark-clean`). A verified node shows who verified it, when and why.
- **Docs**: the node's one-line description and its longer documentation.
- **API**: parameters, return values and exceptions from the docstring, the docstring itself, and the node's annotated steps. **View Source** opens the node's source in the List tab's side panel.
- **Relationships**: the tests that validate the node, then its inbound and outbound edges grouped by type. Click a node to open it.
- **History**: the latest verification, any renames, and the node's change log. Click a commit to diff the node at that commit against its current source, in the List tab's side panel.

## Editing docs in the browser

The Docs tab shows every DocJSON doc in your docs folders (`docs_dirs` under `[axiom_graph.scan]`, `docs/` by default) as a folder tree, one top-level folder per docs folder. The tag panel narrows the tree. **+** creates a doc in the first docs folder, the buttons on a folder create a doc or subfolder inside it, and **↻** reloads docs that changed on disk.

Open a doc to read it rendered, with a **Contents** list beside it. To change it:

- Click **Edit** on a section, change it in the rich-text editor, and click **Apply**.
- Click a heading to rename it, or a section id to change the id.
- Move sections up or down, remove them, add a section, or add a sub-section under a top-level section.
- Add or remove tags on the doc and on each section.
- Search for a node and add it to a section's links, so the section is flagged when that node changes.
- **Diagram** opens a Mermaid editor with a live preview.
- **{ } Source** switches to the raw JSON.

Nothing is written until you click **Save**. Save writes the file directly rather than through the `axiom_graph_*` doc tools, so axiom-graph treats the change as a hand edit (see [DocJSON](concepts/docjson.md)); `axiom-graph stamps accept` keeps it and marks it verified.

## Workflows and tests

The **Workflows** tab lists functions marked with [`@workflow` or `@task`](concepts/annotations.md); the **Workflows** / **Tasks** toggle switches between them. Filter by module, or show only items that have steps, have a critical step, or are linked to a graph node. Select one to see its steps (number, name, purpose, inputs, outputs and any critical note) with its source beside them. Where an `AutoStep` calls another annotated function, that function's steps are listed under it with longer numbers such as `2.3.1`, and the `AutoStep` row shows the called function's purpose. Click a step to jump to its line in the source; **Graph** and **Source** open the function it calls.

The export button (⇩) opens a picker. Tick workflows, or every workflow in a file, and click **Export HTML** to open one standalone page with their steps and the source files behind them. `axiom-graph workflows export` and the `axiom_graph_workflow_export` MCP tool write the same page without the dashboard; [Share a workflow and its code](examples/share-a-workflow.md) walks through all three. The refresh button reruns the build to pick up new workflows.

The **Tests** tab lists test functions with a tier badge that shows how each is annotated:

- **T3**: `@workflow` with `Step` markers
- **T2**: `@workflow` without steps
- **T1**: no annotation

Filter by module or annotation, and group by module or tier. Select a test to see its steps, the code nodes it validates, its fixtures and its source.

## Search and Changed Since

**Search.** Type in the header search box to find nodes whose name, id, summary or documentation contains the text, ignoring case. The results open in the List tab, and the sidebar filters still apply. Clear the box to go back.

**Changed Since.** This sidebar filter narrows Graph and List to the nodes that changed after a point in history:

- **Last Checkpoint**: since the last checkpoint (`axiom-graph history checkpoint`).
- **Last Commit**: since the current HEAD commit.
- **24h**: in the last 24 hours.
- **Browse...**: pick from recent commits. Click a commit to filter since it, or tick two for a range. You can search commit messages and filter by date. Commits the index never recorded are faded but still work.

A node counts as changed if it differs between that point and the current index, so an edit you made and then reverted does not show (see [history](concepts/history.md)). Each changed row has a badge: `added`, `content`, `desc` (description changed), `content+desc`, `renamed` or `deleted`. The **Kinds** toggles hide or show each kind. Deleted nodes appear as dimmed, struck-through rows; click one to see its source as it was at the starting point.

While the filter is on, the side panel's **Diff** button compares a node with its version at the starting point: side by side for code, word by word for each doc section. If the index was built before the latest commit, a banner says how many commits behind it is; run `axiom-graph build` to catch up.

## Refreshing and switching projects

**Check** in the header re-checks the files that changed, like `axiom-graph check`, without re-indexing. The dashboard does the same each time it loads the graph, so a function you edited shows as changed without a build. To turn that off, set `refresh_before_read = "off"` in [configuration](get-started/configuration.md). Functions and sections the index has not seen yet need `axiom-graph build`; run it and reload the page. The refresh buttons on the Workflows and Tests tabs also rebuild.

The project name at the top left opens the project switcher. It lists your registered projects with their paths; click one to switch without restarting the server. To add a project, enter its path; the project must already be indexed. Projects are kept in `~/.axiom_graph/projects.json` and are added when you launch `viz` on them, switch to or add them, or copy an index into a worktree with `axiom-graph checkout`. A git worktree is listed separately, with `[wt: <folder>]` after its name. Projects whose folders no longer exist drop off the list.
