#!/bin/bash
# pev-doc-scope.sh — PreToolUse hook for PEV doc-write axiom-graph tools.
# Enforces that doc-write calls target only the current cycle manifest.
#
# Active ONLY when agent_type starts with "pev:" (a PEV subagent).
# Reads cycle_doc_id from .pev-state.json, compares against the doc_id
# (or doc_json.id or extracted from section_id) in the tool_input.

INPUT=$(cat)

# Gate: PEV subagents only. Auditor is exempt — it runs on main post-merge and
# writes to live feature docs (see DESIGN.md tool permissions matrix). All
# other PEV subagents are scoped to the cycle manifest.
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null)
case "$AGENT_TYPE" in
  pev:pev-auditor) exit 0 ;;
  # Audit-skill subagents (consumer-docs, dev-docs, annotations) write to
  # their own audit manifests, not the active cycle manifest. The audit
  # skill design (D-19) intends agent-frontmatter allowlists to be the
  # only enforcement layer for these. Exempt the whole pev-audit-* family.
  pev:pev-audit-consumer-discovery|pev:pev-audit-consumer-verifier|pev:pev-audit-dev-shard|pev:pev-audit-annotations-fixer) exit 0 ;;
  pev:*) ;;
  *) exit 0 ;;
esac

# Resolve .pev-state.json (lives at cwd root — set by EnterWorktree).
# Claude Code passes cwd as a Windows path on Windows (C:\...\foo); normalize
# to POSIX so file tests and path concatenation work in git-bash.
PROJECT_ROOT=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -z "$PROJECT_ROOT" ] && PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-}"
if command -v cygpath >/dev/null 2>&1; then
  PROJECT_ROOT=$(cygpath -u "$PROJECT_ROOT")
fi
STATE_FILE="$PROJECT_ROOT/.pev-state.json"

if [ ! -f "$STATE_FILE" ]; then
  exit 0
fi

CYCLE_DOC_ID=$(cat "$STATE_FILE" | jq -r '.cycle_doc_id // ""' 2>/dev/null)

if [ -z "$CYCLE_DOC_ID" ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"block","permissionDecisionReason":"No cycle_doc_id in .pev-state.json — cannot verify doc scope"}}' >&2
  exit 2
fi

TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')

case "$TOOL" in
  mcp__axiom-graph__axiom_graph_update_section|mcp__axiom-graph__axiom_graph_patch_section)
    SECTION_ID=$(echo "$INPUT" | jq -r '.tool_input.section_id // ""')
    TARGET=$(echo "$SECTION_ID" | sed 's/::[^:]*$//')
    ;;
  mcp__axiom-graph__axiom_graph_add_section)
    TARGET=$(echo "$INPUT" | jq -r '.tool_input.doc_id // ""')
    ;;
  mcp__axiom-graph__axiom_graph_write_doc)
    DOC_JSON=$(echo "$INPUT" | jq -r '.tool_input.doc_json // ""')
    if echo "$DOC_JSON" | jq -e . >/dev/null 2>&1; then
      TARGET=$(echo "$DOC_JSON" | jq -r '.id // ""')
    else
      TARGET=""
    fi
    ;;
  *)
    exit 0
    ;;
esac

if [ -z "$TARGET" ]; then
  echo "BLOCKED: Could not extract doc_id from tool call" >&2
  exit 2
fi

if [ "$TARGET" != "$CYCLE_DOC_ID" ]; then
  echo "BLOCKED: Doc-scope violation — target '$TARGET' is not the current cycle manifest '$CYCLE_DOC_ID'" >&2
  exit 2
fi
