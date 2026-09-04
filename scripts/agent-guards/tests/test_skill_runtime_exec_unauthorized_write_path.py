from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


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
    # Issue #2199: also ignore `.claude/worktrees/` (the fixed
    # dedicated-worktree path, #2197) and the bare `artifacts/` pattern
    # (matches `.claude/artifacts/**` too, mirroring the real repo's own
    # `.gitignore`) so `capture_primary_checkout_invariant_snapshot()`'s
    # `git status` at `project_root` never sees either as untracked drift,
    # and so this fixed worktree can itself be reused (not fail-closed as
    # dirty) across the multiple dispatches a couple of tests below make
    # against the SAME repo.
    (repo / ".gitignore").write_text(
        ".cache/\n__pycache__/\ntmp/\n.guard_shadow_log.jsonl\n.claude/worktrees/\nartifacts/\n"
    )
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    # Issue #2252 PR #2390 review [BLOCKER 2]: the production repo-root
    # `.gitignore` keeps `.guard_shadow_log.jsonl` as a legacy tombstone (so a
    # stale local file left over from a removed producer never surfaces as an
    # ordinary untracked `??` path). Assert once, at fixture construction
    # time, that this fixture's `.gitignore` reproduces that real
    # ignored-path condition -- otherwise the create/update regression tests
    # below would only ever exercise a plain untracked file, not the actual
    # `!!` ignored-path status the executor's snapshot diff has to handle in
    # production.
    subprocess.run(
        ["git", "check-ignore", "-q", ".guard_shadow_log.jsonl"],
        cwd=str(repo),
        check=True,
    )
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_control_plane_origin(repo_root: Path, origin: Path) -> None:
    """A real, local, deterministic ``file://`` bare remote (Issue #2199)
    that Issue #2199's dedicated-worktree lifecycle (#2196/#2197/#2198) can
    bind against with no real GitHub network access
    (``network_required: false``)."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "push", "-q", str(origin), "HEAD:refs/heads/main"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        env=env,
    )


def _execution_root(repo_root: Path) -> Path:
    """The one fixed dedicated worktree path Issue #2199's wired `main()`
    dispatches `preflight.run`'s child process under (mirrors
    `worktree_bootstrap_exec.fixed_control_plane_worktree_path()`) -- this
    fixture's own seed writes, peer-write races, and artifact-existence
    assertions must target here, not `repo_root`, once the child's cwd is
    the dedicated worktree."""
    return repo_root / ".claude" / "worktrees" / "control-plane-preflight"


def _wait_for_path(path: Path, timeout: float) -> bool:
    """Poll (bounded, not a fixed sleep) for `path` to appear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _materialize_execution_root(repo: Path) -> Path:
    """Issue #2199: `execution_root` only comes into existence once
    `main()`'s dedicated-worktree bootstrap runs (inside a real
    `_run_executor()` call). `test_shadow_log_create_or_update_fails_as_
    generic_unauthorized_write_path[update]` below needs to seed a stale
    `.guard_shadow_log.jsonl` that genuinely predates the real dispatch's
    before-snapshot (not a genuinely-new mid-run write, which this test is
    deliberately NOT testing) -- this runs one throwaway, no-extra-env
    dispatch first solely to materialize `execution_root` (a no-op fixture
    child, see `run_refinement_preflight.py` below), then returns it for
    the caller to seed directly. Reused as-is (#2197 AC5) on the test's own
    subsequent real dispatch, as long as `execution_root`'s own working
    tree stays clean/git-ignored in between (`_make_repo`'s
    `.guard_shadow_log.jsonl`/`.claude/worktrees/`/`artifacts/` ignore
    entries)."""
    execution_root = _execution_root(repo)
    if not execution_root.exists():
        warm_up = _run_executor(repo)
        assert warm_up.returncode == 0, warm_up.stderr
    assert execution_root.exists()
    return execution_root


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/worktree_bootstrap_command_policy.py",
        "scripts/agent-ops/worktree_bootstrap_exec.py",
        "scripts/agent-ops/worktree_catalog.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    # Issue #2199: `preflight.run` is now a production preflight profile
    # `main()` dispatches under a dedicated worktree bound to a real
    # remote's `accepted_oid` (#2197). Source-patch the copied
    # `skill_runtime_exec.py`'s hardcoded canonical remote constant to this
    # fixture's own local, deterministic bare remote -- never the real
    # `https://github.com/...` production remote (`network_required: false`
    # / `auth_required: false` per this Issue's Runtime Verification
    # Applicability).
    control_plane_origin_path = repo_root.parent / "control-plane-origin.git"
    control_plane_remote_url = control_plane_origin_path.as_uri()
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_remote_line = 'CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{TRUSTED_REPO_SLUG}.git"'
    assert default_remote_line in executor_source
    fixture_remote_line = f"CONTROL_PLANE_CANONICAL_REMOTE_URL = {control_plane_remote_url!r}"
    executor_path.write_text(executor_source.replace(default_remote_line, fixture_remote_line), encoding="utf-8")

    pin = _pinned_uv_version(source_root)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "skill-runtime-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        # Issue #2311 AC1 fixture parity: bare `preflight.run` first-hops
        # into `workflow_start_entry.py` (a minimal fixture-local forwarder
        # to `run_refinement_preflight.py` below -- see that file) instead
        # of `run_refinement_preflight.py` directly.
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "workflow_start_entry.py",
        """from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    _inner = Path(__file__).resolve().parent / "run_refinement_preflight.py"
    _proc = subprocess.run([sys.executable, str(_inner), *sys.argv[1:]])
    raise SystemExit(_proc.returncode)
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
                        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
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
import importlib.util
import json
import os
import py_compile
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
    cache_path = Path(importlib.util.cache_from_source(__file__))
    if os.environ.get("SKILL_RUNTIME_TEST_PYC_WRITE") == "bytes":
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"not-a-valid-pyc")
    if os.environ.get("SKILL_RUNTIME_TEST_PYC_WRITE") == "compile":
        py_compile.compile(__file__, cfile=str(cache_path), doraise=True)
    if os.environ.get("SKILL_RUNTIME_TEST_PYC_WRITE") == "replace-parent":
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.parent.rmdir()
        cache_path.parent.symlink_to("../", target_is_directory=True)
    if os.environ.get("SKILL_RUNTIME_TEST_PYC_WRITE") == "modify-existing":
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(b"before")
        cache_path.write_bytes(b"after-with-different-size")
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
    shadow_log_write_mode = os.environ.get("SKILL_RUNTIME_TEST_SHADOW_LOG_WRITE")
    if shadow_log_write_mode == "create":
        # Issue #2252 AC4: `.guard_shadow_log.jsonl` no longer has a typed
        # exact-file special case in skill_runtime_exec.py -- a child
        # command creating it must now fail closed as a generic
        # unauthorized_write_path, exactly like any other untracked
        # repo-root file.
        shadow_log_path = Path(".guard_shadow_log.jsonl")
        shadow_log_path.write_text('{"schema_version":"1","timestamp":"t"}\\n')
    elif shadow_log_write_mode == "update":
        # Issue #2252 AC4 (PR #2390 review [BLOCKER 2]): the same generic
        # fail-close must also hold when `.guard_shadow_log.jsonl` already
        # exists (pre-created by the test harness before this child runs)
        # and the child only appends/updates it -- this is a different
        # detection path from create (no new path in the before/after
        # status-set diff; it is caught by the existing-path
        # mtime_ns/size/kind snapshot comparison instead), so it must be
        # exercised separately from the create case.
        shadow_log_path = Path(".guard_shadow_log.jsonl")
        shadow_log_path.write_text(
            shadow_log_path.read_text() + '{"schema_version":"1","event":"update"}\\n'
        )
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

    # Issue #2199: the dedicated worktree's child process actually runs FROM
    # the dedicated worktree checked out at the remote's `accepted_oid` --
    # everything the child needs must be part of a REAL commit this fixture
    # pushes to its own local bare origin below, not merely present,
    # uncommitted, in the outer working tree.
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install skill runtime fixture", cwd=repo_root)
    _init_control_plane_origin(repo_root, repo_root.parent / "control-plane-origin.git")


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


def _write_after_delay(
    path: Path, content: str, delay_seconds: float, *, wait_for: "Path | None" = None
) -> threading.Thread:
    def _worker() -> None:
        if wait_for is not None:
            # Issue #2199: never race the dedicated worktree's own `git
            # worktree add` bootstrap -- pre-creating `_execution_root(repo)`
            # before that runs would make the fixed dedicated-worktree
            # recovery fail-close as an unknown owner (#2197 AC5).
            assert _wait_for_path(wait_for, timeout=30), f"{wait_for} never materialized"
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
    execution_root = _execution_root(repo)
    peer_path = execution_root / ".claude" / "worktrees" / "issue-9999-peer-session" / "scratch.txt"
    thread = _write_after_delay(peer_path, "peer-session-write\n", delay_seconds=0.2, wait_for=execution_root)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_unrelated_process_write_to_other_issue_artifacts_does_not_fail(tmp_path: Path) -> None:
    """GIVEN a peer local session concurrently writing under a different
    issue's .claude/artifacts/issue-refinement-loop/<other issue>/** root
    WHEN this command's own child subprocess is still running for a
    different target issue
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    peer_path = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1337" / "peer.json"
    thread = _write_after_delay(peer_path, '{"peer": true}\n', delay_seconds=0.2, wait_for=execution_root)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
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


@pytest.mark.parametrize("writer", ["bytes", "compile", "modify-existing"])
def test_bytecode_shaped_self_write_outside_allowed_roots_fails(
    tmp_path: Path, writer: str
) -> None:
    """GIVEN a child writes to its canonical importlib cache path
    WHEN it uses bytes, py_compile, or modifies an existing cache
    THEN the executor rejects the source-tree change rather than excluding pyc."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_PYC_WRITE": writer})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "__pycache__" in result.stderr


def test_bytecode_cache_parent_symlink_replacement_fails_closed(tmp_path: Path) -> None:
    """GIVEN a child replaces its cache parent with a symlink
    WHEN the exact executor compares source-tree state
    THEN it fails closed instead of treating the cache path as volatile."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_PYC_WRITE": "replace-parent"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr


def test_self_write_inside_allowed_roots_still_succeeds(tmp_path: Path) -> None:
    """GIVEN the executed child command writes only inside its
    allowed_write_roots
    WHEN the command completes
    THEN skill_runtime_exec.py must succeed as before (no regression)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo)
    assert result.returncode == 0, result.stderr
    artifact = _execution_root(repo) / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
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
    execution_root = _execution_root(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_WORKTREES": "ignored"})
    assert result.returncode == 0, result.stderr
    self_write_path = execution_root / ".claude" / "worktrees" / "issue-9999-self" / "self-write.txt"
    assert self_write_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
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
    execution_root = _execution_root(repo)
    result = _run_executor(
        repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_OTHER_ISSUE_ARTIFACTS": "ignored"}
    )
    assert result.returncode == 0, result.stderr
    self_write_path = (
        execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1337" / "self-write.json"
    )
    assert self_write_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


@pytest.mark.parametrize("mode", ["create", "update"])
def test_shadow_log_create_or_update_fails_as_generic_unauthorized_write_path(
    tmp_path: Path, mode: str
) -> None:
    """GIVEN the executed child command creates OR updates an already
    pre-existing `.guard_shadow_log.jsonl`
    WHEN no producer-specific typed exact-file special case exists any more
    (Issue #2252 AC4) and the fixture repo's `.gitignore` reproduces the
    production ignored-path condition for this file (PR #2390 review
    [BLOCKER 2])
    THEN skill_runtime_exec.py must fail-close with the generic
    unauthorized_write_path reason code in both cases, reporting the
    shadow-log path exactly like any other repo-root file -- not silently
    authorize it via a shadow-log-specific kind/content transition policy.
    `create` is detected via the before/after status-set diff (a brand new
    path appears); `update` is detected via the existing-path
    mtime_ns/size/kind snapshot comparison instead, since the ignored path's
    status entry itself does not change -- so both detection paths must be
    exercised separately rather than assuming create coverage implies update
    coverage."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    if mode == "update":
        # Issue #2199: the stale shadow log must genuinely predate the real
        # dispatch's before-snapshot (this test is deliberately NOT testing
        # a genuinely-new mid-run creation, that's the "create" mode above)
        # -- materialize `execution_root` with a real throwaway dispatch
        # first, then seed directly into it.
        execution_root = _materialize_execution_root(repo)
        stale_shadow_log = execution_root / ".guard_shadow_log.jsonl"
        stale_shadow_log.write_text(
            '{"schema_version":"1","timestamp":"2026-01-01T00:00:00Z","event":"stale"}\n'
        )
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_WRITE": mode})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr
    assert "target_issue=1228" in result.stderr
