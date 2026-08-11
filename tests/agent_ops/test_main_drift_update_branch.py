from __future__ import annotations
import importlib.util, sys
from pathlib import Path
SPEC = importlib.util.spec_from_file_location("update_branch_main_drift", Path(__file__).resolve().parents[2] / ".claude" / "skills" / "implement-issue" / "scripts" / "update_branch.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = MODULE; SPEC.loader.exec_module(MODULE)

def test_given_invalid_expected_head_when_update_branch_validates_then_no_poll_or_api_is_authorized():
    request = MODULE.UpdateBranchRequest(pr_number=1, repo=MODULE.CANONICAL_REPO, expected_head_sha="old", caller="impl-review-loop.step-5")
    assert MODULE._validate_request(request) == "expected_head_sha must be a full-length hexadecimal commit SHA"

def test_given_production_update_branch_when_inspected_then_poll_bounds_are_fixed_and_positive():
    assert MODULE.PRODUCTION_POLL_MAX > 0 and MODULE.PRODUCTION_POLL_INTERVAL > 0
