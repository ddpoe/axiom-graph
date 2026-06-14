<!-- generated from axiom_graph::docs.consumer.concepts.history @ c4bdd98e97cb; do not edit -->

# History: the append-only spine under drift, diffs, and time travel

## Another read on the same mesh

axiom-graph tracks three kinds of state for every node: its current content (hashes), its current [staleness](staleness.md) (the `own_status` / `link_status` snapshot), and its verification status (who vouched for it, and when). The first two are **overwritten on every build** — a check recomputes them from disk and the previous values are gone. That is fine for answering "what needs attention right now," but it cannot answer "how did we get here."

The **history log** closes that gap. It is a single append-only table, `node_history`, with one row per meaningful change to a node. Nothing ever overwrites a row; events are only ever appended. That one table is the spine under every time-aware feature in axiom-graph — drift transitions, the "changed since" filter, diffs, time-travel reference points, the verification gate, and ghost nodes for deleted code. **Each of those is just a read or a write against `node_history`**, not a separate subsystem.

It is the same idea as [staleness](staleness.md): history is another *read on the mesh*. Rows are keyed by `node_id` — the very same [mesh](the-mesh.md) nodes you traverse for context — so "what changed in this node, and when" obeys the same context-reduction discipline: pull one node's log, or only the events since a reference point, never rescanning the whole project.

| Table | Role | Write pattern |
|---|---|---|
| `nodes.own_status` / `link_status` | Computed snapshot — what needs attention now | Overwritten every build / check |
| `node_verification` | Live verification state — is the current sign-off still valid | Upserted on verify |
| `node_history` | Audit trail — what happened over time | **Append-only**, never recomputed |

History and verification are written together in the same transaction, so the record of *what happened* and the record of *what is currently vouched for* can never drift apart.

## What a history row records

Every row is small and flat. The columns answer who, what, when, and against which commit:

| Column | What it holds |
|---|---|
| `node_id` | The mesh node this event is about. |
| `change_type` | Which event fired — one of roughly a dozen types, grouped into the five families below. |
| `scanned_at` | ISO-8601 timestamp of when the event was recorded. |
| `git_sha` | The repository HEAD at the time, when known. This is what makes a row usable as a time-travel reference point. |
| `meta` | A JSON blob with the event's detail: the `from` / `to` statuses of a transition, a verification reason, the source and target of a link event, or the snapshot of a deleted node. |
| `preserved` | `1` marks the row exempt from pruning; `0` rows are subject to the per-node cap (see *What survives and the verification table*, below). |

Recording is automatic. Whenever the scanner's `upsert_node` step detects that a node's content or descriptor hash changed, it inserts a row in the same write — there is no separate "start tracking" step. The primary key is an autoincrementing `id`, and rows are never edited after the fact: the log is append-only by construction.

## The five change-type families

Every `change_type` belongs to one of five families. The family tells you who wrote the row and what kind of question it answers.

| Family | Change types | Written by | Fires when |
|---|---|---|---|
| **Content** | `INITIAL`, `CONTENT_ONLY`, `DESC_ONLY`, `CONTENT_AND_DESC` | the scanner, during `build` | a node's content or descriptor hash differs from the stored baseline. The names are content-centric on purpose — `CONTENT_ONLY` means a function body *or* a doc section's prose changed. |
| **State** | `BECAME_CONTENT_UPDATED`, `BECAME_DESC_UPDATED`, `BECAME_LINKED_STALE`, `BECAME_NOT_FOUND`, `BECAME_VERIFIED` (plus link-dimension counterparts) | `record_staleness` | a node's `own_status` or `link_status` transitions. These capture *how the staleness engine read the change*, emitted per dimension. |
| **Structural** | `LINK_ADDED`, `LINK_REMOVED` | edge-modifying code (add-link, `write_doc` link arrays, rebuild) | an edge is created or removed. The row is recorded on the source node; `meta` carries both endpoints and the actor. |
| **Lifecycle** | `DELETED` (`preserved=1`) | the purge pass | a node's source file leaves disk. The row snapshots the node's last-known title, type, location, and tags before the node is cascade-deleted. |
| **Actor** | `AGENT_VERIFIED`, `MANUAL_VERIFIED`, `CHECKPOINT` (`preserved=1`) | a human or an agent | someone makes a judgment call — vouching for a node, or dropping a named marker. |

Two distinctions are worth keeping straight. `BECAME_VERIFIED` (a State event) means the hashes *re-aligned on their own* — a revert, or code and prose organically matching again — with nobody reviewing anything; `AGENT_VERIFIED` / `MANUAL_VERIFIED` (Actor events) mean someone actually looked. And Content events answer "what physically changed," while State events answer "how did that change move the node's status" — a single edit usually writes one of each.

## What survives and the verification table

History is append-only, and there is no user command that deletes it. The one pruning mechanism is a silent safety valve: a **per-node cap of 100 rows** (`_HISTORY_ROW_LIMIT`). When a high-churn node accumulates more than 100 rows, the oldest non-preserved ones are dropped. There is deliberately no `--keep` / `--older-than` pruning command — history rows are tiny, and deleting them would destroy the ability to answer "how long was this stale six months ago."

The `preserved` flag is what survives that cap. **Actor and Lifecycle events set `preserved=1`** and are never pruned, which keeps three things permanently legible:

- the **verification trail** — so `history agent-verified` can always list what an agent vouched for but a human has not yet reviewed;
- **checkpoint markers** — so a named reference point is never silently lost;
- **`DELETED` tombstones** — so a node that no longer exists can still be surfaced as a ghost (see *What the history log powers*, below).

### The verification table

`node_history` answers "what happened." Its companion table, `node_verification`, answers "is the current sign-off still valid." It holds one row per node: `status`, `verified_at`, `verified_by` (`human`, `agent`, or `agent:model-name`), an optional `reason`, and two hash columns — `code_hash_at` and `desc_hash_at`. Those hashes are the expiry mechanism: a verification only counts while the stored hashes still match the node's current content. The moment the code or prose changes, the snapshot no longer matches and the node falls back to stale. The two tables are written in the same transaction, so they cannot diverge.

## What the history log powers

Each family of rows is the substrate for a feature you already use. Read the log one way and you get drift history; read it another way and you get time travel.

**Drift transitions and sticky LINKED_STALE.** State events are the audit trail behind [staleness](staleness.md). Because every transition is recorded, the log can answer "when did this become stale, and how long has it been that way" — not just "is it stale now." It is also why `LINKED_STALE` is *sticky*: a State event records that a node went stale, and only an **Actor event on that same node** (a verification snapshot) clears it. Editing the prose, or even verifying the upstream code, does not — the clearing event has to land on the node itself.

**The verification gate.** `history agent-verified` reads the preserved Actor events to build the pre-push sign-off queue: every node an agent marked verified with no later human verification. A person scans that list before the work lands. It is the human-in-the-loop checkpoint of the [docs-honesty loop](../examples/docs-honesty-loop.md).

**Time travel and the "changed since" filter.** Any row carrying a `git_sha` is a usable reference point; `CHECKPOINT` rows are explicit, named ones ("this release matters"). Resolution checks checkpoints first, then falls back to any row matching a SHA prefix, so the filter works even right after `init`. Give it a reference point and you get back every node that changed after it — which powers both the impact report and the viz [Changed Since](../viz.md) filter. For the viz filter, "changed" is a true **net state-diff** of the current index against the baseline: a node edited and then reverted within the window *cancels* and never appears, and each changed node is labelled by kind (added, content, descriptor, content+descriptor, renamed, deleted). The history log still anchors the reference point — but membership is the net state, not a replay of every event row. See the [reporting pipeline](../examples/reporting-pipeline.md) for the end-to-end flow.

**Diffs.** History also supplies the *baseline* for a diff. To show what changed in a node, the diff subsystem reads `git show {sha}:{file}` where `{sha}` comes from the node's own history — its most recent verified or checkpoint row, falling back to the oldest `INITIAL`. No source snapshots are stored in the database; the log just remembers which commit to compare against.

**Ghost nodes.** A `DELETED` tombstone keeps a removed node visible. When the "changed since" filter spans a deletion, the viz synthesizes a dimmed, struck-through [ghost row](../viz.md) from the preserved snapshot, so a node disappearing is itself a visible event rather than a silent gap. The tombstone also preserves the node's source span and the commit it was deleted at, so the ghost's baseline source can be recovered straight from git for inspection.

## How you reach it

The same log is exposed on all three surfaces.

**CLI.** Two `history` subcommands ([use the CLI](../get-started/use-the-cli.md)):

```bash
axiom-graph history checkpoint .                      # drop a named reference point
axiom-graph history checkpoint --message 'v2.1 baseline' .
axiom-graph history agent-verified .                 # the human sign-off queue
```

`checkpoint` only marks — it never prunes. `agent-verified` lists what is still awaiting human review.

**MCP.** Agents reach the log through three tools ([connect your agent](../get-started/connect-your-agent.md)):

- `axiom_graph_history` — a node's change log, annotated with how long any stale window has been open;
- `axiom_graph_report` — the impact report: everything that changed since a checkpoint, SHA, or date, ordered by urgency;
- `axiom_graph_diff` — the structured per-node diff against a baseline commit.

**Viz.** On a node's detail drawer the **History** tab shows the change log with clickable SHAs — click one to diff that commit against the current source. The sidebar **Changed Since** filter and its ghost nodes are the same since-query, rendered. See [the visualization guide](../viz.md).
