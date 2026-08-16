"""Regression coverage for Issue #1997 temp-root cleanup evidence."""

from __future__ import annotations

import argparse
import errno
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts" / "run_agy_permission_boundary_e2e.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_boundary_runner_cleanup_retry_test", RUNNER)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _failure(error_number: int) -> OSError:
    return OSError(error_number, os.strerror(error_number))


def test_rmtree_onexc_preserves_first_failure_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "temporary-root"
    root.mkdir()
    original_rmtree = MODULE.shutil.rmtree
    calls = 0

    def controlled_rmtree(path: Path, *, onexc: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            onexc(os.rmdir, str(root / "credential-looking-name"), _failure(errno.ENOTEMPTY))  # type: ignore[operator]
        original_rmtree(path)

    monkeypatch.setattr(MODULE.shutil, "rmtree", controlled_rmtree)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )

    evidence = MODULE._remove_temporary_tree_with_evidence(root)

    assert evidence["initial_result"] == "failure"
    assert evidence["initial_exception_type"] == "OSError"
    assert evidence["initial_errno"] == errno.ENOTEMPTY
    assert evidence["initial_errno_name"] == "ENOTEMPTY"
    assert evidence["initial_operation"] == "rmdir"
    assert evidence["path_class"] == "descendant"
    assert evidence["relative_path_digest"].startswith("sha256:")
    assert evidence["retry_count"] == 1
    assert evidence["retry_result"] == "success"
    assert evidence["final_cleanup_verdict"] == "removed"
    persisted = json.dumps(evidence, sort_keys=True)
    assert str(root) not in persisted
    assert "credential-looking-name" not in persisted


def test_retry_policy_is_single_and_errno_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "temporary-root"
    root.mkdir()
    original_rmtree = MODULE.shutil.rmtree
    calls = 0

    def retry_once(path: Path, *, onexc: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]
        original_rmtree(path)

    monkeypatch.setattr(MODULE.shutil, "rmtree", retry_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )
    eligible = MODULE._remove_temporary_tree_with_evidence(root)
    assert calls == 2
    assert eligible["retry_eligible"] is True
    assert eligible["retry_count"] == 1

    blocked_root = tmp_path / "permission-root"
    blocked_root.mkdir()
    calls = 0

    def no_retry(path: Path, *, onexc: object) -> None:
        nonlocal calls
        calls += 1
        onexc(os.rmdir, str(blocked_root), _failure(errno.EACCES))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", no_retry)
    blocked = MODULE._remove_temporary_tree_with_evidence(blocked_root)
    assert calls == 1
    assert blocked["retry_eligible"] is False
    assert blocked["retry_count"] == 0
    assert blocked["retry_result"] == "not_run"
    assert blocked["final_cleanup_verdict"] == "failed"


def test_observation_only_mode_never_retries_an_otherwise_eligible_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "temporary-root"
    root.mkdir()
    calls = 0

    def fail_once(path: Path, *, onexc: object) -> None:
        nonlocal calls
        calls += 1
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )
    evidence = MODULE._remove_temporary_tree_with_evidence(root, retry_enabled=False)
    assert calls == 1
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0


def test_retry_policy_mode_distinguishes_phase_a_from_phase_b_with_zero_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact must not infer Phase A from ``retry_count == 0`` alone."""
    root = tmp_path / "temporary-root"
    root.mkdir()

    def fail_once(path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )
    phase_a = MODULE._remove_temporary_tree_with_evidence(root, retry_enabled=False)

    phase_b_root = tmp_path / "phase-b-temporary-root"
    phase_b_root.mkdir()

    def non_retryable_failure(path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(phase_b_root), _failure(errno.EACCES))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", non_retryable_failure)
    phase_b = MODULE._remove_temporary_tree_with_evidence(phase_b_root, retry_enabled=True)

    assert phase_a["retry_count"] == phase_b["retry_count"] == 0
    assert phase_a["retry_policy_mode"] == "observation_only"
    assert phase_b["retry_policy_mode"] == "single_retry_enabled"


def test_proc_holder_fd_scan_limit_is_small_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("/safe-temporary-root")
    proc = Path("/proc")
    process_dir = proc / "431"
    fd_dir = process_dir / "fd"

    def fake_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == proc:
            return iter([process_dir])
        if path == fd_dir:
            return iter(fd_dir / str(index) for index in range(MODULE._MAX_PROC_FDS_PER_PROCESS))
        raise AssertionError(f"unexpected directory observation: {path}")

    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(os, "readlink", lambda _path: "/unrelated")

    assert MODULE._observe_proc_holders(root) == "unsupported"


def test_proc_holder_fd_read_error_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path("/safe-temporary-root")
    proc = Path("/proc")
    process_dir = proc / "431"
    fd_dir = process_dir / "fd"

    def fake_iterdir(path: Path):  # type: ignore[no-untyped-def]
        if path == proc:
            return iter([process_dir])
        if path == fd_dir:
            return iter([fd_dir / "0"])
        raise AssertionError(f"unexpected directory observation: {path}")

    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    def fail_readlink(_path: Path) -> str:
        raise OSError(errno.EIO, "injected read error")

    monkeypatch.setattr(os, "readlink", fail_readlink)

    assert MODULE._observe_proc_holders(root) == "error"


def test_busy_or_unsupported_holder_observation_never_authorizes_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "temporary-root"
    root.mkdir()

    def fail_once(path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "busy"},
    )
    evidence = MODULE._remove_temporary_tree_with_evidence(root)
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0


def test_final_postcondition_requires_lstat_enoent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "temporary-root"
    root.mkdir()

    monkeypatch.setattr(MODULE.shutil, "rmtree", lambda *_args, **_kwargs: None)
    evidence = MODULE._remove_temporary_tree_with_evidence(root)

    assert evidence["initial_result"] == "success"
    assert evidence["postcondition_absent"] is False
    assert evidence["final_cleanup_verdict"] == "failed"


def test_cleanup_schema_additive_provenance_contract(tmp_path: Path) -> None:
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=tmp_path)
    artifact["cleanup"]["temporary_tree_removal"] = {
        "initial_result": "failure",
        "initial_exception_type": "OSError",
        "initial_errno": errno.ENOTEMPTY,
        "initial_errno_name": "ENOTEMPTY",
        "initial_operation": "rmdir",
        "path_class": "root",
        "relative_path_digest": "sha256:" + "0" * 64,
        "residual_observation": {
            "status": "complete",
            "entry_count": 0,
            "holder_status": "absent",
        },
        "retry_policy_mode": "single_retry_enabled",
        "retry_eligible": True,
        "retry_count": 1,
        "retry_result": "success",
        "final_cleanup_verdict": "removed",
        "postcondition_absent": True,
    }
    artifact["cleanup"]["temporary_processes_removed"] = True
    assert MODULE._schema_errors(artifact) == []


def test_setsid_escape_is_not_proven_absent_by_original_pgid(tmp_path: Path) -> None:
    script = tmp_path / "escaped-child-agy.py"
    child_pid = tmp_path / "escaped-child.pid"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        "pid_file = os.path.join(os.getcwd(), 'escaped-child.pid')\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import os, time; os.setsid(); time.sleep(30)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "open(pid_file, 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(0.1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    context = tmp_path / "context.json"
    context.write_text("{}", encoding="utf-8")
    runtime = {
        "env": {"PATH": os.environ.get("PATH", "")},
        "context_path": context,
        "agy_command_prefix": [sys.executable],
        "workspace": tmp_path,
    }

    result = MODULE._invoke(script, runtime, live=False)
    assert result["descendant_processes_absent"] is True
    pid = int(child_pid.read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    except ProcessLookupError as exc:  # pragma: no cover - regression signal
        pytest.fail(f"setsid child exited unexpectedly: {exc}")
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_remove_tree_evidence_records_retry_mode_for_successful_phase_b_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v1 addition remains optional for success-only legacy artifacts."""
    fake = tmp_path / "fake-agy"
    fake.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(MODULE, "_invoke", lambda *_args, **_kwargs: {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "process_group_isolated": True,
        "descendant_processes_absent": True,
    })
    exit_code, artifact = MODULE._run(
        argparse.Namespace(mode="hermetic", agy=str(fake), allow_live=False, profile="no_tools", artifact_dir=tmp_path)
    )
    assert exit_code == 1
    removal = artifact["cleanup"]["temporary_tree_removal"]
    assert removal["initial_result"] == "success"
    assert removal["retry_count"] == 0
    assert removal["retry_policy_mode"] == "single_retry_enabled"

    phase_a_dir = tmp_path / "phase-a"
    phase_a_dir.mkdir()
    _, phase_a_artifact = MODULE._run(
        argparse.Namespace(
            mode="hermetic",
            agy=str(fake),
            allow_live=False,
            profile="no_tools",
            artifact_dir=phase_a_dir,
            cleanup_observation_only=True,
        )
    )
    phase_a_removal = phase_a_artifact["cleanup"]["temporary_tree_removal"]
    assert phase_a_removal["retry_count"] == removal["retry_count"] == 0
    assert phase_a_removal["retry_policy_mode"] == "observation_only"
