"""Fixture builder for the AGY fan-out E2E artifact bundle (Issue #1710).

Not a test module itself (excluded from collection by
``tests/conftest.py``'s ``collect_ignore_glob = ["fixtures/**/*.py"]``).
``test_agy_fanout_e2e_validator.py`` imports ``build_positive_bundle_content()``
and ``materialize_bundle()`` from here to build the positive fixture, then
derives negative / tampering fixtures by mutating a deep copy of the positive
content before materializing.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_sibling_module(filename: str, register_name: str) -> types.ModuleType:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(register_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)
    return module


_rgh = _load_sibling_module("run_gemini_headless.py", "_agy_fanout_e2e_fixture_run_gemini_headless")

PARENT_RUN_ID = "parentrun0123456789abcdef01234567"
PROFILES = ("local_asset_research", "grounded_research", "no_tools")

_ATTEMPT_ID = "attempt-1"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_serena_evidence_record(*, parent_run_id: str, subtask_id: str, attempt_id: str) -> dict[str, Any]:
    evidence_records = [
        {
            "actor": "wrapper_serena_mcp",
            "parent_run_id": parent_run_id,
            "subtask_id": subtask_id,
            "attempt_id": attempt_id,
            "tool_name": "find_symbol",
            "args_sha256": _sha256_hex("docs/product/requirements.md"),
            "is_error": False,
            "repo_relative_path": "docs/product/requirements.md",
            "selector": "requirements",
            "line_range": [1, 20],
            "content_sha256": _sha256_hex("requirements content for local_asset_research objective"),
            "source_kind": "serena_mcp_evidence",
            "serena_pinned_ref": "v1.2.3",
            "serena_manifest_id": "manifest-abc123",
        }
    ]
    evidence_sha256 = _rgh._hash_evidence(evidence_records)
    objective_sha256 = _sha256_hex("summarize local asset research objective")
    target_contract_sha256 = _sha256_hex("target-contract-v1")
    tool_profile = "local_asset_research"
    prompt_envelope_sha256 = _rgh._hash_prompt_envelope(
        evidence_sha256, objective_sha256, target_contract_sha256, tool_profile
    )
    result_binding_sha256 = _rgh._hash_result_binding(evidence_sha256, prompt_envelope_sha256)

    record = dict(evidence_records[0])
    record.update(
        {
            "evidence_sha256": evidence_sha256,
            "objective_sha256": objective_sha256,
            "target_contract_sha256": target_contract_sha256,
            "tool_profile": tool_profile,
            "prompt_envelope_sha256": prompt_envelope_sha256,
            "result_binding_sha256": result_binding_sha256,
        }
    )
    return record


def _make_hook_event(
    *,
    tool_name: str,
    parent_run_id: str,
    subtask_id: str,
    attempt_id: str,
    conversation_id: str,
    transcript_sha256: str,
    step_idx: int,
) -> dict[str, Any]:
    return {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {"name": tool_name, "args_sha256": _sha256_hex(f"{tool_name}-args-{step_idx}")},
        "stepIdx": step_idx,
        "conversationId": conversation_id,
        "transcript_path_ref": "sha256:" + _sha256_hex("transcript-path-ref"),
        "transcript_sha256": transcript_sha256,
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
        "provider": "agy",
        "tool_profile": "grounded_research",
        "monotonic_ns": 1_000_000_000 + step_idx,
        "utc": "2026-07-25T00:00:00.000000Z",
    }


def _audit_records(
    *,
    run_id: str,
    provider_requested: str,
    tool_profile: str,
    parent_run_id: str,
    subtask_id: str,
    attempt_id: str,
) -> list[dict[str, Any]]:
    start = {
        "schema": "delegation_audit_v1",
        "record_type": "start",
        "run_id": run_id,
        "ts": "2026-07-25T00:00:00Z",
        "provider_requested": provider_requested,
        "tool_profile": tool_profile,
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
    }
    end = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": run_id,
        "ts": "2026-07-25T00:00:05Z",
        "ok": True,
        "failure_class": None,
        "failure_reason": None,
        "actual_model": "agy-default",
        "tool_profile": tool_profile,
        "parent_run_id": parent_run_id,
        "subtask_id": subtask_id,
        "attempt_id": attempt_id,
    }
    return [start, end]


def _process_lifecycle_events(subtask_pids: dict[str, int]) -> list[dict[str, Any]]:
    """Two subtasks overlap in monotonic time; the third starts after both exit."""
    events: list[dict[str, Any]] = []
    order = list(subtask_pids.items())
    # local_asset_research and grounded_research overlap [0, 100) / [50, 150)
    (sid_a, pid_a), (sid_b, pid_b), (sid_c, pid_c) = order
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": f"{sid_a}-stem",
            "subtask_id": sid_a,
            "pid": pid_a,
            "started_monotonic_ns": 0,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": f"{sid_b}-stem",
            "subtask_id": sid_b,
            "pid": pid_b,
            "started_monotonic_ns": 50,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": f"{sid_a}-stem",
            "subtask_id": sid_a,
            "pid": pid_a,
            "exited_monotonic_ns": 100,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": f"{sid_b}-stem",
            "subtask_id": sid_b,
            "pid": pid_b,
            "exited_monotonic_ns": 150,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": f"{sid_c}-stem",
            "subtask_id": sid_c,
            "pid": pid_c,
            "started_monotonic_ns": 200,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": f"{sid_c}-stem",
            "subtask_id": sid_c,
            "pid": pid_c,
            "exited_monotonic_ns": 250,
        }
    )
    return events


def build_positive_bundle_content() -> dict[str, Any]:
    """Build the *logical* (pre-materialization) content dict for a fully
    passing artifact bundle. Callers deep-copy this and mutate specific
    fields to build negative / tampering fixtures.
    """
    conversation_id = "conv-grounded-0001"
    transcript_sha256 = _sha256_hex("transcript-content-grounded-research")

    fanout_request = {
        "schema": "agy_fanout_e2e_request_evidence_v1",
        "parent_run_id": PARENT_RUN_ID,
        "max_workers": 3,
        "provider_concurrency": {"agy": 3},
        "profile_concurrency": {p: 1 for p in PROFILES},
        "subtasks": [
            {"subtask_id": p, "profile": p, "provider": "agy", "parent_run_id": PARENT_RUN_ID} for p in PROFILES
        ],
    }

    children: dict[str, Any] = {}

    # local_asset_research
    serena_record = _make_serena_evidence_record(
        parent_run_id=PARENT_RUN_ID, subtask_id="local_asset_research", attempt_id=_ATTEMPT_ID
    )
    children["local_asset_research"] = {
        "request": {
            "schema": "delegation_request_v1",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "local_asset_research",
            "attempt_id": _ATTEMPT_ID,
            "profile": "local_asset_research",
            "provider": "agy",
            "objective": "summarize local asset research objective",
        },
        "result": {
            "schema": "delegation_result_v1",
            "status": "ok",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "local_asset_research",
            "attempt_id": _ATTEMPT_ID,
            "actor": "antigravity_cli",
        },
        "audit": _audit_records(
            run_id=_ATTEMPT_ID,
            provider_requested="agy",
            tool_profile="local_asset_research",
            parent_run_id=PARENT_RUN_ID,
            subtask_id="local_asset_research",
            attempt_id=_ATTEMPT_ID,
        ),
        "permission_events": [
            {"tool_name": "read_file", "source": "wrapper_serena_mcp", "executed": True},
        ],
        "serena_evidence": [serena_record],
    }

    # grounded_research
    hook_events = [
        _make_hook_event(
            tool_name="search_web",
            parent_run_id=PARENT_RUN_ID,
            subtask_id="grounded_research",
            attempt_id=_ATTEMPT_ID,
            conversation_id=conversation_id,
            transcript_sha256=transcript_sha256,
            step_idx=0,
        ),
        _make_hook_event(
            tool_name="read_url_content",
            parent_run_id=PARENT_RUN_ID,
            subtask_id="grounded_research",
            attempt_id=_ATTEMPT_ID,
            conversation_id=conversation_id,
            transcript_sha256=transcript_sha256,
            step_idx=1,
        ),
    ]
    children["grounded_research"] = {
        "request": {
            "schema": "delegation_request_v1",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "grounded_research",
            "attempt_id": _ATTEMPT_ID,
            "profile": "grounded_research",
            "provider": "agy",
            "objective": "research latest AGY fan-out evidence online",
            "requires_read_url_content": True,
        },
        "result": {
            "schema": "delegation_result_v1",
            "status": "ok",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "grounded_research",
            "attempt_id": _ATTEMPT_ID,
            "conversation_id": conversation_id,
            "transcript_sha256": transcript_sha256,
            "tool_calls": ["search_web", "read_url_content"],
        },
        "audit": _audit_records(
            run_id=_ATTEMPT_ID,
            provider_requested="agy",
            tool_profile="grounded_research",
            parent_run_id=PARENT_RUN_ID,
            subtask_id="grounded_research",
            attempt_id=_ATTEMPT_ID,
        ),
        "permission_events": [
            {"tool_name": "search_web", "source": "agy_direct", "executed": True},
            {"tool_name": "read_url_content", "source": "agy_direct", "executed": True},
        ],
        "hook_events": hook_events,
    }

    # no_tools
    children["no_tools"] = {
        "request": {
            "schema": "delegation_request_v1",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "no_tools",
            "attempt_id": _ATTEMPT_ID,
            "profile": "no_tools",
            "provider": "agy",
            "objective": "answer from model knowledge only, no tools",
        },
        "result": {
            "schema": "delegation_result_v1",
            "status": "ok",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "no_tools",
            "attempt_id": _ATTEMPT_ID,
            "actor": "antigravity_cli",
        },
        "audit": _audit_records(
            run_id=_ATTEMPT_ID,
            provider_requested="agy",
            tool_profile="no_tools",
            parent_run_id=PARENT_RUN_ID,
            subtask_id="no_tools",
            attempt_id=_ATTEMPT_ID,
        ),
        "permission_events": [
            {"tool_name": "search_web", "source": "agy_direct", "executed": False},
        ],
    }

    process_lifecycle_events = _process_lifecycle_events(
        {"local_asset_research": 1001, "grounded_research": 1002, "no_tools": 1003}
    )

    environment_manifest = {
        "schema": "agy_fanout_e2e_environment_manifest_v1",
        "repository_sha": "a" * 40,
        "agy_version": "1.1.5",
        "agy_binary_sha256": "b" * 64,
        "serena_pinned_ref": "v1.2.3",
        "serena_manifest_hash": "c" * 64,
        "hook_schema_version": 1,
        "permission_policy_version": 1,
        "python_version": "3.12.3",
        "uv_lock_hash": "d" * 64,
        "os": "Linux",
        "is_wsl": True,
        "locale": "en_US.UTF-8",
        "timezone": "UTC",
        "command_shape": "agy <redacted-args>",
        "authentication_state": "authenticated_env_var_present",
    }

    return {
        "fanout_request": fanout_request,
        "children": children,
        "process_lifecycle_events": process_lifecycle_events,
        "environment_manifest": environment_manifest,
    }


def _relative_paths() -> list[str]:
    paths = ["fanout_request.json", "process_lifecycle_events.jsonl", "environment_manifest.json"]
    for profile in PROFILES:
        paths.append(f"children/{profile}/request.json")
        paths.append(f"children/{profile}/result.json")
        paths.append(f"children/{profile}/audit.jsonl")
        paths.append(f"children/{profile}/permission_events.json")
    paths.append("children/local_asset_research/serena_evidence.json")
    paths.append("children/grounded_research/hook_events.jsonl")
    return paths


def _content_for_path(content: dict[str, Any], rel_path: str) -> Any:
    if rel_path == "fanout_request.json":
        return content["fanout_request"]
    if rel_path == "process_lifecycle_events.jsonl":
        return content["process_lifecycle_events"]
    if rel_path == "environment_manifest.json":
        return content["environment_manifest"]
    parts = rel_path.split("/")
    profile = parts[1]
    name = parts[2]
    key = name.rsplit(".", 1)[0]
    return content["children"][profile][key]


def _serialize(rel_path: str, value: Any) -> bytes:
    if rel_path.endswith(".jsonl"):
        lines = [json.dumps(item, sort_keys=True) for item in value]
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return json.dumps(value, sort_keys=True, indent=2).encode("utf-8")


def materialize_bundle(bundle_dir: Path, content: dict[str, Any], *, corrupt_manifest_path: str | None = None) -> Path:
    """Write *content* to *bundle_dir* as a full artifact bundle, including a
    correct ``artifact_manifest.json`` -- unless ``corrupt_manifest_path`` is
    given, in which case that one manifest entry's recorded sha256 is
    deliberately wrong (tampering fixture: the file on disk is NOT changed to
    match, simulating post-hoc tampering with either the file or the
    manifest).
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for rel_path in _relative_paths():
        value = _content_for_path(content, rel_path)
        raw = _serialize(rel_path, value)
        file_path = bundle_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(raw)
        manifest[rel_path] = _sha256_bytes(raw)

    if corrupt_manifest_path is not None:
        manifest[corrupt_manifest_path] = "0" * 64

    (bundle_dir / "artifact_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )
    return bundle_dir


def build_and_materialize(bundle_dir: Path, mutate=None, **materialize_kwargs) -> Path:
    """Convenience: build positive content, optionally mutate a deep copy in
    place via ``mutate(content) -> None``, then materialize to *bundle_dir*.
    """
    content = copy.deepcopy(build_positive_bundle_content())
    if mutate is not None:
        mutate(content)
    return materialize_bundle(bundle_dir, content, **materialize_kwargs)
