#!/usr/bin/env python3
"""Validator for agent_retrospective_run/v1 and agent_improvement_candidate/v1.

This module intentionally stays a pure, static validation library:

- JSON Schema validation for both schemas (``validate_run`` / ``validate_candidate``),
  including ``format`` (e.g. ``date-time``) checking via an explicit
  ``jsonschema.FormatChecker`` -- ``jsonschema.validate()`` does NOT check ``format``
  by default, so a validator instance is constructed explicitly here instead.
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
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"

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
    LOOP_PROTOCOL-specific choice). This helper always constructs a validator with a
    `FormatChecker` so malformed `date-time` values are rejected rather than silently
    accepted.
    """
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema, format_checker=jsonschema.FormatChecker())
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

    Raises jsonschema.exceptions.ValidationError on schema/format failure.
    """
    _validate_with_format_checking(instance, load_candidate_schema())


def is_valid_run(instance: dict[str, Any]) -> bool:
    try:
        validate_run(instance)
    except (jsonschema.exceptions.ValidationError, RetrospectiveSchemaError):
        return False
    return True


def is_valid_candidate(instance: dict[str, Any]) -> bool:
    try:
        validate_candidate(instance)
    except jsonschema.exceptions.ValidationError:
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
