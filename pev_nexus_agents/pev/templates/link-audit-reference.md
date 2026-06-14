# PEV Link Audit Reference

The shared, change-scoped procedure for keeping the `documents` graph's edges deliberate. All three doc-review roles — the cycle **Auditor**, the cycle **Doc Reviewer**, and `/pev-instance` — run this same audit and read this reference; each skill adds only its role's gate-and-apply wiring (see [Disposition by role](#disposition-by-role)). The one project-specific knob, `Scope`, lives in the project's `.pev/doc-topology.json` (`link-audit` section); everything else is here.

## What it is

A link-audit pass that keeps the documents-graph's edges deliberate, working in both directions. It finds living prose that describes a changed node but declares no `links` edge to it — graph signals (LINKED_STALE, `axiom_graph_drift_query`) can't see those, since they only reach edges that already exist — and it reviews the *existing* edges in the change's neighbourhood for wrong granularity or noise. It runs in every doc review, scoped to the change.

## Tier 1 (change-scoped) — this pass

**This is Tier 1 (change-scoped):** audit only the edges in the current change's impact radius — the changed nodes, their `@workflow`/`@task` envelopes, and the doc sections that link them. Tier 2 — whole-graph hygiene (tree-wide unlinked-node coverage and orphan/broken-edge cleanup) — is NOT this pass; it lives in `/pev-audit-dev-docs`, and the per-cycle Auditor does not run it. Never widen Tier 1 into a whole-graph scan.

## The three verbs

Every finding is an **add**, a **repoint**, or a **drop**:

- **add** — a living section describes a changed node in prose but declares no edge to it. Found by search (derive term families from the change — see [Term families](#term-families) — `axiom_graph_search` each, read the living-doc hits).
- **repoint** — an existing edge sits at the wrong granularity for its section's *kind* (per the [granularity rule](#the-granularity-rule)): a narrative/intent section linked to a bare function should point at the `@workflow`/`@task` envelope; a behavior/contract section linked to an envelope should point at the function. Found by walking the inbound `documents` edges of the changed nodes.
- **drop** — an existing edge is noise: a narrative section fanned across many implementation functions, or an edge a sibling contract section already carries.

## Proposing & the gate

**All three are proposed, never auto-applied — and rejection is a normal outcome.** Each proposal records the section, the target node, the verb, the section's *existing* links (so redundancy and granularity are visible), and a one-line rationale (a `repoint` also names the edge it `replaces`). The lone exception is a link a change *mechanically moved* (its target renamed/relocated): that's repointed directly as reconciliation, not gated. Unreviewed bulk edits dilute LINKED_STALE into noise — every add/repoint/drop is a deliberate signal. Content fixes (drifted prose, gaps) need no gate.

## Disposition by role

Detection is identical for every role; only what you *do* with a finding differs — that part lives in your skill, summarized here:

| Role | What to do with each finding |
|---|---|
| `/pev-instance` (single agent) | Present the verb-tagged list conversationally; apply approved items inline — `add` → `add_link`, `repoint` → `delete_link`+`add_link`, `drop` → `delete_link`. |
| Cycle **Auditor** | Write each as a verb-tagged `proposed_links` entry (section, target, `existing_links`, rationale; `replaces` for a repoint) for the orchestrator's Phase-8 gate. Patch content fixes (drifted prose, gaps) directly. |
| Cycle **Doc Reviewer** | Flag-only — record under `findings.semantic_drift` + `proposed_links`. You hold no link-mutation tools; the orchestrator applies. |
| Cycle **Orchestrator** (Phase-8 proposed-links gate) | Human-gate the combined list; apply approved per verb (`add_link` / `delete_link`+`add_link` / `delete_link`); record per-proposal decisions in the manifest. |

## The granularity rule

What a `repoint` audits against — and the rule to apply when adding a link.

**Link at the granularity that matches what the section promises.** A section's `links` re-evaluate it (LINKED_STALE) whenever a linked node drifts, so choose the target by the section's *kind*:

- **Behavior / contract sections** — design specs explaining *how* something works, interface specs with exact request/response shapes → link the **function / endpoint node**. You *want* these flagged on body-level behavior changes (e.g. a resolution ladder gaining a fail-loud branch).
- **Intent / narrative sections** — PRD user-stories, high-level capability statements → link the **`@workflow` / `@task` envelope** (`fn@workflow`) of the process they describe, not its individual functions. The envelope hashes on the declared *shape* (its `purpose` + `Step` markers), so the section re-evaluates only when the process's contract changes — not on every line edit.
- Don't fan a narrative section across many implementation functions — that dilutes LINKED_STALE into noise. When no envelope exists, prefer one stable representative node, or leave it unlinked and let a sibling contract section carry the signal.

## Term families

The vocabularies the `add` search looks for. The **standing system vocabulary** is constant across projects — PEV runs on axiom-graph: staleness statuses (`VERIFIED`, `CONTENT_UPDATED`, `DESC_UPDATED`, `RENAMED`, `NOT_FOUND`, `LINKED_STALE`, `BROKEN_LINK`), history change_types (`LINK_ADDED`, `LINK_REMOVED`, `DELETED`, `AGENT_VERIFIED`, …), edge types (`documents`, `validates`, `composes`, `annotates`, `delegates_to`, …), MCP tool names (`axiom_graph_*`), CLI subcommands. Plus, **per change**, derive 2–4 families from the cycle itself: the mechanism name the change coins (from the Architect's pitch) and the changed function / tool / command names.

## Scope

Which doc trees are audited is project-specific — read it from the project's `.pev/doc-topology.json` `link-audit` section (`Scope`: living trees that get audited vs frozen/historical trees that are skipped). A pre-1.3 topology may name that section `semantic-sweep` — treat it as the same section. If the project has no `.pev/doc-topology.json`, audit the living current-state doc trees and skip frozen records (cycle/instance/audit manifests, request docs, plans, devlogs, release notes).
