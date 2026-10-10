"""Existing-issue readiness versus kind-aware write guard, and editor procedure (#3006)."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".claude/skills/edit-issue/SKILL.md"
REFINEMENT_SKILL = ROOT / ".claude/skills/issue-refinement-loop/SKILL.md"
TEMPLATE = ROOT / ".github/ISSUE_TEMPLATE/implementation.yml"
READINESS = ROOT / ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py"
GUARD = ROOT / ".claude/skills/edit-issue/scripts/guard-issue-body.py"
VALIDATOR = ROOT / ".claude/skills/create-issue/scripts/validate_issue_body.py"


def _template_stop_items() -> list[str]:
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    field = next(item for item in template["body"] if item.get("id") == "stop-conditions")
    return [line for line in field["attributes"]["value"].splitlines() if line.startswith("- ")]


def _body(stop_items: list[str]) -> str:
    return """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "none"
goal_ref: "verify candidate guard parity"
change_kind: workflow
```

## Parent Issue

none

## Parent Goal Ref

- Goal: Prevent false-ready edit dispatch.
- Desired Destination: Keep the local candidate in the editor.

## Current Validated Scope

- Validate changed candidate body before handing it to the transaction.

## Remaining Parent Gaps

none

## Outcome

A candidate body file passes the local full write guard and the transaction receives it.

## Runtime Verification Applicability

- decision: not_applicable
- reason: Pure validator fixture with no extension surface.

## In Scope

- This synthetic fixture.

## Out of Scope

- GitHub mutation.

## Acceptance Criteria

- [ ] AC1: The validation paths agree on a repaired candidate.

## Verification Commands

```bash
# AC1
$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/test_pre_edit_validation_parity.py -q
```

## Allowed Paths

- src/systems/MovementSystem.ts

## Stop Conditions

""" + "\n".join([*stop_items, "- Fixture-specific condition must remain intact."]) + (
        "\n\n## Required Skills\n\nnone"
        "\n\n## Required Design References\n\n- docs/dev/workflow.md\n"
    )


def _run(script: Path, *args: str) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.stdout, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_existing_validator_profiles_with_real_subprocess(tmp_path: Path) -> None:
    """GIVEN a missing standard condition WHEN validating THEN legacy GO != write LP017."""
    standard_items = _template_stop_items()
    assert len(standard_items) == 6
    candidate = tmp_path / "candidate.md"
    candidate.write_text(_body(standard_items[:-1]), encoding="utf-8")

    ready_rc, ready = _run(READINESS, "--mode", "preflight-static", "--body-file", str(candidate))
    guard_rc, guard = _run(GUARD, str(candidate), "--format", "json")
    assert ready_rc == 0 and ready["status"] == "go", ready
    assert guard_rc == 2 and guard["all_passed"] is False, guard
    body_check = next(item for item in guard["guards"] if item["name"] == "issue_body_validation")
    assert {error["rule_id"] for error in body_check["errors"]} == {"LP017"}, guard
    assert all(item["passed"] for item in guard["guards"] if item is not body_check), guard

    # The standalone validator's exit 1 / status / errors contract covers LP017 only,
    # not the template, quality, alignment, and shell guards above.
    validator_rc, validator = _run(VALIDATOR, "--kind", "implementation", "--body-file", str(candidate))
    assert validator_rc == 1 and validator["status"] == "fail"
    assert {error["rule_id"] for error in validator["errors"]} == {"LP017"}

    repaired = _body(standard_items)
    assert "- Fixture-specific condition must remain intact." in repaired
    candidate.write_text(repaired, encoding="utf-8")
    guard_rc, guard = _run(GUARD, str(candidate), "--format", "json")
    ready_rc, ready = _run(READINESS, "--mode", "preflight-static", "--body-file", str(candidate))
    assert guard_rc == 0 and guard["all_passed"] is True, guard
    assert ready_rc == 0 and ready["status"] == "go", ready


def test_implementation_transaction_rejects_lp017_then_reaches_normal_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GIVEN the same fixture WHEN txn runs THEN LP017 blocks, repair reaches normal path."""
    script = ROOT / ".claude/skills/edit-issue/scripts/edit_issue_txn.py"
    spec = importlib.util.spec_from_file_location("edit_issue_txn_3006_parity", script)
    assert spec is not None and spec.loader is not None
    txn = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = txn
    spec.loader.exec_module(txn)

    repo = tmp_path / "repo"
    (repo / "tmp").mkdir(parents=True)
    (repo / "artifacts").mkdir()
    monkeypatch.setattr(txn, "REPO_ROOT", repo)
    monkeypatch.setattr(txn, "GUARD_SCRIPT", GUARD)
    monkeypatch.setattr(
        txn, "HYGIENE_SCRIPT", ROOT / ".claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py"
    )
    monkeypatch.setattr(txn, "READINESS_SCRIPT", READINESS)
    candidate = repo / "tmp" / "candidate.md"
    standard = _template_stop_items()
    old_body = "old issue body"
    payload = {
        "schema": "ISSUE_EDIT_TXN_INPUT_V1",
        "issue_number": 3006,
        "repo": "squne121/loop-protocol",
        "new_body_file": "tmp/candidate.md",
        "readiness_forwarding_payload": {"readiness_result": {
            "status": "go", "body_sha256": txn._sha256_text(old_body),
            "source_checks": ["contract_readiness_check.py --mode preflight-static"],
            "errors": [], "readiness_result_ref": "artifacts/readiness.json",
        }},
        "comment_mode": {"mode": "skip"},
        "expected_previous_body_sha256": txn._sha256_text(old_body),
        "expected_previous_updated_at": "2026-07-03T10:40:51Z",
        "title_update": {"required": False, "proposed_title": None, "reason": None},
    }
    calls: list[str] = []

    def _fetch(*_args: object, **_kwargs: object) -> tuple[dict, str]:
        return {
            "title": "old", "body": candidate.read_text(encoding="utf-8") if calls else old_body,
            "updatedAt": "2026-07-03T10:41:51Z" if calls else "2026-07-03T10:40:51Z",
        }, ""

    def _invoke(command_id: str, *_args: object, **_kwargs: object) -> tuple[subprocess.CompletedProcess, dict]:
        calls.append(command_id)
        result = {"new_body_sha256": txn._sha256_text(candidate.read_text(encoding="utf-8"))}
        return subprocess.CompletedProcess([], 0, json.dumps(result), ""), result

    monkeypatch.setattr(txn, "_fetch_issue", _fetch)
    monkeypatch.setattr(txn, "_invoke_controlled_exec", _invoke)

    candidate.write_text(_body(standard[:-1]), encoding="utf-8")
    rejected = txn.run_transaction(payload)
    assert rejected["status"] == "failed_no_mutation"
    assert rejected["mutation_started"] is False and rejected["body_update"]["attempted"] is False
    assert calls == []

    candidate.write_text(_body(standard), encoding="utf-8")
    accepted = txn.run_transaction(payload)
    assert accepted["status"] == "ok", accepted["errors"]
    assert accepted["body_update"]["attempted"] is True
    assert calls == ["issue_content.update"]


def _section(text: str, start: str, end: str) -> str:
    assert start in text and end in text
    return text.split(start, 1)[1].split(end, 1)[0]


def test_editor_candidate_guard_repair_transaction_order_contract() -> None:
    """GIVEN an edited candidate WHEN invoking editor THEN its last checked bytes reach txn."""
    procedure = _section(SKILL.read_text(encoding="utf-8"), "## 手順", "## visual_impact")
    # Assert the actual procedural steps (not a generic mention elsewhere in the skill).
    candidate = procedure.index("### 1. candidate body")
    assert "new_body == current_body" in procedure[candidate:]
    change_only = procedure.index("new_body == current_body", candidate)
    command = (
        "uv run --locked python3 .claude/skills/edit-issue/scripts/guard-issue-body.py "
        "<candidate_body_file> --format json"
    )
    guard = procedure.index(command, change_only)
    repair = procedure.index("LP017", guard)
    recheck = procedure.index("full guard を再実行", repair)
    last_checked = procedure.index("最後に検証した候補", recheck)
    handoff = procedure.index("ISSUE_EDIT_TXN_INPUT_V1.new_body_file", last_checked)
    transaction = procedure.index("### 3. helper を起動する", handoff)
    assert candidate < change_only < guard < repair < recheck < last_checked < handoff < transaction
    assert "--orig-file" not in command and "--check-ready-tuple" not in command
    assert "`--orig-file` と `--check-ready-tuple` は渡さない" in procedure[change_only:guard]
    assert "_template" not in procedure[guard:repair]  # no new generator/helper
    assert re.search(r"exit 0.*exit 2", procedure[guard:repair], re.DOTALL)
    assert "readiness" in procedure[candidate:guard]

    step4 = _section(REFINEMENT_SKILL.read_text(encoding="utf-8"), "### Step 4: 書き換え", "### Step 4.5:")
    assert "--mode preflight-static" in step4
    assert "<current_body_file>" in step4
    assert "候補" in step4 and "issue-editor" in step4
    assert "edit-ready" in step4
