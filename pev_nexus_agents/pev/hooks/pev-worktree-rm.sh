#!/bin/bash
# pev-worktree-rm.sh — PreToolUse(Bash) hook for PEV subagents.
# Lets a PEV subagent delete files inside its own cycle worktree without a
# permission prompt, so unattended cycles don't stall on approvals.
#
# Allows only a single plain `rm [-flags] path...` whose targets are all
# relative paths (no "..", no absolute or drive paths, no globs), run from the
# cycle's worktree as named in .pev-state.json. One leading `cd <dir> &&` is
# accepted when <dir> resolves inside that worktree (the Builder's usual
# `cd {worktree_path} && command` form); the rm targets are then relative to
# <dir>. Otherwise the whole command must be that rm: nothing chained with
# ; & | and no $( ) or backticks, so a safe-looking rm cannot carry another
# command past the allow.
#
# Anything else gets no opinion (exit 0, no output): the consumer's own
# permission rules decide. Non-PEV sessions are never affected. A deny from
# another hook (pev-docjson-guard.sh refuses rm of *.docjson) still wins.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool

# Gate: PEV subagents only
AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
case "$AGENT_TYPE" in
  pev:*) ;;
  *) exit 0 ;;
esac

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
TRIMMED=$(printf '%s' "$COMMAND" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')

# Optional single leading `cd <dir> &&` (dir may be quoted). Split it off.
CD_DIR=""
CD_RE='^cd[[:space:]]+("[^"]+"|'"'"'[^'"'"']+'"'"'|[^[:space:]&;|"'"'"']+)[[:space:]]*&&[[:space:]]*(.*)$'
if [[ "$TRIMMED" =~ $CD_RE ]]; then
  CD_DIR="${BASH_REMATCH[1]}"
  CD_DIR="${CD_DIR%\"}"; CD_DIR="${CD_DIR#\"}"; CD_DIR="${CD_DIR%\'}"; CD_DIR="${CD_DIR#\'}"
  TRIMMED="${BASH_REMATCH[2]}"
fi

# Only a single plain rm of simple path tokens.
printf '%s' "$TRIMMED" | grep -qE '^rm([[:space:]]+-[A-Za-z]+)*([[:space:]]+[A-Za-z0-9_./-]+)+$' || exit 0

normalize() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1"
  else
    echo "$1"
  fi
}

# Must be running in the cycle's worktree.
CWD=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null)
[ -z "$CWD" ] && exit 0
CWD=$(normalize "$CWD")
STATE_FILE="$CWD/.pev-state.json"
[ -f "$STATE_FILE" ] || exit 0
WORKTREE_PATH=$(jq -r '.worktree_path // empty' "$STATE_FILE" 2>/dev/null)
[ -z "$WORKTREE_PATH" ] && exit 0
WORKTREE_PATH=$(cd "$(normalize "$WORKTREE_PATH")" 2>/dev/null && pwd -P)
CWD_REAL=$(cd "$CWD" 2>/dev/null && pwd -P)
[ -n "$WORKTREE_PATH" ] && [ "$CWD_REAL" = "$WORKTREE_PATH" ] || exit 0

# A leading cd must land inside the worktree.
if [ -n "$CD_DIR" ]; then
  CD_DIR=$(normalize "$CD_DIR")
  case "$CD_DIR" in
    /*) ;;
    *) CD_DIR="$CWD/$CD_DIR" ;;
  esac
  CD_REAL=$(cd "$CD_DIR" 2>/dev/null && pwd -P) || exit 0
  case "$CD_REAL" in
    "$WORKTREE_PATH"|"$WORKTREE_PATH"/*) ;;
    *) exit 0 ;;
  esac
fi

# Every target relative, inside the worktree.
for tok in $(printf '%s' "$TRIMMED" | sed -E 's/^rm([[:space:]]+-[A-Za-z]+)*//'); do
  case "$tok" in
    /*|~*|*..*|[A-Za-z]:*) exit 0 ;;
  esac
done

printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"PEV: rm of relative paths inside the cycle worktree"}}\n'
exit 0
