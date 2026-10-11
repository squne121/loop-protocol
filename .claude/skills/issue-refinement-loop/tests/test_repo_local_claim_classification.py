"""Issue #2857: producer-to-consumer classification regressions on real planner output."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

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
        "Check local example fixtures for symbol_b.",
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


def test_external_first_local_later_claims_retain_distinct_subjects_and_dedupe():
    graphql = ("GitHub GraphQL request errors must follow the official specification "
               "before maintainers verify repository tests.")
    rest = ("GitHub REST API rate-limit responses must follow the official specification "
            "before maintainers verify repository tests.")
    comments = [{"id": 71, "body": f"{graphql}\n{rest}\n{graphql}"}]
    claims = _web(_plan(_body(), comments))["critical_external_claims"]
    assert len(claims) == 2, claims
    assert {claim["claim"] for claim in claims} == {graphql, rest}
    assert all(claim["source_hint"] == "comment_71" and claim["role"] == "dispositive"
               and claim["affects"] == "VC" for claim in claims)
    assert not _web(_plan(_body(), [{"id": 71, "body": "Verify repository tests."}]))["required"]


@pytest.mark.parametrize("phrase,required", [
    ("Verify against the official GraphQL specification that request error "
     "responses must not contain data.", True),
    ("GraphQL の公式仕様で request error 時の data の返却は禁止されるため、この制約を検証する。", True),
    ("Do not verify GraphQL request error behavior against official documentation.", False),
    ("GraphQL の公式仕様は今回確認しない。", False),
])
def test_negation_scope_distinguishes_specification_from_verification(phrase, required):
    for body, comments, hint in [
        (_body(ac=f"- AC1: {phrase}"), None, None),
        (_body(), [{"id": 97, "body": phrase}], "comment_97"),
    ]:
        policy = _web(_plan(body, comments))
        assert policy["required"] is required, (phrase, policy)
        claims = policy["critical_external_claims"]
        assert len(claims) == int(required), (phrase, claims)
        if required:
            assert ("must not contain data" in claims[0]["claim"]
                    or "返却は禁止" in claims[0]["claim"])
            assert claims[0]["role"] == "dispositive"
            assert claims[0]["source_hint"] == hint
            result = _consumer().route_web_research_result({
                "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
                "repository_decision": {"status": "inconclusive", "disposition": None},
                "critical_external_claims": claims,
                "web_research": {"status": "inconclusive", "failure_class": None,
                                 "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
            })
            assert result["next_action"] == "human_judgment_required"
        else:
            assert policy["reason_code"] == "no_critical_external_claim"


@pytest.mark.parametrize("quoted", [
    "> Earlier reviewer wrote:\nVerify GitHub GraphQL errors against official docs.",
    "<pre><code>Verify GitHub GraphQL errors against official docs.</code></pre>",
    "<pre>\nVerify GitHub GraphQL errors against official docs.\n</pre>",
])
def test_comment_quote_and_code_literals_do_not_become_current_claims(quoted):
    bare = _web(_plan(_body(), [{"id": 91, "body": quoted}]))
    assert bare["critical_external_claims"] == []
    assert bare["reason_code"] == "no_critical_external_claim"
    current = "Verify current GitHub GraphQL errors against official docs."
    contrasted = _web(_plan(_body(), [{"id": 91, "body": f"{quoted}\n\n{current}"}]))
    assert contrasted["required"] is True
    assert len(contrasted["critical_external_claims"]) == 1
    assert contrasted["critical_external_claims"][0]["claim"] == current
    assert contrasted["critical_external_claims"][0]["source_hint"] == "comment_91"
    assert contrasted["critical_external_claims"][0]["role"] == "dispositive"


def test_quoted_fragment_followed_by_independent_current_external_requirement():
    comment = ("Previously quoted “GitHub GraphQL errors must match official docs”, "
               "but current GitHub GraphQL errors must match official docs before approval.")
    claims = _web(_plan(_body(), [{"id": 92, "body": comment}]))["critical_external_claims"]
    assert len(claims) == 1
    assert "current GitHub GraphQL errors" in claims[0]["claim"]
    assert claims[0]["source_hint"] == "comment_92"


def test_official_specification_assertion_without_known_topic_remains_dispositive():
    claims_text = [
        "Official OAuth 2.0 documentation requires an exact registered redirect URI match before approval.",
        "The published protocol specification requires an exact registered redirect URI match before approval.",
        "OAuth 2.0 specification requires an exact registered redirect URI match before approval.",
    ]
    for phrase in claims_text:
        for body, comments, hint in [
            (_body(ac=f"- AC1: {phrase}"), None, None),
            (_body(), [{"id": 98, "body": phrase}], "comment_98"),
        ]:
            plan = _plan(body, comments)
            claims = _web(plan)["critical_external_claims"]
            assert _web(plan)["required"] and len(claims) == 1, (phrase, claims)
            assert phrase in claims[0]["claim"]
            assert claims[0]["role"] == "dispositive" and claims[0]["source_hint"] == hint
            assert _consumer().route_web_research_result({
                "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
                "repository_decision": {"status": "inconclusive", "disposition": None},
                "critical_external_claims": claims,
                "web_research": {"status": "inconclusive", "failure_class": None,
                                 "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
            })["next_action"] == "human_judgment_required"
    assert not _web(_plan(_body(ac="- AC1: Official documentation is mentioned only as background.")))["required"]


def test_repository_specification_fixture_is_local_evidence_not_web():
    phrase = "Check the repository GraphQL specification fixture in tests and confirm its returned shape."
    plan = _plan(_body(ac=f"- AC1: {phrase}"))
    assert _web(plan)["required"] is False
    assert _web(plan)["critical_external_claims"] == []
    assert _investigation(plan)["required"] is True
    assert any(phrase in claim for claim in _investigation(plan)["repo_claims"])
    local = _web(_plan(_body(ac=(
        "- AC1: Check the official repository GraphQL specification fixture in tests."
    ))))
    assert local["required"] is False
    external = _web(_plan(_body(ac=(
        "- AC1: Verify the official GraphQL specification against repository fixture output."
    ))))
    assert external["required"] and len(external["critical_external_claims"]) == 1
    mixed = _web(_plan(_body(ac=(
        "- AC1: Check the repository GraphQL specification fixture against official docs."
    ))))
    assert mixed["required"] and len(mixed["critical_external_claims"]) == 1
    before = _web(_plan(_body(ac=(
        "- AC1: Verify GitHub GraphQL errors against official docs using the repository "
        "GraphQL specification fixture in tests before approval."
    ))))
    assert before["required"] and len(before["critical_external_claims"]) == 1
    assert "official docs using the repository" in before["critical_external_claims"][0]["claim"]
    oauth = _web(_plan(_body(ac=(
        "- AC1: Official OAuth documentation requires a registered redirect URI match "
        "using the repository GraphQL specification fixture in tests."
    ))))
    assert oauth["required"] and len(oauth["critical_external_claims"]) == 1
    local_before = _web(_plan(_body(ac=(
        "- AC1: Check the official GraphQL specification fixture in repository tests."
    ))))
    assert local_before["required"] is False


def test_reference_only_inline_quote_does_not_hide_separate_current_request():
    historical = "Reference only: `Verify GitHub GraphQL errors against official docs.` No action requested."
    policy = _web(_plan(_body(), [{"id": 71, "body": historical}]))
    assert policy["required"] is False and policy["critical_external_claims"] == []
    current = "Verify current GitHub GraphQL errors against official docs before approval."
    policy = _web(_plan(_body(), [{"id": 71, "body": f"{historical}\n\n{current}"}]))
    assert policy["required"] is True
    assert len(policy["critical_external_claims"]) == 1
    assert policy["critical_external_claims"][0]["claim"] == current
    assert policy["critical_external_claims"][0]["source_hint"] == "comment_71"
    inline = _web(_plan(_body(), [{"id": 72, "body": (
        "Reference only: `Verify GitHub GraphQL errors against official docs`, "
        "but current GitHub GraphQL errors must match official docs before approval."
    )}]))
    assert inline["required"] is True
    assert len(inline["critical_external_claims"]) == 1
    assert "current GitHub GraphQL errors" in inline["critical_external_claims"][0]["claim"]


def test_earlier_reviewer_inline_quote_is_not_a_current_request():
    historical = (
        "Earlier reviewer wrote: `Verify GitHub GraphQL errors against official docs.` "
        "This is a historical quote, not a current request."
    )
    policy = _web(_plan(_body(), [{"id": 99, "body": historical}]))
    assert policy["required"] is False and policy["critical_external_claims"] == []
    current = "Verify current GitHub GraphQL errors against official docs before approval."
    policy = _web(_plan(_body(), [{"id": 99, "body": f"{historical}\n\n{current}"}]))
    assert policy["required"] and len(policy["critical_external_claims"]) == 1
    assert policy["critical_external_claims"][0]["claim"] == current
    assert policy["critical_external_claims"][0]["source_hint"] == "comment_99"
    inline = _web(_plan(_body(), [{"id": 99, "body": (
        "Earlier reviewer wrote: `Verify GitHub GraphQL errors against official docs`, "
        "but current GitHub GraphQL errors must match official docs before approval."
    )}]))
    assert inline["required"] and len(inline["critical_external_claims"]) == 1
    assert "current GitHub GraphQL errors" in inline["critical_external_claims"][0]["claim"]
    assert "Earlier reviewer wrote" not in inline["critical_external_claims"][0]["claim"]


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

    mixed_requests = [
        (
            "Do not verify local fixtures and verify GitHub GraphQL errors "
            "against official docs before approval.",
            "verify GitHub GraphQL errors against official docs",
        ),
        (
            "Do not verify local fixtures and verify GitHub GraphQL errors "
            "against official docs before approval and do not change tests.",
            "verify GitHub GraphQL errors against official docs",
        ),
        (
            "リポジトリ fixtures の検証は不要、GitHub GraphQL の data/errors を"
            "公式仕様と照合する必要がある。",
            "GitHub GraphQL の data/errors を公式仕様と照合する必要がある",
        ),
        (
            "Verify GitHub GraphQL errors against official docs before approval "
            "and do not change repository tests.",
            "Verify GitHub GraphQL errors against official docs",
        ),
    ]
    for request, expected in mixed_requests:
        for body, comments, source_hint in [
            (_body(ac=f"- AC1: {request}"), None, None),
            (_body(ac="- AC1: Run current-main repository tests."),
             [{"id": 93, "body": request}], "comment_93"),
        ]:
            mixed_plan = _plan(body, comments)
            mixed_claims = _web(mixed_plan)["critical_external_claims"]
            assert _web(mixed_plan)["required"] and len(mixed_claims) == 1, mixed_claims
            assert expected in mixed_claims[0]["claim"]
            assert "Do not verify local fixtures" not in mixed_claims[0]["claim"]
            assert "do not change" not in mixed_claims[0]["claim"]
            assert "不要" not in mixed_claims[0]["claim"]
            assert mixed_claims[0]["source_hint"] == source_hint
            assert mixed_claims[0]["role"] == "dispositive"
            assert _consumer().route_web_research_result({
                "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
                "repository_decision": {"status": "inconclusive", "disposition": None},
                "critical_external_claims": mixed_claims,
                "web_research": {"status": "inconclusive", "failure_class": None,
                                 "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
            })["next_action"] == "human_judgment_required"
    quoted = _plan(_body(), [{"id": 94, "body": f"Previously quoted: {mixed_requests[0][0]}"}])
    assert not _web(quoted)["required"]
    jp_quote = _plan(_body(), [{"id": 95, "body": f"過去の引用: {mixed_requests[2][0]}"}])
    assert not _web(jp_quote)["required"]
    noise = _plan(_body(ac="- AC1: リポジトリ fixtures の検証は不要、GitHub GraphQL は例示のみ。"))
    assert not _web(noise)["required"]


@pytest.mark.parametrize(
    "phrase,expected",
    [
        (
            "Do not verify local fixtures and GitHub GraphQL errors must match "
            "official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify local example fixtures and GitHub GraphQL errors must match "
            "official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify local fixtures (example: repository API fixtures) and "
            "GitHub GraphQL errors must match official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify local fixtures (historical context: repository API fixtures) "
            "and GitHub GraphQL errors must match official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify local fixtures (過去の引用) and GitHub GraphQL errors "
            "must match official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify historical local fixtures and GitHub GraphQL errors must match "
            "official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify literal example fixtures and GitHub GraphQL errors must match "
            "official docs before approval.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors have to match "
            "official docs before approval.",
            "GitHub GraphQL errors have to match official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are described "
            "in official docs before approval.",
            "GitHub GraphQL errors are described in official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are covered "
            "in official docs before approval.",
            "GitHub GraphQL errors are covered in official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are to be checked "
            "against official docs before approval.",
            "GitHub GraphQL errors are to be checked against official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors will be checked "
            "against official docs before approval.",
            "GitHub GraphQL errors will be checked against official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors will be verified "
            "against official docs before approval.",
            "GitHub GraphQL errors will be verified against official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are checked "
            "against official docs before approval.",
            "GitHub GraphQL errors are checked against official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are verified "
            "against official docs before approval.",
            "GitHub GraphQL errors are verified against official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors must match "
            "official docs no later than release.",
            "GitHub GraphQL errors must match official docs no later than release",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors match "
            "official docs before approval.",
            "GitHub GraphQL errors match official docs before approval",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors have to match "
            "official docs before approval and do not change tests.",
            "GitHub GraphQL errors have to match official docs before approval",
        ),
        (
            "Do not verify local fixtures and API authentication behavior has to "
            "agree with published specification before approval.",
            "API authentication behavior has to agree with published specification",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors must match "
            "official docs before approval and do not change tests.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "Do not inspect repository call chain and GitHub GraphQL data/errors "
            "require validation against the official specification before approval.",
            "GitHub GraphQL data/errors require validation against the official specification",
        ),
        (
            "Do not verify local fixtures and API authentication behavior must "
            "agree with published specification before approval.",
            "API authentication behavior must agree with published specification",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors and API rate "
            "limits must match official docs before approval.",
            "GitHub GraphQL errors and API rate limits must match official docs",
        ),
        (
            "Do not verify local fixtures and GitHub GraphQL errors are documented "
            "in the official API specification before approval.",
            "GitHub GraphQL errors are documented in the official API specification",
        ),
        (
            "GitHub GraphQL errors must match official docs before approval "
            "and do not verify repository fixtures.",
            "GitHub GraphQL errors must match official docs before approval",
        ),
        (
            "リポジトリ fixtures の検証は不要、GitHub GraphQL のエラーは"
            "公式仕様と照合が必要である。",
            "GitHub GraphQL のエラーは公式仕様と照合が必要",
        ),
    ],
)
def test_independent_noun_start_external_claims_remain_dispositive(phrase, expected):
    for body, comments, source_hint in [
        (_body(ac=f"- AC1: {phrase}"), None, None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 93, "body": phrase}], "comment_93"),
    ]:
        plan = _plan(body, comments)
        claims = _web(plan)["critical_external_claims"]
        assert _web(plan)["required"] and len(claims) == 1, claims
        claim = claims[0]
        assert expected in claim["claim"]
        assert "Do not" not in claim["claim"]
        assert "不要" not in claim["claim"]
        assert claim["source_hint"] == source_hint
        assert claim["role"] == "dispositive"
        assert set(claim) == {"claim", "affects", "source_hint", "role"}
        routing_input = {
            "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
            "repository_decision": {"status": "inconclusive", "disposition": None},
            "critical_external_claims": claims,
            "web_research": {"status": "inconclusive", "failure_class": None,
                             "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
        }
        assert _consumer().route_web_research_result(routing_input)["next_action"] == "human_judgment_required"
        routing_input["web_research"] = {
            "status": "ok", "failure_class": None, "verification_route": "grounded_research",
            "claims": [{"claim_id": "0", "text": claim["claim"], "type": "external_spec",
                        "critical": True, "verdict": "supported",
                        "evidence": [{"kind": "web", "ref": "https://docs.github.com/example",
                                      "summary": "Verified against an official source."}]}],
            "unresolved_risks": [],
        }
        assert _consumer().route_web_research_result(routing_input)["next_action"] == "proceed"


@pytest.mark.parametrize("external", [
    "GitHub GraphQL errors will be checked against official docs before approval.",
    "GitHub GraphQL errors will be verified against official docs before approval.",
    "GitHub GraphQL errors are to be checked against official docs before approval.",
    "GitHub GraphQL errors must match official docs no later than release.",
])
def test_external_dependency_survives_independent_negative_local_prefix(external):
    for phrase in (external, f"Do not verify local fixtures and {external}"):
        for body, comments, source_hint in [
            (_body(ac=f"- AC1: {phrase}"), None, None),
            (_body(ac="- AC1: Run current-main repository tests."),
             [{"id": 93, "body": phrase}], "comment_93"),
        ]:
            policy = _web(_plan(body, comments))
            claims = policy["critical_external_claims"]
            assert policy["required"] and len(claims) == 1, claims
            assert external.rstrip(".") in claims[0]["claim"]
            assert "Do not verify local fixtures" not in claims[0]["claim"]
            assert claims[0]["source_hint"] == source_hint
            assert claims[0]["role"] == "dispositive"
            assert set(claims[0]) == {"claim", "affects", "source_hint", "role"}


@pytest.mark.parametrize("phrase,expected", [
    (
        "GitHub GraphQL errors will be checked against official docs "
        "without editing tests.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors will be checked "
        "against official docs without editing tests.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors will be checked "
        "against official docs without changing code before approval.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "GitHub GraphQL errors will be checked against official docs "
        "without modifying files before approval.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors will be checked "
        "against official docs without altering implementation before approval.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors will be checked "
        "against official docs without adding source files before approval.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors must match "
        "official docs without checking repository API fixtures.",
        "GitHub GraphQL errors must match official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors must match "
        "official docs without checking repository fixtures.",
        "GitHub GraphQL errors must match official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors must match "
        "official docs without editing repository API fixtures.",
        "GitHub GraphQL errors must match official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors will be checked "
        "against official docs without checking local GraphQL API tests.",
        "GitHub GraphQL errors will be checked against official docs",
    ),
    (
        "Do not verify local fixtures and GitHub GraphQL errors must match "
        "official docs without checking repository API fixtures or consulting official docs.",
        "GitHub GraphQL errors must match official docs",
    ),
])
def test_repo_local_without_modifier_does_not_erase_external_dependency(phrase, expected):
    for body, comments, source_hint in [
        (_body(ac=f"- AC1: {phrase}"), None, None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 93, "body": phrase}], "comment_93"),
    ]:
        policy = _web(_plan(body, comments))
        claims = policy["critical_external_claims"]
        assert policy["required"] and len(claims) == 1, claims
        assert expected in claims[0]["claim"]
        assert "Do not verify local fixtures" not in claims[0]["claim"]
        assert " without " not in claims[0]["claim"]
        assert claims[0]["source_hint"] == source_hint
        assert claims[0]["role"] == "dispositive"
        assert _consumer().route_web_research_result({
            "schema": "WEB_RESEARCH_ROUTING_INPUT_V1",
            "repository_decision": {"status": "inconclusive", "disposition": None},
            "critical_external_claims": claims,
            "web_research": {"status": "inconclusive", "failure_class": None,
                             "verification_route": "grounded_research", "claims": [], "unresolved_risks": []},
        })["next_action"] == "human_judgment_required"


def test_affirmative_deadline_does_not_negate_external_dependency():
    phrase = "GitHub GraphQL errors must match official docs no later than release."
    for body, comments in [
        (_body(ac=f"- AC1: {phrase}"), None),
        (_body(), [{"id": 96, "body": phrase}]),
    ]:
        policy = _web(_plan(body, comments))
        assert policy["required"] is True
        assert len(policy["critical_external_claims"]) == 1
        assert phrase in policy["critical_external_claims"][0]["claim"]


@pytest.mark.parametrize(
    "phrase",
    [
        "Do not verify local fixtures.",
        "Do not verify local fixtures and check repository tests instead.",
        "Do not verify local fixtures and GitHub GraphQL errors against official docs.",
        "Do not verify local fixtures and API authentication behavior from published specification.",
        "Do not verify local fixtures and do not check GitHub GraphQL errors against official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors have to not match official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors must not match official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors are not to be checked against official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors will not be checked against official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors won't be checked against official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors are not checked against official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors will be checked without official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors will be checked without consulting official docs.",
        "Do not verify local fixtures and GitHub GraphQL errors will be checked without using external docs.",
        "Do not verify local fixtures and GitHub GraphQL errors must match official docs "
        "without consulting official API documentation.",
        "Do not verify local fixtures and GitHub GraphQL errors must match official docs "
        "without checking GitHub GraphQL specification.",
        "Previously quoted: Do not verify local fixtures and GitHub GraphQL errors must match "
        "official docs without checking repository API fixtures.",
        "Previously quoted: Do not verify local example fixtures and GitHub GraphQL errors "
        "must match official docs before approval.",
        "Previously quoted “GitHub GraphQL errors must match official docs before approval.”",
        "(Example: GitHub GraphQL errors must match official docs before approval.)",
        "Previously quoted: Do not verify local fixtures, but verify GitHub GraphQL "
        "errors against official docs before approval.",
        "Previously quoted: Do not verify local fixtures; verify GitHub GraphQL "
        "errors against official docs before approval.",
        "No later than release, do not verify GitHub GraphQL errors against official docs.",
        "GitHub GraphQL errors must not match official docs no later than release.",
        "Previously quoted: Do not verify local fixtures and GitHub GraphQL errors "
        "must match official docs before approval.",
        "Previously quoted: Do not verify local fixtures and GitHub GraphQL errors "
        "have to match official docs before approval.",
        "Literal example: Do not verify local fixtures and GitHub GraphQL errors "
        "must match official docs before approval.",
        "Previously quoted: Do not verify local fixtures and GitHub GraphQL errors "
        "will be checked against official docs before approval.",
        "リポジトリ fixtures の検証は不要、GitHub GraphQL は例示のみ。",
    ],
)
def test_noun_start_mixed_clause_negative_controls_do_not_require_web(phrase):
    for body, comments in [
        (_body(ac=f"- AC1: {phrase}"), None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 94, "body": phrase}]),
    ]:
        policy = _web(_plan(body, comments))
        assert policy["required"] is False
        assert policy["critical_external_claims"] == []

    for external in (
        "GitHub GraphQL errors must match official docs before approval.",
        "GitHub GraphQL errors will be checked against official docs before approval.",
    ):
        fenced = _body(ac="- AC1: Run current-main repository tests.", vc=(
            f"```text\nDo not verify local fixtures and {external}\n```"
        ))
        assert not _web(_plan(fenced))["required"]


def test_parenthetical_quoted_example_does_not_erase_independent_current_claim():
    phrase = (
        "Do not verify local fixtures (example: GitHub GraphQL errors must match official docs) "
        "and GitHub GraphQL data/errors must match official docs before approval."
    )
    for body, comments, source_hint in [
        (_body(ac=f"- AC1: {phrase}"), None, None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 93, "body": phrase}], "comment_93"),
    ]:
        policy = _web(_plan(body, comments))
        claims = policy["critical_external_claims"]
        assert policy["required"] and len(claims) == 1, claims
        assert "GitHub GraphQL data/errors must match official docs" in claims[0]["claim"]
        assert "example:" not in claims[0]["claim"]
        assert claims[0]["source_hint"] == source_hint
        assert claims[0]["role"] == "dispositive"


def test_quoted_contrast_does_not_suppress_next_current_sentence():
    phrase = (
        "Previously quoted: Do not verify local fixtures, but verify GitHub GraphQL "
        "errors against official docs. Verify current GitHub GraphQL errors "
        "against official docs before approval."
    )
    for body, comments, source_hint in [
        (_body(ac=f"- AC1: {phrase}"), None, None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 94, "body": phrase}], "comment_94"),
    ]:
        claims = _web(_plan(body, comments))["critical_external_claims"]
        assert len(claims) == 1, claims
        assert "Verify current GitHub GraphQL errors" in claims[0]["claim"]
        assert "Previously quoted" not in claims[0]["claim"]
        assert claims[0]["source_hint"] == source_hint


@pytest.mark.parametrize("phrase", [
    "GitHub GraphQL errors will not be checked against official docs before approval.",
    "GitHub GraphQL errors won't be checked against official docs before approval.",
])
def test_negated_future_external_claims_do_not_require_web(phrase):
    for body, comments in [
        (_body(ac=f"- AC1: {phrase}"), None),
        (_body(ac="- AC1: Run current-main repository tests."),
         [{"id": 94, "body": phrase}]),
    ]:
        policy = _web(_plan(body, comments))
        assert policy["required"] is False
        assert policy["critical_external_claims"] == []


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
