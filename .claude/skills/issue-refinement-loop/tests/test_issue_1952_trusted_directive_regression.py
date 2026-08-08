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
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。"
            "non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。"
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


def _evidence(
    body: str,
    *,
    url: str = URL,
    payload: dict | None = None,
    human_context_comment_urls: list[str] | None = None,
    agent_report_comment_urls: list[str] | None = None,
) -> dict:
    if human_context_comment_urls is None and agent_report_comment_urls is None:
        human_context_comment_urls = [url]
    comment_payload = dict(payload or _payload())
    if human_context_comment_urls is not None:
        comment_payload["human_context_comment_urls"] = human_context_comment_urls
    if agent_report_comment_urls is not None:
        comment_payload["agent_report_comment_urls"] = agent_report_comment_urls
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=comment_payload,
        comment_body=body,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=url,
        captured_at="2026-08-08T00:00:00Z",
        human_context_comment_urls=human_context_comment_urls,
        agent_report_comment_urls=agent_report_comment_urls,
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
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"
    assert result["route"]["implementation_allowed"] is False
    assert result["provenance"]["source_ref"] == URL


def test_role_split_same_issue_is_not_issue_partition():
    flags = sda.detect_boundary_flags(_directive_body())
    result = _classify(_evidence(_directive_body()))

    assert flags["requires_issue_split"] is False
    assert result["boundary_flags"]["requires_issue_split"] is False
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


def test_explicit_permission_delta_requires_contract_update_not_implementation():
    body = "\n".join(
        [
            "# #1952 の scope delta",
            "- #1952 は分割しない。issue-author を issue-creator / issue-editor へ split するのは "
            "role split であり 1 Issue = 1 PR のままにする。",
            "- contract update で current main から導出した exact Allowed Paths `docs/test.md` を採用し、"
            "required rerun を実行する。",
            "- exact permission delta: read-only -> workspace-write for `.codex/agents/issue-creator.toml` は "
            "human directive の目的に必要であり、least privilege で採用する。"
            "non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["reason_code"] == "explicit_human_contract_directive"
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["implementation_allowed"] is False
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"


def test_permission_delta_stock_phrase_without_concrete_before_after_path_escalates():
    body = "\n".join(
        [
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。"
            "non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。",
            "- allowed paths `docs/test.md` を追加する。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "changes_permission_boundary"


def test_permission_delta_without_human_directive_necessity_escalates():
    body = _directive_body(permission=True).replace("human directive の目的に必要であり、", "")
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "changes_permission_boundary"


def test_cross_bullet_permission_constraint_conjunction_is_not_sufficient():
    body = "\n".join(
        [
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。",
            "- non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "changes_permission_boundary"


def test_quoted_or_negated_permission_templates_do_not_satisfy_constraints():
    body = "\n".join(
        [
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。",
            "- \"least privilege ではない\" を明記する。",
            "- non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "changes_permission_boundary"


def test_permission_boundary_preserves_external_or_destructive_route_priority():
    body = "\n".join(
        [
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。"
            "non-destructive、no secrets、no paid external service、no unrelated privilege widening、"
            "call external service、force-push。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["boundary_flags"]["changes_external_service_boundary"] is True
    assert result["boundary_flags"]["destructive_or_non_idempotent_operation"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "destructive_or_non_idempotent_operation"


def test_vague_expands_allowed_paths_requires_human_escalation():
    body = "- allowed paths を expand する"
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["expands_allowed_paths"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


def test_exact_allowed_path_literals_stay_contract_update_only():
    body = "- allowed paths を必要に応じて追加する: `docs/test.md`"
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["expands_allowed_paths"] is True
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["implementation_allowed"] is False


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
        _evidence(
            body,
            url=handoff_url,
            payload=_payload(comment_id=5224719577),
            agent_report_comment_urls=[handoff_url],
        )
    )

    assert result["provenance"]["source_kind"] == "generated_by_agent"
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


def test_generated_owner_handoff_with_explicit_agent_lane_becomes_generated_by_agent():
    body = "\n".join(
        [
            "LOOP_HANDOFF_RESULT_V1",
            "- contract update: please revise allowed paths",
            "<!-- CONTROLLED_EXEC_MARKER:fixture -->",
        ]
    )
    handoff_url = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5224719577"
    result = _classify(
        _evidence(
            body,
            url=handoff_url,
            payload=_payload(comment_id=5224719577),
            agent_report_comment_urls=[handoff_url],
        )
    )

    assert result["provenance"]["source_kind"] == "generated_by_agent"
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"


def test_quoted_and_fenced_markers_do_not_invoke_body_based_source_derivation():
    body = "\n".join(
        [
            "```",
            "LOOP_HANDOFF_RESULT_V1",
            "<!-- CONTROLLED_EXEC_MARKER:fixture -->",
            "```",
            "> - least privilege ではない",
            "- contract update: please revise allowed paths",
        ]
    )
    result = _classify(_evidence(body, url=URL))

    assert result["provenance"]["source_kind"] == "issue_comment"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


def test_lanes_must_not_overlap_between_human_and_agent_contexts():
    body = _directive_body(permission=True)
    handoff_url = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-1111111111"
    result = _classify(
        _evidence(
            body,
            url=handoff_url,
            payload=_payload(comment_id=1111111111),
            human_context_comment_urls=[handoff_url],
            agent_report_comment_urls=[handoff_url],
        )
    )

    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "ai_inferred_scope_delta"


def test_unlabeled_anchor_is_fail_closed_even_when_its_body_looks_human():
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=_payload(),
        comment_body=_directive_body(permission=True),
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
        captured_at="2026-08-08T00:00:00Z",
    )
    assert evidence is not None
    result = _classify(evidence)

    assert result["provenance"]["source_kind"] == "generated_by_agent"
    assert result["route"]["action"] == "human_escalation"


def test_permission_exception_rechecks_vague_allowed_paths_boundary():
    body = "\n".join(
        [
            "- exact permission delta は human directive の目的に必要であり、least privilege で採用する。"
            "non-destructive、no secrets、no paid external service、no unrelated privilege widening を満たす。",
            "- allowed paths を必要に応じて追加する。",
        ]
    )
    result = _classify(_evidence(body))

    assert result["boundary_flags"]["changes_permission_boundary"] is True
    assert result["boundary_flags"]["expands_allowed_paths"] is True
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "changes_permission_boundary"


def test_direct_interactive_human_materialization_remains_distinct_from_generated_handoff():
    evidence = _evidence(_directive_body(materialized=True))

    assert evidence["source_kind"] == "issue_comment"
    assert _classify(evidence)["route"]["action"] == "human_escalation"
    assert _classify(evidence)["route"]["reason_code"] == "expands_allowed_paths"


def test_derived_patch_detects_source_and_body_staleness():
    body = "- allowed paths を必要に応じて追加する: `docs/test.md`"
    evidence = _evidence(body)
    result = _classify(evidence)
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
    evidence = _evidence("- allowed paths を必要に応じて追加する: `docs/test.md`")
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
        "known_context": {
            "human_context_comment_urls": [URL],
            "agent_report_comment_urls": [],
        },
        "anchor_comments": [
            {
                **_payload(),
                "body": _directive_body(materialized=True)
                + "\n- allowed paths `docs/test.md` を追加する。",
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
        provenance = json.loads(
            (artifact_dir / "refinement_preflight_provenance_v1.json").read_text(encoding="utf-8")
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

    authority = planner_input["known_context"]["scope_delta_authority_evidence"][0]
    assert authority["source_kind"] == "issue_comment"
    assert result["next_action"] != "implementation"
    runtime_evidence = provenance["runtime_evidence"]
    assert runtime_evidence["tested_head_sha"] == preflight._git_head_sha(SKILL_ROOT.parent.parent)
    assert runtime_evidence["source"] == {
        "comment_url": URL,
        "comment_id": 5224799872,
        "body_sha256": authority["body_sha256"],
        "source_kind": "issue_comment",
    }
    assert runtime_evidence["route"] == {
        "action": "contract_update_required",
        "implementation_allowed": False,
        "required_rerun": "rerun_refinement_after_contract_update",
    }
    assert runtime_evidence["terminal_event"]["implementation_allowed"] is False
    assert runtime_evidence["permission_profile_validators"] == {
        "status": "required_before_implementation",
        "passed": False,
    }
