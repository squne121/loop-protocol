"""Issue #1979 AC7: process-group cleanup evidence, separate from directory cleanup."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner_cleanup_test", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_verify_process_group_absent_true_for_exited_group() -> None:
    """A process group whose leader already exited must be confirmed absent."""
    process = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        start_new_session=True,
    )
    process.wait(timeout=10)
    assert MODULE._verify_process_group_absent(process.pid) is True


def test_verify_process_group_absent_false_while_group_alive() -> None:
    """A still-running process group leader must never be reported absent."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        start_new_session=True,
    )
    try:
        assert MODULE._verify_process_group_absent(process.pid) is False
    finally:
        try:
            os.killpg(process.pid, MODULE.signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=10)


def test_invoke_starts_its_own_process_group_and_reports_descendant_absence(tmp_path: Path) -> None:
    """Issue #1979 AC7: `_invoke` isolates the call in its own process group
    (`start_new_session=True`) and, once the call returns, verifies and
    reports that no process remains in that group -- independent of
    directory-based cleanup.
    """
    script = tmp_path / "fake-agy.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('stdout-ok')\nsys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    runtime = {
        "env": {"PATH": os.environ.get("PATH", "")},
        "context_path": tmp_path / "context.json",
        "agy_command_prefix": [sys.executable],
        "workspace": tmp_path,
    }
    (tmp_path / "context.json").write_text("{}", encoding="utf-8")

    result = MODULE._invoke(script, runtime, live=False)

    assert result["process_group_isolated"] is True
    assert result["descendant_processes_absent"] is True
    assert result["timed_out"] is False
    assert result["returncode"] == 0


def test_invoke_timeout_kills_process_group_and_reports_timed_out(tmp_path: Path) -> None:
    """A run that exceeds its timeout must have its process group force-killed
    and reported as `timed_out`, never silently treated as a clean exit.
    """
    script = tmp_path / "slow-agy.py"
    script.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    runtime = {
        "env": {"PATH": os.environ.get("PATH", "")},
        "context_path": tmp_path / "context.json",
        "agy_command_prefix": [sys.executable],
        "workspace": tmp_path,
    }
    (tmp_path / "context.json").write_text("{}", encoding="utf-8")

    original_popen = MODULE.subprocess.Popen

    class _FastTimeoutPopen(original_popen):  # type: ignore[misc,valid-type]
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return super().communicate(timeout=0.2)

    MODULE.subprocess.Popen = _FastTimeoutPopen
    try:
        result = MODULE._invoke(script, runtime, live=False)
    finally:
        MODULE.subprocess.Popen = original_popen

    assert result["timed_out"] is True
    assert result["returncode"] is None
    assert result["descendant_processes_absent"] is True


def test_cleanup_evidence_is_separate_predicate_from_directory_cleanup() -> None:
    schema_props = MODULE.SCHEMA_PATH.read_text(encoding="utf-8")
    assert "process_group_isolated" in schema_props
    assert "descendant_processes_absent" in schema_props
    assert "temporary_processes_removed" in schema_props

    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools")
    cleanup = artifact["cleanup"]
    assert set(cleanup) == {
        "temporary_processes_removed",
        "loopback_servers_stopped",
        "process_group_isolated",
        "descendant_processes_absent",
    }
