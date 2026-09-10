"""Regression coverage for Issue #2611's exact child-gh auth boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import run_scope_rollup_preflight as rsrp  # noqa: E402


def _load_runtime_canary():
    path = REPO_ROOT / "scripts" / "agent-guards" / "verify_scope_rollup_auth_capability_runtime.py"
    spec = importlib.util.spec_from_file_location("scope_rollup_auth_runtime_canary", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fake_gh(tmp_path: Path, *, graphql_partial_error: bool = False) -> tuple[Path, Path, Path]:
    """Create a config-only fake gh that records only nonsecret provenance."""
    selected_config = tmp_path / "selected-config"
    selected_config.mkdir()
    marker = tmp_path / "calls.jsonl"
    fake_gh = tmp_path / "gh"
    graphql_response = (
        '{"data":{"repository":{}},"errors":[{"message":"fixture partial error"}]}'
        if graphql_partial_error
        else '{"data":{"viewer":{"login":"fixture-user"}},"fixture_provenance":{"carrier_source":"gh_config_dir","carrier_path_match":true}}'
    )
    fake_gh.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

expected_config = Path(__file__).with_name("selected-config")
marker = Path(__file__).with_name("calls.jsonl")
args = sys.argv[1:]
kind = "auth_status" if args[:2] == ["auth", "status"] else ("issue_view" if args[:2] == ["issue", "view"] else ("graphql" if args[:2] == ["api", "graphql"] else "other"))
marker.write_text(marker.read_text() + json.dumps({{"path": kind, "carrier_source": "gh_config_dir", "carrier_path_match": os.environ.get("GH_CONFIG_DIR") == str(expected_config), "tokens_unset": "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ}}) + "\\n" if marker.exists() else json.dumps({{"path": kind, "carrier_source": "gh_config_dir", "carrier_path_match": os.environ.get("GH_CONFIG_DIR") == str(expected_config), "tokens_unset": "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ}}) + "\\n")
if kind != "auth_status" and ("GH_TOKEN" in os.environ or "GITHUB_TOKEN" in os.environ):
    sys.stderr.write("fixture token carrier leak")
    raise SystemExit(9)
if kind != "auth_status" and os.environ.get("GH_CONFIG_DIR") != str(expected_config):
    sys.stderr.write("fixture authentication failure")
    raise SystemExit(1)
if kind == "auth_status":
    print("authenticated")
elif kind == "issue_view":
    print(json.dumps({{"number": 2611, "fixture_provenance": {{"carrier_source": "gh_config_dir", "carrier_path_match": True}}}}))
elif kind == "graphql":
    print({graphql_response!r})
else:
    sys.stderr.write("unexpected fixture command")
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    return fake_gh, selected_config, marker


@pytest.fixture
def isolated_config_only_env(monkeypatch, tmp_path):
    fake_gh, selected_config, marker = _write_fake_gh(tmp_path)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    for key, value in {
        "HOME": str(isolated_home),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "GH_CONFIG_DIR": str(selected_config),
        "GH_HOST": "untrusted.example.invalid",
        "GH_REPO": "untrusted/example",
        "GH_DEBUG": "api",
        "GH_PAGER": "untrusted-pager",
        "PAGER": "untrusted-pager",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return fake_gh, selected_config, marker


def test_child_preserves_launcher_selected_config_carrier(isolated_config_only_env):
    """GIVEN config-only caller auth WHEN child env is sanitized THEN only gh retains it."""
    _fake_gh, selected_config, _marker = isolated_config_only_env

    gh_env = rsrp._sanitized_gh_env()
    child_env = rsrp._sanitized_child_env(str(REPO_ROOT))

    assert gh_env["GH_CONFIG_DIR"] == str(selected_config)
    assert gh_env["GH_HOST"] == "github.com"
    assert "GH_REPO" not in gh_env
    assert "GH_DEBUG" not in gh_env
    assert "GH_PAGER" not in gh_env
    assert "PAGER" not in gh_env
    assert "GH_TOKEN" not in gh_env
    assert "GITHUB_TOKEN" not in gh_env
    assert "GH_CONFIG_DIR" not in child_env
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env


def test_actual_process_runtime_canary_requires_direct_graphql_provenance_without_fallback(
    monkeypatch, capsys, isolated_config_only_env, tmp_path
):
    """GIVEN config-only auth WHEN canary runs THEN issue and direct GraphQL use the real child boundary."""
    fake_gh, selected_config, marker = isolated_config_only_env
    monkeypatch.chdir(tmp_path)
    canary = _load_runtime_canary()
    monkeypatch.setattr(canary.rsrp, "_resolve_trusted_gh_binary", lambda _root: str(fake_gh))

    exit_code = canary.main(["--repo", "squne121/loop-protocol", "--issue-number", "2611"])
    output = capsys.readouterr().out
    calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    artifacts = list((tmp_path / "artifacts").glob("runtime-verification-AC4-*.log"))

    assert exit_code == 0
    assert "verdict=PASS" in output
    assert "execution_path=direct_graphql" in output
    assert "fallback=false" in output
    assert "carrier_source=gh_config_dir" in output
    assert "carrier_path_match=true" in output
    assert [call["path"] for call in calls] == ["auth_status", "issue_view", "graphql"]
    assert all(call["carrier_path_match"] for call in calls)
    assert all(call["tokens_unset"] for call in calls)
    assert len(artifacts) == 1
    artifact = artifacts[0].read_text(encoding="utf-8")
    assert artifact.startswith("=== Runtime Verification Log ===\nAC: AC4")
    assert "Result: PASS" in artifact
    assert str(selected_config) not in artifact
    assert "SCOPE_ROLLUP_AUTH_CAPABILITY_RUNTIME_V1" not in artifact


def test_missing_config_carrier_is_fixture_authentication_failure(monkeypatch, isolated_config_only_env):
    """GIVEN config-only fixture WHEN the carrier is removed THEN both child paths fail authentication."""
    fake_gh, _selected_config, _marker = isolated_config_only_env
    monkeypatch.delenv("GH_CONFIG_DIR")

    with pytest.raises(rsrp.ScopeRollupPreflightError, match="fixture authentication failure") as issue_error:
        rsrp._fetch_issue_view(str(fake_gh), "squne121/loop-protocol", 2611)
    with pytest.raises(rsrp.ScopeRollupPreflightError, match="fixture authentication failure") as graphql_error:
        rsrp._run_gh_graphql(str(fake_gh), "query { viewer { login } }", {})

    assert issue_error.value.reason_code == "gh_issue_view_failed"
    assert graphql_error.value.reason_code == "gh_graphql_failed"


@pytest.mark.parametrize("unavailable_path", [("issue", "view"), ("api", "graphql")])
def test_given_child_resource_exception_when_canary_runs_then_it_is_supplemental_unavailability(
    monkeypatch, capsys, isolated_config_only_env, unavailable_path, tmp_path
):
    """GIVEN an unavailable child resource WHEN the canary runs THEN it never escapes as failure."""
    fake_gh, _selected_config, _marker = isolated_config_only_env
    monkeypatch.chdir(tmp_path)
    canary = _load_runtime_canary()
    monkeypatch.setattr(canary.rsrp, "_resolve_trusted_gh_binary", lambda _root: str(fake_gh))

    def unavailable_run_gh(_gh_bin, args, **_kwargs):
        if tuple(args[:2]) == unavailable_path:
            raise rsrp.ScopeRollupPreflightError("gh_timeout")
        if args[:2] == ["issue", "view"]:
            return 0, '{"number":2611}', ""
        return 0, '{"data":{"viewer":{"login":"fixture-user"}}}', ""

    monkeypatch.setattr(canary.rsrp, "_run_gh", unavailable_run_gh)

    exit_code = canary.main(["--repo", "squne121/loop-protocol", "--issue-number", "2611"])
    output = capsys.readouterr().out

    assert exit_code == 77
    assert "verdict=UNAVAILABLE/SKIP" in output
    assert "failure_class=child_resource_unavailable" in output
    assert "fallback=false" in output


def test_graphql_non_auth_failures_remain_distinct_and_fail_closed(monkeypatch, capsys, tmp_path):
    """GIVEN a GraphQL partial response WHEN canary runs THEN it is not rewritten as auth or success."""
    fake_gh, selected_config, _marker = _write_fake_gh(tmp_path, graphql_partial_error=True)
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "HOME": str(tmp_path / "isolated-home"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "GH_CONFIG_DIR": str(selected_config),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    canary = _load_runtime_canary()
    monkeypatch.setattr(canary.rsrp, "_resolve_trusted_gh_binary", lambda _root: str(fake_gh))

    exit_code = canary.main(["--repo", "squne121/loop-protocol", "--issue-number", "2611"])
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "verdict=FAIL" in output
    assert "failure_class=graphql_partial_error" in output
    assert "failure_class=authentication_failure" not in output
    assert "fallback=false" in output


def test_runtime_canary_and_regressions_never_expose_secret_or_config_content(
    monkeypatch, capsys, isolated_config_only_env, tmp_path
):
    """GIVEN secret-like ambient values WHEN regression runs THEN its diagnostic has only nonsecret provenance."""
    fake_gh, selected_config, _marker = isolated_config_only_env
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "ambient-token-must-not-reach-child")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token-must-not-reach-child")
    canary = _load_runtime_canary()
    monkeypatch.setattr(canary.rsrp, "_resolve_trusted_gh_binary", lambda _root: str(fake_gh))

    exit_code = canary.main(["--repo", "squne121/loop-protocol", "--issue-number", "2611"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "ambient-token-must-not-reach-child" not in output
    assert "ambient-github-token-must-not-reach-child" not in output
    assert str(selected_config) not in output
    assert str(Path.home()) not in output
