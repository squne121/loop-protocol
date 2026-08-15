from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plan_refinement_loop.py"
SPEC = importlib.util.spec_from_file_location("plan_refinement_loop_main_drift", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SHA_A = "a" * 40
SHA_B = "b" * 40
ISSUE_BODY = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Parent Issue
#1

## Parent Goal Ref
- Goal: test

## Current Validated Scope
- docs/dev/

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome
Current-base evidence is required.

## In Scope
- docs/dev/workflow.md

## Out of Scope
- none

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: preserve current-base evidence epoch

## Verification Commands
```bash
echo hi
```

## Allowed Paths
- docs/dev/workflow.md

## Stop Conditions
- none

## Required Skills
- none
"""


def _context(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "current_base_sha": SHA_B,
        "evidence_base_sha": SHA_A,
        "allowed_paths_snapshot_base_sha": SHA_B,
        "allowed_paths": ["docs/dev/"],
        "latest_main_net_diff": ["docs/dev/workflow.md"],
        "expected_head_sha": SHA_A,
        "observed_head_sha": SHA_A,
        "expected_old_sha": SHA_B,
        "observed_old_sha": SHA_B,
    }
    value.update(extra)
    return value


def _planner_input(context: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "refinement_loop_planner_input/v1",
        "issue": {
            "number": 2102,
            "title": "main drift evidence epoch",
            "body": ISSUE_BODY,
            "labels": [],
        },
        "comments": [],
        "known_context": {"main_drift": context},
        "now": "2026-08-12T00:00:00+00:00",
    }


def test_given_drift_when_refinement_classifies_then_old_evidence_is_not_reusable():
    result = MODULE.classify_refinement_evidence_epoch(_context())

    assert result["route"] == "scope_clean_reconciliation"
    assert result["reusable_evidence"] == {
        "snapshot": None,
        "ci": None,
        "review": None,
    }
    assert result["mutation_owner"] == "refinement"


def test_given_drift_when_planner_runs_then_current_base_rebind_is_in_actual_output():
    plan, exit_code = MODULE.plan_refinement_loop(_planner_input(_context()))
    decision = plan["decisions"]["main_drift_evidence_epoch"]

    assert exit_code == 0
    assert decision["route"] == "scope_clean_reconciliation"
    assert decision["evidence_epoch"]["base_sha"] == SHA_B
    assert decision["evidence_epoch"]["implementation_iteration_delta"] == 0
    assert decision["reverify"] == {"snapshot": True, "ci": True, "review": True}


def test_given_stale_scope_snapshot_when_refinement_plans_then_it_fails_closed():
    plan, exit_code = MODULE.plan_refinement_loop(
        _planner_input(_context(allowed_paths_snapshot_base_sha=SHA_A))
    )

    assert exit_code == 0
    assert plan["fail_closed"]["required"] is True
    assert plan["fail_closed"]["reason_codes"] == ["stale_allowed_paths_snapshot"]



# ---------------------------------------------------------------------------
# Issue #2102 fix_delta iteration 4, Blocker 4: production wiring tests.
# These exercise the CANONICAL entrypoint (run_refinement_preflight.py's
# `build_live_main_drift_known_context()` / `run_preflight()`) against real
# git subprocess state -- never a hand-injected `known_context["main_drift"]`
# dict -- to prove the producer described as "dead code in production" in
# the prior iteration's contract comment is now actually reachable.
# ---------------------------------------------------------------------------

PREFLIGHT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_refinement_preflight.py"
PREFLIGHT_SPEC = importlib.util.spec_from_file_location("run_refinement_preflight_main_drift", PREFLIGHT_SCRIPT)
assert PREFLIGHT_SPEC and PREFLIGHT_SPEC.loader
PREFLIGHT_MODULE = importlib.util.module_from_spec(PREFLIGHT_SPEC)
sys.modules[PREFLIGHT_SPEC.name] = PREFLIGHT_MODULE
PREFLIGHT_SPEC.loader.exec_module(PREFLIGHT_MODULE)


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_real_repo_with_origin(tmp_path):
    """A real bare `origin` + real clone -- no hand-injected SHAs."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["-c", "init.defaultBranch=main", "init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "docs" / "dev").mkdir(parents=True)
    (repo / "docs" / "dev" / "workflow.md").write_text("base\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "origin", "main"], repo)
    return repo


def test_given_real_git_state_when_producer_reads_live_then_no_drift_yields_no_semantic_ambiguity(tmp_path):
    """Production semantic_ambiguity: an unrelated, non-conflicting real
    commit landing on origin/main must resolve `semantic_ambiguity=False`
    via the real `git merge-tree --write-tree` oracle -- not an asserted
    boolean."""
    repo = _init_real_repo_with_origin(tmp_path)
    evidence_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    (repo / "docs" / "dev" / "another.md").write_text("net-new file\n")
    _git(["add", "docs/dev/another.md"], repo)
    _git(["commit", "-q", "-m", "advance main"], repo)
    _git(["push", "-q", "origin", "main"], repo)
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert current_sha != evidence_sha

    result = PREFLIGHT_MODULE.build_live_main_drift_known_context(
        repo_root=repo,
        issue_body=ISSUE_BODY,
        evidence_base_sha=evidence_sha,
    )

    assert result is not None
    assert result["current_base_sha"] == current_sha
    assert result["evidence_base_sha"] == evidence_sha
    assert result["semantic_ambiguity"] is False
    assert "docs/dev/another.md" in result["latest_main_net_diff"]
    assert result["allowed_paths"] == ["docs/dev/workflow.md"]


def test_given_real_conflicting_git_state_when_producer_reads_live_then_semantic_ambiguity_is_true(tmp_path):
    """Same real-git oracle, but the evidence and live tips make genuinely
    conflicting edits to the SAME file -- the merge-tree probe must report
    `semantic_ambiguity=True` from real conflict detection, not a flag."""
    repo = _init_real_repo_with_origin(tmp_path)
    subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )

    _git(["checkout", "-q", "-b", "conflicting-branch"], repo)
    (repo / "docs" / "dev" / "workflow.md").write_text("branch edit\n")
    _git(["add", "docs/dev/workflow.md"], repo)
    _git(["commit", "-q", "-m", "branch edit"], repo)
    branch_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    _git(["checkout", "-q", "main"], repo)
    (repo / "docs" / "dev" / "workflow.md").write_text("main edit\n")
    _git(["add", "docs/dev/workflow.md"], repo)
    _git(["commit", "-q", "-m", "main edit"], repo)
    _git(["push", "-q", "origin", "main"], repo)
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    result = PREFLIGHT_MODULE.build_live_main_drift_known_context(
        repo_root=repo,
        issue_body=ISSUE_BODY,
        evidence_base_sha=branch_sha,
    )

    assert result is not None
    assert result["current_base_sha"] == current_sha
    assert result["semantic_ambiguity"] is True


def test_given_live_mode_when_run_preflight_executes_then_main_drift_is_populated_without_hand_injection(
    tmp_path, monkeypatch
):
    """Canonical entrypoint: `run_preflight(..., fixture_path=None,
    known_context=None)` must itself construct `known_context["main_drift"]`
    from the real git repo -- the caller supplies NO main_drift facts at
    all, proving the CLI/orchestrator path (not merely a direct unit call
    with hand-injected `known_context`) reaches the producer."""
    repo = _init_real_repo_with_origin(tmp_path)

    monkeypatch.setattr(PREFLIGHT_MODULE, "_find_repo_root", lambda: repo)
    monkeypatch.setattr(
        PREFLIGHT_MODULE,
        "_fetch_issue",
        lambda repo_arg, issue_number: (
            {"number": issue_number, "title": "t", "body": ISSUE_BODY, "labels": [], "html_url": "https://x"},
            "",
        ),
    )
    monkeypatch.setattr(PREFLIGHT_MODULE, "_fetch_issue_comments", lambda repo_arg, issue_number: ([], ""))

    result, _exit_code = PREFLIGHT_MODULE.run_preflight(
        issue_number=2102,
        repo="squne121/loop-protocol",
        anchor_comment_urls=[],
        fixture_path=None,
        known_context=None,
        enable_main_drift_live_readback=True,
    )

    planner_input_path = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "2102" / "planner_input.json"
    assert planner_input_path.exists()
    written = json.loads(planner_input_path.read_text(encoding="utf-8"))
    main_drift = written.get("known_context", {}).get("main_drift")
    assert main_drift is not None, "known_context[main_drift] must be populated by the live orchestrator path"
    current_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert main_drift["current_base_sha"] == current_sha
