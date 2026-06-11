# Changelog

All notable changes to the PEV / hook-spike Claude Code plugins. Versions loosely follow [Semantic Versioning](https://semver.org/) — major bumps mark breaking changes in doc layout or required consumer migration; minor bumps add features; patch bumps are fixes or docs.

## Compatibility

The PEV plugin drives the `axiom-graph` MCP server, so each plugin release has a minimum server version. Pin `axiom-graph` at or above the floor for your plugin version:

| PEV plugin     | Requires axiom-graph |
|----------------|----------------------|
| 1.2.0 and up   | ≥ 2.1.0              |
| 1.0.0 – 1.1.1  | 2.0.x                |

**Why 1.2.0 needs ≥ 2.1.0:** the agents now call `axiom_graph_patch_section`, an MCP tool first shipped by the server in 2.1.0. Running PEV 1.2.0 against the public 2.0.x server will fail the moment an agent attempts a `patch_section` edit.

## [Unreleased]

Nothing pending.

## [1.2.0] — 2026-06-10

> **Requires axiom-graph ≥ 2.1.0** (see [Compatibility](#compatibility)) — this release adds `axiom_graph_patch_section` to the agent toolsets, and that MCP tool first ships in server 2.1.0.

### Added

- **Semantic sweep in every doc review.** Graph signals (LINKED_STALE, `drift_query`) only reach docs that declare a `links` edge to changed nodes; docs that merely *mention* the changed behavior in prose were invisible and rotted silently. A new search-driven pass closes the gap: derive term families from the cycle's change (mechanism names, status vocabulary, edge/event types, changed symbol/tool names), `axiom_graph_search` each, read the living-doc hits, and judge them against the implemented behavior. Wired in three places: the Auditor (proactive — patches drifted prose and gaps directly), the Doc Reviewer (new Step 6 safety net — flag-only, `findings.semantic_drift`), and `/pev-instance` Step 6 (the single agent runs it itself, with a self-review checklist line). The shipped `templates/doc-topology.json` gains a `semantic-sweep` section (term families + living-vs-frozen scope, both project-customizable) and a seventh reviewer pass referencing it — skills carry the procedure, the topology carries the project knobs.
- **Human-gated link proposals from the sweep.** Sweep-discovered missing edges are *proposed, never auto-added*: each proposal records the section, target node, the section's **existing** links (so redundancy is visible), and a rationale. `/pev-instance` gates conversationally (Step 6 item 5); full cycles collect `proposed_links` from the Auditor's Impact Report and the Doc Reviewer's review and present them at a new Phase 8 **proposed-links gate**, applying only what the human approves and recording per-link decisions in the manifest. Rationale: every edge is a deliberate staleness signal — unreviewed bulk-linking bloats the graph and dilutes LINKED_STALE into noise. (Links *moved* by a change are still repointed directly; the gate covers new relationship edges only.)
- **`axiom_graph_patch_section` in every agent's toolset.** The new partial-section edit tool (append / prepend / unique-match replace) is added to all 10 PEV agents' tool allowlists, the `pev-tool-gate` budget-overflow allowlists, and the `pev-doc-scope` hook + `hooks.json` matcher — so PEV subagents can patch a single DocJSON section without re-sending the whole body, while staying cycle-manifest-scoped.
- **Architecture-policy SOP wiring.** Architect, Builder, and Reviewer now read a project-local `.pev/architecture-policy.json` (four-layer rule, single-source-of-truth, test-entry contract, import rules) for layer-discipline guidance. The Architect names the specific destination layer for each new operation; the Builder lands behavioural logic in the api layer and enters tests where production callers do; the Reviewer gains a new Pass 5e for judgment-call layer violations beyond what static checks catch at pre-push. The policy is **not** shipped as a plugin template — architecture varies too much across projects to ship a generic default; the skills proceed without architectural guidance if the file is absent.
- **Auditor reference protocol.** A new `templates/auditor-reference-protocol.md` codifies a reference policy for current-state docs (PRD, design spec, user guide, interface specs): they describe what the system does in its own terms and MUST NOT back-reference origin docs (ADRs, plans, PEV requests, cycle manifests); forward links from origin → current-state remain encouraged. The Auditor strips inline origin citations during PRD/design updates.
- **doc-topology authoring rules.** Two defensive rules added to the shipped `templates/doc-topology.json` (and the project SOP): the capabilities table tracks **abilities, not a doc inventory** (doc-to-feature relatedness is an inbound graph link, never a table row; rows phrased user-facing); and section-content conventions that ban node-ids, reference-links, and code line numbers in prose (relationships belong in the links array; reference code by symbol/node and let the graph resolve line ranges).
- **Cycle staleness bracketing — entry + pre-merge baseline checks.** Every cycle is now bracketed by the *same* `axiom_graph_check` at two points so its staleness reads as a verified delta, not baseline noise. Phase 1 captures main's counts then runs an **entry check** in the freshly-provisioned worktree (left bracket) — the worktree code is identical to main, so any *extra* `CONTENT_UPDATED`/`NOT_FOUND` is an environment divergence (a missing scanner dependency) to fix before building, not cycle drift. Phase 6 runs the **pre-merge check** before the merge HUMAN GATE (right bracket): it classifies staleness as *explained* (entry baseline + the change-set) or *unexplained* (anything the change-set can't account for), surfaces unexplained drift at the gate, and records the verdict (`clean`/`unexplained-drift`) in the manifest `change-set` section. Motivating failure: a missing language-parser extra once produced 333 stale-in-worktree vs 12 on main — ~300 nodes of pure environment noise.
- **Auditor reconciliation — trust the Reviewer's validation of code/test nodes.** The Reviewer already validates changed code and tests pre-merge (full suite at Pass 0 + reverse-map at Pass 2); the Auditor used to re-confirm all of it by hand, node by node. It now reads the `review` verdict and the pre-merge check verdict, and on a passing verdict (`PASS`/`PASS_WITH_CONCERNS`) + a `clean` check it **batch-marks clean** the in-scope code/test nodes whose staleness the change-set explains (own-`CONTENT_UPDATED` in the change-set, or `LINKED_STALE` whose `via` trigger is in the change-set; excluding any Reviewer-flagged node) without a per-node diff read, and hand-reviews only the *residual* (staleness the change-set can't explain, or a flagged node). Docs are never reconciled — always hand-reviewed. The sticky-`LINKED_STALE` invariant holds (cleared only by this explicit `mark_clean`); a failing gate (`FAIL` or `unexplained-drift`) reverts to full hand-review. Needs no new Reviewer output — it consumes the existing verdict.
- **Env-gap recovery for test parity.** Worktree provisioning now targets parity with main for *both* jobs — the scanners resolving every node **and** the full suite running (the Builder's TDD and the Reviewer's Pass 0 both execute it). A missing *test-only* dependency won't show as a stale node, so it surfaces only when a test can't run: the Builder (manifest `env_gaps`) and Reviewer (verdict `env_gaps`, Pass 0) report a test that **couldn't run because a dependency main already declares is missing** — distinct from an assertion failure — and the orchestrator restores parity (installs that dependency, rebuilds the index, re-dispatches) instead of failing the run. Guardrail at every touchpoint: restoring parity installs a dep main *already declares*; a brand-new library a test needs is the Builder's to add to `pyproject`/lockfile and is reviewed like any other change — never silently absorbed.

### Changed

- **dFlow → axiom-annotations rebrand.** The legacy dFlow naming is replaced by axiom-annotations across agents, skills, and templates. The `dflow-markers` reference skill is renamed `axiom-annotations-markers`, with all live inbound links repointed.
- **Deterministic Auditor changes-summary.** The Auditor's hand-written change-ledger is replaced by a deterministic `auditor.changes-summary` rendered at Phase 8 from `axiom_graph_report(since_sha=baseline)` — the source of truth is now `node_history`, not Auditor narrative. The Doc Reviewer is a drift scanner only, with a supplemental linked-drift survey that does not re-grade the Auditor.
- **Skill & template refinements.** Pre-cycle snapshot housekeeping touched the `pev-cycle`, `pev-builder`, `pev-reviewer`, and `pev-doc-reviewer` skills plus the `pev-orchestrator-reference`, `cycle-manifest-template`, and `review-criteria` templates.
- **`/pev-instance` now owns its documentation updates.** The slim single-agent mode previously *flagged* doc drift and deferred the fix to a future `/pev-cycle`; it now folds the Auditor's role into the one agent. A new **"Audit & doc update"** step updates affected doc sections, runs `axiom_graph_check`, `mark_clean`s the touched nodes, and fixes doc-to-code links, with a self-verify (Doc Reviewer) pass added to the self-review checklist. Doc-drift volume is **never** an escalation trigger — the instance always finishes the doc work. A new pre-flight **clean-graph baseline check** (`axiom_graph_check`, overridable like the dirty-repo gate) runs before planning, so the instance can attribute its Step 6 doc staleness to its own change rather than pre-existing drift. `DESIGN.md`'s "no single agent writes code and updates live docs" invariant is reworded to scope it to full-cycle *subagents*, naming `/pev-instance` as the deliberate single-agent exception (its tool-matrix cells for live-doc writes and `mark_clean` flip to ✓). Plugin `README`/`USER_GUIDE` and the rendered consumer mirror updated to match.
- **Self-contained completion (dropped the external `finishing-a-development-branch` dependency).** Phase 8 / Completion Cleanup no longer invokes `superpowers:finishing-a-development-branch` — its menu didn't fit PEV reality (the worktree branch is already merged and removed) and it broke when `superpowers` was absent (headless/cron, or a consumer who never installed it). The orchestrator now presents its own integration options inline behind a HUMAN GATE — keep local / push to origin / open PR, scoped by how far ahead of origin `main` is — under a generalized rule: **the cycle invokes no skill the user hasn't explicitly asked for** (not a named-skill blocklist). The existing "do not invoke `requesting-code-review`" note stays as the rationale (Phase 5 already reviewed the code).
- **Package-manager-agnostic worktree provisioning.** The env-parity step is no longer a hardcoded `poetry install` invocation. It states the principle — provision to parity with main, installing whatever extras/groups the scanners *and* the test suite need — and gives poetry/pip/uv/npm as interchangeable examples, so the cycle runs on any repo with `axiom-graph` installed, not just this one. Pre-existing project-specific snippets (the frontend-deps path, venv cleanup) are marked as examples rather than presented as the only way.

### Removed

- **Auditor honesty-gate and change-ledger.** The orchestrator-side honesty-gate (pev-cycle Phase 7 reconciliation between Impact Report counts and the hand-typed ledger) and the Auditor's hand-written `auditor.change-ledger` are gone, superseded by the deterministic changes-summary above.
- **Doc Reviewer `auditor_cross_check` pass.** The Doc Reviewer's second pass that re-graded the Auditor is removed; it is now a drift scanner only.

## [1.1.1] — 2026-05-02

### Changed

- **PEV audit agents — expanded tool allowlists.** `pev-audit-dev-shard` gains `axiom_graph_add_link` and `axiom_graph_purge_node` (the latter for deleted-intentionally ghost-node resolution); both consumer audit agents (`pev-audit-consumer-discovery`, `pev-audit-consumer-verifier`) gain `axiom_graph_add_link`; the verifier additionally gains `axiom_graph_drift_query` and `axiom_graph_diff` (both already invoked by its three-layer signal workflow but previously missing from the allowlist). Dev-shard agent body updated so the mutation-surface description and Pass-1 ghost-resolve options match the new allowlist.

## [1.0.0] — 2026-04-29

First release in the `ddpoe/axiom-graph` marketplace. Plugin content is byte-identical to the final release of the previous marketplace (`ddpoe/pev-agent-nexus`); only the install namespace changed. Version reset to 1.0.0 because:

- Marketplace name changed (`pev-agent-nexus` → `axiom`), so install IDs differ — `pev@axiom-graph` is a new namespace from the consumer's perspective.
- Plugin tags are now prefixed (`pev-v*`, `hook-spike-v*`) and decoupled from each other; starting both at 1.0.0 lets each evolve independently from a clean baseline.
- Reversibility note: a `git filter-repo --subdirectory-filter pev_nexus_agents/pev` extraction would carry the new `pev-v*` tag history with it.

Install commands (replace any prior install):

```bash
claude plugin marketplace add ddpoe/axiom-graph
claude plugin install pev@axiom-graph
claude plugin install hook-spike@axiom-graph
```

## Pre-1.0.0 history

Earlier release notes and plan documents live in the archived previous home of these plugins:

- **Repo:** [`ddpoe/pev-agent-nexus`](https://github.com/ddpoe/pev-agent-nexus) (archived)
- **CHANGELOG:** [`ddpoe/pev-agent-nexus/CHANGELOG.md`](https://github.com/ddpoe/pev-agent-nexus/blob/main/CHANGELOG.md)
- **Plan archive:** [`ddpoe/pev-agent-nexus/docs/superpowers/plans/`](https://github.com/ddpoe/pev-agent-nexus/tree/main/docs/superpowers/plans)

Install commands shown in those historical entries reference the old marketplace and are no longer valid.
