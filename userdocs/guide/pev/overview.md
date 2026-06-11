<!-- generated from axiom_graph::docs.consumer.pev.overview @ 8f0afa68433a; do not edit -->

# PEV Agent Nexus

## What the PEV Agent Nexus is

The **PEV Agent Nexus** is the flagship application built *on* axiom-graph. It is not a core primitive of the graph itself - it is a [Claude Code](https://docs.claude.com/en/docs/claude-code) plugin that drives structured code changes through explicit **Plan -> Execute -> Validate** cycles, with a human approval gate between every phase.

Where axiom-graph gives an agent a typed mesh of code and docs to read against, PEV is the proof that an agent can *consume* that mesh to do real engineering work: plan against the codebase, implement with tests, review the diff, and update the docs the change made stale - all coordinated through axiom-graph's MCP tools.

PEV ships as its own plugin, separate from the axiom-graph package, so its full guides travel with the plugin - in the source repo under `pev_nexus_agents/pev/`, not on this site: `README.md` (landing + quick start), `SETUP.md` (first-install checklist and migration), `USER_GUIDE.md` (invoking the cycles, the approval gates, the `.pev/` SOPs), and `DESIGN.md` (architecture, tool-permission matrix, hook model). Open those in the source tree when you need step-by-step how-to.

This page is the orientation that stands on its own: *why PEV exists*, *how it leans on the mesh*, and enough of the shape - the agents, the gates, the two cycle modes - to picture a first cycle without leaving the site. It deliberately does not duplicate the plugin guides.

## The problem it solves

AI-assisted development usually runs as a single undifferentiated pass: one agent plans, codes, documents, and verifies inside one context window. Three failure modes follow:

- **No planning gate** - the agent starts coding before a human approves the approach, so wrong approaches burn compute and context before anyone can object.
- **Invisible deviations** - when the agent diverges from the plan, the change is buried in chat with no structured "I did Y instead of X because Z, and it affects W" artifact.
- **Documentation drift** - after the code lands, docs are skipped or bolted on without a consistency check, and [staleness](../concepts/staleness.md) accumulates silently.

PEV answers each one by splitting development into structurally distinct phases with their own tool permissions, recording every plan and deviation in a persistent manifest, and closing the loop with a validation phase that reads axiom-graph's staleness signal to find exactly the docs the change disturbed.

## The agents and the cycle

`/pev-cycle` orchestrates five specialized agents in sequence, each with a runtime-enforced tool allowlist. The orchestrator owns phase transitions, the human gates, and the lifecycle of the cycle manifest.

| Agent | Phase | What it does |
|---|---|---|
| **Architect** | Plan | Explores the codebase through axiom-graph, optionally asks clarifying questions, writes a Shape Up-style pitch (problem, user stories, solution sketch, constraints, test plan). No code writes. |
| **Builder** | Execute | Reads the pitch, decomposes it into tasks, implements with TDD in an isolated worktree, commits on the worktree branch. |
| **Reviewer** | Validate (code) | Read-only scrutiny of the Builder's diff against the pitch - six passes covering tests, source-doc cross-check, spec compliance, functionality preservation, code quality, and PEV-specific checks. |
| **Auditor** | Validate (docs) | Runs on `main` after merge. Reviews every stale node, updates graph-linked docs, and applies project doc rules from `.pev/doc-topology.json`. Writes an Impact Report. |
| **Doc Reviewer** | Validate (docs) | A narrow safety net - verifies the Auditor's doc updates and scans for drift in categories the Auditor may have missed. |

The load-bearing invariant across the whole matrix: **no single agent can both write code and update live feature docs.** The Builder cannot see doc-write tools; the Architect cannot see code-write tools; only the Auditor can mark nodes clean and clear staleness. Permissions are structural, not prompt-based, so a phase cannot quietly overstep its role.

A human gate sits after planning, after review, before merge, and after doc review. Nothing reaches `main` until you approve at the merge gate.

## Two cycle shapes

PEV exposes two user-invocable entry points so the ceremony matches the size of the work:

```
/pev-cycle add a history endpoint that filters by date range
```

`/pev-cycle` is the full multi-agent workflow for non-trivial changes - new features, cross-cutting refactors, anything touching core mechanisms. It runs in an isolated git worktree, walks all five agents, and gates each phase.

```
/pev-instance fix typo in README install section
```

`/pev-instance` is a slim single-agent mode for small, well-scoped tasks - docstring fixes, single-file bug fixes, config tweaks. It runs in your working tree with no worktree isolation and no separate Reviewer: mini-pitch -> human gate -> implement -> structured self-review -> a searchable checkin doc. It shares the *spine* (user-story framing, human gate, self-review against your SOPs) but not the machinery, and it will proactively escalate to `/pev-cycle` if the task turns out bigger than scoped - for instance if it touches a function the project has declared a core mechanism via [workflow markers](../concepts/annotations.md).

When in doubt, start with `/pev-instance` and let it escalate. The full decision table lives in the plugin USER_GUIDE.

## How it is built on axiom-graph

PEV's dependence on axiom-graph is total and deliberate - it is the clearest demonstration of an agent treating [the mesh](../concepts/the-mesh.md) as its working surface. Three distinct uses:

- **Codebase reads.** During planning and review the agents navigate the code through `axiom_graph_search`, `axiom_graph_graph`, and `axiom_graph_source` rather than grepping and reading whole files. The Reviewer traces callers of a changed function via the graph to check functionality preservation; the Architect orients at module level without loading the repo. This is context reduction in practice - the agent pulls exactly the linked nodes it needs.
- **The doc graph and the staleness engine.** After merge the Auditor runs `axiom_graph_build` + `axiom_graph_check` and reads which nodes went stale. Drift is not a side feature here - it is the engine that tells the validation phase *exactly* which docs the change disturbed, so the Auditor updates those and nothing else. The same mesh that provided context during planning now reports drift during validation; they are two reads against one structure.
- **Cycle-manifest persistence.** Every cycle's pitch, build plan, review findings, and Impact Report live in a single DocJSON manifest (`docs/pev/cycles/<cycle-id>.json`), written incrementally through the doc-editing MCP tools. Because the manifest is itself a doc node, `axiom_graph_search` finds cycle and instance history alongside everything else, and partial work survives an interrupted session.

PEV could in principle run without axiom-graph (SOPs as an abstraction layer, `Read`/`Grep` as code-read fallbacks), but that portability is explicitly out of scope - the value of the integration in practice outweighs it. The MCP server is the integration surface: PEV is an MCP client, and connecting it is the same one-time step as [connecting any agent](../get-started/connect-your-agent.md).

## The cycle manifest and keeping docs honest

The cycle manifest is the central artifact - and it is also why PEV produces docs that stay honest rather than docs that rot. Because all documentation is DocJSON written exclusively through axiom-graph's tools, the plan, the deviations, and the doc updates are all nodes in the same mesh:

- **Decisions** record what was chosen and why - a durable design log for the next cycle.
- **Deviations** capture the Builder-vs-plan delta ("plan said X, I did Y, because Z"), feeding the Reviewer's check on divergence.
- **Friction logs** let each agent append in-the-moment observations when something pinches; their value compounds across cycles as `axiom_graph_search` surfaces recurring tags.

The payoff is the validation loop: feature docs are the ground truth for what *exists*, updated in place by the Auditor after each change, while plans live in the cycle manifest. When the Builder's code drifts from a doc, the staleness engine flags it, the Auditor updates it, and `mark_clean` clears the signal once the doc is verified against the new code. That is the same [docs-honesty loop](../examples/docs-honesty-loop.md) axiom-graph uses to keep its own published documentation trustworthy - PEV runs it on every cycle.

## The /pev-audit-* skill family

Alongside the per-change cycles, PEV ships a family of standalone audit skills for backlog-style sweeps when accumulated drift has grown past what an in-cycle Auditor can address:

| Skill | Sweeps |
|---|---|
| `/pev-audit-dev-docs` | Graph-staleness drift across dev docs (feature docs, ADRs, plans) - sequential feature-shard dispatch. |
| `/pev-audit-annotations` | Annotation-rule violations (duplicate step numbers, sequence gaps, undecorated targets) and prose drift in summaries. |
| `/pev-audit-consumer-docs` | A pre-release sweep of user-facing prose, stress-testing each consumer doc's claims against current dev reality. |

Each is its own orchestration with a human gate before any change lands; they are mentioned here for completeness rather than detailed - reach for them when an entire doc category needs a pass, not when a single change has just merged. The consumer-docs sweep, in particular, is what guards the same published prose you are reading now.

## Where to go next

**Running it, in short.** PEV installs as a Claude Code plugin and points at an axiom-graph project you have already indexed - connecting it is the same one-time MCP step as [connecting any agent](../get-started/connect-your-agent.md). From there you drive it with two commands: `/pev-cycle <task>` for a full multi-agent cycle and `/pev-instance <task>` for a small, single-agent change (see [the two cycle shapes](#two-cycle-shapes)). Per-project behavior is tuned through three optional DocJSON SOPs in `<your-project>/.pev/` - doc taxonomy, test policy, review criteria - with plugin-shipped fallbacks if you omit them.

**The full guides** travel with the plugin, in the source repo under `pev_nexus_agents/pev/`: `SETUP.md` to install and verify, `USER_GUIDE.md` for the per-phase walkthrough and gate-by-gate reference, `DESIGN.md` for the architecture and tool-permission matrix. Open them in the source tree when you need the step-by-step.

New to the substrate PEV is built on? Start with [the mesh](../concepts/the-mesh.md), then [connect your own agent](../get-started/connect-your-agent.md) - PEV is one agent doing exactly that, at scale.
