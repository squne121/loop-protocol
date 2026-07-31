"""
test_skill_runtime_exec_anchor.py

Real subprocess chain tests for `preflight.run.with_anchor` in
skill_runtime_exec.py (Issue #1498).

Covers AC4 (executor reaches real subprocess for Matrix #2, Matrix #4 exits 2)
and AC9 (real executor chain positive + negative smoke).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
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
    },
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    "contract_update.run.with_anchor": {
        "id": "contract_update.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--consume-contract-patch-plan",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_contract_update_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        elif token == "{anchor_comment_url}":
            rendered.append(str(values["anchor_comment_url"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / "scripts" / "agent-guards" / "fake_issue_edit_txn.py",
        """from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text())
    artifact_dir = Path(request["artifact_dir"])
    (artifact_dir / "transaction_invoked.json").write_text(json.dumps(request))
    metadata_dir = Path("artifacts") / request["issue_number"] / "issue-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "transaction.json").write_text(json.dumps(request))
    (artifact_dir / "final_body.txt").write_text(request["candidate_body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--consume-contract-patch-plan", action="store_true")
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_number": args.issue_number,
        "repo": args.repo,
        "anchor_comment_url": args.anchor_comment_url,
    }
    if args.consume_contract_patch_plan:
        final_body = artifact_dir / "final_body.txt"
        if final_body.exists():
            handoff = {
                "status": "no_change", "writes": 0, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        else:
            candidate_body = "## Outcome\\ntrusted desired state\\n"
            request = artifact_dir / "issue_edit_txn_input.json"
            request.write_text(json.dumps({
                "schema": "ISSUE_EDIT_TXN_INPUT_V1", "artifact_dir": str(artifact_dir),
                "issue_number": args.issue_number,
                "candidate_body": candidate_body,
            }))
            subprocess.run(
                [sys.executable, "scripts/agent-guards/fake_issue_edit_txn.py", str(request)],
                check=True, shell=False,
            )
            assert final_body.read_text() == candidate_body
            (artifact_dir / "fresh_check_input.json").write_text(json.dumps({"body": candidate_body}))
            handoff = {
                "status": "applied", "writes": 1, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        payload["contract_update"] = handoff
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"


def _run_executor(
    repo: Path,
    command_id: str = "preflight.run.with_anchor",
    issue_number: str = "1498",
    repo_slug: str = "squne121/loop-protocol",
    anchor_comment_url: "str | None" = _VALID_URL,
    extra_args: "list[str] | None" = None,
    extra_env: "dict[str, str] | None" = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        command_id,
        "--issue-number",
        issue_number,
        "--repo",
        repo_slug,
    ]
    if anchor_comment_url is not None:
        argv += ["--anchor-comment-url", anchor_comment_url]
    if extra_args:
        argv += extra_args
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC4 / AC9: Matrix #2 reaches the real subprocess chain.
# ---------------------------------------------------------------------------


def test_executor_reaches_subprocess_and_rejects_anchor_on_preflight_run(tmp_path: Path) -> None:
    """AC4: Matrix #2 (valid anchor) reaches real subprocess execution;
    Matrix #4 (anchor on preflight.run) exits 2 without running a subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    # Matrix #2: positive — real subprocess chain executes end to end.
    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    assert artifact.exists(), "expected preflight artifact to be created by the real subprocess"
    payload = json.loads(artifact.read_text())
    assert payload == {
        "issue_number": "1498",
        "repo": "squne121/loop-protocol",
        "anchor_comment_url": _VALID_URL,
    }
    assert json.loads(positive.stdout)["anchor_comment_url"] == _VALID_URL

    # Matrix #4: negative — anchor flag rejected for preflight.run before any
    # subprocess is spawned.
    negative = _run_executor(repo, command_id="preflight.run", issue_number="1499")
    assert negative.returncode == 2, negative.stderr
    assert "anchor-comment-url" in negative.stderr
    no_artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1499" / "preflight.json"
    assert not no_artifact.exists()


def test_executor_real_subprocess_smoke_positive_and_negative(tmp_path: Path) -> None:
    """AC9: real executor chain positive and negative smoke via subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr

    # Negative: preflight.run.fixture must reject --anchor-comment-url too.
    fixture_negative = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run.fixture",
            "--issue-number",
            "1498",
            "--repo",
            "squne121/loop-protocol",
            "--fixture",
            "tmp/fixture.json",
            "--anchor-comment-url",
            _VALID_URL,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )
    assert fixture_negative.returncode == 2, fixture_negative.stderr

    # Negative: missing anchor-comment-url for preflight.run.with_anchor.
    missing_anchor = _run_executor(repo, anchor_comment_url=None)
    assert missing_anchor.returncode == 2, missing_anchor.stderr
    assert "required" in missing_anchor.stderr


def test_executor_rejects_context_mismatched_anchor_url(tmp_path: Path) -> None:
    """Matrix #22: URL owner/repo/issue must bind to the CLI arguments."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    mismatched = _run_executor(
        repo,
        anchor_comment_url="https://github.com/other/repo/issues/1498#issuecomment-1",
    )
    assert mismatched.returncode == 2, mismatched.stderr


def test_executor_preflight_run_unaffected_without_anchor(tmp_path: Path) -> None:
    """AC1: preflight.run (no anchor) continues to work exactly as before."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run", anchor_comment_url=None)
    assert result.returncode == 0, result.stderr
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    payload = json.loads(artifact.read_text())
    assert payload["anchor_comment_url"] is None


def test_contract_update_phase_reaches_fake_transaction_and_fresh_handoff(tmp_path: Path) -> None:
    """#1877 AC3/AC6/AC10: real registry -> policy -> executor -> phase process.

    The fixture retains the production command id and argv shape, then uses a
    separate fake controlled transaction process so this test never writes a
    live GitHub Issue.  A second invocation proves idempotent no-change
    routing without a second transaction write.
    """
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    first = _run_executor(repo, command_id="contract_update.run.with_anchor")
    assert first.returncode == 0, first.stderr
    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    request = json.loads((artifact_dir / "issue_edit_txn_input.json").read_text())
    assert request["schema"] == "ISSUE_EDIT_TXN_INPUT_V1"
    assert (artifact_dir / "transaction_invoked.json").exists()
    assert (repo / "artifacts" / "1498" / "issue-metadata" / "transaction.json").exists()
    assert (artifact_dir / "final_body.txt").exists()
    assert (artifact_dir / "fresh_check_input.json").exists()
    assert json.loads(first.stdout)["contract_update"] == {
        "status": "applied",
        "writes": 1,
        "iterations": 0,
        "final_readback": "verified",
        "fresh_preflight": "pass",
        "fresh_review": "approve",
        "fresh_readiness": "go",
    }

    replay = _run_executor(repo, command_id="contract_update.run.with_anchor")
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["contract_update"]["status"] == "no_change"
    assert json.loads(replay.stdout)["contract_update"]["writes"] == 0


def test_contract_update_phase_cannot_be_reached_through_preflight_command(tmp_path: Path) -> None:
    """The read-only preflight command never receives the consumer flag."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run.with_anchor")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "contract_update" not in payload
    artifact_dir = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    assert not (artifact_dir / "transaction_invoked.json").exists()
