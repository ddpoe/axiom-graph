#!/bin/bash
# pev-seed.sh — set a project up for PEV: axiom-graph.toml, .pev/templates/ and the SOPs.
#
# Usage: bash pev-seed.sh [--project-root DIR] [--axiom-graph CMD] [--review-criteria] [--no-build]
#
# 1. axiom-graph.toml. .pev must be a docs root, since the seeded templates are
#    only usable once they are indexed, and PEV's run records and closed
#    requests must be frozen (frozen_tags), so they never count in `check`:
#    - no axiom-graph.toml: create one with [axiom_graph] project_id set to the
#      id the index already stores (read off an indexed node id with
#      `axiom-graph list`; never the folder name), [axiom_graph.scan]
#      docs_dirs = ["docs", ".pev"] and [axiom_graph.staleness] frozen_tags.
#      With no index, say so and stop: build first.
#    - a toml with no [axiom_graph.scan] table (what `axiom-graph init` writes):
#      append the table with docs_dirs = ["docs", ".pev"].
#    - a scan table whose docs_dirs lacks .pev: print the exact edit and exit 1
#      without changing anything. An existing table is never rewritten. Scan
#      settings written as dotted or inline keys (scan.docs_dirs = ...,
#      scan = { ... }) are never appended to either: unless that line lists
#      .pev, the script says to check it and exits 1.
#    - no [axiom_graph.staleness] table: append it with PEV's frozen_tags. A
#      table (or dotted / inline keys) lacking some of them gets the edit
#      printed, and seeding goes on. The list is a default: edit it freely.
#
# 2. Seed. Every plugin template that carries meta.template_version and is
#    missing from <project>/.pev/templates/ is copied there, mirroring the
#    plugin's templates/ layout (cycle/ included). The copy carries one
#    provenance stamp, meta.seeded_from: {source, version}, in place of
#    meta.template_version. The SOPs doc-topology and test-policy (and
#    review-criteria with --review-criteria) are copied to .pev/ unchanged when
#    the project has no .pev/<name>.* file. A file that already exists is never
#    touched, so a second run is a no-op and a locally edited file survives.
#    review-criteria is opt-in because its checks are examples the Reviewer
#    would enforce as written.
#
# 3. Index. A copied file is a DocJSON file written without the doc tools, which
#    a build reports as a raw DocJSON edit. So the script runs `CMD build .` and
#    then `CMD stamps accept . <ids>` for the sections of PEV's own files that
#    are still exactly as the plugin ships them (this run's copies, and earlier
#    copies a failed or skipped build left unaccepted). A file you edited is
#    never accepted. --no-build skips this step and prints the commands.
#    --axiom-graph names the CLI command line (default `axiom-graph`, e.g.
#    "poetry run axiom-graph").
#
# Then commit axiom-graph.toml, .pev/ and, if `axiom-graph init` wrote it,
# docs/agent-policy.docjson. The orchestrator clones cycle docs from the
# seeded templates with axiom_graph_clone_doc.
#
# --check and --diff (compare seeded copies against the plugin) are reserved and
# not implemented yet.
#
# Requires bash and jq, the same dependencies as the plugin's hooks.

set -u

usage() {
  echo "Usage: bash pev-seed.sh [--project-root DIR] [--axiom-graph CMD] [--review-criteria] [--no-build]" >&2
}

MODE=seed
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
AXIOM_GRAPH=axiom-graph
WITH_REVIEW_CRITERIA=0
BUILD=1

while [ $# -gt 0 ]; do
  case "$1" in
    --project-root)
      [ $# -ge 2 ] || { usage; exit 2; }
      PROJECT_ROOT="$2"
      shift 2
      ;;
    --axiom-graph)
      [ $# -ge 2 ] || { usage; exit 2; }
      AXIOM_GRAPH="$2"
      shift 2
      ;;
    --review-criteria)
      WITH_REVIEW_CRITERIA=1
      shift
      ;;
    --no-build)
      BUILD=0
      shift
      ;;
    --check|--diff)
      MODE="${1#--}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "pev-seed: unknown argument '$1'" >&2
      usage
      exit 2
      ;;
  esac
done

if [ "$MODE" != seed ]; then
  echo "pev-seed: --$MODE is not implemented yet; only seed mode is available." >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "pev-seed: jq is required (the PEV hooks need it too). Install jq and re-run." >&2
  exit 1
fi

if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
if [ ! -d "$PROJECT_ROOT" ]; then
  echo "pev-seed: project root '$PROJECT_ROOT' is not a directory." >&2
  exit 2
fi

# The CLI's first word is the program; $AXIOM_GRAPH is a command line, split on spaces on purpose.
ag_found() {
  command -v "${AXIOM_GRAPH%% *}" >/dev/null 2>&1
}
ag() {
  # shellcheck disable=SC2086
  (cd "$PROJECT_ROOT" && $AXIOM_GRAPH "$@")
}

# --- axiom-graph.toml -----------------------------------------------------------
TOML="$PROJECT_ROOT/axiom-graph.toml"
PEV_DOCS_DIRS='docs_dirs = ["docs", ".pev"]'
# Frozen by kind: PEV's run records -- cycle docs (and the spike manifest),
# instance checkins, efficiency reports and the three audit manifests. Each is
# a dated record of one run, written once.
# Frozen by status: a request closed as completed, superseded or archived. An
# open request stays live, so it is flagged when the code it describes moves.
PEV_FROZEN_TAGS="pev-cycle pev-instance pev-efficiency pev-audit-dev-docs pev-audit-consumer-docs pev-audit-annotations completed superseded archived"
frozen_list=""
for tag in $PEV_FROZEN_TAGS; do
  frozen_list="${frozen_list:+$frozen_list, }\"$tag\""
done
PEV_FROZEN_LINE="frozen_tags = [$frozen_list]"

# has_header NAME: the toml has an [axiom_graph.NAME] table header (a trailing
# comment allowed).
has_header() {
  tr -d '\r' < "$TOML" | grep -Eq \
    "^[[:space:]]*\[[[:space:]]*axiom_graph[[:space:]]*\.[[:space:]]*$1[[:space:]]*\][[:space:]]*(#.*)?$"
}

# key_lines NAME: the lines that set [axiom_graph.NAME] as dotted or inline keys
# (scan.docs_dirs = ..., scan = { ... }, axiom_graph.scan.docs_dirs = ...). A
# toml with these never gets the table appended, which would redefine it.
key_lines() {
  tr -d '\r' < "$TOML" | grep -E \
    "^[[:space:]]*(axiom_graph[[:space:]]*\.[[:space:]]*)?$1[[:space:]]*[.=]"
}

# table_value NAME KEY: "<line count>\t<value joined onto one line>" of KEY under
# the [axiom_graph.NAME] header; empty when the key is not set there (arrays may
# span lines).
table_value() {
  tr -d '\r' < "$TOML" | awk -v table="[axiom_graph.$1]" -v key="$2" '
    /^[[:space:]]*\[/ { t = $0; sub(/#.*/, "", t); gsub(/[[:space:]]/, "", t); in_table = (t == table); next }
    in_table && !got && $0 ~ ("^[[:space:]]*" key "[[:space:]]*=") { got = 1; lines = 1; val = $0; if (val ~ /\]/) { print lines "\t" val; exit }; next }
    got { lines++; val = val " " $0; if ($0 ~ /\]/) { print lines "\t" val; exit } }
  '
}

if [ ! -f "$TOML" ]; then
  # The project id is the one the index stores: the part before "::" in any node id.
  if ! ag_found; then
    echo "pev-seed: there is no axiom-graph.toml, and the axiom-graph CLI ('$AXIOM_GRAPH') was not found." >&2
    echo "Re-run with --axiom-graph \"<how this project runs it>\", e.g. --axiom-graph \"poetry run axiom-graph\"." >&2
    exit 1
  fi
  list_out=$(ag list . 2>&1)
  project_id=$(printf '%s\n' "$list_out" | tr -d '\r' \
    | awk '{ i = index($1, "::"); if (i > 1) { print substr($1, 1, i - 1); exit } }')
  if [ -z "$project_id" ]; then
    echo "pev-seed: there is no axiom-graph.toml, and no index to read the project id from." >&2
    echo "Build the index first (\`axiom-graph build .\` in $PROJECT_ROOT), then re-run this script." >&2
    echo "('$AXIOM_GRAPH list .' printed: $(printf '%s' "$list_out" | tr -d '\r' | tail -n 2))" >&2
    exit 1
  fi
  case "$project_id" in
    *[!A-Za-z0-9_.-]*)
      echo "pev-seed: unexpected project id '$project_id' read from the index; create axiom-graph.toml by hand." >&2
      exit 1
      ;;
  esac
  printf '[axiom_graph]\nproject_id = "%s"\n\n[axiom_graph.scan]\n%s\n\n[axiom_graph.staleness]\n%s\n' \
    "$project_id" "$PEV_DOCS_DIRS" "$PEV_FROZEN_LINE" > "$TOML"
  echo "created axiom-graph.toml: project_id = \"$project_id\" (the id the index stores), $PEV_DOCS_DIRS, $PEV_FROZEN_LINE"
else
  append=""
  scan_keys=$(key_lines scan)
  if [ -n "$scan_keys" ] && ! has_header scan; then
    if ! printf '%s' "$scan_keys" | grep -Eq "[\"']\\.pev/?[\"']"; then
      echo "pev-seed: axiom-graph.toml sets the [axiom_graph.scan] settings as inline or dotted keys:" >&2
      printf '%s\n' "$scan_keys" | sed 's/^/  /' >&2
      echo "This script does not edit those. Make sure docs_dirs there includes \".pev\" ($PEV_DOCS_DIRS)," >&2
      echo "so the seeded templates are indexed, then re-run this script." >&2
      exit 1
    fi
  elif ! has_header scan; then
    append="${append}"$'\n'"[axiom_graph.scan]"$'\n'"$PEV_DOCS_DIRS"$'\n'
  else
    docs_dirs_value=$(table_value scan docs_dirs)
    if [ -z "$docs_dirs_value" ]; then
      echo "pev-seed: axiom-graph.toml sets no docs_dirs, so only docs/ is indexed and the seeded templates would not be." >&2
      echo "Add this line under [axiom_graph.scan] in axiom-graph.toml:" >&2
      echo "  $PEV_DOCS_DIRS" >&2
      echo "Then re-run this script. It does not rewrite a table axiom-graph.toml already has." >&2
      exit 1
    fi
    dd_lines="${docs_dirs_value%%$'\t'*}"
    dd_val="${docs_dirs_value#*$'\t'}"
    if ! printf '%s' "$dd_val" | grep -Eq "[\"']\\.pev/?[\"']"; then
      echo "pev-seed: docs_dirs in axiom-graph.toml does not list .pev, so the seeded templates would not be indexed." >&2
      echo "Edit [axiom_graph.scan] in axiom-graph.toml:" >&2
      echo "  now:    $dd_val" >&2
      if [ "$dd_lines" = 1 ] && printf '%s' "$dd_val" | grep -Eq '^[^#]*\][[:space:]]*(#.*)?$'; then
        if printf '%s' "$dd_val" | grep -Eq '\[[[:space:]]*\]'; then
          new_val=$(printf '%s' "$dd_val" | sed -E 's/\[[[:space:]]*\]/[".pev"]/')
        else
          new_val=$(printf '%s' "$dd_val" | sed -E 's/[[:space:]]*\]([[:space:]]*(#.*)?)$/, ".pev"]\1/')
        fi
        echo "  change: $new_val" >&2
      else
        echo "  change: add \".pev\" to the docs_dirs list" >&2
      fi
      echo "Then re-run this script. It does not rewrite a table axiom-graph.toml already has." >&2
      exit 1
    fi
  fi

  staleness_keys=$(key_lines staleness)
  if [ -n "$staleness_keys" ] && ! has_header staleness; then
    missing=""
    for tag in $PEV_FROZEN_TAGS; do
      if ! printf '%s' "$staleness_keys" | grep -Eq "[\"']${tag}[\"']"; then
        missing="${missing:+$missing, }\"$tag\""
      fi
    done
    if [ -n "$missing" ]; then
      echo "pev-seed: note: axiom-graph.toml sets the [axiom_graph.staleness] settings as inline or dotted keys:" >&2
      printf '%s\n' "$staleness_keys" | sed 's/^/  /' >&2
      echo "Make sure frozen_tags there includes: $missing" >&2
      echo "Seeding goes on; this script does not edit those keys." >&2
    fi
  elif ! has_header staleness; then
    append="${append}"$'\n'"[axiom_graph.staleness]"$'\n'"$PEV_FROZEN_LINE"$'\n'
  else
    frozen_value=$(table_value staleness frozen_tags)
    frozen_val="${frozen_value#*$'\t'}"
    missing=""
    for tag in $PEV_FROZEN_TAGS; do
      if [ -z "$frozen_value" ] || ! printf '%s' "$frozen_val" | grep -Eq "[\"']${tag}[\"']"; then
        missing="${missing:+$missing, }\"$tag\""
      fi
    done
    if [ -n "$missing" ]; then
      echo "pev-seed: note: PEV's run records and closed requests are not all frozen, so they count in \`check\`." >&2
      if [ -z "$frozen_value" ]; then
        echo "Add this line under [axiom_graph.staleness] in axiom-graph.toml:" >&2
        echo "  $PEV_FROZEN_LINE" >&2
      else
        echo "Add these tags to frozen_tags under [axiom_graph.staleness] in axiom-graph.toml: $missing" >&2
      fi
      echo "Seeding goes on; this script does not rewrite a table axiom-graph.toml already has." >&2
    fi
  fi

  if [ -n "$append" ]; then
    # A new table at the end of the file cannot change what the tables above it hold.
    if [ -s "$TOML" ] && [ -n "$(tail -c 1 "$TOML")" ]; then
      append=$'\n'"$append"
    fi
    printf '%s' "$append" >> "$TOML"
    printf '%s' "$append" | grep -q 'axiom_graph.scan' && echo "added to axiom-graph.toml: [axiom_graph.scan] $PEV_DOCS_DIRS"
    printf '%s' "$append" | grep -q 'axiom_graph.staleness' && echo "added to axiom-graph.toml: [axiom_graph.staleness] $PEV_FROZEN_LINE"
  fi
fi

# --- seed .pev/templates/ and the SOPs ------------------------------------------
PLUGIN_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC="$PLUGIN_ROOT/templates"
DEST="$PROJECT_ROOT/.pev/templates"
PEV_JQ='select(.meta.template_version | type == "string")
        | .meta.seeded_from = {source: $source, version: .meta.template_version}
        | del(.meta.template_version)'

seeded=0
kept=0
failed=0
# Doc paths (no extension) of PEV's files that are still exactly as seeded, so
# their raw-edit flags are PEV's own text, never the user's.
pristine=()

# Templates sit at most one directory deep (templates/*.docjson, templates/cycle/*.docjson).
for f in "$SRC"/*.docjson "$SRC"/*/*.docjson; do
  [ -f "$f" ] || continue
  # Cheap pre-filter (jq start-up is slow on Windows); jq below decides for real.
  grep -q '"template_version"' "$f" || continue

  rel="${f#"$SRC"/}"
  target="$DEST/$rel"
  if [ -e "$target" ]; then
    echo "kept    .pev/templates/$rel (already present)"
    kept=$((kept + 1))
    # Unstamped and byte-identical to a fresh seed: a copy no build has accepted yet.
    if ! grep -q '"axiom_stamp"' "$target" \
        && jq --arg source "pev/templates/$rel" "$PEV_JQ" "$f" 2>/dev/null | cmp -s - "$target"; then
      pristine+=(".pev/templates/${rel%.docjson}")
    fi
    continue
  fi

  mkdir -p "$(dirname "$target")"
  if jq -e --arg source "pev/templates/$rel" "$PEV_JQ" "$f" > "$target.tmp" 2>/dev/null; then
    mv "$target.tmp" "$target"
    echo "seeded  .pev/templates/$rel"
    seeded=$((seeded + 1))
    pristine+=(".pev/templates/${rel%.docjson}")
  else
    rm -f "$target.tmp"
    rmdir "$(dirname "$target")" 2>/dev/null
    echo "FAILED  .pev/templates/$rel (no string meta.template_version in $f, or not valid JSON)" >&2
    failed=$((failed + 1))
  fi
done

sops="doc-topology test-policy"
[ "$WITH_REVIEW_CRITERIA" = 1 ] && sops="$sops review-criteria"
for sop in $sops; do
  src="$SRC/$sop.docjson"
  target="$PROJECT_ROOT/.pev/$sop.docjson"
  existing=$(ls -d "$PROJECT_ROOT/.pev/$sop".* 2>/dev/null | head -n 1)
  if [ -n "$existing" ]; then
    echo "kept    .pev/${existing##*/} (already present)"
    kept=$((kept + 1))
    if [ "$existing" = "$target" ] && cmp -s "$src" "$target"; then
      pristine+=(".pev/$sop")
    fi
    continue
  fi
  mkdir -p "$PROJECT_ROOT/.pev"
  if cp "$src" "$target"; then
    echo "seeded  .pev/$sop.docjson"
    seeded=$((seeded + 1))
    pristine+=(".pev/$sop")
  else
    echo "FAILED  .pev/$sop.docjson (could not copy $src)" >&2
    failed=$((failed + 1))
  fi
done

echo "pev-seed: $seeded seeded, $kept already present."
[ "$failed" -eq 0 ] || exit 1

# --- build, then accept PEV's own unedited files --------------------------------
if [ "${#pristine[@]}" -eq 0 ]; then
  echo "Next: commit axiom-graph.toml, .pev/ and, if axiom-graph init wrote it, docs/agent-policy.docjson."
  exit 0
fi

if [ "$BUILD" = 0 ]; then
  echo "Skipped the build (--no-build). Next: run \`$AXIOM_GRAPH build .\`, then accept the sections of the"
  echo "files above with \`$AXIOM_GRAPH stamps accept . <section ids>\` (\`stamps accept . --list\` lists them),"
  echo "or re-run this script without --no-build, which does both. Then commit axiom-graph.toml, .pev/"
  echo "and, if axiom-graph init wrote it, docs/agent-policy.docjson."
  exit 0
fi

if ! ag_found; then
  echo "pev-seed: the axiom-graph CLI ('$AXIOM_GRAPH') was not found, so the index was not built." >&2
  echo "Re-run with --axiom-graph \"<how this project runs it>\", e.g. --axiom-graph \"poetry run axiom-graph\"." >&2
  exit 1
fi

if ! build_out=$(ag build . 2>&1); then
  echo "pev-seed: \`$AXIOM_GRAPH build .\` failed:" >&2
  printf '%s\n' "$build_out" | tr -d '\r' | tail -n 5 >&2
  echo "Fix it and re-run this script: it builds and accepts PEV's unedited files again." >&2
  exit 1
fi
echo "built the index ($AXIOM_GRAPH build .)"

if ! list_out=$(ag stamps accept . --list 2>&1); then
  echo "pev-seed: \`$AXIOM_GRAPH stamps accept . --list\` failed:" >&2
  printf '%s\n' "$list_out" | tr -d '\r' | tail -n 5 >&2
  echo "Fix it and re-run this script: it builds and accepts PEV's unedited files again." >&2
  exit 1
fi
ids=$(printf '%s\n' "$list_out" | tr -d '\r' | awk -v paths="${pristine[*]}" '
  BEGIN { n = split(paths, p, " ") }
  {
    id = $1; i = index(id, "::"); if (i == 0) next
    rest = substr(id, i + 2)
    for (k = 1; k <= n; k++) if (index(rest, p[k] "::") == 1) { print id; break }
  }')
if [ -z "$ids" ]; then
  echo "No section of PEV's files is flagged as a raw DocJSON edit."
else
  # The CLI records these as verified_by="human": `stamps accept` has no option
  # to name the verifier. They are the plugin's own text, accepted unread.
  # shellcheck disable=SC2086
  if ! accept_out=$(ag stamps accept . $ids 2>&1); then
    echo "pev-seed: \`$AXIOM_GRAPH stamps accept\` failed:" >&2
    printf '%s\n' "$accept_out" | tr -d '\r' | tail -n 5 >&2
    exit 1
  fi
  echo "accepted $(printf '%s\n' "$ids" | wc -l | tr -d ' ') section(s) of PEV's own files (stamped as written by the plugin)."
fi
echo "Next: commit axiom-graph.toml, .pev/ and, if axiom-graph init wrote it, docs/agent-policy.docjson."
exit 0
