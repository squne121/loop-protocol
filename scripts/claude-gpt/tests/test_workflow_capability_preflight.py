"""scripts/claude-gpt/tests/test_workflow_capability_preflight.py

Issue #2273: `--workflow-profile issue-to-impl` workflow capability
preflight. Covers AC1-AC11, AC13, AC14 (AC12 lives in
`scripts/agent-guards/tests/test_trusted_runtime_capability_preflight.py`;
AC15/AC16 live in
`.claude/skills/issue-refinement-loop/scripts/tests/test_root_entry_router_workflow_capability.py`).
"""

from __future__ import annotations

import json
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
    assert set(result.keys()) == {"schema", "profile", "decision", "checks", "reasons"}
    assert set(result["checks"].keys()) == {"uv", "spark", "github"}
    assert set(result["checks"]["uv"].keys()) == {"status", "reason"}
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
        lambda: {"binary_available": False, "chatgpt_auth": {"available": False}},
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
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
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
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
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
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
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
