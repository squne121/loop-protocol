from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    ROOT
    / ".claude"
    / "skills"
    / "impl-review-loop"
    / "scripts"
    / "adjudicate_vc_result.py"
)


spec = importlib.util.spec_from_file_location("adjudicate_vc_result", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _payload_item(
    ac: str,
    *,
    command_hash: str = "sha256:" + "a" * 64,
    failure_keys: list[dict[str, str]] | None = None,
    exit_code: int = 1,
    category: str = "regression_gate",
    raw_command: str = "pytest tests/test_alpha.py",
) -> dict:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "failure_keys": failure_keys
        if failure_keys is not None
        else [{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
        "exit_code": exit_code,
        "category": category,
        "classification": "expected_fail",
        "decision": "blocked",
        "raw_command": raw_command,
        "raw_stdout": "pytest output",
        "raw_stderr": "error",
    }


def _snapshot_payload(items: list[dict[str, object]]) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "status": "go",
        "body_sha256": "sha256:" + "b" * 64,
        "checks": {
            "vc_preflight": {
                "classifications": items,
            }
        },
    }


def _current_payload(
    items: list[dict[str, object]],
    *,
    head_sha: str | None = None,
    reviewed_head_sha: str | None = None,
) -> dict:
    payload = {
        "schema": "baseline_vc_preflight/v1",
        "generated_at": "2026-07-11T10:00:00Z",
        "status": "pass",
        "errors": [],
        "fallback_detected": False,
        "human_review_required": False,
        "stop_condition_triggered": False,
        "source": {"body_sha256": "sha256:" + "b" * 64},
        "results": items,
    }
    if head_sha is not None:
        payload["head_sha"] = head_sha
    if reviewed_head_sha is not None:
        payload["reviewed_head_sha"] = reviewed_head_sha
    return payload


def _allowed_paths_file(tmp_path: Path, paths: list[str]) -> Path:
    path = tmp_path / "allowed_paths.json"
    path.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    return path


def test_no_diff_same_baseline_failure_is_pre_existing_nonblocking():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "pre_existing_fail"
    assert result["blocking"] is False
    assert result["rerun_required"] is False
    assert result["per_ac"][0]["status"] == "pre_existing_fail"


def test_ac3_failure_key_evidence_missing_becomes_indeterminate_blocking():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [_payload_item("AC1", command_hash="sha256:" + "a" * 64, failure_keys=[])],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["status"] == "indeterminate"


def test_diff_related_failure_is_regression_blocking():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "src/main.py::test_regression"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "regression_fail"
    assert result["blocking"] is True
    assert result["per_ac"][0]["status"] == "regression_fail"


def test_allowed_paths_glob_matcher_v2_relevance_is_regression_blocking():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[
                    {
                        "kind": "pytest_nodeid",
                        "key": ".claude/skills/impl-review-loop/tests/test_adjudicate_vc_result.py::test_case",
                    }
                ],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["docs/dev/agent-run-report.md"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "regression_fail"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "related_to_changed_scope"


def test_pytest_exit_5_is_not_regression_fail():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
            [_payload_item("AC1", command_hash="sha256:" + "a" * 64, exit_code=5, category="vc_no_tests_collected")],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] in {"indeterminate", "environment_blocked"}
    assert result["per_ac"][0]["status"] != "regression_fail"


def test_current_head_mismatch_is_indeterminate_blocking():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
        [_payload_item("AC1")],
        head_sha="old-head",
        reviewed_head_sha="old-head",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "new-head"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["source_integrity"]["evidence_fresh"] is False


def test_mixed_expected_baseline_results_with_certified_current_pass_are_nonblocking():
    items = [
        _payload_item(
            f"AC{index}",
            command_hash="sha256:" + f"{index:x}" * 64,
            failure_keys=[],
            exit_code=4 if index not in {6, 7} else 0,
        )
        for index in range(1, 9)
    ]
    for item in items:
        item["classification"] = "expected_pass" if item["ac"] in {"AC6", "AC7"} else "expected_fail"
    current_items = [
        {
            **item,
            "exit_code": 0,
            "failure_keys": [],
        }
        for item in items
    ]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload(items),
        current_vc_result=_current_payload(current_items, head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py"],
    )
    assert result["overall_status"] == "pass"
    assert result["blocking"] is False
    assert {entry["reason_code"] for entry in result["per_ac"]} == {
        "expected_fail_resolved_on_current_head",
        "expected_pass_still_passes",
    }


def test_current_pass_requires_complete_certified_envelope_and_allowed_paths():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    current = _current_payload([current_item], head_sha="head1", reviewed_head_sha="head1")
    current["errors"] = ["producer_error"]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/outside.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"


def test_mapping_and_invalid_paths_fail_closed_for_current_pass():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    current_item = _payload_item("AC1", failure_keys=[], exit_code=0)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={
            "changed_paths": [
                {
                    "path": ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py",
                    "previous_path": "../outside.py",
                }
            ],
            "head_sha": "head1",
        },
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "uncertified_current_pass"

    current_item["ac"] = "AC2"
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([current_item], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["errors"] == ["baseline_current_mapping_mismatch"]


def test_bool_exit_code_and_pass_with_failure_keys_fail_closed():
    baseline_item = _payload_item("AC1", failure_keys=[], exit_code=4)
    baseline_item["classification"] = "expected_fail"
    invalid = _payload_item("AC1", failure_keys=[], exit_code=False)
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([invalid], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["blocking"] is True
    assert result["errors"] == ["current[0]:invalid_exit_code"]

    invalid["exit_code"] = 0
    invalid["failure_keys"] = [{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}]
    result = mod.adjudicate_vc_result(
        contract_snapshot=_snapshot_payload([baseline_item]),
        current_vc_result=_current_payload([invalid], head_sha="head1", reviewed_head_sha="head1"),
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )
    assert result["per_ac"][0]["reason_code"] == "pass_with_failure_keys"


def test_new_unrelated_failure_with_diff_is_not_out_of_scope():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "docs/readme.md::test_readme"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "new_failure_without_scope_proof"


def test_out_of_scope_requires_diff_and_failure_key_evidence():
    test_new_unrelated_failure_with_diff_is_not_out_of_scope()


def test_failure_key_kind_process_exit_cannot_prove_out_of_scope():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "process_exit", "key": "process_exit:1"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "unsupported_failure_key_kind"


def test_invalid_allowed_path_pattern_is_indeterminate():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["docs/**/*.md"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert any(error.startswith("invalid_allowed_path_pattern:") for error in result["errors"])


def test_empty_current_classifications_without_pass_signal_is_indeterminate():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([], head_sha="head1", reviewed_head_sha="head1")

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["errors"] == ["empty_current_results_without_pass_signal"]


def test_full_stdout_stderr_only_in_private_artifact(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    allowed_path_file = _allowed_paths_file(tmp_path, [".claude/skills/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"
    artifact_file = tmp_path / "artifact.json"

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": [], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--artifact-out",
            str(artifact_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    compact_text = json.dumps(compact)
    assert "raw_stdout" not in compact_text
    assert "raw_stderr" not in compact_text
    assert "full_command_output" not in compact_text
    assert compact["artifact_ref"] == "vc-adjudication-private-bundle"
    assert compact["artifact_digest"].startswith("sha256:")

    private = json.loads(artifact_file.read_text(encoding="utf-8"))
    assert private["schema"] == "VC_ADJUDICATION_PRIVATE_BUNDLE_V1"
    assert private["current_vc_result"]["results"][0]["raw_stdout"] == "pytest output"


def test_compact_result_does_not_emit_local_artifact_path(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")
    allowed_path_file = _allowed_paths_file(tmp_path, [".claude/skills/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"
    artifact_file = tmp_path / "nested" / "artifact.json"
    artifact_file.parent.mkdir()

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": [], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--artifact-out",
            str(artifact_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    assert compact["artifact_ref"] == "vc-adjudication-private-bundle"
    assert str(artifact_file) not in result.stdout


def test_compact_stdout_remains_valid_json_when_truncated_budget_is_small(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "src/main.py::test_regression"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )
    allowed_path_file = _allowed_paths_file(tmp_path, ["src/**"])
    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(
        json.dumps({"changed_paths": ["src/main.py"], "head_sha": "head1"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--contract-snapshot-file",
            str(contract_file),
            "--current-vc-result-file",
            str(current_file),
            "--diff-summary-file",
            str(diff_file),
            "--allowed-paths-file",
            str(allowed_path_file),
            "--max-stdout-bytes",
            "200",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    compact = json.loads(result.stdout)
    assert compact["stdout_truncated"] is True
    assert compact["overall_status"] == "indeterminate"
    assert compact["errors"] == ["stdout_budget_exceeded"]


def test_artifact_digest_is_canonical_and_stable_across_key_order():
    payload_a = {"a": 1, "b": 2}
    payload_b = {"b": 2, "a": 1}
    assert mod._sha256(mod._canonical_json(payload_a)) == mod._sha256(mod._canonical_json(payload_b))


def test_bare_legacy_command_hash_is_either_normalized_or_rejected_with_migration_reason():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="a" * 64,
                failure_keys=[{"kind": "pytest_nodeid", "key": "tests/test_alpha.py::test_ok"}],
            )
        ],
        head_sha="head1",
        reviewed_head_sha="head1",
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": [], "head_sha": "head1"},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["per_ac"][0]["command_hash"] == "sha256:" + "a" * 64
    assert result["per_ac"][0]["command_hash_note"] == "normalized_legacy_bare_command_hash"


def test_allowed_paths_matcher_import_failure_returns_machine_json(monkeypatch):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")], head_sha="head1", reviewed_head_sha="head1")

    def _fail():
        return None, "allowed_paths_matcher_import_failed:ImportError"

    monkeypatch.setattr(mod, "_get_allowed_paths_matcher", _fail)

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"], "head_sha": "head1"},
        allowed_paths=["src/**"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert any(error.startswith("allowed_paths_matcher_import_failed:") for error in result["errors"])
