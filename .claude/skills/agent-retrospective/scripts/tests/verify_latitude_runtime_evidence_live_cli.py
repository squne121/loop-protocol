#!/usr/bin/env python3
"""verify_latitude_runtime_evidence_live_cli.py -- opt-in AC6 runtime verification for the
Latitude CLI collector (Issue #2375; PR #2392 fix_delta -- corrected against the REAL,
locally-verified `latitude` CLI v7.10.0's `traces list` command).

Invoked directly via ``uv run --locked pytest
.claude/skills/agent-retrospective/scripts/tests/verify_latitude_runtime_evidence_live_cli.py``
(no shell wrapper -- unlike the older `verify_*_live_cli.sh` convention in this same
``tests/`` directory, this Issue's ``## Runtime Verification Applicability`` / ``## CLI
Boundary`` sections specify the SKIP decision happens INSIDE pytest via ``pytest.skip()``,
stdout prefixed ``SKIP:``).

Contract (Issue #2375 Runtime Verification Applicability):
  - Latitude CLI executable not found in PATH -> SKIP (never FAIL).
  - `LATITUDE_PROJECT` env var unset/blank -> SKIP (never FAIL) -- the collector requires a real
    project slug and never hardcodes one.
  - No real trace discoverable in the configured project (empty project, or a transient
    discovery-call failure) -> SKIP (never FAIL) -- this opt-in test cannot demonstrate a genuine
    `available` PASS without at least one real trace to correlate against.
  - Latitude CLI found but auth/network unavailable, or a resolvable session has zero matching
    traces (`session_id_unresolved`/`project_slug_unresolved`/`no_matching_trace`, alongside the
    original `auth_failed`/`network_unavailable`/`timeout`/`cli_not_found`) -> SKIP (never FAIL).
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

Session discovery (PR #2392 fix_delta): this standalone verification script is not itself a
retrospective run, so it has no `claude_gpt` hook-sink `complete_sessions` to correlate against
(that mechanism is `run_retrospective._resolve_latitude_target_session_id`'s job, exercised by
`test_run_retrospective.py`, not this file). To exercise the real collector's `available` path
for real, this script makes ONE extra, bounded, read-only, unfiltered
`latitude traces list --project-slug <slug> --limit 1 --format json` discovery call (OUTSIDE the
collector under test, and never persisted) to find a real, currently-existing trace's
`sessionId`, then feeds that real session_id into the actual production collector
(`collect_snapshot.collect_latitude_runtime_evidence`) -- which performs its own single,
session-filtered CLI launch (the Collection Budget's "at most 1 launch" bounds the collector under
test, not this test's own one-time setup probe).
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

#: reason_codes that indicate genuine environment unavailability (CLI/auth/network/no resolvable
#: session or project or matching trace), never a collector defect -- these SKIP rather than FAIL
#: (Issue #2375 skip_conditions; PR #2392 fix_delta adds the 3 session-correlation reason_codes).
_ENVIRONMENT_UNAVAILABLE_REASON_CODES = frozenset(
    {
        "cli_not_found",
        "auth_failed",
        "network_unavailable",
        "timeout",
        "session_id_unresolved",
        "project_slug_unresolved",
        "no_matching_trace",
    }
)

#: reason_codes that indicate a real, unexpected collector/CLI-response defect once the CLI
#: actually ran -- these FAIL (never silently treated as SKIP/PASS).
_UNEXPECTED_FAILURE_REASON_CODES = frozenset({"malformed_output", "budget_exceeded"})

#: bounded, read-only discovery-call budget (this script's own setup probe, distinct from the
#: collector-under-test's Collection Budget) -- mirrors `LATITUDE_TIMEOUT_SECONDS`/
#: `LATITUDE_MAX_OUTPUT_BYTES` so the probe itself never becomes an unbounded/blocking call.
_DISCOVERY_TIMEOUT_SECONDS = cs.LATITUDE_TIMEOUT_SECONDS
_DISCOVERY_MAX_OUTPUT_BYTES = cs.LATITUDE_MAX_OUTPUT_BYTES


def _write_artifact(name: str, payload: dict[str, Any]) -> Path:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _ARTIFACTS_DIR / name
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _skip_with_artifact(reason: str, evidence: dict[str, Any] | None = None) -> None:
    artifact_path = _write_artifact(
        "verify_latitude_runtime_evidence_live_cli.result.json",
        {"status": "skip", "generated_at": _now_iso(), "skip_reason": reason, "evidence": evidence},
    )
    pytest.skip(f"SKIP: {reason} (artifact: {artifact_path})")


def _discover_real_session_id(project_slug: str) -> str | None:
    """Bounded, read-only, unfiltered discovery call: `latitude traces list --project-slug <slug>
    --limit 1 --format json` (the CLI's default sort is `startTime desc`, so this returns the
    single most-recent trace in the project). Returns that trace's `sessionId` (a real,
    currently-existing session_id this test can then correlate the production collector against),
    or `None` on any discovery failure/empty-project/absent-sessionId (all treated as SKIP by the
    caller -- a discovery-probe failure is an environment-availability gap, not a collector
    defect: the collector under test is never invoked with a fabricated session_id).
    """
    argv = ["latitude", "traces", "list", "--project-slug", project_slug, "--limit", "1", "--format", "json"]
    try:
        completed = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, timeout=_DISCOVERY_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    stdout = completed.stdout or ""
    if len(stdout.encode("utf-8")) > _DISCOVERY_MAX_OUTPUT_BYTES:
        return None
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    items = parsed.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        return None
    session_id = items[0].get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return None
    return session_id


def test_latitude_cli_runtime_evidence_live_skips_when_cli_absent(monkeypatch):
    """Issue #2375 Runtime Verification Applicability skip_condition: `latitude` executable not
    found in PATH -> SKIP, never FAIL. Simulated here via monkeypatching `shutil.which` within
    this test's own controlled setup (never by uninstalling the real CLI from the host)."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    if shutil.which("latitude") is None:
        artifact_path = _write_artifact(
            "verify_latitude_runtime_evidence_live_cli.cli_absent_simulation.result.json",
            {
                "status": "skip",
                "generated_at": _now_iso(),
                "skip_reason": "latitude_cli_not_found_in_PATH_simulated",
                "evidence": None,
            },
        )
        pytest.skip(f"SKIP: latitude CLI executable not found in PATH (simulated; artifact: {artifact_path})")
    raise AssertionError("monkeypatched shutil.which must report the CLI as absent")


def test_latitude_cli_runtime_evidence_live():
    latitude_bin = shutil.which("latitude")
    if latitude_bin is None:
        _skip_with_artifact("latitude_cli_not_found_in_PATH")
        return

    project_slug = cs._default_latitude_project_slug()
    if not project_slug:
        _skip_with_artifact("project_slug_unresolved_LATITUDE_PROJECT_env_var_not_set")
        return

    session_id = _discover_real_session_id(project_slug)
    if session_id is None:
        _skip_with_artifact("no_real_session_id_discoverable_in_configured_latitude_project")
        return

    result = cs.collect_latitude_runtime_evidence(project_slug=project_slug, session_id=session_id)

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
    for forbidden_substring in ("stdout", "stderr", "traceback", "Authorization:", "Bearer ", session_id):
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
