#!/usr/bin/env python3
"""test_worktree_scope_guard_issue_metadata_executor.py — AC5 of Issue #1284.

Verifies that, in the active-issue + no-matching-worktree state
(same state as WORKTREE_SCOPE_RESOLUTION_V1 fail-closed block), the
controlled_skill_mutation_exec.py executor is allowed for the 3 new issue
metadata command ids (issue_body.update / issue_comment.publish /
contract_snapshot.publish) as an exact command class — while raw
`gh issue edit` / `gh issue comment` remain blocked.

Real PreToolUse hook path: subprocess -> worktree_scope_guard.sh -> .py,
with a real (isolated) git repo (no matching worktree for the active issue).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent  # worktree root
GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_bare_repo(tmp_path: Path) -> Path:
    """Repo with NO matching worktree for the active issue (B1 fail-closed state)."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=main)
    (main / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=main)
    _git("commit", "-q", "-m", "seed", cwd=main)
    return main


def _run_guard(payload: dict, project_root: Path, issue: str | None = None, extra_env: dict | None = None):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    if issue is not None:
        env["LOOP_ISSUE_NUMBER"] = str(issue)
    else:
        env.pop("LOOP_ISSUE_NUMBER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


def _bash_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _install_executor_stub(root: Path) -> None:
    executor_dir = root / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True, exist_ok=True)
    (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")


@pytest.mark.parametrize(
    "command_id,input_rel",
    [
        (
            "issue_body.update",
            "artifacts/1284/issue-metadata/issue_body.update/in.json",
        ),
        (
            "issue_comment.publish",
            "artifacts/1284/issue-metadata/issue_comment.publish/in.json",
        ),
        (
            "contract_snapshot.publish",
            "artifacts/1284/issue-metadata/contract_snapshot.publish/in.json",
        ),
    ],
)
def test_ac5_new_command_ids_allowed_via_executor_no_matching_worktree(
    tmp_path, command_id, input_rel
):
    """AC5: exact controlled executor command is allowed even in the active-issue
    + no-matching-worktree fail-closed state (root/default-branch execution)."""
    repo = _make_bare_repo(tmp_path)
    _install_executor_stub(repo)
    (repo / Path(input_rel)).parent.mkdir(parents=True, exist_ok=True)
    (repo / Path(input_rel)).write_text("{}\n")

    cmd = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
        f" --command-id {command_id}"
        " --issue-number 1284"
        f" --input-file {input_rel}"
        " --repo squne121/loop-protocol"
    )
    payload = _bash_payload(cmd, str(repo))
    # No LOOP_ISSUE_NUMBER env set — root execution without an issue-specific
    # worktree/session env (AC15: env optional for new command ids).
    r = _run_guard(payload, repo, issue="1284")
    assert r.returncode == 0, (
        f"{command_id} controlled executor command must be allowed even with "
        f"active issue + no matching worktree; stderr={r.stderr}"
    )


@pytest.mark.parametrize(
    "command",
    [
        "gh issue edit 1284 --body newbody",
        "gh issue comment 1284 -b hi",
    ],
)
def test_ac5_raw_gh_issue_mutation_still_blocked(tmp_path, command):
    """AC5: raw gh issue edit / gh issue comment remain blocked in the
    active-issue + no-matching-worktree fail-closed state."""
    repo = _make_bare_repo(tmp_path)
    payload = _bash_payload(command, str(repo))
    r = _run_guard(payload, repo, issue="1284")
    assert r.returncode == 2, f"{command!r} must still block; stderr={r.stderr}"
