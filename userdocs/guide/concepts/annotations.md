<!-- generated from axiom_graph::docs/consumer/concepts/annotations @ 9a0b7435b94a; do not edit -->

# Workflow annotations

## What annotations are

Annotations are markers you add to the functions that carry out a process, such as a CLI command, a pipeline or a request handler. `@workflow` and `@task` say what a function is for. `Step` and `AutoStep` number the phases inside it. When axiom-graph builds the index, it reads the markers from your source and records each marked function as a workflow or task with ordered steps. You or your agent can then read a process as a numbered outline, with links to the functions its steps hand off to, instead of reading all of its code.

Only the functions you mark get steps and hand-off links, so the outline covers the processes you chose to describe, not every call.

The markers come from the `axiom-annotations` package, which exists for Python and for TypeScript and JavaScript. At run time they only check their own arguments: a `Step` with an empty `purpose`, for example, raises `StepValidationError`.

## Add markers in Python

Install the package in the project you are annotating, since your code imports it at run time:

```
pip install axiom-annotations
```

```python
from axiom_annotations import workflow, task, Step, AutoStep

@task(purpose="Load and validate the raw records", inputs="CSV path", outputs="records")
def load_records(path):
    ...

@workflow(purpose="Build the weekly sales report", outputs="report.html")
def build_report(path):
    s = AutoStep(step_num=1, name="Load records")
    records = load_records(path)

    s = Step(step_num=2, name="Total by region", purpose="Sum sales for each region")
    totals = {}
    for region in REGIONS:
        s = Step(step_num=2.1, name="Total one region", purpose="Sum one region's rows")
        totals[region] = sum_region(records, region)

    s = Step(step_num=3, name="Render", purpose="Write the HTML report")
    return render(totals)
```

| Marker | Where it goes | Arguments |
|---|---|---|
| `@workflow(...)` | A function that runs a process by calling other functions | `purpose` (required), `inputs`, `outputs`, `critical` |
| `@task(...)` | A unit of work that workflows call | Same as `@workflow` |
| `Step(...)` | Inside a `@workflow` or `@task` function, at the start of a phase | `step_num`, `name`, `purpose` (required); `inputs`, `outputs`, `critical` |
| `AutoStep(...)` | Inside a `@workflow` or `@task` function, on the line before a call to another `@task` or `@workflow` | `step_num` (required), `name` |

Every argument except `step_num` is free text; `critical` is a warning such as `"Takes 30+ minutes"`. An `AutoStep` has no `purpose` of its own: it is described by the `purpose`, `inputs` and `outputs` of the function it calls.

- Import the markers by name and pass their arguments by keyword. axiom-graph reads your source without running it and looks for the names `workflow`, `task`, `Step` and `AutoStep`, so it skips `@ax.workflow(...)` and ignores arguments passed by position.
- Methods can carry markers too.
- Put one call directly under each `AutoStep`: `f(...)` or `x = f(...)`, so a reader sees at a glance what the step runs. The function can be defined in the same file, imported from another module in the project, or a method called on `self` or `cls`. Otherwise the build reports a B4 finding.
  - To return the result, assign it first (`x = f(...)`), then return `x`.
  - Split a nested call such as `g(f(...))` so that `x = f(...)` stands on its own line.
  - When the call is inside a `try`, an `if` or a loop, put the marker inside the block, directly above the call. Guard clauses and other lines go above the marker.
  - Turn a conditional expression such as `x = f(...) if ready else None` into an `if` block with the marker inside it.
  - An awaited call (`x = await f(...)`) is not recognized.
- When the function a phase calls has no `@task` or `@workflow`, mark the phase with a plain `Step` and give it a `purpose`.

## Add markers in TypeScript or JavaScript

```
npm install axiom-annotations
```

JavaScript has no function decorators, so `workflow` and `task` wrap the function instead. Each marker takes one object, with camelCase keys (`stepNum`):

```typescript
import { workflow, task, Step, AutoStep } from 'axiom-annotations';

export const parseRecords = task({
  purpose: 'Parse and validate the raw records',
  outputs: 'records',
})((text: string) => JSON.parse(text).filter(isValid));

export const buildReport = workflow({
  purpose: 'Build the weekly sales report',
  outputs: 'report HTML',
})(async (path: string) => {
  const s1 = Step({ stepNum: 1, name: 'Read export', purpose: 'Load the raw file' });
  const text = await readFile(path, 'utf8');

  const s2 = AutoStep({ stepNum: 2, name: 'Parse records' });
  const records = parseRecords(text);

  const s3 = Step({ stepNum: 3, name: 'Render', purpose: 'Write the HTML report' });
  return render(records);
});
```

The arguments mean the same as in Python. The differences:

- Pass each marker an inline object literal. A variable (`Step(opts)`) works at run time, but axiom-graph can't read it: the workflow or step is left out of the index and the build reports a `JS-LIT-ENV` or `JS-LIT-STEP` finding.
- Wrap functions assigned to a top-level `const`, exported or not. Class methods can't be wrapped.
- As in Python, the statement after an `AutoStep` must call the function directly, not through `await`.

axiom-graph scans JS and TS files only when they match `js_paths` under `[axiom_graph.scan]`, and it needs the `js` extra: `pip install "axiom-graph[js]"`. See [Configuration](../get-started/configuration.md).

## Step numbering rules and findings

Number a function's major steps 1, 2, 3 with no gaps. Use minor numbers (2.1, 2.2) only for steps inside a loop. `build` and `check` test every marker against the rules below and print the findings:

```
Annotation findings: 1 (1 new, 0 resolved)
  ! [B2] app/report.py:12 build_report — major step sequence has gaps in 'build_report': missing [2], have [1, 3]
```

| Rule | Checks that |
|---|---|
| A1 | `step_num` is a positive number |
| A2 | each `Step` has a non-empty `name` and `purpose` |
| A3 | an `AutoStep` `name`, if given, is a non-empty string |
| B1 | no two steps in one function share a `step_num` |
| B2 | major step numbers run 1, 2, 3 with no gaps |
| B3 | minor step numbers (2.1) sit inside a loop |
| B4 | the statement after an `AutoStep` calls a `@task` or `@workflow` function in the project |
| C1 | every `@workflow` and `@task` has a non-empty `purpose` |
| JS-LIT-ENV, JS-LIT-STEP | JS/TS marker arguments are inline object literals |
| X1 | xstate machine configs are inline literals |

After the count line, `build` and `check` list the new findings; `check --all` lists every one. `axiom-graph check --strict-annotations` exits 1 while any finding remains, for use in CI. You can turn the whole check, or single rules A1 to C1, off under `[axiom_graph.validation]`; see [Configuration](../get-started/configuration.md).

## What axiom-graph builds

For each `@workflow` or `@task` function, the build adds a workflow (or task) node beside the function's own node, and one node per `Step` and `AutoStep`. Their ids extend the function's id:

| Node | Example id | Holds |
|---|---|---|
| The function | `myapp::app.report::build_report` | The code, as for any function |
| Its workflow or task | `myapp::app.report::build_report@workflow` (also for a `@task`) | `purpose`, `inputs`, `outputs`, `critical` |
| A step | `myapp::app.report::build_report::step-2.1` | The step number, `name` and `purpose` |

These edges connect them:

- `annotates` links the workflow or task node to its function.
- `composes` links the workflow or task node to each of its steps.
- `delegates_to` links an `AutoStep` to the function called on the next line. Following `delegates_to` from step to step walks a process across files.

For every node and edge type, see [the ontology](ontology.md).

## See the result

After a build, read workflows through the MCP tools:

- `axiom_graph_workflow_list` lists workflows, tasks and state machines with their purpose and file. Filter by `role` (`workflow`, `task` or `state_machine`), `module` (a path substring), `has_steps`, or `scope` (`production`, `tests` or `all`; the default, `production`, leaves out files under `test_paths`).
- `axiom_graph_workflow_detail` takes a function name or node id and lists its steps in order. An `AutoStep` shows the function it calls, and that function's own steps follow under it. `verbose=true` adds each step's purpose, inputs and outputs; for an `AutoStep` they come from the function it calls. `format="json"` returns the same data as JSON.

With the Python example above saved as `app/report.py` in a project whose id is `myapp`, `axiom_graph_workflow_detail` for `build_report` prints:

```
=== build_report (@workflow) ===
file: app/report.py#L7
axiom_graph_node: myapp::app.report::build_report

Steps (4):
  1. Load records → myapp::app.report::load_records
  2. Total by region
  2.1. Total one region
  3. Render
```

From the CLI, `axiom-graph render . --level steps --id <function id>` prints a Python function's steps with their purposes; `axiom_graph_render` with `level=3` does the same:

```
=== myapp::app.report::build_report ===
  [1] Load records
       purpose : 
  [2] Total by region
       purpose : Sum sales for each region
  [2.1] Total one region
       purpose : Sum one region's rows
  [3] Render
       purpose : Write the HTML report
```

The Workflows tab of the [dashboard](../viz.md) shows the steps and their hand-offs. To send a workflow and its code to someone, export it as one HTML page: from that tab, with `axiom-graph workflows export`, or with the `axiom_graph_workflow_export` MCP tool. See [Share a workflow and its code](../examples/share-a-workflow.md).

## How markers affect drift

Annotated functions take part in [drift detection](staleness.md):

- Editing a decorator's `purpose`, `inputs`, `outputs` or `critical` marks the workflow or task node `CONTENT_UPDATED`.
- When a function's code or docstring changes, its workflow or task node becomes `LINKED_STALE`, so you check that its `purpose` still holds. A workflow also becomes `LINKED_STALE` when code changes in a function that one of its `AutoStep`s calls, directly or further down the chain.
- Step nodes have no drift status. In Python, editing a `Step` or `AutoStep` marker counts as a change to the function's description, like a docstring edit (`DESC_UPDATED`), so docs and tests linked to the function are not flagged.
- In TypeScript and JavaScript, the wrapper options and the markers are part of the function's code, so editing them also marks the function `CONTENT_UPDATED`.

## xstate state machines

axiom-graph also reads xstate v5 state machines in the JS and TS files that `js_paths` covers, with no markers needed. It recognizes `createMachine({...})` and `setup({...}).createMachine({...})` and adds:

- a state machine node for each machine, and a node for each state, nested under its parent state;
- a `delegates_to` link for each transition, recorded with its event (`on`), `always`, or delay (`after`);
- a `delegates_to` link from a state that `invoke`s an actor to that actor, and to its `onDone` and `onError` states;
- a link from the machine to each actor it `spawn`s.

```typescript
export const doorMachine = createMachine({
  id: 'door',
  initial: 'closed',
  states: {
    closed: { on: { OPEN: 'open' } },
    open: { on: { CLOSE: 'closed' }, after: { 5000: 'alarm' } },
    alarm: { type: 'final' },
  },
});
```

This gives a `door` machine node, three state nodes, and three `delegates_to` links: `closed` to `open` on `OPEN`, `open` to `closed` on `CLOSE`, and `open` to `alarm` after 5000 ms.

Write the machine config as inline literals. A part that is a variable, a spread or a function call is skipped with an `X1` finding. State machines appear in `axiom_graph_workflow_list`, and `axiom_graph_workflow_detail` lists their states, each with its transitions, and marks final states.
