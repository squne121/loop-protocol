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
        if os.environ.get("FAKE_SKIP_CLASSIFIER_EXEC") != "1":
            subprocess.run(["sh", "-c", command], check=False)
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
        tool("Agent", marker)
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
