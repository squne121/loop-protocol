"""AC7: generic mentions and references never establish Task equivalence."""

from __future__ import annotations

import classifier
import task_context_service as service
import task_context_workflow_signals as signals
from workflow_signal_test_support import REPO, create_origin, implementation_payload


def test_given_reference_only_issue_mention_when_prompt_is_classified_then_it_is_not_an_authoritative_target():
    result = classifier.classify("For reference: see squne121/loop-protocol#20", current_repo=REPO)
    assert result.kind == classifier.KIND_REFERENCE_ONLY
    assert result.target is not None
    assert result.target.ref_number == 20


def test_given_generic_claim_like_mention_without_valid_workflow_fact_when_service_runs_then_no_claim_is_attached(conn):
    task, _, _, _ = create_origin(conn)
    # The service does not infer claims from branch/comment-like strings; only
    # its strict workflow envelope is authoritative.
    invalid = implementation_payload()
    invalid["evidence"] = {"repo": REPO, "issue_number": 20, "pr_number": "Refs #21"}
    assert (
        signals.apply_workflow_signal(conn, invalid, origin_session_id="session-1")["disposition"]
        == "rejected_evidence"
    )
    assert service.find_live_claim(conn, REPO, "issue", 20) is None
    assert task["id"]


def test_given_explicit_pr_prompt_when_submit_fields_are_built_then_hot_path_does_not_resolve_current_repo(monkeypatch):
    import hook_entry

    payload = {}
    monkeypatch.setattr(
        hook_entry,
        "_current_repo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("explicit target must stay local-only")),
    )
    hook_entry._apply_user_prompt_submit_fields(
        payload,
        {"prompt": "Review https://github.com/squne121/loop-protocol/pull/21", "cwd": "/unused"},
    )
    assert payload == {
        "classification_kind": classifier.KIND_EXPLICIT,
        "target_repo": "squne121/loop-protocol",
        "target_ref_kind": "pr",
        "target_ref_number": 21,
    }
