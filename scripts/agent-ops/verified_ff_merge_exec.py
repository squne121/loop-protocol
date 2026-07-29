#!/usr/bin/env python3
# verified_ff_merge_exec.py -- sole Codex-allowlisted executor for the
# verified fast-forward merge lane (Issue 1589 / 1609 fix_delta P1 Blocker).
#
# .codex/rules/default.rules allowlists ONLY the exact invocation shape:
#   uv run --locked --no-sync python3 scripts/agent-ops/verified_ff_merge_exec.py --target-sha SHA
#
# A bare rtk git merge --ff-only SHA command, with or without this shape,
# stays in the generic prompt bucket in Codex execpolicy (a prefix_rule
# allowlisting that shape directly would also match the broader rtk git
# merge prompt rule, and execpolicy takes the most severe decision across
# every matching rule, forbidden over prompt over allow, not first-rule-wins,
# so that shape can never resolve to allow on its own). This script is the
# only non-prompting path, and it performs its own independent
# authorization (Issue resolved from an explicit argument or the canonical
# linked-worktree branch/cwd identity; ambient process state is not authority)
# BEFORE calling execute_verified_ff_merge_transaction, which re-verifies
# every claim against the live repository state (never trusts this
# script own authorization verbatim). Mirrors the authorization ordering
# .claude/hooks/worktree_scope_guard.py performs for the Claude Code hook
# path (P0 Blocker fix).

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_GUARDS_DIR = Path(__file__).resolve().parent.parent / 'agent-guards'
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

from git_mutation_command_policy import (  # noqa: E402
    MERGE_STATUS_MERGED_AND_VERIFIED,
    MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    execute_verified_ff_merge_transaction,
)

_AGENT_OPS_DIR = Path(__file__).resolve().parent
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))
import worktree_catalog as _wcat  # noqa: E402

_ISSUE_WORKTREE_BRANCH_RE = re.compile(r'^worktree-issue-([0-9]+)-[a-z0-9][a-z0-9-]{0,63}\Z')
_SHA_RE = re.compile(r'^[0-9a-f]{40}\Z')


def _current_branch(cwd):
    try:
        result = subprocess.run(
            ['git', 'branch', '--show-current'],
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


def _resolve_issue_worktree(cwd_real, explicit_issue_number=None):
    active_branch = _current_branch(cwd_real)
    if not active_branch:
        return None, None, 'detached_head_not_supported'
    branch_match = _ISSUE_WORKTREE_BRANCH_RE.fullmatch(active_branch)
    if not branch_match:
        return None, None, 'active_branch_not_issue_worktree_branch'
    branch_issue = branch_match.group(1)
    if explicit_issue_number is not None:
        explicit = str(explicit_issue_number).strip()
        if not explicit.isdigit():
            return None, None, 'invalid_issue_number'
        if explicit != branch_issue:
            return None, None, 'branch_issue_number_mismatch'

    catalog = _wcat.list_worktrees(cwd_real)
    if not catalog:
        return None, None, 'git_worktree_catalog_unavailable'
    root_realpath = catalog[0].get('worktree_realpath')
    matches = _wcat.select_issue_worktrees(catalog, branch_issue, root_realpath)
    if len(matches) != 1:
        return None, None, 'zero_matching_worktrees' if not matches else 'multiple_matching_worktrees'
    expected_realpath = matches[0].get('worktree_realpath')
    if expected_realpath == root_realpath:
        return None, None, 'expected_worktree_is_root_checkout'
    if cwd_real != expected_realpath:
        return None, None, 'cwd_not_expected_worktree'
    return branch_issue, expected_realpath, None


def run(target_sha, cwd=None, issue_number=None):
    resolved_cwd = os.path.realpath(cwd if cwd is not None else os.getcwd())

    if not _SHA_RE.fullmatch(target_sha or ''):
        return {'status': 'denied', 'reason_code': 'invalid_target_sha'}

    active_issue_number, expected_realpath, bind_reason = _resolve_issue_worktree(
        resolved_cwd, issue_number
    )
    if active_issue_number is None:
        return {'status': 'denied', 'reason_code': bind_reason}

    transaction = execute_verified_ff_merge_transaction(
        resolved_cwd,
        target_sha,
        expected_worktree_realpath=expected_realpath,
        active_issue_number=active_issue_number,
        remote='origin',
        timeout=30,
    )
    return {
        'status': transaction.status,
        'reason_code': transaction.reason_code,
        'active_branch': transaction.active_branch,
        'verified_local_head': transaction.verified_local_head,
        'target_sha': transaction.target_sha,
        'live_remote_head': transaction.live_remote_head,
        'merge_returncode': transaction.merge_returncode,
        'post_head': transaction.post_head,
    }


def _build_cli_parser():
    parser = argparse.ArgumentParser(
        description=(
            'Authorize and execute a verified fast-forward git merge --ff-only '
            'for the active linked issue worktree (Issue 1589 / 1609 fix_delta).'
        )
    )
    parser.add_argument('--target-sha', required=True, help='40-hex commit SHA to fast-forward to')
    parser.add_argument(
        '--issue-number',
        help='optional explicit Issue number; must equal the canonical linked-worktree branch identity',
    )
    return parser


def main(argv=None):
    args = _build_cli_parser().parse_args(argv if argv is not None else sys.argv[1:])
    result = run(args.target_sha, issue_number=args.issue_number)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    sys.stdout.write(chr(10))
    ok = result.get('status') in (MERGE_STATUS_MERGED_AND_VERIFIED, MERGE_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
