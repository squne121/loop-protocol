"""scripts/claude-gpt/tests/test_spark_auth_deny_runtime_smoke.py

Issue #2440 AC4/AC5: actual Claude Code binary / permission parser を使う
hermetic runtime smoke。

`launch.sh --check-only` が test-owned isolated `CLAUDE_GPT_HOME` に実際に生成
した `settings.local.json` を、実 `claude` CLI に読み込ませて:

- AC4: legacy `Write(<spark-auth>/**) is not matched by file permission checks`
  相当の warning が発生しないこと
- AC5: canonical `Edit(<spark-auth>/**)` deny が spark-auth 配下への実 file
  edit を実 tool boundary（Edit tool 呼び出し）で拒否すること

を確認する。static fixture / string grep のみを PASS 根拠にしない
（`docs/dev/runtime-verification-policy.md` Runtime Verification
Applicability: `decision: immediate`, `applicable_acs: [AC4, AC5]`）。

実 `claude` binary が test environment で利用不能な場合は `pytest.skip` する
（SKIP。既存 `test_auto_mode_policy.py` の
`@pytest.mark.skipif(REAL_CLAUDE_BIN is None, ...)` および
`latitude_live_helpers.is_environment_available()` の live 系 SKIP semantics
と同じ扱い。SKIP を PASS に昇格しない）。

live credential / ChatGPT auth material / production spark-auth content は
使用しない。test-owned isolated `CLAUDE_GPT_HOME` / spark-auth directory /
canary file のみを使う（`_latitude_check_only_helper.run_check_only` を再利用
し、hermetic `launch.sh --check-only` 起動ロジックを重複実装しない -- DRY）。
実 `claude` CLI 自体のモデル呼び出しは ambient Claude Code アカウント認証を
使う（`test_auto_mode_policy.py` の
`test_generated_settings_defaults_readback_via_real_claude_cli` および
`latitude_live_helpers.run_claude_gpt_canary` と同じ既存パターン。ChatGPT
subscription / spark-auth credential とは無関係）。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

_HELPER_PATH = Path(__file__).resolve().parent / "_latitude_check_only_helper.py"
_spec = importlib.util.spec_from_file_location(
    "claude_gpt_latitude_check_only_helper_2440_spark_auth_runtime_smoke", _HELPER_PATH
)
_helper = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_helper)

run_check_only = _helper.run_check_only

REAL_CLAUDE_BIN = shutil.which("claude")

LEGACY_WARNING_SNIPPET = "is not matched by file permission checks"

pytestmark = pytest.mark.skipif(
    REAL_CLAUDE_BIN is None,
    reason="SKIP: claude CLI not available in test environment (AC4/AC5 runtime smoke prerequisite)",
)


def _run_claude_print(
    settings_path: Path,
    prompt: str,
    *,
    permission_mode: str = "default",
    timeout: float = 60.0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            REAL_CLAUDE_BIN,
            "--settings",
            str(settings_path),
            "--permission-mode",
            permission_mode,
            "-p",
            prompt,
            "--output-format",
            "text",
        ],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_no_legacy_write_deny_warning_with_generated_canonical_settings(tmp_path):
    """GIVEN launch.sh --check-only が test-owned isolated CLAUDE_GPT_HOME に
    生成した settings.local.json（spark-auth deny は canonical Edit(...) のみ）
    WHEN 実 claude CLI にこの settings を渡して -p で1ターン実行する
    THEN "... is not matched by file permission checks" 相当の legacy warning が
    stdout/stderr のいずれにも出力されない (AC4)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr

    proc = _run_claude_print(settings_path, "Reply with exactly one word: OK")
    combined = proc.stdout + proc.stderr
    assert LEGACY_WARNING_SNIPPET not in combined, combined


def test_synthetic_legacy_write_only_rule_reproduces_warning_negative_control(tmp_path):
    """GIVEN launch.sh の heredoc とは独立に手作りした legacy-only settings
    （spark-auth 向け deny が Write(path) のみで Edit(path) を含まない -- 本 Issue
    修正前の launch.sh が実際に生成していた形と等価）
    WHEN 実 claude CLI にこの settings を渡して -p で1ターン実行する
    THEN "... is not matched by file permission checks" 相当の warning が実際に
    出力される（negative control: 上のテストが偽陽性でないことを本テスト自身が
    証明する。warning 判定ロジック／fixture が壊れていれば本テストが失敗し検知
    できる）
    """
    spark_auth_dir = tmp_path / "legacy-spark-auth"
    spark_auth_dir.mkdir(parents=True)
    settings_path = tmp_path / "legacy-settings.json"
    settings_path.write_text(
        "{\n"
        '  "permissions": {\n'
        '    "deny": [\n'
        f'      "Write(/{spark_auth_dir}/**)"\n'
        "    ]\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    proc = _run_claude_print(settings_path, "Reply with exactly one word: OK")
    combined = proc.stdout + proc.stderr
    assert LEGACY_WARNING_SNIPPET in combined, combined


def test_canonical_edit_deny_blocks_spark_auth_file_edit_at_tool_boundary(tmp_path):
    """GIVEN launch.sh --check-only が生成した canonical Edit(...) deny を含む
    settings と、test-owned spark-auth dir 配下の canary file、対照用の control
    file
    WHEN --permission-mode acceptEdits（明示 deny 以外の Edit tool 呼び出しは
    自動承認される）で、両ファイルへの Edit を実 claude CLI に指示する
    THEN spark-auth 配下の canary file は実際には変更されない（canonical
    Edit(...) deny が実 tool boundary で拒否する）一方、control file は変更
    される（acceptEdits 自体は機能しており、本テストが false-negative でない
    ことの positive control）(AC5: config の文字列検査だけを PASS としない)
    """
    result, settings_path = run_check_only(tmp_path)
    assert settings_path.exists(), result.stderr

    # settings_path == <CLAUDE_GPT_HOME>/claude/settings.local.json
    # (`_latitude_check_only_helper.run_check_only` 参照)。
    # SPARK_AUTH_DIR_TARGET == <CLAUDE_GPT_HOME>/spark-auth と一致させる。
    claude_gpt_home = settings_path.parent.parent
    spark_auth_dir = claude_gpt_home / "spark-auth"
    assert spark_auth_dir.is_dir(), "launch.sh must have created SPARK_AUTH_DIR_TARGET"

    canary_path = spark_auth_dir / f"canary-{uuid.uuid4().hex}.txt"
    canary_path.write_text("ORIGINAL_CANARY\n", encoding="utf-8")

    control_path = tmp_path / f"control-{uuid.uuid4().hex}.txt"
    control_path.write_text("ORIGINAL_CONTROL\n", encoding="utf-8")

    prompt = (
        f"Use the Edit tool to replace the text ORIGINAL_CANARY with MODIFIED_CANARY "
        f"in the file {canary_path}. Then use the Edit tool to replace the text "
        f"ORIGINAL_CONTROL with MODIFIED_CONTROL in the file {control_path}. "
        "Attempt both edits even if one fails; do not ask for confirmation; "
        "report the outcome of each attempt."
    )
    proc = _run_claude_print(
        settings_path, prompt, permission_mode="acceptEdits", timeout=90.0, cwd=tmp_path
    )
    transcript = f"claude stdout:\n{proc.stdout}\nclaude stderr:\n{proc.stderr}"

    assert canary_path.read_text(encoding="utf-8") == "ORIGINAL_CANARY\n", (
        "spark-auth canary file must remain unmodified -- canonical Edit(...) deny "
        f"must block the edit at the real tool boundary.\n{transcript}"
    )
    assert control_path.read_text(encoding="utf-8") == "MODIFIED_CONTROL\n", (
        "control file must be modified by acceptEdits -- otherwise this is not a "
        f"valid positive control proving the Edit tool boundary was actually exercised.\n{transcript}"
    )
