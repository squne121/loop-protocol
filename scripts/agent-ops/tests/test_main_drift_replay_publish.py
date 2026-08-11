from __future__ import annotations
import importlib.util, sys
from pathlib import Path
SPEC = importlib.util.spec_from_file_location("pr_head_replay_publish", Path(__file__).resolve().parents[1] / "pr_head_replay_publish_exec.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = MODULE; SPEC.loader.exec_module(MODULE)

def test_given_old_review_head_when_pr_readback_differs_then_replay_match_is_false():
    assert not MODULE._pr_matches({"headRefOid": "b" * 40, "headRefName": "issue-2102"}, expected_head="a" * 40, target_branch="issue-2102")

def test_given_allowed_directory_net_diff_when_checked_then_only_that_directory_is_accepted():
    assert MODULE._path_allowed("scripts/agent-ops/pr_head_replay_publish_exec.py", ["scripts/agent-ops/"])
    assert not MODULE._path_allowed("assets/nope", ["assets/"])
