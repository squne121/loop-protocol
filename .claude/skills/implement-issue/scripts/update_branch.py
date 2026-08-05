#!/usr/bin/env python3
"""Deterministic update-branch REST wrapper for implementation-worker."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable


UPDATE_METHOD = 'merge_only'
REASON_EXPECTED_HEAD_SHA_MISSING = 'expected_head_sha_missing'
REASON_EXPECTED_HEAD_SHA_MISMATCH = 'expected_head_sha_mismatch'
REASON_PERMISSION_DENIED = 'permission_denied'
REASON_SECONDARY_RATE_LIMIT = 'secondary_rate_limit'
REASON_PRIMARY_RATE_LIMIT = 'primary_rate_limit'
REASON_VALIDATION_FAILED = 'validation_failed'
REASON_HEAD_UNCHANGED = 'head_unchanged_after_accepted'
REASON_TRANSPORT_ERROR = 'transport_error'
REASON_UNKNOWN_HTTP_STATUS = 'unknown_http_status'
REASON_UNEXPECTED_HEAD_CHANGE = 'unexpected_head_change'
RERUN_REASON = 'pr_head_changed_by_update_branch'
RERUN_REASON_UNEXPECTED = 'unexpected_head_change_after_update_branch'

# Secondary-rate-limit / abuse-detection message markers (#1429 iteration-1
# P2). These are message-body signals, distinct from the *primary* REST
# rate limit, which is instead recognized from the `x-ratelimit-remaining`
# header via _classify_rate_limit(). A generic "rate limit" substring alone
# is intentionally NOT included here, because GitHub's primary rate-limit
# error body also contains the words "rate limit" and would otherwise be
# misclassified as secondary.
SECONDARY_RATE_LIMIT_MARKERS = (
    'secondary rate limit',
    'you have exceeded a secondary rate limit',
    'retry later',
    'abuse detection',
)

# Canonical repository binding. execute_update_branch() refuses to call the
# GitHub API for any other repository (input validation, #1429).
CANONICAL_REPO = 'squne121/loop-protocol'

# Known production callers of update_branch.py (#1429 canonical executor
# unification). 'manual' covers ad-hoc human-invoked runs from the default
# branch; every automated production caller must be added explicitly.
#
# NOTE (#1429 iteration-1 P2): this allowlist is a typo/misconfiguration
# catcher, not an authorization or provenance mechanism. --caller is a
# free-form label supplied by the caller itself and is not independently
# verified against the actual invoking process/identity. Do not treat a
# known caller label as proof of who or what invoked this script.
KNOWN_CALLER_LABELS = frozenset({'impl-review-loop.step-5', 'manual'})
# Backward-compatible alias (kept in case any external reference imports the
# old name); carries the same non-authorization caveat as above.
ALLOWED_CALLERS = KNOWN_CALLER_LABELS

# Full-length hexadecimal commit SHA (git SHA-1, 40 hex chars).
_FULL_SHA_RE = re.compile(r'^[0-9a-f]{40}$')

# Production poll bounds. These are intentionally not exposed as CLI flags
# so a caller cannot relax them (#1429 AC8).
PRODUCTION_POLL_MAX = 12
PRODUCTION_POLL_INTERVAL = 5.0

# GitHub "compare" status values that mean `ancestor_sha` is contained in
# the history of `descendant_sha` (i.e. ancestor_sha is an ancestor of, or
# identical to, descendant_sha).
_COMPARE_ANCESTOR_STATUSES = frozenset({'identical', 'ahead'})


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ''


@dataclass(frozen=True)
class HttpResponse:
    status: int | None
    headers: dict[str, str]
    body: str


@dataclass(frozen=True)
class UpdateBranchRequest:
    pr_number: int
    repo: str
    expected_head_sha: str
    update_method: str = UPDATE_METHOD
    caller: str = 'manual'


GhRunner = Callable[[list[str]], CommandResult]
SleepFn = Callable[[float], None]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr-number', required=True, type=int)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--expected-head-sha', required=True)
    parser.add_argument('--caller', default='manual')
    parser.add_argument('--update-method', default=UPDATE_METHOD)
    # NOTE: poll count/interval are intentionally NOT exposed as CLI flags.
    # Production invocation always uses PRODUCTION_POLL_MAX /
    # PRODUCTION_POLL_INTERVAL so a caller cannot relax the bounded retry
    # (#1429 AC8). Tests exercise poll bounds directly via
    # execute_update_branch(poll_max=..., poll_interval=...).
    return parser.parse_args(argv)


def run_gh(args: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            ['gh', *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = str(exc)
        if exc.stderr:
            stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors='ignore')
        stdout = ''
        if exc.stdout:
            stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(errors='ignore')
        return CommandResult(returncode=124, stdout=stdout, stderr=stderr or 'gh command timed out')
    except (FileNotFoundError, OSError) as exc:
        return CommandResult(returncode=127, stdout='', stderr=str(exc))
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _base_result(request: UpdateBranchRequest) -> dict[str, object]:
    return {
        'status': 'failed',
        'reason_code': None,
        'update_method': request.update_method,
        'http_status': None,
        'before_head_sha': None,
        'after_head_sha': None,
        'new_head_sha': None,
        'before_base_sha': None,
        'poll_attempts': 0,
        'rerun_required': {
            'verification': False,
            'pr_review': False,
            'reason': None,
        },
        'permission_diagnostics': None,
        'rate_limit_diagnostics': None,
        'error_body': None,
        'errors': [],
    }


def _json_loads(raw: str) -> object | None:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_http_response(raw: str) -> HttpResponse:
    lines = raw.splitlines()
    status = None
    headers: dict[str, str] = {}
    body_lines: list[str] = []
    if lines:
        match = re.match(r'^HTTP/\S+\s+(\d{3})', lines[0])
        if match:
            status = int(match.group(1))
            idx = 1
            while idx < len(lines):
                line = lines[idx]
                idx += 1
                if line.strip() == '':
                    break
                if ':' not in line:
                    continue
                name, value = line.split(':', 1)
                headers[name.strip().lower()] = value.strip()
            body_lines = lines[idx:]
    body = '\n'.join(body_lines).strip()
    return HttpResponse(status=status, headers=headers, body=body)


def _get_current_head_sha(
    request: UpdateBranchRequest,
    gh_runner: GhRunner,
) -> tuple[str | None, str | None]:
    result = gh_runner(
        [
            'pr',
            'view',
            str(request.pr_number),
            '--repo',
            request.repo,
            '--json',
            'headRefOid',
            '--jq',
            '.headRefOid',
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or 'gh pr view failed'
        return None, detail
    head = result.stdout.strip()
    if not head:
        return None, 'headRefOid was empty'
    return head, None


def _get_current_base_sha(
    request: UpdateBranchRequest,
    gh_runner: GhRunner,
) -> tuple[str | None, str | None]:
    """Fetch the PR's current base-branch head SHA (#1429 iteration-1 P1-2).

    Used as one of the postcondition topology inputs: the base SHA that was
    current immediately before the update-branch call must be an ancestor
    of the post-update head for the update to be verified as a genuine
    merge_only update rather than an unrelated head change.
    """
    result = gh_runner(
        [
            'pr',
            'view',
            str(request.pr_number),
            '--repo',
            request.repo,
            '--json',
            'baseRefOid',
            '--jq',
            '.baseRefOid',
        ]
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or 'gh pr view (baseRefOid) failed'
        return None, detail
    base = result.stdout.strip()
    if not base:
        return None, 'baseRefOid was empty'
    return base, None


def _get_permission_diagnostics(
    request: UpdateBranchRequest,
    gh_runner: GhRunner,
) -> dict[str, object]:
    auth_actor = ''
    auth = gh_runner(['api', 'user', '--jq', '.login'])
    if auth.returncode == 0:
        auth_actor = auth.stdout.strip()

    pr_view = gh_runner(
        [
            'pr',
            'view',
            str(request.pr_number),
            '--repo',
            request.repo,
            '--json',
            'headRepository,baseRepository,maintainerCanModify,isCrossRepository',
        ]
    )
    payload = _json_loads(pr_view.stdout) if pr_view.returncode == 0 else None
    head_repo = request.repo
    base_repo = request.repo
    maintainer_can_modify = False
    fork_pr = False
    if isinstance(payload, dict):
        head_repo = (payload.get('headRepository', {}) or {}).get('nameWithOwner', request.repo)
        base_repo = (payload.get('baseRepository', {}) or {}).get('nameWithOwner', request.repo)
        maintainer_can_modify = bool(payload.get('maintainerCanModify', False))
        fork_pr = bool(payload.get('isCrossRepository', False))

    return {
        'auth_actor': auth_actor,
        'head_repo': head_repo,
        'base_repo': base_repo,
        'fork_pr': fork_pr,
        'maintainer_can_modify': maintainer_can_modify,
        'required_permissions': 'pull_requests:write, contents:write_on_head_repository_when_github_app',
    }


def _parse_optional_int(raw: str | None) -> int | None:
    if raw is None or raw == '':
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _rate_limit_diagnostics(headers: dict[str, str]) -> dict[str, int | None]:
    return {
        'retry_after_seconds': _parse_optional_int(headers.get('retry-after')),
        'x_ratelimit_remaining': _parse_optional_int(headers.get('x-ratelimit-remaining')),
        'x_ratelimit_reset': _parse_optional_int(headers.get('x-ratelimit-reset')),
    }


def _classify_rate_limit(http_status: int | None, headers: dict[str, str], body: str) -> str | None:
    """Classify a response as primary vs secondary rate limiting, or None.

    Primary rate limit: the response carries `x-ratelimit-remaining: 0`
    (the authoritative REST primary-rate-limit signal) on a status that
    GitHub uses for rate limiting (403/429).

    Secondary rate limit: the response body contains one of the
    SECONDARY_RATE_LIMIT_MARKERS message signals (abuse detection /
    "secondary rate limit" / retry-later wording), independent of the
    x-ratelimit-remaining header (#1429 iteration-1 P2 — previously a bare
    "rate limit" substring conflated the two).
    """
    if http_status not in {403, 422, 429}:
        return None
    remaining = _parse_optional_int(headers.get('x-ratelimit-remaining'))
    if http_status in {403, 429} and remaining == 0:
        return REASON_PRIMARY_RATE_LIMIT
    lowered = body.lower()
    if any(marker in lowered for marker in SECONDARY_RATE_LIMIT_MARKERS):
        return REASON_SECONDARY_RATE_LIMIT
    return None


def _validate_request(request: UpdateBranchRequest) -> str | None:
    """Self-contained input validation, executed before any API call.

    Returns an error message when the request violates the executor
    contract (#1429), or None when the request passes all boundary checks.
    Each violation is fail-closed: execute_update_branch() must not call
    the GitHub API when this returns a non-None message.
    """
    if request.pr_number <= 0:
        return f'pr_number must be a positive integer, got {request.pr_number!r}'
    if request.repo != CANONICAL_REPO:
        return f'repo must be {CANONICAL_REPO!r}, got {request.repo!r}'
    if request.caller not in KNOWN_CALLER_LABELS:
        allowed = ', '.join(sorted(KNOWN_CALLER_LABELS))
        return f'caller {request.caller!r} is not a known caller label ({allowed})'
    expected_head_sha = request.expected_head_sha.strip()
    if expected_head_sha and not _FULL_SHA_RE.match(expected_head_sha):
        return 'expected_head_sha must be a full-length hexadecimal commit SHA'
    return None


def _is_ancestor(
    request: UpdateBranchRequest,
    ancestor_sha: str,
    descendant_sha: str,
    gh_runner: GhRunner,
) -> bool | None:
    """Return True/False when ancestry could be determined via the GitHub
    compare API, or None when it could not be determined (transport error,
    unknown commit, malformed response) — callers must treat None as
    "not verified" (fail closed), not as True.
    """
    if ancestor_sha == descendant_sha:
        return True
    if not (_FULL_SHA_RE.match(ancestor_sha) and _FULL_SHA_RE.match(descendant_sha)):
        return None
    result = gh_runner(
        [
            'api',
            f'repos/{request.repo}/compare/{ancestor_sha}...{descendant_sha}',
            '--jq',
            '.status',
        ]
    )
    if result.returncode != 0:
        return None
    status = result.stdout.strip().lower()
    if not status:
        return None
    return status in _COMPARE_ANCESTOR_STATUSES


def _verify_post_update_head(
    request: UpdateBranchRequest,
    expected_head_sha: str,
    before_base_sha: str | None,
    new_head_sha: str,
    gh_runner: GhRunner,
) -> bool | None:
    """Verify that a post-202-poll head change is a genuine merge_only
    update-branch result rather than an unrelated/concurrent head change
    (#1429 iteration-1 P1-2).

    Returns True only when all of the following hold:
      (a) new_head_sha is a full-length commit SHA
      (b) new_head_sha != expected_head_sha
      (c) expected_head_sha is an ancestor of new_head_sha
      (d) before_base_sha (when known) is an ancestor of new_head_sha

    Returns False when a check definitively fails, or None when ancestry
    could not be determined. Both False and None must be treated as
    "not verified" by the caller (fail closed).
    """
    if not _FULL_SHA_RE.match(new_head_sha):
        return False
    if new_head_sha == expected_head_sha:
        return False

    expected_is_ancestor = _is_ancestor(request, expected_head_sha, new_head_sha, gh_runner)
    if expected_is_ancestor is not True:
        return expected_is_ancestor

    if before_base_sha:
        if not _FULL_SHA_RE.match(before_base_sha):
            return None
        base_is_ancestor = _is_ancestor(request, before_base_sha, new_head_sha, gh_runner)
        if base_is_ancestor is not True:
            return base_is_ancestor

    return True


def execute_update_branch(
    request: UpdateBranchRequest,
    *,
    gh_runner: GhRunner = run_gh,
    sleep_fn: SleepFn = time.sleep,
    poll_max: int = PRODUCTION_POLL_MAX,
    poll_interval: float = PRODUCTION_POLL_INTERVAL,
) -> dict[str, object]:
    result = _base_result(request)

    if request.update_method != UPDATE_METHOD:
        result['reason_code'] = REASON_VALIDATION_FAILED
        result['error_body'] = f'Unsupported update_method={request.update_method!r}; merge_only only.'
        result['errors'].append('update_method must be merge_only')
        return result

    validation_error = _validate_request(request)
    if validation_error:
        result['reason_code'] = REASON_VALIDATION_FAILED
        result['error_body'] = validation_error
        result['errors'].append(validation_error)
        return result

    expected_head_sha = request.expected_head_sha.strip()
    if not expected_head_sha:
        result['status'] = 'blocked'
        result['reason_code'] = REASON_EXPECTED_HEAD_SHA_MISSING
        result['errors'].append('expected_head_sha is required')
        return result

    current_head_sha, preflight_error = _get_current_head_sha(request, gh_runner)
    if preflight_error:
        result['reason_code'] = REASON_TRANSPORT_ERROR
        result['error_body'] = preflight_error
        result['errors'].append(preflight_error)
        return result

    result['before_head_sha'] = current_head_sha
    if current_head_sha != expected_head_sha:
        result['status'] = 'blocked'
        result['reason_code'] = REASON_EXPECTED_HEAD_SHA_MISMATCH
        result['after_head_sha'] = current_head_sha
        result['error_body'] = 'current PR head did not match expected_head_sha; API call skipped'
        result['errors'].append('current PR head mismatch')
        return result

    update_response = gh_runner(
        [
            'api',
            '-i',
            '-X',
            'PUT',
            f'repos/{request.repo}/pulls/{request.pr_number}/update-branch',
            '-H',
            'Accept: application/vnd.github+json',
            '-H',
            'X-GitHub-Api-Version: 2022-11-28',
            '-f',
            f'expected_head_sha={expected_head_sha}',
        ]
    )

    raw_response = update_response.stdout if update_response.stdout else update_response.stderr
    http = _extract_http_response(raw_response)
    result['http_status'] = http.status
    result['error_body'] = http.body or None

    if update_response.returncode != 0 and http.status is None:
        result['reason_code'] = REASON_TRANSPORT_ERROR
        result['errors'].append(update_response.stderr.strip() or 'gh api failed')
        return result

    if http.status == 202:
        before_base_sha, base_error = _get_current_base_sha(request, gh_runner)
        if base_error:
            # Not fatal on its own: postcondition verification degrades to
            # "expected_head_sha ancestry only" when base SHA is unknown.
            # _verify_post_update_head() treats a None before_base_sha as
            # "skip the base-ancestor check", which is intentionally
            # permissive here because the base fetch is an auxiliary check,
            # not the primary race guard.
            before_base_sha = None
        result['before_base_sha'] = before_base_sha

        for attempt in range(1, poll_max + 1):
            result['poll_attempts'] = attempt
            head_sha, poll_error = _get_current_head_sha(request, gh_runner)
            if poll_error:
                result['reason_code'] = REASON_TRANSPORT_ERROR
                result['errors'].append(poll_error)
                return result
            if head_sha and head_sha != expected_head_sha:
                verified = _verify_post_update_head(
                    request,
                    expected_head_sha,
                    before_base_sha,
                    head_sha,
                    gh_runner,
                )
                if verified is True:
                    result['status'] = 'ok'
                    result['after_head_sha'] = head_sha
                    result['new_head_sha'] = head_sha
                    result['rerun_required'] = {
                        'verification': True,
                        'pr_review': True,
                        'reason': RERUN_REASON,
                    }
                    return result
                # verified is False or None: the head changed, but we could
                # not confirm it is the genuine merge_only update-branch
                # result (malformed SHA, unrelated commit, or ancestry
                # undeterminable). Fail closed instead of reporting ok.
                result['status'] = 'blocked'
                result['reason_code'] = REASON_UNEXPECTED_HEAD_CHANGE
                result['after_head_sha'] = head_sha
                result['error_body'] = (
                    'PR head changed during poll but could not be verified as the '
                    'genuine merge_only update-branch result'
                )
                result['errors'].append('unexpected head change after accepted update-branch request')
                result['rerun_required'] = {
                    'verification': True,
                    'pr_review': True,
                    'reason': RERUN_REASON_UNEXPECTED,
                }
                return result
            if attempt < poll_max:
                sleep_fn(poll_interval)

        result['reason_code'] = REASON_HEAD_UNCHANGED
        result['after_head_sha'] = expected_head_sha
        result['error_body'] = 'head did not change after accepted update-branch request'
        result['errors'].append('head unchanged after accepted')
        return result

    rate_limit_class = _classify_rate_limit(http.status, http.headers, http.body)
    if rate_limit_class is not None:
        result['reason_code'] = rate_limit_class
        result['rate_limit_diagnostics'] = _rate_limit_diagnostics(http.headers)
        result['errors'].append(rate_limit_class.replace('_', ' '))
        return result

    if http.status == 403:
        result['status'] = 'permission_blocked'
        result['reason_code'] = REASON_PERMISSION_DENIED
        result['permission_diagnostics'] = _get_permission_diagnostics(request, gh_runner)
        result['errors'].append('permission denied')
        return result

    if http.status == 422:
        lowered = http.body.lower()
        if 'expected_head_sha' in lowered or 'head sha' in lowered:
            latest_head_sha, latest_head_error = _get_current_head_sha(request, gh_runner)
            result['status'] = 'blocked'
            result['reason_code'] = REASON_EXPECTED_HEAD_SHA_MISMATCH
            result['after_head_sha'] = latest_head_sha
            if latest_head_error:
                result['errors'].append(latest_head_error)
            result['errors'].append('expected_head_sha mismatch')
            return result
        result['reason_code'] = REASON_VALIDATION_FAILED
        result['errors'].append('validation failed')
        return result

    if http.status == 429:
        result['reason_code'] = REASON_SECONDARY_RATE_LIMIT
        result['rate_limit_diagnostics'] = _rate_limit_diagnostics(http.headers)
        result['errors'].append('secondary rate limit')
        return result

    result['reason_code'] = REASON_UNKNOWN_HTTP_STATUS
    result['errors'].append(f'unexpected HTTP status: {http.status}')
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = UpdateBranchRequest(
        pr_number=args.pr_number,
        repo=args.repo,
        expected_head_sha=args.expected_head_sha,
        update_method=args.update_method,
        caller=args.caller,
    )
    result = execute_update_branch(
        request,
        poll_max=PRODUCTION_POLL_MAX,
        poll_interval=PRODUCTION_POLL_INTERVAL,
    )
    json.dump(result, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write('\n')
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
