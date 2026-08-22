#!/usr/bin/env python3
"""Validator for agent_retrospective_run/v1 and agent_improvement_candidate/v1.

This module intentionally stays a pure, static validation library:

- JSON Schema validation for both schemas (``validate_run`` / ``validate_candidate``),
  including ``format`` (e.g. ``date-time``) checking via an explicit,
  module-local ``jsonschema.FormatChecker`` instance with a stdlib-only "date-time"
  checker registered on it (see ``_FORMAT_CHECKER`` / ``_check_date_time_stdlib``
  below) -- ``jsonschema.validate()`` does NOT check ``format`` by default, and
  jsonschema's own built-in "date-time" checker only activates when an optional
  format-validation dependency (e.g. rfc3339-validator) is installed, which this
  repo's ``jsonschema`` dependency intentionally does not include (see
  ``scripts/ci/tests/test_runtime_dependency_smoke.py`` and
  ``.claude/skills/issue-refinement-loop/tests/test_issue_execution_decision_schema_parity.py``,
  which pin the exact no-extras dependency specifier and document that gap repo-wide,
  respectively). Registering the checker on a locally-scoped ``FormatChecker``
  instance (rather than adding the dependency) enforces date-time format for this
  module only, without perturbing those other files' contracts.
- Digest consistency: ``validate_run`` recomputes ``compute_source_set_digest()`` over
  the instance's ``source_observations`` and rejects the instance if it does not match
  the stored ``run_identity.source_set_digest`` -- callers cannot supply an arbitrary,
  stale, or fabricated digest and have it pass validation.
- A ``candidate_status`` state-transition validator (``validate_transition`` /
  ``ALLOWED_TRANSITIONS``) that enforces the closed enum's *reachability graph*, which
  the JSON Schema enum alone cannot express (e.g. ``rejected`` and ``implemented`` are
  each individually valid enum values, but the direct transition
  ``rejected -> implemented`` must be rejected). ``rejected`` and ``superseded`` are
  terminal states reachable from every non-terminal state, matching the schema
  description's normative claim.
- ``compute_source_set_digest()``: a deterministic sha256 digest over
  ``source_observations``, intended strictly for *idempotency* (duplicate-suppression)
  use per docs/adr/0007-agent-retrospective-boundaries.md Decision 5 -- this digest is
  NOT an optimistic-concurrency / stale-write-protection token (that mechanism,
  ``expected_previous_digest`` / ``version``, is out of scope for this Issue; see
  Child 5 / #2238). The digest sorts ``source_observations`` by a stable key
  (``source_id``, ``endpoint``, ``fetch_started_at``) before canonicalizing, so the
  digest is independent of both dict key order AND array (collection) order -- two
  concurrent adapters (Child 3 / #2236) racing to append their observation in different
  orders must not change the digest of an otherwise-identical source set.

Migration note (ADR 0007 Decision 7): the existing ``agent_retro_index/v1``
(``docs/dev/agent-retro-index.md``) remains a *derived index* only. It does not hold
run/candidate state and this module does not read from or write to it; run/candidate
state is owned exclusively by the two schemas validated here.

Finding identity / delta-evaluation contract (Issue #2288): this module additionally
provides ``compute_finding_identity()`` / ``validate_finding_identity()`` for the
optional ``finding_contract`` envelope on ``agent_improvement_candidate/v1``. Per the
Issue #2288 Out of Scope list, this module does NOT implement the actual delta
*computation* (comparing a prior run's evaluation to the current run to derive
``presence_delta``/``signal_delta``/``delta_status`` -- that is Issue #2237's
responsibility); ``validate_finding_contract_history()`` here only checks the
*representation* of an already-produced ``evaluations`` list for self-consistency
(structural chain integrity plus the fixed judgement-table invariants that are not
otherwise expressible in JSON Schema -- see that function's docstring for the exact
rule set). ``compute_delta_capability()`` is a pure, non-mutating classifier used for
the legacy-record migration story: it never adds, backfills, or auto-generates a
``finding_contract``/identity for a legacy record.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"

# ---------------------------------------------------------------------------
# format checking (date-time)
# ---------------------------------------------------------------------------
#
# jsonschema does not check "format" unless an explicit FormatChecker is supplied,
# and jsonschema's own built-in "date-time" checker only activates when an optional
# format-validation dependency (e.g. rfc3339-validator, installed via the
# jsonschema[format] extra) is present -- this repo's jsonschema dependency does not
# include that extra (scripts/ci/tests/test_runtime_dependency_smoke.py pins the exact
# `jsonschema>=4.0` specifier with no extras, and
# .claude/skills/issue-refinement-loop/tests/test_issue_execution_decision_schema_parity.py
# documents that "date-time" is intentionally absent from the default FormatChecker's
# registered checkers repo-wide). Rather than adding a new dependency and the
# repo-wide follow-up that test explicitly calls for, this module registers its own
# minimal, stdlib-only "date-time" checker on a locally-scoped FormatChecker instance
# (this does not touch jsonschema's global/default FormatChecker used elsewhere).

_FORMAT_CHECKER = jsonschema.FormatChecker()


@_FORMAT_CHECKER.checks("date-time", raises=ValueError)
def _check_date_time_stdlib(value: object) -> bool:
    """Reject strings that are not a valid ISO 8601 date-time (stdlib-only).

    ``datetime.fromisoformat`` (Python 3.11+) natively accepts the ``Z`` UTC
    suffix, so no manual normalization is needed. Non-string values are left to
    jsonschema's ``type`` keyword to reject; format checkers only apply to values
    that already match the declared type.
    """
    if not isinstance(value, str):
        return True
    datetime.fromisoformat(value)
    return True


RUN_SCHEMA_PATH = _SCHEMAS_DIR / "agent_retrospective_run_v1.schema.json"
CANDIDATE_SCHEMA_PATH = _SCHEMAS_DIR / "agent_improvement_candidate_v1.schema.json"

# ---------------------------------------------------------------------------
# candidate_status state machine
# ---------------------------------------------------------------------------
#
# Directed edges represent the only permitted direct transitions. `superseded` and
# `rejected` are terminal states reachable from any non-terminal state (a candidate can
# be abandoned/superseded/rejected at any point prior to reaching a terminal state), but
# transitions must still go through this table -- an unknown `from_status` /
# `to_status` is rejected.

_NON_TERMINAL_STATES: tuple[str, ...] = (
    "proposed",
    "accepted",
    "implementation_issue_created",
    "implemented",
    "validating",
    "validated",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected", "superseded"},
    "accepted": {"implementation_issue_created", "rejected", "superseded"},
    "implementation_issue_created": {"implemented", "rejected", "superseded"},
    "implemented": {"validating", "rejected", "superseded"},
    "validating": {"validated", "implemented", "rejected", "superseded"},
    "validated": {"rejected", "superseded"},
    "rejected": set(),
    "superseded": set(),
}

CANDIDATE_STATUSES = frozenset(ALLOWED_TRANSITIONS)

# Sanity check module-load time: every non-terminal state must be able to reach both
# terminal states directly, matching the docstring's normative claim.
assert all(
    {"rejected", "superseded"} <= ALLOWED_TRANSITIONS[state] for state in _NON_TERMINAL_STATES
)

# ---------------------------------------------------------------------------
# finding_contract (Issue #2288)
# ---------------------------------------------------------------------------

#: ADR 0007 Decision 2 claim-class taxonomy (exact 9-value closed enum). Mirrors
#: schemas/agent_improvement_candidate_v1.schema.json ``$defs.claim_class.enum``.
CLAIM_CLASSES: frozenset[str] = frozenset(
    {
        "code_content",
        "code_authorship_timing",
        "internal_loop_review_verdict",
        "github_native_review_state",
        "review_comment",
        "mergeability",
        "issue_intent",
        "external_fact",
        "runtime_behavior",
    }
)

FINDING_IDENTITY_ALGORITHM = "sha256-jcs-v1"

#: The only ``finding_contract.identity.algorithm`` values ``compute_finding_identity``/
#: ``validate_finding_identity`` will process. A caller-supplied algorithm outside this
#: set is rejected fail-closed rather than silently hashed under an unspecified scheme
#: (Issue #2288 P0-1: "未対応 algorithm は helper 側で fail-closed に reject される").
SUPPORTED_FINDING_IDENTITY_ALGORITHMS: frozenset[str] = frozenset({FINDING_IDENTITY_ALGORITHM})


def _reject_non_finite(value: Any) -> None:
    """Recursively reject NaN/Infinity float values anywhere within `value`.

    RFC 8785 JCS has no representation for non-finite IEEE 754 values; Python's
    ``json`` module (unlike the JSON spec) accepts ``NaN``/``Infinity``/``-Infinity``
    by default (``allow_nan=True``), so a caller-supplied identity key containing one
    of these would otherwise silently serialize to invalid JSON. Fail closed instead.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RetrospectiveSchemaError(
            f"finding identity key contains a non-finite float value ({value!r}); "
            "NaN/Infinity are not permitted in a FINDING_IDENTITY_V1 key (RFC 8785 "
            "JCS has no representation for non-finite values)."
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_non_finite(item)


def _jcs_canonicalize(value: Any) -> Any:
    """Recursively canonicalize `value` per RFC 8785 JCS (object-property-only sort).

    Object (dict) properties are sorted by key, recursively. Array (list) element
    order is preserved unchanged -- RFC 8785 JCS does NOT reorder arrays; only object
    member ordering is normalized. `FINDING_IDENTITY_V1.key` has no array-typed
    fields today, so this has no observable effect on the current schema, but the
    function itself must not silently reorder arrays if one is ever added (an earlier,
    non-normative implementation of this helper treated arrays as unordered sets --
    that behavior was a deliberate deviation from RFC 8785 and has been removed; see
    Issue #2288 P0-1).
    """
    if isinstance(value, dict):
        return {key: _jcs_canonicalize(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_jcs_canonicalize(item) for item in value]
    return value


def compute_finding_identity(
    identity_key: dict[str, Any], algorithm: str = FINDING_IDENTITY_ALGORITHM
) -> str:
    """Deterministically compute a FINDING_IDENTITY_V1 ``value`` from a typed key.

    ``value = "sha256:" + SHA256(JCS(identity_key))`` -- the hash preimage is the
    RFC 8785 JCS-canonicalized `identity_key` alone. `algorithm` is NOT part of the
    hashed preimage (it is instead the envelope-level namespace identifier stored
    separately as `finding_contract.identity.algorithm`); this function still takes
    `algorithm` as a parameter purely to validate it against
    `SUPPORTED_FINDING_IDENTITY_ALGORITHMS` and raise fail-closed for any value this
    module does not know how to process (a future `sha256-jcs-v2` or similar would
    need its own explicit support added here, not a silent fallback).

    `identity_key` is expected to be limited to run/occurrence-independent fields
    (e.g. repository_id/claim_class/subject_ref/rule_id per FINDING_IDENTITY_V1) --
    this function does not itself filter which keys are "allowed"; callers (and the
    JSON Schema `$defs.finding_identity.key`) are responsible for keeping
    run-varying fields (source_run_ref, base_sha, source_set_digest, timestamps,
    evidence fingerprints) out of `identity_key`.

    Calling this twice with structurally-equal `identity_key` inputs (regardless of
    dict key order) always returns the same value. NaN/Infinity anywhere in
    `identity_key` are rejected fail-closed (see `_reject_non_finite`). Non-ASCII
    string values are serialized as UTF-8 (`ensure_ascii=False`), matching RFC 8785's
    requirement that JCS output be valid UTF-8 text rather than `\\uXXXX`-escaped
    ASCII.
    """
    if algorithm not in SUPPORTED_FINDING_IDENTITY_ALGORITHMS:
        raise RetrospectiveSchemaError(
            f"unsupported finding identity algorithm: {algorithm!r}. Supported: "
            f"{sorted(SUPPORTED_FINDING_IDENTITY_ALGORITHMS)!r}."
        )
    _reject_non_finite(identity_key)
    canonical_key = _jcs_canonicalize(identity_key)
    preimage = json.dumps(
        canonical_key,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_finding_identity(candidate: dict[str, Any]) -> None:
    """Recompute and cross-check `candidate["finding_contract"]["identity"]`.

    No-op when `finding_contract` is absent (legacy candidate). Raises
    RetrospectiveSchemaError if:

    - the stored `algorithm` is not in `SUPPORTED_FINDING_IDENTITY_ALGORITHMS`
      (fail-closed rejection of an unrecognized/future algorithm namespace), or
    - the stored `identity.value` does not match the value recomputed from
      `identity.key` via `compute_finding_identity()` -- callers cannot supply an
      arbitrary, stale/copied, or fabricated identity value and have it pass
      validation.
    """
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
            f"{expected_value!r}. finding_contract.identity.value is derived from "
            "identity.key and cannot be supplied, copied, or reused independently."
        )


def validate_claim_class_consistency(candidate: dict[str, Any]) -> None:
    """Cross-check `finding_contract.claim_class` against `identity.key.claim_class`.

    No-op when `finding_contract` is absent. These two fields are independently
    defined in the schema (the envelope-level "overall claim class of the finding"
    vs. one component of the typed identity key) and the schema alone cannot express
    that they must agree (Issue #2288 P0-5). A mismatch would let a candidate report
    one claim_class in its envelope while its stable cross-run identity is keyed on a
    *different* claim_class, silently corrupting identity stability.
    """
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    envelope_claim_class = finding_contract["claim_class"]
    key_claim_class = finding_contract["identity"]["key"]["claim_class"]
    if envelope_claim_class != key_claim_class:
        raise RetrospectiveSchemaError(
            "finding_contract.claim_class "
            f"({envelope_claim_class!r}) does not match "
            f"finding_contract.identity.key.claim_class ({key_claim_class!r}); "
            "these must be identical -- the envelope's claim_class and the identity "
            "key's claim_class component describe the same claim and must not diverge."
        )


#: `presence_delta` values that assert the finding is currently present.
_PRESENT_STATES: frozenset[str] = frozenset({"new", "active", "recurrent"})
#: `presence_delta` values that assert the finding is currently absent.
_ABSENT_STATES: frozenset[str] = frozenset({"resolved", "still_absent"})


def validate_finding_contract_history(candidate: dict[str, Any]) -> None:
    """Validate `finding_contract.evaluations[]` structural and judgement-table
    self-consistency.

    No-op when `finding_contract` is absent. This function checks *representation*
    consistency of an already-produced `evaluations` list -- it does NOT compute
    delta evaluations from a prior run (that computation is Issue #2237's
    responsibility; see module docstring). Specifically it enforces (Issue #2288
    P0-2/P0-3, none of which are expressible via JSON Schema alone because they
    depend on relationships *between* array elements and/or precise semantic
    interpretation of enum combinations):

    - `evaluation_id` is unique within the history (no duplicate IDs).
    - `previous_evaluation_ref` is `null` only for the first entry, and for every
      other entry equals the `evaluation_id` of the *immediately preceding* entry in
      the list (position i-1) -- forking or skipping ahead in the chain is rejected.
    - `observed` and `presence_delta` must agree: `observed=true` cannot coexist with
      an absence-asserting `presence_delta` (`resolved`/`still_absent`), and
      `observed=false` cannot coexist with a presence-asserting `presence_delta`
      (`new`/`active`/`recurrent`).
    - An `indeterminate` evaluation must never assert `presence_delta` `resolved` or
      `recurrent` (both are strong, evidence-backed conclusions that require
      `classified` status).
    - `presence_delta='resolved'` requires `source_coverage == 'complete'`.
    - Among the `classified` evaluations only (indeterminate entries do not
      participate in this chain):
      - the first classified evaluation's `presence_delta` must be `new` or
        `still_absent` (there is no earlier classified state to transition from).
      - `presence_delta='new'` is only valid for that first classified evaluation
        (a finding cannot become "new" again after having a classified history).
      - `presence_delta='recurrent'` requires the immediately preceding *classified*
        evaluation's `presence_delta` to have been `resolved` or `still_absent`.
      - `presence_delta='resolved'` requires the immediately preceding *classified*
        evaluation's `presence_delta` to have been `new`, `active`, or `recurrent`
        (the finding must have previously been present to be resolved).
    """
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
                f"finding_contract.evaluations[{index}].evaluation_id="
                f"{evaluation_id!r} duplicates an earlier evaluation_id in the same "
                "history (evaluation_id must be unique within evaluations[])."
            )

        previous_ref = evaluation.get("previous_evaluation_ref")
        if index == 0:
            if previous_ref is not None:
                raise RetrospectiveSchemaError(
                    f"finding_contract.evaluations[{index}].previous_evaluation_ref "
                    "is non-null but this is the first evaluation in the history "
                    "(evaluation/history inconsistency)."
                )
        else:
            expected_previous = evaluations[index - 1]["evaluation_id"]
            if previous_ref != expected_previous:
                raise RetrospectiveSchemaError(
                    f"finding_contract.evaluations[{index}].previous_evaluation_ref="
                    f"{previous_ref!r} does not equal the immediately preceding "
                    f"evaluation's evaluation_id ({expected_previous!r}); forking or "
                    "skipping ahead in the evaluation history is not permitted "
                    "(evaluation/history inconsistency)."
                )
        seen_ids.add(evaluation_id)

        observed = evaluation["observed"]
        presence_delta = evaluation["presence_delta"]
        evaluation_status = evaluation["evaluation_status"]
        source_coverage = evaluation["source_coverage"]

        if observed and presence_delta in _ABSENT_STATES:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: observed=true but "
                f"presence_delta={presence_delta!r} asserts absence (observed and "
                "presence_delta are inconsistent)."
            )
        if not observed and presence_delta in _PRESENT_STATES:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: observed=false but "
                f"presence_delta={presence_delta!r} asserts presence (observed and "
                "presence_delta are inconsistent)."
            )

        if evaluation_status == "indeterminate" and presence_delta in {"resolved", "recurrent"}:
            raise RetrospectiveSchemaError(
                f"finding_contract.evaluations[{index}]: evaluation_status="
                f"'indeterminate' cannot assert presence_delta={presence_delta!r}; "
                "an indeterminate evaluation must never be reported as resolved or "
                "recurrent."
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
                        f"(was {presence_delta!r}); there is no earlier classified "
                        "state to transition from."
                    )
            else:
                if presence_delta == "new":
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta='new' "
                        "is only valid for the first classified evaluation in the "
                        "history."
                    )
                if presence_delta == "recurrent" and last_classified_presence not in {
                    "resolved",
                    "still_absent",
                }:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta="
                        "'recurrent' requires the immediately preceding classified "
                        "evaluation's presence_delta to be 'resolved' or "
                        f"'still_absent' (was {last_classified_presence!r})."
                    )
                if presence_delta == "resolved" and last_classified_presence not in {
                    "new",
                    "active",
                    "recurrent",
                }:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: presence_delta="
                        "'resolved' requires the immediately preceding classified "
                        "evaluation's presence_delta to be 'new', 'active', or "
                        f"'recurrent' (was {last_classified_presence!r}); a finding "
                        "must have previously been present to be resolved."
                    )
            last_classified_presence = presence_delta


def validate_signal_specs_consistency(candidate: dict[str, Any]) -> None:
    """Cross-check baseline/current/expected signal specs within each evaluation.

    No-op when `finding_contract` is absent. Among the non-null
    `baseline_signal`/`current_signal`/`expected_signal` of a single evaluation, the
    `signal_type`/`unit`/`comparator`/`worse_direction` fields must agree -- three
    different signals describing the same measurement axis cannot disagree on what
    is being measured, how it is compared, or which direction is worse (Issue #2288
    P0-4). `value`/`tolerance` are exempt (those legitimately vary: baseline/current/
    expected are different observations of the same axis).
    """
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return
    for index, evaluation in enumerate(finding_contract["evaluations"]):
        signals = {
            name: evaluation.get(name)
            for name in ("baseline_signal", "current_signal", "expected_signal")
        }
        present = {name: signal for name, signal in signals.items() if signal is not None}
        if len(present) < 2:
            continue
        reference_name, reference_signal = next(iter(present.items()))
        for field in ("signal_type", "unit", "comparator", "worse_direction"):
            reference_value = reference_signal.get(field)
            for name, signal in present.items():
                if name == reference_name:
                    continue
                if signal.get(field) != reference_value:
                    raise RetrospectiveSchemaError(
                        f"finding_contract.evaluations[{index}]: {name}.{field}="
                        f"{signal.get(field)!r} does not match "
                        f"{reference_name}.{field}={reference_value!r}; baseline/"
                        "current/expected signal specs within the same evaluation "
                        "must agree on signal_type/unit/comparator/worse_direction."
                    )


def compute_delta_capability(candidate: dict[str, Any]) -> str:
    """Classify a candidate's delta-evaluation capability without mutating it.

    Returns `"legacy_unavailable"` when `finding_contract` is absent -- the record is
    a legacy lifecycle-only candidate and is not eligible for delta evaluation.
    Returns `"evaluated"` when `finding_contract` is present. This function never
    adds, backfills, or auto-generates a `finding_contract`/identity on `candidate`;
    it only inspects and classifies.
    """
    return "legacy_unavailable" if not candidate.get("finding_contract") else "evaluated"


class RetrospectiveSchemaError(ValueError):
    """Raised for schema validation, digest-consistency, and state-transition failures."""


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_run_schema() -> dict[str, Any]:
    return _load_schema(RUN_SCHEMA_PATH)


def load_candidate_schema() -> dict[str, Any]:
    return _load_schema(CANDIDATE_SCHEMA_PATH)


def _validate_with_format_checking(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate `instance` against `schema`, checking `format` (e.g. date-time).

    `jsonschema.validate()` does not check `format` unless an explicit
    `format_checker` is supplied (this is documented `jsonschema` behavior, not a
    LOOP_PROTOCOL-specific choice). This helper always constructs a validator with the
    module-local `_FORMAT_CHECKER` (stdlib-only "date-time" checker, see above) so
    malformed `date-time` values are rejected rather than silently accepted, without
    depending on an optional format-validation package.
    """
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=_FORMAT_CHECKER)
    validator.validate(instance)


def validate_run(instance: dict[str, Any]) -> None:
    """Validate an agent_retrospective_run/v1 instance.

    Raises jsonschema.exceptions.ValidationError on schema/format failure, or
    RetrospectiveSchemaError if run_identity.source_set_digest does not match the
    digest recomputed from source_observations (digest consistency).
    """
    _validate_with_format_checking(instance, load_run_schema())

    source_observations = instance["source_observations"]
    expected_digest = compute_source_set_digest(source_observations)
    actual_digest = instance["run_identity"]["source_set_digest"]
    if expected_digest != actual_digest:
        raise RetrospectiveSchemaError(
            "source_set_digest mismatch: run_identity.source_set_digest="
            f"{actual_digest!r} but compute_source_set_digest(source_observations)="
            f"{expected_digest!r}. The digest is derived from source_observations and "
            "cannot be supplied independently by the caller."
        )


def validate_candidate(instance: dict[str, Any]) -> None:
    """Validate an agent_improvement_candidate/v1 instance.

    Raises jsonschema.exceptions.ValidationError on schema/format failure. When
    `finding_contract` is present, additionally raises RetrospectiveSchemaError if:

    - `finding_contract.identity.value` does not match the value recomputed from
      `identity.key` (see `validate_finding_identity()`);
    - `finding_contract.claim_class` does not match
      `finding_contract.identity.key.claim_class` (see
      `validate_claim_class_consistency()`);
    - `finding_contract.evaluations[]` history is internally inconsistent, including
      judgement-table invariant violations (see `validate_finding_contract_history()`);
    - `baseline_signal`/`current_signal`/`expected_signal` within an evaluation
      disagree on signal_type/unit/comparator/worse_direction (see
      `validate_signal_specs_consistency()`).

    No-op for all of the above (legacy candidate) when `finding_contract` is absent.
    """
    _validate_with_format_checking(instance, load_candidate_schema())
    validate_finding_identity(instance)
    validate_claim_class_consistency(instance)
    validate_finding_contract_history(instance)
    validate_signal_specs_consistency(instance)


def is_valid_run(instance: dict[str, Any]) -> bool:
    try:
        validate_run(instance)
    except (jsonschema.exceptions.ValidationError, RetrospectiveSchemaError):
        return False
    return True


def is_valid_candidate(instance: dict[str, Any]) -> bool:
    try:
        validate_candidate(instance)
    except (jsonschema.exceptions.ValidationError, RetrospectiveSchemaError):
        return False
    return True


def validate_transition(from_status: str, to_status: str) -> bool:
    """Return True iff `from_status -> to_status` is an allowed direct transition.

    Unknown statuses (not in the closed enum) are rejected via
    RetrospectiveSchemaError rather than silently returning False, so callers cannot
    mistake "unknown status" for "known but disallowed transition".
    """
    if from_status not in CANDIDATE_STATUSES:
        raise RetrospectiveSchemaError(f"unknown candidate_status: {from_status!r}")
    if to_status not in CANDIDATE_STATUSES:
        raise RetrospectiveSchemaError(f"unknown candidate_status: {to_status!r}")
    return to_status in ALLOWED_TRANSITIONS[from_status]


def compute_source_set_digest(source_observations: list[dict[str, Any]]) -> str:
    """Compute a deterministic sha256 hex digest over source_observations.

    Idempotency use only ((repo, base_sha, source_set_digest, scope) duplicate
    suppression per ADR 0007 Decision 5) -- NOT an optimistic-concurrency token.

    Determinism is achieved via:
      1. Sorting the observations list by a stable key (source_id, endpoint,
         fetch_started_at) before serializing, so the digest is independent of the
         order in which concurrent Child 3 (#2236) adapters appended their entries.
      2. Canonical JSON serialization (sorted object keys, compact separators) of the
         sorted list.

    Calling this function twice with the same (structurally-equal, order-independent)
    input always returns the same digest.
    """

    def _sort_key(observation: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(observation.get("source_id", "")),
            str(observation.get("endpoint", "")),
            str(observation.get("fetch_started_at", "")),
        )

    ordered = sorted(source_observations, key=_sort_key)
    canonical = json.dumps(ordered, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_fixture(name: str) -> dict[str, Any]:
    fixtures_dir = _SCHEMAS_DIR / "fixtures"
    with (fixtures_dir / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)
