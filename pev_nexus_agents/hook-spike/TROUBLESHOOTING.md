# Plugin-Hook Troubleshooting Guide

Reference for debugging Claude Code plugin hooks in the `axiom-graph` marketplace. Covers the two test plugins, their layering, Windows-specific pitfalls, and a catalog of failure modes we've hit and fixed.

Symptom-first where possible — scan the section headings, find what matches, read the fix.

---

## 1. Overview — two plugins, two test layers

```
axiom-graph (marketplace)
├── pev_nexus_agents/pev/         Real PEV workflow + /pev-spike (11-test integration)
└── pev_nexus_agents/hook-spike/  Low-level plugin-hook infra test harness
```

Layer cake:

- **Layer 1 — hook infrastructure**: can a plugin's `hooks.json` fire at all? Does `${CLAUDE_PLUGIN_ROOT}` expand? Does `agent_type` reach the hook? → `hook-spike`.
- **Layer 2 — PEV-specific behavior**: do the worktree/bash/doc/axiom-graph scope hooks enforce correctly? Does the tool-budget counter + allowlist gate work per-agent? → `pev-spike`.

**Always debug bottom-up.** If `/hs-heartbeat` fails, `/pev-spike` cannot meaningfully pass. Don't chase PEV hook bugs until Layer 1 is green.

---

## 2. `hook-spike` plugin

Minimal harness that isolates **one variable at a time**. Each spike agent uses a unique tool (Read / Glob / Grep / none) to avoid hooks from different tests cross-firing. Hooks use **inline commands** in `hooks.json` (no separate `.sh` files) so the thing under test is variable expansion, not file-path resolution.

### Agents

| Agent | Trigger | Hook placement | What it tests |
|---|---|---|---|
| `hs-heartbeat` | `Bash` | plugin `hooks.json` (PreToolUse + PostToolUse) | **Start here**. Does plugin hooks.json fire at all? |
| `hs-hooksjson-control` | `Read` | plugin `hooks.json` PreToolUse(Read) | Baseline: `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PROJECT_DIR}` expansion in hooks.json |
| `hs-frontmatter-plugin-root` | `Glob` | agent frontmatter | Does agent-frontmatter hook fire with `${CLAUDE_PLUGIN_ROOT}`? (Answer: no — see §7) |
| `hs-frontmatter-project-dir` | `Grep` | agent frontmatter | Does agent-frontmatter hook fire with `${CLAUDE_PROJECT_DIR}`? |
| `hs-subagent-stop` | (none) | agent frontmatter `SubagentStop` | Does frontmatter SubagentStop fire on subagent return? |

### Driver skills

| Skill | Purpose |
|---|---|
| `/hs-heartbeat` | Single-agent smoke test. Run this FIRST whenever you suspect hook infra is broken. |
| `/hook-spike` | Dispatches all four matrix agents in parallel. Reads canary files. Returns pass/fail table + what each env var expanded to. |

### Diagnostic side-effects

`hook-spike`'s `hooks.json` also dumps every hook's stdin JSON to `/tmp/hook-spike/input-<event>-<tool>.json`. You can **reuse these captures** to construct realistic hook-input JSON for testing other hooks standalone. Overwrites on each fire, so pay attention to which event you want.

Canary contents include: `fired=true`, `pwd=...`, `CLAUDE_PLUGIN_ROOT=...`, `CLAUDE_PROJECT_DIR=...`. If the var is absent, you see `<UNSET>`.

---

## 3. `pev-spike` agent (inside `pev` plugin)

Integration test for the real PEV hook behavior. Exercises the scope hooks, budget counter, gate, and allowlists end-to-end in a disposable worktree.

### 11-test protocol

| # | Test | What it exercises |
|---|---|---|
| 1 | worktree-scope: Write outside worktree blocked | `pev-worktree-scope.sh` + cwd resolution |
| 2 | bash-scope: `cd /tmp` blocked | `pev-bash-scope.sh` + `cd`-target extraction |
| 3 | axiom-graph-scope: wrong `project_root` blocked | `pev-axiom-graph-scope.sh` + path comparison |
| 4 | doc-scope: write to wrong doc blocked | `pev-doc-scope.sh` + `.pev-state.json` `cycle_doc_id` |
| 5 | doc-scope: write to cycle manifest allowed | hook allow path (axiom-graph layer may still 404 if manifest unindexed — not a hook failure) |
| 6a | budget warning advisory (3/7) | `pev-tool-counter.sh` warn threshold |
| 6b | budget urgent advisory (5/7) | `pev-tool-counter.sh` urgent threshold |
| 6c | budget gate advisory (7/7) | `pev-tool-counter.sh` + `pev-tool-gate.sh` at threshold |
| 7 | gate blocks non-allowlisted tool | `pev-tool-gate.sh` block path |
| 8 | allowlist: Write works after gate | gate allowlist pass-through |
| 9 | allowlist: `axiom_graph_update_section` works after gate | gate + doc-scope stacking |

### Driver skill

`/pev-spike` — orchestrates worktree creation, state file setup, subagent dispatch, result collection, cleanup. See `pev_nexus_agents/pev/skills/pev-spike/SKILL.md` for the exact protocol.

### Why keep pev-spike detailed

Its tests are deliberately specific. When a hook regresses, the matrix tells you *which* hook broke. This has saved hours during prior hook-regression fix arcs — each failure pointed at a single script. Do not collapse it into a smaller test.

---

## 4. How the two layers interface

**Shared infrastructure:**

- Both plugins register hooks in `pev_nexus_agents/<name>/hooks/hooks.json` at plugin root, using `${CLAUDE_PLUGIN_ROOT}/hooks/...` paths.
- Both rely on the **hook input JSON** delivered on stdin, which carries `agent_type`, `agent_id`, `cwd`, `tool_name`, `tool_input`, `hook_event_name`.
- Both write evidence under `/tmp/` — `hook-spike` uses `/tmp/hook-spike/`, `pev` uses `/tmp/pev-counter-<agent_id>.txt` and (when diagnostic logging is on) `/tmp/pev-hook-debug.log`.

**Test targeting:**

- `hook-spike` asks: *is the platform giving me what I expect?* Its hooks are inline, stateless, and as minimal as possible. If these don't work, nothing else will.
- `pev-spike` asks: *does the PEV plugin's logic correctly enforce its intended policies?* It depends on Layer 1 already being green.

**When something breaks, always** — run `/hs-heartbeat`. If the plugin hooks.json isn't firing at all for the simplest case, no amount of PEV debugging will help.

---

## 5. Hook I/O invariants

The invariants below are debugging-relevant — patterns you need to understand to interpret observed hook behavior. For a full architectural tour of how PEV uses hooks (tool permissions matrix, agent responsibilities, cycle manifest structure, `.pev/` SOPs), see [`../pev/DESIGN.md`](../pev/DESIGN.md).

### 5.1 Block signaling

- Reliable pattern: `echo "BLOCKED: reason" >&2; exit 2`
- Unreliable: `echo "{...hookSpecificOutput...}"` on stdout with no `exit 2` (see §7.4)

---

## 5.5 How Claude Code handles hook I/O

Get this model right and 80% of hook debugging clicks into place.

### The model: pipes, not files

A hook is NOT a long-running process that Claude writes state into. It's a **new bash process spawned per tool call**, wired up with pipes:

```
Claude Code process
  │
  │ (tool call triggers a matching hook)
  │
  ├─ spawns: bash ${CLAUDE_PLUGIN_ROOT}/hooks/my-hook.sh
  │             ↑
  │             │ stdin pipe (ephemeral — closes when hook exits)
  │             │
  │             └─ Claude writes JSON payload:
  │                {"session_id":"...", "agent_type":"pev:pev-spike",
  │                 "agent_id":"xyz", "cwd":"C:\\...",
  │                 "tool_name":"Bash", "tool_input":{...}, ...}
  │
  │ (hook reads stdin, runs, exits)
  │
  └─ reads: exit code + stdout + stderr
```

The stdin JSON is **constructed in memory per hook invocation and streamed to the child's stdin pipe**. Claude Code never writes it to a file from its side. When the hook exits, the pipe closes and that payload is gone. The next hook invocation gets a brand-new process with a brand-new payload.

**Fields like `agent_type` and `agent_id` are not files you look up — they're keys in the JSON payload that Claude re-sends on every invocation.**

### Lifetime of identity fields

Claude Code tracks subagent context in its own memory for the lifetime of the subagent session, and stamps that identity into each hook invocation's stdin JSON:

```
orchestrator calls Agent(subagent_type="pev-spike")
  │
  ├─ Claude spawns subagent session internally
  │   (Claude remembers: "active subagent = pev:pev-spike, agent_id = xyz")
  │
  ├─ subagent calls Bash
  │   ├─ PreToolUse hook → stdin JSON has agent_type="pev:pev-spike"
  │   └─ PostToolUse hook → stdin JSON has agent_type="pev:pev-spike"
  │
  ├─ subagent calls Read
  │   └─ PreToolUse hook → stdin JSON has agent_type="pev:pev-spike"
  │
  ├─ subagent returns
  │   └─ SubagentStop hook → stdin JSON has agent_type="pev:pev-spike"
  │       (plus last_assistant_message, agent_transcript_path)
  │
  └─ (subagent session ends; Claude discards the context)
      orchestrator's next tool call → stdin JSON has NO agent_type
```

### Three identity fields in the stdin payload

Different scopes, useful to combine:

| Field | Scope | Uniqueness |
|---|---|---|
| `session_id` | Claude Code session (shared across orchestrator + its subagents) | One per `claude` process |
| `agent_id` | Subagent invocation | One per Agent-tool call — if you dispatch `pev-spike` twice, you get two `agent_id`s |
| `agent_type` | Agent definition used | Same value for every invocation of the same agent (e.g. `pev:pev-spike`) |

This is why `pev-tool-counter.sh` uses `agent_id` for counter files: it guarantees no collision when the orchestrator happens to dispatch two instances of the same agent in one session.

### What CAN'T be passed through the stdin channel

- You can't mutate the next hook's stdin from inside this hook. Claude constructs each payload fresh.
- You can't read another hook's stdin. The pipe closes with the process.
- You can't get state that persists across invocations via env vars — every hook process starts with a fresh environment inherited from Claude Code (which inherited from your shell). `export FOO=bar` inside a hook dies with that process.

For cross-invocation state, use the filesystem (see below).

### The three output channels

| Channel | How you write to it | Who reads it |
|---|---|---|
| **stdout** (fd 1, unredirected) | `echo "x"` with no redirect | Claude Code — parses for `{"hookSpecificOutput": {...}}` |
| **stderr** (fd 2) | `echo "x" >&2` | Claude Code — surfaces as denial reason iff `exit 2` |
| **filesystem** (file redirect) | `echo "x" >> /tmp/log` or `> /tmp/log` | Only whoever inspects the file later (you) |

Shell redirects (`>>`, `>`, `2>`) reassign the underlying file descriptor *before* any bytes leave the process. So `echo "x" >> /tmp/foo` writes nothing to stdout — the bytes are routed to the file instead. Claude Code sees an empty stdout pipe.

### How Claude Code treats stderr + exit code

Claude Code uses stderr as a **reason channel**, not a log channel — it's attached to the tool-call outcome:

| Exit code | Tool outcome | What happens to stderr |
|---|---|---|
| `0` | Tool runs normally | Discarded from the agent's view |
| `2` | Tool BLOCKED | Stderr becomes the `permissionDecisionReason` shown to the agent |
| Other non-zero | Error (tool still runs, fail-open) | Logged internally, not surfaced to the agent |

Two concrete observations from this session:

- `hs-heartbeat`'s hook does `echo '*** HOOK-SPIKE: PreToolUse(Bash) fired ***' >&2` then `exit 0`. The subagent explicitly reported *"No hook messages seen"* — stderr on exit 0 is invisible.
- `pev-worktree-scope.sh` does `echo "BLOCKED: Write/Edit target '...'" >&2 ; exit 2`. The spike recorded exactly that string as the hook_message in its results — stderr on exit 2 is surfaced as the block reason.

Same mechanism. Visibility depends entirely on exit code.

### How Claude Code treats stdout

stdout is a **structured injection channel**. Claude Code parses it as the hook-output schema.

- **Valid JSON with `additionalContext`** → injected into the agent's next turn as a system reminder. `pev-tool-counter.sh` uses this for budget advisories ("TOOL BUDGET: 3/7...") — the agent sees them.
- **Valid JSON with `permissionDecision: deny`** → signals a block, but in practice only reliably triggers one when combined with `exit 2`. Use stderr + exit 2 for blocks instead.
- **Non-JSON plaintext** → reaches Claude Code but fails to parse. Silently discarded.

### Why canary files are authoritative for debugging

Because the filesystem channel is **decoupled from Claude Code's surfacing logic entirely**:

- stderr on exit 0 is invisible to the agent
- stderr on other non-zero is invisible to the agent
- stdout without valid JSON is dropped
- A file on disk is always there — you can inspect it from any process, at any time

So when you add `echo "[trace]" >> /tmp/pev-hook-debug.log 2>/dev/null` to a hook, you get tracing that is **visible to you, invisible to the agent** — no influence on agent behavior, no noise in the transcript, always retrievable. This is why canary files and debug logs are the first tool to reach for when a hook seems to "not fire."

### Output cheat sheet

Pick by intent:

| Intent | Mechanism |
|---|---|
| Block a tool call | `echo "reason" >&2; exit 2` |
| Inject a non-blocking message to the agent | `echo '{"hookSpecificOutput":{"additionalContext":"..."}}'` on stdout, exit 0 |
| Debug trace for yourself | Redirect to file: `echo "..." >> /tmp/some-log 2>/dev/null` |
| Structured artifact for other tools to read | Write a file with a known format (canary) |

### One edge case worth knowing

If a hook process crashes with "command not found" or similar (exit 127 etc.), Claude Code typically surfaces a terse transcript notice like *"Hook PreToolUse completed with non-zero exit code: 127"*. Three distinct observables to keep straight:

- **Silent absence** → hook didn't fire at all (matcher miss, plugin not loaded, or hook gate exited 0 early)
- **Crash notice in transcript** → hook fired but blew up before doing useful work
- **Denial reason in transcript** → hook fired, decided to block, used `exit 2` correctly

---

## 5.6 Environment variables and state persistence

Env vars reach your hook in two ways, and neither is magic. State that survives across invocations is a separate concern — the filesystem — because the env channel is one-way.

### The inheritance chain

```
your shell env
      │
      │ (you run `claude`)
      ▼
Claude Code process env
      │
      │ (Claude spawns a hook)
      ▼
hook process env   ← sees everything above, plus CLAUDE_* vars
                     Claude injects for this hook
```

Hooks inherit whatever your shell had when you launched Claude. So if you `export PATH=/custom:$PATH` before `claude`, hooks see that custom PATH. If you start `claude` from an Anaconda-activated shell, hooks see Anaconda env vars.

### What Claude Code sets for you

Captured by `hook-spike`'s `hooksjson-control.env` canary, here's what Claude injects (values from this session):

| Variable | Scope | Example value | When set |
|---|---|---|---|
| `CLAUDECODE` | Marker that you're inside Claude Code | `1` | Always |
| `CLAUDE_CODE_ENTRYPOINT` | How the process was invoked | `sdk-cli` (from `claude -p`) | Always |
| `CLAUDE_CODE_EXECPATH` | Path to the claude binary | `C:\Users\...\claude.exe` | Always |
| `CLAUDE_PROJECT_DIR` | Consumer project root | `C:\Users\<user>\path\to\your-project` (or `/Users/<user>/path/to/your-project` on macOS/Linux) | Always (points at the project claude was launched from) |
| `CLAUDE_PLUGIN_ROOT` | Root of the plugin this hook belongs to | `/c/Users/<user>/.claude/plugins/cache/axiom-graph/hook-spike/1.0.0` (Windows git-bash form) or `~/.claude/plugins/cache/axiom-graph/hook-spike/1.0.0` | Only for plugin-scoped hooks (those registered via `pev_nexus_agents/<name>/hooks/hooks.json`) |
| `CLAUDE_PLUGIN_DATA` | Persistent per-plugin data dir | `~/.claude/plugins/data/hook-spike-axiom-graph` | Plugin-scoped hooks only |

**Important distinction**: `CLAUDE_PLUGIN_ROOT` is *not* available in every hook — only in hooks that the plugin system invoked. If you register a hook in user-level `~/.claude/settings.json`, there's no plugin context, and `CLAUDE_PLUGIN_ROOT` is unset. Don't depend on it outside plugin hooks.

`CLAUDE_PLUGIN_DATA` is the right place for persistent per-plugin state (settings, caches) — survives session restarts, scoped to the plugin. Use `/tmp/` instead for transient per-run state (counters, debug logs) that should *not* survive.

### Adding your own custom env vars

Three mechanisms, increasing scope:

**1. One-shot per run** — prepend when launching claude:
```bash
PEV_SESSION_ID=xyz123 MSYS_NO_PATHCONV=1 claude -p "/pev-spike" ...
```
All hooks fired during that claude process see `PEV_SESSION_ID=xyz123`.

**2. Per-shell** — export first, then run claude:
```bash
export PEV_SESSION_ID=xyz123
claude       # and every subsequent claude invocation in this shell
```

**3. Per-project persistent** — settings.json `env` block:
```json
{
  "env": {
    "PEV_SESSION_ID": "xyz123",
    "PEV_DEBUG": "1"
  }
}
```
Placed in `<project>/.claude/settings.json` or `~/.claude/settings.json`. Claude Code injects these into every session launched from that scope. Good for stable per-project identifiers.

The hook itself can read them like any other env var:

```bash
SESSION_ID=${PEV_SESSION_ID:-unknown}
```

### What env CANNOT do

- **You cannot persist state between hook invocations via env.** Each hook is a fresh bash process; `export FOO=bar` inside the hook dies when the hook exits. The next hook starts with a fresh environment (the same one Claude inherited).
- **You cannot change Claude's env from inside a hook.** Claude Code doesn't re-read env from hook output — only the exit code and stdout/stderr.

For cross-invocation state, use the filesystem (next section).

### Inspecting env from inside a hook

Drop this single line into a hook during debugging to capture the full env at invocation time:

```bash
env | sort > /tmp/hook-env-$(date +%s).txt
```

Or, if you just want to check for a specific set of vars:

```bash
env | grep -E '^(CLAUDE_|PEV_)' > /tmp/hook-env.txt
```

Remember the file-redirect pattern from §5.5 — the `>` redirects fd 1 to a file before anything reaches Claude Code. No transcript noise, you read the file manually afterward.

You don't actually need to add this yourself for the initial check — `hook-spike`'s `pev-worktree-scope`-style hook already dumps env on every Read call to `/tmp/hook-spike/hooksjson-control.env`. Trigger any Read while the plugin is installed, inspect that file.

### State persistence patterns (the filesystem)

If env is one-way, and you want a counter or a "did I already do X" flag that survives across hook invocations in a session, write a file. The conventions we use in this marketplace:

| Purpose | Path pattern | Written by | Cleaned up by |
|---|---|---|---|
| Per-subagent tool counter | `/tmp/pev-counter-<agent_id>.txt` | `pev-tool-counter.sh` (PostToolUse) | `pev-subagent-stop.sh` (SubagentStop) |
| Debug trace across a full session | `/tmp/pev-hook-debug.log` | Any hook with the diagnostic line from §8.3 | User, manually |
| Hook-input JSON captures (diagnostic) | `/tmp/hook-spike/input-<event>-<tool>.json` | `hook-spike` hooks via `cat > file` | Overwritten per invocation; cleared manually |
| Canary markers | `/tmp/hook-spike/<name>.fired` | `hook-spike` hooks via `printf > file` | User, manually |

The key design choices worth copying:

- **Key on `agent_id`, not `agent_type`**, when state is per-invocation. `agent_type` collides if the orchestrator dispatches the same agent twice in one session; `agent_id` never does.
- **Clean up in SubagentStop.** `pev-subagent-stop.sh` removes the counter file when the subagent returns — keeps `/tmp/` from accumulating stale per-run files across sessions.
- **Use `/tmp/` for transient, `CLAUDE_PLUGIN_DATA` for durable.** `/tmp/` is effectively a scratchpad — OK if it's lost. `CLAUDE_PLUGIN_DATA` persists across claude restarts and is the right place for caches or per-plugin config you genuinely want preserved.
- **Prefix filenames with your plugin name** to avoid collisions between plugins sharing `/tmp/` (`pev-counter-*`, not `counter-*`).

---

## 6. Windows gotchas (explicit)

The Windows + git-bash + native-jq stack has three layers of path handling, each with its own assumptions. Here's the list we hit.

### 6.1 `cwd` in hook JSON is Windows-style

Claude Code passes `cwd` as `C:\Users\<user>\...\worktree`. Concatenating `/.pev-state.json` produces a mixed-slash path that `[ -f ]` can't resolve.

**Fix:**

```bash
PROJECT_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty')
if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
STATE_FILE="$PROJECT_ROOT/.pev-state.json"
```

### 6.2 Native Windows jq cannot open POSIX paths

Once you've cygpath'd to `/c/Users/.../pev-state.json`, `[ -f ]` accepts it, but `jq -r '...' "$STATE_FILE"` silently returns empty because the Windows `jq.exe` binary can't open `/c/...`.

**Fix:** pipe file content via `cat`. jq reads stdin, doesn't care about paths:

```bash
# Wrong
WORKTREE_PATH=$(jq -r '.worktree_path' "$STATE_FILE")

# Right
WORKTREE_PATH=$(cat "$STATE_FILE" | jq -r '.worktree_path // empty')
```

### 6.3 `grep -oP` (PCRE) requires UTF-8 locale

In the hook execution environment, `grep -oP '...'` fails with `grep: -P supports only unibyte and UTF-8 locales` and exits 2. The capture is empty and the hook falls through.

**Fix:** use POSIX `sed` instead. Example — extracting the target of a `cd` command:

```bash
# Wrong (may fail depending on locale)
CD_TARGET=$(echo "$COMMAND" | grep -oP '^\s*cd\s+\K[^\s;&]+')

# Right
CD_TARGET=$(echo "$COMMAND" | sed -n 's/^[[:space:]]*cd[[:space:]]\+\([^[:space:];&]\+\).*/\1/p')
```

### 6.4 MSYS2 path conversion mangles `claude -p "/slash-command"`

In git-bash, `claude -p "/pev-spike"` can get rewritten to `claude -p "C:/Program Files/Git/pev-spike"` before `claude` sees it, and Claude then says "what should I do with this path?" The heuristic is context-sensitive; `/hs-heartbeat` sometimes passed through and `/hook-spike` did not.

**Fix:** always prefix with `MSYS_NO_PATHCONV=1`:

```bash
MSYS_NO_PATHCONV=1 claude -p "/pev-spike" --dangerously-skip-permissions ...
```

### 6.5 Bash heredocs strip `\\` silently (debug-only workaround)

*Only relevant when constructing synthetic hook-input JSON by hand for standalone hook testing. Production hooks never hit this.*

Constructing test JSON in a `<<'EOF'` heredoc — e.g. trying to write `"cwd":"C:\\Users\\..."` — produces single backslashes in the resulting file (`C:\Users\...`), which is **invalid JSON**. jq rejects it with `Invalid escape at line 1, column N`. This appears specific to the git-bash build on this system; a `<<'EOF'` heredoc should preserve `\\` literally, but here it doesn't.

**Fix for test-input construction:** use Python `json.dump` — it escapes correctly without any shell interference:

```python
import json
with open("C:/Users/.../test-input.json", "w") as f:
    json.dump({"cwd": "C:\\Users\\...", "agent_type": "pev:pev-spike"}, f)
```

Also: Python `open("/tmp/x.json")` uses Windows Python's cwd semantics. Pass an absolute Windows path (`C:/Users/<user>/AppData/Local/Temp/x.json`) to avoid surprise.

### 6.6 `/tmp` mapping

In git-bash on Windows, `/tmp` resolves to `C:\Users\<user>\AppData\Local\Temp`. Files written to `/tmp/foo.txt` from bash are readable from the Read tool at `C:\Users\<user>\AppData\Local\Temp\foo.txt`. Use `cygpath -w /tmp/foo.txt` to confirm the Windows equivalent when debugging.

---

## 7. Failure catalog

Things we tried that didn't work, plus the evidence and the fix.

### 7.1 Agent-frontmatter hooks silently no-op in plugin installs

**Symptom:** Every hook in `hooks:` blocks of `pev_nexus_agents/<name>/agents/<agent>.md` appears to do nothing. Budgets don't enforce, scope hooks don't block, SubagentStop doesn't fire.

**Evidence:** The `/hook-spike` matrix (PR #1, #2) confirmed with canary files: `hs-hooksjson-control` (hooks.json) fires; `hs-frontmatter-*` and `hs-subagent-stop` (all in agent frontmatter) do not.

**Fix:** Move every registration to `pev_nexus_agents/<name>/hooks/hooks.json`. Per-agent behavior goes inside the hook script, dispatched on `agent_type` from stdin.

### 7.2 Bare prefix matcher doesn't fire

**Symptom:** A hook registered with `"matcher": "mcp__axiom-graph__"` does not fire for `mcp__axiom-graph__axiom_graph_source`. No error, no log entry.

**Evidence:** Absence of log entries in `/tmp/pev-hook-debug.log` for that matcher, despite the subagent clearly calling the tool.

**Fix:** Claude Code matchers appear to require full-string match. Use `"mcp__axiom-graph__.*"` or an explicit alternation like `"mcp__axiom-graph__axiom_graph_source|mcp__axiom-graph__axiom_graph_graph"`.

### 7.3 `${CLAUDE_PLUGIN_ROOT}` in agent frontmatter is unclear

**Symptom:** Plugin-scoped hooks declared in agent frontmatter with `${CLAUDE_PLUGIN_ROOT}/hooks/...` may not expand. Unclear in docs.

**Fix:** Sidestep the question. Declare plugin hooks in `hooks.json` (where `${CLAUDE_PLUGIN_ROOT}` definitely works). For agent-specific behavior, dispatch inside the script on `agent_type`.

### 7.4 `hookSpecificOutput` on stdout without `exit 2`

**Symptom:** Hook prints `{"hookSpecificOutput":{"permissionDecision":"deny",...}}` to stdout and returns 0. Claude does not block the tool.

**Evidence:** an earlier revision of `pev-bash-scope.sh` used this pattern for its block path; test 2 failed consistently even when the hook was clearly firing with the correct `CD_TARGET`.

**Fix:** Use the simpler pattern that every other working hook uses:

```bash
echo "BLOCKED: reason" >&2
exit 2
```

### 7.5 `.pev-state.json` as dispatch source races with orchestrator

**Symptom:** Every time the orchestrator makes a Bash or axiom-graph call, hooks fire but the state file's "current agent" is stale or ambiguous. An earlier orchestrator design had to "update counter_file for X phase" before every dispatch, which was fragile and would fail if the orchestrator itself called a tool mid-dispatch.

**Fix:** Use `agent_type` from hook input JSON directly. It's populated only when a subagent is executing the tool call, absent when the orchestrator is. Per-agent config branches inside the hook script. Counter files use `agent_id` (unique per invocation).

### 7.6 Hook stderr doesn't surface in subagent transcript

**Symptom:** You add `echo "debug: ..." >&2` to a hook. The subagent runs. You look at the transcript. The message is nowhere.

**Fix:** Canary files, not stderr, are the authoritative signal. Write `echo "..." >> /tmp/pev-hook-debug.log` or a canary marker file. **See §8.3 for the full re-enable recipe** — it's the most common debugging technique for misbehaving hooks.

```bash
echo "[$(date -Is)] hook=<name> pid=$$ agent_type=$AGENT_TYPE tool=$TOOL_NAME" >> /tmp/pev-hook-debug.log 2>/dev/null
```

### 7.7 Local agent files shadow plugin agents

**Symptom:** You install a plugin that provides agent `foo`, but when you dispatch it, the behavior matches an old version. `agent_type` comes through as bare `foo`, not `plugin:foo`.

**Fix:** Claude Code resolves `.claude/agents/<name>.md` before plugin-provided agents. Delete local copies when you want the plugin version. Verify by checking the `agent_type` field — plugin-loaded is `<plugin>:<agent>`; local-loaded is bare `<agent>`.

### 7.8 Stale plugin installs in other projects still count

**Symptom:** Your new plugin version doesn't take effect. `claude plugin list` shows installs at `local` or `project` scope for totally different projects — their `hooks.json` may still load globally.

**Evidence:** a debugging session showed an old cached version's `SessionStart` echo firing despite a newer version being the user-scope install.

**Fix:** `cd` into the owning project and `claude plugin uninstall <name> --scope=<scope>`. You can find the owning project by reading `~/.claude/plugins/installed_plugins.json` — each install has a `projectPath`. Do not edit that file by hand; the harness flags it as self-modification.

### 7.9 `enabledPlugins` in a project's `settings.local.json` overrides user scope

**Symptom:** You enable a plugin at user scope. Plugin list says "disabled" in a specific project.

**Fix:** Check `<project>/.claude/settings.local.json` for `"enabledPlugins": { "<name>": false }`. Flip to `true` or remove the entry. The setting is project-local and overrides the user-scope enabled flag for that project.

### 7.10 Plugin squash-merge can sweep in unrelated local commits

**Symptom:** You open a PR with one small change. The squash-merge commit message shows it brought 10 unrelated commits along.

**Cause:** Branching `feat/...` from local `main` that's ahead of `origin/main`. The PR diff includes everything between `origin/main` and your branch tip.

**Fix:** Before branching, either push `main` first, or branch from `origin/main` explicitly: `git checkout -b my-branch origin/main`. If it already happened, the squash commit content is still correct — just messier than intended.

---

## 8. Debug recipes

Concrete commands to diagnose a failing hook.

> **If you're debugging a silent/misbehaving hook, jump straight to §8.3** — the invocation-trace recipe is the fastest way to see whether a hook is firing, what `agent_type` it received, and where in the script it exited.

### 8.1 Is the plugin hook firing at all?

```bash
# In consumer project
rm -f /tmp/pev-hook-debug.log
MSYS_NO_PATHCONV=1 claude -p "/hs-heartbeat" --dangerously-skip-permissions --no-session-persistence
cat /tmp/hook-spike/heartbeat-*.fired  # look for "fired=true"
```

If the canary exists, plugin hooks.json is active. If not, the plugin isn't loaded — check `claude plugin list`, `enabledPlugins`, `installed_plugins.json`.

### 8.2 What does the hook actually receive?

Run a session that exercises the tool:

```bash
MSYS_NO_PATHCONV=1 claude -p "<whatever triggers the hook>" --dangerously-skip-permissions --no-session-persistence
cat /tmp/hook-spike/input-PreToolUse-<ToolName>.json | jq
```

`hook-spike`'s hooks.json captures every PreToolUse(Bash), PreToolUse(Read), PostToolUse(Bash), SubagentStop, Stop stdin JSON to disk. Look for `agent_type`, `agent_id`, `tool_input`.

### 8.3 Trace a specific PEV hook — re-enabling invocation logging

> **Important:** PEV hooks emit no invocation traces by default — they did at one point while the hook infrastructure was being shaken out, but the always-on log was noise in steady-state use and was removed once hooks stabilized. This recipe restores it temporarily when you need to debug a misbehaving hook. **Reach for it whenever a hook seems silent** — if the log shows no entries for the expected hook, it never fired at all; if it shows entries but the hook still passed through, the bug is in the post-gate logic.

**Setup (two minutes):**

1. Pick the hook script you want to trace. Location: `~/.claude/plugins/cache/axiom-graph/pev/<version>/hooks/<name>.sh`. On Windows git-bash: `/c/Users/<you>/.claude/plugins/cache/axiom-graph/pev/<version>/hooks/<name>.sh`.

2. Decide where to insert the echo line:

| Hook script | Insert location |
|---|---|
| `pev-worktree-scope.sh`, `pev-bash-scope.sh`, `pev-doc-scope.sh`, `pev-axiom-graph-scope.sh` | Right after `INPUT=$(cat)` and before the `# Gate: PEV subagents only` comment |
| `pev-tool-counter.sh`, `pev-tool-gate.sh` | Right after the `AGENT_TYPE=...`, `AGENT_ID=...` (and `TOOL_NAME=...` for gate) extraction block |
| `pev-subagent-stop.sh` | Right after `AGENT_TYPE=...`, `AGENT_ID=...` extraction |

3. Insert the appropriate line — place it **before** the `case "$AGENT_TYPE" in pev:*) ;;` gate so you see invocations from non-PEV agents and the orchestrator too. Otherwise the hook exits 0 before logging and you won't see why.

**Scope hooks** (before the agent_type gate reads `$INPUT`):

```bash
echo "[$(date -Is 2>/dev/null || echo now)] hook=<name> pid=$$ agent_type=$(echo "$INPUT" | jq -r '.agent_type // "<empty>"' 2>/dev/null) tool=$(echo "$INPUT" | jq -r '.tool_name // "<empty>"' 2>/dev/null) event=$(echo "$INPUT" | jq -r '.hook_event_name // "<empty>"' 2>/dev/null)" >> /tmp/pev-hook-debug.log 2>/dev/null
```

Replace `<name>` with the script's short name (e.g., `worktree-scope`).

**tool-counter / tool-gate / subagent-stop** (AGENT_TYPE and AGENT_ID already extracted into variables — use them):

```bash
echo "[$(date -Is 2>/dev/null || echo now)] hook=<name> pid=$$ agent_type=${AGENT_TYPE:-<empty>} agent_id=${AGENT_ID:-<empty>} tool=${TOOL_NAME:-$(echo "$INPUT" | jq -r '.tool_name // "<empty>"' 2>/dev/null)} event=$(echo "$INPUT" | jq -r '.hook_event_name // "<empty>"' 2>/dev/null)" >> /tmp/pev-hook-debug.log 2>/dev/null
```

(For `subagent-stop`, drop the `tool=...` segment — there's no tool name for a SubagentStop event.)

**Using the log:**

```bash
# Clear it before the run
rm -f /tmp/pev-hook-debug.log

# Run whatever scenario reproduces the problem
MSYS_NO_PATHCONV=1 claude -p "/pev-spike" --dangerously-skip-permissions --no-session-persistence

# Inspect
cat /tmp/pev-hook-debug.log
grep 'hook=bash-scope' /tmp/pev-hook-debug.log
grep 'agent_type=pev:pev-builder' /tmp/pev-hook-debug.log
```

You'll see every hook invocation with its agent context, tool name, and event. A missing hook entry means the hook didn't fire at all (matcher miss, plugin not loaded, or gate exited 0 too early). An entry present but followed by no block means the post-gate logic has a bug.

**Cleanup after debugging:**

Remove the lines you added. Either manually, or since you're editing an installed plugin copy, you can force-reinstall:

```bash
claude plugin uninstall pev --scope=user
claude plugin install pev@axiom-graph
```

That resets the installed copy to the marketplace version (no debug lines). A future version of this plugin may ship a `PEV_DEBUG=1` env-var gate so this is a one-line flip — for now it's a manual edit.

### 8.4 Test a hook standalone with real input

```bash
# 1. Capture real hook input by running any scenario with hook-spike active
cp /tmp/hook-spike/input-PreToolUse-Bash.json /tmp/real-input.json

# 2. Edit with jq if you want to change the scenario
jq '.tool_input.command = "cd /tmp && echo escaped" | .agent_type = "pev:pev-spike"' \
  /tmp/real-input.json > /tmp/test-input.json

# 3. Pipe into the hook script under test
cat /tmp/test-input.json | bash pev_nexus_agents/pev/hooks/pev-bash-scope.sh
echo "EXIT=$?"
```

### 8.5 Construct synthetic input (when you don't have a real capture)

Use Python, not bash heredocs:

```python
import json
payload = {
    "session_id": "test",
    "cwd": "C:\\Users\\<user>\\...\\worktree",
    "agent_type": "pev:pev-spike",
    "agent_id": "test-id",
    "tool_name": "Write",
    "tool_input": {"file_path": "C:/...", "content": "x"},
    "hook_event_name": "PreToolUse",
}
with open("C:/Users/<user>/AppData/Local/Temp/test-input.json", "w") as f:
    json.dump(payload, f)
```

Then `cat /tmp/test-input.json | bash <hook>`.

---

## 9. Extending — adding a new hook

Five-step checklist:

1. **Pick the event and matcher.** `PreToolUse`, `PostToolUse`, `SubagentStop`, `Stop`. Matcher is a regex on tool name; use full-string patterns (`.*` at the end if you want a prefix).
2. **Create the script at `pev_nexus_agents/<name>/hooks/<script>.sh`.** Make it executable.
3. **First four lines of the script:**
   ```bash
   #!/bin/bash
   INPUT=$(cat)
   AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null)
   case "$AGENT_TYPE" in pev:*) ;; *) exit 0 ;; esac
   ```
   (Gate on your plugin's agent prefix; pass-through for anything else.)
4. **Read state via stdin, not by path.** When you need to read a file whose path came from hook input, `cygpath -u` the path for bash, then `cat file | jq` for jq.
5. **Block with `echo "msg" >&2; exit 2`.** Don't rely on stdout JSON alone.
6. **Register in `hooks.json`:**
   ```json
   {
     "matcher": "YourMatcher.*",
     "hooks": [
       {
         "type": "command",
         "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/<script>.sh\"",
         "timeout": 5
       }
     ]
   }
   ```
7. **Smoke-test via `/hs-heartbeat`.** If the infra breaks, you'll know immediately.

---

## 10. Version history

Moved to [`../../CHANGELOG.md`](../../CHANGELOG.md) (repo root). Keep a Changelog format, full release notes per version, useful for reasoning about regressions.
