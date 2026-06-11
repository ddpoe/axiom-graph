<!-- generated from axiom_graph::docs.consumer.concepts.the-mesh @ 95d0f753b7d7; do not edit -->

# The Mesh

## The Mesh Is the Product

axiom-graph builds a **typed mesh** over your project: a structured map that weaves your code together with the docs, tests, and workflows that surround it. The mesh is the product. Everything else axiom-graph does — pulling exactly the context an agent needs, detecting drift, keeping published docs honest — is a read against this one structure.

The mesh has exactly two building blocks:

- **Nodes** are the things in your project: a function, a module, a documentation section, a test, a `@workflow` or `@task` envelope, an xstate state machine.
- **Edges** are *intent-typed* relationships between nodes: a module **composes** its functions, a test **validates** a function, a doc section **documents** a class, a workflow envelope **annotates** the function it wraps.

The whole mesh lives in a local SQLite database at `.axiom_graph/graph.db`. You query it three ways: the MCP server (the primary surface for AI agents), the [CLI and viz dashboard](../get-started/use-the-cli.md) for humans, and the [graph traversal tools](../get-started/connect-your-agent.md) that walk edges directly.

The payoff is **context reduction**. Instead of loading a whole file to find one function, or grepping a repo to guess which test covers it, an agent traverses the mesh to *exactly* the nodes that matter — the function, the spec section that describes it, the tests that validate it — and reads only those. The mesh turns "load everything and hope" into "follow the typed edge to the one node you need."

## Two Reads, One Mesh

The single most important thing to understand about axiom-graph is that **intent-scoped retrieval and drift detection are two reads against the same mesh, not two separate systems.**

**Intent-scoped retrieval** is the act of pulling context. You start at one node and follow the intent-typed edges outward to gather exactly what matters — the function, the spec section that documents it, the tests that validate it — and the agent loads that focused bundle instead of the whole repo. It is a forward read along `documents` and `validates` edges: "give me this function, plus the design section that documents it, plus the tests that validate it."

**Drift detection** walks the same edges in the same mesh and asks a different question: "this function's code hash changed — which doc sections and tests linked to it are now suspect?" That is a read along the same `documents` and `validates` edges, propagating staleness outward.

There is no separate "staleness graph" or "context graph." One typed mesh answers both. Build the links once and you get focused retrieval and trustworthy drift for free, because they are the same edges read in two directions. (Drift is covered in depth in [staleness](staleness.md).)

This is why intent-typed edges matter so much: the *type* of the edge tells both reads how to behave. A `validates` edge means "this test depends on that function" — so retrieval pulls the test in, and drift flows one hop from production code to its test. A `documents` edge means "this prose describes that node" — so retrieval pulls the spec in, and drift flows transitively through chained docs. Same edges, two reads.

## Nodes

A node is any meaningful unit in your project that axiom-graph tracks. When the scanner walks your codebase it creates a node for each function, class, module, documentation section, test, configuration file, `@workflow`/`@task` envelope, and xstate state machine it finds.

Nodes are **atomic and modular**. A documentation section is its own node — not a whole file, a section. That granularity is what lets the mesh deliver one section to an agent instead of the entire document, and it is why [DocJSON](docjson.md) documents are stored as section-level nodes rather than opaque markdown blobs.

Every node carries a small, fixed set of fields:

| Field | What it holds |
|---|---|
| **ID** | Unique identifier such as `myproject::myproject.utils::parse_config` |
| **Type** | One of three structural types (see [ontology](ontology.md)) |
| **Subtype** | A finer label: `function`, `module`, `docjson`, `test`, `workflow`, `state_machine` |
| **Title** | A human-readable name |
| **Location** | The file path (and, for code, the line range) where the node lives |
| **Code hash** | A fingerprint of the node's body, used to detect change |
| **Summary** | A one-line description pulled from a docstring or heading |

For example, scanning a `utils.py` that contains two functions produces three nodes: a module node for the file, plus one function node each for the two functions. The line-range stored in a node's location is what lets `axiom_graph_source` hand back a single function by line range instead of the whole file — context reduction at the node level.

## Edges Carry Intent

Edges are what make the mesh more than a pile of nodes — and *intent-typed* edges are what make it more than a generic call graph.

A plain call graph records that function A references function B. It cannot tell you *why*. axiom-graph's edges declare the relationship's meaning as a first-class type: `documents`, `validates`, `annotates`, `delegates_to`, `composes`. These are not labels stapled onto a reference after the fact; they are the semantics the build enforces. The ontology specifies which node types may appear on each side of each edge, and **invalid edges fail the build** — so the mesh stays internally consistent.

The edges you will meet most often:

| Edge | Meaning | Example |
|---|---|---|
| **composes** | Structural containment — X contains Y | A module composes its functions; a doc file composes its sections |
| **documents** | Prose describes a node | A design section documents a function |
| **validates** | A test verifies a node | `test_parse_config` validates `parse_config` |
| **annotates** | A `@workflow`/`@task` envelope wraps a function | A workflow envelope annotates the wrapped function |
| **delegates_to** | Runtime invocation across a boundary | A pipeline function delegates to a utility in another module |
| **depends_on** | Import or execution dependency | A module depends on another it imports |

**Why a generic call graph can't do this.** Because the edge type is known, each read can treat each edge differently. Intent-scoped retrieval follows `documents` and `validates` to pull in the right spec and tests; it follows `delegates_to` to find what actually runs. Drift propagation, meanwhile, respects the *meaning* of each edge rather than uniform graph distance: `validates` propagates one hop (production → its test), `documents` propagates transitively (doc → doc → code), and `annotates` propagates one hop (envelope → function). A call graph has only one kind of edge, so it can do none of this. The full edge catalog and the rules that govern it live in the [ontology](ontology.md); how each type propagates drift is in [staleness](staleness.md).

## Node Identity and Naming

Every node has a stable ID built from parts separated by `::`:

```
{project_id}::{module_path}::{name}
```

- **project_id** — your project's name (e.g. `myproject`)
- **module_path** — the dotted path to the module (e.g. `myproject.utils`)
- **name** — the function, class, or section name (e.g. `parse_config`)

| Node | ID |
|---|---|
| The `utils` module | `myproject::myproject.utils` |
| A function in `utils.py` | `myproject::myproject.utils::parse_config` |
| A method `Model.fit` | `myproject::myproject.ml.model::Model.fit` |
| A test function | `myproject::tests.test_utils::test_parse_config` |
| A doc section | `myproject::docs.architecture::overview` |

Module-level nodes (files) have two parts; function- and section-level nodes have all three. This convention makes IDs predictable: an agent can construct the ID for a node it wants and query it directly, and the graph tools accept these IDs as traversal start points. IDs are how every read — retrieval or drift — addresses the mesh.

## How the Mesh Is Built

The mesh is assembled by a scanning pipeline, then queried. You rarely think about the pipeline day to day, but understanding it explains where edges come from.

1. **Scan.** Language-specific scanners walk the project. A Python AST scanner extracts functions, classes, modules, and imports; a tree-sitter scanner does the same for JS/TS (an optional install); a DocJSON scanner reads section-level documentation; a config scanner reads agent/config directories. An xstate scanner reads `createMachine({...})` declarations — evidence that the mesh extends to declarative frameworks, not just hand-written call sites.
2. **Create nodes.** Each discovered item becomes a node with a content hash (a fingerprint of its body) and a description hash (of its docstring or heading). Those hashes are how the next build knows what changed.
3. **Detect edges.** Scanners emit edges directly: `composes` from modules to their members, `depends_on` from imports, `delegates_to` from cross-module calls, `annotates` from `@workflow`/`@task` envelopes, `validates` from tests to the code they cover, and `documents` from the explicit links declared in DocJSON sections.
4. **Record staleness.** The build re-reads sources, recomputes hashes, and compares them to stored values — the read that powers [drift detection](staleness.md).

The first build (`axiom-graph init`) does a full scan. Later builds (`axiom-graph build`) are incremental: they use file modification times to skip unchanged files, so re-indexing stays fast even on large repos. All read paths — CLI, viz, and the MCP tools — go through the same query layer over `.axiom_graph/graph.db`; no logic lives outside it.

## Traversing the Mesh Instead of Grepping

Once the mesh exists, the cheapest way to understand a piece of code is to *traverse* to it rather than search for it. This is **intent-scoped retrieval** in practice — the core agent workflow, and where context reduction pays off.

The typical loop is three steps:

1. **Locate** a node by name or summary — a full-text search over the mesh returns node IDs, not file dumps.
2. **Traverse** outward from that node along its typed edges — one call returns the linked neighbours: the spec that documents it, the tests that validate it, the modules it delegates to. You can ask for inbound edges ("who calls this?") or outbound ("what does this depend on?").
3. **Read** only the nodes you chose — a single function by its line range, or a single doc section, instead of whole files.

Compare that to grep: a text search returns every literal hit across the repo, with no notion of *why* a match matters, forcing you to open and re-read files to sort signal from noise. Intent-scoped retrieval starts from meaning instead. Because edges are intent-typed, you can ask for *exactly* the relationship you care about — "the tests that validate this," "the doc that documents this" — and pull back a precise, focused bundle: the function, its tests, and the prose that describes it, and nothing else. That is the same forward read described in [two reads, one mesh](#two-reads-one-mesh), now in the agent's hands.

For how to point an agent at the mesh and run this loop over MCP, see [connect your agent](../get-started/connect-your-agent.md). For the human-facing equivalents (optional), see [use the CLI](../get-started/use-the-cli.md).
