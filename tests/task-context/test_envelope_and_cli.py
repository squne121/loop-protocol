"""AC8: stable request/result envelope shape + CLI exit-code mapping,
with additive/open payload and data fields."""

from __future__ import annotations

import io
import json
import sys

import pytest

import task_context_envelope as envelope
import task_contextctl as cli


def _run_cli(argv, stdin_obj, monkeypatch, capsys):
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
    """payload/data internals are additive/open -- unknown extra fields in
    the payload must not be rejected by the envelope layer."""
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
