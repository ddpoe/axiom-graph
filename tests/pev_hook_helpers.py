"""Shared helpers for tests that run the PEV plugin's bash hook scripts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "pev_nexus_agents" / "pev" / "hooks"


def find_bash() -> str | None:
    """Return a bash that can run the hooks, or None.

    On Windows, ``bash`` on PATH may be the WSL launcher, which can't see
    Windows paths the same way; use Git for Windows' bash instead.
    """
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git is None:
        return None
    git_path = Path(git).resolve()
    for root in (git_path.parents[1], git_path.parents[2]):
        candidate = root / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


BASH = find_bash()


def bash_has_jq() -> bool:
    """Whether jq is on the PATH the hooks will run with."""
    if BASH is None:
        return False
    probe = subprocess.run([BASH, "-c", "command -v jq"], capture_output=True, text=True)
    return probe.returncode == 0


def run_hook(
    script: str, payload: dict, cwd: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run one hook script with ``payload`` as its stdin JSON."""
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(cwd), **(extra_env or {}))
    return subprocess.run(
        [BASH, (HOOKS_DIR / script).as_posix()],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=30,
    )
