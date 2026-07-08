"""Tests for the delegation_audit_v1 JSONL audit stream (Issue #1272).

Covers:
  AC1: delegation_audit_v1 closed-schema validation (required/optional keys,
       types, enums, redaction invariant).
  AC2: exactly one start + one end record per top-level run_delegation()
       invocation across success / validation failure / unknown provider /
       AGY timeout / AGY not found / AGY permission denied / provider=auto
       fallback / model downgrade / post failure / grounded redaction fail /
       local Serena retrieval fail.
  AC3: audit output isolation (--audit-log / DELEGATION_AUDIT_LOG_PATH only,
       JSONL, append-only, never mixed with result JSON/NDJSON/stdout/stderr).
  AC4: redaction-before-truncate (raw prompt / credential / transcript / HOME
       path / repo absolute path never appear).
  AC5: provider auto / retry / model downgrade fields match
       provider_auto_policy_v1 (Issue #1270).
  AC6: grounded_research / local_asset_research / auth diagnostics
       public-safe metadata (no raw evidence).
  AC7: post_result separates request success from posting success.
  AC8: parent_run_id / subtask_id / attempt_id reserved fan-out fields
       (Issue #1273).
  AC10: CI coverage of this test file is already implied by the existing
       tests/ directory target in .github/ci/python-test-plan.json.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, mirrors test_quota_fallback.py convention)
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_gemini_headless.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("run_gemini_headless", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rgh = _load_module()


@pytest.fixture(autouse=True)
def _reset_audit_override():
    rgh.set_audit_log_path_override(None)
    yield
    rgh.set_audit_log_path_override(None)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _base_request(**overrides) -> dict:
    base = {
        "schema": "delegation_request_v1",
        "provider": "gemini",
        "tool_profile": "no_tools",
        "model": "gemini-3-flash-preview",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure."],
        "output_sections": ["Summary"],
    }
    base.update(overrides)
    return base


def _core_result(**overrides) -> dict:
    result = {
        "schema": "delegation_result/v1",
        "ok": True,
        "requested_model": "gemini-3-flash-preview",
        "actual_model": "gemini-3-flash-preview",
        "tool_profile": "no_tools",
        "exit_code": 0,
        "failure_class": None,
        "failure_reason": None,
        "response_text": "ok",
        "warnings": [],
    }
    result.update(overrides)
    return result


# ---------------------------------------------------------------------------
# AC1: closed schema validation
# ---------------------------------------------------------------------------


def test_delegation_audit_v1_start_record_closed_schema_rejects_unknown_key():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "start",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "provider_requested": "gemini",
        "tool_profile": "no_tools",
        "unexpected_key": "not allowed",
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("unknown key" in e for e in errors)


def test_delegation_audit_v1_start_record_missing_required_key_rejected():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "start",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "tool_profile": "no_tools",
        # provider_requested missing
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("missing required key" in e for e in errors)


def test_delegation_audit_v1_end_record_closed_schema_valid():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "ok": True,
        "failure_class": None,
        "failure_reason": None,
        "actual_model": "gemini-3-flash-preview",
        "tool_profile": "no_tools",
    }
    assert rgh.validate_delegation_audit_record(record) == []


def test_delegation_audit_v1_wrong_schema_value_rejected():
    record = {
        "schema": "some_other_schema/v1",
        "record_type": "start",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "provider_requested": "gemini",
        "tool_profile": "no_tools",
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("schema" in e for e in errors)


def test_delegation_audit_v1_unknown_record_type_rejected():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "middle",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("record_type" in e for e in errors)


def test_delegation_audit_v1_end_ok_must_be_bool():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "ok": "true",  # wrong type -- must be bool
        "failure_class": None,
        "failure_reason": None,
        "actual_model": "gemini-3-flash-preview",
        "tool_profile": "no_tools",
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("ok must be a bool" in e for e in errors)


def test_delegation_audit_v1_nested_redaction_violation_is_rejected():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "ok": False,
        "failure_class": "unexpected_exception",
        "failure_reason": "unexpected",
        "actual_model": "unknown",
        "tool_profile": "no_tools",
        "provider_attempts": [
            {
                "provider": "gemini",
                "ok": False,
                "failure_class": "unexpected_exception",
                "failure_reason": "repo path leak: " + str(rgh._repo_root()),
                "exit_code": 1,
                "retryable_for_provider_fallback": False,
                "model_downgrades": [],
                "model_chain": [],
                "attempts_by_model": {},
                "post_to_issue_url_requested": False,
                "post_result": None,
                "stopped_by": "stop_if:non_retryable_failure_class:unexpected_exception",
            }
        ],
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("provider_attempts[0].failure_reason" in e for e in errors)


def test_delegation_audit_v1_post_result_closed_schema_rejects_unknown_key():
    record = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": "abc123",
        "ts": "2026-07-07T00:00:00Z",
        "ok": False,
        "failure_class": "agy_post_to_issue_url_forbidden",
        "failure_reason": "forbidden",
        "actual_model": "unknown",
        "tool_profile": "no_tools",
        "post_result": {
            "post_requested": True,
            "post_allowed": False,
            "post_target_type": "issue_only",
            "request_success": False,
            "posting_success": None,
            "post_result": "forbidden",
            "post_failure_class": "agy_post_to_issue_url_forbidden",
            "extra": "not allowed",
        },
    }
    errors = rgh.validate_delegation_audit_record(record)
    assert any("post_result has unknown key" in e for e in errors)


# ---------------------------------------------------------------------------
# AC2: start/end pairing across every named failure path
# ---------------------------------------------------------------------------


_FAILURE_PATH_RESULTS = {
    "success": _core_result(ok=True, failure_class=None, failure_reason=None),
    "validation_failure": _core_result(
        ok=False, failure_class="request_validation_failed", failure_reason="objective is required"
    ),
    "unknown_provider": _core_result(ok=False, failure_class="unknown_provider", failure_reason="unknown_provider: x"),
    "agy_timeout": _core_result(
        ok=False, failure_class="agy_timeout", failure_reason="agy_timeout: process exceeded 600s"
    ),
    "agy_not_found": _core_result(
        ok=False, failure_class="agy_not_found", failure_reason="agy_not_found: agy binary not found in PATH"
    ),
    "agy_permission_denied": _core_result(
        ok=False, failure_class="agy_permission_denied", failure_reason="agy_permission_denied: forbidden"
    ),
    "model_downgrade": _core_result(
        ok=True,
        model_downgrades=[{"from": "gemini-3-flash-preview", "to": "gemini-2.5-flash", "reason": "capacity"}],
    ),
    "post_failure": _core_result(
        ok=False,
        failure_class="post_to_issue_url_failed",
        failure_reason="post_to_issue_url: gh issue comment failed (exit 1)",
        post_request_success=True,
        post_posting_success=False,
        post_failure_class="post_to_issue_url_failed",
        post_result="failed: some gh error",
    ),
    "grounded_redaction_fail": _core_result(
        ok=False,
        failure_class="agy_web_grounding_redaction_failed",
        failure_reason="agy_web_grounding_redaction_failed: fail-closed evidence check failed",
        grounded_research_evidence={
            "grounding_actor": "antigravity_cli",
            "grounding_backend": "none",
            "grounding_status": "failed",
            "web_tool_call_count": 0,
            "search_query_count": 0,
            "url_citation_count": 0,
            "citation_evidence": ["<raw evidence -- must not leak into audit>"],
            "grounding_transcript_evidence": "<raw transcript -- must not leak into audit>",
            "grounding_failure_class": "agy_web_grounding_redaction_failed",
            "raw_transcript_included": False,
            "raw_credential_included": True,
            "repo_absolute_path_included": False,
        },
    ),
    "local_serena_retrieval_fail": _core_result(
        ok=False,
        tool_profile="local_asset_research",
        failure_class="local_asset_research live_serena_mcp_failed",
        failure_reason="local_asset_research live_serena_mcp_failed: mcp session closed",
    ),
}


@pytest.mark.parametrize("path_name", sorted(_FAILURE_PATH_RESULTS))
def test_audit_start_end_pairing_across_failure_paths(tmp_path, path_name):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request()
    result = _FAILURE_PATH_RESULTS[path_name]

    with patch.object(rgh, "_run_delegation_core", return_value=result):
        returned = rgh.run_delegation(request)

    assert returned is result
    records = _read_jsonl(audit_path)
    assert len(records) == 2, f"{path_name}: expected exactly 1 start + 1 end record, got {len(records)}"
    start, end = records
    assert start["record_type"] == "start"
    assert end["record_type"] == "end"
    assert start["run_id"] == end["run_id"]
    for record in records:
        assert rgh.validate_delegation_audit_record(record) == []


def test_audit_provider_auto_fallback_emits_single_pair_not_one_per_attempt(tmp_path):
    """provider="auto" re-enters run_delegation() once per candidate inside
    provider_auto_dispatch() -- only the outermost call may emit an audit
    pair (Issue #1272 AC2)."""
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request(provider="auto", tool_profile="no_tools")

    def fake_core(req, request_path=None, _routing=None):
        provider = req.get("provider")
        if provider == "auto":
            return rgh.provider_auto_dispatch(req, request_path=request_path, _routing=_routing)
        if provider == "gemini":
            return _core_result(ok=False, failure_class="model_chain_exhausted", failure_reason="quota exhausted")
        return _core_result(ok=True, tool_profile="no_tools")

    with patch.object(rgh, "_run_delegation_core", side_effect=fake_core):
        result = rgh.run_delegation(request)

    assert result["ok"] is True
    records = _read_jsonl(audit_path)
    assert len(records) == 2
    assert records[0]["record_type"] == "start"
    assert records[1]["record_type"] == "end"


# ---------------------------------------------------------------------------
# AC3: audit output isolation
# ---------------------------------------------------------------------------


def test_audit_disabled_by_default_no_file_written(tmp_path):
    # No override, no env var set (autouse fixture resets override).
    request = _base_request()
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)
    # Nothing to assert on disk -- absence of any path configuration means
    # _resolve_audit_log_path() returns None and no file is ever created.
    assert rgh._resolve_audit_log_path() is None


def test_audit_env_var_activation(tmp_path, monkeypatch):
    audit_path = tmp_path / "env_audit.jsonl"
    monkeypatch.setenv("DELEGATION_AUDIT_LOG_PATH", str(audit_path))
    request = _base_request()
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)
    records = _read_jsonl(audit_path)
    assert len(records) == 2


def test_audit_log_is_jsonl_one_object_per_line_append_only(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request()
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)

    raw = audit_path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 4  # two invocations x (start + end), appended not overwritten
    for line in lines:
        parsed = json.loads(line)  # each line must be exactly one JSON object
        assert parsed["schema"] == "delegation_audit_v1"


def test_audit_log_separate_from_result_output_file(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    output_path = tmp_path / "result.json"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request()
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        result = rgh.run_delegation(request)
    rgh._dump_json(output_path, result)

    audit_records = _read_jsonl(audit_path)
    assert len(audit_records) == 2
    output_payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert output_payload["schema"] == "delegation_result/v1"
    # The two streams never share records.
    assert output_payload not in audit_records


# ---------------------------------------------------------------------------
# AC4: redaction-before-truncate
# ---------------------------------------------------------------------------


def test_audit_redaction_before_truncate(tmp_path, monkeypatch):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    fake_credential = "ghp_" + ("a" * 36)
    fake_home = "/home/fake-audit-user"
    monkeypatch.setenv("HOME", fake_home)
    repo_root = str(rgh._repo_root())
    long_tail = "y" * 900
    raw_failure_reason = (
        f"leaked credential {fake_credential} at {fake_home}/secret and repo path "
        f"{repo_root}/private -- {long_tail}"
    )
    request = _base_request()
    result = _core_result(ok=False, failure_class="agy_exit_nonzero", failure_reason=raw_failure_reason)

    with patch.object(rgh, "_run_delegation_core", return_value=result):
        rgh.run_delegation(request)

    records = _read_jsonl(audit_path)
    end = records[1]
    reason = end["failure_reason"]
    assert reason is not None
    assert fake_credential not in reason
    assert fake_home not in reason
    assert repo_root not in reason
    # Redaction happens before truncation -- if truncation ran first, a
    # credential fragment could survive past the cut point undetected.
    assert len(reason) <= rgh._AUDIT_FAILURE_REASON_MAX_LEN
    assert rgh.validate_delegation_audit_record(end) == []


def test_audit_mask_text_redacts_home_and_repo_root_and_credentials(monkeypatch):
    fake_home = "/home/another-fake-user"
    monkeypatch.setenv("HOME", fake_home)
    repo_root = str(rgh._repo_root())
    text = f"prompt at {fake_home}/x and {repo_root}/y with ghp_" + ("b" * 36)
    masked = rgh._audit_mask_text(text)
    assert fake_home not in masked
    assert repo_root not in masked
    assert "ghp_" + ("b" * 36) not in masked


# ---------------------------------------------------------------------------
# AC5: provider auto / retry / model downgrade fields match
# provider_auto_policy_v1 (Issue #1270)
# ---------------------------------------------------------------------------


def test_audit_provider_auto_fields_match_provider_auto_policy_v1(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request(provider="auto")
    result = _core_result(
        ok=True,
        provider="auto",
        selected_provider="agy",
        provider_attempts=[
            {
                "provider": "gemini",
                "ok": False,
                "failure_class": "model_chain_exhausted",
                "failure_reason": "quota exhausted",
            },
            {"provider": "agy", "ok": True, "failure_class": None, "failure_reason": None},
        ],
        fallback_reason=None,
        fallback_policy_version=rgh.PROVIDER_AUTO_FALLBACK_POLICY_VERSION,
        attempts_by_model={"gemini-3-flash-preview": 3},
    )

    with patch.object(rgh, "_run_delegation_core", return_value=result):
        rgh.run_delegation(request)

    records = _read_jsonl(audit_path)
    end = records[1]
    for field in rgh.PROVIDER_AUTO_RESULT_FIELDS:
        assert field in end, f"{field!r} must be recorded on the audit end record when present on the result"
    assert end["selected_provider"] == "agy"
    assert end["fallback_policy_version"] == rgh.PROVIDER_AUTO_FALLBACK_POLICY_VERSION
    assert isinstance(end["provider_attempts"], list)
    assert len(end["provider_attempts"]) == 2


def test_build_delegation_audit_record_is_the_single_end_record_builder():
    request = _base_request()
    result = _core_result(ok=True)
    record = rgh._build_delegation_audit_record("run-id-123", request, result)
    assert record["schema"] == "delegation_audit_v1"
    assert record["record_type"] == "end"
    assert record["run_id"] == "run-id-123"
    assert rgh.validate_delegation_audit_record(record) == []


# ---------------------------------------------------------------------------
# AC6: grounded_research / local_asset_research / auth diagnostics
# public-safe metadata
# ---------------------------------------------------------------------------


def test_audit_includes_grounding_serena_auth_public_safe_fields(tmp_path):
    audit_path = tmp_path / "audit.jsonl"

    # -- grounded_research: public-safe subset only, no raw evidence --
    rgh.set_audit_log_path_override(audit_path)
    grounded_result = _core_result(
        ok=True,
        tool_profile="grounded_research",
        grounded_research_evidence={
            "grounding_actor": "antigravity_cli",
            "grounding_backend": "agy_native_websearch",
            "grounding_status": "grounded",
            "web_tool_call_count": 1,
            "search_query_count": 1,
            "url_citation_count": 2,
            "citation_evidence": ["<raw citation text -- must never appear in audit>"],
            "grounding_transcript_evidence": "<raw transcript -- must never appear in audit>",
            "grounding_failure_class": None,
            "raw_transcript_included": False,
            "raw_credential_included": False,
            "repo_absolute_path_included": False,
        },
    )
    with patch.object(rgh, "_run_delegation_core", return_value=grounded_result):
        rgh.run_delegation(_base_request(tool_profile="grounded_research"))
    grounded_end = _read_jsonl(audit_path)[1]
    grounded_meta = grounded_end["grounded_metadata"]
    assert grounded_meta["grounding_status"] == "grounded"
    assert grounded_meta["url_citation_count"] == 2
    assert "citation_evidence" not in grounded_meta
    assert "grounding_transcript_evidence" not in grounded_meta
    dumped = json.dumps(grounded_end)
    assert "<raw citation text" not in dumped
    assert "<raw transcript" not in dumped

    # -- local_asset_research (Serena retrieval): public-safe counts only --
    audit_path.unlink()
    serena_result = _core_result(
        ok=False,
        tool_profile="local_asset_research",
        failure_class="local_asset_research live_serena_mcp_failed",
        failure_reason="local_asset_research live_serena_mcp_failed: mcp session closed",
    )
    with patch.object(rgh, "_run_delegation_core", return_value=serena_result):
        rgh.run_delegation(
            _base_request(
                tool_profile="local_asset_research",
                context_files=["a.md", "b.md"],
            )
        )
    serena_end = _read_jsonl(audit_path)[1]
    local_meta = serena_end["local_asset_metadata"]
    assert local_meta["profile"] == "local_asset_research"
    assert local_meta["context_files_count"] == 2
    assert local_meta["serena_retrieval_failed"] is True

    # -- auth diagnostics: derived from the existing AGY auth failure_class
    # enum (Issue #1267 territory), public-safe (no raw AGY output). --
    audit_path.unlink()
    auth_result = _core_result(
        ok=False,
        failure_class="agy_permission_denied",
        failure_reason="agy_permission_denied: forbidden",
    )
    with patch.object(rgh, "_run_delegation_core", return_value=auth_result):
        rgh.run_delegation(_base_request(provider="agy"))
    auth_end = _read_jsonl(audit_path)[1]
    auth_meta = auth_end["auth_diagnostics_metadata"]
    assert auth_meta["auth_failure_class"] == "agy_permission_denied"


# ---------------------------------------------------------------------------
# AC7: post_result separates request success from posting success
# ---------------------------------------------------------------------------


def test_audit_post_result_separates_request_and_posting_success(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)

    # Content generation succeeded, but the gh issue comment post failed --
    # request_success must stay True even though posting_success is False
    # and the overall ok flips to False (Issue #1272 AC7 / #1270 fix_delta).
    request = _base_request(post_to_issue_url="https://github.com/o/r/issues/1")
    result = _core_result(
        ok=False,
        failure_class="post_to_issue_url_failed",
        failure_reason="post_to_issue_url: gh issue comment failed (exit 1)",
        post_request_success=True,
        post_posting_success=False,
        post_failure_class="post_to_issue_url_failed",
        post_result="failed: some gh error",
    )
    with patch.object(rgh, "_run_delegation_core", return_value=result):
        rgh.run_delegation(request)

    end = _read_jsonl(audit_path)[1]
    post_result = end["post_result"]
    assert post_result["post_requested"] is True
    assert post_result["post_allowed"] is True
    assert post_result["post_target_type"] == "issue_only"
    assert post_result["request_success"] is True
    assert post_result["posting_success"] is False
    assert post_result["post_result"] == "failed: some gh error"
    assert post_result["post_failure_class"] == "post_to_issue_url_failed"
    assert end["ok"] is False


def test_audit_agy_forbidden_post_to_issue_url_surfaces_forbidden_post_result(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request(
        provider="agy",
        tool_profile="no_tools",
        model=None,
        prompt="Return a short summary.",
        post_to_issue_url="https://github.com/owner/repo/issues/1",
    )

    result = rgh.run_delegation(request)

    assert result["ok"] is False
    assert result["failure_class"] == "provider_forbids_post_to_issue_url"
    end = _read_jsonl(audit_path)[1]
    assert end["post_result"] == {
        "post_requested": True,
        "post_allowed": False,
        "post_target_type": "issue_only",
        "request_success": False,
        "posting_success": None,
        "post_result": "forbidden",
        "post_failure_class": "agy_post_to_issue_url_forbidden",
    }


def test_audit_post_result_absent_when_post_not_requested(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request()
    result = _core_result(ok=True)
    with patch.object(rgh, "_run_delegation_core", return_value=result):
        rgh.run_delegation(request)
    end = _read_jsonl(audit_path)[1]
    assert "post_result" not in end


def test_run_delegation_real_post_to_issue_url_populates_separated_success_fields(tmp_path, monkeypatch):
    """Exercise the real (non-mocked) post-processing branch inside
    _run_delegation_core() -- via the public run_delegation() entry point,
    mirroring test_run_gemini_headless.py's own monkeypatch convention -- to
    confirm post_request_success / post_posting_success are actually
    populated on delegation_result/v1, not just asserted against a
    hand-built fixture."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "context.md").write_text("test context", encoding="utf-8")
    request = {
        "schema": "delegation_request_v1",
        "objective": "Investigate build failure in logs/build.log",
        "instructions": ["Summarize the failure.", "List likely root causes."],
        "tool_profile": "no_tools",
        "output_sections": ["Summary"],
        "context_files": ["context.md"],
        "model": "gemini-3-flash-preview",
        "post_to_issue_url": "https://github.com/owner/repo/issues/1",
    }

    class GeminiSuccess:
        returncode = 0
        stdout = json.dumps({"response": "the generated answer"})
        stderr = ""

    monkeypatch.setattr(
        rgh, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: GeminiSuccess()
    )

    def fake_subprocess_run(cmd, **kwargs):
        assert cmd[:3] == ["gh", "issue", "comment"]
        return rgh.subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="gh: not found")

    monkeypatch.setattr(rgh.subprocess, "run", fake_subprocess_run)

    result = rgh.run_delegation(request, request_path=tmp_path / "request.json")

    assert result["post_request_success"] is True
    assert result["post_posting_success"] is False
    assert result["ok"] is False
    assert result["post_failure_class"] == "post_to_issue_url_failed"


def test_run_delegation_unexpected_exception_writes_redacted_audit_end_record(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    secret = "ghp_" + ("z" * 36)
    leaked_path = str(rgh._repo_root())
    request = _base_request()

    with patch.object(
        rgh,
        "_run_delegation_core",
        side_effect=RuntimeError(f"boom {secret} at {leaked_path}/private"),
    ):
        with pytest.raises(RuntimeError):
            rgh.run_delegation(request)

    records = _read_jsonl(audit_path)
    assert len(records) == 2
    end = records[1]
    assert end["record_type"] == "end"
    assert end["failure_class"] == "unexpected_exception"
    assert secret not in (end["failure_reason"] or "")
    assert leaked_path not in (end["failure_reason"] or "")
    assert rgh.validate_delegation_audit_record(end) == []


# ---------------------------------------------------------------------------
# AC8: parent_run_id / subtask_id / attempt_id reserved fan-out fields
# (Issue #1273)
# ---------------------------------------------------------------------------


def test_audit_reserved_fanout_fields_absent_when_not_requested(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request()
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)
    start, end = _read_jsonl(audit_path)
    for key in ("parent_run_id", "subtask_id", "attempt_id"):
        assert key not in start
        assert key not in end


def test_audit_reserved_fanout_fields_propagate_when_present(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    rgh.set_audit_log_path_override(audit_path)
    request = _base_request(
        parent_run_id="parent-run-1",
        subtask_id="subtask-2",
        attempt_id="attempt-3",
    )
    with patch.object(rgh, "_run_delegation_core", return_value=_core_result(ok=True)):
        rgh.run_delegation(request)
    start, end = _read_jsonl(audit_path)
    for record in (start, end):
        assert record["parent_run_id"] == "parent-run-1"
        assert record["subtask_id"] == "subtask-2"
        assert record["attempt_id"] == "attempt-3"
        assert rgh.validate_delegation_audit_record(record) == []


# ---------------------------------------------------------------------------
# AC9 / AC10: documentation and CI coverage cross-checks
# ---------------------------------------------------------------------------


def test_runtime_portability_doc_documents_delegation_audit_v1():
    doc_path = _SCRIPT_PATH.parents[1] / "references" / "runtime-portability.md"
    text = doc_path.read_text(encoding="utf-8")
    assert "delegation_audit_v1" in text
    assert "DELEGATION_AUDIT_LOG_PATH" in text or "--audit-log" in text


def test_main_audit_log_override_is_scoped_per_invocation(tmp_path, monkeypatch):
    request_file = tmp_path / "request.json"
    context_file = tmp_path / "context.md"
    context_file.write_text("context", encoding="utf-8")
    request_file.write_text(
        json.dumps(
            {
                "schema": "delegation_request_v1",
                "objective": "Investigate build failure in logs/build.log",
                "instructions": ["Summarize the failure.", "List likely root causes."],
                "tool_profile": "no_tools",
                "output_sections": ["Summary"],
                "context_files": ["context.md"],
                "model": "gemini-3-flash-preview",
            }
        ),
        encoding="utf-8",
    )
    audit_path = tmp_path / "audit.jsonl"
    output_one = tmp_path / "result-1.json"
    output_two = tmp_path / "result-2.json"

    class GeminiSuccess:
        returncode = 0
        stdout = json.dumps({"response": "same answer"})
        stderr = ""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        rgh, "_run_gemini", lambda command, timeout_sec, prompt=None, cwd=None: GeminiSuccess()
    )

    exit_code_one = rgh.main(
        [
            "--request-file", str(request_file),
            "--output-file", str(output_one),
            "--audit-log", str(audit_path),
        ]
    )
    first_records = _read_jsonl(audit_path)
    exit_code_two = rgh.main(
        [
            "--request-file", str(request_file),
            "--output-file", str(output_two),
        ]
    )

    assert exit_code_one == 0
    assert exit_code_two == 0
    assert len(first_records) == 2
    assert _read_jsonl(audit_path) == first_records


def test_ci_python_test_plan_coverage_confirmed():
    """Issue #1272 AC10: .github/ci/python-test-plan.json must already cover
    this file's directory (no CI configuration change required). The plan
    lists the whole gemini-cli-headless-delegation/tests/ directory, which
    transitively covers this new test_delegation_audit_schema.py file."""
    # _SCRIPT_PATH = <repo>/.claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py
    # parents[0]=scripts parents[1]=gemini-cli-headless-delegation parents[2]=skills
    # parents[3]=.claude parents[4]=<repo root>
    repo_root = _SCRIPT_PATH.parents[4]
    plan_path = repo_root / ".github" / "ci" / "python-test-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    targets = plan["targets"]
    tests_dir_rel = ".claude/skills/gemini-cli-headless-delegation/tests/"
    assert tests_dir_rel in targets, (
        "python-test-plan.json must list the gemini-cli-headless-delegation "
        "tests/ directory so this new audit-schema test file is picked up "
        "without any CI configuration change (ci_python_test_plan_coverage_confirmed)."
    )
    this_file = Path(__file__).resolve()
    assert this_file.name == "test_delegation_audit_schema.py"
    assert this_file.is_relative_to(repo_root / tests_dir_rel)
