#!/usr/bin/env python3
"""Run Issue #2714 AC8's one existing live-verifier pytest node safely.

This minimal runner never changes the read/execute-only verifier or its result
artifact. It only observes that artifact before and after the verifier run,
validates public-safe outcome predicates, and writes redacted diagnostics.

PR #2737 review fix_delta (blockers 1 and 2):

1. The AC8 target pytest subprocess is launched with a copy of the parent
   environment that has ``AGENT_RETROSPECTIVE_CLAUDE_CODE_SESSIONS_DIR``
   removed, so a test-only override left set in the parent process cannot
   substitute fixture/other-project session history for the live on-disk
   ``$HOME/.claude/projects/<slug>/`` evidence AC8 must observe. No other
   environment variable is added, removed, or normalized.
2. Outcome (PASS / SKIP / FAIL) is no longer derived from human-readable
   pytest terminal output (which display settings such as
   ``PYTEST_ADDOPTS=-q`` or colored output can reshape). The subprocess is
   launched with a temporary ``--junitxml`` report, and outcome is derived
   structurally via ``xml.etree.ElementTree`` from that report: the report
   must exist, parse, and contain exactly one testcase whose name matches
   the AC8 target node -- anything else (missing/malformed/ambiguous) is
   fail-closed. The temporary report file lives under a ``tempfile``
   directory that is cleaned up before this process exits and its path/body
   is never written to the redacted diagnostics log below.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[5]
_TEST_FILE = ".claude/skills/agent-retrospective/scripts/tests/verify_since_last_retrospective_live_cli.py"
_TARGET_TEST_NAME = "test_since_last_retrospective_claude_code_collector_live"
_TARGET_NODE = f"{_TEST_FILE}::{_TARGET_TEST_NAME}"
_ARTIFACT_RELATIVE_PATH = (
    "artifacts/agent-retrospective-since-last-retrospective/"
    "verify_since_last_retrospective_live_cli.result.json"
)
_FALLBACK_KEY_RE = re.compile(r"^_.*_fallback$")
_SESSION_DIR_OVERRIDE_ENV_VAR = "AGENT_RETROSPECTIVE_CLAUDE_CODE_SESSIONS_DIR"


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


def _child_env() -> dict[str, str]:
    """Blocker 1 fix_delta: a copy of the parent environment with the
    test-only claude_code session-dir override removed. Every other
    environment variable (HOME, PATH, auth, PYTEST_ADDOPTS, PY_COLORS, ...)
    is passed through unchanged."""
    child_env = os.environ.copy()
    child_env.pop(_SESSION_DIR_OVERRIDE_ENV_VAR, None)
    return child_env


def _parse_junit_outcome(junit_path: Path, target_test_name: str) -> dict[str, bool] | None:
    """Blocker 2 fix_delta: structural outcome for the single AC8 target
    testcase, derived from a pytest ``--junitxml`` report instead of
    human-readable terminal text. Returns ``None`` (fail-closed) unless the
    report exists, parses, and contains EXACTLY ONE testcase matching
    ``target_test_name``."""
    if not junit_path.is_file():
        return None
    try:
        text = junit_path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        root = ET.fromstring(text)
    except (ET.ParseError, OSError, UnicodeDecodeError):
        return None
    testcases = root.findall(".//testcase")
    if len(testcases) != 1:
        return None
    testcase = testcases[0]
    if testcase.get("name") != target_test_name:
        return None
    return {
        "failed": testcase.find("failure") is not None,
        "errored": testcase.find("error") is not None,
        "skipped": testcase.find("skipped") is not None,
    }


def _skip_observed(returncode: int, outcome: dict[str, bool] | None) -> bool:
    if outcome is None:
        return False
    return returncode == 0 and outcome["skipped"] and not outcome["failed"] and not outcome["errored"]


def _pass_observed(returncode: int, outcome: dict[str, bool] | None) -> bool:
    if outcome is None:
        return False
    return returncode == 0 and not outcome["failed"] and not outcome["errored"] and not outcome["skipped"]


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
    with tempfile.TemporaryDirectory(prefix="ac8-live-verifier-junit-") as tmp_dir:
        junit_path = Path(tmp_dir) / "result.xml"
        completed = subprocess.run(
            ["uv", "run", "--locked", "pytest", _TARGET_NODE, "-q", "-r", "s", f"--junitxml={junit_path}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=_child_env(),
        )
        outcome = _parse_junit_outcome(junit_path, _TARGET_TEST_NAME)
    payload, post = _load_fresh_artifact(artifact_path, pre, launch_started_at_ns)

    if _skip_observed(completed.returncode, outcome) and payload is not None and _valid_skip(payload):
        _write_log(pre, post, "skip", completed.returncode)
        print("SKIP: AC8 target runtime verifier skipped")
        return 77
    if not _pass_observed(completed.returncode, outcome) or payload is None or not _valid_pass(payload):
        _write_log(pre, post, "fail", completed.returncode)
        print("FAIL: AC8 runtime verifier did not satisfy its public-safe contract")
        return 1

    _write_log(pre, post, "pass", completed.returncode)
    print("PASS: AC8 target runtime verifier satisfied its public-safe contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
