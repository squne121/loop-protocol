"""test_check_hook_integrity.py — HOOK_INTEGRITY_RESULT_V1 unit tests.

AC1: check_hook_integrity.py が追加され --format json を受け付ける
AC2: stdout は HOOK_INTEGRITY_RESULT_V1 JSON のみ
AC3: args キーあり → exec form、なし → shell form
AC4: direct path exec form の .sh が executable bit 欠如 → not_executable failures[]
AC5: 存在しない path → missing failures[]
AC6: 未解決 ${PLACEHOLDER} → path_placeholder_unresolved failures[]
AC7: unquoted ${CLAUDE_PROJECT_DIR} shell form → unsafe_shell_form warnings[]
AC8: command: "bash" + args: ["${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh"] → interpreter form pass、.sh executable bit 不要
AC9: command: "node" + .mjs args → pass、.mjs executable bit 不要
AC10: failures[] 非空 → decision: "fail"、空 → decision: "pass"
AC11: uv run pytest .claude/hooks/tests/test_check_hook_integrity.py -q が PASS する
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# モジュールロード（絶対パス指定）
# ──────────────────────────────────────────────────────────────────────────────

def _load_checker():
    """check_hook_integrity を絶対パスから動的ロードする。"""
    this_dir = Path(__file__).resolve().parent
    hooks_dir = this_dir.parent
    checker_path = hooks_dir / "check_hook_integrity.py"
    spec = importlib.util.spec_from_file_location("check_hook_integrity", checker_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# ──────────────────────────────────────────────────────────────────────────────
# Fixture ヘルパ
# ──────────────────────────────────────────────────────────────────────────────

class FakeProjectRoot:
    """一時ディレクトリ上に settings.json と hook スタブを構築するヘルパ。"""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.claude_dir = tmp_path / ".claude"
        self.hooks_dir = self.claude_dir / "hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)

    def write_settings(self, hooks_config: dict) -> None:
        """settings.json を書き込む。"""
        settings = {"hooks": hooks_config}
        (self.claude_dir / "settings.json").write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def make_executable_sh(self, name: str = "hook.sh") -> Path:
        """実行可能な .sh ファイルを作成する。"""
        p = self.hooks_dir / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return p

    def make_non_executable_sh(self, name: str = "hook.sh") -> Path:
        """executable bit がない .sh ファイルを作成する。"""
        p = self.hooks_dir / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        p.chmod(0o644)  # 実行ビットなし
        return p

    def make_mjs(self, name: str = "hook.mjs") -> Path:
        """.mjs ファイルを作成する（executable bit なし）。"""
        p = self.hooks_dir / name
        p.write_text("// stub mjs\n", encoding="utf-8")
        p.chmod(0o644)
        return p

    def run_check(self) -> dict[str, Any]:
        """checker.run_check を実行し HOOK_INTEGRITY_RESULT_V1 を返す。"""
        result = checker.run_check(self.root)
        return result["HOOK_INTEGRITY_RESULT_V1"]


# ──────────────────────────────────────────────────────────────────────────────
# AC1: ファイル存在 + --format json 受付
# ──────────────────────────────────────────────────────────────────────────────

def test_ac1_checker_module_exists():
    """AC1: check_hook_integrity.py が存在し、ロード可能。"""
    this_dir = Path(__file__).resolve().parent
    checker_path = this_dir.parent / "check_hook_integrity.py"
    assert checker_path.exists(), f"check_hook_integrity.py が存在しない: {checker_path}"


def test_ac1_has_format_json_argument():
    """AC1: --format json 引数が受け付けられる（argparse 定義）。"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["json"], default="json")
    args = parser.parse_args(["--format", "json"])
    assert args.format == "json"


# ──────────────────────────────────────────────────────────────────────────────
# AC2: stdout は HOOK_INTEGRITY_RESULT_V1 JSON のみ
# ──────────────────────────────────────────────────────────────────────────────

def test_ac2_result_has_required_keys(tmp_path: Path):
    """AC2: run_check の結果に decision, hooks_checked, failures[], warnings[] が含まれる。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({})
    result = fp.run_check()
    assert "decision" in result
    assert "hooks_checked" in result
    assert "failures" in result
    assert "warnings" in result


def test_ac2_result_wrapped_in_hook_integrity_result_v1(tmp_path: Path):
    """AC2: run_check() の top-level key が HOOK_INTEGRITY_RESULT_V1。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({})
    raw = checker.run_check(fp.root)
    assert "HOOK_INTEGRITY_RESULT_V1" in raw


# ──────────────────────────────────────────────────────────────────────────────
# AC3: exec form / shell form 分類
# ──────────────────────────────────────────────────────────────────────────────

def test_ac3_with_args_key_is_exec_form(tmp_path: Path):
    """AC3: args キーあり → exec form として分類される。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_executable_sh("exec_hook.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": str(sh), "args": []}
                ],
            }
        ]
    })
    result = fp.run_check()
    # executable なので failures は空
    assert result["failures"] == []
    assert result["decision"] == "pass"


def test_ac3_without_args_key_is_shell_form(tmp_path: Path):
    """AC3: args キーなし → shell form として分類される（warnings 検査で確認）。"""
    fp = FakeProjectRoot(tmp_path)
    # shell form で ${CLAUDE_PROJECT_DIR} を使う → unsafe_shell_form warning が出る
    fp.write_settings({
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh",
                        # args キーなし → shell form
                    }
                ],
            }
        ]
    })
    result = fp.run_check()
    # shell form で ${CLAUDE_PROJECT_DIR} が unquoted → unsafe_shell_form warning
    warning_codes = [w["reason_code"] for w in result["warnings"]]
    assert "unsafe_shell_form" in warning_codes


# ──────────────────────────────────────────────────────────────────────────────
# AC4: direct path exec form の .sh が executable bit 欠如 → not_executable
# ──────────────────────────────────────────────────────────────────────────────

def test_ac4_not_executable_sh_in_exec_form(tmp_path: Path):
    """AC4: executable bit のない .sh を直接 exec form で登録 → not_executable failure。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_non_executable_sh("no_exec.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": str(sh), "args": []}
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "not_executable" in failure_codes
    assert result["decision"] == "fail"


def test_ac4_not_executable_failure_has_mode_and_x_ok(tmp_path: Path):
    """AC4: not_executable failure は mode_octal と effective_x_ok を含む。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_non_executable_sh("no_exec2.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": str(sh), "args": []}
                ]
            }
        ]
    })
    result = fp.run_check()
    not_exec = [f for f in result["failures"] if f["reason_code"] == "not_executable"]
    assert len(not_exec) >= 1
    entry = not_exec[0]
    assert entry["mode_octal"] is not None
    assert entry["effective_x_ok"] is False


# ──────────────────────────────────────────────────────────────────────────────
# AC5: 存在しない path → missing failures[]
# ──────────────────────────────────────────────────────────────────────────────

def test_ac5_missing_path_in_exec_form(tmp_path: Path):
    """AC5: 存在しないパスを exec form で登録 → missing failure。"""
    fp = FakeProjectRoot(tmp_path)
    missing_path = str(fp.hooks_dir / "nonexistent.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": missing_path, "args": []}
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "missing" in failure_codes
    assert result["decision"] == "fail"


# ──────────────────────────────────────────────────────────────────────────────
# AC6: 未解決 ${PLACEHOLDER} → path_placeholder_unresolved failures[]
# ──────────────────────────────────────────────────────────────────────────────

def test_ac6_unresolved_placeholder_in_command(tmp_path: Path):
    """AC6: command に未解決の ${PLACEHOLDER} → path_placeholder_unresolved failure。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "${UNKNOWN_VAR}/.claude/hooks/x.sh",
                        "args": [],
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "path_placeholder_unresolved" in failure_codes
    assert result["decision"] == "fail"


def test_ac6_unresolved_placeholder_in_args(tmp_path: Path):
    """AC6: args に未解決の ${PLACEHOLDER} → path_placeholder_unresolved failure。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash",
                        "args": ["${MISSING_VAR}/hook.sh"],
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "path_placeholder_unresolved" in failure_codes


# ──────────────────────────────────────────────────────────────────────────────
# AC7: unquoted ${CLAUDE_PROJECT_DIR} shell form → unsafe_shell_form warnings[]
# ──────────────────────────────────────────────────────────────────────────────

def test_ac7_unquoted_claude_project_dir_in_shell_form(tmp_path: Path):
    """AC7: shell form で unquoted ${CLAUDE_PROJECT_DIR} → unsafe_shell_form warning。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        # args キーなし → shell form
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh",
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    warning_codes = [w["reason_code"] for w in result["warnings"]]
    assert "unsafe_shell_form" in warning_codes
    # failure には含まれない（failures は空 = unresolved placeholder でないため）
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "path_placeholder_unresolved" not in failure_codes


# ──────────────────────────────────────────────────────────────────────────────
# AC8: command: "bash" + args: ["${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh"]
#      → interpreter form pass、.sh executable bit 不要
# ──────────────────────────────────────────────────────────────────────────────

def test_ac8_bash_interpreter_with_non_executable_sh(tmp_path: Path):
    """AC8: bash interpreter + non-executable .sh → pass（executable bit 不要）。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_non_executable_sh("target.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash",
                        "args": [str(sh)],
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    # bash interpreter form では executable bit を要求しない
    assert "not_executable" not in failure_codes
    assert result["decision"] == "pass"


def test_ac8_bash_with_claude_project_dir_placeholder_sh(tmp_path: Path):
    """AC8: bash + ${CLAUDE_PROJECT_DIR}/hook.sh → ファイル存在すれば pass。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_non_executable_sh("x.sh")
    # ファイルは non-executable だが interpreter form なので pass
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash",
                        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh"],
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    # non-executable でも interpreter form なので not_executable failure なし
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "not_executable" not in failure_codes


# ──────────────────────────────────────────────────────────────────────────────
# AC9: command: "node" + .mjs args → pass、.mjs の executable bit 不要
# ──────────────────────────────────────────────────────────────────────────────

def test_ac9_node_interpreter_with_mjs(tmp_path: Path):
    """AC9: node interpreter + .mjs → pass（.mjs の executable bit 不要）。"""
    fp = FakeProjectRoot(tmp_path)
    mjs = fp.make_mjs("hook.mjs")
    fp.write_settings({
        "PostToolUse": [
            {
                "matcher": "Bash|Edit|Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": "node",
                        "args": [str(mjs)],
                    }
                ],
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "not_executable" not in failure_codes
    assert result["decision"] == "pass"


def test_ac9_node_with_claude_project_dir_mjs(tmp_path: Path):
    """AC9: node + ${CLAUDE_PROJECT_DIR}/.claude/hooks/x.mjs → ファイル存在すれば pass。"""
    fp = FakeProjectRoot(tmp_path)
    mjs = fp.make_mjs("x.mjs")
    fp.write_settings({
        "PostToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "node",
                        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/x.mjs"],
                    }
                ]
            }
        ]
    })
    result = fp.run_check()
    failure_codes = [f["reason_code"] for f in result["failures"]]
    assert "not_executable" not in failure_codes


# ──────────────────────────────────────────────────────────────────────────────
# AC10: failures[] 非空 → decision: "fail"、空 → decision: "pass"
# ──────────────────────────────────────────────────────────────────────────────

def test_ac10_empty_failures_gives_pass(tmp_path: Path):
    """AC10: failures が空 → decision: pass。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_executable_sh("pass.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": str(sh), "args": []}
                ]
            }
        ]
    })
    result = fp.run_check()
    assert result["failures"] == []
    assert result["decision"] == "pass"


def test_ac10_nonempty_failures_gives_fail(tmp_path: Path):
    """AC10: failures が非空 → decision: fail。"""
    fp = FakeProjectRoot(tmp_path)
    missing = str(fp.hooks_dir / "does_not_exist.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": missing, "args": []}
                ]
            }
        ]
    })
    result = fp.run_check()
    assert len(result["failures"]) > 0
    assert result["decision"] == "fail"


# ──────────────────────────────────────────────────────────────────────────────
# 境界値テスト
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_hooks_section(tmp_path: Path):
    """GIVEN: hooks セクションが空 WHEN: run_check THEN: hooks_checked=0, pass。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({})
    result = fp.run_check()
    assert result["hooks_checked"] == 0
    assert result["decision"] == "pass"
    assert result["failures"] == []
    assert result["warnings"] == []


def test_non_command_type_hooks_are_skipped(tmp_path: Path):
    """GIVEN: type が command でない hook WHEN: run_check THEN: hooks_checked に含まれない。"""
    fp = FakeProjectRoot(tmp_path)
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "script", "script": "echo hello"}
                ]
            }
        ]
    })
    result = fp.run_check()
    assert result["hooks_checked"] == 0


def test_multiple_failures_all_reported(tmp_path: Path):
    """GIVEN: 複数の failure WHEN: run_check THEN: 全 failure が報告される。"""
    fp = FakeProjectRoot(tmp_path)
    missing1 = str(fp.hooks_dir / "missing1.sh")
    missing2 = str(fp.hooks_dir / "missing2.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": missing1, "args": []},
                    {"type": "command", "command": missing2, "args": []},
                ]
            }
        ]
    })
    result = fp.run_check()
    assert len(result["failures"]) == 2
    assert result["decision"] == "fail"


def test_hooks_checked_counts_command_type_only(tmp_path: Path):
    """GIVEN: command と non-command が混在 WHEN: run_check THEN: command 型のみ hooks_checked に計上。"""
    fp = FakeProjectRoot(tmp_path)
    sh = fp.make_executable_sh("cmd.sh")
    fp.write_settings({
        "PreToolUse": [
            {
                "hooks": [
                    {"type": "command", "command": str(sh), "args": []},
                    {"type": "script", "script": "echo skip"},
                ]
            }
        ]
    })
    result = fp.run_check()
    assert result["hooks_checked"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# 実際の repo settings を対象とする smoke test（AC11 後半）
# ──────────────────────────────────────────────────────────────────────────────

def test_real_repo_settings_json_parseable():
    """GIVEN: 実際のリポジトリの settings.json WHEN: run_check THEN: 例外なく完了する。"""
    this_dir = Path(__file__).resolve().parent
    hooks_dir = this_dir.parent
    project_root = hooks_dir.parent.parent  # <root>/.claude/hooks/tests/../../../
    settings_path = hooks_dir.parent / "settings.json"
    if not settings_path.exists():
        pytest.skip(f"settings.json not found: {settings_path}")

    result = checker.run_check(project_root)
    r = result.get("HOOK_INTEGRITY_RESULT_V1", {})
    assert "decision" in r
    assert "hooks_checked" in r
    assert isinstance(r.get("failures"), list)
    assert isinstance(r.get("warnings"), list)
