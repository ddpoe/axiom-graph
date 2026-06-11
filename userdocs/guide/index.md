<!-- generated from axiom_graph::docs.consumer.index @ 484b4c7773cc; do not edit -->

# What is axiom-graph

## A code intelligence layer built for agents

AI agents now write code faster than anyone can read it. The bottleneck moved from *writing* to *comprehension* — and the casualty is everything that's supposed to explain the code: docs go stale the moment they're written, tests lag behind new behavior, and the reasoning behind a change lives in a chat transcript nobody will ever re-open.

axiom-graph attacks that gap. It is a **local-first code intelligence layer designed for AI agents**. It scans your Python and TypeScript source, weaves your docs, tests, and workflows into a single **typed mesh** over the code, and keeps that mesh honest as the code changes. An agent reads the mesh instead of re-reading your whole repository — and the same mesh tells it the moment any of those threads has drifted out of sync.

The payoff is **context reduction**. Instead of loading a whole file to understand one function, an agent asks the mesh for that function and gets back exactly what a careful human would pull up to review the change: the function body, the design-spec section that describes it, the tests that validate it, and the workflow that calls it. One focused bundle of context — not a file dump, not a wall of grep matches.

## See it work: install to drift signal

axiom-graph is `pip install`, no SaaS and no account. Four commands take an unindexed repo to a live drift signal:

```bash
pip install axiom-graph
axiom-graph init .     # one-time: full first index
# ...edit a function that docs and tests describe...
axiom-graph build .    # incremental re-index
axiom-graph check .    # what fell out of sync?
```

`check` answers with exactly what drifted and why — the function you changed, the doc sections that document it, the tests that validate it, and any downstream consumer page riding the chain, each tagged with the symbol that moved:

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 47 VERIFIED
```

That one line is the whole product: change code, and the mesh tells you what it knocked out of date. The rest of this page is *why* that works; the [reporting pipeline](examples/reporting-pipeline.md) walks the full loop on a real module.

## The typed mesh is the product

Most tools treat code, docs, and tests as separate worlds joined by prose references. axiom-graph treats them as one graph of **nodes** joined by **intent-typed edges**. A doc section *documents* a function. A test *validates* it. A `@workflow` decorator *annotates* it. An orchestration step *delegates_to* a task. The edge carries meaning, not just a pointer.

That single design choice gives you two capabilities for the price of one — they are two reads against the same mesh:

- **Intent-scoped retrieval** — traverse the typed edges out from a node and you land on exactly the linked docs, tests, and workflows. No grep, no guessing which file matters.
- **Drift detection** — when a code body or docstring changes, axiom-graph hashes them separately and propagates the change *along the same edges* to every linked doc section, test, and workflow, marking them for re-verification.

Crucially, propagation follows the *semantics* of each edge rather than firing on every caller. A full call graph would mark thousands of nodes stale on one change and create a backlog nobody reviews. axiom-graph propagates `validates` one hop, chains `documents` transitively, and guards `delegates_to` against cycles — so [staleness](concepts/staleness.md) stays a trustworthy signal instead of noise. The mesh isn't a feature on the side; it *is* the product.

## What makes it different

A few choices set axiom-graph apart from "a search index" or "a doc linter":

- **Atomic, modular docs.** Documentation is written in DocJSON, where every *section* is its own addressable node with stable links. You document at function and section granularity, and an agent can read one section instead of a whole document — context reduction applied to the docs themselves. See [DocJSON](concepts/docjson.md).
- **An annotated semantic layer, not a full call graph.** Lightweight `@workflow`/`@task` decorators and `Step`/`AutoStep` markers annotate the *orchestration highways* of your system with step names and intent. You get a readable map of what runs in what order and why — without the noise of every helper call. See [annotations](concepts/annotations.md).
- **Staleness as a read on the mesh.** Drift isn't bolted on; it's the engine that keeps the mesh trustworthy. When code changes, the affected nodes light up, and a verification snapshot clears them once a human or agent confirms they're back in sync.
- **Agent-native via MCP.** The MCP server is the primary integration surface — search, graph traversal, source retrieval, staleness checks, and doc editing all exposed as tools an agent can call directly. The human [CLI](get-started/use-the-cli.md) and the [visualization dashboard](viz.md) are the secondary, human-facing path onto the same mesh.

The storage is a single local SQLite database (`.axiom_graph/graph.db`). It's free, open-source, and there's no SaaS to sign up for — the mesh lives in your repo.

## The middle layer between reference and prose

Two kinds of documentation already wrap every codebase, and both miss the part that matters. **API reference** is autogenerated from signatures — complete at the symbol level, but it never says *why* anything exists or how the pieces fit. **Product prose** describes capabilities for a reader, but it drifts from the code the moment either one changes. The layer in between — *how these functions actually work together, and the intent behind each hop* — normally lives nowhere but a senior developer's head, and walks out the door when they do.

axiom-graph **materializes that middle layer** and anchors it to the code. Its scaffolding is the **axiom-annotations** schema — the `@workflow` / `@task` / `Step` / `AutoStep` markers (a purpose-built, structured intent surface, *not* docstrings or scraped comments) that emit graph nodes and edges narrating the orchestration highways of your system. `documents` edges then aggregate that scaffolding into capability-level prose, so the middle layer is annotation-anchored rather than comment-mined. See [the semantic layer](concepts/annotations.md) for the markers and [the mesh](concepts/the-mesh.md) for how the edges bind it to code.

**One layer, two audiences — with the same need.** An AI agent traverses the middle layer over [MCP](get-started/connect-your-agent.md) to plan against the codebase without re-reading it. A human newcomer has the *identical* need, and reads the same layer by eye in the [viz dashboard](viz.md): browse the annotated highways in the Workflows tab, open a function and read the section that documents it — then **extend the layer yourself**, attaching provenance links with the link picker and editing sections right in the Docs tab. You see the veins through the codebase without a developer walking you through it.

And because every thread is anchored to code, the middle layer **rides the drift signal** instead of rotting the way a wiki does: when the code moves, the sections that describe it light up [stale](concepts/staleness.md) until someone re-verifies them. A materialized middle layer is only worth having if it stays honest — so honesty lives in the same mesh that holds it.

## Agents consume the mesh — the PEV proof

The clearest evidence that this works is the **PEV Agent Nexus**: a Plan–Execute–Validate workflow where a chain of agents (Architect, Builder, Reviewer, Auditor) build real changes by reading and writing the mesh through MCP. They pull intent-scoped context to plan, traverse the graph to check callers before refactoring, and — after merging — let the Auditor update the affected docs and record a verification snapshot so staleness clears cleanly.

It's the flagship demonstration of the thesis end to end: agents doing real work *because* the mesh gives them the right context and an honest drift signal, not despite the absence of one. See [PEV](pev/overview.md) for the full picture.

## Docs that stay honest (this site, dogfooded)

The page you're reading is itself a node in the mesh. Consumer docs link *through* a [dev-doc proxy](examples/docs-honesty-loop.md#the-proxy-linking-architecture) to the code they describe, so they ride that chain and stay stable even when symbols get renamed. Because `consumer` is a transitive tag, these published pages **inherit a stale signal whenever the underlying code drifts**.

That staleness is the cue to revisit the prose; a verification pass clears it; re-rendering the site republishes the corrected page. The documentation you're reading is maintained by the same drift-and-verify loop it describes — the honesty loop, dogfooded.

## Where to go next

Pick your entry point:

- **Understand the model.** Start with [the mesh](concepts/the-mesh.md), then the [ontology](concepts/ontology.md) of node and edge types, [DocJSON](concepts/docjson.md) for the doc format, [annotations](concepts/annotations.md) for the semantic layer, and [staleness](concepts/staleness.md) for the drift engine.
- **Get an agent on it.** [Connect your agent](get-started/connect-your-agent.md) to the MCP server, or drive the mesh yourself with the [CLI](get-started/use-the-cli.md). Tune behavior in [configuration](get-started/configuration.md).
- **See it work.** Read the [PEV overview](pev/overview.md) for agents building on the mesh, then the worked [reporting pipeline](examples/reporting-pipeline.md) and the [docs honesty loop](examples/docs-honesty-loop.md) examples.
- **Look at it.** Explore the graph visually in the [dashboard](viz.md).

```{toctree}
:maxdepth: 2

concepts/index
get-started/index
pev/index
examples/index
viz
```
