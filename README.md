<!-- generated from axiom_graph::docs.consumer.readme @ cd1f13588e96; do not edit -->

# axiom-graph

[![PyPI version](https://img.shields.io/pypi/v/axiom-graph.svg)](https://pypi.org/project/axiom-graph/)
[![Python versions](https://img.shields.io/pypi/pyversions/axiom-graph.svg)](https://pypi.org/project/axiom-graph/)
[![Documentation](https://img.shields.io/readthedocs/axiom-graph.svg)](https://axiom-graph.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A local-first code intelligence layer built for AI agents.** axiom-graph scans your Python and TypeScript, then weaves your code, docs, tests, and workflows into a single typed *mesh* over the source — and keeps that mesh honest as the code changes.

The payoff is **context reduction**. Instead of re-reading a whole file to understand one function, an agent asks the mesh and gets back exactly what a careful reviewer would pull up: the function, the doc section that describes it, the tests that validate it, and the workflow that calls it — one focused bundle of context, not a file dump. And the moment any of those threads drifts out of sync with the code, the mesh says so.

## Quick Start

`pip install`, no SaaS and no account. A few commands take an unindexed repo to a live drift signal:

```bash
pip install axiom-graph[viz]

axiom-graph init .     # one-time: scan and build the full index
# ...edit a function that docs and tests describe...
axiom-graph build .    # incremental re-index
axiom-graph check .    # what fell out of sync?
```

`check` answers with exactly what drifted and why — the symbol you changed, the doc sections that document it, the tests that validate it, and any downstream page riding the chain:

```
own: 1 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 47 VERIFIED
```

That one line is the whole product: change code, and the mesh tells you what it knocked out of date.

## Why it's different

Most tools treat code, docs, and tests as separate worlds joined by prose references. axiom-graph treats them as one graph of **nodes** joined by **intent-typed edges** — a doc section *documents* a function, a test *validates* it, a workflow *delegates to* a task. That single choice buys two capabilities from one mesh:

- **Intent-scoped retrieval** — traverse the typed edges out from a node and land on exactly the linked docs, tests, and workflows. No grep, no guessing which file matters.
- **Drift detection** — when a body or docstring changes, axiom-graph hashes them separately and propagates the change *along the same edges*, marking only the genuinely affected nodes for re-verification. It follows edge semantics rather than firing on every caller, so staleness stays a trustworthy signal instead of noise.

A few more choices set it apart:

- **Atomic, modular docs (DocJSON).** Documentation is written in DocJSON, where every *section* is its own addressable node with stable links — an agent reads one section instead of a whole document.
- **An annotated semantic layer, not a full call graph.** Lightweight `@workflow` / `@task` / `Step` / `AutoStep` markers annotate the *orchestration highways* of your system with intent and order, giving a readable map without the noise of every helper call. The markers come from the companion `axiom-annotations` package (`pip install axiom-annotations`).
- **Local-first.** The whole mesh is a single SQLite database (`.axiom_graph/graph.db`) that lives in your repo. Free, open-source, no SaaS to sign up for.

## Three surfaces onto the mesh

The same mesh is reachable three ways:

- **MCP server — for agents.** The primary integration surface: search, graph traversal, source retrieval, staleness checks, and doc editing, all exposed as tools an agent calls directly over the Model Context Protocol. Setup is in **MCP Server Setup** below.
- **CLI — for humans.** `init`, `build`, and `check`, plus commands to explore the graph, inspect history, and export. `axiom-graph check --fail-on any` doubles as a CI gate that blocks stale docs from merging.
- **Viz dashboard — to browse.** `axiom-graph viz .` launches an interactive browser UI with graph and list views, a doc manager, a workflow explorer, and detail panels over every node.

Full guides for all three live in the [documentation](https://axiom-graph.readthedocs.io).

## Agents on the mesh: the PEV proof

The clearest evidence the model works is the **PEV Agent Nexus** — a Plan–Execute–Validate workflow where a chain of agents (Architect, Builder, Reviewer, Auditor) build real changes by reading and writing the mesh over MCP. They pull intent-scoped context to plan, traverse the graph to check callers before refactoring, and let the Auditor update the affected docs and record a verification snapshot so staleness clears cleanly. It is the thesis end to end: agents doing real work *because* the mesh gives them the right context and an honest drift signal. See the PEV overview in the [documentation](https://axiom-graph.readthedocs.io).

## Documentation

Full documentation — getting started, the core concepts (the mesh, ontology, DocJSON, annotations, staleness, history), the CLI, the viz dashboard, and the MCP tool reference — lives at:

**📖 [axiom-graph.readthedocs.io](https://axiom-graph.readthedocs.io)**

## MCP Server Setup

The MCP server is how AI agents interact with axiom-graph. It uses stdio transport.

**Claude Code** — create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "axiom-graph": {
      "type": "stdio",
      "command": "/path/to/your/venv/bin/python",
      "args": ["-m", "axiom_graph.mcp_server"]
    }
  }
}
```

**Any other client** — the entry point is `axiom-graph-mcp` (an installed console script) or `python -m axiom_graph.mcp_server`; both speak JSON-RPC over stdio with logs on stderr. VS Code (Copilot / Continue) configuration and logging options (`AXIOM_GRAPH_LOG_LEVEL`, `AXIOM_GRAPH_LOG_FILE`) are covered in the [documentation](https://axiom-graph.readthedocs.io).

## Links

- **Documentation:** https://axiom-graph.readthedocs.io
- **PyPI:** https://pypi.org/project/axiom-graph/
- **Source:** https://github.com/ddpoe/axiom-graph

## License

[MIT](LICENSE)
