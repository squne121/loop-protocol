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
import os
import re
import secrets
import signal
import subprocess
import sys
import textwrap
import time
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
# outer deadline 到達時に runner へ SIGTERM を送ってから SIGKILL へ昇格するまでの bounded grace。runner は SIGTERM を
# ``_TerminateRequested`` に変換し、arm の process group を停止（``_CF_ARM_INTERRUPT_GRACE_SECONDS`` = 12s）してから
# ``finally`` で ephemeral worktree 2 個を ``git worktree remove`` する（通常は数秒）。runner 内の個別 git 操作は
# 最大 300s の timeout を持つが、それを待ち切る無制限待機はせず、この上限を超えたら自分の Popen が所有する group
# だけを SIGKILL する。
RUNNER_TERM_GRACE_SECONDS = 120.0
RUNNER_REAP_WAIT_SECONDS = 5.0
DIAGNOSTIC_TAIL_CHARS = 1500
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

    def expect_int(name: str, actual: Any, expected: int) -> None:
        # ``False == 0`` / ``True == 1`` を受理しないよう、型も厳密に int であることを要求する。
        if type(actual) is not int or actual != expected:
            problems.append(f"{name}: expected int {expected!r}, got {actual!r}")

    def expect_bool(name: str, actual: Any, expected: bool) -> None:
        # ``1 == True`` / ``0 == False`` を受理しないよう、bool の同一性で比較する。
        if actual is not expected:
            problems.append(f"{name}: expected bool {expected!r}, got {actual!r}")

    verdict = summary.get("verdict")
    expect("schema", summary.get("schema"), CF_RESULT_SCHEMA)
    expect("verdict", verdict, VERDICT_DISCRIMINATIVE)
    expect_int("exit_code", summary.get("exit_code"), EXIT_OK)
    expect(
        "classification (must equal top-level verdict)",
        _get(summary, "skill_text_counterfactual", "classification"),
        verdict,
    )
    cf = ("skill_text_counterfactual",)
    expect("resolved_base_commit_sha", _get(summary, *cf, "resolved_base_commit_sha"), BASE_COMMIT)
    expect("candidate_head_sha (tested HEAD)", _get(summary, *cf, "candidate_head_sha"), tested_head)
    expect("target_skill_path", _get(summary, *cf, "target_skill_path"), TARGET_SKILL)
    expect_bool("prompt_sha256.identical", _get(summary, *cf, "prompt_sha256", "identical"), True)
    expect_bool(
        "ordered_evidence_match.candidate.verified",
        _get(summary, *cf, "ordered_evidence_match", "candidate", "verified"),
        True,
    )
    expect_bool(
        "ordered_evidence_match.base.verified",
        _get(summary, *cf, "ordered_evidence_match", "base", "verified"),
        False,
    )
    expect_bool("cleanup.all_removed", _get(summary, *cf, "cleanup", "all_removed"), True)

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
        expect_int("arms.candidate.exit_code", arms["candidate"].get("exit_code"), EXIT_OK)
        base_exit = arms["base"].get("exit_code")
        if type(base_exit) is not int or base_exit in (EXIT_OK, EXIT_CAPABILITY_UNAVAILABLE):
            problems.append(f"arms.base.exit_code must be a runner failure, got {base_exit!r}")
        expect(
            "arms runtime_version equality",
            arms["base"].get("runtime_version"),
            arms["candidate"].get("runtime_version"),
        )
    return Judgement("pass" if not problems else "fail", problems)


def _git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, timeout=60).stdout


# --- P2-2: outer deadline 到達時に runner の finally（worktree 回収）を走らせる bounded 起動 ---------------


@dataclass
class RunnerExecution:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    sigkill_escalated: bool = False
    timeout_seconds: float | None = None


def _decode_partial(raw: Any) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def _stop_owned_group(proc: subprocess.Popen[str], *, term_grace: float, reap_wait: float) -> tuple[str, str, bool]:
    """自分の ``Popen``（``start_new_session=True``、pgid == pid）が所有する group だけを停止する。

    SIGTERM（runner の ``finally`` cleanup を走らせる）→ ``term_grace`` 秒まで EOF 待ち → 超過時のみ SIGKILL →
    reap と pipe close。SIGKILL 後は ``communicate()`` を呼ばない（group 外へ逃げた子孫が pipe を保持すると EOF 待ちで
    ハングするため）。返り値は (stdout, stderr, sigkill_escalated)。
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        out, err = proc.communicate(timeout=term_grace)
        return out, err, False
    except subprocess.TimeoutExpired as exc:
        out, err = _decode_partial(exc.stdout), _decode_partial(exc.stderr)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=reap_wait)
    except subprocess.TimeoutExpired:
        pass
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    return out, err, True


def run_runner_bounded(
    argv: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = RUNNER_TIMEOUT_SECONDS,
    term_grace: float = RUNNER_TERM_GRACE_SECONDS,
    reap_wait: float = RUNNER_REAP_WAIT_SECONDS,
) -> RunnerExecution:
    """runner を専用 process group で起動し、outer deadline 到達時は SIGTERM → bounded grace → SIGKILL で止める。

    ``subprocess.run(timeout=...)`` は直接の子を kill するだけで、runner の ``finally``（ephemeral worktree の回収）が
    走らない。ここでは先に SIGTERM を送って runner 自身の cleanup を走らせ、grace を超えた場合だけ SIGKILL する。
    取得できた stdout / stderr は timeout 時も ``RunnerExecution`` に残す。
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err, escalated = _stop_owned_group(proc, term_grace=term_grace, reap_wait=reap_wait)
        return RunnerExecution(proc.returncode, out, err, True, escalated, timeout)
    except BaseException:
        # pytest 自身の中断（KeyboardInterrupt 等）でも runner を孤児にしない。
        _stop_owned_group(proc, term_grace=term_grace, reap_wait=reap_wait)
        raise
    return RunnerExecution(proc.returncode, out, err)


def judge_runner_execution(execution: RunnerExecution, evidence_json: Path, *, tested_head: str) -> Judgement:
    """outer timeout / 強制終了は PASS にしない。それ以外は runner の exit code と evidence の読み戻しで判定する。"""
    if not execution.timed_out:
        return evaluate_counterfactual_result(execution.returncode, evidence_json, tested_head=tested_head)
    how = (
        f"SIGTERM grace exceeded, escalated to SIGKILL of the owned process group (runner returncode="
        f"{execution.returncode}); runner cleanup is not confirmed"
        if execution.sigkill_escalated
        else f"stopped by SIGTERM within the grace (runner returncode={execution.returncode})"
    )
    return Judgement(
        "fail", [f"runner exceeded the outer deadline of {execution.timeout_seconds}s and was terminated: {how}"]
    )


def _tail(text: str) -> str:
    return text[-DIAGNOSTIC_TAIL_CHARS:]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def write_runtime_verification_log(
    judgement: Judgement,
    paths: RunPaths,
    tested_head: str,
    execution: RunnerExecution,
    *,
    log_dir: Path = ROOT / "artifacts",
) -> Path:
    """worktree-local（git-ignored）の ``runtime-verification-AC4-<run directory 名>.log`` を書く（commit しない）。

    run directory 名は ``allocate_run_paths`` が排他的に作った UTC 秒 + 固有 token を含むので、同一秒の run でも
    log path は衝突せず、log が束縛する run（evidence）も名前から一意に分かる。既存 log は上書きしない。
    """
    now = datetime.now(timezone.utc)
    runner_exit = execution.returncode
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
        f"  tested_head={tested_head} evidence_json={_display_path(paths.evidence_json)}",
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
        f"  runner timed_out={execution.timed_out} sigkill_escalated={execution.sigkill_escalated}",
        f"  runner stdout tail: {_tail(execution.stdout)!r}",
        f"  runner stderr tail: {_tail(execution.stderr)!r}",
        "Verdict:",
        f"  Result: {judgement.status.upper()}",
        f"  Exit Code: {runner_exit}",
        f"  Reason: {reason}",
        "Limitation: discrimination evidence for the given prompt/runtime/sample (one LLM sample per arm); "
        "not statistical or absolute causal proof.",
    ]
    log = log_dir / f"runtime-verification-AC4-{paths.run_dir.name}.log"
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
    execution = run_runner_bounded(build_runner_argv(paths), cwd=ROOT)
    judgement = judge_runner_execution(execution, paths.evidence_json, tested_head=tested_head)
    log = write_runtime_verification_log(judgement, paths, tested_head, execution)
    print(f"runtime verification log: {log}")
    print(f"evidence json: {paths.evidence_json}")

    if judgement.status == "unavailable":
        skip_with_exit_77("; ".join(judgement.problems), capsys)
    assert judgement.status == "pass", (
        f"counterfactual smoke is not PASS (runner exit={execution.returncode}): {judgement.problems}\n"
        f"stdout={_tail(execution.stdout)}\nstderr={_tail(execution.stderr)}"
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


# P2-1: exit code は int 型、真偽値は bool 型でなければならない（False == 0 / True == 1 を受理しない）。


@pytest.mark.parametrize(
    ("field_name", "mutate"),
    [
        ("exit_code", lambda s: s.update(exit_code=False)),
        ("exit_code", lambda s: s.update(exit_code=0.0)),
        (
            "arms.candidate.exit_code",
            lambda s: s["skill_text_counterfactual"]["arms"]["candidate"].update(exit_code=False),
        ),
        (
            "arms.candidate.exit_code",
            lambda s: s["skill_text_counterfactual"]["arms"]["candidate"].update(exit_code=0.0),
        ),
        ("arms.base.exit_code", lambda s: s["skill_text_counterfactual"]["arms"]["base"].update(exit_code=True)),
        ("arms.base.exit_code", lambda s: s["skill_text_counterfactual"]["arms"]["base"].update(exit_code=1.0)),
        ("prompt_sha256.identical", lambda s: s["skill_text_counterfactual"]["prompt_sha256"].update(identical=1)),
        (
            "ordered_evidence_match.candidate.verified",
            lambda s: s["skill_text_counterfactual"]["ordered_evidence_match"]["candidate"].update(verified=1),
        ),
        (
            "ordered_evidence_match.base.verified",
            lambda s: s["skill_text_counterfactual"]["ordered_evidence_match"]["base"].update(verified=0),
        ),
        ("cleanup.all_removed", lambda s: s["skill_text_counterfactual"]["cleanup"].update(all_removed=1)),
    ],
)
def test_ac2_boolean_as_int_and_int_as_boolean_are_not_a_pass(
    tmp_path: Path, field_name: str, mutate: Callable[[dict[str, Any]], None]
) -> None:
    judgement = _judge(tmp_path, 0, _mutated(mutate))
    assert judgement.status == "fail"
    assert any(problem.startswith(field_name) for problem in judgement.problems), judgement.problems


# P2-2: outer deadline 到達時は SIGTERM（runner の finally cleanup）→ bounded grace → 所有 group の SIGKILL。


def _fake_runner(source: str, *args: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(source), *args]


_FAKE_NORMAL = """\
import sys
print("normal-out", flush=True)
sys.stderr.write("normal-err\\n")
"""

_FAKE_TERM_CLEANUP = """\
import signal, sys, time
marker = sys.argv[1]


def handler(signum, frame):
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("cleaned-up")
    sys.exit(0)


signal.signal(signal.SIGTERM, handler)
print("started-before-hang", flush=True)
time.sleep(60)
"""

_FAKE_TERM_IGNORED = """\
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(
    [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"]
)
print(os.getpid(), child.pid, flush=True)
time.sleep(60)
"""


def _group_gone(pgid: int, *, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_ac4_a_runner_that_finishes_before_the_deadline_is_returned_unchanged() -> None:
    execution = run_runner_bounded(_fake_runner(_FAKE_NORMAL), timeout=30, term_grace=1, reap_wait=1)

    assert execution == RunnerExecution(0, "normal-out\n", "normal-err\n")


def test_ac4_timeout_sends_sigterm_first_so_the_runner_can_run_its_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "cleanup-marker.txt"
    execution = run_runner_bounded(_fake_runner(_FAKE_TERM_CLEANUP, str(marker)), timeout=3, term_grace=20, reap_wait=1)

    assert execution.timed_out and not execution.sigkill_escalated
    assert execution.returncode == 0
    assert marker.read_text(encoding="utf-8") == "cleaned-up"
    assert "started-before-hang" in execution.stdout


def test_ac4_a_runner_ignoring_sigterm_is_killed_after_the_grace_with_its_owned_group_reaped() -> None:
    started = time.monotonic()
    execution = run_runner_bounded(_fake_runner(_FAKE_TERM_IGNORED), timeout=3, term_grace=1, reap_wait=2)
    elapsed = time.monotonic() - started

    assert execution.timed_out and execution.sigkill_escalated
    assert execution.returncode == -signal.SIGKILL
    assert elapsed < 30, "must not wait unboundedly for EOF held by a descendant"
    runner_pid, grandchild_pid = (int(token) for token in execution.stdout.split())
    assert _group_gone(runner_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_ac4_a_timeout_is_never_a_pass_even_when_the_evidence_json_claims_discriminative(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_valid_summary()), encoding="utf-8")
    assert evaluate_counterfactual_result(0, evidence, tested_head="a" * 40) == Judgement("pass", [])

    for escalated in (False, True):
        execution = RunnerExecution(0, "out", "err", timed_out=True, sigkill_escalated=escalated, timeout_seconds=5)
        judgement = judge_runner_execution(execution, evidence, tested_head="a" * 40)
        assert judgement.status == "fail"
        assert any("outer deadline" in problem for problem in judgement.problems)
        assert any("not confirmed" in problem for problem in judgement.problems) is escalated


def test_ac4_a_runner_that_finishes_in_time_is_judged_by_its_exit_code_and_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_valid_summary()), encoding="utf-8")

    assert judge_runner_execution(RunnerExecution(0), evidence, tested_head="a" * 40).status == "pass"
    assert judge_runner_execution(RunnerExecution(1), evidence, tested_head="a" * 40).status == "fail"


def test_ac4_timeout_diagnostics_keep_the_bounded_output_tails_in_the_runtime_verification_log(tmp_path: Path) -> None:
    paths = allocate_run_paths(tmp_path / "runs")
    marker = tmp_path / "cleanup-marker.txt"
    execution = run_runner_bounded(_fake_runner(_FAKE_TERM_CLEANUP, str(marker)), timeout=3, term_grace=20, reap_wait=1)
    judgement = judge_runner_execution(execution, paths.evidence_json, tested_head="a" * 40)

    log = write_runtime_verification_log(judgement, paths, "a" * 40, execution, log_dir=tmp_path / "logs")

    text = log.read_text(encoding="utf-8")
    assert judgement.status == "fail"
    assert "Result: FAIL" in text and "timed_out=True" in text and "started-before-hang" in text
    assert "outer deadline" in text
    long_tail = RunnerExecution(1, "x" * (DIAGNOSTIC_TAIL_CHARS * 3), "")
    second = write_runtime_verification_log(
        Judgement("fail", ["x"]), allocate_run_paths(tmp_path / "runs"), "a" * 40, long_tail, log_dir=tmp_path / "logs"
    )
    assert second.read_text(encoding="utf-8").count("x") < DIAGNOSTIC_TAIL_CHARS * 2


# P3: runtime verification log の名前は run 固有 token を含み、同一秒でも衝突しない。


def test_p3_logs_of_runs_started_in_the_same_second_get_distinct_paths_and_never_overwrite(tmp_path: Path) -> None:
    frozen = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    first = allocate_run_paths(tmp_path / "runs", token_factory=lambda: "tokenaaa", clock=lambda: frozen)
    second = allocate_run_paths(tmp_path / "runs", token_factory=lambda: "tokenbbb", clock=lambda: frozen)
    log_dir = tmp_path / "logs"
    fail = Judgement("fail", ["x"])

    first_log = write_runtime_verification_log(fail, first, "a" * 40, RunnerExecution(1), log_dir=log_dir)
    second_log = write_runtime_verification_log(fail, second, "a" * 40, RunnerExecution(1), log_dir=log_dir)

    assert first_log != second_log and first_log.is_file() and second_log.is_file()
    assert "tokenaaa" in first_log.name and "tokenbbb" in second_log.name
    first_text = first_log.read_text(encoding="utf-8")
    assert _display_path(first.evidence_json) in first_text and _display_path(second.evidence_json) not in first_text

    with pytest.raises(FileExistsError):
        write_runtime_verification_log(fail, first, "b" * 40, RunnerExecution(0), log_dir=log_dir)
    with pytest.raises(FileExistsError):
        allocate_run_paths(tmp_path / "runs", token_factory=lambda: "tokenaaa", clock=lambda: frozen)
    assert first_log.read_text(encoding="utf-8") == first_text
