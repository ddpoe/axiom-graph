---
name: pev-audit-dev-docs
description: PEV Audit — backlog cleanup of graph-staleness drift in dev docs. Sequential feature-shard dispatch with whole-slice plans and pass-staged execute (ghost → backlog). Use when accumulated drift in `docs/features/**`, `docs/adrs/**`, `docs/plans/**`, etc. has grown past what an in-cycle Auditor can address.
user-invocable: true
---

# PEV Audit — Dev Docs

You orchestrate a backlog audit of dev-doc graph-staleness drift by dispatching `pev-audit-dev-shard` subagents sequentially across feature subtrees. Each shard produces a whole-slice plan covering two passes (ghost → backlog); execute is dispatched once per pass per shard. All mutation is gated by user approval at the plan-review step.

**LINKED_STALE is sticky.** It is set when an upstream node has a CONTENT-class history event and only clears via explicit verification — `mark_clean` on the consumer, an `update_section` / `patch_section` on the consumer that names its offenders in `addresses=` (the consumer clears once every offender is named), or `reverify` on the root offender (which cascade-clears the LINKED_STALE rooted there through the same verification machinery, with `[reverify:<source>]` provenance). Editing the consumer without `addresses=` verifies only its text, never its LINKED_STALE; Pass-1 actions don't auto-cascade-clear consumer LS. Every LS / CU / DU node in scope is walked individually in Pass 2 — a planned `reverify` just lets one walked cluster resolve with one call.

`${CLAUDE_PROJECT_DIR}` is the consumer project root. `${CLAUDE_PLUGIN_ROOT}` is the PEV plugin's install directory.

**Reference docs (when the project has them):** the audit-skills design spec and sub-feature PRD, `{project_id}::docs/features/pev-agent-nexus/sub_features/audit-skills/design` and `.../prd`, exist only in the project that develops PEV. Read them before first use when `axiom_graph_read_doc` finds them; otherwise this skill is the whole reference.

**v1 posture (key constraints):**

- **Sequential dispatch.** One shard at a time, orchestrator waits for return before dispatching next (D-20). Within a pass, shards run sequentially; the orchestrator only advances to the next pass after all shards finish the current one.
- **No worktree.** Audit work mutates the graph DB as a side effect of the work itself; the auto-reindex divergence between worktree and main repo has no clean merge story for a binary file. Run in the main working tree. There is no `--worktree` override (`worktree-posture` section).
- **Zero audit-specific hooks (D-19).** Tool allowlists live in agent frontmatter. The tag-mutex check, resume detection, and spawn-request writes live inline in this skill body — not as separate helpers, not as hooks. User gates carry the real safety load.
- **Code edits are out of scope.** This skill mutates only DocJSON and the graph. If ghost-resolution reveals missing code wiring, the orchestrator authors a `docs/pev-requests/{slug}.docjson` doc and surfaces it; it does not edit source.
- **Manifest section depth caps at 2.** `axiom_graph_add_section` rejects depth-3 paths. Use `parent.child` notation only; no `parent.child.grandchild`. Per-shard slots use a flat `shard-{shard-id}` parent with `plan-{pass}` / `execute-{pass}` children.

## Phases

### 1. Intake

**Project facts: call `axiom_graph_info(project_root)` first, before any other axiom-graph call.** This session already has `axiom_graph_guide`'s text as the server's instructions; `info` adds the project's facts. Take the project id (`{project_id}` in every id below), the docs roots (`docs_dirs`) and the doc file extensions (`docs_extensions`) from its answer, and every other doc id and path from tool results; never hard-code a project id, a docs folder or a doc extension.

Parse the user's `/pev-audit-dev-docs` request. The request may include:

- A path to an audit-request doc in `docs/pev/audit-requests/{slug}.docjson` (optional) — narrows scope, sets goal, supplies user-authored constraints.
- Free-text scope hints (e.g., "focus on the indexer feature").
- No argument — defaults to a full-tree audit.

Each audit run is a directory, `docs/pev/audits/{audit-id}/`, with the audit id `pev-audit-dev-docs-YYYY-MM-DD-{slug}`. Its manifest is the `audit` doc in it, `{project_id}::docs/pev/audits/{audit-id}/audit`. Once the manifest is cloned, take the exact id from the `axiom_graph_clone_doc` result rather than building it by hand.

**Id collision check.** Refuse an audit id, and pick another slug, when the file `docs/pev/audits/{audit-id}<ext>`, for any `<ext>` in `info`'s `docs_extensions`, or the directory `docs/pev/audits/{audit-id}/` already exists.

**Old runs.** Audits made before plugin 3.0 are single docs, `{project_id}::docs/pev/audits/{audit-id}`. Every lookup below (the tag-mutex check, resume) accepts both shapes, and skips any `efficiency` doc.

**Slug generation.** If the user supplied an audit-request, reuse its slug. Otherwise generate a short descriptive slug from the scope hints (e.g., `full-tree`, `indexer-cleanup`, `post-rename-sweep`). Date-prefix `YYYY-MM-DD` is added by manifest naming; the slug itself is just the descriptive tail.

**Tag-mutex check (inline, no hook).** Run:

```
axiom_graph_list_tags(project_root="${CLAUDE_PROJECT_DIR}")
```

…and check whether any existing manifest carries the `pev-audit-active` tag.

- If `pev-audit-active` is present → an interrupted audit run exists. Read that manifest's `meta` and `orchestrator.executions` sections to determine resume point. Present to user:

  ```
  An interrupted dev-docs audit exists: {existing_manifest_id}
    Status: {meta.status}
    Last completed: {orchestrator.executions.current-pass or "discovery"}
  Options:
    (1) RESUME — pick up where it left off
    (2) RELEASE — remove pev-audit-active tag, mark prior run partial, start a new run
    (3) CANCEL — leave it alone and abort this invocation
  ```

  **HUMAN GATE.**

- If no existing audit → present the cycle plan to the user:

  ```
  PEV Audit (dev-docs): pev-audit-dev-docs-{date}-{slug}
  Manifest: docs/pev/audits/pev-audit-dev-docs-{date}-{slug}/audit
  Audit-request: {linked-request-doc-id-or-none}
  Proceed? (or suggest a different slug)
  ```

  **HUMAN GATE.**

Once confirmed:

1. **Clone the manifest** from the seeded template `{project_id}::.pev/templates/audit-dev-docs` (the project id is `info`'s project id). The template already holds every section this skill writes: `meta`, `request`, `orchestrator.discovery`, `orchestrator.partition`, `orchestrator.plans-review`, `orchestrator.executions`, `orchestrator.handoff` (recommended requests live as a bullet list inside its content) and `friction`. The template carries no tags; the clone call passes them:

   ```
   axiom_graph_clone_doc(
     project_root="${CLAUDE_PROJECT_DIR}",
     source_doc_id="{project_id}::.pev/templates/audit-dev-docs",
     new_id="pev/audits/pev-audit-dev-docs-{date}-{slug}/audit",
     title="PEV Audit (dev-docs): {date} {slug}",
     tags=["pev-audit-active", "pev-audit-dev-docs"],
     set_sections={"meta": "status: active\naudit-type: dev-docs\nstarted-at: {ISO timestamp}\ndispatcher: pev-audit-dev-docs\nrequest-link: {audit-request-doc-id or null}",
                   "request": "{pointer to or copy of the audit-request, or the user's scope hints}"}
   )
   ```

2. **Missing template: halt.** If `info`'s `docs_dirs` lacks `.pev`, or `{project_id}::.pev/templates/audit-dev-docs` is not in the index, stop before creating anything and tell the user:

   ```
   The PEV audit templates are not seeded in this project (missing: audit-dev-docs).
   Run: bash "${CLAUDE_PLUGIN_ROOT}/scripts/pev-seed.sh" --project-root {project root} --axiom-graph "{commands.axiom_graph}"
   If it prints an axiom-graph.toml edit (.pev missing from docs_dirs), make it and re-run the script.
   It builds the index when it is done. Then commit axiom-graph.toml and .pev/, and re-run /pev-audit-dev-docs.
   ```

   `{commands.axiom_graph}` is the `axiom_graph` key of the `[commands]` table in `.pev/sops.toml`; when the file or key is missing, use the command that runs axiom-graph in this project (`axiom-graph` when it is on PATH).

   There is no fallback: never hand-write the manifest with `axiom_graph_write_doc`, and never copy the template file on disk.

### 2. Discovery

Build the drift inventory. Use `axiom_graph_drift_query` for the per-node
inventory (paginated, filterable) and `axiom_graph_check` for the
one-line headline counts:

```
axiom_graph_check(project_root="${CLAUDE_PROJECT_DIR}")
axiom_graph_drift_query(project_root="${CLAUDE_PROJECT_DIR}", filter="all", group_by="location_prefix", format="counts")
axiom_graph_drift_query(project_root="${CLAUDE_PROJECT_DIR}", filter="all", group_by="feature", format="ids")
axiom_graph_list_undocumented(project_root="${CLAUDE_PROJECT_DIR}")
```

The grouped `format="ids"` output gives you per-feature ID buckets you
can hand directly to a shard (the shard's allowlist now includes
`drift_query` so it can also self-enumerate its own slice if you give
it a `location_glob`: `*` stays within one directory and `**` crosses directories, e.g. `docs/features/foo/**`).

For each `NOT_FOUND` node, walk its inbound `documents` edges via `axiom_graph_graph(direction="in")` to find which dev-doc sections reference it. For undocumented nodes flagged by `list_undocumented`, decide whether they fall in scope (audit handles graph drift; pure coverage gaps with no doc reference are typically out-of-scope unless the audit-request specifies otherwise).

Filter to in-scope stale nodes per any `scope` constraints in the audit-request (e.g., feature inclusion list, severity floor). All `BROKEN_LINK` nodes are in scope (backlog audit isn't tied to a single change-set).

Write the drift inventory to `orchestrator.discovery`:

- Counts by staleness type (`NOT_FOUND`, `LINKED_STALE`, `BROKEN_LINK`, `CONTENT_UPDATED`, `DESC_UPDATED`)
- In-scope node list (compressed — IDs only, with one-line summaries for the worst-offender groups)
- Out-of-scope nodes (with reason — e.g., `scope-excluded`, `out-of-scope-coverage-gap`)

### 3. Partition

For each in-scope stale node, assign exactly one shard. Signals in priority order:

1. **Doc-tree signal (preferred).** Walk inbound `documents` edges via `axiom_graph_graph(direction="in")`. If any incoming section ID matches `docs/features/{X}/*`, assign to feature `X`. Tie-breaker: deepest feature path (most specific).
2. **File-path signal (fallback).** Map the source-location path to a feature via project-specific heuristic. The cortex-style mapping (in case the audit-request doesn't supply project-specific overrides):
   - `axiom_graph/index/**` → `indexer`
   - `axiom_graph/viz/**` → `viz`
   - `axiom_graph/mcp/**` → `mcp-server`
   - `axiom_graph/scanners/**` → `indexer` (scanning sub-feature)
   - `axiom_graph/docjson/**` → `docjson-extraction`
   - `axiom_graph/cli/**` → `indexer` (cli sub-feature)
   - other `axiom_graph/**` → `general-shard` candidate (re-evaluate via signal 3)
3. **Judgment (last resort).** When neither signal resolves: use recent commit context (which feature was the change in?), cross-cutting role (`models.py`, `_step_helpers.py` → `general-shard`), or user-authored hints from the audit-request.
4. **General-shard fallback.** Anything resisting 1–3 → `general-shard`. ADRs, plans, devlog, axiom-vision live there permanently (no feature alignment).

Record each assignment + rationale in `orchestrator.partition`. The mapping is `shard-id → [node-id, ...]` with a per-shard rationale block. Large feature subtrees may be split across multiple shards (e.g., `indexer-a`, `indexer-b`) if a single shard's slice would exceed reasonable per-dispatch work — record the split rationale.

If partition produces zero shards (no in-scope drift), present the discovery summary to the user and offer to close the manifest as `completed` with status note. **HUMAN GATE.**

### 4. Plan dispatch (sequential)

For each shard in the partition (in any deterministic order — alphabetical by shard-id is fine), dispatch the `pev-audit-dev-shard` agent in plan mode. **One at a time.** Wait for return before dispatching the next.

**Before each dispatch**, the orchestrator pre-creates the agent's writable manifest sections so the agent only needs `update_section` (not `add_section` — see `tool-permissions` in design). For shard `{shard-id}` in plan mode, create a flat `shard-{shard-id}` parent with per-pass children via `axiom_graph_add_section`:

- `shard-{shard-id}` (parent, depth 1)
- `shard-{shard-id}.plan-ghost` (depth 2)
- `shard-{shard-id}.plan-backlog` (depth 2)

Use `subagent_type="pev-audit-dev-shard"`. Do NOT use `isolation: "worktree"`.

Dispatch prompt template:

```
You are the pev-audit-dev-shard agent in PLAN mode for shard {shard-id} of audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Your shard ID: {shard-id}
Your slice: {node-id list — read in detail from orchestrator.partition.{shard-id}}
Slice glob: {location_glob covering the slice's files, e.g. docs/features/foo/** (* stays in one directory, ** crosses them)}
Mode: plan

Read your slice from the manifest: axiom_graph_read_doc(project_root, "{audit-manifest-doc-id}", section="orchestrator.partition") and locate your shard-id block.

Walk every node in your slice. Also run axiom_graph_accept_doc_edits(dry_run=True) once. Any flagged section (a raw DocJSON edit) that is in your slice gets an accept_edits entry in the backlog pass: accept it if the hand edit is right, or plan an update_section to re-apply it. Accepting or re-applying verifies only the text: if the section is also LINKED_STALE, plan its reconciliation too (an addresses list on the update_section, or mark_clean). Produce a whole-slice plan covering two passes:

  Pass 1 (ghost): every NOT_FOUND / BROKEN_LINK node — classify and propose action (purge / repoint / delete_link / friction-flag). Never purge a module whose file still exists: its NOT_FOUND is inherited from removed children, so repoint its doc links and purge only the removed children.
  Pass 2 (backlog): every LINKED_STALE / CONTENT_UPDATED / DESC_UPDATED node — classify as refactor-noise (mark_clean; or reverify on the root offender when a cluster of LINKED_STALE entries trace to one source whose change is inconsequential to all of them — list the entries the reverify will absorb so the gate reviews the blanket, and if even one entry in the cluster is semantic-shift, plan its update_section with addresses (or update_section + mark_clean) first and reverify only the remainder) or semantic-shift (update_section; a LINKED_STALE entry carries its addresses list, the offenders the edit reconciles, because an edit alone never clears LINKED_STALE). LINKED_STALE is sticky; do NOT assume Pass 1 actions will auto-clear any Pass-2 entries. Every LS / CU / DU node in your slice must be walked individually — a planned reverify is one action absorbing a walked cluster, not a skipped walk.

Write each pass's plan to shard-{shard-id}.plan-{pass-id} (pass-id ∈ ghost, backlog) via axiom_graph_update_section. The orchestrator has pre-created these placeholder sections; you only need update_section.

Each plan entry should include: node-id, current staleness, proposed action (mark_clean / reverify / accept_edits / update_section / delete_link / update_doc_meta / friction-flag), and a one-line rationale. For update_section actions, include the current-vs-proposed diff inline so the user can review at the gate, and the addresses list: the offender ids (the via= field of drift_query format="full") the edit reconciles, empty when the entry is not LINKED_STALE. The section clears when every offender is named; naming a node that is not a current offender is an error and nothing is written. For reverify actions, the entry's node-id is the root offender — list every LINKED_STALE entry the cascade will absorb so the user approves the blanket knowingly.

If you encounter a node where ghost-resolution reveals missing code wiring (the doc references a function that should still exist but is genuinely missing), do NOT plan a code edit. Add a friction-flag entry with a suggested spawn-request payload: { "slug": "...", "summary": "...", "scope": "..." }.

Return PLAN_DONE with a one-paragraph summary, OR NEEDS_INPUT with proxy questions if you encounter ambiguity that needs user guidance.
```

Handle returns:

- **PLAN_DONE** — record completion in `orchestrator.executions` (e.g., `plan.{shard-id}: done`). Move to the next shard.
- **NEEDS_INPUT** — collect the agent's questions but do NOT immediately ask the user. Continue dispatching remaining shards. After all shards return, batch all `NEEDS_INPUT` from across shards into a single `AskUserQuestion` call (or several if questions exceed the schema limit). Resume each agent with `SendMessage` containing the answers and the agent's `context` field. (Same proxy-question protocol as the rest of PEV.)
- **CONTINUING** — re-dispatch the same shard with the same prompt; the agent reads its already-written plan blocks from the manifest and resumes.

### 5. Plan review (HUMAN GATE)

After all shards return PLAN_DONE, read every shard's plan blocks together.

**Light cross-shard scan.** Partition guarantees no two shards touched the same node — so this is unusual-pattern detection only, not heavy reconciliation:

- Are 4+ shards proposing 30+ deletes each? Possible commit-base mismatch — flag for the user.
- Are multiple shards flagging correlated own-stale events (same commit hash) on related nodes? Possible coherent feature drift — surface as a candidate for a focused `/pev-cycle` instead of audit cleanup.
- Anything else that looks structurally surprising — flag with a one-line rationale.

Record findings (or "no unusual patterns") in `orchestrator.plans-review.cross-shard-flags`.

**Present plans to the user.** For each shard, render a compact summary:

```
Shard: {shard-id}  ({N} nodes total)
  Pass 1 ghost: {ghost-count} actions ({purge: P, delete_link: W, repoint: R, friction: F})
  Pass 2 backlog: {backlog-count} actions ({mark_clean: M, reverify: R (absorbing A LS entries), update_section: U, accept_edits: E, update_doc_meta: D, friction: F2})
```

For non-trivial `update_section` actions, render the proposed diff inline. For `friction`-flagged spawn-request candidates, render the suggested slug + summary.

**HUMAN GATE** — "Approve this plan to proceed to pass-staged execute, or provide feedback to revise (e.g., 'skip shard X', 'don't update_section for node Y, just mark_clean')?"

Record the user's decision in `orchestrator.plans-review.user-decision` (one of: `approved`, `approved-partial` with notes, `rejected` with notes). For `approved-partial` and `rejected`, redispatch only the affected shards in plan mode with the user's feedback appended.

### 6. Execute pass-staged

For each pass in order — `ghost`, `backlog` — dispatch every shard sequentially in execute mode for that pass. The orchestrator only advances to the next pass after all shards finish the current pass.

**Between passes, re-run discovery.** After Pass 1 completes:

```
axiom_graph_check(project_root="${CLAUDE_PROJECT_DIR}")
axiom_graph_drift_query(project_root="${CLAUDE_PROJECT_DIR}", filter="links", format="counts", group_by="status")
```

This confirms Pass 1's actions landed (NOT_FOUND / BROKEN_LINK counts should drop). It does NOT trim Pass 2: LINKED_STALE is sticky and only clears via explicit verification (per-node `mark_clean`, an approved edit naming its offenders in `addresses=`, or an approved `reverify` on a root offender), so every Pass-2 entry in the original plan remains actionable. The re-run is a sanity check, not a trimming step.

**Before each per-pass dispatch**, pre-create the agent's execute output section for that pass via `axiom_graph_add_section`:

- `shard-{shard-id}.execute-{pass}` (one section per shard per pass; the `shard-{shard-id}` parent already exists from plan-mode dispatch)

This keeps the agent's allowlist limited to `update_section` (matching the design's `tool-permissions` table for dev-shard).

**Per-pass dispatch prompt template:**

```
You are the pev-audit-dev-shard agent in EXECUTE mode for shard {shard-id} of audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Your shard ID: {shard-id}
Slice glob: {the same location_glob given in the plan dispatch}
Mode: execute
Pass: {ghost | backlog}

Read your assigned pass's plan from shard-{shard-id}.plan-{pass} via axiom_graph_read_doc.

LINKED_STALE is sticky — Pass 1 actions do NOT auto-clear any Pass 2 entries. If you want to confirm an entry is still actionable, call axiom_graph_drift_query(filter="all", location_glob=<your Slice glob>, format="full") to enumerate your slice; skip only entries whose own_status changed (e.g., a node now NOT_FOUND that was CONTENT_UPDATED), not entries you assume "must have cleared by now."

Perform the planned actions one at a time:
  - mark_clean    → axiom_graph_mark_clean (per-node verification; `verified_by="agent:pev-audit-dev-docs"`, or `agent:pev-audit-dev-shard:{shard}` from a shard (the label the shard agent uses), never without the `agent:` prefix, which the server would log as a human's verification. A reviewed slice is cleared as one `node_ids=[...]` list of the ids drift_query returned for it, never by filter)
  - reverify      → axiom_graph_reverify (root-offender verification, with the same `verified_by` label as mark_clean: `"agent:pev-audit-dev-docs"`, or `agent:pev-audit-dev-shard:{shard}` from a shard; cascade-clears the LINKED_STALE rooted at the source per your approved plan entry; it skips and reports entries still outstanding via other offenders — those need their own planned action, but reverifies compose, so a plan that reverifies every offender behind an entry clears it on the last one; several planned reverifies can go in one axiom_graph_reverify(node_ids=[...]) call)
  - accept_edits  → axiom_graph_accept_doc_edits (sections flagged as raw DocJSON edits; dry_run=True first — mark_clean leaves them flagged; accepting verifies the text only and does not clear LINKED_STALE)
  - update_section → axiom_graph_update_section (use the proposed-content and the addresses from your plan entry; if the reply ends with "still LINKED_STALE via: …", record those ids in the action's result rather than clearing them unplanned)
  - update_doc_meta → axiom_graph_update_doc_meta
  - delete_link   → axiom_graph_delete_link
  - friction-flag → do not act; record verbatim as a "friction-flags" subsection within your execute records

Write per-action records to shard-{shard-id}.execute-{pass} with: node-id, action taken, result (success / skipped-already-clean / failed-{reason}), and timestamp. The orchestrator has pre-created this placeholder section; you only need update_section.

Stay within your shard. Do NOT edit other shards' manifest sections. Do NOT call any code-editing tool (you don't have one anyway).

Return EXECUTE_DONE with a one-paragraph summary, or CONTINUING if you hit your turn limit. Already-marked-clean nodes don't reappear stale, so progress is automatic across re-dispatches.
```

Handle returns per shard per pass:

- **EXECUTE_DONE** — append `{pass}.{shard-id}: done` as a bullet within `orchestrator.executions` content. Continue.
- **CONTINUING** — re-dispatch the same shard, same pass.
- **NEEDS_INPUT** — batch with any other shards' `NEEDS_INPUT` from the same pass. Ask via `AskUserQuestion`. Resume with `SendMessage`.

**Update the current-pass marker for resume.** After all shards finish a pass, append a `current-pass: {next-pass-or-completed}` line within `orchestrator.executions` content so an interrupted run can pick up at pass boundary on resume.

### 7. Spawn requests + Consolidate

Walk every shard's `friction-flags` from execute (and any flagged from plan that the user approved). For each entry, write a real `docs/pev-requests/{slug}.docjson` doc:

1. Generate a descriptive slug from the entry's suggested slug (collision-check against existing `docs/pev-requests/` docs (any extension in `info`'s `docs_extensions`) — append `-2`, `-3`, etc. if needed).
2. Clone it from the seeded template `{project_id}::.pev/templates/request` (a request stays a single doc), filling its sections from the friction-flag's payload. If the template is missing, halt with the seed-step message from Intake, naming `request`.

   ```
   axiom_graph_clone_doc(
     project_root="${CLAUDE_PROJECT_DIR}",
     source_doc_id="{project_id}::.pev/templates/request",
     new_id="pev-requests/{slug}",
     title="{short human-readable title}",
     tags=["pev-request", "not-started", "audit-spawned"],
     set_sections={"meta": "source-audit: {the audit manifest doc ID}", "problem": "...",
                   "proposed-approach": "...", "scope": "...", "notes": "..."}
   )
   ```
4. Append the spawned request's doc-ID as a bullet under a "Recommended pev-requests" sub-heading within `orchestrator.handoff` content in the audit manifest.

After all spawn-requests are written, read every shard's execute records and append a per-shard summary block within `orchestrator.executions` content (under a "Summary" sub-heading):

- Total actions performed per pass per shard
- Total `mark_clean` count, `update_section` count, etc. across all shards
- Friction-flags raised, with spawn-request links
- Any actions that returned `failed-{reason}`

Update `meta.status` to `completed` (or `partial` if any shards returned `failed` actions or the user halted mid-execute). Update `meta.completed-at` with timestamp. Remove the `pev-audit-active` tag via `axiom_graph_update_doc_meta` on the manifest doc.

**Efficiency report.** Run the analysis for this audit:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_pev_session.py" --find-run {audit-id} --docjson --summary
```

`--find-run` reports only the sessions that worked this audit (dispatched its agents or wrote its docs), and only their calls for it. The script stages the report in the system temp folder; it never writes into `docs/` itself. For each `Staged DocJSON:` line it prints, write the report with the call it prints, `axiom_graph_write_doc(project_root="${CLAUDE_PROJECT_DIR}", doc_file="<staged path>")`. The report lands in the audit's directory as `docs/pev/audits/{audit-id}/efficiency` (`efficiency-s2`, `-s3`… when the audit spanned several sessions), indexed and verified. Its only tag is `pev-efficiency`, so the audit-type tag searches never find it. Present its summary with yours. The script reads Claude Code's private session-log format; if it warns that a session came from a newer Claude Code version than it was checked against, mention it: empty per-agent tool lists then mean the log format changed, not that the agents did nothing.

Run a final `axiom_graph_check` to confirm post-state and present a one-screen summary to the user:

```
PEV Audit (dev-docs) — {audit-manifest-doc-id}

Pre-audit drift:  NOT_FOUND={X}, LINKED_STALE={Y}, BROKEN_LINK={Z}, ...
Post-audit drift: NOT_FOUND={X'}, LINKED_STALE={Y'}, ...
Resolved: {count} stale events
Spawn-requests created: {N}
  - {slug-1} — {summary}
  - {slug-2} — {summary}
Friction notes: {count} (see manifest's friction section)

Status: completed | partial
Manifest: {audit-manifest-doc-id}
Efficiency: {efficiency doc id(s)}
```

If spawn-requests were created, end with: "Run `/pev-cycle <doc-id>` or `/pev-instance <doc-id>` to address each spawned request."

## Friction log

Capture friction as you work — partition decisions that felt forced, plans that didn't survive review, dispatch-prompt fields the agents misinterpreted, gate prompts that confused the user, hooks/tools that didn't behave as expected, etc. Add each entry under `{audit-manifest-doc-id}::friction` as you notice it — not as a Phase 7 summary — as its own subsection: `axiom_graph_add_section(doc_id="{audit-manifest-doc-id}", parent_id="friction", section_id="<short-tag-slug>", heading="<short tag>", content=...)`. Entry ids are slugs with no dots. Omit `content` rather than passing `""`. After two identical failures, change approach instead of retrying. Never rewrite the section with `update_section` or `patch_section`: a new subsection can't clobber entries other agents wrote meanwhile.

**Log every script read.** If you read a document or index data with a script (Python, Node, `jq`, `grep` … over DocJSON files, or raw SQL) instead of an axiom-graph tool, add a friction entry tagged `script-read`. Name the tool you would have used and why it fell short: output too large, no way to select part of a section, search missed it, not in the index, output hard to reuse. Reading this way is allowed. Writing a document this way is not. These entries are how gaps in the tools get found and fixed.

Entry format (the subsection's heading is the short tag; its content is):

```
{one line: what felt off}
Context: {raw paste — tool call, output, error, user exchange}
Wish: {optional — what would've made this easier}
```

Empty is fine. Honest emptiness beats invented friction.

## Error Handling

- **Tag-mutex contention** — handled inline at intake. If `pev-audit-active` is present, force the user choice (resume / release / cancel). Do NOT proceed silently.
- **Discovery returns zero stale nodes** — write `orchestrator.discovery` with the empty inventory, present "no drift to audit" to the user, mark manifest `completed` with note, exit gracefully.
- **A shard returns an error mid-execute** — record the failed action(s), continue with remaining actions in that shard's pass, and surface failures in the post-audit summary. Do NOT abort the whole audit unless the failure pattern indicates systemic breakage.
- **`axiom_graph_check` hangs mid-audit** — timeout and retry; if persistent, proceed to the next phase using the last successful inventory and note the gap in `friction`.
- **User aborts at plan-review or mid-execute** — set `meta.status` to `partial`, leave `pev-audit-active` tag in place so a future re-invocation can resume, and present the manifest doc-id to the user.
- **Spawn-request slug collision** — append numeric suffix (`-2`, `-3`, ...) and proceed; record the resolved slug in the manifest.
- **A subagent dispatch fails entirely** (agent file missing, etc.) — verify `${CLAUDE_PLUGIN_ROOT}/agents/pev-audit-dev-shard.md` exists; suggest `/agents` to reload. Cannot work around this in-band; surface to user.
