""".claude/skills/issue-refinement-loop/scripts/tests/test_root_entry_router_workflow_capability.py

Issue #2273 AC15/AC16: `root_entry_router.GhCliGitHubEntryTransport.
capability_preflight()` must consume the structured
`CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1` result produced by
`scripts/claude-gpt/workflow_capability_preflight.py` (AC15), and a
positive fixture (native GitHub auth + repository read + trusted `uv`
available, no required Spark failure) must let `run_root_transition`
advance to the fresh-review phase exactly once (AC16).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as rer  # noqa: E402

_REPO = "squne121/loop-protocol"


def _fake_workflow_capability_result(decision: str) -> str:
    return json.dumps(
        {
            "schema": "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1",
            "profile": "issue-to-impl",
            "decision": decision,
            "checks": {
                "uv": {"status": "ok", "reason": "resolved"},
                "spark": {"status": "not_required"},
                "github": {"auth": True, "repo_read": True, "operations": {}},
            },
            "reasons": [],
        }
    )


# --- AC15 --------------------------------------------------------------


def test_root_entry_router_consumes_structured_workflow_capabilities(monkeypatch):
    """GIVEN GhCliGitHubEntryTransport.capability_preflight()
    WHEN the workflow_capability_preflight.py subprocess reports
    decision=blocked / decision=ready
    THEN capability_preflight() returns False / True respectively, driven
    by the structured `decision` field -- NOT a bare `gh auth status`
    boolean gate."""

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert any(
            "workflow_capability_preflight.py" in str(part) for part in argv
        ), "capability_preflight must dispatch to workflow_capability_preflight.py, not raw gh auth status"
        assert argv[:2] != ["gh", "auth"], "must not fall back to raw `gh auth status` boolean gate"
        decision = _pending_decision["value"]
        return subprocess.CompletedProcess(
            argv, 0, stdout=_fake_workflow_capability_result(decision), stderr=""
        )

    monkeypatch.setattr(rer.subprocess, "run", _fake_run)
    transport = rer.GhCliGitHubEntryTransport(repo=_REPO)

    _pending_decision = {"value": "blocked"}
    assert transport.capability_preflight() is False

    _pending_decision["value"] = "ready"
    assert transport.capability_preflight() is True

    _pending_decision["value"] = "degraded"
    assert transport.capability_preflight() is True

    assert len(calls) == 3


# --- AC16 ----------------------------------------------------------------


def test_root_entry_router_advances_once_on_positive_fixture(monkeypatch):
    """GIVEN native GitHub auth, repository read, and trusted `uv` are all
    available with no required Spark failure (positive fixture, reusing
    the `decision: ready`/`degraded` semantics established by PR #2303's
    fake_gh.py-style positive lane)
    WHEN run_root_transition drives a GhCliGitHubEntryTransport through the
    real capability_preflight() wiring
    THEN it advances to the fresh-review phase (fetch_live_issue) exactly
    once -- no loop, no double invocation -- and reaches ROUTE_INVOKE."""

    def _fake_run(argv, **kwargs):
        if any("workflow_capability_preflight.py" in str(part) for part in argv):
            return subprocess.CompletedProcess(
                argv, 0, stdout=_fake_workflow_capability_result("ready"), stderr=""
            )
        raise AssertionError(f"unexpected subprocess.run call in this test: {argv}")

    monkeypatch.setattr(rer.subprocess, "run", _fake_run)

    transport = rer.GhCliGitHubEntryTransport(repo=_REPO)
    monkeypatch.setattr(transport, "canonical_repository_identity", lambda: _REPO)

    fetch_calls = {"count": 0}
    fixed_body = "## Outcome\npositive fixture body"
    fixed_base_sha = "positive-fixture-base-sha"

    def _fake_fetch_live_issue(issue_number):
        fetch_calls["count"] += 1
        return {
            "body": fixed_body,
            "base_sha": fixed_base_sha,
            "identity_ok": True,
            "fetch_ok": True,
        }

    monkeypatch.setattr(transport, "fetch_live_issue", _fake_fetch_live_issue)
    monkeypatch.setattr(
        transport, "post_comment", lambda issue_number, body: {"ok": True, "comment_id": 1}
    )

    expected_body_sha = rer.compute_body_sha256(fixed_body)

    def _fake_contract_reviewer(**_kwargs):
        return {"status": "go", "body_sha256": expected_body_sha}

    invoke_calls = {"count": 0}

    def _invoke_step1():
        invoke_calls["count"] += 1

    result = rer.run_root_transition(
        issue_number=2273,
        repo=_REPO,
        transport=transport,
        contract_reviewer=_fake_contract_reviewer,
        invoke_step1=_invoke_step1,
        expected_repository_identity=_REPO,
        publish_audit=False,
    )

    assert result["route"]["route"] == rer.ROUTE_INVOKE
    assert result["invoked"] is True
    assert invoke_calls["count"] == 1
    # `_fetch()` is called twice by design inside run_root_transition (once
    # before the contract-review loop, once after, to detect drift while the
    # reviewer ran) for a SINGLE positive attempt -- not once per retry
    # iteration/loop pass. Assert there was exactly one contract-review
    # attempt by checking the loop did not retry (retry_count stays 0).
    assert result["route"]["retry_count"] == 0
    assert fetch_calls["count"] == 2
