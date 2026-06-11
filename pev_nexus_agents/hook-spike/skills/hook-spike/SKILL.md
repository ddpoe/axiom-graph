---
name: hook-spike
description: Dispatches the 4 hook-spike test agents in parallel, reads canary files from /tmp/hook-spike/, and reports a pass/fail matrix showing which hook patterns resolved and what ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PROJECT_DIR} expanded to.
user-invocable: true
---

# Hook Spike — Plugin Hook Resolution Test

Tests 4 hook-declaration patterns to determine how Claude Code resolves plugin hooks:

| # | Agent | Hook location | Variable under test |
|---|-------|---------------|---------------------|
| 1 | `hs-hooksjson-control` | plugin `hooks.json` PreToolUse(Read) | `${CLAUDE_PLUGIN_ROOT}` (documented baseline) |
| 2 | `hs-frontmatter-plugin-root` | agent frontmatter PreToolUse(Glob) | `${CLAUDE_PLUGIN_ROOT}` (the unknown) |
| 3 | `hs-frontmatter-project-dir` | agent frontmatter PreToolUse(Grep) | `${CLAUDE_PROJECT_DIR}` in frontmatter |
| 4 | `hs-subagent-stop` | agent frontmatter SubagentStop | `${CLAUDE_PLUGIN_ROOT}` on agent return |

Each hook uses an **inline** `command` (no separate .sh file) that dumps env vars and writes a canary marker. This isolates *variable expansion* from *file-path resolution*.

## Protocol

### 1. Clear canary dir

```bash
rm -rf /tmp/hook-spike && mkdir -p /tmp/hook-spike
```

### 2. Dispatch all 4 agents in parallel

Send a single message with 4 Agent tool calls (parallel dispatch). For each agent:

- `subagent_type`: the agent name (`hs-hooksjson-control`, `hs-frontmatter-plugin-root`, `hs-frontmatter-project-dir`, `hs-subagent-stop`)
- `description`: short (3-5 word) label
- `prompt`: "Run the test. Follow your agent instructions exactly."

Each agent returns a `DONE` string — no output parsing needed. We read canaries from `/tmp/hook-spike/` instead.

### 3. Read canary files

```bash
ls -la /tmp/hook-spike/
for f in /tmp/hook-spike/*.fired; do echo "=== $f ==="; cat "$f"; done
```

Expected canary filenames:
- `hooksjson-control.fired`
- `frontmatter-plugin-root.fired`
- `frontmatter-project-dir.fired`
- `subagent-stop.fired`

Each contains:
```
fired=true
pwd=<path>
CLAUDE_PLUGIN_ROOT=<value or <UNSET>>
CLAUDE_PROJECT_DIR=<value or <UNSET>>
```

### 4. Report

Present a pass/fail matrix:

```
Hook Spike Results — <timestamp>

| # | Test                          | Canary | CLAUDE_PLUGIN_ROOT           | CLAUDE_PROJECT_DIR | Verdict |
|---|-------------------------------|--------|------------------------------|--------------------|---------|
| 1 | hooks.json PreToolUse(Read)   | fired  | /path/to/plugin              | /path/to/project   | PASS    |
| 2 | frontmatter PreToolUse(Glob)  | fired  | /path/to/plugin              | /path/to/project   | PASS    |
| 3 | frontmatter PreToolUse(Grep)  | fired  | <UNSET>                      | /path/to/project   | PASS    |
| 4 | frontmatter SubagentStop      | ???    | ???                          | ???                | ???     |
```

**Interpretation:**
- **Canary fired + `${CLAUDE_PLUGIN_ROOT}` populated** → that pattern is safe to use in the PEV plugin.
- **Canary fired + `${CLAUDE_PLUGIN_ROOT}=<UNSET>`** → hook fires but the variable isn't available. Real plugins using `${CLAUDE_PLUGIN_ROOT}/hooks/foo.sh` would fail to resolve the script path. Must fall back to `${CLAUDE_PROJECT_DIR}` or move hook registration to `hooks.json`.
- **Canary did not fire** → that hook declaration pattern doesn't execute at all in this environment. Unusable.

### 5. Recommendation

Based on the matrix, recommend the fix for the `axiom-graph` plugin marketplace:

- If row 2 passes with `${CLAUDE_PLUGIN_ROOT}` populated: update `pev_nexus_agents/pev/agents/*.md` to use `${CLAUDE_PLUGIN_ROOT}/hooks/...` — done.
- If row 2 canary fires but variable is `<UNSET>`: move all frontmatter hooks into `hooks.json` OR use a `${CLAUDE_PROJECT_DIR}` pattern with a SessionStart auto-copy hook.
- If row 2 canary doesn't fire at all: agent-frontmatter hooks don't run at all in this build of Claude Code — everything must live in `hooks.json`.

### 6. Cleanup

```bash
rm -rf /tmp/hook-spike
```
