#!/bin/bash
# pev-doc-scope-md.sh — PreToolUse(Write|Edit) hook for the PEV Auditor.
# The doc-scope family's file-tool member: the Auditor may edit markdown,
# never code.
#
# Active ONLY for agent_type "pev:pev-auditor"; every other caller exits 0
# here (the worktree-scope and docjson-guard hooks still apply to them).
#
# The Auditor's Write and Edit may target only a path ending in ".md"
# (CHANGELOG.md, a plugin README, ...) whose resolved location is inside the
# project root, the hook input's cwd. Anything else is denied:
#   - code or config is the Builder's: report it as a needs_fix;
#   - a .docjson document goes through the axiom-graph doc tools, which keep
#     the index, section hashes and staleness in step with the file;
#   - a markdown file outside the project root is not the cycle's to edit.

INPUT=$(cat)

# Fail closed for PEV agents when jq is missing (see lib/pev-hook-common.sh).
. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"
pev_require_jq pretool

AGENT_TYPE=$(echo "$INPUT" | jq -r '.agent_type // empty')
[ "$AGENT_TYPE" = "pev:pev-auditor" ] || exit 0

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)

# Normalize a Windows path (C:\... or C:/...) to POSIX when cygpath exists.
normalize() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -u "$1"
  else
    printf '%s\n' "$1"
  fi
}

# Resolve a path to its physical location: the deepest existing directory
# goes through `pwd -P` and the rest is appended. Prints nothing when a `..`
# or `.` segment remains past the existing part.
resolve() {
  local path="$1" dir base check rest
  dir=$(dirname "$path")
  base=$(basename "$path")
  check="$dir"
  while [ ! -d "$check" ] && [ "$check" != "/" ] && [ "$check" != "." ]; do
    check=$(dirname "$check")
  done
  rest="${dir#"$check"}"
  case "/$rest/$base/" in */../*|*/./*) return 0 ;; esac
  printf '%s%s/%s\n' "$(cd "$check" 2>/dev/null && pwd -P)" "$rest" "$base"
}

case "$FILE_PATH" in
  *.md)
    ROOT=$(echo "$INPUT" | jq -r '.cwd // empty' 2>/dev/null | tr -d '\r')
    [ -z "$ROOT" ] && ROOT="${CLAUDE_PROJECT_DIR:-}"
    if [ -n "$ROOT" ]; then
      ROOT=$(normalize "$ROOT")
      ROOT=$(cd "$ROOT" 2>/dev/null && pwd -P)
    fi
    TARGET=$(normalize "$FILE_PATH")
    case "$TARGET" in
      /*) ;;
      *) TARGET="$ROOT/$TARGET" ;;
    esac
    TARGET=$(resolve "$TARGET")
    if [ -n "$ROOT" ] && [ -n "$TARGET" ]; then
      case "$TARGET" in
        "$ROOT"/*) exit 0 ;;
      esac
    fi
    ;;
esac

NAME="${FILE_PATH##*[/\\]}"
case "$FILE_PATH" in
  *.md)
    REASON="The Auditor may Write or Edit markdown only inside the project root (the session cwd); '${FILE_PATH}' is outside it." ;;
  *.docjson)
    REASON="$NAME is a DocJSON document: the Auditor edits documents with axiom_graph_update_section, axiom_graph_patch_section or axiom_graph_add_section, never with Write or Edit." ;;
  *)
    REASON="The Auditor may Write or Edit only markdown (*.md) files; '${NAME:-<no file_path>}' is not one. Code and config are the Builder's: report the change as a needs_fix. DocJSON documents go through the axiom-graph doc tools." ;;
esac
# Keep the reason valid inside a JSON string.
REASON=$(printf '%s' "$REASON" | tr -d '"\\' | tr -d '\000-\037')
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$REASON"
exit 0
