"""Issue #2241: trusted toolchain resolution must not depend on the
isolated `HOME` a Claude-GPT launcher sets for a child session.

Covers AC1 (isolated `HOME` `uv` resolution succeeds) and AC6-adjacent
regression coverage (existing fail-closed adversarial trust-path checks
still block once the trust root is generalized to
`_trusted_toolchain_dirs`).

Issue #2251 narrows the trust root itself: account-home `~/.local/bin` is
excluded from `_safe_path_entries()` entirely (CWE-427 search-selection
hardening) rather than merely validated. AC1/AC2/AC3/AC5/AC7 coverage for
that change lives in this module (see the "Issue #2251" section below);
AC4 is this whole module's positive-path regression suite, and AC6 is
`test_skill_runtime_control_plane_exec.py`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as exec_mod  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_fake_uv(hosted_root: Path, version: str, *, mode: int | None = None) -> Path:
    version_dir = hosted_root / version / "x86_64"
    version_dir.mkdir(parents=True, exist_ok=True)
    uv_path = version_dir / "uv"
    uv_path.write_text("#!/bin/sh\necho fake-uv\n", encoding="utf-8")
    default_mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    uv_path.chmod(mode if mode is not None else default_mode)
    return uv_path


def test_resolve_trusted_executable_uv_succeeds_under_isolated_home(monkeypatch, tmp_path):
    """GIVEN an isolated HOME (Claude-GPT launcher style: fresh, empty,
    no `.local/bin` toolchain cache) with no `HOME` override applied by the
    caller, and a deterministic hostedtoolcache `uv` (Issue #2251: real
    host state -- whether `uv` happens to live in hostedtoolcache, a
    system directory, or only account-home `.local/bin` -- must not decide
    this test's outcome; account-home `.local/bin` is no longer a trust
    root candidate at all, see `test_safe_path_entries_excludes_local_bin`)
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it succeeds by resolving `uv` from the hosted toolcache root, not
    from the isolated `HOME`'s now-nonexistent, and now categorically
    untrusted, `.local/bin` (Issue #2241 AC1 / Issue #2251 AC4)."""
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    hosted_uv_path = _write_fake_uv(hosted_root, "0.11.29")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "uv"
    assert os.access(resolved, os.X_OK)
    assert os.path.realpath(resolved) == os.path.realpath(str(hosted_uv_path))
    # The isolated HOME's .local/bin must never be where this actually
    # resolved from -- it is empty by construction, and excluded outright.
    assert not resolved.startswith(str(isolated_home))


def test_safe_path_entries_excludes_local_bin(monkeypatch, tmp_path):
    """GIVEN an existing account-home `.local/bin` directory (real or
    isolated-session-style, via `HOME`)
    WHEN `_safe_path_entries` is called
    THEN no entry is, or is derived from, an account-home `.local/bin`
    directory -- account-home `.local/bin` is no longer a trust root
    candidate at all (Issue #2251 AC1: CWE-427 search-selection hardening
    excludes the location categorically; it used to be merely validated,
    see the superseded `test_safe_path_entries_local_bin_uses_os_account_home_not_isolated_home`
    from Issue #2241)."""
    isolated_home = tmp_path / "isolated-home"
    (isolated_home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))

    entries = exec_mod._safe_path_entries()

    assert not any(entry.endswith(str(Path(".local") / "bin")) for entry in entries)
    assert not any(str(isolated_home) in entry for entry in entries)


def test_trusted_toolchain_dirs_generalizes_beyond_uv(tmp_path, monkeypatch):
    """GIVEN `_trusted_toolchain_dirs` is looked up by executable name
    WHEN called with an executable name that has no registered hosted-tool
    root
    THEN it returns an empty list rather than raising (generic lookup
    contract, Issue #2241)."""
    assert exec_mod._trusted_toolchain_dirs("some-unregistered-tool") == []


def test_trusted_toolchain_dirs_rejects_non_version_shaped_directory(tmp_path, monkeypatch):
    """GIVEN a hostedtoolcache-shaped directory whose version component is
    not version-shaped (e.g. tampered/injected)
    WHEN `_trusted_toolchain_dirs("uv")` is called
    THEN that candidate is rejected (Issue #2241 "version 照合")."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    _write_fake_uv(hosted_root, "not-a-version")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    assert exec_mod._trusted_toolchain_dirs("uv") == []


def test_trusted_toolchain_dirs_accepts_version_shaped_directory(tmp_path, monkeypatch):
    """GIVEN a well-formed hostedtoolcache directory
    WHEN `_trusted_toolchain_dirs("uv")` is called
    THEN the candidate directory is accepted (Issue #2241)."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    _write_fake_uv(hosted_root, "0.4.30")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    dirs = exec_mod._trusted_toolchain_dirs("uv")

    assert len(dirs) == 1
    assert dirs[0].endswith(str(Path("0.4.30") / "x86_64"))


def test_trusted_toolchain_dirs_rejects_symlink_escaping_trust_root(tmp_path, monkeypatch):
    """GIVEN a version directory whose `uv` entry is a symlink pointing
    outside the hostedtoolcache trust root
    WHEN `_trusted_toolchain_dirs("uv")` is called
    THEN that candidate is rejected (commonpath containment check,
    Issue #2241 AC6-style regression)."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    version_dir = hosted_root / "0.4.30" / "x86_64"
    version_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside" / "uv"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text("#!/bin/sh\necho evil\n", encoding="utf-8")
    outside_target.chmod(stat.S_IRWXU)
    (version_dir / "uv").symlink_to(outside_target)
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    assert exec_mod._trusted_toolchain_dirs("uv") == []


@pytest.mark.skipif(os.getuid() == 0, reason="ownership check is only meaningful as non-root")
def test_trusted_toolchain_dirs_rejects_non_root_non_account_ownership(tmp_path, monkeypatch):
    """GIVEN a candidate executable that is neither owned by root(0) nor by
    the account this process runs as
    WHEN `_trusted_toolchain_dirs("uv")` is called
    THEN that candidate is rejected -- this test documents the ownership
    check exists, using a synthetic owner mismatch via monkeypatched
    `os.stat` since tests cannot chown arbitrary files without privileges
    (Issue #2241 ownership(uid) verification)."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    uv_path = _write_fake_uv(hosted_root, "0.4.30")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    real_stat = os.stat

    class _FakeStat:
        def __init__(self, real_result):
            self._real = real_result

        def __getattr__(self, item):
            return getattr(self._real, item)

        @property
        def st_uid(self):
            return 424242

        @property
        def st_mode(self):
            return self._real.st_mode

    def _fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(os.path.realpath(uv_path)):
            return _FakeStat(result)
        return result

    monkeypatch.setattr(exec_mod.os, "stat", _fake_stat)

    assert exec_mod._trusted_toolchain_dirs("uv") == []


# ---------------------------------------------------------------------------
# Issue #2251: account-home `~/.local/bin` is excluded from
# `_safe_path_entries()` entirely (CWE-427 search-selection hardening),
# superseding the PR #2247 review P1-3 same-UID ancestor
# ownership/writability/symlink validation this section used to document
# (that validation function, `_validate_account_local_bin_trust`, has been
# removed along with the `.local/bin` entry it used to gate -- see
# `test_safe_path_entries_excludes_local_bin` above for the AC1 exclusion
# test). This section covers the remaining Issue #2251 ACs: AC2 (fake
# `.local/bin` executable is never selected), AC3 (hostedtoolcache lane
# regression), AC5 (fail-closed when `uv` exists only under `.local/bin`),
# and AC7 (`pyproject.toml` required-version SSOT consumption).
# ---------------------------------------------------------------------------


def test_local_bin_fake_executable_not_selected(monkeypatch, tmp_path):
    """GIVEN a fake `uv` executable planted at account-home `.local/bin/uv`
    (not sourced from the hostedtoolcache trust root)
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it never selects the account-home fake executable -- either it
    fails closed with `uv_not_found`, or (if a real `uv` happens to exist
    elsewhere on the fixed system/hostedtoolcache trust roots) it resolves
    to that real executable, never to the fake one under `.local/bin`
    (Issue #2251 AC2 negative test)."""
    home = tmp_path / "account-home"
    fake_local_bin = home / ".local" / "bin"
    fake_local_bin.mkdir(parents=True)
    fake_uv = fake_local_bin / "uv"
    fake_uv.write_text("#!/bin/sh\necho 'uv 0.11.29'\n", encoding="utf-8")
    fake_uv.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    monkeypatch.setenv("HOME", str(home))

    try:
        resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))
    except RuntimeError as exc:
        assert "uv_not_found" in str(exc)
        return

    assert os.path.realpath(resolved) != os.path.realpath(str(fake_uv))
    assert not resolved.startswith(str(fake_local_bin))


def test_hostedtoolcache_lane_regression(monkeypatch, tmp_path):
    """GIVEN a well-formed hostedtoolcache `uv` (the system/hostedtoolcache
    lane) and no account-home `.local/bin` involvement at all
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it still resolves successfully from the hostedtoolcache root --
    excluding account-home `.local/bin` from the trust root candidates
    does not regress this lane (Issue #2251 AC3)."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    uv_path = _write_fake_uv(hosted_root, "0.11.29")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.realpath(resolved) == os.path.realpath(str(uv_path))


def test_local_bin_only_fails_closed(monkeypatch, tmp_path):
    """GIVEN `uv` exists only at account-home `.local/bin` (no
    hostedtoolcache candidate at all)
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it fails closed with a `uv_not_found` RuntimeError rather than
    silently falling back to the account-home executable -- no new pin
    mechanism is introduced, this is the existing fail-closed
    `{name}_not_found` path (Issue #2251 AC5)."""
    home = tmp_path / "account-home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    uv_path = local_bin / "uv"
    uv_path.write_text("#!/bin/sh\necho 'uv 0.11.29'\n", encoding="utf-8")
    uv_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setitem(
        exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", tmp_path / "no-hostedtoolcache-here"
    )

    with pytest.raises(RuntimeError, match="uv_not_found"):
        exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))


def test_required_version_mismatch_fails_closed(tmp_path):
    """GIVEN a `uv` executable whose `--version` banner does not match
    `pyproject.toml`'s `[tool.uv].required-version` pin
    WHEN `_validate_trusted_executable_version("uv", ...)` is called
    THEN it fails closed (returns False) rather than accepting the
    mismatched version (Issue #2251 AC7: `.local/bin`-derived -- and any
    other non-hostedtoolcache -- version validation consumes the
    `pyproject.toml` SSOT, exact match only)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text(
        '[tool.uv]\nrequired-version = "==0.11.29"\n', encoding="utf-8"
    )
    mismatched_uv = tmp_path / "mismatched-uv"
    mismatched_uv.write_text("#!/bin/sh\necho 'uv 9.9.9'\n", encoding="utf-8")
    mismatched_uv.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    assert not exec_mod._validate_trusted_executable_version(
        "uv", str(mismatched_uv), str(project_root)
    )


def test_required_version_match_succeeds(tmp_path):
    """GIVEN a `uv` executable whose `--version` banner exactly matches
    `pyproject.toml`'s `[tool.uv].required-version` pin
    WHEN `_validate_trusted_executable_version("uv", ...)` is called
    THEN it succeeds (Issue #2251 AC7 positive counterpart)."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text(
        '[tool.uv]\nrequired-version = "==0.11.29"\n', encoding="utf-8"
    )
    matching_uv = tmp_path / "matching-uv"
    matching_uv.write_text("#!/bin/sh\necho 'uv 0.11.29'\n", encoding="utf-8")
    matching_uv.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    assert exec_mod._validate_trusted_executable_version(
        "uv", str(matching_uv), str(project_root)
    )


def test_required_version_missing_ssot_fails_closed(tmp_path):
    """GIVEN a `project_root` whose `pyproject.toml` has no
    `[tool.uv].required-version`
    WHEN `_validate_trusted_executable_version("uv", ...)` is called
    THEN it fails closed (returns False) -- there is nothing trustworthy to
    compare against, so this is not silently skipped (Issue #2251 AC7)."""
    project_root = tmp_path / "project-without-pin"
    project_root.mkdir()
    (project_root / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    some_uv = tmp_path / "some-uv"
    some_uv.write_text("#!/bin/sh\necho 'uv 0.11.29'\n", encoding="utf-8")
    some_uv.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

    assert not exec_mod._validate_trusted_executable_version(
        "uv", str(some_uv), str(project_root)
    )


def test_required_uv_version_reads_repo_pyproject_toml_ssot():
    """GIVEN the real repository `pyproject.toml`
    WHEN `_required_uv_version` is called with `REPO_ROOT`
    THEN it returns the exact pinned version, read-only, from
    `[tool.uv].required-version` (Issue #2251 AC7 SSOT-consumption
    requirement -- no new version literal is introduced by this module)."""
    version = exec_mod._required_uv_version(str(REPO_ROOT))

    assert version is not None
    assert exec_mod._EXACT_VERSION_PIN_RE.fullmatch(f"=={version}")


# ---------------------------------------------------------------------------
# PR #2247 review P1-4.3: `_sanitize_env` must be proven against a real
# child process's actual environment/stdout/stderr/artifact, not just an
# in-process dict comparison. The existing in-process assertions on
# `_sanitize_env`'s return value are kept elsewhere; this test adds the
# missing black-box layer.
# ---------------------------------------------------------------------------


def test_sanitize_env_secret_canary_never_reaches_real_child_process(monkeypatch, tmp_path):
    """GIVEN a secret canary embedded only in a fake host `GH_CONFIG_DIR`
    (its env var value AND its on-disk `hosts.yml` content -- the thing an
    isolated Claude-GPT session's launcher must never forward, per Issue
    #2241 AC2 / #2232 comment 5316900237)
    WHEN `_sanitize_env` builds a child environment for the default
    (non-fixture) command lane, and that environment is used to spawn a
    REAL child process (not an in-process mock)
    THEN the canary never appears in the child's own reported environment
    snapshot, the child's stdout, the child's stderr, or any artifact file
    the child writes -- `_sanitize_env` unconditionally drops the
    `GH_CONFIG_DIR` key itself for this command lane, so its value (the
    canary-bearing directory path) cannot leak even though `GH_TOKEN` is
    intentionally allowlisted for lanes that need authenticated `gh` calls
    (Issue #2241 AC2, PR #2247 review P1-4.3)."""
    canary = "CANARY-SECRET-6f3c9b2a-2247"
    fake_gh_config_dir = tmp_path / "fake-gh-config-with-canary" / canary
    fake_gh_config_dir.mkdir(parents=True)
    (fake_gh_config_dir / "hosts.yml").write_text(f"canary: {canary}\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(fake_gh_config_dir))

    child_env = exec_mod._sanitize_env(str(REPO_ROOT))
    # Non-fixture command lane: `GH_CONFIG_DIR` is dropped outright, so its
    # canary-bearing path value cannot appear anywhere downstream.
    assert "GH_CONFIG_DIR" not in child_env
    assert canary not in json.dumps(child_env)

    artifact_path = tmp_path / "child-artifact.txt"
    probe_script = (
        "import json, os, sys\n"
        "env_snapshot = dict(os.environ)\n"
        "sys.stdout.write(json.dumps(env_snapshot))\n"
        f"open({str(artifact_path)!r}, 'w').write(json.dumps(env_snapshot))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe_script],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )

    assert canary not in result.stdout
    assert canary not in result.stderr
    child_reported_env = json.loads(result.stdout)
    assert canary not in json.dumps(child_reported_env)
    assert "GH_CONFIG_DIR" not in child_reported_env
    artifact_text = artifact_path.read_text(encoding="utf-8")
    assert canary not in artifact_text
