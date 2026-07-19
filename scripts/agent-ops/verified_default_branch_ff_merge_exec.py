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
# Issue #1603 iteration-2 OWNER adversarial review (P1-1 / P2-7): this
# script performs its OWN independent authorization BEFORE calling
# execute_verified_default_branch_ff_merge_transaction, and does so via the
# repository's SSOT worktree catalog (`scripts/agent-ops/worktree_catalog.py`
# `select_issue_worktrees`), not a tautological `cwd == cwd` check. It ALSO
# validates its own argv is the EXACT `["--candidate-branch", <value>]` shape
# before argparse ever runs -- because `.codex/rules/default.rules` is a
# PREFIX rule (Codex execpolicy has no exact-shape / no-trailing-token
# primitive), a duplicate `--candidate-branch` flag, a `--flag=value` form,
# extra positionals, or a trailing token would otherwise still match the
# rule's `allow` prefix. The real enforcement for those shapes is here, not
# in the static rule.

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_AGENT_OPS_DIR = Path(__file__).resolve().parent
if str(_AGENT_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_OPS_DIR))

_GUARDS_DIR = _AGENT_OPS_DIR.parent / 'agent-guards'
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import worktree_catalog as _wcat  # noqa: E402
from git_mutation_command_policy import (  # noqa: E402
    DEFAULT_BRANCH_FF_STATUS_EXECUTION_ERROR_MERGED_AND_VERIFIED,
    DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED,
    DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
    execute_verified_default_branch_ff_merge_transaction,
)

_CANDIDATE_RE = re.compile(r'^(?!-)(?!.*\.\.)[!-~]+\Z')


def _resolve_authorized_worktree(cwd_real, issue_number):
    """Issue #1603 iteration-2 P1-1: resolve the ONE authorized linked
    worktree for `issue_number` via the repository's shared worktree catalog
    SSOT (`worktree_catalog.select_issue_worktrees`) -- exactly one linked
    worktree whose branch AND path basename both match the canonical shape,
    whose realpath is NOT the primary/root checkout, and whose realpath
    equals `cwd_real`. Returns `(expected_realpath, reason_code)` where
    `reason_code` is None on success."""
    catalog = _wcat.list_worktrees(cwd_real)
    if not catalog:
        return None, 'git_worktree_catalog_unavailable'
    # `git worktree list` always lists the primary/root checkout first.
    root_realpath = catalog[0].get('worktree_realpath')
    matches = _wcat.select_issue_worktrees(catalog, issue_number, root_realpath)
    if len(matches) == 0:
        return None, 'zero_matching_worktrees'
    if len(matches) > 1:
        return None, 'multiple_matching_worktrees'
    entry = matches[0]
    expected_realpath = entry.get('worktree_realpath')
    if not expected_realpath:
        return None, 'zero_matching_worktrees'
    if expected_realpath == root_realpath:
        return None, 'expected_worktree_is_root_checkout'
    if cwd_real != expected_realpath:
        return None, 'cwd_not_expected_worktree'
    return expected_realpath, None


def run(candidate_branch, cwd=None):
    resolved_cwd = os.path.realpath(cwd if cwd is not None else os.getcwd())

    if not _CANDIDATE_RE.fullmatch(candidate_branch or ''):
        return {'status': 'denied', 'reason_code': 'invalid_default_branch_candidate'}

    active_issue_number = os.environ.get('LOOP_ISSUE_NUMBER', '').strip()
    if not active_issue_number or not active_issue_number.isdigit():
        return {'status': 'denied', 'reason_code': 'issue_context_required'}

    expected_realpath, bind_reason = _resolve_authorized_worktree(resolved_cwd, active_issue_number)
    if expected_realpath is None:
        return {'status': 'denied', 'reason_code': bind_reason}

    transaction = execute_verified_default_branch_ff_merge_transaction(
        resolved_cwd,
        candidate_branch,
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
        'candidate_default_branch': transaction.candidate_default_branch,
        'live_default_branch_oid': transaction.live_default_branch_oid,
        'merge_returncode': transaction.merge_returncode,
        'post_head': transaction.post_head,
        'trusted_git_dir': transaction.trusted_git_dir,
        'trusted_git_common_dir': transaction.trusted_git_common_dir,
        'trusted_worktree_toplevel': transaction.trusted_worktree_toplevel,
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


def _validate_exact_invocation_argv(raw_argv):
    """Issue #1603 iteration-2 P2-7: require the argv to be EXACTLY
    `["--candidate-branch", <value>]` -- two tokens, no duplicate flag, no
    `--flag=value` form, no positionals, no trailing tokens. This is the
    real enforcement the `.codex/rules/default.rules` PREFIX rule cannot
    provide by itself."""
    if len(raw_argv) != 2:
        return False
    if raw_argv[0] != '--candidate-branch':
        return False
    value = raw_argv[1]
    if not value or value.startswith('-'):
        return False
    return True


def main(argv=None):
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if not _validate_exact_invocation_argv(raw_argv):
        result = {'status': 'denied', 'reason_code': 'invalid_executor_invocation_shape'}
        sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
        sys.stdout.write(chr(10))
        return 1
    args = _build_cli_parser().parse_args(raw_argv)
    result = run(args.candidate_branch)
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))
    sys.stdout.write(chr(10))
    ok = result.get('status') in (
        DEFAULT_BRANCH_FF_STATUS_MERGED_AND_VERIFIED,
        DEFAULT_BRANCH_FF_STATUS_TRANSPORT_ERROR_MERGED_VERIFIED,
        DEFAULT_BRANCH_FF_STATUS_EXECUTION_ERROR_MERGED_AND_VERIFIED,
    )
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
