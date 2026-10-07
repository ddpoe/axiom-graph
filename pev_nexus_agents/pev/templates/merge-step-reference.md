# PEV Merge Step Reference

The one procedure that lands a finished PEV run on main and settles main's index. `/pev-cycle` follows it in its merge phase, after the merge gate approved the branch; `/pev-instance` follows it for each task, after its reviews. Both point here and keep no copy of their own. The procedure is written to stand alone: it names its inputs, and a reader with those inputs and this file needs nothing else to run it.

It starts in the worktree, with every change of the run committed on the branch: code, tests, the audit's doc edits and the run's own docs. It ends on main with the branch merged, main's index settled, a `PEV complete:` commit and checkpoint, and the worktree and branch removed.

## Inputs

| Input | What it is |
|---|---|
| `{run_id}` | The cycle id or the instance id. |
| `{run_kind}` | `cycle` or `instance`. |
| `{run_doc_id}` | The run's own doc: the cycle's `manifest` or the instance's `checkin`. Take it as `cycle_doc_id` from the worktree's `.pev-state.json`, the id its clone result printed; never build it by hand. |
| `{baseline_section}` | Where the run keeps its baseline node ids: `{run_doc_id}::baseline` for a cycle; for an instance, the checkin section the instance skill names. |
| `{run_dir}` | The run directory's path in the repository: the folder that holds `{run_doc_id}`'s file (the `file:` in `axiom_graph_read_doc`'s header), with whatever extension `info`'s `docs_extensions` gave it. |
| `{worktree_path}` | Absolute path to the run's worktree. |
| `{branch}` | The worktree's branch. |
| `{main_branch}` | The branch the main checkout has checked out (e.g. `main` or `master`). |
| `{main_repo_path}` | Absolute path to the main checkout, from `git worktree list`. |
| `{baseline_sha}` | The commit the run started from, from the `Baseline SHA:` line of the run doc. |

Commands come from the `[commands]` table of `.pev/sops.toml` (`test_parallel` or `test`, `axiom_graph`), or from the run doc's `Detected commands:` line when the toml lacks a key. Doc ids and extensions come from `axiom_graph_info`; never write a literal project id, docs folder or extension.

Every command below is one plain Bash call, shaped as the orchestrator reference's **Shell Command Shapes** says: no `&&`, `;` or `||` chains, no `cd`, no heredocs and no `$(…)`. A commit message is a file written with the Write tool and passed to `git commit -F`. Every gate below is presented in the orchestrator reference's **Gate Payload** shape, and each gate first rewrites the run doc's `Resume at:` line to name the step to resume at.

**Which index holds the run doc.** Until step 5's merge commit the run doc exists only on the branch, so main's index has no copy of it. A `Resume at:` rewrite up to then, the steps after step 2 included, goes to the worktree's index: `axiom_graph_update_section(project_root="{worktree_path}", section_id=...)`. It is committed on the branch by the next commit that stages `{run_dir}` (step 3 item 2 when the run resumes, or step 4). From the merge commit on, the run doc is on main, and every rewrite takes `project_root="{main_repo_path}"`.

## Why this order

A session isolated in a worktree cannot run `git -C {main_repo_path} ...`: the harness refuses it ("a worktree-isolated session's git operations must target its own worktree"). So nothing touches main while the session is still inside the worktree. The suite runs first, in the worktree; then the session leaves the worktree; every main-side command comes after that. From step 2 on, plain `git` targets main (the session's cwd) and `git -C {worktree_path}` targets the worktree.

## Steps

### 1. Suite (in the worktree)

Run the full suite once, as the orchestrator reference's **Project Commands** describes (`commands.test_parallel` as one foreground call, else `commands.test`, split in halves when it runs past about 540 s):

```bash
{commands.test_parallel}
```

A failure that the run doc's `Main failing tests:` line doesn't list stops the merge: gate it, with the failing test ids and their error lines. Nothing has touched main yet.

### 2. Leave the worktree

```
ExitWorktree(action="keep")
```

The session is on main from here on. The worktree stays on disk: step 6 reads its index, and step 9 removes it.

### 3. Preflight (from main)

1. **Main has no tracked changes:**
   ```bash
   git status --porcelain --untracked-files=no
   ```
   Any line stops the merge: ask the user (another session's work in progress, or the user's own). Never stage, stash, reset or check out main's files to clear it. Untracked files on main don't block the merge.
2. **The worktree has nothing uncommitted:**
   ```bash
   git -C {worktree_path} status --porcelain
   ```
   A path that belongs to the run (its code, tests, docs or `{run_dir}`) is committed on the branch, staged by explicit path (`git -C {worktree_path} add {path} ...`, then `git -C {worktree_path} commit -F {worktree_path}/.pev-scratch/safety-net-message.txt`). Anything else is shown to the user, never staged.
3. **Main moved since the branch last took it in:**
   ```bash
   git merge-base --is-ancestor {main_branch} {branch}
   ```
   Exit 0: main is already in the branch; go to item 4. Exit 1: main moved. Merge it into the branch, in the worktree:
   ```bash
   git -C {worktree_path} merge --no-edit {main_branch}
   ```
   Never rebase: the branch's commits are cited by the run's docs. **A conflict is a gate:** list the conflicted paths (`git -C {worktree_path} status --short`) and stop. Once the user approves a resolution, resolve each file in the worktree (a DocJSON conflict section by section: keep both sides' edits to different sections, merge a section both sides edited by hand, then keep each hand-merged doc with `axiom_graph_accept_doc_edits(project_root="{worktree_path}", ...)`), stage the resolved paths by name and finish with `git -C {worktree_path} commit --no-edit`.

   After a sync, re-run the full suite against the worktree, the same way as step 1 (`commands.test_parallel` as one foreground call, else `commands.test`, split in halves when it runs past about 540 s). The session is no longer inside the worktree, so point the command at it: the project's runner with the worktree as its directory and the worktree's test paths as its arguments, so both the environment and the collected tests are the worktree's (for a `commands.test_parallel` of `poetry run pytest -q -n 4`, that is `poetry -C {worktree_path} run pytest -q -n 4 {worktree_path}/tests`). A new failure is a gate, as in step 1.

   Main's own changes now read `CONTENT_UPDATED` in the worktree. Main has already verified them, so they never show on main; never ask anyone to re-verify them in the worktree.
4. **Refresh the worktree index**, so the carry in step 6 reads the branch as it will land:
   ```
   axiom_graph_build(project_root="{worktree_path}")
   axiom_graph_check(project_root="{worktree_path}")
   ```

### 4. Record main's pre-merge line

Build main's index so it describes main as it is, then take its line and its stale node ids:

```
axiom_graph_build(project_root="{main_repo_path}")
axiom_graph_check(project_root="{main_repo_path}")
axiom_graph_drift_query(project_root="{main_repo_path}", format="ids", limit=500)
```

Page `drift_query` (`page=1`, `2`, …) while its header says more rows remain. These ids are the merge target: an id here is not residue later, whatever its status.

Write them into the run's baseline in the worktree, so they land with the merge and survive a stop: append `Pre-merge (main): {check line}` and `Pre-merge stale ids (main): {ids, or none}` to `{baseline_section}` (`axiom_graph_patch_section(project_root="{worktree_path}", section_id="{baseline_section}", anchor="$", new_string=...)`), and set the run doc's `Resume at:` line to step 5 of this procedure. Commit them on the branch, staged by path, with a one-line message file (`PEV: pre-merge baseline ({run_id})`):

```bash
git -C {worktree_path} add {run_dir}
```
```bash
git -C {worktree_path} commit -F {worktree_path}/.pev-scratch/baseline-message.txt
```

### 5. Land the branch

This step is self-contained, so a later pull-request variant can replace it alone (push the branch, open a PR and stop with a `Resume at:` line; on resume, pull, build, then carry and finish from step 6). That variant is not built.

First check, right before landing, that main has not moved since step 3 took it in:

```bash
git merge-base --is-ancestor {main_branch} {branch}
```

Exit 1 means main moved (another run landed, say): don't land. Go back to step 3 item 3 and sync again (with its suite re-run), then step 3 item 4 and step 4 again, which appends a new `Pre-merge` pair to `{baseline_section}`; the latest pair is the merge target. Exit 0: land the branch.

```bash
git merge --no-ff --no-commit {branch}
```

Main is in the branch, so this merge has no conflicts. If git reports any, run `git merge --abort` and go back to step 3.

Write the message file `{worktree_path}/.pev-scratch/merge-message.txt` with the Write tool (the worktree still exists, and the file goes away with it):

```
{one line summarizing the run}

PEV Cycle: {run_id}
Architect: {scope summary}
Builder: {implementation summary}
Review: {verdict, e.g. PASS_WITH_CONCERNS (2 minor)}
Auditor: {from the audit's impact report: nodes reviewed, docs updated, links changed, needs_fix count; and the doc review's verdict}

{attribution trailer, if any}
```

An instance writes `PEV Instance: {run_id}` and one `Review: Reviewer {status}, Doc Reviewer {status}` line in place of the four role lines. Then commit; the merge already staged the branch, so there is no `git add`:

```bash
git commit -F {worktree_path}/.pev-scratch/merge-message.txt
```
```bash
git rev-parse HEAD
```

The merge commit's tree is the branch tip's, so every verification made in the worktree can carry.

### 6. Settle main

```
axiom_graph_build(project_root="{main_repo_path}")
axiom_graph_carry_forward(project_root="{main_repo_path}", worktree_path="{worktree_path}", dry_run=True)
axiom_graph_carry_forward(project_root="{main_repo_path}", worktree_path="{worktree_path}")
axiom_graph_check(project_root="{main_repo_path}")
axiom_graph_drift_query(project_root="{main_repo_path}", format="ids", limit=500)
```

- Read the dry run before the real carry: how many would carry, and `Stale here`. An `ERROR: carry-forward refused: ...` (a missing worktree index, a schema or project-id mismatch) stops here: gate it, and leave the worktree in place.
- Keep the real carry's report lines for the completion commit: carried in full and in part, `Stale here: B before -> A after`, and the `Not carried` reasons.
- Compare the ids with step 4's, by node id. An id step 4 didn't have is **residue**. An id step 4 had that is gone was settled by the merge; note it, nothing to do.
- For each residue id, take its status pair and cause from `axiom_graph_drift_query(project_root="{main_repo_path}", format="full")` (`via` / `root` name the offenders) and why it didn't carry from a second dry run with `list_nodes=True` (it writes nothing; it lists every stale node by verdict).

Expected residue is small: nodes that both main and the branch changed during the run (their content matches neither side), and sections held by a transitive doc-to-doc link until the section they link to settles. Frozen `RENAMED` rows are hidden by `check` and counted by the carry; they need no action.

### 7. Residue gate

No residue: skip the gate and record `Final check: at main's pre-merge line` for step 8.

Otherwise present one gate listing each residue id with its status and reason, and a recommendation per node:
- **mark clean:** the node's text was reviewed and is accurate;
- **reverify:** a source whose change didn't affect its dependents (it also clears the `LINKED_STALE` it caused);
- **record:** leave it stale and record the id for a follow-up (a real doc gap becomes a request or finding, not a rewrite here).

Apply only what the user approved, node by node; ids approved for the same action may go in one batched call:

```
axiom_graph_mark_clean(project_root="{main_repo_path}", node_ids=[...], reason="{why}", verified_by="agent:pev-orchestrator")
axiom_graph_reverify(project_root="{main_repo_path}", node_ids=[...], reason="{why}", verified_by="agent:pev-orchestrator")
```

An instance session uses `verified_by="agent:pev-instance"`. Use the MCP tools, never the CLI `mark-clean`, which records a human verifier. Never clear residue in bulk, never with SQL. A section held by a transitive link often settles once the section it links to is cleared, so apply the other decisions first and check again before deciding it. Then:

```
axiom_graph_check(project_root="{main_repo_path}")
```

That line is the final check. Record it as `Final check: {check line}; residue {n}: {id: decision, ...}`, or `Final check: at main's pre-merge line`.

### 8. Completion commit

Everything here is written on main (`project_root="{main_repo_path}"`); the run's docs arrived with the merge.

1. **Status.** A cycle rewrites `{run_doc_id}::status` (phase completed, the transition line, `Completed:`, the `Final check:` line, `Resume at: done`) and appends the `Final check:` line and the final ids (`Final stale ids (main): {ids, or none}`) to `{baseline_section}`. An instance sets the checkin's `meta` to `Status: done`, with the `Final check:` line.
2. **Tags.** `axiom_graph_update_doc_meta(project_root="{main_repo_path}", doc_id="{run_doc_id}", tags=[...])` with the doc's current tags, `in-progress` replaced by `completed`. `tags` replaces the whole list, so pass every other tag unchanged.
3. **Efficiency report(s).** Run the analyzer: `--find-cycle {run_id}` for a cycle, `--find-run {run_id}` for an instance. It reads the session logs under `~/.claude/projects` and keeps only the calls for this run, from every session that worked on it. If it finds no session, pass this session's log path with `--cycle {run_id}` instead.
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" --find-cycle {run_id} --docjson --summary
   ```
   For each `Staged DocJSON:` line it prints, write the report with the call it prints: `axiom_graph_write_doc(project_root="{main_repo_path}", doc_file="<staged path>")`. The report lands in `{run_dir}` as `efficiency` (`efficiency-s2`, … per session). Never have the script write into the docs tree.
4. **Commit.** Check what is there, then stage the run directory by path, and nothing else of main's:
   ```bash
   git status --short
   ```
   ```bash
   git add {run_dir}
   ```
   Write `{worktree_path}/.pev-scratch/complete-message.txt`:
   ```
   PEV complete: {run_id}

   {the Final check: line}
   Carry: {carried in full / in part; Stale here before -> after}

   {attribution trailer, if any}
   ```
   ```bash
   git commit -F {worktree_path}/.pev-scratch/complete-message.txt
   ```
5. **Checkpoint**, after the commit, since it records HEAD:
   ```bash
   {commands.axiom_graph} history checkpoint . --message "{run_id}-complete"
   ```

### 9. Cleanup

1. **Keep the evidence.** `{main_repo_path}/.pev-scratch/` ignores itself: if `{main_repo_path}/.pev-scratch/.gitignore` doesn't exist, write it with the Write tool holding the single line `*`. Then copy the worktree's scratch folder:
   ```bash
   mkdir -p {main_repo_path}/.pev-scratch/runs
   ```
   ```bash
   cp -r {worktree_path}/.pev-scratch {main_repo_path}/.pev-scratch/runs/{run_id}
   ```
2. **Remove the worktree**, with no `--force`:
   ```bash
   git worktree remove {worktree_path}
   ```
   The state file is ignored through `info/exclude` and doesn't block the remove (if it does, the exclude line is missing: delete `{worktree_path}/.pev-state.json` and remove again). Any other untracked file is real work: show it to the user rather than forcing the remove.

   **Windows long paths.** When the remove fails on a path too long for Windows (deep `node_modules` or environment folders), delete the folder through its `\\?\` form, with the path in Windows form, then let git forget it:
   ```powershell
   Remove-Item -LiteralPath '\\?\{worktree_path, backslashes}' -Recurse -Force
   ```
   ```bash
   git worktree prune
   ```
3. **Delete the branch.** It is merged into main, so `-d` is enough:
   ```bash
   git branch -d {branch}
   ```

A per-worktree environment created outside the worktree is not removed with it; the orchestrator reference's Worktree Commands note covers cleaning those up.

## Notes

- **The worktree outlives the carry.** `carry_forward` reads the worktree's index, so cleanup comes last. A run stopped between steps 5 and 9 resumes from its `Resume at:` line with the worktree still there.
- **Carry-forward writes no files.** It writes only main's index, so it adds nothing to commit.
- **The MCP server runs the code it loaded.** When the run changed axiom-graph's MCP tools, ask the user to restart `/mcp` before step 6, or run step 6's build, carry and check through `{commands.axiom_graph}` (`build .`, `carry-forward {worktree_path} --dry-run`, `carry-forward {worktree_path}`, `check .`) from main.
- **Background sessions.** This step writes main (the merge, main's index, the completion commit). A background session needs `"worktree": {"bgIsolation": "none"}` in the project's Claude Code settings to do it; the rest of the run stays in its worktree.
