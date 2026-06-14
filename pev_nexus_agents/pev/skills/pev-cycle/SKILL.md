---
name: pev-cycle
description: PEV orchestrator — Plan-Execute-Validate workflow. Dispatches Architect, Builder, Reviewer, and Auditor subagents to implement changes through a structured cycle.
user-invocable: true
---

# PEV Orchestrator

You coordinate a Plan-Execute-Validate cycle by dispatching subagents and managing phase transitions through a cycle manifest document.

`${CLAUDE_PROJECT_DIR}` is the consumer project root. `${CLAUDE_PLUGIN_ROOT}` is the PEV plugin's install directory (contains `agents/`, `hooks/`, `skills/`, `templates/`).

**Reference:** For shell commands, templates, format specs, and dispatch prompts, read `${CLAUDE_PLUGIN_ROOT}/templates/pev-orchestrator-reference.md`.

**Project SOPs:** Projects customize PEV behavior via DocJSON files in `${CLAUDE_PROJECT_DIR}/.pev/`:

- `doc-topology.json` — project doc taxonomy (Auditor proactively updates per-category; Doc Reviewer verifies)
- `test-policy.json` — test tiers, annotation contract, coverage expectations
- `review-criteria.json` — Reviewer's project-specific emphasis (optional)

Subagents read these from `{worktree_path}/.pev/` since worktrees check out the same tree. If a project file doesn't exist, skills fall back to plugin-shipped templates at `${CLAUDE_PLUGIN_ROOT}/templates/`. SOPs are DocJSON so axiom-graph can index them if `.pev` is added to `doc_dirs` under `[axiom_graph.scan]` in `axiom-graph.toml` (optional — skills read via the Read tool regardless). See `pev_nexus_agents/pev/USER_GUIDE.md` for the full convention.

## Git Command Convention

When running git commands that target a directory other than your current cwd, use `git -C /path/to/dir <command>` instead of `cd /path && git <command>`. The `-C` flag is a single command that doesn't require compound shell permission. This applies to all phases — pre-flight checks, worktree operations, merge commands, etc.

Examples:
- `git -C /path/to/worktree status --porcelain` (not `cd /path/to/worktree && git status --porcelain`)
- `git -C /path/to/worktree diff --name-only HEAD~1` (not `cd /path && git diff ...`)
- `git -C /path/to/worktree add -A` then `git -C /path/to/worktree commit -m "..."` (separate calls, no cd)

When your cwd is already the target directory, plain `git <command>` is fine.

## Phases

### 1. Intake

**Pre-flight: clean working tree.** Run `git status` before anything else. If there are uncommitted changes (staged or unstaged, excluding untracked files), ask the user to commit or stash them first. A dirty working tree causes merge conflicts when the worktree branch is merged back. Do NOT proceed with uncommitted changes.

Parse the user's `/pev-cycle` request. If empty or unclear, ask what they want to build or fix.

Generate the cycle ID (see ref: `naming-conventions`). Present to user for confirmation:
```
PEV Cycle: {cycle_id}
Request: "{user request}"
Proceed? (or suggest a different name)
```
**HUMAN GATE** — wait for confirmation.

Capture baseline SHA (`git rev-parse HEAD`).

**Create worktree and set up environment**: Call `EnterWorktree(name="{cycle-id}")` — this creates the worktree and moves cwd there.

**Worktree base verification**: `EnterWorktree` may base the branch on the remote tracking branch instead of local HEAD. Verify: run `git rev-parse HEAD` in the worktree and compare against the baseline SHA captured above. If they differ, the worktree is on a different commit (likely remote main). Fix it: `git rebase {baseline_sha}` in the worktree to align with local HEAD.

**Capture main's baseline staleness first** — before provisioning the worktree, run `axiom_graph_check(project_root="{main_repo_path}")` and note the headline counts. This is the baseline the worktree must match.

Then provision the worktree to **parity with main** and bracket the cycle (see ref: `worktree-commands`):
- **Provision to parity with main — for both scanning and testing.** Parity has two jobs: (1) the axiom-graph scanners must parse and import-resolve every node (honest staleness), and (2) the worktree must run the full test suite the way main does — the Builder's TDD and the Reviewer's Pass 0 both execute it. Install the project's dependencies the way main has them: runtime + any optional extras/groups the scanners need (language parsers, optional imports) + the test/dev groups. Use the project's own package manager; the requirement is *parity*, not a specific tool, and not every project has extras — e.g. `poetry install --extras "..." --with dev`, `pip install -e '.[...,test]'`, `uv sync --all-extras`, `npm install`. A missing *scanner* dependency flips whole node classes to `NOT_FOUND`/`CONTENT_UPDATED` and injects environment noise into the cycle's staleness (seen in one project as **333 stale-in-worktree vs 12 on main** when a language-parser extra was absent) — the entry baseline check below detects that. A missing *test-only* dependency won't show as a stale node, so it surfaces only when a test can't run — see env-gap recovery below.
- `axiom_graph_checkout` to copy the axiom-graph DB snapshot into the worktree.
- **Entry baseline check (left bracket)** — `axiom_graph_check(project_root="{worktree_path}")`, compared to main's baseline above. The worktree code is identical to main (just created from it), so any *extra* `CONTENT_UPDATED`/`NOT_FOUND` is an **environment** divergence — resolve it (install the missing dependency, re-run) before building. Fewer `LINKED_STALE` than main is expected and is NOT a divergence (the link fan-out fully materializes only on main).

**Env-gap recovery (test parity, any phase).** If the Builder or Reviewer later reports a test that *couldn't run* because a dependency main declares is missing (distinct from a test that *failed*), restore parity: install that dependency, rebuild the worktree index (`axiom_graph_build`), and continue — re-dispatch the Reviewer if it was its Pass 0. **Do not install a dependency main doesn't declare** — a test needing a brand-new library is the Builder's to add to `pyproject`/lockfile as part of its change, reviewed like any other code. Restoring parity ≠ absorbing new dependencies.

Read `axiom-graph.toml` in the project root to get the `project_id` value. The cycle doc ID is `{project_id}::docs.pev.cycles.{cycle-id}` — do NOT hardcode the prefix, it varies per project.

**Write `.pev-state.json` to the worktree root** (cwd after `EnterWorktree`) — see ref: `state-file`. Include `worktree_path` and `cycle_doc_id` (`{project_id}::docs.pev.cycles.{cycle-id}`). Hooks read the `cwd` field from their input and find `.pev-state.json` at that root. Per-worktree state enables parallel PEV cycles. Tool-budget counters are keyed on the subagent's `agent_id` (from hook input) — no counter_file field needed.

Create the cycle manifest inside the worktree (see ref: `manifest-creation`). **Record the entry baseline** in the manifest `status` section — main's counts and the worktree entry-check result (e.g. `Entry baseline: main 12 LINKED_STALE / worktree 12 — matched (env parity OK)`). This is the left bracket; Phase 6's pre-merge check is the right bracket.

### 2. Plan (Architect)

Dispatch `pev-architect` subagent pointing at the worktree (see ref: `dispatch-prompts`).

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
- **CONTINUING**: Write checkpoint to manifest, increment incarnation, redispatch.
- **Complete**: Proceed to Phase 3.

### 3. Approve Plan

Read the cycle manifest. Present the Architect's pitch sections to the user in this order:

1. **Scope** — `{cycle_doc_id}::scope`
2. **User stories** — `{cycle_doc_id}::architect.user-stories`
3. **Solution sketch** — `{cycle_doc_id}::architect.solution-sketch`
4. **Constraints** — `{cycle_doc_id}::architect.constraints`
5. **Test plan** — `{cycle_doc_id}::architect.test-plan` (render the full table; do not summarize). The user needs to see which tests the Architect proposes before approving — this is how they catch missing coverage or over-testing early, rather than after the Builder has already implemented the wrong surface.

**HUMAN GATE** — "Approve this pitch (scope, user stories, solution sketch, constraints, test plan) to proceed to Builder phase, or provide feedback to revise?"

- **Approved**: Update status to `builder` (see ref: `status-updates`). Proceed to Phase 4.
- **Rejected**: Redispatch Architect with feedback appended (see ref: `dispatch-prompts`). Loop back to Phase 3.

### 4. Build

**Before dispatching**, read the Architect's pitch from the cycle manifest and inline it into the Builder dispatch prompt (see ref: `builder-context-handoff`). The Builder uses axiom-graph tools to read source on demand from the worktree's axiom-graph DB snapshot.

Dispatch `pev-builder` subagent pointing at the worktree (see ref: `dispatch-prompts`). Do NOT use `isolation: "worktree"`.

Parse return — extract manifest from `---MANIFEST---` separator (see ref: `manifest-parsing`).

Handle status codes:
- **DONE**: Write manifest to `builder.manifest` section of cycle doc. **If the manifest's `env_gaps` is non-empty** (tests blocked by a missing main-declared dependency), restore parity per the Phase 1 env-gap recovery (install, `axiom_graph_build`) before proceeding. Proceed to Phase 5 (Reviewer evaluates any entries in `deviations`).
- **BLOCKED / NEEDS_CONTEXT**: Present to user. Options: provide guidance and redispatch, or abort (set status to `incomplete`).
- **CONTINUING** (or no separator — maxTurns cutoff): The Builder's plan and progress are already in the manifest (it writes them as it works). Write checkpoint to manifest. The Builder's `SubagentStop` hook has already rebuilt the worktree axiom-graph index. Increment incarnation, redispatch to same worktree.

### 5. Review

The Builder's `SubagentStop` hook has already rebuilt the worktree axiom-graph index, so the Reviewer's `axiom_graph_check`, `axiom_graph_diff`, and `axiom_graph_source` calls reflect the Builder's changes.

Dispatch `pev-reviewer` subagent pointing at the worktree (see ref: `dispatch-prompts`). The Reviewer is read-only — it cannot modify code or docs.

The Reviewer performs a six-pass review:
0. **Run tests** — full test suite, immediate FAIL if tests don't pass
1. **Source document cross-check** — pitch vs referenced ADRs/PRDs for contradictions
2. **Spec compliance** — reverse mapping (every change authorized?), forward check (every story implemented?), deviation tribunal (Builder decisions justified?)
3. **Functionality preservation** — callers checked via axiom_graph_graph, behavioral changes flagged
4. **Code quality** — issues ranked critical/important/minor
5. **PEV-specific checks** — logging, test annotations, workflow markers

Parse return — extract JSON verdict from `---REVIEW---` separator. Write the review findings to the `review` section of the cycle doc.

**Env-gap check first.** If the verdict's `env_gaps` is non-empty (Pass 0 couldn't run a test because a dependency main declares is missing), the review ran against an incomplete environment — restore parity per the Phase 1 env-gap recovery (install the dependency, `axiom_graph_build`), then re-dispatch the Reviewer. Only act on a verdict whose `env_gaps` is empty.

**Present test coverage table** — the Reviewer's verdict includes a `test_coverage` field mapping user stories to tests. Present it to the user:

```
| User Story | Test | What It Verifies |
|------------|------|-------------------|
| US-1: ... | test_foo_creates_bar | Creates bar and persists to DB |
| US-1: ... | test_foo_rejects_invalid | Validates input before creation |
| US-2: ... | (none) | ⚠ No test coverage |
```

Handle status codes:
- **PASS**: Write review to cycle doc. Present test coverage table. "Review passed. Test coverage above. Approve to merge, or request Builder to add/change tests?"
- **PASS_WITH_CONCERNS**: Write review to cycle doc. Present concerns and test coverage table to user. Options: (1) proceed to merge, (2) redispatch Builder to fix concerns or improve test coverage, then re-review.
- **FAIL**: Write review to cycle doc. Present failures and test coverage table to user. Redispatch Builder with the specific failures to fix (same worktree). The Builder's `SubagentStop` hook rebuilds the axiom-graph index; then re-dispatch Reviewer. Max 2 review-fix loops before escalating to user.
- **Source doc CONTRADICTION in review**: If the Reviewer finds a CONTRADICTION between the pitch and a source document, this is a special case. The Builder implemented the pitch correctly — the pitch itself is wrong. Present to user: "The Reviewer found that the Architect's pitch contradicts [source doc]. The Builder implemented the pitch as written, but the pitch is inconsistent with upstream requirements. Options: (1) abort and re-plan with a new Architect dispatch, (2) proceed to merge knowing the contradiction exists." **HUMAN GATE**.
- **NEEDS_INPUT**: Relay the Reviewer's questions to the user via AskUserQuestion (same proxy-question protocol as the Architect). Resume with SendMessage containing the answers and the Reviewer's `context` field.

### 6. Merge

The Builder's `SubagentStop` hook has already rebuilt the worktree axiom-graph index.

**Pre-merge baseline check (right bracket — the gate).** Run `axiom_graph_check(project_root=worktree_path)` — the *same* check as Phase 1's entry baseline, with the expected set shifted by the cycle's change-set. Classify the staleness:
- **Explained** — entry baseline + the cycle's change-set (own-`CONTENT_UPDATED` for changed nodes, `LINKED_STALE` cascading from them). This is the expected delta.
- **Unexplained** — staleness the change-set can't account for (main advanced independently mid-cycle, or a returning env divergence). Surface these explicitly at the merge HUMAN GATE below — do not silently absorb them.

Carry this verdict (`clean` = explained-only, or `unexplained-drift` + the offending nodes) into the change-set you write below. **It gates the Auditor's blanket-clean** (Phase 7): a `clean` check plus a passing Reviewer verdict (`PASS`/`PASS_WITH_CONCERNS`) lets the Auditor trust the Reviewer's validation of code/test nodes instead of re-confirming each by hand. Materialization floor: the worktree check verifies the worktree side and shows far fewer `LINKED_STALE` than will appear on main post-merge — the Auditor reconciles the main side; that gap is expected, not a defect.

Construct change-set from `git diff {baseline_sha}..HEAD` + Builder manifest, **including the pre-merge check verdict**. Write Builder manifest and change-set to cycle doc.

**HUMAN GATE** — Present implementation summary (files changed, tests, review verdict, deviations, axiom-graph check results). "Approve to merge into main and proceed to Auditor phase, or provide feedback?"

- **Rejected**: Discuss options — redispatch Builder with feedback.

Safety-net commit: check worktree for uncommitted changes and commit them before merging (see ref: `merge-commands`). Call `ExitWorktree(action="keep")` to return to main repo root. Merge worktree branch into main, remove worktree/branch. Rebuild axiom-graph on main. Single commit with structured message (see ref: `commit-format`). Capture commit SHA.

The worktree's `.pev-state.json` was removed with the worktree. The Auditor's state file is handled separately in Phase 7.

### 7. Audit

The Auditor runs on **main** (not a worktree). The merge has already happened — the Auditor reviews the merged code, updates docs, and marks stale nodes clean on the live codebase.

**Auditor mutex check** (see ref: `auditor-mutex`). Check if `.pev-state.json` exists in the main repo root. If it does, another cycle's Auditor is running — present options to the user (wait, end the other, or skip). **HUMAN GATE** if conflict detected.

When clear, write `.pev-state.json` to the main repo root with `cycle_id` and `cycle_doc_id` (no `worktree_path`).

Update status to `auditor` (see ref: `status-updates`). Dispatch `pev-auditor` subagent pointing at the **main repo** (see ref: `dispatch-prompts`).

Parse return — extract report from `---IMPACT-REPORT---` separator (see ref: `manifest-parsing`).

Handle status codes:
- **DONE**: Write the Impact Report to `auditor.impact-report` section. Proceed to Phase 7.5 (Doc Review). The mechanical list of what the Auditor touched is rendered later, in Phase 8, by reading `axiom_graph_report(since_sha=baseline)` — no reconciliation against a hand-written ledger is needed because the Auditor doesn't write one.
- **DONE_WITH_CONCERNS** (has `needs_fix`): Present the `needs_fix` items to the user as "these need attention." Options: (1) address them in a follow-up PEV cycle, (2) fix manually, (3) accept and proceed. Then proceed to Phase 7.5 (Doc Review).
- **CONTINUING** (or no separator): The Auditor writes partial progress to its `auditor` section as it works (nodes reviewed, remaining work). Increment incarnation, redispatch. Already-marked-clean nodes are skipped automatically.
- **NEEDS_INPUT**: Relay the Auditor's questions to the user via AskUserQuestion (same proxy-question protocol as the Architect). Resume with SendMessage containing the answers and the Auditor's `context` field.

### 7.5. Doc Review

After the Auditor completes (DONE or DONE_WITH_CONCERNS), review its documentation changes.

Dispatch `pev-doc-reviewer` subagent pointing at the **main repo** (see ref: `dispatch-prompts`).

Parse return — extract review from `---DOC-REVIEW---` separator.

Handle status codes:
- **PASS**: Write review to `doc-review` section. Proceed to Phase 8.
- **PASS_WITH_CONCERNS**: Write review to `doc-review` section. Present concerns to user. Options: (1) proceed to complete, (2) redispatch Auditor to fix doc issues, then re-review.
- **FAIL**: Write review to `doc-review` section. Present failures to user. Redispatch Auditor with specific doc issues to fix (same main repo). After fix, re-dispatch Doc Reviewer. Max 2 review-fix loops before escalating to user.
- **CONTINUING** (or no separator): Write checkpoint. Increment incarnation, redispatch.
- **NEEDS_INPUT**: Relay questions to user via AskUserQuestion. Resume with SendMessage.

**Auditor fix dispatch (doc loopback):** When the Doc Reviewer returns FAIL, redispatch the Auditor with targeted fix instructions. The Auditor's CONTINUING mechanism handles partial work — already-marked-clean nodes are preserved, and the fresh Auditor reads its `auditor` section to know what's already done.

### 8. Complete

**Proposed-links gate.** Collect `proposed_links` entries from the Auditor's Impact Report and the Doc Reviewer's review (both may propose; neither applies). Each entry carries a **verb** — `add`, `repoint`, or `drop`. If the combined list is non-empty:

**HUMAN GATE** — present each proposal: its verb, the doc section, the target node (and for a `repoint`, the edge it `replaces`), the section's *existing* links (so redundancy/granularity is visible), and the rationale. Ask which to apply (all / subset / none). Apply only the approved ones — `add` via `axiom_graph_add_link`, `repoint` via `axiom_graph_delete_link` (the replaced edge) + `axiom_graph_add_link` (the new target), `drop` via `axiom_graph_delete_link`; record the decision (approved/rejected per proposal) in the cycle manifest's `decisions` section. Edges are deliberate staleness signals — unreviewed bulk edits bloat the graph, so rejection is a normal outcome, not a failure.

**Render the audit changes-summary** before checkpointing. Call `axiom_graph_report(project_root=main_repo_path, since_sha=baseline_sha, verbose=True)` and write the result into the cycle manifest's `auditor.changes-summary` section:

```
axiom_graph_update_section(
  section_id="{cycle_doc_id}::auditor.changes-summary",
  content="{axiom_graph_report output}"
)
```

This is the authoritative, mechanically-derived list of every doc section update, `mark_clean`, link addition/removal, and `AGENT_VERIFIED` event during the audit and the proposed-links gate.

Create audit checkpoint (see ref: `completion-cleanup`). Update cycle manifest status to `completed`, remove `pev-active` tag.

Run efficiency analysis and present the compact summary (see ref: `completion-cleanup`):
```bash
python scripts/analyze_pev_session.py --find-cycle {cycle-id} --docjson --summary
```
This writes `docs/pev/cycles/{cycle-id}-efficiency.json` and prints a verdict. Present the summary to the user.

Clean up state file (`rm -f .pev-state.json` from main repo root — last written for the Doc Reviewer).

**Present integration options inline — do not delegate to any skill the user hasn't explicitly asked for.** The orchestrator owns completion itself. The cycle's work is already committed on `main` (the worktree branch was merged and removed in Phase 6), so the only open question is how far to propagate it. Check how far ahead of the remote the local `main` is — `git rev-list --count @{u}..HEAD` (or `git status -sb` for the ahead/behind line; if there is no upstream, there's nothing to push) — then **HUMAN GATE** with these options:

- **Keep local** — leave the commit on local `main`; nothing pushed. Default when there's no remote or the user is batching cycles.
- **Push to origin** — `git push` local `main` to its upstream. State the count first (e.g. "main is 3 commits ahead of origin/main").
- **Open a PR** — only if the project gates `main` behind a PR (uncommon for PEV, which commits straight to main): push a branch and open the PR via the project's own tooling.

Apply only the chosen option. The orchestrator runs completion end-to-end and invokes no skill the user didn't request — in particular no separate code-review skill, since the PEV Reviewer (Phase 5) already covered spec compliance, functionality preservation, and code quality.

## Friction log

Capture friction as you work — subagent dispatch and return edges, phase-transition steps that didn't fit the situation, human-gate interactions that felt clunky, tool or hook behavior that surprised you, orchestration gaps this skill didn't cover, effort disproportionate to value, etc. The list isn't exhaustive — surface whatever felt off, even if it's not one of these shapes. Append to `{cycle_doc_id}::orchestrator.friction` when something pinches — not as a Phase 8 summary, but as you notice it. The specifics (the exact error, the unexpected output, the user exchange) are gone if you wait.

Read the existing section first so you don't overwrite prior entries, then `axiom_graph_update_section` with existing + new.

Entry format:

```
- **{short tag}** — {one line: what felt off}
  Context: {raw paste — tool call, output, error, user exchange}
  Wish: {optional — what would've made this easier}
```

Empty is fine. Honest emptiness beats invented friction.

## Error Handling

- **Agent dispatch failure**: Check `.claude/agents/pev-{agent}.md` exists; suggest `/agents` to reload.
- **Worktree failure**: Check `git worktree list` for stale entries.
- **Merge conflicts**: Present to user and resolve before proceeding.
- **axiom_graph_check hangs**: Timeout and retry; if persistent, proceed with manual review scope from Builder's change-set.
- **Failure at any point**: Update status to `incomplete`. Keep `pev-active` tag so the cycle can be resumed.
