# AGENTS.md

Orientation for agents and contributors working **on** this marketplace (extending plugins, modifying skills, authoring new hooks). If you just want to *use* PEV in a consumer project, see [`pev_nexus_agents/pev/USER_GUIDE.md`](./pev_nexus_agents/pev/USER_GUIDE.md) instead.

**GitHub**: https://github.com/ddpoe/axiom-graph (this directory: `pev_nexus_agents/` inside the axiom monorepo). Old repo `ddpoe/pev-agent-nexus` archived per ADR-005 Phase 5.

## What this repo is

A Claude Code plugin marketplace hosting two plugins:

- **`pev`** — Plan-Execute-Validate agent workflow for structured code changes. Two cycle shapes (`/pev-cycle` full, `/pev-instance` slim), axiom-graph integration, project-customizable via `.pev/` SOPs.
- **`hook-spike`** — Minimal plugin-hook test harness. Debugs plugin infrastructure (matcher mismatches, env-var expansion, stdin JSON shape, cross-platform path handling). Companion to `pev` and useful for any Claude Code plugin work.

## Layout

```
axiom-graph/                                  ← monorepo root (axiom_graph, axiom_annotations, pev_nexus_agents)
├── .claude-plugin/marketplace.json           ← plugin registry
└── pev_nexus_agents/
    ├── AGENTS.md                             ← you are here
    ├── CHANGELOG.md                          ← plugin release history (prefixed tags: pev-v*, hook-spike-v*)
    ├── README.md                             ← plugins landing
    ├── pev/
    │   ├── README.md                         ← PEV plugin landing
    │   ├── USER_GUIDE.md                     ← how to use /pev-cycle + /pev-instance
    │   ├── DESIGN.md                         ← architecture, tool permissions, hook model
    │   ├── .claude-plugin/plugin.json
    │   ├── agents/                           ← pev-architect, pev-builder, pev-reviewer,
    │   │                                       pev-auditor, pev-doc-reviewer, pev-spike
    │   ├── skills/                           ← pev-cycle, pev-instance, per-agent skills
    │   ├── hooks/                            ← hooks.json + 7 shell scripts
    │   └── templates/                        ← DocJSON starters for .pev/ SOPs
    └── hook-spike/
        ├── README.md                         ← harness landing
        ├── TROUBLESHOOTING.md                ← symptom→fix debug reference
        ├── agents/                           ← 5 test agents
        ├── skills/                           ← hs-heartbeat, hook-spike driver
        └── hooks/hooks.json
```

## Where to look when…

| Task | Start here |
|---|---|
| **Setting up PEV in a consumer project (fresh install or upgrade)** | `pev_nexus_agents/pev/SETUP.md` — step-by-step install + migration commands |
| Debugging a plugin hook that's silent / not firing | `pev_nexus_agents/hook-spike/TROUBLESHOOTING.md` §7 (failure catalog) and §8.3 (re-enable trace recipe) |
| Understanding the PEV architecture before modifying it | `pev_nexus_agents/pev/DESIGN.md` |
| Using PEV in a consumer project (after setup) | `pev_nexus_agents/pev/USER_GUIDE.md` |
| Adding a new hook to the `pev` plugin | `pev_nexus_agents/hook-spike/TROUBLESHOOTING.md` §9 (5-step checklist) then `pev_nexus_agents/pev/DESIGN.md` (hook invariants) |
| Adding a new SOP file (`.pev/<new>.json`) | `pev_nexus_agents/pev/DESIGN.md` (SOP extension rules) |
| Reasoning about a regression or comparing against a known-good version | `CHANGELOG.md` |
| Testing a change to PEV's hook behavior | `pev_nexus_agents/pev/skills/pev-spike/SKILL.md` (11-test integration) |
| Testing a change to plugin infrastructure broadly | `pev_nexus_agents/hook-spike/skills/*/SKILL.md` (hook-spike matrix + heartbeat) |

## Key concepts (one line each)

- **`/pev-cycle`** — full five-phase workflow (Architect → Builder → Reviewer → Auditor → Doc Reviewer) with human approval gates, runs in an isolated worktree
- **`/pev-instance`** — slim single-agent cycle for small tasks, runs in the working tree, writes a checkin doc to `docs/pev/instances/`
- **`.pev/` SOPs** — DocJSON files in consumer repos that customize PEV per project (`doc-topology.json`, `test-policy.json`, `review-criteria.json`). Plugin falls back to templates at `${CLAUDE_PLUGIN_ROOT}/templates/` when the project file is absent.
- **Cycle manifest** — per-cycle DocJSON at `docs/pev/cycles/{id}.json` carrying the pitch, build plan, review findings, and impact report
- **`agent_type` dispatch** — PEV hooks read `agent_type` from stdin JSON (value: `pev:pev-<role>`) to branch per-agent budget/allowlist logic in shared scripts
- **`axiom_graph_workflow_list(steps=true)`** — authoritative "developer-declared core mechanisms" signal used by Reviewer Pass 5c/5d and `/pev-instance` escalation
- **hook-spike as Layer 1** — if plugin infrastructure is broken, `hook-spike` isolates which variable (matcher, env expansion, stdin shape, exit code). Run `/hs-heartbeat` before blaming PEV-specific logic.

## Don't do this

- **Don't put per-agent config in `.pev-state.json`** — it races with orchestrator tool calls. Dispatch on `agent_type` from stdin JSON instead (see `DESIGN.md`).
- **Don't declare hooks in agent-frontmatter `hooks:` blocks** — they silently no-op in marketplace installs. Register in `pev_nexus_agents/<name>/hooks/hooks.json` only. (This was the v1.8.0 fix.)
- **Don't use `jq -r '...' "$FILE"`** in hook scripts on Windows — native Windows jq can't open POSIX paths. Use `cat "$FILE" | jq -r '...'`. (This was the v1.8.3 fix.)
- **Don't `grep -oP`** in hook scripts — PCRE needs UTF-8 locale which isn't always set. Use POSIX `sed`. (v1.8.5 fix.)
- **Don't use bare matchers like `"matcher": "mcp__axiom_graph__"`** — Claude Code matchers are full-string. Use `"mcp__axiom_graph__.*"`. (v1.8.5 fix.)
- **Don't ship debug-logging echoes in committed hooks** — add them temporarily per the recipe in `TROUBLESHOOTING.md` §8.3, remove before commit.
- **Don't create `.claude/` mirrors** of plugin content in the repo — they shadow the plugin install and silently change `agent_type` format. The plugin is the canonical source.

## Conventions

- Branch naming: `feat/...`, `fix/...`, `chore/...`, `docs/...`
- PRs target `main`; squash-merge preferred; delete branch after merge
- Version bumps: pev plugin semver in `pev_nexus_agents/pev/.claude-plugin/plugin.json`, marketplace version in `.claude-plugin/marketplace.json`; mirror both when changing pev
- New SOPs must include plugin-fallback path handling; see existing pattern in `pev_nexus_agents/pev/skills/pev-doc-reviewer/SKILL.md` Step 2
- Cross-platform-sensitive shell code gets a comment explaining the Windows quirk it's avoiding

## Running the test harnesses locally

From a consumer project that has both plugins installed:

```bash
# Layer 1: is the plugin infrastructure alive?
MSYS_NO_PATHCONV=1 claude -p "/hs-heartbeat" --dangerously-skip-permissions

# Layer 2: does PEV's 11-test integration pass?
MSYS_NO_PATHCONV=1 claude -p "/pev-spike" --dangerously-skip-permissions
```

Results + canaries land in `/tmp/hook-spike/` and `/tmp/pev-*` — inspect via `Read` or `cat`.
