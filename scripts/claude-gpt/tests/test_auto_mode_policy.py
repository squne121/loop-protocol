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
    assert payload["ok"] is True, payload
    assert payload["checks"]["environment_narrow_label_present"] is True
    assert payload["checks"]["allow_narrow_label_present"] is True
    assert payload["checks"]["hard_deny_defaults_and_additions_present"] is True
    assert payload["checks"]["soft_deny_unmodified"] is True
    assert payload["checks"]["classify_all_shell_enabled"] is True
    # 実機検証（Claude Code 2.1.233, 2026-08-16）: 現行 CLI の `auto-mode config`
    # JSON は classifyAllShell key 自体を公開しない。effective config に key が
    # 現れる場合はそれを正本にし、現れない場合は version gate + settings 文字列の
    # best-effort 二重検証へ fall back する（P0-3 fix-delta。lib.sh コメント参照）。
    assert payload["checks"]["classify_all_shell_verification_source"] in (
        "effective_config",
        "settings_literal_plus_version_gate_best_effort",
    )
    assert payload["digests"]["auto_mode_defaults_digest"] != "unknown"
    assert payload["digests"]["effective_config_digest"] != "unknown"
    assert payload["claude_version"]["ok"] is True


def test_auto_mode_readback_fail_closed_on_unsupported_claude_version(tmp_path):
    """GIVEN claude --version が min supported version 未満を報告する
    WHEN preflight.sh --auto-mode-check を実行する
    THEN classifyAllShell 等の他チェックに関わらず fail-closed（exit 8,
    claude_version_below_minimum_supported）で拒否する（P0-3: settings 文字列
    存在チェックだけでは検出できない version-gate 要件）
    """
    fake_claude_source = r"""#!/usr/bin/env python3
import json
import sys

argv = sys.argv[1:]
if argv and argv[0] == "--version":
    print("2.0.0 (Claude Code)")
    sys.exit(0)
if "auto-mode" in argv:
    idx = argv.index("auto-mode")
    sub = argv[idx + 1] if idx + 1 < len(argv) else ""
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
        "classifyAllShell": True,
    }
    print(json.dumps(baseline))
    sys.exit(0)
sys.exit(1)
"""
    fake_claude = tmp_path / "fake-claude-old-version"
    fake_claude.write_text(fake_claude_source, encoding="utf-8")
    fake_claude.chmod(0o755)

    claude_config_dir = tmp_path / "claude-gpt-home" / "claude"
    claude_config_dir.mkdir(parents=True)
    settings_path = claude_config_dir / "settings.local.json"
    fragment = _run_sh_function("claude_gpt_auto_mode_json_fragment").stdout.strip()
    settings_path.write_text("{\n  " + fragment + "\n}\n", encoding="utf-8")

    env = dict(os.environ)
    env["CLAUDE_GPT_CLAUDE_BIN"] = str(fake_claude)
    result = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 8, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "claude_version_below_minimum_supported" in payload["fail_closed_reasons"]
    assert payload["claude_version"]["ok"] is False


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

argv = sys.argv[1:]

# --- Issue #2203 P0-3 (PR #2214 fix-delta): 通常起動が launch.sh から
#     `preflight.sh --auto-mode-check`（`--version` / `auto-mode defaults` /
#     `auto-mode config`）を必ず呼ぶため、この fake claude もそれらの
#     readback subcommand に応答し PASS を返す。全 invocation は JSONL へ
#     追記する（preflight invocation と本 invocation を別々に assert できる）。
argv_log = os.environ.get("FAKE_CLAUDE_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")

if argv and argv[0] == "--version":
    print(os.environ.get("FAKE_CLAUDE_VERSION") or "2.1.211 (Claude Code)")
    sys.exit(0)
if "auto-mode" in argv:
    auto_mode_idx = argv.index("auto-mode")
    subcommand = argv[auto_mode_idx + 1] if auto_mode_idx + 1 < len(argv) else ""
    baseline = {
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
        "classifyAllShell": False,
    }
    if subcommand == "defaults":
        print(json.dumps(baseline))
        sys.exit(0)
    if subcommand == "config":
        config = dict(baseline)
        settings_path = None
        for i, tok in enumerate(argv):
            if tok == "--settings" and i + 1 < len(argv):
                settings_path = argv[i + 1]
        if settings_path and os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {})

            def _merge(key):
                entries = auto_mode.get(key)
                if entries is None:
                    return
                merged = []
                for entry in entries:
                    if entry == "$defaults":
                        merged.extend(baseline[key])
                    else:
                        merged.append(entry)
                config[key] = merged

            _merge("environment")
            _merge("allow")
            _merge("hard_deny")
            if auto_mode.get("classifyAllShell"):
                config["classifyAllShell"] = True
        print(json.dumps(config))
        sys.exit(0)

argv_file = os.environ.get("FAKE_CLAUDE_ARGV_FILE")
if argv_file:
    with open(argv_file, "w", encoding="utf-8") as fh:
        json.dump(argv, fh)
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


@pytest.mark.github_live
def test_live_github_mutation_canary_skips_or_passes():
    """GIVEN 実行環境
    WHEN auto_mode_canary.py --mode github を実行する
    THEN gh CLI/認証が利用不能なら SKIP（exit 77）、利用可能なら実際に canary
    Issue を create/edit/comment/close して PASS（exit 0）を返す（FAIL は実装
    バグを意味するためテスト失敗として扱う）

    P1-4（PR #2214 OWNER adversarial review 反映）: この test は authenticated
    `gh` があれば通常 pytest 実行時に production repository へ実際に Issue を
    作成する（開発者の個人 credential を使用し、CI/local で意味が変わり、
    repeated run で repository noise が増える）。`github_live` marker
    （既存 project-wide 規約, pyproject.toml `-m 'not github_live'` により既定で
    deselect）に加え、`AUTO_MODE_CANARY_LIVE_GITHUB_MUTATION=1` の明示 env gate
    が無い限り実行しない（marker 単独の deselect し忘れに対する二重の
    fail-closed opt-in）。通常 pytest は fake transport による hermetic
    state-machine test（本ファイルの他 test）のみに限定する。
    """
    if os.environ.get("AUTO_MODE_CANARY_LIVE_GITHUB_MUTATION") != "1":
        pytest.skip(
            "live GitHub mutation test requires explicit opt-in: "
            "AUTO_MODE_CANARY_LIVE_GITHUB_MUTATION=1 (Issue #2203 P1-4)"
        )
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


# --- P0-2: autoMode.hard_deny narrow additions ---------------------------------


def test_hard_deny_preserves_defaults_and_adds_narrow_push_denials():
    """GIVEN lib.sh の claude_gpt_auto_mode_standalone_json
    WHEN autoMode.hard_deny を確認する
    THEN 先頭が "$defaults" であり、default branch push / force push / remote
    ref deletion の narrow 追加分がそれぞれ含まれる（P0-2, PR #2214 OWNER
    adversarial review 反映。$defaults の hard_deny を置換・削除しない）
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    payload = json.loads(result.stdout)
    hard_deny = payload["autoMode"]["hard_deny"]
    assert hard_deny[0] == "$defaults"
    assert len(hard_deny) == 4
    joined = " ".join(hard_deny)
    assert "default branch" in joined or "main" in joined
    assert "force push" in joined
    assert "削除" in joined


# --- P1-1: negative control 違反は github mode を FAIL にする（PR #2214 fix-delta）


def test_github_mode_neg_ok_false_returns_exit_fail_and_attempts_cleanup(monkeypatch):
    """GIVEN negative control の一部が rejected=False（regression で broker に
    forbidden method が生えた等）を返す
    WHEN run_github_mutation_canary() を実行する
    THEN 例外が出なくても EXIT_FAIL を返し（P1-1: neg_ok==False を fail-closed に
    扱う）、cleanup（close）を試みる
    """
    broker_cls = canary.GitHubMutationBroker
    closed = {"value": False}

    def fake_create(self, title, body):  # noqa: ANN001
        self.state.created_issue_number = 42
        self.state.created_issue_node_id = "node-42"
        self.state.final_state = "open"
        self.state.operations.append("canary_issue_create")
        return 42

    def fake_edit(self, issue_number, new_body):  # noqa: ANN001
        self.state.operations.append("canary_issue_edit")

    def fake_comment(self, issue_number, comment_body):  # noqa: ANN001
        self.state.operations.append("canary_issue_comment")

    def fake_close(self, issue_number):  # noqa: ANN001
        self.state.final_state = "closed"
        self.state.operations.append("canary_issue_close")
        closed["value"] = True

    monkeypatch.setattr(broker_cls, "create_canary_issue", fake_create)
    monkeypatch.setattr(broker_cls, "edit_canary_issue", fake_edit)
    monkeypatch.setattr(broker_cls, "comment_canary_issue", fake_comment)
    monkeypatch.setattr(broker_cls, "close_canary_issue", fake_close)
    monkeypatch.setattr(
        canary,
        "run_negative_controls",
        lambda broker: (False, [{"case": "force_push", "rejected": False, "detail": "regression"}]),
    )
    monkeypatch.setattr(canary, "_find_gh_bin", lambda: "/usr/bin/gh")

    class _FakeAuth:
        returncode = 0

    monkeypatch.setattr(canary.subprocess, "run", lambda *a, **k: _FakeAuth())

    rc, detail = canary.run_github_mutation_canary()
    assert rc == canary.EXIT_FAIL
    assert detail["fail_reason"] == "negative_control_not_side_effect_free"
    assert detail["negative_control"]["all_side_effect_free"] is False
    assert closed["value"] is True
    assert detail["cleanup_status"]["orphan_issue"] is False


# --- P1-2: TimeoutExpired handling ----------------------------------------------


def test_run_gh_converts_timeout_expired_to_timed_out_result(monkeypatch):
    """GIVEN subprocess.run が TimeoutExpired を送出する
    WHEN _run_gh を呼ぶ
    THEN 例外を伝播させず、timed_out=True の GhCallResult に変換する（P1-2:
    従来は TimeoutExpired が未捕捉のまま create_canary_issue の recovery 分岐に
    到達できなかった）
    """

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["gh"], timeout=1)

    monkeypatch.setattr(canary, "_find_gh_bin", lambda: "/usr/bin/gh")
    monkeypatch.setattr(canary.subprocess, "run", _raise_timeout)

    result = canary._run_gh(["issue", "view", "1"])
    assert result.timed_out is True
    assert result.returncode is None
    assert result.ok is False


def test_recovery_after_timeout_requires_exact_cardinality_one(monkeypatch):
    """GIVEN create timeout 後、同一 title/run_nonce/creation window に一致する
    候補が2件見つかる（曖昧な状態）
    WHEN _recover_created_issue_after_timeout を呼ぶ
    THEN cardinality != 1 のため None（recovery を諦める。fail-closed）
    """
    broker = canary.GitHubMutationBroker()
    window_start = canary._now_iso()
    candidates = [
        {
            "number": 101,
            "title": "[claude-gpt-auto-mode-canary] run=abc",
            "body": f"run_nonce={broker.state.run_nonce}",
            "createdAt": window_start,
        },
        {
            "number": 102,
            "title": "[claude-gpt-auto-mode-canary] run=abc",
            "body": f"run_nonce={broker.state.run_nonce}",
            "createdAt": window_start,
        },
    ]

    def _fake_run_gh(args, **kwargs):  # noqa: ANN001
        return canary.GhCallResult(returncode=0, stdout=json.dumps(candidates), stderr="")

    monkeypatch.setattr(canary, "_run_gh", _fake_run_gh)
    recovered = broker._recover_created_issue_after_timeout(
        "[claude-gpt-auto-mode-canary] run=abc", window_start
    )
    assert recovered is None


def test_recovery_after_timeout_accepts_single_matching_candidate(monkeypatch):
    """GIVEN cardinality=1 の一致候補のみ
    WHEN _recover_created_issue_after_timeout を呼ぶ
    THEN その issue number を返す
    """
    broker = canary.GitHubMutationBroker()
    window_start = canary._now_iso()
    candidates = [
        {
            "number": 101,
            "title": "[claude-gpt-auto-mode-canary] run=abc",
            "body": f"run_nonce={broker.state.run_nonce}",
            "createdAt": window_start,
        }
    ]

    def _fake_run_gh(args, **kwargs):  # noqa: ANN001
        return canary.GhCallResult(returncode=0, stdout=json.dumps(candidates), stderr="")

    monkeypatch.setattr(canary, "_run_gh", _fake_run_gh)
    recovered = broker._recover_created_issue_after_timeout(
        "[claude-gpt-auto-mode-canary] run=abc", window_start
    )
    assert recovered == 101


# --- P1-3: evidence digest は placeholder ではなく実 readback 結果を転記する ----


def test_effective_policy_transcribes_real_digests_not_placeholder(tmp_path):
    """GIVEN preflight.sh --auto-mode-check の出力 JSON（digests を含む）
    WHEN _effective_policy に渡す
    THEN placeholder 文字列（"see_preflight_auto_mode_check_output"）ではなく
    実 digest がそのまま転記され、canary script / lib.sh / preflight.sh の
    SHA-256 が含まれる（P1-3）
    """
    check_json_path = tmp_path / "auto-mode-check.json"
    check_json_path.write_text(
        json.dumps(
            {
                "ok": True,
                "checks": {"classify_all_shell_enabled": True},
                "digests": {
                    "auto_mode_defaults_digest": "a" * 64,
                    "effective_config_digest": "b" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    settings_path = tmp_path / "settings.local.json"
    settings_path.write_text("{}", encoding="utf-8")

    policy = canary._effective_policy(check_json_path, settings_path)
    assert policy["auto_mode_defaults_digest"] == "a" * 64
    assert policy["effective_config_digest"] == "b" * 64
    assert policy["auto_mode_defaults_digest"] != "see_preflight_auto_mode_check_output"
    assert policy["auto_mode_readback_ok"] is True
    assert policy["canary_script_sha256"] != "unknown"
    assert policy["lib_sh_sha256"] != "unknown"
    assert policy["preflight_sh_sha256"] != "unknown"
    assert policy["settings_sha256"] != "unavailable_not_provided"


# --- P1-2: edit body 完全一致 / comment readback 検証 ---------------------------


def test_edit_canary_issue_requires_exact_body_match(monkeypatch):
    """GIVEN edit 後の readback body が要求した new_body と完全一致しない
    （GitHub 側の正規化・切り詰め・別 Issue への誤適用等）
    WHEN edit_canary_issue を呼ぶ
    THEN edit_body_mismatch で拒否する（run_nonce の部分一致だけでは検出できない）
    """
    broker = canary.GitHubMutationBroker()
    broker.state.created_issue_number = 5
    broker.state.created_issue_node_id = "node-5"
    broker.state.expected_previous_body_sha256 = canary._sha256_text("old body")
    nonce = broker.state.run_nonce
    requested_body = f"requested new body run_nonce={nonce}"
    view_calls = {"n": 0}

    def _fake_run_gh(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["issue", "edit"]:
            return canary.GhCallResult(returncode=0, stdout="", stderr="")
        if args[:2] == ["issue", "view"]:
            view_calls["n"] += 1
            body = "old body" if view_calls["n"] == 1 else f"DIFFERENT body than requested run_nonce={nonce}"
            return canary.GhCallResult(returncode=0, stdout=json.dumps({"id": "node-5", "body": body}), stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(canary, "_run_gh", _fake_run_gh)
    with pytest.raises(canary.BrokerError) as exc_info:
        broker.edit_canary_issue(5, requested_body)
    assert exc_info.value.reason == "edit_body_mismatch"


def test_comment_canary_issue_verifies_readback_body(monkeypatch):
    """GIVEN comment 作成後の readback comment body が投稿した comment_body と一致する
    WHEN comment_canary_issue を呼ぶ
    THEN 正常に完了する（P1-2: comment ID・body を readback で照合する）
    """
    broker = canary.GitHubMutationBroker()
    broker.state.created_issue_number = 7
    broker.state.created_issue_node_id = "node-7"
    broker.state.expected_previous_body_sha256 = canary._sha256_text("body-7")
    comment_url = "https://github.com/squne121/loop-protocol/issues/7#issuecomment-999"

    def _fake_run_gh(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["issue", "comment"]:
            return canary.GhCallResult(returncode=0, stdout=comment_url, stderr="")
        if args[:4] == ["issue", "view", "7", "--json"] and args[4] == "comments":
            return canary.GhCallResult(
                returncode=0,
                stdout=json.dumps({"comments": [{"url": comment_url, "body": "expected comment body"}]}),
                stderr="",
            )
        if args[:2] == ["issue", "view"]:
            return canary.GhCallResult(returncode=0, stdout=json.dumps({"id": "node-7", "body": "body-7"}), stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(canary, "_run_gh", _fake_run_gh)
    broker.comment_canary_issue(7, "expected comment body")
    assert broker.state.operations == ["canary_issue_comment"]


def test_comment_canary_issue_rejects_body_mismatch_in_readback(monkeypatch):
    broker = canary.GitHubMutationBroker()
    broker.state.created_issue_number = 7
    broker.state.created_issue_node_id = "node-7"
    broker.state.expected_previous_body_sha256 = canary._sha256_text("body-7")
    comment_url = "https://github.com/squne121/loop-protocol/issues/7#issuecomment-999"

    def _fake_run_gh(args, **_kwargs):  # noqa: ANN001
        if args[:2] == ["issue", "comment"]:
            return canary.GhCallResult(returncode=0, stdout=comment_url, stderr="")
        if args[:4] == ["issue", "view", "7", "--json"] and args[4] == "comments":
            return canary.GhCallResult(
                returncode=0,
                stdout=json.dumps({"comments": [{"url": comment_url, "body": "tampered body"}]}),
                stderr="",
            )
        if args[:2] == ["issue", "view"]:
            return canary.GhCallResult(returncode=0, stdout=json.dumps({"id": "node-7", "body": "body-7"}), stderr="")
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(canary, "_run_gh", _fake_run_gh)
    with pytest.raises(canary.BrokerError) as exc_info:
        broker.comment_canary_issue(7, "expected comment body")
    assert exc_info.value.reason == "comment_body_mismatch"


def test_effective_policy_marks_unavailable_when_not_provided():
    """GIVEN readback JSON が渡されない
    WHEN _effective_policy に None を渡す
    THEN placeholder ではなく明示的な "unavailable_not_provided" を返す（偽の
    計算済み値を捏造しない）
    """
    policy = canary._effective_policy(None, None)
    assert policy["auto_mode_defaults_digest"] == "unavailable_not_provided"
    assert policy["effective_config_digest"] == "unavailable_not_provided"


# --- P0-6: gh executable の trusted absolute path 固定 --------------------------


def test_find_gh_bin_respects_trusted_path_env_override(monkeypatch, tmp_path):
    """GIVEN AUTO_MODE_CANARY_TRUSTED_GH_PATH が実在ファイルを指す
    WHEN _find_gh_bin を呼ぶ
    THEN ambient PATH 上の gh ではなく、その pinned absolute path を使う
    （P0-6, PR #2214 OWNER adversarial review 反映）
    """
    pinned = tmp_path / "trusted-gh"
    pinned.write_text("#!/bin/sh\necho fake-gh\n", encoding="utf-8")
    pinned.chmod(0o755)
    canary._TRUSTED_GH_BIN_RESOLVED = False
    canary._TRUSTED_GH_BIN_CACHE = None
    monkeypatch.setenv("AUTO_MODE_CANARY_TRUSTED_GH_PATH", str(pinned))
    try:
        resolved = canary._find_gh_bin()
        assert resolved == str(pinned)
        # 同一プロセス内で再解決しない（cache が効く。ambient PATH mutation race
        # を避けるため）。
        monkeypatch.delenv("AUTO_MODE_CANARY_TRUSTED_GH_PATH", raising=False)
        assert canary._find_gh_bin() == str(pinned)
    finally:
        canary._TRUSTED_GH_BIN_RESOLVED = False
        canary._TRUSTED_GH_BIN_CACHE = None
