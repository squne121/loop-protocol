#!/usr/bin/env python3
"""AGY-driven, bounded, read-only `github_research` route (Issue #1920).

Orchestrates up to `limits.max_iterations` rounds of: prompt AGY (provider=agy,
tool_profile=github_research) for exactly one next `gh` command decision (or a
stop signal), validate that decision against
`run_agy_github_research_broker.validate_gh_argv()` *before* executing
anything, execute it via `run_agy_github_research_broker.execute_gh_command()`
(the only component holding `GH_TOKEN`), and feed the (redacted, bounded)
result back into the next round's prompt.

AGY's `github_research` tool_profile has an empty
`agy_permission_policy.PROFILE_ALLOWED_TOOLS` entry, so AGY has no native
tool-call surface under this profile at all -- every `gh` invocation is
chosen by AGY only as *text* in its response, which this module parses. This
keeps `GH_TOKEN` fully out of the AGY subprocess's environment.

Writes an `agy_github_research_evidence/v1` artifact
(`schemas/agy_github_research_evidence_v1.schema.json`) to
`.claude/artifacts/agent-provider-route/<run-id>/`. SKIP (exit 77) is
returned whenever a precondition (agy CLI, GH_TOKEN, read-only auth,
GH_HOST/GH_REPO) is unavailable or unverifiable -- SKIP is never PASS, and
Gemini / direct fallback / a single fixed-evidence injection are never
treated as a successful run of this route (Issue #1920 In Scope /
Out of Scope).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parent))
import run_agy_github_research_broker as broker  # noqa: E402

try:
    import agy_permission_policy as _agy_permission_policy  # noqa: E402
except ImportError:  # pragma: no cover - defensive; module is a sibling file
    _agy_permission_policy = None  # type: ignore[assignment]

SCHEMA_EVIDENCE = "agy_github_research_evidence/v1"
PROFILE = "github_research"
PROVIDER = "agy"

REPO_HOST = "github.com"
REPO_SLUG = "squne121/loop-protocol"

LIMITS: dict[str, Any] = {
    "max_iterations": 8,
    "command_timeout_seconds": 30,
    "total_route_timeout_seconds": 180,
    "stdout_bytes_per_command": 65536,
    "stderr_bytes_per_command": 16384,
    "aggregate_retained_bytes": 262144,
    "max_records_per_command": 100,
    "pagination": False,
}

_ARTIFACT_ROOT = Path(".claude/artifacts/agent-provider-route")

_NEXT_COMMAND_RE = re.compile(r"NEXT_COMMAND:\s*(\{.*\})", re.DOTALL)
_STOP_RE = re.compile(r"\bSTOP\b", re.IGNORECASE)

# Static, deterministic negative probes exercised unconditionally (Issue
# #1920 AC5 close_requirements: at least one pre-execution deny per class).
_NEGATIVE_PROBES: tuple[tuple[str, list[str]], ...] = (
    ("mutation", ["issue", "close", "1"]),
    ("cross_repository", ["issue", "view", "1", "--repo", "other-owner/other-repo"]),
    ("alternate_host", ["issue", "view", "1", "--hostname", "example.com"]),
    ("compound_shell", ["issue", "view", "1;", "rm", "-rf", "/"]),
    ("credential_display", ["auth", "status"]),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest_file(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _digest_binding() -> dict[str, Any]:
    """Reference safety evidence by digest rather than copying #1979/PR#1994 artifacts."""
    agy_bin = shutil.which(os.environ.get("AGY_BIN") or "agy")
    agy_binary_digest = _digest_file(Path(agy_bin)) if agy_bin else None
    agy_version = _probe_agy_version(agy_bin) if agy_bin else None

    scripts_dir = Path(__file__).parent
    permission_policy_digest = _digest_file(scripts_dir / "agy_permission_policy.py")
    hook_executable_digest = _digest_file(scripts_dir / "agy_permission_enforcement_hook.py")
    isolated_settings_digest = None
    if _agy_permission_policy is not None:
        try:
            fixture = json.dumps(
                _agy_permission_policy.build_workspace_permission_policy(PROFILE),
                sort_keys=True,
            )
            isolated_settings_digest = "sha256:" + hashlib.sha256(fixture.encode("utf-8")).hexdigest()
        except Exception:  # noqa: BLE001 - best-effort binding, never fatal
            isolated_settings_digest = None

    return {
        "agy_binary_digest": agy_binary_digest,
        "agy_version": agy_version,
        "permission_policy_digest": permission_policy_digest,
        "hook_executable_digest": hook_executable_digest,
        "isolated_settings_digest": isolated_settings_digest,
        # References the AGY 1.1.10 ephemeralMessage-based live E2E schema
        # established by PR #1994 (#1979); this route's own evidence schema
        # is agy_github_research_evidence/v1, distinct from that one.
        "pr_1994_schema_version": "agy_permission_boundary_e2e/v1",
    }


def _probe_agy_version(agy_bin: str) -> str | None:
    try:
        completed = subprocess.run(
            [agy_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    combined = f"{completed.stdout}\n{completed.stderr}".strip()
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?", combined)
    return match.group(0) if match else None


def _run_negative_probes() -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for probe_class, argv in _NEGATIVE_PROBES:
        result = broker.validate_gh_argv(argv)
        probes.append(
            {
                "probe_class": probe_class,
                "denied_pre_execution": not result.allowed,
                "reason": result.reason,
            }
        )
    return probes


def _preflight(*, gh_token_env: str) -> tuple[bool, str | None, str | None]:
    """Return (ok, skip_reason, gh_token).

    Fail-closed: SKIP unless the `agy` CLI is resolvable, `GH_TOKEN` is
    explicitly present in the runtime environment, and GH_HOST/GH_REPO are
    the fixed repository-bound values this route always uses.
    """
    agy_bin = shutil.which(os.environ.get("AGY_BIN") or "agy")
    if not agy_bin:
        return False, "agy_cli_unavailable", None

    gh_token = os.environ.get(gh_token_env)
    if not gh_token:
        return False, "gh_token_unavailable", None

    host = os.environ.get("GH_HOST", REPO_HOST)
    repo = os.environ.get("GH_REPO", REPO_SLUG)
    if host != REPO_HOST or repo != REPO_SLUG:
        return False, "gh_host_repo_binding_mismatch", None

    # Read-only auth reachability check: a single allowlisted, harmless
    # broker-executed command (does not count against max_iterations).
    try:
        probe = broker.execute_gh_command(["repo", "view"], gh_token=gh_token, timeout_seconds=15)
    except broker.BrokerDenied:
        return False, "gh_readonly_auth_unverifiable", None
    if probe.get("exit_code") != 0:
        return False, "gh_readonly_auth_unverifiable", None

    return True, None, gh_token


def _isolated_agy_env(agy_bin: str) -> tuple[dict[str, str], list[str], Any]:
    """Materialize the isolated, hook-wired AGY workspace for `github_research`.

    Returns (env, command_prefix, workspace) where `workspace` is the
    `agy_permission_policy` workspace handle (the caller is responsible for
    `shutil.rmtree(workspace.workspace_dir, ignore_errors=True)`), and
    `command_prefix` is an optional bwrap prefix that must precede the real
    agy argv (mirrors `run_gemini_headless._run_agy()`'s own usage of
    `workspace.agy_oauth_token_bwrap_prefix`). Falls back to a minimal
    allowlisted env with no prefix if `agy_permission_policy` is unavailable
    (defensive only; the module is a sibling file and is expected to always
    import).
    """
    if _agy_permission_policy is not None and PROFILE in _agy_permission_policy.ALLOWED_PROFILES:
        workspace = _agy_permission_policy.materialize_isolated_agy_workspace(PROFILE)
        env = dict(workspace.env)
        if os.environ.get("AGY_BIN"):
            env["AGY_BIN"] = os.environ["AGY_BIN"]
        prefix = list(getattr(workspace, "agy_oauth_token_bwrap_prefix", None) or [])
        return env, prefix, workspace
    allowlist = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
    env = {key: os.environ[key] for key in allowlist if key in os.environ}
    return env, [], None


def _build_turn_prompt(*, objective: str, transcript: list[dict[str, Any]], iterations_left: int) -> str:
    lines = [
        "You are performing bounded, read-only GitHub research via a broker that",
        f"executes at most one `gh` command per turn against {REPO_HOST}/{REPO_SLUG}.",
        "You have no direct tool access; you may only respond with plain text.",
        "",
        f"Objective: {objective}",
        "",
        "Allowed gh command families: issue view/list, pr view/list/diff/checks,",
        "repo view, search issues/prs/repos, release view/list, api <GET endpoint>.",
        "Mutation, gh auth/alias/extension, gh api graphql, and non-GET gh api are",
        "never permitted and will be denied before execution.",
        "",
        f"Turns remaining: {iterations_left}.",
        "",
        "To request the next command, respond with exactly one line of the form:",
        'NEXT_COMMAND: {"argv": ["issue", "view", "1920"]}',
        "When you have enough evidence to answer the objective, respond with a line",
        "containing only the word STOP followed by your final summary.",
        "",
        "Evidence so far:",
    ]
    if not transcript:
        lines.append("(none yet)")
    for entry in transcript:
        lines.append(f"--- turn {entry['index']} argv={entry['argv']} decision={entry['decision']} ---")
        if entry.get("output_sample") is not None:
            lines.append(entry["output_sample"])
    return "\n".join(lines)


def _parse_agy_turn(response_text: str) -> tuple[str, list[str] | None, str | None]:
    """Return (action, argv_or_none, raw_summary_or_none). action in {next_command, stop, unparseable}."""
    if _STOP_RE.search(response_text):
        return "stop", None, response_text.strip()
    match = _NEXT_COMMAND_RE.search(response_text)
    if not match:
        return "unparseable", None, response_text.strip()
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return "unparseable", None, response_text.strip()
    argv = payload.get("argv") if isinstance(payload, dict) else None
    if not isinstance(argv, list) or not all(isinstance(token, str) for token in argv):
        return "unparseable", None, response_text.strip()
    return "next_command", argv, None


def _run_agy_turn(*, prompt: str, timeout_seconds: int) -> tuple[str, str]:
    """Run one `agy -p <prompt>` turn in the isolated github_research workspace.

    Returns (response_text, failure_class). failure_class is "" on success.
    """
    agy_bin = str(os.environ.get("AGY_BIN") or "agy")
    resolved_bin = shutil.which(agy_bin) or agy_bin
    env, prefix, workspace = _isolated_agy_env(resolved_bin)
    command = [*prefix, resolved_bin, "-p", prompt]
    cwd = str(workspace.workspace_dir) if workspace is not None else None
    try:
        completed = subprocess.run(
            command,
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", "agy_timeout"
    except FileNotFoundError:
        return "", "agy_not_found"
    finally:
        if workspace is not None:
            shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
    if completed.returncode != 0 and not (completed.stdout or "").strip():
        return "", "agy_exit_nonzero"
    return (completed.stdout or "").strip(), ""


def run_github_research_route(
    request: Mapping[str, Any],
    *,
    request_warnings: list[str] | None = None,
    gh_token_env: str = "GH_TOKEN",
) -> dict[str, Any]:
    """Entry point called from `run_gemini_headless.py`'s provider=='agy' dispatch.

    Returns a `delegation_result/v1`-shaped dict; never raises for expected
    failure/SKIP conditions.
    """
    warnings = list(request_warnings or [])
    run_id = f"agy-github-research-{uuid.uuid4().hex[:12]}"
    started_at = time.monotonic()

    negative_probes = _run_negative_probes()
    ok_preflight, skip_reason, gh_token = _preflight(gh_token_env=gh_token_env)

    base_result: dict[str, Any] = {
        "schema": "delegation_result/v1",
        "provider": PROVIDER,
        "safety_mode": "degraded_wrapper_only",
        "tool_profile": PROFILE,
        "requested_model": None,
        "actual_model": "agy-default",
        "model_chain": [],
        "model_downgrades": [],
        "raw_command": ["agy", "-p", "<github_research prompt>"],
        "response_text": None,
        "stats": None,
        "warnings": warnings,
        "parent_run_id": request.get("parent_run_id"),
        "subtask_id": request.get("subtask_id"),
        "attempt_id": request.get("attempt_id"),
        "gemini_invocation_count": 0,
    }

    if not ok_preflight:
        evidence = _build_evidence(
            run_id=run_id,
            status="skip",
            skip_reason=skip_reason,
            iterations=[],
            negative_probes=negative_probes,
            positive_run={
                "observed": False,
                "exit_code": None,
                "iteration_count": 0,
                "adaptive_next_command_observed": False,
            },
            agy_observed_version=_probe_agy_version(shutil.which("agy") or "") if shutil.which("agy") else None,
        )
        artifact_path = _write_evidence(run_id, evidence)
        base_result.update(
            {
                "ok": False,
                "exit_code": 77,
                "result_surface": {
                    "mode": "artifact-first",
                    "summary": f"SKIP: {skip_reason}",
                    "primary_artifact_type": "agy_github_research_evidence_v1",
                    "primary_artifact": str(artifact_path),
                    "next_action": "Provide agy CLI + repository-bound read-only GH_TOKEN, then rerun.",
                },
                "stderr": f"github_research_skip: {skip_reason}",
                "failure_reason": f"github_research_skip: {skip_reason}",
                "failure_class": "github_research_skip",
            }
        )
        return base_result

    objective = str(request.get("prompt") or "").strip()
    transcript: list[dict[str, Any]] = []
    iterations: list[dict[str, Any]] = []
    adaptive_observed = False
    executed_count = 0
    final_summary: str | None = None
    route_failure_class: str | None = None

    for index in range(LIMITS["max_iterations"]):
        if time.monotonic() - started_at > LIMITS["total_route_timeout_seconds"]:
            route_failure_class = "github_research_route_timeout"
            break
        iterations_left = LIMITS["max_iterations"] - index
        prompt = _build_turn_prompt(objective=objective, transcript=transcript, iterations_left=iterations_left)
        response_text, agy_failure = _run_agy_turn(
            prompt=prompt, timeout_seconds=LIMITS["command_timeout_seconds"] * 2
        )
        if agy_failure:
            route_failure_class = agy_failure
            break

        action, argv, summary = _parse_agy_turn(response_text)
        if action == "stop":
            final_summary = summary
            break
        if action == "unparseable":
            warnings.append(f"github_research: unparseable agy turn {index}")
            iterations.append(
                {
                    "index": index,
                    "command_requested": {"argv": []},
                    "decision": "deny",
                    "reason": "unparseable_turn_response",
                    "exit_code": None,
                    "redacted_output_digest": None,
                    "truncated": False,
                    "duration_ms": None,
                }
            )
            continue

        assert argv is not None
        validation = broker.validate_gh_argv(argv)
        if not validation.allowed:
            iterations.append(
                {
                    "index": index,
                    "command_requested": {"argv": argv},
                    "decision": "deny",
                    "reason": validation.reason,
                    "exit_code": None,
                    "redacted_output_digest": None,
                    "truncated": False,
                    "duration_ms": None,
                }
            )
            transcript.append(
                {
                    "index": index,
                    "argv": argv,
                    "decision": "deny",
                    "output_sample": f"DENIED: {validation.reason}",
                }
            )
            continue

        if index > 0:
            adaptive_observed = True
        try:
            command_result = broker.execute_gh_command(
                argv,
                gh_token=gh_token,  # type: ignore[arg-type]
                timeout_seconds=LIMITS["command_timeout_seconds"],
                stdout_cap_bytes=LIMITS["stdout_bytes_per_command"],
                stderr_cap_bytes=LIMITS["stderr_bytes_per_command"],
            )
        except broker.BrokerDenied as exc:
            iterations.append(
                {
                    "index": index,
                    "command_requested": {"argv": argv},
                    "decision": "deny",
                    "reason": str(exc),
                    "exit_code": None,
                    "redacted_output_digest": None,
                    "truncated": False,
                    "duration_ms": None,
                }
            )
            continue

        executed_count += 1
        iterations.append(
            {
                "index": index,
                "command_requested": {"argv": argv},
                "decision": "allow",
                "reason": "allowed_subcommand",
                "exit_code": command_result["exit_code"],
                "redacted_output_digest": command_result["redacted_output_digest"],
                "truncated": command_result["truncated"],
                "duration_ms": command_result["duration_ms"],
            }
        )
        sample = command_result["redacted_stdout_sample"] or command_result["redacted_stderr_sample"]
        transcript.append(
            {
                "index": index,
                "argv": argv,
                "decision": "allow",
                "output_sample": sample[:2000],
            }
        )

    positive_run_observed = executed_count >= 1 and route_failure_class is None
    positive_run = {
        "observed": positive_run_observed,
        "exit_code": 0 if positive_run_observed else None,
        "iteration_count": executed_count,
        "adaptive_next_command_observed": adaptive_observed,
    }

    # Issue #1920 close_requirements: iteration>=2 with an adaptive next
    # command is required to *close* the Issue, but a single-iteration
    # genuine (non-SKIP, non-fallback) run is still a valid PASS result for
    # this individual invocation.
    status = "fail" if route_failure_class else "pass"

    evidence = _build_evidence(
        run_id=run_id,
        status=status,
        skip_reason=None,
        iterations=iterations,
        negative_probes=negative_probes,
        positive_run=positive_run,
        agy_observed_version=_probe_agy_version(shutil.which("agy") or "") if shutil.which("agy") else None,
    )
    artifact_path = _write_evidence(run_id, evidence)

    ok = status == "pass"
    base_result.update(
        {
            "ok": ok,
            "exit_code": 0 if ok else 1,
            "response_text": final_summary,
            "result_surface": {
                "mode": "artifact-first",
                "summary": final_summary or f"github_research route completed with status={status}",
                "primary_artifact_type": "agy_github_research_evidence_v1",
                "primary_artifact": str(artifact_path),
                "next_action": "Inspect the evidence artifact for per-iteration allow/deny detail.",
            },
            "stderr": None if ok else (route_failure_class or "github_research_incomplete"),
            "failure_reason": None if ok else (route_failure_class or "github_research_incomplete"),
            "failure_class": None if ok else (route_failure_class or "github_research_incomplete"),
            "warnings": warnings,
        }
    )
    return base_result


def _build_evidence(
    *,
    run_id: str,
    status: str,
    skip_reason: str | None,
    iterations: list[dict[str, Any]],
    negative_probes: list[dict[str, Any]],
    positive_run: dict[str, Any],
    agy_observed_version: str | None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_EVIDENCE,
        "run_id": run_id,
        "generated_at": _now_iso(),
        "status": status,
        "skip_reason": skip_reason,
        "provider": {"requested": PROVIDER, "observed": agy_observed_version or "unknown"},
        "profile": PROFILE,
        "repository_binding": {"host": REPO_HOST, "repo": REPO_SLUG},
        "limits": LIMITS,
        "iterations": iterations,
        "close_evidence": {"positive_run": positive_run, "negative_probes": negative_probes},
        "digest_binding": _digest_binding(),
        "gemini_invocation_count": 0,
    }


def _write_evidence(run_id: str, evidence: Mapping[str, Any]) -> Path:
    run_dir = _ARTIFACT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = run_dir / "agy_github_research_evidence.json"
    artifact_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return artifact_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, help="Research objective for AGY.")
    parser.add_argument("--gh-token-env", default="GH_TOKEN")
    args = parser.parse_args(argv)

    request = {
        "schema": "delegation_request_v1",
        "provider": PROVIDER,
        "tool_profile": PROFILE,
        "prompt": args.prompt,
    }
    result = run_github_research_route(request, gh_token_env=args.gh_token_env)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    if result["exit_code"] == 77:
        return 77
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
