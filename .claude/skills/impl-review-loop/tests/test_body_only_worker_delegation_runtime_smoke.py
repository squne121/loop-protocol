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
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
RUNNER = ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
PLAN_MODULE_PATH = SKILL_DIR / "scripts" / "body_only_repair_plan.py"
PROMPT_TEMPLATE = SKILL_DIR / "tests" / "fixtures" / "body_only_worker_delegation_runtime_smoke_prompt.md"
OUTPUT_DIR = "artifacts/runtime-smoke/issue-2971-worker-delegation"
INPUT_PARENT = ROOT / "artifacts" / "runtime-smoke"
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


def runner_argv(prompt_file: Path, evidence_json: Path) -> list[str]:
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
        str(ROOT),
        "--prompt-file",
        str(prompt_file),
        "--output-dir",
        OUTPUT_DIR,
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
    argv = runner_argv(Path("/p/prompt.md"), Path("/p/evidence.json"))
    help_text = subprocess.run(
        [sys.executable, str(RUNNER), "--help"], capture_output=True, text=True, check=False, timeout=60
    ).stdout

    for option in {token for token in argv if token.startswith("--")}:
        assert option in help_text, option
    assert argv[argv.index("--expect-marker-source") + 1] == "subagent"
    assert argv[argv.index("--require-min-subagents") + 1] == "1"
    assert argv[argv.index("--expect-marker") + 1] == MARKER


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

    INPUT_PARENT.mkdir(parents=True, exist_ok=True)
    input_dir = Path(tempfile.mkdtemp(prefix="issue-2971-worker-delegation-input-", dir=str(INPUT_PARENT)))
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
        evidence_json = input_dir / "evidence.json"

        # runner は --output-dir の exclusive create を要求する。前回実行の同名 artifact（git-ignored）だけを消す。
        shutil.rmtree(ROOT / OUTPUT_DIR, ignore_errors=True)
        result = subprocess.run(
            runner_argv(prompt_file, evidence_json),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )

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
        summary = ROOT / OUTPUT_DIR / "summary.md"
        assert summary.is_file(), f"expected persisted evidence at {summary}"
        assert summary.read_text(encoding="utf-8").strip()
        assert evidence_json.is_file(), "runner did not write the evidence json"
    finally:
        shutil.rmtree(input_dir, ignore_errors=True)
