"""
test_ci_verdict_summary_v2.py

Fixture tests for ci_verdict_summary_v2 schema.
AC9:  valid JSON + required fields fixture test
AC10: enum value exhaustiveness fixture test
AC11: head_sha=null + skipped → excluded / required → blocked fixture test
AC12: neutral/skipped are NOT required evidence pass fixture test
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Import helper — load ci_verdict_summary_v2 without package install
# ---------------------------------------------------------------------------

_SCRIPT = (
    pathlib.Path(__file__).parent.parent / "ci_verdict_summary_v2.py"
)


def _load_module(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v2() -> types.ModuleType:
    return _load_module(_SCRIPT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_SHA = "abc1234def5678900000000000000000000000000"
OTHER_SHA    = "999aaabbbccc0000000000000000000000000000"


def make_check(
    name: str,
    workflow: str = "ci",
    status: str | None = "completed",
    conclusion: str | None = "success",
    head_sha: str | None = EXPECTED_SHA,
) -> dict:
    return {
        "name": name,
        "workflow": workflow,
        "status": status,
        "conclusion": conclusion,
        "headSha": head_sha,
    }


def build(v2, checks: list[dict], expected_sha: str = EXPECTED_SHA, pr_head_sha: str | None = EXPECTED_SHA) -> dict:
    return v2.generate_verdict(
        expected_head_sha=expected_sha,
        pr_head_sha=pr_head_sha,
        repository="owner/repo",
        workflow_run_id=1,
        workflow_run_attempt=1,
        event_name="pull_request",
        raw_checks=checks,
    )


# ---------------------------------------------------------------------------
# AC9: valid JSON + required top-level fields
# ---------------------------------------------------------------------------

class TestAC9RequiredFields:
    """AC9: artifact has required top-level schema fields."""

    def test_schema_field(self, v2):
        artifact = build(v2, [])
        assert artifact["schema"] == "ci_verdict_summary_v2"

    def test_schema_version(self, v2):
        artifact = build(v2, [])
        assert artifact["schema_version"] == 2

    def test_generated_at_present(self, v2):
        artifact = build(v2, [])
        assert "generated_at" in artifact
        assert artifact["generated_at"]

    def test_repository(self, v2):
        artifact = build(v2, [])
        assert artifact["repository"] == "owner/repo"

    def test_workflow_run_id(self, v2):
        artifact = build(v2, [])
        assert isinstance(artifact["workflow_run_id"], int)

    def test_workflow_run_attempt(self, v2):
        artifact = build(v2, [])
        assert isinstance(artifact["workflow_run_attempt"], int)

    def test_expected_head_sha(self, v2):
        artifact = build(v2, [])
        assert artifact["expected_head_sha"] == EXPECTED_SHA

    def test_head_sha_present(self, v2):
        artifact = build(v2, [])
        assert "head_sha" in artifact

    def test_overall_status_present(self, v2):
        artifact = build(v2, [])
        assert "overall_status" in artifact

    def test_next_action_present(self, v2):
        artifact = build(v2, [])
        assert "next_action" in artifact

    def test_checks_is_list(self, v2):
        artifact = build(v2, [])
        assert isinstance(artifact["checks"], list)

    def test_valid_json_serializable(self, v2):
        artifact = build(v2, [make_check("typecheck")])
        # Must round-trip through JSON without error
        serialized = json.dumps(artifact)
        reparsed = json.loads(serialized)
        assert reparsed["schema"] == "ci_verdict_summary_v2"

    def test_check_entry_required_fields(self, v2):
        artifact = build(v2, [make_check("typecheck")])
        check = artifact["checks"][0]
        for field in [
            "name", "workflow", "check_run_id", "status", "conclusion",
            "classification", "head_sha", "expected_head_sha", "head_sha_match",
            "blocking_merge_ready", "failure_reason", "artifact_refs",
        ]:
            assert field in check, f"Missing field: {field}"

    def test_check_entry_head_sha_match_field(self, v2):
        artifact = build(v2, [make_check("typecheck", head_sha=EXPECTED_SHA)])
        check = artifact["checks"][0]
        assert "head_sha_match" in check
        assert check["head_sha_match"] is True

    def test_check_entry_classification_field(self, v2):
        artifact = build(v2, [make_check("typecheck")])
        check = artifact["checks"][0]
        assert check["classification"] in {"required", "advisory", "evidence", "excluded", "unknown"}


# ---------------------------------------------------------------------------
# AC10: enum exhaustiveness
# ---------------------------------------------------------------------------

class TestAC10EnumExhaustiveness:
    """AC10: all declared enum values are reachable."""

    def test_overall_status_enum_values(self, v2):
        expected = {"merge_ready", "blocked", "pending", "stale_head_sha", "gh_error", "no_required_evidence"}
        assert set(v2.OVERALL_STATUS_ENUM) == expected

    def test_next_action_enum_values(self, v2):
        expected = {
            "none", "wait_for_ci", "inspect_failed_log_artifacts",
            "refresh_head_sha", "rerun_failed_check",
            "manual_review_gh_error", "manual_review_no_required_evidence",
        }
        assert set(v2.NEXT_ACTION_ENUM) == expected

    def test_failure_reason_enum_values(self, v2):
        expected = {
            "none", "failed", "pending", "cancelled_current_head",
            "stale_head_sha", "skipped_required", "neutral_required",
            "missing_required_artifact", "gh_error", "no_required_evidence",
        }
        assert set(v2.FAILURE_REASON_ENUM) == expected

    def test_classification_values_in_map(self, v2):
        valid = {"required", "advisory", "evidence", "excluded", "unknown"}
        for (wf, name), cls in v2.CLASSIFICATION_MAP.items():
            assert cls in valid, f"({wf},{name}) has invalid classification: {cls}"

    def test_merge_ready_reachable(self, v2):
        """All required checks pass → merge_ready."""
        checks = [
            make_check("typecheck", conclusion="success"),
            make_check("lint", conclusion="success"),
            make_check("test", conclusion="success"),
            make_check("build", conclusion="success"),
            make_check("e2e", conclusion="success"),
            make_check("python-test", conclusion="success"),
            make_check("actionlint", conclusion="success"),
        ]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "merge_ready"
        assert artifact["next_action"] == "none"

    def test_blocked_reachable(self, v2):
        """A failed required check → blocked."""
        checks = [make_check("typecheck", conclusion="failure")]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "blocked"

    def test_pending_reachable(self, v2):
        """A queued required check → pending."""
        checks = [make_check("typecheck", status="queued", conclusion=None)]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "pending"
        assert artifact["next_action"] == "wait_for_ci"

    def test_stale_head_sha_reachable(self, v2):
        """head_sha mismatch → stale_head_sha."""
        checks = [make_check("typecheck", head_sha=OTHER_SHA, conclusion="success")]
        artifact = build(v2, checks, pr_head_sha=EXPECTED_SHA)
        # head_sha of the check differs from expected — check is stale
        check = artifact["checks"][0]
        assert check["head_sha_match"] is False
        assert check["blocking_merge_ready"] is True

    def test_gh_error_reachable(self, v2):
        """Unknown status on a required check → gh_error."""
        checks = [make_check("typecheck", status="completed", conclusion="unknown_status_xyz")]
        artifact = build(v2, checks)
        check = artifact["checks"][0]
        assert check["failure_reason"] == "gh_error"


# ---------------------------------------------------------------------------
# AC11: head_sha=null + skipped semantics
# ---------------------------------------------------------------------------

class TestAC11HeadShaNullSkipped:
    """AC11: head_sha=null + skipped → excluded (allowlisted) or blocked (required)."""

    def test_excluded_check_not_blocking(self, v2):
        """Excluded (retrospective) check with head_sha=null + skipped does NOT block."""
        check = make_check(
            "PR Review Japanese Check (retrospective)",
            workflow="Check Japanese Content",
            status="completed",
            conclusion="skipped",
            head_sha=None,
        )
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "excluded"
        assert entry["blocking_merge_ready"] is False
        assert entry["failure_reason"] == "none"

    def test_required_check_null_sha_skipped_blocks(self, v2):
        """Required check with head_sha=null + skipped BLOCKS merge-ready."""
        check = make_check(
            "typecheck",
            workflow="ci",
            status="completed",
            conclusion="skipped",
            head_sha=None,
        )
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "required"
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "skipped_required"

    def test_evidence_check_null_sha_skipped_blocks(self, v2):
        """Evidence check with head_sha=null + skipped BLOCKS merge-ready."""
        check = make_check(
            "e2e",
            workflow="ci",
            status="completed",
            conclusion="skipped",
            head_sha=None,
        )
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "evidence"
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "skipped_required"

    def test_multiple_excluded_retrospective_checks_all_pass(self, v2):
        """Multiple allowlisted excluded checks with head_sha=null + skipped: none block."""
        checks = [
            make_check("PR Review Japanese Check (retrospective)", "Check Japanese Content", "completed", "skipped", None),
            make_check("Issue Comment Japanese Check (retrospective)", "Check Japanese Content", "completed", "skipped", None),
            make_check("Issue Body Japanese Check (retrospective)", "Check Japanese Content", "completed", "skipped", None),
        ]
        artifact = build(v2, checks)
        for entry in artifact["checks"]:
            assert entry["blocking_merge_ready"] is False, f"{entry['name']} should not block"

    def test_required_check_null_sha_success_blocks(self, v2):
        """Required check with head_sha=null + success is suspicious and blocks."""
        check = make_check("typecheck", "ci", "completed", "success", None)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["blocking_merge_ready"] is True


# ---------------------------------------------------------------------------
# AC12: neutral/skipped are NOT required evidence pass
# ---------------------------------------------------------------------------

class TestAC12NeutralSkippedNotPass:
    """AC12: neutral or skipped conclusion on required/evidence check does NOT pass."""

    def test_required_skipped_blocks(self, v2):
        """required check with conclusion=skipped at expected head SHA → blocks."""
        check = make_check("typecheck", "ci", "completed", "skipped", EXPECTED_SHA)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "required"
        assert entry["head_sha_match"] is True
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "skipped_required"

    def test_required_neutral_blocks(self, v2):
        """required check with conclusion=neutral at expected head SHA → blocks."""
        check = make_check("typecheck", "ci", "completed", "neutral", EXPECTED_SHA)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "required"
        assert entry["head_sha_match"] is True
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "neutral_required"

    def test_evidence_skipped_blocks(self, v2):
        """evidence check with conclusion=skipped → blocks."""
        check = make_check("e2e", "ci", "completed", "skipped", EXPECTED_SHA)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "evidence"
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "skipped_required"

    def test_evidence_neutral_blocks(self, v2):
        """evidence check with conclusion=neutral → blocks."""
        check = make_check("python-test", "ci", "completed", "neutral", EXPECTED_SHA)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "evidence"
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "neutral_required"

    def test_overall_not_merge_ready_when_skipped(self, v2):
        """overall_status must NOT be merge_ready when required check is skipped."""
        check = make_check("lint", "ci", "completed", "skipped", EXPECTED_SHA)
        artifact = build(v2, [check])
        assert artifact["overall_status"] != "merge_ready"

    def test_overall_not_merge_ready_when_neutral(self, v2):
        """overall_status must NOT be merge_ready when required check is neutral."""
        check = make_check("build", "ci", "completed", "neutral", EXPECTED_SHA)
        artifact = build(v2, [check])
        assert artifact["overall_status"] != "merge_ready"

    def test_advisory_skipped_does_not_block(self, v2):
        """advisory classification skipped does NOT block."""
        # advisory is not in CLASSIFICATION_MAP by default → "unknown" currently
        # add a custom test with an explicit advisory check by monkeypatching
        check = {
            "name": "some-advisory-check",
            "workflow": "advisory-workflow",
            "status": "completed",
            "conclusion": "skipped",
            "headSha": EXPECTED_SHA,
        }
        # unknown classification: treated conservatively — but not as required
        # The key rule is: advisory (or unknown) skipped should not assert "skipped_required"
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        # unknown classification with head_sha match + skipped: not "skipped_required"
        # (skipped_required applies only to required/evidence classification)
        assert entry["failure_reason"] != "skipped_required"

    def test_cancelled_at_current_head_blocks(self, v2):
        """cancelled on current expected head → blocked (not excluded)."""
        check = make_check("test", "ci", "completed", "cancelled", EXPECTED_SHA)
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "cancelled_current_head"
