"""Regression coverage for trusted-directive normalization (Issue #1951)."""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_module("run_refinement_preflight_1952_regression", "run_refinement_preflight.py")
planner = _load_module("plan_refinement_loop_1952_regression", "plan_refinement_loop.py")
router = importlib.import_module("decide_next_loop_action")

REPO = "squne121/loop-protocol"
ISSUE = 1952
URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5224799872"
ISSUE_URL = f"https://github.com/{REPO}/issues/{ISSUE}"


def _payload(*, comment_id: int = 5224799872, association: str = "OWNER") -> dict:
    return {
        "id": comment_id,
        "author_association": association,
        "user": {"login": "squne121", "type": "User"},
    }


def _directive_body(*, materialized: bool = False, permission: bool = False) -> str:
    lines = [
        "# #1952 の scope delta",
        "- #1952 は分割しない。issue-author を issue-creator / issue-editor へ split するのは "
        "role split であり 1 Issue = 1 PR のままにする。",
        "- contract update で current main から導出した exact Allowed Paths を採用し、required rerun を実行する。",
    ]
    if permission:
        lines.append(
            "- exact permission delta を least privilege で採用する。non-destructive、no secrets、"
            "no paid external service、no unrelated privilege widening を満たす。"
        )
    if materialized:
        lines.extend(
            [
                "<!-- OWNER_DIRECTIVE_MATERIALIZED_FROM_INTERACTIVE_PROMPT_V1",
                "source_kind: direct_interactive_human_instruction",
                "-->",
                "<!-- CONTROLLED_EXEC_MARKER:fixture -->",
            ]
        )
    return "\n".join(lines)


def _evidence(body: str, *, url: str = URL, payload: dict | None = None) -> dict:
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=payload or _payload(),
        comment_body=body,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=url,
        captured_at="2026-08-08T00:00:00Z",
    )
    assert evidence is not None
    return evidence


def _classify(evidence: dict) -> dict:
    return sda.classify_scope_delta_authority(
        evidence,
        triggered=True,
        target_issue_number=ISSUE,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:issue-body",
    )


def test_owner_prose_directive_without_anchor_payload_routes_contract_update():
    result = _classify(_evidence(_directive_body()))

    assert result["authority_category"] == "human_review_directive"
    assert result["directive"]["confidence"] == "explicit"
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["implementation_allowed"] is False
    assert result["contract_patch_plan"]["source_evidence"][0]["source_comment_id"] == 5224799872


def test_role_split_same_issue_is_not_issue_partition():
    flags = sda.detect_boundary_flags(_directive_body())
    result = _classify(_evidence(_directive_body()))

    assert flags["requires_issue_split"] is False
    assert result["boundary_flags"]["requires_issue_split"] is False
    assert result["route"]["action"] == "contract_update_required"


def test_explicit_permission_delta_requires_contract_update_not_implementation():
    body = _directive_body(permission=True)
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["implementation_allowed"] is False
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"


def test_generated_owner_handoff_is_not_human_directive():
    body = "\n".join(
        [
            "LOOP_HANDOFF_RESULT_V1",
            "- contract update: please revise allowed paths",
            "<!-- CONTROLLED_EXEC_MARKER:fixture -->",
        ]
    )
    handoff_url = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5224719577"
    result = _classify(
        _evidence(body, url=handoff_url, payload=_payload(comment_id=5224719577))
    )

    assert result["provenance"]["source_kind"] == "generated_by_agent"
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


def test_direct_interactive_human_materialization_remains_distinct_from_generated_handoff():
    evidence = _evidence(_directive_body(materialized=True))

    assert evidence["source_kind"] == "issue_comment"
    assert _classify(evidence)["route"]["action"] == "contract_update_required"


def test_derived_patch_detects_source_and_body_staleness():
    result = _classify(_evidence(_directive_body()))
    plan = result["contract_patch_plan"]
    source_entry = plan["source_evidence"][0]

    assert plan["base_issue_body_sha256"] == "sha256:issue-body"
    assert source_entry["source_body_sha256"]
    stale = sda.normalize_trusted_anchor_iteration_zero(
        repo=REPO,
        issue_number=ISSUE,
        anchor={
            "html_url": URL,
            "author_association": "OWNER",
            "id": 5224799872,
            "source_body_sha256": "sha256:stale",
        },
        source_body=_directive_body(),
    )
    assert stale["accepted"] is False
    assert stale["failure"] == "anchor_identity_or_trust_changed"


def test_contract_update_rerun_is_required_before_implementation_route():
    evidence = _evidence(_directive_body())
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: test
change_kind: workflow
```

## Parent Issue

none

## Parent Goal Ref

test

## Current Validated Scope

- test

## Remaining Parent Gaps

none

## Outcome

test

## In Scope

- test

## Out of Scope

- none

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
$ true
```

## Allowed Paths

- `docs/test.md`

## Stop Conditions

- none

## Required Skills

- none
"""
    plan, exit_code = planner.plan_refinement_loop(
        {
            "schema_version": "refinement_loop_planner_input/v1",
            "issue": {
                "number": ISSUE,
                "title": "test",
                "body": body,
                "labels": [],
                "html_url": ISSUE_URL,
            },
            "comments": [],
            "known_context": {
                "scope_delta_authority_evidence": [evidence],
                "repo": REPO,
                "scope_signal_delta_input": {
                    "before_body": body.replace("- `docs/test.md`", ""),
                    "current_body": body,
                    "after_body": body,
                    "source_refs": {"before": "fixture:before", "current": "fixture:current", "after": "fixture:after"},
                },
            },
        }
    )
    assert exit_code == 0
    authority = plan["scope_signal_guard_decision_v2"]["scope_delta_authority"]
    assert authority["route"]["action"] == "contract_update_required"
    status, next_action, _commands, blockers, _cause = router.decide_next_action(
        loop_state={
            "iteration": 0,
            "max_iterations": 3,
            "termination_reason": None,
            "scope_signal_guard": {"triggered": True, "excluded_by_anchor_reframe": False},
        },
        review_verdict=None,
        scope_signal_guard_decision_v2=plan["scope_signal_guard_decision_v2"],
    )
    assert status == "pass"
    assert blockers == []
    assert next_action == "proceed_with_contract_update"
    assert authority["route"]["implementation_allowed"] is False


def test_preflight_consumer_chain_normalizes_materialized_directive(tmp_path):
    body = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: none
goal_ref: test
change_kind: workflow
```

## Parent Issue

none

## Parent Goal Ref

test

## Current Validated Scope

- test

## Remaining Parent Gaps

none

## Outcome

test

## In Scope

- test

## Out of Scope

- none

## Acceptance Criteria

- [ ] AC1: test

## Verification Commands

```bash
$ true
```

## Allowed Paths

- `docs/test.md`

## Stop Conditions

- none

## Required Skills

- none
"""
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": ISSUE,
        "repo": REPO,
        "now": "2026-08-08T00:00:00Z",
        "issue": {"number": ISSUE, "title": "test", "body": body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [URL],
        "anchor_comments": [
            {
                **_payload(),
                "body": _directive_body(materialized=True),
                "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
                "created_at": "2026-08-08T00:00:00Z",
                "updated_at": "2026-08-08T00:00:00Z",
                "html_url": URL,
                "url": f"https://api.github.com/repos/{REPO}/issues/comments/5224799872",
            }
        ],
    }
    fixture_path = tmp_path / "preflight.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    artifact_dir = (
        SKILL_ROOT.parent.parent / "artifacts" / "issue-refinement-loop" / str(ISSUE)
    )
    try:
        result, _exit_code = preflight.run_preflight(
            issue_number=ISSUE,
            repo=REPO,
            anchor_comment_urls=[URL],
            fixture_path=fixture_path,
        )
        planner_input = json.loads(
            Path(result["artifacts"]["planner_input"]).read_text(encoding="utf-8")
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

    authority = planner_input["known_context"]["scope_delta_authority_evidence"][0]
    assert authority["source_kind"] == "issue_comment"
    assert result["next_action"] != "implementation"
