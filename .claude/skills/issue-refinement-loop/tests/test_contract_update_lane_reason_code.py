"""#2842 AC3-AC5: bounded consumer failures never impersonate planner failures."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
import run_refinement_preflight as preflight  # noqa: E402

ISSUE = 92842
REPO = "squne121/loop-protocol"
URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-92842"
OWNER_TEXT = "OWNER_PRIVATE_BODY_SENTINEL: write 30 raw lines to Acceptance Criteria"
BODY = (
    "## Machine-Readable Contract\n```yaml\ncontract_schema_version: v1\n"
    "issue_kind: implementation\nparent_issue: none\ngoal_ref: test\nchange_kind: workflow\n```\n"
    "## Outcome\nstatic test\n## In Scope\n- test\n## Out of Scope\n- none\n"
    "## Acceptance Criteria\n- [ ] AC1: test\n## Verification Commands\n```bash\n$ true\n```\n"
    "## Allowed Paths\n- `docs/product/features/example.md`\n## Stop Conditions\n- none\n"
    "## Runtime Verification Applicability\n- decision: not_applicable\n"
)


@pytest.mark.parametrize("consumer_failure,expected", [
    ("unsafe_unstructured_patch_operation", "unsafe_unstructured_patch_operation"),
    ("contract_patch_plan_missing", "contract_patch_plan_missing"),
    (OWNER_TEXT, "contract_update_failed"),
])
def test_given_consumer_failure_when_preflight_runs_then_artifact_stdout_and_planner_are_consistent(
    tmp_path, monkeypatch, capsys, consumer_failure, expected,
):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "schema_version": "refinement_preflight_input/v1", "issue_number": ISSUE, "repo": REPO,
        "now": "2026-09-29T00:00:00Z",
        "issue": {"number": ISSUE, "title": "test", "body": BODY, "labels": []},
        "comments": [], "anchor_comment_urls": [URL],
        "anchor_comments": [{"id": ISSUE, "body": OWNER_TEXT, "html_url": URL,
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
            "created_at": "2026-09-29T00:00:00Z", "updated_at": "2026-09-29T00:00:00Z",
            "url": f"https://api.github.com/repos/{REPO}/issues/comments/{ISSUE}",
            "user": {"login": "squne121", "type": "User"}, "author_association": "OWNER"}],
    }), encoding="utf-8")
    original = preflight._invoke_planner

    def planner_without_fail_closed(*args, **kwargs):
        plan, _exit, stderr, stdout = original(*args, **kwargs)
        plan["fail_closed"] = {"required": False, "reason_codes": []}
        return plan, 0, stderr, stdout

    monkeypatch.setattr(preflight, "_invoke_planner", planner_without_fail_closed)
    monkeypatch.setattr(preflight, "consume_trusted_anchor_contract_patch_plan", lambda **_kw: {
        "status": "blocked", "failure": consumer_failure, "writes": 0, "iterations": 0,
    })
    artifact_dir = SKILL.parent.parent / "artifacts" / "issue-refinement-loop" / str(ISSUE)
    assert not artifact_dir.exists(), "test must never erase someone else's artifacts"
    try:
        result, _exit = preflight.run_preflight(
            issue_number=ISSUE, repo=REPO, fixture_path=fixture_path,
            anchor_comment_urls=[URL], known_context={"human_context_comment_urls": [URL]},
            consume_contract_patch_plan=True,
        )
        stdout = capsys.readouterr().out
        handoff = result["contract_update"]
        assert handoff["status"] == "failed"
        assert handoff["reason_code"] == expected
        assert handoff["writes"] == 0
        assert result["planner_fail_closed"] is False
        assert result["planner_fail_closed_reason_codes"] == []
        assert "PLANNER_FAIL_CLOSED" not in result["blockers"]
        assert "CONTRACT_UPDATE_FAILED" in result["blockers"]
        assert f"CONTRACT_UPDATE_REASON_CODE: {expected}" in stdout
        assert OWNER_TEXT not in stdout
        artifact = json.loads(Path(result["artifacts"]["refinement_preflight_result_v1"]).read_text())
        assert artifact["contract_update"]["reason_code"] == expected
        assert OWNER_TEXT not in json.dumps(artifact, ensure_ascii=False)
        assert preflight._validate_result_artifact(artifact) == []
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)


@pytest.mark.parametrize("consumer_result,expected", [
    ({"status": "invalid", "disposition": {"disposition": "invalid", "reason_code": [OWNER_TEXT]}},
     "invalid_scope_delta_decision"),
    ({"status": "no_change", "rewrite_route": {"route": "issue_editor_required",
        "disposition": "full_rewrite_required", "reason_code": {"raw": OWNER_TEXT}}},
     "contract_update_failed"),
])
def test_given_malformed_consumer_reason_when_handoff_projects_then_no_raw_text_or_crash(
    consumer_result, expected,
):
    handoff = preflight._bounded_contract_update_handoff(consumer_result)
    assert handoff["status"] == "failed"
    assert handoff["reason_code"] == expected
    assert OWNER_TEXT not in json.dumps(handoff)
