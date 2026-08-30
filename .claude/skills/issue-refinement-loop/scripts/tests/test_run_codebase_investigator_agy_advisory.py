"""Tests for the controller-owned codebase-investigator AGY invocation."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "run_codebase_investigator_agy_advisory.py"
_SPEC = importlib.util.spec_from_file_location("codebase_investigator_agy_controller_test", _SCRIPT)
assert _SPEC and _SPEC.loader
controller = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = controller
_SPEC.loader.exec_module(controller)


def _request(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "schema": "AGY_ADVISORY_INVOCATION_REQUEST_V1",
        "schema_version": 1,
        "mode": "codebase_local_asset",
        "purpose": "Inspect the supplied source evidence.",
        "target_paths": ["target.txt"],
        "context_paths": ["context.txt"],
        "agy_investigation_requirement": "advisory",
    }
    request.update(overrides)
    return request


def _write_files(root: Path) -> None:
    (root / "target.txt").write_text("target\n", encoding="utf-8")
    (root / "context.txt").write_text("context\n", encoding="utf-8")


def _wrapper_result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "delegation_result/v1",
        "provider": "agy",
        "ok": True,
        "failure_class": None,
        "agy_failure_kind": None,
        "agy_invocation_attempted": True,
        "response_text": "canonical result",
    }
    result.update(overrides)
    return result


def _install_subprocess_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, object],
) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if "build_request.py" in argv[4]:
            output = Path(argv[argv.index("--output") + 1])
            output.write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert "run_gemini_headless.py" in argv[4]
        output = Path(argv[argv.index("--output-file") + 1])
        output.write_bytes(json.dumps(result, separators=(",", ":")).encode("utf-8"))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    return calls


def test_controller_owns_canonical_child_invocations_and_context_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_files(tmp_path)
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    run = controller._run_controller(_request(), root=tmp_path)

    assert run.decision["status"] == "ok"
    assert run.response_text == "canonical result"
    assert len(calls) == 2
    builder_argv, builder_kwargs = calls[0]
    assert builder_argv[:5] == [
        "uv",
        "run",
        "--locked",
        "python3",
        str(tmp_path / ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py"),
    ]
    assert builder_kwargs["shell"] is False
    contexts = [builder_argv[index + 1] for index, value in enumerate(builder_argv) if value == "--context-file"]
    assert contexts == [str(tmp_path / "target.txt"), str(tmp_path / "context.txt")]
    wrapper_argv, wrapper_kwargs = calls[1]
    assert wrapper_argv[:5] == [
        "uv",
        "run",
        "--locked",
        "python3",
        str(tmp_path / ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"),
    ]
    assert wrapper_kwargs["shell"] is False


def test_ambient_agy_bin_is_stripped_and_only_private_test_overlay_can_restore_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_files(tmp_path)
    monkeypatch.setenv("AGY_BIN", "/ambient/not-allowed")
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    controller._run_controller(_request(), root=tmp_path, test_wrapper_env_overlay={"AGY_BIN": "/test/fake-agy"})

    wrapper_env = calls[1][1]["env"]
    assert wrapper_env["AGY_BIN"] == "/test/fake-agy"
    assert "/ambient/not-allowed" not in wrapper_env.values()


def test_input_unknown_keys_and_legacy_field_are_rejected_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_files(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    run = controller._run_controller(_request(agy_advisory_native_fallback_allowed=True), root=tmp_path)

    assert run.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


@pytest.mark.parametrize(
    "paths",
    [
        ["../outside.txt"],
        ["/absolute.txt"],
        ["missing.txt"],
    ],
)
def test_unsafe_or_missing_paths_fail_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, paths: list[str]
) -> None:
    _write_files(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    run = controller._run_controller(_request(target_paths=paths), root=tmp_path)

    assert run.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_symlink_escape_and_duplicate_paths_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "escape.txt").symlink_to(outside)
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    escaped = controller._run_controller(_request(target_paths=["escape.txt"]), root=tmp_path)
    duplicated = controller._run_controller(
        _request(target_paths=["target.txt"], context_paths=["target.txt"]), root=tmp_path
    )

    assert escaped.decision["reason_code"] == "controller_input_invalid"
    assert duplicated.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_malformed_or_oversized_wrapper_output_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(tmp_path)

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_flag = "--output" if "build_request.py" in argv[4] else "--output-file"
        output = Path(argv[argv.index(output_flag) + 1])
        if output_flag == "--output":
            output.write_text("{}", encoding="utf-8")
        else:
            output.write_bytes(b"{" + b"x" * (controller._MAX_WRAPPER_RESULT_BYTES + 1))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    run = controller._run_controller(_request(), root=tmp_path)

    assert run.decision["status"] == "failed"
    assert run.decision["reason_code"] == "wrapper_result_invalid"


@pytest.mark.parametrize(
    ("result", "requirement", "reason"),
    [
        (
            _wrapper_result(ok=False, failure_class="agy_timeout", agy_failure_kind="operational"),
            "advisory",
            "advisory_operational",
        ),
        (
            _wrapper_result(ok=False, failure_class="agy_timeout", agy_failure_kind="operational"),
            "explicitly_required",
            "explicitly_required",
        ),
        (
            _wrapper_result(ok=False, failure_class="agy_permission_denied", agy_failure_kind="policy_or_permission"),
            "advisory",
            "deny_policy",
        ),
        (
            _wrapper_result(ok=False, failure_class="agy_future_unclassified", agy_failure_kind="contract"),
            "advisory",
            "deny_policy",
        ),
    ],
)
def test_failure_routing_uses_producer_classifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, object],
    requirement: str,
    reason: str,
) -> None:
    _write_files(tmp_path)
    _install_subprocess_fake(monkeypatch, result=result)

    run = controller._run_controller(_request(agy_investigation_requirement=requirement), root=tmp_path)

    assert run.decision["reason_code"] == reason
    if reason == "advisory_operational":
        assert run.decision["status"] == "degraded"
    else:
        assert run.decision["status"] == "failed"


def test_success_sidecar_is_exact_narrow_projection() -> None:
    assert controller._success_sidecar("answer") == (
        b'{"schema":"AGY_ADVISORY_SUCCESS_RESULT_V1","schema_version":1,"response_text":"answer"}\n'
    )


@pytest.mark.parametrize(
    ("ingress", "expected"),
    [
        ({"agy_investigation_requirement": "advisory"}, "advisory"),
        ({"agy_advisory_native_fallback_allowed": True}, "advisory"),
        ({"agy_advisory_native_fallback_allowed": False}, "explicitly_required"),
        ({}, "explicitly_required"),
    ],
)
def test_legacy_ingress_maps_only_the_four_valid_unambiguous_cases(ingress: dict[str, object], expected: str) -> None:
    assert controller._adapt_legacy_investigation_requirement(ingress) == expected


@pytest.mark.parametrize(
    "ingress",
    [
        {
            "agy_investigation_requirement": "advisory",
            "agy_advisory_native_fallback_allowed": True,
        },
        {"agy_investigation_requirement": "other"},
        {"agy_investigation_requirement": []},
        {"agy_advisory_native_fallback_allowed": 1},
    ],
)
def test_legacy_ingress_fails_closed_for_dual_or_type_invalid_inputs(
    ingress: dict[str, object],
) -> None:
    with pytest.raises(controller.ControllerInputError):
        controller._adapt_legacy_investigation_requirement(ingress)


def test_legacy_ingress_deletes_old_field_before_exact_v1_controller_input() -> None:
    adapted = controller._adapt_precontroller_ingress_to_v1(
        {
            "schema": "AGY_ADVISORY_INVOCATION_REQUEST_V1",
            "schema_version": 1,
            "mode": "codebase_local_asset",
            "purpose": "Inspect target evidence.",
            "target_paths": ["target.txt"],
            "agy_advisory_native_fallback_allowed": True,
        }
    )

    assert adapted == {
        "schema": "AGY_ADVISORY_INVOCATION_REQUEST_V1",
        "schema_version": 1,
        "mode": "codebase_local_asset",
        "purpose": "Inspect target evidence.",
        "target_paths": ["target.txt"],
        "agy_investigation_requirement": "advisory",
    }


def test_production_wrapper_environment_strips_ambient_agy_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(tmp_path)
    monkeypatch.setenv("AGY_BIN", "/ambient/not-allowed")
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    run = controller._run_controller(_request(), root=tmp_path)

    assert run.decision["status"] == "ok"
    assert "AGY_BIN" not in calls[1][1]["env"]


def test_fixed_prompt_sections_and_target_only_context_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_files(tmp_path)
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    run = controller._run_controller(
        _request(target_paths=["target.txt"], context_paths=[], purpose="Locate only target evidence."),
        root=tmp_path,
    )

    assert run.decision["status"] == "ok"
    argv = calls[0][0]
    prompt = argv[argv.index("--prompt") + 1]
    assert "Targets:\n- target.txt\nAdditional context:\n- (none)" in prompt
    assert "Locate only target evidence." in prompt
    assert [value for value in argv if value == "--context-file"] == ["--context-file"]
    assert argv[argv.index("--context-file") + 1] == str(tmp_path / "target.txt")
    assert "--objective" not in argv


@pytest.mark.parametrize(
    "override",
    [
        {"schema_version": True},
        {"purpose": ""},
        {"target_paths": []},
        {"target_paths": "target.txt"},
        {"context_paths": "context.txt"},
        {"agy_investigation_requirement": "unknown"},
        {"raw_prompt": "caller injection"},
        {"AGY_BIN": "/caller/fake"},
        {"wrapper_result": {}},
        {"failure_class": "agy_timeout"},
    ],
)
def test_public_non_v1_or_executable_injection_input_never_spawns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: dict[str, object]
) -> None:
    _write_files(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    run = controller._run_controller(_request(**override), root=tmp_path)

    assert run.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_path_count_and_file_size_limits_apply_before_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(tmp_path)
    for number in range(31):
        (tmp_path / f"extra-{number}.txt").write_text("x", encoding="utf-8")
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (controller._MAX_FILE_BYTES + 1))
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    too_many = controller._run_controller(
        _request(target_paths=["target.txt"] + [f"extra-{number}.txt" for number in range(31)]),
        root=tmp_path,
    )
    too_large = controller._run_controller(_request(target_paths=["oversized.txt"]), root=tmp_path)

    assert too_many.decision["reason_code"] == "controller_input_invalid"
    assert too_large.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_exact_maximum_path_count_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_files(tmp_path)
    names = ["target.txt"]
    for number in range(31):
        name = f"boundary-{number}.txt"
        (tmp_path / name).write_text("x", encoding="utf-8")
        names.append(name)
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    run = controller._run_controller(_request(target_paths=names, context_paths=[]), root=tmp_path)

    assert run.decision["status"] == "ok"
    assert len([value for value in calls[0][0] if value == "--context-file"]) == 32


def test_duplicate_key_and_nonzero_wrapper_result_are_read_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_files(tmp_path)

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "build_request.py" in argv[4]:
            Path(argv[argv.index("--output") + 1]).write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")
        Path(argv[argv.index("--output-file") + 1]).write_bytes(b'{"schema":"delegation_result/v1","schema":"other"}')
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    run = controller._run_controller(_request(), root=tmp_path)

    assert run.decision["status"] == "failed"
    assert run.decision["reason_code"] == "wrapper_result_invalid"


@pytest.mark.parametrize(
    "response_text",
    ["", "x" * (controller._MAX_SUCCESS_RESPONSE_BYTES + 1)],
)
def test_success_requires_exact_nonempty_bounded_wrapper_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response_text: str
) -> None:
    _write_files(tmp_path)
    _install_subprocess_fake(monkeypatch, result=_wrapper_result(response_text=response_text))

    run = controller._run_controller(_request(), root=tmp_path)

    assert run.decision["status"] == "failed"
    assert run.decision["reason_code"] == "wrapper_result_invalid"


@pytest.mark.parametrize(
    "value",
    [[], {}, 1, True, None],
)
def test_non_string_requirement_fails_closed_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    _write_files(tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    run = controller._run_controller(_request(agy_investigation_requirement=value), root=tmp_path)

    assert run.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_main_emits_exact_failed_decision_for_malformed_public_input(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(controller.sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"x":1}\n'), encoding="utf-8"))
    monkeypatch.setattr(controller.sys, "stdout", stdout)
    monkeypatch.setattr(controller.sys, "stderr", stderr)

    assert controller.main() == 1
    stdout.flush()
    stderr.flush()
    assert stdout.buffer.getvalue() == (
        b'{"schema":"AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1","schema_version":1,'
        b'"status":"failed","next_action":"fail_closed","failure_class":null,'
        b'"reason_code":"controller_input_invalid"}\n'
    )
    assert stderr.buffer.getvalue() == b""


def test_main_emits_exact_paired_success_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    request = json.dumps(_request(), separators=(",", ":")).encode("utf-8")
    monkeypatch.setattr(controller.sys, "stdin", io.TextIOWrapper(io.BytesIO(request), encoding="utf-8"))
    monkeypatch.setattr(controller.sys, "stdout", stdout)
    monkeypatch.setattr(controller.sys, "stderr", stderr)
    monkeypatch.setattr(
        controller,
        "_run_controller",
        lambda _request: controller.ControllerRun(
            {
                "schema": "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1",
                "schema_version": 1,
                "status": "ok",
                "next_action": "continue_agy_result",
                "failure_class": None,
                "reason_code": "agy_success",
            },
            "exact response",
        ),
    )

    assert controller.main() == 0
    stdout.flush()
    stderr.flush()
    assert stdout.buffer.getvalue() == (
        b'{"schema":"AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1","schema_version":1,'
        b'"status":"ok","next_action":"continue_agy_result","failure_class":null,'
        b'"reason_code":"agy_success"}\n'
    )
    assert stderr.buffer.getvalue() == (
        b'{"schema":"AGY_ADVISORY_SUCCESS_RESULT_V1","schema_version":1,"response_text":"exact response"}\n'
    )


def test_controller_and_producer_accept_shared_four_mebibyte_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []
    for number in range(4):
        name = f"maximum-{number}.txt"
        (tmp_path / name).write_bytes(b"x" * controller._MAX_FILE_BYTES)
        names.append(name)
    calls = _install_subprocess_fake(monkeypatch, result=_wrapper_result())

    run = controller._run_controller(_request(target_paths=names, context_paths=[]), root=tmp_path)
    producer = controller._load_producer_module()

    assert run.decision["status"] == "ok"
    assert len(calls) == 2
    assert producer.LOCAL_ASSET_MAX_CONTEXT_BYTES == controller._MAX_FILE_BYTES
    assert producer.LOCAL_ASSET_MAX_CONTEXT_TOTAL_BYTES == controller._MAX_TOTAL_BYTES
    assert producer._validate_agy_local_asset_payload_bounds([tmp_path / name for name in names]) == []


def test_controller_rejects_aggregate_over_four_mebibytes_before_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names: list[str] = []
    for number in range(4):
        name = f"maximum-{number}.txt"
        (tmp_path / name).write_bytes(b"x" * controller._MAX_FILE_BYTES)
        names.append(name)
    (tmp_path / "extra.txt").write_bytes(b"x")
    calls: list[object] = []
    monkeypatch.setattr(controller.subprocess, "run", lambda *_args, **_kwargs: calls.append(None))

    run = controller._run_controller(_request(target_paths=names + ["extra.txt"], context_paths=[]), root=tmp_path)

    assert run.decision["reason_code"] == "controller_input_invalid"
    assert calls == []


def test_named_precontroller_ingress_maps_legacy_before_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    ingress = _request()
    ingress.pop("agy_investigation_requirement")
    ingress["agy_advisory_native_fallback_allowed"] = True
    captured: list[dict[str, object]] = []
    stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stderr = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(
        controller.sys,
        "stdin",
        io.TextIOWrapper(
            io.BytesIO(json.dumps(ingress, separators=(",", ":")).encode("utf-8")),
            encoding="utf-8",
        ),
    )
    monkeypatch.setattr(controller.sys, "stdout", stdout)
    monkeypatch.setattr(controller.sys, "stderr", stderr)

    def fake_controller(request: dict[str, object]) -> object:
        captured.append(request)
        return controller._failed("builder_failed")

    monkeypatch.setattr(controller, "_run_controller", fake_controller)

    assert controller.main(["--precontroller-legacy-ingress"]) == 1
    assert captured == [
        {
            "schema": "AGY_ADVISORY_INVOCATION_REQUEST_V1",
            "schema_version": 1,
            "mode": "codebase_local_asset",
            "purpose": "Inspect the supplied source evidence.",
            "target_paths": ["target.txt"],
            "context_paths": ["context.txt"],
            "agy_investigation_requirement": "advisory",
        }
    ]
