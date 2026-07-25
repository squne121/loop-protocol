#!/usr/bin/env python3
"""Deterministic bundler: run_fanout() raw output -> validator bundle-dir (Issue #1748).

``fan_out_orchestrator.run_fanout()`` writes its raw artifacts to a ``run_dir``:

  - ``manifest.json``                    -- the ``delegation_fanout_result_v1``
    overall result (also its return value), containing ``results[]`` --
    one entry per subtask with ``subtask_id`` and the child's raw
    ``delegation_result/v1`` ``result`` dict.
  - ``events.ndjson``                    -- process-lifecycle + orchestration
    journal (mix of ``process_lifecycle_event_v1`` records and untagged
    ``subtask_started`` / ``subtask_finished`` / ``overall_timeout_reached``
    records).
  - ``<artifact_stem>.request.json``     -- the stamped per-child request
    written by ``make_subprocess_runner()`` before spawning each child.
  - ``<artifact_stem>.result.json``      -- the raw per-child output file
    (byte-identical to ``manifest["results"][i]["result"]`` when present).

``validate_agy_fanout_e2e_evidence.py`` instead consumes a fixed-layout
*artifact bundle* directory (Issue #1710's ``REQUIRED_ARTIFACT_PATHS``):
``fanout_request.json`` / ``process_lifecycle_events.jsonl`` /
``environment_manifest.json`` / ``children/<profile>/{request.json,
result.json,audit.jsonl,permission_events.json}`` + profile-specific extra
artifacts (``serena_evidence.json`` for ``local_asset_research``,
``hook_events.jsonl`` for ``grounded_research``) + a closed-manifest
``artifact_manifest.json`` (``{relative_path: sha256}``).

This module performs that transformation, deterministically:

  - ``fanout_request.json`` is built by stamping the *original*
    ``delegation_fanout_request_v1`` request (the one passed into
    ``run_fanout()``, not present in ``manifest.json`` itself) with the
    ``parent_run_id`` that ``run_fanout()`` generated, and re-labelling its
    schema as the validator's own request-evidence schema
    (``agy_fanout_e2e_request_evidence_v1``) -- this is a read-only
    relabelling, not a new schema decision (Issue #1710 already defined
    that schema; this bundler is the first thing that actually populates
    it from real ``run_fanout()`` input/output).
  - ``process_lifecycle_events.jsonl`` is a byte-for-byte copy of
    ``events.ndjson`` (the validator's own schema-closure check ignores any
    record without a ``"schema"`` key, so the untagged orchestration events
    interleaved in the journal are harmless passengers).
  - ``environment_manifest.json`` is produced by re-using
    ``validate_agy_fanout_e2e_evidence.build_environment_manifest()``
    (single producer -- this module does not duplicate that logic).
  - Per-child ``request.json`` / ``result.json`` are read straight from the
    ``run_dir`` artifacts described above (matched to each
    ``manifest.json`` result entry by ``subtask_id``).
  - Per-child ``audit.jsonl`` is the subset of an (optional) supplied
    ``delegation_audit_v1`` audit log whose records carry that child's
    ``subtask_id``.
  - Per-child ``permission_events.json`` / ``hook_events.jsonl`` /
    ``serena_evidence.json`` are *optional* local-instrumentation evidence
    this module does not itself capture (that capture lives in #1705-#1708,
    out of this Issue's scope). When the caller does not supply one of
    these for a profile that requires it, this bundler materializes a
    schema-valid *empty* artifact (``[]`` / no lines) rather than failing --
    an empty/absent local-instrumentation artifact is a fail-closed
    *predicate* result (the validator's predicates 7-17 correctly fail, and
    ``AGY_FANOUT_E2E_VERDICT_V1`` is designed to surface that as
    ``BLOCKED_LOCAL_INSTRUMENTATION`` territory), not a bundler error.

This module does not implement any AGY/Serena/hook capture logic itself; it
only lays out already-produced artifacts into the validator's fixed bundle
shape and computes the closing ``artifact_manifest.json``. Fail-closed on any
missing/malformed required input (original fanout request, ``run_dir``,
``manifest.json``, or a `result`/`request` pairing for any subtask).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import types
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent

PROFILE_LOCAL_ASSET_RESEARCH = "local_asset_research"
PROFILE_GROUNDED_RESEARCH = "grounded_research"
PROFILE_NO_TOOLS = "no_tools"
REQUIRED_PROFILES: tuple[str, ...] = (PROFILE_LOCAL_ASSET_RESEARCH, PROFILE_GROUNDED_RESEARCH, PROFILE_NO_TOOLS)

FANOUT_REQUEST_EVIDENCE_SCHEMA = "agy_fanout_e2e_request_evidence_v1"


class BundleBuildError(Exception):
    """Raised (fail-closed) when the bundler cannot deterministically produce
    a bundle-dir from the supplied inputs."""


def _load_sibling_module(filename: str, register_name: str) -> types.ModuleType:
    import importlib.util

    path = _SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(register_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive
        raise ImportError(f"cannot load spec for {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[register_name] = module
    spec.loader.exec_module(module)
    return module


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BundleBuildError(f"required input file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BundleBuildError(f"malformed JSON in {path}: {exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BundleBuildError(f"required input file missing: {path}") from exc
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleBuildError(f"malformed JSONL at {path}:{line_no}: {exc}") from exc
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_child_request_file(run_dir: Path, subtask_id: str) -> Path:
    """Locate ``<artifact_stem>.request.json`` for *subtask_id* in *run_dir*.

    ``artifact_stem`` usually equals ``subtask_id`` (the common case for a
    fresh fan-out request with no id collisions), so the direct-name lookup
    is tried first. If that file is absent (a non-trivial artifact_stem was
    used), every ``*.request.json`` in *run_dir* is opened and matched by its
    own ``subtask_id`` field -- deterministic and fail-closed (no match, or
    more than one match, is an error).
    """
    direct = run_dir / f"{subtask_id}.request.json"
    if direct.is_file():
        return direct
    candidates: list[Path] = []
    for path in sorted(run_dir.glob("*.request.json")):
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(content, dict) and content.get("subtask_id") == subtask_id:
            candidates.append(path)
    if not candidates:
        raise BundleBuildError(f"no <artifact_stem>.request.json in {run_dir} matches subtask_id={subtask_id!r}")
    if len(candidates) > 1:
        raise BundleBuildError(
            f"ambiguous request.json match for subtask_id={subtask_id!r} in {run_dir}: {candidates}"
        )
    return candidates[0]


def build_fanout_request_evidence(original_request: dict[str, Any], parent_run_id: str) -> dict[str, Any]:
    """Stamp *original_request* (the ``delegation_fanout_request_v1`` sent
    into ``run_fanout()``) with the ``parent_run_id`` ``run_fanout()``
    generated, and relabel it as the validator's request-evidence schema.
    """
    subtasks_in = original_request.get("subtasks")
    if not isinstance(subtasks_in, list):
        raise BundleBuildError("original fanout request is missing a 'subtasks' list")
    stamped_subtasks = []
    for subtask in subtasks_in:
        if not isinstance(subtask, dict):
            raise BundleBuildError(f"fanout request subtask is not an object: {subtask!r}")
        stamped_subtasks.append({**subtask, "parent_run_id": parent_run_id})
    return {
        "schema": FANOUT_REQUEST_EVIDENCE_SCHEMA,
        "parent_run_id": parent_run_id,
        "max_workers": original_request.get("max_workers"),
        "provider_concurrency": original_request.get("provider_concurrency"),
        "profile_concurrency": original_request.get("profile_concurrency"),
        "subtasks": stamped_subtasks,
    }


def _child_audit_records(audit_records: list[dict[str, Any]], subtask_id: str) -> list[dict[str, Any]]:
    return [r for r in audit_records if isinstance(r, dict) and r.get("subtask_id") == subtask_id]


def build_bundle_content(
    *,
    original_fanout_request: dict[str, Any],
    run_dir: Path,
    audit_records: list[dict[str, Any]] | None = None,
    hook_events_by_profile: dict[str, list[dict[str, Any]]] | None = None,
    permission_events_by_profile: dict[str, list[dict[str, Any]]] | None = None,
    serena_evidence_by_profile: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the *logical* (pre-materialization) bundle content dict from raw
    ``run_fanout()`` output plus optional supplementary evidence. Mirrors the
    shape ``tests/fixtures/agy_fanout_e2e/build_bundle.py`` uses for its
    positive fixture (``fanout_request`` / ``children`` / ``process_lifecycle_events``
    / ``environment_manifest``), so both can be materialized the same way.
    """
    audit_records = audit_records or []
    hook_events_by_profile = hook_events_by_profile or {}
    permission_events_by_profile = permission_events_by_profile or {}
    serena_evidence_by_profile = serena_evidence_by_profile or {}

    manifest_path = run_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise BundleBuildError(f"{manifest_path} must contain a JSON object")

    parent_run_id = manifest.get("parent_run_id")
    if not isinstance(parent_run_id, str) or not parent_run_id.strip():
        raise BundleBuildError(f"{manifest_path} is missing a non-empty 'parent_run_id'")

    results = manifest.get("results")
    if not isinstance(results, list):
        raise BundleBuildError(f"{manifest_path} is missing a 'results' list")

    results_by_subtask: dict[str, Any] = {}
    for entry in results:
        if not isinstance(entry, dict):
            raise BundleBuildError(f"{manifest_path} 'results' contains a non-object entry: {entry!r}")
        subtask_id = entry.get("subtask_id")
        if not isinstance(subtask_id, str) or not subtask_id:
            raise BundleBuildError(f"{manifest_path} 'results' entry is missing subtask_id: {entry!r}")
        results_by_subtask[subtask_id] = entry

    missing_profiles = [p for p in REQUIRED_PROFILES if p not in results_by_subtask]
    if missing_profiles:
        raise BundleBuildError(f"{manifest_path} 'results' is missing required profile(s): {missing_profiles}")

    events_path = run_dir / "events.ndjson"
    process_lifecycle_events = _read_jsonl(events_path)

    children: dict[str, Any] = {}
    for profile in REQUIRED_PROFILES:
        entry = results_by_subtask[profile]
        child_result = entry.get("result")
        if not isinstance(child_result, dict):
            raise BundleBuildError(f"subtask_id={profile!r} has no child result object in {manifest_path}")

        request_path = _find_child_request_file(run_dir, profile)
        child_request = _read_json(request_path)
        if not isinstance(child_request, dict):
            raise BundleBuildError(f"{request_path} must contain a JSON object")
        child_request = {"schema": "delegation_request_v1", **child_request}

        child: dict[str, Any] = {
            "request": child_request,
            "result": child_result,
            "audit": _child_audit_records(audit_records, profile),
            "permission_events": list(permission_events_by_profile.get(profile, [])),
        }
        if profile == PROFILE_LOCAL_ASSET_RESEARCH:
            child["serena_evidence"] = list(serena_evidence_by_profile.get(profile, []))
        if profile == PROFILE_GROUNDED_RESEARCH:
            child["hook_events"] = list(hook_events_by_profile.get(profile, []))
        children[profile] = child

    fanout_request = build_fanout_request_evidence(original_fanout_request, parent_run_id)

    validator = _load_sibling_module("validate_agy_fanout_e2e_evidence.py", "_build_fanout_evidence_bundle_validator")
    environment_manifest = validator.build_environment_manifest()

    return {
        "fanout_request": fanout_request,
        "children": children,
        "process_lifecycle_events": process_lifecycle_events,
        "environment_manifest": environment_manifest,
    }


def _relative_paths() -> list[str]:
    paths = ["fanout_request.json", "process_lifecycle_events.jsonl", "environment_manifest.json"]
    for profile in REQUIRED_PROFILES:
        paths.append(f"children/{profile}/request.json")
        paths.append(f"children/{profile}/result.json")
        paths.append(f"children/{profile}/audit.jsonl")
        paths.append(f"children/{profile}/permission_events.json")
    paths.append(f"children/{PROFILE_LOCAL_ASSET_RESEARCH}/serena_evidence.json")
    paths.append(f"children/{PROFILE_GROUNDED_RESEARCH}/hook_events.jsonl")
    return paths


def _content_for_path(content: dict[str, Any], rel_path: str) -> Any:
    if rel_path == "fanout_request.json":
        return content["fanout_request"]
    if rel_path == "process_lifecycle_events.jsonl":
        return content["process_lifecycle_events"]
    if rel_path == "environment_manifest.json":
        return content["environment_manifest"]
    parts = rel_path.split("/")
    profile, name = parts[1], parts[2]
    key = name.rsplit(".", 1)[0]
    return content["children"][profile].get(key, [] if name.endswith((".json", ".jsonl")) else None)


def _serialize(rel_path: str, value: Any) -> bytes:
    if rel_path.endswith(".jsonl"):
        items = value or []
        lines = [json.dumps(item, sort_keys=True) for item in items]
        return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return json.dumps(value if value is not None else [], sort_keys=True, indent=2).encode("utf-8")


def materialize_bundle(bundle_dir: Path, content: dict[str, Any]) -> Path:
    """Write *content* to *bundle_dir* as a full, closed artifact bundle
    (every ``REQUIRED_ARTIFACT_PATHS`` entry present, plus a correct
    ``artifact_manifest.json``). Fail-closed if *bundle_dir* already exists
    and is non-empty (never silently overwrite/merge a prior bundle).
    """
    if bundle_dir.exists() and any(bundle_dir.iterdir()):
        raise BundleBuildError(f"bundle_dir already exists and is non-empty: {bundle_dir}")
    bundle_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    for rel_path in _relative_paths():
        value = _content_for_path(content, rel_path)
        raw = _serialize(rel_path, value)
        file_path = bundle_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(raw)
        manifest[rel_path] = _sha256_bytes(raw)

    (bundle_dir / "artifact_manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
    )
    return bundle_dir


def build_and_materialize_from_run_dir(
    *,
    original_fanout_request_file: Path,
    run_dir: Path,
    out_dir: Path,
    audit_log_file: Path | None = None,
    hook_events_files: dict[str, Path] | None = None,
    permission_events_files: dict[str, Path] | None = None,
    serena_evidence_files: dict[str, Path] | None = None,
) -> Path:
    original_fanout_request = _read_json(original_fanout_request_file)
    if not isinstance(original_fanout_request, dict):
        raise BundleBuildError(f"{original_fanout_request_file} must contain a JSON object")
    if not run_dir.is_dir():
        raise BundleBuildError(f"--run-dir does not exist or is not a directory: {run_dir}")

    audit_records = _read_jsonl(audit_log_file) if audit_log_file is not None else []

    def _load_evidence_map(files: dict[str, Path] | None, *, jsonl: bool) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for profile, path in (files or {}).items():
            out[profile] = _read_jsonl(path) if jsonl else _as_list(_read_json(path), path)
        return out

    def _as_list(value: Any, path: Path) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            raise BundleBuildError(f"{path} must contain a JSON array")
        return value

    hook_events_by_profile = _load_evidence_map(hook_events_files, jsonl=True)
    permission_events_by_profile = _load_evidence_map(permission_events_files, jsonl=False)
    serena_evidence_by_profile = _load_evidence_map(serena_evidence_files, jsonl=False)

    content = build_bundle_content(
        original_fanout_request=original_fanout_request,
        run_dir=run_dir,
        audit_records=audit_records,
        hook_events_by_profile=hook_events_by_profile,
        permission_events_by_profile=permission_events_by_profile,
        serena_evidence_by_profile=serena_evidence_by_profile,
    )
    return materialize_bundle(out_dir, content)


def _parse_profile_path_kv(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"expected PROFILE=PATH, got {value!r}")
    profile, _, raw_path = value.partition("=")
    profile = profile.strip()
    if profile not in REQUIRED_PROFILES or not raw_path:
        raise argparse.ArgumentTypeError(f"expected PROFILE=PATH with PROFILE in {REQUIRED_PROFILES}, got {value!r}")
    return profile, Path(raw_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fanout-request-file", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--audit-log-file", type=Path, default=None)
    parser.add_argument("--hook-events-file", action="append", type=_parse_profile_path_kv, default=[])
    parser.add_argument("--permission-events-file", action="append", type=_parse_profile_path_kv, default=[])
    parser.add_argument("--serena-evidence-file", action="append", type=_parse_profile_path_kv, default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        build_and_materialize_from_run_dir(
            original_fanout_request_file=args.fanout_request_file,
            run_dir=args.run_dir,
            out_dir=args.out_dir,
            audit_log_file=args.audit_log_file,
            hook_events_files=dict(args.hook_events_file),
            permission_events_files=dict(args.permission_events_file),
            serena_evidence_files=dict(args.serena_evidence_file),
        )
    except BundleBuildError as exc:
        print(f"[build_fanout_evidence_bundle] error: {exc}", file=sys.stderr)
        return 1
    print(f"[build_fanout_evidence_bundle] bundle written to: {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
