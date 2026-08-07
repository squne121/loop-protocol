#!/usr/bin/env python3
"""run_agent_provider_route_smoke.py — live route smoke producer for the
``agent_provider_route_smoke/v1`` envelope (Issue #1886).

For each required ``(runtime, agent, profile)`` route (Claude Code / Codex
CLI x codebase-investigator / web-researcher), this script:

1. Spawns the target custom agent via the existing, independently-tested
   worktree runtime smoke harness (``run_worktree_agent_runtime_smoke.py``),
   with a ``gemini`` PATH sentinel stub prepended (Issue #1886 AC8: any
   invocation of a binary literally named ``gemini`` is recorded as a real
   failure, never silently tolerated) so any Gemini CLI invocation is
   observed rather than assumed absent.
2. Reads the underlying harness's native spawn-session evidence
   (``parent_session_id`` / ``child_session_id`` / ``native_spawn_event_
   observed``, added by this same Issue to ``run_worktree_agent_runtime_
   smoke.py``) instead of re-deriving it.
3. Packages an ``agent_provider_route_smoke/v1`` artifact per route under
   ``.claude/artifacts/agent-provider-route/<run-id>/``.

This script never fabricates a PASS: an AGY route failure, a Gemini sentinel
hit, or an unobserved native spawn event is recorded as ``status: fail`` with
an explicit ``failure_class`` -- it is never silently retried or downgraded
to SKIP/PASS. Only a genuine infrastructure-classified transient failure
(Issue #1997 class) is eligible for the bounded single retry recorded under
``retry`` (never a silent re-run).

Exit codes:
  0  all requested routes produced a well-formed artifact (regardless of
     each route's own pass/fail/skip status -- this script's own exit code
     reflects "did the producer itself run cleanly", not aggregate route
     pass/fail; use validate_agent_provider_route_smoke.py for aggregate
     pass/fail verdicts)
  1  producer-internal error (e.g. unknown --route, artifact directory not
     writable)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_SMOKE_SCRIPT = Path(__file__).resolve().parent / "run_worktree_agent_runtime_smoke.py"
BUILD_REQUEST_SCRIPT = (
    ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py"
)
RUN_GEMINI_HEADLESS_SCRIPT = (
    ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py"
)
GITHUB_RESEARCH_EVIDENCE_SCHEMA = "agy_github_research_evidence/v1"

SCHEMA = "agent_provider_route_smoke/v1"

REQUIRED_ROUTES: list[dict[str, str]] = [
    {"runtime": "claude_code", "agent": "codebase-investigator", "profile": "local_asset_research"},
    {"runtime": "claude_code", "agent": "codebase-investigator", "profile": "github_research"},
    {"runtime": "claude_code", "agent": "web-researcher", "profile": "grounded_research"},
    {"runtime": "codex_cli", "agent": "codebase-investigator", "profile": "local_asset_research"},
    {"runtime": "codex_cli", "agent": "codebase-investigator", "profile": "github_research"},
    {"runtime": "codex_cli", "agent": "web-researcher", "profile": "grounded_research"},
]

_RUNTIME_TO_HARNESS = {"claude_code": "claude", "codex_cli": "codex"}

_GEMINI_SENTINEL_TEMPLATE = """#!/usr/bin/env python3
import json
import os
import sys
import time

marker = os.environ.get("AGENT_PROVIDER_ROUTE_SMOKE_GEMINI_SENTINEL_MARKER")
if marker:
    record = {{"argv": sys.argv[1:], "timestamp": time.time()}}
    with open(marker, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\\n")
sys.stderr.write(
    "gemini CLI is disabled_by_operator in this repository "
    "(agent_provider_route_smoke sentinel stub; Issue #1886).\\n"
)
sys.exit(127)
"""


def _route_key(route: dict[str, str]) -> str:
    return f"{route['runtime']}:{route['agent']}:{route['profile']}"


def _find_route(runtime: str, agent: str, profile: str) -> dict[str, str] | None:
    for route in REQUIRED_ROUTES:
        if route["runtime"] == runtime and route["agent"] == agent and route["profile"] == profile:
            return route
    return None


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head_sha(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError:
        return None
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return None
    return sha


_RUNTIME_VERSION_UNAVAILABLE_SENTINEL = "unavailable (binary not found or --version produced no output)"


def _runtime_version(bin_name: str) -> str:
    """Best-effort ``<bin_name> --version`` detection.

    The schema (``agent_provider_route_smoke_v1.schema.json``) requires
    ``subject.runtime_version`` to be a non-empty string in every artifact,
    including ``--dry-run`` ones (Issue #1886 fix_delta iteration 3): CI
    runners do not have ``claude`` / ``codex`` on ``PATH`` the way a local
    interactive dev environment does, so a bare ``None`` here previously
    failed schema validation in CI while passing locally. Rather than
    fabricating a fake version string, an absent/unusable binary is recorded
    with an explicit, documented sentinel string -- never ``None`` -- so the
    artifact stays schema-valid while still being honest that no real
    version could be observed.
    """
    try:
        result = subprocess.run(
            [bin_name, "--version"], capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError:
        return _RUNTIME_VERSION_UNAVAILABLE_SENTINEL
    text = (result.stdout or result.stderr).strip()
    first_line = text.splitlines()[0].strip() if text else ""
    return first_line if first_line else _RUNTIME_VERSION_UNAVAILABLE_SENTINEL


def _agent_definition_path(agent: str, runtime: str, repo_root: Path) -> Path:
    if runtime == "claude_code":
        return repo_root / ".claude" / "agents" / f"{agent}.md"
    return repo_root / ".codex" / "agents" / f"{agent}.toml"


def compute_subject(route: dict[str, str], repo_root: Path) -> dict:
    runtime = route["runtime"]
    agent = route["agent"]
    bin_name = "claude" if runtime == "claude_code" else "codex"
    agent_path = _agent_definition_path(agent, runtime, repo_root)
    agent_sha = _sha256_file(agent_path)
    head_sha = _git_head_sha(repo_root)
    return {
        "runtime": runtime,
        "agent_name": agent,
        "head_sha": head_sha,
        "runtime_version": _runtime_version(bin_name),
        "agent_definition_sha256": agent_sha,
        # Deliberately the same digest as agent_definition_sha256 for now:
        # this repository does not yet materialize a separate "effective
        # runtime config" artifact (merged frontmatter + permission profile)
        # distinct from the raw agent definition file. Documented as a
        # known simplification rather than fabricating a second digest.
        "effective_runtime_config_sha256": agent_sha,
    }


def build_route_prompt(route: dict[str, str], evidence_dir: Path) -> str:
    agent = route["agent"]
    profile = route["profile"]
    request_path = evidence_dir / "delegation_request.json"
    result_path = evidence_dir / "delegation_result.json"
    route_evidence_hint = ""
    if profile == "github_research":
        route_evidence_path = evidence_dir / "route_evidence.json"
        route_evidence_hint = (
            f" After the call returns, copy the exact `agy_github_research_evidence/v1` "
            f"artifact JSON you actually received (byte-for-byte, unmodified) to "
            f"`{route_evidence_path}`."
        )
    return (
        f"AGENT_PROVIDER_ROUTE_SMOKE probe (Issue #1886). Use the {agent} custom agent "
        f"(profile: {profile}) for the following bounded task, and report exactly which "
        f"provider you dispatched to.\n\n"
        f"Task: build a delegation_request_v1 via "
        f"`uv run python3 {BUILD_REQUEST_SCRIPT} --provider agy --profile {profile} "
        f"--objective \"agent_provider_route_smoke probe\" --prompt \"Reply with the single "
        f"word OK and do nothing else.\" --output {request_path}` "
        f"and then run it via `uv run python3 {RUN_GEMINI_HEADLESS_SCRIPT} "
        f"--request-file {request_path} --output-file {result_path}`. "
        f"Do not invoke a binary literally named `gemini`. Do not use WebSearch or WebFetch "
        f"as a fallback. If the AGY route fails, report the failure_class and stop -- do not "
        f"retry with a different provider.{route_evidence_hint}\n\n"
        f"After the delegated call returns (success or failure), reply with exactly one line: "
        f"ROUTE_SMOKE_DONE."
    )


def _write_gemini_sentinel(bin_dir: Path, marker_path: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gemini"
    stub.write_text(_GEMINI_SENTINEL_TEMPLATE, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _count_gemini_sentinel_hits(marker_path: Path) -> int:
    if not marker_path.is_file():
        return 0
    return sum(1 for line in marker_path.read_text(encoding="utf-8").splitlines() if line.strip())


def _count_direct_fallback_hits(harness_summary: dict) -> int:
    """Issue #1886 P0-1 fix_delta (PR #2005 adversarial review): the prior
    implementation always returned 0, undocumented as anything but a
    permanent placeholder. ``run_worktree_agent_runtime_smoke.py`` now
    classifies native WebSearch/WebFetch (Claude) and direct-web-shaped
    (Codex) tool events into ``direct_web_tool_event_count`` in the same
    ``summary.md`` this producer already parses generically -- read that
    real, observed count here instead of a hard-coded 0. Absent the key
    entirely (an older harness revision), this fails closed to 0 only
    because the harness cannot have classified anything it doesn't emit at
    all -- never because a real event was seen and discarded."""
    value = harness_summary.get("direct_web_tool_event_count")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return 0


_HARNESS_SUMMARY_LINE_RE = re.compile(r"^- ([a-zA-Z0-9_]+): (.*)$")


def _parse_harness_summary_md(text: str) -> dict:
    """Parse ``run_worktree_agent_runtime_smoke.py``'s ``summary.md``
    ``- key: value`` lines back into a dict. Values are the raw
    ``str(python_value)`` the harness wrote (Python truthy/None literal
    strings for booleans/None), never re-serialized JSON -- this parser only
    needs the small set of string-valued fields this producer consumes
    (``parent_session_id`` / ``child_session_id`` /
    ``native_spawn_event_observed`` / ``direct_web_tool_event_count``)."""
    parsed: dict = {}
    for line in text.splitlines():
        match = _HARNESS_SUMMARY_LINE_RE.match(line)
        if not match:
            continue
        key, raw_value = match.group(1), match.group(2)
        if raw_value == "None":
            parsed[key] = None
        elif raw_value == "True":
            parsed[key] = True
        elif raw_value == "False":
            parsed[key] = False
        else:
            parsed[key] = raw_value
    return parsed


def _read_json_file(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _validate_delegation_request_evidence(evidence_dir: Path, route: dict[str, str]) -> str:
    """Issue #1886 P0-1 fix_delta: read the ACTUAL delegation_request_v1 the
    agent built (via build_request.py --output) instead of stamping
    ``"pass"`` on harness exit 0 alone. Returns a value valid against the
    ``request.validation`` schema enum (``pass`` / ``fail`` / ``not_run``)."""
    payload = _read_json_file(evidence_dir / "delegation_request.json")
    if payload is None:
        return "not_run"
    if not isinstance(payload, dict):
        return "fail"
    if payload.get("provider") != "agy":
        return "fail"
    if payload.get("profile") != route["profile"]:
        return "fail"
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return "fail"
    if payload.get("model"):
        return "fail"
    return "pass"


def _observed_provider_attempts(raw_attempts: object) -> list[str]:
    if not isinstance(raw_attempts, list):
        return []
    names: list[str] = []
    for entry in raw_attempts:
        if isinstance(entry, str) and entry:
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("provider"), str) and entry["provider"]:
            names.append(entry["provider"])
    return names


def _validate_delegation_result_evidence(evidence_dir: Path) -> tuple[str | None, list[str], bool]:
    """Issue #1886 P0-1 fix_delta: read the ACTUAL run_gemini_headless.py
    wrapper result (via --output-file) instead of stamping
    ``selected_provider: "agy"`` / ``provider_attempts: ["agy"]`` on harness
    exit 0 alone. Returns ``(selected_provider, provider_attempts,
    wrapper_ok)``; all fail closed to ``None`` / ``[]`` / ``False`` when the
    result file is missing or malformed -- never guessed."""
    payload = _read_json_file(evidence_dir / "delegation_result.json")
    if not isinstance(payload, dict):
        return None, [], False
    selected_provider = payload.get("selected_provider")
    if not isinstance(selected_provider, str) or not selected_provider:
        selected_provider = None
    provider_attempts = _observed_provider_attempts(payload.get("provider_attempts"))
    wrapper_ok = payload.get("ok") is True
    return selected_provider, provider_attempts, wrapper_ok


def _validate_github_research_route_evidence(evidence_dir: Path) -> str | None:
    """Issue #1886 P0-1 fix_delta: read the ACTUAL agy_github_research_evidence/v1
    artifact the agent copied out (never a hand-typed schema string), and
    return the real sha256 digest of that file iff it is well-formed and
    carries the expected ``schema`` field -- ``None`` (never a guessed
    digest) on any missing/malformed/wrong-schema evidence."""
    path = evidence_dir / "route_evidence.json"
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != GITHUB_RESEARCH_EVIDENCE_SCHEMA:
        return None
    return _sha256_file(path)


# Issue #1886 P0-4 fix_delta (PR #2005 adversarial review): a bounded,
# explicitly-classified single retry for genuine infrastructure-timing
# races (the #1997 class), never a silent re-run of a validation failure,
# provider mismatch, identity mismatch, or fallback detection. Codex CLI's
# child-spawn evidence is recovered by bounded polling of an on-disk
# rollout log (see extract_codex_child_session_id in
# run_worktree_agent_runtime_smoke.py) -- exactly the kind of disk-timing
# race a single bounded retry is meant to absorb. Claude's spawn evidence
# instead comes from the already-captured in-memory stdout stream, so a
# miss there is never classified as transient (retrying it would silently
# paper over a genuine identity/spawn failure).
def _is_transient_infrastructure_candidate(route: dict[str, str], failure_class: str | None) -> bool:
    return route["runtime"] == "codex_cli" and failure_class == "spawn_not_observed"


def _run_route_once(
    route: dict[str, str],
    *,
    run_id: str,
    repo_root: Path,
    worktree: Path,
    output_root: Path,
    timeout_seconds: int,
) -> dict:
    """A single, non-retried attempt at a live route. See ``run_route_live``
    for the bounded-retry wrapper around this."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subject = compute_subject(route, repo_root)
    harness_runtime = _RUNTIME_TO_HARNESS[route["runtime"]]

    artifact: dict = {
        "schema": SCHEMA,
        "run_id": run_id,
        "generated_at": generated_at,
        "subject": subject,
        "spawn": {
            "parent_session_id": "",
            "child_session_id": "",
            "native_spawn_event_observed": False,
        },
        "request": {
            "profile": route["profile"],
            "expected_provider": "agy",
            "validation": "not_run",
        },
        "provider_observation": {
            "selected_provider": None,
            "provider_attempts": [],
            "gemini_invocation_count": 0,
            "direct_fallback_invocation_count": 0,
        },
        "route_evidence": {"schema": None, "sha256": None},
        "status": "fail",
        "failure_class": "other",
    }

    route_key_slug = _route_key(route).replace(":", "-")
    evidence_dir = output_root / f"{route_key_slug}-{run_id}-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="agent-provider-route-smoke-") as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        prompt_file = tmp_dir / "prompt.txt"
        prompt_file.write_text(build_route_prompt(route, evidence_dir), encoding="utf-8")
        sentinel_bin_dir = tmp_dir / "sentinel-bin"
        marker_path = tmp_dir / "gemini-sentinel-hits.jsonl"
        _write_gemini_sentinel(sentinel_bin_dir, marker_path)
        harness_output_dir = output_root / f"{route_key_slug}-{run_id}"
        # run_worktree_agent_runtime_smoke.py's write_evidence() creates this
        # directory itself with exist_ok=False (its own hygiene invariant) --
        # this producer must not pre-create it.

        child_env = dict(os.environ)
        child_env["PATH"] = f"{sentinel_bin_dir}:{child_env.get('PATH', '')}"
        child_env["AGENT_PROVIDER_ROUTE_SMOKE_GEMINI_SENTINEL_MARKER"] = str(marker_path)

        argv = [
            sys.executable,
            str(RUNTIME_SMOKE_SCRIPT),
            "--runtime", harness_runtime,
            "--mode", "structured",
            "--worktree", str(worktree),
            "--prompt-file", str(prompt_file),
            "--output-dir", str(harness_output_dir),
            "--timeout-seconds", str(timeout_seconds),
            "--agent-type", route["agent"],
            "--expect-marker", "ROUTE_SMOKE_DONE",
        ]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout_seconds + 30,
                env=child_env, check=False,
            )
        except subprocess.TimeoutExpired:
            artifact["failure_class"] = "timeout"
            artifact["status"] = "fail"
            return artifact
        except OSError as exc:
            artifact["failure_class"] = "other"
            artifact["status"] = "fail"
            artifact["_producer_error"] = str(exc)
            return artifact

        harness_exit = result.returncode
        # run_worktree_agent_runtime_smoke.py deliberately persists only
        # ``summary.md`` (Issue #1921 P1 evidence-hygiene fix-delta; its own
        # tests assert an exact one-file allowlist per output directory), so
        # this producer parses the ``- key: value`` lines back into a dict
        # rather than adding a second machine-readable file to that
        # directory (which would violate the existing allowlist contract).
        summary_path = harness_output_dir / "summary.md"
        harness_summary: dict = {}
        if summary_path.is_file():
            harness_summary = _parse_harness_summary_md(summary_path.read_text(encoding="utf-8"))

        gemini_hits = _count_gemini_sentinel_hits(marker_path)
        fallback_hits = _count_direct_fallback_hits(harness_summary)

        artifact["spawn"]["parent_session_id"] = harness_summary.get("parent_session_id") or ""
        artifact["spawn"]["child_session_id"] = harness_summary.get("child_session_id") or ""
        artifact["spawn"]["native_spawn_event_observed"] = bool(
            harness_summary.get("native_spawn_event_observed")
        )
        artifact["provider_observation"]["gemini_invocation_count"] = gemini_hits
        artifact["provider_observation"]["direct_fallback_invocation_count"] = fallback_hits

        request_validation = _validate_delegation_request_evidence(evidence_dir, route)
        selected_provider, provider_attempts, wrapper_ok = _validate_delegation_result_evidence(
            evidence_dir
        )
        artifact["request"]["validation"] = request_validation
        artifact["provider_observation"]["selected_provider"] = selected_provider
        artifact["provider_observation"]["provider_attempts"] = provider_attempts

        route_evidence_sha256 = None
        if route["profile"] == "github_research":
            artifact["route_evidence"]["schema"] = GITHUB_RESEARCH_EVIDENCE_SCHEMA
            route_evidence_sha256 = _validate_github_research_route_evidence(evidence_dir)
            artifact["route_evidence"]["sha256"] = route_evidence_sha256

        if gemini_hits > 0:
            artifact["status"] = "fail"
            artifact["failure_class"] = "gemini_invoked"
        elif fallback_hits > 0:
            artifact["status"] = "fail"
            artifact["failure_class"] = "direct_fallback_invoked"
        elif harness_exit == 77:
            artifact["status"] = "skip"
            artifact["failure_class"] = "agy_unavailable"
        elif harness_exit != 0:
            artifact["status"] = "fail"
            artifact["failure_class"] = "validation_failed"
        elif not artifact["spawn"]["native_spawn_event_observed"]:
            artifact["status"] = "fail"
            artifact["failure_class"] = "spawn_not_observed"
        elif request_validation != "pass":
            artifact["status"] = "fail"
            artifact["failure_class"] = "validation_failed"
        elif selected_provider != "agy":
            artifact["status"] = "fail"
            artifact["failure_class"] = "provider_mismatch"
        elif route["profile"] == "github_research" and route_evidence_sha256 is None:
            artifact["status"] = "fail"
            artifact["failure_class"] = "route_evidence_schema_mismatch"
        elif not wrapper_ok:
            artifact["status"] = "fail"
            artifact["failure_class"] = "validation_failed"
        else:
            artifact["status"] = "pass"
            artifact["failure_class"] = None

    return artifact


def run_route_live(
    route: dict[str, str],
    *,
    repo_root: Path,
    worktree: Path,
    output_root: Path,
    timeout_seconds: int,
) -> dict:
    initial = _run_route_once(
        route,
        run_id=str(uuid.uuid4()),
        repo_root=repo_root,
        worktree=worktree,
        output_root=output_root,
        timeout_seconds=timeout_seconds,
    )
    if initial["status"] != "fail" or not _is_transient_infrastructure_candidate(
        route, initial["failure_class"]
    ):
        return initial

    # Issue #1886 P0-4: bounded single retry, ledgered (never a silent
    # re-run). The FIRST artifact's own run_id/generated_at/etc are
    # discarded in favor of the retry's own fresh artifact -- only the
    # retry ledger below (initial_result/initial_failure_class/retry_count/
    # retry_result/final_route_verdict) survives from the first attempt.
    retry_artifact = _run_route_once(
        route,
        run_id=str(uuid.uuid4()),
        repo_root=repo_root,
        worktree=worktree,
        output_root=output_root,
        timeout_seconds=timeout_seconds,
    )
    retry_artifact["retry"] = {
        "initial_result": initial["status"],
        "initial_failure_class": initial["failure_class"],
        "retry_count": 1,
        "retry_result": retry_artifact["status"],
        "final_route_verdict": retry_artifact["status"],
    }
    return retry_artifact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-routes", action="store_true", help="run all 6 required routes")
    parser.add_argument("--runtime", choices=["claude_code", "codex_cli"])
    parser.add_argument("--agent", choices=["codebase-investigator", "web-researcher"])
    parser.add_argument("--profile", choices=["local_asset_research", "github_research", "grounded_research"])
    parser.add_argument("--worktree", default=str(REPO_ROOT))
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="write only subject/request metadata, never spawn a live runtime (hermetic testing)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    worktree = Path(args.worktree).resolve()

    if args.all_routes:
        routes = REQUIRED_ROUTES
    elif args.runtime and args.agent and args.profile:
        route = _find_route(args.runtime, args.agent, args.profile)
        if route is None:
            print(f"unknown route: {args.runtime}/{args.agent}/{args.profile}", file=sys.stderr)
            return 1
        routes = [route]
    else:
        parser.error("specify --all-routes or --runtime/--agent/--profile")
        return 1

    run_id = str(uuid.uuid4())
    output_root = Path(args.output_dir) if args.output_dir else (
        repo_root / ".claude" / "artifacts" / "agent-provider-route" / run_id
    )
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot create output directory {output_root}: {exc}", file=sys.stderr)
        return 1

    # Issue #1886 P0-3 fix_delta (PR #2005 adversarial review): every
    # artifact from a single producer invocation shares this batch_run_id
    # so validate_agent_provider_route_smoke.py's close-gate can prove all
    # required routes came from the SAME run (never a mix-and-match of
    # artifacts from different invocations/heads).
    batch_run_id = run_id

    artifacts: list[dict] = []
    for route in routes:
        if args.dry_run:
            artifact = {
                "schema": SCHEMA,
                "run_id": str(uuid.uuid4()),
                "batch_run_id": batch_run_id,
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "subject": compute_subject(route, repo_root),
                "spawn": {
                    "parent_session_id": "",
                    "child_session_id": "",
                    "native_spawn_event_observed": False,
                },
                "request": {
                    "profile": route["profile"],
                    "expected_provider": "agy",
                    "validation": "not_run",
                },
                "provider_observation": {
                    "selected_provider": None,
                    "provider_attempts": [],
                    "gemini_invocation_count": 0,
                    "direct_fallback_invocation_count": 0,
                },
                "route_evidence": {
                    "schema": GITHUB_RESEARCH_EVIDENCE_SCHEMA if route["profile"] == "github_research" else None,
                    "sha256": None,
                },
                "status": "skip",
                "failure_class": "agy_unavailable",
            }
        else:
            artifact = run_route_live(
                route,
                repo_root=repo_root,
                worktree=worktree,
                output_root=output_root,
                timeout_seconds=args.timeout_seconds,
            )
            artifact["batch_run_id"] = batch_run_id
        artifact_path = output_root / f"{_route_key(route).replace(':', '-')}.json"
        artifact_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        artifacts.append(artifact)
        print(f"[{artifact['status']}] {_route_key(route)} -> {artifact_path}")

    index = {
        "schema": "agent_provider_route_smoke_run_index/v1",
        "output_dir": str(output_root),
        "route_count": len(artifacts),
        "statuses": {_route_key(r): a["status"] for r, a in zip(routes, artifacts)},
    }
    (output_root / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
