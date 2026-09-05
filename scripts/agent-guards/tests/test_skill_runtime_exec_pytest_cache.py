from __future__ import annotations

# Regression tests for Issue #1526: repo-root `.pytest_cache/**` is peer
# pytest's ordinary generated/disposable cache state and must not be
# misattributed to skill_runtime_exec.py's own executor child as an
# unauthorized write.
#
# Reframe rationale (see Issue #1526 Background): pytest's own cacheprovider
# plugin (`NFPlugin`/`LFPlugin` in `_pytest/cacheprovider.py`) writes both
# `cache/nodeids` and `cache/lastfailed` unconditionally from
# `pytest_sessionfinish`, and third-party plugins can write arbitrary
# additional keys under `.pytest_cache/v/cache/` via the public
# `config.cache` API. There is no technical basis for treating `nodeids` as
# legitimate while treating `lastfailed` or a custom key as suspicious, so
# the whole `.pytest_cache/**` tree at repo root is treated uniformly as
# disposable peer-generated state (mirroring
# `_RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS`'s existing directory-root
# exclusion class), while everything outside that exact root -- including
# nested `foo/.pytest_cache/**` and prefix-lookalike `.pytest_cache2/**` --
# remains fully audited.
#
# Fixture pattern reused from
# `test_skill_runtime_exec_unauthorized_write_path.py` (peer-write-during-
# sleep race, `SKILL_RUNTIME_TEST_*` env-var-driven fixture child).

import json
import os
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path


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
    # dedicated-worktree path, #2197) so
    # `capture_primary_checkout_invariant_snapshot()`'s `git status` at
    # `project_root` never sees it as untracked drift.
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.pytest_cache/\n.claude/worktrees/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
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
    dispatches the 4 production preflight profiles' child process under
    (mirrors `worktree_bootstrap_exec.fixed_control_plane_worktree_path()`)
    -- this fixture's own peer-write races and artifact-existence
    assertions must target here, not `repo_root`, once the child's cwd is
    the dedicated worktree."""
    return repo_root / ".claude" / "worktrees" / "control-plane-preflight"


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

    # Issue #2199: `preflight.run` is now one of the 4 production preflight
    # profiles `main()` dispatches under a dedicated worktree bound to a
    # real remote's `accepted_oid` (#2197). Source-patch the copied
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
import json
import os
import sys
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
        outside = Path(".mypy_cache")
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "outside.txt").write_text("self-write")
    # Issue #2199: deterministic independent-process barrier (no fixed
    # sleep, replaces a fixed-`delay_seconds` writer race against this
    # executor's own before-snapshot, which is unbounded under CPU load --
    # see `tests/agent_ops` sibling rationale). The child signals "the
    # before-snapshot race window is now open" by creating a go-file, then
    # blocks (bounded poll) until the peer writer thread has durably
    # created an ack-file confirming its write landed on disk, before the
    # child itself exits.
    go_file = os.environ.get("SKILL_RUNTIME_TEST_BARRIER_GO_FILE")
    ack_file = os.environ.get("SKILL_RUNTIME_TEST_BARRIER_ACK_FILE")
    if go_file:
        Path(go_file).parent.mkdir(parents=True, exist_ok=True)
        Path(go_file).write_text("go")
    if ack_file:
        deadline = time.monotonic() + 10.0
        while not Path(ack_file).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("barrier_ack_file_timeout")
            time.sleep(0.02)
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

    # Issue #2199 (AC7 non-regression, `test_independent_pytest_process_
    # writes_real_pytest_cache_and_is_permitted`): committed here, rather
    # than written at test-invocation time, so it is ALREADY present in the
    # dedicated worktree's checked-out `accepted_oid` (i.e. present in the
    # child dispatch's own before-snapshot) -- a real, independent
    # sibling `pytest` subprocess run with `cwd=execution_root` can collect
    # and run it without racing `main()`'s own before/after snapshot
    # timing. Its own "started" sentinel is written under `.pytest_cache/`
    # (the exempted root this whole file's AC covers), so it is never
    # itself misreported as an unauthorized new top-level file regardless
    # of exactly when it appears.
    _write_text(
        repo_root / "test_peer_pytest_cache.py",
        "from pathlib import Path\n"
        "import time\n"
        "def test_ok():\n"
        "    sentinel = Path('.pytest_cache') / 'peer_pytest_started.sentinel'\n"
        "    sentinel.parent.mkdir(parents=True, exist_ok=True)\n"
        "    sentinel.write_text('started')\n"
        "    time.sleep(0.3)\n"
        "    assert True\n",
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
            "1526",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _wait_for_path(path: Path, timeout: float) -> bool:
    """Poll (bounded, not a fixed sleep) for `path` to appear -- a simple
    file-based barrier/handshake, used here so a peer-write race thread
    never races the dedicated worktree's own `git worktree add` bootstrap
    (Issue #2199): pre-creating `_execution_root(repo)` before that
    bootstrap runs would make the fixed dedicated-worktree recovery
    fail-close as an unknown owner (#2197 AC5)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def _write_after_delay(
    path: Path, content: str, delay_seconds: float, *, wait_for: "Path | None" = None
) -> threading.Thread:
    def _worker() -> None:
        if wait_for is not None:
            assert _wait_for_path(wait_for, timeout=30), f"{wait_for} never materialized"
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def _append_after_delay(
    path: Path, content: str, delay_seconds: float, *, wait_for: "Path | None" = None
) -> threading.Thread:
    def _worker() -> None:
        if wait_for is not None:
            assert _wait_for_path(wait_for, timeout=30), f"{wait_for} never materialized"
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def test_peer_pytest_nodeids_write_is_permitted(tmp_path: Path) -> None:
    """GIVEN a peer pytest process concurrently writes
    `.pytest_cache/v/cache/nodeids`
    WHEN this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    # Issue #2199: `preflight.run` is now a production dedicated-worktree
    # profile -- the child (and its own before/after snapshot) actually run
    # under `execution_root`, not `repo`, so this peer race must target the
    # SAME root the child observes.
    peer_path = execution_root / ".pytest_cache" / "v" / "cache" / "nodeids"
    thread = _write_after_delay(
        peer_path, json.dumps(["test_x.py::test_a"]), delay_seconds=0.2, wait_for=execution_root
    )
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_peer_pytest_lastfailed_write_is_permitted(tmp_path: Path) -> None:
    """GIVEN a failing peer pytest process concurrently writes
    `.pytest_cache/v/cache/lastfailed`
    WHEN this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (Issue #1526: lastfailed is not technically distinguishable from
    nodeids as a "suspicious" write -- both are ordinary cacheprovider
    output)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    peer_path = execution_root / ".pytest_cache" / "v" / "cache" / "lastfailed"
    thread = _write_after_delay(
        peer_path, json.dumps({"test_x.py::test_a": True}), delay_seconds=0.2, wait_for=execution_root
    )
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_peer_pytest_custom_cache_key_write_is_permitted(tmp_path: Path) -> None:
    """GIVEN a third-party pytest plugin writes a custom `config.cache` key
    under `.pytest_cache/v/cache/<custom-key>` (Issue #1526: `cache.set()` is
    a public API any plugin can call, not limited to nodeids/lastfailed)
    WHEN this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    peer_path = execution_root / ".pytest_cache" / "v" / "cache" / "my_custom_plugin" / "state.json"
    thread = _write_after_delay(
        peer_path, json.dumps({"custom": True}), delay_seconds=0.2, wait_for=execution_root
    )
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_repo_root_sibling_write_outside_pytest_cache_is_rejected(tmp_path: Path) -> None:
    """GIVEN the executed child writes to a repo-root sibling cache
    directory that is NOT `.pytest_cache` (e.g. `.mypy_cache/`)
    WHEN no `.pytest_cache` exemption applies
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path (AC4: exemption is not a general "any dotfile
    cache directory" carve-out)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "ignored"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.mypy_cache/" in result.stderr
    assert "target_issue=1526" in result.stderr


def test_tracked_source_file_unauthorized_write_is_rejected(tmp_path: Path) -> None:
    """GIVEN a peer process concurrently modifies a tracked source file
    (README.md) while this command's own child subprocess is running
    WHEN no `.pytest_cache` exemption applies to a tracked file
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path (AC5: source/canonical file integrity is
    unaffected by the `.pytest_cache` reframe).

    Issue #2199: uses the deterministic go-file/ack-file barrier (not a
    fixed `delay_seconds` race) -- under heavy system load (e.g. the full
    `scripts/agent-guards/tests/` directory run), a fixed-delay writer can
    itself race `main()`'s dedicated-worktree bootstrap + before-snapshot
    capture unpredictably, occasionally landing the write AFTER the
    after-snapshot instead of during the race window (a false PASS)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    tracked_path = execution_root / "README.md"
    go_file = tmp_path / "barrier-go-tracked"
    ack_file = tmp_path / "barrier-ack-tracked"

    def _append_after_go() -> None:
        assert _wait_for_path(go_file, timeout=30), "barrier go-file never appeared"
        with tracked_path.open("a", encoding="utf-8") as handle:
            handle.write("tampered\n")
        ack_file.write_text("ack")

    thread = threading.Thread(target=_append_after_go)
    thread.start()
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_BARRIER_GO_FILE": str(go_file),
                "SKILL_RUNTIME_TEST_BARRIER_ACK_FILE": str(ack_file),
            },
        )
    finally:
        thread.join(timeout=10)
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "target_issue=1526" in result.stderr


def test_exemption_is_repo_root_specific_not_nested_or_prefix_lookalike(tmp_path: Path) -> None:
    """GIVEN a peer write targets either a nested `foo/.pytest_cache/**`
    directory or a prefix-lookalike repo-root `.pytest_cache2/**` directory
    WHEN neither is the exact repo-root `.pytest_cache` exemption
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path for both (AC6).

    Issue #2199: uses the deterministic go-file/ack-file barrier (see
    `test_tracked_source_file_unauthorized_write_is_rejected` above for the
    fixed-delay-under-load rationale) for both sub-cases."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    nested_peer = execution_root / "foo" / ".pytest_cache" / "v" / "cache" / "nodeids"
    go_file = tmp_path / "barrier-go-nested"
    ack_file = tmp_path / "barrier-ack-nested"

    def _write_nested_after_go() -> None:
        assert _wait_for_path(go_file, timeout=30), "barrier go-file never appeared"
        nested_peer.parent.mkdir(parents=True, exist_ok=True)
        nested_peer.write_text("[]")
        ack_file.write_text("ack")

    thread = threading.Thread(target=_write_nested_after_go)
    thread.start()
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_BARRIER_GO_FILE": str(go_file),
                "SKILL_RUNTIME_TEST_BARRIER_ACK_FILE": str(ack_file),
            },
        )
    finally:
        thread.join(timeout=10)
    assert result.returncode == 2, result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=foo/" in result.stderr

    (tmp_path / "repo2_root").mkdir(parents=True)
    repo2 = _make_repo(tmp_path / "repo2_root")
    _install_skill_runtime_exec_fixture(repo2)
    execution_root2 = _execution_root(repo2)
    lookalike_peer = execution_root2 / ".pytest_cache2" / "v" / "cache" / "nodeids"
    go_file2 = tmp_path / "barrier-go-lookalike"
    ack_file2 = tmp_path / "barrier-ack-lookalike"

    def _write_lookalike_after_go() -> None:
        assert _wait_for_path(go_file2, timeout=30), "barrier go-file never appeared"
        lookalike_peer.parent.mkdir(parents=True, exist_ok=True)
        lookalike_peer.write_text("[]")
        ack_file2.write_text("ack")

    thread2 = threading.Thread(target=_write_lookalike_after_go)
    thread2.start()
    try:
        result2 = _run_executor(
            repo2,
            {
                "SKILL_RUNTIME_TEST_BARRIER_GO_FILE": str(go_file2),
                "SKILL_RUNTIME_TEST_BARRIER_ACK_FILE": str(ack_file2),
            },
        )
    finally:
        thread2.join(timeout=10)
    assert result2.returncode == 2, result2.stderr
    assert "reason_code=unauthorized_write_path" in result2.stderr
    assert "unauthorized write path=.pytest_cache2" in result2.stderr


def test_independent_pytest_process_writes_real_pytest_cache_and_is_permitted(tmp_path: Path) -> None:
    """AC7 (PR #2364 review P1-2 fix): GIVEN a genuinely independent OS
    process -- a real `pytest` subprocess launched as a *sibling* of this
    command's own guarded child, by the test harness itself, not spawned and
    awaited-to-completion from inside the guarded child -- concurrently
    writes real repo-root `.pytest_cache/**` state via its actual
    cacheprovider plugin while the guarded child is still running
    WHEN both processes overlap in wall-clock time (synchronized by a file
    barrier: the peer pytest writes a "started" sentinel before it begins its
    own brief sleep-then-finish, and this test waits on that sentinel before
    invoking the executor)
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path,
    AND the real independent peer pytest process must itself have exited
    successfully (its exit code is asserted, not ignored).

    Issue #2199: `preflight.run` now dispatches its child (and its own
    before/after snapshot) under `_execution_root(repo)`, not `repo` --
    so the real peer pytest process must run there too. `execution_root`
    itself only materializes once `main()`'s dedicated-worktree bootstrap
    has run, which happens INSIDE the blocking `_run_executor()` call --
    so the executor is driven from a background thread here, and this
    thread waits (bounded poll, not a fixed sleep) for `execution_root` to
    appear before launching the real peer pytest subprocess there.
    `test_peer_pytest_cache.py` itself is committed as part of the fixture
    repo (see `_install_skill_runtime_exec_fixture`) -- already checked out
    in `execution_root` before the child's own before-snapshot is ever
    captured, so it is never itself misreported as an unauthorized new
    file (only its `.pytest_cache/**` output during the run is meant to be
    exercised as the exempted-and-genuinely-concurrent write)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)

    exec_result: dict[str, subprocess.CompletedProcess[str]] = {}

    def _drive_executor() -> None:
        exec_result["result"] = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})

    exec_thread = threading.Thread(target=_drive_executor)
    exec_thread.start()
    peer_proc: "subprocess.Popen[str] | None" = None
    peer_stdout = ""
    try:
        assert _wait_for_path(execution_root, timeout=30), "dedicated worktree never materialized"
        started_sentinel = execution_root / ".pytest_cache" / "peer_pytest_started.sentinel"
        # Issue #2199: `PYTHONDONTWRITEBYTECODE=1` for the PEER pytest
        # process only -- this AC is specifically about `.pytest_cache/**`
        # cacheprovider writes, not about repo-root `__pycache__/**`
        # bytecode compilation (a separate, unrelated, unexempted class this
        # executor still fully audits). Without this, the peer's own
        # import-time `.pyc` write for `test_peer_pytest_cache.py` races
        # the child's before-snapshot and makes this test flaky/order-
        # dependent for a reason orthogonal to the AC under test.
        peer_proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-q", "test_peer_pytest_cache.py"],
            cwd=str(execution_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert _wait_for_path(started_sentinel, timeout=10), "peer pytest never started"
        peer_stdout, _ = peer_proc.communicate(timeout=10)
    finally:
        exec_thread.join(timeout=15)
    result = exec_result["result"]
    assert peer_proc is not None and peer_proc.returncode == 0, peer_stdout
    assert result.returncode == 0, result.stderr
    real_cache_dir = execution_root / ".pytest_cache"
    assert real_cache_dir.exists()
    assert (real_cache_dir / "v" / "cache" / "nodeids").exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_pytest_cache_cold_start_temp_dir_race_is_permitted(tmp_path: Path) -> None:
    """PR #2364 review P1-1: GIVEN a peer pytest process is mid-way through
    its cold-start cache creation -- pytest's `Cache.
    _ensure_cache_dir_and_supporting_files()` first materializes a repo-root
    sibling `tempfile.TemporaryDirectory(prefix="pytest-cache-files-",
    dir=self._cachedir.parent)` (populated with README.md/.gitignore/
    CACHEDIR.TAG) and only atomically renames it onto `.pytest_cache`
    afterwards -- and that transient directory still exists, unrenamed, at
    repo root
    WHEN this command's own child subprocess's after-snapshot observes it
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (the temp dir is pytest's own disposable cache-creation implementation
    detail, not canonical evidence)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    temp_dir = execution_root / "pytest-cache-files-abc123de"
    cachedir_tag_content = "Signature: 8a477f597d28d172789f06886806bc55\n"
    thread = _write_after_delay(
        temp_dir / "CACHEDIR.TAG", cachedir_tag_content, delay_seconds=0.2, wait_for=execution_root
    )
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert temp_dir.exists()
    artifact = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_pytest_cache_cold_start_temp_dir_nested_is_rejected(tmp_path: Path) -> None:
    """PR #2364 review P1-1 scope check: GIVEN a `pytest-cache-files-*`
    -prefixed path appears nested under a subdirectory rather than directly
    at repo root
    WHEN pytest's own `dir=self._cachedir.parent` always resolves to the
    pytest rootdir (repo root for this repository), so a nested occurrence
    can never be pytest's genuine cold-start temp dir
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path (the exemption is repo-root-top-level-only).

    Issue #2199 (CI-observed race fix): uses the deterministic go-file/
    ack-file barrier (see `test_tracked_source_file_unauthorized_write_is_
    rejected` above for the fixed-delay-under-load rationale) instead of a
    fixed `delay_seconds` race -- under increased CI load a fixed 0.2s delay
    is not reliably shorter than the executor's before/after snapshot
    window, so the write can land AFTER the after-snapshot instead of
    during the race window (a false PASS / returncode 0 instead of the
    expected fail-closed returncode 2)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    execution_root = _execution_root(repo)
    nested_temp_dir = execution_root / "sub" / "pytest-cache-files-abc123de" / "CACHEDIR.TAG"
    go_file = tmp_path / "barrier-go-nested-temp-dir"
    ack_file = tmp_path / "barrier-ack-nested-temp-dir"

    def _write_nested_temp_dir_after_go() -> None:
        assert _wait_for_path(go_file, timeout=30), "barrier go-file never appeared"
        nested_temp_dir.parent.mkdir(parents=True, exist_ok=True)
        nested_temp_dir.write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
        ack_file.write_text("ack")

    thread = threading.Thread(target=_write_nested_temp_dir_after_go)
    thread.start()
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_BARRIER_GO_FILE": str(go_file),
                "SKILL_RUNTIME_TEST_BARRIER_ACK_FILE": str(ack_file),
            },
        )
    finally:
        thread.join(timeout=10)
    assert result.returncode == 2, result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=sub/" in result.stderr
