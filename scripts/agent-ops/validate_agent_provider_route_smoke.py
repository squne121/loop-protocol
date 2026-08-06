#!/usr/bin/env python3
"""validate_agent_provider_route_smoke.py — semantic + aggregate validator
for ``agent_provider_route_smoke/v1`` artifacts (Issue #1886).

Reads a directory of ``agent_provider_route_smoke/v1`` JSON artifacts
(produced by ``run_agent_provider_route_smoke.py``), validates each against
the JSON Schema, and applies one or more semantic aggregate assertions:

- ``--require-native-spawn-event``: every ``status: pass`` artifact must have
  ``spawn.native_spawn_event_observed == true`` with non-empty, distinct
  ``parent_session_id`` / ``child_session_id``.
- ``--assert-zero-gemini-and-fallback-invocations``: every artifact
  (regardless of status) must have
  ``provider_observation.gemini_invocation_count == 0`` and
  ``provider_observation.direct_fallback_invocation_count == 0``. A ``skip``
  status is never promoted to an implicit pass for this assertion (exit 77
  from the underlying producer/harness is surfaced as a distinct
  ``skip_count``, not folded into the pass count).

Exit codes:
  0  all requested assertions hold across all discovered artifacts
  1  at least one assertion violation was found
  2  usage error / no artifacts found / malformed artifact JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import jsonschema
except ImportError:  # pragma: no cover - jsonschema is a declared dependency
    jsonschema = None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "agent_provider_route_smoke_v1.schema.json"
ARTIFACTS_ROOT = REPO_ROOT / ".claude" / "artifacts" / "agent-provider-route"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def discover_artifacts(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*.json")
        if p.name not in {"index.json"}
    )


def latest_run_directory(artifacts_root: Path) -> Path | None:
    if not artifacts_root.is_dir():
        return None
    candidates = [p for p in artifacts_root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_artifacts(directory: Path) -> tuple[list[dict], list[str]]:
    artifacts: list[dict] = []
    errors: list[str] = []
    for path in discover_artifacts(directory):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path}: malformed JSON: {exc}")
            continue
        data["_source_path"] = str(path)
        artifacts.append(data)
    return artifacts, errors


def validate_schema(artifacts: list[dict], schema: dict) -> list[str]:
    errors: list[str] = []
    if jsonschema is None:
        errors.append("jsonschema package is not importable; cannot validate artifact shape")
        return errors
    for artifact in artifacts:
        source = artifact.get("_source_path", "<unknown>")
        payload = {k: v for k, v in artifact.items() if k != "_source_path"}
        try:
            jsonschema.validate(payload, schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"{source}: schema violation: {exc.message}")
    return errors


def assert_require_native_spawn_event(artifacts: list[dict]) -> list[str]:
    errors: list[str] = []
    for artifact in artifacts:
        source = artifact.get("_source_path", "<unknown>")
        if artifact.get("status") != "pass":
            continue
        spawn = artifact.get("spawn") or {}
        parent = spawn.get("parent_session_id")
        child = spawn.get("child_session_id")
        observed = spawn.get("native_spawn_event_observed")
        if not observed:
            errors.append(f"{source}: status=pass but native_spawn_event_observed is not true")
            continue
        if not parent or not child:
            errors.append(f"{source}: status=pass but parent_session_id/child_session_id is empty")
            continue
        if parent == child:
            errors.append(
                f"{source}: status=pass but parent_session_id == child_session_id "
                "(self-report, not independent evidence)"
            )
    return errors


def assert_zero_gemini_and_fallback_invocations(artifacts: list[dict]) -> list[str]:
    errors: list[str] = []
    for artifact in artifacts:
        source = artifact.get("_source_path", "<unknown>")
        obs = artifact.get("provider_observation") or {}
        gemini_count = obs.get("gemini_invocation_count")
        fallback_count = obs.get("direct_fallback_invocation_count")
        if gemini_count != 0:
            errors.append(f"{source}: gemini_invocation_count={gemini_count!r} (must be 0)")
        if fallback_count != 0:
            errors.append(f"{source}: direct_fallback_invocation_count={fallback_count!r} (must be 0)")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-dir", default=None,
        help=(
            "directory containing agent_provider_route_smoke/v1 JSON files "
            "(defaults to the most recent run under .claude/artifacts/agent-provider-route/)"
        ),
    )
    parser.add_argument("--require-native-spawn-event", action="store_true")
    parser.add_argument("--assert-zero-gemini-and-fallback-invocations", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.artifacts_dir:
        artifacts_dir = Path(args.artifacts_dir)
    else:
        artifacts_dir = latest_run_directory(ARTIFACTS_ROOT)
        if artifacts_dir is None:
            print(f"no artifact runs found under {ARTIFACTS_ROOT}", file=sys.stderr)
            return 2

    artifacts, load_errors = load_artifacts(artifacts_dir)
    for error in load_errors:
        print(f"[FAIL] {error}", file=sys.stderr)
    if not artifacts:
        print(f"no agent_provider_route_smoke/v1 artifacts found in {artifacts_dir}", file=sys.stderr)
        return 2
    if load_errors:
        return 2

    schema = _load_schema()
    failures: list[str] = validate_schema(artifacts, schema)

    if args.require_native_spawn_event:
        failures.extend(assert_require_native_spawn_event(artifacts))
    if args.assert_zero_gemini_and_fallback_invocations:
        failures.extend(assert_zero_gemini_and_fallback_invocations(artifacts))

    status_counts: dict[str, int] = {}
    for artifact in artifacts:
        status_counts[artifact.get("status", "unknown")] = status_counts.get(artifact.get("status", "unknown"), 0) + 1

    if failures:
        for failure in failures:
            print(f"[FAIL] {failure}")
        print(f"aggregate: {len(artifacts)} artifacts, statuses={status_counts}, {len(failures)} failures")
        return 1

    print(f"OK: {len(artifacts)} agent_provider_route_smoke/v1 artifacts validated, statuses={status_counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
