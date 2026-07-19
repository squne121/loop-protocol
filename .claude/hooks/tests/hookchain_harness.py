"""hookchain_harness.py -- Issue #1539 fix_delta Blocker 4.

Reusable helper that reads the REAL `.claude/settings.json` PreToolUse hook
registration (not a hand-picked subset) and executes every hook whose matcher
covers a given tool ("Bash" by default) IN CONFIGURED ORDER, as real
subprocesses against real stdin JSON -- exactly the shape Claude Code itself
invokes them with. This lets tests assert on the AGGREGATE decision (deny/ask
> allow/no-decision) across the full chain, instead of hand-selecting two
hooks and asserting each returns 0 independently.

Claude Code PreToolUse hook exit code contract used by every hook in this
repo (see each hook's own header comment): 0 = allow / no-decision,
2 = block (deny). None of the hooks currently registered in settings.json use
exit code 1 ("ask"); this harness still classifies exit code 1 as "ask" for
forward compatibility with the documented three-way decision space.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"


def load_pretool_hook_commands(tool_name: str = "Bash") -> list[str]:
    """Return the list of hook `command` templates registered for PreToolUse
    events whose matcher covers tool_name, in the exact order they appear in
    settings.json (Claude Code executes hook groups in array order, and hooks
    within a group in array order)."""
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    pretool_groups = data.get("hooks", {}).get("PreToolUse", [])
    commands: list[str] = []
    for group in pretool_groups:
        matcher = group.get("matcher", "")
        # Matcher is a "|"-separated set of tool names (or a bare tool name).
        matcher_tools = {m.strip() for m in matcher.split("|") if m.strip()}
        if matcher_tools and tool_name not in matcher_tools:
            continue
        for hook in group.get("hooks", []):
            if hook.get("type") != "command":
                continue
            commands.append(hook["command"])
    return commands


def _resolve_command(command_template: str) -> list[str]:
    """Resolve a settings.json hook `command` string's ${CLAUDE_PROJECT_DIR}
    reference to the REAL repo root (where the hook scripts themselves live),
    NOT the tmp_git_repo test fixture. This mirrors how the pre-existing AC8
    test resolves LOCAL_MAIN_GUARD_SH / JAPANESE_PROSE_GUARD_SH: the hook
    SCRIPT is the real one on disk; the tmp_git_repo is only the subject
    repo/cwd the hook evaluates (passed separately as `cwd=` and as the
    CLAUDE_PROJECT_DIR env var the underlying Python guard modules read to
    resolve project-root context for THEIR OWN internal logic).
    """
    resolved = command_template.replace("${CLAUDE_PROJECT_DIR}", str(REPO_ROOT))
    return [resolved]


def run_pretool_hook_chain(
    payload: dict[str, Any],
    cwd: Path,
    tool_name: str = "Bash",
    extra_env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Execute every registered PreToolUse hook matching tool_name, in
    configured order, as a real subprocess with payload as stdin JSON.

    Returns a list of per-hook result dicts:
      {"command": <resolved argv[0]>, "hook_name": <basename stem>,
       "returncode": int, "decision": "allow"|"ask"|"block",
       "stdout": str, "stderr": str}

    Execution stops early (does not run subsequent hooks) once a "block"
    decision is observed -- this mirrors real Claude Code PreToolUse
    semantics, where the first blocking hook short-circuits the chain.
    """
    commands = load_pretool_hook_commands(tool_name)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    if extra_env:
        env.update(extra_env)

    results: list[dict[str, Any]] = []
    stdin_bytes = json.dumps(payload)
    for template in commands:
        argv = _resolve_command(template)
        hook_name = Path(argv[0].split(" ")[0]).stem
        proc = subprocess.run(
            argv, input=stdin_bytes, text=True, capture_output=True,
            cwd=str(cwd), env=env, timeout=30,
        )
        if proc.returncode == 0:
            decision = "allow"
        elif proc.returncode == 1:
            decision = "ask"
        else:
            decision = "block"
        results.append({
            "command": argv[0],
            "hook_name": hook_name,
            "returncode": proc.returncode,
            "decision": decision,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        })
        if decision == "block":
            break
    return results


def aggregate_decision(results: list[dict[str, Any]]) -> str:
    """Aggregate per-hook decisions the way Claude Code itself does: any
    "block" wins outright; else any "ask" wins; else "allow"."""
    if any(r["decision"] == "block" for r in results):
        return "block"
    if any(r["decision"] == "ask" for r in results):
        return "ask"
    return "allow"
