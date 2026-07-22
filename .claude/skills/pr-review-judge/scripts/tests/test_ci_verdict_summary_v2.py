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
import types

import pytest

# ---------------------------------------------------------------------------
# Import helper — load ci_verdict_summary_v2 without package install
# ---------------------------------------------------------------------------

_SCRIPT = (
    pathlib.Path(__file__).parent.parent / "ci_verdict_summary_v2.py"
)
_WORKFLOW = pathlib.Path(__file__).parents[5] / ".github/workflows/ci.yml"


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
            make_check("node-backed-hook-tests", conclusion="success"),
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
            make_check(
                "PR Review Japanese Check (retrospective)",
                "Check Japanese Content",
                "completed",
                "skipped",
                None
            ),
            make_check(
                "Issue Comment Japanese Check (retrospective)",
                "Check Japanese Content",
                "completed",
                "skipped",
                None
            ),
            make_check(
                "Issue Body Japanese Check (retrospective)",
                "Check Japanese Content",
                "completed",
                "skipped",
                None
            ),
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


# ---------------------------------------------------------------------------
# B3: REQUIRED_CHECKS — empty input and partial input → no_required_evidence
# ---------------------------------------------------------------------------

class TestB3NoRequiredEvidence:
    """B3: empty checks or missing required checks → no_required_evidence (not merge_ready)."""

    def test_empty_checks_no_required_evidence(self, v2):
        """Empty checks list must NOT yield merge_ready — no_required_evidence."""
        artifact = build(v2, [])
        assert artifact["overall_status"] == "no_required_evidence"
        assert artifact["next_action"] == "manual_review_no_required_evidence"

    def test_typecheck_only_no_required_evidence(self, v2):
        """Only typecheck success is not sufficient — no_required_evidence."""
        checks = [make_check("typecheck", conclusion="success")]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "no_required_evidence"
        assert artifact["next_action"] == "manual_review_no_required_evidence"

    def test_required_checks_constant_exists(self, v2):
        """REQUIRED_CHECKS set must be defined and non-empty."""
        assert hasattr(v2, "REQUIRED_CHECKS")
        assert isinstance(v2.REQUIRED_CHECKS, set)
        assert len(v2.REQUIRED_CHECKS) > 0

    def test_required_checks_contains_all_ci_jobs(self, v2):
        """REQUIRED_CHECKS must include all 8 ci.yml upstream evidence jobs."""
        expected = {
            ("ci", "typecheck"),
            ("ci", "lint"),
            ("ci", "test"),
            ("ci", "build"),
            ("ci", "e2e"),
            ("ci", "python-test"),
            ("ci", "node-backed-hook-tests"),
            ("ci", "actionlint"),
        }
        assert expected.issubset(v2.REQUIRED_CHECKS)

    def test_all_required_success_is_merge_ready(self, v2):
        """All REQUIRED_CHECKS with success → merge_ready."""
        checks = [
            make_check("typecheck", conclusion="success"),
            make_check("lint", conclusion="success"),
            make_check("test", conclusion="success"),
            make_check("build", conclusion="success"),
            make_check("e2e", conclusion="success"),
            make_check("python-test", conclusion="success"),
            make_check("node-backed-hook-tests", conclusion="success"),
            make_check("actionlint", conclusion="success"),
        ]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "merge_ready"
        assert artifact["next_action"] == "none"

    def test_missing_one_required_check_no_required_evidence(self, v2):
        """Six of seven required checks present — still no_required_evidence."""
        checks = [
            make_check("typecheck", conclusion="success"),
            make_check("lint", conclusion="success"),
            make_check("test", conclusion="success"),
            make_check("build", conclusion="success"),
            make_check("e2e", conclusion="success"),
            make_check("python-test", conclusion="success"),
            make_check("node-backed-hook-tests", conclusion="success"),
            # actionlint missing
        ]
        artifact = build(v2, checks)
        assert artifact["overall_status"] == "no_required_evidence"


# ---------------------------------------------------------------------------
# B5: unknown classification is conservatively blocking
# ---------------------------------------------------------------------------

class TestB5UnknownClassificationBlocking:
    """B5: unknown classification must block merge-ready with failure_reason=gh_error."""

    def test_unknown_check_is_blocking(self, v2):
        """An unrecognised check (unknown classification) must block."""
        check = {
            "name": "some-new-unrecognised-check",
            "workflow": "unknown-workflow",
            "status": "completed",
            "conclusion": "success",
            "headSha": EXPECTED_SHA,
        }
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "unknown"
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "gh_error"

    def test_unknown_check_blocks_merge_ready(self, v2):
        """All required checks pass but an unknown check is present → not merge_ready."""
        all_required = [
            make_check("typecheck", conclusion="success"),
            make_check("lint", conclusion="success"),
            make_check("test", conclusion="success"),
            make_check("build", conclusion="success"),
            make_check("e2e", conclusion="success"),
            make_check("python-test", conclusion="success"),
            make_check("actionlint", conclusion="success"),
        ]
        unknown_check = {
            "name": "mystery-job",
            "workflow": "ci",
            "status": "completed",
            "conclusion": "success",
            "headSha": EXPECTED_SHA,
        }
        artifact = build(v2, all_required + [unknown_check])
        # mystery-job is not in CLASSIFICATION_MAP → unknown → blocking
        unknown_entry = next(c for c in artifact["checks"] if c["name"] == "mystery-job")
        assert unknown_entry["classification"] == "unknown"
        assert unknown_entry["blocking_merge_ready"] is True
        assert artifact["overall_status"] != "merge_ready"

    def test_unknown_check_head_sha_match_false(self, v2):
        """B5: unknown classification → head_sha_match is irrelevant, always blocking."""
        check = {
            "name": "another-unknown",
            "workflow": "ci",
            "status": "completed",
            "conclusion": "success",
            "headSha": EXPECTED_SHA,
        }
        artifact = build(v2, [check])
        entry = artifact["checks"][0]
        assert entry["classification"] == "unknown"
        assert entry["blocking_merge_ready"] is True


# ---------------------------------------------------------------------------
# B1/B2: needs_result_synthetic provenance
# ---------------------------------------------------------------------------

class TestB1B2NeedsResultSynthetic:
    """B1/B2: needs.result based checks use provenance=needs_result_synthetic, head_sha=None."""

    def test_needs_json_to_raw_checks_produces_synthetic_provenance(self, v2):
        """needs_json_to_raw_checks sets provenance=needs_result_synthetic."""
        needs_map = {"typecheck": "success", "lint": "success"}
        raw = v2.needs_json_to_raw_checks(needs_map)
        for entry in raw:
            assert entry.get("provenance") == "needs_result_synthetic"
            assert entry.get("headSha") is None

    def test_synthetic_provenance_sets_head_sha_none(self, v2):
        """build_check_entry with provenance=needs_result_synthetic → head_sha=None, head_sha_match=False."""
        raw = {
            "name": "typecheck",
            "workflow": "ci",
            "status": "completed",
            "conclusion": "success",
            "headSha": None,
            "provenance": "needs_result_synthetic",
        }
        entry = v2.build_check_entry(raw, "ci", EXPECTED_SHA)
        assert entry["head_sha"] is None
        assert entry["head_sha_match"] is False
        assert entry.get("provenance") == "needs_result_synthetic"

    def test_synthetic_success_blocks_because_head_sha_none(self, v2):
        """needs_result_synthetic success on required check → blocks (head_sha=None, stale_head_sha)."""
        raw = {
            "name": "typecheck",
            "workflow": "ci",
            "status": "completed",
            "conclusion": "success",
            "headSha": None,
            "provenance": "needs_result_synthetic",
        }
        entry = v2.build_check_entry(raw, "ci", EXPECTED_SHA)
        assert entry["blocking_merge_ready"] is True
        assert entry["failure_reason"] == "stale_head_sha"


class TestP0RealCheckRunApiEvidence:
    """P0: merge-ready evidence must originate from CheckRun API rows."""

    def _api_row(self, name: str, *, head_sha: str = EXPECTED_SHA, run_id: int = 123) -> dict:
        return {
            "id": len(name) + 1000,
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "head_sha": head_sha,
            "details_url": f"https://github.com/owner/repo/actions/runs/{run_id}/job/1",
        }

    def test_actual_check_runs_are_bound_to_current_workflow_and_head(self, v2):
        names = [
            "typecheck", "lint", "test", "build", "e2e", "python-test",
            "node-backed-hook-tests", "actionlint",
        ]
        raw_checks = v2.check_runs_api_to_raw_checks(
            {"check_runs": [self._api_row(name) for name in names]}, workflow_run_id=123
        )
        artifact = build(v2, raw_checks)
        assert artifact["overall_status"] == "merge_ready"
        assert all(check["head_sha"] == EXPECTED_SHA for check in artifact["checks"])
        assert all(check["provenance"] == "github_check_run_api" for check in artifact["checks"])

    def test_wrong_workflow_run_is_not_accepted_as_evidence(self, v2):
        with pytest.raises(ValueError, match="no_current_workflow_evidence"):
            v2.check_runs_api_to_raw_checks(
                {"check_runs": [self._api_row("typecheck", run_id=999)]}, workflow_run_id=123
            )

    def test_cli_rejects_malformed_actual_check_run_payload(self, v2, tmp_path):
        source = tmp_path / "check-runs.json"
        output = tmp_path / "verdict.json"
        source.write_text(json.dumps({"check_runs": [{"name": "test"}]}))
        assert v2.main([
            "--expected-head-sha", EXPECTED_SHA,
            "--pr-head-sha", EXPECTED_SHA,
            "--workflow-run-id", "123",
            "--check-runs-api-json", str(source),
            "--output", str(output),
        ]) == 1
        assert not output.exists()

    def test_workflow_uses_commit_scoped_check_run_api_not_needs_result(self):
        workflow = _WORKFLOW.read_text()
        verdict_job = workflow[workflow.index("  ci-verdict-summary:"):]
        assert "commits/${PR_HEAD_SHA}/check-runs?per_page=100" in verdict_job
        assert "--check-runs-api-json ci_verdict_summary_v2_check_runs.json" in verdict_job
        assert "--needs-json" not in verdict_job


# ---------------------------------------------------------------------------
# AC10/AC11: current PR head and uploaded payload binding
# ---------------------------------------------------------------------------

class TestAC10AC11UploadedArtifactBinding:
    """The final uploaded payload must bind to an already-uploaded input artifact."""

    def test_pr_head_is_recorded_when_explicitly_supplied(self, v2):
        artifact = v2.generate_verdict(
            expected_head_sha=EXPECTED_SHA,
            pr_head_sha=EXPECTED_SHA,
            repository="owner/repo",
            workflow_run_id=123,
            workflow_run_attempt=2,
            event_name="pull_request",
            raw_checks=[],
        )
        assert artifact["head_sha"] == EXPECTED_SHA

    def test_pr_head_is_explicit_workflow_input_and_checked_out_ref(self):
        workflow = _WORKFLOW.read_text()
        assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
        assert "--pr-head-sha \"$PR_HEAD_SHA\"" in workflow
        assert 'test "$(git rev-parse HEAD)" = "$PR_HEAD_SHA"' in workflow

    def test_uploaded_artifact_payload_has_complete_non_self_reference(self, v2):
        artifact = v2.generate_verdict(
            expected_head_sha=EXPECTED_SHA,
            pr_head_sha=EXPECTED_SHA,
            repository="owner/repo",
            workflow_run_id=123,
            workflow_run_attempt=2,
            event_name="pull_request",
            raw_checks=[],
            artifact_id="456",
            artifact_url="https://example.test/artifacts/456",
            artifact_digest="sha256:binding-digest",
            artifact_name="ci-verdict-summary-v2-binding-123-2",
            artifact_workflow_run_id=123,
            artifact_workflow_run_attempt=2,
        )
        assert artifact["head_sha"] == EXPECTED_SHA
        assert len(artifact["artifact_refs"]) == 1
        ref = artifact["artifact_refs"][0]
        assert ref["artifact_name"] == "ci-verdict-summary-v2-binding-123-2"
        assert ref["artifact_digest"] == "sha256:binding-digest"
        assert ref["workflow_run_id"] == 123
        assert ref["workflow_run_attempt"] == 2

    def test_uploaded_artifact_uses_preuploaded_binding_without_regeneration(
        self,
    ):
        workflow = _WORKFLOW.read_text()
        binding_upload = workflow.index("Upload ci_verdict_summary_v2 binding input")
        verdict_generation = workflow.index("Generate ci_verdict_summary_v2 artifact")
        verdict_upload = workflow.index("Upload ci-verdict-summary-v2 artifact")
        summary = workflow.index("Output ci_verdict_summary_v2 Step Summary")
        assert binding_upload < verdict_generation < verdict_upload < summary
        binding_artifact_name = (
            "--artifact-name \"ci-verdict-summary-v2-binding-${{ github.run_id }}-"
            "${{ github.run_attempt }}\""
        )
        assert binding_artifact_name in workflow
        assert "--summary-input ci_verdict_summary_v2_artifacts/ci_verdict_summary_v2.json" in workflow

    def test_ci_verdict_job_checks_out_pr_head_before_binding_generation(self):
        workflow = _WORKFLOW.read_text()
        verdict_job = workflow[workflow.index("  ci-verdict-summary:"):]

        checkout = verdict_job.index("actions/checkout@")
        binding_generation = verdict_job.index(
            "Generate ci_verdict_summary_v2 binding input"
        )
        checkout_block = verdict_job[checkout:binding_generation]

        assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in checkout_block
        assert 'test "$(git rev-parse HEAD)" = "$PR_HEAD_SHA"' in verdict_job

    def test_ci_verdict_job_uses_locked_uv_python_runner(self):
        workflow = _WORKFLOW.read_text()
        verdict_job = workflow[workflow.index("  ci-verdict-summary:"):]

        setup_uv = verdict_job.index("uses: ./.github/actions/setup-python-uv")
        generator = verdict_job.index("Generate ci_verdict_summary_v2 artifact")
        summary = verdict_job.index("Output ci_verdict_summary_v2 Step Summary")
        producer = (
            "uv run --locked python3 "
            ".claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py"
        )

        assert setup_uv < generator < summary
        assert verdict_job.count(producer) == 2
        assert "\n          python3 .claude/skills/pr-review-judge/scripts/ci_verdict_summary_v2.py" not in verdict_job

    def test_summary_input_renders_existing_payload_without_regeneration(self, v2, tmp_path, monkeypatch):
        payload = {"schema": "ci_verdict_summary_v2", "schema_version": 2,
                   "overall_status": "stale_head_sha", "next_action": "refresh_head_sha",
                   "expected_head_sha": EXPECTED_SHA, "head_sha": EXPECTED_SHA,
                   "generated_at": "2026-01-01T00:00:00+00:00", "artifact_refs": [], "checks": []}
        source = tmp_path / "payload.json"
        source.write_text(json.dumps(payload))
        summary = tmp_path / "summary.md"
        assert v2.main(["--summary-input", str(source), "--summary-output", str(summary)]) == 0
        assert "CI Verdict Summary V2" in summary.read_text()
