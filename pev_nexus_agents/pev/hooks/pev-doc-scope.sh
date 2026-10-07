#!/bin/bash
# pev-doc-scope.sh — PreToolUse hook for PEV doc-write axiom-graph tools.
# Scopes doc writes by PEV subagents to the current cycle.
#
# Active ONLY when agent_type starts with "pev:" (a PEV subagent).
#
# axiom_graph_clone_doc is refused to every PEV subagent: the orchestrator is
# the only cloner (it creates the cycle docs before any scoped agent runs).
#
# Three layouts, chosen by .pev-state.json's "layout" field: "directory",
# "instance", or none (the legacy single-file manifest). A cycle finishes
# on the layout it started with.
#
# builder_docs (optional array of doc ids in .pev-state.json, written by the
# orchestrator at the plan gate from the pitch's required artifacts): in the
# directory and legacy layouts, pev:pev-builder may also write the sections
# of a doc whose id is listed there. It widens the Builder only, and only for
# section writes; write_doc and clone_doc stay refused to it.
#
# layout "directory" — the cycle is docs/pev/cycles/<cycle-id>/, seven docs,
# and cycle_doc_id names its manifest (…/<cycle-id>/manifest). Writes must
# target a doc in that directory, and each agent writes only the docs it owns:
#
#   architect      architect, manifest::scope (its one manifest section)
#   builder        builder
#   reviewer       review (and any review-shard-* doc, reserved for shards)
#   auditor        audit; anything OUTSIDE the cycle directory (live docs)
#   doc-reviewer   audit::doc-review and its subsections (its progress,
#                  findings and verdict); add_section in audit only under it
#   decisions      architect, builder — append-only (add_section)
#   friction       every agent, only under its own group — append-only
#
# manifest belongs to the orchestrator, which has no pev: agent_type and so
# never reaches this hook. On the append-only logs, update_section and
# patch_section are refused: an entry is a new section, never an edit.
# write_doc is refused, except an Auditor writing a doc outside the cycle.
# For a doc_file write the hook reads the file's id; if it can't read the
# file it refuses, since the target can't be shown to be outside the cycle.
#
# layout "instance" — a /pev-instance run; cycle_doc_id names the checkin doc.
# pev:pev-reviewer may write only checkin::review and its subsections,
# pev:pev-doc-reviewer only checkin::doc-review and its subsections, and
# every other pev: agent is denied.
#
# No layout field — the original single-file manifest: every section write
# must target cycle_doc_id exactly (or, for the Builder, a builder_docs doc);
# write_doc is refused; the Auditor is exempt.
#
# Batch edits (axiom-graph >= 3.0.0): an update_section / patch_section call
# with edits=[...] is checked item by item under the layout's rules, and one
# failing item denies the whole call. An item without a section_id, or an
# edits value that isn't an array, is refused. When a call sends edits and a
# top-level section_id, both are checked, so no target in it goes unchecked
# (the server rejects that mix anyway). edits that is null or [] counts as
# absent: the call is checked on its top-level section_id alone.
#
# In both layouts the audit-skill subagents (pev-audit-*) are exempt: they
# write their own audit manifests, and their agent-frontmatter allowlists are
# the enforcement layer.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool

# One jq pass over the input (jq start-up is slow on Windows).
AGENT_TYPE=""
TOOL=""
HOOK_CWD=""
SECTION_ID=""
DOC_ID=""
PARENT_ID=""
BATCH_PARENTS=""
BATCH_COUNT=0
WRITE_ID=""
DOC_FILE=""
WRITE_ROOT=""
EDITS_KIND=none
EDITS_IDS=""
EDITS_HOLE=""
# Batch edits (update_section / patch_section, axiom-graph >= 3.0.0):
#   EDITS_KIND  none (no edits key, null or []) | array | bad (not an array)
#   EDITS_IDS   each item's section_id, one per line
#   EDITS_HOLE  index of the first item without a usable section_id, else ""
eval "$(printf '%s' "$INPUT" | jq -r '
  (.tool_input // {}) as $t
  | ($t.edits | if type == "array" then . else [] end) as $e
  | @sh "AGENT_TYPE=\(.agent_type // "")",
    @sh "EDITS_KIND=\($t.edits | if . == null or . == [] then "none" elif type == "array" then "array" else "bad" end)",
    @sh "EDITS_IDS=\([$e[] | (if type == "object" then .section_id else null end) | (if type == "string" then . else "" end)] | join("\n"))",
    @sh "EDITS_HOLE=\([$e | to_entries[] | select((.value | type) != "object" or (.value.section_id | type) != "string" or (.value.section_id | test("^\\s*$|[\\r\\n]"))) | .key] | (first // "") | tostring)",
    @sh "TOOL=\(.tool_name // "")",
    @sh "HOOK_CWD=\(.cwd // "")",
    @sh "SECTION_ID=\($t.section_id // "" | tostring)",
    @sh "DOC_ID=\($t.doc_id // "" | tostring)",
    @sh "PARENT_ID=\($t.parent_id // "" | tostring)",
    @sh "BATCH_PARENTS=\([($t.sections // [])[]? | (.parent_id // "" | tostring)] | join("\n"))",
    @sh "BATCH_COUNT=\(($t.sections // []) | length)",
    @sh "WRITE_ID=\(($t.doc_json // {}) | (if type == "string" then (fromjson? // {}) else . end) | (.id? // "") | tostring)",
    @sh "DOC_FILE=\($t.doc_file // "" | tostring)",
    @sh "WRITE_ROOT=\($t.project_root // "" | tostring)"
' 2>/dev/null | tr -d '\r')"

case "$AGENT_TYPE" in
  pev:*) ;;
  *) exit 0 ;;
esac

if [ "$TOOL" = "mcp__axiom-graph__axiom_graph_clone_doc" ]; then
  echo "BLOCKED: PEV subagents do not clone documents. The orchestrator is the only cloner: it creates the cycle docs from .pev/templates/ before any subagent runs. Write the sections your role owns with axiom_graph_update_section, axiom_graph_patch_section or axiom_graph_add_section." >&2
  exit 2
fi

# Resolve .pev-state.json (lives at cwd root — set by EnterWorktree).
# Claude Code passes cwd as a Windows path on Windows (C:\...\foo); normalize
# to POSIX so file tests and path concatenation work in git-bash.
PROJECT_ROOT="$HOOK_CWD"
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
STATE_FILE="$PROJECT_ROOT/.pev-state.json"

LAYOUT=""
CYCLE_DOC_ID=""
BUILDER_DOCS=""
if [ -f "$STATE_FILE" ]; then
  eval "$(jq -r '@sh "LAYOUT=\(.layout // "" | tostring)", @sh "CYCLE_DOC_ID=\(.cycle_doc_id // "" | tostring)", @sh "BUILDER_DOCS=\(.builder_docs | if type == "array" then [.[] | select(type == "string")] | join("\n") else "" end)"' "$STATE_FILE" 2>/dev/null | tr -d '\r')"
fi

# builder_doc DOC_ID — true when the caller is the Builder and DOC_ID is one
# of the state file's builder_docs (exact match, one id per line).
builder_doc() {
  [ "$AGENT_TYPE" = "pev:pev-builder" ] && [ -n "$1" ] || return 1
  local d
  while IFS= read -r d || [ -n "$d" ]; do
    [ "$d" = "$1" ] && return 0
  done <<EOF
$BUILDER_DOCS
EOF
  return 1
}

# BUILDER_DOCS_NOTE is appended to the Builder's "may write" list when the
# state file lists deliverable docs, so a deny shows what is open to it.
BUILDER_DOCS_NOTE=""
if [ "$AGENT_TYPE" = "pev:pev-builder" ] && [ -n "$BUILDER_DOCS" ]; then
  BUILDER_DOCS_NOTE=", and the sections of the pitch's deliverable docs (builder_docs): ${BUILDER_DOCS//$'\n'/, }"
fi

# ITEM names the batch edit being checked, so a deny says which item failed.
ITEM=""
block() {
  echo "BLOCKED: $ITEM$1" >&2
  exit 2
}

# check_batch CHECK — a batch update_section / patch_section (edits=[...]).
# Runs CHECK (the layout's single-section check) on every edits[].section_id;
# the first item that fails denies the whole call. An edits value that isn't
# an array, or an item without a section_id, is refused. A top-level
# section_id sent alongside edits is checked too: the server rejects that mix,
# but the hook doesn't rely on it.
check_batch() {
  [ "$EDITS_KIND" = array ] \
    || block "edits must be a JSON array of edit objects, each with its own section_id; the doc-scope check can't read this call's targets"
  [ -z "$EDITS_HOLE" ] \
    || block "edits[$EDITS_HOLE] has no usable section_id (missing, empty or multi-line); every batch edit must name the full section id it writes"
  if [ -n "$SECTION_ID" ]; then
    ITEM="top-level section_id '$SECTION_ID': "
    "$1" "$SECTION_ID"
  fi
  local sid
  while IFS= read -r sid || [ -n "$sid" ]; do
    ITEM="batch edit '$sid': "
    "$1" "$sid"
  done <<EOF
$EDITS_IDS
EOF
  ITEM=""
}

# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------
directory_layout() {
  case "$AGENT_TYPE" in
    pev:pev-audit-consumer-discovery|pev:pev-audit-consumer-verifier|pev:pev-audit-dev-shard|pev:pev-audit-annotations-fixer) exit 0 ;;
  esac

  [ -n "$CYCLE_DOC_ID" ] || block "No cycle_doc_id in .pev-state.json — cannot verify doc scope"
  case "$CYCLE_DOC_ID" in
    */manifest) CYCLE_DIR="${CYCLE_DOC_ID%/manifest}" ;;
    *) block "layout is \"directory\" but cycle_doc_id '$CYCLE_DOC_ID' is not a cycle manifest (…/<cycle-id>/manifest) — cannot verify doc scope" ;;
  esac

  local role group owns
  case "$AGENT_TYPE" in
    pev:pev-architect)    role=architect;    group=architect
                          owns="architect, manifest::scope, decisions (add_section), friction::architect (add_section)" ;;
    pev:pev-builder)      role=builder;      group=builder
                          owns="builder, decisions (add_section), friction::builder (add_section)$BUILDER_DOCS_NOTE" ;;
    pev:pev-reviewer)     role=reviewer;     group=reviewer
                          owns="review (and review-shard-* docs), friction::reviewer (add_section)" ;;
    pev:pev-auditor)      role=auditor;      group=auditor
                          owns="audit and friction::auditor (add_section) inside the cycle directory, and any doc outside it" ;;
    pev:pev-doc-reviewer) role=doc-reviewer; group=doc-review
                          owns="audit::doc-review and its subsections, friction::doc-review (add_section)" ;;
    *)                    role="";           group=""
                          owns="no docs in the cycle directory" ;;
  esac
  local who="${AGENT_TYPE#pev:}"
  deny() {
    block "Doc-scope violation — $1. $who may write: $owns (cycle directory $CYCLE_DIR)."
  }

  local is_add=0
  [ "$TOOL" = "mcp__axiom-graph__axiom_graph_add_section" ] && is_add=1

  # dir_check_target DOC_ID SECTION — the per-section rules for one write.
  # Returns when the write is allowed; denies (exits) otherwise.
  dir_check_target() {
    local target="$1" section="$2" leaf
    [ -n "$target" ] || block "Could not extract doc_id from tool call"

    case "$target" in
      "$CYCLE_DIR"/*) leaf="${target#"$CYCLE_DIR"/}" ;;
      *)
        [ "$role" = auditor ] && return 0
        builder_doc "$target" && return 0
        deny "target '$target' is outside the current cycle directory"
        ;;
    esac

    append_only() {
      [ "$is_add" = 1 ] && return 0
      block "'$leaf' is an append-only log: ${TOOL#mcp__axiom-graph__} is refused on it, so no entry is ever overwritten. Add a new entry with axiom_graph_add_section(doc_id=\"$target\", $1section_id=\"<slug-without-dots>\", heading=..., content=...)."
    }

    case "$leaf" in
      manifest)
        if [ "$role" = architect ] && [ "$is_add" = 0 ] && [ "$section" = scope ]; then
          return 0
        fi
        deny "manifest belongs to the orchestrator (the Architect may write manifest::scope only)"
        ;;
      decisions)
        case "$role" in
          architect|builder) ;;
          *) deny "decisions is written by the Architect, the Builder and the orchestrator" ;;
        esac
        append_only ""
        return 0
        ;;
      friction)
        [ -n "$group" ] || deny "friction has no group for $who"
        append_only "parent_id=\"$group\", "
        local parents
        if [ "$BATCH_COUNT" -gt 0 ] 2>/dev/null; then
          parents="$BATCH_PARENTS"
        else
          parents="$PARENT_ID"
        fi
        local p
        while IFS= read -r p || [ -n "$p" ]; do
          case "$p" in
            "$group"|"$group".*) ;;
            *) deny "friction entries go under your own group: add_section(parent_id=\"$group\"), not parent_id=\"$p\"" ;;
          esac
        done <<EOF
$parents
EOF
        return 0
        ;;
      architect)
        [ "$role" = architect ] && return 0
        deny "architect belongs to the Architect"
        ;;
      builder)
        [ "$role" = builder ] && return 0
        deny "builder belongs to the Builder"
        ;;
      review|review-shard-*)
        [ "$role" = reviewer ] && return 0
        deny "$leaf belongs to the Reviewer"
        ;;
      audit)
        [ "$role" = auditor ] && return 0
        [ "$role" = doc-reviewer ] || deny "audit belongs to the Auditor and the Doc Reviewer"
        # The Doc Reviewer's part of audit is doc-review and its subsections;
        # the rest (impact-report, changes-summary, progress) is the Auditor's.
        if [ "$is_add" = 1 ]; then
          local aparents ap
          if [ "$BATCH_COUNT" -gt 0 ] 2>/dev/null; then
            aparents="$BATCH_PARENTS"
          else
            aparents="$PARENT_ID"
          fi
          while IFS= read -r ap || [ -n "$ap" ]; do
            case "$ap" in
              doc-review|doc-review.*) ;;
              *) deny "new audit sections go under doc-review: add_section(parent_id=\"doc-review\"), not parent_id=\"$ap\"" ;;
            esac
          done <<EOF
$aparents
EOF
          return 0
        fi
        case "$section" in
          doc-review|doc-review.*) return 0 ;;
        esac
        deny "audit::$section is the Auditor's; the Doc Reviewer writes audit::doc-review"
        ;;
      *)
        deny "'$leaf' is not one of the cycle's docs"
        ;;
    esac
  }

  # dir_check_section SECTION_ID — an update_section / patch_section target.
  dir_check_section() {
    local target="" section=""
    case "$1" in
      *::*::*) target="${1%::*}"; section="${1##*::}" ;;
    esac
    dir_check_target "$target" "$section"
  }

  case "$TOOL" in
    mcp__axiom-graph__axiom_graph_update_section|mcp__axiom-graph__axiom_graph_patch_section|mcp__axiom-graph__axiom_graph_delete_section)
      if [ "$EDITS_KIND" != none ]; then
        check_batch dir_check_section
      else
        dir_check_section "$SECTION_ID"
      fi
      exit 0
      ;;
    mcp__axiom-graph__axiom_graph_add_section)
      dir_check_target "$DOC_ID" ""
      exit 0
      ;;
    mcp__axiom-graph__axiom_graph_write_doc)
      if [ "$role" = auditor ]; then
        if [ -n "$DOC_FILE" ]; then
          # Resolve the way write_doc does: a relative path is under project_root.
          local f="$DOC_FILE" r="${WRITE_ROOT:-$PROJECT_ROOT}"
          if command -v cygpath >/dev/null 2>&1; then
            f=$(cygpath -u "$f"); r=$(cygpath -u "$r")
          fi
          case "$f" in /*) ;; *) f="$r/$f" ;; esac
          WRITE_ID=$(jq -er 'if type == "object" then (.id // "" | tostring) else error("not an object") end' "$f" 2>/dev/null) \
            || deny "could not read doc_file '$DOC_FILE' to check that it targets a doc outside the cycle; pass doc_json inline instead"
        fi
        # write_doc strips the id before using it, so compare the stripped id.
        WRITE_ID="${WRITE_ID#"${WRITE_ID%%[![:space:]]*}"}"
        WRITE_ID="${WRITE_ID%"${WRITE_ID##*[![:space:]]}"}"
        local rel="${CYCLE_DIR#*::}"
        case "$WRITE_ID" in
          "$rel"|"$rel"/*|"${rel#*/}"|"${rel#*/}"/*)
            deny "write_doc would replace a cycle doc ('$WRITE_ID'); edit its sections instead" ;;
        esac
        exit 0
      fi
      block "PEV subagents do not write whole documents. The cycle docs under '$CYCLE_DIR' already exist — edit the ones your role owns with axiom_graph_update_section, axiom_graph_patch_section or axiom_graph_add_section. $who may write: $owns."
      ;;
    *)
      exit 0
      ;;
  esac
}

if [ "$LAYOUT" = "directory" ]; then
  directory_layout
  exit 0
fi

# ---------------------------------------------------------------------------
# Instance layout: a /pev-instance run's reviewers, each scoped to its own
# section of the checkin doc (cycle_doc_id).
# ---------------------------------------------------------------------------
instance_layout() {
  [ -n "$CYCLE_DOC_ID" ] || block "No cycle_doc_id in .pev-state.json — cannot verify doc scope"

  local own
  case "$AGENT_TYPE" in
    pev:pev-reviewer)     own=review ;;
    pev:pev-doc-reviewer) own=doc-review ;;
    *) block "Doc-scope violation — during a /pev-instance run only the Reviewer and the Doc Reviewer write docs: pev-reviewer may write $CYCLE_DOC_ID::review and its subsections, pev-doc-reviewer $CYCLE_DOC_ID::doc-review and its subsections. ${AGENT_TYPE#pev:} may write nothing." ;;
  esac
  local allowed="$CYCLE_DOC_ID::$own"
  ideny() {
    block "Doc-scope violation — $1. ${AGENT_TYPE#pev:} may write only $allowed and its subsections."
  }

  # inst_check_section SECTION_ID — an update_section / patch_section target.
  inst_check_section() {
    case "$1" in
      "$allowed"|"$allowed".*) return 0 ;;
    esac
    ideny "target '$1' is not your section of the checkin"
  }

  case "$TOOL" in
    mcp__axiom-graph__axiom_graph_update_section|mcp__axiom-graph__axiom_graph_patch_section|mcp__axiom-graph__axiom_graph_delete_section)
      if [ "$EDITS_KIND" != none ]; then
        check_batch inst_check_section
      else
        inst_check_section "$SECTION_ID"
      fi
      ;;
    mcp__axiom-graph__axiom_graph_add_section)
      [ "$DOC_ID" = "$CYCLE_DOC_ID" ] || ideny "target '$DOC_ID' is not the checkin '$CYCLE_DOC_ID'"
      local parents p
      if [ "$BATCH_COUNT" -gt 0 ] 2>/dev/null; then
        parents="$BATCH_PARENTS"
      else
        parents="$PARENT_ID"
      fi
      while IFS= read -r p || [ -n "$p" ]; do
        case "$p" in
          "$own"|"$own".*) ;;
          *) ideny "new sections go under your own section: add_section(parent_id=\"$own\"), not parent_id=\"$p\"" ;;
        esac
      done <<EOF
$parents
EOF
      ;;
    mcp__axiom-graph__axiom_graph_write_doc)
      ideny "write_doc would replace a whole document"
      ;;
  esac
  exit 0
}

if [ "$LAYOUT" = "instance" ]; then
  instance_layout
  exit 0
fi

# ---------------------------------------------------------------------------
# Legacy single-file layout (no layout field): every write must target the
# cycle manifest exactly. Batch edits are checked item by item (check_batch).
# ---------------------------------------------------------------------------

# Auditor is exempt — it writes live feature docs as well as the manifest
# (see DESIGN.md tool permissions matrix), wherever the cycle runs it. A
# legacy cycle that merged before its audit runs it on main; its Write/Edit
# and axiom-graph calls are still confined by the worktree-scope hooks
# whenever the state file names a worktree_path. All other PEV subagents
# are scoped to the cycle manifest.
case "$AGENT_TYPE" in
  pev:pev-auditor) exit 0 ;;
  # Audit-skill subagents (consumer-docs, dev-docs, annotations) write to
  # their own audit manifests, not the active cycle manifest. Their
  # agent-frontmatter allowlists are the only enforcement layer for these.
  # Exempt the whole pev-audit-* family.
  pev:pev-audit-consumer-discovery|pev:pev-audit-consumer-verifier|pev:pev-audit-dev-shard|pev:pev-audit-annotations-fixer) exit 0 ;;
esac

if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

if [ -z "$CYCLE_DOC_ID" ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"block","permissionDecisionReason":"No cycle_doc_id in .pev-state.json — cannot verify doc scope"}}' >&2
  exit 2
fi

legacy_check_target() {
  [ -n "$1" ] || block "Could not extract doc_id from tool call"
  builder_doc "$1" && return 0
  [ "$1" = "$CYCLE_DOC_ID" ] \
    || block "Doc-scope violation — target '$1' is not the current cycle manifest '$CYCLE_DOC_ID'"
}

legacy_check_section() {
  legacy_check_target "$(echo "$1" | sed 's/::[^:]*$//')"
}

case "$TOOL" in
  mcp__axiom-graph__axiom_graph_update_section|mcp__axiom-graph__axiom_graph_patch_section|mcp__axiom-graph__axiom_graph_delete_section)
    if [ "$EDITS_KIND" != none ]; then
      check_batch legacy_check_section
    else
      legacy_check_section "$SECTION_ID"
    fi
    ;;
  mcp__axiom-graph__axiom_graph_add_section)
    legacy_check_target "$DOC_ID"
    ;;
  mcp__axiom-graph__axiom_graph_write_doc)
    block "PEV subagents do not write whole documents. The cycle manifest '$CYCLE_DOC_ID' already exists — edit it with axiom_graph_update_section, axiom_graph_patch_section or axiom_graph_add_section."
    ;;
esac
exit 0
