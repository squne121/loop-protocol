#!/usr/bin/env python3
"""Controller-owned AGY advisory invocation for codebase-investigator."""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[3]
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_WRAPPER_RESULT_BYTES = 1024 * 1024
_MAX_SUCCESS_RESPONSE_BYTES = 256 * 1024
_MAX_PATHS = 32
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024
_REQUIRED_REQUEST_KEYS = frozenset(
    {
        "schema",
        "schema_version",
        "mode",
        "purpose",
        "target_paths",
        "agy_investigation_requirement",
    }
)
_OPTIONAL_REQUEST_KEYS = frozenset({"context_paths"})
_PRIVATE_SMOKE_SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TERM")

sys.path.insert(0, str(_SCRIPT_DIR))
from route_agy_advisory_fallback import (  # noqa: E402
    ProtocolError,
    encode_closed_json,
    route_agy_advisory_fallback,
    strict_json_object_bytes,
    validate_decision,
)


@dataclass(frozen=True)
class ControllerRun:
    decision: dict[str, Any]
    response_text: str | None


class ControllerInputError(ValueError):
    """A caller-controlled request violates the exact public contract."""


def _private_smoke_wrapper_env(
    private_dir: Path,
    test_wrapper_env_overlay: Mapping[str, str] | None,
) -> dict[str, str]:
    """Build the private fake's credential-sterile wrapper environment.

    The sole actual-wrapper smoke must exercise the canonical wrapper without
    letting its ``proposal_only`` workspace discover host OAuth, keyring, or
    configuration paths. Only the test-owned fake's PATH is admitted; HOME
    and every XDG config/cache/state root are fresh directories below the
    controller-owned temporary directory.
    """
    if test_wrapper_env_overlay is None or set(test_wrapper_env_overlay) != {"PATH"}:
        raise ValueError("private smoke requires exactly the test-owned PATH overlay")
    fake_path = test_wrapper_env_overlay["PATH"]
    if not isinstance(fake_path, str) or not fake_path:
        raise ValueError("private smoke PATH overlay must be non-empty")

    private_home = private_dir / "private-smoke-home"
    xdg_config = private_dir / "private-smoke-xdg-config"
    xdg_cache = private_dir / "private-smoke-xdg-cache"
    xdg_state = private_dir / "private-smoke-xdg-state"
    for directory in (private_home, xdg_config, xdg_cache, xdg_state):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)

    env = {
        key: value
        for key in _PRIVATE_SMOKE_SAFE_ENV_KEYS
        if (value := os.environ.get(key)) is not None
    }
    env.update(
        {
            "PATH": fake_path,
            "HOME": str(private_home),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
        }
    )
    return env


def _failed(reason_code: str, failure_class: str | None = None) -> ControllerRun:
    return ControllerRun(
        {
            "schema": "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1",
            "schema_version": 1,
            "status": "failed",
            "next_action": "fail_closed",
            "failure_class": failure_class,
            "reason_code": reason_code,
        },
        None,
    )


def _adapt_legacy_investigation_requirement(ingress: Mapping[str, Any]) -> str:
    """Map the one legacy ingress field before exact controller validation.

    This is deliberately module-private: stdin and every public controller
    request accept V1 only. The codebase-investigator / issue-refinement-loop
    compatibility ingress consumes this mapping and deletes the legacy field
    before it constructs the public request.
    """
    has_requirement = "agy_investigation_requirement" in ingress
    has_legacy = "agy_advisory_native_fallback_allowed" in ingress
    requirement = ingress.get("agy_investigation_requirement")
    legacy = ingress.get("agy_advisory_native_fallback_allowed")
    if has_requirement and has_legacy:
        raise ControllerInputError("legacy and requirement cannot both be present")
    if has_requirement:
        if not isinstance(requirement, str) or requirement not in {"advisory", "explicitly_required"}:
            raise ControllerInputError("invalid investigation requirement")
        return requirement
    if has_legacy:
        if type(legacy) is not bool:
            raise ControllerInputError("legacy fallback field must be boolean")
        return "advisory" if legacy else "explicitly_required"
    return "explicitly_required"


def _adapt_precontroller_ingress_to_v1(ingress: Mapping[str, Any]) -> dict[str, Any]:
    """Create exact V1 input after the named legacy compatibility ingress.

    This remains outside ``main`` deliberately: the public controller accepts
    exact V1 only. The agent/loop caller invokes this pre-controller mapping
    before it sends stdin, and the test-only smoke uses it to keep that
    boundary executable without widening the public protocol.
    """
    requirement = _adapt_legacy_investigation_requirement(ingress)
    adapted = {key: value for key, value in ingress.items() if key != "agy_advisory_native_fallback_allowed"}
    adapted["agy_investigation_requirement"] = requirement
    return adapted


def _load_producer_module() -> Any:
    path = _REPO_ROOT / ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("_agy_canonical_producer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("canonical producer import unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _validate_relative_file(raw: Any, *, root: Path) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ControllerInputError("invalid path")
    path_shape = PurePosixPath(raw)
    if path_shape.is_absolute() or ".." in path_shape.parts or Path(raw).is_absolute():
        raise ControllerInputError("unsafe path")
    resolved = (root / Path(raw)).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ControllerInputError("path escapes repository") from exc
    try:
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise ControllerInputError("path is not an existing regular file") from exc
    if not stat.S_ISREG(mode):
        raise ControllerInputError("path is not an existing regular file")
    return resolved


def _validate_request(request: Mapping[str, Any], *, root: Path) -> tuple[str, list[Path], list[Path], str]:
    if not _REQUIRED_REQUEST_KEYS.issubset(request) or set(request) - (_REQUIRED_REQUEST_KEYS | _OPTIONAL_REQUEST_KEYS):
        raise ControllerInputError("request keys are not exact")
    if (
        request.get("schema") != "AGY_ADVISORY_INVOCATION_REQUEST_V1"
        or type(request.get("schema_version")) is not int
        or request.get("schema_version") != 1
    ):
        raise ControllerInputError("request schema mismatch")
    if request.get("mode") != "codebase_local_asset":
        raise ControllerInputError("unsupported mode")
    purpose = request.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        raise ControllerInputError("purpose required")
    requirement = request.get("agy_investigation_requirement")
    if not isinstance(requirement, str) or requirement not in {"advisory", "explicitly_required"}:
        raise ControllerInputError("invalid investigation requirement")
    target_raw = request.get("target_paths")
    context_raw = request.get("context_paths", [])
    if not isinstance(target_raw, list) or not target_raw or not isinstance(context_raw, list):
        raise ControllerInputError("invalid path lists")
    if len(target_raw) + len(context_raw) > _MAX_PATHS:
        raise ControllerInputError("too many paths")
    targets = [_validate_relative_file(value, root=root) for value in target_raw]
    contexts = [_validate_relative_file(value, root=root) for value in context_raw]
    all_paths = targets + contexts
    if len(set(all_paths)) != len(all_paths):
        raise ControllerInputError("duplicate paths")
    total_bytes = 0
    for path in all_paths:
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ControllerInputError("path exceeds file limit")
        total_bytes += size
    if total_bytes > _MAX_TOTAL_BYTES:
        raise ControllerInputError("paths exceed aggregate limit")
    return purpose.strip(), targets, contexts, requirement


def _prompt(*, purpose: str, root: Path, targets: list[Path], contexts: list[Path]) -> str:
    def relative(paths: list[Path]) -> str:
        return "\n".join(f"- {path.relative_to(root).as_posix()}" for path in paths)

    context_section = relative(contexts) if contexts else "- (none)"
    return (
        "Perform bounded read-only codebase research.\n"
        f"Purpose: {purpose}\n"
        "Targets:\n"
        f"{relative(targets)}\n"
        "Additional context:\n"
        f"{context_section}\n"
        "Report concrete source evidence and do not perform mutations."
    )


def _read_private_result(path: Path, *, maximum_bytes: int) -> dict[str, Any]:
    """Strictly read a controller-owned regular result file exactly once."""
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise ProtocolError("private result path is not a regular file")
        with path.open("rb") as handle:
            payload = handle.read(maximum_bytes + 1)
    except OSError as exc:
        raise ProtocolError("private result file is unreadable") from exc
    if len(payload) > maximum_bytes:
        raise ProtocolError("wrapper output exceeds byte limit")
    return strict_json_object_bytes(payload)


def _run_controller(
    request: Mapping[str, Any],
    *,
    root: Path = _REPO_ROOT,
    test_wrapper_env_overlay: Mapping[str, str] | None = None,
    _test_profile: str | None = None,
) -> ControllerRun:
    """Own canonical builder → wrapper → readback → pure routing.

    ``test_wrapper_env_overlay`` and ``_test_profile`` are module-private test
    seams. No stdin field, public option, or ambient ``AGY_BIN`` can select an
    executable or a profile.
    """
    root = root.resolve()
    if _test_profile not in {None, "proposal_only"}:
        raise ValueError("unsupported private test profile")
    profile = _test_profile or "local_asset_research"
    try:
        purpose, targets, contexts, requirement = _validate_request(request, root=root)
    except ControllerInputError:
        return _failed("controller_input_invalid")

    producer = _load_producer_module()
    with tempfile.TemporaryDirectory(prefix="codebase-investigator-agy-") as temp_dir:
        private_dir = Path(temp_dir)
        request_file = private_dir / "delegation-request.json"
        result_file = private_dir / "delegation-result.json"
        builder = root / ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py"
        wrapper = root / ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"
        builder_argv = [
            "uv",
            "run",
            "--locked",
            "python3",
            str(builder),
            "--provider",
            "agy",
            "--profile",
            profile,
            "--prompt",
            _prompt(purpose=purpose, root=root, targets=targets, contexts=contexts),
            "--output",
            str(request_file),
        ]
        for path in targets + contexts:
            builder_argv.extend(["--context-file", str(path)])
        try:
            builder_result = subprocess.run(
                builder_argv, cwd=root, capture_output=True, text=True, timeout=60, shell=False, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("builder transport unavailable")
        if builder_result.returncode != 0:
            return _failed("builder_failed")
        try:
            _read_private_result(request_file, maximum_bytes=_MAX_REQUEST_BYTES)
        except ProtocolError:
            return _failed("builder_failed")

        if _test_profile == "proposal_only":
            wrapper_env = _private_smoke_wrapper_env(private_dir, test_wrapper_env_overlay)
        else:
            wrapper_env = dict(os.environ)
            wrapper_env.pop("AGY_BIN", None)
            if test_wrapper_env_overlay is not None:
                # A caller cannot reach this argument through the public CLI/API.
                wrapper_env.update(test_wrapper_env_overlay)
        try:
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--locked",
                    "python3",
                    str(wrapper),
                    "--request-file",
                    str(request_file),
                    "--output-file",
                    str(result_file),
                ],
                cwd=root,
                env=wrapper_env,
                capture_output=True,
                text=True,
                timeout=360,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("wrapper transport unavailable")
        try:
            result = _read_private_result(result_file, maximum_bytes=_MAX_WRAPPER_RESULT_BYTES)
        except ProtocolError:
            return _failed("wrapper_result_invalid")

    decision = route_agy_advisory_fallback(
        result,
        requirement=requirement,
        canonical_failure_kind=producer.canonical_agy_failure_kind,
    )
    if decision["status"] != "ok":
        return ControllerRun(decision, None)
    response_text = result.get("response_text")
    try:
        response_size = len(response_text.encode("utf-8")) if isinstance(response_text, str) else -1
    except UnicodeEncodeError:
        response_size = -1
    if not isinstance(response_text, str) or not response_text or response_size > _MAX_SUCCESS_RESPONSE_BYTES:
        return _failed("wrapper_result_invalid")
    return ControllerRun(decision, response_text)


def _run_fixed_proposal_only_actual_wrapper_smoke(*, root: Path, fake_agy_bin: str) -> ControllerRun:
    """Test-only fixed driver for the sole actual-wrapper smoke.

    It cannot be selected through controller stdin and deliberately uses the
    non-production ``proposal_only`` profile, while retaining the same builder,
    actual wrapper, exact readback, and decision core as production.
    """
    return _run_controller(
        _adapt_precontroller_ingress_to_v1(
            {
                "schema": "AGY_ADVISORY_INVOCATION_REQUEST_V1",
                "schema_version": 1,
                "mode": "codebase_local_asset",
                "purpose": "Verify the fixed non-mutating advisory fallback smoke.",
                "target_paths": [".claude/agents/codebase-investigator.md"],
                "agy_investigation_requirement": "advisory",
            }
        ),
        root=root,
        test_wrapper_env_overlay={
            "PATH": f"{Path(fake_agy_bin).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        _test_profile="proposal_only",
    )


def _success_sidecar(response_text: str) -> bytes:
    return encode_closed_json(
        {
            "schema": "AGY_ADVISORY_SUCCESS_RESULT_V1",
            "schema_version": 1,
            "response_text": response_text,
        }
    )


def _emit_controller_run(run: ControllerRun) -> int:
    decision = validate_decision(run.decision)
    if decision["status"] == "ok" and run.response_text is None:
        return 2
    sys.stdout.buffer.write(encode_closed_json(decision))
    if decision["status"] == "ok":
        sys.stderr.buffer.write(_success_sidecar(run.response_text))
    return 0 if decision["status"] in {"ok", "degraded"} else 1


def _run_stdin_controller() -> int:
    try:
        raw_request = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(raw_request) > _MAX_REQUEST_BYTES:
            return _emit_controller_run(_failed("controller_input_invalid"))
        try:
            request = strict_json_object_bytes(raw_request)
        except ProtocolError:
            return _emit_controller_run(_failed("controller_input_invalid"))
        return _emit_controller_run(_run_controller(request))
    except Exception:
        # Transport/runtime failure emits no decision or sidecar by contract.
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run only the exact public V1 controller input surface."""
    if list(() if argv is None else argv):
        return 2
    return _run_stdin_controller()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
