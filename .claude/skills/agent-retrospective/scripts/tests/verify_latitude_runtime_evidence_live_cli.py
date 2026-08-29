#!/usr/bin/env python3
"""verify_latitude_runtime_evidence_live_cli.py -- opt-in AC6 runtime verification for the
Latitude CLI collector (Issue #2375).

Invoked directly via ``uv run --locked pytest
.claude/skills/agent-retrospective/scripts/tests/verify_latitude_runtime_evidence_live_cli.py``
(no shell wrapper -- unlike the older `verify_*_live_cli.sh` convention in this same
``tests/`` directory, this Issue's ``## Runtime Verification Applicability`` / ``## CLI
Boundary`` sections specify the SKIP decision happens INSIDE pytest via ``pytest.skip()``,
stdout prefixed ``SKIP:``).

Contract (Issue #2375 Runtime Verification Applicability):
  - Latitude CLI executable not found in PATH -> SKIP (never FAIL).
  - Latitude CLI found but auth/network unavailable (collector classifies the real CLI's
    non-zero exit/timeout as `auth_failed`/`network_unavailable`/`timeout`/`cli_not_found`)
    -> SKIP (never FAIL) -- Latitude being genuinely unreachable in this environment is not a
    bug in this collector.
  - Latitude CLI found, ran, but produced an unexpected/unclassifiable failure (`malformed_output`,
    `budget_exceeded`, or the collector raising instead of returning a typed result) -> FAIL
    (assertion) -- this is a real defect, not an environment-availability gap.
  - Latitude CLI found, ran, and returned `availability: available` -> the result MUST be
    `latitude_runtime_evidence/v1`-schema-valid and the produced artifact MUST be public-safe
    (no raw trace/credential content) -> PASS.

A public-safe artifact (never raw trace/credential content -- only the already public-safe
`latitude_runtime_evidence/v1` dict plus a `status` field) is written under ``artifacts/``
(repo-root-relative, created if absent) in every case, including SKIP, per this Issue's
``artifact_requirements``.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_SCRIPTS_DIR))

import collect_snapshot as cs  # noqa: E402
import validate_retrospective_schema as vrs  # noqa: E402

_ARTIFACTS_DIR = _REPO_ROOT / "artifacts" / "agent-retrospective-latitude-runtime-evidence"

#: reason_codes that indicate genuine environment unavailability (CLI/auth/network), never a
#: collector defect -- these SKIP rather than FAIL (Issue #2375 skip_conditions).
_ENVIRONMENT_UNAVAILABLE_REASON_CODES = frozenset(
    {"cli_not_found", "auth_failed", "network_unavailable", "timeout"}
)

#: reason_codes that indicate a real, unexpected collector/CLI-response defect once the CLI
#: actually ran -- these FAIL (never silently treated as SKIP/PASS).
_UNEXPECTED_FAILURE_REASON_CODES = frozenset({"malformed_output", "budget_exceeded"})


def _write_artifact(name: str, payload: dict[str, Any]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / name
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_latitude_cli_runtime_evidence_live():
    latitude_bin = shutil.which("latitude")
    if latitude_bin is None:
        artifact_path = _write_artifact(
            "verify_latitude_runtime_evidence_live_cli.result.json",
            {
                "status": "skip",
                "generated_at": _now_iso(),
                "skip_reason": "latitude_cli_not_found_in_PATH",
                "evidence": None,
            },
        )
        pytest.skip(f"SKIP: latitude CLI executable not found in PATH (artifact: {artifact_path})")

    result = cs.collect_latitude_runtime_evidence()

    if result["availability"] != "available" and result["reason_code"] in _ENVIRONMENT_UNAVAILABLE_REASON_CODES:
        artifact_path = _write_artifact(
            "verify_latitude_runtime_evidence_live_cli.result.json",
            {
                "status": "skip",
                "generated_at": _now_iso(),
                "skip_reason": result["reason_code"],
                "evidence": result,
            },
        )
        pytest.skip(
            f"SKIP: latitude CLI unavailable (reason_code={result['reason_code']!r}, "
            f"artifact: {artifact_path})"
        )

    if result["availability"] != "available":
        artifact_path = _write_artifact(
            "verify_latitude_runtime_evidence_live_cli.result.json",
            {
                "status": "fail",
                "generated_at": _now_iso(),
                "fail_reason": result["reason_code"],
                "evidence": result,
            },
        )
        assert result["reason_code"] not in _UNEXPECTED_FAILURE_REASON_CODES, (
            f"latitude CLI produced an unexpected, unclassifiable failure "
            f"(reason_code={result['reason_code']!r}); see artifact: {artifact_path}"
        )
        raise AssertionError(
            f"latitude CLI runtime verification failed with unrecognized availability="
            f"{result['availability']!r} reason_code={result['reason_code']!r}; see artifact: "
            f"{artifact_path}"
        )

    # availability == "available": must be schema-valid and public-safe.
    vrs.validate_latitude_runtime_evidence(result)
    serialized = json.dumps(result)
    for forbidden_substring in ("stdout", "stderr", "traceback", "Authorization:", "Bearer "):
        assert forbidden_substring not in serialized

    artifact_path = _write_artifact(
        "verify_latitude_runtime_evidence_live_cli.result.json",
        {
            "status": "pass",
            "generated_at": _now_iso(),
            "evidence": result,
        },
    )
    print(f"PASS: latitude CLI runtime evidence verification succeeded (artifact: {artifact_path})")
