#!/usr/bin/env python3
"""
controlled_skill_mutation_policy.py

Shared policy definition for CONTROLLED_SKILL_MUTATION_COMMAND_POLICY.
Consumed by both worktree_scope_guard.py and local_main_branch_guard.py
so the two guards never split-brain on which executor commands are allowed.

This module is SEPARATE from the preflight.run policy (run_refinement_preflight.py).
Command IDs: termination_report.publish, issue_body.update, issue_comment.publish,
contract_snapshot.publish

Issue #1166 (termination_report.publish).
Issue #1284 extends the same executor lane to issue metadata mutation
(issue_body.update / issue_comment.publish / contract_snapshot.publish) so that
these mutations can run from root/default branch without an issue-specific
worktree. Input file namespace for the new command ids is unified under
``artifacts/{issue_number}/issue-metadata/{command-id}/``.
"""

from __future__ import annotations

import os
import re
import shlex

# ── Canonical constants ───────────────────────────────────────────────────────

TRUSTED_REPO = "squne121/loop-protocol"
EXECUTOR_SCRIPT = "scripts/agent-guards/controlled_skill_mutation_exec.py"
COMMAND_ID_PUBLISH = "termination_report.publish"
COMMAND_ID_ISSUE_BODY_UPDATE = "issue_body.update"
COMMAND_ID_ISSUE_COMMENT_PUBLISH = "issue_comment.publish"
COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH = "contract_snapshot.publish"
# Issue #1536: controlled review publisher. `--issue-number` is reused as the
# PR number for this command id (the executor's generic input-file/env
# binding is issue-number-shaped; a PR number occupies the same GitHub
# numbering space). The pr_review.publish field validator additionally
# requires an explicit `pr_number` field and checks it matches.
COMMAND_ID_PR_REVIEW_PUBLISH = "pr_review.publish"

# Allowed write roots for all commands (relative to project root)
ALLOWED_WRITE_ROOTS = ["artifacts/"]

# Issue #1284: input file namespace for the 3 new command ids is unified under
# artifacts/{issue_number}/issue-metadata/{command-id}/
ISSUE_METADATA_NAMESPACE_SEGMENT = "issue-metadata"

# Per-command-id input schema (AC10). Command id ↔ schema mismatch is denied
# before mutation.
INPUT_SCHEMA_BY_COMMAND: dict = {
    COMMAND_ID_PUBLISH: "TERMINATION_REPORT_INPUT_V1",
    COMMAND_ID_ISSUE_BODY_UPDATE: "ISSUE_BODY_UPDATE_INPUT_V1",
    COMMAND_ID_ISSUE_COMMENT_PUBLISH: "ISSUE_COMMENT_PUBLISH_INPUT_V1",
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH: "CONTRACT_SNAPSHOT_PUBLISH_INPUT_V1",
    COMMAND_ID_PR_REVIEW_PUBLISH: "PR_REVIEW_PUBLISH_REQUEST_V1",
}

ALL_COMMAND_IDS = frozenset(INPUT_SCHEMA_BY_COMMAND)

# Command ids that keep the legacy (Issue #1166) mandatory LOOP_ISSUE_NUMBER env
# binding. New command ids (Issue #1284 AC15) treat LOOP_ISSUE_NUMBER as
# optional-but-must-match-if-present, since controlled metadata mutation may run
# from root without an issue-specific worktree/session env.
ENV_BINDING_MANDATORY_COMMAND_IDS = frozenset({COMMAND_ID_PUBLISH})

# Environment variables that the executor sanitizes (removes from child env)
# Issue #1539 fix_delta Blocker 2: GH_HOST / GH_REPO / GH_CONFIG_DIR / GH_DEBUG /
# DEBUG are stripped so an inherited parent-process override cannot redirect
# `gh` subprocess calls to a different host/config/repo or leak debug output.
ENV_SANITIZE_KEYS = [
    "PUBLISH_ARTIFACT_DIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "GH_EDITOR",
    "EDITOR",
    "VISUAL",
    "BROWSER",
    "GH_HOST",
    "GH_REPO",
    "GH_CONFIG_DIR",
    "GH_DEBUG",
    "DEBUG",
]

# ── CONTROLLED_SKILL_MUTATION_COMMAND_POLICY ──────────────────────────────────
#
# Canonical registry for controlled remote mutation transactions.
# Each entry is keyed by command_id and specifies:
#   executor_script:      repo-relative path to the single executor
#   allowed_write_roots:  directories (relative) the executor may write to
#   github_mutation:      GitHub mutation parameters
#   postcondition:        what must be true after the executor runs
#   idempotency:          how duplicate runs are detected
#   env_sanitize:         env vars overridden/removed before execution
#
CONTROLLED_SKILL_MUTATION_COMMAND_POLICY: dict = {
    COMMAND_ID_PUBLISH: {
        "command_id": COMMAND_ID_PUBLISH,
        "description": "Publish termination report as GitHub issue comment (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": "artifacts/{issue_number}/termination_report_published.marker.json",
            "marker_field": "comment_id",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_BODY_UPDATE: {
        "command_id": COMMAND_ID_ISSUE_BODY_UPDATE,
        "description": "Update GitHub issue body with stale-write precondition (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_BODY_UPDATE}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_BODY_UPDATE],
        "github_mutation": {
            "edit_issue_body": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "precondition": {
            "previous_body_sha256_must_match_readback": True,
            "previous_updated_at_must_match_readback": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "new_body_sha256_must_match_readback": True,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_BODY_UPDATE}/issue_body_update.marker.json"
            ),
            "marker_field": "new_body_sha256",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_ISSUE_COMMENT_PUBLISH: {
        "command_id": COMMAND_ID_ISSUE_COMMENT_PUBLISH,
        "description": "Publish a GitHub issue comment with marker readback (controlled remote mutation)",
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_ISSUE_COMMENT_PUBLISH],
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_ISSUE_COMMENT_PUBLISH}/issue_comment_publish.marker.json"
            ),
            "marker_field": "comment_id",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH: {
        "command_id": COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH,
        "description": (
            "Publish Contract Snapshot comment via ensure_contract_snapshot.py "
            "(controlled remote mutation; publisher authority fixed to "
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py)"
        ),
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH],
        "publisher_script": (
            ".claude/skills/impl-review-loop/scripts/ensure_contract_snapshot.py"
        ),
        "github_mutation": {
            "comment_on_issue": True,
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_CONTRACT_SNAPSHOT_PUBLISH}/contract-snapshot-{{issue_number}}.json"
            ),
            "marker_field": "contract_snapshot_url",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
    COMMAND_ID_PR_REVIEW_PUBLISH: {
        "command_id": COMMAND_ID_PR_REVIEW_PUBLISH,
        "description": (
            "Publish a GitHub PR review (event: COMMENT, commit_id-bound, "
            "idempotent) on behalf of the read-only pr-reviewer SubAgent "
            "(controlled remote mutation, Issue #1536 Option C)"
        ),
        "executor_script": EXECUTOR_SCRIPT,
        "allowed_write_roots": ALLOWED_WRITE_ROOTS,
        "input_namespace": (
            f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/{COMMAND_ID_PR_REVIEW_PUBLISH}/"
        ),
        "input_schema": INPUT_SCHEMA_BY_COMMAND[COMMAND_ID_PR_REVIEW_PUBLISH],
        "github_mutation": {
            "review_on_pull_request": True,
            "review_event_fixed": "COMMENT",
            "requires_repo": TRUSTED_REPO,
            "requires_explicit_repo_flag": True,
        },
        "precondition": {
            "expected_head_sha_must_match_remote_pr_head": True,
        },
        "postcondition": {
            "no_tracked_source_changes": True,
            "no_lockfile_changes": True,
            "no_settings_changes": True,
            "allowed_write_roots": ALLOWED_WRITE_ROOTS,
            "review_state_must_be_commented": True,
            "review_commit_id_must_match_expected_head_sha": True,
            "review_body_sha256_must_match_readback": True,
        },
        "idempotency": {
            "marker_file_pattern": (
                f"artifacts/{{issue_number}}/{ISSUE_METADATA_NAMESPACE_SEGMENT}/"
                f"{COMMAND_ID_PR_REVIEW_PUBLISH}/pr_review_publish.marker.json"
            ),
            "marker_field": "idempotency_key",
        },
        "env_sanitize": ENV_SANITIZE_KEYS,
    },
}

# ── Executor argv spec ────────────────────────────────────────────────────────
# Exact argv shape accepted by the executor.
# --flag=value forms are rejected (must use separate tokens).
# Duplicate flags are rejected.
# Unknown flags are rejected.

_EXECUTOR_VALUE_FLAGS: frozenset[str] = frozenset({
    "--command-id",
    "--issue-number",
    "--input-file",
    "--repo",
    # Issue #1539 fix_delta Blocker 1: pr_review.publish "render mode" flags.
    # These let a trusted caller hand the executor the raw verdict fields and
    # a body TEXT file (no self-declared hash/schema/producer_role) instead of
    # a pre-built PR_REVIEW_PUBLISH_REQUEST_V1 JSON. The executor computes
    # body_sha256 / idempotency_key / producer_role / event itself.
    "--render-body-file",
    "--verdict",
    "--reviewed-head-sha",
    "--expected-head-sha",
})
_EXECUTOR_BOOL_FLAGS: frozenset[str] = frozenset({
    "--json",
    "--dry-run",
    "--merge-ready",
})
# Baseline flags required for every invocation. --input-file XOR --render-body-file
# (plus its companion flags) is enforced separately in _validate_executor_argv
# because it is a semantic OR, not a flat set-containment requirement.
_EXECUTOR_REQUIRED_FLAGS: frozenset[str] = frozenset({
    "--command-id",
    "--issue-number",
    "--repo",
})
_EXECUTOR_RENDER_MODE_REQUIRED_FLAGS: frozenset[str] = frozenset({
    "--render-body-file",
    "--verdict",
    "--reviewed-head-sha",
    "--expected-head-sha",
})

# Shell metacharacters that make a command unparseable / compound
_SHELL_METACHAR_RE = re.compile(r"[;&|<>$`\n\r\0\\(){}*?\[\]!~]")


def _validate_executor_argv(args: list[str]) -> bool:
    """True iff args is an exact, non-redundant argv for the executor.

    Rejects:
    - unknown flags
    - duplicate flags
    - --flag=value forms
    - positional arguments (not preceded by a known flag)
    - value-flags with no following value
    - value-flags whose value looks like another flag

    Does NOT validate semantic constraints (repo slug, file existence, etc.) —
    those are enforced by the executor itself.
    """
    seen: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i]
        # Reject positional arguments (bare tokens not starting with --)
        if not tok.startswith("--"):
            return False
        # Reject --flag=value forms (must use separate tokens)
        if "=" in tok:
            return False
        # Reject duplicates
        if tok in seen:
            return False
        seen.add(tok)

        if tok in _EXECUTOR_BOOL_FLAGS:
            i += 1
            continue
        if tok in _EXECUTOR_VALUE_FLAGS:
            if i + 1 >= len(args):
                return False  # value-flag with no following value
            val = args[i + 1]
            if val.startswith("--"):
                return False  # value looks like another flag
            i += 2
            continue
        # Unknown flag
        return False

    # All baseline required flags must be present
    if not _EXECUTOR_REQUIRED_FLAGS.issubset(seen):
        return False

    # Exactly one of --input-file / --render-body-file (Issue #1539 Blocker 1).
    has_input_file = "--input-file" in seen
    has_render_mode = "--render-body-file" in seen
    if has_input_file == has_render_mode:
        # Neither present, or both present -- ambiguous / not allowed.
        return False
    if has_render_mode and not _EXECUTOR_RENDER_MODE_REQUIRED_FLAGS.issubset(seen):
        return False

    return True


def is_controlled_skill_mutation_exec_command(cmd: str, project_root: str) -> bool:
    """Return True iff cmd is an exact controlled_skill_mutation_exec.py invocation.

    Accepted forms:
      uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]
      python3 scripts/agent-guards/controlled_skill_mutation_exec.py [FLAGS]

    Rejected:
    - shell metacharacters (compound commands, injection)
    - --flag=value forms
    - unknown/duplicate flags
    - missing required flags
    - script path that does not resolve to the canonical executor
    - python -c / /tmp/ scripts
    - bash -c wrappers

    This function is the single shared authority consumed by both
    worktree_scope_guard and local_main_branch_guard (Issue #1166 AC4/AC17).
    """
    if not cmd or not cmd.strip():
        return False

    # Reject any shell metacharacter or separator (prevents injection riding
    # along on a legitimate prefix match)
    if _SHELL_METACHAR_RE.search(cmd):
        return False

    try:
        toks = shlex.split(cmd.strip())
    except ValueError:
        return False

    if not toks:
        return False

    # Determine script token index
    if toks[:3] == ["uv", "run", "python3"]:
        rest = toks[3:]
    elif len(toks) >= 1 and os.path.basename(toks[0]) in ("python3", "python"):
        rest = toks[1:]
    else:
        return False

    if not rest:
        return False
    if rest[0].startswith("-"):
        return False  # reject python -c and bare flags

    script_token = rest[0]
    # Reject /tmp/ scripts and python -c forms
    if script_token.startswith("/tmp/") or script_token == "-c":
        return False
    # Reject inline absolute paths that aren't the executor
    if os.path.isabs(script_token):
        script_real = os.path.realpath(script_token)
    else:
        script_real = os.path.realpath(os.path.join(project_root, script_token))

    # Resolve canonical executor path from project_root
    canonical_executor = os.path.realpath(os.path.join(project_root, EXECUTOR_SCRIPT))
    if script_real != canonical_executor:
        return False

    # Validate argv shape
    return _validate_executor_argv(rest[1:])
