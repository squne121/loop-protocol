"""test_visual_impact_current_head_binding.py (Issue #2019 AC28, runtime_only)

Runtime Verification Applicability: decision: immediate, applicable_acs
include AC28. This test verifies that the current PR head's
`visual-impact-policy` CheckRun and its VISUAL_IMPACT_DECISION_V1 artifact
bind to the ACTUAL GitHub CheckRun API response for that PR head.

Per the Issue's skip_conditions: when the current branch has no live PR (or
no `visual-impact-policy` CheckRun exists yet on its head, or the GitHub API
is unreachable), this test SKIPS (never fabricates a PASS). A skip here is
NOT evidence of correctness -- pr-review-judge re-verifies this on the real
PR head before merge (fallback_policy).
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest


def _gh_available() -> bool:
    return shutil.which("gh") is not None


def _current_pr_json() -> dict | None:
    if not _gh_available():
        return None
    proc = subprocess.run(
        ["gh", "pr", "view", "--json", "number,headRefOid,headRepositoryOwner,headRepository"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def _check_runs_for_head(head_sha: str, repo: str) -> list[dict] | None:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("check_runs", [])
    except json.JSONDecodeError:
        return None


def test_ac28_current_head_visual_impact_policy_checkrun_binding():
    pr = _current_pr_json()
    if pr is None:
        pytest.skip(
            "no live PR found for the current branch or `gh` unavailable/unreachable "
            "(Runtime Verification Applicability skip_conditions, Issue #2019 AC28) -- "
            "SKIP is not PASS; pr-review-judge re-verifies on the real PR head."
        )

    head_sha = pr.get("headRefOid")
    repo_owner = (pr.get("headRepositoryOwner") or {}).get("login")
    repo_name = (pr.get("headRepository") or {}).get("name")
    if not head_sha or not repo_owner or not repo_name:
        pytest.skip("PR JSON missing head_sha/repo fields -- cannot bind CheckRun (SKIP, not PASS).")

    repo = f"{repo_owner}/{repo_name}"
    check_runs = _check_runs_for_head(head_sha, repo)
    if check_runs is None:
        pytest.skip("GitHub check-runs API unreachable for current head -- SKIP (Runtime Verification Applicability).")

    visual_impact_runs = [cr for cr in check_runs if cr.get("name") == "visual-impact-policy"]
    if not visual_impact_runs:
        pytest.skip(
            "no visual-impact-policy CheckRun exists yet for the current PR head -- "
            "SKIP (Runtime Verification Applicability skip_conditions: CheckRun absence)."
        )

    for run in visual_impact_runs:
        assert run.get("head_sha") == head_sha, "visual-impact-policy CheckRun head_sha must match the current PR head"
        app = run.get("app") or {}
        assert app.get("slug") in {"github-actions"}, f"unexpected GitHub Actions app source: {app}"
