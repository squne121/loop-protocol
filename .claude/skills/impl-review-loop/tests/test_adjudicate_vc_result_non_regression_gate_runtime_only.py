"""Issue #2467 AC2/AC3: a producer-authorized `runtime_only` skip is only
adjudicated nonblocking PASS when the current-head independent binding
(Issue / PR / current head / reviewed head / diff head / Issue body digest /
source integrity) is fully satisfied. A baseline skip declaration alone --
even when echoed unchanged into the "current" payload -- must never be
sufficient (Issue #2467 Outcome: "baseline skip 単独では nonblocking PASS に
ならない"). artifact / receipt / TEST_VERDICT are optional diagnostic
provenance for runtime_only (unlike pr_review_only) and their absence is not
itself a blocking condition (Issue #2467 In Scope).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)

_spec = importlib.util.spec_from_file_location(
    "adjudicate_vc_result_non_regression_gate_runtime_only", SCRIPT_PATH
)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


HEAD = "c" * 40
STALE_HEAD = "d" * 40
BODY_SHA = "sha256:" + "f" * 64
ALLOWED_PATH = ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"
ISSUE_NUMBER = 2467
PR_NUMBER = 2469


def _runtime_only_item(ac: str, *, command_hash: str, exit_code: int | None = None) -> dict[str, Any]:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "echo runtime-only-fixture",
        "runner": "skipped",
        "exit_code": exit_code,
        "classification": "skipped",
        "category": "preflight_scope_runtime_only",
        "decision": "go",
        "scope_class": "runtime_only",
        "verification_owner": "impl-review-loop",
        "deferred_reason": "VC marked runtime_only; verification deferred to post-implementation runtime",
        "runtime_verification_required": True,
        "failure_keys": [],
    }


def _regular_item(
    ac: str,
    *,
    command_hash: str,
    classification: str,
    decision: str = "go",
    exit_code: int | None,
) -> dict[str, Any]:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "raw_command": "rg -q fixture tracked.txt",
        "runner": "exec",
        "exit_code": exit_code,
        "classification": classification,
        "category": "regression_gate",
        "decision": decision,
        "scope_class": None,
        "failure_keys": [],
    }


def _contract_snapshot(
    items: list[dict[str, Any]], *, body_sha256: str = BODY_SHA, status: str = "go"
) -> dict[str, Any]:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": status,
        "body_sha256": body_sha256,
        "checks": {"vc_preflight": {"classifications": items}},
    }


def _current_vc_result(
    items: list[dict[str, Any]],
    *,
    issue: Any = ISSUE_NUMBER,
    head_sha: str = HEAD,
    reviewed_head_sha: str = HEAD,
    body_sha256: str = BODY_SHA,
    status: str = "pass",
    errors: list[str] | None = None,
    fallback_detected: bool = False,
    human_review_required: bool = False,
    stop_condition_triggered: bool = False,
    generated_at: str | None = "2026-09-02T00:00:00Z",
) -> dict[str, Any]:
    return {
        "schema": "baseline_vc_preflight/v1",
        "issue": issue,
        "generated_at": generated_at,
        "status": status,
        "errors": errors if errors is not None else [],
        "fallback_detected": fallback_detected,
        "human_review_required": human_review_required,
        "stop_condition_triggered": stop_condition_triggered,
        "source": {"body_sha256": body_sha256},
        "head_sha": head_sha,
        "reviewed_head_sha": reviewed_head_sha,
        "results": items,
    }


def _diff_summary(
    *, head_sha: str = HEAD, pr_number: Any = PR_NUMBER, changed_paths: list[str] | None = None
) -> dict[str, Any]:
    return {
        "changed_paths": changed_paths if changed_paths is not None else [ALLOWED_PATH],
        "head_sha": head_sha,
        "pr_number": pr_number,
    }


# --- AC2/AC3: current-head independent binding is required -----------------


def test_non_regression_scope_runtime_only_requires_current_head_binding():
    # GIVEN a producer-authorized runtime_only skip in both baseline and
    # current, but the current head does NOT match the diff head (no fresh
    # current-head independent binding was actually established)
    # WHEN adjudicated
    # THEN the AC is NOT resolved nonblocking PASS -- it fails closed
    item = _runtime_only_item("AC1", command_hash="sha256:" + "1" * 64)
    baseline = _contract_snapshot([item])
    current = _current_vc_result([item], head_sha=HEAD, reviewed_head_sha=HEAD)
    drifted_diff_summary = _diff_summary(head_sha=STALE_HEAD)

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=drifted_diff_summary,
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_head_binding_mismatch"]

    # WHEN the current-head independent binding is fully established (diff
    # head matches current/reviewed head, contract snapshot is go, Issue/PR
    # binding present)
    # THEN the same skip now resolves nonblocking PASS
    certified = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=_diff_summary(head_sha=HEAD),
        allowed_paths=[ALLOWED_PATH],
    )

    assert certified["overall_status"] == "pass"
    assert certified["blocking"] is False
    assert certified["per_ac"][0]["reason_code"] == "runtime_only_current_head_binding_pass"


def test_baseline_skip_alone_does_not_certify_nonblocking_pass():
    # GIVEN the SAME baseline skip envelope echoed unchanged as "current"
    # (i.e. no actual current-head certification: contract snapshot is not
    # go)
    # WHEN adjudicated
    # THEN nonblocking PASS is refused (baseline skip alone is insufficient)
    item = _runtime_only_item("AC1", command_hash="sha256:" + "2" * 64)
    baseline = _contract_snapshot([item], status="human_judgment")
    current = _current_vc_result([item])

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_contract_not_go"]


def test_runtime_only_missing_artifact_or_test_verdict_is_not_blocking():
    # GIVEN a fully-certified runtime_only current-head binding with NO
    # test_verdict/artifact/receipt supplied at all
    # WHEN adjudicated
    # THEN the missing artifact/receipt/TEST_VERDICT is not itself a
    # blocking condition (Issue #2467 In Scope: optional diagnostic
    # provenance only)
    item = _runtime_only_item("AC1", command_hash="sha256:" + "3" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
        test_verdict=None,
    )

    assert result["overall_status"] == "pass"
    assert result["blocking"] is False


def test_runtime_only_missing_issue_number_binding_fails_closed():
    # GIVEN current_vc_result has no issue number bound
    # WHEN adjudicated
    # THEN it fails closed on the Issue binding requirement
    item = _runtime_only_item("AC1", command_hash="sha256:" + "4" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item], issue=None),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_issue_number_missing"]


def test_runtime_only_missing_pr_number_binding_fails_closed():
    # GIVEN diff_summary has no PR number bound
    # WHEN adjudicated
    # THEN it fails closed on the PR binding requirement
    item = _runtime_only_item("AC1", command_hash="sha256:" + "5" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(pr_number=None),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_pr_number_missing"]


def test_runtime_only_stale_contract_body_digest_fails_closed():
    # GIVEN the current_vc_result's source body digest does not match the
    # live contract snapshot digest (stale Issue body binding)
    # WHEN adjudicated
    # THEN it fails closed
    item = _runtime_only_item("AC1", command_hash="sha256:" + "6" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item], body_sha256="sha256:" + "1" * 64),
        current_vc_result=_current_vc_result([item], body_sha256="sha256:" + "2" * 64),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_source_body_sha256_mismatch"]


def test_runtime_only_fallback_detected_fails_closed():
    # GIVEN the current_vc_result reports a fallback was detected
    # WHEN adjudicated
    # THEN it fails closed regardless of the skip envelope validity
    item = _runtime_only_item("AC1", command_hash="sha256:" + "7" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item], fallback_detected=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_fallback_detected"]


def test_runtime_only_human_review_required_fails_closed():
    item = _runtime_only_item("AC1", command_hash="sha256:" + "8" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item], human_review_required=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_human_review_required"]


def test_runtime_only_stop_condition_triggered_fails_closed():
    item = _runtime_only_item("AC1", command_hash="sha256:" + "9" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item], stop_condition_triggered=True),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_stop_condition_triggered"]


def test_runtime_only_changed_path_outside_allowed_paths_fails_closed():
    # GIVEN the diff summary changed an out-of-scope path not covered by
    # Allowed Paths
    # WHEN adjudicated
    # THEN source-integrity certification fails closed
    item = _runtime_only_item("AC1", command_hash="sha256:" + "a" * 64)

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([item]),
        current_vc_result=_current_vc_result([item]),
        diff_summary=_diff_summary(changed_paths=["outside/scope.py"]),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_changed_paths_not_certified"]


# --- AC5 non-regression companion: coverage completeness --------------------


def test_runtime_only_requires_complete_coverage_rejects_missing_skip():
    # GIVEN baseline has a regular AC plus a runtime_only skip AC, but
    # current only reports the regular AC (skip AC missing entirely)
    # WHEN adjudicated
    # THEN it fails closed via the runtime_only-specific coverage mismatch
    # (distinguished from pr_review_only_coverage_mismatch, Issue #2467 AC5)
    baseline_items = [
        _regular_item(
            "AC1", command_hash="sha256:" + "b" * 64, classification="expected_fail", exit_code=1
        ),
        _runtime_only_item("AC2", command_hash="sha256:" + "c" * 64),
    ]
    current = _current_vc_result(
        [
            _regular_item(
                "AC1", command_hash="sha256:" + "b" * 64, classification="expected_pass", exit_code=0
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_coverage_mismatch"]


def test_runtime_only_current_authorization_mismatch_is_distinct_error():
    # GIVEN baseline declares a canonical runtime_only skip, but the current
    # payload's matching AC/command_hash entry is NOT the canonical skip
    # envelope (e.g. it silently dropped runtime_verification_required)
    # WHEN adjudicated
    # THEN the mismatch is reported via the runtime_only-specific
    # authorization-mismatch error, not silently accepted
    baseline_item = _runtime_only_item("AC1", command_hash="sha256:" + "d" * 64)
    tampered_current_item = dict(baseline_item)
    tampered_current_item["runtime_verification_required"] = False

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot([baseline_item]),
        current_vc_result=_current_vc_result([tampered_current_item]),
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["blocking"] is True
    assert result["errors"] == ["runtime_only_current_authorization_mismatch:AC1"]


def test_pr_review_only_coverage_mismatch_is_unaffected_by_runtime_only_addition():
    # Non-regression: a pure pr_review_only coverage-mismatch scenario (no
    # runtime_only involved at all) must keep returning the exact original
    # error string it always has.
    baseline_items = [
        _regular_item(
            "AC1", command_hash="sha256:" + "e" * 64, classification="expected_fail", exit_code=1
        ),
        {
            "ac": "AC2",
            "command_hash": "sha256:" + "f" * 64,
            "raw_command": "rg -q fixture tracked.txt",
            "runner": "skipped",
            "exit_code": None,
            "classification": "skipped",
            "category": "preflight_scope_pr_review_only",
            "decision": "go",
            "scope_class": "pr_review_only",
            "verification_owner": "pr-review-judge",
            "deferred_reason": "VC marked pr_review_only; verification deferred to PR review",
            "runtime_verification_required": False,
            "failure_keys": [],
        },
    ]
    current = _current_vc_result(
        [
            _regular_item(
                "AC1", command_hash="sha256:" + "e" * 64, classification="expected_pass", exit_code=0
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=_contract_snapshot(baseline_items),
        current_vc_result=current,
        diff_summary=_diff_summary(),
        allowed_paths=[ALLOWED_PATH],
    )

    assert result["overall_status"] != "pass"
    assert result["errors"] == ["pr_review_only_coverage_mismatch"]
