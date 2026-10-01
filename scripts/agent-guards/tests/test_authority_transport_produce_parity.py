"""#2842 AC6/AC7: real canonical-root production executor subprocess parity."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import warnings
from pathlib import Path

import pytest

WORKTREE = Path(__file__).resolve().parents[3]
POLICY_DIR = WORKTREE / "scripts" / "agent-guards"
sys.path.insert(0, str(POLICY_DIR))
from skill_runtime_command_policy import (  # noqa: E402
    ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS, SKILL_RUNTIME_COMMAND_POLICY_V2,
    command_allows_root_no_worktree, parse_exact_skill_runtime_authority_transport_produce_command,
    ExactSkillRuntimeCommand,
)

REPO = "squne121/loop-protocol"
# Process-specific sentinel avoids another run's artifact/cleanup racing ours.
# No real GitHub Issue/worktree has this test-only number.
ISSUE = 90_000_000 + os.getpid()


def _canonical_main_root() -> Path:
    common = subprocess.run(["git", "-C", str(WORKTREE), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            text=True, capture_output=True, check=True).stdout.strip()
    root = Path(common).resolve().parent
    branch = subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                            text=True, capture_output=True, check=True).stdout.strip()
    assert branch == "main", "AC6 needs the real canonical main root; a temp repo is not evidence"
    return root


def _command(root: Path, fixture: str, sha: str, *, repo: str = REPO, extra: tuple[str, ...] = (), env=None):
    argv = [sys.executable, str(WORKTREE / "scripts/agent-guards/skill_runtime_exec.py"),
            "--command-id", "authority_transport.produce", "--issue-number", str(ISSUE),
            "--repo", repo, "--invocation-id", f"issue2842-{uuid.uuid4().hex}",
            "--git-head-sha", sha, "--evidence-fixture-path", fixture, *extra]
    run_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    run_env.pop("LOOP_DEFAULT_BRANCH", None)
    if env:
        run_env.update(env)
    return subprocess.run(argv, cwd=root, env=run_env, capture_output=True, text=True, timeout=90)


# Only independently observed root-pipeline scratch filenames are eligible
# for bounded test re-invocation. The actual production executor still rejects
# EVERY write outside its own issue artifact, including these paths. An
# arbitrary tmp/ path (or a renamed/modified pipeline file) must fail the test.
# The second entry is the exact root-owned readiness artifact observed while
# #2842's AC6 ran concurrently with the #2854 review; it is NOT a wildcard.
_FOREIGN_SCRATCH_FAILURE = re.compile(
    rf"SKILL_RUNTIME_FAIL: reason_code=unauthorized_write_path target_issue={ISSUE} "
    r"unauthorized write path=(tmp/root_review_pipeline_[a-z0-9_]{1,32}/"
    r"root_review_pipeline_body_[a-z0-9_]{1,32}\.md|"
    r"tmp/r2854/readiness2\.json|"
    r"\.skill-runtime-git-hooks-[a-z0-9_]{1,32}) "
    r"recovery=do_not_write_outside_allowed_root"
)
_MAX_REAL_INVOCATIONS = 12


def _run_producer_avoiding_foreign_root_scratch(root: Path, fixture: str, sha: str):
    # A failed invocation is NEVER counted as a PASS: each retry runs the
    # production subprocess anew and only its successful manifest is checked.
    # CI runs this file in the serial lane so xdist siblings do not introduce
    # races; this bound handles independent root-owned review processes.
    result = None
    for attempt in range(_MAX_REAL_INVOCATIONS):
        result = _command(root, fixture, sha)
        if result.returncode == 0:
            return result
        if (result.returncode != 2 or result.stdout
                or not _FOREIGN_SCRATCH_FAILURE.fullmatch(result.stderr.strip())):
            return result
        if attempt + 1 < _MAX_REAL_INVOCATIONS:
            warnings.warn("foreign root scratch write raced the executor; "
                          "invocation failed closed, rerunning real producer",
                          RuntimeWarning, stacklevel=2)
            time.sleep(0.3)
    return result


def test_given_no_linked_issue_worktree_when_real_producer_executes_then_local_manifest_created():
    root = _canonical_main_root()
    catalog = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                             text=True, capture_output=True, check=True).stdout
    assert f"issue-{ISSUE}-" not in catalog
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
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         text=True, capture_output=True, check=True).stdout.strip()
    try:
        result = _run_producer_avoiding_foreign_root_scratch(root, str(fixture.relative_to(root)), sha)
        assert result.returncode == 0, (result.stdout[-1000:], result.stderr[-1000:])
        payload = json.loads(result.stdout)
        assert payload["status"] == "ok"
        manifest_path = Path(payload["manifest_path"])
        assert manifest_path.is_file() and manifest_path.is_relative_to(artifact_dir)
        assert json.loads(manifest_path.read_text())["git_head_sha"] == sha
    finally:
        shutil.rmtree(artifact_dir)


@pytest.mark.parametrize("path", [
    "tmp/other_writer/file.md",
    "tmp/root_review_pipeline_abcdefgh/unrelated.md",
    ".skill-runtime-git-hooks-abcdefgh/unrelated.md",
    ".claude/artifacts/issue-refinement-loop/other-issue/file.json",
])
def test_genuine_unauthorized_write_is_not_retried(monkeypatch, tmp_path, path):
    failure = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="",
        stderr=(f"SKILL_RUNTIME_FAIL: reason_code=unauthorized_write_path target_issue={ISSUE} "
                f"unauthorized write path={path} recovery=do_not_write_outside_allowed_root\n"),
    )
    calls = []

    def fake_command(*args):
        calls.append(args)
        return failure

    monkeypatch.setattr(sys.modules[__name__], "_command", fake_command)
    assert _run_producer_avoiding_foreign_root_scratch(tmp_path, "fixture.json", "a" * 40) is failure
    assert len(calls) == 1


def test_known_foreign_scratch_collision_requires_second_successful_invocation(monkeypatch, tmp_path):
    failure = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="",
        stderr=(f"SKILL_RUNTIME_FAIL: reason_code=unauthorized_write_path target_issue={ISSUE} "
                "unauthorized write path=tmp/root_review_pipeline_abcdefgh/"
                "root_review_pipeline_body_abcdefgh.md recovery=do_not_write_outside_allowed_root\n"),
    )
    success = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"status":"ok"}', stderr="")
    calls = []

    def fake_command(*args):
        calls.append(args)
        return (failure, success)[len(calls) - 1]

    monkeypatch.setattr(sys.modules[__name__], "_command", fake_command)
    with pytest.warns(RuntimeWarning, match="invocation failed closed"):
        result = _run_producer_avoiding_foreign_root_scratch(tmp_path, "fixture.json", "a" * 40)
    assert result is success
    assert len(calls) == 2


@pytest.mark.parametrize("foreign_path", [
    "tmp/root_review_pipeline_abcdefgh/root_review_pipeline_body_abcdefgh.md",
    "tmp/r2854/readiness2.json",
])
def test_foreign_scratch_collision_requires_real_success_not_failed_retry(
    monkeypatch, tmp_path, foreign_path
):
    failure = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="",
        stderr=(f"SKILL_RUNTIME_FAIL: reason_code=unauthorized_write_path target_issue={ISSUE} "
                f"unauthorized write path={foreign_path} recovery=do_not_write_outside_allowed_root\n"),
    )
    calls = []

    def fake_command(*args):
        calls.append(args)
        return failure

    monkeypatch.setattr(sys.modules[__name__], "_command", fake_command)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with pytest.warns(RuntimeWarning, match="invocation failed closed"):
        result = _run_producer_avoiding_foreign_root_scratch(tmp_path, "fixture.json", "a" * 40)
    assert result is failure  # Exhaustion is FAIL; no SKIP or fake PASS.
    assert len(calls) == _MAX_REAL_INVOCATIONS


@pytest.mark.parametrize("change,expected", [
    ("repo", "repo_mismatch"),
    ("branch", "branch_mismatch"),
    ("argv", "invalid_argv"),
])
def test_given_invalid_producer_input_when_real_executor_runs_then_bounded_distinct_stderr(change, expected):
    root = _canonical_main_root()
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         text=True, capture_output=True, check=True).stdout.strip()
    kwargs = {"repo": "foreign/repo"} if change == "repo" else {}
    if change == "branch":
        kwargs["env"] = {"LOOP_DEFAULT_BRANCH": "intentionally-wrong-default"}
    if change == "argv":
        kwargs["extra"] = ("--unexpected-OWNER-PRIVATE-SENTINEL",)
    result = _command(root, f".claude/artifacts/issue-refinement-loop/{ISSUE}/no-fixture.json", sha, **kwargs)
    assert result.returncode != 0
    assert result.stderr.strip() == f"skill_runtime_exec: authority_transport.produce: {expected}"
    assert "PRIVATE" not in result.stderr
    assert not (root / ".claude/artifacts/issue-refinement-loop" / str(ISSUE)).exists()


def test_given_registry_command_when_parsed_then_executor_and_policy_share_exact_flag_grammar():
    from skill_runtime_command_policy import SKILL_RUNTIME_EXEC_REL
    root = _canonical_main_root()
    command = (f"uv run python3 {SKILL_RUNTIME_EXEC_REL} --command-id authority_transport.produce "
               f"--issue-number {ISSUE} --repo {REPO} --invocation-id issue2842-test "
               f"--git-head-sha {'a' * 40} --produce-authority-transport evidence.json")
    parsed = parse_exact_skill_runtime_authority_transport_produce_command(command, str(root))
    assert parsed is not None
    assert command_allows_root_no_worktree(parsed)
