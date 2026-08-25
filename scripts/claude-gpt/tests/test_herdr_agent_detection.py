"""scripts/claude-gpt/tests/test_herdr_agent_detection.py

Issue #2332 の focused regression。`scripts/claude-gpt/launch.sh` 冒頭の
guarded self-reexec（Herdr Agents session 認識 hint）ブロックを対象に、以下を
検証する:

- AC1: Phase 0 go/no-go gate ロジック自体の unit-level 検証（pytest fixture）。
  raw な live `herdr` VC line ではなく、gate 判定条件そのものを検証する。
- AC2: HERDR_ENV=1 かつ HERDR_AGENT が unset/空のとき、launcher は exactly once
  self-reexec し、実質的に HERDR_AGENT=claude の状態で動作を継続する。
- AC3: Herdr 外（HERDR_ENV unset/1 以外）、または呼び出し側が非空 HERDR_AGENT を
  設定した場合、launcher の挙動は変わらない（reexec なし、caller 値を exact に
  温存）。
- AC4: 既存の positional-argument parser（--check-only/--dry-run/--claude-bin/
  `--` separator、unexpected_positional_argument_before_double_dash）は本変更の
  前後で同一に動作する。

AC2/AC3 は launch.sh 冒頭の guard block を実ファイルから動的に抽出し、そのまま
POSIX sh で実行することで、ハンドコードされた再実装ではなく実ファイルの
コード自体を検証する（block が将来削除・改変された場合、marker 抽出自体が
失敗し fail-closed でテストが落ちる）。AC2/AC4 はさらに、実際の launch.sh
全体（`--dry-run` / 不正引数）を HERDR_ENV=1 で起動する統合テストも持ち、guard
block が実ファイル中で lib.sh source・diagnostics・parser より前に効いている
ことを確認する。`--dry-run` は proxy/claude 実行ファイルを一切必要としない
（ディレクトリ作成・proxy 起動を行わないため）。
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # scripts/claude-gpt
LAUNCH_SH = SCRIPT_DIR / "launch.sh"

GUARD_MARKER_START = "# --- Herdr Agents session hint: self-reexec (#2332) ---"
GUARD_MARKER_END_ANCHOR = "SELF_PATH=$0"


def _extract_guard_block() -> str:
    """launch.sh から self-reexec guard block を動的に抽出する（実ファイル基準）。

    marker / anchor が見つからない場合は fail-closed で AssertionError を送出
    する（guard block が削除・改名された regression を確実に検出するため）。
    """
    content = LAUNCH_SH.read_text(encoding="utf-8")
    start = content.find(GUARD_MARKER_START)
    assert start != -1, "guard block marker not found in launch.sh -- regression"
    end = content.find(GUARD_MARKER_END_ANCHOR, start)
    assert end != -1, "guard block end anchor (SELF_PATH=$0) not found after marker"
    block = content[start:end]
    assert "exec /bin/sh \"$0\" \"$@\"" in block, "self-reexec must preserve $0/$@"
    return block


def _write_probe_script(tmp_path: Path, guard_block: str) -> Path:
    """guard block + invocation counter + 事後観測マーカーを付加した実行可能
    スクリプトを作成する。"""
    counter_path = tmp_path / "invocation_count.txt"
    script_lines = [
        "#!/bin/sh",
        f'COUNTER_FILE="{counter_path}"',
        'printf "x" >> "$COUNTER_FILE"',
        guard_block,
        'echo "HERDR_AGENT_AFTER=${HERDR_AGENT:-<unset>}"',
        'echo "SELF_PATH_AFTER=$0"',
        "",
    ]
    probe = tmp_path / "probe.sh"
    probe.write_text("\n".join(script_lines), encoding="utf-8")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


def _base_env(**overrides: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    env.update(overrides)
    return env


def _run_probe(probe: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(probe)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _run_launch_sh(args: list, env: dict, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(LAUNCH_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --- AC1: Phase 0 go/no-go gate ロジック自体の unit-level 検証 -------------


def _phase0_gate(baseline_detected: bool, control_detected: bool) -> str:
    """Issue #2332 Background/Phase 0 Decision Gate の判定規則の pure 実装。

    GO は「baseline は未認識、かつ control が認識に改善した」場合のみ。
    改善がない・悪化した・双方とも認識/未認識の場合は NO_GO。
    """
    if (not baseline_detected) and control_detected:
        return "GO"
    return "NO_GO"


def test_phase0_gate_requires_confirmed_improvement():
    # GIVEN: baseline 未認識・control 認識という改善が観測された
    # WHEN: gate 判定を適用する
    # THEN: GO と判定される
    assert _phase0_gate(baseline_detected=False, control_detected=True) == "GO"


def test_phase0_gate_no_improvement_stays_no_go():
    # GIVEN: baseline/control ともに未認識（改善なし）
    # WHEN/THEN: NO_GO のまま
    assert _phase0_gate(baseline_detected=False, control_detected=False) == "NO_GO"


def test_phase0_gate_both_detected_is_no_go():
    # GIVEN: baseline/control ともに認識（control 由来の改善が実証されない）
    # WHEN/THEN: NO_GO
    assert _phase0_gate(baseline_detected=True, control_detected=True) == "NO_GO"


def test_phase0_gate_regression_stays_no_go():
    # GIVEN: baseline は認識、control で未認識（悪化）
    # WHEN/THEN: NO_GO
    assert _phase0_gate(baseline_detected=True, control_detected=False) == "NO_GO"


# --- AC2: HERDR_ENV=1 かつ HERDR_AGENT unset/空 -> exactly once injection ---


def test_herdr_env_empty_agent_injects_claude(tmp_path):
    # GIVEN: HERDR_ENV=1、HERDR_AGENT は unset
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env(HERDR_ENV="1")
    # WHEN: guard block（実ファイル抽出）を実行する
    result = _run_probe(probe, env)
    # THEN: self-reexec 後に HERDR_AGENT=claude として動作を継続する
    assert result.returncode == 0, result.stderr
    assert "HERDR_AGENT_AFTER=claude" in result.stdout
    # exactly once: 元プロセス + reexec 後プロセスの計 2 回だけ guard を通過する
    # （無限/複数回 reexec しない）
    assert (tmp_path / "invocation_count.txt").read_text() == "xx"


def test_herdr_env_explicit_empty_string_agent_injects_claude(tmp_path):
    # GIVEN: HERDR_ENV=1、HERDR_AGENT="" (unset ではなく明示的な空文字)
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env(HERDR_ENV="1", HERDR_AGENT="")
    result = _run_probe(probe, env)
    assert result.returncode == 0, result.stderr
    assert "HERDR_AGENT_AFTER=claude" in result.stdout
    assert (tmp_path / "invocation_count.txt").read_text() == "xx"


def test_self_path_preserved_across_reexec(tmp_path):
    # GIVEN: self-reexec がトリガーされる状態
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env(HERDR_ENV="1")
    result = _run_probe(probe, env)
    # THEN: "$0" は reexec 前後で不変
    assert result.returncode == 0, result.stderr
    assert f"SELF_PATH_AFTER={probe}" in result.stdout


def test_dry_run_succeeds_with_herdr_env_set_real_file():
    # GIVEN: 実ファイル全体（lib.sh source・diagnostics・parser を含む）を
    #        HERDR_ENV=1 で起動する（guard block が side effect より前に効くことの
    #        統合確認）
    env = _base_env(HERDR_ENV="1")
    result = _run_launch_sh(["--dry-run"], env)
    assert result.returncode == 0, result.stderr
    assert '"status":"dry_run"' in result.stdout


# --- AC3: Herdr 外、又は非空 HERDR_AGENT -> no-op ---------------------------


def test_non_herdr_or_nonempty_agent_unchanged_no_herdr_env(tmp_path):
    # GIVEN: HERDR_ENV が unset（Herdr 外）
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env()
    result = _run_probe(probe, env)
    # THEN: reexec せず HERDR_AGENT は unset のまま
    assert result.returncode == 0, result.stderr
    assert "HERDR_AGENT_AFTER=<unset>" in result.stdout
    assert (tmp_path / "invocation_count.txt").read_text() == "x"


def test_non_herdr_or_nonempty_agent_unchanged_herdr_env_not_one(tmp_path):
    # GIVEN: HERDR_ENV="0"（"1" 以外）
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env(HERDR_ENV="0")
    result = _run_probe(probe, env)
    assert result.returncode == 0, result.stderr
    assert "HERDR_AGENT_AFTER=<unset>" in result.stdout
    assert (tmp_path / "invocation_count.txt").read_text() == "x"


def test_non_herdr_or_nonempty_agent_unchanged_caller_value_preserved(tmp_path):
    # GIVEN: HERDR_ENV=1 だが呼び出し側が既に非空の HERDR_AGENT を設定済み
    guard_block = _extract_guard_block()
    probe = _write_probe_script(tmp_path, guard_block)
    env = _base_env(HERDR_ENV="1", HERDR_AGENT="custom-agent")
    result = _run_probe(probe, env)
    # THEN: reexec せず、caller の値を exact に温存する
    assert result.returncode == 0, result.stderr
    assert "HERDR_AGENT_AFTER=custom-agent" in result.stdout
    assert (tmp_path / "invocation_count.txt").read_text() == "x"


# --- AC4: 既存 positional-argument parser の regression coverage -----------


def test_positional_argument_negative_control_dry_run_succeeds():
    env = _base_env()
    result = _run_launch_sh(["--dry-run"], env)
    assert result.returncode == 0, result.stderr
    assert '"status":"dry_run"' in result.stdout


def test_positional_argument_negative_control_unexpected_positional_rejected():
    env = _base_env()
    result = _run_launch_sh(["unexpected-positional", "--", "-p", "hi"], env)
    assert result.returncode == 2
    assert "unexpected_positional_argument_before_double_dash" in result.stderr


def test_positional_argument_negative_control_unexpected_positional_rejected_with_reexec():
    # 同じ regression を HERDR_ENV=1（self-reexec が実際に発火する経路）でも確認する
    env = _base_env(HERDR_ENV="1")
    result = _run_launch_sh(["unexpected-positional", "--", "-p", "hi"], env)
    assert result.returncode == 2
    assert "unexpected_positional_argument_before_double_dash" in result.stderr


def test_positional_argument_negative_control_unknown_option_rejected():
    env = _base_env()
    result = _run_launch_sh(["--totally-bogus-flag"], env)
    assert result.returncode == 2
    assert "unknown_launcher_option" in result.stderr


def test_positional_argument_negative_control_claude_bin_missing_value_rejected():
    env = _base_env()
    result = _run_launch_sh(["--claude-bin"], env)
    assert result.returncode == 2
    assert "missing_value" in result.stderr


def test_positional_argument_negative_control_claude_bin_with_dry_run_accepted(tmp_path):
    fake_bin = tmp_path / "fake-claude"
    fake_bin.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)
    env = _base_env()
    result = _run_launch_sh(["--claude-bin", str(fake_bin), "--dry-run"], env)
    assert result.returncode == 0, result.stderr
    assert '"status":"dry_run"' in result.stdout


def test_positional_argument_negative_control_double_dash_allows_trailing_args():
    env = _base_env()
    result = _run_launch_sh(["--dry-run", "--", "-p", "some prompt"], env)
    assert result.returncode == 0, result.stderr
    assert '"status":"dry_run"' in result.stdout
