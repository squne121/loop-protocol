"""Hermetic tests for agy_tool_provenance.py (Issue #1708).

All tests here use fixture-built `agy_tool_provenance_v1` events and a mocked/no-op
AGY execution path (via the module's pure functions and `unittest.mock.patch` for
subprocess). None of these tests spawn a real `agy` binary -- see Issue #1708 AC10
(this test file's only `subprocess` usage is via the standard library's
`subprocess.run` invoked against `python3` on a locally generated wrapper script, for
exercising the wrapper script's own I/O contract in isolation; no live AGY CLI, no
network, no live WebSearch tool).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agy_tool_provenance as prov  # noqa: E402


RUN_CONTEXT = {
    "conversation_id": "conv-123",
    "parent_run_id": "run-abc",
    "attempt_id": "attempt-1",
    "transcript_sha256": "a" * 64,
}


def _valid_event(**overrides):
    event = prov.build_provenance_event(
        event="PreToolUse",
        tool_name="search_web",
        tool_args={"query": "loop protocol"},
        step_idx=3,
        conversation_id=RUN_CONTEXT["conversation_id"],
        transcript_path="/home/user/repo/.gemini/antigravity-cli/transcript.jsonl",
        transcript_sha256=RUN_CONTEXT["transcript_sha256"],
        parent_run_id=RUN_CONTEXT["parent_run_id"],
        subtask_id="subtask-1",
        attempt_id=RUN_CONTEXT["attempt_id"],
        tool_profile="grounded_research",
    )
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# AC2: unknown tool name / legacy alias fail-closed
# ---------------------------------------------------------------------------


def test_unknown_tool_name_fails_closed():
    event = _valid_event()
    event["toolCall"] = dict(event["toolCall"])
    event["toolCall"]["name"] = "totally_unknown_tool"
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert "unknown_tool_provenance" in violations


@pytest.mark.parametrize(
    "legacy_name",
    [
        "web_search",
        "websearch",
        "browser_navigate",
        "browser",
        "url_read",
        "read_url",
        "fetch_url",
        "fetch",
    ],
)
def test_legacy_alias_tool_name_fails_closed(legacy_name):
    event = _valid_event()
    event["toolCall"] = dict(event["toolCall"])
    event["toolCall"]["name"] = legacy_name
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert "unknown_tool_provenance:legacy_alias" in violations


def test_canonical_tool_names_are_accepted():
    for name in ("search_web", "read_url_content"):
        event = _valid_event()
        event["toolCall"] = dict(event["toolCall"])
        event["toolCall"]["name"] = name
        ok, violations = prov.validate_provenance_event(event)
        assert ok is True, violations


def test_evaluate_websearch_provenance_fails_for_unknown_tool_event():
    event = _valid_event()
    event["toolCall"] = dict(event["toolCall"])
    event["toolCall"]["name"] = "web_search"
    result = prov.evaluate_websearch_provenance(
        hook_events=[event],
        expected_run_context=RUN_CONTEXT,
    )
    assert result["grounding_backend"] == "none"
    assert result["web_tool_call_count"] == 0
    assert result["failure_class"] == "unknown_tool_provenance"


# ---------------------------------------------------------------------------
# AC3 / AC8: stdout self-report alone is never authoritative
# ---------------------------------------------------------------------------


def test_stdout_self_report_alone_is_not_success():
    """stdout tool_calls/marker JSON with NO hook event => not grounded."""
    stdout_self_report = {
        "source": "AGY_WEBSEARCH:",
        "data": {
            "tool_calls": [{"name": "search_web"}],
            "citations": [{"url": "https://example.com/a"}],
        },
    }
    result = prov.evaluate_websearch_provenance(
        hook_events=[],
        expected_run_context=RUN_CONTEXT,
        stdout_self_report=stdout_self_report,
    )
    assert result["grounding_backend"] == "none"
    assert result["web_tool_call_count"] == 0
    assert result["provenance_status"] == "no_hook_event"
    assert result["failure_class"] == "agy_provenance_hook_event_missing"
    # stdout content is preserved for audit but is explicitly non-authoritative.
    assert result["stdout_self_report"] == stdout_self_report


def test_fabricated_tool_calls_json_without_hook_event_fails():
    """Adversarial: model prints a fully-formed, citation-bearing fake tool_calls JSON
    with a plausible-looking (but non-existent) hook event id -- still fails without a
    real, validated, run-matched agy_tool_provenance_v1 event.
    """
    fabricated_stdout_json = {
        "source": "json_line",
        "data": {
            "tool_calls": [{"name": "search_web", "hookEventId": "totally-fabricated"}],
            "sources": [{"url": "https://example.com/fabricated", "title": "Fabricated"}],
        },
    }
    result = prov.evaluate_websearch_provenance(
        hook_events=None,
        expected_run_context=RUN_CONTEXT,
        stdout_self_report=fabricated_stdout_json,
    )
    assert result["grounding_backend"] != "agy_native_websearch"
    assert result["web_tool_call_count"] == 0


def test_real_hook_event_grounds_regardless_of_stdout_absence():
    """A validated, run-matched hook event is sufficient on its own -- stdout content
    (even if empty/None) is irrelevant to the authoritative decision."""
    event = _valid_event()
    result = prov.evaluate_websearch_provenance(
        hook_events=[event],
        expected_run_context=RUN_CONTEXT,
        stdout_self_report=None,
    )
    assert result["grounding_backend"] == "agy_native_websearch"
    assert result["web_tool_call_count"] == 1
    assert result["provenance_status"] == "grounded_by_hook_provenance"


# ---------------------------------------------------------------------------
# AC5: workspace-scoped hook generation does not mutate global settings
# ---------------------------------------------------------------------------


def test_workspace_scoped_hook_does_not_mutate_global_settings(tmp_path):
    fake_global_home = tmp_path / "fake_home"
    fake_global_agents_dir = fake_global_home / ".agents"
    fake_global_agents_dir.mkdir(parents=True)
    fake_global_hooks = fake_global_agents_dir / "hooks.json"
    fake_global_hooks.write_text(json.dumps({"pre-existing": "global-config"}))
    original_global_hooks_content = fake_global_hooks.read_text()

    workspace = tmp_path / "agy-headless-run-1"
    workspace.mkdir()
    hook_log = tmp_path / "logs" / "hook_events.jsonl"
    hook_context = tmp_path / "logs" / "hook_context.json"

    hooks_json_path = prov.generate_workspace_hook_config(
        workspace,
        hook_log_path=hook_log,
        hook_context_path=hook_context,
    )

    assert hooks_json_path == workspace / ".agents" / "hooks.json"
    assert hooks_json_path.exists()
    written = json.loads(hooks_json_path.read_text())
    assert "agy-tool-provenance" in written
    assert written["agy-tool-provenance"]["PreToolUse"][0]["matcher"] == "search_web|read_url_content"

    # The fake global config, elsewhere on disk, must be byte-for-byte unchanged.
    assert fake_global_hooks.read_text() == original_global_hooks_content
    # The wrapper script is workspace-local too.
    wrapper = workspace / ".agents" / "agy_provenance_hook.py"
    assert wrapper.exists()
    assert str(fake_global_home) not in wrapper.read_text()


def test_hook_wrapper_script_appends_event_and_allows(tmp_path):
    """Exercise the generated wrapper script's own stdin/stdout contract in isolation
    (no live AGY binary -- this is a direct subprocess invocation of the generated
    python3 wrapper script only, simulating what AGY's hook runner would send it)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    hook_log = tmp_path / "hook_events.jsonl"
    hook_context = tmp_path / "hook_context.json"
    prov.generate_workspace_hook_config(
        workspace, hook_log_path=hook_log, hook_context_path=hook_context
    )
    prov.write_hook_context(
        hook_context,
        parent_run_id="run-abc",
        subtask_id="subtask-1",
        attempt_id="attempt-1",
        tool_profile="grounded_research",
        transcript_sha256="a" * 64,
    )
    wrapper = workspace / ".agents" / "agy_provenance_hook.py"
    stdin_payload = {
        "toolCall": {"name": "search_web", "args": {"query": "x"}},
        "stepIdx": 1,
        "conversationId": "conv-123",
        "transcriptPath": "/home/someone/repo/.gemini/antigravity-cli/transcript.jsonl",
    }
    env = dict(**prov.hook_env(hook_log, hook_context))
    import os

    completed = subprocess.run(
        [sys.executable, str(wrapper)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    stdout_obj = json.loads(completed.stdout.strip())
    assert stdout_obj == {"decision": "allow"}
    events = prov.load_hook_events(hook_log)
    assert len(events) == 1
    assert events[0]["toolCall"]["name"] == "search_web"
    assert events[0]["conversationId"] == "conv-123"
    assert events[0]["parent_run_id"] == "run-abc"


# ---------------------------------------------------------------------------
# AC6: redaction
# ---------------------------------------------------------------------------


def test_redaction_removes_credential_transcript_home_repo_path():
    home = "/home/testuser"
    repo_root = "/home/testuser/projects/LOOP_PROTOCOL"
    leaking_event = {
        "schema": "agy_tool_provenance_v1",
        "note": f"leaked key AIza{'x' * 35} and home {home}/secret and repo {repo_root}/file.py",
        "transcript_path": f"{home}/.gemini/antigravity-cli/transcript.jsonl",
    }
    violations = prov.scan_event_for_leaks(leaking_event, home=home, repo_root=repo_root)
    assert "raw_credential_detected" in violations
    assert "home_absolute_path_detected" in violations
    assert "repo_absolute_path_detected" in violations
    assert "raw_transcript_path_field_present" in violations

    clean_event = _valid_event()
    clean_violations = prov.scan_event_for_leaks(clean_event, home=home, repo_root=repo_root)
    assert clean_violations == []


def test_transcript_path_ref_never_contains_raw_path():
    home = "/home/testuser"
    ref = prov.transcript_path_ref(
        f"{home}/.gemini/antigravity-cli/transcript.jsonl", home=home, repo_root=None
    )
    assert home not in ref
    assert ref.startswith("sha256:")


# ---------------------------------------------------------------------------
# AC7: conversation/run mismatch fails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value,expected_violation",
    [
        ("conversationId", "wrong-conversation", "conversation_id_mismatch"),
        ("parent_run_id", "wrong-run", "parent_run_id_mismatch"),
        ("attempt_id", "wrong-attempt", "attempt_id_mismatch"),
        ("transcript_sha256", "b" * 64, "transcript_sha256_mismatch"),
    ],
)
def test_conversation_run_mismatch_fails(field, bad_value, expected_violation):
    event = _valid_event()
    event[field] = bad_value
    ok, mismatches = prov.match_run_context(
        event,
        conversation_id=RUN_CONTEXT["conversation_id"],
        parent_run_id=RUN_CONTEXT["parent_run_id"],
        attempt_id=RUN_CONTEXT["attempt_id"],
        transcript_sha256=RUN_CONTEXT["transcript_sha256"],
    )
    assert ok is False
    assert expected_violation in mismatches

    result = prov.evaluate_websearch_provenance(
        hook_events=[event], expected_run_context=RUN_CONTEXT
    )
    assert result["grounding_backend"] == "none"
    assert result["failure_class"] == "run_context_mismatch"


# ---------------------------------------------------------------------------
# AC9: hook generation / parse failure fail-closed
# ---------------------------------------------------------------------------


def test_hook_generation_or_parse_failure_fails_closed(tmp_path):
    # Generation failure: workspace_dir path collides with an existing *file* (not a
    # directory) at the .agents location, so mkdir(parents=True) must raise.
    workspace = tmp_path / "collide-ws"
    workspace.mkdir()
    (workspace / ".agents").write_text("not a directory")
    with pytest.raises(prov.ProvenanceWorkspaceHookError):
        prov.generate_workspace_hook_config(
            workspace,
            hook_log_path=tmp_path / "log.jsonl",
            hook_context_path=tmp_path / "ctx.json",
        )

    # Parse failure: malformed JSON line in the event log must raise, not silently
    # fall back to an empty/success result.
    bad_log = tmp_path / "bad_events.jsonl"
    bad_log.write_text('{"schema": "agy_tool_provenance_v1"\nnot even json\n')
    with pytest.raises(prov.ProvenanceParseError):
        prov.load_hook_events(bad_log)

    # evaluate_websearch_provenance must also fail closed (never silently succeed)
    # when told about a hook load error, even if hook_events happens to be populated.
    result = prov.evaluate_websearch_provenance(
        hook_events=[_valid_event()],
        expected_run_context=RUN_CONTEXT,
        hook_load_error="malformed agy_tool_provenance_v1 event at line 1",
    )
    assert result["grounding_backend"] == "none"
    assert result["provenance_status"] == "hook_load_failed"
    assert result["failure_class"] == "agy_provenance_hook_load_failed"


def test_missing_hook_log_file_returns_empty_list_not_error(tmp_path):
    """A hook log that was never written (hook never fired) is NOT a parse error --
    it correctly surfaces as `no_hook_event` via evaluate_websearch_provenance."""
    missing = tmp_path / "never_written.jsonl"
    assert prov.load_hook_events(missing) == []


# ---------------------------------------------------------------------------
# AC4: required field validation (also covered by schema governance test file)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", list(prov.REQUIRED_TOP_FIELDS))
def test_missing_required_top_field_fails(field):
    event = _valid_event()
    del event[field]
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert f"missing_required_field:{field}" in violations


@pytest.mark.parametrize("field", list(prov.REQUIRED_TOOL_CALL_FIELDS))
def test_missing_required_tool_call_field_fails(field):
    event = _valid_event()
    event["toolCall"] = dict(event["toolCall"])
    del event["toolCall"][field]
    ok, violations = prov.validate_provenance_event(event)
    assert ok is False
    assert f"missing_required_field:toolCall.{field}" in violations
