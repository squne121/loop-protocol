"""
test_evidence_policy_authority_v2.py

Issue #1856 (Phase 1: evidence authority cutover) destination-state tests.

AC3: pr-review-judge の evidence-policy が CI_CHECK_RUN_SCOPED（current-head,
     expected_head_sha/check_run_id 束縛）と exact head SHA + literal command
     SHA256 束縛の独立実行 Issue VC を authoritative とし、TEST_VERDICT_MACHINE
     を advisory へ明示的に降格することを、evidence-policy.md の構造検証で
     確認する。
AC4: CI_CHECK_RUN_SCOPED の missing / skipped / neutral / cancelled /
     stale-head / unknown-classification は、Phase 1 変更後も引き続き
     fail-closed (blocking) であることを、ci_verdict_summary_v2 の実装に
     対する挙動テストで確認する。
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

_POLICY_MD = pathlib.Path(__file__).parents[2] / "references" / "evidence-policy.md"
_SCRIPT = pathlib.Path(__file__).parent.parent / "ci_verdict_summary_v2.py"

EXPECTED_SHA = "abc1234def5678900000000000000000000000000"
STALE_SHA = "999aaabbbccc0000000000000000000000000000"


def _load_module(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v2() -> types.ModuleType:
    return _load_module(_SCRIPT)


def _policy_text() -> str:
    assert _POLICY_MD.is_file(), f"evidence-policy.md not found at {_POLICY_MD}"
    return _POLICY_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC3: authoritative sources are CI_CHECK_RUN_SCOPED + bound issue VC
# ---------------------------------------------------------------------------


def test_authoritative_sources_are_ci_check_run_and_bound_issue_vc():
    text = _policy_text()

    assert "CI_CHECK_RUN_SCOPED" in text
    assert "独立実行 Issue VC" in text
    assert "authoritative" in text

    # TEST_VERDICT_MACHINE must be explicitly demoted to advisory /
    # non-authoritative, not merely absent from the document.
    assert "TEST_VERDICT_MACHINE" in text
    assert "advisory" in text
    assert "non-authoritative" in text

    # The historical "最上位" (top priority) framing for TEST_VERDICT_MACHINE
    # must not remain — that was the pre-cutover priority ordering.
    assert "TEST_VERDICT_MACHINE（最上位）" not in text
    assert "TEST_VERDICT_MACHINE(最上位)" not in text


def test_evidence_policy_ci_check_run_scoped_bound_to_head_sha_and_check_run_id():
    text = _policy_text()
    assert "expected_head_sha" in text
    assert "check_run_id" in text


def test_evidence_policy_bound_issue_vc_requires_exact_sha_and_command_sha256():
    text = _policy_text()
    assert "head SHA" in text
    assert "command SHA256" in text


# ---------------------------------------------------------------------------
# AC4: missing/skipped/neutral/cancelled/stale/unknown remain fail-closed
# ---------------------------------------------------------------------------


def _make_check(**overrides) -> dict:
    base = {
        "name": "python-test",
        "workflow": "ci",
        "check_run_id": 12345,
        "status": "completed",
        "conclusion": "success",
        "classification": "required",
        "head_sha": EXPECTED_SHA,
        "head_sha_match": True,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "scenario,check_kwargs",
    [
        (
            "missing_required_artifact",
            {"check_run_id": None, "status": None, "conclusion": None},
        ),
        (
            "skipped",
            {"conclusion": "skipped"},
        ),
        (
            "neutral",
            {"conclusion": "neutral"},
        ),
        (
            "cancelled",
            {"conclusion": "cancelled"},
        ),
        (
            "stale_head",
            {"head_sha": STALE_SHA, "head_sha_match": False},
        ),
        (
            "unknown_classification",
            {"classification": "unknown"},
        ),
    ],
)
def test_missing_skipped_neutral_cancelled_stale_unknown_still_fail_closed(
    v2, scenario, check_kwargs
):
    check = _make_check(**check_kwargs)
    blocking, failure_reason = v2.determine_check_verdict(check, EXPECTED_SHA)
    assert blocking is True, (
        f"[{scenario}] Expected blocking=True (fail-closed), got False. "
        f"failure_reason={failure_reason!r}"
    )
    assert failure_reason != "none", (
        f"[{scenario}] Expected a non-'none' failure_reason, got 'none'"
    )


def test_compute_overall_status_blocks_when_any_required_check_is_not_clean(v2):
    checks = [v2.build_check_entry(
        {
            "name": "python-test",
            "conclusion": "skipped",
            "status": "completed",
            "id": 999,
        },
        "ci",
        EXPECTED_SHA,
    )]
    overall_status, next_action = v2.compute_overall_status(checks, EXPECTED_SHA, EXPECTED_SHA)
    assert overall_status != "merge_ready", (
        f"Expected non-merge_ready overall_status for a skipped required check, "
        f"got {overall_status!r} (next_action={next_action!r})"
    )
