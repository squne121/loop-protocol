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
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.pytest_cache/\n")
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


def _write_after_delay(path: Path, content: str, delay_seconds: float) -> threading.Thread:
    def _worker() -> None:
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def _append_after_delay(path: Path, content: str, delay_seconds: float) -> threading.Thread:
    def _worker() -> None:
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
    peer_path = repo / ".pytest_cache" / "v" / "cache" / "nodeids"
    thread = _write_after_delay(peer_path, json.dumps(["test_x.py::test_a"]), delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
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
    peer_path = repo / ".pytest_cache" / "v" / "cache" / "lastfailed"
    thread = _write_after_delay(peer_path, json.dumps({"test_x.py::test_a": True}), delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_peer_pytest_custom_cache_key_write_is_permitted(tmp_path: Path) -> None:
    """GIVEN a third-party pytest plugin writes a custom `config.cache` key
    under `.pytest_cache/v/cache/<custom-key>` (Issue #1526: `cache.set()` is
    a public API any plugin can call, not limited to nodeids/lastfailed)
    WHEN this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    peer_path = repo / ".pytest_cache" / "v" / "cache" / "my_custom_plugin" / "state.json"
    thread = _write_after_delay(peer_path, json.dumps({"custom": True}), delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert peer_path.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
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
    unaffected by the `.pytest_cache` reframe)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    tracked_path = repo / "README.md"
    thread = _append_after_delay(tracked_path, "tampered\n", delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "target_issue=1526" in result.stderr


def test_exemption_is_repo_root_specific_not_nested_or_prefix_lookalike(tmp_path: Path) -> None:
    """GIVEN a peer write targets either a nested `foo/.pytest_cache/**`
    directory or a prefix-lookalike repo-root `.pytest_cache2/**` directory
    WHEN neither is the exact repo-root `.pytest_cache` exemption
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path for both (AC6)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    nested_peer = repo / "foo" / ".pytest_cache" / "v" / "cache" / "nodeids"
    thread = _write_after_delay(nested_peer, "[]", delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 2, result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=foo/" in result.stderr

    (tmp_path / "repo2_root").mkdir(parents=True)
    repo2 = _make_repo(tmp_path / "repo2_root")
    _install_skill_runtime_exec_fixture(repo2)
    lookalike_peer = repo2 / ".pytest_cache2" / "v" / "cache" / "nodeids"
    thread2 = _write_after_delay(lookalike_peer, "[]", delay_seconds=0.2)
    try:
        result2 = _run_executor(repo2, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread2.join(timeout=5)
    assert result2.returncode == 2, result2.stderr
    assert "reason_code=unauthorized_write_path" in result2.stderr
    assert "unauthorized write path=.pytest_cache2" in result2.stderr


def _wait_for_file(path: Path, timeout: float) -> bool:
    """Poll (bounded, not a fixed sleep) for `path` to appear -- a simple
    file-based barrier/handshake so the caller can deterministically know a
    sibling process has actually started, instead of guessing a delay."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


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
    successfully (its exit code is asserted, not ignored)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    started_sentinel = repo / "peer_pytest_started.sentinel"
    (repo / "test_peer_pytest_cache.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "def test_ok():\n"
        f"    Path({str(started_sentinel)!r}).write_text('started')\n"
        "    time.sleep(0.3)\n"
        "    assert True\n"
    )
    peer_proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "test_peer_pytest_cache.py"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _wait_for_file(started_sentinel, timeout=10), "peer pytest never started"
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        peer_stdout, _ = peer_proc.communicate(timeout=10)
        (repo / "test_peer_pytest_cache.py").unlink(missing_ok=True)
    assert peer_proc.returncode == 0, peer_stdout
    assert result.returncode == 0, result.stderr
    real_cache_dir = repo / ".pytest_cache"
    assert real_cache_dir.exists()
    assert (real_cache_dir / "v" / "cache" / "nodeids").exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
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
    temp_dir = repo / "pytest-cache-files-abc123de"
    cachedir_tag_content = "Signature: 8a477f597d28d172789f06886806bc55\n"
    thread = _write_after_delay(temp_dir / "CACHEDIR.TAG", cachedir_tag_content, delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert temp_dir.exists()
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1526" / "preflight.json"
    assert artifact.exists()


def test_pytest_cache_cold_start_temp_dir_nested_is_rejected(tmp_path: Path) -> None:
    """PR #2364 review P1-1 scope check: GIVEN a `pytest-cache-files-*`
    -prefixed path appears nested under a subdirectory rather than directly
    at repo root
    WHEN pytest's own `dir=self._cachedir.parent` always resolves to the
    pytest rootdir (repo root for this repository), so a nested occurrence
    can never be pytest's genuine cold-start temp dir
    THEN skill_runtime_exec.py must still fail-close with
    unauthorized_write_path (the exemption is repo-root-top-level-only)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    nested_temp_dir = repo / "sub" / "pytest-cache-files-abc123de" / "CACHEDIR.TAG"
    thread = _write_after_delay(nested_temp_dir, "Signature: 8a477f597d28d172789f06886806bc55\n", delay_seconds=0.2)
    try:
        result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6"})
    finally:
        thread.join(timeout=5)
    assert result.returncode == 2, result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=sub/" in result.stderr
