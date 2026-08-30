"""scripts/claude-gpt/tests/test_latitude_trace_arrival_live.py

Issue #2426 AC6 (Runtime Verification Applicability: immediate):
live Claude-GPT canary の exact session ID で Latitude trace を取得でき、少なく
とも1つの `llm_request` span の model attribute が launcher-owned GPT model set
に一致することを確認する。この判定は #2375 production collector ではなく本
Issue の live verification helper（`latitude_live_helpers.py`）が行う。

Claude-GPT launcher / Latitude API / ChatGPT Pro Codex subscription 認証の
いずれかが利用不能な場合は `pytest.skip()`（fixture-only PASS を主張しない。
`docs/dev/runtime-verification-policy.md` の SKIP 規約参照）。

marked `claude_live`（`pyproject.toml` 既存 marker。default addopts が
`-m 'not claude_live'` のため通常の全件 pytest 実行では自動的に deselect
される。明示的な live 検証を行う際は `-m claude_live` を付けて実行する）。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

_HELPERS_PATH = Path(__file__).resolve().parent / "latitude_live_helpers.py"
_hspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_live_helpers_2426_trace_arrival", _HELPERS_PATH
)
helpers = importlib.util.module_from_spec(_hspec)
assert _hspec.loader is not None
_hspec.loader.exec_module(helpers)

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_mspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_trace_arrival", _HOOK_PATH
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
    """AC の artifact_requirements（PASS/FAIL の bounded summary のみ。prompt/
    tool IO/API key/raw trace は含めない）。"""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    target = ARTIFACT_DIR / f"{name}-{int(time.time())}.json"
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@pytest.mark.claude_live
def test_live_claude_gpt_trace_arrives_with_gpt_model_attribute():
    """GIVEN 実行環境が利用可能（claude / claude-code-proxy / latitude CLI / 資格情報）
    WHEN non-sensitive canary prompt で Claude-GPT session を1回起動する
    THEN 実 session_id で Latitude trace を取得でき、trace の model attribute が
         launcher-owned GPT model set のいずれかに一致する（AC6）

    P1-4 fix-delta (PR #2439 OWNER REQUEST_CHANGES): SKIP は environment
    preflight にのみ予約する。canary launch を実際に試みた後の非0 exit /
    session_id 未解決は本物の regression でありうるため FAIL する（trace 未着信
    は既存の `assert trace is not None` が既に FAIL 済み）。
    """
    available, reason = helpers.is_environment_available()
    if not available:
        pytest.skip(f"SKIP: environment unavailable ({reason})")

    project_slug = _native_project_slug()
    if not project_slug:
        pytest.skip("SKIP: LATITUDE_PROJECT not configured in Native settings")

    nonce = f"ac6trace{int(time.time())}"
    session_id, proc = helpers.run_claude_gpt_canary(
        "Reply with exactly the single word: PONG", nonce=nonce
    )
    if proc.returncode != 0:
        pytest.fail(
            f"claude-gpt canary launch failed with returncode={proc.returncode}: "
            f"stderr={proc.stderr[-2000:]}"
        )
    if not session_id:
        pytest.fail(
            "claude-gpt canary launch completed (returncode 0) but no session_id "
            "was resolved from the Stop hook-sink -- broken hook-sink wiring "
            "or session correlation, not a SKIP-worthy prerequisite absence"
        )

    trace = helpers.query_latitude_trace_by_session_id(session_id, project_slug)
    summary = {
        "schema": "latitude_trace_arrival_live_summary/v1",
        "ac": "AC6",
        "session_id_opaque": bool(session_id),
        "trace_found": trace is not None,
        "models": trace.get("models") if trace else None,
        "runtime_classification": helpers.classify_runtime(trace.get("models", []))
        if trace
        else None,
    }
    _write_bounded_summary("ac6-trace-arrival", summary)

    assert trace is not None, "no matching Latitude trace found for the live canary session"
    assert trace.get("sessionId") == session_id
    classification = helpers.classify_runtime(trace.get("models", []))
    assert classification == "claude_gpt", (
        f"expected claude_gpt classification, got {classification} "
        f"(models={trace.get('models')})"
    )
