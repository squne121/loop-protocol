"""Issue #2810 fix_delta P1-A — focused tests for the production input
materializer (`classify_runtime_migration.py materialize`).

The materializer is the ONLY production path that builds the classifier's
fixed-key-set payload (Step 5 no longer hand-assembles the JSON). These
tests drive its pure functions and its real CLI (subprocess with an
injected env / issue body file / tmp home / failure evidence file).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS_DIR / "classify_runtime_migration.py"
_spec = importlib.util.spec_from_file_location(
    "impl_review_loop_classify_runtime_migration_2810_materializer", MODULE_PATH
)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)

EXACT = mod.EXACT_REPAIR_COMMAND
ALLOW_LINE = f"- migration 実行者: agent-executable bounded repair として `{EXACT}` を agent が実行してよい\n"
FIXED_PAYLOAD_KEYS = {
    "failure_evidence",
    "live_issue_authorizes_migration",
    "effective_env",
    "probes",
    "capability_flags",
    "worker_result",
}


def _launch_result(**overrides):
    result = {
        "schema": "CLAUDE_GPT_LAUNCH_RESULT_V1",
        "status": "failed",
        "reason": "model_alias_not_resolved",
        "cause": "proxy_model_catalog_incompatible",
        "required_models": ["gpt-6-sol", "gpt-6-luna"],
        "missing_models": ["gpt-6-sol", "gpt-6-luna"],
        "repair_command": EXACT,
    }
    result.update(overrides)
    return result


def _clean_env(home: Path, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home.parent), "CLAUDE_GPT_HOME": str(home)}
    env.update(extra)
    return env


# --- issue_authorizes_repair_migration -------------------------------------


def test_issue_authorization_true_for_explicit_allow_line():
    assert mod.issue_authorizes_repair_migration("## Outcome\n\n" + ALLOW_LINE) is True


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "   \n",
        "## Outcome\n\nrepair は不要である\n",
        # literal only inside a fenced VC code block -> not an authorization
        f"## VC\n\n```bash\n{EXACT}\n```\n",
        # literal without any agent-execution allowance marker
        f"- 参考: `{EXACT}` が存在する\n",
        # ambiguity / deny on the same line
        f"- `{EXACT}` は human operator が手動で実行する（agent-executable ではない）\n",
        f"- `{EXACT}` の agent 実行は禁止（agent-executable にしない）\n",
        # added arguments / glued suffix are not the exact literal
        f"- agent-executable bounded repair として `{EXACT} --dry-run` を agent が実行してよい\n",
        f"- agent-executable bounded repair として `{EXACT}.bak` を agent が実行してよい\n",
        # a conflicting deny line elsewhere wins over an allow line
        ALLOW_LINE + f"- Out of Scope: `{EXACT}` の自動実行\n",
    ],
)
def test_issue_authorization_false_when_absent_ambiguous_or_denied(body):
    assert mod.issue_authorizes_repair_migration(body) is False


# --- effective env / probes -------------------------------------------------


def test_effective_env_normalizes_home_and_detects_overrides(tmp_path):
    home = tmp_path / "custom-home"
    env = mod.materialize_effective_env(_clean_env(home))
    assert env == {"claude_gpt_home": str(home), "override_vars_present": False}

    # CLAUDE_GPT_HOME itself is NOT an override; each install-env var is.
    for name in mod.OVERRIDE_ENV_VARS:
        env = mod.materialize_effective_env(_clean_env(home, **{name: "x"}))
        assert env["override_vars_present"] is True, name
    # present-but-empty still counts (fail-closed)
    empty = mod.materialize_effective_env(_clean_env(home, CLAUDE_CODE_PROXY_VERSION=""))
    assert empty["override_vars_present"] is True


def test_effective_env_default_home_and_relative_home_are_absolute(tmp_path):
    default_env = mod.materialize_effective_env({"HOME": str(tmp_path)})
    assert default_env["claude_gpt_home"] == str(tmp_path / ".claude-gpt")
    tilde_env = mod.materialize_effective_env({"HOME": str(tmp_path), "CLAUDE_GPT_HOME": "~/x/../h"})
    assert tilde_env["claude_gpt_home"] == str(tmp_path / "h")
    relative = mod.materialize_effective_env({"CLAUDE_GPT_HOME": "rel/home"})
    assert os.path.isabs(relative["claude_gpt_home"])


def test_host_reachable_definition(tmp_path):
    probe = mod.probe_install_dir_writable(str(tmp_path / "h"))
    assert mod.probe_host_reachable(str(tmp_path / "h"), probe, False) is True
    assert mod.probe_host_reachable(str(tmp_path / "h"), probe, True) is False
    # nearest ancestor is not a directory -> indeterminate (None)
    assert mod.probe_host_reachable("x", {"probed_path": str(tmp_path / "nonexistent-xyz")}, False) is None


# --- materialize_classifier_payload (pure) ---------------------------------


def test_materialize_payload_fixed_keys_and_production_probe_is_called(tmp_path):
    home = tmp_path / "gpt-home"
    calls = []

    def spy_probe(path):
        calls.append(path)
        return mod.probe_install_dir_writable(path)

    payload = mod.materialize_classifier_payload(
        launch_result=_launch_result(),
        issue_body=ALLOW_LINE,
        env=_clean_env(home),
        install_probe=spy_probe,
    )
    assert set(payload) == FIXED_PAYLOAD_KEYS
    assert calls == [str(home)], "probe_install_dir_writable must be called from production"
    assert payload["probes"] == {"install_dir_writable": True, "host_reachable": True}
    assert payload["live_issue_authorizes_migration"] is True
    assert payload["worker_result"] is None
    assert payload["capability_flags"] == {
        "needs_credential": False,
        "needs_secret": False,
        "needs_privilege": False,
        "destructive_or_global": False,
    }
    assert mod.classify_runtime_migration(payload)["class"] == "agent_executable_migration"


def test_materialize_capability_flags_from_preflight_and_install_log(tmp_path):
    home = tmp_path / "gpt-home"
    kwargs = {"launch_result": _launch_result(), "issue_body": ALLOW_LINE, "env": _clean_env(home)}
    auth_off = {"chatgpt_auth": {"available": False, "detail": "no login"}}
    payload = mod.materialize_classifier_payload(preflight=auth_off, **kwargs)
    assert payload["capability_flags"]["needs_credential"] is True
    assert mod.classify_runtime_migration(payload)["class"] == "human_capability_blocker"

    # nested launch_result["preflight"] is honoured; available:true / non-bool are not a basis
    nested = _launch_result(preflight=auth_off)
    payload = mod.materialize_classifier_payload(**{**kwargs, "launch_result": nested})
    assert payload["capability_flags"]["needs_credential"] is True
    ok = mod.materialize_classifier_payload(preflight={"chatgpt_auth": {"available": True}}, **kwargs)
    assert ok["capability_flags"]["needs_credential"] is False
    weird = mod.materialize_classifier_payload(preflight={"chatgpt_auth": {"available": "no"}}, **kwargs)
    assert weird["capability_flags"]["needs_credential"] is False

    sudo = mod.materialize_classifier_payload(install_log="...\nSudo Required to write /usr/local\n", **kwargs)
    assert sudo["capability_flags"]["needs_privilege"] is True
    assert mod.classify_runtime_migration(sudo)["class"] == "human_capability_blocker"
    clean = mod.materialize_classifier_payload(install_log="installed ok\n", **kwargs)
    assert clean["capability_flags"]["needs_privilege"] is False


def test_materialize_worker_result_is_embedded_verbatim(tmp_path):
    worker_result = {
        "status": "failed",
        "reason_code": "repair_failed",
        "exit_code": 1,
        "deny_evidence_verified": False,
        "sudo_required_in_log": False,
    }
    payload = mod.materialize_classifier_payload(
        launch_result=_launch_result(),
        issue_body=ALLOW_LINE,
        env=_clean_env(tmp_path / "h"),
        worker_result=worker_result,
    )
    assert payload["worker_result"] == worker_result
    assert mod.classify_runtime_migration(payload)["class"] == "implementation_defect"


@pytest.mark.parametrize(
    "launch_result",
    [
        None,
        [],
        "x",
        {},  # no schema
        {"schema": "OTHER_V1"},
        _launch_result(cause=1),
        _launch_result(repair_command=["a"]),
        _launch_result(required_models="gpt-6-sol"),
        _launch_result(missing_models=[1]),
    ],
)
def test_materialize_rejects_missing_or_malformed_failure_evidence(launch_result, tmp_path):
    with pytest.raises(mod.MaterializeError):
        mod.materialize_classifier_payload(
            launch_result=launch_result, issue_body=ALLOW_LINE, env=_clean_env(tmp_path / "h")
        )


def test_materialize_rejects_wrong_typed_worker_result(tmp_path):
    with pytest.raises(mod.MaterializeError):
        mod.materialize_classifier_payload(
            launch_result=_launch_result(),
            issue_body=ALLOW_LINE,
            env=_clean_env(tmp_path / "h"),
            worker_result={"status": "ok", "deny_evidence_verified": "false"},
        )
    with pytest.raises(mod.MaterializeError):
        mod.materialize_classifier_payload(
            launch_result=_launch_result(),
            issue_body=ALLOW_LINE,
            env=_clean_env(tmp_path / "h"),
            worker_result=["not", "an", "object"],
        )


def test_materialize_failure_evidence_without_repair_command_is_not_agent_executable(tmp_path):
    launch = _launch_result()
    del launch["repair_command"]
    payload = mod.materialize_classifier_payload(
        launch_result=launch, issue_body=ALLOW_LINE, env=_clean_env(tmp_path / "h")
    )
    assert "repair_command" not in payload["failure_evidence"]
    result = mod.classify_runtime_migration(payload)
    assert result["class"] != "agent_executable_migration"


# --- real CLI (subprocess) --------------------------------------------------


def _materialize_cli(tmp_path, *, home, launch=None, issue_body=ALLOW_LINE, extra_env=None, extra_args=()):
    evidence_path = tmp_path / "launch-result.json"
    evidence_path.write_text(json.dumps(_launch_result() if launch is None else launch), encoding="utf-8")
    args = [sys.executable, str(MODULE_PATH), "materialize", "--failure-evidence-file", str(evidence_path)]
    if issue_body is not None:
        body_path = tmp_path / "issue-body.md"
        body_path.write_text(issue_body, encoding="utf-8")
        args += ["--issue-body-file", str(body_path)]
    args += list(extra_args)
    env = _clean_env(home, **(extra_env or {}))
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=60, check=False)


def _classify_cli(payload_json: str):
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH)], input=payload_json, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_cli_materialize_writable_home_yields_agent_executable(tmp_path):
    proc = _materialize_cli(tmp_path, home=tmp_path / "gpt-home")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload) == FIXED_PAYLOAD_KEYS
    assert payload["probes"] == {"install_dir_writable": True, "host_reachable": True}
    assert _classify_cli(proc.stdout)["class"] == "agent_executable_migration"


def test_cli_materialize_non_writable_home_yields_capability_blocker(tmp_path):
    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    original_mode = parent.stat().st_mode
    try:
        parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
        if os.access(parent, os.W_OK):
            pytest.skip("SKIP: running as a user that bypasses directory permission bits (e.g. root)")
        proc = _materialize_cli(tmp_path, home=parent / "gpt-home")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["probes"]["install_dir_writable"] is False
        result = _classify_cli(proc.stdout)
        assert result["class"] == "human_capability_blocker"
        assert result["human_action_report"]["reason"] == "install_dir_not_writable"
    finally:
        parent.chmod(original_mode)


@pytest.mark.parametrize(
    "name", ["CLAUDE_CODE_PROXY_INSTALL_DIR", "CLAUDE_CODE_PROXY_VERSION", "CLAUDE_GPT_REPAIR_INSTALLER_URL"]
)
def test_cli_materialize_override_env_present_yields_capability_blocker(tmp_path, name):
    proc = _materialize_cli(tmp_path, home=tmp_path / "gpt-home", extra_env={name: "https://example.invalid/x"})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["effective_env"]["override_vars_present"] is True
    result = _classify_cli(proc.stdout)
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "install_env_override_present"


def test_cli_materialize_claude_gpt_home_alone_is_not_an_override(tmp_path):
    proc = _materialize_cli(tmp_path, home=tmp_path / "gpt-home")
    assert json.loads(proc.stdout)["effective_env"]["override_vars_present"] is False


@pytest.mark.parametrize(
    "issue_body",
    [
        None,  # no issue source at all
        "## Outcome\n\n特に記載なし\n",
        f"## VC\n\n```bash\n{EXACT}\n```\n",
        f"- `{EXACT}` は human operator が手動で実行する\n",
    ],
)
def test_cli_materialize_issue_not_authorizing_or_ambiguous_is_not_agent_executable(tmp_path, issue_body):
    proc = _materialize_cli(tmp_path, home=tmp_path / "gpt-home", issue_body=issue_body)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["live_issue_authorizes_migration"] is False
    result = _classify_cli(proc.stdout)
    assert result["class"] == "implementation_defect"
    assert result["route"] == "not_authorized_implementation_defect"


def test_cli_materialize_unreadable_issue_body_file_fails_closed_to_not_authorized(tmp_path):
    evidence_path = tmp_path / "launch-result.json"
    evidence_path.write_text(json.dumps(_launch_result()), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "materialize",
            "--failure-evidence-file",
            str(evidence_path),
            "--issue-body-file",
            str(tmp_path / "does-not-exist.md"),
        ],
        capture_output=True,
        text=True,
        env=_clean_env(tmp_path / "gpt-home"),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["live_issue_authorizes_migration"] is False


def test_cli_materialize_operator_host_differs_yields_unreachable_blocker(tmp_path):
    proc = _materialize_cli(tmp_path, home=tmp_path / "gpt-home", extra_args=["--operator-host-differs"])
    assert json.loads(proc.stdout)["probes"]["host_reachable"] is False
    result = _classify_cli(proc.stdout)
    assert result["class"] == "human_capability_blocker"
    assert result["human_action_report"]["reason"] == "target_operator_host_unreachable"


def test_cli_materialize_evidence_missing_or_malformed_fails_closed_exit_2(tmp_path):
    env = _clean_env(tmp_path / "gpt-home")
    base = [sys.executable, str(MODULE_PATH), "materialize"]

    def run(*extra):
        return subprocess.run(
            base + list(extra), capture_output=True, text=True, env=env, timeout=60, check=False
        )

    missing = run("--failure-evidence-file", str(tmp_path / "nope.json"))
    assert missing.returncode == 2 and missing.stdout == ""
    assert json.loads(missing.stderr)["error"] == "materialize_evidence_invalid"

    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")
    malformed = run("--failure-evidence-file", str(bad))
    assert malformed.returncode == 2 and malformed.stdout == ""

    wrong_schema = tmp_path / "wrong.json"
    wrong_schema.write_text(json.dumps({"cause": "proxy_model_catalog_incompatible"}), encoding="utf-8")
    proc = run("--failure-evidence-file", str(wrong_schema))
    assert proc.returncode == 2 and proc.stdout == ""

    no_arg = run()
    assert no_arg.returncode == 2 and no_arg.stdout == ""


def test_cli_materialize_install_log_and_preflight_auth_derive_capability_flags(tmp_path):
    log = tmp_path / "install.log"
    log.write_text("fatal: sudo required to write /opt/bin\n", encoding="utf-8")
    preflight = tmp_path / "preflight.json"
    preflight.write_text(json.dumps({"chatgpt_auth": {"available": False}}), encoding="utf-8")
    proc = _materialize_cli(
        tmp_path,
        home=tmp_path / "gpt-home",
        extra_args=["--install-log-file", str(log), "--preflight-file", str(preflight)],
    )
    assert proc.returncode == 0, proc.stderr
    flags = json.loads(proc.stdout)["capability_flags"]
    assert flags["needs_privilege"] is True and flags["needs_credential"] is True
    assert flags["needs_secret"] is False and flags["destructive_or_global"] is False


def test_cli_unknown_subcommand_exit_2_and_no_arg_classify_is_backward_compatible():
    proc = subprocess.run(
        [sys.executable, str(MODULE_PATH), "frobnicate"], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 2
    # no-arg stdin classification still works unchanged
    payload = {"failure_evidence": {}, "live_issue_authorizes_migration": False}
    assert _classify_cli(json.dumps(payload))["class"] == "implementation_defect"


def test_body_authoring_sample_line_satisfies_the_materializer_predicate():
    """GIVEN the authorization sample line documented in create-issue
    body-authoring.md WHEN fed (as Issue body text) to the production
    predicate THEN it authorizes -- the guidance and the predicate cannot
    drift apart silently."""
    doc = (Path(__file__).resolve().parents[4] / ".claude/skills/create-issue/references/body-authoring.md").read_text(
        encoding="utf-8"
    )
    sample_lines = [
        line for line in doc.splitlines() if line.startswith("- migration 実行者:") and EXACT in line
    ]
    assert len(sample_lines) == 1
    assert mod.issue_authorizes_repair_migration(sample_lines[0] + "\n") is True
