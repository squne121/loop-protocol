#!/usr/bin/env python3
"""
command_registry.py

ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 — single source of truth for all
operator-facing / orchestrator-facing commands in the issue-refinement-loop.

SubAgents and the main thread consume this registry to build argv arrays;
they MUST NOT hand-craft shell strings.

CLI:
    python command_registry.py --list
    => prints ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 JSON to stdout

API:
    from command_registry import render_command, validate_shell_string, REGISTRY

Security contract:
    - render_command() always returns argv: list[str] (never a shell string)
    - validate_shell_string() rejects compound operators / substitutions /
      redirections / nested shell / unknown commands
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Registry schema version
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "ISSUE_REFINEMENT_COMMAND_REGISTRY_V1"

# Relative to repo root (used as cwd_policy=repo_root)
_SKILL_PREFIX = ".claude/skills/issue-refinement-loop/scripts"

# ---------------------------------------------------------------------------
# Deny tokens for shell-string validation (AC4)
# ---------------------------------------------------------------------------

DENY_TOKENS: frozenset[str] = frozenset({
    # Compound operators
    "&&", "||", ";",
    # Pipe and redirections
    "|", ">", "<", ">>", "<<",
    # Process / command substitution
    "$(", "<(", ">(", "`",
    # Shell launchers that bypass argv
    "bash", "sh",
    # Environment injection
    "env",
    # Directory traversal trick
    "cd",
})

# Characters that indicate shell operators (for unspaced operator detection — Blocker 5)
_SHELL_OPERATOR_CHARS: frozenset[str] = frozenset({"&", "|", ";", "<", ">"})

# Regex patterns that catch substitution syntax even without tokenization
_SUBST_PATTERNS = re.compile(
    r"""
    \$\(        |   # command substitution $(...)
    `[^`]+`     |   # backtick substitution
    <\(         |   # process substitution <(...)
    >\(             # process substitution >(...)
    """,
    re.VERBOSE,
)

# ---------------------------------------------------------------------------
# Known executable allowlist for shell-string validation (Blocker 6)
# ---------------------------------------------------------------------------

KNOWN_EXECUTABLES: frozenset[str] = frozenset({
    "uv",
    "pnpm",
    "gh",
    "rg",
    "python3",
    "pytest",
    "node",
    "npm",
    "git",
    "jq",
    "curl",
    "cat",
    "echo",
    "ls",
    "find",
    "grep",
    "which",
    "true",
    "false",
    "test",
    "mkdir",
})

# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------

REGISTRY: dict[str, dict[str, Any]] = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/workflow_start_entry.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        # Issue #2323: this declared value already matches the ACTUAL
        # stdout shape on every decision branch -- `workflow_start_entry.py`
        # renders the SAME compact stdout line grammar
        # (`STATUS:`/`NEXT_ACTION:`/`BLOCKERS:` etc., via
        # `run_refinement_preflight.py::_build_compact_stdout()`) whether
        # the producer decision is `ready`/`degraded` (passthrough) or
        # `blocked`/caller-capability-request-malformed (rendered directly
        # by `workflow_start_entry.py` itself). Prior to Issue #2323, the
        # blocked/malformed branches instead emitted a raw
        # `{"schema": "WORKFLOW_START_ENTRY_RESULT_V1", ...}` JSON dict --
        # this declared value did not change, only the actual branch
        # behavior was brought into alignment with it.
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    # Issue #1439 Scope Delta 2: test-only command-id driving the real
    # executor -> real preflight -> real planner subprocess chain offline
    # (via --fixture, which bypasses the `gh` CLI). Production `preflight.run`
    # above is entirely unmodified -- this is a sibling entry, not a
    # generalization of it. Same trusted repo slug / default branch /
    # canonical root safety boundary applies (see
    # skill_runtime_command_policy.py / skill_runtime_exec.py).
    "preflight.run.fixture": {
        "id": "preflight.run.fixture",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--fixture", "{fixture}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_fixture",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "local_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "test_only": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "fixture": {"type": "repo_relative_file", "required": True},
        },
    },
    # Issue #2136: test-only sibling profile for the real offline E2E.  It
    # deliberately combines the fixture and human-context contracts without
    # widening either production profile.
    "preflight.run.fixture.with_human_context": {
        "id": "preflight.run.fixture.with_human_context",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--fixture", "{fixture}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--investigation-evidence-transport-path", "{investigation_evidence_transport_path}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor_fixture",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "local_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "test_only": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "fixture": {"type": "repo_relative_file", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
            # The transport is a closed optional flag/value pair.  The
            # test-only sibling must retain both the transport-present and
            # transport-absent command grammars; policy/executor reject every
            # partial, duplicate, reordered, equals, or unknown variant.
            "investigation_evidence_transport_path": {
                "type": "repo_relative_file", "required": False, "optional_flag_pair": True,
            },
        },
    },
    # An anchor URL is not an origin assertion.  The unlabelled profile is
    # intentionally read-only and resolves to generated/unknown provenance.
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    "preflight.run.with_human_context": {
        "id": "preflight.run.with_human_context",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--investigation-evidence-transport-path", "{investigation_evidence_transport_path}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
            # #2086 P0 fix_delta (Blocker 1/2): optional -- an operator lane
            # invocation without a read-only-investigation directive never
            # supplies this, and render_command()'s `optional_flag_pair`
            # mechanism drops the whole `--investigation-evidence-transport-path
            # {value}` pair when absent (see command_registry.render_command
            # docstring / Issue #2053 P0 fix-delta precedent for `decide.run`).
            "investigation_evidence_transport_path": {
                "type": "path", "required": False, "optional_flag_pair": True,
            },
        },
    },
    "preflight.run.with_agent_report": {
        "id": "preflight.run.with_agent_report",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--agent-report-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    # The generic mutation profile remains fail-closed because it carries no
    # origin lane.  Only the human-context profile can reach the consumer.
    "contract_update.run.with_anchor": {
        "id": "contract_update.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--consume-contract-patch-plan",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_contract_update_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": True,
        "main_control_plane_only": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    "contract_update.run.with_human_context": {
        "id": "contract_update.run.with_human_context",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--consume-contract-patch-plan",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_contract_update_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "refinement_preflight_result/v1",
        "timeout_seconds": 120,
        "mutation": True,
        "main_control_plane_only": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    # Issue #1547: scope_rollup.run exact command -- bound directly to
    # scripts/agent-guards/run_scope_rollup_preflight.py (NOT the
    # issue-refinement-loop skill_runtime_exec.py executor, which is
    # hard-coded to run_refinement_preflight.py and is out of this Issue's
    # Allowed Paths). This entry is a documentation/SSOT registration of the
    # canonical argv shape; scope_rollup.run is dispatched directly by
    # local_main_branch_guard.py / skill_runtime_command_policy.py, not via
    # skill_runtime_exec.py's render_command() dispatch path.
    "scope_rollup.run": {
        "id": "scope_rollup.run",
        "argv": [
            "uv", "run", "python3",
            "scripts/agent-guards/run_scope_rollup_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--invocation-id", "{invocation_id}",
            "--requested-at", "{requested_at}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_scope_rollup_run",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [],
        "network_effect": "github_read_only",
        "stdin_contract": "none",
        "stdout_contract": "scope_rollup_run_result/v1",
        "timeout_seconds": 180,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "invocation_id": {"type": "string", "required": True},
            "requested_at": {"type": "string", "required": True},
        },
    },
    "plan.run": {
        "id": "plan.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/plan_refinement_loop.py",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "refinement_loop_planner_input/v1",
        "stdout_contract": "refinement_loop_plan/v1",
        "timeout_seconds": 60,
        "mutation": False,
        "placeholders": {},
    },
    "web_research.route": {
        "id": "web_research.route",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/route_web_research_result.py",
            "--input-file", "{routing_input_file}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "web_research_routing_result/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "routing_input_file": {"type": "repo_relative_file", "required": True},
        },
    },
    # #2086 AC10 (iteration 2, post-#2053/#2068 merge): `decide.run`'s argv
    # was extended by #2053 (now merged to main) to optionally carry the
    # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 router role (issue_number/repo/
    # authority_transport_manifest_path/authority_expected/invocation_id/
    # git_head_sha are all optional_flag_pair / bool_flag placeholders, so a
    # caller that omits them gets byte-identical argv to the pre-#2053
    # loop_state_file/verdict/max_iterations-only shape). `execution_class`
    # / `required_cwd` / `required_branch` / `allowed_write_roots` /
    # `network_effect` must mirror the eligibility invariants declared in
    # skill_runtime_command_policy.py's
    # `SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"]["decide.run"]`
    # exactly, or `validate_registry_entry()` rejects this entry before
    # dispatch (registry/policy declaration without a real dispatch path is
    # exactly the false-green pattern this Issue closes). `allowed_write_roots`
    # is the same `.claude/artifacts/issue-refinement-loop/{active_issue}/`
    # root used by every other eligible command_id -- decide_next_loop_action.py
    # genuinely writes a SCOPE_DELTA_ROUTER_RECEIPT_V1 under
    # `.claude/artifacts/issue-refinement-loop/{issue_number}/authority-transport/
    # {invocation_id}/` (a subpath of that root) whenever both --issue-number
    # and --invocation-id are supplied, so `allowed_write_roots: []` (this
    # Issue's iteration-1 declaration, written before #2053 merged) was stale.
    "decide.run": {
        "id": "decide.run",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/decide_next_loop_action.py",
            "--loop-state-file", "{loop_state_file}",
            "--review-result-verdict", "{verdict}",
            "--max-iterations", "{max_iterations}",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--authority-transport-path", "{authority_transport_manifest_path}",
            "{authority_expected}",
            "--invocation-id", "{invocation_id}",
            "--git-head-sha", "{git_head_sha}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_router_authority_transport",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "local_only",
        "stdin_contract": "none",
        "stdout_contract": "decide_next_loop_action/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "loop_state_file": {"type": "repo_relative_file", "required": True},
            "verdict": {"type": "verdict", "required": True},
            "max_iterations": {"type": "positive_int", "required": False, "optional_flag_pair": True},
            "issue_number": {"type": "positive_int", "required": False, "optional_flag_pair": True},
            "repo": {"type": "owner_repo", "required": False, "optional_flag_pair": True},
            "authority_transport_manifest_path": {"type": "path", "required": False, "optional_flag_pair": True},
            "authority_expected": {"type": "bool_flag", "flag_literal": "--authority-expected"},
            "invocation_id": {"type": "string", "required": False, "optional_flag_pair": True},
            "git_head_sha": {"type": "string", "required": False, "optional_flag_pair": True},
        },
    },
    # #2086 AC10 / AC9 (iteration 2): producer role of the #2053/#2068
    # SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 chain (command_registry.py entry
    # itself is unchanged from #2068's merge -- reused verbatim per AC9,
    # except `required_branch` was missing from #2068's registry entry even
    # though skill_runtime_command_policy.py's eligible_command_ids already
    # declared it, which made `validate_registry_entry()` reject this entry
    # unconditionally with `required_branch_mismatch` before this fix, and
    # `allowed_write_roots` is normalized to the same
    # `.claude/artifacts/issue-refinement-loop/{active_issue}/` root every
    # other eligible command_id (including skill_runtime_command_policy.py's
    # own already-merged eligible_command_ids declaration for this exact
    # command_id) uses, instead of the narrower authority-transport-specific
    # literal that was never cross-validated against policy.py before this
    # Issue's AC10 wiring made `validate_registry_entry()` actually check it).
    "authority_transport.produce": {
        "id": "authority_transport.produce",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--invocation-id", "{invocation_id}",
            "--git-head-sha", "{git_head_sha}",
            "--produce-authority-transport", "{evidence_fixture_path}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_authority_transport_producer",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "local_only",
        "stdin_contract": "none",
        "stdout_contract": "scope_delta_authority_transport_producer_result/v1",
        "timeout_seconds": 60,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "invocation_id": {"type": "string", "required": True},
            "git_head_sha": {"type": "string", "required": True},
            "evidence_fixture_path": {"type": "path", "required": True},
        },
    },
    # #2086 AC10 / AC9 (iteration 2): controlled consumer role of the
    # #2053/#2068 chain. Same `required_branch` fix and `allowed_write_roots`
    # normalization as `authority_transport.produce` above.
    "authority_transport.consume": {
        "id": "authority_transport.consume",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--invocation-id", "{invocation_id}",
            "--git-head-sha", "{git_head_sha}",
            "--consume-authority-transport", "{router_receipt_path}",
            "--contract-patch-plan-file", "{contract_patch_plan_file}",
            "--anchor-context-file", "{anchor_context_file}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_authority_transport_consumer",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_mutation",
        "stdin_contract": "none",
        "stdout_contract": "scope_delta_consumption_receipt/v1",
        "timeout_seconds": 60,
        "mutation": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "invocation_id": {"type": "string", "required": True},
            "git_head_sha": {"type": "string", "required": True},
            "router_receipt_path": {"type": "path", "required": True},
            "contract_patch_plan_file": {"type": "path", "required": False, "optional_flag_pair": True},
            "anchor_context_file": {"type": "path", "required": False, "optional_flag_pair": True},
        },
    },
    # Issue #2039 AC8/AC11: `repair_action.apply` controlled consumer --
    # bridges a repair_issue_contract.py auto_apply_safe candidate to a
    # controlled edit_issue_txn.py Issue-body mutation. Fixed repo-root cwd,
    # `mutation: true`, `network_effect: github_mutation` (this command's
    # default execution path performs a real GitHub Issue mutation via
    # edit_issue_txn.py, never a raw `gh issue edit` call). stdout is
    # constrained to `repair_apply_result/v1`.
    "repair_action.apply": {
        "id": "repair_action.apply",
        "argv": [
            "uv", "run", "python3",
            f"{_SKILL_PREFIX}/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--apply-repair-action", "{preflight_result_path}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_repair_action_apply",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_mutation",
        "stdin_contract": "none",
        "stdout_contract": "repair_apply_result/v1",
        # PR #2202 review fix-delta (P0-6): this outer supervisor timeout
        # MUST stay strictly greater than the sum of
        # run_refinement_preflight.py's own inner subprocess budgets for
        # `run_repair_action_apply()` --
        # REPAIR_APPLY_READINESS_SUBPROCESS_TIMEOUT_SECONDS (30s) +
        # REPAIR_APPLY_EDIT_ISSUE_TXN_SUBPROCESS_TIMEOUT_SECONDS (60s) = 90s
        # worst-case critical path -- PLUS a readback reserve
        # (REPAIR_APPLY_READBACK_RESERVE_SECONDS, one GH_API_TIMEOUT-bounded
        # `_fetch_issue()` read = 30s) so the AC5 authoritative-readback
        # path always has time to run after a genuine
        # timeout/OSError/unparseable-stdout `unknown` outcome, PLUS a
        # margin (REPAIR_APPLY_OUTER_TIMEOUT_MARGIN_SECONDS, 30s) for
        # interpreter startup, argv parsing, and non-subprocess local work.
        # 90 + 30 + 30 = 150. Previously this was 60s (strictly LESS than
        # even the 90s inner critical path alone), which meant the outer
        # supervisor could kill this process mid-dispatch -- after a PATCH
        # may already have been sent to GitHub -- before the readback path
        # ever ran. See run_refinement_preflight.py's
        # REPAIR_APPLY_*_SECONDS constants (kept in sync manually; no
        # runtime cross-import exists between this registry module and
        # run_refinement_preflight.py's inner subprocess timeouts).
        "timeout_seconds": 150,
        "mutation": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "preflight_result_path": {"type": "path", "required": True},
        },
    },
    "gh.issue.view": {
        "id": "gh.issue.view",
        "argv": [
            "gh", "issue", "view", "{issue_number}",
            "--repo", "{repo}",
            "--json", "title,body,number,state,comments,labels",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "gh_issue_json",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "gh.issue.comment": {
        "id": "gh.issue.comment",
        "argv": [
            "gh", "issue", "comment", "{issue_number}",
            "--repo", "{repo}",
            "--body-file", "{body_file}",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "none",
        "timeout_seconds": 30,
        "mutation": True,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "body_file": {"type": "body_file", "required": True},
        },
    },
    "gh.issue.comments.list": {
        "id": "gh.issue.comments.list",
        "argv": [
            "gh", "api",
            "repos/{repo}/issues/{issue_number}/comments?per_page=100",
            "--paginate", "--slurp",
        ],
        "shell": False,
        "cwd_policy": "any",
        "stdin_contract": "none",
        "stdout_contract": "gh_issue_comments_json",
        "timeout_seconds": 30,
        "mutation": False,
        "placeholders": {
            "repo": {"type": "owner_repo", "required": True},
            "issue_number": {"type": "positive_int", "required": True},
        },
    },
    "uv.pytest": {
        "id": "uv.pytest",
        "argv": [
            "uv", "run", "pytest", "{test_path}", "-v",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "pytest_output",
        "timeout_seconds": 300,
        "mutation": False,
        "placeholders": {
            "test_path": {"type": "repo_relative_file", "required": True},
        },
    },
    "pnpm.typecheck": {
        "id": "pnpm.typecheck",
        "argv": ["pnpm", "typecheck"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "typecheck_output",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.lint": {
        "id": "pnpm.lint",
        "argv": ["pnpm", "lint"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "lint_output",
        "timeout_seconds": 120,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.test": {
        "id": "pnpm.test",
        "argv": ["pnpm", "test"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "test_output",
        "timeout_seconds": 300,
        "mutation": False,
        "placeholders": {},
    },
    "pnpm.build": {
        "id": "pnpm.build",
        "argv": ["pnpm", "build"],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "build_output",
        "timeout_seconds": 180,
        "mutation": False,
        "placeholders": {},
    },
    # Issue 1507: fail-closed grammar validator for the issue-reviewer
    # SubAgent stdout. Consumes the SubAgent exact final text via stdin
    # (no re-transcription); see validate_review_compact_output.py and
    # SKILL.md Step 2 / Step 2a.
    #
    # AC22 (P1-2 of the second owner review): argv is rendered with
    # `uv run --locked --offline --no-sync python3 ...` so the rendered
    # argv's actual execution semantics match this entry's own
    # `mutation: False` / `network_effect: local_only` declarations exactly
    # (no implicit lockfile sync / no implicit network access at run time).
    "review_compact.validate": {
        "id": "review_compact.validate",
        "argv": [
            "uv", "run", "--locked", "--offline", "--no-sync", "python3",
            f"{_SKILL_PREFIX}/validate_review_compact_output.py",
            "--issue-number", "{issue_number}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "issue_review_result_compact_v1/raw_text",
        "stdout_contract": "review_compact_validation_result/v1",
        "timeout_seconds": 30,
        "mutation": False,
        "network_effect": "local_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
        },
    },
    # Issue #2049: root-owned producer I/O for the issue-refinement-loop
    # review step. Fetches + pins the live Issue body exactly once, runs
    # check_issue_contract.py / contract_readiness_check.py / merge_readiness,
    # and persists the merged review result to the canonical artifact
    # directory. PR #2135 human REQUEST_CHANGES iteration-3 P0-1: this
    # command is now ALSO the sole producer of the ISSUE_REVIEW_RESULT_COMPACT_V2
    # compact envelope (see `produce_compact_result()` / `compact_result` in
    # the stdout JSON). Issue #2380: canonical Step 2 (issue-refinement-loop
    # SKILL.md) consumes this command's `compact_result.verdict` /
    # `compact_result.next_action` / `verified_transport_artifact` DIRECTLY --
    # it does NOT invoke the read-only `issue-reviewer` custom agent
    # (.codex/agents/issue-reviewer.toml) and does NOT relay
    # `compact_result.stdout_lines` to it. That agent remains available for
    # legacy CLI / diagnostic / regression-test use only (it never invokes
    # compact_review_result.py or performs any producer I/O itself; when
    # invoked via that legacy path it only relays `compact_result.stdout_lines`
    # verbatim). This command's own producer-I/O ownership is a deliberate,
    # narrow exception to #1875's minimal-harness direction (see
    # run_root_review_pipeline.py's module docstring "Architecture delta
    # relative to #1875" for the full rationale/consumer inventory).
    # Issue #2389: this command's stdout JSON ALSO carries a top-level
    # `canonical_step2_route` field (the verbatim return value of
    # `run_root_review_pipeline.route_canonical_step2_result()`, computed by
    # this SAME command before it prints -- see `_emit_produce_result()` in
    # run_root_review_pipeline.py) on EVERY output path (success / body-fetch
    # failure / VC-budget error / transport failure / artifact-readback
    # failure). Canonical Step 2 reads `canonical_step2_route` DIRECTLY as
    # its SOLE routing authority; it does not independently recompute
    # routing from `status` / `compact_result.verdict` /
    # `compact_result.next_action`.
    "root_review_pipeline.produce": {
        "id": "root_review_pipeline.produce",
        "argv": [
            "uv", "run", "--locked", "python3",
            f"{_SKILL_PREFIX}/run_root_review_pipeline.py",
            "produce",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "stdin_contract": "none",
        "stdout_contract": "root_review_pipeline_result/v1",
        "timeout_seconds": 90,
        "mutation": False,
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
}

# ---------------------------------------------------------------------------
# Placeholder validators (AC3, Blocker 7)
# ---------------------------------------------------------------------------

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HTTPS_URL_RE = re.compile(r"^https://")
_VERDICT_VALUES: frozenset[str] = frozenset({"approve", "request_changes", "needs-fix"})

# Issue #1498: canonical GitHub issue comment URL shape.
#   https://github.com/{owner}/{repo}/issues/{digits}#issuecomment-{digits}
# Character classes deliberately exclude "%" so any percent-encoded disguise
# of the canonical shape (e.g. %2e%2e, encoded "#") is rejected by
# construction -- the regex simply cannot match a "%" byte anywhere in
# owner/repo/issue/comment, so no separate decode step is required to catch
# it. Query strings, extra fragments/suffixes, trailing slashes, `/pull/`
# paths, and `discussion_r...` fragments are all rejected because the
# pattern is anchored end-to-end (fullmatch) with no room for extra
# characters.
_GH_ISSUE_COMMENT_URL_RE = re.compile(
    r"^https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/"
    r"issues/(?P<issue>[1-9][0-9]*)"
    r"#issuecomment-(?P<comment>[1-9][0-9]*)$"
)


def _validate_placeholder_value(name: str, value: Any, spec: dict) -> None:
    """Validate a single placeholder value against its type spec.

    Raises ValueError for invalid values (fail-closed per AC3).
    """
    ph_type = spec.get("type", "string")

    if ph_type == "positive_int":
        if isinstance(value, str):
            try:
                int_val = int(value)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Placeholder '{name}': expected positive_int, got non-numeric string {value!r}"
                )
        elif isinstance(value, int):
            int_val = value
        else:
            raise ValueError(
                f"Placeholder '{name}': expected positive_int, got {type(value).__name__}"
            )
        if int_val <= 0:
            raise ValueError(
                f"Placeholder '{name}': must be > 0, got {int_val}"
            )

    elif ph_type == "owner_repo":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty owner/repo string, got {value!r}"
            )
        if not _OWNER_REPO_RE.match(value):
            raise ValueError(
                f"Placeholder '{name}': must match owner/repo format, got {value!r}"
            )

    elif ph_type in ("path", "repo_relative_file", "body_file"):
        # repo_relative_file / body_file: absolute path 禁止、.. 禁止、NUL/newline 禁止、leading - 禁止
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty path string, got {value!r}"
            )
        if ph_type in ("repo_relative_file", "body_file"):
            if value.startswith("/"):
                raise ValueError(
                    f"Placeholder '{name}': absolute path not allowed, got {value!r}"
                )
            if ".." in value.split("/"):
                raise ValueError(
                    f"Placeholder '{name}': path traversal '..' not allowed, got {value!r}"
                )
            if "\x00" in value or "\n" in value or "\r" in value:
                raise ValueError(
                    f"Placeholder '{name}': NUL or newline in path not allowed, got {value!r}"
                )
            if value.startswith("-"):
                raise ValueError(
                    f"Placeholder '{name}': leading '-' in path not allowed, got {value!r}"
                )

    elif ph_type == "url":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty URL string, got {value!r}"
            )
        if not _HTTPS_URL_RE.match(value):
            raise ValueError(
                f"Placeholder '{name}': URL must start with https://, got {value!r}"
            )

    elif ph_type == "github_issue_comment_url":
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Placeholder '{name}': expected non-empty GitHub issue comment URL, got {value!r}"
            )
        if "%" in value:
            raise ValueError(
                f"Placeholder '{name}': percent-encoding not allowed in canonical "
                f"GitHub issue comment URL, got {value!r}"
            )
        match = _GH_ISSUE_COMMENT_URL_RE.fullmatch(value)
        if match is None:
            raise ValueError(
                f"Placeholder '{name}': must be a canonical "
                f"https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<M> URL, got {value!r}"
            )
        # Defense-in-depth cross-check with urlparse: the regex above already
        # rejects userinfo/port/query by construction, but this makes the
        # rejection explicit and independent of the regex implementation.
        parsed = urlparse(value)
        if parsed.scheme != "https":
            raise ValueError(f"Placeholder '{name}': scheme must be https, got {value!r}")
        if parsed.hostname != "github.com":
            raise ValueError(f"Placeholder '{name}': host must be github.com, got {value!r}")
        if parsed.username or parsed.password:
            raise ValueError(f"Placeholder '{name}': userinfo not allowed, got {value!r}")
        if parsed.port is not None:
            raise ValueError(f"Placeholder '{name}': port not allowed, got {value!r}")
        if parsed.query:
            raise ValueError(f"Placeholder '{name}': query string not allowed, got {value!r}")

    elif ph_type == "verdict":
        if not isinstance(value, str) or value not in _VERDICT_VALUES:
            raise ValueError(
                f"Placeholder '{name}': must be one of {sorted(_VERDICT_VALUES)}, got {value!r}"
            )

    elif ph_type == "string":
        if not isinstance(value, str):
            raise ValueError(
                f"Placeholder '{name}': expected string, got {type(value).__name__}"
            )

    elif ph_type == "bool_flag":
        # Issue #2053 P0 fix-delta: a self-contained conditional bare flag
        # (no value token). Only bool is accepted; the flag is emitted
        # verbatim (spec["flag_literal"]) when the value is truthy, and
        # omitted entirely (no token at all) when falsy/absent.
        if not isinstance(value, bool):
            raise ValueError(
                f"Placeholder '{name}': expected bool for bool_flag, got {type(value).__name__}"
            )


def render_command(command_id: str, params: dict[str, Any]) -> list[str]:
    """Render a registry command by substituting placeholders.

    Returns: argv as list[str] — never a shell string.

    Raises:
        KeyError: unknown command_id
        ValueError: invalid placeholder value (fail-closed per AC3)
        ValueError: extra (undefined) params provided
        ValueError: unresolved placeholder remains in rendered argv
    """
    if command_id not in REGISTRY:
        raise KeyError(f"Unknown command_id: {command_id!r}")

    entry = REGISTRY[command_id]
    placeholders = entry.get("placeholders", {})

    # Reject extra params not defined in placeholders (Blocker 7)
    extra_params = set(params.keys()) - set(placeholders.keys())
    if extra_params:
        raise ValueError(
            f"Extra params not defined for command {command_id!r}: {sorted(extra_params)}"
        )

    # Type-validate all provided params
    for name, value in params.items():
        if name in placeholders:
            _validate_placeholder_value(name, value, placeholders[name])

    # Check required placeholders are present
    for name, spec in placeholders.items():
        if spec.get("required", False) and name not in params:
            raise ValueError(
                f"Required placeholder '{name}' missing for command {command_id!r}"
            )

    # Substitute into argv template
    # Supports:
    #   - Whole-token placeholders: "{name}" -> str(value)
    #   - Partial-token placeholders: "prefix/{name}/suffix" -> "prefix/value/suffix"
    #   - Issue #2053 P0 fix-delta: optional whole-token placeholders that
    #     are entirely omitted (flag literal + value token, or a
    #     self-contained bool_flag token) when the caller does not supply
    #     them, so a single command_id (e.g. decide.run) can carry optional
    #     authority-transport routing without a parallel sibling ID.
    argv_tokens = entry["argv"]
    rendered: list[str] = []
    idx = 0
    while idx < len(argv_tokens):
        token = argv_tokens[idx]
        is_whole_placeholder = (
            token.startswith("{") and token.endswith("}") and token.count("{") == 1
        )
        if is_whole_placeholder:
            ph_name = token[1:-1]
            spec = placeholders.get(ph_name, {})
            provided = ph_name in params
            if spec.get("type") == "bool_flag":
                if provided and params[ph_name]:
                    flag_literal = spec.get("flag_literal")
                    if not flag_literal:
                        raise ValueError(
                            f"bool_flag placeholder '{ph_name}' missing 'flag_literal' spec"
                        )
                    rendered.append(flag_literal)
                idx += 1
                continue
            if (
                not provided
                and spec.get("optional_flag_pair", False)
                and not spec.get("required", False)
            ):
                # Drop this value token, and drop the immediately preceding
                # rendered token too if the *template* token right before
                # this one is a bare (non-placeholder) flag literal -- i.e.
                # this is a "--flag {value}" pair, so both go together.
                if idx > 0:
                    prev_template_tok = argv_tokens[idx - 1]
                    if not (prev_template_tok.startswith("{") and prev_template_tok.endswith("}")):
                        if rendered and rendered[-1] == prev_template_tok:
                            rendered.pop()
                idx += 1
                continue
        # Replace all placeholders in the token (supports partial substitution)
        result_token = token
        for ph_name2, value2 in params.items():
            result_token = result_token.replace(f"{{{ph_name2}}}", str(value2))
        rendered.append(result_token)
        idx += 1

    # Verify no unresolved placeholders remain in required positions (Blocker 7)
    for token in rendered:
        if token.startswith("{") and token.endswith("}"):
            ph_name = token[1:-1]
            spec = placeholders.get(ph_name, {})
            if spec.get("required", False):
                raise ValueError(
                    f"Unresolved required placeholder '{{{ph_name}}}' in rendered argv for {command_id!r}"
                )

    return rendered


# ---------------------------------------------------------------------------
# Shell string validator (AC4, Blocker 5, Blocker 6)
# ---------------------------------------------------------------------------

def validate_shell_string(s: str) -> dict[str, Any]:
    """Classify an untrusted shell string using shlex tokenization + deny matrix.

    Returns:
        {"ok": True, "blocked_reason": None}           — safe
        {"ok": False, "blocked_reason": "<reason>"}    — blocked

    AC4 deny list covers:
      - Compound operators: &&, ||, ;
      - Pipe: |
      - Redirections: >, <, >>, <<
      - Command / process substitution: $(), ``, <(), >()
      - Shell launchers: bash, sh (including bash -lc, sh -c)
      - Environment injection: env
      - Directory traversal: cd

    Blocker 5 — unspaced operator detection:
      Uses punctuation_chars=True in shlex to tokenize operators like
      cmd&&rm, a;b, cmd|grep, echo>x, cat<<EOF as separate tokens.

    Blocker 6 — unknown executable block:
      The first token (executable) must be in KNOWN_EXECUTABLES.
      If not, it is blocked. argv list[str] types are NOT validated here
      (only string inputs are validated).
    """
    # First: regex scan for substitution syntax (catches $( even without spaces)
    if _SUBST_PATTERNS.search(s):
        match = _SUBST_PATTERNS.search(s)
        return {"ok": False, "blocked_reason": f"command_substitution_detected: {match.group()!r}"}

    # Second: shlex tokenization with punctuation_chars=True to detect unspaced operators (Blocker 5)
    try:
        lexer = shlex.shlex(s, posix=True, punctuation_chars=True)
        lexer.whitespace_split = False
        tokens = list(lexer)
    except ValueError as exc:
        # shlex failed to parse — treat as blocked (fail-closed)
        return {"ok": False, "blocked_reason": f"shlex_parse_error: {exc}"}

    # Check for shell operator characters in tokens (Blocker 5)
    for token in tokens:
        if any(ch in token for ch in _SHELL_OPERATOR_CHARS):
            return {"ok": False, "blocked_reason": f"shell_operator_detected: {token!r}"}

    # Check deny token list (AC4)
    for token in tokens:
        if token in DENY_TOKENS:
            return {"ok": False, "blocked_reason": f"denied_token: {token!r}"}

    # Blocker 6: check first token (executable) against known allowlist
    # Filter out empty tokens from tokenization
    non_empty_tokens = [t for t in tokens if t]
    if non_empty_tokens:
        first_token = non_empty_tokens[0]
        if first_token not in KNOWN_EXECUTABLES:
            return {
                "ok": False,
                "blocked_reason": f"unknown_executable: {first_token!r} not in KNOWN_EXECUTABLES allowlist",
            }

    return {"ok": True, "blocked_reason": None}


# ---------------------------------------------------------------------------
# Registry export
# ---------------------------------------------------------------------------

def export_registry() -> dict[str, Any]:
    """Return the full registry as a serializable dict."""
    return {
        "schema": SCHEMA_VERSION,
        "commands": {k: dict(v) for k, v in REGISTRY.items()},
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 CLI"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print ISSUE_REFINEMENT_COMMAND_REGISTRY_V1 JSON to stdout",
    )
    return parser.parse_args(argv if argv is not None else sys.argv[1:])


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.list:
        print(json.dumps(export_registry(), ensure_ascii=False, indent=2))
    else:
        print("Usage: command_registry.py --list", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
