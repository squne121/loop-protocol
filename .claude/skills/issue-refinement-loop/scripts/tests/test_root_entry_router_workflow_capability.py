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
import os
import stat
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import root_entry_router as rer  # noqa: E402

_REPO = "squne121/loop-protocol"
_REPO_ROOT = Path(__file__).resolve().parents[5]
_FAKE_GH_FIXTURE = _REPO_ROOT / "scripts" / "claude-gpt" / "tests" / "fixtures" / "fake_gh.py"


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


# --- P0-2: capability-preflight-blocked must not publish an audit comment ---


def test_capability_preflight_blocked_publishes_no_audit_comment(tmp_path):
    """GIVEN capability_preflight() reports False (e.g. trusted uv missing,
    fake `gh auth status` unavailable -- any preflight-level blocked
    decision)
    WHEN run_root_transition is called with publish_audit=True (the
    production default)
    THEN the STOP route is returned AND no audit comment is posted -- a
    blocked/unverifiable capability state must never itself become the
    trigger for a GitHub mutation (P0-2 fix; before the fix, `_finish`
    unconditionally published an audit comment for every STOP route,
    including this one)."""

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                "capability_ok": False,
                "repo_identity": _REPO,
                "issues": {"2273": {"body": "irrelevant", "base_sha": "irrelevant"}},
            }
        ),
        encoding="utf-8",
    )
    transport = rer.FileBackedFakeGitHubEntryTransport(str(fixture_path))

    result = rer.run_root_transition(
        issue_number=2273,
        repo=_REPO,
        transport=transport,
        contract_reviewer=lambda **_kwargs: {"status": "go", "body_sha256": "irrelevant"},
        invoke_step1=lambda: None,
        expected_repository_identity=_REPO,
        publish_audit=True,
    )

    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["route"]["reason"] == "capability_or_identity_unverifiable"
    assert result["invoked"] is False
    assert transport._posted == []
    assert result["audit"]["published"] is None
    assert result["audit"]["warning"] is None


# --- P0-3: production consumer declares its own planned audit mutation ---


def test_gh_cli_transport_declares_spark_and_planned_operations(monkeypatch, tmp_path):
    """GIVEN a GhCliGitHubEntryTransport constructed with spark_mode /
    spark_fallback / planned_operations
    WHEN capability_preflight() dispatches to workflow_capability_preflight.py
    THEN the subprocess argv carries --spark-mode / --spark-fallback /
    --planned-operations-json, and the JSON file behind
    --planned-operations-json contains the declared operations (P0-3 fix:
    the production transport must self-declare the audit `issue_comment`
    mutation it is about to perform, not let the preflight pass on an
    empty/unstated operation set)."""

    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        planned_ops_path = argv[argv.index("--planned-operations-json") + 1]
        # Read the temp file's contents WHILE it still exists (the
        # production code deletes it in a `finally` after this call
        # returns), so capture the written payload here.
        with open(planned_ops_path, "r", encoding="utf-8") as fh:
            captured["written"] = json.load(fh)
        captured["planned_ops_path"] = planned_ops_path
        return subprocess.CompletedProcess(
            argv, 0, stdout=_fake_workflow_capability_result("ready"), stderr=""
        )

    monkeypatch.setattr(rer.subprocess, "run", _fake_run)

    transport = rer.GhCliGitHubEntryTransport(
        repo=_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=(
            {
                "phase": "audit",
                "actor_role": "root_entry_router",
                "operation": "issue_comment",
                "requires_mutation": True,
            },
        ),
    )

    assert transport.capability_preflight() is True

    argv = captured["argv"]
    assert "--spark-mode" in argv
    assert argv[argv.index("--spark-mode") + 1] == "required"
    assert "--spark-fallback" in argv
    assert argv[argv.index("--spark-fallback") + 1] == "forbidden"
    assert "--planned-operations-json" in argv
    assert captured["written"] == [
        {
            "phase": "audit",
            "actor_role": "root_entry_router",
            "operation": "issue_comment",
            "requires_mutation": True,
        }
    ]
    # The temp file must be cleaned up after the subprocess call completes.
    assert not Path(captured["planned_ops_path"]).exists()


def test_gh_cli_transport_default_planned_operations_declares_issue_comment():
    """GIVEN a GhCliGitHubEntryTransport constructed with defaults (no
    explicit planned_operations override)
    THEN its default planned_operations already declares the `issue_comment`
    audit mutation this router performs -- the production call site (the CLI
    entrypoint / `_select_transport`) does not have to remember to pass it
    explicitly."""

    transport = rer.GhCliGitHubEntryTransport(repo=_REPO)
    ops = [entry["operation"] for entry in transport.planned_operations]
    assert "issue_comment" in ops



# --- P1-4: real producer/consumer integration via a fake `gh` on PATH -----


def _install_fake_gh(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} "{_FAKE_GH_FIXTURE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return wrapper


def test_real_producer_blocked_on_fake_gh_auth_failure_posts_no_comment(tmp_path, monkeypatch):
    """GIVEN the REAL `workflow_capability_preflight.py` producer, driven
    through the REAL `GhCliGitHubEntryTransport.capability_preflight()`
    consumer (no `subprocess.run` monkeypatch), with a fake `gh auth
    status` that fails
    WHEN `run_root_transition` is called
    THEN capability_preflight() reports blocked (real subprocess, real
    argv), the router stops before any mutation, and the fake `gh`'s
    persisted state records zero posted issue comments (P1-4: hermetic
    producer/consumer integration, not a canned-JSON `subprocess.run`
    monkeypatch)."""

    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    state_path = tmp_path / "fake_gh_state.json"

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "0")

    transport = rer.GhCliGitHubEntryTransport(repo=_REPO)

    assert transport.capability_preflight() is False

    invoked = {"count": 0}
    result = rer.run_root_transition(
        issue_number=2273,
        repo=_REPO,
        transport=transport,
        contract_reviewer=lambda **_kwargs: {"status": "go", "body_sha256": "irrelevant"},
        invoke_step1=lambda: invoked.__setitem__("count", invoked["count"] + 1),
        expected_repository_identity=_REPO,
        publish_audit=True,
    )

    assert result["route"]["route"] == rer.ROUTE_STOP
    assert result["invoked"] is False
    assert invoked["count"] == 0
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state.get("comments", {}) == {}


def test_real_producer_receives_spark_directive_and_planned_operations(tmp_path, monkeypatch):
    """GIVEN the REAL `workflow_capability_preflight.py` producer, driven
    through the REAL `GhCliGitHubEntryTransport.capability_preflight()`
    consumer, with a required Spark directive and an UNREGISTERED planned
    operation
    THEN the real producer receives and acts on both: the unregistered
    operation makes it `route: unavailable` (visible via a blocked
    decision), proving `--spark-mode`/`--spark-fallback`/
    `--planned-operations-json` actually reach the real subprocess argv end
    to end (not just the unit-level argv assertion in
    ``test_gh_cli_transport_declares_spark_and_planned_operations``)."""

    bin_dir = tmp_path / "bin"
    _install_fake_gh(bin_dir)
    state_path = tmp_path / "fake_gh_state.json"

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state_path))
    monkeypatch.setenv("FAKE_GH_AUTH_OK", "1")
    monkeypatch.setenv("FAKE_GH_REPO_READ_OK", "1")

    transport = rer.GhCliGitHubEntryTransport(
        repo=_REPO,
        spark_mode="required",
        spark_fallback="forbidden",
        planned_operations=(
            {
                "phase": "audit",
                "actor_role": "root_entry_router",
                "operation": "an_unregistered_operation_for_p1_4",
                "requires_mutation": True,
            },
        ),
    )

    # The real producer has no implemented route for this operation name,
    # so it must report decision=blocked -- proving the planned_operations
    # payload written by the consumer was actually read and evaluated by
    # the real subprocess, not merely constructed and discarded.
    assert transport.capability_preflight() is False
