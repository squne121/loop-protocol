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

workflow を実行する場所（claude の cwd）は canonical main root checkout（`git rev-parse
--git-common-dir` から解決）でなければならない。canonical `preflight.run` は「canonical main root /
default branch」を要求し、#2925 の isolation worktree を cwd にすると `exact command class rejected`
で planner / SubAgent が一度も起動しないため。起動する launcher は引き続き PR worktree 側
（SUT）の `scripts/claude-gpt/launch.sh` を絶対 path で指定する。canonical main root が解決できない・
存在しない・default branch 上にない場合は SKIP（exit 77。PASS ではない）。

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


def _git(root: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else None


def resolve_canonical_main_root(sut_root: Path) -> tuple[Path | None, str | None]:
    """Resolve the canonical main root checkout from any checkout/worktree of the repository.

    Returns ``(root, None)`` when the main root exists and is on the repository default branch,
    else ``(None, reason)`` (the caller SKIPs with exit 77; never a PASS)."""
    common = _git(sut_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None, "git common dir of the SUT checkout could not be resolved"
    common_dir = Path(common)
    if common_dir.name != ".git":
        return None, f"git common dir {common_dir} is not a `.git` directory (bare repository?)"
    root = common_dir.parent
    if not root.is_dir():
        return None, f"canonical main root {root} does not exist"
    if _git(root, "rev-parse", "--is-inside-work-tree") != "true":
        return None, f"canonical main root {root} is not a work tree"
    default = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main"
    default_branch = default.split("/", 1)[1] if default.startswith("origin/") else default
    current = _git(root, "branch", "--show-current")
    if current != default_branch:
        return None, (
            f"canonical main root {root} is on {current or 'a detached HEAD'}, not the default branch {default_branch}"
        )
    return root, None


_PREFLIGHT_PATH = _REPO_ROOT / "scripts" / "claude-gpt" / "workflow_capability_preflight.py"
_PROMPT_OPERATION_EXAMPLES = ("issue_edit", "issue_comment")


def build_workflow_prompt(issue_number: str) -> str:
    # issue-refinement-loop の skill docs は、preflight.run に渡す LOOP_PLANNED_OPERATIONS_JSON の
    # operation 名の語彙（workflow_capability_preflight.py の _KNOWN_OPERATION_ROUTES）を記載していない。
    # model が未登録の名前（例: controlled_issue_edit）を推測で作ると preflight が
    # `operation_route_unavailable` で blocked になり、planner / SubAgent に到達しない。これは repository
    # workflow 側の documentation gap であり launcher の欠陥ではない。この smoke は launcher 経路の通信・報告
    # 到達を測るものなので、model の推測という交絡要因を下の一文で除く（docs gap は follow-up Issue で追跡する。
    # Issue 番号は PR 本文に記載）。評価基準（planner_ran / SubAgent completion / terminal step）は緩めない。
    return (
        "Run the repository's issue-refinement-loop skill for Issue "
        f"#{issue_number} in {_REPO} using the current canonical entry (max_iterations: 1). Follow the "
        "skill's documented procedure autonomously up to its documented terminal boundary, including the "
        "canonical termination publish. If the repository workflow itself stops with "
        "human_judgment_required, report that terminal reason; do not work around the stop. "
        "Note: when you build LOOP_PLANNED_OPERATIONS_JSON for preflight.run, each operation value must be a "
        "name registered in _KNOWN_OPERATION_ROUTES of scripts/claude-gpt/workflow_capability_preflight.py "
        f"(for example {', '.join(_PROMPT_OPERATION_EXAMPLES)}); do not invent other operation names."
    )


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
        # preflight が cwd 不適合で拒否され、planner / SubAgent / terminal step が一度も走らなかった run。
        ("preflight_rejected_planner_never_ran",
         [_FULL[0], "uv run --locked python3 .claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py"],
         [], False),
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


def test_workflow_prompt_pins_known_operation_vocabulary_to_the_registry():
    prompt = build_workflow_prompt("2889")
    assert "_KNOWN_OPERATION_ROUTES" in prompt and "LOOP_PLANNED_OPERATIONS_JSON" in prompt
    spec = importlib.util.spec_from_file_location("workflow_capability_preflight_2925_ac5_prompt", _PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    known = module._KNOWN_OPERATION_ROUTES
    assert isinstance(known, frozenset)
    assert _PROMPT_OPERATION_EXAMPLES
    for name in _PROMPT_OPERATION_EXAMPLES:
        assert name in prompt and name in known, name
    # 推測で作られた operation 名は registry に無い（hint が必要だった理由の固定）。
    assert "controlled_issue_edit" not in known and "controlled_issue_comment_publish" not in known
    assert "controlled_issue_edit" not in prompt


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    for cmd in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
    ):
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)


def test_canonical_main_root_is_resolved_from_a_linked_worktree(tmp_path):
    main = tmp_path / "main"
    _init_repo(main)
    linked = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "feature", str(linked)], cwd=main, check=True,
                   capture_output=True)
    root, reason = resolve_canonical_main_root(linked)
    assert reason is None and root is not None and root.resolve() == main.resolve()
    # main root 自身から解決しても同じ（merge 後は SUT と canonical main root が同一）。
    root_self, reason_self = resolve_canonical_main_root(main)
    assert reason_self is None and root_self is not None and root_self.resolve() == main.resolve()


def test_canonical_main_root_not_on_default_branch_or_unresolvable_is_a_skip_reason(tmp_path):
    main = tmp_path / "main"
    _init_repo(main)
    linked = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "feature", str(linked)], cwd=main, check=True,
                   capture_output=True)
    subprocess.run(["git", "checkout", "-q", "-b", "other"], cwd=main, check=True, capture_output=True)
    root, reason = resolve_canonical_main_root(linked)
    assert root is None and "not the default branch" in (reason or "")
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    root, reason = resolve_canonical_main_root(plain)
    assert root is None and reason


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

    workflow_root, root_problem = resolve_canonical_main_root(_REPO_ROOT)
    if workflow_root is None:
        _skip(f"canonical main root checkout unavailable for the workflow ({root_problem})")

    runner = _load_runner()
    prompt = build_workflow_prompt(issue_number)
    # claude の cwd は canonical main root（preflight.run が要求する場所）。launcher は SUT（PR worktree）の
    # launch.sh を絶対 path で指定する。
    rc, out, err, timed_out = runner.run_structured_claude(
        str(workflow_root), prompt, 1800.0, 120, claude_bin=str(_LAUNCHER), claude_adapter="claude-gpt"
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
        "sut_launcher": {"path": str(_LAUNCHER), "git_head": head},
        "claude_cwd": {
            "kind": "canonical_main_root",
            "path": str(workflow_root),
            "is_sut_checkout": workflow_root.resolve() == _REPO_ROOT.resolve(),
            "git_head": _git(workflow_root, "rev-parse", "HEAD"),
            "branch": _git(workflow_root, "branch", "--show-current"),
        },
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
