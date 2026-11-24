"""
tests/ci/test_ci_verdict_summary_push_event.py

Issue #3076: dispatch-only jobs (reliability-assessment,
close-evidence-publication, ci-runtime-baseline-gate-ready) are completed /
skipped on ordinary push runs as well as pull_request runs. The conditional
exclusion predicate must accept both events when the skip is fully bound
(current head, positive CheckRun ID, exact Jobs-API provenance) and stay
fail-closed for every other case.
"""
from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "pr-review-judge"
    / "scripts"
    / "ci_verdict_summary_v2.py"
)

HEAD = "aac926178063ab704fb18c65fcec895dccd28b30"
OTHER_HEAD = "0000000000000000000000000000000000000001"

CONDITIONAL_NAMES = (
    "reliability-assessment",
    "close-evidence-publication",
    "ci-runtime-baseline-gate-ready",
)


def _load_module(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v2() -> types.ModuleType:
    return _load_module(_SCRIPT)


def _check(
    name: str,
    workflow: str = "ci",
    status: str | None = "completed",
    conclusion: str | None = "success",
    head_sha: str | None = HEAD,
    provenance: str | None = "github_actions_job_api",
    check_run_id: object = 138_264_280_620,
) -> dict:
    check: dict = {
        "id": check_run_id,
        "name": name,
        "workflow": workflow,
        "status": status,
        "conclusion": conclusion,
        "headSha": head_sha,
    }
    if provenance is not None:
        check["provenance"] = provenance
    return check


def _skip(name: str, **overrides) -> dict:
    return _check(name, conclusion="skipped", **overrides)


def _required_all_passing(v2) -> list[dict]:
    return [_check(name, workflow=wf) for (wf, name) in sorted(v2.REQUIRED_CHECKS)]


def _build(v2, checks: list[dict], event_name: str) -> dict:
    return v2.generate_verdict(
        expected_head_sha=HEAD,
        pr_head_sha=HEAD,
        repository="owner/repo",
        workflow_run_id=45_338_974_114,
        workflow_run_attempt=1,
        event_name=event_name,
        raw_checks=checks,
    )


@pytest.mark.parametrize("event_name", ["push", "pull_request"])
def test_all_three_exact_skips_excluded_and_merge_ready(v2, event_name):
    checks = _required_all_passing(v2) + [_skip(n, check_run_id=100 + i) for i, n in enumerate(CONDITIONAL_NAMES)]
    artifact = _build(v2, checks, event_name)
    for name in CONDITIONAL_NAMES:
        entry = next(c for c in artifact["checks"] if c["name"] == name)
        assert entry["classification"] == "excluded"
        assert entry["blocking_merge_ready"] is False
    assert artifact["overall_status"] == "merge_ready", artifact


@pytest.mark.parametrize("name", CONDITIONAL_NAMES)
@pytest.mark.parametrize("event_name", ["push", "pull_request"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"conclusion": "failure"},
        {"conclusion": "cancelled"},
        {"status": "queued", "conclusion": None},
        {"status": "in_progress", "conclusion": None},
        {"head_sha": OTHER_HEAD},
        {"provenance": None},
        {"provenance": "needs_result_synthetic"},
        {"check_run_id": None},
        {"check_run_id": 0},
        {"check_run_id": -5},
        {"check_run_id": True},
        {"check_run_id": "123"},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_non_exact_skip_fails_closed(v2, name, event_name, overrides):
    check = _skip(name)
    if "head_sha" in overrides:
        check["headSha"] = overrides["head_sha"]
    if "check_run_id" in overrides:
        check["id"] = overrides["check_run_id"]
    for key in ("status", "conclusion"):
        if key in overrides:
            check[key] = overrides[key]
    if "provenance" in overrides:
        if overrides["provenance"] is None:
            check.pop("provenance", None)
        else:
            check["provenance"] = overrides["provenance"]
    artifact = _build(v2, _required_all_passing(v2) + [check], event_name)
    entry = next(c for c in artifact["checks"] if c["name"] == name)
    assert entry["classification"] == "unknown"
    assert entry["blocking_merge_ready"] is True
    assert artifact["overall_status"] != "merge_ready"


@pytest.mark.parametrize("name", CONDITIONAL_NAMES)
@pytest.mark.parametrize(
    "event_name", ["workflow_dispatch", "schedule", "workflow_run", "", None, "Push"]
)
def test_other_events_fail_closed(v2, name, event_name):
    artifact = _build(v2, _required_all_passing(v2) + [_skip(name)], event_name)
    entry = next(c for c in artifact["checks"] if c["name"] == name)
    assert entry["classification"] == "unknown"
    assert entry["blocking_merge_ready"] is True
    assert artifact["overall_status"] == "gh_error"


@pytest.mark.parametrize("event_name", ["push", "pull_request"])
def test_different_tuple_not_excluded(v2, event_name):
    for check in (
        _skip("reliability-assessment", workflow="other-workflow"),
        _skip("some-other-job"),
    ):
        artifact = _build(v2, _required_all_passing(v2) + [check], event_name)
        assert artifact["overall_status"] != "merge_ready"
        entry = next(c for c in artifact["checks"] if c["name"] == check["name"])
        assert entry["classification"] == "unknown"
        assert entry["blocking_merge_ready"] is True


@pytest.mark.parametrize("event_name", ["push", "pull_request"])
def test_required_failure_or_missing_not_masked_by_exclusions(v2, event_name):
    skips = [_skip(n) for n in CONDITIONAL_NAMES]

    failed = _required_all_passing(v2)
    failed[0]["conclusion"] = "failure"
    assert _build(v2, failed + skips, event_name)["overall_status"] != "merge_ready"

    missing = _required_all_passing(v2)[1:]
    assert _build(v2, missing + skips, event_name)["overall_status"] != "merge_ready"

    skipped_required = _required_all_passing(v2)
    skipped_required[0]["conclusion"] = "skipped"
    assert _build(v2, skipped_required + skips, event_name)["overall_status"] != "merge_ready"


def test_static_contract_unchanged(v2):
    assert v2.CONDITIONAL_EXCLUDED_TUPLES == {("ci", n) for n in CONDITIONAL_NAMES}
    assert v2.EXACT_CHECK_RUN_PROVENANCE == "github_actions_job_api"
    for name in CONDITIONAL_NAMES:
        assert ("ci", name) not in v2.CLASSIFICATION_MAP
