#!/usr/bin/env python3
"""
command_registry.py

ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 — single source of truth for all
operator-facing / orchestrator-facing commands in the issue-refinement-loop.

SubAgents and the main thread consume this registry to build argv arrays;
they MUST NOT hand-craft shell strings.

CLI:
    python command_registry.py --list
    => prints ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 JSON to stdout

API:
    from command_registry import render_command, validate_shell_string, REGISTRY

Security contract:
    - render_command() always returns argv: list[str] (never a shell string)
    - validate_shell_string() rejects compound operators / substitutions /
      redirections / nested shell / unknown commands
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Registry schema version
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "ISSUE_REFINEMENT_COMMAND_REGISTRY_V1"

# Relative to repo root (used as cwd_policy=repo_root)
_SKILL_PREFIX = ".claude/skills/issue-refinement-loop/scripts"

# ---------------------------------------------------------------------------
# Deny tokens for shell-string validation (AC4)
# ---------------------------------------------------------------------------

DENY_TOKENS: frozenset[str] = frozenset({
    # Compound operators
    "&&", "||", ";",
    # Pipe and redirections
    "|", ">", "<", ">>", "<<",
    # Process / command substitution
    "$(", "<(", ">(", "`",
    # Shell launchers that bypass argv
    "bash", "sh",
    # Environment injection
    "env",
    # Directory traversal trick
    "cd",
})

# Characters that indicate shell operators (for unspaced operator detection — Blocker 5)
_SHELL_OPERATOR_CHARS: frozenset[str] = frozenset({"&", "|", ";", "<", ">"})

# Regex patterns that catch substitution syntax even without tokenization
_SUBST_PATTERNS = re.compile(
    r"""
    \$\(        |   # command substitution $(...)
    `[^`]+`     |   # backtick substitution
    <\(         |   # process substitution <(...)
    >\(             # process substitution >(...)
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Known executable allowlist for shell-string validation (Blocker 6)
# ---------------------------------------------------------------------------

KNOWN_EXECUTABLES: frozenset[str] = frozenset({
    "uv",
    "pnpm",
    "gh",
    "rg",
    "python3",
    "pytest",
    "node",
    "npm",
    "git",
    "jq",
    "curl",
    "cat",
    "echo",
    "ls",
    "find",
    "grep",
    "which",
    "true",
    "false",
    "test",
    "mkdir",
})

# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------

REGISTRY: dict[str, dict[str, Any]] = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "plan.run": {
        "id": "plan.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/plan_refinement_loop.py",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "refinement_loop_planner_input/v1",
        "stdout_contract": "refinement_loop_plan/v1",
        "timeout_seconds": 60,
        "mutation": False,
        "placeholders": {},
    },
    "decide.run": {
        "id": "decide.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/decide_next_loop_action.py",
            "--loop-state-file", "{loop_state_file}",
            "--review-result-verdict", "{verdict}",
            "--max-iterations", "{max_iterations}",
            "--phase-state-file", "{phase_state_file}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "decide_next_loop_action/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "loop_state_file": {"type": "repo_relative_file", "required": True},
            "verdict": {"type": "verdict", "required": True},
            "max_iterations": {"type": "positive_int", "required": False},
            "phase_state_file": {"type": "repo_relative_file", "required": False},
        },
    },
    "gh.issue.view": {
        "id": "gh.issue.view",
        "argv": [
            "gh", "issue", "view", "{issue_number}",
            "--repo", "{repo}",
            "--json", "title,body,number,state,comments,labels",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "gh_issue_json",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "gh.issue.comment": {
        "id": "gh.issue.comment",
        "argv": [
            "gh", "issue", "comment", "{issue_number}",
            "--repo", "{repo}",
            "--body-file", "{body_file}",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "none",
        "timeout_seconds": 30,
        "mutation": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "body_file": {"type": "body_file", "required": True},
        },
    },
    "gh.issue.comments.list": {
        "id": "gh.issue.comments.list",
        "argv": [
            "gh", "api",
            "repos/{repo}/issues/{issue_number}/comments?per_page=100",
            "--paginate", "--slurp",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "gh_issue_comments_json",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "repo": {"type": "owner_repo", "required": True},
            "issue_number": {"type": "positive_int", "required": True},
        },
    },
    "uv.pytest": {
        "id": "uv.pytest",
        "argv": [
            "uv", "run", "pytest", "{test_path}", "-v",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "pytest_output",
        "timeout_seconds": 300,
        "mutation": False,
        "placeholders": {
            "test_path": {"type": "repo_relative_file", "required": True},
        },
    },
    "pnpm.typecheck": {
        "id": "pnpm.typecheck",
        "argv": ["pnpm", "typecheck"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "typecheck_output",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.lint": {
        "id": "pnpm.lint",
        "argv": ["pnpm", "lint"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "lint_output",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.test": {
        "id": "pnpm.test",
        "argv": ["pnpm", "test"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "test_output",
        "timeout_seconds": 300,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.build": {
        "id": "pnpm.build",
        "argv": ["pnpm", "build"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "build_output",
        "timeout_seconds": 180,
        "mutation": False,
        "placeholders": {},
    },
}

# ---------------------------------------------------------------------------
# Placeholder validators (AC3, Blocker 7)
# ---------------------------------------------------------------------------

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HTTPS_URL_RE = re.compile(r"^https://")
_VERDICT_VALUES: frozenset[str] = frozenset({"approve", "request_changes", "needs-fix"})


def _validate_placeholder_value(name: str, value: Any, spec: dict) -> None:
    """Validate a single placeholder value against its type spec.

    Raises ValueError for invalid values (fail-closed per AC3).
    """
    ph_type = spec.get("type", "string")

    if ph_type == "positive_int":
        if isinstance(value, str):
            try:
                int_val = int(value)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Placeholder '{name}': expected positive_int, got non-numeric string {value!r}"
                )
        elif isinstance(value, int):
            int_val = value
        else:
            raise ValueError(
                f"Placeholder '{name}': expected positive_int, got {type(value).__name__}"
            )
        if int_val <= 0:
            raise ValueError(
                f"Placeholder '{name}': must be > 0, got {int_val}"
            )

    elif ph_type == "owner_repo":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty owner/repo string, got {value!r}"
            )
        if not _OWNER_REPO_RE.match(value):
            raise ValueError(
                f"Placeholder '{name}': must match owner/repo format, got {value!r}"
            )

    elif ph_type in ("path", "repo_relative_file", "body_file"):
        # repo_relative_file / body_file: absolute path 禁止、.. 禁止、NUL/newline 禁止、leading - 禁止
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty path string, got {value!r}"
            )
        if ph_type in ("repo_relative_file", "body_file"):
            if value.startswith("/"):
                raise ValueError(
                    f"Placeholder '{name}': absolute path not allowed, got {value!r}"
                )
            if ".." in value.split("/"):
                raise ValueError(
                    f"Placeholder '{name}': path traversal '..' not allowed, got {value!r}"
                )
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError(
                    f"Placeholder '{name}': NUL or newline in path not allowed, got {value!r}"
                )
            if value.startswith("-"):
                raise ValueError(
                    f"Placeholder '{name}': leading '-' in path not allowed, got {value!r}"
                )

    elif ph_type == "url":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty URL string, got {value!r}"
            )
        if not _HTTPS_URL_RE.match(value):
            raise ValueError(
                f"Placeholder '{name}': URL must start with https://, got {value!r}"
            )

    elif ph_type == "verdict":
        if not isinstance(value, str) or value not in _VERDICT_VALUES:
            raise ValueError(
                f"Placeholder '{name}': must be one of {sorted(_VERDICT_VALUES)}, got {value!r}"
            )

    elif ph_type == "string":
        if not isinstance(value, str):
            raise ValueError(
                f"Placeholder '{name}': expected string, got {type(value).__name__}"
            )


def render_command(command_id: str, params: dict[str, Any]) -> list[str]:
    """Render a registry command by substituting placeholders.

    Returns: argv as list[str] — never a shell string.

    Raises:
        KeyError: unknown command_id
        ValueError: invalid placeholder value (fail-closed per AC3)
        ValueError: extra (undefined) params provided
        ValueError: unresolved placeholder remains in rendered argv
    """
    if command_id not in REGISTRY:
        raise KeyError(f"Unknown command_id: {command_id!r}")

    entry = REGISTRY[command_id]
    placeholders = entry.get("placeholders", {})

    # Reject extra params not defined in placeholders (Blocker 7)
    extra_params = set(params.keys()) - set(placeholders.keys())
    if extra_params:
        raise ValueError(
            f"Extra params not defined for command {command_id!r}: {sorted(extra_params)}"
        )

    # Type-validate all provided params
    for name, value in params.items():
        if name in placeholders:
            _validate_placeholder_value(name, value, placeholders[name])

    # Check required placeholders are present
    for name, spec in placeholders.items():
        if spec.get("required", False) and name not in params:
            raise ValueError(
                f"Required placeholder '{name}' missing for command {command_id!r}"
            )

    # Substitute into argv template
    # Supports both:
    #   - Whole-token placeholders: "{name}" -> str(value)
    #   - Partial-token placeholders: "prefix/{name}/suffix" -> "prefix/value/suffix"
    rendered: list[str] = []
    for token in entry["argv"]:
        if "{" in token and "}" in token:
            # Replace all placeholders in the token (supports partial substitution)
            result_token = token
            for ph_name, value in params.items():
                result_token = result_token.replace(f"{{{ph_name}}}", str(value))
            rendered.append(result_token)
        else:
            rendered.append(token)

    # Verify no unresolved placeholders remain in required positions (Blocker 7)
    for token in rendered:
        if token.startswith("{") and token.endswith("}"):
            ph_name = token[1:-1]
            spec = placeholders.get(ph_name, {})
            if spec.get("required", False):
                raise ValueError(
                    f"Unresolved required placeholder '{{{ph_name}}}' in rendered argv for {command_id!r}"
                )

    return rendered


# ---------------------------------------------------------------------------
# Shell string validator (AC4, Blocker 5, Blocker 6)
# ---------------------------------------------------------------------------

def validate_shell_string(s: str) -> dict[str, Any]:
    """Classify an untrusted shell string using shlex tokenization + deny matrix.

    Returns:
        {"ok": True, "blocked_reason": None}           — safe
        {"ok": False, "blocked_reason": "<reason>"}    — blocked

    AC4 deny list covers:
      - Compound operators: &&, ||, ;
      - Pipe: |
      - Redirections: >, <, >>, <<
      - Command / process substitution: $(), ``, <(), >()
      - Shell launchers: bash, sh (including bash -lc, sh -c)
      - Environment injection: env
      - Directory traversal: cd

    Blocker 5 — unspaced operator detection:
      Uses punctuation_chars=True in shlex to tokenize operators like
      cmd&&rm, a;b, cmd|grep, echo>x, cat<<EOF as separate tokens.

    Blocker 6 — unknown executable block:
      The first token (executable) must be in KNOWN_EXECUTABLES.
      If not, it is blocked. argv list[str] types are NOT validated here
      (only string inputs are validated).
    """
    # First: regex scan for substitution syntax (catches $( even without spaces)
    if _SUBST_PATTERNS.search(s):
        match = _SUBST_PATTERNS.search(s)
        return {"ok": False, "blocked_reason": f"command_substitution_detected: {match.group()!r}"}

    # Second: shlex tokenization with punctuation_chars=True to detect unspaced operators (Blocker 5)
    try:
        lexer = shlex.shlex(s, posix=True, punctuation_chars=True)
        lexer.whitespace_split = False
        tokens = list(lexer)
    except ValueError as exc:
        # shlex failed to parse — treat as blocked (fail-closed)
        return {"ok": False, "blocked_reason": f"shlex_parse_error: {exc}"}

    # Check for shell operator characters in tokens (Blocker 5)
    for token in tokens:
        if any(ch in token for ch in _SHELL_OPERATOR_CHARS):
            return {"ok": False, "blocked_reason": f"shell_operator_detected: {token!r}"}

    # Check deny token list (AC4)
    for token in tokens:
        if token in DENY_TOKENS:
            return {"ok": False, "blocked_reason": f"denied_token: {token!r}"}

    # Blocker 6: check first token (executable) against known allowlist
    # Filter out empty tokens from tokenization
    non_empty_tokens = [t for t in tokens if t]
    if non_empty_tokens:
        first_token = non_empty_tokens[0]
        if first_token not in KNOWN_EXECUTABLES:
            return {
                "ok": False,
                "blocked_reason": f"unknown_executable: {first_token!r} not in KNOWN_EXECUTABLES allowlist",
            }

    return {"ok": True, "blocked_reason": None}


# ---------------------------------------------------------------------------
# Registry export
# ---------------------------------------------------------------------------

def export_registry() -> dict[str, Any]:
    """Return the full registry as a serializable dict."""
    return {
        "schema": SCHEMA_VERSION,
        "commands": {k: dict(v) for k, v in REGISTRY.items()},
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 CLI"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 JSON to stdout",
    )
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.list:
        print(json.dumps(export_registry(), ensure_ascii=False, indent=2))
    else:
        print("Usage: command_registry.py --list", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
