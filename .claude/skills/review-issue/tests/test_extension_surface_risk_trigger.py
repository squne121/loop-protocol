"""Tests for C14 (`check_c14_extension_surface_risk_trigger`, Issue #2290).

Covers AC6: `decision: immediate` declared but the Runtime Verification
Applicability contract's required immediate fields are missing ->
needs-fix (CheckResult.FAIL).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "check_issue_contract.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
import check_issue_contract as checker  # noqa: E402


_BASE_BODY_TEMPLATE = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "test"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for C14 fixture testing purposes only.

## Acceptance Criteria

- [ ] AC1: concrete AC
<!-- runtime-verification: true -->

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

{allowed_paths}

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

{rva_section}

## Required Skills

none
"""


def test_risk_trigger_immediate_incomplete_returns_needs_fix():
    """AC6: decision: immediate but required immediate fields are missing -> FAIL."""
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- .claude/agents/implementation-worker.md",
        rva_section=(
            "- decision: immediate\n"
            "- reason: risky agent definition change requires runtime smoke test"
        ),
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    assert status == checker.CheckResult.FAIL
    assert issues
    assert any("missing" in issue for issue in issues)


def test_risk_trigger_immediate_complete_with_matching_scope_passes():
    """decision: immediate with all required fields present and Allowed Paths
    matching a risky selector -> PASS (declared decision already at/above
    the policy's most-restrictive requirement, and RVA immediate fields
    are complete)."""
    rva_section = (
        "- decision: immediate\n"
        "applicable_acs: [AC1]\n"
        "execution_environment: worktree-agent-runtime-smoke\n"
        "skip_conditions: none\n"
        "fallback_policy: escalate_to_human\n"
        "artifact_requirements: artifacts/smoke.json\n"
    )
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- .claude/agents/implementation-worker.md",
        rva_section=rva_section,
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "implementation")
    assert status == checker.CheckResult.PASS
    assert issues == []


def test_risk_trigger_docs_only_not_applicable_passes():
    """Non-implementation issue_kind is not applicable to C14."""
    body = _BASE_BODY_TEMPLATE.format(
        allowed_paths="- docs/dev/foo.md",
        rva_section="- decision: not_applicable\n- reason: docs only",
    )
    status, issues = checker.check_c14_extension_surface_risk_trigger(body, "research")
    assert status == checker.CheckResult.NA
    assert issues == []
