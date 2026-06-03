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
    """Minimal valid impl_ready payload (wrapper form)."""
    return {
        "LOOP_HANDOFF_RESULT_V1": {
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
                "result": "auto_fixed",
                "required": [],
                "skipped": [],
            },
            "blockers": [],
            "permissions": {"unavailable": []},
            "generated_at": "2024-01-01T00:00:00Z",
        }
    }


def test_fresh_go_impl_ready(schema):
    """Pattern 1: fresh_go + metadata ready + no blockers → impl_ready."""
    payload = _base_impl_ready()
    jsonschema.validate(instance=payload, schema=schema)
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    assert inner["status"] == "impl_ready"
    assert inner["routing_action"] == "run_impl_review_loop"
    assert inner["contract_review"]["gate_result"] == "fresh_go"
    assert inner["blockers"] == []
    assert inner["auto_fixes"]["required"] == []
    assert inner["auto_fixes"]["skipped"] == []


def test_missing_go_blocked(schema):
    """Pattern 2: missing contract go → blocked."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "blocked"
    inner["routing_action"] = "blocked"
    inner["contract_review"]["status"] = "blocked"
    inner["contract_review"]["gate_result"] = "missing_go"
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] == "blocked"
    assert inner["contract_review"]["gate_result"] == "missing_go"


def test_stale_go_not_impl_ready(schema):
    """Pattern 3: stale_go (body updated after go) → NOT impl_ready."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "blocked"
    inner["routing_action"] = "blocked"
    inner["contract_review"]["status"] = "blocked"
    inner["contract_review"]["gate_result"] = "stale_go"
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] != "impl_ready"
    assert inner["contract_review"]["gate_result"] == "stale_go"


def test_request_changes_after_go_not_impl_ready(schema):
    """Pattern 4: valid request_changes after go → NOT impl_ready."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "blocked"
    inner["routing_action"] = "blocked"
    inner["contract_review"]["status"] = "blocked"
    inner["contract_review"]["gate_result"] = "invalidated_by_request_changes"
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] != "impl_ready"
    assert inner["contract_review"]["gate_result"] == "invalidated_by_request_changes"


def test_auto_fix_evidence_present_impl_ready(schema):
    """Pattern 5: title prefix/phase label missing + auto-fix evidence present → impl_ready OK."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["metadata"]["title_prefix_ready"] = False
    inner["metadata"]["phase_label_ready"] = False
    # auto-fix was applied: impl_ready is allowed
    inner["auto_fixes"]["required"] = [
        {
            "kind": "metadata_hygiene",
            "executor": "implementation-worker",
            "result": "applied",
            "evidence": {
                "before": "old title",
                "after": "実装: new title",
                "comment_url": "https://github.com/owner/repo/issues/1#issuecomment-2",
            },
        }
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] == "impl_ready"
    assert inner["auto_fixes"]["required"][0]["result"] == "applied"


def test_fixer_unavailable_not_impl_ready(schema):
    """Pattern 6: title prefix/phase label missing + fixer unavailable → NOT impl_ready."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "human_judgment_required"
    inner["routing_action"] = "ask_human"
    inner["metadata"]["title_prefix_ready"] = False
    inner["metadata"]["phase_label_ready"] = False
    inner["auto_fixes"]["result"] = "human_judgment_required"
    inner["auto_fixes"]["skipped"] = [
        {
            "kind": "metadata_hygiene",
            "executor": "implementation-worker",
            "result": "skipped",
            "evidence": {
                "before": "old title",
                "after": "old title",
                "comment_url": "https://github.com/owner/repo/issues/1#issuecomment-3",
            },
        }
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] != "impl_ready"
    assert inner["auto_fixes"]["skipped"][0]["result"] == "skipped"


def test_scope_change_human_judgment_required(schema):
    """Pattern 7: scope/goal/AC semantic change → human_judgment_required."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "human_judgment_required"
    inner["routing_action"] = "ask_human"
    inner["auto_fixes"]["result"] = "human_judgment_required"
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] == "human_judgment_required"
    assert inner["routing_action"] == "ask_human"


def test_blocker_exists_blocked(schema):
    """Pattern 8: blocker exists → blocked."""
    payload = _base_impl_ready()
    inner = payload["LOOP_HANDOFF_RESULT_V1"]
    inner["status"] = "blocked"
    inner["routing_action"] = "blocked"
    inner["auto_fixes"]["result"] = "blocked"
    inner["blockers"] = [
        {"kind": "dependency_not_closed", "description": "Issue #100 is still open"}
    ]
    jsonschema.validate(instance=payload, schema=schema)
    assert inner["status"] == "blocked"
    assert len(inner["blockers"]) > 0


# ---------------------------------------------------------------------------
# Negative tests: impl_ready invariants must be enforced by schema
# ---------------------------------------------------------------------------

def test_impl_ready_with_missing_go_is_invalid(schema):
    """impl_ready with gate_result: missing_go must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["contract_review"]["gate_result"] = "missing_go"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_stale_go_is_invalid(schema):
    """impl_ready with gate_result: stale_go must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["contract_review"]["gate_result"] = "stale_go"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_invalidated_go_is_invalid(schema):
    """impl_ready with gate_result: invalidated_by_request_changes must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["contract_review"]["gate_result"] = "invalidated_by_request_changes"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_blockers_is_invalid(schema):
    """impl_ready with non-empty blockers must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["blockers"] = [
        {"kind": "some_blocker", "description": "something"}
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_skipped_autofixes_is_invalid(schema):
    """impl_ready with non-empty auto_fixes.skipped must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["auto_fixes"]["skipped"] = [
        {
            "kind": "metadata_hygiene",
            "executor": "implementation-worker",
            "result": "skipped",
            "evidence": {
                "before": "old",
                "after": "old",
                "comment_url": "https://github.com/owner/repo/issues/1#issuecomment-4",
            },
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_ask_human_routing_is_invalid(schema):
    """impl_ready with routing_action: ask_human must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["routing_action"] = "ask_human"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)


def test_impl_ready_with_unavailable_permissions_is_invalid(schema):
    """impl_ready with non-empty permissions.unavailable must fail schema validation."""
    payload = _base_impl_ready()
    payload["LOOP_HANDOFF_RESULT_V1"]["permissions"]["unavailable"] = ["some_permission"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=schema)
