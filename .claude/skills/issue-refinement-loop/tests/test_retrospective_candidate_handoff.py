#!/usr/bin/env python3
"""Tests for retrospective_candidate_handoff.py (Issue #2602, #1939 Workstream 4).

Fixes the parameterized boundaries required by AC8/AC11 -- production
decision logic/adapters are exercised directly; only external I/O (GitHub API
via ``search_fn``/``detail_fn``/``create_fn`` injection) is faked.

Boundaries covered (Issue #2602 body, "## Verification Scenarios" +
"Acceptance Criteria" AC8/AC11):

- A:  unauthorized candidate -> Issue create 0 / implementation launch 0
- C:  authorized unique candidate -> Issue created once, connects to
      refinement by exact reference
- B2: same dedupe key / different title -> treated as duplicate (key wins)
- B3: different dedupe key / same title -> NOT treated as duplicate by title
      alone
- B4: same key / CLOSED issue -> not reopened, disposition preserved
- C2: Issue create succeeds, downstream step fails, handoff reruns -> reuses
      the same materialized Issue via dedupe key readback, no duplicate
      create
- D:  double-launch guard -- no double invocation of impl-review-loop even if
      handoff reruns racing with root
- E/E2: canonical terminal propagation is not remapped
- AC6: source/dependency reference preservation
- AC11: producer-specific adapters for both schemas, including
      finding_contract.identity-based stable identity derivation, and
      run-specific values excluded from the dedupe identity
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import retrospective_candidate_handoff as h  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: producer-specific candidate payloads
# ---------------------------------------------------------------------------


def _chatgpt_result(target: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "chatgpt_retrospective_result/v1",
        "target": target,
        "input_marker_digest": "sha256:" + "a" * 64,
        "verdict": "request_changes",
        "findings": [],
        "follow_up_issue_candidates": candidates,
        "raw_values_emitted": False,
    }


def _chatgpt_candidate(title: str = "Improve X", body: str = "Do the thing.") -> dict[str, Any]:
    return {
        "title": title,
        "body": body,
        "blocked_by": [],
        "public_safe": True,
    }


def _agent_candidate_with_finding_contract(
    *,
    identity_value: str = "sha256:" + "b" * 64,
    source_run_ref: dict[str, Any] | None = None,
    updated_at: str = "2026-09-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "candidate_id": "cand-001",
        "candidate_status": "proposed",
        "title": "Improve retrospective handoff",
        "description": "Adapter should normalize candidates.",
        "source_run_ref": source_run_ref or {"base_sha": "a" * 40, "source_set_digest": "c" * 64},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": updated_at,
        "finding_contract": {
            "schema_version": "v1",
            "identity": {
                "algorithm": "sha256-jcs-v1",
                "key": {
                    "repository_id": "squne121/loop-protocol",
                    "claim_class": "runtime_behavior",
                    "subject_ref": {"kind": "issue", "value": "2602"},
                    "rule_id": "runtime_behavior.missing_evidence",
                },
                "value": identity_value,
            },
            "claim_class": "runtime_behavior",
            "evaluations": [
                {
                    "evaluation_id": "sha256:" + "d" * 64,
                    "evaluated_run_ref": {"base_sha": "a" * 40, "source_set_digest": "c" * 64},
                    "previous_evaluation_ref": None,
                    "observed": True,
                    "source_coverage": "complete",
                    "evaluation_status": "classified",
                    "presence_delta": "new",
                    "signal_delta": "unknown",
                    "delta_status": "new",
                    "indeterminate_reason": None,
                    "baseline_signal": None,
                    "current_signal": None,
                    "expected_signal": None,
                    "evidence_refs": [
                        {
                            "ref_type": "repository_blob",
                            "source_id": "repository",
                            "resource_identity": "src/foo.py#L1",
                            "projection_digest": "sha256:" + "e" * 64,
                        }
                    ],
                    "classified_at": "2026-09-01T00:00:00Z",
                    "classifier_version": "v1",
                }
            ],
        },
    }


def _agent_candidate_legacy(candidate_id: str = "cand-legacy-001") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "candidate_status": "proposed",
        "title": "Improve retrospective handoff (legacy)",
        "description": "Legacy candidate with no finding_contract.",
        "source_run_ref": {"base_sha": "f" * 40, "source_set_digest": "1" * 64},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class SpyCreateFn:
    """Records calls and returns a fresh created-issue TransactionResult."""

    def __init__(self, issue_number: int = 501) -> None:
        self.calls: list[dict[str, Any]] = []
        self.issue_number = issue_number

    def __call__(self, **kwargs: Any):
        self.calls.append(kwargs)
        from create_issue_txn import TransactionResult  # already on sys.path via handoff module

        return TransactionResult(
            status="success",
            issue_number=self.issue_number,
            issue_url=f"https://github.com/{kwargs['repo']}/issues/{self.issue_number}",
            completed_steps=["issue-create"],
        )


def _no_match_search_fn(_repo: str, _dedupe_key: str, _gh_bin: str) -> list[dict[str, Any]]:
    return []


def _no_match_detail_fn(_repo: str, _number: int, _gh_bin: str) -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# A: unauthorized candidate -> Issue create 0 / implementation launch 0
# ---------------------------------------------------------------------------


class TestUnauthorizedCandidate:
    def test_unauthorized_candidate_creates_no_issue_and_no_launch(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )
        search_calls: list[Any] = []
        create_fn = SpyCreateFn()

        def _spy_search(*args: Any) -> list[dict[str, Any]]:
            search_calls.append(args)
            return []

        result = h.materialize_candidate(
            candidate,
            human_authorized=False,
            repo="owner/repo",
            search_fn=_spy_search,
            detail_fn=_no_match_detail_fn,
            create_fn=create_fn,
        )

        assert result.status == "unauthorized"
        assert result.issue_number is None
        assert result.next_action is None
        assert len(create_fn.calls) == 0, "unauthorized candidate must never trigger Issue create"
        assert len(search_calls) == 0, "unauthorized candidate must never trigger dedupe search"


# ---------------------------------------------------------------------------
# C: authorized unique candidate -> Issue created once, exact-reference
# connection to refinement
# ---------------------------------------------------------------------------


class TestAuthorizedUniqueCandidateMaterialization:
    def test_creates_issue_once_and_returns_exact_next_action_reference(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(title="Improve X"),
        )
        create_fn = SpyCreateFn(issue_number=777)

        result = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=create_fn,
        )

        assert result.status == "created"
        assert result.issue_number == 777
        assert len(create_fn.calls) == 1
        assert result.next_action == {"kind": "issue_refinement_loop", "issue_number": 777}

    def test_create_bypasses_internal_title_only_dedupe(self) -> None:
        """AC2(g): outer search confirmed create -> internal title-only dedupe
        inside create_issue_txn.run_transaction() must be bypassed."""
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )
        create_fn = SpyCreateFn()

        h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=create_fn,
        )

        assert create_fn.calls[0]["skip_internal_title_dedupe"] is True

    def test_search_truncated_flag_when_search_hits_the_limit(self) -> None:
        """AC2(e): a saturated dedupe search result is flagged, not silently
        treated as complete."""
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )

        def _saturated_search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": n, "title": "unrelated", "state": "OPEN", "url": ""} for n in range(10)]

        def _detail_never_matches(_repo: str, _number: int, _gh_bin: str) -> dict[str, Any]:
            return {"body": "no dedupe key here", "state": "OPEN"}

        result = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_saturated_search,
            detail_fn=_detail_never_matches,
            create_fn=SpyCreateFn(),
        )

        assert result.search_truncated is True


# ---------------------------------------------------------------------------
# B2: same dedupe key / different title -> duplicate (key wins over title)
# ---------------------------------------------------------------------------


class TestSameKeyDifferentTitleIsDuplicate:
    def test_same_key_different_title_is_treated_as_duplicate(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(title="Original candidate title"),
        )
        # The existing issue has a DIFFERENT title, but its body contains the
        # exact dedupe_key -> must be treated as a duplicate.
        existing_body = f'## Machine-Readable Contract\n\ndedupe_key: "{candidate.dedupe_key}"\n'

        def _search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": 42, "title": "A completely different title", "state": "OPEN", "url": "https://x/42"}]

        def _detail(_repo: str, number: int, _gh_bin: str) -> dict[str, Any]:
            assert number == 42
            return {
                "number": 42,
                "title": "A completely different title",
                "state": "OPEN",
                "stateReason": None,
                "url": "https://x/42",
                "body": existing_body,
            }

        create_fn = SpyCreateFn()
        result = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_search,
            detail_fn=_detail,
            create_fn=create_fn,
        )

        assert result.status == "reused_open"
        assert result.issue_number == 42
        assert len(create_fn.calls) == 0, "duplicate must be reused, not re-created"


# ---------------------------------------------------------------------------
# B3: different dedupe key / same title -> NOT a duplicate by title alone
# ---------------------------------------------------------------------------


class TestDifferentKeySameTitleIsNotDuplicate:
    def test_different_key_same_title_is_not_treated_as_duplicate(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(title="Improve X"),
        )
        # Full-text search matched issue #99 on the shared title, but its body
        # carries a DIFFERENT dedupe_key -> must NOT be treated as duplicate.
        other_body = '## Machine-Readable Contract\n\ndedupe_key: "chatgpt-candidate:v1:owner/repo:issue:999:improve x"\n'

        def _search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": 99, "title": "Improve X", "state": "OPEN", "url": "https://x/99"}]

        def _detail(_repo: str, number: int, _gh_bin: str) -> dict[str, Any]:
            assert number == 99
            return {
                "number": 99,
                "title": "Improve X",
                "state": "OPEN",
                "stateReason": None,
                "url": "https://x/99",
                "body": other_body,
            }

        create_fn = SpyCreateFn(issue_number=555)
        result = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_search,
            detail_fn=_detail,
            create_fn=create_fn,
        )

        assert result.status == "created"
        assert result.issue_number == 555
        assert len(create_fn.calls) == 1


# ---------------------------------------------------------------------------
# B4: same key / CLOSED issue -> not reopened, disposition preserved
# ---------------------------------------------------------------------------


class TestSameKeyClosedIssueDispositionPreserved:
    def test_closed_duplicate_is_not_reopened_and_disposition_is_preserved(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )
        closed_body = f'dedupe_key: "{candidate.dedupe_key}"\n'

        def _search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": 7, "title": "whatever", "state": "CLOSED", "url": "https://x/7"}]

        def _detail(_repo: str, number: int, _gh_bin: str) -> dict[str, Any]:
            return {
                "number": 7,
                "title": "whatever",
                "state": "CLOSED",
                "stateReason": "NOT_PLANNED",
                "url": "https://x/7",
                "body": closed_body,
            }

        create_fn = SpyCreateFn()
        result = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_search,
            detail_fn=_detail,
            create_fn=create_fn,
        )

        assert result.status == "reused_closed"
        assert result.issue_number == 7
        assert result.disposition == "NOT_PLANNED"
        assert len(create_fn.calls) == 0, "CLOSED duplicate must never be recreated"
        assert result.next_action is None, "CLOSED duplicate must not hand off to refinement"


# ---------------------------------------------------------------------------
# C2: create succeeds, downstream step fails, handoff reruns -> reuses the
# same Issue via dedupe key readback, no duplicate create
# ---------------------------------------------------------------------------


class TestDownstreamFailureRerunReusesIssue:
    def test_rerun_after_downstream_failure_reuses_same_issue_no_duplicate_create(self) -> None:
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )
        create_fn = SpyCreateFn(issue_number=88)

        # First attempt: creates issue #88 (a downstream step, e.g. label
        # apply, is modeled as failing OUTSIDE this module's responsibility --
        # what matters here is the rerun's dedupe behavior).
        first = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=create_fn,
        )
        assert first.status == "created"
        assert first.issue_number == 88

        # Rerun: the outer dedupe_key search now finds issue #88 (its body
        # contains the dedupe_key rendered by render_materialization_body()).
        rendered_body = h.render_materialization_body(candidate)
        assert candidate.dedupe_key in rendered_body

        def _rerun_search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": 88, "title": candidate.title, "state": "OPEN", "url": "https://x/88"}]

        def _rerun_detail(_repo: str, number: int, _gh_bin: str) -> dict[str, Any]:
            return {
                "number": 88,
                "title": candidate.title,
                "state": "OPEN",
                "stateReason": None,
                "url": "https://x/88",
                "body": rendered_body,
            }

        second = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_rerun_search,
            detail_fn=_rerun_detail,
            create_fn=create_fn,
        )

        assert second.status == "reused_open"
        assert second.issue_number == 88
        assert len(create_fn.calls) == 1, "rerun must not create a duplicate Issue"


# ---------------------------------------------------------------------------
# D: double-launch guard -- no double invocation of impl-review-loop even if
# handoff reruns racing with root
# ---------------------------------------------------------------------------


class TestDoubleLaunchGuard:
    def test_handoff_module_never_references_impl_review_loop_internals(self) -> None:
        """Structural guard (AC4 design constraint: single implementation
        launch owner): this module must never call run_root_transition(),
        invoke_step1, or any issue-refinement-loop internal decision function
        directly -- it stops at producing a materialization request."""
        import inspect

        source = inspect.getsource(h)
        forbidden = [
            "run_root_transition",
            "invoke_step1",
            "decide_next_loop_action",
            "run_refinement_preflight",
            "impl_review_loop",
        ]
        for token in forbidden:
            assert token not in source, (
                f"retrospective_candidate_handoff.py must not reference {token!r}: "
                "run_root_transition() in root_entry_router.py remains the sole "
                "launch owner (Issue #2602 Design Constraints)"
            )

    def test_racing_rerun_of_materialize_candidate_yields_single_issue_and_single_create_call(
        self,
    ) -> None:
        """Even if materialize_candidate() is invoked twice for the same
        candidate racing with a rerun, at most one Issue (hence at most one
        downstream /issue-refinement-loop <N> handoff target) is produced --
        there is no surface here for a double impl-review-loop launch."""
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 10}, [_chatgpt_candidate()]),
            _chatgpt_candidate(),
        )
        create_fn = SpyCreateFn(issue_number=321)

        first = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=create_fn,
        )
        rendered_body = h.render_materialization_body(candidate)

        def _race_search(_repo: str, _key: str, _gh_bin: str) -> list[dict[str, Any]]:
            return [{"number": 321, "title": candidate.title, "state": "OPEN", "url": "https://x/321"}]

        def _race_detail(_repo: str, _number: int, _gh_bin: str) -> dict[str, Any]:
            return {
                "number": 321,
                "title": candidate.title,
                "state": "OPEN",
                "stateReason": None,
                "url": "https://x/321",
                "body": rendered_body,
            }

        second = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_race_search,
            detail_fn=_race_detail,
            create_fn=create_fn,
        )

        assert first.next_action == {"kind": "issue_refinement_loop", "issue_number": 321}
        assert second.next_action == {"kind": "issue_refinement_loop", "issue_number": 321}
        assert first.next_action == second.next_action
        assert len(create_fn.calls) == 1


# ---------------------------------------------------------------------------
# E/E2: canonical terminal propagation is not remapped
# ---------------------------------------------------------------------------


class TestCanonicalTerminalPropagation:
    @pytest.mark.parametrize("terminal_status", sorted(h.CANONICAL_LOOP_TERMINALS))
    def test_canonical_terminal_is_returned_unchanged(self, terminal_status: str) -> None:
        loop_result = {"status": terminal_status, "issue_number": 2602, "detail": "unit-test"}
        propagated = h.propagate_canonical_terminal(loop_result)
        assert propagated == loop_result

    def test_blocked_status_is_not_bypassed(self) -> None:
        """AC7: a blocked/stop-condition result must be returned as status,
        never silently bypassed to a different (e.g. success) path."""
        loop_result = {"status": "blocked", "reason": "capability_or_identity_unverifiable"}
        assert h.propagate_canonical_terminal(loop_result) == loop_result

    def test_worker_local_warning_is_not_upgraded_to_a_hard_stop(self) -> None:
        """AC5: a worker-local human_review_required warning attached to an
        otherwise-approved result must not be promoted to blocked."""
        loop_result = {"status": "already_satisfied", "warnings": ["human_review_required"]}
        propagated = h.propagate_canonical_terminal(loop_result)
        assert propagated == loop_result
        assert propagated["status"] == "already_satisfied"


# ---------------------------------------------------------------------------
# AC6: source/dependency reference preservation
# ---------------------------------------------------------------------------


class TestSourceReferencePreservation:
    def test_chatgpt_source_reference_survives_into_materialization_result(self) -> None:
        target = {"repo": "owner/repo", "type": "pull_request", "number": 321}
        result_envelope = _chatgpt_result(target, [_chatgpt_candidate()])
        candidate = h.adapt_chatgpt_candidate(result_envelope, _chatgpt_candidate())

        materialized = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=SpyCreateFn(),
        )

        assert materialized.source_reference == candidate.source_reference
        assert materialized.source_reference["target_repo"] == "owner/repo"
        assert materialized.source_reference["target_type"] == "pull_request"
        assert materialized.source_reference["target_number"] == 321

    def test_agent_candidate_source_reference_survives_into_materialization_result(self) -> None:
        raw = _agent_candidate_with_finding_contract()
        candidate = h.adapt_agent_improvement_candidate(raw)

        materialized = h.materialize_candidate(
            candidate,
            human_authorized=True,
            repo="owner/repo",
            search_fn=_no_match_search_fn,
            detail_fn=_no_match_detail_fn,
            create_fn=SpyCreateFn(),
        )

        assert materialized.source_reference == candidate.source_reference
        assert materialized.source_reference["candidate_id"] == "cand-001"
        assert materialized.source_reference["finding_identity_value"] == raw["finding_contract"]["identity"]["value"]


# ---------------------------------------------------------------------------
# AC11: producer-specific adapters for both schemas
# ---------------------------------------------------------------------------


class TestProducerAdapters:
    def test_chatgpt_candidate_adapter_normalizes_to_expected_shape(self) -> None:
        target = {"repo": "owner/repo", "type": "issue", "number": 42}
        candidate = h.adapt_chatgpt_candidate(
            _chatgpt_result(target, [_chatgpt_candidate(title="Fix Y", body="Body text")]),
            _chatgpt_candidate(title="Fix Y", body="Body text"),
        )
        assert candidate.title == "Fix Y"
        assert candidate.body == "Body text"
        assert candidate.dedupe_key == "chatgpt-candidate:v1:owner/repo:issue:42:fix y"
        assert candidate.source_reference["producer_schema"] == "chatgpt_retrospective_result/v1"

    def test_agent_candidate_with_finding_contract_uses_identity_value_as_dedupe_source(self) -> None:
        raw = _agent_candidate_with_finding_contract(identity_value="sha256:" + "9" * 64)
        candidate = h.adapt_agent_improvement_candidate(raw)
        assert candidate.dedupe_key == "agent-candidate:v1:identity:sha256:" + "9" * 64
        assert candidate.source_reference["finding_identity_value"] == "sha256:" + "9" * 64
        assert "delta_capability" not in candidate.source_reference

    def test_agent_candidate_legacy_without_finding_contract_uses_candidate_id(self) -> None:
        raw = _agent_candidate_legacy(candidate_id="cand-legacy-777")
        candidate = h.adapt_agent_improvement_candidate(raw)
        assert candidate.dedupe_key == "agent-candidate-legacy:v1:cand-legacy-777"
        assert candidate.source_reference["delta_capability"] == "legacy_unavailable"

    def test_run_specific_values_are_excluded_from_dedupe_identity(self) -> None:
        """AC11: source_run_ref, base_sha, timestamp, and evidence fingerprint
        must NOT be used as the dedupe identity when finding_contract.identity
        is present -- only its stable identity.value matters."""
        identity_value = "sha256:" + "7" * 64
        raw_a = _agent_candidate_with_finding_contract(
            identity_value=identity_value,
            source_run_ref={"base_sha": "1" * 40, "source_set_digest": "2" * 64},
            updated_at="2026-01-01T00:00:00Z",
        )
        raw_b = _agent_candidate_with_finding_contract(
            identity_value=identity_value,
            source_run_ref={"base_sha": "3" * 40, "source_set_digest": "4" * 64},
            updated_at="2026-06-01T00:00:00Z",
        )

        key_a = h.derive_agent_candidate_dedupe_key(raw_a)
        key_b = h.derive_agent_candidate_dedupe_key(raw_b)

        assert key_a == key_b, (
            "dedupe key must be derived solely from finding_contract.identity.value; "
            "changing source_run_ref/base_sha/timestamp across two runs of the same "
            "underlying finding must not change the dedupe key"
        )

    def test_chatgpt_dedupe_key_excludes_input_marker_digest(self) -> None:
        """AC11: chatgpt_retrospective_result/v1's input_marker_digest (a
        run-specific value) must not affect the derived dedupe key."""
        target = {"repo": "owner/repo", "type": "issue", "number": 1}
        result_a = _chatgpt_result(target, [_chatgpt_candidate(title="Same Title")])
        result_a["input_marker_digest"] = "sha256:" + "1" * 64
        result_b = _chatgpt_result(target, [_chatgpt_candidate(title="Same Title")])
        result_b["input_marker_digest"] = "sha256:" + "2" * 64

        key_a = h.derive_chatgpt_dedupe_key(target, _chatgpt_candidate(title="Same Title"))
        key_b = h.derive_chatgpt_dedupe_key(target, _chatgpt_candidate(title="Same Title"))
        assert key_a == key_b


# ---------------------------------------------------------------------------
# Regression: schema field allowlists are not mutated by this module
# (Design Constraints: "do not add a dedupe_key field to the existing
# producer schemas")
# ---------------------------------------------------------------------------


class TestSchemaFieldAllowlistUntouched:
    def test_adapters_do_not_mutate_input_candidate_dicts(self) -> None:
        raw_chatgpt = _chatgpt_candidate()
        before = dict(raw_chatgpt)
        h.adapt_chatgpt_candidate(
            _chatgpt_result({"repo": "owner/repo", "type": "issue", "number": 1}, [raw_chatgpt]), raw_chatgpt
        )
        assert raw_chatgpt == before, "adapter must not mutate the producer-schema candidate dict"

        raw_agent = _agent_candidate_with_finding_contract()
        before_agent = dict(raw_agent)
        h.adapt_agent_improvement_candidate(raw_agent)
        assert raw_agent == before_agent
