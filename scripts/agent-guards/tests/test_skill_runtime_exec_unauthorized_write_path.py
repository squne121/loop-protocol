from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


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
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    sleep_seconds = os.environ.get("SKILL_RUNTIME_TEST_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))
    if os.environ.get("SKILL_RUNTIME_TEST_OUTSIDE_WRITE") == "ignored":
        outside = Path(".cache")
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "outside.txt").write_text("self-write")
    if os.environ.get("SKILL_RUNTIME_TEST_SELF_WRITE_WORKTREES") == "ignored":
        # Issue #1343 AC5: a self-write into a volatile peer-session root
        # (.claude/worktrees/**) is a known, accepted limitation of the
        # stdlib-only race-tolerant hotfix (strict attribution is out of
        # scope; see Notes for Reviewer on Issue #1343).
        self_worktree_path = Path(".claude") / "worktrees" / "issue-9999-self" / "self-write.txt"
        self_worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self_worktree_path.write_text("self-write-into-volatile-worktrees-root")
    if os.environ.get("SKILL_RUNTIME_TEST_SELF_WRITE_OTHER_ISSUE_ARTIFACTS") == "ignored":
        # Issue #1343 AC5: a self-write into another issue's artifact root
        # under .claude/artifacts/issue-refinement-loop/** is likewise a
        # known, accepted limitation (excluded from detection by design).
        self_other_artifact_path = (
            Path(".claude") / "artifacts" / "issue-refinement-loop" / "1337" / "self-write.json"
        )
        self_other_artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self_other_artifact_path.write_text('{"self_write": true}')
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


def _run_executor(repo: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
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
        env=env,
        check=False,
    )


def _write_after_delay(path: Path, content: str, delay_seconds: float) -> threading.Thread:
    def _worker() -> None:
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def test_unrelated_process_write_to_worktrees_does_not_fail(tmp_path: Path) -> None:
    """GIVEN a peer local session concurrently writing under .claude/worktrees/**
    WHEN this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    peer_path = repo / ".claude" / "worktrees" / "issue-9999-peer-session" / "scratch.txt"
    thread = _write_after_delay(peer_path, "peer-session-write\n", delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_unrelated_process_write_to_other_issue_artifacts_does_not_fail(tmp_path: Path) -> None:
    """GIVEN a peer local session concurrently writing under a different
    issue's .claude/artifacts/issue-refinement-loop/<other issue>/** root
    WHEN this command's own child subprocess is still running for a
    different target issue
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    peer_path = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1337" / "peer.json"
    thread = _write_after_delay(peer_path, '{"peer": true}\n', delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_self_write_outside_allowed_roots_still_fails(tmp_path: Path) -> None:
    """GIVEN the executed child command itself writes outside its
    allowed_write_roots (e.g. .cache/outside.txt)
    WHEN no peer-session volatile root is involved
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path (no regression from the peer-session fix)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "ignored"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.cache/" in result.stderr
    assert "target_issue=1228" in result.stderr


def test_self_write_inside_allowed_roots_still_succeeds(tmp_path: Path) -> None:
    """GIVEN the executed child command writes only inside its
    allowed_write_roots
    WHEN the command completes
    THEN skill_runtime_exec.py must succeed as before (no regression)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo)
    assert result.returncode == 0, result.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()
    assert json.loads(artifact.read_text()) == {
        "issue_number": "1228",
        "repo": "squne121/loop-protocol",
    }


def test_self_write_to_worktrees_is_known_unsupported_in_stdlib_mode(tmp_path: Path) -> None:
    """GIVEN the executed child command itself (not a peer session) writes
    outside its allowed_write_roots into the volatile peer-session root
    .claude/worktrees/**
    WHEN the command completes
    THEN skill_runtime_exec.py does NOT fail with unauthorized_write_path
    (the self-write silently succeeds).

    KNOWN LIMITATION (Issue #1343 AC3/AC5, adopted via human REQUEST_CHANGES
    on PR #1349, Option B): volatile peer-session roots
    (.claude/worktrees/** and .claude/artifacts/issue-refinement-loop/**)
    are excluded from the before/after snapshot diff entirely so that
    concurrent peer sessions are never misattributed to this command's own
    child process (AC1/AC2). The unavoidable side effect is that this
    executor cannot distinguish a peer-session write from a self-write into
    those same roots. Strict attribution (e.g. process-level syscall
    tracing) is explicitly out of scope for this stdlib-only race-tolerant
    hotfix and is deferred to a separate follow-up issue if ever needed.
    This test documents the gap so it is not mistaken for an oversight.
    """
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_WORKTREES": "ignored"})
    assert result.returncode == 0, result.stderr
    self_write_path = repo / ".claude" / "worktrees" / "issue-9999-self" / "self-write.txt"
    assert self_write_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_self_write_to_other_issue_artifacts_is_known_unsupported_in_stdlib_mode(
    tmp_path: Path,
) -> None:
    """GIVEN the executed child command itself (not a peer session) writes
    outside its allowed_write_roots into another issue's artifact root under
    .claude/artifacts/issue-refinement-loop/<other issue>/**
    WHEN the command completes
    THEN skill_runtime_exec.py does NOT fail with unauthorized_write_path
    (the self-write silently succeeds).

    KNOWN LIMITATION (Issue #1343 AC3/AC5, adopted via human REQUEST_CHANGES
    on PR #1349, Option B): see
    test_self_write_to_worktrees_is_known_unsupported_in_stdlib_mode for the
    full rationale. This test documents the equivalent gap for the
    .claude/artifacts/issue-refinement-loop/** volatile peer-session root.
    """
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(
        repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_OTHER_ISSUE_ARTIFACTS": "ignored"}
    )
    assert result.returncode == 0, result.stderr
    self_write_path = (
        repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1337" / "self-write.json"
    )
    assert self_write_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()
