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
"""

import json
import os
import re
import subprocess
from pathlib import Path

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
