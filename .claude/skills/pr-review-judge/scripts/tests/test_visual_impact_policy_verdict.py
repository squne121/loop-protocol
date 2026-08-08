"""test_visual_impact_policy_verdict.py (Issue #2019 AC22)

GIVEN/WHEN/THEN tests proving the `visual-impact-policy` CheckRun resolves
to the correct verdict across success / failure / missing / skipped /
cancelled / stale-head / missing-CheckRun-ID / wrong-GitHub-App /
unknown-source cases, reusing ci_verdict_summary_v2.py's classifier (now
registered as `("ci", "visual-impact-policy"): "required"`).
"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

_SCRIPT = pathlib.Path(__file__).parent.parent / "ci_verdict_summary_v2.py"


def _load_module(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2_visual_impact", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v2() -> types.ModuleType:
    return _load_module(_SCRIPT)


EXPECTED_SHA = "abc1234def5678900000000000000000000000000"
OTHER_SHA = "999aaabbbccc0000000000000000000000000000"


def make_visual_impact_check(**overrides) -> dict:
    base = {
        "name": "visual-impact-policy",
        "workflow": "ci",
        "status": "completed",
        "conclusion": "success",
        "head_sha": EXPECTED_SHA,
        "check_run_id": 55501,
    }
    base.update(overrides)
    return base


def test_visual_impact_policy_is_registered_as_required(v2):
    assert v2.get_classification("ci", "visual-impact-policy") == "required"
    assert ("ci", "visual-impact-policy") in v2.REQUIRED_CHECKS


def test_success_at_current_head_does_not_block(v2):
    check = v2.build_check_entry(make_visual_impact_check(), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is False
    assert check["failure_reason"] == "none"


def test_failure_blocks(v2):
    check = v2.build_check_entry(make_visual_impact_check(conclusion="failure"), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is True
    assert check["failure_reason"] == "failed"


def test_missing_check_run_blocks_as_no_required_evidence(v2):
    """No visual-impact-policy row at all -> overall_status must never be
    merge_ready (missing required evidence)."""
    artifact = v2.generate_verdict(
        expected_head_sha=EXPECTED_SHA,
        pr_head_sha=EXPECTED_SHA,
        repository="owner/repo",
        workflow_run_id=1,
        workflow_run_attempt=1,
        event_name="pull_request",
        raw_checks=[],
    )
    assert artifact["overall_status"] != "merge_ready"


def test_skipped_blocks_required_check(v2):
    check = v2.build_check_entry(make_visual_impact_check(conclusion="skipped"), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is True
    assert check["failure_reason"] == "skipped_required"


def test_cancelled_blocks(v2):
    check = v2.build_check_entry(make_visual_impact_check(conclusion="cancelled"), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is True
    assert check["failure_reason"] == "cancelled_current_head"


def test_stale_head_blocks(v2):
    check = v2.build_check_entry(make_visual_impact_check(head_sha=OTHER_SHA), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is True
    assert check["failure_reason"] == "stale_head_sha"


def test_missing_check_run_id_blocks_as_gh_error(v2):
    check = v2.build_check_entry(make_visual_impact_check(check_run_id=None), "ci", EXPECTED_SHA)
    assert check["blocking_merge_ready"] is True
    assert check["failure_reason"] == "gh_error"


def test_wrong_github_app_source_is_never_accepted_as_evidence(v2):
    """A CheckRun row named 'visual-impact-policy' but reported by a
    DIFFERENT GitHub App (spoofed name) must never be treated as evidence."""
    payload = {
        "check_runs": [
            {
                "id": 77001,
                "name": "visual-impact-policy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": EXPECTED_SHA,
                "details_url": "https://github.com/owner/repo/actions/runs/123/job/1",
                "app": {"slug": "some-other-app"},
            }
        ]
    }
    with pytest.raises(ValueError, match="no_current_workflow_evidence"):
        v2.check_runs_api_to_raw_checks(payload, workflow_run_id=123)


def test_unknown_source_missing_app_field_is_never_accepted_as_evidence(v2):
    payload = {
        "check_runs": [
            {
                "id": 77002,
                "name": "visual-impact-policy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": EXPECTED_SHA,
                "details_url": "https://github.com/owner/repo/actions/runs/123/job/1",
                # no "app" key at all -- unknown source.
            }
        ]
    }
    with pytest.raises(ValueError, match="no_current_workflow_evidence"):
        v2.check_runs_api_to_raw_checks(payload, workflow_run_id=123)


def test_trusted_github_actions_app_source_is_accepted(v2):
    payload = {
        "check_runs": [
            {
                "id": 77003,
                "name": "visual-impact-policy",
                "status": "completed",
                "conclusion": "success",
                "head_sha": EXPECTED_SHA,
                "details_url": "https://github.com/owner/repo/actions/runs/123/job/1",
                "app": {"slug": "github-actions"},
            }
        ]
    }
    raw_checks = v2.check_runs_api_to_raw_checks(payload, workflow_run_id=123)
    assert len(raw_checks) == 1
    assert raw_checks[0]["name"] == "visual-impact-policy"
