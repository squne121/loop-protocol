"""Tests for `audit_agy_auth_surface.py` (Issue #1778 AC1/AC2/AC6).

Covers:
- AC1: enumerates the 5 auth-reachability surfaces
  `materialize_isolated_agy_workspace()` exposes, against the real
  `agy_permission_policy.py` in this repo.
- AC2: detects `read_only`-named functions whose body never calls an
  OS-level enforcement primitive (P0), including both real-repo functions
  named in the Issue (`_expose_gcloud_adc_read_only` /
  `_expose_agy_oauth_token_read_only`), plus hermetic positive/negative
  fixtures so the detector's logic -- not just this repo's current state --
  is under test.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "audit_agy_auth_surface.py"
)
_REAL_TARGET_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "agy_permission_policy.py"
)
_MODULE_NAME = "audit_agy_auth_surface_1778_test"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT_PATH)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


app = _load_module()


# ---------------------------------------------------------------------------
# AC1: auth surface enumeration against the real repo file
# ---------------------------------------------------------------------------


def test_auth_surface_enumeration_finds_all_five_surfaces() -> None:
    manifest = app.build_manifest(_REAL_TARGET_PATH, "agy_permission_policy.py")
    surface_findings = [
        f for f in manifest["findings"] if f["kind"] == "auth_surface"
    ]
    identifiers = {f["identifier"] for f in surface_findings}
    assert identifiers == {
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "gcloud_adc_path",
        "agy_oauth_token_path",
    }


def test_manifest_schema_field_matches_expected_producer() -> None:
    manifest = app.build_manifest(_REAL_TARGET_PATH, "agy_permission_policy.py")
    assert manifest["schema"] == "AGY_CAUSAL_CLAIM_MANIFEST_V1"
    assert manifest["producer"] == "audit_agy_auth_surface"
    assert manifest["target_files"] == ["agy_permission_policy.py"]


# ---------------------------------------------------------------------------
# AC2: unenforced read_only function detection against the real repo file
# ---------------------------------------------------------------------------


def test_read_only_functions_detected_in_real_repo_file() -> None:
    """AC2: both `_expose_gcloud_adc_read_only` and
    `_expose_agy_oauth_token_read_only` must appear in the detection
    result, run against the actual current agy_permission_policy.py source
    (not a hard-coded expected value -- this proves the detector actually
    fires on the real, currently-unenforced implementation)."""
    manifest = app.build_manifest(_REAL_TARGET_PATH, "agy_permission_policy.py")
    unenforced = {
        f["identifier"]
        for f in manifest["findings"]
        if f["kind"] == "unenforced_read_only"
    }
    assert "_expose_gcloud_adc_read_only" in unenforced
    assert "_expose_agy_oauth_token_read_only" in unenforced
    for finding in manifest["findings"]:
        if finding["kind"] == "unenforced_read_only":
            assert finding["severity"] == "p0"


def test_read_only_function_with_chmod_is_not_flagged(tmp_path: Path) -> None:
    """Negative case (hermetic): a `read_only`-named function whose body
    does call an OS-level enforcement primitive must NOT be flagged."""
    source = '''
from pathlib import Path


def _expose_something_read_only(target: Path) -> None:
    target.symlink_to(Path("/tmp/real"))
    target.chmod(0o444)
'''
    target = tmp_path / "fixture_enforced.py"
    target.write_text(source, encoding="utf-8")
    manifest = app.build_manifest(target, "fixture_enforced.py")
    unenforced = {
        f["identifier"]
        for f in manifest["findings"]
        if f["kind"] == "unenforced_read_only"
    }
    assert "_expose_something_read_only" not in unenforced


def test_read_only_function_without_enforcement_is_flagged(tmp_path: Path) -> None:
    """Positive case (hermetic): a `read_only`-named function whose body
    is only `Path.symlink_to()` must be flagged as P0."""
    source = '''
from pathlib import Path


def _expose_something_read_only(target: Path) -> None:
    target.symlink_to(Path("/tmp/real"))
'''
    target = tmp_path / "fixture_unenforced.py"
    target.write_text(source, encoding="utf-8")
    manifest = app.build_manifest(target, "fixture_unenforced.py")
    findings = [
        f
        for f in manifest["findings"]
        if f["kind"] == "unenforced_read_only"
        and f["identifier"] == "_expose_something_read_only"
    ]
    assert len(findings) == 1
    assert findings[0]["severity"] == "p0"


def test_non_read_only_function_is_never_flagged(tmp_path: Path) -> None:
    source = '''
def _do_something_unrelated() -> None:
    pass
'''
    target = tmp_path / "fixture_unrelated.py"
    target.write_text(source, encoding="utf-8")
    manifest = app.build_manifest(target, "fixture_unrelated.py")
    assert manifest["findings"] == []
    assert manifest["status"] == "ok"


# ---------------------------------------------------------------------------
# AC6: CLI entry point
# ---------------------------------------------------------------------------


def test_main_returns_zero_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = app.main(["--target", str(_REAL_TARGET_PATH)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "AGY_CAUSAL_CLAIM_MANIFEST_V1" in out


def test_main_returns_two_on_missing_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = app.main(["--target", "does/not/exist_1778.py"])
    assert exit_code == 2
