"""Issue #2857: producer-to-consumer classification regressions on real planner output."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import jsonschema

SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "plan_refinement_loop.py"
SCHEMA = SKILL / "schemas" / "refinement_loop_plan_v1.json"


def _body(*, outcome="Local outcome.", scope="", ac="- AC1: Observe output.",
          vc="Use hermetic tests.", background="", out_of_scope="- No unrelated changes."):
    return f"""## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "classification regression"
change_kind: research-only
```

## Background

{background}

## Outcome

{outcome}

## In Scope

{scope}

## Parent Issue

none

## Out of Scope

{out_of_scope}

## Acceptance Criteria

{ac}

## Verification Commands

{vc}

## Stop Conditions

- Stop when evidence is unavailable.

## Allowed Paths

- なし

## Handoff Contract

- `Current Objective`
- `Bounded Current Context`
- `Open Questions`
- `Next Action`
- `Artifact Refs`
"""


def _plan(body: str, comments=None):
    payload = {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {"number": 2857, "title": "Classification regression", "body": body, "labels": []},
        "comments": comments, "known_context": None,
        "now": "2026-10-08T00:00:00+00:00",
    }
    result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    plan = json.loads(result.stdout)
    assert not plan["fail_closed"]["required"], plan["fail_closed"]
    return plan


def _web(plan):
    return plan["decisions"]["web_research_policy"]


def _investigation(plan):
    return plan["decisions"]["investigation_policy"]


def _consumer():
    # Unique import identity avoids collisions with other test modules in a shared session.
    spec = importlib.util.spec_from_file_location(
        "issue_2857_web_route", SKILL / "scripts" / "route_web_research_result.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_local_execution_claims_do_not_require_external_evidence():
    """GIVEN concrete and pathless verification requests WHEN planning THEN repo lane owns them."""
    requests = [
        "current-main で `rg` により symbol_a を探し call chain を確認する。",
        "Run current-main function_a directly and verify its return value.",
        "Repository tests and fixtures must establish the behavior of symbol_b.",
        "`rg symbol_c` でリポジトリの実装を確認する。",
        "Check the repository call-chain of symbol_d with no path literal.",
    ]
    for request in requests:
        plan = _plan(_body(ac=f"- AC1: {request}"))
        assert _web(plan)["required"] is False, request
        assert _web(plan)["critical_external_claims"] == [], request
        repo = _investigation(plan)
        assert repo["required"] is True, request
        assert any(request in claim for claim in repo["repo_claims"]), (request, repo)
        assert repo["evidence_spans"], request
    incidental = _plan(_body(ac="- AC1: Show a client-side authority label."))
    assert not _web(incidental)["required"]
    assert not _investigation(incidental)["required"]


def test_external_dispositive_claims_remain_fail_closed():
    """GIVEN official API dependency WHEN web unavailable THEN unchanged consumer blocks."""
    plan = _plan(_body(ac=(
        "- AC1: Verify GitHub REST API rate-limit response against the official "
        "API specification before choosing retry behavior."
    )))
    policy = _web(plan)
    assert policy["required"] and policy["critical_external_claims"]
    assert all(c["role"] == "dispositive" for c in policy["critical_external_claims"])
    result = _consumer().route_web_research_result({
        "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
        "repository_decision": {"status": "inconclusive", "disposition": None},
        "critical_external_claims": policy["critical_external_claims"],
        "web_research": {"status": "inconclusive", "failure_class": None,
                         "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
    })
    assert result["next_action"] == "human_judgment_required"
    assert "dispositive_external_evidence_unresolved" in result["reason_codes"]


def test_distinct_comment_claims_with_shared_prefix_retain_source_hints_and_dedupe():
    """GIVEN two assertions after a long shared prefix WHEN extracted THEN both survive."""
    prefix = "Context for the review: " + "scope summary " * 12
    comments = [{"id": 71, "body": (
        f"{prefix} Verify GitHub GraphQL error semantics against official docs before approval. "
        "Verify the CLI authentication flow against official docs before approval. "
        "Verify GitHub GraphQL error semantics against official docs before approval."
    )}]
    plan = _plan(_body(), comments)
    claims = _web(plan)["critical_external_claims"]
    assert len(claims) == 2, claims
    assert any("GraphQL error semantics" in c["claim"] for c in claims)
    assert any("CLI authentication flow" in c["claim"] for c in claims)
    assert all(c["source_hint"] == "comment_71" for c in claims)
    assert all(set(c) == {"claim", "affects", "source_hint", "role"} for c in claims)
    assert all(c["affects"] == "VC" and c["role"] == "dispositive" for c in claims)
    jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(plan)


def test_out_of_scope_negative_rate_limit_is_not_external_specification():
    """GIVEN #1659's negative scope line WHEN planned THEN web remains optional."""
    body = _body(out_of_scope="- GitHub API rate-limit policy 全体の変更")
    policy = _web(_plan(body))
    assert policy["required"] is False and policy["critical_external_claims"] == []
    assert policy["reason_code"] == "no_critical_external_claim"


def test_operator_comment_and_pasted_html_are_not_keyword_only_external_claims():
    """GIVEN quoted/negated HTML and substring noise WHEN planned THEN no false web gate."""
    comments = [{"id": 88, "body": """Operator note: Webで確認不要。公式 docs は参照しない。
> Previously: verify externally against official docs.
<!--StartFragment--><html><body><p>"verify externally" は過去の引用。</p>
<p>api graphql web are example labels, not verification requests.</p>
<blockquote>Verify API policy against official docs.</blockquote></body></html><!--EndFragment-->"""}]
    plan = _plan(_body(ac="- AC1: repository authority and client symbols are local implementation details."), comments)
    assert _web(plan)["required"] is False
    assert _web(plan)["critical_external_claims"] == []
    affirmative = _plan(_body(), [{
        "id": 89,
        "body": ("<!--StartFragment--><p>Verify GitHub GraphQL error semantics "
                 "against official docs before approval.</p><!--EndFragment-->"),
    }])
    assert _web(affirmative)["required"]
    assert _web(affirmative)["critical_external_claims"][0]["source_hint"] == "comment_89"


def test_genuine_graphql_api_claim_survives_noise_and_fails_closed():
    """GIVEN mixed clauses, Japanese official sources and VC THEN retain dependencies."""
    plan = _plan(_body(
        background="GraphQL is mentioned here only as historical context.\n"
                   "設計判断は GitHub GraphQL の data/errors の公式仕様に依存する。",
        ac="- AC1: `rg` で symbol_a を検査し、GitHub GraphQL の error 仕様を公式ドキュメントで照合する。",
        vc="""```bash
# literal sample: official API text is a fixture, not a verification instruction
uv run pytest tests/
```
VC: GitHub API rate-limit response must be checked against the official docs before approval.""",
    ))
    claims = _web(plan)["critical_external_claims"]
    assert _web(plan)["required"] and len(claims) == 3, claims
    assert all(c["role"] == "dispositive" for c in claims)
    assert any("data/errors" in c["claim"] for c in claims)
    assert any(c["affects"] == "VC" for c in claims)
    assert not any("literal sample" in c["claim"] for c in claims)
    assert _investigation(plan)["required"]
    assert any("rg" in c for c in _investigation(plan)["repo_claims"])
    background_only = _plan(_body(background="GraphQL is mentioned here only as historical context."))
    assert not _web(background_only)["required"]
    affirmative = _plan(_body(ac=(
        "- AC1: GitHub API rate-limit response follows the official "
        "specification; verify before release."
    )))
    assert _web(affirmative)["required"]

    # A negative repository directive and an affirmative external dependency
    # can share one sentence. The former must not suppress the latter.
    mixed = _plan(_body(ac=(
        "- AC1: Do not check repo fixtures, but verify current GitHub GraphQL "
        "errors against official docs before approval."
    )))
    mixed_claims = _web(mixed)["critical_external_claims"]
    assert _web(mixed)["required"] and len(mixed_claims) == 1, mixed_claims
    assert "verify current GitHub GraphQL errors against official docs" in mixed_claims[0]["claim"]
    assert "Do not check repo fixtures" not in mixed_claims[0]["claim"]
    assert mixed_claims[0]["role"] == "dispositive"
    unresolved = _consumer().route_web_research_result({
        "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
        "repository_decision": {"status": "inconclusive", "disposition": None},
        "critical_external_claims": mixed_claims,
        "web_research": {"status": "inconclusive", "failure_class": None,
                         "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
    })
    assert unresolved["next_action"] == "human_judgment_required"


def test_planner_output_routes_without_manual_role_rewrite_or_web_skip():
    """GIVEN planner output WHEN invoking existing route THEN skip or evidence-gated result."""
    consumer = _consumer()
    no_web = _plan(_body(ac="- AC1: current-main `rg` で symbol_a の call-chain を確認する。"))
    policy = _web(no_web)
    assert policy["required"] is False and policy["critical_external_claims"] == []
    # The existing caller skips the consumer on required=false, recording the
    # planner's canonical reason code rather than passing an invalid [] payload.
    with patch.object(consumer, "route_web_research_result", side_effect=AssertionError("consumer called")):
        skip_reason = policy["reason_code"] if not policy["required"] else None
    assert skip_reason == "no_critical_external_claim"

    web_plan = _plan(_body(ac="- AC1: Check GitHub GraphQL errors against official docs before release."))
    policy = _web(web_plan)
    assert policy["required"] and policy["critical_external_claims"]
    request = {
        "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
        "repository_decision": {"status": "inconclusive", "disposition": None},
        "critical_external_claims": policy["critical_external_claims"],
        "web_research": {"status": "inconclusive", "failure_class": None,
                         "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
    }
    assert request["critical_external_claims"] is policy["critical_external_claims"]
    unresolved = consumer.route_web_research_result(request)
    assert unresolved["next_action"] == "human_judgment_required"
    assert "dispositive_external_evidence_unresolved" in unresolved["reason_codes"]
    # Evidence must cover the *exact* planner claim strings, not merely be nonempty.
    request["web_research"] = {
        "status": "ok", "failure_class": None, "verification_route": "grounded_research",
        "claims": [{"claim_id": str(i), "text": c["claim"], "type": "external_spec",
                    "critical": True, "verdict": "supported",
                    "evidence": [{"kind": "web", "ref": "https://docs.github.com/example",
                                  "summary": "Verified against an official source."}]}
                   for i, c in enumerate(policy["critical_external_claims"])],
        "unresolved_risks": [],
    }
    assert consumer.route_web_research_result(request)["next_action"] == "proceed"
    request["web_research"]["claims"][0]["text"] = "unrelated claim"
    assert consumer.route_web_research_result(request)["next_action"] == "human_judgment_required"
