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
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hookchain_harness  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
LOCAL_MAIN_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
JAPANESE_PROSE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "guard-japanese-prose.sh"

PR_NUMBER = 1530
INPUT_REL = f"artifacts/{PR_NUMBER}/issue-metadata/pr_review.publish/in.json"

PR_REVIEW_PUBLISH_CMD = (
    "uv run --locked python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
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


class TestAC8FullRealHookChainAggregate:
    """Issue #1539 fix_delta Blocker 4: the real `.claude/settings.json`
    PreToolUse registration wires (at minimum) 7 Bash-matching hooks --
    secret_boundary_guard, local_main_branch_guard, worktree_scope_guard,
    guard-japanese-prose, rtk_boundary_shadow_guard, ci_test_performance_advisory,
    root_temporary_residue_advisory. The previous AC8 test hand-picked only 2
    of the 7 and asserted each hook's raw exit code individually, never
    running worktree_scope_guard.sh at all and never computing the AGGREGATE
    deny/ask/defer decision a real Claude Code session would compute. This
    class drives the full chain, in settings.json-declared order, via
    hookchain_harness.run_pretool_hook_chain(), and asserts on the aggregate."""

    def test_settings_json_registers_all_seven_expected_bash_hooks(self):
        commands = hookchain_harness.load_pretool_hook_commands("Bash")
        hook_names = {Path(c.split(" ")[0]).stem for c in commands}
        expected = {
            "secret_boundary_guard",
            "local_main_branch_guard",
            "worktree_scope_guard",
            "guard-japanese-prose",
            "rtk_boundary_shadow_guard",
            "ci_test_performance_advisory",
            "root_temporary_residue_advisory",
        }
        missing = expected - hook_names
        assert not missing, f"settings.json PreToolUse|Bash chain is missing: {missing}"

    def test_full_chain_aggregate_allow_for_pr_review_publish_command(self, tmp_git_repo):
        payload = _pretool_payload(PR_REVIEW_PUBLISH_CMD, str(tmp_git_repo))
        results = hookchain_harness.run_pretool_hook_chain(payload, tmp_git_repo)

        executed_names = {r["hook_name"] for r in results}
        assert "worktree_scope_guard" in executed_names, (
            "worktree_scope_guard.sh must actually be invoked as part of the "
            "chain, not skipped -- it is registered on the same Bash matcher."
        )

        permission_aggregate = hookchain_harness.aggregate_permission_decision(results)
        assert permission_aggregate == "no_decision", (
            "Silent successful hooks must remain no_decision rather than being "
            f"misreported as explicit allow. Per-hook results: {results}"
        )
        aggregate = hookchain_harness.aggregate_decision(results)
        assert aggregate == "allow", (
            "Aggregate PreToolUse decision across the full real hook chain "
            f"must be allow when no hook blocks, defers, or asks. "
            f"Per-hook results: {results}"
        )
        # A silent successful hook is no_decision, not an explicit allow. The
        # canonical command remains safe only when no hook denies, defers,
        # asks, or errors.
        for r in results:
            assert r["decision"] not in {"deny", "defer", "ask", "hook_error"}, (
                f"{r['hook_name']} returned {r['decision']} "
                f"(exit={r['returncode']}); stderr={r['stderr']}"
            )

    def test_full_chain_aggregate_blocks_raw_gh_pr_review_mutation(self, tmp_git_repo):
        """Negative control: proves the harness genuinely detects a real
        block (not a fail-open false negative). A raw `gh pr review --approve`
        issued directly from local root (bypassing the controlled executor
        entirely) must be denied by local_main_branch_guard -- this is
        exactly the unsafe pattern pr_review.publish exists to replace."""
        raw_cmd = f"gh pr review {PR_NUMBER} --approve --body x"
        payload = _pretool_payload(raw_cmd, str(tmp_git_repo))
        results = hookchain_harness.run_pretool_hook_chain(payload, tmp_git_repo)
        aggregate = hookchain_harness.aggregate_decision(results)
        assert aggregate == "block", (
            f"raw `gh pr review --approve` must be denied by the real hook "
            f"chain; results={results}"
        )
