"""
tests/test_ensure_contract_snapshot_fingerprint_patch.py

Issue #1562: live/regression coverage confirming that
ensure_contract_snapshot.py's `materialized_go` two-phase POST+PATCH flow
actually persists `expected_contract_fingerprint` into the real GitHub
comment body on the current HEAD.

fact-check (see Issue #1562's go contract "Notes for Reviewer" /
issue-refinement-loop investigation): the *current* HEAD (PR #1548 and
later) already implements the two-phase flow -- POST a provisional
CONTRACT_SNAPSHOT_MATERIALIZATION_PENDING_V1 body, compute the
source-bound fingerprint from the real posted comment id, PATCH the same
comment with the final CONTRACT_REVIEW_RESULT_V1 body embedding the
fingerprint, and independently re-verify via
`verify_controlled_publisher_comment_id_binding` (a direct GET). The
originally reported symptom ("status: ok / source: materialized_go" posted
with NO `expected_contract_fingerprint` in the comment body) matches the
*pre-#1548* producer, which never called `patch_comment()` at all. These
tests are designed to (a) actually reproduce the defect against current
HEAD if it exists (AC1) and (b) permanently lock the currently-correct
two-phase PATCH behavior as a regression guard (AC3), whichever the AC1
outcome turns out to be.

Runtime Verification Applicability (Issue #1562): decision: immediate,
applicable_acs: [AC1]. AC1 requires an actual `ensure_contract_snapshot.py
--mode auto --post` run against a real GitHub Issue, with an independent
re-fetch of the posted comment. Per
docs/dev/runtime-verification-policy.md, an unavailable execution
environment (no `gh` CLI / no write auth / no network) must be treated as
`environment_blocked`, NOT as a silent skip -- these tests therefore call
`pytest.fail(...)` (not `pytest.skip(...)`) when the precondition check
fails, so the failure is loud and forces human attention rather than being
silently indistinguishable from "not run".

fallback_policy (Issue #1562 contract): fallback-derived "success" must
never be reported as PASS. These tests never fall back to a test-double
transport for the parts under test (`post_comment` / `patch_comment` /
the independent re-fetch) -- only `run_contract_review_once` (Out of
Scope: #1562 does not modify issue-contract-review) is replaced with a
canned "go" result, exactly as the pre-existing
`TestFingerprintMaterializeEndToEnd` class in test_ensure_contract_snapshot.py
already does for its own (test-double) coverage.

artifact_requirements (Issue #1562 contract): the actual re-fetched
GitHub comment body/JSON obtained during the test run is asserted against
directly (not merely logged) -- pytest's own failure output on assertion
mismatch is the artifact.

Cleanup: each test creates one disposable GitHub Issue via `gh issue
create` and closes it in a `finally` block (`gh issue close`). The
disposable Issue title is prefixed with `[disposable-live-test]` so it is
trivially identifiable and does not require deletion (GitHub's REST API
has no issue-delete endpoint for non-admin tokens); it is closed
immediately regardless of test outcome.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import ensure_contract_snapshot from worktree path (same pattern as
# test_ensure_contract_snapshot.py)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent / "scripts"
_ECS_PATH = _SCRIPTS_DIR / "ensure_contract_snapshot.py"

spec = importlib.util.spec_from_file_location(
    "ensure_contract_snapshot_fingerprint_patch_under_test", _ECS_PATH
)
assert spec is not None and spec.loader is not None
_ecs_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_ecs_mod)  # type: ignore[union-attr]

ensure_contract_snapshot = _ecs_mod.ensure_contract_snapshot
patch_comment = _ecs_mod.patch_comment
post_comment = _ecs_mod.post_comment
extract_comment_id_from_url = _ecs_mod.extract_comment_id_from_url

_REPO = "squne121/loop-protocol"
_GH_TIMEOUT = 30

_FINGERPRINT_LINE_RE = re.compile(
    r"expected_contract_fingerprint:\s*(\{.*\})\s*$", re.MULTILINE
)

_EXPECTED_FINGERPRINT_KEYS = {
    "issue_number",
    "contract_source_kind",
    "contract_source_id",
    "contract_body_sha256",
    "allowed_paths_normalized_sha256",
    "base_ref",
    "base_sha_at_snapshot",
}


# ---------------------------------------------------------------------------
# Live-environment preflight + disposable Issue helpers
# ---------------------------------------------------------------------------


def _gh_write_access_available() -> tuple[bool, str]:
    """Best-effort preflight: confirm `gh` CLI is present and authenticated.

    A False result here must cause the caller to fail loudly
    (environment_blocked), never to silently pass or skip.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except FileNotFoundError as exc:
        return False, f"gh_cli_not_found: {exc}"
    except subprocess.TimeoutExpired as exc:
        return False, f"gh_auth_status_timeout: {exc}"
    if result.returncode != 0:
        return False, f"gh_not_authenticated: {result.stderr.strip()}"
    return True, ""


def _create_disposable_issue(title: str) -> Optional[int]:
    """Create a throwaway Issue used only as a live comment-posting target,
    with a well-formed ``## Allowed Paths`` section (required by
    ensure_contract_snapshot's pre-POST canonicalization gate). Returns the
    issue number, or None on failure.
    """
    body = (
        "Disposable live-test Issue created by "
        "test_ensure_contract_snapshot_fingerprint_patch.py (Issue #1562 "
        "AC1/AC3 runtime verification). Safe to ignore; closed automatically "
        "by the test's cleanup step regardless of outcome.\n\n"
        "## Allowed Paths\n\n"
        "- README.md\n"
    )
    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", _REPO,
                "--title", title,
                "--body", body,
            ],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    stdout = result.stdout.strip()
    if not stdout:
        return None
    tail = stdout.splitlines()[-1]
    try:
        return int(tail.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None


def _close_disposable_issue(issue_number: Optional[int]) -> None:
    if issue_number is None:
        return
    subprocess.run(
        [
            "gh", "issue", "close", str(issue_number),
            "--repo", _REPO,
            "--comment", "Automated cleanup: closing disposable live-test Issue "
            "(test_ensure_contract_snapshot_fingerprint_patch.py).",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )


def _fetch_comment_body_direct(comment_id: int) -> str:
    """Independent direct GET of a single comment -- NOT the production
    module's own internal readback function -- mirroring AC1's "同一コメン
    トを再取得すると" requirement with a caller-owned verification path."""
    result = subprocess.run(
        ["gh", "api", f"repos/{_REPO}/issues/comments/{comment_id}"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT,
    )
    assert result.returncode == 0, f"independent comment GET failed: {result.stderr}"
    payload = json.loads(result.stdout)
    return str(payload.get("body") or "")


def _extract_fingerprint_from_comment_body(body: str) -> Optional[dict]:
    match = _FINGERPRINT_LINE_RE.search(body)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _make_review_result_for_live_test() -> dict:
    """A minimal, always-"go" run_contract_review_once() stand-in.

    Out of Scope (Issue #1562): issue-contract-review's own readiness /
    blockers / product-spec / vc-preflight logic is not under test here --
    only the ensure_contract_snapshot POST+PATCH transport is. Mocking this
    single upstream call is consistent with the pre-existing
    TestFingerprintMaterializeEndToEnd coverage in
    test_ensure_contract_snapshot.py.
    """
    return {
        "schema": "CONTRACT_REVIEW_ONCE_RESULT_V1",
        "status": "go",
        "readiness_status": "go",
        "checks": {
            "readiness": "go",
            "blockers": "pass",
            "product_spec": "pass",
            "product_spec_check": {
                "schema": "product_spec_check/v1",
                "applicability": "not_applicable",
                "decision": "pass",
                "triggers": {},
                "conditions": {},
                "blocked_reasons": [],
            },
            "vc_preflight": "pass",
        },
        "vc_preflight_classifications": [],
        "errors": [],
    }


# ---------------------------------------------------------------------------
# AC1: live end-to-end run against a real, disposable GitHub Issue
# ---------------------------------------------------------------------------


@pytest.mark.github_live
class TestFingerprintActuallyPosted:
    """AC1 (decision: immediate). Only `run_contract_review_once` is
    mocked; `post_comment`, `patch_comment`,
    `verify_controlled_publisher_comment_id_binding`, and
    `capture_base_ref_and_sha` all execute their real `gh api` subprocess
    calls against a real, disposable GitHub Issue.

    Marked ``github_live`` (Issue #1562 AC4): deselected from the default
    ``pytest`` full-suite run via ``pyproject.toml``'s
    ``addopts = "... -m 'not github_live'"``; run explicitly with
    ``pytest -m github_live`` against an authenticated ``gh`` CLI."""

    def test_fingerprint_actually_posted(self) -> None:
        available, reason = _gh_write_access_available()
        if not available:
            pytest.fail(
                f"environment_blocked: gh CLI write access unavailable "
                f"({reason}). Per docs/dev/runtime-verification-policy.md "
                "this AC1 test must not be silently skipped -- authenticate "
                "`gh` with repo write scope and network access, then re-run."
            )

        run_id = uuid.uuid4().hex[:8]
        issue_number = _create_disposable_issue(
            f"[disposable-live-test] ensure_contract_snapshot AC1 run {run_id}"
        )
        if issue_number is None:
            pytest.fail(
                "environment_blocked: could not create a disposable test "
                "Issue via `gh issue create` (insufficient permission or "
                "network failure)."
            )

        try:
            review_result = _make_review_result_for_live_test()
            with patch.object(
                _ecs_mod,
                "run_contract_review_once",
                return_value=(review_result, None),
            ):
                result = ensure_contract_snapshot(
                    issue_number=issue_number,
                    repo=_REPO,
                    mode="auto",
                    do_post=True,
                )

            assert result["status"] == "ok", (
                f"expected status=ok, got {result['status']!r}; "
                f"errors={result.get('errors')}"
            )
            assert result["source"] == "materialized_go"
            posted_url = result["contract_snapshot_url"]
            assert posted_url, "materialized_go must set contract_snapshot_url (B3)"

            comment_id = extract_comment_id_from_url(posted_url)
            assert comment_id is not None, f"could not parse comment id from {posted_url!r}"

            # Independent re-fetch, exactly mirroring AC1's
            # "投稿された go コメント本文に expected_contract_fingerprint が
            # 実在する" requirement.
            body = _fetch_comment_body_direct(comment_id)

            assert "expected_contract_fingerprint" in body, (
                "the independently re-fetched go comment body does not "
                "contain expected_contract_fingerprint -- this reproduces "
                "the exact defect Issue #1562 reported"
            )

            fingerprint = _extract_fingerprint_from_comment_body(body)
            assert fingerprint is not None, (
                "expected_contract_fingerprint text is present but could not "
                "be parsed as JSON from the posted comment body"
            )
            assert set(fingerprint.keys()) == _EXPECTED_FINGERPRINT_KEYS, (
                f"expected exactly the 7-item fingerprint schema, got keys "
                f"{sorted(fingerprint.keys())}"
            )
            assert fingerprint["contract_source_id"] == str(comment_id)
            assert fingerprint["issue_number"] == issue_number
            assert fingerprint["contract_source_kind"] == "issue_comment"
            assert fingerprint["base_ref"]
            assert fingerprint["base_sha_at_snapshot"]
        finally:
            _close_disposable_issue(issue_number)


# ---------------------------------------------------------------------------
# AC3: regression-lock the real patch_comment() two-phase PATCH + readback
# ---------------------------------------------------------------------------


@pytest.mark.github_live
class TestFingerprintPatchActuallyApplied:
    """AC3: locks the current (correct) `patch_comment()` behavior at the
    function level -- real POST of a provisional body, real PATCH to the
    final fingerprint-bearing body, and an independent re-GET confirming
    the PATCH was actually applied. This is a permanent regression guard
    against a future accidental removal/short-circuit of the PATCH step,
    independent of whatever AC1 observes on the current HEAD.

    Marked ``github_live`` (Issue #1562 AC4): see
    ``TestFingerprintActuallyPosted`` docstring for the deselect/opt-in
    mechanism."""

    def test_fingerprint_patch_actually_applied(self) -> None:
        available, reason = _gh_write_access_available()
        if not available:
            pytest.fail(
                f"environment_blocked: gh CLI write access unavailable "
                f"({reason}). Per docs/dev/runtime-verification-policy.md "
                "this test must not be silently skipped."
            )

        run_id = uuid.uuid4().hex[:8]
        issue_number = _create_disposable_issue(
            f"[disposable-live-test] ensure_contract_snapshot AC3 run {run_id}"
        )
        if issue_number is None:
            pytest.fail(
                "environment_blocked: could not create a disposable test "
                "Issue via `gh issue create` (insufficient permission or "
                "network failure)."
            )

        try:
            provisional_sha = "sha256:" + "0" * 64
            provisional_body = (
                f"<!-- loop-protocol:contract-snapshot issue={issue_number} "
                f"body_sha256={provisional_sha} schema=CONTRACT_REVIEW_RESULT_V1 -->\n\n"
                "```yaml\n"
                "CONTRACT_SNAPSHOT_MATERIALIZATION_PENDING_V1:\n"
                f"  issue_number: {issue_number}\n"
                f"  body_sha256: \"{provisional_sha}\"\n"
                "  phase: awaiting_comment_id_binding\n"
                "```\n"
            )

            url, post_status, http_status = post_comment(
                issue_number, _REPO, provisional_body
            )
            assert post_status == _ecs_mod.POST_STATUS_POSTED, (
                f"provisional POST failed: post_status={post_status!r} "
                f"http_status={http_status!r}"
            )
            comment_id = extract_comment_id_from_url(url)
            assert comment_id is not None

            # Confirm the provisional body really landed before PATCHing.
            provisional_readback = _fetch_comment_body_direct(comment_id)
            assert "expected_contract_fingerprint" not in provisional_readback

            fingerprint = {
                "issue_number": issue_number,
                "contract_source_kind": "issue_comment",
                "contract_source_id": str(comment_id),
                "contract_body_sha256": "sha256:" + "1" * 64,
                "allowed_paths_normalized_sha256": "2" * 64,
                "base_ref": "main",
                "base_sha_at_snapshot": "3" * 40,
            }
            final_sha = "sha256:" + "1" * 64
            fingerprint_json = json.dumps(fingerprint, separators=(",", ":"))
            final_body = (
                f"<!-- loop-protocol:contract-snapshot issue={issue_number} "
                f"body_sha256={final_sha} schema=CONTRACT_REVIEW_RESULT_V1 -->\n\n"
                "## Contract Review Result\n\n"
                "```yaml\n"
                "CONTRACT_REVIEW_RESULT_V1:\n"
                "  status: go\n"
                f"  issue_url: https://github.com/{_REPO}/issues/{issue_number}\n"
                f"  body_sha256: \"{final_sha}\"\n"
                "  source: ensure_contract_snapshot_auto\n"
                f"  expected_contract_fingerprint: {fingerprint_json}\n"
                "```\n"
            )

            patch_ok, patch_err = patch_comment(
                issue_number, _REPO, comment_id, final_body
            )
            assert patch_ok is True, f"patch_comment() reported failure: {patch_err!r}"

            actual_body = _fetch_comment_body_direct(comment_id)
            assert actual_body == final_body, (
                "independent GET after patch_comment() must return exactly "
                "the PATCHed body -- this is the two-phase PATCH regression "
                "this test locks (AC3)"
            )

            refetched_fingerprint = _extract_fingerprint_from_comment_body(actual_body)
            assert refetched_fingerprint == fingerprint
        finally:
            _close_disposable_issue(issue_number)
