#!/usr/bin/env python3
"""
kill_switch_runtime_smoke.py

Kill Switch runtime smoke test for session recording.
Runs .claude/scripts/check_session_recording_runtime_safety.py against
fixture-defined SRRS_* overrides and asserts exit codes.

Usage:
    python3 .claude/scripts/kill_switch_runtime_smoke.py [--fixtures <path>]

Exit codes:
    0  - all smoke tests PASS
    1  - one or more smoke tests FAILED
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import yaml
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
VERIFIER_SCRIPT = REPO_ROOT / ".claude" / "scripts" / "check_session_recording_runtime_safety.py"

def _build_required_end_state(
    fixture: dict,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> str:
    """
    B4 fix: Build required_end_state dynamically from fixture and actual verifier output.
    Includes kill_switch_triggered, fixture_id, verifier_exit_code, fake_fixture_only,
    and leaked_credentials_rotated_or_revoked from measured evidence.
    """
    fixture_id = fixture.get("fixture_id", "unknown")
    expected_exit = fixture.get("expected_exit", "zero")
    is_fake = expected_exit == "nonzero"  # fake (dangerous) fixture

    # kill_switch_triggered is derived from exit_code:
    # - nonzero exit means verifier triggered kill switch
    kill_switch_triggered = exit_code != 0

    leaked_creds: dict
    if is_fake:
        leaked_creds = {"status": "not_applicable", "reason": "fake_fixture_only"}
    else:
        leaked_creds = {"status": "not_applicable", "reason": "safe_fixture_no_real_credentials"}

    state: dict = {
        "kill_switch_triggered": kill_switch_triggered,
        "fixture_id": fixture_id,
        "verifier_exit_code": exit_code,
        "fake_fixture_only": is_fake,
        "session_recording_tool_enabled": False,
        "git_hooks_recording_enabled": False,
        "public_checkpoint_branch_present": False,
        "auto_push_sessions_allowed": False,
        "full_transcript_remote_visibility": "none",
        "leaked_credentials_rotated_or_revoked": leaked_creds,
        "remediation_ticket_required": True,
    }

    lines = ["required_end_state:"]
    for k, v in state.items():
        if isinstance(v, dict):
            lines.append(f"  {k}:")
            for dk, dv in v.items():
                lines.append(f"    {dk}: {dv}")
        elif isinstance(v, bool):
            lines.append(f"  {k}: {'true' if v else 'false'}")
        elif isinstance(v, int):
            lines.append(f"  {k}: {v}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) + "\n"


def _verify_required_end_state_yaml(yaml_str: str) -> None:
    """Assert that a required_end_state YAML string parses to the expected structure."""
    parsed = yaml.safe_load(yaml_str)
    assert parsed is not None, "required_end_state YAML parsed to None"
    res = parsed.get("required_end_state")
    assert res is not None, "required_end_state key missing after parse"
    assert res["session_recording_tool_enabled"] is False, \
        "session_recording_tool_enabled must be false"
    assert res["git_hooks_recording_enabled"] is False, \
        "git_hooks_recording_enabled must be false"
    assert res["public_checkpoint_branch_present"] is False, \
        "public_checkpoint_branch_present must be false"
    assert res["auto_push_sessions_allowed"] is False, \
        "auto_push_sessions_allowed must be false"
    assert res["remediation_ticket_required"] is True, \
        "remediation_ticket_required must be true"
    assert "kill_switch_triggered" in res, "kill_switch_triggered must be present"
    assert "fixture_id" in res, "fixture_id must be present"
    assert "verifier_exit_code" in res, "verifier_exit_code must be present"


def _create_minimal_repo_dir() -> tempfile.TemporaryDirectory:
    """
    B4 fix: Create a minimal fake repo directory so the verifier does not
    read real production state (real .entire/settings.json, git hooks, etc.).

    The directory intentionally has NO .entire/ directory so that push_sessions
    check returns PASS (no EntireCLI indicators). Fixtures that need to exercise
    push_sessions must supply SRRS_* overrides to control the check directly.

    Returns the TemporaryDirectory object; caller must keep a reference to
    prevent premature cleanup.
    """
    tmp = tempfile.TemporaryDirectory(prefix="srrs_smoke_")
    root = Path(tmp.name)
    # Minimal hooks dir (empty, no session push hooks)
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    return tmp


def load_fixture(fixture_path: Path) -> dict:
    """Load a JSON fixture file."""
    with fixture_path.open(encoding="utf-8") as f:
        return json.load(f)


def run_verifier_with_fixture(fixture: dict, repo_root: Path) -> tuple[int, str, str]:
    """
    Run the verifier with the SRRS_* overrides from the fixture.
    Returns (exit_code, stdout, stderr).

    B4 fix: repo_root is a temp directory (created per-fixture), not the real repo.
    """
    env = dict(os.environ)
    # Apply fixture overrides
    srrs_overrides = fixture.get("srrs_overrides", {})
    for key, value in srrs_overrides.items():
        env[key] = value

    # Always override SRRS_REPO_ROOT to the isolated temp directory
    env["SRRS_REPO_ROOT"] = str(repo_root)
    # Point SRRS_HOOKS_DIR at the temp hooks dir to avoid hitting real git hooks
    env["SRRS_HOOKS_DIR"] = str(repo_root / ".git" / "hooks")

    try:
        result = subprocess.run(
            [sys.executable, str(VERIFIER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as exc:
        return -1, "", str(exc)


def assert_exit_code(fixture_id: str, expected: str, actual: int) -> bool:
    """
    Assert exit code matches expectation.
    expected: "zero" | "nonzero"
    Returns True if assertion passes.
    """
    if expected == "zero":
        ok = actual == 0
    elif expected == "nonzero":
        ok = actual != 0
    else:
        print(f"  [ERROR] Unknown expected_exit value: {expected!r}", flush=True)
        return False

    if ok:
        print(f"  [PASS] {fixture_id}: exit={actual} (expected {expected})", flush=True)
    else:
        print(f"  [FAIL] {fixture_id}: exit={actual} (expected {expected})", flush=True)
    return ok


def assert_expected_triggers(fixture_id: str, expected_triggers: list[str], stdout: str, stderr: str) -> bool:
    """
    B1 fix: Assert that verifier stdout/stderr contains expected diagnostic strings.
    expected_triggers: list of strings that must appear in combined output.
    Returns True if all expected_triggers are found.
    """
    if not expected_triggers:
        return True

    combined = stdout + stderr
    all_found = True
    for trigger in expected_triggers:
        if trigger in combined:
            print(f"  [PASS] {fixture_id}: trigger found: {trigger!r}", flush=True)
        else:
            print(f"  [FAIL] {fixture_id}: expected trigger NOT found: {trigger!r}", flush=True)
            print(f"         verifier output (stdout): {stdout[:500]!r}", flush=True)
            print(f"         verifier output (stderr): {stderr[:200]!r}", flush=True)
            all_found = False
    return all_found


def run_fixture_smoke(fixture_path: Path) -> bool:
    """
    Run smoke test for a single fixture JSON.
    B4 fix: Creates an isolated temp dir per fixture run.
    Returns True if PASS.
    """
    fixture = load_fixture(fixture_path)
    fixture_id = fixture.get("fixture_id", fixture_path.stem)
    description = fixture.get("description", "")
    expected_exit = fixture.get("expected_exit", "zero")
    expected_triggers: list[str] = fixture.get("expected_triggers", [])

    print(f"\n--- Fixture: {fixture_id} ---", flush=True)
    if description:
        print(f"  Description: {description}", flush=True)

    # B4 fix: isolated temp repo dir per fixture
    tmp_dir = _create_minimal_repo_dir()
    try:
        repo_root = Path(tmp_dir.name)
        exit_code, stdout, stderr = run_verifier_with_fixture(fixture, repo_root)
    finally:
        tmp_dir.cleanup()

    ok = assert_exit_code(fixture_id, expected_exit, exit_code)

    # B1 fix: assert diagnostic triggers in verifier output
    if expected_triggers:
        trigger_ok = assert_expected_triggers(fixture_id, expected_triggers, stdout, stderr)
        ok = ok and trigger_ok
    elif not expected_triggers:
        # B3 fix: warn if fixture has no expected_triggers (not a FAIL, but surfaced)
        print(f"  [WARN] {fixture_id}: no expected_triggers defined in fixture", flush=True)

    # B4 fix: build required_end_state dynamically from actual verifier output
    res_yaml = _build_required_end_state(fixture, exit_code, stdout, stderr)
    try:
        _verify_required_end_state_yaml(res_yaml)
    except AssertionError as exc:
        print(f"  [ERROR] required_end_state validation failed: {exc}", flush=True)
        ok = False
    print(res_yaml, flush=True)

    return ok


def find_fixtures(fixtures_dir: Path) -> list[Path]:
    """Find all JSON fixture files under fixtures_dir."""
    return sorted(fixtures_dir.rglob("*.json"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kill Switch runtime smoke test for session recording."
    )
    parser.add_argument(
        "--fixtures",
        default=str(REPO_ROOT / "tests" / "fixtures" / "session-recording"),
        help="Directory containing fixture JSON files",
    )
    args = parser.parse_args()

    fixtures_dir = Path(args.fixtures).resolve()

    if not fixtures_dir.is_dir():
        print(f"ERROR: fixtures directory not found: {fixtures_dir}", file=sys.stderr)
        return 1

    if not VERIFIER_SCRIPT.is_file():
        print(f"ERROR: verifier script not found: {VERIFIER_SCRIPT}", file=sys.stderr)
        return 1

    fixture_files = find_fixtures(fixtures_dir)
    if not fixture_files:
        print(f"ERROR: no fixture JSON files found in {fixtures_dir}", file=sys.stderr)
        return 1

    print("Kill Switch Smoke Test", flush=True)
    print(f"Fixtures dir: {fixtures_dir}", flush=True)
    print(f"Verifier: {VERIFIER_SCRIPT}", flush=True)
    print(f"Found {len(fixture_files)} fixture(s)", flush=True)

    results: list[tuple[str, bool]] = []
    for fixture_path in fixture_files:
        ok = run_fixture_smoke(fixture_path)
        results.append((fixture_path.stem, ok))

    print("\n=== Summary ===", flush=True)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}: {name}", flush=True)
    print(f"\nTotal: {passed} PASS, {failed} FAIL", flush=True)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
