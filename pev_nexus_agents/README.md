# axiom-graph plugins (PEV + hook-spike)

Claude Code plugin marketplace bundled with the [axiom-graph](../) repo. Two plugins:

| Plugin | Purpose |
|---|---|
| [`pev`](./pev/) | Plan-Execute-Validate agent workflow for structured code changes — Architect, Builder, Reviewer, Auditor, and Doc Reviewer subagents with axiom-graph integration. Includes `/pev-cycle` (full multi-agent workflow), `/pev-instance` (slim single-agent mode), and `/pev-spike` (infrastructure smoke test). |
| [`hook-spike`](./hook-spike/) | Minimal plugin-hook test harness. Install when debugging plugin hooks that silently fail. |

## Install

```
/plugin marketplace add ddpoe/axiom-graph
/plugin install pev@axiom-graph
/plugin install hook-spike@axiom-graph
```

For the full setup, including requirements, SOP templates and troubleshooting, see [`pev_nexus_agents/pev/docs/setup.md`](./pev/docs/setup.md).

## Where to go next

- **Setting up PEV in a consumer project** → [`pev_nexus_agents/pev/docs/setup.md`](./pev/docs/setup.md)
- **Using PEV in your project** → [`pev_nexus_agents/pev/docs/user-guide.md`](./pev/docs/user-guide.md)
- **Modifying PEV** → [`pev_nexus_agents/pev/DESIGN.md`](./pev/DESIGN.md)
- **Debugging plugin hooks** → [`pev_nexus_agents/hook-spike/TROUBLESHOOTING.md`](./hook-spike/TROUBLESHOOTING.md)
- **Release history** → [`pev/CHANGELOG.md`](./pev/CHANGELOG.md)
- **Working ON this marketplace** (extending the plugins, authoring PRs) → [`AGENTS.md`](./AGENTS.md)
