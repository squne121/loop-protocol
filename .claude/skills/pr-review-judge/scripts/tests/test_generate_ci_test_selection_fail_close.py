"""Fail-close + plan-parity tests for generate_ci_test_selection_artifact.py (Issue #1064).

The generator must:
- derive its pytest scope argv from the python-test-plan SSOT (AC2/AC7),
- include schemas/tests/ exactly once (AC7),
- fail-close (non-zero exit) and record collection_status when collection fails (AC6),
- fail-close and record diff_status when change detection fails (Issue #1064 review:
  shallow-checkout / missing base-head SHA must NOT report an empty changed-test set).
"""

import argparse
import importlib.util
import json
import os
import subprocess
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[5]
_GEN_PATH = (
    _REPO_ROOT
    / ".claude"
    / "skills"
    / "pr-review-judge"
    / "scripts"
    / "generate_ci_test_selection_artifact.py"
)


def _load_gen():
    spec = importlib.util.spec_from_file_location("gen_under_test", _GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gen = _load_gen()


def _completed(returncode, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_collect(nodeids, returncode=0, stderr=""):
    """Build a subprocess.run replacement that writes COLLECT_NODEIDS_OUT like the plugin."""

    def _run(cmd, *a, **k):
        out = (k.get("env") or os.environ).get("COLLECT_NODEIDS_OUT")
        if out:
            Path(out).write_text(json.dumps({"nodeids": sorted(nodeids), "count": len(nodeids)}))
        return _completed(returncode, stderr=stderr)

    return _run


# --- AC2 / AC7: plan-derived scope argv ---


def test_resolve_pytest_args_derives_from_plan():
    args = argparse.Namespace(pytest_args=None, plan=None)
    argv = gen.resolve_pytest_args(args)
    assert argv.count("schemas/tests/") == 1
    assert any(a.startswith("--ignore=") for a in argv)


def test_resolve_pytest_args_override_wins():
    args = argparse.Namespace(pytest_args=["only/this/"], plan=None)
    assert gen.resolve_pytest_args(args) == ["only/this/"]


# --- AC6: fail-close on collection failure ---


def test_collection_status_non_zero_is_not_ok(monkeypatch):
    monkeypatch.setattr(gen.subprocess, "run", _fake_collect([], returncode=2, stderr="boom"))
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["returncode"] == 2
    assert status["nodeid_count"] == 0


def test_collection_status_timeout_is_not_ok(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(gen.subprocess, "run", _raise)
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["timed_out"] is True


def test_collection_status_zero_nodeids_is_not_ok(monkeypatch):
    monkeypatch.setattr(gen.subprocess, "run", _fake_collect([], returncode=0))
    files, nodeids, status = gen.get_pytest_collected_tests(["x/"])
    assert status["ok"] is False
    assert status["nodeid_count"] == 0


def test_collection_status_ok_when_nodeids_present(monkeypatch):
    monkeypatch.setattr(
        gen.subprocess, "run",
        _fake_collect(["pkg/test_a.py::test_one", "pkg/test_a.py::test_two"], returncode=0),
    )
    files, nodeids, status = gen.get_pytest_collected_tests(["pkg/"])
    assert status["ok"] is True
    assert status["nodeid_count"] == 2
    assert "pkg/test_a.py::test_one" in nodeids
    assert files == ["pkg/test_a.py"]


# --- Issue #1064 review: fail-close on change-detection failure ---


def test_diff_status_missing_sha_is_not_ok():
    changed, excluded, diff_status = gen.get_changed_test_files("", "")
    assert diff_status["ok"] is False
    assert "required" in diff_status["error"]
    assert changed == []


def test_diff_status_non_zero_is_not_ok(monkeypatch):
    monkeypatch.setattr(
        gen.subprocess, "run",
        lambda *a, **k: _completed(128, stderr="fatal: bad object main"),
    )
    changed, excluded, diff_status = gen.get_changed_test_files("base", "head")
    assert diff_status["ok"] is False
    assert diff_status["returncode"] == 128
    assert "bad object" in diff_status["stderr_tail"]


def test_diff_status_ok_lists_changed_tests(monkeypatch):
    out = "scripts/ci/tests/test_python_test_plan.py\nsrc/main.ts\nREADME.md\n"
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **k: _completed(0, stdout=out))
    changed, excluded, diff_status = gen.get_changed_test_files("base", "head")
    assert diff_status["ok"] is True
    assert "scripts/ci/tests/test_python_test_plan.py" in changed


def test_generate_artifact_fail_closes_on_collection(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "get_pytest_collected_tests", lambda argv: ([], [], {
        "returncode": 1, "timed_out": False, "error": None,
        "nodeid_count": 0, "stderr_tail": "collect error", "ok": False,
    }))
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: ([], [], {"ok": True}))
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=["x/"], plan=None, pr_head_sha="h",
        base_sha="b", head_sha="h", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    assert gen.generate_artifact(args) == 2
    data = json.loads(out.read_text())
    assert data["collection_status"]["ok"] is False


def test_generate_artifact_fail_closes_on_diff(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "get_pytest_collected_tests", lambda argv: (
        ["pkg/test_a.py"], ["pkg/test_a.py::test_one"], {
            "returncode": 0, "timed_out": False, "error": None,
            "nodeid_count": 1, "stderr_tail": "", "ok": True,
        }))
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: ([], [], {
        "base_sha": b, "head_sha": h, "returncode": 128, "timed_out": False,
        "error": "fatal: bad object main", "stderr_tail": "fatal", "ok": False,
    }))
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=None, plan=None, pr_head_sha="h",
        base_sha="", head_sha="", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    # collection ok but diff failed -> still fail-closed (exit 2), the false-green fix.
    assert gen.generate_artifact(args) == 2
    data = json.loads(out.read_text())
    assert data["diff_status"]["ok"] is False
    assert data["collection_status"]["ok"] is True


def test_generate_artifact_succeeds_when_both_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "get_pytest_collected_tests", lambda argv: (
        ["pkg/test_a.py"], ["pkg/test_a.py::test_one"], {
            "returncode": 0, "timed_out": False, "error": None,
            "nodeid_count": 1, "stderr_tail": "", "ok": True,
        }))
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: (
        ["pkg/test_a.py"], [], {"base_sha": b, "head_sha": h, "ok": True}))
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=None, plan=None, pr_head_sha="h",
        base_sha="b", head_sha="h", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    rc = gen.generate_artifact(args)
    assert rc == 0  # changed test pkg/test_a.py is covered (collected)
    data = json.loads(out.read_text())
    assert data["collection_status"]["ok"] is True
    assert data["diff_status"]["ok"] is True
    assert data["pytest_argv"].count("schemas/tests/") == 1


def test_generate_artifact_uncovered_changed_test_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "get_pytest_collected_tests", lambda argv: (
        ["pkg/test_a.py"], ["pkg/test_a.py::test_one"], {
            "returncode": 0, "timed_out": False, "error": None,
            "nodeid_count": 1, "stderr_tail": "", "ok": True,
        }))
    # a changed test file that is NOT collected -> uncovered -> exit 1
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: (
        ["pkg/test_uncovered.py"], [], {"base_sha": b, "head_sha": h, "ok": True}))
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=["pkg/"], plan=None, pr_head_sha="h",
        base_sha="b", head_sha="h", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    assert gen.generate_artifact(args) == 1
    data = json.loads(out.read_text())
    assert data["uncovered_changed_test_files"] == ["pkg/test_uncovered.py"]


def test_generate_artifact_plan_only_dedicated_lane_marks_codex_test_covered(monkeypatch, tmp_path):
    monkeypatch.setattr(gen, "get_pytest_collected_tests", lambda argv: (
        ["pkg/test_a.py"], ["pkg/test_a.py::test_one"], {
            "returncode": 0, "timed_out": False, "error": None,
            "nodeid_count": 1, "stderr_tail": "", "ok": True,
        }))
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: (
        ["tests/codex/test_local_main_branch_guard.py"], [],
        {"base_sha": b, "head_sha": h, "ok": True}))
    monkeypatch.setattr(gen, "resolve_pytest_args", lambda args: ["pkg/"])

    fake_plan = {
        "targets": ["pkg/"],
        "secondary_coverage": {
            "plan_targets_provider_job": "python-test",
            "dedicated_lanes": [
                {
                    "provider_job": "python-test",
                    "lane_id": "codex-execpolicy",
                    "paths": ["tests/codex/test_local_main_branch_guard.py"],
                }
            ],
        },
    }
    monkeypatch.setattr(
        gen,
        "_load_plan_module",
        lambda: types.SimpleNamespace(
            load_plan=lambda path: fake_plan,
            scope_argv=lambda plan: ["pkg/"],
        ),
    )

    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=None, plan=".github/ci/python-test-plan.json", pr_head_sha="h",
        base_sha="b", head_sha="h", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    assert gen.generate_artifact(args) == 0
    data = json.loads(out.read_text())
    assert data["cross_job_covered_test_files"] == ["tests/codex/test_local_main_branch_guard.py"]
    assert data["uncovered_changed_test_files"] == []
    assert data["secondary_coverage_provider_job"] == "python-test"


def test_generate_artifact_pr_body_validator_is_covered(monkeypatch, tmp_path):
    """AC5/AC7/AC8: test_pr_body_validator.py in changed_files must be covered by the plan.

    The stub captures argv supplied by the generator and asserts it matches
    artifact["pytest_argv"], binding collection proof to the actual dispatch path.
    """
    changed_file = "scripts/tests/test_pr_body_validator.py"
    seen: dict = {}

    def fake_collect(argv):
        seen["argv"] = list(argv)
        assert seen["argv"].count(changed_file) == 1, (
            f"generator must pass {changed_file!r} exactly once to collector; got {argv!r}"
        )
        nodeids = [
            f"{changed_file}::test_wrapper_emits_error_code_for_parse_failure",
            f"{changed_file}::test_wrapper_schema_change_flag_mismatch",
            f"{changed_file}::test_wrapper_success_path_outputs_json_only",
            f"{changed_file}::test_wrapper_emits_error_code_for_missing_safety_claim_matrix",
            f"{changed_file}::test_wrapper_schema_change_flag_requires_inventory_when_body_decision_invalid",
        ]
        return (
            [changed_file],
            nodeids,
            {
                "returncode": 0,
                "timed_out": False,
                "error": None,
                "nodeid_count": len(nodeids),
                "stderr_tail": "",
                "ok": True,
            },
        )

    monkeypatch.setattr(gen, "get_pytest_collected_tests", fake_collect)
    monkeypatch.setattr(gen, "get_changed_test_files", lambda b, h: (
        [changed_file], [], {"base_sha": b, "head_sha": h, "ok": True}
    ))
    out = tmp_path / "artifact.json"
    args = argparse.Namespace(
        output=str(out), pytest_args=None, plan=None, pr_head_sha="h",
        base_sha="b", head_sha="h", checked_out_sha=None, merge_sha="m",
        workflow="ci", job="python-test", ci_run_url=None,
    )
    rc = gen.generate_artifact(args)
    assert rc == 0
    artifact = json.loads(out.read_text())
    assert artifact["collection_status"]["ok"] is True
    assert artifact["diff_status"]["ok"] is True
    assert artifact["collection_status"]["nodeid_count"] == 5
    assert changed_file in artifact["changed_test_files"]
    assert artifact["collected_test_files"] == [changed_file]
    assert artifact["uncovered_changed_test_files"] == []
    assert seen["argv"] == artifact["pytest_argv"]
    assert seen["argv"].count(changed_file) == 1
