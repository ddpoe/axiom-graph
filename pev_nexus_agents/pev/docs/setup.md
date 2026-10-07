<!-- generated from axiom_graph::docs/consumer/plugins/pev/setup @ e4c4fd77565d; do not edit -->

# PEV setup

How to install PEV, connect it to axiom-graph and check that it works. An agent setting PEV up for a user can run these steps in order.

## Install

### Before you install

- axiom-graph 3.0.0 or later in your project, with an index built and its MCP server connected to Claude Code (see Connect your agent in the [axiom-graph docs](https://axiom-graph.readthedocs.io)). Name the server `axiom-graph` in `.mcp.json`: PEV's agents and hooks call its tools as `mcp__axiom-graph__*`. The server must offer `axiom_graph_info`; PEV's agents call it to learn your project's id, docs folders and file extension instead of assuming them.
- bash and jq. PEV's hooks are bash scripts that read their input with jq. On Windows, bash is Git Bash from [Git for Windows](https://gitforwindows.org/). jq is not installed by default on Ubuntu and is not bundled with Git for Windows:

```bash
command -v jq || echo "jq missing"
# Windows: winget install jqlang.jq
# Ubuntu/Debian: sudo apt install jq
# macOS: brew install jq
```

Restart Claude Code after installing jq so the hooks find it.

- A git identity. PEV commits during every run (the change, the merge and a completion commit), and a commit fails on a machine with no `user.name` and `user.email` set. Check with `git config user.name` and `git config user.email`, and set them if either prints nothing:

```bash
git config --global user.name "Your Name"
git config --global user.email "you@example.com"
```

### Install the plugin

```bash
claude plugin marketplace add ddpoe/axiom-graph
claude plugin install pev@axiom-graph
claude plugin install hook-spike@axiom-graph   # optional
```

`hook-spike` is a small companion plugin. Its `/hs-heartbeat` skill checks in a few seconds whether plugin hooks fire at all, which tells a PEV problem apart from a Claude Code hook problem.

### Seed PEV's files into your project (required)

PEV creates each cycle's docs by cloning templates kept in your project's `.pev/templates/`, so `.pev` has to be one of axiom-graph's docs folders. PEV's own run records (cycle, instance and audit docs, and efficiency reports) and closed requests are frozen, so they never count in `check`. An open request stays live, so it is flagged when the code it describes changes. The plugin's `scripts/pev-seed.sh` sets both up.

Build the index first: `axiom-graph init .` builds it and writes `axiom-graph.toml` with your project id. Then run the script from your project root:

```bash
PEV_DIR=$(claude plugin list --json | jq -r '.[] | select(.id=="pev@axiom-graph") | .installPath')
bash "$PEV_DIR"/scripts/pev-seed.sh
```

This finds the copy `claude plugin list` reports. If you load PEV with `--plugin-dir`, or an older disabled copy is still installed, set `PEV_DIR` to the plugin folder you actually run.

If you run axiom-graph through a tool, pass the command with `--axiom-graph`, for example `--axiom-graph "poetry run axiom-graph"`.

The script does three things:

- **Completes `axiom-graph.toml`.** It appends whichever of these tables the file lacks:

  ```toml
  [axiom_graph.scan]
  docs_dirs = ["docs", ".pev"]

  [axiom_graph.staleness]
  frozen_tags = ["pev-cycle", "pev-instance", "pev-efficiency", "pev-audit-dev-docs", "pev-audit-consumer-docs", "pev-audit-annotations", "completed", "superseded", "archived"]
  ```

  With no `axiom-graph.toml` at all, it creates one with the project id your index stores. It never rewrites a table you already have. If your `[axiom_graph.scan]` table sets `docs_dirs` without `.pev`, it prints the exact edit and stops; make the edit and run it again. If your `[axiom_graph.staleness]` table lacks some of these tags, it prints the ones to add and carries on. The tag list is a default, so edit it as you like. If you set these as dotted or inline keys (`scan.docs_dirs = ...`, `scan = { ... }`), the script never edits them: it shows the lines, stops if `docs_dirs` there lacks `.pev`, and otherwise carries on.
- **Copies PEV's files.** Each template your project is missing goes into `.pev/templates/`, and the `doc-topology` and `test-policy` SOPs go into `.pev/` if you have none. It never overwrites a file, so you can run it again after a plugin update and keep your edits.
- **Builds the index and accepts the files it copied**, so the build doesn't report them as edits made outside the doc tools. A copied file you have since edited is left for you to review and accept. `--no-build` skips this step and prints the commands to run instead.

Then commit `axiom-graph.toml`, `docs/agent-policy.docjson` (written by `axiom-graph init`) and `.pev/`. Cycle worktrees are checked out from your commits, so they need all three. `/pev-cycle` stops at intake until the templates are in place.

PEV runs each cycle and instance in a git worktree under `.claude/worktrees/`. Add that folder to your project's `.gitignore`; otherwise `git status` on main shows `?? .claude/` while a run is open, and a `git add -A` then would stage the worktree:

```
.claude/worktrees/
```

### Customize the SOPs (optional)

PEV reads optional DocJSON SOP files from `.pev/`. The two the seed script copies, `doc-topology` and `test-policy`, hold PEV's defaults; edit them to fit your project. Each one explains what its sections control. Have your agent make the edits with axiom-graph's doc tools, so they stay verified.

A third SOP, `review-criteria`, adds project-specific checks to the Reviewer. Its template's checks are examples, so the script copies it only when you ask: run the script again with `--review-criteria`, then rewrite the checks before your next cycle. Without it, the Reviewer applies only its generic checks.

[user-guide.md](user-guide.md) describes these files and the optional architecture policy, which you write yourself.

### Check the install

- `/hs-heartbeat`, if you installed hook-spike, confirms that plugin hooks fire. It does not check jq, because hook-spike's hooks don't use it.
- `/pev-spike` checks for jq, then runs 13 tests of PEV's hooks in a temporary worktree. Expect 13/13 to pass.
- `/pev-instance` on a small real task checks the everyday path.

### Recommended Claude Code settings

Two worktree settings in `.claude/settings.json` suit PEV:

```json
{
  "worktree": {
    "baseRef": "head",
    "bgIsolation": "none"
  }
}
```

- `baseRef: "head"` makes a new worktree branch from your local HEAD. Claude Code's default, `fresh`, branches from `origin/<default-branch>`, which leaves out commits you have not pushed. If a cycle's worktree base still differs from your local HEAD, `/pev-cycle` fast-forwards it or stops and tells you.
- `bgIsolation: "none"` lets a `/pev-cycle` or `/pev-instance` running in the background write to main after it leaves its worktree, which only the merge step needs. Everything before it, the audit included, runs inside the worktree.

### Dependencies in a fresh worktree

`/pev-cycle` and `/pev-instance` build in a new git worktree, which starts without your installed dependencies. Intake runs your project's install command in it, such as `npm ci` or `poetry install`: the `install` key of `.pev/sops.toml` (next section), or the command detected from your project files.

### Project commands in `.pev/sops.toml` (optional)

PEV's agents install dependencies, run your tests and call the axiom-graph CLI with the commands in the `[commands]` table of `.pev/sops.toml`. The file is optional. Without it, or for a key it lacks, each run detects the command once from your project files (its manifest and lockfile), and a cycle records what it detected on a `Detected commands:` line in its manifest. Writing the file saves that detection and makes every run use the same commands.

You write this file by hand; no PEV agent writes it. Commit it with `.pev/`, since runs read it from their worktree. An example for a Poetry project using pytest:

```toml
[commands]
install = "poetry install"
test = "poetry run pytest -q"
test_parallel = "poetry run pytest -q -n auto"
test_targeted = "poetry run pytest -q {target}"
test_expected_seconds = 300
axiom_graph = "poetry run axiom-graph"
```

| Key | What PEV uses it for |
|---|---|
| `install` | Installing dependencies in a new worktree, so the scanners and the tests run as they do on main. |
| `test` | The full test suite, run serially. |
| `test_parallel` | The full suite in one parallel run, such as pytest-xdist's `-n auto`. Used instead of `test` when set. |
| `test_targeted` | One test file or test id. `{target}` is replaced with it. |
| `test_expected_seconds` | How long a serial full run takes. Without `test_parallel`, a suite over about 540 seconds is run in two halves, each under Claude Code's 10-minute command limit. |
| `axiom_graph` | The axiom-graph command line, for the steps with no MCP tool (such as `history checkpoint`) and in the seed-script command PEV prints. |

Every key is optional; set the ones your project needs.

## Common setup issues

### PEV agent tool calls are denied with "PEV guardrails need jq"

jq isn't on the PATH Claude Code runs hooks with. Install it and restart Claude Code. If `command -v jq` works in your terminal but the hooks still fail, jq is only on your interactive shell's PATH, for example added by conda or a profile script. This is the check PEV runs before a cycle:

```bash
bash --noprofile --norc -c 'command -v jq'
```

### "Plugin "pev" is disabled"

Run `claude plugin enable pev@axiom-graph`. If it stays disabled, look for `"pev@axiom-graph": false` under `enabledPlugins` in `<project>/.claude/settings.local.json`. That project setting overrides your user setting; set it to `true` or remove it.

### "Plugin "pev" not found"

Refresh the marketplace, then install again:

```bash
claude plugin marketplace update axiom-graph
claude plugin install pev@axiom-graph
```

### The installed version is older than expected

```bash
claude plugin marketplace update axiom-graph
claude plugin update pev@axiom-graph
claude plugin list
```

Restart Claude Code to load the update. Each release's entry in the plugin's [CHANGELOG](../CHANGELOG.md) names the axiom-graph version it needs and any migration step.

### Hooks don't seem to fire

Run `/hs-heartbeat` from the hook-spike plugin. If it fails, the problem is Claude Code's hooks rather than PEV; the hook-spike plugin's `TROUBLESHOOTING.md` lists the failure causes (section 7, and 8.1 for the heartbeat). On Windows, when you test with `claude -p` from Git Bash, set `MSYS_NO_PATHCONV=1` so Git Bash doesn't rewrite paths in the command.

### `/pev-instance` hangs while reading `.pev/` files

Check that every file in `.pev/` is well-formed DocJSON. A file that doesn't parse stops the SOP read.

### PEV shows up more than once, or an old version runs

A plugin installed at local or project scope in an earlier session stays registered next to your user-scope install. List the registrations in `~/.claude/plugins/installed_plugins.json`, and from each project that holds a stray one run `claude plugin uninstall pev --scope=<local|project>`. Old version folders under `~/.claude/plugins/cache/axiom-graph/pev/<version>/` are harmless; delete any except the active version's.


## Next steps

Run your first cycle with [user-guide.md](user-guide.md).
