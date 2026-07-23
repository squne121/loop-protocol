"""
tests/test_hook_boundaries_doc.py

AC4: .claude/settings.json と docs の hook 一覧・identity が一致することを検証する。
AC6: hook identity を command と args の両方から解決する（PostToolUse の node ラッパーを取り逃がさない）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ─── テスト対象モジュールのロード ─────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
CHECKER_PATH = REPO_ROOT / "scripts" / "check_hook_boundaries.py"

spec = importlib.util.spec_from_file_location("check_hook_boundaries", CHECKER_PATH)
assert spec is not None
checker = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(checker)  # type: ignore[attr-defined]


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def docs_text() -> str:
    return (REPO_ROOT / "docs" / "dev" / "hook-boundaries.md").read_text(encoding="utf-8")


@pytest.fixture()
def settings_json() -> dict[str, Any]:
    return json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))


@pytest.fixture()
def manifest_entries(docs_text: str) -> list[dict[str, Any]]:
    return checker.extract_manifest(docs_text)


@pytest.fixture()
def settings_hook_entries(settings_json: dict[str, Any]) -> list[dict[str, Any]]:
    return checker.extract_settings_hooks(settings_json)


# ─── AC5: manifest YAML block の存在確認 ─────────────────────────────────────

class TestManifestExists:
    def test_manifest_yaml_block_present(self, docs_text: str) -> None:
        """AC5: docs に hook_boundaries_manifest_v1 YAML block が含まれる。"""
        assert "hook_boundaries_manifest_v1" in docs_text

    def test_manifest_parseable(self, manifest_entries: list[dict[str, Any]]) -> None:
        """manifest が YAML としてパースできる。"""
        assert isinstance(manifest_entries, list)
        assert len(manifest_entries) > 0

    def test_manifest_has_required_fields(self, manifest_entries: list[dict[str, Any]]) -> None:
        """各 manifest entry に必須フィールドが含まれる（AC7）。"""
        required_fields = {"handler_id", "event", "timeout", "classification", "agent_action"}
        for entry in manifest_entries:
            missing = required_fields - set(entry.keys())
            assert not missing, (
                f"handler_id={entry.get('handler_id')!r}, event={entry.get('event')!r} に"
                f"必須フィールドがありません: {missing}"
            )


# ─── AC6: node ラッパー identity 解決 ─────────────────────────────────────────

class TestNodeWrapperIdentity:
    """AC6: command が 'node' かつ args[0] がスクリプトの PostToolUse hook を正しく解決する。"""

    # PostToolUse の node + args 形式の fixture（settings.json の実形式）
    NODE_HOOK_FIXTURE: dict[str, Any] = {
        "command": "node",
        "args": ["${CLAUDE_PROJECT_DIR}/.claude/hooks/generate_session_manifest_from_hook.mjs"],
        "timeout": 60,
    }

    # 通常コマンド形式の fixture
    REGULAR_HOOK_FIXTURE: dict[str, Any] = {
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh",
        "args": [],
        "timeout": 10,
    }

    # ハイフン含むファイル名の fixture
    HYPHEN_HOOK_FIXTURE: dict[str, Any] = {
        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-japanese-prose.sh",
        "args": [],
        "timeout": 15,
    }

    def test_node_wrapper_resolves_from_args(self) -> None:
        """command='node' のとき handler_id は args[0] のファイル名から解決される。"""
        handler_id = checker.resolve_handler_id(self.NODE_HOOK_FIXTURE)
        assert handler_id == "generate_session_manifest_from_hook", (
            f"node ラッパーの handler_id が正しく解決されませんでした: {handler_id!r}"
        )

    def test_regular_command_resolves_from_command(self) -> None:
        """通常の command フックは command パスのファイル名（拡張子なし）から解決される。"""
        handler_id = checker.resolve_handler_id(self.REGULAR_HOOK_FIXTURE)
        assert handler_id == "secret_boundary_guard", (
            f"通常 command の handler_id が正しく解決されませんでした: {handler_id!r}"
        )

    def test_hyphen_filename_preserved(self) -> None:
        """ハイフンを含むファイル名は handler_id にそのまま保持される。"""
        handler_id = checker.resolve_handler_id(self.HYPHEN_HOOK_FIXTURE)
        assert handler_id == "guard-japanese-prose", (
            f"ハイフン含む handler_id が正しく解決されませんでした: {handler_id!r}"
        )

    def test_node_hook_present_in_settings(self, settings_hook_entries: list[dict[str, Any]]) -> None:
        """settings.json に node ラッパー形式の PostToolUse hook が存在する。"""
        node_hooks = [
            e for e in settings_hook_entries
            if e["event"] == "PostToolUse" and Path(e["command"]).name == "node"
        ]
        assert node_hooks, (
            "settings.json に command='node' の PostToolUse hook が見つかりません"
        )

    def test_node_hook_handler_id_resolved(self, settings_hook_entries: list[dict[str, Any]]) -> None:
        """node ラッパー hook の handler_id が generate_session_manifest_from_hook に解決される。"""
        node_hooks = [
            e for e in settings_hook_entries
            if e["event"] == "PostToolUse" and Path(e["command"]).name == "node"
        ]
        assert node_hooks, "node ラッパー hook が見つかりません"
        hids = [e["handler_id"] for e in node_hooks]
        assert "generate_session_manifest_from_hook" in hids, (
            f"node ラッパーの handler_id が期待値と異なります: {hids}"
        )

    def test_node_wrapper_without_args_fails(self) -> None:
        """B1: node wrapper で args が空の場合は __node_no_args__ を返す（checker で検出可能）。"""
        node_no_args_fixture: dict[str, Any] = {
            "command": "node",
            "args": [],
            "timeout": 60,
        }
        handler_id = checker.resolve_handler_id(node_no_args_fixture)
        assert handler_id == "__node_no_args__", (
            f"node wrapper without args は __node_no_args__ を返すべきですが: {handler_id!r}"
        )

    def test_node_wrapper_args0_not_script_path_fails(self) -> None:
        """B1: node wrapper で args[0] がスクリプトパスでない場合でも handler_id に変換される。"""
        node_invalid_args_fixture: dict[str, Any] = {
            "command": "node",
            "args": ["--version"],
            "timeout": 60,
        }
        handler_id = checker.resolve_handler_id(node_invalid_args_fixture)
        # --version はファイル名として stem が "--version" になる
        assert handler_id == "--version", (
            f"args[0] から stem 抽出が期待どおりでない: {handler_id!r}"
        )


# ─── AC4: settings.json と manifest の照合 ────────────────────────────────────

class TestSettingsManifestAlignment:
    def test_no_drift_detected(
        self,
        manifest_entries: list[dict[str, Any]],
        settings_hook_entries: list[dict[str, Any]],
    ) -> None:
        """AC4: settings.json と manifest の間に drift がない。

        Issue #1690 note: local_main_branch_guard と worktree_scope_guard は
        #1690 の方針決定までの間 settings.json から一時的に外されている。
        drift はこの2件のみに限定されることを検証する。#1690 の結論で
        復元された場合、drift-free assertion を復元すること。
        """
        errors = checker.check_drift(manifest_entries, settings_hook_entries)
        expected_errors = {
            "[drift] manifest に存在するが settings.json にない "
            "(handler_id='local_main_branch_guard', event='PreToolUse')",
            "[drift] manifest に存在するが settings.json にない "
            "(handler_id='worktree_scope_guard', event='PreToolUse')",
        }
        assert set(errors) == expected_errors, (
            "drift は #1690 の2件のみである想定:\n" + "\n".join(f"  {e}" for e in errors)
        )

    def test_handler_event_keys_match(
        self,
        manifest_entries: list[dict[str, Any]],
        settings_hook_entries: list[dict[str, Any]],
    ) -> None:
        """manifest の (handler_id, event) 複合キーが settings.json にも存在する。

        Issue #1690 note: local_main_branch_guard と worktree_scope_guard は
        方針決定までの間 settings.json から外れている想定のため、この2件のみ
        欠落を許容する。#1690 の結論で復元された場合、strict assertion を
        復元すること。
        """
        manifest_keys = {(e["handler_id"], e["event"]) for e in manifest_entries}
        settings_keys = {(e["handler_id"], e["event"]) for e in settings_hook_entries}
        missing = manifest_keys - settings_keys
        expected_missing = {
            ("local_main_branch_guard", "PreToolUse"),
            ("worktree_scope_guard", "PreToolUse"),
        }
        assert missing == expected_missing, (
            f"missing は #1690 の2件のみである想定: {missing}"
        )

    def test_settings_hooks_covered_by_manifest(
        self,
        manifest_entries: list[dict[str, Any]],
        settings_hook_entries: list[dict[str, Any]],
    ) -> None:
        """settings.json の全 hook が manifest に記載されている。"""
        manifest_keys = {(e["handler_id"], e["event"]) for e in manifest_entries}
        settings_keys = {(e["handler_id"], e["event"]) for e in settings_hook_entries}
        missing = settings_keys - manifest_keys
        assert not missing, (
            f"settings.json にあるが manifest にない (handler_id, event): {missing}"
        )


# ─── AC2: telemetry hooks の分類確認 ─────────────────────────────────────────

class TestTelemetryHooksClassification:
    TELEMETRY_HOOKS = {
        "session_manifest_coordinator",
        "generate_session_manifest_from_hook",
        "save_loop_state_before_compaction",
        "rtk_boundary_shadow_guard",
    }

    def test_telemetry_hooks_classified(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC2: best-effort telemetry フックが telemetry として分類されている。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        for hid in self.TELEMETRY_HOOKS:
            assert hid in manifest_by_id, f"telemetry hook {hid!r} が manifest にありません"
            classification = manifest_by_id[hid].get("classification")
            assert classification == "telemetry", (
                f"hook {hid!r} の classification が 'telemetry' ではありません: {classification!r}"
            )

    def test_telemetry_hooks_fail_open(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC2: telemetry フックは fail_policy: fail_open である。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        for hid in self.TELEMETRY_HOOKS:
            if hid not in manifest_by_id:
                continue
            fail_policy = manifest_by_id[hid].get("fail_policy")
            assert fail_policy == "fail_open", (
                f"hook {hid!r} の fail_policy が 'fail_open' ではありません: {fail_policy!r}"
            )


# ─── AC3: secret_boundary_guard は blocker かつ fail_closed ──────────────────

class TestSecretBoundaryGuardClassification:
    def test_secret_boundary_guard_is_blocker(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC3: secret_boundary_guard は blocker として分類されている。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        assert "secret_boundary_guard" in manifest_by_id
        entry = manifest_by_id["secret_boundary_guard"]
        assert entry.get("classification") == "blocker", (
            f"secret_boundary_guard の classification が 'blocker' ではありません: "
            f"{entry.get('classification')!r}"
        )

    def test_secret_boundary_guard_fail_closed(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC3: secret_boundary_guard は fail_closed である。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        assert "secret_boundary_guard" in manifest_by_id
        entry = manifest_by_id["secret_boundary_guard"]
        assert entry.get("fail_policy") == "fail_closed", (
            f"secret_boundary_guard の fail_policy が 'fail_closed' ではありません: "
            f"{entry.get('fail_policy')!r}"
        )


# ─── AC8: guard-japanese-prose は mode_dependent ─────────────────────────────

class TestGuardJapaneseProseClassification:
    HANDLER_ID = "guard-japanese-prose"

    def test_guard_japanese_prose_mode_dependent(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC8: guard-japanese-prose は mode_dependent として分類されている。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        assert self.HANDLER_ID in manifest_by_id, (
            f"{self.HANDLER_ID!r} が manifest にありません"
        )
        entry = manifest_by_id[self.HANDLER_ID]
        assert entry.get("classification") == "mode_dependent", (
            f"{self.HANDLER_ID!r} の classification が 'mode_dependent' ではありません: "
            f"{entry.get('classification')!r}"
        )

    def test_guard_japanese_prose_has_mode_values(self, manifest_entries: list[dict[str, Any]]) -> None:
        """AC8: guard-japanese-prose に shadow と enforce の mode_values が記述されている。"""
        manifest_by_id = {e["handler_id"]: e for e in manifest_entries}
        entry = manifest_by_id.get(self.HANDLER_ID, {})
        mode_values = entry.get("mode_values", {})
        assert "unset_or_shadow" in mode_values, "mode_values に unset_or_shadow がありません"
        assert "enforce" in mode_values, "mode_values に enforce がありません"


# ─── B1: duplicate topology 検出テスト ───────────────────────────────────────

class TestDuplicateTopologyDetection:
    """B1: duplicate (handler_id, event) の検出が fail-closed であることを検証する。"""

    def test_duplicate_manifest_key_fails(self) -> None:
        """B1: manifest に同一 (handler_id, event) が重複している場合は fail する。"""
        duplicate_entries = [
            {
                "handler_id": "secret_boundary_guard",
                "event": "PreToolUse",
                "matcher": "Bash",
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh",
                "args": [],
                "timeout": 10,
                "classification": "blocker",
                "fail_policy": "fail_closed",
                "stdout_contract": "silent_on_allow",
                "stderr_contract": "minimal_structural_message_on_block",
                "agent_action": {"on_nonzero": "stop_tool_call", "on_zero": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "blocks_tool_call"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
            },
            {
                "handler_id": "secret_boundary_guard",
                "event": "PreToolUse",
                "matcher": "Write",  # 異なる matcher でも同一 (handler_id, event) は重複
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh",
                "args": [],
                "timeout": 10,
                "classification": "blocker",
                "fail_policy": "fail_closed",
                "stdout_contract": "silent_on_allow",
                "stderr_contract": "minimal_structural_message_on_block",
                "agent_action": {"on_nonzero": "stop_tool_call", "on_zero": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "blocks_tool_call"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
            },
        ]
        errors = checker.detect_duplicates_in_manifest(duplicate_entries)
        assert errors, "manifest の duplicate (handler_id, event) がエラーとして検出されませんでした"
        assert any("duplicate:manifest" in e for e in errors), (
            f"duplicate:manifest エラーが含まれていません: {errors}"
        )

    def test_duplicate_settings_key_fails(self) -> None:
        """B1: settings 抽出結果に同一 (handler_id, event) が重複している場合は fail する。"""
        duplicate_settings_entries = [
            {
                "event": "PreToolUse",
                "matcher": "Bash|Read|Write|Edit|Grep|Glob",
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh",
                "args": [],
                "timeout": 10,
                "type": "command",
                "handler_id": "secret_boundary_guard",
            },
            {
                "event": "PreToolUse",
                "matcher": "Bash",  # 異なる matcher でも同一 (handler_id, event) は重複
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/secret_boundary_guard.sh",
                "args": [],
                "timeout": 10,
                "type": "command",
                "handler_id": "secret_boundary_guard",
            },
        ]
        errors = checker.detect_duplicates_in_settings(duplicate_settings_entries)
        assert errors, "settings の duplicate (handler_id, event) がエラーとして検出されませんでした"
        assert any("duplicate:settings" in e for e in errors), (
            f"duplicate:settings エラーが含まれていません: {errors}"
        )

    def test_same_handler_same_event_different_matcher_fails(self) -> None:
        """B1: 同一 handler_id/event で異なる matcher は (handler_id, event) キーが同一のため重複エラー。"""
        entries = [
            {
                "handler_id": "guard-japanese-prose",
                "event": "PreToolUse",
                "matcher": "Bash|Write|Edit|MultiEdit",
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-japanese-prose.sh",
                "args": [],
                "timeout": 15,
                "classification": "mode_dependent",
                "fail_policy": "shadow_by_default",
                "stdout_contract": "silent",
                "stderr_contract": "jsonl_shadow_log_or_block_reason",
                "agent_action": {"on_nonzero_shadow": "proceed_and_log", "on_zero": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "blocks_tool_call"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
                "mode_env": "GUARD_JAPANESE_PROSE_MODE",
                "mode_values": {"unset_or_shadow": "exit 0", "enforce": "exit 2"},
            },
            {
                "handler_id": "guard-japanese-prose",
                "event": "PreToolUse",
                "matcher": "Read",  # 異なる matcher
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard-japanese-prose.sh",
                "args": [],
                "timeout": 15,
                "classification": "mode_dependent",
                "fail_policy": "shadow_by_default",
                "stdout_contract": "silent",
                "stderr_contract": "jsonl_shadow_log_or_block_reason",
                "agent_action": {"on_nonzero_shadow": "proceed_and_log", "on_zero": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "blocks_tool_call"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
                "mode_env": "GUARD_JAPANESE_PROSE_MODE",
                "mode_values": {"unset_or_shadow": "exit 0", "enforce": "exit 2"},
            },
        ]
        errors = checker.detect_duplicates_in_manifest(entries)
        assert errors, (
            "同一 handler_id/event で異なる matcher の場合に重複エラーが検出されませんでした"
        )

    def test_hook_type_mismatch_fails(self) -> None:
        """B1: session_manifest_coordinator の Stop/SubagentStop は別 key として正当に扱われる。"""
        # session_manifest_coordinator が Stop と SubagentStop に配置される場合は重複ではない
        coordinator_entries = [
            {
                "handler_id": "session_manifest_coordinator",
                "event": "Stop",
                "matcher": None,
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh",
                "args": [],
                "timeout": 180,
                "classification": "telemetry",
                "fail_policy": "fail_open",
                "stdout_contract": "silent",
                "stderr_contract": "diagnostic_on_failure_max_10_lines",
                "agent_action": {"on_any": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "prevents_stop"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
            },
            {
                "handler_id": "session_manifest_coordinator",
                "event": "SubagentStop",
                "matcher": None,
                "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/session_manifest_coordinator.sh",
                "args": [],
                "timeout": 180,
                "classification": "telemetry",
                "fail_policy": "fail_open",
                "stdout_contract": "silent",
                "stderr_contract": "diagnostic_on_failure_max_10_lines",
                "agent_action": {"on_any": "proceed"},
                "claude_event_semantics": {"exit_2_effect": "prevents_subagent_stop"},
                "redaction_contract": {
                    "no_raw_command": True,
                    "no_raw_secret_like_value": True,
                    "no_raw_transcript": True,
                    "no_manifest_body_on_stdout": True,
                },
            },
        ]
        errors = checker.detect_duplicates_in_manifest(coordinator_entries)
        assert not errors, (
            f"session_manifest_coordinator の Stop/SubagentStop は重複ではないはずですが: {errors}"
        )


# ─── B2: classification schema validation テスト ──────────────────────────────

class TestClassificationSchemaValidation:
    """B2: classification の語彙・必須フィールドの検証が fail-closed であることを確認する。"""

    def _make_valid_blocker(self) -> dict[str, Any]:
        return {
            "handler_id": "test_blocker",
            "event": "PreToolUse",
            "matcher": "Bash",
            "command": "/path/to/hook.sh",
            "args": [],
            "timeout": 10,
            "classification": "blocker",
            "fail_policy": "fail_closed",
            "stdout_contract": "silent_on_allow",
            "stderr_contract": "minimal_structural_message_on_block",
            "agent_action": {"on_nonzero": "stop_tool_call", "on_zero": "proceed"},
            "claude_event_semantics": {"exit_2_effect": "blocks_tool_call"},
            "redaction_contract": {
                "no_raw_command": True,
                "no_raw_secret_like_value": True,
                "no_raw_transcript": True,
                "no_manifest_body_on_stdout": True,
            },
        }

    def _make_valid_telemetry(self) -> dict[str, Any]:
        return {
            "handler_id": "test_telemetry",
            "event": "PostToolUse",
            "matcher": "Bash",
            "command": "node",
            "args": ["/path/to/hook.mjs"],
            "timeout": 60,
            "classification": "telemetry",
            "fail_policy": "fail_open",
            "stdout_contract": "silent",
            "stderr_contract": "diagnostic_on_failure",
            "agent_action": {"on_any": "proceed"},
            "claude_event_semantics": {"exit_2_effect": "cannot_block_completed_tool_call"},
            "redaction_contract": {
                "no_raw_command": True,
                "no_raw_secret_like_value": True,
                "no_raw_transcript": True,
                "no_manifest_body_on_stdout": True,
            },
        }

    def test_invalid_classification_vocabulary_fails(self) -> None:
        """B2: classification が許可語彙外（例: typo）の場合は schema error になる。"""
        entry = self._make_valid_blocker()
        entry["classification"] = "telemetery"  # typo
        errors = checker.validate_manifest_schema([entry])
        assert errors, "classification typo がエラーとして検出されませんでした"
        assert any("schema:" in e and "classification" in e for e in errors), (
            f"classification エラーが含まれていません: {errors}"
        )

    def test_blocker_without_fail_closed_fails(self) -> None:
        """B2: blocker で fail_policy が fail_closed でない場合は schema error になる。"""
        entry = self._make_valid_blocker()
        entry["fail_policy"] = "fail_open"  # blocker には不正
        errors = checker.validate_manifest_schema([entry])
        assert errors, "blocker の fail_policy: fail_open がエラーとして検出されませんでした"
        assert any("fail_closed" in e for e in errors), (
            f"fail_closed エラーが含まれていません: {errors}"
        )

    def test_blocker_without_exit_2_contract_fails(self) -> None:
        """B2: blocker で agent_action.on_nonzero が stop_tool_call でない場合は schema error になる。"""
        entry = self._make_valid_blocker()
        entry["agent_action"] = {"on_nonzero": "proceed", "on_zero": "proceed"}  # 不正
        errors = checker.validate_manifest_schema([entry])
        assert errors, "blocker の agent_action.on_nonzero: proceed がエラーとして検出されませんでした"
        assert any("stop_tool_call" in e for e in errors), (
            f"stop_tool_call エラーが含まれていません: {errors}"
        )

    def test_telemetry_with_stop_tool_call_fails(self) -> None:
        """B2: telemetry で agent_action.on_any が proceed でない場合は schema error になる。"""
        entry = self._make_valid_telemetry()
        entry["agent_action"] = {"on_any": "stop_tool_call"}  # telemetry には不正
        errors = checker.validate_manifest_schema([entry])
        assert errors, "telemetry の agent_action.on_any: stop_tool_call がエラーとして検出されませんでした"
        assert any("proceed" in e for e in errors), (
            f"proceed エラーが含まれていません: {errors}"
        )

    def test_missing_stdout_or_stderr_contract_fails(self) -> None:
        """B6: stdout_contract または stderr_contract が欠落している場合は schema error になる。"""
        # stdout_contract が欠落
        entry_no_stdout = self._make_valid_blocker()
        del entry_no_stdout["stdout_contract"]
        errors = checker.validate_manifest_schema([entry_no_stdout])
        assert errors, "stdout_contract 欠落がエラーとして検出されませんでした"
        assert any("stdout_contract" in e for e in errors), (
            f"stdout_contract エラーが含まれていません: {errors}"
        )

        # stderr_contract が欠落
        entry_no_stderr = self._make_valid_blocker()
        del entry_no_stderr["stderr_contract"]
        errors = checker.validate_manifest_schema([entry_no_stderr])
        assert errors, "stderr_contract 欠落がエラーとして検出されませんでした"
        assert any("stderr_contract" in e for e in errors), (
            f"stderr_contract エラーが含まれていません: {errors}"
        )


# ─── B5: claude_event_semantics フィールド存在確認テスト ─────────────────────

class TestClaudeEventSemantics:
    """B5: 全 manifest entry に claude_event_semantics が存在することを確認する。"""

    def test_all_manifest_entries_have_claude_event_semantics(
        self, manifest_entries: list[dict[str, Any]]
    ) -> None:
        """B5: 全 manifest entry に claude_event_semantics フィールドが存在する。"""
        missing = [
            f"{e.get('handler_id')}@{e.get('event')}"
            for e in manifest_entries
            if "claude_event_semantics" not in e
        ]
        assert not missing, (
            f"以下の manifest entry に claude_event_semantics がありません: {missing}"
        )

    def test_all_claude_event_semantics_have_exit_2_effect(
        self, manifest_entries: list[dict[str, Any]]
    ) -> None:
        """B5: 全 manifest entry の claude_event_semantics に exit_2_effect が存在する。"""
        missing = []
        for e in manifest_entries:
            ces = e.get("claude_event_semantics", {})
            if not isinstance(ces, dict) or "exit_2_effect" not in ces:
                missing.append(f"{e.get('handler_id')}@{e.get('event')}")
        assert not missing, (
            f"以下の manifest entry の claude_event_semantics に exit_2_effect がありません: {missing}"
        )

    def test_session_manifest_coordinator_stop_and_subagentstop_are_both_validated(
        self, manifest_entries: list[dict[str, Any]]
    ) -> None:
        """B5: session_manifest_coordinator の Stop/SubagentStop 両エントリが claude_event_semantics を持つ。"""
        coordinator_entries = [
            e for e in manifest_entries
            if e.get("handler_id") == "session_manifest_coordinator"
        ]
        events = {e["event"] for e in coordinator_entries}
        assert "Stop" in events, "session_manifest_coordinator の Stop エントリが manifest にありません"
        assert "SubagentStop" in events, (
            "session_manifest_coordinator の SubagentStop エントリが manifest にありません"
        )

        for entry in coordinator_entries:
            ces = entry.get("claude_event_semantics")
            assert ces is not None, (
                f"session_manifest_coordinator@{entry['event']} に claude_event_semantics がありません"
            )
            assert "exit_2_effect" in ces, (
                f"session_manifest_coordinator@{entry['event']} の claude_event_semantics に "
                f"exit_2_effect がありません"
            )

        # Stop の exit_2_effect が prevents_stop であることを確認
        stop_entry = next(e for e in coordinator_entries if e["event"] == "Stop")
        assert stop_entry["claude_event_semantics"]["exit_2_effect"] == "prevents_stop", (
            f"session_manifest_coordinator@Stop の exit_2_effect が prevents_stop ではありません: "
            f"{stop_entry['claude_event_semantics']['exit_2_effect']!r}"
        )

        # SubagentStop の exit_2_effect が prevents_subagent_stop であることを確認
        subagent_stop_entry = next(e for e in coordinator_entries if e["event"] == "SubagentStop")
        assert subagent_stop_entry["claude_event_semantics"]["exit_2_effect"] == "prevents_subagent_stop", (
            f"session_manifest_coordinator@SubagentStop の exit_2_effect が prevents_subagent_stop ではありません: "
            f"{subagent_stop_entry['claude_event_semantics']['exit_2_effect']!r}"
        )

    def test_posttooluse_exit_2_cannot_block_completed_tool_call(
        self, manifest_entries: list[dict[str, Any]]
    ) -> None:
        """B5: PostToolUse hook の exit_2_effect は cannot_block_completed_tool_call である。"""
        posttooluse_entries = [
            e for e in manifest_entries if e.get("event") == "PostToolUse"
        ]
        assert posttooluse_entries, "PostToolUse エントリが manifest にありません"
        for entry in posttooluse_entries:
            ces = entry.get("claude_event_semantics", {})
            exit_2_effect = ces.get("exit_2_effect")
            assert exit_2_effect == "cannot_block_completed_tool_call", (
                f"PostToolUse {entry.get('handler_id')!r} の exit_2_effect が "
                f"cannot_block_completed_tool_call ではありません: {exit_2_effect!r}"
            )


# ─── B6: redaction_contract テスト ───────────────────────────────────────────

class TestRedactionContract:
    """B6: 全 manifest entry に redaction_contract フィールドが存在することを確認する。"""

    def test_all_manifest_entries_have_redaction_contract(
        self, manifest_entries: list[dict[str, Any]]
    ) -> None:
        """B6: 全 manifest entry に redaction_contract フィールドが存在する。"""
        missing = [
            f"{e.get('handler_id')}@{e.get('event')}"
            for e in manifest_entries
            if "redaction_contract" not in e
        ]
        assert not missing, (
            f"以下の manifest entry に redaction_contract がありません: {missing}"
        )
