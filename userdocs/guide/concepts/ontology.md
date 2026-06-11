<!-- generated from axiom_graph::docs.consumer.concepts.ontology @ 24dfde617563; do not edit -->

# The Ontology

## Node Types

axiom-graph does not model your codebase as "files and symbols." It models it as **processes and entities**, using a type system derived from the W3C Provenance Ontology (PROV-O). This is a deliberate, principled choice: a node type describes the *role* a thing plays in getting work done, not the language construct it happens to be written in. A Python function, a JavaScript arrow function, an axiom-annotation `@task`, and a doc section are all the *same shape* of thing — a unit of work — so they share a type.

There are exactly **three** structural node types. (The model started with five; [ADR-009](the-mesh.md) collapsed `document` and `constraint` into the process/entity split, because almost everything in a codebase is either something that *does* work or something that work *consumes or produces*.)

| Node Type | What it represents | Examples |
|---|---|---|
| **atomic_process** | The smallest meaningful unit of work — a leaf with no sub-parts. | A Python function, a test, a single doc section, a workflow step |
| **composite_process** | A container that orchestrates or holds other processes. Scale-invariant. | A module, a package, a doc file, a config file, a `@workflow`, an xstate state machine |
| **entity** | Data consumed or produced by a process — not logic. | A third-party package, a data file, a trained model, a config artifact |

The key idea is **scale-invariance** for composite processes: a module, a package, a pipeline, and a `@workflow` envelope are all `composite_process`. The model does not care how big the container is, only that it contains other processes. Entities sit at the edges of the graph — they are always treated as CLEAN by [staleness](staleness.md) checks, because a data file has no logic to drift.

Concretely: a `main()` and the test that exercises it are both `atomic_process`; the module holding them and the `@workflow` that wraps it are both `composite_process`; the pandas they import is an `entity`.

This principled typing is what lets one mesh hold code, docs, tests, and orchestration together. Because everything is reduced to three shapes plus subtypes, an agent (or you) can traverse from a doc section straight to the function it documents without ever leaving the type system — that is the context-reduction payoff of [the mesh](the-mesh.md).

## Subtypes

Each of the three node types carries an optional **subtype** that records what kind of process or entity it is. The structural type drives validation and propagation; the subtype is the human- and agent-readable detail.

| Subtype | Node Type | Meaning |
|---|---|---|
| `function` | atomic_process | A Python function or method, or a JS/TS function |
| `test` | atomic_process | A test function (identified by a `test_` prefix or a test-file location) |
| `docjson` | atomic_process | A single section within a DocJSON file |
| `step` | atomic_process | A `Step(...)` marker inside a `@workflow` / `@task` body |
| `autostep` | atomic_process | An `AutoStep(...)` marker — a step whose next call is recorded as a `delegates_to` edge |
| `state` | atomic_process | A leaf (simple) state in an xstate v5 machine |
| `docjson` | composite_process | A DocJSON file (contains sections via `composes` edges) |
| `config` | composite_process | A configuration file (settings, hooks, skills) |
| `workflow` | composite_process | A function decorated with `@workflow` |
| `task` | composite_process | A function decorated with `@task` |
| `state_machine` | composite_process | An xstate v5 `createMachine(...)` envelope |
| `state` | composite_process | A compound (non-leaf) xstate state with substates |
| `external_package` | entity | A third-party dependency outside the standard library |
| `data_artifact` | entity | A data file or dataset a workflow consumes or produces |
| `config_artifact` | entity | A configuration artifact (e.g. a hyperparameter set) |
| `model_artifact` | entity | A trained model produced by a workflow |

Note that some subtype *names* appear under more than one structural type. A `docjson` atomic_process is one **section**; a `docjson` composite_process is the whole **file** that composes those sections — granular documentation falls straight out of the type system, since each section is its own addressable node. Likewise `state` is an atomic_process when it is a leaf state and a composite_process when it nests substates.

The `workflow`, `task`, `step`, `autostep`, `state_machine`, and `state` subtypes belong to the semantic layer — the annotated orchestration highways covered in [Annotations](annotations.md). They are how axiom-graph records *intent* and *step order* through a pipeline without trying to build a full call graph.

## Edge Types

If node types are the nouns, **edges are the verbs — and they are intent-typed.** Each edge declares *why* two nodes are related, not merely that they reference each other. This is the single biggest difference between axiom-graph and a generic call graph or import graph: `documents`, `validates`, and `delegates_to` are first-class semantics, not labels stapled onto a raw reference.

| Edge Type | From → To | Meaning |
|---|---|---|
| `composes` | composite → atomic / composite | Structural containment: module → function, doc file → section, workflow → step |
| `depends_on` | process → process / entity | Import or execution dependency |
| `delegates_to` | process → process | Runtime invocation across a boundary; also how an `AutoStep` records the task it calls |
| `annotates` | composite → atomic / composite | A `@workflow` / `@task` envelope annotates the underlying function (one per decorated function) |
| `validates` | atomic → atomic / composite | Test coverage: a test verifies a function |
| `documents` | process → any | A doc section describes a node |
| `consumes` | process → entity | A process reads an entity as input |
| `produces` | process → entity | A process creates an entity as output |
| `constrains` | atomic → process / entity | A schema or contract restricts behavior |
| `supersedes` | process → process | A replacement obsoletes the thing it replaces |

The edges you will meet most often are `composes`, `depends_on`, `documents`, and `validates`. Because intent is encoded in the edge, the *same typed mesh* answers two very different questions with one read: "what does this depend on?" (traverse `depends_on`) and "what is now out of date?" (a [staleness](staleness.md) read that follows `documents` and `validates`). The mesh is the product; drift detection and intent-scoped retrieval are just two reads against it.

Edge intent also determines how far drift travels. Propagation depth is chosen by what an edge *means*, not by uniform graph distance — `validates` propagates one hop (test → production), `documents` is transitive (doc → doc → code), and `annotates` is one hop. That transitive `documents` chain is exactly what lets consumer docs like this one ride a code → dev-doc → consumer-doc path and inherit staleness when the underlying code changes.

## Build-Time Validation

The type system is not advisory — it is **enforced**. Every node type, edge type, and the legal node-type pairings for each side of every edge are declared in a single file, `axiom_graph/ontology.yaml`. When you run a build, the builder checks each edge against these rules, and **an edge whose endpoints violate the ontology fails the build.**

This is what keeps the mesh trustworthy. You cannot, for example, point a `validates` edge from a doc section, or hang a `composes` edge off an `atomic_process` — the schema forbids it, so the graph can never drift into an incoherent shape. The rules are precise: `composes` may only go from a composite to a process; `consumes` and `produces` may only target an entity; `annotates` runs strictly from an envelope to its function.

Validation runs as part of the normal index build:

```bash
axiom-graph build
```

Because the ontology lives in one declarative YAML file rather than being scattered through scanner code, the type system is also where axiom-graph's framework-awareness is extended. Teaching the indexer a new declarative framework means adding its node and edge shapes here — the xstate v5 scanner (which introduced the `state_machine` and `state` subtypes) is the proven example of that extensibility in action.

## Language Support

The payoff of a process-centric type system is that **languages share types.** axiom-graph currently scans two languages, plus xstate v5 state machines as a concept extracted on top of JS/TS.

| Language | Scanner | What it captures | Install |
|---|---|---|---|
| **Python** | AST-based | Functions, methods, classes, modules, packages, imports, docstrings, decorators, `@workflow` / `@task` envelopes, `Step` / `AutoStep` markers | Built in — no extra install |
| **JavaScript / TypeScript** | tree-sitter | Named functions, arrow functions, class and object methods, HOF wrappers, ESM imports, test-runner calls, xstate v5 machines | `pip install axiom-graph[js]` |

Python support is comprehensive and included by default. The JS/TS scanner is an optional dependency: when tree-sitter is not installed, `.js` and `.ts` files are silently skipped rather than erroring.

The important point is that **both languages feed the same three node types.** A Python function and a JS/TS function both become an `atomic_process` with subtype `function`. A Python module and a JS/TS module both become a `composite_process` (with no subtype — the language is implicit in the file path, so the model needs no per-language node types). Tests, imports, and dependencies map onto the same `validates`, `depends_on`, and `composes` edges regardless of source language. The xstate scanner is the only place that adds language-specific subtypes — `state_machine` and `state` — and it does so by extending the ontology, not by forking it.

To turn on JS/TS scanning, point axiom-graph at the paths to scan in your `axiom-graph.toml`:

```toml
[axiom_graph.scan]
js_paths = ["src/**/*.ts", "lib/**/*.js"]
```

See [Configuration](../get-started/configuration.md) for the full set of scan settings. Because the type schema is shared, a single graph traversal crosses language boundaries seamlessly — an agent following the mesh never has to know, or care, which language a given node was written in.
