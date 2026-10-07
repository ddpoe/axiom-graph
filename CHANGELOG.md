# Changelog

All notable changes to axiom-graph are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Tag scopes.** This repo is now a monorepo. Tags are prefixed by component:
> `axiom-graph-v*` — `axiom_graph` Python package (this CHANGELOG); `pev-v*` and `hook-spike-v*` —
> Claude Code plugins under `pev_nexus_agents/` (see `pev_nexus_agents/pev/CHANGELOG.md`).

## [Unreleased]

## [3.0.0] - 2026-10-07

### Upgrading from 2.x

Run these in order. Details are in the entries below.

1. **Before installing 3.0:** commit your work, finish any PEV cycle, and stop the MCP server and the viz. Copy `.axiom_graph/graph.db` somewhere safe; this is your rollback, since 2.x can't open a 3.0 index. Make sure `axiom-graph.toml` has `project_id` under `[axiom_graph]` (the text before `::` in any node id): the `doc-ids` commands take the id from the toml or the folder name, not from the index.
2. **Upgrade the package, and don't build.** A build before step 3 refuses and names the commands to run.
3. **Migrate the doc ids:** `axiom-graph doc-ids preview .`, then `axiom-graph doc-ids execute .`. Commit the DocJSON files whose `links[]` it rewrote. Optionally run `axiom-graph doc-ids sweep .` (then `--apply`) for ids written in prose; the next build reports the sections it changed as raw DocJSON edits, which `axiom-graph stamps accept . --all` accepts.
4. **Optionally rename documents to `.docjson`:** `axiom-graph doc-ids rename-extension .`, then `--execute`.
5. **Build.** The first build also parses every file once to add the import edges src layouts and `source_roots` need; those edges mark nothing stale. It upgrades the schema to v5 (it keeps a `graph.db.pre-v5.bak` backup), records the version each currently verified doc and test was checked against, and rescans every code file once, which takes minutes on a large repo. The first `check` afterwards re-hashes every file once and may show CONTENT_UPDATED drift that an older index hid; later checks re-hash only the files that change. One kind of CONTENT_UPDATED there is not drift: 3.0 hashes `Step(...)` / `AutoStep(...)` marker text into a function's description hash instead of its code hash, so on an index built before that change every function that contains markers reads CONTENT_UPDATED once even when its source is unchanged, and a doc or test verified against the old hash reads LINKED_STALE through it. Review those functions and `mark_clean` / `reverify` them. Markdown sections that follow a fenced code block also read CONTENT_UPDATED once, because their text is now split correctly (see Fixed): `axiom-graph diff <ids> . --summary` shows which ones are unchanged.
6. **Restart the MCP server** (`/mcp` in Claude Code).
7. **Update PEV to 2.0.0** (`claude plugin update pev@axiom-graph`), only after step 3.
8. **Optionally, block raw edits** with `PEV_DOCJSON_GUARD=block` and `permissions.deny` rules for `Edit(**/*.docjson)` and `Write(**/*.docjson)`.

### Changed (breaking)

- **BREAKING — doc node ids are now derived from a document's path within its configured docs root, and upgrading requires a one-time migration.** A doc id used to flatten every configured docs root into a single `docs.` namespace and rewrite `/` to `.`, so `docs/adrs/013-x.json`, `docs/adrs.013-x.json`, and `.pev/adrs/013-x.json` could all derive one identity and the file scanned last silently won. Ids now carry the root and keep `/` as the joiner — `{project}::docs/adrs/013-x`, `{project}::.pev/adrs/013-x` — and a Markdown document retains its `.md` suffix (`{project}::docs/notes.md`) so that `x.md` and `x.json` in one directory stay two documents. Section ids are unchanged in shape: a document id still holds exactly one `::`, and a section still hangs off it by `::dot.path` (DocJSON) or `#slug` (Markdown).

  **The migration is a mandatory upgrade step, not an optional cleanup.** Run `axiom-graph doc-ids preview <root>` and then `axiom-graph doc-ids execute <root>`. Executing moves every document and section identity and carries its history, verification, graph edges, rename ledger, and on-disk `links[].node_id` references onto the new ids; it backs the database up first and reports where. It is irreversible — rolling back means restoring that backup — and a second run reports that there is nothing to do rather than repeating itself.

  **Order matters, and both wrong orders are destructive.** The safe window is *after* upgrading the package and *before* the next build. Building first re-derives every doc under the new rule while the index still holds the old ids: every document is inserted brand-new with no verification, history, or edges, and every old identity goes `NOT_FOUND`. Migrating and then rolling the package back resurrects the identities just retired. To protect against the first case, a build whose index holds documents under a namespace this version no longer derives now **refuses**, names `axiom-graph doc-ids preview` and `axiom-graph doc-ids execute`, and writes nothing at all — no node row, no `docs` row, no file. The refusal is a comparison between the index and the files on disk, so it clears permanently once the migration has run and never fires on a project with nothing indexed yet. Separately, a structurally unusable doc id in the index (missing or duplicated `::`, wrong project prefix) now produces a build warning and is skipped rather than stopping the build.

  **Prose is not rewritten by the migration.** Doc ids written into DocJSON section content, skills, templates, READMEs, and docstrings are inert text and go stale. `axiom-graph doc-ids sweep <root>` applies the same mapping to them: it is a dry run by default, skips and names any file carrying uncommitted changes so the whole run stays one `git checkout` from undo, leaves references naming no document exactly as written, and emits a review report of what it did to DocJSON prose. In a git repository it reads and writes only tracked files (`git ls-files`), so ignored directories, build output, untracked files and nested worktrees are never touched; `doc-ids preview`'s prose counts cover tracked files the same way. Outside a repository the sweep writes nothing.

- **BREAKING — a doc edit no longer clears LINKED_STALE.** `update_section`, `patch_section`, `add_section`, `write_doc`, `accept_doc_edits`, `update_doc_meta` and `delete_section` mark the text they write as reviewed (own status VERIFIED) and leave every link status as it was: the edited section's, its doc's, and every other node's. To clear a section, name the code it reconciles with the new `addresses=[node ids]` argument on `update_section` / `patch_section` (see Added), or call `mark_clean` / `reverify`. Agents and scripts that relied on an edit clearing LINKED_STALE must do one of those; PEV plugin 2.0.0 does. After a merge, the build no longer verifies a section that was edited while stale and never reconciled.

- **`axiom_graph_write_doc` reports the doc id it wrote.** Its result gains a `doc id` line naming the id the document was indexed under, so a caller that needs the id takes it from there instead of rebuilding it from the project id and path.

- **DocJSON documents may use the `.docjson` extension, and new documents are written as `.docjson`.** A DocJSON document can be `*.docjson` as well as `*.json`, and the build indexes both side by side.
  - **Ids are unchanged.** The extension never reaches a doc id: `docs/x.docjson` is `{project}::docs/x`, the same id as `docs/x.json`. Renaming a document from `.json` to `.docjson` with `git mv` and rebuilding keeps every document and section id, its history, verification and staleness, with nothing recorded as deleted or renamed.
  - **`axiom_graph_write_doc` creates new documents as `.docjson`** (breaking for callers that expect a `.json` file to appear). Overwriting a document that already exists as `.json` rewrites that file in place; a write never creates a sibling with the other extension. The viz create and import routes follow the same rule, and a move keeps the file's extension.
  - **New `[axiom_graph.scan] docs_extensions` key** lists the extensions the scanner reads. The default is `[".docjson", ".json"]`; only `.docjson` and `.json` are accepted, and any other entry is dropped with a build warning. The **first** entry is the extension new documents are written with, so `docs_extensions = [".json"]` keeps writing `.json` and never writes a file the scanner would skip.
  - **`x.json` beside `x.docjson` is a collision.** Both derive one id. The build warns, naming the id, both paths and the one it indexed, and the `doc-ids` migration gate reports it too. `.docjson` always wins, whatever the order of `docs_extensions`, unless only the `.json` file is a DocJSON document: a `.docjson` data file never hides a `.json` document.
  - **`render-site` folder landing** accepts `index.docjson` as well as `index.json`.

- **New `axiom-graph doc-ids rename-extension PROJECT_ROOT` converts a docs tree from `.json` to `.docjson`.** It renames only git-tracked files under the configured docs roots that classify as DocJSON documents; ordinary JSON data files are never touched, and untracked documents are listed but not renamed.
  - **Preview by default.** Without flags it lists the renames and the skipped files and writes nothing.
  - **`--execute`** renames with `git mv` after a confirmation prompt (`--yes` skips it), then re-indexes so the index's file paths match disk. Every document keeps its id, history and verification.
  - **Refusals.** `--execute` refuses, naming the cause, when `.docjson` is not in the configured `docs_extensions` (the renamed documents would not be scanned), when the project is not a git repository, when a document it would rename has uncommitted changes, when a `.docjson` target already exists, or when the index still needs `axiom-graph doc-ids execute`. Nothing is renamed in any of these cases.
  - **Order.** Run it after `doc-ids execute`, in the same upgrade session (see Migration).

- **BREAKING — `axiom_graph_drift_query` output and filters.** Callers that parse its output or pass globs need to adjust:
  - **Labelled rows.** `format="full"` rows now read `id=<node_id>  <own>/<link>  loc=<location>  via=...  root=...`, and the `#` column header changed to match. `format="ids"` is unchanged: it still prints bare ids you can paste into `mark_clean`.
  - **Real offenders.** On a LINKED_STALE row, `via` now lists the direct offenders from the computed staleness attribution, not every target whose own status isn't VERIFIED. A doc that is still stale through a function since re-verified now names that function instead of showing nothing. When a transitive chain's root offenders differ from its direct ones, a `root=` field names them; these are the same root offenders `axiom_graph_reverify` resolves. Both lists show up to 10 ids, then `(+N more)`. The old cap was 3.
  - **Labelled advisory rows.** With `filter="all"`, DOC_SECTION_LONG advisory rows are labelled `[DOC_SECTION_LONG]`. `filter="VERIFIED"` now raises `ValueError`.
  - **Path-aware `location_glob`.** `*` and `?` now stay within one path segment; use `**` to cross directories. `[abc]`, `[!abc]` and `{a,b}` work, and a malformed glob raises. A glob matches the path part of a `file.py#Lx-Ly` location, so `tests/*.py` now selects function and test nodes as well as modules.

- **BREAKING — `axiom_graph_read_doc` reads within a character budget, returns child sections, and marks its footers.**
  - **No more section table.** A whole-doc read over 3,000 characters used to return a table of section slugs. It now renders the doc up to `max_chars` (default 40,000; `None` disables the budget), stopping at a section boundary. A trailing hint lists the omitted section ids, ready to pass as `section_ids`.
  - **Children by default.** Reading a section, by `section` slug or by id, returns the section and every section nested under it. Previously it returned only the parent's own content.
  - **New `section_ids`.** Pass a list of fully-qualified section ids, from any number of docs, to read them in one call in the order given.
  - **Paging inside a section.** A single section larger than the budget is cut, and the hint names an `offset` to resume from (`section_ids=["<id>"], offset=N`).
  - **Marked footers.** The generated linked-nodes list is now wrapped in `<!-- axiom:linked-nodes -->` … `<!-- /axiom:linked-nodes -->`, so a copy pasted back into content is recognised and stripped on write.
  - **Paged `list`.** `doc_id="list"` takes `max_results`, `offset` and `prefix`, and ends with the next offset when more docs remain.
  - **Unchanged under the budget.** Output is byte-identical to the previous full render apart from the footer markers.

- **BREAKING — the project id sticks, and a build under a different id is refused.** `init --id <prefix>` used to record the id nowhere, so the next `build` without `--id` fell back to the directory name and silently indexed the whole project a second time, pairing the duplicates as RENAMED and leaving the originals NOT_FOUND.
  - **`init --id` writes `axiom-graph.toml`.** The id is recorded as `project_id` under `[axiom_graph]`: the file is created when absent, or the key is added with every other line kept. If the toml already holds a different `project_id`, `init` refuses before its prompt and changes nothing; edit the toml to change the id.
  - **The index stores its project id** (new `index_meta` table). A build resolves the id from `--id`, then the toml, then the stored id, then the directory name, so a project with no toml keeps its id without repeating `--id`. An empty `project_id = ""` or `--id ""` counts as unset.
  - **`init` without `--id` keeps the existing id.** Re-initialising reads the old index's id before deleting it and records it in the toml, instead of re-namespacing the project to the directory name.
  - **`init` always records the id.** Without `--id`, `init` records the toml's id, else the old index's id, else the directory name, so a project set up with a plain `init` has `project_id` in its toml from the start.
  - **`init` offers to reset a customised toml.** When `axiom-graph.toml` holds settings that differ from the defaults, `init` lists each one with its default and, run from a terminal, asks whether to reset the file before it deletes anything. The answer defaults to no; without a terminal nothing is reset. A reset keeps `project_id`.
  - **Every command uses the stored id.** Doc writes (`write_doc`, `clone_doc`), the doc-id migration and extension-rename plans, the prose sweep, the consumer site render and the viz resolve the id the same way: the toml, then the stored id, then the directory name. A project with no toml, or a worktree whose folder name differs from the project's, no longer gets doc ids under the folder name.
  - **A mismatch is refused.** When the resolved id differs from the stored one, `build` stops before scanning with `Error: ...` (exit 1) naming both ids and the fix; the `axiom_graph_build` MCP tool returns the same message as `ERROR: ...`. Renaming an existing index's project id is not supported; `init --id <new>` starts over under a new id, discarding the index's history.

### Removed (breaking)

- **Semantic search is gone.** The `[semantic]` and `[semantic-torch]` extras,
  and the `sqlite-vec`, `fastembed`, `onnxruntime` and `sentence-transformers`
  dependencies behind them, have been removed. Deprecated in 2.1.0 per ADR-020;
  the warning ran through 2.2.0, 2.3.0, 2.4.0 and 2.4.1, so anyone pinned to
  `[semantic]` had the full window. Search is now FTS5 keyword search only,
  which is what it already defaulted to.

- **The `mode` parameter is gone** from the `axiom_graph_search` MCP tool, from
  `axiom_graph.query.api.search_nodes`, and from the viz `/api/search` endpoint.
  This breaks callers that passed `mode` *at all*, including `mode="keyword"` —
  not just semantic users. Drop the argument; the behaviour you get is the
  behaviour `mode="keyword"` gave you.

- **The `AXIOM_GRAPH_SKIP_EMBEDDINGS` environment variable is gone.** It has no
  meaning now and is ignored if set.

### Migration

- **Existing indexes load unchanged.** The `vec_embeddings` and
  `embedding_hashes` tables become orphans; SQLite ignores them. Run `VACUUM`
  to reclaim the space if you want it back. No migration ships for them and
  none is needed.
- **Schema v3 rescans your test files on the first build.** The upgrade build
  clears the stored mtimes of Python test files (`test_*.py`, `*_test.py`) so
  they are scanned again under the new test rules (see Changed), and the old
  `validates` edges of helpers, fixtures and nested defs are retired. It
  writes no history or verification rows; a second build is a no-op. An older
  axiom-graph refuses a v3 index (`SchemaVersionError`).
- **Schema v4 stores annotation findings and rescans your code once.** Your
  next `build` or `check` upgrades the index in place, with no `init`: it adds
  the `annotation_findings` table and clears the stored mtimes of every Python
  and JS/TS file. History, verification and renames are untouched. The first
  build afterwards therefore rescans every code file once, which can take
  several minutes on a large repo (about 7 minutes on this one, mostly rename
  matching on a full rescan), and it reports every current annotation finding
  as new that one time. A `check` run before that build rescans everything in
  memory, so it is slow too and also reports every finding as new. An older
  axiom-graph refuses a v4 index on `build` (`SchemaVersionError`); restart a
  running MCP server after upgrading.
- **Schema v5 records what each verification was checked against.** Your
  next `build` upgrades the index in place, with no `init`, after taking a
  `graph.db.pre-v5.bak` backup. It adds the live-hash columns and the
  verification-targets table, and records the version of its targets each
  currently verified doc section and test was checked against. History,
  verification and renames are kept. The first `check` afterwards re-hashes
  every file once and may show CONTENT_UPDATED drift that an older index
  hid; later checks re-hash only the files that change. Verifications made
  before 3.0 whose target changed afterwards keep the time-based rule. An
  older axiom-graph refuses a v5 index (`SchemaVersionError`); restart a
  running MCP server after upgrading.
- **Your index records its project id on the first 3.0 build.** The `index_meta`
  table is added in place (no `init`, no schema-version bump). An existing
  index's id is the one prefix all its node ids share, so a project indexed
  with `--id` keeps that id even with no `project_id` in `axiom-graph.toml`
  and no `--id` on the upgrade build; a different `--id` or toml value is
  refused. Only an empty index, or one whose nodes already carry two
  prefixes, records the id that first build resolves.
- **Converting documents to `.docjson` is optional and comes after the doc-id migration.** Existing `.json` documents keep working. To convert, run `axiom-graph doc-ids rename-extension <root>` to preview, then again with `--execute`, *after* `axiom-graph doc-ids execute` has run; the command refuses while the index still needs the id migration. Commit or stash document edits first, because it refuses on uncommitted changes.
- **If you used `mode="semantic"`,** remove the argument. FTS5 keyword search
  handles the queries the documentation illustrated with semantic mode.
- **If you genuinely need vector retrieval,** the graph is portable SQLite:
  read the `nodes` table and apply your own embedding stack.

### Changed

- **`init` resets only the index.** It no longer asks about resetting `axiom-graph.toml`. Its delete confirm says what it keeps and names the reset flags. After its build it writes an editable `agent-policy` doc when the project has none.
- **`axiom_graph_reverify` returns a short report by default.** It leads with how many `LINKED_STALE` nodes were cleared, gives counts, and lists only the skipped dependents with what still holds them. Pass `verbose=true` for the full source, cleared and kept lists. "Cleared" now lists only nodes the call itself cleared, not parent docs that cleared as a result.
- **Merged doc edits arrive with their text verified.** When `build` finds a merged section whose tool-write stamp matches its text, it now always verifies the text, and verifies each link the stamp recorded at the version this checkout holds. Links the writer had not re-checked stay flagged as before. Previously a section edited while flagged for something unrelated arrived entirely unverified, and `mark_clean` was the only way out.
- **`carry-forward` carries each dimension separately.** A node the worktree verified, but which was still flagged there (or here) for another reason, now brings over its own-content verification and each link verification whose linked node matches. Main then shows what the worktree showed wherever the versions agree. The report counts full and partial carries, lists what each partial carry left open and why, and each carried node's history names the worktree's latest verification of it (a doc tool's write included).
- **`carry-forward --dry-run` prints the summary and the would-carry list.** The full per-verdict list now needs `--list` (MCP: `list_nodes=True`).
- **LINKED_STALE compares versions as well as times.** A verification now
  records the hash of every target the node depends on. A doc section or
  test is flagged when the code it depends on is at a version it was not
  checked against, so a second edit after a verification, and a revert past
  a verification, are both caught. A section updated with a doc tool before
  the build no longer goes LINKED_STALE when the build records the same
  change. Verifications made before 3.0, and links added after a
  verification, keep the time rule.
- **Docs that link a whole module are flagged when a function in it changes,
  or when a function is added or removed.** A pure rename of a function inside
  the module does not flag them.
- **A function marked clean and then reverted to its earlier content reads
  CONTENT_UPDATED instead of VERIFIED,** and its dependents are flagged. A
  function added after the first scan and removed again reads NOT_FOUND.
- **Routine operations cost what changed, not the size of your repo.** On
  this repo an idle `check` takes about 0.3 s instead of about 4.6 s, and a
  `build` with nothing changed about 0.6 s instead of about 7 s. The first
  check after an edit re-checks only what the edit can affect. A `build`
  records where it left off, so the next `check` is incremental.
- **Write tools leave the index current.** `mark_clean` (CLI, MCP and viz),
  `reverify`, the doc tools and `accept_doc_edits` update the statuses of the
  code they read before they return: a function you edited but haven't built
  shows CONTENT_UPDATED, and its tests and docs LINKED_STALE, straight away.
- **`build` re-parses a file when its content changed or its modification
  time moved.** A file restored from a backup with an old timestamp is now
  picked up; touching a file still forces a re-scan. The build summary's skip
  lines now say `content and mtime unchanged` instead of `mtime unchanged`.
- **Reads answer from a current index.** `drift_query`, `read_doc`, `graph`,
  `search`, `source` and the viz re-check changed files before answering (see
  `refresh_before_read` under Added). `read_doc`, `graph`, `search` and `source` tag a non-VERIFIED node
  with `[STATUS]`, and a batch `graph` or `source` call reports a missing
  index once.
- **Two INFO lines on stderr per staleness refresh.** `check`, `build`,
  `mark-clean`, the doc commands and `drift-query` log the refresh mode and
  why it was chosen, then what it did and how long it took.
- **Summary counts include hashless, childless composites** (an empty
  `__init__.py`, a config file) as VERIFIED.
- **A node flagged through several vias may list a different first via** than
  before; the set of vias is unchanged.
- **An incremental build detects the cross-file moves and in-file renames a
  full build detects,** including in JS/TS-only and config-only builds. A
  deletion-only build in a repo without git warns "similarity skipped", as a
  full build does.
- **In Python test files, only tests pytest collects own `validates` edges.**
  A collected test is a `test*` function at module level or a `test*` method
  of a class (not nested in a function, not a fixture). Nested defs, helpers
  and fixtures fold their calls into every test that uses them: a call, a
  helper chain (cycle-guarded), a `self.helper()` in the same class, or a
  fixture named as a parameter. A helper or fixture no test reaches gives its
  edges to the test module's node, which never makes anything stale. Helpers
  and fixtures are no longer reported as tests, so test counts drop. When a
  production function changes, the collected tests that reach it are flagged
  LINKED_STALE instead of helpers nobody verifies. Builds now also retire a
  `validates` edge a rescanned file no longer intends, as they already did
  for workflow delegate links.
- The indexer's build workflow no longer runs an embedding-generation step, and
  the MCP server no longer starts a background model warm-up thread — so first
  search and first build are no longer gated on loading an ONNX or Torch model.
- **The dashboard's search-mode control is gone.** The search box is keyword
  search, which is what the control selected by default. The viz `/api/meta`
  payload no longer carries its `embeddings` block, and `/api/search` no longer
  accepts a `mode` query parameter.
- `axiom-graph report` and `axiom_graph_report` now share one text renderer, so
  the MCP `full` output also shows the actor tag on link rows. The MCP summary
  is now two lines: the reference line, then the headline counts.
- Write tools strip a generated `**Linked nodes:**` footer from incoming
  content: a block marked `<!-- axiom:linked-nodes -->` …
  `<!-- /axiom:linked-nodes -->` anywhere, or an unmarked trailing list. Prose
  after an unmarked block and inline mentions are kept. Pasted `read_doc`
  output no longer accumulates in stored sections, and the result says when a
  footer was removed.
- **The CLI reports the doc-id namespace refusal as an error, not a
  traceback.** A build against an index that still needs the doc-id migration
  prints `Error: ...` and exits 1. The message now names both steps, in order,
  with the project root: `axiom-graph doc-ids preview <root>`, then
  `axiom-graph doc-ids execute <root>`. Any command that reaches the refusal
  reports it the same way.
- **Purge refuses a live module, doc or config node.** When a function or
  section is removed, its module or doc inherits `NOT_FOUND` while its file is
  still on disk. `axiom_graph_purge_node` used to purge such a node, deleting
  its history and verification until the next build re-created it bare. It
  now refuses it with an `ERROR:` that says the `NOT_FOUND` is inherited and
  lists the `NOT_FOUND` nodes in that file, to purge if they were really
  removed; once they are gone the node clears on the next check. When the
  file does not parse, the `ERROR:` says so, lists nothing, and says to fix
  the file and re-run check rather than purge its nodes (see the purge entry
  under [Unreleased] for nodes inside such a file). `axiom-graph purge` refuses the same
  nodes, and its `--all-not-found` keep rule now uses the same check.
  Composite inheritance is unchanged.
- **A purge records who ran it.** The `DELETED` history row of a purged node
  used to carry `actor: agent:pev-auditor` whoever purged it. It now carries
  `agent` from `axiom_graph_purge_node` and `human` from `axiom-graph purge`.
  The Python API `axiom_graph.lifecycle.api.purge_nodes` now takes the project
  root and a required keyword `actor` (no default):
  `purge_nodes(db_path, root, node_ids, reason, *, actor=...)`.
- **Bulk purges point at `axiom-graph purge --all-not-found`.**
  `axiom_graph_build`'s docstring used to send bulk purges to
  `axiom-graph build --purge`, which only re-runs the deleted-file pass every
  build already runs. It now names `axiom-graph purge --all-not-found`, and
  `build --purge`'s help says what the flag does. `build --purge` still works.
- **`check` no longer re-parses your whole tree.** The build now stores each
  file's annotation findings. `check` reads them and rescans in memory only
  the files edited since the last build; it never writes the store. An idle
  `check` on this repo went from 68-106 s to about 9 s (in a worktree, 12 s to
  7 s); what remains is the staleness recompute. `check` also walks exactly
  the files `build` walks: agent worktrees and configured `exclude_dirs` are
  skipped, and JS/TS and xstate findings now appear in it. If the findings
  store can't be read, `check` ends with a one-line
  `Error: could not read annotation findings: ...` instead of a traceback.
- **Annotation findings are reported once.** `build` and `check` print one
  line, `Annotation findings: N (X new, Y resolved)`, and list only the new
  findings; `check --all` lists every current finding. A finding's identity
  is its file, rule, function and message, never its line number, so a
  finding that only moved is not new. Because `check` never stores what it
  finds, a new finding shows as new on every `check` until the next `build`
  stores it; that build lists it once more. `check --format json` still
  returns the full `annotation_findings` list, now sorted by file and line
  with a `new` flag per entry, and `--strict-annotations` still gates on
  every current finding, so CI needs no change. A dotted DocJSON filename is
  advised on once, by the build that first sees it, instead of on every
  build. (finding-queue cli.d-29 and indexing.d-20, pulled forward from 3.x.)

### Added

- **New MCP tool `axiom_graph_info(project_root)`** reports one project's facts: its id, root, docs roots and extensions (and the one new docs get), frozen and transitive tags, db path with node and doc counts, and what a build scans and skips. It ends with the project's agent policy: the doc tagged `agent-policy`, or axiom-graph's shipped default when there is none. It is read-only and answers before the first build too.
- **`init --policy`, `init --settings` and `init --all`.** `--policy` restores the default agent-policy doc and `--settings` resets `axiom-graph.toml` to the defaults, keeping the project id. Each asks first (`--yes` answers for scripts) and never touches the index. `--all` resets the index, the settings and the policy behind one confirm.
- **`axiom-graph carry-forward <worktree>` and `axiom_graph_carry_forward`
  bring a merged worktree's verifications back.** Run in the main checkout after
  its build. Nodes stale there that the worktree verified, with the same
  content and every link at the version verified, take that verification and
  stop being stale (code, tests and docs alike). The report gives the number
  carried, the stale count before and after, how many carried nodes stay stale
  while a node they depend on is still stale (a doc-to-doc link, or a
  workflow's annotated function or delegated task), and why each other node
  was not carried. `--dry-run` writes nothing; it reads the statuses as of main's last
  build or check, so run it after the build. The worktree index is read, never
  written, and one with a different schema version or project id is refused.
- **`axiom_graph_clone_doc`** copies an indexed document to a new one,
  replacing the content of the sections you name (`set_sections`) and
  dropping others (`omit_sections`). An unknown section id is an error and
  writes nothing, so a template mismatch can't produce a document. Title,
  tags and extra top-level keys such as `meta` carry over unless you pass a
  new `title` or `tags`. The tool will not overwrite an existing doc or the
  source.
- **Batch section edits.** `axiom_graph_update_section` and
  `axiom_graph_patch_section` take `edits=[...]`, a list of edits across any
  number of docs, each with the single call's keys including `addresses` and
  `expected_hash`. Every item is checked before anything is written, so one
  bad item writes nothing; each file is then written and re-indexed once. The
  reply gives each item's `content_hash`.
- **`drift_query(group_by="node_kind")`** splits a drift list into `code`,
  `test` and `doc` in one call. `test` covers everything under the
  configured `scan.test_paths`, test helpers included.
- **`expected_hash` on `axiom_graph_write_doc`.** `write_doc` and
  `clone_doc` replies end with a `doc_hash` line; pass it back as
  `expected_hash` and `write_doc` refuses, writing nothing, if the file has
  changed since.
- **`addresses=[node ids]` on `update_section` and `patch_section`.** It names
  the offenders an edit reconciles and refreshes the section's record of the
  version it was checked against for each; the section clears once none is
  left. It can be passed alone, to reconcile without editing. The reply ends
  with `still LINKED_STALE via: …` while offenders remain. Naming a node that
  is not a current offender is an error and nothing is written; on a frozen
  doc's carried LINKED_STALE the error adds "(LINKED_STALE carried on a frozen
  doc; mark_clean clears it)".
- **`axiom-graph check --full` and `axiom_graph_check(full=true)`** force a
  full recompute. They give the same statuses as a plain check, only slower.
  The first check after an upgrade that changes how hashes or statuses are
  computed runs in full once by itself.
- **`[axiom_graph.staleness] refresh_before_read`** controls what
  `drift_query`, `read_doc`, `graph`, `search`, `source` and the viz do before
  answering:
  `"changed-files"` (the default) re-checks files that changed; `"off"`
  answers from the last snapshot and says "index is behind for N files — run
  `check`"; `"check"` runs a full check first. An invalid value is a config
  error.
- **Structural hints.** A check or refresh that finds functions or sections
  the index doesn't have yet says so, for example "`utils.py` has 2 new
  functions — run `build`" (JSON key `structural_changes`). It never adds them
  itself.
- **`axiom-graph diff` diffs a node against a baseline commit from the
  CLI.** `axiom-graph diff <node_id>... <root> [--baseline SHA] [--summary]`
  prints the same JSON as the `axiom_graph_diff` MCP tool, from a fresh
  process. Several nodes are separated by `---`; a node that can't be diffed
  prints its `{"error", "reason"}` in its slot, the rest still run, and the
  command exits 1.
- **`axiom-graph purge` removes NOT_FOUND nodes from the CLI.** It is the
  terminal counterpart of `axiom_graph_purge_node` and uses the same purge.
  Name the nodes (`axiom-graph purge <id>... <root>`), or pass
  `--all-not-found` to take every NOT_FOUND node after a confirmation prompt
  (`--yes` skips it). Only NOT_FOUND nodes are removed: a named node with any
  other status, or one not in the index, is refused and kept, and the command
  exits 1. A module, doc or config node whose file is still on disk is
  refused when named and kept by `--all-not-found`, because such a node is
  NOT_FOUND only by inheriting a removed child's status. `--reason` is
  recorded in each purged node's history, with actor `human`.

- **The MCP server tells agents how to use its tools.** On connect it sends
  an instructions block: why to prefer the tools over reading files, the tool
  families, and usage patterns (read a doc's outline before its sections,
  batch ids into one call, the doc-id grammar, write and staleness rules). It
  stays under 2,048 characters, the length Claude Code shows without
  truncating. A consumer's `CLAUDE.md` no longer needs its own tool overview.
- **New `axiom_graph_guide` tool.** It takes no arguments and returns the
  instructions block plus one line per tool. Subagents never receive server
  instructions, so they get the guidance by calling it.
- **Every tool's first docstring line now says what the tool is for**, in one
  line, since the guide quotes it. `drift_query`, `search`, `graph`, `check`,
  `checkout`, `render` and others were reworded.

- **Every report now starts with a reference line.** It says what the report
  was measured against and how that reference resolved: a checkpoint, a build
  row, the git commit time, a timestamp, a default fallback, or the whole
  history when there is no reference. JSON output carries the same information
  under a `reference` key. The line is printed even when the window is empty.
- `axiom-graph report --format condensed` and
  `axiom_graph_report(detail="condensed")` give a page-sized report. Hand-made
  (actor-authored) rows are printed as they are. Everything else is grouped by
  container: content changes per container, a staleness table with the top 10
  `via` containers, system link purges collapsed into one line, and
  verification split into containers verified directly and containers that
  appear only through cascade retirement.
- `axiom-graph report --exclude-node GLOB` (repeatable) and
  `axiom_graph_report(exclude_node_pattern=...)` (a string or a list) leave
  matching nodes out of every section and out of the headline counts.
- `axiom_graph_report(max_chars=40_000)` caps the MCP response. Longer output
  is cut at a line boundary, and a footer says how many lines were dropped and
  how to narrow the report. Pass `max_chars=None` to turn the cap off. The CLI
  is not capped.
- Tests are baselined at their first scan. A test function indexed for the first time gets a verification covering every code change seen up to and including that build, recorded as an `AGENT_VERIFIED` history event with op `scan_baseline` (it appears in `report`). Later changes to the code it validates still flag it; renamed, moved and re-scanned tests are unaffected. `BuildSummary.tests_baselined` lists them.
- DocJSON sections written by axiom-graph's doc tools now carry a tool-write stamp (`axiom_stamp`: a hash of the section's heading, content and links, plus the hashes of the code it links to). A tool-written section that arrives by `git merge` or `pull` is verified on the next build when its linked code is unchanged since it was written. A section edited outside the doc tools (a raw DocJSON edit) is indexed but never auto-verified; each edit is reported once, as one summary line naming both fixes, and recorded as a `RAW_DOCJSON_EDIT` history event that appears in `report` and `history`.
- New `axiom-graph stamps accept <project_root> <section-id>... | --all` (and `--list`) and MCP tool `axiom_graph_accept_doc_edits` (`dry_run=True` lists) accept raw DocJSON edits: they stamp the section and verify it. Re-applying the edit with any doc write tool also settles it, including when the text is identical.
- New config `[axiom_graph.docjson] raw_docjson_edits = "warn" | "off"` (default `"warn"`). `"off"` silences raw DocJSON edit detection; tool-written sections arriving by merge still verify.

- `update_section(expected_hash=...)` refuses a write based on an outdated read
  and returns the current content and hash. `update_section`,
  `patch_section`, `add_section` and `write_doc` results report each written
  section's `content_hash`.
- Batch forms: `add_section(sections=[...])` and
  `add_link(links=[{section_id, node_id}, ...])` (sections of one doc). Each
  call applies all items or none, with one write and one re-index.
  `add_section` now rejects an empty heading (single and batch form).
- `content_file` on `add_section`, `update_section` and `patch_section`, and
  `doc_file` on `write_doc`, for passing large content from a UTF-8 file under
  the project root or the system temp directory.
- `patch_section` results show the edited region: a fenced
  `edited region (lines A-B of N):` block with the inserted or replaced text
  plus up to 2 lines of context on each side (a region over 40 lines keeps its
  first and last 10 around an omitted-lines marker), and a
  `length: <chars> chars, <lines> lines` line next to `content_hash`. Text
  copied from the block matches as the next call's `old_string`, so an edit
  can be confirmed or chained without re-reading the section. What is written
  is unchanged.
- `axiom_graph_read_doc(outline=True)` lists a doc's section tree instead of
  its bodies. It works with any target `read_doc` takes (`doc_id`, `doc_ids`,
  `section`, `section_ids`) and prints:
  - a header line per doc: id, title, section count and total rendered size;
  - one line per section, indented by depth: full id, heading, the rendered
    size of its subtree (what a read of it spends against `max_chars`), the
    subsection count when non-zero, and `[STATUS]` when the node isn't
    VERIFIED.

  `max_chars` cuts the outline at a line boundary and the hint names the ids
  to outline next. `outline` with `doc_id="list"` or a non-zero `offset` is an
  `ERROR:`.
- `axiom_graph_read_doc` shows each doc's id, tags and file. A generated
  `<!-- doc: {id}  tags: a, b  file: docs/x.json -->` line follows every doc's
  `# title` (`tags: (none)` when untagged), and an outline's doc header ends
  with `[tags: …]`. Tags set with `update_doc_meta` can now be read back
  without opening the file. The line sits above every section, so section
  content and `offset` resumes are unaffected; the viz render is unchanged.
- `axiom_graph_reverify(node_ids=[...])` reverifies a batch of sources in one
  call, mirroring `mark_clean`'s `node_ids` (`node_id` is ignored when given;
  one shared `reason` and `verified_by`). The sources' subtrees form one source
  set, so a dependent held only by batch sources clears in this call whatever
  the order, where single calls would skip it until the last one. Dependents
  also stale via an offender outside the batch are still skipped, naming only
  that offender. The staleness recompute runs once, and one report lists the
  sources, cleared and skipped dependents, LINKED_STALE before and after, and
  unknown IDs under `Not found` (not fatal). Each cleared dependent's history
  names the batch source(s) that held it (`[reverify:<id>]` or
  `[reverify:<a>, <b>]`). The single `node_id` form is unchanged.
- **Export workflows without the dashboard: `axiom-graph workflows export`
  and the `axiom_graph_workflow_export` MCP tool.** Both write the page the
  dashboard's export button opens (the selected workflows' steps beside every
  source file they reach, in one self-contained HTML file) or, with
  `--format json` / `format="json"`, its JSON bundle. Select by function name
  or node id, mixing workflows and tasks, or by source file (`--file` /
  `files`). An id or file that matches nothing is refused by name and nothing
  is written. Neither needs the `viz` extra; without Pygments the code on the
  page is uncoloured. The CLI writes `workflow-export.html` in the current
  directory unless `-o` says otherwise; the MCP tool writes it in the project
  unless `output_path` does, and returns the path and counts, not the page.

### Fixed

- **Python `src/` layouts now link tests to the code they call.** Imports of project code resolve through the project's import roots: pytest `pythonpath`, setuptools / poetry / hatch packaging config, or an auto-detected `src/`. Namespace packages (directories without `__init__.py`) resolve too. Editing a function now flags its tests `LINKED_STALE` in a src layout, as the quick start shows. Node ids do not change.
- **New `[axiom_graph.scan] source_roots` setting** lists the import roots explicitly and always wins over detection.
- **Imports of your own code that can't be found are reported.** `build` prints one line with the count and points at `source_roots`, instead of silently treating them as third-party packages. Packages your `pyproject.toml` declares as dependencies are never reported, even when a copy of their source sits in your tree.
- **Purge never removes the nodes of a file that does not parse.** A syntax error in a Python or DocJSON file makes every function or section in it read `NOT_FOUND`; in a JS/TS file, tree-sitter drops the function the error is in, so that function reads `NOT_FOUND`. `axiom-graph purge --all-not-found` used to list them all for purging, deleting the history and verification of live code. It now keeps them and names the file once in its preview, saying to fix the file and re-run `check`. Naming such a node (`axiom-graph purge <id>`, `axiom_graph_purge_node`) is refused as `file_unparseable` with the same advice. Before, only the module or doc node was refused, and only when every node in the file read `NOT_FOUND`. Purge now parses the file instead of guessing from statuses. A file that parses with every function removed is no longer reported as not parsing: its module's refusal lists those functions to purge. A JS/TS file counts as not parsing when tree-sitter parses it with errors, or when tree-sitter is not installed. Purge parses each file once per run, before it deletes anything.
- **A `links` entry with no usable `node_id` is no longer accepted silently.** `write_doc` used to keep entries such as `{"target": "<id>", "type": "documents"}`, `{}`, `{"id": "<id>"}` or `{"node_id": ""}`. It reported `links registered : 0` and gave no error, so the section never got a link and never went `LINKED_STALE`. `write_doc` and `clone_doc` now refuse those entries. The `ERROR:` names the section's dot-path and the entry, and nothing is written. For an entry with no `node_id` key it also says to use `"node_id"` (every link is a `documents` link). `add_link` and `delete_link` refuse a section that already holds such an entry. Other keys beside a valid `node_id` are still accepted and ignored. A `links` value that is not a list, such as `{}`, is an error too; a missing value, `null` or `[]` still means no links.
- **One bad `links` entry in a hand-edited DocJSON file no longer drops the whole doc.** An entry such as `{"node_id": 123}` used to make the build skip the file with only a log line, and `check` then read every section `NOT_FOUND`. The build now skips just that entry and keeps the doc and its other links. It prints one warning per skipped entry naming the file, the section and the entry. A `.docjson` file that still can't be read (invalid JSON, a missing `title` or `sections`), or a `.json` file that is not valid JSON, is now named in a build warning. A `.json` data file kept beside the docs stays silent.
- **`write_doc` explains a malformed `links` entry.** A link given as a bare node-id string (`"links": ["proj::pkg.mod::fn"]`) is now accepted and saved as `{"node_id": ...}`. Any other shape returns `ERROR:` naming the accepted shapes, and nothing is written. It used to fail with a raw Python error. The tool description now shows the shape, and `add_link`, `delete_link` and rename link rewrites also accept a string link already in a file.
- **`diff` no longer shows an all-new old side for a node indexed before it was committed.** A history entry records the commit checked out at the time, which may not hold the node yet. When a stale node is missing from its default baseline, `diff` now compares against the newest commit that holds the node as it was before it went stale, and `baseline_reason` says so; a commit that already holds the edit is never used. When none of the file's last 20 commits qualifies, `diff` returns `no_baseline` with the reason.
- **`reverify` no longer verifies a dependent's own unreviewed change.** When a dependent it clears also has its own content or docstring change, built or not yet built, `reverify` now clears only the `LINKED_STALE` the source caused and leaves the dependent's own status as it was. The report counts these dependents as "own change kept".
- **`reverify`'s LINKED_STALE before and after counts now match `check`.** Statuses are refreshed first, frozen docs are left out the same way, and the report says so. A dependent whose other dependency was edited but not yet built is now skipped and reported, rather than cleared.
- **Sections an agent wrote on another branch are adopted even when an earlier build had already indexed their text.** Once a build has seen the merged text, the next `build` or `check` adopts the section. A merge no build has seen yet is adopted by the next `build`, or by the second `check` (the first only records the change). Hand-edited sections are still flagged as raw DocJSON edits.
- **Frozen docs no longer show up in the drift counts.** `check`, `drift_query` and build summaries now leave out a frozen doc's own node (the doc envelope), not just its sections. A change inside a frozen doc no longer makes its parent sections `LINKED_STALE`. `BROKEN_LINK` is still reported.
- **A frozen doc's section keeps `LINKED_STALE` only while something still causes it.** A section that was `LINKED_STALE` when its doc was frozen stays flagged as long as a linked node has changed since its last verification, and `mark_clean` still clears it. A flag with no cause left, such as one an earlier version raised through a parent's children, used to stay forever and listed no `via`; it now clears, and the doc clears once no section stays stale. Existing indexes heal on their own: the first `check` or `build` after upgrading re-checks every node once.
- **Markdown docs no longer read NOT_FOUND after a full re-hash.** `init`,
  `check --full` and the re-hash after an upgrade checked Markdown files with
  the DocJSON parser, so every Markdown document and section read NOT_FOUND
  and no `build` cleared it. The hasher now reads `.md` files with the
  Markdown scanner. The hashing scheme changed with the fix, so the next
  `build` or `check` re-hashes every file once and an affected index heals on
  its own.
- **A Markdown section after a fenced code block holds its own text.** Each
  H2 section after a fence used to start inside the section before it and
  lose its own last lines, so an edit to one section could flag the next one
  CONTENT_UPDATED, and search, `read_doc` and `diff` showed the wrong text.
  Section ids are unchanged. On an index built before this fix, every such
  section reads CONTENT_UPDATED once, from the first `check` or `build` after
  upgrading, even when its text is unchanged:
  run `axiom-graph diff <ids> . --summary` on them and `mark_clean` the ones
  that report `+0 / -0 lines in body`.
- **JS/TS imports of dotted filenames resolve.** An import such as
  `'./editor.machine'` now finds `editor.machine.ts` instead of looking for
  `editor.ts`. The missing `depends_on` edges appear, and an xstate actor
  imported from such a file links to its real function instead of a guessed
  id that read BROKEN_LINK. A new edge does not mark anything stale.
- **The first build after an upgrade re-parses every file once** when the
  scanners' output has changed, as if each file had been touched, so
  unchanged files pick up fixes like the two above. Baselines and staleness
  are kept.
- **`diff` on a stale node shows the change.** With no baseline given, a
  node that went CONTENT_UPDATED, DESC_UPDATED or LINKED_STALE and has not
  been verified since diffs against the last commit recorded before it went
  stale. It used to pick the newest checkpoint, which could already hold the
  change and show `+0 / -0`. When no such commit is recorded, `diff` returns
  `no_baseline` with the reason. Every diff now reports `baseline_reason`,
  naming the rule that picked its baseline.
- **`diff` works on a Markdown section** (`doc.md#slug`). It shows that
  section's own heading and body; it used to fail with `no_baseline`
  ("Source file not found").
- **Mermaid diagrams render in the dashboard again,** and the page loads
  without the "anonymous define" and "mermaid is not defined" console errors.
- **`build` no longer prints a `link resolver:` line per retargeted link.**
  Those lines moved to DEBUG; the per-build count line stays at INFO.
- **`axiom_graph_add_section` accepts a child under a depth-1 parent,** so
  the three nesting levels the docs describe all work.
- **`drift_query(group_by="location_prefix")` groups test functions by
  file** instead of giving each one its own `#L...` group. Code and doc
  groups are unchanged.
- **Doc files are never left half-written.** Every doc tool save writes a
  temporary file and swaps it in, so a failed write leaves the previous file
  intact.
- **`axiom_graph_accept_doc_edits` is all-or-nothing.** If any listed
  section can't be accepted (unknown id, unreadable file, doc locked by
  another write), it returns one error naming each and writes nothing.
- **Rewriting links in other docs after a rename waits for those docs' write
  locks.** Renaming a section, `apply_rename` / `revert_rename`, renames
  found during a build and the doc-id migration no longer race a concurrent
  doc write. Renaming a section takes the write locks of its doc and of
  every doc that links it together, so if one of them is busy the rename
  fails with nothing written. Elsewhere a doc that couldn't be rewritten
  because it was busy doesn't fail the operation but is named as still
  linking the old id, and a doc file that couldn't be read is named
  separately as one whose links couldn't be checked: in the
  `update_section` reply, in `apply_rename` / `revert_rename` output (the
  results gain `links_unreadable`), in the build warnings, and on the
  doc-id migration's "DocJSON files whose links could NOT be rewritten"
  line.
- **Renaming a function with `apply_rename` carries the verification of the
  docs that link it over to the new id,** including their stamp's
  `verified_against` entry. A doc stays verified only if the renamed code
  still matches the hashes it was verified against; if the body changed too
  (often why the build missed the rename), the doc goes `LINKED_STALE` and
  needs a fresh review. `apply_rename` and `revert_rename` each run in one
  transaction, so a failure applies nothing.
- **A code rename no longer re-keys a sibling's workflow steps.** The step
  lookup matched `do_it` against `doxit`'s steps; it is now exact and
  case-sensitive.
- **A removed `delegates_to` target makes its envelope BROKEN_LINK at the next
  `check`,** not only after `check --full`.
- **`axiom-graph export` records the real project id and version, and
  writes beside the configured database.** It wrote the folder name as
  `project_id`, `0.1.0` as the version, and always `.axiom_graph/index.json`.
  It now takes the id from `axiom-graph.toml`, then the id the index was
  built with, then the folder name; records the installed version; and writes
  `index.json` next to the database `db_path` names. Its help now says it
  exports the whole index.
- **The workflow export page says `1 file`, not `1 files`, and
  `/api/workflow-export` answers 404 for unknown ids.** The 404 names every
  id that matches no workflow or task, where the route used to serve an empty
  `0 workflows · 0 files` page.
- **The live Workflows tab nests minor steps the way the exported page
  does.** Expanded steps (`/api/workflow/{id}/steps?expand=true`) took
  `depth` from delegation hops, so a minor step such as `1.1` sat flush with
  its parent in the dashboard while the export indented it under `1`. `depth`
  is now the number of dots in the step number on every surface.
- **B4 no longer flags decorated AutoStep targets.** The "target is
  undecorated" finding fired for every `@task`/`@workflow` target, because the
  recorded target id never matched the `@workflow`-suffixed envelope id, and
  for targets in files an incremental build skipped. B4 is now resolved when
  findings are read, against the whole index, following package re-exports,
  so incremental and full builds give the same answer. On this repo only the
  two genuinely undecorated targets remain flagged.
- **A node diff finds the node by identity, not by its current line
  numbers.** `axiom_graph_diff` and the viz node diff re-scan the baseline
  file with the language's scanner and take the range of the same qualified
  name (or its earlier name from rename history), instead of cutting the
  baseline at the node's current line range. A function whose body didn't
  change now reads `+0 / -0` even when code above it was added or removed or
  the file was moved. The current side is located the same way.
- **A DocJSON section diffs as its own heading and content**, found by
  section id, instead of the whole doc file.
- **A node whose position can't be determined returns
  `node_position_unresolved`** (for example when the file didn't parse at the
  baseline) instead of a diff cut from the wrong lines. A node new since the
  baseline still shows an empty old side.
- **A build or check stores the `LINKED_STALE` the code change it detects
  causes.** A build that found a function newly `CONTENT_UPDATED` used to
  store the doc sections that document it and the tests that validate it as
  `VERIFIED`; their `LINKED_STALE` appeared only on the next staleness
  recompute, so `axiom_graph_drift_query` and the viz hid the reach of a
  change until something such as a `check` or a `mark_clean` recomputed. A
  `check` (CLI or `axiom_graph_check`) or the viz's staleness refresh that
  was first to see an edit had the same lag: it reported 0 `LINKED_STALE`
  and stored the linked sections and tests as `VERIFIED`, and only a second
  check reported them. All three now run the staleness engine once more
  after recording the change (a build first re-stamps the verifications it
  wrote itself, so a test added together with the code change it covers
  stays `VERIFIED`), and store what the next `check` would compute; a second
  check changes nothing. Sticky `LINKED_STALE` and the verification filter
  are unchanged.
- **The staleness count a build prints is the count `check` reports.**
  `axiom-graph build`, `axiom-graph init` and `axiom_graph_build` used to
  end with `staleness : N nodes updated (M stale)`, where M counted
  frozen-doc sections that `check` leaves out and missed the `LINKED_STALE`
  above. The line now reads `staleness : own: … · link: … · N VERIFIED`, the
  same counts `check` prints for the same files. In the Python API,
  `BuildSummary.staleness_stale` is counted the same way, the new
  `BuildSummary.check` carries the full `CheckSummary`, and
  `CheckSummary.summary_line()` returns the line both commands print.
- **The doc write tools can address a section whose own id contains a dot.**
  `update_section`, `patch_section`, `delete_section`, `add_section`
  (`parent_id`), `add_link`, `delete_link` and `accept_doc_edits` used to
  split a section id on every `.` and walk one nesting level per part, so a
  flat section such as `.pev/doc-topology::category.adr` (the PEV SOP
  templates ship nine of these) was "not found" and could only be changed by
  rewriting the whole doc. They now match the dot-path against the real ids
  at each level: `category.adr` reaches a top-level section of that id,
  `parent.child` still reaches a nested child, and `schema.category.adr`
  reaches a dotted id under a parent. A dot-path that names two sections, a
  flat `a.b` and a `b` nested under `a`, is refused with an error naming
  both, and nothing is written.
- **A doc can no longer give two sections one dot-path, and a file that
  does is never merged in the index.** `write_doc` accepted a doc with a flat
  `a.b` beside a `b` nested under `a` (or two siblings sharing an id),
  reported both as written, and the index kept one `a.b` node carrying the
  other section's content, links and history. `write_doc`, `add_section` and
  a renaming `update_section` now refuse a write that would create such a
  pair, naming the dot-path and both sections, and write nothing; a
  collision already in a file does not block an edit that adds none. A build
  that finds one in a file (from a hand edit) does not fail: it warns once
  per dot-path, naming the file, indexes the first section in document order
  and skips each later one with its subsections.
- **Diffs follow renamed and moved files.** `axiom_graph_diff`, the viz node
  diff and the viz doc diff read a file's baseline version from its current
  path, so a file renamed or moved since the baseline diffed as if every line
  (or every doc section) were new. They now find the file's old path with
  git rename detection and diff against it. Committed renames and renames
  staged with `git mv` / `git add` are followed; a file at a path git does not
  track yet still reads as new. A file that did not exist at the baseline
  still has an empty old side. When the file is missing at the baseline and
  git cannot tell whether it was renamed (the lookup failed, or git skipped
  inexact rename detection past `diff.renameLimit`), the diff returns
  `{"error": "baseline_path_unresolved"}` instead of an all-new diff.
  `axiom_graph_diff` and both viz diff endpoints add `path` and
  `baseline_path` (the file read at the baseline, `null` for a new file).
- **The viz doc diff no longer claims to need a docs submodule, and drops
  `submodule_sha`.** It always worked for an inline `docs/` folder; its
  documentation said otherwise. The doc-diff response's `submodule_sha` key,
  which held the docs folder's tree id for inline docs, is replaced by
  `baseline_rev`: the docs submodule's commit at the baseline for submodule
  docs, else the baseline SHA itself. A doc whose docs folder did not exist
  at the baseline now diffs as new instead of failing. The panel title reads
  "baseline ↔ current" and notes a rename or a new doc.
- **A hash that returns to its baseline is no longer a change.** A node whose
  hash flips and then comes back to the baseline it was measured against (a
  stale MCP server, a branch switch and back, an edit then revert) no longer
  makes its tests and doc sections LINKED_STALE, including on the build where
  the hash comes back. A change accepted by `mark_clean` at a different hash
  still counts for its dependents until they are verified or the offender is
  reverified. Every change-time reader (staleness passes, `reverify`'s
  offender ordering) now shares one rule.
- **`mark_clean` clears LINKED_STALE carried on a frozen doc's section.** It
  was a no-op: the frozen carry-forward re-asserted the status without
  looking at verification. A section verified after it last became
  LINKED_STALE now clears on the next `check`, and its doc clears once no
  section stays stale. Freezing a doc still never clears LINKED_STALE.
- **A BROKEN_LINK on a frozen doc's section is counted everywhere.** With
  `include_frozen=False`, `check`'s summary and `drift_query` `counts` /
  `ids` dropped it while `format='full'` listed it. They now all keep it,
  counted under BROKEN_LINK only; frozen own-status and LINKED_STALE rows are
  still dropped.
- **MCP tool results are plain text again, not JSON-escaped.** Every tool
  returns a string, but FastMCP also sent each result a second time as
  `structuredContent: {"result": "..."}`, and clients that display that copy
  showed quotes as `\"` and newlines as `\n`. Text an agent copied from a
  result, such as a `read_doc` section pasted into a `patch_section`
  `old_string`, then failed to match. Tools are now registered with
  `structured_output=False`, so each result is a single text block.
- **A `--since-sha` that matches nothing no longer returns the entire
  history.** This applies to `axiom-graph report --since-sha` and to
  `axiom_graph_report(since_sha=...)`. A SHA the index never recorded but git
  knows now resolves to that commit's time, and the reference line says so. An
  unknown or ambiguous SHA, or one shorter than 4 characters, is an error: the
  CLI exits non-zero and the MCP tool returns `ERROR: ...`.
- A full 40-character SHA now matches a checkpoint stored with the legacy
  12-character SHA. `axiom-graph history checkpoint` now stores the full SHA,
  and `--list-refs` still prints 12 characters.
- The viz "changed since" view now resolves commits that aren't in the index by
  their commit time, for both ends of a range. The commit picker lets you
  select these faded commits. A SHA that git doesn't know still shows the
  not-resolved banner, and its text now says why.
- Doc write tools no longer reset the rest of the document. Removing a link,
  deleting a section or renaming one now touches only that section. Its
  siblings keep their verification, history and edges, and links from other
  docs survive. A renamed section (and its children) carries its
  verification, history and edges to the new id, and other docs' links to it
  are rewritten. Deleting a section that another doc links to keeps that
  doc's link; the next check reports it as BROKEN_LINK instead of the link
  silently disappearing.
- Sections created by `add_section`, and every section of a new doc from
  `write_doc`, are now verified by the write that created them, instead of
  being born LINKED_STALE.
- Concurrent writes to the same doc are serialised by a per-file lock, so two
  appends to one section both land. A lock timeout returns `ERROR:` and writes
  nothing.
- **Verified dependents stay settled.** A doc section, test or annotation
  envelope verified after an offender changed no longer lists that offender
  again when a *different* linked node changes later. Only the offenders that
  changed after the verification are reported (in `check`, `drift_query` and
  `reverify`), and reverifying them clears it. Before, one newer change kept
  the dependent's whole offender history, so `reverify` skipped it
  indefinitely (#15). A docstring-only change to an annotated function after
  the verification still counts.
- **Reverifying a composite counts for its parts.** `reverify` on a module, doc
  or parent section now counts as reverifying each of its `composes`
  descendants, so a later reverify of another offender finishes clearing
  dependents that were stale via both. Changing a descendant after the
  reverify re-opens it.
- **`reverify` names the real blocker.** When a dependent is skipped because
  another offender was only marked clean (`mark_clean` does not settle an
  offender for its dependents), the output names that offender and says to
  reverify it. It no longer prints "Nothing to clear — no LINKED_STALE rooted
  at this node" when dependents rooted at the node were skipped.
- **Purging a module no longer launders deleted or drifted functions.** The
  mtime fast pass now only re-confirms files whose nodes are all already
  VERIFIED; it never turns a NOT_FOUND or CONTENT_UPDATED row VERIFIED.
  Purging a file-level anchor (module, DocJSON doc, config) also clears the
  file's stored mtime, so the next build always rescans it and re-creates the
  anchor.

  **Upgrade note:** rows already laundered to VERIFIED in an existing index are
  not repaired automatically. Make a byte change to each affected file (a
  trailing blank line is enough) and run `axiom-graph build`: the file no
  longer matches its module node, so the per-node check runs and re-marks
  dead rows NOT_FOUND and drifted rows CONTENT_UPDATED. You can then revert
  the change; the rows stay put because the fast pass no longer promotes
  them. A bare `touch` is not enough — the build re-stamps the mtime before
  staleness runs, and the unchanged bytes still match the module node.
- **Unchanged files are skipped again after their stored mtime was cleared.**
  A module or doc row whose `file_mtime` is NULL is re-stamped by the next
  build, so the build after that skips the file (#13).

### Deprecated

- `axiom_graph_report(verbose=...)`: use `detail="summary" | "condensed" |
  "full"` instead. `verbose=True` still works in 3.0.0 and maps to
  `detail="full"`. When both are given, `detail` wins.

## [2.4.1] - 2026-09-19

Tag fix release. A document's tags could silently stop reaching the index, and the build summary hid it.

### Fixed

- **Tag edits on a document now always reach the index.** A document's tags could silently fail to update in the index when its `tags` key sat past the first few thousand characters of the file — which is exactly what happens to any long document that was tagged after it was written. Tag search, tag filters and the visualiser kept showing whatever tags the document had when it was first indexed. Tags are now compared as a set on every indexed item, so adding, changing and removing them all take effect.
- **Existing indexes repair themselves on upgrade.** Your next `axiom-graph build` brings already-drifted tag rows back into agreement with each document's stored tags. It runs once, inside a transaction, from data the database already holds — no rescan, no change to staleness or verification state, nothing to run by hand.

### Changed

- **The build summary now reports skipped documentation files** and states that its `files scanned` / `files skipped` counts cover Python files only. An unlabelled `0` there previously read as "no documentation was scanned", which is what sent the original investigation of the tag bug to the wrong subsystem. Both `axiom-graph build` and the `axiom_graph_build` MCP tool gained the line.
- **The index schema version advances to 2.** That is how the automatic repair above knows to run once and only once. As with any schema advance, an index written by 2.4.1 will not open under 2.4.0: the older package refuses it with a "written by a newer axiom-graph" error rather than touching it. Upgrading is the fix; the pre-upgrade database is snapshotted next to it as `graph.db.pre-v2.bak` either way.

## [2.4.0] - 2026-09-17

Fresh-install fix release. A new `pip install axiom-graph` resolved mcp 2.x and the MCP server crashed on start; `mcp` is now capped below 2. The release also carries the offline workflow export, AutoSteps that report the intent of the function they call, links through package re-exports, and a search that no longer returns a confident empty result when its filters drop every ranked hit.

### Added

- **Workflow export.** Pick workflows in the dashboard's Workflows view and download a single HTML file that opens with no server, no checkout and no network. It shows each workflow's step outline and lets you click through to the code behind every step, including code the steps delegate to: the file carries every workflow's module, every step marker's file and every delegate target's file, with targets pulled in transitively so no click-through dead-ends. The page has syntax highlighting, light and dark themes, a contents list for multi-workflow exports, folding for child steps and for step intent, and a search across all the carried files. The same bundle is served as JSON at `GET /api/workflow-export?ids=<id>,<id>&format=json`, and `format=html` returns the page. Highlighting uses Pygments, now part of the `viz` extra; without it the code renders as plain text.
- **`axiom_graph_workflow_detail` takes `format="json"`.** It returns the envelope's full structured form, the same per-workflow shape the export bundle carries. The default `format="text"` output is unchanged.

### Changed

- **AutoSteps now report the intent of the function they call.** An `AutoStep` marker has no purpose, inputs, outputs or `critical` of its own; that intent lives on the `@task` or `@workflow` it delegates to, as `AutoStep`'s own documentation has always said. Those fields are now resolved from the target everywhere steps appear: the dashboard's workflow and test views, the export, and `axiom_graph_workflow_detail` with `verbose=True`. Values written on the marker still win. In 2.3.0 the dashboard inherited only the purpose and MCP inherited nothing, so the same step read as purposeful in the browser and blank through MCP.
- **The dashboard and the MCP tools can no longer name different delegate targets for one step.** Each surface had its own lookup over an unordered result, one taking the first row and the other the last, so they could disagree and the answer could change between builds. Delegate resolution now lives in one shared accessor with one tiebreak (the lexicographically smallest target).
- **Viz HTTP API: the step payload's `cortex_location` and `cortex_line_start` are replaced by a nested `target` object** carrying the delegate target's `id`, `name`, `location` and `line`. Each step also carries its own marker's `location` and `line`, which are not the target's. Anything reading the two removed keys from `/api/workflow/{id}/steps` needs updating. `workflows.api.StepRow` gains `location`, `line`, `depth`, `note` and `target`, all with defaults.

### Fixed

- **`axiom_graph_search` returned nothing when `node_type`, `scope` or `tag` filtered out every ranked hit.** The keyword fallback stages decided whether to run by looking at the ranked hits *before* filtering, so a query whose ranked hits were all filtered away returned an empty result and never tried the substring fallbacks its documentation promises. The fallbacks now run on the filtered result.
- **`axiom_graph_search` accepted any `node_type`.** A value outside the ontology (for example `node_type="doc"` or `node_type="function"`, which are not node types) matched nothing and came back as a clean empty result. It is now rejected with an error that lists the valid node types.
- **A fresh `pip install axiom-graph` produced an MCP server that crashed on start.** The `mcp` dependency had no upper bound, so an install that didn't go through `poetry.lock` resolved mcp 2.x, which removed the `mcp.server.fastmcp` module the server imports. `python -m axiom_graph.mcp_server` died with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`, and Claude Code showed the server only as `failed`. `mcp` is now capped at `<2`, so installs resolve 1.x. Environments built from the lock were never affected: it pins 1.27.2. **If you're on 2.3.0, run `pip install 'mcp<2'`** in the environment the server runs from. Moving to the mcp 2.x API is left for a later release.
- **A test or `AutoStep` that reached a function through a package re-export got no link to it.** axiom-graph links a test to each function it calls (`validates`) and an `AutoStep` to the function it runs (`delegates_to`). Those links are what flag the test or step when the function changes. A caller could import the function through a package `__init__.py` that re-exports it: `from pkg import func`, where `pkg/__init__.py` does `from .impl import func`. That caller got neither link. The test kept only a dependency on the package and never went stale when `func` changed, and the `AutoStep` dangled as `BROKEN_LINK`. Three gaps compounded:
  - Named re-exports (`from .impl import func`, with or without `as`) recorded nothing; only `*` re-exports did.
  - The build-time re-export walk served `delegates_to` only, so a `validates` target was dropped even behind a correctly marked star re-export.
  - Relative imports inside an `__init__.py` resolved one package too high (next entry).

  Both re-export forms are now recorded on the importing module's dependency edge. The walk follows named bindings before star sources and carries aliases across hops, and `validates` targets go through the same walk. One case is deliberately left as before: a call built from an attribute chain the import does not spell (`import pkg; pkg.sub.func()`). It keeps its previous behaviour rather than risk resolving to an unrelated `func` that `pkg` re-exports.
- **Relative imports inside a package's `__init__.py` resolved against the parent package.** `from .resolve import x` in `pkg/sub/__init__.py` was looked up as `pkg.resolve`. It was dropped if that module did not exist, and attached to the wrong module if it did. A `from .core import *` there wrote no re-export marker, so star resolution never switched on for the most common shim layout. Imports inside a package's own `__init__.py` now resolve against the package itself: at module level, inside functions and under guards.
- **A test's `validates` link was dropped when the function it called lived in a file the build skipped as unchanged.** The build decided whether a link target existed from the nodes of the files it had just scanned. So adding or editing a test without touching the code it tests lost the link, until some later build happened to rescan both files. Targets are now checked against the live index: this build's nodes, plus the nodes of unchanged files that still exist. `NOT_FOUND` leftovers of a moved function do not count, so a caller links to where the function now lives rather than to its old id.

### Upgrading an existing index

The re-export and `validates` links above appear only for files a build rescans, and a build skips files whose mtime has not changed. So an existing index gains nothing until its files are rescanned. To backfill, clear the stored file mtimes on `.axiom_graph/graph.db` with any SQLite client:

```sql
UPDATE nodes SET file_mtime = NULL;
```

Then run `axiom-graph build`. This is non-destructive: baselines, verification records and history are kept. Do not use `axiom-graph init` for this; it deletes the index.

An index from before this release has no re-export markers. A build over one that still finds unresolved links warns and names this recipe. That happens on roughly the first incremental build after upgrading. After that, markers written by earlier builds keep the warning quiet. Treat this note as the reference.

### Coming in 3.0.0

The next major release changes three things you will have to act on. 2.4.0 changes none of them.

- **Doc IDs change form.** Document and section IDs move to per-root namespacing, and running `axiom-graph doc-ids execute` becomes a required upgrade step. `axiom-graph doc-ids preview` already shows what every ID in your project will become and whether any two would collide; it is read-only and safe to run now. Keep leaving `execute` alone on 2.x.
- **`.docjson` becomes the default DocJSON extension.** New documents are written as `.docjson`. Existing `.json` documents are still read.
- **Semantic search is removed**, as ADR-020 announced in 2.1.0: `mode="semantic"` on `axiom_graph_search`, the `[semantic]` and `[semantic-torch]` extras, and the viz and CLI toggles. Keyword search, the default, stays.

The first rebuild may show tests as `LINKED_STALE` whose target changed while the link was missing. That is pre-existing debt becoming visible, not a regression.

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
