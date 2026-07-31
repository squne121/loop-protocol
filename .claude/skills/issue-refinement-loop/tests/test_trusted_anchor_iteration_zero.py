from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")
planner = importlib.import_module("plan_refinement_loop")

REPO = "squne121/loop-protocol"
ISSUE = 1835
ANCHOR_BODY = "## Revised Acceptance Criteria\n- AC1: preserve the trusted iteration-zero path."
ANCHOR_URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5136894634"
PRE_BODY = """## Outcome
old outcome

## Acceptance Criteria
- [ ] old contradictory criterion

## Stop Conditions
- old stop condition

## Notes
> ## Acceptance Criteria
> quoted context must not be touched
```md
## Acceptance Criteria
fenced context must not be touched
```
"""


def _anchor(*, association: str = "OWNER", body: str = ANCHOR_BODY) -> dict:
    return {
        "id": 5136894634,
        "html_url": ANCHOR_URL,
        "author_association": association,
        "source_body_sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
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

    evidence = {
        "source_kind": "issue_comment",
        "comment_url": ANCHOR_URL,
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}",
        "author_association": "OWNER",
        "directive_markers": ["revised ac"],
        "extracted_directives": ["AC1: trusted path"],
        "boundary_flags": [],
        "body_sha256": "sha256:anchor",
        "comment_id": 5136894634,
        "source_ref": ANCHOR_URL,
        "captured_at": "2026-08-01T00:00:00Z",
    }
    planner_body = (SKILL_ROOT.parent / "review-issue" / "fixtures" / "pass_issue.md").read_text(encoding="utf-8")
    plan, _ = planner.plan_refinement_loop(
        {
            "schema_version": "refinement_loop_planner_input/v1",
            "issue": {"number": ISSUE, "title": "iteration zero", "body": planner_body, "labels": []},
            "comments": [],
            "known_context": {"repo": REPO, "scope_delta_authority_evidence": [evidence]},
            "now": "2026-08-01T00:00:00+00:00",
        }
    )
    sidecar = plan["scope_signal_guard_decision_v2"]
    assert sidecar["raw_signal"]["triggered"] is False
    assert sidecar["scope_delta_authority"]["route"]["action"] == "contract_update_required"


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


def test_issue_1835_full_post_body_and_non_target_contexts():
    candidate = sda.build_section_aware_candidate_body(
        body=PRE_BODY,
        operations=[{"section": "Acceptance Criteria", "text": "- [ ] AC1: desired", "kind": "replace"}],
        source_identity={"repo": REPO, "issue_number": ISSUE, "comment_id": "5136894634"},
    )
    expected = PRE_BODY.replace("- [ ] old contradictory criterion", "- [ ] AC1: desired")
    assert candidate["candidate_body"] == expected
    assert "> ## Acceptance Criteria" in candidate["candidate_body"]
    assert "fenced context must not be touched" in candidate["candidate_body"]


def test_rebases_body_drift_once_and_revalidates_anchor():
    calls = {"count": 0}
    drifted = PRE_BODY.replace("old outcome", "concurrent outcome")

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
    body = PRE_BODY.replace("- [ ] old contradictory criterion", "- [ ] AC1: desired")
    result = sda.run_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        issue={"body": body},
        anchor=_anchor(),
        anchor_body=ANCHOR_BODY,
        patch_plan=_plan(
            {"section": "Acceptance Criteria", "op": "append", "text": "- [ ] AC1: desired", "source_evidence_index": 0}
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
