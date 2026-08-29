"""Issue #2397 -- `build_compact_v2()` / `validate_compact_v2()` must be able
to produce and accept `NEXT_ACTION: human_judgment_required`.

Background (PR #2391 owner P0-1,
https://github.com/squne121/loop-protocol/pull/2391#issuecomment-5461056774):
`run_root_review_pipeline.route_canonical_step2_result()` routes the exact
triple `status: ok` + `verdict: needs-fix` + `next_action:
human_judgment_required` to `STEP_5_HUMAN_JUDGMENT_REQUIRED`, but the actual
V2 producer -- `reviewer_transport.build_compact_v2()` -- previously fixed
`next_action` to a bare `proceed`/`request_changes` two-way choice, so that
branch was unreachable from real producer output. This test module verifies:

* AC1: `build_compact_v2(failure_class="contract_readiness_human_judgment")`
  emits a wire whose `NEXT_ACTION` line is `human_judgment_required`.
* AC2: omitting `failure_class`, or passing an unrelated value, leaves the
  existing `proceed` / `request_changes` two-way behavior unchanged.
* AC3: `validate_compact_v2()` accepts a `NEXT_ACTION: human_judgment_required`
  wire as a valid V2 compact when `VERDICT` is `needs-fix`, and still
  rejects the inconsistent `VERDICT: approve` +
  `NEXT_ACTION: human_judgment_required` combination.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import reviewer_transport as transport  # noqa: E402

_BODY_SHA = "sha256:" + "a" * 64
_ARTIFACT_SHA = "sha256:" + "b" * 64
_ARTIFACT_RELATIVE = "2397/inv-1/attempt-001/compact_review_result_v2.json"


def _build(**overrides):
    kwargs = dict(
        verdict="needs-fix",
        summary="1 blocker(s)",
        blockers=1,
        reviewed_body_sha256=_BODY_SHA,
        attempt_id="inv-1",
        artifact_relative=_ARTIFACT_RELATIVE,
        artifact_sha256=_ARTIFACT_SHA,
    )
    kwargs.update(overrides)
    return transport.build_compact_v2(**kwargs)


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_given_contract_readiness_human_judgment_failure_class_when_built_then_next_action_is_human_judgment_required():
    raw = _build(failure_class=transport.FAILURE_CLASS_CONTRACT_READINESS_HUMAN_JUDGMENT)
    assert b"NEXT_ACTION: human_judgment_required\n" in raw
    result = transport.validate_compact_v2(raw, issue_number=2397, invocation_id="inv-1", attempt=1)
    assert result["validation_status"] == "valid", result["violations"]
    assert result["normalized_payload"]["NEXT_ACTION"] == "human_judgment_required"


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def test_given_no_failure_class_when_built_then_existing_proceed_request_changes_behavior_is_unchanged():
    needs_fix_raw = _build()
    assert b"NEXT_ACTION: request_changes\n" in needs_fix_raw

    approve_raw = _build(verdict="approve", summary="contract ready", blockers=0)
    assert b"NEXT_ACTION: proceed\n" in approve_raw


def test_given_unrelated_failure_class_when_built_then_existing_request_changes_behavior_is_unchanged():
    raw = _build(failure_class="some_other_reason_code")
    assert b"NEXT_ACTION: request_changes\n" in raw


def test_given_human_judgment_failure_class_with_approve_verdict_when_built_then_next_action_stays_proceed():
    # An approved contract never legitimately carries a readiness
    # `human_judgment` failure -- `failure_class` must not override
    # `NEXT_ACTION` for `verdict: approve` (would otherwise build an
    # inconsistent wire that `validate_compact_v2()` fails closed against).
    raw = _build(
        verdict="approve",
        summary="contract ready",
        blockers=0,
        failure_class=transport.FAILURE_CLASS_CONTRACT_READINESS_HUMAN_JUDGMENT,
    )
    assert b"NEXT_ACTION: proceed\n" in raw


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


def test_given_human_judgment_required_next_action_when_validated_then_accepted():
    raw = _build(failure_class=transport.FAILURE_CLASS_CONTRACT_READINESS_HUMAN_JUDGMENT)
    result = transport.validate_compact_v2(raw, issue_number=2397, invocation_id="inv-1", attempt=1)
    assert result["validation_status"] == "valid", result["violations"]


def test_given_approve_verdict_with_human_judgment_required_next_action_when_validated_then_rejected():
    raw = _build(failure_class=transport.FAILURE_CLASS_CONTRACT_READINESS_HUMAN_JUDGMENT)
    inconsistent = raw.replace(b"VERDICT: needs-fix", b"VERDICT: approve")
    result = transport.validate_compact_v2(inconsistent)
    assert result["validation_status"] == "invalid"
    assert {"code": "value_invalid"} in result["violations"]
