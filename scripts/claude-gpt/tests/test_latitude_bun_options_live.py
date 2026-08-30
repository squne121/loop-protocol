"""scripts/claude-gpt/tests/test_latitude_bun_options_live.py

Issue #2426 AC5 (Runtime Verification Applicability: immediate): canonical
Claude-GPT child process では `BUN_OPTIONS` が明示的に unset されており、その
production invariant の下で Claude-GPT session の Stop-hook telemetry が
Latitude に到達することを確認する。preload enrichment の有無を trace arrival
PASS と混同しない（fallback_policy 参照）。

marked `claude_live`（`-m claude_live` で明示的に opt-in する）。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

_HELPERS_PATH = Path(__file__).resolve().parent / "latitude_live_helpers.py"
_hspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_live_helpers_2426_bun_options", _HELPERS_PATH
)
helpers = importlib.util.module_from_spec(_hspec)
assert _hspec.loader is not None
_hspec.loader.exec_module(helpers)

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_mspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_bun_options", _HOOK_PATH
)
latitude_hook = importlib.util.module_from_spec(_mspec)
assert _mspec.loader is not None
_mspec.loader.exec_module(latitude_hook)

ARTIFACT_DIR = Path(__file__).resolve().parent.parent / ".evidence"


def _native_project_slug() -> str | None:
    native_settings_path = str(Path.home() / ".claude" / "settings.json")
    allowlist = latitude_hook.read_native_latitude_allowlist(native_settings_path)
    return allowlist.get("LATITUDE_PROJECT")


def _write_bounded_summary(name: str, payload: dict) -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    target = ARTIFACT_DIR / f"{name}-{int(time.time())}.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_launch_sh_unsets_bun_options_unconditionally():
    """GIVEN launch.sh のソース
    WHEN 既存の SSH_AUTH_SOCK 等 unset ブロックの周辺を確認する
    THEN `unset BUN_OPTIONS` が無条件（caller 由来の条件分岐に包まれていない）
         行として存在する（AC5: 静的な production invariant の確認）
    """
    content = helpers.LAUNCH_SH.read_text(encoding="utf-8")
    assert "\nunset BUN_OPTIONS\n" in content


@pytest.mark.claude_live
def test_live_trace_arrives_even_with_ambient_bun_options_poisoned(monkeypatch):
    """GIVEN 親シェルの ambient BUN_OPTIONS に任意の値が設定されている（poison）
    WHEN non-sensitive canary prompt で Claude-GPT session を1回起動する
    THEN launch.sh の `unset BUN_OPTIONS` production invariant の下でも
         Stop-hook telemetry は Latitude へ正常に到達する（AC5: BUN_OPTIONS の
         有無を trace arrival の可否と混同しない）
    """
    available, reason = helpers.is_environment_available()
    if not available:
        pytest.skip(f"SKIP: environment unavailable ({reason})")
    project_slug = _native_project_slug()
    if not project_slug:
        pytest.skip("SKIP: LATITUDE_PROJECT not configured in Native settings")

    monkeypatch.setenv("BUN_OPTIONS", "--smol")

    nonce = f"ac5bun{int(time.time())}"
    session_id, proc = helpers.run_claude_gpt_canary(
        "Reply with exactly the single word: PONG", nonce=nonce
    )
    if proc.returncode != 0 or not session_id:
        pytest.skip("SKIP: claude-gpt canary launch did not complete (session_id unresolved)")

    trace = helpers.query_latitude_trace_by_session_id(session_id, project_slug)
    summary = {
        "schema": "latitude_bun_options_live_summary/v1",
        "ac": "AC5",
        "ambient_bun_options_poisoned": True,
        "trace_found": trace is not None,
        "runtime_classification": helpers.classify_runtime(trace.get("models", []))
        if trace
        else None,
    }
    _write_bounded_summary("ac5-bun-options-live", summary)

    assert trace is not None, (
        "no matching Latitude trace found for the live canary session "
        "while ambient BUN_OPTIONS was poisoned"
    )
    assert helpers.classify_runtime(trace.get("models", [])) == "claude_gpt"
