# pev

Plan-Execute-Validate agent workflow for Claude Code. Structured code changes through explicit phases — separate agents for planning, implementing, reviewing, and validating. Human approval gates between each. axiom-graph integration for codebase and doc graph awareness.

This file is the landing page for the plugin. Dig deeper in:

- **[SETUP.md](./SETUP.md)** — first-install checklist and version-to-version migration. **Start here after installing.**
- **[USER_GUIDE.md](./USER_GUIDE.md)** — how to invoke `/pev-cycle` and `/pev-instance`, what you see at each approval gate, customizing PEV for your project via `.pev/` SOPs.
- **[DESIGN.md](./DESIGN.md)** — architecture, tool permissions matrix, hook model, invariants. For anyone modifying or extending the plugin.
- **[../../pev_nexus_agents/hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md)** — debugging plugin hooks when something silently fails.

## What's in this plugin

Two user-invocable skills (you run these directly):

| Skill | Purpose |
|---|---|
| `/pev-cycle` | Full multi-agent workflow for non-trivial changes (new features, cross-cutting refactors, anything touching core mechanisms). Architect → Builder → Reviewer → Auditor → Doc Reviewer, in an isolated worktree, explicit human gates per phase. |
| `/pev-instance` | Slim single-agent mode for small, well-scoped tasks (docstring fixes, single-file bug fixes, small refactors). Mini-pitch → human gate → implement → update affected docs → structured self-review → checkin doc; the one agent owns its own doc upkeep (no separate Auditor). Runs in the working tree, no worktree isolation. Escalates to `/pev-cycle` when a task turns out bigger than scoped. |

Plus `/pev-spike` (11-test integration smoke test for the PEV hook infrastructure — you usually won't need this unless you're debugging PEV itself).

## Quick start

1. Install both plugins (pev + the companion hook-spike for debugging) and set up project directories: see **[SETUP.md](./SETUP.md)** (~3 minutes, one-time).

2. Run a full cycle:
```
/pev-cycle add a history endpoint that filters by date range
```
You'll be walked through Architect planning, human approval, Builder implementation, Reviewer validation, and Auditor doc updates. Full phase-by-phase guide in [USER_GUIDE.md](./USER_GUIDE.md).

3. For small work:
```
/pev-instance fix typo in README install section
```

## Customizing for your project

PEV reads three optional DocJSON SOPs from `<your-project-root>/.pev/`:

| File | Configures |
|---|---|
| `doc-topology.json` | Project doc taxonomy — categories, update triggers, per-category Auditor actions, Doc Reviewer checks |
| `test-policy.json` | Test tiers, annotation contract, coverage expectations |
| `review-criteria.json` | Reviewer's project-specific emphasis (logging conventions, anti-patterns) |

Absent files fall back to plugin-shipped templates at `${CLAUDE_PLUGIN_ROOT}/templates/`. Customize by copying a template into your `.pev/` and editing. The templates are self-documenting — each section explains what field shapes the agents' behavior.

Details in [USER_GUIDE.md](./USER_GUIDE.md#customizing-via-pev-sops).

## Requirements

- [axiom-graph](https://github.com/ddpoe/axiom-graph) MCP installed and configured in your project — PEV uses it for codebase reads, doc graph, and cycle manifest persistence
- A git repository (`/pev-cycle` creates a worktree; `/pev-instance` commits in-place)

## Status

Current version: **2.1.0**. See [../../CHANGELOG.md](../../CHANGELOG.md) for release history and breaking changes. The plugin is actively used in ~3 consumer projects.
