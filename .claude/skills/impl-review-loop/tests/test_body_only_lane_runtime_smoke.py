"""Issue #2971 AC8: body-only lane の skill-invocation runtime smoke（dry-run）。

既存の ``worktree-agent-runtime-smoke`` runner を subprocess で起動し、実 Skill 起動
（``/impl-review-loop body-only-lane-dry-run <fixture>``）が SKILL.md の dry-run 節に従って
dry-run CLI（production 関数の実出力から marker を導出）を実行し、その結果を ordered marker
として出力することを確認する。この smoke は dry-run であり実 PR を変更しない。

``claude_live`` marker 付きなので既定 addopts では deselect され、``-m claude_live`` を
明示した場合だけ実行される。runner の exit 0 のみを PASS とし、exit 77（capability
unavailable）は **skip ではなく fail** として扱う（runtime AC の PASS を主張しない）。
"""

from __future__ import annotations

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
