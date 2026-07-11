#!/usr/bin/env python3
"""
test_session_manifest_legacy_state_async_e2e.py (Issue #1430 AC14)

PR #1440 review (P1-2) observed that in the real PostToolUse hook flow, a
mutating tool_input causes session_manifest_debounce.mjs to spawn a detached
`--worker` child with `stdio: 'ignore'`:

    spawn(process.execPath, [SCRIPT_PATH, '--worker'], {
      detached: true,
      stdio: 'ignore',
      env: process.env,
    })

Whatever that detached child (and any producer subprocess it later spawns
via spawnSync) writes to its own stdout/stderr is discarded -- it is never
observed by the coordinator, by CI, or by an operator. Producer-layout
legacy-state diagnostics (`producer_lock_tmp` / `producer_root_manifest`),
if only ever emitted from inside that detached child, would therefore be
operationally invisible.

AC14 requires that the front gate (session_manifest_debounce.mjs) itself --
on its SYNCHRONOUS invocation, whose stdout/stderr IS captured by the
PostToolUse hook runner -- also detects producer-layout legacy residue and
emits a bounded SESSION_MANIFEST_LEGACY_STATE_V1 diagnostic, independent of
whatever happens to the detached, stdio:'ignore' --worker child.

This test exercises the exact production code path (a mutating PostToolUse
`tool_input` reaching `queueEvent()` and the `spawn(..., {detached: true,
stdio: 'ignore'})` call) with producer-layout legacy fixtures pre-seeded,
and asserts the diagnostic is observed on the SYNCHRONOUS parent's own
(captured) stderr.
"""

import hashlib
import json
import os
import subprocess
import time
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
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(project_root / "current-runtime")
    return env


def extract_legacy_diagnostics(stderr: str) -> list[dict]:
    diagnostics = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith(LEGACY_DIAGNOSTIC_PREFIX):
            diagnostics.append(json.loads(stripped[len(LEGACY_DIAGNOSTIC_PREFIX):]))
    return diagnostics


def test_posttooluse_async_path_observes_producer_legacy_state(tmp_path: Path):
    """
    Simulate the real PostToolUse hook invocation: a mutating tool_input
    (Write) causes the front gate to queue the event and spawn a detached,
    stdio:'ignore' `--worker` child (the same call the production hook makes
    for every mutating PostToolUse event).

    Producer-layout legacy fixtures (artifacts/.lock-<hex>,
    artifacts/private-agent-session-manifest-*.json) are pre-seeded directly
    under CLAUDE_PROJECT_DIR/artifacts (the legacy, pre-#1426 root layout).

    Assertions only depend on the SYNCHRONOUS parent invocation's own
    (captured) stderr, which resolves and returns before this process exits
    -- independent of whether the detached child it spawns ever completes.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True)

    lock_key = hashlib.sha256(b"ac14-producer-lock-fixture").hexdigest()[:32]
    lock_name = f".lock-{lock_key}"
    (artifacts_dir / lock_name).write_text("", encoding="utf-8")

    manifest_key = hashlib.sha256(b"ac14-producer-manifest-fixture").hexdigest()[:32]
    manifest_name = f"private-agent-session-manifest-stop-{int(time.time() * 1000)}-{manifest_key}.json"
    (artifacts_dir / manifest_name).write_text("{}", encoding="utf-8")

    env = make_env(tmp_path)

    # Mirrors the real PostToolUse payload shape for a mutating tool call.
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "session_id": "ac14-async-e2e",
        "tool_input": {"file_path": str(tmp_path / "scratch.md")},
    }

    result = subprocess.run(
        ["node", str(DEBOUNCE_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, f"front gate fail-closed (non-fail-close violated): {result.stderr}"
    assert result.stdout == "", "front gate must remain silent on stdout"

    diagnostics = extract_legacy_diagnostics(result.stderr)

    lock_diag = [d for d in diagnostics if d.get("legacy_kind") == "producer_lock_tmp"]
    assert lock_diag, (
        f"front gate did not observe producer_lock_tmp legacy state on its own "
        f"synchronous (captured) stderr -- this would be unobservable behind "
        f"the detached stdio:'ignore' --worker child in production (AC14 "
        f"violated): {result.stderr!r}"
    )
    assert any(lock_key in p for p in lock_diag[0]["paths"])
    assert "total_count" in lock_diag[0]
    assert "truncated" in lock_diag[0]

    manifest_diag = [d for d in diagnostics if d.get("legacy_kind") == "producer_root_manifest"]
    assert manifest_diag, (
        f"front gate did not observe producer_root_manifest legacy state on "
        f"its own synchronous stderr (AC14 violated): {result.stderr!r}"
    )
    assert any(manifest_key in p for p in manifest_diag[0]["paths"])
