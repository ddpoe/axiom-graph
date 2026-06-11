---
name: pev-audit-annotations
description: PEV Audit — backlog cleanup of annotation rule violations (duplicate step_num, sequence gaps, undecorated AutoStep targets), prose drift in docstrings/level_1/level_2 summaries, and coverage-gap candidates. Sequential single-fixer dispatch with batched human gate before any inline mechanical fix lands. Risky/judgmental fixes spawn `docs/pev-requests/` for `/pev-cycle` or `/pev-instance`.
user-invocable: true
---

# PEV Audit — Annotations

You orchestrate a backlog audit of annotation drift. A single `pev-audit-annotations-fixer` subagent triages findings in three categories — annotation rule violations, prose drift, coverage gaps — and either applies mechanical fixes inline (via `Edit` / `Write`) or drafts spawn-request payloads for risky/judgmental fixes. You (the orchestrator) own all `axiom_graph_write_doc` calls for the spawn requests; the fixer drafts payloads but does not author the actual `docs/pev-requests/*` docs.

`${CLAUDE_PROJECT_DIR}` is the consumer project root. `${CLAUDE_PLUGIN_ROOT}` is the PEV plugin's install directory.

**Reference docs (read before first use):**

- Design spec: `axiom_graph::docs.features.pev-agent-nexus.sub_features.audit-skills.design`
- Sub-feature PRD: `axiom_graph::docs.features.pev-agent-nexus.sub_features.audit-skills.prd`

**v1 posture (key constraints):**

- **Standalone (D-21).** This skill does NOT depend on `/pev-audit-dev-docs`. There is no automated handoff payload — the fixer discovers its own findings via `axiom_graph_check` + `axiom_graph_list_undocumented` + workflow inspection tools. Optional intake guidance from the user (a target subtree, e.g. `axiom_graph/scanners/`) gives a way to scope a run without requiring a prior dev-docs run.
- **Sequential dispatch (D-20).** A single fixer agent type. If the finding set is large, you may dispatch it in batches — one batch at a time, never two in flight.
- **No worktree.** Audit work mutates the graph DB and (for inline edits) source files in the main working tree. There is no `--worktree` override.
- **Zero audit-specific hooks (D-19).** Tool allowlists live in agent frontmatter. The tag-mutex check, resume detection, and spawn-request writes live inline in this skill body.
- **Inline code edits are in scope — but only mechanical, low-risk ones (D-7, D-22).** The fixer agent has `Edit` / `Write`. Risky fixes spawn `docs/pev-requests/{slug}.json` docs that the user invokes via `/pev-cycle` or `/pev-instance`.
- **HUMAN GATE before any inline edit lands.** The fixer drafts both inline-edit proposals and spawn-request payloads; you present them as a batch, the user approves/rejects, then you direct the fixer to apply approved inline edits and you write the approved spawn requests. No edit is applied without that gate.

## Phases

### 1. Intake

Parse the user's `/pev-audit-annotations` request. The request may include:

- A path to an audit-request doc in `docs/pev/audit-requests/{slug}.json` (optional) — narrows scope, sets goal, supplies user-authored constraints.
- Free-text scope hints (e.g., "focus on the scanners feature", "only B-class violations, skip prose drift"). The hints scope discovery — they do NOT change the fixer's classification rules.
- No argument — defaults to a full-tree audit across all three finding categories.

Read `axiom-graph.toml` in the project root to get the `project_id` value. The audit manifest doc ID is `{project_id}::docs.pev.audits.pev-audit-annotations-YYYY-MM-DD-{slug}` — do NOT hardcode the prefix; it varies per project.

**Slug generation.** If the user supplied an audit-request, reuse its slug. Otherwise generate a short descriptive slug from the scope hints (e.g., `scanners-cleanup`, `b-class-sweep`, `full-tree`). Date-prefix `YYYY-MM-DD` is added by manifest naming; the slug itself is just the descriptive tail.

**Tag-mutex check (inline, no hook).** Run:

```
axiom_graph_list_tags(project_root="${CLAUDE_PROJECT_DIR}")
```

…and check whether any existing manifest carries the `pev-audit-active` tag.

- If `pev-audit-active` is present → an interrupted audit run exists. Read that manifest's `meta` section to determine type and last state. Present to user:

  ```
  An interrupted audit exists: {existing_manifest_id}
    Type: {meta.audit-type}
    Status: {meta.status}
    Last activity: {meta.last-activity-summary or "discovery"}
  Options:
    (1) RESUME — pick up where it left off (only if same type: pev-audit-annotations)
    (2) RELEASE — remove pev-audit-active tag, mark prior run partial, start a new run
    (3) CANCEL — leave it alone and abort this invocation
  ```

  **HUMAN GATE.**

  If the interrupted run is a different audit type (`pev-audit-dev-docs`, `pev-audit-consumer-docs`), only options 2 and 3 are valid — resume across types is not supported.

- If no existing audit → present the cycle plan to the user:

  ```
  PEV Audit (annotations): pev-audit-annotations-{date}-{slug}
  Manifest doc ID: {project_id}::docs.pev.audits.pev-audit-annotations-{date}-{slug}
  Audit-request: {linked-request-doc-id-or-none}
  Scope hints: {hints-or-"full-tree"}
  Proceed? (or suggest a different slug)
  ```

  **HUMAN GATE.**

Once confirmed:

1. Create the audit manifest via `axiom_graph_write_doc`. The `id` field is path-slug form (`pev/audits/pev-audit-annotations-{date}-{slug}`), NOT the full node-id. Tags: `pev-audit-active`, `pev-audit-annotations`. Initial sections: `meta`, `request` (pointer or copy of audit-request), `orchestrator.discovery` (placeholder), `orchestrator.handoff.recommended-requests` (empty list), `code-edits-applied` (empty), `friction` (empty).
2. `meta` content includes: `status: active`, `audit-type: annotations`, `started-at: {ISO timestamp}`, `dispatcher: pev-audit-annotations`, `request-link: {audit-request-doc-id or null}`, `scope-hints: {hints or "full-tree"}`.

### 2. Discovery

Build the finding set across all three categories.

**Category 1 — Annotation rule violations.** Use the workflow inspection tools to surface B-class findings:

```
axiom_graph_workflow_list(project_root="${CLAUDE_PROJECT_DIR}", scope="all", max_results=200)
```

For each workflow returned, run `axiom_graph_workflow_detail` (when relevant) to surface duplicate `step_num`, sequence gaps, unresolved `AutoStep` targets. Cross-reference with any project-specific annotation linter output if the project surfaces one (e.g., a CI check). On the cortex project at the time of writing, ~10 unresolved B-class findings are known.

**Category 2 — Prose drift.** Walk own-stale events:

```
axiom_graph_drift_query(project_root="${CLAUDE_PROJECT_DIR}", filter="doc_quality", format="full")
```

`CONTENT_UPDATED` and `DESC_UPDATED` events flag node-level summaries (`level_1` / `level_2`) that drifted from code. Cross-check docstrings via `axiom_graph_source` for affected nodes — the source text is the authority.

**Category 3 — Coverage gaps.** Surface candidates:

```
axiom_graph_list_undocumented(project_root="${CLAUDE_PROJECT_DIR}")
axiom_graph_workflow_list(project_root="${CLAUDE_PROJECT_DIR}", scope="all", has_steps=False)
```

Functions without docstrings, workflows without `Step()` markers, atomic processes with no `level_1` summary at all. Most are out-of-scope (unannotated helpers don't necessarily need annotation), but flag candidates that look like real workflows or user-facing entry points.

**Apply scope filters.** If the audit-request or intake hints narrow scope (e.g., "focus on `axiom_graph/scanners/`"), drop findings whose source-location doesn't match. If hints say "only B-class", drop categories 2 and 3 entirely.

Write the finding inventory to `orchestrator.discovery`:

- Counts by category and by sub-type (e.g., `category-1.duplicate-step-num: 3`, `category-2.linked-stale-summary: 17`, `category-3.workflow-missing-steps: 5`)
- In-scope finding list with stable IDs (`finding-{N}` — sequential within this audit), each with: id, category, source-location, current-state-summary, suggested-classification (`mechanical-likely` / `judgmental-likely` / `unclear`)
- Out-of-scope findings (with reason — e.g., `scope-excluded`, `out-of-scope-helper`)

Pre-create the per-finding placeholder sections (`findings.{finding-id}`) via `axiom_graph_add_section` so the fixer only needs `update_section`. Same approach as cycle 1's pre-creation pattern in `/pev-audit-dev-docs`.

If discovery returns zero in-scope findings, write the empty inventory, present "no annotation drift to audit" to the user, mark manifest `completed` with note, exit gracefully.

### 3. Triage dispatch (sequential)

Dispatch the `pev-audit-annotations-fixer` agent. **Sequential per D-20** — one dispatch at a time. If the finding set is large (heuristic: > ~25 findings), batch into multiple sequential dispatches; otherwise, one dispatch is fine. Re-evaluate the threshold after first run.

Use `subagent_type="pev-audit-annotations-fixer"`. Do NOT use `isolation: "worktree"`.

**Dispatch prompt template:**

```
You are the pev-audit-annotations-fixer agent for audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Batch: {batch-id (1, 2, ...) or "single"}
Finding IDs in this batch: [{finding-id-list}]

Read each finding's metadata from orchestrator.discovery in the manifest. Per-finding placeholder sections (findings.{finding-id}) are pre-created — use update_section, not add_section.

For each finding:
  1. Inspect the code via axiom_graph_source + Read.
  2. Decide disposition: applied-inline | request-spawned | needs-input | false-positive.
  3. If applied-inline: do NOT make the Edit yet. Draft the proposed edit (file path, line span, before/after snippets) into findings.{finding-id}.proposed-inline-edit. The orchestrator will gate user approval before any Edit lands.
  4. If request-spawned: draft the spawn-request payload into findings.{finding-id}.spawn-request-draft (slug, title, summary, scope, source-finding-ids).
  5. If needs-input: write the question into findings.{finding-id}.needs-input and return NEEDS_INPUT batched.
  6. If false-positive: only after NEEDS_INPUT confirmation; record reason.

You have Edit/Write — but DO NOT use them in this dispatch. You'll be re-dispatched in apply mode after the user gate (Phase 5).

Cluster correlated findings (same workflow, same module). A cluster either applies as one inline edit OR spawns one request — never fragment.

Return FIXER_DONE with disposition counts, NEEDS_INPUT batched, or CONTINUING on turn-limit.
```

**Why drafts only.** This dispatch is "draft mode". The fixer agent classifies and drafts but does not apply. The user gate in Phase 5 protects against a doubtful inline fix landing without review. After the gate, we re-dispatch in apply mode for approved inline edits only (Phase 6).

Handle returns per dispatch:

- **`FIXER_DONE`** — record disposition counts in `orchestrator.executions.draft.{batch-id}`. Continue to next batch (or to Phase 4 if no more batches).
- **`NEEDS_INPUT`** — collect the agent's questions, batch with any other batches' questions. After all batches return, ask via `AskUserQuestion`. Resume each agent with `SendMessage` containing answers and the agent's `context` field.
- **`CONTINUING`** — re-dispatch the same batch; already-classified findings persist in their `findings.{finding-id}` sections, so progress carries across re-dispatches.

### 4. Spawn-request flow (drafts → real docs, optional pre-gate review)

Walk every finding with disposition `request-spawned`. For each `spawn-request-draft`:

1. Generate a descriptive slug from the draft's `slug` field (collision-check against existing `docs/pev-requests/*.json` — append `-2`, `-3`, etc. if needed).
2. Compose the request DocJSON:
   - `title` — short human-readable
   - `tags` — `["pev", "request", "audit-spawned"]`
   - `meta.source-audit` — the audit manifest doc ID (provenance)
   - `sections` — `problem`, `proposed-approach`, `scope`, `notes` (populated from the draft's `summary` / `scope` / `source-finding-ids` fields). Keep proposed-approach generic ("apply mechanical fix per linked finding"); the actual implementation is the `/pev-cycle` or `/pev-instance` runner's job.

You may write these speculatively now (so the user sees the spawned-request slugs at the gate) OR wait until after the gate. Pick the timing that produces a cleaner gate UX — typically: write speculatively, then DELETE any rejected at the gate via `axiom_graph_delete_doc` (rare path).

Append each spawned request's doc-ID to `orchestrator.handoff.recommended-requests`.

### 5. Batch human gate

Present the entire batch to the user:

- **Proposed inline edits**, grouped by file:
  ```
  File: path/to/file.py
    Finding finding-3 (mechanical: missing @workflow decorator)
      Lines 142-142
      Before: def process_event(payload):
      After:  @workflow(purpose="Process incoming scanner event")
              def process_event(payload):
    Finding finding-7 (mechanical: duplicate step_num=2)
      Lines 87-87
      Before: 口 = Step(step_num=2, name="Validate")
      After:  口 = Step(step_num=3, name="Validate")
  ```

- **Drafted spawn requests**, one per cluster:
  ```
  Spawn request: renumber-step-markers-cli-handler
    Title: Renumber step markers in cli_handler.py to close sequence gaps
    Source findings: finding-2, finding-5, finding-9, finding-12
    Scope: 12 marker renumbers across 1 file; risk of test-id churn
    Why spawned (not inline): cluster spans 12 markers; mechanical but high-blast-radius — better as a /pev-instance with test review
  ```

- **Needs-input** items still outstanding (rare here — Phase 3 should have batched them all, but list any that surfaced after re-dispatch).

- **False-positive** items (informational, no action needed).

**HUMAN GATE.** "Approve all, approve some (specify which to skip), or reject with feedback?"

Record the user's per-item decisions in `orchestrator.gate.user-decisions`. Format: `{finding-id: approved | skipped | rejected-with-feedback}` plus per-spawn-request: `{slug: approved | rejected}`.

For `rejected-with-feedback` findings, redispatch only those findings to the fixer with the feedback appended — same dispatch shape as Phase 3, but the dispatch prompt notes "user rejected this finding's prior disposition; reclassify per: {feedback}". Re-iterate Phases 3-5 for those findings until the user approves a disposition or declines further iteration.

For rejected spawn requests written speculatively in Phase 4, delete them via `axiom_graph_delete_doc` and remove the entry from `orchestrator.handoff.recommended-requests`.

### 6. Apply approved inline edits

Re-dispatch the fixer in apply mode for the approved inline edits only.

**Dispatch prompt template (apply mode):**

```
You are the pev-audit-annotations-fixer agent in APPLY mode for audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Approved finding IDs: [{finding-id-list}] — user has approved the proposed-inline-edit drafts on these.

For each approved finding:
  1. Re-read findings.{finding-id}.proposed-inline-edit to recover the file path / line span / before-snippet / after-snippet.
  2. Make the Edit call. Use the Read tool first to confirm the before-snippet still matches the file's current content (defensive — the file shouldn't have changed since draft-mode, but verify).
  3. After the Edit, re-read the file to confirm the after-snippet is in place.
  4. Append to code-edits-applied (via update_section): file path, line span, before/after summary.
  5. Update findings.{finding-id}.status to applied-inline-completed.

If any pre-edit verification fails (the before-snippet no longer matches), do NOT apply the edit. Mark the finding's status as apply-aborted-stale-draft and surface in your return summary. The orchestrator may re-run draft mode for that finding.

Return APPLY_DONE with counts, or CONTINUING if you hit your turn limit. Already-applied edits persist across re-dispatches.
```

Handle returns:

- **`APPLY_DONE`** — record `orchestrator.executions.apply: done` with counts. Continue to Phase 7.
- **`CONTINUING`** — re-dispatch with the not-yet-applied IDs.
- Edit failures recorded in `code-edits-applied` (with `status: failed-{reason}`) — surface in Phase 7 summary; do not abort the whole audit.

### 7. Consolidate

Update the manifest:

- `meta.status` to `completed` (or `partial` if any apply-mode failures or the user halted).
- `meta.completed-at` with timestamp.
- Remove the `pev-audit-active` tag via `axiom_graph_update_doc_meta` on the manifest doc.

Run a final `axiom_graph_check` to confirm post-state. Re-run `axiom_graph_workflow_list` to confirm B-class findings cleared as expected for applied inline edits.

Present a one-screen summary to the user:

```
PEV Audit (annotations) — {audit-manifest-doc-id}

Findings discovered: {N}
  Category 1 (rule violations): {n1}
  Category 2 (prose drift):     {n2}
  Category 3 (coverage gaps):   {n3}

Dispositions:
  Applied inline:      {x} (see code-edits-applied ledger)
  Spawn-requests:      {y}
  Needs-input pending: {z}
  False-positive:      {w}

Spawn-requests created:
  - {slug-1} — {summary}
  - {slug-2} — {summary}

Status: completed | partial
Manifest: {audit-manifest-doc-id}
```

If spawn-requests were created, end with: "Run `/pev-cycle <doc-id>` or `/pev-instance <doc-id>` to address each spawned request."

## Friction log

Capture friction as you work — discovery missed a category, the fixer's classifications skewed too aggressive/too conservative, the gate UX was unclear, the apply-mode pre-edit verification fired too often (suggests draft drafts go stale fast), etc. Append to `{audit-manifest-doc-id}::friction` as you notice it. Read the existing section first so you don't overwrite prior entries, then `axiom_graph_update_section` with existing + new.

Entry format:

```
- **{short tag}** — {one line: what felt off}
  Context: {raw paste — tool call, output, error, user exchange}
  Wish: {optional — what would've made this easier}
```

Empty is fine. Honest emptiness beats invented friction.

## Error Handling

- **Tag-mutex contention** — handled inline at intake. If `pev-audit-active` is present, force the user choice (resume / release / cancel).
- **Discovery returns zero findings** — write empty inventory, present "no annotation drift" to user, mark manifest `completed`, exit gracefully.
- **Fixer mid-batch error (turn-limit, tool failure)** — re-dispatch the same batch; per-finding state in `findings.{finding-id}` persists.
- **Apply-mode pre-edit verification fails** — finding status becomes `apply-aborted-stale-draft`; surface in summary, do not auto-retry. User can re-invoke audit with the stale-draft findings as the new scope.
- **Spawn-request slug collision** — append numeric suffix (`-2`, `-3`, ...) and proceed; record the resolved slug in the manifest.
- **User rejects entire batch at gate** — set `meta.status` to `partial`, record `orchestrator.gate.user-decisions: rejected-all`, leave the `pev-audit-active` tag in place so resume is possible if user reconsiders. Present manifest doc-id and exit.
- **A subagent dispatch fails entirely** (agent file missing, etc.) — verify `${CLAUDE_PLUGIN_ROOT}/agents/pev-audit-annotations-fixer.md` exists; suggest `/agents` to reload. Cannot work around this in-band; surface to user.
