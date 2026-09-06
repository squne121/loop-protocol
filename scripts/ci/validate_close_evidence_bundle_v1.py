#!/usr/bin/env python3
"""Issue #2486: standalone validator CLI for the `close-evidence/` bundle
produced by `scripts/ci/build_close_evidence_bundle_v1.py`.

`--bundle-dir <path>` is the ONLY input. This module reads `close_evidence.
json` and `inputs/` from that directory alone -- never the original
producer's working directory, never a pytest fixture variable -- so it
still succeeds after the bundle directory has been physically moved
elsewhere (Issue #2486 AC6).

Unlike a validator that only trusts `close_evidence.json`'s own claims,
this module independently RECOMPUTES every digest from the `inputs/`
copies, re-verifies both receipts' close-grade eligibility, and
re-derives the per-layout run-set binding directly from the two receipts
-- never from `close_evidence.json`'s own `workflow_run_ids` field alone
-- so a `close_evidence.json` whose OWN content has been tampered with
(and whose `bundle_payload_digest` has been correctly recomputed after
that tampering) is still caught (Issue #2486 AC7).

PR #2528 review fix_delta: in addition to the above, this module also
(a) cross-binds the three `inputs/` copies to each other (same
experiment identity, reliability's `manifest_digest` matches the real
manifest, reliability's own self-excluding `canonical_output_digest` is
internally self-consistent, performance's `run_set_digest` matches
reliability's `receipt_run_set_digest` -- see `build_close_evidence_
bundle_v1.verify_input_cross_binding()`), (b) independently re-derives
`close_evidence.json`'s declared `experiment_identity` /
`experiment_manifest_canonical_digest` / `reliability_canonical_output_
digest` / `performance_run_set_digest` / `reliability_receipt_run_set_
digest` / `schema` / `schema_version` from the same `inputs/` copies and
compares them (never trusting the declared value, even when the outer
`bundle_payload_digest` was correctly recomputed after tampering one of
them), and (c) duplicate-checks `close_evidence.json`'s OWN declared
`workflow_run_ids` BEFORE normalizing to a set (never silently absorbing
a bundle-side duplicate run ID via early set-normalization)."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import os
import sys
from typing import Any

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_PRODUCER_MODULE_PATH = os.path.join(_MODULE_DIR, "build_close_evidence_bundle_v1.py")

_producer_module: Any = None


def _load_producer_module() -> Any:
    """Issue #2486: lazy-loads this Issue's OWN producer module
    (`build_close_evidence_bundle_v1.py`) via
    `importlib.util.spec_from_file_location` under a distinct module name,
    so the validator reuses the exact same close-grade eligibility checks,
    run-set binding logic, and digest helpers the producer used -- never a
    second, possibly-divergent reimplementation."""
    global _producer_module
    if _producer_module is None:
        spec = importlib.util.spec_from_file_location(
            "ci_close_evidence_bundle_producer_owner", _PRODUCER_MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _producer_module = module
    return _producer_module


def sha256_of_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# PR #2528 review fix_delta (P1-2): sentinel distinct from a legitimate
# declared value of `None` -- used only to detect "key entirely absent from
# close_evidence.json", never compared/serialized itself.
_ABSENT = object()


def _verify_declared_core_fields(producer: Any, close_evidence: dict, expected: dict) -> list[str]:
    """PR #2528 review fix_delta (P1-2): compares `close_evidence.json`'s
    OWN declared values for `compute_declared_core_fields()`'s 7 fields
    against the values independently recomputed from the `inputs/` copies
    -- so a bundle whose input files are byte-identical and whose OWN
    `bundle_payload_digest` was correctly recomputed after tampering one of
    these fields (Issue #2486 AC7's exact adversarial model) is still
    caught. Deliberately never trusts `close_evidence.json`'s own claim as
    the source of truth for these fields."""
    errors: list[str] = []
    for field, expected_value in expected.items():
        declared_value = close_evidence.get(field, _ABSENT)
        if declared_value is _ABSENT or declared_value != expected_value:
            errors.append(
                "declared_value_mismatch: "
                f"field={field} declared={declared_value!r} expected={expected_value!r}"
            )
    return errors


def _verify_declared_workflow_run_ids(producer: Any, close_evidence: dict, performance_receipt: dict) -> list[str]:
    """PR #2528 review fix_delta (P2): mirrors `verify_run_set_binding()`'s
    per-layout structural checks for `close_evidence.json`'s OWN declared
    `workflow_run_ids` field -- applying `find_duplicate_run_ids()` BEFORE
    normalizing to a set (never normalizing straight to a set first, which
    would silently absorb a duplicate run ID the bundle itself declares),
    and treating a missing required layout as a structural defect (never
    `or []`-coerced into a trivially-matching empty set)."""
    errors: list[str] = []
    declared_workflow_run_ids = close_evidence.get("workflow_run_ids")
    if not isinstance(declared_workflow_run_ids, dict):
        errors.append("close_evidence.workflow_run_ids is missing or not an object")
        declared_workflow_run_ids = {}
    perf_arms = performance_receipt.get("arms") or {}
    for layout in producer.REQUIRED_LAYOUTS:
        if layout not in declared_workflow_run_ids:
            errors.append(f"close_evidence.workflow_run_ids missing required layout: {layout}")
            declared_raw_ids: Any = []
        else:
            declared_raw_ids = declared_workflow_run_ids.get(layout) or []

        declared_dupes = producer.find_duplicate_run_ids(declared_raw_ids)
        if declared_dupes:
            errors.append(f"duplicate_workflow_run_id: layout={layout} source=close_evidence ids={declared_dupes}")

        perf_ids = producer.normalize_run_ids((perf_arms.get(layout) or {}).get("workflow_run_ids"))
        declared_ids = producer.normalize_run_ids(declared_raw_ids)
        if perf_ids != declared_ids:
            errors.append(
                "close_evidence_workflow_run_ids_mismatch: "
                f"layout={layout} recomputed={sorted(perf_ids)} declared={sorted(declared_ids)}"
            )
    return errors


def validate_bundle(bundle_dir: str) -> list[str]:
    """Issue #2486 AC4/AC5/AC6/AC7: returns a list of error strings (empty
    means the bundle is valid). Reads ONLY from `bundle_dir` (`close_
    evidence.json` + `inputs/`) -- no other input."""
    producer = _load_producer_module()
    errors: list[str] = []

    close_evidence_path = os.path.join(bundle_dir, producer.CLOSE_EVIDENCE_FILENAME)
    inputs_dir = os.path.join(bundle_dir, producer.BUNDLE_INPUTS_DIRNAME)
    manifest_path = os.path.join(inputs_dir, producer.MANIFEST_INPUT_FILENAME)
    performance_path = os.path.join(inputs_dir, producer.PERFORMANCE_INPUT_FILENAME)
    reliability_path = os.path.join(inputs_dir, producer.RELIABILITY_INPUT_FILENAME)

    owner = producer._load_reliability_owner_module()

    try:
        close_evidence_raw = _read_bytes(close_evidence_path)
    except OSError as exc:
        return [f"close_evidence_json_not_readable: {close_evidence_path}: {exc}"]
    try:
        close_evidence = owner.strict_json_loads(close_evidence_raw.decode("utf-8"))
    except owner.StrictJSONError as exc:
        return [f"close_evidence_json_invalid: {exc}"]

    # AC8 (defense in depth -- also checked at producer time): the bundle
    # must never carry upload-time-only GitHub artifact identity keys.
    leaked_keys = sorted(set(close_evidence.keys()) & set(producer.GITHUB_UPLOAD_ONLY_KEYS))
    if leaked_keys:
        errors.append(f"github_upload_only_keys_present: {leaked_keys}")

    try:
        manifest_raw = _read_bytes(manifest_path)
        performance_raw = _read_bytes(performance_path)
        reliability_raw = _read_bytes(reliability_path)
    except OSError as exc:
        errors.append(f"inputs_not_readable: {exc}")
        return errors

    try:
        manifest = owner.strict_json_loads(manifest_raw.decode("utf-8"))
    except owner.StrictJSONError as exc:
        errors.append(f"inputs_manifest_invalid_json: {exc}")
        manifest = {}
    try:
        performance_receipt = owner.strict_json_loads(performance_raw.decode("utf-8"))
    except owner.StrictJSONError as exc:
        errors.append(f"inputs_performance_receipt_invalid_json: {exc}")
        performance_receipt = {}
    try:
        reliability_receipt = owner.strict_json_loads(reliability_raw.decode("utf-8"))
    except owner.StrictJSONError as exc:
        errors.append(f"inputs_reliability_receipt_invalid_json: {exc}")
        reliability_receipt = {}

    # Raw-byte copy-integrity digests -- recomputed independently from the
    # inputs/ copies and compared against close_evidence.json's claims.
    recomputed_manifest_sha256 = sha256_of_bytes(manifest_raw)
    if recomputed_manifest_sha256 != close_evidence.get("experiment_manifest_file_sha256"):
        errors.append(
            "experiment_manifest_file_sha256_mismatch: "
            f"recomputed={recomputed_manifest_sha256} "
            f"declared={close_evidence.get('experiment_manifest_file_sha256')!r}"
        )
    if recomputed_manifest_sha256 != performance_receipt.get("manifest_sha256"):
        errors.append(
            "experiment_manifest_file_sha256_vs_performance_receipt_mismatch: "
            f"recomputed={recomputed_manifest_sha256} "
            f"performance_receipt.manifest_sha256={performance_receipt.get('manifest_sha256')!r}"
        )

    recomputed_performance_sha256 = sha256_of_bytes(performance_raw)
    if recomputed_performance_sha256 != close_evidence.get("performance_close_grade_result_file_sha256"):
        errors.append(
            "performance_close_grade_result_file_sha256_mismatch: "
            f"recomputed={recomputed_performance_sha256} "
            f"declared={close_evidence.get('performance_close_grade_result_file_sha256')!r}"
        )

    recomputed_reliability_sha256 = sha256_of_bytes(reliability_raw)
    if recomputed_reliability_sha256 != close_evidence.get("reliability_close_grade_result_file_sha256"):
        errors.append(
            "reliability_close_grade_result_file_sha256_mismatch: "
            f"recomputed={recomputed_reliability_sha256} "
            f"declared={close_evidence.get('reliability_close_grade_result_file_sha256')!r}"
        )

    # Re-verify close-grade eligibility directly from the inputs/ copies
    # (Issue #2486 In Scope: never just trust close_evidence.json's
    # existence as proof of eligibility). PR #2528 review fix_delta
    # (P1-3): verify_reliability_close_grade_eligible() now also checks the
    # assessment_content_digests/validator_results structural invariant
    # (exact 3 metrics, well-formed digests, no individual validator result
    # contradicting an aggregate success declaration).
    perf_errors = producer.verify_performance_close_grade_eligible(performance_receipt)
    if perf_errors:
        errors.append("performance_close_grade_ineligible: " + "; ".join(perf_errors))
    rel_errors = producer.verify_reliability_close_grade_eligible(reliability_receipt)
    if rel_errors:
        errors.append("reliability_close_grade_ineligible: " + "; ".join(rel_errors))

    # Cross-binding between the three inputs (PR #2528 review fix_delta
    # P1-1): individually-eligible receipts alone do not prove they
    # describe the SAME experiment/manifest as each other.
    cross_binding_errors = producer.verify_input_cross_binding(manifest, performance_receipt, reliability_receipt)
    errors.extend(cross_binding_errors)

    # Declared-value re-derivation (PR #2528 review fix_delta P1-2):
    # close_evidence.json's own claims for these 7 fields are never trusted
    # -- they are independently recomputed from the inputs/ copies via the
    # SAME pure function the producer uses to build them, and compared.
    expected_core_fields = producer.compute_declared_core_fields(manifest, performance_receipt, reliability_receipt)
    errors.extend(_verify_declared_core_fields(producer, close_evidence, expected_core_fields))

    # Run-set binding: recomputed directly from the two receipts
    # (independent of close_evidence.json's own workflow_run_ids field),
    # per-layout, never flattened, never using
    # performance_eligible_workflow_run_ids as a substitute for the root
    # set.
    binding_errors = producer.verify_run_set_binding(performance_receipt, reliability_receipt)
    errors.extend(binding_errors)

    # close_evidence.json's OWN declared workflow_run_ids (PR #2528 review
    # fix_delta P2): duplicate-checked BEFORE set-normalization, required
    # layouts checked for presence -- never silently `or []`-coerced.
    errors.extend(_verify_declared_workflow_run_ids(producer, close_evidence, performance_receipt))

    # tested_workflow_sha binding (AC5): bound to the manifest's OWN
    # workflow_sha field, never a substitute provenance SHA.
    manifest_workflow_sha = manifest.get("workflow_sha")
    declared_tested_workflow_sha = close_evidence.get("tested_workflow_sha")
    if manifest_workflow_sha != declared_tested_workflow_sha:
        errors.append(
            "tested_workflow_sha_binding_mismatch: "
            f"manifest.workflow_sha={manifest_workflow_sha!r} "
            f"close_evidence.tested_workflow_sha={declared_tested_workflow_sha!r}"
        )

    # Self-excluding outer bundle_payload_digest: recompute over
    # close_evidence.json's OWN content with that field excluded, and
    # compare to the declared value. Passing this check alone is NEVER
    # sufficient (Issue #2486 AC7) -- it only proves close_evidence.json is
    # internally self-consistent, not that its claims match inputs/.
    close_evidence_without_digest = copy.deepcopy(close_evidence)
    close_evidence_without_digest.pop("bundle_payload_digest", None)
    recomputed_bundle_payload_digest = owner.sha256_of_canonical_json(close_evidence_without_digest)
    if recomputed_bundle_payload_digest != close_evidence.get("bundle_payload_digest"):
        errors.append(
            "bundle_payload_digest_mismatch: "
            f"recomputed={recomputed_bundle_payload_digest} "
            f"declared={close_evidence.get('bundle_payload_digest')!r}"
        )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Issue #2486: standalone validator for a close-evidence bundle "
            "directory (close_evidence.json + inputs/). Reads only from "
            "--bundle-dir; no other input."
        )
    )
    parser.add_argument("--bundle-dir", required=True, help="Path to the close-evidence/ bundle directory")
    args = parser.parse_args(argv)

    errors = validate_bundle(args.bundle_dir)
    if errors:
        for error in errors:
            print(f"::error::close_evidence_bundle_invalid: {error}", file=sys.stderr)
        return 1

    print(f"close_evidence_bundle_valid: bundle_dir={args.bundle_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
