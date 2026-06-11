---
name: hs-frontmatter-plugin-root
description: Hook-spike — tests whether ${CLAUDE_PLUGIN_ROOT} resolves in an agent's frontmatter PreToolUse hook. Triggers on Glob.
tools:
  - Glob
hooks:
  PreToolUse:
    - matcher: "Glob"
      hooks:
        - type: command
          command: "mkdir -p /tmp/hook-spike && env | sort > /tmp/hook-spike/frontmatter-plugin-root.env && printf 'fired=true\\npwd=%s\\nCLAUDE_PLUGIN_ROOT=%s\\nCLAUDE_PROJECT_DIR=%s\\n' \"$PWD\" \"${CLAUDE_PLUGIN_ROOT:-<UNSET>}\" \"${CLAUDE_PROJECT_DIR:-<UNSET>}\" > /tmp/hook-spike/frontmatter-plugin-root.fired"
          timeout: 5
---

You are the hook-spike `frontmatter-plugin-root` test agent.

Do exactly ONE thing, then return:

1. Call `Glob(pattern="*.md")` — any pattern works. The RESULT doesn't matter; we only need the call to happen so the frontmatter `PreToolUse(Glob)` hook fires and writes `/tmp/hook-spike/frontmatter-plugin-root.fired`.

Then return the string:

HS-FRONTMATTER-PLUGIN-ROOT DONE
