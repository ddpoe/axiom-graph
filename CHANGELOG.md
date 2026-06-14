# Changelog

All notable changes to axiom-graph are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Tag scopes.** This repo is now a monorepo. Tags are prefixed by component:
> `axiom-graph-v*` — `axiom_graph` Python package (this CHANGELOG); `pev-v*` and `hook-spike-v*` —
> Claude Code plugins under `pev_nexus_agents/` (see `pev_nexus_agents/CHANGELOG.md`).

## [Unreleased]

_Nothing yet — entries accumulate here for the next release._

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
