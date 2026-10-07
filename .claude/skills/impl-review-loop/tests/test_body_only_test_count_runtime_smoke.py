"""Issue #2971 AC10/AC11: test-runner の ``test_count`` の delegation smoke。

既存の ``worktree-agent-runtime-smoke`` runner を subprocess で起動し、実 ``test-runner`` SubAgent へ
harmless な pytest 系 fixture command（ネットワーク・書込み不要）を 1 件委譲して、返却 report の該当行に
command 固有の ``test_count: {subject, passed}`` が現れ、``passed`` がその command の実際の件数と一致する
ことを確認する（件数は実行時に同じ command を実行して導出する。pytest 全体の件数とは別の値）。

``claude_live`` marker 付きなので既定 addopts では deselect され、``-m claude_live`` を明示した場合だけ
実行される。runner の exit 0 のみを PASS とし、exit 77（capability unavailable）は **skip ではなく fail**
として扱う（runtime AC の PASS を主張しない）。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
ADJUDICATOR_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
PROMPT_FILE = ".claude/skills/impl-review-loop/tests/fixtures/body_only_test_count_runtime_smoke_prompt.md"
OUTPUT_DIR = "artifacts/runtime-smoke/issue-2971-test-count"
EXIT_CAPABILITY_UNAVAILABLE = 77
FIXTURE_AC = "AC10"
FIXTURE_TARGET = ".claude/skills/impl-review-loop/tests/test_label_authority_invariants.py"
FIXTURE_COMMAND = f"uv run --locked pytest {FIXTURE_TARGET} -q"
FIXTURE_VC_BODY = f"## Verification Commands\n\n```bash\n# {FIXTURE_AC}\n$ {FIXTURE_COMMAND}\n```\n"

# 同名 module との sys.modules 衝突を避けるため、一意名で spec_from_file_location 経由の読み込みを行う。
_MODULE_NAME = "adjudicate_vc_result_test_count_runtime_smoke_issue_2971"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, ADJUDICATOR_PATH)
assert _spec is not None and _spec.loader is not None
adjudicator = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = adjudicator
_spec.loader.exec_module(adjudicator)


def count_markers(passed: int) -> list[str]:
    """report の値水準 marker。``test_count`` は command 固有の subject と実測件数に束縛する。"""
    return [
        "schema: TEST_VERDICT_MACHINE/v2",
        "result: PASS",
        f'test_count: {{subject: "{FIXTURE_TARGET}", passed: {passed}}}',
    ]


def runner_argv(markers: list[str]) -> list[str]:
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
        "600",
        "--max-turns",
        "30",
        "--expect-marker-source",
        "subagent",
        "--require-min-subagents",
        "1",
    ]
    for marker in markers:
        argv += ["--expect-marker", marker]
    return argv


def measured_passed_count() -> int:
    """fixture command と同じ pytest 対象を実行して実際の ``<N> passed`` を得る（解釈できなければ fail）。"""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", FIXTURE_TARGET, "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    summary = next((line for line in reversed(completed.stdout.splitlines()) if " passed" in line), "")
    match = re.search(r"(\d+) passed", summary)
    assert completed.returncode == 0 and match and "failed" not in summary and "error" not in summary, completed.stdout[
        -1000:
    ]
    return int(match.group(1))


# --- deterministic (non-live) checks ---------------------------------------------------------


def test_test_count_prompt_constants_match_the_shared_helper_and_the_target_exists() -> None:
    prompt = (ROOT / PROMPT_FILE).read_text(encoding="utf-8")
    rc, payload = adjudicator.extract_vc_metadata(FIXTURE_VC_BODY)

    assert rc == 0 and payload["status"] == "ok"
    [(ac, command, command_hash)] = [(c["ac"], c["raw_command"], c["command_hash"]) for c in payload["commands"]]
    assert (ac, command) == (FIXTURE_AC, FIXTURE_COMMAND)
    assert f"1. ac: {ac} | command: {command} | command_hash: {command_hash}" in prompt
    assert (ROOT / FIXTURE_TARGET).is_file()
    # test-runner の許可コマンド（VC に逐語で記載された repo-relative の pytest target）だけを使う。
    assert command.startswith("uv run --locked pytest ") and not re.search(r"[>|;&]|git |gh ", command)


def test_test_count_prompt_asks_for_the_flow_style_line_without_leaking_a_count() -> None:
    prompt = (ROOT / PROMPT_FILE).read_text(encoding="utf-8")

    assert 'subagent_type: "test-runner"' in prompt
    assert 'test_count: {subject: "<pytest target path>", passed: <N>}' in prompt
    assert not re.search(r"passed: \d+", prompt)  # 件数を prompt に書いて marker を満たさない


def test_test_count_markers_are_command_specific_and_runner_argv_uses_only_existing_options() -> None:
    markers = count_markers(10)
    argv = runner_argv(markers)
    help_text = subprocess.run(
        [sys.executable, str(RUNNER), "--help"], capture_output=True, text=True, check=False, timeout=60
    ).stdout

    assert markers[2] == f'test_count: {{subject: "{FIXTURE_TARGET}", passed: 10}}'
    assert count_markers(9)[2] != markers[2]
    for option in {token for token in argv if token.startswith("--")}:
        assert option in help_text, option
    assert argv.count("--expect-marker") == len(markers)


# --- live (claude_live) ----------------------------------------------------------------------


@pytest.mark.claude_live
def test_ac10_test_runner_delegation_returns_a_command_specific_test_count() -> None:
    assert (ROOT / PROMPT_FILE).is_file()
    assert RUNNER.is_file()
    passed = measured_passed_count()

    # runner は --output-dir の exclusive create を要求する。前回実行の同名 artifact（git-ignored）だけを消す。
    shutil.rmtree(ROOT / OUTPUT_DIR, ignore_errors=True)
    result = subprocess.run(
        runner_argv(count_markers(passed)),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    # exit 77（capability unavailable）は SKIP ではなく fail。runtime AC の PASS を主張しない。
    assert result.returncode != EXIT_CAPABILITY_UNAVAILABLE, (
        "worktree-agent-runtime-smoke reported capability unavailable (exit 77): AC10 is unverified, "
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
