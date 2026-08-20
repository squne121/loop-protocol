"""test_guard_shadow_removal.py

guard-japanese-prose.sh の persistent shadow logging 廃止（#2265）を検証する。

AC3: legacy `shadow` 設定値は `off` の compatibility alias として動作し、
     persistent file を生成しない
AC6: GUARD_JAPANESE_PROSE_SHADOW_LOG 環境変数で出力先を上書きした場合でも、
     一切 write が発生しない
AC7: stale な .guard_shadow_log.jsonl を事前配置した状態で hook を実行しても、
     そのファイルの内容/ハッシュが変化しない（=書き込まれない）
AC8: invalid mode 設定時、tool 実行は allow され（non-blocking diagnostic）、
     かつ persistent file を生成しない
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent.parent / "guard-japanese-prose.sh"
PROJECT_DIR = Path(__file__).parent.parent.parent.parent


def run_hook(
    tool_name: str,
    command: str | None = None,
    *,
    env_mode: str | None = None,
    shadow_log_file: str | None = None,
    extra_env: dict | None = None,
    cwd: str | None = None,
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
        cwd=cwd,
    )


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# AC3: legacy shadow は off の compatibility alias
# ---------------------------------------------------------------------------

class TestLegacyShadowAlias:
    def test_legacy_shadow_alias_no_persistent_file(self, tmp_path):
        """AC3: GUARD_JAPANESE_PROSE_MODE=shadow は off と同一挙動で、
        persistent file を生成しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "This is entirely English prose used to trigger the would-deny "
            "route in legacy shadow alias mode. No Japanese here at all.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        result_shadow = run_hook(
            "Bash", command, env_mode="shadow", shadow_log_file=log_file
        )
        result_off = run_hook(
            "Bash", command, env_mode="off", shadow_log_file=log_file
        )

        assert result_shadow.returncode == result_off.returncode == 0, (
            "legacy shadow alias と off の exit code は一致するはず: "
            f"shadow={result_shadow.returncode} off={result_off.returncode}"
        )
        assert not os.path.exists(log_file), (
            "legacy shadow alias でも persistent file を生成してはならない"
        )


# ---------------------------------------------------------------------------
# AC6: GUARD_JAPANESE_PROSE_SHADOW_LOG override でも write しない
# ---------------------------------------------------------------------------

class TestShadowLogEnvOverride:
    def test_shadow_log_env_override(self, tmp_path):
        """AC6: GUARD_JAPANESE_PROSE_SHADOW_LOG で出力先を上書きしても write が発生しない。"""
        override_dir = tmp_path / "custom_shadow_dir"
        override_dir.mkdir()
        override_path = str(override_dir / "override.jsonl")

        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English prose, no Japanese whatsoever, override env var test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command, env_mode="shadow", shadow_log_file=override_path
        )
        assert result.returncode == 0
        assert not os.path.exists(override_path), (
            "GUARD_JAPANESE_PROSE_SHADOW_LOG で指定した出力先にも write してはならない"
        )
        assert list(override_dir.iterdir()) == [], (
            "override 出力先ディレクトリに何らかのファイルが生成されている"
        )


# ---------------------------------------------------------------------------
# AC7: stale な .guard_shadow_log.jsonl を事前配置しても変化しない
# ---------------------------------------------------------------------------

class TestStaleShadowLogUnchanged:
    def test_stale_shadow_log_unchanged(self, tmp_path):
        """AC7: stale な .guard_shadow_log.jsonl を事前配置しても内容/ハッシュが変化しない。"""
        stale_path = tmp_path / ".guard_shadow_log.jsonl"
        stale_content = '{"schema_version": "1", "stale": true}\n'
        stale_path.write_text(stale_content, encoding="utf-8")
        stale_hash_before = sha256_of(stale_path)
        stale_mtime_before = stale_path.stat().st_mtime_ns

        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English prose, no Japanese whatsoever, stale file test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        # デフォルト出力先（GUARD_JAPANESE_PROSE_SHADOW_LOG 未設定時の
        # ${PROJECT_DIR}/.guard_shadow_log.jsonl 相当）を模した stale_path を
        # 明示的な override 先として使う（production repo 直下を汚さないため）。
        result = run_hook(
            "Bash", command, env_mode="shadow", shadow_log_file=str(stale_path)
        )
        assert result.returncode == 0

        stale_hash_after = sha256_of(stale_path)
        stale_mtime_after = stale_path.stat().st_mtime_ns
        stale_content_after = stale_path.read_text(encoding="utf-8")

        assert stale_hash_before == stale_hash_after, (
            "stale な shadow log ファイルのハッシュが変化した（書き込みが発生した）"
        )
        assert stale_mtime_before == stale_mtime_after, (
            "stale な shadow log ファイルの mtime が変化した（書き込みが発生した）"
        )
        assert stale_content == stale_content_after

    def test_production_shadow_log_not_touched(self, tmp_path):
        """production repo-root の .guard_shadow_log.jsonl が新規作成されない。

        GUARD_JAPANESE_PROSE_SHADOW_LOG を明示指定せず、hook を PROJECT_DIR
        （worktree ルート）で実行しても production 側ファイルへ書き込まない
        ことを確認する。
        """
        production_path = PROJECT_DIR / ".guard_shadow_log.jsonl"
        existed_before = production_path.exists()
        mtime_before = production_path.stat().st_mtime_ns if existed_before else None

        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English prose, no Japanese whatsoever, production path test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command, env_mode="shadow", cwd=str(PROJECT_DIR)
        )
        assert result.returncode == 0

        existed_after = production_path.exists()
        assert existed_before == existed_after, (
            "production .guard_shadow_log.jsonl の存在状態が変化した（書き込みが発生した）"
        )
        if existed_before:
            mtime_after = production_path.stat().st_mtime_ns
            assert mtime_before == mtime_after, (
                "production .guard_shadow_log.jsonl の mtime が変化した"
            )


# ---------------------------------------------------------------------------
# AC8: invalid mode は allow され persistent file を生成しない
# ---------------------------------------------------------------------------

class TestInvalidMode:
    def test_invalid_mode(self, tmp_path):
        """AC8: invalid mode 設定時、tool 実行は allow され persistent file を生成しない。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "All English prose, no Japanese whatsoever, invalid mode test.",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            shadow_log_file=log_file,
            extra_env={"GUARD_JAPANESE_PROSE_MODE": "not_a_real_mode"},
        )
        assert result.returncode == 0, (
            f"invalid mode must allow (non-blocking diagnostic): exit={result.returncode}"
        )
        assert not os.path.exists(log_file), (
            "invalid mode で persistent file が生成されている"
        )

    def test_invalid_mode_japanese_body_also_allowed(self, tmp_path):
        """invalid mode: 日本語 body も allow される（enforce ではない）。"""
        log_file = str(tmp_path / "shadow.jsonl")
        body_file = tmp_path / "body.md"
        body_file.write_text(
            "これは日本語で書かれた本文です。invalid mode でも通過します。",
            encoding="utf-8",
        )
        command = f"gh issue create --body-file {body_file}"

        result = run_hook(
            "Bash", command,
            shadow_log_file=log_file,
            extra_env={"GUARD_JAPANESE_PROSE_MODE": "not_a_real_mode"},
        )
        assert result.returncode == 0
        assert not os.path.exists(log_file)
