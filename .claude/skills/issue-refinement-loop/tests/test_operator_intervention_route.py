"""Issue #2397 -- `route_canonical_step2_result()` operator intervention
route.

`merged_review_result.failure_class == "contract_readiness_human_judgment"`
marks an environment/tool/timeout/unknown-classification readiness state
(`contract_readiness_check.py` -- e.g. `env_missing_dep`, `timeout`,
`unknown` classification, `command_not_allowed`, `package_manager_no_tty_prompt`,
regression-gate failure, validator internal/timeout errors, body-retrieval
failure) that a Step 4 Issue-body rewrite cannot fix. This is an
operator-intervention condition, not a genuine semantic/owner-ambiguity
judgment call (that remains `STEP_5_HUMAN_JUDGMENT_REQUIRED`, unchanged by
this Issue).

Verifies:

* AC1: a `status: ok` + `verdict: needs-fix` + `next_action: request_changes`
  payload whose SAME-call `merged_review_result.failure_class` is exactly
  `"contract_readiness_human_judgment"` routes to
  `STEP_5_OPERATOR_INTERVENTION_REQUIRED`, both at the pure-function level
  (`route_canonical_step2_result()` called directly) and via a REAL
  `_cmd_produce()` CLI invocation (the whole real checker subprocess chain
  -- `check_issue_contract.py` -> `contract_readiness_check.py` ->
  `merge_readiness` -> `reviewer_transport.run_reviewer_transport()` ->
  `build_compact_v2()`/`validate_compact_v2()` -> artifact write/verify ->
  `_cmd_produce()`'s own independent readback -- runs for real; only the
  live GitHub body fetch is monkeypatched, matching the existing
  `test_root_review_canonical_delivery.py` E2E pattern). This is the
  reachability proof the Issue #2397 background (OWNER PR #2391 review
  P0-1) requires: the routing decision is exercised against the SAME
  payload shape the real producer emits, not a hand-authored stand-in.
* AC2: the same `needs-fix` + `request_changes` triple with
  `merged_review_result` missing OR its `failure_class` missing/`None`
  still routes to `STEP_4` (Issue #2397 does not change this existing
  default; a body-rewrite-fixable `needs-fix` result has no readiness
  `failure_class` at all).
* AC3: an unrecognized non-empty `failure_class`, or a non-`Mapping`
  `merged_review_result`, fails closed
  (`FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE`) instead of silently
  falling through to `STEP_4` -- an unknown/malformed readiness signal must
  never be treated as an ordinary body-rewrite-fixable defect.
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
        "run_root_review_pipeline_operator_intervention_route", PIPELINE_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("run_root_review_pipeline_operator_intervention_route", module)
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_pipeline_module()
_REPO = "squne121/loop-protocol"

# `zzz-not-a-real-preflight-allowlisted-tool-2397` is not on
# `baseline_vc_preflight.py`'s closed allowlist (`_ALLOWED_COMMANDS`), so the
# REAL checker chain classifies it `category: command_not_allowed` /
# `decision: blocked` BEFORE any attempt to execute it (the allowlist check
# is purely static, no exec, no missing-binary lookup, no network) --
# `command_not_allowed` is not a key in
# `contract_readiness_check._PREFLIGHT_CATEGORY_TO_READINESS`, so
# `map_preflight_result_to_errors()` falls through to its `else` branch and
# reports `readiness_status: "human_judgment"`. `merge_readiness_into_review_result()`
# then sets `merged["failure_class"] = "contract_readiness_human_judgment"`
# (`readiness_status_to_failure_class()`) and force-upgrades a would-be
# `verdict: approve` to `verdict: needs-fix` so the failure_class is never
# silently dropped -- producing a genuine, deterministic, hermetic
# `needs-fix` + `request_changes` + `contract_readiness_human_judgment`
# payload with no live GitHub / network / missing-binary dependency.
_CONTRACT_READINESS_HUMAN_JUDGMENT_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #2397 operator intervention route fixture (contract_readiness_human_judgment branch)"
change_kind: research
```

## Outcome

Fixture body for a genuine end-to-end `produce` CLI regression test proving
`route_canonical_step2_result()` routes a genuine
`merged_review_result.failure_class: contract_readiness_human_judgment`
result to `STEP_5_OPERATOR_INTERVENTION_REQUIRED` (Issue #2397).

## Acceptance Criteria

- [ ] AC1: fixture body's Verification Command uses a binary that is not on
      the VC preflight allowlist, so the real checker chain's readiness
      check classifies it `category: command_not_allowed` -> readiness
      `human_judgment`, producing a genuine
      `merged_review_result.failure_class: contract_readiness_human_judgment`.

## Verification Commands

```bash
# AC1
$ zzz-not-a-real-preflight-allowlisted-tool-2397 --version
```

## Allowed Paths

- fixture/e2e_produce_operator_intervention_contract_readiness_human_judgment.md
"""


def _run_real_produce(tmp_path: Path, monkeypatch, *, body: str, issue_number: int) -> dict:
    """Run a REAL `_cmd_produce()` invocation (real checker subprocess chain
    via `reviewer_transport.run_reviewer_transport()`); only the live GitHub
    body fetch is replaced with a pinned fixture, matching the existing
    `test_root_review_canonical_delivery.py` E2E pattern."""
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
    assert rc == 0, out
    assert out["status"] == "ok", out
    return out


# ---------------------------------------------------------------------------
# AC1: `needs-fix` + `request_changes` + `merged_review_result.failure_class
# == "contract_readiness_human_judgment"` routes to
# `STEP_5_OPERATOR_INTERVENTION_REQUIRED`.
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_needs_fix_request_changes_with_contract_readiness_human_judgment_routes_to_operator_intervention():
    result = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"failure_class": "contract_readiness_human_judgment"},
    }
    assert (
        _PIPELINE.route_canonical_step2_result(result)
        == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED
    )
    assert _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED != _PIPELINE.STEP_5_HUMAN_JUDGMENT_REQUIRED
    assert _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED != _PIPELINE.STEP_4


def test_given_real_produce_needs_fix_body_with_contract_readiness_human_judgment_when_run_then_routes_to_operator_intervention(
    tmp_path: Path, monkeypatch
):
    out = _run_real_produce(
        tmp_path,
        monkeypatch,
        body=_CONTRACT_READINESS_HUMAN_JUDGMENT_BODY,
        issue_number=2397001,
    )
    compact_result = out["compact_result"]
    assert compact_result["verdict"] == "needs-fix", out
    # Issue #2397 Out of Scope: the compact V2 wire's `NEXT_ACTION` two-value
    # contract (`proceed | request_changes`) is unchanged by this Issue --
    # NOT a new `human_judgment_required` wire value.
    assert compact_result["next_action"] == "request_changes", out
    assert (
        out["merged_review_result"]["failure_class"] == "contract_readiness_human_judgment"
    ), out
    assert out["canonical_step2_route"] == _PIPELINE.STEP_5_OPERATOR_INTERVENTION_REQUIRED, out


# ---------------------------------------------------------------------------
# AC2: `needs-fix` + `request_changes` with `merged_review_result` missing or
# its `failure_class` missing/`None` still routes to `STEP_4` (existing
# default, unchanged).
# ---------------------------------------------------------------------------


def test_route_canonical_step2_result_needs_fix_request_changes_without_failure_class_routes_to_step_4():
    no_merged_key = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
    }
    assert _PIPELINE.route_canonical_step2_result(no_merged_key) == _PIPELINE.STEP_4

    merged_none = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": None,
    }
    assert _PIPELINE.route_canonical_step2_result(merged_none) == _PIPELINE.STEP_4

    failure_class_missing = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"blocking_issues": ["some C3 fix"]},
    }
    assert _PIPELINE.route_canonical_step2_result(failure_class_missing) == _PIPELINE.STEP_4

    failure_class_none = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": {"failure_class": None},
    }
    assert _PIPELINE.route_canonical_step2_result(failure_class_none) == _PIPELINE.STEP_4


# ---------------------------------------------------------------------------
# AC3: an unrecognized non-empty `failure_class`, or a non-`Mapping`
# `merged_review_result`, fails closed instead of falling through to
# `STEP_4`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "merged_review_result",
    [
        {"failure_class": "some_unrecognized_failure_class"},
        {"failure_class": ""},
        ["unexpected", "list"],
        "unexpected string",
        1,
    ],
    ids=[
        "unknown_non_empty_failure_class",
        "empty_string_failure_class",
        "merged_review_result_list",
        "merged_review_result_string",
        "merged_review_result_int",
    ],
)
def test_route_canonical_step2_result_needs_fix_request_changes_with_unknown_failure_class_fails_closed(
    merged_review_result,
):
    result = {
        "status": "ok",
        "compact_result": {"verdict": "needs-fix", "next_action": "request_changes"},
        "merged_review_result": merged_review_result,
    }
    assert (
        _PIPELINE.route_canonical_step2_result(result)
        == _PIPELINE.FAIL_CLOSED_ENVIRONMENT_OR_INTEGRITY_FAILURE
    )
