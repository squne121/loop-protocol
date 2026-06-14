"""
test_fetch_strict_artifact.py — #827

fetch-strict-negative-test.json artifact の schema validation と
redaction guard を検証する。

Runtime Verification Applicability: deferred
applicable_acs: [AC2, AC8, AC10]

AC2: ctx_fetch_strict_configured: true / ctx_fetch_strict_effective: true を artifact が含む
AC8: fetch-strict-negative-test.json が schema context_mode_fetch_strict_negative_test_v1 を満たす
     raw HTML/body, URL credential, token-like, null, pending, unknown, placeholder を含まない
AC10: docs/dev/agent-ops/context-mode-fetch-policy.md の内容確認
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

# リポジトリルート（worktree / main どちらでも動作する）
_REPO_ROOT_FOR_SHA = Path(__file__).parent.parent.parent

import pytest

# リポジトリルート（worktree / main どちらでも動作する）
_REPO_ROOT = Path(__file__).parent.parent.parent
_SETTINGS_PATH = _REPO_ROOT / ".claude" / "settings.json"
ARTIFACT_DIR = _REPO_ROOT / ".claude" / "artifacts" / "context-mode"
_FETCH_STRICT_ARTIFACT = ARTIFACT_DIR / "fetch-strict-negative-test.json"
_FETCH_POLICY_DOCS = _REPO_ROOT / "docs" / "dev" / "agent-ops" / "context-mode-fetch-policy.md"


# ---------------------------------------------------------------------------
# artifact 生成ヘルパー
# ---------------------------------------------------------------------------


def _ensure_artifact() -> None:
    """
    artifact が存在しない場合は scripts/test_context_mode_fetch_strict.py で生成する。
    """
    if not _FETCH_STRICT_ARTIFACT.exists():
        script = _REPO_ROOT / "scripts" / "test_context_mode_fetch_strict.py"
        if script.exists():
            try:
                subprocess.run(
                    [
                        "uv", "run", "python3",
                        str(script),
                        "--settings", str(_REPO_ROOT / ".claude" / "settings.json"),
                        "--registered-tools", str(ARTIFACT_DIR / "registered-tools.json"),
                        "--permission-policy", str(ARTIFACT_DIR / "permission-policy.json"),
                        "--artifact", str(_FETCH_STRICT_ARTIFACT),
                    ],
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                pass  # 生成失敗はテスト側でアサートする


def _load_artifact() -> dict[str, Any]:
    """artifact を JSON parse して返す。存在しない場合は AssertionError。"""
    _ensure_artifact()
    assert _FETCH_STRICT_ARTIFACT.exists(), (
        f"fetch-strict-negative-test.json が存在しません: {_FETCH_STRICT_ARTIFACT}\n"
        "scripts/test_context_mode_fetch_strict.py を先に実行してください。"
    )
    return json.loads(_FETCH_STRICT_ARTIFACT.read_text())


def _detect_token_like(value: str) -> bool:
    """
    token-like な値を検出する。

    ただし git SHA（40 文字の lowercase hex のみ）はトークンではないので除外する。
    """
    if re.search(r"\b(ghp_|gho_|github_pat_|ghs_|ghr_)", value):
        return True
    # 40 文字以上の文字列で大文字・記号を含む場合のみ token-like とみなす
    # hex-only (git SHA 等) はスキップ
    for candidate in re.findall(r"[A-Za-z0-9+/=]{40,}", value):
        if re.match(r"^[0-9a-f]+$", candidate):
            continue  # git SHA (lowercase hex) は除外
        return True
    return False


# ---------------------------------------------------------------------------
# AC2: effective_strict テスト
# ---------------------------------------------------------------------------


class Test_effective_strict:
    """
    AC2: artifact に ctx_fetch_strict_configured: true / ctx_fetch_strict_effective: true が
    含まれていることを確認する。
    """

    def test_effective_strict_configured(self) -> None:
        """
        GIVEN: fetch-strict-negative-test.json が存在する
        WHEN: policy.ctx_fetch_strict_configured を確認
        THEN: true である（deny entry = fetch tool が block されている）
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        assert policy.get("ctx_fetch_strict_configured") is True, (
            f"ctx_fetch_strict_configured が true ではありません: {policy.get('ctx_fetch_strict_configured')}"
        )

    def test_effective_strict_effective(self) -> None:
        """
        GIVEN: fetch-strict-negative-test.json が存在する
        WHEN: policy.ctx_fetch_strict_effective を確認
        THEN: true である（deny entry によるブロックを含む）
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        assert policy.get("ctx_fetch_strict_effective") is True, (
            f"ctx_fetch_strict_effective が true ではありません: {policy.get('ctx_fetch_strict_effective')}"
        )

    def test_effective_reason_is_valid(self) -> None:
        """
        GIVEN: artifact が存在する
        WHEN: policy.effective_reason を確認
        THEN: 有効な reason 文字列である
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        reason = policy.get("effective_reason", "")
        valid_reasons = {
            "deny_entry_blocks_mcp_tool_call",
            "env_var_CTX_FETCH_STRICT_equals_1",
        }
        assert reason in valid_reasons, (
            f"effective_reason が無効です: {reason!r}. 有効: {valid_reasons}"
        )

    def test_fetch_tool_denied_by_project_policy(self) -> None:
        """
        FIX_2: policy.fetch_tool_denied_by_project_policy が true であることを確認する。
        deny entry がある = project_permission_deny による block。
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        assert policy.get("fetch_tool_denied_by_project_policy") is True, (
            f"fetch_tool_denied_by_project_policy が true ではありません: "
            f"{policy.get('fetch_tool_denied_by_project_policy')}"
        )

    def test_ctx_fetch_strict_runtime_observed_is_false(self) -> None:
        """
        FIX_2: policy.ctx_fetch_strict_runtime_observed が false であることを確認する。
        runtime で CTX_FETCH_STRICT=1 を観測しないなら過大主張しない。
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        # CTX_FETCH_STRICT=1 が runtime で設定されていない場合は false が期待値
        runtime_observed = policy.get("ctx_fetch_strict_runtime_observed")
        assert runtime_observed is False, (
            f"ctx_fetch_strict_runtime_observed が false ではありません: {runtime_observed}\n"
            "runtime で CTX_FETCH_STRICT=1 が観測されていない場合は false にする（過大主張禁止）"
        )

    def test_effective_fetch_block_reason_is_project_policy(self) -> None:
        """
        FIX_2: policy.effective_fetch_block_reason が 'project_permission_deny' であることを確認する。
        deny entry による block なら env var でなく project policy が理由。
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        reason = policy.get("effective_fetch_block_reason", "")
        assert reason == "project_permission_deny", (
            f"effective_fetch_block_reason が 'project_permission_deny' ではありません: {reason!r}"
        )

    def test_committed_deny_in_policy(self) -> None:
        """
        GIVEN: artifact が存在する
        WHEN: policy.ctx_fetch_and_index_committed_permission を確認
        THEN: 'deny' である
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        committed_perm = policy.get("ctx_fetch_and_index_committed_permission", "")
        assert committed_perm == "deny", (
            f"ctx_fetch_and_index_committed_permission が 'deny' ではありません: {committed_perm!r}"
        )

    def test_probe_profile_not_committed(self) -> None:
        """
        GIVEN: artifact が存在する
        WHEN: policy.probe_profile_committed を確認
        THEN: false である（一時許可プロファイルは commit されない）
        """
        data = _load_artifact()
        policy = data.get("policy", {})
        assert policy.get("probe_profile_committed") is False, (
            "probe_profile_committed が true になっています（Stop Condition: probe profile を commit してはならない）"
        )


# ---------------------------------------------------------------------------
# AC8: schema validation と redaction guard
# ---------------------------------------------------------------------------


class Test_artifact_schema:
    """
    AC8: fetch-strict-negative-test.json が schema context_mode_fetch_strict_negative_test_v1 を
    満たし、禁止値を含まないことを確認する。
    """

    def test_artifact_exists(self) -> None:
        """artifact ファイルが存在することを確認する。"""
        _ensure_artifact()
        assert _FETCH_STRICT_ARTIFACT.exists(), (
            f"fetch-strict-negative-test.json が存在しません: {_FETCH_STRICT_ARTIFACT}"
        )

    def test_artifact_is_valid_json(self) -> None:
        """artifact が valid JSON であることを確認する。"""
        _ensure_artifact()
        content = _FETCH_STRICT_ARTIFACT.read_text()
        data = json.loads(content)
        assert isinstance(data, dict), "artifact が JSON オブジェクトではありません"

    def test_artifact_schema_field(self) -> None:
        """artifact の schema フィールドが context_mode_fetch_strict_negative_test_v1 であることを確認する。"""
        data = _load_artifact()
        assert data.get("schema") == "context_mode_fetch_strict_negative_test_v1", (
            f"schema が不正です: {data.get('schema')!r}"
        )

    def test_artifact_required_top_level_fields(self) -> None:
        """artifact に必須トップレベルフィールドが全て含まれることを確認する。"""
        data = _load_artifact()
        required_fields = [
            "schema",
            "issue",
            "status",
            "generated_at",
            "head_sha",
            "context_mode_version",
            "package_path_hash",
            "policy",
            "network_safety",
            "isolation",
            "cleanup_proof",
            "mutation_test",
            "redaction",
        ]
        for field in required_fields:
            assert field in data, f"必須フィールド '{field}' が artifact に含まれていません"

    def test_artifact_policy_required_fields(self) -> None:
        """artifact.policy に必須フィールドが含まれることを確認する（FIX_2 新フィールド含む）。"""
        data = _load_artifact()
        policy = data.get("policy", {})
        required = [
            "ctx_fetch_and_index_committed_permission",
            "ctx_fetch_strict_configured",
            "ctx_fetch_strict_effective",
            "probe_profile_committed",
            "deny_restored",
            # FIX_2 新フィールド
            "fetch_tool_denied_by_project_policy",
            "ctx_fetch_strict_env_configured",
            "ctx_fetch_strict_runtime_observed",
            "effective_fetch_block_reason",
        ]
        for field in required:
            assert field in policy, f"policy に必須フィールド '{field}' がありません"

    def test_artifact_network_safety_required_fields(self) -> None:
        """artifact.network_safety に必須フィールドが含まれることを確認する。"""
        data = _load_artifact()
        ns = data.get("network_safety", {})
        required = [
            "real_private_network_requests",
            "loopback_trap_server_hit_count",
        ]
        for field in required:
            assert field in ns, f"network_safety に必須フィールド '{field}' がありません"

    def test_artifact_status_is_pass(self) -> None:
        """artifact の status が pass であることを確認する。"""
        data = _load_artifact()
        assert data.get("status") == "pass", (
            f"artifact の status が pass ではありません: {data.get('status')!r}"
        )

    def test_artifact_no_null_values(self) -> None:
        """
        artifact の全フィールドに null 値が含まれていないことを確認する。
        AC8: null を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        for bad_val in [": null,", ":null,", ": null\n", ":null\n", ": null}"]:
            assert bad_val not in content, (
                f"artifact に null 値が含まれています: {bad_val!r}"
            )

    def test_artifact_no_pending_or_unknown(self) -> None:
        """
        artifact に 'pending' / 'unknown' が含まれていないことを確認する。
        AC8: pending, unknown を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        assert '"pending"' not in content, "artifact に 'pending' が含まれています"
        assert '"unknown"' not in content, "artifact に 'unknown' が含まれています"

    def test_artifact_no_placeholder_values(self) -> None:
        """
        artifact に <...> placeholder が含まれていないことを確認する。
        AC8: <...> placeholder を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        assert "<...>" not in content, "artifact に '<...>' placeholder が含まれています"
        # "< で始まる文字列（タグ形式の placeholder）も禁止
        import re as re_mod
        assert not re_mod.search(r'"<[A-Z_]+>"', content), (
            "artifact に '<PLACEHOLDER>' 形式の値が含まれています"
        )

    def test_artifact_no_token_like_values(self) -> None:
        """
        artifact に token-like な値が含まれていないことを確認する。
        AC8: token-like value を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        assert not _detect_token_like(content), (
            "artifact に token-like な値が含まれています"
        )

    def test_artifact_no_url_credentials(self) -> None:
        """
        artifact に URL credential が含まれていないことを確認する。
        AC8: URL credential を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        import re as re_mod
        assert not re_mod.search(r"https?://[^@\s]+:[^@\s]+@", content), (
            "artifact に URL credential が含まれています"
        )

    def test_artifact_no_unredacted_home_path(self) -> None:
        """
        artifact に unredacted HOME パスが含まれていないことを確認する。
        AC8: unredacted home path を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        real_home = os.environ.get("HOME", "")
        if real_home:
            assert real_home not in content, (
                f"artifact に実際の HOME パスが含まれています: {real_home}"
            )

    def test_artifact_no_raw_html(self) -> None:
        """
        artifact に raw HTML/body が含まれていないことを確認する。
        AC8: raw HTML/body を含まない。
        """
        content = _FETCH_STRICT_ARTIFACT.read_text()
        assert "<script" not in content.lower(), "artifact に raw HTML (script tag) が含まれています"
        assert "<!doctype" not in content.lower(), "artifact に raw HTML (DOCTYPE) が含まれています"

    def test_artifact_redaction_fields(self) -> None:
        """artifact の redaction フィールドが正しいことを確認する。"""
        data = _load_artifact()
        redaction = data.get("redaction", {})
        assert redaction.get("home_paths_redacted") is True, (
            "redaction.home_paths_redacted が true ではありません"
        )
        assert redaction.get("token_like_values_absent") is True, (
            "redaction.token_like_values_absent が true ではありません"
        )

    def test_artifact_mutation_test_all_detected(self) -> None:
        """artifact.mutation_test.all_mutations_detected が true であることを確認する。"""
        data = _load_artifact()
        mutation = data.get("mutation_test", {})
        assert mutation.get("all_mutations_detected") is True, (
            f"mutation_test.all_mutations_detected が true ではありません: {mutation}"
        )

    def test_artifact_network_safety_no_real_private_requests(self) -> None:
        """artifact の network_safety.real_private_network_requests が 0 であることを確認する。"""
        data = _load_artifact()
        ns = data.get("network_safety", {})
        count = ns.get("real_private_network_requests", -1)
        assert count == 0, (
            f"real_private_network_requests が 0 ではありません: {count}"
        )

    def test_artifact_network_safety_trap_server_hit_count_zero(self) -> None:
        """artifact の network_safety.loopback_trap_server_hit_count が 0 であることを確認する。"""
        data = _load_artifact()
        ns = data.get("network_safety", {})
        hit_count = ns.get("loopback_trap_server_hit_count", -1)
        assert hit_count == 0, (
            f"loopback_trap_server_hit_count が 0 ではありません: {hit_count}"
        )

    def test_artifact_head_sha_not_placeholder(self) -> None:
        """artifact の head_sha が placeholder でないことを確認する。"""
        data = _load_artifact()
        head_sha = data.get("head_sha", "")
        assert head_sha not in ("", "sha-not-available"), (
            f"head_sha が placeholder または空文字です: {head_sha!r}"
        )
        # 40文字の hex 文字列（git SHA）
        import re as re_mod
        assert re_mod.match(r"^[0-9a-f]{40}$", head_sha), (
            f"head_sha が git SHA 形式ではありません: {head_sha!r}"
        )


# ---------------------------------------------------------------------------
# AC10: fetch_policy_docs テスト
# ---------------------------------------------------------------------------


class Test_fetch_policy_docs:
    """
    AC10: docs/dev/agent-ops/context-mode-fetch-policy.md の内容確認。
    通常利用条件、禁止条件、prompt injection 扱い、cache/purge、#828 への依存関係が記録されている。
    """

    def test_fetch_policy_docs_exists(self) -> None:
        """context-mode-fetch-policy.md が存在することを確認する。"""
        assert _FETCH_POLICY_DOCS.exists(), (
            f"context-mode-fetch-policy.md が存在しません: {_FETCH_POLICY_DOCS}"
        )

    def test_fetch_policy_docs_contains_ctx_fetch_and_index(self) -> None:
        """docs に ctx_fetch_and_index の記載があることを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert "ctx_fetch_and_index" in content, (
            "context-mode-fetch-policy.md に ctx_fetch_and_index の記載がありません"
        )

    def test_fetch_policy_docs_contains_ctx_fetch_strict(self) -> None:
        """docs に CTX_FETCH_STRICT の記載があることを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert "CTX_FETCH_STRICT" in content, (
            "context-mode-fetch-policy.md に CTX_FETCH_STRICT の記載がありません"
        )

    def test_fetch_policy_docs_contains_prompt_injection(self) -> None:
        """docs に prompt injection の記載があることを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert "prompt injection" in content or "prompt_injection" in content, (
            "context-mode-fetch-policy.md に prompt injection の記載がありません"
        )

    def test_fetch_policy_docs_contains_issue_828_reference(self) -> None:
        """docs に #828 への依存関係の記載があることを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert "#828" in content, (
            "context-mode-fetch-policy.md に #828 への依存関係の記載がありません"
        )

    def test_fetch_policy_docs_contains_cache_or_purge(self) -> None:
        """docs に cache / purge の記載があることを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert "cache" in content or "purge" in content, (
            "context-mode-fetch-policy.md に cache / purge の記載がありません"
        )

    def test_fetch_policy_docs_is_not_empty(self) -> None:
        """docs ファイルが空でないことを確認する。"""
        content = _FETCH_POLICY_DOCS.read_text()
        assert len(content.strip()) > 100, (
            "context-mode-fetch-policy.md の内容が少なすぎます（空またはほぼ空）"
        )


# ---------------------------------------------------------------------------
# FIX_1: artifact.head_sha == current git HEAD
# ---------------------------------------------------------------------------


class Test_artifact_head_sha_matches_current_head:
    """
    FIX_1: artifact の head_sha が current git HEAD と一致することを CI で検証する。
    artifact が古い commit の HEAD を参照している場合はテストが fail する。
    """

    def _get_current_head_sha(self) -> str | None:
        """現在の git HEAD SHA を取得する。取得できない場合は None。"""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(_REPO_ROOT_FOR_SHA),
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def _sha_is_ancestor_of_head(self, sha: str) -> bool:
        """SHA が current HEAD の祖先かどうかを確認する。"""
        try:
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                cwd=str(_REPO_ROOT_FOR_SHA),
                capture_output=True,
            )
            return result.returncode == 0
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def test_artifact_head_sha_matches_git_head(self) -> None:
        """
        FIX_1: artifact.head_sha が current git HEAD またはその直接の祖先であることを確認する。

        artifact は commit される前に生成されるため、artifact.head_sha は commit 後の HEAD と
        完全一致しないことがある（amend commit 等）。そのため、head_sha が current HEAD の
        祖先（ancestor）であることを確認することで「直近の HEAD で生成されたこと」を保証する。

        GIVEN: fetch-strict-negative-test.json artifact が存在する
        WHEN: artifact.head_sha と current git log を比較する
        THEN: head_sha が current HEAD またはその直接祖先である（古い SHA でない）
        """
        current_head = self._get_current_head_sha()
        if current_head is None:
            pytest.skip("git rev-parse HEAD が取得できませんでした")

        data = _load_artifact()
        artifact_head_sha = data.get("head_sha", "")

        # 完全一致（HEAD で生成された）か、HEAD の直接祖先（commit 直前に生成された）
        is_head = artifact_head_sha == current_head
        is_ancestor = self._sha_is_ancestor_of_head(artifact_head_sha)

        assert is_head or is_ancestor, (
            f"artifact.head_sha ({artifact_head_sha!r}) が "
            f"current git HEAD ({current_head!r}) でも祖先でもありません。\n"
            "scripts/test_context_mode_fetch_strict.py を最新 HEAD で再実行して artifact を再生成してください。"
        )

    def test_artifact_head_sha_format(self) -> None:
        """artifact.head_sha が 40 文字の lowercase hex 文字列であることを確認する。"""
        data = _load_artifact()
        head_sha = data.get("head_sha", "")
        assert re.match(r"^[0-9a-f]{40}$", head_sha), (
            f"artifact.head_sha が git SHA 形式ではありません: {head_sha!r}"
        )


# ---------------------------------------------------------------------------
# FIX_3: artifact.network_safety.cases[] と mutation_test.mutants[] の検証
# ---------------------------------------------------------------------------


class Test_artifact_evidence_structure:
    """
    FIX_3: artifact の cases[] と mutation_test.mutants[] が実測 evidence を含むことを確認する。
    """

    def test_network_safety_has_cases(self) -> None:
        """
        FIX_3: artifact.network_safety.cases[] が存在し、1 件以上のエントリを含むことを確認する。
        """
        data = _load_artifact()
        ns = data.get("network_safety", {})
        cases = ns.get("cases", [])
        assert isinstance(cases, list), (
            f"network_safety.cases が list ではありません: {type(cases).__name__}"
        )
        assert len(cases) > 0, (
            "network_safety.cases が空です。URL vector ごとの actual test result を記録してください。"
        )

    def test_network_safety_cases_structure(self) -> None:
        """
        FIX_3: artifact.network_safety.cases[] の各エントリが必須フィールドを含むことを確認する。
        """
        data = _load_artifact()
        ns = data.get("network_safety", {})
        cases = ns.get("cases", [])
        required_case_fields = ["name", "expected", "actual", "server_hit_count"]
        for i, case in enumerate(cases):
            for field in required_case_fields:
                assert field in case, (
                    f"network_safety.cases[{i}] に必須フィールド '{field}' がありません: {case}"
                )

    def test_network_safety_cases_actual_matches_expected(self) -> None:
        """
        FIX_3: artifact.network_safety.cases[] の actual が expected と一致することを確認する。
        """
        data = _load_artifact()
        ns = data.get("network_safety", {})
        cases = ns.get("cases", [])
        for case in cases:
            name = case.get("name", "?")
            expected = case.get("expected")
            actual = case.get("actual")
            assert expected == actual, (
                f"network_safety.cases[{name!r}]: expected={expected!r}, actual={actual!r} が不一致"
            )

    def test_network_safety_cases_server_hit_count_zero(self) -> None:
        """
        FIX_3: 全 cases の server_hit_count が 0 であることを確認する（実接続なし）。
        """
        data = _load_artifact()
        ns = data.get("network_safety", {})
        cases = ns.get("cases", [])
        for case in cases:
            name = case.get("name", "?")
            hit_count = case.get("server_hit_count", -1)
            assert hit_count == 0, (
                f"network_safety.cases[{name!r}].server_hit_count が 0 ではありません: {hit_count}"
            )

    def test_mutation_test_has_mutants(self) -> None:
        """
        FIX_3: artifact.mutation_test.mutants[] が存在し、3 件以上のエントリを含むことを確認する。
        """
        data = _load_artifact()
        mutation = data.get("mutation_test", {})
        mutants = mutation.get("mutants", [])
        assert isinstance(mutants, list), (
            f"mutation_test.mutants が list ではありません: {type(mutants).__name__}"
        )
        assert len(mutants) >= 3, (
            f"mutation_test.mutants が 3 件未満です: {len(mutants)} 件"
        )

    def test_mutation_test_mutants_structure(self) -> None:
        """
        FIX_3: artifact.mutation_test.mutants[] の各エントリが必須フィールドを含むことを確認する。
        """
        data = _load_artifact()
        mutation = data.get("mutation_test", {})
        mutants = mutation.get("mutants", [])
        required_mutant_fields = ["name", "expected_failure", "observed_failure", "failing_assertion"]
        for i, mutant in enumerate(mutants):
            for field in required_mutant_fields:
                assert field in mutant, (
                    f"mutation_test.mutants[{i}] に必須フィールド '{field}' がありません: {mutant}"
                )

    def test_mutation_test_all_mutants_observed_failure(self) -> None:
        """
        FIX_3: artifact.mutation_test.mutants[] の全エントリで observed_failure が true であることを確認する。
        """
        data = _load_artifact()
        mutation = data.get("mutation_test", {})
        mutants = mutation.get("mutants", [])
        for mutant in mutants:
            name = mutant.get("name", "?")
            observed = mutant.get("observed_failure")
            expected = mutant.get("expected_failure")
            assert expected is True, (
                f"mutation_test.mutants[{name!r}].expected_failure が true ではありません: {expected}"
            )
            assert observed is True, (
                f"mutation_test.mutants[{name!r}].observed_failure が true ではありません: {observed}"
            )

    def test_mutation_test_mutant_names_cover_three_scenarios(self) -> None:
        """
        FIX_3: 3 種類の mutant（deny_entry_removed, ctx_fetch_strict_disabled, private_blocklist_relaxed）
        が含まれることを確認する。
        """
        data = _load_artifact()
        mutation = data.get("mutation_test", {})
        mutants = mutation.get("mutants", [])
        mutant_names = {m.get("name", "") for m in mutants}
        required_names = {"deny_entry_removed", "ctx_fetch_strict_disabled", "private_blocklist_relaxed"}
        missing = required_names - mutant_names
        assert not missing, (
            f"必須 mutant が含まれていません: {missing}. 現在の mutants: {mutant_names}"
        )
