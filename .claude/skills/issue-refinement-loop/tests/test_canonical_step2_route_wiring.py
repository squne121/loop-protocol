"""Issue #2389 -- `route_canonical_step2_result()` CLI wiring.

Verifies:

* AC1: EVERY `_cmd_produce()` stdout JSON output path (success / body-fetch
  failure / VC-budget policy error / reviewer-transport failure /
  artifact-readback failure) carries a top-level `canonical_step2_route`
  field.
* AC2: `status: ok` + `verdict: approve` + `next_action: proceed` routes to
  `STEP_2_5` (NOT directly to `STEP_4_5`), so the Step 2.5 semantic design
  review applicability gate is never bypassed.
* AC3: `needs-fix` + `request_changes` routes to `STEP_4`;
  `next_action: human_judgment_required` routes to
  `STEP_5_HUMAN_JUDGMENT_REQUIRED` regardless of `verdict`.
* AC4: `status: input_or_runtime_error` with a KNOWN structured `error_code`
  (`reviewer_transport_environment_failure` / `artifact_readback_failed`)
  routes to `STEP_5_HUMAN_JUDGMENT_REQUIRED`; any other/unknown/missing
  `error_code` (including body-fetch and VC-budget errors, which remain
  Out of Scope per Issue #2389) routes to
  `FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE`.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_pipeline_module():
    spec = importlib.util.spec_from_file_location(
        "run_root_review_pipeline_canonical_step2_route_wiring", PIPELINE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_canonical_step2_route_wiring", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = "squne121/loop-protocol"

# Same fixture shape as `test_root_review_canonical_delivery.py`'s
# `_APPROVE_BODY` -- well-formed enough for the REAL checker chain
# (`check_issue_contract.py` / `contract_readiness_check.py` /
# `merge_readiness`) to synthesize a genuine `verdict: approve` result.
_APPROVE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2389 canonical_step2_route wiring fixture (approve branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
`canonical_step2_route` is wired into every stdout JSON output path
(Issue #2389).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for check_issue_contract.py to
      synthesize a complete REVIEW_ISSUE_RESULT_V1 with verdict approve.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/e2e_produce_canonical_step2_route_wiring_approve.md
"""


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_root_review_canonical_delivery.py` / `test_root_review_pipeline_readback_v2_ssot.py`
    E2E pattern. Returns the parsed stdout JSON regardless of `status`
    (unlike `_cmd_produce()`'s success-only callers, this helper is also
    used to observe error-path output)."""
    body_sha256 = _PIPELINE.sha256_of(body)
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return body, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    args = argparse.Namespace(issue_number=issue_number, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())
    return {"rc": rc, "out": out}


# ---------------------------------------------------------------------------
# AC1: `canonical_step2_route` is present on EVERY `_cmd_produce()` stdout
# JSON output path.
# ---------------------------------------------------------------------------


def test_produce_success_stdout_top_level_field_present(tmp_path: Path, monkeypatch):
    result = _run_real_produce(tmp_path, monkeypatch, body=_APPROVE_BODY, issue_number=2389001)
    out = result["out"]
    assert result["rc"] == 0, out
    assert out["status"] == "ok", out
    assert "canonical_step2_route" in out, "success path must carry top-level canonical_step2_route"
    assert out["canonical_step2_route"] == _PIPELINE.STEP_2_5


def test_produce_body_fetch_failure_stdout_top_level_field_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)

    def _fake_fetch_failure(issue_number_, repo, timeout_seconds=15):
        return None, None, "live_body_fetch_transport_error"

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch_failure)

    args = argparse.Namespace(issue_number=2389002, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())

    assert rc == 2
    assert out["status"] == "input_or_runtime_error", out
    assert "canonical_step2_route" in out, "body-fetch-failure path must carry top-level canonical_step2_route"
    # Issue #2389 Out of Scope: body-fetch failure stays fail-closed, it is
    # NOT one of the two known human-judgment-eligible producer error codes.
    assert out["canonical_step2_route"] == _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE


def test_produce_vc_budget_error_stdout_top_level_field_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)
    body_sha256 = _PIPELINE.sha256_of(_APPROVE_BODY)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return _APPROVE_BODY, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    def _raise_budget_error(*args, **kwargs):
        raise _PIPELINE._VerificationBudgetExceedsPolicyError(999, 10)

    monkeypatch.setattr(_PIPELINE, "_compute_canonical_vc_plan", _raise_budget_error)

    args = argparse.Namespace(issue_number=2389003, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())

    assert rc == 2
    assert out["status"] == "input_or_runtime_error", out
    assert out["error_code"] == "verification_budget_exceeds_policy", out
    assert "canonical_step2_route" in out, "VC-budget-error path must carry top-level canonical_step2_route"
    # Issue #2389 Out of Scope: VC-budget errors stay fail-closed, they are
    # NOT one of the two known human-judgment-eligible producer error codes.
    assert out["canonical_step2_route"] == _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE


def test_produce_transport_failure_stdout_top_level_field_present(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)
    body_sha256 = _PIPELINE.sha256_of(_APPROVE_BODY)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return _APPROVE_BODY, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    def _fake_run_reviewer_transport(**kwargs):
        return {"transport_status": "failed", "invocation_id": "test-forced-transport-failure"}

    monkeypatch.setattr(_PIPELINE._reviewer_transport, "run_reviewer_transport", _fake_run_reviewer_transport)

    args = argparse.Namespace(issue_number=2389004, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())

    assert rc == 2
    assert out["status"] == "input_or_runtime_error", out
    assert out["error_code"] == "reviewer_transport_environment_failure", out
    assert "canonical_step2_route" in out, "transport-failure path must carry top-level canonical_step2_route"
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED


def test_produce_artifact_readback_failure_stdout_top_level_field_present(tmp_path: Path, monkeypatch):
    """`run_reviewer_transport()` performs its OWN internal `verify_artifact()`
    call as part of validating each attempt (see `reviewer_transport.py`);
    `_cmd_produce()` then performs a SEPARATE, independent re-verification
    readback of its own (its own module-docstring rationale: it "never
    trusts the child's `compact` fields without an independent
    artifact-bytes readback of its own"). To exercise `_cmd_produce()`'s
    OWN readback failure branch specifically (not the inner transport's
    unrelated attempt-validation gate), the first `verify_artifact()` call
    (the inner transport's) is allowed to genuinely succeed and only the
    SECOND call (`_cmd_produce()`'s own) is forced invalid."""
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", tmp_path)
    body_sha256 = _PIPELINE.sha256_of(_APPROVE_BODY)

    def _fake_fetch(issue_number_, repo, timeout_seconds=15):
        return _APPROVE_BODY, body_sha256, None

    monkeypatch.setattr(_PIPELINE, "fetch_and_pin_live_body", _fake_fetch)

    real_verify_artifact = _PIPELINE._reviewer_transport.verify_artifact
    call_count = {"n": 0}

    def _fake_verify_artifact(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_verify_artifact(**kwargs)
        return {"status": "invalid", "reason": "test_forced_invalid_readback"}

    monkeypatch.setattr(_PIPELINE._reviewer_transport, "verify_artifact", _fake_verify_artifact)

    args = argparse.Namespace(issue_number=2389005, repo=_REPO)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(args)
    out = json.loads(buf.getvalue())

    assert rc == 2
    assert out["status"] == "input_or_runtime_error", out
    assert out["error_code"] == "artifact_readback_failed", out
    assert "canonical_step2_route" in out, "artifact-readback-failure path must carry top-level canonical_step2_route"
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED


# ---------------------------------------------------------------------------
# AC2: `status: ok` + `verdict: approve` + `next_action: proceed` routes to
# `STEP_2_5`, not directly to `STEP_4_5` (the Step 2.5 semantic design review
# applicability gate must never be bypassed by the routing table itself).
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_approve_routes_to_step_2_5():
    result = {"status": "ok", "compact_result": {"verdict": "approve", "next_action": "proceed"}}
    assert _PIPELINE.route_canonical_step2_result(result) == _PIPELINE.STEP_2_5
    # Regression guard: approve+proceed must NOT still resolve to the old
    # STEP_4_5 target (that would silently reintroduce the Step 2.5 bypass
    # Issue #2389 closes).
    assert _PIPELINE.route_canonical_step2_result(result) != _PIPELINE.STEP_4_5


# ---------------------------------------------------------------------------
# AC3: `needs-fix` + `request_changes` -> STEP_4;
# `next_action: human_judgment_required` -> STEP_5_HUMAN_JUDGMENT_REQUIRED,
# independent of `verdict`.
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_needs_fix_and_human_judgment_routes_correctly():
    needs_fix_result = {"status": "ok", "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"}}
    assert _PIPELINE.route_canonical_step2_result(needs_fix_result) == _PIPELINE.STEP_4

    for verdict in ("needs-fix", "approve"):
        human_judgment_result = {
            "status": "ok",
            "compact_result": {"verdict": verdict, "next_action": "human_judgment_required"},
        }
        assert (
            _PIPELINE.route_canonical_step2_result(human_judgment_result)
            == _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED
        )


# ---------------------------------------------------------------------------
# AC4: known producer `error_code`s (`reviewer_transport_environment_failure`
# / `artifact_readback_failed`) route to STEP_5_HUMAN_JUDGMENT_REQUIRED;
# every other/unknown/missing `error_code` routes to
# FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_code,expected_attr",
    [
        ("reviewer_transport_environment_failure", "STEP_5_HUMAN_JUDGMENT_REQUIRED"),
        ("artifact_readback_failed", "STEP_5_HUMAN_JUDGMENT_REQUIRED"),
        ("some_unrecognized_error_code", "FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE"),
        ("verification_budget_exceeds_policy", "FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE"),
        (None, "FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE"),
    ],
    ids=[
        "known_reviewer_transport_environment_failure",
        "known_artifact_readback_failed",
        "unrecognized_error_code",
        "vc_budget_error_code_out_of_scope_stays_fail_closed",
        "missing_error_code",
    ],
)
def test_route_canonical_step2_result_producer_error_route_distinction(error_code, expected_attr):
    result: dict = {"status": "input_or_runtime_error"}
    if error_code is not None:
        result["error_code"] = error_code
    assert _PIPELINE.route_canonical_step2_result(result) == getattr(_PIPELINE, expected_attr)
