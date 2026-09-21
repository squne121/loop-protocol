"""scripts/claude-gpt/tests/test_classify_all_shell_native_projection.py

Issue #2709 の focused regression matrix。

Claude-GPT launcher は従来 `classifyAllShell: true` を launcher-generated
`autoMode` へ無条件注入していた。native Claude Code の既定は narrow な
Bash/PowerShell allow が classifier より先に解決される key-omission 相当で
あり、`classifyAllShell: true` は launcher 固有の classifier coverage 拡張
だった（native full parity の主張ではない）。本ファイルはこの無条件注入の
除去（AC1）、readback/preflight/canary の tri-state・availability evidence
表現への更新（AC2）、既存の安全境界（hard_deny 追加分等）の保持（AC3）、
isolated launcher runtime での real CLI readback または SKIP（AC4）、
`direct_readback_available: false` 自体を merge blocker にしないが
矛盾する値は fail-closed にする境界（AC5）、および version floor の
non-blocking capability 化（AC6）を検証する。

Runtime Verification Applicability: immediate（applicable_acs: AC2, AC4）。
real Claude CLI が利用不能な場合、real-CLI 依存テストは pytest skip
（`--auto-mode-check` の exit 77 SKIP 契約と同様に PASS への昇格を行わない）
とする。fallback 実行や擬似成功判定は行わない。
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
PREFLIGHT_SH = SCRIPT_DIR / "preflight.sh"
CANARY_PY = SCRIPT_DIR / "auto_mode_canary.py"

REAL_CLAUDE_BIN = shutil.which("claude")

_EXPECTED_TRI_STATE_KEYS = frozenset(
    {"generated_key_present", "direct_readback_available", "effective_value", "native_parity_claimed"}
)


def _load_canary_module():
    # 一意な module 名で sys.modules に登録する（test_auto_mode_policy.py が
    # 同一 pytest セッション内で別の module 名 "auto_mode_canary" を使って
    # 同じファイルを独立ロードするため、名前衝突を避ける）。
    spec = importlib.util.spec_from_file_location("auto_mode_canary_native_projection", CANARY_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


canary = _load_canary_module()


def _run_sh_function(function_name: str, *args: str) -> subprocess.CompletedProcess[str]:
    """lib.sh を source し、指定した関数を呼び出して stdout を返す（POSIX sh subshell）。"""
    quoted_args = " ".join(f'"{arg}"' for arg in args)
    script = f'. "{LIB_SH}"; {function_name} {quoted_args}'
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20)


def _write_fake_claude(path: Path, *, version: str, expose_classify_all_shell: bool | None) -> Path:
    """auto-mode defaults/config readback に応答する fake claude binary を書き出す。

    `expose_classify_all_shell` が None の場合、effective config は現行 vendor CLI
    実機検証（Claude Code 2.1.233, 2026-08-16）通り classifyAllShell key を一切
    公開しない。True/False の場合は effective config にその値の key を明示的に
    含める（direct readback availability のテスト用）。
    """
    if expose_classify_all_shell is None:
        classify_all_shell_line = ""
    else:
        classify_all_shell_line = f'        config["classifyAllShell"] = {expose_classify_all_shell!r}\n'
    source = f"""#!/usr/bin/env python3
import json
import sys

argv = sys.argv[1:]
if argv and argv[0] == "--version":
    print("{version} (Claude Code)")
    sys.exit(0)
if "auto-mode" in argv:
    idx = argv.index("auto-mode")
    sub = argv[idx + 1] if idx + 1 < len(argv) else ""
    baseline = {{
        "environment": ["defaults-env-baseline"],
        "allow": ["defaults-allow-baseline"],
        "hard_deny": ["defaults-hard-deny-baseline"],
        "soft_deny": ["defaults-soft-deny-baseline"],
    }}
    if sub == "defaults":
        print(json.dumps(baseline))
        sys.exit(0)
    if sub == "config":
        config = dict(baseline)
        settings_path = None
        for i, tok in enumerate(argv):
            if tok == "--settings" and i + 1 < len(argv):
                settings_path = argv[i + 1]
        if settings_path:
            with open(settings_path, encoding="utf-8") as fh:
                settings = json.load(fh)
            auto_mode = settings.get("autoMode", {{}})

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
{classify_all_shell_line}        print(json.dumps(config))
        sys.exit(0)
sys.exit(1)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_generated_settings(tmp_path: Path) -> Path:
    claude_config_dir = tmp_path / "claude-gpt-home" / "claude"
    claude_config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_config_dir / "settings.local.json"
    fragment = _run_sh_function("claude_gpt_auto_mode_json_fragment").stdout.strip()
    settings_path.write_text("{\n  " + fragment + "\n}\n", encoding="utf-8")
    return settings_path


# --- AC1: generated autoMode omits classifyAllShell ----------------------------


def test_generated_settings_omits_classify_all_shell():
    """GIVEN lib.sh の claude_gpt_auto_mode_json_fragment / standalone_json
    WHEN 生成された autoMode を確認する
    THEN classifyAllShell キーは一切含まれず（native default 相当の省略）、
    autoMode 自体も environment/allow/hard_deny 以外の無関係な user/project
    settings キーを取り込まない
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert set(payload.keys()) == {"autoMode"}
    auto_mode = payload["autoMode"]
    assert "classifyAllShell" not in auto_mode
    assert set(auto_mode.keys()) == {"environment", "allow", "hard_deny"}

    raw_fragment = _run_sh_function("claude_gpt_auto_mode_json_fragment").stdout
    assert '"classifyAllShell"' not in raw_fragment


# --- AC2: preflight/canary tri-state / availability evidence -------------------


def test_effective_policy_reports_tri_state_evidence(tmp_path):
    """GIVEN preflight.sh --auto-mode-check の出力 JSON（新 classify_all_shell
    tri-state evidence を含む）
    WHEN _effective_policy() に渡す、または check json 未提供のまま呼ぶ
    THEN 4 フィールド（generated_key_present / direct_readback_available /
    effective_value / native_parity_claimed）を持つ evidence を返し、未読出の
    boolean を enabled・native parity・denial-rate 改善として報告しない
    """
    # 1. check json 未提供時の安全な既定値（未評価・未確認。true を推定しない）。
    default_policy = canary._effective_policy(None, None)
    assert set(default_policy["classify_all_shell"].keys()) == _EXPECTED_TRI_STATE_KEYS
    assert default_policy["classify_all_shell"] == {
        "generated_key_present": False,
        "direct_readback_available": False,
        "effective_value": None,
        "native_parity_claimed": False,
    }

    # 2. lib.sh の実 readback 出力形状をそのまま転記する（P1-3 の転記方針を踏襲）。
    check_json_path = tmp_path / "auto-mode-check.json"
    check_json_path.write_text(
        json.dumps(
            {
                "ok": True,
                "classify_all_shell": {
                    "generated_key_present": False,
                    "direct_readback_available": True,
                    "effective_value": False,
                    "native_parity_claimed": False,
                },
                "digests": {
                    "auto_mode_defaults_digest": "c" * 64,
                    "effective_config_digest": "d" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    policy = canary._effective_policy(check_json_path, None)
    assert set(policy["classify_all_shell"].keys()) == _EXPECTED_TRI_STATE_KEYS
    assert policy["classify_all_shell"]["generated_key_present"] is False
    assert policy["classify_all_shell"]["direct_readback_available"] is True
    assert policy["classify_all_shell"]["effective_value"] is False
    assert policy["classify_all_shell"]["native_parity_claimed"] is False

    # 3. lib.sh の readback 自体も同じ 4-field tri-state shape を返す（key 未公開）。
    fake_claude = _write_fake_claude(
        tmp_path / "fake-claude-tri-state", version="2.1.233", expose_classify_all_shell=None
    )
    settings_path = _write_generated_settings(tmp_path)
    result = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude)},
    )
    payload = json.loads(result.stdout)
    assert set(payload["classify_all_shell"].keys()) == _EXPECTED_TRI_STATE_KEYS
    assert payload["classify_all_shell"]["generated_key_present"] is False
    assert payload["classify_all_shell"]["direct_readback_available"] is False
    assert payload["classify_all_shell"]["effective_value"] is None
    assert payload["classify_all_shell"]["native_parity_claimed"] is False


# --- AC3: hard_deny projection is preserved despite key omission ---------------


def test_native_aligned_projection_preserves_hard_deny(tmp_path):
    """GIVEN classifyAllShell を省略した generated autoMode
    WHEN readback で effective config を確認する
    THEN $defaults 由来の hard_deny を保持したまま default branch push / force
    push / remote ref deletion の narrow 追加分が反映され、environment/allow の
    narrow label・soft_deny 不変も回帰しない（既存安全境界は classifyAllShell
    の有無に依存しない）
    """
    result = _run_sh_function("claude_gpt_auto_mode_standalone_json")
    assert result.returncode == 0, result.stderr
    hard_deny = json.loads(result.stdout)["autoMode"]["hard_deny"]
    assert hard_deny[0] == "$defaults"
    assert len(hard_deny) == 4
    joined = " ".join(hard_deny)
    assert "force push" in joined
    assert "削除" in joined

    fake_claude = _write_fake_claude(
        tmp_path / "fake-claude-hard-deny", version="2.1.233", expose_classify_all_shell=None
    )
    settings_path = _write_generated_settings(tmp_path)
    check = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude)},
    )
    assert check.returncode == 0, check.stdout + check.stderr
    payload = json.loads(check.stdout)
    assert payload["ok"] is True
    assert payload["checks"]["environment_narrow_label_present"] is True
    assert payload["checks"]["allow_narrow_label_present"] is True
    assert payload["checks"]["hard_deny_defaults_and_additions_present"] is True
    assert payload["checks"]["soft_deny_unmodified"] is True


# --- AC4: real CLI focused runtime verifier, or SKIP (no fallback promotion) ---


@pytest.mark.skipif(REAL_CLAUDE_BIN is None, reason="claude CLI not available")
def test_real_cli_projection_readback_or_skip(tmp_path):
    """GIVEN 実 claude CLI と isolated launcher runtime
    WHEN classifyAllShell を省略した generated settings で
    `preflight.sh --auto-mode-check` を実行する
    THEN generated policy（key omission・4 rule lists の readback・hard-deny
    追加分）と readback availability を sanitized evidence として出力して
    PASS する。GitHub mutation や classifier request を発生させない
    （real claude CLI 未利用時は pytest skip とし PASS へ昇格しない）
    """
    settings_path = _write_generated_settings(tmp_path)
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

    classify_all_shell = payload["classify_all_shell"]
    assert set(classify_all_shell.keys()) == _EXPECTED_TRI_STATE_KEYS
    assert classify_all_shell["generated_key_present"] is False
    assert classify_all_shell["native_parity_claimed"] is False
    assert isinstance(classify_all_shell["direct_readback_available"], bool)
    if classify_all_shell["direct_readback_available"]:
        assert isinstance(classify_all_shell["effective_value"], bool)
    else:
        assert classify_all_shell["effective_value"] is None

    # sanitized evidence のみ: raw settings/prompt/transcript/token/credential/
    # HOME 絶対パスを含めない（digest 化された値と narrow label のみ）。
    for forbidden_key in ("prompt", "response", "transcript", "token", "credential"):
        assert forbidden_key not in payload


# --- AC5: direct_readback_available:false is not a merge blocker,   -----------
#     but a contradicting readback value is fail-closed ------------------------


def test_unreadable_native_boolean_does_not_claim_parity(tmp_path):
    """GIVEN native CLI が classifyAllShell の direct boolean readback を
    公開しない（現行 vendor CLI の既知の limitation）
    WHEN preflight.sh --auto-mode-check を実行する
    THEN `direct_readback_available: false` 自体は merge blocker にならず
    （ok は他の安全境界チェックのみで決まる）、native parity も主張しない。
    一方、direct readback が利用可能になり generated key 省略と矛盾する値
    （projection が想定しない True）を返した場合は fail-closed で拒否する
    （AC5 block 条件 (b)）
    """
    settings_path = _write_generated_settings(tmp_path)

    # 1. unreadable（key 自体が公開されない）: merge blocker にならない。
    fake_claude_unreadable = _write_fake_claude(
        tmp_path / "fake-claude-unreadable", version="2.1.233", expose_classify_all_shell=None
    )
    unreadable = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude_unreadable)},
    )
    assert unreadable.returncode == 0, unreadable.stdout + unreadable.stderr
    unreadable_payload = json.loads(unreadable.stdout)
    assert unreadable_payload["ok"] is True
    assert unreadable_payload["classify_all_shell"]["direct_readback_available"] is False
    assert unreadable_payload["classify_all_shell"]["native_parity_claimed"] is False
    assert "classify_all_shell_effective_value_contradicts_omitted_key" not in (
        unreadable_payload["fail_closed_reasons"]
    )

    # 2. contradicting readback（generated key を省略したのに native が True を
    #    報告する）: fail-closed で拒否する。
    fake_claude_contradicts = _write_fake_claude(
        tmp_path / "fake-claude-contradicts", version="2.1.233", expose_classify_all_shell=True
    )
    contradicts = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude_contradicts)},
    )
    assert contradicts.returncode == 8, contradicts.stdout + contradicts.stderr
    contradicts_payload = json.loads(contradicts.stdout)
    assert contradicts_payload["ok"] is False
    assert contradicts_payload["classify_all_shell"]["direct_readback_available"] is True
    assert contradicts_payload["classify_all_shell"]["effective_value"] is True
    assert contradicts_payload["classify_all_shell"]["native_parity_claimed"] is False
    assert "classify_all_shell_effective_value_contradicts_omitted_key" in (
        contradicts_payload["fail_closed_reasons"]
    )

    # 3. direct readback が false を報告する場合（矛盾なし）は blocker にならない。
    fake_claude_false = _write_fake_claude(
        tmp_path / "fake-claude-false", version="2.1.233", expose_classify_all_shell=False
    )
    false_result = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude_false)},
    )
    assert false_result.returncode == 0, false_result.stdout + false_result.stderr
    false_payload = json.loads(false_result.stdout)
    assert false_payload["ok"] is True
    assert false_payload["classify_all_shell"]["direct_readback_available"] is True
    assert false_payload["classify_all_shell"]["effective_value"] is False


# --- AC6: version floor is non-blocking capability info -------------------------


def test_min_supported_version_gate_is_non_blocking(tmp_path):
    """GIVEN claude --version が CLAUDE_GPT_MIN_SUPPORTED_CLAUDE_VERSION 未満
    WHEN 他の安全境界（narrow label・hard_deny・soft_deny）が全て満たされている
    状態で preflight.sh --auto-mode-check を実行する
    THEN version floor は launcher 起動そのものを拒否しない（ok は true のまま、
    fail_closed_reasons に version 関連の理由は含まれない）。claude_version.ok
    のみが capability 情報として false を示す
    """
    below_min_version = "2.0.0"

    fake_claude = _write_fake_claude(
        tmp_path / "fake-claude-old-but-compliant", version=below_min_version, expose_classify_all_shell=None
    )
    settings_path = _write_generated_settings(tmp_path)
    result = subprocess.run(
        [str(PREFLIGHT_SH), "--auto-mode-check", str(settings_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "CLAUDE_GPT_CLAUDE_BIN": str(fake_claude)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"]["environment_narrow_label_present"] is True
    assert payload["checks"]["allow_narrow_label_present"] is True
    assert payload["checks"]["hard_deny_defaults_and_additions_present"] is True
    assert payload["checks"]["soft_deny_unmodified"] is True
    assert not any(reason.startswith("claude_version_") for reason in payload["fail_closed_reasons"])
    assert payload["claude_version"]["ok"] is False
    assert payload["claude_version"]["raw"].startswith(below_min_version)
