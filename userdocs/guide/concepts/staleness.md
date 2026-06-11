<!-- generated from axiom_graph::docs.consumer.concepts.staleness @ 2c4153b28327; do not edit -->

# Staleness: the engine that keeps the mesh trustworthy

## Drift is a read on the mesh

Code changes faster than the prose that describes it. A developer renames a parameter, refactors a function body, or deletes a module, and the docs that describe that code silently become wrong. Anyone reading those docs (human or agent) then makes decisions on information that no longer matches reality.

Staleness is how axiom-graph stops that. Recap, if you came from [the mesh](the-mesh.md#two-reads-one-mesh): drift detection is not a separate system bolted on beside the index. It is **a read against the same typed mesh** you use for context retrieval. When you ask the mesh which doc sections describe a function, you traverse `documents` edges. When that function changes, staleness propagates along *exactly those same edges* to flag every linked section, test, and workflow envelope. Intent-scoped retrieval and drift detection are two reads against one mesh; the mesh is the drift system.

That is why staleness is the engine, not a feature. Every time you run `axiom-graph check` (or `axiom-graph build`, which checks as it indexes), the engine compares the current state of your code against the last reviewed baseline and reports precisely which nodes need attention and why. When axiom-graph says a node is verified, it means the code it tracks has not changed since a human or trusted agent last reviewed it. When it says something is stale, it tells you what changed and through which edge.

Because the signal lives on the mesh, you read only what drifted. You do not re-scan the whole project or re-read whole files; you ask the engine for only the nodes that drifted and act on them. That is the same context-reduction discipline the rest of axiom-graph is built on, turned on the documentation itself.

## The two-column status model

Every node carries **two independent status columns**: `own_status` (has this node's own content changed?) and `link_status` (have the things it depends on changed?). They move independently. A doc section can be `CONTENT_UPDATED` because you edited its prose *and* `LINKED_STALE` because the function it documents also changed, at the same time.

### own_status (the content dimension)

| Status | Meaning |
|---|---|
| `VERIFIED` | Content matches the last reviewed baseline. Current. |
| `DESC_UPDATED` | Only the descriptor changed: a function's docstring, or a doc section's heading. |
| `CONTENT_UPDATED` | The primary content changed: a function body, or a doc section's prose. |
| `RENAMED` | The node's identity moved to a new location via rename detection (see [renames](staleness.md#renames-not_found-and-the-rename-lifecycle)). |
| `NOT_FOUND` | The file or node no longer exists on disk. Deleted or moved without a detected match. |

These form a severity ladder: `VERIFIED` < `DESC_UPDATED` < `CONTENT_UPDATED` < `RENAMED` < `NOT_FOUND`. When a container (a module or a document file) inherits status from its children, it shows the worst one, so problems surface at the container level without drilling into every leaf.

### link_status (the dependency dimension)

| Status | Meaning |
|---|---|
| `VERIFIED` | Everything this node links to is healthy. |
| `LINKED_STALE` | Something this node references changed. The doc may no longer describe current behavior. |
| `BROKEN_LINK` | This node links to a node ID that no longer exists in the index. |

The payoff of two columns is precision. `CONTENT_UPDATED` says "the author touched this"; `LINKED_STALE` says "reality moved under this." They demand different responses, so the engine never collapses them into one undifferentiated "stale" flag.

## How detection works

The engine checks cheap signals first and only does expensive work when something actually changed.

1. **File existence.** If a file is gone, every node from it is `NOT_FOUND` immediately.
2. **Timestamp fast path.** If the file's mtime matches the stored baseline, all its nodes stay `VERIFIED` with no parsing. Checks over an unchanged project are nearly instantaneous.
3. **Hash comparison.** For modified files, the engine re-parses and compares per-node content and descriptor hashes against the baseline. Content hash changed gives `CONTENT_UPDATED`; only the descriptor changed gives `DESC_UPDATED`; both changed reports `CONTENT_UPDATED` (the stronger signal); neither changed (the file was touched but the node's content is identical) stays `VERIFIED`.
4. **Link analysis.** For every doc section that links to code, the engine asks: (a) has the linked code drifted since baseline, and (b) is there a verification snapshot on the doc newer than that drift? If (a) is yes and (b) is no, the section is `LINKED_STALE`. The same two-pass logic runs for tests that validate code.
5. **Composite inheritance.** Modules and document files inherit the worst status from their children, surfacing trouble at the container level.
6. **Verification promotion.** If a node has a verification snapshot whose hashes still match the current content, it is promoted back to `VERIFIED`, even if the file was otherwise modified.

Each edge type propagates to a deliberately chosen depth, because a full call graph would flag every transitive caller and produce a backlog no one reviews. axiom-graph propagates by what each relationship *means*:

| Edge | Source to target | Propagation |
|---|---|---|
| `validates` | test to production code | 1 hop |
| `documents` | doc section to code, or doc to doc | Transitive, fixed-point, tag-gated |
| `annotates` | workflow envelope to function | 1 hop |
| `delegates_to` | AutoStep to task | Transitive, cycle-guarded |

A docstring edit flips the workflow that `annotates` the function, but not every caller. A function-body change flips all linked doc sections and tests directly, and cascades to downstream consumer docs through the transitive loop on `documents` edges.

## LINKED_STALE and transitive propagation

`LINKED_STALE` is the engine's most useful signal. It answers "which documentation needs review because the code it describes changed?"

When you link a doc section to a code node, you are telling the mesh "this prose describes that code." From then on, any change to that code flags the section `LINKED_STALE`. The output carries a **breadcrumb** so you know exactly where to look:

```
docs.architecture::caching-layer   VERIFIED   LINKED_STALE   via myproject::cache::invalidate
```

That reads: the caching-layer section is stale because `invalidate` changed.

### Transitive propagation

User-facing docs rarely link straight to code. A guide links to a developer spec section, which links to code:

```
Consumer doc section  -->  Dev spec section  -->  Code node
```

Without transitive propagation, a code change would flag the dev spec but the consumer doc would drift in silence. So axiom-graph propagates `LINKED_STALE` across `documents` edges as a cycle-guarded fixed-point: if the dev spec goes stale, every consumer section that links to it goes stale too, and the breadcrumbs trace the whole chain.

```
docs.consumer.guide::how-it-works   VERIFIED   LINKED_STALE   via docs.design::architecture
docs.design::architecture           VERIFIED   LINKED_STALE   via myproject::core::process
```

Follow the trail: the consumer doc is stale because the design spec is stale, which is stale because `process()` changed.

Transitive propagation is opt-in and tag-gated. You list the participating tags in `axiom-graph.toml` (see [configuration](../get-started/configuration.md)):

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
```

Only docs carrying a listed tag pick up transitive signals. Developer specs that link to other specs are unaffected unless their tag is included. A companion `frozen_tags` setting does the opposite, holding historical docs (ADRs, plans, completed PEV cycles) out of propagation so they stay as written.

## LINKED_STALE is sticky

The core invariant: **editing is not verification.** A doc section's prose can sit untouched while the linked code's contract shifts, and a single edit can be cosmetic, a typo fix, or entirely unrelated to the drift. Treating any edit as "resolved" would hide exactly the problem the engine exists to surface.

So `LINKED_STALE` is **sticky**. It does *not* clear when you:

- edit the doc's prose,
- run another `check`, or
- update (or even mark clean) the upstream code node.

The only thing that clears `LINKED_STALE` on a node is a **verification snapshot on that node itself**, recorded by one of:

- `axiom-graph mark-clean <node-id>` (or the `axiom_graph_mark_clean` MCP tool), or
- the MCP **auto-mark-clean-on-write** hook: saving a section through `axiom_graph_update_section` / `axiom_graph_write_doc` records a verification snapshot whose timestamp is newer than the linked code's last change, which lets the link-analysis pass filter it out.

A note on the surfaces: plain `axiom-graph mark-clean <node-id>` clears `LINKED_STALE` on that section just as the `axiom_graph_mark_clean` MCP tool does — both record the verification snapshot the next check reads. The one MCP-only convenience is auto-mark-clean-on-write: saving a section through `axiom_graph_update_section` / `axiom_graph_write_doc` records that snapshot for you, so an agent's edit clears the flag without a separate step.

Stickiness flows through transitive propagation for free: an edited consumer doc cannot clear its own transitive `LINKED_STALE` just by editing itself. The chain stays stale until the upstream is verified, or the consumer section is verified with a snapshot newer than every contributing change. That is what makes the signal trustworthy enough to gate a merge on. Every one of those transitions — and the verification event that finally clears it — is appended to the [history log](history.md), the table this stickiness rule is enforced against.

## Renames, NOT_FOUND, and the rename lifecycle

When a symbol moves, naive detection sees one node vanish (`NOT_FOUND`) and a new one appear, severing the history and links that made the old node trustworthy. axiom-graph tries to repair that instead.

During `build`, **rename detection** matches lost nodes against newly-discovered ones. It uses git as a scope reducer and body similarity to score candidates, plus an exact-hash pre-pass that catches cross-file moves a plain diff would miss. High-confidence pairs are **auto-applied**: the new node inherits the old node's history and edges and is marked `RENAMED` (sitting just below `NOT_FOUND` on the severity ladder), so its links survive the move. The build prints both buckets, for example `auto-applied N (revertable) ... M became NOT_FOUND`.

There is deliberately **no pending tier and no "list pending" tool** — renames surface through the `RENAMED` status, not a separate queue. For the cases automation gets wrong, two lifecycle operations give you manual control, each on both the MCP and CLI surfaces:

| Operation | What it does | MCP | CLI |
|---|---|---|---|
| Apply a rename | Weld a `NOT_FOUND` old node to a newly-created live node, migrating history and edges; new node becomes `RENAMED`. The escape hatch for a real rename that scored just under threshold. | `axiom_graph_apply_rename` | `axiom-graph rename apply <old> <new>` |
| Revert a rename | Symmetric migrate-back: restore the old identity as live, detach the new node, drop the rename rows. | `axiom_graph_revert_rename` | `axiom-graph rename revert <new>` |

Acknowledging a correct rename needs no new tool: a `RENAMED` node is cleared by `mark-clean`, the same as any other own-status drift. So a move that would otherwise have orphaned a doc instead arrives as a reviewable `RENAMED` flag with its provenance intact.

## Verification: human and agent

Clearing drift is a deliberate act of vouching for a node, not a side effect of touching a file. axiom-graph records that act in a verification table: each row stores the node ID, who verified it (`human` or `agent`), the content hash at the time, a timestamp, and an optional reason.

During a check, after hashing produces a status, a **promotion step** runs: if a node would be stale but a verification snapshot exists whose hash still matches the current content, it is promoted back to `VERIFIED`. There are two paths to record one:

- **Human:** `axiom-graph mark-clean <node-id> --reason "..."` records a human verification.
- **Agent:** the `axiom_graph_mark_clean` MCP tool records an agent verification. On the MCP write path, saving a doc section auto-records an agent verification at the new hash.

Agent verifications are **provisional, but visible**. The gate does not require human sign-off at the node level, which keeps an agent workflow moving, but `axiom-graph history agent-verified` lists every node an agent approved that a human has not yet reviewed. That list is the **pre-push gate**: a human scans what the agent vouched for before the work lands.

The batch-level companion is the impact report. After making changes, marking nodes clean with reasons, and running tests, an agent runs `axiom_graph_report` (the `report` MCP tool) to produce a deterministic, SQL-only summary: what changed, what is verified versus still unverified, by whom, and the test results. The report attaches to the PR; the developer reads it as part of review, and CI runs `axiom-graph check --fail-on` so documentation that drifted out of date cannot merge.

| `--fail-on` | Fails when |
|---|---|
| `none` | Never (default) |
| `stale` | Any `CONTENT_UPDATED`, `DESC_UPDATED`, `RENAMED`, `NOT_FOUND`, `LINKED_STALE`, or `BROKEN_LINK` |
| `unverified` | Any non-`VERIFIED` own_status |
| `any` | Any non-`VERIFIED` status in either column |

## Finding and resolving drift

`axiom-graph check` (and the `axiom_graph_check` MCP tool) returns a one-line headline plus the problem nodes:

```
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 1 NOT_FOUND · link: 3 LINKED_STALE / 0 BROKEN_LINK · 142 VERIFIED

NODE                                  OWN_STATUS       LINK_STATUS
-----------------------------------------------------------------------------
myproject::utils::parse_config        CONTENT_UPDATED  VERIFIED
docs.architecture::config-section     VERIFIED         LINKED_STALE  via myproject::utils::parse_config
docs.consumer.guide::configuration    VERIFIED         LINKED_STALE  via docs.architecture::config-section
```

To enumerate, filter, group, or paginate the drift inventory, use the `axiom_graph_drift_query` MCP tool. Group by feature for a triage view, filter to one status and emit bare IDs to feed straight into a batch `mark-clean`, or scope by a path glob. The MCP `check` stays narrow on purpose so an agent that only needs the headline does not pay for a full enumeration; the CLI mirror is `axiom-graph check --all`.

Resolving each status:

- **`CONTENT_UPDATED` / `DESC_UPDATED` / `RENAMED`:** review the code, fix the doc if it is wrong, then `mark-clean` the node with a reason. The next check sees matching hashes and promotes it to `VERIFIED`.
- **`LINKED_STALE`:** follow the `via` breadcrumb, read what changed, update the prose if needed, then verify *the section itself* (the upstream code being clean does not clear it; LINKED_STALE is sticky, as covered above).
- **`NOT_FOUND`:** the target is gone with no detected rename. Update the doc to the new location, remove the stale reference, or apply a manual rename if it really moved.
- **`BROKEN_LINK`:** an edge points at a node ID that no longer exists. Remove the dangling link, then re-link to the correct target.

For the full command reference, see [use the CLI](../get-started/use-the-cli.md). The everyday loop is: write code, `build`, `check`, review what flagged, fix what is wrong, `mark-clean` what is still right, repeat. Drift becomes visible the moment it happens, not months later when someone trips over it.

## Keeping published docs honest

The staleness engine is what lets the published site you are reading stay honest about its own subject — this site is built that way, dogfooding.

Consumer pages don't link to raw code; they link **through a dev-doc proxy** ([why, in detail](../examples/docs-honesty-loop.md#the-proxy-linking-architecture)). Because `consumer` is listed in `transitive_tags`, those pages **inherit `LINKED_STALE`** the moment code drift reaches the proxy they ride. That inherited flag is the signal — in precise breadcrumb form, "this published page now describes code that moved." A human or agent reviews the page against the changed proxy, updates the prose, and verifies it; per the sticky-`LINKED_STALE` rule above, that verification is the only thing that clears it. Then `render-site` republishes.

Detect, signal, review, verify, republish — the same engine that protects internal docs closes the loop on the public ones, so the published site cannot quietly drift from the code it documents. For the end-to-end walkthrough, see [the docs-honesty loop](../examples/docs-honesty-loop.md).
