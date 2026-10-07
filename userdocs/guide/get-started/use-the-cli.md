<!-- generated from axiom_graph::docs/consumer/get-started/use-the-cli @ 3c17f5fd0082; do not edit -->

# Use the CLI

## Overview

The `axiom-graph` command indexes your project, checks its docs for drift, and lets you explore and publish what it indexed. If you are setting up an AI agent, use the MCP server instead; most commands here have an MCP tool that does the same thing ([connect your agent](connect-your-agent.md)).

Most commands take the project directory as their last argument, usually `.`:

```bash
axiom-graph <command> [options] PROJECT_ROOT
```

The index is stored at `.axiom_graph/graph.db` in the project unless `db_path` in `axiom-graph.toml` points elsewhere ([configuration](configuration.md)). `axiom-graph <command> --help` lists every option of a command.

## Install

axiom-graph needs Python 3.10 or later:

```bash
pip install axiom-graph
```

This installs the `axiom-graph` CLI and the `axiom-graph-mcp` server. Two extras add optional features:

| Extra | Adds |
|---|---|
| `viz` | The `axiom-graph viz` dashboard |
| `js` | JavaScript and TypeScript indexing; also list the files under `js_paths` ([configuration](configuration.md)) |

```bash
pip install "axiom-graph[viz,js]"
```

## Index your project: init and build

Run `init` once to create the index. It scans your code and docs, records each module, function, test and doc section as a node, links them, and stores the baseline that later drift checks compare against:

```bash
axiom-graph init .
```

`--id <prefix>` sets the project id, the part before `::` in every node id. It defaults to the id of the index being replaced, else the directory name, and `init` records the id as `project_id` in `axiom-graph.toml`. Every later `build` must use the same id, or it stops with an error naming both. To change the id, edit `project_id` and run `init` again.

Running `init` on a project that already has an index asks for confirmation, then deletes the index and starts over. Every verification and the whole change history are lost, so use `init` only to start fresh. It resets only the index: your `axiom-graph.toml` settings and your agent policy stay as they are.

When the build finishes, `init` also writes axiom-graph's default agent policy as an editable doc named `agent-policy` in your docs folder, unless the project already has one, and tells you where it wrote it. The policy is the rules your agents follow in this project, and the `axiom_graph_info` tool shows it to them. Edit the doc to change the rules. `init` never overwrites a policy you already have.

Three flags reset one thing each:

```bash
axiom-graph init . --policy    # restore the shipped agent policy
axiom-graph init . --settings  # reset axiom-graph.toml to the defaults
axiom-graph init . --all       # reset the index, the settings and the policy
```

`--policy` and `--settings` never touch the index. `--settings` lists each setting that differs from its default and asks before it rewrites the file; `project_id` is kept. `--policy` asks before it overwrites an existing policy doc, and needs an index. Pass both together if you want both, and add `--yes` to answer yes to their questions (without a terminal the answer is no). `--all` asks once, listing everything it will reset, and `--yes` never skips that question.

After that, run `build` whenever code or docs change:

```bash
axiom-graph build .
```

`build` adds new nodes, refreshes edges, removes the nodes of deleted files, detects renames and validates your annotations. Nodes already in the index keep their baselines, so drift stays flagged until you clear it. When a function changed, the docs and tests linked to it are marked `LINKED_STALE` in the same build. The report ends with the counts `check` would print:

```
Done.
  staleness     : own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 412 VERIFIED
```

If your `@workflow`, `@task`, `Step` or `AutoStep` markers have problems, the report also prints `Annotation findings: N (X new, Y resolved)` and lists the new ones ([annotations](../concepts/annotations.md)).

## Explore the index: list, render, graph

`list` prints one line per node with its id and summary. Filter with `--type` (`atomic_process` or `composite_process`; see [the ontology](../concepts/ontology.md)) or `--tag`:

```bash
axiom-graph list .
axiom-graph list --type atomic_process .
axiom-graph list --tag consumer .
```

`render` prints nodes at the detail set by `--level`, which is required: `0` ids only, `1` one-line summaries, `2` the full docstring or section text and the file location, `steps` the numbered steps of annotated workflows ([annotations](../concepts/annotations.md)). `--id` renders one node and `--type` filters by node type:

```bash
axiom-graph render --level 2 --id myproject::pkg.payments::charge .
```

`graph` prints a node's edges. `--direction out` (the default) shows what the node depends on; `in` shows what points at it, such as the doc sections that document it and the tests that validate it; `both` shows both. `--depth` (default 1) sets how many hops to follow:

```bash
axiom-graph graph myproject::pkg.payments::charge --direction in .
```

```
[atomic_process] myproject::pkg.payments::charge  @ pkg/payments.py#L40-L72
  incoming:
  <--[documents]-- myproject::docs/payments::charging
  <--[validates]-- myproject::tests.test_payments::test_charge
```

`link` adds an edge the scanners cannot find, such as a test that validates a function:

```bash
axiom-graph link myproject::tests.test_payments::test_charge --edge-type validates --to myproject::pkg.payments::charge .
```

The ontology must allow that edge type between the two nodes. Links from a doc section to code usually go in the section's `links` list instead ([DocJSON](../concepts/docjson.md)).

## Check for drift

`check` brings every node's status up to date, stores it, then prints a summary line and the nodes that need attention. It re-checks only the files that changed since the last check:

```bash
axiom-graph check .
```

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 1 LINKED_STALE / 0 BROKEN_LINK · 41 VERIFIED

NODE                                OWN_STATUS       LINK_STATUS
--------------------------------------------------------------------------
myproject::pkg.payments::charge     CONTENT_UPDATED  VERIFIED
myproject::docs/payments::charging  VERIFIED         LINKED_STALE  via myproject::pkg.payments::charge
```

`own` statuses compare a node with its last verified state; `link` statuses cover the nodes it links to.

| Status | Meaning | What to do |
|---|---|---|
| `CONTENT_UPDATED` | Its body changed. | Review it, then `mark-clean`. |
| `DESC_UPDATED` | Its docstring or heading changed. | Review it, then `mark-clean`. |
| `RENAMED` | It was matched as a renamed node. | Review it, then `mark-clean`. |
| `NOT_FOUND` | It is no longer on disk. | `build` or `purge` removes it. |
| `LINKED_STALE` | Something it documents or tests changed since it was verified. | Update the doc or test, then `mark-clean`. |
| `BROKEN_LINK` | A link points at a node that no longer exists. | Fix or remove the link. |

A doc or test is compared with the version of the code it was last verified against. If the code goes back to that version, `LINKED_STALE` clears on the next `check`.

Editing a doc does not clear `LINKED_STALE`. Saving it through an `axiom_graph_*` doc write tool does not clear it either; the tool verifies the doc's own text only. `mark-clean` clears it, and so does an agent that names the changed code in `addresses` when it saves the section with `axiom_graph_update_section` or `axiom_graph_patch_section`. [Staleness](../concepts/staleness.md) explains each status.

| Option | Effect |
|---|---|
| `--all` | Also list `VERIFIED` nodes and every annotation finding. |
| `--format json` | Print every node's statuses, the counts, the files that need a `build` and all annotation findings as JSON. |
| `--fail-on LEVEL` | Exit 1 if problems remain, for CI or a pre-push hook: `stale` (any status above), `unverified` (any own status other than `VERIFIED`) or `any` (anything not `VERIFIED`). Default `none`. |
| `--strict-annotations` | Exit 1 if there is any annotation finding. |
| `--full` | Re-check every file, not only the ones that changed. It gives the same statuses as a plain `check` and takes longer. |

`check` also prints the annotation findings line that `build` prints. A finding introduced since the last `build` shows as new on every `check` until the next `build` records it.

`check` never adds nodes. When a file has functions or sections the index does not have yet, it prints a line such as `utils.py has 2 new functions — run build`; run `build` to add them.

## Resolve drift: mark-clean, purge and renames

`mark-clean` records that you reviewed a node and it is still correct. The node returns to `VERIFIED`, and `--reason` is stored with the verification:

```bash
axiom-graph mark-clean myproject::docs/payments::charging . --reason "Prose still matches the new rounding rule"
```

It clears `CONTENT_UPDATED`, `DESC_UPDATED`, `RENAMED` and `LINKED_STALE` on the node you name, and records you, not an agent, as the verifier. If you name a whole doc whose `LINKED_STALE` comes from its sections, it lists those sections for you to mark instead.

`build` drops deleted files, but a function deleted from a file that still exists stays `NOT_FOUND` until you purge it:

```bash
axiom-graph purge myproject::pkg.payments::old_charge .
axiom-graph purge --all-not-found .
```

`purge` removes only `NOT_FOUND` nodes and refuses anything else. `--all-not-found` lists the nodes and asks before purging (`--yes` skips the question), and `--reason` is saved in each node's history. A module that reads `NOT_FOUND` only because a function inside it was deleted is kept; purge the function and the module clears on the next `check`. A syntax error makes functions read `NOT_FOUND` too: every function in a Python file, or the broken function in a JavaScript or TypeScript file. `purge` never removes any node of such a file: it names the file and tells you to fix it and run `check` again. `build --purge` only repeats the deleted-file pass that every build already runs.

`build` detects most renames itself and marks the new node `RENAMED` ([rename settings](configuration.md)). When it misses one, the old id shows `NOT_FOUND` and the new id appears as a new node. Join them by hand before you purge the old node:

```bash
axiom-graph rename apply myproject::pkg.old::charge myproject::pkg.payments::charge .
```

This moves the old node's history, verifications and edges to the new id and marks it `RENAMED`. The old node must be `NOT_FOUND` and the new one newly created. Doc links to the old id are changed to the new id. A linked doc stays verified if the code still matches the version the doc was checked against, and goes `LINKED_STALE` if the body changed too. If a doc file was busy with another write or could not be read, the command prints a warning that names it; point its links at the new id yourself. To undo it:

```bash
axiom-graph rename revert myproject::pkg.payments::charge .
```

## Accept hand-edited docs: stamps accept

A DocJSON section edited by hand, rather than through the doc tools, is indexed but not verified, and the next `build` warns about it. To keep the edit as it stands, accept it with `stamps accept`. This command takes the project directory first:

```bash
axiom-graph stamps accept . --list
axiom-graph stamps accept . myproject::docs/payments::charging
axiom-graph stamps accept . --all
```

`--list` shows the flagged sections and changes nothing; `--all` accepts every flagged section. `mark-clean` does not clear this flag. See [DocJSON](../concepts/docjson.md).

## Inspect change history

axiom-graph keeps a history of each node's changes, tagged with the git commit ([history](../concepts/history.md)).

`history checkpoint` records the current commit as a reference point on every node of the types in `--node-types` (default `atomic_process,composite_process`, every node). `--message` (`-m`) labels it. `report` starts from the latest checkpoint by default:

```bash
axiom-graph history checkpoint -m "v2.1 release" .
```

`report` lists what changed since a reference point:

```bash
axiom-graph report .
axiom-graph report --since-sha a1b2c3d --format condensed .
axiom-graph report --list-refs .
```

| Option | Effect |
|---|---|
| `--since-sha SHA` | Start at this commit (4 or more characters). An unknown SHA is an error. |
| `--since DATETIME` | Start at this ISO-8601 time. |
| `--format FORMAT` | `text` (one line per event, the default), `condensed` (grouped by container) or `json`. |
| `--change-type GLOB` | Only these change types, such as `*STALE*`. |
| `--node GLOB`, `--exclude-node GLOB` | Only, or never, these node ids. `--exclude-node` can be repeated. |
| `--node-type TYPE` | Only `atomic_process` or `composite_process` nodes. |
| `--list-refs` | List the checkpoints and commits you can start from. |

The [reporting pipeline](../examples/reporting-pipeline.md) walks through a change and its report.

`diff` prints, as JSON, how nodes changed since a commit. Without `--baseline`, a stale node is compared with the last commit recorded before it went stale (or, if that commit doesn't hold the node yet, the newest commit that holds it as it was before the change), and any other node with its last verification or checkpoint; `--summary` prints only the line counts:

```bash
axiom-graph diff myproject::pkg.payments::charge . --baseline HEAD~3 --summary
```

The node is found by its identity, so a function nobody touched shows no changes even when code above it moved. Several ids print one result each, separated by `---`, and the command exits 1 if any of them could not be diffed.

`history agent-verified` lists nodes whose latest history entry is a verification recorded by an agent, so a person can review them. Running `mark-clean` on a node takes it off the list:

```bash
axiom-graph history agent-verified .
```

## Export the index, or share it with a worktree

`export` writes the whole index, every node and edge, as JSON for other tools to read. The file is `index.json` beside the index database: `.axiom_graph/index.json`, unless `db_path` in `axiom-graph.toml` puts the database elsewhere. It records the project id and the axiom-graph version that wrote it:

```bash
axiom-graph export .
```

To share workflows with the source code behind their steps, use `workflows export` instead (next section).

`checkout` copies the index into another directory, usually a git worktree, so work there starts with the same history and verifications. It takes the target directory as its argument and the source project as `-p` (default `.`). A target that already has an index is skipped unless you pass `--force`:

```bash
axiom-graph checkout ../my-worktree -p .
```

Run `build` once in the worktree before you read its first `check`, then `build` and `check` as you work there, so its index matches its files. Until that first build, files the main checkout has but the worktree doesn't, such as untracked or gitignored ones, read `NOT_FOUND`. The build clears them, and `carry-forward` never brings them back to main.

`carry-forward` brings the worktree's verifications back. After you merge the worktree, run `build` in the main checkout, then `carry-forward` with the worktree's path. A node you verified in the worktree that has the same content and links here becomes verified here too, so you don't review it twice:

```bash
axiom-graph build .
axiom-graph carry-forward ../my-worktree --dry-run
axiom-graph carry-forward ../my-worktree
```

```
Carried 2 verification(s) from feat @ 620aa9b7e45e.
Stale here: 4 before -> 0 after.
Stale only through their children or linked sections (2).
```

A module or doc that was stale only through its functions or sections clears with them. A node the worktree verified at the content main holds, but which is still flagged for something else, carries in part: its own text, and each link whose linked node is at the version the worktree checked. Its other links keep the state they have here. Every other node stays stale, and the report says why: not verified in the worktree, content differs, a link is absent in the worktree, a linked node differs, or it is `NOT_FOUND`, `RENAMED` or `BROKEN_LINK` here. [Staleness](../concepts/staleness.md) shows the full report. When both branches changed the same thing, it matches neither and stays stale. A carried node that links to a doc section still stale here, or a workflow whose function or one of its tasks is still stale here, stays stale until that one is settled; the report counts these.

| Option | Effect |
|---|---|
| `--dry-run` | Show the summary and the nodes that would carry, in full and in part, and write nothing. It reads the statuses this checkout's last `build` or `check` stored, so run it after the build. |
| `--list` | List every stale node by verdict, on a dry run or a real one. |
| `-p`, `--project-root` | The checkout the worktree was merged into. Default `.`. |

`WORKTREE_PATH` can be the worktree directory or its `.axiom_graph/graph.db`. `carry-forward` refuses, and writes nothing, when the two indexes have different project ids or were written by axiom-graph versions with different index formats. An index left out of date in the worktree only means fewer nodes carry.

## Export workflows: workflows export

`workflows export` writes workflows, and every source file their steps reach, to one HTML page. The page opens in any browser, offline, without axiom-graph or your repository. It is the same page the dashboard's export button produces:

```bash
axiom-graph workflows export build_report .
axiom-graph workflows export --file reporting/pipeline.py -o pipeline.html .
axiom-graph workflows export build_report load_records --format json .
```

| Option | What it does |
|---|---|
| `IDS` | The workflows and tasks to export, by function name or node id. Workflows and tasks can be mixed. |
| `--file PATH` | Also export every workflow and task defined in that file. Repeat it for more files. |
| `--format` | `html` (the default) writes the page; `json` writes the same content as JSON. |
| `-o`, `--output` | The file to write. Default: `workflow-export.html` (or `.json`) in the current directory. |

The command prints the file it wrote and what it holds, such as `Wrote workflow-export.html: 1 workflow · 2 files`. A name or file that matches nothing is an error that names it, and nothing is written. The `viz` extra isn't needed; without it the code on the page is shown without syntax colouring. An agent writes the same page with the `axiom_graph_workflow_export` MCP tool.

[Share a workflow and its code](../examples/share-a-workflow.md) walks through an export and what the page shows.

## Launch the dashboard

`viz` starts the dashboard at `http://127.0.0.1:8080` and opens it in your browser. It needs the `viz` extra:

```bash
axiom-graph viz .
axiom-graph viz --port 9090 --no-browser .
```

See [the dashboard guide](../viz.md).

## Publish the docs site: render-site

`render-site` renders DocJSON docs to Markdown pages: a Sphinx site, a README, or any other target listed under `[[axiom_graph.site.targets]]` in `axiom-graph.toml`. With no targets configured, it renders the docs listed in `site-nav.yml` to `userdocs/guide`:

```bash
axiom-graph render-site .
axiom-graph render-site --target readme .
```

`--target NAME` renders only that target and can be repeated. `--build` also runs `sphinx-build`. `--nav PATH` and `--output DIR` render a single Sphinx tree from a nav file, ignoring the configured targets. Targets are described in [configuration](configuration.md); [multi-target rendering](../examples/multi-target-rendering.md) walks through an example.

## Next steps

- [Connect your agent](connect-your-agent.md): the MCP tools that match these commands.
- [Configuration](configuration.md): what gets indexed, the project id, rename and staleness settings, site targets.
- [Staleness](../concepts/staleness.md): how each status is detected and cleared.
- [The docs-honesty loop](../examples/docs-honesty-loop.md): keeping published docs in step with the code.
