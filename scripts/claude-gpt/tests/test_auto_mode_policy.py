"""scripts/claude-gpt/tests/test_auto_mode_policy.py

Issue #2203（2026-08-16 OWNER adversarial review 反映）の regression matrix。

`autoMode` は permissions.deny / PreToolUse hook / GitHub mutation transaction
broker の後段にある second-gate の判断補助であり、決定論的 authority ではない
（Configure auto mode ドキュメント準拠）。本ファイルはこの区別を前提として、
launcher-generated settings への narrow autoMode 注入・permission-mode
enforcement・classifyAllShell 必須化・GitHubMutationBroker の object-identity
state machine・standalone runtime canary（`auto_mode_canary.py`）の exit code
semantics・sanitized evidence schema・negative control を検証する。

Runtime Verification Applicability: immediate（applicable_acs: AC4, AC5, AC6,
AC7, AC8）。live 系テスト（AGY causal canary / GitHub mutation canary）は
required CLI/auth/runtime capability が利用不能な場合、`auto_mode_canary.py`
自身が stdout `SKIP:` 相当の exit 77 を返すことをアサートする（SKIP を PASS に
昇格しない。fallback 実行や擬似成功判定は行わない）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
LIB_SH = SCRIPT_DIR / "lib.sh"
LAUNCH_SH = SCRIPT_DIR / "launch.sh"
PREFLIGHT_SH = SCRIPT_DIR / "preflight.sh"
CANARY_PY = SCRIPT_DIR / "auto_mode_canary.py"

REAL_CLAUDE_BIN = shutil.which("claude")
REAL_GH_BIN = shutil.which("gh")


def _load_canary_module():
    spec = importlib.util.spec_from_file_location("auto_mode_canary", CANARY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass() の postponed annotation 解決には sys.modules[__module__] の登録が
    # 必要なため、exec 前に一意な module 名で登録する（複数回 import されても衝突しない）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


canary = _load_canary_module()


def _run_sh_function(function_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    """lib.sh を source し、指定した関数を呼び出して stdout を返す（POSIX sh subshell）。"""
    quoted_args = " ".join(f'"{arg}"' for arg in args)
    script = f'. "{LIB_SH}"; {function_name} {quoted_args}'
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20)


# --- AC1: generated_settings_defaults ---------------------------------------


def test_generated_settings_defaults_present_and_valid_json():
    """GIVEN lib.sh の claude_gpt_auto_mode_standalone_json
    WHEN 呼び出す
    THEN 妥当な JSON であり、environment/allow 配列の先頭に "$defaults" が
    存在し、classifyAllShell が true である
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    auto_mode = payload["autoMode"]
    assert auto_mode["environment"][0] == "$defaults"
    assert auto_mode["allow"][0] == "$defaults"
    assert auto_mode["classifyAllShell"] is True
    assert len(auto_mode["environment"]) >= 2
    assert len(auto_mode["allow"]) >= 2


def test_generated_settings_not_written_to_project_settings():
    """GIVEN project の .claude/settings.json / .claude/settings.local.json
    WHEN autoMode キーを検索する
    THEN autoMode は launcher-owned --settings にのみ存在し、project settings には
    追加されていない
    """
    repo_root = SCRIPT_DIR.parent.parent
    for name in (".claude/settings.json", ".claude/settings.local.json"):
        path = repo_root / name
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        assert '"autoMode"' not in content, f"{name} must not declare autoMode (Issue #2203 Out of Scope)"


@pytest.mark.skipif(REAL_CLAUDE_BIN is None, reason="claude CLI not available")
def test_generated_settings_defaults_readback_via_real_claude_cli(tmp_path):
    """GIVEN 実 claude CLI と launcher-owned settings fragment
    WHEN `preflight.sh --auto-mode-check <settings_path>` を実行する
    THEN readback は ok、$defaults 由来の hard_deny/soft_deny は不変、
    classifyAllShell は有効と判定される（AC1 PASS evidence）
    """
    claude_config_dir = tmp_path / "claude-gpt-home" / "claude"
    claude_config_dir.mkdir(parents=True)
    settings_path = claude_config_dir / "settings.local.json"
    fragment = _run_sh_function("claude_gpt_auto_mode_json_fragment").stdout.strip()
    settings_path.write_text("{\n  " + fragment + "\n}\n", encoding="utf-8")

    result = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"]["environment_narrow_label_present"] is True
    assert payload["checks"]["allow_narrow_label_present"] is True
    assert payload["checks"]["hard_soft_deny_unmodified"] is True
    assert payload["checks"]["classify_all_shell_enabled"] is True
    assert payload["digests"]["auto_mode_defaults_digest"] != "unknown"
    assert payload["digests"]["effective_config_digest"] != "unknown"


# --- AC2: narrow_policy_scope -------------------------------------------------


def test_narrow_policy_scope_mentions_only_trusted_repo_and_agy_route():
    """GIVEN 生成された autoMode fragment
    WHEN environment/allow の narrow label 文字列を確認する
    THEN squne121/loop-protocol と provider=agy route のみを明示し、broad gh api・
    arbitrary provider・credential forwarding は含まない
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    payload = json.loads(result.stdout)
    auto_mode = payload["autoMode"]
    env_label = auto_mode["environment"][1]
    allow_label = auto_mode["allow"][1]

    for label in (env_label, allow_label):
        assert "squne121/loop-protocol" in label
        assert "second-gate" in label or "second-gate" in env_label

    assert "provider=agy" in env_label or "agy" in allow_label
    # 「gh api」自体は "対象外である"（除外文脈）としてのみ現れてよいが、それを許可
    # する文言（bypassPermissions 等）は一切含まれてはならない。
    for term in ("bypassPermissions", "--dangerously-skip-permissions"):
        assert term not in env_label
        assert term not in allow_label
    assert "broad gh api" in env_label or "broad" in env_label
    assert "対象外" in env_label


def test_narrow_policy_scope_documents_second_gate_not_authority():
    """GIVEN narrow label
    WHEN authority に関する記述を確認する
    THEN autoMode が authority ではなく判断補助であることを明示する
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    payload = json.loads(result.stdout)
    allow_label = payload["autoMode"]["allow"][1]
    assert "authority" in allow_label


# --- AC3: isolation_and_auto_mode_enforcement --------------------------------

FAKE_PROXY_SOURCE = r"""#!/usr/bin/env python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODELS = ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna"]


def _serve(port: int) -> int:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/v1/models":
                body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            return

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return 1
    if args[0] == "--version":
        print("fake-claude-code-proxy 0.0.0-test")
        return 0
    if args[0] == "codex" and len(args) >= 3 and args[1] == "auth" and args[2] == "status":
        print("Account: fake-test-account")
        return 0
    if args[0] == "serve":
        port = None
        i = 1
        while i < len(args):
            if args[i] == "--port" and i + 1 < len(args):
                port = int(args[i + 1])
                i += 2
            else:
                i += 1
        if port is None:
            return 1
        return _serve(port)
    return 1


if __name__ == "__main__":
    sys.exit(main())
"""

FAKE_CLAUDE_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import sys

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(sys.argv[1:], fh)
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT_CODE", "0")))
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_launch(tmp_path: Path, claude_argv: list[str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_GPT_HOME"] = str(tmp_path / "claude-gpt-home")
    fake_proxy = _write_executable(tmp_path / "fake-claude-code-proxy", FAKE_PROXY_SOURCE)
    fake_claude = _write_executable(tmp_path / "fake-claude", FAKE_CLAUDE_SOURCE)
    argv_file = tmp_path / "claude-argv.json"
    env["CLAUDE_GPT_PROXY_BIN"] = str(fake_proxy)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    env["FAKE_CLAUDE_ARGV_FILE"] = str(argv_file)
    return subprocess.run(
        [str(LAUNCH_SH), "--", *claude_argv],
        cwd=str(SCRIPT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )


def test_isolation_and_auto_mode_enforcement_rejects_caller_permission_mode(tmp_path):
    """GIVEN caller が --permission-mode auto を渡す
    WHEN launch.sh を実行する
    THEN policy_weakening_flag_rejected として拒否される（bypassPermissions 以外の
    値でも一律拒否。Outcome 節「区別して全面拒否」）
    """
    result = _run_launch(tmp_path, ["--permission-mode", "auto"])
    assert result.returncode == 2, result.stderr
    assert "policy_weakening_flag_rejected" in result.stderr


def test_isolation_and_auto_mode_enforcement_rejects_permission_mode_equals_variant(tmp_path):
    result = _run_launch(tmp_path, ["--permission-mode=plan"])
    assert result.returncode == 2, result.stderr
    assert "policy_weakening_flag_rejected" in result.stderr


def test_isolation_and_auto_mode_enforcement_injects_exactly_one_permission_mode_auto(tmp_path):
    """GIVEN caller が --permission-mode を渡さない
    WHEN launch.sh を実行する
    THEN launcher 自身が exactly one の --permission-mode auto を最終 invocation に
    注入する
    """
    result = _run_launch(tmp_path, ["-p", "hello"])
    assert result.returncode == 0, result.stderr
    argv_file = tmp_path / "claude-argv.json"
    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv.count("--permission-mode") == 1
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "auto"


def test_isolation_and_auto_mode_enforcement_settings_has_classify_all_shell_and_defaults(tmp_path):
    """GIVEN 正常起動
    WHEN 生成された settings.local.json を読む
    THEN autoMode.classifyAllShell が true であり、既存 isolation guard（read
    deny・enabledPlugins 空）を回帰させない
    """
    result = _run_launch(tmp_path, ["-p", "hello"])
    assert result.returncode == 0, result.stderr
    settings_path = tmp_path / "claude-gpt-home" / "claude" / "settings.local.json"
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["autoMode"]["classifyAllShell"] is True
    assert payload["autoMode"]["environment"][0] == "$defaults"
    assert payload["autoMode"]["allow"][0] == "$defaults"
    assert payload["enabledPlugins"] == {}
    deny_rules = payload["permissions"]["deny"]
    assert any("proxy-config" in rule for rule in deny_rules)


def test_forbidden_extra_flags_list_includes_permission_mode():
    """GIVEN lib.sh の CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS
    WHEN 内容を読む
    THEN --permission-mode が含まれる
    """
    content = LIB_SH.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS="):
            assert "--permission-mode" in line
            return
    pytest.fail("CLAUDE_GPT_FORBIDDEN_EXTRA_FLAGS not found in lib.sh")


# --- AC4: live_agy_canary -----------------------------------------------------


def test_live_agy_canary_skips_when_receipt_unavailable(tmp_path):
    """GIVEN causal receipt ファイルが存在しない実行環境
    WHEN auto_mode_canary.py --mode agy を実行する
    THEN SKIP（exit 77）を返し、fallback を PASS に変換しない
    """
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "agy", "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == canary.EXIT_SKIP, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["exit_classification"] == "skip"


def test_live_agy_canary_fails_on_marker_only_insufficient_receipt(tmp_path):
    """GIVEN marker_only_insufficient: true の causal receipt
    WHEN auto_mode_canary.py --mode agy を実行する
    THEN FAIL（exit 1）を返す（marker のみの証拠は不十分。Issue #2183 契約）
    """
    receipt = {
        "agent_id": "agent-1",
        "tool_use_id": "tool-1",
        "builder_path": "x",
        "wrapper_path": "y",
        "provider": "agy",
        "profile": "default",
        "request_nonce": "n1",
        "fallback_used": False,
        "provider_skipped": False,
        "wrapper_exit_code": 0,
        "terminal_completion": True,
        "marker_only_insufficient": True,
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "agy", "--agy-receipt-path", str(receipt_path), "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == canary.EXIT_FAIL, result.stdout + result.stderr


def test_live_agy_canary_passes_on_valid_receipt(tmp_path):
    """GIVEN すべての required field を満たし fallback/skip/marker-only が false な
    causal receipt
    WHEN auto_mode_canary.py --mode agy を実行する
    THEN PASS（exit 0）を返す
    """
    receipt = {
        "agent_id": "agent-1",
        "tool_use_id": "tool-1",
        "builder_path": "x",
        "wrapper_path": "y",
        "provider": "agy",
        "profile": "default",
        "request_nonce": "n1",
        "fallback_used": False,
        "provider_skipped": False,
        "wrapper_exit_code": 0,
        "terminal_completion": True,
        "marker_only_insufficient": False,
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "agy", "--agy-receipt-path", str(receipt_path), "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == canary.EXIT_OK, result.stdout + result.stderr


# --- AC5: live_github_mutation_canary -----------------------------------------


def test_live_github_mutation_canary_skips_or_passes():
    """GIVEN 実行環境
    WHEN auto_mode_canary.py --mode github を実行する
    THEN gh CLI/認証が利用不能なら SKIP（exit 77）、利用可能なら実際に canary
    Issue を create/edit/comment/close して PASS（exit 0）を返す（FAIL は実装
    バグを意味するためテスト失敗として扱う）
    """
    if REAL_GH_BIN is None:
        pytest.skip("gh CLI not available in this environment")
    auth = subprocess.run([REAL_GH_BIN, "auth", "status"], capture_output=True, text=True, timeout=15)
    if auth.returncode != 0:
        pytest.skip("gh CLI not authenticated in this environment")

    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "github", "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode in (canary.EXIT_OK, canary.EXIT_SKIP), result.stdout + result.stderr
    payload = json.loads(result.stdout)
    if result.returncode == canary.EXIT_OK:
        github_result = payload["github_remote_object_identity"]
        assert github_result["final_state"] == "closed"
        assert github_result["operations"] == [
            "canary_issue_create",
            "canary_issue_edit",
            "canary_issue_comment",
            "canary_issue_close",
        ]
        assert github_result["negative_control"]["all_side_effect_free"] is True
        assert github_result["cleanup_status"]["orphan_issue"] is False


# --- AC6: runtime_exit_semantics ----------------------------------------------


def test_runtime_exit_semantics_invalid_mode_returns_exit_2():
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "not-a-real-mode", "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == canary.EXIT_INVALID_INVOCATION


def test_runtime_exit_semantics_missing_required_argument_returns_exit_2():
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == canary.EXIT_INVALID_INVOCATION


def test_runtime_exit_semantics_agy_skip_is_exit_77_not_promoted_to_pass():
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "agy", "--no-evidence"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == canary.EXIT_SKIP
    assert result.returncode != canary.EXIT_OK


def test_runtime_exit_semantics_constants_match_documented_contract():
    assert canary.EXIT_OK == 0
    assert canary.EXIT_FAIL == 1
    assert canary.EXIT_INVALID_INVOCATION == 2
    assert canary.EXIT_SKIP == 77


# --- AC7: sanitized_evidence ---------------------------------------------------


def test_sanitized_evidence_schema_has_required_top_level_keys(tmp_path):
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "agent_id": "a",
                "tool_use_id": "t",
                "builder_path": "b",
                "wrapper_path": "w",
                "provider": "agy",
                "profile": "default",
                "request_nonce": "n",
                "fallback_used": False,
                "provider_skipped": False,
                "wrapper_exit_code": 0,
                "terminal_completion": True,
                "marker_only_insufficient": False,
            }
        ),
        encoding="utf-8",
    )
    evidence_dir = tmp_path / "evidence"
    result = subprocess.run(
        [sys.executable, str(CANARY_PY), "--mode", "agy", "--agy-receipt-path", str(receipt_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(evidence_dir.parent) if evidence_dir.parent.exists() else None,
    )
    payload = json.loads(result.stdout)
    for key in (
        "schema",
        "generated_at",
        "sut_revision",
        "effective_policy",
        "agy_causal_receipt",
        "github_remote_object_identity",
        "negative_control",
        "cleanup_status",
        "exit_classification",
    ):
        assert key in payload
    assert payload["schema"] == "AUTO_MODE_CANARY_EVIDENCE_V1"


def test_sanitized_evidence_never_contains_raw_prompt_response_or_credential():
    """GIVEN 生成された evidence payload
    WHEN forbidden raw content key（prompt/response/transcript/tool_stdout/
    credential/token）を検索する
    THEN 一切含まれない（AC7: raw prompt/response/transcript/tool stdout/
    credential/token は保存・投稿しない）
    """
    payload = {
        "schema": "AUTO_MODE_CANARY_EVIDENCE_V1",
        "nested": {"agent_id_digest": "abc123", "provider": "agy"},
        "list": [{"receipt_digest": "def456"}],
    }
    canary._assert_no_raw_content(payload)  # should not raise

    forbidden_payload = {"schema": "X", "prompt": "leaked raw prompt text"}
    with pytest.raises(AssertionError):
        canary._assert_no_raw_content(forbidden_payload)


def test_sanitized_evidence_written_to_ignored_worktree_local_dir():
    gitignore_path = SCRIPT_DIR.parent.parent / ".gitignore"
    content = gitignore_path.read_text(encoding="utf-8")
    assert "scripts/claude-gpt/.evidence/" in content
    assert canary.EVIDENCE_DIR == SCRIPT_DIR / ".evidence"


# --- AC8: negative_controls -----------------------------------------------------


def test_negative_controls_reject_preexisting_and_foreign_issue_without_gh_calls(monkeypatch):
    """GIVEN broker がまだ何も create していない状態
    WHEN pre-existing Issue の close / 他 run 作成 Issue の close を試みる
    THEN gh を一切呼び出さず（_run_gh を呼んだら即エラーにする monkeypatch で保証）
    BrokerError で拒否される
    """

    def _explode(*_args, **_kwargs):
        raise AssertionError("gh must not be invoked for a non-session-owned issue")

    monkeypatch.setattr(canary, "_run_gh", _explode)
    broker = canary.GitHubMutationBroker()

    with pytest.raises(canary.BrokerError) as exc_info:
        broker.close_canary_issue(1)
    assert exc_info.value.reason == "no_session_created_issue"

    with pytest.raises(canary.BrokerError):
        broker.edit_canary_issue(42, "malicious body")


def test_negative_controls_broker_exposes_only_allowed_operations():
    """GIVEN GitHubMutationBroker
    WHEN 公開メソッド一覧を確認する
    THEN forbidden operation に対応するメソッド（force push・branch/tag/release
    削除・generic gh api・repository settings mutation 等）は一切存在しない
    """
    broker = canary.GitHubMutationBroker()
    forbidden_method_names = (
        "gh_api",
        "force_push",
        "delete_branch_tag_or_release",
        "mutate_repository_settings",
        "push_default_branch",
        "invoke_agy_directly",
        "bypass_wrapper",
        "override_permission_mode",
    )
    for name in forbidden_method_names:
        assert not hasattr(broker, name), f"broker must not expose {name}"


def test_negative_controls_all_cases_are_side_effect_free(monkeypatch):
    """GIVEN run_negative_controls
    WHEN 全 NEGATIVE_CONTROL_CASES を実行する
    THEN gh を一切呼び出さずに全件 rejected=True となる（cross-repo/force-push/
    branch-deletion 等の live GitHub 呼び出しを一切発生させない）
    """

    def _explode(*_args, **_kwargs):
        raise AssertionError("negative controls must not invoke gh")

    monkeypatch.setattr(canary, "_run_gh", _explode)
    broker = canary.GitHubMutationBroker()
    all_ok, attempts = canary.run_negative_controls(broker)
    assert all_ok is True
    assert len(attempts) == len(canary.NEGATIVE_CONTROL_CASES)
    for attempt in attempts:
        assert attempt["rejected"] is True


def test_negative_controls_trusted_repo_is_fixed_constant():
    assert canary.TRUSTED_REPO == "squne121/loop-protocol"
