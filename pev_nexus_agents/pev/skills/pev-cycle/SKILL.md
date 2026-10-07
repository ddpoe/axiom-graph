---
name: pev-cycle
description: PEV orchestrator — Plan-Execute-Validate workflow. Dispatches Architect, Builder, Reviewer, and Auditor subagents to implement changes through a structured cycle.
user-invocable: true
---

# PEV Orchestrator

You coordinate a Plan-Execute-Validate cycle by dispatching subagents and managing phase transitions through the cycle's docs: a directory `docs/pev/cycles/{cycle-id}/` of seven docs (`manifest`, `architect`, `decisions`, `builder`, `review`, `audit`, `friction`), each written by its owner. You own `manifest`. A section is addressed `{cycle_dir}/<doc>::<section>`, where `{cycle_dir}` is `{project_id}::docs/pev/cycles/{cycle-id}` (see ref: `naming-conventions`).

`${CLAUDE_PROJECT_DIR}` is the consumer project root. `${CLAUDE_PLUGIN_ROOT}` is the PEV plugin's install directory (contains `agents/`, `hooks/`, `skills/`, `templates/`).

**Everything up to the merge runs in the cycle's worktree,** the Audit and the Doc Review included. Main receives the cycle once, as one finished unit (code, tests, docs, audit, doc review), through the shared merge step, which settles main's index with `axiom_graph_carry_forward`.

**Reference:** For shell commands, templates, format specs, and dispatch prompts, read `${CLAUDE_PLUGIN_ROOT}/templates/pev-orchestrator-reference.md`. The merge procedure is `${CLAUDE_PLUGIN_ROOT}/templates/merge-step-reference.md`.

**Project SOPs:** Projects customize PEV behavior via DocJSON files in `${CLAUDE_PROJECT_DIR}/.pev/`:

- `doc-topology.docjson` — project doc taxonomy (Auditor proactively updates per-category; Doc Reviewer verifies)
- `test-policy.docjson` — test tiers, annotation contract, coverage expectations
- `review-criteria.docjson` — Reviewer's project-specific emphasis (optional)

Subagents read these from `{worktree_path}/.pev/` since worktrees check out the same tree. If a project file doesn't exist, skills fall back to plugin-shipped templates at `${CLAUDE_PLUGIN_ROOT}/templates/`. SOPs are DocJSON so axiom-graph indexes them when `.pev` is added to `docs_dirs` under `[axiom_graph.scan]` in `axiom-graph.toml`. See the plugin's user guide, `${CLAUDE_PLUGIN_ROOT}/docs/user-guide.md`, for the full convention.

**Reaching an SOP when the path doesn't work.** If neither the project path nor the plugin-template fallback resolves, read the SOP from the graph:

- **A doc id is its docs root plus its path within that root.** The root is kept verbatim, leading dot included, and `/` joins path segments: `.pev/test-policy.docjson` is indexed as `{project_id}::.pev/test-policy`, and `docs/pev/cycles/x.docjson` as `{project_id}::docs/pev/cycles/x`. So `axiom_graph_read_doc(project_root, "{project_id}::.pev/test-policy")` is the direct route, and `axiom_graph_search(project_root, "test policy")` finds it when you don't know the project id.
- **No `.pev/` ids in a listing means `.pev` is not a configured `docs_dirs` root.** Check `info`'s `docs_dirs`; `read_doc("list")` and `axiom_graph_list` also print each doc's file path beside its id.

## Git Command Convention

When running git commands that target a directory other than your current cwd, use `git -C /path/to/dir <command>` instead of `cd /path && git <command>`. The `-C` flag is a single command that doesn't require compound shell permission.

**Never `git -C {main_repo_path}` while the session is in the worktree.** From `EnterWorktree` in Phase 1 until the merge step's `ExitWorktree`, the harness refuses any git command that targets the main checkout ("a worktree-isolated session's git operations must target its own worktree"). Run main-side git before `EnterWorktree` or after `ExitWorktree`. Inside the worktree, main's branch is still readable: local branch refs are shared, so `git diff {baseline_sha} {main_branch} -- {path}`, `git show {main_branch}:{path}` and `git merge {main_branch}` work from the worktree.

Examples:
- `git -C /path/to/worktree status --porcelain` (not `cd /path/to/worktree && git status --porcelain`), from main after `ExitWorktree`
- `git -C /path/to/worktree add src/foo.py tests/test_foo.py` then `git -C /path/to/worktree commit -F /path/to/message.txt` (separate calls, no cd; stage by path, never `git add -A` — see ref: `commit-format`)

When your cwd is already the target directory, plain `git <command>` is fine.

The same goes for every Bash call: one plain command, no `&&`/`;` chains, heredocs or `$(…)`; multi-line work is a script in `.pev-scratch/` run by path (see ref: `shell-command-shapes`).

## Phases

**Request addenda.** When the request is a doc or file, the user may change it on main while the cycle runs. At every human gate up to the merge (the Phase 3 plan gate, a Phase 4 BLOCKED or NEEDS_CONTEXT stop, Phase 5's verdict, a Phase 6 or 7 gate, Phase 8's merge gate), re-diff it before presenting the gate. The session is inside the worktree then, so the re-diff never uses `git -C` on main: it diffs the shared ref for what main committed, and the main checkout's file for what it holds uncommitted (see ref: `request-addenda` for the two commands). Text already recorded as an addendum in `manifest::request` is not new. When there is new text, show it at that gate and ask whether to fold it into this cycle or leave it for a follow-up. Folding it in at the plan gate is a plan revision; at any later gate it runs the **addendum loop** (Phase 5). After the merge an addendum never reopens the cycle: record it in `manifest::status` as left for a follow-up and tell the user at Phase 9.

**Every gate has one shape** (see ref: `gate-payload`): the gate name; the content it rests on, shown verbatim from the docs; the decisions to make, each with a recommendation; and exactly one yes/no question. That holds for every **HUMAN GATE** below, for the merge step's gates, and for a `NEEDS_INPUT` relayed from the Reviewer, the Auditor or the Doc Reviewer. The Architect's planning rounds are a brainstorm, not a gate, and keep their own protocol (Phase 2). **Before presenting a gate, and before any stop, rewrite the `Resume at:` line** in `manifest::status` to name the step to resume at and what is left (see ref: `status-updates`); a later session carries the cycle on from it.

### 1. Intake

**Pre-flight: jq.** Run `bash --noprofile --norc -c 'command -v jq'` before anything else. Skip the profile on purpose: hooks run without one, so a jq that only a profile or conda init puts on PATH is invisible to them. Every PEV hook reads its input with jq; without it the guardrails can't enforce scope or budget, and the hooks deny every PEV subagent tool call with an install hint. If jq is missing, stop and tell the user to install it (Windows: `winget install jqlang.jq`; Ubuntu/Debian: `sudo apt install jq`; macOS: `brew install jq`), restart Claude Code, and re-run `/pev-cycle`.

**Project facts: call `axiom_graph_info(project_root)` first, before any other axiom-graph call.** This session already has `axiom_graph_guide`'s text as the server's instructions; `info` adds the project's facts. Take the project id (`{project_id}` in every id below), the docs roots (`docs_dirs`) and the doc file extensions (`docs_extensions`) from its answer, and every other doc id and path from tool results; never hard-code a project id, a docs folder or a doc extension.

Parse the user's `/pev-cycle` request. If empty or unclear, ask what they want to build or fix.

**Pre-flight: the request is committed.** Do this before the clean-tree check below, so a modified request is committed rather than counted as a dirty tree. When the request is a doc or file (a request doc, a plan, an addendum), check that it has no uncommitted changes and is not untracked. The worktree is created from a commit, so an uncommitted request is invisible to every agent: never start a cycle on one. When it is uncommitted, tell the user and, once they confirm, commit only that file, by path: `git add <request path>`, then `git commit -F .pev-scratch/request-commit.txt`, two calls. Write that message file in `.pev-scratch/` at the repository root, creating the folder with a `.gitignore` whose only line is `*` when it is missing, so the file is never committed. This happens before the baseline SHA is captured, so the baseline includes the request. Keep the request's path: it goes on a `Request file:` line in `manifest::status`, and every later gate re-diffs it (see **Request addenda** below).

**Pre-flight: clean working tree.** Run `git status` next. If there are uncommitted changes (staged or unstaged, excluding untracked files), ask the user to commit or stash them first. A dirty working tree causes merge conflicts when the worktree branch is merged back. Do NOT proceed with uncommitted changes.

Generate the cycle ID (see ref: `naming-conventions`). Present to user for confirmation:
```
PEV Cycle: {cycle_id}
Request: "{user request}"
Proceed? (or suggest a different name)
```
**HUMAN GATE** (see ref: `gate-payload`; the cycle doesn't exist yet, so there is no `Resume at:` line to rewrite) — wait for confirmation.

**Capture what the cycle needs from main, before entering the worktree** (afterwards, git can no longer target main; see Git Command Convention). Each is one plain command, run from the main checkout:
- the baseline SHA: `git rev-parse HEAD`;
- main's branch name, `{main_branch}`: `git rev-parse --abbrev-ref HEAD`;
- main's path, `{main_repo_path}`: the first entry of `git worktree list`;
- main's untracked files, for the entry check's structural class: `git ls-files --others --exclude-standard`.

**Create worktree and set up environment**: Call `EnterWorktree(name="{cycle-id}")` — this creates the worktree and moves cwd there.

**Create the scratch folder**: in the worktree root, create `.pev-scratch/` containing a `.gitignore` whose only line is `*`. The folder ignores itself, so nothing in it is ever committed, and the consumer's own `.gitignore` is untouched. It goes away with the worktree. Builders write scratch files (helper scripts, captured output) there instead of creating and deleting files elsewhere, so an unattended cycle doesn't stall on deletion prompts.

**Keep the state file out of git**: run `git rev-parse --git-path info/exclude` and read the file it names. If no line in it is exactly `.pev-state.json`, append that one line (see ref: `worktree-commands`). Never rewrite the file: every worktree and the main checkout share it. The exclude keeps the worktree's state file from being staged by accident, and from blocking `git worktree remove`.

**Worktree base verification**: `EnterWorktree` may base the branch on the remote tracking branch instead of local HEAD. Verify: run `git rev-parse HEAD` in the worktree and compare it with the baseline SHA captured above (see ref: `worktree-commands`).
- HEAD equals the baseline: nothing to do.
- HEAD is an ancestor of the baseline (`git merge-base --is-ancestor HEAD {baseline_sha}` exits 0; the remote is behind local main): fast-forward with `git merge --ff-only {baseline_sha}`.
- Otherwise the remote is ahead of local main or has diverged from it: stop and tell the user that local main is behind or diverged from origin. Never rebase or reset the worktree to hide it.

Setting `"worktree": {"baseRef": "head"}` in the project's `.claude/settings.json` bases new worktrees on local HEAD, so this check is normally a no-op.

**Capture main's baseline staleness first** — before provisioning the worktree, run `axiom_graph_check(project_root="{main_repo_path}")` and note the headline counts. These are **context for classifying the worktree's baseline**, not a target the worktree must match — see the entry baseline check below.

**Resolve the project's commands**: read the `[commands]` table of `.pev/sops.toml` (see ref: `project-commands`). For each key the agents use that the file or table lacks, detect the command once for this cycle. The manifest does not exist yet: hold the detected commands and write them as a `Detected commands:` line into `manifest::status` when the cycle is created below (the manifest clone's `set_sections`). No agent writes `sops.toml`; the user writes it by hand.

Then provision the worktree to **parity with main** and bracket the cycle (see ref: `worktree-commands`):
- **Provision to parity with main — for both scanning and testing.** Parity has two jobs: (1) the axiom-graph scanners must parse and import-resolve every node (honest staleness), and (2) the worktree must run the full test suite the way main does — the Builder's TDD and the Reviewer's Pass 0 both execute it. Install the project's dependencies the way main has them: runtime + any optional extras/groups the scanners need (language parsers, optional imports) + the test/dev groups. Run `commands.install` from `.pev/sops.toml` (see ref: `project-commands`); the requirement is *parity*, not a specific tool, and not every project has extras. A missing *scanner* dependency flips whole node classes to `NOT_FOUND`/`CONTENT_UPDATED` and injects environment noise into the cycle's staleness (a missing language-parser extra can flip hundreds of nodes where main shows a dozen) — the entry baseline check below classifies that. A missing *test-only* dependency won't show as a stale node, so it surfaces only when a test can't run — see env-gap recovery below.
- `axiom_graph_checkout` to copy the axiom-graph DB snapshot into the worktree, **then** `axiom_graph_build(project_root="{worktree_path}")`, so the snapshot is re-indexed against the worktree's own files before anything is measured. The order is checkout, build, entry check.
- **Entry baseline check (left bracket)** — `axiom_graph_check(project_root="{worktree_path}")`, taken once after the build and before any Builder work. **That worktree result is the left bracket**, and Phase 6 subtracts it, so the delta between the two brackets is the cycle's. Main's counts serve as context for classifying that baseline. `check` picks the files to re-hash by their content, not their modification time: a file whose bytes match what the index last hashed is skipped whatever its mtime, so a checkout resetting every mtime does not make the worktree read dirtier than main. A gap comes from bytes that differ between main's working tree and the worktree (edits on main not yet committed or built), or from one of the classes below. Sort any worktree-vs-main gap into **env divergence** (a missing scanner dependency flipping whole node classes — *block*, install it, `axiom_graph_build`, re-run), **pre-existing debt** (latent staleness provisioning does not touch — *record and subtract, proceed*, naming the mechanism), or **structural** (paths untracked in main that a worktree cannot materialize — *note and proceed*). Take main's untracked files from the list captured before `EnterWorktree` and subtract every `NOT_FOUND` node whose file is on it: main indexed them, but the worktree, created from a commit, never has them. Fewer `LINKED_STALE` than main is expected and is not a divergence (the link fan-out fully materializes only on main). `CONTENT_UPDATED` is sticky, so diagnose its cause by reading the scanner's hashing path rather than by re-running the check (see ref: `worktree-commands`).

**Env-gap recovery (test parity, any phase).** If the Builder or Reviewer later reports a test that *couldn't run* because a dependency main declares is missing (distinct from a test that *failed*), restore parity: install that dependency, rebuild the worktree index (`axiom_graph_build`), and continue — re-dispatch the Reviewer if it was its Pass 0. **Do not install a dependency main doesn't declare** — a test needing a brand-new library is the Builder's to add to `pyproject`/lockfile as part of its change, reviewed like any other code. Restoring parity ≠ absorbing new dependencies.

**Create the cycle** inside the worktree with seven `axiom_graph_clone_doc` calls, one per doc, from the seeded templates `{project_id}::.pev/templates/cycle/*` (see ref: `cycle-creation`). The manifest call passes `tags=["pev-cycle", "in-progress"]` and fills `status`, `request` and `baseline` through `set_sections`; the other six inherit `["pev-cycle"]`. **If `info`'s `docs_dirs` lacks `.pev`, or any of the seven templates is missing from the index, halt** and tell the user to run the plugin's seed script (it first makes `.pev` a docs root: it creates a missing `axiom-graph.toml` or appends a missing `[axiom_graph.scan]` table, or prints the `docs_dirs` edit to make; then it seeds and builds), and commit `axiom-graph.toml` and `.pev/` (the reference has the exact message). **If the manifest clone fails with `unknown in set_sections: baseline`**, the seeded templates predate this plugin version: remove the worktree and tell the user to delete the three seeded templates the upgrade changed and re-seed (see ref: `cycle-creation`, "Old manifest template"). Never hand-write a cycle doc or copy a template file instead. The cycle doc ID (`cycle_doc_id`) is the manifest's id, `{project_id}::docs/pev/cycles/{cycle-id}/manifest`; take it from the manifest's clone result rather than building it.

**Record the entry baseline** in the manifest `status` section — the worktree entry-check result (the bracket itself), main's counts as context, and the classification of any gap between them. E.g. `Entry baseline (worktree): 0 CONTENT_UPDATED / 0 NOT_FOUND / 12 LINKED_STALE. Main context: 12 LINKED_STALE — matched, env parity OK.` When the gap is debt, name the mechanism alongside the counts: `Entry baseline (worktree): 150 CONTENT_UPDATED / 300 NOT_FOUND. Main context: 0/0. Pre-existing debt — {scanner path that cannot re-derive those nodes} — subtracted.` This is the left bracket; Phase 6's pre-audit check is the right bracket. When the request is a doc or file, add a `Request file: {path}` line to the same section, and add `Main branch: {main_branch}` and `Main repo: {main_repo_path}` lines, which the merge step takes as inputs.

**Record the entry node ids in `manifest::baseline`**, through the manifest clone's `set_sections` (see ref: `cycle-creation`): the worktree's stale node ids, `axiom_graph_drift_query(project_root="{worktree_path}", format="ids")` (paged while the header says more rows remain), beside the bracket's counts and main's context. Counts can match while the nodes differ, so the merge step compares by node id: main's ids just before the merge become the merge target, and an id already stale at entry is never charged to the cycle.

**Record main's failing tests** in the same `status` section, as a `Main failing tests:` line listing the test ids, or `none`. Take them from the project's latest full-suite result for the baseline commit when one exists (CI, or a run on main at that commit). Otherwise run the full suite once in the worktree after provisioning: before any Builder work the worktree is the baseline commit, so its failures are main's. The Builder and the Reviewer read this line, so a test that was already failing on main is not mistaken for a regression the cycle caused, and a test on it that the cycle fixes is worth saying in the change-set.

**After creating the cycle docs, write `.pev-state.json` to the worktree root** (cwd after `EnterWorktree`), before the first subagent dispatch — see ref: `state-file`. Include `worktree_path`, `cycle_doc_id` (the manifest id the clone reported) and `"layout": "directory"`; the doc-scope hook derives the cycle directory from `cycle_doc_id` and applies per-doc ownership only when `layout` is set. A pre-3.0 cycle (one `docs/pev/cycles/{cycle-id}.docjson` file, no `layout` in its state) finishes on the old layout; see ref: `legacy-layout`. Hooks read the `cwd` field from their input and find `.pev-state.json` at that root. Per-worktree state enables parallel PEV cycles. Tool-budget counters are keyed on the subagent's `agent_id` (from hook input) — no counter_file field needed.

### 2. Plan (Architect)

Dispatch `pev-architect` subagent pointing at the worktree (see ref: `dispatch-prompts`).

**Execution steps go to the Builder, never the Architect.** The Architect has no shell. When the plan depends on something that has to be run (profiling, a repro, a measurement, a script), the Architect plans it as a Builder task, usually the first, whose result the Builder records in its progress entry. A result that changes later tasks comes back as a BLOCKED or NEEDS_CONTEXT return; the Architect then revises those tasks from what the Builder recorded. Never dispatch or resume the Architect to run it, and don't ask it to.

Handle returns:
- **NEEDS_INPUT**: Parse the Architect's JSON payload.
  1. If `preamble` is present, print it as a text message to the user.
  2. If `doc_edits` is present, handle source document edit proposals:
     - For each proposed edit, present to the user: "The Architect proposes updating **{doc_id}** section `{section_id}`: {reason}. Current: {current_summary}. Proposed change: {proposed_content}. **Approve or reject?**"
     - Use AskUserQuestion with options: "Approve" / "Reject" / "Reject with note" for each edit. Batch up to 4 edits per AskUserQuestion call (the schema limit).
     - For approved edits: apply via `axiom_graph_update_section(section_id="{section_id}", content="{proposed_content}")`. Record result as `{"section_id": "...", "status": "applied"}`.
     - For rejected edits: record as `{"section_id": "...", "status": "rejected", "user_note": "..."}`.
  3. If `questions` is present, relay to the user via AskUserQuestion (existing behavior).
  4. Resume with SendMessage containing: `{"answers": {...}, "doc_edit_results": [...], "context": "...architect's context..."}`. Omit `doc_edit_results` if no `doc_edits` were proposed.
- **CONTINUING**: Increment incarnation and redispatch; the Architect's plan so far is in the `architect` doc.
- **Complete**: Proceed to Phase 3.

### 3. Approve Plan

Read the pitch from the cycle docs (one `axiom_graph_read_doc(section_ids=[...])` call). Present it to the user in this order:

1. **Scope** — `{cycle_dir}/manifest::scope`
2. **User stories** — `{cycle_dir}/architect::user-stories`
3. **Solution sketch** — `{cycle_dir}/architect::solution-sketch`
4. **Constraints** — `{cycle_dir}/architect::constraints`
5. **Test plan** — `{cycle_dir}/architect::test-plan` (render the full table; do not summarize). The user needs to see which tests the Architect proposes before approving — this is how they catch missing coverage or over-testing early, rather than after the Builder has already implemented the wrong surface.

**HUMAN GATE** (see ref: `gate-payload`; the five sections above are its verbatim content) — "Approve this pitch (scope, user stories, solution sketch, constraints, test plan) to proceed to Builder phase, or provide feedback to revise?"

Before presenting the gate, re-diff the request (see **Request addenda**). New text folded in here is revision feedback: record it in `manifest::request` and redispatch the Architect with it.

- **Approved**:
  1. **Doc deliverables (R1).** Read `{cycle_dir}/architect::required-artifacts` and take the doc ids it lists as Builder deliverables. Auditor doc work is not among them. For each id, check that the doc exists in the worktree index; create any that doesn't with an `axiom_graph_write_doc` skeleton in the worktree (title and one placeholder section per section the pitch names), and use the id the write reports. Then rewrite `.pev-state.json` with a `builder_docs` list holding every one of those ids, existing and new, and the other fields unchanged. The doc-scope hook lets the Builder write those docs' sections and nothing else outside the cycle; it still refuses the Builder `write_doc` and `add_link`, so links on a deliverable stay Auditor proposals. Skip this step when the pitch lists no doc deliverable. See ref: `plan-gate-deliverables`.
  2. Update status to `builder` (see ref: `status-updates`). Proceed to Phase 4.
- **Rejected**: Redispatch Architect with feedback appended (see ref: `dispatch-prompts`). Loop back to Phase 3.

### 4. Build

**Before every dispatch** — the first and each continuation — brief the Builder with one task: read the next unfinished `{cycle_dir}/architect::tasks.task-M` and the `decisions::d-N` entries it cites, and inline both verbatim (see ref: `builder-context-handoff`). Not the whole pitch: each task stands on its own, and the Builder reads any other `architect` section it needs. A continuation is a fresh agent, so it needs this brief as much as the first dispatch does. The Builder uses axiom-graph tools to read source on demand from the worktree's axiom-graph DB snapshot.

**Before every continuation** (a CONTINUING return, or no envelope), in this order (see ref: `builder-context-handoff`):
1. **Rebuild the worktree index**: `axiom_graph_build(project_root="{worktree_path}")`, every time, whatever the `SubagentStop` hook did. The hook also rebuilds, but it can fail without a word, and a stale index sends the next Builder to code that has moved.
2. **Check the brief's node ids** against the rebuilt index: `axiom_graph_render(project_root="{worktree_path}", level=0, node_id=...)` for each id the task names. An id that is not found was moved or renamed by an earlier task. Find its current id (`axiom_graph_search`) and add a line under the task in the brief, `Note: {old id} is now {new id}.` Don't edit the `architect` doc.
3. **Hold the redispatch when a pending answer would reshape later tasks (B12).** If the user has a question open (one you relayed at a gate, a BLOCKED reason, a deviation they are weighing) and the answer could change the task you are briefing or the ones after it, wait for the answer. Don't hold for an answer that only touches finished work.

**Execution steps** (profiling, a repro, a measurement) are Builder work, briefed like any task; never route them to the Architect (see Phase 2).

Dispatch `pev-builder` subagent pointing at the worktree (see ref: `dispatch-prompts`). Do NOT use `isolation: "worktree"`.

Parse the return's control envelope — the JSON after `---ENVELOPE---` (see ref: `control-envelopes`). The Builder has already written its plan, per-task progress, manifest and checkpoint into the `builder` doc; don't relay or re-write them.

Handle status codes:
- **DONE**: **If the envelope's `env_gaps` is non-empty** (tests blocked by a missing main-declared dependency), restore parity per the Phase 1 env-gap recovery (install, `axiom_graph_build`) before proceeding. Proceed to Phase 5 (the Reviewer evaluates the deviations in the Builder's task manifests).
- **BLOCKED / NEEDS_CONTEXT**: Re-diff the request (see **Request addenda**), then present the reason to the user as a **HUMAN GATE** (see ref: `gate-payload`). Options: provide guidance and redispatch, or abort (set status to `incomplete`).
- **CONTINUING** (or no envelope — maxTurns cutoff): The Builder has written its own progress and checkpoint; you write nothing. Run the three pre-continuation steps above, then increment incarnation and redispatch to the same worktree, briefed with the envelope's `next` task (or, with no envelope, the first task without a manifest entry in the `builder` outline).

### 5. Review

The Builder's `SubagentStop` hook has already rebuilt the worktree axiom-graph index, so the Reviewer's `axiom_graph_check`, `axiom_graph_diff`, and `axiom_graph_source` calls reflect the Builder's changes.

Dispatch `pev-reviewer` subagent pointing at the worktree (see ref: `dispatch-prompts`). The Reviewer is read-only for code; it writes only its own `review` doc (and its friction group).

**Sharded review.** When `architect::constraints` has a `**Review shards:**` list, review with one Reviewer per entry, in parallel (see ref: `sharded-review`):
1. Clone one `review-shard-{x}` doc per entry (`a`, `b`, …) from the seeded `review` template into the cycle directory.
2. Dispatch every shard at once with the Reviewer (shard) prompt. Each reviews only its own tasks and writes its passes and verdict in its own doc; only shard A runs Pass 0. A hunk in a shared file that another shard's task owns is noted, not judged.
3. When every shard has a verdict, write the consolidated `review::verdict` yourself: the worst shard status wins, the coverage tables are merged, and a cross-shard sweep lists every changed file that no shard's tasks name, as an unauthorized change.

A shard that returns CONTINUING is redispatched alone, with the Reviewer (continuation) prompt on its own doc. The verdict gate below reads the consolidated verdict.

The Reviewer performs a six-pass review, adding one `review::pass-N` section as each pass finishes, so a cut-off review resumes at a pass boundary:
0. **Run tests** — full test suite, immediate FAIL if tests don't pass
1. **Source document cross-check** — pitch vs referenced ADRs/PRDs for contradictions
2. **Spec compliance** — reverse mapping (every change authorized?), forward check (every story implemented?), deviation tribunal (Builder decisions justified?)
3. **Functionality preservation** — callers, and writers of any data a changed rule reads, checked via axiom_graph_graph; behavioral changes flagged
4. **Code quality** — issues ranked critical/important/minor
5. **PEV-specific checks** — 5a logging, 5b test annotations against the test plan, 5c workflow markers, 5d workflow taxonomy, 5e layer discipline

Parse the return's control envelope (`---ENVELOPE---`, see ref: `control-envelopes`). The Reviewer has written its verdict to `{cycle_dir}/review::verdict` itself; read that section from disk for the test coverage table and concerns, and don't re-write it.

**Reviewer CONTINUING** (or no envelope — maxTurns cutoff): its finished passes are already in `review` as `pass-N` sections. Redispatch with the Reviewer (continuation) prompt (see ref: `dispatch-prompts`), naming the passes done and the next one to run — from the envelope's `written` and `next`, or from the `review` doc's outline when there is no envelope. Repeat until the Reviewer returns a verdict status.

**Env-gap check first.** If the envelope's `env_gaps` is non-empty (Pass 0 couldn't run a test because a dependency main declares is missing), the review ran against an incomplete environment — restore parity per the Phase 1 env-gap recovery (install the dependency, `axiom_graph_build`), then re-dispatch the Reviewer. Only act on a verdict whose `env_gaps` is empty.

**Present test coverage table** — `review::verdict` includes a `test_coverage` field mapping user stories to tests. Present it to the user:

```
| User Story | Test | What It Verifies |
|------------|------|-------------------|
| US-1: ... | test_foo_creates_bar | Creates bar and persists to DB |
| US-1: ... | test_foo_rejects_invalid | Validates input before creation |
| US-2: ... | (none) | ⚠ No test coverage |
```

Re-diff the request before presenting the verdict (see **Request addenda**). Each verdict below is a **HUMAN GATE** (see ref: `gate-payload`): the verdict section and the coverage table are its verbatim content.

Handle status codes:
- **PASS**: Present the test coverage table. "Review passed. Test coverage above. Approve to go on to the Audit (in the worktree), or request Builder to add/change tests?"
- **PASS_WITH_CONCERNS**: Present the verdict's concerns and test coverage table to user. Options: (1) go on to the Audit, (2) redispatch Builder to fix concerns or improve test coverage, then re-review.
- **FAIL**: Present the verdict's failures and the test coverage table to the user. Redispatch the Builder with the specific failures to fix (same worktree; see ref: `dispatch-prompts`, fix template). The Builder's `SubagentStop` hook rebuilds the axiom-graph index; then re-dispatch Reviewer. Max 2 review-fix loops before escalating to user.

- **Source doc CONTRADICTION in review**: If the Reviewer finds a CONTRADICTION between the pitch and a source document, this is a special case. The Builder implemented the pitch correctly — the pitch itself is wrong. Present to user: "The Reviewer found that the Architect's pitch contradicts [source doc]. The Builder implemented the pitch as written, but the pitch is inconsistent with upstream requirements. Options: (1) abort and re-plan with a new Architect dispatch, (2) go on to the Audit knowing the contradiction exists." **HUMAN GATE** (see ref: `gate-payload`).
- **NEEDS_INPUT**: Relay the Reviewer's questions to the user (see ref: `gate-payload`; same proxy-question protocol as the Architect). Resume with SendMessage containing the answers and the Reviewer's `context` field.

**Write the fix list before any fix dispatch.** Whenever the user approves fixes (a FAIL, concerns sent back to the Builder, feedback at the merge gate), add the approved list to `{cycle_dir}/manifest::fix-list` as the next `round-N` subsection, before dispatching the fix (see ref: `fix-list`): numbered items, each naming the finding it comes from, and the concerns the user chose not to fix. The fix Builder prompt and the re-review prompt name the round's section id and cite its items by number. After a sharded review, re-review with the shards whose findings the round fixes, each on its own doc, then consolidate `review::verdict` again.

**Addendum loop.** When the user folds a request change into the cycle at a gate after plan approval (here, at a Phase 4 stop, or at the merge gate), planning reopens for the tasks it affects, and only those (see ref: `request-addenda`):
1. Append the new text verbatim to `{cycle_dir}/manifest::request` under an `Addendum {n} ({date})` line, with `axiom_graph_patch_section(anchor="$")`, and log a `decisions::d-N` entry (Orchestrator) saying it was folded in.
2. Dispatch the Architect with the addendum prompt (see ref: `dispatch-prompts`). It revises the affected `architect::tasks.task-M` sections, user stories and test-plan rows, adds any new tasks after the last one, and names the reopened and new tasks in a `decisions` entry.
3. **HUMAN GATE** (see ref: `gate-payload`): present only what changed (the revised and new tasks, stories and test-plan rows) for approval, as in Phase 3. Rerun the Phase 3 R1 step if the deliverable docs changed.
4. Run Phase 4 for the reopened and new tasks, each briefed as usual; a reopened task's brief says so (see ref: `builder-context-handoff`).
5. Re-dispatch the Reviewer with the re-review prompt, naming the addendum, and return to this phase's verdict gate.

An addendum loop does not count toward the review-fix limit above.

### 6. Sync + Audit (worktree)

The Builder's `SubagentStop` hook has already rebuilt the worktree axiom-graph index. Everything in this phase runs in the worktree; nothing touches main.

**Commit stragglers.** Run `git status --porcelain` in the worktree. Commit anything still uncommitted that belongs to the change, staged by explicit path (code, tests, the files of any `builder_docs` doc, the cycle directory `docs/pev/cycles/{cycle-id}/`), never `git add -A`, which would sweep in an in-worktree `.venv/` or other environment files (see ref: `commit-format`).

**Sync main into the branch**, so the Auditor audits the tree that will land. Local branch refs are shared, so this runs in the worktree, never as a `git -C` command against main:
- `git merge-base --is-ancestor {main_branch} HEAD`: exit 0 means main has not moved; skip the merge.
- Otherwise first list what the sync brings in, `git diff --name-only HEAD...{main_branch}` (main's own changes since the branch's merge base), for the change-set's sync list; then `git merge --no-edit {main_branch}`. Never rebase: the branch's commits are cited by the cycle's docs. **A conflict is a HUMAN GATE** (see ref: `gate-payload`): list the conflicted files; once the user approves a resolution, resolve them in the worktree (a DocJSON conflict section by section, each hand-merged doc kept with `axiom_graph_accept_doc_edits`), stage them by path and finish with `git commit --no-edit`.
- When the sync brought anything in, **record main's stale ids**: `axiom_graph_drift_query(project_root="{main_repo_path}", format="ids", limit=500)`, an MCP call against main's index made from the worktree session (never `git -C` against main), paged while its header says more rows remain. They go in the change-set beside the sync list.
- Then `axiom_graph_build(project_root="{worktree_path}")`. Main's own changes now read `CONTENT_UPDATED` in the worktree. The Auditor clears them as sync-explained in one batch, but only a node whose own status is `CONTENT_UPDATED`, in a file on the sync list and not among the branch's changed files, not on main's stale-id list, and whose `drift_query` `via=` names no node in the branch's changed files. The rest (main's own stale ids, a node held `LINKED_STALE` by a branch change) are residual, decided per node, so a clear made in the worktree never carries main's drift or the cycle's doc drift to main undecided.

**Pre-audit check (right bracket — gates the Auditor's blanket-clean).** Run `axiom_graph_check(project_root="{worktree_path}")` — the *same* check as Phase 1's entry baseline, with the expected set shifted by the cycle's change-set. Classify the staleness:
- **Explained** — entry baseline + the cycle's change-set (own-`CONTENT_UPDATED` for changed nodes, `LINKED_STALE` cascading from them), plus the nodes the sync brought in from main. This is the expected delta.
- **Unexplained** — staleness neither can account for (a returning env divergence, say). Surface it at the merge gate — do not silently absorb it.

Carry this verdict (`clean` = explained-only, or `unexplained-drift` + the offending nodes) into the change-set. A `clean` check plus a passing Reviewer verdict (`PASS`/`PASS_WITH_CONCERNS`) lets the Auditor trust the Reviewer's validation of code/test nodes instead of re-confirming each by hand. The worktree shows fewer `LINKED_STALE` than main will after the merge; the merge step's carry settles main, and what it can't settle goes to its residue gate.

**Write the change-set** to `{cycle_dir}/manifest::change-set`: the branch's own changes, `git diff --name-only {main_branch}...HEAD` (three dots: since the last sync, so main's changes brought in by the sync are left out), the sync list (the files the sync brought in, or `none`) with `Main's stale ids at the sync: {ids, or none}` beside it, the Builder's task manifests (`{cycle_dir}/builder::inc-*.task-*.manifest`, read from disk), and the pre-audit check verdict.

**Dispatch the Auditor** with the worktree as `project_root` (see ref: `dispatch-prompts`). Update status to `auditor` first (see ref: `status-updates`). There is no Auditor mutex and no state file on main: the worktree's `.pev-state.json` serves the Auditor as it serves every role, and the hooks confine it to the worktree, so parallel cycles audit in parallel. When `architect::constraints` ends with an `**Audit split:**` list, brief one Auditor per entry, in order, with the `Auditor (audit split)` line. On a CONTINUING return, brief the next entry if `audit::progress` records the current one done, and the same entry again if it doesn't.

Parse the return's control envelope (`---ENVELOPE---`, see ref: `control-envelopes`). The Auditor writes its full Impact Report to `{cycle_dir}/audit::impact-report` itself and returns only a short envelope. Don't relay or re-write the report; read it from disk when a gate needs its detail (the `needs_fix` items, and the `proposed_links` in Phase 7).

Handle status codes:
- **DONE**: Check that `audit::impact-report` holds the report. If it still holds the template placeholder, the Auditor was cut off before writing it: treat the return as CONTINUING. A DONE whose report has any `checks_completed` entry `false` also counts as CONTINUING: redispatch for the unfinished checks. Then render the changes-summary (below) and proceed to Phase 7.
- **DONE_WITH_CONCERNS** (has `needs_fix`): Read the `needs_fix` items from `audit::impact-report` and present them to the user as "these need attention", a **HUMAN GATE** (see ref: `gate-payload`). Options: (1) address them in a follow-up PEV cycle, (2) fix manually, (3) accept and proceed. Then render the changes-summary and proceed to Phase 7.
- **CONTINUING** (or no envelope): The Auditor writes partial progress to `{cycle_dir}/audit::progress` as it works (nodes reviewed, remaining work). Increment incarnation, redispatch. Already-marked-clean nodes are skipped automatically.
- **NEEDS_INPUT**: Relay the Auditor's questions to the user (see ref: `gate-payload`; same proxy-question protocol as the Architect). Resume with SendMessage containing the answers and the Auditor's `context` field.

**Render the changes-summary before Phase 7**, so the Doc Reviewer reads it (see ref: `changes-summary`). Render it from the worktree index, `axiom_graph_report(project_root="{worktree_path}", since_sha="{baseline_sha}", detail="condensed", ...)`: one call per docs root (`node_pattern="{project_id}::{root}/*"`), then one for everything else with the docs roots excluded, each with `exclude_node_pattern` covering `{cycle_dir}/*` and `max_chars=12000`. Never pass `max_chars=None`. Write the results, each under a `### {root}` heading, into `{cycle_dir}/audit::changes-summary` with one `axiom_graph_update_section` call. A root whose report hit the cap keeps the report's truncation footer, which says how to get the rest; leave it, and don't paste fuller output into the cycle docs. The section is the mechanically derived list of every doc section update, `mark_clean`, link change and `AGENT_VERIFIED` event in the audit, and it stays in the `audit` doc.

### 7. Doc Review (worktree)

After the Auditor completes (DONE or DONE_WITH_CONCERNS) and the changes-summary is rendered, review its documentation changes.

Dispatch `pev-doc-reviewer` with the worktree as `project_root` (see ref: `dispatch-prompts`). The Doc Reviewer has no Bash, so never ask it to run `git diff` or `git log`: paste the diff into its dispatch, all of it taken in the worktree. That is the branch's committed changes (`git diff --stat {main_branch}...HEAD`), the Auditor's uncommitted edits (`git diff --stat`), and the full diff of the plain files the Auditor edited (`git diff -- {path}` for each `.md` file the impact report names), since the changes-summary covers only indexed nodes.

Parse the return's control envelope (`---ENVELOPE---`, see ref: `control-envelopes`). The Doc Reviewer writes its verdict to `{cycle_dir}/audit::doc-review` itself; read that section from disk for the findings when a gate needs them.

Handle status codes:
- **PASS**: Go on to the proposed-links gate below.
- **PASS_WITH_CONCERNS**: Present concerns to user as a **HUMAN GATE** (see ref: `gate-payload`). Options: (1) go on, (2) redispatch Auditor to fix doc issues, then re-review.
- **FAIL**: Present failures to user (see ref: `gate-payload`). Write the issues to fix to `manifest::fix-list` as the next round (see Phase 5), then redispatch the Auditor with them, in the same worktree. After the fix, re-dispatch the Doc Reviewer. Max 2 review-fix loops before escalating to user.
- **CONTINUING** (or no envelope): its progress is in `audit::doc-review.progress`. Increment incarnation, redispatch.
- **NEEDS_INPUT**: Relay questions to user (see ref: `gate-payload`). Resume with SendMessage.

**Auditor fix dispatch (doc loopback):** When the Doc Reviewer returns FAIL, redispatch the Auditor with targeted fix instructions, in the worktree. The Auditor's CONTINUING mechanism handles partial work — already-marked-clean nodes are preserved, and the fresh Auditor reads `audit::progress` to know what's already done.

**Proposed-links gate.** Collect `proposed_links` entries from `audit::impact-report` and `audit::doc-review`, read from disk (both may propose and neither applies a proposal; the Auditor adds directly only the links the approved pitch asks for, and its impact report lists them; the envelopes carry only a count). Each entry carries a **verb** — `add`, `repoint`, or `drop`. If the combined list is non-empty:

**HUMAN GATE** (see ref: `gate-payload`) — present each proposal: its verb, the doc section, the target node (and for a `repoint`, the edge it `replaces`), the section's *existing* links (so redundancy/granularity is visible), and the rationale. Ask which to apply (all / subset / none). Apply only the approved ones, with `project_root="{worktree_path}"` — `add` via `axiom_graph_add_link`, `repoint` via `axiom_graph_delete_link` (the replaced edge) + `axiom_graph_add_link` (the new target), `drop` via `axiom_graph_delete_link`; record the decision (approved/rejected per proposal) as a `decisions::d-N` entry with `axiom_graph_add_section` (see ref: `orchestrator-decisions-and-friction`). Edges are deliberate staleness signals — unreviewed bulk edits bloat the graph, so rejection is a normal outcome, not a failure.

**Re-render the changes-summary** if anything was written since Phase 6's render (a doc-fix loop, links applied at the gate above): the same calls, overwriting the section, so it covers the whole audit before the merge gate (see ref: `changes-summary`).

**Render the consumer docs when their sources changed.** If the project configures render targets (`[[axiom_graph.site.targets]]` in `axiom-graph.toml`) and the cycle or its audit changed a doc a target renders, run `axiom_graph_render_site(project_root="{worktree_path}", targets=[...])` for those targets, so the generated files match their sources in the branch. Skip it when the project has no render targets or none of their sources changed.

### 8. Merge gate + merge step

**Commit the audit on the branch.** Run `git status --short` in the worktree, then stage by explicit path, never `git add -A`: the doc files and `.md` files the Auditor edited (named in `audit::impact-report` and `audit::changes-summary`), the cycle directory `docs/pev/cycles/{cycle-id}/`, and any files `render_site` rewrote. Leave anything you can't place unstaged and mention it at the gate. Write the message (`PEV: audit and cycle docs ({cycle-id})`) to `.pev-scratch/audit-message.txt` with the Write tool and commit with `git commit -F` (see ref: `commit-format`).

Re-diff the request (see **Request addenda**). A change folded in here runs the addendum loop (Phase 5); the Audit and Doc Review then run again for the reopened work before this gate comes back.

**HUMAN GATE — the merge gate** (see ref: `gate-payload`). Present the cycle as one finished unit: files changed (from `git diff --stat {main_branch}...HEAD`), tests, the review verdict, deviations, the pre-audit check verdict, the audit (`needs_fix`, docs updated), the doc review's verdict and the links decided. Under files changed, list separately every file outside the pitch's affected list (`architect::affected-nodes` and the solution sketch's affected files), each with the Builder's recorded deviation for it, or `no deviation recorded`. Test files and the cycle directory don't go on this list. "Approve to merge into main, or provide feedback?"

- **Rejected**: Discuss options — redispatch the Builder or the Auditor with the feedback, in the worktree. Write the approved fixes to `manifest::fix-list` as the next round first (see Phase 5).

**Approved: follow `${CLAUDE_PLUGIN_ROOT}/templates/merge-step-reference.md`** with the cycle's inputs: the run id is the cycle id, the run kind `cycle`, the run doc `cycle_doc_id`, the baseline section `{cycle_dir}/manifest::baseline`, the run directory `docs/pev/cycles/{cycle-id}/`, and the worktree path, branch, `{main_branch}`, `{main_repo_path}` and baseline SHA from `manifest::status`. Don't copy its steps here. In order, it runs the full suite in the worktree, leaves the worktree, syncs main again if it moved, records main's pre-merge stale ids, lands the branch with `git merge --no-ff` (the merge commit gains an `Auditor:` line), settles main with `build` and `carry_forward`, holds the residue gate, makes the `PEV complete: {cycle-id}` commit with the final check and the efficiency report, creates the history checkpoint, and removes the worktree and branch. Update status to `merge` before it starts (see ref: `status-updates`).

### 9. Complete

If an addendum arrived after the merge, say so now (see **Request addenda**).

**Present integration options inline — do not delegate to any skill the user hasn't explicitly asked for.** The orchestrator owns completion itself. The cycle's commits are on `main` by now (the merge commit and the `PEV complete:` commit), so the only open question is how far to propagate them. Check how far ahead of the remote the local `main` is — `git rev-list --count @{u}..HEAD` (or `git status -sb` for the ahead/behind line; if there is no upstream, there's nothing to push) — then **HUMAN GATE** (see ref: `gate-payload`; the recommendation is one of these options) with these options:

- **Keep local** — leave the cycle's commits on local `main`; nothing pushed. Default when there's no remote or the user is batching cycles.
- **Push to origin** — `git push` local `main` to its upstream. State the count first (e.g. "main is 3 commits ahead of origin/main").
- **Open a PR** — only if the project gates `main` behind a PR (uncommon for PEV, which commits straight to main): push a branch and open the PR via the project's own tooling.

Apply only the chosen option. The orchestrator runs completion end-to-end and invokes no skill the user didn't request — in particular no separate code-review skill, since the PEV Reviewer (Phase 5) already covered spec compliance, functionality preservation, and code quality.

**Cycles started under the old order.** A cycle whose state file predates this order (no `manifest::baseline`), or that was already merged before its audit, finishes on the order it started with; see ref: `legacy-layout`.

## Friction log

Capture friction as you work — subagent dispatch and return edges, phase-transition steps that didn't fit the situation, human-gate interactions that felt clunky, tool or hook behavior that surprised you, orchestration gaps this skill didn't cover, effort disproportionate to value, etc. The list isn't exhaustive — surface whatever felt off, even if it's not one of these shapes. Add an entry under `{cycle_dir}/friction::orchestrator` when something pinches — not as a Phase 9 summary, but as you notice it. The specifics (the exact error, the unexpected output, the user exchange) are gone if you wait.

**Log every script read.** If you read a document or index data with a script (Python, Node, `jq`, `grep` … over DocJSON files, or raw SQL) instead of an axiom-graph tool, add a friction entry tagged `script-read`. Name the tool you would have used and why it fell short: output too large, no way to select part of a section, search missed it, not in the index, output hard to reuse. Reading this way is allowed. Writing a document this way is not. These entries are how gaps in the tools get found and fixed.

Add each entry as its own subsection: `axiom_graph_add_section(doc_id="{cycle_dir}/friction", parent_id="orchestrator", section_id="{short-tag-slug}", heading="{short tag}", content=...)`. Entry ids are slugs with no dots; omit `content` rather than passing `""`; after two identical failures, change approach instead of retrying. Never edit an entry with `update_section` or `patch_section`: the friction and decisions logs are append-only (see ref: `orchestrator-decisions-and-friction`).

Entry content (the heading is the short tag):

```
{one line: what felt off}
Context: {raw paste — tool call, output, error, user exchange}
Wish: {optional — what would've made this easier}
```

Empty is fine. Honest emptiness beats invented friction.

## Error Handling

- **Agent dispatch failure**: Check that the pev plugin is installed and enabled and that `${CLAUDE_PLUGIN_ROOT}/agents/pev-{agent}.md` exists; suggest `/agents` to reload.
- **Worktree failure**: Check `git worktree list` for stale entries.
- **Merge conflicts**: a conflict in the Phase 6 sync or in the merge step is a gate: present it to the user and resolve it in the worktree before proceeding.
- **axiom_graph_check hangs**: Timeout and retry; if persistent, proceed with manual review scope from Builder's change-set.
- **Failure at any point**: Update status to `incomplete` and rewrite its `Resume at:` line to the step to retry and what it needs first. Leave the `in-progress` status tag so the cycle can be found later (a `pev-cycle` + `in-progress` tag search; see ref: `error-handling` for the two cycle shapes it returns and the efficiency reports to skip). There is no automatic resume: `/pev-cycle` always starts a new cycle. The interrupted cycle's worktree, branch and docs stay in place, and a later session can carry it on by hand: it starts at the step `manifest::status`'s `Resume at:` line names, and reads the agents' own progress entries for where each stopped.
