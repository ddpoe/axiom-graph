<!-- generated from axiom_graph::docs.consumer.examples.reporting-pipeline @ 580da6d3e2ce; do not edit -->

# Tutorial: The Reporting Pipeline End to End

## What You'll Build

This is a hands-on tour of axiom-graph on one small, real codebase. By the end you'll have watched the three things that make the tool worth running work *together* on the same data: **intent-scoped retrieval** (reading exactly the node you need instead of a whole file or doc), **drift detection** (the graph noticing when code and its docs/tests fall out of sync), and **verification** (clearing that drift so the graph stays trustworthy).

The sample is a **reporting pipeline** — a tiny three-function module that loads a CSV, normalizes and filters it, and exports a JSON report. It's deliberately small so you can hold the whole mesh in your head while watching it react to change.

The pipeline lives in `reporting/pipeline.py`:

```python
def load_data(path: str) -> pd.DataFrame:
    """Read the raw CSV from disk."""
    return pd.read_csv(path)

def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns and filter rows where status == 'active'."""
    df = df.rename(columns=str.lower)
    return df[df['status'] == 'active']

def export_report(df: pd.DataFrame, dest: str) -> None:
    """Write the filtered DataFrame to JSON."""
    df.to_json(dest, orient='records')
```

Around that code sit the artifacts every real project accumulates: three unit tests (one per function), one end-to-end test decorated with `@workflow`, a developer design spec with a section per function, and a consumer user guide. The consumer guide is linked *through* the dev spec rather than straight at the code — the same proxy pattern this documentation site uses, which you'll see pay off in Scenario 4.

If you haven't installed and connected axiom-graph yet, start with [Use the CLI](../get-started/use-the-cli.md) and [Connect Your Agent](../get-started/connect-your-agent.md), then come back here.

## Step 1 — Build the Mesh

From the project root, run one command:

```
axiom-graph build .
```

The scanner walks the code and the docs and records what it finds as a typed graph — nodes joined by intent-typed edges. That graph is the product; everything else in this tutorial is a read or a write against it.

### Nodes

- **3 code nodes** — `reporting.pipeline.load_data`, `.transform`, `.export_report` (one per function, not one per file — you can read and reason about a single function in isolation)
- **4 test nodes** — `test_load_data_reads_csv`, `test_transform_filters_inactive`, `test_export_report_writes_json`, `test_reporting_pipeline_e2e`
- **1 workflow envelope node** — `test_reporting_pipeline_e2e@workflow`, the orchestration highway the `@workflow` decorator annotates
- **6 doc-section nodes** — 3 under the dev design spec (one per function) and 3 under the consumer guide (intro, guide, example). Each *section* is its own node, so docs are addressable at the same granularity as code.

### Edges

| From | Edge | To | How it's discovered |
|---|---|---|---|
| Each unit test | `validates` | Its production function | Auto — AST call-graph scan of the test body |
| E2E test | `validates` (x3) | All three production functions | Auto |
| Each dev-spec section | `documents` | Its production function | Author-declared in DocJSON `links` |
| Consumer-guide section | `documents` | Dev-spec section | Author-declared, tagged for transitive propagation |
| E2E workflow envelope | `annotates` | E2E test function | `@workflow` decorator |
| E2E workflow envelope | `delegates_to` (x3) | Each production function | `AutoStep()` markers inside the E2E test |

Notice the edges carry *intent*, not just connectivity: `validates` is different from `documents` is different from `annotates`. That typing is what lets drift detection (next steps) and intent-scoped retrieval (Step 2) be two reads against the *same* mesh rather than two separate tools. The semantic layer here — `@workflow` + `AutoStep` — is not a full call graph; it annotates only the orchestration highway with step names and intent, which is why the E2E test grows a workflow envelope but the plain unit tests don't.

Confirm everything is in sync:

```
axiom-graph check .
```

Every node reports `VERIFIED`. No drift, no stale flags. Green baseline established — now we change something.

## Step 2 — Pull Only the Context You Need

Before changing the code, get oriented the cheap way. The reflex on an unfamiliar codebase is to open files and scroll, or to grep and read the hits. The mesh lets you load far less.

**Read one function, not the file.** Ask the graph for the body of a single node by ID and you get just that function's source, by line range:

```
axiom_graph_source(node_id="proj::reporting.pipeline::transform")
```

**Read one doc section, not the whole doc.** The same applies to documentation — pull a single section by its slug instead of the entire guide:

```
axiom_graph_read_doc(doc_id="proj::docs.design.reporting-pipeline", section="transform")
```

**Traverse to exactly the linked nodes.** To learn what depends on `transform` before you touch it, follow the edges rather than guessing at call sites:

```
axiom_graph_graph(node_id="proj::reporting.pipeline::transform", direction="in")
```

That returns the three tests that `validates` it and the dev-spec section that `documents` it — the precise blast radius of an edit, with no false positives from a text search and nothing extra loaded into context. This is the core value for an agent: it arrives at the relevant function, its tests, and its docs having read a few hundred tokens instead of a few thousand. The CLI and viz offer the same traversal for humans, but the MCP server is the primary surface — agents query the mesh directly. See [Connect Your Agent](../get-started/connect-your-agent.md) for the wiring.

Now you know exactly what a change to `transform` will touch. Let's make one.

## Step 3 — Change Code and Watch Drift Propagate

**The change:** fix a bug in `transform()`. The old filter, `status == 'active'`, drops trial users who should be included. Change it to `status.isin(['active', 'trial'])`. Body only — the docstring stays as it is for now.

Rebuild and check:

```
axiom-graph build .
axiom-graph check .
```

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 5 LINKED_STALE / 0 BROKEN_LINK · 11 VERIFIED
transform                                 CONTENT_UPDATED
test_transform_filters_inactive           LINKED_STALE  via reporting.pipeline.transform
test_reporting_pipeline_e2e               LINKED_STALE  via reporting.pipeline.transform
docs.design.reporting-pipeline::transform LINKED_STALE  via reporting.pipeline.transform
docs.consumer.generating-reports::example LINKED_STALE  via docs.design.reporting-pipeline::transform
test_reporting_pipeline_e2e@workflow      LINKED_STALE  via reporting.pipeline.transform
```

One node changed its own content (`CONTENT_UPDATED`), and that signal travelled outward along the edges you traced in Step 2 — the same mesh, read for drift instead of retrieval, which is why it can name *why* each node flipped:

- the unit test and the E2E test, each 1 hop via `validates`
- the dev-spec section, 1 hop via `documents`
- the consumer-doc example, **2 hops** via transitive `documents` (consumer doc → dev spec → code) — the consumer guide rides the proxy chain even though it never links to code directly
- the workflow envelope, via `annotates` + `delegates_to`

The `via` breadcrumb is the payoff: a call-graph tool flags only the tests; a markdown doc-sync tool misses the transitive consumer doc; neither tells you the *reason*. The mesh catches all five and points at the offender. That `LINKED_STALE` state is sticky on purpose — editing the linked file won't silently clear it. Only an explicit verification does, which is Step 5. (More on the model in [staleness](../concepts/staleness.md).)

## Step 4 — Edit a Docstring and Watch Drift Stay Bounded

Not every change should set off the same alarm. Reset to the Step 3 state, then this time change *only* the docstring on `transform()` — from `"Normalize columns and filter rows."` to `"Rename columns to lowercase and filter rows where status is 'active' or 'trial'."` Leave the body alone. Rebuild and check:

```
own: 0 CONTENT_UPDATED / 1 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 1 LINKED_STALE / 0 BROKEN_LINK · 15 VERIFIED
transform                                 DESC_UPDATED
test_reporting_pipeline_e2e@workflow      LINKED_STALE  via reporting.pipeline.transform
```

The difference is the whole point. A docstring edit produces `DESC_UPDATED`, not `CONTENT_UPDATED`, and it does **not** storm-cloud through the call graph. The unit test's `validates` edge stays green; so does the dev-spec `documents` edge and the consumer-doc transitive link. The behavior didn't change, so the things that assert on behavior don't need a second look.

The one node that *does* flip is the workflow envelope — because a `@workflow(purpose=...)` decorator can reference the function's description in its own metadata, so a description change is a legitimate reason to re-read it. That's a catch a plain text search can't make: it requires knowing the *type* of the relationship.

The practical effect: your cleanup is two acknowledgements, not fifteen. axiom-graph distinguishes "the code does something different" from "we described it better," and only the first kind cascades. This precision is what keeps drift detection a trustworthy signal instead of noise people learn to ignore.

## Step 5 — Verify to Clear the Drift

Take the behavioral change from Step 3 — the five `LINKED_STALE` nodes — and resolve them. Verification is how drift gets cleared: you bring each flagged artifact back into agreement with the code, then record that you did. Working down the `via` list:

1. **Unit test.** Update `test_transform_filters_inactive` to expect both active and trial rows. Run it; it passes. Mark it verified:
   ```
   axiom-graph mark-clean proj::tests::test_transform_filters_inactive .
   ```
2. **Dev-spec section.** The prose still says "filters rows where status == 'active'" — now wrong. Patch it via `axiom_graph_update_section` to "filters rows where status is 'active' or 'trial'." Editing a DocJSON section through the write tools auto-records verification at the new hash, so this section clears without a separate `mark-clean`.
3. **Consumer-guide example.** Its expected output showed only active rows. Refresh the example.
4. **E2E test + workflow envelope.** Run the E2E test, confirm it passes, and `mark-clean` the test and its envelope.

Now `axiom-graph check .` is green again. The distinction between `own_status` (a node's own content) and `link_status` (its links) matters here: a `CONTENT_UPDATED` or `DESC_UPDATED` node — your intentional code change — is promotable with `mark-clean`, because *you* are vouching that the new content is correct. A `LINKED_STALE` node clears only when you verify *that section itself* with `mark-clean` — not by marking the upstream code clean — so update the prose or test it points at, then verify it. The mesh won't let you wave away drift by asserting a stale doc is fine — you fix the doc.

This is the loop that keeps the graph honest — and the loop the consumer docs you're reading run through too, via the same [dev-doc proxy](docs-honesty-loop.md#the-proxy-linking-architecture). See the [docs honesty loop](docs-honesty-loop.md) for that story end to end.

## Step 6 — Let an Agent Draft the Update

Steps 3 and 5 were the manual loop. With an agent connected over MCP, the same work compresses into a reviewable pull request. Make the Step 3 bug fix inside Claude Code (or Cursor, or any MCP client), then say:

> "Draft updates to any docs and tests that went stale."

The agent works the mesh the same way you did, just faster:

1. Reads `axiom_graph_check` and sees the five `LINKED_STALE` nodes
2. For each, calls `axiom_graph_source` for the current code, `axiom_graph_read_doc` for the linked dev-spec section, and `axiom_graph_diff` to see exactly what changed in `transform` — pulling only the context it needs, not loading the repo
3. Drafts new section text via `axiom_graph_update_section`
4. Edits the unit-test assertions directly
5. Runs the suite and confirms green
6. Opens a PR: the code change, the updated test, the patched dev-spec section, the refreshed consumer example, and a comment listing which nodes are ready to verify after review

What lands in front of the human is one ordinary PR with diffs. You review the doc edits the same way you review code — at PR time, in context — and merge if they're right or ask the agent to redo a section if they're not. On merge, CI runs `axiom-graph check` and confirms zero `LINKED_STALE` remain.

This is the flywheel: the agent drafts, the human reviews, and mesh density *grows* with every PR instead of decaying between doc sprints. The graph is the substrate that makes it possible — an agent without it makes the code change and never knows the linked docs exist. For the deeper version of agents consuming the mesh to do real work, see [the PEV nexus](../pev/overview.md).

## Step 7 — Rename a Function Without Breaking the Mesh

One change reliably breaks naive link tracking: moving a symbol. Rename `transform()` to `normalize()` to match newer conventions — body unchanged, just the name. Rebuild:

```
axiom-graph build .
axiom-graph check .
```

```
own: 0 CONTENT_UPDATED / 0 DESC_UPDATED / 1 RENAMED / 0 NOT_FOUND · link: 0 LINKED_STALE / 0 BROKEN_LINK · 15 VERIFIED
```

Zero broken links. The build's rename matcher noticed that `reporting.pipeline.transform` disappeared and `reporting.pipeline.normalize` appeared with a near-identical body, and re-homed every edge — the three `validates` edges, the `documents` edge, and the `annotates` + `delegates_to` from the workflow envelope — onto the new identity. A pure rename with an unchanged body is caught by an exact-hash pre-pass before any scoring runs; otherwise git narrows the candidate pool and a body-similarity score (default threshold 0.6) makes the call. The new node reports `RENAMED` rather than a fresh creation, and the dev-spec section still reads "transform" in its prose but already points at `normalize`.

This is exactly why consumer docs link through a [dev-doc proxy](docs-honesty-loop.md#the-proxy-linking-architecture) instead of at raw code: the link target is stable under renames, so the page you're reading doesn't break when a symbol moves underneath it.

Acknowledge the rename and you're green:

```
axiom-graph mark-clean proj::reporting.pipeline::normalize .
```

### When detection needs a hand

If a rename lands together with a big body rewrite, similarity can fall below threshold: the old node goes `NOT_FOUND`, the new one looks newly created, and a `RENAME_SCORING_SKIPPED` history event flags it as a rename candidate. Weld it manually:

```
axiom-graph rename apply proj::reporting.pipeline::transform proj::reporting.pipeline::normalize .
```

The escape hatch enforces a safety contract — `old_id` must be `NOT_FOUND` and `new_id` a fresh live node — and is symmetric: `axiom-graph rename revert proj::reporting.pipeline::normalize .` un-welds it cleanly. Both have MCP equivalents (`axiom_graph_apply_rename` / `axiom_graph_revert_rename`) so an agent can do the same.

Rename-awareness is what keeps drift detection from drowning in false positives every time someone tidies a name — the signal stays about real divergence.

## What You Just Saw

Seven steps on one tiny module, and the three capabilities never operated in isolation:

- **Intent-scoped retrieval** (Step 2) read one function, one doc section, and the exact dependents of a node — the same traversal an agent uses to keep its working set small.
- **Drift detection** rode those identical edges: a body change cascaded full-mesh through `validates`, `documents`, transitive `documents`, `annotates`, and `delegates_to` (Step 3); a docstring edit stayed bounded to the one node that referenced it (Step 4); a rename re-homed every edge with nothing broken (Step 7).
- **Verification** (Step 5) cleared the drift — and the model's refusal to let you `mark-clean` a `LINKED_STALE` node is what forces docs and tests to actually catch up, keeping the graph trustworthy over time.

The through-line is that there is **one mesh**. The graph you query for context is the graph that detects drift is the graph you verify against. That's why the alternatives each miss something a piece at a time — a call graph plus `git blame` ignores the docs/tests half entirely; a doc-sync tool flags the dev spec but not the transitive consumer doc or the workflow layer; a hosted docs host happily publishes the stale version. And it's why an agent (Step 6) can draft real doc and test updates: the mesh is the substrate that tells it what went stale and why.

### Where to go next

- [The mesh](../concepts/the-mesh.md) — nodes and intent-typed edges, the model underneath everything here
- [Staleness](../concepts/staleness.md) — the full drift vocabulary and the sticky `LINKED_STALE` rule
- [Use the CLI](../get-started/use-the-cli.md) and [Connect Your Agent](../get-started/connect-your-agent.md) — run these steps on your own project
- [The docs honesty loop](docs-honesty-loop.md) — how this very site is built the way this tutorial describes
