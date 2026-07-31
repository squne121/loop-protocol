from __future__ import annotations

import hashlib
import importlib
import json
import base64
import subprocess
import sys
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")
planner = importlib.import_module("plan_refinement_loop")
preflight = importlib.import_module("run_refinement_preflight")

REPO = "squne121/loop-protocol"
ISSUE = 1835
ANCHOR_URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5136894634"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "issue_1835_trusted_anchor_iteration_zero.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
ANCHOR_BODY = base64.b64decode(FIXTURE["anchor"]["raw_body_base64"]).decode("utf-8")
PRE_BODY = base64.b64decode(FIXTURE["pre_body_base64"]).decode("utf-8")
EXPECTED_POST_BODY = base64.b64decode(FIXTURE["expected_post_body_base64"]).decode("utf-8")


def _anchor(*, association: str = "OWNER", body: str = ANCHOR_BODY) -> dict:
    return {
        "id": FIXTURE["anchor"]["comment_id"],
        "html_url": FIXTURE["anchor"]["url"],
        "author_association": association,
        "source_body_sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }


def _plan(*operations: dict) -> dict:
    return {"operations": list(operations)}


def _readiness(_body: str) -> dict:
    return {
        "status": "go",
        "body_sha256": "sha256:candidate",
        "source_checks": [],
        "errors": [],
        "readiness_result_ref": "fixture",
    }


def _current(body: str, anchor: dict):
    return lambda: ({"body": body, "updatedAt": "2026-08-01T00:00:00Z"}, anchor)


def test_classifies_trusted_anchor_without_prior_snapshot():
    result = sda.normalize_trusted_anchor_iteration_zero(
        repo=REPO, issue_number=ISSUE, anchor=_anchor(), source_body=ANCHOR_BODY
    )
    assert result["accepted"] is True
    assert result["source_identity"]["comment_id"] == "5136894634"
    assert result["states"]["directive_acceptance"]["status"] == "accepted"


def test_issue_1835_immutable_fixture_preserves_raw_owner_anchor_and_full_bodies():
    anchor = FIXTURE["anchor"]
    assert FIXTURE["issue_number"] == ISSUE
    assert anchor["comment_id"] == 5136894634
    assert anchor["author_association"] == "OWNER"
    assert anchor["created_at"] == anchor["updated_at"] == "2026-07-30T22:25:10Z"
    assert anchor["url"] == ANCHOR_URL
    assert "sha256:" + hashlib.sha256(ANCHOR_BODY.encode("utf-8")).hexdigest() == anchor["source_body_sha256"]
    assert "sha256:" + hashlib.sha256(PRE_BODY.encode("utf-8")).hexdigest() == FIXTURE["pre_body_sha256"]
    assert "sha256:" + hashlib.sha256(EXPECTED_POST_BODY.encode("utf-8")).hexdigest() == FIXTURE[
        "expected_post_body_sha256"
    ]
    assert "<html>" in ANCHOR_BODY
    assert "<blockquote>" in ANCHOR_BODY
    assert "<pre><code" in ANCHOR_BODY
    assert "# 指定コメントへの返信案" in ANCHOR_BODY


def test_issue_1835_raw_html_quote_fence_and_reply_draft_cannot_be_target_sections():
    unsafe_sections = {
        "指定コメントへの直接回答",
        "P0-2 — `4` が契約にもテストにも含まれていない",
        "指定コメントへの返信案",
    }
    assert unsafe_sections.isdisjoint(sda.extract_sections(PRE_BODY))
    for section in unsafe_sections:
        try:
            sda.build_section_aware_candidate_body(
                body=PRE_BODY,
                operations=[{"section": section, "text": ANCHOR_BODY, "kind": "replace"}],
                source_identity={"repo": REPO},
            )
        except ValueError as exc:
            assert str(exc) == "invalid_section_bound_operation"
        else:
            raise AssertionError(f"anchor-only section was accepted as a target: {section}")


def test_bootstrap_lane_does_not_use_prepatch_human_judgment_as_go():
    result = sda.run_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        issue={"body": PRE_BODY},
        anchor=_anchor(),
        anchor_body=ANCHOR_BODY,
        patch_plan=_plan(
            {
                "section": "Acceptance Criteria",
                "op": "append",
                "text": "- [ ] AC1: trusted path",
                "source_evidence_index": 0,
            }
        ),
        candidate_readiness=lambda _body: {"status": "human_judgment"},
        fetch_current=_current(PRE_BODY, _anchor()),
    )
    assert result["status"] == "blocked"
    assert result["failure"] == "candidate_readiness_not_go"

    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload={**_anchor(), "user": {"login": "squne121", "type": "User"}},
        comment_body=ANCHOR_BODY,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=ANCHOR_URL,
        captured_at=FIXTURE["anchor"]["created_at"],
    )
    assert evidence is not None
    assert evidence["body_sha256"] == FIXTURE["anchor"]["source_body_sha256"].removeprefix("sha256:")
    plan, _ = planner.plan_refinement_loop(
        {
            "schema_version": "refinement_loop_planner_input/v1",
            "issue": {"number": ISSUE, "title": "issue 1835 pre-body", "body": PRE_BODY, "labels": []},
            "comments": [],
            "known_context": {"repo": REPO, "scope_delta_authority_evidence": [evidence]},
            "now": "2026-08-01T00:00:00+00:00",
        }
    )
    sidecar = plan["scope_signal_guard_decision_v2"]
    assert sidecar["raw_signal"]["triggered"] is False
    assert sidecar["scope_delta_authority"]["route"]["action"] == "human_escalation"


def test_patch_plan_is_consumed_by_builder_and_edit_transaction():
    normalized = sda.normalize_trusted_anchor_iteration_zero(
        repo=REPO, issue_number=ISSUE, anchor=_anchor(), source_body=ANCHOR_BODY
    )
    candidate = sda.build_section_aware_candidate_body(
        body=PRE_BODY,
        operations=[
            {
                "section": "Acceptance Criteria",
                "op": "append",
                "text": "- [ ] AC1: trusted path",
                "source_evidence_index": 0,
            }
        ],
        source_identity=normalized["source_identity"],
    )
    txn = sda.build_issue_edit_txn_input(
        issue_number=ISSUE,
        repo=REPO,
        previous_body_sha256="sha256:before",
        previous_updated_at="2026-08-01T00:00:00Z",
        new_body_file="tmp/candidate.md",
        readiness_result=_readiness(candidate["candidate_body"]),
    )
    assert candidate["changed"] is True
    assert txn["schema"] == "ISSUE_EDIT_TXN_INPUT_V1"
    assert txn["readiness_forwarding_payload"]["readiness_result"]["status"] == "go"


def test_preflight_consumer_executes_controlled_transaction_and_final_readback(tmp_path):
    state = {"body": PRE_BODY}
    transaction_inputs: list[dict] = []
    expected_preconditions = sda.extract_sections(EXPECTED_POST_BODY)["Preconditions"]

    def fetch_live_issue(_repo: str, _issue_number: int):
        return ({"body": state["body"], "updatedAt": "2026-08-01T00:00:00Z"}, "")

    def fetch_live_anchor(_repo: str, _comment_id: int):
        return ({**_anchor(), "body": ANCHOR_BODY}, "")

    def controlled_transaction(argv, **_kwargs):
        payload = json.loads((tmp_path / argv[-1]).read_text(encoding="utf-8"))
        transaction_inputs.append(payload)
        state["body"] = (tmp_path / payload["new_body_file"]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"status": "ok"}), "")

    with (
        mock.patch.object(preflight, "_find_repo_root", return_value=tmp_path),
        mock.patch.object(preflight, "_fetch_issue", side_effect=fetch_live_issue),
        mock.patch.object(preflight, "_fetch_single_comment", side_effect=fetch_live_anchor),
        mock.patch.object(preflight.subprocess, "run", side_effect=controlled_transaction),
    ):
        result = preflight.consume_trusted_anchor_contract_patch_plan(
            repo=REPO,
            issue_number=ISSUE,
            issue={"body": PRE_BODY},
            anchor_url=ANCHOR_URL,
            anchor_payload=_anchor(),
            anchor_body=ANCHOR_BODY,
            contract_patch_plan=_plan(
                {
                    "section": "Preconditions",
                    "op": "replace",
                    "kind": "replace",
                    "after_section": "Parent Issue",
                    "text": expected_preconditions,
                    "source_evidence_index": 0,
                }
            ),
            callbacks={
                "candidate_readiness": _readiness,
                "fresh_checks": lambda _issue: {"preflight": "pass", "review": "approve", "readiness": "go"},
            },
        )

    assert result["status"] == "applied", result
    assert transaction_inputs[0]["schema"] == "ISSUE_EDIT_TXN_INPUT_V1"
    assert transaction_inputs[0]["expected_previous_body_sha256"].startswith("sha256:")
    assert "## Preconditions\n" + expected_preconditions in state["body"]
    assert result["fresh_checks"] == {"preflight": "pass", "review": "approve", "readiness": "go"}


def test_bounded_contract_update_handoff_retains_only_parent_routing_fields():
    handoff = preflight._bounded_contract_update_handoff(
        {
            "status": "applied",
            "states": {"contract_update": {"status": "applied"}},
            "writes": 1,
            "iterations": 1,
            "candidate_body": "must not escape the transaction-local phase",
            "fresh_checks": {"preflight": "pass", "review": "approve", "readiness": "go"},
        }
    )
    assert handoff == {
        "status": "rebased",
        "writes": 1,
        "iterations": 1,
        "final_readback": "verified",
        "fresh_preflight": "pass",
        "fresh_review": "approve",
        "fresh_readiness": "go",
    }


def test_live_issue_fetch_requests_updated_at_for_transaction_precondition():
    with mock.patch.object(preflight, "_run_gh", return_value=({"updatedAt": "2026-08-01T00:00:00Z"}, "")) as run_gh:
        issue, error = preflight._fetch_issue(REPO, ISSUE)

    assert error == ""
    assert issue["updatedAt"] == "2026-08-01T00:00:00Z"
    assert "updatedAt" in run_gh.call_args.args[0][-1].split(",")


def test_section_aware_desired_state_replaces_and_removes_contradictions():
    candidate = sda.build_section_aware_candidate_body(
        body=PRE_BODY,
        operations=[
            {
                "section": "Acceptance Criteria",
                "text": "- [ ] AC1: desired",
                "kind": "upsert",
                "remove_text": "old contradictory",
            },
            {"section": "Acceptance Criteria", "text": "- [ ] AC1: desired", "kind": "upsert"},
            {"section": "Stop Conditions", "text": "old stop condition", "kind": "remove"},
        ],
        source_identity={"repo": REPO},
    )
    assert "old contradictory" not in candidate["candidate_body"]
    assert candidate["candidate_body"].count("- [ ] AC1: desired") == 1
    assert "old stop condition" not in sda.extract_sections(candidate["candidate_body"])["Stop Conditions"]


def test_issue_1835_full_post_body_is_built_from_immutable_section_desired_state():
    desired_preconditions = sda.extract_sections(EXPECTED_POST_BODY)["Preconditions"]
    candidate = sda.build_section_aware_candidate_body(
        body=PRE_BODY,
        operations=[
            {
                "section": "Preconditions",
                "text": desired_preconditions,
                "kind": "replace",
                "after_section": "Parent Issue",
            }
        ],
        source_identity={"repo": REPO, "issue_number": ISSUE, "comment_id": "5136894634"},
    )
    assert "## Preconditions\n" + desired_preconditions in candidate["candidate_body"]
    assert EXPECTED_POST_BODY.startswith("## Machine-Readable Contract\n")
    assert "CODEX_DISPATCH_CONTRACT_V1" in EXPECTED_POST_BODY
    assert "#1842" in EXPECTED_POST_BODY


def test_issue_1835_full_post_body_and_non_target_contexts():
    """AC5: the fixture keeps the exact golden body and non-target contexts."""
    assert "sha256:" + hashlib.sha256(EXPECTED_POST_BODY.encode("utf-8")).hexdigest() == FIXTURE[
        "expected_post_body_sha256"
    ]
    unsafe_sections = {"指定コメントへの直接回答", "指定コメントへの返信案"}
    assert unsafe_sections.isdisjoint(sda.extract_sections(EXPECTED_POST_BODY))


def test_rebases_body_drift_once_and_revalidates_anchor():
    calls = {"count": 0}
    drifted = PRE_BODY.replace("## Outcome", "## Outcome\nconcurrent outcome")

    def fetch_current():
        calls["count"] += 1
        return ({"body": drifted if calls["count"] == 1 else drifted}, _anchor())

    result = sda.run_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        issue={"body": PRE_BODY},
        anchor=_anchor(),
        anchor_body=ANCHOR_BODY,
        patch_plan=_plan(
            {"section": "Acceptance Criteria", "op": "append", "text": "- [ ] AC1: desired", "source_evidence_index": 0}
        ),
        candidate_readiness=_readiness,
        fetch_current=fetch_current,
    )
    assert result["status"] == "ready_for_controlled_mutation"
    assert result["iterations"] == 1
    assert "concurrent outcome" in result["candidate_body"]


def test_keeps_authority_fact_precondition_and_update_states_separate():
    result = sda.normalize_trusted_anchor_iteration_zero(
        repo=REPO, issue_number=ISSUE, anchor=_anchor(), source_body=ANCHOR_BODY
    )
    states = result["states"]
    assert set(states) == {"directive_acceptance", "repo_fact_verification", "external_precondition", "contract_update"}
    assert states["directive_acceptance"]["status"] == "accepted"
    assert states["contract_update"]["status"] == "pending"


def test_desired_state_replay_is_no_change_without_write_or_iteration():
    desired = "- [ ] AC1: desired"
    body = sda.build_section_aware_candidate_body(
        body=PRE_BODY,
        operations=[{"section": "Acceptance Criteria", "op": "append", "text": desired}],
        source_identity={"repo": REPO},
    )["candidate_body"]
    result = sda.run_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        issue={"body": body},
        anchor=_anchor(),
        anchor_body=ANCHOR_BODY,
        patch_plan=_plan(
                {"section": "Acceptance Criteria", "op": "append", "text": desired, "source_evidence_index": 0}
        ),
        candidate_readiness=_readiness,
        fetch_current=_current(body, _anchor()),
    )
    assert result["status"] == "no_change"
    assert result["writes"] == 0 and result["iterations"] == 0


def test_fails_closed_only_for_trust_conflict_or_security_boundaries():
    rejected = sda.normalize_trusted_anchor_iteration_zero(
        repo=REPO, issue_number=ISSUE, anchor=_anchor(association="CONTRIBUTOR"), source_body=ANCHOR_BODY
    )
    assert rejected["accepted"] is False
    authority = sda.classify_scope_delta_authority(
        {
            "source_kind": "issue_comment",
            "comment_url": ANCHOR_URL,
            "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}",
            "author_association": "OWNER",
            "directive_markers": ["revised ac"],
            "extracted_directives": ["AC1: desired"],
            "boundary_flags": ["changes_permission_boundary"],
        },
        target_issue_number=ISSUE,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:body",
    )
    assert authority["route"]["action"] == "human_escalation"


def test_final_readback_verifies_postconditions_and_restarts_fresh_preflight():
    state = {"body": PRE_BODY}
    fresh = []

    def fetch_current():
        return ({"body": state["body"]}, _anchor())

    def apply(_issue, candidate, _readiness):
        state["body"] = candidate
        return {"status": "ok"}

    result = sda.run_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        issue={"body": PRE_BODY},
        anchor=_anchor(),
        anchor_body=ANCHOR_BODY,
        patch_plan=_plan(
            {"section": "Acceptance Criteria", "op": "append", "text": "- [ ] AC1: desired", "source_evidence_index": 0}
        ),
        candidate_readiness=_readiness,
        fetch_current=fetch_current,
        apply_transaction=apply,
        fresh_checks=lambda current: (
            fresh.append(current["body"]) or {"preflight": "pass", "review": "approve", "readiness": "go"}
        ),
    )
    assert result["status"] == "applied" and result["writes"] == 1
    assert fresh and result["fresh_checks"]["review"] == "approve"


def test_uses_only_existing_current_main_paths():
    assert not (SCRIPTS_DIR / "build_refinement_phase_state.py").exists()
    assert not (SKILL_ROOT.parent / "issue-author").exists()
    assert (SCRIPTS_DIR / "scope_signal_delta.py").is_file()
