"""Tests for the Issue #1779 kernel-enforced read-only boundary of
`materialize_isolated_agy_workspace()`'s `agy_oauth_token_path` exposure in
`agy_permission_policy.py`.

Covers Issue #1779 AC4/AC5/AC6/AC7 (Runtime Verification Applicability:
`decision: immediate` -- these are hermetic integration tests that actually
spawn `bwrap` and attempt real filesystem writes against a dummy fixture
token file; no real AGY credential is used):

- AC4 (`-k kernel_enforced`): when `bwrap` is available,
  `agy_oauth_token_readonly_mode == AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED`
  and a usable `bwrap` argv prefix is returned.
- AC5 (`-k write_fails`): the `bwrap` prefix, prepended to a real subprocess
  command, kernel-enforces read-only access -- a write attempt through
  `agy_oauth_token_path` fails, a read attempt succeeds with the real
  content, and the real host fixture file is never mutated.
- AC6 (`-k degraded`): when `bwrap` is unavailable,
  `agy_oauth_token_readonly_mode == AGY_OAUTH_TOKEN_READONLY_DEGRADED`, no
  `bwrap` prefix is returned, and workspace materialization still succeeds
  for a non-security-sensitive profile (`grounded_research`).
- AC7 (`-k fail_closed`): when `bwrap` is unavailable, the real agy OAuth
  token file exists, and the profile is `no_tools` or `local_asset_research`,
  `materialize_isolated_agy_workspace()` fail-closes with
  `AgyReadOnlyBoundaryError` and no workspace is created (Stop Conditions:
  CI green must not depend on `bwrap` being present -- this branch is
  exercised via monkeypatching `app._bwrap_available`, not by requiring an
  actual bwrap-less CI runner).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors the pattern
# used by test_agy_permission_policy.py / test_agy_permission_policy_oauth_token.py.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"
_MODULE_NAME = "agy_permission_policy_1779_readonly_boundary_test"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


app = _load_module()

_BWRAP_AVAILABLE = shutil.which("bwrap") is not None


def _make_fake_agy_home(tmp_path: Path, *, token_content: str = "fake-dummy-token-value") -> Path:
    fake_real_home = tmp_path / "real-home"
    token_dir = fake_real_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text(token_content, encoding="utf-8")
    return fake_real_home


# ---------------------------------------------------------------------------
# AC4: kernel_enforced_ro_bind mode when bwrap is available
#
# Issue #1779 Stop Conditions: `bwrap` must not be assumed present in every
# CI/implementation environment. Rather than skipping when it is absent,
# these tests adapt to the *actual* `_bwrap_available()` result on the host
# running them and assert the mode that result implies -- the host with
# `bwrap` available (this dev sandbox; also expected in the CI runner image)
# exercises the AC4/AC5 kernel-enforced assertions for real, while a host
# without it exercises the AC6 degraded-mode assertions instead (also
# covered unconditionally, without depending on host `bwrap` state, by the
# monkeypatched AC6/AC7 tests below).
# ---------------------------------------------------------------------------


def test_kernel_enforced_mode_when_bwrap_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        assert workspace.agy_oauth_token_path is not None
        if _BWRAP_AVAILABLE:
            assert (
                workspace.agy_oauth_token_readonly_mode
                == app.AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED
            )
            assert workspace.agy_oauth_token_bwrap_prefix is not None
            assert workspace.agy_oauth_token_bwrap_prefix[0] == "bwrap"
            assert str(workspace.agy_oauth_token_path) in workspace.agy_oauth_token_bwrap_prefix
        else:
            assert (
                workspace.agy_oauth_token_readonly_mode
                == app.AGY_OAUTH_TOKEN_READONLY_DEGRADED
            )
            assert workspace.agy_oauth_token_bwrap_prefix is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_kernel_enforced_mode_absent_when_no_real_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home-no-token"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        assert workspace.agy_oauth_token_path is None
        assert workspace.agy_oauth_token_readonly_mode == app.AGY_OAUTH_TOKEN_READONLY_ABSENT
        assert workspace.agy_oauth_token_bwrap_prefix is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC5: bwrap prefix actually kernel-enforces read-only (write fails, read
# succeeds) against a dummy fixture token file -- never a real credential.
# ---------------------------------------------------------------------------


def test_bwrap_prefix_write_fails_read_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy_content = "DUMMY_FIXTURE_TOKEN_NOT_A_REAL_CREDENTIAL"
    fake_real_home = _make_fake_agy_home(tmp_path, token_content=dummy_content)
    monkeypatch.setenv("HOME", str(fake_real_home))
    real_token_file = fake_real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        prefix = workspace.agy_oauth_token_bwrap_prefix
        token_path = str(workspace.agy_oauth_token_path)

        if not _BWRAP_AVAILABLE:
            # Host has no bwrap: degraded mode was correctly selected (AC6),
            # and this AC's kernel-enforcement assertions do not apply here
            # (covered instead by test_degraded_mode_when_bwrap_unavailable,
            # which forces the same branch deterministically on any host).
            assert (
                workspace.agy_oauth_token_readonly_mode
                == app.AGY_OAUTH_TOKEN_READONLY_DEGRADED
            )
            assert prefix is None
            return

        assert (
            workspace.agy_oauth_token_readonly_mode
            == app.AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED
        )
        assert prefix is not None

        # Read attempt through the bwrap-shadowed path succeeds with the
        # real (dummy fixture) content.
        read_script = f"print(open({token_path!r}).read(), end='')"
        read_result = subprocess.run(
            prefix + [sys.executable, "-c", read_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert read_result.returncode == 0, read_result.stderr
        assert read_result.stdout == dummy_content

        # Write attempt through the bwrap-shadowed path fails (kernel-level
        # EROFS / "Read-only file system", not merely a Python-level guard).
        write_script = f"open({token_path!r}, 'a').write('SHOULD_NOT_WRITE')"
        write_result = subprocess.run(
            prefix + [sys.executable, "-c", write_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert write_result.returncode != 0
        assert (
            "Read-only file system" in write_result.stderr
            or "EROFS" in write_result.stderr
        )

        # The real host fixture file itself is never mutated (this is the
        # exact class of write `AGY_READONLY_BOUNDARY_V1` proved possible
        # via a bare symlink, and this test proves the bwrap prefix
        # prevents it).
        assert real_token_file.read_text(encoding="utf-8") == dummy_content
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_baseline_symlink_alone_is_writable_without_bwrap_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control proving AC5 is testing something real: without the
    bwrap prefix, the pre-#1779 plain symlink exposure IS writable-through
    (the exact `AGY_READONLY_BOUNDARY_V1` finding this Issue fixes) --
    independent of host `bwrap` availability, since this test never uses
    the prefix at all."""
    dummy_content = "DUMMY_FIXTURE_TOKEN_NOT_A_REAL_CREDENTIAL_2"
    fake_real_home = _make_fake_agy_home(tmp_path, token_content=dummy_content)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        token_path = str(workspace.agy_oauth_token_path)
        write_script = f"open({token_path!r}, 'a').write('HACK')"
        # No bwrap prefix -- direct write through the symlink.
        result = subprocess.run(
            [sys.executable, "-c", write_script], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6: degraded_symlink_reachability mode when bwrap is unavailable
# ---------------------------------------------------------------------------


def test_degraded_mode_when_bwrap_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    # grounded_research is not in _AUTH_READONLY_FAIL_CLOSED_PROFILES, so
    # degraded-mode continuation (not fail-closed) is expected.
    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        assert workspace.agy_oauth_token_path is not None
        assert (
            workspace.agy_oauth_token_readonly_mode == app.AGY_OAUTH_TOKEN_READONLY_DEGRADED
        )
        assert workspace.agy_oauth_token_bwrap_prefix is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_degraded_mode_proposal_only_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    workspace = app.materialize_isolated_agy_workspace(
        app.PROPOSAL_ONLY_PROFILE, parent_dir=tmp_path
    )
    try:
        assert (
            workspace.agy_oauth_token_readonly_mode == app.AGY_OAUTH_TOKEN_READONLY_DEGRADED
        )
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_degraded_mode_absent_when_no_real_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home-no-token-degraded"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path
    )
    try:
        assert workspace.agy_oauth_token_readonly_mode == app.AGY_OAUTH_TOKEN_READONLY_ABSENT
        assert workspace.agy_oauth_token_bwrap_prefix is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC7: fail-closed for security-sensitive profiles when bwrap is unavailable
# and the real token file exists.
# ---------------------------------------------------------------------------


def test_fail_closed_no_tools_when_bwrap_unavailable_and_token_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    with pytest.raises(app.AgyReadOnlyBoundaryError):
        app.materialize_isolated_agy_workspace(app.NO_TOOLS_PROFILE, parent_dir=tmp_path)

    # Stop Conditions: no workspace directory should be left behind by a
    # fail-closed call (the exception is raised before mkdtemp()).
    assert list(tmp_path.iterdir()) == [fake_real_home]


def test_fail_closed_local_asset_research_when_bwrap_unavailable_and_token_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    with pytest.raises(app.AgyReadOnlyBoundaryError):
        app.materialize_isolated_agy_workspace(
            app.LOCAL_ASSET_RESEARCH_PROFILE, parent_dir=tmp_path
        )


def test_no_fail_closed_when_bwrap_unavailable_and_token_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI-safety: fail-closed must never trigger merely because bwrap is
    absent -- only when a real token file is also present. Hermetic/CI
    environments (no real agy login) must stay green regardless of whether
    the CI image happens to have bwrap installed (Issue #1779 Stop
    Conditions)."""
    fake_real_home = tmp_path / "real-home-no-token-failclosed"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: False)

    workspace = app.materialize_isolated_agy_workspace(
        app.NO_TOOLS_PROFILE, parent_dir=tmp_path
    )
    try:
        assert workspace.agy_oauth_token_readonly_mode == app.AGY_OAUTH_TOKEN_READONLY_ABSENT
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_no_fail_closed_when_bwrap_available_even_for_sensitive_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))
    monkeypatch.setattr(app, "_bwrap_available", lambda: True)

    workspace = app.materialize_isolated_agy_workspace(
        app.NO_TOOLS_PROFILE, parent_dir=tmp_path
    )
    try:
        assert (
            workspace.agy_oauth_token_readonly_mode
            == app.AGY_OAUTH_TOKEN_READONLY_KERNEL_ENFORCED
        )
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
