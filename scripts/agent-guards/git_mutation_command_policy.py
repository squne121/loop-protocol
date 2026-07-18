#!/usr/bin/env python3
"""Shared bounded policy for issue-worktree `rtk git` mutations.

This module is intentionally narrow: it recognizes only the exact command
shapes that Issue #1241 wants to recover (`rtk git add/commit/push`) and keeps
the rest fail-closed.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


ALLOWED_RTK_GIT_SUBCOMMANDS = frozenset({"add", "commit", "push", "merge"})
DENIED_PUSH_FLAGS = frozenset({"--force", "-f", "--tags", "--all", "--mirror", "--delete"})
# Issue #1408 iteration-2 adversarial review (P1): `github_branch_api` /
# `fetch_then_show_ref` never actually re-read the remote — they were
# self-reported labels that trusted `LOOP_PUBLISH_CURRENT_REMOTE_HEAD`
# verbatim. Only `ls_remote` performs a live `git ls-remote` readback, so it
# is the sole authorized source until a verified implementation for the
# other sources exists (tracked separately, not in this PR's scope).
ALLOWED_REMOTE_READBACK_SOURCES = frozenset({"ls_remote"})
COMMAND_CLASS_RTK_GIT_ADD = "rtk_git_add"
COMMAND_CLASS_RTK_GIT_COMMIT = "rtk_git_commit"
COMMAND_CLASS_RTK_GIT_PUSH = "rtk_git_push"
# Issue #1449: the initial_branch_create lane is a separate decision path from
# the existing_branch_update lane (COMMAND_CLASS_RTK_GIT_PUSH above) — new
# branch initial publish (remote ref absent) uses a fully-qualified
# empty-expect `--force-with-lease` compare-and-create primitive instead of
# the plain `HEAD:refs/heads/<branch>` refspec the existing lane expects.
COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE = "rtk_git_initial_branch_create"
COMMAND_CLASS_RTK_GIT_UNKNOWN = "rtk_git_unknown"
# Issue #1589: verified fast-forward merge lane -- exact `rtk git merge
# --ff-only <40-hex-sha>` command class. Mirrors the
# execute_initial_branch_create_transaction pattern (Issue #1449): the
# transaction performs verify -> live-remote-probe -> `git merge --ff-only`
# -> postcondition-readback inside ONE trusted execution boundary, and
# `classify_rtk_git_mutation` always returns "deny" for the raw command
# afterward (the transaction outcome is carried in `reason_code`).
COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY = "rtk_git_merge_ff_only"
ALLOWED_ALLOWED_PATHS_GATE_STATUSES = frozenset({"ok", "fail_closed", "indeterminate"})
# Issue #1408 iteration-2 (P2): canonical push destination identity. New
# branch initial publish (remote ref absent) is explicitly out of scope for
# this bridge — see Issue #1449.
CANONICAL_REPO_IDENTITY_DEFAULT = "squne121/loop-protocol"
# Issue #1408 iteration-2 (P2): policy-level default branch destination
# guard, independent of #360's hook-level destination guard.
DEFAULT_BRANCH_NAMES = frozenset({"main", "master", "trunk"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Issue #1589: the verified-ff-merge lane restricts the active branch to the
# canonical linked-issue-worktree naming shape (docs/dev/workflow.md#Worktree
# 配置規約) so a drifted root checkout on a non-default, non-issue branch
# cannot be treated as an eligible merge target -- independent of, and in
# addition to, the DEFAULT_BRANCH_NAMES check below.
_ISSUE_WORKTREE_BRANCH_RE = re.compile(r"^worktree-issue-(\d+)-[a-z0-9][a-z0-9-]{0,63}$")
_CANONICAL_REPO_URL_TEMPLATE = r"^(?:https://github\.com/|git@github\.com:){identity}(?:\.git)?/?$"

# Issue #1449: remote branch state — the exclusive 3-state classification
# used by the initial_branch_create lane. `present`/`absent` are derived ONLY
# from a live `git ls-remote --refs --exit-code` performed in the same
# execution cycle; any other non-zero/timeout/malformed-output outcome is
# `probe_error` (never folded into `absent`) and must fail-closed.
REMOTE_STATE_PRESENT = "present"
REMOTE_STATE_ABSENT = "absent"
REMOTE_STATE_PROBE_ERROR = "probe_error"
REMOTE_BRANCH_STATES = frozenset({REMOTE_STATE_PRESENT, REMOTE_STATE_ABSENT, REMOTE_STATE_PROBE_ERROR})

# Issue #1449 (PR #1479 OWNER review, P2 High): `probe_error` is not a single
# undifferentiated failure mode. `error_category` discriminates *why* the
# live probe/readback could not confirm a state, so callers can tell a
# timeout apart from a transport failure apart from malformed output — this
# is the discriminated-union information the OWNER review asked for, without
# forcing every existing caller to migrate to a nested object shape.
PROBE_ERROR_CATEGORY_TIMEOUT = "timeout"
PROBE_ERROR_CATEGORY_TRANSPORT_ERROR = "transport_error"
PROBE_ERROR_CATEGORY_UNEXPECTED_RETURNCODE = "unexpected_returncode"
PROBE_ERROR_CATEGORY_MALFORMED_OUTPUT = "malformed_output"

# Issue #1449 (PR #1479 OWNER review, P1 Blocker 1): the exclusive outcome
# vocabulary for `execute_initial_branch_create_transaction` — every push
# attempt (success, race-lost, timeout, transport failure) is classified
# into exactly one of these, never silently treated as success.
INITIAL_BRANCH_CREATE_STATUS_CREATED_VERIFIED = "created_and_verified"
INITIAL_BRANCH_CREATE_STATUS_REJECTED_CONFLICT = "rejected_conflict"
INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_CREATED = "transport_error_but_created_and_verified"
INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_ABSENT = "transport_error_remote_absent"
INITIAL_BRANCH_CREATE_STATUS_READBACK_MISMATCH = "readback_mismatch"
INITIAL_BRANCH_CREATE_STATUS_READBACK_UNAVAILABLE = "readback_unavailable"
INITIAL_BRANCH_CREATE_STATUS_DENIED = "denied"


@dataclass(frozen=True)
class GitMutationPolicyResult:
    status: str
    command_class: str
    reason_code: str
    suggested_command: str | None = None
    verification_command: str | None = None
    expected_remote_head: str | None = None
    current_remote_head: str | None = None
    local_head: str | None = None
    verified_head: str | None = None
    declared_publish_head: str | None = None
    allowed_paths_gate_status: str | None = None
    target_branch: str | None = None
    pr_number: str | None = None
    remote_readback_source: str | None = None
    decision_inputs_complete: bool | None = None
    required_decisions: tuple[str, ...] = ()
    boundary_layer: str | None = None
    remote_state: str | None = None
    # Issue #1449 (PR #1479 OWNER review, P2 High): populated only when
    # `remote_state == probe_error` — discriminates WHY the live probe or
    # readback could not confirm a state (`timeout` / `transport_error` /
    # `unexpected_returncode` / `malformed_output`), never folded back into
    # a bare `probe_error` string.
    remote_state_error_category: str | None = None
    # Issue #1609 fix_delta: populated only for the merge-ff-only command
    # class -- the shape-validated (lowercased) 40-hex target SHA, carried
    # from the PURE classifier to the caller that performs authorization
    # and executes the actual transaction (never executed as a classify
    # side effect any more).
    target_sha: str | None = None


@dataclass(frozen=True)
class PublishGuardContext:
    expected_remote_head: str
    current_remote_head: str
    declared_publish_head: str
    verified_head: str
    allowed_paths_gate_status: str
    remote_readback_source: str
    decision_inputs_complete: bool
    allowed_paths_gate_issue_number: str
    allowed_paths_gate_base_sha: str
    allowed_paths_gate_head_sha: str


@dataclass(frozen=True)
class InitialBranchCreateGuardContext:
    """Issue #1449 (PR #1479 OWNER review, P2 High): a SEPARATE, narrower
    guard-context shape for the initial_branch_create lane. Unlike
    `PublishGuardContext` (existing_branch_update lane), this does NOT
    require `expected_remote_head` / `current_remote_head` as mandatory
    40-char SHAs — for a brand-new branch the remote ref does not exist yet,
    so there is no real remote head to declare, and the previous design
    forced callers to fabricate a SHA (typically the local head) into those
    fields purely to satisfy schema validation, which was audit-inaccurate.
    Remote state for this lane is instead derived exclusively from the live
    `classify_remote_branch_state` probe performed in the same execution
    cycle (see `execute_initial_branch_create_transaction`)."""

    declared_publish_head: str
    verified_head: str
    allowed_paths_gate_status: str
    remote_readback_source: str
    decision_inputs_complete: bool
    allowed_paths_gate_issue_number: str
    allowed_paths_gate_head_sha: str


@dataclass(frozen=True)
class PublishLaneDecision:
    status: str
    publish_failure_reason: dict[str, str]
    issue_number: int | None
    pr_number: str | None
    branch: str
    remote: str
    expected_remote_head: str
    current_remote_head: str
    local_head: str
    verified_head: str
    declared_publish_head: str
    allowed_paths_gate_status: str
    remote_readback_source: str
    decision_inputs_complete: bool
    allowed_command: str | None
    postcondition: str
    required_human_decision: list[str]


def evaluate_publish_lane(
    *,
    remote: str,
    active_branch: str,
    target_branch: str,
    expected_remote_head: str,
    current_remote_head: str,
    local_head: str,
    verified_head: str,
    declared_publish_head: str,
    allowed_paths_gate_status: str,
    remote_readback_source: str,
    decision_inputs_complete: bool,
    remote_drift_reason: str | None = None,
    boundary_layer: str,
    issue_number: int | None = None,
    pr_number: str | None = None,
) -> PublishLaneDecision:
    """Return the bounded publish-lane decision for a failed branch publish."""

    reason_code = ""
    if remote != "origin":
        reason_code = "branch_mismatch"
    elif active_branch != target_branch:
        reason_code = "branch_mismatch"
    elif not decision_inputs_complete:
        reason_code = "publish_guard_context_invalid"
    elif remote_readback_source not in ALLOWED_REMOTE_READBACK_SOURCES:
        reason_code = "publish_guard_context_invalid"
    elif allowed_paths_gate_status != "ok":
        reason_code = "allowed_paths_gate_not_ok"
    elif expected_remote_head != current_remote_head:
        reason_code = (
            remote_drift_reason or "remote_head_scope_contamination"
            if current_remote_head not in {expected_remote_head, local_head}
            else "stale_remote_head"
        )
    elif local_head != declared_publish_head:
        reason_code = "local_head_mismatch"
    elif local_head != verified_head:
        reason_code = "local_head_mismatch"

    publish_failure_reason = {
        "boundary_layer": boundary_layer,
        "reason_code": reason_code or "remote_write_requires_approval",
    }
    allowed_command = "rtk git " + f"push origin HEAD:refs/heads/{target_branch}"
    if reason_code:
        return PublishLaneDecision(
            status="safety_stop",
            publish_failure_reason=publish_failure_reason,
            issue_number=issue_number,
            pr_number=pr_number,
            branch=target_branch,
            remote=remote,
            expected_remote_head=expected_remote_head,
            current_remote_head=current_remote_head,
            local_head=local_head,
            verified_head=verified_head,
            declared_publish_head=declared_publish_head,
            allowed_paths_gate_status=allowed_paths_gate_status,
            remote_readback_source=remote_readback_source,
            decision_inputs_complete=decision_inputs_complete,
            allowed_command=None,
            postcondition="remote branch head == local_head",
            required_human_decision=[
                "PR branch を linked issue 専用 head へ戻す",
                "混入 commit を別 PR / 別 branch へ退避する",
            ],
        )
    return PublishLaneDecision(
        status="allow_retry",
        publish_failure_reason=publish_failure_reason,
        issue_number=issue_number,
        pr_number=pr_number,
        branch=target_branch,
        remote=remote,
        expected_remote_head=expected_remote_head,
        current_remote_head=current_remote_head,
        local_head=local_head,
        verified_head=verified_head,
        declared_publish_head=declared_publish_head,
        allowed_paths_gate_status=allowed_paths_gate_status,
        remote_readback_source=remote_readback_source,
        decision_inputs_complete=decision_inputs_complete,
        allowed_command=allowed_command,
        postcondition="remote branch head == local_head",
        required_human_decision=[],
    )


def _tokenize(command: str) -> list[str] | None:
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None


def _current_branch(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = result.stdout.strip()
    return branch or None


def _current_head(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = result.stdout.strip()
    return head or None


def _git_toplevel(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    root = result.stdout.strip()
    return root or None


def _remote_tracking_head(cwd: str, remote: str, branch: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--hash", "--", f"refs/remotes/{remote}/{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    oid = result.stdout.strip()
    return oid or None


def _ls_remote_head(cwd: str, remote: str, branch: str) -> tuple[str | None, bool]:
    """Return `(oid, remote_absent)`.

    `remote_absent=True` only when `git ls-remote --exit-code` confirms the
    ref does not exist on the remote (returncode 2). Any other non-zero
    returncode (network error, auth failure, etc.) is treated as an
    indeterminate readback failure, not an absence signal (Issue #1408
    iteration-2, P1: new-branch initial publish is out of scope — #1449 —
    so an absent remote ref must be denied explicitly, not folded into the
    generic `publish_guard_context_invalid` catch-all).
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--refs", "--exit-code", remote, f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, False
    if result.returncode == 2:
        return None, True
    if result.returncode != 0:
        return None, False
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    oid = first.split()[0] if first else ""
    if _SHA_RE.fullmatch(oid.lower()):
        return oid.lower(), False
    return None, False


def classify_remote_branch_state(
    cwd: str, remote: str, branch: str, timeout: int = 10
) -> tuple[str, str | None, str | None]:
    """Return `(state, oid, error_category)` for `branch` on `remote`,
    classified into the exclusive 3-state vocabulary (Issue #1449 AC1):

      - `present`: the ref exists on the remote; `oid` is its live SHA.
      - `absent`: `git ls-remote --refs --exit-code` confirmed (returncode 2)
        the ref does not exist on the remote.
      - `probe_error`: timeout, auth failure, network failure, malformed
        output, or any other non-{0,2} returncode — never folded into
        `absent`, always fail-closed at the call site. `error_category`
        discriminates why (Issue #1449 PR #1479 review, P2 High): `timeout`,
        `transport_error`, `unexpected_returncode`, or `malformed_output`.

    `remote` may be a remote name (e.g. `origin`) OR a resolved push URL —
    `git ls-remote` accepts either. Callers that need the probe and the
    subsequent push/readback to hit the SAME repository identity (Issue
    #1449 PR #1479 review, P1 Blocker 2) MUST pass the same resolved URL to
    all three calls.

    `state`/`oid` are derived ONLY from a live `git ls-remote` performed in
    this call (same execution cycle) — never from a cached/self-reported
    value."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--refs", "--exit-code", remote, f"refs/heads/{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return REMOTE_STATE_PROBE_ERROR, None, PROBE_ERROR_CATEGORY_TIMEOUT
    except OSError:
        return REMOTE_STATE_PROBE_ERROR, None, PROBE_ERROR_CATEGORY_TRANSPORT_ERROR
    if result.returncode == 2:
        return REMOTE_STATE_ABSENT, None, None
    if result.returncode != 0:
        return REMOTE_STATE_PROBE_ERROR, None, PROBE_ERROR_CATEGORY_UNEXPECTED_RETURNCODE
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    oid = first.split()[0] if first else ""
    if _SHA_RE.fullmatch(oid.lower()):
        return REMOTE_STATE_PRESENT, oid.lower(), None
    # Non-empty, non-SHA-shaped stdout on returncode 0 is malformed output —
    # fail-closed, not `present` and not `absent`.
    return REMOTE_STATE_PROBE_ERROR, None, PROBE_ERROR_CATEGORY_MALFORMED_OUTPUT


def build_initial_branch_create_argv(
    remote: str, target_branch: str, verified_sha: str | None = None
) -> list[str]:
    """Return the fully-qualified empty-expect `--force-with-lease` argv for
    the initial_branch_create lane (Issue #1449 AC2):

      git push --force-with-lease=refs/heads/<branch>: <remote> <src>:refs/heads/<branch>

    A single-token `--force-with-lease=refs/heads/<branch>:` with nothing
    after the trailing colon is the git-native "the ref MUST NOT already
    exist" empty-expect form — this is the sole primitive this lane executes.

    Issue #1449 (PR #1479 OWNER review, P1 Blocker 3): when `verified_sha` is
    given, the refspec source is the verified 40-char commit SHA itself
    (`<sha>:refs/heads/<branch>`), NOT the literal `HEAD` token — `HEAD` is
    resolved at push-execution time, so a bare `HEAD:` refspec can publish a
    commit that was never the one verified against the Allowed Paths gate /
    publish-guard context if another process moves `HEAD` between
    verification and push. `verified_sha=None` is kept only for the argv
    *shape* assertions (AC2) that predate this fix; production callers
    (`execute_initial_branch_create_transaction`) always pass it.

    Returned as an argv list (never a shell string) so callers execute it via
    `subprocess.run(argv, shell=False)`, never shell-string concatenation."""
    source = verified_sha if verified_sha is not None else "HEAD"
    return [
        "git",
        "push",
        f"--force-with-lease=refs/heads/{target_branch}:",
        remote,
        f"{source}:refs/heads/{target_branch}",
    ]


def validate_initial_branch_create_argv(
    args: list[str], target_branch: str, remote: str = "origin"
) -> tuple[bool, str]:
    """Return `(is_valid, reason_code)` for a candidate initial_branch_create
    lane push argv (Issue #1449 AC9). `args` is the tail of a `git push`
    argv (i.e. excludes the leading `git push` tokens themselves).

    Validates the EXACT fully-qualified empty-expect lease shape produced by
    `build_initial_branch_create_argv` — any deviation (bare `--force` / `-f`,
    `+refspec`, an argument-less `--force-with-lease`, a lease with only
    `<refname>` (no trailing colon), a non-empty expected value, a lease ref
    that differs from the push target branch, multiple lease flags, multiple
    refspecs, `--tags`/`--all`/`--mirror`/`--delete`, a branch-deletion
    refspec, or a default-branch target) is rejected — never falls back to a
    looser match. The refspec source may be either the literal `HEAD` token
    (legacy shape, pre-Blocker-3 fix) or a 40-char verified SHA (the shape
    `execute_initial_branch_create_transaction` now emits)."""
    if target_branch in DEFAULT_BRANCH_NAMES:
        return False, "push_target_is_default_branch"
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch):
        return False, "invalid_target_branch"
    expected_head_shaped = build_initial_branch_create_argv(remote, target_branch)[2:]
    if args == expected_head_shaped:
        return True, "initial_branch_create_argv_valid"
    if len(args) == 3:
        lease_flag, arg_remote, refspec = args
        suffix = f":refs/heads/{target_branch}"
        if (
            lease_flag == f"--force-with-lease=refs/heads/{target_branch}:"
            and arg_remote == remote
            and refspec.endswith(suffix)
        ):
            source = refspec[: -len(suffix)]
            if _SHA_RE.fullmatch(source):
                return True, "initial_branch_create_argv_valid"
    return False, "initial_branch_create_argv_invalid"


def execute_initial_branch_create_push(
    cwd: str, remote: str, target_branch: str, verified_sha: str | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Execute the initial_branch_create lease push. Always invoked with an
    argv list (never a shell string) and `shell=False` (the `subprocess.run`
    default) — Issue #1449 AC2/AC12: the remote-write execution must never be
    assembled via shell-string concatenation. `verified_sha`, when given, is
    embedded directly into the refspec source (Blocker 3 fix); omitted only
    for pre-existing tests that assert the legacy `HEAD:` shape."""
    argv = build_initial_branch_create_argv(remote, target_branch, verified_sha=verified_sha)
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def verify_initial_branch_create_readback(
    cwd: str, remote: str, target_branch: str, local_head: str, timeout: int = 10
) -> tuple[bool, str, str | None]:
    """Post-push readback (Issue #1449 AC7/AC8): re-read the same remote ref
    via a fresh live `git ls-remote` and confirm it now matches `local_head`.
    Returns `(matched, reason_code, remote_oid)`. Any non-`present` state, or
    a `present` state whose oid differs from `local_head`, is a structured
    safety stop (`matched=False`) — never treated as success. `remote`
    SHOULD be the same resolved push URL used for the probe/push (Issue
    #1449 PR #1479 review, P1 Blocker 2) — never independently re-derived."""
    state, oid, _error_category = classify_remote_branch_state(cwd, remote, target_branch, timeout=timeout)
    if state == REMOTE_STATE_PROBE_ERROR:
        return False, "readback_failed_after_push", None
    if state == REMOTE_STATE_ABSENT:
        return False, "readback_failed_after_push", None
    if oid != local_head:
        return False, "readback_mismatch_local_head", oid
    return True, "readback_matches_local_head", oid


def evaluate_initial_branch_create_lane(
    *,
    remote_state: str,
    local_head: str,
    declared_publish_head: str,
    verified_head: str,
    allowed_paths_gate_status: str,
    decision_inputs_complete: bool,
    remote_readback_source: str,
) -> tuple[str, str]:
    """Pure decision core for the initial_branch_create lane (Issue #1449
    AC1/AC5/AC6): returns `(status, reason_code)` where `status` is one of
    `allow` / `route_existing_update` / `deny`. Does not perform any I/O —
    `remote_state` must already be the result of a live
    `classify_remote_branch_state` call in the same execution cycle."""
    if not decision_inputs_complete or remote_readback_source not in ALLOWED_REMOTE_READBACK_SOURCES:
        return "deny", "publish_guard_context_invalid"
    if allowed_paths_gate_status != "ok":
        return "deny", "allowed_paths_gate_not_ok"
    if local_head != declared_publish_head or local_head != verified_head:
        return "deny", "local_head_mismatch"
    if remote_state == REMOTE_STATE_PROBE_ERROR:
        return "deny", "probe_error_fail_closed"
    if remote_state == REMOTE_STATE_PRESENT:
        return "route_existing_update", "remote_branch_present_route_existing_update"
    if remote_state == REMOTE_STATE_ABSENT:
        return "allow", "initial_branch_create_allowed"
    return "deny", "unknown_remote_state"  # pragma: no cover - defensive fail-closed


def validate_branch_name_via_git(cwd: str, branch: str, timeout: int = 10) -> tuple[bool, str]:
    """Validate `branch` against Git's own ref-name grammar (Issue #1449 PR
    #1479 review, P2) via `git check-ref-format --branch <branch>`, instead
    of the hand-rolled `[A-Za-z0-9._/-]+` regex, which is looser than Git's
    real rules and incorrectly accepts names Git itself rejects (a leading
    `.`, `.lock` suffix, `..` component, `//`, a trailing `/` or `.`, or a
    leading `-`). Returns `(is_valid, reason_code)`; any subprocess failure
    (missing git, timeout) fails closed as invalid."""
    if not branch:
        return False, "invalid_target_branch"
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "branch_name_validation_unavailable"
    if result.returncode != 0:
        return False, "invalid_target_branch"
    return True, "valid_target_branch"


def resolve_single_push_url(cwd: str, remote: str = "origin") -> tuple[str | None, str]:
    """Resolve exactly one push URL configured for `remote` (Issue #1449 PR
    #1479 review, P1 Blocker 2). Returns `(url, reason_code)`.

    A named remote's push URL(s) (`git remote get-url --push --all`) can
    differ from its plain URL — `[remote "origin"] url=A pushurl=B` is valid
    Git config, and a remote may have MULTIPLE configured push URLs. If the
    probe (pre-push read), the push itself, and the readback (post-push
    read) do not all target the exact same single resolved URL, the
    "same-remote-ref" contract this lane depends on does not hold. Fails
    closed (returns `(None, reason_code)`) when zero or more than one push
    URL is configured — never guesses or falls back to the plain `url`."""
    urls = _origin_push_urls(cwd, remote=remote)
    if not urls:
        return None, "push_url_unresolved"
    if len(urls) > 1:
        return None, "push_url_ambiguous_multiple_configured"
    return urls[0], "push_url_resolved"


@dataclass(frozen=True)
class InitialBranchCreateTransactionResult:
    """Outcome of `execute_initial_branch_create_transaction` (Issue #1449 PR
    #1479 review, P1 Blocker 1/2/3, P1 High). `status` is either
    `INITIAL_BRANCH_CREATE_STATUS_DENIED` (no push was attempted — a guard
    failed before the push) or one of the six push-outcome categories the
    OWNER review required (`created_and_verified`, `rejected_conflict`,
    `transport_error_but_created_and_verified`, `transport_error_remote_absent`,
    `readback_mismatch`, `readback_unavailable`). `push_url` is intentionally
    NOT logged/serialized anywhere outside this in-process dataclass (never
    written to structured result output — Blocker 2 constraint: don't emit
    the raw URL)."""

    status: str
    reason_code: str
    remote_state: str | None
    remote_oid: str | None
    push_returncode: int | None
    push_error_category: str | None


def execute_initial_branch_create_transaction(
    cwd: str,
    target_branch: str,
    expected_head: str,
    remote: str = "origin",
    timeout: int = 30,
) -> InitialBranchCreateTransactionResult:
    """Single atomic authorize -> probe -> push -> readback boundary for the
    initial_branch_create lane (Issue #1449 PR #1479 OWNER review on
    PR #1479, P1 Blocker 1/2/3 and P1 High).

    This function is the ONE place that performs the live remote write for
    this lane — `git_mutation_command_policy.py`'s CLI entrypoint calls this
    directly (never leaves the actual push to a separately-executed shell
    command the caller runs afterward), so authorization / push / readback
    happen inside a single trusted execution boundary instead of being
    split across a policy-classification step and an unrelated later shell
    invocation.

    Sequence (each step fails closed):
      1. Validate `target_branch` via real Git ref-name grammar.
      2. Resolve exactly one push URL for `remote` (fail-closed on 0 or >1).
      3. Confirm local HEAD still equals `expected_head` (head-binding check
         — narrows, but cannot eliminate, the verify-to-push race window).
      4. Probe remote state via the resolved push URL (never `origin` the
         name — the same URL used for the eventual push/readback).
      5. If `present` -> deny, route to existing_branch_update lane.
         If `probe_error` -> deny, fail-closed.
      6. Re-confirm local HEAD == `expected_head` immediately before push.
      7. Execute the empty-expect lease push with `expected_head` (a
         verified 40-char SHA) embedded directly in the refspec source —
         NEVER the literal `HEAD` token, which git would re-resolve at
         push-time and could publish an unverified commit (Blocker 3).
      8. ALWAYS perform a fresh readback via the SAME push URL afterward,
         regardless of the push's returncode / timeout / transport
         exception (P1 High) — never treat "no readback" as success.
    """
    is_valid_name, name_reason = validate_branch_name_via_git(cwd, target_branch)
    if not is_valid_name:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code=name_reason,
            remote_state=None,
            remote_oid=None,
            push_returncode=None,
            push_error_category=None,
        )
    push_url, url_reason = resolve_single_push_url(cwd, remote=remote)
    if push_url is None:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code=url_reason,
            remote_state=None,
            remote_oid=None,
            push_returncode=None,
            push_error_category=None,
        )
    if _current_head(cwd) != expected_head:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code="head_changed_before_push",
            remote_state=None,
            remote_oid=None,
            push_returncode=None,
            push_error_category=None,
        )
    remote_state, remote_oid, probe_error_category = classify_remote_branch_state(
        cwd, push_url, target_branch, timeout=timeout
    )
    if remote_state == REMOTE_STATE_PRESENT:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code="remote_branch_present_route_existing_update",
            remote_state=remote_state,
            remote_oid=remote_oid,
            push_returncode=None,
            push_error_category=None,
        )
    if remote_state == REMOTE_STATE_PROBE_ERROR:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code="probe_error_fail_closed",
            remote_state=remote_state,
            remote_oid=None,
            push_returncode=None,
            push_error_category=probe_error_category,
        )
    # remote_state == absent. Re-confirm HEAD has not moved since the probe
    # (narrows, does not eliminate, the verify-to-push race — Blocker 3).
    if _current_head(cwd) != expected_head:
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_DENIED,
            reason_code="head_changed_before_push",
            remote_state=remote_state,
            remote_oid=None,
            push_returncode=None,
            push_error_category=None,
        )
    argv = build_initial_branch_create_argv(push_url, target_branch, verified_sha=expected_head)
    push_returncode: int | None = None
    push_error_category: str | None = None
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
        push_returncode = proc.returncode
    except subprocess.TimeoutExpired:
        push_error_category = PROBE_ERROR_CATEGORY_TIMEOUT
    except OSError:
        push_error_category = PROBE_ERROR_CATEGORY_TRANSPORT_ERROR

    # P1 High: ALWAYS readback via the same push URL, regardless of the push
    # outcome above — never skip readback on exception, never treat a
    # missing readback as success.
    matched, readback_reason, readback_oid = verify_initial_branch_create_readback(
        cwd, push_url, target_branch, expected_head, timeout=timeout
    )

    if push_error_category is not None:
        if matched:
            return InitialBranchCreateTransactionResult(
                status=INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_CREATED,
                reason_code=push_error_category,
                remote_state=REMOTE_STATE_PRESENT,
                remote_oid=readback_oid,
                push_returncode=None,
                push_error_category=push_error_category,
            )
        if readback_reason == "readback_failed_after_push":
            return InitialBranchCreateTransactionResult(
                status=INITIAL_BRANCH_CREATE_STATUS_TRANSPORT_ERROR_ABSENT,
                reason_code=push_error_category,
                remote_state=None,
                remote_oid=None,
                push_returncode=None,
                push_error_category=push_error_category,
            )
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_READBACK_MISMATCH,
            reason_code=push_error_category,
            remote_state=REMOTE_STATE_PRESENT,
            remote_oid=readback_oid,
            push_returncode=None,
            push_error_category=push_error_category,
        )

    if push_returncode == 0:
        if matched:
            return InitialBranchCreateTransactionResult(
                status=INITIAL_BRANCH_CREATE_STATUS_CREATED_VERIFIED,
                reason_code="initial_branch_create_completed",
                remote_state=REMOTE_STATE_PRESENT,
                remote_oid=readback_oid,
                push_returncode=push_returncode,
                push_error_category=None,
            )
        if readback_reason == "readback_failed_after_push":
            return InitialBranchCreateTransactionResult(
                status=INITIAL_BRANCH_CREATE_STATUS_READBACK_UNAVAILABLE,
                reason_code=readback_reason,
                remote_state=None,
                remote_oid=None,
                push_returncode=push_returncode,
                push_error_category=None,
            )
        return InitialBranchCreateTransactionResult(
            status=INITIAL_BRANCH_CREATE_STATUS_READBACK_MISMATCH,
            reason_code=readback_reason,
            remote_state=REMOTE_STATE_PRESENT,
            remote_oid=readback_oid,
            push_returncode=push_returncode,
            push_error_category=None,
        )

    # push failed (non-zero, no exception) — most commonly the empty-expect
    # lease was rejected because a competing process created the ref between
    # our probe and our push (Issue #1449 AC4 race scenario).
    return InitialBranchCreateTransactionResult(
        status=INITIAL_BRANCH_CREATE_STATUS_REJECTED_CONFLICT,
        reason_code="race_lease_rejected",
        remote_state=REMOTE_STATE_PRESENT if matched or readback_reason != "readback_failed_after_push" else None,
        remote_oid=readback_oid,
        push_returncode=push_returncode,
        push_error_category=None,
    )


# Issue #1589 (verified-ff-merge lane): exclusive outcome vocabulary for
# `execute_verified_ff_merge_transaction` -- every merge attempt (success,
# non-fast-forward rejection, postcondition violation, or a precondition
# failure that means no merge was attempted at all) is classified into
# exactly one of these, never silently treated as success.
MERGE_STATUS_MERGED_AND_VERIFIED = "merged_and_verified"
MERGE_STATUS_MERGE_REJECTED = "merge_rejected_non_fast_forward"
MERGE_STATUS_POSTCONDITION_VIOLATION = "postcondition_violation"
MERGE_STATUS_DENIED = "denied"
# Issue #1609 fix_delta (P1 Blocker): a timeout/transport exception
# during `git merge` execution is NEVER folded into a bare `denied` --
# it is classified into exactly one of these four outcomes after an
# unconditional postcondition readback.
MERGE_STATUS_EXECUTION_NOT_STARTED = "execution_not_started"
MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED = "transport_error_but_merged_and_verified"
MERGE_STATUS_TRANSPORT_ERROR_NO_MERGE = "transport_error_no_merge_observed"
MERGE_STATUS_TRANSPORT_ERROR_AMBIGUOUS = "transport_error_state_ambiguous"


@dataclass(frozen=True)
class VerifiedFfMergeTransactionResult:
    """Outcome of `execute_verified_ff_merge_transaction` (Issue #1589). `status`
    is `MERGE_STATUS_DENIED` (no merge attempted -- a precondition failed
    before `git merge` was ever invoked), `MERGE_STATUS_MERGE_REJECTED` (the
    merge itself was attempted but git refused the fast-forward),
    `MERGE_STATUS_POSTCONDITION_VIOLATION` (the merge returned success but the
    post-merge state does not match the required invariants -- e.g. a
    `post-merge` hook side effect), or `MERGE_STATUS_MERGED_AND_VERIFIED`
    (the merge succeeded and every postcondition was confirmed)."""

    status: str
    reason_code: str
    active_branch: str | None
    verified_local_head: str | None
    target_sha: str | None
    live_remote_head: str | None
    merge_returncode: int | None
    post_head: str | None


def _is_worktree_clean(cwd: str) -> bool | None:
    """Return True iff the working tree, index, and submodules are clean
    (Issue #1589 AC1/AC4). Returns None on probe failure (fail-closed at the
    call site -- never folded into `False`, mirrors the `probe_error`
    discrimination used elsewhere in this module)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--ignore-submodules=none"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return not result.stdout.strip()


_OPERATION_STATE_FILES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
_OPERATION_STATE_DIRS = ("rebase-merge", "rebase-apply")


def _git_path(cwd: str, relative: str) -> str | None:
    """Resolve `relative` via `git rev-parse --git-path <relative>` (Issue
    #1609 fix_delta P1 Blocker): operation-state markers such as MERGE_HEAD /
    CHERRY_PICK_HEAD / rebase-merge live under the PER-WORKTREE `$GIT_DIR`
    for a linked worktree, NOT the shared `$GIT_COMMON_DIR` -- resolving via
    `--git-common-dir` (the previous implementation) silently checked the
    wrong directory for every linked worktree and could never observe an
    in-progress operation there."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", relative],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    return out if os.path.isabs(out) else os.path.join(cwd, out)


def _has_in_progress_operation(cwd: str) -> bool | None:
    """Return True iff a git operation (merge/cherry-pick/revert/bisect/
    rebase) is currently in progress in `cwd` (Issue #1589 AC1/AC4; fixed for
    linked worktrees in Issue #1609 fix_delta). Returns None on probe failure
    (fail-closed at the call site)."""
    for name in _OPERATION_STATE_FILES:
        resolved = _git_path(cwd, name)
        if resolved is None:
            return None
        if os.path.exists(resolved):
            return True
    for name in _OPERATION_STATE_DIRS:
        resolved = _git_path(cwd, name)
        if resolved is None:
            return None
        if os.path.isdir(resolved):
            return True
    return False


def _git_dir(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    return os.path.realpath(out if os.path.isabs(out) else os.path.join(cwd, out))


def _git_common_dir(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    if not out:
        return None
    return os.path.realpath(out if os.path.isabs(out) else os.path.join(cwd, out))


def _is_linked_worktree(cwd: str) -> bool | None:
    """Return True iff `cwd` is inside a LINKED git worktree (its `$GIT_DIR`
    differs from `$GIT_COMMON_DIR`) rather than the primary/root checkout
    (Issue #1609 fix_delta P0). Self-verifying: does not rely on a
    caller-supplied `project_root` claim. Returns None on probe failure."""
    git_dir = _git_dir(cwd)
    common_dir = _git_common_dir(cwd)
    if git_dir is None or common_dir is None:
        return None
    return git_dir != common_dir


def _is_local_commit_object(cwd: str, sha: str) -> bool:
    if not _SHA_RE.fullmatch(sha or ""):
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "commit"


def _merge_denied(
    reason_code: str,
    *,
    active_branch: str | None = None,
    verified_local_head: str | None = None,
    target_sha: str | None = None,
    live_remote_head: str | None = None,
) -> VerifiedFfMergeTransactionResult:
    return VerifiedFfMergeTransactionResult(
        status=MERGE_STATUS_DENIED,
        reason_code=reason_code,
        active_branch=active_branch,
        verified_local_head=verified_local_head,
        target_sha=target_sha,
        live_remote_head=live_remote_head,
        merge_returncode=None,
        post_head=None,
    )


def _origin_fetch_url(cwd: str, remote: str = "origin") -> str | None:
    """Resolve `remote`'s single effective FETCH url (Issue #1609 fix_delta P1
    Blocker). `git remote get-url <remote>` (no `--push`) returns the fetch
    URL -- distinct from `--push`, which can be independently configured via
    `pushurl` and can point at a different repository while the fetch URL
    stays canonical. The live remote-state probe (`classify_remote_branch_state`)
    and the canonical-identity check MUST both operate on this SAME resolved
    URL so the thing that was verified canonical is the thing actually
    probed -- never the bare remote NAME, which `git` resolves independently
    and could disagree with what was checked here."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    return lines[0]


def execute_verified_ff_merge_transaction(
    cwd: str,
    target_sha: str,
    *,
    expected_worktree_realpath: str,
    active_issue_number: str,
    remote: str = "origin",
    timeout: int = 30,
) -> VerifiedFfMergeTransactionResult:
    """Single trusted verify -> live-remote-probe -> `git merge --ff-only` ->
    postcondition-readback boundary (Issue #1589), following the
    `execute_initial_branch_create_transaction` pattern (Issue #1449 / PR
    #1479): every precondition, the live remote probe, the merge itself, and
    the postcondition verification happen inside ONE call, so the state that
    was verified is the SAME state that gets mutated -- closing the TOCTOU
    window a classify-then-allow-the-raw-command design would leave open.

    Issue #1609 fix_delta (P0 Blocker): `classify_rtk_git_mutation` no longer
    performs this call as a side effect of classification -- the caller
    (`.claude/hooks/worktree_scope_guard.py` / `scripts/agent-ops/
    verified_ff_merge_exec.py`) MUST independently authorize the active
    Issue / matching worktree / cwd binding BEFORE invoking this function,
    and pass the authorized `expected_worktree_realpath` /
    `active_issue_number` in. This function re-verifies both against the
    LIVE repository state below -- it never trusts the caller's claim
    verbatim -- so authorization bypass requires compromising the live git
    state itself, not just the caller's self-reported strings.

    Sequence (each step fails closed):
      0. `cwd` must resolve to the SAME realpath as `expected_worktree_realpath`,
         and that path must be a LINKED worktree (its `$GIT_DIR` differs from
         `$GIT_COMMON_DIR`) -- never the primary/root checkout.
      1. `target_sha` must be an exact lowercase 40-hex SHA.
      2. Attached HEAD must be a non-default branch matching the canonical
         issue-worktree naming shape (`worktree-issue-<N>-<slug>`), and the
         captured `<N>` must equal `active_issue_number`.
      3. Worktree/index/submodules must be clean; no git operation may be
         in progress (`MERGE_HEAD` / `CHERRY_PICK_HEAD` / rebase state / etc,
         resolved via the PER-WORKTREE `git rev-parse --git-path`, not the
         shared `$GIT_COMMON_DIR`).
      4. `origin`'s single resolved FETCH url must match the canonical
         repository identity; the SAME url is used for the live remote probe
         below (never the bare remote name, never the push url).
      5. Live `git ls-remote --refs --exit-code` against that resolved fetch
         url for the active branch must return exactly `target_sha` (absent /
         mismatch / probe error all deny).
      6. `target_sha` must be a local commit object, and local HEAD must be
         an ancestor of it.
      7. Branch/HEAD are re-confirmed unchanged immediately before the merge
         (narrows, does not eliminate, the verify-to-merge race window).
      8. `git merge --ff-only <target_sha>` is executed as an argv list with
         `shell=False` (never shell-string concatenation).
      9. Postconditions are checked unconditionally after the merge attempt,
         INCLUDING after a timeout/transport exception (Issue #1609 fix_delta
         P1 Blocker) -- a timeout never means "denied": it means "ambiguous,
         go read the actual state back". active branch unchanged,
         `HEAD == target_sha`, worktree/index clean, and no operation residue
         (a `post-merge` hook side effect that leaves the tree dirty is NEVER
         treated as success).
    """
    if not _SHA_RE.fullmatch(target_sha or ""):
        return _merge_denied("invalid_target_sha", target_sha=target_sha)

    if not expected_worktree_realpath:
        return _merge_denied("expected_worktree_unresolved", target_sha=target_sha)
    if os.path.realpath(cwd) != os.path.realpath(expected_worktree_realpath):
        return _merge_denied("cwd_not_expected_worktree", target_sha=target_sha)

    is_linked = _is_linked_worktree(cwd)
    if is_linked is None:
        return _merge_denied("worktree_identity_probe_error", target_sha=target_sha)
    if not is_linked:
        return _merge_denied("expected_worktree_is_root_checkout", target_sha=target_sha)

    active_branch = _current_branch(cwd)
    if not active_branch:
        return _merge_denied("detached_head_not_supported", target_sha=target_sha)
    if active_branch in _resolve_default_branch_names(cwd):
        return _merge_denied(
            "merge_target_is_default_branch", active_branch=active_branch, target_sha=target_sha
        )
    branch_match = _ISSUE_WORKTREE_BRANCH_RE.fullmatch(active_branch)
    if not branch_match:
        return _merge_denied(
            "active_branch_not_issue_worktree_branch", active_branch=active_branch, target_sha=target_sha
        )
    if not active_issue_number or branch_match.group(1) != str(active_issue_number):
        return _merge_denied(
            "branch_issue_number_mismatch", active_branch=active_branch, target_sha=target_sha
        )

    is_clean = _is_worktree_clean(cwd)
    if is_clean is None:
        return _merge_denied(
            "worktree_status_probe_error", active_branch=active_branch, target_sha=target_sha
        )
    if not is_clean:
        return _merge_denied("worktree_dirty", active_branch=active_branch, target_sha=target_sha)

    has_op = _has_in_progress_operation(cwd)
    if has_op is None:
        return _merge_denied(
            "operation_state_probe_error", active_branch=active_branch, target_sha=target_sha
        )
    if has_op:
        return _merge_denied(
            "in_progress_git_operation", active_branch=active_branch, target_sha=target_sha
        )

    fetch_url = _origin_fetch_url(cwd, remote=remote)
    if not fetch_url or not _canonical_repo_url_pattern().match(fetch_url):
        return _merge_denied(
            "origin_remote_identity_mismatch", active_branch=active_branch, target_sha=target_sha
        )

    local_head = _current_head(cwd)
    if not local_head:
        return _merge_denied(
            "local_head_unavailable", active_branch=active_branch, target_sha=target_sha
        )

    remote_state, remote_oid, _probe_error_category = classify_remote_branch_state(
        cwd, fetch_url, active_branch, timeout=timeout
    )
    if remote_state != REMOTE_STATE_PRESENT or remote_oid != target_sha:
        if remote_state == REMOTE_STATE_PROBE_ERROR:
            reason_code = "live_remote_probe_failed"
        elif remote_state == REMOTE_STATE_ABSENT:
            reason_code = "live_remote_branch_absent"
        else:
            reason_code = "live_remote_head_mismatch"
        return _merge_denied(
            reason_code,
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
        )

    if not _is_local_commit_object(cwd, target_sha):
        return _merge_denied(
            "target_not_local_commit_object",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
        )

    if _is_ancestor(cwd, local_head, target_sha) is not True:
        return _merge_denied(
            "target_not_descendant_of_head",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
        )

    # Re-confirm branch/HEAD have not moved since verification (narrows,
    # does not eliminate, the verify-to-merge race window).
    if _current_branch(cwd) != active_branch or _current_head(cwd) != local_head:
        return _merge_denied(
            "branch_or_head_changed_before_merge",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
        )

    try:
        proc = subprocess.run(
            ["git", "merge", "--ff-only", target_sha],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError:
        # Issue #1609 fix_delta (P1 Blocker): an OSError here means the
        # subprocess never actually spawned (e.g. the `git` executable could
        # not be found) -- distinct from a timeout, where the merge process
        # DID start and may have completed. No merge was attempted.
        return VerifiedFfMergeTransactionResult(
            status=MERGE_STATUS_EXECUTION_NOT_STARTED,
            reason_code="merge_execution_not_started",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
            merge_returncode=None,
            post_head=None,
        )
    except subprocess.TimeoutExpired:
        # Issue #1609 fix_delta (P1 Blocker): a timeout does NOT mean the
        # merge was rejected/denied -- `git merge` may have completed (e.g.
        # stalled inside a slow post-merge hook) before the transport timed
        # out. ALWAYS perform an unconditional postcondition readback and
        # classify the ambiguous outcome into one of three disjoint buckets,
        # never a bare "denied".
        post_branch = _current_branch(cwd)
        post_head = _current_head(cwd)
        post_clean = _is_worktree_clean(cwd)
        post_has_op = _has_in_progress_operation(cwd)
        if (
            post_branch == active_branch
            and post_head == target_sha
            and post_clean is True
            and post_has_op is False
        ):
            return VerifiedFfMergeTransactionResult(
                status=MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
                reason_code="merge_execution_timeout_but_merged_and_verified",
                active_branch=active_branch,
                verified_local_head=local_head,
                target_sha=target_sha,
                live_remote_head=remote_oid,
                merge_returncode=None,
                post_head=post_head,
            )
        if (
            post_branch == active_branch
            and post_head == local_head
            and post_clean is True
            and post_has_op is False
        ):
            return VerifiedFfMergeTransactionResult(
                status=MERGE_STATUS_TRANSPORT_ERROR_NO_MERGE,
                reason_code="merge_execution_timeout_no_merge_observed",
                active_branch=active_branch,
                verified_local_head=local_head,
                target_sha=target_sha,
                live_remote_head=remote_oid,
                merge_returncode=None,
                post_head=post_head,
            )
        return VerifiedFfMergeTransactionResult(
            status=MERGE_STATUS_TRANSPORT_ERROR_AMBIGUOUS,
            reason_code="merge_execution_timeout_state_ambiguous",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
            merge_returncode=None,
            post_head=post_head,
        )

    post_branch = _current_branch(cwd)
    post_head = _current_head(cwd)
    post_clean = _is_worktree_clean(cwd)
    post_has_op = _has_in_progress_operation(cwd)

    if proc.returncode != 0:
        return VerifiedFfMergeTransactionResult(
            status=MERGE_STATUS_MERGE_REJECTED,
            reason_code="merge_ff_only_rejected",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
            merge_returncode=proc.returncode,
            post_head=post_head,
        )

    if post_branch != active_branch or post_head != target_sha or post_clean is not True or post_has_op is not False:
        return VerifiedFfMergeTransactionResult(
            status=MERGE_STATUS_POSTCONDITION_VIOLATION,
            reason_code="postcondition_check_failed",
            active_branch=active_branch,
            verified_local_head=local_head,
            target_sha=target_sha,
            live_remote_head=remote_oid,
            merge_returncode=proc.returncode,
            post_head=post_head,
        )

    return VerifiedFfMergeTransactionResult(
        status=MERGE_STATUS_MERGED_AND_VERIFIED,
        reason_code="verified_ff_merge_completed",
        active_branch=active_branch,
        verified_local_head=local_head,
        target_sha=target_sha,
        live_remote_head=remote_oid,
        merge_returncode=proc.returncode,
        post_head=post_head,
    )


def _canonical_repo_identity() -> str:
    return os.environ.get("LOOP_CANONICAL_REPO_IDENTITY", "").strip() or CANONICAL_REPO_IDENTITY_DEFAULT


def _canonical_repo_url_pattern() -> re.Pattern[str]:
    override = os.environ.get("LOOP_CANONICAL_REPO_URL_PATTERN", "").strip()
    if override:
        return re.compile(override)
    identity = re.escape(_canonical_repo_identity())
    return re.compile(_CANONICAL_REPO_URL_TEMPLATE.format(identity=identity))


def _origin_push_urls(cwd: str, remote: str = "origin") -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "--push", "--all", remote],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    urls = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return urls or None


def _origin_push_urls_match_canonical_repo(cwd: str) -> bool:
    """Verify every configured `origin` push URL resolves to the canonical
    repository identity (Issue #1408 iteration-2, P2). Guards against
    remote reconfiguration / `insteadOf` redirection pointing the push at a
    different repository while the `origin` name and branch/head checks
    still pass."""
    urls = _origin_push_urls(cwd)
    if not urls:
        return False
    pattern = _canonical_repo_url_pattern()
    return all(pattern.match(url) for url in urls)


def _resolve_default_branch_names(cwd: str) -> frozenset[str]:
    """Return the set of branch names treated as protected default branches
    for the push-target destination guard (Issue #1408 iteration-2, P2).
    Independent of #360's hook-level destination guard — this is a
    policy-internal regression backstop."""
    names = set(DEFAULT_BRANCH_NAMES)
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        ref = result.stdout.strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix):
            resolved = ref[len(prefix) :]
            if resolved:
                names.add(resolved)
    env_default = os.environ.get("LOOP_DEFAULT_BRANCH", "").strip()
    if env_default:
        names.add(env_default)
    return frozenset(names)


def _is_ancestor(cwd: str, ancestor: str, descendant: str) -> bool | None:
    if not (_SHA_RE.fullmatch(ancestor) and _SHA_RE.fullmatch(descendant)):
        return None
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _classify_remote_drift(cwd: str, expected_remote_head: str, current_remote_head: str) -> str:
    ancestry = _is_ancestor(cwd, expected_remote_head, current_remote_head)
    if ancestry is True:
        return "remote_fast_forward_by_same_scope"
    if ancestry is False:
        return "non_fast_forward_remote_rewrite"
    return "remote_head_scope_contamination"


def _extract_git_argv(tokens: list[str]) -> list[str] | None:
    if len(tokens) >= 3 and tokens[0] == "rtk" and tokens[1] == "git":
        return tokens[1:]
    return None


def _load_publish_guard_context() -> tuple[PublishGuardContext | None, str | None]:
    expected_remote_head = os.environ.get("LOOP_PUBLISH_EXPECTED_REMOTE_HEAD", "").strip().lower()
    current_remote_head = os.environ.get("LOOP_PUBLISH_CURRENT_REMOTE_HEAD", "").strip().lower()
    declared_publish_head = os.environ.get("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", "").strip().lower()
    verified_head = os.environ.get("LOOP_PUBLISH_VERIFIED_HEAD", "").strip().lower()
    allowed_paths_gate_status = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "").strip().lower()
    remote_readback_source = os.environ.get("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "").strip().lower()
    # Issue #1408 iteration-2 (P2): bind the Allowed Paths gate `status: ok`
    # to the issue / base / head it was evaluated against, so a stale `ok`
    # from a prior head or a different issue cannot be replayed.
    allowed_paths_gate_issue_number = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", "").strip()
    allowed_paths_gate_base_sha = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA", "").strip().lower()
    allowed_paths_gate_head_sha = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA", "").strip().lower()
    fields = (
        expected_remote_head,
        current_remote_head,
        declared_publish_head,
        verified_head,
        allowed_paths_gate_status,
        remote_readback_source,
        allowed_paths_gate_issue_number,
        allowed_paths_gate_base_sha,
        allowed_paths_gate_head_sha,
    )
    if not any(fields):
        return None, "publish_guard_context_missing"
    if not all(fields):
        return None, "publish_guard_context_invalid"
    if not (
        _SHA_RE.fullmatch(expected_remote_head or "")
        and _SHA_RE.fullmatch(current_remote_head or "")
        and _SHA_RE.fullmatch(declared_publish_head or "")
        and _SHA_RE.fullmatch(verified_head or "")
        and _SHA_RE.fullmatch(allowed_paths_gate_base_sha or "")
        and _SHA_RE.fullmatch(allowed_paths_gate_head_sha or "")
    ):
        return None, "publish_guard_context_invalid"
    if not allowed_paths_gate_issue_number.isdigit():
        return None, "publish_guard_context_invalid"
    if allowed_paths_gate_status not in ALLOWED_ALLOWED_PATHS_GATE_STATUSES:
        return None, "publish_guard_context_invalid"
    if remote_readback_source not in ALLOWED_REMOTE_READBACK_SOURCES:
        return None, "publish_guard_context_invalid"
    # Note: the issue/base/head binding check (Issue #1408 iteration-2, P2)
    # is performed in `classify_rtk_git_mutation` against the actual local
    # HEAD (`_current_head`), not here — binding to the *real* HEAD (rather
    # than the self-declared `declared_publish_head` / `verified_head`
    # claims) keeps this check independent of `local_head_mismatch`.
    return PublishGuardContext(
        expected_remote_head=expected_remote_head,
        current_remote_head=current_remote_head,
        declared_publish_head=declared_publish_head,
        verified_head=verified_head,
        allowed_paths_gate_status=allowed_paths_gate_status,
        remote_readback_source=remote_readback_source,
        decision_inputs_complete=True,
        allowed_paths_gate_issue_number=allowed_paths_gate_issue_number,
        allowed_paths_gate_base_sha=allowed_paths_gate_base_sha,
        allowed_paths_gate_head_sha=allowed_paths_gate_head_sha,
    ), None


def _load_initial_branch_create_guard_context() -> tuple[InitialBranchCreateGuardContext | None, str | None]:
    """Issue #1449 (PR #1479 OWNER review, P2 High): load the guard context
    for the initial_branch_create lane WITHOUT requiring
    `LOOP_PUBLISH_EXPECTED_REMOTE_HEAD` / `LOOP_PUBLISH_CURRENT_REMOTE_HEAD`
    (there is no remote head yet for a branch that has never been pushed).
    Only `declared_publish_head` / `verified_head` (both real 40-char local
    commit SHAs the caller is claiming to publish) plus the Allowed Paths
    gate binding are required."""
    declared_publish_head = os.environ.get("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", "").strip().lower()
    verified_head = os.environ.get("LOOP_PUBLISH_VERIFIED_HEAD", "").strip().lower()
    allowed_paths_gate_status = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "").strip().lower()
    remote_readback_source = os.environ.get("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "").strip().lower()
    allowed_paths_gate_issue_number = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER", "").strip()
    allowed_paths_gate_head_sha = os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA", "").strip().lower()
    fields = (
        declared_publish_head,
        verified_head,
        allowed_paths_gate_status,
        remote_readback_source,
        allowed_paths_gate_issue_number,
        allowed_paths_gate_head_sha,
    )
    if not any(fields):
        return None, "publish_guard_context_missing"
    if not all(fields):
        return None, "publish_guard_context_invalid"
    if not (
        _SHA_RE.fullmatch(declared_publish_head)
        and _SHA_RE.fullmatch(verified_head)
        and _SHA_RE.fullmatch(allowed_paths_gate_head_sha)
    ):
        return None, "publish_guard_context_invalid"
    if not allowed_paths_gate_issue_number.isdigit():
        return None, "publish_guard_context_invalid"
    if allowed_paths_gate_status not in ALLOWED_ALLOWED_PATHS_GATE_STATUSES:
        return None, "publish_guard_context_invalid"
    if remote_readback_source not in ALLOWED_REMOTE_READBACK_SOURCES:
        return None, "publish_guard_context_invalid"
    return InitialBranchCreateGuardContext(
        declared_publish_head=declared_publish_head,
        verified_head=verified_head,
        allowed_paths_gate_status=allowed_paths_gate_status,
        remote_readback_source=remote_readback_source,
        decision_inputs_complete=True,
        allowed_paths_gate_issue_number=allowed_paths_gate_issue_number,
        allowed_paths_gate_head_sha=allowed_paths_gate_head_sha,
    ), None


def _load_allowed_paths() -> list[str] | None:
    raw = os.environ.get("CODEX_ALLOWED_PATHS", "").strip()
    if not raw:
        return None
    entries = [line.strip() for line in raw.splitlines() if line.strip()]
    return entries or None


def _normalize_path(path: str) -> str | None:
    if not path or "\\" in path or path.startswith("/"):
        return None
    normalized = path[2:] if path.startswith("./") else path
    if normalized in {"", "."}:
        return None
    segments = normalized.split("/")
    if ".." in segments or "" in segments:
        return None
    return normalized


def _normalize_allowed_pattern(pattern: str) -> str | None:
    if pattern.endswith("/"):
        bare = pattern[:-1]
        if bare.endswith("/") or "*" in bare:
            return None
        normalized_bare = _normalize_path(bare)
        if normalized_bare is None:
            return None
        return normalized_bare + "/**"
    normalized = _normalize_path(pattern)
    if normalized is None:
        return None
    for segment in normalized.split("/"):
        if "*" in segment and segment not in ("*", "**"):
            return None
    return normalized


def _segment_match(file_parts: list[str], pattern_parts: list[str]) -> bool:
    n = len(file_parts)
    m = len(pattern_parts)
    dp = [[False] * (m + 1) for _ in range(n + 1)]
    dp[n][m] = True
    for j in range(m - 1, -1, -1):
        if pattern_parts[j] == "**":
            dp[n][j] = dp[n][j + 1]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            segment = pattern_parts[j]
            if segment == "**":
                dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
            elif segment == "*":
                dp[i][j] = dp[i + 1][j + 1]
            else:
                dp[i][j] = segment == file_parts[i] and dp[i + 1][j + 1]
    return dp[0][0]


def _is_allowed_path(file_path: str, allowed_paths: list[str]) -> bool:
    normalized_file = _normalize_path(file_path)
    if normalized_file is None:
        return False
    for pattern in allowed_paths:
        normalized_pattern = _normalize_allowed_pattern(pattern)
        if normalized_pattern is None:
            continue
        if _segment_match(normalized_file.split("/"), normalized_pattern.split("/")):
            return True
    return False


def _pathspec_to_repo_relative(pathspec: str, cwd: str, repo_root: str) -> str | None:
    candidate = Path(cwd, pathspec).resolve()
    repo_path = Path(repo_root).resolve()
    try:
        relative = candidate.relative_to(repo_path)
    except ValueError:
        return None
    return _normalize_path(relative.as_posix())


def _staged_repo_paths(cwd: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=cwd,
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw_entries = [entry.decode("utf-8", errors="replace") for entry in result.stdout.split(b"\x00") if entry]
    normalized = []
    for entry in raw_entries:
        candidate = _normalize_path(entry)
        if candidate is None:
            return None
        normalized.append(candidate)
    return normalized


def _contains_broad_pathspec(pathspecs: list[str], cwd: str) -> bool:
    if not pathspecs:
        return True
    for pathspec in pathspecs:
        if pathspec in {".", "..", ":/"}:
            return True
        if any(ch in pathspec for ch in "*?[]"):
            return True
        if pathspec.startswith(":("):
            return True
        resolved = os.path.realpath(os.path.join(cwd, pathspec))
        if os.path.isdir(resolved):
            return True
    return False


def _publish_safety_stop_result(
    *,
    reason_code: str,
    target_branch: str,
    expected_remote_head: str | None,
    current_remote_head: str | None,
    local_head: str | None,
    verified_head: str,
    declared_publish_head: str,
    allowed_paths_gate_status: str,
    pr_number: str | None,
    remote_readback_source: str | None,
    decision_inputs_complete: bool,
    boundary_layer: str,
    command_class: str = COMMAND_CLASS_RTK_GIT_PUSH,
    remote_state: str | None = None,
    remote_state_error_category: str | None = None,
) -> GitMutationPolicyResult:
    return GitMutationPolicyResult(
        status="deny",
        command_class=command_class,
        reason_code=reason_code,
        suggested_command=None,
        verification_command=(
            "uv run --locked python3 scripts/agent-ops/git_ref_probe.py "
            f"--branch {target_branch} --remote origin --json"
        ),
        expected_remote_head=expected_remote_head,
        current_remote_head=current_remote_head,
        local_head=local_head,
        verified_head=verified_head,
        declared_publish_head=declared_publish_head,
        allowed_paths_gate_status=allowed_paths_gate_status,
        target_branch=target_branch,
        pr_number=pr_number,
        remote_readback_source=remote_readback_source,
        decision_inputs_complete=decision_inputs_complete,
        required_decisions=(
            "PR branch を linked issue 専用 head へ戻す",
            "混入 commit を別 PR / 別 branch へ退避する",
        ),
        boundary_layer=boundary_layer,
        remote_state=remote_state,
        remote_state_error_category=remote_state_error_category,
    )


def _classify_initial_branch_create_push(
    args: list[str],
    *,
    cwd: str,
    require_active_branch_push: bool,
    boundary_layer: str,
) -> GitMutationPolicyResult:
    """Classify an `rtk git push --force-with-lease=refs/heads/<b>: origin
    HEAD:refs/heads/<b>` initial_branch_create lane candidate (Issue #1449).

    This is a SEPARATE decision path from the existing_branch_update lane
    (`git_argv` shape `push origin HEAD:refs/heads/<b>` with exactly 2 args)
    handled further below in `classify_rtk_git_mutation`. It is only reached
    when the push argv has 3 tokens whose first token starts with
    `--force-with-lease=` (see call site)."""
    lease_flag, remote, refspec = args[0], args[1], args[2]
    del lease_flag  # validated via validate_initial_branch_create_argv below
    if remote != "origin" or not refspec.startswith("HEAD:refs/heads/"):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    target_branch = refspec.removeprefix("HEAD:refs/heads/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    # Issue #1449 (PR #1479 OWNER review, P2): validate against Git's own
    # ref-name grammar, not just the looser regex above (which incorrectly
    # accepts `.foo`, `foo.lock`, `foo..bar`, `foo//bar`, trailing `/`/`.`,
    # and a leading `-`).
    is_valid_git_name, git_name_reason = validate_branch_name_via_git(cwd, target_branch)
    if not is_valid_git_name:
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            reason_code=git_name_reason,
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
            target_branch=target_branch,
        )
    if target_branch in _resolve_default_branch_names(cwd):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            reason_code="push_target_is_default_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
            target_branch=target_branch,
        )
    if require_active_branch_push:
        current = _current_branch(cwd)
        if not current or current != target_branch:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
                reason_code="push_refspec_requires_active_branch",
                suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
                verification_command="git branch --show-current",
            )
    is_valid_argv, argv_reason = validate_initial_branch_create_argv(args, target_branch)
    if not is_valid_argv:
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            reason_code=argv_reason,
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
            target_branch=target_branch,
        )
    # Issue #1449 (PR #1479 OWNER review, P2 High): the initial_branch_create
    # lane uses its OWN guard-context loader — it does not require a
    # fabricated remote-head SHA for a branch that has no remote ref yet.
    publish_guard, publish_guard_error = _load_initial_branch_create_guard_context()
    local_head = _current_head(cwd)
    if publish_guard is None:
        return _publish_safety_stop_result(
            reason_code=publish_guard_error or "publish_guard_context_missing",
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=None,
            local_head=local_head,
            verified_head=os.environ.get("LOOP_PUBLISH_VERIFIED_HEAD", "").strip().lower(),
            declared_publish_head=os.environ.get("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", "").strip().lower(),
            allowed_paths_gate_status=(
                os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "").strip().lower()
                or "indeterminate"
            ),
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=os.environ.get("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "").strip().lower(),
            decision_inputs_complete=False,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    loop_issue_number = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    if (
        publish_guard.allowed_paths_gate_issue_number != loop_issue_number
        or publish_guard.allowed_paths_gate_head_sha != (local_head or "")
    ):
        return _publish_safety_stop_result(
            reason_code="allowed_paths_gate_binding_mismatch",
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=None,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if publish_guard.allowed_paths_gate_status != "ok":
        return _publish_safety_stop_result(
            reason_code="allowed_paths_gate_not_ok",
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=None,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if local_head != publish_guard.declared_publish_head or local_head != publish_guard.verified_head:
        return _publish_safety_stop_result(
            reason_code="local_head_mismatch",
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=None,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if not _origin_push_urls_match_canonical_repo(cwd):
        return _publish_safety_stop_result(
            reason_code="origin_remote_identity_mismatch",
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=None,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )

    # Issue #1449 (PR #1479 OWNER review, P1 Blocker 1/2/3, P1 High): the
    # actual remote write happens HERE, inside this single trusted
    # classify-and-execute boundary — probe, push (with the verified SHA
    # embedded in the refspec, via the single resolved push URL), and
    # readback (via that SAME URL) all happen inside
    # `execute_initial_branch_create_transaction`. The CLI/adapter never
    # separately "allows" a raw shell push for this lane and lets it run on
    # its own afterward.
    transaction = execute_initial_branch_create_transaction(
        cwd, target_branch, local_head or "", remote="origin", timeout=30
    )
    if transaction.status == INITIAL_BRANCH_CREATE_STATUS_DENIED:
        return _publish_safety_stop_result(
            reason_code=transaction.reason_code,
            target_branch=target_branch,
            expected_remote_head=None,
            current_remote_head=transaction.remote_oid,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            remote_state=transaction.remote_state,
            remote_state_error_category=transaction.push_error_category,
        )
    # Every non-DENIED transaction outcome (`created_and_verified`,
    # `rejected_conflict`, `transport_error_but_created_and_verified`,
    # `transport_error_remote_absent`, `readback_mismatch`,
    # `readback_unavailable`) means the real push was already attempted and
    # (for the transport/readback-ambiguous cases) MAY have already
    # succeeded on the remote. `status` stays `deny` so the caller's raw
    # shell command is never independently re-run against the same
    # empty-expect lease (it would either be a harmless no-op rejection or,
    # worse, mask which of our controlled attempt vs. a redundant retry
    # actually created the ref) — `reason_code` carries the transaction
    # outcome for the caller to inspect.
    return GitMutationPolicyResult(
        status="deny",
        command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        reason_code=transaction.status,
        expected_remote_head=None,
        current_remote_head=transaction.remote_oid,
        local_head=local_head,
        verified_head=publish_guard.verified_head,
        declared_publish_head=publish_guard.declared_publish_head,
        allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
        target_branch=target_branch,
        pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
        remote_readback_source=publish_guard.remote_readback_source,
        decision_inputs_complete=publish_guard.decision_inputs_complete,
        boundary_layer=boundary_layer,
        remote_state=transaction.remote_state,
        remote_state_error_category=transaction.push_error_category,
    )


def _classify_rtk_git_merge(
    args: list[str], *, cwd: str, boundary_layer: str
) -> GitMutationPolicyResult:
    """Classify an `rtk git merge` candidate (Issue #1589 / #1609 fix_delta).
    Only the exact 2-token shape `--ff-only <40-hex-sha>` is recognized;
    every other shape (short/non-hex SHA, uppercase SHA, reordered flags,
    extra options, `--no-ff`, a bare branch name, etc.) is denied. This
    function is a PURE shape classifier -- it performs NO subprocess calls
    and has NO side effects (Issue #1609 P0 Blocker fix: the previous
    implementation executed the real merge as a side effect of
    classification, BEFORE the caller had authorized the active Issue /
    matching worktree / cwd binding). The actual verify -> probe -> merge ->
    readback transaction is executed ONLY by the caller
    (`.claude/hooks/worktree_scope_guard.py`), and ONLY after that caller has
    independently authorized the command -- never as a classify side
    effect."""
    # Issue #1609 fix_delta (P0 Blocker): PURE shape classification only --
    # no subprocess call, no merge execution, no cwd/branch/remote
    # inspection here. `args[1]` is checked against the RAW (non-lowercased)
    # value first so an uppercase-hex SHA is rejected outright (P2 fix --
    # the previous implementation lowercased before matching the
    # lowercase-only regex, silently accepting uppercase input). The actual
    # verify -> probe -> merge -> readback transaction is executed ONLY by
    # the caller AFTER it independently authorizes the active Issue /
    # matching worktree / cwd binding (see
    # `execute_verified_ff_merge_transaction`).
    if len(args) != 2 or args[0] != "--ff-only":
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
            reason_code="merge_shape_requires_exact_ff_only_sha",
            suggested_command="rtk git merge --ff-only <40-hex-target-sha>",
            verification_command="git branch --show-current",
        )
    raw_sha = args[1] or ""
    if not _SHA_RE.fullmatch(raw_sha):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
            reason_code="merge_shape_requires_exact_ff_only_sha",
            suggested_command="rtk git merge --ff-only <40-hex-target-sha>",
            verification_command="git branch --show-current",
        )
    target_sha = raw_sha
    return GitMutationPolicyResult(
        status="allow",
        command_class=COMMAND_CLASS_RTK_GIT_MERGE_FF_ONLY,
        reason_code="merge_shape_valid_pending_authorization",
        target_sha=target_sha,
        boundary_layer=boundary_layer,
        verification_command="git rev-parse HEAD",
    )


def classify_rtk_git_mutation(
    command: str,
    *,
    cwd: str,
    require_active_branch_push: bool,
    boundary_layer: str = "worktree_scope_guard_denied",
) -> GitMutationPolicyResult | None:
    """Return a bounded policy result for recognized `rtk git` commands.

    `boundary_layer` identifies the PreToolUse-layer caller for
    `PUBLISH_SAFETY_STOP_REPORT_V1`-shaped deny reasons (Issue #1408). It
    defaults to the historical `worktree_scope_guard_denied` value used by
    `.claude/hooks/worktree_scope_guard.py` so existing callers are unaffected.
    """
    tokens = _tokenize(command)
    if not tokens:
        return None
    git_argv = _extract_git_argv(tokens)
    if not git_argv or git_argv[0] != "git" or len(git_argv) < 2:
        return None

    subcommand = git_argv[1]
    args = git_argv[2:]
    if subcommand not in ALLOWED_RTK_GIT_SUBCOMMANDS:
        return None

    if subcommand == "add":
        if not args or any(arg in {"-A", "-u", "--all"} for arg in args):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="git_add_requires_explicit_pathspec",
                suggested_command="rtk git add <allowed-path-file>",
                verification_command="git diff --name-only",
            )
        filtered = [arg for arg in args if arg != "--"]
        if any(arg.startswith("--pathspec-from-file") for arg in filtered) or _contains_broad_pathspec(filtered, cwd):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="git_add_requires_explicit_pathspec",
                suggested_command="rtk git add <allowed-path-file>",
                verification_command="git diff --name-only",
            )
        allowed_paths = _load_allowed_paths()
        repo_root = _git_toplevel(cwd)
        if not allowed_paths or not repo_root:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_ADD,
                reason_code="allowed_paths_missing_for_git_mutation",
                suggested_command="git diff --name-only",
                verification_command="git diff --name-only",
            )
        for pathspec in filtered:
            repo_relative = _pathspec_to_repo_relative(pathspec, cwd, repo_root)
            if repo_relative is None or not _is_allowed_path(repo_relative, allowed_paths):
                return GitMutationPolicyResult(
                    status="deny",
                    command_class=COMMAND_CLASS_RTK_GIT_ADD,
                    reason_code="git_add_outside_allowed_paths",
                    suggested_command="rtk git add <allowed-path-file>",
                    verification_command="git diff --name-only",
                )
        return GitMutationPolicyResult(
            status="allow",
            command_class=COMMAND_CLASS_RTK_GIT_ADD,
            reason_code="rtk_git_add_allowed",
        )

    if subcommand == "commit":
        if len(args) != 2 or args[0] != "-m" or not args[1].strip():
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="rtk_git_commit_requires_message",
                suggested_command='rtk git commit -m "issue-1241 update"',
                verification_command="git diff --cached --name-only",
            )
        allowed_paths = _load_allowed_paths()
        staged_paths = _staged_repo_paths(cwd)
        if not allowed_paths or staged_paths is None:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="allowed_paths_missing_for_git_mutation",
                suggested_command="git diff --cached --name-only",
                verification_command="git diff --cached --name-only",
            )
        if any(not _is_allowed_path(path, allowed_paths) for path in staged_paths):
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
                reason_code="commit_staged_changes_outside_allowed_paths",
                suggested_command='rtk git commit -m "issue-1241 update"',
                verification_command="git diff --cached --name-only",
            )
        return GitMutationPolicyResult(
            status="allow",
            command_class=COMMAND_CLASS_RTK_GIT_COMMIT,
            reason_code="rtk_git_commit_allowed",
        )

    if subcommand == "merge":
        return _classify_rtk_git_merge(args, cwd=cwd, boundary_layer=boundary_layer)

    if any(flag in args for flag in DENIED_PUSH_FLAGS):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )

    # Issue #1449: initial_branch_create lane candidate — a SEPARATE decision
    # path from the existing_branch_update lane below. Only entered when the
    # push argv is exactly 3 tokens whose first token starts with
    # `--force-with-lease=` (the existing_branch_update lane never has more
    # than 2 args: `origin HEAD:refs/heads/<branch>`).
    if len(args) == 3 and args[0].startswith("--force-with-lease="):
        return _classify_initial_branch_create_push(
            args,
            cwd=cwd,
            require_active_branch_push=require_active_branch_push,
            boundary_layer=boundary_layer,
        )

    if len(args) != 2 or args[0] != "origin":
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    refspec = args[1]
    if not refspec.startswith("HEAD:refs/heads/"):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    target_branch = refspec.removeprefix("HEAD:refs/heads/")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_refspec_requires_active_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
        )
    # Issue #1408 iteration-2 (P2): policy-internal destination guard,
    # independent of #360's hook-level default-branch destination guard.
    if target_branch in _resolve_default_branch_names(cwd):
        return GitMutationPolicyResult(
            status="deny",
            command_class=COMMAND_CLASS_RTK_GIT_PUSH,
            reason_code="push_target_is_default_branch",
            suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
            verification_command="git branch --show-current",
            target_branch=target_branch,
        )
    if require_active_branch_push:
        current = _current_branch(cwd)
        if not current or current != target_branch:
            return GitMutationPolicyResult(
                status="deny",
                command_class=COMMAND_CLASS_RTK_GIT_PUSH,
                reason_code="push_refspec_requires_active_branch",
                suggested_command="rtk git push origin HEAD:refs/heads/<active-branch>",
                verification_command="git branch --show-current",
            )
    publish_guard, publish_guard_error = _load_publish_guard_context()
    local_head = _current_head(cwd)
    if publish_guard is None:
        return _publish_safety_stop_result(
            reason_code=publish_guard_error or "publish_guard_context_missing",
            target_branch=target_branch,
            expected_remote_head=os.environ.get("LOOP_PUBLISH_EXPECTED_REMOTE_HEAD", "").strip().lower(),
            current_remote_head=os.environ.get("LOOP_PUBLISH_CURRENT_REMOTE_HEAD", "").strip().lower(),
            local_head=local_head,
            verified_head=os.environ.get("LOOP_PUBLISH_VERIFIED_HEAD", "").strip().lower(),
            declared_publish_head=os.environ.get("LOOP_PUBLISH_DECLARED_PUBLISH_HEAD", "").strip().lower(),
            allowed_paths_gate_status=(
                os.environ.get("LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS", "").strip().lower()
                or "indeterminate"
            ),
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=os.environ.get("LOOP_PUBLISH_REMOTE_READBACK_SOURCE", "").strip().lower(),
            decision_inputs_complete=False,
            boundary_layer=boundary_layer,
        )
    if publish_guard is not None:
        # Issue #1408 iteration-2 (P2): bind the Allowed Paths gate `ok` to
        # this issue and to the *actual* local HEAD (not the self-declared
        # `declared_publish_head` / `verified_head` claims), so a stale gate
        # from a prior head or a different issue cannot be replayed.
        loop_issue_number = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
        if (
            publish_guard.allowed_paths_gate_issue_number != loop_issue_number
            or publish_guard.allowed_paths_gate_base_sha != publish_guard.expected_remote_head
            or publish_guard.allowed_paths_gate_head_sha != (local_head or "")
        ):
            return _publish_safety_stop_result(
                reason_code="allowed_paths_gate_binding_mismatch",
                target_branch=target_branch,
                expected_remote_head=publish_guard.expected_remote_head,
                current_remote_head=publish_guard.current_remote_head,
                local_head=local_head,
                verified_head=publish_guard.verified_head,
                declared_publish_head=publish_guard.declared_publish_head,
                allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
                pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
                remote_readback_source=publish_guard.remote_readback_source,
                decision_inputs_complete=publish_guard.decision_inputs_complete,
                boundary_layer=boundary_layer,
            )
        current_remote_head = publish_guard.current_remote_head
        if publish_guard.remote_readback_source == "ls_remote":
            live_remote_head, remote_absent = _ls_remote_head(cwd, "origin", target_branch)
            if remote_absent:
                # Issue #1408 iteration-2 (P1): new-branch initial publish
                # (remote ref does not exist yet) via the existing_branch_update
                # lane's plain `HEAD:refs/heads/<branch>` refspec remains out
                # of scope for THIS lane — see Issue #1449, which adds a
                # SEPARATE initial_branch_create lane (handled above via the
                # `--force-with-lease=` argv shape) rather than changing this
                # lane's behavior.
                return _publish_safety_stop_result(
                    reason_code="remote_branch_absent_not_supported",
                    target_branch=target_branch,
                    expected_remote_head=publish_guard.expected_remote_head,
                    current_remote_head=current_remote_head,
                    local_head=local_head,
                    verified_head=publish_guard.verified_head,
                    declared_publish_head=publish_guard.declared_publish_head,
                    allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
                    pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
                    remote_readback_source=publish_guard.remote_readback_source,
                    decision_inputs_complete=False,
                    boundary_layer=boundary_layer,
                )
            if not live_remote_head:
                return _publish_safety_stop_result(
                    reason_code="publish_guard_context_invalid",
                    target_branch=target_branch,
                    expected_remote_head=publish_guard.expected_remote_head,
                    current_remote_head=current_remote_head,
                    local_head=local_head,
                    verified_head=publish_guard.verified_head,
                    declared_publish_head=publish_guard.declared_publish_head,
                    allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
                    pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
                    remote_readback_source=publish_guard.remote_readback_source,
                    decision_inputs_complete=False,
                    boundary_layer=boundary_layer,
                )
            current_remote_head = live_remote_head
        remote_drift_reason = None
        if publish_guard.expected_remote_head != current_remote_head:
            remote_drift_reason = _classify_remote_drift(
                cwd,
                publish_guard.expected_remote_head,
                current_remote_head,
            )
        decision = evaluate_publish_lane(
            remote="origin",
            active_branch=_current_branch(cwd) or "",
            target_branch=target_branch,
            expected_remote_head=publish_guard.expected_remote_head,
            current_remote_head=current_remote_head,
            local_head=local_head or "",
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            remote_drift_reason=remote_drift_reason,
            boundary_layer=boundary_layer,
            issue_number=int(os.environ.get("LOOP_ISSUE_NUMBER", "0") or "0"),
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
        )
        if decision.status != "allow_retry":
            return _publish_safety_stop_result(
                reason_code=decision.publish_failure_reason["reason_code"],
                target_branch=target_branch,
                expected_remote_head=publish_guard.expected_remote_head,
                current_remote_head=current_remote_head,
                local_head=local_head,
                verified_head=publish_guard.verified_head,
                declared_publish_head=publish_guard.declared_publish_head,
                allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
                pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
                remote_readback_source=publish_guard.remote_readback_source,
                decision_inputs_complete=publish_guard.decision_inputs_complete,
                boundary_layer=boundary_layer,
            )
        # Issue #1408 iteration-2 (P2): verify the actual push destination
        # (not just the `origin` remote *name*) resolves to the canonical
        # repository before allowing the push. Guards against remote
        # reconfiguration / `insteadOf` redirection to a different repo.
        if not _origin_push_urls_match_canonical_repo(cwd):
            return _publish_safety_stop_result(
                reason_code="origin_remote_identity_mismatch",
                target_branch=target_branch,
                expected_remote_head=publish_guard.expected_remote_head,
                current_remote_head=current_remote_head,
                local_head=local_head,
                verified_head=publish_guard.verified_head,
                declared_publish_head=publish_guard.declared_publish_head,
                allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
                pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
                remote_readback_source=publish_guard.remote_readback_source,
                decision_inputs_complete=publish_guard.decision_inputs_complete,
                boundary_layer=boundary_layer,
            )
    return GitMutationPolicyResult(
        status="allow",
        command_class=COMMAND_CLASS_RTK_GIT_PUSH,
        reason_code="rtk_git_push_allowed",
        expected_remote_head=publish_guard.expected_remote_head if publish_guard else None,
        current_remote_head=current_remote_head,
        local_head=local_head,
        verified_head=publish_guard.verified_head if publish_guard else None,
        declared_publish_head=publish_guard.declared_publish_head if publish_guard else None,
        allowed_paths_gate_status=publish_guard.allowed_paths_gate_status if publish_guard else None,
        target_branch=target_branch,
        pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
        remote_readback_source=publish_guard.remote_readback_source,
        decision_inputs_complete=publish_guard.decision_inputs_complete,
        boundary_layer=boundary_layer,
    )


def _result_to_json(result: GitMutationPolicyResult | None) -> dict:
    """Serialize a `classify_rtk_git_mutation` result for the `--json` CLI mode
    (Issue #1408: consumed by `scripts/session-recording/codex-hook-adapter.mjs`
    so the PreToolUse publish-lane decision is not re-implemented in JS)."""
    if result is None:
        return {"status": "no_match"}
    return {
        "status": result.status,
        "command_class": result.command_class,
        "reason_code": result.reason_code,
        "suggested_command": result.suggested_command,
        "verification_command": result.verification_command,
        "expected_remote_head": result.expected_remote_head,
        "current_remote_head": result.current_remote_head,
        "local_head": result.local_head,
        "verified_head": result.verified_head,
        "declared_publish_head": result.declared_publish_head,
        "allowed_paths_gate_status": result.allowed_paths_gate_status,
        "target_branch": result.target_branch,
        "pr_number": result.pr_number,
        "remote_readback_source": result.remote_readback_source,
        "decision_inputs_complete": result.decision_inputs_complete,
        "required_decisions": list(result.required_decisions),
        "boundary_layer": result.boundary_layer,
        # Issue #1449 (PR #1479 OWNER review, P2 High): kept as a flat string
        # for backward compatibility with existing consumers, PLUS a
        # discriminated-union object (`kind` / `oid` / `error_category`) that
        # never conflates "the remote ref does not exist" with "we could not
        # tell" — the exact schema shape the OWNER review asked for.
        "remote_state": result.remote_state,
        "remote_state_detail": {
            "kind": result.remote_state,
            "oid": result.current_remote_head if result.remote_state == REMOTE_STATE_PRESENT else None,
            "error_category": (
                result.remote_state_error_category if result.remote_state == REMOTE_STATE_PROBE_ERROR else None
            ),
        },
    }


def _build_cli_parser():
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Classify a single command via classify_rtk_git_mutation and print the "
            "result as JSON. Non-`rtk git` commands and unrecognized shapes print "
            '{"status": "no_match"}. This CLI wrapper is the single reuse surface for '
            "non-Python callers (Issue #1408) — it does not re-implement policy logic."
        )
    )
    parser.add_argument("--command", required=True, help="the shell command string to classify")
    parser.add_argument("--cwd", required=True, help="working directory the command would run in")
    parser.add_argument(
        "--boundary-layer",
        default="worktree_scope_guard_denied",
        help="caller identifier embedded in safety-stop deny results (default: worktree_scope_guard_denied)",
    )
    parser.add_argument(
        "--no-require-active-branch-push",
        action="store_true",
        help="disable the require_active_branch_push check (default: enabled)",
    )
    return parser


def _main(argv: list[str] | None = None) -> int:
    import json as json_module
    import sys

    args = _build_cli_parser().parse_args(argv if argv is not None else sys.argv[1:])
    result = classify_rtk_git_mutation(
        args.command,
        cwd=args.cwd,
        require_active_branch_push=not args.no_require_active_branch_push,
        boundary_layer=args.boundary_layer,
    )
    print(json_module.dumps(_result_to_json(result)))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
