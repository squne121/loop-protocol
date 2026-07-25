"""Tests for build_fanout_evidence_bundle.py (Issue #1748 AC6/AC7/AC8).

Builds a fixture that mimics ``fan_out_orchestrator.run_fanout()``'s raw
on-disk output (``manifest.json`` / ``events.ndjson`` /
``<subtask_id>.request.json``), plus the original
``delegation_fanout_request_v1`` request and optional supplementary
per-child evidence (audit log / hook events / permission events / Serena
evidence), then bundles it via ``build_fanout_evidence_bundle`` and asserts
the result against ``validate_agy_fanout_e2e_evidence.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
_BUNDLER_PATH = _SCRIPTS_DIR / "build_fanout_evidence_bundle.py"
_VALIDATOR_PATH = _SCRIPTS_DIR / "validate_agy_fanout_e2e_evidence.py"


def _load_module(path: Path, register_name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(register_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)
    return module


bundler = _load_module(_BUNDLER_PATH, "_test_build_fanout_evidence_bundle")
validator = _load_module(_VALIDATOR_PATH, "_test_build_fanout_evidence_bundle_validator")

_rgh = _load_module(_SCRIPTS_DIR / "run_gemini_headless.py", "_test_build_fanout_evidence_bundle_rgh")

PARENT_RUN_ID = "runfanoutparent0123456789abcdef"
PROFILES = ("local_asset_research", "grounded_research", "no_tools")
ATTEMPT_ID = "attempt-1"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _original_fanout_request() -> dict[str, Any]:
    return {
        "schema": "delegation_fanout_request_v1",
        "max_workers": 3,
        "provider_concurrency": {"agy": 3},
        "profile_concurrency": {p: 1 for p in PROFILES},
        "subtasks": [{"subtask_id": p, "profile": p, "provider": "agy"} for p in PROFILES],
    }


def _child_request(profile: str, *, requires_read_url_content: bool = False) -> dict[str, Any]:
    request: dict[str, Any] = {
        "parent_run_id": PARENT_RUN_ID,
        "subtask_id": profile,
        "attempt_id": ATTEMPT_ID,
        "profile": profile,
        "provider": "agy",
        "objective": f"objective for {profile}",
    }
    if requires_read_url_content:
        request["requires_read_url_content"] = True
    return request


def _serena_evidence_record() -> dict[str, Any]:
    evidence_records = [
        {
            "actor": "wrapper_serena_mcp",
            "parent_run_id": PARENT_RUN_ID,
            "subtask_id": "local_asset_research",
            "attempt_id": ATTEMPT_ID,
            "tool_name": "find_symbol",
            "args_sha256": _sha256_hex("docs/product/requirements.md"),
            "is_error": False,
            "repo_relative_path": "docs/product/requirements.md",
            "selector": "requirements",
            "line_range": [1, 20],
            "content_sha256": _sha256_hex("requirements content"),
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


def _child_result(profile: str, conversation_id: str, transcript_sha256: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": "delegation_result/v1",
        "ok": True,
        "parent_run_id": PARENT_RUN_ID,
        "subtask_id": profile,
        "attempt_id": ATTEMPT_ID,
    }
    if profile == "local_asset_research":
        base["local_asset_retrieval_metadata"] = {
            "actor": "wrapper_serena_mcp",
            "retrieval_actor": "wrapper_serena_mcp",
            "analysis_actor": "antigravity_cli",
        }
    if profile == "grounded_research":
        base["conversation_id"] = conversation_id
        base["transcript_sha256"] = transcript_sha256
        base["tool_calls"] = ["search_web", "read_url_content"]
    return base


def _hook_event(*, tool_name: str, conversation_id: str, transcript_sha256: str, step_idx: int) -> dict[str, Any]:
    return {
        "schema": "agy_tool_provenance_v1",
        "version": 1,
        "event": "PreToolUse",
        "toolCall": {"name": tool_name, "args_sha256": _sha256_hex(f"{tool_name}-args-{step_idx}")},
        "stepIdx": step_idx,
        "conversationId": conversation_id,
        "transcript_path_ref": "sha256:" + _sha256_hex("transcript-path-ref"),
        "transcript_sha256": transcript_sha256,
        "parent_run_id": PARENT_RUN_ID,
        "subtask_id": "grounded_research",
        "attempt_id": ATTEMPT_ID,
        "provider": "agy",
        "tool_profile": "grounded_research",
        "monotonic_ns": 1_000_000_000 + step_idx,
        "utc": "2026-07-25T00:00:00.000000Z",
    }


def _audit_pair(profile: str) -> list[dict[str, Any]]:
    start = {
        "schema": "delegation_audit_v1",
        "record_type": "start",
        "run_id": ATTEMPT_ID,
        "ts": "2026-07-25T00:00:00Z",
        "provider_requested": "agy",
        "tool_profile": profile,
        "parent_run_id": PARENT_RUN_ID,
        "subtask_id": profile,
        "attempt_id": ATTEMPT_ID,
    }
    end = {
        "schema": "delegation_audit_v1",
        "record_type": "end",
        "run_id": ATTEMPT_ID,
        "ts": "2026-07-25T00:00:05Z",
        "ok": True,
        "failure_class": None,
        "failure_reason": None,
        "actual_model": "agy-default",
        "tool_profile": profile,
        "parent_run_id": PARENT_RUN_ID,
        "subtask_id": profile,
        "attempt_id": ATTEMPT_ID,
    }
    return [start, end]


def _process_lifecycle_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    pids = {"local_asset_research": 2001, "grounded_research": 2002, "no_tools": 2003}
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": "local_asset_research",
            "subtask_id": "local_asset_research",
            "pid": pids["local_asset_research"],
            "started_monotonic_ns": 0,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": "grounded_research",
            "subtask_id": "grounded_research",
            "pid": pids["grounded_research"],
            "started_monotonic_ns": 50,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": "local_asset_research",
            "subtask_id": "local_asset_research",
            "pid": pids["local_asset_research"],
            "exited_monotonic_ns": 100,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": "grounded_research",
            "subtask_id": "grounded_research",
            "pid": pids["grounded_research"],
            "exited_monotonic_ns": 150,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_start",
            "artifact_stem": "no_tools",
            "subtask_id": "no_tools",
            "pid": pids["no_tools"],
            "started_monotonic_ns": 200,
        }
    )
    events.append(
        {
            "schema": "process_lifecycle_event_v1",
            "event": "process_exit",
            "artifact_stem": "no_tools",
            "subtask_id": "no_tools",
            "pid": pids["no_tools"],
            "exited_monotonic_ns": 250,
        }
    )
    # An untagged orchestration event (no "schema" key) -- the validator's
    # schema-closure check must tolerate this (Issue #1748 AC6).
    events.append({"event": "subtask_started", "subtask_id": "no_tools", "parent_run_id": PARENT_RUN_ID})
    return events


class _FanoutRunDirFixture:
    """Writes a fixture that mimics run_fanout()'s raw run_dir + surrounding
    inputs, and exposes the paths build_fanout_evidence_bundle.py's CLI/API
    expects.
    """

    def __init__(self, tmp_path: Path, *, full_evidence: bool = True):
        self.tmp_path = tmp_path
        self.conversation_id = "conv-grounded-0001"
        self.transcript_sha256 = _sha256_hex("transcript-content-grounded-research")

        self.original_request = _original_fanout_request()
        self.fanout_request_file = tmp_path / "original_fanout_request.json"
        self.fanout_request_file.write_text(json.dumps(self.original_request), encoding="utf-8")

        self.run_dir = tmp_path / "run_dir"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        results = []
        for profile in PROFILES:
            request = _child_request(profile, requires_read_url_content=(profile == "grounded_research"))
            (self.run_dir / f"{profile}.request.json").write_text(json.dumps(request), encoding="utf-8")
            result = _child_result(profile, self.conversation_id, self.transcript_sha256)
            results.append(
                {
                    "subtask_id": profile,
                    "original_ids": [profile],
                    "fanout_status": "succeeded",
                    "result": result,
                    "reasons": [],
                }
            )

        manifest = {
            "schema": "delegation_fanout_result_v1",
            "status": "success",
            "ok": True,
            "parent_run_id": PARENT_RUN_ID,
            "counts": {"requested": 3, "unique": 3, "succeeded": 3, "failed": 0, "cancelled": 0},
            "results": results,
            "failures": [],
            "deduplicated_aliases": {},
            "run_dir": str(self.run_dir),
            "manifest_path": str(self.run_dir / "manifest.json"),
        }
        (self.run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        events_lines = [json.dumps(e, sort_keys=True) for e in _process_lifecycle_events()]
        (self.run_dir / "events.ndjson").write_text("\n".join(events_lines) + "\n", encoding="utf-8")

        self.audit_log_file = tmp_path / "audit.jsonl"
        audit_records: list[dict[str, Any]] = []
        for profile in PROFILES:
            audit_records.extend(_audit_pair(profile))
        self.audit_log_file.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in audit_records) + "\n", encoding="utf-8"
        )

        self.hook_events_files: dict[str, Path] = {}
        self.permission_events_files: dict[str, Path] = {}
        self.serena_evidence_files: dict[str, Path] = {}

        if full_evidence:
            hook_events_path = tmp_path / "grounded_research.hook_events.jsonl"
            hook_events = [
                _hook_event(
                    tool_name="search_web",
                    conversation_id=self.conversation_id,
                    transcript_sha256=self.transcript_sha256,
                    step_idx=0,
                ),
                _hook_event(
                    tool_name="read_url_content",
                    conversation_id=self.conversation_id,
                    transcript_sha256=self.transcript_sha256,
                    step_idx=1,
                ),
            ]
            hook_events_path.write_text(
                "\n".join(json.dumps(e, sort_keys=True) for e in hook_events) + "\n", encoding="utf-8"
            )
            self.hook_events_files["grounded_research"] = hook_events_path

            for profile, events in (
                ("local_asset_research", [{"tool_name": "read_file", "source": "wrapper_serena_mcp", "executed": True}]),
                (
                    "grounded_research",
                    [
                        {"tool_name": "search_web", "source": "agy_direct", "executed": True},
                        {"tool_name": "read_url_content", "source": "agy_direct", "executed": True},
                    ],
                ),
                ("no_tools", [{"tool_name": "search_web", "source": "agy_direct", "executed": False}]),
            ):
                path = tmp_path / f"{profile}.permission_events.json"
                path.write_text(json.dumps(events), encoding="utf-8")
                self.permission_events_files[profile] = path

            serena_path = tmp_path / "local_asset_research.serena_evidence.json"
            serena_path.write_text(json.dumps([_serena_evidence_record()]), encoding="utf-8")
            self.serena_evidence_files["local_asset_research"] = serena_path

    def build(self, out_dir: Path) -> Path:
        return bundler.build_and_materialize_from_run_dir(
            original_fanout_request_file=self.fanout_request_file,
            run_dir=self.run_dir,
            out_dir=out_dir,
            audit_log_file=self.audit_log_file,
            hook_events_files=self.hook_events_files,
            permission_events_files=self.permission_events_files,
            serena_evidence_files=self.serena_evidence_files,
        )


# ---------------------------------------------------------------------------
# AC6: positive round-trip -- run_fanout()-shaped raw output -> bundle-dir
# ---------------------------------------------------------------------------


def test_bundle_matches_required_artifact_paths_and_loads_ok(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    bundle_dir = fixture.build(tmp_path / "bundle")

    manifest = json.loads((bundle_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest.keys()) == validator.REQUIRED_ARTIFACT_PATHS

    load_result = validator.load_bundle(bundle_dir)
    assert load_result.ok is True, load_result.errors


def test_bundle_fanout_request_evidence_is_stamped_with_parent_run_id(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    bundle_dir = fixture.build(tmp_path / "bundle")
    fanout_request = json.loads((bundle_dir / "fanout_request.json").read_text(encoding="utf-8"))
    assert fanout_request["schema"] == bundler.FANOUT_REQUEST_EVIDENCE_SCHEMA
    assert fanout_request["parent_run_id"] == PARENT_RUN_ID
    for subtask in fanout_request["subtasks"]:
        assert subtask["parent_run_id"] == PARENT_RUN_ID


# ---------------------------------------------------------------------------
# AC7: bundler fail-closes on missing artifact / malformed json
# ---------------------------------------------------------------------------


def test_missing_manifest_json_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    (fixture.run_dir / "manifest.json").unlink()
    with pytest.raises(bundler.BundleBuildError, match="manifest.json"):
        fixture.build(tmp_path / "bundle")


def test_malformed_manifest_json_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    (fixture.run_dir / "manifest.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(bundler.BundleBuildError, match="malformed JSON"):
        fixture.build(tmp_path / "bundle")


def test_missing_child_request_file_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    (fixture.run_dir / "grounded_research.request.json").unlink()
    with pytest.raises(bundler.BundleBuildError, match="no <artifact_stem>.request.json"):
        fixture.build(tmp_path / "bundle")


def test_missing_required_profile_in_manifest_results_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    manifest_path = fixture.run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["results"] = [r for r in manifest["results"] if r["subtask_id"] != "no_tools"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(bundler.BundleBuildError, match="missing required profile"):
        fixture.build(tmp_path / "bundle")


def test_malformed_evidence_file_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    fixture.serena_evidence_files["local_asset_research"].write_text("not json", encoding="utf-8")
    with pytest.raises(bundler.BundleBuildError, match="malformed JSON"):
        fixture.build(tmp_path / "bundle")


def test_out_dir_already_populated_fails_closed(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    out_dir = tmp_path / "bundle"
    out_dir.mkdir()
    (out_dir / "stray_file.txt").write_text("pre-existing", encoding="utf-8")
    with pytest.raises(bundler.BundleBuildError, match="already exists and is non-empty"):
        fixture.build(out_dir)


def test_cli_missing_run_dir_exits_nonzero(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path)
    exit_code = bundler.main(
        [
            "--fanout-request-file",
            str(fixture.fanout_request_file),
            "--run-dir",
            str(tmp_path / "does-not-exist"),
            "--out-dir",
            str(tmp_path / "bundle"),
        ]
    )
    assert exit_code != 0


# ---------------------------------------------------------------------------
# AC8: end-to-end -- bundled output fed into the validator CLI entrypoint
# ---------------------------------------------------------------------------


def test_bundle_end_to_end_validator_pass(tmp_path):
    fixture = _FanoutRunDirFixture(tmp_path, full_evidence=True)
    bundle_dir = fixture.build(tmp_path / "bundle")

    verdict = validator.build_verdict(bundle_dir)
    assert verdict["conclusion"] == "PASS", verdict.get("failed_predicates")
    assert verdict["status"] == "pass"
    assert verdict["failed_predicates"] == []
    assert validator.validate_verdict_schema(verdict) == []


def test_bundle_end_to_end_missing_local_instrumentation_fails_predicates_not_bundler(tmp_path):
    """Absent optional local-instrumentation evidence (hook/permission/serena)
    must not stop the bundler from producing a structurally valid bundle --
    it is a fail-closed *predicate* outcome, not a bundler error."""
    fixture = _FanoutRunDirFixture(tmp_path, full_evidence=False)
    bundle_dir = fixture.build(tmp_path / "bundle")

    load_result = validator.load_bundle(bundle_dir)
    assert load_result.ok is True, load_result.errors

    verdict = validator.build_verdict(bundle_dir)
    assert verdict["status"] == "fail"
    assert verdict["conclusion"] == "FAIL_RUNTIME"
    assert "predicate_07" in verdict["failed_predicates"]
