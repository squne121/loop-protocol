"""scripts/claude-gpt/tests/test_latitude_runtime_classifier_live.py

Issue #2426 AC7 (Runtime Verification Applicability: immediate): Native Claude
Code trace と Claude-GPT trace を runtime classifier（本 Issue の live
verification helper）に入力するとそれぞれ `claude_code_native` / `claude_gpt`
に分類され、unknown model は fail-closed で `unknown` になることを確認する。

marked `claude_live`（`-m claude_live` で明示的に opt-in する。default addopts
では deselect される）。
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

_HELPERS_PATH = Path(__file__).resolve().parent / "latitude_live_helpers.py"
_hspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_live_helpers_2426_classifier", _HELPERS_PATH
)
helpers = importlib.util.module_from_spec(_hspec)
assert _hspec.loader is not None
_hspec.loader.exec_module(helpers)

_HOOK_PATH = Path(__file__).resolve().parent.parent / "latitude_hook.py"
_mspec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_hook_module_2426_classifier", _HOOK_PATH
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


def test_unknown_model_classifies_as_unknown_fail_closed():
    """GIVEN launcher-owned GPT model set にも `^claude-` にも一致しない model
    WHEN classify_runtime() を呼ぶ
    THEN "unknown" を返す（`claude_code_native` へ誤って倒れない。AC7 fail-closed）
    """
    assert helpers.classify_runtime(["gpt-4-turbo"]) == "unknown"
    assert helpers.classify_runtime([]) == "unknown"


def test_gpt_model_variants_with_context_hint_suffix_classify_as_claude_gpt():
    """GIVEN `[1m]` context hint suffix 付きの launcher model 名
    WHEN classify_runtime() を呼ぶ
    THEN suffix を除去した上で claude_gpt と判定される（AC7）
    """
    assert helpers.classify_runtime(["gpt-5.6-terra[1m]"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-5.6-sol[1m]"]) == "claude_gpt"
    assert helpers.classify_runtime(["gpt-5.6-luna[1m]"]) == "claude_gpt"


def test_claude_model_classifies_as_claude_code_native():
    """GIVEN `claude-` prefix の model 名
    WHEN classify_runtime() を呼ぶ
    THEN claude_code_native と判定される（AC7）
    """
    assert helpers.classify_runtime(["claude-sonnet-5"]) == "claude_code_native"
    assert helpers.classify_runtime(["claude-haiku-4-5-20251001", "claude-sonnet-5"]) == "claude_code_native"


@pytest.mark.claude_live
def test_live_native_trace_classifies_as_claude_code_native():
    """GIVEN 実行環境が利用可能で、直近の LOCAL Native Claude Code transcript
         file（`~/.claude/projects/**/*.jsonl`）が存在する
    WHEN その transcript のファイル名（= 実 session_id、classify_runtime() を
         一切経由しない独立 provenance。P1-2 fix-delta, PR #2439 OWNER
         REQUEST_CHANGES）で Latitude trace を取得し、その trace の models を
         classify_runtime() へ渡す
    THEN claude_code_native と判定される（AC7: 実 Native trace での確認。
         選別ロジックと検証ロジックが独立しているため、
         `classifier(x) == native implies classifier(x) == native` という
         循環にならない）
    """
    available, reason = helpers.is_environment_available()
    if not available:
        pytest.skip(f"SKIP: environment unavailable ({reason})")
    project_slug = _native_project_slug()
    if not project_slug:
        pytest.skip("SKIP: LATITUDE_PROJECT not configured in Native settings")

    session_id = helpers.find_recent_native_session_id_from_transcripts()
    if not session_id:
        pytest.skip("SKIP: no local Native Claude Code transcript file found")

    trace = helpers.query_latitude_trace_by_session_id(session_id, project_slug)
    if trace is None:
        pytest.skip(
            "SKIP: no matching Latitude trace found for the most recent local "
            "Native transcript session"
        )

    classification = helpers.classify_runtime(trace.get("models", []))
    _write_bounded_summary(
        "ac7-native-classification",
        {
            "schema": "latitude_runtime_classifier_live_summary/v1",
            "ac": "AC7",
            "sample": "native",
            "runtime_classification": classification,
        },
    )
    assert classification == "claude_code_native"


@pytest.mark.claude_live
def test_live_claude_gpt_trace_classifies_as_claude_gpt():
    """GIVEN 実行環境が利用可能
    WHEN non-sensitive canary prompt で Claude-GPT session を1回起動し、その
         exact session ID の trace を取得する
    THEN classify_runtime() が claude_gpt と判定する（AC7: 実 Claude-GPT trace
         での確認。#2375 production collector とは独立した本 Issue 専用 helper）

    P1-4 fix-delta (PR #2439 OWNER REQUEST_CHANGES): SKIP は environment
    preflight（`is_environment_available()` / `LATITUDE_PROJECT` 未設定）にのみ
    予約する。canary launch を実際に試みた後は、非0 exit / session_id 未解決 /
    trace 未着信のいずれも本物の regression でありうるため FAIL する
    （`docs/dev/runtime-verification-policy.md` の SKIP 規約: fixture-only PASS
    と同様、実行済みの失敗を SKIP へ吸収しない）。
    """
    available, reason = helpers.is_environment_available()
    if not available:
        pytest.skip(f"SKIP: environment unavailable ({reason})")
    project_slug = _native_project_slug()
    if not project_slug:
        pytest.skip("SKIP: LATITUDE_PROJECT not configured in Native settings")

    nonce = f"ac7classify{int(time.time())}"
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
    if trace is None:
        pytest.fail(
            f"no matching Latitude trace found for the live canary session "
            f"(session_id={session_id!r}) within the bounded wait window -- "
            f"telemetry did not arrive, not a SKIP-worthy prerequisite absence"
        )

    classification = helpers.classify_runtime(trace.get("models", []))
    _write_bounded_summary(
        "ac7-claude-gpt-classification",
        {
            "schema": "latitude_runtime_classifier_live_summary/v1",
            "ac": "AC7",
            "sample": "claude_gpt",
            "runtime_classification": classification,
        },
    )
    assert classification == "claude_gpt"
