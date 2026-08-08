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
_PRODUCER_PATH = Path(__file__).resolve().parent / "run_agent_provider_route_smoke.py"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_producer_module():
    """Import ``run_agent_provider_route_smoke.py`` as a sibling module (not
    a package -- ``scripts/agent-ops`` uses a hyphen) so the required
    six-route set is read from the single producer-owned source of truth
    (``REQUIRED_ROUTES`` / ``_route_key``) instead of a second, driftable
    copy here."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_agent_provider_route_smoke_producer_for_validator", _PRODUCER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PRODUCER = _load_producer_module()
REQUIRED_ROUTE_KEYS: frozenset[str] = frozenset(
    _PRODUCER._route_key(route) for route in _PRODUCER.REQUIRED_ROUTES
)


def _artifact_route_key(artifact: dict) -> str | None:
    subject = artifact.get("subject") or {}
    request = artifact.get("request") or {}
    runtime = subject.get("runtime")
    agent_name = subject.get("agent_name")
    profile = request.get("profile")
    if not runtime or not agent_name or not profile:
        return None
    return f"{runtime}:{agent_name}:{profile}"


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
    format_checker = jsonschema.FormatChecker()
    for artifact in artifacts:
        source = artifact.get("_source_path", "<unknown>")
        payload = {k: v for k, v in artifact.items() if k != "_source_path"}
        try:
            jsonschema.validate(payload, schema, format_checker=format_checker)
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


def assert_batch_close_gate(artifacts: list[dict], *, expected_head_sha: str | None) -> list[str]:
    """Issue #1886 P0-3 fix_delta (PR #2005 adversarial review, decisive
    false-pass repro): the prior validator accepted ANY well-formed
    directory of artifacts -- including a full six-route ``--dry-run``
    SKIP cohort -- as an aggregate PASS, because it never required the
    exact required six-route set, all-PASS status, a shared batch, a
    shared HEAD, or a real route_evidence digest. This is the merge-gate
    close condition: exactly the required six ``(runtime, agent, profile)``
    routes, no duplicates, no unexpected routes, every artifact
    ``status: pass``, all sharing one non-empty ``batch_run_id``, all
    sharing one non-null ``subject.head_sha`` (optionally cross-checked
    against ``--expected-head-sha``), and (for the ``github_research``
    route) a real 64-hex ``route_evidence.sha256`` digest."""
    errors: list[str] = []
    route_keys = [_artifact_route_key(a) for a in artifacts]
    if any(k is None for k in route_keys):
        errors.append(
            "one or more artifacts could not be classified into a "
            "(runtime, agent, profile) route key (missing subject/request fields)"
        )
        return errors

    key_counts: dict[str, int] = {}
    for key in route_keys:
        key_counts[key] = key_counts.get(key, 0) + 1
    duplicates = sorted(key for key, count in key_counts.items() if count > 1)
    if duplicates:
        errors.append(f"duplicate route artifacts present: {duplicates}")
    unexpected = sorted(set(key_counts) - REQUIRED_ROUTE_KEYS)
    if unexpected:
        errors.append(f"unexpected route(s) not in the required six-route set: {unexpected}")
    missing = sorted(REQUIRED_ROUTE_KEYS - set(key_counts))
    if missing:
        errors.append(f"missing required route(s): {missing}")
    if len(artifacts) != len(REQUIRED_ROUTE_KEYS):
        errors.append(
            f"expected exactly {len(REQUIRED_ROUTE_KEYS)} artifacts for the aggregate close gate, "
            f"found {len(artifacts)}"
        )

    non_pass = [
        (artifact.get("_source_path", "<unknown>"), artifact.get("status"))
        for artifact in artifacts
        if artifact.get("status") != "pass"
    ]
    if non_pass:
        errors.append(
            f"aggregate close gate requires all required routes to be status=pass; "
            f"non-pass artifacts: {non_pass}"
        )

    batch_ids = {artifact.get("batch_run_id") for artifact in artifacts}
    if len(batch_ids) != 1 or None in batch_ids or "" in batch_ids:
        errors.append(
            f"all artifacts must share the same non-empty batch_run_id, "
            f"found: {sorted(str(b) for b in batch_ids)}"
        )

    head_shas = {(artifact.get("subject") or {}).get("head_sha") for artifact in artifacts}
    if len(head_shas) != 1 or None in head_shas:
        errors.append(
            f"all artifacts must share the same non-null subject.head_sha, "
            f"found: {sorted(str(h) for h in head_shas)}"
        )
    elif expected_head_sha is not None:
        (actual_head_sha,) = head_shas
        if actual_head_sha != expected_head_sha:
            errors.append(
                f"subject.head_sha {actual_head_sha!r} does not match "
                f"--expected-head-sha {expected_head_sha!r}"
            )

    for artifact in artifacts:
        request = artifact.get("request") or {}
        if request.get("profile") != "github_research":
            continue
        source = artifact.get("_source_path", "<unknown>")
        route_evidence = artifact.get("route_evidence") or {}
        sha256 = route_evidence.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            errors.append(
                f"{source}: github_research route_evidence.sha256 must be a real 64-hex "
                "digest (not null/placeholder) for the aggregate close gate to pass"
            )
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
    parser.add_argument(
        "--expected-head-sha",
        default=None,
        help=(
            "if set, the aggregate close gate additionally requires every "
            "artifact's subject.head_sha to equal this value"
        ),
    )
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

    # Issue #1886 P0-3 fix_delta: the aggregate close gate (exact six-route
    # set, all-pass, shared batch, shared head, real route_evidence digest)
    # runs whenever either merge-gate assertion flag is requested -- this is
    # precisely the invocation shape (both flags together) the PR #2005
    # adversarial review used to demonstrate the decisive false-pass repro
    # (six ``--dry-run`` SKIP artifacts passing aggregate validation).
    if args.require_native_spawn_event or args.assert_zero_gemini_and_fallback_invocations:
        failures.extend(assert_batch_close_gate(artifacts, expected_head_sha=args.expected_head_sha))

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
