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
        "producer_contract_revision": MODULE.PRODUCER_CONTRACT_REVISION,
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



# ---------------------------------------------------------------------------
# Issue #1997 fix_delta regression coverage (OWNER review issuecomment-5307195963)
# ---------------------------------------------------------------------------


def test_top_level_enoent_is_a_self_consistent_removed_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P0-1: a root that is already gone before rmtree even starts (top-level
    ENOENT, propagated to the caller rather than routed through ``onexc``)
    must produce a ledger that agrees with its own ``postcondition_absent``:
    the goal (no temp root) is met, so this is a "removed" outcome even
    though no delete operation of ours "succeeded"."""
    root = tmp_path / "temporary-root"  # deliberately never created

    def raise_enoent(_path: Path, *, onexc: object) -> None:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))

    monkeypatch.setattr(MODULE.shutil, "rmtree", raise_enoent)

    evidence = MODULE._remove_temporary_tree_with_evidence(root)

    assert evidence["initial_result"] == "failure"
    assert evidence["initial_errno"] == errno.ENOENT
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0
    assert evidence["postcondition_absent"] is True
    assert evidence["final_cleanup_verdict"] == "removed"


def test_validator_rejects_postcondition_absent_without_removed_verdict(tmp_path: Path) -> None:
    """P1-1: the validator must enforce the postcondition<->verdict
    equivalence in both directions, not just reject `removed` without
    ENOENT."""
    artifact = MODULE._unavailable_artifact(MODULE.FAILURE_UNAVAILABLE, profile="no_tools", artifact_dir=tmp_path)
    artifact["cleanup"]["temporary_tree_removal"] = {
        "initial_result": "failure",
        "initial_exception_type": "OSError",
        "initial_errno": errno.ENOENT,
        "initial_errno_name": "ENOENT",
        "initial_operation": "rmtree",
        "path_class": "root",
        "relative_path_digest": "sha256:" + "0" * 64,
        "residual_observation": {"status": "not_run", "entry_count": None, "holder_status": "not_checked"},
        "retry_policy_mode": "single_retry_enabled",
        "retry_eligible": False,
        "retry_count": 0,
        "retry_result": "not_run",
        # Inconsistent on purpose: postcondition says absent but the verdict
        # still claims failure.
        "final_cleanup_verdict": "failed",
        "postcondition_absent": True,
        "producer_contract_revision": MODULE.PRODUCER_CONTRACT_REVISION,
    }
    artifact["cleanup"]["temporary_processes_removed"] = True
    valid, reason = MODULE.validate_artifact(artifact)
    assert valid is False
    # The schema-level allOf bidirectional equivalence (added alongside this
    # fix_delta) rejects the inconsistent state before the Python cross-field
    # check ever runs -- defense in depth working as intended.
    assert reason == "draft202012_invalid"


def test_retry_failure_after_eligible_attempt_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-2: an eligible retry that ALSO fails must record
    `retry_result: failure`, keep `final_cleanup_verdict: failed`, and never
    discard the original failure provenance."""
    root = tmp_path / "temporary-root"
    root.mkdir()
    calls = 0

    def always_fail(_path: Path, *, onexc: object) -> None:
        nonlocal calls
        calls += 1
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", always_fail)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )
    evidence = MODULE._remove_temporary_tree_with_evidence(root)

    assert calls == 2
    assert evidence["retry_eligible"] is True
    assert evidence["retry_count"] == 1
    assert evidence["retry_result"] == "failure"
    assert evidence["final_cleanup_verdict"] == "failed"
    assert evidence["postcondition_absent"] is False
    assert evidence["initial_errno"] == errno.ENOTEMPTY


def test_entry_count_positive_blocks_retry_even_with_absent_holder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-2: `ENOTEMPTY` genuinely means non-empty. A positive `entry_count`
    must block retry regardless of `holder_status`, since no-matching-FD-observed
    is not proof the directory is actually empty."""
    root = tmp_path / "temporary-root"
    root.mkdir()

    def fail_once(_path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 3, "holder_status": "absent"},
    )
    evidence = MODULE._remove_temporary_tree_with_evidence(root)
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0


def test_root_identity_replacement_between_failure_and_decision_blocks_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-2: if *root* is replaced (rename / symlink substitution / recreation
    by another actor) between the initial failure and the eligibility
    decision, retry must never be authorized against the new object."""
    root = tmp_path / "temporary-root"
    root.mkdir()

    def fail_once(_path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        lambda _root: {"status": "complete", "entry_count": 0, "holder_status": "absent"},
    )
    identity_calls = {"count": 0}

    def flaky_identity(_root: Path) -> tuple[int, int] | None:
        identity_calls["count"] += 1
        return (1, 100) if identity_calls["count"] == 1 else (1, 999)

    monkeypatch.setattr(MODULE, "_root_identity", flaky_identity)
    evidence = MODULE._remove_temporary_tree_with_evidence(root)
    assert identity_calls["count"] == 2
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0


def test_root_removed_by_another_actor_before_retry_decision_is_still_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-1/P0-2: if some other actor removes *root* between our initial
    failure and the retry decision, we must not blindly retry against a
    vanished target -- but the final ledger must still honestly report
    "removed", since the goal (no temp root) was actually achieved."""
    root = tmp_path / "temporary-root"
    root.mkdir()

    def fail_once(_path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]

    def observe_then_vanish(_root: Path) -> dict[str, object]:
        # Simulate another actor removing root during our bounded, read-only
        # residual observation window -- i.e. strictly between the two
        # `_root_identity()` captures in `_remove_temporary_tree_with_evidence`.
        root.rmdir()
        return {"status": "complete", "entry_count": 0, "holder_status": "absent"}

    monkeypatch.setattr(MODULE.shutil, "rmtree", fail_once)
    monkeypatch.setattr(MODULE, "_observe_temporary_residual", observe_then_vanish)
    evidence = MODULE._remove_temporary_tree_with_evidence(root)
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0
    assert evidence["postcondition_absent"] is True
    assert evidence["final_cleanup_verdict"] == "removed"


def test_failure_artifact_preserves_full_prior_cleanup_ledger_losslessly(tmp_path: Path) -> None:
    """P0-1 #4: when `main()` replaces a result via `_failure_artifact()`
    (e.g. after a validator rejection), the real cleanup ledger -- including
    `temporary_tree_removal` -- must survive verbatim rather than being
    silently reconstructed (and potentially fabricated) from two booleans."""
    prior_cleanup = {
        "temporary_processes_removed": False,
        "loopback_servers_stopped": True,
        "process_group_isolated": True,
        "descendant_processes_absent": True,
        "temporary_tree_removal": {
            "initial_result": "failure",
            "initial_exception_type": "OSError",
            "initial_errno": errno.EACCES,
            "initial_errno_name": "EACCES",
            "initial_operation": "rmdir",
            "path_class": "root",
            "relative_path_digest": "sha256:" + "1" * 64,
            "residual_observation": {"status": "not_run", "entry_count": None, "holder_status": "not_checked"},
            "retry_policy_mode": "single_retry_enabled",
            "retry_eligible": False,
            "retry_count": 0,
            "retry_result": "not_run",
            "final_cleanup_verdict": "failed",
            "postcondition_absent": False,
            "producer_contract_revision": MODULE.PRODUCER_CONTRACT_REVISION,
        },
    }
    prior_result = {"cleanup": prior_cleanup}
    artifact = MODULE._failure_artifact(
        "agy_permission_boundary_validator_exception",
        profile="no_tools",
        artifact_dir=tmp_path,
        prior_result=prior_result,
    )
    assert artifact["cleanup"] == prior_cleanup


def test_setsid_escaped_child_recreates_root_content_after_initial_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5/P0-2: an escaped `setsid()` child that is still writing into the
    temp root after the initial rmtree failure must never be authorized for
    retry solely because `holder_status: absent` (no *matching* FD observed)
    -- deterministically modeled here (rather than as a true OS-level race)
    since the same escaped-child semantics are already exercised live by
    `test_setsid_escape_is_not_proven_absent_by_original_pgid`; this test
    isolates the retry-authorization predicate itself."""
    root = tmp_path / "temporary-root"
    root.mkdir()

    def escaped_child_recreates_content(_path: Path, *, onexc: object) -> None:
        onexc(os.rmdir, str(root), _failure(errno.ENOTEMPTY))  # type: ignore[operator]
        # The escaped child (already outside our process group -- see
        # test_setsid_escape_is_not_proven_absent_by_original_pgid) is still
        # alive and recreates a file in root right after our first attempt.
        (root / "still-alive-marker").write_text("x", encoding="utf-8")

    monkeypatch.setattr(MODULE.shutil, "rmtree", escaped_child_recreates_content)
    monkeypatch.setattr(
        MODULE,
        "_observe_temporary_residual",
        # The escaped child holds no FD we can observe via /proc matching,
        # but the residual entry it just created is real.
        lambda _root: {"status": "complete", "entry_count": 1, "holder_status": "absent"},
    )
    evidence = MODULE._remove_temporary_tree_with_evidence(root)
    assert evidence["retry_eligible"] is False
    assert evidence["retry_count"] == 0
    assert evidence["final_cleanup_verdict"] == "failed"
