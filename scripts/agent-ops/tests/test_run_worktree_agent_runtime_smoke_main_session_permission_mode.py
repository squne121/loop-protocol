"""Issue #2935: top-level ``permission_mode_observed`` in the runner evidence.

The key is read from the FIRST ``system/init`` stream-json event only, is valid
only for a non-empty string in ``_PERMISSION_MODE_VALUES`` (case-sensitive), and
is otherwise ``null``. It is never backfilled from argv / SubagentStop / any
other surface. Test names: ``observed`` (AC1), ``unobserved`` (AC2),
``runner_help`` (AC5).
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_BASE_PATH = Path(__file__).resolve().parent / "test_run_worktree_agent_runtime_smoke.py"
_BASE_NAME = "_runtime_smoke_base_for_main_session_permission_mode_tests"


def _load_base():
    # Unique module name + sys.modules registration: avoids bare-name cache
    # collisions in a unified pytest session.
    if _BASE_NAME in sys.modules:
        return sys.modules[_BASE_NAME]
    spec = importlib.util.spec_from_file_location(_BASE_NAME, _BASE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_BASE_NAME] = module
    spec.loader.exec_module(module)
    return module


_base = _load_base()
_run = _base._run
_write_fake_exe = _base._write_fake_exe
_prompt_file = _base._prompt_file
_HELP_BRANCH = _base._HELP_BRANCH
_SCRIPT = _base.SCRIPT


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    return _base._build_repo_with_worktree(tmp_path)


def _echo(payload: dict) -> str:
    return f"echo {shlex.quote(json.dumps(payload))}\n"


def _init(**fields: object) -> dict:
    return {"type": "system", "subtype": "init", **fields}


def _fake_body(*events: dict) -> str:
    return (
        "\ncat > /dev/null\n"
        + "".join(_echo(event) for event in events)
        + _echo({"type": "result", "subtype": "success"})
        + "exit 0\n"
    )


def _run_for_evidence(
    repo: Path, worktree: Path, tmp_path: Path, events: list[dict], *extra_args: str
) -> tuple[subprocess.CompletedProcess[str], dict]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + _fake_body(*events))
    evidence_path = tmp_path / "evidence.json"
    result = _run(
        repo,
        worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(_prompt_file(tmp_path)),
        "--output-dir", str(tmp_path / "out"),
        "--evidence-json", str(evidence_path),
        *extra_args,
        fake_bin_dir=fake_bin,
    )
    assert evidence_path.exists(), result.stderr
    return result, json.loads(evidence_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["auto", "default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"])
def test_given_valid_init_permission_mode_when_run_then_top_level_value_observed(
    repo_with_worktree, tmp_path, mode
):
    repo, worktree = repo_with_worktree
    result, evidence = _run_for_evidence(
        repo, worktree, tmp_path, [_init(session_id="s-1", permissionMode=mode)]
    )
    assert result.returncode == 0, result.stderr
    assert evidence["schema"] == "WORKTREE_AGENT_RUNTIME_SMOKE_RESULT_V1"
    assert evidence["permission_mode_observed"] == mode


def test_given_valid_init_without_session_id_when_run_then_top_level_value_observed(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    result, evidence = _run_for_evidence(
        repo, worktree, tmp_path, [_init(permissionMode="auto")]
    )
    assert result.returncode == 0, result.stderr
    assert evidence["permission_mode_observed"] == "auto"


_UNOBSERVED_CASES = [
    pytest.param([], id="no-init"),
    pytest.param([_init(session_id="s-1")], id="init-without-permission-mode"),
    pytest.param([_init(session_id="s-1", permissionMode=None)], id="null"),
    pytest.param([_init(session_id="s-1", permissionMode=1)], id="number"),
    pytest.param([_init(session_id="s-1", permissionMode=True)], id="bool"),
    pytest.param([_init(session_id="s-1", permissionMode={"mode": "auto"})], id="object"),
    pytest.param([_init(session_id="s-1", permissionMode=["auto"])], id="list"),
    pytest.param([_init(session_id="s-1", permissionMode="")], id="empty-string"),
    pytest.param([_init(session_id="s-1", permissionMode="bogus")], id="bogus"),
    pytest.param([_init(session_id="s-1", permissionMode="AUTO")], id="case-mismatch-AUTO"),
    pytest.param(
        [_init(), _init(session_id="s-2", permissionMode="auto")],
        id="first-init-without-session-id-or-mode-then-later-valid-init",
    ),
    pytest.param(
        [_init(session_id="s-1"), _init(session_id="s-2", permissionMode="auto")],
        id="first-init-without-mode-then-later-valid-init",
    ),
    pytest.param(
        [_init(session_id="s-1", permissionMode="bogus"), _init(session_id="s-2", permissionMode="auto")],
        id="first-init-invalid-then-later-valid-init",
    ),
]


@pytest.mark.parametrize("events", _UNOBSERVED_CASES)
def test_given_unobserved_init_permission_mode_when_run_then_top_level_null_and_exit0(
    repo_with_worktree, tmp_path, events
):
    repo, worktree = repo_with_worktree
    result, evidence = _run_for_evidence(repo, worktree, tmp_path, events)
    # An unobserved mode must neither change the exit code nor add an exit-77 path.
    assert result.returncode == 0, result.stderr
    assert "permission_mode_observed" in evidence
    assert evidence["permission_mode_observed"] is None


def test_given_unobserved_init_mode_when_subagentstop_has_mode_then_top_level_not_backfilled(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    stop = {
        "type": "system",
        "subtype": "hook_response",
        "hook_event": "SubagentStop",
        "hook_name": "SubagentStop",
        "session_id": "fixture-session",
        "stdout": json.dumps(
            {"hook_event_name": "SubagentStop", "agent_id": "child-1", "permission_mode": "auto"}
        ),
    }
    result, evidence = _run_for_evidence(
        repo, worktree, tmp_path, [_init(session_id="s-1"), stop],
        "--require-observed-runtime-field", "permission_mode",
    )
    assert result.returncode == 0, result.stderr
    assert evidence["observed_runtime_fields"]["permission_mode"]["value"] == "auto"
    assert evidence["permission_mode_observed"] is None


def test_given_runner_help_when_invoked_via_subprocess_then_exit0():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
