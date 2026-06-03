"""
Tests for LOOP_HANDOFF_RESULT_V1 routing patterns.
Schema SSOT: .claude/skills/issue-refinement-loop/references/termination-policy.md
JSON Schema: .claude/skills/issue-refinement-loop/schemas/loop_handoff_result_v1.json
"""

import json
import pathlib
import pytest
import jsonschema

SCHEMA_PATH = pathlib.Path(__file__).parent.parent / "schemas" / "loop_handoff_result_v1.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


def _base_impl_ready() -> dict:
    """Minimal valid impl_ready payload."""
    return {
        "status": "impl_ready",
        "routing_action": "run_impl_review_loop",
        "contract_review": {
            "status": "go",
            "gate_result": "fresh_go",
            "latest_comment_url": "https://github.com/owner/repo/issues/1#issuecomment-1",
            "generated_at": "2024-01-01T00:00:00Z",
            "body_sha256": "abc123",
        },
        "metadata": {
            "title_prefix_ready": True,
            "phase_label_ready": True,
        },
        "auto_fixes": {
            "required": [],
            "skipped": [],
        },
        "blockers": [],
        "permissions": {"unavailable": []},
        "generated_at": "2024-01-01T00:00:00Z",
    }


def test_fresh_go_impl_ready(schema):
    """Pattern 1: fresh_go + metadata ready + no blockers → impl_ready."""
    payload = _base_impl_ready()
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] == "impl_ready"
    assert payload["routing_action"] == "run_impl_review_loop"
    assert payload["contract_review"]["gate_result"] == "fresh_go"
    assert payload["blockers"] == []
    assert payload["auto_fixes"]["required"] == []
    assert payload["auto_fixes"]["skipped"] == []


def test_missing_go_blocked(schema):
    """Pattern 2: missing contract go → blocked."""
    payload = _base_impl_ready()
    payload["status"] = "blocked"
    payload["routing_action"] = "blocked"
    payload["contract_review"]["status"] = "blocked"
    payload["contract_review"]["gate_result"] = "missing_go"
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] == "blocked"
    assert payload["contract_review"]["gate_result"] == "missing_go"


def test_stale_go_not_impl_ready(schema):
    """Pattern 3: stale_go (body updated after go) → NOT impl_ready."""
    payload = _base_impl_ready()
    payload["status"] = "blocked"
    payload["routing_action"] = "blocked"
    payload["contract_review"]["status"] = "blocked"
    payload["contract_review"]["gate_result"] = "stale_go"
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] != "impl_ready"
    assert payload["contract_review"]["gate_result"] == "stale_go"


def test_request_changes_after_go_not_impl_ready(schema):
    """Pattern 4: valid request_changes after go → NOT impl_ready."""
    payload = _base_impl_ready()
    payload["status"] = "blocked"
    payload["routing_action"] = "blocked"
    payload["contract_review"]["status"] = "blocked"
    payload["contract_review"]["gate_result"] = "invalidated_by_request_changes"
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] != "impl_ready"
    assert payload["contract_review"]["gate_result"] == "invalidated_by_request_changes"


def test_auto_fix_evidence_present_impl_ready(schema):
    """Pattern 5: title prefix/phase label missing + auto-fix evidence present → impl_ready OK."""
    payload = _base_impl_ready()
    payload["metadata"]["title_prefix_ready"] = False
    payload["metadata"]["phase_label_ready"] = False
    # auto-fix was applied: impl_ready is allowed
    payload["auto_fixes"]["required"] = [
        {
            "kind": "metadata_hygiene",
            "executor": "deterministic-fixer",
            "result": "applied",
            "evidence": {
                "before": "old title",
                "after": "実装: new title",
                "comment_url": "https://github.com/owner/repo/issues/1#issuecomment-2",
            },
        }
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] == "impl_ready"
    assert payload["auto_fixes"]["required"][0]["result"] == "applied"


def test_fixer_unavailable_not_impl_ready(schema):
    """Pattern 6: title prefix/phase label missing + fixer unavailable → NOT impl_ready."""
    payload = _base_impl_ready()
    payload["status"] = "human_judgment_required"
    payload["routing_action"] = "ask_human"
    payload["metadata"]["title_prefix_ready"] = False
    payload["metadata"]["phase_label_ready"] = False
    payload["auto_fixes"]["skipped"] = [
        {
            "kind": "metadata_hygiene",
            "executor": "deterministic-fixer",
            "result": "skipped",
            "evidence": {
                "before": "old title",
                "after": "old title",
                "comment_url": "https://github.com/owner/repo/issues/1#issuecomment-3",
            },
        }
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] != "impl_ready"
    assert payload["auto_fixes"]["skipped"][0]["result"] == "skipped"


def test_scope_change_human_judgment_required(schema):
    """Pattern 7: scope/goal/AC semantic change → human_judgment_required."""
    payload = _base_impl_ready()
    payload["status"] = "human_judgment_required"
    payload["routing_action"] = "ask_human"
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] == "human_judgment_required"
    assert payload["routing_action"] == "ask_human"


def test_blocker_exists_blocked(schema):
    """Pattern 8: blocker exists → blocked."""
    payload = _base_impl_ready()
    payload["status"] = "blocked"
    payload["routing_action"] = "blocked"
    payload["blockers"] = [
        {"kind": "dependency_not_closed", "description": "Issue #100 is still open"}
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert payload["status"] == "blocked"
    assert len(payload["blockers"]) > 0
