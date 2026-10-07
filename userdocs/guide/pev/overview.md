<!-- generated from axiom_graph::docs/consumer/pev/overview @ 19b3c6b0e21f; do not edit -->

# PEV Agent Nexus

## What the PEV Agent Nexus is

The PEV Agent Nexus (PEV: Plan, Execute, Validate) is a Claude Code plugin that runs a code change through separate agents. One plans the change, one builds it, one reviews it, and one updates the docs the change made stale. You approve the plan, the review result and the merge before the cycle moves on.

Use it when you want an agent's approach approved before any code is written, the result checked against that plan, and the docs brought up to date in the same cycle.

PEV runs on axiom-graph. Its agents read the code through the MCP tools, and the Auditor uses the [staleness check](../concepts/staleness.md) to find the docs a change affected. The plugin is installed separately and carries its own docs: the [readme](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/readme.md), [setup guide](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/setup.md) and [user guide](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/user-guide.md).

## The agents and the cycle

`/pev-cycle` runs five agents in turn and records their work in the cycle docs, a small set of DocJSON docs kept together in one folder for that cycle.

| Agent | Phase | What it does |
|---|---|---|
| Architect | Plan | Reads the codebase through axiom-graph, asks you questions if it needs to, and writes a pitch: problem, user stories, solution sketch, constraints and test plan. |
| Builder | Execute | Implements the pitch test-first in a separate git worktree. |
| Reviewer | Validate | Runs the test suite and checks the diff against the pitch: every change authorized, every user story implemented, existing callers unaffected. Changes no code. |
| Auditor | Validate | Runs in the worktree, on the tree that will land, before the merge. Updates the docs the change made stale, marks them clean, and writes an impact report. |
| Doc Reviewer | Validate | Also in the worktree. Checks the Auditor's updates and looks for drift in docs that are not linked to code. |

Each agent gets only the tools its phase needs, and the plugin's hooks hold it to them. Only the Builder edits code, and only the Auditor edits project docs or clears drift. The other agents write only to the cycle docs, and each writes only the docs that are its own.

You confirm the cycle name, approve the plan and the review result, and approve the finished cycle, audit included, once. Nothing is merged into main before that last approval. A shared merge step then lands the branch and copies the doc verifications made in the worktree onto main, so the docs are not audited twice. The plugin's [user guide](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/user-guide.md) lists every gate.

## Two cycle shapes

| Command | Use it for | How it runs |
|---|---|---|
| `/pev-cycle <task>` | Changes that span several systems, change a public API, or need a design decision | Five agents, a separate worktree, your approval between phases |
| `/pev-instance <task>` | A small change to a few files that still affects docs | One agent in a worktree of its own for each task: a mini-pitch for your approval, the change, doc updates, an independent review and a checkin doc, then the same merge step as `/pev-cycle` |

`/pev-instance` suggests switching to `/pev-cycle` when a task turns out larger than it looked, for example when it would change a function that has [step markers](../concepts/annotations.md). If you are not sure which to use, start with `/pev-instance`.

## How PEV uses axiom-graph

PEV needs axiom-graph 3.0.0 or later, an index of your project, and the MCP server [connected to Claude Code](../get-started/connect-your-agent.md). It uses axiom-graph in three ways:

- Reading code. The agents navigate with `axiom_graph_search`, `axiom_graph_graph` and `axiom_graph_source` instead of reading whole files. The Reviewer uses the graph to find the callers of each changed function.
- Finding stale docs. Once the branch has main merged into it, the worktree's index is rebuilt, and the Auditor works through the doc sections `axiom_graph_drift_query` lists as stale. Editing a section does not clear its stale flag by itself, so for each one the Auditor names the code changes its edit accounts for, or marks the section clean once it has checked it.
- Keeping a record. A cycle's docs are written to the folder `docs/pev/cycles/<cycle-id>/` with the axiom-graph doc tools. They hold the pitch, the build plan, the review, the decisions, the Builder's deviations from the plan, and the impact report. `/pev-instance` writes a shorter checkin doc to `docs/pev/instances/<instance-id>/`. Both are indexed, so `axiom_graph_search` finds past work.

## Audit skills

Three more skills clean up drift across a whole category of docs, rather than after a single change. Each one shows you what it proposes to change before it changes anything.

| Skill | What it checks |
|---|---|
| `/pev-audit-dev-docs` | Drift between code and dev docs: feature docs, ADRs, plans |
| `/pev-audit-annotations` | Annotation rule violations, such as duplicate step numbers or gaps in a sequence, and docstrings or summaries that no longer match the code |
| `/pev-audit-consumer-docs` | User-facing docs, checked against recent code and dev-doc changes; run it before a release |

## Install and next steps

```bash
claude plugin marketplace add ddpoe/axiom-graph
claude plugin install pev@axiom-graph
```

Then follow the plugin's [setup guide](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/setup.md) to check the requirements (axiom-graph 3.0.0 or later, bash, jq), copy the optional `.pev/` config files and test the install. The [user guide](https://github.com/ddpoe/axiom-graph/blob/HEAD/pev_nexus_agents/pev/docs/user-guide.md) covers running cycles, the approval gates and customization.
