# PEV Design

Architecture reference for the `pev` plugin. For end-user documentation, see [docs/user-guide.md](./docs/user-guide.md) and [docs/setup.md](./docs/setup.md); they are rendered from `docs/consumer/plugins/pev/` in the repo (the `plugin-pev` render target), so edit the source docs, not the rendered files. For debugging plugin hooks, see [../hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md).

## Architecture overview

PEV is a multi-agent orchestration for structured code changes. Ten agent definitions, each with structurally enforced tool permissions: the five phase agents of a cycle (Architect, Builder, Reviewer, Auditor, Doc Reviewer), `pev-spike` for testing the hooks, and four `pev-audit-*` agents that the audit skills dispatch. The user runs skills: `/pev-cycle` and `/pev-instance`, the audit skills `/pev-audit-consumer-docs`, `/pev-audit-dev-docs` and `/pev-audit-annotations`, and `/pev-spike`. Claude owns reasoning within each phase. axiom-graph MCP tools own the knowledge layer (code index, doc graph). The orchestrator owns phase transitions, human gates, and the cycle manifest lifecycle.

```
User request
  → Orchestrator, on main: baseline SHA, main branch, main repo path, untracked files
  → Orchestrator, in the worktree: axiom-graph checkout + build + entry check
      → cycle docs (in-progress) + .pev-state.json (the only state file)
  → Architect     [doc-write: architect doc, manifest::scope]
    → explore codebase → NEEDS_INPUT questions (proxy-relay) → pitch
    → Human gate (plan)
  → Builder       [code-write; doc-write: builder doc]
    → one task per dispatch → TDD → build plan, progress, task manifests
    → commit on the worktree branch
  → Reviewer      [read-only code; doc-write: review doc, or review-shard-* per shard]
    → 6-pass review (test, source-doc, spec, functionality, quality, PEV-checks)
    → PASS → Human gate (verdict)
    → FAIL → fix list saved in the manifest → Builder loopback (max 2x)
  → Sync          [orchestrator, in the worktree]
    → git merge {main branch} into the branch (never rebase) → record main's stale ids at the sync → build → pre-audit check
  → Auditor       [no code-write; live-doc write, in the worktree]
    → axiom_graph_build + axiom_graph_check → review stale nodes
    → read .pev/doc-topology.docjson → per-category auditor-action
    → update graph-linked + topology-listed docs
    → Impact Report
  → Doc Reviewer  [read-only; audit::doc-review only; same worktree]
    → verify Auditor's doc updates → scan for missed drift
    → FAIL → Auditor loopback in the same worktree (max 2x)
  → Proposed-links gate → changes-summary + consumer-docs render (worktree index)
  → Audit committed on the branch → Merge gate (one unit: code, tests, docs, audit)
  → Merge step    [orchestrator; templates/merge-step-reference.md]
    → full suite → ExitWorktree(keep) → preflight from main (re-sync if main moved)
    → record main's stale ids → git merge --no-ff (Auditor: line)
    → build → carry_forward (dry run, then real) → id diff → residue gate (per node)
    → PEV complete: commit + checkpoint → worktree and branch removed
  → Complete: push options
```

**Planning model: Shape Up over waterfall.** The Architect writes a pitch — problem, user stories (3–5 coarse outcomes), solution sketch (fat-marker approach at module level), constraints (rabbit holes and no-gos). The Builder receives orientation and boundaries, not a prescriptive implementation spec. The Builder reads source code and makes implementation decisions.

**Cycle docs as central artifact.** Each cycle is a directory, `docs/pev/cycles/<cycle-id>/`, of seven DocJSON docs (see "Cycle docs structure" below) carrying the pitch, build plan, review findings, impact report, and a machine-rendered changes-summary derived from `axiom_graph_report(since_sha=baseline)` on the worktree's index, which the orchestrator writes after the Auditor returns and before the doc review. Agents write their phase's outputs to their own docs as they work, so partial progress survives incarnation cutoffs. The orchestrator reads the manifest's `status` section to determine phase transitions.

**`/pev-instance` shares the spine, not the machinery.** Same user-story framing, same plan gate, same worktree and merge step; no Architect, Builder or Auditor. It takes one or more tasks and runs them one after another, each in its own worktree created from local HEAD: one agent in the user's session plans, implements, updates the docs its change affects (the Auditor's doc-update role is folded into it, not dropped) and commits on the branch. It then dispatches the Reviewer and the Doc Reviewer in parallel, in that worktree, for an independent review of the commit, fixes what they find, and lands the task through the merge step before the next task's worktree exists. Its record is a checkin doc at `docs/pev/instances/<id>/checkin` (older instances are single files, `docs/pev/instances/<id>.docjson`), under the same namespace so `axiom_graph_search` finds both cycle and instance history.

## Worktree to main: the merge step

**Everything up to the merge happens in the worktree.** The Auditor and the Doc Reviewer run there, after the orchestrator has merged local main into the branch, so they audit the tree that will land. The merge gate then approves one finished unit (code, tests, docs, audit, doc review), and main receives only finished work. There is no Auditor mutex: parallel cycles audit in parallel, each in its own worktree.

**Nothing touches main from inside the worktree.** A worktree-isolated session cannot run `git -C` on the main checkout; the harness refuses it. Main-side git runs before `EnterWorktree` (intake records the baseline SHA, main's branch and path, and main's untracked files) or after the merge step's `ExitWorktree`. Inside the worktree, main is reached through its branch ref, which worktrees share (`git merge {main_branch}`, `git diff {baseline_sha} {main_branch} -- <path>`).

**One state file.** `.pev-state.json` is written once, at the worktree's root, and serves every role, the Auditor and the Doc Reviewer included. There is no copy on main; the hooks read the file from the caller's working-directory root and confine each role to the worktree's `project_root`. `/pev-instance` writes one in each task's worktree, with `layout: "instance"`.

**The merge step settles main by carry, not by a second audit.** `templates/merge-step-reference.md` is the one procedure, shared by `/pev-cycle` and `/pev-instance`. It names its inputs (run id and kind, run doc, baseline section, run directory, worktree, branch, main branch and path, baseline SHA), so it reads on its own and could later be a merge agent's brief. In order: the full suite in the worktree; `ExitWorktree(keep)`; preflight from main, syncing main into the branch again if it moved; main's stale node ids recorded in the run's `baseline` section; `git merge --no-ff` of the branch; `axiom_graph_build`; `axiom_graph_carry_forward`, a dry run and then the real carry; a diff of main's stale ids against the recorded set; the residue gate; the completion commit and the history checkpoint; removal of the worktree and the branch.

- **Why carry works.** The merge commit's tree equals the branch tip, so every node the worktree verified has the same content on main, and `carry_forward` copies its verification across, each dimension (own content, each link) separately. The worktree must outlive the carry, because the carry reads its index; the carry writes only main's index, never files.
- **Residue is a per-node decision.** Whatever is still stale after the carry is compared by node id with main's pre-merge set; an id that was already there is not the run's. The new ids (nodes main and the branch both changed, or sections a doc-to-doc link holds until its target settles) go to one gate, decided per node: `mark_clean` / `reverify` on main with an `agent:` verifier, or recorded for a follow-up. Nothing is cleared in bulk.
- **Never rebase.** `carry_forward` matches content, so the history's shape doesn't matter to it, while a rebase would rewrite the branch SHAs the cycle docs cite and replay DocJSON conflicts once per commit. Main takes the branch with `--no-ff`: one merge commit per run, whose message for a cycle carries an `Auditor:` line (an instance's carries one `Review:` line instead).
- **Two commits on main.** The merge commit, then `PEV complete: {run-id}`: the run's manifest (or checkin) with its final check and status flip, and the efficiency report, which now counts the merge step's own calls. The final check can only exist after the carry, so this commit is written on main.
- **Cycles started under the old order** (merged first, audited on main) finish on that order; the orchestrator reference's Legacy Layout section keeps the short note.

## Gates, resume points and sharded review

**One gate shape.** Every human gate the orchestrator or an instance presents, and every Reviewer, Auditor or Doc Reviewer `NEEDS_INPUT`, gives the gate's name, the decisions to make with a recommendation for each, exactly one yes/no question, and the content quoted verbatim from the docs. The shape lives in one place, the orchestrator reference's **Gate Payload** section, and the skills cite it. The Architect's brainstorm rounds are not gates and keep their own protocol.

**`Resume at:`.** The cycle's `manifest::status`, and an instance checkin's `meta`, carry one `Resume at:` line naming the step a fresh session picks the run up at. It is rewritten at every gate and phase transition and before any stop.

**Sharded review.** When the pitch's constraints list `**Review shards:**` (many tasks, or several subsystems), the orchestrator clones one `review-shard-{a,b,...}` doc per entry and dispatches the shards in parallel. Shard A runs Pass 0 (the tests) for everyone; each shard runs Passes 1-5 for its own tasks and writes its own verdict. The orchestrator writes the consolidated `review::verdict` (the worst status wins) and a cross-shard sweep. Approved fixes are written to `manifest::fix-list.round-N` before the fix dispatch, so the fix Builder and the re-review cite items by number.

## Tool permissions matrix

Each agent's `tools:` frontmatter is a runtime-enforced allowlist. Agents cannot see or call tools outside their list. The invariant:

> **In the full `/pev-cycle`, no single subagent both writes code AND updates live feature docs.** The Builder writes code; the Auditor writes docs; never the same agent. `/pev-instance` is the deliberate single-agent exception — see footnote ¹.

| Capability | Architect | Builder | Reviewer | Auditor | Doc Reviewer | pev-instance¹ |
|---|---|---|---|---|---|---|
| Read code (`axiom_graph_source`, `Read`, `Grep`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Search/graph (`axiom_graph_search`, `axiom_graph_graph`) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Write code (`Edit`, `Write`, `Bash`) | ✗ | ✓ | ✗² | ✗ | ✗ | ✓ |
| Write cycle manifest (`axiom_graph_update_section`) | ✓ | ✓ | ✓ | (not used) | ✓ | ✓ |
| Write live feature docs | ✗ | ✗ | ✗ | **✓** | ✗ | ✓¹ |
| `axiom_graph_build` / `axiom_graph_check` | ✓ | ✓ | ✓ (check only) | ✓ | ✗ | ✓ |
| `axiom_graph_mark_clean`, `axiom_graph_reverify`, `axiom_graph_accept_doc_edits`, `axiom_graph_purge_node` | ✗ | ✗ | ✗ | **✓** | ✗ | ✓¹ |
| Commit in git | ✗ | ✓³ | ✗ | ✗ | ✗ | ✓ |
| User interaction | Proxy⁴ | ✗ | Proxy⁴ | Proxy⁴ | Proxy⁴ | Direct |
| Dispatch subagents | ✗ | ✗ | ✗ | ✗ | ✗ | ✓¹ |

**Doc edits and linked staleness.** A doc write verifies only the text it writes. It never clears `LINKED_STALE`, the staleness a section gets when a node it links to changes. Clearing that is a separate, explicit judgment: `addresses=[...]` on `axiom_graph_update_section` / `axiom_graph_patch_section` names the changed nodes an edit reconciles (the section clears once every one is named), and `mark_clean` / `reverify` clear it outright. Only the agents that write live docs use `addresses=`: the Auditor, `/pev-instance` and the dev-docs audit shards.

**¹ `/pev-instance`** runs in the user's own session, which enters a worktree for each task. The only subagents it dispatches are the Reviewer and the Doc Reviewer, for the independent review after its commit. Full tool access by design; the discipline comes from the skill's prompt, not tool restriction. This is precisely why it is the exception to the invariant above: the one agent both writes code *and* updates live docs (`mark_clean`/`reverify` included), folding the Auditor's doc-update role into itself for small tasks. See the `/pev-instance` skill's "Audit & doc update" step.

**² Reviewer has `Bash`** for read-only use (`git diff`, `pytest`, `git log`). `Edit` and `Write` are absent from its allowlist. It's expected to use Bash only for inspection.

**³ Builder commits** on the worktree branch (`git add <paths>`, staged by explicit path, + `git commit -F .pev-scratch/msg.txt`) as a transport mechanism. The orchestrator commits the audit on the same branch and lands it on main through the merge step. Builder does not push.

**⁴ Proxy-question protocol:** `AskUserQuestion` is not available in subagents (Claude Code platform limitation). Subagents return a `NEEDS_INPUT` JSON payload; the orchestrator relays questions to the user via `AskUserQuestion` and resumes the agent with answers via `SendMessage`.

## Hook architecture — the load-bearing invariants

PEV's runtime enforcement lives in plugin hooks. Three invariants that any contributor extending the plugin must respect.

### 1. All hooks live in `pev_nexus_agents/pev/hooks/hooks.json`

Agent-frontmatter `hooks:` blocks silently no-op in marketplace installs (see the `hook-spike` matrix). Register every hook at plugin level using `${CLAUDE_PLUGIN_ROOT}/hooks/<name>.sh` paths.

### 2. Per-agent behavior dispatches inside scripts on `agent_type`

Each hook's stdin JSON carries `agent_type` (e.g. `pev:pev-builder`) when a PEV subagent is the caller; absent when the orchestrator is. Every hook script:

```bash
INPUT=$(cat)
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool   # or posttool / stop: no jq → deny PEV agents, pass everyone else
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
case "$AGENT_TYPE" in pev:*) ;; *) exit 0 ;; esac
# ...agent-specific logic dispatched by full agent_type value
```

**One deliberate exception:** `pev-docjson-guard.sh` has no `agent_type` gate. A raw edit of a `*.docjson` document desyncs the index whoever makes it, so the guard applies to every session. It only ever matches paths ending in `.docjson`, so it is inert in a project whose documents are still `*.json`. `PEV_DOCJSON_GUARD` picks how it answers a detected raw write; detection is the same in every mode:

- **`warn`** (the default; also unset or any unknown value) lets the write through with a note (`additionalContext`) naming the doc tools and `axiom_graph_accept_doc_edits`. It never answers `allow`, because that would skip the user's own permission rules and prompts.
- **`block`** denies the write.
- **`off`** silences the hook.

This guard never denies a read, in any mode, with or without jq, for main sessions and PEV agents alike. Reading a document, including with a script, never desyncs the index. In `warn` and `block` mode the first raw read of a `.docjson` in a session (a Read, a Grep whose `path` or `glob` names one, or a Bash command naming one without writing it) gets one line of context pointing at `axiom_graph_read_doc` (`outline=true`, then `section_ids`) and `axiom_graph_search` (a quoted phrase, `scope="docs"`). A marker file in `${TMPDIR:-/tmp}`, named from the sanitised `session_id`, keeps it to once per session; with no `session_id` it fires every time, and without jq it is skipped. Because the matcher includes Read and Grep, those calls exit before the shared jq check, and so does a Bash call whose no-jq write checks find no document write; only a write tool (Write/Edit/MultiEdit/NotebookEdit) by a PEV agent without jq reaches the shared install-hint deny. That is a property of this guard only: without jq, other PEV hooks (`pev-tool-gate.sh`, `pev-bash-scope.sh`, ...) still fail closed for PEV agents, reads included. Agents still log script reads as `script-read` friction, so the reads show where the axiom-graph tools fall short. A write the guard misses, or lets through in warn mode, is still reported afterwards as a raw DocJSON edit.

Per-agent config (budgets, allowlists, scope rules) lives in the hook script, dispatched via `case "$AGENT_TYPE" in pev:pev-<role>) ... ;; esac`. Do **not** maintain per-agent config in `.pev-state.json` — state-file dispatch races with orchestrator tool calls (the state file says "builder phase" while the orchestrator is doing intermediate work between phases).

### 3. Counter files keyed on `agent_id`

Tool-budget counters are per-subagent-invocation. The `agent_id` field in stdin JSON is unique per dispatch, so `/tmp/pev-counter-<agent_id>.txt` never collides even when the orchestrator dispatches the same agent type twice in one cycle. Cleaned up by `pev-subagent-stop.sh` when the subagent returns.

### Hook roster

| Hook | Event | Purpose |
|---|---|---|
| `pev-worktree-scope.sh` | PreToolUse (Write/Edit) | Blocks writes outside the worktree. One carve-out: a write into this session's own Claude Code scratchpad directory (`<temp>/claude[-<uid>]/<project>/<session_id>/scratchpad/`) is allowed |
| `pev-doc-scope-md.sh` | PreToolUse (Write/Edit) | Auditor only: its Write and Edit may target only `*.md` files (CHANGELOG, plugin READMEs); code and config are the Builder's, and `.docjson` goes through the axiom-graph doc tools |
| `pev-bash-scope.sh` | PreToolUse (Bash) | Blocks `cd` out of the worktree |
| `pev-worktree-rm.sh` | PreToolUse (Bash) | Allows, without a permission prompt, a single plain `rm` of relative paths inside the agent's cycle worktree (optionally after one `cd <dir-in-worktree> &&`), so unattended cycles don't stall on approvals |
| `pev-docjson-guard.sh` | PreToolUse (Write/Edit/MultiEdit/NotebookEdit/Bash/Read/Grep) | Raw edits of `*.docjson` in every session: warns by default, denies with `PEV_DOCJSON_GUARD=block`, silent with `off`; points at the axiom-graph doc tools. Hints once per session on a raw read; this guard never denies one, even without jq |
| `pev-doc-scope.sh` | PreToolUse (axiom-graph doc-write tools) | Restricts each agent's doc writes to the cycle docs it owns (the Auditor also writes docs outside the cycle directory); refuses `clone_doc` to every PEV agent |
| `pev-axiom-graph-scope.sh` | PreToolUse (mcp__axiom-graph__.*) | Enforces axiom-graph `project_root` matches worktree |
| `pev-tool-gate.sh` | PreToolUse (.*) | Post-budget allowlist gate |
| `pev-tool-counter.sh` | PostToolUse (.*) | Increments counter; emits budget advisories |
| `pev-subagent-stop.sh` | SubagentStop | Counter cleanup + Builder axiom-graph rebuild |

## Cycle docs structure

Each cycle is a directory, `docs/pev/cycles/<cycle-id>/`, of seven docs:

| Doc | Written by | Holds |
|---|---|---|
| `manifest` | Orchestrator (the Architect writes `scope`) | `status` (every phase transition, and the `Resume at:` line), `request` (the user's prompt, verbatim), `scope`, `change-set` (after the Phase 6 sync: the branch's diff, the sync list, main's stale ids at the sync, task manifests, the pre-audit verdict), `baseline` (the entry check's stale ids, main's pre-merge ids, the final check), `fix-list` (one `round-N` per approved fix round) |
| `architect` | Architect | The pitch: problem, user stories, solution sketch, constraints, tasks, test plan |
| `decisions` | Architect, Builder | `d-N` entries, append-only: what was chosen, the alternatives, why |
| `builder` | Builder | `build-plan`, then per incarnation and task `inc-N.task-M.{progress,manifest,checkpoint}` |
| `review` | Reviewer (orchestrator: the consolidated verdict of a sharded review) | One `pass-N` entry per pass, then the verdict. A sharded review adds one `review-shard-{a,b,...}` doc per shard |
| `audit` | Auditor; Doc Reviewer (`doc-review`); orchestrator (`changes-summary`) | `impact-report`, `progress`, `changes-summary` (from `axiom_graph_report`, after the Auditor returns and before the doc review), `doc-review.{progress,findings}` |
| `friction` | Every agent and the orchestrator, each under its own group | Append-only friction entries |

A cycle started on an earlier version is a single file, `docs/pev/cycles/<cycle-id>.docjson` (the legacy layout), and finishes in that form; the doc-scope hook keeps a legacy branch for it.

`/pev-instance` writes one checkin doc, `docs/pev/instances/<id>/checkin`, cloned from `.pev/templates/instance.docjson` in the task's worktree at the plan gate: `meta` (status, `Resume at:`, the worktree, branch and main lines the merge step reads), `baseline` (the entry check's ids, then main's pre-merge ids), `problem`, `user-stories`, `acceptance`, `plan`, `changes`, `doc-updates`, `review` and `doc-review` (the independent review's results), an optional `escalation`, and `friction`.

### Friction logs

Each phase-agent (Architect, Builder, Reviewer, Auditor, Doc Reviewer) and the orchestrator owns a group in the cycle's `friction` doc (`friction::architect`, `friction::builder`, `friction::reviewer`, `friction::auditor`, `friction::doc-review`, `friction::orchestrator`). Agents append observations when something pinches during work — instructions that didn't fit the situation, tool output that was awkward, role constraints that forced workarounds, upstream inputs that required guessing, effort disproportionate to value.

This is distinct from the `decisions` log (cycle-wide record of what was chosen and why) and from the `deviations` in the Builder's task manifests (structured Builder-vs-plan delta). Friction is phenomenological: what was hard or felt off, regardless of whether the agent deviated.

Entries follow a short-tag + raw-context-paste format documented in each skill; each entry is added as its own subsection under the agent's group with `axiom_graph_add_section` (structure in `templates/cycle/friction.docjson`). Initiative-based, not gated — agents capture in-the-moment or not at all. Empty sections are expected and acceptable; the value compounds across cycles as `axiom_graph_search` surfaces recurring tags (e.g., `axiom-graph-staleness`, `instruction-ambiguity`, `role-pinch`) that drive skill and tool evolution.

Each cycle is a directory of seven docs cloned from the project's seeded templates (`.pev/templates/cycle/`): `manifest`, `architect`, `decisions`, `builder`, `review`, `audit` and `friction`. Each doc has one owner, which the doc-scope hook enforces by agent type. Agent-owned sections that exist from the start (`builder::build-plan`, `audit::impact-report`, `audit::doc-review.findings` and the rest) are filled with `axiom_graph_update_section`. Log entries are added at runtime as their own subsections with `axiom_graph_add_section`: `decisions::d-N`, friction entries under each agent's group in `friction`, the Builder's `builder::inc-N.task-M.{progress,manifest,checkpoint}` and the Reviewer's `review::pass-N`. The `decisions` and `friction` docs are append-only: the hook refuses `update_section` and `patch_section` there.

## Agent responsibilities (one-line each)

- **Architect** — read codebase, interact with user via proxy-questions, write Shape Up pitch + test plan to the `architect` doc. No code writes. Works in worktree.
- **Builder** — read Architect pitch, implement with TDD in worktree, commit on worktree branch. Writes its build plan, per-task progress, manifests and checkpoints to the `builder` doc and returns a short control envelope. Has worktree-scoped code-write tools.
- **Reviewer** — six-pass review of Builder's code against Architect pitch (tests, source docs, spec compliance, functionality preservation, code quality, PEV-specific). Read-only code access. Writes review results to the `review` doc. Also reviews a `/pev-instance` commit, writing to the checkin's `review` section.
- **Auditor** — runs in the cycle's worktree, after main is synced into the branch and before the merge; the merge step carries its verifications to main. Reviews every stale node; updates graph-linked docs; reads `.pev/doc-topology.docjson` and performs `auditor-action` per triggered category. Writes Impact Report. Doc-write on live feature docs and `*.md` edits, but no code-write.
- **Doc Reviewer** — runs in the same worktree, before the merge. Verifies Auditor's doc updates against Builder's work; scans for drift in doc categories the Auditor may have missed. Read-only except its `audit::doc-review` sections (or the checkin's `doc-review` for `/pev-instance`).
- **pev-spike** — special test agent. 13-test integration smoke test of PEV hook infrastructure (worktree scope and its scratchpad carve-out, bash scope, doc scope, axiom-graph scope, budget warnings, gate, allowlist). Used only for validating PEV itself.
- **`pev-audit-*`** — the audit skills' agents: `pev-audit-consumer-discovery` and `pev-audit-consumer-verifier` (`/pev-audit-consumer-docs`), `pev-audit-dev-shard` (`/pev-audit-dev-docs`) and `pev-audit-annotations-fixer` (`/pev-audit-annotations`). Each writes only its own audit run's docs; its frontmatter allowlist is the enforcement layer.

## `.pev/` SOPs — extension points

Four project-customizable DocJSON files consumers may create in their repo:

| File | Consumed by | Purpose |
|---|---|---|
| `.pev/doc-topology.docjson` | Auditor (proactive) + Doc Reviewer (verify) | Project doc taxonomy with per-category `auditor-action` and `doc-reviewer-check` |
| `.pev/test-policy.docjson` | Architect + Builder + Reviewer | Test tier system, annotation contract, coverage, budget |
| `.pev/review-criteria.docjson` | Reviewer | Project-specific code-review emphasis |
| `.pev/architecture-policy.docjson` | Architect + Builder + Reviewer | Code layers: where new behavior belongs, what each layer may import, where tests enter |

The first three have a plugin-shipped fallback at `${CLAUDE_PLUGIN_ROOT}/templates/<name>.docjson`; skills read the project file first and fall back to the template if absent. The architecture policy has no template: without it, agents follow the layering of the existing code.

### Procedure vs. knobs: when to split a SOP into a plugin reference

A `.pev/` SOP holds content the **consumer edits** — project knobs (paths, thresholds, vocabulary) and customizable *defaults* (doc categories, the test tier system). That's why it's a copyable file with a template fallback: the consumer shapes it.

Content the **consumer never edits** — fixed, role-shared plugin *procedure* — does not belong in a copied SOP. Living there, it gets duplicated across every skill that uses it and forces consumers to hand-merge it on each plugin update. It belongs in a **plugin-internal reference** instead: a `templates/<name>-reference.md` read from `${CLAUDE_PLUGIN_ROOT}`, never copied, that the skills point at and the consumer never sees (the same shape as `pev-orchestrator-reference.md` and `auditor-reference-protocol.md`). Each skill keeps only a pointer plus its role-specific disposition.

The test: **does a consumer ever edit this?** Yes → a `.pev/` copyable SOP. No → a plugin reference.

`doc-topology.docjson` shows both halves. Its `category.*` sections are consumer-customizable, so they stay in the copied file — but the **Link Audit** procedure (the fixed add/repoint/drop logic every role runs identically) was extracted to `templates/link-audit-reference.md`, leaving only the project's `Scope` knob behind. By contrast, `test-policy.docjson`'s tier system is a customizable *default* — the policy explicitly invites projects to add or replace tiers — so it correctly stays in the copyable SOP, not a reference.

### Adding a new SOP

If you extend PEV with a new concern that varies per project:

1. Create `pev_nexus_agents/pev/templates/<new-sop>.docjson` as the plugin's default. DocJSON format with clearly-named sections. Include an `overview` section documenting which skills read this file and what fields matter.
2. Update the relevant skill to read `{worktree_path}/.pev/<new-sop>.docjson` (or `.json`) first, fall back to `${CLAUDE_PLUGIN_ROOT}/templates/<new-sop>.docjson`. Use `Read` tool + JSON parse; don't require axiom-graph indexing.
3. Document the new SOP in this file's table above and in the consumer user guide's `customizing-via-pev-sops` section (`docs/consumer/plugins/pev/user-guide` in the repo), then re-render the `plugin-pev` target so [docs/user-guide.md](./docs/user-guide.md) matches.
4. **Split fixed procedure out.** If the SOP mixes project knobs/defaults with fixed plugin procedure the consumer never edits, put the procedure in a `templates/<name>-reference.md` (read from `${CLAUDE_PLUGIN_ROOT}`, never copied) that the skills point at — don't bury it in the copyable SOP, where it would duplicate across skills and create a merge burden on upgrades (see "Procedure vs. knobs" above).

### Adding a category to `doc-topology.docjson`

Add a section with ID `category.<name>` containing four fields in markdown content: Path, Triggered by, Auditor action, Doc Reviewer check. The Auditor reads all `category.*` sections and iterates; no skill change needed for a new category in a consumer's topology.

## Cross-agent signals

Signals that several agents consult to stay consistent:

- **`axiom_graph_workflow_list(has_steps=true)`** — returns functions with `@workflow` + `Step()` markers. Framed as **developer-declared core mechanisms**. Used by:
  - Reviewer Pass 3 (functionality preservation — scrutinize callers harder for workflow-marked functions)
  - Reviewer Pass 4 (code quality — findings in workflow-marked code rank `important`/`critical`, not `minor`)
  - Reviewer Pass 5c (workflow markers match code behavior)
  - Reviewer Pass 5d (taxonomy hygiene — suggest new markers for Builder-added functions that cross core-mechanism thresholds)
  - `/pev-instance` scope check (escalates to `/pev-cycle` if the task touches any workflow-marked function)

- **`.pev/test-policy.docjson` tier table** — canonical tier-to-annotation mapping. Architect uses it for `architect.test-plan`, Builder for actual test annotation, Reviewer for Pass 5b cross-check.

- **Cycle manifest `architect.test-plan`** — the source of truth for what tests the Builder owes. Reviewer checks Builder's tests against this row by row.

## Why axiom-graph-integrated

PEV depends on axiom-graph for:
- Code reads during planning and review (`axiom_graph_source`, `axiom_graph_graph`, `axiom_graph_search`)
- Doc graph for the Auditor's graph-linked doc updates
- Cycle manifest persistence + searchability via DocJSON
- Workflow marker introspection via `axiom_graph_workflow_list`

PEV could, in principle, be axiom-graph-independent (`.pev/` SOPs as an abstraction layer + `Read`/`Grep` fallbacks for code reads). This is deliberately out of scope — the value of the integration in practice outweighs the portability.

## Platform observations worth preserving

These are cross-platform and cross-Claude-Code-version issues that shaped the plugin:

- **Windows `cwd` in hook JSON uses backslashes** — hooks must `cygpath -u` before building state-file paths. Without this, `[ -f ]` tests fall open.
- **Native Windows jq cannot open POSIX paths** — `jq -r '...' "$STATE_FILE"` returns empty when `$STATE_FILE` starts with `/c/`. Hook scripts use `cat "$STATE_FILE" | jq -r '...'` to work around.
- **`grep -oP` requires a UTF-8 locale** which the hook execution env doesn't reliably set. All PCRE uses replaced with POSIX `sed`.
- **Hook matchers are full-string regex.** `"mcp__axiom-graph__"` does not match `mcp__axiom-graph__axiom_graph_source`; use `"mcp__axiom-graph__.*"`.
- **Agent-frontmatter hooks silently no-op** in marketplace plugin installs. All PEV hooks live in `pev_nexus_agents/pev/hooks/hooks.json`, none in agent frontmatter.
- **Slash commands through `claude -p` on git-bash need `MSYS_NO_PATHCONV=1`** or the slash becomes a mangled filesystem path.

All of these are documented with symptoms + fixes in [../hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md) §6 and §7.
