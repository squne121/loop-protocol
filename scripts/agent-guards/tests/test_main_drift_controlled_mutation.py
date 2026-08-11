from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "agent-guards"))
from git_mutation_command_policy import (  # noqa: E402
    parse_controlled_git_change_exec_command,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _command(flag: str, value: str) -> str:
    return " ".join(
        (
            "uv run --locked python3",
            "scripts/agent-guards/controlled_git_change_exec.py",
            "--cwd .claude/worktrees/issue-2102-main-drift",
            f"{flag} {value}",
            "--path scripts/agent-guards/controlled_git_change_exec.py",
            "--message fix",
            f"--expected-head {SHA_A}",
            f"--expected-old {SHA_B}",
        )
    )


def test_given_materialize_request_and_two_cas_values_when_policy_parses_then_it_is_accepted():
    parsed = parse_controlled_git_change_exec_command(
        _command("--materialize-request", "tmp/request.json"),
        str(ROOT),
    )

    assert parsed is not None
    assert parsed.materialize_request == "tmp/request.json"
    assert parsed.expected_old == SHA_B


def test_given_snapshot_json_only_when_policy_parses_then_it_is_rejected():
    parsed = parse_controlled_git_change_exec_command(
        _command("--snapshot-json", "tmp/snapshot.json"),
        str(ROOT),
    )

    assert parsed is None


def test_given_missing_expected_old_when_policy_parses_then_it_is_rejected():
    command = _command("--materialize-request", "tmp/request.json")
    parsed = parse_controlled_git_change_exec_command(
        command.replace(f" --expected-old {SHA_B}", ""),
        str(ROOT),
    )

    assert parsed is None
