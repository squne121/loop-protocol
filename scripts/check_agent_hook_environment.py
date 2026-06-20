#!/usr/bin/env python3
"""
check_agent_hook_environment.py

hook 実行環境の破損を fast-fail で検出するスクリプト。
issue-refinement-loop や agent-ops-review の開始前に呼ぶ。

Exit codes:
  0 — ok または warn（環境正常または minor 警告のみ）
  1 — blocked または environment_failure（環境破損）

Usage:
  uv run python3 scripts/check_agent_hook_environment.py --repo-root .

AC1: /bin/sh が存在しない → {"status": "blocked", "reason": "bin_sh_missing", ...}
AC2: cwd が存在しない → {"status": "environment_failure", "reason": "cwd_missing", ...}
AC3: hook command が解決できない → handler_id と event を含む JSON
AC4: blocker hook と telemetry hook の failure を分類
AC5: stdout は compact JSON のみ。raw command body を含まない
AC6: check_hook_boundaries.py と競合しない。manifest drift は path reference のみ
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


# ─── hook classification ──────────────────────────────────────────────────────

# exit 2 = block effect がある event（AC4）
BLOCKER_EVENTS = {"PreToolUse", "Stop", "SubagentStop", "PreCompact"}
# exit 2 でも tool 完了後のため blocking 効果なし（PostToolUse は prevent-before-execution 不可）
TELEMETRY_EVENTS = {"PostToolUse"}


def classify_hook(event: str) -> str:
    """event 名から hook_class を返す（AC4）。unknown event は fail-closed で "unknown" 扱い。"""
    if event in BLOCKER_EVENTS:
        return "blocker"
    if event in TELEMETRY_EVENTS:
        return "telemetry"
    return "unknown"


# ─── handler_id 解決 ──────────────────────────────────────────────────────────

def resolve_handler_id(hook: dict[str, Any]) -> str:
    """
    hook dict から handler_id を解決する。

    check_hook_boundaries.py の resolve_handler_id と同一ロジックを使用する。
    node ラッパーパターン（PostToolUse 等）に対応。
    """
    command: str = hook.get("command", "")
    args: list[str] = hook.get("args", [])

    if Path(command).name == "node":
        if not args:
            return "__node_no_args__"
        script_path = args[0]
        return Path(script_path).stem

    return Path(command).stem


def resolve_command_path(hook: dict[str, Any], repo_root: Path) -> Path:
    """
    hook の実際の実行スクリプトパスを解決する。

    ${CLAUDE_PROJECT_DIR} を repo_root の絶対パスに置換する。
    node ラッパーの場合は args[0] を解決対象とする。
    """
    command: str = hook.get("command", "")
    args: list[str] = hook.get("args", [])
    repo_root_str = str(repo_root.resolve())

    if Path(command).name == "node":
        if not args:
            return repo_root / "__node_no_args__"
        script_template = args[0]
    else:
        script_template = command

    resolved = script_template.replace("${CLAUDE_PROJECT_DIR}", repo_root_str)
    return Path(resolved)


def _is_node_hook(hook: dict[str, Any]) -> bool:
    """hook が node ラッパーパターン（command: "node", args: [script]）かを判定する。"""
    command: str = hook.get("command", "")
    return Path(command).name == "node"


# ─── チェック関数 ─────────────────────────────────────────────────────────────

def check_bin_sh() -> dict[str, Any]:
    """AC1: /bin/sh の存在確認。"""
    bin_sh = Path("/bin/sh")
    if bin_sh.exists():
        return {"status": "ok"}
    return {"status": "missing"}


def check_cwd() -> dict[str, Any]:
    """AC2: cwd が存在するか確認。"""
    try:
        cwd = os.getcwd()
        cwd_path = Path(cwd)
        if cwd_path.exists():
            return {"status": "ok", "path": cwd}
        return {"status": "missing", "path": cwd}
    except FileNotFoundError:
        return {"status": "missing", "path": "<deleted>"}
    except OSError:
        return {"status": "missing", "path": "<error>"}


def check_hooks(settings: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    """
    AC3 / AC4: hook command の解決と分類。

    - ${CLAUDE_PROJECT_DIR} を repo_root の絶対パスに置換してスクリプトを解決
    - スクリプトの存在・実行可能性・symlink escape を確認
    - node ラッパー（command: "node"）の場合は script が readable であれば ok（executable 不要）
    - AC5: command_ref は script path のみ（raw command body 非掲載）
    - AC6: manifest drift は manifest_drift_ref フィールドで参照のみ
    """
    hooks_section: dict[str, Any] = settings.get("hooks", {})
    result: list[dict[str, Any]] = []

    for event, event_entries in hooks_section.items():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            hooks_list: list[dict[str, Any]] = entry.get("hooks", [])
            for hook in hooks_list:
                if hook.get("type") != "command":
                    continue

                handler_id = resolve_handler_id(hook)
                hook_class = classify_hook(event)
                script_path = resolve_command_path(hook, repo_root)
                is_node = _is_node_hook(hook)

                hook_status = _check_script_status(script_path, repo_root, is_node_script=is_node)

                # AC5: command_ref はパスのみ、raw command body を含まない
                if hook_status != "symlink_escape":
                    command_ref = str(script_path)
                else:
                    command_ref = "<redacted - symlink escape detected>"

                result.append(
                    {
                        "event": event,
                        "handler_id": handler_id,
                        "hook_class": hook_class,
                        "status": hook_status,
                        "command_ref": command_ref,
                    }
                )

    return result


def _check_script_status(
    script_path: Path,
    repo_root: Path,
    *,
    is_node_script: bool = False,
) -> str:
    """スクリプトの存在・実行可能性・symlink escape をチェック。

    is_node_script=True の場合は executable チェックをスキップ（node が executable であれば足りる）。
    symlink escape は resolve 後の path relation で判定し、startswith による sibling-prefix bypass を防ぐ。
    """
    # symlink escape チェック: resolve 後の path relation で判定
    if script_path.is_symlink():
        try:
            real = script_path.resolve()
            repo_real = repo_root.resolve()
            if real != repo_real and not real.is_relative_to(repo_real):
                return "symlink_escape"
        except OSError:
            return "missing"

    if not script_path.exists():
        return "missing"

    if not script_path.is_file():
        return "not_executable"

    # node ラッパーは node 自体が executable。script は readable であれば足りる。
    if not is_node_script and not os.access(str(script_path), os.X_OK):
        return "not_executable"

    return "ok"


# ─── メイン ──────────────────────────────────────────────────────────────────

def run_checks(
    repo_root: Path,
    _cwd_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """全チェックを実行し、結果 dict を返す。

    _cwd_result: main() が早期実行した check_cwd() の結果を渡すと再実行を省略する。
    """
    bin_sh_result = check_bin_sh()
    cwd_result = _cwd_result if _cwd_result is not None else check_cwd()

    settings_path = repo_root / ".claude" / "settings.json"
    hooks_results: list[dict[str, Any]] = []
    settings_error: str | None = None

    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            hooks_results = check_hooks(settings, repo_root)
        except (json.JSONDecodeError, OSError) as e:
            settings_error = str(e)
    else:
        settings_error = f"{settings_path} not found"

    overall_status = _determine_status(bin_sh_result, cwd_result, hooks_results, settings_error)

    result: dict[str, Any] = {
        "status": overall_status,
        "checks": {
            "bin_sh": bin_sh_result,
            "cwd": cwd_result,
            "hooks": hooks_results,
        },
        # AC6: manifest drift は check_hook_boundaries.py に委譲（path reference のみ）
        "manifest_drift_ref": "run scripts/check_hook_boundaries.py for manifest diff",
    }

    # AC1/AC2: top-level reason を追加（downstream consumer が documented schema で分岐できるよう）
    if overall_status == "blocked" and bin_sh_result.get("status") == "missing":
        result["reason"] = "bin_sh_missing"
    elif overall_status == "environment_failure" and cwd_result.get("status") == "missing":
        result["reason"] = "cwd_missing"

    if settings_error:
        result["settings_error"] = settings_error

    return result


def _determine_status(
    bin_sh: dict[str, Any],
    cwd: dict[str, Any],
    hooks: list[dict[str, Any]],
    settings_error: str | None,
) -> str:
    """AC1-4 に基づき総合 status を決定する。"""
    # AC1: /bin/sh missing → blocked
    if bin_sh.get("status") == "missing":
        return "blocked"

    # AC2: cwd missing → environment_failure
    if cwd.get("status") == "missing":
        return "environment_failure"

    # AC3/4: blocker または unknown hook の failure → blocked（fail-closed）
    for hook in hooks:
        hook_class = hook.get("hook_class", "unknown")
        if hook_class in ("blocker", "unknown") and hook.get("status") != "ok":
            return "blocked"

    # telemetry hook failure のみ → warn
    for hook in hooks:
        if hook.get("status") != "ok":
            return "warn"

    if settings_error:
        return "warn"

    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="hook 実行環境の破損を fast-fail で検出する"
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="リポジトリルートのパス（デフォルト: カレントディレクトリ）",
    )
    args = parser.parse_args()

    # AC2: cwd チェックを Path.resolve() より前に実行
    # deleted cwd では Path(".").resolve() が内部で getcwd() を呼び OSError になる
    cwd_result = check_cwd()
    if cwd_result["status"] == "missing":
        out: dict[str, Any] = {
            "status": "environment_failure",
            "reason": "cwd_missing",
            "checks": {
                "bin_sh": check_bin_sh(),
                "cwd": cwd_result,
                "hooks": [],
            },
            "manifest_drift_ref": "run scripts/check_hook_boundaries.py for manifest diff",
        }
        print(json.dumps(out, separators=(",", ":")))
        return 1

    repo_root = Path(args.repo_root).resolve()
    result = run_checks(repo_root, _cwd_result=cwd_result)

    # AC5: stdout は compact JSON のみ
    print(json.dumps(result, separators=(",", ":")))

    status = result.get("status", "ok")
    if status in ("blocked", "environment_failure"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
