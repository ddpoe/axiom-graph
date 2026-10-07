<!-- generated from axiom_graph::docs/consumer/concepts/history @ 0dc80010a27f; do not edit -->

# Change history, diffs, and reports

## What axiom-graph records

axiom-graph keeps a change history for every node in the index: functions, modules, tests, docs, doc sections and the rest (see [the mesh](the-mesh.md)). Each entry has a date, an event type, the git commit at the time when one is known, and details such as a verification reason or the target of a link.

Entries are added as you work. `build` records new nodes and deleted files. Status changes are recorded whenever statuses are refreshed: by `build`, `check`, and the MCP tools that refresh what they show or write. Adding or removing a link, verifying a node or dropping a checkpoint each add an entry. Entries are never edited. Re-running `axiom-graph init` deletes the index, history included.

## Event types

Each entry has one of these event types. The status names are explained in [staleness](staleness.md).

| Event | Recorded when |
|---|---|
| `INITIAL` | A build indexes the node for the first time. |
| `BECAME_CONTENT_UPDATED`, `BECAME_DESC_UPDATED` | An edit makes the node drift from the version last verified. Further edits add no entry while it stays stale. |
| `BECAME_RENAMED`, `BECAME_NOT_FOUND` | The node was renamed or moved, or it disappeared. |
| `BECAME_LINKED_STALE`, `BECAME_BROKEN_LINK` | Something the node links to changed or no longer exists. The entry names that node. |
| `BECAME_VERIFIED`, `LINK_BECAME_VERIFIED` | The node's status, or its link status, returns to `VERIFIED`. |
| `AGENT_VERIFIED` | An agent verified the node with `axiom_graph_mark_clean` or `axiom_graph_reverify`, or saved a doc section through the doc tools. The entry carries the reason. |
| `MANUAL_VERIFIED` | A person verified the node with `axiom-graph mark-clean` or from the dashboard. |
| `LINK_ADDED`, `LINK_REMOVED` | A link from this node was added or removed. The entry names the target. |
| `CHECKPOINT` | Someone ran `axiom-graph history checkpoint`. The entry carries the commit. |
| `DELETED` | The node's file or doc was deleted, or the node was purged. The entry keeps its last title, type and location. |
| `RAW_DOCJSON_EDIT` | A doc section was edited outside the doc tools. `axiom_graph_accept_doc_edits` accepts the edit. |
| `RENAME_SCORING_SKIPPED` | A node went missing and rename detection could not check it, so its `NOT_FOUND` may be a rename. `axiom-graph rename apply` links the old and new ids. |

`axiom-graph carry-forward` (see [staleness](staleness.md#after-merging-a-worktree)) copies verifications made in a worktree. Each one is recorded as `AGENT_VERIFIED` or `MANUAL_VERIFIED`, matching who last verified the node in the worktree (a doc tool's save counts), and its reason names the branch and commit. When only part of a node carried, the entry's detail says whether it was the node's own text or the checks of some of its links:

```
2026-10-04  MANUAL_VERIFIED   manually verified — "[carry_forward:feat@ae1de6054c70] verified by human: encoding added"
```

## See a node's history

Agents call `axiom_graph_history` with a `node_id`, or `node_ids` for several nodes. Entries come newest first, 10 at a time; `max_results` raises that to at most 100 and `offset` pages back.

```
history for myproject::src.payments::charge
[4 of 4 entries]
────────────────────────────────────────────────────────────
2026-10-02  AGENT_VERIFIED    agent verified — "refund path checked against the new signature"
2026-10-01  BECAME_CONTENT_UPDATED  became CONTENT_UPDATED
2026-09-23  CHECKPOINT        git:ba9edf7fd551  (earlier history: git log --follow)
2026-09-20  INITIAL           first scan
```

In the dashboard, a node's detail panel has a **History** tab with the same entries (without checkpoints), its last verification and any renames. Each entry's SHA is a button that opens the node's source diffed against that commit. See [the dashboard guide](../viz.md).

From the terminal, `axiom-graph history agent-verified .` lists the nodes whose latest entry is an agent's verification, so a person can review them before you push:

```
AGENT-VERIFIED NODES (not yet human-reviewed)
--------------------------------------------------
myproject::src.payments::charge    verified 2026-10-02  "refund path checked against the new signature"

1 node(s) pending human review.
```

## Diff a node against an earlier commit

`axiom_graph_diff` (CLI: `axiom-graph diff`) returns a node's source, or a doc section's heading and text, at an earlier commit next to its current version. Run it on a stale node to see what changed since it was last checked.

```bash
axiom-graph diff myproject::src.payments::charge . --summary
axiom-graph diff myproject::src.payments::charge . --baseline HEAD~3
```

```json
{
  "node_id": "myproject::src.payments::charge",
  "baseline_sha": "ba9edf7fd551a3c09e4b2f0d1c7e8a6b5d4c3b2a",
  "baseline_date": "2026-09-23T14:02:11+00:00",
  "baseline_reason": "last commit recorded before the node went CONTENT_UPDATED at 2026-09-24T09:15:40+00:00",
  "path": "src/payments.py",
  "baseline_path": "src/payments.py",
  "summary": "+4 / -1 lines in body",
  "lines_added": 4,
  "lines_removed": 1
}
```

That is the `--summary` form (MCP: `summary_only=true`). Without it, the result carries `old_content` and `new_content` in place of the line counts.

By default, a stale node is compared with the last commit recorded before it went stale, so the diff shows the change that flagged it. If the node wasn't committed yet when it was indexed, that commit doesn't hold it, so the diff uses the newest commit that holds the node as it was before the change. Any other node is compared with the latest commit recorded at a verification or checkpoint, or, when there is none, the commit where it was first indexed. `baseline_reason` says which of these the diff used. `--baseline` (MCP: `baseline_sha`) takes any commit: a SHA, a tag or an expression such as `HEAD~3`.

The node is found by name in both versions, so code that moved around it does not show as a change, and a file renamed or moved since the baseline is read from its old path (`baseline_path`). A node added since the baseline has an empty `old_content`. When a diff can't be built, the result is an `error` and a `reason` instead, for example `no_baseline` when the node's history holds no commit, or no commit holds a stale node as it was before its change. Several node ids give one result each; the CLI separates them with `---` and exits 1 if any failed.

## Reports and reference points

A **reference point** is where a report starts. Any commit recorded in the history can be one. A **checkpoint** is a reference point you mark on purpose, for example at a release; the PEV plugin drops one after each cycle's audit.

```bash
axiom-graph history checkpoint --message "v2.1 release" .
```

`axiom_graph_list_reference_points` (CLI: `axiom-graph report . --list-refs`) lists checkpoints, then the other recorded commits, newest first in each group:

```
  ba9edf7fd551  checkpoint   2026-09-23  (7748 rows)  "v2.1 release"
  e7c429fa29d8  build        2026-10-02  (574 rows)
```

`axiom_graph_report` (CLI: `axiom-graph report`) summarizes what was recorded after a reference point: content changes, status changes, link changes and verifications. Every report opens with a `reference:` line that names what it was measured against:

```
reference: ba9edf7fd551 — checkpoint at 2026-09-23T14:02:11+00:00
12 nodes changed, 31 became stale, 40 verified (40 agent-only), 9 links modified
```

Choose the start with `since_sha` (CLI: `--since-sha`), a SHA prefix of at least 4 characters, or `since_timestamp` (CLI: `--since`), an ISO-8601 time. A commit the index never recorded but git knows resolves to its commit time. A SHA that neither knows, or that matches more than one commit, is an error, never a report over a different window:

```
ERROR: since_sha 'deadbeef' is not in node_history and is not a commit in this repository
```

With neither option, the report starts at the latest checkpoint, or at the latest recorded commit if there is no checkpoint.

The MCP tool returns only the reference and headline by default; `detail="condensed"` groups entries by module or doc, and `detail="full"` lists every entry. MCP output is capped at 40,000 characters (`max_chars`). The CLI's `--format` is `text` (every entry), `condensed` or `json`. Both narrow the report: `change_type_pattern` and `node_pattern` take globs and `node_type` a node type (CLI: `--change-type`, `--node`, `--node-type`), and `exclude_node_pattern` (CLI: `--exclude-node`, repeatable) leaves nodes out. [Use the CLI](../get-started/use-the-cli.md) lists every flag, and the [reporting pipeline](../examples/reporting-pipeline.md) walks through a change and its report.

The dashboard's **Changed Since** filter works from the same reference points: pick a checkpoint, a commit or the last 24 hours, and it shows the nodes that differ from that point, with deleted nodes as dimmed ghost rows. See [the dashboard guide](../viz.md).
