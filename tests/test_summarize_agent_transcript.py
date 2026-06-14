"""
tests/test_summarize_agent_transcript.py

summarize_agent_transcript.py のテスト。

fixture 設計方針 (input_source_policy):
  - hook_input: SubAgent の hook_event_name / session_id / cwd 等のフィールド
  - fixture_observed: fixture JSONL から実際に観測されたフィールドのみ昇格
  - trusted_field: schema metric として使用する信頼済みフィールド

AC カバレッジ:
  AC1: Claude transcript -> AGENT_SESSION_HOTSPOTS_V1 生成
  AC2: Codex transcript でも同様
  AC3: 欠落 metadata -> {"availability":"unknown","value":null} wrapper
  AC4: raw transcript 本文が stdout に含まれず、redaction 動作
  AC5/AC9: schema フィールド確認 (docs 側は別途)
  AC10: hook_input / fixture_observed / trusted_field の区別
  AC11: no_raw / raw_body / artifact_privacy キーワード
  AC12: ordering / concurrent / timestamp キーワード
"""

from __future__ import annotations

import json
import sys
import os
from io import StringIO
from pathlib import Path
from unittest.mock import patch
import pytest

# スクリプトを import するためにパスを追加
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import summarize_agent_transcript as sat

# fixture パス
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE_FIXTURE = FIXTURES_DIR / "claude-sample.jsonl"
CODEX_FIXTURE = FIXTURES_DIR / "codex-sample.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_main_capture(args: list[str]) -> tuple[int, str]:
    """main() を実行して (exit_code, stdout) を返す。"""
    buf = StringIO()
    with patch("sys.stdout", buf):
        code = sat.main(args)
    return code, buf.getvalue()


# ---------------------------------------------------------------------------
# AC1: Claude transcript -> AGENT_SESSION_HOTSPOTS_V1 artifact
# ---------------------------------------------------------------------------


class TestClaudeTranscript:
    """
    hook_input_schema.claude.trusted_fields: session_id, transcript_path, version, model
    transcript_jsonl.policy: fixture_observed_only
    """

    def test_artifact_schema_generated(self, tmp_path: Path) -> None:
        """AC1: Claude transcript から AGENT_SESSION_HOTSPOTS_V1 が生成される。"""
        # trusted_field: transcript_path は fixture_observed フィールドを使用
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        # exit code 0 (pass)
        assert exit_code == 0, f"exit code should be 0, got {exit_code}; stdout={stdout}"

        # STATUS が 0
        assert "STATUS: 0" in stdout

        # ARTIFACT 行が存在する
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is not None, "ARTIFACT: line not found in stdout"
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())

        # artifact JSON が存在して読める
        assert artifact_path.exists(), f"Artifact file not found: {artifact_path}"
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        # schema フィールド確認 (AC9)
        assert artifact["schema"] == "AGENT_SESSION_HOTSPOTS_V1"
        assert "generated_at" in artifact
        assert "producer" in artifact
        assert artifact["producer"]["script"] == "scripts/summarize_agent_transcript.py"
        assert "input_refs" in artifact
        assert "privacy" in artifact
        assert "metrics" in artifact
        assert artifact["privacy"]["raw_transcript_included"] is False

    def test_tool_metadata_extracted(self) -> None:
        """trusted_field: tool / version / model は session_start から抽出される。"""
        result, warnings = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        metrics = result["metrics"]

        # fixture_observed: claude-sample.jsonl の session_start に tool フィールドあり
        assert metrics["tool"]["name"] == "claude"
        assert metrics["tool"]["version"] == "claude-sonnet-4-6"
        assert metrics["model"]["name"] == "claude-sonnet-4-6"
        assert metrics["model"]["reasoning_effort"] == "high"

    def test_subagents_counted(self) -> None:
        """trusted_field: subagent_spawn イベント数を集計する。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        # fixture に subagent_spawn 2行あり
        assert result["metrics"]["subagents"]["spawned_count"] == 2

    def test_failed_commands_counted(self) -> None:
        """trusted_field: exit_code != 0 の tool_use を failed command として計上。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        # fixture に exit_code=1 が 2行あり
        assert result["metrics"]["commands"]["failed_count"] == 2

    def test_repeated_reads_detected(self) -> None:
        """trusted_field: 同一 file_path への複数 Read は repeated_read_count に反映。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        # fixture に README.md の Read が 2回あり -> repeated_read_count = 1
        assert result["metrics"]["reads"]["repeated_read_count"] == 1

    def test_hooks_counted(self) -> None:
        """trusted_field: hook_fired の result (allowed/blocked/skipped) 別集計。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        hooks = result["metrics"]["hooks"]
        assert hooks["fired_count"] == 1     # allowed
        assert hooks["blocked_count"] == 1   # blocked
        assert hooks["skipped_count"] == 1   # skipped

    def test_compaction_marker_seen(self) -> None:
        """trusted_field: compaction_marker イベントが存在する場合は True。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert result["metrics"]["compaction"]["marker_seen"] is True

    def test_human_intervention_counted(self) -> None:
        """trusted_field: human_intervention イベント数を集計。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert result["metrics"]["human_intervention"]["count"] == 1

    def test_token_usage_extracted(self) -> None:
        """trusted_field: token_usage イベントから prompt/completion/total を抽出。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        tokens = result["metrics"]["tokens"]
        assert tokens["prompt"] == 15000
        assert tokens["completion"] == 3000
        assert tokens["total"] == 18000


# ---------------------------------------------------------------------------
# AC2: Codex transcript でも AGENT_SESSION_HOTSPOTS_V1 が生成される
# ---------------------------------------------------------------------------


class TestCodexTranscript:
    """
    hook_input_schema.codex.trusted_fields: turn_id, tool_name, agent_transcript_path
    transcript_jsonl.policy: fixture_observed_only
    """

    def test_artifact_schema_generated_codex(self) -> None:
        """AC2: Codex transcript から AGENT_SESSION_HOTSPOTS_V1 が生成される。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CODEX_FIXTURE)]
        )
        assert exit_code == 0, f"exit code should be 0, got {exit_code}; stdout={stdout}"
        assert "STATUS: 0" in stdout

        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is not None
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
        assert artifact_path.exists()

        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        assert artifact["schema"] == "AGENT_SESSION_HOTSPOTS_V1"

    def test_codex_tool_extracted(self) -> None:
        """fixture_observed: codex fixture の session_start に tool=codex フィールドあり。"""
        result, _ = sat.parse_transcript(CODEX_FIXTURE, redact=False)
        assert result["metrics"]["tool"]["name"] == "codex"

    def test_codex_failed_command(self) -> None:
        """fixture_observed: exit_code=1 のコマンドが codex fixture に 1行ある。"""
        result, _ = sat.parse_transcript(CODEX_FIXTURE, redact=False)
        assert result["metrics"]["commands"]["failed_count"] == 1


# ---------------------------------------------------------------------------
# AC3: 欠落 metadata は {"availability":"unknown","value":null} wrapper
# ---------------------------------------------------------------------------


class TestUnknownMetadata:
    """AC3: 欠落フィールドは UNKNOWN wrapper として明示される。"""

    def test_unknown_wrapper_for_missing_tokens(self, tmp_path: Path) -> None:
        """token_usage イベントがない場合は tokens フィールドが UNKNOWN になる。"""
        # token_usage なしの最小 fixture
        minimal_fixture = tmp_path / "minimal.jsonl"
        minimal_fixture.write_text(
            '{"type":"session_start","tool":"test","model":"test-model","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )

        result, _ = sat.parse_transcript(minimal_fixture, redact=False)
        tokens = result["metrics"]["tokens"]

        # token_usage がないため UNKNOWN wrapper
        assert tokens["prompt"] == {"availability": "unknown", "value": None}
        assert tokens["completion"] == {"availability": "unknown", "value": None}
        assert tokens["total"] == {"availability": "unknown", "value": None}

    def test_unknown_wrapper_for_missing_reasoning_effort(self, tmp_path: Path) -> None:
        """reasoning_effort がない場合は UNKNOWN wrapper になる。"""
        fixture = tmp_path / "no_reasoning.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"claude","model":"claude-opus","timestamp":"2026-01-01T00:00:00Z"}\n',
            encoding="utf-8",
        )

        result, _ = sat.parse_transcript(fixture, redact=False)
        assert result["metrics"]["model"]["reasoning_effort"] == {
            "availability": "unknown",
            "value": None,
        }

    def test_unknown_const_value(self) -> None:
        """UNKNOWN は {"availability":"unknown","value":null} であることを確認。"""
        assert sat.UNKNOWN == {"availability": "unknown", "value": None}


# ---------------------------------------------------------------------------
# AC4: raw transcript 本文が stdout に含まれない / redaction 動作
# (no_raw, raw_body, artifact_privacy キーワード対応 — AC11 も兼ねる)
# ---------------------------------------------------------------------------


class TestNoRawAndRedaction:
    """AC4/AC11: raw transcript が stdout に出ない、redaction が動作する。"""

    def test_no_raw_transcript_in_stdout(self) -> None:
        """no_raw: stdout に transcript の生の行が含まれない。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        # fixture の raw 行が stdout に含まれないことを確認
        assert '"type":"session_start"' not in stdout
        assert '"session_id"' not in stdout

    def test_raw_body_not_in_artifact(self) -> None:
        """raw_body: artifact JSON に raw transcript 行が保存されない。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is not None
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())

        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        # artifact_privacy: raw_transcript_included は False
        assert artifact["privacy"]["raw_transcript_included"] is False
        # artifact 全体に raw JSONL 行が含まれないことを確認
        artifact_str = json.dumps(artifact)
        assert '"type":"session_start"' not in artifact_str

    def test_artifact_privacy_field_structure(self) -> None:
        """artifact_privacy: privacy フィールドが schema を満たす。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        privacy = artifact["privacy"]
        assert "raw_transcript_included" in privacy
        assert "redaction_enabled" in privacy
        assert "public_projection_safe" in privacy
        assert privacy["raw_transcript_included"] is False

    def test_redact_flag_masks_paths(self, tmp_path: Path) -> None:
        """--redact フラグで absolute path が <PATH> に置換される。"""
        fixture = tmp_path / "redact-test.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"claude","model":"test","timestamp":"2026-01-01T00:00:00Z"}\n',
            encoding="utf-8",
        )

        exit_code, stdout = run_main_capture(
            ["--transcript", str(fixture), "--redact"]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is not None
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())

        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        # redact enabled の場合は public_projection_safe: true
        assert artifact["privacy"]["redaction_enabled"] is True
        assert artifact["privacy"]["public_projection_safe"] is True

        # input_refs.transcript_path.value は redact されている (absolute path -> <PATH>)
        t_value = artifact["input_refs"]["transcript_path"]["value"]
        # PATH が含まれているか、または <PATH> に変換されている
        assert "<PATH>" in t_value or not t_value.startswith("/")

    def test_github_token_redacted(self) -> None:
        """redact: GitHub token パターンが <GITHUB_TOKEN> に置換される。"""
        text = "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456789"
        result = sat.redact_string(text)
        assert "ghp_" not in result
        assert "<GITHUB_TOKEN>" in result

    def test_openai_key_redacted(self) -> None:
        """redact: OpenAI key パターンが <OPENAI_KEY> に置換される。"""
        text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyzABCDEFGH"
        result = sat.redact_string(text)
        assert "sk-" not in result
        assert "<OPENAI_KEY>" in result


# ---------------------------------------------------------------------------
# AC7: artifact が tmp/agent-session-hotspots/ に書き込まれる
# ---------------------------------------------------------------------------


class TestArtifactOutput:
    """AC7: artifact 出力先が tmp/agent-session-hotspots/ であることを確認。"""

    def test_artifact_written_to_tmp_dir(self) -> None:
        """artifact は tmp/agent-session-hotspots/ 以下に生成される。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is not None
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())

        assert "tmp/agent-session-hotspots" in str(artifact_path) or \
               str(artifact_path).startswith("tmp/agent-session-hotspots"), \
               f"Unexpected artifact path: {artifact_path}"


# ---------------------------------------------------------------------------
# exit code テスト
# ---------------------------------------------------------------------------


class TestExitCodes:
    """exit code 規約のテスト。"""

    def test_missing_transcript_returns_2(self) -> None:
        """missing_input: 存在しない transcript は exit code 2 を返す。"""
        exit_code, _ = run_main_capture(
            ["--transcript", "/nonexistent/path/to/transcript.jsonl"]
        )
        assert exit_code == 2

    def test_warn_exit_code_on_parse_warnings(self, tmp_path: Path) -> None:
        """warn: parser_warnings がある場合は exit code 1 を返す。"""
        # 1行が不正な JSONL
        bad_fixture = tmp_path / "bad.jsonl"
        bad_fixture.write_text(
            '{"type":"session_start","tool":"test","timestamp":"2026-01-01T00:00:00Z"}\n'
            "THIS IS NOT JSON\n",
            encoding="utf-8",
        )
        exit_code, stdout = run_main_capture(["--transcript", str(bad_fixture)])
        assert exit_code == 1
        assert "STATUS: 1" in stdout


# ---------------------------------------------------------------------------
# AC10: hook_input / fixture_observed / trusted_field の区別
# ---------------------------------------------------------------------------


class TestInputSourcePolicy:
    """AC10: fixture 設計方針 hook_input / fixture_observed / trusted_field の区別。"""

    def test_fixture_observed_session_id_not_promoted(self) -> None:
        """
        hook_input: session_id は hook_event_name などの hook 入力フィールドであり、
        fixture_observed_only ポリシー下では metrics に昇格しない。
        """
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        metrics = result["metrics"]
        # session_id は hook_input フィールド — metrics に昇格されない
        assert "session_id" not in metrics

    def test_trusted_field_tool_name_present(self) -> None:
        """trusted_field: tool.name は session_start の fixture_observed フィールド。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert "name" in result["metrics"]["tool"]

    def test_unknown_event_types_counted_not_promoted(self, tmp_path: Path) -> None:
        """
        fixture_observed_only: 未知の event type は event_counts に記録されるが
        schema metrics には昇格されない。
        """
        fixture = tmp_path / "unknown-events.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"test","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"UNKNOWN_FUTURE_EVENT_TYPE","data":"something","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )
        result, warnings = sat.parse_transcript(fixture, redact=False)
        # 未知 event は event_counts に記録
        assert "UNKNOWN_FUTURE_EVENT_TYPE" in result["event_counts"]
        # metrics に昇格されていない
        assert "UNKNOWN_FUTURE_EVENT_TYPE" not in result["metrics"]

    def test_codex_turn_id_is_hook_input_not_metric(self) -> None:
        """
        hook_input: codex の turn_id は hook_input フィールド。
        fixture_observed_only ポリシーで metrics に昇格しない。
        """
        result, _ = sat.parse_transcript(CODEX_FIXTURE, redact=False)
        assert "turn_id" not in result["metrics"]


# ---------------------------------------------------------------------------
# AC12: ordering / concurrent / timestamp キーワード対応
# ---------------------------------------------------------------------------


class TestOrderingAndTimestamp:
    """AC12: ordering / concurrent / timestamp に関するテスト。"""

    def test_ordering_event_counts_by_type(self) -> None:
        """ordering: event_counts は type 別集計（順序は insert 順）。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        event_counts = result["event_counts"]
        # session_start と session_end が 1 ずつ
        assert event_counts.get("session_start") == 1
        assert event_counts.get("session_end") == 1

    def test_timestamp_field_in_artifact(self) -> None:
        """timestamp: artifact の generated_at は ISO 8601 形式。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        generated_at = artifact["generated_at"]
        # ISO 8601 形式であることを確認
        from datetime import datetime
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        assert dt is not None

    def test_concurrent_subagent_counts_accumulated(self, tmp_path: Path) -> None:
        """
        concurrent: 複数の subagent_spawn は spawned_count に累積される。
        concurrent spawn は区別しないが count は正確。
        """
        fixture = tmp_path / "concurrent.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"claude","model":"m","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"subagent_spawn","subagent_id":"s1","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"subagent_spawn","subagent_id":"s2","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"subagent_spawn","subagent_id":"s3","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )
        result, _ = sat.parse_transcript(fixture, redact=False)
        # concurrent spawn 3つ
        assert result["metrics"]["subagents"]["spawned_count"] == 3


# ---------------------------------------------------------------------------
# AC9 schema フィールド確認 (補足テスト)
# ---------------------------------------------------------------------------


class TestSchemaFields:
    """AC9: artifact に必須フィールドが全て含まれる。"""

    def test_all_required_fields_present(self) -> None:
        """artifact に AGENT_SESSION_HOTSPOTS_V1 / generated_at / producer /
        input_refs / privacy / metrics が全て含まれる。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
        with open(artifact_path, encoding="utf-8") as f:
            artifact = json.load(f)

        required_keys = [
            "schema",
            "generated_at",
            "producer",
            "input_refs",
            "privacy",
            "metrics",
        ]
        for key in required_keys:
            assert key in artifact, f"Missing required key: {key}"

        assert artifact["schema"] == "AGENT_SESSION_HOTSPOTS_V1"
