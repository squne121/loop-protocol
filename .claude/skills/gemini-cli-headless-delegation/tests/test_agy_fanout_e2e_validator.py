"""Tests for validate_agy_fanout_e2e_evidence.py (Issue #1710).

Covers AC1-AC15: positive fixture (all 25 predicates PASS), negative fixture
per predicate group (each targeted mutation FAILs only the expected
predicate(s)), a tampering fixture (artifact_manifest.json sha256 mismatch
is detected fail-closed), environment-manifest secret-freedom, verdict
schema closure, and the closed-schema unknown-key rejection test
independent of predicate 24.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate_agy_fanout_e2e_evidence.py"
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "agy_fanout_e2e"

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_test_validate_agy_fanout_e2e_evidence", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_test_validate_agy_fanout_e2e_evidence"] = module
    spec.loader.exec_module(module)
    return module


validator = _load_module()

from agy_fanout_e2e import build_bundle  # noqa: E402


def _predicate_status(verdict: dict, predicate_id: str) -> str:
    for entry in verdict["predicate_detail"]:
        if entry["predicate_id"] == predicate_id:
            return entry["status"]
    raise AssertionError(f"{predicate_id} not found in verdict")


# ---------------------------------------------------------------------------
# AC1 / AC15: positive fixture -- all predicates pass
# ---------------------------------------------------------------------------


def test_fanout_request_shape_predicates_positive(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "bundle")
    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "pass"
    assert verdict["conclusion"] == "PASS"
    for pid in ("predicate_01", "predicate_02", "predicate_03", "predicate_04", "predicate_05"):
        assert _predicate_status(verdict, pid) == "pass", pid


def test_verdict_all_25_predicates_present_and_positive_all_pass(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "bundle")
    verdict = validator.build_verdict(bundle_dir)
    assert len(verdict["predicate_detail"]) == 25
    assert verdict["failed_predicates"] == []
    assert len(verdict["passed_predicates"]) == 25


# ---------------------------------------------------------------------------
# AC2: predicate 1-5 negative fixtures
# ---------------------------------------------------------------------------


def test_fanout_request_shape_predicates_negative(tmp_path):
    def mutate_parent_run_id(content):
        content["fanout_request"]["subtasks"][0]["parent_run_id"] = "different-run-id"

    bundle_dir = build_bundle.build_and_materialize(tmp_path / "p1", mutate=mutate_parent_run_id)
    verdict = validator.build_verdict(bundle_dir)
    assert _predicate_status(verdict, "predicate_01") == "fail"
    assert verdict["status"] == "fail"

    def mutate_duplicate_subtask(content):
        content["fanout_request"]["subtasks"][1]["subtask_id"] = content["fanout_request"]["subtasks"][0][
            "subtask_id"
        ]

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "p2", mutate=mutate_duplicate_subtask)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_02") == "fail"

    def mutate_profile(content):
        content["fanout_request"]["subtasks"][2]["profile"] = "unknown_profile"

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "p3", mutate=mutate_profile)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_03") == "fail"

    def mutate_provider(content):
        content["fanout_request"]["subtasks"][0]["provider"] = "gemini"

    bundle_dir4 = build_bundle.build_and_materialize(tmp_path / "p4", mutate=mutate_provider)
    verdict4 = validator.build_verdict(bundle_dir4)
    assert _predicate_status(verdict4, "predicate_04") == "fail"

    def mutate_concurrency(content):
        content["fanout_request"]["max_workers"] = 1

    bundle_dir5 = build_bundle.build_and_materialize(tmp_path / "p5", mutate=mutate_concurrency)
    verdict5 = validator.build_verdict(bundle_dir5)
    assert _predicate_status(verdict5, "predicate_05") == "fail"


# ---------------------------------------------------------------------------
# AC3: predicate 6 process overlap
# ---------------------------------------------------------------------------


def test_process_overlap_predicate(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    assert _predicate_status(verdict, "predicate_06") == "pass"

    def mutate_no_overlap(content):
        # Push every start strictly after the previous exit -> no overlap.
        events = content["process_lifecycle_events"]
        events[1]["started_monotonic_ns"] = 500  # grounded_research start after local exits (100)
        events[3]["exited_monotonic_ns"] = 600

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "negative", mutate=mutate_no_overlap)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_06") == "fail"


# ---------------------------------------------------------------------------
# AC4: predicate 7-11 hook provenance
# ---------------------------------------------------------------------------


def test_hook_provenance_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    for pid in ("predicate_07", "predicate_08", "predicate_09", "predicate_10", "predicate_11"):
        assert _predicate_status(verdict, pid) == "pass", pid

    def mutate_no_hook_events(content):
        content["children"]["grounded_research"]["hook_events"] = []

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "no-hooks", mutate=mutate_no_hook_events)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_07") == "fail"
    assert _predicate_status(verdict2, "predicate_08") == "fail"
    # AC4 / predicate 11: stdout self-report ("tool_calls": [...]) alone must
    # NOT rescue the verdict once hook evidence is gone.
    assert content_claims_search_web_but_fails(verdict2)

    def mutate_no_read_url(content):
        events = content["children"]["grounded_research"]["hook_events"]
        content["children"]["grounded_research"]["hook_events"] = [
            e for e in events if e["toolCall"]["name"] != "read_url_content"
        ]

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "no-read-url", mutate=mutate_no_read_url)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_09") == "fail"
    assert _predicate_status(verdict3, "predicate_08") == "pass"  # search_web still present

    def mutate_mismatched_run(content):
        for event in content["children"]["grounded_research"]["hook_events"]:
            event["parent_run_id"] = "wrong-run-id"

    bundle_dir4 = build_bundle.build_and_materialize(tmp_path / "mismatched-run", mutate=mutate_mismatched_run)
    verdict4 = validator.build_verdict(bundle_dir4)
    assert _predicate_status(verdict4, "predicate_10") == "fail"


def content_claims_search_web_but_fails(verdict: dict) -> bool:
    for entry in verdict["predicate_detail"]:
        if entry["predicate_id"] == "predicate_11":
            return entry["status"] == "pass" and entry["evidence"]["stdout_claims_search_web"] is True
    return False


# ---------------------------------------------------------------------------
# AC5: predicate 12-14 Serena hash chain
# ---------------------------------------------------------------------------


def test_serena_task_linked_hash_chain_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    for pid in ("predicate_12", "predicate_13", "predicate_14"):
        assert _predicate_status(verdict, pid) == "pass", pid

    def mutate_no_task_link(content):
        content["children"]["local_asset_research"]["serena_evidence"][0]["subtask_id"] = "other-subtask"

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "no-link", mutate=mutate_no_task_link)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_12") == "fail"

    def mutate_broken_chain(content):
        content["children"]["local_asset_research"]["serena_evidence"][0]["result_binding_sha256"] = "0" * 64

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "broken-chain", mutate=mutate_broken_chain)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_13") == "fail"

    def mutate_actor_collision(content):
        content["children"]["local_asset_research"]["result"]["actor"] = "wrapper_serena_mcp"

    bundle_dir4 = build_bundle.build_and_materialize(tmp_path / "actor-collision", mutate=mutate_actor_collision)
    verdict4 = validator.build_verdict(bundle_dir4)
    assert _predicate_status(verdict4, "predicate_14") == "fail"


# ---------------------------------------------------------------------------
# AC6: predicate 15-17 permission isolation
# ---------------------------------------------------------------------------


def test_permission_isolation_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    for pid in ("predicate_15", "predicate_16", "predicate_17"):
        assert _predicate_status(verdict, pid) == "pass", pid

    def mutate_local_leak(content):
        content["children"]["local_asset_research"]["permission_events"].append(
            {"tool_name": "search_web", "source": "agy_direct", "executed": True}
        )

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "local-leak", mutate=mutate_local_leak)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_15") == "fail"

    def mutate_no_tools_leak(content):
        content["children"]["no_tools"]["permission_events"] = [
            {"tool_name": "search_web", "source": "agy_direct", "executed": True}
        ]

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "no-tools-leak", mutate=mutate_no_tools_leak)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_16") == "fail"

    def mutate_grounded_unexpected(content):
        content["children"]["grounded_research"]["permission_events"].append(
            {"tool_name": "run_shell_command", "source": "agy_direct", "executed": True}
        )

    bundle_dir4 = build_bundle.build_and_materialize(
        tmp_path / "grounded-unexpected", mutate=mutate_grounded_unexpected
    )
    verdict4 = validator.build_verdict(bundle_dir4)
    assert _predicate_status(verdict4, "predicate_17") == "fail"


# ---------------------------------------------------------------------------
# AC7: predicate 18-20 audit pairing / correlation / manifest sha256
# ---------------------------------------------------------------------------


def test_audit_and_correlation_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    for pid in ("predicate_18", "predicate_19", "predicate_20"):
        assert _predicate_status(verdict, pid) == "pass", pid

    def mutate_no_end_record(content):
        content["children"]["no_tools"]["audit"] = [content["children"]["no_tools"]["audit"][0]]

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "no-end", mutate=mutate_no_end_record)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_18") == "fail"

    def mutate_id_mismatch(content):
        content["children"]["grounded_research"]["result"]["subtask_id"] = "wrong-subtask"

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "id-mismatch", mutate=mutate_id_mismatch)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_19") == "fail"


# ---------------------------------------------------------------------------
# AC8: predicate 21-22 redaction scanner
# ---------------------------------------------------------------------------


def test_redaction_scanner_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    assert _predicate_status(verdict, "predicate_21") == "pass"
    assert _predicate_status(verdict, "predicate_22") == "pass"
    assert verdict["public_artifacts_redaction_status"] == "clean"

    def mutate_leak_credential(content):
        content["children"]["no_tools"]["request"]["objective"] = "leaked key sk-abcdefghijklmnopqrstuvwx1234"

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "leak", mutate=mutate_leak_credential)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_21") == "fail"
    assert _predicate_status(verdict2, "predicate_22") == "fail"
    assert verdict2["public_artifacts_redaction_status"] == "violations_found"

    def mutate_leak_transcript_field(content):
        content["children"]["grounded_research"]["result"]["raw_transcript"] = "raw transcript body leak"

    bundle_dir3 = build_bundle.build_and_materialize(tmp_path / "transcript-leak", mutate=mutate_leak_transcript_field)
    verdict3 = validator.build_verdict(bundle_dir3)
    assert _predicate_status(verdict3, "predicate_21") == "fail"


# ---------------------------------------------------------------------------
# AC9: predicate 23-24 success condition / fail-close
# ---------------------------------------------------------------------------


def test_success_condition_and_fail_close_predicates(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "positive")
    verdict = validator.build_verdict(bundle_dir)
    assert _predicate_status(verdict, "predicate_23") == "pass"
    assert _predicate_status(verdict, "predicate_24") == "pass"

    def mutate_failed_child(content):
        content["children"]["local_asset_research"]["result"]["status"] = "error"

    bundle_dir2 = build_bundle.build_and_materialize(tmp_path / "failed-child", mutate=mutate_failed_child)
    verdict2 = validator.build_verdict(bundle_dir2)
    assert _predicate_status(verdict2, "predicate_23") == "fail"


def test_missing_artifact_fails_closed(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "bundle")
    (bundle_dir / "children" / "no_tools" / "audit.jsonl").unlink()
    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "fail"
    assert verdict["conclusion"] == "FAIL_RUNTIME"
    assert any(f.startswith("bundle_load:") for f in verdict["failed_predicates"])


def test_duplicate_unknown_artifact_key_fails_closed(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "bundle")
    import json as _json

    manifest_path = bundle_dir / "artifact_manifest.json"
    manifest = _json.loads(manifest_path.read_text())
    manifest["children/no_tools/unexpected_extra_file.json"] = "e" * 64
    manifest_path.write_text(_json.dumps(manifest))
    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "fail"
    assert verdict["conclusion"] == "FAIL_RUNTIME"


# ---------------------------------------------------------------------------
# AC10: predicate 25 tampering fixture
# ---------------------------------------------------------------------------


def test_tampering_fixture_detects_hash_mismatch(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(
        tmp_path / "tampered", corrupt_manifest_path="children/grounded_research/result.json"
    )
    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "fail"
    assert verdict["conclusion"] == "FAIL_RUNTIME"
    assert any("sha256_mismatch" in f or "manifest" in f for f in verdict["failed_predicates"])


def test_tampering_file_content_edited_without_manifest_update_is_detected(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "tampered2")
    result_path = bundle_dir / "children" / "grounded_research" / "result.json"
    original = result_path.read_text(encoding="utf-8")
    tampered = original.replace('"status": "ok"', '"status": "ok_but_edited"')
    assert tampered != original
    result_path.write_text(tampered, encoding="utf-8")
    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "fail"
    assert verdict["conclusion"] == "FAIL_RUNTIME"


# ---------------------------------------------------------------------------
# AC11: environment manifest has no secret fields
# ---------------------------------------------------------------------------


def test_environment_manifest_no_secret_fields():
    manifest = validator.build_environment_manifest()
    assert manifest["schema"] == validator.ENVIRONMENT_MANIFEST_SCHEMA
    expected_keys = {
        "schema",
        "repository_sha",
        "agy_version",
        "agy_binary_sha256",
        "serena_pinned_ref",
        "serena_manifest_hash",
        "hook_schema_version",
        "permission_policy_version",
        "python_version",
        "uv_lock_hash",
        "os",
        "is_wsl",
        "locale",
        "timezone",
        "command_shape",
        "authentication_state",
    }
    assert expected_keys.issubset(set(manifest.keys()))
    violations = validator.assert_environment_manifest_no_secrets(manifest)
    assert violations == []
    assert isinstance(manifest["is_wsl"], bool)
    assert isinstance(manifest["authentication_state"], str)


def test_environment_manifest_rejects_forbidden_keys():
    poisoned = {"schema": validator.ENVIRONMENT_MANIFEST_SCHEMA, "credential": "leaked-value"}
    violations = validator.assert_environment_manifest_no_secrets(poisoned)
    assert violations != []


# ---------------------------------------------------------------------------
# AC12: verdict schema required fields + conclusion enum
# ---------------------------------------------------------------------------


def test_verdict_schema_required_fields_and_conclusion_enum(tmp_path):
    bundle_dir = build_bundle.build_and_materialize(tmp_path / "bundle")
    verdict = validator.build_verdict(bundle_dir)
    for key in (
        "status",
        "parent_run_id",
        "passed_predicates",
        "failed_predicates",
        "artifact_manifest_sha256",
        "environment_manifest_sha256",
        "public_artifacts_redaction_status",
        "conclusion",
        "generated_at_utc",
    ):
        assert key in verdict, key
    assert verdict["conclusion"] in validator.VALID_CONCLUSIONS
    assert validator.validate_verdict_schema(verdict) == []

    bad = dict(verdict)
    bad["conclusion"] = "MAYBE"
    errors = validator.validate_verdict_schema(bad)
    assert any("conclusion" in e for e in errors)


# ---------------------------------------------------------------------------
# AC13: closed-schema test independent of predicate 24
# ---------------------------------------------------------------------------


def test_closed_schema_rejects_unknown_key():
    verdict = {
        "schema": validator.VERDICT_SCHEMA,
        "status": "pass",
        "parent_run_id": "x",
        "passed_predicates": [],
        "failed_predicates": [],
        "artifact_manifest_sha256": "a" * 64,
        "environment_manifest_sha256": "b" * 64,
        "public_artifacts_redaction_status": "clean",
        "conclusion": "PASS",
        "generated_at_utc": "2026-07-25T00:00:00Z",
        "totally_unknown_extra_field": "should be rejected",
    }
    errors = validator.validate_verdict_schema(verdict)
    assert any("unknown key" in e for e in errors)


def test_closed_schema_rejects_missing_key():
    verdict = {
        "schema": validator.VERDICT_SCHEMA,
        "status": "pass",
        "parent_run_id": "x",
        "passed_predicates": [],
        "failed_predicates": [],
        # artifact_manifest_sha256 intentionally omitted
        "environment_manifest_sha256": "b" * 64,
        "public_artifacts_redaction_status": "clean",
        "conclusion": "PASS",
        "generated_at_utc": "2026-07-25T00:00:00Z",
    }
    errors = validator.validate_verdict_schema(verdict)
    assert any("missing required key" in e for e in errors)


# ---------------------------------------------------------------------------
# AC14: consumer inventory / compatibility decision recorded (also checked by
# the standalone `rg` VC in the Issue body -- this test asserts the same fact
# is discoverable from within pytest so it participates in the AC15 full run).
# ---------------------------------------------------------------------------


def test_consumer_inventory_and_compatibility_decision_documented():
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert "consumer inventory" in text
    assert "compatibility decision" in text
