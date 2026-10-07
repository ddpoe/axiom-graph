#!/bin/bash
# pev-tool-gate.sh — PreToolUse hook for PEV subagents.
# BLOCKS non-allowlisted tools once the budget limit is reached.
#
# Active ONLY for the budgeted PEV roles below; every other agent_type,
# audit-skill agents included, is ungated. Budget threshold and allowlist
# are dispatched on agent_type. Counter file is keyed on agent_id (matched
# to pev-tool-counter.sh); an input with no agent_id is not gated at all.
# The hand-back tool (SubagentHandback) is always allowed, so an agent
# past its budget can still return.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool

AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

# Handing back is always allowed (exact match), whatever the count.
[ "$TOOL_NAME" = "SubagentHandback" ] && exit 0

# Dispatch limit + allowlist on agent_type. Allowlist is a regex-ready
# pipe-separated string; tool names are matched by substring against it.
case "$AGENT_TYPE" in
  pev:pev-architect)
    LIMIT=80
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_write_doc|axiom_graph_add_section|axiom_graph_build"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_write_doc, axiom_graph_add_section, axiom_graph_build"
    ;;
  pev:pev-builder)
    LIMIT=100
    ALLOWLIST="Bash|Edit|Write|axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_add_section"
    ALLOWLIST_HUMAN="Bash, Edit, Write, axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_add_section"
    ;;
  pev:pev-reviewer)
    LIMIT=85
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_add_section"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_add_section"
    ;;
  pev:pev-auditor)
    LIMIT=75
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_write_doc|axiom_graph_add_section|axiom_graph_add_link|axiom_graph_delete_link|axiom_graph_update_doc_meta|axiom_graph_mark_clean|axiom_graph_reverify|axiom_graph_accept_doc_edits|axiom_graph_purge_node|axiom_graph_build|axiom_graph_check"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_write_doc, axiom_graph_add_section, axiom_graph_add_link, axiom_graph_delete_link, axiom_graph_update_doc_meta, axiom_graph_mark_clean, axiom_graph_reverify, axiom_graph_accept_doc_edits, axiom_graph_purge_node, axiom_graph_build, axiom_graph_check"
    ;;
  pev:pev-doc-reviewer)
    LIMIT=60
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_add_section"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_add_section"
    ;;
  pev:pev-spike)
    LIMIT=7
    ALLOWLIST="Write|axiom_graph_update_section|axiom_graph_patch_section"
    ALLOWLIST_HUMAN="Write, axiom_graph_update_section, axiom_graph_patch_section"
    ;;
  *) exit 0 ;;
esac

# No agent_id: nothing was counted, so nothing is gated.
[ -z "$AGENT_ID" ] && exit 0

# Counter file (matches pev-tool-counter.sh keying)
PEV_TOOL_COUNTER="/tmp/pev-counter-${AGENT_ID}.txt"

COUNT=0
if [ -f "$PEV_TOOL_COUNTER" ]; then
  COUNT=$(cat "$PEV_TOOL_COUNTER" 2>/dev/null || echo 0)
fi

# Under the limit — allow everything
if [ "$COUNT" -lt "$LIMIT" ]; then
  exit 0
fi

# Over limit — check allowlist
if echo "$TOOL_NAME" | grep -qE "$ALLOWLIST"; then
  exit 0
fi

# Blocked. Name add_section in the hint only for roles whose allowlist holds it.
SAVE_HINT="axiom_graph_update_section for a progress section"
case "|${ALLOWLIST}|" in
  *"|axiom_graph_add_section|"*)
    SAVE_HINT="${SAVE_HINT}; axiom_graph_add_section for a checkpoint or log entry, which is a new section" ;;
esac
echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${TOOL_NAME} is blocked (budget ${COUNT}/${LIMIT}). Tools still available: ${ALLOWLIST_HUMAN}. Save your state to your own sections (${SAVE_HINT}), then hand back with CONTINUING status; handing back is always allowed. The next incarnation continues from your progress.\"}}"
