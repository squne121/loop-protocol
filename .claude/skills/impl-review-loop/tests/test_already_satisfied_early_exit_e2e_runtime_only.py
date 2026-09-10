"""
AC12 runtime-verification e2e for the `already_satisfied` early-exit choke
point (Issue #2607, `## Runtime Verification Applicability: decision:
immediate`).

Proves, with NO mocking, that the single common choke point documented in
`preparation.md`'s "0-a-1. Already-Satisfied Early-Exit choke point" section
is reachable from the REAL production dispatch-chain routing functions --
`build_intake_capsule.py::_next_action_route()` (real, unmodified, Out of
Scope for #2607) and `evaluate_product_spec_gate.py::evaluate_product_spec_payload()`
(real, unmodified, invoked as an actual subprocess) -- for BOTH of the two
upstream route values the Issue calls out:

  - `next_action.route == "proceed_to_step_1"`
  - `product_spec_preflight.routing_action == "refresh_contract_snapshot"`

and that Step 1 (implementation-worker dispatch) is never reached in either
case once `base_ac_satisfied=true` and no PR exists for the fixture Issue.

This Issue's Allowed Paths do not include a new production script for the
choke point itself (only SKILL.md / route_loop_verdict_v2.py /
preparation.md / step-5 docs / these two test files). The choke-point
decision function therefore lives in `test_already_satisfied_routing.py`
(this directory) and is loaded here by absolute file path (importlib,
established pattern -- see e.g. `test_adjudicate_vc_result_e2e_runtime_only.py`
loading `adjudicate_vc_result.py` the same way) so both the unit-level
coverage there and this real-dispatch-chain e2e coverage exercise the exact
same, single-source-of-truth decision function.

SKIP / fallback policy (`docs/dev/runtime-verification-policy.md`):
  - `uv` / repo-local python3 unavailable, or `build_intake_capsule.py`'s
    dependency (a working repo-local git checkout) is broken -> SKIP
    (`pytest.skip("SKIP: ...")`, matching this repo's established
    directly-pytest-invoked-VC precedent, e.g.
    `.claude/skills/gemini-cli-headless-delegation/tests/
    test_agy_structured_output_capability_runtime.py`).
  - A `_*_fallback: true` marker anywhere in a real production script's
    JSON output is treated as FAIL, never PASS (asserted explicitly below).

Artifact: a real subprocess execution log is written to
`artifacts/runtime-verification-AC12-<timestamp>.log` (worktree-local,
gitignored -- Issue #2607 artifact_requirements).
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
IMPL_REVIEW_LOOP_DIR = TESTS_DIR.parent
REPO_ROOT = IMPL_REVIEW_LOOP_DIR.parents[2]

BUILD_INTAKE_CAPSULE_PATH = IMPL_REVIEW_LOOP_DIR / "scripts" / "build_intake_capsule.py"
EVALUATE_PRODUCT_SPEC_GATE_PATH = IMPL_REVIEW_LOOP_DIR / "scripts" / "evaluate_product_spec_gate.py"
ALREADY_SATISFIED_ROUTING_TEST_PATH = TESTS_DIR / "test_already_satisfied_routing.py"

_ARTIFACTS_DIR = REPO_ROOT / "artifacts"
_ARTIFACT_LOG_PATH = _ARTIFACTS_DIR / (
    f"runtime-verification-AC12-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
)


def _log(line: str) -> None:
    _ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with _ARTIFACT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def _environment_blocked_reason() -> str | None:
    if shutil.which("uv") is None:
        return "environment blocked: uv not found on PATH"
    if sys.executable is None:
        return "environment blocked: repo-local python3 unavailable"
    if not BUILD_INTAKE_CAPSULE_PATH.is_file():
        return "environment blocked: build_intake_capsule.py dependency missing"
    if not EVALUATE_PRODUCT_SPEC_GATE_PATH.is_file():
        return "environment blocked: evaluate_product_spec_gate.py dependency missing"
    if not (REPO_ROOT / ".git").exists():
        return "environment blocked: repo-local git checkout is broken"
    return None


_ENV_BLOCKED_REASON = _environment_blocked_reason()


@pytest.fixture(autouse=True)
def _artifact_log(request: pytest.FixtureRequest):
    if _ENV_BLOCKED_REASON is not None:
        _log(f"SKIP {request.node.name}: {_ENV_BLOCKED_REASON}")
        pytest.skip(f"SKIP: {_ENV_BLOCKED_REASON}")
    _log(f"START {request.node.name}")
    try:
        yield
    except Exception:
        _log(f"FAIL {request.node.name}")
        raise
    else:
        _log(f"PASS {request.node.name}")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_build_intake_capsule = _load_module("already_satisfied_e2e_build_intake_capsule", BUILD_INTAKE_CAPSULE_PATH)
_already_satisfied_routing = _load_module(
    "already_satisfied_e2e_routing_helper", ALREADY_SATISFIED_ROUTING_TEST_PATH
)


def _assert_no_fallback_markers(payload: Any) -> None:
    """Issue #2607 fallback_policy: `_*_fallback: true` anywhere in a real
    production script's JSON output must be treated as FAIL, never PASS."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert not (
                isinstance(key, str) and key.startswith("_") and key.endswith("_fallback") and value is True
            ), f"fallback marker detected: {key}=True -- must be treated as FAIL, not PASS"
            _assert_no_fallback_markers(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_fallback_markers(item)


def _run_evaluate_product_spec_gate(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Real subprocess execution of the unmodified, real production script
    (Out of Scope for #2607 -- `evaluate_product_spec_gate.py` itself is not
    edited by this Issue)."""
    completed = subprocess.run(
        [sys.executable, str(EVALUATE_PRODUCT_SPEC_GATE_PATH), "--snapshot-json", "-"],
        input=json.dumps(snapshot),
        capture_output=True,
        text=True,
        check=False,
    )
    _log(f"evaluate_product_spec_gate.py exit={completed.returncode} stdout={completed.stdout.strip()!r}")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    _assert_no_fallback_markers(payload)
    return payload


# ---------------------------------------------------------------------------
# Fixture: next_action.route == "proceed_to_step_1" (real
# build_intake_capsule.py::_next_action_route(), real production function,
# not re-implemented/mocked).
# ---------------------------------------------------------------------------


def _real_next_action_route_proceed_to_step_1() -> str:
    route = _build_intake_capsule._next_action_route(
        "pass", {"normalized_status": "go"}
    )
    assert route == "proceed_to_step_1", f"fixture assumption violated: got {route!r}"
    return route


# ---------------------------------------------------------------------------
# Fixture: product_spec_preflight.routing_action ==
# "refresh_contract_snapshot" (real evaluate_product_spec_gate.py, executed
# as an actual subprocess -- a malformed product_spec_check payload is the
# real, documented way this script itself produces refresh_contract_snapshot,
# per its own `_validate_product_spec_check_payload()` path).
# ---------------------------------------------------------------------------


def _real_product_spec_routing_action_refresh_contract_snapshot() -> str:
    malformed_snapshot = {
        "CONTRACT_REVIEW_RESULT_V1": {
            "issue_url": "https://github.com/squne121/loop-protocol/issues/2607",
            "checks": {
                "product_spec_check": {
                    "schema": "product_spec_check/v1",
                    # missing "applicability" / "decision" / "triggers" /
                    # "conditions" / "blocked_reasons" / "body_sha256" /
                    # "source_provenance" -> real validation_error path.
                },
            },
        },
        "body_sha256": "sha256:" + "0" * 64,
    }
    payload = _run_evaluate_product_spec_gate(malformed_snapshot)
    assert payload["routing_action"] == "refresh_contract_snapshot", (
        f"fixture assumption violated: got {payload['routing_action']!r}"
    )
    return payload["routing_action"]


# ---------------------------------------------------------------------------
# AC12 positive path: base_ac_satisfied=true + no PR -> Step 1 never reached,
# for BOTH real upstream route fixtures.
# ---------------------------------------------------------------------------


def test_ac12_proceed_to_step_1_route_never_reaches_step1_dispatch_when_already_satisfied():
    real_route = _real_next_action_route_proceed_to_step_1()

    calls: list[str] = []

    def _dispatch_implementation_worker() -> None:
        calls.append("dispatched")

    decision = _already_satisfied_routing.resolve_already_satisfied_early_exit_decision(
        next_action_route=real_route,
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=True,
    )
    if decision["dispatch_step1"]:
        _dispatch_implementation_worker()

    assert decision["early_exit"] is True
    assert decision["dispatch_step1"] is False
    assert calls == [], "implementation-worker dispatch must never fire"
    assert decision["recommendation"]["pr"]["action"] == "none"
    assert decision["result"]["termination_reason"] == "already_satisfied"


def test_ac12_refresh_contract_snapshot_routing_action_never_reaches_step1_dispatch_when_already_satisfied():
    real_routing_action = _real_product_spec_routing_action_refresh_contract_snapshot()

    calls: list[str] = []

    def _dispatch_implementation_worker() -> None:
        calls.append("dispatched")

    decision = _already_satisfied_routing.resolve_already_satisfied_early_exit_decision(
        next_action_route="proceed_to_step_1",
        product_spec_routing_action=real_routing_action,
        pr_exists=False,
        base_ac_satisfied=True,
    )
    if decision["dispatch_step1"]:
        _dispatch_implementation_worker()

    assert decision["early_exit"] is True
    assert decision["dispatch_step1"] is False
    assert calls == [], "implementation-worker dispatch must never fire"
    assert decision["recommendation"]["pr"]["action"] == "none"


# ---------------------------------------------------------------------------
# AC12 negative coverage: the choke point does not ALWAYS block -- it only
# fires under the documented two-condition gate, for both real fixtures.
# ---------------------------------------------------------------------------


def test_ac12_proceed_to_step_1_route_dispatches_step1_when_base_not_satisfied():
    real_route = _real_next_action_route_proceed_to_step_1()

    calls: list[str] = []

    def _dispatch_implementation_worker() -> None:
        calls.append("dispatched")

    decision = _already_satisfied_routing.resolve_already_satisfied_early_exit_decision(
        next_action_route=real_route,
        product_spec_routing_action="continue",
        pr_exists=False,
        base_ac_satisfied=False,
    )
    if decision["dispatch_step1"]:
        _dispatch_implementation_worker()

    assert decision["early_exit"] is False
    assert decision["dispatch_step1"] is True
    assert calls == ["dispatched"]


def test_ac12_refresh_contract_snapshot_routing_action_dispatches_step1_when_pr_already_exists():
    real_routing_action = _real_product_spec_routing_action_refresh_contract_snapshot()

    calls: list[str] = []

    def _dispatch_implementation_worker() -> None:
        calls.append("dispatched")

    decision = _already_satisfied_routing.resolve_already_satisfied_early_exit_decision(
        next_action_route="proceed_to_step_1",
        product_spec_routing_action=real_routing_action,
        pr_exists=True,
        base_ac_satisfied=True,
    )
    if decision["dispatch_step1"]:
        _dispatch_implementation_worker()

    assert decision["early_exit"] is False
    assert decision["dispatch_step1"] is True
    assert calls == ["dispatched"]
