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


def test_checker_rejects_malformed_toml_without_traceback(tmp_path: Path):
    config_path = _write_config(tmp_path, "[features.multi_agent_v2\nenabled = true\n")

    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 1
    assert "malformed TOML" in result.stdout
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


def test_checker_preserves_existing_explicit_spawn_note_assertion(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        """[features.multi_agent_v2]
enabled = true
max_concurrent_threads_per_session = 4
""",
    )

    assert callable(module.assert_explicit_spawn_notes)
    result = _run_checker(config_path, "--assert-project-multi-agent-v2-config")
    assert result.returncode == 0, result.stdout + result.stderr
