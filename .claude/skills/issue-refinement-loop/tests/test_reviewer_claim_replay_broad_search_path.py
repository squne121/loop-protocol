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
- AC3: a readiness error backed by rule_id/source_check/category for the
  same body hash makes analyze() return deterministic_fail_confirmed /
  should_consume_iteration=True / checker_gap=False.
- AC4: a direct vc_preflight_result producer shape (status/classification/
  category/decision variants, same body_sha256) is also deterministic
  backed.
- AC5: a stale or missing vc_preflight_result.source.body_sha256 fails
  closed with input_or_runtime_error and is not used as broad-path
  evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import (  # noqa: E402
    REVIEWER_CHECKER_TAXONOMY_V1,
    analyze,
    normalize_taxonomy_key,
)

BODY_SHA256 = "sha256:body-broad-path"

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": BODY_SHA256,
    "errors": [],
}

READINESS_VCP_BROAD_SEARCH_PATH_UN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": BODY_SHA256,
    "errors": [
        {
            "rule_id": "VCP_BROAD_SEARCH_PATH_UN",
            "source_check": "baseline_vc_preflight",
            "category": "broad_search_path_unbounded",
            "line_start": 30,
            "line_end": 30,
        }
    ],
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


def _producer_vc_preflight_result(
    *,
    body_sha256: str = BODY_SHA256,
    status: str = "blocked",
    result_overrides: dict | None = None,
    source_overrides: dict | None = None,
) -> dict:
    result = {
        "ac": "AC1",
        "line": 30,
        "raw_command": "rg -n \"pattern\" .",
        "command_hash": "sha256:command-broad-path",
        "classification": "broad_search_path_unbounded",
        "category": "broad_search_path_unbounded",
        "decision": "blocked",
        "scope_class": "broad_path_unexpected",
        "confidence": "high",
    }
    if result_overrides:
        result.update(result_overrides)

    source = {"kind": "body_file", "body_sha256": body_sha256}
    if source_overrides:
        source.update(source_overrides)

    return {
        "schema": "baseline_vc_preflight/v1",
        "source": source,
        "status": status,
        "results": [result],
        "errors": [],
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
# AC3: readiness error (rule_id/source_check/category) backs
# deterministic_fail_confirmed / should_consume_iteration=True /
# checker_gap=False, for each reviewer code alias.
# --------------------------------------------------------------------------


def test_vcp_broad_search_path_un_maps_to_deterministic_fail_confirmed():
    for alias in (
        "vcp_broad_search_path_un",
        "VCP_BROAD_SEARCH_PATH_UN",
        "broad_search_path_unbounded",
    ):
        result, _ = analyze(
            review_result=_compact_review(code=alias),
            readiness_result=READINESS_VCP_BROAD_SEARCH_PATH_UN,
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


# --------------------------------------------------------------------------
# AC4: direct vc_preflight_result producer shape (status/classification/
# category/decision variants), same body_sha256, is deterministic backed.
# --------------------------------------------------------------------------


def test_direct_vc_preflight_result_producer_shape_deterministic_backed():
    preflight = _producer_vc_preflight_result(
        status="blocked",
        result_overrides={
            "classification": "broad_search_path_unbounded",
            "category": "broad_search_path_unbounded",
            "decision": "blocked",
        },
    )
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
    assert blocker["evidence"][0]["classification"] == "broad_search_path_unbounded"
    assert blocker["evidence"][0]["decision"] == "blocked"


def test_direct_vc_preflight_result_producer_shape_requires_blocked_decision():
    # A matching category but decision != "blocked" must not be treated as
    # broad-path evidence (mirrors the existing unexpected_pass semantics).
    preflight = _producer_vc_preflight_result(
        status="blocked",
        result_overrides={
            "classification": "broad_search_path_unbounded",
            "category": "broad_search_path_unbounded",
            "decision": "go",
        },
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


# --------------------------------------------------------------------------
# AC5: stale or missing vc_preflight_result.source.body_sha256 fails closed
# and is not used as broad-path evidence.
# --------------------------------------------------------------------------


def test_stale_or_missing_body_sha256_fails_closed():
    stale_preflight = _producer_vc_preflight_result(
        body_sha256="sha256:stale-body",
        result_overrides={
            "classification": "broad_search_path_unbounded",
            "category": "broad_search_path_unbounded",
            "decision": "blocked",
        },
    )
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

    missing_preflight = _producer_vc_preflight_result(
        source_overrides={"body_sha256": ""},
        result_overrides={
            "classification": "broad_search_path_unbounded",
            "category": "broad_search_path_unbounded",
            "decision": "blocked",
        },
    )
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
