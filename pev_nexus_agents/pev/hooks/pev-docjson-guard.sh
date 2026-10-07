#!/bin/bash
# pev-docjson-guard.sh — PreToolUse(Write|Edit|MultiEdit|NotebookEdit|Bash|Read|Grep) hook.
# Watches raw edits of *.docjson documents, so document changes go through
# the axiom-graph MCP tools, which keep the index, section hashes and
# staleness in step with the file.
#
# PEV_DOCJSON_GUARD picks the response; detection is the same in every mode:
#   warn (default, also unset or any unknown value)
#         a raw write goes through with a note (additionalContext) naming the
#         doc tools and axiom_graph_accept_doc_edits. It never answers
#         "allow", so the user's own permission rules and prompts still apply.
#   block a raw write is denied.
#   off   the hook is silent.
#
# Read hint: in warn and block mode, the first raw read of a .docjson in a
# session (Read, a Grep whose path or glob names one, or a Bash command
# naming one without writing it) gets one line of additionalContext pointing
# at axiom_graph_read_doc and axiom_graph_search. A marker file in
# ${TMPDIR:-/tmp}, named from the sanitised session_id, keeps it to once per
# session; with no session_id the hint fires every time. This guard never
# denies a read (Read, Grep, or a Bash command that writes no document), in
# any mode, with or without jq, for main sessions and PEV agents alike.
#
# Unlike the other PEV hooks this one is NOT gated on a PEV agent_type: a
# raw edit desyncs the index whoever makes it, so it applies to every
# session. It only ever matches paths ending in .docjson, so it does nothing
# in a project whose documents are still *.json.
#
# Without jq the tool input can't be parsed. The other hooks go inert there
# for main sessions and fail closed for PEV agents (see
# lib/pev-hook-common.sh); this one runs the same write checks over the raw
# payload instead and answers per the mode. The read hint is skipped without
# jq. Read, Grep and any Bash call with no document write exit before the jq
# check, so a missing jq never turns a read into a deny *from this guard*.
# Other PEV hooks (pev-tool-gate.sh, pev-bash-scope.sh, ...) keep their own
# fail-closed install-hint deny for PEV agents without jq.
#
# Bash coverage is best-effort: it catches redirection (including `>|` and
# quoted targets containing spaces), tee, dd of=, truncate, in-place sed/perl
# (`-i` and `--in-place`), cp/mv/install onto a document, touch/rm, and
# interpreter one-liners (-c/-e) whose program shows a write marker. `git mv`
# is left alone: it is step F's sanctioned rename. Only the command string is
# judged (never the call's description, with or without jq), and a git commit's
# message text (-m/--message values, a heredoc body) is taken out first, so a
# .docjson named in a commit message is neither a read nor a write. A write this guard misses
# is still caught afterwards: the next build or doc-tool write reports it as
# a raw DocJSON edit (RAW_DOCJSON_EDIT). It is a guardrail, not a sandbox.

INPUT=$(cat)

case "${PEV_DOCJSON_GUARD:-warn}" in
  off) exit 0 ;;
  block) MODE=block ;;
  *) MODE=warn ;;
esac

. "$(dirname "${BASH_SOURCE[0]}")/lib/pev-hook-common.sh"

DOC_TOOLS="Use the axiom-graph MCP tools instead: axiom_graph_update_section or axiom_graph_patch_section for section content, axiom_graph_add_section / axiom_graph_delete_section for structure, axiom_graph_update_doc_meta for title and tags, axiom_graph_write_doc for a new document. To keep an edit that already landed, axiom_graph_accept_doc_edits."

READ_HINT="axiom_graph_read_doc with outline=true, then section_ids, reads just the sections you need; axiom_graph_search with a quoted phrase and scope=docs finds text across documents. (Shown once per session.)"

# Make $1 safe inside a JSON string: control characters and the two
# characters that would break it are stripped (Windows paths arrive with
# backslashes).
json_safe() {
  printf '%s' "$1" | tr -d '"\\' | tr -d '\000-\037'
}

# Emit a PreToolUse deny.
emit_deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$(json_safe "$1")"
  exit 0
}

# Emit PreToolUse context only. Deliberately no permissionDecision: an
# "allow" would skip the user's own permission rules and prompts.
emit_context() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"%s"}}\n' "$(json_safe "$1")"
  exit 0
}

# Answer a detected raw write per the mode: deny in block mode, a note in
# warn mode.
respond_write() {
  if [ "$MODE" = block ]; then
    emit_deny "$1 $2"
  fi
  emit_context "$1 The write is allowed, but it leaves the index out of step with the file until the next build reports it. $2 Set PEV_DOCJSON_GUARD=block to deny such writes."
}

write_found() {
  # Report the bare file name, not the whole path.
  local name="${1##*[/\\]}"
  respond_write "$name is a DocJSON document; do not edit it raw." "$DOC_TOOLS"
}

# Hint once per session that a document has better readers than a raw read.
# The marker is keyed on the sanitised session_id; without one the hint
# fires every time.
read_hint() {
  local name="${1##*[/\\]}" session marker=""
  session=$(echo "$INPUT" | jq -r '.session_id // empty' | tr -cd 'A-Za-z0-9_-')
  if [ -n "$session" ]; then
    marker="${TMPDIR:-/tmp}/pev-docjson-read-hint-$session"
    [ -e "$marker" ] && exit 0
    : >"$marker" 2>/dev/null
  fi
  emit_context "$name is a DocJSON document. $READ_HINT"
}

DQ='"'
SQ="'"
# What may follow a document name: a quote, whitespace, a shell operator or
# the end of the command. Without it `.docjson` inside a longer name -- the
# module path `axiom_graph.docjson.api`, or `notes.docjson.bak` -- matched.
END_RE="([${DQ}${SQ}[:space:];&|<>)]|\$)"
# A path ending in .docjson, quoted (so it may contain spaces) or bare.
PATH_RE="(${DQ}[^$DQ]*\.docjson$DQ|${SQ}[^$SQ]*\.docjson$SQ|[^[:space:];|&<>$DQ$SQ]+\.docjson$END_RE)"
# Inside an interpreter one-liner: a write/append open mode, or a call that
# writes, moves or deletes. A program with none of these only reads.
WRITE_MARKER_RE="[,(][[:space:]]*[$DQ$SQ](w|a|x|r\+|w\+|a\+)b?[$DQ$SQ]|write|dump|appendFile|truncate|save|unlink|remove|rename|replace"

# Pull the document name out of a matched command fragment, preferring a
# quoted path so names containing spaces survive.
target_name() {
  local name
  name=$(printf '%s' "$1" | grep -oE "${DQ}[^$DQ]*\.docjson$DQ|${SQ}[^$SQ]*\.docjson$SQ" | head -n 1)
  if [ -n "$name" ]; then
    name=${name#[\"\']}
    name=${name%[\"\']}
  else
    name=$(printf '%s' "$1" | grep -oE "[^[:space:]=>|]+\.docjson$END_RE" | head -n 1 | sed -E 's/\.docjson.*$/.docjson/')
  fi
  printf '%s' "$name"
}

# Print the fragment of Bash command $1 that writes a .docjson document, or
# nothing. Shared by the jq path and the no-jq fallback.
bash_write_target() {
  local cmd="$1" target one_liner

  # Redirection (`>`, `>>`, `>|`), tee, dd of=.
  target=$(printf '%s\n' "$cmd" | grep -oE "(>>?\|?|tee([[:space:]]+-a)?|of=)[[:space:]]*$PATH_RE" | head -n 1)

  # In-place editors (short -i and GNU --in-place) and truncate.
  [ -z "$target" ] && target=$(printf '%s\n' "$cmd" |
    grep -oE "(^|[;&|[:space:]])((sed|perl)[[:space:]]+([^;&|]*[[:space:]])?(-[a-zA-Z]*i|--in-place[^[:space:]]*)|truncate[[:space:]])[^;&|]*\.docjson$END_RE" | head -n 1)

  # Copying or moving onto a document. `git mv` is the sanctioned rename,
  # so any command using it is left alone.
  if [ -z "$target" ] && ! printf '%s' "$cmd" | grep -qE '(^|[;&|[:space:]])git[[:space:]]+mv[[:space:]]'; then
    target=$(printf '%s\n' "$cmd" |
      grep -oE "(^|[;&|[:space:]])(cp|mv|install)[[:space:]][^;&|]*$PATH_RE" | head -n 1)
  fi

  # Creating or deleting one: the index still has to be told.
  [ -z "$target" ] && target=$(printf '%s\n' "$cmd" |
    grep -oE "(^|[;&|[:space:]])(touch|rm|unlink|shred)[[:space:]][^;&|]*$PATH_RE" | head -n 1)

  # An interpreter one-liner naming a document, denied only when its program
  # shows a write marker. One that only reads passes.
  if [ -z "$target" ]; then
    one_liner=$(printf '%s\n' "$cmd" |
      grep -oE "(^|[;&|[:space:]])(python[0-9.]*|node|ruby|perl|php)[[:space:]]+([^;&|]*[[:space:]])?-(c|e)[[:space:]]?[^;&|]*\.docjson$END_RE" | head -n 1)
    if [ -n "$one_liner" ] && printf '%s' "$cmd" | grep -qE "$WRITE_MARKER_RE"; then
      target=$one_liner
    fi
  fi

  printf '%s' "$target"
}

# Print Bash command $1 with any git commit message text taken out: the
# quoted value of -m/--message (also -am, -m"...", --message=...) and the
# body of a heredoc. A .docjson mentioned in a commit message is neither a
# read nor a write. Commands that aren't a git commit come back unchanged.
drop_commit_messages() {
  local cmd="$1" tag
  [[ "$cmd" =~ (^|[\;\&\|[:space:]])git[[:space:]]([^\;\&\|]*[[:space:]])?commit([[:space:]]|$) ]] || {
    printf '%s' "$cmd"
    return
  }
  local msg_re="(^|[[:space:]])(-[a-zA-Z]*m|--message)(=|[[:space:]]*)(\"([^\"\\\\]|\\\\.)*\"|'[^']*')"
  while [[ "$cmd" =~ $msg_re ]]; do
    cmd="${cmd/"${BASH_REMATCH[0]}"/${BASH_REMATCH[1]}-m MSG}"
  done
  if [[ "$cmd" =~ \<\<-?[[:space:]]*[\"\']?([A-Za-z_][A-Za-z0-9_]*) ]]; then
    tag="${BASH_REMATCH[1]}"
    cmd=$(printf '%s\n' "$cmd" | awk -v tag="$tag" '
      skip { if ($0 ~ ("^[\t]*" tag "$")) { skip = 0; print } ; next }
      { print }
      index($0, "<<") { skip = 1 }')
  fi
  printf '%s' "$cmd"
}

# jq missing: run the same write checks over the raw payload. A Bash call
# with no document write leaves; only a write tool falls through to the
# shared fail-closed install hint for PEV agents. For Bash, the
# payload is cut at the start of the command value (so its first word
# starts a line, as the patterns expect) and JSON's escaped quotes are
# undone (so quoted paths still match).
HAVE_JQ=1
command -v "${PEV_HOOKS_JQ:-jq}" >/dev/null 2>&1 || HAVE_JQ=

# Reads are never denied, so Read and Grep leave here, before the jq check
# below could deny a PEV agent's call. Without jq there is no hint either.
if printf '%s' "$INPUT" | grep -qE '"tool_name"[[:space:]]*:[[:space:]]*"(Read|Grep)"'; then
  [ -n "$HAVE_JQ" ] || exit 0
  TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
  if [ "$TOOL_NAME" = Read ]; then
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
    case "$FILE_PATH" in
      *.docjson) read_hint "$FILE_PATH" ;;
    esac
  elif [ "$TOOL_NAME" = Grep ]; then
    GREP_PATH=$(echo "$INPUT" | jq -r '.tool_input.path // empty')
    GREP_GLOB=$(echo "$INPUT" | jq -r '.tool_input.glob // empty')
    case "$GREP_PATH" in
      *.docjson) read_hint "$GREP_PATH" ;;
    esac
    case "$GREP_GLOB" in
      *.docjson*) read_hint "$GREP_GLOB" ;;
    esac
  fi
  exit 0
fi

if [ -z "$HAVE_JQ" ]; then
  case "$INPUT" in
    *.docjson*)
      JQ_NOTE="jq is not on PATH, so the DocJSON guard checked the raw tool input. $PEV_JQ_HINT $DOC_TOOLS"
      if printf '%s' "$INPUT" | grep -qE '"tool_name"[[:space:]]*:[[:space:]]*"(Write|Edit|MultiEdit|NotebookEdit)"' &&
        printf '%s' "$INPUT" | grep -qE '"(file_path|notebook_path)"[[:space:]]*:[[:space:]]*"[^"]*\.docjson"'; then
        respond_write "This call edits a .docjson document." "$JQ_NOTE"
      fi
      if printf '%s' "$INPUT" | grep -qE '"tool_name"[[:space:]]*:[[:space:]]*"Bash"'; then
        # Only the command string is judged, never the call's description.
        RAW=$(printf '%s' "$INPUT" |
          sed -n -E 's/.*"command"[[:space:]]*:[[:space:]]*"(([^"\\]|\\.)*)".*/\1/p' | head -n 1 |
          sed 's/\\"/"/g; s/\\n/\n/g; s/\\\\/\\/g')
        RAW=$(drop_commit_messages "$RAW")
        if [ -n "$(bash_write_target "$RAW")" ]; then
          respond_write "This call writes a .docjson document." "$JQ_NOTE"
        fi
      fi
      ;;
  esac
  # No document write found. A Bash call goes through untouched: it may be a
  # read, and this guard never denies a read. Only a write tool (Write, Edit,
  # MultiEdit, NotebookEdit) by a PEV agent gets the shared install hint.
  printf '%s' "$INPUT" | grep -qE '"tool_name"[[:space:]]*:[[:space:]]*"(Write|Edit|MultiEdit|NotebookEdit)"' || exit 0
fi
pev_require_jq pretool

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')

case "$TOOL_NAME" in
  Write|Edit|MultiEdit)
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
    ;;
  NotebookEdit)
    FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.notebook_path // empty')
    ;;
  Bash)
    COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
    COMMAND=$(drop_commit_messages "$COMMAND")
    case "$COMMAND" in
      *.docjson*) ;;
      *) exit 0 ;;
    esac
    TARGET=$(bash_write_target "$COMMAND")
    [ -n "$TARGET" ] && write_found "$(target_name "$TARGET")"
    # No write. `git mv` is the sanctioned rename, not a read; anything else
    # naming a document path is a raw read.
    printf '%s' "$COMMAND" | grep -qE '(^|[;&|[:space:]])git[[:space:]]+mv[[:space:]]' && exit 0
    printf '%s\n' "$COMMAND" | grep -qE "$PATH_RE" && read_hint "$(target_name "$COMMAND")"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac

case "$FILE_PATH" in
  *.docjson) write_found "$FILE_PATH" ;;
esac
exit 0
