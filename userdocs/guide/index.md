<!-- generated from axiom_graph::docs/consumer/index @ 150a4aff0b7f; do not edit -->

# What is axiom-graph

## What it does

axiom-graph indexes a project's code, docs and tests into one graph, and when the code changes it tells you which docs and tests are now out of date. It is meant for projects where AI agents write much of the code and the docs have to keep up.

Agents use it through an MCP server. The `axiom_graph_*` tools let an agent search the project, read one function or one doc section at a time, follow the links between code, docs and tests, and update the docs a change made stale. A CLI and a browser dashboard give people the same view.

It runs locally. You install it with pip, there is no account, and the index is a SQLite file at `.axiom_graph/graph.db` in your project.

## Quick start

axiom-graph needs Python 3.10 or later. From your project's root:

```bash
pip install axiom-graph
axiom-graph init .
```

`init` scans the project and builds the index. Now change the body of a function that one of your tests calls, and run `check`:

```bash
axiom-graph check .
```

```
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 0 VERIFIED

NODE                                              OWN_STATUS       LINK_STATUS
----------------------------------------------------------------------------------------
myproject::reports                                CONTENT_UPDATED  VERIFIED
myproject::reports::build_report                  CONTENT_UPDATED  VERIFIED
myproject::tests.test_reports                     VERIFIED         LINKED_STALE
myproject::tests.test_reports::test_build_report  VERIFIED         LINKED_STALE  via myproject::reports::build_report
```

The function you edited is `CONTENT_UPDATED`, and its module shows the same status. The test that calls it is `LINKED_STALE`: code it depends on changed and nobody has reviewed the test since. Once you have checked that the test still fits the new code, mark both as verified and the list clears:

```bash
axiom-graph mark-clean myproject::reports::build_report .
axiom-graph mark-clean myproject::tests.test_reports::test_build_report .
```

A node id is `<project>::<module>::<function>`. The project id defaults to the directory name, and `init` records it as `project_id` in `axiom-graph.toml`. `axiom-graph list .` prints every id.

Docs work the same way: a doc section that links to the function goes `LINKED_STALE` when the function changes. Docs are DocJSON files under `docs/`, one node per section. The easiest way to write and link them is to [connect your agent](get-started/connect-your-agent.md) and let it use the `axiom_graph_*` doc tools. You can also do it in the [dashboard](viz.md)'s Docs tab.

`check` sees edits to anything already indexed. After you add files, run `axiom-graph build .` so new functions, tests and docs join the index.

The [reporting pipeline tutorial](examples/reporting-pipeline.md) runs the same loop on a project with linked docs.

## What gets indexed

Each thing axiom-graph indexes becomes a **node**, and the relationships between nodes are typed **edges**.

| Source | Nodes | Edges |
|---|---|---|
| Python files | modules, classes, functions | a module `composes` its functions; imports become `depends_on` |
| Tests (the `test*` functions pytest collects from `test_*.py` and `*_test.py` files) | test functions | a test `validates` each function it calls, found automatically |
| Docs under `docs/` (DocJSON and Markdown) | each document and each section | a section `documents` the code its links name |
| `@workflow` / `@task` markers | workflows, tasks and their steps | a marker `annotates` its function; an `AutoStep` step `delegates_to` the function it calls |
| Files under `.claude/` | one node per file | none |

JavaScript and TypeScript need the `js` extra (`pip install "axiom-graph[js]"`) and a `js_paths` list in `axiom-graph.toml` naming the files to scan. See [configuration](get-started/configuration.md).

The `@workflow`, `@task`, `Step` and `AutoStep` markers come from the `axiom-annotations` package. You add them to your main pipelines so the graph shows what runs in what order, without recording every function call. See [annotations](concepts/annotations.md). [The mesh](concepts/the-mesh.md) covers nodes, edges and ids, and the [ontology](concepts/ontology.md) lists every node and edge type.

## How drift is detected

`check` compares each node with the version that was last verified.

- A function whose body changed is `CONTENT_UPDATED`, and one whose docstring changed is `DESC_UPDATED`. For a doc section, a text change is `CONTENT_UPDATED` and a heading change is `DESC_UPDATED`.
- A node linked to a changed node becomes `LINKED_STALE`: a test that `validates` it, a doc section that `documents` it, a workflow that `annotates` it. The `via` column names the change.
- `LINKED_STALE` stays until you verify the node or the changed code goes back to the version it was verified against. Verify it with `axiom-graph mark-clean`. When an agent edits a doc section with `axiom_graph_update_section` or `axiom_graph_patch_section`, it can verify the section in the same call by naming the changed code in `addresses`. Saving the text alone, editing the file by hand or running `check` again does not clear it.

Drift travels only along these edges, not to every caller, so a change flags the docs and tests that describe or cover it. In CI, `axiom-graph check --fail-on stale .` exits 1 while anything is stale. [Staleness](concepts/staleness.md) has every status and clearing rule, and [history](concepts/history.md) covers the change log behind diffs and time travel.

## Ways to use it

- **MCP server, for agents.** Connect Claude Code, Cursor or any other MCP client and the agent gets the `axiom_graph_*` tools: `axiom_graph_search` to find nodes, `axiom_graph_source` and `axiom_graph_read_doc` to read one function or doc section, `axiom_graph_graph` to follow edges, `axiom_graph_check` and `axiom_graph_drift_query` for drift, and tools to write and link docs. `axiom_graph_clone_doc` starts a new doc as a copy of an existing one, and `axiom_graph_workflow_export` writes workflows and their code to one page you can share. See [connect your agent](get-started/connect-your-agent.md).
- **CLI, for people and CI.** `init`, `build`, `check` and `mark-clean`, plus commands to list and render nodes, walk the graph, show history and diffs, and render docs to a site or README. `axiom-graph workflows export` writes workflows and their code to one HTML page, and `axiom-graph carry-forward` copies the verifications made in a merged git worktree into your main checkout's index. See [use the CLI](get-started/use-the-cli.md).
- **Dashboard, for browsing.** Install the `viz` extra (`pip install "axiom-graph[viz]"`) and run `axiom-graph viz .` to open a browser UI with Graph, List, Docs, Workflows and Tests tabs. You can edit doc sections and add links in the Docs tab. See [the dashboard](viz.md).

## The PEV Agent Nexus

The PEV Agent Nexus (PEV) is a Claude Code plugin that runs a code change through Plan, Execute and Validate phases. Separate agents plan the change (Architect), implement it with tests (Builder), review it (Reviewer) and update the docs it made stale (Auditor), with a human approval gate between phases. The agents read and write the project through the axiom-graph MCP tools. You start a cycle with `/pev-cycle <task>`, or `/pev-instance <task>` for a small change done by one agent. See [PEV Agent Nexus](pev/overview.md).

## Where to go next

- **Concepts:** [the mesh](concepts/the-mesh.md) (nodes, edges, ids), the [ontology](concepts/ontology.md) (every node and edge type), [DocJSON](concepts/docjson.md) (the doc format and linking), [annotations](concepts/annotations.md) (the workflow markers), [staleness](concepts/staleness.md) (drift statuses and clearing) and [history](concepts/history.md).
- **Setup:** [connect your agent](get-started/connect-your-agent.md), [use the CLI](get-started/use-the-cli.md) and [configuration](get-started/configuration.md).
- **Tutorials:** the [reporting pipeline](examples/reporting-pipeline.md) (the full drift loop), the [docs honesty loop](examples/docs-honesty-loop.md) (keeping user-facing docs in step with the code, as this guide does), [multi-target rendering](examples/multi-target-rendering.md) (one set of docs rendered to a site, a README and plugin docs) and [share a workflow](examples/share-a-workflow.md) (export a workflow and its code to one page).
- **Tools:** the [dashboard](viz.md) and [PEV](pev/overview.md).

```{toctree}
:maxdepth: 2

concepts/index
get-started/index
pev/index
examples/index
viz
```
