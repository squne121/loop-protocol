"""test_guard_shadow_mode.py

guard-japanese-prose.sh の unset/off/shadow(legacy alias)/enforce/invalid mode の
動作を検証する（#2265: persistent shadow logging 廃止後）。

AC1: unset/off/shadow(legacy alias) のとき guard は block せず exit 0 で通過し、
     .guard_shadow_log.jsonl を新規作成・更新しない
AC2: GUARD_JAPANESE_PROSE_MODE=enforce のとき従来どおり英語 prose を block する
AC4: mode は環境変数で rollback 可能で、default 値が off（未設定 = off）
AC6: GUARD_JAPANESE_PROSE_SHADOW_LOG で出力先を上書きしても write は発生しない
AC7: mode semantics が固定される（未設定/off/shadow(legacy alias)/enforce/不正値の各挙動）
AC8: invalid mode 設定時（#2265 BLOCKER1 fix_delta）、tool 実行は exit 1 の
     non-blocking diagnostic として通知され（block しない）、persistent file を生成しない
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# guard-japanese-prose.sh の絶対パス
HOOK_SCRIPT = Path(__file__).parent.parent / "guard-japanese-prose.sh"


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

    proc_env = os.environ.copy()
    if env_mode is not None:
        proc_env["GUARD_JAPANESE_PROSE_MODE"] = env_mode
    elif "GUARD_JAPANESE_PROSE_MODE" in proc_env:
        del proc_env["GUARD_JAPANESE_PROSE_MODE"]

    if shadow_log_file is not None:
        proc_env["GUARD_JAPANESE_PROSE_SHADOW_LOG"] = shadow_log_file

    if extra_env:
        proc_env.update(extra_env)

    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=proc_env,
    )


# ---------------------------------------------------------------------------
# AC1 / AC4: default (unset/off/shadow) は block しない、persistent file なし
# ---------------------------------------------------------------------------

class TestShadowDoesNotBlock:
    """AC1: unset/off/shadow(legacy alias) は exit 0 で通過する。"""

    def test_shadow_does_not_block_english_prose(self, tmp_path):
        """off 相当の shadow mode: 英語 prose でも exit 0 で通過する（block しない）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose. No Japanese here at all. "
            "We are testing that shadow mode does not block.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"shadow(legacy alias) mode must not block: exit={result.returncode}\n"
            f"stderr: {result.stderr}"
        )
        assert not os.path.exists(log_file), (
            "persistent shadow log は #2265 で廃止済みのため生成されてはならない"
        )

    def test_shadow_does_not_block_when_mode_unset(self, tmp_path):
        """AC4: GUARD_JAPANESE_PROSE_MODE 未設定 = off → block しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English, no Japanese at all. Testing default off mode.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"default (unset) mode must be off and not block: "
            f"exit={result.returncode}\nstderr: {result.stderr}"
        )
        assert not os.path.exists(log_file)

    def test_shadow_does_not_block_japanese_body(self, tmp_path):
        """off 相当の shadow mode: 日本語 prose は正常通過。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた本文です。日本語の比率が高いため通過します。",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"shadow(legacy alias) mode must not block Japanese body: "
            f"exit={result.returncode}\nstderr: {result.stderr}"
        )
        assert not os.path.exists(log_file)


# ---------------------------------------------------------------------------
# AC1 / AC6 / AC8: persistent logging が完全に廃止されていることを確認する
# ---------------------------------------------------------------------------

class TestNoPersistentLogging:
    """#2265: shadow/off/invalid mode で persistent JSONL が一切生成されない。"""

    def test_no_persistent_file_created_for_would_deny_case(self, tmp_path):
        """would-deny 相当（英語 prose）のケースでも JSONL が作成されない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is English prose only. No Japanese at all. Would-deny case.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0
        assert not os.path.exists(log_file), "would-deny ケースでも JSONL を生成してはならない"

    def test_shadow_log_env_override_does_not_write(self, tmp_path):
        """AC6: GUARD_JAPANESE_PROSE_SHADOW_LOG で出力先を上書きしても write が発生しない。"""
        override_path = str(tmp_path / "custom_override.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English no Japanese. Override path write test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=override_path)
        assert result.returncode == 0
        assert not os.path.exists(override_path), (
            "GUARD_JAPANESE_PROSE_SHADOW_LOG で上書きした出力先にも write してはならない"
        )


# ---------------------------------------------------------------------------
# AC2: enforce mode は block する
# ---------------------------------------------------------------------------

class TestEnforceBlocksEnglishProse:
    """AC2: enforce mode は英語 prose を block する。"""

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
        assert not os.path.exists(log_file)

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
        assert not os.path.exists(log_file)


# ---------------------------------------------------------------------------
# AC4: default mode / rollback
# ---------------------------------------------------------------------------

class TestDefaultModeAndRollback:
    """AC4: GUARD_JAPANESE_PROSE_MODE 未設定 = off、rollback 可能。"""

    def test_default_mode_is_shadow(self, tmp_path):
        """未設定のときは off（block しない）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "Entirely English. Should be off by default.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0, (
            f"default (unset) must be off: exit={result.returncode}"
        )
        assert not os.path.exists(log_file)

    def test_rollback_from_enforce_to_shadow(self, tmp_path):
        """enforce → shadow(legacy alias) への rollback が環境変数で可能。"""
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

        # shadow(legacy alias): allow（rollback）
        result_shadow = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result_shadow.returncode == 0, "shadow(legacy alias) must allow (rollback)"
        assert not os.path.exists(log_file)


# ---------------------------------------------------------------------------
# AC7: mode semantics
# ---------------------------------------------------------------------------

class TestModeSemantics:
    """AC7: mode semantics 固定（未設定/off/shadow(legacy alias)/enforce/不正値）。"""

    def test_mode_semantics_unset_is_shadow(self, tmp_path):
        """AC7: 未設定 = off として動作。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only prose no Japanese.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode=None, shadow_log_file=log_file)
        assert result.returncode == 0
        assert not os.path.exists(log_file)

    def test_mode_semantics_off(self, tmp_path):
        """AC7: off = block しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only no Japanese at all off mode.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="off", shadow_log_file=log_file)
        assert result.returncode == 0
        assert not os.path.exists(log_file)

    def test_mode_semantics_shadow(self, tmp_path):
        """AC7: shadow(legacy alias) = block しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only no Japanese at all.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="shadow", shadow_log_file=log_file)
        assert result.returncode == 0
        assert not os.path.exists(log_file)

    def test_mode_semantics_enforce(self, tmp_path):
        """AC7: enforce = block する。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text("English only no Japanese at all enforce test.", encoding="utf-8")
        command = f"gh issue create --body-file {body_file}"
        result = run_hook("Bash", command, env_mode="enforce", shadow_log_file=log_file)
        assert result.returncode == 2
        assert not os.path.exists(log_file)

    def test_mode_semantics_invalid_value_is_nonblocking_diagnostic(self, tmp_path):
        """AC7/AC8 (#2265 BLOCKER1 fix_delta): 不正値（例: 'dry-run'）は
        exit 1 の non-blocking diagnostic として動作する。

        PreToolUse hook contract 上、exit 1 は tool call を block しない
        （block するのは exit 2 のみ）。silent allow (exit 0) から
        visible-but-non-blocking diagnostic (exit 1) へ変更された。
        """
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
        # (a) returncode == 1
        assert result.returncode == 1, (
            f"invalid mode must be a non-blocking diagnostic (exit 1), "
            f"not silent-allow (exit 0) or block (exit 2): exit={result.returncode}"
        )
        # (b) exit 1 is distinguishable from the enforce-violation exit 2 path
        assert result.returncode != 2, "invalid mode must not use the block exit code"
        # (c) stderr contains the stable reason code string
        assert "invalid_guard_mode" in result.stderr, (
            f"stderr must contain stable reason code 'invalid_guard_mode': "
            f"{result.stderr!r}"
        )
        assert "dry-run" in result.stderr, (
            f"stderr must surface the offending mode value: {result.stderr!r}"
        )

    def test_mode_semantics_invalid_value_does_not_create_persistent_file(self, tmp_path):
        """AC7/AC8: 不正値のとき persistent file は生成されない（exit 1 でも変わらず）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "English only no Japanese. Invalid mode persistent file test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"
        result = run_hook(
            "Bash", command,
            shadow_log_file=log_file,
            extra_env={"GUARD_JAPANESE_PROSE_MODE": "invalid_value_xyz"},
        )

        # (d) no persistent file created as a side effect of this path
        assert not os.path.exists(log_file), (
            f"invalid mode で persistent file が生成されている: {log_file}"
        )
        assert result.returncode == 1
        assert "invalid_guard_mode" in result.stderr
