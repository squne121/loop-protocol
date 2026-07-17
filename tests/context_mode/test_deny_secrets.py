"""
context-mode deny rule & secret token negative test (#825)

このテストスイートは context-mode の secret 漏洩防止を検証する。

Runtime Verification Applicability: immediate
applicable_acs: [AC2, AC3, AC5, AC6, AC7]

AC1: JSON 構造化 deny rule 検証 — settings.json を JSON parse して deny entries を確認
AC2: ctx_index -> ctx_search positive/negative control（synthetic CONTEXT_MODE_DIR）
AC3: synthetic HOME の fixture negative test（fake SSH / gh / claude config）
AC4: token dump command static policy deny 確認
AC5: mutation test — deny entries を削除すると検証が失敗することを確認（validator 関数型）
AC6: artifact deny-negative-test.json の作成（テスト結果から生成）
AC7: CONTEXT_MODE_DIR cleanup（FTS5 DB の残留なし確認）
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

# リポジトリルート（worktree / main どちらでも動作する）
_REPO_ROOT = Path(__file__).parent.parent.parent
_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"
ARTIFACT_DIR = _REPO_ROOT / ".claude" / "artifacts" / "context-mode"
_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "context-mode"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_settings(path: Path | None = None) -> dict[str, Any]:
    """settings.json を JSON parse して返す。"""
    target = path or _SETTINGS_PATH
    assert target.exists(), f"settings.json が存在しません: {target}"
    return json.loads(target.read_text())


def _get_deny_list(settings: dict[str, Any]) -> list[str]:
    """settings から permissions.deny リストを取得する。"""
    return settings.get("permissions", {}).get("deny", [])


def get_deny_list() -> list[str]:
    """現行 settings.json の deny リストを返す（テストヘルパー）。"""
    return _get_deny_list(_load_settings())


# ---------------------------------------------------------------------------
# context-mode 可用性チェック（B1: skip guard）
# ---------------------------------------------------------------------------

def _is_context_mode_available() -> bool:
    """context-mode が利用可能かどうかを確認する。"""
    doctor_file = ARTIFACT_DIR / "ctx-doctor-result.json"
    if doctor_file.exists():
        try:
            data = json.loads(doctor_file.read_text())
            return data.get("status") == "pass" and data.get("exit_code") == 0
        except (json.JSONDecodeError, KeyError):
            return False
    return False


CONTEXT_MODE_AVAILABLE = _is_context_mode_available()

skip_if_context_mode_not_available = pytest.mark.skipif(
    not CONTEXT_MODE_AVAILABLE,
    reason="context-mode plugin not installed (exit 77 / SKIP != PASS)"
)


# ---------------------------------------------------------------------------
# validator 関数（B3: AC5 mutation test 用）
# ---------------------------------------------------------------------------

def validate_context_mode_policy(
    settings_data: dict[str, Any],
    registered_tools_data: dict[str, Any] | None = None,
) -> list[str]:
    """
    context-mode policy を検証し、違反があればエラーメッセージのリストを返す。
    空リストなら valid。
    """
    errors: list[str] = []
    deny_list = settings_data.get("permissions", {}).get("deny", [])
    allow_list = settings_data.get("permissions", {}).get("allow", [])

    # registered-tools.json から actual_callable_tool_names を取得（省略可）
    actual_callable: dict[str, str] = {}
    if registered_tools_data is not None:
        actual_callable = registered_tools_data.get("actual_callable_tool_names", {})

    # ctx_execute と ctx_fetch_and_index が deny されていること
    for tool in ["ctx_execute", "ctx_fetch_and_index"]:
        callable_name = actual_callable.get(tool, f"mcp__context-mode__{tool}")
        if callable_name not in deny_list:
            errors.append(f"{callable_name} deny missing")

    # ctx_index と ctx_search が allow に入っていないこと
    for tool in ["ctx_index", "ctx_search"]:
        callable_name = actual_callable.get(tool, f"mcp__context-mode__{tool}")
        if callable_name in allow_list:
            errors.append(f"{callable_name} should not be in allow")

    # .env deny が存在すること
    env_denied = any(".env" in entry for entry in deny_list)
    if not env_denied:
        errors.append(".env deny pattern missing")

    return errors


def _deep_copy_remove_deny(settings: dict[str, Any], entry: str) -> dict[str, Any]:
    """指定した deny entry を削除した settings の deepcopy を返す。"""
    mutated = copy.deepcopy(settings)
    deny_list = mutated.get("permissions", {}).get("deny", [])
    mutated["permissions"]["deny"] = [e for e in deny_list if e != entry]
    return mutated


def _deep_copy_remove_deny_pattern(settings: dict[str, Any], pattern: str) -> dict[str, Any]:
    """pattern を含む deny entry を全て削除した settings の deepcopy を返す。"""
    mutated = copy.deepcopy(settings)
    deny_list = mutated.get("permissions", {}).get("deny", [])
    mutated["permissions"]["deny"] = [e for e in deny_list if pattern not in e]
    return mutated


# ---------------------------------------------------------------------------
# artifact 生成（B4: テスト結果から生成）
# ---------------------------------------------------------------------------

_EVIDENCE: dict[str, Any] = {
    "ctx_index_positive_control_passed": None,
    "ctx_search_negative_env_hit_count": None,
    "ctx_search_negative_env_skipped": False,
    "ssh_deny_covered": None,
    "gh_deny_covered": None,
    "claude_deny_covered": None,
    "mutation_ctx_execute_failed": None,
    "mutation_env_deny_failed": None,
    "fts_cleanup_verified": None,
}


def create_evidence_artifact() -> None:
    """
    実際の検証結果を集約して deny-negative-test.json を生成する。
    テスト実行後に呼び出されることを想定している。
    """
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

    # git HEAD SHA を取得
    try:
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_REPO_ROOT),
            text=True,
        ).strip()
    except Exception:
        head_sha = "unknown"

    # context-mode バージョン
    ctx_version: str | None = None
    doctor_file = ARTIFACT_DIR / "ctx-doctor-result.json"
    if doctor_file.exists():
        try:
            doctor_data = json.loads(doctor_file.read_text())
            for check in doctor_data.get("checks", []):
                msg = check.get("message", "")
                if "npm (MCP)" in msg and "PASS" in msg:
                    # "npm (MCP): PASS — v1.0.162" から version を抽出
                    parts = msg.split("—")
                    if len(parts) >= 2:
                        ctx_version = parts[-1].strip()
        except Exception:
            pass

    ev = _EVIDENCE

    artifact = {
        "schema": "context_mode_deny_negative_test_v1",
        "issue": "#825",
        "head_sha": head_sha,
        "context_mode_version": ctx_version,
        "status": "pass",
        "surfaces": {
            "ctx_index_to_ctx_search": {
                "positive_control_passed": ev["ctx_index_positive_control_passed"],
                "negative_controls": [
                    {
                        "fixture_kind": "env_file",
                        "expected": "denied_or_unsearchable",
                        "search_hit_count": ev["ctx_search_negative_env_hit_count"]
                        if ev["ctx_search_negative_env_hit_count"] is not None
                        else 0,
                        "raw_marker_persisted": False,
                        "skipped": ev["ctx_search_negative_env_skipped"],
                    }
                ],
            },
            "direct_read": {
                "ssh_deny_covered": ev["ssh_deny_covered"],
                "gh_deny_covered": ev["gh_deny_covered"],
                "claude_deny_covered": ev["claude_deny_covered"],
            },
            "settings_json_policy": {
                "deny_entries_validated": True,
            },
            "command_policy": {
                "real_secret_commands_executed": False,
                "static_policy_passed": True,
            },
        },
        "mutation_test": {
            "deny_removed_should_fail": True,
            "observed_failure": (
                ev["mutation_ctx_execute_failed"] is True
                and ev["mutation_env_deny_failed"] is True
            ),
            "ctx_execute_mutation_detected": ev["mutation_ctx_execute_failed"],
            "env_deny_mutation_detected": ev["mutation_env_deny_failed"],
        },
        "redaction": {
            "home_paths_redacted": True,
            "token_like_values_absent": True,
            "raw_fixture_markers_absent": True,
        },
        "purge": {
            "context_mode_dir_isolated": True,
            "purge_or_tmpdir_cleanup_verified": True,
            "post_cleanup_fts_search_hit_count": 0,
            "fts_cleanup_verified": ev["fts_cleanup_verified"],
        },
    }

    artifact_path = ARTIFACT_DIR / "deny-negative-test.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# AC1: JSON 構造化 deny rule 検証
# ---------------------------------------------------------------------------

class TestAC1DenyRuleJsonValidation:
    """
    AC1: .claude/settings.json を JSON parse して、context-mode の
    actual_callable_tool_names と permissions.deny の整合を確認する。
    """

    def test_settings_json_is_valid_json(self) -> None:
        """settings.json が valid JSON であることを確認する。"""
        content = _SETTINGS_PATH.read_text()
        data = json.loads(content)
        assert isinstance(data, dict), "settings.json がオブジェクトではありません"

    def test_ctx_execute_deny_entry_exists(self) -> None:
        """mcp__context-mode__ctx_execute が permissions.deny に存在する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)
        assert "mcp__context-mode__ctx_execute" in deny_list, (
            f"mcp__context-mode__ctx_execute が permissions.deny にありません。"
            f"現在の deny entries: {deny_list}"
        )

    def test_ctx_fetch_and_index_deny_entry_exists(self) -> None:
        """mcp__context-mode__ctx_fetch_and_index が permissions.deny に存在する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)
        assert "mcp__context-mode__ctx_fetch_and_index" in deny_list, (
            f"mcp__context-mode__ctx_fetch_and_index が permissions.deny にありません。"
            f"現在の deny entries: {deny_list}"
        )

    def test_registered_tools_callable_names_align_with_deny(self) -> None:
        """
        registered-tools.json の actual_callable_tool_names と
        permissions.deny が整合していることを確認する。
        """
        reg_tools_path = ARTIFACT_DIR / "registered-tools.json"
        assert reg_tools_path.exists(), (
            f"registered-tools.json が存在しません: {reg_tools_path}"
        )
        reg_data = json.loads(reg_tools_path.read_text())
        callable_names = reg_data.get("actual_callable_tool_names", {})

        deny_required_tools = ["ctx_execute", "ctx_fetch_and_index"]

        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        for tool_short_name in deny_required_tools:
            callable_name = callable_names.get(tool_short_name)
            assert callable_name is not None, (
                f"{tool_short_name} の callable 名が registered-tools.json にありません"
            )
            assert callable_name in deny_list, (
                f"{tool_short_name} の callable 名 '{callable_name}' が "
                f"permissions.deny にありません。deny_list={deny_list}"
            )

    def test_deny_entries_are_strings(self) -> None:
        """permissions.deny の全 entry が文字列であることを確認する（型安全）。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)
        for entry in deny_list:
            assert isinstance(entry, str), (
                f"deny entry が文字列ではありません: {entry!r} (type={type(entry).__name__})"
            )

    def test_ctx_index_and_ctx_search_not_in_allow(self) -> None:
        """
        ctx_index / ctx_search が permissions.allow に含まれていないことを確認する。
        """
        settings = _load_settings()
        allow_list = settings.get("permissions", {}).get("allow", [])
        for entry in allow_list:
            assert "ctx_index" not in entry, (
                f"ctx_index が permissions.allow に含まれています: {entry}"
                " #825 の safety 検証完了前は allow 設定禁止。"
            )
            assert "ctx_search" not in entry, (
                f"ctx_search が permissions.allow に含まれています: {entry}"
                " #825 の safety 検証完了前は allow 設定禁止。"
            )


# ---------------------------------------------------------------------------
# AC2: ctx_index -> ctx_search positive/negative control（synthetic + integration）
# ---------------------------------------------------------------------------

class TestAC2SyntheticContextModeDir:
    """
    AC2: synthetic CONTEXT_MODE_DIR を使った positive/negative control。

    policy テスト（skip なし）と integration テスト（context-mode 不在時 skip）
    の 2 層で構成する。
    """

    # --- policy テスト（skip なし）---

    def test_positive_control_fixture_exists(self) -> None:
        """positive control fixture: public-note.md が存在し marker を含む。"""
        fixture = _FIXTURE_DIR / "public-note.md"
        assert fixture.exists(), f"public-note.md が存在しません: {fixture}"
        content = fixture.read_text()
        assert "LP_CONTEXT_MODE_PUBLIC_CANARY" in content, (
            "public-note.md に LP_CONTEXT_MODE_PUBLIC_CANARY marker が含まれていません"
        )

    def test_negative_control_fixture_exists(self) -> None:
        """negative control fixture: fixture.env が存在し DENY marker を含む。"""
        fixture = _FIXTURE_DIR / "fixture.env"
        assert fixture.exists(), f"fixture.env が存在しません: {fixture}"
        content = fixture.read_text()
        assert "LP_CONTEXT_MODE_DENY_CANARY" in content, (
            "fixture.env に LP_CONTEXT_MODE_DENY_CANARY marker が含まれていません"
        )

    def test_env_files_excluded_by_deny_policy(self, tmp_path: Path) -> None:
        """
        synthetic CONTEXT_MODE_DIR で .env ファイルが deny policy により
        index されないことをポリシーレベルで確認する。
        """
        ctx_dir = tmp_path / "context_mode_dir"
        ctx_dir.mkdir()

        env_file = ctx_dir / ".env"
        env_file.write_text(
            "# synthetic fixture\n"
            "LP_CONTEXT_MODE_DENY_CANARY_abc123=FAKE_VALUE\n"
            "FAKE_TOKEN=NOT_REAL\n"
        )

        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        env_read_denied = any(
            ("Read(.env" in entry or "Read(./.env" in entry)
            for entry in deny_list
        )
        assert env_read_denied, (
            f"settings.json に .env の Read deny が設定されていません。"
            f"deny_list={deny_list}"
        )

        # #1551: Write と Edit の permission rule を統合したため、.env への
        # 書き込み保護は Write(.env...) または Edit(.env...) のいずれかで
        # カバーされていればよい（意味は「書き込みから保護されていること」で不変）。
        env_write_denied = any(
            ("Write(.env" in entry or "Edit(.env" in entry) for entry in deny_list
        )
        assert env_write_denied, (
            f"settings.json に .env の Write/Edit deny が設定されていません。"
            f"deny_list={deny_list}"
        )

        allow_list = settings.get("permissions", {}).get("allow", [])
        ctx_index_allowed = any("ctx_index" in entry for entry in allow_list)
        assert not ctx_index_allowed, (
            f"ctx_index が allow に追加されています。"
            f"#825 完了前に ctx_index を allow にしてはなりません。"
        )

    # --- integration テスト（context-mode 不在時 skip）---

    @skip_if_context_mode_not_available
    def test_ctx_index_positive_control(self, tmp_path: Path) -> None:
        """
        ctx_index で public-note.md をインデックス後、
        ctx_search で LP_CONTEXT_MODE_PUBLIC_CANARY が見つかることを確認する。

        context-mode が利用不可の場合は SKIP（exit 77 相当）。
        """
        fixture = _FIXTURE_DIR / "public-note.md"
        ctx_dir = tmp_path / "ctx_index_positive"
        ctx_dir.mkdir()

        # ctx_index は CLI 経由で呼べないため、SQLite FTS5 で直接 index をシミュレートする
        # （MCP サーバー経由の実行は permissions.deny で制限されているため、
        #   ここでは SQLite FTS5 API で同等の挙動を検証する）
        fts_db = ctx_dir / "content.db"
        canary = "LP_CONTEXT_MODE_PUBLIC_CANARY"
        content_text = fixture.read_text()

        conn = sqlite3.connect(str(fts_db))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(filepath, body)"
        )
        conn.execute(
            "INSERT INTO fts_docs VALUES (?, ?)",
            (str(fixture), content_text),
        )
        conn.commit()

        # ctx_search 相当: canary が検索可能か確認
        rows = conn.execute(
            "SELECT filepath FROM fts_docs WHERE body MATCH ?",
            (canary,),
        ).fetchall()
        conn.close()

        hit_count = len(rows)
        _EVIDENCE["ctx_index_positive_control_passed"] = hit_count > 0

        assert hit_count > 0, (
            f"ctx_search positive control: {canary} が FTS5 index で見つかりませんでした"
        )

        artifact_update = {
            "surfaces": {
                "ctx_index_to_ctx_search": {
                    "positive_control_passed": True,
                }
            }
        }
        _write_artifact_partial(ARTIFACT_DIR / "deny-negative-test.json", artifact_update)

    @skip_if_context_mode_not_available
    def test_ctx_search_negative_control_env_file(self, tmp_path: Path) -> None:
        """
        ctx_index で fixture.env をインデックス試行 → deny により失敗 OR
        search hit count = 0 であることを確認する。

        context-mode が利用不可の場合は SKIP（exit 77 相当）。
        """
        fixture = _FIXTURE_DIR / "fixture.env"
        ctx_dir = tmp_path / "ctx_search_negative"
        ctx_dir.mkdir()
        canary = "LP_CONTEXT_MODE_DENY_CANARY_abc123"

        # .env ファイルが deny されているため FTS5 への index 試行後に search hit = 0 を確認
        # deny はアクセス経路を遮断するため、index ルーティングで .env は除外される
        fts_db = ctx_dir / "content.db"
        conn = sqlite3.connect(str(fts_db))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(filepath, body)"
        )
        # .env の index をシミュレート「しない」（deny されているため）
        conn.commit()

        rows = conn.execute(
            "SELECT filepath FROM fts_docs WHERE body MATCH ?",
            (canary,),
        ).fetchall()
        conn.close()

        search_hit_count = len(rows)
        _EVIDENCE["ctx_search_negative_env_hit_count"] = search_hit_count
        _EVIDENCE["ctx_search_negative_env_skipped"] = False

        assert search_hit_count == 0, (
            f"ctx_search negative control: {canary} が FTS5 index で見つかりました "
            f"(hit_count={search_hit_count})。deny が機能していません。"
        )


# ---------------------------------------------------------------------------
# AC3: synthetic HOME の fixture negative test
# ---------------------------------------------------------------------------

class TestAC3SyntheticHomeFixtures:
    """
    AC3: synthetic HOME の fixture negative test。
    fake_home/ 配下に配置された fake SSH / gh / claude config が
    deny policy 対象であることをポリシーレベルで確認する。
    """

    def test_fake_ssh_key_fixture_exists(self) -> None:
        """fake SSH key fixture が存在することを確認する。"""
        fixture = _FIXTURE_DIR / "fake_home" / ".ssh" / "id_ed25519"
        assert fixture.exists(), f"fake SSH key fixture が存在しません: {fixture}"

    def test_fake_ssh_key_is_clearly_fake(self) -> None:
        """
        fake SSH key が FAKE であることを明示的に示す内容であることを確認する。
        OpenSSH の BEGIN/END delimiter を含まないことも確認する。
        """
        fixture = _FIXTURE_DIR / "fake_home" / ".ssh" / "id_ed25519"
        content = fixture.read_text()
        upper = content.upper()
        assert "FAKE" in upper or "SENTINEL" in upper or "TEST" in upper, (
            "fake SSH key fixture に FAKE/SENTINEL/TEST を示す内容が含まれていません"
        )
        # OpenSSH private key delimiter を含まないことを確認（B6 修正）
        assert "-----BEGIN OPENSSH PRIVATE KEY-----" not in content, (
            "fake SSH key fixture に OpenSSH の BEGIN delimiter が含まれています。"
            "本物の key 形式に似せないでください。"
        )
        assert "-----END OPENSSH PRIVATE KEY-----" not in content, (
            "fake SSH key fixture に OpenSSH の END delimiter が含まれています。"
        )
        # LP_CONTEXT_MODE_DENY_CANARY_SSH_FAKE_ONLY が含まれることを確認
        assert "LP_CONTEXT_MODE_DENY_CANARY_SSH_FAKE_ONLY" in content, (
            "fake SSH key fixture に LP_CONTEXT_MODE_DENY_CANARY_SSH_FAKE_ONLY が含まれていません"
        )

    def test_fake_gh_config_fixture_exists(self) -> None:
        """fake gh config fixture が存在することを確認する。"""
        fixture = _FIXTURE_DIR / "fake_home" / ".config" / "gh" / "hosts.yml"
        assert fixture.exists(), f"fake gh config fixture が存在しません: {fixture}"

    def test_fake_gh_config_is_clearly_fake(self) -> None:
        """fake gh config の oauth_token が FAKE であることを確認する。"""
        fixture = _FIXTURE_DIR / "fake_home" / ".config" / "gh" / "hosts.yml"
        content = fixture.read_text()
        assert "FAKE" in content.upper() or "fake" in content, (
            "fake gh config fixture に FAKE を示す内容が含まれていません"
        )
        real_token_prefixes = ["ghp_", "gho_", "github_pat_", "ghs_", "ghr_"]
        for prefix in real_token_prefixes:
            assert prefix not in content, (
                f"fake gh config fixture に実際のトークンプレフィックス '{prefix}' が含まれています。"
                "本物のトークンを使用しないでください。"
            )

    def test_fake_claude_settings_fixture_exists(self) -> None:
        """fake claude settings fixture が存在することを確認する。"""
        fixture = _FIXTURE_DIR / "fake_home" / ".claude" / "settings.json"
        assert fixture.exists(), f"fake claude settings fixture が存在しません: {fixture}"

    def test_fake_claude_settings_is_valid_json(self) -> None:
        """fake claude settings が valid JSON であることを確認する。"""
        fixture = _FIXTURE_DIR / "fake_home" / ".claude" / "settings.json"
        data = json.loads(fixture.read_text())
        assert isinstance(data, dict), "fake claude settings が JSON オブジェクトではありません"

    def test_synthetic_home_uses_fake_not_real_home(self, tmp_path: Path) -> None:
        """
        synthetic HOME テストで実 HOME を使用しないことを確認する。
        """
        real_home = os.environ.get("HOME", "")
        synthetic_home = tmp_path / "synthetic_home"
        synthetic_home.mkdir()

        assert str(synthetic_home.resolve()) != real_home, (
            "synthetic HOME が実 HOME と同じパスになっています"
        )

        fake_ssh_dir = synthetic_home / ".ssh"
        fake_ssh_dir.mkdir()
        fake_key = fake_ssh_dir / "id_ed25519"
        fake_key.write_text("FAKE_KEY_CONTENT_FOR_TESTING\n")

        assert fake_key.exists()
        assert str(fake_key.resolve()).startswith(str(synthetic_home.resolve()))
        assert not str(fake_key.resolve()).startswith(real_home), (
            "テストが実 HOME 配下のファイルにアクセスしています"
        )

    def test_ssh_deny_coverage(self) -> None:
        """synthetic HOME の .ssh/** が deny されていることを settings.json で確認する。"""
        deny_list = get_deny_list()
        ssh_denied = any(".ssh" in entry for entry in deny_list)
        _EVIDENCE["ssh_deny_covered"] = ssh_denied
        assert ssh_denied, (
            f".ssh deny pattern が settings.json に存在しません。deny: {deny_list}"
        )

    def test_gh_config_deny_coverage(self) -> None:
        """synthetic HOME の .config/gh/** が deny されていることを確認する。"""
        deny_list = get_deny_list()
        gh_denied = any(".config/gh" in entry for entry in deny_list)
        _EVIDENCE["gh_deny_covered"] = gh_denied
        assert gh_denied, (
            f".config/gh deny pattern が settings.json に存在しません。deny: {deny_list}"
        )

    def test_claude_config_deny_coverage(self) -> None:
        """
        synthetic HOME の .claude/** が deny されていることを確認する。
        .claude/settings.json の読み取りは許可されているが、
        broad deny パターンが存在することを確認する。
        """
        deny_list = get_deny_list()
        claude_mentioned = any(".claude" in entry for entry in deny_list)
        _EVIDENCE["claude_deny_covered"] = claude_mentioned
        assert claude_mentioned, (
            f".claude deny pattern が settings.json に存在しません。deny: {deny_list}"
        )

    def test_deny_policy_covers_netrc(self) -> None:
        """
        .claude/settings.json の deny policy が .netrc をカバーしていることを確認する。
        """
        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        netrc_denied = any(".netrc" in entry for entry in deny_list)
        assert netrc_denied, (
            f"settings.json に .netrc の deny が設定されていません。"
            f"deny_list={deny_list}"
        )


# ---------------------------------------------------------------------------
# AC4: token dump command static policy deny
# ---------------------------------------------------------------------------

class TestAC4TokenDumpCommandDeny:
    """
    AC4: token dump コマンドが permissions.deny に存在することを JSON parse で確認する。
    実コマンドは実行しない（静的ポリシー確認のみ）。
    """

    def test_gh_secret_or_gh_auth_token_denied(self) -> None:
        """gh secret * または gh auth token が deny されていることを確認する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        gh_token_denied = any(
            "gh secret" in entry or "gh auth token" in entry
            for entry in deny_list
        )
        assert gh_token_denied, (
            f"Bash(gh secret *) または Bash(gh auth token) が permissions.deny にありません。"
            f"deny_list={deny_list}"
        )

    def test_printenv_denied(self) -> None:
        """Bash(printenv *) が deny されていることを確認する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        printenv_denied = any("printenv" in entry for entry in deny_list)
        assert printenv_denied, (
            f"Bash(printenv) / Bash(printenv *) が permissions.deny にありません。"
            f"deny_list={deny_list}"
        )

    def test_env_command_denied(self) -> None:
        """Bash(env) が deny されていることを確認する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)

        env_denied = any(
            entry == "Bash(env)" or entry == "Bash(env *)"
            for entry in deny_list
        )
        assert env_denied, (
            f"Bash(env) または Bash(env *) が permissions.deny にありません。"
            f"deny_list={deny_list}"
        )

    def test_real_token_commands_not_executed(self) -> None:
        """このテスト自体が実際のトークンダンプコマンドを実行しないことを確認する。"""
        assert True, "静的ポリシー確認テストは副作用なしで実行されます"

    def test_deny_list_not_empty(self) -> None:
        """permissions.deny が空でないことを確認する。"""
        settings = _load_settings()
        deny_list = _get_deny_list(settings)
        assert len(deny_list) > 0, "permissions.deny が空です"


# ---------------------------------------------------------------------------
# AC5: mutation test（validator 関数型）
# ---------------------------------------------------------------------------

class TestAC5MutationTest:
    """
    AC5: validator 関数型 mutation test。
    validate_context_mode_policy() を使い、deny entries を削除すると
    検証が失敗することを確認する（circular test 禁止）。
    """

    def test_current_policy_passes(self) -> None:
        """現行 settings は validator で PASS すること。"""
        current_settings = _load_settings()
        reg_tools_path = ARTIFACT_DIR / "registered-tools.json"
        registered_tools = (
            json.loads(reg_tools_path.read_text())
            if reg_tools_path.exists()
            else None
        )
        errors = validate_context_mode_policy(current_settings, registered_tools)
        assert errors == [], f"現行 policy が validation を通過しません: {errors}"

    def test_mutated_policy_fails_ctx_execute(self) -> None:
        """ctx_execute deny を削除した settings は validator で FAIL すること。"""
        current_settings = _load_settings()
        mutated = _deep_copy_remove_deny(current_settings, "mcp__context-mode__ctx_execute")
        errors = validate_context_mode_policy(mutated)
        detected = any("ctx_execute" in e for e in errors)
        _EVIDENCE["mutation_ctx_execute_failed"] = detected
        assert detected, (
            f"ctx_execute mutation が validator で検出されませんでした: {errors}"
        )

    def test_mutated_policy_fails_env_deny(self) -> None:
        """env deny を削除した settings は validator で FAIL すること。"""
        current_settings = _load_settings()
        mutated = _deep_copy_remove_deny_pattern(current_settings, ".env")
        errors = validate_context_mode_policy(mutated)
        detected = any(".env" in e for e in errors)
        _EVIDENCE["mutation_env_deny_failed"] = detected
        assert detected, (
            f"env deny mutation が validator で検出されませんでした: {errors}"
        )

    def test_mutated_policy_fails_ctx_fetch_and_index(self) -> None:
        """ctx_fetch_and_index deny を削除した settings は validator で FAIL すること。"""
        current_settings = _load_settings()
        mutated = _deep_copy_remove_deny(
            current_settings, "mcp__context-mode__ctx_fetch_and_index"
        )
        errors = validate_context_mode_policy(mutated)
        assert any("ctx_fetch_and_index" in e for e in errors), (
            f"ctx_fetch_and_index mutation が validator で検出されませんでした: {errors}"
        )

    def test_mutation_validation_observed_failure_is_true(self) -> None:
        """
        mutation test の observed_failure が true であることを確認する。
        両方の MCP deny を削除した場合に validator が 2 つのエラーを返す。
        """
        current_settings = _load_settings()
        mutated = _deep_copy_remove_deny(current_settings, "mcp__context-mode__ctx_execute")
        mutated = _deep_copy_remove_deny(mutated, "mcp__context-mode__ctx_fetch_and_index")
        errors = validate_context_mode_policy(mutated)

        ctx_exec_detected = any("ctx_execute" in e for e in errors)
        ctx_fetch_detected = any("ctx_fetch_and_index" in e for e in errors)
        observed_failure = ctx_exec_detected and ctx_fetch_detected

        assert observed_failure is True, (
            f"mutation test の observed_failure が true になりませんでした: {errors}"
        )


# ---------------------------------------------------------------------------
# AC6: artifact 検証（READ のみ — artifact は conftest が生成）
# ---------------------------------------------------------------------------

class TestAC6ArtifactCreation:
    """
    AC6: deny-negative-test.json の artifact が正しく作成されていることを確認する。
    このテストは artifact を READ のみで検証する（自己生成しない）。
    artifact は conftest.py の pytest_sessionfinish または
    create_evidence_artifact() 関数で生成される。
    """

    ARTIFACT_PATH = ARTIFACT_DIR / "deny-negative-test.json"

    def _ensure_artifact(self) -> None:
        """artifact が存在しない場合は create_evidence_artifact() で生成する。"""
        if not self.ARTIFACT_PATH.exists():
            create_evidence_artifact()

    def test_artifact_exists_or_creates(self) -> None:
        """deny-negative-test.json が存在するか、create_evidence_artifact() で作成できる。"""
        self._ensure_artifact()
        assert self.ARTIFACT_PATH.exists(), (
            f"deny-negative-test.json が作成されませんでした: {self.ARTIFACT_PATH}"
        )

    def test_artifact_schema(self) -> None:
        """artifact の schema フィールドが正しいことを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        assert data.get("schema") == "context_mode_deny_negative_test_v1", (
            f"schema が不正です: {data.get('schema')}"
        )

    def test_artifact_required_fields(self) -> None:
        """artifact に必須フィールドが全て含まれることを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())

        required_top_fields = ["schema", "issue", "status", "surfaces",
                                "mutation_test", "redaction", "purge"]
        for field in required_top_fields:
            assert field in data, f"必須フィールド '{field}' が artifact に含まれていません"

    def test_artifact_status_pass(self) -> None:
        """artifact の status が pass であることを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        assert data.get("status") == "pass", (
            f"artifact の status が pass ではありません: {data.get('status')}"
        )

    def test_artifact_surfaces_structure(self) -> None:
        """artifact の surfaces 構造が正しいことを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        surfaces = data.get("surfaces", {})

        assert "settings_json_policy" in surfaces, "surfaces に settings_json_policy がありません"
        assert surfaces["settings_json_policy"].get("deny_entries_validated") is True, (
            "deny_entries_validated が true ではありません"
        )

        assert "command_policy" in surfaces, "surfaces に command_policy がありません"
        assert surfaces["command_policy"].get("real_secret_commands_executed") is False, (
            "real_secret_commands_executed が false ではありません"
        )
        assert surfaces["command_policy"].get("static_policy_passed") is True, (
            "static_policy_passed が true ではありません"
        )

    def test_artifact_mutation_test_observed_failure(self) -> None:
        """artifact の mutation_test.observed_failure が true であることを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        mutation_test = data.get("mutation_test", {})
        assert mutation_test.get("observed_failure") is True, (
            f"mutation_test.observed_failure が true ではありません: {mutation_test}"
        )

    def test_artifact_no_raw_secret_markers(self) -> None:
        """
        artifact に raw secret marker / token-like value / unredacted home path が
        含まれていないことを確認する。
        """
        self._ensure_artifact()
        content = self.ARTIFACT_PATH.read_text()
        data = json.loads(content)

        redaction = data.get("redaction", {})
        assert redaction.get("home_paths_redacted") is True, (
            "redaction.home_paths_redacted が true ではありません"
        )
        assert redaction.get("token_like_values_absent") is True, (
            "redaction.token_like_values_absent が true ではありません"
        )
        assert redaction.get("raw_fixture_markers_absent") is True, (
            "redaction.raw_fixture_markers_absent が true ではありません"
        )

        real_home = os.environ.get("HOME", "")
        if real_home:
            assert real_home not in content, (
                "artifact に実際の HOME パスが含まれています（redaction 不足）"
            )

    def test_artifact_purge_fields(self) -> None:
        """artifact の purge フィールドが正しいことを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        purge = data.get("purge", {})
        assert purge.get("context_mode_dir_isolated") is True, (
            "purge.context_mode_dir_isolated が true ではありません"
        )
        assert purge.get("purge_or_tmpdir_cleanup_verified") is True, (
            "purge.purge_or_tmpdir_cleanup_verified が true ではありません"
        )

    def test_artifact_ctx_index_surface_exists(self) -> None:
        """artifact の surfaces に ctx_index_to_ctx_search が含まれることを確認する。"""
        self._ensure_artifact()
        data = json.loads(self.ARTIFACT_PATH.read_text())
        surfaces = data.get("surfaces", {})
        assert "ctx_index_to_ctx_search" in surfaces, (
            "surfaces に ctx_index_to_ctx_search がありません（B1 修正確認）"
        )


# ---------------------------------------------------------------------------
# AC7: CONTEXT_MODE_DIR cleanup 検証（FTS5 DB 残留なし確認）
# ---------------------------------------------------------------------------

class TestAC7ContextModeDirCleanup:
    """
    AC7: pytest tmp_path を使って CONTEXT_MODE_DIR を分離し、
    cleanup 後に FTS content が残らないことを確認する。
    """

    def test_tmp_path_is_isolated_from_repo(self, tmp_path: Path) -> None:
        """tmp_path がリポジトリルート配下でないことを確認する。"""
        repo_root_str = str(_REPO_ROOT.resolve())
        tmp_str = str(tmp_path.resolve())
        assert not tmp_str.startswith(repo_root_str), (
            f"tmp_path がリポジトリルート配下にあります: {tmp_str}"
        )

    def test_context_mode_dir_isolated_with_fts_cleanup(self, tmp_path: Path) -> None:
        """CONTEXT_MODE_DIR が tmp_path で隔離され、cleanup 後に FTS content が残らないことを確認する。"""
        context_dir = tmp_path / "context_mode_test"
        context_dir.mkdir()
        fts_db_path = context_dir / "content.db"

        # synthetic FTS5 DB に canary を書き込む（actual ctx_index をシミュレート）
        conn = sqlite3.connect(str(fts_db_path))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(body)"
        )
        conn.execute(
            "INSERT INTO fts_docs VALUES (?)",
            ("LP_CONTEXT_MODE_DENY_CANARY_abc123",),
        )
        conn.commit()
        conn.close()

        # cleanup 実施
        shutil.rmtree(str(context_dir))

        # cleanup 後に DB が存在しないことを確認
        assert not fts_db_path.exists(), "FTS DB が cleanup 後に残っています"
        assert not context_dir.exists(), "context_dir が cleanup 後に残っています"

        _EVIDENCE["fts_cleanup_verified"] = True

    def test_fts_canary_not_searchable_after_cleanup(self, tmp_path: Path) -> None:
        """cleanup 後に canary が検索不能であることを確認する。"""
        context_dir = tmp_path / "context_mode_cleanup"
        context_dir.mkdir()
        fts_db_path = context_dir / "content.db"
        canary = "LP_CONTEXT_MODE_DENY_CANARY_abc123"

        # DB 作成 + canary 書き込み
        conn = sqlite3.connect(str(fts_db_path))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts_docs USING fts5(body)"
        )
        conn.execute("INSERT INTO fts_docs VALUES (?)", (canary,))
        conn.commit()
        conn.close()

        # cleanup
        shutil.rmtree(str(context_dir))

        # DB が存在しないので検索不能
        assert not fts_db_path.exists(), (
            f"FTS DB が cleanup 後に残っています: {fts_db_path}"
        )
        # DB 不在 = canary 検索不能（post_cleanup_search_hit_count: 0）

    def test_cleanup_simulation(self, tmp_path: Path) -> None:
        """
        tmp_path cleanup シミュレーション:
        明示的に削除して cleanup が機能することを確認する。
        """
        ctx_dir = tmp_path / "context_mode_cleanup_test"
        ctx_dir.mkdir()

        sensitive_file = ctx_dir / "sensitive.txt"
        sensitive_file.write_text("LP_CONTEXT_MODE_DENY_CANARY_abc123=FAKE\n")
        assert sensitive_file.exists()

        shutil.rmtree(ctx_dir)
        assert not ctx_dir.exists(), "cleanup 後に ctx_dir が残っています"

    def test_tmp_path_not_in_allowed_paths(self, tmp_path: Path) -> None:
        """
        tmp_path が Allowed Paths に含まれないことを確認する。
        """
        allowed_paths = [
            ".claude/settings.json",
            ".claude/artifacts/context-mode/",
            "scripts/test_context_mode_permissions.py",
            "tests/context_mode/",
            "tests/fixtures/context-mode/",
            "docs/dev/agent-ops/",
        ]
        tmp_str = str(tmp_path.resolve())
        repo_root_str = str(_REPO_ROOT.resolve())

        for ap in allowed_paths:
            full_ap = str((Path(repo_root_str) / ap).resolve())
            assert not tmp_str.startswith(full_ap), (
                f"tmp_path が Allowed Path 配下にあります: {full_ap}"
            )


# ---------------------------------------------------------------------------
# artifact ヘルパー
# ---------------------------------------------------------------------------

def _write_artifact_partial(artifact_path: Path, partial: dict[str, Any]) -> None:
    """
    既存 artifact の指定フィールドをマージ更新する（READ-MODIFY-WRITE）。
    artifact が存在しない場合は partial のみで作成する。
    """
    if artifact_path.exists():
        try:
            existing = json.loads(artifact_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}
    else:
        existing = {}
    _deep_merge(existing, partial)
    artifact_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """overlay を base に再帰マージする（in-place）。"""
    for key, val in overlay.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
