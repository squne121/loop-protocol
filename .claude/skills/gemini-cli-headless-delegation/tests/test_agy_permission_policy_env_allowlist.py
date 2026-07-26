"""Tests for the Issue #1726 env-allowlist extension of
`materialize_isolated_agy_workspace()` in `agy_permission_policy.py`.

Covers AC1-AC7 (Issue #1726 original numbering) plus Issue #1779's
`auth_profile` minimization -- Issue #1779 AC1/AC3 (this file's canonical
VC target):

- AC1 (#1726) / AC3 (#1779): `DBUS_SESSION_BUS_ADDRESS` / `XDG_RUNTIME_DIR`
  propagate through **only** when the caller explicitly opts in via
  `auth_profile=app.AGY_AUTH_PROFILE_EXTENDED`. With the default
  `auth_profile` (omitted -- `AGY_AUTH_PROFILE_MINIMAL`), neither surface is
  exposed even when present in the real env (Issue #1779 AC1: env excludes
  DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR/GOOGLE_APPLICATION_CREDENTIALS
  and `gcloud_adc_path` is `None`).
- AC2: `HOME`/`XDG_CONFIG_HOME`/`XDG_CACHE_HOME`/`XDG_STATE_HOME` isolation
  regression (still redirected into the isolated tmp workspace) for both
  `auth_profile` values.
- AC3 (#1726) is a static `rg` check on the source comments (see the Issue
  VC list); not exercised here.
- AC4: adversarial redaction -- a credential-like value that happens to be
  assigned to `DBUS_SESSION_BUS_ADDRESS` must never leak into any public
  artifact string (env dict values are opaque pass-through; the *helpers*
  that build public-facing strings -- `record_denied_tool_attempt()` /
  `redact_secret_safe()` -- must still redact it wherever they touch it).
- AC5: tool-deny matrix regression (hostile_global_settings_fixture) after
  the allowlist extension.
- AC6: `find_credential_like_files()` regression after the allowlist
  extension.
- AC7: hermetic integration test simulating isolated-workspace auth
  reachability via a mocked dbus/keyring endpoint (a fake Unix domain
  socket bound at the propagated `DBUS_SESSION_BUS_ADDRESS` path), under
  `auth_profile=app.AGY_AUTH_PROFILE_EXTENDED`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors the pattern
# used by test_agy_permission_policy.py.
# ---------------------------------------------------------------------------

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("agy_permission_policy", _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


app = _load_module()

ALL_PROFILES = [
    app.NO_TOOLS_PROFILE,
    app.LOCAL_ASSET_RESEARCH_PROFILE,
    app.GROUNDED_RESEARCH_PROFILE,
    app.PROPOSAL_ONLY_PROFILE,
]

ALL_DENY_PROFILES = [
    app.NO_TOOLS_PROFILE,
    app.LOCAL_ASSET_RESEARCH_PROFILE,
    app.PROPOSAL_ONLY_PROFILE,
]


def _materialize_and_cleanup(profile: str, *, parent_dir: Path | None = None):
    workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=parent_dir)
    return workspace


# ---------------------------------------------------------------------------
# Issue #1779 AC1: default (auth_profile omitted == AGY_AUTH_PROFILE_MINIMAL)
# excludes DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR /
# GOOGLE_APPLICATION_CREDENTIALS and gcloud_adc_path stays None, even when
# all are present/set in the real env.
# ---------------------------------------------------------------------------


def test_isolated_workspace_minimal_auth_profile_omits_env_allowlist_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/home/real-user/.config/gcloud/adc.json")

    # auth_profile omitted -- defaults to AGY_AUTH_PROFILE_MINIMAL.
    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE)
    try:
        assert "DBUS_SESSION_BUS_ADDRESS" not in workspace.env
        assert "XDG_RUNTIME_DIR" not in workspace.env
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in workspace.env
        assert workspace.gcloud_adc_path is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_isolated_workspace_explicit_minimal_auth_profile_omits_env_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_MINIMAL
    )
    try:
        assert "DBUS_SESSION_BUS_ADDRESS" not in workspace.env
        assert "XDG_RUNTIME_DIR" not in workspace.env
        assert workspace.gcloud_adc_path is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_materialize_isolated_agy_workspace_rejects_unknown_auth_profile() -> None:
    with pytest.raises(ValueError):
        app.materialize_isolated_agy_workspace(
            app.GROUNDED_RESEARCH_PROFILE, auth_profile="not_a_real_auth_profile"
        )


# ---------------------------------------------------------------------------
# AC1 (#1726) / AC3 (#1779): DBUS_SESSION_BUS_ADDRESS / XDG_RUNTIME_DIR
# propagate through only under the explicit
# auth_profile=AGY_AUTH_PROFILE_EXTENDED opt-in.
# ---------------------------------------------------------------------------


def test_isolated_workspace_env_includes_dbus_session_bus_address_when_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bus_address = "unix:path=/run/user/1000/bus"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", fake_bus_address)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert workspace.env.get("DBUS_SESSION_BUS_ADDRESS") == fake_bus_address
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_isolated_workspace_env_omits_dbus_session_bus_address_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert "DBUS_SESSION_BUS_ADDRESS" not in workspace.env
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_isolated_workspace_env_includes_xdg_runtime_dir_when_present_and_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        assert workspace.env.get("XDG_RUNTIME_DIR") == "/run/user/1000"
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC2: HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME/XDG_STATE_HOME isolation regression
# ---------------------------------------------------------------------------


def test_isolated_workspace_home_and_xdg_still_redirected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_home = os.environ.get("HOME", "/home/real-user")
    monkeypatch.setenv("HOME", real_home)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(
            profile, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.env["HOME"] != real_home
            assert workspace.env["XDG_CONFIG_HOME"] == str(workspace.workspace_dir / "xdg-config")
            assert workspace.env["XDG_CACHE_HOME"] == str(workspace.workspace_dir / "xdg-cache")
            assert workspace.env["XDG_STATE_HOME"] == str(workspace.workspace_dir / "xdg-state")
            # the real $HOME value must not leak into the isolated identity
            # variables (PATH legitimately may contain path segments under
            # the real $HOME, e.g. ~/.local/bin, so only the isolation
            # target variables are asserted here).
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
                assert real_home not in workspace.env[key]
            # the reachability variables are still present alongside isolation
            assert workspace.env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
            assert workspace.env["XDG_RUNTIME_DIR"] == "/run/user/1000"
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_isolated_workspace_home_and_xdg_still_redirected_minimal_auth_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same isolation guarantee holds for the default (minimal) auth_profile,
    which never exposes the reachability variables at all (Issue #1779)."""
    real_home = os.environ.get("HOME", "/home/real-user")
    monkeypatch.setenv("HOME", real_home)

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile)
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.env["HOME"] != real_home
            assert workspace.env["XDG_CONFIG_HOME"] == str(workspace.workspace_dir / "xdg-config")
            assert workspace.env["XDG_CACHE_HOME"] == str(workspace.workspace_dir / "xdg-cache")
            assert workspace.env["XDG_STATE_HOME"] == str(workspace.workspace_dir / "xdg-state")
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC4: adversarial redaction -- credential-like DBUS_SESSION_BUS_ADDRESS value
# ---------------------------------------------------------------------------


def test_env_allowlist_extension_never_leaks_credential_like_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate a pathological / adversarial environment where
    # DBUS_SESSION_BUS_ADDRESS happens to contain a credential-like
    # substring (this should never happen in practice -- it is a socket
    # path -- but the redaction helpers must still be robust to it).
    adversarial_value = "unix:path=/run/user/1000/bus;token=sk-abcdefghijklmnopqrstuvwx"
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", adversarial_value)

    workspace = app.materialize_isolated_agy_workspace(
        app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
    )
    try:
        # The raw env dict is an internal wiring structure (never printed to
        # a public artifact directly) and does propagate the value verbatim
        # -- that is the documented contract (AC1). But sanity-check the
        # credential-like scanner actually flags it, and that the two public
        # artifact-facing helpers redact it correctly wherever a caller
        # might pass it through (e.g. logging denied-tool-attempt args that
        # embed environment context).
        assert app.scan_credential_like(adversarial_value) is True
        assert app.scan_credential_like(workspace.env["DBUS_SESSION_BUS_ADDRESS"]) is True

        redacted = app.redact_secret_safe(adversarial_value)
        assert "sk-abcdefghijklmnopqrstuvwx" not in redacted
        assert app.scan_credential_like(redacted) is False

        # record_denied_tool_attempt() redacts raw_args before storing --
        # simulate a denied tool call whose args happen to embed the
        # adversarial env value (e.g. copy-pasted debugging context).
        record = app.record_denied_tool_attempt(
            app.NO_TOOLS_PROFILE,
            "shell",
            raw_args={"env_hint": adversarial_value},
        )
        assert "sk-abcdefghijklmnopqrstuvwx" not in record["args_redacted"]
        assert record["contained_credential_like_pattern"] is True
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC5: tool-deny matrix regression after the allowlist extension
# ---------------------------------------------------------------------------


def test_tool_deny_matrix_unaffected_by_env_allowlist_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    hostile = app.hostile_global_settings_fixture()
    assert hostile["permissions"]["default"] == "allow"
    assert set(hostile["permissions"]["allow"]) == app.AGY_DIRECT_TOOL_NAMES
    assert hostile["permissions"]["deny"] == []

    for profile in ALL_DENY_PROFILES:
        for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
            decision = app.resolve_tool_permission(profile, tool_name, global_settings=hostile)
            assert decision == "deny", (
                f"hostile global settings must not widen {profile!r} allowlist for "
                f"{tool_name!r} after the env allowlist extension"
            )

    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}):
        decision = app.resolve_tool_permission(
            app.GROUNDED_RESEARCH_PROFILE, tool_name, global_settings=hostile
        )
        assert decision == "deny"

    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "search_web") == "allow"
    assert (
        app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "read_url_content") == "allow"
    )


# ---------------------------------------------------------------------------
# AC6: find_credential_like_files() regression after the allowlist extension
# ---------------------------------------------------------------------------


def test_credential_like_file_scan_unaffected_by_env_allowlist_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "real-home-with-secrets"))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")

    real_home = Path(os.environ["HOME"])
    real_home.mkdir(parents=True, exist_ok=True)
    (real_home / ".netrc").write_text("machine example.com login x password y\n")
    (real_home / ".ssh").mkdir(parents=True, exist_ok=True)
    (real_home / ".ssh" / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert app.find_credential_like_files(workspace) == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC7: hermetic integration test -- isolated workspace reaches a mocked
# dbus/keyring endpoint via the propagated DBUS_SESSION_BUS_ADDRESS
# ---------------------------------------------------------------------------


def test_isolated_workspace_reaches_mocked_dbus_secret_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate an existing authenticated session's dbus secret service.

    This does not talk to a real dbus daemon or OS keyring (hermetic /
    mocked, per Issue #1726 In-Scope item 6) -- it binds a fake Unix domain
    socket at the path that `DBUS_SESSION_BUS_ADDRESS` would name, then
    verifies that the env produced by `materialize_isolated_agy_workspace()`
    carries a `DBUS_SESSION_BUS_ADDRESS` value the isolated subprocess could
    use to connect to that same socket -- i.e. reachability is preserved
    through the isolation boundary, without copying any credential value.
    """
    socket_dir = tmp_path / "mock-runtime-dir"
    socket_dir.mkdir(parents=True, exist_ok=True)
    socket_path = socket_dir / "bus"

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    try:
        bus_address = f"unix:path={socket_path}"
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus_address)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_dir))

        workspace = app.materialize_isolated_agy_workspace(
            app.GROUNDED_RESEARCH_PROFILE, auth_profile=app.AGY_AUTH_PROFILE_EXTENDED
        )
        try:
            # env carries the endpoint pointer through the isolation boundary
            assert workspace.env["DBUS_SESSION_BUS_ADDRESS"] == bus_address
            assert workspace.env["XDG_RUNTIME_DIR"] == str(socket_dir)

            # simulate the isolated subprocess using that env value to reach
            # the (mocked) existing authenticated secret-service session
            addr_from_env = workspace.env["DBUS_SESSION_BUS_ADDRESS"]
            assert addr_from_env.startswith("unix:path=")
            resolved_socket_path = addr_from_env[len("unix:path=") :]

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(resolved_socket_path)
                accepted, _ = server.accept()
                try:
                    client.sendall(b"AUTH EXTERNAL mocked-secret-service-handshake\n")
                    received = accepted.recv(1024)
                    assert b"AUTH EXTERNAL" in received
                finally:
                    accepted.close()
            finally:
                client.close()

            # HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME/XDG_STATE_HOME stay isolated
            # even while auth reachability is preserved.
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
    finally:
        server.close()
