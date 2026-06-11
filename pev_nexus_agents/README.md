# axiom-graph plugins (PEV + hook-spike)

Claude Code plugin marketplace bundled with the [axiom-graph](../) repo. Absorbed from the standalone `ddpoe/pev-agent-nexus` repo per ADR-005 Phase 5 — same plugins, new home. Two plugins:

| Plugin | Purpose |
|---|---|
| [`pev`](./pev_nexus_agents/pev/) | Plan-Execute-Validate agent workflow for structured code changes — Architect, Builder, Reviewer, Auditor, and Doc Reviewer subagents with axiom-graph integration. Includes `/pev-cycle` (full multi-agent workflow), `/pev-instance` (slim single-agent mode), and `/pev-spike` (infrastructure smoke test). |
| [`hook-spike`](./pev_nexus_agents/hook-spike/) | Minimal plugin-hook test harness. Install when debugging plugin hooks that silently fail. |

## Install

```
/plugin marketplace add ddpoe/axiom-graph
/plugin install pev@axiom-graph
/plugin install hook-spike@axiom-graph
```

For the full setup including directory creation, SOP templates, and per-version migration steps, see [`pev_nexus_agents/pev/SETUP.md`](./pev_nexus_agents/pev/SETUP.md).

## Where to go next

- **Setting up PEV in a consumer project** → [`pev_nexus_agents/pev/SETUP.md`](./pev_nexus_agents/pev/SETUP.md)
- **Using PEV in your project** → [`pev_nexus_agents/pev/USER_GUIDE.md`](./pev_nexus_agents/pev/USER_GUIDE.md)
- **Modifying PEV** → [`pev_nexus_agents/pev/DESIGN.md`](./pev_nexus_agents/pev/DESIGN.md)
- **Debugging plugin hooks** → [`pev_nexus_agents/hook-spike/TROUBLESHOOTING.md`](./pev_nexus_agents/hook-spike/TROUBLESHOOTING.md)
- **Release history** → [`CHANGELOG.md`](./CHANGELOG.md)
- **Working ON this marketplace** (extending the plugins, authoring PRs) → [`AGENTS.md`](./AGENTS.md)
