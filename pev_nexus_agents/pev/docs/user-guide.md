<!-- generated from axiom_graph::docs.consumer.plugins.pev.user-guide @ c861ddce32e2; do not edit -->

# PEV User Guide

How to actually use the `pev` plugin. Covers the two cycle shapes, the human approval gates, customization via `.pev/` SOPs, resume behavior, and common decision points.

**Before you start:** if this is a fresh install or you're upgrading from an earlier version, work through [setup.md](setup.md) first — it covers plugin install, directory creation, SOP template setup, and per-version migration steps. This guide assumes setup is complete.

## Two cycle shapes

### `/pev-cycle` — full workflow

For a **cross-cutting change that fans out across several core systems at once** — e.g. a config knob every layer must honor, or a field threaded from ingestion through storage, the API, and the UI. Five phases, five approval gates, isolated worktree, persistent cycle manifest.

*Example — a real cycle:*

```
/pev-cycle add configurable doc-scan dirs + a db-path key, plumbed through the builder, CLI, MCP, renderers, diff, and viz
```

Phases: Intake → Plan (Architect) → Build (Builder) → Review (Reviewer) → Merge → Audit (Auditor) → Doc Review (Doc Reviewer) → Complete. Each agent reads and writes the cycle manifest; human gates sit between the plan, review, merge, and doc-review phases.

### `/pev-instance` — slim mode

For a **localized change with a small blast radius** — one subsystem, a handful of files — that is still a real change with doc impact (not a one-line typo or docstring; any agent can do those). One agent, no worktree, no sub-dispatches — it plays Builder, Reviewer, and Auditor itself, so it runs its own doc-review/impact pass and updates the documentation its change affects rather than handing that off.

*Example — a real instance:*

```
/pev-instance auto-register a worktree as a viz project on checkout so it shows up in the project picker
```

Flow: pre-flight checks (dirty-repo + clean-graph baseline via `axiom_graph_check`, both overridable) → read `.pev/` SOPs → scope check (escalates to `/pev-cycle` on 4+ files, public API change, new architecture, or core-mechanism touch) → mini-pitch + human gate → implement in working tree, single commit → update the docs the change affects + an impact note (its own Auditor pass — no separate Auditor) → structured self-review → write a checkin doc.

### When to use which

Use `/pev-instance` for 1–3 files, no public-API/architecture change, when you want the discipline surface (user story, self-review, doc updates, searchable record). Use `/pev-cycle` for changes that fan out across multiple systems, public API changes, architectural decisions, new features, or anything touching core mechanisms. When in doubt, start with `/pev-instance` and let it escalate.

## Human approval gates

Every gate follows the same pattern — the orchestrator presents the relevant artifact, asks approve/revise/abort, and waits.

| Gate | You see | You can |
|---|---|---|
| Post-plan | Full Architect pitch (scope, user stories, solution sketch, constraints, test plan) | Approve → Builder runs. Revise → redispatch Architect with feedback. Abort. |
| Post-review | Reviewer verdict + test coverage table | Approve → merge. Request Builder fixes → loopback. Abort. |
| Pre-merge | Change summary (files, tests, deviations, axiom-graph check) | Approve merge → Auditor runs. Provide feedback → Builder loopback. |
| Post-doc-review | Doc Reviewer verdict | Approve → complete. Request Auditor fixes → loopback (max 2). Abort. |
| `/pev-instance` | Mini-pitch | Approve → implement. Revise → re-pitch. Escalate → bail to `/pev-cycle`. |

You remain in control throughout. Nothing writes to main until you approve at the merge gate.

## Customizing via .pev/ SOPs

Three optional DocJSON files under `<project_root>/.pev/`:

- **`.pev/doc-topology.json`** — project doc taxonomy. Lists doc categories (PRD, interface spec, ADR, design spec, README, etc.) each with a path glob, the cycle changes that trigger it, the Auditor action, and the Doc Reviewer check. Without it, the Auditor sticks to axiom-graph-linked docs only.
- **`.pev/test-policy.json`** — test classification and budget. Defines the tier system (Tier 1 plain pytest / Tier 2 `@workflow(purpose=...)` / Tier 3 `@workflow + Step()`), decision rule, coverage expectations, and budget guidance.
- **`.pev/review-criteria.json`** (optional) — project-specific code-review emphasis (logging correlation IDs, typed exceptions, anti-patterns) with per-check severity guidance.

The files are DocJSON (JSON with a `sections` array; each section's `content` is markdown). Only the structure is JSON; the content is markdown you can read directly.

## Friction logs

Every PEV agent and the orchestrator maintain a `{agent}.friction` section in the cycle manifest where they append in-the-moment observations when something pinches. **Expected to be empty most of the time.** Agents capture only when something genuinely pinches; they do not invent friction to look thorough.

The cycle manifest has three overlapping-but-distinct accumulating sections:

| Section | Shape | Purpose |
|---|---|---|
| `decisions` | "What was chosen, alternatives considered, why" | Durable record of design choices |
| `builder.deviations` | "Plan said X, I did Y, because Z" | Structured Builder-vs-Architect delta |
| `{agent}.friction` | "This felt hard or off, here's what I was looking at" | Phenomenological signal about skill/tool/process quality |
