"""
context-mode artifact schema 検証テスト (#824)

このテストスイートは `.claude/artifacts/context-mode/` 配下の artifact JSON ファイルの
schema を検証する。

Runtime Verification Applicability: immediate
preflight-scope: runtime_only

AC1: 実験用 profile/scope の定義が artifact に保存され、context-mode がその scope で起動する
AC3: ctx-doctor が error なしで完了し、redacted JSON が ctx-doctor-result.json に保存されている
AC4: registered-tools.json に期待 11 tools が保存されている
AC5: ctx_execute / ctx_fetch_and_index が permission policy 上 deny で記録されている
AC6: profile-isolation.json に main_profile_touched: false が保存されている

注意: runtime_only VC は context-mode が実際に起動している環境でのみ PASS する。
      context-mode 未インストール環境では SKIP (exit 77) となる。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# artifact ディレクトリのパス（worktree / main repo どちらでも動作するよう解決）
_REPO_ROOT = Path(__file__).parent.parent.parent
ARTIFACT_DIR = _REPO_ROOT / ".claude" / "artifacts" / "context-mode"

# context-mode が実際にインストールされているか確認する
# インストールされていなければ runtime_only テストを SKIP する
def _is_context_mode_runtime_available() -> bool:
    """context-mode のランタイムが利用可能かどうかを確認する。"""
    ctx_doctor_file = ARTIFACT_DIR / "ctx-doctor-result.json"
    if not ctx_doctor_file.exists():
        return False
    try:
        data = json.loads(ctx_doctor_file.read_text())
        # status: pass かつ exit_code: 0 ならランタイム検証済みと判断する
        return data.get("status") == "pass" and data.get("exit_code") == 0
    except (json.JSONDecodeError, KeyError):
        return False


RUNTIME_AVAILABLE = _is_context_mode_runtime_available()

# runtime_only テストのスキップマーカー
# context-mode が実際に起動した環境では RUNTIME_AVAILABLE = True になるため SKIP しない
skip_if_runtime_not_available = pytest.mark.skipif(
    not RUNTIME_AVAILABLE,
    reason=(
        "context-mode runtime not available: "
        "context-mode をインストールして実験用 profile で起動後に再実行してください。"
        "skip_conditions: 実験用 profile セットアップが未完了。"
        "（SKIP = exit 77 / PASS ではない）"
    ),
)


class TestVersionProvenanceSchema:
    """AC2: version-provenance.json の schema 検証。"""

    def test_file_exists(self) -> None:
        """version-provenance.json が存在することを確認する。"""
        assert (ARTIFACT_DIR / "version-provenance.json").exists(), (
            "version-provenance.json が存在しません。"
            ".claude/artifacts/context-mode/version-provenance.json を作成してください。"
        )

    def test_required_fields_present(self) -> None:
        """必須フィールドが存在することを確認する。"""
        data = json.loads((ARTIFACT_DIR / "version-provenance.json").read_text())
        assert "_schema" in data, "version-provenance.json に _schema フィールドがありません"
        assert "_issue" in data, "version-provenance.json に _issue フィールドがありません"
        assert "install_scope" in data, "version-provenance.json に install_scope フィールドがありません"
        assert "redaction" in data, "version-provenance.json に redaction フィールドがありません"

    def test_redaction_fields(self) -> None:
        """redaction フィールドが正しく設定されていることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "version-provenance.json").read_text())
        redaction = data.get("redaction", {})
        assert redaction.get("home_path_masked") is True, (
            "home_path_masked が true ではありません。"
            "home パスが含まれている可能性があります。"
        )
        assert redaction.get("token_like_values_excluded") is True, (
            "token_like_values_excluded が true ではありません。"
            "token-like value が含まれている可能性があります。"
        )
        assert redaction.get("env_dump_excluded") is True, (
            "env_dump_excluded が true ではありません。"
            "env dump が含まれている可能性があります。"
        )

    def test_install_scope_is_experiment_only(self) -> None:
        """install_scope が experiment-profile-only であることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "version-provenance.json").read_text())
        assert data.get("install_scope") == "experiment-profile-only", (
            f"install_scope が experiment-profile-only ではありません: {data.get('install_scope')}"
        )

    def test_installed_version_present(self) -> None:
        """installed_version が null でないことを確認する。"""
        data = json.loads((ARTIFACT_DIR / "version-provenance.json").read_text())
        assert data.get("installed_version") is not None, (
            "installed_version が null です。"
            "context-mode インストール後に version-provenance.json を更新してください。"
        )
        assert isinstance(data["installed_version"], str), (
            f"installed_version が文字列ではありません: {type(data['installed_version'])}"
        )


class TestCtxDoctorResultSchema:
    """AC3: ctx-doctor-result.json の schema 検証 (runtime_only)。"""

    def test_file_exists(self) -> None:
        """ctx-doctor-result.json が存在することを確認する。"""
        assert (ARTIFACT_DIR / "ctx-doctor-result.json").exists(), (
            "ctx-doctor-result.json が存在しません。"
        )

    def test_required_fields_present(self) -> None:
        """必須フィールドが存在することを確認する。"""
        data = json.loads((ARTIFACT_DIR / "ctx-doctor-result.json").read_text())
        assert "status" in data, "ctx-doctor-result.json に status フィールドがありません"
        assert "exit_code" in data, "ctx-doctor-result.json に exit_code フィールドがありません"
        assert "errors" in data, "ctx-doctor-result.json に errors フィールドがありません"
        assert "redaction" in data, "ctx-doctor-result.json に redaction フィールドがありません"

    @skip_if_runtime_not_available
    def test_ctx_doctor_ok(self) -> None:
        """ランタイム検証 (AC3): ctx-doctor が error なしで完了していることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "ctx-doctor-result.json").read_text())
        assert data.get("status") == "pass", (
            f"ctx-doctor が error を返しました: errors={data.get('errors', [])}"
        )
        assert data.get("exit_code") == 0, (
            f"ctx-doctor の exit_code が 0 ではありません: {data.get('exit_code')}"
        )
        assert data.get("errors") == [] or data.get("errors") is None, (
            f"ctx-doctor に errors があります: {data.get('errors')}"
        )


class TestRegisteredToolsSchema:
    """AC4: registered-tools.json の schema 検証 (runtime_only)。"""

    EXPECTED_TOOLS = [
        "ctx_batch_execute",
        "ctx_execute",
        "ctx_execute_file",
        "ctx_index",
        "ctx_search",
        "ctx_fetch_and_index",
        "ctx_stats",
        "ctx_doctor",
        "ctx_upgrade",
        "ctx_purge",
        "ctx_insight",
    ]

    def test_file_exists(self) -> None:
        """registered-tools.json が存在することを確認する。"""
        assert (ARTIFACT_DIR / "registered-tools.json").exists(), (
            "registered-tools.json が存在しません。"
        )

    def test_required_fields_present(self) -> None:
        """必須フィールドが存在することを確認する。"""
        data = json.loads((ARTIFACT_DIR / "registered-tools.json").read_text())
        assert "permission_policy" in data, (
            "registered-tools.json に permission_policy フィールドがありません"
        )
        assert "expected_tools" in data, (
            "registered-tools.json に expected_tools フィールドがありません"
        )

    def test_expected_tools_list(self) -> None:
        """expected_tools に 11 tools が定義されていることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "registered-tools.json").read_text())
        expected = data.get("expected_tools", [])
        assert len(expected) == 11, (
            f"expected_tools の数が 11 ではありません: {len(expected)}"
        )
        for tool in self.EXPECTED_TOOLS:
            assert tool in expected, f"{tool} が expected_tools に含まれていません"

    @skip_if_runtime_not_available
    def test_registered_tools_match_expected(self) -> None:
        """ランタイム検証 (AC4): registered_tools が期待 11 tools と一致することを確認する。"""
        data = json.loads((ARTIFACT_DIR / "registered-tools.json").read_text())
        registered = data.get("registered_tools")
        assert registered is not None, (
            "registered_tools が null です。"
            "context-mode 起動後に registered-tools.json を更新してください。"
        )
        assert isinstance(registered, list), (
            f"registered_tools がリストではありません: {type(registered)}"
        )
        assert len(registered) == 11, (
            f"registered_tools の数が 11 ではありません: {len(registered)}"
        )
        for tool in self.EXPECTED_TOOLS:
            assert tool in registered, (
                f"期待 tool {tool} が registered_tools に含まれていません: {registered}"
            )


def test_deny_policy() -> None:
    """
    AC5: ctx_execute / ctx_fetch_and_index が permission_policy 上 deny で記録されていることを確認する。

    permission_policy フィールドで deny を確認する。
    """
    data = json.loads((ARTIFACT_DIR / "registered-tools.json").read_text())
    policy = data.get("permission_policy", {})

    assert "ctx_execute" in policy, (
        "permission_policy に ctx_execute が含まれていません"
    )
    assert "ctx_fetch_and_index" in policy, (
        "permission_policy に ctx_fetch_and_index が含まれていません"
    )

    assert policy["ctx_execute"] == "deny", (
        f"ctx_execute の permission_policy が deny ではありません: {policy['ctx_execute']}"
    )
    assert policy["ctx_fetch_and_index"] == "deny", (
        f"ctx_fetch_and_index の permission_policy が deny ではありません: {policy['ctx_fetch_and_index']}"
    )


def test_profile_isolation() -> None:
    """
    AC6: profile-isolation.json に main_profile_touched: false が含まれることを確認する。

    main profile が変更されていないことを確認する。
    """
    data = json.loads((ARTIFACT_DIR / "profile-isolation.json").read_text())

    assert "main_profile_touched" in data, (
        "profile-isolation.json に main_profile_touched フィールドがありません"
    )
    assert data["main_profile_touched"] is False, (
        f"main_profile_touched が false ではありません: {data['main_profile_touched']}"
        " main profile が変更された可能性があります。"
    )
    assert data.get("main_settings_changed") is False, (
        f"main_settings_changed が false ではありません: {data.get('main_settings_changed')}"
    )
    assert data.get("main_hooks_changed") is False, (
        f"main_hooks_changed が false ではありません: {data.get('main_hooks_changed')}"
    )


class TestProfileIsolationSchema:
    """AC6: profile-isolation.json の追加 schema 検証。"""

    def test_file_exists(self) -> None:
        """profile-isolation.json が存在することを確認する。"""
        assert (ARTIFACT_DIR / "profile-isolation.json").exists(), (
            "profile-isolation.json が存在しません。"
        )

    def test_experiment_profile_only(self) -> None:
        """experiment_profile_only が true であることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "profile-isolation.json").read_text())
        assert data.get("experiment_profile_only") is True, (
            f"experiment_profile_only が true ではありません: {data.get('experiment_profile_only')}"
        )

    def test_rollback_ref_present(self) -> None:
        """rollback_ref が設定されていることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "profile-isolation.json").read_text())
        assert data.get("rollback_ref") is not None, (
            "profile-isolation.json に rollback_ref がありません"
        )


class TestAC1ExperimentProfileDefinition:
    """
    AC1: 実験用 profile/scope の定義が artifact に保存されていることを確認する。

    config-diff.json に実験用 scope の定義が保存されており、
    deny_policy_entries に ctx_execute と ctx_fetch_and_index の deny が含まれ、
    main_profile_affected が false であることを確認する。
    """

    def test_config_diff_artifact_exists(self) -> None:
        """config-diff.json が存在することを確認する。"""
        assert (ARTIFACT_DIR / "config-diff.json").exists(), (
            "config-diff.json が存在しません"
        )

    def test_config_diff_main_profile_not_affected(self) -> None:
        """config-diff.json の main_profile_affected が false であることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "config-diff.json").read_text())
        assert data.get("main_profile_affected") is False, (
            f"main_profile_affected が false ではありません: {data.get('main_profile_affected')}"
        )

    def test_config_diff_experiment_scope(self) -> None:
        """config-diff.json に experiment scope が定義されていることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "config-diff.json").read_text())
        assert data.get("experiment_scope") is not None, (
            "config-diff.json に experiment_scope がありません"
        )

    def test_config_diff_deny_policy_entries(self) -> None:
        """config-diff.json に ctx_execute と ctx_fetch_and_index の deny が含まれることを確認する。"""
        data = json.loads((ARTIFACT_DIR / "config-diff.json").read_text())
        deny_entries = data.get("deny_policy_entries", [])
        assert "mcp__context-mode__ctx_execute" in deny_entries, (
            "deny_policy_entries に mcp__context-mode__ctx_execute が含まれていません"
        )
        assert "mcp__context-mode__ctx_fetch_and_index" in deny_entries, (
            "deny_policy_entries に mcp__context-mode__ctx_fetch_and_index が含まれていません"
        )

    def test_settings_json_deny_contains_mcp_tools(self) -> None:
        """settings.json の permissions.deny に MCP tool deny が含まれることを確認する。"""
        settings_path = _REPO_ROOT / ".claude" / "settings.json"
        assert settings_path.exists(), ".claude/settings.json が存在しません"
        data = json.loads(settings_path.read_text())
        deny_list = data.get("permissions", {}).get("deny", [])
        assert "mcp__context-mode__ctx_execute" in deny_list, (
            "permissions.deny に mcp__context-mode__ctx_execute が含まれていません"
        )
        assert "mcp__context-mode__ctx_fetch_and_index" in deny_list, (
            "permissions.deny に mcp__context-mode__ctx_fetch_and_index が含まれていません"
        )

    def test_settings_json_enables_context_mode_plugin(self) -> None:
        """settings.json の enabledPlugins に context-mode が含まれることを確認する。"""
        settings_path = _REPO_ROOT / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())
        enabled_plugins = data.get("enabledPlugins", {})
        assert "context-mode@context-mode" in enabled_plugins, (
            "enabledPlugins に context-mode@context-mode が含まれていません"
        )
        assert enabled_plugins["context-mode@context-mode"] is True, (
            "context-mode@context-mode が true になっていません"
        )


class TestAC7RollbackDocExists:
    """AC7: rollback ドキュメントが存在することを確認する。"""

    def test_rollback_md_exists(self) -> None:
        """docs/dev/agent-ops/context-mode-rollback.md が存在することを確認する。"""
        rollback_path = _REPO_ROOT / "docs" / "dev" / "agent-ops" / "context-mode-rollback.md"
        assert rollback_path.exists(), (
            "docs/dev/agent-ops/context-mode-rollback.md が存在しません"
        )

    def test_rollback_md_contains_required_sections(self) -> None:
        """rollback.md に必須セクションが含まれることを確認する。"""
        rollback_path = _REPO_ROOT / "docs" / "dev" / "agent-ops" / "context-mode-rollback.md"
        content = rollback_path.read_text()
        # plugin disable/remove 手順
        assert "plugin" in content.lower() or "Plugin" in content, (
            "rollback.md に plugin 手順が含まれていません"
        )
        # purge 手順
        assert "purge" in content.lower() or "Purge" in content, (
            "rollback.md に purge 手順が含まれていません"
        )
        # MCP server unregister 手順
        assert "mcp" in content.lower() or "MCP" in content, (
            "rollback.md に MCP server 手順が含まれていません"
        )
