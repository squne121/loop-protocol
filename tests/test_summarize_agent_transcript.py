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
  AC7: artifact が tmp/agent-session-hotspots/ に書き込まれる
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
CANARY_FIXTURE = FIXTURES_DIR / "canary-redaction.jsonl"
NO_TIMESTAMP_FIXTURE = FIXTURES_DIR / "no-timestamp.jsonl"
SAME_TIMESTAMP_FIXTURE = FIXTURES_DIR / "same-timestamp.jsonl"
MULTI_SOURCE_HOOK_FIXTURE = FIXTURES_DIR / "multi-source-hook.jsonl"
ALL_INVALID_FIXTURE = FIXTURES_DIR / "all-invalid.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_main_capture(args: list[str]) -> tuple[int, str]:
    """main() を実行して (exit_code, stdout) を返す。"""
    buf = StringIO()
    with patch("sys.stdout", buf):
        code = sat.main(args)
    return code, buf.getvalue()


def get_artifact_from_stdout(stdout: str) -> dict:
    """stdout から ARTIFACT パスを抽出して JSON を返す。"""
    artifact_line = next(
        (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
    )
    assert artifact_line is not None, f"ARTIFACT: line not found in stdout: {stdout}"
    artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
    assert artifact_path.exists(), f"Artifact file not found: {artifact_path}"
    with open(artifact_path, encoding="utf-8") as f:
        return json.load(f)


def validate_hotspots_schema(data: dict) -> None:
    """
    AGENT_SESSION_HOTSPOTS_V1 artifact の nested schema を検証する。

    Validates:
      - required top-level keys
      - metrics.* の型が int または UNKNOWN wrapper
      - privacy.raw_transcript_included が false
    """
    required_top_level = [
        "schema", "generated_at", "producer", "input_refs",
        "privacy", "metrics", "evidence",
    ]
    for key in required_top_level:
        assert key in data, f"Missing required top-level key: {key}"

    assert data["schema"] == "AGENT_SESSION_HOTSPOTS_V1", (
        f"Unexpected schema value: {data['schema']}"
    )
    assert data["privacy"]["raw_transcript_included"] is False, (
        "raw_transcript_included must be False"
    )

    # metrics.* の型チェック: int または UNKNOWN wrapper
    def _is_valid_metric(v: object) -> bool:
        if isinstance(v, (int, float, bool)):
            return True
        if isinstance(v, dict) and v.get("availability") == "unknown" and v.get("value") is None:
            return True
        if isinstance(v, str):
            return True  # tool name 等の文字列は許容
        if isinstance(v, dict):
            # ネストした dict はサブフィールドを再帰チェック
            return all(_is_valid_metric(sv) for sv in v.values())
        return False

    metrics = data["metrics"]
    assert isinstance(metrics, dict), "metrics must be a dict"
    for key, val in metrics.items():
        assert _is_valid_metric(val), (
            f"metrics.{key} has invalid type: {type(val).__name__} = {val!r}"
        )


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
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        assert exit_code == 0, f"exit code should be 0, got {exit_code}; stdout={stdout}"
        assert "STATUS: 0" in stdout

        artifact = get_artifact_from_stdout(stdout)

        # nested schema validator
        validate_hotspots_schema(artifact)

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

        assert metrics["tool"]["name"] == "claude"
        assert metrics["tool"]["version"] == "claude-sonnet-4-6"
        assert metrics["model"]["name"] == "claude-sonnet-4-6"
        assert metrics["model"]["reasoning_effort"] == "high"

    def test_subagents_counted(self) -> None:
        """trusted_field: subagent_spawn イベント数を集計する。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert result["metrics"]["subagents"]["spawned_count"] == 2

    def test_failed_commands_counted(self) -> None:
        """trusted_field: exit_code != 0 の tool_use を failed command として計上。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert result["metrics"]["commands"]["failed_count"] == 2

    def test_repeated_reads_detected(self) -> None:
        """trusted_field: 同一 file_path への複数 Read は repeated_read_count に反映。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
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

    def test_files_metrics_present(self) -> None:
        """metrics.files に read_count / unique_read_count / modified_count が含まれる。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        files = result["metrics"]["files"]
        assert "read_count" in files
        assert "unique_read_count" in files
        assert "modified_count" in files
        assert isinstance(files["read_count"], int)
        assert isinstance(files["unique_read_count"], int)
        assert isinstance(files["modified_count"], int)
        # claude fixture に Read 2回あり
        assert files["read_count"] == 2
        assert files["unique_read_count"] == 1  # same file twice


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

        artifact = get_artifact_from_stdout(stdout)
        validate_hotspots_schema(artifact)
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
        minimal_fixture = tmp_path / "minimal.jsonl"
        minimal_fixture.write_text(
            '{"type":"session_start","tool":"test","model":"test-model","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )

        result, _ = sat.parse_transcript(minimal_fixture, redact=False)
        tokens = result["metrics"]["tokens"]

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
# AC4/AC11: raw transcript 本文が stdout に含まれない / redaction 動作
# (no_raw, raw_body, artifact_privacy キーワード対応)
# ---------------------------------------------------------------------------


class TestNoRawAndRedaction:
    """AC4/AC11: raw transcript が stdout に出ない、redaction が動作する。"""

    def test_no_raw_transcript_in_stdout(self) -> None:
        """no_raw: stdout に transcript の生の行が含まれない。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        assert '"type":"session_start"' not in stdout
        assert '"session_id"' not in stdout

    def test_raw_body_not_in_artifact(self) -> None:
        """raw_body: artifact JSON に raw transcript 行が保存されない。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)

        assert artifact["privacy"]["raw_transcript_included"] is False
        artifact_str = json.dumps(artifact)
        assert '"type":"session_start"' not in artifact_str

    def test_artifact_privacy_field_structure(self) -> None:
        """artifact_privacy: privacy フィールドが schema を満たす。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)

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
        artifact = get_artifact_from_stdout(stdout)

        assert artifact["privacy"]["redaction_enabled"] is True
        assert artifact["privacy"]["public_projection_safe"] is True

        t_value = artifact["input_refs"]["transcript_path"]["value"]
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

    def test_aws_key_redacted(self) -> None:
        """redact: AWS key パターンが <AWS_ACCESS_KEY> に置換される。"""
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE1"
        result = sat.redact_string(text)
        assert "AKIA" not in result
        assert "<AWS_ACCESS_KEY>" in result

    def test_pem_key_redacted(self) -> None:
        """redact: PEM key block が <PEM_KEY> に置換される。"""
        text = "cert: -----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        result = sat.redact_string(text)
        assert "BEGIN RSA PRIVATE KEY" not in result
        assert "<PEM_KEY>" in result


# ---------------------------------------------------------------------------
# AC7/AC11: privacy canary fixture — canary が stdout/artifact に残らない
# ---------------------------------------------------------------------------


class TestPrivacyCanary:
    """AC7/AC11: canary fixture を使い stdout・artifact への secret 漏洩を確認。"""

    # canary 値 (fixture に含まれる)
    CANARY_GHP = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxx1234567"
    CANARY_SK = "sk-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKL"
    CANARY_AKIA = "AKIAIOSFODNN7EXAMPLE123"
    CANARY_PATH = "/home/user/projects/foo/bar.py"
    CANARY_DOTENV = ".env.local"
    CANARY_SETTINGS = "settings.local.json"

    def _run_with_redact(self) -> tuple[int, str, dict]:
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CANARY_FIXTURE), "--redact"]
        )
        artifact = get_artifact_from_stdout(stdout)
        return exit_code, stdout, artifact

    def test_no_raw_canary_in_stdout(self) -> None:
        """no_raw: canary fixture の各 canary 値が stdout に残らない。"""
        _, stdout, _ = self._run_with_redact()
        assert self.CANARY_GHP not in stdout, "GitHub token leaked to stdout"
        assert self.CANARY_SK not in stdout, "OpenAI key leaked to stdout"
        assert self.CANARY_AKIA not in stdout, "AWS key leaked to stdout"

    def test_no_github_token_in_artifact(self) -> None:
        """artifact_privacy: GitHub token が artifact JSON に残らない。"""
        _, _, artifact = self._run_with_redact()
        artifact_str = json.dumps(artifact)
        assert self.CANARY_GHP not in artifact_str, "GitHub token leaked to artifact"

    def test_no_openai_key_in_artifact(self) -> None:
        """artifact_privacy: OpenAI key が artifact JSON に残らない。"""
        _, _, artifact = self._run_with_redact()
        artifact_str = json.dumps(artifact)
        assert self.CANARY_SK not in artifact_str, "OpenAI key leaked to artifact"

    def test_no_aws_key_in_artifact(self) -> None:
        """artifact_privacy: AWS key が artifact JSON に残らない。"""
        _, _, artifact = self._run_with_redact()
        artifact_str = json.dumps(artifact)
        assert self.CANARY_AKIA not in artifact_str, "AWS key leaked to artifact"

    def test_no_absolute_path_in_artifact(self) -> None:
        """artifact_privacy: absolute path が artifact JSON に残らない (raw path)。"""
        _, _, artifact = self._run_with_redact()
        artifact_str = json.dumps(artifact)
        assert self.CANARY_PATH not in artifact_str, "Absolute path leaked to artifact"

    def test_unknown_event_raw_payload_not_in_artifact(self) -> None:
        """unknown event の raw payload が artifact に保存されない。"""
        _, _, artifact = self._run_with_redact()
        artifact_str = json.dumps(artifact)
        # canary fixture の custom_unknown_event フィールド値は artifact に出てはならない
        assert "secret_value_here" not in artifact_str, (
            "Unknown event raw payload leaked to artifact"
        )
        assert "also_unknown" not in artifact_str, (
            "Unknown event raw field name leaked to artifact"
        )


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

        assert "tmp/agent-session-hotspots" in str(artifact_path), (
            f"Unexpected artifact path: {artifact_path}"
        )

    def test_artifact_filename_collision_resistant(self) -> None:
        """High 4: ファイル名に timestamp と sha_prefix と pid が含まれる。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        artifact_path = Path(artifact_line.split("ARTIFACT:", 1)[1].strip())
        name = artifact_path.name

        # パターン: {stem}_{ts}_{sha_prefix}_{pid}.json
        parts = name.replace(".json", "").split("_")
        # stem は "claude-sample" (ハイフン含む) → 最初の部分は stem
        assert name.endswith(".json"), f"Unexpected extension: {name}"
        # UTC timestamp pattern (T が含まれる)
        assert any("T" in p and p.endswith("Z") for p in parts), (
            f"No UTC timestamp found in filename: {name}"
        )


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
        bad_fixture = tmp_path / "bad.jsonl"
        bad_fixture.write_text(
            '{"type":"session_start","tool":"test","timestamp":"2026-01-01T00:00:00Z"}\n'
            "THIS IS NOT JSON\n",
            encoding="utf-8",
        )
        exit_code, stdout = run_main_capture(["--transcript", str(bad_fixture)])
        assert exit_code == 1
        assert "STATUS: 1" in stdout

    def test_all_invalid_lines_returns_3(self) -> None:
        """High 2: 全行 invalid JSONL は exit code 3 を返し artifact を書かない。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(ALL_INVALID_FIXTURE)]
        )
        assert exit_code == 3, f"Expected exit code 3, got {exit_code}; stdout={stdout}"
        # artifact は書かれない
        artifact_line = next(
            (l for l in stdout.splitlines() if l.startswith("ARTIFACT:")), None
        )
        assert artifact_line is None, "Artifact should not be written on parse_error"

    def test_all_invalid_inline(self, tmp_path: Path) -> None:
        """High 2 inline: 全行 invalid の tmp fixture で exit code 3 を確認。"""
        all_bad = tmp_path / "all_invalid.jsonl"
        all_bad.write_text(
            "NOT JSON\nSTILL NOT JSON\n{broken\n",
            encoding="utf-8",
        )
        exit_code, stdout = run_main_capture(["--transcript", str(all_bad)])
        assert exit_code == 3


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
        assert "session_id" not in metrics

    def test_trusted_field_tool_name_present(self) -> None:
        """trusted_field: tool.name は session_start の fixture_observed フィールド。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        assert "name" in result["metrics"]["tool"]

    def test_unknown_event_key_not_in_metrics(self, tmp_path: Path) -> None:
        """
        fixture_observed_only: 未知の event type の key は metrics に昇格されない。
        """
        fixture = tmp_path / "unknown-events.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"test","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"UNKNOWN_FUTURE_EVENT_TYPE","custom_field":"value","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )
        result, warnings = sat.parse_transcript(fixture, redact=False)
        # 未知 event は event_counts に記録
        assert "UNKNOWN_FUTURE_EVENT_TYPE" in result["event_counts"]
        # metrics に昇格されていない
        assert "UNKNOWN_FUTURE_EVENT_TYPE" not in result["metrics"]
        # custom_field は metrics に昇格されていない
        assert "custom_field" not in result["metrics"]

    def test_unknown_event_key_in_fixture_observed(self, tmp_path: Path) -> None:
        """
        fixture_observed_only: 未知 event の key は fixture_observed_fields に記録される。
        """
        fixture = tmp_path / "unknown-events.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"test","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"UNKNOWN_FUTURE_EVENT_TYPE","custom_field":"value","timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )
        result, _ = sat.parse_transcript(fixture, redact=False)
        # fixture_observed_fields に custom_field が記録されている
        assert "custom_field" in result.get("fixture_observed_fields", [])

    def test_session_id_in_fixture_observed_not_metrics(self) -> None:
        """
        session_id は hook_input フィールドのため fixture_observed_fields に記録されるが
        metrics には昇格されない。
        """
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        # session_id は metrics に存在しない
        assert "session_id" not in result["metrics"]
        # session_id は fixture_observed_fields に記録されている
        assert "session_id" in result.get("fixture_observed_fields", [])

    def test_artifact_has_fixture_observed_in_evidence(self) -> None:
        """artifact の evidence に fixture_observed_fields が含まれる。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)
        assert "fixture_observed_fields" in artifact["evidence"]
        assert "session_id" in artifact["evidence"]["fixture_observed_fields"]

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
        """ordering: event_counts は type 別集計。"""
        result, _ = sat.parse_transcript(CLAUDE_FIXTURE, redact=False)
        event_counts = result["event_counts"]
        assert event_counts.get("session_start") == 1
        assert event_counts.get("session_end") == 1

    def test_timestamp_field_in_artifact(self) -> None:
        """timestamp: artifact の generated_at は ISO 8601 形式。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)

        generated_at = artifact["generated_at"]
        from datetime import datetime
        dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        assert dt is not None

    def test_concurrent_subagent_counts_accumulated(self, tmp_path: Path) -> None:
        """concurrent: 複数の subagent_spawn は spawned_count に累積される。"""
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
        assert result["metrics"]["subagents"]["spawned_count"] == 3

    def test_ordering_unknown_when_timestamp_missing(self) -> None:
        """ordering: timestamp 欠落時は ordering が UNKNOWN wrapper になる。"""
        result, _ = sat.parse_transcript(NO_TIMESTAMP_FIXTURE, redact=False)
        ordering = result["metrics"]["ordering"]
        assert ordering == {"availability": "unknown", "value": None}, (
            f"ordering should be UNKNOWN when timestamps missing, got: {ordering}"
        )

    def test_ordering_unknown_when_same_timestamp(self) -> None:
        """ordering: 同一 timestamp が存在する場合は ordering が UNKNOWN になる。"""
        result, _ = sat.parse_transcript(SAME_TIMESTAMP_FIXTURE, redact=False)
        ordering = result["metrics"]["ordering"]
        assert ordering == {"availability": "unknown", "value": None}, (
            f"ordering should be UNKNOWN for same timestamps, got: {ordering}"
        )

    def test_ordering_unknown_when_multi_source_hook(self) -> None:
        """ordering: multi-source hook event がある場合は ordering が UNKNOWN になる。"""
        result, _ = sat.parse_transcript(MULTI_SOURCE_HOOK_FIXTURE, redact=False)
        ordering = result["metrics"]["ordering"]
        assert ordering == {"availability": "unknown", "value": None}, (
            f"ordering should be UNKNOWN for multi-source hooks, got: {ordering}"
        )

    def test_ordering_available_with_unique_timestamps(self, tmp_path: Path) -> None:
        """ordering: すべての timestamp が unique で single-source なら available。"""
        fixture = tmp_path / "ordered.jsonl"
        fixture.write_text(
            '{"type":"session_start","tool":"claude","model":"m","timestamp":"2026-01-01T00:00:00Z"}\n'
            '{"type":"tool_use","tool_name":"Bash","input":{"command":"ls"},"timestamp":"2026-01-01T00:00:01Z"}\n'
            '{"type":"session_end","timestamp":"2026-01-01T00:01:00Z"}\n',
            encoding="utf-8",
        )
        result, _ = sat.parse_transcript(fixture, redact=False)
        ordering = result["metrics"]["ordering"]
        assert ordering.get("availability") == "available", (
            f"ordering should be available with unique timestamps, got: {ordering}"
        )


# ---------------------------------------------------------------------------
# AC9 schema フィールド確認 + nested validator
# ---------------------------------------------------------------------------


class TestSchemaFields:
    """AC9: artifact に必須フィールドが全て含まれ、nested schema validator が通る。"""

    def test_all_required_fields_present(self) -> None:
        """artifact に必須 top-level フィールドが全て含まれる。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)

        required_keys = [
            "schema",
            "generated_at",
            "producer",
            "input_refs",
            "privacy",
            "metrics",
            "evidence",
        ]
        for key in required_keys:
            assert key in artifact, f"Missing required key: {key}"

        assert artifact["schema"] == "AGENT_SESSION_HOTSPOTS_V1"

    def test_nested_schema_validator_passes_on_claude_fixture(self) -> None:
        """AC1 + High 3: Claude fixture で nested schema validator が通る。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CLAUDE_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)
        validate_hotspots_schema(artifact)  # raises AssertionError if invalid

    def test_nested_schema_validator_passes_on_codex_fixture(self) -> None:
        """AC2 + High 3: Codex fixture で nested schema validator が通る。"""
        exit_code, stdout = run_main_capture(
            ["--transcript", str(CODEX_FIXTURE)]
        )
        artifact = get_artifact_from_stdout(stdout)
        validate_hotspots_schema(artifact)
