"""Issue #2854: ``--require-observed-runtime-field permission_mode``.

``permission_mode`` is observed ONLY from the native stream-json
``system/hook_response`` ``SubagentStop`` hook stdin payload. Every other
location, an invalid value, or a conflict is not an observation (fail closed),
and the other four required-field names stay unsupported (exit 77).
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

_BASE_PATH = Path(__file__).resolve().parent / "test_run_worktree_agent_runtime_smoke.py"
_BASE_NAME = "_runtime_smoke_base_for_permission_mode_tests"


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


@pytest.fixture()
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    return _base._build_repo_with_worktree(tmp_path)


def _echo(payload: dict) -> str:
    return f"echo {shlex.quote(json.dumps(payload))}\n"


def _hook_response(
    hook_event: str,
    *,
    inner: dict | None = None,
    stdout_inner: dict | None = None,
    output_inner: dict | None = None,
    subtype: str = "hook_response",
) -> str:
    """One native-shaped ``system/hook_response`` line. ``inner`` is echoed on
    both ``stdout`` and ``output`` (the real ``cat`` hook shape); the explicit
    per-channel args override it for channel-conflict fixtures."""
    stdout_payload = stdout_inner if stdout_inner is not None else inner
    output_payload = output_inner if output_inner is not None else inner
    payload: dict = {
        "type": "system",
        "subtype": subtype,
        "hook_event": hook_event,
        "hook_name": hook_event,
        "session_id": "fixture-session",
    }
    if stdout_payload is not None:
        payload["stdout"] = json.dumps(stdout_payload)
    if output_payload is not None:
        payload["output"] = json.dumps(output_payload)
    return _echo(payload)


def _stop_payload(mode: object = "auto", *, agent_id: str = "child-1", include_mode: bool = True) -> dict:
    payload: dict = {"agent_id": agent_id, "agent_type": "general-purpose"}
    if include_mode:
        payload["permission_mode"] = mode
    return payload


def _fake_body(*stream_lines: str, exit_code: int = 0) -> str:
    return (
        "\ncat > /dev/null\n"
        + _echo({"type": "system", "subtype": "init"})
        + "".join(stream_lines)
        + _echo({"type": "result", "subtype": "success"})
        + f"exit {exit_code}\n"
    )


def _run_smoke(
    repo: Path,
    worktree: Path,
    tmp_path: Path,
    fake_body: str,
    *extra_args: str,
    name: str = "out",
):
    fake_bin = tmp_path / f"bin-{name}"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", _HELP_BRANCH + fake_body)
    out_dir = tmp_path / name
    result = _run(
        repo,
        worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(_prompt_file(tmp_path)), "--output-dir", str(out_dir),
        *extra_args,
        fake_bin_dir=fake_bin,
    )
    summary_path = out_dir / "summary.md"
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    return result, summary


def _summary_value(summary: str, key: str) -> str | None:
    prefix = f"- {key}: "
    for line in summary.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _summary_dict(summary: str, key: str) -> dict:
    raw = _summary_value(summary, key)
    assert raw is not None, f"summary.md has no {key!r} line"
    import ast

    return ast.literal_eval(raw)


@pytest.mark.parametrize("mode", ["auto", "default"])
def test_given_require_permission_mode_when_subagentstop_payload_has_mode_then_observed_and_exit0(
    repo_with_worktree, tmp_path, mode
):
    repo, worktree = repo_with_worktree
    body = _fake_body(
        _hook_response("SubagentStart", inner=_stop_payload(include_mode=False)),
        _hook_response("SubagentStop", inner=_stop_payload(mode)),
    )
    result, summary = _run_smoke(
        repo, worktree, tmp_path, body, "--require-observed-runtime-field", "permission_mode"
    )
    assert result.returncode == 0, result.stderr
    observed = _summary_dict(summary, "observed_runtime_fields")
    assert observed == {
        "permission_mode": {
            "value": mode,
            "source_event": "system/hook_response",
            "source_hook_event": "SubagentStop",
            "source_field": "permission_mode",
        }
    }
    assert _summary_dict(summary, "unavailable_required_runtime_observation_reasons") == {}
    assert _summary_value(summary, "unavailable_required_runtime_observations") == "[]"
    assert "capability_decision: required_runtime_evidence_unavailable" not in summary
    assert "native_event_field_unavailable" not in summary


@pytest.mark.parametrize(
    ("stream_lines", "expected_reason"),
    [
        pytest.param(
            [_hook_response("SubagentStop", inner=_stop_payload(include_mode=False))],
            "field_absent",
            id="subagentstop-without-permission_mode",
        ),
        pytest.param([], "no_subagentstop_hook_event", id="no-subagentstop-event"),
        pytest.param(
            [_hook_response("SubagentStart", inner=_stop_payload("auto"))],
            "no_subagentstop_hook_event",
            id="only-subagentstart-event",
        ),
    ],
)
def test_given_require_permission_mode_when_payload_lacks_mode_then_exit77_unavailable(
    repo_with_worktree, tmp_path, stream_lines, expected_reason
):
    repo, worktree = repo_with_worktree
    result, summary = _run_smoke(
        repo, worktree, tmp_path, _fake_body(*stream_lines),
        "--require-observed-runtime-field", "permission_mode",
    )
    assert result.returncode == 77
    assert result.stderr.startswith("SKIP:")
    assert "capability_decision: required_runtime_evidence_unavailable" in summary
    assert "unavailable_required_runtime_observations: ['permission_mode']" in summary
    assert _summary_dict(summary, "unavailable_required_runtime_observation_reasons") == {
        "permission_mode": expected_reason
    }
    assert _summary_dict(summary, "observed_runtime_fields") == {}


def _non_native_cases() -> list:
    inner_json = json.dumps({"permission_mode": "auto", "agent_id": "child-1"})
    cases = [
        pytest.param(
            [_echo({"type": "assistant", "message": {"content": [{"type": "text", "text": inner_json}]}})],
            id="assistant-text",
        ),
        pytest.param(
            [_echo({"type": "result", "subtype": "success", "result": inner_json, "permission_mode": "auto"})],
            id="result-text",
        ),
        pytest.param(
            [_hook_response("SubagentStart", inner=_stop_payload("auto"))],
            id="subagentstart-hook-event",
        ),
        pytest.param(
            [_hook_response("PreToolUse", inner=_stop_payload("auto"))],
            id="other-hook-event",
        ),
        pytest.param(
            [_hook_response("SubagentStop", inner=_stop_payload("auto"), subtype="hook_started")],
            id="subagentstop-not-hook_response-subtype",
        ),
        pytest.param(
            [_hook_response("SubagentStop", inner=_stop_payload("auto", agent_id=""))],
            id="empty-agent_id",
        ),
        pytest.param(
            [_hook_response("SubagentStop", inner={"permission_mode": "auto"})],
            id="missing-agent_id",
        ),
        pytest.param(
            [
                _hook_response(
                    "SubagentStop",
                    stdout_inner=_stop_payload("auto"),
                    output_inner=_stop_payload("default"),
                )
            ],
            id="stdout-output-conflict",
        ),
        pytest.param(
            [
                _hook_response("SubagentStop", inner=_stop_payload("auto", agent_id="a")),
                _hook_response("SubagentStop", inner=_stop_payload("default", agent_id="b")),
            ],
            id="conflicting-multiple-subagentstop-events",
        ),
        pytest.param(
            [
                _hook_response("SubagentStop", inner=_stop_payload("auto", agent_id="a")),
                _hook_response("SubagentStop", inner=_stop_payload("not-a-mode", agent_id="b")),
            ],
            id="valid-plus-invalid-subagentstop-events",
        ),
    ]
    for label, bad in (
        ("number", 1),
        ("bool", True),
        ("object", {"mode": "auto"}),
        ("list", ["auto"]),
        ("null", None),
        ("out-of-enum", "bogus"),
        ("wrong-case", "AUTO"),
        ("empty-string", ""),
    ):
        cases.append(
            pytest.param(
                [_hook_response("SubagentStop", inner=_stop_payload(bad))],
                id=f"non-string-or-invalid-{label}",
            )
        )
    return cases


@pytest.mark.parametrize("stream_lines", _non_native_cases())
def test_given_require_permission_mode_when_mode_only_in_non_native_source_then_exit77(
    repo_with_worktree, tmp_path, stream_lines
):
    repo, worktree = repo_with_worktree
    result, summary = _run_smoke(
        repo, worktree, tmp_path, _fake_body(*stream_lines),
        "--require-observed-runtime-field", "permission_mode",
    )
    assert result.returncode == 77, result.stderr
    assert "unavailable_required_runtime_observations: ['permission_mode']" in summary
    assert _summary_dict(summary, "observed_runtime_fields") == {}
    reasons = _summary_dict(summary, "unavailable_required_runtime_observation_reasons")
    assert set(reasons) == {"permission_mode"}
    assert reasons["permission_mode"] in {
        "no_subagentstop_hook_event",
        "field_absent",
        "invalid_or_conflicting_value",
    }


def test_given_permission_mode_observed_when_other_fields_required_then_only_unsupported_unavailable(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    body = _fake_body(
        _hook_response("SubagentStart", inner=_stop_payload(include_mode=False)),
        _hook_response("SubagentStop", inner=_stop_payload("auto")),
    )
    result, summary = _run_smoke(
        repo, worktree, tmp_path, body,
        "--require-observed-runtime-field", "permission_mode",
        "--require-observed-runtime-field", "executor",
        name="combined",
    )
    assert result.returncode == 77
    assert "unavailable_required_runtime_observations: ['executor']" in summary
    assert _summary_dict(summary, "unavailable_required_runtime_observation_reasons") == {
        "executor": "no_native_extractor"
    }
    assert set(_summary_dict(summary, "observed_runtime_fields")) == {"permission_mode"}

    for field in ("effective_permission_profile", "loaded_skill", "executor", "mutation"):
        alone_result, alone_summary = _run_smoke(
            repo, worktree, tmp_path, body,
            "--require-observed-runtime-field", field,
            name=f"alone-{field}",
        )
        assert alone_result.returncode == 77, (field, alone_result.stderr)
        assert f"unavailable_required_runtime_observations: ['{field}']" in alone_summary
        assert _summary_dict(alone_summary, "unavailable_required_runtime_observation_reasons") == {
            field: "no_native_extractor"
        }
        assert _summary_dict(alone_summary, "observed_runtime_fields") == {}


def test_given_permission_mode_observed_when_runtime_fails_then_original_failure_preserved(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    stop_lines = (
        _hook_response("SubagentStart", inner=_stop_payload(include_mode=False)),
        _hook_response("SubagentStop", inner=_stop_payload("auto")),
    )

    # (a) non-zero fake claude exit.
    failing_body = _fake_body(*stop_lines, exit_code=3)
    baseline, _ = _run_smoke(repo, worktree, tmp_path, failing_body, name="fail-baseline")
    assert baseline.returncode not in (0, 77)
    required, required_summary = _run_smoke(
        repo, worktree, tmp_path, failing_body,
        "--require-observed-runtime-field", "permission_mode",
        name="fail-required",
    )
    assert required.returncode == baseline.returncode
    assert "observed_runtime_fields" in required_summary
    unsupported, _ = _run_smoke(
        repo, worktree, tmp_path, failing_body,
        "--require-observed-runtime-field", "executor",
        name="fail-unsupported",
    )
    assert unsupported.returncode == baseline.returncode

    # (b) existing causal-evidence gate FAIL (a lone SubagentStop has no
    # correlated SubagentStart, so hook_id_correlated is not reached).
    causal_body = _fake_body(_hook_response("SubagentStop", inner=_stop_payload("auto")))
    causal_baseline, _ = _run_smoke(
        repo, worktree, tmp_path, causal_body,
        "--require-subagent-causal-evidence",
        name="causal-baseline",
    )
    assert causal_baseline.returncode == 1
    causal_required, causal_summary = _run_smoke(
        repo, worktree, tmp_path, causal_body,
        "--require-subagent-causal-evidence",
        "--require-observed-runtime-field", "permission_mode",
        name="causal-required",
    )
    assert causal_required.returncode == 1
    assert "'permission_mode'" in (_summary_value(causal_summary, "observed_runtime_fields") or "")
    assert "capability_decision: required_runtime_evidence_unavailable" not in causal_summary


def test_given_no_require_flag_when_permission_mode_present_then_no_observation_keys(
    repo_with_worktree, tmp_path
):
    repo, worktree = repo_with_worktree
    body = _fake_body(
        _hook_response("SubagentStart", inner=_stop_payload(include_mode=False)),
        _hook_response("SubagentStop", inner=_stop_payload("auto")),
    )
    result, summary = _run_smoke(repo, worktree, tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert "observed_runtime_fields" not in summary
    assert "unavailable_required_runtime_observation_reasons" not in summary
    assert "required_runtime_observations" not in summary
    assert "unavailable_required_runtime_observations" not in summary
