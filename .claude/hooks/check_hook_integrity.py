#!/usr/bin/env python3
"""check_hook_integrity.py — HOOK_INTEGRITY_RESULT_V1 出力スクリプト。

`.claude/settings.json` に登録された全 hook command の実行可能性・パス解決を
deterministic に検査し、`HOOK_INTEGRITY_RESULT_V1` JSON を stdout に出力する。

Exit codes:
  0  — decision: pass（failures なし）
  1  — decision: fail（failures あり）
  2  — 実行エラー（settings.json 読み取り失敗等）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────────────────────────────────────

# exec form として扱う interpreter（PATH 解決可能なコマンド名）
_INTERPRETER_COMMANDS = {"bash", "sh", "zsh", "python", "python3", "node", "uv", "npx"}

# 未解決 placeholder パターン（既知 placeholder 以外の変数）
_KNOWN_PLACEHOLDERS = frozenset({"CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA"})
_PLACEHOLDER_RE = re.compile(
    r"\$\{(?!" + "|".join(re.escape(p) + r"\}" for p in sorted(_KNOWN_PLACEHOLDERS)) + r")[^}]+\}"
)

# shell form で unsafe な ${CLAUDE_PROJECT_DIR} のパターン（引用符なし）
_UNQUOTED_CLAUDE_PROJECT_DIR_RE = re.compile(
    r'(?<!["\'])\$\{CLAUDE_PROJECT_DIR\}(?!["\'])'
)


# ──────────────────────────────────────────────────────────────────────────────
# プロジェクトルート解決
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_project_root() -> Path:
    """プロジェクトルートを解決する。

    優先順位:
      1. CLAUDE_PROJECT_DIR 環境変数
      2. __file__ からの相対パス（<root>/.claude/hooks/check_hook_integrity.py）
    """
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        return Path(env_root).resolve()
    # __file__ = <root>/.claude/hooks/check_hook_integrity.py
    return Path(__file__).resolve().parent.parent.parent


# ──────────────────────────────────────────────────────────────────────────────
# settings.json パーサ
# ──────────────────────────────────────────────────────────────────────────────

def _load_settings(project_root: Path) -> dict[str, Any]:
    """`.claude/settings.json` を読み込む。"""
    settings_path = project_root / ".claude" / "settings.json"
    if not settings_path.exists():
        raise FileNotFoundError(f"settings.json not found: {settings_path}")
    with settings_path.open(encoding="utf-8") as f:
        return json.load(f)


def _iter_hook_entries(settings: dict[str, Any]):
    """settings.json の hooks セクションから全 hook entry を yield する。

    Yields (event_name, matcher, hook_entry) tuples.
    """
    hooks_section = settings.get("hooks", {})
    for event_name, event_entries in hooks_section.items():
        if not isinstance(event_entries, list):
            continue
        for event_item in event_entries:
            matcher = event_item.get("matcher")  # PreToolUse などはオプション
            inner_hooks = event_item.get("hooks", [])
            if not isinstance(inner_hooks, list):
                continue
            for hook in inner_hooks:
                if not isinstance(hook, dict):
                    continue
                yield event_name, matcher, hook


# ──────────────────────────────────────────────────────────────────────────────
# パス解決ヘルパ
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_placeholder(s: str, project_root: Path) -> str:
    """${CLAUDE_PROJECT_DIR} を project_root の絶対パスに置換する。"""
    return s.replace("${CLAUDE_PROJECT_DIR}", str(project_root))


def _has_unresolved_placeholder(s: str) -> bool:
    """未解決の ${PLACEHOLDER} が残っているか確認する。"""
    return bool(_PLACEHOLDER_RE.search(s))


def _mode_octal(path: str) -> str | None:
    """ファイルのパーミッション octal 文字列を返す。存在しない場合は None。"""
    try:
        st = os.stat(path)
        return oct(stat.S_IMODE(st.st_mode))
    except OSError:
        return None


def _effective_x_ok(path: str) -> bool | None:
    """実効的な実行権限を返す。存在しない場合は None。"""
    if not os.path.exists(path):
        return None
    return os.access(path, os.X_OK)


# ──────────────────────────────────────────────────────────────────────────────
# 単一 hook entry の検査
# ──────────────────────────────────────────────────────────────────────────────

def _check_hook(
    event_name: str,
    matcher: str | None,
    hook: dict[str, Any],
    project_root: Path,
) -> tuple[list[dict], list[dict]]:
    """単一 hook entry を検査し (failures, warnings) を返す。"""
    failures: list[dict] = []
    warnings: list[dict] = []

    hook_type = hook.get("type")
    if hook_type != "command":
        # command 以外の type は対象外（スキップ）
        return failures, warnings

    command_raw: str = hook.get("command", "")
    args_raw: list[str] | None = hook.get("args")  # args key 存在 → exec form

    # exec / shell form 分類（args key の有無で判定）
    has_args_key = args_raw is not None
    command_form = "exec" if has_args_key else "shell"

    # ${CLAUDE_PROJECT_DIR} を解決
    command_resolved = _resolve_placeholder(command_raw, project_root)
    args_resolved = (
        [_resolve_placeholder(a, project_root) for a in args_raw]
        if args_raw is not None
        else None
    )

    def _base_entry(reason_code: str, subject_path: str | None = None) -> dict:
        entry: dict[str, Any] = {
            "reason_code": reason_code,
            "hook_event": event_name,
            "matcher": matcher,
            "command_form": command_form,
            "command": command_raw,
            "args": args_raw,
            "resolved_command": command_resolved,
            "resolved_args": args_resolved,
            "subject_path": subject_path,
            "mode_octal": None,
            "effective_x_ok": None,
            "remediation": "",
        }
        return entry

    # ── 未解決 placeholder チェック ──────────────────────────────────────────
    if _has_unresolved_placeholder(command_raw):
        e = _base_entry("path_placeholder_unresolved", command_raw)
        e["remediation"] = (
            f"Resolve placeholder in command: {command_raw!r}"
        )
        failures.append(e)
        return failures, warnings

    if args_raw is not None:
        for arg in args_raw:
            if _has_unresolved_placeholder(arg):
                e = _base_entry("path_placeholder_unresolved", arg)
                e["remediation"] = (
                    f"Resolve placeholder in args: {arg!r}"
                )
                failures.append(e)
                return failures, warnings

    # ── exec form 検査 ───────────────────────────────────────────────────────
    if command_form == "exec":
        cmd_is_path = "/" in command_resolved or command_resolved.startswith(".")
        cmd_is_interpreter = command_resolved in _INTERPRETER_COMMANDS

        if cmd_is_path:
            # direct path exec: 存在・実行可能性を検査
            _check_direct_exec(
                event_name, matcher, command_form, command_raw, command_resolved,
                args_raw, args_resolved,
                subject_path=command_resolved,
                failures=failures,
                project_root=project_root,
            )
        elif cmd_is_interpreter:
            # interpreter form: command 自体は PATH 解決可能前提
            # まず interpreter が PATH で見つかるか検査する
            if shutil.which(command_resolved) is None:
                e = _base_entry("interpreter_missing", command_resolved)
                e["remediation"] = (
                    f"Install interpreter or fix PATH: {command_resolved!r}"
                )
                failures.append(e)
                return failures, warnings
            # args 内のパスが存在するかを検査（executable bit 不要）
            if args_resolved:
                for arg_resolved, arg_raw in zip(args_resolved, args_raw or []):
                    if "/" in arg_resolved or arg_resolved.startswith("."):
                        # パスとして解釈
                        if not os.path.exists(arg_resolved):
                            e = _base_entry("missing", arg_resolved)
                            e["remediation"] = (
                                f"Interpreter arg path does not exist: {arg_resolved!r}"
                            )
                            failures.append(e)
        else:
            # interpreter でも direct path でもない → PATH で解決
            resolved_cmd = shutil.which(command_resolved)
            if resolved_cmd is None:
                e = _base_entry("missing", command_resolved)
                e["remediation"] = (
                    f"Command not found in PATH: {command_resolved!r}"
                )
                failures.append(e)

    # ── shell form 検査 ──────────────────────────────────────────────────────
    else:
        # ${CLAUDE_PROJECT_DIR} を unquoted で使っている場合 → warning
        if _UNQUOTED_CLAUDE_PROJECT_DIR_RE.search(command_raw):
            w = _base_entry("unsafe_shell_form", command_raw)
            w["remediation"] = (
                "Quote ${CLAUDE_PROJECT_DIR} in shell form command, "
                "or use exec form (add args key)."
            )
            warnings.append(w)

    return failures, warnings


def _check_direct_exec(
    event_name: str,
    matcher: str | None,
    command_form: str,
    command_raw: str,
    command_resolved: str,
    args_raw: list[str] | None,
    args_resolved: list[str] | None,
    subject_path: str,
    failures: list[dict],
    project_root: Path,
) -> None:
    """Direct path exec form の存在・実行可能性を検査する。"""
    def _base_entry(reason_code: str, sp: str | None = None) -> dict:
        return {
            "reason_code": reason_code,
            "hook_event": event_name,
            "matcher": matcher,
            "command_form": command_form,
            "command": command_raw,
            "args": args_raw,
            "resolved_command": command_resolved,
            "resolved_args": args_resolved,
            "subject_path": sp or subject_path,
            "mode_octal": None,
            "effective_x_ok": None,
            "remediation": "",
        }

    if not os.path.exists(subject_path):
        e = _base_entry("missing")
        e["remediation"] = f"File does not exist: {subject_path!r}"
        failures.append(e)
        return

    # executable bit チェック
    x_ok = os.access(subject_path, os.X_OK)
    mode = _mode_octal(subject_path)
    if not x_ok:
        e = _base_entry("not_executable")
        e["mode_octal"] = mode
        e["effective_x_ok"] = False
        e["remediation"] = f"Run: chmod +x {subject_path!r}"
        failures.append(e)


# ──────────────────────────────────────────────────────────────────────────────
# メインロジック
# ──────────────────────────────────────────────────────────────────────────────

def run_check(project_root: Path) -> dict[str, Any]:
    """全 hook を検査し HOOK_INTEGRITY_RESULT_V1 dict を返す。"""
    settings = _load_settings(project_root)
    all_failures: list[dict] = []
    all_warnings: list[dict] = []
    hooks_checked = 0

    for event_name, matcher, hook in _iter_hook_entries(settings):
        if hook.get("type") != "command":
            continue
        hooks_checked += 1
        f, w = _check_hook(event_name, matcher, hook, project_root)
        all_failures.extend(f)
        all_warnings.extend(w)

    decision = "fail" if all_failures else "pass"

    return {
        "HOOK_INTEGRITY_RESULT_V1": {
            "decision": decision,
            "hooks_checked": hooks_checked,
            "failures": all_failures,
            "warnings": all_warnings,
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check .claude/settings.json hook integrity."
    )
    parser.add_argument(
        "--format",
        choices=["json"],
        default="json",
        help="Output format (currently only 'json' is supported)",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Path to project root (default: auto-detect)",
    )
    args = parser.parse_args()

    project_root = (
        Path(args.project_root).resolve()
        if args.project_root
        else _resolve_project_root()
    )

    try:
        result = run_check(project_root)
    except Exception as exc:
        error_result = {
            "HOOK_INTEGRITY_RESULT_V1": {
                "decision": "fail",
                "hooks_checked": 0,
                "failures": [
                    {
                        "reason_code": "checker_error",
                        "hook_event": None,
                        "matcher": None,
                        "command_form": None,
                        "command": None,
                        "args": None,
                        "resolved_command": None,
                        "resolved_args": None,
                        "subject_path": None,
                        "mode_octal": None,
                        "effective_x_ok": None,
                        "remediation": str(exc),
                    }
                ],
                "warnings": [],
            }
        }
        print(json.dumps(error_result, ensure_ascii=False, indent=2))
        return 2

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))

    result_v1 = result.get("HOOK_INTEGRITY_RESULT_V1", {})
    return 0 if result_v1.get("decision") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
