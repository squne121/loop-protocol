"""scripts/claude-gpt/tests/test_proxy_model_compatibility.py

Issue #2801 (updated by Issue #2925): focused tests for the launcher's proxy model catalog
compatibility diagnostic (AC1, AC2, AC3, AC6, AC8, AC9, AC10, AC11 of Issue #2801).

All catalog-compatibility checks in this file are fixture-based (a hermetic
fake `claude-code-proxy` HTTP server bound to a real loopback port, queried by
`launch.sh --check-only`) and never
depend on live network access or a real ChatGPT account/subscription
(Runtime Verification Applicability: fixture-based
catalog compatibility checks for AC1/AC2/AC5/AC6/AC7 do not depend on external
auth -- see Issue #2801 body). Tests derive the "required model set"
dynamically by sourcing `lib.sh` (`claude_gpt_required_model_set`) rather than
hardcoding literal model IDs, so they remain correct across future model policy
changes (Recurrence Prevention).

Issue #2925 changed WHAT is diagnosed: the authority is the running server that
`ANTHROPIC_BASE_URL` points at, not a proxy binary the launcher starts (the
launcher no longer starts any proxy). `CLAUDE_GPT_PROXY_BIN` therefore only
selects the AUXILIARY binary path/version evidence. The connected-server
diagnostics themselves are covered in `test_connected_proxy_diagnostics.py`;
this file keeps the Issue #2801 catalog-compatibility regressions.

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
import re
import stat
import subprocess
from pathlib import Path

import pytest

# Loaded via importlib.util.spec_from_file_location (not a bare `import`) with a name unique to
# this test module, to avoid sys.modules collisions with same-named sibling helper modules under
# the shared repo-wide pytest session.
_HARNESS_PATH = Path(__file__).resolve().parent / "_launcher_harness.py"
_spec = importlib.util.spec_from_file_location("claude_gpt_launcher_harness_2925_compat", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

SCRIPT_DIR = H.SCRIPT_DIR
LAUNCH_SH = H.LAUNCH_SH
LIB_SH = H.LIB_SH

# Mirrors scripts/agent-ops/run_worktree_agent_runtime_smoke.py's
# `_CLAUDE_GPT_LAUNCH_RESULT_RE` / `extract_claude_gpt_launcher_receipt()` WITHOUT importing that
# file. Kept byte-for-byte equivalent so a forward-compat regression there would also be caught
# here (AC9).
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


def _check_only(tmp_path: Path, server_url: str, **env_overrides) -> subprocess.CompletedProcess:
    env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server_url, **env_overrides)
    return H.run_launcher(["--check-only"], env)


def _write_labelled_proxy_bin(path: Path, version_line: str) -> Path:
    path.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version_line}"; fi\nexit 0\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --- AC1 --------------------------------------------------------------------


def test_incompatible_catalog_returns_actionable_failure(tmp_path):
    """GIVEN a fixture server whose live catalog is missing one required model
    (v0.1.36-equivalent incompatible catalog)
    WHEN `launch.sh --check-only` runs
    THEN it exits 7 with the existing top-level `reason: model_alias_not_resolved` plus the
    additive fields: missing model, auxiliary binary path/version, repair command."""
    required = _required_models()
    incompatible_catalog = required[:-1]
    aux_bin = _write_labelled_proxy_bin(tmp_path / "aux-proxy", "claude-code-proxy 0.1.36")
    with H.FakeServer(models=incompatible_catalog) as server:
        result = _check_only(tmp_path, server.url, CLAUDE_GPT_PROXY_BIN=str(aux_bin))
    assert result.returncode == 7, result.stderr

    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None, f"no receipt found in stdout: {result.stdout!r}"
    assert receipt["schema"] == "CLAUDE_GPT_LAUNCH_RESULT_V1"
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "model_alias_not_resolved"
    assert receipt["connected_server"]["missing_models"] == [required[-1]]
    assert sorted(receipt["connected_server"]["required_models"]) == sorted(required)
    assert receipt["local_proxy_binary_auxiliary"]["path"] == str(aux_bin)
    assert receipt["local_proxy_binary_auxiliary"]["version"] == "claude-code-proxy 0.1.36"
    assert receipt["repair_command"] == "scripts/claude-gpt/repair_proxy.sh"


# --- AC2 / AC3 ----------------------------------------------------------------


def test_compatible_catalog_passes(tmp_path):
    required = _required_models()
    with H.FakeServer(models=required) as server:
        result = _check_only(tmp_path, server.url)
    assert result.returncode == 0, result.stderr
    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None and receipt["status"] == "ok"
    assert receipt["connected_server"]["model_catalog_ok"] is True


def test_compatible_catalog_passes_even_with_old_version_label(tmp_path):
    """Version number is NOT the authority (Issue #2801 Design 4): a server whose catalog is
    compatible passes even if the auxiliary PATH binary claims an old version."""
    required = _required_models()
    aux_bin = _write_labelled_proxy_bin(tmp_path / "old-label-proxy", "claude-code-proxy 0.1.30")
    with H.FakeServer(models=required) as server:
        result = _check_only(tmp_path, server.url, CLAUDE_GPT_PROXY_BIN=str(aux_bin))
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["local_proxy_binary_auxiliary"]["version"] == "claude-code-proxy 0.1.30"
    assert receipt["connected_server"]["version"] == "未確認"


def test_catalog_incompatible_not_conflated_with_entitlement(tmp_path):
    """AC3: a catalog mismatch is reported as a local catalog incompatibility only -- never as an
    account entitlement / inference failure."""
    required = _required_models()
    with H.FakeServer(models=required[:-1]) as server:
        result = _check_only(tmp_path, server.url)
    receipt = json.loads(result.stdout)
    assert receipt["cause"] == "connected_server_model_catalog_incomplete"
    blob = json.dumps(receipt).lower()
    for forbidden in ("entitlement", "subscription_not", "not_authenticated", "inference"):
        assert forbidden not in blob, forbidden


def test_explicit_proxy_bin_selects_only_the_auxiliary_evidence(tmp_path):
    """`CLAUDE_GPT_PROXY_BIN` (explicit override) is honored for the auxiliary path/version
    evidence, and never changes which server is diagnosed (that is `ANTHROPIC_BASE_URL`)."""
    required = _required_models()
    explicit = _write_labelled_proxy_bin(tmp_path / "explicit-proxy", "claude-code-proxy 0.1.42")
    with H.FakeServer(models=required) as good, H.FakeServer(models=required[:-1]) as bad:
        ok = _check_only(tmp_path, good.url, CLAUDE_GPT_PROXY_BIN=str(explicit))
        ng = _check_only(tmp_path, bad.url, CLAUDE_GPT_PROXY_BIN=str(explicit))
    assert ok.returncode == 0 and ng.returncode == 7
    for result in (ok, ng):
        assert json.loads(result.stdout)["local_proxy_binary_auxiliary"]["path"] == str(explicit)


# --- AC8 --------------------------------------------------------------------


def test_required_set_derived_from_effective_runtime_consumers():
    """The required set is a one-way derivation from the effective runtime consumers (main /
    small-fast / opus / sonnet / haiku), de-duplicated, with no `[1m]` context hint."""
    required = _required_models()
    assert len(required) == len(set(required))
    assert all("[" not in model for model in required)
    consumers = subprocess.run(
        ["sh", "-c", '. ./lib.sh; printf "%s\\n" "$CLAUDE_GPT_MODEL_MAIN" "$CLAUDE_GPT_MODEL_SMALL_FAST" '
                     '"$CLAUDE_GPT_MODEL_OPUS" "$CLAUDE_GPT_MODEL_SONNET" "$CLAUDE_GPT_MODEL_HAIKU"'],
        cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=10,
    ).stdout.split()
    assert set(required) == {re.sub(r"\[[^\]]*\]$", "", alias) for alias in consumers}
    # on-demand escalation models are not startup-critical.
    assert "gpt-6-astra" not in required


def test_diagnostics_use_the_derived_required_set_not_a_hardcoded_subset():
    lib = LIB_SH.read_text(encoding="utf-8")
    assert "claude_gpt_required_model_set" in lib
    assert "claude_gpt_run_connected_server_diagnostics" in lib
    body = lib.split("claude_gpt_run_connected_server_diagnostics() {", 1)[1].split("\n}\n", 1)[0]
    assert "claude_gpt_required_model_set" in body
    assert "gpt-6-sol" not in body and "gpt-6-luna" not in body


def test_required_set_reflects_a_sonnet_only_drift(tmp_path):
    """If only the sonnet role alias moves to another model, the derived required set (and thus
    the diagnostic against the connected server) follows without editing any hardcoded list."""
    required = _required_models()
    drifted = "gpt-6-drift-probe"
    out = subprocess.run(
        ["sh", "-c", f'. ./lib.sh; CLAUDE_GPT_MODEL_SONNET="{drifted}[1m]"; claude_gpt_required_model_set'],
        cwd=str(SCRIPT_DIR), capture_output=True, text=True, timeout=10,
    ).stdout.split()
    assert drifted in out and set(required) <= set(out)

    drift_lib = tmp_path / "lib-drift.sh"
    drift_lib.write_text(
        LIB_SH.read_text(encoding="utf-8").replace(
            'CLAUDE_GPT_MODEL_SONNET="gpt-6-sol[1m]"', f'CLAUDE_GPT_MODEL_SONNET="{drifted}[1m]"'
        ),
        encoding="utf-8",
    )
    script_copy = tmp_path / "scripts" / "claude-gpt"
    script_copy.mkdir(parents=True)
    (script_copy / "lib.sh").write_text(drift_lib.read_text(encoding="utf-8"), encoding="utf-8")
    (script_copy / "launch.sh").write_text(LAUNCH_SH.read_text(encoding="utf-8"), encoding="utf-8")
    with H.FakeServer(models=required) as server:  # does not list the drifted model
        env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        result = subprocess.run(
            ["sh", str(script_copy / "launch.sh"), "--check-only"], env=env, capture_output=True, text=True, timeout=60
        )
    assert result.returncode == 7
    assert json.loads(result.stdout)["connected_server"]["missing_models"] == [drifted]


# --- AC9 --------------------------------------------------------------------


def test_launcher_receipt_extraction_regex_tolerates_additive_fields(tmp_path):
    """The runner-side single-line receipt regex keeps extracting the failure receipt even though
    additive fields were appended (forward compatibility)."""
    required = _required_models()
    with H.FakeServer(models=required[:-1]) as server:
        result = _check_only(tmp_path, server.url)
    receipt = extract_claude_gpt_launcher_receipt(result.stdout)
    assert receipt is not None
    for key in ("schema", "status", "reason", "cause", "connected_server", "local_proxy_binary_auxiliary",
                "start_hint", "repair_command", "repair_scope"):
        assert key in receipt, key


# --- AC10 / AC11 ----------------------------------------------------------------


def test_diagnostics_are_independent_of_notifier_and_other_ambient_tooling(tmp_path):
    """Nothing but sh/curl is needed to diagnose; unrelated ambient tooling variables do not matter."""
    required = _required_models()
    with H.FakeServer(models=required) as server:
        result = _check_only(tmp_path, server.url, HERDR_ENV="1", NOTIFY_COMMAND="/nonexistent/notifier")
    assert result.returncode == 0, result.stderr


def test_check_only_passes_with_compatible_server(tmp_path):
    """AC11 (fixture-based part): `launch.sh --check-only` PASSes against a compatible server and
    reports the launch env it would use. Real ChatGPT subscription smoke is SKIP (exit 77) /
    environment_blocked when unavailable and is exercised out-of-band."""
    required = _required_models()
    with H.FakeServer(models=required) as server:
        result = _check_only(tmp_path, server.url)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "ok" and receipt["mode"] == "check_only"
    assert receipt["launch_env"]["ANTHROPIC_BASE_URL"] == server.url
    assert receipt["launch_env"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert "CCP_AUTO_REVIEW_MODEL" not in receipt["launch_env"]


@pytest.mark.parametrize("flag", ["--check-only", "--dry-run"])
def test_launcher_options_never_start_or_stop_a_server(tmp_path, flag):
    required = _required_models()
    with H.FakeServer(models=required) as server:
        env = H.base_env(tmp_path, ANTHROPIC_BASE_URL=server.url)
        result = H.run_launcher([flag], env)
        assert result.returncode == 0, result.stderr
        assert server.alive() and server.listening()
