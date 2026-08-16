"""Issue #2196 (Child 1 of #2190): sanitized Git subprocess exec tests.

Covers `sanitized_git_subprocess_env`, `resolve_git_subprocess_executable`,
`git_subprocess_trusted_hooks_dir`, the private `_run_sanitized_git_subprocess`
engine, and the closed `run_control_plane_git_*` command builders in
`skill_runtime_exec.py` (AC1-AC11). Reflects the PR #2201 owner adversarial
review fix delta (P1-1 through P1-5, P2-1 through P2-5): raw-argv access is
private/internal only, `PATH`/`GIT_SSH_COMMAND` are trusted-allowlist-only,
`core.hooksPath` is a fresh per-invocation trusted directory, the
`insteadOf`/`pushInsteadOf` probe uses a structured NUL-delimited query
bound to identical config authority as the real command, probe failure is
fail-closed, and credential helper/askpass/ssh marker executables are
proven never invoked (not merely "does not hang").
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
from skill_runtime_command_policy import (  # noqa: E402
    GitSubprocessConfigProbeFailed,
    GitSubprocessRewriteRejected,
)


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
    base_env["XDG_CONFIG_HOME"] = str(home_dir / ".config")
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


def _write_marker_script(path: Path, marker_file: Path) -> None:
    path.write_text(f'#!/bin/sh\ntouch "{marker_file}"\nexit 1\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# AC1 / AC5 / AC7: sanitized_git_subprocess_env
# ---------------------------------------------------------------------------


def test_sanitized_git_subprocess_env_unsets_all_ac1_keys(monkeypatch, tmp_path):
    """GIVEN every AC1 GIT_* variable (post owner-review 14-variable set)
    is present in the ambient environment
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
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_EXEC_PATH",
        "GIT_CEILING_DIRECTORIES",
        "GIT_SSL_NO_VERIFY",
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


def test_sanitized_git_subprocess_env_path_is_trusted_allowlist_not_inherited(monkeypatch, tmp_path):
    """GIVEN an ambient PATH with a malicious directory prepended
    WHEN sanitized_git_subprocess_env is called
    THEN the returned PATH is exactly the trusted allowlist -- the
    malicious directory never survives (AC7 / P1-2)."""
    malicious_dir = tmp_path / "malicious-bin"
    malicious_dir.mkdir()
    monkeypatch.setenv("PATH", f"{malicious_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))

    assert str(malicious_dir) not in env["PATH"].split(os.pathsep)
    assert env["PATH"] == os.pathsep.join(exec_mod._safe_path_entries())


def test_sanitized_git_subprocess_env_ssh_command_is_absolute_trusted_path(tmp_path):
    """GIVEN no special setup
    WHEN sanitized_git_subprocess_env is called
    THEN GIT_SSH_COMMAND's ssh binary is either an absolute path resolved
    from the trusted allowlist, or the "always fail" fallback -- never an
    unqualified `ssh` token that would resolve via inherited PATH
    (AC7 / P1-2)."""
    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))
    ssh_bin = env["GIT_SSH_COMMAND"].split()[0]
    if ssh_bin == "false":
        return
    assert os.path.isabs(ssh_bin)
    assert os.path.dirname(ssh_bin) in {
        os.path.realpath(entry) for entry in exec_mod._safe_path_entries()
    } | set(exec_mod._safe_path_entries())


def test_sanitized_git_subprocess_env_ssh_command_ignores_poisoned_path(monkeypatch, tmp_path):
    """GIVEN a poisoned PATH containing a fake `ssh` executable
    WHEN sanitized_git_subprocess_env is called
    THEN GIT_SSH_COMMAND never resolves to the poisoned executable
    (AC7 / P1-2)."""
    malicious_dir = tmp_path / "malicious-bin"
    malicious_dir.mkdir()
    fake_ssh = malicious_dir / "ssh"
    fake_ssh.write_text("#!/bin/sh\necho fake\nexit 0\n", encoding="utf-8")
    fake_ssh.chmod(fake_ssh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{malicious_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    env = exec_mod.sanitized_git_subprocess_env(str(tmp_path))
    ssh_bin = env["GIT_SSH_COMMAND"].split()[0]
    assert ssh_bin != str(fake_ssh)


# ---------------------------------------------------------------------------
# AC2: resolve_git_subprocess_executable caching
# ---------------------------------------------------------------------------


def test_resolve_git_subprocess_executable_returns_absolute_git_path(tmp_path):
    resolved = exec_mod.resolve_git_subprocess_executable(str(tmp_path))
    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "git"
    assert os.access(resolved, os.X_OK)


def test_resolve_git_subprocess_executable_is_cached_and_reused(monkeypatch, tmp_path):
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
    assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# AC3: git_subprocess_trusted_hooks_dir
# ---------------------------------------------------------------------------


def test_git_subprocess_trusted_hooks_dir_creates_empty_dir(tmp_path):
    hooks_dir = exec_mod.git_subprocess_trusted_hooks_dir(str(tmp_path))
    assert Path(hooks_dir).is_dir()
    assert list(Path(hooks_dir).iterdir()) == []


def test_git_subprocess_trusted_hooks_dir_is_per_invocation(tmp_path):
    """GIVEN repeated calls with the same scratch_root
    WHEN git_subprocess_trusted_hooks_dir is called each time
    THEN a distinct directory is created every time (never a fixed,
    reused default -- P2-1)."""
    first = exec_mod.git_subprocess_trusted_hooks_dir(str(tmp_path))
    second = exec_mod.git_subprocess_trusted_hooks_dir(str(tmp_path))
    assert first != second
    assert Path(first).is_dir()
    assert Path(second).is_dir()


def test_git_subprocess_trusted_hooks_dir_rejects_relative_scratch_root():
    with pytest.raises(RuntimeError):
        exec_mod.git_subprocess_trusted_hooks_dir("relative/scratch")


def test_git_subprocess_trusted_hooks_dir_rejects_symlinked_scratch_root(tmp_path):
    real_root = tmp_path / "real-scratch"
    real_root.mkdir()
    symlink_root = tmp_path / "symlinked-scratch"
    symlink_root.symlink_to(real_root)

    with pytest.raises(RuntimeError):
        exec_mod.git_subprocess_trusted_hooks_dir(str(symlink_root))


def test_git_subprocess_trusted_hooks_dir_fails_closed_if_not_empty(monkeypatch, tmp_path):
    """GIVEN the freshly-minted trusted hooks directory unexpectedly
    contains a file (simulated by faking tempfile.mkdtemp to hand back a
    pre-populated directory)
    WHEN git_subprocess_trusted_hooks_dir is called
    THEN it raises RuntimeError instead of silently proceeding."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    poisoned = scratch / "poisoned-hooks-dir"
    poisoned.mkdir()
    (poisoned / "post-checkout").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def _fake_mkdtemp(prefix=None, dir=None):
        return str(poisoned)

    monkeypatch.setattr(exec_mod.tempfile, "mkdtemp", _fake_mkdtemp)

    with pytest.raises(RuntimeError):
        exec_mod.git_subprocess_trusted_hooks_dir(str(scratch))


def test_git_subprocess_trusted_hooks_dir_fails_closed_if_realpath_differs(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    real_realpath = exec_mod.os.path.realpath

    def _tampered_realpath(path):
        result = real_realpath(path)
        if str(scratch) in str(path) and ".skill-runtime-git-hooks-" in str(path):
            return result + "-tampered"
        return result

    monkeypatch.setattr(exec_mod.os.path, "realpath", _tampered_realpath)

    with pytest.raises(RuntimeError):
        exec_mod.git_subprocess_trusted_hooks_dir(str(scratch))


# ---------------------------------------------------------------------------
# AC3 (behavioral): _run_sanitized_git_subprocess prevents post-checkout firing
# ---------------------------------------------------------------------------


def test_run_sanitized_git_subprocess_prevents_post_checkout_hook_firing(tmp_path):
    """GIVEN a fixture repo whose default .git/hooks/post-checkout writes a
    marker file
    WHEN a checkout-equivalent operation is run through
    _run_sanitized_git_subprocess
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

    baseline = subprocess.run(
        ["git", "checkout", "-q", "-b", "baseline-branch"],
        cwd=repo_dir,
        env=base_env,
    )
    assert baseline.returncode == 0
    assert marker.exists(), "baseline hook did not fire; fixture is broken"
    marker.unlink()

    result = exec_mod._run_sanitized_git_subprocess(
        ["checkout", "-q", "-b", "sanitized-branch"],
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "post-checkout hook fired despite core.hooksPath sanitization"


# ---------------------------------------------------------------------------
# AC4 / AC6 (behavioral): insteadOf/pushInsteadOf rejection + GIT_CONFIG split
# ---------------------------------------------------------------------------


def test_run_sanitized_git_subprocess_rejects_insteadof_rewrite(tmp_path):
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
        exec_mod._run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_run_sanitized_git_subprocess_rejects_pushinsteadof_rewrite(tmp_path):
    """AC4: the pre-owner-review code only checked `insteadOf`, never
    `pushInsteadOf` -- P1-4 requires both."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    base_env = _init_fixture_repo(repo_dir, home_dir)
    subprocess.run(
        [
            "git",
            "config",
            "--local",
            "url.https://rewritten.example/.pushInsteadOf",
            "https://example.com/",
        ],
        cwd=repo_dir,
        check=True,
        env=base_env,
    )

    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod._run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_run_sanitized_git_subprocess_rejects_insteadof_with_equals_in_subsection(tmp_path):
    """AC4 / P1-4: a legal subsection name containing '=' must still be
    detected -- this is only possible because detection is now structured
    (name-only query), never a hand split of `key=value` line text."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    base_env = _init_fixture_repo(repo_dir, home_dir)
    subprocess.run(
        [
            "git",
            "config",
            "--local",
            "url.https://evil.example/?x=y.insteadOf",
            "https://good.example/",
        ],
        cwd=repo_dir,
        check=True,
        env=base_env,
    )

    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod._run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_run_sanitized_git_subprocess_allows_repo_without_insteadof(tmp_path):
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)

    result = exec_mod._run_sanitized_git_subprocess(
        ["status", "--short"],
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_run_sanitized_git_subprocess_rejects_insteadof_despite_ambient_git_config_split(
    tmp_path, monkeypatch
):
    """AC6 / P1-1: even if the caller's ambient environment sets GIT_CONFIG
    to point at an empty config file, the probe must not be split from the
    real command's config authority -- GIT_CONFIG is unset for both, so a
    repo-local insteadOf rule is still detected and rejected."""
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

    empty_config = tmp_path / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG", str(empty_config))

    with pytest.raises(GitSubprocessRewriteRejected):
        exec_mod._run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_run_sanitized_git_subprocess_rejects_leading_git_token():
    with pytest.raises(ValueError):
        exec_mod._run_sanitized_git_subprocess(
            ["git", "status"],
            cwd=".",
            project_root=".",
            timeout=10,
        )


# ---------------------------------------------------------------------------
# AC9 (behavioral): config probe failure is fail-closed, never "no rewrite"
# ---------------------------------------------------------------------------


def test_run_sanitized_git_subprocess_fails_closed_when_probe_cannot_be_evaluated(tmp_path):
    """AC9 / P1-5: a corrupted repo-local config makes the insteadOf probe
    itself fail (nonzero exit with stderr) -- this must raise
    GitSubprocessConfigProbeFailed and stop before the real command runs,
    never be silently treated as "no rewrite exists"."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)
    config_path = repo_dir / ".git" / "config"
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write("this is not valid git config syntax [[[\n")

    with pytest.raises(GitSubprocessConfigProbeFailed):
        exec_mod._run_sanitized_git_subprocess(
            ["status", "--short"],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=30,
        )


def test_probe_insteadof_rewrite_keys_fails_closed_on_timeout(tmp_path, monkeypatch):
    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=1)

    monkeypatch.setattr(exec_mod.subprocess, "run", _raise_timeout)

    with pytest.raises(GitSubprocessConfigProbeFailed):
        exec_mod._probe_insteadof_rewrite_keys(
            "/usr/bin/git",
            cwd=str(tmp_path),
            env={},
            hooks_dir=str(tmp_path),
            timeout=1,
        )


# ---------------------------------------------------------------------------
# AC5 (behavioral): disabled prompts/credential surfaces never invoked
# ---------------------------------------------------------------------------


class _AuthRequiredHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib API name)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        self.end_headers()

    def log_message(self, *_args):  # silence default request logging
        return


def test_run_sanitized_git_subprocess_does_not_hang_on_credential_prompt(tmp_path):
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

        result = exec_mod._run_sanitized_git_subprocess(
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


def test_run_sanitized_git_subprocess_never_invokes_credential_or_askpass_markers(
    tmp_path, monkeypatch
):
    """P2-3: directly prove credential helper and askpass are never
    invoked, using marker executables, instead of only proving "does not
    hang" against a 401 endpoint."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthRequiredHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo_dir = tmp_path / "repo"
        home_dir = tmp_path / "home"
        scratch_dir = tmp_path / "scratch"
        base_env = _init_fixture_repo(repo_dir, home_dir)

        cred_marker = tmp_path / "credential-helper.marker"
        askpass_marker = tmp_path / "askpass.marker"
        cred_script = tmp_path / "marker-credential-helper.sh"
        askpass_script = tmp_path / "marker-askpass.sh"
        _write_marker_script(cred_script, cred_marker)
        _write_marker_script(askpass_script, askpass_marker)

        subprocess.run(
            ["git", "config", "--global", "credential.helper", str(cred_script)],
            cwd=repo_dir,
            check=True,
            env=base_env,
        )

        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/fixture-repo.git"

        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("GIT_ASKPASS", str(askpass_script))
        monkeypatch.setenv("SSH_ASKPASS", str(askpass_script))

        result = exec_mod._run_sanitized_git_subprocess(
            ["ls-remote", url],
            cwd=str(repo_dir),
            project_root=str(repo_dir),
            scratch_root=str(scratch_dir),
            timeout=10,
        )

        assert result.returncode != 0
        assert not cred_marker.exists(), "credential helper marker fired"
        assert not askpass_marker.exists(), "askpass marker fired"
    finally:
        server.shutdown()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# AC7 (behavioral): poisoned PATH/GIT_EXEC_PATH cannot substitute helpers
# ---------------------------------------------------------------------------


def test_run_control_plane_git_ls_remote_ignores_poisoned_path_and_git_exec_path(
    monkeypatch, tmp_path
):
    """AC7 / P1-2: a poisoned PATH + GIT_EXEC_PATH containing a fake
    `git-remote-https` marker executable must never be invoked -- the
    trusted PATH allowlist and GIT_EXEC_PATH removal together close this
    substitution."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)

    malicious_dir = tmp_path / "malicious-bin"
    malicious_dir.mkdir()
    marker = tmp_path / "fake-git-remote-https.marker"
    fake_helper = malicious_dir / "git-remote-https"
    fake_helper.write_text(
        f'#!/bin/sh\ntouch "{marker}"\necho "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef refs/heads/main"\nexit 0\n',
        encoding="utf-8",
    )
    fake_helper.chmod(fake_helper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", f"{malicious_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GIT_EXEC_PATH", str(malicious_dir))

    result = exec_mod.run_control_plane_git_ls_remote(
        "https://also-unresolvable.invalid.example/repo.git",
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=10,
    )

    assert not marker.exists(), "poisoned PATH/GIT_EXEC_PATH git-remote-https helper was invoked"
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# AC8 / P1-3: closed command builder API rejects global options; no raw API
# ---------------------------------------------------------------------------


def test_no_public_raw_argv_production_api_exists():
    assert not hasattr(exec_mod, "run_sanitized_git_subprocess")
    assert hasattr(exec_mod, "_run_sanitized_git_subprocess")
    for builder_name in (
        "run_control_plane_git_ls_remote",
        "run_control_plane_git_fetch",
        "run_control_plane_git_cat_file",
        "run_control_plane_git_update_ref",
        "run_control_plane_git_worktree",
    ):
        assert hasattr(exec_mod, builder_name)


@pytest.mark.parametrize(
    "bad_value",
    [
        "-c",
        "--config-env=core.hooksPath=/tmp/evil",
        "-C",
        "--git-dir=/etc",
        "--work-tree=/",
        "--exec-path=/tmp/evil-helpers",
        "--namespace=evil",
    ],
)
def test_run_control_plane_git_ls_remote_rejects_global_option_like_repo_value(bad_value, tmp_path):
    with pytest.raises(ValueError):
        exec_mod.run_control_plane_git_ls_remote(
            bad_value,
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            timeout=10,
        )


def test_internal_engine_rejects_global_option_argv_defense_in_depth():
    with pytest.raises(ValueError):
        exec_mod._run_sanitized_git_subprocess(
            ["-c", "core.hooksPath=/tmp/evil", "status"],
            cwd=".",
            project_root=".",
            timeout=10,
        )


def test_run_control_plane_git_cat_file_rejects_disallowed_mode(tmp_path):
    with pytest.raises(ValueError):
        exec_mod.run_control_plane_git_cat_file(
            "deadbeef",
            mode="--batch",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            timeout=10,
        )


def test_run_control_plane_git_worktree_rejects_disallowed_action(tmp_path):
    with pytest.raises(ValueError):
        exec_mod.run_control_plane_git_worktree(
            "lock",
            cwd=str(tmp_path),
            project_root=str(tmp_path),
            timeout=10,
        )


def test_run_control_plane_git_worktree_list_succeeds(tmp_path):
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)
    result = exec_mod.run_control_plane_git_worktree(
        "list",
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# AC10 / AC11: bounded timeout required, stdin=DEVNULL, --no-replace-objects
# ---------------------------------------------------------------------------


def test_run_control_plane_git_worktree_requires_timeout_kwarg(tmp_path):
    with pytest.raises(TypeError):
        exec_mod.run_control_plane_git_worktree(
            "list", cwd=str(tmp_path), project_root=str(tmp_path)
        )  # missing required timeout


def test_internal_engine_rejects_none_timeout():
    with pytest.raises(ValueError):
        exec_mod._run_sanitized_git_subprocess(
            ["status"], cwd=".", project_root=".", timeout=None
        )


def test_internal_engine_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        exec_mod._run_sanitized_git_subprocess(
            ["status"], cwd=".", project_root=".", timeout=0
        )


def test_run_control_plane_git_worktree_applies_no_replace_objects_and_devnull_stdin(
    monkeypatch, tmp_path
):
    """AC10 / AC11: every subprocess.run call the engine makes (both the
    insteadOf probe and the real command) fixes `--no-replace-objects` and
    defaults `stdin=subprocess.DEVNULL`, with the caller-supplied bounded
    timeout forwarded unchanged."""
    repo_dir = tmp_path / "repo"
    home_dir = tmp_path / "home"
    scratch_dir = tmp_path / "scratch"
    _init_fixture_repo(repo_dir, home_dir)

    calls = []
    real_run = subprocess.run

    def _spy(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(exec_mod.subprocess, "run", _spy)

    result = exec_mod.run_control_plane_git_worktree(
        "list",
        cwd=str(repo_dir),
        project_root=str(repo_dir),
        scratch_root=str(scratch_dir),
        timeout=12,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2, "expected exactly the probe call and the real command call"
    for argv, kwargs in calls:
        assert "--no-replace-objects" in argv
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("timeout") == 12
