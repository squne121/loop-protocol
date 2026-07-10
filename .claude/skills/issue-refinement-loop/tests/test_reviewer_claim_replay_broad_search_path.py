"""
test_reviewer_claim_replay_broad_search_path.py

Issue #1406: reviewer_claim_replay.py's REVIEWER_CHECKER_TAXONOMY_V1 gains a
`broad_search_path_unbounded` entry so that a reviewer blocker pointing at a
Verification Command's unbounded `rg` broad search path
(`readiness_rule_ids: ["VCP_BROAD_SEARCH_PATH_UN"]` /
`readiness_categories: ["broad_search_path_unbounded"]`, as emitted by
`contract_readiness_check.py` from `baseline_vc_preflight.py`'s
`broad_search_path_unbounded` category) is classified as
`deterministic_fail_confirmed` instead of `checker_gap`.

- AC1: taxonomy has an entry_id == "broad_search_path_unbounded" entry with
  the expected readiness_rule_ids / source_check / readiness_categories /
  source_check / deterministic_checks / non-colliding domain_keys.
- AC2: reviewer code aliases (vcp_broad_search_path_un /
  VCP_BROAD_SEARCH_PATH_UN / broad_search_path_unbounded) all normalize to
  the same taxonomy entry.
- AC3: a readiness error produced by contract_readiness_check.py's
  map_preflight_result_to_errors() from a real broad-path preflight result
  (rule_id/source_check/category + producer-shape source_payload), for the
  same body hash, makes analyze() return deterministic_fail_confirmed /
  should_consume_iteration=True / checker_gap=False.
- AC4: a direct vc_preflight_result producer shape (schema/status/
  classification/category/decision/scope_class), same body_sha256, is also
  deterministic backed.
- AC5: a stale or missing vc_preflight_result.source.body_sha256 fails
  closed with input_or_runtime_error and is not used as broad-path
  evidence.
- AC8 (PR #1412 review, Blocker 1): a direct vc_preflight_result with an
  unsupported/spoofed top-level schema fails closed (ValueError ->
  input_or_runtime_error at the CLI layer); classification/scope_class
  mismatches are not treated as broad-path evidence.
- AC9 (PR #1412 review, Blocker 2 / High): readiness errors are only
  adopted as broad-path evidence when rule_id AND category both match (not
  either/or -- guards against the 20-char rule_id truncation collision) and
  source_payload reflects the real producer shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

CONTRACT_REVIEW_SCRIPTS_DIR = (
    SKILL_ROOT.parent / "issue-contract-review" / "scripts"
)
sys.path.insert(0, str(CONTRACT_REVIEW_SCRIPTS_DIR))

from reviewer_claim_replay import (  # noqa: E402
    REVIEWER_CHECKER_TAXONOMY_V1,
    analyze,
    normalize_taxonomy_key,
)
from contract_readiness_check import map_preflight_result_to_errors  # noqa: E402

BODY_SHA256 = "sha256:body-broad-path"

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": BODY_SHA256,
    "errors": [],
}


def _producer_vc_preflight_result(
    *,
    body_sha256: str = BODY_SHA256,
    status: str = "blocked",
    schema: str = "baseline_vc_preflight/v1",
    result_overrides: dict | None = None,
    source_overrides: dict | None = None,
) -> dict:
    """Build a vc_preflight_result matching baseline_vc_preflight.py's real
    producer shape for a broad_search_path_unbounded blocked item. Callers
    override individual fields to exercise spoof/negative cases."""
    result = {
        "ac": "AC1",
        "line": 30,
        "raw_command": 'rg -n "pattern" .',
        "command_hash": "sha256:command-broad-path",
        "classification": "blocked",
        "category": "broad_search_path_unbounded",
        "decision": "blocked",
        "scope_class": "baseline_fail_expected",
        "confidence": "high",
    }
    if result_overrides:
        result.update(result_overrides)

    source = {"kind": "body_file", "body_sha256": body_sha256}
    if source_overrides:
        source.update(source_overrides)

    payload: dict = {
        "source": source,
        "status": status,
        "results": [result],
        "errors": [],
    }
    if schema is not None:
        payload["schema"] = schema
    return payload


def _producer_readiness_result(
    *,
    body_sha256: str = BODY_SHA256,
    preflight_result: dict | None = None,
) -> dict:
    """Build a readiness result via the real
    contract_readiness_check.map_preflight_result_to_errors() producer,
    instead of a hand-rolled fixture (PR #1412 review, Medium item)."""
    preflight_result = preflight_result or _producer_vc_preflight_result(body_sha256=body_sha256)
    errors, aggregate = map_preflight_result_to_errors(preflight_result)
    return {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": body_sha256,
        "status": aggregate,
        "errors": errors,
    }


def _compact_review(*, code: str, body_sha256: str = BODY_SHA256) -> dict:
    return {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1406",
        "body_sha256": body_sha256,
        "blocking_issues": [{"code": code, "message": "rg broad search path unbounded"}],
        "structured_blockers": [],
        "findings": [],
    }


# --------------------------------------------------------------------------
# AC1: taxonomy entry shape.
# --------------------------------------------------------------------------


def test_taxonomy_has_broad_search_path_entry():
    entries_by_id = {entry["entry_id"]: entry for entry in REVIEWER_CHECKER_TAXONOMY_V1}
    assert "broad_search_path_unbounded" in entries_by_id
    entry = entries_by_id["broad_search_path_unbounded"]

    assert entry["readiness_rule_ids"] == ["VCP_BROAD_SEARCH_PATH_UN"]
    assert entry["readiness_rule_id_source_check"] == "baseline_vc_preflight"
    assert entry["readiness_categories"] == ["broad_search_path_unbounded"]
    assert entry["readiness_category_source_check"] == "baseline_vc_preflight"
    assert entry["deterministic_checks"] == []

    other_domain_keys = {
        key
        for entry_id, other_entry in entries_by_id.items()
        if entry_id != "broad_search_path_unbounded"
        for key in other_entry["domain_keys"]
    }
    assert not set(entry["domain_keys"]) & other_domain_keys


# --------------------------------------------------------------------------
# AC2: reviewer code alias normalization.
# --------------------------------------------------------------------------


def test_reviewer_code_variants_normalize_to_broad_search_path_unbounded():
    for alias in (
        "vcp_broad_search_path_un",
        "VCP_BROAD_SEARCH_PATH_UN",
        "broad_search_path_unbounded",
    ):
        assert normalize_taxonomy_key(alias) == "broad_search_path_unbounded", alias


# --------------------------------------------------------------------------
# AC3 / AC9: readiness error produced by the real
# map_preflight_result_to_errors() producer (rule_id + source_check +
# category + producer-shape source_payload) backs
# deterministic_fail_confirmed / should_consume_iteration=True /
# checker_gap=False, for each reviewer code alias.
# --------------------------------------------------------------------------


def test_vcp_broad_search_path_un_maps_to_deterministic_fail_confirmed():
    readiness_result = _producer_readiness_result()
    assert readiness_result["status"] == "needs_fix"
    error = readiness_result["errors"][0]
    assert error["rule_id"] == "VCP_BROAD_SEARCH_PATH_UN"
    assert error["source_check"] == "baseline_vc_preflight"
    assert error["category"] == "broad_search_path_unbounded"
    assert error["source_payload"]["classification"] == "blocked"
    assert error["source_payload"]["scope_class"] == "baseline_fail_expected"

    for alias in (
        "vcp_broad_search_path_un",
        "VCP_BROAD_SEARCH_PATH_UN",
        "broad_search_path_unbounded",
    ):
        result, _ = analyze(
            review_result=_compact_review(code=alias),
            readiness_result=readiness_result,
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state={},
        )
        assert result["verdict"] == "deterministic_fail_confirmed", alias
        assert result["verdict_detail_v1"] == "deterministic_fail_confirmed", alias
        assert result["should_consume_iteration"] is True, alias
        blocker = result["blockers"][0]
        assert blocker["normalized_kind"] == "broad_search_path_unbounded", alias
        assert blocker["checker_gap"] is False, alias
        assert blocker["deterministic_backed"] is True, alias
        assert blocker["evidence"][0]["rule_id"] == "VCP_BROAD_SEARCH_PATH_UN", alias
        assert blocker["evidence"][0]["source_check"] == "baseline_vc_preflight", alias


def test_mapping_broad_path_preflight_result_yields_needs_fix_aggregate():
    """Mapping row from the review's minimal test matrix: feeding a
    broad-path preflight result through map_preflight_result_to_errors()
    directly must aggregate to needs_fix (not human_judgment)."""
    preflight = _producer_vc_preflight_result()
    errors, aggregate = map_preflight_result_to_errors(preflight)
    assert aggregate == "needs_fix"
    assert len(errors) == 1
    assert errors[0]["category"] == "broad_search_path_unbounded"


# --------------------------------------------------------------------------
# Readiness negative cases (AC9 / High item): rule_id-only or category-only
# matches, or a missing/mismatched source_payload, must NOT be adopted as
# broad-path evidence.
# --------------------------------------------------------------------------


def test_readiness_rule_id_only_match_category_mismatch_not_adopted():
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "status": "needs_fix",
        "errors": [
            {
                "rule_id": "VCP_BROAD_SEARCH_PATH_UN",
                "source_check": "baseline_vc_preflight",
                "category": "broad_search_path_unrelated",
                "source_payload": {
                    "classification": "blocked",
                    "category": "broad_search_path_unrelated",
                    "decision": "blocked",
                    "scope_class": "baseline_fail_expected",
                },
            }
        ],
    }
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=readiness_result,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


def test_readiness_category_only_match_rule_id_mismatch_not_adopted():
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "status": "needs_fix",
        "errors": [
            {
                "rule_id": "VCP_SOMETHING_ELSE",
                "source_check": "baseline_vc_preflight",
                "category": "broad_search_path_unbounded",
                "source_payload": {
                    "classification": "blocked",
                    "category": "broad_search_path_unbounded",
                    "decision": "blocked",
                    "scope_class": "baseline_fail_expected",
                },
            }
        ],
    }
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=readiness_result,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


def test_readiness_missing_source_payload_not_adopted():
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "status": "needs_fix",
        "errors": [
            {
                "rule_id": "VCP_BROAD_SEARCH_PATH_UN",
                "source_check": "baseline_vc_preflight",
                "category": "broad_search_path_unbounded",
            }
        ],
    }
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=readiness_result,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


def test_readiness_source_payload_classification_mismatch_not_adopted():
    readiness_result = {
        "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
        "body_sha256": BODY_SHA256,
        "status": "needs_fix",
        "errors": [
            {
                "rule_id": "VCP_BROAD_SEARCH_PATH_UN",
                "source_check": "baseline_vc_preflight",
                "category": "broad_search_path_unbounded",
                "source_payload": {
                    "classification": "expected_fail",
                    "category": "broad_search_path_unbounded",
                    "decision": "go",
                    "scope_class": "baseline_fail_expected",
                },
            }
        ],
    }
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=readiness_result,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


# --------------------------------------------------------------------------
# AC4 / AC8: direct vc_preflight_result producer shape (schema/status/
# classification/category/decision/scope_class), same body_sha256, is
# deterministic backed; mismatches are not adopted or fail closed.
# --------------------------------------------------------------------------


def test_direct_vc_preflight_result_producer_shape_deterministic_backed():
    preflight = _producer_vc_preflight_result()
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["should_consume_iteration"] is True
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "broad_search_path_unbounded"
    assert blocker["checker_gap"] is False
    assert blocker["evidence"][0]["source_check"] == "baseline_vc_preflight"
    assert blocker["evidence"][0]["category"] == "broad_search_path_unbounded"
    assert blocker["evidence"][0]["classification"] == "blocked"
    assert blocker["evidence"][0]["decision"] == "blocked"
    assert blocker["evidence"][0]["scope_class"] == "baseline_fail_expected"


def test_direct_vc_preflight_result_producer_shape_requires_blocked_decision():
    # A matching category but decision != "blocked" must not be treated as
    # broad-path evidence (mirrors the existing unexpected_pass semantics).
    preflight = _producer_vc_preflight_result(result_overrides={"decision": "go"})
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


def test_direct_vc_preflight_result_classification_mismatch_not_adopted():
    # PR #1412 review (Blocker 1): classification="broad_search_path_unbounded"
    # (the reviewer-facing category label) is not a valid producer
    # `classification` value -- the real producer always emits
    # classification="blocked" for a blocked VC. This spoofed shape must not
    # be adopted as evidence.
    preflight = _producer_vc_preflight_result(
        result_overrides={"classification": "broad_search_path_unbounded"}
    )
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False
    assert result["blockers"][0]["evidence"] == []


def test_direct_vc_preflight_result_scope_class_mismatch_not_adopted():
    for bad_scope_class in ("broad_path_unexpected", "regression_gate", ""):
        preflight = _producer_vc_preflight_result(
            result_overrides={"scope_class": bad_scope_class}
        )
        result, _ = analyze(
            review_result=_compact_review(code="broad_search_path_unbounded"),
            readiness_result=READINESS_CLEAN,
            vc_syntax_result=None,
            vc_preflight_result=preflight,
            previous_state={},
        )
        assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker", bad_scope_class
        assert result["blockers"][0]["evidence"] == [], bad_scope_class


def test_direct_vc_preflight_result_unsupported_schema_fails_closed():
    # PR #1412 review (Blocker 1): a spoofed/unknown top-level schema on the
    # vc_preflight_result artifact is an artifact contract violation and
    # must fail closed (raise), not silently fall through to "unbacked".
    preflight = _producer_vc_preflight_result(schema="not-baseline-vc-preflight")
    with pytest.raises(ValueError):
        analyze(
            review_result=_compact_review(code="broad_search_path_unbounded"),
            readiness_result=READINESS_CLEAN,
            vc_syntax_result=None,
            vc_preflight_result=preflight,
            previous_state={},
        )


def test_direct_vc_preflight_result_missing_schema_fails_closed():
    preflight = _producer_vc_preflight_result(schema=None)
    with pytest.raises(ValueError):
        analyze(
            review_result=_compact_review(code="broad_search_path_unbounded"),
            readiness_result=READINESS_CLEAN,
            vc_syntax_result=None,
            vc_preflight_result=preflight,
            previous_state={},
        )


# --------------------------------------------------------------------------
# AC5: stale or missing vc_preflight_result.source.body_sha256 fails closed
# and is not used as broad-path evidence.
# --------------------------------------------------------------------------


def test_stale_or_missing_body_sha256_fails_closed():
    stale_preflight = _producer_vc_preflight_result(body_sha256="sha256:stale-body")
    result, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=stale_preflight,
        previous_state={},
    )
    assert result["verdict"] == "input_or_runtime_error"
    assert result["verdict_detail_v1"] == "input_or_runtime_error"
    assert result["should_consume_iteration"] is False
    assert result["reason_code"] == "vc_preflight_body_sha_mismatch"

    missing_preflight = _producer_vc_preflight_result(source_overrides={"body_sha256": ""})
    result_missing, _ = analyze(
        review_result=_compact_review(code="broad_search_path_unbounded"),
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=missing_preflight,
        previous_state={},
    )
    assert result_missing["verdict"] == "input_or_runtime_error"
    assert result_missing["verdict_detail_v1"] == "input_or_runtime_error"
    assert result_missing["should_consume_iteration"] is False
    assert result_missing["reason_code"] == "vc_preflight_body_sha_missing"
