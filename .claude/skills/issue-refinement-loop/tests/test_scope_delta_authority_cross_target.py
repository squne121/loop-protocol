"""#2053 AC6: cross_target_direct_mutation is always rejected and is typed
separately from primary_target_exact_delta + non-mutating
follow_up_candidates.

GIVEN a review comment that targets a DIFFERENT Issue than the one being
refined
WHEN classify_scope_delta_authority() runs with target_issue_number bound to
the Issue under refinement
THEN the cross-target directive never grants mutation authority and is
surfaced only as a non-mutating follow_up_candidates entry, distinct from a
same-target explicit directive (primary_target_exact_delta).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")

REPO = "squne121/loop-protocol"
PRIMARY_ISSUE = 1323
CROSS_ISSUE = 999


def _evidence(
    *,
    target_issue_number,
    comment_id,
    directive_markers=None,
    extracted_directives=None,
):
    return {
        "schema_version": "SCOPE_DELTA_AUTHORITY_EVIDENCE_V1",
        "source_kind": "issue_comment",
        "source_ref": f"https://github.com/{REPO}/issues/{target_issue_number}#issuecomment-{comment_id}",
        "source_issue_number": target_issue_number,
        "comment_id": comment_id,
        "comment_url": f"https://github.com/{REPO}/issues/{target_issue_number}#issuecomment-{comment_id}",
        "issue_url": f"https://github.com/{REPO}/issues/{target_issue_number}",
        "body_sha256": "sha256:deadbeef",
        "author_login": "reviewer",
        "author_type": "User",
        "author_association": "OWNER",
        "captured_at": "2026-08-09T00:00:00Z",
        "directive_markers": directive_markers or [],
        "extracted_directives": extracted_directives or [],
        "ambiguity_flags": [],
        "boundary_flags": [],
        "confidence": None,
    }


def test_cross_target_mutation_rejected_and_follow_up_candidates_separated():
    # GIVEN only a cross-target directive (targets issue 999) while the
    # loop is refining issue 1323.
    cross_only_evidence = [
        _evidence(
            target_issue_number=CROSS_ISSUE,
            comment_id=1,
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC1: do X on #999"],
        )
    ]

    # WHEN classified against the primary target
    result = sda.classify_scope_delta_authority(
        cross_only_evidence,
        target_issue_number=PRIMARY_ISSUE,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:base",
    )

    # THEN mutation is always rejected and typed as cross_target_direct_mutation
    assert result["route"]["action"] != "contract_update_required"
    assert result["route"]["implementation_allowed"] is False
    assert result["route"]["reason_code"] == "cross_target_direct_mutation"
    assert result["route"]["target_binding"] == "cross_target_direct_mutation"

    # AND it is surfaced only as a non-mutating follow_up_candidates entry
    follow_ups = result["follow_up_candidates"]
    assert len(follow_ups) == 1
    assert follow_ups[0]["target_issue_number"] == CROSS_ISSUE
    assert follow_ups[0]["mutation_authority"] is False


def test_same_target_exact_delta_is_separated_from_cross_target_follow_up():
    # GIVEN a same-target explicit directive AND a cross-target directive
    same_target_evidence = _evidence(
        target_issue_number=PRIMARY_ISSUE,
        comment_id=2,
        directive_markers=["revised acceptance criteria"],
        extracted_directives=["AC2: do Y on #1323"],
    )
    cross_target_evidence = _evidence(
        target_issue_number=CROSS_ISSUE,
        comment_id=3,
        directive_markers=["revised acceptance criteria"],
        extracted_directives=["AC3: do Z on #999"],
    )

    # WHEN classified against the primary target
    result = sda.classify_scope_delta_authority(
        [same_target_evidence, cross_target_evidence],
        target_issue_number=PRIMARY_ISSUE,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:base",
    )

    # THEN the same-target directive is an exact delta bound to the primary
    # target...
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["target_binding"] == "primary_target_exact_delta"
    assert result["route"]["implementation_allowed"] is False

    # ...and the cross-target directive is separated out as a non-mutating
    # follow_up_candidate rather than merged into the primary mutation.
    follow_ups = result["follow_up_candidates"]
    assert len(follow_ups) == 1
    assert follow_ups[0]["target_issue_number"] == CROSS_ISSUE
    assert follow_ups[0]["mutation_authority"] is False


def test_cross_target_without_target_issue_number_keeps_prior_fail_closed_behavior():
    # GIVEN target_issue_number is not supplied (unchanged pre-#2053 caller)
    cross_only_evidence = [
        _evidence(
            target_issue_number=CROSS_ISSUE,
            comment_id=4,
            directive_markers=["revised acceptance criteria"],
            extracted_directives=["AC4: do W"],
        )
    ]

    # WHEN classified without a target_issue_number
    result = sda.classify_scope_delta_authority(
        cross_only_evidence,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:base",
    )

    # THEN no cross-target partitioning happens (existing fail-closed path
    # is exercised unchanged, no follow_up_candidates key is added).
    assert "follow_up_candidates" not in result
