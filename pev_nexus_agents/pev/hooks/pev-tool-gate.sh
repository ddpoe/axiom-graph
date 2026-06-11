#!/bin/bash
# pev-tool-gate.sh — PreToolUse hook for PEV subagents.
# BLOCKS non-allowlisted tools once the budget limit is reached.
#
# Active ONLY when agent_type starts with "pev:". Budget threshold and
# allowlist are dispatched on agent_type. Counter file is keyed on
# agent_id (matched to pev-tool-counter.sh).

INPUT=$(cat)

AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty' 2>/dev/null)
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

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
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section"
    ;;
  pev:pev-auditor)
    LIMIT=75
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_write_doc|axiom_graph_add_section|axiom_graph_delete_link|axiom_graph_update_doc_meta|axiom_graph_mark_clean|axiom_graph_purge_node|axiom_graph_build|axiom_graph_check"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_write_doc, axiom_graph_add_section, axiom_graph_delete_link, axiom_graph_update_doc_meta, axiom_graph_mark_clean, axiom_graph_purge_node, axiom_graph_build, axiom_graph_check"
    ;;
  pev:pev-doc-reviewer)
    LIMIT=60
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section"
    ;;
  pev:pev-audit-dev-shard)
    LIMIT=60
    ALLOWLIST="axiom_graph_update_section|axiom_graph_patch_section|axiom_graph_read_doc"
    ALLOWLIST_HUMAN="axiom_graph_update_section, axiom_graph_patch_section, axiom_graph_read_doc"
    ;;
  pev:pev-spike)
    LIMIT=7
    ALLOWLIST="Write|axiom_graph_update_section|axiom_graph_patch_section"
    ALLOWLIST_HUMAN="Write, axiom_graph_update_section, axiom_graph_patch_section"
    ;;
  *) exit 0 ;;
esac

# Counter file (matches pev-tool-counter.sh keying)
if [ -n "$AGENT_ID" ]; then
  PEV_TOOL_COUNTER="/tmp/pev-counter-${AGENT_ID}.txt"
else
  PEV_TOOL_COUNTER="/tmp/pev-counter-${AGENT_TYPE//:/-}.txt"
fi

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

# Blocked
echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${TOOL_NAME} is blocked (budget ${COUNT}/${LIMIT}). Tools still available: ${ALLOWLIST_HUMAN}. Use axiom_graph_update_section to save your state to the cycle manifest, then return with CONTINUING status. The next incarnation continues from your progress.\"}}"
