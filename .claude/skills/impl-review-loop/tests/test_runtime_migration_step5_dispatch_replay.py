"""Issue #2810 AC12 — consumer replay test for the Step 5 dispatch procedure.

Proves that Step 5's dispatch goes through the PRODUCTION path
(`classify_runtime_migration.py materialize` -> `classify_runtime_migration.py`
classify, both real CLI subprocesses) rather than a hand-assembled classifier
payload or a markdown string match. Each representative fixture from
AC2-AC7 is injected as *evidence* (a launcher failure receipt file, a live
Issue body file, a controlled env, a tmp CLAUDE_GPT_HOME, optional install
log / preflight / worker_result files) and the parsed `class` / `route` /
`human_action_report` of the classifier CLI output is asserted.

(AC7's pre/post identity binding is a separate pure function,
`bind_pre_post_identity`, covered in test_runtime_migration_classification.py;
it has no CLI entry point and is not part of the materialize -> classify
path.)
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
CLASSIFY_SCRIPT = SCRIPTS_DIR / "classify_runtime_migration.py"
EXACT = "bash scripts/claude-gpt/repair_proxy.sh"

ALLOWING_ISSUE_BODY = (
    "## Outcome\n\n"
    f"- migration 実行者: agent-executable bounded repair として `{EXACT}` を agent が実行してよい\n"
)
REPORT_FIELDS = (
    "reason",
    "required_human_action",
    "target_environment",
    "verification_command",
    "resume_condition",
)


def _failure_receipt(**overrides):
    receipt = {
        "schema": "CLAUDE_GPT_LAUNCH_RESULT_V1",
        "status": "failed",
        "reason": "model_alias_not_resolved",
        "cause": "proxy_model_catalog_incompatible",
        "required_models": ["gpt-6-sol", "gpt-6-luna"],
        "missing_models": ["gpt-6-sol", "gpt-6-luna"],
        "repair_command": EXACT,
    }
    receipt.update(overrides)
    return receipt


def _replay(
    tmp_path,
    *,
    home=None,
    receipt=None,
    issue_body=ALLOWING_ISSUE_BODY,
    extra_env=None,
    preflight=None,
    install_log=None,
    worker_result=None,
    operator_host_differs=False,
):
    """Run the production path: materialize CLI -> classifier CLI. Returns
    (materialized_payload, classifier_result)."""
    home = home or (tmp_path / "gpt-home")
    evidence = tmp_path / "failure-receipt.json"
    evidence.write_text(json.dumps(receipt or _failure_receipt()), encoding="utf-8")
    argv = [sys.executable, str(CLASSIFY_SCRIPT), "materialize", "--failure-evidence-file", str(evidence)]
    if issue_body is not None:
        body = tmp_path / "issue-body.md"
        body.write_text(issue_body, encoding="utf-8")
        argv += ["--issue-body-file", str(body)]
    if preflight is not None:
        path = tmp_path / "preflight.json"
        path.write_text(json.dumps(preflight), encoding="utf-8")
        argv += ["--preflight-file", str(path)]
    if install_log is not None:
        path = tmp_path / "install.log"
        path.write_text(install_log, encoding="utf-8")
        argv += ["--install-log-file", str(path)]
    if worker_result is not None:
        path = tmp_path / "worker-result.json"
        path.write_text(json.dumps(worker_result), encoding="utf-8")
        argv += ["--worker-result-file", str(path)]
    if operator_host_differs:
        argv.append("--operator-host-differs")

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_GPT_HOME": str(home)}
    env.update(extra_env or {})
    materialized = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert materialized.returncode == 0, f"materialize exited {materialized.returncode}: {materialized.stderr!r}"
    classified = subprocess.run(
        [sys.executable, str(CLASSIFY_SCRIPT)],
        input=materialized.stdout,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert classified.returncode == 0, f"classifier exited {classified.returncode}: {classified.stderr!r}"
    return json.loads(materialized.stdout), json.loads(classified.stdout)


def _assert_full_report(result):
    report = result["human_action_report"]
    assert result["class"] == "human_capability_blocker"
    for field in REPORT_FIELDS:
        assert report[field], f"missing/empty {field}: {result}"


def test_replays_ac2_agent_executable_migration_via_production_path(tmp_path):
    """GIVEN the AC2 representative evidence (live Issue allows, writable tmp
    home, clean env, structured receipt) WHEN replayed through materialize ->
    classify THEN class == agent_executable_migration on the Step 5 -> Step 1
    route, and the payload really came from the materializer."""
    payload, result = _replay(tmp_path)
    assert payload["live_issue_authorizes_migration"] is True
    assert payload["effective_env"]["claude_gpt_home"] == str(tmp_path / "gpt-home")
    assert payload["probes"] == {"install_dir_writable": True, "host_reachable": True}
    assert result["class"] == "agent_executable_migration"
    assert result["route"] == "route_to_step1_runtime_migration_fix_delta"
    assert result["human_action_report"] is None


@pytest.mark.parametrize(
    "issue_body",
    [
        None,
        "## Outcome\n\n特に記載なし\n",
        f"## VC\n\n```bash\n{EXACT}\n```\n",
    ],
)
def test_replays_ac2_negative_issue_not_authorizing(tmp_path, issue_body):
    """GIVEN the AC2 negative evidence (live Issue does not authorize) WHEN
    replayed THEN class != agent_executable_migration."""
    _, result = _replay(tmp_path, issue_body=issue_body)
    assert result["class"] == "implementation_defect"
    assert result["route"] == "not_authorized_implementation_defect"


def test_replays_ac2_negative_non_exact_repair_command(tmp_path):
    _, result = _replay(tmp_path, receipt=_failure_receipt(repair_command=f"{EXACT} --dry-run"))
    assert result["class"] == "implementation_defect"
    assert result["route"] == "repair_command_not_exact_literal"


def test_replays_ac4_implementation_defect_worker_result(tmp_path):
    """GIVEN the AC4 evidence (repair execution failed) WHEN replayed THEN
    class == implementation_defect."""
    _, result = _replay(
        tmp_path,
        worker_result={
            "status": "failed",
            "reason_code": "repair_failed",
            "exit_code": 1,
            "deny_evidence_verified": False,
            "sudo_required_in_log": False,
        },
    )
    assert result["class"] == "implementation_defect"
    assert result["human_action_report"] is None


def test_replays_ac5_credential_blocker(tmp_path):
    payload, result = _replay(tmp_path, preflight={"chatgpt_auth": {"available": False, "detail": "logged out"}})
    assert payload["capability_flags"]["needs_credential"] is True
    _assert_full_report(result)


def test_replays_ac5_privilege_blocker_from_install_log(tmp_path):
    payload, result = _replay(tmp_path, install_log="installer: sudo required for /usr/local/bin\n")
    assert payload["capability_flags"]["needs_privilege"] is True
    _assert_full_report(result)


def test_replays_ac5_unreachable_host_blocker(tmp_path):
    payload, result = _replay(tmp_path, operator_host_differs=True)
    assert payload["probes"]["host_reachable"] is False
    _assert_full_report(result)
    assert result["human_action_report"]["reason"] == "target_operator_host_unreachable"


def test_replays_ac5_non_writable_install_dir_blocker(tmp_path):
    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    original_mode = parent.stat().st_mode
    try:
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        if os.access(parent, os.W_OK):
            pytest.skip("SKIP: running as a user that bypasses directory permission bits (e.g. root)")
        payload, result = _replay(tmp_path, home=parent / "gpt-home")
        assert payload["probes"]["install_dir_writable"] is False
        _assert_full_report(result)
        assert result["human_action_report"]["reason"] == "install_dir_not_writable"
    finally:
        parent.chmod(original_mode)


@pytest.mark.parametrize(
    "name", ["CLAUDE_CODE_PROXY_INSTALL_DIR", "CLAUDE_CODE_PROXY_VERSION", "CLAUDE_GPT_REPAIR_INSTALLER_URL"]
)
def test_replays_ac5_install_env_override_blocker(tmp_path, name):
    payload, result = _replay(tmp_path, extra_env={name: "file:///tmp/evil-installer.sh"})
    assert payload["effective_env"]["override_vars_present"] is True
    _assert_full_report(result)
    assert result["human_action_report"]["reason"] == "install_env_override_present"


def test_replays_ac5_sudo_required_worker_result_blocker(tmp_path):
    _, result = _replay(
        tmp_path,
        worker_result={
            "status": "failed",
            "reason_code": "repair_failed",
            "exit_code": 1,
            "deny_evidence_verified": False,
            "sudo_required_in_log": True,
        },
    )
    _assert_full_report(result)


def test_replays_ac5_verified_tool_call_denial_blocker(tmp_path):
    _, result = _replay(
        tmp_path,
        worker_result={
            "status": "permission_blocked",
            "reason_code": "permission_denied",
            "exit_code": None,
            "deny_evidence_verified": True,
            "sudo_required_in_log": False,
        },
    )
    _assert_full_report(result)


def test_replays_ac6_unverified_denial_is_not_a_capability_blocker(tmp_path):
    _, result = _replay(
        tmp_path,
        worker_result={
            "status": "permission_blocked",
            "reason_code": "permission_denied",
            "exit_code": None,
            "deny_evidence_verified": False,
            "sudo_required_in_log": False,
        },
    )
    assert result["class"] == "implementation_defect"


def test_classifier_cli_fails_closed_on_string_typed_flags_from_a_hand_written_payload():
    """GIVEN a hand-written payload whose bool fields are JSON strings ("false"
    is truthy under bool()) WHEN sent to the classifier CLI directly THEN it is
    implementation_defect / malformed_input_type_fail_closed."""
    payload = {
        "failure_evidence": {
            "cause": "proxy_model_catalog_incompatible",
            "repair_command": EXACT,
            "required_models": [],
            "missing_models": [],
        },
        "live_issue_authorizes_migration": "false",
        "effective_env": {"claude_gpt_home": "/h/.claude-gpt", "override_vars_present": "false"},
        "probes": {"install_dir_writable": "true", "host_reachable": "true"},
        "capability_flags": {
            "needs_credential": "false",
            "needs_secret": "false",
            "needs_privilege": "false",
            "destructive_or_global": "false",
        },
        "worker_result": None,
    }
    proc = subprocess.run(
        [sys.executable, str(CLASSIFY_SCRIPT)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    result = json.loads(proc.stdout)
    assert result["class"] == "implementation_defect"
    assert result["route"] == "malformed_input_type_fail_closed"


def test_cli_rejects_malformed_json_input_exit_2():
    """GIVEN malformed (non-JSON) stdin WHEN the CLI is invoked THEN it
    exits 2 (not a silent 0/success), never crashing with an unhandled
    traceback that could be mistaken for a passing classification."""
    proc = subprocess.run(
        [sys.executable, str(CLASSIFY_SCRIPT)],
        input="not valid json {{{",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 2


def test_step5_document_specifies_the_materialize_then_classify_canonical_path():
    """GIVEN step-5-feedback-and-termination.md WHEN inspected THEN the
    documented canonical path is the two-stage materialize -> classify CLI
    (not an LLM hand-assembled JSON), and it states the authorization
    predicate and the host_reachable definition."""
    step5 = (Path(__file__).resolve().parents[1] / "steps/step-5-feedback-and-termination.md").read_text(
        encoding="utf-8"
    )
    assert "classify_runtime_migration.py materialize" in step5
    assert "LLM が手組みしない" in step5
    assert "許可述語" in step5
    assert "issue_authorizes_repair_migration" in step5
    assert "probes.host_reachable" in step5
    assert "--operator-host-differs" in step5
