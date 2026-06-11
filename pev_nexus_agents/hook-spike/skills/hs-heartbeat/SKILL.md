---
name: hs-heartbeat
description: One-shot hook smoke test — dispatches the hs-heartbeat subagent, which makes one Bash call. Reports whether the plugin's PreToolUse and PostToolUse hooks fired, and what ${CLAUDE_PLUGIN_ROOT} / ${CLAUDE_PROJECT_DIR} expanded to. Use this first before hook-spike (the full matrix) — if heartbeat doesn't fire, nothing will.
user-invocable: true
---

# Heartbeat — Minimum Hook Smoke Test

Simplest possible test that the plugin's `hooks.json` hooks fire at all in this Claude Code install. If this fails, plugin hooks don't work in your environment, period — don't bother with the full spike matrix until this passes.

## Protocol

### 1. Clear canaries

```bash
rm -f /tmp/hook-spike/heartbeat-pre.fired /tmp/hook-spike/heartbeat-post.fired
mkdir -p /tmp/hook-spike
```

### 2. Dispatch hs-heartbeat

```
Agent(
  subagent_type="hs-heartbeat",
  description="Hook heartbeat smoke test",
  prompt="Run the heartbeat test per your agent instructions. Make exactly one Bash call, then return. Quote any '*** HOOK-SPIKE: ***' lines you see in the Bash stderr output."
)
```

### 3. Read canaries + agent output

```bash
ls -la /tmp/hook-spike/heartbeat-*.fired 2>/dev/null
for f in /tmp/hook-spike/heartbeat-*.fired; do [ -f "$f" ] && { echo "=== $f ==="; cat "$f"; }; done
```

### 4. Report

```
Hook Heartbeat Results

| Event              | Canary fired | CLAUDE_PLUGIN_ROOT  | CLAUDE_PROJECT_DIR | Message visible in transcript |
|--------------------|--------------|---------------------|--------------------|-------------------------------|
| PreToolUse(Bash)   | yes/no       | <value or UNSET>    | <value or UNSET>   | yes/no                        |
| PostToolUse(Bash)  | yes/no       | <value or UNSET>    | <value or UNSET>   | yes/no                        |

Verdict:
  - BOTH fired → plugin hooks.json is fully wired. Proceed to /hook-spike matrix.
  - ONE fired → partial support; investigate which event works.
  - NEITHER fired → plugin hooks.json isn't being applied. Check:
      * Plugin is installed AND enabled (`claude plugin list`)
      * Marketplace was re-added after recent push (`/plugin marketplace update ddpoe/axiom-graph`)
      * Claude Code version supports plugin hooks
```

Quote the agent's echoed `*** HOOK-SPIKE: ... ***` lines (if any) directly in the report so the user can see the raw evidence.
