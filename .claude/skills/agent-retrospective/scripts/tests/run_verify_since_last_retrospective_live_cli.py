#!/usr/bin/env python3
"""Run Issue #2714 AC8's one existing live-verifier pytest node safely.

This minimal runner never changes the read/execute-only verifier or its result
artifact. It only observes that artifact before and after the verifier run,
validates public-safe outcome predicates, and writes redacted diagnostics.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
_TEST_FILE = ".claude/skills/agent-retrospective/scripts/tests/verify_since_last_retrospective_live_cli.py"
_TARGET_NODE = f"{_TEST_FILE}::test_since_last_retrospective_claude_code_collector_live"
_ARTIFACT_RELATIVE_PATH = (
    "artifacts/agent-retrospective-since-last-retrospective/"
    "verify_since_last_retrospective_live_cli.result.json"
)
_FALLBACK_KEY_RE = re.compile(r"^_.*_fallback$")
_SUMMARY_RE = re.compile(r"\b(\d+)\s+(passed|skipped|failed|error(?:s)?)\b")


def _artifact_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False, "mtime_ns": None, "sha256": None}
    try:
        payload = path.read_bytes()
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return {"exists": False, "mtime_ns": None, "sha256": None}
    return {
        "exists": True,
        "mtime_ns": mtime_ns,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _is_fresh(pre: dict[str, Any], post: dict[str, Any], launch_started_at_ns: int) -> bool:
    if not post["exists"] or post["mtime_ns"] is None:
        return False
    if post["mtime_ns"] < launch_started_at_ns:
        return False
    return not pre["exists"] or (pre["mtime_ns"] is not None and post["mtime_ns"] > pre["mtime_ns"])


def _has_true_fallback(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (_FALLBACK_KEY_RE.match(str(key)) is not None and child is True) or _has_true_fallback(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_true_fallback(child) for child in value)
    return False


def _load_fresh_artifact(
    path: Path, pre: dict[str, Any], launch_started_at_ns: int
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    post = _artifact_state(path)
    if not _is_fresh(pre, post, launch_started_at_ns):
        return None, post
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, post
    return (payload if isinstance(payload, dict) else None), post


def _summary_counts(output: str) -> dict[str, int] | None:
    counts: dict[str, int] = {}
    for amount, label in _SUMMARY_RE.findall(output):
        if label in counts:
            return None
        counts[label] = int(amount)
    return counts or None


def _skip_observed(returncode: int, counts: dict[str, int] | None) -> bool:
    return returncode == 0 and counts == {"skipped": 1}


def _pass_observed(returncode: int, counts: dict[str, int] | None) -> bool:
    return returncode == 0 and counts == {"passed": 1}


def _valid_skip(payload: dict[str, Any]) -> bool:
    return (
        payload.get("status") == "skip"
        and isinstance(payload.get("generated_at"), str)
        and bool(payload["generated_at"])
        and isinstance(payload.get("skip_reason"), str)
        and bool(payload["skip_reason"])
    )


def _valid_pass(payload: dict[str, Any]) -> bool:
    count = payload.get("real_session_file_count")
    result = payload.get("result")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or not isinstance(result, dict):
        return False
    coverage = result.get("source_coverage")
    entry = coverage.get("claude_code") if isinstance(coverage, dict) else None
    coverage_status = entry.get("status") if isinstance(entry, dict) else None
    completeness = result.get("analysis_completeness")
    if _has_true_fallback(payload):
        return False
    if payload.get("status") == "pass_empty_directory":
        return coverage_status in {"required", "unavailable"} and completeness == "unavailable"
    if payload.get("status") == "pass_real_sessions_observed":
        return count >= 1 and coverage_status in {"observed", "partial"} and completeness in {"complete", "degraded"}
    return False


def _write_log(pre: dict[str, Any], post: dict[str, Any], verdict: str, pytest_returncode: int) -> None:
    artifacts_dir = _REPO_ROOT / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_path = artifacts_dir / f"runtime-verification-AC8-{timestamp}.log"
    log_path.write_text(
        json.dumps(
            {
                "target_artifact": _ARTIFACT_RELATIVE_PATH,
                "pre": pre,
                "post": post,
                "pytest_returncode": pytest_returncode,
                "verdict": verdict,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    artifact_path = _REPO_ROOT / _ARTIFACT_RELATIVE_PATH
    pre = _artifact_state(artifact_path)
    launch_started_at_ns = time.time_ns()
    completed = subprocess.run(
        ["uv", "run", "--locked", "pytest", _TARGET_NODE, "-q", "-r", "s"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    counts = _summary_counts(completed.stdout + completed.stderr)
    payload, post = _load_fresh_artifact(artifact_path, pre, launch_started_at_ns)

    if _skip_observed(completed.returncode, counts) and payload is not None and _valid_skip(payload):
        _write_log(pre, post, "skip", completed.returncode)
        print("SKIP: AC8 target runtime verifier skipped")
        return 77
    if not _pass_observed(completed.returncode, counts) or payload is None or not _valid_pass(payload):
        _write_log(pre, post, "fail", completed.returncode)
        print("FAIL: AC8 runtime verifier did not satisfy its public-safe contract")
        return 1

    _write_log(pre, post, "pass", completed.returncode)
    print("PASS: AC8 target runtime verifier satisfied its public-safe contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
