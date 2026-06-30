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
    failure_keys: list[str] | None = None,
    exit_code: int = 1,
    category: str = "regression_gate",
    raw_command: str = "pytest tests/test_alpha.py",
) -> dict:
    return {
        "ac": ac,
        "command_hash": command_hash,
        "failure_keys": failure_keys if failure_keys is not None else ["tests/test_alpha.py::test_ok"],
        "exit_code": exit_code,
        "category": category,
        "decision": "blocked",
        "raw_command": raw_command,
        "raw_stdout": "pytest output",
        "raw_stderr": "error",
    }


def _snapshot_payload(items: list[dict[str, object]]) -> dict:
    return {
        "schema": "CONTRACT_REVIEW_RESULT_V1",
        "checks": {
            "vc_preflight": {
                "classifications": items,
            }
        },
    }


def _current_payload(items: list[dict[str, object]]) -> dict:
    return {
        "schema": "baseline_vc_preflight/v1",
        "results": items,
    }


def test_no_diff_same_baseline_failure_is_pre_existing_nonblocking():
    baseline = _snapshot_payload([_payload_item("AC1", failure_keys=["tests/test_alpha.py::test_ok"])])
    current = _current_payload([_payload_item("AC1", failure_keys=["tests/test_alpha.py::test_ok"])])

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": []},
        allowed_paths=[".claude/skills"],
    )

    assert result["overall_status"] == "pre_existing_fail"
    assert result["blocking"] is False
    assert result["rerun_required"] is False
    assert result["per_ac"][0]["status"] == "pre_existing_fail"


def test_ac3_failure_key_evidence_missing_becomes_indeterminate_blocking():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload([_payload_item("AC1", command_hash="sha256:" + "b" * 64, failure_keys=[])])

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"]},
        allowed_paths=[".claude/skills"],
    )

    assert result["overall_status"] == "indeterminate"
    assert result["blocking"] is True
    assert result["per_ac"][0]["status"] == "indeterminate"


def test_out_of_scope_requires_diff_and_failure_key_evidence():
    baseline = _snapshot_payload([_payload_item("AC1", command_hash="sha256:" + "a" * 64)])
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "b" * 64,
                failure_keys=["docs/readme.md::test_readme"],
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"]},
        allowed_paths=[".claude/skills"],
    )

    assert result["overall_status"] == "out_of_scope_fail"
    assert result["blocking"] is False
    assert result["per_ac"][0]["status"] == "out_of_scope_fail"


def test_diff_related_failure_is_regression_blocking():
    baseline = _snapshot_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "a" * 64,
                failure_keys=["tests/test_alpha.py::test_ok"],
            )
        ]
    )
    current = _current_payload(
        [
            _payload_item(
                "AC1",
                command_hash="sha256:" + "b" * 64,
                failure_keys=["src/main.py::test_regression"],
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"]},
        allowed_paths=["src/main.py"],
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
                command_hash="sha256:" + "b" * 64,
                failure_keys=[
                    ".claude/skills/impl-review-loop/tests/test_adjudicate_vc_result.py::test_case"
                ],
            )
        ]
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["docs/dev/agent-run-report.md"]},
        allowed_paths=[".claude/skills/**"],
    )

    assert result["overall_status"] == "regression_fail"
    assert result["blocking"] is True
    assert result["per_ac"][0]["reason_code"] == "related_to_changed_scope"


def test_pytest_exit_5_is_not_regression_fail():
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload(
        [_payload_item("AC1", command_hash="sha256:" + "b" * 64, exit_code=5, category="vc_no_tests_collected")],
    )

    result = mod.adjudicate_vc_result(
        contract_snapshot=baseline,
        current_vc_result=current,
        diff_summary={"changed_paths": ["src/main.py"]},
        allowed_paths=[".claude/skills"],
    )

    assert result["overall_status"] in {"indeterminate", "environment_blocked"}
    assert result["per_ac"][0]["status"] != "regression_fail"


def test_full_stdout_stderr_only_in_private_artifact(tmp_path: Path):
    baseline = _snapshot_payload([_payload_item("AC1")])
    current = _current_payload([_payload_item("AC1")])

    allowed_path_file = tmp_path / "allowed_paths.json"
    allowed_path_file.write_text(json.dumps([".claude/skills"], ensure_ascii=False), encoding="utf-8")

    contract_file = tmp_path / "contract.json"
    current_file = tmp_path / "current.json"
    diff_file = tmp_path / "diff.json"
    artifact_file = tmp_path / "artifact.json"

    contract_file.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    diff_file.write_text(json.dumps({"changed_paths": []}, ensure_ascii=False), encoding="utf-8")

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

    assert result.returncode in (0, 1)
    compact = json.loads(result.stdout)

    compact_text = json.dumps(compact)
    assert "raw_stdout" not in compact_text
    assert "raw_stderr" not in compact_text
    assert "full_command_output" not in compact_text
    assert "artifact_ref" in compact
    assert "artifact_digest" in compact

    private = json.loads(artifact_file.read_text(encoding="utf-8"))
    assert private["schema"] == "VC_ADJUDICATION_PRIVATE_BUNDLE_V1"
    assert private["contract_snapshot"]["checks"]
    assert private["current_vc_result"]["results"]
    assert private["current_vc_result"]["results"][0]["raw_stdout"] == "pytest output"
