#!/usr/bin/env python3
"""validate_retrospective_schema.py -- plugin-local validator for
``agent_improvement_candidate/v1`` (Issue #2240, agent-retrospective plugin
distribution).

This is a deliberately trimmed port of the host repository's own project
Skill implementation of this same validator (that sibling module is
unmodified -- Out of Scope). Only the functions the plugin's
``run_retrospective.py`` call graph actually needs are ported:

- ``compute_finding_identity`` / ``FINDING_IDENTITY_ALGORITHM`` (finding
  identity -- Issue #2288's ``FINDING_IDENTITY_V1``)
- ``validate_candidate`` (+ its ``finding_contract`` sub-checks) -- the
  canonical ``agent_improvement_candidate/v1`` validator used by
  ``run_retrospective.py``'s wire-contract candidate validation
- ``compute_source_set_digest`` -- the ``SourcePlan.source_set_digest``
  digest

The plugin does not need ``validate_run``/``agent_retrospective_run_v1``
(persistence -- Child 5 / #2238's responsibility, out of this plugin's
scope) or ``validate_transition``/``load_fixture``/Latitude helpers, so
those are intentionally not ported here.

Path resolution note (Issue #2240 AC2): schema files live in this script's
own sibling ``schemas/`` directory (``skills/run/schemas/``), resolved via
two chained ``.parent`` accesses on ``Path(__file__).resolve()`` -- never
the multi-level-index repo-root-guessing walk this plugin's ``scripts/``
directory deliberately avoids, and never a loop-protocol-specific
project-relative path rooted under the host repository's own
dotfolder-based Skill layout.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

# ---------------------------------------------------------------------------
# format checking (date-time) -- mirrors the project Skill's own rationale:
# jsonschema does not check "format" unless an explicit FormatChecker is
# supplied, and this repo's jsonschema dependency does not include the
# optional format-validation extra.
# ---------------------------------------------------------------------------

_FORMAT_CHECKER = jsonschema.FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_date_time_stdlib(value: object) -> bool:
    if not isinstance(value, str):
        return True
    datetime.fromisoformat(value)
    return True


CANDIDATE_SCHEMA_PATH = _SCHEMAS_DIR / "agent_improvement_candidate_v1.schema.json"

FINDING_IDENTITY_ALGORITHM = "sha256-jcs-v1"

#: mirrors the project Skill's own closed set -- an unsupported algorithm is
#: rejected fail-closed rather than silently hashed under an unspecified
#: scheme.
SUPPORTED_FINDING_IDENTITY_ALGORITHMS: frozenset[str] = frozenset({FINDING_IDENTITY_ALGORITHM})


class RetrospectiveSchemaError(ValueError):
    """Raised for schema validation and digest/identity-consistency failures."""


def _reject_non_finite(value: Any) -> None:
    """Recursively reject NaN/Infinity float values anywhere within `value`
    (RFC 8785 JCS has no representation for non-finite IEEE 754 values)."""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RetrospectiveSchemaError(
            f"finding identity key contains a non-finite float value ({value!r}); "
            "NaN/Infinity are not permitted in a FINDING_IDENTITY_V1 key."
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _jcs_canonicalize(value: Any) -> Any:
    """Recursively canonicalize `value` per RFC 8785 JCS (object-property
    sort only -- array order is preserved unchanged)."""
    if isinstance(value, dict):
        return {key: _jcs_canonicalize(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_jcs_canonicalize(item) for item in value]
    return value


def compute_finding_identity(identity_key: dict[str, Any], algorithm: str = FINDING_IDENTITY_ALGORITHM) -> str:
    """``value = "sha256:" + SHA256(JCS(identity_key))``. See the project
    Skill's ``validate_retrospective_schema.py`` for the full rationale
    (identical algorithm, ported verbatim)."""
    if algorithm not in SUPPORTED_FINDING_IDENTITY_ALGORITHMS:
        raise RetrospectiveSchemaError(
            f"unsupported finding identity algorithm: {algorithm!r}. Supported: "
            f"{sorted(SUPPORTED_FINDING_IDENTITY_ALGORITHMS)!r}."
        )
    _reject_non_finite(identity_key)
    canonical_key = _jcs_canonicalize(identity_key)
    preimage = json.dumps(
        canonical_key, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_finding_identity(candidate: dict[str, Any]) -> None:
    """Recompute and cross-check `candidate["finding_contract"]["identity"]`.
    No-op when `finding_contract` is absent (legacy candidate)."""
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    identity = finding_contract["identity"]
    algorithm = identity.get("algorithm", FINDING_IDENTITY_ALGORITHM)
    expected_value = compute_finding_identity(identity["key"], algorithm)
    actual_value = identity["value"]
    if expected_value != actual_value:
        raise RetrospectiveSchemaError(
            "finding_contract.identity.value mismatch: stored value="
            f"{actual_value!r} but compute_finding_identity(identity.key)="
            f"{expected_value!r}."
        )


def validate_claim_class_consistency(candidate: dict[str, Any]) -> None:
    """Cross-check `finding_contract.claim_class` against
    `identity.key.claim_class`. No-op when `finding_contract` is absent."""
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    envelope_claim_class = finding_contract["claim_class"]
    key_claim_class = finding_contract["identity"]["key"]["claim_class"]
    if envelope_claim_class != key_claim_class:
        raise RetrospectiveSchemaError(
            f"finding_contract.claim_class ({envelope_claim_class!r}) does not match "
            f"finding_contract.identity.key.claim_class ({key_claim_class!r})."
        )


#: `presence_delta` values that assert the finding is currently present/absent.
_PRESENT_STATES: frozenset[str] = frozenset({"new", "active", "recurrent"})
_ABSENT_STATES: frozenset[str] = frozenset({"resolved", "still_absent"})


def validate_finding_contract_history(candidate: dict[str, Any]) -> None:
    """Validate `finding_contract.evaluations[]` structural and
    judgement-table self-consistency (Issue #2288 P0-2/P0-3 invariants,
    ported verbatim from the project Skill's own validator)."""
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    evaluations = finding_contract["evaluations"]
    seen_ids: set[str] = set()
    last_classified_presence: str | None = None

    for index, evaluation in enumerate(evaluations):
        evaluation_id = evaluation["evaluation_id"]
        if evaluation_id in seen_ids:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}].evaluation_id={evaluation_id!r} "
                "duplicates an earlier evaluation_id in the same history."
            )

        previous_ref = evaluation.get("previous_evaluation_ref")
        if index == 0:
            if previous_ref is not None:
                raise RetrospectiveSchemaError(
                    f"finding_contract.evaluations[{index}].previous_evaluation_ref "
                    "is non-null but this is the first evaluation in the history."
                )
        else:
            expected_previous = evaluations[index - 1]["evaluation_id"]
            if previous_ref != expected_previous:
                raise RetrospectiveSchemaError(
                    f"finding_contract.evaluations[{index}].previous_evaluation_ref="
                    f"{previous_ref!r} does not equal the immediately preceding "
                    f"evaluation's evaluation_id ({expected_previous!r})."
                )
        seen_ids.add(evaluation_id)

        observed = evaluation["observed"]
        presence_delta = evaluation["presence_delta"]
        evaluation_status = evaluation["evaluation_status"]
        source_coverage = evaluation["source_coverage"]

        if observed and presence_delta in _ABSENT_STATES:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: observed=true but "
                f"presence_delta={presence_delta!r} asserts absence."
            )
        if not observed and presence_delta in _PRESENT_STATES:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: observed=false but "
                f"presence_delta={presence_delta!r} asserts presence."
            )

        if evaluation_status == "indeterminate" and presence_delta in {"resolved", "recurrent"}:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: evaluation_status='indeterminate' "
                f"cannot assert presence_delta={presence_delta!r}."
            )

        if presence_delta == "resolved" and source_coverage != "complete":
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: presence_delta='resolved' "
                f"requires source_coverage == 'complete' (was {source_coverage!r})."
            )

        if evaluation_status == "classified":
            if last_classified_presence is None:
                if presence_delta not in {"new", "still_absent"}:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: the first classified "
                        f"evaluation must have presence_delta 'new' or 'still_absent' "
                        f"(was {presence_delta!r})."
                    )
            else:
                if presence_delta == "new":
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta='new' is "
                        "only valid for the first classified evaluation."
                    )
                if presence_delta == "recurrent" and last_classified_presence not in {"resolved", "still_absent"}:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta='recurrent' "
                        "requires the immediately preceding classified evaluation's "
                        f"presence_delta to be 'resolved' or 'still_absent' "
                        f"(was {last_classified_presence!r})."
                    )
                if presence_delta == "resolved" and last_classified_presence not in {"new", "active", "recurrent"}:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta='resolved' "
                        "requires the immediately preceding classified evaluation's "
                        f"presence_delta to be 'new', 'active', or 'recurrent' "
                        f"(was {last_classified_presence!r})."
                    )
            last_classified_presence = presence_delta


def validate_signal_specs_consistency(candidate: dict[str, Any]) -> None:
    """Cross-check baseline/current/expected signal specs within each
    evaluation (Issue #2288 P0-4)."""
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    for index, evaluation in enumerate(finding_contract["evaluations"]):
        signals = {name: evaluation.get(name) for name in ("baseline_signal", "current_signal", "expected_signal")}
        present = {name: signal for name, signal in signals.items() if signal is not None}
        if len(present) < 2:
            continue
        reference_name, reference_signal = next(iter(present.items()))
        for field_name in ("signal_type", "unit", "comparator", "worse_direction"):
            reference_value = reference_signal.get(field_name)
            for name, signal in present.items():
                if name == reference_name:
                    continue
                if signal.get(field_name) != reference_value:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: {name}.{field_name}="
                        f"{signal.get(field_name)!r} does not match "
                        f"{reference_name}.{field_name}={reference_value!r}."
                    )


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_candidate_schema() -> dict[str, Any]:
    return _load_schema(CANDIDATE_SCHEMA_PATH)


def _validate_with_format_checking(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=_FORMAT_CHECKER)
    validator.validate(instance)


def validate_candidate(instance: dict[str, Any]) -> None:
    """Validate an agent_improvement_candidate/v1 instance. Raises
    jsonschema.exceptions.ValidationError on schema/format failure, or
    RetrospectiveSchemaError on any finding_contract self-consistency
    violation (see the individual `validate_*` helpers above)."""
    _validate_with_format_checking(instance, load_candidate_schema())
    validate_finding_identity(instance)
    validate_claim_class_consistency(instance)
    validate_finding_contract_history(instance)
    validate_signal_specs_consistency(instance)


def is_valid_candidate(instance: dict[str, Any]) -> bool:
    try:
        validate_candidate(instance)
    except (jsonschema.exceptions.ValidationError, RetrospectiveSchemaError):
        return False
    return True


def compute_source_set_digest(source_observations: list[dict[str, Any]]) -> str:
    """Deterministic sha256 hex digest over source_observations (idempotency
    use only -- see the project Skill's docstring for the full rationale).
    Ported verbatim."""

    def _sort_key(observation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(observation.get("source_id", "")),
            str(observation.get("endpoint", "")),
            str(observation.get("fetch_started_at", "")),
        )

    ordered = sorted(source_observations, key=_sort_key)
    canonical = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
