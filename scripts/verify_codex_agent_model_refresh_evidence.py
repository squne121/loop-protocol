#!/usr/bin/env python3
"""Fail-closed verifier for CODEX_AGENT_MODEL_REFRESH_EVIDENCE_V1."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
REQUIRED_SMOKES = {
    ("gpt-5.6-terra", "low"),
    ("gpt-5.6-terra", "medium"),
    ("gpt-5.6-terra", "high"),
    ("gpt-5.6-luna", "low"),
    ("gpt-5.6-luna", "medium"),
}
AUTH_MODES = {"chatgpt", "api_key", "enterprise"}
SHA256_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def verdict(code: str | None, message: str, *, status: str) -> dict:
    return {
        "schema": "CODEX_AGENT_MODEL_REFRESH_EVIDENCE_VERDICT_V1",
        "status": status,
        "error_code": code,
        "message": message,
    }


def parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def read_json(path: Path, code: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(code) from exc
    if not isinstance(value, dict):
        raise ValueError(code)
    return value


def resolve_repo_path(value: object, field: str, *, allow_external: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}_missing")
    raw = Path(value)
    path = (raw if raw.is_absolute() else REPO_ROOT / raw).resolve()
    if not allow_external and not path.is_relative_to(REPO_ROOT):
        raise ValueError(f"{field}_outside_repo")
    return path


def fail(code: str, message: str, *, human: bool = False) -> tuple[int, dict]:
    status = "HUMAN_ACTION_REQUIRED" if human else "BLOCKED"
    return (2 if human else 1), verdict(code, message, status=status)


def validate(evidence: dict, evidence_path: Path) -> tuple[int, dict]:
    if evidence.get("schema") != "CODEX_AGENT_MODEL_REFRESH_EVIDENCE_V1":
        return fail("evidence_schema_invalid", "unexpected evidence schema")

    hook_trust = evidence.get("hook_trust")
    if not isinstance(hook_trust, dict) or hook_trust.get("status") != "trusted":
        return fail("hook_trust_unconfirmed", "hook trust is not mechanically confirmed", human=True)
    if not SHA256_RE.fullmatch(str(hook_trust.get("digest", ""))):
        return fail("hook_trust_digest_invalid", "trusted hook requires a SHA-256 digest", human=True)

    try:
        started = parse_time(evidence.get("started_at"), "started_at")
        completed = parse_time(evidence.get("completed_at"), "completed_at")
    except (ValueError, TypeError) as exc:
        return fail("evidence_time_invalid", str(exc))
    if completed < started or completed > datetime.now(timezone.utc):
        return fail("evidence_time_invalid", "evidence time range is invalid")
    max_age_seconds = evidence.get("max_age_seconds")
    if not isinstance(max_age_seconds, int) or max_age_seconds <= 0:
        return fail("freshness_policy_invalid", "max_age_seconds must be a positive integer")
    if (datetime.now(timezone.utc) - completed).total_seconds() > max_age_seconds:
        return fail("stale_or_replayed_evidence", "evidence is older than max_age_seconds")

    run_id = evidence.get("evidence_run_id")
    head = evidence.get("repo_head_sha")
    if not isinstance(run_id, str) or not run_id.strip():
        return fail("evidence_run_id_missing", "evidence_run_id is required")
    if not isinstance(head, str) or not HEAD_RE.fullmatch(head):
        return fail("repo_head_invalid", "repo_head_sha must be a full commit SHA")
    if head != evidence.get("expected_head_sha"):
        return fail("foreign_or_stale_commit", "evidence head does not bind to expected head")
    if not isinstance(evidence.get("worktree_dirty"), bool):
        return fail("worktree_state_missing", "worktree_dirty must be recorded")
    if not isinstance(evidence.get("codex_version"), str) or not evidence["codex_version"].strip():
        return fail("codex_version_missing", "Codex CLI version is required")
    if evidence.get("auth_mode") not in AUTH_MODES:
        return fail("auth_restriction", "auth mode is missing, restricted, or unsupported")
    if evidence.get("fallback_used") is not False:
        return fail("model_fallback_detected", "fallback must be explicitly false")

    smokes = evidence.get("direct_smokes")
    if not isinstance(smokes, list):
        return fail("direct_smoke_missing", "direct_smokes must be a list")
    seen_smokes: set[tuple[str, str]] = set()
    for smoke in smokes:
        if not isinstance(smoke, dict):
            return fail("direct_smoke_invalid", "direct smoke entry must be an object")
        pair = (smoke.get("model"), smoke.get("reasoning_effort"))
        if pair not in REQUIRED_SMOKES or pair in seen_smokes:
            return fail("direct_smoke_invalid", f"unexpected or duplicate direct smoke: {pair!r}")
        if smoke.get("status") != "pass" or smoke.get("fallback_used") is not False:
            return fail("model_unavailable_or_unsupported_effort", f"direct smoke did not pass without fallback: {pair!r}")
        try:
            observed_at = parse_time(smoke.get("observed_at"), "direct_smoke.observed_at")
        except (ValueError, TypeError) as exc:
            return fail("direct_smoke_invalid", str(exc))
        if not started <= observed_at <= completed:
            return fail("stale_or_replayed_evidence", "direct smoke timestamp is outside the evidence run")
        seen_smokes.add(pair)
    if seen_smokes != REQUIRED_SMOKES:
        return fail("direct_smoke_missing", "all distinct Terra/Luna model-effort pairs are required")

    try:
        ledger_path = resolve_repo_path(evidence.get("ledger_path"), "ledger_path", allow_external=True)
        ledger = read_json(ledger_path, "dispatch_evidence_missing")
        contract = read_json(CONTRACT_PATH, "runtime_contract_invalid")
    except ValueError as exc:
        return fail(str(exc), "launch ledger or runtime contract is absent or malformed")
    launches = ledger.get("launches")
    if not isinstance(launches, list):
        return fail("dispatch_evidence_missing", "ledger launches must be a list")
    observed_models: set[str] = set()
    observed_agents: set[str] = set()
    for launch in launches:
        if not isinstance(launch, dict):
            continue
        correlation = launch.get("correlation", {})
        if correlation.get("evidence_run_id") != run_id:
            continue
        if correlation.get("repo_head_sha") != head or correlation.get("worktree_dirty") != evidence["worktree_dirty"]:
            return fail("foreign_or_stale_commit", "launch correlation differs from evidence run")
        agent = launch.get("agent_name")
        expected = contract.get("required_agents", {}).get(agent)
        declared = launch.get("declared_runtime", {})
        observed = launch.get("observed_dispatch", {})
        if not expected:
            return fail("dispatch_agent_unknown", "launch agent is absent from allocation contract")
        for field in ("model", "session_id", "turn_id", "agent_id", "observed_at"):
            if not isinstance(observed.get(field), str) or not observed[field].strip():
                return fail("dispatch_evidence_missing", f"observed dispatch field missing: {field}")
        expected_declared = {
            "model": expected["model"],
            "reasoning_effort": expected["model_reasoning_effort"],
            "default_permissions": expected["default_permissions"],
        }
        if any(declared.get(key) != value for key, value in expected_declared.items()):
            return fail("runtime_contract_mismatch", "declared runtime differs from allocation contract")
        if observed["model"] != declared.get("model"):
            return fail("runtime_contract_mismatch", "observed dispatch differs from declared runtime")
        definition_path = resolve_repo_path(expected["path"], "agent_definition")
        actual_digest = hashlib.sha256(definition_path.read_bytes()).hexdigest()
        if declared.get("agent_definition_sha256", "").removeprefix("sha256:") != actual_digest:
            return fail("agent_definition_digest_mismatch", "agent definition digest differs from current file")
        try:
            observed_at = parse_time(observed["observed_at"], "observed_dispatch.observed_at")
        except (ValueError, TypeError) as exc:
            return fail("dispatch_evidence_missing", str(exc))
        if not started <= observed_at <= completed:
            return fail("stale_or_replayed_evidence", "launch timestamp is outside the evidence run")
        observed_models.add(observed["model"])
        observed_agents.add(agent)
    if not {"gpt-5.6-terra", "gpt-5.6-luna"} <= observed_models:
        return fail("terra_luna_coverage_missing", "both Terra and Luna observed dispatches are required")
    if not any(contract["required_agents"][name]["model"] == "gpt-5.6-terra" for name in observed_agents):
        return fail("terra_coverage_missing", "Terra custom-agent launch is required")
    if not any(contract["required_agents"][name]["model"] == "gpt-5.6-luna" for name in observed_agents):
        return fail("luna_coverage_missing", "Luna custom-agent launch is required")

    changed_files = evidence.get("changed_files")
    allowed_paths_status = evidence.get("allowed_paths_gate")
    if not isinstance(changed_files, list) or any(not isinstance(path, str) for path in changed_files):
        return fail("allowed_paths_evidence_missing", "changed_files must be recorded")
    if allowed_paths_status != "pass":
        return fail("allowed_paths_violation", "Allowed Paths gate must pass")

    digest = "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    result = verdict(None, "runtime evidence passed", status="PASS")
    result["evidence_sha256"] = digest
    return 0, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict-config", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = read_json(args.strict_config, "evidence_config_invalid")
    except ValueError:
        code, result = fail("evidence_config_invalid", "evidence config is absent or malformed")
    else:
        code, result = validate(evidence, args.strict_config)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
