# PEV Orchestrator Reference

## Naming Conventions

Cycle ID format: `pev-YYYY-MM-DD-{slug}`

- Date: today's date
- Slug: derived from user request — lowercase, hyphens only, truncated to 40 chars
- Example: `pev-2026-03-21-add-history-filtering`

Collision check. A cycle is either a directory `docs/pev/cycles/{cycle-id}/` or, for cycles created before plugin 3.0, a single file `docs/pev/cycles/{cycle-id}.docjson`. Check both, and never treat `docs/pev/cycles/efficiency/` (the efficiency reports) as a cycle:
```bash
ls docs/pev/cycles/
```
Read the listing yourself (a missing directory means no cycles yet), skipping `efficiency`. A match on either `{cycle-id}` or `{cycle-id}<ext>`, for any `<ext>` in `info`'s `docs_extensions`, is a collision: append `-2`, `-3`, etc.

Audit and instance ids follow the same rule. An audit run is a directory `docs/pev/audits/{audit-id}/` holding its manifest `audit` (`{audit-id}` is `pev-audit-{dev-docs|consumer-docs|annotations}-YYYY-MM-DD-{slug}`); an instance is a directory `docs/pev/instances/{instance-id}/` holding its `checkin` (`pev-instance-YYYY-MM-DD-{slug}`). Each is cloned from its seeded template (`.pev/templates/audit-*`, `.pev/templates/instance`). A new id is refused when the file `{id}<ext>`, for any `<ext>` in `info`'s `docs_extensions`, or the directory `{id}/` already exists, since runs made before plugin 3.0 are single files. Lookups of past runs accept both shapes and skip every `efficiency` doc. Requests stay single docs, `docs/pev-requests/{slug}`, cloned from `.pev/templates/request`.

Worktree path: `.claude/worktrees/{cycle-id}`

Branch name: `worktree-{cycle-id}`

**Cycle directory and doc ids.** A cycle is seven docs under `docs/pev/cycles/{cycle-id}/`: `manifest`, `architect`, `decisions`, `builder`, `review`, `audit` and `friction`. This reference writes `{cycle_dir}` for the shared prefix `{project_id}::docs/pev/cycles/{cycle-id}`, so the docs are `{cycle_dir}/manifest`, `{cycle_dir}/architect` and so on, and a section is `{cycle_dir}/<doc>::<section>` (e.g. `{cycle_dir}/architect::tasks.task-3`). `cycle_doc_id` is the manifest's id, `{cycle_dir}/manifest`. Do NOT build the ids by hand: each `axiom_graph_clone_doc` result names the id the doc was indexed under (see Cycle Creation), and those values are the ones to use. Section ids inside a doc are slugs with no dots; a dot in a section path means nesting (`tasks.task-3` is `task-3` under `tasks`). `{project_id}` is always the project id `axiom_graph_info(project_root)` reports, and doc extensions are its `docs_extensions`; never write a literal project id, docs folder or extension into an id or path.

## Cycle Creation

The orchestrator creates the cycle as **seven `axiom_graph_clone_doc` calls**, one per doc, from the project's seeded templates at `.pev/templates/cycle/`. Cloning copies each template's sections byte for byte, so every section a downstream agent writes to exists before it runs. Do NOT hand-write the docs with `axiom_graph_write_doc`, and do NOT copy template files on disk: a raw file is a DocJSON edit the build flags and never verifies.

**Source ids.** The seeded templates are indexed project docs: `{project_id}::.pev/templates/cycle/manifest`, `.../architect`, `.../decisions`, `.../builder`, `.../review`, `.../audit`, `.../friction`. The project id is the one `axiom_graph_info(project_root="{worktree_path}")` reports (intake calls `info` before any other axiom-graph call); list the seven with `axiom_graph_read_doc(project_root="{worktree_path}", doc_id="list", prefix="{project_id}::.pev/templates/cycle/")`.

**Missing template: halt.** If `info`'s `docs_dirs` lacks `.pev` (then all seven count as missing), or any of the seven is not in the index, stop before creating anything and tell the user:

```
The PEV cycle templates are not seeded in this project (missing: {names}).
Run: bash "${CLAUDE_PLUGIN_ROOT}/scripts/pev-seed.sh" --project-root {main_repo_path} --axiom-graph "{commands.axiom_graph}"
The script first makes .pev a docs root: with no axiom-graph.toml it creates one
(project_id = the id your index stores, docs_dirs = ["docs", ".pev"]), and with no
[axiom_graph.scan] table it appends one; if your toml's docs_dirs lacks .pev it prints
the exact edit and stops. Make that edit and re-run it.
It builds the index when it is done. Then commit axiom-graph.toml and .pev/, and re-run /pev-cycle.
```

The worktree is already created at this point; leave it for the user's re-run or remove it with `ExitWorktree(action="remove")`. There is no fallback: never improvise a doc the templates don't define.

**Old manifest template: halt.** When the manifest clone fails with `unknown in set_sections: baseline`, the project's seeded manifest template predates the `baseline` section, and the seed script never overwrites a seeded file. Nothing is created yet: remove the worktree with `ExitWorktree(action="remove")` and tell the user:

```
The seeded PEV templates in this project predate this plugin version.
On main, delete .pev/templates/cycle/manifest, .pev/templates/cycle/review and
.pev/templates/instance (each with the extension it was seeded with), then
run: bash "${CLAUDE_PLUGIN_ROOT}/scripts/pev-seed.sh" --project-root {main_repo_path} --axiom-graph "{commands.axiom_graph}"
It copies the current templates and builds the index. Then commit axiom-graph.toml and .pev/,
and re-run /pev-cycle. The upgrade note in the plugin's CHANGELOG.md has the details.
```

**The seven calls** (`project_root="{worktree_path}"` in each):

```
axiom_graph_clone_doc(
  project_root="{worktree_path}",
  source_doc_id="{project_id}::.pev/templates/cycle/manifest",
  new_id="pev/cycles/{cycle-id}/manifest",
  title="PEV Cycle: {cycle-id}",
  tags=["pev-cycle", "in-progress"],
  set_sections={"status": "{Phase, layout, baseline SHA, Main branch and Main repo lines, timestamp, cycle ID, entry baseline, Structural and Main failing tests lines, the Detected commands: line when a command was detected, and Resume at: Phase 2 (Architect)}",
                "request": "{the user's /pev-cycle prompt, verbatim}",
                "baseline": "{Entry baseline (worktree): counts and Main context, as in status\nEntry stale ids (worktree): the entry check's drift_query ids, or none}"}
)
axiom_graph_clone_doc(project_root="{worktree_path}",
  source_doc_id="{project_id}::.pev/templates/cycle/architect",
  new_id="pev/cycles/{cycle-id}/architect", title="PEV Cycle {cycle-id}: Architect")
```

and the same for `decisions`, `builder`, `review`, `audit` and `friction`, with titles `PEV Cycle {cycle-id}: Decisions` and so on. Only the manifest call passes `tags`: it replaces the template's `["pev-cycle"]` with `["pev-cycle", "in-progress"]`, so the manifest is the one doc per cycle that carries a status tag, and a search for in-progress cycles finds exactly one doc per cycle. The other six inherit `["pev-cycle"]`, a frozen tag (the seed script lists it in `[axiom_graph.staleness] frozen_tags`), so the cycle's docs never count in `axiom_graph_check`.

The manifest result's id is `cycle_doc_id`; store it, as printed, in `.pev-state.json`. A sharded review adds one more doc per shard, `review-shard-{x}`, cloned from the same `review` template at Phase 5 (see Sharded Review).

The manifest's `baseline` gets its entry lines here, from the entry check the intake ran before cloning: the counts as recorded in `status`, and the worktree's stale node ids from `axiom_graph_drift_query(project_root="{worktree_path}", format="ids")`, paged while its header says more rows remain. The merge step appends main's pre-merge and final lines to it later. `fix-list` stays as the template has it until the first approved fix round.

**Do NOT add, rename, drop or summarize template sections per cycle.** If the workflow needs a section the template lacks, fix the plugin template (and re-seed), so every cycle stays consistent. Sections added after creation are the agents' own entries: `decisions::d-N`, friction entries, `builder::inc-N...`, `review::pass-N`, and `architect::tasks.task-N`; and the orchestrator's `manifest::fix-list.round-N`, one per approved fix round.

**Old cycles.** A cycle created before plugin 3.0 is one file, `docs/pev/cycles/{cycle-id}.docjson`, and its `.pev-state.json` has no `layout` field. It finishes on the layout it started with: never migrate it. See **Legacy Layout** at the end of this reference for the differences.

## State File

Write `.pev-state.json` to the **worktree root** (the cwd after `EnterWorktree`) once per cycle, before the first subagent dispatch. The doc-scope, axiom-graph-scope, and worktree-scope hooks all find this file by reading the `cwd` field from their input and locating `.pev-state.json` at that root. It is rewritten once more, at plan approval, to add `builder_docs` (see Plan-Gate Deliverables); otherwise it does NOT change between phases — hooks dispatch per-agent behavior on the `agent_type` field in hook input, not on state.

This one file serves every role, the Auditor and the Doc Reviewer included: they run in the worktree like the Builder and the Reviewer. There is no state file on main and no Auditor mutex, so parallel cycles audit in parallel. Main is written only by the merge step (`merge-step-reference.md`), which the orchestrator runs itself after leaving the worktree.

Format:
```json
{
  "cycle_id": "{cycle-id}",
  "cycle_doc_id": "{cycle_doc_id}",
  "layout": "directory",
  "worktree_path": "{absolute-path-to-worktree}",
  "builder_docs": ["{doc id}", "..."]
}
```

- `builder_docs`: written at plan approval, only when the pitch lists doc deliverables for the Builder: every such doc id, as indexed. The doc-scope hook lets `pev:pev-builder` write those docs' sections; every other role, and every other doc, keeps its old rule. Leave the field out when there are none.

- `cycle_doc_id`: the full axiom-graph doc ID of the cycle's `manifest` doc, `{project_id}::docs/pev/cycles/{cycle-id}/manifest`, copied from the manifest's `axiom_graph_clone_doc` result. All dispatch prompts and hooks use this value; the doc-scope hook derives the cycle directory from it.
- `layout`: `"directory"` for every cycle this plugin creates. The doc-scope hook applies per-doc ownership and the append-only rule only when it is present. A pre-3.0 cycle's state file has no `layout` and keeps the old single-manifest rules (see Legacy Layout); when you rewrite the state file for such a cycle, leave the field out.
- `worktree_path`: absolute path to the worktree created in Phase 1. Every role runs with it — hooks use it to scope Write/Edit, Bash, and axiom-graph `project_root` calls, so an agent's call with main's `project_root` is denied.
- Tool-budget counters are keyed on the subagent's `agent_id` (read by hooks from stdin JSON) at `/tmp/pev-counter-<agent_id>.txt`. Files are auto-created on first increment and auto-deleted by the `SubagentStop` hook. No counter_file field needed in state.
- Use the Write tool to create this file.
- The file is never committed. Phase 1 adds `.pev-state.json` to the repository's shared `info/exclude` (see Worktree Commands), so it is never staged and doesn't block `git worktree remove`.

## Project Commands

Agents take the project's commands from the `[commands]` table of `.pev/sops.toml`. The user writes that file by hand (setup.md has an example); no PEV agent writes it, because a write from inside a worktree would ship it in the feature commit.

| Key | Used for |
|---|---|
| `test` | the full suite, serial |
| `test_parallel` | the full suite in one parallel run, when set (e.g. pytest-xdist's `-n auto`) |
| `test_targeted` | one test file or test id; `{target}` is replaced with it |
| `test_expected_seconds` | how long a serial full run takes, for planning the run |
| `install` | worktree provisioning (Phase 1) |
| `axiom_graph` | the axiom-graph CLI, for the commands with no MCP tool (e.g. `history checkpoint`) |

**Missing file or key.** The orchestrator detects the command once for this cycle (the project's package manager and test runner, read from its manifest and lockfile) and records it on a `Detected commands:` line in `manifest::status`, e.g. `Detected commands: test=uv run pytest -q; test_targeted=uv run pytest -q {target}`. Agents read that line when the toml lacks a key. An agent that finds neither detects the command itself, uses it, and names it in its envelope summary so the orchestrator records it.

**Running the full suite.** When `test_parallel` is set, run it as one foreground Bash call (timeout up to 600000 ms). Otherwise, if `test_expected_seconds` is over about 540, split the suite into two halves by test path and run each as its own foreground call under the 600 s limit. A run that has to go to the background is waited on with one blocking `Monitor` call, never a `sleep`/`tail` loop, and no agent returns while a run it started is still in flight. The full suite runs once per task, not after every edit; use `test_targeted` in between.

## Worktree Commands

The orchestrator creates the worktree in Phase 1 (Intake), before any subagent is dispatched. Every role runs inside it: the Builder and the Reviewer, then, after Phase 6 syncs main into the branch, the Auditor and the Doc Reviewer. The session stays in the worktree until the merge step's `ExitWorktree` (`merge-step-reference.md`), so every main-side command is placed before `EnterWorktree` or after `ExitWorktree` (see **Shell Command Shapes**).

**Capture main's facts first (Phase 1, before `EnterWorktree`).** Four Bash calls, one command each, run from the main checkout:
```bash
git rev-parse HEAD
git rev-parse --abbrev-ref HEAD
git worktree list
git ls-files --others --exclude-standard
```
That is the baseline SHA, `{main_branch}`, `{main_repo_path}` (the first entry of the list) and main's untracked files (for the entry check's structural class below). Record `Main branch: {main_branch}` and `Main repo: {main_repo_path}` in `manifest::status` once the cycle exists; the merge step takes both as inputs.

**Create worktree (Phase 1 — Intake):**
```
EnterWorktree(name="{cycle-id}")
```
Creates `.claude/worktrees/{cycle-id}/` with branch `worktree-{cycle-id}` based on HEAD. Moves session cwd to the worktree.

**Verify worktree base (immediately after EnterWorktree):**
`EnterWorktree` may base the new branch on the remote tracking branch (e.g., `origin/main`) instead of local HEAD. This means the worktree starts from the remote state, missing any local commits not yet pushed.

```bash
git rev-parse HEAD
```

Compare the output against the baseline SHA captured before `EnterWorktree`:
- **Equal:** nothing to do.
- **HEAD is an ancestor of the baseline** (the remote is behind local main). Check, then fast-forward, as two calls:
  ```bash
  git merge-base --is-ancestor HEAD {baseline_sha}
  git merge --ff-only {baseline_sha}
  ```
  The first exits 0 when HEAD is an ancestor.
- **Anything else** (the remote is ahead of local main, or the two have diverged): stop and tell the user that local main is behind or diverged from origin, and let them decide. Never rebase or reset here: a fresh worktree has no commits of its own, so a fast-forward is the only move that cannot hide commits.

Setting `"worktree": {"baseRef": "head"}` in the project's `.claude/settings.json` makes `EnterWorktree` base new worktrees on local HEAD, so this check is normally a no-op.

**Create the scratch folder (immediately after base verification):** use the Write tool to create `{worktree_path}/.pev-scratch/.gitignore` with the single line `*` (Write creates the folder). The `.gitignore` of `*` makes the folder ignore itself: nothing in it is committed, and the consumer's `.gitignore` is not touched. It is removed with the worktree. Builders put scratch files here (see the Builder skill's Constraints).

**Exclude the state file (immediately after the scratch folder):**
```bash
git rev-parse --git-path info/exclude
```
This prints the path of the exclude file. It is in the repository's common git directory, so every worktree and the main checkout share it. Read it (it may not exist yet). If no line in it is exactly `.pev-state.json`, append one with a single command, the path quoted as printed:
```bash
printf '\n.pev-state.json\n' >> "{exclude_path}"
```
The leading newline keeps the entry on its own line when the file doesn't end with one. Never rewrite or reorder the file: it may hold the user's own entries, and another cycle may be reading it. With the line in place, the worktree's state file is never staged and doesn't block `git worktree remove`.

**Install dependencies to parity with main — for both scanning AND testing:**
Provision the worktree so it (1) scans every node exactly as main does *and* (2) runs the full test suite as main does (the Builder's TDD and the Reviewer's Pass 0 both execute it). Install the project's dependencies + any optional extras/groups the scanners need (language parsers, optional imports) + the test/dev groups. Run `commands.install` (see **Project Commands**), one plain command:
```bash
{commands.install}
```
The requirement is parity, not a specific tool, and not every project has extras. When the key is missing, the detected command (the project's package manager with the extras and groups main has) is the one recorded on the `Detected commands:` line.

**Why parity matters:** a missing *scanner* dependency makes whole node classes flip `NOT_FOUND`/`CONTENT_UPDATED` (e.g. language-module nodes when the parser extra is absent) and injects environment noise into the cycle's staleness — a missing language-parser extra can flip hundreds of nodes where main shows a dozen; that gap must be *classified* by the entry baseline check below, not silently absorbed as drift. A missing *test-only* dependency won't show as a stale node — it surfaces only when a test can't run (see **Env-gap recovery** below). Install the project in editable mode (not a `--no-root`-style deps-only install) so console scripts and import paths match a developer's local checkout.

**Note:** A per-worktree environment is created and is not auto-cleaned when the worktree is removed. Periodically clean up stale environments from the main repo with your package manager's own commands.

**Install frontend deps (if the project has a JS/TS package):** when `commands.install` doesn't cover it, check the package's `package.json` exists (Glob or Read), then run the package manager with its directory flag, one plain command, e.g.:
```bash
npm install --prefix {package_dir}
```
**Don't `cd` in a provisioning step.** A bare `cd` persists for every later Bash call in the session, so later commands run from a directory they did not choose. Use the tool's own directory flag (`npm --prefix`, `git -C`) instead — see **Shell Command Shapes** below.

**Copy axiom-graph DB into worktree:**
```
axiom_graph_checkout(
  project_root="{main_repo_path}",
  worktree_path="{worktree_path}"
)
```

`{main_repo_path}` = the main working tree path from `git worktree list`.
`{worktree_path}` = absolute path to `.claude/worktrees/{cycle-id}`.

**Rebuild the copied index in the worktree:**
```
axiom_graph_build(project_root="{worktree_path}")
```
The snapshot describes main's working tree; the build re-indexes it against the worktree's own files. The order is checkout, then build, then the entry check.

**Entry baseline check (left bracket):**
After provisioning, `axiom_graph_checkout` and the build, run the check in the worktree:
```
axiom_graph_check(project_root="{worktree_path}")
axiom_graph_drift_query(project_root="{worktree_path}", format="ids", limit=500)
```
**This worktree result is the left bracket.** Record the counts in the manifest `status` section and, with the ids (`drift_query` paged while its header says more rows remain), in `manifest::baseline` (see Cycle Creation); Phase 6's pre-audit check subtracts it. Take it once, after the build and before any Builder work, so the delta between the two brackets is the cycle's.

Main's counts, captured before provisioning, are context for classifying the worktree's baseline rather than a target it must match. `check` picks the files to re-hash by their content, not their modification time: a file whose bytes match what the index last hashed is skipped whatever its mtime. A checkout resets every mtime, but that alone does not make the worktree read dirtier than main. A gap between them comes from files whose bytes differ (edits on main not yet committed or built), or from one of the three classes below.

Sort any worktree-vs-main gap into one of three outcomes:

| Class | What it is | Action |
|---|---|---|
| **Env divergence** | A missing scanner dependency flips whole node classes — e.g. every module node for a language whose parser extra is absent. | **Block.** Install the dependency, `axiom_graph_build`, re-run. |
| **Pre-existing debt** | Latent staleness main is masking behind the fast-pass. Provisioning does not touch it, and it surfaces on main too once those files are touched. | **Record and subtract, then proceed.** Name the mechanism in the manifest. |
| **Structural** | Paths untracked in main that a worktree cannot materialize. | **Note and proceed.** |

Fewer `LINKED_STALE` than main is expected and is not a divergence (link fan-out materializes fully only on main).

**Subtract files untracked on main.** Main's index holds nodes from files that exist on main's disk but were never committed, and a worktree created from a commit never has them, so each reads `NOT_FOUND` in the worktree. Take them from the `git ls-files --others --exclude-standard` listing captured before `EnterWorktree` (above); from inside the worktree that command would list the worktree's files, not main's. Every `NOT_FOUND` node whose file is on that list is structural: subtract it from the bracket and record the count in the manifest (`Structural: {n} NOT_FOUND in files untracked on main`).

**Main's failing tests.** Record a `Main failing tests:` line in the manifest `status` section, listing the failing test ids or `none`. Take them from the project's latest full-suite result for the baseline commit if there is one (CI, or a run on main at that commit). Otherwise run the full suite once in the worktree now, before any Builder work: the worktree is still the baseline commit, so its failures are main's. The Builder and the Reviewer read the line, so a test that already failed on main is not taken for a regression the cycle caused.

**Telling env divergence from pre-existing debt.** Env divergence responds to provisioning: install the dependency, rebuild, and the class disappears. Pre-existing debt does not. Confirm debt by naming the mechanism — which scanner or dispatch path cannot re-derive those nodes — and record that mechanism alongside the count.

**Diagnose by reading the hashing path, not by re-measuring.** `CONTENT_UPDATED` is sticky: `mark_clean` and `reverify` retire it, its cause going away does not. Once nodes have flipped, later `axiom_graph_check` calls return the same count regardless of what changed in between, so two different tree states read identically. Settle the cause by reading the scanner's hashing path and recomputing a hash by hand. Keep diagnostics read-only — commands that rewrite the working tree (`checkout-index`, re-checkouts, bulk file rewrites) replace the evidence being measured.

**Env-gap recovery (test parity):** if the Builder or Reviewer later reports a test that *couldn't run* because a dependency main declares is missing (vs. a test that *failed*), install that dependency to restore parity, rebuild the worktree index (`axiom_graph_build`), and continue (re-dispatch the Reviewer if it was its Pass 0). Do NOT install a dependency main doesn't declare — a brand-new library a test needs is the Builder's to add to `pyproject`/lockfile and is reviewed like any other change.

**Check for stale worktrees:**
```bash
git worktree list
```

## Shell Command Shapes

Every PEV role runs Bash the same way, so commands match the permission rules without a prompt and behave the same on Windows Git Bash, macOS and Linux:

- **One plain command per Bash call.** No `&&`, `;` or `||` chains, no `cd x && …`, no heredocs, no `$?`, `$(…)` or process substitution, no `if …; then` blocks.
- **Use the tool's directory flag, not `cd`.** `git -C <path>` when targeting a directory other than cwd (plain `git` when cwd is already the target), `npm --prefix <dir>`. A bare `cd` persists for every later call.
- **No main-side git from inside the worktree.** A session isolated in a worktree cannot run `git -C {main_repo_path} ...`: the harness refuses it ("a worktree-isolated session's git operations must target its own worktree"). Main-side git runs before `EnterWorktree` (Phase 1 intake) or after the merge step's `ExitWorktree`, never in between. Inside the worktree, main is still readable through its branch, since local branch refs are shared: `git diff {baseline_sha} {main_branch} -- {path}`, `git merge-base --is-ancestor {main_branch} HEAD` and `git merge --no-edit {main_branch}` all run from the worktree. A file on main's disk is read with the Read tool, or with `git diff --no-index`, which compares two paths and touches no repository. MCP tools are not git: `axiom_graph_check(project_root="{main_repo_path}")` works from inside the worktree.
- **Multi-line work is a script.** Write it to `.pev-scratch/` with the Write tool and run it by path (`python3 .pev-scratch/fix_ids.py`, through the project's runner where it has one). Never name a scratch file after a standard-library module (`json.py`, `re.py`, `test.py`): the interpreter imports it in place of the module. A Python script that writes text files uses `write_text(..., newline="\n")` so Windows doesn't turn the line endings into CRLF.
- **Files for git are written without a BOM.** Write commit-message files with the Write tool, never PowerShell 5.1's `Set-Content`/`Out-File`. Don't use PowerShell 5.1's `2>&1` either: it wraps every stderr line in an error record.
- **Quiet output.** `-q --tb=short` for pytest, `git diff --stat` before a full diff, `--exclude-dir=node_modules` on a recursive grep.

## Pre-Audit Check and Change-Set

Phase 6, in the worktree, after the review verdict and the sync of main into the branch (`git merge --no-edit {main_branch}`, see the skill's Phase 6), before the Auditor runs. The merge itself comes later, in the merge step (see **Merge Step**).

**Pre-audit check (right bracket — gates the Auditor's blanket-clean and informs the merge gate):**
Run `axiom_graph_check(project_root="{worktree_path}")`, the same mechanism as the Phase 1 entry check with the expected set shifted by the cycle's change-set. Classify each stale node as **explained** (entry baseline + change-set: own-`CONTENT_UPDATED` for changed nodes, `LINKED_STALE` cascading from them, plus the nodes the sync brought in from main, which main has already verified) or **unexplained** (staleness neither can account for — a returning env divergence, say). Surface any unexplained drift at the merge HUMAN GATE; carry the verdict (`clean` or `unexplained-drift` + nodes) into the `change-set` section. A `clean` verdict plus a passing Reviewer verdict (`PASS`/`PASS_WITH_CONCERNS`) is what licenses the Auditor's blanket-clean. The worktree under-materializes `LINKED_STALE` vs main — that floor is expected; the merge step's carry settles main, and what it can't settle goes to its residue gate.

**Get changed files for change-set** (three dots: the branch's own changes since it last took in main, so main's changes brought in by the sync are left out):
```bash
git diff --name-only {main_branch}...HEAD
```

**Get the sync list** (main's own changes since the branch's merge base, the files the sync brings in). Run it before the sync's `git merge`: afterwards HEAD contains main and the list is empty. It goes in the change-set's `Brought in by the sync` block (`none` when main had not moved):
```bash
git diff --name-only HEAD...{main_branch}
```

**Record main's stale ids at the sync** whenever the sync list is not `none`. They come from main's index through the MCP tool, called from the worktree session (not git, so no `git -C`), paged while the header says more rows remain:
```
axiom_graph_drift_query(project_root="{main_repo_path}", format="ids", limit=500)
```
They go in the change-set beside the sync list, as `Main's stale ids at the sync: {ids, or none}`. The Auditor's sync-explained batch leaves them out (its rule is in the Auditor (initial) prompt under **Dispatch Prompts**): an id already stale on main is main's own drift, and a clear in the worktree would carry to main with no one deciding it, so the Auditor decides it per node.

**Files outside the pitch.** At the merge gate, list separately every changed file that is not in the pitch's affected list (`architect::affected-nodes` and the solution sketch's affected files), leaving out test files and the cycle directory. Give each the Builder's recorded deviation for it (from the task manifests' `deviations` and `decisions`), or `no deviation recorded`.

## Plan-Gate Deliverables

Phase 3, on approval, before the status update (R1). The Builder may write a doc that the approved pitch lists as a Builder deliverable; the orchestrator opens that door and creates any such doc that doesn't exist yet.

1. Read `{cycle_dir}/architect::required-artifacts`. Its Builder deliverables list doc deliverables one per line, by doc id or path. Doc work the pitch gives the Auditor is not a Builder deliverable.
2. For each one, check it is indexed in the worktree: `axiom_graph_read_doc(project_root="{worktree_path}", doc_id="{doc id}", outline=true)`.
3. Create each missing doc as a skeleton in the worktree: `axiom_graph_write_doc(project_root="{worktree_path}", doc_json={...})` with the doc's id and title and one placeholder section per section the pitch names (one placeholder section when it names none). Take its id from the write result. The Builder fills the sections; it cannot create docs.
4. Rewrite `.pev-state.json` with the Write tool: the same fields, plus `"builder_docs": [...]` holding every deliverable id, existing and new.

The Builder still has no `write_doc` or `add_link`. Links on a deliverable's sections stay Auditor proposals. The doc files are committed with the Builder's work; Phase 6's straggler commit stages them by path if they aren't.

## Request Addenda

A request that is a doc or file can change on main while the cycle runs. Its path is on the `Request file:` line of `manifest::status`.

**Re-diff at every gate up to the merge** (Phase 3, a Phase 4 BLOCKED or NEEDS_CONTEXT stop, Phase 5's verdict, a Phase 6 or 7 gate, Phase 8's merge gate), before presenting the gate. The session is inside the worktree at every one of them, so the re-diff never runs `git -C` against main (see **Shell Command Shapes**). Two calls, run from the worktree:
```bash
git diff {baseline_sha} {main_branch} -- {request path}
```
```bash
git diff --no-index {request path} {main_repo_path}/{request path}
```
The first diffs the shared branch ref, so it shows what main committed to the request since the baseline. The second compares the worktree's copy of the request (the baseline's, or the last sync's) with the file on main's disk, so it shows edits main holds uncommitted; it exits 1 when the files differ, which is not an error. Text already recorded as an addendum in `manifest::request` is not new. When there is new text, show it at the gate and ask: fold it into this cycle, or leave it for a follow-up?

**Folding it in:**
- **At the plan gate:** append it to `manifest::request` (below) and redispatch the Architect with it as revision feedback.
- **At any later gate, the addendum loop:**
  1. Append the new text verbatim to `{cycle_dir}/manifest::request`: `axiom_graph_patch_section(section_id="{cycle_dir}/manifest::request", anchor="$", new_string="Addendum {n} ({date}, {request path}):\n{text}")`. Add a `decisions::d-N` (Orchestrator) entry saying it was folded in at which gate.
  2. Dispatch the Architect (addendum) prompt (see Dispatch Prompts).
  3. HUMAN GATE on the delta only: the revised and new tasks, stories and test-plan rows. Rerun Plan-Gate Deliverables if the deliverable docs changed.
  4. Builder dispatches for the reopened and new tasks, in task order, each with the usual brief plus the reopened line (see Dispatch Prompts).
  5. Reviewer (re-review) naming the addendum, then back to the verdict gate, where the request is re-diffed again.

  An addendum loop doesn't count toward the max-2 review-fix loops.

**After the merge** an addendum never reopens the cycle. Add `Addendum after merge: {request path} changed, left for a follow-up` to `manifest::status` and tell the user at Phase 9.

A user who states a change in words at a gate, with no request file, goes through the same loop; record their words as the addendum text.

## Dispatch Prompts

**Architect (initial):**
```
You are the PEV Architect for cycle {cycle_id}.

Cycle manifest doc ID: {cycle_doc_id}
Project root: {worktree_path}

User request:
{user request verbatim}

The cycle is the directory {cycle_dir}/ (layout: directory). You own the `architect` doc and `manifest::scope`; add decisions as `decisions::d-N` entries and friction under `friction::architect`.

Read the manifest's `request`, explore the codebase, engage with the user (brainstorm if appropriate), and write your plan to the `architect` doc. Follow your skill instructions.
```

**Architect (revision):**
Append: `REVISION REQUESTED. Read your previous plan from the architect doc and revise based on this feedback: {user feedback}`

**Architect (addendum — after plan approval):**
Append: `ADDENDUM {n}: the request changed after the plan was approved; the new text is the last addendum in {cycle_dir}/manifest::request. Tasks done so far: {task ids with a builder manifest}. Revise only the tasks, user stories and test-plan rows the addendum affects, and add any new tasks after the last one. Add a decisions entry naming each reopened task (done work the addendum changes) and each new task. Leave unaffected tasks as they are.`

**The Architect never runs anything.** It has no shell. Profiling, a repro, a measurement or a script the plan depends on is a Builder task: the Architect plans it (usually as the first task) and the Builder records the result in its progress entry. Don't dispatch or resume the Architect to run it, and don't run it on its behalf mid-planning.

**Builder (every dispatch — initial and continuation):**

Each Builder is a fresh agent with no memory of an earlier incarnation, so every dispatch carries the same brief: the next unfinished task, inlined, plus the decisions it cites (see Builder Context Handoff). The Builder reads anything else in `architect` itself.

```
You are the PEV Builder for cycle {cycle_id}, incarnation {N}.

Cycle manifest doc ID: {cycle_doc_id}
Cycle directory: {cycle_dir}/ (layout: directory)
Project root: {worktree_path}

Your working directory is: {worktree_path}
Your cwd is already set to this directory. Use git commands directly (no -C flag needed).
Tests: {commands.test_parallel, or commands.test} for the full suite, {commands.test_targeted} for one target (from .pev/sops.toml [commands], or the Detected commands: line in manifest::status). Run them from cwd with relative paths.
The worktree has a axiom-graph DB snapshot — use {worktree_path} as project_root for all axiom-graph tool calls.

== YOUR TASK: {cycle_dir}/architect::tasks.task-{M} ==

{the task-M section text, verbatim}

== DECISIONS THIS TASK CITES ==

{each cited decisions::d-N entry, verbatim, with its heading}

== INSTRUCTIONS ==

- Work task {M}. If it is done and budget remains, read the next `architect::tasks.task-{M+1}` yourself and continue.
- The rest of the pitch (problem, user-stories, solution-sketch, constraints, test-plan) is in {cycle_dir}/architect; read the sections you need.
- You own the `builder` doc. Record progress, your task manifest and checkpoint as `builder::inc-{N}.task-{M}.{progress,manifest,checkpoint}`; add decisions as `decisions::d-N` and friction under `friction::builder`.
- Return the control envelope (reference: Control Envelopes). Follow your skill instructions.
```

For a continuation (incarnation 2 and later), add one line after the header: `CONTINUATION: incarnation {N-1} returned CONTINUING. Its committed code is on disk. Read your earlier inc-*.task-*.checkpoint and inc-*.task-*.progress entries first.` The task brief is not optional on a continuation: the new agent has none of the earlier context. If the node-id check (Builder Context Handoff) found ids that moved, add a `Note: {old id} is now {new id}.` line under the task text.

For a task the addendum loop reopened, add after the task text: `REOPENED by addendum {n} (manifest::request). The earlier builder::inc-*.task-{M}.manifest describes the work before the addendum; change it to match the revised task, and write this run's entries as usual.`

**Builder (fix — review loopback):**
```
You are the PEV Builder for cycle {cycle_id} (targeted fix — review iteration {N}, incarnation {I}).

Cycle manifest doc ID: {cycle_doc_id}
Cycle directory: {cycle_dir}/ (layout: directory)
Project root: {worktree_path}

Your cwd is already set to this directory. Use git commands directly (no -C flag needed).
Tests: {commands.test_parallel, or commands.test} for the full suite, {commands.test_targeted} for one target (from .pev/sops.toml [commands], or the Detected commands: line in manifest::status). Run them from cwd with relative paths.

This is a TARGETED FIX dispatch. The approved fixes are {cycle_dir}/manifest::fix-list.round-{N}; read it first. Fix ONLY its numbered items:
{the round's items, one line each, numbered as in the section}

Record the fix as `builder::inc-{I}.task-fix-{N}.{progress,manifest}`, citing each item by number (`round-{N} item 2: done`), and return the control envelope.
```

Write the round before this dispatch (see Fix List). `{N}` is the round number, the same in the section id and in the Builder's `task-fix-{N}` entries.

**Reviewer (initial):**
```
You are the PEV Reviewer for cycle {cycle_id}.

Cycle manifest doc ID: {cycle_doc_id}
Cycle directory: {cycle_dir}/ (layout: directory)
Project root: {worktree_path}

Your cwd is already set to this directory. Use git commands directly (no -C flag needed).
Tests: {commands.test_parallel, or commands.test} for the full suite, {commands.test_targeted} for one target (from .pev/sops.toml [commands], or the Detected commands: line in manifest::status). Run them from cwd with relative paths.

Review the Builder's code changes against the Architect's pitch AND the pitch's source documents. You are read-only for code. Your default stance is skeptical.

**Read the pitch FIRST** ({cycle_dir}/architect: problem, user-stories, solution-sketch, constraints, source-documents, required-artifacts, test-plan). Form expectations before reading the Builder's notes.

Then read the Builder's context: {cycle_dir}/builder (build-plan and the inc-*.task-* manifests) and {cycle_dir}/decisions. Note tensions between Builder claims and Architect expectations.

Files changed by the Builder:
{git diff --stat output from worktree branch vs baseline}

Use axiom-graph tools (axiom_graph_diff, axiom_graph_source, axiom_graph_graph) to review the actual code changes on demand.

You own the `review` doc. Add one `review::pass-N` section per pass as you finish it (passes 0, 1, 2, 3, 4 and 5, with 5a–5e inside pass 5), write your final verdict to `review::verdict` yourself, and return the control envelope. Friction goes under `friction::reviewer`.

Follow your skill instructions.
```

**Reviewer (continuation):** a Reviewer cut off mid-review (CONTINUING, or no envelope) is redispatched with the Reviewer (initial) prompt plus:
```
CONTINUATION: a previous Reviewer incarnation returned CONTINUING. Its completed passes are in {cycle_dir}/review as pass-N sections (completed: {list from the envelope's `written`, or from the review doc's outline}). Do not redo them. Resume at {next pass, e.g. "Pass 3" or "Pass 5c"}; if a pass was cut off part-way, add its continuation as `pass-{N}-cont-{K}`. Write the final verdict to review::verdict when all passes are done.
```
The review passes are Pass 0 (tests), 1 (source documents), 2 (spec compliance), 3 (functionality preservation), 4 (code quality) and 5 (PEV checks: 5a logging, 5b test annotations, 5c workflow markers, 5d workflow taxonomy, 5e layer discipline).

**Reviewer (re-review after fix):**
Add: `RE-REVIEW: The Builder has addressed the fixes listed in {cycle_dir}/manifest::fix-list.round-{N}; read it first. Verify each numbered item and re-evaluate, citing items by number (round-{N} item 2). Add this review's passes as pass-N sections numbered after the existing ones, and overwrite review::verdict.` After a sharded review, send it to each shard whose findings the round fixes, with the shard line below, and `review::verdict` becomes that shard doc's `verdict`; then consolidate again.

**Reviewer (shard):** for a sharded review (see Sharded Review), every shard gets the Reviewer (initial) prompt with the files-changed block, plus:
```
REVIEW SHARD {X} of {n}: you review tasks {range} ({entry text from architect::constraints}). Your doc is {cycle_dir}/review-shard-{x}: write your pass-N sections and your verdict (its `verdict` section) there, not in `review`.
- {Shard A only:} run Pass 0, the full suite, for every shard. {Other shards:} skip Pass 0; shard A runs it, and its result is in {cycle_dir}/review-shard-a::pass-0.
- Passes 1-5 cover your tasks only: the forward check is every item of your tasks; the reverse mapping is every hunk in the files your tasks' manifests list. A hunk owned by another shard's task is noted with that task's number, not judged.
- To find one task's hunks in a shared file, use the Builder's commits: a commit whose subject names the task (`git log --oneline {main_branch}..HEAD`), then `git show {sha} -- {file}`; otherwise the task manifests' files_changed.
```

**Sync main before the Auditor.** Before the first Auditor dispatch, Phase 6 merges local main into the cycle branch, so the Auditor audits the tree that will land. Local branch refs are shared, so the sync is `git merge --no-edit {main_branch}`, run in the worktree (after `git merge-base --is-ancestor {main_branch} HEAD` exits 1, meaning main moved), never a `git -C` command against main. Never rebase. A conflict is a HUMAN GATE, resolved in the worktree. When main moved, take the sync list before the merge and main's stale ids from main's index, both for the change-set (see **Pre-Audit Check and Change-Set**). After the sync, rebuild the worktree index, and take every branch diff with three dots, `{main_branch}...HEAD`, so main's changes brought in by the sync are not counted as the cycle's. The Auditor and the Doc Reviewer have no Bash: the orchestrator runs the diffs below in the worktree and pastes their output.

**Auditor (initial):**
```
You are the PEV Auditor for cycle {cycle_id}.

Cycle manifest doc ID: {cycle_doc_id}
Project root: {worktree_path}

The merge has not happened. You are auditing the cycle branch in its worktree, with main already merged into it, so this is the tree that will land. Use {worktree_path} as project_root for every axiom-graph call; a call with the main checkout's project_root is denied. Your edits stay in the worktree; the orchestrator commits them on the branch, and the merge step carries your verifications to main after the merge.

Cycle directory: {cycle_dir}/ (layout: directory). Inside it you write only the `audit` doc (impact-report, progress) and `friction::auditor`; outside it you update the project's live docs, in the worktree, as usual.

You have no Bash, so the diff is pasted here; don't try to run git.

== CYCLE CHANGES (branch; git diff --stat {main_branch}...HEAD) ==
{output}

== UNCOMMITTED AUDIT EDITS IN THE WORKTREE (git diff --stat) ==
{output, or "none"}

Main's own changes, brought in by the sync, may read CONTENT_UPDATED here: main has already verified them. They are the files in the change-set's `Brought in by the sync` list. One batched mark_clean, without re-reading each node, clears only the sync-explained ones: own status CONTENT_UPDATED, in a file on the sync list and not among the branch's changed files, not on the change-set's `Main's stale ids at the sync` list, and with a drift_query via= that names no node in the branch's changed files. Every other node the sync touched (an id already stale on main, a node held LINKED_STALE by a branch change, a node that is only LINKED_STALE, a node in a file the branch changed too) is residual: decide it per node.

Read {cycle_dir}/manifest (status, scope, change-set), then run axiom_graph_build + axiom_graph_check to determine the review scope. Follow the Auditor Reference Protocol for the full checklist.

You do not write a change ledger — every `update_section`, `mark_clean`, and link operation is recorded in `node_history` automatically and rendered into `audit::changes-summary` by the orchestrator after you return, via `axiom_graph_report(since_sha=baseline)`. Record only non-obvious judgment calls, in your own `audit` doc: in the impact report's narrative, or in audit::progress while you work. You do not write `decisions`.

Write your Impact Report to {cycle_dir}/audit::impact-report, then return the control envelope (reference: Control Envelopes), not the report.
```

**Auditor (audit split):** when `architect::constraints` ends with an `**Audit split:**` list, add to each Auditor dispatch, initial or continuation: `AUDIT SPLIT: this incarnation covers entry {k} of {n}: {entry text}. Run the change-set doc pass and the staleness review for that entry's files only, record "split entry {k} done" in {cycle_dir}/audit::progress, and return CONTINUING while entries remain. The last entry writes the Impact Report for the whole cycle.` Brief entry k+1 once `audit::progress` records entry k done; otherwise redispatch entry k as a continuation. Every entry runs in the worktree with the Auditor (initial) prompt's project root and pasted diffs, the uncommitted-edits block re-run for each dispatch.

**Auditor (continuation):** the Auditor (initial) prompt, its uncommitted-edits block re-run so it shows the earlier incarnations' edits, plus:
`CONTINUATION: A previous Auditor incarnation was dispatched and returned CONTINUING. The merge still has not happened; keep working in the worktree. Already-marked-clean nodes will not appear stale on axiom_graph_check. Its progress is in {cycle_dir}/audit::progress — read it first.`

**Doc Reviewer (initial):**
```
You are the PEV Doc Reviewer for cycle {cycle_id}.

Cycle manifest doc ID: {cycle_doc_id}
Project root: {worktree_path}

The merge has not happened. The Auditor has made its documentation updates in the cycle's worktree, on the branch that will land (main is already merged into it). Use {worktree_path} as project_root for every axiom-graph call; a call with the main checkout's project_root is denied. Review the Auditor's doc changes against templates and the actual implementation.

Cycle directory: {cycle_dir}/ (layout: directory). You write only `audit::doc-review` (its `progress` and `findings`, and the verdict in the section itself) and `friction::doc-review`.

Read the cycle docs for context: {cycle_dir}/architect (what was requested), the Builder's task manifests in {cycle_dir}/builder (what was built), {cycle_dir}/audit::impact-report (audit summary), and {cycle_dir}/audit::changes-summary (the mechanical list of what the Auditor touched, rendered from `axiom_graph_report`, grouped by root).

You have no Bash, so the diff is pasted here; don't try to run git.

== CYCLE CHANGES (branch; git diff --stat {main_branch}...HEAD) ==
{output}

== UNCOMMITTED AUDIT EDITS IN THE WORKTREE (git diff --stat) ==
{output, or "none"}

== AUDITOR'S PLAIN-FILE EDITS (git diff -- {each .md path the impact report names}) ==
{output, or "none"}

Use axiom-graph tools to verify doc content matches the code. Follow your skill instructions. Write your verdict to audit::doc-review, then return the control envelope (reference: Control Envelopes).
```

**Doc Reviewer (re-review after Auditor fix):** the Doc Reviewer (initial) prompt, its three diff blocks re-run in the worktree so they show the fix, plus:
`RE-REVIEW: The Auditor has addressed the following doc issues from your previous review, in the worktree; the merge still has not happened. The fixes are listed in {cycle_dir}/manifest::fix-list.round-{N}; verify each numbered item, citing it by number, and re-evaluate.`

**Auditor (doc fix — doc review loopback):**
```
You are the PEV Auditor for cycle {cycle_id} (targeted doc fix — doc review iteration {N}).

Cycle manifest doc ID: {cycle_doc_id}
Project root: {worktree_path}

The merge has not happened. Fix in the cycle's worktree: use {worktree_path} as project_root for every axiom-graph call.

You have no Bash, so the diff is pasted here; don't try to run git.

== CYCLE CHANGES (branch; git diff --stat {main_branch}...HEAD) ==
{output}

== UNCOMMITTED AUDIT EDITS IN THE WORKTREE (git diff --stat) ==
{output, or "none"}

This is a TARGETED DOC FIX dispatch from the Doc Reviewer. The approved fixes are {cycle_dir}/manifest::fix-list.round-{N}; read it first. Fix ONLY its numbered items, and cite them by number in audit::progress:
{the round's items, one line each, numbered as in the section}

Your previous `audit::progress` and marked-clean nodes are preserved. Focus on the specific doc issues identified.
```

**All dispatches use:** `subagent_type="pev-{agent}"` (agents: `architect`, `builder`, `reviewer`, `auditor`, `doc-reviewer`). Do NOT use `isolation: "worktree"` — the orchestrator owns the worktree lifecycle.

## Handling Architect doc_edits

When the Architect's NEEDS_INPUT payload includes `doc_edits`, process them before (or alongside) questions:

1. Print the Architect's preamble (if any)
2. For each doc_edit entry:
   ```
   The Architect proposes updating {doc_id}:
   Section: {section_id}
   Reason: {reason}
   Currently: {current_summary}
   Proposed: {proposed_content}
   ```
   Present via AskUserQuestion: "Approve this source doc edit?" with options: Approve / Reject / Reject with note.
3. Apply approved edits:
   ```
   axiom_graph_update_section(
     section_id="{section_id}",
     content="{proposed_content}"
   )
   ```
4. Collect results into `doc_edit_results` array
5. Process questions (if any) via AskUserQuestion
6. Resume Architect with SendMessage:
   ```json
   {"answers": {"question text": "selected label"}, "doc_edit_results": [{"section_id": "...", "status": "applied|rejected", "user_note": "..."}], "context": "...architect's context field verbatim..."}
   ```

## Builder Context Handoff

Every Builder dispatch, initial or continuation, briefs the Builder with **one task**: the next unfinished `architect::tasks.task-M`, inlined verbatim, plus the `decisions::d-N` entries that task cites. Not the whole pitch: each task is written to stand on its own (the Architect sizes it at about 60 Builder calls), and the Builder reads any other `architect` section it needs on demand.

**Step 1: Pick the task.** On the first dispatch it is `task-1`. After a CONTINUING return, it is the task named in the envelope's `next` field; if there is no envelope (maxTurns cutoff), read the `builder` doc's outline (`axiom_graph_read_doc(doc_id="{cycle_dir}/builder", outline=true)`) and take the lowest task with no `inc-*.task-M.manifest`.

**Step 2: Read the task and its decisions in one call.** `axiom_graph_read_doc(section_ids=["{cycle_dir}/architect::tasks.task-M"])`, then read the `d-N` ids it cites the same way: `axiom_graph_read_doc(section_ids=["{cycle_dir}/decisions::d-2", "{cycle_dir}/decisions::d-5"])`.

**Step 3 (continuations only): rebuild, check ids, and hold if needed.**
- Run `axiom_graph_build(project_root="{worktree_path}")` before every continuation, unconditionally. The Builder's `SubagentStop` hook also rebuilds, but a failed hook leaves no trace, and the next Builder would read moved code at old line ranges.
- Check each node id the task names against the rebuilt index: `axiom_graph_render(project_root="{worktree_path}", level=0, node_id="{id}")`, one call per id; level 0 prints only the id, or an error when it is gone. For a missing id, find the current one with `axiom_graph_search(project_root="{worktree_path}", query="{name}", scope="code")` and add a `Note: {old id} is now {new id}.` line to the brief. Don't edit the `architect` doc.
- **Hold the redispatch (B12)** when the user has an open question (a gate question you relayed, a BLOCKED reason, a deviation they are weighing) whose answer could change this task or a later one. Wait for the answer, then brief. An answer that only touches finished work doesn't hold anything.

**Step 4: Assemble the prompt** from the Builder (every dispatch) template in Dispatch Prompts, pasting both texts verbatim.

Source code is not inlined. The Builder reads source on demand using axiom-graph tools (`axiom_graph_source`, `axiom_graph_graph`, `axiom_graph_search`) against the axiom-graph DB snapshot (copied via `axiom_graph_checkout` during worktree setup). The task names the node IDs to read, so the Builder can start with targeted reads.

**Fix dispatches** (review loopback) carry a fix round from `manifest::fix-list` instead of a task (see Fix List).

## Sharded Review

A large cycle is reviewed by several Reviewers in parallel, one per task range, when `architect::constraints` has a `**Review shards:**` list. Without the list, one Reviewer reviews the whole cycle.

1. **Clone the shard docs.** One per entry, in the worktree, before the dispatch:
   ```
   axiom_graph_clone_doc(project_root="{worktree_path}",
     source_doc_id="{project_id}::.pev/templates/cycle/review",
     new_id="pev/cycles/{cycle-id}/review-shard-{x}",
     title="PEV Cycle {cycle-id}: Review (shard {X})")
   ```
   `{x}` is `a`, `b`, … in list order. The tags (`pev-cycle`) come from the template. The doc-scope hook lets the Reviewer write the `review` doc and any `review-shard-*` doc in the cycle directory; it does not tell a shard from the whole-cycle Reviewer. What keeps a shard off `review::verdict`, which the orchestrator consolidates, is the shard prompt, which names the shard's own doc as the only one it writes.
2. **Dispatch every shard at once**, each with the Reviewer (shard) prompt (see Dispatch Prompts). Only shard A runs Pass 0, so only one test run uses the worktree at a time; the other shards read its result.
3. **Handle each return on its own.** A shard that returns CONTINUING is redispatched alone, with the Reviewer (continuation) prompt naming its own doc; a NEEDS_INPUT is relayed as usual (see Gate Payload). Wait until every shard has written its `verdict`.
4. **Consolidate** into `review::verdict`, with one `axiom_graph_update_section` call, in the verdict JSON the Reviewer skill defines:
   - `status`: the worst shard status (FAIL over PASS_WITH_CONCERNS over PASS);
   - `test_coverage`: the shards' tables merged, one row per user story and test;
   - the shards' concerns, quality issues, deviation rulings and `env_gaps`, each tagged with its shard (`B: ...`);
   - **the cross-shard sweep**: list the changed files (`git diff --name-only {main_branch}...HEAD`: three dots, the branch's own changes, so main's files a sync brought in are left out, before the sync and after it) and the files the shards' tasks name (the task manifests' `files_changed` and `tests_added`). A changed file outside every shard's list is an unauthorized change, reported as an `important` concern unless a task manifest records it as a deviation. Test files and the cycle directory don't count.

   The verdict gate (Phase 5) reads the consolidated verdict and shows each shard's `verdict` section under its id.

**Shared files.** A file several tasks edit appears in several shards. Each shard judges the hunks of its own tasks and notes the rest with the owning task's number. A hunk no task's shard claims comes up in the sweep as a gap in attribution; read it and decide which shard re-reviews it.

**Re-review after a fix** goes to the shards whose findings the fix round lists, each on its own doc (see Dispatch Prompts, Reviewer (re-review after fix)); then consolidate again. Re-dispatch one Reviewer over the whole cycle instead only when the round touches most shards.

## Fix List

Every approved fix round is written to `{cycle_dir}/manifest::fix-list` before the fix is dispatched, so the fix agent and the re-reviewer cite the same numbered items, and a later session can find them. A list that lives only in a dispatch prompt is lost when the agent returns.

When the user approves fixes (a review FAIL, review concerns sent back to the Builder, a doc review FAIL, the audit's `needs_fix`, feedback at the merge gate), add the next round:

```
axiom_graph_add_section(project_root="{worktree_path}",
  doc_id="{cycle_doc_id}", parent_id="fix-list",
  section_id="round-{N}", heading="Round {N}: {source} ({date})",
  content="Source: {the verdict or finding section ids}, approved {timestamp}\n1. {file or node}: {the finding, by its id}. {What done means.}\n2. ...\nNot fixed: {each concern the user chose not to fix, and why}")
```

- **Number the rounds** across the cycle (`round-1`, `round-2`, …), whatever the source. The Builder's `task-fix-{N}` entries reuse the round number.
- **Each item names its finding** (`review::verdict` concern 3, `review-shard-b::verdict` quality issue 1, `audit::impact-report` needs_fix 2) and says what done means.
- **Never edit a round after its dispatch.** A follow-up is a new round. Then rewrite `Resume at:` (see Status Updates) to name the round being fixed.

The fix dispatch (Builder fix, or Auditor doc fix) and the re-review prompt both name `manifest::fix-list.round-{N}`. The orchestrator writes it; the doc-scope hook denies it to every agent.

## Gate Payload

Every human gate has one shape: the gates of `/pev-cycle` and of the merge step (`merge-step-reference.md`), the gates of `/pev-instance`, and every `NEEDS_INPUT` relayed from the Reviewer, the Auditor or the Doc Reviewer. One shape keeps each gate to a single answer.

**Before presenting it,** rewrite the `Resume at:` line (`manifest::status` for a cycle, the checkin's `meta` for an instance) to name this gate and what follows it, so a session that stops here can be resumed from the doc alone (see Status Updates).

**The payload,** in this order:

1. **Gate name**: one line, `Gate: {name} ({run id})`, e.g. `Gate: plan approval (pev-2026-03-21-add-history-filtering)`.
2. **Content, verbatim**: the doc sections and tables the decision rests on, as the docs hold them, each under its section id. Read them with one `axiom_graph_read_doc(section_ids=[...])` call and show the text; don't paraphrase or summarize it. For a relayed `NEEDS_INPUT`, the agent's `preamble` plus the sections it names.
3. **Decisions**: a numbered list. Each decision states the choice to make, the options, and a recommendation with its reason in one line. A relayed `NEEDS_INPUT` question is a decision: take the agent's recommendation when it gives one, otherwise add yours.
4. **One question**: exactly one, last, answerable yes or no: "Accept the recommendations above?" or the gate's own yes/no wording ("Approve to merge into main?"). Yes applies every recommendation. A no is followed by the user naming the decisions they change; answer it and present the gate again.

Ask the question with `AskUserQuestion` (options `Yes` and `No`), or as the last line of the message. Never ask more than one question per gate, and never split one gate across several turns.

```
Gate: merge (pev-2026-03-21-add-history-filtering)

{cycle_dir}/review::verdict
{the section's text, verbatim}

Decisions:
1. The audit's needs_fix item on history.py: fix it in a follow-up cycle, fix it now, or accept it. Recommend: follow-up cycle (a doc wording issue, not a defect).
2. Proposed links: apply 3 of 4. Recommend: skip the link to the private helper (it would flag on every refactor).

Accept the recommendations and merge into main? (yes / no)
```

**Not a gate:** the Architect's `NEEDS_INPUT` rounds during planning are a brainstorm, relayed as Phase 2 of the skill and **Handling Architect doc_edits** describe, not in this shape. The plan approval that follows them is a gate.

## Status Updates

Use `axiom_graph_update_section` to update the manifest's `status` section at each phase transition. Until the merge step leaves the worktree, every update goes to the worktree's index (`project_root="{worktree_path}"`); the merge step's updates after `ExitWorktree` go to main's.

**`Resume at:`** is one line in `status` naming the step a fresh session picks the cycle up at, and what is left: `Resume at: Phase 4, task-3 (Builder incarnation 2); tasks 1-2 done`, `Resume at: Phase 8, merge gate (audit committed on the branch)`, `Resume at: merge step 6 (branch merged as {merge_sha}; settle main)`. Rewrite it at every gate, before presenting the gate (see Gate Payload), at every phase transition, and before any stop, including a failure. Rewrite only that line when nothing else changed: `axiom_graph_patch_section(section_id="{cycle_doc_id}::status", old_string="Resume at: {current text}", new_string="Resume at: {new text}")`. Once the cycle completes it reads `Resume at: done`.

**Planning → Builder:**
```
axiom_graph_update_section(
  section_id="{cycle_doc_id}::status",
  content="Phase: builder\nLayout: directory\nBaseline SHA: {baseline_sha}\nMain branch: {main_branch}\nMain repo: {main_repo_path}\nStarted: {original-timestamp}\nCycle ID: {cycle-id}\nEntry baseline (worktree): {left bracket, as recorded at intake}\nStructural: {as recorded at intake, when there was one}\nMain failing tests: {as recorded at intake}\nDetected commands: {as recorded at intake, when there was one}\nRequest file: {path, when the request is a doc or file}\nResume at: Phase 4, task-1 (Builder incarnation 1)\n\nPhase transitions:\n- planning: {original-timestamp} — cycle created\n- builder: {now-timestamp} — plan approved"
)
```

**Review → Auditor** (Phase 6, after the sync and the pre-audit check):
Add: `- auditor: {now-timestamp} — reviewed, main synced into the branch, auditing in the worktree`

**Auditor → Doc Review:**
Add: `- doc-review: {now-timestamp} — audit complete, reviewing docs in the worktree`

**Doc Review → Merge** (Phase 8, after the merge gate approved, before the merge step starts):
Add: `- merge: {now-timestamp} — merge gate approved, running the merge step`

**Merge → Completed** (the merge step's completion commit, step 8, on main):
Add: `- completed: {now-timestamp} — merged as {merge_sha}, main settled`
Also add: `Completed: {now-timestamp}` and the `Final check:` line from the merge step's residue gate (step 7), and set `Resume at: done`.

**Addendum folded in (any phase before the merge):**
Add: `- addendum-{n}: {now-timestamp} — folded in at {gate}; reopened {task ids}, new {task ids}`

**Failure at any point:**
Set `Phase: incomplete` with reason, and rewrite `Resume at:` to the step to retry and what it needs first (e.g. `Resume at: Phase 6, Auditor incarnation 3; the user is reinstalling a scanner extra`). Leave the `in-progress` status tag in place so the cycle can be found later; there is no automatic resume (see Error Handling).

## Control Envelopes

The Builder, the Reviewer, the Auditor and the Doc Reviewer write their full results into their own docs and return only a short **control envelope**: enough for the orchestrator to pick the next step. The orchestrator does not relay or re-write their bodies; when it needs the detail (the Reviewer's test-coverage table, the Builder's deviations for the change-set, the Auditor's `needs_fix` items, either audit role's `proposed_links`) it reads the doc from disk, once, at the gate that shows it. The role skills point here for the shape.

**Shape.** The agent's final message is a status line, at most a few lines of prose for the user (the BLOCKED / NEEDS_CONTEXT reason, or concerns worth surfacing), then the separator and one JSON object:

```
BUILDER CONTINUING

Task 3 is half done; the parser change is in, the CLI flag is not.

---ENVELOPE---
{
  "role": "builder",
  "status": "CONTINUING",
  "written": ["builder::inc-2.task-3.progress", "builder::inc-2.task-3.checkpoint"],
  "next": "task-3",
  "env_gaps": [],
  "summary": "one line"
}
```

The common fields:

| Field | Builder | Reviewer | Auditor | Doc Reviewer |
|---|---|---|---|---|
| `role` | `"builder"` | `"reviewer"` | `"auditor"` | `"doc-reviewer"` |
| `status` | `DONE`, `CONTINUING`, `BLOCKED`, `NEEDS_CONTEXT` | `PASS`, `PASS_WITH_CONCERNS`, `FAIL`, `CONTINUING`, `NEEDS_INPUT` | `DONE`, `DONE_WITH_CONCERNS`, `CONTINUING`, `NEEDS_INPUT` | `PASS`, `PASS_WITH_CONCERNS`, `FAIL`, `CONTINUING`, `NEEDS_INPUT` |
| `written` | the `<doc>::<section>` ids it wrote this run, relative to the cycle directory | same (`review::pass-N` sections, `review::verdict`) | same (`audit::impact-report`, `audit::progress`) | same (`audit::doc-review`, `.progress`, `.findings`) |
| `next` | the next unfinished task (`"task-4"`), or `null` when every task is done | the next pass to run on a CONTINUING (`"pass-3"`, `"pass-5c"`), else `null` | the step to resume at on a CONTINUING (e.g. `"staleness-review"`), else `null` | the category or step to resume at on a CONTINUING, else `null` |
| `env_gaps` | tests that couldn't run for a missing main-declared dependency (`[{"test", "missing", "note"}]`) | same, from Pass 0 | — (runs no tests) | — (runs no tests) |
| `summary` | one line | one line | one line | one line |

Role-specific fields, only what a gate branches on:

| Field | Role | Content |
|---|---|---|
| `needs_fix` | Auditor | a short list, `[{"node_id", "severity"}]`; the descriptions stay in `audit::impact-report` |
| `checks_completed` | Auditor | the report's `checks_completed` object, copied; DONE with any entry `false` is treated as CONTINUING |
| `findings` | Doc Reviewer | counts by severity, `{"critical": 0, "important": 1, "minor": 2}` |
| `proposed_links` | Auditor, Doc Reviewer | the number of proposals (an integer); the proposals stay in the doc |

**NEEDS_INPUT** (Reviewer, Auditor, Doc Reviewer): the proxy-question JSON from the skill's Asking the User section (`status`, `preamble`, `questions`, `context`) comes back in place of the envelope, as it does for the Architect.

Where the bodies live:
- **Builder:** `builder::inc-N.task-M.manifest` (files changed, tests added, deviations, env gaps — the old `---MANIFEST---` JSON), `builder::inc-N.task-M.progress`, and `builder::inc-N.task-M.checkpoint` (the code map a continuation needs) on a CONTINUING. The Builder writes all of them itself, including the checkpoint: the orchestrator never adds a checkpoint section.
- **Reviewer:** `review::pass-N` per pass and `review::verdict` (status, `test_coverage`, `reverse_mapping`, `deviation_tribunal`, `env_gaps`, concerns).
- **Auditor:** `audit::impact-report` (findings narrative, `needs_fix`, `proposed_links`, `checks_completed`, `skipped_nodes`, advisory `counts`, summary) and `audit::progress` while it works. It replaces the old `---IMPACT-REPORT---` return.
- **Doc Reviewer:** `audit::doc-review` (the verdict JSON) with its `progress` and `findings` children. It replaces the old `---DOC-REVIEW---` return.

The orchestrator writes none of these sections. An agent cut off before writing its report or verdict is a CONTINUING: redispatch it, rather than filling the section in yourself.

**Auditor changes-summary:** the Auditor does NOT write a hand-typed change ledger. The orchestrator renders it after the Auditor returns, before the Doc Review (see Changes Summary below).

**No envelope found:** the agent was cut off by the `maxTurns` limit before writing a structured return. Treat this as `CONTINUING` — the work on disk (code and `builder` entries for the Builder, `review::pass-N` for the Reviewer, marked-clean nodes and `audit::progress` for the Auditor, `audit::doc-review.progress` for the Doc Reviewer) is the real state. Pick the next task or pass from the doc's outline, as Builder Context Handoff describes.

## Changes Summary

`{cycle_dir}/audit::changes-summary` is the mechanically derived list of every doc section update, `mark_clean` / `reverify`, link change and `AGENT_VERIFIED` event in the audit, rendered from `node_history` by `axiom_graph_report`. It is grouped by root, capped, and kept in the `audit` doc.

**When:** after the Auditor returns DONE or DONE_WITH_CONCERNS and **before Phase 7** (Doc Review), so the Doc Reviewer reads it. Render it again at the end of Phase 7, before the audit commit and the merge gate, when anything was written after that: a doc-fix loop, or links applied at the proposed-links gate.

**Where:** from the worktree's index, `project_root="{worktree_path}"`. The audit happened there and the merge has not, so main's index doesn't hold its history yet. The section is committed on the branch with the rest of the audit and lands with the merge.

**How:** one condensed report per root, each capped:
- one call per docs root (each entry of `info`'s `docs_dirs`):
  ```
  axiom_graph_report(project_root="{worktree_path}", since_sha="{baseline_sha}", detail="condensed",
    node_pattern="{project_id}::{root}/*", exclude_node_pattern="{cycle_dir}/*", max_chars=12000)
  ```
- one call for everything else (code and tests), excluding the docs roots and the cycle:
  ```
  axiom_graph_report(project_root="{worktree_path}", since_sha="{baseline_sha}", detail="condensed",
    exclude_node_pattern=["{cycle_dir}/*", "{project_id}::{root 1}/*", "{project_id}::{root 2}/*"], max_chars=12000)
  ```

Write them in one call, each under a `### {root}` heading (`### code` for the last):
```
axiom_graph_update_section(project_root="{worktree_path}", section_id="{cycle_dir}/audit::changes-summary", content="### docs\n{report}\n\n### .pev\n{report}\n\n### code\n{report}")
```

- `detail="condensed"` keeps every actor-authored row (doc section edits, `mark_clean` / `reverify`, link adds and removals) verbatim and aggregates the build's own staleness and link churn by container. `"full"` lists every row and has run past 200,000 characters on a real cycle.
- **Never pass `max_chars=None`.** A root whose report hits the cap keeps the report's truncation footer, which says how to get the rest. Leave the footer in place: anyone who needs every row for one root or change type runs the report again with `detail="full"` and a `node_pattern` or `change_type_pattern`, and doesn't paste the result into the cycle docs.
- The `exclude_node_pattern` on `{cycle_dir}/*` leaves out all the cycle's own docs, including its efficiency report.

## Orchestrator Decisions and Friction

The orchestrator's entries go in the same append-only logs as the agents', and are added the same way:

- **Decision** (e.g. the Phase 7 proposed-links outcome): read the `decisions` outline (`axiom_graph_read_doc(doc_id="{cycle_dir}/decisions", outline=true)`) for the next free number, then `axiom_graph_add_section(doc_id="{cycle_dir}/decisions", section_id="d-N", heading="D-N (Orchestrator): {title}", content="**Phase:** ...\n**Choice:** ...\n**Alternatives:** ...\n**Reason:** ...")`. `add_section` refuses a duplicate id; if someone took the number first, re-read the outline and take the next one.
- **Friction:** `axiom_graph_add_section(doc_id="{cycle_dir}/friction", parent_id="orchestrator", section_id="{short-tag-slug}", heading="{short tag}", content=...)`.

Entry ids are slugs with no dots. Omit `content` rather than passing `""`. After two identical failures, change approach instead of retrying. Never `update_section` or `patch_section` an entry: the logs are append-only, and the hook refuses edits to them from every subagent.

## Commit Format

Everything up to the merge is committed on the cycle branch, in the worktree, with plain `git` (the session's cwd is the worktree):
- the Builder's commits, one or more per task;
- Phase 6's straggler commit, when the Builder left anything uncommitted;
- the Phase 6 sync's merge of `{main_branch}` into the branch, when main had moved (`git merge --no-edit`, git's own message);
- the **audit commit** on the branch, at the start of Phase 8, before the merge gate (below);
- the merge step's pre-merge baseline commit (step 4 of `merge-step-reference.md`).

Main then gets exactly two commits per cycle, both written by the merge step: the **merge commit** (`git merge --no-ff` of the branch, its message carrying an `Auditor:` line) and the **`PEV complete: {cycle-id}`** commit (the final check and the efficiency report). There is no separate audit commit on main: the audit lands inside the merge.

Every commit follows the same rules:

- **Stage by explicit path, never `git add -A`.** `-A` sweeps in whatever else is in the tree: an in-worktree `.venv/`, scratch output, and on main another cycle's files.
- **Write the message to a file, then `git commit -F <file>`.** Use the Write tool, which writes UTF-8 without a BOM. Don't write it with PowerShell 5.1's `Set-Content` or `Out-File`, which add a BOM that git keeps at the start of the subject line. No heredoc and no `$(cat …)` in the command.
- **Attribution:** add the attribution trailer your session or the project's commit convention specifies. Never hard-code a model name.

**Straggler commit** (Phase 6, before the sync). Run `git status --porcelain` in the worktree. Stage what belongs to the change by path: the cycle directory `docs/pev/cycles/{cycle-id}/`, the files of any `builder_docs` doc, and any code or test paths the listing shows. Never stage `.venv/` or another environment folder; leave anything you can't place unstaged and mention it at the next gate. Write a one-line message (`PEV: commit uncommitted changes before the audit ({cycle-id})`) to `.pev-scratch/straggler-message.txt` with the Write tool, then:
```bash
git add docs/pev/cycles/{cycle-id} {other listed paths}
```
```bash
git commit -F .pev-scratch/straggler-message.txt
```

**Audit commit on the branch** (Phase 8, before the merge gate, so the gate shows the cycle as one committed unit). Run `git status --short` in the worktree, then stage by explicit path:
- the doc files and `.md` files the Auditor edited (named in `audit::impact-report` and `audit::changes-summary`);
- the cycle directory, `docs/pev/cycles/{cycle-id}/` (manifest status, the audit doc, the review docs);
- the files `render_site` rewrote at the end of Phase 7.

Leave anything you can't place unstaged and mention it at the merge gate. Write the message to `.pev-scratch/audit-message.txt` with the Write tool:
```
PEV: audit and cycle docs ({cycle-id})

Auditor: {summary from audit::impact-report — nodes reviewed, docs updated, links changed, needs_fix count}
Doc review: {verdict}

{attribution trailer, if any}
```
```bash
git add {each path above}
```
```bash
git commit -F .pev-scratch/audit-message.txt
```

**Merge commit and `PEV complete:` commit.** Their message shapes are in `merge-step-reference.md` (step 5, the merge commit with its `Architect:`, `Builder:`, `Review:` and `Auditor:` lines; step 8, the `PEV complete: {cycle-id}` commit with the `Final check:` line and the carry's numbers). Use them as written there; this reference keeps no copy.

## Merge Step

Phase 8, after the merge gate approves. The procedure that leaves the worktree, lands the branch on main and settles main's index is `${CLAUDE_PLUGIN_ROOT}/templates/merge-step-reference.md`, shared with `/pev-instance`. Follow it as written; this reference keeps no copy of its steps. In order: the full suite in the worktree, `ExitWorktree(action="keep")`, preflight from main (no tracked changes on main, nothing uncommitted in the worktree, main synced into the branch again if it moved), main's pre-merge line and stale ids, `git merge --no-ff` of the branch, build and `carry_forward` on main, the per-node residue gate, the `PEV complete:` commit and the history checkpoint, then removal of the worktree and the branch.

A cycle passes these inputs:

| Input | Cycle value |
|---|---|
| `{run_id}` | the cycle id |
| `{run_kind}` | `cycle` |
| `{run_doc_id}` | `cycle_doc_id` |
| `{baseline_section}` | `{cycle_dir}/manifest::baseline` |
| `{run_dir}` | `docs/pev/cycles/{cycle-id}/` |
| `{worktree_path}`, `{branch}` | the worktree's path and `worktree-{cycle-id}` |
| `{main_branch}`, `{main_repo_path}`, `{baseline_sha}` | the `Main branch:`, `Main repo:` and `Baseline SHA:` lines of `manifest::status` |

What stays the cycle's own around it:
- **Before it:** the right bracket (**Pre-Audit Check and Change-Set**) and the branch's change-set feed the merge gate; the audit commit on the branch (**Commit Format**) comes before the gate. Update status to `merge` before step 1 (see **Status Updates**).
- **After it:** Phase 9 presents the integration options (keep local, push, or open a PR), and says so when an addendum arrived after the merge (see **Request Addenda**).

## Error Handling

**Agent dispatch fails:** Check that the pev plugin is installed and enabled (`claude plugin list`). Agent definitions ship in the plugin at `${CLAUDE_PLUGIN_ROOT}/agents/pev-{agent}.md`. If the plugin looks fine but dispatch still fails, see `pev_nexus_agents/hook-spike/TROUBLESHOOTING.md` §7 for known failure modes.

**axiom_graph_clone_doc fails:** an unknown `set_sections` id means the seeded template no longer matches this reference (re-seed it); a missing source means the templates are not seeded (see Cycle Creation's halt). Do not fall back to `axiom_graph_write_doc` or a file copy.

**Worktree creation fails:** Check for stale worktrees with `git worktree list` and remove them.

**Merge conflicts:** a conflict in the Phase 6 sync of main into the branch, or in the merge step's preflight sync, is a HUMAN GATE: present the conflicted paths to the user and resolve them in the worktree before proceeding. Landing the branch on main (merge step 5) has no conflicts of its own once main is synced; if it reports one, main moved again: abort that merge and sync once more.

**axiom_graph_check hangs:** Known issue — set a timeout and retry. If it hangs again, proceed with manual review scope based on the Builder's change-set.

**Failure at any point:** Update the cycle manifest status to `incomplete`. The `in-progress` status tag stays, so the cycle can be found later with a tag search (`pev-cycle` plus `in-progress`). A result is either a cycle directory's manifest, `{project_id}::docs/pev/cycles/{cycle-id}/manifest`, or a cycle made before plugin 3.0, the single doc `{project_id}::docs/pev/cycles/{cycle-id}`; accept both. Skip every efficiency report: `docs/pev/cycles/efficiency/`, `docs/pev/cycles-efficiency/`, and any `efficiency` or `efficiency-sN` doc inside a run directory. There is no automatic resume: `/pev-cycle` always starts a new cycle and does not look for interrupted ones. The interrupted cycle's worktree, branch and docs stay in place, and a later session can carry it on by hand. It starts from `manifest::status`: the `Resume at:` line names the step to pick up at and what is left, and the `Phase`, `Main branch:`, `Main repo:` and `Baseline SHA:` lines give the rest of the cycle's facts. It then reads the agents' own progress entries for where each stopped, and `manifest::baseline` for the node ids the merge step compares. A cycle stopped inside the merge step resumes at the step its `Resume at:` line names; the worktree is still there until step 9.

**Subagent returns CONTINUING:** redispatch with the continuation prompt (see Dispatch Prompts). The agent has already written its own progress and checkpoint; the orchestrator writes nothing (see Control Envelopes).

## Legacy Layout

A cycle created before plugin 3.0 is one doc, `{project_id}::docs/pev/cycles/{cycle-id}`, and its `.pev-state.json` has no `layout` field. It finishes on that layout; never migrate it. When you carry one on, the differences are:

- Sections are dotted paths in the one doc: `{cycle_doc_id}::architect.tasks`, `builder.progress`, `reviewer.progress`, `auditor.impact-report`, `doc-review`, `decisions`, `orchestrator.friction`.
- The hook keeps the old rules: every section of the one manifest is writable by every PEV subagent, and `decisions` and the `*.friction` sections are appended to with `axiom_graph_patch_section(anchor="$")` (besides the consumer-docs audit agents' findings appends, which wait for that audit doc's redesign, this legacy-layout note is the only place that advice remains).
- Agents return the old separators (`---MANIFEST---`, `---REVIEW---`, `---IMPACT-REPORT---`, `---DOC-REVIEW---`) with the full JSON, and the orchestrator writes the Builder's manifest to `builder.manifest`, the review to `review`, and a `checkpoint-{N}` section under `builder` on CONTINUING.
- Add a line to every dispatch prompt: `LEGACY LAYOUT: this cycle is the single doc {cycle_doc_id}; use its dotted section paths and the old return separators, not the per-doc layout your skill describes.`
- The changes-summary report excludes `{cycle_doc_id}*`.

**Cycles started under the old order.** Before the audit moved into the worktree, a cycle merged first and audited on main. A cycle that started that way, recognised by a manifest with no `baseline` section or by a branch already merged into main before its audit, finishes on that order; don't switch it mid-way. Not yet merged: run the merge step's steps 1 to 5 (suite, `ExitWorktree`, preflight, land the branch) with no `Auditor:` line in the merge message, skipping step 4's id record (there is no baseline section to write it to), and remove the worktree and branch (step 9) once the merge commit exists. On main: write `.pev-state.json` to the main repo root with `cycle_id`, `cycle_doc_id` and the `layout` the worktree's had (no `worktree_path`), after checking that no other cycle's file is there (one is another cycle's main-side audit still running: wait for it or ask the user); dispatch the Auditor and the Doc Reviewer with `Project root: {main_repo_path}` and the note that the merge has already happened, pasting `git diff --stat {baseline_sha} {merge_sha}` and main's uncommitted edits; render the changes-summary from main's index. Finish with the final check: compare main's `axiom_graph_check` counts with main's counts on the entry-baseline line, and gate anything above them. Then flip the tag, write the efficiency report, delete the state file on main, render any consumer docs, commit the audit on main as `PEV audit: {cycle-id}` (the `Auditor:` line in its message; staged by path: the edited docs, the cycle's file or directory, the rendered files) and create the `pev-cycle-{cycle-id}-audit-complete` checkpoint after it.
