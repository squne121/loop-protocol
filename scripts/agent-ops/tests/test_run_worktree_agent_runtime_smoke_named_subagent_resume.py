"""Issue #2840: ``--named-subagent-resume`` scenario of the worktree agent runtime smoke runner.

This module covers

- AC1  the scenario's ``--settings`` overlay / argv (and that the default smoke is unchanged),
- AC2  the evidence records runtime identity and the causal chain,
- Issue #2925 AC4  the Claude-GPT live wrapper additionally requires the role-routed SubAgents
  (``model: haiku`` ``codebase-investigator`` and ``model: sonnet`` ``issue-design-reviewer``) to
  spawn -> terminally complete -> hand back a real result, judged by
  ``_claude_gpt_role_subagent_smoke.evaluate_role_subagent`` (hermetically unit-tested below)
- AC3/AC4  live wrapper tests for Native and the repository-owned Claude-GPT launcher
  (skipped under ``CI`` with an explicit reason, SKIP exit 77 propagated, never PASS),
- AC6  runner-side generic name <-> agent ID correlation controls (real behavioural
  tests: every negative control is paired with a normal case built from the same
  synthetic stream builder and differs from it by exactly one fact),
- AC7  failure-layer classification (``unclassified`` is never a PASS),
- AC8  ``evaluate_evidence_freshness``,
- AC9  public evidence excludes raw prompt / transcript / credential / HOME path.

Task Context resolver semantics stay owned by ``tests/task-context/``; nothing here
re-implements the resolver.

Live tests need an authenticated ``claude`` (Native), the repository-owned launcher
and its proxy.  Only the two live test functions carry ``@pytest.mark.claude_live``:
the default ``addopts`` deselects them, so a plain ``pytest`` never launches a live
run and nobody has to fake ``CI`` to avoid it; opt in with ``-m claude_live``.  The
offline regressions stay in the default selection.  The wrapper's ``CI`` skip is
kept as defence in depth: that skip is NOT a PASS (the reason carries
``RUNTIME_VERIFICATION_SKIPPED_NOT_PASS``) and AC3/AC4 are judged only from the
evidence JSON a local live run writes.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
FIXTURE_DIR = REPO_ROOT / ".claude" / "skills" / "worktree-agent-runtime-smoke" / "fixtures"
PROMPT_FIXTURE = FIXTURE_DIR / "named-subagent-resume.prompt.md"
COMPAT_FIXTURE = FIXTURE_DIR / "named-subagent-resume.compat.md"

SKIP_REASON_TOKEN = "RUNTIME_VERIFICATION_SKIPPED_NOT_PASS"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_worktree_agent_runtime_smoke_named_resume_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()

_ROLE_HELPER_PATH = Path(__file__).resolve().parent / "_claude_gpt_role_subagent_smoke.py"
_role_spec = importlib.util.spec_from_file_location("claude_gpt_role_subagent_smoke_2925", _ROLE_HELPER_PATH)
assert _role_spec is not None and _role_spec.loader is not None
ROLE = importlib.util.module_from_spec(_role_spec)
sys.modules[_role_spec.name] = ROLE
_role_spec.loader.exec_module(ROLE)
FIRST = MODULE.NAMED_SUBAGENT_RESUME_FIRST_MARKER
SECOND = MODULE.NAMED_SUBAGENT_RESUME_SECOND_MARKER
NAME = MODULE.NAMED_SUBAGENT_RESUME_AGENT_NAME


# ---------------------------------------------------------------------------
# Synthetic stream-json builder (shapes observed against Claude Code 2.1.287)
# ---------------------------------------------------------------------------


class Stream:
    """Builds a stream-json stdout string in the shape the live runtime emits."""

    def __init__(self, session: str = "sess-main", *, tools=("Task", "SendMessage", "Bash"), model="model-x"):
        self.session = session
        self.lines: list[str] = []
        self.add({
            "type": "system", "subtype": "init", "session_id": session, "model": model,
            "claude_code_version": "2.1.287", "permissionMode": "auto", "tools": list(tools),
        })

    def add(self, event: dict) -> "Stream":
        self.lines.append(json.dumps(event))
        return self

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def _hook(self, hook_event: str, hook_name: str, payload: dict | None, *, exit_code: int = 0,
              raw_stdout: str | None = None, outcome: str = "success") -> "Stream":
        stdout = raw_stdout if raw_stdout is not None else (json.dumps(payload) if payload is not None else "")
        return self.add({
            "type": "system", "subtype": "hook_response", "hook_event": hook_event, "hook_name": hook_name,
            "stdout": stdout, "output": stdout, "stderr": "", "exit_code": exit_code, "outcome": outcome,
            "session_id": self.session,
        })

    def agent_call(self, tool_use_id: str, name: str | None, subagent_type: str = "general-purpose",
                   **extra_input) -> "Stream":
        tool_input = {"description": "d", "subagent_type": subagent_type, "prompt": "p", **extra_input}
        if name is not None:
            tool_input["name"] = name
        return self.add({
            "type": "assistant", "parent_tool_use_id": None, "session_id": self.session,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "Agent", "input": tool_input}]},
        })

    def post_agent(self, tool_use_id: str, name: str | None, agent_id: str, *, session: str | None = None,
                   handback: str | None = None) -> "Stream":
        tool_input = {"prompt": "p"}
        if name is not None:
            tool_input["name"] = name
        response: dict = {"status": "completed", "agentId": agent_id, "agentType": "general-purpose"}
        if handback is not None:
            response["handbackReport"] = {"text": handback}
        return self._hook("PostToolUse", "PostToolUse:Agent", {
            "hook_event_name": "PostToolUse", "session_id": session or self.session, "tool_name": "Agent",
            "tool_input": tool_input, "tool_response": response, "tool_use_id": tool_use_id,
        })

    def start(self, agent_id: str, *, session: str | None = None, agent_type: str = "general-purpose") -> "Stream":
        return self._hook("SubagentStart", f"SubagentStart:{agent_type}", {
            "hook_event_name": "SubagentStart", "session_id": session or self.session,
            "agent_id": agent_id, "agent_type": agent_type,
        })

    def stop(self, agent_id: str, *, session: str | None = None, agent_type: str = "general-purpose") -> "Stream":
        return self._hook("SubagentStop", "SubagentStop", {
            "hook_event_name": "SubagentStop", "session_id": session or self.session,
            "agent_id": agent_id, "agent_type": agent_type,
        })

    def child_text(self, text: str, parent_tool_use_id: str = "toolu_agent_1") -> "Stream":
        return self.add({
            "type": "assistant", "parent_tool_use_id": parent_tool_use_id, "session_id": self.session,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_hb", "name": "SubagentHandback", "input": {"message": text}}]},
        })

    def send_call(self, tool_use_id: str, to: str) -> "Stream":
        return self.add({
            "type": "assistant", "parent_tool_use_id": None, "session_id": self.session,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_use_id, "name": "SendMessage",
                 "input": {"to": to, "message": "m", "summary": "s"}}]},
        })

    def pretool_send(self, to: str, *, decision: str | None = None, session: str | None = None,
                     tool_use_id: str = "toolu_send_1", echo_exit_code: int = 0, project_exit_code: int = 0,
                     project_outcome: str = "success") -> "Stream":
        self._hook("PreToolUse", "PreToolUse:SendMessage", {
            "hook_event_name": "PreToolUse", "session_id": session or self.session, "tool_name": "SendMessage",
            "tool_input": {"to": to}, "tool_use_id": tool_use_id,
        }, exit_code=echo_exit_code)
        # A second hook on the same matcher (the repository's own project hook): empty
        # stdout means "no decision"; a decision object is a generic permission decision.
        raw = ""
        if decision is not None:
            raw = json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": decision}})
        return self._hook(
            "PreToolUse", "PreToolUse:SendMessage", None, raw_stdout=raw,
            exit_code=project_exit_code, outcome=project_outcome)

    def send_result(self, tool_use_id: str, *, success: bool = True, resumed: str | None = None,
                    pin_name: str | None = None) -> "Stream":
        result: dict = {"success": success, "message": "Resuming agent" if success else "failed"}
        if resumed is not None:
            result["resumedAgentId"] = resumed
            result["pin"] = {"id": resumed, "name": pin_name or "n", "ref": "r"}
        return self.add({
            "type": "user", "session_id": self.session, "tool_use_result": result,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": [{"type": "text", "text": json.dumps(result)}]}]},
        })

    def notification(self, task_id: str, tool_use_id: str = "toolu_send_1", status: str = "completed") -> "Stream":
        return self.add({
            "type": "system", "subtype": "task_notification", "task_id": task_id,
            "tool_use_id": tool_use_id, "status": status, "session_id": self.session,
        })

    def parent_text(self, text: str) -> "Stream":
        return self.add({
            "type": "assistant", "parent_tool_use_id": None, "session_id": self.session,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        })

    def result(self, **fields) -> "Stream":
        return self.add({"type": "result", "subtype": "success", "is_error": False, **fields})


def full_chain(
    *, name: str | None = NAME, agent_id: str = "agent-A", send_to: str | None = None,
    pretool_decision: str | None = None, session: str = "sess-main", pretool: bool = True,
    pretool_kwargs: dict | None = None,
) -> Stream:
    """A complete, correct spawn -> complete -> name resume -> complete stream."""
    stream = Stream(session)
    stream.agent_call("toolu_agent_1", name)
    stream.start(agent_id)
    stream.child_text(FIRST)
    stream.stop(agent_id)
    stream.post_agent("toolu_agent_1", name, agent_id, handback=FIRST)
    stream.send_call("toolu_send_1", send_to if send_to is not None else (name or agent_id))
    if pretool:
        stream.pretool_send(
            send_to if send_to is not None else (name or agent_id), decision=pretool_decision,
            **(pretool_kwargs or {}))
    stream.send_result("toolu_send_1", resumed=agent_id, pin_name=name)
    stream.start(agent_id)
    stream.child_text(SECOND)
    stream.stop(agent_id)
    stream.notification(agent_id)
    stream.parent_text(f"resumed agent returned {SECOND}")
    stream.result()
    return stream


def evaluate(stream: Stream) -> dict:
    return MODULE.evaluate_named_subagent_resume_chain(MODULE.extract_named_subagent_resume_observations(stream.text()))


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS_EXPECTED = json.dumps({
    "crossSessionInbound": "refuse",
    "permissions": {"deny": ["SendMessage", "ListAgents"]},
    "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}],
    },
})
DEFAULT_SETTINGS_WITH_EXPANSION_EXPECTED = json.dumps({
    "crossSessionInbound": "refuse",
    "permissions": {"deny": ["SendMessage", "ListAgents"]},
    "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": "cat"}]}],
        "UserPromptExpansion": [{"hooks": [{"type": "command", "command": "cat"}]}],
    },
})


def _capture_run(monkeypatch) -> dict:
    captured: dict = {}

    def fake_run(argv, *, cwd=None, timeout, input_text=None, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        return 0, "", "", False

    monkeypatch.setattr(MODULE, "_run", fake_run)
    return captured


def _settings_arg(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


def test_scenario_settings_drop_only_sendmessage_deny_and_default_unchanged(monkeypatch, tmp_path):
    scenario = json.loads(MODULE.select_native_observation_settings_json(named_subagent_resume=True))
    default = json.loads(MODULE.select_native_observation_settings_json())

    # Only the blanket SendMessage deny differs; the rest of the policy is kept.
    assert default["permissions"]["deny"] == ["SendMessage", "ListAgents"]
    assert scenario["permissions"]["deny"] == ["ListAgents"]
    assert "SendMessage" not in scenario["permissions"]["deny"]
    assert scenario["crossSessionInbound"] == default["crossSessionInbound"] == "refuse"
    # No permission mode / bypass knob is introduced by the overlay.
    assert set(scenario) == {"crossSessionInbound", "permissions", "hooks"}
    assert set(scenario["permissions"]) == {"deny"}
    raw = MODULE.select_native_observation_settings_json(named_subagent_resume=True)
    assert "bypass" not in raw.lower() and "dangerously" not in raw.lower()

    # Generic observation hook set only: SubagentStart / SubagentStop / PostToolUse:Agent / PreToolUse:SendMessage.
    hooks = scenario["hooks"]
    assert set(hooks) == {"SubagentStart", "SubagentStop", "PostToolUse", "PreToolUse"}
    assert hooks["PostToolUse"][0]["matcher"] == "Agent"
    assert hooks["PreToolUse"][0]["matcher"] == "SendMessage"
    observed = {
        (event, group.get("matcher"))
        for event, groups in hooks.items()
        for group in groups
    }
    assert observed == set(MODULE.NAMED_SUBAGENT_RESUME_OBSERVATION_HOOKS)
    for groups in hooks.values():
        for group in groups:
            assert group["hooks"] == [{"type": "command", "command": "cat"}]
    # No Task Context verdict / hook entry is carried by the generic overlay.
    assert "task_context" not in raw and "hook_entry" not in raw

    # The default overlay is byte-identical to the pre-existing literal (flag absent).
    assert MODULE.select_native_observation_settings_json() == DEFAULT_SETTINGS_EXPECTED
    assert (
        MODULE.select_native_observation_settings_json(include_user_prompt_expansion_hook=True)
        == DEFAULT_SETTINGS_WITH_EXPANSION_EXPECTED
    )
    with pytest.raises(ValueError):
        MODULE.select_native_observation_settings_json(
            named_subagent_resume=True, include_hook_chain_evidence_hooks=True
        )

    # argv: default keeps --no-session-persistence and both denies; scenario drops only what it must.
    captured = _capture_run(monkeypatch)
    MODULE.run_structured_claude(str(tmp_path), "p", 5.0, 7, claude_bin="/bin/claude-fake")
    default_argv = captured["argv"]
    assert "--no-session-persistence" in default_argv
    assert _settings_arg(default_argv)["permissions"]["deny"] == ["SendMessage", "ListAgents"]
    assert "--append-system-prompt-file" not in default_argv

    MODULE.run_structured_claude(
        str(tmp_path), "p", 5.0, 7, claude_bin="/bin/claude-fake", named_subagent_resume=True,
        append_system_prompt_file="/abs/compat.md",
    )
    scenario_argv = captured["argv"]
    assert "--no-session-persistence" not in scenario_argv
    assert _settings_arg(scenario_argv)["permissions"]["deny"] == ["ListAgents"]
    assert scenario_argv[scenario_argv.index("--append-system-prompt-file") + 1] == "/abs/compat.md"
    assert "--permission-mode" not in scenario_argv
    assert "--dangerously-skip-permissions" not in scenario_argv
    flags = [tok for tok in scenario_argv if tok.startswith("-") and tok != "--"]
    assert set(MODULE.named_resume_invocation_flag_readback("native", True)) == set(flags)

    # claude-gpt adapter (Issue #2925): the thin launcher forwards everything after its own
    # ``--``, so the runner passes the SAME fixed scenario overlay as the native adapter and
    # sets no launcher-owned env channel.
    MODULE.run_structured_claude(
        str(tmp_path), "p", 5.0, 7, claude_bin="/abs/launch.sh", claude_adapter="claude-gpt",
        named_subagent_resume=True, append_system_prompt_file="/abs/compat.md",
    )
    assert captured["env"] is None or "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS" not in captured["env"]
    assert captured["argv"][1] == "--"
    assert captured["argv"].index("--settings") > 1
    assert _settings_arg(captured["argv"]) == _settings_arg(scenario_argv)
    assert set(MODULE.named_resume_invocation_flag_readback("claude-gpt", True)) == {
        tok for tok in captured["argv"] if tok.startswith("-") and tok != "--"
    }
    MODULE.run_structured_claude(
        str(tmp_path), "p", 5.0, 7, claude_bin="/abs/launch.sh", claude_adapter="claude-gpt"
    )
    assert captured["env"] is None or "CLAUDE_GPT_RUNTIME_SMOKE_HOOKS" not in captured["env"]
    assert _settings_arg(captured["argv"])["permissions"]["deny"] == ["SendMessage", "ListAgents"]
    assert "--no-session-persistence" in captured["argv"]

    # The compat note is scenario-only: never a permanent injection into other lanes.
    with pytest.raises(ValueError):
        MODULE.run_structured_claude(
            str(tmp_path), "p", 5.0, 7, claude_bin="/bin/claude-fake", append_system_prompt_file="/abs/compat.md"
        )


def test_fixture_paths_consistent_across_runner_docs_and_skill():
    assert PROMPT_FIXTURE.is_file() and COMPAT_FIXTURE.is_file()
    assert MODULE.NAMED_SUBAGENT_RESUME_PROMPT_FIXTURE_RELPATH == str(PROMPT_FIXTURE.relative_to(REPO_ROOT))
    assert MODULE.NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH == str(COMPAT_FIXTURE.relative_to(REPO_ROOT))
    prompt = PROMPT_FIXTURE.read_text(encoding="utf-8")
    assert NAME in prompt and FIRST in prompt and SECOND in prompt
    compat = COMPAT_FIXTURE.read_text(encoding="utf-8")
    assert "--append-system-prompt-file" in compat
    for doc in (
        REPO_ROOT / ".claude" / "skills" / "worktree-agent-runtime-smoke" / "SKILL.md",
        REPO_ROOT / "docs" / "dev" / "claude-gpt-runtime-prerequisites.md",
    ):
        text = doc.read_text(encoding="utf-8")
        assert "Named SubAgent resume scenario" in text, doc
        assert MODULE.NAMED_SUBAGENT_RESUME_PROMPT_FIXTURE_RELPATH in text, doc
        assert MODULE.NAMED_SUBAGENT_RESUME_COMPAT_FIXTURE_RELPATH in text, doc


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def _fake_launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "wt" / "scripts" / "claude-gpt" / "launch.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (launcher.parent / "lib.sh").write_text('CLAUDE_GPT_MODEL_MAIN="gpt-test[1m]"\n', encoding="utf-8")
    return launcher


def _evidence(stream: Stream, *, adapter="native", tmp_path: Path | None = None, **overrides) -> dict:
    kwargs = dict(
        stdout=stream.text(), stderr="", process_exit_code=0, timed_out=False, adapter=adapter,
        tested_head="a" * 40, runtime_version="2.1.287 (Claude Code)", resolved_runtime_bin=None,
        worktree=str(tmp_path or REPO_ROOT), prompt_fixture_path=str(PROMPT_FIXTURE),
        compat_fixture_path=str(COMPAT_FIXTURE), compat_note_applied=True,
        invocation_flags=MODULE.named_resume_invocation_flag_readback(adapter, True), env={},
    )
    kwargs.update(overrides)
    return MODULE.build_named_subagent_resume_evidence(**kwargs)


def test_evidence_records_runtime_identity_and_causal_chain(tmp_path):
    launcher = _fake_launcher(tmp_path)
    stderr = f"launcher={launcher} git=abc1234 dirty=false proxy=claude-code-proxy 0.1.42\n"
    evidence = _evidence(
        full_chain(), adapter="claude-gpt", tmp_path=tmp_path / "wt", stderr=stderr,
        resolved_runtime_bin=str(launcher), proxy_bin="/opt/proxy/claude-code-proxy",
    )
    assert evidence["verdict"] == "pass" and evidence["failure_layer"] is None
    assert evidence["adapter"] == "claude-gpt" and evidence["tested_head"] == "a" * 40
    assert evidence["claude_code_version"] == "2.1.287"
    assert evidence["runtime"] == "claude" and evidence["mode"] == "structured"
    assert evidence["agent_name"] == NAME and evidence["agent_id"] == "agent-A"
    assert evidence["caller_session_id"] == "sess-main"
    assert evidence["addressing"] == "name"
    assert evidence["agent_kind"]["kind"] == "ordinary_subagent"
    assert "subagent_start_stop_hook_events_observed" in evidence["agent_kind"]["basis"]
    assert evidence["agent_teams"]["effective_state"] == "disabled"
    assert evidence["agent_teams"]["structured_print_lane"] is True
    assert evidence["first_completion"] == {"observed": True, "marker_from_child": True}
    assert evidence["same_agent_id_after_resume"] is True
    assert all(evidence["causal_chain"].values()) and len(evidence["causal_chain"]) == 9
    assert evidence["launcher"]["path"] == "scripts/claude-gpt/launch.sh"
    assert evidence["launcher"]["sha256"] == MODULE._nr_file_sha256(str(launcher))
    assert evidence["proxy"]["version"] == "claude-code-proxy 0.1.42"
    assert evidence["model_route"] == {
        "observed_main_model": "model-x", "launcher_policy_main_model": "gpt-test[1m]"}
    assert evidence["fixtures"]["prompt_sha256"] == MODULE._nr_file_sha256(str(PROMPT_FIXTURE))
    assert evidence["fixtures"]["compat_note_sha256"] == MODULE._nr_file_sha256(str(COMPAT_FIXTURE))
    assert evidence["compat_note"]["applied_via_append_system_prompt_file"] is True
    assert "--append-system-prompt-file" in evidence["invocation_flags_readback"]
    assert evidence["false_ask_observed"] is False
    assert [d["decision"] for d in evidence["sendmessage_hook_decisions"]] == ["none", "none"]
    # flat freshness record round-trips every AC8 key
    record = MODULE.freshness_record_from_evidence(evidence)
    assert record["tested_head"] == "a" * 40 and record["launcher_sha256"] and record["proxy_version"]
    assert record["model_route"] == "model-x" and record["fixture_sha256"] and record["compat_note_sha256"]

    # Agent kind is decided from hook events / team signals, never from the name or a panel.
    teammate_like_name = full_chain(name="team-lead-teammate")
    assert _evidence(teammate_like_name)["agent_kind"]["kind"] == "ordinary_subagent"
    teammate = Stream()
    teammate.agent_call("toolu_agent_1", NAME, team_name="squad")
    teammate.start("agent-A").child_text(FIRST).stop("agent-A")
    teammate.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    assert MODULE.determine_named_resume_agent_kind(
        MODULE.extract_named_subagent_resume_observations(teammate.text()), {"agent_id": "agent-A"}
    )["kind"] == "teammate"
    # Agent Teams effective state is an independent observation: enabled by env,
    # yet the structured lane still starts an ordinary SubAgent (no team signal).
    enabled = _evidence(full_chain(), env={"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"})
    assert enabled["agent_teams"]["effective_state"] == "enabled"
    assert enabled["agent_kind"]["kind"] == "ordinary_subagent"
    # No lifecycle hook for the agent -> kind stays unknown rather than guessed.
    no_lifecycle = Stream()
    no_lifecycle.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A")
    assert MODULE.determine_named_resume_agent_kind(
        MODULE.extract_named_subagent_resume_observations(no_lifecycle.text()), {"agent_id": "agent-A"}
    )["kind"] == "unknown"


# ---------------------------------------------------------------------------
# AC3 / AC4 live wrappers
# ---------------------------------------------------------------------------


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True
    ).stdout.strip()


def _adjudicate_live_evidence(evidence_path: Path, *, adapter: str, expected_head: str) -> dict:
    """Judge ONLY from the evidence JSON a live run wrote: verdict=pass and tested HEAD match."""
    assert evidence_path.is_file(), "live run wrote no evidence JSON (never a PASS)"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence.get("schema") == "NAMED_SUBAGENT_RESUME_EVIDENCE_V1"
    assert evidence.get("adapter") == adapter
    assert evidence.get("verdict") == "pass", f"verdict={evidence.get('verdict')!r}"
    assert evidence.get("failure_layer") is None
    assert evidence.get("tested_head") == expected_head, "evidence tested HEAD does not match current HEAD"
    assert evidence.get("runner_exit_code") == 0
    return evidence


def _run_live_wrapper(adapter: str, *, runner_argv: list[str] | None = None, out_root: Path, env=None) -> dict:
    """Run the runner (subprocess) for one adapter and adjudicate its evidence.

    ``CI`` -> skip with an explicit non-PASS reason.  Runner exit 77 (SKIP) is
    propagated as ``pytest.exit(returncode=77)``; any other non-zero exit fails."""
    if os.environ.get("CI"):
        pytest.skip(
            f"{SKIP_REASON_TOKEN}: live Native/Claude-GPT named SubAgent resume needs an authenticated "
            "runtime and is not run in CI; AC3/AC4 are judged only from the local live evidence JSON"
        )
    head = _git_head()
    evidence_path = out_root / f"runtime-verification-2840-{adapter}-{head[:8]}.json"
    if runner_argv is None:
        runner_argv = [
            sys.executable, str(SCRIPT), "--runtime", "claude", "--mode", "structured",
            "--worktree", str(REPO_ROOT), "--prompt-file", str(PROMPT_FIXTURE),
            "--output-dir", str(Path(tempfile.mkdtemp(prefix=f"named-resume-{adapter}-")) / "out"),
            "--named-subagent-resume", "--append-system-prompt-file", str(COMPAT_FIXTURE),
            "--named-resume-evidence-json", str(evidence_path),
            "--claude-adapter", adapter, "--timeout-seconds", "540",
        ]
    proc = subprocess.run(runner_argv, cwd=str(REPO_ROOT), capture_output=True, text=True,
                          timeout=600, env=env)
    if proc.returncode == 77:
        pytest.exit(
            f"{SKIP_REASON_TOKEN}: runner exited 77 (SKIP) for adapter={adapter}; never a PASS",
            returncode=77,
        )
    assert proc.returncode == 0, f"runner exit={proc.returncode}: {proc.stderr[-1500:]}"
    return _adjudicate_live_evidence(evidence_path, adapter=adapter, expected_head=head)


def _live_artifacts_dir() -> Path:
    path = REPO_ROOT / "artifacts"
    path.mkdir(exist_ok=True)
    return path


@pytest.mark.claude_live
def test_live_native_named_subagent_resume():
    evidence = _run_live_wrapper("native", out_root=_live_artifacts_dir())
    assert evidence["agent_kind"]["kind"] == "ordinary_subagent"
    assert evidence["same_agent_id_after_resume"] is True


def _run_default_runtime_smoke(out_root: Path) -> dict:
    """Issue #2925 AC4: text / Read / Bash / SubAgent tool use through the Minimal launcher.

    Runs ``scripts/claude-gpt/runtime_smoke_test.sh --scenario default`` (exit 77 = SKIP is propagated,
    never a PASS) and adjudicates ONLY from the evidence JSON it writes."""
    head = _git_head()
    evidence_path = out_root / f"runtime-verification-2925-claude-gpt-smoke-{head[:8]}.json"
    proc = subprocess.run(
        ["sh", str(REPO_ROOT / "scripts" / "claude-gpt" / "runtime_smoke_test.sh"),
         "--scenario", "default", "--evidence-out", str(evidence_path)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=900,
    )
    if proc.returncode == 77:
        pytest.exit(f"{SKIP_REASON_TOKEN}: runtime_smoke_test.sh exited 77 (SKIP); never a PASS", returncode=77)
    assert proc.returncode == 0, f"smoke exit={proc.returncode}: {(proc.stdout + proc.stderr)[-1500:]}"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["status"] == "pass" and evidence["sut"]["git_head"] == head
    assert [step["name"] for step in evidence["steps"]] == ["text", "read", "bash", "subagent"]
    assert all(step["check"]["ok"] for step in evidence["steps"])
    return evidence


def _run_role_routed_subagents(out_root: Path) -> dict:
    """Issue #2925 AC4: haiku ``codebase-investigator`` and sonnet ``issue-design-reviewer``."""
    launcher = REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"
    results: dict = {}

    haiku_prompt = ROLE.haiku_prompt(str(REPO_ROOT))
    rc, out, err, timed_out = MODULE.run_structured_claude(
        str(REPO_ROOT), haiku_prompt, 600.0, 30, claude_bin=str(launcher), claude_adapter="claude-gpt")
    assert not timed_out and rc == 0, f"haiku role run rc={rc} timed_out={timed_out}: {err[-800:]}"
    results["haiku"] = ROLE.evaluate_role_subagent(
        out, ROLE.HAIKU_AGENT, ROLE.validate_haiku_handback,
        hook_events=MODULE.extract_claude_hook_lifecycle_events(out))

    spec = importlib.util.spec_from_file_location(
        "semantic_review_transport_2925",
        REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "semantic_review_transport.py")
    assert spec is not None and spec.loader is not None
    transport = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = transport
    spec.loader.exec_module(transport)
    pinned = transport.pin_bundle(
        issue_number=2925, body_text=ROLE.SAMPLE_ISSUE_BODY, prompt_version="2925-smoke",
        requested_model="sonnet", artifacts_root=Path(tempfile.mkdtemp(prefix="role-subagent-bundle-")))
    rc, out, err, timed_out = MODULE.run_structured_claude(
        str(REPO_ROOT), ROLE.sonnet_prompt(pinned["invocation_dir"]), 600.0, 30,
        claude_bin=str(launcher), claude_adapter="claude-gpt")
    assert not timed_out and rc == 0, f"sonnet role run rc={rc} timed_out={timed_out}: {err[-800:]}"
    results["sonnet"] = ROLE.evaluate_role_subagent(
        out, ROLE.SONNET_AGENT, ROLE.validate_sonnet_handback,
        hook_events=MODULE.extract_claude_hook_lifecycle_events(out))

    artifact = out_root / f"runtime-verification-2925-role-subagents-{_git_head()[:8]}.json"
    artifact.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def _skip_unless_connected_server_available() -> None:
    """Issue #2925: the Minimal launcher never starts a proxy, so an unreachable / incomplete
    connected server is an UNAVAILABLE runtime (exit 77), never a FAIL and never a PASS."""
    if os.environ.get("CI"):
        return  # `_run_live_wrapper` reports the explicit CI skip
    check = subprocess.run(
        ["sh", str(REPO_ROOT / "scripts" / "claude-gpt" / "launch.sh"), "--check-only"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    if check.returncode != 0:
        pytest.exit(
            f"{SKIP_REASON_TOKEN}: launch.sh --check-only exited {check.returncode} (connected "
            "claude-code-proxy unavailable or its model catalog is incomplete); never a PASS",
            returncode=77,
        )


@pytest.mark.claude_live
def test_live_claude_gpt_named_subagent_resume():
    _skip_unless_connected_server_available()
    evidence = _run_live_wrapper("claude-gpt", out_root=_live_artifacts_dir())
    assert evidence["agent_kind"]["kind"] == "ordinary_subagent"
    assert evidence["same_agent_id_after_resume"] is True
    assert evidence["false_ask_observed"] is False
    assert evidence["hook_event_counts"]["SubagentStart"] >= 2
    assert evidence["hook_event_counts"]["SubagentStop"] >= 2
    assert evidence["launcher"]["path"] == "scripts/claude-gpt/launch.sh"

    # Issue #2925 AC4: same current Claude Code binary / same repository HEAD, Minimal Claude-GPT.
    _run_default_runtime_smoke(_live_artifacts_dir())
    roles = _run_role_routed_subagents(_live_artifacts_dir())
    assert roles["haiku"]["ok"], roles["haiku"]
    assert roles["sonnet"]["ok"], roles["sonnet"]


# ---------------------------------------------------------------------------
# Issue #2925 AC4: role-routed SubAgent hand-back evaluator (hermetic; dispatch-only,
# fixture-only and context-starved stops are never a PASS)
# ---------------------------------------------------------------------------


def _role_stream(agent_type: str, *, handback: str | None, start: bool = True, stop: bool = True,
                 stop_before_start: bool = False, call_type: str | None = None) -> str:
    stream = Stream()
    stream.agent_call("toolu_role_1", None, subagent_type=call_type or agent_type)
    if stop_before_start and stop:
        stream.stop("agent-R", agent_type=agent_type)
    if start:
        stream.start("agent-R", agent_type=agent_type)
    if stop and not stop_before_start:
        stream.stop("agent-R", agent_type=agent_type)
    if handback is not None:
        stream.add({
            "type": "user", "session_id": stream.session,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_role_1",
                 "content": [{"type": "text", "text": handback}]}]},
        })
    stream.result()
    return stream.text()


_HANDBACK_NOTICE = (
    "  This agent's report was delivered to you as a message from \"agent-R\" (its SubagentHandback call). "
    "Read it there; it is not repeated here.\n  \nagentId: agent-R (use SendMessage with to: 'agent-R')\n"
    "<usage>subagent_tokens: 27852\ntool_uses: 3\nduration_ms: 13535</usage>"
)


def _real_shape_role_stream(
    agent_type: str, *, handback: str | None, agent_result: bool = True, agent_is_error: bool = False,
    handback_success: bool = True, handback_parent: str = "toolu_role_1", prompt_text: str | None = None,
) -> str:
    """Claude Code 2.1.289 stream shape: the Agent tool_result is a fixed delivery notice and the
    report itself is the SubAgent's own ``SubagentHandback`` tool_use input (+ a success tool_result)."""
    stream = Stream()
    extra = {"prompt": prompt_text} if prompt_text else {}
    stream.agent_call("toolu_role_1", None, subagent_type=agent_type, **extra)
    stream.start("agent-R", agent_type=agent_type)
    if handback is not None:
        stream.add({
            "type": "assistant", "parent_tool_use_id": handback_parent, "session_id": stream.session,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_hb_1", "name": "SubagentHandback", "input": {"message": handback}}]},
        })
        stream.add({
            "type": "user", "parent_tool_use_id": handback_parent, "session_id": stream.session,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_hb_1", "is_error": not handback_success,
                 "content": [{"type": "text", "text": json.dumps({"success": handback_success})}]}]},
        })
    stream.stop("agent-R", agent_type=agent_type)
    if agent_result:
        stream.add({
            "type": "user", "parent_tool_use_id": None, "session_id": stream.session,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_role_1", "is_error": agent_is_error,
                 "content": [{"type": "text", "text": _HANDBACK_NOTICE}]}]},
        })
    stream.result()
    return stream.text()


# live で観測された `codebase-investigator` の `status: ok` shape（存在確認）。
_HAIKU_OK_HANDBACK = (
    "CODEBASE_INVESTIGATION_RESULT_V1\n"
    '{"schema_version": 1, "status": "ok", "investigation_route": "local_asset_research", '
    '"discovery_summary": "The Python function validate_haiku_handback is defined in '
    '_claude_gpt_role_subagent_smoke.py.", "failure_reason": null}'
)
_HAIKU_NEGATIVES = {
    "not_defined": 'CODEBASE_INVESTIGATION_RESULT_V1\n{"status": "ok", "discovery_summary": '
                   '"validate_haiku_handback is not defined in the file."}',
    "status_inconclusive": 'CODEBASE_INVESTIGATION_RESULT_V1\n{"status": "inconclusive", '
                           '"discovery_summary": "validate_haiku_handback is defined but unverified"}',
    "status_failed": 'CODEBASE_INVESTIGATION_RESULT_V1\n{"status": "failed", '
                     '"failure_reason": "validate_haiku_handback is defined? wrapper failed"}',
    "insufficient_context": "INSUFFICIENT_CONTEXT: validate_haiku_handback is defined\nstatus: ok",
    "empty": "",
    "echoed_prompt": ROLE.haiku_prompt("/repo"),
    "status_ok_without_symbol": '{"status": "ok", "discovery_summary": "the function is defined"}',
    "status_ok_japanese_undefined": '{"status": "ok", "discovery_summary": "validate_haiku_handback は未定義"}',
}


@pytest.mark.parametrize("label", sorted(_HAIKU_NEGATIVES))
def test_validate_haiku_handback_rejects_non_confirming_reports(label):
    assert ROLE.validate_haiku_handback(_HAIKU_NEGATIVES[label]) is False, label


def test_validate_haiku_handback_accepts_the_live_ok_defined_shape_and_japanese_variant():
    assert ROLE.validate_haiku_handback(_HAIKU_OK_HANDBACK) is True
    assert ROLE.validate_haiku_handback("status: ok\nvalidate_haiku_handback は定義されています") is True


def test_role_subagent_real_shape_handback_is_taken_from_subagent_handback_tool_use():
    haiku = _evaluate_role(
        _real_shape_role_stream(ROLE.HAIKU_AGENT, handback=_HAIKU_OK_HANDBACK),
        ROLE.HAIKU_AGENT, ROLE.validate_haiku_handback)
    assert haiku["ok"] is True and haiku["parent_handback"] is True, haiku
    sonnet = _evaluate_role(
        _real_shape_role_stream(ROLE.SONNET_AGENT, handback='{"assessment": "clear", "findings": []}'),
        ROLE.SONNET_AGENT, ROLE.validate_sonnet_handback)
    assert sonnet["ok"] is True, sonnet


@pytest.mark.parametrize(
    "label, kwargs",
    [
        ("subagent_handback_absent", dict(handback=None)),
        ("agent_tool_result_missing", dict(handback=_HAIKU_OK_HANDBACK, agent_result=False)),
        ("agent_tool_result_is_error", dict(handback=_HAIKU_OK_HANDBACK, agent_is_error=True)),
        ("handback_tool_result_failed", dict(handback=_HAIKU_OK_HANDBACK, handback_success=False)),
        ("handback_belongs_to_another_agent_call", dict(handback=_HAIKU_OK_HANDBACK, handback_parent="toolu_other")),
        ("context_starved_handback", dict(handback="INSUFFICIENT_CONTEXT")),
        ("requested_result_missing", dict(handback="I could not find the value.")),
        ("not_defined", dict(handback=_HAIKU_NEGATIVES["not_defined"])),
        ("status_inconclusive", dict(handback=_HAIKU_NEGATIVES["status_inconclusive"])),
        ("status_failed", dict(handback=_HAIKU_NEGATIVES["status_failed"])),
        ("echoed_prompt_without_result", dict(handback=_HAIKU_NEGATIVES["echoed_prompt"])),
        # prompt (Agent tool_use input) carries the requested value, but nothing was handed back.
        ("value_only_in_dispatch_prompt", dict(handback=None, prompt_text="validate_haiku_handback is defined")),
    ],
)
def test_role_subagent_real_shape_negative_controls_are_never_a_pass(label, kwargs):
    result = _evaluate_role(
        _real_shape_role_stream(ROLE.HAIKU_AGENT, **kwargs), ROLE.HAIKU_AGENT, ROLE.validate_haiku_handback)
    assert result["ok"] is False, (label, result)


def _evaluate_role(stdout: str, agent_type: str, validator) -> dict:
    return ROLE.evaluate_role_subagent(
        stdout, agent_type, validator, hook_events=MODULE.extract_claude_hook_lifecycle_events(stdout))


def test_role_subagent_handback_normal_controls_pass():
    haiku = _evaluate_role(
        _role_stream(ROLE.HAIKU_AGENT, handback=_HAIKU_OK_HANDBACK),
        ROLE.HAIKU_AGENT, ROLE.validate_haiku_handback)
    assert haiku["ok"] is True, haiku
    sonnet = _evaluate_role(
        _role_stream(ROLE.SONNET_AGENT, handback='{"assessment": "clear", "findings": []}'),
        ROLE.SONNET_AGENT, ROLE.validate_sonnet_handback)
    assert sonnet["ok"] is True, sonnet


@pytest.mark.parametrize(
    "label, kwargs",
    [
        ("dispatch_only_no_completion", dict(handback=None, stop=False)),
        ("no_parent_handback", dict(handback=None)),
        ("context_starved_stop", dict(handback="INSUFFICIENT_CONTEXT")),
        ("requested_result_missing", dict(handback="I could not find the value.")),
        ("status_inconclusive", dict(handback=_HAIKU_NEGATIVES["status_inconclusive"])),
        ("not_defined", dict(handback=_HAIKU_NEGATIVES["not_defined"])),
        ("spawn_never_started", dict(handback=_HAIKU_OK_HANDBACK, start=False)),
        ("stop_precedes_start", dict(handback=_HAIKU_OK_HANDBACK, stop_before_start=True)),
        ("different_subagent_type_requested", dict(handback=_HAIKU_OK_HANDBACK, call_type="general-purpose")),
    ],
)
def test_role_subagent_handback_negative_controls_are_never_a_pass(label, kwargs):
    result = _evaluate_role(_role_stream(ROLE.HAIKU_AGENT, **kwargs), ROLE.HAIKU_AGENT, ROLE.validate_haiku_handback)
    assert result["ok"] is False, (label, result)


def test_role_subagent_sonnet_handback_requires_the_semantic_review_object():
    for text in ("Looks fine to me.", '{"verdict": "clear"}', "INSUFFICIENT_CONTEXT"):
        result = _evaluate_role(
            _role_stream(ROLE.SONNET_AGENT, handback=text), ROLE.SONNET_AGENT, ROLE.validate_sonnet_handback)
        assert result["ok"] is False, (text, result)
    accepted = _evaluate_role(
        _role_stream(ROLE.SONNET_AGENT, handback='{"assessment": "findings", "findings": []}'),
        ROLE.SONNET_AGENT, ROLE.validate_sonnet_handback)
    assert accepted["ok"] is True


def test_role_subagent_agents_resolve_to_the_pinned_role_aliases():
    for agent, role in ((ROLE.HAIKU_AGENT, "haiku"), (ROLE.SONNET_AGENT, "sonnet")):
        text = (REPO_ROOT / ".claude" / "agents" / f"{agent}.md").read_text(encoding="utf-8")
        assert f"model: {role}" in text, agent


def test_live_wrapper_skip_is_explicit_and_never_adjudicated_as_pass(tmp_path, monkeypatch):
    # (1) CI: an explicit skip whose reason says it is NOT a pass.
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.skip.Exception) as skipped:
        _run_live_wrapper("native", out_root=tmp_path)
    assert SKIP_REASON_TOKEN in str(skipped.value)
    monkeypatch.delenv("CI", raising=False)

    # (2) Runner exit 77 is propagated as pytest.exit(returncode=77), not turned into a pass.
    fake_skip_runner = tmp_path / "fake_runner_skip.py"
    fake_skip_runner.write_text("import sys\nsys.exit(77)\n", encoding="utf-8")
    with pytest.raises(pytest.exit.Exception) as exited:
        _run_live_wrapper("native", runner_argv=[sys.executable, str(fake_skip_runner)], out_root=tmp_path)
    assert exited.value.returncode == 77
    assert SKIP_REASON_TOKEN in str(exited.value)

    # (3) A runner that exits 0 but leaves no evidence / a non-pass verdict / a different
    #     tested HEAD is never adjudicated as a pass.
    fake_ok_runner = tmp_path / "fake_runner_ok.py"
    fake_ok_runner.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        _run_live_wrapper("native", runner_argv=[sys.executable, str(fake_ok_runner)], out_root=tmp_path)
    head = _git_head()
    base = {
        "schema": "NAMED_SUBAGENT_RESUME_EVIDENCE_V1", "adapter": "native", "verdict": "pass",
        "failure_layer": None, "tested_head": head, "runner_exit_code": 0,
    }
    good = tmp_path / "good.json"
    good.write_text(json.dumps(base), encoding="utf-8")
    assert _adjudicate_live_evidence(good, adapter="native", expected_head=head)["verdict"] == "pass"
    for label, mutation in (
        ("skip verdict", {"verdict": "skip"}),
        ("fail verdict", {"verdict": "fail", "failure_layer": "unclassified"}),
        ("stale head", {"tested_head": "0" * 40}),
        ("wrong adapter", {"adapter": "claude-gpt"}),
        ("layer set", {"failure_layer": "hook_lifecycle"}),
        ("runner exit", {"runner_exit_code": 77}),
    ):
        bad = tmp_path / f"bad-{label.replace(' ', '-')}.json"
        bad.write_text(json.dumps({**base, **mutation}), encoding="utf-8")
        with pytest.raises(AssertionError):
            _adjudicate_live_evidence(bad, adapter="native", expected_head=head)


# ---------------------------------------------------------------------------
# AC6 (runner side only; resolver semantics stay in tests/task-context/)
# ---------------------------------------------------------------------------


def test_runner_correlates_unique_named_subagent_resume():
    """(a) unique own-session named SubAgent -> name resume is a PASS with the exact agent ID."""
    chain = evaluate(full_chain())
    assert chain["verdict"] == "pass" and chain["chain_break"] is None
    assert chain["addressing"] == "name" and chain["agent_name"] == NAME and chain["agent_id"] == "agent-A"
    assert all(chain["steps"].values())

    # Controls differing from the normal stream by exactly one fact are never a pass.
    # (i) resume accepted but a DIFFERENT agent was resumed (new child, same marker)
    other = Stream()
    other.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    other.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    other.send_call("toolu_send_1", NAME).pretool_send(NAME)
    other.send_result("toolu_send_1", resumed="agent-NEW", pin_name=NAME)
    other.start("agent-NEW").child_text(SECOND).stop("agent-NEW").notification("agent-NEW")
    other.parent_text(SECOND)
    mismatch = evaluate(other)
    assert mismatch["verdict"] != "pass" and mismatch["chain_break"] == "resumed_agent_id_mismatch"
    # (ii) a fresh Agent invocation produces the same marker instead of a resume
    fresh = Stream()
    fresh.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    fresh.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    fresh.send_call("toolu_send_1", NAME).pretool_send(NAME).send_result("toolu_send_1", resumed="agent-A")
    fresh.agent_call("toolu_agent_2", "other-name").start("agent-B").child_text(SECOND).stop("agent-B")
    fresh.start("agent-A").stop("agent-A").notification("agent-A").parent_text(SECOND)
    fresh_chain = evaluate(fresh)
    assert fresh_chain["verdict"] != "pass" and fresh_chain["chain_break"] in {
        "new_agent_invocation_observed", "resume_marker_not_from_child"}
    # (iii) the resume marker only appears in the parent's own text (parroted), never from the child
    parroted = full_chain()
    parroted_text = parroted.text().replace(json.dumps({"message": SECOND}), json.dumps({"message": "x"}))
    parroted_chain = MODULE.evaluate_named_subagent_resume_chain(
        MODULE.extract_named_subagent_resume_observations(parroted_text))
    assert parroted_chain["verdict"] != "pass" and parroted_chain["chain_break"] == "resume_marker_not_from_child"
    # (iv) parent text before the resume completion notification is not a retrieval
    early = Stream()
    early.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    early.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    early.send_call("toolu_send_1", NAME).pretool_send(NAME).send_result("toolu_send_1", resumed="agent-A")
    early.start("agent-A").child_text(SECOND).stop("agent-A")
    # the parent only ANNOUNCES the marker it is waiting for, before the resume completion
    # notification re-wakes it; there is no parent text after the notification at all
    early.parent_text(f"still waiting for {SECOND}")
    early.notification("agent-A")
    early_chain = evaluate(early)
    assert early_chain["verdict"] != "pass"
    assert early_chain["chain_break"] == "parent_did_not_retrieve_resume_result"
    # (v) a non-allow decision from a PreToolUse(SendMessage) hook (the false ASK) is never a pass
    asked = evaluate(full_chain(pretool_decision="ask"))
    assert asked["verdict"] != "pass" and asked["chain_break"] == "pretooluse_sendmessage_non_allow_decision"
    assert any(d["decision"] == "ask" for d in asked["sendmessage_decisions"])


def test_runner_nameless_agent_id_lane_not_broken():
    """(b) name-less SubAgent -> SendMessage to a valid agent ID is correlated as the agent-ID lane;
    it is neither mis-reported as an unrecorded name nor promoted to a name-resume PASS."""
    chain = evaluate(full_chain(name=None, agent_id="agent-N"))
    assert chain["addressing"] == "agent_id" and chain["agent_id"] == "agent-N"
    assert chain["agent_id_lane"] == {"agent_id": "agent-N", "accepted": True, "resumed_same_agent_id": True}
    assert chain["chain_break"] == "agent_id_lane_only_not_name_resume"
    assert chain["verdict"] != "pass"
    assert chain["steps"]["sendmessage_to_name_issued"] is False
    assert chain["steps"]["resume_completion"] and chain["steps"]["parent_retrieved_resume_result"]
    # an invalid agent ID is not the lane
    bogus = evaluate(full_chain(name=None, agent_id="agent-N", send_to="agent-DOES-NOT-EXIST"))
    assert bogus["agent_id_lane"]["accepted"] is False and bogus["addressing"] is None
    assert bogus["chain_break"] == "agent_call_without_name"


def test_runner_other_session_same_name_not_false_pass():
    """(c) a same name recorded only by ANOTHER caller session must not resolve."""
    stream = Stream("sess-main")
    stream.agent_call("toolu_agent_1", "mine")
    stream.post_agent("toolu_agent_1", "mine", "agent-MINE")
    stream.start("agent-MINE").child_text(FIRST).stop("agent-MINE")
    # another session's PostToolUse:Agent recorded `shared-name` -> agent-OTHER
    stream.post_agent("toolu_other", "shared-name", "agent-OTHER", session="sess-other")
    stream.send_call("toolu_send_1", "shared-name").pretool_send("shared-name")
    # even if the harness reports a successful resume of the OTHER session's agent
    stream.send_result("toolu_send_1", resumed="agent-OTHER", pin_name="shared-name")
    stream.start("agent-OTHER").child_text(SECOND).stop("agent-OTHER").notification("agent-OTHER")
    stream.parent_text(SECOND)
    chain = evaluate(stream)
    assert chain["verdict"] != "pass"
    assert chain["chain_break"] == "sendmessage_target_not_recorded_name"
    assert chain["agent_id"] is None and chain["addressing"] is None
    # control: the SAME record in the caller session resolves normally
    own = Stream("sess-main")
    own.agent_call("toolu_agent_1", "shared-name").start("agent-OTHER").child_text(FIRST).stop("agent-OTHER")
    own.post_agent("toolu_agent_1", "shared-name", "agent-OTHER", handback=FIRST)
    own.send_call("toolu_send_1", "shared-name").pretool_send("shared-name")
    own.send_result("toolu_send_1", resumed="agent-OTHER", pin_name="shared-name")
    own.start("agent-OTHER").child_text(SECOND).stop("agent-OTHER").notification("agent-OTHER").parent_text(SECOND)
    assert evaluate(own)["verdict"] == "pass"


def test_runner_same_session_name_collision_not_auto_resolved():
    """(d) two agents under one name in the SAME session: never silently resolved to one."""
    stream = Stream()
    stream.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A1", handback=FIRST)
    stream.start("agent-A1").child_text(FIRST).stop("agent-A1")
    stream.agent_call("toolu_agent_2", NAME).post_agent("toolu_agent_2", NAME, "agent-A2", handback=FIRST)
    stream.start("agent-A2").child_text(FIRST).stop("agent-A2")
    stream.send_call("toolu_send_1", NAME).pretool_send(NAME)
    stream.send_result("toolu_send_1", resumed="agent-A2", pin_name=NAME)
    stream.start("agent-A2").child_text(SECOND).stop("agent-A2").notification("agent-A2").parent_text(SECOND)
    chain = evaluate(stream)
    assert chain["verdict"] != "pass"
    assert chain["chain_break"] == "name_collision_same_session"
    assert chain["agent_id"] is None
    # collision is unattributable -> unclassified, never a pass
    obs = MODULE.extract_named_subagent_resume_observations(stream.text())
    assert MODULE.classify_named_subagent_resume_failure_layer(obs, chain, adapter="native") == "unclassified"
    # a second Agent CALL under the same name collides even before it completed
    pending = Stream()
    pending.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A1", handback=FIRST)
    pending.start("agent-A1").child_text(FIRST).stop("agent-A1")
    pending.agent_call("toolu_agent_2", NAME)
    pending.send_call("toolu_send_1", NAME)
    assert evaluate(pending)["chain_break"] == "name_collision_same_session"


def test_runner_unrecorded_name_or_agent_type_only_not_counted_as_name():
    """(e) an unrecorded name, or an agent_type-only match, is never an addressable name."""
    unrecorded = evaluate(full_chain(send_to="ghost-worker"))
    assert unrecorded["verdict"] != "pass" and unrecorded["addressing"] is None
    assert unrecorded["chain_break"] == "sendmessage_target_not_recorded_name"
    assert unrecorded["agent_id"] is None
    type_only = evaluate(full_chain(send_to="general-purpose"))
    assert type_only["verdict"] != "pass" and type_only["addressing"] is None
    assert type_only["chain_break"] == "sendmessage_target_is_agent_type_only"
    assert type_only["agent_id"] is None
    # control: the recorded name itself resolves
    assert evaluate(full_chain(send_to=NAME))["addressing"] == "name"
    # agent_type-only is attributed to model emission, an unrecorded name is not guessed
    obs = MODULE.extract_named_subagent_resume_observations(full_chain(send_to="general-purpose").text())
    assert MODULE.classify_named_subagent_resume_failure_layer(
        obs, type_only, adapter="native") == "backend_model_emission"


# ---------------------------------------------------------------------------
# AC7
# ---------------------------------------------------------------------------


def _classify(stream: Stream, *, adapter="native", receipt=None, stderr="", exit_code=0):
    obs = MODULE.extract_named_subagent_resume_observations(stream.text())
    chain = MODULE.evaluate_named_subagent_resume_chain(obs)
    return chain, MODULE.classify_named_subagent_resume_failure_layer(
        obs, chain, adapter=adapter, launcher_receipt=receipt, process_exit_code=exit_code, stderr_text=stderr)


def test_failure_layer_classification_never_passes_unclassified():
    layers = set(MODULE.NAMED_RESUME_FAILURE_LAYERS)
    assert layers == {
        "client_schema", "launcher_config", "proxy_translation", "backend_model_emission",
        "hook_lifecycle", "unclassified"}

    chain, layer = _classify(full_chain())
    assert chain["verdict"] == "pass" and layer is None  # a pass carries no failure layer

    # client schema: the client rejected the tool input
    schema_stream = Stream()
    schema_stream.agent_call("toolu_agent_1", NAME)
    schema_stream.add({
        "type": "user", "session_id": "sess-main", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_agent_1", "is_error": True,
             "content": "InputValidationError: unexpected parameter `name`"}]}})
    schema_stream.post_agent("toolu_agent_1", None, "agent-A").start("agent-A").stop("agent-A")
    assert _classify(schema_stream)[1] == "client_schema"
    # client schema: the SendMessage tool is not exposed by the client at all
    no_send_tool = Stream(tools=("Task", "Bash"))
    no_send_tool.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A")
    no_send_tool.start("agent-A").child_text(FIRST).stop("agent-A")
    assert _classify(no_send_tool)[1] == "client_schema"

    # launcher config: launcher refused before Claude Code ever started
    empty = Stream()
    empty.lines = []
    chain_e, layer_e = _classify(
        empty, adapter="claude-gpt", receipt={"status": "blocked", "reason": "preflight_failed"}, exit_code=2)
    assert chain_e["verdict"] != "pass" and layer_e == "launcher_config"

    # proxy translation: API / tool-schema translation error surfaced on the claude-gpt lane
    proxy_stream = Stream()
    proxy_stream.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A")
    proxy_stream.start("agent-A").child_text(FIRST).stop("agent-A")
    proxy_stream.result(is_error=True, result="API Error: 400 invalid_request_error: tool schema", api_error_status=400)
    assert _classify(proxy_stream, adapter="claude-gpt")[1] == "proxy_translation"
    # the same stream on the native adapter is not blamed on the proxy
    assert _classify(proxy_stream, adapter="native")[1] != "proxy_translation"

    # backend model emission: the Agent call carried no name / no SendMessage was ever emitted
    nameless = Stream()
    nameless.agent_call("toolu_agent_1", None).post_agent("toolu_agent_1", None, "agent-A")
    nameless.start("agent-A").child_text(FIRST).stop("agent-A")
    assert _classify(nameless)[1] == "backend_model_emission"
    no_send = Stream()
    no_send.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A")
    no_send.start("agent-A").child_text(FIRST).stop("agent-A")
    assert _classify(no_send)[1] == "backend_model_emission"

    # hook lifecycle: the resume was accepted but no resume SubagentStart / Stop followed
    no_resume_hooks = Stream()
    no_resume_hooks.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    no_resume_hooks.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    no_resume_hooks.send_call("toolu_send_1", NAME).pretool_send(NAME)
    no_resume_hooks.send_result("toolu_send_1", resumed="agent-A", pin_name=NAME)
    chain_h, layer_h = _classify(no_resume_hooks)
    assert chain_h["chain_break"] == "resume_subagent_start_missing" and layer_h == "hook_lifecycle"
    # hook lifecycle: a false ASK from the PreToolUse(SendMessage) hook
    assert _classify(full_chain(pretool_decision="ask"))[1] == "hook_lifecycle"

    # unclassified: a failure no rule can attribute; it is a FAIL and never a PASS
    mismatch = Stream()
    mismatch.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    mismatch.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    mismatch.send_call("toolu_send_1", NAME).pretool_send(NAME)
    mismatch.send_result("toolu_send_1", resumed="agent-ELSE", pin_name=NAME)
    chain_u, layer_u = _classify(mismatch)
    assert chain_u["verdict"] == "fail" and layer_u == "unclassified"
    evidence_u = _evidence(mismatch)
    assert evidence_u["verdict"] == "fail" and evidence_u["failure_layer"] == "unclassified"
    assert evidence_u["failure_layer"] in layers
    # a fail verdict can never carry no layer, and a pass can never carry one
    for stream in (no_send, nameless, mismatch, no_resume_hooks, full_chain(pretool_decision="ask")):
        ev = _evidence(stream)
        assert ev["verdict"] == "fail" and ev["failure_layer"] in layers
    assert _evidence(full_chain())["failure_layer"] is None
    # a timed-out run is never a pass even when the stream looks complete
    timed = _evidence(full_chain(), timed_out=True)
    assert timed["verdict"] == "fail" and timed["failure_layer"] == "unclassified"

    # unobservable causal evidence (#2846 no_evidence style) is SKIP, never PASS and never a layer
    unobservable = Stream()
    unobservable.agent_call("toolu_agent_1", NAME)
    unobservable.send_call("toolu_send_1", NAME)
    chain_s, layer_s = _classify(unobservable)
    assert chain_s["verdict"] == "skip" and chain_s["chain_break"] == "subagent_causal_observation_unavailable"
    assert layer_s is not None  # never promoted to pass
    assert _evidence(unobservable)["verdict"] == "skip"


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------

_NATIVE_RECORDED = {
    "adapter": "native", "verdict": "pass", "tested_head": "a" * 40, "claude_code_version": "2.1.287",
    "model_route": "claude-sonnet-5-5", "proxy_version": None, "launcher_sha256": None,
    "fixture_sha256": "f" * 64, "compat_note_sha256": "c" * 64, "runner_exit_code": 0,
}
_GPT_RECORDED = {
    **_NATIVE_RECORDED, "adapter": "claude-gpt", "model_route": "gpt-6-sol[1m]",
    "proxy_version": "claude-code-proxy 0.1.42", "launcher_sha256": "l" * 64,
}


def _current(recorded: dict, **overrides) -> dict:
    current = {
        "head": "b" * 40, "changed_paths": [], "changed_paths_base": recorded["tested_head"],
        "claude_code_version": recorded["claude_code_version"], "model_route": recorded["model_route"],
        "fixture_sha256": recorded["fixture_sha256"], "compat_note_sha256": recorded["compat_note_sha256"],
        "proxy_version": recorded["proxy_version"], "launcher_sha256": recorded["launcher_sha256"],
    }
    current.update(overrides)
    return current


def test_freshness_rule_recaptures_only_on_result_affecting_change():
    reuse = MODULE.evaluate_evidence_freshness
    # unrelated commits only: reuse (never a recapture reason on their own)
    unrelated = ["docs/dev/other.md", "src/game/foo.ts", "scripts/ci/x.py", ".claude/skills/other/SKILL.md"]
    for recorded in (_NATIVE_RECORDED, _GPT_RECORDED):
        verdict = reuse(recorded, _current(recorded, changed_paths=unrelated))
        assert verdict["reusable"] is True and verdict["reasons"] == []
    assert reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, head=_NATIVE_RECORDED["tested_head"]))["reusable"]

    # the claude-gpt-only path re-captures ONLY the claude-gpt canary
    gpt_only = ["scripts/claude-gpt/launch.sh"]
    assert reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, changed_paths=gpt_only))["reusable"] is True
    fresh = reuse(_GPT_RECORDED, _current(_GPT_RECORDED, changed_paths=gpt_only))
    assert fresh["reusable"] is False and fresh["affected_adapters"] == ["claude-gpt"]

    # every both-adapter path re-captures both canaries
    for path in (
        "scripts/agent-ops/run_worktree_agent_runtime_smoke.py",
        ".claude/skills/worktree-agent-runtime-smoke/fixtures/named-subagent-resume.compat.md",
        ".claude/settings.json", ".claude/hooks/task_context/hook_entry.py",
        "scripts/task-context/task_context_runtime_smoke_verifier.py",
    ):
        for recorded in (_NATIVE_RECORDED, _GPT_RECORDED):
            verdict = reuse(recorded, _current(recorded, changed_paths=["docs/x.md", path]))
            assert verdict["reusable"] is False, path
            assert verdict["affected_adapters"] == ["claude-gpt", "native"]
            assert verdict["result_affecting_changed_paths"] == [path]
            assert f"result_affecting_path_changed:{path}" in verdict["reasons"]
    # near-miss paths outside the closed allowlist are not result-affecting
    for path in ("scripts/agent-ops/run_worktree_agent_runtime_smoke_helper.py", "scripts/claude-gpt-docs/x.md",
                 ".claude/settings.local.json", "scripts/task-contextual/x.py"):
        assert reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, changed_paths=[path]))["reusable"] is True, path

    # version / route / hash drift forces a recapture
    drift = {
        "claude_code_version": "2.1.290", "model_route": "other-model",
        "fixture_sha256": "e" * 64, "compat_note_sha256": "d" * 64,
    }
    for key, value in drift.items():
        verdict = reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, **{key: value}))
        assert verdict["reusable"] is False and f"{key}_changed" in verdict["reasons"]
    for key, value in {"proxy_version": "claude-code-proxy 0.1.50", "launcher_sha256": "9" * 64}.items():
        verdict = reuse(_GPT_RECORDED, _current(_GPT_RECORDED, **{key: value}))
        assert verdict["reusable"] is False and f"{key}_changed" in verdict["reasons"]
        # a proxy / launcher drift does not invalidate the Native canary
        assert reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, **{key: value}))["reusable"] is True

    # fail closed: missing current-side keys, wrong diff base, non-pass or foreign recorded evidence
    for key in ("head", "changed_paths", "claude_code_version", "model_route", "fixture_sha256", "compat_note_sha256"):
        broken = _current(_NATIVE_RECORDED)
        broken.pop(key)
        verdict = reuse(_NATIVE_RECORDED, broken)
        assert verdict["reusable"] is False and f"current_key_missing:{key}" in verdict["reasons"], key
    for key in ("proxy_version", "launcher_sha256"):
        broken = _current(_GPT_RECORDED)
        broken.pop(key)
        assert f"current_key_missing:{key}" in reuse(_GPT_RECORDED, broken)["reasons"]
    wrong_base = reuse(_NATIVE_RECORDED, _current(_NATIVE_RECORDED, changed_paths_base="9" * 40))
    assert wrong_base["reusable"] is False and "changed_paths_base_mismatch" in wrong_base["reasons"]
    assert reuse({**_NATIVE_RECORDED, "verdict": "skip"}, _current(_NATIVE_RECORDED))["reusable"] is False
    assert reuse({**_NATIVE_RECORDED, "adapter": "other"}, _current(_NATIVE_RECORDED))["reusable"] is False
    assert reuse({**_NATIVE_RECORDED, "tested_head": None}, _current(_NATIVE_RECORDED))["reusable"] is False


def test_compute_changed_paths_uses_git_diff_name_only(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.c",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.c"}

    def git(*args):
        return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, env=env)

    git("init", "-b", "main")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "one")
    base = git("rev-parse", "HEAD").stdout.strip()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "b.txt").write_text("2", encoding="utf-8")
    git("add", "scripts/b.txt")
    git("commit", "-m", "two")
    head = git("rev-parse", "HEAD").stdout.strip()
    assert MODULE.compute_changed_paths(str(repo), base, head) == ["scripts/b.txt"]
    assert MODULE.compute_changed_paths(str(repo), base, "0" * 40) is None


# ---------------------------------------------------------------------------
# AC9
# ---------------------------------------------------------------------------


def test_public_evidence_excludes_raw_prompt_and_transcript(tmp_path):
    sentinel_prompt = "RAW_PROMPT_SENTINEL_do_not_persist"
    sentinel_message = "RAW_MESSAGE_SENTINEL_do_not_persist"
    credential = "sk-ant-api03-" + "A" * 48
    home_path = "/home/someone/.claude/projects/x/transcript.jsonl"
    stream = Stream()
    stream.agent_call("toolu_agent_1", NAME, prompt=sentinel_prompt)
    stream.start("agent-A").child_text(f"{FIRST} {sentinel_message} {credential}").stop("agent-A")
    stream.post_agent("toolu_agent_1", NAME, "agent-A", handback=f"{FIRST} {sentinel_message}")
    stream.add({"type": "user", "session_id": "sess-main", "message": {"role": "user", "content": [
        {"type": "text", "text": sentinel_prompt}]}, "parent_tool_use_id": "toolu_agent_1"})
    stream.send_call("toolu_send_1", NAME)
    # the hook payload echoes a HOME transcript path (as the live runtime does)
    stream._hook("PreToolUse", "PreToolUse:SendMessage", {
        "hook_event_name": "PreToolUse", "session_id": "sess-main", "tool_name": "SendMessage",
        "tool_input": {"to": NAME}, "transcript_path": home_path, "tool_use_id": "toolu_send_1"})
    stream.send_result("toolu_send_1", resumed="agent-A", pin_name=NAME)
    stream.start("agent-A").child_text(f"{SECOND} {sentinel_message}").stop("agent-A").notification("agent-A")
    stream.parent_text(f"{SECOND} {sentinel_message} {credential}")
    evidence = _evidence(stream, stderr=f"launcher={home_path} proxy=claude-code-proxy 0.1.42")
    assert evidence["verdict"] == "pass"
    blob = json.dumps(evidence)
    for forbidden in (sentinel_prompt, sentinel_message, credential, home_path, "/home/", "transcript",
                      "last_assistant_message", "agent_transcript_path"):
        assert forbidden not in blob, forbidden
    assert MODULE._nr_public_path(str(Path.home() / ".local" / "bin" / "claude-code-proxy")) == (
        "~/.local/bin/claude-code-proxy")
    assert "/home/" not in (MODULE._nr_public_path("/home/someone/x/proxy", repo_root="/repo") or "")


# ---------------------------------------------------------------------------
# Hermetic end-to-end through main(): exit-code mapping and launcher resolution
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


@pytest.fixture()
def hermetic_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    launcher = repo / "scripts" / "claude-gpt" / "launch.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _git("add", "README.md", "scripts/claude-gpt/launch.sh", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return worktree


def _write_exe(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_main(tmp_path: Path, worktree: Path, stream: Stream | None, *, extra: list[str], name: str,
              adapter: str = "native") -> tuple[int, dict | None, Path]:
    """Run main() against a fake ``claude`` that records argv/env and replays ``stream``."""
    record = tmp_path / f"{name}-record.txt"
    replay = tmp_path / f"{name}-stream.jsonl"
    replay.write_text(stream.text() if stream is not None else "", encoding="utf-8")
    fake = tmp_path / f"{name}-fake-claude"
    _write_exe(
        fake,
        f'if [ "$1" = "--version" ]; then echo "2.1.287 (Claude Code)"; exit 0; fi\n'
        f'{{ printf "ARGV:"; printf " %s" "$@"; printf "\\n"; '
        f'printf "HOOKS=%s\\n" "${{CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}}"; }} > "{record}"\n'
        f'cat > /dev/null\ncat "{replay}"',
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the smoke\n", encoding="utf-8")
    evidence_json = tmp_path / f"{name}-evidence.json"
    argv = [
        "--runtime", "claude", "--mode", "structured", "--worktree", str(worktree),
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / f"{name}-out"),
        "--named-subagent-resume", "--named-resume-evidence-json", str(evidence_json),
        "--repo-root", str(worktree.parent.parent.parent), "--claude-adapter", adapter, *extra,
    ]
    if adapter == "native":
        argv += ["--claude-bin", str(fake)]
    code = MODULE.main(argv)
    evidence = json.loads(evidence_json.read_text(encoding="utf-8")) if evidence_json.exists() else None
    return code, evidence, record


def test_main_maps_chain_verdict_to_exit_code_and_writes_public_evidence(tmp_path, hermetic_worktree):
    compat = tmp_path / "compat.md"
    compat.write_text("compat note\n", encoding="utf-8")
    code, evidence, record = _run_main(
        tmp_path, hermetic_worktree, full_chain(), name="pass",
        extra=["--append-system-prompt-file", str(compat)])
    assert code == 0 and evidence["verdict"] == "pass" and evidence["runner_exit_code"] == 0
    argv_line = record.read_text(encoding="utf-8")
    assert "--no-session-persistence" not in argv_line
    assert "--append-system-prompt-file" in argv_line and str(compat) in argv_line
    assert "--permission-mode" not in argv_line and "--dangerously-skip-permissions" not in argv_line
    # the same fake with a broken chain is a FAIL (exit 1), never PASS
    broken = full_chain(pretool_decision="ask")
    code, evidence, _ = _run_main(tmp_path, hermetic_worktree, broken, name="fail", extra=[])
    assert code == 1 and evidence["verdict"] == "fail" and evidence["failure_layer"] == "hook_lifecycle"
    assert evidence["runner_exit_code"] == 1 and evidence["false_ask_observed"] is True
    # an unobservable chain is SKIP 77, never PASS
    unobservable = Stream()
    unobservable.agent_call("toolu_agent_1", NAME)
    unobservable.result()
    code, evidence, _ = _run_main(tmp_path, hermetic_worktree, unobservable, name="skip", extra=[])
    assert code == 77 and evidence["verdict"] == "skip" and evidence["runner_exit_code"] == 77


def test_main_resolves_repo_launcher_and_forwards_only_the_fixed_scenario_overlay(tmp_path, hermetic_worktree):
    compat = tmp_path / "compat.md"
    compat.write_text("compat note\n", encoding="utf-8")
    launcher = hermetic_worktree / "scripts" / "claude-gpt" / "launch.sh"
    assert launcher.is_file()
    replay = tmp_path / "gpt-stream.jsonl"
    replay.write_text(full_chain().text(), encoding="utf-8")
    record = tmp_path / "gpt-record.txt"
    _write_exe(
        launcher,
        f'if [ "$1" = "--version" ]; then echo "launcher 1"; exit 0; fi\n'
        f'{{ printf "ARGV:"; printf " %s" "$@"; printf "\\n"; '
        f'printf "HOOKS=%s\\n" "${{CLAUDE_GPT_RUNTIME_SMOKE_HOOKS:-}}"; }} > "{record}"\n'
        f'cat > /dev/null\ncat "{replay}"',
    )
    prompt = tmp_path / "prompt.md"
    prompt.write_text("p\n", encoding="utf-8")
    evidence_json = tmp_path / "gpt-evidence.json"
    code = MODULE.main([
        "--runtime", "claude", "--mode", "structured", "--worktree", str(hermetic_worktree),
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "gpt-out"),
        "--named-subagent-resume", "--claude-adapter", "claude-gpt",
        "--append-system-prompt-file", str(compat), "--named-resume-evidence-json", str(evidence_json),
        "--repo-root", str(hermetic_worktree.parent.parent.parent),
    ])
    assert code == 0
    text = record.read_text(encoding="utf-8")
    # Issue #2925: no launcher-owned env channel; the fixed scenario overlay is forwarded after ``--``.
    assert "HOOKS=\n" in text
    argv_line = next(line for line in text.splitlines() if line.startswith("ARGV:"))
    assert argv_line.split()[1] == "--"
    assert "--settings" in argv_line and "--permission-mode" not in argv_line
    assert "--no-session-persistence" not in argv_line
    assert f"--append-system-prompt-file {compat}" in argv_line
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["launcher"]["path"] == "scripts/claude-gpt/launch.sh"
    assert evidence["launcher"]["sha256"] == MODULE._nr_file_sha256(str(launcher))
    assert str(hermetic_worktree) not in json.dumps(evidence)


def test_cli_rejects_invalid_scenario_flag_combinations(tmp_path, hermetic_worktree, capsys):
    prompt = tmp_path / "p.md"
    prompt.write_text("p\n", encoding="utf-8")
    compat = tmp_path / "c.md"
    compat.write_text("c\n", encoding="utf-8")
    base = ["--runtime", "claude", "--worktree", str(hermetic_worktree), "--prompt-file", str(prompt),
            "--output-dir", str(tmp_path / "o")]
    cases = [
        ["--mode", "structured", "--append-system-prompt-file", str(compat)],  # compat note without scenario
        ["--mode", "structured", "--named-resume-evidence-json", str(tmp_path / "e.json")],
        ["--mode", "interactive", "--named-subagent-resume"],
        ["--mode", "structured", "--named-subagent-resume", "--require-hook-chain-evidence"],
        ["--mode", "structured", "--named-subagent-resume", "--claude-agent-name", "x"],
        ["--mode", "structured", "--claude-adapter", "claude-gpt"],  # claude-gpt still needs --claude-bin w/o scenario
    ]
    for extra in cases:
        with pytest.raises(SystemExit) as exc:
            MODULE.main([*base, *extra])
        assert exc.value.code == 2, extra
    capsys.readouterr()


# ---------------------------------------------------------------------------
# PR #2879 fix_delta: every negative control below differs from the normal control
# (``_causal_control()``) by exactly one fact and must never be a PASS.
# ---------------------------------------------------------------------------

CALL_TUID = "toolu_agent_1"


def _causal_control(
    *, call_name: str | None = NAME, post_tuid: str = CALL_TUID, first_start: bool = True,
    resume_marker_position: str = "after_start", resume_marker_via: str = "child_text",
    first_marker_via: str = "child_text", foreign_child: bool = False, pretool: bool = True,
    pretool_kwargs: dict | None = None,
) -> Stream:
    """Spawn -> complete -> SendMessage(to=name) -> resume -> complete, with explicit identities.

    Defaults are the correct chain; each keyword flips exactly one fact."""
    stream = Stream()
    stream.agent_call(CALL_TUID, call_name)
    if first_start:
        stream.start("agent-A")
    if first_marker_via == "child_text":
        stream.child_text(FIRST)
    else:  # a tool_use whose *input* merely contains the marker (e.g. Grep(pattern=marker))
        stream.add({
            "type": "assistant", "parent_tool_use_id": CALL_TUID, "session_id": stream.session,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_grep_1", "name": "Grep", "input": {"pattern": FIRST}}]},
        })
    stream.stop("agent-A")
    stream.post_agent(post_tuid, NAME, "agent-A", handback=FIRST if first_marker_via == "child_text" else None)
    if foreign_child:
        # B is a different child: its own earlier Agent call and no SubagentStart/Stop hook.
        stream.agent_call("toolu_agent_2", "other-name")
    stream.send_call("toolu_send_1", NAME)
    if pretool:
        stream.pretool_send(NAME, **(pretool_kwargs or {}))
    stream.send_result("toolu_send_1", resumed="agent-A", pin_name=NAME)
    if resume_marker_position == "before_start":
        stream.child_text(SECOND)
    stream.start("agent-A")
    if foreign_child:
        # the resume marker comes from B's event, not from A's resumed run
        stream.child_text(SECOND, parent_tool_use_id="toolu_agent_2")
    elif resume_marker_position == "after_start":
        if resume_marker_via == "child_text":
            stream.child_text(SECOND)
        else:
            stream.add({
                "type": "assistant", "parent_tool_use_id": CALL_TUID, "session_id": stream.session,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_grep_2", "name": "Grep", "input": {"pattern": SECOND}}]},
            })
    stream.stop("agent-A")
    stream.notification("agent-A")
    stream.parent_text(f"resumed agent returned {SECOND}")
    stream.result()
    return stream


def test_causal_control_is_a_pass():
    assert evaluate(_causal_control())["verdict"] == "pass"


@pytest.mark.parametrize("label,kwargs", [
    ("agent_call_name_removed", {"call_name": None}),
    ("first_subagent_start_removed", {"first_start": False}),
    ("post_tool_use_tool_use_id_swapped", {"post_tuid": "toolu_other"}),
    ("resume_marker_before_resume_start", {"resume_marker_position": "before_start"}),
    ("resume_marker_only_in_grep_pattern", {"resume_marker_via": "tool_use_input"}),
    ("first_marker_only_in_grep_pattern", {"first_marker_via": "tool_use_input"}),
    ("resume_marker_from_other_child_event", {"foreign_child": True}),
])
def test_named_resume_chain_requires_identity_and_order_not_collected_steps(label, kwargs):
    stream = _causal_control(**kwargs)
    chain = evaluate(stream)
    assert chain["verdict"] != "pass", (label, chain["chain_break"], chain["steps"])
    assert chain["chain_break"] is not None, label
    # the persisted evidence mirrors it: never a pass, always a classified layer
    evidence = _evidence(stream)
    assert evidence["verdict"] != "pass", label
    assert evidence["failure_layer"] in set(MODULE.NAMED_RESUME_FAILURE_LAYERS), label


def test_text_blocks_do_not_treat_arbitrary_tool_use_inputs_as_child_results():
    grep = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "Grep", "input": {"pattern": SECOND}}]}
    bash = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": f"echo {SECOND}"}}]}
    assert not any(SECOND in str(block) for block in MODULE._nr_text_blocks(grep))
    assert not any(SECOND in str(block) for block in MODULE._nr_text_blocks(bash))
    handback = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "SubagentHandback", "input": {"message": SECOND}}]}
    assert any(SECOND in str(block) for block in MODULE._nr_text_blocks(handback))
    plain = {"role": "assistant", "content": [{"type": "text", "text": SECOND}]}
    assert any(SECOND in str(block) for block in MODULE._nr_text_blocks(plain))


def test_extracted_child_observations_keep_provenance():
    obs = MODULE.extract_named_subagent_resume_observations(_causal_control().text())
    assert obs["child_texts"], "child texts must be extracted"
    for record in obs["child_texts"]:
        assert record["parent_tool_use_id"] == CALL_TUID
        assert isinstance(record["index"], int)


# F2: PreToolUse:SendMessage hook normal / unobservable / execution failure -------------


@pytest.mark.parametrize("label,kwargs", [
    ("hook_observation_missing", {"pretool": False}),
    ("echo_hook_exit_1", {"pretool_kwargs": {"echo_exit_code": 1}}),
    ("project_hook_exit_1", {"pretool_kwargs": {"project_exit_code": 1}}),
    ("project_hook_outcome_error", {"pretool_kwargs": {"project_outcome": "error"}}),
    ("hook_response_only_for_another_tool_use_id", {"pretool_kwargs": {"tool_use_id": "toolu_other"}}),
])
def test_sendmessage_hook_missing_or_failed_is_never_a_pass(label, kwargs):
    stream = _causal_control(**kwargs)
    chain, layer = _classify(stream)
    assert chain["verdict"] != "pass", label
    assert layer == "hook_lifecycle", (label, chain["chain_break"], layer)
    evidence = _evidence(stream)
    assert evidence["verdict"] != "pass" and evidence["failure_layer"] == "hook_lifecycle", label
    # the SendMessage itself still went through: only this smoke's verdict is withheld
    if label != "hook_observation_missing":
        assert chain["steps"]["sendmessage_accepted"] is True


def test_sendmessage_hook_normal_observation_and_deliberate_decision():
    ok = evaluate(_causal_control())
    assert ok["verdict"] == "pass" and ok["hook_observation"]["status"] == "observed"
    # a deliberate hook decision keeps its own classification (existing behaviour kept)
    asked = evaluate(_causal_control(pretool_kwargs={"decision": "ask"}))
    assert asked["chain_break"] == "pretooluse_sendmessage_non_allow_decision"


def test_sendmessage_hook_failure_outside_the_send_window_is_not_blamed():
    stream = _causal_control()
    # a later, unrelated PreToolUse:SendMessage hook failure belongs to another call
    stream._hook("PreToolUse", "PreToolUse:SendMessage", None, exit_code=1, outcome="error")
    assert evaluate(stream)["verdict"] == "pass"


# F4: a normal launcher startup line must not change the failure layer ---------------------


def test_normal_proxy_startup_line_does_not_turn_hook_failure_into_proxy_translation():
    no_resume_hooks = Stream()
    no_resume_hooks.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    no_resume_hooks.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    no_resume_hooks.send_call("toolu_send_1", NAME).pretool_send(NAME)
    no_resume_hooks.send_result("toolu_send_1", resumed="agent-A", pin_name=NAME)
    startup = "launcher=/x/scripts/claude-gpt/launch.sh git=abc1234 dirty=false proxy=claude-code-proxy 0.1.42\n"
    bare = _classify(no_resume_hooks, adapter="claude-gpt", stderr="")
    with_startup = _classify(no_resume_hooks, adapter="claude-gpt", stderr=startup)
    assert bare[0]["chain_break"] == with_startup[0]["chain_break"] == "resume_subagent_start_missing"
    assert bare[1] == with_startup[1] == "hook_lifecycle"
    # the product name alone, outside the launcher's own startup line, is not evidence either
    product_only = _classify(no_resume_hooks, adapter="claude-gpt", stderr="claude-code-proxy listening\n")
    assert product_only[1] == "hook_lifecycle"
    # a real translation error on stderr keeps being attributed to the proxy
    translation = _classify(
        no_resume_hooks, adapter="claude-gpt",
        stderr=startup + "API Error: 400 invalid_request_error: tool schema strict function\n")
    assert translation[1] == "proxy_translation"
    # the launcher line is still the source of the recorded proxy version
    assert MODULE._nr_stderr_proxy_version(startup) == "claude-code-proxy 0.1.42"
    assert _evidence(no_resume_hooks, adapter="claude-gpt", stderr=startup)["failure_layer"] == "hook_lifecycle"


def _hook_failure_stream() -> Stream:
    stream = Stream()
    stream.agent_call("toolu_agent_1", NAME).start("agent-A").child_text(FIRST).stop("agent-A")
    stream.post_agent("toolu_agent_1", NAME, "agent-A", handback=FIRST)
    stream.send_call("toolu_send_1", NAME).pretool_send(NAME)
    stream.send_result("toolu_send_1", resumed="agent-A", pin_name=NAME)
    return stream


@pytest.mark.parametrize("status", [401, 429, 529, 500, 503])
def test_generic_api_error_status_is_not_attributed_to_proxy_translation_on_stderr(status):
    stream = _hook_failure_stream()
    stderr = f"API Error: {status} upstream said no\n"
    chain, layer = _classify(stream, adapter="claude-gpt", stderr=stderr)
    assert chain["chain_break"] == "resume_subagent_start_missing"
    # no translation evidence: the layer stays with the hook failure, never the proxy
    assert layer == "hook_lifecycle", (status, layer)


@pytest.mark.parametrize("status", [401, 429, 529, 500, 503])
def test_generic_api_error_status_is_not_attributed_to_proxy_translation_in_result_event(status):
    stream = _hook_failure_stream()
    stream.result(is_error=True, result=f"API Error: {status} upstream said no", api_error_status=status)
    chain, layer = _classify(stream, adapter="claude-gpt")
    assert chain["chain_break"] == "resume_subagent_start_missing"
    assert layer == "hook_lifecycle", (status, layer)
    # a result event whose text alone says so (no api_error_status field) is not translation either
    text_only = _hook_failure_stream()
    text_only.result(is_error=True, result=f"API Error: {status} upstream said no")
    assert _classify(text_only, adapter="claude-gpt")[1] == "hook_lifecycle"


def test_generic_api_error_without_hook_failure_is_never_proxy_translation():
    # started session, no chain-break reason mapped to another layer: never a proxy blame
    stream = Stream()
    stream.agent_call("toolu_agent_1", NAME).post_agent("toolu_agent_1", NAME, "agent-A")
    stream.start("agent-A").child_text(FIRST).stop("agent-A")
    for status in (401, 429, 529):
        layer = _classify(stream, adapter="claude-gpt", stderr=f"API Error: {status} nope\n")[1]
        assert layer != "proxy_translation", (status, layer)
        assert layer in set(MODULE.NAMED_RESUME_FAILURE_LAYERS)


@pytest.mark.parametrize("text", [
    "API Error: 400 bad request",
    "API Error: 422 unprocessable",
    "API Error: 400 invalid_request_error",
    "invalid_request_error: messages.1.content",
    "tool input schema rejected",
    "strict mode not supported for function",
])
def test_translation_error_evidence_is_still_attributed_to_proxy_translation(text):
    stderr_stream = _hook_failure_stream()
    assert _classify(stderr_stream, adapter="claude-gpt", stderr=text + "\n")[1] == "proxy_translation", text
    result_stream = _hook_failure_stream()
    result_stream.result(is_error=True, result=text)
    assert _classify(result_stream, adapter="claude-gpt")[1] == "proxy_translation", text
    # the native adapter is never blamed on the proxy
    assert _classify(_hook_failure_stream(), adapter="native", stderr=text + "\n")[1] != "proxy_translation"


@pytest.mark.parametrize("status", [400, 422])
def test_result_event_api_error_status_400_422_is_proxy_translation(status):
    stream = _hook_failure_stream()
    stream.result(is_error=True, result="request rejected", api_error_status=status)
    assert _classify(stream, adapter="claude-gpt")[1] == "proxy_translation"


# F5: run-level verdict is finalised once from the final exit code --------------------------


def test_late_assertion_failure_is_reflected_in_saved_verdict_and_freshness(tmp_path, hermetic_worktree):
    compat = tmp_path / "compat.md"
    compat.write_text("compat note\n", encoding="utf-8")
    with_compat = ["--append-system-prompt-file", str(compat)]
    code, evidence, _ = _run_main(
        tmp_path, hermetic_worktree, _causal_control(), name="late-fail",
        extra=[*with_compat, "--expect-marker", "MARKER_THAT_IS_NEVER_PRODUCED"])
    assert code != 0
    assert evidence["runner_exit_code"] == code
    assert evidence["verdict"] != "pass", "run-level verdict must reflect the final exit code"
    assert evidence["failure_layer"] in set(MODULE.NAMED_RESUME_FAILURE_LAYERS)
    # the causal chain itself was fine: kept in its own field, distinct from the run verdict
    assert evidence["causal_chain_verdict"] == "pass"
    record = MODULE.freshness_record_from_evidence(evidence)
    current = {
        "head": evidence["tested_head"], "changed_paths": [], "claude_code_version": record["claude_code_version"],
        "model_route": record["model_route"], "fixture_sha256": record["fixture_sha256"],
        "compat_note_sha256": record["compat_note_sha256"],
    }
    assert MODULE.evaluate_evidence_freshness(record, current)["reusable"] is False
    # the passing control stays reusable (no over-blocking)
    code_ok, ok_evidence, _ = _run_main(
        tmp_path, hermetic_worktree, _causal_control(), name="late-ok", extra=with_compat)
    assert code_ok == 0 and ok_evidence["verdict"] == "pass" and ok_evidence["causal_chain_verdict"] == "pass"
    ok_record = MODULE.freshness_record_from_evidence(ok_evidence)
    ok_current = {
        "head": ok_evidence["tested_head"], "changed_paths": [],
        "claude_code_version": ok_record["claude_code_version"], "model_route": ok_record["model_route"],
        "fixture_sha256": ok_record["fixture_sha256"], "compat_note_sha256": ok_record["compat_note_sha256"],
    }
    ok_reuse = MODULE.evaluate_evidence_freshness(ok_record, ok_current)
    assert ok_reuse["reusable"] is True, ok_reuse["reasons"]
    # ... and the failed run is rejected for the exit code / verdict, not for an unrelated gap
    bad_reuse = MODULE.evaluate_evidence_freshness(record, current)
    assert "recorded_runner_exit_code_not_zero" in bad_reuse["reasons"]
    assert "recorded_verdict_not_pass" in bad_reuse["reasons"]


def test_freshness_rejects_recorded_evidence_of_a_failed_run():
    base = {
        "adapter": "native", "verdict": "pass", "tested_head": "a" * 40, "claude_code_version": "2.1.287",
        "model_route": "claude-sonnet-5-5", "proxy_version": None, "launcher_sha256": None,
        "fixture_sha256": "f" * 64, "compat_note_sha256": "c" * 64, "runner_exit_code": 0,
    }
    current = {
        "head": "a" * 40, "changed_paths": [], "claude_code_version": "2.1.287",
        "model_route": "claude-sonnet-5-5", "fixture_sha256": "f" * 64, "compat_note_sha256": "c" * 64,
    }
    assert MODULE.evaluate_evidence_freshness(base, current)["reusable"] is True
    for label, recorded in (
        ("exit_code_1", {**base, "runner_exit_code": 1}),
        ("exit_code_missing", {k: v for k, v in base.items() if k != "runner_exit_code"}),
        ("verdict_fail_exit_0", {**base, "verdict": "fail"}),
    ):
        assert MODULE.evaluate_evidence_freshness(recorded, current)["reusable"] is False, label
    # producer/consumer contract: the flat record carries the run-level exit code
    evidence = {"adapter": "native", "verdict": "pass", "runner_exit_code": 1}
    assert MODULE.freshness_record_from_evidence(evidence)["runner_exit_code"] == 1


def test_haiku_prompt_requests_an_existence_check_of_a_python_function_in_a_regular_file():
    prompt = ROLE.haiku_prompt("/repo")
    assert f"target_path: /repo/{ROLE.HAIKU_TARGET_RELATIVE_PATH}" in prompt
    assert ROLE.HAIKU_TARGET_RELATIVE_PATH == "scripts/agent-ops/tests/_claude_gpt_role_subagent_smoke.py"
    assert f"target_symbol: {ROLE.HAIKU_TARGET_SYMBOL}" in prompt
    assert ROLE.HAIKU_TARGET_SYMBOL == "validate_haiku_handback"
    assert "defined or not defined" in prompt and "agy_advisory_native_fallback_allowed: true" in prompt
    # 値 / 行番号 / shell 変数 / directory は要求しない。
    assert "272000" not in prompt and "lib.sh" not in prompt and "COMPACT_WINDOW" not in prompt
    # 調査対象は regular Python file で、symbol が実際に def されている。
    target = REPO_ROOT / ROLE.HAIKU_TARGET_RELATIVE_PATH
    assert target.is_file() and target.suffix == ".py"
    assert f"def {ROLE.HAIKU_TARGET_SYMBOL}(" in target.read_text(encoding="utf-8")
