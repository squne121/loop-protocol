"""Regression tests for Issue #1327.

The legacy stateless ``_detect_scope_signals()`` fallback in
``plan_refinement_loop.py`` (used when ``known_context.scope_signal_delta_input``
is not provided) must not mistake a single In Scope path token that happens to
contain two known layer prefixes (e.g. ``.claude/`` and ``tests/`` both appear
inside ``.claude/skills/gemini-cli-headless-delegation/tests/test_quota_fallback.py``)
for two independent new-layer mentions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "plan_refinement_loop.py"

FALSE_POSITIVE_ISSUE_BODY = """# Test Issue: Scope Signal Guard False Positive Regression (#1327)

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "single path token with 2 embedded prefixes must not trigger new_in_scope_area"
change_kind: research-only
```

## Outcome

A single In Scope bullet references one test file whose path happens to
contain two of the known layer prefixes as an embedded substring.

## In Scope

- gemini-cli-headless-delegation スキルの
  `.claude/skills/gemini-cli-headless-delegation/tests/test_quota_fallback.py` を確認する

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Scope signal guard does not trigger on this single path token

## Verification Commands

```bash
echo "verify scope detection"
```

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合は即停止

## Allowed Paths

- なし

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
"""


def _run_planner(issue_body: str) -> dict:
    input_data = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 1270,
            "title": "Test Issue: Scope Signal Guard False Positive Regression",
            "body": issue_body,
            "labels": [],
        },
        "comments": None,
        "known_context": None,
        "now": "2025-05-25T12:00:00+00:00",
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=json.dumps(input_data, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"planner exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


class TestScopeSignalGuardFalsePositiveRegression:
    """AC1/AC3: single path token with 2 embedded prefixes must not trigger."""

    def test_single_path_token_two_embedded_prefixes_does_not_trigger(self):
        output = _run_planner(FALSE_POSITIVE_ISSUE_BODY)
        scope_signal_guard = output["decisions"]["scope_signal_guard"]
        assert scope_signal_guard["triggered"] is False
        assert scope_signal_guard["reason_code"] == "no_scope_signal"

    def test_two_separate_bullets_still_trigger_new_in_scope_area(self):
        """AC2 companion check: 2 distinct bullets referencing distinct layers
        must still trigger (true-positive regression must not be broken)."""
        true_positive_body = """# Test Issue: True Positive Control

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "scope detection identifies multiple in-scope layers"
change_kind: research-only
```

## Outcome

This issue involves updates across multiple framework layers.

## In Scope

- Changes to `.claude/skills` framework layer
- Updates to `docs/product` specification layer
- Both layers require modifications

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Scope detection identifies multiple layers
- AC2: Evidence is properly captured

## Verification Commands

```bash
echo "verify scope detection"
```

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合は即停止

## Allowed Paths

- なし

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
"""
        output = _run_planner(true_positive_body)
        scope_signal_guard = output["decisions"]["scope_signal_guard"]
        assert scope_signal_guard["triggered"] is True
        assert scope_signal_guard["reason_code"] == "new_in_scope_area"

    def test_single_bullet_two_independent_path_tokens_still_triggers(self):
        """B4 (legacy fallback side, Issue #1327 iteration-2): a *single*
        In Scope bullet that references two independent path tokens (not two
        embedded prefixes within one token) must still trigger
        new_in_scope_area. This is distinct from
        test_two_separate_bullets_still_trigger_new_in_scope_area (which uses
        two separate bullets); here both tokens are on the same line."""
        same_bullet_body = """# Test Issue: Same Bullet Two Independent Tokens

## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "single bullet with 2 independent path tokens must still trigger"
change_kind: research-only
```

## Outcome

A single In Scope bullet references two independent path tokens that each
belong to a different layer.

## In Scope

- `.claude/skills/foo` と `docs/foo.md` を更新する

## Parent Issue

none

## Out of Scope

- 実装コード変更

## Acceptance Criteria

- AC1: Scope detection identifies multiple layers in a single bullet

## Verification Commands

```bash
echo "verify scope detection"
```

## Stop Conditions

- Outcome / Scope / AC が検索可能な形で記載されていない場合は即停止
- Allowed Paths 外への書き込みを試みた場合は即停止
- 権限不足により操作が完了できない場合は即停止
- 成果物の書き込みに失敗した場合は即停止

## Allowed Paths

- なし

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
"""
        output = _run_planner(same_bullet_body)
        scope_signal_guard = output["decisions"]["scope_signal_guard"]
        assert scope_signal_guard["triggered"] is True
        assert scope_signal_guard["reason_code"] == "new_in_scope_area"
