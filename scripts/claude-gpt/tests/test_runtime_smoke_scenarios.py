"""scripts/claude-gpt/tests/test_runtime_smoke_scenarios.py

Issue #2925: `scripts/claude-gpt/runtime_smoke_test.sh`（default / auto_classifier scenario）の
hermetic regression。実 smoke script を subprocess で駆動し、scripted fake `claude`
（stream-json を出力し、Bash tool を実際に実行する）と fake server を相手にする。

live runtime での PASS はここでは主張しない（AC9(b) の実 runtime 検証は root が実 Claude Code
process で行う）。ここで固定するのは、smoke script の判定ロジックが次を満たすこと:
  - 実 tool 完了（tool_use と tool_result の対応）と permission mode を見て判定し、text の自己申告
    だけでは PASS しない。
  - Auto mode が effective でなければ FAIL。classifier path の操作（scratch directory 内の
    ファイル作成）が実際に完了していなければ FAIL。
  - 接続先 server / claude が利用不能なら SKIP(77)。SKIP は PASS ではない。
  - routing 先 model は接続先 proxy の構造化ログが観測できる場合だけ記録し、観測できなければ
    「未観測」と記録する（route 確認済みとは扱わない）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_HARNESS_PATH = Path(__file__).resolve().parent / "_launcher_harness.py"
_spec = importlib.util.spec_from_file_location("claude_gpt_launcher_harness_2925_smoke", _HARNESS_PATH)
assert _spec is not None and _spec.loader is not None
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)

_SCRIPTED_CLAUDE = textwrap.dedent(
    '''\
    #!{python}
    import json, os, re, subprocess, sys

    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    mode = os.environ.get("FAKE_PERMISSION_MODE", "auto")
    marker = (re.findall(r"(CLAUDE_GPT_SMOKE_[A-Z]+_OK_[0-9]+)", prompt) or [""])[0]

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")

    if os.environ.get("FAKE_PROMPT_LOG"):
        with open(os.environ["FAKE_PROMPT_LOG"], "a", encoding="utf-8") as fh:
            fh.write(prompt + "\\n=====\\n")

    emit({{"type": "system", "subtype": "init", "permissionMode": mode, "model": "gpt-6-sol[1m]"}})

    def tool(name, body, is_error=False, tid="toolu_1"):
        use = {{"type": "tool_use", "id": tid, "name": name, "input": {{}}}}
        result = {{"type": "tool_result", "tool_use_id": tid, "content": body, "is_error": is_error}}
        emit({{"type": "assistant", "message": {{"content": [use]}}}})
        emit({{"type": "user", "message": {{"content": [result]}}}})

    final = marker
    if "Use the Read tool" in prompt:
        tool("Read", "CLAUDE_GPT_AUTO_COMPACT_WINDOW=272000")
        final = "272000"
    elif "Use the Bash tool" in prompt and "mkdir -p" in prompt:
        command = re.search(r"run exactly: (.*?)  After it finishes", prompt, re.S).group(1)
        # classifier が deny した run は command を実行しない（FAKE_BASH_RESULT_ERROR）。
        if os.environ.get("FAKE_SKIP_CLASSIFIER_EXEC") != "1" and os.environ.get("FAKE_BASH_RESULT_ERROR") != "1":
            subprocess.run(["sh", "-c", command], check=False)
        if os.environ.get("FAKE_BASH_RESULT_ERROR") == "1":
            emit({{"type": "system", "subtype": "permission_denied", "tool": "Bash"}})
            tool("Bash", "Permission denied by classifier. key sk-abcdefgh12345678 " + "x" * 600, is_error=True)
        else:
            tool("Bash", "")
        if os.environ.get("FAKE_PROXY_LOG"):
            with open(os.environ["FAKE_PROXY_LOG"], "a", encoding="utf-8") as fh:
                for i, model in enumerate(os.environ.get("FAKE_PROXY_MODELS", "").split(",")):
                    if not model:
                        continue
                    for msg, fields in (
                        ("request", {{"reqId": f"r{{i}}", "path": "/v1/messages"}}),
                        ("codex_upstream_request_started", {{"reqId": f"r{{i}}", "transport": "http", "model": model}}),
                        ("request_completed", {{"reqId": f"r{{i}}", "status": 200, "model": model}}),
                    ):
                        fh.write(json.dumps({{"fields": fields, "level": "info", "msg": msg}}) + "\\n")
    elif "Use the Bash tool" in prompt:
        tool("Bash", marker)
    elif "Use the Agent tool" in prompt:
        # Claude Code 2.1.289 の実 stream shape: Agent tool_result は定型文だけで marker を含まず、
        # marker は SubAgent 側の SubagentHandback tool_use（parent_tool_use_id = Agent tool_use id）にある。
        shape = os.environ.get("FAKE_AGENT_SHAPE", "real")
        notice = (
            "This agent's report was delivered to you as a message from \\"a1b2c3\\" (its SubagentHandback "
            "call). Read it there; it is not repeated here.\\nagentId: a1b2c3\\n<usage>tool_uses: 1</usage>"
        )
        agent_input = {{"subagent_type": "general-purpose", "description": "d", "prompt": prompt}}
        emit({{"type": "assistant", "parent_tool_use_id": None, "message": {{"content": [
            {{"type": "tool_use", "id": "toolu_agent", "name": "Agent", "input": agent_input}}]}}}})
        if shape not in ("prompt_only", "no_handback", "inline"):
            handback_message = marker if shape != "handback_failed" else "no marker here"
            hb_body = json.dumps({{"success": shape != "handback_is_error", "message": "Report delivered."}})
            hb_parent = "toolu_other" if shape == "handback_other_parent" else "toolu_agent"
            emit({{"type": "assistant", "parent_tool_use_id": hb_parent,
                  "message": {{"content": [{{"type": "tool_use", "id": "toolu_hb", "name": "SubagentHandback",
                                           "input": {{"message": handback_message}}}}]}}}})
            emit({{"type": "user", "parent_tool_use_id": "toolu_agent", "message": {{"content": [
                {{"type": "tool_result", "tool_use_id": "toolu_hb", "is_error": shape == "handback_is_error",
                  "content": [{{"type": "text", "text": hb_body}}]}}]}}}})
        if shape != "no_agent_result":
            body = marker if shape == "inline" else notice
            emit({{"type": "user", "parent_tool_use_id": None, "message": {{"content": [
                {{"type": "tool_result", "tool_use_id": "toolu_agent", "is_error": shape == "agent_is_error",
                  "content": [{{"type": "text", "text": body}}]}}]}}}})
        if shape == "final_text_missing":
            final = "done"
    emit({{"type": "result", "is_error": os.environ.get("FAKE_RESULT_ERROR") == "1", "result": final}})
    '''
)


@pytest.fixture()
def smoke_repo(tmp_path):
    """Clean throwaway git checkout holding copies of the real scripts (so git_dirty=false)."""
    repo = tmp_path / "repo"
    target = repo / "scripts" / "claude-gpt"
    target.mkdir(parents=True)
    for name in ("launch.sh", "lib.sh", "runtime_smoke_test.sh", "transport_log.py"):
        shutil.copy2(H.SCRIPT_DIR / name, target / name)
    (repo / ".gitignore").write_text("scripts/claude-gpt/.evidence/\n", encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    claude = tmp_path / "scripted-claude"  # repo の外に置く（git_dirty を汚さない）
    claude.write_text(_SCRIPTED_CLAUDE.format(python=sys.executable), encoding="utf-8")
    claude.chmod(0o755)
    return repo, claude


def _run_smoke(tmp_path, smoke_repo, args, *, server_url, **env_overrides):
    repo, claude = smoke_repo
    env = H.base_env(
        tmp_path,
        ANTHROPIC_BASE_URL=server_url,
        CLAUDE_GPT_CLAUDE_BIN=str(claude),
        **env_overrides,
    )
    evidence = tmp_path / "evidence.json"
    proc = subprocess.run(
        ["sh", str(repo / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"), *args, "--evidence-out", str(evidence)],
        env=env,
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=120,
    )
    payload = json.loads(evidence.read_text(encoding="utf-8")) if evidence.exists() else None
    return proc, payload


def test_default_scenario_passes_only_on_completed_tool_uses(tmp_path, smoke_repo):
    with H.FakeServer() as server:
        proc, payload = _run_smoke(tmp_path, smoke_repo, ["--scenario", "default"], server_url=server.url)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert payload["status"] == "pass" and payload["scenario"] == "default"
    names = [step["name"] for step in payload["steps"]]
    assert names == ["text", "read", "bash", "subagent"]
    assert all(step["check"]["ok"] for step in payload["steps"])
    assert payload["auto_mode"]["effective"] is True
    assert payload["launch_check_only"]["connected_server"]["version"] == "未確認"


def _subagent_check(tmp_path, smoke_repo, shape):
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path, smoke_repo, ["--scenario", "default"], server_url=server.url, FAKE_AGENT_SHAPE=shape
        )
    by_name = {step["name"]: step["check"] for step in payload["steps"]}
    return proc, payload, by_name["subagent"]


def test_subagent_step_passes_on_the_real_claude_code_handback_stream_shape(tmp_path, smoke_repo):
    # Agent tool_result は定型文のみ（marker なし）。marker は SubagentHandback tool_use の input にある。
    proc, payload, check = _subagent_check(tmp_path, smoke_repo, "real")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert check["tool_use_observed"] is True and check["tool_completed_with_marker"] is True
    assert check["final_text_marker"] is True and check["result_is_error"] is False and check["ok"] is True


def test_subagent_step_still_accepts_a_report_delivered_inside_the_agent_tool_result(tmp_path, smoke_repo):
    proc, _payload, check = _subagent_check(tmp_path, smoke_repo, "inline")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert check["ok"] is True


@pytest.mark.parametrize(
    "shape",
    [
        "prompt_only",  # marker は Agent tool_use の prompt にしかなく、SubagentHandback も無い（dispatch-only）
        "no_handback",  # Agent tool_result は定型文だが SubagentHandback tool_use が無い
        "no_agent_result",  # SubagentHandback はあるが Agent tool_result が無い
        "agent_is_error",  # Agent tool_result が is_error
        "handback_is_error",  # SubagentHandback の tool_result が失敗
        "handback_failed",  # SubagentHandback の message に marker が無い
        "handback_other_parent",  # 別の Agent tool_use に属する SubagentHandback
        "final_text_missing",  # parent の最終 text に marker が無い
    ],
)
def test_subagent_step_negative_controls_are_never_a_pass(tmp_path, smoke_repo, shape):
    proc, payload, check = _subagent_check(tmp_path, smoke_repo, shape)
    assert proc.returncode == 1, (shape, proc.stdout, proc.stderr)
    assert check["ok"] is False and payload["status"] == "fail", (shape, check)
    # 失敗理由が意図した因果欠落であること（別要因の偶発 fail で緑にならない）。
    if shape == "final_text_missing":
        assert check["tool_completed_with_marker"] is True and check["final_text_marker"] is False, check
    else:
        assert check["tool_use_observed"] is True and check["tool_completed_with_marker"] is False, (shape, check)


def test_default_scenario_is_not_passed_by_a_text_claim_without_tool_completion(tmp_path, smoke_repo):
    repo, claude = smoke_repo
    # 自己申告 text だけを返し、tool_use を一切行わない claude。
    claude.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' \'{"type":"system","subtype":"init","permissionMode":"auto"}\'\n'
        'M=$(printf "%s" "$*" | grep -o "CLAUDE_GPT_SMOKE_[A-Z]*_OK_[0-9]*" | head -n1)\n'
        'printf \'{"type":"result","is_error":false,"result":"%s"}\\n\' "$M"\n',
        encoding="utf-8",
    )
    with H.FakeServer() as server:
        proc, payload = _run_smoke(tmp_path, smoke_repo, ["--scenario", "default"], server_url=server.url)
    assert proc.returncode == 1
    by_name = {step["name"]: step["check"] for step in payload["steps"]}
    assert by_name["text"]["ok"] is True  # text-only step は text で成立してよい
    assert by_name["bash"]["ok"] is False and by_name["subagent"]["ok"] is False
    assert payload["status"] == "fail"


def test_auto_classifier_scenario_passes_when_scratch_file_is_created_in_auto_mode(tmp_path, smoke_repo):
    with H.FakeServer() as server:
        proc, payload = _run_smoke(tmp_path, smoke_repo, ["--scenario", "auto_classifier"], server_url=server.url)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    auto = payload["auto_mode"]
    assert payload["status"] == "pass"
    assert auto["effective"] is True and auto["permission_mode_observed"] == "auto"
    assert auto["classifier_file_create_completed"] is True
    # 接続先 proxy の構造化ログが観測できない場合は「未観測」。route 確認済みとは扱わない。
    assert auto["routing_models_observed"] == "未観測" and auto["routing_observed"] is False


def test_auto_classifier_scenario_fails_when_auto_mode_is_not_effective(tmp_path, smoke_repo):
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path,
            smoke_repo,
            ["--scenario", "auto_classifier"],
            server_url=server.url,
            FAKE_PERMISSION_MODE="default",
        )
    assert proc.returncode == 1
    assert payload["status"] == "fail" and payload["auto_mode"]["effective"] is False


def test_auto_classifier_scenario_fails_when_the_classified_operation_did_not_complete(tmp_path, smoke_repo):
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path,
            smoke_repo,
            ["--scenario", "auto_classifier"],
            server_url=server.url,
            FAKE_SKIP_CLASSIFIER_EXEC="1",
        )
    assert proc.returncode == 1
    assert payload["auto_mode"]["classifier_file_create_completed"] is False
    assert payload["status"] == "fail"
    # 最終 text が成功を主張し tool_result も非 error でも（step の stream check は ok）、file が実際に
    # 作られていなければ FAIL（text / tool_result の自己申告だけでは PASS にしない）。
    check = payload["steps"][0]["check"]
    assert check["ok"] is True and check["final_text_marker"] is True


def test_classifier_probe_prompt_has_the_overwrite_ambiguity_guard_on_a_fresh_scratch_dir(tmp_path, smoke_repo):
    prompt_log = tmp_path / "prompts.log"
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path,
            smoke_repo,
            ["--scenario", "auto_classifier"],
            server_url=server.url,
            FAKE_PROMPT_LOG=str(prompt_log),
        )
    assert proc.returncode == 0 and payload["status"] == "pass", (proc.stdout, proc.stderr)
    prompt = prompt_log.read_text(encoding="utf-8")
    assert "This creates a brand-new file inside a fresh empty scratch directory; nothing is overwritten." in prompt
    match = re.search(
        r"run exactly: test ! -e (\S+/probe/classifier_probe\.txt) && mkdir -p (\S+)/probe && "
        r"printf '%s' (\S+) > (\S+/probe/classifier_probe\.txt)  After it finishes",
        prompt,
    )
    assert match, prompt
    guarded, scratch, _marker, written = match.groups()
    assert guarded == written == f"{scratch}/probe/classifier_probe.txt"
    # scratch は mktemp -d で作った fresh な空 directory（probe は実行後に消える）。classifier を通す
    # 操作であり続ける: 事前許可済みの `echo` は使わない。
    assert re.search(r"/claude-gpt-auto-classifier\.[A-Za-z0-9]{6}$", scratch), scratch
    assert not Path(scratch).exists()
    assert "run exactly: echo" not in prompt


def test_failed_step_records_public_safe_failure_detail_and_passing_steps_do_not(tmp_path, smoke_repo):
    # 失敗した step にだけ failure_detail（診断）を付ける。PASS 判定そのものは変えない。
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path,
            smoke_repo,
            ["--scenario", "auto_classifier"],
            server_url=server.url,
            FAKE_BASH_RESULT_ERROR="1",
        )
    assert proc.returncode == 1 and payload["status"] == "fail"
    assert payload["auto_mode"]["classifier_file_create_completed"] is False
    check = payload["steps"][0]["check"]
    assert check["ok"] is False and check["tool_completed_with_marker"] is False
    detail = check["failure_detail"]
    assert detail["tool_use_present"] is True
    result = detail["tool_results"][0]
    assert result["tool_result_present"] is True and result["tool_result_is_error"] is True
    assert result["tool_result_head"].startswith("Permission denied by classifier.")
    assert len(result["tool_result_head"]) <= 300
    assert "sk-abcdefgh12345678" not in json.dumps(payload) and "[REDACTED]" in result["tool_result_head"]
    assert "system/permission_denied" in detail["permission_denial_event_kinds"]
    # 成功 step は failure_detail を持たない。
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    with H.FakeServer() as server:
        ok_proc, ok_payload = _run_smoke(ok_dir, smoke_repo, ["--scenario", "auto_classifier"], server_url=server.url)
    assert ok_proc.returncode == 0
    assert all("failure_detail" not in step["check"] for step in ok_payload["steps"])


def test_auto_classifier_scenario_records_observed_routing_models_only_from_the_proxy_log(tmp_path, smoke_repo):
    proxy_log = tmp_path / "proxy.log"
    proxy_log.write_text("", encoding="utf-8")
    with H.FakeServer() as server:
        proc, payload = _run_smoke(
            tmp_path,
            smoke_repo,
            ["--scenario", "auto_classifier"],
            server_url=server.url,
            CLAUDE_GPT_PROXY_LOG=str(proxy_log),
            FAKE_PROXY_LOG=str(proxy_log),
            FAKE_PROXY_MODELS="gpt-6-sol,gpt-6-luna",
        )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    auto = payload["auto_mode"]
    assert auto["routing_observed"] is True
    assert auto["routing_models_observed"] == ["gpt-6-luna", "gpt-6-sol"]


def test_smoke_skips_with_77_when_the_connected_server_is_unavailable(tmp_path, smoke_repo):
    proc, payload = _run_smoke(
        tmp_path, smoke_repo, ["--scenario", "default"], server_url=f"http://127.0.0.1:{H.closed_port()}"
    )
    assert proc.returncode == 77
    assert payload["status"] == "skip" and payload["reason"] == "connected_server_unavailable_or_catalog_incomplete"
    assert "SKIP は PASS ではありません" in proc.stdout


def test_smoke_skips_with_77_when_the_server_lacks_a_required_model(tmp_path, smoke_repo):
    with H.FakeServer(models=("gpt-6-sol",)) as server:
        proc, payload = _run_smoke(tmp_path, smoke_repo, ["--scenario", "default"], server_url=server.url)
    assert proc.returncode == 77
    assert payload["launch_check_only"]["connected_server"]["missing_models"] == ["gpt-6-luna"]


def test_smoke_fails_on_a_dirty_worktree_instead_of_claiming_the_current_head(tmp_path, smoke_repo):
    repo, _claude = smoke_repo
    (repo / "scripts" / "claude-gpt" / "lib.sh").write_text(
        (repo / "scripts" / "claude-gpt" / "lib.sh").read_text(encoding="utf-8") + "\n# dirty\n", encoding="utf-8"
    )
    with H.FakeServer() as server:
        proc, payload = _run_smoke(tmp_path, smoke_repo, ["--scenario", "default"], server_url=server.url)
    assert proc.returncode == 1 and payload["sut"]["git_dirty"] == "true"


def test_unknown_scenario_and_retired_spark_flag_never_fall_back_to_ordinary_smoke(tmp_path, smoke_repo):
    repo, _claude = smoke_repo
    script = str(repo / "scripts" / "claude-gpt" / "runtime_smoke_test.sh")
    for args in (["--scenario", "issue_to_impl"], ["--scenario", "nope"], ["--spark-delegation"], ["--bogus"]):
        proc = subprocess.run(["sh", script, *args], capture_output=True, text=True, timeout=30, cwd=str(repo))
        assert proc.returncode == 2, (args, proc.stdout, proc.stderr)
