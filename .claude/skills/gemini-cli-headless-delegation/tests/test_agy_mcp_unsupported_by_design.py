"""Issue #1979 AC5: MCP is unsupported_by_design and never a completion blocker."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).parents[1]
PREFLIGHT_PATH = SKILL_DIR / "scripts" / "preflight_agy.py"
HOOK_PATH = SKILL_DIR / "scripts" / "agy_permission_enforcement_hook.py"
RUNNER_PATH = SKILL_DIR / "scripts" / "run_agy_permission_boundary_e2e.py"

_SPEC = importlib.util.spec_from_file_location("preflight_agy_for_mcp_test", PREFLIGHT_PATH)
assert _SPEC and _SPEC.loader
PREFLIGHT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = PREFLIGHT
_SPEC.loader.exec_module(PREFLIGHT)


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
