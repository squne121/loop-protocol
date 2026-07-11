#!/usr/bin/env python3
"""
Legacy-state diagnostic tests for session_manifest_debounce.mjs (Issue #1430).

Verifies that startup detection of pre-#1426-cutover legacy runtime state
(the old `artifacts/session-manifest-debounce/{events,worker.lock}` layout)
emits a fixed-schema `SESSION_MANIFEST_LEGACY_STATE_V1=<JSON>` diagnostic on
stderr without fail-closing the hook (exit code stays 0 -- non-fail-close),
and that no diagnostic is emitted when no legacy state is present.

Isolation: each test points CLAUDE_PROJECT_DIR at a synthetic tmp "repo
root" so legacy-path detection (which is intentionally independent of the
current-layout SESSION_MANIFEST_DEBOUNCE_DIR override -- Issue #1430
contract) never touches this checkout's own artifacts/ directory.

AC coverage:
  AC1  legacy events dir present -> legacy_kind=debounce_events_dir diagnostic.
  AC2  legacy worker.lock present -> legacy_kind=debounce_worker_lock diagnostic.
  AC5  no legacy state (new subtree only, or artifacts/ absent) -> no diagnostic.
  AC6  the emitted diagnostic line survives session_manifest_coordinator.sh's
       sanitize_stderr() transform (path redaction + first-line 220-char
       truncation) with legacy_kind still recoverable.
  AC8  the diagnostic line is ordered AFTER an existing
       SESSION_MANIFEST_DEBOUNCE_RESULT_V1 result line, so a coordinator-side
       "first line only" truncation never hides the result line.
  AC10 producer failure during a debounce flush does not delete the queued
       event files (they remain pending for a later retry).
  AC13 the same residue does not re-trigger the diagnostic on a second,
       later invocation (duplicate-suppression marker).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

DEBOUNCE_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_debounce.mjs"

LEGACY_DIAGNOSTIC_PREFIX = "SESSION_MANIFEST_LEGACY_STATE_V1="
RESULT_PREFIX = "SESSION_MANIFEST_DEBOUNCE_RESULT_V1="


def make_env(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    # Route current-layout state elsewhere so front-gate bootstrapping never
    # touches the synthetic legacy fixtures under project_root/artifacts.
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(project_root / "current-runtime")
    return env


def run_front_gate_readonly(project_root: Path) -> "subprocess.CompletedProcess[str]":
    """
    Invoke the front gate with a readonly Bash payload so the event is
    classified `readonly_bash` (never queued, never spawns a detached
    worker) -- isolates the legacy-state startup detection from
    queue/flush side effects.
    """
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "cwd": str(project_root),
        "tool_input": {"command": "rg -n legacy-state-probe README.md"},
    }
    return subprocess.run(
        ["node", str(DEBOUNCE_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=make_env(project_root),
        check=False,
        timeout=30,
    )


def extract_legacy_diagnostics(stderr: str) -> list[dict]:
    diagnostics = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(LEGACY_DIAGNOSTIC_PREFIX):
            diagnostics.append(json.loads(stripped[len(LEGACY_DIAGNOSTIC_PREFIX) :]))
    return diagnostics


def sanitize_stderr_like_coordinator(text: str) -> str:
    """
    Replicates session_manifest_coordinator.sh's sanitize_stderr() transform:
    POSIX/Windows/WSL path redaction followed by first-line 220-char
    truncation (AC6).
    """
    redacted = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<path>", text)
    redacted = re.sub(r"/mnt/[A-Za-z]/[^\s\"']+", "<path>", redacted)
    redacted = re.sub(r"/[^\s\"']+", "<path>", redacted)
    lines = [line.strip() for line in redacted.splitlines() if line.strip()]
    return lines[0][:220] if lines else ""


# ---------------------------------------------------------------------------
# AC1: legacy events directory
# ---------------------------------------------------------------------------


def test_legacy_events_diagnostic(tmp_path: Path):
    legacy_events_dir = tmp_path / "artifacts" / "session-manifest-debounce" / "events"
    legacy_events_dir.mkdir(parents=True)

    result = run_front_gate_readonly(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "debounce_events_dir"]
    assert matching, f"expected debounce_events_dir diagnostic, got: {diagnostics}"
    diagnostic = matching[0]
    assert diagnostic["status"] == "legacy_state_detected"
    assert isinstance(diagnostic["paths"], list) and diagnostic["paths"]
    assert "detected_at" in diagnostic and diagnostic["detected_at"]


# ---------------------------------------------------------------------------
# AC2: legacy worker.lock
# ---------------------------------------------------------------------------


def test_legacy_worker_lock_diagnostic(tmp_path: Path):
    legacy_state_dir = tmp_path / "artifacts" / "session-manifest-debounce"
    legacy_state_dir.mkdir(parents=True)
    (legacy_state_dir / "worker.lock").write_text("{}", encoding="utf-8")

    result = run_front_gate_readonly(tmp_path)

    assert result.returncode == 0, f"hook fail-closed (non-fail-close violated): {result.stderr}"
    diagnostics = extract_legacy_diagnostics(result.stderr)
    matching = [d for d in diagnostics if d.get("legacy_kind") == "debounce_worker_lock"]
    assert matching, f"expected debounce_worker_lock diagnostic, got: {diagnostics}"
    diagnostic = matching[0]
    assert diagnostic["status"] == "legacy_state_detected"
    assert isinstance(diagnostic["paths"], list) and diagnostic["paths"]


# ---------------------------------------------------------------------------
# AC5: clean state -> no diagnostic
# ---------------------------------------------------------------------------


def test_no_legacy_state_no_diagnostic(tmp_path: Path):
    # New subtree only -- must not be misclassified as legacy residue.
    (tmp_path / "artifacts" / "session-manifest-runtime" / "events").mkdir(parents=True)

    result = run_front_gate_readonly(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    assert diagnostics == [], f"unexpected legacy diagnostic on clean (new-subtree-only) state: {diagnostics}"


def test_no_legacy_state_no_diagnostic_when_artifacts_dir_absent(tmp_path: Path):
    # artifacts/ itself is not created at all.
    result = run_front_gate_readonly(tmp_path)

    assert result.returncode == 0
    diagnostics = extract_legacy_diagnostics(result.stderr)
    assert diagnostics == [], f"unexpected legacy diagnostic when artifacts/ absent: {diagnostics}"


# ---------------------------------------------------------------------------
# AC6: diagnostic survives coordinator-style truncation/redaction
# ---------------------------------------------------------------------------


def test_legacy_state_survives_coordinator_truncation(tmp_path: Path):
    legacy_events_dir = tmp_path / "artifacts" / "session-manifest-debounce" / "events"
    legacy_events_dir.mkdir(parents=True)

    result = run_front_gate_readonly(tmp_path)
    assert result.returncode == 0

    raw_lines = [
        line.strip()
        for line in result.stderr.splitlines()
        if line.strip().startswith(LEGACY_DIAGNOSTIC_PREFIX)
    ]
    assert raw_lines, f"no legacy diagnostic emitted: {result.stderr}"
    raw_line = raw_lines[0]

    transformed = sanitize_stderr_like_coordinator(raw_line)
    assert len(transformed) <= 220
    assert "debounce_events_dir" in transformed, (
        f"legacy_kind not recoverable after coordinator-style truncation: {transformed!r}"
    )


# ---------------------------------------------------------------------------
# AC8: legacy diagnostic ordered after an existing result line
# ---------------------------------------------------------------------------


def test_legacy_diagnostic_does_not_shadow_existing_result_line(tmp_path: Path):
    """
    When --flush emits an existing SESSION_MANIFEST_DEBOUNCE_RESULT_V1 result
    line (from a successful producer call) AND legacy-layout residue is
    present, the legacy diagnostic must be ordered strictly AFTER the result
    line. A coordinator that only keeps the first non-empty stderr line as
    its `detail=` field must therefore still see the RESULT_V1 line, not the
    legacy diagnostic (PR #1440 review P1-1).
    """
    legacy_events_dir = tmp_path / "artifacts" / "session-manifest-debounce" / "events"
    legacy_events_dir.mkdir(parents=True)

    runtime_dir = tmp_path / "current-runtime"
    events_dir = runtime_dir / "events"
    events_dir.mkdir(parents=True)
    (events_dir / "1-pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "ac8-test",
                "session_manifest_delta": [
                    {"mutation_type": "write", "relative_paths": ["docs/dev/x.md"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)

    env = make_env(tmp_path)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "3000"

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, f"--flush failed: {result.stderr}"

    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    result_indices = [i for i, line in enumerate(lines) if line.startswith(RESULT_PREFIX)]
    legacy_indices = [i for i, line in enumerate(lines) if line.startswith(LEGACY_DIAGNOSTIC_PREFIX)]

    assert result_indices, f"expected an existing result line, got: {lines}"
    assert legacy_indices, f"expected a legacy diagnostic line, got: {lines}"
    assert max(result_indices) < min(legacy_indices), (
        f"legacy diagnostic must not precede the existing result line (AC8): {lines}"
    )

    # Coordinator-style: sanitize_stderr keeps only the FIRST non-empty line
    # as the `detail=` field -- it must be the existing result line.
    first_line = lines[0]
    assert first_line.startswith(RESULT_PREFIX), (
        f"first stderr line must be the existing result line, not the legacy "
        f"diagnostic (AC8 violated): {first_line!r}"
    )


# ---------------------------------------------------------------------------
# AC10: producer failure does not delete queued events
# ---------------------------------------------------------------------------


def test_queued_event_not_deleted_on_producer_failure(tmp_path: Path):
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 1\n", encoding="utf-8")
    producer.chmod(0o755)

    env = make_env(tmp_path)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "3000"

    runtime_dir = tmp_path / "current-runtime"
    events_dir = runtime_dir / "events"
    events_dir.mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    event_file = events_dir / f"{now_ms}-pending.json"
    event_file.write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "ac10-test",
                "session_manifest_delta": [
                    {"mutation_type": "write", "relative_paths": ["docs/dev/y.md"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, f"--flush unexpectedly fail-closed: {result.stderr}"

    remaining = sorted(events_dir.glob("*.json"))
    assert remaining, (
        f"queued event was deleted despite producer failure -- AC10 violated: {result.stderr}"
    )
    assert event_file in remaining


# ---------------------------------------------------------------------------
# AC13: duplicate-residue diagnostic suppressed on repeat invocation
# ---------------------------------------------------------------------------


def test_duplicate_residue_diagnostic_suppressed_on_repeat_invocation(tmp_path: Path):
    legacy_events_dir = tmp_path / "artifacts" / "session-manifest-debounce" / "events"
    legacy_events_dir.mkdir(parents=True)

    first = run_front_gate_readonly(tmp_path)
    assert first.returncode == 0
    first_diagnostics = extract_legacy_diagnostics(first.stderr)
    matching_first = [d for d in first_diagnostics if d.get("legacy_kind") == "debounce_events_dir"]
    assert matching_first, f"expected diagnostic on first invocation: {first.stderr}"

    second = run_front_gate_readonly(tmp_path)
    assert second.returncode == 0
    second_diagnostics = extract_legacy_diagnostics(second.stderr)
    matching_second = [d for d in second_diagnostics if d.get("legacy_kind") == "debounce_events_dir"]
    assert not matching_second, (
        f"expected suppressed diagnostic on repeat invocation with unchanged "
        f"residue (AC13 violated), got: {matching_second}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
