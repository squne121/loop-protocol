"""scripts/claude-gpt/tests/test_workflow_capability_preflight.py

Issue #2273: `--workflow-profile issue-to-impl` workflow capability
preflight. Covers AC1-AC11, AC13, AC14 (AC12 lives in
`scripts/agent-guards/tests/test_trusted_runtime_capability_preflight.py`;
AC15/AC16 live in
`.claude/skills/issue-refinement-loop/scripts/tests/test_root_entry_router_workflow_capability.py`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

for _p in (_SCRIPTS_DIR, _GUARDS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import workflow_capability_preflight as wcp  # noqa: E402
import trusted_runtime_capabilities as trusted_uv_mod  # noqa: E402
import skill_runtime_exec as exec_mod  # noqa: E402

PREFLIGHT_SH = _SCRIPTS_DIR / "preflight.sh"
_DEFAULT_REPO = "squne121/loop-protocol"


def _completed_probe(stdout: str = "") -> wcp.ProbeOutcome:
    return wcp.ProbeOutcome(wcp.PROBE_COMPLETED, stdout=stdout, returncode=0)


def _run_preflight_cli(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(PREFLIGHT_SH), "--workflow-profile", *extra_args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class _FakePasswdEntry:
    def __init__(self, home: str) -> None:
        self.pw_dir = home


def _patch_real_account_home(monkeypatch, home_dir: Path) -> None:
    """Patch `pwd.getpwuid` (not `HOME`) so `_os_account_home()` resolves to
    `home_dir` -- the same pattern used by
    `scripts/agent-guards/tests/test_trusted_toolchain_isolated_home.py`."""
    monkeypatch.setattr(exec_mod.pwd, "getpwuid", lambda uid: _FakePasswdEntry(str(home_dir)))


def _write_fake_uv(bin_dir: Path, version: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv_path = bin_dir / "uv"
    uv_path.write_text(f"#!/bin/sh\necho 'uv {version} (x86_64-unknown-linux-gnu)'\n")
    uv_path.chmod(0o755)
    return uv_path


# --- AC1 ---------------------------------------------------------------


def test_workflow_capability_returns_structured_result():
    proc = _run_preflight_cli("issue-to-impl")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["schema"] == "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1"
    assert result["profile"] == "issue-to-impl"
    assert result["decision"] in ("ready", "degraded", "blocked")
    assert set(result.keys()) == {
        "schema", "profile", "decision", "checks", "reasons", "actor_capabilities",
    }
    # Issue #2340 AC2: actor/execution-substrate-scoped capability entries.
    assert set(result["actor_capabilities"].keys()) == {
        "root_github_read", "controlled_github_read", "delegated_research_agy", "spark_delegation",
    }
    for entry in result["actor_capabilities"].values():
        assert set(entry.keys()) == {"status", "reason_code", "fallback_route", "probe_execution_class"}
        assert entry["status"] in ("ready", "degraded", "unavailable")
    assert set(result["checks"].keys()) == {"uv", "spark", "github"}
    assert set(result["checks"]["uv"].keys()) == {"status", "reason", "diagnostic"}
    assert set(result["checks"]["spark"].keys()) == {"status"}
    assert set(result["checks"]["github"].keys()) == {"auth", "repo_read", "operations"}
    assert isinstance(result["reasons"], list)


# --- AC2 -----------------------------------------------------------------


def test_workflow_capability_uses_trusted_resolver(monkeypatch):
    calls = []

    def fake_check(project_root):
        calls.append(project_root)
        return {"status": "ok", "reason": "resolved", "resolved_path": "/fake/uv"}

    monkeypatch.setattr(wcp.trusted_uv_mod, "check_trusted_uv", fake_check)
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert calls == [str(_REPO_ROOT)]
    assert result["checks"]["uv"]["status"] == "ok"


# --- AC3 -------------------------------------------------------------------


def test_workflow_capability_accepts_pinned_local_bin_uv(monkeypatch, tmp_path):
    _patch_real_account_home(monkeypatch, tmp_path)
    required = exec_mod._required_uv_version(str(_REPO_ROOT))
    assert required, "pyproject.toml must declare [tool.uv] required-version for this test"
    _write_fake_uv(tmp_path / ".local" / "bin", required)

    result = trusted_uv_mod.check_trusted_uv(str(_REPO_ROOT))

    assert result["status"] == trusted_uv_mod.STATUS_OK


def test_workflow_capability_rejects_unpinned_local_bin_uv(monkeypatch, tmp_path):
    _patch_real_account_home(monkeypatch, tmp_path)
    # `_resolve_trusted_executable`'s search order for `uv` is
    # hostedtoolcache -> account-home (user-local) -> system standard
    # directories (see its ordering docstring; corrected in Issue #2275
    # fix_delta P1-3 -- account-home is checked BEFORE system, not after).
    # On a CI runner that already provisioned a correctly pinned `uv` via
    # hostedtoolcache, `shutil.which` would resolve THAT one first and
    # never reach this test's mismatched account-home fake, masking the
    # rejection this test exists to prove. Isolate resolution to the
    # account-home lane by emptying the higher-priority hostedtoolcache
    # lane and the lower-priority system lane.
    monkeypatch.setattr(exec_mod, "_trusted_toolchain_dirs", lambda executable_name: [])
    monkeypatch.setattr(exec_mod, "_SYSTEM_STANDARD_PATH_DIRS", ())
    required = exec_mod._required_uv_version(str(_REPO_ROOT))
    assert required
    mismatched = "0.0.1" if required != "0.0.1" else "0.0.2"
    _write_fake_uv(tmp_path / ".local" / "bin", mismatched)

    result = trusted_uv_mod.check_trusted_uv(str(_REPO_ROOT))

    assert result["status"] == trusted_uv_mod.STATUS_VERSION_MISMATCH


# --- AC4 ---------------------------------------------------------------


def test_workflow_capability_accepts_pinned_uv():
    # The real system `uv` (whichever trust lane it resolves through --
    # hostedtoolcache, account-home, or system PATH) must satisfy the exact
    # pyproject.toml pin in THIS dev/CI environment for the repository's own
    # `uv run --locked` invocations to work at all, so this is a genuine,
    # non-mocked end-to-end check of the shared acceptance path (Issue
    # #2273 AC4: system/hosted-toolcache AND re-permitted account-home `uv`
    # are treated as the SAME acceptance route).
    result = trusted_uv_mod.check_trusted_uv(str(_REPO_ROOT))
    assert result["status"] == trusted_uv_mod.STATUS_OK
    assert result["resolved_path"]


# --- AC5 / AC6 -----------------------------------------------------------


def _no_spark_env(monkeypatch):
    monkeypatch.setattr(
        wcp,
        "_run_env_only_preflight",
        lambda deadline_ns: _completed_probe(
            json.dumps({"binary_available": False, "chatgpt_auth": {"available": False}})
        ),
    )


def test_workflow_capability_blocks_required_spark_incompatibility(monkeypatch):
    _no_spark_env(monkeypatch)
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=[],
    )
    assert result["checks"]["spark"]["status"] == wcp.SPARK_UNAVAILABLE
    assert result["decision"] == wcp.DECISION_BLOCKED
    assert any("spark" in reason for reason in result["reasons"])


def test_workflow_capability_degrades_preferred_spark_incompatibility(monkeypatch):
    _no_spark_env(monkeypatch)
    # Isolate this assertion to the Spark route judgment: without pinning
    # GitHub auth/repo-read to available, a CI runner without `gh auth`
    # configured would report decision=blocked via the (unrelated)
    # `not github_auth` branch before the Spark fallback_only -> degraded
    # branch is ever reached.
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: _completed_probe())
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: _completed_probe())
    # Issue #2340 AC2: `controlled_github_read` is a REQUIRED capability that
    # fails closed with priority over the Spark route's OPTIONAL `degraded`
    # signal (see `test_actor_scoped_capability_probe.py::
    # test_root_read_pass_controlled_read_fail_blocks_before_downstream_actor`,
    # which asserts `controlled_github_unavailable` blocks regardless of any
    # other signal). Without pinning this probe to `ready`, a CI runner
    # without a live, sanitized-env `gh api` route would report
    # decision=blocked via `controlled_github_unavailable` before the Spark
    # fallback_only -> degraded branch under test is ever reached.
    monkeypatch.setattr(
        wcp,
        "_controlled_github_read_probe",
        lambda repo, deadline_ns: _completed_probe(),
    )
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode="preferred",
        spark_fallback="allowed",
        planned_operations=[],
    )
    assert result["checks"]["spark"]["status"] == wcp.SPARK_FALLBACK_ONLY
    assert result["decision"] == wcp.DECISION_DEGRADED


# --- AC7 -------------------------------------------------------------------


def test_workflow_capability_separates_read_and_write_github_capability(monkeypatch):
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: _completed_probe())
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: _completed_probe())
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[
            {"phase": "implement", "actor_role": "issue-editor", "operation": "issue_comment"}
        ],
    )
    github = result["checks"]["github"]
    assert github["auth"] is True
    assert github["repo_read"] is True
    assert github["operations"]["issue_comment"]["route"] == "available"
    # read and write are reported through DISTINCT fields, not a single
    # collapsed boolean.
    assert "operations" in github and isinstance(github["operations"], dict)


# --- AC8 -------------------------------------------------------------------


def test_workflow_capability_read_only_route_does_not_pass_write(monkeypatch):
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: _completed_probe())
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: _completed_probe())
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[
            {
                "phase": "implement",
                "actor_role": "issue-editor",
                "operation": "unregistered_mutation_op",
            }
        ],
    )
    github = result["checks"]["github"]
    assert github["auth"] is True
    assert github["repo_read"] is True
    assert github["operations"]["unregistered_mutation_op"]["route"] == "unavailable"
    assert github["operations"]["unregistered_mutation_op"]["permission"] == "unverified"
    assert result["decision"] == wcp.DECISION_BLOCKED


# --- AC9 -------------------------------------------------------------------


def test_workflow_capability_blocks_missing_root_owned_mutation_route(monkeypatch):
    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: _completed_probe())
    monkeypatch.setattr(wcp, "_github_repo_read_probe", lambda repo, deadline_ns: _completed_probe())
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[
            {"phase": "merge", "actor_role": "unregistered-actor", "operation": "no_such_route"}
        ],
    )
    assert result["decision"] == wcp.DECISION_BLOCKED
    assert result["checks"]["github"]["operations"]["no_such_route"]["route"] == "unavailable"
    assert any("no_such_route" in reason for reason in result["reasons"])


# --- AC10 ------------------------------------------------------------------


def test_workflow_capability_output_excludes_raw_credential(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_super_secret_value_1234567890")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_another_secret_value_0987654321")
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    serialized = json.dumps(result)
    assert "ghp_super_secret_value_1234567890" not in serialized
    assert "ghp_another_secret_value_0987654321" not in serialized


# --- AC11 ------------------------------------------------------------------


def test_workflow_capability_blocked_result_includes_remediation(monkeypatch):
    monkeypatch.setattr(
        wcp.trusted_uv_mod,
        "check_trusted_uv",
        lambda project_root: {
            "status": trusted_uv_mod.STATUS_MISSING,
            "reason": "uv_not_found",
            "resolved_path": None,
        },
    )
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert result["decision"] == wcp.DECISION_BLOCKED
    assert result["reasons"], "blocked result must include remediation reasons"
    assert any("uv" in reason and ("install" in reason or "hostedtoolcache" in reason) for reason in result["reasons"])


# --- AC13 ------------------------------------------------------------------


def test_launch_check_only_contract_not_regressed():
    syntax_check = subprocess.run(["sh", "-n", str(PREFLIGHT_SH)], capture_output=True, text=True)
    assert syntax_check.returncode == 0, syntax_check.stderr

    # The pre-existing full-mode / --env-only lanes (used by launch.sh
    # --check-only via preflight.sh) must keep returning their own
    # CLAUDE_GPT_PREFLIGHT_RESULT_V1 schema, untouched by the new
    # --workflow-profile dispatch branch (schema separation, Issue #2273 In
    # Scope).
    proc = subprocess.run(
        ["sh", str(PREFLIGHT_SH), "--env-only"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode in (0, 3, 4, 5, 6)
    payload = json.loads(proc.stdout)
    assert payload["schema"] == "CLAUDE_GPT_PREFLIGHT_RESULT_V1"


# --- AC14 ------------------------------------------------------------------


def test_no_broker_dependency_reference():
    source = (_SCRIPTS_DIR / "workflow_capability_preflight.py").read_text(encoding="utf-8")
    forbidden_terms = (
        "mutation_broker",
        "MutationBroker",
        "controlled_executor",
        "ControlledExecutor",
        "production_broker",
    )
    for term in forbidden_terms:
        assert term not in source, f"unexpected broker dependency reference: {term}"



# --- Issue #2275 fix_delta P1-1: uv_not_found diagnostic reaches outer JSON -


def test_workflow_capability_uv_not_found_reason_and_diagnostic_reach_outer_json(monkeypatch):
    """GIVEN the trusted resolver reports `uv_not_found` (a structured
    diagnostic payload embedded in the underlying `RuntimeError`)
    THEN the outer `CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1.checks.uv` result
    must expose a short stable `reason` string (never a JSON-in-a-string
    double-encoding) AND the parsed `diagnostic` object, all the way
    through `trusted_runtime_capabilities.check_trusted_uv()` ->
    `workflow_capability_preflight.assess()` -> outer JSON (Issue #2275
    fix_delta P1-1 consumer path). Uses `monkeypatch` on the resolver only
    -- no real uv uninstall, no live canary."""
    payload = {
        "error": "uv_not_found",
        "candidates_searched": ["/opt/hostedtoolcache/uv"],
        "expected_version": "0.11.29",
        "recommended_install_dir": "/fake/home/.local/bin",
        "remediation_hint": "install the pinned uv version",
    }
    message = "uv_not_found: " + json.dumps(payload)

    class _FakeResolverModule:
        def _resolve_trusted_executable(self, name, project_root):  # noqa: ANN001
            raise RuntimeError(message)

    monkeypatch.setattr(trusted_uv_mod, "_load_skill_runtime_exec", lambda: _FakeResolverModule())

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )

    uv_check = result["checks"]["uv"]
    assert uv_check["status"] == trusted_uv_mod.STATUS_MISSING
    assert uv_check["reason"] == "uv_not_found"
    assert "{" not in uv_check["reason"]
    assert uv_check["diagnostic"] == payload
    assert result["decision"] == wcp.DECISION_BLOCKED
    # `reasons` free-text must still surface a stable status code, not the
    # raw JSON diagnostic blob, keeping the human-readable summary short.
    assert any("uv:trusted_uv_missing" in reason for reason in result["reasons"])


# --- P1-1: repo_read: false must be blocked, not degraded ------------------


def test_workflow_capability_repo_read_false_blocks(monkeypatch):
    """GIVEN github auth succeeds but the repository read probe fails
    (`gh repo view` non-zero)
    THEN decision must be `blocked`, not `degraded` -- a workflow cannot
    read the very issue/PR state it needs to operate on, so this is not a
    merely-degraded capability state (P1-1 fix)."""

    monkeypatch.setattr(wcp, "_github_auth_probe", lambda deadline_ns: _completed_probe())
    monkeypatch.setattr(
        wcp,
        "_github_repo_read_probe",
        lambda repo, deadline_ns: wcp.ProbeOutcome(wcp.PROBE_NONZERO_EXIT, returncode=1),
    )
    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo=_DEFAULT_REPO,
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )
    assert result["checks"]["github"]["auth"] is True
    assert result["checks"]["github"]["repo_read"] is False
    assert result["decision"] == wcp.DECISION_BLOCKED
    assert any("repo_read_unavailable" in reason for reason in result["reasons"])


# --- P1-2: --workflow-profile must not bootstrap via an unverified PATH uv -


def test_workflow_profile_dispatch_does_not_invoke_path_uv(tmp_path):
    """GIVEN the `--workflow-profile` branch of `preflight.sh`
    WHEN it dispatches to `workflow_capability_preflight.py`
    THEN the dispatch must launch the Python module directly (system
    `python3`), never a PATH-resolved `uv run --locked ...` bootstrap step
    -- the module performs its OWN trusted-uv judgment internally, so
    executing an unverified PATH `uv` first would run untrusted code before
    that judgment even happens (P1-2 fix)."""

    fake_bin_dir = tmp_path / "fakebin"
    fake_bin_dir.mkdir()
    # A `uv` on PATH that, if invoked at all by the dispatcher, records the
    # fact by writing a sentinel file -- proving whether it was executed.
    sentinel = tmp_path / "uv_was_invoked.sentinel"
    fake_uv = fake_bin_dir / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
    fake_uv.chmod(0o755)

    child_env = dict(os.environ)
    child_env["PATH"] = str(fake_bin_dir) + os.pathsep + child_env.get("PATH", "")

    proc = subprocess.run(
        ["sh", str(PREFLIGHT_SH), "--workflow-profile", "issue-to-impl"],
        capture_output=True,
        text=True,
        timeout=60,
        env=child_env,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["schema"] == "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1"
    assert not sentinel.exists(), "the fake PATH `uv` must never be invoked by the dispatcher"


# --- P1-3: malformed --planned-operations-json must fail closed, not [] ---


def _run_with_planned_operations_file(planned_ops_path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS_DIR / "workflow_capability_preflight.py"),
            "--profile",
            "issue-to-impl",
            "--repo",
            _DEFAULT_REPO,
            "--planned-operations-json",
            str(planned_ops_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_planned_operations_missing_file_fails_closed(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    proc = _run_with_planned_operations_file(missing_path)
    assert proc.returncode == 2
    error_payload = json.loads(proc.stderr)
    assert error_payload["error"] == "invalid_planned_operations_input"


def test_planned_operations_invalid_json_fails_closed(tmp_path):
    bad_json_path = tmp_path / "bad.json"
    bad_json_path.write_text("{not valid json", encoding="utf-8")
    proc = _run_with_planned_operations_file(bad_json_path)
    assert proc.returncode == 2
    error_payload = json.loads(proc.stderr)
    assert error_payload["error"] == "invalid_planned_operations_input"


def test_planned_operations_malformed_entry_fails_closed(tmp_path):
    malformed_path = tmp_path / "malformed.json"
    malformed_path.write_text(json.dumps([{"phase": "audit"}]), encoding="utf-8")
    proc = _run_with_planned_operations_file(malformed_path)
    assert proc.returncode == 2
    error_payload = json.loads(proc.stderr)
    assert error_payload["error"] == "invalid_planned_operations_input"


def test_planned_operations_not_a_list_fails_closed(tmp_path):
    not_a_list_path = tmp_path / "not_a_list.json"
    not_a_list_path.write_text(json.dumps({"operation": "issue_comment"}), encoding="utf-8")
    proc = _run_with_planned_operations_file(not_a_list_path)
    assert proc.returncode == 2
    error_payload = json.loads(proc.stderr)
    assert error_payload["error"] == "invalid_planned_operations_input"


def test_planned_operations_omitted_is_still_valid_empty_list():
    """Sanity check that omitting `--planned-operations-json` entirely
    (the legitimate no-planned-mutations case) is NOT treated as invalid
    input -- only an explicitly supplied but malformed file is fail-closed
    (P1-3 scope)."""

    proc = _run_preflight_cli("issue-to-impl")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["checks"]["github"]["operations"] == {}
