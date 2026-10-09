"""Issue #2971 AC10: implementation-worker の body-only hygiene pre-mutation guard の delegation smoke。

既存の ``worktree-agent-runtime-smoke`` runner を subprocess で起動し、実 ``implementation-worker``
SubAgent へ ``IMPLEMENTATION_WORKER_REQUEST_V2``（``update_pr_body_hygiene``。body file は現 branch の
PR の live body と同一内容、``expected_live_body_sha256`` は故意に不一致）を delegation して、worker が
overwrite せず ``blocked`` / ``live_body_hash_mismatch`` を返すこと、および delegation の前後で PR body が
byte 同一であること（overwrite されていないこと）を確認する。

prompt は実行時に生成する（静的 fixture は雛形だけ）。``claude_live`` marker 付きなので既定 addopts では
deselect され、``-m claude_live`` を明示した場合だけ実行される。runner の exit 0 のみを PASS とし、
exit 77（capability unavailable）と ``gh`` / PR を取得できない場合は **skip ではなく fail** として扱う
（runtime AC の PASS を主張しない）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
REAL_ROOT = ROOT  # hermetic fixture が ROOT を差し替えても、実 repo root を識別するための不変値
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
PLAN_MODULE_PATH = SKILL_DIR / "scripts" / "body_only_repair_plan.py"
PROMPT_TEMPLATE = SKILL_DIR / "tests" / "fixtures" / "body_only_worker_delegation_runtime_smoke_prompt.md"
OUTPUT_PARENT = "artifacts/runtime-smoke"
OUTPUT_DIR_PREFIX = "issue-2971-worker-delegation"
INPUT_DIR_PREFIX = "issue-2971-worker-delegation-input-"
MARKER = "live_body_hash_mismatch"
ISSUE_NUMBER = 2971
EXIT_CAPABILITY_UNAVAILABLE = 77
PLACEHOLDERS = frozenset(
    {
        "PR_NUMBER",
        "ISSUE_NUMBER",
        "EXPECTED_HEAD_SHA",
        "BODY_FILE_PATH",
        "BODY_FILE_SHA256",
        "EXPECTED_LIVE_BODY_SHA256",
    }
)

# 同名 module との sys.modules 衝突を避けるため、一意名で spec_from_file_location 経由の読み込みを行う。
_MODULE_NAME = "body_only_repair_plan_worker_delegation_smoke_issue_2971"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, PLAN_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
plan_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = plan_module
_spec.loader.exec_module(plan_module)


def render_prompt(template: str, values: dict[str, str]) -> str:
    """``@@NAME@@`` placeholder を実値へ置換する。未知・未置換の placeholder は拒否する。"""
    assert set(values) == PLACEHOLDERS, sorted(set(values) ^ PLACEHOLDERS)
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace(f"@@{name}@@", value)
    leftover = re.findall(r"@@[A-Z0-9_]+@@", rendered)
    assert not leftover, leftover
    return rendered


def stale_live_body_sha256(live_body: str) -> str:
    """live body の hash と必ず異なる（故意に不一致の）expected_live_body_sha256。"""
    value = plan_module.body_sha256("stale-expected-live-body-issue-2971\n" + live_body)
    assert value != plan_module.body_sha256(live_body)
    return value


def unique_output_dir() -> str:
    """呼出しごとに固有の ``--output-dir`` 相対 path（UTC timestamp + UUID）を返す。

    runner は ``--output-dir`` の exclusive create を要求するため、directory はここでは作らない
    （``mkdir`` / ``tempfile.mkdtemp`` を使わない）。過去 run の evidence を削除・再利用しない。
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{OUTPUT_PARENT}/{OUTPUT_DIR_PREFIX}-{stamp}-{uuid.uuid4().hex}"


def runner_argv(prompt_file: Path, evidence_json: Path, output_dir: str, root: Path | None = None) -> list[str]:
    """runner の argv。``--worktree`` は ``root``（省略時は呼出し時点の module global ``ROOT``）に束縛する。"""
    worktree = ROOT if root is None else root
    return [
        sys.executable,
        str(RUNNER),
        "--runtime",
        "claude",
        "--mode",
        "structured",
        "--claude-adapter",
        "native",
        "--worktree",
        str(worktree),
        "--prompt-file",
        str(prompt_file),
        "--output-dir",
        output_dir,
        "--evidence-json",
        str(evidence_json),
        "--timeout-seconds",
        "600",
        "--max-turns",
        "30",
        "--expect-marker-source",
        "subagent",
        "--require-min-subagents",
        "1",
        "--expect-marker",
        MARKER,
    ]


@dataclass(frozen=True)
class DelegationRun:
    """``run_worker_delegation_smoke`` の結果。summary / evidence は実行単位の終了後に読み出した値。"""

    result: subprocess.CompletedProcess[str]
    output_dir: str
    input_dir: Path
    summary: Path
    summary_text: str | None
    evidence_json: Path
    evidence_exists: bool


def run_worker_delegation_smoke(
    pr_number: int,
    head: str,
    live_body: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    root: Path = ROOT,
) -> DelegationRun:
    """gh / claude を伴わない部分: input_dir 作成 -> runner 起動 -> input_dir cleanup -> summary / evidence 確認。

    live test と hermetic test が同じ実行単位を呼ぶ。``run`` は runner 起動点（``subprocess.run``）の差し替え口。
    output dir は run 固有で一度だけ生成し、runner の ``--output-dir`` / ``--evidence-json`` / summary の
    読み出しに共通使用する。output dir は作らず・削除せず、cleanup するのは一時入力 ``input_dir`` だけ。
    """
    input_parent = root / OUTPUT_PARENT
    input_parent.mkdir(parents=True, exist_ok=True)
    input_dir = Path(tempfile.mkdtemp(prefix=INPUT_DIR_PREFIX, dir=str(input_parent)))
    output_dir = unique_output_dir()
    try:
        # body file は PR の現 body と同一内容（guard が無くても mutation が冪等になる）。
        body_file = input_dir / "body.md"
        body_file.write_bytes(live_body.encode("utf-8"))
        prompt_file = input_dir / "prompt.md"
        prompt_file.write_text(
            render_prompt(
                PROMPT_TEMPLATE.read_text(encoding="utf-8"),
                {
                    "PR_NUMBER": str(pr_number),
                    "ISSUE_NUMBER": str(ISSUE_NUMBER),
                    "EXPECTED_HEAD_SHA": head,
                    "BODY_FILE_PATH": str(body_file),
                    "BODY_FILE_SHA256": plan_module.body_sha256(live_body),
                    "EXPECTED_LIVE_BODY_SHA256": stale_live_body_sha256(live_body),
                },
            ),
            encoding="utf-8",
        )
        # 保持する証跡（summary / evidence.json）は run 固有の output dir 配下。input_dir は一時入力のみ。
        evidence_json = root / output_dir / "evidence.json"
        result = run(
            runner_argv(prompt_file, evidence_json, output_dir, root),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
    finally:
        shutil.rmtree(input_dir, ignore_errors=True)
    summary = root / output_dir / "summary.md"
    return DelegationRun(
        result=result,
        output_dir=output_dir,
        input_dir=input_dir,
        summary=summary,
        summary_text=summary.read_text(encoding="utf-8") if summary.is_file() else None,
        evidence_json=evidence_json,
        evidence_exists=evidence_json.is_file(),
    )


# --- deterministic (non-live) checks of the template ---------------------------------------


def test_worker_delegation_prompt_template_has_exactly_the_expected_placeholders() -> None:
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")

    assert set(re.findall(r"@@([A-Z0-9_]+)@@", template)) == PLACEHOLDERS
    assert 'subagent_type: "implementation-worker"' in template
    assert "mode: update_pr_body_hygiene" in template
    for field in (
        "body_file_path",
        "body_file_sha256",
        "expected_live_body_sha256",
        "expected_head_sha",
        "issue_number",
    ):
        assert re.search(rf"^  {field}: @@", template, re.M), field


def test_worker_delegation_prompt_never_contains_the_expected_marker() -> None:
    """marker は worker の出力にだけ現れる。prompt（雛形・生成後）に marker を書いて満たさない。"""
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    rendered = render_prompt(
        template,
        {
            "PR_NUMBER": "1",
            "ISSUE_NUMBER": str(ISSUE_NUMBER),
            "EXPECTED_HEAD_SHA": "a" * 40,
            "BODY_FILE_PATH": "/x/body.md",
            "BODY_FILE_SHA256": "b" * 64,
            "EXPECTED_LIVE_BODY_SHA256": "c" * 64,
        },
    )

    assert MARKER not in template and MARKER not in rendered
    assert "gh pr edit" in rendered  # 直接呼出しの禁止を指示している
    assert "Never call gh pr edit directly" in rendered


def test_worker_delegation_stale_hash_is_deliberately_different_from_the_live_body_hash() -> None:
    body = "## 概要\r\n本文\r\n"

    assert stale_live_body_sha256(body) != plan_module.body_sha256(body)
    # canonicalization の差（CRLF / 末尾改行）だけでは別 hash にならない（= 故意の不一致は内容の差による）。
    assert plan_module.body_sha256(body) == plan_module.body_sha256("## 概要\n本文")


def test_worker_delegation_runner_argv_uses_only_existing_runner_options() -> None:
    argv = runner_argv(Path("/p/prompt.md"), Path("/p/evidence.json"), unique_output_dir())
    help_text = subprocess.run(
        [sys.executable, str(RUNNER), "--help"], capture_output=True, text=True, check=False, timeout=60
    ).stdout

    for option in {token for token in argv if token.startswith("--")}:
        assert option in help_text, option
    assert argv[argv.index("--expect-marker-source") + 1] == "subagent"
    assert argv[argv.index("--require-min-subagents") + 1] == "1"
    assert argv[argv.index("--expect-marker") + 1] == MARKER


def _arg_of(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """hermetic test 用: module global ``ROOT`` を ``tmp_path`` へ差し替える（autouse ではない）。

    live test の root semantics（実 repo root）は変えない。``run_worker_delegation_smoke`` の ``root`` 既定値は
    定義時に評価されるため、呼出し側は同じ ``tmp_path`` を ``root=`` へ明示的に渡すこと。
    """
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert ROOT == tmp_path and tmp_path.resolve() != REAL_ROOT
    return tmp_path


class _FakeRunner:
    """runner 起動点の fake。実 runner と同様に ``--worktree`` 基準で相対 ``--output-dir`` を解決し、
    exclusive create を模擬する（output dir が既存なら失敗）。``evidence.json`` も output dir 配下へ書く。"""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.worktrees: list[str] = []
        self.cwds: list[str] = []
        self.existed_at_launch: list[bool] = []
        self.input_dir_existed_at_launch: list[bool] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        worktree = Path(_arg_of(argv, "--worktree"))
        output_dir = _arg_of(argv, "--output-dir")
        assert Path(kwargs["cwd"]) == worktree, (kwargs["cwd"], str(worktree))  # cwd == --worktree
        assert not Path(output_dir).is_absolute(), output_dir
        target = worktree / output_dir  # 実 runner: Path(worktree) / output_dir
        evidence = Path(_arg_of(argv, "--evidence-json"))
        assert evidence == target / "evidence.json", (str(evidence), str(target))  # 同 output dir 配下を指す
        self.argvs.append(argv)
        self.worktrees.append(str(worktree))
        self.cwds.append(str(kwargs["cwd"]))
        self.existed_at_launch.append(target.exists())
        self.input_dir_existed_at_launch.append(Path(_arg_of(argv, "--prompt-file")).is_file())
        target.mkdir(parents=True)  # 既存なら FileExistsError（exclusive create の模擬）
        (target / "summary.md").write_text(f"summary for {output_dir}\n", encoding="utf-8")
        evidence.write_text('{"ok": true}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="OK\n", stderr="")


def _delegate(root: Path, fake: Callable[..., subprocess.CompletedProcess[str]]) -> DelegationRun:
    return run_worker_delegation_smoke(7, "a" * 40, "## 概要\n本文\n", run=fake, root=root)


def _assert_sentinels_preserved(sentinels: dict[Path, str]) -> None:
    for path, content in sentinels.items():
        assert path.is_file(), f"sentinel lost: {path}"
        assert path.read_text(encoding="utf-8") == content, f"sentinel content changed: {path}"


def _seed_sentinels(root: Path) -> dict[Path, str]:
    legacy = root / "artifacts" / "runtime-smoke" / "issue-2971-worker-delegation"
    past_run = root / "artifacts" / "runtime-smoke" / f"{OUTPUT_DIR_PREFIX}-20200101T000000Z-{'0' * 32}"
    sentinels = {legacy / "sentinel.txt": "legacy evidence", past_run / "sentinel.txt": "past run evidence"}
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    return sentinels


def test_unique_output_dir_worker_delegation_is_fresh_and_not_created() -> None:
    paths = [unique_output_dir() for _ in range(5)]

    assert len(set(paths)) == len(paths)
    for path in paths:
        assert path.startswith("artifacts/runtime-smoke/"), path
        assert path.split("/")[-1].startswith("issue-2971-worker-delegation-"), path
        assert not (ROOT / path).exists(), path  # 関数は directory を作らない


def test_unique_output_dir_worker_delegation_invocations_share_path_between_argv_and_summary(
    sandbox_root: Path,
) -> None:
    fake = _FakeRunner()

    first = _delegate(sandbox_root, fake)
    second = _delegate(sandbox_root, fake)

    assert first.output_dir != second.output_dir
    assert [_arg_of(argv, "--output-dir") for argv in fake.argvs] == [first.output_dir, second.output_dir]
    assert fake.existed_at_launch == [False, False]  # runner 起動時点で未存在
    assert fake.input_dir_existed_at_launch == [True, True]  # 一時入力は runner 起動時点で存在
    # runner_argv の --worktree == subprocess cwd == sentinel 配置 root == summary / evidence 読み出し root
    assert fake.worktrees == fake.cwds == [str(sandbox_root)] * 2
    for run_result in (first, second):
        assert run_result.summary == sandbox_root / run_result.output_dir / "summary.md"
        assert run_result.summary.is_file()  # 解決済み output dir 配下に作られる
        assert run_result.summary_text == f"summary for {run_result.output_dir}\n"
        assert run_result.evidence_json == sandbox_root / run_result.output_dir / "evidence.json"
        assert run_result.result.returncode == 0


def test_unique_output_dir_worker_delegation_preserves_existing_evidence(sandbox_root: Path) -> None:
    sentinels = _seed_sentinels(sandbox_root)
    fake = _FakeRunner()

    delegation = _delegate(sandbox_root, fake)

    assert fake.worktrees == fake.cwds == [str(sandbox_root)]  # sentinel と同じ root で起動された
    assert delegation.summary_text is not None and delegation.evidence_exists
    _assert_sentinels_preserved(sentinels)


def test_unique_output_dir_worker_delegation_keeps_evidence_json_after_input_cleanup(sandbox_root: Path) -> None:
    fake = _FakeRunner()

    run_result = _delegate(sandbox_root, fake)

    [argv] = fake.argvs
    evidence_arg = Path(_arg_of(argv, "--evidence-json"))
    prompt_file = Path(_arg_of(argv, "--prompt-file"))
    assert evidence_arg == sandbox_root / run_result.output_dir / "evidence.json"
    assert evidence_arg.parent == sandbox_root / _arg_of(argv, "--output-dir")
    assert prompt_file.parent == run_result.input_dir
    assert not run_result.input_dir.exists()  # 一時入力は cleanup 済み
    assert evidence_arg.is_file() and run_result.evidence_exists  # 証跡は残る
    assert run_result.summary.is_file()


def test_unique_output_dir_worker_delegation_destructive_mutation_is_detected_by_sentinel_loss(
    sandbox_root: Path,
) -> None:
    """negative control: 起動前に sandbox root 基準で OUTPUT_PARENT / 旧固定 dir を削除する mutation を入れると、
    保全 assertion が sentinel 喪失で FAIL する（fake の起動不能による FAIL とは区別する）。"""
    sentinels = _seed_sentinels(sandbox_root)
    fake = _FakeRunner()

    def destructive_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        worktree = Path(_arg_of(argv, "--worktree"))
        assert worktree.resolve() != REAL_ROOT and worktree == sandbox_root  # 実 repo の artifacts/ は消さない
        shutil.rmtree(worktree / OUTPUT_PARENT, ignore_errors=True)  # mutation（sandbox root のみ）
        shutil.rmtree(worktree / "artifacts" / "runtime-smoke" / "issue-2971-worker-delegation", ignore_errors=True)
        return fake(argv, **kwargs)

    delegation = _delegate(sandbox_root, destructive_run)

    # fake は正常に起動・完走している（FAIL 理由が起動不能ではないことの証明）
    assert delegation.result.returncode == 0 and delegation.summary_text is not None and delegation.evidence_exists
    assert fake.existed_at_launch == [False]
    # sentinel は実在しない -> 保全 assertion は sentinel 喪失で FAIL する
    assert not any(path.exists() for path in sentinels)
    with pytest.raises(AssertionError, match="sentinel lost"):
        _assert_sentinels_preserved(sentinels)


# --- live (claude_live) ----------------------------------------------------------------------


def _gh_pr_view(fields: str) -> dict[str, Any]:
    """現 branch の PR を ``gh pr view`` で取得する。取得できなければ PASS を主張せず fail（exit 77 相当）。"""
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", "--json", fields],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.fail(
            f"capability unavailable (exit 77 equivalent): gh is not usable ({type(exc).__name__}); AC10 is unverified"
        )
    if completed.returncode != 0:
        pytest.fail(
            "capability unavailable (exit 77 equivalent): the current branch's PR could not be read with gh; "
            f"AC10 is unverified.\nstderr={completed.stderr[-1000:]}"
        )
    return json.loads(completed.stdout)


@pytest.mark.claude_live
def test_ac10_worker_delegation_rejects_a_stale_live_body_hash_and_leaves_the_pr_body_byte_identical() -> None:
    assert RUNNER.is_file()
    before = _gh_pr_view("number,headRefOid,body,state")
    assert before["state"] == "OPEN"
    pr_number, head, live_body = before["number"], before["headRefOid"], before["body"]
    assert isinstance(live_body, str) and live_body

    # output dir は run 固有（runner 起動前に未存在）。過去 run の evidence は削除・再利用しない。
    # input_dir（body.md / prompt.md の一時入力）だけが実行単位の中で cleanup される。
    delegation = run_worker_delegation_smoke(pr_number, head, live_body)
    result = delegation.result

    after = _gh_pr_view("number,headRefOid,body")
    # 最優先: delegation の前後で PR body が byte 同一（overwrite されていない）で、head も不変。
    assert after["body"].encode("utf-8") == live_body.encode("utf-8"), "PR body changed across the delegation"
    assert after["headRefOid"] == head

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
    assert delegation.summary_text is not None, f"expected persisted evidence at {delegation.summary}"
    assert delegation.summary_text.strip()
    assert delegation.evidence_exists, "runner did not write the evidence json"
