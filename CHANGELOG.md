# Changelog

All notable changes to axiom-graph are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Tag scopes.** This repo is now a monorepo. Tags are prefixed by component:
> `axiom-graph-v*` — `axiom_graph` Python package (this CHANGELOG); `pev-v*` and `hook-spike-v*` —
> Claude Code plugins under `pev_nexus_agents/` (see `pev_nexus_agents/pev/CHANGELOG.md`).

## [2.3.0] - 2026-08-16

Workflow-graph correctness release. Delegation chains the Python scanner used to drop — method-to-method calls on `self`/`cls`, receivers imported inside a function body, calls routed through a re-export shim — now resolve; superseded delegate edges are retired instead of accumulating; a delegate target that names nothing is reported rather than written as a plausible-looking id; and `Step` / `AutoStep` markers declared inside a loop or `try` body stop being silently discarded. Alongside that: preview tooling for a future doc-id namespace migration (which changes no ids in this release), rename-engine repairs that were losing section verification, a build that no longer spends most of its time re-indexing FTS, an mtime fast-pass that recovers instead of dying permanently, composing reverifies, and six viz fixes.

### Added

- **`GET /api/workflow/{id}/steps?expand=true` returns the transitive AutoStep tree.** Each row carries the expanded dotted `step_number` (`2.3.1.3.1`), a `depth` counting delegation hops, and the `note` the expander emits when it stops early (cycle detected, delegate target not annotated). Omitting the flag returns the previous single-envelope payload unchanged, with no `depth`/`note` keys. `/api/test/{id}/steps` and `/api/test/{id}/detail` are untouched.
- **`AutoStep` delegation resolves `self.method()` and `cls.method()` to the sibling method.** An `AutoStep` followed by a method call on `self` or `cls` emitted no `delegates_to` edge at all — attribute-form calls resolved only when the receiver was an import binding, and `self` / `cls` never are — so every method-to-method delegation broke the workflow chain at the class boundary. The target is now built from the enclosing class as `{module_id}::{ClassName}.{attr}`, read from the scanner's walk position rather than a lookup table, so two classes in one module that share a method name each resolve to their own. Only a *direct* receiver resolves: `self.collaborator.method()` names a method of the collaborator rather than of the class, and now resolves to nothing — the honest answer — instead of to an id no build ever mints. JS/TS is unaffected: a class method there cannot be an envelope, so there is no in-class `AutoStep` for a `this.` branch to resolve from.
- **Delegate links are now checked for broken targets.** Broken-link detection covered `documents` and `validates` edges. A `delegates_to` edge naming a node that does not exist is the same class of defect — navigation dead-ends, and consumers print an identifier that resolves to no source — and was previously written into the index unreported. A finding on a step is attributed to the envelope that composes it: that is the node a maintainer acts on, and steps carry no staleness dimensions of their own, so a status written on one would be recomputed away. A leftover step that no `workflow` / `task` composes keeps the finding on itself, because its own id is the only identifier that names it. **Expect new `BROKEN_LINK` findings on the first build after upgrading** — see *Upgrading an existing index* below.
- **New `axiom-graph doc-ids preview` command.** Doc node ids flatten every configured docs root into one `docs.` namespace and rewrite `/` to `.`. Neither transform is injective, so `docs/adrs/013-x.json`, `docs/adrs.013-x.json` and `.pev/adrs/013-x.json` can all derive one identity, and the file scanned last silently wins. `preview` projects what every document *and* section id would become under per-root namespacing, reports whether any two would collide, and enumerates the prose references a migration would not rewrite, with file and line. It is strictly read-only — no database mutation, no file mutation. **This release changes no doc ids**; the derivation still produces the current form, and the projection is a planning aid for the namespace change that will land in a future major release. An `execute` subcommand exists beside it and is deliberately not documented as ready to run — see the note under **Changed**.

### Fixed

- **The mtime fast-pass stopped working the first time a file's mtime moved, and never recovered.** `file_mtime` was written only by `upsert_node`'s insert path, so in discovery-only mode — the default for every CLI and MCP `build` — an existing node's stored mtime stayed frozen at first insertion. Any later move dropped that file out of the fast-pass permanently, and the moves are routine: an ordinary edit, a branch switch, a rebase, a stash pop, `git worktree add`. Measured on a fresh worktree, 462 of 463 locations missed the fast-pass. `build()` now stamps the on-disk mtime of every file it opened and parsed, in both build modes, and reports the count as `file_mtimes_stamped` in the build summary. The stamped set is harvested from the collected node set, so a scanner-skipped file contributes zero nodes and falls out for free rather than by subtraction. Stamping is a promise that the next build may skip the file entirely, so it runs last of the passes gated on this build's scanned set — vanished-section pruning and `documents`-edge reconciliation — and is suppressed outright when either of them failed, leaving the stored mtime behind so the next build re-scans the file and retries. Scanners now sample a file's mtime *before* reading its bytes, so a concurrent write costs a redundant re-scan rather than a permanent skip. `compute_staleness` batch-loads stored mtimes once instead of opening a connection per location, and the point reader selects `MAX(file_mtime)` to agree with the bulk reader on locations stored across several rows. Known and accepted gap: embedding generation is also scanned-set-gated but runs after the stamp, so a file whose embeddings failed is not retried until its bytes change.
- **`axiom_graph_reverify` could never clear a dependent attributed to more than one offender — in any order.** 2.2.0 shipped the tool skipping any node whose root-offender set was not wholly inside the reverified source's subtree, and that check ran against the raw offender set every time: reverifying offender A left the dependent skipped, and reverifying B afterwards skipped it again, so a two-offender dependent was unreachable by `reverify` no matter how many of its offenders you verified. **Reverifies now compose.** An offender carrying a `reverify` verification newer than its own most recent content-bearing change — ordered by monotonic `node_history` row id, so no clock is read — counts as no longer outstanding and drops out of each dependent's root set, so the last reverify in a series clears the dependent, reaching the same end state as marking that dependent clean directly. The discount is computed before any write in the call, so a reverify never discounts its own source mid-flight, and every ambiguous case still resolves to "outstanding": an offender with no qualifying verification row, none with a change row to order against, and rows written before this provenance existed. Under-clearing remains acceptable, over-clearing does not. The skip report still decides relatedness on the raw offender set — narrowing there would make a dependent whose remaining offenders lie outside the source look unrelated and silently drop it from the report — but now lists only the offenders still outstanding, and the MCP skip block closes with the action that clears them. Verification rows record which operation wrote them (`verification_op` in the history `meta` payload), keeping `mark_clean` and `reverify` distinguishable; rows written before this release carry no such value and are treated conservatively. This supersedes the 2.2.0 description of the skip rule.
- **Builds no longer spend most of their time re-indexing doc sections into FTS.** `index_doc_sections_fts` refreshed `node_fts` with a `DELETE`/`INSERT` pair per section. `node_fts` is an FTS5 virtual table, and FTS5 cannot carry a secondary index on a column, so `DELETE FROM node_fts WHERE id = ?` had no index to use and resolved to a full scan of the FTS table — about 11.5 ms per statement. One scan per section made the pass quadratic in the number of sections, and because the pass has no dirty-check it ran in full on every build, including no-op builds where every node was skipped as unchanged; on a ~3,300-section graph it accounted for roughly 78 seconds of a 95-second build, making it the single dominant build cost. The refresh now issues one bulk `DELETE` driven by a subquery over the section filter, plus one `executemany`. On a 2,668-section graph the pass drops from ~27s to 0.72s and a full build of this repo to 22s. Indexed content is unchanged: duplicate rows for one section still collapse to a single current row, rows that are not section nodes (code nodes, and orphans with no backing node row) are still left untouched, and a NULL section body still indexes as an empty string. `upsert_node`'s equivalent per-node refresh is untouched — it only pays the cost for nodes that actually changed.
- **Viz: the Workflows tab showed only one envelope's own step markers.** The tab rendered whatever `Step`/`AutoStep` markers were physically written in the selected function and stopped there, so a workflow whose AutoSteps delegate into `@task`-decorated helpers displayed as a handful of steps with no way to see the flow inline — a 3-marker function backed by an 18-step tree read as 3 steps. `workflow_expanded_steps` had produced the full tree since the Phase 3 expansion work but had no caller anywhere under `axiom_graph/viz/`; the tab now requests the expanded payload and indents rows by delegation depth. Nested steps indent per hop rather than per dot, so a minor marker (`2.3.1`) sits level with its siblings in the same function while a real delegation (`2.3.1.1`) indents further.
- **Viz: AutoStep rows rendered without a purpose.** An `AutoStep` marker carries no purpose of its own — the intent lives on the `@task`/`@workflow` it delegates to — so these rows showed a bare name and a jump button. Expanded rows now resolve the purpose from the delegate target's envelope. The unexpanded payload is unchanged, and MCP `workflow_detail` still reports an AutoStep's own (empty) purpose; unifying the two is tracked in `docs/pev-requests/autostep-purpose-inheritance-in-shared-api.json`.
- **Viz: clicking a workflow step opened the wrong file.** `_step_row_to_dict` dropped the location its query already selected, so a step dict carried a line number with no file, and the click handler fell back to the root workflow's module for every step regardless of depth. A step whose marker lives in a sibling module therefore opened the wrong file at a line borrowed from another — landing on a plausible wrong line rather than failing visibly. The payload now carries each step's own location and the handler reads it, falling back to the envelope only when it is genuinely absent. Step ordering is fixed alongside: `Number()` coercion is `NaN` beyond one dot and reads a single dot as a decimal, so `3.10` tied with `3.1` and sorted ahead of `3.2`. A segment-wise comparator replaces it.
- **Viz: only the last section's mermaid diagrams rendered in the doc viewer.** The viewer renders a doc one section at a time, and each pass *replaced* the shared mermaid source collector instead of adding to it — so by the time the diagrams were painted the collector held only the final section's fences, and every earlier diagram stayed an empty block. A document ending in prose (the common shape: diagram in the model section, decisions last) lost all of its diagrams; one whose last section held a diagram rendered that source in the first diagram's slot, because placeholder indices restarted at zero per section too. Sources now accumulate across the whole render pass, indexed document-wide, and reset once per document. The diagram editor's live preview was never affected — it renders its source directly rather than through the collector.
- **Viz: the doc tree flattened every configured docs root into one folder.** `docDir()` derived a doc's folder from its id dotpath, and ids flatten every root into the same `docs.` namespace — so `.pev/architecture-policy.json` rendered as a top-level file in `docs/`, and same-named subfolders in different roots (`docs/cycles` and `.pev/cycles`) merged into a single folder. The folder now comes from the doc's `file_path`, which the list endpoint already returned, and each configured root gets its own top-level folder in `docs_dirs` order with the primary first. A configured root holding no docs still appears, as an empty folder; a directory under no configured root still falls back to the primary. Each root is defaulted to expanded once, recorded per root, so a tree whose saved expanded-set predates root folders doesn't come up looking empty — and a root you deliberately collapse stays collapsed across reloads.
- **Viz: "Open in Docs" from the source panel only worked once per session.** The button wrote its target to `cortex-doc-id`, which the docs view honors only when nothing is selected there yet, so the first jump landed and every one afterwards silently left whichever doc was already open. It now also writes a one-shot `cortex-doc-pending` key that the docs view consumes unconditionally, expanding the target's ancestor folders before the tree renders and scrolling its row into view. The plain last-selected restore is unchanged and still applies when there is no explicit navigation.
- **Delegate targets bound by a submodule import or a function-local import resolved to nothing.** Three gaps compounded on the shape that actually occurs — a receiver imported inside a function body and then called by attribute. `from pkg import name` bound `name` to the package that merely contains it rather than to the submodule, even when `pkg/name.py` exists on disk, and the relative form (`from . import name`) went the same way. An attribute call could not resolve from that binding at all. And an import declared inside a function body never bound. All three now work: the submodule edge is added beside the package dependency edge rather than replacing it; a namespace receiver makes the attribute a module-level function while a named import makes it a member of the imported symbol; `delegates_to` gets a per-function overlay that shadows the enclosing scope the way Python does; and `depends_on` gets a flattened whole-file union, so a deferred import counts as a module dependency whether or not the file carries annotations.
- **A delegate call routed through a re-export shim resolved to a module that defines nothing.** A scanner sees one file at a time, so a call that reaches its target through a package `__init__` re-exporting it had no way to find where the symbol is actually defined. The build now walks the re-export relation outward from the guessed module *after* the upserts, when the index holds every node and every marker: breadth-first, depth-bounded, cycle-guarded, with the smallest module id winning a tie so a name exported by two sources resolves the same way on every build. The symbol table is read from the index, never from this build's scan output — an incremental build only scans changed files, so a table derived from the scan would resolve on full rebuilds and flap on every other one. Retargeting mints a new edge and rewrites the build's edge list, so the reconciliation pass below sees the corrected targets as this build's intended set and retires the superseded rows. A build that still has unresolved delegate links but finds an empty re-export relation warns and names the full-rescan remedy.
- **Retargeting an `AutoStep` left the old `delegates_to` edge behind, and consumers disagreed about which one was real.** The edge table was append-only for scanner-derived edges, and an edge's identity includes its target — so changing which function a step delegates to minted a new row and orphaned the old one. The step node updated in place while its edge set only ever grew. Consumers assume at most one such edge and broke the tie differently: the viz took the first row, the workflows API the last. The Workflows tab and MCP `workflow_detail` could therefore name **different** delegate targets for the same step, and either could name a function the code had stopped calling. A build now diffs each walked source's stored delegate edges against what the scan intended and deletes the difference, generalising the shipped `documents` reconciler off its hardcoded edge type. Scoping matches the build's other per-file passes — only sources this build actually read participate, and a file whose scan failed is left alone — and a one-line notice reports leftovers that scoping cannot reach.
- **`Step` and `AutoStep` markers declared inside a loop or `try` body were silently dropped.** The statement-list walker deduplicated the bodies it yielded with a visited set keyed on `id(body)`. Every list it pushes is a fresh copy, so two pushes can never denote the same list and the guard was incapable of a true positive — it could only fire when CPython recycled the address of an already-yielded list, skipping a body it had never seen. Whole-integer steps live in the function's top-level body, which is yielded first and was never affected; minor steps live in loop and `try` bodies, which are exactly the recycled allocations. So every `Step` / `AutoStep` declared inside a loop was lost. Emission for these markers was always implemented and intended — `parse_step_num`, `_step_num_from_call` and `step_id_for` all handle non-integer tails — so nothing needed building beyond removing the guard. On this repository a build now emits 17 step nodes where it emitted 12, with none lost.
- **Renaming a document lost verification for every one of its sections.** `record_doc_rename` had explicit loops migrating section history and section edges, but its `node_verification` update was parent-only — so a rename carried the document envelope's verification across and left every section behind. On this repository 598 of 760 verified doc nodes are sections; all of them would have resurfaced as never-verified. The underlying cause was ordering: the foreign key onto `nodes(id)` silently dropped rows written before the new identity existed, which cost even the parent's verification. `rekey_doc_identity` now materialises the new `nodes` rows first. Link rewriting is prefix-aware in the same pass, so section-targeted `links[].node_id` entries move with their document — previously the rewrite matched `node_id == old_id` exactly and was only ever called with the parent id, missing roughly 80% of doc-targeted links — and the batch form walks the doc tree once per run instead of once per rename.
- **Workflow step lists no longer show steps that were deleted from the source.** Previously, renumbering a workflow's steps, removing a marker, or moving an annotated function to another module left the old step entries in the index forever. They showed up in workflow views and the visualiser as if they were real — sometimes colliding with genuine steps that had taken over their numbering. A build now reconciles the steps it has recorded for each file it reads against what that file actually contains, and removes the ones the source no longer justifies.
- **Only files a build actually reads are affected.** A file skipped by the unchanged-file fast pass, or one a scanner failed to parse, is left completely alone. Nothing is removed on the basis of a file the build did not open.
- **Builds now report when they remove something.** Removing index entries during a normal build is new, so a build that does it says so in one line, and stays silent when it does not.
- **`axiom-graph init` now tells you what it actually costs.** Its confirmation prompt previously said only that re-initialising resets baselines and clears staleness signals. It deletes and rebuilds the index database, so verification records and change history go too. The prompt now says so. The command's behaviour has not changed — only its warning, which was understating the consequence.

### Changed

- **The duplicate-doc-id build warning now scans the whole tree instead of only the files the build read.** 2.2.0 shipped this warning scoped to a single build's walked file set, so a collision against a file the mtime fast-pass skipped went unreported — which, on an incremental build, is most files. It is now computed from a whole-tree enumeration performed directly from disk, independent of the fast pass, so the warning no longer depends on which files happened to change. The enumeration admits only real DocJSON documents, so ordinary data JSON sitting beside your docs can neither raise a warning nor block the migration gate. A second advisory names DocJSON files whose stem contains a dot, since those dots are indistinguishable from directory separators in the derived id. Neither signal alters a derived id.
- **`axiom-graph doc-ids execute` is present but should not be run yet.** It is the write half of the migration tooling above and is fully implemented — it re-runs the collision gate immediately before writing, copies the database to a timestamped backup and echoes the path, then migrates in one transaction, aborting the run and restoring rather than leaving a half-migrated index. It is nonetheless **not usable in this release**, because the derivation still produces the current id form: any build after a migration re-creates exactly the identities the migration retired. Nothing in this release guards against that. Run `doc-ids preview` freely; leave `execute` alone until the derivation change ships, which is when it becomes a required upgrade step rather than an optional one. There is no per-document revert path in either case — rollback means restoring the backup.

### Deprecated

- **Semantic search remains deprecated and scheduled for removal in 3.0.0.** Unchanged from 2.1.0 and restated here because the deprecation window has now been open across three releases. `get_embedder()` (`axiom_graph/index/embeddings.py`) and `init_embeddings()` (`axiom_graph/db/embeddings.py`) emit `DeprecationWarning`; the `[semantic]` and `[semantic-torch]` extras, the `mode='semantic'` parameter on `axiom_graph_search`, and the viz / CLI toggles all continue to work through the 2.x line. See **ADR-020**. Migration: use keyword search (`mode='keyword'`, the default — FTS5-backed) or layer external semantic tooling against the exported SQLite DB.

### Upgrading an existing index

Two things change on the first build after upgrading. Neither needs action before you upgrade.

#### New `BROKEN_LINK` findings on workflows

Delegate links are checked for the first time in this release, so targets that have always been dangling surface all at once rather than gradually. The usual causes are a delegate target that was never annotated, one renamed or moved without its caller being updated, and — most often on an index built before this release — a step left behind by a renumbering, which the same build now also reaps.

These are real findings about real dead ends, not noise, but the first batch reflects accumulated history rather than anything you just did. Read them once, fix or annotate the targets worth fixing, and the count stabilises. Findings are reported on the `workflow` / `task` envelope that composes the step, so that is where to look.

#### Orphaned step entries

**Steps deleted before this release are still in your index.** The fix applies to each file the moment that file is next scanned, so entries left behind by earlier refactors persist until something causes their file to be re-read. If you upgrade and still see steps that are not in your source, this is why.

You have two options, depending on how thorough you want to be. Both work today — you do not need to wait for a future release.

#### Option 1 — clear the ones the build tells you about

Run a build. If orphaned step entries remain, it prints one line naming the files that carry them. Touch or re-save those files, then build again:

```
touch path/to/file.py
axiom-graph build .
```

This is enough for most projects.

#### Option 2 — clear everything, including entries the build cannot name

The notice above finds orphaned entries whose enclosing function is gone. It cannot name entries whose function still exists — for example, a workflow that was renumbered so its old step numbers were left behind. Those are real, and on an index built before this release there may be a few.

**You do not need to find them. You need to re-read every file.** Reconciliation happens per file and covers every file the build reads, so if the build reads everything, everything is reconciled. Update the timestamp on all your source files, then build:

```
# macOS / Linux
find . -name '*.py' -exec touch {} +
axiom-graph build .
```

```
# Windows PowerShell
Get-ChildItem -Recurse -Filter *.py | ForEach-Object { $_.LastWriteTime = Get-Date }
axiom-graph build .
```

Adjust the file pattern to the languages you index. The cost is one slow build — every file is parsed instead of skipped. Your baselines, verification records and staleness signals are untouched: `axiom-graph build` only adds newly-discovered nodes and never resets existing ones.

#### Do not use `axiom-graph init` for this

`init` re-reads every file, so it looks like the right tool. It is not. **`init` deletes your index database and rebuilds it from scratch** — every baseline, every verification record, and your entire change history are lost. It asks for confirmation first, and as of this release that prompt says plainly what is lost. Use `init` to set up a new project, never to clean up an existing one.

Similarly, `build --purge` does not help here — it removes entries whose *file* has disappeared, which is not what an orphaned step entry is.

#### After that, it maintains itself

You only have to do this once. Every change that removes a step marker is itself an edit to the file holding that step's entries, so the next build re-reads that file and clears them automatically.

Two situations still leave entries behind, and the same remedy applies to both: a file a scanner cannot parse (a build already warns about these separately, and the entries clear once it parses again), and a file restored with an *older* timestamp than the index recorded — from a checkout, revert, or backup restore — which the fast pass skips. If you ever suspect entries are stale after restoring files, touch them and rebuild.

## [2.2.0] - 2026-07-24

Doc-graph release. DocJSON sections become first-class envelope-pattern graph nodes (ADR-021) via an automatic, data-preserving in-place schema migration, and a new scoped `axiom_graph_reverify` MCP tool clears a verified source's LINKED_STALE cascade in one call. Multi-root `docs_dirs` projects gain the tooling they were missing: authoring into any configured root, seeing which root each doc came from, and a warning when two roots claim one doc id. Plus honest `mark_clean` reporting on aggregates, the end of the two-table drift bug class, same-file AutoStep delegation in the Python scanner, and two viz doc-viewer/search fixes.

### Added

- **`axiom_graph_write_doc(docs_root=...)` targets any configured docs root.** New docs could only ever be created under `docs_dirs[0]`; a doc destined for a secondary root (e.g. `.pev/`) had to be hand-written as raw JSON and picked up by a later build. Pass `docs_root` to pick the destination — it must match an entry of `[axiom_graph.scan].docs_dirs` (compared as POSIX paths, so `.pev`, `./.pev`, and `.pev/` are equivalent), and an unknown value is a hard error that lists the configured roots without writing anything. Omitting it keeps the previous behavior exactly. Doc-id derivation is unchanged, so a doc written this way gets the same id a full build derives for that file. Updating existing docs in any root (`update_section` / `patch_section` / `add_section`) already worked and is untouched.
- **Build warns when two docs roots derive the same doc id.** Doc ids flatten every root into one `docs.` namespace, so `docs/x.json` and `.pev/x.json` both resolve to `{project_id}::docs.x` and silently overwrite each other — last scanned wins. The docs scan loop now tracks id → source file across roots and appends a warning naming both files and the shared id. A warning, not an error: existing projects keep building. Only files actually walked in a given build participate, so mtime-skipped files don't produce spurious pairs.
- **New `axiom_graph_reverify(node_id)` MCP tool.** Verify a node and clear the LINKED_STALE it caused in one operation: expands composite sources (doc envelopes, modules, sections with children) to their subtree, resolves transitive doc-to-doc chains back to their root offender, conservatively skips nodes that are also stale via other offenders (reported with the blocking offender IDs), and finishes with a staleness recompute plus a before/after report — so aggregates visibly clear in the same call. Cascade-cleared nodes carry `[reverify:<source>]` provenance in their history/verification rows, keeping them distinguishable from individually reviewed verifications.
- **Same-file AutoStep delegation resolution.** The Python module scanner now runs a `local_func_ids` pre-pass, so an `AutoStep` marker's `delegates_to` resolves functions defined in the same module — including forward references to functions defined later in the file — instead of only cross-module targets.

### Changed

- **⚠️ Breaking (schema): DocJSON sections are now first-class graph nodes** (envelope pattern, ADR-021): each section is a real node, section nesting is queryable via `composes` edges like workflows and state machines, and the separate `doc_sections` store is gone. **Breaking schema change with automatic, data-preserving in-place migration** — run your normal `build` after upgrading; history, verification baselines, and renames are preserved and a backup is written first.
- **MCP doc tools unchanged** — `read_doc`, `update_section`, `patch_section`, and friends behave identically; only the underlying storage moved.
- **Doc listings show which root each doc lives under.** `read_doc("list")` appends the file path (`{project_id}::docs.test-policy  Project Test Policy  [.pev/test-policy.json]`), and `axiom_graph_list` now appends the location for doc, doc-envelope, and doc-section rows the way it already did for functions. Previously nothing in any listing distinguished a doc in a secondary root from one in `docs/` — the truth was only visible in the DB `file_path` column, which led at least one session to conclude a configured root was not being indexed at all. This is the display half only — doc rows also became reachable by the `location` / `location_glob` **filters**, listed under **Fixed**. Code-node rows are unchanged.

### Fixed

- **`mark_clean` no longer reports false success on aggregates.** Marking a doc envelope, module, or section-with-children clean when its LINKED_STALE is inherited from descendants now returns an explicit "inherited — no direct effect" result naming the stale descendants to clean (MCP and CLI), instead of a success message that changed nothing. Mixed nodes (own stale signal AND stale descendants) report "own signal cleared; inherited remains". Ordinary own-signal nodes keep the exact same plain-success output.
- **Two-table drift bug class eliminated**: purged doc sections no longer resurrect as `NOT_FOUND`, `drift_query` now reaches doc-quality signals (e.g. `DOC_SECTION_LONG`), and the shadow-row sync machinery is retired.
- **Build prunes doc sections that vanished from their file.** A section removed from a DocJSON outside the section tools — a raw editor edit, a bulk find-replace, a `write_doc` overwrite that dropped it — used to survive in the index indefinitely. Nothing flagged it, since the discovery build had no doc-section diff, so it sat at `VERIFIED`; and `purge_node` refused to remove it, because that tool's precondition only accepts `NOT_FOUND`. Ghost rows accumulated with no supported way to clear them, and `read_doc` kept serving them as real content. The docs scan now diffs each freshly-scanned doc's section rows against the file and cascade-deletes the ones that are gone, recording a preserved `DELETED` tombstone. Sections inside mtime-skipped files are excluded, matching the `documents`-edge reconciler's scoping.
- **Doc sections are reachable by the location filters.** `axiom_graph_list(location=…)` matches on `level_3_location` and `drift_query(location_glob=…)` on `COALESCE(level_3_location, location)`. The retired shadow-row insert wrote doc-section rows with `level_3_location` NULL and a synthesized `location` (`docs/{id-tail}.json`) that was wrong for any doc in a subdirectory — so `list(location="pev-requests")` returned nothing across a whole tree of doc files, and nested doc sections were unreachable by glob. Sections are scanner-minted nodes now and carry the real file path, so both filters reach them. Doc-root rows always carried the correct path and are unchanged.
- **Viz: doc viewer no longer jumps to the top on every edit.** The doc viewer rebuilt its scroll container via `innerHTML` on each re-render, so editing or opening a section, changing a heading/slug, toggling collapse, or editing tags/links reset the viewport to the top. The scroll offset is now captured before the rebuild and reapplied after; a fresh doc load still opens at the top.
- **Viz: keyword search is client-side substring matching (uncapped, name-first).** Keyword search previously round-tripped to the server's whole-token, ranked, top-50 FTS endpoint, so partial names like `env` → `store_env_content` could rank past the result cap and never appear. It now filters the already-loaded node set in the browser with substring matching; name (title/id/summary) matches sort ahead of body-text matches. Semantic mode still queries the server, where the embeddings live.

## [2.1.1] - 2026-06-14

Viz-focused release. The "changed since" history view is reworked from event-log replay to a **net state-diff**, gaining deleted-source recovery, change-kind badges with a kind filter, and a fail-loud index-behind banner. Backend route handlers and frontend only — no MCP or CLI signature changes.

### Added

- **Deleted-source recovery for "ghost" nodes.** A node deleted since the baseline SHA now retains its `level_3_location` span and originating `git_sha`, so the viz can fetch and display its source as it existed before deletion (`recover_deleted_source`). Deleted ghosts are selectable in the source panel; `/api/history/since` carries the recovered source in an expanded `deleted_nodes` shape (`level_3_location`, `recovered_source`).
- **Change-kind badges and kind filter in the history view.** Each changed row shows a change-kind badge, and a kind filter lets you slice the "changed since" set by kind. The net change-kinds are active; the link kind is disabled/deferred pending ADR-021.
- **Index-behind banner — fail loud on an un-indexed "changed since" SHA.** Choosing a baseline SHA that isn't in the index now raises a clear error and surfaces an index-behind banner, instead of silently computing against a stale or partial index.

### Fixed

- **`/api/source` route restored.** The path-based source endpoint (`GET /api/source?path=…`) that backs the workflow- and test-view source panels was dropped during the viz `server.py` router split and never re-added, so those panels 404'd. Restored into the workflows router with its directory-traversal guard intact, with a regression test.

### Changed

- **"Changed since" computes a net state-diff instead of replaying the event log.** Membership now reflects the net difference between the baseline SHA and the current index, so a node that was edited and then reverted back to its baseline state no longer appears as changed. The keystone `compute_net_diff` derives membership from `get_name_status_changes` plus a `node_hashes_for_blob` baseline-blob-vs-stored-hash classification; blob hashing is **non-destructive** (it never mutates stored node hashes). `/api/history/since` now returns a `change_kinds` map and the change-kind vocabulary.

## [2.1.0] - 2026-06-10

Feature + maintenance release: new MCP tooling (`axiom_graph_drift_query`, `axiom_graph_patch_section`), an XState v5 state-machine scanner, configurable multi-target consumer rendering, full removal of the legacy dFlow package, and a cluster of staleness-correctness fixes. Two minor breaking changes to MCP tool signatures (`axiom_graph_check` and `mark_clean` / `purge_node`) — see **Changed** and **Migration notes**.

### Added

- **XState v5 state-machine scanner.** A new scanner recognizes XState v5 `createMachine` definitions and emits state-machine envelope nodes — states, transitions, and `after` / `always` delays — into `graph.db`, surfaced through `axiom_graph_workflow_list` and `axiom_graph_workflow_detail` alongside `@workflow` / `@task` annotations. Non-literal transition targets (an identifier used as an `after.{delay}` value, or `always: [identifier, …]` array elements) raise an `X1` IMPORTANT finding instead of being silently skipped.
- **`axiom_graph_drift_query` MCP tool** — a paginated, filtered, optionally aggregated view of the staleness inventory. Supports `filter`, `location_glob`, `group_by ∈ {status, location_prefix, feature}`, `format ∈ {full, ids, counts}`, and offset/limit pagination. Replaces the `axiom_graph_sql` aggregate pattern previously needed to slice large drift inventories.
- **`axiom_graph_patch_section` MCP tool** — surgical section edits that do not require re-sending the whole section body: `append`, `prepend`, or `replace` a unique matched substring. Complements `axiom_graph_update_section` (full-section replace).
- **Configurable multi-target consumer rendering.** `render-site` is generalized from one implicit Sphinx site to N declared render targets via `[[axiom_graph.site.targets]]`, each with its own output path and format — **plain GFM** (Markdown link lists; contentless folders emit no stub) or **Sphinx**. Adds a single-doc→file renderer, a hybrid output manifest (co-located for subtrees, central for single-file outputs), and `--target` (CLI) / `targets` (API + MCP) plumbing. This is what regenerates `README.md`, `userdocs/guide/`, and the PEV plugin docs from their DocJSON source. Guide output is byte-identical when no targets are configured.

### Fixed

- **`axiom_graph_build` now reconciles `documents` edges against DocJSON `links` as the source of truth.** Previously, external edits to DocJSON files (raw `Edit`, bulk find-replace, manual JSON edits) left orphan `documents` edges in the DB even after rebuild, causing spurious `BROKEN_LINK` flags that could only be cleared with manual SQL `DELETE`. The build pass now deletes any DB `documents` edge whose target is no longer in the section's `links` array — including the case where the array is emptied entirely. `LINK_REMOVED` history rows are emitted for each orphan removed, tagged with `actor: "build:reconcile"`. Scope is strictly `documents` edges; other edge types are unchanged. Sections inside mtime-skipped files are not affected — reconciliation only runs on freshly-scanned sections.
- **`AXIOM_GRAPH_SKIP_EMBEDDINGS=1` now also gates the MCP server startup warm-up.** The flag previously only suppressed embedding generation at build time (`axiom_graph/index/builder.py`); the parallel pre-load thread in `axiom_graph/mcp/server.py::_warm_embedder` ran unconditionally on every server start, blocking on HuggingFace cache hydration on Windows symlink-degraded machines and causing MCP transport hangs for users who never opted into `[semantic]` / `[semantic-torch]`. The flag now honors both call sites.
- **`LINKED_STALE` is sticky — only `mark_clean` clears it.** A regression had ordinary edits auto-clearing the transitive linked-stale flag; transitive staleness now persists until an explicit `mark_clean`. Relatedly, `get_stale_doc_sections` now joins on `doc_sections.updated_at` rather than the frozen-nodes shadow, so stale doc sections are detected reliably.
- **`mark_clean` no longer advances `file_mtime`,** so a clean stops freezing node summaries on the next scan.
- **`frozen_tags` threaded through `build_index` and the viz server,** so frozen / reference-point state is honored consistently across indexing and visualization.
- **JS/TS nodes are now hashed during staleness computation,** so they stop flapping to `NOT_FOUND` on every rebuild.
- **`axiom_graph_drift_query` grouped output is bounded** (a conditional default plus real pagination) so large groupings paginate instead of dumping every path.
- **Rendering robustness:** headingless lead sections render correctly, and a corrupted central render manifest is now detected, logged, and reset (ADR-014) instead of failing the build.

### Deprecated

- **Semantic search** is deprecated and scheduled for removal in 3.0.0. Calling `get_embedder()` (`axiom_graph/index/embeddings.py`) or `init_embeddings()` (`axiom_graph/db/embeddings.py`) now emits `DeprecationWarning`. The `[semantic]` and `[semantic-torch]` Poetry extras, the `mode='semantic'` parameter on `axiom_graph_search`, and the corresponding viz/CLI toggles all continue to work through the 2.x line. See **ADR-020**. Migration: use keyword search (`mode='keyword'`, the default — FTS5-backed) or layer external semantic tooling against the exported SQLite DB.

### Changed

- **⚠️ Breaking (MCP): `axiom_graph_check` slimmed.** The `verbose` and `filter` parameters were removed; `check` now returns the staleness summary only. Migrate filtered or verbose queries to `axiom_graph_drift_query`: `check(verbose=True, filter=F)` → `drift_query(filter=F)`. MCP clients still passing the removed parameters get a `TypeError`.
- **⚠️ Breaking (MCP): `mark_clean` / `purge_node` signatures reordered.** `reason` now precedes `node_id` positionally, and `node_id` is optional (defaults to `""`) so batch `node_ids` callers need not supply it. Call these tools with keyword arguments; MCP clients passing named JSON arguments are unaffected.
- **Consumer docs site migrated MkDocs → MyST/Sphinx.** The rendered site under `userdocs/` is now built with Sphinx + myst-parser + furo + sphinxcontrib-mermaid + sphinx-click, and the consumer-docs source is folder-defined (nested DocJSON plus a slim `site-nav.yml`). MkDocs is retired.
- **ADR-005 Phase 5 — absorbed `pev-agent-nexus` into the monorepo.** Plugin sources copied to `pev_nexus_agents/pev/` and `pev_nexus_agents/hook-spike/`; marketplace registry placed at `.claude-plugin/marketplace.json`. New install URL: `/plugin marketplace add ddpoe/axiom-graph`; install IDs `pev@axiom-graph`, `hook-spike@axiom-graph`. Both plugins reset to **1.0.0** in the new marketplace (was `pev` v3.0.1 + `hook-spike` v0.2.0 in `pev-agent-nexus`) — content unchanged, fresh version namespace for the new marketplace. Old `ddpoe/pev-agent-nexus` repo is being archived with a redirect README. Plugin tags use prefixed form (`pev-v*`, `hook-spike-v*`) to keep release cadences independent of `axiom-graph-v*` releases. The published wheel does not bundle `pev_nexus_agents/` (verified — `pyproject.toml` `packages = [{include = "axiom_graph"}]` only).
- **Path-filtered CI.** `axiom-graph CI` now triggers only on `axiom_graph/**`, `axiom-annotations/**`, `tests/**`, `docs/**`, `pyproject.toml`, `poetry.lock`, and config files. New `plugins CI` workflow validates `marketplace.json` / `plugin.json` / `hooks.json`, cross-checks marketplace ↔ plugin versions, and shellchecks hook scripts; triggers only on `pev_nexus_agents/**` and `.claude-plugin/**`.

### Removed

- **Legacy dFlow decorator package removed** in favor of `axiom-annotations` plus the unified `graph.db`. The `from dflow.core.decorators import …` import path, the dead `[axiom_graph.dflow]` config table, and the `.dflow/` working directory are all gone; decorators now come from `axiom_annotations`, and all workflow / step metadata lives in `graph.db`. Schema-level identifiers (the `dflow_meta` column and the ontology `dflow_mapping`) are intentionally retained for a separate schema-rename effort.

### Migration notes

Upgrading from 2.0.x:

1. **`axiom_graph_check` callers** — drop the `verbose` / `filter` arguments and use `axiom_graph_drift_query(filter=…)` for filtered or aggregated staleness views.
2. **`mark_clean` / `purge_node` callers** — pass `reason` and `node_id` as keyword arguments (the positional order changed).
3. **dFlow decorator imports** — any remaining `from dflow.core.decorators import …` must become `from axiom_annotations import …`; remove the `[axiom_graph.dflow]` config table and `.dflow/` directory if still present.

## [2.0.0] - 2026-04-28

Major release: package rename, multi-language layout, structured annotation layer, internal directory restructure. Multiple breaking changes — read the migration notes before upgrading.

### Breaking changes

- **Package renamed `cortex` → `axiom-graph`.** The PyPI distribution is now `axiom-graph`; the importable Python package is `axiom_graph`. All `import cortex` and `from cortex import …` lines must be updated.
- **CLI renamed `cortex` → `axiom-graph`.** The `cortex` console script no longer exists. Use `axiom-graph <subcommand>` or `python -m axiom_graph.cli`.
- **MCP tool prefix `cortex_*` → `axiom_graph_*`.** All 29 tool names changed; e.g. `cortex_search` → `axiom_graph_search`, `cortex_check` → `axiom_graph_check`. Existing `.mcp.json` configs continue to work (transport unchanged) but tool calls inside agents must use the new names.
- **Config file renamed `cortex.toml` → `axiom-graph.toml`** with the top-level table renamed `[cortex]` → `[axiom_graph]`. All sub-tables follow (e.g. `[cortex.scan]` → `[axiom_graph.scan]`). The legacy file is no longer read.
- **Default DB path `.cortex/index.db` → `.axiom_graph/graph.db`.** Both directory names are still in the built-in scan-exclusion set for back-compat, but new builds write to `.axiom_graph/`.
- **Annotation imports moved.** `from dflow.core.decorators import workflow, task, Step, AutoStep` → `from axiom_annotations import workflow, task, Step, AutoStep`. The new `axiom-annotations` package ships separately on PyPI.
- **`[axiom_graph.dflow]` config section removed.** dFlow integration is built into the scanner pipeline; there is no `enabled` toggle and no separate `workflow.db`. Workflow and step metadata live in the main `graph.db` as envelope nodes connected via `composes` and `delegates_to` edges.
- **`cortex.api` module renamed to `axiom_graph.api`** (covers `workflow_list` and `workflow_detail` added in 1.0.6 / 1.0.7).
- **`cortex_ontology.yaml` renamed to `ontology.yaml`** (still ships inside the wheel under `axiom_graph/`).

### Added

- **axiom-annotations layer** (Phase 3): structured `@workflow` / `@task` envelope nodes, `Step()` / `AutoStep()` markers as child nodes, `annotates` and `composes` edges, plus Pass A/B validation rules (A1–A3, B1–B4, C1) selectable via `[axiom_graph.validation.rules]`.
- **axiom-annotations JS/TS package** — sibling port of the Python annotations package, shipped from `axiom-annotations/axiom_annotations_js/` (separate PyPI/npm releases). Provides the same `workflow(opts)(fn)` / `task(opts)(fn)` HOF wrappers and `Step()` / `AutoStep()` markers for JS/TS code.
- **Multi-language repo layout** under `axiom-annotations/`: `axiom_annotations_py/` (Python) and `axiom_annotations_js/` (JS/TS).
- **Configurable paths**: `[axiom_graph.scan]` now accepts `docs_dirs`, `config_dirs`, and `[axiom_graph]` accepts `db_path` for non-default project layouts. See `docs/consumer/configuration.json`.
- **MCP `axiom_graph_workflow_list` and `axiom_graph_workflow_detail`** for AI agents to enumerate and inspect dFlow-annotated workflow/task functions (originally landed in 1.0.6 / 1.0.7, retained under the new naming).
- **MCP `axiom_graph_write_doc` rejects node-id form for `id`** (validates that callers pass a path slug, not a fully-qualified node ID).
- **ADR-005 Phase 4** internal restructure for maintainability: `axiom_graph/viz/` split into `nodes`, `docs`, `workflows` routers + `_core`; `axiom_graph/cli/` split into `indexing`, `rendering`, `inspection`, `_core`; new `axiom_graph/docjson/` (`parse`, `render_consumer`), `axiom_graph/workflows/` (`api`, `validation`, `mcp_tools`), `axiom_graph/db/` and `axiom_graph/mcp/` subpackages. Backwards-compat shims preserved for the old single-file imports.

### Removed

- The `[cortex.dflow]` config section and the standalone `workflow.db` SQLite file. dFlow data now lives entirely in `graph.db`.
- Legacy `cortex_*` MCP tool names (no shim — agents must use `axiom_graph_*`).
- The standalone `cortex` console script.

### Fixed

- `axiom_graph_write_doc` now validates the `id` field shape, rejecting fully-qualified node IDs at write time instead of producing a malformed graph node.
- 27 NOT_FOUND artifacts left over from earlier rename passes (see `073c3b7`).
- Phase 4 Task 6 followup: preserve observability tests by inlining `run()` in the `mcp_server.py` shim.

### Migration notes

If you are upgrading from 1.0.x:

1. **Update imports** — `from cortex import X` → `from axiom_graph import X`. Search-and-replace is safe; no public-API surface area changed apart from the namespace.
2. **Rename your config file** — `mv cortex.toml axiom-graph.toml`. Then rewrite the top-level table: `[cortex]` → `[axiom_graph]` (and every `[cortex.subtable]` accordingly).
3. **Update annotation imports** — `from dflow.core.decorators import workflow, task, Step, AutoStep` → `from axiom_annotations import workflow, task, Step, AutoStep`. Install the `axiom-annotations` package alongside `axiom-graph`.
4. **Update CLI scripts and CI jobs** — replace `cortex <cmd>` with `axiom-graph <cmd>`.
5. **Update MCP-client tool calls** — replace any `cortex_*` tool names with `axiom_graph_*`. Server transport and arguments are unchanged.
6. **Discard any standalone `.dflow/workflow.db`** — it is no longer read; workflow data is in `graph.db`.
7. **Delete the old `.cortex/` index directory** if you don't need its history; rebuild with `axiom-graph init` to populate `.axiom_graph/graph.db`.

### Known issues

- `axiom_graph_check` does **not** flag `BROKEN_LINK` for `documents` edges that point at missing **doc** nodes (only missing **code** nodes are caught). Consumer-tagged docs without any outbound `documents` edges are also exempt from the transitive-staleness mesh. Tracked in `docs/pev-requests/broken-link-doc-targets-and-coverage.json`.
- Many consumer-facing internal strings (browser title, viz `sessionStorage` keys, viz HTTP API field names like `cortex_node_id`) still carry the legacy "cortex" prefix. These are deferred to a follow-up cycle to avoid breaking saved UI state and any external integrations against the viz HTTP API.

## [1.0.7] - unreleased (rolled into 2.0.0)

### Added
- `cortex.api.workflow_detail` structured Python API.

## [1.0.6] - prior release

### Added
- `cortex.api.workflow_list` structured Python API.

## [1.0.5] - prior release

Last release under the `cortex` package name. See git history before commit `73314df` for earlier changes.
