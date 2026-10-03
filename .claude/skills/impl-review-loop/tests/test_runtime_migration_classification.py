"""Issue #2810 AC2/AC4/AC5/AC7 — focused tests for
`classify_runtime_migration.py`'s three pure functions.

`classify_runtime_migration()` is the root-owned classifier: it consumes a
fixed-key-set payload (failure evidence, live-issue authorization,
effective env, probes, capability flags, and an optional post-execution
`worker_result`) and returns exactly one of `agent_executable_migration` /
`implementation_defect` / `human_capability_blocker`.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS_DIR / "classify_runtime_migration.py"

_spec = importlib.util.spec_from_file_location(
    "impl_review_loop_classify_runtime_migration_2810", MODULE_PATH
)
classify_runtime_migration_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(classify_runtime_migration_mod)

classify_runtime_migration = classify_runtime_migration_mod.classify_runtime_migration
probe_install_dir_writable = classify_runtime_migration_mod.probe_install_dir_writable
bind_pre_post_identity = classify_runtime_migration_mod.bind_pre_post_identity
EXACT_REPAIR_COMMAND = classify_runtime_migration_mod.EXACT_REPAIR_COMMAND


def _base_payload(**overrides):
    payload = {
        "failure_evidence": {
            "cause": "proxy_model_catalog_incompatible",
            "repair_command": EXACT_REPAIR_COMMAND,
            "required_models": ["gpt-6-sol", "gpt-6-luna"],
            "missing_models": ["gpt-6-sol", "gpt-6-luna"],
        },
        "live_issue_authorizes_migration": True,
        "effective_env": {
            "claude_gpt_home": "/home/operator/.claude-gpt",
            "override_vars_present": False,
        },
        "probes": {
            "install_dir_writable": True,
            "host_reachable": True,
        },
        "capability_flags": {
            "needs_credential": False,
            "needs_secret": False,
            "needs_privilege": False,
            "destructive_or_global": False,
        },
        "worker_result": None,
    }
    payload.update(overrides)
    return payload


# --- AC2: agent_executable_migration ---------------------------------------


def test_agent_executable_migration_when_all_conditions_met():
    """GIVEN a fully-authorized, structured, safe, writable, reachable
    failure WHEN classified THEN class == agent_executable_migration and
    route sends it to the Step 5 -> Step 1 fix_delta route (never a human
    escalation)."""
    result = classify_runtime_migration(_base_payload())
    assert result["class"] == "agent_executable_migration"
    assert result["route"] == "route_to_step1_runtime_migration_fix_delta"
    assert result["human_action_report"] is None


def test_agent_executable_migration_rejected_when_issue_does_not_authorize():
    """GIVEN live Issue does NOT authorize migration WHEN classified THEN
    class != agent_executable_migration."""
    result = classify_runtime_migration(
        _base_payload(live_issue_authorizes_migration=False)
    )
    assert result["class"] != "agent_executable_migration"


def test_agent_executable_migration_rejected_when_repair_command_not_exact_literal():
    """GIVEN repair_command has extra arguments WHEN classified THEN
    class != agent_executable_migration (Outcome bullet 1: literal, no
    added arguments)."""
    result = classify_runtime_migration(
        _base_payload(
            failure_evidence={
                "cause": "proxy_model_catalog_incompatible",
                "repair_command": EXACT_REPAIR_COMMAND + " --force",
                "required_models": [],
                "missing_models": [],
            }
        )
    )
    assert result["class"] != "agent_executable_migration"


def test_agent_executable_migration_rejected_when_cause_not_structured_incompatibility():
    """GIVEN failure_evidence.cause is not the known structured
    incompatibility WHEN classified THEN class != agent_executable_migration."""
    result = classify_runtime_migration(
        _base_payload(
            failure_evidence={
                "cause": "some_other_unrelated_failure",
                "repair_command": EXACT_REPAIR_COMMAND,
                "required_models": [],
                "missing_models": [],
            }
        )
    )
    assert result["class"] != "agent_executable_migration"


# --- AC4: implementation_defect ---------------------------------------------


def test_implementation_defect_when_repair_execution_failed():
    """GIVEN a worker_result reporting repair_failed WHEN classified THEN
    class == implementation_defect (stays in normal fix loop, never
    human_action_required)."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "failed",
                "reason_code": "repair_failed",
                "exit_code": 1,
                "deny_evidence_verified": False,
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] == "implementation_defect"
    assert result["human_action_report"] is None


def test_implementation_defect_when_repair_execution_failed_exit_2():
    """GIVEN repair_proxy.sh's post-install catalog mismatch (exit 2)
    surfaced as repair_failed WHEN classified THEN class ==
    implementation_defect."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "failed",
                "reason_code": "repair_failed",
                "exit_code": 2,
                "deny_evidence_verified": False,
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] == "implementation_defect"


# --- AC5: human_capability_blocker ------------------------------------------


@pytest.mark.parametrize(
    "flag_name,reason",
    [
        ("needs_credential", "credential_login_or_reauth_required"),
        ("needs_secret", "secret_or_token_operation_required"),
        ("needs_privilege", "privilege_escalation_required"),
        ("destructive_or_global", "destructive_or_global_mutation_required"),
    ],
)
def test_human_capability_blocker_for_each_capability_flag(flag_name, reason):
    """GIVEN a single capability_flags entry is true WHEN classified THEN
    class == human_capability_blocker with the matching reason and a fully
    populated stop report."""
    flags = {
        "needs_credential": False,
        "needs_secret": False,
        "needs_privilege": False,
        "destructive_or_global": False,
    }
    flags[flag_name] = True
    result = classify_runtime_migration(_base_payload(capability_flags=flags))
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == reason


def test_human_capability_blocker_report_fields_are_all_present_and_non_empty():
    """GIVEN any human_capability_blocker classification WHEN inspecting
    human_action_report THEN reason / required_human_action /
    target_environment / verification_command / resume_condition are all
    present and non-empty (Issue #2810 AC5 required stop-report fields)."""
    result = classify_runtime_migration(
        _base_payload(
            capability_flags={
                "needs_credential": True,
                "needs_secret": False,
                "needs_privilege": False,
                "destructive_or_global": False,
            }
        )
    )
    report = result["human_action_report"]
    for field in (
        "reason",
        "required_human_action",
        "target_environment",
        "verification_command",
        "resume_condition",
    ):
        assert field in report
        assert report[field], f"{field} must be non-empty"


def test_human_capability_blocker_non_writable_install_dir_does_not_start_repair():
    """GIVEN probes.install_dir_writable is False WHEN classified THEN
    class == human_capability_blocker and route is human_escalation (repair
    command is never started)."""
    result = classify_runtime_migration(
        _base_payload(probes={"install_dir_writable": False, "host_reachable": True})
    )
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "install_dir_not_writable"
    assert result["route"] == "human_escalation_capability_blocker"


def test_human_capability_blocker_unreachable_target_host():
    """GIVEN probes.host_reachable is False WHEN classified THEN class ==
    human_capability_blocker."""
    result = classify_runtime_migration(
        _base_payload(probes={"install_dir_writable": True, "host_reachable": False})
    )
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "target_operator_host_unreachable"


def test_human_capability_blocker_install_env_override_present():
    """GIVEN effective_env.override_vars_present is True (one of
    CLAUDE_CODE_PROXY_INSTALL_DIR / CLAUDE_CODE_PROXY_VERSION /
    CLAUDE_GPT_REPAIR_INSTALLER_URL is set) WHEN classified THEN class ==
    human_capability_blocker (never agent_executable_migration, since an
    arbitrary-URL installer could run)."""
    result = classify_runtime_migration(
        _base_payload(
            effective_env={
                "claude_gpt_home": "/home/operator/.claude-gpt",
                "override_vars_present": True,
            }
        )
    )
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "install_env_override_present"


def test_human_capability_blocker_install_log_reports_sudo_required():
    """GIVEN worker_result.sudo_required_in_log is True WHEN classified
    THEN class == human_capability_blocker with reason
    privileged_mutation_required (second line of defense beyond the
    pre-execution writability probe)."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "failed",
                "reason_code": "repair_failed",
                "exit_code": 1,
                "deny_evidence_verified": False,
                "sudo_required_in_log": True,
            }
        )
    )
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "privileged_mutation_required"


def test_human_capability_blocker_permission_denied_with_verified_deny_evidence():
    """GIVEN worker_result reports permission_blocked/permission_denied AND
    root has independently verified the deny evidence (
    deny_evidence_verified=True) WHEN classified THEN class ==
    human_capability_blocker."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "permission_blocked",
                "reason_code": "permission_denied",
                "exit_code": None,
                "deny_evidence_verified": True,
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "agent_tool_call_denied_by_policy"


def test_unverified_permission_denied_self_report_is_not_a_capability_blocker():
    """GIVEN worker_result reports permission_blocked/permission_denied but
    root could NOT verify deny evidence (deny_evidence_verified=False,
    i.e. a bare self-report) WHEN classified THEN class !=
    human_capability_blocker (AC6: self-report alone has no stop
    authority)."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "permission_blocked",
                "reason_code": "permission_denied",
                "exit_code": None,
                "deny_evidence_verified": False,
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] != "human_capability_blocker"
    assert result["class"] == "implementation_defect"


# --- AC7: bind_pre_post_identity() ------------------------------------------


def _identity_pre_post(**post_overrides):
    pre = {
        "claude_gpt_home_absolute_path": "/home/operator/.claude-gpt",
        "launch_sh_sha256": "a" * 64,
        "repo_head": "deadbeef",
    }
    post = {
        "claude_gpt_home_absolute_path": "/home/operator/.claude-gpt",
        "launch_sh_sha256": "a" * 64,
        "repo_head": "deadbeef",
        "claude_gpt_proxy_bin_env_set": False,
        "selected_proxy_absolute_path": "/home/operator/.claude-gpt/bin/claude-code-proxy",
    }
    post.update(post_overrides)
    return pre, post


def test_identity_binding_accepts_matching_pre_post_evidence():
    """GIVEN matching pre/post evidence for the same effective launcher
    environment WHEN bound THEN identity_bound is True with no
    mismatches."""
    pre, post = _identity_pre_post()
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is True
    assert result["mismatches"] == []


def test_identity_binding_rejects_claude_gpt_home_mismatch():
    """GIVEN pre/post CLAUDE_GPT_HOME absolute paths differ WHEN bound THEN
    identity_bound is False (AC7)."""
    pre, post = _identity_pre_post(claude_gpt_home_absolute_path="/tmp/other-home")
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is False
    assert "claude_gpt_home_absolute_path_mismatch" in result["mismatches"]


def test_identity_binding_rejects_launch_sh_sha256_mismatch():
    """GIVEN pre/post launch_sh_sha256 differ (launch.sh changed between
    probe and re-verification) WHEN bound THEN identity_bound is False."""
    pre, post = _identity_pre_post(launch_sh_sha256="b" * 64)
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is False
    assert "launch_sh_sha256_mismatch" in result["mismatches"]


def test_identity_binding_rejects_repo_head_mismatch():
    """GIVEN pre/post repository head differ WHEN bound THEN identity_bound
    is False."""
    pre, post = _identity_pre_post(repo_head="c0ffee")
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is False
    assert "repo_head_mismatch" in result["mismatches"]


def test_identity_binding_rejects_claude_gpt_proxy_bin_set_post_repair():
    """GIVEN CLAUDE_GPT_PROXY_BIN is set post-repair WHEN bound THEN
    identity_bound is False (post-repair verification must use unset
    CLAUDE_GPT_PROXY_BIN so the managed binary is actually selected)."""
    pre, post = _identity_pre_post(claude_gpt_proxy_bin_env_set=True)
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is False
    assert "post_repair_claude_gpt_proxy_bin_env_set" in result["mismatches"]


def test_identity_binding_rejects_non_managed_selected_proxy():
    """GIVEN the post-repair selected proxy is not the managed binary path
    WHEN bound THEN identity_bound is False."""
    pre, post = _identity_pre_post(
        selected_proxy_absolute_path="/usr/local/bin/claude-code-proxy"
    )
    result = bind_pre_post_identity(pre, post)
    assert result["identity_bound"] is False
    assert "selected_proxy_not_managed_binary" in result["mismatches"]


# --- probe_install_dir_writable() -------------------------------------------


def test_probe_install_dir_writable_true_for_fresh_writable_home(tmp_path):
    """GIVEN a fresh writable CLAUDE_GPT_HOME with no existing bin dir WHEN
    probed THEN writable is True (nearest existing ancestor -- tmp_path
    itself -- is writable)."""
    claude_gpt_home = tmp_path / "claude-gpt-home"
    result = probe_install_dir_writable(str(claude_gpt_home))
    assert result["writable"] is True
    assert result["target_bin_dir"] == str(claude_gpt_home / "bin")


def test_probe_install_dir_writable_false_for_non_writable_ancestor(tmp_path):
    """GIVEN the nearest existing ancestor directory is not writable WHEN
    probed THEN writable is False."""
    claude_gpt_home = tmp_path / "readonly-parent" / "claude-gpt-home"
    readonly_parent = tmp_path / "readonly-parent"
    readonly_parent.mkdir()
    original_mode = readonly_parent.stat().st_mode
    try:
        readonly_parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        if os.access(readonly_parent, os.W_OK):
            pytest.skip("SKIP: running as a user that bypasses directory permission bits (e.g. root)")
        result = probe_install_dir_writable(str(claude_gpt_home))
        assert result["writable"] is False
    finally:
        readonly_parent.chmod(original_mode)


def test_probe_install_dir_writable_false_when_existing_proxy_blocks_replace(tmp_path):
    """GIVEN an existing claude-code-proxy file that cannot be replaced
    (neither the bin dir nor the file itself is writable) WHEN probed THEN
    writable is False and existing_proxy_blocks_replace is True."""
    claude_gpt_home = tmp_path / "claude-gpt-home"
    bin_dir = claude_gpt_home / "bin"
    bin_dir.mkdir(parents=True)
    proxy_path = bin_dir / "claude-code-proxy"
    proxy_path.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    proxy_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    bin_original_mode = bin_dir.stat().st_mode
    try:
        bin_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        if os.access(bin_dir, os.W_OK) or os.access(proxy_path, os.W_OK):
            pytest.skip("SKIP: running as a user that bypasses file permission bits (e.g. root)")
        result = probe_install_dir_writable(str(claude_gpt_home))
        assert result["writable"] is False
        assert result["existing_proxy_blocks_replace"] is True
    finally:
        bin_dir.chmod(bin_original_mode)
        proxy_path.chmod(stat.S_IWUSR | stat.S_IRUSR)


# --- primitive type validation (Issue #2810 fix_delta P1-C) -----------------

MALFORMED_ROUTE = "malformed_input_type_fail_closed"


def _with_path(payload, path, value):
    """Return a deep-ish copy of ``payload`` with ``path`` (dotted) set."""
    import copy

    clone = copy.deepcopy(payload)
    parts = path.split(".")
    target = clone
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    return clone


@pytest.mark.parametrize(
    "path,bad_value",
    [
        ("live_issue_authorizes_migration", "false"),
        ("live_issue_authorizes_migration", "true"),
        ("live_issue_authorizes_migration", 1),
        ("live_issue_authorizes_migration", [True]),
        ("effective_env.override_vars_present", "false"),
        ("effective_env.override_vars_present", 0),
        ("probes.install_dir_writable", "true"),
        ("probes.install_dir_writable", 1),
        ("probes.host_reachable", "true"),
        ("probes.host_reachable", []),
        ("capability_flags.needs_credential", "false"),
        ("capability_flags.needs_secret", "false"),
        ("capability_flags.needs_privilege", "false"),
        ("capability_flags.destructive_or_global", 0),
        ("failure_evidence.repair_command", ["bash", "scripts/claude-gpt/repair_proxy.sh"]),
        ("failure_evidence.cause", True),
        ("effective_env.claude_gpt_home", 123),
    ],
)
def test_wrong_primitive_type_is_never_agent_executable_and_fails_closed(path, bad_value):
    """GIVEN a fully agent-executable payload with ONE fixed key carrying a
    wrong primitive type (JSON string "false"/"true", int 0/1, list, ...)
    WHEN classified THEN the result is implementation_defect with the fixed
    malformed_input_type_fail_closed route -- never agent_executable_migration
    and never a (mis)flipped human_capability_blocker."""
    result = classify_runtime_migration(_with_path(_base_payload(), path, bad_value))
    assert result["class"] == "implementation_defect", (path, bad_value)
    assert result["route"] == MALFORMED_ROUTE
    assert result["human_action_report"] is None


@pytest.mark.parametrize(
    "path,bad_value",
    [
        ("status", 1),
        ("reason_code", ["repair_failed"]),
        ("exit_code", "1"),
        ("exit_code", True),
        ("deny_evidence_verified", "false"),
        ("deny_evidence_verified", 1),
        ("sudo_required_in_log", "false"),
    ],
)
def test_worker_result_wrong_primitive_type_fails_closed(path, bad_value):
    """GIVEN a verified-deny worker_result with one field of a wrong
    primitive type WHEN classified THEN it never becomes a
    human_capability_blocker (deny_evidence_verified:"false" is truthy under
    bool() and must not be honoured) nor agent-executable."""
    worker_result = {
        "status": "permission_blocked",
        "reason_code": "permission_denied",
        "exit_code": None,
        "deny_evidence_verified": True,
        "sudo_required_in_log": False,
    }
    worker_result[path] = bad_value
    result = classify_runtime_migration(_base_payload(worker_result=worker_result))
    assert result["class"] == "implementation_defect"
    assert result["route"] == MALFORMED_ROUTE


def test_string_false_deny_evidence_does_not_grant_human_capability_blocker():
    """GIVEN worker_result.deny_evidence_verified is the JSON string "false"
    (truthy under bool()) WHEN classified THEN NOT human_capability_blocker."""
    result = classify_runtime_migration(
        _base_payload(
            worker_result={
                "status": "permission_blocked",
                "reason_code": "permission_denied",
                "exit_code": None,
                "deny_evidence_verified": "false",
                "sudo_required_in_log": False,
            }
        )
    )
    assert result["class"] != "human_capability_blocker"
    assert result["class"] != "agent_executable_migration"


@pytest.mark.parametrize("section", ["failure_evidence", "effective_env", "probes", "capability_flags"])
def test_non_object_sub_object_fails_closed(section):
    """GIVEN a sub-object that is a JSON string/list instead of an object
    WHEN classified THEN it fails closed (not silently defaulted)."""
    for bad in ("x", ["x"], 1, True):
        result = classify_runtime_migration(_base_payload(**{section: bad}))
        assert result["class"] == "implementation_defect", (section, bad)
        assert result["route"] == MALFORMED_ROUTE


def test_missing_keys_keep_historical_defaults_and_only_wrong_types_are_rejected():
    """GIVEN keys that are simply ABSENT WHEN classified THEN the historical
    default behaviour is preserved (no malformed_input_type_fail_closed):
    missing live_issue_authorizes_migration -> not_authorized, missing
    capability flags -> treated as False (agent-executable stays reachable)."""
    payload = _base_payload()
    del payload["live_issue_authorizes_migration"]
    result = classify_runtime_migration(payload)
    assert result["route"] == "not_authorized_implementation_defect"

    payload = _base_payload()
    payload["capability_flags"] = {}
    payload["worker_result"] = None
    assert classify_runtime_migration(payload)["class"] == "agent_executable_migration"


def test_find_payload_type_violations_lists_offending_paths():
    """GIVEN a payload with two wrong-typed keys WHEN inspected THEN both
    dotted paths are reported."""
    violations = classify_runtime_migration_mod.find_payload_type_violations(
        _with_path(_with_path(_base_payload(), "probes.host_reachable", "true"), "capability_flags.needs_secret", "no")
    )
    assert any(v.startswith("probes.host_reachable") for v in violations)
    assert any(v.startswith("capability_flags.needs_secret") for v in violations)
