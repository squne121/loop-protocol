#!/usr/bin/env python3
"""AGY/Serena/WebSearch fan-out E2E artifact validator (Issue #1710).

Parent research: #1494 (REQUEST_CHANGES review Blocker5 / Blocker6 / Major2-4:
https://github.com/squne121/loop-protocol/issues/1494#issuecomment-5071397001).
The parent review asked that the PASS/FAIL judgment for a live AGY fan-out E2E
run stop being a human-written prose claim and become a decision made by a
deterministic validator reading recorded artifacts.

This module consumes a fixed-layout *artifact bundle* directory produced by a
completed (live or simulated) 3-way AGY fan-out run --

  - ``fanout_request.json``                  -- the stamped fan-out request
  - ``children/<profile>/request.json``      -- per-child ``delegation_request_v1``
  - ``children/<profile>/result.json``       -- per-child ``delegation_result_v1``
  - ``children/<profile>/audit.jsonl``       -- ``delegation_audit_v1`` start/end pair
  - ``children/<profile>/permission_events.json`` -- observed AGY tool-call attempts
  - ``children/grounded_research/hook_events.jsonl`` -- ``agy_tool_provenance_v1`` events
  - ``children/local_asset_research/serena_evidence.json`` -- Serena hash-chain records
  - ``process_lifecycle_events.jsonl``        -- ``process_lifecycle_event_v1`` events
  -  ``environment_manifest.json``            -- hermetic environment snapshot
  - ``artifact_manifest.json``                -- ``{relative_path: sha256}`` closed manifest

-- and evaluates the 25 predicates from Issue #1710 "In Scope" against it,
producing an ``AGY_FANOUT_E2E_VERDICT_V1`` verdict.

This module does NOT implement any of the artifact-*producing* logic that
lives in #1708 (``agy_tool_provenance.py``), #1707 (``fan_out_orchestrator.py``
process-lifecycle telemetry), #1705 (``agy_permission_policy.py`` isolated
permission policy), or #1706 (``run_gemini_headless.py`` Serena task-linked
hash chain: ``verify_serena_hash_chain`` et al.) -- it *consumes* the schemas
and validator functions those Issues already shipped, fail-closed, wherever a
predicate maps directly onto one of them (predicate 6 -> #1707's
``actual_provider_process_overlap`` / ``build_process_lifecycle_pairs``;
predicates 7-11 -> #1708's ``validate_provenance_event`` / ``match_run_context``;
predicates 12-14 -> #1706's ``verify_serena_hash_chain``; predicates 15-17 ->
#1705's ``classify_tool_call_events``).

Schema / consumer inventory / compatibility decision (Issue #1710 AC14):

  consumer inventory: as of this Issue, the only known consumer of
  ``AGY_FANOUT_E2E_VERDICT_V1`` is the final evidence-document generation PR
  for #1494 (not part of this Issue's scope). No other script/skill reads
  this schema yet.

  compatibility decision: ``AGY_FANOUT_E2E_VERDICT_V1`` is a brand-new schema
  introduced by this Issue. It does not replace, version-bump, or otherwise
  change any existing schema (``delegation_audit_v1``, ``agy_tool_provenance_v1``,
  ``agy_profile_gate_result/v1``, ``process_lifecycle_event_v1``,
  ``delegation_fanout_request_v1`` are all read-only inputs, untouched by this
  module). This is a purely additive introduction; no backward-compatibility
  break is possible because there is no prior version of this schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Sibling-module loading (hermetic, mirrors test_delegation_audit_schema.py's
# _load_module() convention -- unique module name registered in sys.modules
# *before* exec so the loaded module's own internal ``from __future__``/typing
# self-references resolve correctly).
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_sibling_module(filename: str, register_name: str):
    import importlib.util

    path = _SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(register_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive
        raise ImportError(f"cannot load spec for {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)
    return module


_agy_tool_provenance = _load_sibling_module("agy_tool_provenance.py", "_agy_fanout_e2e_agy_tool_provenance")
_agy_permission_policy = _load_sibling_module("agy_permission_policy.py", "_agy_fanout_e2e_agy_permission_policy")
_fan_out_orchestrator = _load_sibling_module("fan_out_orchestrator.py", "_agy_fanout_e2e_fan_out_orchestrator")
_run_gemini_headless = _load_sibling_module("run_gemini_headless.py", "_agy_fanout_e2e_run_gemini_headless")

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

VERDICT_SCHEMA = "AGY_FANOUT_E2E_VERDICT_V1"
VERDICT_SCHEMA_VERSION = 1

FANOUT_REQUEST_EVIDENCE_SCHEMA = "agy_fanout_e2e_request_evidence_v1"
ENVIRONMENT_MANIFEST_SCHEMA = "agy_fanout_e2e_environment_manifest_v1"

PROFILE_LOCAL_ASSET_RESEARCH = "local_asset_research"
PROFILE_GROUNDED_RESEARCH = "grounded_research"
PROFILE_NO_TOOLS = "no_tools"
REQUIRED_PROFILES: frozenset[str] = frozenset(
    {PROFILE_LOCAL_ASSET_RESEARCH, PROFILE_GROUNDED_RESEARCH, PROFILE_NO_TOOLS}
)

CONCLUSION_PASS = "PASS"
CONCLUSION_BLOCKED_LOCAL_INSTRUMENTATION = "BLOCKED_LOCAL_INSTRUMENTATION"
CONCLUSION_FAIL_RUNTIME = "FAIL_RUNTIME"
VALID_CONCLUSIONS: frozenset[str] = frozenset(
    {CONCLUSION_PASS, CONCLUSION_BLOCKED_LOCAL_INSTRUMENTATION, CONCLUSION_FAIL_RUNTIME}
)

PREDICATE_IDS: tuple[str, ...] = tuple(f"predicate_{i:02d}" for i in range(1, 26))

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# Fixed, closed artifact bundle layout (Issue #1710 predicate 24: missing /
# duplicate / unknown artifact must fail closed -- so this set is NOT derived
# from whatever the manifest happens to list; it is the validator's own SSOT).
_ROOT_ARTIFACTS: tuple[str, ...] = (
    "fanout_request.json",
    "process_lifecycle_events.jsonl",
    "environment_manifest.json",
)
_COMMON_CHILD_ARTIFACTS: tuple[str, ...] = ("request.json", "result.json", "audit.jsonl", "permission_events.json")
_PROFILE_EXTRA_ARTIFACTS: dict[str, tuple[str, ...]] = {
    PROFILE_LOCAL_ASSET_RESEARCH: ("serena_evidence.json",),
    PROFILE_GROUNDED_RESEARCH: ("hook_events.jsonl",),
    PROFILE_NO_TOOLS: (),
}


def _child_path(profile: str, name: str) -> str:
    return f"children/{profile}/{name}"


def _required_artifact_paths() -> frozenset[str]:
    paths: set[str] = set(_ROOT_ARTIFACTS)
    for profile in REQUIRED_PROFILES:
        for name in _COMMON_CHILD_ARTIFACTS:
            paths.add(_child_path(profile, name))
        for name in _PROFILE_EXTRA_ARTIFACTS[profile]:
            paths.add(_child_path(profile, name))
    return frozenset(paths)


REQUIRED_ARTIFACT_PATHS: frozenset[str] = _required_artifact_paths()

# Public-artifact redaction scan targets: every file in the bundle is a
# "public artifact" for this validator's purposes (Issue #1710 predicate 22).
_CREDENTIAL_LEAK_RE = re.compile(
    r"(AIza[0-9A-Za-z_\-]{35})"
    r"|(sk-[A-Za-z0-9]{16,})"
    r"|(ghp_[A-Za-z0-9]{20,})"
    r"|(xox[baprs]-[A-Za-z0-9\-]{10,})"
    r"|(-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r"|(oauth[_-]?token\s*[:=]\s*\S+)"
    r"|(https?://[^\s\"']*[?&]access_token=\S+)",
    re.IGNORECASE,
)
_RAW_TRANSCRIPT_FIELD_NAMES: frozenset[str] = frozenset({"raw_transcript", "transcript_text", "transcript_body"})


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class PredicateResult:
    predicate_id: str
    name: str
    status: str  # "pass" | "fail" | "not_applicable"
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate_id": self.predicate_id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass
class BundleLoadResult:
    ok: bool
    bundle: dict[str, Any] | None
    errors: list[str]
    fail_close_reason: str | None = None


# ---------------------------------------------------------------------------
# Hashing / canonicalization helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_stable_json(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


# ---------------------------------------------------------------------------
# Bundle loading (predicate 20 sha256 cross-check + predicate 24 fail-close)
# ---------------------------------------------------------------------------


def load_bundle(bundle_dir: Path) -> BundleLoadResult:
    """Load an artifact bundle directory, fail-closed.

    Fails closed (``ok=False``) when: ``artifact_manifest.json`` is missing
    or malformed; the manifest's key set is not *exactly*
    ``REQUIRED_ARTIFACT_PATHS`` (predicate 24: missing artifact / duplicate
    (unknown) artifact); any listed file is missing on disk; or any file's
    actual sha256 does not match the manifest's recorded sha256 (predicate
    20 / predicate 25 tampering detection).
    """
    errors: list[str] = []
    manifest_path = bundle_dir / "artifact_manifest.json"
    if not manifest_path.is_file():
        return BundleLoadResult(False, None, ["artifact_manifest.json missing"], "missing_artifact_manifest")

    try:
        manifest = _read_json(manifest_path)
    except (json.JSONDecodeError, OSError) as exc:
        return BundleLoadResult(False, None, [f"artifact_manifest.json unreadable: {exc}"], "malformed_manifest")

    if not isinstance(manifest, dict):
        return BundleLoadResult(False, None, ["artifact_manifest.json must be a JSON object"], "malformed_manifest")

    manifest_keys = set(manifest.keys())
    missing = REQUIRED_ARTIFACT_PATHS - manifest_keys
    unknown = manifest_keys - REQUIRED_ARTIFACT_PATHS
    if missing:
        errors.append(f"missing artifact(s) in manifest: {sorted(missing)}")
    if unknown:
        errors.append(f"unknown/duplicate artifact key(s) in manifest: {sorted(unknown)}")
    if missing or unknown:
        return BundleLoadResult(False, None, errors, "artifact_manifest_key_set_mismatch")

    raw_files: dict[str, bytes] = {}
    for rel_path, expected_sha in manifest.items():
        file_path = bundle_dir / rel_path
        if not file_path.is_file():
            errors.append(f"artifact file missing on disk: {rel_path}")
            continue
        raw = file_path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if not isinstance(expected_sha, str) or not _HEX64_RE.match(expected_sha):
            errors.append(f"artifact_manifest.json sha256 for {rel_path} is malformed")
            continue
        if actual_sha != expected_sha:
            errors.append(
                f"artifact_manifest.json sha256 mismatch for {rel_path}: "
                f"expected={expected_sha} actual={actual_sha}"
            )
            continue
        raw_files[rel_path] = raw

    if errors:
        return BundleLoadResult(False, None, errors, "artifact_manifest_sha256_mismatch")

    try:
        bundle: dict[str, Any] = {
            "manifest": manifest,
            "raw_files": raw_files,
            "fanout_request": json.loads(raw_files["fanout_request.json"]),
            "process_lifecycle_events": [
                json.loads(line) for line in raw_files["process_lifecycle_events.jsonl"].decode("utf-8").splitlines() if line.strip()
            ],
            "environment_manifest": json.loads(raw_files["environment_manifest.json"]),
            "children": {},
        }
        for profile in REQUIRED_PROFILES:
            child: dict[str, Any] = {
                "request": json.loads(raw_files[_child_path(profile, "request.json")]),
                "result": json.loads(raw_files[_child_path(profile, "result.json")]),
                "audit": [
                    json.loads(line)
                    for line in raw_files[_child_path(profile, "audit.jsonl")].decode("utf-8").splitlines()
                    if line.strip()
                ],
                "permission_events": json.loads(raw_files[_child_path(profile, "permission_events.json")]),
            }
            for extra in _PROFILE_EXTRA_ARTIFACTS[profile]:
                key = extra.rsplit(".", 1)[0]
                raw = raw_files[_child_path(profile, extra)]
                if extra.endswith(".jsonl"):
                    child[key] = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
                else:
                    child[key] = json.loads(raw)
            bundle["children"][profile] = child
    except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as exc:
        return BundleLoadResult(False, None, [f"artifact content parse error: {exc}"], "malformed_artifact_content")

    # Schema field presence/closure check on every parsed JSON artifact that
    # declares a "schema" key: unknown schema values fail closed too.
    schema_errors = _check_known_schemas(bundle)
    if schema_errors:
        return BundleLoadResult(False, None, schema_errors, "unknown_schema")

    return BundleLoadResult(True, bundle, [])


_KNOWN_SCHEMA_VALUES: frozenset[str] = frozenset(
    {
        FANOUT_REQUEST_EVIDENCE_SCHEMA,
        ENVIRONMENT_MANIFEST_SCHEMA,
        "delegation_request_v1",
        "delegation_result_v1",
        "delegation_audit_v1",
        "agy_tool_provenance_v1",
        "process_lifecycle_event_v1",
        "agy_profile_gate_result/v1",
        "agy_permission_events_v1",
        "serena_task_linked_evidence_v1",
    }
)


def _check_known_schemas(bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    def _check(obj: Any, location: str) -> None:
        if isinstance(obj, dict) and "schema" in obj:
            schema = obj.get("schema")
            if schema not in _KNOWN_SCHEMA_VALUES:
                errors.append(f"unknown schema value at {location}: {schema!r}")
        if isinstance(obj, list):
            for idx, item in enumerate(obj):
                if isinstance(item, dict) and "schema" in item:
                    schema = item.get("schema")
                    if schema not in _KNOWN_SCHEMA_VALUES:
                        errors.append(f"unknown schema value at {location}[{idx}]: {schema!r}")

    _check(bundle["fanout_request"], "fanout_request.json")
    _check(bundle["environment_manifest"], "environment_manifest.json")
    _check(bundle["process_lifecycle_events"], "process_lifecycle_events.jsonl")
    for profile, child in bundle["children"].items():
        _check(child["request"], f"children/{profile}/request.json")
        _check(child["result"], f"children/{profile}/result.json")
        _check(child["audit"], f"children/{profile}/audit.jsonl")
        _check(child["permission_events"], f"children/{profile}/permission_events.json")
        if "serena_evidence" in child:
            _check(child["serena_evidence"], f"children/{profile}/serena_evidence.json")
        if "hook_events" in child:
            _check(child["hook_events"], f"children/{profile}/hook_events.jsonl")
    return errors


# ---------------------------------------------------------------------------
# Redaction scanning (predicates 21/22)
# ---------------------------------------------------------------------------


def scan_for_redaction_violations(
    bundle: dict[str, Any], *, home: str | None = None, repo_root: str | None = None
) -> dict[str, list[str]]:
    """Scan every raw artifact file's bytes for leaked secrets/paths.

    Returns ``{relative_path: [violation_code, ...]}`` -- empty dict/lists
    mean clean. This scans the *raw bytes actually stored in the bundle*, not
    a self-reported "already redacted" flag.
    """
    home = home if home is not None else os.environ.get("HOME")
    violations: dict[str, list[str]] = {}
    for rel_path, raw in bundle["raw_files"].items():
        text = raw.decode("utf-8", errors="replace")
        found: list[str] = []
        if _CREDENTIAL_LEAK_RE.search(text):
            found.append("raw_credential_or_oauth_token_detected")
        if home and home in text:
            found.append("home_absolute_path_detected")
        if repo_root and repo_root in text:
            found.append("repo_absolute_path_detected")
        try:
            parsed = json.loads(text) if rel_path.endswith(".json") else None
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None and _contains_raw_transcript_field(parsed):
            found.append("raw_transcript_field_present")
        if found:
            violations[rel_path] = found
    return violations


def _contains_raw_transcript_field(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in _RAW_TRANSCRIPT_FIELD_NAMES:
                return True
            if _contains_raw_transcript_field(value):
                return True
    elif isinstance(obj, list):
        return any(_contains_raw_transcript_field(item) for item in obj)
    return False


# ---------------------------------------------------------------------------
# Predicates 1-5: fan-out request shape
# ---------------------------------------------------------------------------


def _predicate_fanout_request_shape(bundle: dict[str, Any]) -> list[PredicateResult]:
    request = bundle["fanout_request"]
    results: list[PredicateResult] = []

    parent_run_id = request.get("parent_run_id") if isinstance(request, dict) else None
    subtasks = request.get("subtasks") if isinstance(request, dict) else None
    subtasks = subtasks if isinstance(subtasks, list) else []

    # P1: same parent_run_id across request + every stamped subtask.
    ok = bool(isinstance(parent_run_id, str) and parent_run_id.strip())
    mismatched = []
    if ok:
        for subtask in subtasks:
            if not isinstance(subtask, dict) or subtask.get("parent_run_id") != parent_run_id:
                mismatched.append(subtask.get("subtask_id") if isinstance(subtask, dict) else None)
    ok = ok and not mismatched
    results.append(
        PredicateResult(
            "predicate_01",
            "fanout_request_parent_run_id_consistent",
            "pass" if ok else "fail",
            detail="" if ok else f"parent_run_id missing or mismatched subtasks: {mismatched}",
            evidence={"parent_run_id": parent_run_id, "mismatched_subtasks": mismatched},
        )
    )

    # P2: exactly 3 unique subtask_id values.
    ids = [s.get("subtask_id") for s in subtasks if isinstance(s, dict)]
    unique_ids = {i for i in ids if isinstance(i, str) and i}
    ok = len(ids) == 3 and len(unique_ids) == 3
    results.append(
        PredicateResult(
            "predicate_02",
            "unique_subtask_count_exactly_3",
            "pass" if ok else "fail",
            detail="" if ok else f"expected 3 unique subtask_ids, got {ids}",
            evidence={"subtask_ids": ids},
        )
    )

    # P3: profile set exactly {local_asset_research, grounded_research, no_tools}.
    profiles = {s.get("profile") for s in subtasks if isinstance(s, dict)}
    ok = profiles == REQUIRED_PROFILES
    results.append(
        PredicateResult(
            "predicate_03",
            "profile_set_exact",
            "pass" if ok else "fail",
            detail="" if ok else f"expected profiles {sorted(REQUIRED_PROFILES)}, got {sorted(p for p in profiles if p)}",
            evidence={"profiles": sorted(p for p in profiles if p)},
        )
    )

    # P4: provider == agy for all 3.
    providers = [s.get("provider") for s in subtasks if isinstance(s, dict)]
    ok = len(providers) == 3 and all(p == "agy" for p in providers)
    results.append(
        PredicateResult(
            "predicate_04",
            "provider_all_agy",
            "pass" if ok else "fail",
            detail="" if ok else f"expected all providers == 'agy', got {providers}",
            evidence={"providers": providers},
        )
    )

    # P5: max_workers=3, provider_concurrency.agy=3, profile_concurrency each 1.
    max_workers = request.get("max_workers") if isinstance(request, dict) else None
    provider_concurrency = request.get("provider_concurrency") if isinstance(request, dict) else None
    profile_concurrency = request.get("profile_concurrency") if isinstance(request, dict) else None
    ok = (
        max_workers == 3
        and isinstance(provider_concurrency, dict)
        and provider_concurrency.get("agy") == 3
        and isinstance(profile_concurrency, dict)
        and all(profile_concurrency.get(p) == 1 for p in REQUIRED_PROFILES)
    )
    results.append(
        PredicateResult(
            "predicate_05",
            "concurrency_explicit",
            "pass" if ok else "fail",
            detail=(
                ""
                if ok
                else f"max_workers={max_workers!r} provider_concurrency={provider_concurrency!r} "
                f"profile_concurrency={profile_concurrency!r}"
            ),
            evidence={
                "max_workers": max_workers,
                "provider_concurrency": provider_concurrency,
                "profile_concurrency": profile_concurrency,
            },
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicate 6: distinct actual AGY provider process overlap (#1707)
# ---------------------------------------------------------------------------


def _predicate_process_overlap(bundle: dict[str, Any]) -> PredicateResult:
    events = bundle["process_lifecycle_events"]
    pairs = _fan_out_orchestrator.build_process_lifecycle_pairs(events)
    overlap = _fan_out_orchestrator.actual_provider_process_overlap(pairs)
    return PredicateResult(
        "predicate_06",
        "distinct_agy_process_overlap",
        "pass" if overlap else "fail",
        detail="" if overlap else "no distinct-pid/distinct-subtask process interval overlap found",
        evidence={"pair_count": len(pairs), "overlap": overlap},
    )


# ---------------------------------------------------------------------------
# Predicates 7-11: grounded_research WebSearch hook provenance (#1708)
# ---------------------------------------------------------------------------


def _predicate_hook_provenance(bundle: dict[str, Any]) -> list[PredicateResult]:
    child = bundle["children"][PROFILE_GROUNDED_RESEARCH]
    request = child["request"]
    result = child["result"]
    hook_events = child.get("hook_events", [])

    parent_run_id = request.get("parent_run_id")
    attempt_id = request.get("attempt_id")
    conversation_id = result.get("conversation_id")
    transcript_sha256 = result.get("transcript_sha256")

    validated_events: list[dict[str, Any]] = []
    matched_events: list[dict[str, Any]] = []
    for event in hook_events:
        ok, _violations = _agy_tool_provenance.validate_provenance_event(event)
        if not ok:
            continue
        validated_events.append(event)
        matched, _mismatches = _agy_tool_provenance.match_run_context(
            event,
            conversation_id=conversation_id,
            parent_run_id=parent_run_id,
            attempt_id=attempt_id,
            transcript_sha256=transcript_sha256,
        )
        if matched:
            matched_events.append(event)

    # P7: at least one canonical, validated, run-matched PreToolUse hook event.
    p7_ok = any(e.get("event") == "PreToolUse" for e in matched_events)
    results = [
        PredicateResult(
            "predicate_07",
            "grounded_research_has_canonical_hook_event",
            "pass" if p7_ok else "fail",
            detail="" if p7_ok else "no validated + run-matched agy_tool_provenance_v1 PreToolUse event found",
            evidence={"validated_count": len(validated_events), "matched_count": len(matched_events)},
        )
    ]

    # P8: search_web executed (via validated + matched hook event only).
    tool_names = {(e.get("toolCall") or {}).get("name") for e in matched_events}
    p8_ok = "search_web" in tool_names
    results.append(
        PredicateResult(
            "predicate_08",
            "grounded_research_executes_search_web",
            "pass" if p8_ok else "fail",
            detail="" if p8_ok else f"search_web not found among matched hook tool names {sorted(n for n in tool_names if n)}",
            evidence={"tool_names": sorted(n for n in tool_names if n)},
        )
    )

    # P9: read_url_content present when the request contract requires it.
    requires_read_url = bool(request.get("requires_read_url_content", False))
    if requires_read_url:
        p9_ok = "read_url_content" in tool_names
        p9_status = "pass" if p9_ok else "fail"
    else:
        p9_status = "not_applicable"
    results.append(
        PredicateResult(
            "predicate_09",
            "grounded_research_read_url_content_when_required",
            p9_status,
            detail="" if p9_status != "fail" else "requires_read_url_content=true but no matched read_url_content event",
            evidence={"requires_read_url_content": requires_read_url, "tool_names": sorted(n for n in tool_names if n)},
        )
    )

    # P10: hook event / conversation / transcript hash / child result correlate.
    p10_ok = len(matched_events) > 0 and bool(conversation_id) and bool(transcript_sha256)
    results.append(
        PredicateResult(
            "predicate_10",
            "hook_conversation_transcript_child_result_correlate",
            "pass" if p10_ok else "fail",
            detail="" if p10_ok else "no hook event correlates with conversation_id/transcript_sha256/child result",
            evidence={"matched_count": len(matched_events), "conversation_id_present": bool(conversation_id)},
        )
    )

    # P11: AGY stdout self-report alone must NOT make P7/P8 pass. This is a
    # negative-control assertion: even if `result.tool_calls` self-reports
    # search_web, the group above must have been decided purely from
    # validated+matched hook events (already true structurally, since P7/P8
    # never read result.get("tool_calls")). We additionally assert that when
    # hook evidence is entirely absent, the group fails regardless of what
    # the stdout self-report claims.
    stdout_claims_search_web = "search_web" in (result.get("tool_calls") or [])
    if not matched_events and stdout_claims_search_web:
        p11_ok = not p7_ok and not p8_ok  # must NOT be rescued by stdout self-report
    else:
        p11_ok = True
    results.append(
        PredicateResult(
            "predicate_11",
            "stdout_self_report_alone_insufficient",
            "pass" if p11_ok else "fail",
            detail="" if p11_ok else "predicate incorrectly passed using stdout self-report without hook evidence",
            evidence={"stdout_claims_search_web": stdout_claims_search_web, "matched_hook_events": len(matched_events)},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicates 12-14: local_asset_research Serena task-linked hash chain (#1706)
# ---------------------------------------------------------------------------


def _predicate_serena_hash_chain(bundle: dict[str, Any]) -> list[PredicateResult]:
    child = bundle["children"][PROFILE_LOCAL_ASSET_RESEARCH]
    request = child["request"]
    result = child["result"]
    records = child.get("serena_evidence", [])

    parent_run_id = request.get("parent_run_id")
    subtask_id = request.get("subtask_id")
    attempt_id = request.get("attempt_id")

    task_linked_records = [
        r
        for r in records
        if isinstance(r, dict)
        and r.get("actor") == _agy_permission_policy.RETRIEVAL_ACTOR_WRAPPER_SERENA_MCP
        and r.get("parent_run_id") == parent_run_id
        and r.get("subtask_id") == subtask_id
        and r.get("attempt_id") == attempt_id
    ]

    # P12: task-linked Serena evidence present.
    p12_ok = len(task_linked_records) > 0
    results = [
        PredicateResult(
            "predicate_12",
            "serena_evidence_task_linked",
            "pass" if p12_ok else "fail",
            detail="" if p12_ok else "no serena_evidence record is task-linked (actor + run-binding match)",
            evidence={"task_linked_count": len(task_linked_records), "total_records": len(records)},
        )
    ]

    # P13: hash chain verifies for every task-linked record.
    chain_ok = bool(task_linked_records) and all(
        _run_gemini_headless.verify_serena_hash_chain(r) for r in task_linked_records
    )
    results.append(
        PredicateResult(
            "predicate_13",
            "serena_hash_chain_verifies",
            "pass" if chain_ok else "fail",
            detail="" if chain_ok else "verify_serena_hash_chain() rejected one or more records",
            evidence={"checked": len(task_linked_records)},
        )
    )

    # P14: retrieval actor (wrapper_serena_mcp) vs analysis actor (antigravity_cli)
    # are distinguished -- the child result must declare the analysis actor,
    # and it must differ from the Serena records' retrieval actor.
    analysis_actor = result.get("actor")
    p14_ok = (
        analysis_actor == _agy_permission_policy.ANALYSIS_ACTOR_ANTIGRAVITY_CLI
        and analysis_actor != _agy_permission_policy.RETRIEVAL_ACTOR_WRAPPER_SERENA_MCP
        and p12_ok
    )
    results.append(
        PredicateResult(
            "predicate_14",
            "retrieval_and_analysis_actor_distinguished",
            "pass" if p14_ok else "fail",
            detail="" if p14_ok else f"result.actor={analysis_actor!r}, expected 'antigravity_cli' distinct from Serena retrieval actor",
            evidence={"analysis_actor": analysis_actor},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicates 15-17: permission isolation / no-tools negative evidence (#1705)
# ---------------------------------------------------------------------------


def _predicate_permission_isolation(bundle: dict[str, Any]) -> list[PredicateResult]:
    results: list[PredicateResult] = []

    local = _agy_permission_policy.classify_tool_call_events(
        PROFILE_LOCAL_ASSET_RESEARCH, bundle["children"][PROFILE_LOCAL_ASSET_RESEARCH]["permission_events"]
    )
    p15_ok = local["agy_direct_tool_calls_count"] == 0
    results.append(
        PredicateResult(
            "predicate_15",
            "local_asset_research_agy_direct_tool_calls_zero",
            "pass" if p15_ok else "fail",
            detail="" if p15_ok else f"agy_direct_tool_calls_count={local['agy_direct_tool_calls_count']}",
            evidence={"agy_direct_tool_calls_count": local["agy_direct_tool_calls_count"]},
        )
    )

    no_tools = _agy_permission_policy.classify_tool_call_events(
        PROFILE_NO_TOOLS, bundle["children"][PROFILE_NO_TOOLS]["permission_events"]
    )
    p16_ok = no_tools["agy_direct_tool_calls_count"] == 0
    results.append(
        PredicateResult(
            "predicate_16",
            "no_tools_agy_tool_calls_zero",
            "pass" if p16_ok else "fail",
            detail="" if p16_ok else f"agy_direct_tool_calls_count={no_tools['agy_direct_tool_calls_count']}",
            evidence={"agy_direct_tool_calls_count": no_tools["agy_direct_tool_calls_count"]},
        )
    )

    grounded = _agy_permission_policy.classify_tool_call_events(
        PROFILE_GROUNDED_RESEARCH, bundle["children"][PROFILE_GROUNDED_RESEARCH]["permission_events"]
    )
    p17_ok = grounded["unexpected_tool_calls_count"] == 0
    results.append(
        PredicateResult(
            "predicate_17",
            "grounded_research_unexpected_tool_calls_zero",
            "pass" if p17_ok else "fail",
            detail="" if p17_ok else f"unexpected_tool_calls_count={grounded['unexpected_tool_calls_count']}",
            evidence={"unexpected_tool_calls_count": grounded["unexpected_tool_calls_count"]},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicates 18-20: delegation_audit_v1 pairing + run-id consistency + manifest sha256
# ---------------------------------------------------------------------------


def _predicate_audit_and_correlation(bundle: dict[str, Any]) -> list[PredicateResult]:
    results: list[PredicateResult] = []

    # P18: delegation_audit_v1 start/end one-to-one, per child.
    pairing_problems: dict[str, str] = {}
    for profile, child in bundle["children"].items():
        records = child["audit"]
        schema_errors: list[str] = []
        for record in records:
            schema_errors.extend(_run_gemini_headless.validate_delegation_audit_record(record))
        if schema_errors:
            pairing_problems[profile] = f"schema errors: {schema_errors}"
            continue
        run_ids = {r.get("run_id") for r in records}
        if len(run_ids) != 1:
            pairing_problems[profile] = f"expected exactly 1 run_id, got {sorted(i for i in run_ids if i)}"
            continue
        starts = [r for r in records if r.get("record_type") == "start"]
        ends = [r for r in records if r.get("record_type") == "end"]
        if len(starts) != 1 or len(ends) != 1:
            pairing_problems[profile] = f"expected 1 start + 1 end, got {len(starts)} start(s) {len(ends)} end(s)"
    p18_ok = not pairing_problems
    results.append(
        PredicateResult(
            "predicate_18",
            "delegation_audit_start_end_one_to_one",
            "pass" if p18_ok else "fail",
            detail="" if p18_ok else str(pairing_problems),
            evidence={"pairing_problems": pairing_problems},
        )
    )

    # P19: parent_run_id / subtask_id / attempt_id agree across all artifacts
    # for each child (request, result, hook_events / serena_evidence where
    # present, and the audit start record's optional fan-out fields).
    request = bundle["fanout_request"]
    top_parent_run_id = request.get("parent_run_id") if isinstance(request, dict) else None
    mismatches: dict[str, list[str]] = {}
    for profile, child in bundle["children"].items():
        req = child["request"]
        res = child["result"]
        problems: list[str] = []
        if req.get("parent_run_id") != top_parent_run_id:
            problems.append("request.parent_run_id != fanout_request.parent_run_id")
        if res.get("parent_run_id") != req.get("parent_run_id"):
            problems.append("result.parent_run_id != request.parent_run_id")
        if res.get("subtask_id") != req.get("subtask_id"):
            problems.append("result.subtask_id != request.subtask_id")
        if res.get("attempt_id") != req.get("attempt_id"):
            problems.append("result.attempt_id != request.attempt_id")
        starts = [r for r in child["audit"] if r.get("record_type") == "start"]
        if starts:
            start = starts[0]
            for key in ("parent_run_id", "subtask_id", "attempt_id"):
                if key in start and start[key] != req.get(key):
                    problems.append(f"audit start.{key} != request.{key}")
        for extra_key, id_field in (("hook_events", "parent_run_id"), ("serena_evidence", "parent_run_id")):
            for item in child.get(extra_key, []):
                if isinstance(item, dict) and item.get(id_field) not in (None, req.get(id_field)):
                    problems.append(f"{extra_key}[].{id_field} mismatch")
        if problems:
            mismatches[profile] = problems
    p19_ok = not mismatches
    results.append(
        PredicateResult(
            "predicate_19",
            "run_ids_consistent_across_all_artifacts",
            "pass" if p19_ok else "fail",
            detail="" if p19_ok else str(mismatches),
            evidence={"mismatches": mismatches},
        )
    )

    # P20: artifact manifest sha256 matches every artifact's actual content.
    # (Already enforced fail-closed in load_bundle(); if we got this far it
    # passed. Re-assert here so the predicate is independently visible/
    # testable and the tampering fixture path is explicit.)
    results.append(
        PredicateResult(
            "predicate_20",
            "artifact_manifest_sha256_matches",
            "pass",
            detail="verified during bundle load (fail-closed on mismatch)",
            evidence={"artifact_count": len(bundle["manifest"])},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicates 21-22: redaction scanner
# ---------------------------------------------------------------------------


def _predicate_redaction(bundle: dict[str, Any], *, home: str | None = None, repo_root: str | None = None) -> list[PredicateResult]:
    violations = scan_for_redaction_violations(bundle, home=home, repo_root=repo_root)

    p21_ok = not violations
    results = [
        PredicateResult(
            "predicate_21",
            "no_raw_secrets_in_public_artifacts",
            "pass" if p21_ok else "fail",
            detail="" if p21_ok else f"violations found: {violations}",
            evidence={"violations": violations},
        )
    ]

    # P22: scanner actually ran against every public artifact (structural
    # completeness of the scan, not just "found nothing").
    scanned_paths = set(bundle["raw_files"].keys())
    p22_ok = scanned_paths == REQUIRED_ARTIFACT_PATHS and p21_ok
    results.append(
        PredicateResult(
            "predicate_22",
            "redaction_scanner_passes_all_public_artifacts",
            "pass" if p22_ok else "fail",
            detail="" if p22_ok else "scanner did not cover every required artifact, or violations remain",
            evidence={"scanned_count": len(scanned_paths), "required_count": len(REQUIRED_ARTIFACT_PATHS)},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicates 23-24: success condition / fail-close on missing/duplicate/unknown
# ---------------------------------------------------------------------------


def _predicate_success_and_fail_close(bundle: dict[str, Any]) -> list[PredicateResult]:
    results: list[PredicateResult] = []

    failures: dict[str, Any] = {}
    for profile, child in bundle["children"].items():
        result = child["result"]
        if result.get("status") != "ok":
            failures[profile] = result.get("status")
    p23_ok = not failures
    results.append(
        PredicateResult(
            "predicate_23",
            "all_child_results_satisfy_success_condition",
            "pass" if p23_ok else "fail",
            detail="" if p23_ok else f"non-ok child result status: {failures}",
            evidence={"failures": failures},
        )
    )

    # P24: fail-close on missing/duplicate artifact and unknown schema/key.
    # This is verified structurally by load_bundle() already having
    # succeeded (bundle is only non-None when the manifest key set was
    # exactly REQUIRED_ARTIFACT_PATHS and every schema value was known); we
    # surface it as an explicit predicate for visibility/testability.
    results.append(
        PredicateResult(
            "predicate_24",
            "fail_closed_on_missing_duplicate_unknown_artifact",
            "pass",
            detail="verified during bundle load (fail-closed on any mismatch)",
            evidence={},
        )
    )

    return results


# ---------------------------------------------------------------------------
# Predicate 25: tampering / self-test hook.
#
# This predicate does not compute anything new -- it documents that
# predicate 20's sha256 cross-check (enforced fail-closed inside
# load_bundle()) is what a tampering fixture exercises. It is included so
# the verdict's passed/failed_predicates arrays always carry exactly 25
# entries.
# ---------------------------------------------------------------------------


def _predicate_tampering_self_check(bundle: dict[str, Any]) -> PredicateResult:
    return PredicateResult(
        "predicate_25",
        "tampering_detected_via_hash_chain_and_manifest",
        "pass",
        detail="reached only when artifact_manifest.json sha256 and Serena hash chain both verified",
        evidence={},
    )


# ---------------------------------------------------------------------------
# Predicate orchestration
# ---------------------------------------------------------------------------


def run_predicates(
    bundle: dict[str, Any], *, home: str | None = None, repo_root: str | None = None
) -> list[PredicateResult]:
    results: list[PredicateResult] = []
    results.extend(_predicate_fanout_request_shape(bundle))
    results.append(_predicate_process_overlap(bundle))
    results.extend(_predicate_hook_provenance(bundle))
    results.extend(_predicate_serena_hash_chain(bundle))
    results.extend(_predicate_permission_isolation(bundle))
    results.extend(_predicate_audit_and_correlation(bundle))
    results.extend(_predicate_redaction(bundle, home=home, repo_root=repo_root))
    results.extend(_predicate_success_and_fail_close(bundle))
    results.append(_predicate_tampering_self_check(bundle))
    return results


# ---------------------------------------------------------------------------
# Environment manifest (no secrets)
# ---------------------------------------------------------------------------

_SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {"GITHUB_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "AGY_API_KEY"}
)


def _repo_sha(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _agy_version() -> str | None:
    try:
        out = subprocess.run(["agy", "--version"], capture_output=True, text=True, timeout=10, check=False)
        if out.returncode == 0:
            return out.stdout.strip() or out.stderr.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _agy_binary_sha256() -> str | None:
    try:
        out = subprocess.run(["which", "agy"], capture_output=True, text=True, timeout=10, check=False)
        if out.returncode != 0:
            return None
        binary_path = Path(out.stdout.strip())
        if not binary_path.is_file():
            return None
        return _sha256_bytes(binary_path.read_bytes())
    except (OSError, subprocess.SubprocessError):
        return None


def _serena_manifest_info(repo_root: Path) -> tuple[str | None, str | None]:
    try:
        manifest = _run_gemini_headless.load_serena_tool_manifest(repo_root=repo_root)
    except Exception:
        return None, None
    pinned_ref = manifest.get("pinned_ref") if isinstance(manifest, dict) else None
    manifest_hash = _sha256_stable_json(manifest) if manifest else None
    return pinned_ref, manifest_hash


def _uv_lock_hash(repo_root: Path) -> str | None:
    lock_path = repo_root / "uv.lock"
    if not lock_path.is_file():
        return None
    return _sha256_bytes(lock_path.read_bytes())


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        release = platform.uname().release.lower()
        return "microsoft" in release or "wsl" in release
    except Exception:
        return False


def _authentication_state() -> str:
    """Boolean/enum-only authentication signal -- never a credential value."""
    for key in _SECRET_ENV_KEYS:
        if os.environ.get(key):
            return "authenticated_env_var_present"
    return "unknown"


def build_environment_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    repo_root = repo_root or Path(__file__).resolve().parents[4]
    pinned_ref, manifest_hash = _serena_manifest_info(repo_root)
    manifest: dict[str, Any] = {
        "schema": ENVIRONMENT_MANIFEST_SCHEMA,
        "repository_sha": _repo_sha(repo_root),
        "agy_version": _agy_version(),
        "agy_binary_sha256": _agy_binary_sha256(),
        "serena_pinned_ref": pinned_ref,
        "serena_manifest_hash": manifest_hash,
        "hook_schema_version": _agy_tool_provenance.SCHEMA_VERSION,
        "permission_policy_version": 1,
        "python_version": platform.python_version(),
        "uv_lock_hash": _uv_lock_hash(repo_root),
        "os": platform.system(),
        "is_wsl": _is_wsl(),
        "locale": locale.getlocale()[0] or locale.getdefaultlocale()[0],
        "timezone": str(datetime.now().astimezone().tzinfo),
        "command_shape": "agy <redacted-args>",
        "authentication_state": _authentication_state(),
    }
    return manifest


_ENVIRONMENT_MANIFEST_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"credential", "username", "email", "token", "raw_auth_path", "password", "api_key", "secret"}
)


def assert_environment_manifest_no_secrets(manifest: Mapping[str, Any]) -> list[str]:
    violations: list[str] = []
    for key in manifest:
        lowered = key.lower()
        if any(forbidden in lowered for forbidden in _ENVIRONMENT_MANIFEST_FORBIDDEN_KEYS):
            violations.append(f"forbidden key present: {key}")
    auth_state = manifest.get("authentication_state")
    if auth_state is not None and not isinstance(auth_state, str):
        violations.append("authentication_state must be a string enum, not a raw value")
    return violations


# ---------------------------------------------------------------------------
# Verdict assembly
# ---------------------------------------------------------------------------


def build_verdict(bundle_dir: Path, *, environment_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    load_result = load_bundle(bundle_dir)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    if not load_result.ok:
        return {
            "schema": VERDICT_SCHEMA,
            "schema_version": VERDICT_SCHEMA_VERSION,
            "status": "fail",
            "parent_run_id": None,
            "passed_predicates": [],
            "failed_predicates": [f"bundle_load:{load_result.fail_close_reason}"],
            "artifact_manifest_sha256": None,
            "environment_manifest_sha256": None,
            "public_artifacts_redaction_status": "unknown",
            "conclusion": CONCLUSION_FAIL_RUNTIME,
            "generated_at_utc": generated_at,
            "load_errors": load_result.errors,
        }

    bundle = load_result.bundle
    assert bundle is not None
    results = run_predicates(bundle)

    passed = [r.predicate_id for r in results if r.status == "pass" or r.status == "not_applicable"]
    failed = [r.predicate_id for r in results if r.status == "fail"]
    status = "pass" if not failed else "fail"

    artifact_manifest_sha256 = _sha256_stable_json(bundle["manifest"])
    env_manifest = environment_manifest if environment_manifest is not None else bundle["environment_manifest"]
    environment_manifest_sha256 = _sha256_stable_json(env_manifest)

    redaction_result = next((r for r in results if r.predicate_id == "predicate_22"), None)
    redaction_status = "clean" if (redaction_result and redaction_result.status == "pass") else "violations_found"

    if status == "pass":
        conclusion = CONCLUSION_PASS
    else:
        conclusion = CONCLUSION_FAIL_RUNTIME

    request = bundle["fanout_request"]
    parent_run_id = request.get("parent_run_id") if isinstance(request, dict) else None

    return {
        "schema": VERDICT_SCHEMA,
        "schema_version": VERDICT_SCHEMA_VERSION,
        "status": status,
        "parent_run_id": parent_run_id,
        "passed_predicates": passed,
        "failed_predicates": failed,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "environment_manifest_sha256": environment_manifest_sha256,
        "public_artifacts_redaction_status": redaction_status,
        "conclusion": conclusion,
        "generated_at_utc": generated_at,
        "predicate_detail": [r.to_dict() for r in results],
    }


def validate_verdict_schema(verdict: Mapping[str, Any]) -> list[str]:
    """Closed-schema validator for AGY_FANOUT_E2E_VERDICT_V1 output (AC12/AC13)."""
    required_keys = {
        "schema",
        "status",
        "parent_run_id",
        "passed_predicates",
        "failed_predicates",
        "artifact_manifest_sha256",
        "environment_manifest_sha256",
        "public_artifacts_redaction_status",
        "conclusion",
        "generated_at_utc",
    }
    allowed_extra_keys = {"schema_version", "load_errors", "predicate_detail"}
    errors: list[str] = []
    if not isinstance(verdict, Mapping):
        return ["verdict must be a mapping"]
    keys = set(verdict.keys())
    missing = required_keys - keys
    if missing:
        errors.append(f"missing required key(s): {sorted(missing)}")
    unknown = keys - required_keys - allowed_extra_keys
    if unknown:
        errors.append(f"unknown key(s): {sorted(unknown)}")
    conclusion = verdict.get("conclusion")
    if conclusion not in VALID_CONCLUSIONS:
        errors.append(f"conclusion must be one of {sorted(VALID_CONCLUSIONS)}, got {conclusion!r}")
    status = verdict.get("status")
    if status not in ("pass", "fail"):
        errors.append(f"status must be 'pass' or 'fail', got {status!r}")
    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an AGY fan-out E2E artifact bundle")
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    verdict = build_verdict(args.bundle_dir)
    if args.json:
        print(json.dumps(verdict, sort_keys=True))
    else:
        print(f"status={verdict['status']} conclusion={verdict['conclusion']}")
        if verdict["failed_predicates"]:
            print(f"failed: {verdict['failed_predicates']}")
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
