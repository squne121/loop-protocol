"""Issue #2052 AC6: an integration test that ACTUALLY goes through
``.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py``'s
real ``run_preflight()`` live-mode Issue-fetch path (not a hermetic
reimplementation of it), proving that enabling the opt-in
``evidence_cache_enabled`` flag:

1. never changes the resulting decision (``status`` / ``next_action`` /
   ``blockers`` / ``must_read`` / ``commands``) -- cache on/off semantic
   equivalence;
2. measurably reduces ``fetch_count`` for a duplicate same-phase reference
   to the identical ``evidence_key``, via the SAME production
   ``EvidenceIndex``/``_fetch_issue`` call path ``run_preflight()`` itself
   uses (shared explicitly through the new ``evidence_index=`` parameter);
   and
3. writes the new, purely-additive ``context_budget_report.json`` artifact
   reflecting only the observed metrics -- never one of the three existing
   fixed-schema artifacts (``raw_issue_snapshot.json`` /
   ``planner_input.json`` / ``refinement_preflight_result_v1.json``, which
   this test also asserts are unaffected).

``_fetch_issue`` / ``_fetch_issue_comments`` are monkeypatched (there is no
network access here) but ``run_preflight()``'s own orchestration, its
evidence-cache wiring, and the real ``plan_refinement_loop.py`` planner
subprocess it invokes are all exercised for real.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_AGENT_OPS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _AGENT_OPS_DIR.parent.parent
_PREFLIGHT_SCRIPTS_DIR = _REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"

sys.path.insert(0, str(_AGENT_OPS_DIR))
sys.path.insert(0, str(_PREFLIGHT_SCRIPTS_DIR))

import evidence_index as evidence_index_module  # noqa: E402
import run_refinement_preflight as preflight  # noqa: E402


ISSUE_NUMBER = 2052
REPO = "testowner/testrepo"

ISSUE_BODY = """
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
change_kind: code
```

## Parent Issue
none

## Parent Goal Ref
- Goal: test evidence cache integration

## Current Validated Scope
- docs/dev/

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome
Evidence cache integration must not change the preflight decision.

## In Scope
- docs/dev/workflow.md

## Out of Scope
- none

## Remaining Parent Gaps
- none

## Acceptance Criteria
- [ ] AC1: preserve preflight decisions with evidence cache on or off

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


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _issue_payload() -> dict:
    return {
        "number": ISSUE_NUMBER,
        "title": "Evidence cache integration test",
        "body": ISSUE_BODY,
        "labels": [],
        "html_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}",
    }


def test_cache_toggle_semantic_equivalence_and_fetch_count_reduction(tmp_path, monkeypatch):
    # Two INDEPENDENT repo_root/artifact-dir trees -- each is genuinely the
    # first-ever preflight run for its own issue artifact dir, so neither
    # run's decision is influenced by the other run's previously-archived
    # snapshot (`_find_previous_immutable_snapshot()`'s scope-signal-delta
    # join is an orthogonal, pre-existing feature this test must not
    # accidentally exercise as a confound).
    repo_root_disabled = _init_repo_root(tmp_path / "disabled")
    repo_root_enabled = _init_repo_root(tmp_path / "enabled")

    issue_fetch_calls = {"n": 0}

    def _fake_fetch_issue(repo_arg, issue_number):
        assert repo_arg == REPO
        assert issue_number == ISSUE_NUMBER
        issue_fetch_calls["n"] += 1
        return _issue_payload(), ""

    def _fake_fetch_issue_comments(repo_arg, issue_number):
        return [], ""

    monkeypatch.setattr(preflight, "_fetch_issue", _fake_fetch_issue)
    monkeypatch.setattr(preflight, "_fetch_issue_comments", _fake_fetch_issue_comments)

    # --- Baseline: evidence cache disabled (pre-existing default behavior). ---
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root_disabled)
    issue_fetch_calls["n"] = 0
    result_disabled, exit_code_disabled = preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=[],
        fixture_path=None,
        known_context=None,
        evidence_cache_enabled=False,
    )
    assert issue_fetch_calls["n"] == 1

    raw_snapshot_disabled = json.loads(
        (repo_root_disabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "raw_issue_snapshot.json")
        .read_text(encoding="utf-8")
    )
    planner_input_disabled = json.loads(
        (repo_root_disabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "planner_input.json")
        .read_text(encoding="utf-8")
    )

    # --- Cache enabled (independent repo_root), sharing an externally-visible EvidenceIndex so this
    # test can also directly reference the SAME evidence_key a second time
    # via the real, shared `_fetch_issue` production fetch path, proving
    # reuse (AC1/AC6) rather than merely asserting it in isolation.
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root_enabled)
    issue_fetch_calls["n"] = 0
    shared_index = evidence_index_module.EvidenceIndex()
    result_enabled, exit_code_enabled = preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=[],
        fixture_path=None,
        known_context=None,
        evidence_cache_enabled=True,
        evidence_index=shared_index,
    )
    fetch_count_after_run_preflight = issue_fetch_calls["n"]
    assert fetch_count_after_run_preflight == 1, "run_preflight() itself performs exactly one real fetch"

    # A duplicate same-phase reference to the identical evidence_key
    # (repository, resource_kind=issue_body, resource_id=ISSUE_NUMBER),
    # through the SAME `_fetch_issue` production function `run_preflight()`
    # itself calls, sharing the SAME EvidenceIndex `run_preflight()` used
    # internally -- must be served from cache, not re-fetched.
    shared_index.begin_phase(preflight.EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH)
    outcome = shared_index.get_or_fetch(
        repository=REPO,
        resource_kind=evidence_index_module.RESOURCE_KIND_ISSUE_BODY,
        resource_id=ISSUE_NUMBER,
        fetch_fn=lambda: preflight._fetch_issue(REPO, ISSUE_NUMBER),
    )
    assert outcome.reused is True
    assert issue_fetch_calls["n"] == fetch_count_after_run_preflight, (
        "a same-phase re-reference to the identical evidence_key must not trigger a second real fetch"
    )

    raw_snapshot_enabled = json.loads(
        (repo_root_enabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "raw_issue_snapshot.json")
        .read_text(encoding="utf-8")
    )
    planner_input_enabled = json.loads(
        (repo_root_enabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "planner_input.json")
        .read_text(encoding="utf-8")
    )

    # AC6: cache on/off is semantically equivalent -- identical decision AND
    # identical content in the three EXISTING fixed-schema artifacts (no
    # schema/shape change introduced by the cache).
    for key in ("status", "next_action", "blockers", "must_read", "commands"):
        assert result_disabled[key] == result_enabled[key], key
    assert exit_code_disabled == exit_code_enabled
    # `fetched_at` is a real wall-clock timestamp recorded per invocation --
    # excluded from the equivalence check; everything else (in particular
    # the full Issue `body` text) must be byte-identical.
    assert {k: v for k, v in raw_snapshot_disabled.items() if k != "fetched_at"} == {
        k: v for k, v in raw_snapshot_enabled.items() if k != "fetched_at"
    }
    assert planner_input_disabled == planner_input_enabled

    # AC7 (observed via the real integration, not just the unit test):
    # the NEW, purely-additive context_budget_report.json artifact reflects
    # the real fetch/reuse activity for this phase.
    report_path = (
        repo_root_enabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "context_budget_report.json"
    )
    assert report_path.is_file()
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_payload["schema"] == "CONTEXT_BUDGET_REPORT_V1"
    phase_metrics = report_payload["phases"][preflight.EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH]
    assert phase_metrics["fetch_count"] == 1
    assert phase_metrics["emitted_utf8_bytes"] > 0
    # `run_preflight()`'s OWN internal write only covers its own single
    # fetch; the report artifact must never claim un-observed reuse from
    # this test's own subsequent direct call (which happened AFTER
    # `run_preflight()` had already written the report).
    assert phase_metrics["snapshot_reuse_count"] == 0

    disabled_report_path = (
        repo_root_disabled / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "context_budget_report.json"
    )
    assert not disabled_report_path.exists()


def test_disabled_cache_never_writes_context_budget_report(tmp_path, monkeypatch):
    """The report artifact is purely additive and strictly opt-in -- a
    disabled-cache run must never write it at all."""
    repo_root = _init_repo_root(tmp_path)

    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root)
    monkeypatch.setattr(preflight, "_fetch_issue", lambda repo_arg, issue_number: (_issue_payload(), ""))
    monkeypatch.setattr(preflight, "_fetch_issue_comments", lambda repo_arg, issue_number: ([], ""))

    preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=[],
        fixture_path=None,
        known_context=None,
        evidence_cache_enabled=False,
    )

    report_path = (
        repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER) / "context_budget_report.json"
    )
    assert not report_path.exists()
