---
name: pev-spike
description: Smoke-test all PEV hooks — creates a worktree, dispatches the spike agent with tiny budget limits, and reports pass/fail results for every hook.
user-invocable: true
---

# PEV Spike — Hook Smoke Test

Smoke-tests the PEV hook infrastructure by dispatching a test agent with all hooks enabled at tiny budget limits (warn 3, urgent 5, gate 7). The agent runs a structured checklist testing scope enforcement, budget warnings, gate blocking, and allowlist pass-through. Results are written to a JSON file and presented as a pass/fail summary.

## Protocol

### 1. Create worktree

```
EnterWorktree(name="pev-spike")
```

Record the worktree path (cwd after EnterWorktree). Record the main repo path from `git worktree list` (the first entry).

### 2. Set up environment

```bash
poetry install
```

Copy axiom-graph DB into worktree:
```
axiom_graph_checkout(
  project_root="{main_repo_path}",
  worktree_path="{worktree_path}"
)
```

### 3. Create spike manifest

Create a minimal axiom-graph doc for the spike so doc-scope has something to test against:

```
axiom_graph_write_doc(
  project_root="{worktree_path}",
  doc_json='{"title": "PEV Spike Manifest", "id": "pev/cycles/pev-spike", "tags": ["pev-cycle", "pev-spike"], "sections": [{"id": "results", "heading": "Spike Results", "content": "(spike agent writes results here)"}]}'
)
```

The doc ID for state file is: `axiom_graph::docs.pev.cycles.pev-spike`

### 4. Write `.pev-state.json`

Write to cwd (worktree root):

```json
{
  "cycle_id": "pev-spike",
  "cycle_doc_id": "axiom_graph::docs.pev.cycles.pev-spike",
  "worktree_path": "{worktree_path}"
}
```

### 5. Dispatch spike agent

Use `subagent_type="pev-spike"`. Do NOT use `isolation: "worktree"`.

Dispatch prompt:

```
You are the PEV Spike agent testing hook infrastructure for cycle pev-spike.

Cycle manifest doc ID: axiom_graph::docs.pev.cycles.pev-spike
Project root: {worktree_path}
Main repo path: {main_repo_path}

Your cwd is the worktree. Run the test checklist below IN ORDER. Record every result.

== TEST PROTOCOL ==

PHASE 1: SCOPE TESTS
These calls should be BLOCKED by scope hooks. Blocked calls do not count toward your tool budget.

Test 1 — worktree-scope: Try to Write a file OUTSIDE the worktree.
  Call: Write(file_path="{main_repo_path}/spike-test-canary.txt", content="canary")
  Expected: BLOCKED by worktree-scope hook
  Record: blocked=true/false, hook_message (the denial reason text)

Test 2 — bash-scope: Try to cd outside the worktree.
  Call: Bash(command="cd /tmp && echo escaped")
  Expected: BLOCKED by bash-scope hook
  Record: blocked=true/false, hook_message

Test 3 — axiom-graph-scope: Try a axiom-graph call with wrong project_root.
  Call: axiom_graph_source(project_root="{main_repo_path}", node_id="axiom_graph::axiom_graph")
  Expected: BLOCKED by axiom-graph-scope hook
  Record: blocked=true/false, hook_message

Test 4 — doc-scope (block): Try to write to a doc that is NOT the cycle manifest.
  Call: axiom_graph_update_section(section_id="axiom_graph::docs.features.fake::content", content="canary")
  Expected: BLOCKED by doc-scope hook
  Record: blocked=true/false, hook_message

Test 5 — doc-scope (allow): Write to the CORRECT cycle manifest.
  Call: axiom_graph_update_section(section_id="axiom_graph::docs.pev.cycles.pev-spike::results", content="Spike test in progress...")
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

Test 8 — allowlist Write: Write the results file (Write IS on the allowlist).
  Call: Write(file_path="{worktree_path}/spike-results.json", content=<your results JSON>)
  Expected: ALLOWED
  Record: allowed=true/false

Test 9 — allowlist axiom_graph_update_section: Write final results to the manifest.
  Call: axiom_graph_update_section(section_id="axiom_graph::docs.pev.cycles.pev-spike::results", content=<formatted results summary>)
  Expected: ALLOWED
  Record: allowed=true/false

== RESULTS FORMAT ==

Write spike-results.json with this structure:

{
  "spike_id": "pev-spike",
  "timestamp": "<ISO 8601>",
  "worktree_path": "{worktree_path}",
  "tests": {
    "worktree_scope": {"test": 1, "description": "Write outside worktree blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "bash_scope": {"test": 2, "description": "cd outside worktree blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "axiom_graph_scope": {"test": 3, "description": "Wrong project_root blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "doc_scope_block": {"test": 4, "description": "Write to wrong doc blocked", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "doc_scope_allow": {"test": 5, "description": "Write to cycle manifest allowed", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false},
    "budget_warning": {"test": 6, "description": "Warning advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "budget_urgent": {"test": 6, "description": "Urgent advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "budget_gate": {"test": 6, "description": "Gate advisory received", "expected": "seen", "actual": "seen|not_seen", "pass": true|false, "message": "..."},
    "gate_blocks": {"test": 7, "description": "Non-allowlisted tool blocked after gate", "expected": "blocked", "actual": "blocked|allowed", "pass": true|false, "hook_message": "..."},
    "allowlist_write": {"test": 8, "description": "Allowlisted Write works after gate", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false},
    "allowlist_axiom_graph": {"test": 9, "description": "Allowlisted axiom_graph_update_section works after gate", "expected": "allowed", "actual": "blocked|allowed", "pass": true|false}
  },
  "summary": {
    "total": 11,
    "passed": <count>,
    "failed": <count>,
    "verdict": "ALL PASS" | "FAILURES DETECTED"
  }
}

After writing the results file and updating the manifest, return:

SPIKE COMPLETE

{summary line: X/11 tests passed}

---SPIKE-RESULTS---
{the full JSON from spike-results.json}
```

### 6. Read results

After the spike agent returns, parse the `---SPIKE-RESULTS---` separator to get the JSON.

If no separator (agent was cut off), read `spike-results.json` from the worktree directly:
```bash
cat {worktree_path}/spike-results.json
```

### 7. Present results

Format the results as a table:

```
PEV Spike Results — {timestamp}

| # | Test | Expected | Actual | Result |
|---|------|----------|--------|--------|
| 1 | worktree-scope: write outside blocked | blocked | blocked | PASS |
| 2 | bash-scope: cd outside blocked | blocked | blocked | PASS |
| 3 | axiom-graph-scope: wrong project_root blocked | blocked | blocked | PASS |
| 4 | doc-scope: write to wrong doc blocked | blocked | blocked | PASS |
| 5 | doc-scope: write to cycle manifest allowed | allowed | allowed | PASS |
| 6a | budget: warning advisory received | seen | seen | PASS |
| 6b | budget: urgent advisory received | seen | seen | PASS |
| 6c | budget: gate advisory received | seen | seen | PASS |
| 7 | gate: non-allowlisted tool blocked | blocked | blocked | PASS |
| 8 | allowlist: Write works after gate | allowed | allowed | PASS |
| 9 | allowlist: axiom_graph_update_section works after gate | allowed | allowed | PASS |

Verdict: 11/11 passed — ALL PASS
```

If any tests failed, present the `hook_message` for each failure so the user can debug.

### 8. Clean up

Remove the spike manifest doc:
```bash
rm -f docs/pev/cycles/pev-spike.json
```

Clean up the worktree:
```
ExitWorktree(action="remove")
```

If ExitWorktree refuses (uncommitted files from the spike), use `discard_changes: true` — this is a test, no real work to preserve.

Clean up counter file:
```bash
rm -f /tmp/pev-spike-test-1
```
