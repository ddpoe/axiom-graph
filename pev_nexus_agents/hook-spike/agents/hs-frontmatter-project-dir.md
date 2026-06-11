---
name: hs-frontmatter-project-dir
description: Hook-spike — tests whether ${CLAUDE_PROJECT_DIR} resolves in an agent's frontmatter PreToolUse hook. Triggers on Grep.
tools:
  - Grep
hooks:
  PreToolUse:
    - matcher: "Grep"
      hooks:
        - type: command
          command: "mkdir -p /tmp/hook-spike && env | sort > /tmp/hook-spike/frontmatter-project-dir.env && printf 'fired=true\\npwd=%s\\nCLAUDE_PLUGIN_ROOT=%s\\nCLAUDE_PROJECT_DIR=%s\\n' \"$PWD\" \"${CLAUDE_PLUGIN_ROOT:-<UNSET>}\" \"${CLAUDE_PROJECT_DIR:-<UNSET>}\" > /tmp/hook-spike/frontmatter-project-dir.fired"
          timeout: 5
---

You are the hook-spike `frontmatter-project-dir` test agent.

Do exactly ONE thing, then return:

1. Call `Grep(pattern="the", path=".", output_mode="files_with_matches", head_limit=1)` — any simple search. The RESULT doesn't matter; we only need the call to happen so the frontmatter `PreToolUse(Grep)` hook fires and writes `/tmp/hook-spike/frontmatter-project-dir.fired`.

Then return the string:

HS-FRONTMATTER-PROJECT-DIR DONE
