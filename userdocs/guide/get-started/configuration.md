<!-- generated from axiom_graph::docs.consumer.get-started.configuration @ 13d8315f1f5f; do not edit -->

# Configuration

## Overview

axiom-graph is configured through a single `axiom-graph.toml` file in your project root. Every section and key is optional: missing values fall back to sensible defaults, and if the file is absent entirely axiom-graph runs with all defaults. You only add the keys you want to change.

The file uses [TOML](https://toml.io/) syntax, and all settings live under the `[axiom_graph]` table (sub-tables like `[axiom_graph.scan]` group related keys). A small number of behaviors -- logging in particular -- are controlled by environment variables instead; see [Environment Variables](#environment-variables) below.

Most of what you configure here shapes one thing: which files become nodes in the mesh, and how staleness flows across the edges between them. If you have not yet connected an agent, start with [Connect your agent](connect-your-agent.md); the CLI commands referenced throughout this page are documented in [Use the CLI](use-the-cli.md).

## What gets indexed: the [axiom_graph.scan] section

The `[axiom_graph.scan]` section decides which files axiom-graph discovers and turns into nodes. The fewer irrelevant files in the mesh, the less an agent has to wade through later -- scanning configuration is the first lever on context reduction.

```toml
[axiom_graph.scan]
docs_dirs   = ["docs", ".pev"]
config_dirs = [".claude"]
test_paths  = ["tests/"]
js_paths    = ["axiom_graph/viz/static/ts/*.ts"]
exclude_dirs = ["worktrees", "pev-worktrees"]
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `docs_dirs` | list of strings | `["docs"]` | Directories scanned for Markdown and DocJSON files. The first entry is the primary write target for new docs. |
| `config_dirs` | list of strings | `[".claude"]` | Directories scanned for agent/config artifacts (settings, commands, skills). |
| `test_paths` | list of strings | `[]` | Path prefixes that contain test code, e.g. `["tests/"]`. Lets the viz dashboard filter test functions out of the Workflows tab. |
| `js_paths` | list of strings | `[]` | Glob patterns for JS/TS files to scan. Empty means no JS/TS scanning. Requires the JS/TS extra (see below). |
| `exclude_dirs` | list of strings | `[]` | Bare directory names to skip, **in addition to** the built-in set below. |

### Built-in exclusions

axiom-graph ships with a built-in set of directories it always skips — version-control, virtualenv, build-output, cache, and worktree directories. The authoritative list lives in code (`_BASE_SKIP_DIRS` in `axiom_graph/index/builder.py`), not here, so this guide doesn't duplicate it. Your `exclude_dirs` entries **add to** that set; they never replace it. Use `exclude_dirs` for project-specific data folders, scratch notebooks, or generated trees that would otherwise pollute the graph.

### Multi-root docs and config

Because `docs_dirs` and `config_dirs` are lists, projects that scatter docs and config across several top-level directories do not have to consolidate them. axiom-graph scans each entry independently. Node IDs derive from each path's form *relative to its own root*, so a file at `.pev/cycles/foo.json` becomes `myproject::pev.cycles.foo`, not `myproject::docs.pev.cycles.foo`. Relative paths resolve against the project root; absolute paths are honored as-is.

### JavaScript / TypeScript scanning

By default only Python is scanned. Set `js_paths` to glob patterns to also index `.js`, `.ts`, `.jsx`, and `.tsx` files; this requires the JS/TS optional extra, which installs tree-sitter:

```bash
pip install "axiom-graph[js]"
```

The JS/TS scanner is what extends the semantic layer beyond Python: it picks up `workflow(opts)(fn)` / `task(opts)(fn)` envelopes and their `Step` / `AutoStep` markers, and -- as proof the semantic layer is framework-aware -- recognizes xstate v5 state machines (`createMachine`, `setup().createMachine`) as state/transition nodes. See [Annotations](../concepts/annotations.md) for what those markers mean.

## Project identity and database path

Two top-level keys live directly under `[axiom_graph]`:

```toml
[axiom_graph]
project_id = "my-project"
db_path = ".axiom_graph/graph.db"
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `project_id` | string | directory name | Namespace prefix on every node ID (the part before `::`). Equivalent to the `--id` CLI flag. When omitted, the project directory name is used. |
| `db_path` | string | `".axiom_graph/graph.db"` | Location of the indexed mesh -- the SQLite database that `build` writes and every read tool queries. Relative paths resolve against the project root; absolute paths are honored as-is. |

`project_id` is worth setting explicitly: it is baked into every node ID, so changing it later re-namespaces the whole graph. `db_path` rarely needs to change, but you can point it elsewhere if `.axiom_graph/` is inconvenient for your layout.

## Staleness propagation: the [axiom_graph.staleness] section

Staleness is not a bolt-on feature -- it is a read on the same typed mesh that powers intent-scoped retrieval. Drift detection and "give me exactly the linked nodes" are two queries against one graph. The `[axiom_graph.staleness]` section tunes how a drift signal travels along the edges. For the full model, see [Staleness](../concepts/staleness.md).

```toml
[axiom_graph.staleness]
transitive_tags = ["consumer"]
frozen_tags = ["adr", "plan", "pev-cycle", "pev-instance", "pev-request"]
```

### transitive_tags -- let staleness flow doc-to-doc

By default, staleness is computed only for direct code-to-doc links: edit a function, the doc section that documents it goes `LINKED_STALE`. `transitive_tags` extends this one more hop, propagating the signal through doc-to-doc `documents` edges -- but only for documents carrying one of the listed tags.

This is exactly how the published docs you are reading stay honest. Consumer pages link *through* a dev-doc proxy rather than to raw code; because `"consumer"` is in `transitive_tags`, a consumer page inherits `LINKED_STALE` when the code beneath its proxy drifts. When this list is empty (the default), no transitive propagation occurs.

| Value | Type | Default | Effect |
|---|---|---|---|
| `transitive_tags` | list of strings | `[]` | Doc-level tags that opt **in** to transitive `LINKED_STALE` through `documents` edges. |

### frozen_tags -- exempt historical docs

Some documents are intentionally point-in-time and should *not* light up when the code they describe moves on: ADRs, planning docs, and PEV cycle/instance/request manifests record decisions as of a date. `frozen_tags` lists doc-level tags whose documents are excluded from `LINKED_STALE` -- both direct and transitive.

| Value | Type | Default | Effect |
|---|---|---|---|
| `frozen_tags` | list of strings | `[]` | Doc-level tags that opt **out** of `LINKED_STALE` propagation. |

With `frozen_tags` set:

- Frozen sections are dropped from the default `check` summary and from `drift_query` results.
- A `BROKEN_LINK` on a frozen doc still surfaces, annotated `[frozen-source]` -- a deleted target warrants human review regardless of freeze status.
- Any pre-existing `LINKED_STALE` on a frozen section is preserved (the sticky invariant); only `mark-clean` can clear it.

### Including frozen docs on demand

When you do want to see frozen rows, the staleness read tools take an `include_frozen` switch. Pass `include_frozen=true` to `axiom_graph_check` or `axiom_graph_drift_query` and frozen sections are included, marked `[frozen]` in full-format output. The depth of the read is otherwise unchanged -- this only widens *which* nodes are reported, not how far signals travel.

## Rename detection: the [axiom_graph.rename] section

When a node disappears from one build and a similar node appears, axiom-graph tries to recognize that as a *rename* (identity moved) rather than a delete-plus-create. Welding the old identity to the new one preserves history, verification snapshots, and edges -- so a symbol rename does not silently break every link pointing at it. The `[axiom_graph.rename]` section tunes the matcher.

```toml
[axiom_graph.rename]
code_threshold = 0.6   # min body-similarity ratio to auto-apply a code rename
prose_threshold = 0.5  # min ratio for prose (DocJSON) renames
pool_cap = 50          # max scoped-pool size before exact-hash fallback
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `code_threshold` | float | `0.6` | Minimum body-similarity ratio (0-1) for a lost code node to auto-weld to a newly-appeared one. |
| `prose_threshold` | float | `0.5` | Minimum ratio for prose/DocJSON renames. |
| `pool_cap` | integer | `50` | Maximum number of candidate nodes in a scoped comparison pool. Above this, the matcher falls back to an exact-hash-only pass. |

Lower thresholds catch more renames but risk false welds; higher thresholds are conservative. The `pool_cap` keeps similarity scoring bounded -- when a build touches a large number of nodes at once, exact-hash matching still catches cross-file moves without an expensive all-pairs comparison. If the matcher ever guesses wrong, the welds it makes are not permanent: review them with the rename CLI/MCP tools and undo with `rename revert`. See [Use the CLI](use-the-cli.md) for the `rename apply` / `rename revert` commands.

## Annotation validation

One more section governs the semantic layer -- the annotated orchestration highways (`@workflow` / `@task` envelopes with `Step` / `AutoStep` markers). These are not a full call graph; they are the spots you deliberately annotate so an agent can read intent and step names instead of tracing every call. See [Annotations](../concepts/annotations.md).

### Validation rules

At scan time, axiom-graph runs eight static rules (A1-A3, B1-B4, C1) over your annotations -- checking that step numbers are well-formed, sequential, and so on. Findings flow into the `build` summary and `check --format json`. You can toggle the whole pass or individual rules:

```toml
[axiom_graph.validation]
enabled = true

[axiom_graph.validation.rules]
A1 = true
A2 = true
A3 = true
B1 = true
B2 = true
B3 = true
B4 = true
C1 = true
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `validation.enabled` | boolean | `true` | Master switch. When `false`, all annotation findings are suppressed. |
| `validation.rules.<ID>` | boolean | `true` | Per-rule toggle. An unknown rule ID raises a config error rather than being silently ignored. |

The rules, briefly: **A1** step_num is a positive int/float; **A2** `Step(name, purpose)` both non-empty; **A3** `AutoStep` arg shape valid; **B1** no duplicate step_num in an envelope; **B2** major step numbers form 1, 2, 3 with no gaps; **B3** non-integer step_num must sit inside a loop; **B4** `AutoStep` must be immediately followed by a call to a decorated function; **C1** `@workflow` / `@task` `purpose` is non-empty.

## Size and age thresholds: [axiom_graph.thresholds]

These thresholds drive advisory flags during builds and the age-based staleness clock.

```toml
[axiom_graph.thresholds]
max_function_lines = 80
max_module_lines   = 600
stale_days         = 90
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `max_function_lines` | integer | `80` | A function longer than this is flagged as oversized during builds. |
| `max_module_lines` | integer | `600` | A module longer than this is flagged as oversized during builds. |
| `stale_days` | integer | `90` | An unverified node older than this many days is considered stale. |

The line limits nudge toward atomic, function-granular code -- which keeps each node small enough that reading one source range, not a whole file, is genuinely useful.

## Publishing the docs site: [axiom_graph.site]

The `[axiom_graph.site]` section controls the `axiom-graph render-site` pipeline, which converts DocJSON documents into clean Markdown for a static site generator.

```toml
[axiom_graph.site]
nav_file   = "site-nav.yml"
output_dir = "site"
```

| Key | Type | Default | Purpose |
|---|---|---|---|
| `nav_file` | string | `"site-nav.yml"` | YAML file defining the site navigation and which docs to include. Resolved relative to the project root. Used as the implicit `guide` target when no `[[axiom_graph.site.targets]]` are declared. |
| `output_dir` | string | `"site"` | Output directory for the implicit render when no targets are declared. |

Both can be overridden per invocation with `render-site`'s `--nav` and `--output` flags.

## Configurable render targets: [[axiom_graph.site.targets]]

Declare one or more named render targets. When any targets are present, `nav_file` / `output_dir` are ignored for the default run and only the explicit targets apply. Use `--target NAME` (repeatable) to render a subset.

```toml
[[axiom_graph.site.targets]]
name   = "guide"
output = "userdocs/guide"
format = "sphinx"
nav    = "site-nav.yml"

[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
format    = "plain"
doc       = "myproject::docs.consumer.readme"
overwrite = true

[[axiom_graph.site.targets]]
name      = "plugin-pev"
output    = "pev_nexus_agents/pev/docs"
format    = "plain"
nav       = "docs/consumer/plugins/pev/nav.yml"
overwrite = true
```

| Key | Type | Required | Purpose |
|---|---|---|---|
| `name` | string | yes | Unique target identifier used by `--target` / `only=`. |
| `output` | string | yes | Output path relative to project root. For `nav` targets: a directory. For `doc` targets: a file. |
| `format` | string | `"plain"` | `"plain"` (GitHub-friendly GFM) or `"sphinx"` (MyST/toctree). Only nav targets may use `"sphinx"`; doc targets must use `"plain"`. |
| `nav` | string | one of nav/doc | Path to a `site-nav.yml` relative to project root. Mutually exclusive with `doc`. |
| `doc` | string | one of nav/doc | A single DocJSON doc-id to render to `output`. Mutually exclusive with `nav`. |
| `overwrite` | bool | `false` | When `true`, an existing un-stamped file at `output` is replaced; otherwise it is warn-and-skipped (path safety). Set `true` for the first run when replacing hand-authored files. |

**Validation rules (enforced at config load):** exactly one of `nav`/`doc` must be set; `format` must be `"plain"` or `"sphinx"`; `sphinx` requires `nav` (never `doc`); target names must be unique.

**Implicit target synthesis:** when `targets` is empty or absent, a synthetic `guide` target is created pointing `nav_file` → `userdocs/guide` with `format="sphinx"` — preserving the pre-targets default behavior.

**Hybrid manifest:** subtree (`nav`) targets keep their co-located `.render-manifest.json` in the output directory (unchanged). Single-file (`doc`) targets are recorded in `.axiom_graph/render-manifest.json` keyed by repo-relative output path. Subset runs (`--target NAME`) merge their entries into the central manifest without erasing other targets' entries.

For a worked, runnable walkthrough — declaring README, plugin-docs, and guide targets and rendering each — see [the multi-target rendering tutorial](../examples/multi-target-rendering.md).

This section is the publishing end of the docs-honesty loop. Consumer/published docs are themselves DocJSON nodes in the mesh; they link through a [dev-doc proxy](../examples/docs-honesty-loop.md#the-proxy-linking-architecture) to the code, and — because `"consumer"` is listed in `transitive_tags` — they inherit `LINKED_STALE` when that code drifts. That staleness is the signal to update the prose; verifying or `mark-clean`-ing the section clears it; `render-site` republishes the corrected page. The site you are reading is built this way (axiom-graph dogfooding its own loop). For the full walkthrough, see [the docs-honesty loop](../examples/docs-honesty-loop.md).

## Environment Variables

Logging is controlled by environment variables rather than the TOML file. These apply to both the CLI and the MCP server (the primary integration surface for agents).

| Variable | Default | Description |
|---|---|---|
| `AXIOM_GRAPH_LOG_LEVEL` | `INFO` | Override the log level. Accepts standard Python levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. At `DEBUG`, SQLite trace logging is also enabled. |
| `AXIOM_GRAPH_LOG_FILE` | (none) | When set, log output is also written to this file path (in addition to stderr). Applies to the MCP server only. |

When an MCP tool call behaves unexpectedly, setting `AXIOM_GRAPH_LOG_LEVEL=DEBUG` (and optionally `AXIOM_GRAPH_LOG_FILE`) is the fastest way to see what the server is doing.

## Complete Example

A full `axiom-graph.toml` with every section. All keys are optional -- include only the ones you want to change from their defaults.

```toml
[axiom_graph]
project_id = "my-project"
db_path = ".axiom_graph/graph.db"

[axiom_graph.scan]
docs_dirs   = ["docs", ".pev"]
config_dirs = [".claude"]
test_paths  = ["tests/"]
js_paths    = ["src/**/*.ts"]
exclude_dirs = ["worktrees", "pev-worktrees"]

[axiom_graph.thresholds]
max_function_lines = 80
max_module_lines   = 600
stale_days         = 90

[axiom_graph.staleness]
transitive_tags = ["consumer"]
frozen_tags = ["adr", "plan", "pev-cycle", "pev-instance", "pev-request"]

[axiom_graph.rename]
code_threshold = 0.6
prose_threshold = 0.5
pool_cap = 50

[axiom_graph.validation]
enabled = true

[axiom_graph.validation.rules]
A1 = true
A2 = true
A3 = true
B1 = true
B2 = true
B3 = true
B4 = true
C1 = true

# Configurable render targets. With no --target, render-site regenerates all
# targets; --target NAME renders a subset. overwrite = true lets the first run
# replace hand-authored files; subsequent runs see the provenance stamp and
# regenerate cleanly.
[[axiom_graph.site.targets]]
name   = "guide"
output = "userdocs/guide"
format = "sphinx"
nav    = "site-nav.yml"

[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
format    = "plain"
doc       = "my-project::docs.consumer.readme"
overwrite = true

[[axiom_graph.site.targets]]
name      = "plugin-docs"
output    = "plugins/myplugin/docs"
format    = "plain"
nav       = "docs/consumer/plugins/myplugin/nav.yml"
overwrite = true
```

> Note: a `[semantic]` install extra and embeddings-based search existed in earlier versions. Semantic search is deprecated (2.1.0) and removed in 3.0 -- there is no embeddings extra or config to set.
>
> Note: omitting `[[axiom_graph.site.targets]]` entirely preserves the pre-targets default behavior: a synthetic `guide` sphinx target is created from `nav_file` → `userdocs/guide`.
