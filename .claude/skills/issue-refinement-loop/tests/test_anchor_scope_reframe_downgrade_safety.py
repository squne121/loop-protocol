"""#2053 AC2: a structured ANCHOR_SCOPE_REFRAME_V1 payload that WAS present
but is invalid/stale/wrong-target must never be reinterpreted as freeform
SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 built from the same comment body ("downgrade
fallback").

GIVEN an anchor comment carrying a structured ANCHOR_SCOPE_REFRAME_V1 payload
WHEN that payload is invalid (schema_invalid / wrong_repo / wrong_issue_number)
THEN _structured_anchor_payload_present_but_invalid() reports True and
_build_scope_delta_authority_evidence() must not be interpreted as a valid
freeform directive for the same comment.

GIVEN an anchor comment with NO structured payload at all (legitimate
freeform lane, e.g. Issue #1270)
THEN the downgrade guard does not fire -- freeform evidence generation is
unaffected.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as preflight  # noqa: E402

_classify_anchor_scope_reframe = preflight._classify_anchor_scope_reframe
_structured_anchor_payload_present_but_invalid = (
    preflight._structured_anchor_payload_present_but_invalid
)

TARGET_REPO = "squne121/loop-protocol"
TARGET_ISSUE = 2053


def _comment_payload(author_association="OWNER"):
    return {
        "id": 1,
        "author_association": author_association,
        "user": {"login": "owner-user"},
        "issue_url": f"https://api.github.com/repos/{TARGET_REPO}/issues/{TARGET_ISSUE}",
    }


def _comment_payload_with_timestamps(*, created_at, updated_at, author_association="OWNER"):
    payload = _comment_payload(author_association=author_association)
    payload["created_at"] = created_at
    payload["updated_at"] = updated_at
    return payload


def _anchor_url(comment_id=1):
    return f"https://github.com/{TARGET_REPO}/issues/{TARGET_ISSUE}#issuecomment-{comment_id}"


def test_invalid_structured_anchor_does_not_downgrade_to_freeform():
    # GIVEN a structured payload present but schema-invalid (missing
    # required fields -- `target` omitted entirely).
    invalid_yaml_block = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "decision: approve_scope_delta\n"
        "```\n"
    )
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload(),
        anchor_body=invalid_yaml_block,
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    assert decision["status"] == "fail_closed"
    assert decision["reason"].startswith("schema_invalid:") or "target" in decision["reason"]

    # THEN the downgrade guard fires for a present-but-invalid payload.
    assert _structured_anchor_payload_present_but_invalid(decision) is True


def test_wrong_target_structured_anchor_does_not_downgrade_to_freeform():
    # GIVEN a structured payload targeting a DIFFERENT issue number.
    wrong_target_yaml = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n"
        f"  repo: {TARGET_REPO}\n"
        "  issue_number: 1\n"
        "decision: approve_scope_delta\n"
        "allowed_path_deltas:\n"
        "  - some/path.txt\n"
        "rationale: wrong target test\n"
        "required_rerun:\n"
        "  - refinement_preflight\n"
        "```\n"
    )
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload(),
        anchor_body=wrong_target_yaml,
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    assert decision["status"] == "fail_closed"
    assert decision["reason"].startswith("wrong_issue_number:")

    # THEN the downgrade guard fires -- wrong-target is not "no payload".
    assert _structured_anchor_payload_present_but_invalid(decision) is True


def test_no_structured_payload_keeps_legitimate_freeform_lane_open():
    # GIVEN a freeform review comment with NO ANCHOR_SCOPE_REFRAME_V1 payload
    # at all (e.g. Issue #1270's Revised Acceptance Criteria comment shape).
    freeform_body = "## Revised Acceptance Criteria\n- AC21: do the thing\n"
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload(),
        anchor_body=freeform_body,
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    # #2156 AC2: genuine absence (no ```yaml fence at all) classifies as
    # not_applicable (freeform lane continues), not fail_closed.
    assert decision["status"] == "not_applicable"
    assert decision["reason"] == "no_anchor_scope_reframe_v1_payload"

    # THEN the downgrade guard does NOT fire -- freeform evidence generation
    # from the same comment remains the legitimate lane.
    assert _structured_anchor_payload_present_but_invalid(decision) is False


def test_untrusted_author_association_does_not_trigger_downgrade_guard():
    # GIVEN an untrusted author association (independent of payload
    # presence/validity -- already fail-closed downstream by
    # classify_scope_delta_authority()'s own author-association check).
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload(author_association="NONE"),
        anchor_body="no payload here",
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    assert decision["status"] == "fail_closed"
    assert decision["reason"].startswith("untrusted_author_association:")

    # THEN the downgrade guard does not classify this as a
    # present-but-invalid structured payload case.
    assert _structured_anchor_payload_present_but_invalid(decision) is False


def test_stale_structured_anchor_edited_after_creation_does_not_downgrade_to_freeform():
    """#2053 P2 fix-delta (iteration 3, OWNER PR review): a structured
    ANCHOR_SCOPE_REFRAME_V1 anchor whose source generation/body revision no
    longer matches current state (the comment was edited after it was
    posted -- created_at != updated_at) is genuinely STALE, not merely
    invalid or wrong-target. It must fail closed with a "stale:" reason and
    must not be reinterpreted as a legitimate freeform directive from the
    same (edited) comment body.
    """
    valid_yaml_block = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n"
        f"  repo: {TARGET_REPO}\n"
        f"  issue_number: {TARGET_ISSUE}\n"
        "decision: approve_scope_delta\n"
        "allowed_path_deltas:\n"
        "  - some/path.txt\n"
        "rationale: stale test\n"
        "required_rerun:\n"
        "  - refinement_preflight\n"
        "```\n"
    )
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload_with_timestamps(
            created_at="2026-08-01T00:00:00Z",
            updated_at="2026-08-05T00:00:00Z",
        ),
        anchor_body=valid_yaml_block,
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    assert decision["status"] == "fail_closed"
    assert decision["reason"].startswith("stale:")

    # THEN the downgrade guard fires -- staleness is not "no payload".
    assert _structured_anchor_payload_present_but_invalid(decision) is True


def test_unedited_structured_anchor_with_matching_timestamps_is_not_stale():
    """GIVEN the same valid structured anchor, but with created_at ==
    updated_at (never edited)
    THEN the stale check does not fire -- the anchor is approved normally.
    """
    valid_yaml_block = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n"
        f"  repo: {TARGET_REPO}\n"
        f"  issue_number: {TARGET_ISSUE}\n"
        "decision: approve_scope_delta\n"
        "allowed_path_deltas:\n"
        "  - some/path.txt\n"
        "rationale: unedited test\n"
        "required_rerun:\n"
        "  - refinement_preflight\n"
        "```\n"
    )
    decision = _classify_anchor_scope_reframe(
        comment_payload=_comment_payload_with_timestamps(
            created_at="2026-08-01T00:00:00Z",
            updated_at="2026-08-01T00:00:00Z",
        ),
        anchor_body=valid_yaml_block,
        repo=TARGET_REPO,
        issue_number=TARGET_ISSUE,
        anchor_url=_anchor_url(),
    )
    assert decision["status"] == "approved_by_trusted_anchor"
