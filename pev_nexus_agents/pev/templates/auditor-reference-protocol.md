# Auditor Reference Protocol

## Purpose

Single reference for the PEV Auditor agent. Combines post-implementation documentation updates with the change-scoped Link Audit. The Auditor skill points at this doc.

The Auditor IS the post-implementation protocol — there is no separate step. Follow sections in order: post-implementation updates first (fast, targeted), then audit checks (systematic), then checkpoint.

Two modes:
- **PEV cycle** — the Auditor reads the `change-set` section from the cycle manifest to categorize findings as `expected` (in the change-set) or `collateral` (from external merges or indirect effects).
- **Manual audit** — run independently via `axiom-graph check`. No cycle manifest; all findings are treated equally.

## Feature Doc Hierarchy

The project uses a structured doc hierarchy under `docs/features/`. The Auditor must understand this structure to find and update the right docs.

### Directory structure

```
docs/features/{feature}/
    prd.json                    ← Feature PRD
    design.json                 ← Design spec
    user-guide.json             ← User guide (if applicable)
    interfaces/
        cli.json                ← CLI interface spec
        data-model.json         ← DB schema, tables, columns
        {other}.json            ← Other interface specs as needed
    sub_features/{sub-feature}/
        prd.json                ← Sub-feature PRD (lighter than feature PRD)
        design.json             ← Sub-feature design spec
        workflows/              ← Workflow diagrams (if applicable)
```

### Doc types and their key sections

**Feature PRD** (`prd.json`):
- `problem` — Problem statement
- `user-stories` — User stories (may have sub-sections per phase)
- `requirements` — V1 requirements
- `non-goals` — Scope shield
- `icebox` — Future ideas

**Sub-feature PRD** (`sub_features/{name}/prd.json`):
- `problem` — Problem statement
- `user-stories` — User stories (prefixed US-XX-##)
- `current-capabilities` — **Status table** of what's built (Done / In progress / Not started). This is the primary section the Auditor updates after a Builder implements new capabilities.
- `backlog` — Planned enhancements

**Design spec** (`design.json`):
- `architecture` — High-level system flow
- `sequence-diagram` — Mermaid diagrams
- `data-modeling` — Data model and schema
- `decision-log` — Implementation trade-offs
- `verification-plan` — Test strategy

**Interface specs** (`interfaces/*.json`):
- `data-model.json` — DB tables, columns, schemas. **Critical to update when the Builder adds/modifies schema.**
- `cli.json` — CLI commands, subcommands, flags, options
- Other interface specs — tool parameters, return types, examples

### Discovering feature docs

To find which feature docs to update, map the Builder's changed files to feature areas. Use the directory structure under `docs/features/` — each top-level directory corresponds to a feature area. Then search for existing docs:

```
axiom_graph_list(location="docs/features/")
axiom_graph_search(query="features {feature-area}", node_type="doc")
```

Walk the feature directory to find PRDs, design specs, and interface specs for the affected area. If a sub-feature directory exists under the feature, check its PRD's capabilities table.

## Post-Implementation Updates

After `axiom_graph_build` on merged main, perform these updates before the audit checks. Use `axiom_graph_update_section` to patch sections directly.

### 1. Sub-feature PRD capabilities table
**Section:** `current-capabilities` in the relevant sub-feature PRD.
**Action:** Read the current capabilities table. Cross-reference against the Builder's `change-set` and the Architect's user stories (the outcomes that define "done"). For each capability that the Builder implemented:
- If the capability row exists with status "Not started" or "In progress", update to "Done"
- If the capability is new (not in the table), add a new row
- If the capability was partially implemented, update to "In progress" with a note

**Tool:** `axiom_graph_update_section(section_id=..., content=<updated table>)`

**Also check the backlog section** — if a backlog item was implemented by the Builder, remove it from the backlog and ensure it appears in `current-capabilities` as Done.

### 2. Interface specs (if applicable)
**Action:** Read the relevant interface spec. Add new commands, flags, parameters, DB tables/columns, or API endpoints that the Builder added. Remove deprecated ones. Update examples if behavior changed.

**Critical triggers:**
- Builder added/modified DB tables or columns → update `data-model.json`
- Builder added/modified CLI commands or flags → update `cli.json`
- Builder added/modified MCP tool parameters → update the tool's interface spec
- Builder added/modified graph node or edge types → update `ontology.json`

### 3. Design spec (if architecture changed)
**Action:** Update architecture, sequence diagrams, data model, or decision log if the implementation changed the system structure. Add a decision log entry if the Builder made a significant trade-off.

### 4. Doc-to-code links
**Tool:** `axiom_graph_add_link(section_id=..., node_id=...)`
**Decision test:** If a developer rewrites the linked function, would this section need review? If yes, link it.
**What to link:** Public entry points named in the prose, functions whose contract is explicitly documented.
**What NOT to link:** Private helpers (unless the section documents their internals), modules mentioned for orientation, test functions.

See the Linking Policy section below for detailed rules.

### 5. Create missing docs

If the Builder's work created a new subsystem, feature area, or significant capability that has no corresponding documentation, create docs from templates rather than leaving gaps.

**Templates** are at `docs/templates/`:

| Gap | Template path |
|---|---|
| Sub-feature needs PRD | `docs/templates/sub_feature_template/sub_feature_prd_template.json` |
| Sub-feature needs design spec | `docs/templates/feature_template/design_spec_template.json` |
| New feature needs PRD | `docs/templates/feature_template/product_review_document_template.json` |
| New feature needs design spec | `docs/templates/feature_template/design_spec_template.json` |

**How to create:** Read the template, populate sections from the Architect's pitch (problem statement, user stories) and the Builder's manifest (what was built → capabilities table). Write with `axiom_graph_write_doc` to the appropriate path in the feature hierarchy.

**Placement:** If the changed code lives under a module that already has a feature doc, the new doc is a sub-feature under that feature. If the code is an entirely new top-level module, create a new feature directory. Use NEEDS_INPUT only for genuine ambiguity.

**When to create:**
- Builder implemented a new subsystem with 3+ public functions and no existing sub-feature PRD → create one
- Builder added a new interface type (new CLI subcommand group, new MCP tool category) with no interface spec → create one
- Builder's work is substantial enough to warrant its own design spec (new architecture, new data model) and none exists → create one

**When NOT to create:** Small additions to existing subsystems that are already documented. A single new helper function doesn't need its own sub-feature PRD.

## Staleness & Clean Review

**Only the Auditor marks nodes clean.** `axiom_graph_mark_clean` is exclusively an Auditor tool. Every `mark_clean` call is a deliberate, evidence-backed judgment. For **residual and doc nodes** the evidence is your own diff read; for **reconciled code/test nodes** (see the reconciliation paragraph below) the evidence is the Reviewer's pass plus the clean pre-merge bracket — those are batch-cleaned without a per-node read.

**Scope determination:** The Auditor determines review scope empirically:

1. **`axiom_graph_check`** — the primary signal. Every stale node after `axiom_graph_build` is in scope for review.
2. **Builder's `change-set`** — what the Builder actually changed. Used to categorize findings as `expected` (in change-set) or `collateral` (not in change-set, from external merges or indirect effects).
3. **Architect's coarse scope boundary** — which modules/subsystems were in scope. Sanity check only — flag if the Builder touched something wildly outside scope.

The Auditor does NOT use the Architect's pitch to enumerate individual nodes for review. The staleness engine answers "what changed and needs review" mechanically.

**Reviewer-validated reconciliation.** When the cycle's `review` verdict is `PASS`/`PASS_WITH_CONCERNS` and the manifest's pre-merge baseline check is `clean`, the changed **code and test** nodes were already validated by the Reviewer (full suite + reverse-map). Partition the in-scope code/test nodes: **reconciled** (staleness change-set-explained — own-`CONTENT_UPDATED` in the change-set, or `LINKED_STALE` whose `via` trigger is in the change-set; minus any Reviewer-flagged node) go straight to **batch `mark_clean`** with a reason citing the Reviewer pass; **residual** (staleness the change-set can't explain, or a flagged node) get hand-reviewed. Docs are always hand-reviewed — never reconciled. If the verdict is `FAIL` or the pre-merge check shows `unexplained-drift`, there is no blanket — hand-review everything. The sticky-`LINKED_STALE` invariant holds either way: nodes clear only via this explicit `mark_clean`.

After `axiom_graph_build` + `axiom_graph_check`, review every **residual** stale node and every **doc** node (reconciled code/test nodes were batch-cleaned per the paragraph above):

- **AGREE** (node is fine) → `mark_clean` with reason → remove tag. Scope: `expected` if in change-set, `collateral` if not.
- **DOC NEEDS UPDATE** → `axiom_graph_update_section` → `mark_clean` → remove tag
- **CODE NEEDS FIX** → add to `needs_fix` in Impact Report for user review, do NOT mark clean

Also re-check any `AGENT_VERIFIED` events via `axiom_graph_report` — verify the agent's judgment was correct.

**Key principle:** Stale ≠ broken. Most stale nodes after a Builder run are fine — changed intentionally. For residual and doc nodes: read the diff, make a judgment, mark clean. Only flag things that are actually wrong. (Reconciled code/test batches skip the diff read — they ride on the Reviewer's pass plus the clean pre-merge bracket.)

## Link Audit (change-scoped) & Whole-graph Hygiene

After the staleness review, run the change-scoped **Link Audit** — the shared procedure is in `${CLAUDE_PLUGIN_ROOT}/templates/link-audit-reference.md` (the three verbs add/repoint/drop, detection, the granularity rule, term families). Read the project's `Scope` from `.pev/doc-topology.json` (`link-audit` section; a pre-1.3 topology may name it `semantic-sweep`). Auditor disposition: patch content fixes directly; record each add/repoint/drop as a verb-tagged `proposed_links` entry for the Phase 8 gate (see the §4a.0 skill step).

**Whole-graph hygiene is NOT run per-cycle.** A tree-wide census of unlinked public nodes or orphan/broken edges is change-independent maintenance that re-flags standing conditions every cycle and dilutes the signal — it lives in `/pev-audit-dev-docs`. The change-scoped slices that matter this cycle are already covered:

- **New public surface** this change added → linked in §4a (post-implementation doc-to-code links), filtering `_`-prefixed (unless core internal with its own section), `test_`-prefixed, fixtures/helpers, external-package and entity nodes.
- **Orphan / dead links** from this change's renames or deletions → caught in the staleness review as `BROKEN_LINK` / `NOT_FOUND`; repoint to the new ID, or remove the dead link and update the prose.
- **Over-fanned or wrong-granularity edges** in the change's neighbourhood → the Link Audit's `repoint` / `drop` verbs above.

(Section length is surfaced standing by `axiom_graph_check` as `DOC_SECTION_LONG`; split oversized sections opportunistically when you touch them. The retired composite-coverage <50% and fan-out >8 metrics were arbitrary thresholds — `list_undocumented` by identity and the Link Audit's kind-aware judgment are the real signals.)

## Reference Policy for Current-State Docs

Current-state docs (PRD, design spec, user guide, interface specs) describe what the system does in the system's own terms. They do **not** back-reference origin docs (ADRs, plans, PEV requests, cycle manifests).

### The rule

- **Origin docs may forward-link into current-state docs.** An ADR section can carry an outbound link to the design or PRD section it shaped. That link is correct.
- **Current-state docs must not back-link to origin docs.** A design or PRD section that says `see ADR-X`, `per plan-Y`, or `cycle pev-Z` is drift. Strip such citations on sight.

### Why this asymmetry

Origin docs are write-once and own their own status (`accepted`, `superseded`, etc.). When an ADR is superseded, you update one place. Inline back-references in design docs don't auto-update — they silently rot. Direction matters: forward links flow with how decisions decay; backward links accumulate stale claims.

### What goes where

- **Trade-offs the implementation revealed** → `design.json::decision-log`, written in the system's terms. Good: *"We use a hash table because the size estimate didn't justify a tree."* Bad: *"Per ADR-007, we chose a hash table."* Same content, no rotting reference.
- **Decisions consequential enough for an ADR** → a new ADR, which forward-links into the design section it affects.
- **Historical "when did this change" questions** → `axiom_graph_history(node_id=...)` and the commit log. Git already records the timeline; current-state prose does not.
- **"What origin docs touched this section?"** → `axiom_graph_graph(node_id=..., direction="in")`. Computed from current inbound links; superseded ADRs and accepted ones are both visible.

### Auditor enforcement

When updating PRD or design content under §1 Sub-feature PRD or §3 Design spec above, **strip any inline references to ADRs, plans, PEV requests, or cycle manifests** that appear in the prose. If the content the citation was carrying still needs to be expressed, rephrase it in the system's terms (a trade-off, a constraint) without the citation. If the citation referenced a real architectural decision that the design section doesn't yet capture, ensure the origin doc itself has a forward `documents` edge into this section via `axiom_graph_add_link` — that's how navigation survives.

## Linking Policy

### Link the contract boundary, not the implementation

The key question: **is the doc section describing what the system does, or how a specific function works?**

- **Behavior docs** (what happens during a build, how purging works) → link the **public entry point** that triggers the behavior. If the private helper gets renamed, the link survives.
- **Mechanism docs** (how `_derive_change_type` decides, how `_extract_sections` parses) → link the **private function directly**. The doc IS about that function's internals.

### What to link
- Public entry points named in the prose (CLI commands, MCP tools, API endpoints)
- Functions whose contract (inputs, outputs, behavior) is explicitly documented
- Private functions when the section describes their internal logic specifically

### What NOT to link
- Functions mentioned only for orientation ("this lives in db.py")
- Test functions, fixtures, and test helpers
- External package nodes
- Entity nodes
- Module-level composite nodes — link the children, not the container

### Audit check for existing links
- For each link to a `_`-prefixed function: verify the doc section is about that function's internals. If it describes broader behavior, relink to the public caller.
- For each link to a composite (module) node: verify the section is about the module's structure. If about a child, relink to the child.

## Resolution & Checkpoint

### Triage order
Process findings in this order (highest impact first):

1. **CODE NEEDS FIX** items → add to `needs_fix` in Impact Report for user review
2. **MISSING DOCS** → create from templates via `axiom_graph_write_doc`
3. **DOC NEEDS UPDATE** → fix via `axiom_graph_update_section` + `mark_clean`
4. **New public surface (this change)** → `axiom_graph_add_link` to existing doc sections
5. **Orphan / broken links (this change)** → repoint to new ID or remove dead link
6. **Link Audit proposals** → record verb-tagged `add` / `repoint` / `drop` in `proposed_links` for the Phase 8 gate
7. **Section length** → split oversized sections opportunistically for maintainability

### After all findings resolved
1. `axiom_graph_build` — re-index
2. `axiom_graph_check` — verify clean state on resolved nodes
3. `axiom-graph history checkpoint --message "pev-cycle-{cycle-id}-audit-complete"` — mark the audit as a reference point

## Quick Reference Checklist

```
## PEV Audit: {cycle-id}

### Post-Implementation
- [ ] Sub-feature PRD: capabilities table updated to Done
- [ ] Interface spec: added/removed commands, flags, options (if applicable)
- [ ] Design spec: updated architecture/decisions (if changed)
- [ ] Missing docs: created from templates for new subsystems/features
- [ ] Doc-to-code links added (per linking policy)

### Staleness Review
- [ ] axiom_graph_build + axiom_graph_check — all stale nodes identified
- [ ] Each stale node: reviewed, marked clean or flagged
- [ ] AGENT_VERIFIED events re-checked
- [ ] Scope categorization: expected vs collateral for each finding

### Link Audit (change-scoped)
- [ ] Link Audit run — add / repoint / drop proposals recorded in `proposed_links` (verb-tagged)
- [ ] New public surface this change added → linked
- [ ] Orphan / broken links from this change → repointed or removed
- [ ] Whole-graph hygiene (coverage / fan-out census) — N/A, delegated to `/pev-audit-dev-docs`

### Completion
- [ ] axiom_graph_build + axiom_graph_check — clean after fixes
- [ ] axiom-graph history checkpoint — audit reference point recorded
- [ ] Impact Report written to cycle manifest auditor section
```
