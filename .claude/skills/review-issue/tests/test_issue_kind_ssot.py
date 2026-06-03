"""
test_issue_kind_ssot.py

Tests for ISSUE_KIND_POLICY_V1 SSOT integration.

AC1: plan_refinement_loop.py has no local ISSUE_KIND_ALLOWLIST definition
AC2: check_issue_contract.py has no local issue_kind allowlist definition
AC3: detect_issue_kind("design") does not return "implementation"
AC4: detect_issue_kind("foobar") does not silently return "implementation"
AC5: docs/dev/github-ops.md contains ISSUE_KIND_POLICY_V1
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
REVIEW_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
REFINEMENT_SCRIPTS = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
GITHUB_OPS_MD = REPO_ROOT / "docs" / "dev" / "github-ops.md"

sys.path.insert(0, str(REVIEW_SCRIPTS))
sys.path.insert(0, str(REFINEMENT_SCRIPTS))


# ---------------------------------------------------------------------------
# AC1: plan_refinement_loop.py has no local ISSUE_KIND_ALLOWLIST
# ---------------------------------------------------------------------------

def test_plan_refinement_loop_no_local_allowlist():
    """AC1: plan_refinement_loop.py must not define ISSUE_KIND_ALLOWLIST at module level."""
    source = (REFINEMENT_SCRIPTS / "plan_refinement_loop.py").read_text(encoding="utf-8")
    # Must not have a top-level assignment like: ISSUE_KIND_ALLOWLIST = ...
    match = re.search(r"^ISSUE_KIND_ALLOWLIST\s*=", source, re.MULTILINE)
    assert match is None, (
        "plan_refinement_loop.py must not define a local ISSUE_KIND_ALLOWLIST. "
        "Use _get_issue_kind_allowlist() which reads from SSOT instead."
    )


# ---------------------------------------------------------------------------
# AC2: check_issue_contract.py has no local issue_kind allowlist
# ---------------------------------------------------------------------------

def test_check_issue_contract_no_local_allowlist():
    """AC2: check_issue_contract.py must not define ISSUE_KIND_ALLOWLIST at module level."""
    source = (REVIEW_SCRIPTS / "check_issue_contract.py").read_text(encoding="utf-8")
    match = re.search(r"^ISSUE_KIND_ALLOWLIST\s*=", source, re.MULTILINE)
    assert match is None, (
        "check_issue_contract.py must not define a local ISSUE_KIND_ALLOWLIST. "
        "Use _load_issue_kind_policy() which reads from SSOT instead."
    )


# ---------------------------------------------------------------------------
# AC3: detect_issue_kind does not return "implementation" for "design"
# ---------------------------------------------------------------------------

def test_design_not_implementation():
    """AC3: detect_issue_kind with issue_kind=design must NOT return 'implementation'.

    'design' is an alias for 'research' in ISSUE_KIND_POLICY_V1.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    # Build a minimal issue body with Machine-Readable Contract declaring design
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: design
```

## Outcome

Some outcome text.
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result != "implementation", (
        f"detect_issue_kind returned 'implementation' for issue_kind=design. "
        f"Expected 'research' (alias normalization) but got: {result!r}"
    )
    # Should be normalized to "research" via alias
    assert result == "research", (
        f"Expected detect_issue_kind to normalize 'design' to 'research', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# AC4: detect_issue_kind does not silently return "implementation" for unknown kind
# ---------------------------------------------------------------------------

def test_unknown_kind_no_silent_fallback():
    """AC4: detect_issue_kind with unknown kind must NOT return 'implementation'.

    Unknown kinds (not in canonical_kinds or aliases) must return the
    UNKNOWN_ISSUE_KIND_SENTINEL instead of silently falling back to 'implementation'.
    """
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: foobar
```

## Outcome

Some outcome text.
"""
    result = cic.detect_issue_kind(body, labels="", title="")
    assert result != "implementation", (
        f"detect_issue_kind silently returned 'implementation' for unknown kind 'foobar'. "
        f"Unknown kinds must be blocked (UNKNOWN_ISSUE_KIND_SENTINEL)."
    )
    assert result == cic.UNKNOWN_ISSUE_KIND_SENTINEL, (
        f"Expected UNKNOWN_ISSUE_KIND_SENTINEL for unknown kind 'foobar', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# AC5: docs/dev/github-ops.md contains ISSUE_KIND_POLICY_V1
# ---------------------------------------------------------------------------

def test_github_ops_md_has_issue_kind_policy_v1():
    """AC5: docs/dev/github-ops.md must contain ISSUE_KIND_POLICY_V1."""
    assert GITHUB_OPS_MD.exists(), f"docs/dev/github-ops.md not found at {GITHUB_OPS_MD}"
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "ISSUE_KIND_POLICY_V1" in content, (
        "docs/dev/github-ops.md must contain 'ISSUE_KIND_POLICY_V1'"
    )


def test_github_ops_md_policy_has_canonical_kinds():
    """AC5: ISSUE_KIND_POLICY_V1 must include canonical_kinds."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "canonical_kinds" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'canonical_kinds'"
    )


def test_github_ops_md_policy_has_aliases():
    """AC5: ISSUE_KIND_POLICY_V1 must include aliases."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "aliases" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'aliases'"
    )


def test_github_ops_md_policy_has_unknown_kind_policy():
    """AC5: ISSUE_KIND_POLICY_V1 must include unknown_kind_policy."""
    content = GITHUB_OPS_MD.read_text(encoding="utf-8")
    assert "unknown_kind_policy" in content, (
        "ISSUE_KIND_POLICY_V1 in docs/dev/github-ops.md must include 'unknown_kind_policy'"
    )


# ---------------------------------------------------------------------------
# Additional: SSOT consistency — plan_refinement_loop loads from SSOT
# ---------------------------------------------------------------------------

def test_plan_refinement_loop_ssot_loader_exists():
    """plan_refinement_loop.py must expose _load_issue_kind_policy and _get_issue_kind_allowlist."""
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    assert hasattr(prl, "_load_issue_kind_policy"), (
        "plan_refinement_loop.py must define _load_issue_kind_policy()"
    )
    assert hasattr(prl, "_get_issue_kind_allowlist"), (
        "plan_refinement_loop.py must define _get_issue_kind_allowlist()"
    )


def test_plan_refinement_loop_allowlist_matches_ssot_canonical_kinds():
    """plan_refinement_loop._get_issue_kind_allowlist() must match SSOT canonical_kinds."""
    import importlib
    import plan_refinement_loop as prl
    importlib.reload(prl)
    prl._clear_issue_kind_policy_cache()

    allowlist = prl._get_issue_kind_allowlist()
    assert "implementation" in allowlist
    assert "research" in allowlist
    assert "parent" in allowlist
    # design/tracking are aliases, not canonical — must NOT be in canonical_kinds
    assert "design" not in allowlist, (
        "'design' must not be in canonical_kinds (it is an alias for 'research')"
    )
    assert "foobar" not in allowlist


def test_check_issue_contract_ssot_policy_has_design_alias():
    """SSOT policy loaded by check_issue_contract must have design→research alias."""
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    policy = cic._load_issue_kind_policy()
    aliases = policy["aliases"]
    assert "design" in aliases, "SSOT aliases must include 'design'"
    assert aliases["design"] == "research", (
        f"'design' alias must map to 'research', got: {aliases['design']!r}"
    )


def test_check_issue_contract_ssot_policy_unknown_kind_policy_is_block():
    """SSOT policy loaded by check_issue_contract must have unknown_kind_policy: block."""
    import importlib
    import check_issue_contract as cic
    importlib.reload(cic)
    cic._clear_issue_kind_policy_cache()

    policy = cic._load_issue_kind_policy()
    assert policy["unknown_kind_policy"] == "block", (
        f"unknown_kind_policy must be 'block', got: {policy['unknown_kind_policy']!r}"
    )
