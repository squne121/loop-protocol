#!/usr/bin/env python3
"""classify_runtime_migration.py — Issue #2810.

Deterministic, root-owned classification of a Claude-GPT model/proxy
runtime-acceptance failure into exactly one of three buckets:

  - ``agent_executable_migration``: the repository-owned bounded repair
    (``bash scripts/claude-gpt/repair_proxy.sh``, literal, no added
    arguments) is safe and sufficient to run via the existing
    ``fix_delta -> Step 1 implementation-worker`` route.
  - ``implementation_defect``: the repair path is reachable but the repair
    itself (or the surrounding implementation) is broken; stays in the
    normal implementation fix loop.
  - ``human_capability_blocker``: a genuine human action is required
    (credential/secret/privilege/destructive mutation, a non-writable
    install target, an unreachable target host, an install-env override
    that would run an arbitrary installer, or a verified tool-call denial).

This module is intentionally narrow (Issue #2810 Out of Scope: "generic
remediation framework"). It only classifies the single ``repair_proxy.sh``
action; it is NOT a general-purpose remediation/approval engine.

Root (``impl-review-loop`` Step 5) is the ONLY caller of this module. A
SubAgent's own self-reported ``human_action_required`` /
``human_review_required`` has no classification authority here (#1860 Owner
Decision, reaffirmed by Issue #2810 AC6): a SubAgent's ``worker_result`` is
only ever consumed as *evidence* (``status`` / ``reason_code`` /
``exit_code`` / ``deny_evidence_verified`` / ``sudo_required_in_log``),
never as a self-classification.

CLI usage (stdin JSON -> stdout JSON, exit 0 on any successful --
deterministic -- classification; exit 2 only on malformed/non-JSON input):

    uv run --locked python3 \\
      .claude/skills/impl-review-loop/scripts/classify_runtime_migration.py \\
      < payload.json

Input schema (fixed key set -- Issue #2810 "In Scope"; do not add/remove
top-level keys without a fresh Issue, since this is a "固定契約"):

    {
      "failure_evidence": {
        "cause": "proxy_model_catalog_incompatible" | <any string>,
        "repair_command": "<string, as reported by the failure evidence>",
        "required_models": ["gpt-6-sol", "gpt-6-luna"],
        "missing_models": ["gpt-6-sol", "gpt-6-luna"]
      },
      "live_issue_authorizes_migration": true | false,
      "effective_env": {
        "claude_gpt_home": "<absolute path>",
        "override_vars_present": true | false
      },
      "probes": {
        "install_dir_writable": true | false | null,
        "host_reachable": true | false | null
      },
      "capability_flags": {
        "needs_credential": true | false,
        "needs_secret": true | false,
        "needs_privilege": true | false,
        "destructive_or_global": true | false
      },
      "worker_result": null | {
        "status": "ok" | "failed" | "blocked" | "permission_blocked",
        "reason_code": "<string | null>",
        "exit_code": <int | null>,
        "deny_evidence_verified": true | false,
        "sudo_required_in_log": true | false
      }
    }

Output schema (fixed key set):

    {
      "class": "agent_executable_migration" | "implementation_defect"
                | "human_capability_blocker",
      "route": "<string, machine-routable action label>",
      "human_action_report": null | {
        "reason": "<string>",
        "required_human_action": "<string>",
        "target_environment": "<string>",
        "verification_command": "<string>",
        "resume_condition": "<string>"
      }
    }

Note on the ``blocked`` + ``reason_code: command_mismatch`` worker result
(Issue #2810 In Scope, step-5-feedback-and-termination.md): that case is a
ROOT CONTRACT VIOLATION (root asked the worker to run a command other than
the exact literal it validated itself against), not a capability question.
It is handled directly by Step 5's own procedure as an immediate
fail-closed stop -- it never reaches this classifier, and does not need (or
get) a 4th ``class`` value.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# The ONLY repair command this classifier (and the worker it routes to) will
# ever treat as agent-executable. Literal, no added arguments (Issue #2810
# Outcome bullet 1).
EXACT_REPAIR_COMMAND = "bash scripts/claude-gpt/repair_proxy.sh"

EXPECTED_STRUCTURED_CAUSE = "proxy_model_catalog_incompatible"

_CLASS_AGENT_EXECUTABLE = "agent_executable_migration"
_CLASS_IMPLEMENTATION_DEFECT = "implementation_defect"
_CLASS_HUMAN_CAPABILITY_BLOCKER = "human_capability_blocker"

_ROUTE_TO_STEP1 = "route_to_step1_runtime_migration_fix_delta"
_ROUTE_CONTINUE_FIX_LOOP = "continue_loop_fix_delta"
_ROUTE_HUMAN_ESCALATION = "human_escalation_capability_blocker"

_VERIFICATION_COMMAND = "bash -c 'unset CLAUDE_GPT_PROXY_BIN; bash scripts/claude-gpt/launch.sh --check-only'"
_RESUME_CONDITION = (
    "human が required action を完了した後、fresh Step 2 で current-head の "
    "canonical runtime VC を再実行する（stale evidence の再利用は禁止）"
)


def _human_action_report(
    reason: str,
    required_human_action: str,
    target_environment: str,
    verification_command: str = _VERIFICATION_COMMAND,
    resume_condition: str = _RESUME_CONDITION,
) -> dict[str, Any]:
    return {
        "reason": reason,
        "required_human_action": required_human_action,
        "target_environment": target_environment,
        "verification_command": verification_command,
        "resume_condition": resume_condition,
    }


def _human_capability_blocker_result(
    *,
    reason: str,
    required_human_action: str,
    target_environment: str,
) -> dict[str, Any]:
    return {
        "class": _CLASS_HUMAN_CAPABILITY_BLOCKER,
        "route": _ROUTE_HUMAN_ESCALATION,
        "human_action_report": _human_action_report(
            reason=reason,
            required_human_action=required_human_action,
            target_environment=target_environment,
        ),
    }


def _implementation_defect_result(route: str = _ROUTE_CONTINUE_FIX_LOOP) -> dict[str, Any]:
    return {
        "class": _CLASS_IMPLEMENTATION_DEFECT,
        "route": route,
        "human_action_report": None,
    }


def _agent_executable_result() -> dict[str, Any]:
    return {
        "class": _CLASS_AGENT_EXECUTABLE,
        "route": _ROUTE_TO_STEP1,
        "human_action_report": None,
    }


def classify_runtime_migration(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify a runtime-migration failure/repair situation into exactly one
    of ``agent_executable_migration`` / ``implementation_defect`` /
    ``human_capability_blocker``. Pure function: no I/O, no env reads, no
    subprocess. See module docstring for the fixed input/output schema."""

    failure_evidence = payload.get("failure_evidence") or {}
    live_issue_authorizes_migration = bool(payload.get("live_issue_authorizes_migration", False))
    effective_env = payload.get("effective_env") or {}
    probes = payload.get("probes") or {}
    capability_flags = payload.get("capability_flags") or {}
    worker_result = payload.get("worker_result")

    cause = failure_evidence.get("cause")
    repair_command = failure_evidence.get("repair_command")

    claude_gpt_home = effective_env.get("claude_gpt_home")
    override_vars_present = bool(effective_env.get("override_vars_present", False))

    install_dir_writable = probes.get("install_dir_writable")
    host_reachable = probes.get("host_reachable")

    needs_credential = bool(capability_flags.get("needs_credential", False))
    needs_secret = bool(capability_flags.get("needs_secret", False))
    needs_privilege = bool(capability_flags.get("needs_privilege", False))
    destructive_or_global = bool(capability_flags.get("destructive_or_global", False))

    target_environment = f"CLAUDE_GPT_HOME={claude_gpt_home!s}"

    # --- Post-execution reclassification path (worker_result present) -----
    # This is the ONLY branch that consumes worker_result. A SubAgent's
    # self-reported status/reason_code is evidence, never a self-
    # classification (AC6): every branch below re-derives the class from
    # that evidence using this classifier's own fixed rules.
    if isinstance(worker_result, dict):
        w_status = worker_result.get("status")
        w_reason = worker_result.get("reason_code")
        deny_evidence_verified = bool(worker_result.get("deny_evidence_verified", False))
        sudo_required_in_log = bool(worker_result.get("sudo_required_in_log", False))

        # AC5: repair could not even start because the install target
        # requires privileged mutation (installer's sudo fallback path was
        # reached). This is verifiable FROM THE WORKER'S OWN evidence (the
        # install log), not a bare self-report. Checked BEFORE the generic
        # repair_failed -> implementation_defect branch below, since a
        # sudo-required failure is a capability blocker, not an ordinary
        # implementation defect, even though it may also be reported with
        # status=failed/reason_code=repair_failed.
        if sudo_required_in_log:
            return _human_capability_blocker_result(
                reason="privileged_mutation_required",
                required_human_action=(
                    "operator が対話環境で sudo 権限を使って "
                    f"{EXACT_REPAIR_COMMAND} を手動実行する（installer が "
                    "非対話 sudo 分岐に到達したため agent は実行不能）"
                ),
                target_environment=target_environment,
            )

        # AC4: repository-owned repair reachable but failed for an
        # implementation reason (installer error, version/tag mismatch,
        # wrong interpreter, managed-binary precedence defect, post-install
        # catalog mismatch, ...). Stays in the normal fix loop.
        if w_status == "failed" and w_reason == "repair_failed":
            return _implementation_defect_result()

        # AC5: a tool call for the exact repair_command was denied by
        # policy/hook, and root independently verified the deny evidence
        # (not the worker's bare self-report -- AC6).
        if w_status == "permission_blocked" and w_reason == "permission_denied":
            if deny_evidence_verified:
                return _human_capability_blocker_result(
                    reason="agent_tool_call_denied_by_policy",
                    required_human_action=(
                        "operator が許可設定（permission profile / hook policy）を "
                        f"確認し、必要なら手動で {EXACT_REPAIR_COMMAND} を実行する"
                    ),
                    target_environment=target_environment,
                )
            # Unverified self-report has no stop authority (AC6): treat as
            # an ordinary implementation-loop failure, never as a silent
            # "please run this command manually" termination.
            return _implementation_defect_result()

        # Any other worker failure surface (unexpected status/reason_code)
        # is not itself evidence of a human capability blocker; it stays in
        # the fix loop rather than escalating on an unrecognized signal.
        if w_status not in ("ok",):
            return _implementation_defect_result()

    # --- Pre-execution capability-blocker checks (independent of whether
    # this is a first-pass classification or a worker_result: status "ok"
    # re-check) ---------------------------------------------------------
    if needs_credential:
        return _human_capability_blocker_result(
            reason="credential_login_or_reauth_required",
            required_human_action="operator が本人 credential で login / re-auth する",
            target_environment=target_environment,
        )
    if needs_secret:
        return _human_capability_blocker_result(
            reason="secret_or_token_operation_required",
            required_human_action="operator が secret/token の入力・変更を行う",
            target_environment=target_environment,
        )
    if needs_privilege:
        return _human_capability_blocker_result(
            reason="privilege_escalation_required",
            required_human_action="operator が sudo / privilege escalation を伴う操作を行う",
            target_environment=target_environment,
        )
    if destructive_or_global:
        return _human_capability_blocker_result(
            reason="destructive_or_global_mutation_required",
            required_human_action="operator が destructive/global mutation の要否を判断し実行する",
            target_environment=target_environment,
        )
    if host_reachable is False:
        return _human_capability_blocker_result(
            reason="target_operator_host_unreachable",
            required_human_action="operator が target runtime host 上で直接 repair を実行する",
            target_environment=target_environment,
        )
    if install_dir_writable is False:
        return _human_capability_blocker_result(
            reason="install_dir_not_writable",
            required_human_action=(
                f"operator が {claude_gpt_home!s}/bin（またはその祖先 directory）の "
                "書き込み権限を修正するか、非対話環境外で手動 repair する"
            ),
            target_environment=target_environment,
        )
    if override_vars_present:
        # CLAUDE_CODE_PROXY_INSTALL_DIR / CLAUDE_CODE_PROXY_VERSION /
        # CLAUDE_GPT_REPAIR_INSTALLER_URL の存在 -- 任意 URL の installer 実行
        # や probe 対象と実 mutation target の乖離を招くため agent-executable
        # にしない。CLAUDE_GPT_HOME の override 自体はこのフラグに含まれない
        # （root が正当な入力として扱う -- Issue #2810 Outcome 1）。
        return _human_capability_blocker_result(
            reason="install_env_override_present",
            required_human_action=(
                "operator が CLAUDE_CODE_PROXY_INSTALL_DIR / "
                "CLAUDE_CODE_PROXY_VERSION / CLAUDE_GPT_REPAIR_INSTALLER_URL を "
                "unset してから再開する"
            ),
            target_environment=target_environment,
        )

    # --- Eligibility checks for agent_executable_migration -----------------
    if not live_issue_authorizes_migration:
        return _implementation_defect_result(route="not_authorized_implementation_defect")
    if repair_command != EXACT_REPAIR_COMMAND:
        return _implementation_defect_result(route="repair_command_not_exact_literal")
    if cause != EXPECTED_STRUCTURED_CAUSE:
        return _implementation_defect_result(route="unstructured_failure_evidence")
    if install_dir_writable is not True or host_reachable is not True:
        # Unknown (None) writability/reachability is not affirmatively safe;
        # fail-closed into the ordinary fix loop rather than guessing.
        return _implementation_defect_result(route="probe_result_indeterminate")

    return _agent_executable_result()


def probe_install_dir_writable(claude_gpt_home: str) -> dict[str, Any]:
    """Determine writability of ``$CLAUDE_GPT_HOME/bin`` (or its nearest
    existing ancestor directory) via ``os.access``. An existing
    ``claude-code-proxy`` file that cannot be replaced (bin dir not
    writable AND the file itself not writable) is also treated as
    non-writable (Issue #2810 In Scope)."""

    home = Path(claude_gpt_home)
    bin_dir = home / "bin"
    proxy_path = bin_dir / "claude-code-proxy"

    probed_path = bin_dir
    while not probed_path.exists():
        parent = probed_path.parent
        if parent == probed_path:
            break
        probed_path = parent

    ancestor_writable = os.access(probed_path, os.W_OK)

    existing_proxy_blocks_replace = False
    if proxy_path.exists():
        bin_dir_writable = os.access(bin_dir, os.W_OK) if bin_dir.exists() else False
        proxy_file_writable = os.access(proxy_path, os.W_OK)
        if not (bin_dir_writable or proxy_file_writable):
            existing_proxy_blocks_replace = True

    writable = bool(ancestor_writable and not existing_proxy_blocks_replace)

    return {
        "writable": writable,
        "probed_path": str(probed_path),
        "target_bin_dir": str(bin_dir),
        "existing_proxy_blocks_replace": existing_proxy_blocks_replace,
    }


def bind_pre_post_identity(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    """Determine whether ``pre`` (before-repair) and ``post`` (after-repair)
    evidence are bound to the SAME effective launcher environment. Rejects
    (never silently accepts) any of: differing ``CLAUDE_GPT_HOME`` absolute
    path, differing ``launch_sh_sha256``, differing repository head,
    ``CLAUDE_GPT_PROXY_BIN`` being set post-repair, or a post-repair
    selected proxy that is not the managed binary
    (``<CLAUDE_GPT_HOME>/bin/claude-code-proxy``). Issue #2810 AC7.

    Expected keys on both ``pre`` and ``post``:
      - ``claude_gpt_home_absolute_path``
      - ``launch_sh_sha256``
      - ``repo_head``

    Expected additional keys on ``post`` only:
      - ``claude_gpt_proxy_bin_env_set`` (bool)
      - ``selected_proxy_absolute_path``
    """

    mismatches: list[str] = []

    if pre.get("claude_gpt_home_absolute_path") != post.get("claude_gpt_home_absolute_path"):
        mismatches.append("claude_gpt_home_absolute_path_mismatch")
    if pre.get("launch_sh_sha256") != post.get("launch_sh_sha256"):
        mismatches.append("launch_sh_sha256_mismatch")
    if pre.get("repo_head") != post.get("repo_head"):
        mismatches.append("repo_head_mismatch")
    if bool(post.get("claude_gpt_proxy_bin_env_set", False)):
        mismatches.append("post_repair_claude_gpt_proxy_bin_env_set")

    post_home = post.get("claude_gpt_home_absolute_path")
    expected_selected_proxy = f"{post_home}/bin/claude-code-proxy" if post_home else None
    selected_proxy = post.get("selected_proxy_absolute_path")
    if expected_selected_proxy is None or selected_proxy != expected_selected_proxy:
        mismatches.append("selected_proxy_not_managed_binary")

    return {
        "identity_bound": len(mismatches) == 0,
        "mismatches": mismatches,
    }


def _run_cli(argv: list[str]) -> int:
    del argv  # stdin JSON -> stdout JSON only; no CLI flags.
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(
            json.dumps({"error": "invalid_json_input", "detail": str(exc)}),
            file=sys.stderr,
        )
        return 2
    if not isinstance(payload, dict):
        print(json.dumps({"error": "input_must_be_json_object"}), file=sys.stderr)
        return 2

    result = classify_runtime_migration(payload)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli(sys.argv[1:]))
