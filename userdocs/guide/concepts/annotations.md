<!-- generated from axiom_graph::docs.consumer.concepts.annotations @ 5ea76e475d1c; do not edit -->

# The Semantic Layer: Annotated Highways

## Why not a full call graph?

A full call graph answers "what calls what." That is mechanically complete and almost useless to an agent: every helper, every logging shim, every getter is a node, and the path that actually *matters* (the orchestration spine of a command) is buried in thousands of edges with no labels on them. To understand a workflow you would still have to read all the code.

axiom-graph is deliberately **not** a full call graph. Instead it indexes a small, intentional set of *highways* — the orchestration paths a human marked as worth narrating — and annotates each one with step names, purposes, and the intent of every hop. The result: when an agent reads a workflow function, it gets a narrated path ("step 1 loads the index, step 2 hashes, step 10 delegates to the purge task") instead of a raw AST. That is the context-reduction payoff in its purest form — you load the spine, not the whole skeleton.

This semantic layer is the first of axiom-graph's three pillars. It lives alongside the AST index and DocJSON, and it is what turns "a graph of symbols" into "a graph that explains itself." Everything below is about the model behind it, not just the marker syntax — for the field-by-field syntax, see the axiom-annotations markers reference linked at the end.

## The markers: @workflow, @task, Step, AutoStep

You annotate highways with four markers from the `axiom-annotations` package. They are deliberately lightweight — decorators and call-site markers, no framework, no runtime behavior change.

```python
from axiom_annotations import workflow, task, Step, AutoStep

@workflow(purpose="Verify docs still match code", inputs="index", outputs="report")
def cmd_check(...):
    s = Step(step_num=1, name="Load index", purpose="Read the current graph.db")
    ...
    s = AutoStep(step_num=10, name="Purge stale entries")
    nodes_purged = _purge_stale_entries(...)
```

| Marker | Marks | Captures |
|---|---|---|
| `@workflow` | An orchestration function — the top of a highway | `purpose`, `inputs`, `outputs`, `critical` |
| `@task` | A leaf unit of work a workflow delegates to | same decorator contract as `@workflow` |
| `Step(...)` | An internal phase inside a decorated function | `step_num`, `name`, `purpose` |
| `AutoStep(...)` | A `Step` whose *next call* is the work it delegates | same fields as `Step`; records the delegation |

The split between `@workflow` and `@task` is the intent distinction: a workflow sequences, a task does. `Step` markers narrate the phases of either; `AutoStep` is the one that also records *where execution goes next*, which is how the graph captures cross-function delegation without a full call graph.

The markers are polyglot. The same shapes ship as a Python package (`pip install axiom-annotations`) and an npm package (`npm install axiom-annotations`), and the Python and JS/TS scanners read the identical marker shapes and emit identical graph nodes. You write the same intent surface in either language. For the complete field rules — the minor-step-inside-loops constraint, validation behavior, and the strict-annotations gate — see the axiom-annotations markers reference rather than reaching for the syntax here.

## The node model: envelopes and steps

Markers do not replace your function node — they sit beside it. When the scanner finds a decorated function it produces an **envelope** node as a *peer* of the function node, plus one **step** node per `Step` / `AutoStep` call site.

| Node | Kind | Produced from |
|---|---|---|
| Function | `atomic_process` / `function` | the function itself — the code truth |
| Workflow envelope | `composite_process` / `workflow` | a `@workflow`-decorated function |
| Task envelope | `composite_process` / `task` | a `@task`-decorated function |
| Step node | `atomic_process` / `step` | a `Step(...)` call site |
| AutoStep node | `atomic_process` / `autostep` | an `AutoStep(...)` call site |

The key idea: **the function is the code truth; the envelope is the annotation contract.** They are separate rows. The function's hash tracks the body; the envelope's hash tracks the decorator kwargs (`purpose`, `inputs`, `outputs`). That separation is what lets the intent contract drift independently of the implementation — if you change *what* a workflow promises without changing *how* it works, the envelope goes stale while the function does not, and vice versa. Drift is a read on the [mesh](staleness.md), and the envelope/function split gives that read two independent dimensions.

Step and AutoStep nodes are real, addressable nodes — they get stable IDs (`...::step-3`, `...::step-3.1`, preserving the authored number for display fidelity) — but they carry **no staleness of their own**. A step's content already changes when its enclosing function body changes, so giving each step its own staleness dimension would only produce noise and force per-step verification ceremony with no review value. Steps exist purely for addressability: they are the targets of delegation edges and the rows the step-by-step renderer walks. The right review gate is the function and the envelope, not every marker.

## The edges: annotates and delegates_to

Two intent-typed edges wire the semantic layer into the rest of the [mesh](the-mesh.md). Like every edge in axiom-graph, they are typed by *intent*, not by mechanism — which is what lets you traverse straight to the meaning instead of reconstructing it from a wall of call sites.

| Edge | From | To | Reads as |
|---|---|---|---|
| `annotates` | workflow / task envelope | the function it wraps | "this contract annotates that function" |
| `delegates_to` | an AutoStep node (or an envelope) | the called function or task | "at this step, execution hands off there" |

`annotates` is the bridge between the contract and the code: one per envelope, pointing from the envelope to the function that is the real implementation. `delegates_to` is the edge that makes the highway a *path* — it originates from the **AutoStep node**, not the envelope, because the AutoStep has a concrete position in the function body. The edge `autostep --delegates_to--> task` therefore says "*at this step position* in this workflow, execution delegates to that task," preserving the step ordering that an envelope-level edge would flatten away.

This is the whole reason the semantic layer beats a call graph for comprehension. Follow one workflow envelope's `annotates` edge to its code, then walk its AutoStep nodes' `delegates_to` edges, and you traverse exactly the spine of the operation — narrated, ordered, and intent-labeled — without grepping. These two edge types take their place in the broader [ontology](ontology.md) alongside `documents` (a doc section describing code) and `validates` (a test covering code); the semantic layer adds the *orchestration* dimension to that same shared mesh.

## Reading the highways back

Annotation only pays off if the narrated path is easy to pull back out. Three reads against the mesh surface it — two MCP tools your agent calls, plus a render:

- `axiom_graph_workflow_list` — every workflow, task, and state-machine envelope, with role, purpose summary, and file location. Filter to one role to scope it.
- `axiom_graph_workflow_detail` — for a `@workflow` / `@task`, the ordered steps and their delegation targets; for a state machine, the state list and transitions. It dispatches automatically on the envelope's subtype.
- `axiom_graph_render(level=3)` — renders a workflow-annotated function with its step markers inline, next to the code summary.

The same data appears graphically in the Workflows tab of the [viz dashboard](../viz.md), with delegation edges drawn beside each function's step breakdown. Either way, the consumer reads one narrated highway instead of loading the file and reconstructing the sequence by hand — the agent-native MCP path is the primary surface, the viz a secondary lens on the identical mesh.

## Beyond annotations: framework-aware extraction

Annotations are how *you* narrate a highway. But some frameworks already encode the highway structurally — their own syntax names the states and transitions. For those, asking an author to re-annotate by hand would be redundant. So the semantic layer extends a second way: **framework-aware extraction**, where a dedicated scanner reads a framework's native structure directly and emits the same envelope + edge model.

The proven example is **xstate v5**. The `xstate_scanner` reads `createMachine({...})` (and the `setup(...).createMachine(...)` form) and produces a `state_machine` envelope, one `state` node per state, `composes` edges from machine to states and parents to substates, and `delegates_to` edges that carry transition metadata — `{event}` for `on`, `{via: "always"}`, `{delay}` for `after`. `invoke` and `spawn` resolve through the same import-resolution machinery the JS/TS marker scanner uses. The result lands in the *same* graph as your hand-annotated Python workflows and surfaces through the *same* `workflow_list` / `workflow_detail` tools — a state machine is just another kind of narrated highway.

The important part is the discipline, not the toggle count. A framework earns its own scanner only when the trade is clearly worth it: its native structure is richer than annotations could express, it is load-bearing in the codebase long-term, and there are enough instances to amortize the scanner's maintenance cost. When a framework fails that bar, the cheaper extension is usually a new field on the `axiom-annotations` envelope — linear value, sub-linear maintenance, and it works across every language the annotations support. xstate v5 clears the bar; most things don't, and that's by design. The point of showing xstate here is not the framework itself but the evidence that the model extends: the same node-and-edge mesh absorbs hand-written intent and machine-extracted structure without forking.
