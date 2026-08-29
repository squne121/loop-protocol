#!/usr/bin/env python3
"""Tests for `latitude_runtime_evidence/v1` (schema/validator) and the bounded
Latitude CLI collector `collect_snapshot.collect_latitude_runtime_evidence` (Issue #2375; PR #2392
fix_delta -- corrected against the REAL, locally-verified `latitude` CLI v7.10.0: `latitude traces
list --project-slug <slug> --filters <JSON> --limit <n> --format json`, response shape
`{items: [...], nextCursor, hasMore}`, NOT the fictitious `latitude runs list` command/flat
`{trace_count, span_count, duration_ms}` response this Issue's code originally guessed).

Covers:
  AC1 `latitude_runtime_evidence/v1` validator: closed key set, availability-conditioned
      nullability, closed reason_code enum, canonical public identity recomputation
      (identity/evidence_ref mismatch, unknown schema_version -- both fail closed).
  AC2 collector: at most one allowlisted-argv Latitude CLI launch, Collection Budget
      (10s timeout / 64 KiB output / 3 allowlisted metrics) enforcement without raw output.
      Session correlation (`session_id`/`project_slug` resolution, `no_matching_trace`/
      `session_id_unresolved`/`project_slug_unresolved`) is exercised here too.
  AC5 no public `source_kind` field / `latitude_otlp` literal introduced by this Issue's
      new code (existing #1223 public `source_kind` enum boundary left untouched).

Fixture/mock-based only; the real `latitude` executable is never invoked here (that is
`verify_latitude_runtime_evidence_live_cli.py`'s opt-in, AC6 responsibility).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import collect_snapshot as cs  # noqa: E402
import validate_retrospective_schema as vrs  # noqa: E402

_COLLECTOR_VERSION = "latitude-collector/v1"
_COLLECTED_AT = "2026-08-29T00:00:00Z"
_METRICS = {"trace_count": 5, "span_count": 12, "duration_ms": 340}

#: fixed test-only session_id/project_slug -- every AC2 collector test below supplies these
#: explicitly (never relying on `LATITUDE_PROJECT` env var / process env), so tests are hermetic
#: regardless of the real environment's Latitude configuration.
_TEST_SESSION_ID = "test-session-id-0001"
_TEST_PROJECT_SLUG = "test-project-slug"


def _traces_response(
    *, span_count: Any = 12, duration_ns: Any = 340_000_000, session_id: str = _TEST_SESSION_ID
) -> str:
    """Builds a real-CLI-shaped `latitude traces list --format json` response body
    (`{items: [...], nextCursor, hasMore}`) with exactly one item -- the shape this collector's
    `runner` fake stands in for."""
    return json.dumps(
        {
            "items": [
                {
                    "traceId": "0" * 31 + "1",
                    "sessionId": session_id,
                    "spanCount": span_count,
                    "durationNs": duration_ns,
                }
            ],
            "nextCursor": None,
            "hasMore": False,
        }
    )


def _empty_traces_response() -> str:
    return json.dumps({"items": [], "nextCursor": None, "hasMore": False})


def _make_available_instance(
    *, collector_version: str = _COLLECTOR_VERSION, metrics: dict[str, Any] | None = None
) -> dict[str, Any]:
    metrics = metrics if metrics is not None else dict(_METRICS)
    ref = vrs.compute_latitude_evidence_ref(collector_version, metrics, _COLLECTED_AT)
    identity = vrs.compute_latitude_evidence_identity(collector_version, ref, metrics)
    return {
        "schema_version": "latitude_runtime_evidence/v1",
        "availability": "available",
        "collected_at": _COLLECTED_AT,
        "collector_version": collector_version,
        "evidence_identity": identity,
        "evidence_ref": ref,
        "metrics": metrics,
        "reason_code": None,
    }


def _make_unavailable_instance(reason_code: str = "cli_not_found") -> dict[str, Any]:
    return {
        "schema_version": "latitude_runtime_evidence/v1",
        "availability": "unavailable",
        "collected_at": _COLLECTED_AT,
        "collector_version": _COLLECTOR_VERSION,
        "evidence_identity": None,
        "evidence_ref": None,
        "metrics": {"trace_count": None, "span_count": None, "duration_ms": None},
        "reason_code": reason_code,
    }


# ---------------------------------------------------------------------------
# AC1: schema + validator
# ---------------------------------------------------------------------------


def test_available_instance_is_schema_valid():
    vrs.validate_latitude_runtime_evidence(_make_available_instance())


def test_unavailable_instance_is_schema_valid():
    vrs.validate_latitude_runtime_evidence(_make_unavailable_instance())


def test_error_availability_with_reason_code_is_schema_valid():
    instance = _make_unavailable_instance(reason_code="budget_exceeded")
    instance["availability"] = "error"
    vrs.validate_latitude_runtime_evidence(instance)


@pytest.mark.parametrize(
    "raw_field",
    [
        "raw_trace",
        "raw_transcript",
        "prompt",
        "message",
        "tool_input",
        "tool_output",
        "stdout",
        "stderr",
        "authorization",
        "token",
        "secret",
        "absolute_path",
    ],
)
def test_schema_rejects_unknown_raw_field_top_level(raw_field: str):
    instance = _make_available_instance()
    instance[raw_field] = "untrusted raw content that must never be schema-allowed"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_schema_rejects_unknown_metric_field():
    instance = _make_available_instance()
    instance["metrics"]["extra_metric"] = 1
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_schema_rejects_non_closed_availability_value():
    instance = _make_available_instance()
    instance["availability"] = "maybe"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_schema_rejects_arbitrary_reason_code_not_in_closed_enum():
    instance = _make_unavailable_instance(reason_code="totally_made_up_reason")
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_available_requires_non_null_evidence_identity():
    instance = _make_available_instance()
    instance["evidence_identity"] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_available_requires_non_null_evidence_ref():
    instance = _make_available_instance()
    instance["evidence_ref"] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_available_requires_null_reason_code():
    instance = _make_available_instance()
    instance["reason_code"] = "cli_not_found"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


@pytest.mark.parametrize("metric_key", ["trace_count", "span_count", "duration_ms"])
def test_available_requires_non_null_metric(metric_key: str):
    """PR #2392 review blocker: `availability: "available"` must require ALL 3 allowlisted
    metrics (not just `evidence_identity`/`evidence_ref`) to be non-null, matching the Issue's
    `## latitude_runtime_evidence/v1` contract text."""
    instance = _make_available_instance()
    instance["metrics"][metric_key] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_unavailable_requires_null_evidence_identity():
    instance = _make_unavailable_instance()
    instance["evidence_identity"] = "sha256:" + "0" * 64
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_unavailable_requires_null_evidence_ref():
    instance = _make_unavailable_instance()
    instance["evidence_ref"] = "opaque-ref"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_unavailable_requires_null_metrics():
    instance = _make_unavailable_instance()
    instance["metrics"]["trace_count"] = 1
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_unavailable_requires_non_null_reason_code():
    instance = _make_unavailable_instance()
    instance["reason_code"] = None
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_evidence_identity_pattern_enforced():
    instance = _make_available_instance()
    instance["evidence_identity"] = "not-a-sha256-digest"
    with pytest.raises(jsonschema.exceptions.ValidationError):
        vrs.validate_latitude_runtime_evidence(instance)


def test_identity_mismatch_fails_closed():
    """A caller cannot supply an arbitrary/stale evidence_identity and have it pass --
    it must equal compute_latitude_evidence_identity(collector_version, evidence_ref, metrics)."""
    instance = _make_available_instance()
    instance["evidence_identity"] = "sha256:" + "1" * 64
    with pytest.raises(vrs.RetrospectiveSchemaError, match="evidence_identity mismatch"):
        vrs.validate_latitude_runtime_evidence(instance)


def test_evidence_ref_mismatch_fails_closed():
    instance = _make_available_instance()
    instance["evidence_ref"] = "sha256:" + "2" * 64
    with pytest.raises(vrs.RetrospectiveSchemaError, match="evidence_ref mismatch"):
        vrs.validate_latitude_runtime_evidence(instance)


def test_unknown_schema_version_fails_closed():
    instance = _make_available_instance()
    instance["schema_version"] = "latitude_runtime_evidence/v2"
    with pytest.raises(vrs.RetrospectiveSchemaError, match="unknown latitude_runtime_evidence schema_version"):
        vrs.validate_latitude_runtime_evidence(instance)


def test_missing_schema_version_fails_closed():
    instance = _make_available_instance()
    del instance["schema_version"]
    with pytest.raises(vrs.RetrospectiveSchemaError, match="unknown latitude_runtime_evidence schema_version"):
        vrs.validate_latitude_runtime_evidence(instance)


def test_is_valid_latitude_runtime_evidence_true_for_valid_instance():
    assert vrs.is_valid_latitude_runtime_evidence(_make_available_instance()) is True


def test_is_valid_latitude_runtime_evidence_false_for_invalid_instance():
    instance = _make_available_instance()
    instance["evidence_identity"] = "sha256:" + "9" * 64
    assert vrs.is_valid_latitude_runtime_evidence(instance) is False


def test_identity_and_ref_are_deterministic():
    ref1 = vrs.compute_latitude_evidence_ref(_COLLECTOR_VERSION, dict(_METRICS), _COLLECTED_AT)
    ref2 = vrs.compute_latitude_evidence_ref(_COLLECTOR_VERSION, dict(_METRICS), _COLLECTED_AT)
    assert ref1 == ref2
    identity1 = vrs.compute_latitude_evidence_identity(_COLLECTOR_VERSION, ref1, dict(_METRICS))
    identity2 = vrs.compute_latitude_evidence_identity(_COLLECTOR_VERSION, ref2, dict(_METRICS))
    assert identity1 == identity2


def test_identity_changes_when_metrics_differ():
    metrics_a = {"trace_count": 1, "span_count": 1, "duration_ms": 1}
    metrics_b = {"trace_count": 2, "span_count": 1, "duration_ms": 1}
    ref_a = vrs.compute_latitude_evidence_ref(_COLLECTOR_VERSION, metrics_a, _COLLECTED_AT)
    ref_b = vrs.compute_latitude_evidence_ref(_COLLECTOR_VERSION, metrics_b, _COLLECTED_AT)
    assert ref_a != ref_b


# ---------------------------------------------------------------------------
# AC2: bounded, argv-only, read-only collector
# ---------------------------------------------------------------------------


def _collect(**kwargs: Any) -> dict[str, Any]:
    """Convenience wrapper defaulting `session_id`/`project_slug` to the fixed test constants --
    every test below that wants to exercise past-the-gate behavior (CLI launch, response parsing)
    calls this instead of `cs.collect_latitude_runtime_evidence` directly."""
    kwargs.setdefault("session_id", _TEST_SESSION_ID)
    kwargs.setdefault("project_slug", _TEST_PROJECT_SLUG)
    return cs.collect_latitude_runtime_evidence(**kwargs)


def test_collector_returns_session_id_unresolved_when_session_id_missing():
    """PR #2392 fix_delta Session Correlation: no target session_id resolvable -> the collector
    never launches the CLI (no "latest trace" fallback)."""
    calls: list[list[str]] = []

    def unexpected_runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        raise AssertionError("collector must not launch the CLI when session_id is unresolved")

    result = cs.collect_latitude_runtime_evidence(
        which=lambda n: "/usr/bin/latitude", runner=unexpected_runner, project_slug=_TEST_PROJECT_SLUG, session_id=None
    )
    assert calls == []
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == "session_id_unresolved"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_returns_project_slug_unresolved_when_project_slug_missing():
    calls: list[list[str]] = []

    def unexpected_runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        raise AssertionError("collector must not launch the CLI when project_slug is unresolved")

    result = cs.collect_latitude_runtime_evidence(
        which=lambda n: "/usr/bin/latitude",
        runner=unexpected_runner,
        session_id=_TEST_SESSION_ID,
        project_slug=None,
        project_slug_resolver=lambda: None,
    )
    assert calls == []
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == "project_slug_unresolved"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_project_slug_defaults_to_resolver():
    """When `project_slug` is omitted, the collector calls `project_slug_resolver` (defaulting to
    reading `LATITUDE_PROJECT`) -- injecting a fake resolver here keeps the test hermetic."""
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(), stderr="")

    result = cs.collect_latitude_runtime_evidence(
        which=lambda n: "/usr/bin/latitude",
        runner=fake_runner,
        session_id=_TEST_SESSION_ID,
        project_slug_resolver=lambda: "resolved-project-slug",
    )
    assert result["availability"] == "available"
    assert "--project-slug" in captured[0]
    assert captured[0][captured[0].index("--project-slug") + 1] == "resolved-project-slug"


def test_collector_returns_cli_not_found_when_executable_missing():
    result = _collect(which=lambda name: None)
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == "cli_not_found"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_launches_cli_at_most_once_on_success():
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=fake_runner)
    assert len(calls) == 1
    assert result["availability"] == "available"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_uses_only_the_allowlisted_argv_shape():
    captured: list[list[str]] = []

    def fake_runner(argv: list[str]) -> subprocess.CompletedProcess:
        captured.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(), stderr="")

    _collect(which=lambda n: "/usr/bin/latitude", runner=fake_runner)
    expected = cs.build_latitude_allowed_argv(
        project_slug=_TEST_PROJECT_SLUG, session_id=_TEST_SESSION_ID, limit=cs.LATITUDE_DEFAULT_LIMIT
    )
    assert captured == [expected]
    # argv-only: no shell metacharacters/strings, always a list of plain tokens.
    assert all(isinstance(token, str) and ";" not in token and "|" not in token for token in captured[0])


def test_build_latitude_allowed_argv_shape_is_fixed_with_3_dynamic_slots():
    """The argv shape has exactly 11 fixed-position tokens; only the values at the
    `--project-slug`/`--filters`/`--limit` positions vary with the caller-supplied inputs -- every
    flag token is a fixed literal, and the `--filters` value is always `json.dumps` output (never a
    hand-built/concatenated string), so a session_id containing quotes/braces cannot inject an
    additional flag or filter condition."""
    argv = cs.build_latitude_allowed_argv(project_slug="proj-a", session_id="sess-a", limit=1)
    assert argv == [
        "latitude",
        "traces",
        "list",
        "--project-slug",
        "proj-a",
        "--filters",
        json.dumps({"sessionId": [{"op": "eq", "value": "sess-a"}]}, sort_keys=True),
        "--limit",
        "1",
        "--format",
        "json",
    ]
    assert json.loads(argv[6]) == {"sessionId": [{"op": "eq", "value": "sess-a"}]}


def test_build_latitude_allowed_argv_session_id_cannot_inject_extra_argv_tokens():
    hostile_session_id = '"} malicious {"sessionId": [{"op": "eq", "value": "other'
    argv = cs.build_latitude_allowed_argv(project_slug="proj-a", session_id=hostile_session_id, limit=1)
    # still exactly 11 tokens -- the hostile value is confined to a single JSON string value, not
    # split into additional argv entries.
    assert len(argv) == 11
    assert json.loads(argv[6]) == {"sessionId": [{"op": "eq", "value": hostile_session_id}]}


def test_default_runner_never_uses_a_shell_and_disables_stdin_prompts(monkeypatch):
    captured_kwargs: dict[str, Any] = {}

    def fake_subprocess_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        captured_kwargs["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(cs.subprocess, "run", fake_subprocess_run)
    argv = cs.build_latitude_allowed_argv(project_slug=_TEST_PROJECT_SLUG, session_id=_TEST_SESSION_ID)
    cs._default_latitude_runner(argv)
    assert isinstance(captured_kwargs["argv"], list)
    assert captured_kwargs.get("shell", False) is False
    assert captured_kwargs["stdin"] == cs.subprocess.DEVNULL
    assert captured_kwargs["timeout"] == cs.LATITUDE_TIMEOUT_SECONDS


def test_collector_returns_timeout_on_subprocess_timeout():
    def timeout_runner(argv: list[str]) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=cs.LATITUDE_TIMEOUT_SECONDS)

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=timeout_runner)
    assert result == {
        "schema_version": "latitude_runtime_evidence/v1",
        "availability": "unavailable",
        "collected_at": result["collected_at"],
        "collector_version": cs.LATITUDE_COLLECTOR_VERSION,
        "evidence_identity": None,
        "evidence_ref": None,
        "metrics": {"trace_count": None, "span_count": None, "duration_ms": None},
        "reason_code": "timeout",
    }
    vrs.validate_latitude_runtime_evidence(result)


@pytest.mark.parametrize(
    "stderr_text,expected_reason",
    [
        ("Error: unauthorized, please run `latitude login`", "auth_failed"),
        ("FATAL: could not resolve host api.latitude.example", "network_unavailable"),
        ("Error: unexpected internal server error", "non_zero_exit"),
    ],
)
def test_collector_classifies_non_zero_exit_by_reason(stderr_text: str, expected_reason: str):
    def fail_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr_text)

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=fail_runner)
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == expected_reason
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_never_retains_stderr_text_on_non_zero_exit():
    secret_marker = "SECRET_TOKEN_MUST_NOT_LEAK_abc123"

    def fail_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=f"unauthorized: {secret_marker}")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=fail_runner)
    assert secret_marker not in repr(result)


def test_collector_budget_exceeded_on_oversized_stdout():
    def big_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout="x" * (cs.LATITUDE_MAX_OUTPUT_BYTES + 1), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=big_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "budget_exceeded"
    assert "x" * 100 not in repr(result)
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_budget_exceeded_on_oversized_stderr():
    def big_stderr_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="y" * (cs.LATITUDE_MAX_OUTPUT_BYTES + 1))

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=big_stderr_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "budget_exceeded"


def test_collector_malformed_output_not_json():
    def bad_json_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout="not-json{{{", stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=bad_json_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_malformed_output_json_array_not_object():
    def array_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout="[1, 2, 3]", stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=array_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"


def test_collector_malformed_output_missing_items_key():
    """Real-CLI-shape parser: a JSON object without an `items` key/array is malformed_output, not
    silently treated as `{"items": []}`."""

    def no_items_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout='{"hasMore": false}', stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=no_items_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"


def test_collector_malformed_output_items_not_a_list():
    def bad_items_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout='{"items": "not-a-list"}', stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=bad_items_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"


def test_collector_zero_matching_traces_is_unavailable_no_matching_trace_not_error():
    """PR #2392 fix_delta: a well-formed, zero-item response is a legitimate `unavailable`
    outcome (the session genuinely has no attributed trace yet), never `error`/`malformed_output`,
    and never silently substituted with an unrelated trace."""

    def empty_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=_empty_traces_response(), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=empty_runner)
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == "no_matching_trace"
    assert result["metrics"] == {"trace_count": None, "span_count": None, "duration_ms": None}
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_success_projects_only_allowlisted_metrics():
    payload = json.dumps(
        {
            "items": [
                {
                    "traceId": "SECRET_SHOULD_NOT_LEAK",
                    "sessionId": _TEST_SESSION_ID,
                    "spanCount": 12,
                    "durationNs": 340_000_000,
                    "credential": "tok_abcdef",
                    "prompt": "system prompt text",
                    "absolute_path": "/home/user/.latitude/config",
                }
            ],
            "nextCursor": None,
            "hasMore": False,
        }
    )

    def success_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=success_runner)
    assert result["availability"] == "available"
    assert set(result["metrics"].keys()) == {"trace_count", "span_count", "duration_ms"}
    assert result["metrics"] == {"trace_count": 1, "span_count": 12, "duration_ms": 340}
    serialized = repr(result)
    for forbidden in ("SECRET_SHOULD_NOT_LEAK", "tok_abcdef", "system prompt text", "/home/user/.latitude/config"):
        assert forbidden not in serialized
    vrs.validate_latitude_runtime_evidence(result)


@pytest.mark.parametrize(
    "duration_ns,expected_duration_ms",
    [
        (0, 0),
        (1_000_000, 1),
        (1_999_999, 1),  # floor division, not rounding
        (21_903_000_000, 21_903),
        (21_903_000_000.0, 21_903),  # the real CLI's `durationNs` is JSON `number` (may be float)
    ],
)
def test_duration_ns_to_ms_floor_conversion(duration_ns: Any, expected_duration_ms: int):
    def runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(duration_ns=duration_ns), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=runner)
    assert result["availability"] == "available"
    assert result["metrics"]["duration_ms"] == expected_duration_ms


def test_collector_negative_span_count_becomes_malformed_output_error():
    """A single out-of-range metric (coerced to null by `_coerce_latitude_metric`) must not
    surface as `availability: "available"` with a null metric -- Issue #2375's
    `latitude_runtime_evidence/v1` contract requires all 3 allowlisted metrics to be non-null
    integers whenever `availability == "available"`; the whole result normalizes to a closed
    `error`/`malformed_output` instead (see PR #2392 review blocker)."""

    def negative_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(span_count=-1), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=negative_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"
    assert result["metrics"] == {"trace_count": None, "span_count": None, "duration_ms": None}
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_boolean_span_count_becomes_malformed_output_error():
    """Same as above for a boolean (not `int`) metric value coerced to null."""

    def bool_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(span_count=True), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=bool_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"
    assert result["metrics"] == {"trace_count": None, "span_count": None, "duration_ms": None}
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_missing_span_count_key_becomes_malformed_output_error():
    """A parseable-but-key-missing CLI response item (missing `spanCount`) must not surface as
    `availability: "available"` with the missing metric null -- it normalizes to a closed
    `error`/`malformed_output` result instead (Issue #2375 PR #2392 review blocker:
    `latitude_runtime_evidence/v1` requires all 3 allowlisted metrics to be non-null integers
    whenever `availability == "available"`)."""

    def partial_runner(argv: list[str]) -> subprocess.CompletedProcess:
        payload = json.dumps(
            {"items": [{"sessionId": _TEST_SESSION_ID, "durationNs": 1_000_000}], "nextCursor": None, "hasMore": False}
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=partial_runner)
    assert result["availability"] == "error"
    assert result["reason_code"] == "malformed_output"
    assert result["metrics"] == {"trace_count": None, "span_count": None, "duration_ms": None}
    vrs.validate_latitude_runtime_evidence(result)


_FIXTURES_DIR = _SCRIPTS_DIR.parent / "schemas" / "fixtures"


def _load_fixture_json(name: str) -> dict[str, Any]:
    with (_FIXTURES_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_collector_parses_the_real_cli_derived_with_match_golden_fixture():
    """PR #2392 fix_delta golden fixture: an ANONYMIZED, real-CLI-derived (latitude v7.10.0)
    `latitude traces list` response (real field names/value types/magnitudes, fake IDs) --
    anchors the parser to the REAL response shape instead of an invented one."""
    fixture = _load_fixture_json("latitude_traces_list_response.v1.with_match.example.json")
    fixture.pop("_comment", None)

    def fixture_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fixture), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=fixture_runner)
    assert result["availability"] == "available"
    item = fixture["items"][0]
    assert result["metrics"] == {
        "trace_count": 1,
        "span_count": item["spanCount"],
        "duration_ms": item["durationNs"] // 1_000_000,
    }
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_parses_the_real_cli_derived_no_match_golden_fixture():
    fixture = _load_fixture_json("latitude_traces_list_response.v1.no_match.example.json")
    fixture.pop("_comment", None)

    def fixture_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(fixture), stderr="")

    result = _collect(which=lambda n: "/usr/bin/latitude", runner=fixture_runner)
    assert result["availability"] == "unavailable"
    assert result["reason_code"] == "no_matching_trace"
    vrs.validate_latitude_runtime_evidence(result)


def test_collector_result_is_deterministic_given_same_inputs():
    def success_runner(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout=_traces_response(), stderr="")

    def fixed_clock():
        return datetime(2026, 8, 29, tzinfo=timezone.utc)

    result_a = _collect(which=lambda n: "/usr/bin/latitude", runner=success_runner, clock=fixed_clock)
    result_b = _collect(which=lambda n: "/usr/bin/latitude", runner=success_runner, clock=fixed_clock)
    assert result_a == result_b


# ---------------------------------------------------------------------------
# AC5: no public `source_kind` / `latitude_otlp` leakage from this Issue's new code
# ---------------------------------------------------------------------------

_NEW_FILES_TO_SCAN = (
    _SCRIPTS_DIR / "collect_snapshot.py",
    _SCRIPTS_DIR / "validate_retrospective_schema.py",
    _SCRIPTS_DIR / "run_retrospective.py",
    _SCRIPTS_DIR.parent / "schemas" / "latitude_runtime_evidence_v1.schema.json",
)


@pytest.mark.parametrize("path", _NEW_FILES_TO_SCAN)
def test_no_public_source_kind_or_latitude_otlp_literal_introduced(path: Path):
    text = path.read_text(encoding="utf-8")
    assert "latitude_otlp" not in text, f"{path} must not introduce the private latitude_otlp source_kind"
    assert "source_kind" not in text, f"{path} must not introduce a public source_kind field"


def test_agent_retrospective_run_schema_has_no_source_kind_property():
    """Issue #2375 AC5: existing agent_retrospective_run/v1 public schema (outside this
    Issue's Allowed Paths, never touched) must remain unchanged with respect to source_kind."""
    run_schema = vrs.load_run_schema()
    assert "source_kind" not in run_schema.get("properties", {})
    assert list(run_schema.get("properties", {}).keys()) == ["run_identity", "source_observations"]
