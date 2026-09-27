"""scripts/claude-gpt/tests/test_proxy_model_compatibility.py

Issue #2801: focused tests for the launcher's proxy model catalog
compatibility preflight (AC1, AC2, AC3, AC6, AC8, AC9, AC10, AC11).

All catalog-compatibility checks in this file are fixture-based (a hermetic
fake `claude-code-proxy` HTTP server started via `launch.sh --check-only`,
mirroring the pattern already established in
`scripts/claude-gpt/test_launch_transport_policy.py`, Issue #2204) and never
depend on live network access or a real ChatGPT account/subscription
(Runtime Verification Applicability: fixture-based catalog compatibility
checks for AC1/AC2/AC5/AC6/AC7 do not depend on external auth -- see Issue
#2801 body). Tests derive the "required model set" dynamically by sourcing
`lib.sh` (`claude_gpt_required_model_set`) rather than hardcoding literal
model IDs, so they remain correct across future model policy changes
(Recurrence Prevention).

AC11's *real* ChatGPT subscription smoke (bounded real request against a
live compatible proxy) is intentionally NOT exercised here -- per the
Issue's own Runtime Verification Applicability skip_conditions, that portion
is executed manually/out-of-band and is SKIP (exit 77) / environment_blocked
when unavailable. This file only covers the fixture-based
`launch.sh --check-only` PASS path AC11 also requires.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

# Loaded via importlib.util.spec_from_file_location (not a bare `import`) with
# a name unique to this test module, to avoid sys.modules collisions with
# same-named sibling helper modules under the shared repo-wide pytest session
# (see `scripts/claude-gpt/tests/_latitude_check_only_helper.py` consumers for
# the established precedent).
_HELPER_PATH = Path(__file__).resolve().parent / "_proxy_model_compat_fixture_helpers.py"
_helper_spec = importlib.util.spec_from_file_location(
    "claude_gpt_proxy_model_compat_fixture_helpers_2801_compat", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_helper_spec)
assert _helper_spec.loader is not None
_helper_spec.loader.exec_module(_helper)
write_fake_proxy = _helper.write_fake_proxy

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt/
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
LIB_SH = SCRIPT_DIR / "lib.sh"

# Mirrors scripts/agent-ops/run_worktree_agent_runtime_smoke.py:991's
# `_CLAUDE_GPT_LAUNCH_RESULT_RE` / `extract_claude_gpt_launcher_receipt()`
# WITHOUT importing that file (it is outside this Issue's Allowed Paths).
# Kept byte-for-byte equivalent so a forward-compat regression there would
# also be caught here (AC9).
_CLAUDE_GPT_LAUNCH_RESULT_RE = re.compile(r'\{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1"[^\n]*\}')


def extract_claude_gpt_launcher_receipt(text: str) -> dict | None:
    match = _CLAUDE_GPT_LAUNCH_RESULT_RE.search(text or "")
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _required_models() -> list[str]:
    """Source lib.sh in a throwaway subshell and return the live
    `claude_gpt_required_model_set()` output, so fixtures track the actual
    repository-owned model policy instead of hardcoded literals."""
    result = subprocess.run(
        ["sh", "-c", ". ./lib.sh; claude_gpt_required_model_set"],
        cwd=str(SCRIPT_DIR),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    models = [line for line in result.stdout.splitlines() if line]
    assert models, "claude_gpt_required_model_set() returned no models"
    return models


def _run_check_only(
    tmp_path: Path,
    *,
    proxy_models: list[str],
    proxy_version: str,
    extra_env: dict[str, str] | None = None,
    timeout: float = 40.0,
) -> tuple[subprocess.CompletedProcess, Path]:
    claude_gpt_home = tmp_path / "claude-gpt-home"
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)

    fake_proxy = write_fake_proxy(
        tmp_path / "fake-claude-code-proxy", models=proxy_models, version=proxy_version
    )
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    if extra_env:
        env.update(extra_env)

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result, fake_proxy


# --- AC1 --------------------------------------------------------------------


def test_incompatible_catalog_returns_actionable_failure(tmp_path):
    """GIVEN a fixture proxy whose live catalog is missing one required model
    (v0.1.36-equivalent incompatible catalog)
    WHEN launch.sh --check-only runs
    THEN it exits 7 with a structured, actionable failure that names the
    missing model IDs and the selected proxy's identity -- not just a bare
    `model_alias_not_resolved` string.
    """
    required = _required_models()
    incompatible_catalog = required[:-1]  # drop the last required model
    dropped = required[-1]

    result, fake_proxy = _run_check_only(
        tmp_path, proxy_models=incompatible_catalog, proxy_version="claude-code-proxy 0.1.36"
    )
    assert result.returncode == 7, result.stderr

    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None, f"no receipt found in stdout: {result.stdout!r}"
    assert receipt["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "model_alias_not_resolved"
    assert receipt["model_alias_ok"] is False
    assert receipt["cause"] == "proxy_model_catalog_incompatible"
    assert dropped in receipt["missing_models"]
    assert set(receipt["required_models"]) == set(required)
    assert receipt["proxy"]["path"] == str(fake_proxy)
    assert receipt["proxy"]["version"] == "claude-code-proxy 0.1.36"
    assert receipt["minimum_known_compatible_version"] == "0.1.42"
    assert receipt["repair_command"] == "scripts/claude-gpt/repair_proxy.sh"


# --- AC2 --------------------------------------------------------------------


def test_compatible_catalog_passes(tmp_path):
    """GIVEN a fixture proxy whose live catalog has every required model
    (v0.1.42+-equivalent compatible catalog)
    WHEN launch.sh --check-only runs
    THEN compatibility preflight PASSes and the normal launcher path
    continues (exit 0, model_alias_ok true).
    """
    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required, proxy_version="claude-code-proxy 0.1.42"
    )
    assert result.returncode == 0, result.stderr

    # This success receipt embeds preflight.sh's own pretty-printed
    # (multi-line) JSON verbatim under the `preflight` key, so it is not a
    # single-line receipt like the failure path -- parse the whole stdout
    # directly as JSON rather than via the single-line extraction regex
    # (that regex is only exercised against the single-line failure receipt
    # in the AC9 test below, matching real launcher/consumer behavior).
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "ok"
    assert receipt["mode"] == "check_only"
    assert receipt["model_alias_ok"] is True


def test_compatible_catalog_passes_even_with_old_version_label(tmp_path):
    """GIVEN a fixture proxy reporting an OLD version string but whose live
    catalog nonetheless has every required model
    WHEN launch.sh --check-only runs
    THEN it still PASSes -- version number is auxiliary; live catalog
    capability is the authority (Outcome section).
    """
    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required, proxy_version="claude-code-proxy 0.0.1-ancient"
    )
    assert result.returncode == 0, result.stderr


# --- AC3 ---------------------------------------------------------------------


def test_catalog_incompatible_not_conflated_with_entitlement(tmp_path):
    """GIVEN an incompatible catalog fixture
    WHEN launch.sh --check-only runs
    THEN the failure `cause` is exactly `proxy_model_catalog_incompatible`
    and no entitlement/auth/quota-style classification leaks into the
    receipt -- local catalog incompatibility must never be conflated with
    ChatGPT account entitlement/runtime rejection (Design section 4).
    """
    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required[:-1], proxy_version="claude-code-proxy 0.1.36"
    )
    assert result.returncode == 7, result.stderr

    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None
    assert receipt["cause"] == "proxy_model_catalog_incompatible"

    forbidden_tokens = ["entitlement", "quota", "account_rejected", "auth_denied"]
    raw = json.dumps(receipt).lower()
    for token in forbidden_tokens:
        assert token not in raw, f"unexpected entitlement-style token {token!r} in receipt"


# --- AC6 ----------------------------------------------------------------------


def test_explicit_proxy_bin_precedence_preserved(tmp_path):
    """GIVEN an explicit CLAUDE_GPT_PROXY_BIN pointing at an INCOMPATIBLE
    proxy, while PATH *and* the Claude-GPT-owned managed install location
    (`$CLAUDE_GPT_HOME/bin`, fix_delta F3's second-precedence candidate) both
    expose a different, COMPATIBLE `claude-code-proxy` binary
    WHEN launch.sh --check-only runs
    THEN the launcher does not silently fall back to either the managed
    binary or the PATH binary -- it reports the explicit binary's own
    path/version and its missing models (binary precedence: explicit
    CLAUDE_GPT_PROXY_BIN always wins over every other candidate, even an
    otherwise-compatible one; Design section 3). This also regression-guards
    fix_delta F3 (home_bin_dir precedence) against silently overriding an
    explicit operator override -- not just against the PATH candidate.
    """
    required = _required_models()

    claude_gpt_home = tmp_path / "claude-gpt-home"

    # PATH candidate: compatible, but must NOT be selected.
    path_bin_dir = tmp_path / "path-bin"
    path_bin_dir.mkdir()
    path_proxy = write_fake_proxy(
        path_bin_dir / "claude-code-proxy", models=required, version="claude-code-proxy 0.1.42"
    )

    # Managed (`$CLAUDE_GPT_HOME/bin`) candidate: also compatible, also must
    # NOT be selected (fix_delta F3 regression guard).
    managed_bin_dir = claude_gpt_home / "bin"
    managed_bin_dir.mkdir(parents=True)
    managed_proxy = write_fake_proxy(
        managed_bin_dir / "claude-code-proxy", models=required, version="claude-code-proxy 0.1.42"
    )

    # Explicit candidate: incompatible, must be the one actually used.
    explicit_proxy = write_fake_proxy(
        tmp_path / "explicit-incompatible-proxy",
        models=required[:-1],
        version="claude-code-proxy 0.1.30",
    )

    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(claude_gpt_home)
    env["CLAUDE_GPT_PROXY_BIN"] = str(explicit_proxy)
    env["PATH"] = f"{path_bin_dir}{os.pathsep}{env['PATH']}"
    env.pop("CLAUDE_GPT_CLAUDE_BIN", None)

    result = subprocess.run(
        [str(LAUNCH_SH), "--check-only"],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 7, result.stderr

    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None
    assert receipt["proxy"]["path"] == str(explicit_proxy)
    assert receipt["proxy"]["path"] != str(path_proxy)
    assert receipt["proxy"]["path"] != str(managed_proxy)
    assert receipt["proxy"]["version"] == "claude-code-proxy 0.1.30"
    assert receipt["missing_models"]  # non-empty: explicit binary really is incompatible


# --- AC8 -----------------------------------------------------------------------


def test_required_set_derived_from_effective_runtime_consumers():
    """GIVEN lib.sh's claude_gpt_required_model_set()
    WHEN its body is inspected
    THEN it derives from all five effective runtime consumers (main / opus /
    sonnet / haiku / Auto review classifier) rather than a hand-picked
    MAIN/OPUS/HAIKU subset (AC8).
    """
    content = LIB_SH.read_text(encoding="utf-8")
    start = content.index("claude_gpt_required_model_set() {")
    end = content.index("\n}", start)
    body = content[start:end]
    for consumer_const in (
        "CLAUDE_GPT_MODEL_MAIN",
        "CLAUDE_GPT_MODEL_OPUS",
        "CLAUDE_GPT_MODEL_SONNET",
        "CLAUDE_GPT_MODEL_HAIKU",
        "CLAUDE_GPT_AUTO_REVIEW_MODEL_POLICY",
    ):
        assert consumer_const in body, f"{consumer_const} missing from required-set derivation"


def test_launch_sh_model_check_loop_uses_derived_required_set_not_hardcoded_subset():
    """GIVEN launch.sh's live catalog compatibility loop
    WHEN its content is inspected
    THEN it iterates over `claude_gpt_required_model_set()` output, not a
    hardcoded literal `MAIN`/`OPUS`/`HAIKU` enumeration (recurrence
    prevention: a future SONNET-only or Auto-review-only model change must
    still be checked).
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    assert "claude_gpt_required_model_set" in content
    old_hardcoded_enum = (
        'for m in "$CLAUDE_GPT_MODEL_MAIN" "$CLAUDE_GPT_MODEL_OPUS" "$CLAUDE_GPT_MODEL_HAIKU"'
    )
    assert old_hardcoded_enum not in content


def test_required_set_reflects_a_sonnet_only_drift(tmp_path):
    """GIVEN a copy of lib.sh where ONLY CLAUDE_GPT_MODEL_SONNET is changed to
    a distinct model ID (simulating a future policy change touching just the
    Auto-review/Sonnet consumer)
    WHEN claude_gpt_required_model_set() is sourced from the patched copy
    THEN the new distinct model ID appears in the derived required set --
    proving the one-directional derivation actually reacts to a
    SONNET-only change instead of silently keeping a stale fixed subset
    (AC8 recurrence prevention).
    """
    patched_dir = tmp_path / "patched-claude-gpt"
    shutil.copytree(SCRIPT_DIR, patched_dir, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    patched_lib = patched_dir / "lib.sh"
    original = patched_lib.read_text(encoding="utf-8")

    # Extract the *current* `CLAUDE_GPT_MODEL_SONNET="..."` assignment line
    # itself, rather than hardcoding a specific generation's baseline model
    # name (that hardcoded literal breaks every time the shared model policy
    # rotates -- e.g. Issue #2772/#2800 -- even though this test's actual
    # subject, the one-directional derivation, is untouched by that rotation).
    # If the assignment line can't be found at all, that is itself a policy
    # drift this test must not silently swallow -- fail loudly instead of
    # skipping.
    sonnet_line_match = re.search(
        r'^CLAUDE_GPT_MODEL_SONNET="[^"]*"$', original, flags=re.MULTILINE
    )
    assert sonnet_line_match is not None, (
        "CLAUDE_GPT_MODEL_SONNET assignment line not found in lib.sh -- "
        "policy drift the recurrence test must not silently ignore"
    )
    original_sonnet_line = sonnet_line_match.group(0)
    patched_sonnet_line = 'CLAUDE_GPT_MODEL_SONNET="gpt-9.9-drift-sentinel[1m]"'
    assert patched_sonnet_line != original_sonnet_line

    patched = original.replace(original_sonnet_line, patched_sonnet_line, 1)
    assert patched != original
    patched_lib.write_text(patched, encoding="utf-8")
    patched_lib.chmod(patched_lib.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        ["sh", "-c", ". ./lib.sh; claude_gpt_required_model_set"],
        cwd=str(patched_dir),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "gpt-9.9-drift-sentinel" in result.stdout.splitlines()


# --- AC9 ------------------------------------------------------------------------


def test_launcher_receipt_extraction_regex_tolerates_additive_fields(tmp_path):
    """GIVEN the launcher's own additive-field failure receipt (nested
    `proxy` object, new `cause`/`required_models`/`missing_models`/
    `minimum_known_compatible_version`/`repair_command` fields), embedded in
    a larger multi-line stdout+stderr blob (simulating a real subprocess
    capture with other lines around it)
    WHEN the same extraction regex as
    `scripts/agent-ops/run_worktree_agent_runtime_smoke.py`'s
    `extract_claude_gpt_launcher_receipt()` is applied
    THEN it still extracts the receipt and the existing `reason` /
    `model_alias_ok` / `schema` semantics survive unbroken (AC9). The
    Allowed-Paths-excluded consumer file itself is not imported or modified.
    """
    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required[:-1], proxy_version="claude-code-proxy 0.1.36"
    )
    assert result.returncode == 7, result.stderr

    noisy_blob = (
        "launcher=/some/path/launch.sh git=abc1234 dirty=false proxy=claude-code-proxy 0.1.36\n"
        + result.stdout
        + "\nCLAUDE_GPT_PROXY_PORT=12345\n"
    )

    receipt = extract_claude_gpt_launcher_receipt(noisy_blob)
    assert receipt is not None, "regex failed to extract receipt from noisy multi-line blob"
    assert receipt["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert receipt["reason"] == "model_alias_not_resolved"
    assert receipt["model_alias_ok"] is False
    assert isinstance(receipt["proxy"], dict)
    assert "path" in receipt["proxy"] and "version" in receipt["proxy"]
    assert isinstance(receipt["required_models"], list)
    assert isinstance(receipt["missing_models"], list)
    assert receipt["minimum_known_compatible_version"] == "0.1.42"
    assert receipt["repair_command"] == "scripts/claude-gpt/repair_proxy.sh"


# --- AC10 -----------------------------------------------------------------------


def test_preflight_independent_of_notifier_availability(tmp_path):
    """GIVEN lib.sh / launch.sh
    WHEN their source is inspected for any coupling to the #2773
    upstream-update notifier
    THEN no reference exists -- and a compatible-catalog fixture run PASSes
    regardless (AC10: preflight/repair guidance must function independently
    of notifier availability/staleness/offline state).
    """
    lib_content = LIB_SH.read_text(encoding="utf-8")
    launch_content = LAUNCH_SH.read_text(encoding="utf-8")
    for forbidden in ("notifier", "notify_upstream", "2773"):
        assert forbidden not in lib_content.lower()
        assert forbidden not in launch_content.lower()

    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required, proxy_version="claude-code-proxy 0.1.42"
    )
    assert result.returncode == 0, result.stderr


# --- AC11 (fixture-based check-only portion only; see module docstring) --------


def test_check_only_passes_with_compatible_proxy(tmp_path):
    """GIVEN a fixture proxy at the known-compatible version
    (`CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION`) whose live catalog has
    every current-head required model
    WHEN launch.sh --check-only runs
    THEN it PASSes without any live network/ChatGPT-auth dependency (AC11
    fixture-based portion; the bounded real-subscription smoke is a separate,
    optionally-SKIPped manual step per Runtime Verification Applicability).
    """
    required = _required_models()
    result, _ = _run_check_only(
        tmp_path, proxy_models=required, proxy_version="claude-code-proxy 0.1.42"
    )
    assert result.returncode == 0, result.stderr
    # See test_compatible_catalog_passes() for why this parses the raw
    # stdout directly instead of via the single-line extraction regex.
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "ok"
    assert receipt["model_alias_ok"] is True


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
