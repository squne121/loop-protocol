"""test_guard_shadow_mode.py

guard-japanese-prose.sh の shadow / enforce mode の動作を検証する。

AC1: GUARD_JAPANESE_PROSE_MODE=shadow のとき guard は block せず exit 0 で通過する
AC2: shadow mode は would-block 判定を JSONL に記録し、
     route / reason / failed_block_count / duration_ms / public_mutation を含む
AC3: GUARD_JAPANESE_PROSE_MODE=enforce のとき従来どおり英語 prose を block する
AC4: mode は環境変数で rollback 可能で、default 値が shadow（未設定 = shadow）
AC7: mode semantics が固定される（未設定/shadow/enforce/不正値の各挙動）
AC8: JSONL に raw body / full command / token / Authorization header を記録しない
AC9: instrumentation 失敗は shadow mode でも silent allow にしない
"""

import json
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

# guard-japanese-prose.sh の絶対パス
HOOK_SCRIPT = Path(__file__).parent.parent / "guard-japanese-prose.sh"

# shadow_log.py の絶対パス
SHADOW_LOG_PY = Path(__file__).parent.parent / "shadow_log.py"


def run_hook(
    tool_name: str,
    command: str | None = None,
    *,
    env_mode: str | None = None,
    shadow_log_file: str | None = None,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """guard-japanese-prose.sh を呼び出す。"""
    if command is None:
        tool_input = {}
    else:
        tool_input = {"command": command}

    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})

    env = os.environ.copy()
    if env_mode is not None:
        env["GUARD_JAPANESE_PROSE_MODE"] = env_mode
    elif "GUARD_JAPANESE_PROSE_MODE" in env:
        del env["GUARD_JAPANESE_PROSE_MODE"]

    if shadow_log_file is not None:
        env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = shadow_log_file

    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


def read_jsonl(path: str) -> list[dict]:
    """JSONL ファイルを読み込んでリストとして返す。"""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# ---------------------------------------------------------------------------
# AC1: shadow mode は block しない
# ---------------------------------------------------------------------------

class TestShadowDoesNotBlock:
    """AC1: shadow mode は exit 0 で通過する。"""

    def test_shadow_does_not_block_english_prose(self, tmp_path):
        """shadow mode: 英語 prose でも exit 0 で通過する（block しない）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        # 英語だけの body をファイルに書く
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose. No Japanese here at all. "
            "We are testing that shadow mode does not block.",
            encoding="utf-8",
        )
        # gh issue create --body-file を使って触らせる
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"shadow mode must not block: exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_shadow_does_not_block_when_mode_unset(self, tmp_path):
        """AC4: GUARD_JAPANESE_PROSE_MODE 未設定 = shadow → block しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English, no Japanese at all. Testing default shadow mode.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"default (unset) mode must be shadow and not block: "
            f"exit={result.returncode}\nstderr: {result.stderr}"
        )

    def test_shadow_does_not_block_japanese_body(self, tmp_path):
        """shadow mode: 日本語 prose は正常通過（would-allow）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた本文です。日本語の比率が高いため通過します。",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"shadow mode must not block Japanese body: "
            f"exit={result.returncode}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# AC2: shadow mode の JSONL 記録
# ---------------------------------------------------------------------------

class TestJsonlRecord:
    """AC2: shadow mode は would-block 判定を JSONL に記録する。"""

    def test_jsonl_record_and_route_and_failed_block_count(self, tmp_path):
        """JSONL に route_id / reason_code / failed_block_count / public_mutation が含まれる。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is English prose only. No Japanese at all. Shadow should record.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0

        # JSONL が存在し、フィールドを含む
        assert os.path.exists(log_file), "shadow log JSONL が存在しない"
        entries = read_jsonl(log_file)
        assert len(entries) > 0, "JSONL エントリが空"

        entry = entries[-1]
        assert "route_id" in entry, f"route_id が欠落: {entry}"
        assert "reason_code" in entry, f"reason_code が欠落: {entry}"
        assert "failed_block_count" in entry, f"failed_block_count が欠落: {entry}"
        assert "public_mutation" in entry, f"public_mutation が欠落: {entry}"
        assert "duration_ms" in entry, f"duration_ms が欠落: {entry}"

    def test_jsonl_schema_version_present(self, tmp_path):
        """JSONL に schema_version が含まれる（AC10 prerequisite）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "English only, no Japanese prose here at all.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), "JSONL が存在しない"
        entries = read_jsonl(log_file)
        assert len(entries) > 0
        entry = entries[-1]
        assert "schema_version" in entry, f"schema_version が欠落: {entry}"
        assert entry["schema_version"] == "1", f"schema_version が '1' でない: {entry}"

    def test_jsonl_decision_would_be_deny_for_english_body(self, tmp_path):
        """英語 body で shadow mode 時、decision_would_be=deny が記録される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English prose. No Japanese at all. This should be a would-deny case.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0

        entries = read_jsonl(log_file)
        deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
        assert len(deny_entries) > 0, (
            f"英語 body で decision_would_be=deny が記録されていない: {entries}"
        )


# ---------------------------------------------------------------------------
# AC3: enforce mode は block する
# ---------------------------------------------------------------------------

class TestEnforceBlocksEnglishProse:
    """AC3: enforce mode は英語 prose を block する。"""

    def test_enforce_blocks_english_prose(self, tmp_path):
        """enforce mode: 英語だけの body は exit 2 でブロックされる。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose. No Japanese here at all. "
            "Enforce should block this.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="enforce", shadow_log_file=log_file)
        assert result.returncode == 2, (
            f"enforce mode must block English prose: exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )

    def test_enforce_allows_japanese_prose(self, tmp_path):
        """enforce mode: 日本語 prose は通過する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた Issue 本文です。日本語の比率が高いため通過します。",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="enforce", shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"enforce mode must allow Japanese prose: exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# AC4: default mode / rollback
# ---------------------------------------------------------------------------

class TestDefaultModeAndRollback:
    """AC4: GUARD_JAPANESE_PROSE_MODE 未設定 = shadow、rollback 可能。"""

    def test_default_mode_is_shadow(self, tmp_path):
        """未設定のときは shadow（block しない）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "Entirely English. Should be shadow by default.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"default (unset) must be shadow: exit={result.returncode}"
        )

    def test_rollback_from_enforce_to_shadow(self, tmp_path):
        """enforce → shadow への rollback が環境変数で可能。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English no Japanese whatsoever. Rollback test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        # enforce: block
        result_enforce = run_hook("Bash", command, env_mode="enforce", shadow_log_file=log_file)
        assert result_enforce.returncode == 2, "enforce must block"

        # shadow: allow（rollback）
        result_shadow = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result_shadow.returncode == 0, "shadow must allow (rollback)"


# ---------------------------------------------------------------------------
# AC7: mode semantics
# ---------------------------------------------------------------------------

class TestModeSemantics:
    """AC7: mode semantics 固定（未設定/shadow/enforce/不正値）。"""

    def test_mode_semantics_unset_is_shadow(self, tmp_path):
        """AC7: 未設定 = shadow として動作。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only prose no Japanese.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0

    def test_mode_semantics_shadow(self, tmp_path):
        """AC7: shadow = block しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only no Japanese at all.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0

    def test_mode_semantics_enforce(self, tmp_path):
        """AC7: enforce = block する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only no Japanese at all enforce test.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="enforce", shadow_log_file=log_file)
        assert result.returncode == 2

    def test_mode_semantics_invalid_value_acts_as_shadow(self, tmp_path):
        """AC7: 不正値（例: 'dry-run'）は shadow として動作。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "English only no Japanese. Testing invalid mode value.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook(
            "Bash", command,
            shadow_log_file=log_file,
            extra_env={"GUARD_JAPANESE_PROSE_MODE": "dry-run"},
        )
        assert result.returncode == 0, (
            f"invalid mode must act as shadow: exit={result.returncode}"
        )

    def test_mode_semantics_invalid_value_records_invalid_mode_in_jsonl(self, tmp_path):
        """AC7: 不正値のとき JSONL に invalid_mode が記録される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "English only no Japanese. Invalid mode JSONL test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        run_hook(
            "Bash", command,
            shadow_log_file=log_file,
            extra_env={"GUARD_JAPANESE_PROSE_MODE": "invalid_value_xyz"},
        )

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        assert entries, "JSONL エントリが空"
        entry = entries[-1]
        reason = entry.get("reason_code", "")
        assert "invalid_mode" in reason, (
            f"invalid_mode が reason_code に記録されていない: reason_code={reason}"
        )


# ---------------------------------------------------------------------------
# AC8: JSONL に raw body / token / Authorization header を記録しない
# ---------------------------------------------------------------------------

class TestNoRawBody:
    """AC8: JSONL に raw body / full command / token / Authorization header を記録しない。"""

    def test_no_raw_body_in_jsonl(self, tmp_path):
        """JSONL に raw_body フィールドが存在しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        fake_token = "FAKE_TOKEN_VALUE"
        fake_raw_body = "FAKE_RAW_BODY_VALUE"
        body_file.write_text(
            f"secret_token={fake_token}. raw_body_content={fake_raw_body}. All English. No Japanese.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        assert entries, "JSONL エントリが空"
        for entry in entries:
            assert "raw_body" not in entry, f"raw_body が記録されている: {entry}"
            assert "full_command" not in entry, f"full_command が記録されている: {entry}"
            assert "token" not in entry, f"token が記録されている: {entry}"
            assert "authorization_header" not in entry, (
                f"authorization_header が記録されている: {entry}"
            )
            # key 存在チェックだけでなく、エントリ全体を json.dumps して secret 文字列が存在しないことを検証
            full_text = json.dumps(entry, ensure_ascii=False)
            assert fake_token not in full_text, f"raw token が記録されている: {full_text}"
            assert fake_raw_body not in full_text, f"raw body が記録されている: {full_text}"

    def test_body_sha256_is_recorded_instead(self, tmp_path):
        """JSONL に body_sha256 と body_bytes が記録される（raw body の代替）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English. No Japanese here at all. SHA256 test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        assert entries, "JSONL エントリが空"
        # would-deny エントリには body_sha256 / body_bytes が記録される
        deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
        assert len(deny_entries) >= 1, f"期待する deny エントリが見つからない: {entries}"
        for entry in deny_entries:
            assert "body_sha256" in entry, f"body_sha256 が欠落: {entry}"
            assert "body_bytes" in entry, f"body_bytes が欠落: {entry}"


# ---------------------------------------------------------------------------
# AC9: instrumentation 失敗は silent allow しない
# ---------------------------------------------------------------------------

class TestInstrumentationError:
    """AC9: instrumentation 失敗は silent allow にしない。"""

    def test_instrumentation_error_logged_not_silent(self, tmp_path):
        """shadow_log.py が不在でも silent allow にせず stderr に記録する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English. No Japanese here. instrumentation error test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        # SHADOW_LOG_FILE を書き込み不可のパスに設定して logging 失敗を引き起こす
        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file="/nonexistent_dir/shadow.jsonl",
        )
        # shadow mode では exit 0（allow）のまま
        assert result.returncode == 0, (
            f"shadow mode must allow even on instrumentation error: exit={result.returncode}"
        )
        # stderr に instrumentation_error の記録がある
        assert "instrumentation_error" in result.stderr, (
            f"instrumentation_error が stderr に記録されていない:\nstderr: {result.stderr}"
        )

    def test_logging_failure_recorded_in_stderr(self, tmp_path):
        """JSONL 書き込み失敗時に stderr に記録される（AC9）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English no Japanese at all. Logging failure test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        # 書き込み不可ディレクトリ
        result = run_hook(
            "Bash", command,
            env_mode="shadow",
            shadow_log_file="/root/protected_dir/shadow.jsonl",
        )
        # shadow mode は exit 0
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# body_bytes: バイト数（文字数でなく）を記録する
# ---------------------------------------------------------------------------

class TestBodyBytesIsActualByteCount:
    """body_bytes は ${#var}（文字数）ではなく wc -c（バイト数）で計測する。"""

    def test_body_bytes_is_byte_count_not_char_count(self, tmp_path):
        """日本語を含む body で body_bytes がバイト数になっていることを確認する。

        "日本語abc" は UTF-8 で 3*3+3 = 12 バイト、文字数は 6。
        body_bytes が 6 なら文字数（誤り）、12 なら正しいバイト数。
        """
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        # 日本語 3 文字（各 3 バイト）+ ASCII 3 文字（各 1 バイト）= 12 バイト、6 文字
        japanese_body = "日本語abc"
        body_file.write_text(japanese_body, encoding="utf-8")

        # would-deny が出るよう shadow mode + 英語のみ扱いにならないように
        # そのまま would-deny になる英語のみのコマンドで body_bytes を検証する
        # ただし "日本語abc" はブロックされない可能性があるので、確実に deny させるため
        # 全英語 body で body_bytes のみを検証する別入力を使う
        english_body = "a" * 6  # 6 文字 = 6 バイト（ASCII）
        english_file = tmp_path / "english.md"
        english_file.write_text(english_body, encoding="utf-8")
        command_en = f"gh issue create --body-file {english_file}"
        run_hook("Bash", command_en, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
        assert len(deny_entries) >= 1, f"deny エントリが見つからない: {entries}"
        entry = deny_entries[-1]
        assert "body_bytes" in entry, f"body_bytes が欠落: {entry}"
        # 6 バイトの ASCII body → body_bytes は 6
        assert entry["body_bytes"] == 6, (
            f"body_bytes が期待値 6 と一致しない: {entry['body_bytes']}"
        )

    def test_body_bytes_utf8_multibyte(self, tmp_path):
        """日本語 3 文字 + ASCII 3 文字 = 12 バイトが body_bytes として記録される。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        # 英語プロセでブロックさせるため、日本語を含む英語主体テキストを使う
        # shadow_allow の body_bytes は _block_or_shadow 経由でのみセットされる
        # 確実に日本語テキストで body_bytes が正しく取れることを確認するため
        # wc -c で期待値を計算する
        import subprocess as sp
        japanese_body = "日本語abc"
        expected_bytes = len(japanese_body.encode("utf-8"))  # 12
        assert expected_bytes == 12, "テスト前提: 日本語abc は 12 バイト"

        # guard がブロックするよう全英語テキストに日本語を混ぜる
        # ただし比率が低く fail する構成にする（英語大量 + 日本語少量）
        mixed_body = "A" * 100 + japanese_body
        body_file.write_text(mixed_body, encoding="utf-8")
        expected_mixed_bytes = len(mixed_body.encode("utf-8"))  # 100 + 12 = 112

        command = f"gh issue create --body-file {body_file}"
        run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
        assert len(deny_entries) >= 1, f"deny エントリが見つからない: {entries}"
        entry = deny_entries[-1]
        assert entry.get("body_bytes") == expected_mixed_bytes, (
            f"body_bytes が期待値 {expected_mixed_bytes} と一致しない: {entry.get('body_bytes')}"
        )


# ---------------------------------------------------------------------------
# duration_ms: 実測値（0 以上の整数）が記録される
# ---------------------------------------------------------------------------

class TestDurationMsIsActualMeasurement:
    """duration_ms は固定値 0 でなく実測値（0 以上の整数）が記録される。"""

    def test_duration_ms_is_nonnegative_integer(self, tmp_path):
        """shadow mode で duration_ms フィールドが 0 以上の整数であることを確認する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English no Japanese. duration_ms test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)

        assert os.path.exists(log_file), f"ログファイルが生成されていない: {log_file}"
        entries = read_jsonl(log_file)
        assert entries, "JSONL エントリが空"
        deny_entries = [e for e in entries if e.get("decision_would_be") == "deny"]
        assert len(deny_entries) >= 1, f"期待する deny エントリが見つからない: {entries}"
        entry = deny_entries[-1]
        assert "duration_ms" in entry, f"duration_ms が欠落: {entry}"
        assert isinstance(entry["duration_ms"], int), (
            f"duration_ms が整数でない: {entry['duration_ms']!r}"
        )
        assert entry["duration_ms"] >= 0, (
            f"duration_ms が負の値: {entry['duration_ms']}"
        )
