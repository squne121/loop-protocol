"""P1-1 fix delta parity test (Issue #2290, PR #2335 OWNER review).

`.claude/skills/review-issue/scripts/check_issue_contract.py`'s
`check_c14_extension_surface_risk_trigger()` guards on
``issue_kind != "implementation"`` (returns NA). Before this fix delta,
`.claude/skills/issue-contract-review/scripts/contract_readiness_check.py`'s
`check_extension_surface_risk_trigger()` had no equivalent guard and would
flag a research-kind Issue whose declared Allowed Paths happen to overlap an
extension-surface selector, breaking cross-consumer parity (Issue #2290 AC7
requirement). This test fixes a research-kind Issue with a risky
(``.claude/agents/**``) Allowed Path entry and asserts both consumers agree
on not-applicable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_REVIEW_ISSUE_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "review-issue" / "scripts"
_CONTRACT_READINESS_SCRIPTS = _HERE.parent / "scripts"


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: "none"
goal_ref: "extension surface parity fixture"
change_kind: workflow
```

## Outcome

Research-kind fixture for extension-surface risk-trigger parity testing only.

## Acceptance Criteria

- [ ] AC1: research fixture AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/agents/foo.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: research fixture, no runtime execution needed

## Required Skills

none
"""


def test_research_kind_issue_is_not_applicable_on_both_consumers():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_p11_parity_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_p11_parity_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_status, review_issue_reasons = check_issue_contract.check_c14_extension_surface_risk_trigger(
        _RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY, "research"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_risk_trigger(
        _RESEARCH_KIND_RISKY_ALLOWED_PATH_BODY
    )

    # review-issue: NA, no reasons.
    assert review_issue_status == check_issue_contract.CheckResult.NA
    assert review_issue_reasons == []

    # issue-contract-review: empty error list (not-applicable equivalent).
    assert readiness_errors == []
