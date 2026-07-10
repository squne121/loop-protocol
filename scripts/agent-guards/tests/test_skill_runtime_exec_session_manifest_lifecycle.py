from __future__ import annotations

"""Real-process lifecycle integration tests for skill_runtime_exec.py (Issue #1409).

These tests exercise the *actual* `.claude/hooks/session_manifest_debounce.mjs`
and `.claude/hooks/generate_session_manifest_from_hook.mjs` scripts as real
subprocesses (not mocks), invoked from inside a fixture child command that
runs under the privileged `skill_runtime_exec.py` executor -- exactly the
scenario reported in Issue #1409's Notes for Reviewer.

Synchronization design (no fixed-sleep polling):
  The fixture child command pre-seeds a live `worker.lock` (owned by its own
  pid) before queueing an event through the real debounce front-gate, which
  suppresses the autonomous detached worker. It then releases the lock and
  invokes the real debounce front-gate with `--flush`, which is a *blocking*
  call: it does not return until the aggregated event has been synchronously
  handed to the real producer chain (spawnSync -> real
  generate_session_manifest_from_hook.mjs -> lock acquire -> temp file write
  -> atomic rename -> lock release) and the event file has been removed. This
  makes the "before-snapshot completed, hook write observed by after-snapshot"
  ordering deterministic by construction rather than by a fixed sleep --
  the child process does not return control to skill_runtime_exec.py until the
  real hook write is durably on disk.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_BIN = shutil.which("node")


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
    # Issue #1409 REQUEST_CHANGES (P1): the fixture `.gitignore` must match
    # the real repo's `.gitignore`, which ignores `artifacts/` as a whole
    # (not just a narrow `artifacts/session-manifest-runtime/` pattern).
    # A narrower fixture pattern hides the ignored-ancestor-folding bug that
    # only reproduces when Git collapses the *entire* `artifacts/` tree into
    # a single `!! artifacts/` status entry.
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\nartifacts/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


_RUN_REFINEMENT_PREFLIGHT_PY = '''from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def _node_env(repo_root: str) -> dict[str, str]:
    node_bin = os.environ["SKILL_RUNTIME_TEST_NODE_BIN"]
    node_dir = os.path.dirname(node_bin)
    return {
        "PATH": node_dir + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "CLAUDE_PROJECT_DIR": repo_root,
        "SESSION_MANIFEST_PRODUCER_SCRIPT": os.path.join(
            repo_root, "scripts", "agent-guards", "tests", "fixtures", "stub_manifest_producer.mjs"
        ),
    }


def _run_real_session_manifest_lifecycle(repo_root: str, variant: str = "default") -> None:
    """Drive the REAL debounce + generate hook scripts synchronously.

    Pre-seeds a live worker.lock (owned by this process) so the front-gate
    does not spawn an autonomous detached worker, queues one event, releases
    the lock, then runs `--flush` (a blocking call) so the producer chain
    (lock/tmp/atomic-rename/event-removal) completes durably before this
    function returns control to the caller.

    `variant` is folded into the event payload (session_id + file_path) so
    that repeated invocations within the same fixture repo produce distinct
    stable-key digests -- the real generate hook's duplicate-artifact guard
    (Issue #1409 fixture reuse across multiple assertions in one test) would
    otherwise silently short-circuit a second invocation with an identical
    payload as a no-op duplicate skip.
    """
    node_bin = os.environ["SKILL_RUNTIME_TEST_NODE_BIN"]
    debounce_path = os.path.join(repo_root, ".claude", "hooks", "session_manifest_debounce.mjs")
    lock_path = os.path.join(repo_root, "artifacts", "session-manifest-runtime", "locks", "worker.lock")
    env = _node_env(repo_root)

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    now_ms = int(time.time() * 1000)
    with open(lock_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"owner_pid": os.getpid(), "role": "worker", "started_at_ms": now_ms, "heartbeat_at_ms": now_ms},
            fh,
        )

    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "cwd": repo_root,
        "session_id": f"lifecycle-test-{variant}",
        "tool_input": {"file_path": os.path.join(repo_root, "docs", "dev", f"lifecycle-test-{variant}.md")},
    }
    enqueue = subprocess.run(
        [node_bin, debounce_path],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
        timeout=30,
    )
    if enqueue.returncode != 0:
        raise RuntimeError(f"debounce_enqueue_failed rc={enqueue.returncode} stderr={enqueue.stderr}")

    os.remove(lock_path)

    flush = subprocess.run(
        [node_bin, debounce_path, "--flush"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        env=env,
        timeout=30,
    )
    if flush.returncode != 0:
        raise RuntimeError(f"debounce_flush_failed rc={flush.returncode} stderr={flush.stderr}")


def _run_real_session_manifest_lifecycle_with_forced_failed_rename(repo_root: str) -> None:
    """Same as above, but makes the manifests/ dir read-only after warm-up so
    the real generate_session_manifest_from_hook.mjs write succeeds but the
    atomic rename into manifests/ fails, exercising the failed-rename branch
    (renameSync(tmpPath, `${tmpPath}.failed`))."""
    manifests_dir = Path(repo_root) / "artifacts" / "session-manifest-runtime" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifests_dir.chmod(0o500)
        _run_real_session_manifest_lifecycle(repo_root, variant="failed-rename")
    finally:
        manifests_dir.chmod(0o700)


def main() -> int:
    parser = argparse_module()
    args = parser.parse_args()

    if os.environ.get("SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE") == "enabled":
        _run_real_session_manifest_lifecycle(os.getcwd())

    if os.environ.get("SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE_FAILED_RENAME") == "enabled":
        _run_real_session_manifest_lifecycle_with_forced_failed_rename(os.getcwd())

    if os.environ.get("SKILL_RUNTIME_TEST_SELF_WRITE_SESSION_MANIFEST_RUNTIME") == "ignored":
        # Issue #1409 AC4: a self-write into the new hook-owned subtree by the
        # child command itself (not the real hook) is a known, accepted
        # limitation of the stdlib-only race-tolerant exclusion -- it cannot
        # be distinguished from a legitimate hook write.
        self_write_path = (
            Path("artifacts") / "session-manifest-runtime" / "manifests" / "self-write.json"
        )
        self_write_path.parent.mkdir(parents=True, exist_ok=True)
        self_write_path.write_text('{"self_write": true}')

    if os.environ.get("SKILL_RUNTIME_TEST_SELF_WRITE_OUTSIDE_SESSION_MANIFEST_RUNTIME") == "ignored":
        # Issue #1409 AC3: a self-write into artifacts/ *outside* the new
        # subtree must still fail-close (no regression -- the exclusion is
        # narrow, not repo-root artifacts/ as a whole).
        outside_path = Path("artifacts") / "unrelated" / "self-write.txt"
        outside_path.parent.mkdir(parents=True, exist_ok=True)
        outside_path.write_text("self-write-outside-session-manifest-runtime")

    if os.environ.get("SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_SYMLINK") == "enabled":
        # Issue #1409 REQUEST_CHANGES (P1 test 5): a parent-substitution
        # attack -- `artifacts` appears mid-run as a symlink (not a real
        # ignored directory) instead of the folded ignored ancestor the
        # expansion logic expects. Must never be silently expanded/allowed.
        real_target = Path(os.environ["SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_SYMLINK_TARGET"])
        real_target.mkdir(parents=True, exist_ok=True)
        os.symlink(str(real_target), "artifacts", target_is_directory=True)

    if os.environ.get("SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_FILE") == "enabled":
        # Issue #1409 REQUEST_CHANGES (P1 test 5): a parent-substitution
        # attack -- `artifacts` appears mid-run as a plain file instead of
        # the folded ignored ancestor the expansion logic expects.
        Path("artifacts").write_text("not-a-directory")

    sleep_seconds = os.environ.get("SKILL_RUNTIME_TEST_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))

    # Issue #1409 REQUEST_CHANGES (P2-2): deterministic independent-process
    # barrier -- no fixed sleep. The child signals "I have started, the
    # before-snapshot race window is now open" by creating a go-file, then
    # blocks (bounded poll) until an independent writer *process* (not a
    # same-process thread) has durably created an ack-file confirming its
    # write landed on disk, before the child itself exits.
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


def argparse_module():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
'''


_STUB_MANIFEST_PRODUCER_MJS = '''#!/usr/bin/env node
// Trivial stand-in for scripts/generate-session-manifest.mjs used only to
// keep the real generate_session_manifest_from_hook.mjs lifecycle test
// deterministic and fast (Issue #1409 AC1/AC5). Emits a minimal, syntactically
// valid manifest JSON on stdout and exits 0.
process.stdout.write(JSON.stringify({
  schema: "agent_session_manifest/v1",
  repository: "squne121/loop-protocol",
  generated_at: new Date().toISOString(),
}))
process.exit(0)
'''


def _install_lifecycle_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        ".claude/hooks/session_manifest_debounce.mjs",
        ".claude/hooks/generate_session_manifest_from_hook.mjs",
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
        _RUN_REFINEMENT_PREFLIGHT_PY,
    )

    _write_text(
        repo_root / "scripts" / "agent-guards" / "tests" / "fixtures" / "stub_manifest_producer.mjs",
        _STUB_MANIFEST_PRODUCER_MJS,
    )


def _run_executor(
    repo: Path, extra_env: dict[str, str] | None = None, issue_number: str = "1409"
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "SKILL_RUNTIME_TEST_NODE_BIN": NODE_BIN or ""}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            issue_number,
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


_INDEPENDENT_WRITER_PY = '''from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    go_file = Path(sys.argv[1])
    ack_file = Path(sys.argv[2])
    target_path = Path(sys.argv[3])
    content = sys.argv[4]

    deadline = time.monotonic() + 10.0
    while not go_file.exists():
        if time.monotonic() > deadline:
            raise SystemExit("independent_writer_go_file_timeout")
        time.sleep(0.02)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content)
    os.fsync(os.open(str(target_path), os.O_RDONLY))
    ack_file.parent.mkdir(parents=True, exist_ok=True)
    ack_file.write_text("ack")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _spawn_independent_writer_process(
    repo: Path, go_file: Path, ack_file: Path, target_path: Path, content: str
) -> subprocess.Popen[str]:
    """Spawn a genuinely independent OS process (not a same-process thread)
    that blocks on `go_file` before writing `target_path` and signalling
    `ack_file`. Issue #1409 REQUEST_CHANGES (P2-2): a real child process tree
    distinct from the executor's own child is required to meaningfully
    distinguish "child command's own write" from "peer/background process
    write" -- a same-process `threading.Thread` cannot exercise that
    distinction because it always runs inside the same PID as the test
    (and, transitively, could be conflated with the child subprocess's PID
    tree in a stricter future strace-based attribution mode)."""
    script_path = repo / "scripts" / "agent-guards" / "tests" / "fixtures" / "independent_writer.py"
    _write_text(script_path, _INDEPENDENT_WRITER_PY)
    return subprocess.Popen(
        [sys.executable, str(script_path), str(go_file), str(ack_file), str(target_path), content],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _require_node() -> None:
    if NODE_BIN is None:
        import pytest

        pytest.skip("node executable not found on PATH -- required for real hook subprocess lifecycle test")


# ---------------------------------------------------------------------------
# AC1 + AC5: real debounce/generate-hook subprocess lifecycle
# ---------------------------------------------------------------------------


def test_real_debounce_process_write_to_session_manifest_runtime_does_not_fail(tmp_path: Path) -> None:
    """GIVEN the real `.claude/hooks/session_manifest_debounce.mjs` (and the
    real `.claude/hooks/generate_session_manifest_from_hook.mjs` it spawns)
    write lock/temp/final-manifest files under
    `artifacts/session-manifest-runtime/**` synchronously while
    skill_runtime_exec.py's own child subprocess is still running
    WHEN the child subprocess returns
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (post-fix behavior for Issue #1409; pre-fix, this same real-process
    sequence reproduces the reported unauthorized_write_path fail-close)."""
    _require_node()
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE": "enabled"})
    assert result.returncode == 0, result.stderr
    manifests_dir = repo / "artifacts" / "session-manifest-runtime" / "manifests"
    assert any(manifests_dir.glob("private-agent-session-manifest-posttooluse-*.json")), (
        f"expected a real manifest file to have been written; dir contents: "
        f"{list(manifests_dir.iterdir()) if manifests_dir.exists() else '<missing>'}"
    )
    events_dir = repo / "artifacts" / "session-manifest-runtime" / "events"
    assert list(events_dir.glob("*.json")) == [], "event file was not removed after flush -- AC5 violated"
    lock_path = repo / "artifacts" / "session-manifest-runtime" / "locks" / "worker.lock"
    assert not lock_path.exists(), "worker.lock still exists after flush -- AC5 violated"
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1409" / "preflight.json"
    assert artifact.exists()


def test_real_debounce_process_lock_rename_and_failed_rename_lifecycle(tmp_path: Path) -> None:
    """AC5: exercise lock creation/removal, temp-file creation, atomic rename,
    event-file deletion (success path) and the failed-rename branch
    (forced by making manifests/ read-only) using the real hook scripts, and
    verify skill_runtime_exec.py does not fail-close in either case."""
    _require_node()
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)

    # Success path: lock create/remove, tmp create, atomic rename, event delete.
    ok_result = _run_executor(
        repo, {"SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE": "enabled"}, issue_number="1409"
    )
    assert ok_result.returncode == 0, ok_result.stderr
    manifests_dir = repo / "artifacts" / "session-manifest-runtime" / "manifests"
    assert any(manifests_dir.glob("*.json"))

    # Failed-rename path: manifests/ made read-only mid-lifecycle so the real
    # atomic rename fails and the hook falls back to `${tmpPath}.failed`.
    failed_result = _run_executor(
        repo,
        {"SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE_FAILED_RENAME": "enabled"},
        issue_number="1410",
    )
    assert failed_result.returncode == 0, (
        f"failed-rename lifecycle must not fail-close the executor: {failed_result.stderr}"
    )
    tmp_dir = repo / "artifacts" / "session-manifest-runtime" / "tmp"
    assert any(tmp_dir.glob("*.failed")), (
        f"expected a *.failed debris file after forced rename failure; tmp dir contents: "
        f"{list(tmp_dir.iterdir()) if tmp_dir.exists() else '<missing>'}"
    )
    lock_path = repo / "artifacts" / "session-manifest-runtime" / "locks" / "worker.lock"
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# AC2: race-tolerant stdlib mode excludes the new subtree for peer/background writes
# ---------------------------------------------------------------------------


def test_race_tolerant_stdlib_excludes_session_manifest_runtime_subtree(tmp_path: Path) -> None:
    """GIVEN a peer/background writer (not the child command itself) creates a
    NEW file and updates an EXISTING file under
    `artifacts/session-manifest-runtime/**` while skill_runtime_exec.py's
    own child subprocess is still running
    WHEN the child subprocess completes
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    for either the new-file or the updated-existing-file case."""
    _require_node()
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)

    existing_path = repo / "artifacts" / "session-manifest-runtime" / "manifests" / "existing.json"
    existing_path.parent.mkdir(parents=True, exist_ok=True)
    existing_path.write_text('{"seed": true}')

    new_path = repo / "artifacts" / "session-manifest-runtime" / "events" / "peer-new.json"

    def _update_existing_after_delay() -> None:
        time.sleep(0.2)
        existing_path.write_text('{"seed": true, "updated": true}')

    update_thread = threading.Thread(target=_update_existing_after_delay)
    update_thread.start()
    new_thread = _write_after_delay(new_path, '{"peer": true}\n', delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        update_thread.join(timeout=5)
        new_thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert new_path.exists()
    assert json.loads(existing_path.read_text()) == {"seed": True, "updated": True}
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1409" / "preflight.json"
    assert artifact.exists()


# ---------------------------------------------------------------------------
# AC3: writes outside the new subtree still fail-close (no regression)
# ---------------------------------------------------------------------------


def test_self_write_outside_session_manifest_runtime_still_fails(tmp_path: Path) -> None:
    """GIVEN the child command itself writes to `artifacts/unrelated/**`
    (outside the new `artifacts/session-manifest-runtime/` subtree)
    WHEN the command completes
    THEN skill_runtime_exec.py must still fail-close with unauthorized_write_path
    (the Issue #1409 exclusion is narrow: repo-root `artifacts/` as a whole
    remains audited; only the hook-owned subtree is excluded)."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    result = _run_executor(
        repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_OUTSIDE_SESSION_MANIFEST_RUNTIME": "ignored"}
    )
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=artifacts/unrelated" in result.stderr


# ---------------------------------------------------------------------------
# AC4: known-limitation -- self-write INTO the new subtree is unattributable
# ---------------------------------------------------------------------------


def test_self_write_to_session_manifest_runtime_is_known_unsupported_in_stdlib_mode(
    tmp_path: Path,
) -> None:
    """GIVEN the child command itself (not the real hook) writes into
    `artifacts/session-manifest-runtime/**`
    WHEN the command completes
    THEN skill_runtime_exec.py does NOT fail with unauthorized_write_path (the
    self-write silently succeeds).

    KNOWN LIMITATION (Issue #1409, same rationale as Issue #1343's
    `.claude/worktrees` / `.claude/artifacts/issue-refinement-loop`
    precedent): the new subtree is pruned from the before/after snapshot
    diff entirely so that concurrent hook writes are never misattributed to
    the child's own subprocess. The unavoidable side effect is that a
    self-write by the child into this same subtree cannot be distinguished
    from a legitimate hook write in stdlib-only race-tolerant mode. Strict
    attribution (Issue #1363 strict_strace mode) is the documented handoff
    for closing this gap."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    result = _run_executor(
        repo, {"SKILL_RUNTIME_TEST_SELF_WRITE_SESSION_MANIFEST_RUNTIME": "ignored"}
    )
    assert result.returncode == 0, result.stderr
    self_write_path = (
        repo / "artifacts" / "session-manifest-runtime" / "manifests" / "self-write.json"
    )
    assert self_write_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1409" / "preflight.json"
    assert artifact.exists()


# ---------------------------------------------------------------------------
# Issue #1409 REQUEST_CHANGES (P1): ignored-ancestor-folding cold start
# ---------------------------------------------------------------------------
#
# Reproduces (and fixes) the reported blocker: the real repo `.gitignore`
# ignores `artifacts/` as a whole, so Git's `--ignored=matching` collapses a
# cold-start creation of `artifacts/session-manifest-runtime/**` into a
# single `!! artifacts/` status entry -- misreported as
# `unauthorized_write_path=artifacts/` before the fix in this file.


def test_cold_start_peer_writes_only_race_tolerant_subtree_succeeds(tmp_path: Path) -> None:
    """GIVEN `artifacts/` does not exist before the command runs (cold start)
    and a peer/background writer creates ONLY files under
    `artifacts/session-manifest-runtime/**`
    WHEN the child subprocess completes
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (Safety Claim: cold-start ignored-ancestor folding does not misfire)."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    assert not (repo / "artifacts").exists()

    new_path = repo / "artifacts" / "session-manifest-runtime" / "events" / "peer-cold-start.json"
    writer_thread = _write_after_delay(new_path, '{"peer": true}\n', delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        writer_thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert new_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1409" / "preflight.json"
    assert artifact.exists()


def test_cold_start_peer_writes_subtree_and_unauthorized_sibling_fails(tmp_path: Path) -> None:
    """GIVEN `artifacts/` does not exist before the command runs (cold start)
    and a peer/background writer creates files under BOTH
    `artifacts/session-manifest-runtime/**` (excluded) AND
    `artifacts/unrelated/**` (still audited)
    WHEN the child subprocess completes
    THEN skill_runtime_exec.py must fail-close with unauthorized_write_path
    pointing at the leaf path under `artifacts/unrelated/`, not the folded
    `artifacts/` ancestor (Safety Claim: expansion is precise, not a blanket
    allow of the whole collapsed directory)."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    assert not (repo / "artifacts").exists()

    subtree_path = repo / "artifacts" / "session-manifest-runtime" / "events" / "peer-cold-start.json"
    unrelated_path = repo / "artifacts" / "unrelated" / "peer-cold-start.txt"

    def _write_both() -> None:
        time.sleep(0.2)
        subtree_path.parent.mkdir(parents=True, exist_ok=True)
        subtree_path.write_text('{"peer": true}\n')
        unrelated_path.parent.mkdir(parents=True, exist_ok=True)
        unrelated_path.write_text("peer-unrelated\n")

    writer_thread = threading.Thread(target=_write_both)
    writer_thread.start()
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        writer_thread.join(timeout=5)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=artifacts/unrelated/peer-cold-start.txt" in result.stderr


def test_existing_artifacts_dir_hook_subtree_update_succeeds(tmp_path: Path) -> None:
    """GIVEN `artifacts/` already exists (pre-created, not cold start) with
    seed content under the race-tolerant subtree
    WHEN a peer/background writer updates an existing file and creates a new
    file, both under `artifacts/session-manifest-runtime/**`
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    seed_path = repo / "artifacts" / "session-manifest-runtime" / "manifests" / "seed.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text('{"seed": true}')

    new_path = repo / "artifacts" / "session-manifest-runtime" / "events" / "peer-existing.json"

    def _write_both() -> None:
        time.sleep(0.2)
        seed_path.write_text('{"seed": true, "updated": true}')
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text('{"peer": true}\n')

    writer_thread = threading.Thread(target=_write_both)
    writer_thread.start()
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        writer_thread.join(timeout=5)

    assert result.returncode == 0, result.stderr
    assert json.loads(seed_path.read_text()) == {"seed": True, "updated": True}
    assert new_path.exists()


def test_existing_artifacts_dir_unrelated_update_fails(tmp_path: Path) -> None:
    """GIVEN `artifacts/` already exists (pre-created)
    WHEN a peer/background writer creates a file under
    `artifacts/unrelated/**` (outside the race-tolerant subtree)
    THEN skill_runtime_exec.py must fail-close with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    seed_path = repo / "artifacts" / "session-manifest-runtime" / "manifests" / "seed.json"
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text('{"seed": true}')

    unrelated_path = repo / "artifacts" / "unrelated" / "peer-existing.txt"
    writer_thread = _write_after_delay(unrelated_path, "peer-unrelated\n", delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        writer_thread.join(timeout=5)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=artifacts/unrelated/peer-existing.txt" in result.stderr


def test_artifacts_as_symlink_created_mid_run_fails_closed(tmp_path: Path) -> None:
    """GIVEN `artifacts` does not exist before the command runs (cold start)
    WHEN the child subprocess itself creates `artifacts` as a symlink (not a
    real directory) mid-run instead of a real ignored directory
    THEN skill_runtime_exec.py must fail-close (a parent-substitution via
    symlink must never be silently treated as a foldable ignored directory
    and expanded/allowed)."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    assert not (repo / "artifacts").exists()
    symlink_target = tmp_path / "artifacts-symlink-target"

    result = _run_executor(
        repo,
        {
            "SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_SYMLINK": "enabled",
            "SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_SYMLINK_TARGET": str(symlink_target),
        },
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=artifacts" in result.stderr


def test_artifacts_as_file_created_mid_run_fails_closed(tmp_path: Path) -> None:
    """GIVEN `artifacts` does not exist before the command runs (cold start)
    WHEN the child subprocess itself creates `artifacts` as a plain file
    mid-run instead of a real ignored directory
    THEN skill_runtime_exec.py must fail-close (a parent-substitution via a
    plain file must never be silently treated as a foldable ignored
    directory and expanded/allowed)."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)
    assert not (repo / "artifacts").exists()

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_CREATE_ARTIFACTS_FILE": "enabled"})
    assert result.returncode == 2, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=artifacts" in result.stderr


# ---------------------------------------------------------------------------
# Issue #1409 REQUEST_CHANGES (P2-2): independent-process barrier, no fixed
# sleep -- distinguishes "child command's own write" from "wrapper-external
# peer process write" deterministically.
# ---------------------------------------------------------------------------


def test_independent_writer_process_with_explicit_barrier_succeeds(tmp_path: Path) -> None:
    """GIVEN a genuinely independent OS process (not a same-process thread,
    not a descendant of the executor's own child) writes a new file under
    the race-tolerant subtree, synchronized via an explicit go-file/ack-file
    barrier (no fixed sleep on either side)
    WHEN both processes have completed
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)

    go_file = tmp_path / "barrier-go"
    ack_file = tmp_path / "barrier-ack"
    target_path = repo / "artifacts" / "session-manifest-runtime" / "events" / "independent-peer.json"

    writer = _spawn_independent_writer_process(
        repo, go_file, ack_file, target_path, '{"independent_peer": true}'
    )
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_BARRIER_GO_FILE": str(go_file),
                "SKILL_RUNTIME_TEST_BARRIER_ACK_FILE": str(ack_file),
            },
        )
        writer_stdout, writer_stderr = writer.communicate(timeout=10)
    finally:
        if writer.poll() is None:
            writer.kill()

    assert writer.returncode == 0, writer_stderr
    assert result.returncode == 0, result.stderr
    assert target_path.exists()


# ---------------------------------------------------------------------------
# Issue #1409 REQUEST_CHANGES (P2-1): legacy runtime state coexistence
# (intentional hard cutover -- see PR body Compatibility Decision).
# ---------------------------------------------------------------------------


def test_legacy_runtime_state_present_does_not_deadlock_or_duplicate(tmp_path: Path) -> None:
    """GIVEN legacy pre-Issue-#1409 runtime state paths already exist
    (`artifacts/session-manifest-debounce/events/`, `worker.lock`,
    `artifacts/.lock-*`, `artifacts/.tmp-*`,
    `artifacts/private-agent-session-manifest-*.json`)
    WHEN the real debounce + generate hook lifecycle runs against the new
    subtree
    THEN it must complete without deadlock (bounded by the subprocess
    timeout) and must not skip the new manifest as a false duplicate (the
    duplicate scan is now scoped to the new `manifests/` subdirectory only
    -- legacy root-level files are never consulted, per the documented hard
    cutover)."""
    _require_node()
    repo = _make_repo(tmp_path)
    _install_lifecycle_fixture(repo)

    legacy_debounce_events = repo / "artifacts" / "session-manifest-debounce" / "events"
    legacy_debounce_events.mkdir(parents=True, exist_ok=True)
    (legacy_debounce_events / "stale-event.json").write_text("{}")
    (repo / "artifacts" / "session-manifest-debounce" / "worker.lock").write_text(
        json.dumps({"owner_pid": 999999, "role": "worker", "started_at_ms": 1, "heartbeat_at_ms": 1})
    )
    (repo / "artifacts" / ".lock-legacy").write_text("legacy-lock")
    (repo / "artifacts" / ".tmp-legacy").write_text("legacy-tmp")
    (repo / "artifacts" / "private-agent-session-manifest-legacy.json").write_text("{}")

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SESSION_MANIFEST_LIFECYCLE": "enabled"})
    assert result.returncode == 0, result.stderr
    manifests_dir = repo / "artifacts" / "session-manifest-runtime" / "manifests"
    assert any(manifests_dir.glob("private-agent-session-manifest-posttooluse-*.json")), (
        "expected a new-subtree manifest even with legacy state present"
    )
    # Legacy state must be left untouched (hard cutover -- no migration).
    assert (repo / "artifacts" / "session-manifest-debounce" / "worker.lock").exists()
    assert (repo / "artifacts" / ".lock-legacy").exists()
