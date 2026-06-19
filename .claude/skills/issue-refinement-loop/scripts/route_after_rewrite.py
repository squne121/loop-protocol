#!/usr/bin/env python3
"""
route_after_rewrite.py — Rewrite Loop Router Wrapper

Orchestrates the full rewrite-loop routing sequence in a single invocation:
  1. Fetch issue body from GitHub (or load from file for testing)
  2. Compute body_hash (sha256)
  3. Run check_issue_contract.py (checker) — stdout JSON only; stderr NOT merged
  4. Build a schema-valid LOOP_REWRITE_ROUTER_STATE_V1 from checker output
  5. Call decide_rewrite_route.py and print the RouteResult JSON to stdout

Design constraints (AC4):
  - Checker exit 0 (approve) and exit 1 (needs-fix) are BOTH routed normally.
    Exit 1 is NOT treated as an infrastructure failure.
  - checker stdout JSON is parsed exclusively; stderr is NOT merged into JSON.
  - Only LOOP_REWRITE_ROUTER_STATE_V1 schema-allowlist keys are set; no
    additional keys from the checker result are injected into the router state.
  - load_rewrite_router_state() / save_rewrite_router_state() are used for
    persistence; attempt counter is NEVER silently reset to 0.

Usage:
    uv run python3 route_after_rewrite.py \\
        --issue <number> \\
        --repo <owner/repo> \\
        --state-path <path/to/state.json> \\
        --max-rewrite-attempts <int>

    # File-based mode (for testing, bypasses gh):
    uv run python3 route_after_rewrite.py \\
        --file <path/to/issue_body.md> \\
        --state-path <path/to/state.json> \\
        --max-rewrite-attempts <int>

Exit codes:
    0 — RouteResult JSON written to stdout
    2 — Invalid input / schema error
    3 — Internal error (checker infrastructure failure, not checker exit 1)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPTS_DIR.parent
_CHECKER_SCRIPT = (
    _SKILL_ROOT.parent / "review-issue" / "scripts" / "check_issue_contract.py"
)
_ROUTER_SCRIPT = _SCRIPTS_DIR / "decide_rewrite_route.py"

# Import decide_rewrite_route helpers for persistence
sys.path.insert(0, str(_SCRIPTS_DIR))
from decide_rewrite_route import (  # noqa: E402
    LOOP_REWRITE_ROUTER_STATE_V1,
    SCHEMA_VERSION,
    load_rewrite_router_state,
    save_rewrite_router_state,
    validate_state_dict,
    RewriteRouterStateError,
)


# ---------------------------------------------------------------------------
# LOOP_REWRITE_ROUTER_STATE_V1 schema allowlist
# These are the only keys that may appear in the router state dict.
# Do NOT add checker_result keys or any other extra fields here.
# ---------------------------------------------------------------------------

_STATE_ALLOWLIST = frozenset({
    "schema_version",
    "rewrite_attempt_count",
    "max_rewrite_attempts",
    "checker_exit_code",
    "checked_body_sha256",
    "fix_category",
    "rewrite_history",
    "occurrence_count",
    "missing_sections",
    "missing_contract_keys",
    "previous_checked_body_sha256",
    "previous_missing_sections",
    "previous_missing_contract_keys",
    "source_issue_body_sha256",
    "replay_safe",
    "source_body_reset",
})


# ---------------------------------------------------------------------------
# Body hash helper
# ---------------------------------------------------------------------------


def _sha256_of_body(body: str) -> str:
    """Compute sha256 hex digest of the issue body text (UTF-8 encoded)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Issue body fetcher
# ---------------------------------------------------------------------------


def _fetch_issue_body_from_github(issue_number: int, repo: str) -> str:
    """Fetch issue body text from GitHub using gh CLI."""
    cmd = [
        "gh", "issue", "view", str(issue_number),
        "--repo", repo,
        "--json", "body",
        "--jq", ".body",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"ERROR: gh issue view failed (exit {result.returncode}): {result.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)
    return result.stdout.strip()


def _load_body_from_file(path: str) -> str:
    """Load issue body from a local file (for testing)."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        print(f"ERROR: Cannot read issue file {path}: {e}", file=sys.stderr)
        sys.exit(3)

    # Strip optional YAML-like front-matter (same convention as load_fixture_file)
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            content = content[end + 5:].strip()

    return content


# ---------------------------------------------------------------------------
# Checker invocation
# ---------------------------------------------------------------------------


def _run_checker(
    body: str,
    source_file_path: Optional[str] = None,
) -> tuple[int, dict]:
    """
    Run check_issue_contract.py on the given body text.

    Returns (exit_code, checker_json).

    AC4: checker exit 1 is NOT an infrastructure failure — it means needs-fix.
    Only exit codes other than 0/1 are treated as infrastructure failures.

    stderr is deliberately NOT captured into the JSON payload.

    When source_file_path is provided (--file mode), the original file is passed
    directly to the checker so that fixture front-matter (LABELS / TITLE) is
    preserved for issue_kind detection. In --issue mode (body from GitHub), a
    temp file is created from the fetched body text.
    """
    import tempfile

    if source_file_path is not None:
        # Pass original file directly — checker's load_fixture_file handles front-matter
        checker_file = source_file_path
        tmp_to_delete: Optional[str] = None
    else:
        # Write body to a temp file (no front-matter; GitHub-fetched body)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        ) as tf:
            tf.write(body)
            checker_file = tf.name
            tmp_to_delete = tf.name

    try:
        proc = subprocess.run(
            [sys.executable, str(_CHECKER_SCRIPT), "--file", checker_file, "--json"],
            capture_output=True,
            text=True,
        )
    finally:
        if tmp_to_delete is not None:
            os.unlink(tmp_to_delete)

    # exit 0 = approve, exit 1 = needs-fix — both are normal routing outcomes
    # exit 2+ = infrastructure/input error
    if proc.returncode not in (0, 1):
        print(
            f"ERROR: check_issue_contract.py exited with unexpected code "
            f"{proc.returncode}.\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)

    # Parse stdout JSON only — never merge stderr
    try:
        checker_json = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(
            f"ERROR: check_issue_contract.py stdout is not valid JSON: {e}\n"
            f"stdout: {proc.stdout[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)

    return proc.returncode, checker_json


# ---------------------------------------------------------------------------
# Build schema-valid state dict (allowlist enforcement)
# ---------------------------------------------------------------------------


def _derive_fix_category(
    blocking_issues: list[str],
    missing_sections: list[str],
    missing_contract_keys: list[str],
) -> str:
    """Derive a deterministic fix category from checker blockers."""
    if missing_sections:
        return "missing_section"
    if missing_contract_keys:
        return "missing_contract_key"

    for msg in blocking_issues:
        normalized = msg.lower()
        if "contract" in normalized and "fail" in normalized:
            return "unknown_contract_failure"
    return "unknown_contract_failure"


def _build_state_dict(
    *,
    rewrite_attempt_count: int,
    max_rewrite_attempts: int,
    checker_exit_code: int,
    checked_body_sha256: str,
    checker_json: dict,
    previous_state: Optional[LOOP_REWRITE_ROUTER_STATE_V1],
    source_issue_body_sha256: Optional[str],
    source_body_reset: bool,
) -> dict:
    """
    Construct a LOOP_REWRITE_ROUTER_STATE_V1 dict from first principles.

    Only keys in _STATE_ALLOWLIST are set.  checker_json keys are NOT merged
    (AC4c: schema allowlist-outside keys must not enter router state).

    missing_sections and missing_contract_keys are extracted from checker_json
    by explicit key lookup — no update() / **-unpacking of the full result.
    """
    # Extract only the fields we need from checker_json (explicit key access)
    blocking_issues: list[str] = checker_json.get("blocking_issues") or []

    # missing_sections: extract section names from blocking_issues that match
    # "必須セクション '## X' が存在しない" pattern
    import re as _re
    missing_sections: list[str] = []
    for msg in blocking_issues:
        m = _re.search(r"必須セクション '## ([^']+)' が存在しない", msg)
        if m:
            missing_sections.append(m.group(1))

    # missing_contract_keys: blocking issues that reference missing contract keys
    # (pattern: "contract key '<key>' が欠けている" or similar)
    # For now, use blocking_issues that reference "contract" or "key" patterns
    missing_contract_keys: list[str] = []
    for msg in blocking_issues:
        m = _re.search(r"contract(?:_key|_keys)?\s+['\"]?([A-Za-z_]+)['\"]?\s+が欠けている", msg)
        if m:
            missing_contract_keys.append(m.group(1))

    fix_category = _derive_fix_category(
        blocking_issues,
        missing_sections,
        missing_contract_keys,
    )

    # previous_* fields from loaded state
    previous_checked_body_sha256: Optional[str] = None
    previous_missing_sections: list[str] = []
    previous_missing_contract_keys: list[str] = []
    previous_rewrite_history: list[str] = []
    if previous_state is not None:
        if source_body_reset:
            # source body changed by human — reset stale history.
            previous_rewrite_history = []
        else:
            previous_rewrite_history = list(previous_state.rewrite_history)
        previous_checked_body_sha256 = previous_state.checked_body_sha256
        previous_missing_sections = list(previous_state.missing_sections)
        previous_missing_contract_keys = list(previous_state.missing_contract_keys)

    rewrite_history = previous_rewrite_history + [fix_category]
    occurrence_count = rewrite_history.count(fix_category)

    state_dict: dict = {
        "schema_version": SCHEMA_VERSION,
        "rewrite_attempt_count": rewrite_attempt_count,
        "max_rewrite_attempts": max_rewrite_attempts,
        "checker_exit_code": checker_exit_code,
        "checked_body_sha256": checked_body_sha256,
        "fix_category": fix_category,
        "rewrite_history": rewrite_history,
        "occurrence_count": occurrence_count,
        "missing_sections": missing_sections,
        "missing_contract_keys": missing_contract_keys,
        "previous_checked_body_sha256": previous_checked_body_sha256,
        "previous_missing_sections": previous_missing_sections,
        "previous_missing_contract_keys": previous_missing_contract_keys,
        "source_issue_body_sha256": source_issue_body_sha256,
        "replay_safe": previous_state is not None,
        "source_body_reset": source_body_reset,
    }

    # Enforce allowlist (defensive: remove any unexpected keys)
    unexpected = set(state_dict.keys()) - _STATE_ALLOWLIST
    for key in unexpected:
        del state_dict[key]

    return state_dict


# ---------------------------------------------------------------------------
# Router invocation (subprocess, same as orchestrator)
# ---------------------------------------------------------------------------


def _run_router(state_dict: dict) -> dict:
    """
    Invoke decide_rewrite_route.py via subprocess (mirrors orchestrator invocation).

    Returns the RouteResult dict from stdout.
    """
    proc = subprocess.run(
        [sys.executable, str(_ROUTER_SCRIPT)],
        input=json.dumps(state_dict),
        capture_output=True,
        text=True,
    )
    if proc.returncode == 2:
        print(
            f"ERROR: decide_rewrite_route.py rejected input (schema violation).\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(2)
    if proc.returncode != 0:
        print(
            f"ERROR: decide_rewrite_route.py exited with unexpected code "
            f"{proc.returncode}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(
            f"ERROR: decide_rewrite_route.py stdout is not valid JSON: {e}\n"
            f"stdout: {proc.stdout[:500]}",
            file=sys.stderr,
        )
        sys.exit(3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite Loop Router Wrapper — orchestrates checker + router in one call."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--issue", "-i", type=int, help="GitHub Issue 番号"
    )
    source_group.add_argument(
        "--file", "-f", help="Issue body ファイルパス（テスト用）"
    )
    parser.add_argument(
        "--repo", "-r",
        help="GitHub repo (owner/repo)。--issue と共に使用",
    )
    parser.add_argument(
        "--state-path",
        required=True,
        help="router state JSON の永続化パス",
    )
    parser.add_argument(
        "--max-rewrite-attempts",
        type=int,
        required=True,
        help="最大 rewrite 試行回数",
    )
    args = parser.parse_args(argv)

    if args.issue and not args.repo:
        print("ERROR: --issue には --repo が必要です", file=sys.stderr)
        sys.exit(2)

    if args.max_rewrite_attempts < 1:
        print(
            f"ERROR: --max-rewrite-attempts must be >= 1, got {args.max_rewrite_attempts}",
            file=sys.stderr,
        )
        sys.exit(2)

    # Step 1: Fetch / load issue body
    if args.file:
        body = _load_body_from_file(args.file)
    else:
        body = _fetch_issue_body_from_github(args.issue, args.repo)

    # Step 2: Compute body hash
    checked_body_sha256 = _sha256_of_body(body)

    # Step 3: Load persisted router state (replay-safe; fail-closed on corruption)
    try:
        previous_state = load_rewrite_router_state(
            args.state_path,
            current_source_body_sha256=checked_body_sha256,
        )
    except RewriteRouterStateError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(3)

    source_body_reset = (
        previous_state is not None and previous_state.source_body_reset
    )

    # Step 4: Increment attempt counter
    if previous_state is None:
        rewrite_attempt_count = 0
    else:
        # If source body was reset, attempt count was already reset to 0 in the
        # loaded state; we still increment for this invocation.
        rewrite_attempt_count = previous_state.rewrite_attempt_count + 1

    # Step 5: Run checker (stdout JSON only; exit 0/1 are both routed normally)
    # In --file mode, pass original file path so checker's load_fixture_file
    # can read fixture front-matter (LABELS / TITLE) for issue_kind detection.
    checker_exit_code, checker_json = _run_checker(
        body,
        source_file_path=args.file if args.file else None,
    )

    # Step 6: Build schema-valid state dict (allowlist enforcement)
    state_dict = _build_state_dict(
        rewrite_attempt_count=rewrite_attempt_count,
        max_rewrite_attempts=args.max_rewrite_attempts,
        checker_exit_code=checker_exit_code,
        checked_body_sha256=checked_body_sha256,
        checker_json=checker_json,
        previous_state=previous_state,
        source_issue_body_sha256=checked_body_sha256,
        source_body_reset=source_body_reset,
    )

    # Step 7: Validate state dict against schema
    valid, error_msg = validate_state_dict(state_dict)
    if not valid:
        print(f"ERROR: Built state dict is schema-invalid: {error_msg}", file=sys.stderr)
        sys.exit(2)

    # Step 8: Invoke router
    route_result = _run_router(state_dict)

    # Step 9: Persist state (atomic write; attempt counter survives session restart)
    state_obj = LOOP_REWRITE_ROUTER_STATE_V1(
        rewrite_attempt_count=state_dict["rewrite_attempt_count"],
        max_rewrite_attempts=state_dict["max_rewrite_attempts"],
        checker_exit_code=state_dict["checker_exit_code"],
        checked_body_sha256=state_dict["checked_body_sha256"],
        fix_category=state_dict["fix_category"],
        rewrite_history=state_dict["rewrite_history"],
        occurrence_count=state_dict["occurrence_count"],
        missing_sections=state_dict["missing_sections"],
        missing_contract_keys=state_dict["missing_contract_keys"],
        previous_checked_body_sha256=state_dict["previous_checked_body_sha256"],
        previous_missing_sections=state_dict["previous_missing_sections"],
        previous_missing_contract_keys=state_dict["previous_missing_contract_keys"],
        source_issue_body_sha256=state_dict["source_issue_body_sha256"],
        replay_safe=True,
        source_body_reset=state_dict["source_body_reset"],
    )
    save_rewrite_router_state(state_obj, args.state_path)

    # Step 10: Emit RouteResult JSON to stdout
    print(json.dumps(route_result, ensure_ascii=False, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    main()
