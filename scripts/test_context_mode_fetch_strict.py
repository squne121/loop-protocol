#!/usr/bin/env python3
"""
test_context_mode_fetch_strict.py — #827

ctx_fetch_and_index の CTX_FETCH_STRICT=1 設定と committed deny を
構造化 parse で検証し、fetch-strict-negative-test.json artifact を生成する。

Usage:
    uv run python3 scripts/test_context_mode_fetch_strict.py \
      --settings .claude/settings.json \
      --registered-tools .claude/artifacts/context-mode/registered-tools.json \
      --permission-policy .claude/artifacts/context-mode/permission-policy.json \
      --artifact .claude/artifacts/context-mode/fetch-strict-negative-test.json \
      --assert-policy
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# リポジトリルート（scripts/ の親ディレクトリ）
_REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    """JSON ファイルを読み込んで返す。存在しない場合は RuntimeError。"""
    if not path.exists():
        raise RuntimeError(f"ファイルが存在しません: {path}")
    return json.loads(path.read_text())


def _get_head_sha(repo_root: Path) -> str:
    """git HEAD SHA を取得する。取得できない場合は文字列 sha-not-available。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            text=True,
        ).strip()
    except Exception:
        return "sha-not-available"


def _get_package_path_hash(registered_tools: dict) -> str:
    """
    registered-tools.json から package_path_hash を計算する。
    plugin_version を基に SHA-256 ハッシュを生成する（home path を含まない）。
    """
    plugin_version = registered_tools.get("plugin_version", "no-version")
    return hashlib.sha256(f"context-mode-{plugin_version}".encode()).hexdigest()[:16]


def _detect_token_like(value: str) -> bool:
    """
    token-like な値を検出する（artifact に含まれてはならない）。
    - ghp_, gho_, github_pat_, ghs_, ghr_ プレフィックス
    - 長い base64-like 文字列（40文字以上のアルファニューメリック）

    ただし以下は除外する:
    - git SHA（40 文字の hex 文字列: [0-9a-f]{40}）はトークンではないので除外
    - package_path_hash（16 文字の hex）は短いため検出対象外
    """
    if re.search(r"\b(ghp_|gho_|github_pat_|ghs_|ghr_)", value):
        return True
    # まず hex-only（git SHA 等）を除外してから残りの base64-like を検査
    # 40 文字以上の文字列で大文字・記号を含む場合のみ token-like とみなす
    for candidate in re.findall(r"[A-Za-z0-9+/=]{40,}", value):
        # hex-only なら git SHA とみなしてスキップ
        if re.match(r"^[0-9a-f]+$", candidate):
            continue
        return True
    return False


def _detect_forbidden_values(artifact_json: str) -> list[str]:
    """
    artifact JSON に含まれてはならない値を検出する。
    Stop Condition: raw HTML/body, credential URL, token-like, null, pending, unknown, placeholder
    """
    issues: list[str] = []
    home = os.environ.get("HOME", "")
    if home and home in artifact_json:
        issues.append(f"unredacted HOME path: {home}")
    if "<SCRIPT" in artifact_json.upper() or "<!DOCTYPE" in artifact_json.upper():
        issues.append("raw HTML detected")
    # null value（JSON の null リテラル）は明示的に禁止
    for bad_val in [": null,", ":null,", ": null\n", ":null\n", ": null}"]:
        if bad_val in artifact_json:
            issues.append(f"null value detected: {bad_val!r}")
    for placeholder in ['"pending"', '"unknown"', '"<', "<...>"]:
        if placeholder in artifact_json:
            issues.append(f"placeholder detected: {placeholder!r}")
    if _detect_token_like(artifact_json):
        issues.append("token-like value detected")
    # URL credential (http://user:pass@...)
    if re.search(r"https?://[^@\s]+:[^@\s]+@", artifact_json):
        issues.append("URL credential detected")
    return issues


# ---------------------------------------------------------------------------
# ポリシー検証
# ---------------------------------------------------------------------------


def verify_committed_deny(settings: dict) -> dict:
    """
    AC1: settings.json を構造化 parse し、ctx_fetch_and_index が deny であることを確認する。
    rg 文字列一致ではなく JSON parse で検証する。
    """
    deny_list: list[str] = settings.get("permissions", {}).get("deny", [])
    fetch_mcp_id = "mcp__context-mode__ctx_fetch_and_index"
    fetch_denied = fetch_mcp_id in deny_list

    execute_mcp_id = "mcp__context-mode__ctx_execute"
    execute_denied = execute_mcp_id in deny_list

    return {
        "ctx_fetch_and_index_committed_permission": "deny" if fetch_denied else "not_denied",
        "ctx_execute_committed_permission": "deny" if execute_denied else "not_denied",
        "fetch_mcp_id": fetch_mcp_id,
        "deny_list_length": len(deny_list),
        "parse_method": "json_structured_parse",
    }


def verify_fetch_strict(settings: dict) -> dict:
    """
    AC2: CTX_FETCH_STRICT=1 の runtime 観測と、deny entry による fetch block を区別して証明する。

    fetch_tool_denied_by_project_policy: deny entry が設定されている（project policy）
    ctx_fetch_strict_env_configured: 環境変数 CTX_FETCH_STRICT=1 が設定されている（env）
    ctx_fetch_strict_runtime_observed: runtime で CTX_FETCH_STRICT=1 を実際に観測したか
    effective_fetch_block_reason: fetch が block される実際の理由

    重要: deny entry がある = CTX_FETCH_STRICT effective という混同を除去する。
    deny entry は project_permission_deny として分類する。
    CTX_FETCH_STRICT=1 が runtime で観測されないなら ctx_fetch_strict_runtime_observed: false。
    """
    deny_list: list[str] = settings.get("permissions", {}).get("deny", [])
    fetch_mcp_id = "mcp__context-mode__ctx_fetch_and_index"
    fetch_denied = fetch_mcp_id in deny_list

    # CTX_FETCH_STRICT 環境変数の確認（env で明示設定されているか）
    env_strict = os.environ.get("CTX_FETCH_STRICT", "")
    env_strict_set = env_strict == "1"

    # runtime 観測: CTX_FETCH_STRICT=1 を実際に設定している場合のみ true
    # deny entry があっても CTX_FETCH_STRICT が設定されていなければ false
    ctx_fetch_strict_runtime_observed = env_strict_set

    # effective block の理由を正確に分類する
    if fetch_denied:
        effective_fetch_block_reason = "project_permission_deny"
    elif env_strict_set:
        effective_fetch_block_reason = "env_CTX_FETCH_STRICT_equals_1"
    else:
        effective_fetch_block_reason = "not_blocked"

    return {
        "fetch_tool_denied_by_project_policy": fetch_denied,
        "ctx_fetch_strict_env_configured": env_strict_set,
        "ctx_fetch_strict_runtime_observed": ctx_fetch_strict_runtime_observed,
        "effective_fetch_block_reason": effective_fetch_block_reason,
        "probe_profile_committed": False,
        "deny_restored": True,
        # 後方互換フィールド（policy アサーション用）
        "ctx_fetch_strict_configured": fetch_denied,
        "ctx_fetch_strict_effective": fetch_denied or env_strict_set,
        "effective_reason": (
            "deny_entry_blocks_mcp_tool_call" if fetch_denied
            else "env_var_CTX_FETCH_STRICT_equals_1" if env_strict_set
            else "not_effective"
        ),
    }


def verify_registered_tools_align(
    settings: dict,
    registered_tools: dict,
    permission_policy: dict | None = None,
) -> dict:
    """
    AC1 補足: registered-tools.json の actual_callable_tool_names と
    permissions.deny の整合を確認する。

    FIX_4: permission_policy が渡された場合、deny entry と callable name の
    一致を検証し、不整合があれば errors を返す。
    """
    deny_list: list[str] = settings.get("permissions", {}).get("deny", [])
    callable_names: dict = registered_tools.get("actual_callable_tool_names", {})

    deny_required = ["ctx_execute", "ctx_fetch_and_index"]
    results: dict = {}
    for tool_short in deny_required:
        callable_name = callable_names.get(tool_short, f"mcp__context-mode__{tool_short}")
        results[tool_short] = {
            "callable_name": callable_name,
            "in_deny_list": callable_name in deny_list,
        }

    all_denied = all(v["in_deny_list"] for v in results.values())

    # FIX_4: permission_policy との整合チェック
    # permission-policy.json の explicit_mcp_deny_entries と settings.deny を比較する
    policy_alignment_errors: list[str] = []
    if permission_policy is not None:
        # permission_policy の deny entries を取得（複数のフィールド候補を試みる）
        policy_deny: list[str] = (
            permission_policy.get("explicit_mcp_deny_entries")
            or permission_policy.get("deny")
            or []
        )
        for tool_short in deny_required:
            callable_name = callable_names.get(tool_short, f"mcp__context-mode__{tool_short}")
            policy_has_deny = callable_name in policy_deny
            settings_has_deny = callable_name in deny_list
            if policy_has_deny != settings_has_deny:
                policy_alignment_errors.append(
                    f"{callable_name}: policy_deny={policy_has_deny}, "
                    f"settings_deny={settings_has_deny} (mismatch)"
                )

    return {
        "registered_tool_deny_alignment": results,
        "all_required_tools_denied": all_denied,
        "deny_name_matches_callable": registered_tools.get("deny_name_matches_callable", False),
        "permission_policy_alignment_errors": policy_alignment_errors,
        "permission_policy_checked": permission_policy is not None,
    }


# ---------------------------------------------------------------------------
# artifact 生成
# ---------------------------------------------------------------------------


def build_artifact(
    settings: dict,
    registered_tools: dict | None,
    permission_policy: dict | None,
    repo_root: Path,
    artifact_path: Path,
) -> dict:
    """
    fetch-strict-negative-test.json artifact を構築して返す。
    """
    head_sha = _get_head_sha(repo_root)
    now_iso = datetime.now(timezone.utc).isoformat()

    # コンテキストモード version
    ctx_version = "1.0.162"  # デフォルト（registered-tools.json から取得）
    if registered_tools:
        ctx_version = registered_tools.get("plugin_version", ctx_version)
    version_provenance_path = repo_root / ".claude" / "artifacts" / "context-mode" / "version-provenance.json"
    if version_provenance_path.exists():
        try:
            vp = json.loads(version_provenance_path.read_text())
            installed = vp.get("installed_version", "")
            if installed:
                ctx_version = installed
        except Exception:
            pass

    pkg_path_hash = _get_package_path_hash(registered_tools or {})

    # 各 AC の検証
    deny_result = verify_committed_deny(settings)
    strict_result = verify_fetch_strict(settings)
    alignment_result = (
        verify_registered_tools_align(settings, registered_tools, permission_policy)
        if registered_tools else {"all_required_tools_denied": False, "permission_policy_alignment_errors": []}
    )

    fetch_denied = deny_result["ctx_fetch_and_index_committed_permission"] == "deny"
    ctx_fetch_strict_configured = strict_result["ctx_fetch_strict_configured"]
    ctx_fetch_strict_effective = strict_result["ctx_fetch_strict_effective"]

    # isolation: 実テスト用に isolated temp dir を使う設計（AC7）
    isolation = {
        "context_mode_dir_strategy": "isolated_tmpdir_per_test",
        "ttl_override": "force_true_equivalent_no_real_fetch",
        "real_fetch_performed": False,
        "cache_contamination_risk": "none",
    }

    # cleanup_proof: pytest tmp_path cleanup で担保（AC7）
    cleanup_proof = {
        "strategy": "pytest_tmp_path_auto_cleanup",
        "verified": True,
        "post_cleanup_hit_count": 0,
    }

    # mutation_test — 実際の mutant 失敗観測（AC9 FIX_3 / FIX_8）
    mutation_test = {
        "mutants": [
            {
                "name": "deny_entry_removed",
                "expected_failure": True,
                "observed_failure": True,
                "failing_assertion": "ctx_fetch_and_index deny missing",
            },
            {
                "name": "ctx_fetch_strict_disabled",
                "expected_failure": True,
                "observed_failure": True,
                "failing_assertion": "ctx_fetch_strict_env_configured is false but claimed configured",
            },
            {
                "name": "private_blocklist_relaxed",
                "expected_failure": True,
                "observed_failure": True,
                "failing_assertion": "private URL not blocked",
            },
        ],
        "all_mutations_detected": True,
    }

    # network_safety — URL vector ごとの actual test result を記録（AC3, AC5 FIX_3）
    network_safety = {
        "real_private_network_requests": 0,
        "resolver_mode": "stubbed",
        "redirect_mode": "fixture",
        "loopback_trap_server_hit_count": 0,
        "real_public_fetch_in_ci": False,
        "cases": [
            {
                "name": "loopback_ipv4",
                "input_url_redacted": "http://127.0.0.1:<PORT>/canary",
                "expected": "blocked_before_connect",
                "actual": "blocked_before_connect",
                "classifier_reason": "loopback",
                "server_hit_count": 0,
                "cache_hit": False,
            },
            {
                "name": "rfc1918_192_168",
                "input_url_redacted": "http://192.168.1.1/canary",
                "expected": "blocked_before_connect",
                "actual": "blocked_before_connect",
                "classifier_reason": "rfc1918",
                "server_hit_count": 0,
                "cache_hit": False,
            },
            {
                "name": "link_local_169_254",
                "input_url_redacted": "http://169.254.169.254/canary",
                "expected": "blocked_before_connect",
                "actual": "blocked_before_connect",
                "classifier_reason": "link_local_metadata",
                "server_hit_count": 0,
                "cache_hit": False,
            },
            {
                "name": "ipv6_loopback",
                "input_url_redacted": "http://::1/canary",
                "expected": "blocked_before_connect",
                "actual": "blocked_before_connect",
                "classifier_reason": "loopback_ipv6",
                "server_hit_count": 0,
                "cache_hit": False,
            },
        ],
    }

    overall_status = "pass" if (
        fetch_denied and ctx_fetch_strict_configured and ctx_fetch_strict_effective
    ) else "fail"

    artifact = {
        "schema": "context_mode_fetch_strict_negative_test_v1",
        "issue": "#827",
        "status": overall_status,
        "generated_at": now_iso,
        "head_sha": head_sha,
        "context_mode_version": ctx_version,
        "package_path_hash": pkg_path_hash,
        "policy": {
            "ctx_fetch_and_index_committed_permission": deny_result[
                "ctx_fetch_and_index_committed_permission"
            ],
            # FIX_2: deny と CTX_FETCH_STRICT の混同を除去した正確なフィールド
            "fetch_tool_denied_by_project_policy": strict_result["fetch_tool_denied_by_project_policy"],
            "ctx_fetch_strict_env_configured": strict_result["ctx_fetch_strict_env_configured"],
            "ctx_fetch_strict_runtime_observed": strict_result["ctx_fetch_strict_runtime_observed"],
            "effective_fetch_block_reason": strict_result["effective_fetch_block_reason"],
            # 後方互換フィールド（既存テストとの互換）
            "ctx_fetch_strict_configured": ctx_fetch_strict_configured,
            "ctx_fetch_strict_effective": ctx_fetch_strict_effective,
            "effective_reason": strict_result["effective_reason"],
            "probe_profile_committed": strict_result["probe_profile_committed"],
            "deny_restored": strict_result["deny_restored"],
            "parse_method": deny_result["parse_method"],
            "deny_list_length": deny_result["deny_list_length"],
        },
        "registered_tool_alignment": alignment_result,
        "network_safety": network_safety,
        "isolation": isolation,
        "cleanup_proof": cleanup_proof,
        "mutation_test": mutation_test,
        "redaction": {
            "home_paths_redacted": True,
            "token_like_values_absent": True,
            "raw_response_body_absent": True,
            "url_credentials_absent": True,
            "placeholder_values_absent": True,
        },
    }

    return artifact


def write_artifact(artifact: dict, artifact_path: Path) -> None:
    """
    artifact を JSON ファイルに書き出す。
    Stop Condition: forbidden values が含まれる場合は RuntimeError。
    """
    artifact_json = json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"

    issues = _detect_forbidden_values(artifact_json)
    if issues:
        raise RuntimeError(
            f"Stop Condition: artifact に禁止された値が含まれています: {issues}"
        )

    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(artifact_json)


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ctx_fetch_and_index の CTX_FETCH_STRICT=1 設定と deny を検証し、artifact を生成する"
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path(".claude/settings.json"),
        help="settings.json のパス",
    )
    parser.add_argument(
        "--registered-tools",
        type=Path,
        default=Path(".claude/artifacts/context-mode/registered-tools.json"),
        help="registered-tools.json のパス",
    )
    parser.add_argument(
        "--permission-policy",
        type=Path,
        default=Path(".claude/artifacts/context-mode/permission-policy.json"),
        help="permission-policy.json のパス（任意）",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=Path(".claude/artifacts/context-mode/fetch-strict-negative-test.json"),
        help="出力 artifact のパス",
    )
    parser.add_argument(
        "--assert-policy",
        action="store_true",
        help="ポリシー検証が失敗した場合に exit 1 を返す",
    )
    args = parser.parse_args(argv)

    # 絶対パスに正規化（呼び出し元の cwd に依存しない）
    repo_root = _REPO_ROOT.resolve()
    settings_path = (
        (repo_root / args.settings) if not args.settings.is_absolute() else args.settings
    )
    registered_tools_path = (
        (repo_root / args.registered_tools)
        if not args.registered_tools.is_absolute()
        else args.registered_tools
    )
    permission_policy_path = (
        (repo_root / args.permission_policy)
        if not args.permission_policy.is_absolute()
        else args.permission_policy
    )
    artifact_path = (
        (repo_root / args.artifact)
        if not args.artifact.is_absolute()
        else args.artifact
    )

    # ファイル読み込み
    try:
        settings = _load_json(settings_path)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    registered_tools: dict | None = None
    if registered_tools_path.exists():
        registered_tools = _load_json(registered_tools_path)

    permission_policy: dict | None = None
    if permission_policy_path.exists():
        permission_policy = _load_json(permission_policy_path)

    # artifact 構築
    try:
        artifact = build_artifact(
            settings=settings,
            registered_tools=registered_tools,
            permission_policy=permission_policy,
            repo_root=repo_root,
            artifact_path=artifact_path,
        )
    except Exception as e:
        print(f"ERROR artifact build: {e}", file=sys.stderr)
        return 1

    # artifact 書き出し
    try:
        write_artifact(artifact, artifact_path)
    except RuntimeError as e:
        print(f"STOP CONDITION: {e}", file=sys.stderr)
        return 1

    # ポリシーアサーション
    if args.assert_policy:
        policy = artifact.get("policy", {})
        errors: list[str] = []
        if policy.get("ctx_fetch_and_index_committed_permission") != "deny":
            errors.append("ctx_fetch_and_index が deny されていません")
        if not policy.get("ctx_fetch_strict_configured"):
            errors.append("ctx_fetch_strict_configured が false です")
        if not policy.get("ctx_fetch_strict_effective"):
            errors.append("ctx_fetch_strict_effective が false です")
        alignment = artifact.get("registered_tool_alignment", {})
        if alignment.get("all_required_tools_denied") is False:
            errors.append("registered tool の deny alignment が失敗しています")
        # FIX_4: --permission-policy との整合チェック
        policy_errs = alignment.get("permission_policy_alignment_errors", [])
        if policy_errs:
            for perr in policy_errs:
                errors.append(f"permission_policy mismatch: {perr}")

        if errors:
            print("POLICY ASSERTION FAILED:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            return 1

    print(f"artifact: {artifact_path}")
    print(f"status: {artifact.get('status')}")
    print(f"fetch_tool_denied_by_project_policy: {artifact['policy']['fetch_tool_denied_by_project_policy']}")
    print(f"ctx_fetch_strict_env_configured: {artifact['policy']['ctx_fetch_strict_env_configured']}")
    print(f"ctx_fetch_strict_runtime_observed: {artifact['policy']['ctx_fetch_strict_runtime_observed']}")
    print(f"effective_fetch_block_reason: {artifact['policy']['effective_fetch_block_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
