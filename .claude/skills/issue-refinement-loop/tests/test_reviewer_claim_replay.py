from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import SCHEMA, analyze  # noqa: E402


READINESS_LP001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP001",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 1,
            "line_end": 1,
        }
    ],
}

READINESS_LP010 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP010",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 5,
            "line_end": 5,
        }
    ],
}

READINESS_LP005 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP005",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 3,
            "line_end": 3,
        }
    ],
}

READINESS_VCS001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "VCS001",
            "source_check": "contract_readiness_check",
            "category": "compound_command_disallowed",
            "line_start": 10,
            "line_end": 10,
        }
    ],
}

READINESS_C9 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "C9_runtime_applicability_present",
            "source_check": "contract_readiness_check",
            "category": "rva_immediate_field_missing",
            "line_start": 8,
            "line_end": 8,
        }
    ],
}

READINESS_UNEXPECTED_PASS = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "",
            "source_check": "baseline_vc_preflight",
            "category": "unexpected_pass",
            "line_start": 12,
            "line_end": 12,
        }
    ],
}

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [],
}

# NOTE (PR #1304 iteration-4 fix_delta, Blocker 2): every review-result
# fixture below now carries a `body_sha256` matching the paired readiness
# fixture's `body_sha256` ("sha256:body-a"), because `analyze()` now fails
# closed (`reason_code: body_sha_missing` / `body_sha_mismatch`) instead of
# silently falling back to the readiness artifact's hash when the review
# artifact carries none. See `test_body_hash_*` below for the fail-closed
# behavior itself.

COMPACT_C4 = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
    "structured_blockers": [],
    "findings": [],
}

COMPACT_MISSING_SECTION = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "missing_section", "message": "missing section"}],
    "structured_blockers": [],
    "findings": [],
}

COMPACT_C9_DETERMINISTIC = {
    # Producer-derived shape: mirrors what `check_issue_contract.py`'s C9
    # FAIL/LEGACY_MISSING path (post narrow-approval fix) actually emits via
    # `_append_findings(..., finding_kind=REVIEW_ISSUE_FINDING_KIND_DETERMINISTIC_DOMAIN_BLOCKER,
    # checker_evidence=_make_self_checker_evidence(...), reviewer_blocker_code="C9")`,
    # then compacted by `compact_review_result.py` into the
    # ISSUE_REVIEW_RESULT_COMPACT_V1 shape consumed here.
    #
    # NOTE (PR #1319 reviewer blocker fix, #1314): `compact_review_result.py`
    # emits the canonical body-hash field as `producer_body_sha256`
    # (`"producer_body_sha256": raw_result.get("body_sha256")`); the bare
    # `body_sha256` key is only a back-compat fallback consumed in
    # `reviewer_claim_replay.py`. This fixture must exercise the canonical
    # producer shape, not the fallback, so it carries `producer_body_sha256`
    # here, matching `READINESS_CLEAN["body_sha256"]` ("sha256:body-a").
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "producer_body_sha256": "sha256:body-a",
    "blocking_issues": [
        "## Runtime Verification Applicability セクションがない（レガシー Issue）"
    ],
    "structured_blockers": [
        {
            "code": "C9",
            "message": "## Runtime Verification Applicability セクションがない（レガシー Issue）",
            "finding_kind": "deterministic_domain_blocker",
            "deterministic_domain_key": "runtime_applicability",
            "blocking": True,
            "checker_evidence": [
                {
                    "source_check": "check_issue_contract",
                    "rule_id": "C9_runtime_applicability_present",
                    "category": "runtime_applicability",
                    "artifact_path": "check_issue_contract.py",
                    "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
                    "body_sha256": "sha256:body-a",
                    "iteration_id": "check_issue_contract_current",
                    "line_start": None,
                    "line_end": None,
                }
            ],
        }
    ],
    "findings": [
        {
            "finding_kind": "deterministic_domain_blocker",
            "deterministic_domain_key": "runtime_applicability",
            "blocking": True,
            "checker_evidence": [
                {
                    "source_check": "check_issue_contract",
                    "rule_id": "C9_runtime_applicability_present",
                    "category": "runtime_applicability",
                    "artifact_path": "check_issue_contract.py",
                    "artifact_schema": "CHECK_ISSUE_CONTRACT_V1",
                    "body_sha256": "sha256:body-a",
                    "iteration_id": "check_issue_contract_current",
                    "line_start": None,
                    "line_end": None,
                }
            ],
            "message": "## Runtime Verification Applicability セクションがない（レガシー Issue）",
        }
    ],
}

COMPACT_C9 = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "rva_immediate_field_missing", "message": "missing runtime applicability"}],
    "structured_blockers": [],
    "findings": [],
}

COMPACT_UNEXPECTED_PASS = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1368",
    "body_sha256": "sha256:body-a",
    "blocking_issues": [{"code": "unexpected_pass", "message": "unexpected pass"}],
    "structured_blockers": [],
    "findings": [],
}


def _finding(
    *,
    finding_kind: str,
    deterministic_domain_key: str,
    body_sha256: str = "sha256:body-a",
    blocking: bool,
    artifact_schema: str = "REVIEW_ISSUE_RESULT_V1",
    artifact_path: str = ".claude/artifacts/review.json",
) -> dict:
    evidence = []
    if finding_kind == "deterministic_domain_blocker":
        evidence = [
            {
                "source_check": "check_issue_contract",
                "rule_id": deterministic_domain_key,
                "category": "deterministic_check",
                "artifact_path": artifact_path,
                "artifact_schema": artifact_schema,
                "body_sha256": body_sha256,
                "iteration_id": "iter-1",
                "line_start": 10,
                "line_end": 10,
            }
        ]
    return {
        "finding_kind": finding_kind,
        "deterministic_domain_key": deterministic_domain_key,
        "blocking": blocking,
        "checker_evidence": evidence,
        "message": deterministic_domain_key,
    }


def test_c4_with_lp001_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["schema"] == SCHEMA
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_c4_with_lp010_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_missing_section_with_real_lp001_is_backed():
    result, _ = analyze(
        review_result=COMPACT_MISSING_SECTION,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "missing_section"
    assert blocker["evidence"][0]["rule_id"] == "LP001"


def test_lp010_requires_exact_lp010_match():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "LP010", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["blockers"][0]["evidence"][0]["rule_id"] == "LP010"


def test_c5_maps_to_ac_vc_number_mismatch_and_reconstructs_lp010():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C5", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "ac_vc_number_mismatch"
    assert blocker["evidence"][0]["rule_id"] == "LP010"


def test_c5_checker_gap_with_failed_deterministic_check_is_inconsistency():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C5", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
        "deterministic_checks": {"C5_ac_vc_number_alignment": "fail"},
        "findings": [
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key="vc_number_alignment",
                blocking=False,
            )
        ],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "checker_artifact_inconsistency"
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "ac_vc_number_mismatch"
    assert blocker["checker_artifact_inconsistency"] is True
    assert blocker["evidence"][0]["rule_id"] == "LP010"


@pytest.mark.parametrize(
    ("review", "readiness_result", "deterministic_domain_key", "check_name", "expected_kind", "expected_rule_id"),
    [
        (
            COMPACT_MISSING_SECTION,
            READINESS_LP001,
            "required_sections",
            "C1_required_sections",
            "missing_section",
            "LP001",
        ),
        (
            COMPACT_C4,
            READINESS_VCS001,
            "vc_command_format",
            "C4_vc_commands_present",
            "vc_command_format",
            "VCS001",
        ),
        (
            {
                "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
                "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
                "body_sha256": "sha256:body-a",
                "blocking_issues": [{"code": "C5", "message": "ac/vc mismatch"}],
                "structured_blockers": [],
            },
            READINESS_LP010,
            "vc_number_alignment",
            "C5_ac_vc_number_alignment",
            "ac_vc_number_mismatch",
            "LP010",
        ),
        (
            COMPACT_C9,
            READINESS_C9,
            "runtime_applicability",
            "C9_runtime_applicability_present",
            "rva_immediate_field_missing",
            "C9_runtime_applicability_present",
        ),
    ],
)
def test_checker_gap_failed_deterministic_check_mapping_is_fixed(
    review,
    readiness_result,
    deterministic_domain_key,
    check_name,
    expected_kind,
    expected_rule_id,
):
    review_result = {
        **review,
        "deterministic_checks": {check_name: "fail"},
        "findings": [
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key=deterministic_domain_key,
                blocking=False,
            )
        ],
    }
    result, _ = analyze(
        review_result=review_result,
        readiness_result=readiness_result,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "checker_artifact_inconsistency"
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == expected_kind
    assert blocker["checker_artifact_inconsistency"] is True
    assert blocker["evidence"][0]["rule_id"] == expected_rule_id


def test_missing_section_with_lp005_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_MISSING_SECTION,
        readiness_result=READINESS_LP005,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_c9_blocker_is_deterministic_backed_not_checker_gap():
    """GIVEN a producer-derived compact review result whose structured_blockers/findings
    carry C9 deterministic evidence (post narrow-approval check_issue_contract.py fix)
    WHEN analyze() replays it THEN the verdict is deterministic_fail_confirmed, the
    blocker normalizes to rva_immediate_field_missing, it is deterministic_backed, and
    it is NOT classified as a checker_gap (readiness artifact intentionally has no
    errors -- the backing comes entirely from the review artifact's own structured
    evidence, not from readiness fallback).
    """
    result, _ = analyze(
        review_result=COMPACT_C9_DETERMINISTIC,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["should_consume_iteration"] is True

    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "rva_immediate_field_missing"
    assert blocker["deterministic_backed"] is True
    assert blocker["checker_gap"] is False
    assert blocker["checker_artifact_inconsistency"] is False
    assert blocker["taxonomy_gap"] is False
    assert blocker["evidence"][0]["rule_id"] == "C9_runtime_applicability_present"
    assert blocker["evidence"][0]["category"] == "runtime_applicability"
    assert blocker["evidence"][0]["body_sha256"] == result["body_sha256"]

    # PR #1319 reviewer blocker fix (#1314): assert directly against the
    # fixture's own `findings[*].checker_evidence` (the canonical producer
    # shape), not only against the derived `blocker["evidence"]`, so this
    # test actually exercises the `producer_body_sha256` canonical field
    # instead of silently passing through the `body_sha256` back-compat
    # fallback path in `reviewer_claim_replay.py`.
    for finding in COMPACT_C9_DETERMINISTIC["findings"]:
        for evidence_entry in finding["checker_evidence"]:
            assert evidence_entry["rule_id"] == "C9_runtime_applicability_present"
            assert evidence_entry["category"] == "runtime_applicability"
            assert evidence_entry["body_sha256"] == result["body_sha256"]

    assert len(result["rewrite_ready_blockers"]) == 1
    assert result["rewrite_ready_blockers"][0]["normalized_kind"] == "rva_immediate_field_missing"


def test_second_unbacked_same_body_becomes_false_positive():
    previous = {
        "schema": "REVIEWER_CLAIM_REPLAY_STATE_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:body-a",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 1,
        "last_review_artifact": "/tmp/prior.json",
    }
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=previous,
    )
    assert result["verdict"] == "reviewer_false_positive_suspected"
    assert result["routing"] == "human_escalation"
    assert next_state["consecutive_unbacked_count"] == 2


def test_checker_gap_with_deterministic_check_fail_becomes_checker_artifact_inconsistency():
    review = {
        **COMPACT_C4,
        "deterministic_checks": {"C4_vc_commands_present": "fail"},
        "findings": [
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key="vc_command_format",
                blocking=False,
            )
        ],
    }
    result, next_state = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "checker_artifact_inconsistency"
    assert result["routing"] == "fix_checker_artifact"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["checker_artifact_inconsistency"] is True
    assert next_state["consecutive_unbacked_count"] == 0


def test_body_hash_change_resets_consecutive_count():
    previous = {
        "schema": "REVIEWER_CLAIM_REPLAY_STATE_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:old",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 3,
        "last_review_artifact": "/tmp/prior.json",
    }
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=previous,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert next_state["consecutive_unbacked_count"] == 1


def test_vc_preflight_category_backs_c4():
    preflight = {
        "schema": "baseline_vc_preflight/v1",
        "results": [{"category": "compound_command_disallowed", "line_start": 10, "line_end": 10}],
    }
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["blockers"][0]["evidence"][0]["source_check"] == "baseline_vc_preflight"


def test_unexpected_pass_readiness_category_is_deterministic_backed():
    result, _ = analyze(
        review_result=COMPACT_UNEXPECTED_PASS,
        readiness_result=READINESS_UNEXPECTED_PASS,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["should_consume_iteration"] is True
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "unexpected_pass"
    assert blocker["evidence"][0]["source_check"] == "baseline_vc_preflight"
    assert blocker["evidence"][0]["category"] == "unexpected_pass"


def test_unexpected_pass_vc_preflight_category_is_deterministic_backed():
    preflight = {
        "schema": "baseline_vc_preflight/v1",
        "results": [{"category": "unexpected_pass", "line_start": 12, "line_end": 12}],
    }
    result, _ = analyze(
        review_result=COMPACT_UNEXPECTED_PASS,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["should_consume_iteration"] is True
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "unexpected_pass"
    assert blocker["evidence"][0]["source_check"] == "baseline_vc_preflight"
    assert blocker["evidence"][0]["category"] == "unexpected_pass"


def test_checker_gap_does_not_suppress_fallback_evidence_search():
    review = {
        **COMPACT_C4,
        "deterministic_checks": {"C4_vc_commands_present": "fail"},
        "findings": [
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key="vc_command_format",
                blocking=False,
            )
        ],
    }
    preflight = {
        "schema": "baseline_vc_preflight/v1",
        "results": [{"category": "compound_command_disallowed", "line_start": 10, "line_end": 10}],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "checker_artifact_inconsistency"
    assert result["blockers"][0]["evidence"][0]["source_check"] == "baseline_vc_preflight"


def test_supported_deterministic_finding_routes_only_matching_blocker():
    review = {
        **COMPACT_C4,
        "findings": [
            _finding(
                finding_kind="deterministic_domain_blocker",
                deterministic_domain_key="vc_command_format",
                blocking=True,
            ),
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key="required_sections",
                blocking=False,
            ),
        ],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert len(result["rewrite_ready_blockers"]) == 1
    assert result["rewrite_ready_blockers"][0]["normalized_kind"] == "vc_command_format"


def test_mixed_supported_and_unsupported_findings_do_not_promote_all_blockers():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:body-a",
        "blocking_issues": [
            {"code": "C4", "message": "missing $ prefix"},
            {"code": "missing_section", "message": "missing section"},
        ],
        "structured_blockers": [],
        "findings": [
            _finding(
                finding_kind="deterministic_domain_blocker",
                deterministic_domain_key="vc_command_format",
                blocking=True,
            ),
            _finding(
                finding_kind="checker_gap",
                deterministic_domain_key="required_sections",
                blocking=False,
            ),
        ],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert len(result["rewrite_ready_blockers"]) == 1
    assert result["rewrite_ready_blockers"][0]["reviewer_blocker_code"] == "C4"
    unbacked = [item for item in result["blockers"] if not item["deterministic_backed"]]
    assert len(unbacked) == 1
    assert unbacked[0]["reviewer_blocker_code"] == "missing_section"
    # PR #1304 iteration-4 fix_delta (High item): the grouped
    # taxonomy_gap_blockers / checker_gap_blockers arrays must never
    # contain a blocker that also appears in rewrite_ready_blockers (the
    # only list a Step 4 issue-author rewrite payload may consume from).
    rewrite_ready_codes = {b["reviewer_blocker_code"] for b in result["rewrite_ready_blockers"]}
    taxonomy_gap_codes = {b["reviewer_blocker_code"] for b in result["taxonomy_gap_blockers"]}
    checker_gap_codes = {b["reviewer_blocker_code"] for b in result["checker_gap_blockers"]}
    assert rewrite_ready_codes.isdisjoint(taxonomy_gap_codes)
    assert rewrite_ready_codes.isdisjoint(checker_gap_codes)


def test_stale_or_wrong_schema_evidence_fails_closed():
    review = {
        **COMPACT_C4,
        "findings": [
            _finding(
                finding_kind="deterministic_domain_blocker",
                deterministic_domain_key="vc_command_format",
                blocking=True,
                body_sha256="sha256:stale",
                artifact_schema="WRONG_SCHEMA",
            )
        ],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["blockers"][0]["deterministic_backed"] is False


def test_cli_bad_optional_json_fails_closed(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    vc_syntax_path = tmp_path / "vc_syntax.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")
    vc_syntax_path.write_text("{bad", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--vc-syntax-result-file",
            str(vc_syntax_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["verdict"] == "input_or_runtime_error"


def test_cli_writes_and_reuses_state_file(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    state_path = tmp_path / "state.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")

    first = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--state-file",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    second = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--state-file",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(second.stdout)["verdict"] == "reviewer_false_positive_suspected"
    assert json.loads(state_path.read_text(encoding="utf-8"))["consecutive_unbacked_count"] == 2


def test_state_file_with_nan_is_rejected(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    state_path = tmp_path / "state.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")
    state_path.write_text('{"schema":"REVIEWER_CLAIM_REPLAY_STATE_V1","bad":NaN}', encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--state-file",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["verdict"] == "input_or_runtime_error"


def test_save_state_rejects_nan(tmp_path: Path):
    from reviewer_claim_replay import _save_state  # noqa: WPS433

    with pytest.raises(ValueError):
        _save_state(
            str(tmp_path / "state.json"),
            {"schema": "REVIEWER_CLAIM_REPLAY_STATE_V1", "bad": float("nan")},
        )


def test_cli_stdout_is_compact_json(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_VCS001), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "\n" not in proc.stdout.strip()
    assert len(proc.stdout.encode("utf-8")) <= 2048


# ---------------------------------------------------------------------------
# Body hash fail-closed contract (PR #1304 iteration-4 fix_delta, human
# review Blocker 2): `analyze()` must not silently fall back to the
# readiness artifact's body hash when the review artifact carries neither
# `producer_body_sha256` nor `body_sha256` -- and must reject a mismatch
# between the two artifacts' hashes rather than proceeding to taxonomy
# classification.
# ---------------------------------------------------------------------------


def test_body_hash_missing_on_review_side_fails_closed():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        # No `producer_body_sha256` / `body_sha256` at all.
        "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
        "structured_blockers": [],
    }
    result, next_state = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={"consecutive_unbacked_count": 1},
    )
    assert result["verdict"] == "input_or_runtime_error"
    assert result["verdict_detail_v1"] == "input_or_runtime_error"
    assert result["routing"] == "human_judgment_required"
    assert result["reason_code"] == "body_sha_missing"
    assert result["blockers"] == []
    # State must not advance -- a subsequent, correctly-paired replay must
    # not be misclassified as a repeated reviewer claim.
    assert next_state == {"consecutive_unbacked_count": 1}


def test_body_hash_missing_on_readiness_side_fails_closed():
    readiness_missing_hash = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": "",
        "errors": [],
    }
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=readiness_missing_hash,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["reason_code"] == "body_sha_missing"
    assert result["routing"] == "human_judgment_required"


def test_producer_body_sha256_mismatch_fails_closed_not_taxonomy_gap():
    # A stale review artifact (fewer/older body hash) paired with a fresh
    # readiness artifact must not be misclassified as `taxonomy_gap` --
    # this is the exact scenario the previous unconditional readiness-hash
    # fallback allowed through.
    stale_review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1286",
        "producer_body_sha256": "sha256:stale-review-body",
        "blocking_issues": [{"code": "C5", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
        "deterministic_checks": {"C5_ac_vc_number_alignment": "fail"},
    }
    fresh_readiness = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": "sha256:fresh-readiness-body",
        "errors": [],
    }
    result, next_state = analyze(
        review_result=stale_review,
        readiness_result=fresh_readiness,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={"consecutive_unbacked_count": 0},
    )
    assert result["verdict"] != "checker_artifact_inconsistency"
    assert result["verdict_detail_v1"] != "taxonomy_gap"
    assert result["verdict"] == "input_or_runtime_error"
    assert result["verdict_detail_v1"] == "input_or_runtime_error"
    assert result["reason_code"] == "body_sha_mismatch"
    assert result["routing"] == "human_judgment_required"
    assert result["should_consume_iteration"] is False
    assert next_state == {"consecutive_unbacked_count": 0}


def test_producer_body_sha256_takes_priority_over_legacy_body_sha256():
    # `producer_body_sha256` is the canonical field (compact_review_result.py
    # writes it); a stale/legacy `body_sha256` value on the same artifact
    # must not override it.
    review = {
        **COMPACT_C4,
        "producer_body_sha256": "sha256:body-a",
        "body_sha256": "sha256:different-legacy-value",
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    # producer_body_sha256 ("sha256:body-a") matches readiness body_sha256,
    # so this must proceed to normal classification, not fail closed.
    assert result["reason_code"] is None
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"


def test_legacy_body_sha256_fallback_used_when_producer_field_absent():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
        "structured_blockers": [],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["reason_code"] is None
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"


# ---------------------------------------------------------------------------
# taxonomy_gap_blockers / checker_gap_blockers / unbacked_blockers grouping
# (PR #1304 iteration-4 fix_delta, High item)
# ---------------------------------------------------------------------------


def test_checker_gap_blocker_is_grouped_separately_from_rewrite_ready():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [{"code": "z9_unregistered", "message": "??"}],
        "structured_blockers": [],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["rewrite_ready_blockers"] == []
    assert result["taxonomy_gap_blockers"] == []
    assert len(result["checker_gap_blockers"]) == 1
    assert result["checker_gap_blockers"][0]["reviewer_blocker_code"] == "z9_unregistered"
    assert result["unbacked_blockers"] == []


def test_unbacked_blocker_is_grouped_in_unbacked_blockers():
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["rewrite_ready_blockers"] == []
    assert result["taxonomy_gap_blockers"] == []
    assert result["checker_gap_blockers"] == []
    assert len(result["unbacked_blockers"]) == 1
    assert result["unbacked_blockers"][0]["reviewer_blocker_code"] == "C4"


# ---------------------------------------------------------------------------
# 2048-byte trimming must preserve blocker-level taxonomy_gap / checker_gap
# classification (PR #1304 iteration-4 fix_delta, High item)
# ---------------------------------------------------------------------------


def test_cli_trimming_preserves_taxonomy_gap_and_checker_gap_fields(tmp_path: Path):
    # Build a review artifact with enough blockers / long messages to force
    # the CLI's 2048-byte trim path, mixing a checker_gap blocker with an
    # unbacked one.
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "body_sha256": "sha256:body-a",
        "blocking_issues": [
            {
                "code": f"z_unregistered_{i}",
                "message": "x" * 200,
            }
            for i in range(6)
        ],
        "structured_blockers": [],
    }
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert len(proc.stdout.encode("utf-8")) <= 2048
    # Top-level verdict_detail_v1 must survive trimming.
    assert "verdict_detail_v1" in payload
    # Each trimmed blocker entry must still carry taxonomy_gap / checker_gap.
    assert payload["blockers"], "trimmed output must still include some blockers"
    for blocker in payload["blockers"]:
        assert "taxonomy_gap" in blocker
        assert "checker_gap" in blocker
        assert "normalized_kind" in blocker
        assert "checker_artifact_inconsistency" in blocker
