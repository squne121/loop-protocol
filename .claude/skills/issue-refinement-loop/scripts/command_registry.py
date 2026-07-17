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
from typing import Any
from urllib.parse import urlparse

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
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    # Issue #1439 Scope Delta 2: test-only command-id driving the real
    # executor -> real preflight -> real planner subprocess chain offline
    # (via --fixture, which bypasses the `gh` CLI). Production `preflight.run`
    # above is entirely unmodified -- this is a sibling entry, not a
    # generalization of it. Same trusted repo slug / default branch /
    # canonical root safety boundary applies (see
    # skill_runtime_command_policy.py / skill_runtime_exec.py).
    "preflight.run.fixture": {
        "id": "preflight.run.fixture",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--fixture", "{fixture}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_fixture",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "local_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "test_only": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "fixture": {"type": "repo_relative_file", "required": True},
        },
    },
    # Issue #1498: sibling exact profile — anchor-comment-scoped preflight
    # run. `preflight.run` above is entirely unmodified by this addition;
    # this is an independent registry entry, not a generalization of it.
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    # Issue #1547: scope_rollup.run exact command -- bound directly to
    # scripts/agent-guards/run_scope_rollup_preflight.py (NOT the
    # issue-refinement-loop skill_runtime_exec.py executor, which is
    # hard-coded to run_refinement_preflight.py and is out of this Issue's
    # Allowed Paths). This entry is a documentation/SSOT registration of the
    # canonical argv shape; scope_rollup.run is dispatched directly by
    # local_main_branch_guard.py / skill_runtime_command_policy.py, not via
    # skill_runtime_exec.py's render_command() dispatch path.
    "scope_rollup.run": {
        "id": "scope_rollup.run",
        "argv": [
            "uv", "run", "python3",
            "scripts/agent-guards/run_scope_rollup_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_scope_rollup_run",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "scope_rollup_run_result/v1",
        "timeout_seconds": 180,
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
    # Issue 1507: fail-closed grammar validator for the issue-reviewer
    # SubAgent stdout. Consumes the SubAgent exact final text via stdin
    # (no re-transcription); see validate_review_compact_output.py and
    # SKILL.md Step 2 / Step 2a.
    #
    # AC22 (P1-2 of the second owner review): argv is rendered with
    # `uv run --locked --offline --no-sync python3 ...` so the rendered
    # argv's actual execution semantics match this entry's own
    # `mutation: False` / `network_effect: local_only` declarations exactly
    # (no implicit lockfile sync / no implicit network access at run time).
    "review_compact.validate": {
        "id": "review_compact.validate",
        "argv": [
            "uv", "run", "--locked", "--offline", "--no-sync", "python3",
            f"{_SKILL_PREFIX}/validate_review_compact_output.py",
            "--issue-number", "{issue_number}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "issue_review_result_compact_v1/raw_text",
        "stdout_contract": "review_compact_validation_result/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "network_effect": "local_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
        },
    },
    # Issue #1532: parent-local replay integrity binding. The orchestrator is
    # the SOLE caller -- it supplies its own readiness/checker inventory
    # files, its OWN current body snapshot file, and the child SubAgent's
    # bounded REVIEWER_BLOCKER_CLAIM_V1 (never a raw child artifact or raw
    # findings/checker_evidence), then replays
    # `reviewer_claim_replay.analyze()` in-process to derive
    # PARENT_REPLAY_BINDING_ARTIFACT_V1 (PARENT_REPLAY_NEXT_STATE +
    # binding_digest, surfaced as PARENT_REPLAY_BINDING_DIGEST). This is a
    # parent-local replay integrity binding, NOT a producer identity /
    # supply-chain provenance attestation (no signatures, no key
    # management, no same-OS-UID authentication).
    "parent_replay.bind": {
        "id": "parent_replay.bind",
        "argv": [
            "uv", "run", "--locked", "--offline", "--no-sync", "python3",
            f"{_SKILL_PREFIX}/parent_replay_binding.py",
            "--reviewer-blocker-claim-file", "{reviewer_blocker_claim_file}",
            "--readiness-result-file", "{readiness_result_file}",
            "--previous-state-inline", "{previous_state_inline}",
            "--current-body-file", "{current_body_file}",
            "--issue-url", "{issue_url}",
            "--repository-full-name", "{repo}",
            "--issue-number", "{issue_number}",
            "--refinement-session-id", "{refinement_session_id}",
            "--iteration-id", "{iteration_id}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "parent_replay_binding_artifact/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "network_effect": "local_only",
        "placeholders": {
            "reviewer_blocker_claim_file": {"type": "repo_relative_file", "required": True},
            "readiness_result_file": {"type": "repo_relative_file", "required": True},
            "previous_state_inline": {"type": "string", "required": True},
            "current_body_file": {"type": "repo_relative_file", "required": True},
            "issue_url": {"type": "url", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "issue_number": {"type": "positive_int", "required": True},
            "refinement_session_id": {"type": "string", "required": True},
            "iteration_id": {"type": "string", "required": True},
        },
    },
    # Issue #1532 AC1/AC4/High-1: V2 validator sibling of
    # `review_compact.validate` -- REQUIRES a binding artifact file and full
    # parent-owned identity/body context. Never optional; there is no
    # "V2 validation without a binding artifact" code path.
    "review_compact.validate_v2": {
        "id": "review_compact.validate_v2",
        "argv": [
            "uv", "run", "--locked", "--offline", "--no-sync", "python3",
            f"{_SKILL_PREFIX}/validate_review_compact_output.py",
            "--v2",
            "--issue-number", "{issue_number}",
            "--binding-artifact-file", "{binding_artifact_file}",
            "--repository-full-name", "{repo}",
            "--refinement-session-id", "{refinement_session_id}",
            "--iteration-id", "{iteration_id}",
            "--current-body-file", "{current_body_file}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "issue_review_result_compact_v2/raw_text",
        "stdout_contract": "review_compact_validation_result/v2",
        "timeout_seconds": 30,
        "mutation": False,
        "network_effect": "local_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "binding_artifact_file": {"type": "repo_relative_file", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "refinement_session_id": {"type": "string", "required": True},
            "iteration_id": {"type": "string", "required": True},
            "current_body_file": {"type": "repo_relative_file", "required": True},
        },
    },
    # Issue #1532 AC5/High-3: the sole V2 state-write path. Rejects
    # caller-fabricated validation_status and cross-issue/session/digest
    # substitution via the required `expected_*` identity args.
    "state.write-v2": {
        "id": "state.write-v2",
        "argv": [
            "uv", "run", "--locked", "--offline", "--no-sync", "python3",
            f"{_SKILL_PREFIX}/reviewer_claim_replay_state_store.py",
            "--write-v2",
            "--state-dir", "{state_dir}",
            "--repository-full-name", "{repo}",
            "--issue-number", "{issue_number}",
            "--refinement-session-id", "{refinement_session_id}",
            "--validation-result-v2-inline", "{validation_result_v2_inline}",
            "--expected-parent-binding-digest", "{expected_parent_binding_digest}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "reviewer_claim_replay_state_store_result/v1",
        "timeout_seconds": 30,
        "mutation": True,
        "network_effect": "local_only",
        "placeholders": {
            "state_dir": {"type": "repo_relative_file", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "issue_number": {"type": "positive_int", "required": True},
            "refinement_session_id": {"type": "string", "required": True},
            "validation_result_v2_inline": {"type": "string", "required": True},
            "expected_parent_binding_digest": {"type": "string", "required": True},
        },
    },
}

# ---------------------------------------------------------------------------
# Placeholder validators (AC3, Blocker 7)
# ---------------------------------------------------------------------------

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HTTPS_URL_RE = re.compile(r"^https://")
_VERDICT_VALUES: frozenset[str] = frozenset({"approve", "request_changes", "needs-fix"})

# Issue #1498: canonical GitHub issue comment URL shape.
#   https://github.com/{owner}/{repo}/issues/{digits}#issuecomment-{digits}
# Character classes deliberately exclude "%" so any percent-encoded disguise
# of the canonical shape (e.g. %2e%2e, encoded "#") is rejected by
# construction -- the regex simply cannot match a "%" byte anywhere in
# owner/repo/issue/comment, so no separate decode step is required to catch
# it. Query strings, extra fragments/suffixes, trailing slashes, `/pull/`
# paths, and `discussion_r...` fragments are all rejected because the
# pattern is anchored end-to-end (fullmatch) with no room for extra
# characters.
_GH_ISSUE_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"issues/(?P<issue>[1-9][0-9]*)"
    r"#issuecomment-(?P<comment>[1-9][0-9]*)$"
)


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

    elif ph_type == "github_issue_comment_url":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty GitHub issue comment URL, got {value!r}"
            )
        if "%" in value:
            raise ValueError(
                f"Placeholder '{name}': percent-encoding not allowed in canonical "
                f"GitHub issue comment URL, got {value!r}"
            )
        match = _GH_ISSUE_COMMENT_URL_RE.fullmatch(value)
        if match is None:
            raise ValueError(
                f"Placeholder '{name}': must be a canonical "
                f"https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M> URL, got {value!r}"
            )
        # Defense-in-depth cross-check with urlparse: the regex above already
        # rejects userinfo/port/query by construction, but this makes the
        # rejection explicit and independent of the regex implementation.
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError(f"Placeholder '{name}': scheme must be https, got {value!r}")
        if parsed.hostname != "github.com":
            raise ValueError(f"Placeholder '{name}': host must be github.com, got {value!r}")
        if parsed.username or parsed.password:
            raise ValueError(f"Placeholder '{name}': userinfo not allowed, got {value!r}")
        if parsed.port is not None:
            raise ValueError(f"Placeholder '{name}': port not allowed, got {value!r}")
        if parsed.query:
            raise ValueError(f"Placeholder '{name}': query string not allowed, got {value!r}")

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
