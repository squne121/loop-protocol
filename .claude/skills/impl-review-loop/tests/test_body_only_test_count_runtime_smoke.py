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
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
ADJUDICATOR_PATH = SKILL_DIR / "scripts" / "adjudicate_vc_result.py"
PROMPT_FILE = ".claude/skills/impl-review-loop/tests/fixtures/body_only_test_count_runtime_smoke_prompt.md"
OUTPUT_PARENT = "artifacts/runtime-smoke"
OUTPUT_DIR_PREFIX = "issue-2971-test-count"
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


def unique_output_dir() -> str:
    """呼出しごとに固有の ``--output-dir`` 相対 path（UTC timestamp + UUID）を返す。

    runner は ``--output-dir`` の exclusive create を要求するため、directory はここでは作らない
    （``mkdir`` / ``tempfile.mkdtemp`` を使わない）。過去 run の evidence を削除・再利用しない。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{OUTPUT_PARENT}/{OUTPUT_DIR_PREFIX}-{stamp}-{uuid.uuid4().hex}"


def runner_argv(markers: list[str], output_dir: str) -> list[str]:
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
        output_dir,
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


@dataclass(frozen=True)
class SmokeRun:
    """``run_test_count_smoke`` の結果。``summary_text`` は summary が無ければ ``None``。"""

    result: subprocess.CompletedProcess[str]
    output_dir: str
    summary: Path
    summary_text: str | None


def run_test_count_smoke(
    markers: list[str],
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    root: Path = ROOT,
) -> SmokeRun:
    """gh / claude を伴わない部分: 固有 output dir を一度だけ生成し、runner 起動と summary 読み出しに共通使用する。

    live test と hermetic test が同じ実行単位を呼ぶ。``run`` は runner 起動点（``subprocess.run``）の差し替え口。
    """
    output_dir = unique_output_dir()
    result = run(
        runner_argv(markers, output_dir),
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    summary = root / output_dir / "summary.md"
    summary_text = summary.read_text(encoding="utf-8") if summary.is_file() else None
    return SmokeRun(result=result, output_dir=output_dir, summary=summary, summary_text=summary_text)


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
    argv = runner_argv(markers, unique_output_dir())
    help_text = subprocess.run(
        [sys.executable, str(RUNNER), "--help"], capture_output=True, text=True, check=False, timeout=60
    ).stdout

    assert markers[2] == f'test_count: {{subject: "{FIXTURE_TARGET}", passed: 10}}'
    assert count_markers(9)[2] != markers[2]
    for option in {token for token in argv if token.startswith("--")}:
        assert option in help_text, option
    assert argv.count("--expect-marker") == len(markers)


def _output_dir_of(argv: list[str]) -> str:
    return argv[argv.index("--output-dir") + 1]


class _FakeRunner:
    """runner 起動点の fake。runner の exclusive create を模擬し、output dir が既存なら失敗させる。"""

    def __init__(self) -> None:
        self.output_dirs: list[str] = []
        self.existed_at_launch: list[bool] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_dir = _output_dir_of(argv)
        target = Path(kwargs["cwd"]) / output_dir
        self.output_dirs.append(output_dir)
        self.existed_at_launch.append(target.exists())
        target.mkdir(parents=True)  # 既存なら FileExistsError（exclusive create の模擬）
        (target / "summary.md").write_text(f"summary for {output_dir}\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="OK\n", stderr="")


def test_unique_output_dir_test_count_is_fresh_and_not_created() -> None:
    paths = [unique_output_dir() for _ in range(5)]

    assert len(set(paths)) == len(paths)
    for path in paths:
        assert path.startswith("artifacts/runtime-smoke/"), path
        assert path.split("/")[-1].startswith("issue-2971-test-count-"), path
        assert not (ROOT / path).exists(), path  # 関数は directory を作らない


def test_unique_output_dir_test_count_invocations_share_path_between_argv_and_summary(tmp_path: Path) -> None:
    fake = _FakeRunner()

    first = run_test_count_smoke(count_markers(3), run=fake, root=tmp_path)
    second = run_test_count_smoke(count_markers(3), run=fake, root=tmp_path)

    assert first.output_dir != second.output_dir
    assert fake.output_dirs == [first.output_dir, second.output_dir]
    assert fake.existed_at_launch == [False, False]  # runner 起動時点で未存在
    for run_result in (first, second):
        assert run_result.summary == tmp_path / run_result.output_dir / "summary.md"
        assert run_result.summary_text == f"summary for {run_result.output_dir}\n"
        assert run_result.result.returncode == 0


def test_unique_output_dir_test_count_preserves_existing_evidence(tmp_path: Path) -> None:
    legacy = tmp_path / "artifacts" / "runtime-smoke" / "issue-2971-test-count"
    past_run = tmp_path / "artifacts" / "runtime-smoke" / f"{OUTPUT_DIR_PREFIX}-20200101T000000Z-{'0' * 32}"
    sentinels = {legacy / "sentinel.txt": "legacy evidence", past_run / "sentinel.txt": "past run evidence"}
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")

    run_test_count_smoke(count_markers(3), run=_FakeRunner(), root=tmp_path)

    for path, content in sentinels.items():
        assert path.read_text(encoding="utf-8") == content, path


# --- live (claude_live) ----------------------------------------------------------------------


@pytest.mark.claude_live
def test_ac10_test_runner_delegation_returns_a_command_specific_test_count() -> None:
    assert (ROOT / PROMPT_FILE).is_file()
    assert RUNNER.is_file()
    passed = measured_passed_count()

    # output dir は run 固有（runner 起動前に未存在）。過去 run の evidence は削除・再利用しない。
    smoke = run_test_count_smoke(count_markers(passed))
    result = smoke.result

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
    assert smoke.summary_text is not None, f"expected persisted evidence at {smoke.summary}"
    assert smoke.summary_text.strip()
