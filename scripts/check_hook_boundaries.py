#!/usr/bin/env python3
"""
check_hook_boundaries.py

docs/dev/hook-boundaries.md の hook_boundaries_manifest_v1 YAML block と
.claude/settings.json の hooks topology を構造照合し drift を検出する。

照合キーは (handler_id, event) の複合キー。
同一スクリプトが複数 event（Stop / SubagentStop 等）に配置される場合は別エントリとして扱う。

Exit codes:
  0 — manifest と settings.json が一致（drift なし）
  1 — drift 検出 または パース失敗
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


# ─── パス定数 ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
DOCS_PATH = REPO_ROOT / "docs" / "dev" / "hook-boundaries.md"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
CODEX_HOOKS_PATH = REPO_ROOT / ".codex" / "hooks.json"

# Issue #1289: name of the shared fast-path classifier library. It must NEVER
# appear as its own PreToolUse hook command in either .claude/settings.json or
# .codex/hooks.json (it is imported by existing guards, not registered as an
# independent hook).
_FASTPATH_CLASSIFIER_MODULE_NAME = "pretool_fastpath_classifier"
_CHECK_CODEX_AGENTS_PRETOOL = (
    'rtk pnpm exec node "$(git rev-parse --show-toplevel)/scripts/check-codex-agents.mjs" '
    "--hook-pretool"
)
_SESSION_RECORDING_PRETOOL = (
    'rtk pnpm exec node "$(git rev-parse --show-toplevel)/.codex/hooks/session-recording-composite.mjs" '
    "--event PreToolUse"
)

# Issue #1367: fixed, fail-closed expected snapshot of .codex/hooks.json
# PreToolUse hook entries. Drift in order, command, timeout, statusMessage,
# type, or extra handler fields must fail closed.
EXPECTED_CODEX_PRETOOL_TOPOLOGY: dict[str, list[dict[str, Any]]] = {
    "^Bash$": [
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/local_main_branch_guard.sh"',
            "timeout": 10,
            "statusMessage": "Checking local root branch policy",
        },
        {
            "type": "command",
            "command": 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/worktree_scope_guard.py"',
            "timeout": 20,
            "statusMessage": "Checking worktree cleanup scope policy (shared core)",
        },
        {
            "type": "command",
            "command": _CHECK_CODEX_AGENTS_PRETOOL,
            "timeout": 30,
            "statusMessage": "Checking LOOP_PROTOCOL Bash guardrail",
        },
        {
            "type": "command",
            "command": _SESSION_RECORDING_PRETOOL,
            "timeout": 30,
            "statusMessage": "Checking Codex session-recording PreToolUse guard",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking CI/test-lane path advisory",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking root temporary residue advisory",
        },
    ],
    "^(apply_patch|Edit|Write)$": [
        {
            "type": "command",
            "command": 'python3 "$(git rev-parse --show-toplevel)/scripts/agent-guards/codex_apply_patch_adapter.py"',
            "timeout": 20,
            "statusMessage": "Checking worktree containment for apply_patch/Edit/Write (shared core)",
        },
        {
            "type": "command",
            "command": _CHECK_CODEX_AGENTS_PRETOOL,
            "timeout": 30,
            "statusMessage": "Checking LOOP_PROTOCOL patch guardrail",
        },
        {
            "type": "command",
            "command": _SESSION_RECORDING_PRETOOL,
            "timeout": 30,
            "statusMessage": "Checking Codex session-recording patch guard",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/ci_test_performance_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking CI/test-lane path advisory",
        },
        {
            "type": "command",
            "command": 'bash "$(git rev-parse --show-toplevel)/.codex/hooks/root_temporary_residue_advisory.sh"',
            "timeout": 10,
            "statusMessage": "Checking root temporary residue advisory",
        },
    ],
}

# ─── manifest 抽出 ────────────────────────────────────────────────────────────

MANIFEST_PATTERN = re.compile(
    r"```yaml\s*\n(hook_boundaries_manifest_v1:.*?)```",
    re.DOTALL,
)

# B2: classification の許可語彙
VALID_CLASSIFICATIONS = {"blocker", "telemetry", "warning", "mode_dependent"}

# B5: event ごとの期待 exit_2_effect
EVENT_EXIT_2_EFFECT: dict[str, str] = {
    "PreToolUse": "blocks_tool_call",
    "Stop": "prevents_stop",
    "SubagentStop": "prevents_subagent_stop",
    "PreCompact": "blocks_compaction",
    "PostToolUse": "cannot_block_completed_tool_call",
}

STALE_NARRATIVE_PATTERNS = (
    "`generate_session_manifest_from_hook.mjs` | telemetry | 継続 |",
    "`generate_session_manifest_from_hook.mjs`（PostToolUse）",
)

REQUIRED_NARRATIVE_SNIPPETS = (
    "`session_manifest_debounce.mjs` | telemetry | 継続 |",
    "`session_manifest_debounce.mjs`（PostToolUse front gate）",
    "`generate_session_manifest_from_hook.mjs` は Stop/SubagentStop producer / debounce worker downstream",
)


def extract_manifest(docs_text: str) -> list[dict[str, Any]]:
    """docs から hook_boundaries_manifest_v1 YAML block を抽出してパースする。"""
    match = MANIFEST_PATTERN.search(docs_text)
    if not match:
        raise ValueError("hook_boundaries_manifest_v1 YAML block が docs に見つかりません")
    raw_yaml = match.group(1)
    parsed = yaml.safe_load(raw_yaml)
    entries = parsed.get("hook_boundaries_manifest_v1")
    if not isinstance(entries, list):
        raise ValueError("hook_boundaries_manifest_v1 の値がリストではありません")
    return entries


# ─── settings.json 解析 ───────────────────────────────────────────────────────

def resolve_handler_id(hook: dict[str, Any]) -> str:
    """
    hook dict から handler_id を解決する。

    AC6: command が "node" の場合は args[0] のファイル名を使う
    （PostToolUse の node ラッパーを取り逃がさない）。

    handler_id はスクリプトのファイル名（拡張子なし）をそのまま使用する。
    ハイフンを含む場合もファイル名通りに保持する（例: guard-japanese-prose）。
    """
    command: str = hook.get("command", "")
    args: list[str] = hook.get("args", [])

    # node ラッパーパターン: command が "node" かつ args[0] がスクリプトパス
    # Path(command).name が "node" であることを確認（フルパスの場合も含む）
    if Path(command).name == "node":
        if not args:
            # B1 guard: node wrapper without args はエラーとして扱う（caller で検出）
            return "__node_no_args__"
        script_path = args[0]
        # パス展開変数を除去してファイル名を取得
        stem = Path(script_path).stem
        return stem

    # 通常パターン: command パスのファイル名（拡張子なし）を handler_id とする
    stem = Path(command).stem
    return stem


def extract_settings_hooks(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """
    settings.json の hooks セクションを正規化されたリストに変換する。

    返却フォーマット（per hook）:
      event, matcher, command, args, timeout, handler_id, type

    照合キー: (handler_id, event) の複合キーを使用する。
    """
    hooks_section: dict[str, Any] = settings.get("hooks", {})
    result: list[dict[str, Any]] = []

    for event, event_entries in hooks_section.items():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            matcher = entry.get("matcher")  # Stop/PreCompact/SubagentStop では null
            hooks_list: list[dict[str, Any]] = entry.get("hooks", [])
            for hook in hooks_list:
                handler_id = resolve_handler_id(hook)
                result.append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "command": hook.get("command", ""),
                        "args": hook.get("args", []),
                        "timeout": hook.get("timeout"),
                        "type": hook.get("type"),
                        "handler_id": handler_id,
                    }
                )
    return result


# ─── duplicate 検出 (B1) ──────────────────────────────────────────────────────

def detect_duplicates_in_manifest(
    manifest_entries: list[dict[str, Any]],
) -> list[str]:
    """
    B1: manifest 内の duplicate (handler_id, event) を検出する。

    session_manifest_coordinator は Stop / SubagentStop に正当に複数配置されるが、
    同一 (handler_id, event) は重複として fail-closed にする。
    """
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for idx, me in enumerate(manifest_entries):
        hid = me.get("handler_id", "")
        event = me.get("event", "")
        key = (hid, event)
        if key in seen:
            errors.append(
                f"[duplicate:manifest] (handler_id={hid!r}, event={event!r}) が "
                f"index {seen[key]} と index {idx} の両方に存在します"
            )
        else:
            seen[key] = idx
    return errors


def detect_duplicates_in_settings(
    settings_entries: list[dict[str, Any]],
) -> list[str]:
    """
    B1: settings 内の duplicate (handler_id, event) を検出する。

    session_manifest_coordinator は Stop / SubagentStop に正当に複数配置されるが、
    同一 (handler_id, event) は重複として fail-closed にする。
    """
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for idx, se in enumerate(settings_entries):
        hid = se.get("handler_id", "")
        event = se.get("event", "")
        key = (hid, event)
        if key in seen:
            errors.append(
                f"[duplicate:settings] (handler_id={hid!r}, event={event!r}) が "
                f"index {seen[key]} と index {idx} の両方に存在します"
            )
        else:
            seen[key] = idx
    return errors


# ─── manifest schema validation (B2, B5, B6) ─────────────────────────────────

def validate_manifest_schema(
    manifest_entries: list[dict[str, Any]],
) -> list[str]:
    """
    B2/B5/B6: manifest の各 entry に対してスキーマ検証を行う。

    - classification の vocabulary チェック（B2）
    - blocker の必須フィールドチェック（B2）
    - telemetry の必須フィールドチェック（B2）
    - mode_dependent の必須フィールドチェック（B2）
    - claude_event_semantics フィールドの存在チェック（B5）
    - stdout_contract / stderr_contract 必須チェック（B6）
    - redaction_contract の存在チェック（B6）
    """
    errors: list[str] = []

    for me in manifest_entries:
        hid = me.get("handler_id", "<unknown>")
        event = me.get("event", "<unknown>")
        label = f"{hid}@{event}"

        # B2: classification vocabulary チェック
        classification = me.get("classification")
        if classification is None:
            errors.append(f"[schema:{label}] classification フィールドがありません")
        elif classification not in VALID_CLASSIFICATIONS:
            errors.append(
                f"[schema:{label}] classification の値 {classification!r} が不正です "
                f"（許可値: {sorted(VALID_CLASSIFICATIONS)}）"
            )
        else:
            # B2: blocker の必須フィールドチェック
            if classification == "blocker":
                fail_policy = me.get("fail_policy")
                if fail_policy != "fail_closed":
                    errors.append(
                        f"[schema:{label}] blocker は fail_policy: fail_closed が必須ですが "
                        f"{fail_policy!r} です"
                    )
                agent_action = me.get("agent_action", {})
                if not isinstance(agent_action, dict):
                    errors.append(
                        f"[schema:{label}] blocker は agent_action が必須です（dict 形式）"
                    )
                elif agent_action.get("on_nonzero") != "stop_tool_call":
                    errors.append(
                        f"[schema:{label}] blocker は agent_action.on_nonzero: stop_tool_call が必須ですが "
                        f"{agent_action.get('on_nonzero')!r} です"
                    )

            # B2: telemetry の必須フィールドチェック
            elif classification == "telemetry":
                fail_policy = me.get("fail_policy")
                if fail_policy != "fail_open":
                    errors.append(
                        f"[schema:{label}] telemetry は fail_policy: fail_open が必須ですが "
                        f"{fail_policy!r} です"
                    )
                agent_action = me.get("agent_action", {})
                if not isinstance(agent_action, dict):
                    errors.append(
                        f"[schema:{label}] telemetry は agent_action が必須です（dict 形式）"
                    )
                elif agent_action.get("on_any") != "proceed":
                    errors.append(
                        f"[schema:{label}] telemetry は agent_action.on_any: proceed が必須ですが "
                        f"{agent_action.get('on_any')!r} です"
                    )

            # B2: mode_dependent の必須フィールドチェック
            elif classification == "mode_dependent":
                if "mode_env" not in me:
                    errors.append(
                        f"[schema:{label}] mode_dependent は mode_env フィールドが必須です"
                    )
                if "mode_values" not in me:
                    errors.append(
                        f"[schema:{label}] mode_dependent は mode_values フィールドが必須です"
                    )

        # B5: claude_event_semantics フィールドの存在チェック
        if "claude_event_semantics" not in me:
            errors.append(
                f"[schema:{label}] claude_event_semantics フィールドがありません"
            )
        else:
            ces = me["claude_event_semantics"]
            if not isinstance(ces, dict):
                errors.append(
                    f"[schema:{label}] claude_event_semantics は dict 形式が必須です"
                )
            else:
                if "exit_2_effect" not in ces:
                    errors.append(
                        f"[schema:{label}] claude_event_semantics に exit_2_effect がありません"
                    )

        # B6: stdout_contract / stderr_contract 必須チェック
        if "stdout_contract" not in me:
            errors.append(
                f"[schema:{label}] stdout_contract フィールドがありません"
            )
        if "stderr_contract" not in me:
            errors.append(
                f"[schema:{label}] stderr_contract フィールドがありません"
            )

        # B6: redaction_contract 存在チェック
        if "redaction_contract" not in me:
            errors.append(
                f"[schema:{label}] redaction_contract フィールドがありません"
            )

    return errors


# ─── 比較ロジック ─────────────────────────────────────────────────────────────

# AC7: 照合対象フィールド
COMPARE_FIELDS = ("event", "matcher", "handler_id", "timeout", "classification", "agent_action")

# 複合照合キー
def make_key(handler_id: str, event: str) -> tuple[str, str]:
    return (handler_id, event)


def check_drift(
    manifest_entries: list[dict[str, Any]],
    settings_entries: list[dict[str, Any]],
) -> list[str]:
    """
    manifest と settings を構造照合し drift を返す。

    AC7: event / matcher / handler_id / timeout / classification / agent_action を比較する。

    照合キーは (handler_id, event) の複合キー。
    """
    errors: list[str] = []

    # B1: duplicate チェックを先に実施（dict 化前に実行）
    dup_manifest = detect_duplicates_in_manifest(manifest_entries)
    dup_settings = detect_duplicates_in_settings(settings_entries)
    errors.extend(dup_manifest)
    errors.extend(dup_settings)

    # duplicate があった場合、その後の dict 化ロジックが不正確になるため早期リターン
    if dup_manifest or dup_settings:
        return errors

    # B2/B5/B6: manifest schema validation
    schema_errors = validate_manifest_schema(manifest_entries)
    errors.extend(schema_errors)

    # settings の (handler_id, event) → entry マップを構築
    settings_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for se in settings_entries:
        key = make_key(se["handler_id"], se["event"])
        settings_by_key[key] = se

    # manifest の (handler_id, event) → entry マップを構築
    manifest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for me in manifest_entries:
        key = make_key(me["handler_id"], me["event"])
        manifest_by_key[key] = me

    settings_keys = set(settings_by_key.keys())
    manifest_keys = set(manifest_by_key.keys())

    # settings に存在するが manifest にない (handler_id, event)
    missing_in_manifest = settings_keys - manifest_keys
    missing_in_settings = manifest_keys - settings_keys

    for key in sorted(missing_in_manifest):
        errors.append(
            f"[drift] settings.json に存在するが manifest にない "
            f"(handler_id={key[0]!r}, event={key[1]!r})"
        )

    for key in sorted(missing_in_settings):
        errors.append(
            f"[drift] manifest に存在するが settings.json にない "
            f"(handler_id={key[0]!r}, event={key[1]!r})"
        )

    # 両方に存在するキーについて各フィールドを照合
    for key in sorted(settings_keys & manifest_keys):
        se = settings_by_key[key]
        me = manifest_by_key[key]
        hid, event = key
        label = f"{hid}@{event}"

        # matcher 比較
        if se["matcher"] != me.get("matcher"):
            errors.append(
                f"[drift:{label}] matcher mismatch: "
                f"settings={se['matcher']!r} manifest={me.get('matcher')!r}"
            )

        # timeout 比較
        if se["timeout"] != me.get("timeout"):
            errors.append(
                f"[drift:{label}] timeout mismatch: "
                f"settings={se['timeout']!r} manifest={me.get('timeout')!r}"
            )

        # command 比較（${CLAUDE_PROJECT_DIR} 展開前で比較）
        if se["command"] != me.get("command", ""):
            errors.append(
                f"[drift:{label}] command mismatch: "
                f"settings={se['command']!r} manifest={me.get('command')!r}"
            )

        # args 比較
        if se["args"] != (me.get("args") or []):
            errors.append(
                f"[drift:{label}] args mismatch: "
                f"settings={se['args']!r} manifest={me.get('args')!r}"
            )

        # classification は manifest にのみ存在するが drift チェック対象に含める
        if "classification" not in me:
            errors.append(f"[drift:{label}] manifest に classification フィールドがありません")

        # agent_action は manifest にのみ存在する（settings は構造持たない）
        if "agent_action" not in me:
            errors.append(f"[drift:{label}] manifest に agent_action フィールドがありません")

    return errors


def load_codex_hooks_topology(path: Path = CODEX_HOOKS_PATH) -> dict[str, list[dict[str, Any]]]:
    """Return the exact .codex/hooks.json PreToolUse topology per matcher."""
    data = json.loads(path.read_text(encoding="utf-8"))
    pretool = data.get("hooks", {}).get("PreToolUse", [])
    topology: dict[str, list[dict[str, Any]]] = {}
    for entry in pretool:
        matcher = entry.get("matcher", "<none>")
        topology[matcher] = entry.get("hooks", [])
    return topology


def check_codex_hooks_pretool_topology(
    path: Path = CODEX_HOOKS_PATH,
    expected: dict[str, list[dict[str, Any]]] | None = None,
) -> list[str]:
    """Fail-closed comparison of the current .codex/hooks.json PreToolUse
    topology against the frozen expected handler matrix."""
    if expected is None:
        expected = EXPECTED_CODEX_PRETOOL_TOPOLOGY
    errors: list[str] = []
    if not path.exists():
        errors.append(f"[error] .codex/hooks.json が見つかりません: {path}")
        return errors
    actual = load_codex_hooks_topology(path)
    if actual != expected:
        errors.append(
            "[codex:pretool_topology] .codex/hooks.json の PreToolUse hook "
            f"topology が期待値と一致しません: expected={expected!r} actual={actual!r} "
            "（意図した hook 追加/削除であれば EXPECTED_CODEX_PRETOOL_TOPOLOGY を更新しレビューを受けること）"
        )
    return errors


def check_codex_hooks_no_fastpath_classifier(path: Path = CODEX_HOOKS_PATH) -> list[str]:
    """Issue #1289 AC6: pretool_fastpath_classifier must never be registered as
    its own PreToolUse hook command in .codex/hooks.json."""
    errors: list[str] = []
    if not path.exists():
        errors.append(f"[error] .codex/hooks.json が見つかりません: {path}")
        return errors
    data = json.loads(path.read_text(encoding="utf-8"))
    for event, event_entries in data.get("hooks", {}).items():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                if _FASTPATH_CLASSIFIER_MODULE_NAME in command:
                    errors.append(
                        f"[codex:fastpath_topology] {_FASTPATH_CLASSIFIER_MODULE_NAME} は "
                        f".codex/hooks.json の独立 PreToolUse hook として登録されてはいけません "
                        f"(event={event!r} command={command!r})"
                    )
    return errors


def check_codex_hooks_root_keys(path: Path = CODEX_HOOKS_PATH) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"[codex:root_keys] .codex/hooks.json が見つかりません: {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"[codex:root_keys] .codex/hooks.json の JSON parse に失敗: {exc}"]

    if not isinstance(data, dict):
        return ["[codex:root_keys] .codex/hooks.json root は object である必要があります"]

    root_keys = sorted(data.keys())
    if root_keys != ["hooks"]:
        errors.append(
            "[codex:root_keys] .codex/hooks.json root keys must be exactly "
            f"['hooks'], got {root_keys}"
        )
    return errors


def check_settings_no_fastpath_classifier(path: Path = SETTINGS_PATH) -> list[str]:
    """Issue #1289 AC6: same check for .claude/settings.json."""
    errors: list[str] = []
    if not path.exists():
        return errors
    data = json.loads(path.read_text(encoding="utf-8"))
    for event, event_entries in data.get("hooks", {}).items():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                if _FASTPATH_CLASSIFIER_MODULE_NAME in command:
                    errors.append(
                        f"[settings:fastpath_topology] {_FASTPATH_CLASSIFIER_MODULE_NAME} は "
                        f".claude/settings.json の独立 PreToolUse hook として登録されてはいけません "
                        f"(event={event!r} command={command!r})"
                    )
    return errors


def validate_narrative_consistency(docs_text: str) -> list[str]:
    """
    本文の説明表・telemetry 説明が current topology と一致しているか検証する。

    YAML manifest と settings.json の構造一致だけでは、本文表に残った stale な
    「generate_session_manifest_from_hook.mjs が PostToolUse hook」という記述を検出できない。
    """
    errors: list[str] = []

    for snippet in STALE_NARRATIVE_PATTERNS:
        if snippet in docs_text:
            errors.append(
                f"[narrative] stale topology 記述を検出しました: {snippet}"
            )

    for snippet in REQUIRED_NARRATIVE_SNIPPETS:
        if snippet not in docs_text:
            errors.append(
                f"[narrative] current topology を示す本文記述が不足しています: {snippet}"
            )

    return errors


# ─── メイン ───────────────────────────────────────────────────────────────────

def main() -> int:
    errors: list[str] = []

    # docs 読み込み
    if not DOCS_PATH.exists():
        print(f"[error] docs が見つかりません: {DOCS_PATH}", file=sys.stderr)
        return 1
    docs_text = DOCS_PATH.read_text(encoding="utf-8")

    # settings.json 読み込み
    if not SETTINGS_PATH.exists():
        print(f"[error] settings.json が見つかりません: {SETTINGS_PATH}", file=sys.stderr)
        return 1
    settings_text = SETTINGS_PATH.read_text(encoding="utf-8")

    # manifest パース
    try:
        manifest_entries = extract_manifest(docs_text)
    except Exception as exc:
        print(f"[error] manifest パース失敗: {exc}", file=sys.stderr)
        return 1

    # settings.json パース
    try:
        settings = json.loads(settings_text)
    except json.JSONDecodeError as exc:
        print(f"[error] settings.json パース失敗: {exc}", file=sys.stderr)
        return 1

    # hooks 抽出
    settings_entries = extract_settings_hooks(settings)

    # drift チェック
    drift_errors = check_drift(manifest_entries, settings_entries)
    errors.extend(drift_errors)
    errors.extend(validate_narrative_consistency(docs_text))

    # Issue #1289 (AC4/AC6): .codex/hooks.json is also in scope now — verify
    # the shared fast-path classifier was never registered as an independent
    # PreToolUse hook in either manifest.
    errors.extend(check_codex_hooks_root_keys())
    errors.extend(check_codex_hooks_no_fastpath_classifier())
    errors.extend(check_settings_no_fastpath_classifier())
    errors.extend(check_codex_hooks_pretool_topology())

    # 結果出力
    if errors:
        print("[check_hook_boundaries] FAIL: drift を検出しました", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    manifest_count = len(manifest_entries)
    settings_count = len(settings_entries)
    print(
        f"[check_hook_boundaries] OK: manifest={manifest_count} entries, "
        f"settings={settings_count} entries — drift なし"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
