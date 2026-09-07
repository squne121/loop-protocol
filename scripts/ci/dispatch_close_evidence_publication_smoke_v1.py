#!/usr/bin/env python3
"""Issue #2555 AC8: bounded runtime-verification smoke run for the
`close-evidence-publication` `workflow_dispatch` opt-in job added to
`.github/workflows/ci.yml`.

This module never re-implements the job's own logic (artifact download,
`build_close_evidence_bundle_v1.py` / `validate_close_evidence_bundle_v1.py`,
`actions/upload-artifact@v7`, `build_publication_receipt()`) -- it only
drives the REAL `close-evidence-publication` job end-to-end through the
GitHub Actions REST/CLI surface (`gh workflow run` / `gh run` / `gh api`)
and asserts on the outcome:

1. resolve preconditions (GH token with `actions: read` scope, an existing
   non-expired Reliability close-grade artifact ID, and a pushed ref this
   workflow_dispatch can target) -- SKIP (never FAIL) when any precondition
   cannot be resolved (`docs/dev/runtime-verification-policy.md` SKIP
   contract: SKIP != PASS).
2. `gh workflow run` the `ci.yml` workflow with
   `close_evidence_source_artifact_id=<resolved id>` on the resolved ref.
3. poll the newly created `workflow_dispatch` run until `status: completed`
   (bounded retries -- never an unbounded loop).
4. assert the `close-evidence-publication` job's own conclusion is
   `success`, and that BOTH `close-evidence-bundle-v1` (artifact A) and
   `close-evidence-publication-receipt-v1` (artifact B) exist for that run.

Exit codes (mirrors the pytest wrapper's `pytest.skip()` mapping to
exit 77 -- see `docs/dev/runtime-verification-policy.md`):
    0  PASS
    1  FAIL (job ran but did not satisfy the AC8 assertions)
    77 SKIP (a precondition could not be resolved in this environment)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

WORKFLOW_FILE = "ci.yml"
JOB_NAME = "close-evidence-publication"
ARTIFACT_A_NAME = "close-evidence-bundle-v1"
ARTIFACT_B_NAME = "close-evidence-publication-receipt-v1"
RELIABILITY_ARTIFACT_NAME_PREFIX = "ci-reliability-close-grade-result-"
# PR #2559 review fix_delta (item D): the artifact-name-prefix match alone
# does not prove the artifact's OWN producing job succeeded (a failed run
# still uploads via ci.yml's `if: !cancelled()`) -- this is the exact job
# name (`.github/workflows/ci.yml`'s `reliability-assessment:` job) whose
# `conclusion` must be `success` for a candidate artifact to be usable.
RELIABILITY_JOB_NAME = "reliability-assessment"

# Bounded search window over the newest artifacts (never an unbounded scan
# of a potentially 100k+ artifact repository history) -- recent, non-expired
# artifacts are the only ones usable for a smoke run anyway.
ARTIFACT_SEARCH_PAGES = 5
ARTIFACT_SEARCH_PER_PAGE = 100

# Bounded poll for the dispatched run's completion -- never an unbounded
# wait (mirrors update_branch.py's own bounded-retry precedent).
RUN_POLL_MAX_ATTEMPTS = 60
RUN_POLL_INTERVAL_SECONDS = 15

# Bounded wait for the dispatched run to first appear in `gh run list`
# (workflow_dispatch does not return a run ID synchronously).
RUN_DISCOVERY_MAX_ATTEMPTS = 12
RUN_DISCOVERY_INTERVAL_SECONDS = 5

# PR #2559 review fix_delta (item C): the pre-dispatch snapshot / discovery
# `gh run list` calls need enough headroom to still find the newly
# dispatched run after excluding every pre-existing (workflow, ref, event)
# run -- bounded (never unbounded), but larger than the previous bare `5`.
RUN_DISCOVERY_LIST_LIMIT = 10


class SkipCondition(Exception):
    """Raised when a precondition cannot be resolved -- caller must SKIP
    (exit 77 / pytest.skip()), never FAIL."""


@dataclass
class SmokeResult:
    status: str  # pass | fail | skip
    reason: str
    run_id: int | None = None
    run_url: str | None = None
    artifact_a_id: int | None = None
    artifact_b_id: int | None = None
    diagnostics: dict = field(default_factory=dict)


def _run_gh(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        input=input_text,
        timeout=60,
    )


def _run_git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise SkipCondition(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_repo() -> str:
    env_repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if env_repo:
        return env_repo
    result = _run_gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if result.returncode != 0 or not result.stdout.strip():
        raise SkipCondition(f"cannot resolve target repository: {result.stderr.strip()}")
    return result.stdout.strip()


def check_actions_read_scope(repo: str) -> None:
    """Issue #2555 AC8 SKIP condition: GH_TOKEN/GITHUB_TOKEN without
    `actions: read` scope. Verified behaviorally (a real artifacts list
    call) rather than by trusting a token-scopes header alone, since a
    fine-grained / GitHub App token does not always advertise scopes the
    same way a classic PAT does."""
    result = _run_gh(["api", f"repos/{repo}/actions/artifacts?per_page=1"])
    if result.returncode != 0:
        raise SkipCondition(
            f"GH_TOKEN/GITHUB_TOKEN lacks actions: read scope (or auth unavailable): {result.stderr.strip()}"
        )


def _fetch_json(args: list[str]) -> Any | None:
    """Returns the parsed JSON body of a `gh` invocation, or `None` on any
    transport/parse failure (callers treat `None` as "this candidate cannot
    be verified usable", never as a fabricated pass)."""
    result = _run_gh(args)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _reliability_artifact_job_succeeded(repo: str, artifact: dict) -> bool:
    """Issue #2555 PR #2559 review fix_delta (item D): a name-prefix match
    plus `expired == false` alone is NOT sufficient evidence the artifact is
    a USABLE close-evidence-publication source -- `.github/workflows/ci.yml`
    uploads the `reliability-assessment` job's own artifact unconditionally
    (`if: !cancelled()`), so a FAILED/ineligible assessment run still
    produces a non-expired, correctly-named artifact. Cross-checks the
    artifact's own `workflow_run.id` -> that run's `reliability-assessment`
    job conclusion == success before accepting it as usable."""
    workflow_run = artifact.get("workflow_run") or {}
    run_id = workflow_run.get("id")
    if not run_id:
        return False
    jobs_payload = _fetch_json(["api", f"repos/{repo}/actions/runs/{run_id}/jobs"])
    if not jobs_payload:
        return False
    jobs = jobs_payload.get("jobs") or []
    target_job = next((job for job in jobs if job.get("name") == RELIABILITY_JOB_NAME), None)
    return target_job is not None and target_job.get("conclusion") == "success"


def resolve_reliability_artifact_id(repo: str) -> tuple[int, str]:
    """Issue #2555 AC8 SKIP condition: no resolvable existing, USABLE
    Reliability close-grade artifact ID. Searches only the newest
    ARTIFACT_SEARCH_PAGES * ARTIFACT_SEARCH_PER_PAGE artifacts (bounded --
    never a full-history scan) for a non-expired
    `ci-reliability-close-grade-result-*` artifact (the exact name the
    `reliability-assessment` job's own upload step uses) whose OWN
    `reliability-assessment` job conclusion is `success` (PR #2559 review
    fix_delta item D -- see `_reliability_artifact_job_succeeded()`). Takes
    the first prefix-matching candidate (in newest-first order) that passes
    this additional check; if none pass within the bounded search window,
    this remains a SKIP (never a fabricated PASS), per the Issue contract."""
    for page in range(1, ARTIFACT_SEARCH_PAGES + 1):
        result = _run_gh(
            [
                "api",
                f"repos/{repo}/actions/artifacts?per_page={ARTIFACT_SEARCH_PER_PAGE}&page={page}",
            ]
        )
        if result.returncode != 0:
            raise SkipCondition(f"artifact list query failed on page={page}: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SkipCondition(f"artifact list response is not valid JSON: {exc}") from exc
        artifacts = payload.get("artifacts") or []
        if not artifacts:
            break
        for artifact in artifacts:
            name = artifact.get("name") or ""
            if name.startswith(RELIABILITY_ARTIFACT_NAME_PREFIX) and artifact.get("expired") is False:
                if _reliability_artifact_job_succeeded(repo, artifact):
                    return int(artifact["id"]), name
    raise SkipCondition(
        "no non-expired existing Reliability close-grade artifact "
        f"('{RELIABILITY_ARTIFACT_NAME_PREFIX}*') whose own reliability-assessment "
        f"job succeeded was found within the newest "
        f"{ARTIFACT_SEARCH_PAGES * ARTIFACT_SEARCH_PER_PAGE} artifacts"
    )


def resolve_dispatch_ref(repo: str) -> tuple[str, str]:
    """Issue #2555 AC8 SKIP condition: the current branch must already be
    pushed to origin with the EXACT commit under test (`workflow_dispatch`
    runs the workflow file AS IT EXISTS AT THE DISPATCHED ref -- an unpushed
    or stale ref cannot exercise this Issue's own new job). Returns
    `(branch, head_sha)` -- PR #2559 review fix_delta (item C) reuses this
    already-verified `head_sha` for `discover_dispatched_run()`'s exact
    binding instead of re-deriving it."""
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        raise SkipCondition("current worktree is in detached HEAD state; cannot resolve a dispatchable ref")
    local_sha = _run_git(["rev-parse", "HEAD"])
    result = _run_gh(["api", f"repos/{repo}/commits/{branch}", "-q", ".sha"])
    if result.returncode != 0 or not result.stdout.strip():
        raise SkipCondition(
            f"branch '{branch}' is not pushed to origin (or unreadable); cannot workflow_dispatch against it: "
            f"{result.stderr.strip()}"
        )
    remote_sha = result.stdout.strip()
    if remote_sha != local_sha:
        raise SkipCondition(
            f"local HEAD ({local_sha}) does not match origin/{branch} ({remote_sha}); "
            "push the current commit before running this smoke test"
        )
    return branch, local_sha


def dispatch_workflow(repo: str, ref: str, artifact_id: int) -> None:
    """PR #2559 review fix_delta (item B): a `gh workflow run` dispatch
    failure is an implementation-path fault (the environment/precondition
    checks above already passed), never a SKIP precondition -- raises
    `RuntimeError` (mapped to `EXIT_FAIL` by `main()`)."""
    result = _run_gh(
        [
            "workflow",
            "run",
            WORKFLOW_FILE,
            "--repo",
            repo,
            "--ref",
            ref,
            "-f",
            f"close_evidence_source_artifact_id={artifact_id}",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh workflow run failed to dispatch: {result.stderr.strip()}")


def _parse_iso8601_utc_to_epoch(value: str) -> float:
    """Parses a GitHub Actions REST/CLI `createdAt` timestamp (ISO 8601,
    `Z`-suffixed UTC, e.g. `2026-09-07T12:30:42Z`) into a POSIX epoch float
    comparable against `time.time()`-based markers."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def snapshot_existing_run_ids(repo: str, ref: str) -> set[int]:
    """PR #2559 review fix_delta (item C): captures the run IDs already
    visible for this exact (workflow, ref, event) tuple BEFORE dispatching,
    so `discover_dispatched_run()` below can exclude them. Fixes a
    misattribution bug: the previous implementation ignored its own
    `dispatched_after` parameter entirely and unconditionally accepted
    `gh run list`'s `runs[0]` (the newest run for this tuple), which
    misattributes a concurrent unrelated run to this smoke test."""
    result = _run_gh(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            WORKFLOW_FILE,
            "--branch",
            ref,
            "--event",
            "workflow_dispatch",
            "--limit",
            str(RUN_DISCOVERY_LIST_LIMIT),
            "--json",
            "databaseId",
        ]
    )
    if result.returncode != 0:
        raise SkipCondition(f"pre-dispatch run snapshot query failed: {result.stderr.strip()}")
    try:
        runs = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError as exc:
        raise SkipCondition(f"pre-dispatch run snapshot response is not valid JSON: {exc}") from exc
    return {int(run["databaseId"]) for run in runs}


def discover_dispatched_run(
    repo: str,
    ref: str,
    dispatched_after: float,
    pre_dispatch_run_ids: set[int],
    head_sha: str,
) -> int:
    """PR #2559 review fix_delta (item C): exact dispatch-run binding. A
    candidate run must (a) NOT already be a member of
    `pre_dispatch_run_ids` (the pre-dispatch snapshot), (b) have
    `headSha == head_sha` (the exact commit under test --
    `resolve_dispatch_ref()` already asserted this equals `origin/<ref>`),
    and (c) `createdAt >= dispatched_after` (with a 1-second tolerance for
    GitHub's whole-second timestamp truncation). Zero candidates in a given
    poll keeps the existing bounded retry going (the dispatched run may not
    have appeared in the list yet); exhausting the bounded window with zero
    candidates, or ever seeing more than one candidate (an unresolvable
    ambiguity -- e.g. a concurrent dispatch on the exact same head), is a
    FAIL (`RuntimeError`) -- never a guessed `runs[0]` (the prior, incorrect
    behavior) and never a SKIP (this is a runtime binding fault, not an
    environment precondition)."""
    for _ in range(RUN_DISCOVERY_MAX_ATTEMPTS):
        result = _run_gh(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                WORKFLOW_FILE,
                "--branch",
                ref,
                "--event",
                "workflow_dispatch",
                "--limit",
                str(RUN_DISCOVERY_LIST_LIMIT),
                "--json",
                "databaseId,createdAt,status,headSha",
            ]
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                runs = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"run list response is not valid JSON during discovery: {exc}") from exc
            candidates: list[int] = []
            for run in runs:
                run_id = int(run["databaseId"])
                if run_id in pre_dispatch_run_ids:
                    continue
                if run.get("headSha") != head_sha:
                    continue
                created_at = run.get("createdAt")
                if not created_at:
                    continue
                try:
                    created_epoch = _parse_iso8601_utc_to_epoch(created_at)
                except ValueError:
                    continue
                if created_epoch < dispatched_after - 1:
                    continue
                candidates.append(run_id)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise RuntimeError(
                    "dispatched run binding is ambiguous: "
                    f"{len(candidates)} candidate runs matched (head_sha={head_sha}, "
                    f"dispatched_after={dispatched_after}): {candidates}"
                )
        time.sleep(RUN_DISCOVERY_INTERVAL_SECONDS)
    raise RuntimeError(
        "dispatched run did not appear (exact pre-dispatch-snapshot + head_sha binding) "
        "within the bounded discovery window"
    )


def poll_run_completion(repo: str, run_id: int) -> dict:
    """PR #2559 review fix_delta (item B): a transient `gh run view`
    failure (or malformed JSON response) during polling is retried within
    the EXISTING bounded `RUN_POLL_MAX_ATTEMPTS` window (never a new
    unbounded retry harness) rather than immediately raised -- but if the
    window is exhausted (whether due to `status` never reaching
    `completed`, or `gh run view` failing on every attempt), this is a FAIL
    (`RuntimeError`, caught and formatted by `main()` as `EXIT_FAIL`), never
    a SKIP and never an unformatted traceback."""
    last_error: str | None = None
    for _ in range(RUN_POLL_MAX_ATTEMPTS):
        result = _run_gh(
            [
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--json",
                "status,conclusion,jobs,url",
            ]
        )
        if result.returncode != 0:
            last_error = f"gh run view failed for run_id={run_id}: {result.stderr.strip()}"
            time.sleep(RUN_POLL_INTERVAL_SECONDS)
            continue
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = f"gh run view response is not valid JSON for run_id={run_id}: {exc}"
            time.sleep(RUN_POLL_INTERVAL_SECONDS)
            continue
        if payload.get("status") == "completed":
            return payload
        last_error = None
        time.sleep(RUN_POLL_INTERVAL_SECONDS)
    if last_error:
        raise RuntimeError(
            f"run_id={run_id} did not reach status=completed within the bounded poll window "
            f"(last error: {last_error})"
        )
    raise RuntimeError(f"run_id={run_id} did not reach status=completed within the bounded poll window")


def list_run_artifacts(repo: str, run_id: int) -> list[dict]:
    result = _run_gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"])
    if result.returncode != 0:
        raise RuntimeError(f"failed to list artifacts for run_id={run_id}: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    return payload.get("artifacts") or []


def run_smoke(*, repo: str | None = None) -> SmokeResult:
    resolved_repo = repo or resolve_repo()
    check_actions_read_scope(resolved_repo)
    artifact_id, artifact_name = resolve_reliability_artifact_id(resolved_repo)
    ref, head_sha = resolve_dispatch_ref(resolved_repo)

    # PR #2559 review fix_delta (item C): snapshot BEFORE dispatch so the
    # newly dispatched run can be told apart from any pre-existing run for
    # this exact (workflow, ref, event) tuple.
    pre_dispatch_run_ids = snapshot_existing_run_ids(resolved_repo, ref)
    dispatched_at = time.time()
    dispatch_workflow(resolved_repo, ref, artifact_id)
    run_id = discover_dispatched_run(resolved_repo, ref, dispatched_at, pre_dispatch_run_ids, head_sha)
    run_payload = poll_run_completion(resolved_repo, run_id)

    jobs = run_payload.get("jobs") or []
    target_job = next((job for job in jobs if job.get("name") == JOB_NAME), None)
    if target_job is None:
        return SmokeResult(
            status="fail",
            reason=f"job '{JOB_NAME}' not found in dispatched run_id={run_id}",
            run_id=run_id,
            run_url=run_payload.get("url"),
            diagnostics={"jobs": [job.get("name") for job in jobs]},
        )
    if target_job.get("conclusion") != "success":
        return SmokeResult(
            status="fail",
            reason=f"job '{JOB_NAME}' conclusion={target_job.get('conclusion')!r} (expected success)",
            run_id=run_id,
            run_url=run_payload.get("url"),
        )

    artifacts = list_run_artifacts(resolved_repo, run_id)
    artifact_a = next((a for a in artifacts if a.get("name") == ARTIFACT_A_NAME), None)
    artifact_b = next((a for a in artifacts if a.get("name") == ARTIFACT_B_NAME), None)
    if artifact_a is None or artifact_b is None:
        return SmokeResult(
            status="fail",
            reason=(
                f"missing artifact(s): A={ARTIFACT_A_NAME} present={artifact_a is not None}, "
                f"B={ARTIFACT_B_NAME} present={artifact_b is not None}"
            ),
            run_id=run_id,
            run_url=run_payload.get("url"),
        )

    return SmokeResult(
        status="pass",
        reason="close-evidence-publication job succeeded with both artifacts present",
        run_id=run_id,
        run_url=run_payload.get("url"),
        artifact_a_id=int(artifact_a["id"]),
        artifact_b_id=int(artifact_b["id"]),
        diagnostics={"source_artifact_id": artifact_id, "source_artifact_name": artifact_name},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="owner/repo (defaults to GH_REPO/GITHUB_REPOSITORY/gh repo view)")
    args = parser.parse_args(argv)

    try:
        result = run_smoke(repo=args.repo)
    except SkipCondition as exc:
        print(f"SKIP: {exc}")
        return EXIT_SKIP
    except RuntimeError as exc:
        # PR #2559 review fix_delta (item B): dispatch_workflow() /
        # discover_dispatched_run() / poll_run_completion() implementation-
        # path faults are FAIL, never SKIP -- and never an unformatted
        # traceback (the previous behavior for poll_run_completion()'s own
        # timeout RuntimeError, which this except clause was missing).
        print(f"FAIL: {exc}")
        return EXIT_FAIL

    if result.status == "pass":
        print(
            "PASS: close_evidence_publication_smoke: "
            f"run_id={result.run_id} run_url={result.run_url} "
            f"artifact_a_id={result.artifact_a_id} artifact_b_id={result.artifact_b_id}"
        )
        return EXIT_PASS

    print(f"FAIL: {result.reason} (run_id={result.run_id} run_url={result.run_url})")
    return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
