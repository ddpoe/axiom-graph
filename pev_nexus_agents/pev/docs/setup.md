<!-- generated from axiom_graph::docs.consumer.plugins.pev.setup @ 24b87769be94; do not edit -->

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

**Flat-dotted projects** — axiom-graph serializes docs as `docs/<dotted-id>.json`. Cycle manifests land at `docs/pev.cycles.<cycle-id>.json` and instance checkins at `docs/pev.instances.<id>.json`. No directories to create; PEV writes the files directly under `docs/`.

### 1c. Copy SOP templates (optional but recommended)

PEV reads three DocJSON SOPs from `.pev/` in your repo. If the files are absent, the plugin falls back to generic defaults shipped at `${CLAUDE_PLUGIN_ROOT}/templates/`. Copy into your repo to customize:

```bash
mkdir -p .pev
cp "$(claude plugin path pev@axiom-graph)/templates/doc-topology.json" .pev/
cp "$(claude plugin path pev@axiom-graph)/templates/test-policy.json" .pev/
cp "$(claude plugin path pev@axiom-graph)/templates/review-criteria.json" .pev/
```

Edit each file to match your project's conventions. The templates are self-documenting — each section explains which skill reads the fields.

### 1d. Verify

Quick check (10 seconds): `/hs-heartbeat` confirms plugin `hooks.json` fires at all. Daily usage smoke (2-5 minutes): `/pev-instance fix a trivial thing`. Total confirmation (3-8 minutes): `/pev-spike` runs the 11-test PEV hook-infrastructure matrix; expect **11/11 pass**.

## 2. Common setup issues

### "Plugin "pev" is disabled"

Check `<project>/.claude/settings.local.json` for an `enabledPlugins` override. A local `"pev@axiom-graph": false` will override a user-scope `true`. Flip to `true` or remove the override.

### "Plugin "pev" not found"

Run `claude plugin marketplace update axiom-graph` to refresh the marketplace cache, then retry the install.

### `claude plugin list` shows an older version than I expected

If `/pev-cycle` seems to resolve to stale behavior, the active install may be behind the marketplace:

```bash
claude plugin list
claude plugin marketplace update axiom-graph
claude plugin update pev@axiom-graph
claude plugin list
```

## 3. After setup, where to go

- **First `/pev-cycle`** → [user-guide.md](user-guide.md)
- **Customizing deeper** → [user-guide.md](user-guide.md)
