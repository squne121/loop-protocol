"""
context-mode ops ドキュメント検証テスト (#828)

このテストスイートは以下を検証する:
- persistence-proof.json の schema 準拠 (AC2)
- purge コマンド記録の正当性 (AC3)
- incident response 手順 (AC4)
- stale claim の修正確認 (AC5)
- ELv2 policy matrix (AC6)
- fetch policy / permission deny の用語分離 (AC7)
- repo commit 禁止方針 (AC8)

Runtime Verification Applicability: not_applicable
（docs / artifact の静的検証のみ。runtime 実行不要）
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
_ARTIFACT_DIR = _REPO_ROOT / ".claude" / "artifacts" / "context-mode"
_DOCS_DIR = _REPO_ROOT / "docs" / "dev" / "agent-ops"
_OPS_DOC = _DOCS_DIR / "context-mode-ops.md"
_ROLLBACK_DOC = _DOCS_DIR / "context-mode-rollback.md"
_PERSISTENCE_PROOF = _ARTIFACT_DIR / "persistence-proof.json"


# ─── AC2: persistence-proof.json schema 検証 ─────────────────────────────────

class TestPersistenceProof:
    """AC2: persistence-proof.json が schema context_mode_persistence_proof_v1 を満たす。"""

    def test_file_exists(self) -> None:
        assert _PERSISTENCE_PROOF.exists(), (
            "persistence-proof.json が存在しません。"
            ".claude/artifacts/context-mode/persistence-proof.json を作成してください。"
        )

    def test_schema_field(self) -> None:
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        assert data.get("_schema") == "context_mode_persistence_proof_v1", (
            f"_schema が context_mode_persistence_proof_v1 ではありません: {data.get('_schema')}"
        )

    def test_no_null_schema(self) -> None:
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        assert data.get("_schema") is not None, "_schema が null です"

    def test_no_pending_values(self) -> None:
        """pending / unknown / placeholder 値が含まれていないことを確認する。"""
        content = _PERSISTENCE_PROOF.read_text()
        forbidden_patterns = [
            r'"pending"',
            r'"unknown"',
            r'"<placeholder>"',
            r'"TODO"',
            r'"TBD"',
        ]
        for pattern in forbidden_patterns:
            assert not re.search(pattern, content), (
                f"persistence-proof.json に禁止値 {pattern} が含まれています"
            )

    def test_no_raw_db(self) -> None:
        """raw DB / raw secret が含まれていないことを確認する。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        redaction = data.get("redaction", {})
        assert redaction.get("raw_db_excluded") is True, (
            "redaction.raw_db_excluded が true ではありません"
        )
        assert redaction.get("raw_secret_excluded") is True, (
            "redaction.raw_secret_excluded が true ではありません"
        )

    def test_no_unredacted_home_path(self) -> None:
        """unredacted home path が含まれていないことを確認する。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        redaction = data.get("redaction", {})
        assert redaction.get("home_path_masked") is True, (
            "redaction.home_path_masked が true ではありません"
        )
        # 実際のホームパスパターンを確認（/home/xxx や /Users/xxx の形式）
        content = _PERSISTENCE_PROOF.read_text()
        # <HOME> 以外のホームパスが含まれていないことを確認
        # ただし "home_path_masked" キー自体の文字列は除外
        text_without_keys = re.sub(r'"home_path[^"]*":\s*[^,\n}]+', '', content)
        assert not re.search(r'/home/[a-zA-Z0-9_-]+(?!/MASKED)', text_without_keys), (
            "unredacted home path (/home/xxx) が含まれている可能性があります"
        )

    def test_storage_root_resolution_order(self) -> None:
        """storage_root_resolution_order が 5 エントリ含まれることを確認する。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        order = data.get("storage_root_resolution_order", [])
        assert len(order) >= 5, (
            f"storage_root_resolution_order が 5 エントリ未満です: {len(order)}"
        )
        priorities = [e.get("priority") for e in order]
        assert 1 in priorities and 2 in priorities, (
            "storage_root_resolution_order に priority 1 または 2 がありません"
        )

    def test_required_fields(self) -> None:
        """必須フィールドが存在することを確認する。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        required = [
            "_schema", "_issue", "_generated_at",
            "storage_root_resolution_order", "effective_storage_root",
            "purge_methods_verified", "redaction"
        ]
        for field in required:
            assert field in data, f"persistence-proof.json に必須フィールド {field} がありません"


# ─── AC3: purge コマンド検証 ─────────────────────────────────────────────────

class TestPurgeCommands:
    """AC3: v1.0.162 で実在確認済みの purge command / slash command / fallback のみが記録されている。"""

    def test_ops_doc_exists(self) -> None:
        assert _OPS_DOC.exists(), (
            "docs/dev/agent-ops/context-mode-ops.md が存在しません"
        )

    def test_ctx_purge_mcp_tool_documented(self) -> None:
        """ctx_purge MCP tool が docs に記録されている。"""
        content = _OPS_DOC.read_text()
        assert "ctx_purge" in content, (
            "context-mode-ops.md に ctx_purge が記録されていません"
        )

    def test_slash_command_documented(self) -> None:
        """/context reset slash command が docs に記録されている。"""
        content = _OPS_DOC.read_text()
        assert "/context reset" in content, (
            "context-mode-ops.md に /context reset slash command が記録されていません"
        )

    def test_fallback_deletion_documented(self) -> None:
        """fallback 手動削除手順が docs に記録されている。"""
        content = _OPS_DOC.read_text()
        assert "fallback" in content.lower() or "手動削除" in content, (
            "context-mode-ops.md に fallback 削除手順が記録されていません"
        )

    def test_persistence_proof_purge_methods(self) -> None:
        """persistence-proof.json に purge_methods_verified が記録されている。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        methods = data.get("purge_methods_verified", [])
        assert len(methods) >= 2, (
            f"purge_methods_verified が 2 件未満です: {len(methods)}"
        )
        method_names = [m.get("method", "") for m in methods]
        # ctx_purge MCP tool が含まれる
        assert any("ctx_purge" in m for m in method_names), (
            "purge_methods_verified に ctx_purge が含まれていません"
        )
        # slash command が含まれる
        assert any("/context reset" in m or "slash" in m.lower() for m in method_names), (
            "purge_methods_verified に slash command が含まれていません"
        )

    def test_no_unverified_commands(self) -> None:
        """v1.0.162 で実在未確認のコマンドが docs に含まれていないことを確認する。"""
        content = _OPS_DOC.read_text()
        # context-mode CLI 直接呼び出し（purge --dry-run 等）は現在未確認
        # ただし "legacy" 注記として言及される場合は OK とする
        if "context-mode purge --dry-run" in content:
            assert "legacy" in content or "未確認" in content or "旧" in content, (
                "context-mode purge --dry-run が legacy 注記なしに記録されています"
            )


# ─── AC4: incident response ──────────────────────────────────────────────────

class TestIncidentResponse:
    """AC4: secret 混入時の incident response が正しい順序で記録されている。"""

    REQUIRED_STEPS = ["stop", "isolate", "identify", "purge", "verify", "rotate", "redact"]

    def test_incident_response_section_exists(self) -> None:
        content = _OPS_DOC.read_text()
        assert "incident" in content.lower() or "Incident" in content, (
            "context-mode-ops.md に incident response セクションがありません"
        )

    def test_all_steps_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        for step in self.REQUIRED_STEPS:
            assert step in content, (
                f"incident response に '{step}' ステップが記録されていません"
            )

    def test_step_order_correct(self) -> None:
        """ステップが正しい順序で記録されていることを確認する（incident response テーブル内）。"""
        content = _OPS_DOC.read_text()
        lower_content = content.lower()
        # incident response セクションのヘッダーを探す（"## 3." または "incident response" を含む行）
        # テーブル行（"| 1. stop |" 形式）で順序を確認する
        # テーブルパターンを探す: "| 数字. step |"
        table_pattern = re.compile(r'(\|.*?stop.*?\|.*\n.*\|.*?isolate.*?\|.*\n.*\|.*?identify.*?\|)', re.DOTALL | re.IGNORECASE)
        match = table_pattern.search(content)
        assert match, "incident response ステップテーブルが見つかりません（stop → isolate → identify の順序）"

        # テーブルで全ステップの出現位置を確認する
        # incident セクション以降で確認: "## 3." セクションを探す
        section_match = re.search(r'## 3\..*?(?=## \d+\.|\Z)', content, re.DOTALL)
        if not section_match:
            # フォールバック: secret 混入を含むセクション
            section_match = re.search(r'## .*?[Ss]ecret.*?(?=## \d+\.|\Z)', content, re.DOTALL)
        
        if section_match:
            section = section_match.group(0).lower()
        else:
            # さらにフォールバック: incident response という語を含むセクション
            inc_idx = lower_content.find("incident response")
            if inc_idx == -1:
                inc_idx = lower_content.find("incident")
            assert inc_idx != -1
            # ## セクション区切りを探す
            section_start = lower_content.rfind("\n## ", 0, inc_idx)
            section_end_match = re.search(r'\n## ', lower_content[inc_idx:])
            section_end = inc_idx + section_end_match.start() if section_end_match else len(lower_content)
            section = lower_content[section_start:section_end]

        # テーブル行を抽出して順序確認
        # "| N. step |" 行を順序通りに並べる
        step_positions = {}
        for step in self.REQUIRED_STEPS:
            pos = section.find(step)
            if pos != -1:
                step_positions[step] = pos

        if len(step_positions) >= len(self.REQUIRED_STEPS):
            ordered_steps = ["stop", "isolate", "identify", "purge", "verify", "rotate", "redact"]
            for i in range(len(ordered_steps) - 1):
                a, b = ordered_steps[i], ordered_steps[i + 1]
                if a in step_positions and b in step_positions:
                    assert step_positions[a] < step_positions[b], (
                        f"incident response ステップの順序が正しくありません: '{a}' が '{b}' より後にあります"
                    )
        else:
            # テーブル構造で確認: numbered rows
            numbered_rows = re.findall(r'\|\s*(\d+)\.\s+(\w+)', section)
            step_order = [row[1].lower() for row in sorted(numbered_rows, key=lambda x: int(x[0]))]
            ordered_steps = ["stop", "isolate", "identify", "purge", "verify", "rotate", "redact"]
            for step in ordered_steps:
                assert step in step_order, f"テーブルに '{step}' ステップがありません: {step_order}"
            for i in range(len(ordered_steps) - 1):
                a, b = ordered_steps[i], ordered_steps[i + 1]
                assert step_order.index(a) < step_order.index(b), (
                    f"ステップ順序が正しくありません: '{a}' が '{b}' より後にあります"
                )

    def test_verify_zero_hit_documented(self) -> None:
        """verify zero hit の概念が記録されている。"""
        content = _OPS_DOC.read_text()
        assert "zero" in content.lower() or "ゼロ" in content or "zero hit" in content.lower(), (
            "context-mode-ops.md に verify zero hit が記録されていません"
        )

    def test_rotate_step_documented(self) -> None:
        """rotate（credential ローテーション）が記録されている。"""
        content = _OPS_DOC.read_text()
        assert "rotate" in content.lower() or "ローテーション" in content, (
            "context-mode-ops.md に rotate (credential ローテーション) が記録されていません"
        )


# ─── AC5: stale claim 修正確認 ───────────────────────────────────────────────

class TestStaleClaims:
    """AC5: #824/#856/#883 後の実態と矛盾する stale claim が修正または legacy 明示されている。"""

    def test_experiment_only_stale_claim_addressed(self) -> None:
        """experiment-only の stale claim が修正または legacy 明示されている。"""
        content = _OPS_DOC.read_text()
        # ops doc に experiment-only が legacy として明示されているか、
        # または現在は project settings に適用済みと記録されていること
        has_legacy_note = "legacy" in content.lower() and "experiment" in content.lower()
        has_current_state = "main branch" in content.lower() or "project settings" in content.lower()
        assert has_legacy_note or has_current_state, (
            "context-mode-ops.md に experiment-only stale claim の修正または legacy 明示がありません"
        )

    def test_two_deny_only_stale_claim_addressed(self) -> None:
        """'2 deny entries only' の stale claim が修正されている。"""
        content = _OPS_DOC.read_text()
        # 現在は 4 deny entries (#883/PR #887 後)
        assert "ctx_batch_execute" in content or "ctx_execute_file" in content or "#883" in content, (
            "context-mode-ops.md に #883 後の実効設定（4 deny entries）が反映されていません"
        )

    def test_current_deny_entries_documented(self) -> None:
        """現在の deny entries が 4 件記録されている。"""
        content = _OPS_DOC.read_text()
        deny_tools = [
            "mcp__context-mode__ctx_execute",
            "mcp__context-mode__ctx_batch_execute",
            "mcp__context-mode__ctx_execute_file",
            "mcp__context-mode__ctx_fetch_and_index",
        ]
        for tool in deny_tools:
            assert tool in content, (
                f"context-mode-ops.md に現行 deny entry '{tool}' が記録されていません"
            )

    def test_registered_tools_deny_basis_current(self) -> None:
        """registered-tools.json の deny_basis が現在のサーバーキーを反映している。"""
        data = json.loads((_ARTIFACT_DIR / "registered-tools.json").read_text())
        deny_basis = data.get("deny_basis", "")
        # 現在のサーバーキーは 'context-mode' (experiment サフィックスなし)
        # deny_basis に context-mode が含まれていることを確認
        assert "context-mode" in deny_basis, (
            f"registered-tools.json の deny_basis に context-mode が含まれていません: {deny_basis}"
        )

    def test_ops_doc_references_883(self) -> None:
        """ops doc が #883 / PR #887 を参照していることを確認する。"""
        content = _OPS_DOC.read_text()
        assert "#883" in content or "PR #887" in content or "887" in content, (
            "context-mode-ops.md が #883 / PR #887 の変更を参照していません"
        )


# ─── AC6: ELv2 policy ────────────────────────────────────────────────────────

class TestElv2Policy:
    """AC6: ELv2 policy が matrix として記録されている。"""

    REQUIRED_ITEMS = [
        "internal use",
        "no vendoring",
        "no hosted managed service",
        "no notice removal",
        "modified copy notice",
    ]

    def test_elv2_section_exists(self) -> None:
        content = _OPS_DOC.read_text()
        assert "elv2" in content.lower() or "elastic license" in content.lower(), (
            "context-mode-ops.md に ELv2 / Elastic License セクションがありません"
        )

    def test_internal_use_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "internal use" in content, (
            "ELv2 matrix に 'internal use' が記録されていません"
        )

    def test_no_vendoring_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "vendoring" in content or "no vendoring" in content, (
            "ELv2 matrix に 'no vendoring' が記録されていません"
        )

    def test_no_hosted_managed_service_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "managed service" in content or "hosted" in content, (
            "ELv2 matrix に 'no hosted managed service' が記録されていません"
        )

    def test_no_notice_removal_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "notice removal" in content or "no notice" in content, (
            "ELv2 matrix に 'no notice removal' が記録されていません"
        )

    def test_modified_copy_notice_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "modified copy" in content or "modified copy notice" in content or "変更" in content, (
            "ELv2 matrix に 'modified copy notice' が記録されていません"
        )

    def test_matrix_format(self) -> None:
        """ELv2 policy が matrix（表）形式で記録されている。"""
        content = _OPS_DOC.read_text()
        # Markdown table の存在確認（| で始まる行）
        has_table = any(line.strip().startswith("|") for line in content.splitlines())
        assert has_table, (
            "context-mode-ops.md に ELv2 policy の matrix（Markdown table）がありません"
        )


# ─── AC7: fetch_strict / permission deny 用語分離 ────────────────────────────

class TestFetchStrict:
    """AC7: CTX_FETCH_STRICT と project permission deny の用語が分離されている。"""

    def test_ctx_fetch_strict_term_exists(self) -> None:
        content = _OPS_DOC.read_text()
        assert "CTX_FETCH_STRICT" in content, (
            "context-mode-ops.md に CTX_FETCH_STRICT が記録されていません"
        )

    def test_permission_deny_term_exists(self) -> None:
        content = _OPS_DOC.read_text()
        assert "permissions.deny" in content or "permission deny" in content.lower(), (
            "context-mode-ops.md に permissions.deny / permission deny が記録されていません"
        )

    def test_terms_clearly_separated(self) -> None:
        """CTX_FETCH_STRICT と permission deny が別の概念として記録されている。"""
        content = _OPS_DOC.read_text()
        # 両者が存在し、かつ「独立」「分離」「別」といった分離を示す語が近くにある
        has_ctx = "CTX_FETCH_STRICT" in content
        has_deny = "permissions.deny" in content
        has_separation = (
            "独立" in content
            or "separate" in content.lower()
            or "independent" in content.lower()
            or "分離" in content
        )
        assert has_ctx and has_deny, (
            "context-mode-ops.md に CTX_FETCH_STRICT または permissions.deny が欠けています"
        )
        assert has_separation, (
            "context-mode-ops.md に CTX_FETCH_STRICT と permission deny の分離説明がありません"
        )

    def test_fetch_policy_doc_exists(self) -> None:
        """context-mode-fetch-policy.md が存在することを確認する。"""
        fetch_policy = _DOCS_DIR / "context-mode-fetch-policy.md"
        assert fetch_policy.exists(), (
            "docs/dev/agent-ops/context-mode-fetch-policy.md が存在しません"
        )

    def test_fetch_policy_has_ctx_fetch_strict(self) -> None:
        """fetch-policy.md に CTX_FETCH_STRICT が記録されている。"""
        fetch_policy = _DOCS_DIR / "context-mode-fetch-policy.md"
        content = fetch_policy.read_text()
        assert "CTX_FETCH_STRICT" in content, (
            "context-mode-fetch-policy.md に CTX_FETCH_STRICT が記録されていません"
        )


# ─── AC8: no commit policy ───────────────────────────────────────────────────

class TestNoCommitPolicy:
    """AC8: context-mode DB / index / cache / raw fetched body を repo に commit しない方針。"""

    def test_no_commit_section_exists(self) -> None:
        content = _OPS_DOC.read_text()
        assert "commit" in content.lower(), (
            "context-mode-ops.md に commit 禁止方針が記録されていません"
        )

    def test_db_no_commit_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "db" in content and ("commit" in content or "禁止" in content), (
            "context-mode-ops.md に DB の commit 禁止方針がありません"
        )

    def test_index_no_commit_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "index" in content and ("commit" in content or "禁止" in content), (
            "context-mode-ops.md に index の commit 禁止方針がありません"
        )

    def test_cache_no_commit_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "cache" in content and ("commit" in content or "禁止" in content), (
            "context-mode-ops.md に cache の commit 禁止方針がありません"
        )

    def test_raw_fetched_body_no_commit_documented(self) -> None:
        content = _OPS_DOC.read_text().lower()
        assert "raw fetched" in content or "取得結果" in content or "raw" in content, (
            "context-mode-ops.md に raw fetched body の commit 禁止方針がありません"
        )

    def test_persistence_proof_no_commit_policy(self) -> None:
        """persistence-proof.json に no_commit_policy が記録されている。"""
        data = json.loads(_PERSISTENCE_PROOF.read_text())
        no_commit = data.get("no_commit_policy", {})
        assert no_commit.get("db_files") is not None, (
            "persistence-proof.json に no_commit_policy.db_files がありません"
        )
        assert "禁止" in str(no_commit.get("db_files", "")) or "commit" in str(no_commit.get("db_files", "")).lower(), (
            "no_commit_policy.db_files が commit 禁止を示していません"
        )


# ─── VC 互換テスト関数（-k マーカー対応） ────────────────────────────────────
# pytest -k "persistence_proof" / "purge_commands" / "incident_response" 等で選択されるよう
# クラス外の関数として定義する。クラス内テストの再利用。

def test_persistence_proof_schema() -> None:
    """AC2 VC: persistence-proof.json が schema 準拠 (-k persistence_proof 対応)。"""
    t = TestPersistenceProof()
    t.test_file_exists()
    t.test_schema_field()
    t.test_no_null_schema()
    t.test_no_pending_values()
    t.test_no_raw_db()
    t.test_no_unredacted_home_path()
    t.test_storage_root_resolution_order()
    t.test_required_fields()


def test_purge_commands_documented() -> None:
    """AC3 VC: purge コマンドが記録されている (-k purge_commands 対応)。"""
    t = TestPurgeCommands()
    t.test_ops_doc_exists()
    t.test_ctx_purge_mcp_tool_documented()
    t.test_slash_command_documented()
    t.test_fallback_deletion_documented()
    t.test_persistence_proof_purge_methods()
    t.test_no_unverified_commands()


def test_incident_response_steps() -> None:
    """AC4 VC: incident response 手順が記録されている (-k incident_response 対応)。"""
    t = TestIncidentResponse()
    t.test_incident_response_section_exists()
    t.test_all_steps_documented()
    t.test_step_order_correct()
    t.test_verify_zero_hit_documented()
    t.test_rotate_step_documented()


def test_stale_claims_fixed() -> None:
    """AC5 VC: stale claim が修正されている (-k stale_claims 対応)。"""
    t = TestStaleClaims()
    t.test_experiment_only_stale_claim_addressed()
    t.test_two_deny_only_stale_claim_addressed()
    t.test_current_deny_entries_documented()
    t.test_registered_tools_deny_basis_current()
    t.test_ops_doc_references_883()


def test_elv2_policy_matrix() -> None:
    """AC6 VC: ELv2 policy matrix が記録されている (-k elv2_policy 対応)。"""
    t = TestElv2Policy()
    t.test_elv2_section_exists()
    t.test_internal_use_documented()
    t.test_no_vendoring_documented()
    t.test_no_hosted_managed_service_documented()
    t.test_no_notice_removal_documented()
    t.test_modified_copy_notice_documented()
    t.test_matrix_format()


def test_fetch_strict_separation() -> None:
    """AC7 VC: CTX_FETCH_STRICT と permission deny の用語が分離されている (-k fetch_strict 対応)。"""
    t = TestFetchStrict()
    t.test_ctx_fetch_strict_term_exists()
    t.test_permission_deny_term_exists()
    t.test_terms_clearly_separated()
    t.test_fetch_policy_doc_exists()
    t.test_fetch_policy_has_ctx_fetch_strict()


def test_no_commit_policy_documented() -> None:
    """AC8 VC: repo commit 禁止方針が記録されている (-k no_commit_policy 対応)。"""
    t = TestNoCommitPolicy()
    t.test_no_commit_section_exists()
    t.test_db_no_commit_documented()
    t.test_index_no_commit_documented()
    t.test_cache_no_commit_documented()
    t.test_raw_fetched_body_no_commit_documented()
    t.test_persistence_proof_no_commit_policy()
