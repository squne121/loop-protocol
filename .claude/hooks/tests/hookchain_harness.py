"""hookchain_harness.py -- Issue #1539 fix_delta Blocker 4 (Issue #1636 revision).

Reusable helper that reads the REAL `.claude/settings.json` PreToolUse hook
registration (not a hand-picked subset) and executes every hook whose matcher
covers a given tool ("Bash" by default) IN CONFIGURED ORDER, as real
subprocesses against real stdin JSON -- exactly the shape Claude Code itself
invokes them with. This lets tests assert on the AGGREGATE decision across
the full chain, instead of hand-selecting two hooks and asserting each
returns 0 independently.

Decision vocabulary (Issue #1636 AC1)
--------------------------------------
Per-hook decisions are classified into one of six values:

  "deny"        -- explicit block. Either a structured
                   ``hookSpecificOutput.permissionDecision: "deny"`` (or the
                   deprecated top-level ``decision: "block"``), or exit code 2.
                   Exit code 2 takes precedence over all stdout, matching the
                   Claude Code PreToolUse contract.
  "defer"       -- structured ``hookSpecificOutput.permissionDecision: "defer"``.
                   No hook currently registered in settings.json emits this,
                   but the vocabulary supports it for forward compatibility.
  "ask"         -- structured ``hookSpecificOutput.permissionDecision: "ask"``
                   (or the deprecated ``decision`` field spelling an
                   equivalent value). Distinct from "hook_error" below --
                   exit code 1 alone is NOT "ask".
  "allow"       -- structured permissionDecision "allow" (or deprecated
                   ``decision: "approve"``) returned with exit code 0.
  "no_decision" -- exit code 0 with no structured permission decision. This
                   includes silent hooks and advisory-only envelopes; the hook
                   expressed no explicit allow/deny opinion.
  "hook_error"  -- any non-zero exit other than 2. Per the Claude Code
                   PreToolUse hook contract, this is a NON-BLOCKING execution
                   error (stderr surfaced to the user, tool execution
                   continues) and must not be conflated with "ask".

Claude Code PreToolUse hook exit code contract used by every hook in this
repo (see each hook's own header comment): 0 = allow / no-decision,
2 = block (deny), 1 = non-blocking hook execution error.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"

# The six-value decision vocabulary (Issue #1636 AC1).
DECISION_VALUES = frozenset(
    {"deny", "defer", "ask", "allow", "no_decision", "hook_error"}
)

_LEGACY_DECISION_MAP = {
    "block": "deny",
    "approve": "allow",
}


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


def _extract_structured_decision(stdout: str) -> str | None:
    """Attempt to recover a permission decision from structured JSON stdout.

    Understands both the current Claude Code PreToolUse contract
    (``hookSpecificOutput.permissionDecision``) and the deprecated top-level
    ``decision`` field (``"block"`` / ``"approve"``). Returns ``None`` when
    stdout is not parseable JSON, or is JSON but carries no recognizable
    decision field.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    hook_specific = data.get("hookSpecificOutput")
    if isinstance(hook_specific, dict):
        decision = hook_specific.get("permissionDecision")
        if isinstance(decision, str) and decision:
            return _LEGACY_DECISION_MAP.get(decision, decision)

    legacy_decision = data.get("decision")
    if isinstance(legacy_decision, str) and legacy_decision:
        return _LEGACY_DECISION_MAP.get(legacy_decision, legacy_decision)

    return None


def _has_hook_specific_output(stdout: str) -> bool:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and "hookSpecificOutput" in data


def classify_decision(returncode: int, stdout: str) -> str:
    """Classify a single hook's raw (returncode, stdout) into one of the six
    decision vocabulary values documented in this module's docstring."""
    # Claude Code only processes structured hook output after a successful
    # exit. Exit 2 always blocks, and every other non-zero exit is a
    # non-blocking hook execution error, regardless of stale/malformed stdout.
    if returncode == 2:
        return "deny"
    if returncode != 0:
        return "hook_error"

    structured = _extract_structured_decision(stdout)
    if structured is not None:
        if structured in DECISION_VALUES:
            return structured
        # Unrecognized structured value: treat conservatively as ask rather
        # than silently allowing.
        return "ask"

    return "no_decision"


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
       "returncode": int,
       "decision": "deny"|"defer"|"ask"|"allow"|"no_decision"|"hook_error",
       "stdout": str, "stderr": str}

    All matching hooks are run and collected even when one returns "deny".
    The harness is intentionally sequential, but this result collection
    mirrors Claude Code's aggregate semantics without modelling its scheduler.
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
        decision = classify_decision(proc.returncode, proc.stdout)
        results.append({
            "command": argv[0],
            "hook_name": hook_name,
            "returncode": proc.returncode,
            "decision": decision,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        })
    return results


def aggregate_permission_decision(results: list[dict[str, Any]]) -> str:
    """Return the lossless Claude Code PreToolUse aggregate decision.

    Permission decisions use precedence ``deny > defer > ask > allow``. If
    no hook made a permission decision, preserve ``no_decision``; if all hooks
    failed non-blockingly, preserve ``hook_error`` instead of claiming allow.
    """
    decisions = {r["decision"] for r in results}
    for decision in ("deny", "defer", "ask", "allow"):
        if decision in decisions:
            return decision
    if "no_decision" in decisions:
        return "no_decision"
    if "hook_error" in decisions:
        return "hook_error"
    return "no_decision"


def aggregate_decision(results: list[dict[str, Any]]) -> str:
    """Return the legacy three-value aggregate for existing callers.

    New callers that need Claude Code semantics must use
    :func:`aggregate_permission_decision`. The legacy facade keeps ``block`` /
    ``ask`` / ``allow`` output stable; ``defer`` maps conservatively to
    ``ask`` rather than being treated as allow.
    """
    decision = aggregate_permission_decision(results)
    if decision == "deny":
        return "block"
    if decision in {"defer", "ask"}:
        return "ask"
    return "allow"
