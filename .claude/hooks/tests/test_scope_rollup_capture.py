#!/usr/bin/env python3
"""Tests for deterministic scope-rollup final response capture.

Issue #1527 Scope Delta (2): the Codex-gated eligibility/readiness path
(``SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY=1``) is validated
exclusively against the fixed private location artifacts written by
``.claude/scripts/check_session_recording_runtime_safety.py`` — never from
hook-payload-supplied inline objects or arbitrary paths (AC12/AC13/AC14/
AC15). The Claude ``session_manifest_coordinator.sh`` raw-payload path never
sets that env var and must keep capturing exactly as before #1527 (AC16).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

COORDINATOR_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_coordinator.sh"
PRODUCER_PATH = REPO_ROOT / ".claude" / "hooks" / "capture_scope_rollup_final_response.py"
_POLICY_PATH = REPO_ROOT / "docs" / "dev" / "session-recording-policy.md"
_SECRET_POLICY_PATH = REPO_ROOT / "docs" / "dev" / "secret-policy.md"
POLICY_DIGEST = f"sha256:{hashlib.sha256(_POLICY_PATH.read_bytes()).hexdigest()}"
SECRET_POLICY_DIGEST = f"sha256:{hashlib.sha256(_SECRET_POLICY_PATH.read_bytes()).hexdigest()}"
PRODUCER_DIGEST = f"sha256:{hashlib.sha256(PRODUCER_PATH.read_bytes()).hexdigest()}"

# Fixture markers below are generated_at 2026-06-15T12:00:0{1,2,3}Z. Eligibility
# must be minted *before* that (real-world: at session start), and expiry set
# far in the future so the artifact also remains valid at the real wall-clock
# "now" the test executes at (the producer's hook_received_at check uses the
# real clock, not a simulated one).
ELIGIBILITY_GENERATED_AT = "2026-06-15T11:00:00Z"
ELIGIBILITY_EXPIRES_AT = "2030-01-01T00:00:00Z"


def _render_marker(
    *,
    invocation_id: str = "inv-2026-06-15",
    requested_at: str = "2026-06-15T12:00:00Z",
    generated_at: str = "2026-06-15T12:00:01Z",
    status: str = "ok",
) -> str:
    return """```yaml
ISSUE_SCOPE_ROLLUP_RUN_RESULT_V1:
  status: {status}
  schema_version: 1
  repo: squne121/loop-protocol
  current_issue: 873
  invocation_id: {invocation_id}
  requested_at: {requested_at}
  generated_at: {generated_at}
  git_head_sha: 0000000000000000000000000000000000000000
  script_path: .claude/skills/issue-refinement-loop/scripts/plan_issue_scope_rollup.py
  script_blob_sha256: deadbeef
  result:
    plan_schema: ISSUE_SCOPE_ROLLUP_PLAN_V2
    raw_plan_location: /tmp/scope_rollup_{invocation_id}.json
    result_sha256: deadbeef
    verify_status: verified
    suggested_actions_summary: No action
    candidate_count: 0
    high_confidence_count: 0
```""".format(
        status=status,
        invocation_id=invocation_id,
        requested_at=requested_at,
        generated_at=generated_at,
    )


def _run_coordinator(payload: dict[str, object], capture_dir: Path) -> subprocess.CompletedProcess[str]:
    """AC16: the coordinator receives the RAW payload, unmodified — no
    eligibility/readiness JSON injection. This is the exact production
    wiring of .claude/hooks/session_manifest_coordinator.sh."""
    guard_stub = capture_dir / "guard.sh"
    guard_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    guard_stub.chmod(0o755)

    producer_stub = capture_dir / "producer.sh"
    producer_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    producer_stub.chmod(0o755)

    return subprocess.run(
        [str(COORDINATOR_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={
            "PATH": str(Path("/usr/bin")),
            "SESSION_MANIFEST_GUARD": str(guard_stub),
            "SESSION_MANIFEST_PRODUCER": str(producer_stub),
            "SESSION_MANIFEST_NODE": "bash",
            "SCOPE_ROLLUP_CAPTURE_DIR": str(capture_dir),
        },
    )


def _write_json_mode_0600(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _default_eligibility(tmp_path: Path, **overrides: object) -> dict[str, object]:
    artifact = {
        "schema": "SESSION_RECORDING_SCOPE_ROLLUP_ELIGIBILITY_V1",
        "artifact_version": 1,
        "repo_root_realpath": str(REPO_ROOT.resolve()),
        "head_sha": None,
        "policy_digest": POLICY_DIGEST,
        "secret_policy_digest": SECRET_POLICY_DIGEST,
        "public_checkpoint_present": False,
        "visibility": "public",
        "secrets_mode": "none",
        "generated_at": ELIGIBILITY_GENERATED_AT,
        "expires_at": ELIGIBILITY_EXPIRES_AT,
        "safety_verdict": "allow",
    }
    artifact.update(overrides)
    return artifact


def _default_readiness(**overrides: object) -> dict[str, object]:
    artifact = {
        "schema": "SESSION_RECORDING_SCOPE_ROLLUP_READINESS_V1",
        "artifact_version": 1,
        "repo_root_realpath": str(REPO_ROOT.resolve()),
        "uv_lock_digest": None,
        "python_version_digest": None,
        "interpreter_realpath": sys.executable,
        "interpreter_version": "Python 3.x",
        "producer_digest": PRODUCER_DIGEST,
        "prepared": True,
        "generated_at": ELIGIBILITY_GENERATED_AT,
    }
    artifact.update(overrides)
    return artifact


def _run_producer_gated(
    payload: dict[str, object],
    tmp_path: Path,
    *,
    eligibility: dict[str, object] | None = "default",  # type: ignore[assignment]
    readiness: dict[str, object] | None = "default",  # type: ignore[assignment]
    eligibility_path: Path | None = None,
    readiness_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    eligibility_artifact_path = eligibility_path or (tmp_path / "eligibility.json")
    readiness_artifact_path = readiness_path or (tmp_path / "readiness.json")

    if eligibility == "default":
        eligibility = _default_eligibility(tmp_path)
    if eligibility is not None:
        _write_json_mode_0600(eligibility_artifact_path, eligibility)

    if readiness == "default":
        readiness = _default_readiness()
    if readiness is not None:
        _write_json_mode_0600(readiness_artifact_path, readiness)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "SCOPE_ROLLUP_CAPTURE_DIR": str(tmp_path),
        "SCOPE_ROLLUP_REQUIRE_SOURCE_BOUND_ELIGIBILITY": "1",
        "SCOPE_ROLLUP_REPO_ROOT": str(REPO_ROOT.resolve()),
        "SCOPE_ROLLUP_ELIGIBILITY_ARTIFACT_PATH": str(eligibility_artifact_path),
        "SCOPE_ROLLUP_READINESS_ARTIFACT_PATH": str(readiness_artifact_path),
    }
    return subprocess.run(
        [sys.executable, str(PRODUCER_PATH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _read_capture_result(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    result = parsed["SCOPE_ROLLUP_CAPTURE_RESULT_V1"]
    assert isinstance(result, dict)
    return result


def _single_capture_record(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    records = list(tmp_path.glob("scope_rollup_*.capture.yaml"))
    assert len(records) == 1
    return records[0], _read_capture_result(records[0])


def test_deterministic_capture_writes_exact_final_response(tmp_path: Path) -> None:
    message = _render_marker()
    result = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "agent_transcript_path": "/tmp/transcript.log",
            "last_assistant_message": message,
            "stop_hook_active": False,
        },
        tmp_path,
    )

    assert result.returncode == 0, result.stderr

    capture_path = tmp_path / "scope_rollup_inv-2026-06-15.txt"
    record_path = tmp_path / "scope_rollup_inv-2026-06-15.capture.yaml"
    assert capture_path.exists()
    assert capture_path.read_text(encoding="utf-8") == message
    assert stat.S_IMODE(capture_path.stat().st_mode) == 0o600
    record = _read_capture_result(record_path)
    assert record["capture_mode"] == "subagent_stop_hook"
    assert record["capture_status"] == "captured"
    assert record["parser_status"] == "ok"
    assert record["capture_routing_action"] == "continue"
    assert record["capture_source"] == "last_assistant_message"


def test_deterministic_capture_records_duplicate_sidecar_without_overwrite(tmp_path: Path) -> None:
    message = _render_marker()
    first = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message,
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert first.returncode == 0

    capture_path = tmp_path / "scope_rollup_inv-2026-06-15.txt"
    original = capture_path.read_text(encoding="utf-8")

    second = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "last_assistant_message": message + "\nmutated",
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert second.returncode == 0
    assert capture_path.read_text(encoding="utf-8") == original

    captured_record = _read_capture_result(tmp_path / "scope_rollup_inv-2026-06-15.capture.yaml")
    assert captured_record["capture_status"] == "captured"

    duplicate_records = list(tmp_path.glob("scope_rollup_inv-2026-06-15.duplicate_invocation.*.capture.yaml"))
    assert len(duplicate_records) == 1
    duplicate_record = _read_capture_result(duplicate_records[0])
    assert duplicate_record["capture_status"] == "duplicate_invocation"
    assert duplicate_record["capture_routing_action"] == "stop_human"


def test_invocation_id_is_canonicalized_for_filename_safety(tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": _render_marker(invocation_id="2026-06-15T12:00:00Z:abc"),
        "stop_hook_active": False,
    }

    result = _run_coordinator(payload, tmp_path)
    assert result.returncode == 0
    assert (tmp_path / "scope_rollup_2026-06-15T12_00_00Z_abc.txt").exists()


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        (
            {
                "hook_event_name": "Stop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": _render_marker(),
                "stop_hook_active": False,
            },
            "hook_unavailable",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": "",
                "stop_hook_active": False,
            },
            "missing_final_response",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "implementation-worker",
                "last_assistant_message": _render_marker(),
                "stop_hook_active": False,
            },
            "agent_type_mismatch",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": _render_marker(generated_at="2026-06-15T12:00:00Z"),
                "stop_hook_active": False,
            },
            "stale_capture",
        ),
        (
            {
                "hook_event_name": "SubagentStop",
                "agent_type": "scope-rollup-runner",
                "last_assistant_message": "```yaml\nISSUE_SCOPE_ROLLUP_RUN_RESULT_V1: [bad\n```",
                "stop_hook_active": False,
            },
            "parser_rejected",
        ),
    ],
)
def test_fail_closed_capture_paths(
    tmp_path: Path,
    payload: dict[str, object],
    expected_status: str,
) -> None:
    result = _run_coordinator(payload, tmp_path)
    assert result.returncode == 0
    assert not any(tmp_path.glob("scope_rollup_*.txt"))

    record_path, record = _single_capture_record(tmp_path)
    assert record_path.name.startswith("scope_rollup_")
    assert record["capture_status"] == expected_status
    assert record["capture_routing_action"] == "stop_human"
    if expected_status == "hook_unavailable":
        assert record["capture_mode"] == "unsupported"
        assert record["parser_status"] == "not_applicable"
    if expected_status == "missing_final_response":
        assert record["parser_status"] == "marker_missing"
    if expected_status == "agent_type_mismatch":
        assert record["parser_status"] == "not_applicable"
    if expected_status == "parser_rejected":
        assert record["parser_status"] == "marker_malformed"


# ---------------------------------------------------------------------------
# Issue #1527 Scope Delta (2) AC12/AC13/AC14/AC15/AC16
# ---------------------------------------------------------------------------


def test_eligibility_rejects_payload_inline_or_arbitrary_path_artifacts(tmp_path: Path) -> None:
    """AC12: payload-supplied inline objects / arbitrary paths / artifacts[]
    fuzzy-match / source_bound(_artifacts) keys are never consulted — the
    producer only reads the fixed private location env-resolved paths."""
    message = _render_marker()
    forged_eligibility = tmp_path / "forged-eligibility.json"
    _write_json_mode_0600(forged_eligibility, _default_eligibility(tmp_path))
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
        # All of these were the pre-Scope-Delta-(2) accepted payload keys —
        # none of them may be consulted any more.
        "source_bound_eligibility_artifact_path": str(forged_eligibility),
        "source_bound_eligibility": _default_eligibility(tmp_path),
        "source_bound_readiness": _default_readiness(),
        "source_bound": {"eligibility": _default_eligibility(tmp_path)},
        "artifacts": [{"kind": "eligibility", "artifact": _default_eligibility(tmp_path)}],
    }
    # No fixed-location artifacts written at the env-resolved path (which is
    # NOT forged_eligibility above) => the gate must fail-closed as missing,
    # proving the payload-supplied fields above are entirely ignored.
    result = _run_producer_gated(payload, tmp_path, eligibility=None, readiness=None)
    assert result.returncode == 0
    record_path, record = _single_capture_record(tmp_path)
    assert record["capture_status"] == "parser_rejected"
    assert record["parser_status"] == "eligibility_missing"
    assert not any(tmp_path.glob("scope_rollup_*.txt"))


def test_eligibility_binding_rejects_wrong_repo_head_policy_mode_owner_symlink(tmp_path: Path) -> None:
    """AC13: binding validation — wrong repo root, wrong policy digest,
    non-0600 mode, and symlinked artifacts are all fail-closed rejected."""
    message = _render_marker()
    base_payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }

    # wrong repo root binding
    result = _run_producer_gated(
        base_payload, tmp_path,
        eligibility=_default_eligibility(tmp_path, repo_root_realpath="/nonexistent/other/repo"),
    )
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path)
    assert record["parser_status"] == "eligibility_binding_repo_mismatch"

    # wrong policy digest binding
    tmp_path_2 = tmp_path / "case2"
    tmp_path_2.mkdir()
    result = _run_producer_gated(
        base_payload, tmp_path_2, eligibility=_default_eligibility(tmp_path_2, policy_digest="sha256:" + "0" * 64),
    )
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path_2)
    assert record["parser_status"] == "eligibility_binding_policy_digest_mismatch"

    # non-0600 mode
    tmp_path_3 = tmp_path / "case3"
    tmp_path_3.mkdir()
    eligibility_path = tmp_path_3 / "eligibility.json"
    _write_json_mode_0600(eligibility_path, _default_eligibility(tmp_path_3))
    eligibility_path.chmod(0o644)
    result = _run_producer_gated(base_payload, tmp_path_3, eligibility=None, eligibility_path=eligibility_path)
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path_3)
    assert record["parser_status"] == "eligibility_invalid_mode"

    # symlinked artifact
    tmp_path_4 = tmp_path / "case4"
    tmp_path_4.mkdir()
    real_target = tmp_path_4 / "real-eligibility.json"
    _write_json_mode_0600(real_target, _default_eligibility(tmp_path_4))
    symlink_path = tmp_path_4 / "eligibility.json"
    symlink_path.symlink_to(real_target)
    result = _run_producer_gated(base_payload, tmp_path_4, eligibility=None, eligibility_path=symlink_path)
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path_4)
    assert record["parser_status"] == "eligibility_invalid_symlink"


def test_eligibility_lifecycle_requires_pregenerated_artifact_before_marker_and_expiry(tmp_path: Path) -> None:
    """AC14: eligibility.generated_at must be <= marker.generated_at and
    <= hook_received_at, and hook_received_at must be < expires_at."""
    message = _render_marker(generated_at="2026-06-15T12:00:01Z")
    base_payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }

    # eligibility generated AFTER the marker (the exact defect the
    # adversarial review flagged: generated_at > marker.generated_at must be
    # rejected, not required).
    result = _run_producer_gated(
        base_payload, tmp_path,
        eligibility=_default_eligibility(tmp_path, generated_at="2026-06-15T12:00:05Z"),
    )
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path)
    assert record["parser_status"] == "eligibility_stale_generated_after_marker"

    # expired at real "now" (expires_at far in the past).
    tmp_path_2 = tmp_path / "case2"
    tmp_path_2.mkdir()
    result = _run_producer_gated(
        base_payload, tmp_path_2,
        eligibility=_default_eligibility(tmp_path_2, expires_at="2020-01-01T00:00:00Z"),
    )
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path_2)
    assert record["parser_status"] == "eligibility_stale_expired"

    # correctly pre-generated (before marker, not expired) => allowed.
    tmp_path_3 = tmp_path / "case3"
    tmp_path_3.mkdir()
    result = _run_producer_gated(base_payload, tmp_path_3)
    assert result.returncode == 0
    capture_path = tmp_path_3 / "scope_rollup_inv-2026-06-15.txt"
    assert capture_path.exists()


def test_sidecar_provenance_records_artifact_digest_and_verification_verdict(tmp_path: Path) -> None:
    """AC15: sidecar provenance records the verified artifact digests and
    verification reason codes (not just a pass/fail boolean)."""
    message = _render_marker()
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "scope-rollup-runner",
        "last_assistant_message": message,
        "stop_hook_active": False,
    }
    result = _run_producer_gated(payload, tmp_path)
    assert result.returncode == 0
    _, record = _single_capture_record(tmp_path)
    provenance = record["provenance"]
    assert isinstance(provenance, dict)
    assert isinstance(provenance["eligibility_artifact_digest"], str)
    assert provenance["eligibility_artifact_digest"].startswith("sha256:")
    assert provenance["eligibility_verification_reason_code"] == "ok"
    assert isinstance(provenance["readiness_artifact_digest"], str)
    assert provenance["readiness_artifact_digest"].startswith("sha256:")
    assert provenance["readiness_verification_reason_code"] == "ok"


def test_claude_coordinator_raw_payload_still_captures_without_regression(tmp_path: Path) -> None:
    """AC16: the Claude session_manifest_coordinator.sh raw-payload path
    (no eligibility/readiness injection whatsoever, no gate env var) still
    produces the canonical .txt and full SCOPE_ROLLUP_CAPTURE_RESULT_V1
    sidecar exactly as it did before Issue #1527 introduced source-bound
    eligibility. This is the actual production coordinator invocation —
    _run_coordinator() never injects synthetic eligibility/readiness JSON."""
    message = _render_marker(invocation_id="inv-claude-regression")
    result = _run_coordinator(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "scope-rollup-runner",
            "agent_transcript_path": "/tmp/transcript.log",
            "last_assistant_message": message,
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr

    capture_path = tmp_path / "scope_rollup_inv-claude-regression.txt"
    record_path = tmp_path / "scope_rollup_inv-claude-regression.capture.yaml"
    assert capture_path.exists()
    assert capture_path.read_text(encoding="utf-8") == message
    assert stat.S_IMODE(capture_path.stat().st_mode) == 0o600

    record = _read_capture_result(record_path)
    assert record["capture_mode"] == "subagent_stop_hook"
    assert record["capture_status"] == "captured"
    assert record["parser_status"] == "ok"
    assert record["capture_routing_action"] == "continue"
    assert record["capture_sha256"]
    assert record["invocation_id"] == "inv-claude-regression"
    assert record["agent_type"] == "scope-rollup-runner"
    assert record["capture_source"] == "last_assistant_message"
    # ungated path: provenance digests are absent (no fixed-location gate
    # was consulted at all for this caller).
    assert record["provenance"]["eligibility_artifact_digest"] is None
