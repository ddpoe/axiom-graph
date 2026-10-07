---
name: pev-spike
description: Smoke-test all PEV hooks — creates a worktree, dispatches the spike agent with tiny budget limits, and reports pass/fail results for every hook.
user-invocable: true
---

# PEV Spike — Hook Smoke Test

Smoke-tests the PEV hook infrastructure by dispatching a test agent with all hooks enabled at tiny budget limits (warn 3, urgent 5, gate 7). The agent runs a structured checklist testing scope enforcement (including the session-scratchpad carve-out), budget warnings, gate blocking, allowlist pass-through and the hand-back after the gate. Results are written to a JSON file and presented as a pass/fail summary.

## Protocol

### 0. Check for jq

Run `bash --noprofile --norc -c 'command -v jq'` (no profile — hooks run without one). If it prints nothing, **stop here** and report a single finding instead of running the matrix:

```
PEV Spike — step 0 FAILED: jq is not on PATH, so every PEV hook is inert.
The hooks deny PEV subagent tool calls with an install hint rather than
enforcing anything; the 13-test matrix would only measure that.
Install jq (Windows: winget install jqlang.jq; Ubuntu/Debian: sudo apt install jq;
macOS: brew install jq), restart Claude Code, and re-run /pev-spike.
```

`/hs-heartbeat` passing does not rule this out: hook-spike's hooks don't use jq.

### 1. Create worktree

```
EnterWorktree(name="pev-spike")
```

Record the worktree path (cwd after EnterWorktree). Record the main repo path from `git worktree list` (the first entry).

Call `axiom_graph_guide`, then `axiom_graph_info(project_root="{main_repo_path}")`, before any other axiom-graph call. Record `info`'s project id as `{project_id}`, its `docs_dirs` and its `docs_extensions`. Every doc id, path and file extension below comes from these answers or from a tool result; never hard-code a project id, a docs folder or a doc extension.

Record the canary path for Test 1: `pev-spike-canary.txt` in the **parent directory** of the main repo path (e.g. main repo `/home/me/git/demo` gives `/home/me/git/pev-spike-canary.txt`). It lies outside the main checkout, so Claude Code's own worktree isolation (which refuses writes to the main checkout's files) lets the call through, and outside the worktree and the session scratchpad, so `pev-worktree-scope.sh` must block it.

Record the scratchpad path for Test 8: `pev-spike-scratch.txt` inside the scratchpad directory your system prompt names for this session (`<temp root>/claude[-<uid>]/<project slug>/<session_id>/scratchpad/`). `pev-worktree-scope.sh` allows a write there, keyed on the hook input's `session_id`, and the spike agent runs under this session's id. If your system prompt names no scratchpad directory, drop Test 8 from the dispatch and from the results table, and report it as not run.

### 2. Set up environment

**Install dependencies only when the project declares an install command:** `commands.install` in `.pev/sops.toml`, or the install command the project's own setup docs name. Run it as one plain Bash call:
```bash
{commands.install}
```
If the project declares none, skip the install. Never guess a package manager: `poetry install` in a non-Poetry repo creates a virtualenv and writes a `poetry.lock` into the worktree. The spike tests hooks, not the project's code, so it needs no dependencies beyond the axiom-graph index.

Copy axiom-graph DB into worktree:
```
axiom_graph_checkout(
  project_root="{main_repo_path}",
  worktree_path="{worktree_path}"
)
```

### 3. Create spike manifest

A cycle can exist in either layout: a single file `<docs root>/pev/cycles/<id><ext>` or a directory `<docs root>/pev/cycles/<id>/` (skip `pev/cycles/efficiency/`, which holds efficiency reports, not cycles). Before creating the spike manifest, check every `<docs root>` in `info`'s `docs_dirs`: neither `pev/cycles/pev-spike<ext>`, for each `<ext>` in `info`'s `docs_extensions`, nor `pev/cycles/pev-spike/` may exist; a leftover from an interrupted spike in either layout would make the doc-scope tests check the wrong doc. Remove it as in step 8 first.

The spike manifest itself stays single-file for now: a directory-layout spike needs `axiom_graph_clone_doc`, which is not wired into the spike yet.

Create a minimal axiom-graph doc for the spike so doc-scope has something to test against:

```
axiom_graph_write_doc(
  project_root="{worktree_path}",
  doc_json='{"title": "PEV Spike Manifest", "id": "pev/cycles/pev-spike", "tags": ["pev-cycle", "pev-spike"], "sections": [{"id": "results", "heading": "Spike Results", "content": "(spike agent writes results here)"}]}'
)
```

The doc ID for the state file is the `doc id` line of the `write_doc` result; record it as `{spike_doc_id}` (it starts with `{project_id}::`), and record the file path the result reports. Steps 4 and 5 use that id, and step 8 deletes the manifest by the id and the path.

### 4. Write `.pev-state.json`

Write to cwd (worktree root):

```json
{
  "cycle_id": "pev-spike",
  "cycle_doc_id": "{spike_doc_id}",
  "worktree_path": "{worktree_path}"
}
```

### 5. Dispatch spike agent

Use `subagent_type="pev-spike"`. Do NOT use `isolation: "worktree"`.

Dispatch prompt:

```
You are the PEV Spike agent testing hook infrastructure for cycle pev-spike.

Cycle manifest doc ID: {spike_doc_id}
Project root: {worktree_path}
Main repo path: {main_repo_path}
Scratchpad path: {scratchpad_path}

Your cwd is the worktree. Run the test checklist below IN ORDER. Record every result.

== TEST PROTOCOL ==

PHASE 1: SCOPE TESTS
These calls should be BLOCKED by scope hooks. Blocked calls do not count toward your tool budget.

Test 1 — worktree-scope: Try to Write a file OUTSIDE the worktree, the main checkout and your scratchpad.
  Call: Write(file_path="{canary_path}", content="canary")
  Expected: BLOCKED by the PEV worktree-scope hook, whose message starts "BLOCKED: Write/Edit target".
  Record: blocked=true/false, hook_message (the denial reason text), blocked_by:
    "pev-hook" when the message starts "BLOCKED: Write/Edit target";
    "native" when it is Claude Code's own isolation text ("This session is isolated in the worktree…"),
    which means the PEV hook never ran: Test 1 FAILS as inconclusive;
    "none" when the Write succeeded.

Test 2 — bash-scope: Try to cd outside the worktree.
  Call: Bash(command="cd /tmp && echo escaped")
  Expected: BLOCKED by bash-scope hook
  Record: blocked=true/false, hook_message

Test 3 — axiom-graph-scope: Try a axiom-graph call with wrong project_root.
  Call: axiom_graph_source(project_root="{main_repo_path}", node_id="{spike_doc_id}")
  Expected: BLOCKED by axiom-graph-scope hook
  Record: blocked=true/false, hook_message

Test 4 — doc-scope (block): Try to write to a doc that is NOT the cycle manifest.
  Call: axiom_graph_update_section(project_root="{worktree_path}", section_id="{project_id}::not-the-spike-doc::content", content="canary")
  Expected: BLOCKED by doc-scope hook
  Record: blocked=true/false, hook_message

Test 5 — doc-scope (allow): Write to the CORRECT cycle manifest.
  Call: axiom_graph_update_section(project_root="{worktree_path}", section_id="{spike_doc_id}::results", content="Spike test in progress...")
  Expected: ALLOWED (this counts as tool call 1)
  Record: allowed=true/false

PHASE 2: BUDGET TESTS
Make real tool calls to burn through budget. Track the hook advisory messages that appear after each call.

Test 6 — budget calls: Make Read calls to burn budget. Read any small file in the worktree (e.g., pyproject.toml, axiom-graph.toml, or any .py file).
  Calls: Read the same file repeatedly until you have made 7 total tool calls (including the 1 from Test 5).
  After each call, check if you received a hook advisory message containing "TOOL BUDGET".
  Record for each threshold:
    - warning_seen: true/false (should appear around call 3)
    - warning_message: the full advisory text
    - urgent_seen: true/false (should appear around call 5)
    - urgent_message: the full advisory text
    - gate_seen: true/false (should appear around call 7)
    - gate_message: the full advisory text

PHASE 3: GATE + ALLOWLIST TESTS
After the gate activates (7 tool calls), test that non-allowlisted tools are blocked and allowlisted tools still work.

Test 7 — gate blocks non-allowlisted: Try to Read a file.
  Call: Read(file_path="{worktree_path}/pyproject.toml")
  Expected: BLOCKED by tool-gate hook (Read is not on the allowlist)
  Record: blocked=true/false, hook_message

Test 8 — worktree-scope (allow scratchpad): Write into the session scratchpad. Write is on the allowlist,
  so the gate lets it through and only the worktree-scope hook decides.
  Call: Write(file_path="{scratchpad_path}", content="scratch")
  Expected: ALLOWED (the hook's one carve-out outside the worktree)
  Record: allowed=true/false, hook_message (the denial reason text, if blocked)

Test 9 — allowlist Write: Write the results file (Write IS on the allowlist).
  Call: Write(file_path="{worktree_path}/spike-results.json", content=<your results JSON>)
  Expected: ALLOWED
  Record: allowed=true/false

Test 10 — allowlist axiom_graph_update_section: Write final results to the manifest.
  Call: axiom_graph_update_section(project_root="{worktree_path}", section_id="{spike_doc_id}::results", content=<formatted results summary>)
  Expected: ALLOWED
  Record: allowed=true/false

PHASE 4: HAND-BACK AFTER THE GATE

Test 11 — hand-back: Your return below comes after the gate. Make no other tool call after Test 10. If you
  have a SubagentHandback tool, deliver your final report with it; otherwise end with the report as text.
  Expected: ALLOWED on the first attempt (the gate never blocks the hand-back, and it is not counted).
  You cannot record this one: write it into the JSON as "actual": "pending", "pass": null. The orchestrator
  scores it when your report arrives.

== RESULTS FORMAT ==

Write spike-results.json with this structure:

{
  "spike_id": "pev-spike",
  "timestamp": "<ISO 8601>",
  "worktree_path": "{worktree_path}",
  "tests": {
    "worktree_scope": {"test": 1, "description": "Write outside worktree blocked by the PEV hook", "expected": "blocked", "actual": "blocked|allowed", "blocked_by": "pev-hook|native|none", "pass": true|false, "hook_message": "..."},
    "bash_scope": {"test": 2, "description": "cd outside worktree blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "axiom_graph_scope": {"test": 3, "description": "Wrong project_root blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "doc_scope_block": {"test": 4, "description": "Write to wrong doc blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "doc_scope_allow": {"test": 5, "description": "Write to cycle manifest allowed", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false},
    "budget_warning": {"test": 6, "description": "Warning advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "budget_urgent": {"test": 6, "description": "Urgent advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "budget_gate": {"test": 6, "description": "Gate advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "gate_blocks": {"test": 7, "description": "Non-allowlisted tool blocked after gate", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "scratchpad_allow": {"test": 8, "description": "Write into the session scratchpad allowed", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "allowlist_write": {"test": 9, "description": "Allowlisted Write works after gate", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false},
    "allowlist_axiom_graph": {"test": 10, "description": "Allowlisted axiom_graph_update_section works after gate", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false},
    "handback_after_gate": {"test": 11, "description": "Hand-back after the gate allowed", "expected": "allowed", "actual": "pending", "pass": null}
  },
  "summary": {
    "total": 13,
    "passed": <count, of the 12 you scored>,
    "failed": <count>,
    "pending": 1,
    "verdict": "ALL PASS" | "FAILURES DETECTED"
  }
}

Test 1 passes only when blocked_by is "pev-hook".

After writing the results file and updating the manifest, return (Test 11):

SPIKE COMPLETE

{summary line: X/12 scored tests passed, test 11 pending}

---SPIKE-RESULTS---
{the full JSON from spike-results.json}
```

### 6. Read results

After the spike agent returns, parse the `---SPIKE-RESULTS---` separator to get the JSON.

If no separator (agent was cut off), read `spike-results.json` from the worktree directly:
```bash
cat {worktree_path}/spike-results.json
```

**Score Test 11 (hand-back after the gate).** It passes when the agent's report reached you with the `---SPIKE-RESULTS---` separator: the agent handed back after the gate had activated. It fails when the report is missing but `spike-results.json` exists (the hand-back was refused or the agent ran out of turns retrying it); quote any denial text from the agent's last message. Set its `actual` and `pass`, then add it to `passed`/`failed` and drop `pending`.

### 7. Present results

Format the results as a table:

```
PEV Spike Results — {timestamp}

| # | Test | Expected | Actual | Result |
|---|------|----------|--------|--------|
| 1 | worktree-scope: write outside blocked by the PEV hook | blocked | blocked (pev-hook) | PASS |
| 2 | bash-scope: cd outside blocked | blocked | blocked | PASS |
| 3 | axiom-graph-scope: wrong project_root blocked | blocked | blocked | PASS |
| 4 | doc-scope: write to wrong doc blocked | blocked | blocked | PASS |
| 5 | doc-scope: write to cycle manifest allowed | allowed | allowed | PASS |
| 6a | budget: warning advisory received | seen | seen | PASS |
| 6b | budget: urgent advisory received | seen | seen | PASS |
| 6c | budget: gate advisory received | seen | seen | PASS |
| 7 | gate: non-allowlisted tool blocked | blocked | blocked | PASS |
| 8 | worktree-scope: write into the session scratchpad allowed | allowed | allowed | PASS |
| 9 | allowlist: Write works after gate | allowed | allowed | PASS |
| 10 | allowlist: axiom_graph_update_section works after gate | allowed | allowed | PASS |
| 11 | hand-back after the gate allowed | allowed | allowed | PASS |

Verdict: 13/13 passed — ALL PASS
```

If any tests failed, present the `hook_message` for each failure so the user can debug. A Test 1 blocked by Claude Code's own isolation (`blocked_by: native`) means the canary path was inside the main checkout or the platform's guard has widened; the PEV hook went untested. A Test 8 block means the scratchpad carve-out no longer matches this platform's scratchpad path or session id.

### 8. Clean up

Delete the spike manifest by the id the `write_doc` result reported in step 3:
```
axiom_graph_delete_doc(project_root="{worktree_path}", doc_id="{spike_doc_id}")
```
If that fails, delete the file at the path the `write_doc` result reported, as one plain `rm <relative path>` call from the worktree root. A leftover found in step 3 goes the same way: `axiom_graph_delete_doc` on its indexed id (a directory-layout leftover has one doc per file under it). A leftover the index doesn't hold is removed by the path step 3 found it at: a docs root from `info`'s `docs_dirs` plus `pev/cycles/pev-spike` and one of `info`'s `docs_extensions`. Never delete by a guessed path.

If Test 1 was not blocked, the canary file exists: delete it with one plain `rm "{canary_path}"` call and report the failure.

Clean up the worktree:
```
ExitWorktree(action="remove")
```

If ExitWorktree refuses (uncommitted files from the spike), use `discard_changes: true` — this is a test, no real work to preserve.

The budget counter needs no cleanup: the plugin's `SubagentStop` hook removes the spike agent's counter file when it returns.
