<!-- generated from axiom_graph::docs/consumer/plugins/pev/user-guide @ 213b3f1d08ff; do not edit -->

# PEV user guide

How to run PEV: choosing between `/pev-cycle` and `/pev-instance`, what you approve at each gate, customizing PEV with `.pev/` files, and what a cycle leaves behind. Install and check the plugin first with [setup.md](setup.md).

## Two cycle shapes

### `/pev-cycle`: full workflow

For new features, public API changes, design decisions, and changes that span several systems, such as a config option every layer must honor or a field threaded from storage through the API to the UI.

```
/pev-cycle add configurable doc-scan dirs + a db-path key, plumbed through the builder, CLI, MCP, renderers, diff, and viz
```

Commit or stash changes to tracked files first; `/pev-cycle` won't start without a clean working tree. The cycle then runs these phases:

1. Intake. You confirm a cycle id such as `pev-2026-03-21-add-history-filtering`. The orchestrator creates a git worktree at `.claude/worktrees/<cycle-id>`, installs the project's dependencies in it, and creates the cycle's docs.
2. Plan. The Architect writes the pitch.
3. Build. The Builder implements it in the worktree, test-first.
4. Review. The Reviewer runs the tests and reviews the diff.
5. Sync. If main has moved since the worktree was created, it is merged into the branch, so the Auditor works on the tree that will land. You are asked only when that merge conflicts.
6. Audit, in the worktree. The Auditor updates the docs the change made stale. Editing a section does not clear its stale flag by itself. A section stays flagged until it has been checked against every change that flagged it: the Auditor passes `addresses=[...]` to `axiom_graph_update_section` or `axiom_graph_patch_section`, naming the changes the edit accounts for, or calls `axiom_graph_mark_clean` once it has checked the section. Cycles audit in parallel, because each audits its own worktree.
7. Doc review, also in the worktree. The Doc Reviewer checks the Auditor's work, and approved link changes are applied. The audit is committed on the branch.
8. Merge. You approve the whole finished cycle at once: code, tests, review, audit and doc review. The merge step then runs the full test suite, merges the branch into main with `--no-ff`, and copies the verifications made in the worktree onto main with `axiom_graph_carry_forward`, so the docs are not audited a second time. A node main shows as stale after the merge that it did not show before is residue, and you decide it one node at a time: mark it clean, reverify it, or leave it for a follow-up. The merge step then writes the completion commit with an efficiency report, removes the worktree and branch, and you choose whether to push.

When an agent uses up its tool-call budget, it saves its progress to the manifest and the orchestrator starts a fresh one to continue.

If a cycle is interrupted, the next `/pev-cycle` finds its manifest (tagged `pev-cycle` and `in-progress`) and offers to resume it, release it as incomplete, or start a new cycle. A resumed cycle continues at the phase its manifest `status` records.

### `/pev-instance`: single-agent mode

For a small change, one subsystem and a few files, that still affects docs. A one-line typo fix doesn't need PEV.

```
/pev-instance auto-register a worktree as a viz project on checkout so it shows up in the project picker
```

One agent does the whole job, with no separate Architect, Builder or Auditor, in a git worktree of its own for each task (`.claude/worktrees/<instance-id>`), so your working tree is never touched. Uncommitted work on main does not block it, because the worktree is created from a commit. It checks the task is small, writes a mini-pitch (problem, user story, acceptance criteria, plan) for your approval, and records which nodes were already stale when it started, so it answers only for drift its own change causes. It makes and commits the change in the worktree, updates the docs the change made stale and clears their flags the way the Auditor does, and writes a checkin doc. It then has an independent Reviewer and Doc Reviewer check the branch, in parallel, and fixes what they find. Last, it lands the task through the same merge step as `/pev-cycle`: the test suite, a `--no-ff` merge into main, `axiom_graph_carry_forward`, and a per-node decision on any residue. When a request holds several tasks they run one after another, each merging before the next worktree is created. The checkin lives in the instance's own folder, `docs/pev/instances/<instance-id>/checkin`, and the instance's efficiency report is written beside it. If you interrupt an instance, run `/pev-instance` again with the same task and it picks up from its checkin. The unmerged work sits in the worktree, so from the review step on, resume from a Claude Code session in that worktree.

It suggests `/pev-cycle` instead when a task looks like four or more files, a public API change, a new design decision, or a change to a function with step markers. If the task grows after it has started, it stops, records its progress in the checkin, and tells you to continue with `/pev-cycle`. If you're not sure which command to use, start with `/pev-instance` and let it escalate.

## Approval gates

At each gate the orchestrator shows you what the last phase produced and waits for your answer. Every gate has the same shape: its name, the content it rests on, the decisions to make with a recommendation for each, and one yes/no question.

| Gate | You see | Your options |
|---|---|---|
| Cycle name | The proposed cycle id and your request | Proceed, or suggest another name |
| Plan | The Architect's pitch: scope, user stories, solution sketch, constraints, test plan | Approve, or give feedback for the Architect to revise |
| Review | The Reviewer's verdict, any concerns, and a table mapping each user story to its tests | Approve, or send the Builder back to fix concerns or tests |
| Doc review | The Doc Reviewer's concerns, shown only when it has some | Accept, or send the Auditor back |
| Link changes | Each doc-to-code link the Auditor or Doc Reviewer proposes to add, repoint or drop, with the section's current links | Apply all, some or none |
| Merge | The finished cycle as one unit: files changed, tests, the review verdict, deviations from the plan, any drift the change doesn't explain, the audit and the doc review's verdict | Approve the merge, or give the Builder or Auditor feedback |
| Residue | Shown only when main shows stale nodes after the merge that it didn't show before and the carry-forward could not settle: each node with its status and why | Per node: mark clean, reverify, or leave for a follow-up |
| Finish | How many commits local main is ahead of its upstream | Keep local, push, or open a PR |

Other points where the cycle asks you:

- During planning the Architect may ask questions, or propose edits to existing docs that you approve or reject one by one.
- If the Reviewer finds that the pitch contradicts a source document, you choose whether to re-plan or merge anyway.
- If main has moved and merging it into the branch conflicts, you approve how to resolve the conflicted files before the cycle continues. The same applies at the merge step.
- If the full test suite fails in the merge step, or main has uncommitted changes to tracked files, the merge stops and asks you.
- If the Auditor finishes with items it could not fix, you choose to handle them in a follow-up cycle, fix them yourself, or accept them.

A failed review sends the Builder back automatically, and a failed doc review sends the Auditor back, up to twice each before the orchestrator asks you. Nothing is merged into main until you approve the merge gate.

When the plan has several independent groups of tasks, the Architect can mark them for a sharded review: one Reviewer per group, in parallel, each writing its own verdict. The orchestrator combines them into one verdict, in which the worst shard status wins, and you see that one at the review gate.

`/pev-instance` asks you to approve the mini-pitch (approve, revise, or escalate to `/pev-cycle`), and asks before applying any link changes. After its independent review it fixes the findings itself and re-runs a reviewer only after a substantial fix, at most twice per reviewer before asking you. It then goes through the same merge step as `/pev-cycle`, with the same test, conflict and residue gates.

## Customizing with .pev/ files

PEV reads four optional DocJSON files from `.pev/` in your project root. DocJSON is JSON with a `sections` array, and each section's `content` is markdown, so you can read and edit the content as text.

| File | What it controls | If it is absent |
|---|---|---|
| `doc-topology.docjson` | Your doc categories (PRD, interface spec, ADR, README, ...), each with a path glob, the changes that trigger it, what the Auditor does and what the Doc Reviewer checks | The plugin's generic categories |
| `test-policy.docjson` | Test tiers (by default: plain tests, `@workflow(purpose=...)`, and `@workflow` with `Step()` markers), how to pick a tier, coverage and test budget. The Architect plans tests with it, the Builder follows it and the Reviewer checks against it. | The plugin's default policy |
| `review-criteria.docjson` | Project-specific review checks, such as logging conventions, error handling or anti-patterns, each with a severity | Generic review only |
| `architecture-policy.docjson` | The layers of your code: where new behavior belongs, what each layer may import, and how tests should call into the code. The Architect places new code with it, the Builder follows it and the Reviewer checks the diff against it. | The Architect follows the layering of the existing code, and the Reviewer skips its layer checks |

Your files can also use the `.json` extension; PEV reads either. The plugin's seed script copies `doc-topology` and `test-policy` into `.pev/`, and `review-criteria` when you run it with `--review-criteria` (see [setup.md](setup.md)); edit them to fit your project. Each one explains what its sections control. No template ships for the architecture policy: write it yourself, one section per rule.

### Indexing your SOPs

PEV needs `.pev` indexed: the cycle and instance templates live in `.pev/templates/` and are cloned through axiom-graph, so `.pev` must be in `docs_dirs`. `scripts/pev-seed.sh` from the plugin adds the setting when your `axiom-graph.toml` has no `[axiom_graph.scan]` table (and creates the file when it is missing), along with the `frozen_tags` that keep PEV's run records and closed requests out of `check`; [setup.md](setup.md) describes the step. The setting looks like this:

```toml
[axiom_graph.scan]
docs_dirs = ["docs", ".pev"]
```

The key is `docs_dirs`; a misspelled key is ignored without an error. With `.pev` indexed, your SOP files get ids that keep the `.pev` root, such as `<project_id>::.pev/test-policy`, and agents can read them through axiom-graph when a file path doesn't resolve.

### Project commands

PEV's agents run your tests and axiom-graph through the commands in the `[commands]` table of `.pev/sops.toml`, if you have that file. A key the file lacks is detected from your project files for each run. PEV only reads this file; you write it yourself. [setup.md](setup.md) lists the keys and has an example.

## Cycle records and friction logs

Each cycle is a directory, `docs/pev/cycles/<cycle-id>/`, of seven docs, each written by one role:

| Doc | What it holds |
|---|---|
| `manifest` | The status and phase, the request, the scope and the change set |
| `architect` | The Architect's pitch, with one subsection per task |
| `decisions` | What was chosen, the alternatives considered, and why |
| `builder` | The Builder's plan and, per task, its progress and report, including deviations: "plan said X, I did Y, because Z" |
| `review` | The Reviewer's result for each pass, then its verdict |
| `audit` | The Auditor's impact report, a summary of its doc changes, and the doc review |
| `friction` | An agent's notes on a tool, skill or step that got in its way, in a group per agent |

The manifest also holds the stale node ids recorded when the cycle started (`baseline`), which the merge step compares against, and the list of fixes you approved at each round (`fix-list`). A sharded review adds one `review-shard-<letter>` doc per shard beside `review`. The cycle's docs are created and audited in the worktree and reach main with the merge.

`decisions` and `friction` are logs: each entry is its own subsection, added once and never edited. Agents can only write the docs they own, and the plugin refuses an edit that targets another one. The manifest carries the cycle's status tag. A cycle started on an earlier version is a single file, `docs/pev/cycles/<cycle-id>.docjson`, and finishes in that form.

The cycle docs are copied from templates in your project's `.pev/templates/cycle/`. If they are missing, `/pev-cycle` stops at intake and tells you to run `scripts/pev-seed.sh` from the plugin, which copies in any missing templates and never overwrites one you have edited.

Friction groups are usually empty: agents write an entry only when something got in the way. An agent also logs a `script-read` entry when it had to read a doc or index data with a script instead of an axiom-graph tool. `/pev-instance` puts its notes in the checkin doc's `friction` section. Because manifests and checkins are indexed, `axiom_graph_search` finds recurring friction across cycles.

To use the logs, search by the tags agents give their entries, such as `axiom-graph-staleness`, `instruction-ambiguity` and `role-pinch`, and read the friction of the last several cycles now and then: cluster the entries and turn the repeated ones into changes to a skill, an SOP or a tool. Keep the three records apart: `decisions` hold what was chosen and why, the Builder's deviations hold where the work departed from the plan, and friction holds what got in an agent's way.

Every run is one folder holding its docs, and each cycle, `/pev-instance` run and audit skill run (`/pev-audit-dev-docs`, `/pev-audit-annotations`, `/pev-audit-consumer-docs`) ends with an efficiency report in it that records each agent's tool calls. A cycle's is `docs/pev/cycles/<cycle-id>/efficiency`, an instance's is `docs/pev/instances/<instance-id>/efficiency` and an audit's is `docs/pev/audits/<audit-id>/efficiency`. When a run spanned several sessions the reports are `efficiency-s2`, `efficiency-s3` and so on. The plugin's `scripts/analyze_pev_session.py` builds them: `--find-run <run-id>` finds the sessions that worked a run in this project (its main checkout and its worktrees; `--project-root` names another one) and counts only their calls for it, so another project that reused the run id is left out. Reports written by earlier plugin versions stay under `docs/pev/cycles/efficiency/`.

## Warnings on direct DocJSON edits

The plugin watches for direct edits of `*.docjson` files in every Claude Code session, not only PEV's, because a direct edit leaves the axiom-graph index out of step with the file. By default the edit goes through with a note naming the axiom-graph doc tools to use instead, and `axiom_graph_accept_doc_edits` for keeping an edit that already landed. Set `PEV_DOCJSON_GUARD` in the environment Claude Code runs with, for example the `env` block of `.claude/settings.json`, to change this:

| Value | Effect |
|---|---|
| `warn` (default) | Allows the edit and adds the note |
| `block` | Denies the edit |
| `off` | Does nothing |

In `warn` and `block` mode, the first direct read of a `.docjson` file in a session also gets a one-line hint about `axiom_graph_read_doc` and `axiom_graph_search`. Reads are never blocked.
