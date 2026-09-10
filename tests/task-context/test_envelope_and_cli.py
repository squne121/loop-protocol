"""AC8: stable request/result envelope shape + CLI exit-code mapping,
with additive/open payload and data fields.

fix_delta finding 1: the CLI must actually validate/unwrap the top-level
request envelope (not treat raw stdin JSON as the operation payload
directly). ``_run_cli`` below wraps every test payload into a canonical
envelope via ``envelope.build_request`` by default -- tests that need to
exercise malformed-envelope handling pass ``raw_stdin_obj`` to bypass that
wrapping.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import task_context_envelope as envelope
import task_contextctl as cli

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "task-context"
_CLI_PATH = _SCRIPTS_DIR / "task_contextctl.py"


def _operation_for_argv(argv: list[str]) -> str:
    if argv[0] == "hook":
        return "hook"
    if argv[:2] == ["signal", "apply"]:
        return "signal_apply"
    if argv[:2] == ["query", "current"]:
        return "query_current"
    if argv[:2] == ["projection", "flush"]:
        return "projection_flush"
    if argv[:2] == ["smoke", "seed"]:
        return "smoke_seed"
    raise ValueError(f"no known operation mapping for argv {argv!r}")


def _run_cli(argv, payload, monkeypatch, capsys, *, operation=None, raw_stdin_obj=None):
    """Run the CLI in-process. Unless ``raw_stdin_obj`` is given, ``payload``
    is automatically wrapped into a canonical request envelope matching the
    operation ``argv`` maps to (or the explicit ``operation`` override, for
    envelope/operation-mismatch negative tests)."""
    if raw_stdin_obj is None:
        stdin_obj = envelope.build_request(operation or _operation_for_argv(argv), payload)
    else:
        stdin_obj = raw_stdin_obj
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_obj)))
    exit_code = cli.main(argv)
    captured = capsys.readouterr()
    # stdout carries EXACTLY one JSON object -- no leading/trailing noise.
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {captured.out!r}"
    result = json.loads(lines[0])
    return result, exit_code, captured


def test_given_request_envelope_builder_when_called_then_shape_matches_frozen_contract():
    req = envelope.build_request("smoke_seed", {"title": "x"})
    assert envelope.is_valid_request_envelope(req)
    assert req["schema_version"] == "task-context-request/v1"


def test_given_result_envelope_builder_when_called_then_shape_matches_frozen_contract():
    res = envelope.build_ok_result({"foo": "bar"})
    assert envelope.is_valid_result_envelope(res)
    assert res["status"] == "ok"
    assert res["code"] == "OK"


def test_given_smoke_seed_when_run_via_cli_then_exit_zero_and_ok_envelope(state_root, monkeypatch, capsys):
    result, exit_code, _ = _run_cli(["smoke", "seed"], {}, monkeypatch, capsys)
    assert exit_code == 0
    assert envelope.is_valid_result_envelope(result)
    assert result["status"] == "ok"
    assert "task_id" in result["data"]
    assert "binding_id" in result["data"]
    assert "run_id" in result["data"]


def test_given_smoke_seed_with_empty_stdin_when_run_via_cli_then_no_envelope_required(
    state_root, monkeypatch, capsys
):
    """Completely empty stdin (no request envelope at all) is allowed for
    operations that need no payload -- only *non-empty* stdin content must
    be a valid envelope (fix_delta finding 1)."""
    result, exit_code, _ = _run_cli(["smoke", "seed"], {}, monkeypatch, capsys, raw_stdin_obj={})
    assert exit_code == 0
    assert result["status"] == "ok"


def test_given_query_current_missing_task_id_when_run_via_cli_then_validation_error_exit_code(
    state_root, monkeypatch, capsys
):
    result, exit_code, _ = _run_cli(["query", "current"], {}, monkeypatch, capsys)
    assert exit_code == 2
    assert result["status"] == "error"
    assert result["code"] == "VALIDATION_ERROR"


def test_given_query_current_unknown_task_id_when_run_via_cli_then_not_found_exit_code(
    state_root, monkeypatch, capsys
):
    result, exit_code, _ = _run_cli(["query", "current"], {"task_id": "does-not-exist"}, monkeypatch, capsys)
    assert exit_code == 5
    assert result["code"] == "NOT_FOUND"


def test_given_query_current_after_smoke_seed_when_run_via_cli_then_ok_and_task_found(
    state_root, monkeypatch, capsys
):
    seeded, _, _ = _run_cli(["smoke", "seed"], {}, monkeypatch, capsys)
    task_id = seeded["data"]["task_id"]
    result, exit_code, _ = _run_cli(["query", "current"], {"task_id": task_id}, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["task"]["id"] == task_id


def test_given_hook_event_when_run_via_cli_with_extra_additive_payload_fields_then_still_accepted(
    state_root, monkeypatch, capsys
):
    """payload/data internals are additive/open -- unknown extra fields
    *inside* payload must not be rejected by the envelope layer."""
    result, exit_code, _ = _run_cli(
        ["hook", "UserPromptSubmit"],
        {"metadata": {"status": "ok"}, "future_consumer_field_not_yet_specified": {"whatever": 1}},
        monkeypatch,
        capsys,
    )
    assert exit_code == 0
    assert result["status"] == "ok"


def test_given_signal_apply_missing_fields_when_run_via_cli_then_validation_error(state_root, monkeypatch, capsys):
    result, exit_code, _ = _run_cli(["signal", "apply"], {}, monkeypatch, capsys)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_projection_flush_when_nothing_enqueued_then_ok_with_null_projection(
    state_root, monkeypatch, capsys
):
    result, exit_code, _ = _run_cli(["projection", "flush"], {"projection_key": "herdr:tab-x"}, monkeypatch, capsys)
    assert exit_code == 0
    assert result["data"]["projection"] is None


@pytest.mark.parametrize(
    ("code", "expected_exit"),
    [
        ("OK", 0),
        ("VALIDATION_ERROR", 2),
        ("TEMPORARILY_UNAVAILABLE", 3),
        ("CONFLICT", 4),
        ("NOT_FOUND", 5),
        ("CORRUPT_DATABASE", 6),
        ("SCHEMA_TOO_NEW", 7),
    ],
)
def test_given_known_error_code_when_mapped_then_exit_code_matches_fixed_table(code, expected_exit):
    assert cli.EXIT_CODE_BY_ERROR_CODE[code] == expected_exit


# ---------------------------------------------------------------------------
# fix_delta finding 1: the CLI actually validates/unwraps the top-level
# request envelope (not just the loose envelope-builder self-tests above).
# ---------------------------------------------------------------------------


def test_given_top_level_field_missing_when_run_via_cli_then_validation_error(state_root, monkeypatch, capsys):
    raw = {"schema_version": "task-context-request/v1", "operation": "smoke_seed", "payload": {}}  # no request_id
    result, exit_code, _ = _run_cli(["smoke", "seed"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_unexpected_top_level_field_when_run_via_cli_then_validation_error(state_root, monkeypatch, capsys):
    raw = {
        "schema_version": "task-context-request/v1",
        "operation": "smoke_seed",
        "request_id": "r1",
        "payload": {},
        "unexpected_extra_field": "nope",
    }
    result, exit_code, _ = _run_cli(["smoke", "seed"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_wrong_schema_version_when_run_via_cli_then_validation_error(state_root, monkeypatch, capsys):
    raw = {"schema_version": "task-context-request/v999", "operation": "smoke_seed", "request_id": "r1", "payload": {}}
    result, exit_code, _ = _run_cli(["smoke", "seed"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_non_object_payload_when_run_via_cli_then_validation_error(state_root, monkeypatch, capsys):
    raw = {
        "schema_version": "task-context-request/v1",
        "operation": "smoke_seed",
        "request_id": "r1",
        "payload": "not-an-object",
    }
    result, exit_code, _ = _run_cli(["smoke", "seed"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_envelope_operation_mismatched_with_argv_derived_operation_when_run_via_cli_then_validation_error(
    state_root, monkeypatch, capsys
):
    """The envelope's `operation` must match the operation the CLI already
    determined from argv/the subcommand invoked -- e.g. piping a
    `query_current` envelope into `task-contextctl smoke seed` is rejected,
    not silently dispatched as smoke_seed with the wrong declared
    operation."""
    raw = {
        "schema_version": "task-context-request/v1",
        "operation": "query_current",
        "request_id": "r1",
        "payload": {"task_id": "whatever"},
    }
    result, exit_code, _ = _run_cli(["smoke", "seed"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 2
    assert result["code"] == "VALIDATION_ERROR"


def test_given_canonical_envelope_when_query_current_payload_unwrapped_then_task_id_used(
    state_root, monkeypatch, capsys
):
    """Positive-path proof that the CLI actually unwraps `payload` as the
    operation input -- not just accepts/rejects envelope shape."""
    seeded, _, _ = _run_cli(["smoke", "seed"], {}, monkeypatch, capsys)
    task_id = seeded["data"]["task_id"]
    raw = envelope.build_request("query_current", {"task_id": task_id}, request_id="fixed-request-id")
    result, exit_code, _ = _run_cli(["query", "current"], None, monkeypatch, capsys, raw_stdin_obj=raw)
    assert exit_code == 0
    assert result["data"]["task"]["id"] == task_id


def test_given_canonical_envelope_when_piped_to_real_cli_subprocess_then_ok_envelope_returned(state_root, monkeypatch):
    """End-to-end: pipe a canonical request envelope into the *actual*
    `task_contextctl.py` CLI as a real subprocess (not the in-process
    `cli.main()` call the other tests use) and assert the frozen result
    envelope shape and business outcome (fix_delta finding 1)."""
    request = envelope.build_request("smoke_seed", {"title": "subprocess smoke"})
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "smoke", "seed"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {proc.stdout!r}"
    result = json.loads(lines[0])
    assert envelope.is_valid_result_envelope(result)
    assert result["status"] == "ok"
    assert "task_id" in result["data"]


def test_given_mismatched_operation_envelope_when_piped_to_real_cli_subprocess_then_validation_error(
    state_root, monkeypatch
):
    """End-to-end negative case for the same real-CLI-subprocess path:
    envelope.operation not matching the invoked subcommand must be rejected
    by the real process too, not only by the in-process `cli.main()` calls
    used elsewhere in this file."""
    request = envelope.build_request("query_current", {"task_id": "whatever"})
    env = dict(os.environ)
    env["LOOP_TASK_CONTEXT_STATE_ROOT"] = str(state_root)
    proc = subprocess.run(
        [sys.executable, str(_CLI_PATH), "smoke", "seed"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["status"] == "error"
    assert result["code"] == "VALIDATION_ERROR"
    assert proc.returncode == 2
