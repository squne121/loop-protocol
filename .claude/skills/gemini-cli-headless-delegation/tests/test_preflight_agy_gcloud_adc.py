"""Tests for the Issue #1730 gcloud ADC auth_mode detection extension of
`preflight_agy.py`'s `_build_auth_diagnostics()`.

Covers AC7: `auth_mode` detects a gcloud ADC file-based auth cache
(`$HOME/.config/gcloud/application_default_credentials.json` /
`access_tokens.db`) even when no D-Bus session bus is present -- the exact
WSL2 fan-out scenario Issue #1730's Source section describes.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "preflight_agy.py"
    spec = importlib.util.spec_from_file_location("preflight_agy", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_fake_gcloud_home(tmp_path: Path) -> Path:
    fake_home = tmp_path / "fake-home"
    gcloud_dir = fake_home / ".config" / "gcloud"
    gcloud_dir.mkdir(parents=True)
    (gcloud_dir / "application_default_credentials.json").write_text(
        json.dumps({"type": "authorized_user"}), encoding="utf-8"
    )
    (gcloud_dir / "access_tokens.db").write_text("fake-sqlite-like-content", encoding="utf-8")
    return fake_home


def test_gcloud_adc_presence_detected_via_filesystem(tmp_path):
    """GIVEN a real $HOME/.config/gcloud with ADC file + token DB present
    WHEN _detect_gcloud_adc is called
    THEN all three presence flags are True, existence-check only."""
    module = load_module()
    fake_home = _make_fake_gcloud_home(tmp_path)

    info = module._detect_gcloud_adc(env_home=str(fake_home))

    assert info["config_dir_present"] is True
    assert info["adc_file_present"] is True
    assert info["access_tokens_db_present"] is True


def test_gcloud_adc_presence_absent_when_no_gcloud_dir(tmp_path):
    module = load_module()
    fake_home = tmp_path / "fake-home-no-gcloud"
    fake_home.mkdir(parents=True)

    info = module._detect_gcloud_adc(env_home=str(fake_home))

    assert info["config_dir_present"] is False
    assert info["adc_file_present"] is False
    assert info["access_tokens_db_present"] is False


def test_auth_mode_detects_gcloud_adc_file_based(tmp_path, monkeypatch):
    """GIVEN WSL2 platform, no D-Bus session bus (keyring unavailable), no
    explicit auth_signal, and smoke_ok is not True
    AND a real $HOME/.config/gcloud ADC cache is present
    WHEN _build_auth_diagnostics is called
    THEN auth_mode is 'gcloud_adc_file_based' (inferred), not
    'unauthenticated' -- reproducing the Issue #1730 Source scenario where
    keyring-based detection alone would have misreported this as
    unauthenticated despite a real, existing gcloud ADC session."""
    module = load_module()
    fake_home = _make_fake_gcloud_home(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = module._build_auth_diagnostics(combined_output="", smoke_ok=None)

    assert result["auth_mode"] == "gcloud_adc_file_based"
    assert result["auth_mode_confidence"] == "inferred"
    assert result["gcloud_adc"]["adc_file_present"] is True
    assert result["gcloud_adc"]["access_tokens_db_present"] is True
    # keyring is still correctly reported unavailable (WSL2, no D-Bus) --
    # gcloud ADC detection augments, it does not hide, the keyring signal.
    assert result["keyring"]["failure_class"] == "system_keyring_unavailable"


def test_auth_mode_falls_back_to_unauthenticated_without_gcloud_adc(tmp_path, monkeypatch):
    """GIVEN the same WSL2/no-D-Bus scenario but no gcloud ADC cache present
    WHEN _build_auth_diagnostics is called
    THEN auth_mode stays 'unauthenticated' (pre-Issue #1730 behavior
    unaffected when there is nothing gcloud-ADC-shaped to detect)."""
    module = load_module()
    fake_home = tmp_path / "fake-home-no-gcloud"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.delenv("AGY_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = module._build_auth_diagnostics(combined_output="", smoke_ok=None)

    assert result["auth_mode"] == "unauthenticated"
    assert result["gcloud_adc"]["adc_file_present"] is False
