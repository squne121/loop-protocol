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


# ─── AC4: settings.json と manifest の照合 ────────────────────────────────────

class TestSettingsManifestAlignment:
    def test_no_drift_detected(
        self,
        manifest_entries: list[dict[str, Any]],
        settings_hook_entries: list[dict[str, Any]],
    ) -> None:
        """AC4: settings.json と manifest の間に drift がない。"""
        errors = checker.check_drift(manifest_entries, settings_hook_entries)
        assert not errors, (
            "drift を検出しました:\n" + "\n".join(f"  {e}" for e in errors)
        )

    def test_handler_event_keys_match(
        self,
        manifest_entries: list[dict[str, Any]],
        settings_hook_entries: list[dict[str, Any]],
    ) -> None:
        """manifest の (handler_id, event) 複合キーが settings.json にも存在する。"""
        manifest_keys = {(e["handler_id"], e["event"]) for e in manifest_entries}
        settings_keys = {(e["handler_id"], e["event"]) for e in settings_hook_entries}
        missing = manifest_keys - settings_keys
        assert not missing, (
            f"manifest にあるが settings.json にない (handler_id, event): {missing}"
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
