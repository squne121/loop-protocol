"""Issue #1979 AC5: MCP is unsupported_by_design and never a completion blocker."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).parents[1]
PREFLIGHT_PATH = SKILL_DIR / "scripts" / "preflight_agy.py"
HOOK_PATH = SKILL_DIR / "scripts" / "agy_permission_enforcement_hook.py"
RUNNER_PATH = SKILL_DIR / "scripts" / "run_agy_permission_boundary_e2e.py"

_SPEC = importlib.util.spec_from_file_location("preflight_agy_for_mcp_test", PREFLIGHT_PATH)
assert _SPEC and _SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = PREFLIGHT
_SPEC.loader.exec_module(PREFLIGHT)

_HOOK_SPEC = importlib.util.spec_from_file_location("agy_permission_enforcement_hook_for_mcp_test", HOOK_PATH)
assert _HOOK_SPEC and _HOOK_SPEC.loader
HOOK = importlib.util.module_from_spec(_HOOK_SPEC)
sys.modules[_HOOK_SPEC.name] = HOOK
_HOOK_SPEC.loader.exec_module(HOOK)


def test_mcp_capability_status_is_unsupported_by_design_not_completion_blocker() -> None:
    record = PREFLIGHT.mcp_capability_status()
    assert record["status"] == "unsupported_by_design"
    assert record["completion_blocker"] is False
    assert record["reason"]


def test_enforcement_hook_never_imports_permission_policy() -> None:
    """Structural basis of `unsupported_by_design`: the enforcement hook that
    decides allow/deny for every profile never imports the policy module that
    would be required to grant direct MCP access.
    """
    source = HOOK_PATH.read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:import|from)\s+agy_permission_policy\b", source, re.MULTILINE)


def test_mcp_is_not_among_the_runner_capabilities() -> None:
    source = RUNNER_PATH.read_text(encoding="utf-8")
    match = re.search(r'CAPABILITIES\s*=\s*\(([^)]*)\)', source)
    assert match is not None
    capability_names = {token.strip().strip('"').strip("'") for token in match.group(1).split(",") if token.strip()}
    assert "mcp" not in capability_names
    assert capability_names == {"command", "write", "read", "network"}


def test_mcp_capability_status_reason_cites_the_import_boundary() -> None:
    record = PREFLIGHT.mcp_capability_status()
    assert "agy_permission_policy" in record["reason"]
    assert "agy_permission_enforcement_hook" in record["reason"]


def _write_private_json(path: Path, value: dict, *, mode: int) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    path.chmod(mode)


@pytest.mark.parametrize("profile", sorted(HOOK.ALLOWED_PROFILES))
@pytest.mark.parametrize("mcp_tool_name", ["mcp_call_tool", "mcp_list_tools", "mcp_read_resource"])
def test_unknown_mcp_tool_call_is_denied_under_every_profile_by_real_hook_logic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, mcp_tool_name: str
) -> None:
    """Issue #1979 fix_delta major_7: replace the import-absence-only proxy
    with a genuine BEHAVIOR test.  This drives `agy_permission_enforcement_hook.decide()`
    with a synthetic MCP tool-call payload under every profile and asserts an
    explicit deny, produced by the hook's real `NATIVE_TO_RESOURCE` dispatch
    (no entry maps any `mcp_*` name to a resource) -- not by a syntactic
    "does the source import X" check, which would never catch a future MCP
    dispatcher entry being added to the hook.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    control = tmp_path / "control"
    control.mkdir()

    # `allowed_resources` deliberately includes every canonical resource
    # (including "mcp") so this test proves denial comes from the
    # NATIVE_TO_RESOURCE dispatch gap, not merely from a restrictive policy.
    policy = {
        "schema": "agy_permission_boundary_policy/v1",
        "profile": profile,
        "allowed_resources": sorted(HOOK.CANONICAL_PERMISSION_RESOURCES),
        "denied_resources": [],
    }
    policy_path = control / "policy.json"
    _write_private_json(policy_path, policy, mode=0o400)
    policy_sha256 = "sha256:" + hashlib.sha256(policy_path.read_bytes()).hexdigest()

    enforcement_log = control / "enforcement.jsonl"
    context = {
        "schema": HOOK.RUN_CONTEXT_SCHEMA,
        "run_id": "mcp-behavior-test-run",
        "workspace": str(workspace),
        "tool_profile": profile,
        "policy_path": str(policy_path),
        "policy_sha256": policy_sha256,
        "enforcement_log_path": str(enforcement_log),
        "events_path": str(control / "events.jsonl"),
        "canary_id": "mcp-behavior-test-canary",
        "native_capabilities": {},
    }
    context_path = control / "run-context.json"
    _write_private_json(context_path, context, mode=0o400)
    monkeypatch.setenv(HOOK.CONTEXT_ENV, str(context_path))

    payload = {
        "toolCall": {"name": mcp_tool_name, "args": {"server": "synthetic", "tool": "synthetic"}},
        "conversationId": "conv-mcp-behavior-test",
        "stepIdx": 0,
        "workspacePaths": [str(workspace)],
    }

    decision = HOOK.decide(payload)

    assert decision["decision"] == "deny"
    assert decision["reason"] == "unknown_native_tool"
    events = [json.loads(line) for line in enforcement_log.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["decision"] == "deny"
    assert events[0]["reason"] == "unknown_native_tool"
    assert events[0]["resource"] == ""


def test_no_native_to_resource_entry_maps_any_tool_to_the_mcp_resource() -> None:
    """Structural corroboration of the behavior test above: assert directly on
    the dispatch table the hook actually consults, rather than only on the
    absence of an import statement.
    """
    assert "mcp" not in HOOK.NATIVE_TO_RESOURCE.values()
