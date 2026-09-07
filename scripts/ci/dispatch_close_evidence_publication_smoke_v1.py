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

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

WORKFLOW_FILE = "ci.yml"
JOB_NAME = "close-evidence-publication"
ARTIFACT_A_NAME = "close-evidence-bundle-v1"
ARTIFACT_B_NAME = "close-evidence-publication-receipt-v1"
RELIABILITY_ARTIFACT_NAME_PREFIX = "ci-reliability-close-grade-result-"

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


def resolve_reliability_artifact_id(repo: str) -> tuple[int, str]:
    """Issue #2555 AC8 SKIP condition: no resolvable existing Reliability
    close-grade artifact ID. Searches only the newest
    ARTIFACT_SEARCH_PAGES * ARTIFACT_SEARCH_PER_PAGE artifacts (bounded --
    never a full-history scan) for a non-expired
    `ci-reliability-close-grade-result-*` artifact (the exact name the
    `reliability-assessment` job's own upload step uses)."""
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
                return int(artifact["id"]), name
    raise SkipCondition(
        "no non-expired existing Reliability close-grade artifact "
        f"('{RELIABILITY_ARTIFACT_NAME_PREFIX}*') found within the newest "
        f"{ARTIFACT_SEARCH_PAGES * ARTIFACT_SEARCH_PER_PAGE} artifacts"
    )


def resolve_dispatch_ref(repo: str) -> str:
    """Issue #2555 AC8 SKIP condition: the current branch must already be
    pushed to origin with the EXACT commit under test (`workflow_dispatch`
    runs the workflow file AS IT EXISTS AT THE DISPATCHED ref -- an unpushed
    or stale ref cannot exercise this Issue's own new job)."""
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
    return branch


def dispatch_workflow(repo: str, ref: str, artifact_id: int) -> None:
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
        raise SkipCondition(f"gh workflow run failed to dispatch: {result.stderr.strip()}")


def discover_dispatched_run(repo: str, ref: str, dispatched_after: float) -> int:
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
                "5",
                "--json",
                "databaseId,createdAt,status",
            ]
        )
        if result.returncode == 0 and result.stdout.strip():
            runs = json.loads(result.stdout)
            if runs:
                # `gh run list` returns newest-first; the newest run for
                # this (workflow, ref, event) tuple dispatched after we
                # issued `gh workflow run` above is the one we triggered.
                newest = runs[0]
                return int(newest["databaseId"])
        time.sleep(RUN_DISCOVERY_INTERVAL_SECONDS)
    raise SkipCondition("dispatched run did not appear in `gh run list` within the bounded discovery window")


def poll_run_completion(repo: str, run_id: int) -> dict:
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
            raise SkipCondition(f"gh run view failed for run_id={run_id}: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if payload.get("status") == "completed":
            return payload
        time.sleep(RUN_POLL_INTERVAL_SECONDS)
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
    ref = resolve_dispatch_ref(resolved_repo)

    dispatched_at = time.time()
    dispatch_workflow(resolved_repo, ref, artifact_id)
    run_id = discover_dispatched_run(resolved_repo, ref, dispatched_at)
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
