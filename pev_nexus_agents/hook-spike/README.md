# hook-spike

Minimal Claude Code plugin-hook test harness. Install this when a plugin hook isn't firing and you need to isolate which variable is broken (matcher shape, env-var expansion, stdin JSON field presence, cross-platform path handling).

**Recommended install alongside the [`pev`](../pev/) plugin** — step-by-step instructions in [`../pev/SETUP.md`](../pev/SETUP.md). `hook-spike` adds a 10-second smoke test (`/hs-heartbeat`) that's invaluable when debugging PEV issues: run it first to isolate "is the plugin-hook platform alive?" before assuming PEV-specific logic is at fault.

## What it provides

**Driver skills:**

| Skill | Purpose |
|---|---|
| `/hs-heartbeat` | Single-agent smoke test — one Bash call, canary files + stderr messages. *Does plugin `hooks.json` fire at all?* Run this first when anything's silent. |
| `/hook-spike` | Four-agent matrix across agent-frontmatter hooks vs `hooks.json`, `${CLAUDE_PLUGIN_ROOT}` vs `${CLAUDE_PROJECT_DIR}`, PreToolUse vs SubagentStop. Returns pass/fail table with env-var values per hook shape. |

**Bonus:** the plugin's `hooks.json` dumps every triggered hook's stdin JSON to `/tmp/hook-spike/input-<event>-<tool>.json`. Reuse those captures as realistic test inputs when debugging other plugins' hooks.

## When to reach for this

- A plugin hook appears silent (canary missing, tool wasn't blocked, env var unset)
- You're about to add a new hook and want to confirm the pattern works in your env
- You need to see the raw stdin JSON Claude Code passes to hooks (fields, shapes, encoding)
- Debugging a PEV issue and want to rule out generic plugin infra before blaming PEV-specific logic

## Troubleshooting

[`TROUBLESHOOTING.md`](./TROUBLESHOOTING.md) — symptom-first guide covering the failure catalog (Windows `cwd` handling, native jq POSIX-path bug, grep PCRE locale, matcher gotchas, stderr visibility rules) and the debug recipes that unblocked them. Also §5.5 explains the hook I/O contract in detail — worth reading once even when nothing is broken.

## Related

Companion to the [`pev`](../pev/) plugin. When a PEV hook misbehaves, debug bottom-up: `/hs-heartbeat` → `/hook-spike` → PEV-specific issue. PEV's design expectations for plugin hooks are documented in [`../pev/DESIGN.md`](../pev/DESIGN.md).
