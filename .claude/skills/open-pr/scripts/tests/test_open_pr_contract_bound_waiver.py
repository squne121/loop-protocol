#!/usr/bin/env python3
"""Tests for the generic contract-bound overlap readback waiver (Issue #1509).

`open_pr.py`'s overlap preflight hard gate previously accepted an
overlap_readback_waiver only for a single hard-coded (repository,
linked_issue) pair fixed to Issue #1477 (`_load_verified_overlap_readback_
waiver` / `_has_only_fixed_readback_incomplete_blockers`, both unmodified
here and still fully covered by `test_open_pr_overlap_gate.py`).

This file covers the ADDITIONAL, generic consumer added for #1509
(`_load_verified_generic_overlap_readback_waiver` /
`_validate_generic_overlap_readback_waiver_schema` /
`_incomplete_candidates_match_generic_waiver` /
`_is_readback_incomplete_only_blocker`), which accepts a waiver
self-declared inside the SAME linked Issue's own live body for ANY
(repository, linked_issue) pair OTHER than the fixed #1477 binding, as long
as every field (repository, linked_issue, candidate issue_number /
updated_at / reason, expiry, approver) is verified against caller arguments,
fresh evidence, and a trusted `status: go` contract snapshot bound to the
same live body SHA.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import open_pr


class FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _generic_waiver(
    *,
    repository: str = "squne121/loop-protocol",
    linked_issue: int = 1503,
    candidates: list[dict] | None = None,
    expires_on: str = "2026-12-31",
    approved_by: str = "user_session",
) -> dict:
    return {
        "repository": repository,
        "linked_issue": linked_issue,
        "candidates": candidates
        if candidates is not None
        else [
            {
                "issue_number": 521,
                "updated_at": "2026-06-01T00:00:00Z",
                "reason": "readback_incomplete_missing_outcome_or_in_scope",
            }
        ],
        "expires_on": expires_on,
        "approved_by": approved_by,
    }


def _readback_incomplete_candidate(number: int, updated_at: str, reason: str) -> dict:
    return {
        "issue_number": number,
        "updated_at": updated_at,
        "readback_complete": False,
        "reasons": [reason],
    }


def _complete_c1_candidate(number: int) -> dict:
    return {
        "issue_number": number,
        "updated_at": "2026-06-01T00:00:00Z",
        "readback_complete": True,
        "policy_class": "C1",
        "reasons": ["readback_confirmed_disjoint"],
    }


def _waiver_live_body(waiver: dict) -> str:
    candidates_yaml = "\n".join(
        f"""    - issue_number: {c["issue_number"]}
      updated_at: "{c["updated_at"]}"
      reason: {c["reason"]}"""
        for c in waiver["candidates"]
    )
    return f"""## Machine-Readable Contract

```yaml
overlap_readback_waiver:
  repository: {waiver["repository"]}
  linked_issue: {waiver["linked_issue"]}
  candidates:
{candidates_yaml}
  expires_on: "{waiver["expires_on"]}"
  approved_by: {waiver["approved_by"]}
```
"""


def _snapshot_comment(
    body_sha256: str,
    linked_issue: int,
    *,
    status: str = "go",
    comment_id: int = 1,
    created_at: str = "2026-06-01T00:00:00Z",
) -> dict:
    return {
        "id": comment_id,
        "html_url": f"https://github.com/squne121/loop-protocol/issues/{linked_issue}#issuecomment-{comment_id}",
        "created_at": created_at,
        "updated_at": created_at,
        "author": "squne121",
        "author_id": 63350259,
        "author_type": "User",
        "author_association": "OWNER",
        "body": f"""```yaml
CONTRACT_REVIEW_RESULT_V1:
  status: {status}
  generated_at: "{created_at}"
  generated_by: issue-contract-review
  issue_url: https://github.com/squne121/loop-protocol/issues/{linked_issue}
  body_sha256: "{body_sha256}"
```
""",
    }


def _patch_live_waiver_readback(
    monkeypatch, linked_issue: int, body: str, comments: list[dict], error: str | None = None
) -> None:
    payload = {
        "body": body,
        "url": f"https://github.com/squne121/loop-protocol/issues/{linked_issue}",
    }
    monkeypatch.setattr(
        open_pr,
        "run_gh",
        lambda *args, **kwargs: FakeCompletedProcess(0, json.dumps(payload), ""),
    )
    monkeypatch.setattr(
        open_pr.contract_review_parser,
        "fetch_issue_comments",
        lambda issue, repo: (comments, error),
    )


# ---------------------------------------------------------------------------
# AC1: fresh evidence with exactly the waiver-declared stale (readback
# incomplete) candidate(s) and no other unresolved blocker -> proceeds.
# ---------------------------------------------------------------------------


def test_contract_bound_waiver_allows_exact_stale_candidate_only(monkeypatch):
    waiver = _generic_waiver(linked_issue=1503)
    body = _waiver_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment(sha, 1503)])

    loaded, error, live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert error is None
    assert loaded == waiver
    assert live_body_sha256 == sha

    fresh = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(
                521, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
            _complete_c1_candidate(1470),
        ],
    }
    assert open_pr._is_readback_incomplete_only_blocker(fresh) is True
    assert open_pr._incomplete_candidates_match_generic_waiver(fresh, waiver) is True

    monkeypatch.setattr(
        open_pr,
        "_load_verified_generic_overlap_readback_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    stored = fresh | {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        # P1-binding-gap: current_issue.body_sha256 must equal the live body
        # SHA the (monkeypatched) waiver loader "verified" against.
        "current_issue": {"number": 1503, "body_sha256": sha},
        "source": {
            "complete": True,
            "saturated": False,
            "limit": 100,
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 50,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
        },
        "validation_errors": {},
        "dependency_resolution": {"unresolved_refs": [], "blocking_predecessor": None},
        "decision_inputs_sha256": "sha256:" + "a" * 64,
        "repository": "squne121/loop-protocol",
    }
    canonical = json.dumps(
        {k: v for k, v in stored.items() if k != "evidence_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    stored["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac1_evidence.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    try:
        ok, err_code, detail, effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1503,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is True, (err_code, detail)
        assert effective is not None
        assert effective["route"] == "proceed_with_collision_evidence"
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC2: candidate set / reason / updated_at drift is rejected (fail-closed).
# ---------------------------------------------------------------------------


def test_contract_bound_waiver_rejects_candidate_timestamp_or_reason_drift(monkeypatch):
    waiver = _generic_waiver(linked_issue=1503)
    body = _waiver_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    # updated_at drift: fresh candidate's updated_at no longer matches the
    # waiver-declared value.
    drifted_updated_at = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(
                521, "2026-07-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
        ],
    }
    assert open_pr._incomplete_candidates_match_generic_waiver(drifted_updated_at, waiver) is False

    # reason drift: fresh candidate's reason no longer matches the
    # waiver-declared reason (even though it still starts with the required
    # prefix).
    drifted_reason = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(521, "2026-06-01T00:00:00Z", "readback_incomplete_other"),
        ],
    }
    assert open_pr._incomplete_candidates_match_generic_waiver(drifted_reason, waiver) is False

    # additional unresolved candidate not covered by the waiver.
    extra_candidate = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(
                521, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
            _readback_incomplete_candidate(
                522, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
        ],
    }
    assert open_pr._incomplete_candidates_match_generic_waiver(extra_candidate, waiver) is False

    # a complete candidate outside {C1, C2a} (e.g. C3) is never waived away.
    non_c1_complete = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(
                521, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
            {
                "issue_number": 777,
                "updated_at": "2026-06-01T00:00:00Z",
                "readback_complete": True,
                "policy_class": "C3",
                "reasons": ["structural_or_textual_collision_detected"],
            },
        ],
    }
    assert open_pr._incomplete_candidates_match_generic_waiver(non_c1_complete, waiver) is False

    # end-to-end: run_overlap_preflight_gate must fail-closed with
    # E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE (not silently proceed) when drift is
    # present.
    monkeypatch.setattr(
        open_pr,
        "_load_verified_generic_overlap_readback_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    stored = drifted_updated_at | {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": 1503, "body_sha256": sha},
        "source": {
            "complete": True,
            "saturated": False,
            "limit": 100,
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 50,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
        },
        "validation_errors": {},
        "dependency_resolution": {"unresolved_refs": [], "blocking_predecessor": None},
        "decision_inputs_sha256": "sha256:" + "b" * 64,
        "repository": "squne121/loop-protocol",
    }
    canonical = json.dumps(
        {k: v for k, v in stored.items() if k != "evidence_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    stored["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac2_evidence.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    try:
        ok, err_code, _detail, _effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1503,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert err_code == open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC3: invalid live contract / trusted snapshot combinations are rejected.
# ---------------------------------------------------------------------------


def test_contract_bound_waiver_rejects_invalid_live_contract_or_snapshot(monkeypatch):
    waiver = _generic_waiver(linked_issue=1503)
    body = _waiver_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()

    # repository mismatch (waiver declares a different repository than the
    # canonical PR mutation target passed by the caller).
    mismatched_repo_waiver = _generic_waiver(repository="someone-else/other-repo", linked_issue=1503)
    mismatched_body = _waiver_live_body(mismatched_repo_waiver)
    mismatched_sha = "sha256:" + hashlib.sha256(mismatched_body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1503, mismatched_body, [_snapshot_comment(mismatched_sha, 1503)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # linked_issue mismatch (waiver self-declares a different linked_issue
    # than the caller's actual linked_issue -- prevents cross-issue reuse).
    mismatched_issue_waiver = _generic_waiver(linked_issue=9999)
    mismatched_issue_body = _waiver_live_body(mismatched_issue_waiver)
    mismatched_issue_sha = "sha256:" + hashlib.sha256(mismatched_issue_body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(
        monkeypatch, 1503, mismatched_issue_body, [_snapshot_comment(mismatched_issue_sha, 1503)]
    )
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # expired waiver.
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment(sha, 1503)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2027, 1, 1)
    )
    assert loaded is None
    assert error is not None
    assert "期限" in error

    # live body missing an overlap_readback_waiver block entirely.
    _patch_live_waiver_readback(
        monkeypatch, 1503, "## Outcome\n\nno waiver here\n", [_snapshot_comment("sha256:" + "0" * 64, 1503)]
    )
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # trusted go snapshot body_sha256 does not match the live body (stale /
    # tampered snapshot).
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment("sha256:" + "0" * 64, 1503)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # no trusted go snapshot at all (only a non-go / non-trusted comment).
    _patch_live_waiver_readback(
        monkeypatch,
        1503,
        body,
        [_snapshot_comment(sha, 1503, status="blocked")],
    )
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # comment readback itself incomplete (paginated fetch failure).
    _patch_live_waiver_readback(monkeypatch, 1503, body, [], error="comments_fetch_incomplete")
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert "不完全" in error

    # a valid, matching waiver still succeeds (control case, proves the
    # rejections above are specific to the injected defects, not a broken
    # happy path).
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment(sha, 1503)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert error is None
    assert loaded == waiver


# ---------------------------------------------------------------------------
# AC10: #1675 → #1677 ordered continuation waiver.
# ---------------------------------------------------------------------------


def _ordered_continuation_waiver(**overrides: object) -> dict:
    waiver = {
        "candidate_issue": "#1677",
        "candidate_updated_at": "2026-07-23T11:44:01Z",
        "candidate_reasons": [
            "successor_dependency_ordering",
            "structural_or_textual_collision_detected",
            "independent_strong_signal_detected",
        ],
        "relation": "successor",
        "first": "#1675",
        "next": "#1677",
        "expires_on": "2026-07-25",
        "approved_by": "user_session",
    }
    waiver.update(overrides)
    return waiver


def _ordered_continuation_live_body(waiver: dict) -> str:
    reasons = "\n".join(f'    - "{reason}"' for reason in waiver["candidate_reasons"])
    return f"""## Machine-Readable Contract

```yaml
overlap_ordered_continuation_waiver:
  candidate_issue: "{waiver["candidate_issue"]}"
  candidate_updated_at: "{waiver["candidate_updated_at"]}"
  candidate_reasons:
{reasons}
  relation: "{waiver["relation"]}"
  first: "{waiver["first"]}"
  next: "{waiver["next"]}"
  expires_on: "{waiver["expires_on"]}"
  approved_by: "{waiver["approved_by"]}"
```
"""


def _ordered_continuation_evidence(body_sha256: str, *, native_blocked_by: list[dict] | None = None) -> dict:
    evidence = {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "repository": "squne121/loop-protocol",
        "current_issue": {"number": 1675, "body_sha256": body_sha256},
        "source": {
            "complete": True,
            "saturated": False,
            "limit": 100,
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 50,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
        },
        "route": "human_review_required",
        "candidates": [
            {
                "issue_number": 1677,
                "updated_at": "2026-07-23T11:44:01Z",
                "readback_complete": True,
                "policy_class": "C3",
                "reasons": [
                    "successor_dependency_ordering",
                    "structural_or_textual_collision_detected",
                    "independent_strong_signal_detected",
                ],
            }
        ],
        "dependency_resolution": {
            "unresolved_refs": [],
            "blocking_predecessor": None,
            "native_blocked_by": native_blocked_by if native_blocked_by is not None else [],
            # current #1675 が後続 #1677/#1674 を止める向きは、#1675 の
            # predecessor blocker ではない。ここで非空でも happy path を通す。
            "native_blocking": [{"number": 1677}, {"number": 1674}],
        },
        "validation_errors": {},
        "decision_inputs_sha256": "sha256:" + "c" * 64,
    }
    canonical = json.dumps(evidence, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    evidence["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return evidence


def test_ordered_continuation_waiver_allows_only_1675_then_1677(monkeypatch):
    waiver = _ordered_continuation_waiver()
    body = _ordered_continuation_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, body, [_snapshot_comment(sha, 1675)])

    loaded, error, live_body_sha256 = open_pr._load_verified_ordered_continuation_waiver(
        "squne121/loop-protocol", 1675, today=open_pr.date(2026, 7, 24)
    )
    assert error is None
    assert loaded == waiver
    assert live_body_sha256 == sha

    stored = _ordered_continuation_evidence(sha)
    assert open_pr._ordered_continuation_candidate_matches_waiver(stored, waiver) is True
    timestamp_drift = json.loads(json.dumps(stored))
    timestamp_drift["candidates"][0]["updated_at"] = "2026-07-23T11:44:02Z"
    assert open_pr._ordered_continuation_candidate_matches_waiver(timestamp_drift, waiver) is False
    extra_candidate = json.loads(json.dumps(stored))
    extra_candidate["candidates"].append(json.loads(json.dumps(stored["candidates"][0])))
    assert open_pr._ordered_continuation_candidate_matches_waiver(extra_candidate, waiver) is False
    reason_drift = json.loads(json.dumps(stored))
    reason_drift["candidates"][0]["reasons"].append("unapproved_reason")
    assert open_pr._ordered_continuation_candidate_matches_waiver(reason_drift, waiver) is False
    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac10_ordered.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_ordered_continuation_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    try:
        ok, error_code, detail, effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1675,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is True, (error_code, detail)
        assert effective is not None
        assert effective["route"] == "proceed_with_collision_evidence"
    finally:
        evidence_path.unlink(missing_ok=True)


def test_ordered_continuation_waiver_rejects_reverse_direction_and_native_predecessor(monkeypatch):
    reverse = _ordered_continuation_waiver(candidate_issue="#1675", relation="predecessor", first="#1677", next="#1675")
    reverse_body = _ordered_continuation_live_body(reverse)
    reverse_sha = "sha256:" + hashlib.sha256(reverse_body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, reverse_body, [_snapshot_comment(reverse_sha, 1675)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_ordered_continuation_waiver(
        "squne121/loop-protocol", 1675, today=open_pr.date(2026, 7, 24)
    )
    assert loaded is None
    assert error is not None

    expired = _ordered_continuation_waiver(expires_on="2026-07-23")
    expired_body = _ordered_continuation_live_body(expired)
    expired_sha = "sha256:" + hashlib.sha256(expired_body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, expired_body, [_snapshot_comment(expired_sha, 1675)])
    loaded, error, _live_body_sha256 = open_pr._load_verified_ordered_continuation_waiver(
        "squne121/loop-protocol", 1675, today=open_pr.date(2026, 7, 24)
    )
    assert loaded is None
    assert error is not None
    assert "期限" in error

    waiver = _ordered_continuation_waiver()
    body = _ordered_continuation_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    stored = _ordered_continuation_evidence(sha, native_blocked_by=[{"number": 1669}])
    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac10_native_predecessor.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_ordered_continuation_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    try:
        ok, error_code, _detail, _effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1675,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert error_code == open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC10: #1675 outbound-only waiver. native_blocking は #1675 が後続を止める
# 向きであり、candidate / actual predecessor がない場合にだけ許容する。
# ---------------------------------------------------------------------------


def _outbound_only_waiver(**overrides: object) -> dict:
    waiver = {
        "route": "human_review_required",
        "repo": "squne121/loop-protocol",
        "issue": 1675,
        "expires_on": "2026-07-25",
        "approved_by": "user_session",
        "source": {"complete": True, "saturated": False},
        "candidates": [],
        "dependency_resolution": {"blocking_predecessor": None},
        "native_blocked_by": [],
        "native_blocking": [1677, 1674],
    }
    waiver.update(overrides)
    return waiver


def _outbound_only_live_body(waiver: dict) -> str:
    source = waiver["source"]
    source_complete = str(source["complete"]).lower()
    source_saturated = str(source["saturated"]).lower()
    return f'''## Machine-Readable Contract

```yaml
outbound_only_waiver:
  route: "{waiver["route"]}"
  repo: "{waiver["repo"]}"
  issue: {waiver["issue"]}
  expires_on: "{waiver["expires_on"]}"
  approved_by: "{waiver["approved_by"]}"
  source: {{complete: {source_complete}, saturated: {source_saturated}}}
  candidates: {waiver["candidates"]}
  dependency_resolution: {{blocking_predecessor: null}}
  native_blocked_by: {waiver["native_blocked_by"]}
  native_blocking: {waiver["native_blocking"]}
```
'''


def _refresh_evidence_sha256(evidence: dict) -> None:
    canonical = json.dumps(
        {key: value for key, value in evidence.items() if key != "evidence_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    evidence["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _outbound_only_evidence(body_sha256: str) -> dict:
    evidence = _ordered_continuation_evidence(body_sha256)
    evidence["candidates"] = []
    evidence["dependency_resolution"]["native_blocking"] = [
        {"repository": "squne121/loop-protocol", "number": 1677, "state": "OPEN"},
        {"repository": "squne121/loop-protocol", "number": 1674, "state": "OPEN"},
    ]
    _refresh_evidence_sha256(evidence)
    return evidence


def test_outbound_only_waiver_allows_exact_native_blocking_only(monkeypatch):
    waiver = _outbound_only_waiver()
    body = _outbound_only_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, body, [_snapshot_comment(sha, 1675)])

    loaded, error, live_body_sha256 = open_pr._load_verified_outbound_only_waiver(
        "squne121/loop-protocol", 1675, today=open_pr.date(2026, 7, 24)
    )
    assert error is None
    assert loaded == waiver
    assert live_body_sha256 == sha

    stored = _outbound_only_evidence(sha)
    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac10_outbound_only.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_outbound_only_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    try:
        ok, error_code, detail, effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1675,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is True, (error_code, detail)
        assert effective is not None
        assert effective["route"] == "proceed_with_collision_evidence"
    finally:
        evidence_path.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda waiver: waiver.__setitem__("candidates", [1677]),
        lambda waiver: waiver.__setitem__("native_blocking", [1674, 1677]),
        lambda waiver: waiver.__setitem__("native_blocked_by", [1677]),
        lambda waiver: waiver["dependency_resolution"].__setitem__("blocking_predecessor", 1674),
        lambda waiver: waiver.__setitem__("approved_by", "other"),
        lambda waiver: waiver.__setitem__("unexpected", True),
    ],
)
def test_outbound_only_waiver_schema_rejects_unsafe_or_unknown_fields(monkeypatch, mutate):
    waiver = _outbound_only_waiver()
    mutate(waiver)
    # The fixture renderer is intentionally only used for loader coverage; a
    # malformed object is checked by the closed-schema predicate directly.
    assert open_pr._validate_outbound_only_waiver_schema(waiver) is not None


@pytest.mark.parametrize(
    "mutate_evidence",
    [
        lambda evidence: evidence.__setitem__("candidates", [{"issue_number": 1677}]),
        lambda evidence: evidence["dependency_resolution"].__setitem__("blocking_predecessor", 1674),
        lambda evidence: evidence["dependency_resolution"].__setitem__("native_blocked_by", [1677]),
        lambda evidence: evidence["dependency_resolution"].__setitem__(
            "native_blocking",
            [
                {"repository": "squne121/loop-protocol", "number": 1674, "state": "OPEN"},
                {"repository": "squne121/loop-protocol", "number": 1677, "state": "OPEN"},
            ],
        ),
        lambda evidence: evidence["dependency_resolution"].__setitem__("native_blocking", [1677, 1674]),
        lambda evidence: evidence["dependency_resolution"]["native_blocking"][0].__setitem__(
            "repository", "other/repository"
        ),
    ],
)
def test_outbound_only_waiver_rejects_candidate_or_actual_predecessor(monkeypatch, mutate_evidence):
    waiver = _outbound_only_waiver()
    body = _outbound_only_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, body, [_snapshot_comment(sha, 1675)])
    stored = _outbound_only_evidence(sha)
    mutate_evidence(stored)
    _refresh_evidence_sha256(stored)
    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac10_outbound_unsafe.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_outbound_only_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    try:
        ok, error_code, _detail, _effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1675,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert error_code in {
            open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE,
            open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID,
        }
    finally:
        evidence_path.unlink(missing_ok=True)


def test_outbound_only_waiver_rejects_snapshot_or_fresh_body_sha_evidence_gap(monkeypatch):
    waiver = _outbound_only_waiver()
    body = _outbound_only_live_body(waiver)
    sha = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1675, body, [_snapshot_comment(sha, 1675)])
    stored = _outbound_only_evidence("sha256:" + "0" * 64)
    evidence_path = Path(str(Path(__file__).parent / "_tmp_ac10_outbound_sha_gap.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    monkeypatch.setattr(
        open_pr,
        "_load_verified_outbound_only_waiver",
        lambda repo, linked_issue: (waiver, None, sha),
    )
    try:
        ok, error_code, detail, _effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1675,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        assert ok is False
        assert error_code == open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
        assert "body_sha256" in detail
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Schema validation (supporting coverage for AC2/AC3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda w: w.pop("repository"),
        lambda w: w.__setitem__("repository", ""),
        lambda w: w.pop("linked_issue"),
        lambda w: w.__setitem__("linked_issue", "1503"),
        lambda w: w.__setitem__("linked_issue", True),
        lambda w: w.__setitem__("candidates", []),
        lambda w: w.__setitem__("candidates", "not-a-list"),
        lambda w: w["candidates"][0].__setitem__("issue_number", "521"),
        lambda w: w["candidates"][0].__setitem__("issue_number", True),
        lambda w: w["candidates"][0].pop("updated_at"),
        lambda w: w["candidates"][0].__setitem__("reason", "unrelated_reason"),
        lambda w: w.pop("expires_on"),
        lambda w: w.__setitem__("expires_on", "not-a-date"),
        lambda w: w.pop("approved_by"),
        lambda w: w.__setitem__("approved_by", ""),
    ],
)
def test_generic_waiver_schema_validation_rejects_malformed_fields(mutate):
    waiver = _generic_waiver()
    mutate(waiver)
    assert open_pr._validate_generic_overlap_readback_waiver_schema(waiver) is not None


def test_generic_waiver_schema_validation_rejects_duplicate_candidate_numbers():
    waiver = _generic_waiver(
        candidates=[
            {
                "issue_number": 521,
                "updated_at": "2026-06-01T00:00:00Z",
                "reason": "readback_incomplete_missing_outcome_or_in_scope",
            },
            {
                "issue_number": 521,
                "updated_at": "2026-06-02T00:00:00Z",
                "reason": "readback_incomplete_missing_outcome_or_in_scope",
            },
        ]
    )
    assert open_pr._validate_generic_overlap_readback_waiver_schema(waiver) is not None


def test_generic_waiver_schema_validation_accepts_well_formed_waiver():
    waiver = _generic_waiver()
    assert open_pr._validate_generic_overlap_readback_waiver_schema(waiver) is None


# ---------------------------------------------------------------------------
# The generic path never activates for the #1477 fixed binding's own
# (repository, linked_issue) pair -- that pair must always route through the
# unmodified, existing `_has_only_fixed_readback_incomplete_blockers` /
# `_load_verified_overlap_readback_waiver` path (regression-protected by
# test_open_pr_overlap_gate.py, outside this Issue's Allowed Paths).
# ---------------------------------------------------------------------------


def test_generic_waiver_path_does_not_activate_for_the_fixed_1477_binding(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("generic waiver loader must not run for the fixed #1477 binding")

    monkeypatch.setattr(open_pr, "_load_verified_generic_overlap_readback_waiver", fail_if_called)

    fresh = {
        "route": "human_review_required",
        "candidates": [
            _readback_incomplete_candidate(
                521, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"
            ),
        ],
    }
    stored = fresh | {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": 1477},
        "source": {
            "complete": True,
            "saturated": False,
            "limit": 100,
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 50,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
        },
        "validation_errors": {},
        "dependency_resolution": {"unresolved_refs": [], "blocking_predecessor": None},
        "decision_inputs_sha256": "sha256:" + "c" * 64,
        "repository": "squne121/loop-protocol",
    }
    canonical = json.dumps(
        {k: v for k, v in stored.items() if k != "evidence_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    stored["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    evidence_path = Path(str(Path(__file__).parent / "_tmp_fixed1477_evidence.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    try:
        ok, err_code, _detail, _effective = open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1477,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        )
        # The #521-shaped candidate is not one of the fixed {519, 520, 1429}
        # candidates, so the fixed path rejects it -- and, per the isolation
        # guarantee above, the generic path is never even attempted for
        # linked_issue=1477.
        assert ok is False
        assert err_code == open_pr.E_OVERLAP_PREFLIGHT_UNSAFE_ROUTE
    finally:
        evidence_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PR #1627 review fix_delta (P1-binding-gap): the generic waiver loader's
# live_body_sha256 must be connected to the fresh evidence's own
# current_issue.body_sha256 -- otherwise a waiver verified against a live
# body read *after* the fresh (route-producing) readback could be applied
# even though the fresh readback actually saw a different body.
# ---------------------------------------------------------------------------


def _build_stored_evidence(
    *,
    linked_issue: int,
    candidates: list[dict],
    current_issue: dict,
    decision_inputs_byte: bytes,
) -> dict:
    stored = {
        "route": "human_review_required",
        "candidates": candidates,
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": current_issue,
        "source": {
            "complete": True,
            "saturated": False,
            "limit": 100,
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 50,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
        },
        "validation_errors": {},
        "dependency_resolution": {"unresolved_refs": [], "blocking_predecessor": None},
        "decision_inputs_sha256": "sha256:" + decision_inputs_byte.hex()[:64].ljust(64, "0"),
        "repository": "squne121/loop-protocol",
    }
    canonical = json.dumps(
        {k: v for k, v in stored.items() if k != "evidence_sha256"},
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    stored["evidence_sha256"] = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    return stored


def _run_binding_gap_case(monkeypatch, tmp_name: str, *, current_issue_body_sha256):
    """Shared harness for the P1-binding-gap cases: live body/snapshot always
    describe waiver-body "A" (matching candidates), but the fresh evidence's
    ``current_issue.body_sha256`` is injected by the caller so each case can
    independently control whether it matches the live body actually read
    back by the (real, non-monkeypatched) generic waiver loader."""
    waiver = _generic_waiver(linked_issue=1503)
    body_a = _waiver_live_body(waiver)
    sha_a = "sha256:" + hashlib.sha256(body_a.encode("utf-8")).hexdigest()
    _patch_live_waiver_readback(monkeypatch, 1503, body_a, [_snapshot_comment(sha_a, 1503)])

    candidates = [
        _readback_incomplete_candidate(521, "2026-06-01T00:00:00Z", "readback_incomplete_missing_outcome_or_in_scope"),
    ]
    current_issue = {"number": 1503}
    if current_issue_body_sha256 is not _UNSET:
        current_issue["body_sha256"] = current_issue_body_sha256
    stored = _build_stored_evidence(
        linked_issue=1503,
        candidates=candidates,
        current_issue=current_issue,
        decision_inputs_byte=tmp_name.encode("utf-8"),
    )
    evidence_path = Path(str(Path(__file__).parent / f"_tmp_{tmp_name}_evidence.json"))
    evidence_path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(
        open_pr.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompletedProcess(0, json.dumps(stored), ""),
    )
    try:
        return open_pr.run_overlap_preflight_gate(
            repo="squne121/loop-protocol",
            linked_issue=1503,
            evidence_file=evidence_path,
            expected_evidence_sha256=stored["evidence_sha256"],
            expected_decision_inputs_sha256=stored["decision_inputs_sha256"],
        ), sha_a
    finally:
        evidence_path.unlink(missing_ok=True)


_UNSET = object()


def test_binding_gap_rejects_when_fresh_body_sha_does_not_match_live_body_read_by_waiver_loader(
    monkeypatch,
):
    """fresh 本文A（producer が readback した時点の body_sha256）が、waiver
    検証時に読み戻した live 本文（この harness では実際には本文Aだが、fresh
    側は本文Bを読んだと主張する）と一致しない場合、waiver は適用されず
    fail-closed になる。"""
    other_body_sha256 = "sha256:" + hashlib.sha256(b"body-B-unrelated-content").hexdigest()
    (ok, error, detail, _effective), _sha_a = _run_binding_gap_case(
        monkeypatch, "gap_mismatch", current_issue_body_sha256=other_body_sha256
    )
    assert ok is False
    assert error == open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    assert "body_sha256" in detail


def test_binding_gap_allows_when_fresh_body_sha_matches_live_body_read_by_waiver_loader(
    monkeypatch,
):
    """fresh 本文A / live 本文A+snapshot A が一致する（binding gap がない）
    control case: waiver は正しく適用され proceed する。"""
    # `current_issue_body_sha256` は、live 本文を読み戻す前に決まる値が要る
    # ため、ここでは harness を2段で呼ぶ代わりに waiver-body の SHA を先に
    # 計算してから harness へ渡す。
    waiver = _generic_waiver(linked_issue=1503)
    expected_sha = "sha256:" + hashlib.sha256(_waiver_live_body(waiver).encode("utf-8")).hexdigest()
    (ok, error, detail, effective), sha_a = _run_binding_gap_case(
        monkeypatch, "gap_match", current_issue_body_sha256=expected_sha
    )
    assert sha_a == expected_sha
    assert ok is True, (error, detail)
    assert effective is not None
    assert effective["route"] == "proceed_with_collision_evidence"


def test_binding_gap_rejects_missing_current_issue_body_sha256(monkeypatch):
    """current_issue.body_sha256 が欠落している fresh evidence は、waiver 検証
    済みの live body SHA と接続できないため fail-closed で拒否する。"""
    (ok, error, detail, _effective), _sha_a = _run_binding_gap_case(
        monkeypatch, "gap_missing", current_issue_body_sha256=_UNSET
    )
    assert ok is False
    assert error == open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    assert "body_sha256" in detail


def test_binding_gap_rejects_malformed_current_issue_body_sha256(monkeypatch):
    """current_issue.body_sha256 が sha256:<64 hex> 形式でない場合も
    fail-closed で拒否する（型は正しいが形式不正）。"""
    (ok, error, detail, _effective), _sha_a = _run_binding_gap_case(
        monkeypatch, "gap_malformed", current_issue_body_sha256="not-a-sha256-value"
    )
    assert ok is False
    assert error == open_pr.E_OVERLAP_PREFLIGHT_EVIDENCE_INVALID
    assert "body_sha256" in detail


# ---------------------------------------------------------------------------
# PR #1627 review fix_delta (P2-reason-partial-match): the waiver only ever
# permits the single ``reason`` it declares per candidate. Fresh candidates
# whose ``reasons`` list contains the permitted reason *plus* an additional,
# unwaived reason must still be rejected (subset/superset is not equality).
# ---------------------------------------------------------------------------


def test_incomplete_candidates_match_generic_waiver_rejects_extra_reason_added_to_permitted_candidate():
    waiver = _generic_waiver(linked_issue=1503)
    fresh_with_extra_reason = {
        "route": "human_review_required",
        "candidates": [
            {
                "issue_number": 521,
                "updated_at": "2026-06-01T00:00:00Z",
                "readback_complete": False,
                "reasons": [
                    "readback_incomplete_missing_outcome_or_in_scope",
                    "readback_incomplete_another_unwaived_reason",
                ],
            },
        ],
    }
    assert open_pr._incomplete_candidates_match_generic_waiver(fresh_with_extra_reason, waiver) is False
