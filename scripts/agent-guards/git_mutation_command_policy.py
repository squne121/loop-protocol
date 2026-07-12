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


ALLOWED_RTK_GIT_SUBCOMMANDS = frozenset({"add", "commit", "push"})
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
ALLOWED_ALLOWED_PATHS_GATE_STATUSES = frozenset({"ok", "fail_closed", "indeterminate"})
# Issue #1408 iteration-2 (P2): canonical push destination identity. New
# branch initial publish (remote ref absent) is explicitly out of scope for
# this bridge — see Issue #1449.
CANONICAL_REPO_IDENTITY_DEFAULT = "squne121/loop-protocol"
# Issue #1408 iteration-2 (P2): policy-level default branch destination
# guard, independent of #360's hook-level destination guard.
DEFAULT_BRANCH_NAMES = frozenset({"main", "master", "trunk"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
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


def classify_remote_branch_state(cwd: str, remote: str, branch: str, timeout: int = 10) -> tuple[str, str | None]:
    """Return `(state, oid)` for `branch` on `remote`, classified into the
    exclusive 3-state vocabulary (Issue #1449 AC1):

      - `present`: the ref exists on the remote; `oid` is its live SHA.
      - `absent`: `git ls-remote --refs --exit-code` confirmed (returncode 2)
        the ref does not exist on the remote.
      - `probe_error`: timeout, auth failure, network failure, malformed
        output, or any other non-{0,2} returncode — never folded into
        `absent`, always fail-closed at the call site.

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
    except (OSError, subprocess.TimeoutExpired):
        return REMOTE_STATE_PROBE_ERROR, None
    if result.returncode == 2:
        return REMOTE_STATE_ABSENT, None
    if result.returncode != 0:
        return REMOTE_STATE_PROBE_ERROR, None
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    oid = first.split()[0] if first else ""
    if _SHA_RE.fullmatch(oid.lower()):
        return REMOTE_STATE_PRESENT, oid.lower()
    # Non-empty, non-SHA-shaped stdout on returncode 0 is malformed output —
    # fail-closed, not `present` and not `absent`.
    return REMOTE_STATE_PROBE_ERROR, None


def build_initial_branch_create_argv(remote: str, target_branch: str) -> list[str]:
    """Return the fully-qualified empty-expect `--force-with-lease` argv for
    the initial_branch_create lane (Issue #1449 AC2):

      git push --force-with-lease=refs/heads/<branch>: origin HEAD:refs/heads/<branch>

    A single-token `--force-with-lease=refs/heads/<branch>:` with nothing
    after the trailing colon is the git-native "the ref MUST NOT already
    exist" empty-expect form — this is the sole primitive this lane executes.
    Returned as an argv list (never a shell string) so callers execute it via
    `subprocess.run(argv, shell=False)`, never shell-string concatenation."""
    return [
        "git",
        "push",
        f"--force-with-lease=refs/heads/{target_branch}:",
        remote,
        f"HEAD:refs/heads/{target_branch}",
    ]


def validate_initial_branch_create_argv(args: list[str], target_branch: str, remote: str = "origin") -> tuple[bool, str]:
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
    looser match."""
    if target_branch in DEFAULT_BRANCH_NAMES:
        return False, "push_target_is_default_branch"
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", target_branch):
        return False, "invalid_target_branch"
    expected = build_initial_branch_create_argv(remote, target_branch)[2:]
    if args == expected:
        return True, "initial_branch_create_argv_valid"
    return False, "initial_branch_create_argv_invalid"


def execute_initial_branch_create_push(
    cwd: str, remote: str, target_branch: str, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Execute the initial_branch_create lease push. Always invoked with an
    argv list (never a shell string) and `shell=False` (the `subprocess.run`
    default) — Issue #1449 AC2/AC12: the remote-write execution must never be
    assembled via shell-string concatenation."""
    argv = build_initial_branch_create_argv(remote, target_branch)
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
    safety stop (`matched=False`) — never treated as success."""
    state, oid = classify_remote_branch_state(cwd, remote, target_branch, timeout=timeout)
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


def _canonical_repo_identity() -> str:
    return os.environ.get("LOOP_CANONICAL_REPO_IDENTITY", "").strip() or CANONICAL_REPO_IDENTITY_DEFAULT


def _canonical_repo_url_pattern() -> re.Pattern[str]:
    override = os.environ.get("LOOP_CANONICAL_REPO_URL_PATTERN", "").strip()
    if override:
        return re.compile(override)
    identity = re.escape(_canonical_repo_identity())
    return re.compile(_CANONICAL_REPO_URL_TEMPLATE.format(identity=identity))


def _origin_push_urls(cwd: str) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "--push", "--all", "origin"],
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
    expected_remote_head: str,
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
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if publish_guard.allowed_paths_gate_status != "ok":
        return _publish_safety_stop_result(
            reason_code="allowed_paths_gate_not_ok",
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
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if local_head != publish_guard.declared_publish_head or local_head != publish_guard.verified_head:
        return _publish_safety_stop_result(
            reason_code="local_head_mismatch",
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
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    if not _origin_push_urls_match_canonical_repo(cwd):
        return _publish_safety_stop_result(
            reason_code="origin_remote_identity_mismatch",
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
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        )
    remote_state, remote_oid = classify_remote_branch_state(cwd, "origin", target_branch)
    lane_status, lane_reason = evaluate_initial_branch_create_lane(
        remote_state=remote_state,
        local_head=local_head or "",
        declared_publish_head=publish_guard.declared_publish_head,
        verified_head=publish_guard.verified_head,
        allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
        decision_inputs_complete=publish_guard.decision_inputs_complete,
        remote_readback_source=publish_guard.remote_readback_source,
    )
    if lane_status != "allow":
        return _publish_safety_stop_result(
            reason_code=lane_reason,
            target_branch=target_branch,
            expected_remote_head=publish_guard.expected_remote_head,
            current_remote_head=remote_oid,
            local_head=local_head,
            verified_head=publish_guard.verified_head,
            declared_publish_head=publish_guard.declared_publish_head,
            allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
            pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
            remote_readback_source=publish_guard.remote_readback_source,
            decision_inputs_complete=publish_guard.decision_inputs_complete,
            boundary_layer=boundary_layer,
            command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
            remote_state=remote_state,
        )
    return GitMutationPolicyResult(
        status="allow",
        command_class=COMMAND_CLASS_RTK_GIT_INITIAL_BRANCH_CREATE,
        reason_code="initial_branch_create_allowed",
        expected_remote_head=publish_guard.expected_remote_head,
        current_remote_head=remote_oid,
        local_head=local_head,
        verified_head=publish_guard.verified_head,
        declared_publish_head=publish_guard.declared_publish_head,
        allowed_paths_gate_status=publish_guard.allowed_paths_gate_status,
        target_branch=target_branch,
        pr_number=os.environ.get("LOOP_PR_NUMBER", ""),
        remote_readback_source=publish_guard.remote_readback_source,
        decision_inputs_complete=publish_guard.decision_inputs_complete,
        boundary_layer=boundary_layer,
        remote_state=remote_state,
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
        "remote_state": result.remote_state,
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
