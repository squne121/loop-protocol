#!/usr/bin/env python3
"""build_request.py — Build a delegation_request_v1 JSON for gemini-cli-headless-delegation.

Usage:
    uv run python3 build_request.py \\
      --profile <tool_profile> \\
      --objective <str> \\
      [--instruction <str> ...]  \\
      [--context-file <path> ...] \\
      [--gh-pr <N>] \\
      [--gh-issue <N>] \\
      [--output <path>]

    uv run python3 build_request.py model-policy \\
      --provider {gemini,agy,auto} \\
      [--role <role_name>] \\
      [--profile <TOOL_PROFILE>]   # required when --provider auto

Exit codes:
    0  Request JSON written and validated successfully (legacy invocation),
       or model-policy inspection completed successfully.
    1  Validation or usage error (failure JSON written to --output when provided,
       or printed to stdout for model-policy).
    2  Internal error.

The generated JSON conforms to delegation_request_v1 schema and is validated
against run_gemini_headless.validate_request before being written.

The `model-policy` subcommand is a read-only, no-side-effect inspector: it
never writes a request file. It dispatches only when argv[0] == "model-policy"
so the legacy `--profile`/`--objective` invocation above is completely
unaffected (Issue #1269).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA = "delegation_request_v1"
DEFAULT_INSTRUCTIONS_BY_PROFILE: dict[str, list[str]] = {
    "no_tools": [
        "Summarise the key findings from the provided context files.",
        "List any gaps or issues found with evidence.",
    ],
    "grounded_research": [
        "Search for authoritative sources relevant to the objective.",
        "Summarise findings with evidence and citations.",
    ],
    "local_asset_research": [
        "Use Serena MCP read-only tools to investigate the objective.",
        "List file paths and symbol names as evidence.",
    ],
    "proposal_only": [
        "Draft a proposal addressing the objective.",
        "Return the proposal as structured text only; do not execute commands.",
    ],
    "github_research": [
        "Investigate the GitHub resources relevant to the objective.",
        "Summarise findings with links to issues, PRs, or comments as evidence.",
    ],
}
DEFAULT_OUTPUT_SECTIONS_BY_PROFILE: dict[str, list[str]] = {
    "no_tools": ["Summary", "Findings", "Evidence"],
    "grounded_research": ["Summary", "Findings", "Evidence"],
    "local_asset_research": ["Summary", "Findings", "Evidence"],
    "proposal_only": ["implementation_draft"],
    "github_research": ["Summary", "Findings", "Evidence"],
}
VALID_PROFILES = frozenset(DEFAULT_INSTRUCTIONS_BY_PROFILE.keys())

# ---------------------------------------------------------------------------
# Loader: run_gemini_headless.validate_request
# ---------------------------------------------------------------------------


def _load_run_gemini_headless_module():
    """Dynamically load the run_gemini_headless.py module (fresh instance).

    Used both by the legacy request builder (to reach validate_request) and by
    the model-policy subcommand (to reach load_model_routing /
    resolve_model_chain / PROVIDER_AUTO_* constants) -- Issue #1269 Blocker 5:
    the runtime resolver is the single source of truth and must not be
    reimplemented here.

    Issue #1695 PR review (Blocker 1): ``spec.loader.exec_module()`` triggers
    CPython's normal bytecode-cache behaviour, which would otherwise write
    ``scripts/__pycache__/run_gemini_headless.cpython-*.pyc`` into the
    repository's runtime source directory -- a real filesystem side effect
    that the model-policy subcommand's "read-only, no-side-effect inspector"
    claim (module docstring, AC6) must not have, independent of whether the
    caller's environment happens to set ``PYTHONDONTWRITEBYTECODE``.
    ``sys.dont_write_bytecode`` is saved/restored around the load so this
    function never leaves global interpreter state changed for callers.
    """
    script_dir = Path(__file__).resolve().parent
    module_path = script_dir / "run_gemini_headless.py"
    spec = importlib.util.spec_from_file_location("run_gemini_headless", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load run_gemini_headless from {module_path}")
    module = importlib.util.module_from_spec(spec)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    return module


def _load_validate_request():
    """Dynamically load validate_request from run_gemini_headless.py."""
    return _load_run_gemini_headless_module().validate_request


# ---------------------------------------------------------------------------
# Failure JSON helpers
# ---------------------------------------------------------------------------


def _build_failure_json(
    failure_class: str,
    failure_reason: str,
    next_action_argv: list[str],
    next_action_command: str,
) -> dict[str, Any]:
    return {
        "schema": "build_request_failure_v1",
        "ok": False,
        "failure_class": failure_class,
        "failure_reason": failure_reason,
        "next_action": {
            "argv": next_action_argv,
            "command": next_action_command,
        },
    }


def _write_failure(
    output: Path | None,
    failure_class: str,
    failure_reason: str,
    next_action_argv: list[str],
) -> None:
    next_action_command = shlex.join(next_action_argv)
    payload = _build_failure_json(
        failure_class=failure_class,
        failure_reason=failure_reason,
        next_action_argv=next_action_argv,
        next_action_command=next_action_command,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Command-line reconstruction helpers
# ---------------------------------------------------------------------------


def _build_full_argv(
    profile: str,
    objective: str,
    instructions: list[str] | None,
    context_file: str,
    output: Path | None,
) -> list[str]:
    """Build the full argv needed to re-run build_request.py with given args.

    Used for next_action.argv in failure JSON so callers can retry the complete
    command rather than a stub (B3).
    """
    argv = [
        "uv", "run", "python3",
        ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py",
    ]
    if profile:
        argv += ["--profile", profile]
    if objective:
        argv += ["--objective", objective]
    if instructions:
        for inst in instructions:
            argv += ["--instruction", inst]
    argv += ["--context-file", context_file]
    if output is not None:
        argv += ["--output", str(output)]
    return argv


# ---------------------------------------------------------------------------
# Context file resolution
# ---------------------------------------------------------------------------


def _resolve_context_files(
    raw_paths: list[str],
    base_dir: Path,
    output: Path | None,
    profile: str = "",
    objective: str = "",
    instructions: list[str] | None = None,
) -> list[str] | None:
    """Resolve context files to absolute paths. Returns None on failure."""
    resolved: list[str] = []
    for raw in raw_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = (base_dir / raw).resolve()
        else:
            p = p.resolve()
        if not p.exists():
            # B2: use failure_class='context_file_missing' (matches Issue #313 contract)
            # B3: include complete argv so callers can re-run the full command
            _write_failure(
                output=output,
                failure_class="context_file_missing",
                failure_reason=f"context file not found: {raw} (resolved: {p})",
                next_action_argv=_build_full_argv(
                    profile=profile,
                    objective=objective,
                    instructions=instructions,
                    context_file=str(p),
                    output=output,
                ),
            )
            return None
        if not p.is_file():
            # B2/B3: same fix for is-not-a-file case
            _write_failure(
                output=output,
                failure_class="context_file_missing",
                failure_reason=f"context file is not a regular file: {raw} (resolved: {p})",
                next_action_argv=_build_full_argv(
                    profile=profile,
                    objective=objective,
                    instructions=instructions,
                    context_file=str(p),
                    output=output,
                ),
            )
            return None
        resolved.append(str(p))
    return resolved


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_request(
    profile: str,
    objective: str,
    instructions: list[str] | None,
    context_files: list[str] | None,
    gh_pr: int | None,
    gh_issue: int | None,
    output: Path | None,
    base_dir: Path | None = None,
) -> int:
    """Build and validate a delegation_request_v1.

    Returns exit code: 0 = success, 1 = validation/usage error, 2 = internal error.
    """
    if profile not in VALID_PROFILES:
        _write_failure(
            output=output,
            failure_class="invalid_profile",
            failure_reason=f"tool_profile '{profile}' is not valid; choose one of: {sorted(VALID_PROFILES)}",
            next_action_argv=["build_request.py", "--profile", "<valid_profile>", "--objective", objective],
        )
        return 1

    # Resolve base dir for relative context file paths
    cwd = base_dir or Path.cwd()

    # B5: --gh-pr / --gh-issue are only allowed with github_research profile.
    if (gh_pr is not None or gh_issue is not None) and profile != "github_research":
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason=(
                f"--gh-pr/--gh-issue (gh_commands) are only supported with github_research profile, got: {profile}"
            ),
            next_action_argv=_build_full_argv(
                profile="github_research",
                objective=objective,
                instructions=instructions,
                context_file="<context-file>",
                output=output,
            ),
        )
        return 1

    # B4: --instruction fail-closed when explicitly provided but count < 2.
    # When instructions is None, use profile defaults (OK).
    # When instructions is explicitly provided (non-None), require >= 2 entries.
    if instructions is not None and len(instructions) < 2:
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason="--instruction must be specified at least twice when provided explicitly",
            next_action_argv=_build_full_argv(
                profile=profile,
                objective=objective,
                instructions=instructions,
                context_file="<context-file>",
                output=output,
            ),
        )
        return 1

    # Resolve context_files
    raw_context = context_files or []
    if not raw_context:
        _write_failure(
            output=output,
            failure_class="context_file_missing",
            failure_reason=(
                "context_files is required (at least 1 file must be specified). "
                "Use --context-file <path> to add a context file."
            ),
            next_action_argv=_build_full_argv(
                profile=profile,
                objective=objective,
                instructions=instructions,
                context_file="<path>",
                output=output,
            ),
        )
        return 1
    resolved_context = _resolve_context_files(
        raw_paths=raw_context,
        base_dir=cwd,
        output=output,
        profile=profile,
        objective=objective,
        instructions=instructions,
    )
    if resolved_context is None:
        return 1

    # Resolve instructions: use profile defaults when not explicitly provided.
    effective_instructions = instructions if instructions is not None else DEFAULT_INSTRUCTIONS_BY_PROFILE[profile]

    # Resolve output sections
    output_sections = DEFAULT_OUTPUT_SECTIONS_BY_PROFILE[profile]

    # Build gh_commands if gh-pr or gh-issue specified (only for github_research — already validated above)
    gh_commands: list[dict[str, list[str]]] | None = None
    if gh_pr is not None or gh_issue is not None:
        gh_commands = []
        if gh_issue is not None:
            gh_commands.append({"argv": ["issue", "view", str(gh_issue)]})
        if gh_pr is not None:
            gh_commands.append({"argv": ["pr", "view", str(gh_pr)]})

    request: dict[str, Any] = {
        "schema": SCHEMA,
        "objective": objective,
        "instructions": effective_instructions,
        "tool_profile": profile,
        "output_sections": output_sections,
        "context_files": resolved_context,
        "timeout_sec": 600,
    }
    if gh_commands:
        request["gh_commands"] = gh_commands

    # Validate via run_gemini_headless.validate_request
    try:
        validate_request = _load_validate_request()
        validation_errors = validate_request(request, request_path=output)
    except Exception as exc:  # pylint: disable=broad-except
        _write_failure(
            output=output,
            failure_class="internal_error",
            failure_reason=f"Failed to load validate_request: {exc}",
            next_action_argv=["build_request.py", "--help"],
        )
        return 2

    if validation_errors:
        _write_failure(
            output=output,
            failure_class="validation_error",
            failure_reason=validation_errors[0],
            next_action_argv=["build_request.py", "--profile", profile, "--objective", objective, "--help"],
        )
        return 1

    # Write output
    payload_str = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload_str, encoding="utf-8")
        print(f"[build_request] request written to: {output}")
    else:
        print(payload_str, end="")

    return 0


# ---------------------------------------------------------------------------
# model-policy subcommand (Issue #1269)
#
# Read-only, no-side-effect inspector: it never writes a request file and
# never generates a delegation_request_v1. It calls run_gemini_headless's
# load_model_routing() / resolve_model_chain() directly (Blocker 5) so the
# runtime resolver stays the single source of truth for YAML parsing,
# default-merge, and precedence. AGY has no configurable model chain at
# runtime (run_delegation() never calls resolve_model_chain() for
# provider="agy"), so this inspector mirrors that: resolved_chain is only
# ever populated for the gemini resolution path, actual_model is always
# null (this is a dry-run; nothing was executed), and "agy-default" -- the
# literal actual_model the real runtime returns for AGY -- is surfaced only
# as legacy_compatibility_label (Blocker 4).
# ---------------------------------------------------------------------------

MODEL_POLICY_SCHEMA = "delegation_model_policy/v1"
MODEL_POLICY_PROVIDERS: tuple[str, ...] = ("gemini", "agy", "auto")


class _ModelPolicyArgumentError(Exception):
    """Raised by _ModelPolicyArgumentParser.error() instead of SystemExit(2).

    Issue #1695 PR review (Major 1): argparse's default ``error()`` prints
    usage text to stderr and calls ``sys.exit(2)``, which drops model-policy
    out of its own delegation_model_policy/v1 JSON contract on a CLI usage
    error (invalid --provider, unknown option, etc). Raising this exception
    instead lets main_model_policy() convert the failure into a normal
    schema-conformant failure payload on stdout.
    """


class _ModelPolicyArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ModelPolicyArgumentError(message)


def _resolve_gemini_chain(
    rgh: Any,
    role: str | None,
    routing: dict[str, Any],
) -> tuple[list[str], str | None]:
    """Call run_gemini_headless.resolve_model_chain() directly (no reimplementation)."""
    request: dict[str, Any] = {"role": role} if role else {}
    return rgh.resolve_model_chain(request, routing)


def _agy_provider_info(role: str | None) -> dict[str, Any]:
    """Describe AGY's fixed (non-configurable) model policy for the dry-run inspector.

    actual_model is always null here -- this is inspection, not execution --
    and "agy-default" (the literal actual_model the real runtime returns for
    provider=agy) is surfaced only as legacy_compatibility_label, never as
    actual_model itself (Blocker 4).

    Issue #1695 PR review (Blocker 2): this function never calls
    load_model_routing() / resolve_model_chain() and is never given a
    ``rgh`` module handle -- AGY has no model-chain concept at runtime
    (_run_delegation_core() dispatches provider="agy" entirely before
    Gemini's routing/validation), so a broken model_routing.yaml, or any
    routing config at all, must never affect this branch.

    Issue #1695 PR review (Major 3): upstream_capability now distinguishes
    "the wrapper does not document upstream supporting explicit model
    selection" (documented_explicit_model_selection) from "we never probed
    the installed CLI's version" (installed_version / installed_version_probed).
    configured_chain / readiness_checked / credentials_checked /
    provider_available make explicit that this is a static, offline
    inspection -- none of them reflect a live probe of the agy CLI
    (implementing a live probe is out of scope for this dry-run inspector).
    """
    info: dict[str, Any] = {
        "provider": "agy",
        "resolved_chain": None,
        "configured_chain": None,
        "actual_model": None,
        "legacy_compatibility_label": "agy-default",
        "wrapper_capability": {
            "explicit_model_selection": False,
            "role_based_model_chain": False,
        },
        "upstream_capability": {
            "probed": False,
            "documented_explicit_model_selection": False,
            "installed_version": None,
            "installed_version_probed": False,
            "note": (
                "model-policy is a dry-run inspector; it does not invoke the "
                "agy CLI, so upstream (Antigravity CLI) model capability is "
                "not probed."
            ),
        },
        "readiness_checked": False,
        "credentials_checked": False,
        "provider_available": None,
    }
    if role:
        info["role_applied"] = False
        info["role_note"] = (
            "role has no effect for provider=agy: the AGY wrapper does not "
            "support role-based model chains, and run_delegation() never "
            "calls resolve_model_chain() for provider=agy."
        )
    return info


def _config_invalid_payload(base: dict[str, Any], exc: ValueError) -> dict[str, Any]:
    return {
        **base,
        "ok": False,
        "failure_class": "config_invalid",
        "reason_code": "routing_config_invalid",
        "failure_reason": str(exc),
    }


def _resolve_gemini_chain_or_failure(
    rgh: Any,
    role: str | None,
    config_path: Path | None,
    base: dict[str, Any],
) -> tuple[list[str] | None, dict[str, Any] | None, int]:
    """Load routing and resolve the Gemini chain, or build a failure payload.

    Returns (chain, failure_payload, failure_exit_code). Exactly one of
    (chain, failure_payload) is non-None: failure_payload is None on
    success, chain is None on any failure. Shared by provider="gemini" and
    provider="auto" (profile-eligible) -- both resolve the chain the same
    way (Issue #1695 PR review: extracted to avoid duplicating the
    routing-load / resolve-chain / failure-classification logic).
    """
    try:
        routing = rgh.load_model_routing(config_path=config_path)
    except ValueError as exc:
        return None, _config_invalid_payload(base, exc), 1

    chain, error = _resolve_gemini_chain(rgh, role, routing)
    if error:
        failure_class = error.split(":", 1)[0].strip()
        payload = {
            **base,
            "ok": False,
            "failure_class": failure_class,
            "failure_reason": error,
        }
        return None, payload, 1
    return chain, None, 0


def build_model_policy(
    provider: str,
    role: str | None,
    profile: str | None,
    config_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Build a delegation_model_policy/v1 payload without any side effects.

    Returns (payload, exit_code). exit_code follows the same convention as
    build_request(): 0 = success, 1 = validation/usage error (including
    unknown_role / empty_chain / routing_config_invalid / missing --profile
    for provider=auto / invalid provider), 2 = internal error.

    config_path is not exposed on the CLI (Issue #1269 In Scope is limited to
    --provider/--role/--profile); it exists so tests can inject a hermetic
    model_routing.yaml override without touching the real config file.

    Issue #1695 PR review (Blocker 2): control flow here mirrors
    run_gemini_headless.py's own dispatch order, not an independent
    ordering invented for this inspector:
      1. provider is validated against MODEL_POLICY_PROVIDERS up front
         (Minor fix) -- the Python API can be called directly, bypassing
         argparse's ``choices`` constraint.
      2. provider="agy" is dispatched immediately, with no routing config
         ever loaded -- matching _run_delegation_core()'s early agy branch,
         which runs entirely before Gemini validation/routing.
      3. provider="auto" checks --profile presence, then
         PROVIDER_AUTO_ELIGIBLE_PROFILES eligibility -- BOTH before loading
         any routing config -- matching provider_auto_dispatch(), which
         returns a "no provider attempted" result (and therefore never
         loads routing) for an ineligible profile.
      4. Only provider="gemini", or provider="auto" with an eligible
         profile, ever call load_model_routing() / resolve_model_chain().
    """
    base: dict[str, Any] = {
        "schema": MODEL_POLICY_SCHEMA,
        "provider": provider,
        "role": role,
        "profile": profile,
    }

    if provider not in MODEL_POLICY_PROVIDERS:
        payload = {
            **base,
            "ok": False,
            "failure_class": "invalid_provider",
            "failure_reason": (
                f"provider must be one of {list(MODEL_POLICY_PROVIDERS)}, got {provider!r}"
            ),
        }
        return payload, 1

    if provider == "agy":
        info = _agy_provider_info(role)
        payload = {
            **base,
            "ok": True,
            "failure_class": None,
            "failure_reason": None,
            **info,
        }
        return payload, 0

    if provider == "auto":
        if not profile:
            payload = {
                **base,
                "ok": False,
                "failure_class": "profile_required_for_auto",
                "failure_reason": (
                    "--provider auto requires --profile: dry-run inspection "
                    "mirrors runtime provider_auto_policy_v1 eligibility gating "
                    "(PROVIDER_AUTO_ELIGIBLE_PROFILES), which is profile-scoped."
                ),
            }
            return payload, 1

        try:
            rgh = _load_run_gemini_headless_module()
        except Exception as exc:  # pylint: disable=broad-except
            payload = {
                **base,
                "ok": False,
                "failure_class": "internal_error",
                "failure_reason": f"Failed to load run_gemini_headless: {exc}",
            }
            return payload, 2

        runtime_order = list(rgh.PROVIDER_AUTO_RUNTIME_ORDER)
        profile_eligible = profile in rgh.PROVIDER_AUTO_ELIGIBLE_PROFILES

        if not profile_eligible:
            # Mirrors provider_auto_dispatch()'s own stop-condition: no
            # provider is attempted at all for an ineligible profile, so no
            # routing config is loaded and no chain is resolved here either
            # -- a broken model_routing.yaml or an unknown --role must not
            # fail this branch.
            payload = {
                **base,
                "ok": True,
                "failure_class": None,
                "failure_reason": None,
                "runtime_order": runtime_order,
                "profile_eligible": False,
                "provider_candidates": None,
                "consumer_constraints": None,
            }
            return payload, 0

        chain, failure_payload, failure_exit = _resolve_gemini_chain_or_failure(
            rgh, role, config_path, base
        )
        if failure_payload is not None:
            return failure_payload, failure_exit

        provider_candidates: list[dict[str, Any]] = []
        for candidate in runtime_order:
            if candidate == "gemini":
                provider_candidates.append(
                    {
                        "provider": "gemini",
                        "resolved_chain": chain,
                        "actual_model": None,
                    }
                )
            elif candidate == "agy":
                provider_candidates.append(_agy_provider_info(role))
            else:
                provider_candidates.append(
                    {"provider": candidate, "resolved_chain": None, "actual_model": None}
                )

        consumer_constraints = {
            # Major 2 (Issue #1695 PR review): provider_auto_dispatch()
            # attempts exactly one provider at a time because
            # PROVIDER_AUTO_RETRYABLE_FAILURE_CLASSES / get_retry_budget()
            # define per-provider attempt/backoff budgets -- running two
            # providers concurrently would make attempts_by_model /
            # provider_attempts unauditable and would let a single request
            # exceed its configured retry budget across providers (not
            # merely "it happens to run sequentially"). reason_code is read
            # from run_gemini_headless.py's own exported constant so this
            # value cannot drift from the runtime's own definition.
            "fan_out": {
                "supported": False,
                "reason_code": rgh.PROVIDER_AUTO_FAN_OUT_UNSUPPORTED_REASON_CODE,
            },
            # _validate_agy_request() rejects an empty/missing "prompt" field
            # (agy_empty_prompt) for every AGY-eligible profile, so a fallback
            # attempt to provider=agy always requires a non-empty prompt.
            "agy_fallback_requires_prompt": True,
            # _validate_agy_request() rejects request["model"] outright
            # (unsupported_provider_option), so an explicit model set for the
            # initial gemini attempt does not survive a fallback to agy.
            "explicit_model_survives_fallback": False,
        }

        payload = {
            **base,
            "ok": True,
            "failure_class": None,
            "failure_reason": None,
            "runtime_order": runtime_order,
            "profile_eligible": True,
            "provider_candidates": provider_candidates,
            "consumer_constraints": consumer_constraints,
        }
        return payload, 0

    # provider == "gemini"
    try:
        rgh = _load_run_gemini_headless_module()
    except Exception as exc:  # pylint: disable=broad-except
        payload = {
            **base,
            "ok": False,
            "failure_class": "internal_error",
            "failure_reason": f"Failed to load run_gemini_headless: {exc}",
        }
        return payload, 2

    chain, failure_payload, failure_exit = _resolve_gemini_chain_or_failure(
        rgh, role, config_path, base
    )
    if failure_payload is not None:
        return failure_payload, failure_exit

    payload = {
        **base,
        "ok": True,
        "failure_class": None,
        "failure_reason": None,
        "resolved_chain": chain,
        "actual_model": None,
        "resolver_source": "run_gemini_headless.resolve_model_chain",
    }
    return payload, 0


def build_model_policy_arg_parser() -> argparse.ArgumentParser:
    parser = _ModelPolicyArgumentParser(
        prog="build_request.py model-policy",
        description=(
            "Inspect the provider/role model policy that run_gemini_headless.py "
            "would resolve at runtime, without generating or writing a delegation "
            "request (read-only, no-side-effect)."
        ),
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=list(MODEL_POLICY_PROVIDERS),
        metavar="{gemini,agy,auto}",
        help="Provider to inspect the model policy for.",
    )
    parser.add_argument(
        "--role",
        default=None,
        metavar="ROLE_NAME",
        help=(
            "Role used for model-chain resolution (passed through to "
            "resolve_model_chain()). Optional; has no effect for provider=agy."
        ),
    )
    parser.add_argument(
        "--profile",
        dest="profile",
        default=None,
        metavar="TOOL_PROFILE",
        help=(
            "tool_profile to check provider=auto eligibility for "
            "(PROVIDER_AUTO_ELIGIBLE_PROFILES). Required when --provider auto."
        ),
    )
    return parser


def main_model_policy(argv: list[str]) -> int:
    parser = build_model_policy_arg_parser()
    try:
        args = parser.parse_args(argv)
    except _ModelPolicyArgumentError as exc:
        # Major 1 (Issue #1695 PR review): keep argparse usage errors (invalid
        # --provider, unknown option, etc) inside the delegation_model_policy/v1
        # JSON contract instead of falling back to argparse's own usage text +
        # bare exit code 2.
        payload = {
            "schema": MODEL_POLICY_SCHEMA,
            "provider": None,
            "role": None,
            "profile": None,
            "ok": False,
            "failure_class": "invalid_cli_arguments",
            "failure_reason": str(exc),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    payload, exit_code = build_model_policy(
        provider=args.provider,
        role=args.role,
        profile=args.profile,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        required=True,
        metavar="TOOL_PROFILE",
        help=f"tool_profile for the request. Valid values: {sorted(VALID_PROFILES)}",
    )
    parser.add_argument(
        "--objective",
        required=True,
        metavar="STR",
        help="Specific objective for the Gemini delegation.",
    )
    parser.add_argument(
        "--instruction",
        dest="instructions",
        action="append",
        default=None,
        metavar="STR",
        help=(
            "Instruction to add to the request. Repeat for multiple instructions. "
            "Defaults to profile-specific instructions when omitted."
        ),
    )
    parser.add_argument(
        "--context-file",
        dest="context_files",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "Path to a context file. Repeat for multiple files. "
            "Relative paths are resolved from cwd."
        ),
    )
    parser.add_argument(
        "--gh-pr",
        type=int,
        default=None,
        metavar="N",
        help="GitHub PR number to add as a gh_commands entry (pr view <N>).",
    )
    parser.add_argument(
        "--gh-issue",
        type=int,
        default=None,
        metavar="N",
        help="GitHub Issue number to add as a gh_commands entry (issue view <N>).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output path for the request JSON. Prints to stdout when omitted.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    if raw_argv and raw_argv[0] == "model-policy":
        return main_model_policy(raw_argv[1:])
    parser = build_arg_parser()
    args = parser.parse_args(raw_argv)
    return build_request(
        profile=args.profile,
        objective=args.objective,
        instructions=args.instructions,
        context_files=args.context_files,
        gh_pr=args.gh_pr,
        gh_issue=args.gh_issue,
        output=args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
