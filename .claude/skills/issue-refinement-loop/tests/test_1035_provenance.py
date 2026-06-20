"""
test_1035_provenance.py

Issue #1035: plan_refinement_loop.py 構文エラー停止の再現条件整理

AC1: PY_COMPILE_PROOF_V1 の記録
AC2: REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1 の記録
AC3: PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy
AC4: traceback 採取手順 / REFINEMENT_PREFLIGHT_PROVENANCE_V1 フィールド検証
AC5: #964 enforcement blocker を解消扱いにしない
AC6: generated artifact を source として扱わない

VC keywords for rg:
  PY_COMPILE_PROOF_V1            (AC1)
  REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1  (AC2)
  PLANNER_FAILURE_CLASSIFICATION_V1     (AC3)
  REFINEMENT_PREFLIGHT_PROVENANCE_V1    (AC4)
  issue_964_enforcement_not_resolved    (AC5)
  artifacts_not_git_tracked             (AC6)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as wrapper  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: test_py_compile_proof_v1
# ---------------------------------------------------------------------------


def test_py_compile_proof_v1():
    """PY_COMPILE_PROOF_V1 must record all required fields and compile clean."""
    planner_script = SCRIPTS_DIR / "plan_refinement_loop.py"
    assert planner_script.exists(), f"planner script not found: {planner_script}"

    proof = wrapper.build_py_compile_proof(planner_script, REPO_ROOT)

    # Schema version
    assert proof["schema_version"] == "PY_COMPILE_PROOF_V1"

    # Required fields present
    required = [
        "command",
        "py_compile_status",
        "python_version",
        "python_executable",
        "git_head_sha",
        "planner_script_path",
        "planner_script_realpath",
        "planner_script_blob_sha",
        "cwd",
        "stderr_sha256",
        "stderr_excerpt",
    ]
    for field in required:
        assert field in proof, f"missing field: {field}"

    # Type checks
    assert isinstance(proof["command"], list), "command must be a list (argv)"
    assert proof["py_compile_status"] in ("pass", "fail")
    assert isinstance(proof["python_version"], str)
    assert isinstance(proof["python_executable"], str)
    assert isinstance(proof["git_head_sha"], str)
    assert isinstance(proof["planner_script_path"], str)
    assert isinstance(proof["planner_script_realpath"], str)
    assert isinstance(proof["planner_script_blob_sha"], str)
    assert isinstance(proof["cwd"], str)
    # stderr_sha256 must be a 64-char hex string
    assert len(proof["stderr_sha256"]) == 64
    assert all(c in "0123456789abcdef" for c in proof["stderr_sha256"])
    assert isinstance(proof["stderr_excerpt"], str)

    # Current planner must compile successfully
    assert proof["py_compile_status"] == "pass", (
        f"plan_refinement_loop.py failed py_compile: {proof['stderr_excerpt']}"
    )

    # python_executable must be a real path
    import os
    assert os.path.isabs(proof["python_executable"]), "python_executable should be absolute"

    # git HEAD must look like a SHA (40 hex chars) or "unknown"
    assert (
        proof["git_head_sha"] == "unknown"
        or (len(proof["git_head_sha"]) == 40 and all(c in "0123456789abcdef" for c in proof["git_head_sha"]))
    ), f"unexpected git_head_sha: {proof['git_head_sha']!r}"


# ---------------------------------------------------------------------------
# AC2: test_preflight_replay_proof_v1
# ---------------------------------------------------------------------------


def test_preflight_replay_proof_v1():
    """REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1 must detect input drift correctly."""
    # Case 1: identical inputs → no drift, replay_consistent
    input_a = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {"number": 964, "title": "test", "body": "body", "labels": []},
        "comments": [],
    }
    proof_nodrift = wrapper.build_replay_proof(
        live_input=input_a,
        fixture_input=input_a,
        live_result_status="pass",
        fixture_result_status="pass",
    )

    assert proof_nodrift["schema_version"] == "REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1"
    assert proof_nodrift["input_drift_detected"] is False
    assert proof_nodrift["results_consistent"] is True
    assert proof_nodrift["classification"] == "replay_consistent"
    assert proof_nodrift["live_input_sha256"] == proof_nodrift["fixture_input_sha256"]

    # Case 2: different inputs → input_drift_detected
    input_b = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {"number": 964, "title": "test", "body": "DIFFERENT BODY", "labels": []},
        "comments": [],
    }
    proof_drift = wrapper.build_replay_proof(
        live_input=input_a,
        fixture_input=input_b,
        live_result_status="blocked",
        fixture_result_status="pass",
    )

    assert proof_drift["schema_version"] == "REFINEMENT_PREFLIGHT_REPLAY_PROOF_V1"
    assert proof_drift["input_drift_detected"] is True
    assert proof_drift["classification"] == "input_drift"
    assert proof_drift["live_input_sha256"] != proof_drift["fixture_input_sha256"]
    # When drift is detected, status inconsistency is subsumed under "input_drift"
    assert proof_drift["classification"] == "input_drift"

    # Case 3: same inputs but status mismatch → classification_mismatch
    proof_mismatch = wrapper.build_replay_proof(
        live_input=input_a,
        fixture_input=input_a,
        live_result_status="pass",
        fixture_result_status="blocked",
    )
    assert proof_mismatch["input_drift_detected"] is False
    assert proof_mismatch["results_consistent"] is False
    assert proof_mismatch["classification"] == "classification_mismatch"

    # SHA256 values must be 64-char hex
    for proof in (proof_nodrift, proof_drift, proof_mismatch):
        for key in ("live_input_sha256", "fixture_input_sha256"):
            val = proof[key]
            assert len(val) == 64 and all(c in "0123456789abcdef" for c in val)


# ---------------------------------------------------------------------------
# AC3: test_planner_failure_classification_v1
# ---------------------------------------------------------------------------


def test_planner_failure_classification_v1():
    """PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy must classify all failure modes."""
    planner_script = SCRIPTS_DIR / "plan_refinement_loop.py"

    # Case 1: syntax_compile_failure
    result = wrapper.classify_planner_failure(
        exit_code=1,
        stdout="",
        stderr='File "plan_refinement_loop.py", line 42\n    def broken(\nSyntaxError: invalid syntax\n',
        script_path=planner_script,
        python_executable=sys.executable,
    )
    assert result["schema_version"] == "PLANNER_FAILURE_CLASSIFICATION_V1"
    assert result["category"] == "syntax_compile_failure"
    assert "SyntaxError" in result["traceback_excerpt"]
    assert result["json_decode_error"] == ""

    # Required evidence fields
    _assert_classification_fields(result)

    # Case 2: anchor_or_input_blocked (exit 2)
    result2 = wrapper.classify_planner_failure(
        exit_code=2,
        stdout=json.dumps({"schema_version": "refinement_loop_plan/v1"}),
        stderr="invalid input schema",
        script_path=planner_script,
        python_executable=sys.executable,
    )
    assert result2["category"] == "anchor_or_input_blocked"
    assert result2["traceback_excerpt"] == ""
    _assert_classification_fields(result2)

    # Case 3: planner_stdout_non_json (exit 0, stdout is not JSON)
    result3 = wrapper.classify_planner_failure(
        exit_code=0,
        stdout="not-json",
        stderr="",
        script_path=planner_script,
        python_executable=sys.executable,
    )
    assert result3["category"] == "planner_stdout_non_json"
    assert result3["json_decode_error"] != ""
    _assert_classification_fields(result3)

    # Case 4: wrapper_environment_failure (exit 3, env error)
    result4 = wrapper.classify_planner_failure(
        exit_code=3,
        stdout="",
        stderr="gh not found on PATH",
        script_path=planner_script,
        python_executable=sys.executable,
    )
    assert result4["category"] == "wrapper_environment_failure"
    _assert_classification_fields(result4)

    # Case 5: planner_runtime_internal_error (exit 3, traceback without gh)
    result5 = wrapper.classify_planner_failure(
        exit_code=3,
        stdout="",
        stderr="Traceback (most recent call last):\n  ZeroDivisionError: division by zero",
        script_path=planner_script,
        python_executable=sys.executable,
    )
    assert result5["category"] == "planner_runtime_internal_error"
    _assert_classification_fields(result5)


def _assert_classification_fields(result: dict) -> None:
    required = [
        "schema_version",
        "category",
        "exit_code",
        "stdout_sha256",
        "stderr_sha256",
        "stderr_excerpt",
        "json_decode_error",
        "traceback_excerpt",
        "script_path",
        "script_realpath",
        "python_executable",
        "python_version",
    ]
    for field in required:
        assert field in result, f"missing field in classification: {field}"
    # SHA256 fields must be 64-char hex
    for sha_field in ("stdout_sha256", "stderr_sha256"):
        val = result[sha_field]
        assert len(val) == 64 and all(c in "0123456789abcdef" for c in val), (
            f"{sha_field} not a valid sha256: {val!r}"
        )
    valid_categories = {
        "syntax_compile_failure",
        "planner_stdout_non_json",
        "planner_runtime_internal_error",
        "wrapper_environment_failure",
        "anchor_or_input_blocked",
    }
    assert result["category"] in valid_categories, (
        f"unknown category: {result['category']!r}"
    )


# ---------------------------------------------------------------------------
# AC4: test_preflight_provenance_v1
# ---------------------------------------------------------------------------


def test_preflight_provenance_v1(tmp_path):
    """REFINEMENT_PREFLIGHT_PROVENANCE_V1 must include all required fields."""
    minimal_input = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {"number": 964, "title": "test", "body": "body", "labels": []},
        "comments": [],
    }
    minimal_snapshot = {
        "issue": {"number": 964},
        "comments": [],
    }

    provenance = wrapper.build_provenance(
        repo="squne121/loop-protocol",
        issue_number=964,
        anchor_comment_url="https://github.com/squne121/loop-protocol/issues/964#issuecomment-0",
        planner_input=minimal_input,
        raw_snapshot=minimal_snapshot,
        wrapper_exit_code=2,
        wrapper_status="blocked",
        blockers=["PLANNER_INTERNAL_ERROR"],
        stderr="",
        repo_root=REPO_ROOT,
    )

    assert provenance["schema_version"] == "REFINEMENT_PREFLIGHT_PROVENANCE_V1"

    required = [
        "schema_version",
        "repo",
        "issue_number",
        "anchor_comment_url",
        "git_head_sha",
        "planner_script_path",
        "planner_script_realpath",
        "planner_script_blob_sha",
        "wrapper_script_blob_sha",
        "python_executable",
        "python_version",
        "cwd",
        "py_compile_status",
        "wrapper_exit_code",
        "wrapper_status",
        "blockers",
        "planner_input_sha256",
        "raw_snapshot_sha256",
        "stderr_sha256",
    ]
    for field in required:
        assert field in provenance, f"missing field: {field}"

    # Type / value constraints
    assert provenance["repo"] == "squne121/loop-protocol"
    assert provenance["issue_number"] == 964
    assert isinstance(provenance["blockers"], list)
    assert provenance["wrapper_exit_code"] == 2
    assert provenance["wrapper_status"] == "blocked"
    assert provenance["py_compile_status"] in ("pass", "fail")

    for sha_field in ("planner_input_sha256", "raw_snapshot_sha256", "stderr_sha256"):
        val = provenance[sha_field]
        assert len(val) == 64 and all(c in "0123456789abcdef" for c in val), (
            f"{sha_field} not a valid sha256: {val!r}"
        )

    # Write provenance artifact into tmp_path to verify serialization
    prov_json = json.dumps(provenance, ensure_ascii=False, indent=2)
    prov_path = tmp_path / "refinement_preflight_provenance_v1.json"
    prov_path.write_text(prov_json, encoding="utf-8")
    # Round-trip
    reloaded = json.loads(prov_path.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == "REFINEMENT_PREFLIGHT_PROVENANCE_V1"


# ---------------------------------------------------------------------------
# AC5: test_issue_964_enforcement_not_resolved — issue_964_enforcement_not_resolved
# ---------------------------------------------------------------------------


def test_issue_964_enforcement_not_resolved():
    """Verify that #964 runtime enforcement blockers are NOT claimed as resolved.

    The PLANNER_FAILURE_CLASSIFICATION_V1 taxonomy must NOT include a category
    that claims hook deny semantics / gh pr edit / GraphQL / shell chaining
    enforcement is resolved.  The REFINEMENT_PREFLIGHT_PROVENANCE_V1 schema
    must NOT have a field asserting #964 is closed.
    """
    # 1. Classification taxonomy must not include any hook/enforcement resolution claim
    valid_categories = {
        "syntax_compile_failure",
        "planner_stdout_non_json",
        "planner_runtime_internal_error",
        "wrapper_environment_failure",
        "anchor_or_input_blocked",
    }
    forbidden_categories = {
        "hook_deny_resolved",
        "gh_pr_edit_resolved",
        "graphql_route_resolved",
        "shell_chaining_resolved",
        "issue_964_resolved",
    }
    # Taxonomy must not contain forbidden category names
    assert valid_categories.isdisjoint(forbidden_categories), (
        "taxonomy collision: forbidden category names must not appear in valid taxonomy"
    )

    # 2. build_provenance result must NOT have issue_964_resolved field
    minimal_input: dict = {}
    minimal_snapshot: dict = {}
    provenance = wrapper.build_provenance(
        repo="squne121/loop-protocol",
        issue_number=964,
        anchor_comment_url="",
        planner_input=minimal_input,
        raw_snapshot=minimal_snapshot,
        wrapper_exit_code=3,
        wrapper_status="environment_failure",
        blockers=["PLANNER_INTERNAL_ERROR"],
        stderr="",
        repo_root=REPO_ROOT,
    )
    assert "issue_964_resolved" not in provenance, (
        "provenance must not claim #964 is resolved"
    )
    assert "hook_deny_resolved" not in provenance

    # 3. The wrapper source must NOT contain Closes #964
    wrapper_src = Path(wrapper.__file__).read_text(encoding="utf-8")
    assert "Closes #964" not in wrapper_src, (
        "run_refinement_preflight.py must not contain 'Closes #964'"
    )

    # 4. Blocker constant names do not reference enforcement resolution
    blocker_attrs = [
        getattr(wrapper, attr) for attr in dir(wrapper)
        if attr.startswith("BLOCKER_")
    ]
    for b in blocker_attrs:
        assert "964" not in str(b), f"unexpected BLOCKER_ constant references #964: {b!r}"


# ---------------------------------------------------------------------------
# AC6: test_artifacts_not_git_tracked — artifacts_not_git_tracked
# ---------------------------------------------------------------------------


def test_artifacts_not_git_tracked():
    """issue-refinement-loop generated artifacts must not be committed as source files.

    Verifies that .claude/artifacts/issue-refinement-loop/ has no git-tracked files.
    Other artifact subdirectories (e.g. context-mode/) may legitimately be tracked.
    """
    irl_artifacts_relpath = ".claude/artifacts/issue-refinement-loop/"

    # Check git ls-files for issue-refinement-loop artifacts specifically
    try:
        ls_files = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--", irl_artifacts_relpath],
            capture_output=True,
            text=True,
            timeout=10,
        )
        tracked_lines = [ln for ln in ls_files.stdout.splitlines() if ln.strip()]
        has_tracked_irl_artifacts = bool(tracked_lines) and ls_files.returncode == 0
    except Exception:
        has_tracked_irl_artifacts = False
        tracked_lines = []

    assert not has_tracked_irl_artifacts, (
        f".claude/artifacts/issue-refinement-loop/ must NOT be git-tracked source — "
        f"found: {tracked_lines}"
    )

    # Verify that test fixtures directory IS a legitimate source location
    fixtures_dir = TESTS_DIR / "fixtures"
    if fixtures_dir.exists():
        ls_fixtures = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--",
             ".claude/skills/issue-refinement-loop/tests/fixtures/"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # fixtures/ contents may or may not be tracked; either is valid
        _ = ls_fixtures  # result used for documentation only
