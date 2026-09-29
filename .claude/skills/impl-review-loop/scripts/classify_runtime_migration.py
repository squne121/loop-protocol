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

CLI usage. Three entry points, all root-owned (Step 5 / implementation-worker
pre-check); none of them is a general remediation framework:

  1. classify (default, no arguments; backward compatible). stdin JSON ->
     stdout JSON, exit 0 on any successful -- deterministic -- classification;
     exit 2 only on malformed/non-JSON input:

         uv run --locked python3 \\
           .claude/skills/impl-review-loop/scripts/classify_runtime_migration.py \\
           < payload.json

  2. ``materialize`` (Issue #2810 fix_delta P1-A). Builds the fixed-key-set
     classifier payload from CURRENT evidence (a launcher failure receipt, the
     live Issue body, the process environment, filesystem probes) so that no
     LLM has to hand-assemble the JSON. stdout = payload JSON; exit 2 with a
     stderr JSON error if any evidence is missing/malformed (fail-closed;
     see ``materialize_classifier_payload`` for the exact derivation rules):

         uv run --locked python3 \\
           .claude/skills/impl-review-loop/scripts/classify_runtime_migration.py \\
           materialize --failure-evidence-file <launch-result.json> \\
           [--issue-body-file <file> | --issue-number <N> [--repo <owner/repo>]] \\
           [--preflight-file <file>] [--install-log-file <file>] \\
           [--worker-result-file <file>] [--operator-host-differs] \\
         | uv run --locked python3 \\
           .claude/skills/impl-review-loop/scripts/classify_runtime_migration.py

  3. ``pre-repair-check`` (Issue #2810 fix_delta P1-B). Deterministic
     pre-mutation identity binding used by implementation-worker BEFORE it
     runs ``repair_proxy.sh``: exit 0 = bound (safe to run), exit 1 = blocked
     (identity_mismatch; repair MUST NOT run), exit 2 = usage/malformed input
     (also MUST NOT run). See ``verify_pre_repair_binding``.

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

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
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
# Fixed, identifiable route label for a payload whose primitive types violate
# the fixed schema (Issue #2810 fix_delta P1-C). Never agent-executable.
_ROUTE_MALFORMED_INPUT_TYPE = "malformed_input_type_fail_closed"

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


# --- Primitive type validation (Issue #2810 fix_delta P1-C) ------------------
#
# ``bool("false")`` is truthy, so a JSON string/int/list that slipped in where
# a bool belongs would silently flip a safety flag. The classifier therefore
# checks the primitive type of every fixed key that is PRESENT before using
# it. A key that is ABSENT keeps its historical default (unchanged
# behaviour); a key that is present with the WRONG type is never
# agent-executable and maps to ``implementation_defect`` with the fixed route
# ``malformed_input_type_fail_closed``. This is deliberately not a schema
# framework: it is exactly the fixed key set documented above.


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_bool_or_none(value: Any) -> bool:
    return value is None or type(value) is bool


def _is_str_or_none(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_int_or_none(value: Any) -> bool:
    # ``bool`` is an ``int`` subclass in Python; exclude it explicitly.
    return value is None or (isinstance(value, int) and type(value) is not bool)


def _is_str_list_or_none(value: Any) -> bool:
    return value is None or (isinstance(value, list) and all(isinstance(v, str) for v in value))


# (section, key) -> predicate. ``section`` None means a top-level key.
_TYPE_RULES: tuple[tuple[str | None, str, Callable[[Any], bool], str], ...] = (
    (None, "live_issue_authorizes_migration", _is_bool, "bool"),
    ("failure_evidence", "cause", _is_str_or_none, "str|null"),
    ("failure_evidence", "repair_command", _is_str_or_none, "str|null"),
    ("failure_evidence", "required_models", _is_str_list_or_none, "list[str]|null"),
    ("failure_evidence", "missing_models", _is_str_list_or_none, "list[str]|null"),
    ("effective_env", "claude_gpt_home", _is_str_or_none, "str|null"),
    ("effective_env", "override_vars_present", _is_bool, "bool"),
    ("probes", "install_dir_writable", _is_bool_or_none, "bool|null"),
    ("probes", "host_reachable", _is_bool_or_none, "bool|null"),
    ("capability_flags", "needs_credential", _is_bool, "bool"),
    ("capability_flags", "needs_secret", _is_bool, "bool"),
    ("capability_flags", "needs_privilege", _is_bool, "bool"),
    ("capability_flags", "destructive_or_global", _is_bool, "bool"),
    ("worker_result", "status", _is_str_or_none, "str|null"),
    ("worker_result", "reason_code", _is_str_or_none, "str|null"),
    ("worker_result", "exit_code", _is_int_or_none, "int|null"),
    ("worker_result", "deny_evidence_verified", _is_bool, "bool"),
    ("worker_result", "sudo_required_in_log", _is_bool, "bool"),
)

_SECTION_KEYS = ("failure_evidence", "effective_env", "probes", "capability_flags", "worker_result")


def find_payload_type_violations(payload: Any) -> list[str]:
    """Return a list of ``path: expected <type>`` strings for every fixed key
    that is present with a primitive type outside the fixed schema. Empty
    list means the payload is type-clean. Absent keys are NOT violations."""

    if not isinstance(payload, dict):
        return ["<payload>: expected object"]
    violations: list[str] = []
    for section in _SECTION_KEYS:
        if section in payload and payload[section] is not None and not isinstance(payload[section], dict):
            violations.append(f"{section}: expected object|null")
    for section, key, predicate, expected in _TYPE_RULES:
        container: Any = payload if section is None else payload.get(section)
        if not isinstance(container, dict) or key not in container:
            continue
        if not predicate(container[key]):
            path = key if section is None else f"{section}.{key}"
            violations.append(f"{path}: expected {expected}")
    return violations


def classify_runtime_migration(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify a runtime-migration failure/repair situation into exactly one
    of ``agent_executable_migration`` / ``implementation_defect`` /
    ``human_capability_blocker``. Pure function: no I/O, no env reads, no
    subprocess. See module docstring for the fixed input/output schema.

    Primitive types of the fixed keys are validated first (P1-C): a present
    key with an out-of-schema type (e.g. the JSON string ``"false"`` where a
    bool belongs) fails closed to ``implementation_defect`` /
    ``malformed_input_type_fail_closed`` and is never agent-executable."""

    if find_payload_type_violations(payload):
        return _implementation_defect_result(route=_ROUTE_MALFORMED_INPUT_TYPE)

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


# --- Pre-repair identity binding (Issue #2810 fix_delta P1-B) ---------------


def resolve_effective_claude_gpt_home(env: Mapping[str, str]) -> str:
    """Normalize the effective ``CLAUDE_GPT_HOME`` to an absolute path.

    ``CLAUDE_GPT_HOME`` unset/empty -> the launcher default ``~/.claude-gpt``
    (``~`` expanded from ``env["HOME"]``, falling back to the process home).
    A leading ``~`` is expanded from the injected ``env`` (not the process
    env) so the function stays a pure function of ``env``. The result is
    ``os.path.abspath`` (lexical normalization; symlinks are NOT resolved so
    that root and worker derive the identical string)."""

    raw = env.get("CLAUDE_GPT_HOME") or ""
    if not raw:
        raw = "~/.claude-gpt"
    if raw == "~" or raw.startswith("~/"):
        home = env.get("HOME") or os.path.expanduser("~")
        raw = home + raw[1:]
    return os.path.abspath(raw)


def verify_pre_repair_binding(
    expected_home: Any,
    effective_home: Any,
    pre_repair_evidence: Any,
    current_head: Any,
) -> dict[str, Any]:
    """Deterministic pre-mutation check run BEFORE ``repair_proxy.sh``.

    All of the following must hold, otherwise the repair MUST NOT start:

      1. ``expected_home`` (request ``expected_claude_gpt_home``) is an
         absolute path string;
      2. it equals the worker's effective ``CLAUDE_GPT_HOME`` (already
         normalized by ``resolve_effective_claude_gpt_home``) exactly;
      3. ``pre_repair_evidence`` (the parsed inline-JSON
         ``pre_repair_evidence_ref``) is a JSON object whose
         ``claude_gpt_home_absolute_path`` equals the effective home and whose
         ``repo_head`` equals ``current_head`` (the current repository HEAD).

    Extra keys in the evidence (e.g. ``launch_sh_sha256``) are accepted and
    ignored here. Returns ``{"bound": bool, "mismatches": [str, ...]}``."""

    mismatches: list[str] = []

    if not isinstance(expected_home, str) or not expected_home:
        mismatches.append("expected_claude_gpt_home_missing")
    elif not os.path.isabs(expected_home):
        mismatches.append("expected_claude_gpt_home_not_absolute")
    elif not isinstance(effective_home, str) or expected_home != effective_home:
        mismatches.append("effective_claude_gpt_home_mismatch")

    if not isinstance(pre_repair_evidence, dict):
        mismatches.append("pre_repair_evidence_missing_or_malformed")
    else:
        evidence_home = pre_repair_evidence.get("claude_gpt_home_absolute_path")
        evidence_head = pre_repair_evidence.get("repo_head")
        if not isinstance(evidence_home, str) or not evidence_home:
            mismatches.append("pre_repair_evidence_home_missing")
        elif evidence_home != effective_home:
            mismatches.append("pre_repair_evidence_home_mismatch")
        if not isinstance(evidence_head, str) or not evidence_head:
            mismatches.append("pre_repair_evidence_repo_head_missing")
        elif not isinstance(current_head, str) or not current_head:
            mismatches.append("current_repo_head_unavailable")
        elif evidence_head != current_head:
            mismatches.append("pre_repair_evidence_repo_head_mismatch")

    return {"bound": len(mismatches) == 0, "mismatches": mismatches}


# --- Root-owned input materializer (Issue #2810 fix_delta P1-A) --------------

OVERRIDE_ENV_VARS = (
    "CLAUDE_CODE_PROXY_INSTALL_DIR",
    "CLAUDE_CODE_PROXY_VERSION",
    "CLAUDE_GPT_REPAIR_INSTALLER_URL",
)

LAUNCH_RESULT_SCHEMA = "CLAUDE_GPT_LAUNCH_RESULT_V1"


class MaterializeError(ValueError):
    """Evidence handed to the materializer is missing/malformed (fail-closed)."""


# Authorization predicate for ``live_issue_authorizes_migration``. See
# ``issue_authorizes_repair_migration``.
_REPAIR_LITERAL_RE = re.compile(
    re.escape(EXACT_REPAIR_COMMAND)
    # no glued suffix (e.g. ``repair_proxy.sh.bak``) and no added arguments
    + r"(?![-A-Za-z0-9_./$])(?!\s+[-A-Za-z0-9_./$])"
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_ALLOW_MARKERS = (
    "agent-executable",
    "agent が実行してよい",
    "agent が実行可能",
    "agent 実行を許可",
    "agent による実行を許可",
    "実行を許可する",
)
_DENY_MARKERS = (
    "禁止",
    "してはならない",
    "不可",
    "許可しない",
    "human operator",
    "人間が",
    "手動",
    "Out of Scope",
    "Out of scope",
    "対象外",
    "not authorized",
    "must not",
    "do not",
)


def issue_authorizes_repair_migration(issue_body: str | None) -> bool:
    """Deterministic predicate for ``live_issue_authorizes_migration``.

    True ONLY when the live Issue body states, on a single non-code-fence
    line, the exact literal ``bash scripts/claude-gpt/repair_proxy.sh``
    (glued to no suffix and with no added arguments) TOGETHER WITH an
    explicit agent-execution allowance marker (``agent-executable`` /
    ``agent が実行してよい`` / ...), and NO non-code-fence line mentioning the
    literal carries a deny/ambiguity marker (``禁止`` / ``手動`` /
    ``human operator`` / ``Out of Scope`` / ...). Anything else -- body
    missing, literal only inside a fenced code block (e.g. a VC command),
    allowance absent, or any conflicting mention -- is False (fail-closed).

    This matches the ``ランタイム依存 migration の ownership 明記規則``
    (create-issue ``references/body-authoring.md``): an Issue that wants the
    agent to run the bounded repair writes one line such as
    ``agent-executable bounded repair として `bash scripts/claude-gpt/repair_proxy.sh` を agent が実行してよい``."""

    if not isinstance(issue_body, str) or not issue_body.strip():
        return False

    in_fence = False
    allowed = False
    for line in issue_body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or not _REPAIR_LITERAL_RE.search(line):
            continue
        if any(marker in line for marker in _DENY_MARKERS):
            return False
        if any(marker in line for marker in _ALLOW_MARKERS):
            allowed = True
    return allowed


def materialize_effective_env(env: Mapping[str, str]) -> dict[str, Any]:
    """``effective_env`` of the classifier payload from an injected ``env``.

    ``claude_gpt_home`` is the absolute-normalized effective
    ``CLAUDE_GPT_HOME`` (default ``~/.claude-gpt``).
    ``override_vars_present`` is True when ANY of ``OVERRIDE_ENV_VARS`` is
    present in ``env`` (even if empty -- fail-closed); ``CLAUDE_GPT_HOME``
    itself is deliberately NOT an override (Issue #2810 Outcome 1)."""

    return {
        "claude_gpt_home": resolve_effective_claude_gpt_home(env),
        "override_vars_present": any(name in env for name in OVERRIDE_ENV_VARS),
    }


def probe_host_reachable(
    claude_gpt_home: str, install_probe: Mapping[str, Any], operator_host_differs: bool
) -> bool | None:
    """Deterministic definition of ``probes.host_reachable``.

    False: the caller states the operator/target runtime host differs from the
    execution host (``--operator-host-differs``). True: the nearest existing
    ancestor of ``<CLAUDE_GPT_HOME>/bin`` (the very path the
    ``install_dir_writable`` probe inspected) exists as a directory on THIS
    execution host. None (indeterminate -> the classifier fails closed to
    ``probe_result_indeterminate``): that ancestor cannot be stat'ed or is not
    a directory."""

    del claude_gpt_home  # the nearest-ancestor path is taken from install_probe
    if operator_host_differs:
        return False
    try:
        return True if Path(str(install_probe.get("probed_path", ""))).is_dir() else None
    except OSError:
        return None


_SUDO_REQUIRED_RE = re.compile(r"sudo\s+required", re.IGNORECASE)


def _validate_failure_evidence(launch_result: Any) -> dict[str, Any]:
    if not isinstance(launch_result, dict):
        raise MaterializeError("failure evidence must be a JSON object")
    if launch_result.get("schema") != LAUNCH_RESULT_SCHEMA:
        raise MaterializeError(f"failure evidence schema must be {LAUNCH_RESULT_SCHEMA}")
    evidence: dict[str, Any] = {}
    for key in ("cause", "repair_command"):
        if key in launch_result:
            if not isinstance(launch_result[key], str):
                raise MaterializeError(f"failure evidence {key} must be a string")
            evidence[key] = launch_result[key]
    for key in ("required_models", "missing_models"):
        if key in launch_result:
            if not _is_str_list_or_none(launch_result[key]):
                raise MaterializeError(f"failure evidence {key} must be a list of strings")
            evidence[key] = launch_result[key]
    return evidence


def _chatgpt_auth_unavailable(*candidates: Any) -> bool:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        auth = candidate.get("chatgpt_auth")
        if isinstance(auth, dict) and auth.get("available") is False:
            return True
    return False


def materialize_classifier_payload(
    *,
    launch_result: Any,
    issue_body: str | None,
    env: Mapping[str, str],
    preflight: Any = None,
    install_log: str | None = None,
    worker_result: Any = None,
    operator_host_differs: bool = False,
    install_probe: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the fixed-key-set classifier payload from CURRENT evidence.

    Derivation (deterministic; no LLM judgement):

      - ``failure_evidence``: ``cause`` / ``repair_command`` /
        ``required_models`` / ``missing_models`` copied verbatim from the
        ``CLAUDE_GPT_LAUNCH_RESULT_V1`` receipt after type validation
        (absent keys stay absent -> the classifier fails closed).
      - ``live_issue_authorizes_migration``:
        ``issue_authorizes_repair_migration(issue_body)``.
      - ``effective_env``: ``materialize_effective_env(env)``.
      - ``probes.install_dir_writable``: ``probe_install_dir_writable()``
        called against the effective home. ``probes.host_reachable``:
        ``probe_host_reachable()``.
      - ``capability_flags``: ``needs_credential`` is True iff the preflight
        (``preflight`` file or ``launch_result["preflight"]``) reports
        ``chatgpt_auth.available == false``; ``needs_privilege`` is True iff
        ``install_log`` contains ``sudo required``. ``needs_secret`` and
        ``destructive_or_global`` have NO deterministic evidence source here
        and are always False (never fabricated); the classifier's other
        gates (exact literal command, writable/reachable target, no
        override env) bound the action.
      - ``worker_result``: the root-supplied object verbatim (or null).

    Raises ``MaterializeError`` when evidence is unusable."""

    evidence = _validate_failure_evidence(launch_result)
    if worker_result is not None and not isinstance(worker_result, dict):
        raise MaterializeError("worker_result must be a JSON object or null")
    if preflight is not None and not isinstance(preflight, dict):
        raise MaterializeError("preflight must be a JSON object or null")

    effective_env = materialize_effective_env(env)
    probe = (install_probe or probe_install_dir_writable)(effective_env["claude_gpt_home"])

    payload: dict[str, Any] = {
        "failure_evidence": evidence,
        "live_issue_authorizes_migration": issue_authorizes_repair_migration(issue_body),
        "effective_env": effective_env,
        "probes": {
            "install_dir_writable": bool(probe.get("writable")),
            "host_reachable": probe_host_reachable(
                effective_env["claude_gpt_home"], probe, operator_host_differs
            ),
        },
        "capability_flags": {
            "needs_credential": _chatgpt_auth_unavailable(preflight, launch_result.get("preflight")),
            "needs_secret": False,
            "needs_privilege": bool(install_log and _SUDO_REQUIRED_RE.search(install_log)),
            "destructive_or_global": False,
        },
        "worker_result": worker_result,
    }
    violations = find_payload_type_violations(payload)
    if violations:
        raise MaterializeError("materialized payload violates fixed types: " + "; ".join(violations))
    return payload


def _read_json_file(path: str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MaterializeError(f"{label} unreadable or not JSON: {exc}") from exc


def _fetch_live_issue_body(issue_number: int, repo: str | None) -> str | None:
    cmd = ["gh", "issue", "view", str(issue_number), "--json", "body", "--jq", ".body"]
    if repo:
        cmd += ["--repo", repo]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _fail(error: str, detail: str, code: int = 2) -> int:
    print(json.dumps({"error": error, "detail": detail}), file=sys.stderr)
    return code


def _run_materialize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="classify_runtime_migration.py materialize")
    parser.add_argument("--failure-evidence-file", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--issue-body-file")
    source.add_argument("--issue-number", type=int)
    parser.add_argument("--repo")
    parser.add_argument("--preflight-file")
    parser.add_argument("--install-log-file")
    parser.add_argument("--worker-result-file")
    parser.add_argument("--operator-host-differs", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    try:
        launch_result = _read_json_file(args.failure_evidence_file, "failure evidence")
        preflight = _read_json_file(args.preflight_file, "preflight") if args.preflight_file else None
        worker_result = (
            _read_json_file(args.worker_result_file, "worker_result") if args.worker_result_file else None
        )
        install_log = None
        if args.install_log_file:
            try:
                install_log = Path(args.install_log_file).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise MaterializeError(f"install log unreadable: {exc}") from exc
        issue_body: str | None = None
        if args.issue_body_file:
            try:
                issue_body = Path(args.issue_body_file).read_text(encoding="utf-8")
            except OSError:
                issue_body = None  # unreadable -> not authorized (fail-closed)
        elif args.issue_number is not None:
            issue_body = _fetch_live_issue_body(args.issue_number, args.repo)
        payload = materialize_classifier_payload(
            launch_result=launch_result,
            issue_body=issue_body,
            env=os.environ,
            preflight=preflight,
            install_log=install_log,
            worker_result=worker_result,
            operator_host_differs=args.operator_host_differs,
        )
    except MaterializeError as exc:
        return _fail("materialize_evidence_invalid", str(exc))

    print(json.dumps(payload))
    return 0


def _current_repo_head() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and head else None


def _run_pre_repair_check(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="classify_runtime_migration.py pre-repair-check")
    parser.add_argument("--expected-claude-gpt-home", required=True)
    parser.add_argument("--pre-repair-evidence-json", required=True)
    parser.add_argument("--current-head", help="override for tests; default: git rev-parse HEAD")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    try:
        evidence: Any = json.loads(args.pre_repair_evidence_json)
    except ValueError:
        evidence = None
    effective_home = resolve_effective_claude_gpt_home(os.environ)
    current_head = args.current_head if args.current_head else _current_repo_head()
    verdict = verify_pre_repair_binding(
        args.expected_claude_gpt_home, effective_home, evidence, current_head
    )
    print(
        json.dumps(
            {
                "status": "ok" if verdict["bound"] else "blocked",
                "reason_code": None if verdict["bound"] else "identity_mismatch",
                "mismatches": verdict["mismatches"],
                "effective_claude_gpt_home": effective_home,
            }
        )
    )
    return 0 if verdict["bound"] else 1


def _run_classify(argv: list[str]) -> int:
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


def _run_cli(argv: list[str]) -> int:
    if not argv:
        return _run_classify(argv)
    if argv[0] == "materialize":
        return _run_materialize(argv[1:])
    if argv[0] == "pre-repair-check":
        return _run_pre_repair_check(argv[1:])
    return _fail("unknown_subcommand", argv[0])


if __name__ == "__main__":
    raise SystemExit(_run_cli(sys.argv[1:]))
