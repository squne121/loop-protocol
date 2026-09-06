#!/usr/bin/env python3
"""Issue #2424: production builder for CI_TEST_RELIABILITY_ASSESSMENT_V1 evidence.

This module is a CONSUMER of three completed prerequisite contracts and never
re-implements, imports, or mutates their owner schemas/producers/validators:

- #2422 `e2e_performance_benchmark_manifest_v2` (immutable topology manifest;
  `scripts/ci/collect_e2e_performance_benchmark.py`).
- #2423 `CI_PERFORMANCE_CLOSE_GRADE_RESULT_V1` receipt (canonical cohort
  materialization; `tests/ci/test_ci_performance_gate.py::build_close_grade_receipt`).
- #2432/#2507 `CI_TEST_RELIABILITY_ASSESSMENT_V1` schema/validator/power design
  (`.claude/skills/ci-test-performance/scripts/validate_ci_reliability_assessment_v1.py`,
  `schemas/ci_test_reliability_assessment_v1.schema.json`).

`#2422 experiment_run_set_digest` and `#2423 run_set_digest` are separate owner
algorithms over separate inputs -- this module never compares them by string
equality. It only compares the EXPANDED `workflow_run_id` membership sets for
exact equality (see `verify_exact_run_set_binding`).

See docs/dev/ci-test-reliability-assessment.md ("#2424 production wiring 契約")
for the full input/output contract this module implements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from typing import Any

METRICS = (
    "workflow_failure_rate",
    "playwright_flaky_test_rate",
    "playwright_terminal_failure_rate",
)
ARMS = ("before", "after")
LAYOUT_TO_ARM = {"monolith": "before", "split": "after"}
ARM_TO_LAYOUT = {"before": "monolith", "after": "split"}

# AC3 binary mapping. `success` -> 0 (0-observation), `failure`/`timed_out` ->
# 1 (affected observation). Everything else is evidence-ineligible (hard
# error, never silently coerced). Duplicated (not imported) from the #2432/
# #2507 validator's `ELIGIBLE_WORKFLOW_CONCLUSIONS` -- both files are
# independently frozen consumers of the same fixed contract; this module
# never imports/mutates the validator itself (Out of Scope).
ELIGIBLE_WORKFLOW_CONCLUSIONS = ("success", "failure", "timed_out")

# Repo-static Reliability V1 power design (docs/dev/ci-test-reliability-assessment.md).
# This is the fixed golden value (`n=22` is the first qualifying sample count for
# the retained `newcombe_wilson_hybrid_exact_binomial_power_v1` design) -- V1 has
# exactly one design, so this constant is duplicated here (not imported) as a
# frozen literal, never re-derived or re-negotiated by this producer.
POWER_DESIGN_ID = "newcombe_wilson_hybrid_exact_binomial_power_v1"
REQUIRED_SAMPLE_COUNT_PER_ARM = 22

DEFAULT_VALIDATOR_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        ".claude",
        "skills",
        "ci-test-performance",
        "scripts",
        "validate_ci_reliability_assessment_v1.py",
    )
)

# Issue #2424 Finding 3 fix_delta (OWNER REQUEST_CHANGES issuecomment-
# 5556542041): the ONLY genuinely public, importable, production (non-test)
# owner entry point that independently re-derives #2422's
# `experiment_run_set_digest` (and the manifest's other semantic invariants)
# from a manifest dict is `validate_manifest_v2_semantics` in
# `collect_e2e_performance_benchmark.py` -- reused here via file-path import
# (never copy/re-implemented) so this module never invents a competing
# digest algorithm.
#
# Issue #2424 Finding 3 residual-gap closure (issue-refinement-loop scope
# delta review, live Issue #2424 Allowed Paths update +
# `tests/ci/test_ci_performance_gate.py` AC1 clarification note): #2423's
# `receipt.run_set_digest` algorithm previously had no public/importable
# production entry point (`_run_set_digest` was a leading-underscore
# private helper). That helper has since been renamed (algorithm
# unchanged) to the public `compute_run_set_digest` in
# `tests/ci/test_ci_performance_gate.py`, which the live Issue body's AC1
# clarification explicitly authorizes this module to import and use for
# independent owner-algorithm re-verification of `receipt.run_set_digest`
# (this is importing an owner-side PUBLIC function, not re-implementing or
# duplicating #2423's digest algorithm).
_COLLECT_E2E_MODULE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_e2e_performance_benchmark.py")
)
_collect_e2e_module = None

_PERFORMANCE_GATE_TEST_MODULE_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "tests",
        "ci",
        "test_ci_performance_gate.py",
    )
)
_performance_gate_test_module = None


def _load_collect_e2e_performance_benchmark_module():
    global _collect_e2e_module
    if _collect_e2e_module is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "collect_e2e_performance_benchmark_owner", _COLLECT_E2E_MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _collect_e2e_module = module
    return _collect_e2e_module


def _load_performance_gate_test_module():
    """Loads #2423's owner module (`tests/ci/test_ci_performance_gate.py`)
    via file-path import (mirroring `_load_collect_e2e_performance_
    benchmark_module` above), so this module can call its public
    `compute_run_set_digest` entry point without re-implementing #2423's
    digest algorithm."""
    global _performance_gate_test_module
    if _performance_gate_test_module is None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "ci_performance_gate_owner", _PERFORMANCE_GATE_TEST_MODULE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _performance_gate_test_module = module
    return _performance_gate_test_module


def verify_manifest_v2_owner_semantics(manifest: dict[str, Any]) -> list[str]:
    """Issue #2424 Finding 3 (AC1): independently re-verifies `manifest`
    (including a recomputation of `experiment_run_set_digest`) via the real
    #2422 owner function `validate_manifest_v2_semantics` -- never a
    re-implementation of that algorithm. Returns the owner function's own
    violation strings verbatim (empty list means the manifest is
    self-consistent under #2422's own semantics)."""
    module = _load_collect_e2e_performance_benchmark_module()
    return list(module.validate_manifest_v2_semantics(manifest))


def recompute_receipt_run_set_digest(receipt: dict[str, Any]) -> str:
    """Issue #2424 Finding 3 residual-gap closure (AC1): independently
    recomputes #2423's `receipt.run_set_digest` from
    `receipt.arms.{monolith,split}.workflow_run_ids` via the real #2423
    owner function `compute_run_set_digest` -- never a re-implementation
    of that algorithm."""
    module = _load_performance_gate_test_module()
    monolith_ids = receipt.get("arms", {}).get("monolith", {}).get("workflow_run_ids", [])
    split_ids = receipt.get("arms", {}).get("split", {}).get("workflow_run_ids", [])
    return module.compute_run_set_digest(monolith_ids, split_ids)


_SHA256_DIGEST_PATTERN_RE = None


def _sha256_digest_pattern():
    global _SHA256_DIGEST_PATTERN_RE
    if _SHA256_DIGEST_PATTERN_RE is None:
        import re

        _SHA256_DIGEST_PATTERN_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
    return _SHA256_DIGEST_PATTERN_RE


def is_well_formed_sha256_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(_sha256_digest_pattern().match(value))


# Issue #2424 Finding 4 fix_delta: #2423's `receipt.evidence_errors` mixes
# two distinct classes of problem: (1) identity/run-set/manifest-binding
# integrity violations (a genuine Reliability-blocking defect), and (2)
# Performance-measurement-ineligibility reasons -- a run's OWN Performance
# metrics could not be computed (e.g. a missing paired Performance provider
# job, or an invalid gate-ready timestamp) even though the SAME run's
# workflow/Playwright evidence for Reliability may be completely fine. Only
# reasons in this explicit allowlist are treated as Performance-only (never
# block Reliability by themselves); everything else -- including any reason
# string this module does not recognize -- remains fail-closed, per Finding
# 4's "未知エラーを無条件に無視しない" requirement. This allowlist is
# duplicated (not imported) from the #2423 owner's own literal reason
# strings (`tests/ci/test_ci_performance_gate.py`) -- a fixed set of
# string labels is not a "digest algorithm" reuse concern (unlike Finding 3
# above): duplicating a small, stable, already-public string vocabulary
# used purely for constant-membership classification, never re-executing or
# re-deriving any owner computation.
RECEIPT_PERFORMANCE_ONLY_EVIDENCE_ERROR_REASONS = frozenset(
    {
        "gate_ready_timestamp_missing_or_invalid",
        "missing_pair_e2e-core",
        "missing_pair_e2e-responsive-matrix",
    }
)


class StrictJSONError(ValueError):
    """Duplicate keys / non-finite constants / invalid JSON."""


class EnvelopeBuildError(RuntimeError):
    """Raised only for genuinely unrecoverable operational failures (missing
    files, unreadable JSON). Binding/evidence violations are reported as
    `errors` strings, never exceptions -- callers must always receive a
    diagnostic artifact, even on fail-closed rejection."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate_json_key: {key!r}")
        result[key] = value
    return result


def _reject_constant(constant: str) -> float:
    raise StrictJSONError(f"non_finite_json_constant: {constant}")


def strict_json_loads(raw_text: str) -> dict[str, Any]:
    try:
        return json.loads(raw_text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJSONError(f"json_syntax_error: {exc}") from exc


def load_json_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return strict_json_loads(handle.read())
    except OSError as exc:
        raise EnvelopeBuildError(f"file_not_readable: {path}: {exc}") from exc
    except StrictJSONError as exc:
        raise EnvelopeBuildError(f"invalid_json: {path}: {exc}") from exc


def _write_json_file(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2)
        handle.write("\n")


def sha256_of_canonical_json(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_workspace_relative_path(full_path: str, workspace: str) -> str:
    """Issue #2424 AC2: `PLAYWRIGHT_JSON_OUTPUT_FILE` is set as a
    `$GITHUB_WORKSPACE`-relative full path; the manifest's `evidence_file`
    is workspace-relative. Normalizes `full_path` to workspace-relative
    (POSIX separators) so binding never requires literal absolute-path
    equality."""
    workspace_norm = os.path.normpath(workspace)
    full_norm = os.path.normpath(full_path)
    if full_norm == workspace_norm:
        return ""
    prefix = workspace_norm + os.sep
    if full_norm.startswith(prefix):
        return full_norm[len(prefix) :].replace(os.sep, "/")
    # Not under the workspace at all -- return as-is (caller decides whether
    # this is a binding error) rather than raising.
    return full_norm.replace(os.sep, "/")


# --------------------------------------------------------------------------- #
# Prerequisite availability (AC10) -- checked BEFORE any binding/evidence work
# starts. Distinguishes "prerequisite contract output missing/malformed" from
# a genuine evidence binding mismatch (AC1/AC3) so a caller never begins
# runtime execution against an incomplete #2422/#2423/#2432/#2507 surface.
# --------------------------------------------------------------------------- #
def verify_prerequisites_available(
    manifest: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    manifest_raw_bytes: bytes | None = None,
) -> list[str]:
    errors: list[str] = []
    if manifest is None:
        errors.append("prerequisite_unavailable: manifest")
    else:
        for key in ("experiment_identity", "workflow_sha", "frozen_non_treatment", "blocks"):
            if key not in manifest:
                errors.append(f"prerequisite_incomplete: manifest.{key}")
        if isinstance(manifest.get("frozen_non_treatment"), dict):
            # Issue #2424 Finding 1 fix_delta: `expected_test_count` is
            # OPTIONAL in the real #2422 schema (`e2e_performance_benchmark_
            # manifest_v2.schema.json`'s `FrozenNonTreatment.required` omits
            # it) -- only `expected_playwright_invocations` is required.
            # Treating `expected_test_count` as mandatory here was an #2424
            # producer-local incompatibility with the upstream schema.
            if "expected_playwright_invocations" not in manifest["frozen_non_treatment"]:
                errors.append("prerequisite_incomplete: manifest.frozen_non_treatment.expected_playwright_invocations")
        # Issue #2424 Finding 3 fix_delta: independent re-verification of
        # `experiment_run_set_digest` (and the manifest's other semantic
        # invariants) via the real #2422 owner function -- never a
        # re-implementation of that algorithm (see
        # `verify_manifest_v2_owner_semantics` above).
        try:
            owner_violations = verify_manifest_v2_owner_semantics(manifest)
        except Exception as exc:  # pragma: no cover -- operational failure only
            owner_violations = [f"owner_semantics_verification_operational_error: {exc}"]
        errors.extend(f"manifest_owner_semantics_violation: {v}" for v in owner_violations)
    if receipt is None:
        errors.append("prerequisite_unavailable: receipt")
    else:
        for key in ("experiment_identity", "manifest_sha256", "run_set_digest", "materialization_policy", "arms"):
            if key not in receipt:
                errors.append(f"prerequisite_incomplete: receipt.{key}")
        if isinstance(receipt.get("arms"), dict):
            for layout in ("monolith", "split"):
                if layout not in receipt["arms"]:
                    errors.append(f"prerequisite_incomplete: receipt.arms.{layout}")
        # Issue #2424 Finding 4 fix_delta: bifurcate `receipt.evidence_errors`
        # -- only non-Performance-only reasons block Reliability (fail-closed
        # for anything not explicitly recognized as Performance-only).
        evidence_errors = receipt.get("evidence_errors") or []
        blocking_evidence_errors = [
            e
            for e in evidence_errors
            if not (isinstance(e, dict) and e.get("reason") in RECEIPT_PERFORMANCE_ONLY_EVIDENCE_ERROR_REASONS)
        ]
        if blocking_evidence_errors:
            errors.append(
                "prerequisite_incomplete: receipt.evidence_errors_contains_non_performance_only_reason: "
                f"{[e.get('reason') if isinstance(e, dict) else e for e in blocking_evidence_errors]}"
            )
        # Issue #2424 Finding 3 fix_delta: `receipt.manifest_sha256` is
        # independently re-verifiable by this module WITHOUT importing any
        # #2423 owner algorithm -- it is a standard sha256 digest of the
        # `--manifest` file's own bytes (the same universal operation this
        # module already trusts for its own `sha256_of_canonical_json`
        # helper), never #2423-specific logic.
        if manifest_raw_bytes is not None and isinstance(receipt.get("manifest_sha256"), str):
            recomputed = "sha256:" + hashlib.sha256(manifest_raw_bytes).hexdigest()
            if receipt["manifest_sha256"] != recomputed:
                errors.append(
                    "prerequisite_manifest_sha256_mismatch: "
                    f"receipt={receipt['manifest_sha256']!r} recomputed={recomputed!r}"
                )
        # Issue #2424 Finding 3 residual-gap closure (issue-refinement-loop
        # scope delta review): `receipt.run_set_digest` is now independently
        # re-verified via the real #2423 owner algorithm
        # (`compute_run_set_digest`, see `recompute_receipt_run_set_digest`
        # above), not format validation alone. The ACTUAL correctness of the
        # run-set membership this digest claims to identify is ALSO
        # independently guaranteed by `verify_exact_run_set_binding`'s
        # direct membership comparison against the manifest (defense in
        # depth -- neither check alone is trusted as sufficient).
        raw_run_set_digest = receipt.get("run_set_digest")
        if "run_set_digest" in receipt:
            if not is_well_formed_sha256_digest(raw_run_set_digest):
                errors.append(f"prerequisite_incomplete: receipt.run_set_digest_malformed: {raw_run_set_digest!r}")
            elif isinstance(receipt.get("arms"), dict) and all(
                layout in receipt["arms"] for layout in ("monolith", "split")
            ):
                try:
                    recomputed_run_set_digest = recompute_receipt_run_set_digest(receipt)
                except Exception as exc:  # pragma: no cover -- operational failure only
                    errors.append(f"run_set_digest_owner_recomputation_operational_error: {exc}")
                else:
                    if raw_run_set_digest != recomputed_run_set_digest:
                        errors.append(
                            "prerequisite_run_set_digest_mismatch: "
                            f"receipt={raw_run_set_digest!r} recomputed={recomputed_run_set_digest!r}"
                        )
    if manifest is not None and receipt is not None and manifest.get("experiment_identity") != receipt.get(
        "experiment_identity"
    ):
        errors.append(
            "prerequisite_experiment_identity_mismatch: "
            f"manifest={manifest.get('experiment_identity')!r} receipt={receipt.get('experiment_identity')!r}"
        )
    return errors


# --------------------------------------------------------------------------- #
# Exact run-set binding (AC1) -- membership equality only, never digest string
# equality (#2422 experiment_run_set_digest and #2423 run_set_digest are
# separate owner algorithms/inputs).
# --------------------------------------------------------------------------- #
def manifest_run_ids_for_layout(manifest: dict[str, Any], layout: str) -> set[int]:
    ids: set[int] = set()
    for block in manifest.get("blocks", []):
        for run in block.get("runs", []):
            if run.get("benchmark_layout") == layout:
                ids.add(int(run["workflow_run_id"]))
    return ids


def manifest_run_record(manifest: dict[str, Any], layout: str, workflow_run_id: int) -> dict[str, Any] | None:
    for block in manifest.get("blocks", []):
        for run in block.get("runs", []):
            if run.get("benchmark_layout") == layout and int(run.get("workflow_run_id", -1)) == workflow_run_id:
                return run
    return None


def receipt_run_ids_for_layout(receipt: dict[str, Any], layout: str) -> set[int]:
    return {int(x) for x in receipt.get("arms", {}).get(layout, {}).get("workflow_run_ids", [])}


def verify_exact_run_set_binding(manifest: dict[str, Any], receipt: dict[str, Any]) -> dict[str, list[str]]:
    """Returns `{"monolith": [...errors...], "split": [...errors...]}`. An
    empty list means exact membership equality for that layout."""
    result: dict[str, list[str]] = {}
    for layout in ("monolith", "split"):
        manifest_ids = manifest_run_ids_for_layout(manifest, layout)
        receipt_ids = receipt_run_ids_for_layout(receipt, layout)
        errors: list[str] = []
        if manifest_ids != receipt_ids:
            errors.append(
                f"exact_run_set_membership_mismatch: layout={layout} "
                f"manifest={sorted(manifest_ids)} receipt={sorted(receipt_ids)}"
            )
        if not manifest_ids:
            errors.append(f"empty_canonical_run_set: layout={layout}")
        result[layout] = errors
    return result


# --------------------------------------------------------------------------- #
# Official Playwright JSON reporter parsing.
# --------------------------------------------------------------------------- #
def _walk_specs(suites: list[dict[str, Any]]):
    for suite in suites:
        yield from suite.get("specs", [])
        yield from _walk_specs(suite.get("suites", []))


def extract_playwright_test_cases(playwright_json: dict[str, Any]) -> list[dict[str, str]]:
    """Returns `[{"test_id": str, "outcome": str}, ...]` from the official
    Playwright JSON reporter's `suites[].specs[].tests[]` tree. `test.status`
    is Playwright's own computed outcome (`expected|unexpected|flaky|skipped`)
    -- the primary classification source (never re-derived from
    `results[].retry`)."""
    cases: list[dict[str, str]] = []
    for spec in _walk_specs(playwright_json.get("suites", [])):
        spec_id = spec.get("id", "")
        for test in spec.get("tests", []):
            project_id = test.get("projectId", "")
            test_id = f"{project_id}:{spec_id}" if project_id else spec_id
            cases.append({"test_id": test_id, "outcome": test.get("status", "")})
    return cases


def playwright_report_errors(playwright_json: dict[str, Any]) -> list[Any]:
    return playwright_json.get("errors", []) or []


def playwright_config_metadata(playwright_json: dict[str, Any]) -> dict[str, Any]:
    return (playwright_json.get("config", {}) or {}).get("metadata", {}) or {}


# --------------------------------------------------------------------------- #
# Composite envelope construction (AC1/AC3/AC5/AC6/AC9).
# --------------------------------------------------------------------------- #
def build_composite_envelope(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    workflow_evidence: dict[str, dict[str, dict[str, Any]]],
    playwright_json_loader,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """`workflow_evidence`: `{"monolith": {"<run_id>": <WorkflowEvidenceRecord>},
    "split": {...}}` where each record is
    `{"conclusion": str, "run_attempt": int, "latest_run_attempt": int,
    "jobs": [{"name": str, "conclusion": str|None, "status": str}]}` --
    `run_attempt` MUST be the attempt-1 authoritative record (fetched via
    `GET .../runs/{id}/attempts/1`); `latest_run_attempt` is the run's
    CURRENT latest attempt count (fetched via `GET .../runs/{id}`), used only
    for AC6 rerun detection -- an old attempt-1 artifact is never assumed
    available beyond what `workflow_evidence` actually supplies.

    `playwright_json_loader(layout: str, workflow_run_id: int, invocation_id:
    str) -> dict | None` returns the parsed official Playwright JSON for
    that (layout, run, invocation) triple, or `None` if the artifact is
    missing. Issue #2424 Finding 2 fix_delta: the signature now carries
    `workflow_run_id` -- MULTIPLE runs per arm (the real 2-run/22-run
    close-grade cohort case, not just #2424's own 1-run-per-arm smoke) each
    have their OWN Playwright JSON per invocation; a loader keyed only by
    `(layout, invocation_id)` cannot distinguish a second run in the same
    arm from the first, and silently re-reads the first run's file.

    Returns `(envelope_or_none, diagnostic)`. `envelope` is `None` whenever
    `diagnostic["errors"]` is non-empty (fail-closed; no partial assessment).
    `diagnostic` is always populated (workflow_run_id / conclusion / expected
    invocations / observed invocations / missing artifacts / binding errors /
    per-run eligibility reason), independent of pass/fail."""
    errors: list[str] = []
    per_run_diagnostics: list[dict[str, Any]] = []
    rerun_run_ids: set[int] = set()

    if manifest.get("experiment_identity") != receipt.get("experiment_identity"):
        errors.append(
            "experiment_identity_mismatch: "
            f"manifest={manifest.get('experiment_identity')!r} receipt={receipt.get('experiment_identity')!r}"
        )

    binding = verify_exact_run_set_binding(manifest, receipt)
    for layout_errors in binding.values():
        errors.extend(layout_errors)

    runs_by_arm: dict[str, dict[int, dict[str, Any]]] = {"before": {}, "after": {}}
    expected_test_count = manifest.get("frozen_non_treatment", {}).get("expected_test_count")
    # Issue #2424 Finding 1 fix_delta: the real #2422 schema's
    # `expected_playwright_invocations` entries share the SAME logical
    # identity (`invocation_id`/`lane`) across BOTH `benchmark_layout` arms
    # -- only `provider_placement[layout]` (the PHYSICAL provider job) is
    # arm-dependent. This module never filters the expected-invocation list
    # by arm (the removed, schema-nonconformant `benchmark_layout_only`
    # producer-local field, which does not exist in
    # `ExpectedPlaywrightInvocation`'s schema); every expected invocation is
    # expected under EVERY arm.
    expected_invocations = manifest.get("frozen_non_treatment", {}).get("expected_playwright_invocations", [])

    for layout in ("monolith", "split"):
        if binding[layout]:
            # Exact run-set binding already failed for this layout -- do not
            # attempt evidence resolution against an unverified run set.
            continue
        arm = LAYOUT_TO_ARM[layout]
        for run_id in sorted(manifest_run_ids_for_layout(manifest, layout)):
            run_diag: dict[str, Any] = {
                "layout": layout,
                "arm": arm,
                "workflow_run_id": run_id,
                "expected_invocations": [inv["invocation_id"] for inv in expected_invocations],
                "observed_invocations": [],
                "missing_artifacts": [],
                "binding_errors": [],
                "eligibility_reason": None,
                "rerun_detected": False,
            }
            manifest_run = manifest_run_record(manifest, layout, run_id)
            record = workflow_evidence.get(layout, {}).get(str(run_id))
            if manifest_run is None:
                run_diag["binding_errors"].append("manifest_run_record_missing")
                errors.append(f"manifest_run_record_missing: layout={layout} workflow_run_id={run_id}")
                per_run_diagnostics.append(run_diag)
                continue
            if record is None:
                run_diag["binding_errors"].append("workflow_evidence_missing")
                errors.append(f"workflow_evidence_missing: layout={layout} workflow_run_id={run_id}")
                per_run_diagnostics.append(run_diag)
                continue
            run_diag["conclusion"] = record.get("conclusion")
            if int(record.get("run_attempt", -1)) != 1:
                run_diag["binding_errors"].append("workflow_evidence_not_attempt_1")
                errors.append(f"workflow_evidence_not_attempt_1: layout={layout} workflow_run_id={run_id}")
            if manifest_run.get("conclusion") != record.get("conclusion"):
                run_diag["binding_errors"].append("workflow_conclusion_binding_mismatch")
                errors.append(
                    f"workflow_conclusion_binding_mismatch: layout={layout} workflow_run_id={run_id} "
                    f"manifest={manifest_run.get('conclusion')!r} evidence={record.get('conclusion')!r}"
                )
            latest_attempt = record.get("latest_run_attempt", record.get("run_attempt"))
            if latest_attempt is not None and int(latest_attempt) > 1:
                run_diag["rerun_detected"] = True
                rerun_run_ids.add(run_id)

            expected_job_names = {job.get("job") for job in manifest_run.get("provider_jobs", [])}
            observed_jobs_by_name = {job.get("name"): job for job in record.get("jobs", [])}
            missing_jobs = expected_job_names - set(observed_jobs_by_name)
            if missing_jobs:
                run_diag["binding_errors"].append(f"missing_expected_job_evidence:{sorted(missing_jobs)}")
                errors.append(
                    f"missing_expected_job_evidence: layout={layout} workflow_run_id={run_id} "
                    f"jobs={sorted(missing_jobs)}"
                )
            # Issue #2424 Finding 5 fix_delta: job-level evidence must prove
            # the target job actually FINISHED (status=="completed"), not
            # merely that a job record with that name exists (e.g. a
            # `queued`/`in_progress` job is not usable evidence). This never
            # re-defines `workflow_failure_rate` from job-level
            # conclusions -- it is a completeness check only.
            incomplete_jobs = sorted(
                name
                for name in expected_job_names & set(observed_jobs_by_name)
                if observed_jobs_by_name[name].get("status") != "completed"
            )
            if incomplete_jobs:
                run_diag["binding_errors"].append(f"job_not_completed:{incomplete_jobs}")
                errors.append(
                    f"job_not_completed: layout={layout} workflow_run_id={run_id} jobs={incomplete_jobs}"
                )

            conclusion = record.get("conclusion")
            if conclusion not in ELIGIBLE_WORKFLOW_CONCLUSIONS:
                run_diag["eligibility_reason"] = f"ineligible_workflow_conclusion:{conclusion}"
                errors.append(
                    f"workflow_run_evidence_ineligible: layout={layout} workflow_run_id={run_id} "
                    f"conclusion={conclusion!r}"
                )
                per_run_diagnostics.append(run_diag)
                continue
            classification_workflow = "failure" if conclusion in ("failure", "timed_out") else "success"

            total_test_cases = 0
            has_flaky = False
            has_unexpected = False
            collected_cases: list[dict[str, str]] = []
            for inv in expected_invocations:
                # Issue #2424 Finding 1 fix_delta: resolve the PHYSICAL
                # provider job this invocation actually ran under, in THIS
                # arm, via `provider_placement[layout]` (never a
                # producer-local `benchmark_layout_only` field).
                provider_placement = inv.get("provider_placement") or {}
                provider_job = provider_placement.get(layout)
                if not provider_job:
                    run_diag["binding_errors"].append(
                        f"missing_provider_placement:{inv.get('invocation_id')}:{layout}"
                    )
                    errors.append(
                        f"invocation_missing_provider_placement: layout={layout} workflow_run_id={run_id} "
                        f"invocation_id={inv.get('invocation_id')!r}"
                    )
                    continue
                pj = playwright_json_loader(layout, run_id, inv["invocation_id"])
                if pj is None:
                    run_diag["missing_artifacts"].append(inv["invocation_id"])
                    errors.append(
                        f"missing_playwright_json_artifact: layout={layout} workflow_run_id={run_id} "
                        f"invocation_id={inv['invocation_id']}"
                    )
                    continue
                run_diag["observed_invocations"].append(inv["invocation_id"])
                report_errors = playwright_report_errors(pj)
                if report_errors:
                    run_diag["binding_errors"].append(f"report_errors_non_empty:{inv['invocation_id']}")
                    errors.append(
                        f"playwright_report_errors_non_empty: layout={layout} workflow_run_id={run_id} "
                        f"invocation_id={inv['invocation_id']}"
                    )
                cases = extract_playwright_test_cases(pj)
                if not cases:
                    run_diag["binding_errors"].append(f"zero_test_case_count:{inv['invocation_id']}")
                    errors.append(
                        f"zero_test_case_count: layout={layout} workflow_run_id={run_id} "
                        f"invocation_id={inv['invocation_id']}"
                    )
                meta = playwright_config_metadata(pj)
                expected_meta = {
                    "experiment_identity": manifest.get("experiment_identity", ""),
                    "workflow_run_id": str(run_id),
                    "run_attempt": "1",
                    "benchmark_layout": layout,
                    "invocation_id": inv["invocation_id"],
                    "lane": inv.get("lane", ""),
                    "workflow_sha": manifest.get("workflow_sha", ""),
                }
                for key, expected_value in expected_meta.items():
                    observed_value = str(meta.get(key, ""))
                    if observed_value != str(expected_value):
                        run_diag["binding_errors"].append(f"metadata_mismatch:{inv['invocation_id']}.{key}")
                        errors.append(
                            f"playwright_metadata_binding_mismatch: layout={layout} workflow_run_id={run_id} "
                            f"invocation_id={inv['invocation_id']} field={key} "
                            f"expected={expected_value!r} observed={observed_value!r}"
                        )
                total_test_cases += len(cases)
                if any(case["outcome"] == "flaky" for case in cases):
                    has_flaky = True
                if any(case["outcome"] == "unexpected" for case in cases):
                    has_unexpected = True
                collected_cases.extend(cases)

            if expected_test_count is not None and total_test_cases != expected_test_count:
                run_diag["binding_errors"].append("expected_test_count_mismatch")
                errors.append(
                    f"expected_test_count_mismatch: layout={layout} workflow_run_id={run_id} "
                    f"expected={expected_test_count} observed={total_test_cases}"
                )

            per_run_diagnostics.append(run_diag)
            runs_by_arm[arm][run_id] = {
                "conclusion": conclusion,
                "classification_workflow": classification_workflow,
                "has_flaky": has_flaky,
                "has_unexpected": has_unexpected,
                "test_cases": collected_cases,
                "rerun_detected": run_diag["rerun_detected"],
            }

    diagnostic = {
        "schema": "CI_RELIABILITY_COMPOSITE_ENVELOPE_DIAGNOSTIC_V1",
        "errors": errors,
        "runs": per_run_diagnostics,
        "rerun_run_ids": sorted(rerun_run_ids),
    }

    if errors:
        return None, diagnostic

    envelope = {
        "experiment_identity": manifest.get("experiment_identity", ""),
        "workflow_sha": manifest.get("workflow_sha", ""),
        "runs_by_arm": runs_by_arm,
        "rerun_run_ids": sorted(rerun_run_ids),
    }
    return envelope, diagnostic


# --------------------------------------------------------------------------- #
# Statistical functions -- mirrors (duplicated, never imported from) the
# retained #2432/#2507 design so the producer and the independent validator
# never share code, only the same frozen contract
# (docs/dev/ci-test-reliability-assessment.md).
# --------------------------------------------------------------------------- #
def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float, max_iter: int = 300, eps: float = 1e-14) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1e-300 if abs(d) < 1e-300 else d
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1e-300 if abs(d) < 1e-300 else d
        c = 1.0 + aa / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(-_log_beta(a, b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(a: float, b: float, p: float, tol: float = 1e-12) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def clopper_pearson_interval(numerator: int, denominator: int, confidence_level: float) -> tuple[float, float]:
    if denominator <= 0:
        return (0.0, 1.0)
    alpha = 1.0 - confidence_level
    lower = 0.0 if numerator == 0 else _beta_quantile(numerator, denominator - numerator + 1, alpha / 2.0)
    upper = (
        1.0 if numerator == denominator else _beta_quantile(numerator + 1, denominator - numerator, 1.0 - alpha / 2.0)
    )
    return lower, upper


def _wilson_score_interval_one_sided(numerator: int, denominator: int, confidence_level: float) -> tuple[float, float]:
    if denominator <= 0:
        return (0.0, 1.0)
    z = statistics.NormalDist().inv_cdf(confidence_level)
    n, x = float(denominator), float(numerator)
    p = x / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (x + z2 / 2.0) / (n + z2)
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    return max(0.0, center - half), min(1.0, center + half)


def newcombe_risk_difference_one_sided_upper(
    numerator_after: int,
    denominator_after: int,
    numerator_before: int,
    denominator_before: int,
    confidence_level: float,
) -> tuple[float, float]:
    p_after = numerator_after / denominator_after
    p_before = numerator_before / denominator_before
    _, upper_after = _wilson_score_interval_one_sided(numerator_after, denominator_after, confidence_level)
    lower_before, _ = _wilson_score_interval_one_sided(numerator_before, denominator_before, confidence_level)
    point = p_after - p_before
    return point, point + math.sqrt((upper_after - p_after) ** 2 + (p_before - lower_before) ** 2)


def evaluate_non_inferiority(
    before: dict[str, Any], after: dict[str, Any], required_sample_count_per_arm: int
) -> dict[str, Any]:
    inconclusive = {"outcome": "inconclusive", "point_estimate": None, "ci_upper": None}
    if before["denominator"] < required_sample_count_per_arm or after["denominator"] < required_sample_count_per_arm:
        return inconclusive
    if before["denominator"] != after["denominator"]:
        return inconclusive
    point, ci_upper = newcombe_risk_difference_one_sided_upper(
        after["numerator"], after["denominator"], before["numerator"], before["denominator"], 0.95
    )
    return {
        "outcome": "non_inferior" if ci_upper <= 0.20 else "inferior",
        "point_estimate": point,
        "ci_upper": ci_upper,
    }


# --------------------------------------------------------------------------- #
# Assessment building (AC3/AC4).
# --------------------------------------------------------------------------- #
def _classification_for_metric(metric: str, run_data: dict[str, Any]) -> str:
    if metric == "workflow_failure_rate":
        return run_data["classification_workflow"]
    flagged = run_data["has_flaky"] if metric == "playwright_flaky_test_rate" else run_data["has_unexpected"]
    return "affected" if flagged else "not_affected"


def build_reliability_metrics_block(envelope: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    metrics: dict[str, dict[str, dict[str, Any]]] = {"before": {}, "after": {}}
    for arm in ARMS:
        for metric in METRICS:
            runs = envelope["runs_by_arm"][arm]
            numerator = sum(
                1
                for run_data in runs.values()
                if _classification_for_metric(metric, run_data) in ("failure", "affected")
            )
            denominator = len(runs)
            metrics[arm][metric] = {
                "numerator": numerator,
                "denominator": denominator,
                "rate": (numerator / denominator) if denominator else 0.0,
            }
    return metrics


def build_sample_provenance(envelope: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    provenance: dict[str, dict[str, dict[str, Any]]] = {"before": {}, "after": {}}
    for arm in ARMS:
        runs = envelope["runs_by_arm"][arm]
        for metric in METRICS:
            observations = [
                {
                    "workflow_run_id": run_id,
                    "arm": arm,
                    "run_attempt": 1,
                    "classification": _classification_for_metric(metric, run_data),
                }
                for run_id, run_data in sorted(runs.items())
            ]
            provenance[arm][metric] = {"design_id": POWER_DESIGN_ID, "observations": observations}
    return provenance


def build_workflow_records(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for arm in ARMS:
        for run_id, run_data in sorted(envelope["runs_by_arm"][arm].items()):
            records.append(
                {"workflow_run_id": run_id, "arm": arm, "run_attempt": 1, "conclusion": run_data["conclusion"]}
            )
    return records


def build_playwright_test_cases(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    cases = []
    for arm in ARMS:
        for run_id, run_data in sorted(envelope["runs_by_arm"][arm].items()):
            for case in run_data["test_cases"]:
                cases.append(
                    {
                        "test_id": case["test_id"],
                        "workflow_run_id": run_id,
                        "arm": arm,
                        "run_attempt": 1,
                        "outcome": case["outcome"],
                    }
                )
    return cases


def build_assessment(
    envelope: dict[str, Any],
    target_metric: str,
    issue_number: int,
    pr_number: int | None,
    measured_at: str | None = None,
) -> dict[str, Any]:
    reliability_metrics = build_reliability_metrics_block(envelope)
    sample_provenance = build_sample_provenance(envelope)
    workflow_records = build_workflow_records(envelope)
    playwright_test_cases = build_playwright_test_cases(envelope)

    before_fields = reliability_metrics["before"][target_metric]
    after_fields = reliability_metrics["after"][target_metric]
    before_ci = clopper_pearson_interval(before_fields["numerator"], before_fields["denominator"], 0.95)
    after_ci = clopper_pearson_interval(after_fields["numerator"], after_fields["denominator"], 0.95)
    evaluation = evaluate_non_inferiority(before_fields, after_fields, REQUIRED_SAMPLE_COUNT_PER_ARM)

    assessment = {
        "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1",
        "schema_version": 1,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "target_metric": target_metric,
        "reliability_metrics": reliability_metrics,
        "sample_identity": {"key": "workflow_run_id", "required_run_attempt": 1},
        "confidence_level": 0.95,
        "non_inferiority_margin": 0.2,
        "sample_count_rule": {
            "design_id": POWER_DESIGN_ID,
            "required_sample_count_per_arm": REQUIRED_SAMPLE_COUNT_PER_ARM,
        },
        "non_inferiority_evaluation": {
            "metric": target_metric,
            "effect_measure": "risk_difference",
            "method": "newcombe_wilson_hybrid_mover_v1",
            "sidedness": "one_sided",
            "before": {
                "numerator": before_fields["numerator"],
                "denominator": before_fields["denominator"],
                "ci_lower": before_ci[0],
                "ci_upper": before_ci[1],
            },
            "after": {
                "numerator": after_fields["numerator"],
                "denominator": after_fields["denominator"],
                "ci_lower": after_ci[0],
                "ci_upper": after_ci[1],
            },
            "risk_difference": {
                "method": "newcombe_wilson_hybrid_mover_v1",
                "point_estimate": evaluation["point_estimate"],
                "ci_upper": evaluation["ci_upper"],
            },
            "outcome": evaluation["outcome"],
        },
        "workflow_records": workflow_records,
        "playwright_test_cases": playwright_test_cases,
        "sample_provenance": sample_provenance,
        "raw_attempts": [],
    }
    if measured_at is not None:
        assessment["measured_at"] = measured_at
    return assessment


def build_all_assessments(
    envelope: dict[str, Any], issue_number: int, pr_number: int | None, measured_at: str | None = None
) -> dict[str, dict[str, Any]]:
    return {metric: build_assessment(envelope, metric, issue_number, pr_number, measured_at) for metric in METRICS}


# --------------------------------------------------------------------------- #
# Validator invocation (AC4) -- production route always shells out to the
# real, unmodified #2432/#2507 validator; exit 0 alone is never treated as
# aggregate PASS (see `aggregate_gate`).
# --------------------------------------------------------------------------- #
def run_validator(
    assessment_path: str, output_path: str, validator_path: str = DEFAULT_VALIDATOR_PATH
) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, validator_path, "--assessment", assessment_path, "--output", output_path],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    try:
        with open(output_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        return {
            "schema": "CI_TEST_RELIABILITY_ASSESSMENT_V1_VALIDATION_RESULT",
            "exit_code": result.returncode,
            "operational_error": f"validator_output_unreadable: {exc}",
            "stderr": result.stderr,
        }
    payload.setdefault("exit_code", result.returncode)
    return payload


# --------------------------------------------------------------------------- #
# Aggregate gate (AC5) -- the ONLY authority for PASS; validator exit 0 alone
# is never sufficient.
# --------------------------------------------------------------------------- #
def aggregate_gate(
    envelope: dict[str, Any] | None,
    envelope_diagnostic: dict[str, Any],
    assessments: dict[str, dict[str, Any]] | None,
    validator_results: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    errors: list[str] = list(envelope_diagnostic.get("errors", []))
    if envelope is None or assessments is None or validator_results is None:
        return {
            "schema": "CI_RELIABILITY_AGGREGATE_RESULT_V1",
            "complete": False,
            "semantic_valid": False,
            "sample_satisfied": False,
            "all_non_inferior": False,
            "rerun_detected": bool(envelope_diagnostic.get("rerun_run_ids")),
            "exit_code": 1,
            "errors": errors or ["envelope_unavailable"],
        }

    rerun_detected = bool(envelope.get("rerun_run_ids"))

    structural_semantic_ok = True
    for metric, result in validator_results.items():
        if result.get("exit_code") != 0 or not result.get("semantic_valid", False) or not result.get(
            "structural_valid", False
        ):
            structural_semantic_ok = False
            errors.append(f"validator_not_valid: metric={metric} exit_code={result.get('exit_code')}")

    sample_satisfied = True
    all_non_inferior = True
    for metric, assessment in assessments.items():
        evaluation = assessment["non_inferiority_evaluation"]
        before_denominator = evaluation["before"]["denominator"]
        after_denominator = evaluation["after"]["denominator"]
        if (
            before_denominator < REQUIRED_SAMPLE_COUNT_PER_ARM
            or after_denominator < REQUIRED_SAMPLE_COUNT_PER_ARM
            or before_denominator != after_denominator
        ):
            sample_satisfied = False
        if evaluation["outcome"] != "non_inferior":
            all_non_inferior = False

    if rerun_detected:
        errors.append(f"rerun_detected_close_grade_ineligible: workflow_run_ids={envelope['rerun_run_ids']}")

    complete = structural_semantic_ok and sample_satisfied and all_non_inferior and not rerun_detected and not errors

    if complete:
        exit_code = 0
    elif rerun_detected:
        exit_code = 2
    elif not structural_semantic_ok:
        exit_code = 3
    elif not sample_satisfied:
        exit_code = 4
    elif not all_non_inferior:
        exit_code = 5
    else:
        exit_code = 1

    return {
        "schema": "CI_RELIABILITY_AGGREGATE_RESULT_V1",
        "complete": complete,
        "semantic_valid": structural_semantic_ok,
        "sample_satisfied": sample_satisfied,
        "all_non_inferior": all_non_inferior,
        "rerun_detected": rerun_detected,
        "exit_code": exit_code,
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# Canonical single output (AC11).
# --------------------------------------------------------------------------- #
def build_canonical_output(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    envelope: dict[str, Any] | None,
    envelope_diagnostic: dict[str, Any],
    assessments: dict[str, dict[str, Any]] | None,
    validator_results: dict[str, dict[str, Any]] | None,
    aggregate: dict[str, Any],
    invocation_artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest_digest = sha256_of_canonical_json(manifest)
    composite_envelope_digest = sha256_of_canonical_json(envelope) if envelope is not None else None
    assessment_digests = (
        {metric: sha256_of_canonical_json(assessment) for metric, assessment in assessments.items()}
        if assessments is not None
        else {}
    )

    canonical: dict[str, Any] = {
        "schema": "CI_RELIABILITY_CLOSE_GRADE_RESULT_V1",
        "schema_version": 1,
        "experiment_identity": manifest.get("experiment_identity"),
        "manifest_digest": manifest_digest,
        "canonical_workflow_run_ids": {
            "monolith": sorted(manifest_run_ids_for_layout(manifest, "monolith")),
            "split": sorted(manifest_run_ids_for_layout(manifest, "split")),
        },
        "receipt_run_set_digest": receipt.get("run_set_digest"),
        "assessment_content_digests": assessment_digests,
        "validator_results": validator_results or {},
        "composite_envelope_digest": composite_envelope_digest,
        "invocation_artifacts": invocation_artifacts or [],
        "aggregate": {
            "complete": aggregate["complete"],
            "semantic_valid": aggregate["semantic_valid"],
            "sample_satisfied": aggregate["sample_satisfied"],
            "all_non_inferior": aggregate["all_non_inferior"],
            "exit_code": aggregate["exit_code"],
        },
        "diagnostic_errors": envelope_diagnostic.get("errors", []),
    }
    canonical["canonical_output_digest"] = sha256_of_canonical_json(canonical)
    return canonical


# --------------------------------------------------------------------------- #
# Invocation artifact provenance (AC11/Finding 5) -- optional: populated only
# when the caller supplies an artifact index (GitHub Actions Artifacts REST
# API metadata keyed by artifact name), never fabricated.
# --------------------------------------------------------------------------- #
# Issue #2424 Finding 1/2/5 fix_delta: the GitHub Actions artifact NAME is
# not always identical to the logical `invocation_id` -- monolith's
# `e2e-responsive` invocation uploads under the `e2e-core-responsive`
# artifact-name suffix (`.github/workflows/ci.yml`'s own upload step
# comment) to stay collision-free against split's `e2e-responsive-matrix`
# job artifact of the SAME invocation_id (statically enforced by
# `tests/ci/test_verify_e2e_lane_partition.py`, Allowed Paths外). Mirrors
# ci.yml's OWN `ARTIFACT_NAME_SUFFIX_BY_LAYOUT_AND_INVOCATION` table
# exactly (never re-derived from `invocation_id` alone); an
# `(layout, invocation_id)` pair not present here falls back to
# `invocation_id` itself (the default 1:1 pattern for any future
# invocation this table has not been extended for yet).
ARTIFACT_NAME_SUFFIX_BY_LAYOUT_AND_INVOCATION = {
    ("monolith", "e2e-core"): "e2e-core",
    ("monolith", "e2e-responsive"): "e2e-core-responsive",
    ("split", "e2e-core"): "e2e-core",
    ("split", "e2e-responsive"): "e2e-responsive",
}


def build_invocation_artifacts(
    envelope: dict[str, Any] | None,
    manifest: dict[str, Any],
    artifact_index: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Issue #2424 Finding 5 fix_delta: for each (layout, workflow_run_id,
    invocation_id) triple actually included in `envelope`, records the
    per-invocation GitHub Actions artifact this evidence was downloaded
    from -- `artifact_id`/`digest` come from `artifact_index` (built by the
    CI producer via the GitHub Actions Artifacts REST API, e.g. `gh api
    repos/{repo}/actions/artifacts`), matched by the SAME
    `ci-reliability-<run_id>-a1-<invocation_id>` artifact-naming contract
    AC2 defines. Never fabricates an entry for evidence this run did not
    actually download."""
    if envelope is None or not artifact_index:
        return []
    invocations = manifest.get("frozen_non_treatment", {}).get("expected_playwright_invocations", [])
    invocation_ids = [inv["invocation_id"] for inv in invocations]
    entries: list[dict[str, Any]] = []
    for arm, runs in envelope.get("runs_by_arm", {}).items():
        layout = ARM_TO_LAYOUT[arm]
        for run_id in sorted(runs):
            for invocation_id in invocation_ids:
                suffix = ARTIFACT_NAME_SUFFIX_BY_LAYOUT_AND_INVOCATION.get((layout, invocation_id), invocation_id)
                artifact_name = f"ci-reliability-{run_id}-a1-{suffix}"
                meta = artifact_index.get(artifact_name)
                if meta is None:
                    continue
                entries.append(
                    {
                        "layout": layout,
                        "workflow_run_id": run_id,
                        "invocation_id": invocation_id,
                        "artifact_name": artifact_name,
                        "artifact_id": meta.get("id"),
                        "digest": meta.get("digest"),
                    }
                )
    return entries


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default_playwright_json_loader(playwright_json_dir: str):
    """Issue #2424 Finding 2 fix_delta: layout is `<dir>/<layout>/
    <workflow_run_id>/<invocation_id>.json` -- resolves at minimum
    `run_id x invocation_id` uniquely (attempt is separately hard-bound to
    `1` by `workflow_evidence_not_attempt_1` in `build_composite_envelope`,
    so a further `<attempt>` path segment would be redundant, never a
    second run silently colliding with the first)."""

    def loader(layout: str, workflow_run_id: int, invocation_id: str) -> dict[str, Any] | None:
        path = os.path.join(playwright_json_dir, layout, str(workflow_run_id), f"{invocation_id}.json")
        if not os.path.isfile(path):
            return None
        try:
            return load_json_file(path)
        except EnvelopeBuildError:
            return None

    return loader


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CI_TEST_RELIABILITY_ASSESSMENT_V1 production evidence (#2424)")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--workflow-evidence", required=True)
    parser.add_argument("--playwright-json-dir", required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--validator-path", default=DEFAULT_VALIDATOR_PATH)
    parser.add_argument(
        "--artifact-index",
        default=None,
        help=(
            "Issue #2424 Finding 5: optional path to a JSON object mapping "
            "GitHub Actions artifact name -> {'id': int, 'digest': "
            "'sha256:<hex>'} (from the GitHub Actions Artifacts REST API), "
            "used to populate the canonical output's `invocation_artifacts` "
            "provenance. Omitted -> `invocation_artifacts` stays empty "
            "(never fabricated)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.makedirs(args.output_dir, exist_ok=True)

    manifest_raw_bytes: bytes | None = None
    try:
        with open(args.manifest, "rb") as handle:
            manifest_raw_bytes = handle.read()
        manifest = strict_json_loads(manifest_raw_bytes.decode("utf-8"))
    except (OSError, StrictJSONError) as exc:
        manifest = None
        prereq_errors = [f"file_not_readable_or_invalid_json: {args.manifest}: {exc}"]
    else:
        prereq_errors = []
    try:
        receipt = load_json_file(args.receipt)
    except EnvelopeBuildError as exc:
        receipt = None
        prereq_errors.append(str(exc))

    prereq_errors.extend(verify_prerequisites_available(manifest, receipt, manifest_raw_bytes))
    if prereq_errors:
        diagnostic = {
            "schema": "CI_RELIABILITY_COMPOSITE_ENVELOPE_DIAGNOSTIC_V1",
            "errors": prereq_errors,
            "runs": [],
            "rerun_run_ids": [],
        }
        _write_json_file(os.path.join(args.output_dir, "composite_envelope_diagnostic.json"), diagnostic)
        aggregate = aggregate_gate(None, diagnostic, None, None)
        canonical = build_canonical_output(manifest or {}, receipt or {}, None, diagnostic, None, None, aggregate)
        _write_json_file(os.path.join(args.output_dir, "ci_reliability_close_grade_result_v1.json"), canonical)
        return aggregate["exit_code"]

    workflow_evidence = load_json_file(args.workflow_evidence)
    loader = _default_playwright_json_loader(args.playwright_json_dir)
    envelope, diagnostic = build_composite_envelope(manifest, receipt, workflow_evidence, loader)
    _write_json_file(os.path.join(args.output_dir, "composite_envelope_diagnostic.json"), diagnostic)
    # Issue #2424 Finding 5 fix_delta: the composite envelope BODY (not just
    # its digest, already embedded in the canonical output) is itself
    # published so a downstream reader can inspect the raw binding this
    # experiment's assessments were computed from, without needing to
    # re-derive it.
    if envelope is not None:
        _write_json_file(os.path.join(args.output_dir, "composite_envelope.json"), envelope)

    assessments = None
    validator_results = None
    if envelope is not None:
        assessments = build_all_assessments(envelope, args.issue_number, args.pr_number)
        validator_results = {}
        for metric, assessment in assessments.items():
            assessment_path = os.path.join(args.output_dir, f"assessment_{metric}.json")
            _write_json_file(assessment_path, assessment)
            validator_output_path = os.path.join(args.output_dir, f"validator_result_{metric}.json")
            validator_results[metric] = run_validator(assessment_path, validator_output_path, args.validator_path)

    aggregate = aggregate_gate(envelope, diagnostic, assessments, validator_results)
    _write_json_file(os.path.join(args.output_dir, "aggregate_result.json"), aggregate)

    artifact_index = None
    if args.artifact_index:
        try:
            artifact_index = load_json_file(args.artifact_index)
        except EnvelopeBuildError:
            artifact_index = None
    invocation_artifacts = build_invocation_artifacts(envelope, manifest, artifact_index)

    canonical = build_canonical_output(
        manifest, receipt, envelope, diagnostic, assessments, validator_results, aggregate, invocation_artifacts
    )
    _write_json_file(os.path.join(args.output_dir, "ci_reliability_close_grade_result_v1.json"), canonical)

    return aggregate["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
