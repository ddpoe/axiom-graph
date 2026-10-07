<!-- generated from axiom_graph::docs/consumer/plugins/pev/readme @ 9aad3db255f5; do not edit -->

# pev

The PEV Agent Nexus (PEV: Plan, Execute, Validate) is a Claude Code plugin for making code changes in phases you approve. Separate agents plan the change, build it, review it, and update the docs it made stale. They use [axiom-graph](https://github.com/ddpoe/axiom-graph) to read the code and to find those docs.

- [setup.md](setup.md): requirements, install, configuration and troubleshooting. Start here.
- [user-guide.md](user-guide.md): running `/pev-cycle` and `/pev-instance`, the approval gates, and customizing PEV with `.pev/` files.

## What's in this plugin

Skills you run:

| Skill | What it does |
|---|---|
| `/pev-cycle <task>` | Full workflow for larger changes. Architect, Builder, Reviewer, Auditor and Doc Reviewer agents run in turn, everything up to the merge in a separate git worktree, with your approval between phases. |
| `/pev-instance <task>` | Single-agent workflow for small changes, in a git worktree of its own for each task: a mini-pitch for your approval, the change, doc updates, then a review by an independent Reviewer and Doc Reviewer running in parallel, a checkin doc, and the same merge step as `/pev-cycle`. Suggests `/pev-cycle` if the task grows. |
| `/pev-audit-dev-docs` | Clears drift between code and dev docs across the project. |
| `/pev-audit-annotations` | Finds annotation rule violations and docstrings or summaries that no longer match the code, and fixes the ones you approve. |
| `/pev-audit-consumer-docs` | Checks user-facing docs against recent code and dev-doc changes, before a release. |
| `/pev-spike` | Tests that PEV's hooks work. Use it after installing or when debugging. |

The plugin also contains the agents these skills dispatch, hooks that keep each agent within its tools, its worktree and its tool-call budget, templates for the `.pev/` files, and `axiom-annotations-markers`, a reference skill agents load when they write step markers.

## Quick start

1. Install and check the plugin with [setup.md](setup.md).

2. Run a full cycle:

```
/pev-cycle add a history endpoint that filters by date range
```

The Architect plans the change and shows you the pitch. Once you approve, the Builder implements it and the Reviewer checks it. The Auditor then updates the docs in the worktree, and you approve the finished cycle once before it is merged into main. [user-guide.md](user-guide.md) walks through each phase.

3. For a small change:

```
/pev-instance return a 404 instead of a 500 when a history entry is missing
```

## Customizing for your project

PEV reads optional DocJSON files from `.pev/` in your project. Three have templates in the plugin's `templates/` folder: `doc-topology.docjson` (your doc categories and how the Auditor updates each), `test-policy.docjson` (test tiers and coverage) and `review-criteria.docjson` (project-specific review checks). A fourth, `architecture-policy.docjson`, describes the layers of your code; no template ships for it. [user-guide.md](user-guide.md) explains each file.

## Requirements

- axiom-graph 3.0.0 or later, with your project indexed and its MCP server connected to Claude Code under the name `axiom-graph`. The server must offer the `axiom_graph_info` tool, which PEV's agents call to learn your project's id, docs folders and file extension.
- `.pev` listed in `docs_dirs` of your `axiom-graph.toml`, so PEV's templates and SOP files are indexed. [setup.md](setup.md) shows how.
- Python 3.10 or later. axiom-graph needs it, and the efficiency report at the end of a run is made by a plugin script that runs with `python3`.
- A git repository. `/pev-cycle` and `/pev-instance` each build in a git worktree and merge into main.
- bash. On Windows, Git Bash from [Git for Windows](https://gitforwindows.org/).
- [jq](https://jqlang.org/) on the PATH Claude Code runs hooks with. It is not installed by default on Ubuntu or bundled with Git for Windows. Install it with `winget install jqlang.jq` (Windows), `sudo apt install jq` (Ubuntu/Debian) or `brew install jq` (macOS). Without jq, the hooks deny every tool call from a PEV agent.
