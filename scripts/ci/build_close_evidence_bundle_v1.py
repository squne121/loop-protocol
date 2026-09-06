#!/usr/bin/env python3
"""Issue #2486: producer CLI for the `close-evidence/` bundle.

Reads #2423's `CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1` performance close-grade
receipt and #2424's `CI_RELIABILITY_CLOSE_GRADE_RESULT_V1` reliability
close-grade receipt (both consumed AS-IS -- this module never re-implements
either owner's statistics, cohort materialization, or eligibility logic --
see `tests/ci/test_ci_performance_gate.py` and
`scripts/ci/build_ci_reliability_assessment_v1.py`, the respective owner
modules), plus the exact experiment manifest (#2422 `e2e_performance_
benchmark_manifest_v2`) both receipts reference, and -- ONLY when BOTH
receipts are close-grade eligible -- emits a standalone-verifiable
`close-evidence/` bundle directory:

```
close-evidence/
  close_evidence.json
  inputs/
    experiment-manifest.json
    performance-close-grade-result.json
    ci_reliability_close_grade_result_v1.json
```

If either receipt is NOT close-grade eligible, this producer refuses to
write ANY bundle output (fail-closed, non-zero exit) -- see
`CloseEvidenceBundleError` / `build_close_evidence_bundle()`.

Digest semantics (Issue #2486 In Scope -- kept deliberately distinct, never
conflated):

- `experiment_manifest_file_sha256`: raw-byte SHA-256 of the copied
  `inputs/experiment-manifest.json`, independently re-derived here AND
  cross-checked against the performance receipt's own `manifest_sha256`
  (also raw-byte SHA-256 -- the SAME algorithm, different digest computed
  over the same bytes by two independent producers).
- `experiment_manifest_canonical_digest`: the reliability receipt's own
  `manifest_digest` field, copied verbatim (a DIFFERENT algorithm --
  normalized-JSON SHA-256 via #2424's `sha256_of_canonical_json()` -- never
  treated as equal to `experiment_manifest_file_sha256` above).
- `performance_close_grade_result_file_sha256` /
  `reliability_close_grade_result_file_sha256`: raw-byte SHA-256 of the
  copied receipt files under `inputs/` (copy integrity only -- distinct
  from any digest field carried INSIDE either receipt).
- `reliability_canonical_output_digest`: the reliability receipt's own
  self-excluding `canonical_output_digest` field, copied verbatim (never
  recomputed with a different algorithm here).
- `bundle_payload_digest`: a self-excluding SHA-256 of `close_evidence.json`
  itself (canonical JSON, `sort_keys=True`, compact separators), computed
  over the dict BEFORE this field is added -- the same self-exclusion
  pattern #2424's `build_canonical_output()` uses for its own
  `canonical_output_digest`.

GitHub Actions artifact ID/digest/URL are deliberately NEVER written into
`close_evidence.json` (see Artifact semantics below) -- they belong to the
separate, smaller `CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1` schema
(`build_publication_receipt()`), whose actual population/posting is #2155's
scope, not this Issue's.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from typing import Any

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_RELIABILITY_OWNER_MODULE_PATH = os.path.join(_MODULE_DIR, "build_ci_reliability_assessment_v1.py")

_reliability_owner_module: Any = None


def _load_reliability_owner_module() -> Any:
    """Issue #2486: lazy-loads #2424's `build_ci_reliability_assessment_v1.py`
    via `importlib.util.spec_from_file_location` under a distinct module
    name (never a bare `import`, to avoid `sys.modules` collisions in a
    shared pytest session -- the same reuse pattern that module itself uses
    to load #2423's owner modules), so this producer reuses
    `strict_json_loads()` / `sha256_of_canonical_json()` / `METRICS` /
    `StrictJSONError` byte-for-byte instead of re-implementing a new ad-hoc
    JSON policy, digest algorithm, or metric list (Stop Condition: do not
    change #2423/#2424 schemas or logic here, only consume them)."""
    global _reliability_owner_module
    if _reliability_owner_module is None:
        spec = importlib.util.spec_from_file_location(
            "ci_reliability_owner_for_close_evidence_bundle", _RELIABILITY_OWNER_MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _reliability_owner_module = module
    return _reliability_owner_module


BUNDLE_INPUTS_DIRNAME = "inputs"
MANIFEST_INPUT_FILENAME = "experiment-manifest.json"
PERFORMANCE_INPUT_FILENAME = "performance-close-grade-result.json"
RELIABILITY_INPUT_FILENAME = "ci_reliability_close_grade_result_v1.json"
CLOSE_EVIDENCE_FILENAME = "close_evidence.json"

# PR #2528 review fix_delta: run-set layouts are always exactly these two --
# never derived from receipt content (a receipt missing one of these keys is
# a structural defect, never silently treated as an empty set).
REQUIRED_LAYOUTS = ("monolith", "split")

CLOSE_EVIDENCE_SCHEMA = "CI_CLOSE_EVIDENCE_BUNDLE_V1"
CLOSE_EVIDENCE_SCHEMA_VERSION = 1

# PR #2528 review fix_delta (P1-3): #2424's own assessment digest format --
# `sha256:` + 64 lowercase hex chars (`sha256_of_canonical_json()`'s own
# output shape). Used only to check well-formedness, never to recompute the
# statistic behind the digest.
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Issue #2486 AC8: these keys must NEVER appear at close_evidence.json's top
# level -- they are upload-time-only GitHub Actions artifact identity,
# recorded instead in the separate CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1
# schema (build_publication_receipt()).
GITHUB_UPLOAD_ONLY_KEYS = ("github_artifact_id", "github_artifact_digest", "artifact_url")


class CloseEvidenceBundleError(Exception):
    """Fail-closed error: the producer refuses to emit a bundle."""


def sha256_of_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _load_strict_json_file(path: str) -> tuple[dict, bytes]:
    owner = _load_reliability_owner_module()
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise CloseEvidenceBundleError(f"file_not_readable: {path}: {exc}") from exc
    try:
        data = owner.strict_json_loads(raw.decode("utf-8"))
    except owner.StrictJSONError as exc:
        raise CloseEvidenceBundleError(f"invalid_json: {path}: {exc}") from exc
    return data, raw


def verify_performance_close_grade_eligible(receipt: dict) -> list[str]:
    """Issue #2486 AC3: Performance close-grade success condition. Reads
    ONLY the fields #2423's own receipt already exposes -- never
    re-derives eligibility from `arms.*` / `evidence_errors` (that
    materialization belongs to #2423's `_materialize_close_grade_arm()`
    alone)."""
    errors: list[str] = []
    performance_assessment = receipt.get("performance_assessment") or {}
    if performance_assessment.get("complete") is not True:
        errors.append("performance_assessment.complete is not true")
    validation = receipt.get("validation") or {}
    if validation.get("semantic_valid") is not True:
        errors.append("validation.semantic_valid is not true")
    if validation.get("approval_eligible") is not True:
        errors.append("validation.approval_eligible is not true")
    if receipt.get("exit_code") != 0:
        errors.append(f"exit_code is not 0 (got {receipt.get('exit_code')!r})")
    return errors


def verify_reliability_close_grade_eligible(receipt: dict) -> list[str]:
    """Issue #2486 AC2: Reliability close-grade success condition
    (`aggregate.*` fields consumed AS-IS from #2424's `aggregate_gate()`
    output -- sample count / non-inferiority statistics are never
    recomputed here), plus the exact-3-metric / matching-`validator_results`
    structural invariant.

    PR #2528 review fix_delta (P1-3): "validator_results has exactly the 3
    metric keys" alone is NOT the real upstream structural invariant --
    #2424's `build_canonical_output()` keeps `assessment_content_digests`
    and `validator_results` as two SEPARATE per-metric maps, and its
    `aggregate` is computed FROM the per-metric `exit_code` /
    `structural_valid` / `semantic_valid` fields. So this function now also
    requires: `assessment_content_digests` has the exact 3 metric keys with
    well-formed `sha256:<hex>` digests, `validator_results` entries are
    objects (never `null`), and -- since `aggregate.*` above is already
    required to be a success -- no individual `validator_results` entry may
    contradict that success (`exit_code != 0` / `structural_valid is not
    True` / `semantic_valid is not True`). This never recomputes the
    assessment statistics or re-runs a validator -- only checks the
    receipt's OWN internal consistency."""
    errors: list[str] = []
    aggregate = receipt.get("aggregate") or {}
    for field in ("complete", "semantic_valid", "sample_satisfied", "all_non_inferior"):
        if aggregate.get(field) is not True:
            errors.append(f"aggregate.{field} is not true")
    if aggregate.get("exit_code") != 0:
        errors.append(f"aggregate.exit_code is not 0 (got {aggregate.get('exit_code')!r})")

    owner = _load_reliability_owner_module()
    required_metrics = set(owner.METRICS)

    assessment_content_digests = receipt.get("assessment_content_digests")
    if not isinstance(assessment_content_digests, dict):
        errors.append("assessment_content_digests is missing or not an object")
        assessment_content_digests = {}
    present_digest_metrics = set(assessment_content_digests.keys())
    if present_digest_metrics != required_metrics:
        missing = sorted(required_metrics - present_digest_metrics)
        extra = sorted(present_digest_metrics - required_metrics)
        if missing:
            errors.append(f"assessment_content_digests missing required metrics: {missing}")
        if extra:
            errors.append(f"assessment_content_digests has unexpected extra metrics: {extra}")
    for metric, digest_value in assessment_content_digests.items():
        if not isinstance(digest_value, str) or not _SHA256_DIGEST_RE.match(digest_value):
            errors.append(f"assessment_content_digests.{metric} is not a well-formed sha256 digest: {digest_value!r}")

    validator_results = receipt.get("validator_results")
    if not isinstance(validator_results, dict):
        errors.append("validator_results is missing or not an object")
        validator_results = {}
    present_metrics = set(validator_results.keys())
    if present_metrics != required_metrics:
        missing = sorted(required_metrics - present_metrics)
        extra = sorted(present_metrics - required_metrics)
        if missing:
            errors.append(f"validator_results missing required metrics: {missing}")
        if extra:
            errors.append(f"validator_results has unexpected extra metrics: {extra}")
    for metric, result in validator_results.items():
        if not isinstance(result, dict):
            errors.append(f"validator_results.{metric} is not an object (got {result!r})")
            continue
        if result.get("exit_code") != 0:
            errors.append(f"validator_results.{metric}.exit_code is not 0 (got {result.get('exit_code')!r})")
        if result.get("structural_valid") is not True:
            errors.append(f"validator_results.{metric}.structural_valid is not true")
        if result.get("semantic_valid") is not True:
            errors.append(f"validator_results.{metric}.semantic_valid is not true")

    return errors


def normalize_run_ids(ids: Any) -> set[str]:
    """Explicit type normalization used ONLY for cross-owner comparison
    (performance's string run IDs vs reliability's integer run IDs) --
    never used to rewrite either owner receipt's own representation."""
    return {str(x) for x in (ids or [])}


def find_duplicate_run_ids(ids: Any) -> list[str]:
    ids = list(ids or [])
    seen: set[str] = set()
    duplicates: list[str] = []
    for raw_id in ids:
        normalized = str(raw_id)
        if normalized in seen and normalized not in duplicates:
            duplicates.append(normalized)
        seen.add(normalized)
    return duplicates


def verify_run_set_binding(performance_receipt: dict, reliability_receipt: dict) -> list[str]:
    """Issue #2486 In Scope / AC4: per-layout (never flattened) root
    run-set membership binding between #2423's `arms.<layout>.
    workflow_run_ids` and #2424's `canonical_workflow_run_ids.<layout>`.
    Deliberately never reads `performance_eligible_workflow_run_ids` (a
    metric-specific projection, not the root run set).

    PR #2528 review fix_delta (P2): a required arm/layout that is entirely
    ABSENT from a receipt is a structural defect, never silently coerced to
    an empty run-set via `or {}` / `or []` (two receipts that both omit a
    required layout must NOT be accepted as "matching empty sets")."""
    errors: list[str] = []
    perf_arms = performance_receipt.get("arms")
    if not isinstance(perf_arms, dict):
        errors.append("performance_receipt.arms is missing or not an object")
        perf_arms = {}
    rel_canonical = reliability_receipt.get("canonical_workflow_run_ids")
    if not isinstance(rel_canonical, dict):
        errors.append("reliability_receipt.canonical_workflow_run_ids is missing or not an object")
        rel_canonical = {}

    for layout in REQUIRED_LAYOUTS:
        perf_arm = perf_arms.get(layout)
        if not isinstance(perf_arm, dict) or "workflow_run_ids" not in perf_arm:
            errors.append(f"performance_receipt missing required arm workflow_run_ids: layout={layout}")
            perf_raw_ids: Any = []
        else:
            perf_raw_ids = perf_arm.get("workflow_run_ids") or []

        if layout not in rel_canonical:
            errors.append(f"reliability_receipt missing required canonical_workflow_run_ids: layout={layout}")
            rel_raw_ids: Any = []
        else:
            rel_raw_ids = rel_canonical.get(layout) or []

        perf_dupes = find_duplicate_run_ids(perf_raw_ids)
        if perf_dupes:
            errors.append(f"duplicate_workflow_run_id: layout={layout} source=performance ids={perf_dupes}")
        rel_dupes = find_duplicate_run_ids(rel_raw_ids)
        if rel_dupes:
            errors.append(f"duplicate_workflow_run_id: layout={layout} source=reliability ids={rel_dupes}")

        perf_ids = normalize_run_ids(perf_raw_ids)
        rel_ids = normalize_run_ids(rel_raw_ids)
        if perf_ids != rel_ids:
            errors.append(
                "run_set_binding_mismatch: "
                f"layout={layout} performance={sorted(perf_ids)} reliability={sorted(rel_ids)}"
            )
    return errors


def verify_input_cross_binding(manifest: dict, performance_receipt: dict, reliability_receipt: dict) -> list[str]:
    """PR #2528 review fix_delta (P1-1): cross-binds the THREE validated
    inputs to each other -- each being individually close-grade eligible is
    NOT sufficient to prove they describe the SAME experiment run. Catches
    receipt/manifest mix-ups from a stale or different evaluation run that
    `verify_performance_close_grade_eligible()` / `verify_reliability_
    close_grade_eligible()` alone cannot see:

    - `manifest.experiment_identity == performance.experiment_identity ==
      reliability.experiment_identity` (all three, never just two).
    - `sha256_of_canonical_json(manifest) == reliability.manifest_digest`
      (reuses #2424's OWN `sha256_of_canonical_json()` helper -- never a
      new digest algorithm).
    - reliability's OWN self-excluding `canonical_output_digest` is
      internally self-consistent (recomputed the same way #2424's
      `build_canonical_output()` computes it: canonical-JSON-hash the
      receipt dict with `canonical_output_digest` itself excluded).
    - `performance.run_set_digest == reliability.receipt_run_set_digest`
      (same upstream value, copied verbatim by #2424 -- see module
      docstring). Deliberately never compares `manifest.
      experiment_run_set_digest` to `performance.run_set_digest` -- those
      are a DIFFERENT owner algorithm over a different input (#2424
      module's own docstring)."""
    errors: list[str] = []
    owner = _load_reliability_owner_module()

    manifest_identity = manifest.get("experiment_identity")
    performance_identity = performance_receipt.get("experiment_identity")
    reliability_identity = reliability_receipt.get("experiment_identity")
    if not manifest_identity:
        errors.append("experiment_identity_missing: source=manifest")
    if not performance_identity:
        errors.append("experiment_identity_missing: source=performance")
    if not reliability_identity:
        errors.append("experiment_identity_missing: source=reliability")
    if len({manifest_identity, performance_identity, reliability_identity}) != 1:
        errors.append(
            "experiment_identity_cross_binding_mismatch: "
            f"manifest={manifest_identity!r} performance={performance_identity!r} "
            f"reliability={reliability_identity!r}"
        )

    recomputed_manifest_canonical_digest = owner.sha256_of_canonical_json(manifest)
    reliability_manifest_digest = reliability_receipt.get("manifest_digest")
    if recomputed_manifest_canonical_digest != reliability_manifest_digest:
        errors.append(
            "reliability_manifest_digest_mismatch: "
            f"recomputed={recomputed_manifest_canonical_digest} declared={reliability_manifest_digest!r}"
        )

    reliability_declared_canonical_output_digest = reliability_receipt.get("canonical_output_digest")
    reliability_without_self_digest = {
        key: value for key, value in reliability_receipt.items() if key != "canonical_output_digest"
    }
    recomputed_canonical_output_digest = owner.sha256_of_canonical_json(reliability_without_self_digest)
    if recomputed_canonical_output_digest != reliability_declared_canonical_output_digest:
        errors.append(
            "reliability_canonical_output_digest_self_inconsistent: "
            f"recomputed={recomputed_canonical_output_digest} "
            f"declared={reliability_declared_canonical_output_digest!r}"
        )

    performance_run_set_digest = performance_receipt.get("run_set_digest")
    reliability_receipt_run_set_digest = reliability_receipt.get("receipt_run_set_digest")
    if performance_run_set_digest != reliability_receipt_run_set_digest:
        errors.append(
            "performance_reliability_run_set_digest_mismatch: "
            f"performance.run_set_digest={performance_run_set_digest!r} "
            f"reliability.receipt_run_set_digest={reliability_receipt_run_set_digest!r}"
        )

    return errors


def compute_declared_core_fields(manifest: dict, performance_receipt: dict, reliability_receipt: dict) -> dict:
    """PR #2528 review fix_delta (P1-2): a pure function computing the
    subset of `close_evidence.json`'s declared fields that are copied
    VERBATIM from validated inputs (schema/schema_version are fixed
    constants; the rest are `.get()` passthroughs already used by
    `build_close_evidence_bundle()`) -- shared by the producer (to build
    `close_evidence.json`) and the validator (to independently recompute
    the EXPECTED value from the same `inputs/` copies and compare against
    whatever `close_evidence.json` itself declares, instead of trusting
    it). This is the single source of truth for those 7 fields so a future
    field addition cannot add a "write" without also adding the matching
    "verify"."""
    return {
        "schema": CLOSE_EVIDENCE_SCHEMA,
        "schema_version": CLOSE_EVIDENCE_SCHEMA_VERSION,
        "experiment_identity": performance_receipt.get("experiment_identity"),
        "experiment_manifest_canonical_digest": reliability_receipt.get("manifest_digest"),
        "reliability_canonical_output_digest": reliability_receipt.get("canonical_output_digest"),
        "performance_run_set_digest": performance_receipt.get("run_set_digest"),
        "reliability_receipt_run_set_digest": reliability_receipt.get("receipt_run_set_digest"),
    }


def build_workflow_run_ids(performance_receipt: dict) -> dict:
    """PR #2528 review fix_delta (operational P5): raises
    `CloseEvidenceBundleError` (never a bare `ValueError`) on a
    non-integer-parseable run ID, so callers can require this to run
    BEFORE any filesystem write (see `build_close_evidence_bundle()`) --
    "the producer never emits a partial bundle directory" must also cover
    parsing failures that happen while assembling `close_evidence.json`,
    not just the earlier eligibility/binding checks."""
    arms = performance_receipt.get("arms") or {}
    result: dict[str, list[int]] = {}
    for layout in REQUIRED_LAYOUTS:
        ids = (arms.get(layout) or {}).get("workflow_run_ids") or []
        try:
            result[layout] = sorted(int(x) for x in ids)
        except (TypeError, ValueError) as exc:
            raise CloseEvidenceBundleError(f"workflow_run_id_not_integer: layout={layout}: {exc}") from exc
    return result


def build_close_evidence_bundle(
    *,
    performance_receipt_path: str,
    reliability_receipt_path: str,
    manifest_path: str,
    output_dir: str,
) -> dict:
    """Issue #2486 AC1: assembles and writes the `close-evidence/` bundle
    directory. Raises `CloseEvidenceBundleError` (no filesystem writes at
    all) if either receipt is not close-grade eligible, the run-set binding
    does not match, or the manifest raw-byte digest does not match the
    performance receipt's own `manifest_sha256` claim -- see module
    docstring."""
    manifest, manifest_raw = _load_strict_json_file(manifest_path)
    performance_receipt, performance_raw = _load_strict_json_file(performance_receipt_path)
    reliability_receipt, reliability_raw = _load_strict_json_file(reliability_receipt_path)

    perf_errors = verify_performance_close_grade_eligible(performance_receipt)
    if perf_errors:
        raise CloseEvidenceBundleError("performance_close_grade_ineligible: " + "; ".join(perf_errors))

    rel_errors = verify_reliability_close_grade_eligible(reliability_receipt)
    if rel_errors:
        raise CloseEvidenceBundleError("reliability_close_grade_ineligible: " + "; ".join(rel_errors))

    binding_errors = verify_run_set_binding(performance_receipt, reliability_receipt)
    if binding_errors:
        raise CloseEvidenceBundleError("run_set_binding_invalid: " + "; ".join(binding_errors))

    # PR #2528 review fix_delta (P1-1): individually-eligible receipts are
    # not enough -- they must also describe the SAME experiment/manifest as
    # each other (see verify_input_cross_binding() docstring).
    cross_binding_errors = verify_input_cross_binding(manifest, performance_receipt, reliability_receipt)
    if cross_binding_errors:
        raise CloseEvidenceBundleError("input_cross_binding_invalid: " + "; ".join(cross_binding_errors))

    tested_workflow_sha = manifest.get("workflow_sha")
    if not tested_workflow_sha:
        raise CloseEvidenceBundleError("experiment_manifest.workflow_sha missing")

    experiment_manifest_file_sha256 = sha256_of_bytes(manifest_raw)
    if experiment_manifest_file_sha256 != performance_receipt.get("manifest_sha256"):
        raise CloseEvidenceBundleError(
            "experiment_manifest_file_sha256 does not match performance receipt's manifest_sha256: "
            f"computed={experiment_manifest_file_sha256} "
            f"receipt={performance_receipt.get('manifest_sha256')!r}"
        )

    # PR #2528 review fix_delta (operational P5): parse/normalize
    # `workflow_run_ids` (including the `int()` conversion that used to
    # happen only when building the close_evidence dict, AFTER the
    # inputs/ writes below) BEFORE any filesystem write, so a malformed
    # run ID never leaves a partial bundle directory behind.
    workflow_run_ids = build_workflow_run_ids(performance_receipt)
    declared_core_fields = compute_declared_core_fields(manifest, performance_receipt, reliability_receipt)

    # Fail-closed checks above all passed -- only now perform filesystem
    # writes (never emit a partial/incomplete bundle directory).
    inputs_dir = os.path.join(output_dir, BUNDLE_INPUTS_DIRNAME)
    os.makedirs(inputs_dir, exist_ok=True)
    with open(os.path.join(inputs_dir, MANIFEST_INPUT_FILENAME), "wb") as fh:
        fh.write(manifest_raw)
    with open(os.path.join(inputs_dir, PERFORMANCE_INPUT_FILENAME), "wb") as fh:
        fh.write(performance_raw)
    with open(os.path.join(inputs_dir, RELIABILITY_INPUT_FILENAME), "wb") as fh:
        fh.write(reliability_raw)

    close_evidence: dict[str, Any] = {
        **declared_core_fields,
        "tested_workflow_sha": tested_workflow_sha,
        "workflow_run_ids": workflow_run_ids,
        "experiment_manifest_file_sha256": experiment_manifest_file_sha256,
        "performance_close_grade_result_file_sha256": sha256_of_bytes(performance_raw),
        "reliability_close_grade_result_file_sha256": sha256_of_bytes(reliability_raw),
    }

    owner = _load_reliability_owner_module()
    # Self-excluding digest (Issue #2486 In Scope): computed over the dict
    # ABOVE, i.e. before `bundle_payload_digest` itself is added -- same
    # pattern as #2424's build_canonical_output()'s canonical_output_digest.
    close_evidence["bundle_payload_digest"] = owner.sha256_of_canonical_json(close_evidence)

    close_evidence_path = os.path.join(output_dir, CLOSE_EVIDENCE_FILENAME)
    with open(close_evidence_path, "w", encoding="utf-8") as fh:
        json.dump(close_evidence, fh, indent=2, sort_keys=True)
        fh.write("\n")

    return close_evidence


def build_publication_receipt(
    close_evidence: dict,
    *,
    github_artifact_id: str,
    github_artifact_digest: str,
    artifact_url: str,
) -> dict:
    """Issue #2486 AC8: a separate, small publication receipt schema for
    upload-time-only GitHub Actions artifact identity. `close_evidence.json`
    itself is generated BEFORE upload and must never carry these 3 fields
    (a circular dependency -- the bundle cannot know its own future
    artifact ID/digest/URL). Actually populating and posting this receipt
    is #2155's scope, not this Issue's; this function only defines the
    schema shape."""
    return {
        "schema": "CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1",
        "schema_version": 1,
        "experiment_identity": close_evidence.get("experiment_identity"),
        "bundle_payload_digest": close_evidence.get("bundle_payload_digest"),
        "github_artifact_id": github_artifact_id,
        "github_artifact_digest": github_artifact_digest,
        "artifact_url": artifact_url,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Issue #2486: build a standalone-verifiable close-evidence bundle "
            "from #2423's performance close-grade receipt and #2424's "
            "reliability close-grade receipt, only when both are close-grade "
            "eligible."
        )
    )
    parser.add_argument("--performance-receipt", required=True, help="#2423 CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1 file")
    parser.add_argument("--reliability-receipt", required=True, help="#2424 CI_RELIABILITY_CLOSE_GRADE_RESULT_V1 file")
    parser.add_argument("--experiment-manifest", required=True, help="#2422 e2e_performance_benchmark_manifest_v2 file")
    parser.add_argument("--output-dir", required=True, help="Destination close-evidence/ bundle directory")
    args = parser.parse_args(argv)

    try:
        close_evidence = build_close_evidence_bundle(
            performance_receipt_path=args.performance_receipt,
            reliability_receipt_path=args.reliability_receipt,
            manifest_path=args.experiment_manifest,
            output_dir=args.output_dir,
        )
    except CloseEvidenceBundleError as exc:
        print(f"::error::close_evidence_bundle_fail_closed: {exc}", file=sys.stderr)
        return 1

    close_evidence_path = os.path.join(args.output_dir, CLOSE_EVIDENCE_FILENAME)
    print(
        "close_evidence_bundle_generated: "
        f"path={close_evidence_path} bundle_payload_digest={close_evidence['bundle_payload_digest']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
