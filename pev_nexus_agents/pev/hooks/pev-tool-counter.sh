#!/bin/bash
# pev-tool-counter.sh — PostToolUse hook for PEV subagents.
# Increments the tool call counter and pushes advisory warnings at
# warn / urgent / limit thresholds. Actual blocking is handled by
# pev-tool-gate.sh (PreToolUse).
#
# Active ONLY for the budgeted PEV roles below; every other agent_type,
# audit-skill agents included, is uncounted. Budget thresholds are
# dispatched on agent_type. Counter file is keyed on agent_id (one counter
# per subagent invocation); cleaned up by pev-subagent-stop.sh. An input
# with no agent_id is not counted, and the hand-back tool
# (SubagentHandback) is never counted.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq posttool

AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
AGENT_ID=$(echo "$INPUT" | jq -r '.agent_id // empty' 2>/dev/null)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

# Dispatch thresholds on agent_type
case "$AGENT_TYPE" in
  pev:pev-architect)    WARN=50; URGENT=65; LIMIT=80  ;;
  pev:pev-builder)      WARN=60; URGENT=85; LIMIT=100 ;;
  pev:pev-reviewer)     WARN=50; URGENT=70; LIMIT=85  ;;
  pev:pev-auditor)      WARN=45; URGENT=60; LIMIT=75  ;;
  pev:pev-doc-reviewer) WARN=35; URGENT=50; LIMIT=60  ;;
  pev:pev-spike)        WARN=3;  URGENT=5;  LIMIT=7   ;;
  *) exit 0 ;;
esac

# No agent_id: skip counting entirely. The hand-back is never counted.
[ -z "$AGENT_ID" ] && exit 0
[ "$TOOL_NAME" = "SubagentHandback" ] && exit 0

# Counter file keyed on agent_id
PEV_TOOL_COUNTER="/tmp/pev-counter-${AGENT_ID}.txt"

COUNT=0
if [ -f "$PEV_TOOL_COUNTER" ]; then
  COUNT=$(cat "$PEV_TOOL_COUNTER" 2>/dev/null || echo 0)
fi

COUNT=$((COUNT + 1))
echo "$COUNT" > "$PEV_TOOL_COUNTER"

# Push advisory warnings (blocking is handled by PreToolUse gate)
if [ "$COUNT" -ge "$LIMIT" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"TOOL BUDGET: ${COUNT}/${LIMIT}. Some tools are now blocked — you still have your allowlisted tools. If you are mid-task, save your state to your own sections (progress and checkpoint): what is done, what is in progress, what remains, and context the next incarnation needs. Then produce your structured return with CONTINUING status. The next incarnation picks up where you left off with a fresh budget. Do not rush or cut corners.\"}}"
elif [ "$COUNT" -ge "$URGENT" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"TOOL BUDGET: ${COUNT}/${LIMIT}. Approaching the tool limit — after ${LIMIT}, some tools will be blocked. Finish your current task if close. If not, that is OK — save your progress to your own sections so the next incarnation can continue seamlessly. Do not start a new task.\"}}"
elif [ "$COUNT" -ge "$WARN" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"TOOL BUDGET: ${COUNT}/${LIMIT}. You have used more than half your budget. Check your progress against your plan — if many tasks remain, focus on completing one at a time rather than exploring broadly.\"}}"
fi
