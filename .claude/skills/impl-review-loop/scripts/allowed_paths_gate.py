#!/usr/bin/env python3
"""Deterministic evaluation module for ALLOWED_PATHS_GATE_RESULT_V1.

This module is the canonical source for evaluating whether:
1. Changed files fall within Allowed Paths (allow / fail_closed)
2. Commands are safe to execute (hard_invariant_block / allow)
3. File paths contain sensitive data (hard_invariant_block / normal)
4. Quality issues should block execution (advisory / hard_invariant_block)

Used by impl-review-loop Step 1 worker and test fixtures for regression testing.

AC8 note: This is the ONLY source for gate evaluation logic. Tests import these
functions directly; they do not redefine evaluators inline (preventing tautology).
"""

from __future__ import annotations

import fnmatch
import hashlib
from typing import Any


def evaluate_allowed_paths_gate(
    allowed_paths: list[str],
    changed_files: list[str],
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Evaluate ALLOWED_PATHS_GATE_RESULT_V1.

    Args:
        allowed_paths: List of glob patterns (e.g., [".claude/agents/**", "tests/**"])
        changed_files: List of files changed by worker (e.g., [".claude/agents/worker.md"])
        manifest_sha256: Expected manifest hash (for race guard). If provided and
            does not match, returns status="stale_snapshot".

    Returns:
        dict with status: "ok" | "fail_closed" | "stale_snapshot" | "indeterminate"
        - "ok": all changed_files match allowed_paths patterns
        - "fail_closed": one or more changed_files violate allowed_paths
        - "stale_snapshot": manifest_sha256 mismatch (contract stale)
        - "indeterminate": input validation error (None allowed_paths, etc)
    """
    # Validate inputs
    if not allowed_paths or None in allowed_paths:
        return {
            "status": "indeterminate",
            "manifest_snapshot_sha256": None,
            "violations": [],
            "final_diff_paths": changed_files,
            "reason": "allowed_paths is None or empty",
        }

    if not isinstance(changed_files, list):
        return {
            "status": "indeterminate",
            "manifest_snapshot_sha256": None,
            "violations": [],
            "final_diff_paths": [],
            "reason": "changed_files must be a list",
        }

    # Compute actual manifest hash
    manifest_str = "|".join(sorted(allowed_paths))
    actual_manifest_hash = hashlib.sha256(manifest_str.encode()).hexdigest()

    # Check for stale snapshot
    if manifest_sha256 is not None and manifest_sha256 != actual_manifest_hash:
        return {
            "status": "stale_snapshot",
            "manifest_snapshot_sha256": actual_manifest_hash,
            "violations": [],
            "final_diff_paths": changed_files,
            "reason": f"manifest mismatch: expected {manifest_sha256}, got {actual_manifest_hash}",
        }

    # Check each file against patterns
    violations = []
    for file_path in changed_files:
        matched = False
        for pattern in allowed_paths:
            if fnmatch.fnmatch(file_path, pattern):
                matched = True
                break
        if not matched:
            violations.append(file_path)

    # Determine final status
    status = "fail_closed" if violations else "ok"

    return {
        "status": status,
        "manifest_snapshot_sha256": actual_manifest_hash,
        "violations": violations,
        "final_diff_paths": changed_files,
    }


def classify_command(cmd: str) -> str:
    """Classify command as hard_invariant_block or allow.

    Hard invariant block cases:
    - git reset --hard, --force-with-lease, etc (destructive git)
    - git push --force, -f (force push)
    - rm -rf, dd if=/dev/zero, etc (destructive file ops)
    - chmod 000 (permission death)

    Allow cases:
    - pnpm typecheck, lint, test, build (standard verification)
    - git diff, status, log, show, rev-parse (read-only inspection)
    - gh pr view, list, issue view, list (read-only GitHub inspection)
    - grep, rg, find, etc (read-only text search)

    Args:
        cmd: Shell command string

    Returns:
        "hard_invariant_block" | "allow"
    """
    # Hard invariant: destructive git commands
    if "git reset" in cmd and "--hard" in cmd:
        return "hard_invariant_block"
    if "git push" in cmd and "--force" in cmd:
        return "hard_invariant_block"
    if "git push" in cmd and " -f" in cmd:
        return "hard_invariant_block"

    # Hard invariant: destructive file operations
    if "rm -rf" in cmd or "rm -rf /" in cmd:
        return "hard_invariant_block"
    if "dd if=/dev/zero" in cmd:
        return "hard_invariant_block"
    if "chmod 000" in cmd:
        return "hard_invariant_block"

    # Allow: standard verification
    standard_verification = [
        "pnpm typecheck",
        "pnpm lint",
        "pnpm test",
        "pnpm build",
    ]
    if any(cmd.startswith(pattern) for pattern in standard_verification):
        return "allow"

    # Allow: read-only git inspection
    read_only_git = ["diff", "status", "log", "show", "rev-parse"]
    if cmd.startswith("git ") and any(ro in cmd for ro in read_only_git):
        return "allow"

    # Allow: read-only GitHub inspection
    if ("gh pr view" in cmd or "gh issue view" in cmd or
        "gh pr list" in cmd or "gh issue list" in cmd):
        return "allow"

    # Allow: read-only text search
    if cmd.startswith("grep ") or cmd.startswith("rg "):
        return "allow"

    # Default: unknown command => must be evaluated elsewhere
    return "allow"  # permissive default for unknown commands


def classify_path(path: str) -> str:
    """Classify file path as hard_invariant_block or normal.

    Hard invariant block cases:
    - .env, .env.*, .env.local, .env.prod
    - .git/config, .git/credentials
    - credentials.json, secret*.key, private*.pem
    - api_key.txt, token.*.txt, password.txt

    Args:
        path: File path string (e.g., ".env", "src/credentials.json")

    Returns:
        "hard_invariant_block" | "normal"
    """
    path_lower = path.lower()

    # Sensitive patterns
    sensitive_patterns = [
        ".env",
        "credentials",
        "secret",
        "private",
        "api_key",
        "token",
        "password",
        ".git",
    ]

    for pattern in sensitive_patterns:
        if pattern in path_lower:
            return "hard_invariant_block"

    return "normal"


def classify_quality_issue(kind: str) -> dict[str, bool]:
    """Classify quality issue as advisory or blocking.

    Policy: **Quality issues (style, language, format, naming) are ALWAYS advisory.**
    They must never block execution. This is a documented policy, not a bug.

    Acceptable kinds:
    - "language": Japanese/English consistency
    - "commit_format": Conventional Commits style
    - "pr_body_format": PR body structure / section order
    - "test_naming": GIVEN/WHEN/THEN convention
    - "yaml_key_order": YAML/JSON key ordering
    - "docstring_quality": Comment / docstring clarity
    - any other style/format/language issue

    Args:
        kind: Issue category (language | commit_format | test_naming | etc)

    Returns:
        dict with keys:
        - "category": "advisory" (always, by policy)
        - "should_block": False (always, by policy)
    """
    # All quality issues are advisory by design
    # This is NOT a bug; it is the documented loop-prevention policy
    return {
        "category": "advisory",
        "should_block": False,
    }
