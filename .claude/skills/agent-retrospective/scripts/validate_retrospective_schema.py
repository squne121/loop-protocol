#!/usr/bin/env python3
"""Validator for agent_retrospective_run/v1 and agent_improvement_candidate/v1.

This module intentionally stays a pure, static validation library:

- JSON Schema validation for both schemas (``validate_run`` / ``validate_candidate``).
- A ``candidate_status`` state-transition validator (``validate_transition`` /
  ``ALLOWED_TRANSITIONS``) that enforces the closed enum's *reachability graph*, which
  the JSON Schema enum alone cannot express (e.g. ``rejected`` and ``implemented`` are
  each individually valid enum values, but the direct transition
  ``rejected -> implemented`` must be rejected).
- ``compute_source_set_digest()``: a deterministic sha256 digest over
  ``source_observations``, intended strictly for *idempotency* (duplicate-suppression)
  use per docs/adr/0007-agent-retrospective-boundaries.md Decision 5 -- this digest is
  NOT an optimistic-concurrency / stale-write-protection token (that mechanism,
  ``expected_previous_digest`` / ``version``, is out of scope for this Issue; see
  Child 5 / #2238).

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
# be abandoned/superseded at any point prior to being validated), but transitions must
# still go through this table -- an unknown `from_status` / `to_status` is rejected.

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"accepted", "rejected", "superseded"},
    "accepted": {"implementation_issue_created", "rejected", "superseded"},
    "implementation_issue_created": {"implemented", "superseded"},
    "implemented": {"validating", "superseded"},
    "validating": {"validated", "implemented", "superseded"},
    "validated": {"superseded"},
    "rejected": set(),
    "superseded": set(),
}

CANDIDATE_STATUSES = frozenset(ALLOWED_TRANSITIONS)


class RetrospectiveSchemaError(ValueError):
    """Raised for schema validation and state-transition failures."""


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_run_schema() -> dict[str, Any]:
    return _load_schema(RUN_SCHEMA_PATH)


def load_candidate_schema() -> dict[str, Any]:
    return _load_schema(CANDIDATE_SCHEMA_PATH)


def validate_run(instance: dict[str, Any]) -> None:
    """Validate an agent_retrospective_run/v1 instance.

    Raises jsonschema.exceptions.ValidationError on failure.
    """
    jsonschema.validate(instance=instance, schema=load_run_schema())


def validate_candidate(instance: dict[str, Any]) -> None:
    """Validate an agent_improvement_candidate/v1 instance.

    Raises jsonschema.exceptions.ValidationError on failure.
    """
    jsonschema.validate(instance=instance, schema=load_candidate_schema())


def is_valid_run(instance: dict[str, Any]) -> bool:
    try:
        validate_run(instance)
    except jsonschema.exceptions.ValidationError:
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

    Determinism is achieved via canonical JSON serialization (sorted keys, compact
    separators); calling this function twice with the same (structurally-equal) input
    always returns the same digest, regardless of dict key insertion order.
    """
    canonical = json.dumps(source_observations, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_fixture(name: str) -> dict[str, Any]:
    fixtures_dir = _SCHEMAS_DIR / "fixtures"
    with (fixtures_dir / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)
