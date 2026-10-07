"""Issue #2971 AC8: body-only lane の skill-invocation runtime smoke（dry-run）。

既存の ``worktree-agent-runtime-smoke`` runner を subprocess で起動し、実 Skill 起動
（``/impl-review-loop body-only-lane-dry-run <fixture>``）が SKILL.md の dry-run 節に従って
dry-run CLI（production 関数の実出力から marker を導出）を実行し、その結果を ordered marker
として出力することを確認する。この smoke は dry-run であり実 PR を変更しない。

``claude_live`` marker 付きなので既定 addopts では deselect され、``-m claude_live`` を
明示した場合だけ実行される。runner の exit 0 のみを PASS とし、exit 77（capability
unavailable）は **skip ではなく fail** として扱う（runtime AC の PASS を主張しない）。

AC8 は #2981 の counterfactual control による「candidate PASS + BASE FAIL」でのみ PASS となる。runner に
counterfactual / BASE 対照の option が無い間（および option があっても BASE 対照の判定が未配線の間）は、
candidate だけで PASS を返す false-green を避けるため、``SKIP:`` + 理由を出して ``pytest.exit(returncode=77)``
とする（pytest 内部の skip / xfail は使わない。#2981 の flag 名は推測して配線しない）。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
PROMPT_FILE = ".claude/skills/impl-review-loop/tests/fixtures/body_only_lane_runtime_smoke_prompt.md"
OUTPUT_DIR = "artifacts/runtime-smoke/issue-2971-body-only-lane"
EXPECTED_ORDERED_MARKERS = (
    "BODY_ONLY_LANE_ELIGIBLE",
    "BODY_ONLY_LANE_STEP1_NOT_DISPATCHED",
    "BODY_ONLY_LANE_REPAIR_VIA_UPDATE_PR",
    "BODY_ONLY_LANE_STEP4_REUSE_STORED",
    "BODY_ONLY_LANE_STEP5_TERMINAL_GATE",
)
EXIT_CAPABILITY_UNAVAILABLE = 77
COUNTERFACTUAL_OPTION_PATTERN = re.compile(r"--[a-z0-9-]*counterfactual[a-z0-9-]*")
NO_COUNTERFACTUAL_REASON = (
    "SKIP: AC8 is unverified: the runtime smoke runner has no counterfactual / BASE control option "
    "(#2981 counterfactual control is not landed on main), so candidate PASS + BASE FAIL cannot be established."
)
BASE_NOT_WIRED_REASON = (
    "SKIP: AC8 is unverified: the runner exposes a counterfactual option but the BASE-run discrimination "
    "(candidate PASS + BASE FAIL) is not wired to the #2981 interface yet."
)


def runner_counterfactual_options(help_text: str) -> list[str]:
    """runner の ``--help`` 出力から counterfactual を示す option を探す（#2981 の flag 名は推測しない）。"""
    return sorted(set(COUNTERFACTUAL_OPTION_PATTERN.findall(help_text)))


def exit_77_unless_discrimination_is_available(help_text: str) -> None:
    """candidate PASS + BASE FAIL を確認できない限り PASS を返さない（pytest.exit 77。skip / xfail は使わない）。

    counterfactual option が無ければ即座に、あれば BASE 対照が未配線であることを理由に exit 77 とする。
    """
    reason = BASE_NOT_WIRED_REASON if runner_counterfactual_options(help_text) else NO_COUNTERFACTUAL_REASON
    pytest.exit(reason, returncode=EXIT_CAPABILITY_UNAVAILABLE)


def _runner_help() -> str:
    return subprocess.run(
        [sys.executable, str(RUNNER), "--help"], capture_output=True, text=True, check=False, timeout=60
    ).stdout


def _runner_argv() -> list[str]:
    argv = [
        sys.executable,
        str(RUNNER),
        "--runtime",
        "claude",
        "--mode",
        "structured",
        "--claude-adapter",
        "native",
        "--worktree",
        str(ROOT),
        "--prompt-file",
        PROMPT_FILE,
        "--output-dir",
        OUTPUT_DIR,
        "--timeout-seconds",
        "420",
        "--max-turns",
        "12",
        "--expect-marker-source",
        "main",
        "--expect-skill-command",
        "impl-review-loop",
    ]
    for marker in EXPECTED_ORDERED_MARKERS:
        argv += ["--expect-ordered-marker", marker]
    return argv


@pytest.mark.claude_live
def test_ac8_body_only_lane_dry_run_skill_invocation_emits_ordered_markers() -> None:
    assert (ROOT / PROMPT_FILE).is_file()
    assert RUNNER.is_file()
    help_text = _runner_help()
    if not runner_counterfactual_options(help_text):
        exit_77_unless_discrimination_is_available(help_text)

    # runner は --output-dir の exclusive create を要求する。前回実行の同名 artifact（git-ignored）だけを消す。
    shutil.rmtree(ROOT / OUTPUT_DIR, ignore_errors=True)

    result = subprocess.run(
        _runner_argv(),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    # exit 77（capability unavailable）は SKIP ではなく fail。runtime AC の PASS を主張しない。
    assert result.returncode != EXIT_CAPABILITY_UNAVAILABLE, (
        "worktree-agent-runtime-smoke reported capability unavailable (exit 77): AC8 is unverified, "
        "do not claim PASS and follow the Stop Condition.\n"
        f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
    )
    assert result.returncode == 0, (
        f"worktree-agent-runtime-smoke did not report success (exit={result.returncode}).\n"
        f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
    )
    summary = ROOT / OUTPUT_DIR / "summary.md"
    assert summary.is_file(), f"expected persisted evidence at {summary}"
    assert summary.read_text(encoding="utf-8").strip()
    # candidate は PASS したが、BASE run との discrimination（candidate PASS + BASE FAIL）は未配線のため PASS にしない。
    exit_77_unless_discrimination_is_available(help_text)


# --- deterministic (non-live) checks of the AC8 gate ---------------------------------------------


def test_ac8_gate_exits_77_with_skip_reason_when_the_runner_has_no_counterfactual_option() -> None:
    with pytest.raises(pytest.exit.Exception) as excinfo:
        exit_77_unless_discrimination_is_available(
            "usage: runner [-h] [--mode {structured}] [--require-clean-postcondition]"
        )

    assert excinfo.value.returncode == EXIT_CAPABILITY_UNAVAILABLE == 77
    assert str(excinfo.value).startswith("SKIP:")
    assert "#2981" in str(excinfo.value) and "counterfactual" in str(excinfo.value)


def test_ac8_gate_never_passes_even_when_a_counterfactual_option_exists_but_base_is_not_wired() -> None:
    with pytest.raises(pytest.exit.Exception) as excinfo:
        exit_77_unless_discrimination_is_available("usage: runner [-h] [--some-counterfactual-flag X]")

    assert excinfo.value.returncode == 77
    assert str(excinfo.value).startswith("SKIP:") and "BASE" in str(excinfo.value)


def test_ac8_counterfactual_option_detection_ignores_unrelated_baseline_options() -> None:
    assert (
        runner_counterfactual_options("[--require-session-baseline-preservation] [--require-clean-postcondition]") == []
    )
    assert runner_counterfactual_options("--counterfactual-base-ref REF --x") == ["--counterfactual-base-ref"]


def test_ac8_the_real_runner_currently_exposes_no_counterfactual_option_so_the_live_smoke_cannot_pass() -> None:
    """#2981 が land して runner に option が現れたら、この test が落ちて BASE 対照の配線が必要になる。"""
    assert runner_counterfactual_options(_runner_help()) == []
