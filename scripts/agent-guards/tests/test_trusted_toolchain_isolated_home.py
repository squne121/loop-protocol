"""Issue #2241: trusted toolchain resolution must not depend on the
isolated `HOME` a Claude-GPT launcher sets for a child session.

Covers AC1 (isolated `HOME` `uv` resolution succeeds) and AC6-adjacent
regression coverage (existing fail-closed adversarial trust-path checks
still block once the trust root is generalized to
`_trusted_toolchain_dirs`).

Issue #2251 originally excluded account-home `~/.local/bin` from
`_safe_path_entries()` entirely (CWE-427 search-selection hardening).
Issue #2276 / #2280 re-permits it as a no-sudo local-dev lane, gated on
the *real* OS account home (`pwd.getpwuid(os.getuid()).pw_dir`, never the
ambient `HOME` env var) and the same exact-version pin used by the
system PATH lane. AC1/AC2/AC3/AC5/AC7 coverage for the #2251 change, and
the #2276/#2280 account-home lane coverage, both live in this module
(see the "Issue #2251 / #2276" section below); AC4 is this whole
module's positive-path regression suite, and AC6 is
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
    caller, and a deterministic hostedtoolcache `uv` (real host state --
    whether the real OS account also happens to have a valid `uv` under
    its own `.local/bin` -- must not decide this test's outcome, since the
    hostedtoolcache trust root is always consulted first, see
    `test_safe_path_entries_includes_real_account_home_local_bin` for the
    account-home lane's own dedicated coverage)
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it succeeds by resolving `uv` from the hosted toolcache root, not
    from the isolated `HOME`'s now-nonexistent `.local/bin` (Issue #2241
    AC1 / Issue #2251 AC4)."""
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


def test_safe_path_entries_excludes_env_spoofed_home_local_bin(monkeypatch, tmp_path):
    """GIVEN an existing `.local/bin` directory under an env-spoofed `HOME`
    that differs from the real (`pwd`-resolved) account home
    WHEN `_safe_path_entries` is called
    THEN no entry is, or is derived from, that env-spoofed-HOME `.local/bin`
    directory -- only the real account home's `.local/bin` (see
    `test_safe_path_entries_includes_real_account_home_local_bin` below) is
    ever a candidate (Issue #2251 AC1 origin; Issue #2276 narrows the
    exclusion from "all `.local/bin`" to "any `.local/bin` not derived from
    the real OS account home")."""
    real_account_home = tmp_path / "real-account-home-different-from-spoof"
    real_account_home.mkdir()
    _patch_real_account_home(monkeypatch, real_account_home)
    spoofed_home = tmp_path / "isolated-home"
    (spoofed_home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(spoofed_home))

    entries = exec_mod._safe_path_entries()

    assert not any(str(spoofed_home) in entry for entry in entries)


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
# Issue #2251 / #2276: account-home `~/.local/bin` was excluded from
# `_safe_path_entries()` entirely by Issue #2251 (CWE-427 search-selection
# hardening), then re-permitted by Issue #2276 / #2280 as a no-sudo
# local-dev lane -- gated on the *real* OS account home
# (`pwd.getpwuid(os.getuid()).pw_dir`, never the ambient `HOME` env var)
# and the same exact-version pin used by the system PATH lane. This
# section covers: AC2 (env-spoofed-HOME-derived `.local/bin` executable is
# never selected), AC3 (hostedtoolcache lane regression), AC7
# (`pyproject.toml` required-version SSOT consumption), and the #2276/#2280
# account-home lane itself (real-account-home positive, HOME-spoof
# negative, wrong-version fail-closed).
# ---------------------------------------------------------------------------


class _FakePasswdEntry:
    def __init__(self, pw_dir: str) -> None:
        self.pw_dir = pw_dir


def _patch_real_account_home(monkeypatch, home_dir) -> None:
    """Patch `pwd.getpwuid` (not `HOME`) so `_os_account_home()` resolves
    to `home_dir`, independent of whatever the test also does to the
    `HOME` env var."""
    monkeypatch.setattr(
        exec_mod.pwd, "getpwuid", lambda uid: _FakePasswdEntry(str(home_dir))
    )


def test_fake_home_local_bin_never_selected_even_if_populated(monkeypatch, tmp_path):
    """GIVEN a fake `uv` executable (correct-version banner) planted at
    `$HOME/.local/bin/uv`, where `HOME` is env-spoofed to an attacker path
    that differs from the real OS account home
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it never selects the `HOME`-derived fake executable -- either it
    fails closed with `uv_not_found`, or (if a real `uv` happens to exist
    on the fixed system/hostedtoolcache trust roots, or the real account
    home) it resolves to that real executable, never to the fake one
    (Issue #2251 AC2 / Issue #2276 AC4 negative test)."""
    spoofed_home = tmp_path / "attacker-controlled-home"
    fake_local_bin = spoofed_home / ".local" / "bin"
    fake_local_bin.mkdir(parents=True)
    fake_uv = fake_local_bin / "uv"
    fake_uv.write_text("#!/bin/sh\necho 'uv 0.11.29'\n", encoding="utf-8")
    fake_uv.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    monkeypatch.setenv("HOME", str(spoofed_home))
    real_account_home = tmp_path / "real-account-home-empty"
    real_account_home.mkdir()
    _patch_real_account_home(monkeypatch, real_account_home)
    monkeypatch.setitem(
        exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", tmp_path / "no-hostedtoolcache-here"
    )

    try:
        resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))
    except RuntimeError as exc:
        assert "uv_not_found" in str(exc) or "uv_version_mismatch" in str(exc)
        return

    # A dev/CI box may legitimately have a real, correctly pinned `uv` on
    # the system PATH lane (e.g. `/usr/local/bin/uv`) -- that is a valid
    # resolution and not what this test guards against. What must never
    # happen is resolving to the `HOME`-spoofed fake.
    assert os.path.realpath(resolved) != os.path.realpath(str(fake_uv))
    assert not resolved.startswith(str(fake_local_bin))


def test_hostedtoolcache_lane_regression(monkeypatch, tmp_path):
    """GIVEN a well-formed hostedtoolcache `uv` (the system/hostedtoolcache
    lane) and no account-home `.local/bin` involvement at all
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it still resolves successfully from the hostedtoolcache root --
    re-permitting account-home `.local/bin` as a candidate does not
    regress this lane (Issue #2251 AC3 / Issue #2276 AC7)."""
    hosted_root = tmp_path / "hostedtoolcache" / "uv"
    uv_path = _write_fake_uv(hosted_root, "0.11.29")
    monkeypatch.setitem(exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", hosted_root)

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.realpath(resolved) == os.path.realpath(str(uv_path))


def test_safe_path_entries_includes_real_account_home_local_bin(monkeypatch, tmp_path):
    """GIVEN a real (`pwd`-resolved) account home with a `.local/bin`
    directory, and a *different* `HOME`-env-spoofed directory that also has
    a `.local/bin` directory
    WHEN `_safe_path_entries` is called
    THEN the real account home's `.local/bin` is included when resolving
    `uv`, but the `HOME`-spoofed directory's `.local/bin` is never included
    regardless (Issue #2276 AC2/AC4)."""
    real_account_home = tmp_path / "real-account-home"
    (real_account_home / ".local" / "bin").mkdir(parents=True)
    _patch_real_account_home(monkeypatch, real_account_home)
    spoofed_home = tmp_path / "spoofed-home"
    (spoofed_home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(spoofed_home))

    entries = exec_mod._safe_path_entries("uv")

    real_local_bin = str(real_account_home / ".local" / "bin")
    spoofed_local_bin = str(spoofed_home / ".local" / "bin")
    assert real_local_bin in entries
    assert spoofed_local_bin not in entries


def test_safe_path_entries_excludes_account_home_for_non_uv_names(monkeypatch, tmp_path):
    """GIVEN a real account home whose `.local/bin` contains a fake `git`
    executable (same-UID writable, exactly the CWE-427 lookalike-binary
    scenario Issue #2251 closed)
    WHEN `_safe_path_entries("git")` -- or the default, name-agnostic
    `_safe_path_entries()` used for `_sanitize_env`/`sanitized_git_subprocess_env`
    -- is called
    THEN the account-home lane is NOT included: it is scoped to `uv`
    resolution only (Issue #2280 Out of Scope: this Issue's `uv` trust
    boundary decision must not widen trust for `git` or any other
    executable name, since `_validate_trusted_executable_version` is a
    no-op for non-`uv` names and would provide no defense-in-depth if this
    lane were shared)."""
    real_account_home = tmp_path / "real-account-home"
    (real_account_home / ".local" / "bin").mkdir(parents=True)
    _patch_real_account_home(monkeypatch, real_account_home)

    real_local_bin = str(real_account_home / ".local" / "bin")
    assert real_local_bin not in exec_mod._safe_path_entries("git")
    assert real_local_bin not in exec_mod._safe_path_entries(None)
    assert real_local_bin not in exec_mod._safe_path_entries()


def test_resolve_trusted_executable_git_never_selects_account_home_fake(monkeypatch, tmp_path):
    """GIVEN a fake `git` executable planted at the real account home's
    `.local/bin` (same-UID writable), and no real `git` reachable from the
    hostedtoolcache/system lanes
    WHEN `_resolve_trusted_executable("git", ...)` is called
    THEN it never resolves to the account-home fake -- either it fails
    closed with `git_not_found`, or (if a real `git` happens to exist on
    the fixed system directories) it resolves to that real executable,
    never to the account-home fake, since the account-home lane is `uv`-
    scoped only (Issue #2280 adversarial regression: this Issue's `uv`
    trust boundary decision must not widen trust for `git`)."""
    real_account_home = tmp_path / "real-account-home"
    fake_local_bin = real_account_home / ".local" / "bin"
    fake_local_bin.mkdir(parents=True)
    fake_git = fake_local_bin / "git"
    fake_git.write_text("#!/bin/sh\necho fake-git\n", encoding="utf-8")
    fake_git.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _patch_real_account_home(monkeypatch, real_account_home)

    try:
        resolved = exec_mod._resolve_trusted_executable("git", str(REPO_ROOT))
    except RuntimeError as exc:
        assert "git_not_found" in str(exc)
        return

    assert os.path.realpath(resolved) != os.path.realpath(str(fake_git))
    assert not resolved.startswith(str(fake_local_bin))


def test_account_home_local_bin_uv_correct_version_succeeds(monkeypatch, tmp_path):
    """GIVEN `uv` exists only at the real (`pwd`-resolved) account home's
    `.local/bin`, with a `--version` banner matching `pyproject.toml`'s
    pin, and no hostedtoolcache candidate at all
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it succeeds, resolving from the account-home lane (Issue #2276
    AC3: no-sudo local-dev positive lane)."""
    real_account_home = tmp_path / "real-account-home"
    local_bin = real_account_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    uv_path = local_bin / "uv"
    required_version = exec_mod._required_uv_version(str(REPO_ROOT))
    assert required_version is not None
    uv_path.write_text(f"#!/bin/sh\necho 'uv {required_version}'\n", encoding="utf-8")
    uv_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _patch_real_account_home(monkeypatch, real_account_home)
    monkeypatch.setitem(
        exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", tmp_path / "no-hostedtoolcache-here"
    )

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.realpath(resolved) == os.path.realpath(str(uv_path))


def test_fake_home_env_does_not_move_account_home_trust_root(monkeypatch, tmp_path):
    """GIVEN a real (`pwd`-resolved) account home with a correct-version
    `uv` under `.local/bin`, while `HOME` is env-spoofed to a different,
    empty directory
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it still resolves successfully via the real account home -- the
    `HOME` env var spoof does not relocate the trust root (Issue #2276
    AC1/AC4: account-home resolution is `pwd`-derived, not `HOME`-derived)."""
    real_account_home = tmp_path / "real-account-home"
    local_bin = real_account_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    uv_path = local_bin / "uv"
    required_version = exec_mod._required_uv_version(str(REPO_ROOT))
    assert required_version is not None
    uv_path.write_text(f"#!/bin/sh\necho 'uv {required_version}'\n", encoding="utf-8")
    uv_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _patch_real_account_home(monkeypatch, real_account_home)
    spoofed_home = tmp_path / "spoofed-home-empty"
    spoofed_home.mkdir()
    monkeypatch.setenv("HOME", str(spoofed_home))
    monkeypatch.setitem(
        exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", tmp_path / "no-hostedtoolcache-here"
    )

    resolved = exec_mod._resolve_trusted_executable("uv", str(REPO_ROOT))

    assert os.path.realpath(resolved) == os.path.realpath(str(uv_path))


def test_account_home_local_bin_uv_wrong_version_fails_closed(monkeypatch, tmp_path):
    """GIVEN `uv` exists only at the real account home's `.local/bin`, but
    its `--version` banner does not match `pyproject.toml`'s pin
    WHEN `_resolve_trusted_executable("uv", ...)` is called
    THEN it fails closed with `uv_version_mismatch` rather than trusting a
    wrong-version executable from the account-home lane (Issue #2276
    AC5)."""
    real_account_home = tmp_path / "real-account-home"
    local_bin = real_account_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    uv_path = local_bin / "uv"
    uv_path.write_text("#!/bin/sh\necho 'uv 0.0.1'\n", encoding="utf-8")
    uv_path.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    _patch_real_account_home(monkeypatch, real_account_home)
    monkeypatch.setitem(
        exec_mod._TRUSTED_TOOLCHAIN_HOSTED_ROOTS, "uv", tmp_path / "no-hostedtoolcache-here"
    )

    with pytest.raises(RuntimeError, match="uv_version_mismatch"):
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
