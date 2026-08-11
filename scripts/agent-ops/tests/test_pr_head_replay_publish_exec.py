"""Offline regression tests for pr_head_replay_publish_exec.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _ROOT / "scripts" / "agent-ops" / "pr_head_replay_publish_exec.py"
_SPEC = importlib.util.spec_from_file_location("pr_head_replay_publish_exec", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class OfflineLiveRunner:
    def __init__(self, *, bare: Path, reported_head: str, issue_body: str, post_mode: str = "new") -> None:
        self.bare = bare
        self.reported_head = reported_head
        self.issue_body = issue_body
        self.post_mode = post_mode
        self.pr_reads = 0

    def __call__(self, argv, **kwargs):  # noqa: ANN001
        command = list(argv)
        if command[:4] == ["rtk", "gh", "issue", "view"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"body": self.issue_body}), "")
        if command[:4] == ["rtk", "gh", "pr", "view"]:
            self.pr_reads += 1
            head = self.reported_head
            if self.pr_reads >= 3 and self.post_mode == "new":
                head = _git(self.bare, "rev-parse", "refs/heads/target")
            payload = {"headRefName": "target", "headRefOid": head}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.run(command, **kwargs, capture_output=True, text=True, check=False)


@pytest.fixture
def replay_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _commit(repo, "allowed.txt", "base\n", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(tmp_path, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", f"{base}:refs/heads/target")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo, bare, base


def _body(*allowed: str) -> str:
    return "## Allowed Paths\n\n" + "\n".join(f"- `{path}`" for path in allowed) + "\n\n## Stop Conditions\n"


def _execute(
    repo: Path,
    bare: Path,
    base: str,
    head: str,
    body: str,
    *,
    expected: str | None = None,
    reported_head: str | None = None,
    post_mode: str = "new",
):
    expected = expected or _git(bare, "rev-parse", "refs/heads/target")
    runner = OfflineLiveRunner(
        bare=bare,
        reported_head=reported_head or _git(bare, "rev-parse", "refs/heads/target"),
        issue_body=body,
        post_mode=post_mode,
    )
    return _MODULE.execute(
        repo="owner/repo",
        issue_number=2040,
        pr_number=2081,
        target_branch="target",
        expected_remote_pr_head=expected,
        source_base=base,
        source_head=head,
        project_root=repo,
        runner=runner,
    )["PR_HEAD_REPLAY_PUBLISH_RESULT_V1"]


def test_given_approved_range_when_replayed_then_pushes_one_new_commit(replay_repo):
    repo, bare, base = replay_repo
    head = _commit(repo, "allowed.txt", "source\n", "approved source")
    result = _execute(repo, bare, base, head, _body("allowed.txt"))
    assert result["status"] == "ok"
    assert result["pushed"] is True
    assert _git(bare, "rev-parse", "refs/heads/target") == result["new_commit_sha"]
    assert not list((repo / ".claude" / "worktrees").glob("pr-head-replay-*"))


def test_given_stale_pr_head_when_checked_then_never_pushes(replay_repo):
    repo, bare, base = replay_repo
    head = _commit(repo, "allowed.txt", "source\n", "approved source")
    original = _git(bare, "rev-parse", "refs/heads/target")
    result = _execute(
        repo, bare, base, head, _body("allowed.txt"), expected="f" * 40, reported_head=original
    )
    assert result["status"] == "blocked"
    assert result["errors"] == ["pr_head_or_branch_mismatch"]
    assert _git(bare, "rev-parse", "refs/heads/target") == original


def test_given_disallowed_source_path_when_checked_then_never_pushes(replay_repo):
    repo, bare, base = replay_repo
    head = _commit(repo, "forbidden.txt", "source\n", "disallowed source")
    original = _git(bare, "rev-parse", "refs/heads/target")
    result = _execute(repo, bare, base, head, _body("allowed.txt"))
    assert result["status"] == "blocked"
    assert result["errors"] == ["source_range_contains_disallowed_path"]
    assert _git(bare, "rev-parse", "refs/heads/target") == original


def test_given_non_ancestor_source_base_when_checked_then_never_pushes(replay_repo):
    repo, bare, base = replay_repo
    head = _commit(repo, "allowed.txt", "source\n", "approved source")
    _git(repo, "checkout", "--orphan", "unrelated")
    _git(repo, "rm", "-rf", ".")
    unrelated = _commit(repo, "other.txt", "unrelated\n", "unrelated")
    _git(repo, "checkout", "master")
    original = _git(bare, "rev-parse", "refs/heads/target")
    result = _execute(repo, bare, unrelated, head, _body("allowed.txt"))
    assert result["status"] == "blocked"
    assert result["errors"] == ["source_base_not_ancestor"]
    assert _git(bare, "rev-parse", "refs/heads/target") == original


def test_given_post_publish_readback_mismatch_when_pushed_then_reports_failure(replay_repo):
    repo, bare, base = replay_repo
    head = _commit(repo, "allowed.txt", "source\n", "approved source")
    result = _execute(repo, bare, base, head, _body("allowed.txt"), post_mode="stale")
    assert result["status"] == "failed"
    assert result["errors"] == ["post_publish_pr_readback_mismatch"]
    assert _git(bare, "rev-parse", "refs/heads/target") == result["new_commit_sha"]
