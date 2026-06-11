---
name: pev-audit-consumer-docs
description: PEV Audit — pre-release sweep of user-facing consumer prose. Three-phase orchestration (Discovery → Verification → Consolidate+Propose) with sequential subagent dispatch. Discovery walks dev sources (cycles / features / ADRs) for additions; verification stress-tests each consumer doc's claims via three layered staleness signals; Phase 3 user-gates every proposed change before any patch lands. Use before a release to audit user-facing prose against current dev reality.
user-invocable: true
---

# PEV Audit — Consumer Docs

You orchestrate a backlog audit of consumer-facing documentation. The skill runs three phases, sequentially:

1. **Discovery** — three `pev-audit-consumer-discovery` agents (one per `mode ∈ {cycles, features, adrs}`) walk the dev side looking for additions consumer docs are missing.
2. **Verification** — `pev-audit-consumer-verifier` agents (typically one per consumer doc, batched for very small docs) stress-test each consumer doc's claims against three layered staleness signals.
3. **Consolidate + Propose** — you merge all Phase 1 additions and Phase 2 corrections, present the full proposal set to the user via `AskUserQuestion`, and apply only what the user approves. **All changes are user-gated; nothing applies inline (D-18).**

`${CLAUDE_PROJECT_DIR}` is the consumer project root. `${CLAUDE_PLUGIN_ROOT}` is the PEV plugin's install directory.

**Reference docs (read before first use):**

- Design spec: `axiom_graph::docs.features.pev-agent-nexus.sub_features.audit-skills.design`
- Sub-feature PRD: `axiom_graph::docs.features.pev-agent-nexus.sub_features.audit-skills.prd`

**v1 posture (key constraints):**

- **Sequential dispatch (D-20).** All discovery and verification dispatches run one-at-a-time. 3 discovery + N verification = `3 + N` sequential dispatches. First-ever run is heaviest (no since-window narrows Phase 2).
- **Trust assumption: dev docs are correct (D-6).** Consumer-docs uses dev-doc capability tables, design specs, ADR records, and source-code summaries as ground truth. Pre-flight check warns and offers to bail if `axiom_graph_check` shows incomplete state in `docs.features.*`.
- **Orchestrator has full `Bash` (D-15 revised).** Same trust posture as the pev-cycle skill body. Used narrowly for `git diff` / `git log` / `git show` at run-start. Subagents have no Bash. Earlier design proposed a `git-command-allowlist` PreToolUse hook; that hook is dropped (D-19). Use Bash narrowly; the user can interrupt if it goes off-piste.
- **No worktree.** Audit work mutates the graph DB and DocJSON in the main working tree; the auto-reindex divergence between worktree and main has no clean merge story. There is no `--worktree` override (`worktree-posture` section).
- **Zero audit-specific hooks (D-19).** Tool allowlists live in agent frontmatter. The tag-mutex check, since-window resolution, dev-docs cleanliness pre-flight, and resume detection live INLINE in this skill body — not as separate helpers, not as hooks.
- **No inline-apply (D-18).** Every Phase 1 addition and Phase 2 correction goes through Phase 3 user approval before any `axiom_graph_update_section` or `axiom_graph_write_doc` call. Consumer prose is high-stakes; the rare cadence (pre-release) makes the gate friction negligible.

## Phases

### 1. Intake

Parse the user's `/pev-audit-consumer-docs` request. The request may include:

- A path to an audit-request doc in `docs/pev/audit-requests/{slug}.json` (optional) — narrows scope, sets goal, supplies user-authored constraints (e.g., a specific consumer doc to focus on, a `constraints.since` override).
- Free-text scope hints (e.g., "skip ADR mode this run", "focus on getting-started.md").
- No argument — defaults to a full sweep (all three discovery modes, all consumer docs).

Read `axiom-graph.toml` in the project root to get the `project_id` value. The audit manifest doc ID is `{project_id}::docs.pev.audits.pev-audit-consumer-docs-YYYY-MM-DD-{slug}` — do NOT hardcode the prefix; it varies per project.

**Slug generation.** If the user supplied an audit-request, reuse its slug. Otherwise generate a short descriptive slug from the scope hints (e.g., `pre-release-sweep`, `getting-started-only`, `full-sweep`). Date-prefix `YYYY-MM-DD` is added by manifest naming; the slug itself is just the descriptive tail.

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
    Last activity: {meta.current-phase or "discovery"}
  Options:
    (1) RESUME — pick up where it left off (only if same type: pev-audit-consumer-docs)
    (2) RELEASE — remove pev-audit-active tag, mark prior run partial, start a new run
    (3) CANCEL — leave it alone and abort this invocation
  ```

  **HUMAN GATE.** If the interrupted run is a different audit type (`pev-audit-dev-docs`, `pev-audit-annotations`), only options 2 and 3 are valid — resume across types is not supported.

- If no existing audit → present the cycle plan to the user:

  ```
  PEV Audit (consumer-docs): pev-audit-consumer-docs-{date}-{slug}
  Manifest doc ID: {project_id}::docs.pev.audits.pev-audit-consumer-docs-{date}-{slug}
  Audit-request: {linked-request-doc-id-or-none}
  Scope hints: {hints-or-"full-sweep"}
  Proceed? (or suggest a different slug)
  ```

  **HUMAN GATE.**

**Dev-docs cleanliness pre-flight (informational; chaining section).** Run:

```
axiom_graph_drift_query(project_root="${CLAUDE_PROJECT_DIR}", filter="staleness", group_by="status", format="counts")
```

If any `NOT_FOUND` or `LINKED_STALE` exists in the `docs.features.*` subtree, warn the user:

```
Dev docs show {N} stale events in docs.features.* — consumer-docs assumes
dev docs are truth (D-6). Running this audit now risks propagating dev-side
staleness into consumer prose.

Options:
  (1) BAIL — recommend running /pev-audit-dev-docs first
  (2) PROCEED with caveat — log this in the manifest's friction section
  (3) CANCEL
```

This is informational, not enforced. The user can override.

**Since-window resolution (consumer-docs only).** Search prior audit manifests with the `pev-audit-consumer-docs` tag for the most recent's `meta.audit-commit-sha`:

```
axiom_graph_search(project_root, "", scope="docs", tag="pev-audit-consumer-docs", max_results=20)
```

For each result, read `meta.audit-commit-sha`. The most recent (by `meta.completed-at` or `meta.started-at`) is the since-commit reference. **First-ever run → `meta.since-commit: null`** (full-history scan; expect heavier work shape on first run, especially Phase 2).

The user may override via the audit-request's `constraints.since` field. If set, use that instead and record it in `meta.since-commit-overridden: true`.

**Record `meta.audit-commit-sha = HEAD` at run start.** Run:

```
git rev-parse HEAD
```

via Bash. Write the SHA to `meta.audit-commit-sha`. This is what future audits will use as their since-window reference.

**Compute orchestrator-level git diff.** If `meta.since-commit` is non-null, run:

```
git diff <since>..HEAD --name-only
```

via Bash. (If `since-commit` is `null` for first-ever run, write `meta.git-diff-summary: "first-run-full-history"` and skip the diff — no narrowing possible.)

Summarize the diff into `meta.git-diff-summary`:

- File list (paths only)
- Per-path classification: `code` / `dev-doc` / `consumer-doc` / `test` / `other` (best-effort heuristic by directory)
- Aggregate counts by classification

Subagents read `meta.git-diff-summary` in their dispatch payload — they don't have Bash.

Once intake completes:

1. Create the audit manifest via `axiom_graph_write_doc`. The `id` field is path-slug form (`pev/audits/pev-audit-consumer-docs-{date}-{slug}`), NOT the full node-id. Tags: `pev-audit-active`, `pev-audit-consumer-docs`. Initial sections (placeholders pre-created so subagents can use `update_section` instead of `add_section`):
   - `meta`
   - `request` (pointer or copy of audit-request)
   - `orchestrator.discovery` (placeholder for pre-dispatch summary)
   - `discovery.findings.cycles` (empty placeholder for cycles-mode discovery agent)
   - `discovery.findings.features` (empty placeholder for features-mode discovery agent)
   - `discovery.findings.adrs` (empty placeholder for adrs-mode discovery agent)
   - `orchestrator.handoff.recommended-requests` (empty list)
   - `friction` (empty)
2. `meta` content includes: `status: active`, `audit-type: consumer-docs`, `started-at: {ISO timestamp}`, `dispatcher: pev-audit-consumer-docs`, `request-link: {audit-request-doc-id or null}`, `audit-commit-sha: {HEAD-sha}`, `since-commit: {resolved-sha or null}`, `since-commit-overridden: {true if user override else false}`, `git-diff-summary: {summary block from above}`, `current-phase: intake-complete`.

### 2. Phase 1 — Discovery (sequential)

Build the **known consumer-doc list** that the discovery agents will use. Search docs with the `consumer` transitive tag (cortex's `axiom-graph.toml` declares `transitive_tags = ["consumer"]`):

```
axiom_graph_search(project_root, "", scope="docs", tag="consumer", max_results=100)
```

Also include any explicit user-supplied target list from the audit-request's `scope` section. Filter out anything obviously not consumer-facing (admin-only docs, internal runbooks). The resulting list is the "known-consumer-doc list" passed to discovery agents in their dispatch payload.

**Update `meta.current-phase: discovery`.**

Dispatch the `pev-audit-consumer-discovery` agent **once per mode**, in this order: `cycles` → `features` → `adrs`. Sequential — wait for return before dispatching next.

Use `subagent_type="pev-audit-consumer-discovery"`. Do NOT use `isolation: "worktree"`.

**Per-mode dispatch prompt template:**

```
You are the pev-audit-consumer-discovery agent in mode {mode} for audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Mode: {cycles | features | adrs}
Since-commit: {sha or null}    # null = first-ever run, full-history scan
Known consumer-doc list:
  - {consumer-doc-id-1}
  - {consumer-doc-id-2}
  ...

Read your mode's strategy from the agent body. Walk the dev source for your mode, identify candidate additions, and write findings to discovery.findings.{mode} via axiom_graph_update_section. The orchestrator has pre-created the placeholder section.

Stay strictly in mode={mode}. Do NOT walk the other modes' sources; sibling agents handle those.

Return DISCOVERY_DONE with a summary, NEEDS_INPUT batched if you encounter ambiguity, or CONTINUING if you hit the turn limit.
```

Handle returns:

- **DISCOVERY_DONE** — record completion in `orchestrator.discovery.{mode}: done` with the agent's summary appended. Continue to next mode.
- **NEEDS_INPUT** — collect the agent's questions; do NOT immediately ask the user. After the third mode returns, batch all NEEDS_INPUT from the three modes into one `AskUserQuestion` call. Resume each agent with `SendMessage` containing answers and the agent's `context` field.
- **CONTINUING** — re-dispatch the same mode with the same prompt; the agent reads its already-written `discovery.findings.{mode}` and resumes.

After all three modes return DISCOVERY_DONE, **update `meta.current-phase: discovery-complete`** and proceed to Phase 2.

### 3. Phase 2 — Verification (sequential, per consumer doc)

Update `meta.current-phase: verification`.

For each consumer doc in the known consumer-doc list, you'll dispatch one verifier (or batch small docs together — see below).

**Determine verification scope.** A consumer doc only needs verification if it has anchored claims AND there's some signal it might have drifted. Use these short-circuits to skip docs:

- If the consumer doc was last modified after `meta.audit-commit-sha` (it's already up to date with current state), skip with rationale `up-to-date`.
- If the consumer doc has NO outbound graph edges AND no symbols in its prose appear in `meta.git-diff-summary`, lower priority but still verify (its capability cross-reference / Layer 3 may still flag).
- Otherwise, queue for verification.

Record skip decisions in `orchestrator.verification.skipped` with rationale.

**Batch small docs.** For each consumer doc to be verified, get its character length:

```
axiom_graph_read_doc(project_root, {consumer-doc-id})
```

…and measure the rendered output. Default heuristic: `< 500 chars` → batchable. Group batchable docs into batches of up to 5 docs each. Larger docs get their own dispatch. Record batches in `orchestrator.verification.batches`.

**The `< 500 chars` threshold is a starting heuristic** (see design's open-questions section). Tune after the first run; record observed batch shapes in `friction`.

**Pre-create per-doc verification sections.** For every consumer doc to be verified (whether solo or batched), pre-create `verification.findings.{consumer-doc-id-tail}` via `axiom_graph_add_section`. The tail is the last `::`-separated segment of the consumer doc-id, slug-safe-ified. Subagents only need `update_section`.

**Per-verifier dispatch prompt template:**

```
You are the pev-audit-consumer-verifier agent for audit {audit-manifest-doc-id}.

Project root: ${CLAUDE_PROJECT_DIR}
Audit manifest doc ID: {audit-manifest-doc-id}
Target consumer doc(s): {consumer-doc-id} OR [list of consumer-doc-ids for batched dispatch]
Audit-commit-sha: {sha}
Since-commit: {sha or null}    # null = first-ever full-history; downgrade Layer 2 noise
First-run-mode: {true|false}   # affects Layer 2 verbosity

Read meta.git-diff-summary from the audit manifest for Layer 2 cross-reference.

For each target consumer doc:
  1. Read the doc via axiom_graph_read_doc.
  2. Apply the scope guard (D-14): only verify claims with code or dev-doc anchors. Pure prose → out-of-scope, default skip from output.
  3. Run the three signal layers per anchored claim:
     - Layer 1: LINKED_STALE on graph edges from the consumer-doc section.
     - Layer 2: git diff slice — does any file in meta.git-diff-summary touch a symbol the claim mentions? Drill in via axiom_graph_source.
     - Layer 3: capability cross-reference — locate the claim's capability in feature PRDs; compare description / status.
  4. Issue a verdict per anchored claim: still-true (default skip) | now-wrong (with proposed correction) | needs-review (with ambiguity summary) | out-of-scope (default skip).
  5. Write verdicts to verification.findings.{consumer-doc-id-tail} via axiom_graph_update_section. The orchestrator pre-created the placeholder.

Return VERIFICATION_DONE with summary, NEEDS_INPUT batched if you cannot decide between verdicts, or CONTINUING if you hit your turn limit.
```

Handle returns per verifier:

- **VERIFICATION_DONE** — record `orchestrator.verification.{batch-or-doc-id}: done` with summary. Continue to next.
- **NEEDS_INPUT** — collect questions; batch with other in-flight verifiers' NEEDS_INPUT (rare under sequential dispatch, but possible if a verifier returns NEEDS_INPUT and you immediately dispatch the next which also does). Ask via `AskUserQuestion` once you have a reasonable batch (or after every 3-5 verifiers complete, to avoid asking too late).
- **CONTINUING** — re-dispatch the same target/batch; already-written verdicts persist.

After all consumer docs are verified (or skipped), **update `meta.current-phase: verification-complete`** and proceed to Phase 3.

### 4. Phase 3 — Consolidate + Propose (Pattern X gate)

Update `meta.current-phase: consolidate`.

**Step 1 — Merge findings into per-target proposals.** For each consumer doc that's a proposal target, gather:

- All Phase 1 additions targeting it (from `discovery.findings.cycles` + `discovery.findings.features` + `discovery.findings.adrs` where `proposed-target-consumer-doc` matches).
- All Phase 2 corrections for it (from `verification.findings.{consumer-doc-id-tail}` with verdict `now-wrong` or `needs-review`).

Pre-create `proposals.{consumer-doc-id-tail}` via `axiom_graph_add_section`. Write the merged proposal set:

```
- proposal-id: prop-{consumer-doc-id-tail}-{N}
  source: discovery.findings.cycles[discovery-cycles-3] | verification.findings.{tail}[verify-{tail}-7]
  target-section: {section-id-in-target-consumer-doc, or "needs-section-decision"}
  type: addition | correction | review-needed
  proposed-text: "{added prose for additions / replacement prose for corrections / null for review-needed}"
  current-text: "{the existing prose being replaced, only for corrections}"
  rationale: "{one or two sentences linking back to the source-of-truth: cycle-id / PRD capability / diffed file}"
  ambiguity-summary: "{only for review-needed}"
  dev-side-suspect: {true|false; only for review-needed flagged by verifier}
```

For `needs-routing` discovery findings (no target consumer doc proposed), make a routing decision: assign to the most plausible consumer doc based on capability category, OR flag as `unrouted` and surface to the user as part of the gate.

**Step 2 — Cross-target dedup pass.** If multiple proposals across different target docs are about the same underlying change (e.g., a new capability that should be mentioned in both README and getting-started.md), record the relationship in `proposals.{tail}.related-proposals` so the user can approve consistently. Don't try to auto-merge — the user may want different framings per target.

**Step 3 — Spawn-request prep for `dev-side-suspect` cases (chaining section).** Walk all proposals with `dev-side-suspect: true`. For each, draft a spawn-request payload (NOT yet written — staged for the gate):

```
- proposed-spawn-request:
    slug: descriptive-kebab-case
    title: short human-readable
    summary: one paragraph — the verifier flagged this consumer-doc claim, but the dev side appears to be the source of truth's drift; recommend a /pev-cycle to fix dev-side first
    scope: which dev-side files / docs / capabilities
    source-finding-ids: [verify-{tail}-{N}]
```

Record drafts in `proposals.{tail}.draft-spawn-requests`.

**Step 4 — Present the entire proposal set to the user.** Use `AskUserQuestion`. For large proposal sets (more than ~10 proposals), present in chunks of 5-10 per question, or render a compact summary first and follow up with per-item drill-in.

Format per consumer doc:

```
Target consumer doc: {consumer-doc-id}
  Additions (from Phase 1): {count}
    + prop-{tail}-1 (cycle source: {cycle-id}): "{proposed prose, truncated to ~80 chars}"
        Reason: {rationale}
        Target section: {section-id or "needs-section-decision"}
    + prop-{tail}-2 (feature source: {prd-id}::{capability-row}): ...
  Corrections (from Phase 2): {count}
    Δ prop-{tail}-3 (verifier finding: {verify-id}, layers [1,2]):
        Currently: "{current text, truncated}"
        Proposed:  "{proposed text, truncated}"
        Reason: {rationale}
  Review-needed (from Phase 2): {count}
    ? prop-{tail}-4 (verifier finding: {verify-id}, dev-side-suspect: true)
        Quote: "{claim, truncated}"
        Ambiguity: {summary}
        Suggested action: spawn pev-request for dev-side fix instead of patching consumer doc

Across-target relationships:
  prop-{tail-A}-3 and prop-{tail-B}-7 cover the same underlying change.

Drafted spawn requests (for dev-side fixes):
  - {slug-1} — {summary}

For each consumer doc, choose:
  (a) APPROVE ALL — apply every proposal
  (b) APPROVE SOME — specify which to apply
  (c) REJECT ALL — apply nothing for this target
  (d) REJECT WITH FEEDBACK — provide notes for redrafting (optional)

For drafted spawn requests:
  Approve to write or skip?
```

**HUMAN GATE.** Use `AskUserQuestion` with one question per consumer doc, plus one additional question for spawn-request approval. If the proposal set is small (1-2 targets), one combined question is fine. The user can also choose `REJECT WITH FEEDBACK` and supply notes; do NOT redraft on the user's behalf inside the same dispatch — record the feedback and surface in the manifest for a future re-invocation to address.

Record decisions in `proposals.{consumer-doc-id-tail}.user-decision`:

```
status: approved | approved-partial | rejected | rejected-with-feedback
approved-proposal-ids: [...]
skipped-proposal-ids: [...]
rejected-proposal-ids: [...]
feedback: "{free-text user notes, only for rejected-with-feedback}"
```

For approved spawn requests, similarly record in `orchestrator.handoff.recommended-requests-decisions`.

### 5. Phase 4 — Apply approved patches

Update `meta.current-phase: apply`.

**Step 1 — Apply approved consumer-doc patches.** For each approved proposal, call `axiom_graph_update_section` against the target consumer doc's section. For additions where `target-section` is `"needs-section-decision"`, the user must have made a section choice during the gate (handle this within the AskUserQuestion presentation — if you don't have a section choice, treat as a follow-up needs-input rather than guessing).

In rare cases an addition is large enough to warrant a new section in the target consumer doc; in those cases use `axiom_graph_add_section` (which the orchestrator has — see design's `tool-permissions`). Even rarer: a brand-new consumer doc — use `axiom_graph_write_doc` (also in the orchestrator's allowlist). Both rare cases require explicit user confirmation at the gate.

Write a per-patch record to `applied.{consumer-doc-id-tail}`:

```
- patch-id: applied-{tail}-{N}
  source-proposal: prop-{tail}-{N}
  action: update_section | add_section | write_doc
  target: {full section or doc id}
  before: "{previous prose, only for update_section corrections}"
  after: "{new prose}"
  applied-at: {ISO timestamp}
```

**Step 2 — Write approved spawn requests.** For each approved spawn-request draft, write the actual `docs/pev-requests/{slug}.json` doc:

1. Slug collision-check: search `docs/pev-requests/*.json` (via `axiom_graph_search` with `tag="request"` or by reading the directory if simpler). If collision, append numeric suffix.
2. Compose the request DocJSON with sections `meta` (including `meta.source-audit` = the audit manifest doc-id), `problem`, `proposed-approach`, `scope`, `notes`. Tag: `["pev", "request", "audit-spawned"]`.
3. `axiom_graph_write_doc` with `id` field in path-slug form (`pev-requests/{slug}`).
4. Append the spawned request's doc-ID to `orchestrator.handoff.recommended-requests`.

### 6. Phase 5 — Consolidate & close

Update the manifest:

- `meta.status` to `completed` (or `partial` if the user halted mid-apply, or any apply failed).
- `meta.completed-at` with ISO timestamp.
- `meta.current-phase: completed`.
- Remove the `pev-audit-active` tag via `axiom_graph_update_doc_meta` on the manifest doc. (The `pev-audit-consumer-docs` tag stays — that's how future audits resolve their since-window.)

Run a final `axiom_graph_check` to confirm post-state is reasonable (consumer-docs audits don't directly resolve graph staleness — that's dev-docs' job — so post-`check` should be roughly identical to pre, modulo any incidental graph rewiring from `update_section` calls).

Present a one-screen summary to the user:

```
PEV Audit (consumer-docs) — {audit-manifest-doc-id}

Audit window: {since-commit or "first-run-full-history"} → {audit-commit-sha}
Files in window: {count from meta.git-diff-summary}

Phase 1 — Discovery findings:
  Mode cycles:   {count} candidate additions
  Mode features: {count}
  Mode adrs:     {count}

Phase 2 — Verification findings:
  Consumer docs verified: {count} (skipped {skip-count} as up-to-date)
  Verdicts: now-wrong={x} needs-review={y}

Phase 3 — Consolidated proposals: {total-prop-count}
  Approved:   {a}
  Skipped:    {s}
  Rejected:   {r}

Phase 4 — Applied:
  update_section calls: {n}
  add_section calls:    {m}
  write_doc calls:      {k}
  Spawn-requests written: {p}
    - {slug-1} — {summary}
    - {slug-2} — {summary}

Status: completed | partial
Manifest: {audit-manifest-doc-id}
```

If spawn-requests were created, end with: "Run `/pev-cycle <doc-id>` or `/pev-instance <doc-id>` to address each spawned request."

## Friction log

Capture friction as you work — discovery missed an obvious source, verification's three layers fired in confusing combinations, the batching threshold produced bad batches, the dev-docs cleanliness pre-flight bit when it shouldn't have, the user gate's chunking confused review, etc. Append to `{audit-manifest-doc-id}::friction` as you notice it. Read the existing section first so you don't overwrite prior entries, then `axiom_graph_update_section` with existing + new.

Entry format:

```
- **{short tag}** — {one line: what felt off}
  Context: {raw paste — tool call, output, error, user exchange}
  Wish: {optional — what would've made this easier}
```

Empty is fine. Honest emptiness beats invented friction.

## Error Handling

- **Tag-mutex contention** — handled inline at intake. If `pev-audit-active` is present, force the user choice (resume / release / cancel). Do NOT proceed silently.
- **Dev-docs are stale at pre-flight** — warn the user; offer bail / proceed-with-caveat / cancel. If the user proceeds, log a `dev-docs-pre-flight-stale` entry in `friction` so the impact is searchable later.
- **`git rev-parse HEAD` or `git diff` fails** — usually means the working tree isn't a git checkout or the `since-commit` is invalid. Surface to user; offer to override via the audit-request's `constraints.since` or to abort. Don't silently fall back to full-history if since-commit was set.
- **First-ever run** (no prior `pev-audit-consumer-docs` manifest) — proceed with `meta.since-commit: null` and full-history scan. Warn user that the run will be heavier than typical and Phase 2 may take longer. Consider proposing `--first-run-mode` chunking if the discovery output is large; otherwise proceed straight through.
- **Discovery returns zero findings across all modes** — proceed to Phase 2 anyway (the audit can still find corrections). If Phase 2 also yields zero verdicts ≠ still-true, present "no consumer-doc drift found" to the user, mark manifest `completed` with note, exit gracefully.
- **A subagent returns CONTINUING repeatedly without progress** — surface to user after 3 successive CONTINUING returns; this likely indicates a stuck loop. The user can mark the agent's slice as `partial` and proceed.
- **A subagent dispatch fails entirely** (agent file missing, etc.) — verify `${CLAUDE_PLUGIN_ROOT}/agents/pev-audit-consumer-discovery.md` and `${CLAUDE_PLUGIN_ROOT}/agents/pev-audit-consumer-verifier.md` exist; suggest `/agents` to reload. Cannot work around this in-band; surface to user.
- **User aborts at the Phase 3 gate** — set `meta.status: partial`, leave `pev-audit-active` tag in place so a future re-invocation can resume from the proposals (no need to redo Phase 1/2 work). Present the manifest doc-id and exit.
- **`axiom_graph_update_section` fails on a target consumer doc** during Phase 4 — record the failure in `applied.{consumer-doc-id-tail}` with `status: failed-{reason}` and continue with the remaining patches. Do not abort the whole audit. Surface failures in the Phase 5 summary.
- **Spawn-request slug collision** — append numeric suffix (`-2`, `-3`, ...) and proceed; record the resolved slug in the manifest.
