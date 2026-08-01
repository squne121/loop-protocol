from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "check_impl_review_loop_codex_dispatch.py"
spec = importlib.util.spec_from_file_location("check_impl_review_loop_codex_dispatch", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def _write_config(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _run_checker(config_path: Path, *assertions: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--config-path",
            str(config_path),
            *assertions,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_accepts_exact_multi_agent_v2_config(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    assert module.assert_project_declares_multi_agent_v2_enabled(config_path) == []
    assert module.assert_no_max_depth_setting(config_path) == []
    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["0", "3", "5", '\"4\"', "true"])
def test_rejects_non_integer_or_non_four_concurrency(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = %s
""" % value,
    )

    failures = module.assert_project_declares_multi_agent_v2_enabled(config_path)
    assert any("max_concurrent_threads_per_session must be strict integer 4" in failure for failure in failures)


@pytest.mark.parametrize("value", ["0", "1", "99", '\"1\"', "true"])
def test_rejects_any_max_depth_setting(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4

[agents]
max_depth = %s
""" % value,
    )

    failures = module.assert_no_max_depth_setting(config_path)
    assert failures == [".codex/config.toml: [agents].max_depth must be absent"]


def test_checker_accepts_injected_config_path(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 0, result.stdout + result.stderr


def test_checker_requires_an_assertion_flag():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert "specify at least one assertion flag" in result.stderr
    assert "OK:" not in result.stdout


def test_checker_rejects_malformed_toml_without_traceback(tmp_path: Path):
    config_path = _write_config(tmp_path, "[features.multi_agent_v2\nenabled = true\n")

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 1
    assert "malformed TOML" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_checker_reports_missing_config_without_traceback(tmp_path: Path):
    config_path = tmp_path / "missing.toml"

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")

    assert result.returncode == 1
    assert "TOML file not found" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("text", "diagnostic"),
    [
        ('features = "string"\n', "[features] must be a table"),
        ("features = 1\n", "[features] must be a table"),
        ('features = ["array"]\n', "[features] must be a table"),
        ('[features]\nmulti_agent_v2 = "string"\n', "[features.multi_agent_v2] must be declared"),
    ],
)
def test_checker_rejects_wrong_shaped_toml_without_traceback(
    tmp_path: Path,
    text: str,
    diagnostic: str,
):
    config_path = _write_config(tmp_path, text)

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")

    assert result.returncode == 1
    assert diagnostic in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_checker_rejects_wrong_shaped_agents_table_without_traceback(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """agents = "string"

[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-no-max-depth-setting")

    assert result.returncode == 1
    assert "[agents] must be a table" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "[features]\nenabled = true\n",
        "[features.multi_agent_v2]\nenabled = false\nmax_concurrent_threads_per_session = 4\n",
        "[features.multi_agent_v2]\nenabled = \"true\"\nmax_concurrent_threads_per_session = 4\n",
        "[features.multi_agent_v2]\nenabled = true\n",
    ],
)
def test_rejects_missing_or_non_strict_v2_enabled_setting(tmp_path: Path, text: str):
    config_path = _write_config(tmp_path, text)

    failures = module.assert_project_declares_multi_agent_v2_enabled(config_path)
    assert failures


DISPATCH_SITE = ".claude/skills/impl-review-loop/steps/step-1-implementation.md"
DISPATCH_SITES = {
    DISPATCH_SITE: {
        "task_name": "implementation_i0",
        "agent_type": "implementation-worker",
    }
}


def _write_dispatch_site(tmp_path: Path, text: str) -> None:
    path = tmp_path / DISPATCH_SITE
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")


def _valid_dispatch_block() -> str:
    return """```yaml
spawn_agent:
  task_name: implementation_i0
  agent_type: implementation-worker
  fork_turns: none
  message: |
    Objective: implement the linked Issue.
    Live reference: the current linked Issue.
    Bounded scope: the live Allowed Paths only.
    Expected result: IMPLEMENT_RESULT_V1.
```
"""


def test_given_current_repository_when_native_v2_dispatch_contract_runs_then_it_passes():
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--assert-native-v2-dispatch-contract"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_given_current_repository_when_explicit_spawn_alias_runs_then_it_passes(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    result = _run_checker(config_path, "--assert-explicit-spawn-notes")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("replacement", "diagnostic"),
    [
        ("agent_type: test-runner", "agent_type must be 'implementation-worker'"),
        ("fork_turns: none", "fork_turns must be 'none'"),
        ("task_name: implementation_i0", "task_name must be 'implementation_i0'"),
    ],
)
def test_given_invalid_dispatch_field_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    replacement: str,
    diagnostic: str,
):
    block = _valid_dispatch_block()
    if diagnostic == "fork_turns must be 'none'":
        block = block.replace(replacement, "fork_turns: all")
    elif diagnostic == "task_name must be 'implementation_i0'":
        block = block.replace(replacement, "task_name: implementation-i0")
    else:
        block = block.replace("agent_type: implementation-worker", replacement)
    _write_dispatch_site(tmp_path, block)

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any(diagnostic in failure for failure in failures)


def test_given_missing_fork_turns_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(tmp_path, _valid_dispatch_block().replace("  fork_turns: none\n", ""))

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("fork_turns must be 'none'" in failure for failure in failures)


@pytest.mark.parametrize("element", ["Objective", "Live reference", "Bounded scope", "Expected result"])
def test_given_missing_required_message_element_when_static_contract_runs_then_it_rejects(
    tmp_path: Path,
    element: str,
):
    block = "\n".join(
        line
        for line in _valid_dispatch_block().splitlines()
        if not line.startswith(f"    {element}:")
    )
    _write_dispatch_site(tmp_path, f"{block}\n")

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any(f"message must include non-empty '{element}:'" in failure for failure in failures)


def test_given_duplicate_dispatch_blocks_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(tmp_path, _valid_dispatch_block() * 2)

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("expected exactly one V2 dispatch block, found 2" in failure for failure in failures)


def test_given_legacy_phrase_only_when_static_contract_runs_then_it_rejects(tmp_path: Path):
    _write_dispatch_site(
        tmp_path,
        "Codex CLI: spawn the custom agent named implementation-worker for this step; the root thread must not.\n",
    )

    failures: list[str] = []
    module.assert_native_v2_dispatch_contract(
        failures,
        repo_root=tmp_path,
        dispatch_sites=DISPATCH_SITES,
    )

    assert any("expected exactly one V2 dispatch block, found 0" in failure for failure in failures)


def test_accepts_documented_v1_rollback_config(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = false
max_concurrent_threads_per_session = 4

[agents]
max_depth = 1
""",
    )

    assert module.assert_project_declares_multi_agent_v1_config(config_path) == []
    result = _run_checker(config_path, "--assert-project-multi-agent-v1-config")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("value", ["0", "2", '"1"', "true"])
def test_rejects_non_strict_v1_max_depth(tmp_path: Path, value: str):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = false

[agents]
max_depth = %s
""" % value,
    )

    failures = module.assert_project_declares_multi_agent_v1_config(config_path)
    assert any("max_depth must be strict integer 1" in failure for failure in failures)
