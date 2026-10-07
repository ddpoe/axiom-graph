# Auditor Reference Protocol

## Purpose

Single reference for the PEV Auditor agent. Combines post-implementation documentation updates with the change-scoped Link Audit. The Auditor skill points at this doc.

The Auditor IS the post-implementation protocol — there is no separate step. Follow sections in order: the change-set doc pass first (the project topology pass and the Link Audit, every cycle, even when `check` is clean), then post-implementation updates (fast, targeted), then the staleness review. The history checkpoint is the merge step's, on main.

Two modes:
- **PEV cycle** — the Auditor reads the `change-set` section from the cycle manifest to categorize findings as `expected` (in the change-set), `sync-explained` (main's own changes, brought into the branch by the orchestrator's sync of main: they read `CONTENT_UPDATED` in the worktree because its index never saw them; which of them one batched `mark_clean` may clear is under **Scope determination** below), or `collateral` (indirect effects neither explains). The Auditor runs in the cycle's worktree, after main was synced into the branch and before the merge; the orchestrator carries its verifications to main after the merge.
- **Manual audit** — run independently via `axiom-graph check`. No cycle manifest; all findings are treated equally.

## Feature Doc Hierarchy

The project uses a structured doc hierarchy under `docs/features/`. The Auditor must understand this structure to find and update the right docs.

### Directory structure

```
docs/features/{feature}/
    prd.docjson                 ← Feature PRD
    design.docjson              ← Design spec
    user-guide.docjson          ← User guide (if applicable)
    interfaces/
        cli.docjson             ← CLI interface spec
        data-model.docjson      ← DB schema, tables, columns
        {other}.docjson         ← Other interface specs as needed
    sub_features/{sub-feature}/
        prd.docjson             ← Sub-feature PRD (lighter than feature PRD)
        design.docjson          ← Sub-feature design spec
        workflows/              ← Workflow diagrams (if applicable)
```

### Doc types and their key sections

**Feature PRD** (`prd.docjson`):
- `problem` — Problem statement
- `user-stories` — User stories (may have sub-sections per phase)
- `requirements` — V1 requirements
- `non-goals` — Scope shield
- `icebox` — Future ideas

**Sub-feature PRD** (`sub_features/{name}/prd.docjson`):
- `problem` — Problem statement
- `user-stories` — User stories (prefixed US-XX-##)
- `current-capabilities` — **Status table** of what's built (Done / In progress / Not started). This is the primary section the Auditor updates after a Builder implements new capabilities.
- `backlog` — Planned enhancements

**Design spec** (`design.docjson`):
- `architecture` — High-level system flow
- `sequence-diagram` — Mermaid diagrams
- `data-modeling` — Data model and schema
- `decision-log` — Implementation trade-offs
- `verification-plan` — Test strategy

**Interface specs** (`interfaces/*.docjson`):
- `data-model.docjson` — DB tables, columns, schemas. **Critical to update when the Builder adds/modifies schema.**
- `cli.docjson` — CLI commands, subcommands, flags, options
- Other interface specs — tool parameters, return types, examples

### Discovering feature docs

To find which feature docs to update, map the Builder's changed files to feature areas. Use the directory structure under `docs/features/` — each top-level directory corresponds to a feature area. Then search for existing docs:

```
axiom_graph_list(location="docs/features/")
axiom_graph_search(query="features {feature-area}", node_type="doc")
```

Walk the feature directory to find PRDs, design specs, and interface specs for the affected area. If a sub-feature directory exists under the feature, check its PRD's capabilities table.

## Link Audit (change-scoped) & Whole-graph Hygiene

**Before the staleness review, every cycle,** run the project topology pass and then the change-scoped **Link Audit** (skill Step 4). Both are driven by the change-set (the Builder's task manifests and the git diff), not by staleness, so a clean `check` doesn't skip them. The Link Audit — the shared procedure is in `${CLAUDE_PLUGIN_ROOT}/templates/link-audit-reference.md` (the three verbs add/repoint/drop, detection, the granularity rule, term families). Read the project's `Scope` from `.pev/doc-topology.docjson` (`link-audit` section). Auditor disposition: patch content fixes directly; add a link the approved pitch asks for (a stated deliverable in `architect::required-artifacts`, or an audit note in `architect::constraints` naming the link) directly and count it in `links_added`; record every other add/repoint/drop as a verb-tagged `proposed_links` entry for the Phase 8 gate (skill Step 4.2). The Impact Report states the step's result as its own `findings` entry, "no proposals" included, and sets `checks_completed.topology` and `checks_completed.link_audit`.

**Whole-graph hygiene is NOT run per-cycle.** A tree-wide census of unlinked public nodes or orphan/broken edges is change-independent maintenance that re-flags standing conditions every cycle and dilutes the signal — it lives in `/pev-audit-dev-docs`. The change-scoped slices that matter this cycle are already covered:

- **New public surface** this change added → linked in skill Step 5a (post-implementation doc-to-code links), filtering `_`-prefixed (unless core internal with its own section), `test_`-prefixed, fixtures/helpers, external-package and entity nodes.
- **Orphan / dead links** from this change's renames or deletions → caught in the staleness review as `BROKEN_LINK` / `NOT_FOUND`; repoint to the new ID, or remove the dead link and update the prose.
- **Over-fanned or wrong-granularity edges** in the change's neighbourhood → the Link Audit's `repoint` / `drop` verbs above.

(Section length is surfaced standing by `axiom_graph_check` as `DOC_SECTION_LONG`; split oversized sections opportunistically when you touch them. The retired composite-coverage <50% and fan-out >8 metrics were arbitrary thresholds — `list_undocumented` by identity and the Link Audit's kind-aware judgment are the real signals.)

## Post-Implementation Updates

After `axiom_graph_build` in the worktree (main already synced into the branch, the merge still to come), perform these updates before the audit checks. Use `axiom_graph_update_section` to patch sections directly. An edit verifies only the text it writes: when the section is `LINKED_STALE` and the edit reconciles it with the changed code, pass `addresses=[the offenders]` (the `via=` ids from `drift_query format="full"`) so it clears; anything left stale is picked up in the staleness review below.

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
- Builder added/modified DB tables or columns → update `data-model.docjson`
- Builder added/modified CLI commands or flags → update `cli.docjson`
- Builder added/modified MCP tool parameters → update the tool's interface spec
- Builder added/modified graph node or edge types → update `ontology.docjson`

### 3. Design spec (if architecture changed)
**Action:** Update architecture, sequence diagrams, data model, or decision log if the implementation changed the system structure. Add a decision log entry if the Builder made a significant trade-off.

### 4. Doc-to-code links
**Tool:** `axiom_graph_add_link(section_id=..., node_id=...)`, for links the approved pitch asks for. Any other link is a `proposed_links` add for the Phase 8 gate (skill Step 4.2).
**Decision test:** If a developer rewrites the linked function, would this section need review? If yes, link it.
**What to link:** Public entry points named in the prose, functions whose contract is explicitly documented.
**What NOT to link:** Private helpers (unless the section documents their internals), modules mentioned for orientation, test functions.

See the Linking Policy section below for detailed rules.

### 5. Create missing docs

If the Builder's work created a new subsystem, feature area, or significant capability that has no corresponding documentation, create docs from templates rather than leaving gaps.

**Templates** are at `docs/templates/`:

| Gap | Template path |
|---|---|
| Sub-feature needs PRD | `docs/templates/sub_feature_template/sub_feature_prd_template.docjson` |
| Sub-feature needs design spec | `docs/templates/feature_template/design_spec_template.docjson` |
| New feature needs PRD | `docs/templates/feature_template/product_review_document_template.docjson` |
| New feature needs design spec | `docs/templates/feature_template/design_spec_template.docjson` |

**How to create:** Read the template, populate sections from the Architect's pitch (problem statement, user stories) and the Builder's manifest (what was built → capabilities table). Write with `axiom_graph_write_doc` to the appropriate path in the feature hierarchy. In `doc_json`, a section's links are `"links": [{"node_id": "<node id>"}]` or bare node-id strings; every link is a `documents` link, so there is no `type` or `target` field.

**Placement:** If the changed code lives under a module that already has a feature doc, the new doc is a sub-feature under that feature. If the code is an entirely new top-level module, create a new feature directory. Use NEEDS_INPUT only for genuine ambiguity.

**When to create:**
- Builder implemented a new subsystem with 3+ public functions and no existing sub-feature PRD → create one
- Builder added a new interface type (new CLI subcommand group, new MCP tool category) with no interface spec → create one
- Builder's work is substantial enough to warrant its own design spec (new architecture, new data model) and none exists → create one

**When NOT to create:** Small additions to existing subsystems that are already documented. A single new helper function doesn't need its own sub-feature PRD.

## Staleness & Clean Review

**Only the Auditor clears staleness.** `axiom_graph_mark_clean` and `axiom_graph_reverify` are exclusively Auditor tools, and so is the third clean action, `addresses=` on an `axiom_graph_update_section` / `axiom_graph_patch_section` of a live doc: it names the offenders the edit reconciles, and the section clears once every offender is named. A doc edit without `addresses=` verifies only the text it writes and never clears `LINKED_STALE`. Every clean call is a deliberate, evidence-backed judgment. For **residual and doc nodes** the evidence is your own diff read; for **reconciled code/test nodes** (see the reconciliation paragraph below) the evidence is the Reviewer's pass plus the clean pre-audit bracket — those are batch-cleaned without a per-node read.

`mark_clean` names each node the evidence covers; `reverify` names a *source* and asserts the change to it was inconsequential to its dependents, clearing the LINKED_STALE rooted at it. That assertion is about the change, not a per-dependent review — which is exactly why it demands care: skim the cascade for dependents describing the changed aspect before calling it, and handle those individually first (`update_section(addresses=[…])`, or `update_section` then `mark_clean`). Under-clearing is recoverable; a wrong blanket silently buries a doc that needed updating.

**Scope determination:** The Auditor determines review scope empirically:

1. **`axiom_graph_check`** — the primary signal for the staleness review. Every stale node after `axiom_graph_build` is in scope for review. It is not the signal for the change-set doc pass: the topology pass and the Link Audit are driven by the change-set and run even when `check` is clean.
2. **Builder's `change-set`** — what the Builder actually changed. Used to categorize findings as `expected` (in the branch's changed files), `sync-explained` (main's changes the sync brought in), or `collateral` (in neither: indirect effects). A node is sync-explained, and goes in one batched `mark_clean` without a per-node read, only when all of these hold: its own status is `CONTENT_UPDATED`; its file is on the change-set's `Brought in by the sync` list and not among the branch's own changed files; its id is not on the change-set's `Main's stale ids at the sync` list; and, if it is also `LINKED_STALE`, its `via=` (`drift_query format="full"`) names no node in the branch's changed files and is not truncated with `(+N more)`. Every other node the sync touched is residual and decided per node: an id already stale on main (a clear in the worktree would carry to main with no one deciding it), a node held `LINKED_STALE` by a branch change (the cycle's own doc drift), a node that is only `LINKED_STALE`, and a node in a file both main and the branch changed (`expected`).
3. **Architect's coarse scope boundary** — which modules/subsystems were in scope. Sanity check only — flag if the Builder touched something wildly outside scope.

The Auditor does NOT use the Architect's pitch to enumerate individual nodes for review. The staleness engine answers "what changed and needs review" mechanically.

**Reviewer-validated reconciliation.** When the cycle's `review` verdict is `PASS`/`PASS_WITH_CONCERNS` and the change-set's pre-audit check (taken in the worktree after the sync) is `clean`, the changed **code and test** nodes were already validated by the Reviewer (full suite + reverse-map). Partition the in-scope code/test nodes: **reconciled** (staleness change-set-explained — own-`CONTENT_UPDATED` in the change-set, or `LINKED_STALE` whose direct offenders — the `via=` field of `drift_query`, up to 10 ids — are all in the change-set; hand-review any row truncated with `(+N more)`; a test whose `validates` edge points into the change-set qualifies too, checked with `axiom_graph_graph(direction="out")` rather than `via=`; minus any Reviewer-flagged node) go straight to **batch `mark_clean`** with a reason citing the Reviewer pass; **residual** (staleness the change-set can't explain, or a flagged node) get hand-reviewed. Docs are always hand-reviewed — never reconciled. If the verdict is `FAIL` or the pre-audit check shows `unexplained-drift`, there is no blanket — hand-review everything. The sticky-`LINKED_STALE` invariant holds either way: nodes clear only via an explicit clean action (`mark_clean`, a guarded `reverify` on a source, or a doc edit naming the offender in `addresses=`). An edit alone never clears `LINKED_STALE`.

After `axiom_graph_build` + `axiom_graph_check`, review every **residual** stale node and every **doc** node (reconciled code/test nodes were batch-cleaned per the paragraph above):

- **AGREE** (node is fine) → `mark_clean` with reason → remove tag. Scope: `expected` if in change-set, `collateral` if not.
- **DOC NEEDS UPDATE** → `axiom_graph_update_section(addresses=[…])`, or `axiom_graph_update_section` then `mark_clean` → remove tag
- **CODE NEEDS FIX** → add to `needs_fix` in Impact Report for user review, do NOT mark clean

After your edits, re-read the sections you wrote (one batched `read_doc(section_ids=[...])`) and report each one's status before and after in the report's `sections_written`.

**Bulk clear:** take a reviewed slice's ids from `drift_query` and pass them in one `mark_clean(node_ids=[...])`. Never clear by filter. Under budget pressure, never `mark_clean` a node you have not read: list it as unread in `audit::progress` and return `CONTINUING`.

**Purge only what is gone.** Never purge a module whose file still exists: its `NOT_FOUND` is inherited from removed children. Repoint the links and purge the removed children only.

Also re-check any `AGENT_VERIFIED` events via `axiom_graph_report(since_sha=baseline, change_type_pattern="AGENT_VERIFIED", detail="condensed", max_chars=12000)` — verify the agent's judgment was correct. Condensed keeps each agent-authored row verbatim; if the footer says rows were dropped, narrow it with `node_pattern` rather than lifting the cap.

**Key principle:** Stale ≠ broken. Most stale nodes after a Builder run are fine — changed intentionally. For residual and doc nodes: read the diff, make a judgment, mark clean. Only flag things that are actually wrong. (Reconciled code/test batches skip the diff read — they ride on the Reviewer's pass plus the clean pre-audit bracket.)

## Reference Policy for Current-State Docs

Current-state docs (PRD, design spec, user guide, interface specs) describe what the system does in the system's own terms. They do **not** back-reference origin docs (ADRs, plans, PEV requests, cycle manifests).

### The rule

- **Origin docs may forward-link into current-state docs.** An ADR section can carry an outbound link to the design or PRD section it shaped. That link is correct.
- **Current-state docs must not back-link to origin docs.** A design or PRD section that says `see ADR-X`, `per plan-Y`, or `cycle pev-Z` is drift. Strip such citations on sight.

### Why this asymmetry

Origin docs are write-once and own their own status (`accepted`, `superseded`, etc.). When an ADR is superseded, you update one place. Inline back-references in design docs don't auto-update — they silently rot. Direction matters: forward links flow with how decisions decay; backward links accumulate stale claims.

### What goes where

- **Trade-offs the implementation revealed** → `design.docjson::decision-log`, written in the system's terms. Good: *"We use a hash table because the size estimate didn't justify a tree."* Bad: *"Per ADR-007, we chose a hash table."* Same content, no rotting reference.
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
3. **DOC NEEDS UPDATE** → fix via `axiom_graph_update_section(addresses=[…])`, or `axiom_graph_update_section` then `mark_clean`
4. **New public surface (this change)** → `axiom_graph_add_link` when the pitch asks for the link; otherwise a `proposed_links` add
5. **Orphan / broken links (this change)** → repoint to new ID or remove dead link
6. **Link Audit proposals** → record verb-tagged `add` / `repoint` / `drop` in `proposed_links` for the Phase 8 gate
7. **Section length** → split oversized sections opportunistically for maintainability

### After all findings resolved
1. `axiom_graph_build` — re-index
2. `axiom_graph_check` — verify clean state on resolved nodes
3. No checkpoint here: the merge step creates the run's history checkpoint on main, after the `PEV complete:` commit (`templates/merge-step-reference.md`)

## Quick Reference Checklist

```
## PEV Audit: {cycle-id}

### Change-set doc pass (every cycle, before the staleness review)
- [ ] Topology pass: each triggered category's Auditor action done
- [ ] Link Audit run from the change-set, old-behaviour wording included; add / repoint / drop proposals recorded in `proposed_links` (verb-tagged)
- [ ] Result stated in the Impact Report, "no proposals" included; `checks_completed.topology` and `.link_audit` set

### Post-Implementation
- [ ] Sub-feature PRD: capabilities table updated to Done
- [ ] Interface spec: added/removed commands, flags, options (if applicable)
- [ ] Design spec: updated architecture/decisions (if changed)
- [ ] Missing docs: created from templates for new subsystems/features
- [ ] Doc-to-code links added (per linking policy)

### Staleness Review
- [ ] axiom_graph_build + axiom_graph_check — all stale nodes identified
- [ ] Each stale node: reviewed, marked clean or flagged; none cleared unread
- [ ] Sections written: re-read, before/after status in `sections_written`
- [ ] AGENT_VERIFIED events re-checked
- [ ] Scope categorization: expected vs collateral for each finding

### Link hygiene (change-scoped)
- [ ] New public surface this change added → linked
- [ ] Orphan / broken links from this change → repointed or removed
- [ ] Whole-graph hygiene (coverage / fan-out census) — N/A, delegated to `/pev-audit-dev-docs`

### Completion
- [ ] axiom_graph_build + axiom_graph_check — clean after fixes
- [ ] No history checkpoint: the merge step records it on main
- [ ] Impact Report written to cycle manifest auditor section
```
