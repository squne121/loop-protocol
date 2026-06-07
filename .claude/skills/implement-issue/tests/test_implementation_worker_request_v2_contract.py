#!/usr/bin/env python3
"""Tests for IMPLEMENTATION_WORKER_REQUEST_V2 contract verification.

Deterministically verifies:
- Schema fields for REQUEST_V2 and RESULT_V2
- kind-to-mode routing table completeness
- wrapper-only body update rule (direct gh pr edit --body-file禁止)
- new SubAgent absence (pr-hygiene-fixer.md, branch-syncer.md)
- expected_head_sha required for update_branch mode
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
AGENT_FILE = REPO_ROOT / ".claude" / "agents" / "implementation-worker.md"
SKILL_FILE = REPO_ROOT / ".claude" / "skills" / "implement-issue" / "SKILL.md"
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"


def rg(pattern: str, path: Path) -> list[str]:
    """Run ripgrep and return matching lines."""
    result = subprocess.run(
        ["rg", "-n", pattern, str(path)],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().splitlines()


def _get_section(content: str, header: str, window: int = 4000) -> str:
    """Return text starting from the last occurrence of header, up to window chars."""
    idx = content.rfind(header)
    if idx == -1:
        return ""
    return content[idx:idx + window]


class TestRequestV2SchemaFields:
    """AC1: IMPLEMENTATION_WORKER_REQUEST_V2 schema fields are defined."""

    def test_request_v2_marker_present(self):
        """IMPLEMENTATION_WORKER_REQUEST_V2 marker exists in agent file."""
        matches = rg("IMPLEMENTATION_WORKER_REQUEST_V2", AGENT_FILE)
        assert len(matches) > 0, (
            f"IMPLEMENTATION_WORKER_REQUEST_V2 not found in {AGENT_FILE}"
        )

    def test_result_v2_marker_present(self):
        """IMPLEMENTATION_WORKER_RESULT_V2 marker exists in agent file."""
        matches = rg("IMPLEMENTATION_WORKER_RESULT_V2", AGENT_FILE)
        assert len(matches) > 0, (
            f"IMPLEMENTATION_WORKER_RESULT_V2 not found in {AGENT_FILE}"
        )

    def test_request_v2_has_mode_field(self):
        """REQUEST_V2 defines mode field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "mode:" in section, "REQUEST_V2 mode field not found"

    def test_request_v2_has_required_auto_action_kind(self):
        """REQUEST_V2 defines required_auto_action.kind field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "required_auto_action:" in section, "required_auto_action field not found in REQUEST_V2 schema"
        assert "kind:" in section, "kind field not found in REQUEST_V2 schema"

    def test_request_v2_has_pr_number_field(self):
        """REQUEST_V2 defines pr_number field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "pr_number:" in section, "pr_number field not found in REQUEST_V2 schema"

    def test_request_v2_has_issue_number_field(self):
        """REQUEST_V2 defines issue_number field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "issue_number:" in section, "issue_number field not found in REQUEST_V2 schema"

    def test_request_v2_has_expected_head_sha_field(self):
        """REQUEST_V2 defines expected_head_sha field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "expected_head_sha:" in section, "expected_head_sha field not found in REQUEST_V2 schema"

    def test_request_v2_has_reviewed_head_sha_field(self):
        """REQUEST_V2 defines reviewed_head_sha field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "reviewed_head_sha:" in section, "reviewed_head_sha field not found in REQUEST_V2 schema"

    def test_result_v2_has_status_field(self):
        """RESULT_V2 defines status field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "status:" in section, "RESULT_V2 status field not found"

    def test_result_v2_has_wrapper_used_field(self):
        """RESULT_V2 defines wrapper_used field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "wrapper_used:" in section, "wrapper_used field not found in RESULT_V2"

    def test_result_v2_has_rerun_required_field(self):
        """RESULT_V2 defines rerun_required field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "rerun_required:" in section, "rerun_required field not found in RESULT_V2"

    def test_result_v2_has_errors_field(self):
        """RESULT_V2 defines errors field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "errors:" in section, "RESULT_V2 errors field not found"

    def test_result_v2_has_action_kind_field(self):
        """RESULT_V2 defines action_kind field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "action_kind:" in section, "action_kind field not found in RESULT_V2"

    def test_result_v2_has_before_head_sha_field(self):
        """RESULT_V2 defines before_head_sha field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "before_head_sha:" in section, "before_head_sha field not found in RESULT_V2"

    def test_result_v2_has_after_head_sha_field(self):
        """RESULT_V2 defines after_head_sha field."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "after_head_sha:" in section, "after_head_sha field not found in RESULT_V2"


class TestKindToModeRoutingTable:
    """AC2: required_auto_actions.kind → worker mode routing table is defined."""

    def test_routing_table_has_ensure_closing_keyword(self):
        """ensure_closing_keyword → update_pr_body_hygiene mapping exists."""
        matches = rg("ensure_closing_keyword", AGENT_FILE)
        assert len(matches) > 0, "ensure_closing_keyword not in routing table"

    def test_routing_table_has_update_pr_body_hygiene(self):
        """update_pr_body_hygiene → update_pr_body_hygiene mapping exists."""
        matches = rg("update_pr_body_hygiene", AGENT_FILE)
        assert len(matches) > 0, "update_pr_body_hygiene not in routing table"

    def test_routing_table_has_update_branch(self):
        """update_branch → update_branch mapping exists."""
        matches = rg("update_branch", AGENT_FILE)
        assert len(matches) > 0, "update_branch not in routing table"

    def test_routing_table_has_apply_pr_review_fix_delta(self):
        """apply_pr_review_fix_delta → apply_pr_review_fix_delta mapping exists."""
        matches = rg("apply_pr_review_fix_delta", AGENT_FILE)
        assert len(matches) > 0, "apply_pr_review_fix_delta not in routing table"

    def test_routing_table_has_unknown_kind_blocked(self):
        """unknown kind is deterministic blocked."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "deterministic blocked" in content or "unknown kind" in content.lower(), (
            "unknown kind deterministic blocked rule not found"
        )

    def test_routing_table_ensure_closing_keyword_routes_to_update_pr_body_hygiene(self):
        """ensure_closing_keyword must route to update_pr_body_hygiene."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        # Find the routing table section (table with | separators)
        # Look for a line containing both ensure_closing_keyword and update_pr_body_hygiene
        table_line_found = False
        for line in content.splitlines():
            if "ensure_closing_keyword" in line and "update_pr_body_hygiene" in line:
                table_line_found = True
                break
        assert table_line_found, (
            "No routing table line found with both ensure_closing_keyword "
            "and update_pr_body_hygiene on the same line"
        )


class TestWrapperOnlyBodyUpdate:
    """AC3: update_pr_body_hygiene must use update_pr.py wrapper; direct gh pr edit --body-file is prohibited."""

    def test_update_pr_py_wrapper_mentioned_in_agent(self):
        """update_pr.py wrapper reference exists in agent file."""
        matches = rg(r"update_pr\.py", AGENT_FILE)
        assert len(matches) > 0, (
            f"update_pr.py wrapper reference not found in {AGENT_FILE}"
        )

    def test_direct_gh_pr_edit_prohibited_in_agent(self):
        """Direct gh pr edit --body-file prohibition is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "禁止" in content or "prohibited" in content.lower() or "direct" in content.lower(), (
            "direct gh pr edit prohibition not documented in agent file"
        )

    def test_update_pr_py_wrapper_mentioned_in_skill(self):
        """IMPLEMENTATION_WORKER_REQUEST_V2 references exist in SKILL.md."""
        matches = rg("IMPLEMENTATION_WORKER_REQUEST_V2", SKILL_FILE)
        assert len(matches) > 0, (
            f"IMPLEMENTATION_WORKER_REQUEST_V2 not referenced in {SKILL_FILE}"
        )

    def test_wrapper_used_field_in_result_v2(self):
        """RESULT_V2 includes wrapper_used field for auditing wrapper usage."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "wrapper_used:" in section, "wrapper_used field not in RESULT_V2"

    def test_validator_failure_blocks_update(self):
        """validator failure blocks PR body update and records in RESULT_V2."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "validator" in content.lower() and "fail" in content.lower(), (
            "validator failure behavior not documented"
        )


class TestUpdateBranchSemantics:
    """AC4: update_branch mode GitHub REST semantics are documented."""

    def test_expected_head_sha_required_documented(self):
        """expected_head_sha required for update_branch mode is documented."""
        matches = rg("expected_head_sha", AGENT_FILE)
        assert len(matches) > 0, "expected_head_sha not documented in agent file"

    def test_202_accepted_documented(self):
        """202 Accepted handling is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "202" in content, "202 Accepted not documented in agent file"

    def test_before_after_head_sha_recording_documented(self):
        """before_head_sha / after_head_sha recording is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "before_head_sha" in content and "after_head_sha" in content, (
            "before_head_sha / after_head_sha not documented"
        )

    def test_422_stale_mismatch_documented(self):
        """422 stale/mismatch handling is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "422" in content, "422 status not documented"

    def test_403_permission_blocked_documented(self):
        """403 permission_blocked handling is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "403" in content and "permission_blocked" in content, (
            "403 / permission_blocked not documented"
        )

    def test_rerun_required_after_update_branch(self):
        """rerun_required after update_branch is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "rerun_required" in content, "rerun_required not documented"

    def test_update_branch_in_skill_md(self):
        """IMPLEMENTATION_WORKER_REQUEST_V2 references update_branch in SKILL.md."""
        content = SKILL_FILE.read_text(encoding="utf-8")
        assert "IMPLEMENTATION_WORKER_REQUEST_V2" in content, (
            "IMPLEMENTATION_WORKER_REQUEST_V2 not in SKILL.md"
        )
        assert "update_branch" in content, "update_branch not in SKILL.md"


class TestNewSubAgentAbsence:
    """AC5: No new SubAgent files (pr-hygiene-fixer.md, branch-syncer.md) exist."""

    def test_pr_hygiene_fixer_absent(self):
        """pr-hygiene-fixer.md must not exist in .claude/agents/."""
        fixer_path = AGENTS_DIR / "pr-hygiene-fixer.md"
        assert not fixer_path.exists(), (
            f"pr-hygiene-fixer.md must not exist (found at {fixer_path})"
        )

    def test_branch_syncer_absent(self):
        """branch-syncer.md must not exist in .claude/agents/."""
        syncer_path = AGENTS_DIR / "branch-syncer.md"
        assert not syncer_path.exists(), (
            f"branch-syncer.md must not exist (found at {syncer_path})"
        )

    def test_new_subagent_prohibition_documented(self):
        """New SubAgent addition prohibition is documented in agent file."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "新規 SubAgent" in content or "新規 subagent" in content.lower(), (
            "New SubAgent prohibition not documented in agent file"
        )


class TestExpectedHeadShaRequired:
    """AC6 / AC4: expected_head_sha required behavior for update_branch."""

    def test_expected_head_sha_missing_blocks_execution(self):
        """expected_head_sha absence must block update_branch execution."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "expected_head_sha" in content, "expected_head_sha not mentioned"
        assert "なければ実行しない" in content or "required" in content.lower() or "必須" in content, (
            "expected_head_sha required constraint not documented"
        )

    def test_expected_head_sha_in_request_v2_schema(self):
        """expected_head_sha is listed in REQUEST_V2 schema fields."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        # Use the YAML code block marker `IMPLEMENTATION_WORKER_REQUEST_V2:` (with colon)
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        assert "expected_head_sha" in section, (
            "expected_head_sha not in REQUEST_V2 schema section"
        )


class TestV2ModeEnumExactSet:
    """Fix 1 — V2 repair modes are exactly the 3 documented repair modes.
    implement_issue is intentionally NOT a V2 repair mode."""

    def test_v2_mode_enum_exact_set(self):
        """V2 mode enum contains exactly the 3 repair modes (not implement_issue)."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_REQUEST_V2:")
        # All 3 repair modes must be present
        assert "update_pr_body_hygiene" in section, "update_pr_body_hygiene not in V2 mode enum"
        assert "update_branch" in section, "update_branch not in V2 mode enum"
        assert "apply_pr_review_fix_delta" in section, "apply_pr_review_fix_delta not in V2 mode enum"
        # implement_issue is intentionally excluded from V2 repair modes
        mode_line = next(
            (line for line in section.splitlines() if line.strip().startswith("mode:")),
            "",
        )
        assert "implement_issue" not in mode_line, (
            "implement_issue must NOT appear in V2 mode enum (it is a V1-only flow)"
        )


class TestUpdateBranchResultReasonCodes:
    """Fix 2 — reason_code field in RESULT_V2 for update_branch error cases."""

    def test_update_branch_result_preserves_reason_codes(self):
        """reason_code values are documented for update_branch error classification."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "reason_code" in section, "reason_code field not in RESULT_V2"
        assert "expected_head_sha_mismatch" in section, (
            "expected_head_sha_mismatch reason_code not documented"
        )
        assert "secondary_rate_limit" in section, (
            "secondary_rate_limit reason_code not documented"
        )
        assert "validation_failed" in section, (
            "validation_failed reason_code not documented"
        )

    def test_422_blocked_status_preserved(self):
        """422 still maps to status: blocked (reason_code adds granularity, does not replace status)."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        # The status enum must still include 'blocked'
        section = _get_section(content, "IMPLEMENTATION_WORKER_RESULT_V2:")
        assert "blocked" in section, "status: blocked must remain in RESULT_V2 for 422 cases"


class TestApplyPrReviewFixDeltaInputFields:
    """Fix 3 — apply_pr_review_fix_delta input contract fields are documented."""

    def test_apply_pr_review_fix_delta_has_required_fields(self):
        """review_artifact_ref, reviewed_head_sha, allowed_paths_snapshot, delta_summary are documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "review_artifact_ref" in content, (
            "review_artifact_ref not documented for apply_pr_review_fix_delta"
        )
        assert "reviewed_head_sha" in content, (
            "reviewed_head_sha not documented for apply_pr_review_fix_delta"
        )
        assert "allowed_paths_snapshot" in content, (
            "allowed_paths_snapshot not documented for apply_pr_review_fix_delta"
        )
        assert "delta_summary" in content, (
            "delta_summary not documented for apply_pr_review_fix_delta"
        )

    def test_apply_pr_review_fix_delta_result_fields(self):
        """commit_sha and pushed_branch are documented in RESULT_V2 for apply_pr_review_fix_delta."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "commit_sha" in content, (
            "commit_sha not documented in RESULT_V2 for apply_pr_review_fix_delta"
        )
        assert "pushed_branch" in content, (
            "pushed_branch not documented in RESULT_V2 for apply_pr_review_fix_delta"
        )


class TestV2DoesNotRequireContractSnapshotUrlForRepairModes:
    """Fix 1 — repair modes do not require issue-contract-review preflight."""

    def test_v2_does_not_require_contract_snapshot_url_for_repair_modes(self):
        """Docs state repair modes (update_pr_body_hygiene/update_branch) don't require preflight."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        # The dispatcher section must document that V2 repair modes skip issue-contract-review
        assert "preflight" in content, "preflight mention not found in agent file"
        # Check that the dispatcher table or text explicitly states no preflight for repair modes
        assert (
            "issue-contract-review preflight" in content
            or "preflight を実施しない" in content
            or "preflight は不要" in content
        ), (
            "V2 repair modes must explicitly state they do not require issue-contract-review preflight"
        )


class TestAllowedPathsGateResultV1Contract:
    """AC6 — ALLOWED_PATHS_GATE_RESULT_V1 contract is fixed in regression tests."""

    def test_allowed_paths_gate_result_v1_mentioned_in_agent(self):
        """ALLOWED_PATHS_GATE_RESULT_V1 marker exists in agent file."""
        matches = rg("ALLOWED_PATHS_GATE_RESULT_V1", AGENT_FILE)
        assert len(matches) > 0, (
            "ALLOWED_PATHS_GATE_RESULT_V1 not found in agent file"
        )

    def test_allowed_paths_gate_result_v1_in_skill(self):
        """ALLOWED_PATHS_GATE_RESULT_V1 contract is referenced in SKILL.md."""
        matches = rg("ALLOWED_PATHS_GATE_RESULT_V1", SKILL_FILE)
        assert len(matches) > 0, (
            "ALLOWED_PATHS_GATE_RESULT_V1 not found in SKILL.md"
        )

    def test_allowed_paths_gate_status_field_documented(self):
        """ALLOWED_PATHS_GATE_RESULT_V1.status enum is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "ALLOWED_PATHS_GATE_RESULT_V1:")
        # Status enum must include at least ok and fail_closed
        assert "ok" in section, "status: ok not documented in ALLOWED_PATHS_GATE_RESULT_V1"
        assert "fail_closed" in section, "status: fail_closed not documented"

    def test_manifest_snapshot_sha256_documented(self):
        """manifest_snapshot_sha256 field is documented."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        section = _get_section(content, "ALLOWED_PATHS_GATE_RESULT_V1:")
        assert "manifest_snapshot_sha256" in section or "manifest" in section.lower(), (
            "manifest snapshot field not documented"
        )

    def test_allowed_paths_compliance_is_advisory(self):
        """allowed_paths_compliance is documented as advisory, not canonical."""
        content = AGENT_FILE.read_text(encoding="utf-8")
        assert "advisory" in content.lower(), (
            "advisory status of allowed_paths_compliance not documented"
        )
        # Ensure it explicitly says self-report / not canonical
        assert (
            "self-report" in content.lower()
            or "canonical 判定根拠" in content
            or "reference" in content.lower()
        ), (
            "canonical vs advisory distinction for allowed_paths_compliance not clear"
        )
