# PEV Setup

Everything a consumer project needs to go from "just installed the plugin" to "running `/pev-cycle` successfully."

If you're an agent reading this on behalf of a user who just said "we installed pev, now what?" — the install checklist below is what you run, in order. Each step has the concrete command.

## 1. Install

### 1a. Plugin install

Both plugins recommended:

```bash
claude plugin marketplace add ddpoe/axiom-graph
claude plugin install pev@axiom-graph
claude plugin install hook-spike@axiom-graph   # companion: plugin-hook debugging
```

**Why both?** `hook-spike` is a small companion plugin that gives you a 10-second smoke-test for plugin infrastructure (`/hs-heartbeat`) — invaluable when PEV misbehaves and you need to isolate whether the problem is in PEV itself or in the plugin-hook platform underneath. You can skip it and install later if needed, but the footprint is tiny and it pays for itself the first time something breaks.

### 1b. Cycle & instance locations

Your project's doc-serialization convention determines whether you need to pre-create directories:

**Nested-dir projects** — cycle manifests live at `docs/pev/cycles/<cycle-id>.json` and instance checkins at `docs/pev/instances/<id>.json`. Pre-create:

```bash
mkdir -p docs/pev/cycles docs/pev/instances
```

Commit `.gitkeep` files if you want empty dirs tracked.

**Flat-dotted projects** — axiom-graph serializes docs as `docs/<dotted-id>.json`. Cycle manifests land at `docs/pev.cycles.<cycle-id>.json` and instance checkins at `docs/pev.instances.<id>.json`. No directories to create; PEV writes the files directly under `docs/`.

**Not sure which you are?** Look at existing axiom-graph docs:
- `docs/features/<feature>/prd.json` → nested
- `docs/features.<feature>.prd.json` → flat-dotted

### 1c. Copy SOP templates (optional but recommended)

PEV reads three DocJSON SOPs from `.pev/` in your repo. If the files are absent, the plugin falls back to generic defaults shipped at `${CLAUDE_PLUGIN_ROOT}/templates/`. Copy into your repo to customize:

```bash
mkdir -p .pev

# Doc taxonomy — which doc categories the Auditor + Doc Reviewer work on
cp "$(claude plugin path pev@axiom-graph)/templates/doc-topology.json" .pev/

# Test policy — tier system, annotation contract, coverage expectations
cp "$(claude plugin path pev@axiom-graph)/templates/test-policy.json" .pev/

# Review criteria — project-specific code-review emphasis (optional)
cp "$(claude plugin path pev@axiom-graph)/templates/review-criteria.json" .pev/
```

If `claude plugin path` isn't available on your Claude Code version, the path is `~/.claude/plugins/cache/axiom-graph/pev/<version>/templates/`.

Edit each file to match your project's conventions. The templates are self-documenting — each section explains which skill reads the fields.

See [USER_GUIDE.md §Customizing via `.pev/` SOPs](./USER_GUIDE.md#customizing-via-pev-sops) for what to change and why.

### 1d. (Optional) Let axiom-graph index your SOPs

`axiom-graph.toml` has a `doc_dirs` key under `[axiom_graph.scan]` that defaults to `["docs"]`. To make your `.pev/` SOPs searchable via `axiom_graph_search` and trackable via `axiom_graph_history`, add `.pev` to the list:

```toml
[axiom_graph.scan]
doc_dirs = ["docs", ".pev"]
```

Then re-index:

```bash
axiom-graph build .
```

**Not required** — PEV skills read `.pev/*.json` directly via the Read tool regardless of indexing. Opt in when you want axiom-graph-native queries over your SOP history (who changed what, when, and why).

### 1e. Verify

Work through these in order until one fails or you get to the last:

#### Quick check — 10 seconds
```
/hs-heartbeat
```
Confirms plugin `hooks.json` fires at all. Expect a pass/fail matrix with both canaries (`PreToolUse` + `PostToolUse`) marked as fired. If this fails, the platform-level plugin infrastructure isn't working — see [`../hook-spike/TROUBLESHOOTING.md`](../hook-spike/TROUBLESHOOTING.md) §7 (failure catalog).

Requires `hook-spike` installed (step 1a).

#### Daily usage smoke — 2-5 minutes
```
/pev-instance fix a trivial thing (docstring, README typo, etc.)
```
Confirms `/pev-instance` dispatches, reads SOPs, writes a checkin, and commits. Good sanity check after any plugin upgrade. If this hangs at the SOP read step, check `${CLAUDE_PROJECT_DIR}/.pev/` files are well-formed DocJSON.

#### Total confirmation — 3-8 minutes
```
/pev-spike
```
Runs the 11-test PEV hook-infrastructure matrix — worktree scope, bash scope, axiom-graph scope, doc scope, budget warn/urgent/gate, non-allowlisted block, allowlist pass-through. Expect **11/11 pass**. Run this after a fresh install or when `/hs-heartbeat` passes but you still suspect PEV-specific hook logic is off.

## 2. Common setup issues

### "Plugin "pev" is disabled"

Check `<project>/.claude/settings.local.json` for an `enabledPlugins` override. A local `"pev@axiom-graph": false` will override a user-scope `true`. Flip to `true` or remove the override.

### "Plugin "pev" not found"

Run `claude plugin marketplace update axiom-graph` to refresh the marketplace cache, then retry the install.

### Hooks appear silent / `/hs-heartbeat` fails

Start with [`../hook-spike/TROUBLESHOOTING.md`](../hook-spike/TROUBLESHOOTING.md) §8.1 (plugin hook firing check) and §7 (failure catalog). Common culprits on Windows: `MSYS_NO_PATHCONV=1` missing on `claude -p` invocations, native Windows jq not on PATH.

### Multiple PEV versions showing up in `claude plugin list`

Old registrations from previous projects' local/project-scope installs can linger in `~/.claude/plugins/installed_plugins.json`. `cd` into each original project and run `claude plugin uninstall pev --scope=<local|project>` to clean up. The user-scope install remains.

### `claude plugin list` shows an older version than I expected

If `/pev-cycle` seems to resolve to stale behavior, the active install may be behind the marketplace:

```bash
claude plugin list                          # confirm active version
claude plugin marketplace update axiom-graph      # refresh marketplace cache
claude plugin update pev@axiom-graph              # pull latest
claude plugin list                          # reconfirm
```

Old cache dirs under `~/.claude/plugins/cache/axiom-graph/pev/` from prior installs are **harmless but can be removed** if you want a tidy cache. Keep only the directory matching your active version:

```bash
ls ~/.claude/plugins/cache/axiom-graph/pev/             # see everything on disk
rm -rf ~/.claude/plugins/cache/axiom-graph/pev/1.0.0    # remove a specific old version
```

Do not remove the directory matching the currently-registered version — Claude Code loads from it at runtime.

## 3. After setup, where to go

- **First `/pev-cycle`** → [USER_GUIDE.md §Typical walk-through](./USER_GUIDE.md#typical-walk-through)
- **Understanding what agent does what** → [DESIGN.md §Agent responsibilities](./DESIGN.md#agent-responsibilities-one-line-each)
- **Customizing deeper** → [USER_GUIDE.md §Customizing via `.pev/` SOPs](./USER_GUIDE.md#customizing-via-pev-sops)
- **Something broke** → [../hook-spike/TROUBLESHOOTING.md](../hook-spike/TROUBLESHOOTING.md)
- **Version history** → [../CHANGELOG.md](../CHANGELOG.md)
