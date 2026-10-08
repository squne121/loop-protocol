"""Issue #2986 (#2971 AC8 の引き継ぎ): body-only lane の runtime smoke を counterfactual control に接続する。

既存の ``worktree-agent-runtime-smoke`` runner の opt-in 対照モード（#2981）を subprocess で起動し、
実 Skill 起動（``/impl-review-loop body-only-lane-dry-run <fixture>``）について、候補の
``impl-review-loop/SKILL.md`` と BASE（#2971 の merge 前の親 commit の同 file）を、同一 prompt・同一
``--expect-skill-command``・同一 ordered marker で実行する。候補が PASS し BASE が FAIL する
（runner の top-level verdict が ``discriminative``）場合だけ PASS とする。

``claude_live`` marker 付きなので既定 addopts では deselect され、``-m claude_live`` を明示した場合だけ
実行される。runner の top-level verdict / exit code が採否の authority であり、この test は独立の
意味分類器を持たない。``--evidence-json`` の読み戻しは identity と整合性の確認だけに使う。

この smoke が与えるのは「指定した prompt・runtime・sample における SKILL.md の変更に対する discrimination
evidence」であり、SKILL.md の手順文が挙動を決定したことの統計的・絶対的な因果証明ではない
（各 arm は LLM の 1 sample である）。
"""

from __future__ import annotations

import ast
import copy
import json
import re
import secrets
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[4]
THIS_FILE = Path(__file__).resolve()
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
DRY_RUN_CLI = ROOT / ".claude" / "skills" / "impl-review-loop" / "scripts" / "body_only_repair_plan.py"
DRY_RUN_FIXTURE = ".claude/skills/impl-review-loop/tests/fixtures/body_only_lane_incident_2963.json"
PROMPT_FILE = ".claude/skills/impl-review-loop/tests/fixtures/body_only_lane_runtime_smoke_prompt.md"
TARGET_SKILL = ".claude/skills/impl-review-loop/SKILL.md"
# #2971 / PR #2976 の merge commit 4afb0551 の親。body-only lane の dry-run 節を持たない変更前の SKILL.md。
BASE_COMMIT = "8561d147a1164e712ca5fe3d8124a9ff437614c6"
EXPECT_SKILL_COMMAND = "impl-review-loop"
RUN_OUTPUT_ROOT = "artifacts/runtime-smoke"
RUN_DIR_PREFIX = "issue-2986-body-only-lane"
EXPECTED_ORDERED_MARKERS = (
    "BODY_ONLY_LANE_ELIGIBLE",
    "BODY_ONLY_LANE_STEP1_NOT_DISPATCHED",
    "BODY_ONLY_LANE_REPAIR_VIA_UPDATE_PR",
    "BODY_ONLY_LANE_STEP4_REUSE_STORED",
    "BODY_ONLY_LANE_STEP5_TERMINAL_GATE",
)
DOCUMENTED_COUNTERFACTUAL_OPTIONS = (
    "--skill-text-counterfactual-base-ref",
    "--skill-text-counterfactual-skill",
    "--evidence-json",
)
CF_RESULT_SCHEMA = "SKILL_TEXT_COUNTERFACTUAL_RESULT_V1"
VERDICT_DISCRIMINATIVE = "discriminative"
EXIT_OK = 0
EXIT_CAPABILITY_UNAVAILABLE = 77
HELP_TIMEOUT_SECONDS = 60
RUNNER_TIMEOUT_SECONDS = 1800
ARM_TIMEOUT_SECONDS = 420
ARM_MAX_TURNS = 12
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RunnerFailure(Exception):
    """runner の起動・``--help`` 自体の失敗（capability unavailable ではなく runner failure）。"""


# --- F5: capability gate（runner --help の documented option の有無だけを見る） --------------------


def runner_cmd() -> list[str]:
    return [sys.executable, str(RUNNER)]


def missing_documented_options(help_text: str) -> list[str]:
    """``--help`` 出力に documented な flag 名が現れるか（推測用の正規表現検知ではなく flag 名の存在確認）。"""
    return [
        flag
        for flag in DOCUMENTED_COUNTERFACTUAL_OPTIONS
        if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text) is None
    ]


def probe_runner_capability(cmd: list[str], *, timeout: float = HELP_TIMEOUT_SECONDS) -> list[str]:
    """runner の ``--help`` を実行し、欠けている documented option を返す（空なら capability あり）。

    ``--help`` の非ゼロ終了・timeout・import error・起動失敗は ``RunnerFailure``（runner failure）であり、
    capability unavailable（exit 77 の SKIP）には混同しない。
    """
    try:
        completed = subprocess.run([*cmd, "--help"], capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RunnerFailure(f"runner --help timed out after {timeout}s") from exc
    except OSError as exc:
        raise RunnerFailure(f"runner could not be launched: {type(exc).__name__}: {exc}") from exc
    if completed.returncode != 0:
        raise RunnerFailure(
            f"runner --help exited with {completed.returncode}; stderr tail: {completed.stderr[-400:]!r}"
        )
    return missing_documented_options(completed.stdout)


def skip_with_exit_77(reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    """``SKIP:`` + 理由を stdout に出し、pytest.exit(returncode=77) とする（pytest 内部の skip / xfail は使わない）。"""
    message = f"SKIP: {reason}"
    with capsys.disabled():
        print(message, flush=True)
    pytest.exit(message, returncode=EXIT_CAPABILITY_UNAVAILABLE)


def require_runner_capability(
    capsys: pytest.CaptureFixture[str], cmd: list[str] | None = None, *, timeout: float = HELP_TIMEOUT_SECONDS
) -> None:
    """documented option が揃っていなければ exit 77、runner failure なら fail、揃っていれば何もしない。"""
    try:
        missing = probe_runner_capability(cmd if cmd is not None else runner_cmd(), timeout=timeout)
    except RunnerFailure as exc:
        pytest.fail(f"runner failure (not capability unavailable): {exc}", pytrace=False)
    if missing:
        skip_with_exit_77(
            "runtime smoke runner does not document the counterfactual options "
            f"{', '.join(missing)}; candidate PASS + BASE FAIL cannot be established (unverified, not PASS).",
            capsys,
        )


# --- F1: run ごとに固有の artifact directory ------------------------------------------------------


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    output_dir: Path
    evidence_json: Path


def allocate_run_paths(
    output_root: Path,
    *,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(8),
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> RunPaths:
    """衝突しない固有の run directory を排他的に作る（既存 directory の削除・再利用はしない）。

    ``run_dir`` は ``--evidence-json`` の親として先に作る。runner の ``--output-dir`` は ``run_dir`` 配下の
    未作成 path とし、runner 自身が排他的 create する。名前の衝突は ``FileExistsError`` で失敗させる。
    """
    stamp = clock().strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_root / f"{RUN_DIR_PREFIX}-{stamp}-{token_factory()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return RunPaths(run_dir=run_dir, output_dir=run_dir / "runner-output", evidence_json=run_dir / "evidence.json")


# --- AC1: runner の documented interface での呼び出し --------------------------------------------


def build_runner_argv(paths: RunPaths, *, worktree: Path = ROOT) -> list[str]:
    argv = [
        *runner_cmd(),
        "--runtime",
        "claude",
        "--mode",
        "structured",
        "--claude-adapter",
        "native",
        "--worktree",
        str(worktree),
        "--prompt-file",
        PROMPT_FILE,
        "--output-dir",
        str(paths.output_dir),
        "--timeout-seconds",
        str(ARM_TIMEOUT_SECONDS),
        "--max-turns",
        str(ARM_MAX_TURNS),
        "--expect-marker-source",
        "main",
        "--expect-skill-command",
        EXPECT_SKILL_COMMAND,
    ]
    for marker in EXPECTED_ORDERED_MARKERS:
        argv += ["--expect-ordered-marker", marker]
    argv += [
        "--skill-text-counterfactual-base-ref",
        BASE_COMMIT,
        "--skill-text-counterfactual-skill",
        TARGET_SKILL,
        "--evidence-json",
        str(paths.evidence_json),
    ]
    return argv


# --- F3: runner の top-level verdict / exit code を authority とする判定 -------------------------


@dataclass
class Judgement:
    status: str  # "pass" | "unavailable" | "fail"
    problems: list[str] = field(default_factory=list)


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def evaluate_counterfactual_result(returncode: int, evidence_json: Path, *, tested_head: str) -> Judgement:
    """runner の exit code と ``--evidence-json`` の読み戻しから PASS を判定する。

    PASS は runner の exit 0 かつ top-level ``verdict == "discriminative"`` かつ identity の整合が全て成立する
    場合だけ。exit 77 は unavailable（PASS ではない）。JSON の欠損・解析失敗・不整合は PASS にしない。
    runner の分類を再計算する意味分類器は持たない。
    """
    problems: list[str] = []
    if returncode == EXIT_CAPABILITY_UNAVAILABLE:
        return Judgement(
            "unavailable", [f"runner exit {EXIT_CAPABILITY_UNAVAILABLE}: capability unavailable (unverified)"]
        )
    if returncode != EXIT_OK:
        problems.append(f"runner exit code {returncode} != 0")
    try:
        raw = evidence_json.read_text(encoding="utf-8")
    except OSError as exc:
        return Judgement("fail", [*problems, f"evidence json missing or unreadable: {type(exc).__name__}"])
    try:
        summary = json.loads(raw)
    except ValueError:
        return Judgement("fail", [*problems, "evidence json is not parseable"])
    if not isinstance(summary, dict):
        return Judgement("fail", [*problems, "evidence json is not an object"])

    def expect(name: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            problems.append(f"{name}: expected {expected!r}, got {actual!r}")

    verdict = summary.get("verdict")
    expect("schema", summary.get("schema"), CF_RESULT_SCHEMA)
    expect("verdict", verdict, VERDICT_DISCRIMINATIVE)
    expect("exit_code", summary.get("exit_code"), EXIT_OK)
    expect(
        "classification (must equal top-level verdict)",
        _get(summary, "skill_text_counterfactual", "classification"),
        verdict,
    )
    cf = ("skill_text_counterfactual",)
    expect("resolved_base_commit_sha", _get(summary, *cf, "resolved_base_commit_sha"), BASE_COMMIT)
    expect("candidate_head_sha (tested HEAD)", _get(summary, *cf, "candidate_head_sha"), tested_head)
    expect("target_skill_path", _get(summary, *cf, "target_skill_path"), TARGET_SKILL)
    expect("prompt_sha256.identical", _get(summary, *cf, "prompt_sha256", "identical"), True)
    expect(
        "ordered_evidence_match.candidate.verified",
        _get(summary, *cf, "ordered_evidence_match", "candidate", "verified"),
        True,
    )
    expect(
        "ordered_evidence_match.base.verified", _get(summary, *cf, "ordered_evidence_match", "base", "verified"), False
    )
    expect("cleanup.all_removed", _get(summary, *cf, "cleanup", "all_removed"), True)

    candidate_blob = _get(summary, *cf, "candidate_skill_blob_id")
    base_blob = _get(summary, *cf, "base_skill_blob_id")
    if not (isinstance(candidate_blob, str) and candidate_blob and isinstance(base_blob, str) and base_blob):
        problems.append("skill blob ids missing")
    elif candidate_blob == base_blob:
        problems.append("candidate_skill_blob_id == base_skill_blob_id")

    arms = {name: _get(summary, *cf, "arms", name) for name in ("candidate", "base")}
    for name, arm in arms.items():
        if not isinstance(arm, dict):
            problems.append(f"arms.{name} missing")
            continue
        for key in ("run_id", "runtime_version", "tested_head"):
            if not (isinstance(arm.get(key), str) and arm[key]):
                problems.append(f"arms.{name}.{key} missing")
    if all(isinstance(arm, dict) for arm in arms.values()):
        expect("arms.candidate.tested_head", arms["candidate"].get("tested_head"), tested_head)
        expect("arms.base.tested_head", arms["base"].get("tested_head"), _get(summary, *cf, "base_arm_commit_sha"))
        if arms["candidate"].get("tested_head") == arms["base"].get("tested_head"):
            problems.append("candidate and base arms share the same tested_head")
        if arms["candidate"].get("run_id") == arms["base"].get("run_id"):
            problems.append("candidate and base arms share the same run_id")
        expect("arms.candidate.exit_code", arms["candidate"].get("exit_code"), EXIT_OK)
        base_exit = arms["base"].get("exit_code")
        if base_exit in (None, EXIT_OK, EXIT_CAPABILITY_UNAVAILABLE):
            problems.append(f"arms.base.exit_code must be a runner failure, got {base_exit!r}")
        expect(
            "arms runtime_version equality",
            arms["base"].get("runtime_version"),
            arms["candidate"].get("runtime_version"),
        )
    return Judgement("pass" if not problems else "fail", problems)


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, timeout=60).stdout


def write_runtime_verification_log(judgement: Judgement, paths: RunPaths, tested_head: str, runner_exit: int) -> Path:
    """worktree-local（git-ignored）の ``artifacts/runtime-verification-AC4-<UTC>.log`` を書く（commit しない）。"""
    now = datetime.now(timezone.utc)
    summary: dict[str, Any] = {}
    try:
        loaded = json.loads(paths.evidence_json.read_text(encoding="utf-8"))
        summary = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    cf = summary.get("skill_text_counterfactual") if isinstance(summary.get("skill_text_counterfactual"), dict) else {}
    arms = cf.get("arms") if isinstance(cf.get("arms"), dict) else {}

    def arm_line(name: str) -> str:
        arm = arms.get(name) if isinstance(arms.get(name), dict) else {}
        return (
            f"  {name}: run_id={arm.get('run_id')} runtime_version={arm.get('runtime_version')} "
            f"tested_head={arm.get('tested_head')} exit_code={arm.get('exit_code')}"
        )

    reason = "; ".join(judgement.problems) or "discriminative (candidate PASS + BASE FAIL)"
    ordered = cf.get("ordered_evidence_match") if isinstance(cf.get("ordered_evidence_match"), dict) else {}
    lines = [
        "AC: AC4",
        f"Timestamp: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "Environment:",
        arm_line("candidate"),
        arm_line("base"),
        "Input:",
        f"  prompt_file={PROMPT_FILE} base_ref={BASE_COMMIT} target={TARGET_SKILL}",
        f"  tested_head={tested_head} evidence_json={paths.evidence_json.relative_to(ROOT)}",
        "Output:",
        f"  verdict={summary.get('verdict')} exit_code={summary.get('exit_code')}",
        f"  resolved_base_commit_sha={cf.get('resolved_base_commit_sha')}",
        f"  candidate_head_sha={cf.get('candidate_head_sha')}",
        f"  candidate_skill_blob_id={cf.get('candidate_skill_blob_id')}",
        f"  base_skill_blob_id={cf.get('base_skill_blob_id')}",
        f"  prompt_sha256.identical={_get(cf, 'prompt_sha256', 'identical')}",
        f"  ordered_evidence_match.candidate.verified={_get(ordered, 'candidate', 'verified')}",
        f"  ordered_evidence_match.base.verified={_get(ordered, 'base', 'verified')}",
        f"  cleanup.all_removed={_get(cf, 'cleanup', 'all_removed')}",
        "Verdict:",
        f"  Result: {judgement.status.upper()}",
        f"  Exit Code: {runner_exit}",
        f"  Reason: {reason}",
        "Limitation: discrimination evidence for the given prompt/runtime/sample (one LLM sample per arm); "
        "not statistical or absolute causal proof.",
    ]
    log = ROOT / "artifacts" / f"runtime-verification-AC4-{now.strftime('%Y%m%dT%H%M%SZ')}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return log


@pytest.mark.claude_live
def test_ac4_body_only_lane_counterfactual_is_discriminative_on_committed_head(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (ROOT / PROMPT_FILE).is_file()
    assert RUNNER.is_file()
    require_runner_capability(capsys)

    # F4: live smoke は commit 済みで clean な linked worktree の candidate HEAD に対してのみ開始する。
    assert _git("status", "--porcelain") == "", "candidate worktree must be clean (commit before the live smoke)"
    tested_head = _git("rev-parse", "HEAD").strip()
    assert SHA_RE.match(tested_head)

    paths = allocate_run_paths(ROOT / RUN_OUTPUT_ROOT)
    result = subprocess.run(
        build_runner_argv(paths),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=RUNNER_TIMEOUT_SECONDS,
        check=False,
    )
    judgement = evaluate_counterfactual_result(result.returncode, paths.evidence_json, tested_head=tested_head)
    log = write_runtime_verification_log(judgement, paths, tested_head, result.returncode)
    print(f"runtime verification log: {log}")
    print(f"evidence json: {paths.evidence_json}")

    if judgement.status == "unavailable":
        skip_with_exit_77("; ".join(judgement.problems), capsys)
    assert judgement.status == "pass", (
        f"counterfactual smoke is not PASS (runner exit={result.returncode}): {judgement.problems}\n"
        f"stdout={result.stdout[-1500:]}\nstderr={result.stderr[-1500:]}"
    )
    summary = paths.output_dir / "summary.md"
    assert summary.is_file() and summary.read_text(encoding="utf-8").strip()


# --- hermetic tests ---------------------------------------------------------------------------


def _valid_summary(head: str = "a" * 40) -> dict[str, Any]:
    return {
        "schema": CF_RESULT_SCHEMA,
        "mode": "skill_text_counterfactual",
        "run_id": "r0",
        "verdict": VERDICT_DISCRIMINATIVE,
        "exit_code": 0,
        "errors": [],
        "skill_text_counterfactual": {
            "requested_base_ref": BASE_COMMIT,
            "resolved_base_commit_sha": BASE_COMMIT,
            "candidate_head_sha": head,
            "target_skill_path": TARGET_SKILL,
            "candidate_skill_blob_id": "c" * 40,
            "base_skill_blob_id": "d" * 40,
            "base_arm_commit_sha": "b" * 40,
            "prompt_sha256": {"expected": "e" * 64, "candidate": "e" * 64, "base": "e" * 64, "identical": True},
            "ordered_evidence_match": {"candidate": {"verified": True}, "base": {"verified": False}},
            "arms": {
                "candidate": {"run_id": "cand-1", "runtime_version": "2.1.0", "tested_head": head, "exit_code": 0},
                "base": {"run_id": "base-1", "runtime_version": "2.1.0", "tested_head": "b" * 40, "exit_code": 1},
            },
            "classification": VERDICT_DISCRIMINATIVE,
            "cleanup": {"all_removed": True, "failures": []},
        },
    }


def _mutated(mutator: Callable[[dict[str, Any]], None], head: str = "a" * 40) -> dict[str, Any]:
    summary = copy.deepcopy(_valid_summary(head))
    mutator(summary)
    return summary


def _judge(tmp_path: Path, returncode: int, summary: Any, *, raw: str | None = None, head: str = "a" * 40) -> Judgement:
    evidence = tmp_path / "evidence.json"
    if raw is not None:
        evidence.write_text(raw, encoding="utf-8")
    elif summary is not None:
        evidence.write_text(json.dumps(summary), encoding="utf-8")
    return evaluate_counterfactual_result(returncode, evidence, tested_head=head)


def _set_final_verdict(verdict: str, exit_code: int) -> Callable[[dict[str, Any]], None]:
    def mutate(summary: dict[str, Any]) -> None:
        summary["verdict"] = verdict
        summary["exit_code"] = exit_code
        summary["skill_text_counterfactual"]["classification"] = verdict

    return mutate


def test_ac1_runner_is_invoked_through_the_documented_counterfactual_interface(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path / "run", tmp_path / "run" / "runner-output", tmp_path / "run" / "evidence.json")
    argv = build_runner_argv(paths)

    def value_of(flag: str) -> str:
        assert argv.count(flag) == 1, flag
        return argv[argv.index(flag) + 1]

    assert value_of("--skill-text-counterfactual-base-ref") == BASE_COMMIT == "8561d147a1164e712ca5fe3d8124a9ff437614c6"
    assert value_of("--skill-text-counterfactual-skill") == TARGET_SKILL == ".claude/skills/impl-review-loop/SKILL.md"
    assert value_of("--evidence-json") == str(paths.evidence_json)
    assert value_of("--output-dir") == str(paths.output_dir)
    assert value_of("--expect-skill-command") == EXPECT_SKILL_COMMAND == "impl-review-loop"
    assert value_of("--prompt-file") == PROMPT_FILE
    assert (value_of("--runtime"), value_of("--mode"), value_of("--claude-adapter")) == (
        "claude",
        "structured",
        "native",
    )
    ordered = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--expect-ordered-marker"]
    assert ordered == list(EXPECTED_ORDERED_MARKERS)
    # 対照 mode は arm ごとに flag を再生成するので、candidate 用と BASE 用で分岐する option を渡さない。
    assert "--require-clean-postcondition" not in argv
    assert set(DOCUMENTED_COUNTERFACTUAL_OPTIONS) <= set(argv)


def test_ac1_the_base_commit_is_the_pre_dry_run_skill_text_and_markers_match_the_production_cli() -> None:
    base_skill = _git("show", f"{BASE_COMMIT}:{TARGET_SKILL}")
    head_skill = (ROOT / TARGET_SKILL).read_text(encoding="utf-8")
    assert "body-only-lane-dry-run" not in base_skill
    assert "body-only-lane-dry-run" in head_skill
    assert _git("rev-parse", "--verify", f"{BASE_COMMIT}^{{commit}}").strip() == BASE_COMMIT

    cli = subprocess.run(
        [sys.executable, str(DRY_RUN_CLI), "--dry-run-fixture", str(ROOT / DRY_RUN_FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert cli.returncode == 0, cli.stderr
    assert [line for line in cli.stdout.splitlines() if line] == list(EXPECTED_ORDERED_MARKERS)


def test_ac1_the_prompt_is_one_shared_file_and_is_free_of_cli_commands_and_markers() -> None:
    prompt = (ROOT / PROMPT_FILE).read_text(encoding="utf-8")
    assert "body-only-lane-dry-run" in prompt and DRY_RUN_FIXTURE in prompt
    assert "body_only_repair_plan" not in prompt
    assert "--dry-run-fixture" not in prompt
    assert not [marker for marker in EXPECTED_ORDERED_MARKERS if marker in prompt]


def test_ac2_only_a_discriminative_summary_with_exit_zero_passes(tmp_path: Path) -> None:
    judgement = _judge(tmp_path, 0, _valid_summary())
    assert judgement == Judgement("pass", [])


@pytest.mark.parametrize(
    ("verdict", "exit_code"),
    [
        ("non_discriminative", 1),
        ("control_invalid", 1),
        ("candidate_fail", 1),
        ("cleanup_failed", 1),
        ("preflight_failed", 1),
        ("isolation_violation", 1),
        ("runner_error", 1),
    ],
)
def test_ac2_every_non_discriminative_runner_verdict_is_not_a_pass(
    tmp_path: Path, verdict: str, exit_code: int
) -> None:
    summary = _mutated(_set_final_verdict(verdict, exit_code))
    judgement = _judge(tmp_path, exit_code, summary)
    assert judgement.status == "fail"
    assert judgement.problems


def test_ac2_non_discriminative_where_base_also_passes_is_a_fail(tmp_path: Path) -> None:
    def mutate(summary: dict[str, Any]) -> None:
        _set_final_verdict("non_discriminative", 1)(summary)
        summary["skill_text_counterfactual"]["ordered_evidence_match"]["base"]["verified"] = True
        summary["skill_text_counterfactual"]["arms"]["base"]["exit_code"] = 0

    judgement = _judge(tmp_path, 1, _mutated(mutate))
    assert judgement.status == "fail"
    assert any("verdict" in problem for problem in judgement.problems)


def test_ac2_runner_exit_77_is_unavailable_and_never_a_pass(tmp_path: Path) -> None:
    summary = _mutated(_set_final_verdict("candidate_skip", 77))
    assert _judge(tmp_path, 77, summary).status == "unavailable"
    # JSON が無くても exit 77 は PASS にならない。
    assert _judge(tmp_path / "missing", 77, None).status == "unavailable"


def test_ac2_a_discriminative_json_cannot_override_a_non_zero_runner_exit(tmp_path: Path) -> None:
    judgement = _judge(tmp_path, 1, _valid_summary())
    assert judgement.status == "fail"
    assert any("runner exit code" in problem for problem in judgement.problems)


def test_ac2_a_non_discriminative_json_cannot_pass_even_with_runner_exit_zero(tmp_path: Path) -> None:
    summary = _mutated(_set_final_verdict("non_discriminative", 1))
    assert _judge(tmp_path, 0, summary).status == "fail"


def test_ac2_missing_evidence_json_is_not_a_pass(tmp_path: Path) -> None:
    judgement = _judge(tmp_path, 0, None)
    assert judgement.status == "fail"
    assert any("missing" in problem for problem in judgement.problems)


@pytest.mark.parametrize("raw", ["", "{not json", "[]", "null", '"discriminative"'])
def test_ac2_unparseable_or_non_object_evidence_json_is_not_a_pass(tmp_path: Path, raw: str) -> None:
    assert _judge(tmp_path, 0, None, raw=raw).status == "fail"


def test_ac2_top_level_verdict_must_agree_with_the_nested_classification(tmp_path: Path) -> None:
    def mutate(summary: dict[str, Any]) -> None:
        summary["skill_text_counterfactual"]["classification"] = "non_discriminative"

    judgement = _judge(tmp_path, 0, _mutated(mutate))
    assert judgement.status == "fail"
    assert any("classification" in problem for problem in judgement.problems)


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("resolved base sha", lambda s: s["skill_text_counterfactual"].update(resolved_base_commit_sha="f" * 40)),
        ("candidate head sha", lambda s: s["skill_text_counterfactual"].update(candidate_head_sha="f" * 40)),
        ("prompt identity", lambda s: s["skill_text_counterfactual"]["prompt_sha256"].update(identical=False)),
        (
            "candidate ordered match",
            lambda s: s["skill_text_counterfactual"]["ordered_evidence_match"]["candidate"].update(verified=False),
        ),
        (
            "base ordered match",
            lambda s: s["skill_text_counterfactual"]["ordered_evidence_match"]["base"].update(verified=True),
        ),
        ("cleanup", lambda s: s["skill_text_counterfactual"]["cleanup"].update(all_removed=False)),
        ("same blob id", lambda s: s["skill_text_counterfactual"].update(base_skill_blob_id="c" * 40)),
        ("missing blob id", lambda s: s["skill_text_counterfactual"].update(candidate_skill_blob_id=None)),
        ("missing candidate run id", lambda s: s["skill_text_counterfactual"]["arms"]["candidate"].pop("run_id")),
        ("same run id", lambda s: s["skill_text_counterfactual"]["arms"]["base"].update(run_id="cand-1")),
        ("missing runtime version", lambda s: s["skill_text_counterfactual"]["arms"]["base"].pop("runtime_version")),
        (
            "runtime version mismatch",
            lambda s: s["skill_text_counterfactual"]["arms"]["base"].update(runtime_version="9.9.9"),
        ),
        ("missing base arm", lambda s: s["skill_text_counterfactual"]["arms"].update(base=None)),
        ("arm tested head", lambda s: s["skill_text_counterfactual"]["arms"]["candidate"].update(tested_head="f" * 40)),
        ("base arm passed", lambda s: s["skill_text_counterfactual"]["arms"]["base"].update(exit_code=0)),
        ("json exit code", lambda s: s.update(exit_code=1)),
        ("wrong schema", lambda s: s.update(schema="OTHER")),
    ],
)
def test_ac2_identity_or_consistency_violations_are_not_a_pass(
    tmp_path: Path, name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    judgement = _judge(tmp_path, 0, _mutated(mutate))
    assert judgement.status == "fail", name


def test_ac2_a_stale_tested_head_is_not_a_pass(tmp_path: Path) -> None:
    judgement = _judge(tmp_path, 0, _valid_summary("a" * 40), head="9" * 40)
    assert judgement.status == "fail"


# AC3: capability gate を REAL pytest の子 process で実行し、stdout / stderr / exit code を観測する。

_CHILD_TEMPLATE = """\
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("body_only_lane_smoke_under_test", {module_path!r})
module = importlib.util.module_from_spec(spec)
sys.modules["body_only_lane_smoke_under_test"] = module
spec.loader.exec_module(module)


def test_gate(capsys):
    module.require_runner_capability(capsys, {cmd!r}, timeout={timeout!r})
    print("GATE_PASSED_THROUGH")
"""


def _run_gate_in_child_pytest(
    tmp_path: Path, fake_runner_source: str, *, timeout: float = 20
) -> subprocess.CompletedProcess[str]:
    fake_runner = tmp_path / "fake_runner.py"
    fake_runner.write_text(textwrap.dedent(fake_runner_source), encoding="utf-8")
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    (child_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    cmd = [sys.executable, str(fake_runner)]
    (child_dir / "test_child_gate.py").write_text(
        _CHILD_TEMPLATE.format(module_path=str(THIS_FILE), cmd=cmd, timeout=timeout), encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_child_gate.py",
            "-q",
            "-c",
            "pytest.ini",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            ".",
        ],
        cwd=str(child_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _help_stdout_runner(options: tuple[str, ...]) -> str:
    return f"print('usage: runner [-h] ' + ' '.join({list(options)!r}))\n"


def _stdout_lines(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return completed.stdout.splitlines()


def test_ac3_a_clean_help_without_the_documented_options_exits_77_with_skip_on_stdout(tmp_path: Path) -> None:
    child = _run_gate_in_child_pytest(tmp_path, _help_stdout_runner(("--skill-text-counterfactual-base-ref",)))

    assert child.returncode == EXIT_CAPABILITY_UNAVAILABLE == 77
    skip_lines = [line for line in _stdout_lines(child) if line.startswith("SKIP:")]
    assert skip_lines, child.stdout
    assert "--skill-text-counterfactual-skill" in skip_lines[0] and "--evidence-json" in skip_lines[0]
    assert "GATE_PASSED_THROUGH" not in child.stdout


def test_ac3_a_help_with_every_documented_option_passes_through_the_gate(tmp_path: Path) -> None:
    child = _run_gate_in_child_pytest(tmp_path, _help_stdout_runner(DOCUMENTED_COUNTERFACTUAL_OPTIONS))

    assert child.returncode == 0, child.stdout + child.stderr
    assert not [line for line in _stdout_lines(child) if line.startswith("SKIP:")]


@pytest.mark.parametrize(
    ("scenario", "source", "timeout"),
    [
        ("non-zero --help", "import sys\nprint('usage: runner')\nsys.exit(3)\n", 20),
        ("import error", "import module_that_does_not_exist_for_this_smoke\n", 20),
        ("--help timeout", "import time\ntime.sleep(30)\n", 1),
    ],
)
def test_ac3_runner_failures_are_failures_and_never_confused_with_exit_77(
    tmp_path: Path, scenario: str, source: str, timeout: float
) -> None:
    child = _run_gate_in_child_pytest(tmp_path, source, timeout=timeout)

    assert child.returncode == 1, (scenario, child.returncode, child.stdout, child.stderr)
    assert child.returncode != EXIT_CAPABILITY_UNAVAILABLE
    assert not [line for line in _stdout_lines(child) if line.startswith("SKIP:")], scenario
    assert "runner failure" in child.stdout
    assert "GATE_PASSED_THROUGH" not in child.stdout


def test_ac3_a_runner_that_cannot_be_launched_is_a_failure_not_exit_77(tmp_path: Path) -> None:
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    (child_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    missing_executable = str(tmp_path / "no-such-executable")
    (child_dir / "test_child_gate.py").write_text(
        _CHILD_TEMPLATE.format(module_path=str(THIS_FILE), cmd=[missing_executable], timeout=20), encoding="utf-8"
    )
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_child_gate.py",
            "-q",
            "-c",
            "pytest.ini",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            ".",
        ],
        cwd=str(child_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert child.returncode == 1, (child.returncode, child.stdout, child.stderr)
    assert not [line for line in _stdout_lines(child) if line.startswith("SKIP:")]
    assert "could not be launched" in child.stdout


def test_ac3_documented_flag_detection_matches_whole_flag_names_only() -> None:
    assert missing_documented_options(" ".join(DOCUMENTED_COUNTERFACTUAL_OPTIONS)) == []
    assert missing_documented_options("--skill-text-counterfactual-base-ref-x --skill-text-counterfactual-skill") == [
        "--skill-text-counterfactual-base-ref",
        "--evidence-json",
    ]
    assert missing_documented_options("--require-session-baseline-preservation --require-clean-postcondition") == list(
        DOCUMENTED_COUNTERFACTUAL_OPTIONS
    )


def test_ac3_the_real_runner_documents_every_counterfactual_option() -> None:
    """#2981 で導入された documented option の存在のみを確認する（BASE 対照の成立は意味しない）。"""
    assert probe_runner_capability(runner_cmd()) == []


# AC5: run ごとに固有の artifact directory。


def test_ac5_each_run_gets_a_distinct_output_path_and_existing_directories_survive(tmp_path: Path) -> None:
    first = allocate_run_paths(tmp_path)
    marker = first.run_dir / "keep.txt"
    marker.write_text("evidence of an earlier run", encoding="utf-8")
    first.output_dir.mkdir()
    (first.output_dir / "summary.md").write_text("earlier summary", encoding="utf-8")

    runs = [allocate_run_paths(tmp_path) for _ in range(5)]
    all_paths = [first, *runs]

    assert len({p.run_dir for p in all_paths}) == len(all_paths)
    assert len({p.output_dir for p in all_paths}) == len(all_paths)
    assert len({p.evidence_json for p in all_paths}) == len(all_paths)
    for paths in all_paths:
        assert paths.run_dir.is_dir()
        assert paths.output_dir.parent == paths.run_dir and paths.evidence_json.parent == paths.run_dir
        assert paths.run_dir.name.startswith(RUN_DIR_PREFIX)
    assert marker.read_text(encoding="utf-8") == "evidence of an earlier run"
    assert (first.output_dir / "summary.md").read_text(encoding="utf-8") == "earlier summary"
    # runner が排他的に作る output dir 自体は、こちらでは作らない（既存 dir の再利用をしない）。
    assert not any(p.output_dir.exists() for p in runs)


def test_ac5_a_name_collision_fails_without_deleting_or_reusing_the_existing_directory(tmp_path: Path) -> None:
    frozen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = allocate_run_paths(tmp_path, token_factory=lambda: "fixedtoken", clock=lambda: frozen)
    first.evidence_json.write_text('{"keep": true}', encoding="utf-8")

    with pytest.raises(FileExistsError):
        allocate_run_paths(tmp_path, token_factory=lambda: "fixedtoken", clock=lambda: frozen)

    assert json.loads(first.evidence_json.read_text(encoding="utf-8")) == {"keep": True}
    assert first.run_dir.is_dir()


def test_ac5_the_test_file_keeps_no_fixed_output_directory_and_deletes_nothing() -> None:
    tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"))
    forbidden_names = {"OUTPUT" + "_DIR"}
    forbidden_calls = {"rm" + "tree", "unlink", "remove", "removedirs"}
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_calls
    assert "shutil" not in imported
