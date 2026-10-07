<!-- generated from axiom_graph::docs/consumer/concepts/the-mesh @ d83206c8fbfa; do not edit -->

# The Mesh

## What the mesh is

axiom-graph scans your project into an index called the **mesh**. It has two parts:

- **Nodes** are the things in your project: a module, a function, a doc section, a test, a `@workflow` envelope.
- **Edges** are typed relationships between nodes: a module `composes` its functions, a test `validates` the function it calls, a doc section `documents` the code it describes.

The mesh is a SQLite file, `.axiom_graph/graph.db` by default. Agents query it through the [MCP server](../get-started/connect-your-agent.md); people use the [CLI](../get-started/use-the-cli.md) and the [dashboard](../viz.md).

Two kinds of read use the same edges. An agent follows them outward from one node to collect what it needs: a function, the doc section that documents it, the tests that validate it. The drift check follows them from code that changed to the doc sections and tests linked to it, and flags those for review (see [staleness](staleness.md)).

## Nodes

A build creates a node for each of these:

- a Python module, and each function and method in it, including nested functions (a class has no node of its own; its methods do); JS/TS modules and functions too, when `js_paths` is set
- a DocJSON document and each of its sections; a Markdown file and each of its `##` sections
- a file in a config directory such as `.claude/`
- a `@workflow` or `@task` envelope, and each `Step` or `AutoStep` marker inside it
- an xstate state machine and each of its states

Each node has an id, a type and subtype (for example `atomic_process` / `function`), a title, a location (the file, plus the line range for code), and a one-line summary from its docstring or heading. [Node and edge types](ontology.md) lists every type.

Because a function or a doc section is a node of its own, `axiom_graph_source` returns one function's lines instead of the whole file, and `axiom_graph_read_doc` can return one section instead of the whole document.

## Edges

An edge's type says how two nodes are related. The scanners create six types:

| Edge | Meaning | Example |
|---|---|---|
| `composes` | X contains Y | a module and its functions; a document and its sections; an envelope and its steps |
| `depends_on` | X imports or uses Y | a module and a module it imports |
| `validates` | test X calls Y | `test_parse_config` and `parse_config` |
| `documents` | doc section X describes Y | a section and each node in its `links` |
| `annotates` | envelope X describes function Y | a `@workflow` envelope and the function it decorates |
| `delegates_to` | X hands off to Y | an `AutoStep` marker and the function called after it; an xstate transition |

A read can ask for one type, for example only the tests that validate a function or only the sections that document it. The drift check follows `documents`, `validates`, `annotates` and `delegates_to` edges.

The mesh does not record every function call. Calls show up only as `validates` (from tests) and `delegates_to` (from `AutoStep` markers); [annotations](annotations.md) explains the markers. [Node and edge types](ontology.md) shows which node types each edge may connect.

## Node ids

Every node id starts with the project id, and its parts are separated by `::`. For code:

```
{project_id}::{module path}::{name}
```

- **project_id**: the `project_id` in `axiom-graph.toml`, which `init` records. Without it, the id the index was built with, else the project directory's name.
- **module path**: the file's path from the project root, with `/` replaced by `.` and the extension dropped. A package's `__init__.py` takes the package's path.
- **name**: the function name. A method is `Class.method`; a nested function is `outer.inner`.

| Node | Id |
|---|---|
| Module `myproject/utils.py` | `myproject::myproject.utils` |
| Function `parse_config` in it | `myproject::myproject.utils::parse_config` |
| Method `Model.fit` in `myproject/ml/model.py` | `myproject::myproject.ml.model::Model.fit` |
| Test in `tests/test_utils.py` | `myproject::tests.test_utils::test_parse_config` |
| `@workflow` or `@task` envelope on `run` | `myproject::myproject.pipeline::run@workflow` |
| `Step` 2 inside `run` | `myproject::myproject.pipeline::run::step-2` |
| Section `overview` of `docs/architecture.docjson` | `myproject::docs/architecture::overview` |
| Config file `.claude/settings.json` | `myproject::config.claude.settings` |

Doc ids use the file path instead of a module path; [DocJSON](docjson.md) gives the rules. Pass an id to `axiom-graph graph` or to any `axiom_graph_*` tool that takes one. To find an id you don't know, use `axiom_graph_search` or `axiom-graph list` rather than guessing.

## How the index is built

Create the index once with `axiom-graph init`. After that, run `axiom-graph build` (or the `axiom_graph_build` MCP tool) whenever code or docs change. Running `init` again asks first, then deletes the index along with its history and verification records.

A build:

1. re-reads the files whose content changed or whose modification time moved since the last build, and skips the rest (after an upgrade that changes how files are read, the first build re-reads every file once)
2. adds nodes for new code and docs, and updates edges
3. computes drift and ends with a one-line count of nodes per status

The next `check` starts where the build left off and re-checks only what changed since.

Python files are found anywhere under the project root, outside excluded directories. JS/TS files, doc directories and config directories come from `[axiom_graph.scan]`; see [configuration](../get-started/configuration.md).

Between builds, `check` and the read tools keep drift current for the files you edit, but only a build adds new functions and sections to the index or removes deleted ones. When a file has some the index does not match yet, they say so:

```
myproject/utils.py has 2 new functions — run build
```

A build keeps each existing node's stored fingerprint, so changed code shows up as drift until someone verifies it. [Staleness](staleness.md) covers how.

## Reading the mesh

To work on a piece of code, find its node, follow its edges, and read only what you need:

1. Find the node. `axiom_graph_search` searches node names and summaries and returns ids.
2. Follow its edges. `axiom_graph_graph` lists a node's outgoing edges; `direction="in"` lists incoming ones, such as the tests that validate it and the sections that document it. `depth` follows more than one hop.
3. Read what you picked. `axiom_graph_source` returns one node's source, and `axiom_graph_read_doc` returns doc sections.

Before answering, these tools re-check the files behind the nodes they show, then mark each node that is not `VERIFIED` with its status:

```
  --[documents]--> myproject::docs/architecture::config-loading  [LINKED_STALE]
```

`refresh_before_read` in [configuration](../get-started/configuration.md) turns the re-check off or makes it a full `check`.

The CLI shows the same edges, without the status marks:

```
$ axiom-graph graph myproject::myproject.utils::parse_config . --direction in
[atomic_process] myproject::myproject.utils::parse_config  @ myproject/utils.py#L12-L40
  incoming:
  <--[validates]-- myproject::tests.test_utils::test_parse_config
  <--[composes]-- myproject::myproject.utils
  <--[documents]-- myproject::docs/architecture::config-loading
```

[Connect your agent](../get-started/connect-your-agent.md) sets up the MCP tools; [use the CLI](../get-started/use-the-cli.md) covers the commands.
