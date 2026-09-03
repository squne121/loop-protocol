"""Regression contract for trusted close/not-planned anchor dispositions."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCHEMA_PATH = SKILL_ROOT / "schemas" / "anchor_scope_reframe_v1.schema.json"
sys.path.insert(0, str(SCRIPTS_DIR))
import run_refinement_preflight as preflight  # noqa: E402

REPO = "squne121/loop-protocol"
ISSUE = 2472
URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-2472001"


def _schema_errors(payload: dict) -> list[object]:
    return list(jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text())).iter_errors(payload))


def _close_payload(*, include_deltas: bool = False) -> dict:
    payload = {
        "schema_version": "ANCHOR_SCOPE_REFRAME_V1",
        "target": {"repo": REPO, "issue_number": ISSUE},
        "decision": "close_not_planned",
        "rationale": "The requested work is not planned.",
        "required_rerun": ["refinement_preflight"],
    }
    if include_deltas:
        payload["allowed_path_deltas"] = ["docs/not-allowed.md"]
    return payload


def _approve_payload() -> dict:
    return {
        "schema_version": "ANCHOR_SCOPE_REFRAME_V1",
        "target": {"repo": REPO, "issue_number": ISSUE},
        "decision": "approve_scope_delta",
        "allowed_path_deltas": ["docs/allowed.md"],
        "rationale": "Explicit scope approval.",
        "required_rerun": ["refinement_preflight"],
    }


def _yaml(payload: dict) -> str:
    deltas = ""
    if "allowed_path_deltas" in payload:
        deltas = "allowed_path_deltas:\n" + "".join(f"  - {item}\n" for item in payload["allowed_path_deltas"])
    return (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        f"target:\n  repo: {REPO}\n  issue_number: {ISSUE}\n"
        f"decision: {payload['decision']}\n"
        f"{deltas}"
        f"rationale: {payload['rationale']}\n"
        "required_rerun:\n  - refinement_preflight\n```\n"
    )


def _classify(body: str, association: str = "OWNER") -> dict:
    return preflight._classify_anchor_scope_reframe(
        comment_payload={"id": 2472001, "body": body, "author_association": association},
        anchor_body=body,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
    )


def test_close_not_planned_valid_without_allowed_path_deltas():
    assert _schema_errors(_close_payload()) == []


def test_approve_scope_delta_still_requires_allowed_path_deltas():
    payload = _approve_payload()
    del payload["allowed_path_deltas"]
    assert _schema_errors(payload)


def test_close_not_planned_rejects_allowed_path_deltas():
    assert _schema_errors(_close_payload(include_deltas=True))


def test_classify_anchor_scope_reframe_close_not_planned_owner_approved():
    result = _classify(_yaml(_close_payload()))
    assert result["status"] == "approved_by_trusted_anchor"
    assert result["decision"] == "close_not_planned"
    assert result["authorized_mutation_category"] == "not_planned"
    assert result["implementation_go"] is False
    assert "allowed_path_deltas" not in result


def test_classify_anchor_scope_reframe_close_not_planned_non_owner_rejected():
    assert _classify(_yaml(_close_payload()), "CONTRIBUTOR")["status"] != "approved_by_trusted_anchor"


def test_close_not_planned_member_collaborator_not_approved_but_scope_delta_trust_unaffected():
    for association in ("MEMBER", "COLLABORATOR"):
        assert _classify(_yaml(_close_payload()), association)["status"] != "approved_by_trusted_anchor"
        assert _classify(_yaml(_approve_payload()), association)["status"] == "approved_by_trusted_anchor"


def test_heavy_mutation_gate_allows_close_not_planned():
    decision = _classify(_yaml(_close_payload()))
    assert preflight._classify_heavy_mutation_gate(
        mutation_category="not_planned", scope_delta_decision=decision
    )["status"] == "allowed"


def test_heavy_mutation_gate_rejects_mismatched_category():
    decision = _classify(_yaml(_close_payload()))
    for category in ("close", "replacement_issue_creation", "dependency_removal", "parent_child_change"):
        assert preflight._classify_heavy_mutation_gate(
            mutation_category=category, scope_delta_decision=decision
        )["status"] != "allowed"


def test_preflight_with_human_context_close_not_planned_routes_human_judgment_required(tmp_path, monkeypatch):
    """Exercise the production run_preflight path without GitHub mutation."""
    body = _yaml(_close_payload())
    comment = {
        "id": 2472001,
        "body": body,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "html_url": URL,
        "url": f"https://api.github.com/repos/{REPO}/issues/comments/2472001",
        "user": {"login": "owner", "type": "User"},
        "author_association": "OWNER",
    }
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": ISSUE,
        "repo": REPO,
        "now": "2026-09-01T00:00:00Z",
        "issue": {"number": ISSUE, "title": "test", "body": "", "labels": []},
        "comments": [],
        "anchor_comment_urls": [URL],
        "anchor_comments": [comment],
    }))
    monkeypatch.setattr(
        preflight,
        "_invoke_repair",
        lambda _body: {"schema": "repair_issue_contract/v1", "changed": False, "repairs": []},
    )
    monkeypatch.setattr(preflight, "build_structural_repair_bundle", None)
    monkeypatch.setattr(preflight, "route_structural_repair_disposition", None)
    monkeypatch.setattr(
        preflight,
        "_invoke_planner",
        lambda *_args, **_kwargs: (
            {"fail_closed": {"required": True, "reason_codes": ["missing_required_section"]}, "decisions": {}},
            0, "", "{}",
        ),
    )
    result, _ = preflight.run_preflight(
        issue_number=ISSUE, repo=REPO, anchor_comment_urls=[URL], fixture_path=fixture_path
    )
    assert result["status"] != "blocked"
    assert result["next_action"] == "human_judgment_required"
    assert "missing_required_section" not in result["blockers"]
    assert result.get("contract_update") is None
    assert _classify(body)["implementation_go"] is False
    assert "allowed_path_deltas" not in _classify(body)


def test_missing_required_section_without_close_decision_still_blocked():
    status, _ = preflight._apply_exit_code_mapping(
        0, True, [preflight.BLOCKER_FAIL_CLOSED, "missing_required_section"]
    )
    assert status == "blocked"


def test_freeform_terminal_marker_parity_with_structured():
    result = _classify("Owner decision follows.\n\nDecision: CLOSE / NOT_PLANNED\n")
    assert result["status"] == "approved_by_trusted_anchor"
    assert result["authorized_mutation_category"] == "not_planned"
    assert result["implementation_go"] is False


def test_freeform_terminal_marker_in_fenced_code_not_recognized():
    assert _classify("```text\nDecision: CLOSE / NOT_PLANNED\n```\n")["status"] == "not_applicable"


def test_freeform_terminal_marker_in_blockquote_not_recognized():
    assert _classify("> Decision: CLOSE / NOT_PLANNED\n")["status"] == "not_applicable"


def test_freeform_prose_mention_without_exact_marker_not_recognized():
    assert _classify("This may CLOSE / NOT_PLANNED, pending discussion.\n")["status"] == "not_applicable"
