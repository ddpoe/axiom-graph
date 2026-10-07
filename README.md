<!-- generated from axiom_graph::docs/consumer/readme @ 3556ee605af8; do not edit -->

# axiom-graph

[![PyPI version](https://img.shields.io/pypi/v/axiom-graph.svg)](https://pypi.org/project/axiom-graph/)
[![Python versions](https://img.shields.io/pypi/pyversions/axiom-graph.svg)](https://pypi.org/project/axiom-graph/)
[![Documentation](https://img.shields.io/readthedocs/axiom-graph.svg)](https://axiom-graph.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/ddpoe/axiom-graph/blob/master/LICENSE)

axiom-graph indexes a project's code, docs and tests into one graph, and when the code changes it tells you which docs and tests are now out of date. It is built for AI coding agents: its MCP server lets an agent search the project, read one function or one doc section at a time, follow the links between code, docs and tests, and update the docs a change made stale. A CLI and a browser dashboard give people the same view.

It runs locally. Install it with pip; there is no account, and the index is a SQLite file in your project.

## Quick start

Requires Python 3.10 or later. From your project's root:

```bash
pip install axiom-graph
axiom-graph init .      # scan the project and build the index
```

Change the body of a function that one of your tests calls, then run:

```bash
axiom-graph check .
```

```
own: 2 CONTENT_UPDATED / 0 DESC_UPDATED / 0 RENAMED / 0 NOT_FOUND · link: 2 LINKED_STALE / 0 BROKEN_LINK · 0 VERIFIED

NODE                                              OWN_STATUS       LINK_STATUS
----------------------------------------------------------------------------------------
myproject::reports                                CONTENT_UPDATED  VERIFIED
myproject::reports::build_report                  CONTENT_UPDATED  VERIFIED
myproject::tests.test_reports                     VERIFIED         LINKED_STALE
myproject::tests.test_reports::test_build_report  VERIFIED         LINKED_STALE  via myproject::reports::build_report
```

The edited function and its module are `CONTENT_UPDATED`, and the test that calls it is `LINKED_STALE`. Doc sections that link to the function are flagged the same way. After reviewing, run `axiom-graph mark-clean <node id> .` on the function and the test to clear them. Run `axiom-graph build .` when you add files, so they join the index.

## How it works

Every module, class, function, test, doc section and annotated workflow becomes a node, joined by typed edges. A test `validates` the functions it calls (found automatically), a doc section `documents` the code it links to, and a `@workflow` or `@task` marker `annotates` its function. When a function's body or docstring changes, the nodes linked to it become `LINKED_STALE` and stay that way until someone verifies them. Drift follows only these edges, not every caller.

- **Docs** are DocJSON files in your docs folders (`docs/` by default; `docs_dirs` in `axiom-graph.toml` changes it). Each section is its own node with its own links, so an agent can read one section instead of a whole document. Markdown docs are indexed too.
- **Languages:** Python out of the box. JavaScript and TypeScript need `pip install "axiom-graph[js]"` and a `js_paths` setting in `axiom-graph.toml`.
- **Workflow markers:** `@workflow`, `@task`, `Step` and `AutoStep` come from the `axiom-annotations` package. They name the steps of your main pipelines so the graph shows what runs in what order.

## Ways to use it

- **MCP server, for agents:** tools to search, read source and doc sections, follow edges, check drift, write, copy and link docs, and export workflows with their code to one page you can share. Setup is below.
- **CLI:** `init`, `build`, `check` and `mark-clean`, plus commands to explore the graph, show history, render docs and export workflows. `axiom-graph carry-forward` copies the verifications made in a merged git worktree into your main checkout's index. `axiom-graph check --fail-on stale .` exits 1 while anything is stale, for CI.
- **Dashboard:** `pip install "axiom-graph[viz]"`, then `axiom-graph viz .` opens a browser UI with Graph, List, Docs, Workflows and Tests tabs.

## Connect an agent

The server reads the index, so run `axiom-graph init .` first. It talks to the client over stdio. For Claude Code, create `.mcp.json` in your project root:

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

Point `command` at the Python in the virtualenv where axiom-graph is installed (`Scripts/python.exe` on Windows), then restart Claude Code. Other MCP clients take the same command, or the `axiom-graph-mcp` console script. If the client shows the server as failed, run the command by hand to see the error. Setup for other clients, logging and troubleshooting are in the [documentation](https://axiom-graph.readthedocs.io).

## PEV Agent Nexus

The PEV Agent Nexus is a Claude Code plugin, shipped from this repository, that runs code changes through Plan, Execute and Validate phases. Separate agents plan, implement, review, and update the docs the change made stale, with a human approval gate between phases. They work through the axiom-graph MCP tools. Installation and use are covered in the [documentation](https://axiom-graph.readthedocs.io).

## Documentation and links

- **Documentation:** https://axiom-graph.readthedocs.io (user guide, CLI reference and MCP tool reference)
- **PyPI:** https://pypi.org/project/axiom-graph/
- **Source:** https://github.com/ddpoe/axiom-graph
- **Changelog:** https://github.com/ddpoe/axiom-graph/blob/master/CHANGELOG.md

## License

[MIT](https://github.com/ddpoe/axiom-graph/blob/master/LICENSE)
