"""Issue #2052 AC6: an integration test that ACTUALLY goes through
``.claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py``'s
real ``run_preflight()`` live-mode Issue-fetch path (not a hermetic
reimplementation of it), proving that enabling the opt-in
``evidence_cache_enabled`` flag:

1. never changes the resulting decision (``status`` / ``next_action`` /
   ``blockers`` / ``must_read`` / ``commands``) -- cache on/off semantic
   equivalence;
2. measurably reduces the number of REAL resolution operations for a
   genuine, PRODUCTION-PATH duplicate consumer this fix_delta actually
   found and fixed (Issue #2052 fix_delta A): when an anchor comment URL is
   supplied, ``run_preflight()`` resolves the exact same ``comment_id``
   TWICE within a single invocation -- once via
   ``_validate_anchor_comments_batch()`` (structural validation) and again
   via its own post-batch-validation re-resolution just below. Both call
   sites route through the shared ``_resolve_anchor_comment_payload()``
   helper; with the cache enabled the second resolution is served from
   cache (0 additional list-scan/GET operations), and without it each
   resolution does its own independent work (2 total). This is asserted
   PURELY by observing ``run_preflight()``'s own single call -- no
   artificial extra call to ``evidence_index.get_or_fetch()`` is made by
   this test after ``run_preflight()`` returns (a prior version of this
   test did exactly that, which only proved the cache primitive works in
   isolation, never that ``run_preflight()`` itself produces a single
   cache hit in normal operation); and
3. writes the new, purely-additive ``context_budget_report.json`` artifact
   reflecting only the observed metrics -- never one of the three existing
   fixed-schema artifacts (``raw_issue_snapshot.json`` /
   ``planner_input.json`` / ``refinement_preflight_result_v1.json``, which
   this test also asserts are unaffected).

``_fetch_issue`` / ``_fetch_issue_comments`` are monkeypatched (there is no
network access here) but ``run_preflight()``'s own orchestration, its
evidence-cache wiring, and the real ``plan_refinement_loop.py`` planner
subprocess it invokes are all exercised for real. The underlying
comment-list-scan primitive (``_lookup_comment_in_fetched_list()``) is
wrapped with a call counter (not monkeypatched away) so this test observes
the REAL number of times ``run_preflight()`` itself performs that
resolution work -- the exact same production function both call sites
share.
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

import run_refinement_preflight as preflight  # noqa: E402


ISSUE_NUMBER = 2052
REPO = "testowner/testrepo"


def _artifact_dir(repo_root: Path) -> Path:
    """The per-issue artifact directory shared by every fixed-schema and
    purely-additive artifact this test asserts on."""
    return repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)

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


ANCHOR_COMMENT_ID = 5550000001
ANCHOR_COMMENT_URL = f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}#issuecomment-{ANCHOR_COMMENT_ID}"


def _anchor_comment_payload() -> dict:
    """A schema-conformant (``anchor_comment.schema.json``) raw comment
    payload for ``ANCHOR_COMMENT_ID`` -- present in the SAME paginated
    comments list ``_fetch_issue_comments()`` returns, so `run_preflight()`
    resolves it twice per invocation (Issue #2052 fix_delta A's actual
    production duplicate consumer): once via
    `_validate_anchor_comments_batch()`, once via its own
    post-batch-validation re-resolution."""
    return {
        "id": ANCHOR_COMMENT_ID,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}",
        "html_url": ANCHOR_COMMENT_URL,
        "url": f"https://api.github.com/repos/{REPO}/issues/comments/{ANCHOR_COMMENT_ID}",
        "user": {"login": "octocat"},
        "author_association": "OWNER",
        "body": "anchor comment body for evidence cache integration test",
        "created_at": "2026-09-05T00:00:00Z",
        "updated_at": "2026-09-05T00:00:00Z",
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
    comments_fetch_calls = {"n": 0}
    # Issue #2052 fix_delta A: count REAL invocations of the actual
    # production resolution primitive both `_validate_anchor_comment_url()`
    # and `run_preflight()`'s own post-batch-validation re-resolution share
    # (via `_resolve_anchor_comment_payload()`) -- this is the genuine
    # duplicate consumer this fix_delta found and fixed. Wrapping (not
    # monkeypatching away) the real function means this counts EXACTLY how
    # many times `run_preflight()` itself actually performs that lookup
    # work, not an artificial proxy.
    comment_lookup_calls = {"n": 0}
    _original_lookup = preflight._lookup_comment_in_fetched_list

    def _counting_lookup(comments, comment_id):
        comment_lookup_calls["n"] += 1
        return _original_lookup(comments, comment_id)

    def _fake_fetch_issue(repo_arg, issue_number):
        assert repo_arg == REPO
        assert issue_number == ISSUE_NUMBER
        issue_fetch_calls["n"] += 1
        return _issue_payload(), ""

    def _fake_fetch_issue_comments(repo_arg, issue_number):
        comments_fetch_calls["n"] += 1
        return [_anchor_comment_payload()], ""

    monkeypatch.setattr(preflight, "_fetch_issue", _fake_fetch_issue)
    monkeypatch.setattr(preflight, "_fetch_issue_comments", _fake_fetch_issue_comments)
    monkeypatch.setattr(preflight, "_lookup_comment_in_fetched_list", _counting_lookup)

    # --- Baseline: evidence cache disabled (pre-existing default behavior). ---
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root_disabled)
    issue_fetch_calls["n"] = 0
    comments_fetch_calls["n"] = 0
    comment_lookup_calls["n"] = 0
    result_disabled, exit_code_disabled = preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=[ANCHOR_COMMENT_URL],
        fixture_path=None,
        known_context=None,
        now="2026-09-05T00:00:00+00:00",
        evidence_cache_enabled=False,
    )
    assert issue_fetch_calls["n"] == 1
    assert comments_fetch_calls["n"] == 1
    # Disabled (pre-existing default behavior): the SAME comment_id is
    # resolved from the in-memory comments list TWICE -- once by
    # `_validate_anchor_comments_batch()`, once by `run_preflight()`'s own
    # post-batch-validation re-resolution. This is the real, un-suppressed
    # duplicate this fix_delta's cache wiring targets.
    assert comment_lookup_calls["n"] == 2, (
        "evidence cache disabled: the anchor comment is genuinely resolved twice per invocation"
    )

    raw_snapshot_disabled = json.loads(
        (_artifact_dir(repo_root_disabled) / "raw_issue_snapshot.json").read_text(encoding="utf-8")
    )
    planner_input_disabled = json.loads(
        (_artifact_dir(repo_root_disabled) / "planner_input.json").read_text(encoding="utf-8")
    )

    # --- Cache enabled (independent repo_root). ---
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root_enabled)
    issue_fetch_calls["n"] = 0
    comments_fetch_calls["n"] = 0
    comment_lookup_calls["n"] = 0
    result_enabled, exit_code_enabled = preflight.run_preflight(
        issue_number=ISSUE_NUMBER,
        repo=REPO,
        anchor_comment_urls=[ANCHOR_COMMENT_URL],
        fixture_path=None,
        known_context=None,
        now="2026-09-05T00:00:00+00:00",
        evidence_cache_enabled=True,
    )
    assert issue_fetch_calls["n"] == 1
    assert comments_fetch_calls["n"] == 1
    # Enabled: the SECOND resolution of the identical comment_id is served
    # from the phase-scoped EvidenceIndex cache instead of performing a
    # second real list-scan resolution -- this is the actual,
    # production-path fetch/resolution-count reduction AC1/AC6 require
    # (proven here SOLELY by observing `run_preflight()`'s own single call,
    # with no artificial extra cache reference made by this test).
    assert comment_lookup_calls["n"] == 1, (
        "evidence cache enabled: the second reference to the same comment_id must be served from "
        "cache, not perform a second real resolution"
    )

    raw_snapshot_enabled = json.loads(
        (_artifact_dir(repo_root_enabled) / "raw_issue_snapshot.json").read_text(encoding="utf-8")
    )
    planner_input_enabled = json.loads(
        (_artifact_dir(repo_root_enabled) / "planner_input.json").read_text(encoding="utf-8")
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
    report_path = _artifact_dir(repo_root_enabled) / "context_budget_report.json"
    assert report_path.is_file()
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_payload["schema"] == "CONTEXT_BUDGET_REPORT_V1"
    phase_metrics = report_payload["phases"][preflight.EVIDENCE_CACHE_PHASE_PREFLIGHT_FETCH]
    # fetch_count: one real issue_body fetch + one real comment resolution
    # (the SECOND comment reference is the cache hit, not counted here).
    assert phase_metrics["fetch_count"] == 2
    assert phase_metrics["emitted_utf8_bytes"] > 0
    # The second, cache-served comment reference IS the observed reuse.
    assert phase_metrics["snapshot_reuse_count"] == 1

    disabled_report_path = _artifact_dir(repo_root_disabled) / "context_budget_report.json"
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

    report_path = _artifact_dir(repo_root) / "context_budget_report.json"
    assert not report_path.exists()
