from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
LOCAL_MAIN_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "local_main_branch_guard.sh"
WORKTREE_SCOPE_GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _payload(command: str, cwd: Path) -> str:
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)})


def _run_guard(script: Path, payload: str, repo: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    env.pop("LOOP_ISSUE_NUMBER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    return None
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv",
            "run",
            "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number",
            "{issue_number}",
            "--repo",
            "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    }
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SKILL_RUNTIME_TEST_OUTSIDE_WRITE") == "ignored":
        ignored_dir = Path(".cache")
        ignored_dir.mkdir(parents=True, exist_ok=True)
        (ignored_dir / "outside.txt").write_text("persisted")
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


def test_local_main_branch_guard_no_worktree_local_main_branch_guard(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _payload(
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1228 --repo squne121/loop-protocol",
        repo,
    )
    result = _run_guard(LOCAL_MAIN_GUARD_SH, payload, repo)
    assert result.returncode == 0, result.stderr


def test_worktree_scope_guard_no_worktree_worktree_scope_guard(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _payload(
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1228 --repo squne121/loop-protocol",
        repo,
    )
    result = _run_guard(WORKTREE_SCOPE_GUARD_SH, payload, repo)
    assert result.returncode == 0, result.stderr


def test_direct_preflight_block_direct_preflight_block(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _payload(
        "uv run python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py --issue-number 1228 --repo squne121/loop-protocol",
        repo,
    )
    result = _run_guard(LOCAL_MAIN_GUARD_SH, payload, repo)
    assert result.returncode == 2


def test_non_exact_executor_argv_non_exact_executor_argv(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    payload = _payload(
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --command-id preflight.run --issue-number 1228 --repo squne121/loop-protocol",
        repo,
    )
    result = _run_guard(LOCAL_MAIN_GUARD_SH, payload, repo)
    assert result.returncode == 2


def test_artifact_only_write_postcondition_artifact_only_write_postcondition(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    env = {
        "SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "ignored",
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **env},
        check=False,
    )
    assert result.returncode == 2
    assert "unauthorized write path" in result.stderr
