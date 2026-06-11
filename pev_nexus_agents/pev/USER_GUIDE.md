# PEV User Guide

How to actually use the `pev` plugin. Covers the two cycle shapes, the human approval gates, customization via `.pev/` SOPs, resume behavior, and common decision points.

**Before you start:** if this is a fresh install or you're upgrading from an earlier version, work through [SETUP.md](./SETUP.md) first — it covers plugin install, directory creation, SOP template setup, and per-version migration steps. This guide assumes setup is complete.

For architecture details (tool permissions, hook model, agent responsibilities), see [DESIGN.md](./DESIGN.md). For debugging plugin hooks when something silently misbehaves, see [../hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md).

## Two cycle shapes

### `/pev-cycle` — full workflow

For non-trivial changes. Five phases, five approval gates, isolated worktree, persistent cycle manifest.

```
/pev-cycle add a history endpoint that filters by date range
```

Phases:

1. **Intake** — orchestrator verifies a clean working tree, creates a worktree at `.claude/worktrees/<cycle-id>`, checks out a fresh branch, copies the axiom-graph DB into the worktree (via `axiom_graph_checkout`), creates the cycle manifest at `docs/pev/cycles/<cycle-id>.json`. Cycle IDs are `pev-YYYY-MM-DD-<slug>`.
2. **Plan (Architect)** — Architect explores the codebase via axiom-graph, optionally asks you clarifying questions, writes a Shape Up-style pitch to the manifest: problem, user stories ("As a [persona], I want…"), solution sketch, constraints, test plan. **Human gate**: review the pitch (scope, stories, sketch, constraints, test plan all shown) and approve, revise, or abort.
3. **Build (Builder)** — Builder reads the pitch, decomposes into tasks, implements with TDD in the worktree, commits when done. Returns a manifest (files changed, tests added, deviations). On `CONTINUING`, the Builder is re-dispatched with its previous progress preserved.
4. **Review (Reviewer)** — read-only scrutiny of the Builder's code against the pitch and source docs. Six passes: test run, source doc cross-check, spec compliance (per user story), functionality preservation (callers traced via axiom-graph), code quality, PEV-specific checks (logging, test annotations, workflow markers). **Human gate**: presented review verdict, test coverage table. Approve merge, redispatch Builder to fix, or abort.
5. **Merge** — orchestrator merges the worktree branch into main, rebuilds axiom-graph, presents impact summary. **Human gate**: approve to proceed to Auditor.
6. **Audit (Auditor)** — runs on main post-merge. Reviews every stale node, updates graph-linked docs (feature docs, design specs, interface specs) AND reads `.pev/doc-topology.json` to proactively update project-specific doc categories (PRDs, ADRs, etc.). Writes Impact Report.
7. **Doc Review (Doc Reviewer)** — verifies the Auditor's doc updates + scans for drift in doc categories the Auditor may have missed. Narrow safety-net role. On FAIL, the Auditor is redispatched.
8. **Complete** — final commit assembled, cycle manifest marked `completed`, worktree cleanup.

### `/pev-instance` — slim mode

For small, well-scoped tasks. One agent, no worktree, no sub-dispatches, no separate Reviewer — it plays Builder, Reviewer, and Auditor itself, so it updates the docs its change affects rather than deferring them.

```
/pev-instance fix typo in README install section
```

Flow:

1. Pre-flight checks (both conversational overrides): a dirty-repo check ("repo has uncommitted changes — proceed / stash first / escalate to /pev-cycle?") and a clean-graph baseline check via `axiom_graph_check` ("the doc graph already shows stale nodes — clear them first / proceed and note them / cancel?"), so the instance starts from a known-clean impact baseline.
2. Read `.pev/` SOPs (fallback to plugin templates).
3. Scope check: runs `axiom_graph_workflow_list(steps=true)` to see what the project considers core mechanisms. If the task looks likely to touch any of those, or crosses other escalation signals (4+ files, public API change, new architectural decision, 3+ new tests), bails out with `status: escalated` and recommends `/pev-cycle`.
4. Writes a mini-pitch (problem, user story, acceptance, plan). **Human gate**: approve, revise, or escalate.
5. Implements in your working tree. Single commit when done.
6. Updates the documentation the change affects — `axiom_graph_check` plus `.pev/doc-topology.json` to find drift, patches the sections, marks the touched nodes clean, fixes any moved doc-to-code links. This is the Auditor's role, folded into the one agent (there's no separate Auditor phase). Doc-drift volume is never an escalation trigger — the instance always finishes the doc work.
7. Structured self-review: acceptance criteria verified, test-policy tier compliance, review-criteria violations noted, doc-update verification (every affected doc updated and its node clean), workflow-marker check (including forward-looking: did this change introduce a new function that should become a workflow?), collateral grep.
8. Writes a checkin doc to `docs/pev/instances/pev-instance-YYYY-MM-DD-<slug>.json`. Doc is searchable via `axiom_graph_search` alongside full-cycle manifests.

Status codes match full PEV (`DONE` / `CONTINUING` / `BLOCKED` / `NEEDS_INPUT` / `ESCALATED`) so moving up to `/pev-cycle` mid-task is natural.

### When to use which

Use `/pev-instance` when:
- 1–2 files touched
- No public API, architecture, or user-facing behavior change
- You'd normally "just do it" but want the discipline surface (user story, self-review, searchable record)
- Docstring fixes, single-file bug fixes, small refactors, config tweaks

Use `/pev-cycle` when:
- 3+ files, public API changes, architectural decisions, new features
- You want the Reviewer safety net
- Anything touching `axiom_graph_workflow_list(steps=true)` functions ("core mechanisms")
- You're uncertain about scope — `/pev-instance` will proactively escalate if it grows, but starting full costs one extra dispatch

When in doubt, start with `/pev-instance` and let it escalate.

## Human approval gates

Every gate follows the same pattern — the orchestrator presents the relevant artifact, asks approve/revise/abort, and waits. Specifically:

| Gate | You see | You can |
|---|---|---|
| Post-plan | Full Architect pitch (scope, user stories, solution sketch, constraints, test plan) | Approve → Builder runs. Revise → redispatch Architect with feedback. Abort → cycle marked incomplete. |
| Post-review | Reviewer verdict + test coverage table | Approve → merge. Request Builder fixes → loopback. Abort. |
| Pre-merge | Change summary (files, tests, deviations, axiom-graph check) | Approve merge → Auditor runs. Provide feedback → Builder loopback. |
| Post-doc-review | Doc Reviewer verdict | Approve → complete. Request Auditor fixes → loopback (max 2). Abort. |
| `/pev-instance` | Mini-pitch | Approve → implement. Revise → re-pitch. Escalate → bail to `/pev-cycle`. |

You remain in control throughout. Nothing writes to main until you approve at the merge gate.

## Resume

If a cycle is interrupted (session crash, `/pev-cycle` cancelled, context pressure auto-continues an agent mid-phase), resume happens automatically on your next `/pev-cycle` invocation:

- Orchestrator queries for cycle manifests tagged `pev-active`
- Presents them: "Resume, release (mark incomplete), or start new"
- On resume, picks up at the phase the manifest's `status` section records. Partial work in sections survives.

For interrupted `/pev-instance` runs, you just reinvoke `/pev-instance <same task description>` — the skill will re-write the checkin or pick up where it left off.

## Customizing via `.pev/` SOPs

Three optional DocJSON files under `<project_root>/.pev/`:

### `.pev/doc-topology.json`

Project doc taxonomy. Lists doc categories (PRD, interface spec, ADR, design spec, README, etc.), each with:

- **Path** — glob for matching files
- **Triggered by** — what cycle changes activate this category
- **Auditor action** — what the Auditor does proactively when triggered
- **Doc Reviewer check** — what the Doc Reviewer verifies

The Auditor reads this and iterates triggered categories, performing the action. The Doc Reviewer reads it and verifies. Without this file, the Auditor sticks to axiom-graph-linked docs only — you get less doc coverage.

Copy from `${CLAUDE_PLUGIN_ROOT}/templates/doc-topology.json` and customize for your project's doc structure.

### `.pev/test-policy.json`

Test classification and budget. Defines tier system (default: Tier 1 plain pytest / Tier 2 `@workflow(purpose=...)` / Tier 3 `@workflow + Step()`), decision rule, coverage expectations, over/under-testing signs, budget guidance. Architect uses this to propose tests; Builder uses it to annotate correctly; Reviewer cross-checks Builder output.

Most projects don't need to change the default — edit if your project has distinct test surfaces (GUI, provenance tracking, etc.) or uses a different annotation convention.

### `.pev/review-criteria.json` (optional)

Project-specific code-review emphasis. Define checks the Reviewer should apply on top of generic quality review (logging correlation IDs, typed exceptions, project-specific anti-patterns). Severity guidance per check. Skip the file entirely if the generic review is enough.

### Editing DocJSON

The files are DocJSON (JSON with a `sections` array, each section's `content` is markdown). If editing JSON feels annoying, flatten to markdown for reading with:

```bash
jq -r '.sections[] | "## " + .heading + "\n\n" + .content + "\n"' .pev/test-policy.json
```

Only the structure is JSON; the content is markdown you can read directly in any text editor.

### axiom-graph indexing (optional)

If you want `axiom_graph_search` / `axiom_graph_history` over your SOPs, add `.pev` to `doc_dirs` in your `axiom-graph.toml`:

```toml
[axiom_graph.scan]
doc_dirs = ["docs", ".pev"]
```

Then run `axiom-graph build .`. Not required for skills to function — they read files directly via the Read tool.

## Typical walk-through

A concrete `/pev-cycle` session, abbreviated:

```
> /pev-cycle add filter-by-date to the history endpoint

[Architect phase — ~2-5 minutes]
The Architect reads your codebase via axiom-graph, identifies the affected
modules, and may ask: "Use a WHERE clause on an indexed column, or a
Python-side filter? (previous ADR prefers DB-level)."

You answer. It writes a pitch: 2 user stories, solution sketch at
module level, scope boundary, constraints, test plan (4 Tier 2 tests
proposed).

[Human gate — approve the plan]
Plan presented. You say "approve."

[Builder phase — ~5-15 minutes]
Builder writes its build plan to the manifest, implements the 4 tests
first (red), implements the endpoint (green), commits.

[Review phase — ~3-10 minutes]
Reviewer runs tests (all pass), cross-checks pitch vs referenced ADR,
verifies each user story is implemented, traces callers of changed
functions. Verdict: PASS_WITH_CONCERNS (one minor Pass 4 finding about
log message format).

[Human gate — merge?]
Change summary shown. You approve the merge.

[Auditor phase — ~3-8 minutes]
Auditor reads .pev/doc-topology.json, sees "PRD" category triggered
(you changed user-facing behavior). Updates the capabilities table in
docs/prd/history.md to mark "filter by date range" as Done. Also
checks axiom-graph staleness; confirms no unexpected cascades.

[Doc Reviewer phase — ~1-3 minutes]
Verifies the PRD update matches the Builder manifest. PASS.

[Complete]
Final commit assembled, cycle manifest marked completed, worktree
removed. Total: ~20 minutes of wall time, three human approvals.
```

`/pev-instance` sessions usually wrap in 5–10 minutes.

## Friction logs

Every PEV agent and the orchestrator maintain a `{agent}.friction` section in the cycle manifest where they append in-the-moment observations when something pinches — instructions that didn't fit the situation, tool output that was awkward, upstream inputs that forced guessing, role constraints that felt mispriced, effort disproportionate to value.

Where to find them:

- `{cycle_doc_id}::architect.friction`
- `{cycle_doc_id}::builder.friction`
- `{cycle_doc_id}::reviewer.friction`
- `{cycle_doc_id}::auditor.friction`
- `{cycle_doc_id}::doc-review.friction`
- `{cycle_doc_id}::orchestrator.friction`

For `/pev-instance` runs, friction lives in the checkin doc's `friction` section.

**Expected to be empty most of the time.** Agents capture only when something genuinely pinches; they do not invent friction to look thorough. A cycle with empty friction sections is a clean cycle, not a silent one.

**How to use the logs.** The compounding value comes from patterns across cycles, not any single entry:

```bash
# Surface recurring friction across all cycles
axiom_graph_search "friction"

# Filter by tag for a specific friction type
axiom_graph_search "axiom-graph-staleness"
axiom_graph_search "instruction-ambiguity"
axiom_graph_search "role-pinch"
```

Plan a periodic `/pev-instance` (or `/pev-cycle`) whose input is "read the last N cycles' friction logs, cluster the entries, propose skill or tool changes." Without a committed read cadence the logs decay to write-only telemetry — the patterns are only valuable if somebody reads them.

**Friction vs decisions vs deviations.** The cycle manifest has three overlapping-but-distinct accumulating sections:

| Section | Shape | Purpose |
|---|---|---|
| `decisions` | "What was chosen, alternatives considered, why" | Durable record of design choices for future-you or the next cycle to reference |
| `builder.deviations` | "Plan said X, I did Y, because Z" | Structured Builder-vs-Architect delta, used by the Reviewer's deviation tribunal |
| `{agent}.friction` | "This felt hard or off, here's what I was looking at" | Phenomenological signal about skill/tool/process quality, consumed by periodic meta-review |

If you're not sure where something goes: decisions for durable choices, deviations for plan divergence, friction for "this was harder than it should have been."

## Troubleshooting

If agents seem to behave inconsistently or hooks appear silent:

1. Run `claude plugin list` — confirm `pev@axiom-graph` is enabled at the right scope
2. Run `/hs-heartbeat` — smoke tests plugin hook infrastructure at the lowest layer
3. See [../hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md) §7 (failure catalog) for symptom-first debugging

For changes to PEV itself, see [DESIGN.md](./DESIGN.md).
