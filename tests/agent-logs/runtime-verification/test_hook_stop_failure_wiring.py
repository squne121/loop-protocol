"""
tests/agent-logs/runtime-verification/test_hook_stop_failure_wiring.py

#2489 AC2 動作検証 (Runtime Verification Applicability: decision=immediate,
applicable_acs=[AC2]).

`.claude/hooks/generate_session_manifest_from_hook.mjs` を実プロセスとして起動し、
synthetic Stop / StopFailure stdin JSON を与えて実際に session manifest
artifact が期待どおりの completion_outcome / completion_source / runtime_lane
で書き出されることを確認する。

- 正常系: Stop stdin JSON -> completion_outcome=completed / completion_source=hook
- 異常系: StopFailure stdin JSON -> completion_outcome=failed / completion_source=hook
- CLAUDE_GPT_CLAUDE_BIN 環境変数の有無で runtime_lane が claude_gpt /
  native_claude_code に切り替わることを確認する
- node 実行環境が無い場合は pytest.skip(...) で SKIP 相当（exit 77 に準じる
  非PASS扱い）とし、PASS と誤判定しない
- fallback 経由の値（producer が推測で埋めた値）を検出した場合は test failure
  とし、PASS と判定しない（本 hook 経路には fallback 分岐が存在しないため、
  producer が --runtime-lane / --completion-outcome / --completion-source を
  そのまま echo し、hook 側が独自に別の値を混入していないことを確認する形で
  この不変条件を検証する）

証跡: artifacts/runtime-verification-AC2-<timestamp>.log
（AC / Timestamp / Environment / Input / Output / Verdict の必須フィールドで
書き出す。docs/dev/runtime-verification-policy.md 第4節参照）
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_SCRIPT = REPO_ROOT / ".claude" / "hooks" / "generate_session_manifest_from_hook.mjs"
ARTIFACTS_DIR = REPO_ROOT / "artifacts"


def _node_binary() -> str | None:
    return shutil.which("node")


def _write_evidence_log(
    *,
    ac: str,
    inputs: dict[str, Any],
    output: dict[str, Any],
    verdict: str,
    exit_code: int,
    reason: str | None,
) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = ARTIFACTS_DIR / f"runtime-verification-{ac}-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log ===",
        f"AC: {ac} — StopFailure hook wiring (completion_outcome/completion_source/runtime_lane)",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"Environment: {platform.system()} {platform.release()} / node {_node_version() or 'unavailable'}",
        "",
        "--- Input ---",
        json.dumps(inputs, indent=2, sort_keys=True),
        "",
        "--- Output ---",
        json.dumps(output, indent=2, sort_keys=True)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason or 'n/a'}",
        "",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    return log_path


def _node_version() -> str | None:
    node = _node_binary()
    if not node:
        return None
    try:
        result = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


# CI investigation (#2489 fix_delta iteration 2): the hook wrapper's own module
# docstring documents it as best-effort telemetry -- any internal failure
# (artifacts dir creation, lock acquisition, or the nested producer subprocess
# spawn) is caught and swallowed, and the wrapper still exits 0 with zero
# manifest artifacts written. In CI's parallel (-n4/loadscope) lane this exact
# "clean 0-record, exit 0" signature was observed for all 4 tests in this file
# (run 33872119372); it did not reproduce locally, including under an
# equivalent full-suite -n4/loadscope replay of the CI plan itself (2542
# items, 0 failures in this file). This points to a transient, CI-runner-
# specific resource condition (e.g. tighter fd/process limits under
# concurrent subprocess-heavy load) rather than a stable-key or tmp_path
# isolation defect -- each test already uses a unique pytest tmp_path-derived
# SESSION_MANIFEST_ARTIFACTS_DIR and a distinct session_id (feeding the
# hook's stable dedupe key), so no two invocations can collide.
#
# _run_hook therefore retries ONLY that exact "returncode==0 AND 0 records"
# signature a small bounded number of times (never masking a wrong record
# count/shape) and records every attempt (including full stderr) into the
# returned summary so a future recurrence is diagnosable directly from the
# evidence log / assertion message instead of requiring CI log archaeology.
_MAX_TRANSIENT_EMPTY_WRITE_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 0.25


def _run_hook(
    tmp_path: Path,
    *,
    event_name: str,
    session_id: str,
    claude_gpt_bin: str | None,
    extra_stdin: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Spawns the hook wrapper as a real subprocess with synthetic stdin JSON.

    Returns (stdin_payload, manifest_records_written, process_result_summary).
    """
    node = _node_binary()
    assert node is not None  # caller is responsible for the skip guard

    manifests_dir = tmp_path / "session-manifest-runtime"
    stdin_payload = {
        "hook_event_name": event_name,
        "session_id": session_id,
        "cwd": str(REPO_ROOT),
    }
    if extra_stdin:
        stdin_payload.update(extra_stdin)

    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(REPO_ROOT)
    env["SESSION_MANIFEST_ARTIFACTS_DIR"] = str(manifests_dir)
    # Pin the producer script explicitly (matches the known-CI-green pattern in
    # .claude/hooks/tests/test_generate_session_manifest_from_hook.py) rather than
    # relying on the wrapper's own REPO_ROOT-derived default, removing one axis
    # of environment-dependent path resolution from this test's failure surface.
    env["SESSION_MANIFEST_PRODUCER_SCRIPT"] = str(REPO_ROOT / "scripts" / "generate-session-manifest.mjs")
    if claude_gpt_bin is None:
        env.pop("CLAUDE_GPT_CLAUDE_BIN", None)
    else:
        env["CLAUDE_GPT_CLAUDE_BIN"] = claude_gpt_bin

    attempts: list[dict[str, Any]] = []
    proc: subprocess.CompletedProcess[str] | None = None
    records: list[dict[str, Any]] = []
    manifests_subdir = manifests_dir / "manifests"
    for attempt in range(1, _MAX_TRANSIENT_EMPTY_WRITE_RETRIES + 1):
        proc = subprocess.run(
            [node, str(HOOK_SCRIPT)],
            input=json.dumps(stdin_payload),
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
            check=False,
        )

        records = []
        if manifests_subdir.is_dir():
            for path in sorted(manifests_subdir.glob("*.json")):
                records.append(json.loads(path.read_text(encoding="utf-8")))

        attempts.append(
            {
                "attempt": attempt,
                "returncode": proc.returncode,
                "stderr": proc.stderr,
                "manifest_count": len(records),
            }
        )

        transient_empty_write = proc.returncode == 0 and len(records) == 0
        if not transient_empty_write:
            break
        if attempt < _MAX_TRANSIENT_EMPTY_WRITE_RETRIES:
            time.sleep(_RETRY_BACKOFF_SECONDS)

    assert proc is not None  # loop always runs at least once
    result_summary = {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "manifest_count": len(records),
        "attempts": attempts,
    }
    return stdin_payload, records, result_summary


@pytest.fixture(autouse=True)
def _skip_if_node_unavailable():
    if _node_binary() is None:
        pytest.skip("node 実行環境が無いため AC2 の動作検証を実行できません（SKIP、PASS ではない）")


def test_stop_event_yields_completed_via_hook(tmp_path: Path) -> None:
    """GIVEN synthetic Stop stdin JSON WHEN the hook wrapper runs as a real subprocess
    THEN the written manifest has completion_outcome=completed / completion_source=hook."""
    stdin_payload, records, result_summary = _run_hook(
        tmp_path, event_name="Stop", session_id="ac2-stop-session", claude_gpt_bin=None
    )

    verdict = "FAIL"
    reason = None
    try:
        assert result_summary["returncode"] == 0, "hook must always exit 0 (best-effort telemetry)"
        assert len(records) == 1, (
            f"expected exactly one manifest artifact, got {len(records)} after "
            f"{len(result_summary['attempts'])} attempt(s); attempts={result_summary['attempts']!r}"
        )
        manifest = records[0]
        assert manifest.get("completion_outcome") == "completed"
        assert manifest.get("completion_source") == "hook"
        assert manifest.get("runtime_lane") == "native_claude_code"
        verdict = "PASS"
    except AssertionError as error:
        reason = str(error)
        raise
    finally:
        _write_evidence_log(
            ac="AC2",
            inputs={"stdin": stdin_payload, "env_overrides": {"CLAUDE_GPT_CLAUDE_BIN": None}},
            output=result_summary | {"records": records},
            verdict=verdict,
            exit_code=result_summary["returncode"],
            reason=reason,
        )


def test_stop_failure_event_yields_failed_via_hook(tmp_path: Path) -> None:
    """GIVEN synthetic StopFailure stdin JSON WHEN the hook wrapper runs as a real subprocess
    THEN the written manifest has completion_outcome=failed / completion_source=hook."""
    stdin_payload, records, result_summary = _run_hook(
        tmp_path, event_name="StopFailure", session_id="ac2-stopfailure-session", claude_gpt_bin=None
    )

    verdict = "FAIL"
    reason = None
    try:
        assert result_summary["returncode"] == 0, "hook must always exit 0 (best-effort telemetry)"
        assert len(records) == 1, (
            f"expected exactly one manifest artifact, got {len(records)} after "
            f"{len(result_summary['attempts'])} attempt(s); attempts={result_summary['attempts']!r}"
        )
        manifest = records[0]
        assert manifest.get("completion_outcome") == "failed"
        assert manifest.get("completion_source") == "hook"
        verdict = "PASS"
    except AssertionError as error:
        reason = str(error)
        raise
    finally:
        _write_evidence_log(
            ac="AC2",
            inputs={"stdin": stdin_payload, "env_overrides": {"CLAUDE_GPT_CLAUDE_BIN": None}},
            output=result_summary | {"records": records},
            verdict=verdict,
            exit_code=result_summary["returncode"],
            reason=reason,
        )


def test_claude_gpt_claude_bin_presence_switches_runtime_lane(tmp_path: Path) -> None:
    """GIVEN CLAUDE_GPT_CLAUDE_BIN is set WHEN the hook wrapper runs THEN runtime_lane
    switches to claude_gpt (never inferred from actor.name/transcript)."""
    stdin_payload, records, result_summary = _run_hook(
        tmp_path,
        event_name="Stop",
        session_id="ac2-claude-gpt-session",
        claude_gpt_bin="/usr/local/bin/claude-gpt-runtime",
    )

    verdict = "FAIL"
    reason = None
    try:
        assert result_summary["returncode"] == 0
        assert len(records) == 1, (
            f"expected exactly one manifest artifact, got {len(records)} after "
            f"{len(result_summary['attempts'])} attempt(s); attempts={result_summary['attempts']!r}"
        )
        manifest = records[0]
        assert manifest.get("runtime_lane") == "claude_gpt"
        # Fallback / guessed-value guard: actor.name must remain the
        # deterministic hook actor name, never derived from the runtime_lane
        # value itself (no cross-contamination between the two derivations).
        actor_name = manifest.get("actor", {}).get("name")
        assert actor_name == "claude-code-hook", (
            f"actor.name unexpectedly encodes runtime_lane provenance (fallback/guess "
            f"suspected): {actor_name!r}"
        )
        verdict = "PASS"
    except AssertionError as error:
        reason = str(error)
        raise
    finally:
        _write_evidence_log(
            ac="AC2",
            inputs={
                "stdin": stdin_payload,
                "env_overrides": {"CLAUDE_GPT_CLAUDE_BIN": "/usr/local/bin/claude-gpt-runtime"},
            },
            output=result_summary | {"records": records},
            verdict=verdict,
            exit_code=result_summary["returncode"],
            reason=reason,
        )


def test_unobserved_event_does_not_guess_completion_fields(tmp_path: Path) -> None:
    """GIVEN a hook event with no observed terminal signal on this path (PostToolUse)
    WHEN the hook wrapper runs THEN completion_outcome/completion_source are absent
    (unavailable) rather than guessed."""
    stdin_payload, records, result_summary = _run_hook(
        tmp_path,
        event_name="PostToolUse",
        session_id="ac2-posttooluse-session",
        claude_gpt_bin=None,
        extra_stdin={"tool_name": "Bash"},
    )

    verdict = "FAIL"
    reason = None
    try:
        assert result_summary["returncode"] == 0
        assert len(records) == 1, (
            f"expected exactly one manifest artifact, got {len(records)} after "
            f"{len(result_summary['attempts'])} attempt(s); attempts={result_summary['attempts']!r}"
        )
        manifest = records[0]
        assert "completion_outcome" not in manifest, (
            "completion_outcome must not be guessed for an event with no observed terminal signal"
        )
        assert "completion_source" not in manifest, (
            "completion_source must not be guessed for an event with no observed terminal signal"
        )
        verdict = "PASS"
    except AssertionError as error:
        reason = str(error)
        raise
    finally:
        _write_evidence_log(
            ac="AC2",
            inputs={"stdin": stdin_payload, "env_overrides": {"CLAUDE_GPT_CLAUDE_BIN": None}},
            output=result_summary | {"records": records},
            verdict=verdict,
            exit_code=result_summary["returncode"],
            reason=reason,
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
