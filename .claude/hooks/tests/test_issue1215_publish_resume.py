#!/usr/bin/env python3
"""Issue #1215: publish-resume related scope-guard coverage."""

from worktree_scope_guard_testkit import _bash_payload, _make_repo_with_worktree, _run_guard


def test_issue1215_publish_resume_direct_publish_command_blocked(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="publish-resume")
    cmd = (
        "python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
        " --issue-number 1215 --repo squne121/loop-protocol"
    )
    payload = _bash_payload(cmd, str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1215"}
    result = _run_guard(payload, repo["root"], issue="1215", extra_env=env)
    assert result.returncode == 2, f"expected block, got={result.returncode}; stderr={result.stderr}"


def test_issue1215_publish_resume_controlled_skill_mutation_allowed(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1215", slug="publish-resume")
    guard_dir = repo["root"] / "scripts" / "agent-guards"
    guard_dir.mkdir(parents=True, exist_ok=True)
    (guard_dir / "controlled_skill_mutation_exec.py").write_text("print('ok')\n")

    artifact_dir = repo["root"] / "artifacts" / "1215"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "termination_report_input.json").write_text("{}\n")

    cmd = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
        " --command-id termination_report.publish --issue-number 1215"
        " --input-file artifacts/1215/termination_report_input.json"
        " --repo squne121/loop-protocol"
    )
    payload = _bash_payload(cmd, str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1215"}
    result = _run_guard(payload, repo["root"], issue="1215", extra_env=env)
    assert result.returncode == 0, f"expected allow, got={result.returncode}; stderr={result.stderr}"
