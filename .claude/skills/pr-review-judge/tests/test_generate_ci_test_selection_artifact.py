"""
test_generate_ci_test_selection_artifact.py — Tests for H3 secondary coverage provenance fields.

Tests cover:
  - secondary_coverage_provider_job field presence and value
  - cross_job_covered_test_files field presence and value
  - secondary_coverage_error field presence
  - plan + pytest_args → cross_job_covered_test_files populated
  - plan only (no pytest_args) → secondary coverage logic not active
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — import the module under test
# ---------------------------------------------------------------------------

_SKILL_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
)
if _SKILL_SCRIPTS not in sys.path:
    sys.path.insert(0, _SKILL_SCRIPTS)

import generate_ci_test_selection_artifact as gen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(
    output="/tmp/test_artifact.json",
    pytest_args=None,
    plan=None,
    pr_head_sha="abc123",
    base_sha="base000",
    head_sha="head111",
    checked_out_sha=None,
    merge_sha=None,
    workflow="ci",
    job="python-test",
    ci_run_url=None,
):
    """Build a minimal argparse-like namespace for generate_artifact()."""
    args = MagicMock()
    args.output = output
    args.pytest_args = pytest_args
    args.plan = plan
    args.pr_head_sha = pr_head_sha
    args.base_sha = base_sha
    args.head_sha = head_sha
    args.checked_out_sha = checked_out_sha
    args.merge_sha = merge_sha
    args.workflow = workflow
    args.job = job
    args.ci_run_url = ci_run_url
    return args


# ---------------------------------------------------------------------------
# Tests for H3: secondary coverage provenance fields
# ---------------------------------------------------------------------------

class TestSecondaryCoverageFields(unittest.TestCase):
    """H3: Verify secondary_coverage_provider_job, cross_job_covered_test_files,
    secondary_coverage_error fields are present in the artifact."""

    def _run_generate(self, args, collected_nodeids=None, changed_files=None):
        """Run generate_artifact with minimal mocking. Returns the artifact dict."""
        import json
        import tempfile

        out_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        out_file.close()
        args.output = out_file.name

        collected = collected_nodeids or []
        changed = changed_files or []

        # Mock get_pytest_collected_tests
        collected_files = sorted({nid.split("::")[0] for nid in collected})
        collection_status = {
            "returncode": 0, "timed_out": False, "error": None,
            "nodeid_count": len(collected), "stderr_tail": "", "ok": len(collected) > 0,
        }

        # Mock get_changed_test_files
        diff_status = {
            "base_sha": args.base_sha, "head_sha": args.head_sha,
            "returncode": 0, "timed_out": False, "error": None,
            "stderr_tail": "", "ok": True,
        }

        with patch.object(gen, "get_pytest_collected_tests",
                          return_value=(collected_files, collected, collection_status)), \
             patch.object(gen, "get_changed_test_files",
                          return_value=(changed, [], diff_status)):
            gen.generate_artifact(args)

        with open(out_file.name) as f:
            artifact = json.load(f)
        os.unlink(out_file.name)
        return artifact

    def test_secondary_coverage_fields_present_when_no_plan(self):
        """Fields present even when plan=None / pytest_args=None."""
        args = _make_args(
            pytest_args=["scripts/tests/"],
            plan=None,
        )
        artifact = self._run_generate(
            args,
            collected_nodeids=["scripts/tests/test_foo.py::test_a"],
            changed_files=["scripts/tests/test_foo.py"],
        )
        self.assertIn("secondary_coverage_provider_job", artifact)
        self.assertIn("cross_job_covered_test_files", artifact)
        self.assertIn("secondary_coverage_error", artifact)
        # plan=None → secondary coverage not active → provider_job is None
        self.assertIsNone(artifact["secondary_coverage_provider_job"])
        self.assertEqual(artifact["cross_job_covered_test_files"], [])

    def test_secondary_coverage_not_active_when_only_plan_no_pytest_args(self):
        """plan provided but pytest_args=None → secondary coverage NOT active."""
        args = _make_args(
            pytest_args=None,
            plan=".github/ci/python-test-plan.json",
        )

        # Mock resolve_pytest_args to return a default list (plan-derived)
        with patch.object(gen, "resolve_pytest_args", return_value=["scripts/tests/"]):
            artifact = self._run_generate(
                args,
                collected_nodeids=["scripts/tests/test_foo.py::test_a"],
                changed_files=["scripts/tests/test_bar.py"],
            )

        # pytest_args not set on args → secondary coverage inactive
        self.assertIsNone(artifact["secondary_coverage_provider_job"])
        self.assertEqual(artifact["cross_job_covered_test_files"], [])

    def test_cross_job_covered_test_files_when_plan_covers_changed_file(self):
        """plan + pytest_args: changed file covered by plan → in cross_job_covered_test_files."""
        args = _make_args(
            pytest_args=["scripts/tests/"],
            plan=".github/ci/python-test-plan.json",
            job="python-test",
        )

        # Simulate plan that covers ".claude/skills/pr-review-judge/tests/"
        fake_plan = {
            "targets": [
                "scripts/tests/test_milestone_rollup.py",
                ".claude/skills/pr-review-judge/tests/",
            ]
        }

        # Changed file: in .claude/skills/pr-review-judge/tests/ (plan-covered), not in collected
        changed = [".claude/skills/pr-review-judge/tests/test_generate_ci_test_selection_artifact.py"]
        # collected: only scripts/tests scope
        collected = ["scripts/tests/test_milestone_rollup.py::test_foo"]

        def mock_load_plan_module():
            m = MagicMock()
            m.load_plan = MagicMock(return_value=fake_plan)
            m.scope_argv = MagicMock(return_value=["scripts/tests/"])
            return m

        with patch.object(gen, "_load_plan_module", side_effect=mock_load_plan_module):
            artifact = self._run_generate(
                args,
                collected_nodeids=collected,
                changed_files=changed,
            )

        self.assertEqual(artifact["secondary_coverage_provider_job"], "python-test")
        self.assertIn(
            ".claude/skills/pr-review-judge/tests/test_generate_ci_test_selection_artifact.py",
            artifact["cross_job_covered_test_files"],
        )
        # The file should NOT be in uncovered (it's cross-job covered)
        self.assertNotIn(
            ".claude/skills/pr-review-judge/tests/test_generate_ci_test_selection_artifact.py",
            artifact["uncovered_changed_test_files"],
        )

    def test_secondary_coverage_error_recorded_on_plan_load_failure(self):
        """When plan loading fails, secondary_coverage_error is set (not None)."""
        args = _make_args(
            pytest_args=["scripts/tests/"],
            plan="nonexistent-plan.json",
            job="python-test",
        )

        def failing_load_plan_module():
            m = MagicMock()
            m.load_plan = MagicMock(side_effect=RuntimeError("plan not found"))
            return m

        with patch.object(gen, "_load_plan_module", side_effect=failing_load_plan_module):
            artifact = self._run_generate(
                args,
                collected_nodeids=["scripts/tests/test_foo.py::test_a"],
                changed_files=[],
            )

        # Error should be recorded
        self.assertIsNotNone(artifact["secondary_coverage_error"])
        # Provider job should not be set (failed before assignment)
        self.assertIsNone(artifact["secondary_coverage_provider_job"])


if __name__ == "__main__":
    unittest.main()
