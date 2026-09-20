"""Tests for the Issue #1740 / #1743 agy OAuth token file exposure extension
of `materialize_isolated_agy_workspace()` in `agy_permission_policy.py`.

Covers AC1-AC8 (this file's own numbering; distinct from Issue #1743's AC1-9):
- AC1: `$HOME/.gemini/antigravity-cli/antigravity-oauth-token` (when present)
  is exposed read-only under the *isolated* workspace's own `HOME`
  (`<isolated HOME>/.gemini/antigravity-cli/antigravity-oauth-token`) --
  never under `XDG_CONFIG_HOME` (Issue #1743: the real `agy` binary reads
  this file from `$HOME/.gemini/antigravity-cli/`, not from
  `$XDG_CONFIG_HOME`; #1740's original `XDG_CONFIG_HOME`-based placement
  left `agy -p` failing with `agy_auth_required` inside isolated
  workspaces even though the symlink itself was created successfully).
- AC2: when the real token file is absent, exposure is a no-op and workspace
  materialization does not fail.
- AC3: the exposure function never opens/reads the target file's content.
- AC4: only the minimal `.gemini/antigravity-cli/antigravity-oauth-token`
  subpath is exposed -- no other real `$HOME` subdirectory (`.ssh`,
  `.netrc`, other `.gemini/*` state) is reachable.
- AC5: tool deny matrix (hostile_global_settings_fixture) regression after
  the agy OAuth token exposure change.
- AC6: adversarial redaction -- a credential-like value inside the fixture
  token file never appears in the workspace's return value, settings file,
  repr, or `find_credential_like_files()` output (the code never reads file
  content, only checks presence / creates a symlink).
- AC7: `find_credential_like_files()` still detects credential-like basenames
  elsewhere in the workspace, excluding only the intentionally-exposed
  gcloud ADC / agy OAuth token subtrees.
- AC8: hermetic integration test -- the agy OAuth token file is reachable,
  at an existence-check level, from a subprocess whose `HOME` is redirected
  to the isolated workspace's `HOME` -- simulating the file-path resolution
  `agy -p` performs, without invoking the real `agy` binary or reading the
  token's content (Issue #1743 AC7).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading helper (hermetic, no side-effects) -- mirrors the pattern
# used by test_agy_permission_policy.py / test_agy_permission_policy_gcloud_adc.py.
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


@pytest.fixture(autouse=True)
def _force_bwrap_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #1779: this file's tests (#1740/#1743 scope) exercise
    `agy_oauth_token_path` *exposure/reachability* behavior, which is
    unaffected by `auth_profile` or the new `bwrap`-based read-only
    enforcement mode. Several tests below iterate `ALL_PROFILES` /
    `ALL_DENY_PROFILES` (including the security-sensitive `no_tools` /
    `local_asset_research`) against a *fixture* real-home token file; without
    pinning `_bwrap_available()` here, whether `materialize_isolated_agy_workspace()`
    fail-closes (Issue #1779 AC7) would depend on whether the host actually
    has `bwrap` installed, making this file's pre-existing (#1740/#1743)
    assertions environment-dependent by accident. Pinning it `True` keeps
    this file deterministic and focused on its own scope; the fail-closed /
    degraded-mode *selection logic itself* is covered independently by
    `test_agy_permission_policy_readonly_boundary.py`.
    """
    monkeypatch.setattr(app, "_bwrap_available", lambda: True)


@pytest.fixture(autouse=True)
def _clear_agy_oauth_token_handoff_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #2670: this file's pre-existing (#1740/#1743) tests exercise the
    LEGACY `os.environ["HOME"]`-only lookup path (`monkeypatch.setenv("HOME",
    ...)`, no handoff env vars) -- they must stay deterministic regardless of
    whether the actual host process happens to have
    `AGY_OAUTH_TOKEN_HANDOFF_ROOT` / `AGY_OAUTH_TOKEN_HANDOFF_SOURCE` set
    ambiently (e.g. when this suite itself runs inside a
    `scripts/claude-gpt/launch.sh` session). Clearing both here, for every
    test in this file, keeps the pre-existing assertions exercising exactly
    the `no_handoff_ordinary_lookup` branch they were written against; the
    dedicated Issue #2670 tests below set them explicitly per case.
    """
    monkeypatch.delenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", raising=False)
    monkeypatch.delenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", raising=False)


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


def _make_fake_agy_home(tmp_path: Path, *, dirname: str = "real-home", token_content: str = "fake-token-value") -> Path:
    fake_real_home = tmp_path / dirname
    token_dir = fake_real_home / ".gemini" / "antigravity-cli"
    token_dir.mkdir(parents=True)
    (token_dir / "antigravity-oauth-token").write_text(token_content, encoding="utf-8")
    return fake_real_home


# ---------------------------------------------------------------------------
# AC1: agy OAuth token file exposed read-only
# ---------------------------------------------------------------------------


def test_agy_oauth_token_exposed_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.agy_oauth_token_path is not None
            assert workspace.agy_oauth_token_path.is_symlink()
            # existence-check level only -- Path.exists(), never opened/read
            assert workspace.agy_oauth_token_path.exists()
            # Issue #1743 AC1/AC2: exposed under the isolated workspace's own
            # HOME -- this is the path the real `agy` binary reads from.
            assert workspace.agy_oauth_token_path == (
                Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            )
            # Issue #1743 AC1: never placed under XDG_CONFIG_HOME (the
            # pre-#1743 buggy placement `agy` never actually reads from).
            assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli" / "antigravity-oauth-token").exists()
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC2: no-op when the real token file is absent
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_noop_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = tmp_path / "real-home-no-token"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        assert workspace.agy_oauth_token_path is None
        # Issue #1758: the `.gemini/antigravity-cli` dir is now always created
        # (unconditionally, unlike the oauth-token symlink) to hold the
        # explicit-toolPermission settings.json -- but it must contain
        # exactly that file and never the (absent) oauth token.
        antigravity_cli_dir = Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli"
        assert antigravity_cli_dir.exists()
        assert sorted(p.name for p in antigravity_cli_dir.iterdir()) == ["settings.json"]
        assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli").exists()
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_expose_agy_oauth_token_noop_returns_none_directly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = tmp_path / "real-home-no-token-2"
    fake_real_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_real_home))

    isolated_home = tmp_path / "isolated-home-standalone"
    isolated_home.mkdir(parents=True)
    result = app._expose_agy_oauth_token_read_only(isolated_home)
    assert result is None
    assert not (isolated_home / ".gemini" / "antigravity-cli").exists()


# ---------------------------------------------------------------------------
# AC3: the exposure function never opens/reads the target file's content
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_never_reads_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path, token_content="SUPER_SECRET_TOKEN_VALUE_XYZ")
    monkeypatch.setenv("HOME", str(fake_real_home))

    token_file = fake_real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    real_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def _guarded_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file or (self.is_symlink() and self.resolve() == token_file.resolve()):
            raise AssertionError(f"content-access attempted on {self}")
        return real_open(self, *args, **kwargs)

    def _guarded_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file:
            raise AssertionError(f"read_text attempted on {self}")
        return real_read_text(self, *args, **kwargs)

    def _guarded_read_bytes(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self == token_file:
            raise AssertionError(f"read_bytes attempted on {self}")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _guarded_open)
    monkeypatch.setattr(Path, "read_text", _guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", _guarded_read_bytes)

    isolated_home = tmp_path / "isolated-home-guarded"
    isolated_home.mkdir(parents=True)
    result = app._expose_agy_oauth_token_read_only(isolated_home)

    assert result is not None
    assert result.is_symlink()
    assert result == isolated_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


# ---------------------------------------------------------------------------
# AC4: only the minimal antigravity-cli/antigravity-oauth-token subpath is
# exposed, never the full real $HOME or other .gemini state
# ---------------------------------------------------------------------------


def test_expose_agy_oauth_token_minimal_subpath_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    token_dir = fake_real_home / ".gemini" / "antigravity-cli"
    # Other files confirmed present in the real directory (Issue #1740 body)
    # that must NOT be exposed.
    (token_dir / "jetski_state.pbtxt").write_text("state", encoding="utf-8")
    (token_dir / "history.jsonl").write_text("{}", encoding="utf-8")
    (token_dir / "settings.json").write_text("{}", encoding="utf-8")
    (fake_real_home / ".config" / "other-app").mkdir(parents=True)
    (fake_real_home / ".config" / "other-app" / "secret.txt").write_text("nope", encoding="utf-8")
    (fake_real_home / ".ssh").mkdir(parents=True)
    (fake_real_home / ".ssh" / "id_rsa").write_text(
        "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    (fake_real_home / ".netrc").write_text("machine example.com login x password y\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            antigravity_cli_children = sorted(
                p.name for p in (Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli").iterdir()
            )
            # Issue #1758: settings.json (explicit toolPermission) now also
            # lives alongside the oauth token symlink in this directory.
            assert antigravity_cli_children == ["antigravity-oauth-token", "settings.json"]
            assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli").exists()
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert str(fake_real_home) != workspace.env["HOME"]

            all_names = {p.name for p in workspace.workspace_dir.rglob("*")}
            assert "id_rsa" not in all_names
            assert ".netrc" not in all_names
            assert "other-app" not in all_names
            assert "secret.txt" not in all_names
            assert "jetski_state.pbtxt" not in all_names
            assert "history.jsonl" not in all_names
            # "settings.json" from the *real* .gemini/antigravity-cli dir must
            # not appear; the workspace's own .antigravity/settings.json
            # (freshly generated policy doc) is a distinct, expected file.
            # Issue #1758: `<workspace>/.gemini/antigravity-cli/settings.json`
            # now legitimately exists -- but only as freshly generated
            # official settings (toolPermission plus permissions.deny), never
            # a copy of the *real* fake_real_home
            # `.gemini/antigravity-cli/settings.json` (`{}`) written above.
            gemini_settings_paths = [
                p for p in workspace.workspace_dir.rglob("settings.json") if "antigravity-cli" in p.parts
            ]
            assert gemini_settings_paths == [workspace.agy_tool_permission_settings_path]
            assert json.loads(gemini_settings_paths[0].read_text(encoding="utf-8")) == (
                app.build_official_agy_settings(profile)
            )
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC5: tool deny matrix regression after agy OAuth token exposure
# ---------------------------------------------------------------------------


def test_tool_deny_matrix_unaffected_by_agy_oauth_token_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    hostile = app.hostile_global_settings_fixture()
    assert hostile["permissions"]["default"] == "allow"

    for profile in ALL_DENY_PROFILES:
        for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES):
            decision = app.resolve_tool_permission(profile, tool_name, global_settings=hostile)
            assert decision == "deny", (
                f"hostile global settings must not widen {profile!r} allowlist for "
                f"{tool_name!r} after agy OAuth token exposure"
            )

    for tool_name in sorted(app.AGY_DIRECT_TOOL_NAMES - {"search_web", "read_url_content"}):
        decision = app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, tool_name, global_settings=hostile)
        assert decision == "deny"

    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "search_web") == "allow"
    assert app.resolve_tool_permission(app.GROUNDED_RESEARCH_PROFILE, "read_url_content") == "allow"

    # materialize still isolates HOME (and denies via workspace policy) even
    # while the agy OAuth token file is exposed.
    for profile in ALL_DENY_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.env["HOME"] == str(workspace.workspace_dir)
            assert workspace.agy_oauth_token_path is not None
            policy = json.loads(workspace.settings_path.read_text(encoding="utf-8"))
            assert policy["permissions"]["default"] == "deny"
            assert policy["permissions"]["allow"] == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC6: adversarial redaction -- credential-like token value never leaks
# ---------------------------------------------------------------------------


def test_agy_oauth_token_value_never_leaked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_secret = "ya29.FAKE_AGY_OAUTH_SECRET_TOKEN_VALUE_1234567890abcdef"
    fake_real_home = _make_fake_agy_home(tmp_path, token_content=dummy_secret)
    monkeypatch.setenv("HOME", str(fake_real_home))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert dummy_secret not in json.dumps(workspace.env)
            assert dummy_secret not in workspace.settings_path.read_text(encoding="utf-8")
            assert dummy_secret not in str(workspace)
            assert dummy_secret not in repr(workspace)
            assert dummy_secret not in str(workspace.agy_oauth_token_path)
            # find_credential_like_files() only checks basenames -- it never
            # opens/reads file content, so the dummy secret cannot appear
            # here either, and "antigravity-oauth-token" is not itself a
            # credential-*named* basename in CREDENTIAL_FILE_BASENAMES.
            assert app.find_credential_like_files(workspace) == []
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC7: find_credential_like_files() regression -- still detects real
# credential-like basenames elsewhere, excluding only the intentionally
# exposed subtrees (gcloud ADC / agy OAuth token).
# ---------------------------------------------------------------------------


def test_find_credential_like_files_still_detects_unexpected_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        # Simulate an unexpected leak: a credential-like basename appearing
        # somewhere else in the workspace that is NOT the intentionally
        # exposed agy_oauth_token_path / gcloud_adc_path subtree.
        rogue = workspace.workspace_dir / "id_rsa"
        rogue.write_text("not-actually-exposed-intentionally", encoding="utf-8")

        hits = app.find_credential_like_files(workspace)
        assert rogue in hits
        # the intentionally-exposed agy OAuth token path itself must not be
        # flagged.
        assert workspace.agy_oauth_token_path not in hits
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC8: hermetic integration test -- agy OAuth token file reachable at
# existence-check level from inside the isolated workspace
# ---------------------------------------------------------------------------


def test_agy_oauth_token_reachability_integration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        # Issue #1743: `agy` resolves its OAuth token from
        # `$HOME/.gemini/antigravity-cli/antigravity-oauth-token`, matching
        # the state-directory layout its own auth flow writes to on a
        # non-isolated host -- not from `$XDG_CONFIG_HOME`.
        isolated_token_path = Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        assert isolated_token_path.exists()
        assert isolated_token_path.is_symlink()
        # the pre-#1743 (buggy) XDG_CONFIG_HOME placement must be absent.
        assert not (Path(workspace.env["XDG_CONFIG_HOME"]) / "antigravity-cli" / "antigravity-oauth-token").exists()

        # HOME/XDG_CACHE_HOME/XDG_STATE_HOME stay isolated even while agy
        # OAuth token reachability is preserved.
        assert workspace.env["HOME"] == str(workspace.workspace_dir)
        assert workspace.env["XDG_CACHE_HOME"] == str(workspace.workspace_dir / "xdg-cache")
        assert workspace.env["XDG_STATE_HOME"] == str(workspace.workspace_dir / "xdg-state")

        # Issue #1743 AC7: a lightweight `agy -p` smoke-test simulation --
        # run a subprocess with HOME redirected to the isolated workspace's
        # HOME and perform the exact existence-check-level path resolution
        # `agy` performs before authenticating. This never invokes the real
        # `agy` binary and never opens/reads the token file's content (only
        # `Path.is_file()` / `Path.exists()`), matching the diagnosis
        # recorded in Issue #1743's Source section
        # (`LOOP_AGY_ISOLATED_SMOKE_OK` on success).
        smoke_script = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "home = os.environ.get('HOME')\n"
            "token = Path(home) / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token'\n"
            "if token.is_file():\n"
            "    print('LOOP_AGY_ISOLATED_SMOKE_OK')\n"
            "    sys.exit(0)\n"
            "sys.exit(1)  # would surface as agy_auth_required\n"
        )
        smoke_result = subprocess.run(
            [sys.executable, "-c", smoke_script],
            env={**os.environ, "HOME": workspace.env["HOME"]},
            capture_output=True,
            text=True,
            check=False,
        )
        assert smoke_result.returncode == 0, smoke_result.stderr
        assert "LOOP_AGY_ISOLATED_SMOKE_OK" in smoke_result.stdout
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Issue #2670 AC1/AC2/AC4: pre-isolation launcher handoff for the approved
# AGY OAuth token source -- `resolve_agy_oauth_token_source()`'s closed,
# four-member input-state classification, and its integration into
# `materialize_isolated_agy_workspace()`.
# ---------------------------------------------------------------------------


def _make_handoff_fixture(
    tmp_path: Path, *, dirname: str = "handoff-root", token_content: "str | None" = "dummy-fixture-token-value"
) -> tuple[Path, Path]:
    """Return (root, source) -- a fixture `<root>/antigravity-oauth-token`
    layout. *token_content* `None` creates *root* without the source file
    (source_absent fixture)."""
    root = tmp_path / dirname
    root.mkdir(parents=True)
    source = root / "antigravity-oauth-token"
    if token_content is not None:
        source.write_text(token_content, encoding="utf-8")
    return root, source


# --- AC1: wholly absent interface falls back to ordinary lookup (never
#     enumerates an alternate candidate/backend) -----------------------------


def test_resolve_handoff_wholly_absent_interface_permits_ordinary_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = _make_fake_agy_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_real_home))

    result = app.resolve_agy_oauth_token_source()
    assert result.classification == app.AGY_HANDOFF_NO_HANDOFF_ORDINARY_LOOKUP
    assert result.source_path == fake_real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def test_resolve_handoff_wholly_absent_interface_legacy_lookup_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_real_home = tmp_path / "real-home-no-token-legacy"
    fake_real_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_real_home))

    result = app.resolve_agy_oauth_token_source()
    assert result.classification == app.AGY_HANDOFF_NO_HANDOFF_ORDINARY_LOOKUP
    assert result.source_path is None


# --- AC2: partial handoff (exactly one of the two dedicated values) is
#     rejected with no fallback -- never falls back to ordinary lookup ------


@pytest.mark.parametrize(
    "handoff_root,handoff_source",
    [
        pytest.param("/some/root", None, id="root-only"),
        pytest.param(None, "/some/root/antigravity-oauth-token", id="source-only"),
        pytest.param("  ", "/some/root/antigravity-oauth-token", id="root-blank-string"),
    ],
)
def test_resolve_handoff_partial_is_invalid_rejected_no_fallback(
    handoff_root: "str | None", handoff_source: "str | None"
) -> None:
    result = app.resolve_agy_oauth_token_source(handoff_root=handoff_root, handoff_source=handoff_source)
    assert result.classification == app.AGY_HANDOFF_INVALID_REJECTED
    assert result.source_path is None


# --- AC2: complete valid handoff -- exact single-source candidate, takes
#     validated precedence over whatever the ordinary lookup would find ------


def test_resolve_handoff_complete_valid_selects_regular_file(tmp_path: Path) -> None:
    root, source = _make_handoff_fixture(tmp_path)
    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(source))
    assert result.classification == app.AGY_HANDOFF_VALIDATED_SELECTED
    assert result.source_path == source


def test_resolve_handoff_complete_valid_takes_precedence_over_different_ordinary_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DIFFERENT, unrelated `$HOME` fixture is set ambiently -- the
    validated handoff must still be selected (never the ordinary-lookup
    fixture), proving validated precedence (Issue #2670 truth table item
    1)."""
    unrelated_home = _make_fake_agy_home(tmp_path, dirname="unrelated-ordinary-home")
    monkeypatch.setenv("HOME", str(unrelated_home))
    root, source = _make_handoff_fixture(tmp_path, dirname="handoff-root-precedence")

    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(source))
    assert result.classification == app.AGY_HANDOFF_VALIDATED_SELECTED
    assert result.source_path == source
    assert result.source_path != unrelated_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"


def test_resolve_handoff_complete_valid_accepts_in_root_symlink(tmp_path: Path) -> None:
    root, source = _make_handoff_fixture(tmp_path, token_content=None)
    real_target = root / "other-file-in-root"
    real_target.write_text("dummy-fixture-token-value", encoding="utf-8")
    source.symlink_to(real_target)

    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(source))
    assert result.classification == app.AGY_HANDOFF_VALIDATED_SELECTED
    assert result.source_path == source


# --- AC2: launcher-observed exact source absence -- no fallback, SKIP/
#     incomplete for AC5, but not the same classification as a malformed
#     handoff -----------------------------------------------------------------


def test_resolve_handoff_source_absent_when_candidate_entry_missing(tmp_path: Path) -> None:
    root, source = _make_handoff_fixture(tmp_path, token_content=None)
    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(source))
    assert result.classification == app.AGY_HANDOFF_SOURCE_ABSENT
    assert result.source_path is None


# --- AC2: structural/logical mismatch and boundary-escape rejections --------


def test_resolve_handoff_invalid_when_source_is_sibling_subdirectory_same_name(tmp_path: Path) -> None:
    root = tmp_path / "handoff-root-subdir"
    root.mkdir()
    subdir = root / "subdir"
    subdir.mkdir()
    nested_source = subdir / "antigravity-oauth-token"
    nested_source.write_text("dummy-fixture-token-value", encoding="utf-8")

    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(nested_source))
    assert result.classification == app.AGY_HANDOFF_INVALID_REJECTED


def test_resolve_handoff_invalid_when_symlink_escapes_root(tmp_path: Path) -> None:
    root, source = _make_handoff_fixture(tmp_path, dirname="handoff-root-escape", token_content=None)
    outside_dir = tmp_path / "outside-root"
    outside_dir.mkdir()
    outside_file = outside_dir / "escape-target"
    outside_file.write_text("dummy-fixture-token-value", encoding="utf-8")
    source.symlink_to(outside_file)

    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(source))
    assert result.classification == app.AGY_HANDOFF_INVALID_REJECTED
    assert result.source_path is None


def test_resolve_handoff_invalid_when_candidate_entry_is_directory(tmp_path: Path) -> None:
    root = tmp_path / "handoff-root-dir-entry"
    root.mkdir()
    dir_as_source = root / "antigravity-oauth-token"
    dir_as_source.mkdir()

    result = app.resolve_agy_oauth_token_source(handoff_root=str(root), handoff_source=str(dir_as_source))
    assert result.classification == app.AGY_HANDOFF_INVALID_REJECTED


def test_resolve_handoff_invalid_when_paths_are_not_absolute(tmp_path: Path) -> None:
    result = app.resolve_agy_oauth_token_source(
        handoff_root="relative/root", handoff_source="relative/root/antigravity-oauth-token"
    )
    assert result.classification == app.AGY_HANDOFF_INVALID_REJECTED


def test_resolve_handoff_classifications_are_only_the_closed_four(tmp_path: Path) -> None:
    assert app.AGY_HANDOFF_CLASSIFICATIONS == {
        app.AGY_HANDOFF_VALIDATED_SELECTED,
        app.AGY_HANDOFF_INVALID_REJECTED,
        app.AGY_HANDOFF_SOURCE_ABSENT,
        app.AGY_HANDOFF_NO_HANDOFF_ORDINARY_LOOKUP,
    }


# --- AC4: materialize_isolated_agy_workspace() integration -- the resolved
#     classification is recorded on IsolatedAgyWorkspace, and the exposed
#     symlink is built from the handoff-selected candidate under HOME
#     isolation (never the ordinary/ambient HOME lookup once a validated
#     handoff is supplied). ----------------------------------------------------


def test_materialize_workspace_records_validated_handoff_classification_and_exposes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a HOME-isolating caller (like scripts/claude-gpt/launch.sh):
    # the ambient HOME the ordinary lookup would use points at an empty,
    # unrelated directory that never contains the real source -- only the
    # dedicated handoff carries the approved source through.
    isolated_ambient_home = tmp_path / "isolated-ambient-home"
    isolated_ambient_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_ambient_home))

    root, source = _make_handoff_fixture(tmp_path, dirname="handoff-root-materialize")
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", str(root))
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", str(source))

    for profile in ALL_PROFILES:
        workspace = app.materialize_isolated_agy_workspace(profile, parent_dir=tmp_path)
        try:
            assert workspace.agy_oauth_token_handoff_classification == app.AGY_HANDOFF_VALIDATED_SELECTED
            assert workspace.agy_oauth_token_path is not None
            assert workspace.agy_oauth_token_path.is_symlink()
            assert workspace.agy_oauth_token_path.resolve() == source.resolve()
            assert workspace.agy_oauth_token_path == (
                Path(workspace.env["HOME"]) / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
            )
        finally:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_materialize_workspace_records_source_absent_classification_and_no_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated_ambient_home = tmp_path / "isolated-ambient-home-absent"
    isolated_ambient_home.mkdir()
    monkeypatch.setenv("HOME", str(isolated_ambient_home))

    root, source = _make_handoff_fixture(tmp_path, dirname="handoff-root-materialize-absent", token_content=None)
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", str(root))
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", str(source))

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        assert workspace.agy_oauth_token_handoff_classification == app.AGY_HANDOFF_SOURCE_ABSENT
        # source_absent has no fallback -- no exposure at all, even though
        # the (unrelated) ambient ordinary-lookup HOME is also empty here.
        assert workspace.agy_oauth_token_path is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)


def test_materialize_workspace_records_invalid_rejected_classification_and_no_fallback_to_ordinary_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial handoff must fail closed with no fallback -- even when the
    ordinary-lookup ambient HOME genuinely has a valid real token file, that
    ordinary fixture must NOT be used once any handoff value was supplied
    (Issue #2670 truth table item 2: fail closed, no fallback)."""
    fake_real_home_with_real_token = _make_fake_agy_home(tmp_path, dirname="ordinary-home-with-real-token")
    monkeypatch.setenv("HOME", str(fake_real_home_with_real_token))
    monkeypatch.setenv("AGY_OAUTH_TOKEN_HANDOFF_ROOT", str(tmp_path / "partial-root-only"))
    monkeypatch.delenv("AGY_OAUTH_TOKEN_HANDOFF_SOURCE", raising=False)

    workspace = app.materialize_isolated_agy_workspace(app.GROUNDED_RESEARCH_PROFILE, parent_dir=tmp_path)
    try:
        assert workspace.agy_oauth_token_handoff_classification == app.AGY_HANDOFF_INVALID_REJECTED
        assert workspace.agy_oauth_token_path is None
    finally:
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
