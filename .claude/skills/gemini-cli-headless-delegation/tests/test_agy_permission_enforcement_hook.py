"""Hermetic negative tests for the independent AGY enforcement hook."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "scripts" / "agy_permission_enforcement_hook.py"
SPEC = importlib.util.spec_from_file_location("agy_permission_enforcement_hook", PATH)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HOOK
SPEC.loader.exec_module(HOOK)


def _write_private_json(path: Path, payload: dict[str, object], *, mode: int = stat.S_IRUSR | stat.S_IWUSR) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(mode)


def _context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, profile: str = "no_tools") -> Path:
    policy_path = tmp_path / "policy.json"
    policy = {
        "schema": HOOK.POLICY_SCHEMA,
        "profile": profile,
        "allowed_resources": ["read_url"] if profile == "grounded_research" else [],
        "denied_resources": [
            resource
            for resource in sorted(HOOK.CANONICAL_PERMISSION_RESOURCES)
            if resource != "read_url" or profile != "grounded_research"
        ],
    }
    _write_private_json(policy_path, policy, mode=stat.S_IRUSR)
    digest = "sha256:" + hashlib.sha256(policy_path.read_bytes()).hexdigest()
    context_path = tmp_path / "run-context.json"
    _write_private_json(
        context_path,
        {
            "schema": HOOK.RUN_CONTEXT_SCHEMA,
            "run_id": "run-1",
            "workspace": "/isolated/workspace",
            "tool_profile": profile,
            "policy_path": str(policy_path),
            "policy_sha256": digest,
            "enforcement_log_path": str(tmp_path / "enforcement.jsonl"),
            "canary_id": "canary-1",
            "native_capabilities": {
                "run_command": "command",
                "view_file": "read_file",
                "write_to_file": "write_file",
                "search_web": "read_url",
                "mcp_call": "mcp",
            },
        },
        mode=stat.S_IRUSR,
    )
    monkeypatch.setenv(HOOK.CONTEXT_ENV, str(context_path))
    return context_path


def _payload(name: str = "run_command", *, args: object | None = None) -> dict[str, object]:
    return {
        "toolCall": {"name": name, "args": {"secret": "do-not-persist"} if args is None else args},
        "stepIdx": 2,
        "conversationId": "conversation-1",
        "workspacePaths": ["/isolated/workspace"],
    }


def test_restrictive_profile_denies_malformed_or_unknown_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _context(tmp_path, monkeypatch)
    assert HOOK.decide({})["decision"] == "deny"
    assert HOOK.decide({"toolCall": {"name": "run_command", "args": {}}, "stepIdx": 0})["reason"] == "malformed_payload"
    assert HOOK.decide(_payload("made_up_native_tool"))["reason"] == "unknown_native_tool"


def test_profile_is_runner_context_not_stdin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _context(tmp_path, monkeypatch, profile="no_tools")
    payload = _payload("search_web")
    payload["tool_profile"] = "grounded_research"
    assert HOOK.decide(payload)["reason"] == "policy_deny"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("workspace", "workspace_binding_mismatch"),
        ("policy_digest", "policy_digest_mismatch"),
        ("missing_policy", "policy_load_failure"),
        ("policy_profile", "context_policy_mismatch"),
    ],
)
def test_context_or_policy_failures_are_explicit_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, reason: str
) -> None:
    context_path = _context(tmp_path, monkeypatch)
    payload = _payload()
    if mutation == "workspace":
        payload["workspacePaths"] = ["/other/workspace"]
    else:
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        if mutation == "policy_digest":
            context["policy_sha256"] = "sha256:" + "0" * 64
        elif mutation == "missing_policy":
            context["policy_path"] = str(tmp_path / "missing-policy.json")
        else:
            policy_path = Path(context["policy_path"])
            policy_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["profile"] = "proposal_only"
            _write_private_json(policy_path, policy, mode=stat.S_IRUSR)
            context["policy_sha256"] = "sha256:" + hashlib.sha256(policy_path.read_bytes()).hexdigest()
        _write_private_json(context_path, context, mode=stat.S_IRUSR)
    assert HOOK.decide(payload)["reason"] == reason


def test_allowed_event_logs_only_canonical_args_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _context(tmp_path, monkeypatch, profile="grounded_research")
    secret = "never-write-this-raw-value"
    assert HOOK.decide(_payload("search_web", args={"q": secret}))["decision"] == "allow"
    record = json.loads((tmp_path / "enforcement.jsonl").read_text(encoding="utf-8"))
    assert record["args_digest"].startswith("sha256:")
    assert secret not in json.dumps(record)
    assert "/isolated/workspace" not in json.dumps(record)
    assert record["run_id"] == "run-1"
    assert record["conversation_id"] == "conversation-1"
    assert record["step_index"] == 2
    assert record["tool_profile"] == "grounded_research"
    assert record["canary_id"] == "canary-1"


def test_untrusted_context_permissions_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context_path = _context(tmp_path, monkeypatch)
    context_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IWGRP)
    assert HOOK.decide(_payload())["reason"] == "context_load_failure"


def test_owner_writable_runner_context_is_rejected_by_local_guardrail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context_path = _context(tmp_path, monkeypatch)
    context_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert HOOK.decide(_payload())["reason"] == "context_load_failure"


def test_allowed_attempt_fails_closed_when_safe_event_logging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _context(tmp_path, monkeypatch, profile="grounded_research")
    monkeypatch.setattr(HOOK, "_write_event", lambda context, event: False)
    assert HOOK.decide(_payload("search_web"))["reason"] == "log_write_failed"


# ---------------------------------------------------------------------------
# Issue #1920: github_research ALLOWED_PROFILES connection
# ---------------------------------------------------------------------------


def test_github_research_is_in_allowed_profiles() -> None:
    assert "github_research" in HOOK.ALLOWED_PROFILES


def test_github_research_context_loads_successfully_instead_of_context_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before Issue #1920, any run-context stamped with tool_profile="github_research"
    was rejected at context-load time (profile not in ALLOWED_PROFILES) --
    i.e. every attempt failed with "context_load_failure" regardless of the
    payload. This must no longer happen: a well-formed github_research
    context/policy pair must be evaluated on its own merits (denied here
    because "run_command" is not in this profile's empty allowed_resources
    set, not because the profile itself could not be loaded).
    """
    _context(tmp_path, monkeypatch, profile="github_research")
    decision = HOOK.decide(_payload("run_command"))
    assert decision["reason"] != "context_load_failure"


def test_github_research_denies_every_native_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """github_research has zero native tool-call surface (Issue #1920): the
    single `gh` invocation per turn is executed by the external broker
    (run_agy_github_research_broker.py), never by an AGY-native tool call.
    """
    _context(tmp_path, monkeypatch, profile="github_research")
    for tool_name in ("run_command", "view_file", "write_to_file", "search_web"):
        decision = HOOK.decide(_payload(tool_name))
        assert decision["decision"] == "deny"
        assert decision["reason"] == "policy_deny"
