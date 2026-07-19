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

    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert error is None
    assert loaded == waiver

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
        lambda repo, linked_issue: (waiver, None),
    )
    stored = fresh | {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": 1503},
        "source": {"complete": True, "saturated": False, "limit": 100},
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
        lambda repo, linked_issue: (waiver, None),
    )
    stored = drifted_updated_at | {
        "schema": open_pr.OVERLAP_PREFLIGHT_SCHEMA,
        "current_issue": {"number": 1503},
        "source": {"complete": True, "saturated": False, "limit": 100},
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
    _patch_live_waiver_readback(
        monkeypatch, 1503, mismatched_body, [_snapshot_comment(mismatched_sha, 1503)]
    )
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
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
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # expired waiver.
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment(sha, 1503)])
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2027, 1, 1)
    )
    assert loaded is None
    assert error is not None
    assert "期限" in error

    # live body missing an overlap_readback_waiver block entirely.
    _patch_live_waiver_readback(
        monkeypatch, 1503, "## Outcome\n\nno waiver here\n", [_snapshot_comment("sha256:" + "0" * 64, 1503)]
    )
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # trusted go snapshot body_sha256 does not match the live body (stale /
    # tampered snapshot).
    _patch_live_waiver_readback(
        monkeypatch, 1503, body, [_snapshot_comment("sha256:" + "0" * 64, 1503)]
    )
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
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
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert error is not None

    # comment readback itself incomplete (paginated fetch failure).
    _patch_live_waiver_readback(monkeypatch, 1503, body, [], error="comments_fetch_incomplete")
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert loaded is None
    assert "不完全" in error

    # a valid, matching waiver still succeeds (control case, proves the
    # rejections above are specific to the injected defects, not a broken
    # happy path).
    _patch_live_waiver_readback(monkeypatch, 1503, body, [_snapshot_comment(sha, 1503)])
    loaded, error = open_pr._load_verified_generic_overlap_readback_waiver(
        "squne121/loop-protocol", 1503, today=open_pr.date(2026, 6, 15)
    )
    assert error is None
    assert loaded == waiver


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
        "source": {"complete": True, "saturated": False, "limit": 100},
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
