#!/bin/bash
# pev-hook-common.sh — shared preamble sourced by every wired PEV hook.
#
# Every PEV hook reads its JSON input with jq. Without jq, each read comes
# back empty, the agent-type gate falls through to its "not a PEV agent"
# arm, and every guardrail silently allows everything. This preamble
# closes that hole:
#
#   - jq present           → return, the hook runs as normal.
#   - jq missing, PEV agent → deny (PreToolUse) or warn (PostToolUse /
#                             SubagentStop) with an install hint.
#   - jq missing, anything else → exit 0, as before. The `.*` matchers run
#                             these hooks on every tool call in every
#                             session, so failing closed for non-PEV
#                             callers would lock the user out of Claude Code.
#
# Usage, right after INPUT=$(cat):
#   . "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
#   pev_require_jq pretool|posttool|stop
#
# PEV_HOOKS_JQ names the jq binary to probe for (default: jq). It exists so
# tests can simulate a missing jq on machines where jq shares a directory
# with bash and coreutils and can't be dropped from PATH.

PEV_JQ_HINT="PEV guardrails need jq, which is not on PATH, so every PEV hook is inert. Install it (Windows: winget install jqlang.jq; Ubuntu/Debian: sudo apt install jq; macOS: brew install jq) and restart Claude Code."

# Is the hook input from a PEV subagent? Answered without jq: the one
# decision that has to be made when jq is missing.
pev_is_pev_agent() {
  printf '%s' "$INPUT" | grep -qE '"agent_type"[[:space:]]*:[[:space:]]*"pev:'
}

# Return if jq is usable; otherwise answer for the hook event and exit.
pev_require_jq() {
  command -v "${PEV_HOOKS_JQ:-jq}" >/dev/null 2>&1 && return 0
  pev_is_pev_agent || exit 0
  case "$1" in
    pretool)
      printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$PEV_JQ_HINT"
      ;;
    posttool)
      printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"WARNING: %s"}}\n' "$PEV_JQ_HINT"
      ;;
    stop)
      printf '{"systemMessage":"WARNING: %s"}\n' "$PEV_JQ_HINT"
      ;;
  esac
  exit 0
}
