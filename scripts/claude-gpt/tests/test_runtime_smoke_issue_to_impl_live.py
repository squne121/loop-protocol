"""scripts/claude-gpt/tests/test_runtime_smoke_issue_to_impl_live.py

Issue #2925 AC5 (Runtime Verification Applicability: `decision: immediate`).

Repository workflow smoke: Minimal Claude-GPT（`scripts/claude-gpt/launch.sh` の default path）から
representative な `issue-refinement-loop` を current canonical entry で実行し、次まで到達することを
実 Claude Code process の stream-json で確認する。

  - GitHub read（native `gh` 経由。launcher は認証 carrier を再注入しない）
  - planner / preflight（`plan_refinement_loop.py`）
  - 起動された SubAgent の completion（Start だけで Stop の無い dispatch-only を許さない）
  - controlled Issue mutation（`edit_issue_txn.py`）または canonical termination publish
    （`publish_termination_report.py`）

repository contract 由来の `human_judgment_required` 等の terminal reason は launcher FAIL としない
（terminal reason は証跡に記録するだけ）。ただしこの AC の PASS は通信・報告経路が機能している
ことの確認に限り、実用性の PASS ではない（実用性は AC7 の merge 後 trial で判定する）。

実行条件（満たさなければ SKIP: exit 77。SKIP は PASS ではない）:
  - 実 `claude` と、`ANTHROPIC_BASE_URL` の接続先で動いている互換 claude-code-proxy
  - 認証済みの `gh`
  - 対象 Issue 番号: 環境変数 `CLAUDE_GPT_AC5_ISSUE_NUMBER`（既定 2889。OPEN の Issue のみ）。この
    テストは対象 Issue へ termination report 等を実際に投稿しうる live 検証である。

`evaluate_workflow_stream` は pure で、実 process を起動しない（下の hermetic test が positive /
negative control を固定する）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent.parent.parent
_RUNNER_PATH = _REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
_LAUNCHER = _REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
_REPO = "squne121/loop-protocol"
_DEFAULT_ISSUE = "2889"

_PLANNER_MARKER = "plan_refinement_loop.py"
_TERMINAL_MARKERS = ("publish_termination_report.py", "edit_issue_txn.py")
_GITHUB_READ_RE = re.compile(r"\bgh\s+(issue|api|repo|pr)\b|run_refinement_preflight\.py|plan_refinement_loop\.py")


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_2925_ac5", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _events(stdout: str):
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def evaluate_workflow_stream(stdout: str, hook_events: list[dict]) -> dict:
    """Judge the issue-refinement-loop run from structured events only."""
    bash_commands: list[str] = []
    tool_use_ids: dict[str, str] = {}
    errored_results = 0
    final_text = ""
    result_is_error = None
    for event in _events(stdout):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str):
                    bash_commands.append(command)
                    if block.get("id"):
                        tool_use_ids[block["id"]] = command
            elif block.get("type") == "tool_result" and block.get("is_error"):
                errored_results += 1
        if event.get("type") == "result":
            final_text = str(event.get("result") or "")
            result_is_error = bool(event.get("is_error"))

    joined = "\n".join(bash_commands)
    starts = {e.get("agent_id") for e in hook_events if e.get("hook_event") == "SubagentStart" and e.get("agent_id")}
    stops = {e.get("agent_id") for e in hook_events if e.get("hook_event") == "SubagentStop" and e.get("agent_id")}
    terminal = next((m for m in _TERMINAL_MARKERS if m in joined), None)
    reason_match = re.search(
        r"\b(human_judgment_required|approved|needs_fix|max_iterations_reached|scope_change|blocked)\b", final_text
    )
    summary = {
        "github_read": bool(_GITHUB_READ_RE.search(joined)),
        "planner_ran": _PLANNER_MARKER in joined,
        "subagents_started": len(starts),
        "subagents_completed": len(starts & stops),
        "dispatch_only_subagents": sorted(a for a in starts - stops if a),
        "terminal_step": terminal,
        "canonical_terminal_reason": reason_match.group(1) if reason_match else None,
        "result_is_error": result_is_error,
        "errored_tool_results": errored_results,
    }
    summary["ok"] = bool(
        summary["github_read"]
        and summary["planner_ran"]
        and summary["terminal_step"]
        and not summary["dispatch_only_subagents"]
        and result_is_error is False
    )
    return summary


# ---------------------------------------------------------------------------
# hermetic controls for the evaluator (always run; no live process)
# ---------------------------------------------------------------------------


def _bash(command: str, tool_id: str) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]},
    })


def _stream(commands, *, is_error=False, final="terminal reason: human_judgment_required"):
    lines = [_bash(cmd, f"toolu_{i}") for i, cmd in enumerate(commands)]
    lines.append(json.dumps({"type": "result", "is_error": is_error, "result": final}))
    return "\n".join(lines) + "\n"


_FULL = [
    "gh issue view 2889 --repo squne121/loop-protocol --json title,body",
    "uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py --issue-number 2889",
    "uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py "
    "--input-file x.json",
]


def test_workflow_evaluator_accepts_a_run_that_reaches_a_canonical_terminal_step():
    result = evaluate_workflow_stream(_stream(_FULL), [])
    assert result["ok"] is True, result
    # repository contract 由来の human_judgment_required は launcher FAIL ではない。
    assert result["canonical_terminal_reason"] == "human_judgment_required"
    assert result["terminal_step"] == "publish_termination_report.py"


@pytest.mark.parametrize(
    "label, commands, hook_events, is_error",
    [
        ("no_terminal_step", _FULL[:2], [], False),
        ("no_planner", [_FULL[0], _FULL[2]], [], False),
        ("no_github_read", ["echo hello", "echo again"], [], False),
        ("claude_result_is_error", _FULL, [], True),
        ("dispatch_only_subagent", _FULL,
         [{"hook_event": "SubagentStart", "agent_id": "a1"}], False),
    ],
)
def test_workflow_evaluator_rejects_runs_that_stop_short(label, commands, hook_events, is_error):
    result = evaluate_workflow_stream(_stream(commands, is_error=is_error), hook_events)
    assert result["ok"] is False, (label, result)


def test_workflow_evaluator_accepts_completed_subagents():
    events = [
        {"hook_event": "SubagentStart", "agent_id": "a1"},
        {"hook_event": "SubagentStop", "agent_id": "a1"},
    ]
    result = evaluate_workflow_stream(_stream(_FULL), events)
    assert result["ok"] is True and result["subagents_completed"] == 1


# ---------------------------------------------------------------------------
# live (Runtime Verification: immediate)
# ---------------------------------------------------------------------------


def _skip(reason: str):
    print(f"SKIP: {reason}")
    pytest.exit(f"SKIP: runtime_smoke_issue_to_impl_live unavailable ({reason}); never a PASS", returncode=77)


def _write_artifact(payload: dict) -> Path:
    artifact_dir = Path(os.environ.get("RUNTIME_VERIFICATION_ARTIFACT_DIR", "artifacts"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / (
        "runtime-verification-2925-AC5-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return artifact


@pytest.mark.claude_live
def test_live_issue_refinement_loop_through_minimal_claude_gpt():
    if os.environ.get("CI"):
        pytest.skip("RUNTIME_VERIFICATION_SKIPPED_NOT_PASS: live Claude-GPT workflow smoke is not run in CI")
    if shutil.which("gh") is None:
        _skip("gh CLI unavailable")
    if subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30).returncode != 0:
        _skip("gh is not authenticated")
    issue_number = os.environ.get("CLAUDE_GPT_AC5_ISSUE_NUMBER", _DEFAULT_ISSUE)
    view = subprocess.run(
        ["gh", "issue", "view", issue_number, "--repo", _REPO, "--json", "state", "-q", ".state"],
        capture_output=True, text=True, timeout=60,
    )
    if view.returncode != 0 or view.stdout.strip() != "OPEN":
        _skip(f"representative Issue #{issue_number} is not an OPEN Issue")
    check = subprocess.run(
        ["sh", str(_LAUNCHER), "--check-only"], capture_output=True, text=True, timeout=60, cwd=str(_REPO_ROOT)
    )
    if check.returncode != 0:
        _skip("connected claude-code-proxy unavailable or its model catalog is incomplete")

    runner = _load_runner()
    prompt = (
        "Run the repository's issue-refinement-loop skill for Issue "
        f"#{issue_number} in {_REPO} using the current canonical entry (max_iterations: 1). Follow the "
        "skill's documented procedure autonomously up to its documented terminal boundary, including the "
        "canonical termination publish. If the repository workflow itself stops with "
        "human_judgment_required, report that terminal reason; do not work around the stop."
    )
    rc, out, err, timed_out = runner.run_structured_claude(
        str(_REPO_ROOT), prompt, 1800.0, 120, claude_bin=str(_LAUNCHER), claude_adapter="claude-gpt"
    )
    hook_events = runner.extract_claude_hook_lifecycle_events(out)
    summary = evaluate_workflow_stream(out, hook_events)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(_REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout.strip()
    artifact = _write_artifact({
        "ac": "AC5",
        "command": "pytest -m claude_live scripts/claude-gpt/tests/test_runtime_smoke_issue_to_impl_live.py",
        "tested_head": head,
        "issue_number": int(issue_number),
        "claude_exit_code": rc,
        "timed_out": timed_out,
        "workflow": summary,
        "launcher_receipt": runner.extract_claude_gpt_launcher_receipt(err),
        "scope_note": "AC5 PASS confirms the communication/reporting path only; practical usability is judged by AC7.",
    })
    assert not timed_out, f"timed out; artifact={artifact}"
    assert rc == 0, f"claude exit={rc}; artifact={artifact}; stderr tail={err[-600:]}"
    assert summary["ok"], f"{summary}; artifact={artifact}"
