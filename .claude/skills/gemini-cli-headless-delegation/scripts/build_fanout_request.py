#!/usr/bin/env python3
"""build_fanout_request.py -- construct a delegation_fanout_request_v1 (Issue #1273 AC3).

Assembles an explicit ``subtasks[]`` list from one or more already-built
``delegation_request_v1`` JSON files (typically produced by
``build_request.py``) and the fan-out execution-control knobs
(``max_workers`` / ``max_subtasks`` / ``max_total_attempts`` /
``overall_timeout_sec`` / per-provider / per-profile concurrency). Validates
the assembled request against ``fan_out_orchestrator.validate_fanout_request``
(a *closed* schema -- unknown top-level keys are rejected) before writing it.

Planner mode (dynamic task-count / provider selection) is out of scope for
v1: every subtask must be supplied explicitly by the caller.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "delegation_fanout_request_v1"


def _load_validate_fanout_request():
    script_dir = Path(__file__).resolve().parent
    module_path = script_dir / "fan_out_orchestrator.py"
    spec = importlib.util.spec_from_file_location("fan_out_orchestrator", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load fan_out_orchestrator from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.validate_fanout_request


def _write_failure(output: Path | None, failure_class: str, failure_reason: str) -> None:
    payload = {
        "schema": SCHEMA,
        "ok": False,
        "failure_class": failure_class,
        "failure_reason": failure_reason,
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(text, encoding="utf-8")
    else:
        print(text)


class ConcurrencyMapParseError(ValueError):
    """Raised by _parse_concurrency_entries() on a malformed name=value pair."""


def _parse_concurrency_entries(entries: list[str] | None) -> dict[str, int]:
    """Parse repeatable ``name=value`` CLI entries (Issue #1273 iteration 3
    Major 3: --provider-concurrency / --profile-concurrency) into a
    ``{name: positive_int}`` map. Raises ConcurrencyMapParseError with a
    human-readable message on any malformed entry (missing '=', empty name,
    non-integer or non-positive value) -- validated again downstream by
    ``validate_fanout_request`` regardless, but a precise CLI-level error is
    friendlier than a generic schema validation failure.
    """
    result: dict[str, int] = {}
    for entry in entries or []:
        if "=" not in entry:
            raise ConcurrencyMapParseError(f"malformed concurrency entry {entry!r}: expected 'name=value'")
        name, _, raw_value = entry.partition("=")
        if not name:
            raise ConcurrencyMapParseError(f"malformed concurrency entry {entry!r}: empty name")
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConcurrencyMapParseError(
                f"malformed concurrency entry {entry!r}: value must be an integer"
            ) from exc
        if value < 1:
            raise ConcurrencyMapParseError(f"malformed concurrency entry {entry!r}: value must be >= 1")
        result[name] = value
    return result


def build_fanout_request(
    subtask_request_files: list[Path],
    max_workers: int | None,
    max_subtasks: int | None,
    max_total_attempts: int | None,
    overall_timeout_sec: float | None,
    provider_concurrency: list[str] | None,
    profile_concurrency: list[str] | None,
    output: Path | None,
) -> int:
    """Build and validate a delegation_fanout_request_v1.

    Returns exit code: 0 = success, 1 = validation/usage error, 2 = internal error.
    """
    if not subtask_request_files:
        _write_failure(output, "validation_error", "at least one --subtask-request is required")
        return 1

    subtasks: list[dict[str, Any]] = []
    for path in subtask_request_files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _write_failure(output, "invalid_subtask_request_file", f"cannot load {path}: {exc}")
            return 1
        if not isinstance(raw, dict):
            _write_failure(output, "invalid_subtask_request_file", f"{path} must contain a JSON object")
            return 1
        subtasks.append(raw)

    request: dict[str, Any] = {"schema": SCHEMA, "subtasks": subtasks}
    if max_workers is not None:
        request["max_workers"] = max_workers
    if max_subtasks is not None:
        request["max_subtasks"] = max_subtasks
    if max_total_attempts is not None:
        request["max_total_attempts"] = max_total_attempts
    if overall_timeout_sec is not None:
        request["overall_timeout_sec"] = overall_timeout_sec

    try:
        provider_concurrency_map = _parse_concurrency_entries(provider_concurrency)
        profile_concurrency_map = _parse_concurrency_entries(profile_concurrency)
    except ConcurrencyMapParseError as exc:
        _write_failure(output, "validation_error", str(exc))
        return 1
    if provider_concurrency_map:
        request["provider_concurrency"] = provider_concurrency_map
    if profile_concurrency_map:
        request["profile_concurrency"] = profile_concurrency_map

    try:
        validate_fanout_request = _load_validate_fanout_request()
        validation_errors = validate_fanout_request(request)
    except Exception as exc:  # pylint: disable=broad-except
        _write_failure(output, "internal_error", f"failed to load validate_fanout_request: {exc}")
        return 2

    if validation_errors:
        _write_failure(output, "validation_error", validation_errors[0])
        return 1

    text = json.dumps(request, indent=2, sort_keys=True)
    if output is not None:
        output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subtask-request",
        dest="subtask_request_files",
        action="append",
        type=Path,
        default=[],
        help="Path to an already-built delegation_request_v1 JSON file. Repeatable.",
    )
    parser.add_argument("--max-workers", type=int, default=None)
    parser.add_argument("--max-subtasks", type=int, default=None)
    parser.add_argument("--max-total-attempts", type=int, default=None)
    parser.add_argument("--overall-timeout-sec", type=float, default=None)
    parser.add_argument(
        "--provider-concurrency",
        dest="provider_concurrency",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Per-provider concurrency limit, e.g. --provider-concurrency gemini=2. Repeatable.",
    )
    parser.add_argument(
        "--profile-concurrency",
        dest="profile_concurrency",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Per-profile concurrency limit, e.g. --profile-concurrency github_research=1. Repeatable.",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return build_fanout_request(
        subtask_request_files=args.subtask_request_files,
        max_workers=args.max_workers,
        max_subtasks=args.max_subtasks,
        max_total_attempts=args.max_total_attempts,
        overall_timeout_sec=args.overall_timeout_sec,
        provider_concurrency=args.provider_concurrency,
        profile_concurrency=args.profile_concurrency,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
