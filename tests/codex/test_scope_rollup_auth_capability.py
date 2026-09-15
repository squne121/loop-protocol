"""Regression coverage for Issue #2611's exact child-gh auth boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import run_scope_rollup_preflight as rsrp  # noqa: E402


def _write_fake_gh(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an auth fixture that records only nonsecret carrier facts."""
    selected_config = tmp_path / "selected-config"
    selected_config.mkdir()
    marker = tmp_path / "calls.jsonl"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

expected_config = Path(__file__).with_name("selected-config")
marker = Path(__file__).with_name("calls.jsonl")
args = sys.argv[1:]
kind = "issue_view" if args[:2] == ["issue", "view"] else ("graphql" if args[:2] == ["api", "graphql"] else "other")
record = {{
    "path": kind,
    "config_preserved": os.environ.get("GH_CONFIG_DIR") == str(expected_config),
    "gh_token_present": "GH_TOKEN" in os.environ,
    "github_token_present": "GITHUB_TOKEN" in os.environ,
    "host_pinned": os.environ.get("GH_HOST") == "github.com",
    "ambient_values_scrubbed": all(key not in os.environ for key in ("GH_REPO", "GH_DEBUG", "GH_PAGER", "PAGER")),
}}
with marker.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True) + "\\n")
if kind == "other":
    sys.stderr.write("unexpected fixture command")
    raise SystemExit(2)
if not record["config_preserved"] and not (record["gh_token_present"] or record["github_token_present"]):
    sys.stderr.write("fixture authentication failure")
    raise SystemExit(1)
if kind == "issue_view":
    print(json.dumps({{"number": 2611}}))
else:
    print(json.dumps({{"data": {{"viewer": {{"login": "fixture-user"}}}}}}))
""",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    return fake_gh, selected_config, marker


@pytest.fixture
def isolated_config_only_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Build the only fixture that deliberately unsets both token carriers."""
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


def test_given_gh_child_when_sanitized_then_selected_config_and_token_carriers_are_retained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """GIVEN caller authentication WHEN gh runs THEN only its exact child keeps it."""
    _fake_gh, selected_config, _marker = _write_fake_gh(tmp_path)
    monkeypatch.setenv("GH_CONFIG_DIR", str(selected_config))
    monkeypatch.setenv("GH_TOKEN", "test-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")
    monkeypatch.setenv("GH_REPO", "untrusted/example")
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("PAGER", "untrusted-pager")

    gh_env = rsrp._sanitized_gh_env()
    child_env = rsrp._sanitized_child_env(str(REPO_ROOT))

    assert gh_env["GH_CONFIG_DIR"] == str(selected_config)
    assert gh_env["GH_TOKEN"] == "test-gh-token"
    assert gh_env["GITHUB_TOKEN"] == "test-github-token"
    assert gh_env["GH_HOST"] == "github.com"
    assert "GH_REPO" not in gh_env
    assert "GH_DEBUG" not in gh_env
    assert "PAGER" not in gh_env
    assert "GH_CONFIG_DIR" not in child_env
    assert "GH_TOKEN" not in child_env
    assert "GITHUB_TOKEN" not in child_env


def test_given_config_only_auth_when_production_gh_paths_run_then_both_receive_selected_config(
    isolated_config_only_env,
):
    """GIVEN isolated config-only auth WHEN _run_gh runs THEN both real paths authenticate."""
    fake_gh, _selected_config, marker = isolated_config_only_env

    issue, _raw = rsrp._fetch_issue_view(str(fake_gh), "squne121/loop-protocol", 2611)
    graphql = rsrp._run_gh_graphql(str(fake_gh), "query { viewer { login } }", {})
    calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]

    assert issue["number"] == 2611
    assert graphql["data"]["viewer"]["login"] == "fixture-user"
    assert [call["path"] for call in calls] == ["issue_view", "graphql"]
    assert all(call["config_preserved"] for call in calls)
    assert all(not call["gh_token_present"] for call in calls)
    assert all(not call["github_token_present"] for call in calls)
    assert all(call["host_pinned"] and call["ambient_values_scrubbed"] for call in calls)


def test_given_config_only_auth_when_config_is_absent_then_both_paths_fail_authentication(
    monkeypatch: pytest.MonkeyPatch, isolated_config_only_env
):
    """GIVEN config-only auth WHEN its carrier is removed THEN no false success is possible."""
    fake_gh, _selected_config, _marker = isolated_config_only_env
    monkeypatch.delenv("GH_CONFIG_DIR")

    with pytest.raises(rsrp.ScopeRollupPreflightError, match="fixture authentication failure") as issue_error:
        rsrp._fetch_issue_view(str(fake_gh), "squne121/loop-protocol", 2611)
    with pytest.raises(rsrp.ScopeRollupPreflightError, match="fixture authentication failure") as graphql_error:
        rsrp._run_gh_graphql(str(fake_gh), "query { viewer { login } }", {})

    assert issue_error.value.reason_code == "gh_issue_view_failed"
    assert graphql_error.value.reason_code == "gh_graphql_failed"


@pytest.mark.parametrize(
    ("carrier", "other_carrier"),
    [("GH_TOKEN", "GITHUB_TOKEN"), ("GITHUB_TOKEN", "GH_TOKEN")],
)
def test_given_token_only_auth_when_production_gh_paths_run_then_token_fallback_is_retained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, carrier: str, other_carrier: str
):
    """GIVEN either token carrier WHEN _run_gh runs THEN existing token auth remains usable."""
    fake_gh, _selected_config, marker = _write_fake_gh(tmp_path)
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setenv(carrier, "test-token")
    monkeypatch.delenv(other_carrier, raising=False)

    rsrp._fetch_issue_view(str(fake_gh), "squne121/loop-protocol", 2611)
    rsrp._run_gh_graphql(str(fake_gh), "query { viewer { login } }", {})
    calls = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]

    assert len(calls) == 2
    assert all(not call["config_preserved"] for call in calls)
    assert all(call["gh_token_present"] == (carrier == "GH_TOKEN") for call in calls)
    assert all(call["github_token_present"] == (carrier == "GITHUB_TOKEN") for call in calls)


def test_given_python_test_plan_when_scope_rollup_suite_is_registered_then_it_is_exactly_once_and_collectable():
    """GIVEN CI targets WHEN this suite is registered THEN its one target collects successfully."""
    target = "tests/codex/test_scope_rollup_auth_capability.py"
    plan = json.loads((REPO_ROOT / ".github" / "ci" / "python-test-plan.json").read_text(encoding="utf-8"))

    assert plan["targets"].count(target) == 1
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", target],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert collected.returncode == 0, collected.stderr
    assert "test_scope_rollup_auth_capability.py" in collected.stdout
    assert "0 tests collected" not in collected.stdout
