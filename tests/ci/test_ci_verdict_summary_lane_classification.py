"""
tests/ci/test_ci_verdict_summary_lane_classification.py

Issue #2119 AC12: e2e-core / e2e-responsive-matrix must have explicit
CLASSIFICATION_MAP entries so determine_check_verdict() does not fall back
to `unknown` -> `blocking=True, failure_reason="gh_error"` when both
providers actually succeeded (the same class of regression Issue #1760
fixed for python-test-core/the former codex-execpolicy job, retired by
Issue #2161's native Codex CLI retirement).
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


def _load_module(path: pathlib.Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ci_verdict_summary_v2", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def v2() -> types.ModuleType:
    return _load_module(_SCRIPT)


def test_e2e_core_and_e2e_responsive_matrix_are_explicitly_classified_and_do_not_fall_back_to_unknown(v2):
    for check_name in ("e2e-core", "e2e-responsive-matrix"):
        classification = v2.get_classification("ci", check_name)
        assert classification != "unknown", (
            f"jobs.{check_name} must have an explicit CLASSIFICATION_MAP entry "
            f"(got 'unknown', which determine_check_verdict() always blocks with gh_error)"
        )
        assert classification in {"required", "evidence"}, (
            f"jobs.{check_name} classification must be 'required' or 'evidence', got {classification!r}"
        )

        check = {
            "classification": classification,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "head_sha_match": True,
            "check_run_id": 12345,
        }
        blocking, failure_reason = v2.determine_check_verdict(check, expected_head_sha="a" * 40)
        assert blocking is False, (
            f"a successful, head-SHA-matched jobs.{check_name} check must not block merge-ready "
            f"(got blocking={blocking}, failure_reason={failure_reason!r})"
        )
        assert failure_reason == "none"
