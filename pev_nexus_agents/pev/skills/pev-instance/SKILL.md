---
name: pev-instance
description: Slim PEV mode for small, well-scoped tasks, one after another, each in its own worktree — mini-pitch + human gate (the checkin is cloned there) + implement + update affected docs + commit on the branch, then an independent Reviewer and Doc Reviewer in parallel and the shared merge step. The one agent builds and owns documentation upkeep itself (no separate Auditor). Its checkin doc under docs/pev/instances/{id}/ leaves a searchable record. Escalates to /pev-cycle when a task turns out bigger than scoped.
user-invocable: true
---

# PEV Instance — Slim Mode

You are the PEV Instance agent — a slim alternative to the full `/pev-cycle` orchestration for small, well-scoped tasks. Same discipline (user story, acceptance, doc upkeep, independent review, documented record), much less orchestration overhead. Where the full cycle splits the work across an Architect, a Builder and an Auditor, **you plan, build and do the Auditor's doc upkeep yourself** — so updating the documentation your change affects is *your* job, not something you flag and defer. Then an independent Reviewer and Doc Reviewer check your code and your docs: the author of a change must not be the only one checking it. Each task runs in its own git worktree, created from local HEAD, and lands on main through the same merge step as a cycle.

**Use this when:** the task touches 1–2 files, no public API or architecture change, no new user-facing feature — docstring fixes, single-file bug fixes, small refactors, config tweaks, documentation updates.

**Don't use this when:** the task is cross-cutting, touches core mechanisms (see "Escalation signal" below), or you're uncertain about scope. Use `/pev-cycle` instead — you can always escalate mid-instance if you discover the task is bigger than it looked.

## Instruction flow

**Reference.** The worktree commands, the gate shape and the shell command shapes are in `${CLAUDE_PLUGIN_ROOT}/templates/pev-orchestrator-reference.md` (its **Worktree Commands**, **Gate Payload** and **Shell Command Shapes** sections). The merge procedure is `${CLAUDE_PLUGIN_ROOT}/templates/merge-step-reference.md`. This skill points to them and keeps no copy.

**Every gate has one shape** (reference: **Gate Payload**): the gate name; the content it rests on, shown verbatim; the decisions to make, each with a recommendation; and exactly one yes/no question. That holds for every **HUMAN GATE** below and for the merge step's gates. Once the checkin exists, rewrite its `meta` `Resume at:` line before presenting a gate and before any stop.

**Resuming an open instance.** An open instance lives on its worktree's branch until it merges, so main's index does not hold its checkin. `git worktree list` shows the instance worktrees, `.claude/worktrees/pev-instance-*`. Search a worktree's own index for its checkin: `axiom_graph_search(project_root="{worktree_path}", query="pev-instance", tag="in-progress", scope="docs")`. The same search on main (`project_root="${CLAUDE_PROJECT_DIR}"`) finds an instance stopped after its merge but before its completion commit, and older instances that ran without a worktree. A result is either a checkin in its own directory, `{project_id}::docs/pev/instances/{instance-id}/checkin`, or an older single-file checkin, `{project_id}::docs/pev/instances/{instance-id}`; accept both, and skip any `efficiency` doc. A directory checkin's `meta` has a `Resume at:` line naming the step to continue from, and `Worktree:` and `Branch:` lines naming where it runs. Continue in that worktree: enter it with `EnterWorktree` when the harness can enter an existing worktree. When it can't, only Steps 5-7 may go on from main, passing the worktree's path as every `project_root` and running its git commands as `git -C {worktree_path} ...`. Step 8, up to the merge step's step 2 (leave the worktree), needs the session's cwd in the worktree: the hooks look for `.pev-state.json` at the root of the agent's cwd and, finding none, fail open, so reviewers dispatched from main would run with no doc-scope or `project_root` confinement, and the merge step's suite runs inside the worktree. So when `Resume at:` names Step 8 or the merge step's step 1 and the session can't enter the worktree, stop: leave `Resume at:` as it is and ask the user to open a session in the worktree (`{worktree_path}`) and resume the instance there. A `Resume at:` at the merge step's step 3 or later runs from main, as the procedure says. An older checkin has only its `Status` and `escalation` sections to go on.

### Step 1: Pre-flight checks

**jq check (hard stop).** Run `bash --noprofile --norc -c 'command -v jq'` in Bash — no profile, because hooks run without one, so a jq that only a profile or conda init puts on PATH won't count. PEV's hooks read their input with jq; without it they can't enforce anything, and they deny every PEV subagent tool call with an install hint. If jq is missing, stop and tell the user: *"PEV's hooks need jq, which isn't on PATH. Install it (Windows: `winget install jqlang.jq`; Ubuntu/Debian: `sudo apt install jq`; macOS: `brew install jq`), restart Claude Code, and re-run."* This one is not overridable.

**Project facts: call `axiom_graph_info(project_root)` first, before any other axiom-graph call.** This session already has `axiom_graph_guide`'s text as the server's instructions; `info` adds the project's facts. Take the project id (`{project_id}` in every id below), the docs roots (`docs_dirs`) and the doc file extensions (`docs_extensions`) from its answer, and every other doc id and path from tool results; never hard-code a project id, a docs folder or a doc extension.

**Tasks.** The request holds one or more tasks. They run one after another, each through Steps 3-8 in its own worktree: a task merges into main before the next task's worktree is created, so each task starts from a main that already holds the one before it. Never run two tasks at once. When the user lists several, confirm the order before Step 3. A task that escalates or stops ends the run: the later tasks are not started, and Step 9 lists them.

Uncommitted work on main is no gate. A task's worktree is created from a commit, so it never sees that work, and the merge step checks main again before anything lands.

### Step 2: Read the project SOPs

Load the project SOPs the same way the full-cycle agents do. Each with plugin fallback:

- Test policy: `${CLAUDE_PROJECT_DIR}/.pev/test-policy.docjson` (or another extension in `info`'s `docs_extensions`) → fallback `${CLAUDE_PLUGIN_ROOT}/templates/test-policy.docjson`
- Review criteria (optional): `${CLAUDE_PROJECT_DIR}/.pev/review-criteria.docjson` (or another extension in `info`'s `docs_extensions`) → no fallback; absent means no project-specific rules
- Doc review guide: `${CLAUDE_PROJECT_DIR}/.pev/doc-topology.docjson` (or another extension in `info`'s `docs_extensions`) → fallback `${CLAUDE_PLUGIN_ROOT}/templates/doc-topology.docjson`

These are your reference for tier assignments (test-policy), code-quality emphasis (review-criteria), and doc-drift checks (doc-topology).

If a path above doesn't resolve, the SOP is very likely still in the graph: when `.pev` is a configured `docs_dirs` root, `.pev/test-policy.docjson` is indexed as `{project_id}::.pev/test-policy` — a doc id keeps its docs root as the prefix. Reach it with `axiom_graph_read_doc`, or `axiom_graph_search("test policy")`. If no listing shows a `.pev/` id, check `info`'s `docs_dirs`: `.pev` is indexed only when it is one of them.

### Step 3: Scope assessment + escalation signal

Before planning, check whether the task is actually small. This step runs on main, before the task's worktree exists, so an escalation here leaves nothing to clean up.

**Run `axiom_graph_workflow_list(project_root="${CLAUDE_PROJECT_DIR}", has_steps=true)`** — these are the developer-declared core mechanisms in the project. If your task is likely to modify any of these functions, the plan gate asks the user "instance or cycle?" (Step 4); strongly consider recommending `/pev-cycle`, which adds an Architect, a separate Builder and an Auditor.

Other signals that warrant escalation (these are **examples**, use judgement):
- Task description mentions or clearly implies 4+ files affected
- Public API surface change (a function's signature, a CLI flag, an HTTP endpoint)
- New architectural decision needed (anything that would normally get an ADR)
- Change to authentication, storage, serialization, or any boundary other code depends on
- You expect to write more than ~3 new tests
- You're uncertain about scope

If you decide to escalate before writing the checkin:

```
SCOPE TOO LARGE FOR /pev-instance

This task {reason}. Recommend running `/pev-cycle` instead — it gives
you an Architect, a separate Builder and an Auditor.

I have not made any changes. Re-invoke with /pev-cycle when ready.
```

…and stop. No checkin doc, no commit.

If you decide to proceed, continue.

### Step 4: Worktree, mini-pitch, plan gate and the checkin

#### 4a. Pick the id and find the template (on main)

1. **Pick the id and check it is free.** Instance id: `pev-instance-YYYY-MM-DD-{slug}`, date-prefixed so listings show instances chronologically. It names the worktree, its branch and the run directory. Refuse the id, and pick another slug, when the file `docs/pev/instances/{instance-id}<ext>`, for any `<ext>` in `info`'s `docs_extensions`, or the directory `docs/pev/instances/{instance-id}/` already exists, or `git worktree list` already shows a worktree of that name.
2. **Find the template.** The seeded template is the indexed doc `{project_id}::.pev/templates/instance`. The project id is `info`'s project id. If `info`'s `docs_dirs` lacks `.pev`, or the template is not in the index, stop before creating anything and tell the user:

   ```
   The PEV instance template is not seeded in this project.
   Run: bash "${CLAUDE_PLUGIN_ROOT}/scripts/pev-seed.sh" --project-root {project root} --axiom-graph "{commands.axiom_graph}"
   If it prints an axiom-graph.toml edit (.pev missing from docs_dirs), make it and re-run the script.
   It builds the index when it is done. Then commit axiom-graph.toml and .pev/, and re-run /pev-instance.
   ```

   `{commands.axiom_graph}` is the `axiom_graph` key of the `[commands]` table in `.pev/sops.toml`; when the file or key is missing, use the command that runs axiom-graph in this project (`axiom-graph` when it is on PATH).

   There is no fallback: never hand-write the checkin with `axiom_graph_write_doc`, and never copy the template file on disk.

#### 4b. Enter the task's worktree

Follow the reference's **Worktree Commands**, with `{instance-id}` where it says `{cycle-id}` and the checkin where it says the manifest. In order:

1. **Main's facts, before `EnterWorktree`** (afterwards git can only target the worktree): the baseline SHA, `{main_branch}`, `{main_repo_path}` and main's untracked files, one plain command each. Then main's counts for context: `axiom_graph_check(project_root="{main_repo_path}")`.
2. **`EnterWorktree(name="{instance-id}")`.** `{worktree_path}` is the path it reports; the branch is `worktree-{instance-id}`.
3. **The base check**, the same rule as `/pev-cycle` Phase 1: HEAD equal to the baseline SHA is fine; HEAD an ancestor of it is fast-forwarded with `git merge --ff-only {baseline_sha}`; anything else stops the task, with nothing to keep: `ExitWorktree(action="remove")`, then tell the user that local main is behind or diverged from origin. Never rebase or reset the worktree.
4. **Provision:** the `.pev-scratch/` folder (its `.gitignore` holds the single line `*`), the `.pev-state.json` line in `info/exclude` (append it only when missing; never rewrite the file), and `commands.install` from `.pev/sops.toml`, for parity with main.
5. **Index:** `axiom_graph_checkout(project_root="{main_repo_path}", worktree_path="{worktree_path}")`, then `axiom_graph_build(project_root="{worktree_path}")`.
6. **Entry check:** `axiom_graph_check(project_root="{worktree_path}")` and `axiom_graph_drift_query(project_root="{worktree_path}", format="ids", limit=500)`, paged while its header says more rows remain. These stale ids are **the instance's baseline**: Step 6 charges the change only with ids that are not on it. Pre-existing residue is recorded in the checkin, not gated: don't clear it first and don't ask about it. Sort a gap between the worktree's counts and main's as the reference says: env divergence blocks (install the missing dependency, build, re-run the check), pre-existing debt and structural gaps are recorded.
7. **Main's failing tests:** as the reference's **Main's failing tests** paragraph says: the latest full-suite result for the baseline commit, or one run in the fresh worktree.

From here on, every axiom-graph call takes `project_root="{worktree_path}"`, every edit lands in the worktree and every git command is plain `git` from the worktree. Nothing touches main until the merge step.

#### 4c. Mini-pitch and the plan gate

**Trace callers and writers first.** For each function you expect to change, find its callers (`axiom_graph_graph(project_root="{worktree_path}", node_id, direction="in")`). If the change alters what data a rule reads, list every place that writes that data, not only the callers of the function you edited. Both go into the plan's file lists.

Compose the pitch in conversation. Required sections, half-page max:

```markdown
## Mini-pitch: {slug}

**Problem.** {one paragraph, what's broken / missing}

**User stories.** As a {user type}, I want {outcome} so that {benefit}.

**Acceptance.**
- {observable criterion 1}
- {observable criterion 2}

**Plan.**
- Will touch: {file path} — {what changes}
- May touch: {file path} — {why it might need to change: a caller, a writer, a likely knock-on}
- Writers: {every place that writes data a changed rule reads, or "none: {why}"}
- Tests: {N} test(s) at Tier {X} per .pev/test-policy.docjson, proving {acceptance criterion}
```

Make **May touch** generous. You approve nothing mid-run: a code file outside both lists is logged, and two of them stop the run (Step 5).

**Core mechanism.** When either list holds a function from Step 3's `workflow_list(has_steps=true)`, name it in the pitch, and the gate asks the user instead of you deciding alone.

**HUMAN GATE** (reference: **Gate Payload**). `Gate: plan approval ({instance-id})`; the content is the pitch in full, and the entry check's line. The checkin doesn't exist yet, so there is no `Resume at:` line to rewrite. When the plan touches a core mechanism, one decision is "instance or cycle for `{function}`", with your recommendation. The question: *"Approve the plan and implement it as an instance?"*

Proceed only on approval. If feedback, revise the pitch and present the gate again. If escalate (or "cycle"), bail per Step 3, and remove the worktree first: it holds no work yet, so `ExitWorktree(action="remove")`. When the tool refuses for untracked files, check that `git status --porcelain` lists only the copied index and the scratch folder, then remove it with `discard_changes: true`.

#### 4d. Create the checkin, before you edit anything

**Clone the template into the instance's own directory**, in the worktree, with the approved pitch and the entry baseline:

```
axiom_graph_clone_doc(
  project_root="{worktree_path}",
  source_doc_id="{project_id}::.pev/templates/instance",
  new_id="pev/instances/{instance-id}/checkin",
  title="{one-line human-readable title}",
  tags=["pev-instance", "in-progress"],
  set_sections={"meta": "Date: YYYY-MM-DD\nStatus: planned\nDuration (mins): {approx}\nWorktree: {worktree_path}\nBranch: worktree-{instance-id}\nBaseline SHA: {baseline_sha}\nMain branch: {main_branch}\nMain repo: {main_repo_path}\nMain failing tests: {ids, or none}\nResume at: Step 5 (implement)",
                "baseline": "Entry baseline (worktree): {check line}. Main context: {main's counts, and the gap's class when they differ}\nEntry stale ids (worktree): {ids, or none}",
                "problem": "...", "user-stories": "...", "acceptance": "...",
                "plan": "{Will touch, May touch, Writers and Tests, as approved}"}
)
```

When the clone reports the template missing from the worktree's index (it is seeded on main but not committed), remove the worktree as at the plan gate and give the user the 4a message.

When the clone fails with `unknown in set_sections: baseline`, the seeded template predates the checkin's `baseline` section, and the seed script never overwrites a seeded file. Remove the worktree as at the plan gate and tell the user to delete three seeded files on main, each with the extension it was seeded with: `.pev/templates/instance`, `.pev/templates/cycle/manifest` and `.pev/templates/cycle/review`. The same plugin upgrade changed all three, and `/pev-cycle` stops the same way at its manifest clone while the old manifest template stays. Then give the 4a message: the seed script copies the current templates, and after the build and the commit (`axiom-graph.toml` and `.pev/`) the instance can be re-run. The upgrade note in the plugin's `CHANGELOG.md` (Unreleased, "Audit in the worktree, one shared merge step") has the full steps.

The result prints the id the checkin was indexed under, `{project_id}::docs/pev/instances/{instance-id}/checkin`. That printed value is **the checkin id**: use it wherever you need the id, and don't build it by hand. The directory `docs/pev/instances/{instance-id}/` is the instance's: other records of this run go beside the checkin. `checkin::baseline` is the `{baseline_section}` the merge step appends main's pre-merge ids to.

**Keep the checkin current.** At every later gate, and before any stop, rewrite `meta`'s `Status` and `Resume at:` lines (`axiom_graph_update_section`), so a fresh session can pick the instance up from the checkin alone. The `Worktree:` and `Branch:` lines stay as written.

**Status tag.** The checkin carries `in-progress` until the instance finishes; the merge step's completion commit flips it to `completed` with `axiom_graph_update_doc_meta` (pass the full tag list; `tags` replaces it).

### Step 5: Implement

Edits in the worktree, within the approved plan:

- **Test first.** Write each new test first and confirm it fails for the reason its name gives. When the change has more than one part, run it once with only the part the test names left out; it should fail. Then implement. Run the tests from the worktree: the commands are in the `[commands]` table of `.pev/sops.toml` (`test_targeted` for one target, `test_parallel` or `test` for the suite); if they aren't, detect the runner from the project's manifest and lockfile.
- **A code file outside the plan is logged, not asked about.** When you need to change a code file in neither Will touch nor May touch, append a line to `checkin::plan` (`axiom_graph_patch_section(project_root="{worktree_path}", ..., anchor="$")`): "added: `<file>`, because …". Then keep working. Test files and doc files never count.
- **Hard line.** Two or more off-plan code files, or a function from `workflow_list(has_steps=true)` that the pitch didn't name, end the run. Stop cleanly, and leave the work on the branch:
  1. Finish or revert the edit in hand.
  2. Write your progress and findings to the checkin: `meta` `Status: continuing` and `Resume at:`, what is done and what is left in `escalation`.
  3. Commit the work in progress on the branch, staged by path (the files you changed and `docs/pev/instances/{instance-id}/`), with a clear subject: `{slug}: WIP, stopped at the hard line`.
  4. Leave the worktree in place: `ExitWorktree(action="keep")`.
  5. Return (Step 9), ending with one question for the user: *"Continue as an instance, or make it a cycle?"* Don't wait mid-run for an answer: the user may be away.

  Either answer starts from that branch. Continuing as an instance resumes in the worktree (see **Resuming an open instance**). A cycle starts its own worktree from main and can read the work in progress from the branch (`git diff {baseline_sha}...{branch}`); once it has, the user removes the instance's worktree and branch (`git worktree remove {worktree_path}`, then `git branch -D {branch}`, which `-D` needs because the branch never merged).

Don't commit yet otherwise. The commit comes after the doc work, so the checkin goes into it (Step 7).

### Step 6: Audit & doc update

There's no separate Auditor phase here — **you are the Auditor too.** Update the documentation your change affects *now*; do not flag-and-defer. The instance closes the doc-staleness loop itself, in the worktree's index; the merge step's `carry_forward` brings those verifications to main.

1. **Find affected docs.** Run `axiom_graph_check(project_root="{worktree_path}")` and `axiom_graph_drift_query(project_root="{worktree_path}", format="ids", limit=500)`, and compare the ids with the entry ids in `checkin::baseline`: an id not there is one your change made stale; an id already there is pre-existing and stays out of scope. Also cross-reference every category in `.pev/doc-topology.docjson` whose trigger conditions match this change (PRDs, interface specs, ADRs, READMEs, etc.).
2. **Link audit.** Run the shared Link Audit procedure in `${CLAUDE_PLUGIN_ROOT}/templates/link-audit-reference.md` (the three verbs add/repoint/drop, detection, the granularity rule, term families) over the nodes your change touched, reading the project's `Scope` from `.pev/doc-topology.docjson` (`link-audit` section). It works both ways — **add** (prose describing a changed node with no edge) and **repoint / drop** (existing edges at the wrong granularity or noise).
3. **Update the prose.** For each affected doc section (graph-flagged or sweep-found), `axiom_graph_update_section` it so it matches the new code behavior — capability tables, interface specs, examples, lifecycle subsections, anything the change contradicted or now leaves undocumented. `axiom_graph_read_doc(doc_id, outline=True)` lists a doc's section ids and sizes, so you can find the section without reading the bodies. For a partial edit, `axiom_graph_patch_section` returns the edited region, so there's no need to re-read the section to confirm it. An edit verifies only the section's text and never clears `LINKED_STALE` by itself: when the section is `LINKED_STALE` and your edit reconciles it, pass `addresses=[the offenders]` (the `via=` ids from `axiom_graph_drift_query(format="full")`). It clears once every offender is named; the reply's `still LINKED_STALE via: …` line names any left. A doc your change needs that doesn't exist yet is written with `axiom_graph_write_doc(project_root="{worktree_path}", ...)`. In `doc_json`, a section's links are `"links": [{"node_id": "<node id>"}]` or bare node-id strings; every link is a `documents` link, so there is no `type` or `target` field.
4. **Close the staleness loop.** When a section reflects the code and is still stale (no `addresses=` edit cleared it), `axiom_graph_mark_clean` its node. When your change to a *source* node was inconsequential to its dependents — nothing they describe changed (e.g. a test-assertion tweak cascading `LINKED_STALE` across 20 nodes) — `axiom_graph_reverify` the source to clear the rooted cascade in one call instead of enumerating dependents. But skim the cascade first: any dependent that documents the changed aspect gets its prose updated, with `addresses=[<source>]` or its own `mark_clean`, before you blanket the rest (reverify conservatively skips nodes still outstanding via other offenders — reverifies compose, so the last of them to be reverified clears the node — and cascade-cleared nodes carry `[reverify:<source>]` provenance). Several sources go in one `axiom_graph_reverify(node_ids=[...])` call, which clears dependents held only by those sources together. Fix any doc-to-code links the change *moved* (`axiom_graph_add_link` / `axiom_graph_delete_link`). Every one of these calls takes `project_root="{worktree_path}"`.
5. **Propose link changes — HUMAN GATE** (reference: **Gate Payload**). `Gate: link proposals ({instance-id})`; the content is the verb-tagged proposals from the link audit (the reference defines the record shape) with the sections they touch; each proposal is a decision with your recommendation. Apply only what the user approves — inline, since you're the single agent (`add_link` for an add, `delete_link`+`add_link` for a repoint, `delete_link` for a drop). Never bulk-edit links unreviewed — each edge is a deliberate staleness signal, and bloat dilutes LINKED_STALE into noise. (This gate is for *judgment* edits; a link whose target your change mechanically moved/renamed in item 4 is repointed directly, no gate.)
6. **No escalation for doc volume.** However many docs the change touches, you finish the work — doc-drift size is *never* an escalation trigger. (Code scope still escalates per Step 3; documentation does not.)

Record the work in the checkin as you go: what you updated, marked clean and linked goes in `checkin::doc-updates`, and the files you changed in `checkin::changes`.

### Step 7: Finalise the checkin and commit on the branch

The checkin goes into the same commit as the change, so finish it first:

1. **Workflow markers.** If the change touched a function in `axiom_graph_workflow_list(has_steps=true)`, make its step markers match the new behavior (use the `axiom-annotations-markers` skill for the syntax and numbering rules). If the change added an entry point or a function with 3 or more logical phases that should become a workflow, say so in `changes`.
2. **Finish the checkin.** `changes`, `doc-updates` and `friction` are complete; `meta` reads `Status: in review` and `Resume at: Step 8 (review)`. The checkin has no commit line: a commit cannot record its own sha.
3. **Commit on the branch.** Stage by path — the code and test files you changed, the doc files you edited, and the instance's run directory `docs/pev/instances/{instance-id}/` (it holds the checkin, whatever extension `info`'s `docs_extensions` gives it) — never `git add -A`. Write the message (`{slug}: {one-line summary}`) to `.pev-scratch/commit-message.txt` with the Write tool, then commit, one plain command each:

   ```bash
   git add {path} {path} ...
   ```
   ```bash
   git commit -F .pev-scratch/commit-message.txt
   ```

### Step 8: Independent review, then the merge step

An independent Reviewer checks the code and a Doc Reviewer checks the docs, on the branch, in the task's worktree. This replaces the old self-review. You run as a skill in the user's session, not as a `pev:` subagent, so you dispatch them yourself. Then the task lands through the shared merge step.

1. **Write the state file in the worktree.** Write `{worktree_path}/.pev-state.json` with the Write tool:

   ```json
   {
     "cycle_id": "{instance-id}",
     "cycle_doc_id": "{the checkin id}",
     "layout": "instance",
     "worktree_path": "{worktree_path}"
   }
   ```

   The file is the worktree's own: no other run can hold it, so there is nothing to check on main first. Step 4b's `info/exclude` line keeps it out of commits, and it goes with the worktree at cleanup. With this file the doc-scope hook lets `pev:pev-reviewer` write only `checkin::review` and its subsections, and `pev:pev-doc-reviewer` only `checkin::doc-review` and its subsections, and denies every other `pev:` agent; the scope hooks confine both to the worktree, so a call with main's `project_root` is denied.
2. **Dispatch both in parallel** — two `Agent` calls in one message, `subagent_type` `pev:pev-reviewer` and `pev:pev-doc-reviewer`. Both prompts carry:

   ```
   Instance review (layout: instance). Read with the Instance column of your where-to-read table.
   Checkin: {the checkin id}
   Project root: {worktree_path} (the instance's worktree; the change is on branch {branch}, not merged)
   Baseline: {baseline_sha} (the commit the branch started from)
   ```

   Add `git diff --stat {baseline_sha} HEAD` to the Reviewer's prompt. The Doc Reviewer has no Bash, so paste the branch's full diff (`git diff {baseline_sha} HEAD`) into its prompt; when that is very large, paste the stat plus the full diff of every doc and markdown file.
3. **Fix their findings.** First read each reviewer's envelope status. `CONTINUING` means it ran out of budget before finishing: dispatch it again with the same prompt plus a line naming the passes its section already records as done, and wait for that run before fixing. `NEEDS_INPUT` means it needs an answer only the user has: relay it as a gate (reference: **Gate Payload**), then dispatch it again with the answer and the passes it has done. Once both have returned a final verdict (`PASS`, `PASS_WITH_CONCERNS` or `FAIL`), read `checkin::review` and `checkin::doc-review`. Fix each finding yourself, on the branch — code and tests per Step 5, docs per Step 6 — and record the fix in `changes` or `doc-updates`. A finding you decide not to fix gets a line in `changes` saying why, and goes in your Step 9 summary. Re-dispatch a reviewer only after a non-trivial fix (a logic change, a new or rewritten test, a rewritten doc section), and at most twice per reviewer before asking the user. Commit the fixes on the branch before the re-dispatch, as in item 4 (staged by path, `{slug}: review fixes`), so the `{baseline_sha} HEAD` diffs pasted into the re-review prompts contain them.
4. **Commit the review on the branch.** Set `meta` to `Status: merging` and `Resume at: Step 8.5 (merge step, step 1)`. Stage the fixed files not yet committed and the run directory `docs/pev/instances/{instance-id}/` (the checkin, with both reviewers' sections) by path, write the message (`{slug}: review fixes`, or `{slug}: review` when nothing needed fixing) to `.pev-scratch/review-message.txt`, and commit with `git commit -F .pev-scratch/review-message.txt`.
5. **The merge step.** Follow `${CLAUDE_PLUGIN_ROOT}/templates/merge-step-reference.md` as written, with these inputs:

   | Input | Instance value |
   |---|---|
   | `{run_id}` | the instance id |
   | `{run_kind}` | `instance` |
   | `{run_doc_id}` | the checkin id (`cycle_doc_id` in the state file) |
   | `{baseline_section}` | `{the checkin id}::baseline` |
   | `{run_dir}` | `docs/pev/instances/{instance-id}/` |
   | `{worktree_path}`, `{branch}`, `{main_branch}`, `{main_repo_path}`, `{baseline_sha}` | the `Worktree:`, `Branch:`, `Main branch:`, `Main repo:` and `Baseline SHA:` lines of the checkin's `meta` |

   It runs the suite in the worktree, leaves it, merges main into the branch when main moved, records main's pre-merge ids in `checkin::baseline`, lands the branch with `--no-ff`, builds and carries the worktree's verifications to main, holds the residue gate, writes the completion commit and the checkpoint, and removes the worktree and the branch. The instance's own parts are in the procedure: the merge commit's `PEV Instance:` and `Review:` lines, `verified_by="agent:pev-instance"` at the residue gate, and at the completion commit the checkin's `meta` set to `Status: done` with the `Final check:` line, its tags flipped to `["pev-instance", "completed"]`, and the efficiency report.

   **Efficiency report.** It is written at the completion commit, on main, so it counts the merge step's calls too. The procedure runs `analyze_pev_session.py --find-run {instance-id}`: it reports the sessions that worked this instance, and only their calls for it. Your own calls in the main session count as the instance's, alongside the two reviewers'. The report lands in the run directory as `efficiency` (`efficiency-s2`, `-s3`… when the instance spanned several sessions). Its only tag is `pev-efficiency`, so the open-instance search never finds it. If the script warns that a session came from a newer Claude Code version than it was checked against, mention it in your Step 9 summary: empty per-agent tool lists then mean the log format changed.

   Every gate the procedure holds (a failing suite, a conflict, residue) takes the **Gate Payload** shape, and rewrites the checkin's `Resume at:` line first. The state file needs no removal: it goes with the worktree.
6. **The next task.** The session is on main again. When the request has another task, go back to Step 3 for it: it gets its own scope check, worktree, checkin and merge. Otherwise return (Step 9).

### Step 9: Return

One block per task the run worked, in order:

```
PEV-INSTANCE {status}

Slug: {slug}
Commits: {change}, {review}, {merge}, {complete} (or the WIP commit, when the task stopped)
Checkin: {the checkin id}
Review: Reviewer {status}, Doc Reviewer {status}; {N findings fixed, M declined}
Docs: {N sections updated, M nodes marked clean — or "none affected"}
Carry: {the real carry's report line: carried in full / in part; Stale here before -> after}
Residue: {id: decision, ... — or "none"}; {the Final check: line}
Efficiency: {efficiency doc id(s)}
Duration: {minutes} min

{Brief summary of what was done, and any declined finding with its reason}
```

When the run stopped before its last task, end with a `Not started: {tasks}` line.

Status codes match full PEV:

| Status | When |
|---|---|
| `DONE` | Every task implemented, reviewed, merged through the merge step and completed |
| `CONTINUING` | Budget or maxTurns cutoff mid-work; the checkin's `Resume at:` says where to pick up |
| `BLOCKED` | Need user input on something that wasn't a simple clarification — same meaning as in `/pev-cycle` |
| `NEEDS_INPUT` | Proxy-question protocol (same shape as full PEV — return NEEDS_INPUT JSON payload) |
| `ESCALATED` | Task was bigger than /pev-instance — see the Step 3 escalation and the Step 5 hard line |

## Friction log

Capture friction as you work — tool output that didn't fit the task, instructions or SOP items that didn't match the actual situation, the pitch you wrote yourself that turned out underspecified, effort disproportionate to the value of the task, etc. The list isn't exhaustive — surface whatever felt off, even if it's not one of these shapes. Keep running notes in conversation as you notice things; the specifics (the exact tool output, the unclear instruction, the moment you had to guess) are gone by Step 7.

**Log every script read.** If you read a document or index data with a script (Python, Node, `jq`, `grep` … over DocJSON files, or raw SQL) instead of an axiom-graph tool, add a friction entry tagged `script-read`. Name the tool you would have used and why it fell short: output too large, no way to select part of a section, search missed it, not in the index, output hard to reuse. Reading this way is allowed. Writing a document this way is not. These entries are how gaps in the tools get found and fixed.

At Step 7, fold those observations into the checkin's `friction` section using the format below. If nothing pinched, leave the section empty — honest emptiness beats invented friction. The reviewers add their own friction under their sections.

Entry format:

```
- **{short tag}** — {one line: what felt off}
  Context: {raw paste — tool call, output, instruction fragment, error}
  Wish: {optional — what would've made this easier}
```

## Constraints

- **One author.** You plan, build and do the doc upkeep yourself. The only agents you dispatch are the Reviewer and the Doc Reviewer, at Step 8.
- **A worktree per task.** Each task runs in its own worktree, created from local HEAD, and lands through the shared merge step before the next task starts. The worktree is the safety net: nothing reaches main before the merge step, and uncommitted work on main never mixes into the change.
- **Independent review.** Your work is checked by the Reviewer and the Doc Reviewer, not by you. They report; you fix.
- **You are the Auditor too.** Unlike the full cycle, doc upkeep isn't a separate phase — you update the docs your change affects (Step 6), and the Doc Reviewer checks them (Step 8). Doc drift is *fixed*, not flagged. Doc-drift volume is never an escalation trigger; you always finish the doc work, however large it turns out.
- **One checkin doc.** No architect, builder or review docs: the checkin IS the record.
- **Escalate proactively.** If the task grows, the Step 5 hard line stops it. A `/pev-instance` that silently became too big is worse than one that stopped early.

## Notes

- An open instance's checkin is on its worktree's branch until the merge step lands it on main; **Resuming an open instance** says where to look.
- Checkins live in `docs/pev/instances/{instance-id}/checkin`, parallel to the cycle directories under `docs/pev/cycles/`; older instances are single files, `docs/pev/instances/{instance-id}`. Both are searchable via `axiom_graph_search`. Over time, the instance history becomes a searchable "small work we did" archive — useful for spotting patterns or finding prior similar fixes before starting new work.
- The full `/pev-cycle` orchestrator can (optionally) scan recent instances during its intake phase to see if a similar task was already done. Not implemented yet; natural future extension.
