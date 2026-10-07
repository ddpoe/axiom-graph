<!-- generated from axiom_graph::docs/consumer/concepts/ontology @ 751540b69193; do not edit -->

# Node and Edge Types

## Node types

Every node is an `atomic_process` or a `composite_process`. The type is the role a node plays, not its language, so a Python function and a TypeScript function are both `atomic_process`.

| Type | What it is | Examples |
|---|---|---|
| `atomic_process` | A single unit of work | a function or method, a test, a doc section, a `Step` marker, a simple xstate state |
| `composite_process` | A container of other nodes | a module, a doc file, a config file, a `@workflow` or `@task` envelope, a state machine |

To filter by type, use `axiom-graph list --type atomic_process` or the `node_type` argument of `axiom_graph_list` and `axiom_graph_search`.

## Subtypes

The subtype says what kind of node it is within its type.

| Subtype | Type | What it is |
|---|---|---|
| `function` | atomic_process | A Python or JS/TS function or method |
| `test` | atomic_process | A test function (see below) |
| `docjson_section` | atomic_process | A section of a DocJSON document |
| `docjson` | atomic_process | A `##` section of a Markdown doc |
| `step` | atomic_process | A `Step(...)` marker inside a `@workflow` or `@task` function |
| `autostep` | atomic_process | An `AutoStep(...)` marker |
| `state` | atomic_process | An xstate state with no nested states |
| `module` | composite_process | A Python or JS/TS file |
| `docjson_doc` | composite_process | A DocJSON document |
| `docjson` | composite_process | A Markdown doc file |
| `config` | composite_process | A file in a config directory |
| `workflow` | composite_process | A `@workflow` envelope |
| `task` | composite_process | A `@task` envelope |
| `state_machine` | composite_process | An xstate machine (`createMachine(...)`) |
| `state` | composite_process | An xstate state with nested states |

A Python test is a function or method whose name starts with `test` in a `test_*.py` or `*_test.py` file (fixtures and nested functions excluded), or a `test_*` function in any other file. In JS/TS, every function in a test file (`*.test.*`, `*.spec.*`, `test_*`, `*_test`, `*_spec`) is a test.

Envelopes, steps and state machines are covered in [annotations](annotations.md).

## Edge types

| Edge | From → to | Meaning | Created by |
|---|---|---|---|
| `composes` | process → process | X contains Y | module → function, module → `@workflow` or `@task` envelope, function → nested function, document → section, section → subsection, envelope → step, machine → state |
| `depends_on` | process → process | X imports or uses Y | module imports; Python functions that use an imported project module |
| `delegates_to` | process → process | X hands off to Y | an `AutoStep` marker and the function called after it; xstate transitions and invoked actors |
| `annotates` | composite → process | envelope X describes function Y | each `@workflow` or `@task` |
| `validates` | process → process | test X calls Y | Python tests and the project functions they call |
| `documents` | doc section → any node | doc section X describes Y | the `links` of a DocJSON section |

*Process* means `atomic_process` or `composite_process`; *composite* means `composite_process`.

The scanners create every edge except `documents`. To add one, add a link to the doc section, for example with `axiom_graph_add_link` (see [DocJSON](docjson.md)). Which edges carry drift is covered in [staleness](staleness.md).

## How edges are checked

The node types, edge types and allowed endpoints above are defined in `ontology.yaml`, which ships inside the axiom-graph package.

`axiom-graph link` refuses an edge whose endpoints break these rules and prints the endpoints that are allowed. A build checks every edge the same way and lists any that break the rules under `warnings` in its output.

## Language support

| Source | What becomes nodes and edges | Setup |
|---|---|---|
| Python (`.py`) | modules, functions, methods, nested functions, imports, tests, `@workflow` / `@task` envelopes and their `Step` / `AutoStep` markers | Built in. Every `.py` file under the project root outside excluded directories |
| JavaScript / TypeScript (`.js`, `.jsx`, `.ts`, `.tsx`) | modules, named functions, arrow functions assigned to a `const`, class and object methods, `import` statements, tests, `workflow(...)(fn)` / `task(...)(fn)` envelopes and their markers | `pip install "axiom-graph[js]"`, then set `js_paths` |
| xstate v5 machines in JS/TS files | `createMachine({...})` and `setup({...}).createMachine({...})`: the machine, its states and its transitions | Same as JS/TS |
| DocJSON (`.docjson`, `.json`) | documents, sections, and section links | Built in. Files under `docs_dirs` |
| Markdown (`.md`) | the file and each `##` section | Built in. Files under `docs_dirs` |
| Config files (`.md`, `.json`, `.yaml`, `.yml`, `.toml`) | one node per file | Built in. Files under `config_dirs` (default `.claude`) |

JS/TS tests get the `test` subtype but no `validates` edges; only Python tests are linked to the code they call. JS/TS dependencies come from `import` statements only, not `require()`.

JS/TS scanning is off until you list paths in `axiom-graph.toml`:

```toml
[axiom_graph.scan]
js_paths = ["src/**/*.ts", "lib/**/*.js"]
```

If `js_paths` is set but the `js` extra is not installed, the build skips those files and prints a warning telling you to install it. [Configuration](../get-started/configuration.md) covers all the scan settings.
