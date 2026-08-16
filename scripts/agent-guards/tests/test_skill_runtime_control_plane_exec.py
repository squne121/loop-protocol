"""Issue #2196 (Child 1 of #2190): sanitized Git subprocess exec tests.

Covers `sanitized_git_subprocess_env`, `resolve_git_subprocess_executable`,
`git_subprocess_trusted_hooks_dir`, and `run_sanitized_git_subprocess` in
`skill_runtime_exec.py` (AC1 / AC2 / AC3 / AC4 / AC5).
"""

from __future__ import annotations

import http.server
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402
from skill_runtime_command_policy import GitSubprocessRewriteRejected  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_git_subprocess_cache():
    exec_mod._reset_git_subprocess_executable_cache_for_tests()
    yield
    exec_mod._reset_git_subprocess_executable_cache_for_tests()


def _init_fixture_repo(repo_dir: Path, home_dir: Path) -> dict[str, str]:
    """Create a minimal git repo with one commit, using a clean HOME so the
    real developer's global gitconfig never leaks into the test."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    base_env = dict(os.environ)
    base_env["HOME"] = str(home_dir)
    base_env["GIT_AUTHOR_NAME"] = "Test"
    base_env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    base_env["GIT_COMMITTER_NAME"] = "Test"
    base_env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    base_env["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True, env=base_env)
    (repo_dir / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, env=base_env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo_dir, check=True, env=base_env
    )
    return base_env


# ---------------------------------------------------------------------------
# AC1 / AC5: sanitized_git_subprocess_env
# ---------------------------------------------------------------------------


def test_sanitized_git_subprocess_env_unsets_all_ac1_keys(monkeypatch, tmp_path):
    """GIVEN every AC1 GIT_* variable is present in the ambient environment
    WHEN sanitized_git_subprocess_env is called
    THEN none of them appear in the returned mapping."""
    ac1_keys = [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_COUNT",
    ]
    for key in ac1_keys:
        monkeypatch.setenv(key, "/tmp/attacker-controlled")

    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))

    for key in ac1_keys:
        assert key not in env, f"{key} leaked into sanitized env"


def test_sanitized_git_subprocess_env_disables_prompts_and_credential_surfaces(tmp_path):
    """GIVEN no special setup
    WHEN sanitized_git_subprocess_env is called
    THEN interactive terminal prompting, askpass, and interactive SSH are
    all disabled (AC5)."""
    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_sanitized_git_subprocess_env_preserves_unrelated_keys(monkeypatch, tmp_path):
    """GIVEN an unrelated environment variable
    WHEN sanitized_git_subprocess_env is called
    THEN it is preserved (only the AC1 named set is stripped)."""
    monkeypatch.setenv("SKILL_RUNTIME_TEST_MARKER", "keep-me")
    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))
    assert env.get("SKILL_RUNTIME_TEST_MARKER") == "keep-me"


# ---------------------------------------------------------------------------
# AC2: resolve_git_subprocess_executable caching
# ---------------------------------------------------------------------------


def test_resolve_git_subprocess_executable_returns_absolute_git_path(tmp_path):
    """GIVEN a clean cache
    WHEN resolve_git_subprocess_executable is called
    THEN it returns an absolute path to a real, executable `git` binary."""
    resolved = exec_mod.resolve_git_subprocess_executable(str(tmp_path))
    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "git"
    assert os.access(resolved, os.X_OK)


def test_resolve_git_subprocess_executable_is_cached_and_reused(monkeypatch, tmp_path):
    """GIVEN resolve_git_subprocess_executable was already called once
    WHEN it is called again after the underlying resolution mechanism
    would return a *different* value
    THEN the original cached absolute path string is returned unchanged
    (AC2: resolved once, reused for every subsequent invocation)."""
    first = exec_mod.resolve_git_subprocess_executable(str(tmp_path))

    call_count = {"n": 0}
    real_resolve = exec_mod._resolve_trusted_executable

    def _spy(name, project_root):
        call_count["n"] += 1
        return real_resolve(name, project_root)

    monkeypatch.setattr(exec_mod, "_resolve_trusted_executable", _spy)

    second = exec_mod.resolve_git_subprocess_executable(str(tmp_path))
    third = exec_mod.resolve_git_subprocess_executable(str(tmp_path))

    assert second == first
    assert third == first
    # The cache hit path must never re-invoke the underlying resolver.
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# AC3: git_subprocess_trusted_hooks_dir
# ---------------------------------------------------------------------------


def test_git_subprocess_trusted_hooks_dir_creates_empty_dir(tmp_path):
    hooks_dir = exec_mod.git_subprocess_trusted_hooks_dir(str(tmp_path))
    assert Path(hooks_dir).is_dir()
    assert list(Path(hooks_dir).iterdir()) == []


def test_git_subprocess_trusted_hooks_dir_fails_closed_if_not_empty(tmp_path):
    """GIVEN the trusted hooks directory unexpectedly contains a file
    WHEN git_subprocess_trusted_hooks_dir is called
    THEN it raises RuntimeError instead of silently proceeding."""
    hooks_dir = Path(tmp_path) / ".skill-runtime-git-empty-hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "post-checkout").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        exec_mod.git_subprocess_trusted_hooks_dir(str(tmp_path))


# ---------------------------------------------------------------------------
# AC3 (behavioral): run_sanitized_git_subprocess prevents post-checkout firing
# ---------------------------------------------------------------------------


def test_run_sanitized_git_subprocess_prevents_post_checkout_hook_firing(tmp_path):
    """GIVEN a fixture repo whose default .git/hooks/post-checkout writes a
    marker file
    WHEN a checkout-equivalent operation is run through
    run_sanitized_git_subprocess
    THEN the marker file is NOT created (core.hooksPath pinned to a
    verified-empty trusted directory suppresses hook firing) -- and a
    baseline direct `git checkout` (without sanitization) DOES create the
    marker, proving the hook is real and would otherwise fire."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    base_env = _init_fixture_repo(repo_dir, home_dir)

    marker = repo_dir / "post-checkout-fired.marker"
    hooks_dir = repo_dir / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-checkout"
    hook_path.write_text(
        f'#!/bin/sh\ntouch "{marker}"\n',
        encoding="utf-8",
    )
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Baseline: a raw, unsanitized checkout DOES fire the hook.
    baseline = subprocess.run(
        ["git", "checkout", "-q", "-b", "baseline-branch"],
        cwd=repo_dir,
        env=base_env,
    )
    assert baseline.returncode == 0
    assert marker.exists(), "baseline hook did not fire; fixture is broken"
    marker.unlink()

    # Sanitized: run_sanitized_git_subprocess must NOT let the hook fire.
    result = exec_mod.run_sanitized_git_subprocess(
        ["checkout", "-q", "-b", "sanitized-branch"],
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "post-checkout hook fired despite core.hooksPath sanitization"


# ---------------------------------------------------------------------------
# AC4 (behavioral): run_sanitized_git_subprocess rejects insteadOf rewrites
# ---------------------------------------------------------------------------


def test_run_sanitized_git_subprocess_rejects_insteadof_rewrite(tmp_path):
    """GIVEN a fixture repo with a local `url.<base>.insteadOf` rewrite
    configured
    WHEN run_sanitized_git_subprocess is called for that repo
    THEN it raises GitSubprocessRewriteRejected before running the real
    command."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    base_env = _init_fixture_repo(repo_dir, home_dir)
    subprocess.run(
        [
            "git",
            "config",
            "--local",
            "url.https://rewritten.example/.insteadOf",
            "https://example.com/",
        ],
        cwd=repo_dir,
        check=True,
        env=base_env,
    )

    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod.run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_run_sanitized_git_subprocess_allows_repo_without_insteadof(tmp_path):
    """GIVEN a fixture repo with no insteadOf rewrite configured
    WHEN run_sanitized_git_subprocess is called
    THEN the real command runs and succeeds (no false-positive rejection)."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)

    result = exec_mod.run_sanitized_git_subprocess(
        ["status", "--short"],
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_run_sanitized_git_subprocess_rejects_leading_git_token():
    with pytest.raises(ValueError):
        exec_mod.run_sanitized_git_subprocess(
            ["git", "status"],
            cwd=".",
            project_root=".",
        )


# ---------------------------------------------------------------------------
# AC5 (behavioral): disabled prompts/credential surfaces do not hang
# ---------------------------------------------------------------------------


class _AuthRequiredHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API name)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        self.end_headers()

    def log_message(self, *_args):  # silence default request logging
        return


def test_run_sanitized_git_subprocess_does_not_hang_on_credential_prompt(tmp_path):
    """GIVEN a local HTTP server that always demands Basic auth (401 on
    every request)
    WHEN run_sanitized_git_subprocess attempts a fetch-equivalent operation
    against it
    THEN the subprocess exits promptly (bounded by a short timeout, never
    hangs waiting for interactive credential input) with a non-zero
    return code (AC5)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthRequiredHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        scratch_dir = tmp_path / "scratch"
        _init_fixture_repo(repo_dir, home_dir)
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/fixture-repo.git"

        result = exec_mod.run_sanitized_git_subprocess(
            ["ls-remote", url],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=10,
        )
        assert result.returncode != 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
