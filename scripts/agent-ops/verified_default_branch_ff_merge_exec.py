#!/usr/bin/env python3
# verified_default_branch_ff_merge_exec.py -- sole Codex-allowlisted executor
# for the default-branch fast-forward sync lane (Issue #1603).
#
# Sibling to verified_ff_merge_exec.py (Issue #1589 / #1609 fix_delta): that
# script verifies the ACTIVE branch's OWN live remote head (a caller-supplied
# 40-hex SHA); this script verifies the CANONICAL default branch's live
# identity via `git ls-remote --symref`, fetches it by object, and only then
# fast-forward merges -- exact `rtk git merge --ff-only origin/<candidate>`.
#
# .codex/rules/default.rules allowlists ONLY the exact invocation shape:
#   uv run --locked --no-sync python3 scripts/agent-ops/verified_default_branch_ff_merge_exec.py --candidate-branch NAME
#
# A bare `rtk git merge --ff-only origin/<branch>` command, with or without
# this shape, stays in the generic prompt bucket in Codex execpolicy (a
# prefix_rule allowlisting that shape directly would also match the broader
# `rtk git merge` prompt rule, and execpolicy takes the most severe decision
# across every matching rule -- forbidden over prompt over allow, not
# first-rule-wins -- so that shape can never resolve to allow on its own).
# This script is the only non-prompting path, and it performs its own
# independent authorization (active Issue resolved from LOOP_ISSUE_NUMBER,
# cwd bound to a linked worktree matching that issue number canonical branch
# shape) BEFORE calling execute_verified_default_branch_ff_merge_transaction,
# which re-verifies every claim against the live repository state (never
# trusts this script's own authorization verbatim). Mirrors the
# authorization ordering .claude/hooks/worktree_scope_guard.py performs for
# the Claude Code hook path.

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
    DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED,
    DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    execute_verified_default_branch_ff_merge_transaction,
)

_ISSUE_WORKTREE_BRANCH_RE = re.compile(r'^worktree-issue-([0-9]+)-[a-z0-9][a-z0-9-]{0,63}\Z')
_CANDIDATE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


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


def run(candidate_branch, cwd=None):
    resolved_cwd = os.path.realpath(cwd if cwd is not None else os.getcwd())

    if not _CANDIDATE_RE.fullmatch(candidate_branch or ''):
        return {'status': 'denied', 'reason_code': 'invalid_default_branch_candidate'}

    active_issue_number = os.environ.get('LOOP_ISSUE_NUMBER', '').strip()
    if not active_issue_number or not active_issue_number.isdigit():
        return {'status': 'denied', 'reason_code': 'issue_context_required'}

    active_branch = _current_branch(resolved_cwd)
    if not active_branch:
        return {'status': 'denied', 'reason_code': 'detached_head_not_supported'}
    branch_match = _ISSUE_WORKTREE_BRANCH_RE.fullmatch(active_branch)
    if not branch_match or branch_match.group(1) != active_issue_number:
        return {'status': 'denied', 'reason_code': 'branch_issue_number_mismatch'}

    transaction = execute_verified_default_branch_ff_merge_transaction(
        resolved_cwd,
        candidate_branch,
        expected_worktree_realpath=resolved_cwd,
        active_issue_number=active_issue_number,
        remote='origin',
        timeout=30,
    )
    return {
        'status': transaction.status,
        'reason_code': transaction.reason_code,
        'active_branch': transaction.active_branch,
        'verified_local_head': transaction.verified_local_head,
        'candidate_default_branch': transaction.candidate_default_branch,
        'live_default_branch_oid': transaction.live_default_branch_oid,
        'merge_returncode': transaction.merge_returncode,
        'post_head': transaction.post_head,
    }


def _build_cli_parser():
    parser = argparse.ArgumentParser(
        description=(
            'Authorize and execute a verified fast-forward git merge --ff-only '
            'from the canonical default branch live head, for the active linked '
            'issue worktree (Issue #1603).'
        )
    )
    parser.add_argument(
        '--candidate-branch',
        required=True,
        help='candidate default branch name (e.g. main) to verify and fast-forward to',
    )
    return parser


def main(argv=None):
    args = _build_cli_parser().parse_args(argv if argv is not None else sys.argv[1:])
    result = run(args.candidate_branch)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    sys.stdout.write(chr(10))
    ok = result.get('status') in (
        DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED,
        DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    )
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
