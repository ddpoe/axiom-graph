<!-- generated from axiom_graph::docs/consumer/examples/share-a-workflow @ 151c94c53253; do not edit -->

# Tutorial: Share a workflow and its code

## What you'll build

axiom-graph can export workflows as one HTML page that shows their numbered steps next to the source code those steps run through. Anyone with a browser can open the page; they don't need axiom-graph or a copy of your repository. You can export from the dashboard, from the command line with `axiom-graph workflows export`, or by asking an agent, which uses the `axiom_graph_workflow_export` MCP tool. All three write the same page.

In this tutorial you write a small project with a workflow that hands off to a task in another file, index it, export the workflow, and look at what the page contains.

Install axiom-graph with the `viz` extra, and the `axiom-annotations` package that your code imports the markers from:

```
pip install "axiom-graph[viz]" axiom-annotations
```

The dashboard needs the `viz` extra. The command-line and agent exports work without it, but the code on the page is then shown without syntax highlighting.

## Step 1: Write the code

Create these files in a new directory:

```
axiom-graph.toml
reporting/__init__.py        (empty)
reporting/sources.py
reporting/pipeline.py
```

`axiom-graph.toml` sets the project id, which starts every node id:

```toml
[axiom_graph]
project_id = "proj"
```

`reporting/sources.py` holds a task with two steps:

```python
import csv

from axiom_annotations import task, Step


@task(purpose="Read the sales CSV and keep complete rows", inputs="CSV path", outputs="list of rows")
def load_records(path):
    s = Step(step_num=1, name="Read CSV", purpose="Parse the file into one dict per row")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    s = Step(step_num=2, name="Drop incomplete rows", purpose="Keep rows that have a region and an amount")
    return [r for r in rows if r.get("region") and r.get("amount")]
```

`reporting/pipeline.py` holds the workflow. Step 1 is an `AutoStep`: the call on the next line, to `load_records`, is the step. Steps 2 and 3 are plain `Step` markers:

```python
import json

from axiom_annotations import workflow, Step, AutoStep

from reporting.sources import load_records


@workflow(purpose="Build the weekly sales report", inputs="CSV path", outputs="totals as JSON")
def build_report(path, dest):
    s = AutoStep(step_num=1, name="Load records")
    records = load_records(path)

    s = Step(step_num=2, name="Total by region", purpose="Sum the amounts for each region")
    totals = {}
    for r in records:
        totals[r["region"]] = totals.get(r["region"], 0) + float(r["amount"])

    s = Step(step_num=3, name="Write report", purpose="Save the totals as JSON")
    with open(dest, "w") as f:
        json.dump(totals, f, indent=2)
```

The statement after an `AutoStep` must call the function directly, as in `records = load_records(path)`. [Annotations](../concepts/annotations.md) explains every marker argument and the numbering rules.

## Step 2: Index the project and check the steps

Run these commands from the project root. Index the project:

```
axiom-graph init .
```

The output ends with a status line:

```
Done.
  staleness     : own: 0 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 12 VERIFIED
```

If the output also has an `Annotation findings` line, fix the markers it lists before you export.

Print the workflow's steps:

```
axiom-graph render --level steps --id proj::reporting.pipeline::build_report .
```

```
=== proj::reporting.pipeline::build_report ===
  [1] Load records
       purpose :
  [2] Total by region
       purpose : Sum the amounts for each region
  [3] Write report
       purpose : Save the totals as JSON
```

Step 1 has no purpose of its own. To check that it hands off to `load_records`, list its edges:

```
axiom-graph graph proj::reporting.pipeline::build_report::step-1 .
```

```
[atomic_process] proj::reporting.pipeline::build_report::step-1  @ reporting/pipeline.py#L10
  outgoing:
  --[delegates_to]--> proj::reporting.sources::load_records
```

An agent reads the same workflow with `axiom_graph_workflow_detail`, which also lists the steps of `load_records` under step 1.

## Step 3: Export the workflow

Use whichever route fits: the dashboard, the command line or your agent.

**From the dashboard.** Start the dashboard:

```
axiom-graph viz .
```

It opens `http://127.0.0.1:8080` in your browser. Then:

1. Open the **Workflows** tab.
2. Click the export button (⇩) in the header of the workflow list. A picker opens with the workflows grouped by file.
3. Tick `build_report`. Ticking a file name selects every workflow in that file; the search box and **Select all** help with long lists.
4. Click **Export HTML**. The page opens in a new browser tab.
5. Save the tab as a file (Ctrl+S, or Cmd+S on macOS) and send that file.

The picker lists what the **Workflows** / **Tasks** toggle shows. `load_records` is a task, so it isn't listed, and you don't need to pick it: the export follows step 1 into it.

**From the command line.** `workflows export` writes the page to a file without starting the dashboard, so it also works in a script or CI job:

```
axiom-graph workflows export build_report -o build-report.html .
```

```
Wrote build-report.html: 1 workflow · 2 files
```

Name workflows and tasks by function name or node id, as many as you like, and mix the two. `--file reporting/pipeline.py` adds every workflow and task defined in that file. Without `-o`, the page goes to `workflow-export.html` in the current directory. `--format json` writes the same content as JSON instead of a page. A name or file that matches nothing stops the command with an error naming it, and nothing is written.

**From your agent.** Ask an agent connected to the MCP server to export `build_report`. It calls `axiom_graph_workflow_export` with `workflow_ids=["build_report"]`, which writes `workflow-export.html` in the project (or the `output_path` the agent passes) and replies with the file's path and the same count:

```
Wrote /path/to/project/workflow-export.html: 1 workflow · 2 files
```

If you change the code later, run `axiom-graph build .` before you export again. The steps come from the index and the code from the files on disk, so they line up only after a build.

## Step 4: Read the exported page

The page header reads `Workflow export  1 workflow · 2 files`. At the default detail level, the outline on the left reads:

```
build_report  workflow  5 steps
reporting/pipeline.py
Build the weekly sales report
IN CSV path   OUT totals as JSON

1       Load records  AUTO → load_records  reporting/sources.py  …
        Read the sales CSV and keep complete rows
   1.1    Read CSV
          Parse the file into one dict per row
   1.2    Drop incomplete rows
          Keep rows that have a region and an amount
2       Total by region
        Sum the amounts for each region
3       Write report
        Save the totals as JSON
```

Step 1 shows the purpose of `load_records` and lists its steps under it as 1.1 and 1.2. The file picker above the code pane holds the two files the outline points into:

```
reporting/pipeline.py
reporting/sources.py
```

You ticked only `build_report`; `sources.py` is included because step 1 calls into it. An export carries every file the selected workflows reach: the file each workflow is defined in, the files its steps are written in, and the file of each function an `AutoStep` calls.

The page loads nothing from outside the file, so it works offline. In it, the reader can:

- click a step name to show its marker in the code pane, or a function name or file path to show that definition, with the lines highlighted
- fold the steps under a step with its arrow, or every step with ⊟ and ⊞ beside **Steps**
- set **Detail** to **Compact** (step names only), **Purpose** (adds each step's purpose; the default) or **All** (adds inputs, outputs and critical notes); **…** on a step shows everything for that step alone
- type in **Filter steps...** to hide the steps that don't match
- pick a file, search all files with **Search code...**, and turn line wrapping on or off with **Wrap**
- switch between dark and light with ◑

When the export holds two or more workflows, a **Contents** list above the outline jumps between them.

Each file is included in full, not only the lines the steps point at. Check the file list for anything you don't want to share before you send the page.

## Where to go next

- [Annotations](../concepts/annotations.md): every marker, its arguments and the numbering rules
- [The viz dashboard](../viz.md): the rest of the **Workflows** tab
- [Use the CLI](../get-started/use-the-cli.md): every option of `workflows export`
- [Connect your agent](../get-started/connect-your-agent.md): set up the MCP server so an agent can read workflows with `axiom_graph_workflow_detail` and export them with `axiom_graph_workflow_export`
