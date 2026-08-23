"""scripts/claude-gpt/workflow_capability_preflight.py

Issue #2273: `--workflow-profile issue-to-impl` workflow capability
preflight. Produces the `CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1` structured
result described in the Issue's `## Result Schema` section.

This module judges, SEPARATELY:

  - trusted `uv` availability (delegated to
    `scripts/agent-guards/trusted_runtime_capabilities.py`, which itself
    delegates to the canonical `skill_runtime_exec` resolver -- no second
    resolver is introduced here)
  - Spark delegation route capability (`not_required` / `eligible` /
    `fallback_only` / `unavailable`) -- a static, observable-config based
    judgment, NOT a runtime model-identity proof (that remains Child A
    `#2274`'s `resolvedModel`-based responsibility)
  - GitHub READ capability (`gh auth status` + a repository read probe)
  - GitHub WRITE (mutation) capability, evaluated PER planned operation
    (`phase`/`actor_role`/`operation`) against a small, invocation-scoped
    `planned_operations` list the caller supplies -- never a persistent
    actor/operation authorization registry (Issue #2223 Owner Decision:
    no second capability registry)

`permission` is always reported as `"unverified"`: this module never
proves GitHub server-side write permission -- that is established later by
a mutation-capable actor's pre/post live readback (Issue #2223 model).

This module performs NO GitHub state mutation of its own (AC12): every
GitHub call it makes is a read (`gh auth status`, `gh repo view`).

Exit code contract: a well-formed assessment (including `decision:
blocked`) exits 0. Non-zero exit codes are reserved for invalid input /
internal errors, so a `set -e` caller does not lose the JSON payload.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"

if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import trusted_runtime_capabilities as trusted_uv_mod  # noqa: E402

SCHEMA = "CLAUDE_GPT_WORKFLOW_CAPABILITIES_V1"
SUPPORTED_PROFILES = ("issue-to-impl",)

DECISION_READY = "ready"
DECISION_DEGRADED = "degraded"
DECISION_BLOCKED = "blocked"

SPARK_NOT_REQUIRED = "not_required"
SPARK_ELIGIBLE = "eligible"
SPARK_FALLBACK_ONLY = "fallback_only"
SPARK_UNAVAILABLE = "unavailable"

# A small, static registry of GitHub operations this repository has an
# actual implemented route for (Issue #2223: NOT a persistent
# actor/operation authorization registry -- just "does an implementation
# route exist", the same way `_resolve_trusted_executable` answers "does a
# trusted binary exist" rather than "is this caller allowed to run it").
# `actor_role` authorization itself remains a workflow/Agent-contract
# responsibility, not this preflight's.
_KNOWN_OPERATION_ROUTES = frozenset(
    {
        "issue_comment",
        "issue_edit",
        "issue_create",
        "pr_comment",
        "pr_create",
        "pr_edit",
        "pr_update_branch",
    }
)

_DEFAULT_REPO = "squne121/loop-protocol"


def _run_env_only_preflight() -> dict:
    """Reuse the existing `preflight.sh --env-only` lane (binary_available +
    chatgpt_auth.available) as the observable input for the Spark route
    judgment, instead of re-implementing proxy binary/auth discovery here
    (single source of truth for those two checks stays in `lib.sh`)."""
    preflight_sh = _SCRIPT_DIR / "preflight.sh"
    try:
        proc = subprocess.run(
            ["sh", str(preflight_sh), "--env-only"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return json.loads(proc.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}


def _spark_capability(
    spark_mode: str | None,
    spark_fallback: str | None,
    env_only_result: dict,
) -> str:
    """Judge Spark route capability from a caller-declared directive
    (`spark_mode`/`spark_fallback`) plus observable proxy binary/auth
    state. This is a "no known blocking reason" judgment, not a runtime
    model-identity proof (Issue #2273 In Scope note)."""
    if not spark_mode:
        return SPARK_NOT_REQUIRED

    binary_available = bool(env_only_result.get("binary_available"))
    auth_available = bool(env_only_result.get("chatgpt_auth", {}).get("available"))
    known_blocking_reason = not (binary_available and auth_available)

    if not known_blocking_reason:
        return SPARK_ELIGIBLE

    if spark_fallback == "allowed":
        return SPARK_FALLBACK_ONLY
    # spark_fallback == "forbidden" (or unspecified/malformed): fail closed.
    return SPARK_UNAVAILABLE


def _github_auth_ok() -> bool:
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=30, check=False
        )
        return proc.returncode == 0
    except OSError:
        return False


def _github_repo_read_ok(repo: str) -> bool:
    try:
        proc = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "name"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False


def _operation_route(operation: str) -> str:
    return "available" if operation in _KNOWN_OPERATION_ROUTES else "unavailable"


def _load_planned_operations(path: str | None) -> list[dict]:
    """Load and validate the caller-supplied planned-operations JSON.

    When `path` is omitted (None/empty), an empty list is the legitimate
    default (no planned mutation-requiring operations declared). When a path
    IS explicitly supplied, any malformation (unreadable file, invalid JSON,
    wrong top-level shape, malformed entry) is a caller input error and MUST
    raise `ValueError` rather than silently degrade to an empty list -- a
    silent `[]` would make a genuinely mutation-requiring operation set
    disappear and let `assess()` PASS as if no mutation were planned (P1-3
    fix)."""

    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise ValueError(f"cannot read planned-operations-json file {path!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"planned-operations-json is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("planned-operations-json top-level value must be a JSON list")
    normalized = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(f"planned-operations-json entry is not an object: {entry!r}")
        operation = entry.get("operation")
        phase = entry.get("phase")
        actor_role = entry.get("actor_role")
        if not isinstance(operation, str) or not operation:
            raise ValueError(f"planned-operations-json entry missing non-empty 'operation': {entry!r}")
        if "requires_mutation" in entry and not isinstance(entry["requires_mutation"], bool):
            raise ValueError(
                f"planned-operations-json entry 'requires_mutation' must be a bool: {entry!r}"
            )
        normalized.append(
            {
                "phase": phase if isinstance(phase, str) else "unknown",
                "actor_role": actor_role if isinstance(actor_role, str) else "unknown",
                "operation": operation,
                # `requires_mutation` defaults to True: an operation
                # entry that doesn't explicitly mark itself read-only is
                # treated as mutation-requiring (fail closed).
                "requires_mutation": bool(entry.get("requires_mutation", True)),
            }
        )
    return normalized


def assess(
    *,
    project_root: str,
    profile: str,
    repo: str,
    spark_mode: str | None,
    spark_fallback: str | None,
    planned_operations: list[dict],
) -> dict:
    reasons: list[str] = []

    uv_result = trusted_uv_mod.check_trusted_uv(project_root)
    uv_ok = uv_result["status"] == trusted_uv_mod.STATUS_OK
    if not uv_ok:
        reasons.append(
            f"uv:{uv_result['status']}: install the pinned uv version to the account-home "
            "~/.local/bin (official standalone installer) or use the hostedtoolcache "
            "provisioned uv; see docs/dev/claude-gpt-runtime-prerequisites.md"
        )

    env_only_result = _run_env_only_preflight()
    spark_status = _spark_capability(spark_mode, spark_fallback, env_only_result)
    if spark_status == SPARK_UNAVAILABLE:
        reasons.append(
            "spark:unavailable: required Spark delegation route has no known-available "
            "binary/auth and fallback is forbidden by the directive; do not launch the "
            "SubAgent until the Spark route (claude-code-proxy binary + ChatGPT "
            "subscription auth) is available, or relax the directive to preferred/allowed"
        )
    elif spark_status == SPARK_FALLBACK_ONLY:
        reasons.append(
            "spark:fallback_only: Spark route is not currently available; the directive "
            "permits fallback so the workflow may proceed in degraded mode"
        )

    github_auth = _github_auth_ok()
    github_repo_read = github_auth and _github_repo_read_ok(repo)
    if not github_auth:
        reasons.append(
            "github:auth_unavailable: run `gh auth login` (or ensure GH_TOKEN/GH_CONFIG_DIR "
            "are populated) before starting a workflow that reads or mutates GitHub state"
        )
    elif not github_repo_read:
        reasons.append(
            f"github:repo_read_unavailable: `gh repo view {repo}` failed; verify repository "
            "access for the authenticated account"
        )

    operations: dict[str, dict] = {}
    missing_mutation_route = False
    for entry in planned_operations:
        route = _operation_route(entry["operation"])
        operations[entry["operation"]] = {"route": route, "permission": "unverified"}
        if route == "unavailable" and entry.get("requires_mutation", True):
            missing_mutation_route = True
            reasons.append(
                f"github:operation_route_unavailable:{entry['operation']}: no implemented "
                f"native GitHub route for phase={entry.get('phase', 'unknown')} actor_role="
                f"{entry.get('actor_role', 'unknown')}; do not start this mutation-requiring phase"
            )

    if missing_mutation_route or not github_auth or not github_repo_read:
        decision = DECISION_BLOCKED
    elif not uv_ok or spark_status == SPARK_UNAVAILABLE:
        decision = DECISION_BLOCKED
    elif spark_status == SPARK_FALLBACK_ONLY:
        decision = DECISION_DEGRADED
    else:
        decision = DECISION_READY

    return {
        "schema": SCHEMA,
        "profile": profile,
        "decision": decision,
        "checks": {
            "uv": {
                "status": uv_result["status"],
                "reason": uv_result["reason"],
                "diagnostic": uv_result.get("diagnostic"),
            },
            "spark": {"status": spark_status},
            "github": {
                "auth": github_auth,
                "repo_read": github_repo_read,
                "operations": operations,
            },
        },
        "reasons": reasons,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=SUPPORTED_PROFILES)
    parser.add_argument("--project-root", default=str(_REPO_ROOT))
    parser.add_argument("--repo", default=_DEFAULT_REPO)
    parser.add_argument("--spark-mode", default=None, choices=["required", "preferred", None])
    parser.add_argument("--spark-fallback", default=None, choices=["forbidden", "allowed", None])
    parser.add_argument("--planned-operations-json", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        planned_operations = _load_planned_operations(args.planned_operations_json)
    except ValueError as exc:
        error_payload = {
            "schema": SCHEMA,
            "error": "invalid_planned_operations_input",
            "detail": str(exc),
        }
        print(json.dumps(error_payload, ensure_ascii=False), file=sys.stderr)
        return 2

    result = assess(
        project_root=args.project_root,
        profile=args.profile,
        repo=args.repo,
        spark_mode=args.spark_mode,
        spark_fallback=args.spark_fallback,
        planned_operations=planned_operations,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
