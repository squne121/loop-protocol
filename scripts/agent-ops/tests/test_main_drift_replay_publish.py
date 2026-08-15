"""Real subprocess/git integration tests for the main-drift reconciliation
path of pr_head_replay_publish_exec.py (Issue #2102 fix_delta).

Prior revision of this file only re-tested unrelated generic invariants
(_pr_matches, _path_allowed) and never exercised main_drift / evidence_epoch
/ scope_clean_reconciliation. It is rewritten here to actually drive
execute() with ``current_base_branch`` / ``expected_current_base_sha`` set,
using a real bare-remote git repository (not mocks), so the deterministic
merge-tree conflict oracle and candidate_final_net_diff Allowed Paths gate
are exercised end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "pr_head_replay_publish_exec.py"
SPEC = importlib.util.spec_from_file_location("pr_head_replay_publish", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class _Runner:
    """Intercepts only the ``rtk gh`` calls; everything else is a real
    subprocess invocation against the fixture's bare remote."""

    def __init__(self, *, issue_body: str) -> None:
        self.issue_body = issue_body

    def __call__(self, argv, **kwargs):  # noqa: ANN001
        command = list(argv)
        if command[:4] == ["rtk", "gh", "issue", "view"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"body": self.issue_body}), "")
        if command[:4] == ["rtk", "gh", "pr", "view"]:
            head = _git(self._bare, "rev-parse", "refs/heads/target")
            payload = {"headRefName": "target", "headRefOid": head}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.run(command, **kwargs, capture_output=True, text=True, check=False)

    def bind(self, bare: Path) -> "_Runner":
        self._bare = bare
        return self


def _body(*allowed: str) -> str:
    return "## Allowed Paths\n\n" + "\n".join(f"- `{path}`" for path in allowed) + "\n\n## Stop Conditions\n"


@pytest.fixture
def main_drift_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    bare = tmp_path / "origin.git"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    _commit(repo, "allowed.txt", "base\n", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(tmp_path, "init", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "origin", f"{base}:refs/heads/main")
    _git(repo, "push", "origin", f"{base}:refs/heads/target")
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo, bare, base


def _execute(repo: Path, bare: Path, *, source_base, source_head, body, current_base_sha):
    runner = _Runner(issue_body=body).bind(bare)
    expected = _git(bare, "rev-parse", "refs/heads/target")
    return MODULE.execute(
        repo="owner/repo",
        issue_number=2102,
        pr_number=2118,
        target_branch="target",
        expected_remote_pr_head=expected,
        source_base=source_base,
        source_head=source_head,
        project_root=repo,
        runner=runner,
        current_base_branch="main",
        expected_current_base_sha=current_base_sha,
    )["PR_HEAD_REPLAY_PUBLISH_RESULT_V1"]


def test_given_main_drift_reconciliation_when_scope_clean_then_publishes_with_final_net_diff(main_drift_repo):
    repo, bare, base = main_drift_repo
    head = _commit(repo, "allowed.txt", "candidate\n", "candidate change")
    live_main_sha = _git(bare, "rev-parse", "refs/heads/main")

    result = _execute(
        repo, bare, source_base=base, source_head=head,
        body=_body("allowed.txt"), current_base_sha=live_main_sha,
    )

    assert result["status"] == "ok"
    assert result["pushed"] is True
    assert result["candidate_final_net_diff"] == ["allowed.txt"]


def test_given_main_drift_reconciliation_when_current_base_has_drifted_then_blocked(main_drift_repo):
    repo, bare, base = main_drift_repo
    head = _commit(repo, "allowed.txt", "candidate\n", "candidate change")
    stale_main_sha = "f" * 40

    result = _execute(
        repo, bare, source_base=base, source_head=head,
        body=_body("allowed.txt"), current_base_sha=stale_main_sha,
    )

    assert result["status"] == "blocked"
    assert "current_base_drift_before_publish" in result["errors"]


def test_given_main_drift_reconciliation_when_candidate_conflicts_with_current_main_then_blocked(main_drift_repo):
    repo, bare, base = main_drift_repo
    # Advance main with a change to the same line the candidate also touches,
    # producing a real (deterministic, git-detected) merge conflict.
    _commit(repo, "allowed.txt", "main-side-change\n", "main advances")
    _git(repo, "push", "origin", "HEAD:refs/heads/main")
    live_main_sha = _git(bare, "rev-parse", "refs/heads/main")

    # Candidate is built by resetting to base and applying a *different*
    # change to the same line.
    _git(repo, "checkout", "-B", "target-source", base)
    head = _commit(repo, "allowed.txt", "candidate-side-change\n", "candidate change")

    result = _execute(
        repo, bare, source_base=base, source_head=head,
        body=_body("allowed.txt"), current_base_sha=live_main_sha,
    )

    assert result["status"] == "blocked"
    assert "current_base_merge_conflict" in result["errors"]


def test_given_main_drift_reconciliation_when_final_net_diff_touches_disallowed_path_then_blocked(main_drift_repo):
    repo, bare, base = main_drift_repo
    # Simulate the PR branch already carrying an out-of-scope commit
    # (other.txt) before this replay/publish transaction begins. main is
    # untouched, so a base(main)..candidate final net diff includes
    # other.txt even though this transaction's source range only touches
    # allowed.txt -- proving the final-net-diff gate is a distinct check
    # from the source-range gate above it.
    pre_existing = _commit(repo, "other.txt", "pre-existing\n", "pr branch already diverged")
    _git(repo, "push", "origin", f"{pre_existing}:refs/heads/target")

    source_base = pre_existing
    head = _commit(repo, "allowed.txt", "candidate\n", "candidate change")
    live_main_sha = _git(bare, "rev-parse", "refs/heads/main")

    result = _execute(
        repo, bare, source_base=source_base, source_head=head,
        body=_body("allowed.txt"), current_base_sha=live_main_sha,
    )

    assert result["status"] == "blocked"
    assert "candidate_final_net_diff_contains_disallowed_path" in result["errors"]
    assert "other.txt" in result["disallowed_paths"]
