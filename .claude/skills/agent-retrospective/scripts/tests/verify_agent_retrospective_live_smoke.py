#!/usr/bin/env python3
"""verify_agent_retrospective_live_smoke.py -- dual-runtime (Claude Code /
Claude-GPT) live smoke verifier for agent-retrospective (Issue #2239, Child 6
of #2192).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import run_retrospective as rr  # noqa: E402

_RESULT_SCHEMA = "AGENT_RETROSPECTIVE_LIVE_SMOKE_RESULT_V1"
_OBSERVER_SCHEMA_PATH = _SCRIPTS_DIR / "schemas" / "observer_result_v1.schema.json"
_LIVE_TIMEOUT_SEC = 180
_PREFLIGHT_TIMEOUT_SEC = 20
_ARTIFACTS_DIRNAME = "artifacts"

_RUNTIME_PROFILES = {
    "claude_code": {"runtime": "claude", "claude_bin": None, "claude_adapter": "native"},
    "claude_gpt": {"runtime": "claude", "claude_bin": "scripts/claude-gpt/launch.sh", "claude_adapter": "claude-gpt"},
}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _fail(reason_code: str, message: str, *, extra: dict[str, Any] | None = None) -> int:
    payload = {
        "schema": _RESULT_SCHEMA,
        "status": "fail",
        "reason_code": reason_code,
        "message": message,
    }
    if extra:
        payload.update(extra)
    _emit(payload)
    return 1


def _skip(reason: str) -> int:
    print(f"SKIP: {reason}", file=sys.stderr)
    _emit({"schema": _RESULT_SCHEMA, "status": "skip", "reason_code": "runtime_or_auth_unavailable", "message": reason})
    return 77


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=30)


def _repo_status_snapshot(repo_root: Path, *, exclude_relpath: str) -> set[str]:
    completed = _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    filtered = set()
    for line in lines:
        path = line[3:].strip()
        if path.startswith(exclude_relpath):
            continue
        filtered.add(line)
    return filtered


def _resolve_claude_code(repo_root: Path) -> tuple[str | None, list[str]]:
    resolved = shutil.which("claude")
    if resolved is None:
        return None, []
    return resolved, [resolved]


def _resolve_claude_gpt(repo_root: Path) -> tuple[str | None, list[str]]:
    launcher = repo_root / "scripts" / "claude-gpt" / "launch.sh"
    if not launcher.is_file() or not __import__("os").access(launcher, __import__("os").X_OK):
        return None, []
    return str(launcher), [str(launcher), "-C", str(repo_root)]


def _preflight_runtime_ok(argv_prefix: list[str], *, is_claude_gpt: bool) -> bool:
    try:
        if is_claude_gpt:
            completed = subprocess.run(
                [*argv_prefix, "--", "-p", "--output-format", "json", "--no-session-persistence"],
                input="reply with the single word: ok",
                capture_output=True,
                text=True,
                timeout=_PREFLIGHT_TIMEOUT_SEC,
            )
        else:
            completed = subprocess.run(
                [*argv_prefix, "auth", "status"], capture_output=True, text=True, timeout=_PREFLIGHT_TIMEOUT_SEC
            )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-profile", required=True, choices=sorted(_RUNTIME_PROFILES))
    parser.add_argument("--repo-root", required=False, default=None)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else _SCRIPTS_DIR.parents[3]
    profile = _RUNTIME_PROFILES[args.runtime_profile]
    is_claude_gpt = profile["claude_adapter"] == "claude-gpt"

    if is_claude_gpt:
        resolved_executable, argv_prefix = _resolve_claude_gpt(repo_root)
    else:
        resolved_executable, argv_prefix = _resolve_claude_code(repo_root)

    if resolved_executable is None:
        return _skip(f"skip_condition: runtime binary not available for profile={args.runtime_profile}")

    if not _preflight_runtime_ok(argv_prefix, is_claude_gpt=is_claude_gpt):
        return _skip(f"skip_condition: preflight check failed for profile={args.runtime_profile}")

    tested_head = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    if not tested_head:
        return _fail("tested_head_unresolvable", "could not resolve current HEAD via git rev-parse")

    artifacts_dir = _SCRIPTS_DIR / "tests" / _ARTIFACTS_DIRNAME
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    exclude_relpath = (
        str(artifacts_dir.relative_to(repo_root)) if artifacts_dir.is_relative_to(repo_root) else "__never__"
    )

    before_snapshot = _repo_status_snapshot(repo_root, exclude_relpath=exclude_relpath)

    nonce = uuid.uuid4().hex
    run_id = f"live-smoke-{args.runtime_profile}-{nonce}"
    base_sha = tested_head
    source_set_digest = hashlib.sha256(f"{run_id}:{base_sha}".encode("utf-8")).hexdigest()
    observer_id = "retrospective-runtime-observer"
    evidence_ref = f"evidence://live-smoke/{nonce}"

    expected_payload = {
        "schema_version": "observer_result/v1",
        "run_id": run_id,
        "base_sha": base_sha,
        "source_set_digest": source_set_digest,
        "observer_id": observer_id,
        "evidence_ref": evidence_ref,
        "findings": [{"claim": f"live-smoke-nonce:{nonce}", "claim_class": "process"}],
    }
    prompt = (
        "This is a deterministic dual-runtime live smoke verification (Issue #2239). "
        "Output ONLY a single JSON object conforming exactly to the observer_result/v1 "
        "schema, with EXACTLY these field values and no other fields (copy every value "
        "verbatim, do not paraphrase or alter any string):\n" + json.dumps(expected_payload, sort_keys=True)
    )
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    request = rr.AgentInvocationRequest(
        agent_name=observer_id,
        prompt=prompt,
        json_schema_path=str(_OBSERVER_SCHEMA_PATH),
        cwd=str(repo_root),
        timeout_sec=_LIVE_TIMEOUT_SEC,
    )
    policy = rr.DelegatedAgentPermissionPolicy(run_id=run_id)
    claude_gpt_launcher_receipt: dict[str, Any] | None = None

    if is_claude_gpt:
        schema_text = _OBSERVER_SCHEMA_PATH.read_text(encoding="utf-8")
        inner_argv = [
            "-p",
            "--agent",
            observer_id,
            "--output-format",
            "json",
            "--json-schema",
            schema_text,
            "--no-session-persistence",
            "--disallowedTools",
            *sorted(policy.denied_tools),
        ]
        full_argv = [*argv_prefix, "--", *inner_argv]
        import os as _os

        forwarded_env = policy.sanitize_subprocess_env(dict(_os.environ))
        forwarded_env["CLAUDE_GPT_RUNTIME_SMOKE_HOOKS"] = "subagent-start-stop"
        try:
            completed = subprocess.run(
                full_argv,
                cwd=str(repo_root),
                env=forwarded_env,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=_LIVE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return _fail("live_invocation_timeout", "claude-gpt launcher invocation timed out")
        if completed.returncode != 0:
            return _fail(
                "live_invocation_nonzero_exit",
                "claude-gpt launcher invocation returned non-zero exit",
                extra={"exit_code": completed.returncode, "stderr_excerpt": completed.stderr[:400]},
            )
        try:
            wrapper_payload = json.loads(completed.stdout)
            structured_output = wrapper_payload.get("structured_output")
            if not isinstance(structured_output, dict):
                raise ValueError("missing structured_output")
        except (json.JSONDecodeError, ValueError):
            return _fail("live_invocation_malformed_output", "claude-gpt launcher output was not parseable JSON")
        result = rr.AgentInvocationResult(
            status="ok", structured_output=structured_output, raw_stdout_excerpt=None, exit_code=0, reason_code=None
        )
        claude_gpt_launcher_receipt = {
            "resolved_executable": resolved_executable,
            "resolved_executable_digest": _sha256_file(Path(resolved_executable)),
        }
    else:
        result = rr.invoke_agent(request, policy=policy)

    fallback_used = False  # no runtime-profile-switch fallback is ever attempted by this script

    if result.status != "ok" or not isinstance(result.structured_output, dict):
        return _fail(
            "live_invocation_failed",
            f"invoke_agent status={result.status} reason_code={result.reason_code}",
            extra={"exit_code": result.exit_code, "raw_stdout_excerpt": result.raw_stdout_excerpt},
        )

    try:
        bundle = rr.EvidenceBundle.from_wire(
            json.dumps(result.structured_output, sort_keys=True, separators=(",", ":"))
        )
    except rr.WireContractError as exc:
        return _fail("evidence_bundle_invalid", str(exc), extra={"reason_code": exc.reason_code})

    if bundle.run_id != run_id or bundle.base_sha != base_sha or bundle.source_set_digest != source_set_digest:
        return _fail(
            "evidence_bundle_identity_mismatch",
            "run_id/base_sha/source_set_digest did not match the fixture bound into the prompt",
        )
    nonce_bound_ok = any(f"live-smoke-nonce:{nonce}" in json.dumps(finding) for finding in bundle.findings)
    if not nonce_bound_ok:
        return _fail("prompt_nonce_not_bound", "prompt nonce was not echoed back in the bundle's findings")

    ctx = rr.RunContext(base_sha_resolver=lambda: base_sha, run_id=run_id)
    plan = rr.SourcePlan(
        run_id=run_id,
        base_sha=base_sha,
        source_set_digest=source_set_digest,
        sources=["runtime"],
        generated_at=rr._iso(rr._utcnow()),
    )
    finding_sets = rr.build_finding_sets(ctx, plan, [bundle])
    evaluation = rr.Evaluation(
        run_id=run_id,
        base_sha=base_sha,
        source_set_digest=source_set_digest,
        candidate_records=[],
        evidence_ref=evidence_ref,
    )
    del finding_sets  # projected for schema-conformance proof only; finalize() itself needs only evaluation/plan/ctx
    publish_request = rr.finalize(
        ctx,
        plan,
        evaluation,
        repository_id="squne121/loop-protocol",
        target_issue=2239,
        request_id=f"live-smoke-req-{nonce}",
        idempotency_key=f"live-smoke-idem-{nonce}",
    )

    after_snapshot = _repo_status_snapshot(repo_root, exclude_relpath=exclude_relpath)
    fingerprint_clean = before_snapshot == after_snapshot

    receipt: dict[str, Any] = {
        "schema": _RESULT_SCHEMA,
        "status": "pass",
        "runtime_profile": args.runtime_profile,
        "adapter": profile["claude_adapter"],
        "resolved_executable": resolved_executable,
        "resolved_executable_digest": _sha256_file(Path(resolved_executable)),
        "tested_head": tested_head,
        "repository_fingerprint_diff_clean": fingerprint_clean,
        "fallback_used": fallback_used,
        "run_id": run_id,
        "base_sha": base_sha,
        "source_set_digest": source_set_digest,
        "nonce": nonce,
        "prompt_digest": prompt_digest,
        "exit_code": result.exit_code,
        "publish_request": json.loads(publish_request.to_wire()),
    }
    if claude_gpt_launcher_receipt is not None:
        receipt["claude_gpt_launcher_receipt"] = claude_gpt_launcher_receipt

    artifact_path = artifacts_dir / f"live_smoke_{args.runtime_profile}_{int(time.time())}_{nonce}.json"
    artifact_path.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")

    if not fingerprint_clean:
        return _fail(
            "repository_fingerprint_diff_detected",
            "repository status changed (outside artifacts/) between pre/post live invocation",
            extra={"before": sorted(before_snapshot), "after": sorted(after_snapshot)},
        )

    _emit(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
