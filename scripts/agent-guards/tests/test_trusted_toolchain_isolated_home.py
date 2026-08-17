"""Issue #2241: trusted toolchain resolution must not depend on the
isolated `HOME` a Claude-GPT launcher sets for a child session.

Covers AC1 (isolated `HOME` `uv` resolution succeeds) and AC6-adjacent
regression coverage (existing fail-closed adversarial trust-path checks
still block once the trust root is generalized to
`_trusted_toolchain_dirs`).
"""

from __future__ import annotations

import os
import stat
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
    caller
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it succeeds by resolving `uv` from the real hosted toolcache root
    (or the account/system PATH), not from the isolated `HOME`'s
    now-nonexistent `.local/bin` (Issue #2241 AC1)."""
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "uv"
    assert os.access(resolved, os.X_OK)
    # The isolated HOME's .local/bin must never be where this actually
    # resolved from -- it is empty by construction.
    assert not resolved.startswith(str(isolated_home))


def test_os_account_home_ignores_home_env_override(monkeypatch, tmp_path):
    """GIVEN `HOME` overridden to an attacker/isolated-session-controlled
    directory
    WHEN `_os_account_home` is called
    THEN it still returns the real OS account home directory (via the
    passwd database), never the overridden `HOME` value (Issue #2241)."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    account_home = exec_mod._os_account_home()

    assert account_home != str(fake_home)


def test_safe_path_entries_local_bin_uses_os_account_home_not_isolated_home(monkeypatch, tmp_path):
    """GIVEN an isolated HOME
    WHEN `_safe_path_entries` is called
    THEN its `.local/bin` trust entry is derived from the OS account home,
    not the isolated `HOME` (Issue #2241 AC1 / AC6 regression)."""
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_home))

    entries = exec_mod._safe_path_entries()
    local_bin_entries = [e for e in entries if e.endswith(str(Path(".local") / "bin"))]

    assert local_bin_entries, "expected a .local/bin trust entry"
    assert not any(str(isolated_home) in entry for entry in local_bin_entries)


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
