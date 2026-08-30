"""Unit tests for the closed AGY advisory fallback routing core."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "route_agy_advisory_fallback.py"
_SPEC = importlib.util.spec_from_file_location("route_agy_advisory_fallback_test", _SCRIPT)
assert _SPEC and _SPEC.loader
route = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(route)


def _kind(failure_class: str | None) -> str | None:
    return {
        "agy_timeout": "operational",
        "agy_permission_denied": "policy_or_permission",
        "agy_empty_prompt": "contract",
    }.get(failure_class)


def _result(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": "delegation_result/v1",
        "provider": "agy",
        "ok": False,
        "failure_class": "agy_timeout",
        "agy_failure_kind": "operational",
        "agy_invocation_attempted": True,
    }
    result.update(overrides)
    return result


def test_advisory_operational_failure_is_the_only_native_route() -> None:
    decision = route.route_agy_advisory_fallback(
        _result(), requirement="advisory", canonical_failure_kind=_kind
    )

    assert decision == {
        "schema": "AGY_ADVISORY_FALLBACK_ROUTE_DECISION_V1",
        "schema_version": 1,
        "status": "degraded",
        "next_action": "native_non_mutating_fallback",
        "failure_class": "agy_timeout",
        "reason_code": "advisory_operational",
    }


@pytest.mark.parametrize(
    ("result", "reason_code"),
    [
        (_result(provider="gemini"), "non_agy_or_pre_agy"),
        (_result(agy_invocation_attempted=False), "non_agy_or_pre_agy"),
        (_result(failure_class="request_policy_denied"), "wrapper_result_invalid"),
        (_result(failure_class="agy_future_unclassified", agy_failure_kind="contract"), "deny_policy"),
        (_result(agy_failure_kind="policy_or_permission"), "deny_policy"),
    ],
)
def test_untrusted_or_mismatched_failure_pairs_fail_closed(
    result: dict[str, object], reason_code: str
) -> None:
    decision = route.route_agy_advisory_fallback(
        result, requirement="advisory", canonical_failure_kind=_kind
    )

    assert decision["status"] == "failed"
    assert decision["next_action"] == "fail_closed"
    assert decision["reason_code"] == reason_code


@pytest.mark.parametrize("failure_class", ["agy_permission_denied", "agy_empty_prompt"])
def test_policy_and_contract_failure_kinds_fail_closed(failure_class: str) -> None:
    decision = route.route_agy_advisory_fallback(
        _result(failure_class=failure_class, agy_failure_kind=_kind(failure_class)),
        requirement="advisory",
        canonical_failure_kind=_kind,
    )

    assert decision["status"] == "failed"
    assert decision["reason_code"] == "deny_policy"


def test_required_mode_never_degrades_to_native_fallback() -> None:
    decision = route.route_agy_advisory_fallback(
        _result(), requirement="explicitly_required", canonical_failure_kind=_kind
    )

    assert decision["status"] == "failed"
    assert decision["next_action"] == "fail_closed"
    assert decision["reason_code"] == "explicitly_required"


def test_success_requires_actual_agy_attempt_and_null_pair() -> None:
    decision = route.route_agy_advisory_fallback(
        _result(ok=True, failure_class=None, agy_failure_kind=None),
        requirement="advisory",
        canonical_failure_kind=_kind,
    )

    assert decision["status"] == "ok"
    assert decision["next_action"] == "continue_agy_result"
    assert decision["reason_code"] == "agy_success"


@pytest.mark.parametrize("payload", [b'{"a":1}\n', b'{"a":1}\n\n', b' {"a":1}', b'{"a":1}{"b":2}', b'{"a":1,"a":2}'])
def test_strict_json_byte_framing(payload: bytes) -> None:
    if payload == b'{"a":1}\n':
        assert route.strict_json_object_bytes(payload) == {"a": 1}
    else:
        with pytest.raises(route.ProtocolError):
            route.strict_json_object_bytes(payload)
