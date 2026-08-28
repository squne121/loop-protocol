"""Issue #2339 AC7 parity test: `review-issue`'s
`get_extension_surface_candidate_advisories()` and `issue-contract-review`'s
`check_extension_surface_advisory()` must return the same non-blocking
`unclassified_candidate` advisory classification for the same fixture body,
because both call the exact same shared evaluator
(`scripts/agent-guards/extension_surface_policy_matcher.py`'s
`evaluate_allowed_paths()`) -- structural parity, not independently
re-derived logic (same pattern as the pre-existing
`test_extension_surface_risk_trigger_parity.py` for the blocking C14 gate).
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


_UNCLASSIFIED_CANDIDATE_FIXTURE_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "extension surface advisory routing parity fixture"
change_kind: workflow
```

## Outcome

Concrete outcome sentence for extension-surface advisory routing parity fixture testing only.

## Acceptance Criteria

- [ ] AC1: concrete AC

## Verification Commands

```bash
# AC1
$ rg -n "concrete" file.py
```

## Allowed Paths

- .claude/rules/project-constitution.md

## Stop Conditions

- one
- two
- three
- four
- five
- six

## Runtime Verification Applicability

- decision: not_applicable
- reason: advisory routing parity fixture, no runtime execution needed

## Required Skills

none
"""

_ORDINARY_FIXTURE_BODY = _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY.replace(
    "- .claude/rules/project-constitution.md", "- docs/dev/foo.md"
)


def test_unclassified_candidate_advisory_parity_across_consumers():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_advisory_parity_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_advisory_parity_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_advisories = check_issue_contract.get_extension_surface_candidate_advisories(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_advisory(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )

    assert review_issue_advisories, "expected a non-empty unclassified_candidate advisory"
    assert len(readiness_errors) == 1
    assert readiness_errors[0]["category"] == "extension_surface_candidate_advisory"
    readiness_advisories = readiness_errors[0]["minimal_context"]

    # Structural parity: same shared-evaluator advisory messages on both sides.
    assert review_issue_advisories == readiness_advisories


def test_ordinary_path_no_advisory_parity_across_consumers():
    check_issue_contract = _load_module_from_path(
        "check_issue_contract_for_ext_surface_advisory_parity_ordinary_test",
        _REVIEW_ISSUE_SCRIPTS / "check_issue_contract.py",
    )
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_advisory_parity_ordinary_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    review_issue_advisories = check_issue_contract.get_extension_surface_candidate_advisories(
        _ORDINARY_FIXTURE_BODY, "implementation"
    )
    readiness_errors = contract_readiness_check.check_extension_surface_advisory(_ORDINARY_FIXTURE_BODY)

    assert review_issue_advisories == []
    assert readiness_errors == []


def test_advisory_never_raises_readiness_overall_status():
    """AC6 parity check: the advisory-only fixture must not raise
    `overall_status` above `go` via `run_contract_readiness_check()` --
    `ext_surface_advisory_errors` must stay excluded from the
    `if ext_surface_errors:` escalation."""
    contract_readiness_check = _load_module_from_path(
        "contract_readiness_check_for_ext_surface_advisory_parity_status_test",
        _CONTRACT_READINESS_SCRIPTS / "contract_readiness_check.py",
    )

    validate_result = contract_readiness_check.run_validate_issue_body(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY
    )
    result = contract_readiness_check.build_result(
        _UNCLASSIFIED_CANDIDATE_FIXTURE_BODY, "static", validate_result, None, None
    )
    assert result["status"] == "go"
    categories = {e.get("category") for e in result["errors"]}
    assert "extension_surface_candidate_advisory" in categories
    assert "extension_surface_risk_trigger" not in categories
