#!/usr/bin/env python3
"""Tests for run_retrospective.py's session-window coverage layer (Issue
#2601 P0: ``--since-last-retrospective`` false-green prevention).

Fixture/mock-free where possible: `collect_session_sources` / the CLI
entrypoint below are exercised against the REAL `collect_snapshot.py`
collector functions and real temp-directory JSONL fixtures, not a stand-in
double -- only `clock`/`run_nonce` are injected for determinism.

Covers every Issue #2601 AC that is a pytest -k target:
  AC1  source_coverage_five_state_vocabulary_reuses_collector_enum
  AC2  scenario_a_zero_collector_wired_never_complete
  AC3  scenario_b_one_source_unavailable_evidence_retained
  AC4  scenario_c_both_observed_zero_selected_is_complete
  AC5  watermark_invariant / guard_checkpoint_advancement_regression
  AC6  scenario_d_e_checkpoint_disposition
  AC7  orchestration_independent_of_analysis_completeness
  AC8  focused_tests: no_collector / one_source_unavailable / both_sources_zero /
       both_sources_nonzero / first_run_no_publication
  AC10 schema-valid output (session_window_coverage_v1.schema.json)

AC9 (live runtime verification) is a SEPARATE opt-in file:
``verify_since_last_retrospective_live_cli.py`` in this same directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_SKILL_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_collect_snapshot = rr._collect_snapshot_module()

_FIXED_NOW = "2026-09-10T12:00:00Z"


def _clock():
    from datetime import datetime, timezone

    return datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def _collector_result(
    *,
    source_status: str,
    provenance: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
):
    return _collect_snapshot.CollectorResult(
        observation={
            "source_type": "runtime",
            "source_id": "x",
            "source_status": source_status,
            "pagination_completeness": "complete",
        },
        private_evidence={"provenance": provenance or {}, "diagnostics": diagnostics or {}},
    )


# ---------------------------------------------------------------------------
# AC1: five-state vocabulary, reuses collect_snapshot.py's source_status enum
# ---------------------------------------------------------------------------


def test_ac1_source_coverage_five_state_vocabulary_reuses_collector_enum():
    # not requested -> not_requested, regardless of collector_result
    entry = rr.compute_source_coverage_entry("claude_code", None, required=False)
    assert entry == {"status": "not_requested", "reason_code": None, "selected_session_count": None}

    # required, never wired -> required / collector_not_configured
    entry = rr.compute_source_coverage_entry("claude_code", None, required=True)
    assert entry["status"] == "required"
    assert entry["reason_code"] == "collector_not_configured"
    assert entry["selected_session_count"] is None

    # required, collector "complete" -> observed
    result = _collector_result(source_status="complete", provenance={"sessions_read": 3})
    entry = rr.compute_source_coverage_entry("claude_code", result, required=True)
    assert entry == {"status": "observed", "reason_code": None, "selected_session_count": 3}

    # required, collector "partial" -> partial, reason_code non-null, best-effort count retained
    result = _collector_result(
        source_status="partial", provenance={"sessions_read": 2}, diagnostics={"reason_code": "malformed_response"}
    )
    entry = rr.compute_source_coverage_entry("claude_code", result, required=True)
    assert entry["status"] == "partial"
    assert entry["reason_code"] == "malformed_response"
    assert entry["selected_session_count"] == 2

    # required, collector "unavailable"/"blocked" -> unavailable, count always None (never 0)
    for collector_status in ("unavailable", "blocked"):
        result = _collector_result(source_status=collector_status, diagnostics={"reason_code": "source_not_present"})
        entry = rr.compute_source_coverage_entry("claude_code", result, required=True)
        assert entry["status"] == "unavailable"
        assert entry["reason_code"] == "source_not_present"
        assert entry["selected_session_count"] is None


def test_ac1_unrecognized_collector_status_fails_closed():
    with pytest.raises(ValueError):
        rr.compute_source_coverage_entry(
            "claude_code", _collector_result(source_status="totally_unknown"), required=True
        )


def test_ac1_claude_gpt_selected_session_count_uses_complete_sessions_provenance():
    result = _collector_result(source_status="complete", provenance={"complete_sessions": ["s1", "s2"]})
    entry = rr.compute_source_coverage_entry("claude_gpt", result, required=True)
    assert entry["selected_session_count"] == 2


# ---------------------------------------------------------------------------
# AC2 / AC8 no_collector: Verification Scenario A
# ---------------------------------------------------------------------------


def test_ac2_ac8_no_collector_scenario_a_zero_wired_never_complete():
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    assert result["analysis_completeness"] == "unavailable"
    assert result["analysis_completeness"] != "complete"
    for source_id in ("claude_code", "claude_gpt"):
        entry = result["source_coverage"][source_id]
        assert entry["status"] == "required"
        assert entry["selected_session_count"] is None, "0 collector wiring must never be reported as 0 sessions"
    assert result["checkpoint"]["checkpoint_advanced"] is False
    assert result["checkpoint"]["checkpoint_advance_reason"] == "blocked_missing_required_source"


# ---------------------------------------------------------------------------
# AC3 / AC8 one_source_unavailable: Verification Scenario B
# ---------------------------------------------------------------------------


def test_ac3_ac8_one_source_unavailable_scenario_b_evidence_retained():
    claude_code_result = _collector_result(source_status="complete", provenance={"sessions_read": 5})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={"claude_code": claude_code_result, "claude_gpt": None},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    # the observed side's own evidence is retained, not zeroed/discarded
    assert result["source_coverage"]["claude_code"] == {
        "status": "observed",
        "reason_code": None,
        "selected_session_count": 5,
    }
    assert result["source_coverage"]["claude_gpt"]["status"] == "required"
    assert result["analysis_completeness"] == "degraded"
    assert result["cross_runtime_comparison"]["status"] in ("unavailable", "partial")
    assert result["cross_runtime_comparison"]["reason_code"] is not None


# ---------------------------------------------------------------------------
# AC4 / AC8 both_sources_zero: Verification Scenario C
# ---------------------------------------------------------------------------


def test_ac4_ac8_both_sources_zero_scenario_c_complete_distinct_from_ac2():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={"claude_code": zero_result, "claude_gpt": zero_result},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    assert result["analysis_completeness"] == "complete"
    for source_id in ("claude_code", "claude_gpt"):
        entry = result["source_coverage"][source_id]
        assert entry["status"] == "observed"
        assert entry["selected_session_count"] == 0
        # AC2's unwired state uses status "required" -- deterministically distinct from "observed"
        assert entry["status"] != "required"


# ---------------------------------------------------------------------------
# AC8 both_sources_nonzero
# ---------------------------------------------------------------------------


def test_ac8_both_sources_nonzero():
    nonzero_result = _collector_result(source_status="complete", provenance={"sessions_read": 4})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={"claude_code": nonzero_result, "claude_gpt": nonzero_result},
        prior_watermark={"from_exclusive": None, "to_inclusive": "2026-09-01T00:00:00Z", "covered_sources": []},
        publish_authorized=True,
        clock=_clock,
    )
    assert result["analysis_completeness"] == "complete"
    assert result["checkpoint"]["checkpoint_advanced"] is True
    assert result["checkpoint"]["checkpoint_advance_reason"] == "advanced_full_coverage"


# ---------------------------------------------------------------------------
# AC5: watermark invariant + SessionWindowCoverageRegression guard
# ---------------------------------------------------------------------------


_PRIOR_WATERMARK_NO_COVERAGE = {"from_exclusive": None, "to_inclusive": "2026-09-01T00:00:00Z", "covered_sources": []}
_PRIOR_WATERMARK_BOTH_SOURCES = {
    "from_exclusive": None,
    "to_inclusive": "2026-09-01T00:00:00Z",
    "covered_sources": ["claude_code", "claude_gpt"],
}
_PRIOR_WATERMARK_CLAUDE_CODE_ONLY = {
    "from_exclusive": None,
    "to_inclusive": "2026-09-01T00:00:00Z",
    "covered_sources": ["claude_code"],
}


def test_ac5_watermark_covers_only_observed_sources():
    observed = _collector_result(source_status="complete", provenance={"sessions_read": 1})
    coverage = rr.compute_source_coverage_map(
        ["claude_code", "claude_gpt"], {"claude_code": observed, "claude_gpt": None}
    )
    watermark = rr.compute_session_window(
        prior_watermark=_PRIOR_WATERMARK_NO_COVERAGE,
        source_coverage=coverage,
        clock=_clock,
    )
    assert watermark == {
        "from_exclusive": "2026-09-01T00:00:00Z",
        "to_inclusive": _FIXED_NOW,
        "covered_sources": ["claude_code"],
    }


def test_ac5_guard_raises_on_direct_regression():
    observed_only_claude_code = rr.compute_source_coverage_map(
        ["claude_code"],
        {"claude_code": _collector_result(source_status="complete", provenance={"sessions_read": 1})},
    )
    with pytest.raises(rr.SessionWindowCoverageRegression) as exc_info:
        rr.guard_checkpoint_advancement(observed_only_claude_code, _PRIOR_WATERMARK_BOTH_SOURCES)
    assert exc_info.value.reason_code == "session_window_coverage_regression"


def test_ac5_guard_noop_on_first_run():
    coverage = rr.compute_source_coverage_map(["claude_code"], {})
    rr.guard_checkpoint_advancement(coverage, None)  # must not raise


def test_ac5_ac8_scenario_d_narrowed_required_sources_blocks_via_guard_not_step1():
    """Verification Scenario D's cross-run-narrowing case: this run's
    `required_sources` narrows to only `claude_code` (fully observed --
    step 1's `all_observed` check alone would PASS), but the PRIOR watermark
    covered `claude_gpt` too. `compute_checkpoint_disposition` must still
    block via `guard_checkpoint_advancement` -- disabling that guard's
    `raise` would make this exact assertion go red while step 1 alone stays
    green (AC8 mutation-test target)."""
    observed = _collector_result(source_status="complete", provenance={"sessions_read": 1})
    coverage = rr.compute_source_coverage_map(["claude_code"], {"claude_code": observed})
    assert all(coverage[s]["status"] == "observed" for s in ("claude_code",)), "precondition: step 1 alone would pass"
    disposition = rr.compute_checkpoint_disposition(
        required_sources=["claude_code"],
        source_coverage=coverage,
        prior_watermark=_PRIOR_WATERMARK_BOTH_SOURCES,
        publish_authorized=True,
    )
    assert disposition["checkpoint_advanced"] is False
    assert disposition["checkpoint_advance_reason"] == "blocked_missing_required_source"


# ---------------------------------------------------------------------------
# AC6 / AC8 first_run_no_publication: Verification Scenario E
# ---------------------------------------------------------------------------


def test_ac6_ac8_scenario_e_first_run_no_publication_blocked_explicit():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={"claude_code": zero_result, "claude_gpt": zero_result},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    assert result["checkpoint"]["checkpoint_advanced"] is False
    assert result["checkpoint"]["checkpoint_advance_reason"] == "blocked_no_publish_authorization"
    # never misreported as an already-durable checkpoint
    assert result["checkpoint"]["checkpoint_advance_reason"] != "first_run_no_prior_state"


def test_ac6_first_run_with_authorization_advances():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code"],
        collector_results={"claude_code": zero_result},
        prior_watermark=None,
        publish_authorized=True,
        clock=_clock,
    )
    assert result["checkpoint"] == {
        "checkpoint_advanced": True,
        "checkpoint_advance_reason": "first_run_no_prior_state",
    }


def test_ac6_no_new_sessions_selected_when_prior_state_exists():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code"],
        collector_results={"claude_code": zero_result},
        prior_watermark=_PRIOR_WATERMARK_CLAUDE_CODE_ONLY,
        publish_authorized=True,
        clock=_clock,
    )
    assert result["checkpoint"] == {
        "checkpoint_advanced": True,
        "checkpoint_advance_reason": "no_new_sessions_selected",
    }


# ---------------------------------------------------------------------------
# AC7: orchestration success is structurally independent of analysis_completeness
# ---------------------------------------------------------------------------


def test_ac7_orchestration_succeeded_even_when_analysis_completeness_unavailable():
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code", "claude_gpt"],
        collector_results={},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    assert result["orchestration"] == {"status": "succeeded", "reason_code": None}
    assert result["analysis_completeness"] == "unavailable"


def test_ac7_run_since_last_retrospective_cli_never_raises_reports_orchestration_failed(tmp_path):
    # repo_root is deliberately NOT a git checkout -- manual_trigger_preflight raises ValueError
    # internally; run_since_last_retrospective_cli must catch it and return, never propagate.
    result = rr.run_since_last_retrospective_cli(
        repo_root=tmp_path,
        required_sources=["claude_code"],
        env={},
        clock=_clock,
    )
    assert result["orchestration"]["status"] == "failed"
    assert result["orchestration"]["reason_code"] is not None
    assert result["analysis_completeness"] == "unavailable"
    rr.validate_session_window_coverage(result)


# ---------------------------------------------------------------------------
# schema validity (AC10): forcing invariants are real, not decorative
# ---------------------------------------------------------------------------


def test_schema_valid_full_envelope_round_trip():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code"],
        collector_results={"claude_code": zero_result},
        prior_watermark=None,
        publish_authorized=True,
        clock=_clock,
    )
    rr.validate_session_window_coverage(result)  # must not raise


def test_schema_rejects_observed_with_non_null_reason_code():
    zero_result = _collector_result(source_status="complete", provenance={"sessions_read": 0})
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code"],
        collector_results={"claude_code": zero_result},
        prior_watermark=None,
        publish_authorized=True,
        clock=_clock,
    )
    result["source_coverage"]["claude_code"]["reason_code"] = "should_not_be_allowed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        rr.validate_session_window_coverage(result)


def test_schema_rejects_unavailable_with_non_null_selected_session_count():
    result = rr.build_session_window_coverage_result(
        required_sources=["claude_code"],
        collector_results={},
        prior_watermark=None,
        publish_authorized=False,
        clock=_clock,
    )
    result["source_coverage"]["claude_code"]["selected_session_count"] = 0
    with pytest.raises(jsonschema.exceptions.ValidationError):
        rr.validate_session_window_coverage(result)


# ---------------------------------------------------------------------------
# collector-config resolution helpers (deterministic, env-based wiring)
# ---------------------------------------------------------------------------


def test_default_claude_code_sessions_dir_env_override_takes_precedence(tmp_path):
    override = tmp_path / "custom-sessions"
    resolved = rr.default_claude_code_sessions_dir(
        {rr._CLAUDE_CODE_SESSIONS_DIR_ENV: str(override)}, repo_root=tmp_path
    )
    assert resolved == override


def test_default_claude_code_sessions_dir_falls_back_to_home_convention(tmp_path):
    resolved = rr.default_claude_code_sessions_dir({"HOME": str(tmp_path)}, repo_root=tmp_path)
    expected_slug = str(tmp_path.resolve()).replace("/", "-")
    assert resolved == tmp_path / ".claude" / "projects" / expected_slug


def test_default_claude_code_sessions_dir_none_when_home_unset(tmp_path):
    assert rr.default_claude_code_sessions_dir({}, repo_root=tmp_path) is None


def test_default_claude_gpt_hook_sink_path_none_when_unset():
    assert rr.default_claude_gpt_hook_sink_path({}) is None


def test_default_claude_gpt_hook_sink_path_env_override():
    resolved = rr.default_claude_gpt_hook_sink_path({rr._CLAUDE_GPT_HOOK_SINK_PATH_ENV: "/tmp/sink.jsonl"})
    assert resolved == Path("/tmp/sink.jsonl")


def test_resolve_claude_code_session_paths_missing_dir_returns_empty(tmp_path):
    assert rr.resolve_claude_code_session_paths(tmp_path / "does-not-exist") == []


def test_resolve_claude_code_session_paths_sorted(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "b.jsonl").write_text('{"type": "user"}\n', encoding="utf-8")
    (sessions_dir / "a.jsonl").write_text('{"type": "assistant"}\n', encoding="utf-8")
    paths = rr.resolve_claude_code_session_paths(sessions_dir)
    assert [p.name for p in paths] == ["a.jsonl", "b.jsonl"]


# ---------------------------------------------------------------------------
# end-to-end: collect_session_sources / run_since_last_retrospective_cli
# against real collect_snapshot.py functions (no mocking of the collector
# layer itself)
# ---------------------------------------------------------------------------


def test_collect_session_sources_unwired_when_env_unresolvable(tmp_path):
    results = rr.collect_session_sources(
        required_sources=["claude_code", "claude_gpt"],
        env={},
        repo_root=tmp_path,
        run_nonce="nonce-1",
        clock=_clock,
    )
    assert results == {"claude_code": None, "claude_gpt": None}


def test_collect_session_sources_wires_real_claude_code_collector(tmp_path):
    slug = str(tmp_path.resolve()).replace("/", "-")
    sessions_dir = tmp_path / ".claude" / "projects" / slug
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session1.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": "s1"}) + "\n", encoding="utf-8"
    )
    results = rr.collect_session_sources(
        required_sources=["claude_code"],
        env={"HOME": str(tmp_path)},
        repo_root=tmp_path,
        run_nonce="nonce-1",
        clock=_clock,
    )
    assert results["claude_code"] is not None
    assert results["claude_code"].observation["source_status"] == "complete"


def test_run_since_last_retrospective_cli_end_to_end_degraded_when_unwired(tmp_path):
    (tmp_path / ".git").mkdir()
    result = rr.run_since_last_retrospective_cli(
        repo_root=tmp_path,
        required_sources=["claude_code", "claude_gpt"],
        env={},
        publish_authorized=True,
        clock=_clock,
    )
    assert result["orchestration"]["status"] == "succeeded"
    assert result["analysis_completeness"] == "unavailable"
    rr.validate_session_window_coverage(result)


def test_run_since_last_retrospective_cli_end_to_end_complete_with_real_session_files(tmp_path):
    (tmp_path / ".git").mkdir()
    slug = str(tmp_path.resolve()).replace("/", "-")
    sessions_dir = tmp_path / ".claude" / "projects" / slug
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "session1.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": "s1"}) + "\n", encoding="utf-8"
    )
    result = rr.run_since_last_retrospective_cli(
        repo_root=tmp_path,
        required_sources=["claude_code"],
        env={"HOME": str(tmp_path)},
        publish_authorized=True,
        clock=_clock,
    )
    assert result["analysis_completeness"] == "complete"
    assert result["source_coverage"]["claude_code"]["status"] == "observed"
    assert result["checkpoint"]["checkpoint_advanced"] is True
    rr.validate_session_window_coverage(result)


# ---------------------------------------------------------------------------
# CLI argparse wiring (main())
# ---------------------------------------------------------------------------


def test_main_since_last_retrospective_flag_bypasses_required_target_issue_args(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    exit_code = rr.main(
        [
            "--repo-root",
            str(tmp_path),
            "--since-last-retrospective",
            "--session-sources",
            "claude_code",
        ]
    )
    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["schema_version"] == "session_window_coverage/v1"


def test_main_default_mode_still_requires_target_issue_args():
    with pytest.raises(SystemExit):
        rr.main(["--repo-root", str(_SKILL_DIR)])
