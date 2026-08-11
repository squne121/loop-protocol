from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]; sys.path.insert(0, str(ROOT / "scripts" / "agent-guards"))
from git_mutation_command_policy import parse_controlled_git_change_exec_command

def _command(flag, value):
    return "uv run --locked python3 scripts/agent-guards/controlled_git_change_exec.py --cwd .claude/worktrees/issue-2102-main-drift " + flag + " " + value + " --path scripts/agent-guards/controlled_git_change_exec.py --message fix --expected-head " + "a" * 40 + " --expected-old " + "b" * 40

def test_given_materialize_request_and_two_cas_values_when_policy_parses_then_it_is_accepted():
    parsed = parse_controlled_git_change_exec_command(_command("--materialize-request", "tmp/request.json"), str(ROOT))
    assert parsed and parsed.materialize_request == "tmp/request.json" and parsed.expected_old == "b" * 40

def test_given_snapshot_json_only_when_policy_parses_then_it_is_rejected():
    assert parse_controlled_git_change_exec_command(_command("--snapshot-json", "tmp/snapshot.json"), str(ROOT)) is None

def test_given_missing_expected_old_when_policy_parses_then_it_is_rejected():
    assert parse_controlled_git_change_exec_command(_command("--materialize-request", "tmp/request.json").replace(" --expected-old " + "b" * 40, ""), str(ROOT)) is None
