<!-- generated from axiom_graph::docs/consumer/get-started/configuration @ 74bc00ade0e0; do not edit -->

# Configuration

## Overview

axiom-graph reads its settings from `axiom-graph.toml` in the project root. Every key is optional: a key you leave out takes its default, and with no file at all axiom-graph runs on defaults. All keys sit under the `[axiom_graph]` table and its sub-tables, such as `[axiom_graph.scan]`.

A misspelled key is ignored without a warning, so check names against the tables below. A bad value in `[axiom_graph.validation.rules]`, `[axiom_graph.docjson]`, `[axiom_graph.staleness]` (`refresh_before_read`) or a render target stops every command with an error that names it.

Logging is set with environment variables instead; see [Environment variables](#environment-variables). The commands named on this page are covered in [Use the CLI](use-the-cli.md).

## Project id and database: [axiom_graph]

Two keys sit directly under `[axiom_graph]`.

```toml
[axiom_graph]
project_id = "my-project"
db_path    = ".axiom_graph/graph.db"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `project_id` | string | the existing index's id, else the project directory name | Prefix of every node id, the part before `::`. |
| `db_path` | string | `".axiom_graph/graph.db"` | Where the index database is stored. A relative path resolves against the project root. |

`axiom-graph init` writes `project_id` for you: the `--id` you pass, else the existing index's id, else the directory name. The index records the id it was built with; if `project_id` later names a different id, `build` stops with an error that names both. To change the id, set the new `project_id` and run `axiom-graph init`, which rebuilds the index and discards its history and verification records. `axiom-graph init` leaves every other setting in the file alone. To return the whole file to the defaults, keeping `project_id`, run `axiom-graph init . --settings`; it lists what changes and asks first, and `axiom-graph build` then applies the new settings.

## What gets indexed: [axiom_graph.scan]

Every `.py` file under the project root is scanned. `[axiom_graph.scan]` adds docs, config files and JS/TS, and names directories to skip.

```toml
[axiom_graph.scan]
docs_dirs    = ["docs", ".pev"]
config_dirs  = [".claude"]
test_paths   = ["tests/"]
js_paths     = ["web/src/**/*.ts"]
exclude_dirs = ["data"]
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `docs_dirs` | list of strings | `["docs"]` | Directories read for Markdown and DocJSON docs, relative to the project root. New docs are written to the first entry. |
| `docs_extensions` | list of strings | `[".docjson", ".json"]` | Extensions read as DocJSON under `docs_dirs`. Only `.docjson` and `.json` are valid; other entries are dropped with a build warning. New docs get the first entry's extension. |
| `config_dirs` | list of strings | `[".claude"]` | Directories read for agent config files (Markdown, JSON, YAML and TOML). Each file becomes one node. |
| `test_paths` | list of strings | `[]` | Path prefixes that hold tests. `axiom_graph_workflow_list` leaves them out unless you pass `scope="tests"` or `scope="all"`, the dashboard hides them until you tick **Show test files**, and `axiom_graph_drift_query(group_by="node_kind")` counts the code under them as `test`. |
| `js_paths` | list of strings | `[]` | Glob patterns, relative to the project root, for `.js`, `.jsx`, `.ts` and `.tsx` files to scan. Empty means no JS/TS scanning. |
| `exclude_dirs` | list of strings | `[]` | Directory names (not paths) to skip when scanning code and `config_dirs`. A name matches at any depth. |
| `source_roots` | list of strings | `[]` | Directories, relative to the project root, that Python imports start from. Needed when your code lives somewhere other than the project root, such as `lib/` or `app/`, and axiom-graph cannot work it out. A single string is accepted as one root. |

A `docs_dirs` or `config_dirs` entry that does not exist gives a build warning.

### Python import roots

An import such as `from demo.stats import mean` is linked to the file it names by trying a list of directories in order. The first one that holds the module wins:

1. the directories in `source_roots`;
2. pytest's `pythonpath` setting, from the first pytest config file found (`pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini` or `setup.cfg`);
3. the package directories your packaging config declares (setuptools, poetry or hatch in `pyproject.toml`, or `setup.cfg`);
4. the project root;
5. `src/`, when it holds Python code.

So a project with a `src/` layout works without any setting. Entries that do not exist or lie outside the project are ignored. A directory without an `__init__.py` still counts as a package when the import names a real `.py` file inside it. Relative imports (`from . import x`) always start from the importing file's own package.

When you add or change roots, the next build re-reads every Python file once so the links are found; nothing else is needed.

If imports of what looks like your own code still match no file, the build prints one line naming them and pointing at `source_roots`. Add the directory they start from, and the line goes away on the next build. Imports of packages you list as dependencies in `pyproject.toml`, and of the standard library, are never reported.

### Skipped directories

axiom-graph always skips `.git`, `.venv`, `venv`, `node_modules`, `.tox`, `.pixi`, `dist`, `build`, `__pycache__`, `worktrees` and its own `.axiom_graph` directory. `exclude_dirs` adds to that list. It does not apply inside `docs_dirs`, where every doc file is read.

### Several docs roots

Each `docs_dirs` entry is scanned on its own, and a doc id keeps its root's path: `.pev/test-policy.docjson` becomes `my-project::.pev/test-policy`, so it cannot clash with `docs/test-policy`. To write a new doc into a root other than the first, pass `docs_root=".pev"` to `axiom_graph_write_doc`. To convert existing `.json` docs to `.docjson`, use `axiom-graph doc-ids rename-extension`.

### JavaScript and TypeScript

Scanning `js_paths` needs the `js` extra:

```bash
pip install "axiom-graph[js]"
```

Without it, a build with `js_paths` set warns and skips those files. The scanner indexes functions and classes, the `workflow` / `task` / `Step` / `AutoStep` markers (see [Annotations](../concepts/annotations.md)), and xstate state machines built with `createMachine`.

## Staleness propagation: [axiom_graph.staleness]

Two lists decide which docs `LINKED_STALE` spreads into. A third key decides whether the read tools bring statuses up to date before they answer. The statuses and how to clear them are in [Staleness](../concepts/staleness.md).

```toml
[axiom_graph.staleness]
transitive_tags     = ["consumer"]
frozen_tags         = ["adr", "plan"]
refresh_before_read = "changed-files"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `transitive_tags` | list of strings | `[]` | Doc tags that let `LINKED_STALE` pass from one doc to another. |
| `frozen_tags` | list of strings | `[]` | Doc tags whose sections get no new `LINKED_STALE`. |
| `refresh_before_read` | string | `"changed-files"` | How current the statuses a read shows are: `"changed-files"`, `"check"` or `"off"`. See below. |

The two lists match the `tags` list at the top of a DocJSON file.

### transitive_tags

Without it, a section goes `LINKED_STALE` only when code it links to changes. With it, a section in a doc carrying a listed tag also goes `LINKED_STALE` when a doc section it links to is `LINKED_STALE`, and the signal carries on along further links from tagged docs. Use it for published pages that link to dev docs instead of code; [the docs-honesty loop](../examples/docs-honesty-loop.md) shows the setup.

### frozen_tags

Use it for point-in-time records such as ADRs and plans. Their sections get no new `LINKED_STALE`, and `check` (CLI and MCP) and `axiom_graph_drift_query` leave out both the sections and the frozen doc itself. A `BROKEN_LINK` on a frozen doc is still reported. Adding a tag does not clear `LINKED_STALE` a section already has while its cause remains; mark it clean as usual.

To see frozen sections, pass `include_frozen=true` to the `axiom_graph_check` or `axiom_graph_drift_query` MCP tool. Full-format rows mark them `[frozen]`.

### refresh_before_read

The MCP read tools `axiom_graph_drift_query`, `axiom_graph_read_doc`, `axiom_graph_graph`, `axiom_graph_search` and `axiom_graph_source`, and the dashboard, show statuses. This key sets what they do first, so a function you edited a moment ago can already read as changed without a `check`:

- `"changed-files"` (the default) re-checks the changed files behind what the read shows, then answers.
- `"check"` runs a full `check` of the project first, the same as `axiom-graph check`. Slower on a large project.
- `"off"` answers from the statuses the last `build` or `check` stored. When files changed since then, a read tool's answer ends with a line such as ``index is behind for 3 files — run `check` ``.

Any other value stops every command with an error that names the key.

## Rename detection: [axiom_graph.rename]

When a code node disappears in a build and a similar new one appears, the build records a rename: the new node takes over the old one's history, verification and links, and shows as `RENAMED` until you mark it clean.

```toml
[axiom_graph.rename]
code_threshold = 0.6
pool_cap       = 50
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `code_threshold` | float | `0.6` | Minimum body similarity, from 0 to 1, for an automatic rename. Lower values catch more renames and risk wrong matches. |
| `pool_cap` | integer | `50` | Most nodes (disappeared plus new) compared by similarity in one group. A larger group matches identical bodies only. |

An identical body matches anywhere in the project. Similar bodies are compared within the old node's file and the file git reports it was renamed to; without git, only identical bodies match. Undo a wrong rename with `axiom-graph rename revert`, and record a missed one with `axiom-graph rename apply`.

## Annotation checks: [axiom_graph.validation]

Each scan checks your annotations (`@workflow`, `@task`, `Step` and `AutoStep`; see [Annotations](../concepts/annotations.md)) against eight rules. Findings appear in the output of `axiom-graph build` and `axiom-graph check`, and `check --strict-annotations` exits 1 while any remain. Changes here apply on the next `build` or `check`, with no rescan.

```toml
[axiom_graph.validation]
enabled = true

[axiom_graph.validation.rules]
B3 = false
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | boolean | `true` | `false` turns every rule off. |
| `rules.<ID>` | boolean | `true` | `false` turns one rule off. An unknown rule id is an error. |

| Rule | Checks that |
|---|---|
| A1 | `step_num` is a positive int or float. |
| A2 | each `Step` has a non-empty `name` and `purpose`. |
| A3 | an `AutoStep` `name`, if given, is a non-empty string. |
| B1 | no two markers in one function share a `step_num`. |
| B2 | major step numbers run 1, 2, 3 with no gaps. |
| B3 | a fractional `step_num` such as `2.1` sits inside a `for` or `while` loop. |
| B4 | each `AutoStep` is followed directly by a call to a `@task` or `@workflow` function. |
| C1 | each `@workflow` and `@task` has a non-empty `purpose`. |

## Raw DocJSON edits: [axiom_graph.docjson]

One key sets what a build does with a DocJSON section edited outside the doc tools, by hand or by a script.

```toml
[axiom_graph.docjson]
raw_docjson_edits = "warn"
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `raw_docjson_edits` | string | `"warn"` | `"warn"` records a `RAW_DOCJSON_EDIT` history event for each such section and reports it after the build. `"off"` records nothing. Any other value is an error. |

A flagged section is never verified automatically. Accept its current text with `axiom-graph stamps accept <PROJECT_ROOT> --all` or `axiom_graph_accept_doc_edits`, or make the change again through a doc tool. With `"off"`, an edited section stays unverified until you mark it clean, like any other change. Editing docs is covered in [DocJSON](../concepts/docjson.md).

## Rendering docs: [axiom_graph.site]

`axiom-graph render-site` turns DocJSON docs into Markdown: a Sphinx guide, a README, or a folder of plain pages. Each `[[axiom_graph.site.targets]]` entry is one output. [The multi-target rendering tutorial](../examples/multi-target-rendering.md) walks through a full setup.

```toml
[[axiom_graph.site.targets]]
name   = "guide"
output = "userdocs/guide"
format = "sphinx"
nav    = "site-nav.yml"

[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
doc       = "my-project::docs/consumer/readme"
overwrite = true
```

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Unique target name, used by `render-site --target NAME`. |
| `output` | string | required | Output path inside the project: a directory for a `nav` target, a file for a `doc` target. |
| `nav` | string | none | Nav YAML file listing the docs to render as a page tree. |
| `doc` | string | none | Id of one doc to render to a single file. |
| `format` | string | `"plain"` | `"plain"` (GitHub-flavored Markdown) or `"sphinx"` (MyST pages with toctrees). Only a `nav` target can use `"sphinx"`. |
| `overwrite` | boolean | `false` | Replace an existing file at `output` that `render-site` did not generate. Without it, such a file is skipped with a warning. Files `render-site` generated are always replaced. |

Each target sets exactly one of `nav` or `doc`, and no two targets share a name.

`render-site` with no options renders every target; `--target NAME`, repeatable, renders only those. With no targets declared, it renders one `guide` target: the nav file below to `userdocs/guide`, in `sphinx` format.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `nav_file` | string | `"site-nav.yml"` | Nav file for the implicit `guide` target, and for `render-site --output` when `--nav` is not given. Set it under `[axiom_graph.site]`. |

`render-site --nav PATH` or `--output DIR` renders one Sphinx tree and ignores the target list.

## Environment variables

Logging is set with environment variables, not in `axiom-graph.toml`. Logs go to stderr.

| Variable | Default | Meaning |
|---|---|---|
| `AXIOM_GRAPH_LOG_LEVEL` | `INFO` | Log level for the CLI and the MCP server: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. `DEBUG` also logs every SQL statement. |
| `AXIOM_GRAPH_LOG_FILE` | none | MCP server only. Also writes logs to this file, which rotates at 5 MB and keeps two old files. |

To see what the MCP server is doing, set both in the server's environment; [Connect your agent](connect-your-agent.md) shows where.

## Complete example

An `axiom-graph.toml` that uses every table. Keep only the keys you change.

```toml
[axiom_graph]
project_id = "my-project"
db_path    = ".axiom_graph/graph.db"

[axiom_graph.scan]
docs_dirs       = ["docs", ".pev"]
docs_extensions = [".docjson", ".json"]
config_dirs     = [".claude"]
test_paths      = ["tests/"]
js_paths        = ["web/src/**/*.ts"]
exclude_dirs    = ["data", "scratch"]
source_roots    = ["lib"]

[axiom_graph.staleness]
transitive_tags     = ["consumer"]
frozen_tags         = ["adr", "plan"]
refresh_before_read = "changed-files"   # or "check", or "off"

[axiom_graph.rename]
code_threshold = 0.6
pool_cap       = 50

[axiom_graph.validation]
enabled = true

[axiom_graph.validation.rules]
B3 = false   # allow fractional step numbers outside loops

[axiom_graph.docjson]
raw_docjson_edits = "warn"

[[axiom_graph.site.targets]]
name   = "guide"
output = "userdocs/guide"
format = "sphinx"
nav    = "site-nav.yml"

[[axiom_graph.site.targets]]
name      = "readme"
output    = "README.md"
doc       = "my-project::docs/consumer/readme"
overwrite = true
```
