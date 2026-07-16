"""test_pr_review_publish_hookchain.py -- Issue #1536 AC8.

Real PreToolUse hook chain verification for the pr_review.publish controlled
executor command, launched as subprocess (stdin JSON, real git repo) rather
than in-process function calls.

Covers two dimensions:
  1. local_main_branch_guard.sh must ALLOW the exact
     controlled_skill_mutation_exec.py --command-id pr_review.publish argv
     (same authorization lane as the existing issue_body.update /
     issue_comment.publish / contract_snapshot.publish command ids -- adding
     pr_review.publish to ALL_COMMAND_IDS is sufficient, no guard script
     changes were made or needed).
  2. guard-japanese-prose.sh (the shadow/enforce-toggleable hook flagged by
     the OWNER review as a P1-3 risk for the old `gh pr review --body <value>`
     CLI-flag design) does not block the controlled-executor invocation in
     either GUARD_JAPANESE_PROSE_MODE=shadow or =enforce, because the verdict
     body is transported via --input-file (a JSON artifact path argument),
     never as a literal --body/-b/-F CLI flag value that the prose guard's
     argv parser inspects.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOCAL_MAIN_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
JAPANESE_PROSE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "guard-japanese-prose.sh"

PR_NUMBER = 1530
INPUT_REL = f"artifacts/{PR_NUMBER}/issue-metadata/pr_review.publish/in.json"

PR_REVIEW_PUBLISH_CMD = (
    "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
    " --command-id pr_review.publish"
    f" --issue-number {PR_NUMBER}"
    f" --input-file {INPUT_REL}"
    " --repo squne121/loop-protocol"
)


@pytest.fixture
def tmp_git_repo():
    tmpdir = tempfile.mkdtemp(prefix="pr_review_publish_hookchain_")
    try:
        subprocess.run(["git", "init", "-b", "main", tmpdir], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.email", "t@t.com"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "config", "user.name", "T"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", tmpdir, "remote", "add", "origin",
             "https://github.com/squne121/loop-protocol.git"],
            check=True, capture_output=True,
        )
        (Path(tmpdir) / "README.md").write_text("test")
        subprocess.run(["git", "-C", tmpdir, "add", "README.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", tmpdir, "commit", "-m", "init"], check=True, capture_output=True)

        executor = Path(tmpdir) / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
        executor.parent.mkdir(parents=True, exist_ok=True)
        executor.write_text("# stub\n")

        input_file = Path(tmpdir) / INPUT_REL
        input_file.parent.mkdir(parents=True, exist_ok=True)
        input_file.write_text("{}\n")

        yield Path(tmpdir)
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pretool_payload(command: str, cwd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _run_local_main_branch_guard(payload: dict, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    return subprocess.run(
        [str(LOCAL_MAIN_GUARD_SH)], input=json.dumps(payload), text=True,
        capture_output=True, cwd=str(cwd), env=env,
    )


def _run_japanese_prose_guard(payload: dict, cwd: Path, mode: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(cwd)
    env["GUARD_JAPANESE_PROSE_MODE"] = mode
    return subprocess.run(
        [str(JAPANESE_PROSE_GUARD_SH)], input=json.dumps(payload), text=True,
        capture_output=True, cwd=str(cwd), env=env,
    )


class TestAC8ShadowAndEnforceAllow:
    def test_ac8_shadow_and_enforce_allow(self, tmp_git_repo):
        payload = _pretool_payload(PR_REVIEW_PUBLISH_CMD, str(tmp_git_repo))

        lmbg_result = _run_local_main_branch_guard(payload, tmp_git_repo)
        assert lmbg_result.returncode == 0, (
            "local_main_branch_guard.sh must allow the exact "
            "controlled_skill_mutation_exec.py --command-id pr_review.publish "
            f"invocation; stderr={lmbg_result.stderr}"
        )

        for mode in ("shadow", "enforce"):
            gjp_result = _run_japanese_prose_guard(payload, tmp_git_repo, mode)
            assert gjp_result.returncode == 0, (
                f"guard-japanese-prose.sh (mode={mode}) must not block the "
                "controlled executor invocation -- the verdict body is "
                "transported via --input-file, never as a literal "
                f"--body/-b/-F CLI flag; stderr={gjp_result.stderr}"
            )
