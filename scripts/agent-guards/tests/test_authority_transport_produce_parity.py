"""#2842 AC6/AC7: authentic repository, production executor subprocess parity.

CI checks out the PR as detached HEAD. An isolated Git root whose main branch
points to the *actual checked-out commit* exercises the same tracked registry,
policy and executor without mutating the primary checkout or relying on other
processes not to write there. No registry stub, monkeypatched executor, GitHub
mutation, linked Issue worktree, or production write-guard exemption is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[3]
POLICY_DIR = SOURCE_ROOT / "scripts" / "agent-guards"
sys.path.insert(0, str(POLICY_DIR))
from skill_runtime_command_policy import (  # noqa: E402
    ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS,
    SKILL_RUNTIME_COMMAND_POLICY_V2,
    ExactSkillRuntimeCommand,
    command_allows_root_no_worktree,
    parse_exact_skill_runtime_authority_transport_produce_command,
)

REPO = "squne121/loop-protocol"
ISSUE = 90_000_000 + os.getpid()  # Not any live or linked Issue worktree.
PRODUCTION_FILES = (
    "scripts/agent-guards/skill_runtime_exec.py",
    "scripts/agent-guards/skill_runtime_command_policy.py",
    ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
    ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
)


def _git(*argv: str, cwd: Path) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *argv], check=True,
                            capture_output=True, text=True, timeout=90)
    return result.stdout.strip()


@pytest.fixture(scope="module")
def canonical_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a real offline Git checkout of the source HEAD, on main.

    Fetch HEAD from the actual worktree (not GitHub) so detached CI PR merge
    commits and local linked worktrees both provide their actual code. The
    only repository mutations are under pytest's isolated tmp_path. An
    origin GitHub URL is metadata for the production repo-binding policy;
    there is no network request or remote mutation in this fixture.
    """
    root = tmp_path_factory.mktemp("issue2842-authentic-root") / "repo"
    root.mkdir()
    _git("init", "-q", "-b", "unused-unborn-fixture", cwd=root)
    source_head = _git("rev-parse", "HEAD", cwd=SOURCE_ROOT)
    _git("fetch", "--quiet", "--update-shallow", "--no-tags", str(SOURCE_ROOT), "HEAD", cwd=root)
    assert _git("rev-parse", "FETCH_HEAD", cwd=root) == source_head
    _git("switch", "--quiet", "--create", "main", "FETCH_HEAD", cwd=root)
    _git("remote", "add", "origin", f"https://github.com/{REPO}.git", cwd=root)
    assert _git("rev-parse", "HEAD", cwd=root) == source_head
    assert _git("branch", "--show-current", cwd=root) == "main"
    common_dir = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=root)
    assert Path(common_dir).resolve() == (root / ".git").resolve()
    for relative in PRODUCTION_FILES:
        assert hashlib.sha256((root / relative).read_bytes()).digest() == hashlib.sha256(
            (SOURCE_ROOT / relative).read_bytes()
        ).digest(), f"real production bytes drifted: {relative}"
    # Provision the isolated environment BEFORE the executor snapshots its
    # checkout; no dependency setup is part of the producer dispatch itself.
    subprocess.run(["uv", "sync", "--locked", "--group", "dev"], cwd=root,
                   check=True, capture_output=True, text=True, timeout=180)
    assert _git("status", "--porcelain", cwd=root) == ""
    return root


def _command(root: Path, fixture: str, sha: str, *, repo: str = REPO,
             extra: tuple[str, ...] = (), env=None):
    argv = [sys.executable, str(root / "scripts/agent-guards/skill_runtime_exec.py"),
            "--command-id", "authority_transport.produce", "--issue-number", str(ISSUE),
            "--repo", repo, "--invocation-id", f"issue2842-{uuid.uuid4().hex}",
            "--git-head-sha", sha, "--evidence-fixture-path", fixture, *extra]
    run_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    run_env.pop("LOOP_DEFAULT_BRANCH", None)
    if env:
        run_env.update(env)
    return subprocess.run(argv, cwd=root, env=run_env, capture_output=True,
                          text=True, timeout=90)


def test_given_no_linked_issue_worktree_when_real_producer_executes_then_local_manifest_created(
    canonical_root: Path,
):
    root = canonical_root
    catalog = _git("worktree", "list", "--porcelain", cwd=root)
    assert f"issue-{ISSUE}-" not in catalog
    assert catalog.count("worktree ") == 1
    policy = SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]["authority_transport.produce"]
    assert policy["required_cwd"] == "canonical_main_root"
    assert policy["required_branch"] == "default_branch"
    assert policy["network_effect"] == "local_only"
    assert "authority_transport.produce" in ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS
    assert command_allows_root_no_worktree(ExactSkillRuntimeCommand(
        "authority_transport.produce", str(ISSUE), REPO, (),
    ))
    artifact_dir = root / ".claude/artifacts/issue-refinement-loop" / str(ISSUE)
    assert not artifact_dir.exists(), "test must not remove a foreign artifact"
    artifact_dir.mkdir(parents=True)
    fixture = artifact_dir / "fixture.json"
    fixture.write_text(json.dumps({"source_kind": "generated_by_agent"}), encoding="utf-8")
    sha = _git("rev-parse", "HEAD", cwd=root)
    result = _command(root, str(fixture.relative_to(root)), sha)
    assert result.returncode == 0, (result.stdout[-1000:], result.stderr[-1000:])
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    manifest_path = Path(payload["manifest_path"])
    assert manifest_path.is_file() and manifest_path.is_relative_to(artifact_dir)
    assert json.loads(manifest_path.read_text())["git_head_sha"] == sha
    assert _git("status", "--porcelain", cwd=root) == ""


@pytest.mark.parametrize("change,expected", [
    ("repo", "repo_mismatch"),
    ("branch", "branch_mismatch"),
    ("argv", "invalid_argv"),
])
def test_given_invalid_producer_input_when_real_executor_runs_then_bounded_distinct_stderr(
    canonical_root: Path, change, expected,
):
    root = canonical_root
    sha = _git("rev-parse", "HEAD", cwd=root)
    kwargs = {"repo": "foreign/repo"} if change == "repo" else {}
    if change == "branch":
        kwargs["env"] = {"LOOP_DEFAULT_BRANCH": "intentionally-wrong-default"}
    if change == "argv":
        kwargs["extra"] = ("--unexpected-OWNER-PRIVATE-SENTINEL",)
    result = _command(root, f".claude/artifacts/issue-refinement-loop/{ISSUE}/no-fixture.json", sha, **kwargs)
    assert result.returncode != 0
    assert result.stderr.strip() == f"skill_runtime_exec: authority_transport.produce: {expected}"
    assert "PRIVATE" not in result.stderr


def test_given_registry_command_when_parsed_then_executor_and_policy_share_exact_flag_grammar(
    canonical_root: Path,
):
    from skill_runtime_command_policy import SKILL_RUNTIME_EXEC_REL
    root = canonical_root
    command = (f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id authority_transport.produce "
               f"--issue-number {ISSUE} --repo {REPO} --invocation-id issue2842-test "
               f"--git-head-sha {'a' * 40} --produce-authority-transport evidence.json")
    parsed = parse_exact_skill_runtime_authority_transport_produce_command(command, str(root))
    assert parsed is not None
    assert command_allows_root_no_worktree(parsed)
