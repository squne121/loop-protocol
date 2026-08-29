#!/usr/bin/env python3
"""run_retrospective.py -- agent-retrospective deterministic phase engine and
stable executable entrypoint (Issue #2237, iteration-3 fix_delta for OWNER
review #2237#issuecomment-5378291560).

Owns, as a single coherent call graph, the four deterministic phases
(``prepare`` / ``validate-observers`` / ``prepare-evaluator`` / ``finalize``)
plus:

  - the ephemeral wire contract (``SourcePlan`` / ``EvidenceBundle`` /
    ``FindingSet`` / ``EvaluatorRequest`` / ``Evaluation`` / ``PublishRequest``)
    as strict dataclasses with a round-trippable JSON serializer/deserializer,
    including nested smuggled-authority-field rejection and canonical
    ``agent_improvement_candidate/v1`` (#2288/#2289) candidate validation
  - a production Agent invocation adapter (headless CLI subprocess:
    ``claude -p --agent <name> --output-format json --json-schema <schema
    text> --no-session-persistence``, prompt via stdin)
  - a ``PreviousStateProvider`` read-only port (fixture/in-memory
    implementation; the persistence-backed production provider is #2238) and
    a delta engine that classifies against ``finding_contract.identity`` /
    ``finding_contract.evaluations[]`` (never the legacy lifecycle enum)
  - the ``PUBLISH_REQUEST_V1`` producer schema (proposal-only, forbidden
    fields rejected structurally, digest bound to run_identity + concurrency
    token)
  - a delegated-Agent permission policy that is consumed by the real
    invocation path (subprocess env sanitation + ``--disallowedTools`` argv)
  - exact observer manifest / base_sha / role-authority enforcement for the
    fan-in step
  - a run-scoped temp artifact directory with mode ``0700`` and cleanup on
    every exit path (success / exception / SIGINT / SIGTERM)
  - ``run_cli()``/``main()``: the single stable executable entrypoint the
    root Skill (``SKILL.md``'s procedure) invokes via Bash. Root Skill owns
    *triggering* the run (a single Bash call) and any high-level stop
    decision; this module owns everything from collector closures through
    the final ``PublishRequest`` (or typed failure) printed to stdout. This
    module never calls Claude Code's interactive ``Agent`` tool -- the
    headless CLI subprocess it shells out to is a distinct, non-interactive
    invocation transport. See ``references/`` for the full rationale.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import typing
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = Path(__file__).resolve().parent

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# sibling module loading (reuse Child 2/3 logic without editing it -- those
# files are outside this Issue's Allowed Paths)
# ---------------------------------------------------------------------------


def _load_sibling_module(module_name: str, filename: str):
    import importlib.util

    module_path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load sibling module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _validate_retrospective_schema_module():
    return _load_sibling_module("agent_retrospective_validate_schema", "validate_retrospective_schema.py")


def _default_finding_identity_algorithm() -> str:
    """Reuse Child 2's canonical ``FINDING_IDENTITY_ALGORITHM`` constant
    (Issue #2235/#2236) as the ``finding_identity_algorithm`` argument passed
    to ``PreviousStateProvider.get()`` on the production call graph (Issue
    #2237 fix_delta iteration-4, Warning 1: ``compute_delta()`` wiring)."""
    return _validate_retrospective_schema_module().FINDING_IDENTITY_ALGORITHM


def _collect_snapshot_module():
    return _load_sibling_module("agent_retrospective_collect_snapshot", "collect_snapshot.py")


def _persist_retrospective_run_module():
    """Lazily load Issue #2238's persistence module (``persist_retrospective_run.py``,
    a sibling script in the same Allowed Paths set). Loaded lazily -- like the
    other sibling loaders above -- so importing this module never requires
    ``persist_retrospective_run.py`` to be importable unless a caller actually
    resolves the ``issue-comments`` state backend (Issue #2238 AC1)."""
    return _load_sibling_module("agent_retrospective_persist_run", "persist_retrospective_run.py")


def compute_source_set_digest(source_observations: list[dict[str, Any]]) -> str:
    """Reuse Child 2's canonical ``source_set_digest`` computation (Issue
    #2235/#2236). Loaded lazily so importing this module never requires the
    sibling scripts to be importable unless a caller actually needs the
    digest (e.g. pure wire-contract unit tests)."""
    return _validate_retrospective_schema_module().compute_source_set_digest(source_observations)


# ---------------------------------------------------------------------------
# ephemeral wire contract (P0-2/P0-3): strict dataclass serializer/deserializer
# ---------------------------------------------------------------------------

#: byte-size bound for a single serialized ephemeral wire envelope.
MAX_ENVELOPE_BYTES = 262_144

#: schema repair retry bound (P1-2 execution budget). Fixed, not configurable
#: by callers -- see references/execution-budget.md.
SCHEMA_REPAIR_RETRIES = 1

WIRE_SCHEMA_SOURCE_PLAN = "source_plan/v1"
WIRE_SCHEMA_EVIDENCE_BUNDLE = "observer_result/v1"
WIRE_SCHEMA_FINDING_SET = "finding_set/v1"
WIRE_SCHEMA_EVALUATOR_REQUEST = "evaluator_request/v1"
WIRE_SCHEMA_EVALUATION = "evaluation_result/v1"
WIRE_SCHEMA_PUBLISH_REQUEST = "publish_request/v1"

#: default ``PreviousStateProvider.get()`` ``scope`` value used by
#: ``execute_run()``/``run_cli()`` when the caller doesn't override it (Issue
#: #2237 fix_delta iteration-4, Warning 1). This module scopes delta
#: correlation to the whole repository; a narrower scope is a future
#: extension the ``PreviousStateProvider`` port already supports.
DEFAULT_PREVIOUS_STATE_SCOPE = "repository"

#: Issue #2238 P0-5 fix_delta: the runtime_version this module's own
#: execute_run()/run_cli() production call graph stamps into the extended
#: run_identity dict it passes to finalize() -- distinct from
#: persist_retrospective_run.py's own RUNTIME_VERSION (that module
#: identifies the *publisher*; this one identifies the *observer/evaluator
#: run* that produced the source_observations/candidate_records).
RUNTIME_VERSION = "agent-retrospective-run/v1"

#: forbidden fields on PUBLISH_REQUEST_V1 (Issue #2237 P0-4). These are never
#: declared as PublishRequest dataclass fields, so the generic strict
#: deserializer already rejects them as "unknown_field" -- listed here only
#: so tests/documentation can assert the forbidden set explicitly.
PUBLISH_REQUEST_FORBIDDEN_FIELDS = frozenset(
    {"authorized", "authorized_by_human", "authorization_token", "mutation_capability"}
)

#: keys that must never appear anywhere in a wire envelope payload -- not
#: merely at the top level. Any of these appearing at *any* nesting depth
#: inside ``findings[]`` / ``finding_sets[]`` / ``candidate_records[]`` /
#: ``run_identity`` (or anywhere else) is rejected fail-closed (Issue #2237
#: P0-3: nested smuggled raw-evidence / mutation-authority fields).
SMUGGLED_AUTHORITY_KEYS = frozenset(
    {
        "private_evidence",
        "authorized",
        "authorized_by_human",
        "authorization_token",
        "mutation_capability",
        "raw_stdout",
        "raw_stderr",
        "raw_transcript",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "api_key",
        "access_token",
        "absolute_path",
    }
)


class WireContractError(ValueError):
    """Raised for any ephemeral wire contract violation: JSON decode
    failure, missing field, unknown field, field-type mismatch, oversize
    payload, schema_version mismatch, nested smuggled-authority field, or
    invalid nested candidate record (Issue #2237 P0-2/P0-3)."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class SchemaRepairExhausted(WireContractError):
    """Raised when Agent output still fails strict validation after the
    bounded schema repair retry (``SCHEMA_REPAIR_RETRIES``) is exhausted
    (AC14). The caller MUST NOT invoke the evaluator when this is raised."""


def _check_field_type(value: Any, annotation: Any) -> bool:
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if origin is list:
        if not isinstance(value, list):
            return False
        if not args or args[0] is Any:
            return True
        return all(_check_field_type(v, args[0]) for v in value)
    if origin is dict:
        return isinstance(value, dict)
    if annotation is Any:
        return True
    if args and type(None) in args:
        non_none = [a for a in args if a is not type(None)]
        if value is None:
            return True
        return any(_check_field_type(value, a) for a in non_none)
    if annotation is bool:
        return isinstance(value, bool)
    if annotation is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation is str:
        return isinstance(value, str)
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True  # pragma: no cover - unmodeled typing construct, fail open on shape


def _scan_for_smuggled_keys(value: Any, path: str = "$") -> None:
    """Recursively scan ``value`` (already-decoded JSON) for any key in
    ``SMUGGLED_AUTHORITY_KEYS`` at *any* nesting depth. This closes the P0-3
    gap where only top-level ``additionalProperties: false`` was enforced --
    ``findings[]`` / ``finding_sets[]`` / ``candidate_records[]`` /
    ``run_identity`` are ``dict[str, Any]``/``list[dict]`` at the dataclass
    level, so nested smuggled fields previously passed straight through."""
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if key in SMUGGLED_AUTHORITY_KEYS:
                raise WireContractError(
                    f"smuggled_authority_field:{path}.{key}", reason_code="smuggled_authority_field"
                )
            _scan_for_smuggled_keys(sub_value, f"{path}.{key}")
    elif isinstance(value, list):
        for index, sub_value in enumerate(value):
            _scan_for_smuggled_keys(sub_value, f"{path}[{index}]")


def _validate_candidate_records(records: Sequence[dict[str, Any]]) -> None:
    """Validate every entry of ``records`` against the canonical, currently
    merged ``agent_improvement_candidate/v1`` schema (#2288/#2289) -- the
    same schema/validator ``validate_retrospective_schema.py`` uses -- and
    reject duplicate ``candidate_id`` values within the same list (Issue
    #2237 P0-3/P0-4). No-op on an empty list."""
    import jsonschema

    validator_mod = _validate_retrospective_schema_module()
    seen_ids: set[Any] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise WireContractError(f"candidate_record_not_object[{index}]", reason_code="candidate_schema_invalid")
        try:
            validator_mod.validate_candidate(record)
        except (validator_mod.RetrospectiveSchemaError, jsonschema.exceptions.ValidationError) as exc:
            raise WireContractError(
                f"candidate_schema_invalid[{index}]:{exc}", reason_code="candidate_schema_invalid"
            ) from exc
        candidate_id = record.get("candidate_id")
        if candidate_id in seen_ids:
            raise WireContractError(f"duplicate_candidate_id:{candidate_id}", reason_code="duplicate_identity")
        seen_ids.add(candidate_id)


def _parse_wire_payload(cls: type["_WireEnvelope"], text: str) -> dict[str, Any]:
    """Generic wire-envelope field-shape validation (Issue #2362): oversize
    bound, JSON decode, unknown-field / missing-field / type-mismatch
    checks, and the recursive ``_scan_for_smuggled_keys()`` nested scan.
    Returns the validated raw payload dict WITHOUT constructing ``cls`` --
    this is the single shared implementation `_WireEnvelope.from_wire()`
    (which constructs `cls` immediately afterwards, firing
    `_post_validate()` via `__post_init__`) and the deterministic-
    enrichment outer-envelope-parse phase (`run_evaluation()`, which needs
    the validated payload dict BEFORE `Evaluation` is constructed so
    `_post_validate()`/`_validate_candidate_records()` do not fire until
    after enrichment) both call, so this generic validation logic is
    implemented exactly once."""
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) > MAX_ENVELOPE_BYTES:
        raise WireContractError("envelope_oversize", reason_code="oversize")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WireContractError(f"invalid_json:{exc}", reason_code="decode_failure") from exc
    if not isinstance(payload, dict):
        raise WireContractError("payload_not_object", reason_code="decode_failure")

    declared_fields = {f.name: f for f in dataclasses.fields(cls)}
    resolved_types = typing.get_type_hints(cls)
    payload_keys = set(payload.keys())
    declared_keys = set(declared_fields.keys())

    unknown = payload_keys - declared_keys
    if unknown:
        raise WireContractError(f"unknown_fields:{sorted(unknown)}", reason_code="unknown_field")
    missing = declared_keys - payload_keys
    if missing:
        raise WireContractError(f"missing_fields:{sorted(missing)}", reason_code="missing_field")

    for name in declared_keys:
        annotation = resolved_types.get(name, Any)
        if not _check_field_type(payload[name], annotation):
            raise WireContractError(f"field_type_mismatch:{name}", reason_code="type_mismatch")

    # nested smuggled-authority-field scan (P0-3): every nested dict/list
    # value is walked, not just the top-level key set.
    for name in declared_keys:
        _scan_for_smuggled_keys(payload[name], f"$.{name}")

    return payload


def _retry_wire_parse(
    raw_text: str,
    parse_fn: Callable[[str], Any],
    *,
    repair: Callable[[str, "WireContractError"], str] | None,
    max_retries: int,
) -> Any:
    """Shared retry-on-``WireContractError`` loop (AC14): both
    ``parse_agent_output_with_repair`` (constructs the envelope) and
    ``run_evaluation()``'s parse -> deterministic-enrichment -> canonical-
    construction pipeline (Issue #2367 fix_delta item 6 -- the ``repair``
    boundary covers all three steps, not only the outer-envelope parse)
    delegate to this single implementation for identical retry/backoff
    semantics."""
    attempt = 0
    text = raw_text
    last_error: WireContractError | None = None
    while True:
        try:
            return parse_fn(text)
        except WireContractError as exc:
            last_error = exc
            if attempt >= max_retries or repair is None:
                break
            text = repair(text, exc)
            attempt += 1
    raise SchemaRepairExhausted(
        f"schema_repair_exhausted:attempts={attempt + 1}:{last_error}",
        reason_code=(last_error.reason_code if last_error else "unknown"),
    )


@dataclass
class _WireEnvelope:
    """Mixin base for every ephemeral wire contract dataclass. Subclasses
    MUST be ``@dataclass``. ``to_wire``/``from_wire`` implement the strict
    round-trippable serializer/deserializer required by AC7: unknown fields,
    missing fields, field-type mismatches, oversize payloads, malformed
    JSON, and nested smuggled-authority fields are all rejected fail-closed.

    ``__post_init__`` calls ``_post_validate()`` so subclass-specific checks
    (e.g. ``Evaluation``/``PublishRequest``'s canonical candidate-schema
    validation) run on *direct* dataclass construction too -- not only when
    going through ``from_wire`` -- so a caller cannot bypass validation by
    constructing the dataclass directly instead of parsing wire text."""

    def __post_init__(self) -> None:
        self._post_validate()

    def to_wire(self) -> str:
        payload = dataclasses.asdict(self)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_wire(cls, text: str) -> "_WireEnvelope":
        # Issue #2362: generic field-shape validation (oversize/decode/
        # unknown/missing/type_mismatch + nested smuggled-key scan) lives in
        # the shared `_parse_wire_payload()` helper -- see that function's
        # docstring for why this must not be reimplemented here.
        payload = _parse_wire_payload(cls, text)
        try:
            instance = cls(**payload)
        except TypeError as exc:  # pragma: no cover - defensive, shape already checked above
            raise WireContractError(f"construction_failed:{exc}", reason_code="construction_failed") from exc
        instance._post_validate()
        return instance

    def _post_validate(self) -> None:
        """Hook for subclass-specific structural checks beyond generic field
        shape (e.g. ``schema_version`` must equal the canonical wire id, or
        nested ``candidate_records`` must satisfy the canonical candidate
        schema). Cross-envelope checks such as ``run_id`` agreement are the
        caller's responsibility (see ``validate_run_id_agreement``), not this
        hook's, because a single envelope cannot know about its peers."""
        return None


def _require_schema_version(instance: "_WireEnvelope", expected: str) -> None:
    actual = getattr(instance, "schema_version", None)
    if actual != expected:
        raise WireContractError(
            f"schema_version_mismatch:expected={expected},actual={actual}", reason_code="schema_version_mismatch"
        )


@dataclass
class SourcePlan(_WireEnvelope):
    """``SOURCE_PLAN_V1``: output of the ``prepare`` phase."""

    schema_version: str = WIRE_SCHEMA_SOURCE_PLAN
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    sources: list[str] = field(default_factory=list)
    generated_at: str = ""

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_SOURCE_PLAN)


@dataclass
class EvidenceBundle(_WireEnvelope):
    """``OBSERVER_RESULT_V1``: a single observer's serialized output. Only a
    public-safe ``evidence_ref`` string is carried -- there is no field for
    raw payload / stdout / stderr / absolute paths / credentials, so an
    attempt to smuggle a ``private_evidence`` (or similarly-shaped) key --
    at any nesting depth, including inside ``findings[]`` -- is rejected
    (AC10/P0-3)."""

    schema_version: str = WIRE_SCHEMA_EVIDENCE_BUNDLE
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    observer_id: str = ""
    evidence_ref: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_EVIDENCE_BUNDLE)


@dataclass
class FindingSet(_WireEnvelope):
    """``FINDING_SET_V1``: the fan-in projection of one observer's findings,
    handed to the evaluator wave. Never carries raw evidence -- only the
    schema-controlled ``findings`` projection produced during
    ``validate-observers``."""

    schema_version: str = WIRE_SCHEMA_FINDING_SET
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    observer_id: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_FINDING_SET)


@dataclass
class EvaluatorRequest(_WireEnvelope):
    """``EVALUATOR_REQUEST_V1``: the ``prepare-evaluator`` phase output,
    fed to the (fresh-context) evaluator Agent invocation."""

    schema_version: str = WIRE_SCHEMA_EVALUATOR_REQUEST
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    finding_sets: list[dict[str, Any]] = field(default_factory=list)

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_EVALUATOR_REQUEST)


@dataclass
class Evaluation(_WireEnvelope):
    """``EVALUATION_RESULT_V1``: the evaluator's serialized output, consumed
    only by the ``finalize`` phase. ``candidate_records`` must each satisfy
    the canonical, currently-merged ``agent_improvement_candidate/v1``
    schema (#2288/#2289) -- not a private shadow dialect (Issue #2237 P0-4)."""

    schema_version: str = WIRE_SCHEMA_EVALUATION
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    candidate_records: list[dict[str, Any]] = field(default_factory=list)
    evidence_ref: str = ""

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_EVALUATION)
        _validate_candidate_records(self.candidate_records)


@dataclass
class PublishRequest(_WireEnvelope):
    """``PUBLISH_REQUEST_V1``: proposal-only envelope (Issue #2237 P0-4).
    Contains no mutation authority / human-approval trust root field --
    ``authorized``/``authorized_by_human``/``authorization_token``/
    ``mutation_capability`` are not declared fields, so any input containing
    one of them (at any nesting depth) is rejected (AC16/P0-3).
    ``authorization_required`` is always ``True``; the receipt-based
    authorization channel is #2238's responsibility (out of scope here)."""

    schema_version: str = WIRE_SCHEMA_PUBLISH_REQUEST
    request_id: str = ""
    repository_id: str = ""
    target_issue: int = 0
    run_identity: dict[str, Any] = field(default_factory=dict)
    candidate_records: list[dict[str, Any]] = field(default_factory=list)
    expected_previous_digest: str | None = None
    idempotency_key: str = ""
    public_projection_digest: str = ""
    authorization_required: bool = True
    #: per-finding ``compute_delta()`` output (Issue #2237 fix_delta
    #: iteration-4, Warning 1): each entry is one of the dicts
    #: ``compute_delta()`` returns (``finding_identity``/``evaluation_status``/
    #: ``delta_status``, plus ``indeterminate_reason`` when
    #: ``evaluation_status == "indeterminate"``). Empty when the caller wires
    #: no ``PreviousStateProvider`` result (e.g. direct ``finalize()`` callers
    #: in tests that don't pass ``delta_results``). Not part of
    #: ``public_projection_digest``'s hash preimage -- delta classification is
    #: a deterministic function of ``run_identity``/``candidate_records`` plus
    #: previously-persisted state, not itself a concurrency precondition.
    delta_results: list[dict[str, Any]] = field(default_factory=list)

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_PUBLISH_REQUEST)
        if self.authorization_required is not True:
            raise WireContractError("authorization_required_must_be_true", reason_code="invalid_value")
        _validate_candidate_records(self.candidate_records)


def validate_run_id_agreement(*envelopes: _WireEnvelope) -> None:
    """Cross-envelope check: every envelope in a given fan-in/fan-out step
    must share the same ``run_id`` (Issue #2237 "production Agent invocation
    経路" section)."""
    run_ids = {getattr(e, "run_id") for e in envelopes if hasattr(e, "run_id")}
    if len(run_ids) > 1:
        raise WireContractError(f"run_id_mismatch:{sorted(run_ids)}", reason_code="run_id_mismatch")


def parse_agent_output_with_repair(
    raw_text: str,
    envelope_cls: type[_WireEnvelope],
    *,
    repair: Callable[[str, WireContractError], str] | None = None,
    max_retries: int = SCHEMA_REPAIR_RETRIES,
) -> _WireEnvelope:
    """Parse ``raw_text`` (a serialized Agent output string) as
    ``envelope_cls``, retrying via ``repair`` up to ``max_retries`` times on
    ``WireContractError`` (AC14). Raises ``SchemaRepairExhausted`` -- never
    returns a partially-valid result -- once the retry budget is spent.

    NOTE: for ``envelope_cls`` subclasses whose ``_post_validate()`` does
    more than generic field-shape checks (e.g. ``Evaluation``'s canonical
    candidate-schema validation), constructing ``envelope_cls`` here means
    those subclass-specific checks run INSIDE this retry loop too (via
    ``from_wire()``'s ``cls(**payload)`` -> ``__post_init__`` ->
    ``_post_validate()``). ``run_evaluation()`` (Issue #2362/#2367)
    deliberately does NOT call this function for ``Evaluation`` -- a
    deterministic-enrichment phase must run BETWEEN outer-envelope parsing
    and canonical construction (Issue #2362 steps 2-3), so it builds its
    own parse -> enrich -> construct pipeline function and drives it
    through ``_retry_wire_parse`` directly (Issue #2367 fix_delta item 6),
    so one ``repair`` attempt covers the full pipeline, not only the
    outer-envelope parse."""
    return _retry_wire_parse(raw_text, envelope_cls.from_wire, repair=repair, max_retries=max_retries)


# ---------------------------------------------------------------------------
# base_sha fixed-once run context (AC8)
# ---------------------------------------------------------------------------


class RunContext:
    """Owns the single ``run_id`` nonce and the single ``base_sha``
    resolution for one ``run_retrospective.py`` execution. ``base_sha`` is
    resolved lazily but memoized -- ``base_sha_resolver`` is invoked at most
    once per ``RunContext`` instance regardless of how many collectors read
    ``.base_sha`` (AC8)."""

    def __init__(self, *, base_sha_resolver: Callable[[], str], run_id: str | None = None) -> None:
        self._base_sha_resolver = base_sha_resolver
        self._base_sha: str | None = None
        self._resolve_count = 0
        self.run_id = run_id or str(uuid.uuid4())

    @property
    def base_sha(self) -> str:
        if self._base_sha is None:
            resolved = self._base_sha_resolver()
            if not isinstance(resolved, str) or not _FULL_SHA_RE.match(resolved):
                raise ValueError(f"base_sha must be a full 40-char hex commit SHA, got {resolved!r}")
            self._base_sha = resolved
            self._resolve_count += 1
        return self._base_sha

    @property
    def resolve_count(self) -> int:
        return self._resolve_count


# ---------------------------------------------------------------------------
# manual trigger preflight
# ---------------------------------------------------------------------------


def manual_trigger_preflight(*, repo_root: Path) -> None:
    """Minimal preflight run before ``prepare``: the run must be launched
    from inside an actual git checkout. This is intentionally narrow --
    Allowed Paths / worktree binding / CI gating are the root Skill's
    responsibility, not this deterministic engine's."""
    if not (repo_root / ".git").exists():
        raise ValueError(f"repo_root is not a git checkout root: {repo_root}")


# ---------------------------------------------------------------------------
# prepare phase
# ---------------------------------------------------------------------------


def prepare(
    *,
    base_sha_resolver: Callable[[], str],
    collectors: Sequence[Callable[[str], Any]],
    clock: Callable[[], datetime] = _utcnow,
    run_id: str | None = None,
) -> tuple[RunContext, SourcePlan, list[Any]]:
    """``prepare`` phase: fixes ``run_id``/``base_sha`` once, runs every
    caller-supplied collector (each a ``Callable[[base_sha], CollectorResult]``
    -- see Child 3's ``collect_snapshot.py``), and produces ``SourcePlan``."""
    ctx = RunContext(base_sha_resolver=base_sha_resolver, run_id=run_id)
    base_sha = ctx.base_sha  # resolved (at most) once, memoized for the rest of the run
    results = [collector(base_sha) for collector in collectors]
    observations = [r.observation for r in results]
    digest = compute_source_set_digest(observations)
    plan = SourcePlan(
        run_id=ctx.run_id,
        base_sha=base_sha,
        source_set_digest=digest,
        sources=[str(o.get("source_id", "")) for o in observations],
        generated_at=_iso(clock()),
    )
    return ctx, plan, results


def build_source_digest_registry(results: Sequence[Any]) -> dict[str, str]:
    """Derive a ``source_id -> per-source evidence digest`` registry from the
    ``results`` returned by ``prepare()`` (each a Child 3 ``CollectorResult``
    with a public ``.observation`` and a private ``.private_evidence``).
    Used only in-process by ``build_finding_sets`` (never serialized into any
    wire envelope) to bind a discovery-role (web) finding's claimed
    ``evidence_digest`` to the independently, deterministically recomputed
    digest -- never trusting the observing Agent's own claim alone (Issue
    #2237 P0-6)."""
    registry: dict[str, str] = {}
    for result in results:
        observation = getattr(result, "observation", {}) or {}
        source_id = str(observation.get("source_id", ""))
        private_evidence = getattr(result, "private_evidence", {}) or {}
        digest = private_evidence.get("evidence_digest")
        if source_id and digest:
            registry[source_id] = str(digest)
    return registry


# ---------------------------------------------------------------------------
# production Agent invocation adapter (AC13, P0-1)
# ---------------------------------------------------------------------------


@dataclass
class AgentInvocationRequest:
    """Typed request for a single headless CLI subprocess Agent invocation.
    ``json_schema_path`` is a path on disk to a JSON Schema file -- the
    *contents* of that file (not the path string) are what the real
    ``claude`` CLI's ``--json-schema`` flag expects (Issue #2237 P0-1)."""

    agent_name: str
    prompt: str
    json_schema_path: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 300
    #: Issue #2374: opt-in role-adapter identifier. ``None`` (the default)
    #: for every existing caller/agent (``retrospective-runtime-observer``,
    #: ``web-researcher``, ``retrospective-evaluator``, and
    #: codebase-investigator's own default/no-task invocation) -- with this
    #: unset, ``invoke_agent``'s result-text recovery path
    #: (``_structured_output_from_result_compat``) behaves EXACTLY as
    #: before (no behavior change to any pre-existing caller). Only
    #: ``build_observer_requests()``'s substantive-caller-supplied-task
    #: codebase-investigator branch sets this to
    #: ``_ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1``, which
    #: additionally recognizes the SubAgent's own native
    #: ``CODEBASE_INVESTIGATION_RESULT_V1`` output contract
    #: (``.claude/agents/codebase-investigator.md``) among the candidates
    #: recovered from the wrapper's ``result`` text, so a role adapter
    #: (``apply_codebase_investigator_role_adapter``) can convert it into
    #: this module's ``EvidenceBundle``/``OBSERVER_RESULT_V1`` wire
    #: contract instead of unconditionally failing closed with
    #: ``missing_structured_output``.
    role_adapter: str | None = None


@dataclass
class AgentInvocationResult:
    """Typed result. ``status`` covers every branch the Issue body requires
    the adapter to satisfy: success / timeout / SIGTERM / api error / partial
    result / malformed structured output."""

    status: str  # ok | timeout | terminated | api_error | partial_result | malformed_output
    structured_output: dict[str, Any] | None
    raw_stdout_excerpt: str | None
    exit_code: int | None
    reason_code: str | None
    #: Issue #2374: ``True`` only when ``request.role_adapter ==
    #: _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1`` AND the result-text
    #: recovery path matched exactly one candidate against the native
    #: ``CODEBASE_INVESTIGATION_RESULT_V1`` shape (never the
    #: ``observer_result_v1`` shape) -- a marker consumed only by
    #: ``apply_codebase_investigator_role_adapter``/
    #: ``invoke_agent_with_role_adapter``. ``status`` is still ``"ok"`` and
    #: ``structured_output`` still carries the (not-yet-converted) native
    #: dict in this case; every other caller/status combination leaves this
    #: ``False``, matching pre-#2374 behavior exactly.
    native_role_adapter_candidate: bool = False


_AGENT_INVOCATION_STATUSES = frozenset(
    {"ok", "timeout", "terminated", "api_error", "partial_result", "malformed_output"}
)

#: bounded excerpt length kept on failure for diagnostics -- never the full
#: raw stdout blob (mirrors collect_snapshot.py's `_safe_diagnostic_text`).
_MAX_STDOUT_EXCERPT = 200

#: subprocess environment passthrough allowlist used when no
#: ``DelegatedAgentPermissionPolicy`` is supplied (defensive fallback only --
#: production callers always supply a policy; see ``run_cli``).
_DEFAULT_ENV_PASSTHROUGH_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})

# ---------------------------------------------------------------------------
# codebase-investigator role adapter (Issue #2374)
# ---------------------------------------------------------------------------

#: ``AgentInvocationRequest.role_adapter`` value that opts a codebase-
#: investigator invocation into native ``CODEBASE_INVESTIGATION_RESULT_V1``
#: candidate recognition (see ``_looks_like_native_codebase_investigation_result``)
#: and role-adapted conversion into ``EvidenceBundle``/``OBSERVER_RESULT_V1``
#: (see ``apply_codebase_investigator_role_adapter``). Only
#: ``build_observer_requests()``'s substantive-caller-supplied-task
#: codebase-investigator branch ever sets this.
_ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1 = "codebase_investigator_observer_v1"

#: Required top-level keys of ``.claude/agents/codebase-investigator.md``'s
#: own native ``CODEBASE_INVESTIGATION_RESULT_V1`` output contract
#: (``schema_version``/``status``/``investigation_route``/``evidence_refs``/
#: ``discovery_summary``/``impact_scope``/``failure_reason``/
#: ``source_evidence_result`` -- 8 fields, matching the SubAgent's own
#: "Result: CODEBASE_INVESTIGATION_RESULT_V1" section, corrected from the
#: pre-Issue-#2374 Issue body's mistaken "7 fields"). This module never adds
#: a second on-disk JSON Schema file for this shape (Issue #2374 Allowed
#: Paths note: a schema file is needed only if the *native --json-schema*
#: mode is chosen -- this module instead recognizes the shape structurally,
#: in Python, keeping the fix additive within the Allowed Paths already
#: granted).
_NATIVE_CODEBASE_INVESTIGATION_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "investigation_route",
        "evidence_refs",
        "discovery_summary",
        "impact_scope",
        "failure_reason",
        "source_evidence_result",
    }
)

#: ``CODEBASE_INVESTIGATION_RESULT_V1.schema_version`` is the integer ``1``
#: (distinct from ``observer_result_v1.schema.json``'s ``schema_version``
#: const string ``"observer_result/v1"`` -- the two shapes' ``schema_version``
#: values are never confusable with one another).
_NATIVE_CODEBASE_INVESTIGATION_SCHEMA_VERSION = 1


def _looks_like_native_codebase_investigation_result(candidate: dict[str, Any]) -> bool:
    """Structural (non-jsonschema) recognizer for
    ``.claude/agents/codebase-investigator.md``'s own native
    ``CODEBASE_INVESTIGATION_RESULT_V1`` output contract -- distinct from,
    and never validated against, this module's ``observer_result_v1.schema.json``
    wire contract. Only used when ``role_adapter ==
    _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1`` (see
    ``_structured_output_from_result_compat``)."""
    if not isinstance(candidate, dict):
        return False
    if not _NATIVE_CODEBASE_INVESTIGATION_RESULT_KEYS.issubset(candidate.keys()):
        return False
    return candidate.get("schema_version") == _NATIVE_CODEBASE_INVESTIGATION_SCHEMA_VERSION


def _stdout_excerpt(text: str | None) -> str | None:
    if not text:
        return None
    return text[:_MAX_STDOUT_EXCERPT]


def _default_sanitized_env(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in _DEFAULT_ENV_PASSTHROUGH_ALLOWLIST}


#: Matches every markdown fence *delimiter* line (an opening line such as
#: ```json / ```text / ```python / bare ``` , OR a closing bare ``` line),
#: allowing up to 3 leading spaces of indent per GFM. `_iter_fenced_json_candidates`
#: below pairs up consecutive fence-delimiter matches (1st+2nd, 3rd+4th, ...)
#: as (opener, closer) regardless of the opener's info string, then filters
#: pairs down to those whose opener info string is "" or "json"
#: (case-insensitive). This fixes Issue #2348's regression where a
#: foreign-language fenced block (e.g. ```text ... ```) appearing before the
#: real ```json ... ``` block had its own *closing* fence misread as a new
#: bare *opening* fence by the prior opener-only regex, silently swallowing
#: the real JSON block's closing fence and making the real candidate vanish
#: from `finditer()` results entirely.
_FENCE_DELIMITER_RE = re.compile(r"^[ \t]{0,3}```([^\n`]*)$", re.MULTILINE)


def _iter_fenced_json_candidates(text: str) -> list[str]:
    """Enumerate the body text of every JSON-eligible markdown fenced code
    block found anywhere within `text`, in encounter order (Issue #2348).

    Unlike the pre-#2348 implementation, which matched only ```json / bare
    ``` *opener* fences directly via a single regex (and thus silently
    misread a foreign-language fence's *closing* delimiter as a new bare
    opener, corrupting subsequent fence pairing -- see
    `_FENCE_DELIMITER_RE` docstring), this scans every backtick fence
    delimiter line first, regardless of its info string, and pairs them up
    sequentially (1st opener + 2nd closer, 3rd opener + 4th closer, ...).
    Only pairs whose opener info string is "" or "json" (case-insensitive)
    are returned as JSON candidates; foreign-language fences (```text,
    ```python, ...) are paired and skipped, not misread as delimiters of an
    unrelated block. An unpaired trailing fence (odd total delimiter count)
    is ignored. Pure string transform -- callers still parse/validate each
    candidate themselves (`_structured_output_from_result_compat`)."""
    fence_matches = list(_FENCE_DELIMITER_RE.finditer(text))
    candidates: list[str] = []
    for opener, closer in zip(fence_matches[0::2], fence_matches[1::2]):
        info = opener.group(1).strip().lower()
        if info not in ("", "json"):
            continue
        content_start = opener.end()
        if content_start < len(text) and text[content_start] == "\n":
            content_start += 1
        content = text[content_start:closer.start()]
        if content.endswith("\n"):
            content = content[:-1]
        candidates.append(content.strip())
    return candidates


@dataclass
class _RecoveredStructuredOutput:
    """Return type of ``_structured_output_from_result_compat`` (Issue
    #2374, replacing its prior bare ``dict[str, Any] | None`` return --
    an internal, ``_``-prefixed helper's return shape, never a wire
    contract). ``matched_kind`` is ``"observer"`` (the pre-#2374, still
    default behavior), ``"native"`` (Issue #2374's codebase-investigator
    role-adapter recognition -- ``payload`` is the NOT-YET-CONVERTED native
    ``CODEBASE_INVESTIGATION_RESULT_V1`` dict), or ``None`` (no unambiguous
    candidate -- ``payload`` is also ``None``). The remaining fields are
    diagnostics (Issue #2374 In Scope "診断精緻化") describing every
    fenced/unfenced JSON candidate considered, regardless of outcome."""

    payload: dict[str, Any] | None
    matched_kind: str | None
    result_fence_count: int = 0
    json_candidate_count: int = 0
    observer_schema_valid_candidate_count: int = 0
    native_schema_valid_candidate_count: int = 0
    observed_top_level_keys: list[list[str]] = field(default_factory=list)


def _structured_output_from_result_compat(
    payload: dict[str, Any], *, json_schema_path: str, role_adapter: str | None = None
) -> "_RecoveredStructuredOutput":
    """Best-effort recovery of the schema-conformant business payload from
    the wrapper's `result` text field, attempted only when the
    `structured_output` wrapper field is absent or explicitly `None`
    (Issue #2301 P0-1 adapter fix; PR #2324 review fix_delta narrowed the
    trigger condition to absent/null only -- a present-but-wrong-type value,
    e.g. a string, list, or number, is never routed through this recovery
    path, since the CLI would only ever legitimately omit the field, not
    populate it with the wrong shape). This behavior was observed against
    the real `claude` CLI, version 2.1.241, for `--agent <custom-subagent>
    --json-schema ...` invocations: unlike schema-less (no `--agent`) `-p
    --json-schema ...` invocations, which populate `structured_output`
    directly, these custom-subagent invocations were observed to omit
    `structured_output` and instead carry the schema-conformant JSON in
    `result` (frequently wrapped in a markdown ```json code fence, with the
    fence itself sometimes preceded and/or followed by explanatory prose --
    Issue #2348 root cause: the prior whole-string anchor regex required the
    fence to be the *entire* `result` text and silently failed on this
    common shape). This is an observation about invocations actually
    exercised, not an absolute claim covering every possible custom-subagent
    invocation.

    Recovery strategy (Issue #2348): every markdown fenced code block found
    anywhere in `result` (see `_iter_fenced_json_candidates`) is treated as
    an independent JSON candidate; if `result` contains no fence at all, the
    whole (stripped) `result` text is treated as the sole candidate
    (preserves the pre-#2348 behavior for unfenced `result` text). Each
    candidate is independently parsed with `json.loads` and, only if that
    succeeds and yields a dict, independently, strictly validated against
    the exact schema file this invocation was given (`request.
    json_schema_path`) -- mirrors the Issue #2237 P0-1 rationale against
    re-parsing the *wrapper* itself as the business schema. The recovered
    payload is returned only when EXACTLY ONE candidate both parses as JSON
    and passes schema validation: zero schema-valid candidates is the
    pre-existing `missing_structured_output` fail-closed outcome, and MORE
    THAN ONE schema-valid candidate is treated as ambiguous and also
    rejected (fail-closed) rather than guessing which fence is the intended
    business payload. A malformed/absent `result`, or one where no
    candidate passes schema validation, is never silently accepted as
    `ok`.

    Issue #2374: when ``role_adapter ==
    _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1``, every candidate that
    FAILS observer-schema validation is additionally probed against
    ``_looks_like_native_codebase_investigation_result`` (the
    codebase-investigator SubAgent's own native output contract). An
    observer-schema-valid candidate is never also probed as native (the two
    shapes are mutually exclusive by construction -- see that recognizer's
    docstring). Exactly one native match (and zero observer matches) is
    returned as ``matched_kind="native"`` -- still the RAW, NOT-YET-CONVERTED
    native dict; conversion into ``EvidenceBundle``/``OBSERVER_RESULT_V1`` is
    ``apply_codebase_investigator_role_adapter``'s responsibility, not this
    function's. When ``role_adapter`` is ``None`` (every other caller),
    this function's behavior is byte-for-byte identical to its pre-#2374
    form: only observer-schema-valid candidates are ever considered."""
    result_text = payload.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        return _RecoveredStructuredOutput(None, None)
    try:
        schema = json.loads(Path(json_schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _RecoveredStructuredOutput(None, None)

    fenced_candidates = _iter_fenced_json_candidates(result_text)
    candidate_texts = fenced_candidates if fenced_candidates else [result_text.strip()]

    observer_valid: list[dict[str, Any]] = []
    native_valid: list[dict[str, Any]] = []
    json_candidate_count = 0
    observed_top_level_keys: list[list[str]] = []
    for candidate_text in candidate_texts:
        try:
            candidate = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        json_candidate_count += 1
        observed_top_level_keys.append(sorted(candidate.keys()))
        try:
            jsonschema.validate(candidate, schema)
            observer_valid.append(candidate)
            continue
        except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError):
            pass
        if role_adapter == _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1 and (
            _looks_like_native_codebase_investigation_result(candidate)
        ):
            native_valid.append(candidate)

    diagnostics_kwargs = {
        "result_fence_count": len(fenced_candidates),
        "json_candidate_count": json_candidate_count,
        "observer_schema_valid_candidate_count": len(observer_valid),
        "native_schema_valid_candidate_count": len(native_valid),
        "observed_top_level_keys": observed_top_level_keys,
    }

    if len(observer_valid) == 1 and not native_valid:
        return _RecoveredStructuredOutput(observer_valid[0], "observer", **diagnostics_kwargs)
    if len(native_valid) == 1 and not observer_valid:
        return _RecoveredStructuredOutput(native_valid[0], "native", **diagnostics_kwargs)
    return _RecoveredStructuredOutput(None, None, **diagnostics_kwargs)


def build_agent_invocation_argv(
    request: AgentInvocationRequest, *, policy: "DelegatedAgentPermissionPolicy | None" = None
) -> list[str]:
    """Construct the real ``claude`` CLI argv for ``request`` (Issue #2237
    P0-1): ``--agent <name>`` selects the custom SubAgent, ``--json-schema``
    receives the schema *file contents* (not a path), ``--output-format
    json`` requests the metadata-wrapper JSON response, and
    ``--no-session-persistence`` prevents the headless invocation from
    persisting/resuming a session across runs. The prompt is NOT placed in
    argv (see ``invoke_agent``'s ``input=`` stdin wiring) -- passing
    arbitrary prompt text as an argv element is both a shell-quoting hazard
    and an argv-length hazard. Subscription login is assumed (no ``--bare``).
    """
    schema_text = Path(request.json_schema_path).read_text(encoding="utf-8")
    argv = [
        "claude",
        "-p",
        "--agent",
        request.agent_name,
        "--output-format",
        "json",
        "--json-schema",
        schema_text,
        "--no-session-persistence",
    ]
    if policy is not None:
        argv += ["--disallowedTools", *sorted(policy.denied_tools)]
    return argv


def invoke_agent(
    request: AgentInvocationRequest,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    policy: "DelegatedAgentPermissionPolicy | None" = None,
) -> AgentInvocationResult:
    """Production Agent invocation adapter. ``runner`` is dependency-injected
    (defaults to ``subprocess.run``) so tests exercise this exact function
    via a subprocess mock harness without starting a real process (AC13).

    ``policy``, when supplied, is the *runtime mechanism* this invocation
    actually consumes (Issue #2237 P0-5): its denied-tool set is serialized
    into the real subprocess argv (``--disallowedTools``, see
    ``build_agent_invocation_argv``) and its ``sanitize_subprocess_env`` is
    used to build the child process environment, stripping mutation
    credentials before the subprocess ever starts -- not merely checked by a
    test that calls the policy directly and throws the result away."""
    argv = build_agent_invocation_argv(request, policy=policy)
    merged_env = {**os.environ, **request.env}
    env = policy.sanitize_subprocess_env(merged_env) if policy is not None else _default_sanitized_env(merged_env)
    try:
        completed = runner(
            argv,
            cwd=request.cwd,
            env=env,
            input=request.prompt,
            capture_output=True,
            text=True,
            timeout=request.timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return AgentInvocationResult(
            status="timeout", structured_output=None, raw_stdout_excerpt=None, exit_code=None, reason_code="timeout"
        )
    except OSError as exc:
        return AgentInvocationResult(
            status="api_error",
            structured_output=None,
            raw_stdout_excerpt=None,
            exit_code=None,
            reason_code=type(exc).__name__,
        )

    # SIGTERM shows up as a negative returncode (POSIX: -signal.SIGTERM) when
    # the child is killed, or as 128+signum under some shells/wrappers.
    if completed.returncode in (-signal.SIGTERM, 128 + signal.SIGTERM):
        return AgentInvocationResult(
            status="terminated",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code="sigterm",
        )

    if completed.returncode != 0:
        return AgentInvocationResult(
            status="api_error",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stderr or completed.stdout),
            exit_code=completed.returncode,
            reason_code="nonzero_exit",
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code="json_decode_failure",
        )
    if not isinstance(payload, dict):
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code="payload_not_object",
        )

    # `api_error_with_partial_text: reject_as_evidence` (P1-2 budget): a
    # response carrying an error marker alongside partial text is never
    # treated as usable evidence -- it is surfaced as `partial_result`, which
    # the caller (observer wave / evaluator invocation) always rejects.
    if payload.get("is_error"):
        return AgentInvocationResult(
            status="partial_result",
            structured_output=payload,
            raw_stdout_excerpt=None,
            exit_code=completed.returncode,
            reason_code="api_error_with_partial_text",
        )

    # Real `claude -p --output-format json` responses are a metadata wrapper
    # (`type: "result"`, `subtype`, `is_error`, `result` (text), plus, when a
    # `--json-schema` was supplied, `structured_output` carrying the actual
    # schema-conformant business payload). Re-parsing the *wrapper* itself as
    # the business schema (the pre-fix_delta behavior) silently accepted a
    # malformed/absent business payload as long as the wrapper's own shape
    # happened to satisfy the target dataclass -- Issue #2237 P0-1.
    if payload.get("type") != "result":
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code="unexpected_wrapper_shape",
        )

    # PR #2324 review fix_delta (P0-1): the wrapper's own `subtype` must be
    # checked *before* any `result`/`structured_output` recovery is
    # attempted. A non-`"success"` subtype (e.g.
    # `error_max_structured_output_retries`) signals the CLI itself did not
    # consider this a successful structured-output invocation, even when
    # `result` happens to contain schema-conformant JSON text -- promoting
    # such a response to `status="ok"` would silently mask the CLI's own
    # failure signal.
    subtype = payload.get("subtype")
    if subtype != "success":
        return AgentInvocationResult(
            status="partial_result",
            structured_output=None,
            raw_stdout_excerpt=None,
            exit_code=completed.returncode,
            reason_code=f"result_subtype_not_success:{subtype or 'missing'}",
        )

    # Issue #2301 P0-1 adapter fix, narrowed by PR #2324 review fix_delta:
    # `_structured_output_from_result_compat` recovery is attempted ONLY
    # when `structured_output` is absent or explicitly `None` -- never when
    # it is present but a non-dict, wrong-type value (string/list/number/
    # bool), which remains `missing_structured_output` unconditionally. See
    # `_structured_output_from_result_compat`'s docstring for the full
    # rationale.
    _MISSING = object()
    raw_structured_output = payload.get("structured_output", _MISSING)
    structured_output_presence = (
        "present" if isinstance(raw_structured_output, dict)
        else "null" if raw_structured_output is None
        else "absent" if raw_structured_output is _MISSING
        else "present_wrong_type"
    )
    matched_kind: str | None = None
    recovery_diagnostics: "_RecoveredStructuredOutput | None" = None
    if isinstance(raw_structured_output, dict):
        structured_output = raw_structured_output
    elif raw_structured_output is _MISSING or raw_structured_output is None:
        recovery_diagnostics = _structured_output_from_result_compat(
            payload, json_schema_path=request.json_schema_path, role_adapter=request.role_adapter
        )
        structured_output = recovery_diagnostics.payload
        matched_kind = recovery_diagnostics.matched_kind
    else:
        structured_output = None

    # Issue #2374: a native (codebase-investigator's own
    # CODEBASE_INVESTIGATION_RESULT_V1) recovery match is surfaced as `ok`
    # with the raw, NOT-YET-CONVERTED native dict plus the
    # `native_role_adapter_candidate` marker -- conversion into
    # `EvidenceBundle`/`OBSERVER_RESULT_V1` is
    # `apply_codebase_investigator_role_adapter`'s responsibility (it needs
    # this run's `ctx.base_sha`/`ctx.run_id`/`plan.source_set_digest`, none
    # of which this context-free adapter function has access to).
    if matched_kind == "native" and isinstance(structured_output, dict):
        return AgentInvocationResult(
            status="ok",
            structured_output=structured_output,
            raw_stdout_excerpt=None,
            exit_code=completed.returncode,
            reason_code=None,
            native_role_adapter_candidate=True,
        )

    if not isinstance(structured_output, dict):
        reason_code = "missing_structured_output"
        # Issue #2374 In Scope "診断精緻化": only role_adapter-enabled
        # requests get the enriched reason_code -- every other caller keeps
        # the exact pre-#2374 literal `"missing_structured_output"` (no
        # observable behavior change for the other 2 observers / the
        # default/no-task codebase-investigator path -- AC6/AC7/AC8).
        if request.role_adapter is not None:
            diagnostics = {
                "wrapper_subtype": payload.get("subtype"),
                "structured_output_presence": structured_output_presence,
                "structured_output_type": (
                    type(raw_structured_output).__name__ if raw_structured_output is not _MISSING else None
                ),
                "result_fence_count": recovery_diagnostics.result_fence_count if recovery_diagnostics else 0,
                "json_candidate_count": recovery_diagnostics.json_candidate_count if recovery_diagnostics else 0,
                "observer_schema_valid_candidate_count": (
                    recovery_diagnostics.observer_schema_valid_candidate_count if recovery_diagnostics else 0
                ),
                "native_schema_valid_candidate_count": (
                    recovery_diagnostics.native_schema_valid_candidate_count if recovery_diagnostics else 0
                ),
                "observed_top_level_keys": recovery_diagnostics.observed_top_level_keys if recovery_diagnostics else [],
            }
            reason_code = "missing_structured_output:" + json.dumps(diagnostics, sort_keys=True)
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code=reason_code,
        )

    return AgentInvocationResult(
        status="ok",
        structured_output=structured_output,
        raw_stdout_excerpt=None,
        exit_code=completed.returncode,
        reason_code=None,
    )


class NativeResultAdaptationFailed(Exception):
    """Raised by ``apply_codebase_investigator_role_adapter`` when the
    codebase-investigator SubAgent's native ``CODEBASE_INVESTIGATION_RESULT_V1``
    cannot be role-adapted into a valid ``EvidenceBundle``/
    ``OBSERVER_RESULT_V1`` (Issue #2374 AC4/AC5). Callers MUST NOT convert
    this into an empty-``findings`` success -- see
    ``apply_codebase_investigator_role_adapter``'s caller
    (``invoke_agent_with_role_adapter``), which surfaces this as a typed
    ``AgentInvocationResult(status="malformed_output", ...)`` instead."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def adapt_native_codebase_investigation_result(
    native_result: dict[str, Any],
    *,
    run_id: str,
    base_sha: str,
    source_set_digest: str,
    observer_id: str,
) -> dict[str, Any]:
    """Role adapter (Issue #2374): converts a recognized native
    ``CODEBASE_INVESTIGATION_RESULT_V1`` dict (produced during an AGY
    advisory native fallback -- ``.claude/agents/codebase-investigator.md``)
    into an ``EvidenceBundle``-conformant (``observer_result/v1``) dict.
    Raises ``NativeResultAdaptationFailed`` -- never silently downgrades to
    an empty-``findings`` success -- when:

    - ``status`` is not ``"ok"`` (``"failed"``/``"inconclusive"`` -- AC4)
    - ``evidence_refs`` is missing/empty, is not a list of objects, or any
      entry's ``commit_sha`` does not equal this run's authoritative
      ``base_sha`` (AC5 -- ``REPO_EVIDENCE_REF_V1.commit_sha != ctx.base_sha``)
    - ``discovery_summary`` is missing/empty (nothing to report as a finding)

    The returned dict's key set is EXACTLY ``EvidenceBundle``'s 7 declared
    fields (``_parse_wire_payload`` rejects unknown/missing fields) -- no
    extra/renamed keys, and no ``SMUGGLED_AUTHORITY_KEYS`` collision (the
    nested ``evidence_refs`` carries only ``REPO_EVIDENCE_REF_V1`` public
    fields, never raw stdout/credentials/absolute paths)."""
    status = native_result.get("status")
    if status != "ok":
        raise NativeResultAdaptationFailed("native_result_status_not_ok")

    evidence_refs = native_result.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise NativeResultAdaptationFailed("native_result_missing_evidence_refs")
    for ref in evidence_refs:
        if not isinstance(ref, dict):
            raise NativeResultAdaptationFailed("native_result_evidence_ref_not_object")
        if ref.get("commit_sha") != base_sha:
            raise NativeResultAdaptationFailed("native_result_evidence_base_sha_mismatch")

    discovery_summary = native_result.get("discovery_summary")
    if not isinstance(discovery_summary, str) or not discovery_summary.strip():
        raise NativeResultAdaptationFailed("native_result_missing_discovery_summary")

    impact_scope = native_result.get("impact_scope")
    finding = {
        "claim": discovery_summary.strip(),
        "claim_class": "codebase_investigation",
        "investigation_route": native_result.get("investigation_route"),
        "impact_scope": impact_scope if isinstance(impact_scope, list) else [],
        "evidence_refs": evidence_refs,
    }

    return {
        "schema_version": WIRE_SCHEMA_EVIDENCE_BUNDLE,
        "run_id": run_id,
        "base_sha": base_sha,
        "source_set_digest": source_set_digest,
        "observer_id": observer_id,
        "evidence_ref": "codebase-investigator-native-fallback-evidence-ref",
        "findings": [finding],
    }


def apply_codebase_investigator_role_adapter(
    result: AgentInvocationResult,
    *,
    ctx: "RunContext",
    plan: "SourcePlan",
    observer_id: str,
) -> AgentInvocationResult:
    """Issue #2374 role adapter entry point: a pure, additive wrapper around
    an already-produced ``AgentInvocationResult``. When
    ``result.native_role_adapter_candidate`` is ``False`` (every request
    except a substantive-task codebase-investigator invocation that actually
    hit the native-recognition path), ``result`` is returned COMPLETELY
    UNCHANGED. Only when the marker is set does this function attempt
    ``adapt_native_codebase_investigation_result`` -- success replaces
    ``structured_output`` with the converted ``EvidenceBundle`` dict
    (``status="ok"``); failure (AC4/AC5 typed rejection) is surfaced as
    ``status="malformed_output"`` with a ``native_fallback_adaptation_failed:``
    -prefixed ``reason_code`` (never silently promoted to an empty-findings
    success)."""
    if not result.native_role_adapter_candidate:
        return result
    if not isinstance(result.structured_output, dict):  # pragma: no cover - defensive, invariant of the marker
        return result
    try:
        converted = adapt_native_codebase_investigation_result(
            result.structured_output,
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id=observer_id,
        )
    except NativeResultAdaptationFailed as exc:
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=result.raw_stdout_excerpt,
            exit_code=result.exit_code,
            reason_code=f"native_fallback_adaptation_failed:{exc.reason_code}",
        )
    return AgentInvocationResult(
        status="ok",
        structured_output=converted,
        raw_stdout_excerpt=None,
        exit_code=result.exit_code,
        reason_code=None,
    )


def invoke_agent_with_role_adapter(
    request: AgentInvocationRequest,
    *,
    ctx: "RunContext",
    plan: "SourcePlan",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    policy: "DelegatedAgentPermissionPolicy | None" = None,
) -> AgentInvocationResult:
    """Issue #2374: thin wrapper around ``invoke_agent`` that applies
    ``apply_codebase_investigator_role_adapter`` afterwards. Every request
    with ``role_adapter is None`` (every existing caller) passes through
    ``invoke_agent``'s own result completely unchanged -- this wrapper never
    alters ``invoke_agent``'s own behavior, it only adds a post-processing
    step for the codebase-investigator role-adapter path."""
    result = invoke_agent(request, runner=runner, policy=policy)
    if request.role_adapter != _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1:
        return result
    return apply_codebase_investigator_role_adapter(result, ctx=ctx, plan=plan, observer_id=request.agent_name)


# ---------------------------------------------------------------------------
# observer manifest / role authority (P0-6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObserverRoleSpec:
    """One entry of the expected observer manifest: which observer_id must
    appear in a run's observer wave, what authority role it holds
    (``interpreter`` / ``advisory`` / ``discovery``), and which source_type
    it corresponds to (Issue #2237 reused-Agent capability matrix)."""

    observer_id: str
    role: str
    source_type: str


#: the exact, fixed 3-observer manifest every full run must satisfy: no
#: missing observer, no extra/unknown observer, no duplicate (Issue #2237
#: P0-6). ``run_observer_wave``'s ``expected_manifest`` parameter defaults to
#: ``None`` (manifest-completeness check skipped) so pre-existing unit tests
#: exercising a subset of observers keep passing; the production entrypoint
#: (``run_cli``) always passes this exact tuple.
EXPECTED_OBSERVER_MANIFEST: tuple[ObserverRoleSpec, ...] = (
    ObserverRoleSpec("retrospective-runtime-observer", "interpreter", "runtime"),
    ObserverRoleSpec("codebase-investigator", "advisory", "repository"),
    ObserverRoleSpec("web-researcher", "discovery", "web"),
)


class UnboundEvidenceAuthority(WireContractError):
    """Raised when a discovery-role (web) finding claims an
    ``evidence_digest`` that does not match the independently, deterministic
    recomputed source digest registry (``build_source_digest_registry``).
    Web findings are only ever a *candidate* evidence reference from the
    observing Agent's own claim -- final finding authority requires the
    deterministic Web collector's (Child 3) re-fetched, digest-bound
    projection to agree (Issue #2237 P0-6)."""


# ---------------------------------------------------------------------------
# validate-observers phase (fan-out + fail-closed fan-in)
# ---------------------------------------------------------------------------


class ObserverWaveFailed(Exception):
    """Raised when any observer invocation in the wave fails (non-``ok``
    status, schema repair exhaustion, run/digest/base_sha mismatch, an
    observer outside the expected manifest, a duplicate observer_id, or an
    incomplete manifest). Per ``partial_agent_output: reject``, the caller
    MUST NOT invoke the evaluator once this is raised -- see
    ``run_evaluation``'s precondition and ``execute_run``'s ordering.

    Issue #2341 AC1: ``reason_code``/``exit_code`` are additive diagnostic
    attributes (never required by callers) so ``main()``'s top-level failure
    output can surface the underlying ``AgentInvocationResult.reason_code``
    (e.g. ``missing_structured_output``) instead of only the generic
    ``observer_failed:<agent>:<status>`` message text. When the raise site
    does not have an underlying ``AgentInvocationResult`` to attribute this
    to (envelope/base_sha/manifest mismatches), ``reason_code`` falls back
    to this exception class's own name -- exactly matching this module's
    pre-#2341 behavior for those cases."""

    def __init__(self, message: str, *, reason_code: str | None = None, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code if reason_code is not None else type(self).__name__
        self.exit_code = exit_code


def run_observer_wave(
    ctx: RunContext,
    plan: SourcePlan,
    *,
    invoke: Callable[[AgentInvocationRequest], AgentInvocationResult],
    observer_requests: Sequence[AgentInvocationRequest],
    repair: Callable[[str, WireContractError], str] | None = None,
    expected_manifest: Sequence[ObserverRoleSpec] | None = None,
) -> list[EvidenceBundle]:
    """``validate-observers`` phase (fan-out half): invoke every observer in
    ``observer_requests`` and strictly validate its serialized output into an
    ``EvidenceBundle``. All observers must succeed -- the first failure
    aborts the wave (fail-closed; ``observer_parallelism: 3`` is an execution
    budget for the caller's actual concurrency, not modeled by this
    sequential reference implementation).

    Every bundle's ``base_sha`` MUST equal ``ctx.base_sha`` (Issue #2237
    P0-6 -- previously unchecked here, letting a mismatched ``base_sha`` slip
    through and then be silently overwritten downstream). When
    ``expected_manifest`` is supplied, the observer_id set MUST match it
    exactly (no missing, no extra, no duplicate observer_id)."""
    expected_ids = {spec.observer_id for spec in expected_manifest} if expected_manifest is not None else None
    seen_ids: set[str] = set()
    bundles: list[EvidenceBundle] = []
    for request in observer_requests:
        result = invoke(request)
        if result.status != "ok":
            # Issue #2341 AC1: thread the underlying adapter-level
            # reason_code/exit_code through so main()'s top-level failure
            # output is diagnosable (e.g. distinguishes
            # missing_structured_output from other observer_failed causes).
            raise ObserverWaveFailed(
                f"observer_failed:{request.agent_name}:{result.status}",
                reason_code=result.reason_code,
                exit_code=result.exit_code,
            )
        raw_text = json.dumps(result.structured_output, sort_keys=True, separators=(",", ":"))
        bundle = parse_agent_output_with_repair(raw_text, EvidenceBundle, repair=repair)
        if bundle.run_id != ctx.run_id:
            raise ObserverWaveFailed(
                f"observer_run_id_mismatch:{request.agent_name}",
                reason_code="observer_run_id_mismatch",
            )
        if bundle.source_set_digest != plan.source_set_digest:
            raise ObserverWaveFailed(
                f"observer_source_set_digest_mismatch:{request.agent_name}",
                reason_code="observer_source_set_digest_mismatch",
            )
        if bundle.base_sha != ctx.base_sha:
            raise ObserverWaveFailed(
                f"observer_base_sha_mismatch:{request.agent_name}",
                reason_code="observer_base_sha_mismatch",
            )
        if bundle.observer_id in seen_ids:
            raise ObserverWaveFailed(
                f"duplicate_observer_id:{bundle.observer_id}",
                reason_code="duplicate_observer_id",
            )
        if expected_ids is not None and bundle.observer_id not in expected_ids:
            raise ObserverWaveFailed(
                f"observer_id_not_in_manifest:{bundle.observer_id}",
                reason_code="observer_id_not_in_manifest",
            )
        seen_ids.add(bundle.observer_id)
        bundles.append(bundle)
    if expected_ids is not None and seen_ids != expected_ids:
        raise ObserverWaveFailed(
            f"observer_manifest_incomplete:missing={sorted(expected_ids - seen_ids)}",
            reason_code="observer_manifest_incomplete",
        )
    return bundles


def build_finding_sets(
    ctx: RunContext,
    plan: SourcePlan,
    bundles: Sequence[EvidenceBundle],
    *,
    manifest: Sequence[ObserverRoleSpec] = EXPECTED_OBSERVER_MANIFEST,
    source_digest_registry: dict[str, str] | None = None,
) -> list[FindingSet]:
    """Fan-in half of ``validate-observers``: project each validated
    ``EvidenceBundle`` into a schema-controlled ``FindingSet`` (AC10 -- only
    this projection, never the bundle's ``evidence_ref``/raw channel, ever
    reaches the evaluator).

    Each projected finding is tagged with ``finding_authority`` derived from
    the observer's manifest role (Issue #2237 P0-6 capability matrix
    enforcement, not merely prose): ``interpreter`` role -> ``primary``;
    every other role (``advisory``/``discovery``, and any observer_id not in
    ``manifest``) -> ``advisory`` -- an advisory/discovery-role observer's
    output is never elevated to ``primary`` finding authority by this
    function. A discovery-role (web) finding that claims an
    ``evidence_digest`` not matching ``source_digest_registry`` raises
    ``UnboundEvidenceAuthority`` (fail-closed -- an unbound claim is rejected
    outright rather than silently downgraded)."""
    role_by_id = {spec.observer_id: spec.role for spec in manifest}
    source_type_by_id = {spec.observer_id: spec.source_type for spec in manifest}
    finding_sets: list[FindingSet] = []
    for bundle in bundles:
        role = role_by_id.get(bundle.observer_id, "advisory")
        source_type = source_type_by_id.get(bundle.observer_id)
        tagged_findings: list[dict[str, Any]] = []
        for finding in bundle.findings:
            tagged = dict(finding)
            tagged["finding_authority"] = "primary" if role == "interpreter" else "advisory"
            if role == "discovery" and source_digest_registry is not None:
                claimed_digest = finding.get("evidence_digest")
                expected_digest = source_digest_registry.get(source_type or "web")
                if claimed_digest is not None and expected_digest is not None and claimed_digest != expected_digest:
                    raise UnboundEvidenceAuthority(
                        f"web_evidence_digest_mismatch:observer={bundle.observer_id}",
                        reason_code="unbound_web_evidence",
                    )
            tagged_findings.append(tagged)
        finding_sets.append(
            FindingSet(
                run_id=ctx.run_id,
                base_sha=ctx.base_sha,
                source_set_digest=plan.source_set_digest,
                observer_id=bundle.observer_id,
                findings=tagged_findings,
            )
        )
    return finding_sets


# ---------------------------------------------------------------------------
# prepare-evaluator + evaluation (AC9: strict ordering)
# ---------------------------------------------------------------------------


class EvaluatorInvocationFailed(Exception):
    """Raised when the evaluator invocation itself fails or returns an
    envelope that fails cross-validation against the run.

    Issue #2341 AC1: ``reason_code``/``exit_code`` are additive diagnostic
    attributes mirroring ``ObserverWaveFailed`` (see its docstring); falls
    back to this class's own name when no underlying
    ``AgentInvocationResult`` applies (envelope mismatch)."""

    def __init__(self, message: str, *, reason_code: str | None = None, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code if reason_code is not None else type(self).__name__
        self.exit_code = exit_code


def prepare_evaluator_request(
    ctx: RunContext, plan: SourcePlan, finding_sets: Sequence[FindingSet]
) -> EvaluatorRequest:
    """``prepare-evaluator`` phase: build the single ``EvaluatorRequest`` fed
    to the fresh-context evaluator invocation. This function's mere existence
    as a caller-invoked step (never auto-chained from ``run_observer_wave``)
    is what lets ``execute_run``/``run_cli`` prove observer-wave completion
    precedes evaluator invocation (AC9)."""
    return EvaluatorRequest(
        run_id=ctx.run_id,
        base_sha=ctx.base_sha,
        source_set_digest=plan.source_set_digest,
        finding_sets=[dataclasses.asdict(fs) for fs in finding_sets],
    )


#: `finding_contract.identity.key` components that are model-judgment
#: (evaluator-authoritative) values, per Issue #2362's Identity/
#: Deterministic Field Authority Matrix. `repository_id` is deliberately
#: excluded -- it is always Python-side caller context, never evaluator
#: judgment (see `_enrich_candidate_record`).
_IDENTITY_KEY_JUDGMENT_FIELDS = ("claim_class", "subject_ref", "rule_id")


def _extract_candidate_identity_judgment(raw_candidate: dict[str, Any]) -> dict[str, Any]:
    """Judgment-only extraction (Issue #2362, Scope Reframe 2026-08-28) for
    a single raw (not yet validated) candidate record dict: pulls ONLY the
    `claim_class`/`subject_ref`/`rule_id` model-judgment values a
    deterministic `identity.key` needs. `repository_id` and any
    evaluator-supplied `identity`/`finding_contract`/`evaluations` are never
    read here -- the judgment-only wire schema
    (`schemas/evaluation_result_v1.schema.json`) no longer even accepts
    those fields from the evaluator, and `_enrich_candidate_record` always
    (re)constructs `identity`/`evaluations` from Python-side context /
    `compute_finding_identity()` / `compute_delta()`.

    `claim_class`/`subject_ref`/`rule_id` are now TOP-LEVEL fields on
    `raw_candidate` (the judgment-only wire shape has no `finding_contract`
    nesting at all -- unlike the superseded Issue #2362 design this Scope
    Reframe replaces, where the evaluator still nested a fully-shaped
    `finding_contract.identity.key`).

    Returns an all-``None`` judgment dict when `raw_candidate` is not a
    dict -- callers (`_enrich_candidate_record`) that need a schema-valid
    `identity.key` are responsible for letting the subsequent canonical
    candidate validation (which fires only after enrichment, at
    `Evaluation` construction) reject a candidate with missing judgment
    values; this extraction step itself never raises."""
    if not isinstance(raw_candidate, dict):
        return {field_name: None for field_name in _IDENTITY_KEY_JUDGMENT_FIELDS}
    return {
        "claim_class": raw_candidate.get("claim_class"),
        "subject_ref": raw_candidate.get("subject_ref"),
        "rule_id": raw_candidate.get("rule_id"),
    }


#: `subject_ref.kind` enum (mirrors `agent_improvement_candidate_v1.schema.json`
#: `$defs.subject_ref.properties.kind.enum` -- duplicated here only as a
#: shape-validity check for `_is_valid_subject_ref_judgment`, never as an
#: independent authority; the canonical schema remains the SSOT for
#: `Evaluation` construction).
_SUBJECT_REF_KINDS = frozenset({"repository_path", "issue", "pull_request", "workflow", "runtime", "external_resource"})

#: mirrors `agent_improvement_candidate_v1.schema.json`
#: `$defs.finding_identity.key.properties.rule_id.pattern`.
_RULE_ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


def _is_valid_subject_ref_judgment(value: Any) -> bool:
    """Shape-validity check (Issue #2362) for an evaluator-supplied
    `subject_ref` candidate: mirrors (does not replace) the canonical
    schema's `$defs.subject_ref` constraints closely enough to decide
    whether this value is safe to use as-is. `retrospective-evaluator.md`'s
    own prompt/frontmatter shows only a `{"...": "..."}` placeholder for
    `identity.key` (frozen, Out of Scope) -- a real evaluator response
    cannot always be relied on to nest a schema-conforming `subject_ref`
    there. Issue #2367 fix_delta item 3 (superseding the earlier Issue
    #2362 design): when this check fails, `_enrich_candidate_record` no
    longer substitutes a Python-synthesized `_fallback_subject_ref` --
    `subject_ref`/`rule_id` are evaluator judgment per Issue #2362's
    Identity/Deterministic Field Authority Matrix, and synthesizing them
    from `candidate_id` degrades cross-run finding identity correlation to
    the candidate lifecycle ID. A failing check now raises a typed
    `WireContractError(reason_code="candidate_schema_invalid")` instead."""
    if not isinstance(value, dict) or set(value.keys()) != {"kind", "value"}:
        return False
    kind = value.get("kind")
    ref_value = value.get("value")
    if kind not in _SUBJECT_REF_KINDS or not isinstance(ref_value, str) or not ref_value:
        return False
    if kind in ("issue", "pull_request") and not re.fullmatch(r"[0-9]+", ref_value):
        return False
    if kind == "repository_path" and (
        ref_value.startswith("/") or ref_value.startswith("./") or re.search(r"(^|/)\.\.(/|$)", ref_value)
    ):
        return False
    return True


def _is_valid_rule_id_judgment(value: Any) -> bool:
    """Shape-validity check (Issue #2362) mirroring
    `agent_improvement_candidate_v1.schema.json`
    `$defs.finding_identity.key.properties.rule_id.pattern` -- see
    `_is_valid_subject_ref_judgment`'s docstring for why a failing check
    (Issue #2367 fix_delta item 3) raises `candidate_schema_invalid`
    instead of substituting a Python-synthesized fallback."""
    return isinstance(value, str) and bool(_RULE_ID_RE.fullmatch(value))


def _observer_source_type_index(
    finding_sets: Sequence[dict[str, Any]], manifest: Sequence[ObserverRoleSpec] = EXPECTED_OBSERVER_MANIFEST
) -> dict[str, list[dict[str, Any]]]:
    """Build a ``source_type -> real observer findings`` index from
    ``evaluator_request.finding_sets`` (Issue #2362 Scope Reframe): the
    ACTUAL, already public-safe/redacted evidence data the evaluator had
    available this run, grouped by the observer's ``source_type`` (per
    ``EXPECTED_OBSERVER_MANIFEST``: ``runtime``/``repository``/``web``).
    Used only to recompute ``evidence_refs[].projection_digest`` from real
    data (`_enrich_evidence_ref`) -- never to fabricate evidence. A
    ``source_id`` with no corresponding observer in ``manifest`` (e.g.
    ``github`` -- no observer in the current 3-observer manifest produces
    GitHub-sourced findings) or whose observer reported zero findings has
    no entry here, which is the correct "no real evidence available"
    signal `_enrich_evidence_ref` uses to fail closed rather than
    fabricate a digest."""
    source_type_by_id = {spec.observer_id: spec.source_type for spec in manifest}
    index: dict[str, list[dict[str, Any]]] = {}
    for finding_set in finding_sets:
        if not isinstance(finding_set, dict):
            continue
        source_type = source_type_by_id.get(finding_set.get("observer_id"))
        if source_type is None:
            continue
        findings = finding_set.get("findings")
        if isinstance(findings, list) and findings:
            index.setdefault(source_type, []).extend(f for f in findings if isinstance(f, dict))
    return index


def _enrich_evidence_ref(
    raw_ref: Any, *, real_evidence_index: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Recompute ``projection_digest`` for one evaluator-supplied
    (judgment-only) ``evidence_ref`` from REAL evidence data (Issue #2362
    Scope Reframe) -- the evaluator's own `ref_type`/`source_id`/
    `resource_identity` judgment is trusted (it is genuine model judgment
    about which evidence it examined), but the digest is always computed
    here from the actual, real ``finding_sets`` content the evaluator was
    given for that ``source_id`` (a JCS-canonicalized hash of the real,
    already-redacted observer findings) -- NEVER from an evaluator-supplied
    value (the wire schema does not even accept one) and NEVER from a
    fabricated/placeholder string. Returns ``None`` (never a fabricated
    digest) when `raw_ref` is malformed, or when `real_evidence_index` has
    no real evidence for the claimed ``source_id`` (evaluator referenced a
    source with no backing evidence this run -- honest failure, not
    something to paper over); the caller (`_enrich_evidence_refs`) drops
    such refs rather than keep an unverifiable claim."""
    if not isinstance(raw_ref, dict):
        return None
    ref_type = raw_ref.get("ref_type")
    source_id = raw_ref.get("source_id")
    resource_identity = raw_ref.get("resource_identity")
    if not (isinstance(ref_type, str) and ref_type):
        return None
    if not (isinstance(source_id, str) and source_id):
        return None
    if not (isinstance(resource_identity, str) and resource_identity):
        return None
    real_findings = real_evidence_index.get(source_id)
    if not real_findings:
        return None
    projection = json.dumps(real_findings, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(projection.encode("utf-8")).hexdigest()
    return {
        "ref_type": ref_type,
        "source_id": source_id,
        "resource_identity": resource_identity,
        "projection_digest": digest,
    }


def _enrich_evidence_refs(
    raw_evidence_refs: Any, *, real_evidence_index: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Apply `_enrich_evidence_ref` to every entry of the evaluator's
    (judgment-only) ``evidence_refs`` list, dropping any entry that cannot
    be backed by real evidence data (Issue #2362 Scope Reframe -- never
    fabricate a digest to keep an unverifiable ref)."""
    if not isinstance(raw_evidence_refs, list):
        return []
    enriched_refs = []
    for raw_ref in raw_evidence_refs:
        enriched_ref = _enrich_evidence_ref(raw_ref, real_evidence_index=real_evidence_index)
        if enriched_ref is not None:
            enriched_refs.append(enriched_ref)
    return enriched_refs


def _find_previous_candidate(previous_state: "PreviousStateResult", identity_value: str) -> dict[str, Any] | None:
    """Look up the previous run's candidate record (if any) sharing the
    same ``finding_contract.identity.value`` (Issue #2362 Scope Reframe) --
    used to carry over the prior ``evaluations[]`` history so the new
    entry this run produces is APPENDED to it, never replacing it."""
    for candidate in previous_state.candidates:
        if _finding_identity_value(candidate) == identity_value:
            return candidate
    return None


def _classify_current_candidate_delta(previous_state: "PreviousStateResult", identity_value: str) -> dict[str, Any]:
    """Classify ONE currently-reported candidate's presence/absence delta
    against ``previous_state`` (Issue #2362 Scope Reframe -- AC3) by
    delegating to `compute_delta()` (the exact same algorithm the
    `PublishRequest.delta_results` sidecar uses -- no separate/duplicated
    classification logic that could drift from it) with a single-item
    current-candidates batch. `compute_delta()`'s "resolved" synthesis loop
    (for previous identities absent from the batch) may append spurious
    entries for OTHER previous candidates when given a singleton batch --
    those are irrelevant here and discarded; only the entry whose
    ``finding_identity`` matches `identity_value` is returned, since a
    currently-reported candidate's own identity is always present in the
    single-item ``current_identities`` set `compute_delta()` builds, so its
    own classification entry is always produced."""
    synthetic_candidate = {"finding_contract": {"identity": {"value": identity_value}}}
    for result in compute_delta(previous_state, [synthetic_candidate]):
        if result.get("finding_identity") == identity_value:
            return result
    # Defensive: compute_delta() always classifies a present current
    # candidate with a resolvable identity value; reaching this branch
    # signals an internal precondition violation, not a legitimate
    # business classification -- fail closed rather than guess.
    raise WireContractError(
        f"delta_classification_unresolved:{identity_value}", reason_code="candidate_schema_invalid"
    )


#: maps `compute_delta()`'s coarse `delta_status` (for a CURRENTLY-reported
#: candidate -- never `"resolved"`-for-absent, which `compute_delta()` only
#: emits for previous-only identities, never through
#: `_classify_current_candidate_delta`'s single-item-batch call) to a
#: `presence_delta` value that keeps `agent_improvement_candidate_v1.schema.json`
#: `$defs.evaluation`'s `allOf` presence/delta invariants internally
#: consistent (Issue #2362 Scope Reframe): `presence_delta` `"new"`/
#: `"resolved"`/`"recurrent"` each FORCE `delta_status` to the identical
#: value, so those three map 1:1; `"unchanged"` maps to `"active"` +
#: `signal_delta: "unknown"`, which together also force `delta_status`
#: `"unchanged"` (the only remaining `allOf` branch), so the pairing stays
#: consistent either way.
_PRESENCE_DELTA_BY_DELTA_STATUS = {
    "new": "new",
    "resolved": "resolved",
    "recurrent": "recurrent",
    "unchanged": "active",
}

#: maps `PreviousStateResult.status` (when it forces `evaluation_status:
#: "indeterminate"`) to a canonical `source_coverage` enum value for the
#: entry `_build_evaluation_entry` constructs. `"stale"` has no identically-
#: named `source_coverage` enum member -- `"unavailable"` is the closest
#: honest characterization (previous data too old to treat as a reliable
#: comparison baseline).
_SOURCE_COVERAGE_BY_PREVIOUS_STATUS = {"partial": "partial", "stale": "unavailable"}


def _build_evaluation_entry(
    *,
    classification: dict[str, Any],
    prev_evaluations: list[dict[str, Any]],
    evidence_refs: list[dict[str, Any]],
    base_sha: str,
    source_set_digest: str,
    timestamp: str,
    identity_value: str,
    previous_status: str,
) -> dict[str, Any]:
    """Construct ONE full canonical `finding_contract.evaluations[]` entry
    (Issue #2362 Scope Reframe -- AC3) entirely from Python-side
    deterministic sources: `classification` (`_classify_current_candidate_delta`,
    itself sourced from `compute_delta()`/`PreviousStateResult`),
    `prev_evaluations` (the prior run's own history for this finding
    identity, from `PreviousStateProvider`), and `evidence_refs` (already
    digest-recomputed from real evidence data by `_enrich_evidence_refs`
    -- never the evaluator's raw claim). Never parses or reads any
    `evaluations[]` value from the evaluator's wire payload -- the
    judgment-only wire schema does not even accept one.

    Presence is the only continuous signal this module tracks (there is no
    real numeric/metric pipeline behind `baseline_signal`/`current_signal`
    -- inventing one would be exactly the kind of fabrication this Issue
    exists to remove), so `current_signal`/`baseline_signal` (when set) are
    always the same honest boolean "was this finding's identity present"
    signal, `worse_direction: "not_applicable"`, matching this module's
    superseded `_fallback_evaluation_entry`'s approach to the same
    constraint (Issue #2367 fix_delta) -- never a fabricated numeric
    severity/confidence score."""
    previous_evaluation_ref = prev_evaluations[-1]["evaluation_id"] if prev_evaluations else None
    evaluation_id_seed = f"evaluation_id:{identity_value}:{base_sha}:{source_set_digest}:{timestamp}".encode()
    evaluation_id = "sha256:" + hashlib.sha256(evaluation_id_seed).hexdigest()
    entry: dict[str, Any] = {
        "evaluation_id": evaluation_id,
        "evaluated_run_ref": {"base_sha": base_sha, "source_set_digest": source_set_digest},
        "previous_evaluation_ref": previous_evaluation_ref,
        "observed": True,
        "classified_at": timestamp,
        "classifier_version": "run_retrospective/v1",
        "evidence_refs": evidence_refs,
    }
    if classification["evaluation_status"] == "indeterminate":
        entry["source_coverage"] = _SOURCE_COVERAGE_BY_PREVIOUS_STATUS.get(previous_status, "unavailable")
        entry["evaluation_status"] = "indeterminate"
        entry["presence_delta"] = "active"
        entry["signal_delta"] = "unknown"
        entry["indeterminate_reason"] = classification.get("indeterminate_reason") or "source_partial"
        entry["baseline_signal"] = None
        entry["current_signal"] = None
        entry["expected_signal"] = None
        # `delta_status` intentionally OMITTED -- the canonical schema
        # forbids the key entirely when `evaluation_status ==
        # "indeterminate"` (Issue #2367 fix_delta item 1's fix, preserved).
    else:
        delta_status = classification["delta_status"]
        presence_delta = _PRESENCE_DELTA_BY_DELTA_STATUS.get(delta_status, "active")
        signal = {"signal_type": "boolean", "value": True, "comparator": "eq", "worse_direction": "not_applicable"}
        entry["source_coverage"] = "complete"
        entry["evaluation_status"] = "classified"
        entry["presence_delta"] = presence_delta
        entry["signal_delta"] = "unknown"
        entry["delta_status"] = delta_status
        entry["indeterminate_reason"] = None
        entry["baseline_signal"] = None if presence_delta == "new" else signal
        entry["current_signal"] = signal
        entry["expected_signal"] = None
    return entry


def _enrich_candidate_record(
    raw_candidate: dict[str, Any],
    *,
    repository_id: str,
    base_sha: str,
    source_set_digest: str,
    timestamp: str,
    previous_state: "PreviousStateResult",
    real_evidence_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Deterministic enrichment (Issue #2362 Scope Reframe, 2026-08-28
    owner-approved) for a single raw JUDGMENT-ONLY candidate record: builds
    the ENTIRE canonical `agent_improvement_candidate/v1` record from the
    evaluator's judgment-only fields (`candidate_id`/`title`/`description`/
    `claim_class`/`subject_ref`/`rule_id`/`evidence_refs`) plus 100%
    Python-side deterministic sources -- `repository_id`/`base_sha`/
    `source_set_digest`/`timestamp` (Python-side run context),
    `compute_finding_identity()` (identity.value SSOT, via the sibling
    module loader, never reimplemented), and `compute_delta()`/
    `previous_state` (evaluations[] history -- Issue #2362 AC3). The
    evaluator is NEVER asked for, and this function never reads,
    `identity`/`finding_contract`/`evaluations`/`repository_id`/
    `source_run_ref`/`created_at`/`updated_at`/`candidate_status` from
    `raw_candidate` -- the judgment-only wire schema
    (`schemas/evaluation_result_v1.schema.json`) does not even accept
    those fields, closing the architectural gap the superseded (Issue
    #2362 original / PR #2367 items 1-6) design could not: this function
    no longer merely OVERWRITES an evaluator-supplied `evaluations[]`/
    `identity` (which the frozen evaluator prompt could still emit in an
    incompatible vocabulary, causing `candidate_schema_invalid`) -- it
    never parses one from the wire payload in the first place.

    `subject_ref`/`rule_id` remain evaluator judgment (Issue #2362
    Identity/Deterministic Field Authority Matrix, unchanged by this Scope
    Reframe): when the evaluator's own values fail
    `_is_valid_subject_ref_judgment`/`_is_valid_rule_id_judgment`, this
    function raises `WireContractError(reason_code=
    "candidate_schema_invalid")` -- never a Python-synthesized fallback
    (PR #2367 fix_delta item 3's fail-closed contract, preserved).

    `evidence_refs[].projection_digest` is always Python-recomputed from
    real `finding_sets` data (`_enrich_evidence_refs`/`real_evidence_index`)
    -- an evaluator-claimed ref with no real backing evidence is dropped,
    never kept with a fabricated digest (`evidence_refs[].projection_digest`
    Authority Matrix row).

    Raises `WireContractError(reason_code="candidate_schema_invalid")` for
    a non-dict `raw_candidate` (defensive -- the outer envelope's
    `candidate_records` field is only type-checked as `list[dict[str,
    Any]]` by `_parse_wire_payload`, which does not itself validate each
    list ELEMENT's type) rather than silently passing it through, since
    every candidate produced by this function must be a genuinely
    schema-eligible dict for the canonical validator (step 4) to assess."""
    if not isinstance(raw_candidate, dict):
        raise WireContractError("candidate_record_not_object", reason_code="candidate_schema_invalid")

    candidate_id = raw_candidate.get("candidate_id")
    judgment = _extract_candidate_identity_judgment(raw_candidate)
    subject_ref = judgment["subject_ref"]
    if not _is_valid_subject_ref_judgment(subject_ref):
        raise WireContractError(
            f"candidate_schema_invalid[subject_ref]:candidate_id={candidate_id!r}:{subject_ref!r}",
            reason_code="candidate_schema_invalid",
        )
    rule_id = judgment["rule_id"]
    if not _is_valid_rule_id_judgment(rule_id):
        raise WireContractError(
            f"candidate_schema_invalid[rule_id]:candidate_id={candidate_id!r}:{rule_id!r}",
            reason_code="candidate_schema_invalid",
        )
    key = {
        "repository_id": repository_id,
        "claim_class": judgment["claim_class"],
        "subject_ref": subject_ref,
        "rule_id": rule_id,
    }
    algorithm = _default_finding_identity_algorithm()
    identity_value = _validate_retrospective_schema_module().compute_finding_identity(key, algorithm=algorithm)

    prev_candidate = _find_previous_candidate(previous_state, identity_value)
    prev_evaluations: list[dict[str, Any]] = []
    if prev_candidate is not None:
        prev_finding_contract = prev_candidate.get("finding_contract")
        if isinstance(prev_finding_contract, dict):
            prev_evaluations = list(prev_finding_contract.get("evaluations") or [])

    classification = _classify_current_candidate_delta(previous_state, identity_value)
    enriched_evidence_refs = _enrich_evidence_refs(
        raw_candidate.get("evidence_refs"), real_evidence_index=real_evidence_index
    )
    new_entry = _build_evaluation_entry(
        classification=classification,
        prev_evaluations=prev_evaluations,
        evidence_refs=enriched_evidence_refs,
        base_sha=base_sha,
        source_set_digest=source_set_digest,
        timestamp=timestamp,
        identity_value=identity_value,
        previous_status=previous_state.status,
    )

    return {
        "candidate_id": candidate_id,
        "candidate_status": "proposed",
        "title": raw_candidate.get("title"),
        "description": raw_candidate.get("description"),
        "source_run_ref": {"base_sha": base_sha, "source_set_digest": source_set_digest},
        "created_at": timestamp,
        "updated_at": timestamp,
        "finding_contract": {
            "schema_version": "v1",
            "identity": {"algorithm": algorithm, "key": key, "value": identity_value},
            "claim_class": judgment["claim_class"],
            "evaluations": prev_evaluations + [new_entry],
        },
    }


def _enrich_evaluation_payload(
    payload: dict[str, Any],
    *,
    repository_id: str,
    base_sha: str,
    source_set_digest: str,
    timestamp: str,
    previous_state: "PreviousStateResult",
    finding_sets: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Apply `_enrich_candidate_record` to every entry of
    `payload["candidate_records"]` (Issue #2362 Scope Reframe). Returns a
    NEW payload dict -- the outer-envelope-parsed, not-yet-validated
    `payload` dict `run_evaluation()` passes in is never mutated in place.

    `real_evidence_index` (`_observer_source_type_index`) is built ONCE
    from `finding_sets` (the evaluator's actual input evidence, i.e.
    `evaluator_request.finding_sets`) and shared across every candidate
    record's `evidence_refs[]` digest recomputation."""
    real_evidence_index = _observer_source_type_index(finding_sets)
    enriched = dict(payload)
    enriched["candidate_records"] = [
        _enrich_candidate_record(
            record,
            repository_id=repository_id,
            base_sha=base_sha,
            source_set_digest=source_set_digest,
            timestamp=timestamp,
            previous_state=previous_state,
            real_evidence_index=real_evidence_index,
        )
        for record in payload.get("candidate_records", [])
    ]
    return enriched


def run_evaluation(
    ctx: RunContext,
    evaluator_request: EvaluatorRequest,
    *,
    invoke_evaluator: Callable[[EvaluatorRequest], AgentInvocationResult],
    repository_id: str,
    previous_state: "PreviousStateResult",
    clock: Callable[[], datetime] = _utcnow,
    repair: Callable[[str, WireContractError], str] | None = None,
) -> Evaluation:
    """Invoke the evaluator exactly once with ``evaluator_request`` (built
    only from validated ``FindingSet`` projections -- see
    ``build_finding_sets``/``prepare_evaluator_request``) and strictly
    validate its output. The caller (``execute_run``/``run_cli``) is
    responsible for only calling this after ``run_observer_wave`` has
    succeeded for every observer in the wave (AC9).

    Issue #2362 (Scope Reframe, 2026-08-28 owner-approved): `Evaluation`
    construction (the canonical candidate-validation firing point, via
    `__post_init__` -> `_post_validate()` -> `_validate_candidate_records()`)
    is deliberately the LAST step here, preceded by a deterministic
    enrichment phase: (1) outer envelope parse -- generic wire shape only
    (shared with `from_wire()` via `_parse_wire_payload()`), WITHOUT
    constructing `Evaluation`; (2)-(3) judgment-only extraction +
    deterministic enrichment -- `_enrich_evaluation_payload()` builds every
    candidate record's ENTIRE canonical shape (`candidate_status`/
    `source_run_ref`/`created_at`/`updated_at`/`finding_contract.identity`/
    `finding_contract.evaluations[]`) from `repository_id` (Python-side
    caller context -- NOT part of `EvaluatorRequest`), `ctx.base_sha`,
    `evaluator_request.source_set_digest`, `clock()`, `previous_state`
    (`compute_delta()`/`PreviousStateProvider`, AC3), and the evaluator's
    own judgment-only `candidate_id`/`title`/`description`/`claim_class`/
    `subject_ref`/`rule_id`/`evidence_refs` values -- the evaluator's wire
    payload is never asked for, and this phase never reads, `identity`/
    `finding_contract`/`evaluations`/`repository_id`/`source_run_ref`/
    `created_at`/`updated_at`; (4) construction --
    `Evaluation(**enriched_payload)` fires canonical candidate validation
    for the FIRST time, against the ENRICHED payload, never the raw
    evaluator output.

    `previous_state` (Issue #2362 Scope Reframe -- AC3) MUST be obtained
    by the caller (`execute_run()`/`run_cli()`) from a
    `PreviousStateProviderProtocol.get()` call BEFORE invoking this
    function (a re-sequencing relative to the pre-Scope-Reframe call graph,
    where `PreviousStateProvider.get()`/`compute_delta()` only ran AFTER
    `run_evaluation()` returned, purely to populate the separate
    `PublishRequest.delta_results` sidecar) -- `finding_contract.evaluations[]`
    construction now needs it too, to append this run's new evaluation
    entry to the correct prior history chain and to classify presence/
    absence deltas via `compute_delta()`.

    Issue #2367 fix_delta item 6: steps (1)-(4) above are now driven as a
    SINGLE unit through `_retry_wire_parse()` (the same shared retry
    helper `parse_agent_output_with_repair()` uses), instead of only
    wrapping step (1) in a `repair` retry loop. A `candidate_schema_invalid`
    raised by step (4) -- which only fires AFTER enrichment, so it could
    never have been observed by a step-(1)-only retry loop -- is now
    retried via `repair` against the ORIGINAL raw evaluator text exactly
    like a step-(1) parse failure would be, and the full parse -> enrich ->
    construct sequence re-runs from scratch on the repaired text. The
    current production call site passes `repair=None`, so this is a
    structural fix to the existing API contract (an unused repair
    opportunity is now available to future callers) rather than an
    immediate behavior change for `repair=None` callers, who still fail
    closed on the first attempt (as `SchemaRepairExhausted`, a
    `WireContractError` subclass, wrapping the same `reason_code`)."""
    result = invoke_evaluator(evaluator_request)
    if result.status != "ok":
        # Issue #2341 AC1: thread the underlying adapter-level
        # reason_code/exit_code through, mirroring run_observer_wave().
        raise EvaluatorInvocationFailed(
            f"evaluator_failed:{result.status}",
            reason_code=result.reason_code,
            exit_code=result.exit_code,
        )
    raw_text = json.dumps(result.structured_output, sort_keys=True, separators=(",", ":"))

    def _parse_enrich_construct(text: str) -> Evaluation:
        # Step 1 (outer envelope parse): generic wire-shape validation only
        # -- Evaluation is NOT constructed here, so canonical candidate
        # validation has not fired yet.
        payload = _parse_wire_payload(Evaluation, text)
        # Steps 2-3 (judgment-only extraction + deterministic enrichment).
        enriched_payload = _enrich_evaluation_payload(
            payload,
            repository_id=repository_id,
            base_sha=ctx.base_sha,
            source_set_digest=evaluator_request.source_set_digest,
            timestamp=_iso(clock()),
            previous_state=previous_state,
            finding_sets=evaluator_request.finding_sets,
        )
        # Step 4 (construction): canonical candidate validation
        # (_post_validate()/_validate_candidate_records()/validate_candidate())
        # fires for the FIRST time here, against the enriched payload.
        try:
            return Evaluation(**enriched_payload)
        except TypeError as exc:  # pragma: no cover - defensive, shape already checked in step 1
            raise WireContractError(f"construction_failed:{exc}", reason_code="construction_failed") from exc

    evaluation = _retry_wire_parse(
        raw_text, _parse_enrich_construct, repair=repair, max_retries=SCHEMA_REPAIR_RETRIES
    )
    if evaluation.run_id != ctx.run_id or evaluation.source_set_digest != evaluator_request.source_set_digest:
        raise EvaluatorInvocationFailed("evaluation_envelope_mismatch")
    return evaluation


# ---------------------------------------------------------------------------
# finalize phase: PublishRequest producer (proposal-only, no mutation)
# ---------------------------------------------------------------------------


def finalize(
    ctx: RunContext,
    plan: SourcePlan,
    evaluation: Evaluation,
    *,
    repository_id: str,
    target_issue: int,
    request_id: str,
    idempotency_key: str,
    expected_previous_digest: str | None = None,
    delta_results: list[dict[str, Any]] | None = None,
    source_observations: list[dict[str, Any]] | None = None,
    runtime_version: str = RUNTIME_VERSION,
) -> PublishRequest:
    """``finalize`` phase: produce the proposal-only ``PublishRequest``. This
    function performs no I/O, no GitHub/Issue mutation, and no filesystem
    write -- it only returns a value (AC11). ``delta_results`` (Issue #2237
    fix_delta iteration-4, Warning 1) is the already-computed
    ``compute_delta()`` output the caller (``execute_run()``/``run_cli()``)
    obtained from a ``PreviousStateProvider`` *before* calling ``finalize`` --
    ``finalize`` itself never calls a provider or ``compute_delta``, so its
    no-I/O guarantee is preserved; ``None``/omitted defaults to an empty list
    (matches every existing direct ``finalize()`` caller that doesn't wire a
    ``PreviousStateProvider``).

    ``source_observations`` (Issue #2238 P0-5 fix_delta) is the canonical
    per-collector acquisition-window observation list ``prepare()`` produced
    (each Child 3 ``CollectorResult.observation``) -- the caller
    (``execute_run()``/``run_cli()``) passes the SAME observations that were
    used to compute ``plan.source_set_digest``, so
    ``persist_retrospective_run.py``'s ``build_run_envelope()`` can persist
    the real acquisition-window coverage instead of inventing a fixed
    single-entry placeholder that didn't match the declared
    ``source_set_digest``. Additive-only: threaded into the EXISTING
    ``run_identity`` dict field's VALUE (``run_identity: dict[str, Any]`` on
    ``PublishRequest`` is untyped/free-form) alongside ``plan.generated_at``
    and ``runtime_version`` -- this deliberately does NOT add a new
    ``PublishRequest`` dataclass field, because
    ``test_run_retrospective.py`` (outside this Issue's Allowed Paths) pins
    ``{f.name for f in dataclasses.fields(rr.PublishRequest)}`` to an exact
    set.

    ``public_projection_digest`` (Issue #2237 P1-2) remains bound to the
    ORIGINAL 3-key ``run_identity`` subset (``run_id``/``base_sha``/
    ``source_set_digest``) and to ``expected_previous_digest`` -- unchanged
    preimage shape, so no existing digest-comparison test's behavior
    changes when ``source_observations``/``runtime_version`` are added."""
    digest_run_identity = {
        "run_id": ctx.run_id,
        "base_sha": ctx.base_sha,
        "source_set_digest": plan.source_set_digest,
    }
    projection = {
        "run_identity": digest_run_identity,
        "candidate_records": evaluation.candidate_records,
        "expected_previous_digest": expected_previous_digest,
    }
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    full_run_identity = dict(digest_run_identity)
    full_run_identity["generated_at"] = plan.generated_at
    full_run_identity["runtime_version"] = runtime_version
    full_run_identity["source_observations"] = list(source_observations) if source_observations is not None else []

    return PublishRequest(
        request_id=request_id,
        repository_id=repository_id,
        target_issue=target_issue,
        run_identity=full_run_identity,
        candidate_records=evaluation.candidate_records,
        expected_previous_digest=expected_previous_digest,
        idempotency_key=idempotency_key,
        public_projection_digest=digest,
        authorization_required=True,
        delta_results=list(delta_results) if delta_results is not None else [],
    )


# ---------------------------------------------------------------------------
# whole-run orchestration helper (composes the four phases; still performs
# no Agent tool call itself -- `invoke`/`invoke_evaluator` are injected by
# the caller; `run_cli` is the production caller that injects real
# `invoke_agent` callbacks)
# ---------------------------------------------------------------------------


def execute_run(
    *,
    base_sha_resolver: Callable[[], str],
    collectors: Sequence[Callable[[str], Any]],
    observer_requests: Sequence[AgentInvocationRequest],
    invoke: Callable[[AgentInvocationRequest], AgentInvocationResult],
    invoke_evaluator: Callable[[EvaluatorRequest], AgentInvocationResult],
    repository_id: str,
    target_issue: int,
    request_id: str,
    idempotency_key: str,
    run_id: str | None = None,
    clock: Callable[[], datetime] = _utcnow,
    previous_state_provider: "PreviousStateProviderProtocol | None" = None,
    previous_state_scope: str = DEFAULT_PREVIOUS_STATE_SCOPE,
) -> PublishRequest:
    """Reference composition of ``prepare`` -> ``validate-observers`` ->
    ``prepare-evaluator`` -> evaluator invocation -> delta computation ->
    ``finalize``. Raises
    ``ObserverWaveFailed``/``EvaluatorInvocationFailed``/``WireContractError``
    fail-closed on any phase failure; never invokes the evaluator unless
    every observer succeeded (AC9).

    ``previous_state_provider`` (Issue #2237 fix_delta iteration-4, Warning
    1) is any ``PreviousStateProviderProtocol`` implementation -- defaults to
    an empty ``FixturePreviousStateProvider`` (every finding classifies as
    ``no_history`` -> ``new``) so this parameter is purely additive and every
    existing caller keeps its prior behavior unless it opts in. The read
    result is fed into ``compute_delta()`` and the resulting per-finding
    classification is attached to the returned ``PublishRequest`` via
    ``finalize(..., delta_results=...)`` -- this is the actual production
    wiring point the standalone ``compute_delta()`` unit tests do not
    exercise."""
    ctx, plan, results = prepare(base_sha_resolver=base_sha_resolver, collectors=collectors, clock=clock, run_id=run_id)
    bundles = run_observer_wave(ctx, plan, invoke=invoke, observer_requests=observer_requests)
    finding_sets = build_finding_sets(ctx, plan, bundles)
    evaluator_request = prepare_evaluator_request(ctx, plan, finding_sets)
    resolved_provider = (
        previous_state_provider if previous_state_provider is not None else FixturePreviousStateProvider(fixtures={})
    )
    # Issue #2362 Scope Reframe: fetched BEFORE run_evaluation() now (moved
    # up from immediately after it) -- run_evaluation()'s deterministic
    # enrichment phase needs `previous_state` to construct
    # `finding_contract.evaluations[]` (AC3), not only this function's own
    # `delta_results` sidecar below.
    previous_state = resolved_provider.get(
        repository_id=repository_id,
        scope=previous_state_scope,
        finding_identity_algorithm=_default_finding_identity_algorithm(),
    )
    evaluation = run_evaluation(
        ctx,
        evaluator_request,
        invoke_evaluator=invoke_evaluator,
        repository_id=repository_id,
        previous_state=previous_state,
        clock=clock,
    )
    delta_results = compute_delta(previous_state, evaluation.candidate_records)
    return finalize(
        ctx,
        plan,
        evaluation,
        repository_id=repository_id,
        target_issue=target_issue,
        request_id=request_id,
        idempotency_key=idempotency_key,
        expected_previous_digest=previous_state.read_version,
        delta_results=delta_results,
        source_observations=[r.observation for r in results],
    )


# ---------------------------------------------------------------------------
# PreviousStateProvider (P0-3): read-only port, fixture/in-memory only
# ---------------------------------------------------------------------------

PREVIOUS_STATE_STATUSES = frozenset({"available", "no_history", "legacy_unavailable", "partial", "stale"})
DELTA_STATUSES = frozenset({"new", "resolved", "recurrent", "regressed", "unchanged"})


@dataclass
class PreviousStateResult:
    """``PREVIOUS_RETROSPECTIVE_STATE_V1``: the read-only port's output
    shape. ``status`` is one of ``PREVIOUS_STATE_STATUSES``. ``candidates``
    holds canonical ``agent_improvement_candidate/v1`` records (Issue #2288/
    #2289) -- delta classification reads ``finding_contract.identity`` /
    ``finding_contract.evaluations[]`` from them, never a private
    ``candidate_status``/``severity`` dialect (Issue #2237 P0-4)."""

    status: str
    previous_run_ref: str | None
    candidates: list[dict[str, Any]]
    read_version: str | None

    def __post_init__(self) -> None:
        if self.status not in PREVIOUS_STATE_STATUSES:
            raise ValueError(f"invalid PreviousStateResult.status: {self.status!r}")


class PreviousStateProviderProtocol(typing.Protocol):
    """Structural type every ``PreviousStateProvider`` implementation
    satisfies (this module's ``FixturePreviousStateProvider`` here; #2238's
    real persistence-backed provider is expected to implement this exact
    ``get()`` signature too). ``execute_run()``/``run_cli()`` accept any
    object implementing this protocol via their ``previous_state_provider``
    parameter (Issue #2237 fix_delta iteration-4, Warning 1) -- this is what
    lets #2238 swap in the real provider without touching the call graph."""

    def get(self, *, repository_id: str, scope: str, finding_identity_algorithm: str) -> "PreviousStateResult": ...


class FixturePreviousStateProvider:
    """Fixture/in-memory ``PreviousStateProvider`` (Issue #2237 scope). The
    persistence-backed production provider (real read + ``read_version``
    optimistic concurrency) is #2238's responsibility and MUST implement this
    exact ``get()`` signature / 5-state output surface."""

    def __init__(self, *, fixtures: dict[tuple[str, str], PreviousStateResult]) -> None:
        self._fixtures = fixtures

    def get(self, *, repository_id: str, scope: str, finding_identity_algorithm: str) -> PreviousStateResult:
        del finding_identity_algorithm  # unused by the fixture provider; part of the port signature
        key = (repository_id, scope)
        if key not in self._fixtures:
            return PreviousStateResult(status="no_history", previous_run_ref=None, candidates=[], read_version=None)
        return self._fixtures[key]


#: ``main()``'s ``--state-backend`` choices (Issue #2238 AC1). ``fixture`` is
#: the default and preserves exact prior behavior (empty
#: ``FixturePreviousStateProvider``); ``issue-comments`` is the real,
#: persistence-backed production backend.
STATE_BACKEND_CHOICES = ("fixture", "issue-comments")


class GhAuthUnavailable(RuntimeError):
    """Issue #2238 P1-3: raised when ``state_backend == "issue-comments"`` is
    requested but ``gh`` authentication is not available/verifiable. This
    module never silently substitutes the fixture backend in that case --
    callers who actually want the fixture backend must pass
    ``--state-backend fixture`` explicitly. ``main()`` converts this into the
    same typed ``{"status": "failed", ...}`` stdout payload as every other
    phase failure."""

    reason_code = "gh_auth_unavailable"


def _check_gh_auth_available(*, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
    """Issue #2238 P1-3: preflight check run before constructing a live
    ``issue-comments`` backend. A module-level function (rather than inlined
    into ``resolve_previous_state_provider``) so tests can
    ``monkeypatch.setattr(rr, "_check_gh_auth_available", ...)`` to bypass
    the real ``gh auth status`` subprocess call without needing a live
    ``gh`` session."""
    try:
        completed = runner(["gh", "auth", "status"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GhAuthUnavailable(f"gh_auth_status_unavailable:{exc}") from exc
    if completed.returncode != 0:
        raise GhAuthUnavailable(f"gh_auth_status_failed:{completed.stderr.strip()}")


def resolve_previous_state_provider(
    *, state_backend: str, repository_id: str, target_issue: int
) -> "PreviousStateProviderProtocol":
    """Build the ``previous_state_provider`` ``main()`` injects into
    ``run_cli()`` (Issue #2238 AC1). This is the actual production wiring
    point: ``main()`` calls this function (not a hand-rolled fixture) and
    passes its result straight into ``run_cli(previous_state_provider=...)``,
    so any test that stubs ``run_cli`` and asserts on the object this
    function returned is exercising the real production call graph, not a
    parallel/duplicate one.

    ``state_backend == "fixture"`` (the default) reproduces the exact prior
    ``main()`` behavior byte-for-byte: an empty ``FixturePreviousStateProvider``
    (every finding classifies as ``no_history`` -> ``new``).

    ``state_backend == "issue-comments"`` lazily loads the sibling
    persistence module (Issue #2238's ``persist_retrospective_run.py``, in
    this Issue's Allowed Paths) and constructs its real
    ``IssueCommentPreviousStateProvider`` -- a persistence-backed provider
    that reads actual prior run publication comments from
    ``repository_id``/``target_issue`` instead of a caller-supplied
    fixture."""
    if state_backend == "fixture":
        return FixturePreviousStateProvider(fixtures={})
    if state_backend == "issue-comments":
        _check_gh_auth_available()
        persist_mod = _persist_retrospective_run_module()
        return persist_mod.IssueCommentPreviousStateProvider(
            repo=repository_id,
            target_issue=target_issue,
            transport=persist_mod.GhCliIssueCommentTransport(),
            trusted_publisher_logins=persist_mod.resolve_trusted_publisher_logins(),
        )
    raise ValueError(f"unknown state_backend: {state_backend!r}. Choices: {STATE_BACKEND_CHOICES}")


def _finding_identity_value(candidate: dict[str, Any]) -> str | None:
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return None
    return finding_contract["identity"]["value"]


def _last_evaluation(candidate: dict[str, Any]) -> dict[str, Any] | None:
    finding_contract = candidate.get("finding_contract")
    if not finding_contract:
        return None
    evaluations = finding_contract.get("evaluations") or []
    return evaluations[-1] if evaluations else None


def compute_delta(previous: PreviousStateResult, current_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify each canonical (``agent_improvement_candidate/v1``,
    #2288/#2289) candidate in ``current_candidates`` against ``previous``
    (Issue #2237 P0-4). Identity is read from
    ``candidate["finding_contract"]["identity"]["value"]`` -- never a
    top-level ``finding_identity`` field, which does not exist in the
    canonical schema. Prior state is read from the *last* entry of
    ``finding_contract.evaluations[]`` on each previous candidate -- never
    from the (unrelated, independent) lifecycle ``candidate_status`` enum
    (which has no ``open``/``resolved`` values; see ADR/#2288). A candidate
    with no ``finding_contract`` (legacy lifecycle-only) has no identity to
    correlate on and is excluded from delta classification.

    Incomplete source coverage on the *previous* read (``partial``/``stale``)
    forces every current candidate's classification to ``indeterminate`` --
    an indeterminate evaluation is never reported as ``resolved`` (absence
    observed under incomplete coverage is not evidence of resolution)."""
    if previous.status in ("no_history", "legacy_unavailable"):
        results: list[dict[str, Any]] = []
        for candidate in current_candidates:
            identity = _finding_identity_value(candidate)
            if identity is None:
                continue
            results.append({"finding_identity": identity, "evaluation_status": "classified", "delta_status": "new"})
        return results

    source_incomplete = previous.status in ("partial", "stale")

    prev_by_identity: dict[str, dict[str, Any] | None] = {}
    for prev_candidate in previous.candidates:
        identity = _finding_identity_value(prev_candidate)
        if identity is not None:
            prev_by_identity[identity] = _last_evaluation(prev_candidate)

    current_identities: set[str] = set()
    results = []
    for candidate in current_candidates:
        identity = _finding_identity_value(candidate)
        if identity is None:
            continue
        current_identities.add(identity)
        if source_incomplete:
            results.append(
                {
                    "finding_identity": identity,
                    "evaluation_status": "indeterminate",
                    "delta_status": None,
                    "indeterminate_reason": "source_partial" if previous.status == "partial" else "source_stale",
                }
            )
            continue
        prev_eval = prev_by_identity.get(identity)
        if identity not in prev_by_identity:
            delta_status = "new"
        elif prev_eval is not None and prev_eval.get("presence_delta") in ("resolved", "still_absent"):
            delta_status = "recurrent"
        else:
            delta_status = "unchanged"
        results.append({"finding_identity": identity, "evaluation_status": "classified", "delta_status": delta_status})

    if not source_incomplete:
        for identity, prev_eval in prev_by_identity.items():
            if identity in current_identities:
                continue
            if prev_eval is not None and prev_eval.get("presence_delta") in ("resolved", "still_absent"):
                continue  # already absent as of the previous run; nothing new to report
            results.append(
                {"finding_identity": identity, "evaluation_status": "classified", "delta_status": "resolved"}
            )

    return results


# ---------------------------------------------------------------------------
# delegated-Agent permission policy / tool callback (P0-5)
# ---------------------------------------------------------------------------

#: (command, subcommand) pairs that are always denied regardless of
#: allowlisting, matched via *tokenized* command parsing (``shlex.split``) so
#: that flag/option insertion (``git -C . commit``, ``gh --repo x/y issue
#: comment``) cannot bypass a naive substring match (Issue #2237 P0-5).
_DENIED_BASH_VERB_PAIRS: dict[str, frozenset[str]] = {
    "git": frozenset({"commit", "push"}),
    "gh": frozenset({"issue", "pr", "api", "comment", "release"}),
}
#: standalone commands denied outright (network egress / remote exec tools).
_DENIED_BASH_STANDALONE_COMMANDS = frozenset({"curl", "wget", "nc", "ncat", "ssh", "scp", "rsync"})
#: shell metacharacters that always deny -- redirection can write files
#: (`printf x > file`), pipes/chains can compose otherwise-innocuous tokens
#: into a denied sequence.
_DENIED_BASH_METACHAR_TOKENS = frozenset({">", ">>", "|", "&&", "||", ";", "`", "$("})
#: interpreters whose inline-execution flag is denied (`python -c '...'` can
#: perform arbitrary git/gh/network calls that a token scan of the outer
#: command line would never see).
_DENIED_INLINE_EXEC_INTERPRETERS = frozenset({"python", "python3"})
_DENIED_INLINE_EXEC_FLAGS = frozenset({"-c"})

_DENIED_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Skill"})

#: environment variable names never forwarded to a delegated Agent's
#: subprocess, regardless of allowlist configuration -- these carry mutation
#: authority (git/gh credentials, cloud credentials, SSH agent socket) that
#: has no legitimate use inside a read-only observer/evaluator invocation
#: (Issue #2237 P0-5).
_MUTATION_CREDENTIAL_ENV_VARS = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GIT_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "NPM_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "NPM_AUTH_TOKEN",
    }
)
#: environment variables always safe to pass through unchanged.
_ENV_PASSTHROUGH_ALLOWLIST = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ"})
#: caller-supplied run-scoped variables (never credentials) are passed
#: through when prefixed like this -- e.g. ``AGENT_RETROSPECTIVE_RUN_ID``.
_RUN_SCOPED_ENV_PREFIX = "AGENT_RETROSPECTIVE_"


class PermissionDenied(Exception):
    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


class DelegatedAgentPermissionPolicy:
    """Permission policy / tool callback enforced by the real invocation path
    (``invoke_agent``, see its ``policy=`` parameter) around every delegated
    observer/evaluator Agent invocation (Issue #2237 P0-5 capability matrix
    "本番制約" column). Denies ``git commit``/``git push``,
    ``gh issue``/``gh pr``/comment/api mutation, filesystem write, any
    non-allowlisted Bash command, and resuming a session belonging to a
    different run.

    ``allowed_bash_commands`` defaults to the empty set, which now means
    **deny all Bash** (Issue #2237 P0-5 fail-open fix -- the prior
    implementation's ``if self.allowed_bash_commands and ...`` guard made an
    *empty* allowlist mean "no restriction", i.e. every non-blacklisted Bash
    command was permitted by default). A command must be both (a) present
    verbatim in ``allowed_bash_commands`` AND (b) pass the tokenized
    denylist scan below -- allowlisting a literal string does not bypass the
    tokenized checks, closing the substring-blacklist bypasses identified in
    OWNER review #2237#issuecomment-5378291560 (``git -C . commit -m x``,
    ``gh --repo owner/repo issue comment 1 --body x``, ``python -c '...'``,
    ``curl -X POST ...``, ``printf data > repository-file``)."""

    def __init__(self, *, run_id: str, allowed_bash_commands: frozenset[str] = frozenset()) -> None:
        self.run_id = run_id
        self.allowed_bash_commands = allowed_bash_commands

    def check_bash(self, command: str) -> None:
        normalized = " ".join(command.split())
        if normalized not in self.allowed_bash_commands:
            raise PermissionDenied("bash_not_allowlisted", command=command)
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise PermissionDenied(f"bash_unparsable:{exc}", command=command) from exc
        if not tokens:
            raise PermissionDenied("bash_empty", command=command)
        lowered_tokens = [tok.lower() for tok in tokens]
        token_set = set(lowered_tokens)
        if token_set & _DENIED_BASH_METACHAR_TOKENS:
            raise PermissionDenied("denied_bash_metacharacter", command=command)
        head = Path(lowered_tokens[0]).name  # strip any leading path component
        if head in _DENIED_BASH_STANDALONE_COMMANDS:
            raise PermissionDenied(f"denied_bash_standalone:{head}", command=command)
        for base_command, denied_subcommands in _DENIED_BASH_VERB_PAIRS.items():
            if base_command in token_set and (token_set & denied_subcommands):
                raise PermissionDenied(f"denied_bash_pattern:{base_command}_mutation", command=command)
        if (token_set & _DENIED_INLINE_EXEC_INTERPRETERS) and (token_set & _DENIED_INLINE_EXEC_FLAGS):
            raise PermissionDenied("denied_bash_pattern:inline_exec", command=command)

    def check_filesystem_write(self, path: str) -> None:
        raise PermissionDenied(f"filesystem_write_denied:{path}", command=f"write:{path}")

    def check_tool(self, tool_name: str) -> None:
        if tool_name in _DENIED_TOOLS:
            raise PermissionDenied(f"tool_denied:{tool_name}", command=tool_name)

    def check_resume(self, session_run_id: str) -> None:
        if session_run_id != self.run_id:
            raise PermissionDenied(f"cross_run_resume_denied:{session_run_id}", command=f"resume:{session_run_id}")

    @property
    def denied_tools(self) -> frozenset[str]:
        return _DENIED_TOOLS

    def sanitize_subprocess_env(self, env: dict[str, str]) -> dict[str, str]:
        """Build the environment actually forwarded to a delegated Agent's
        subprocess (Issue #2237 P0-5): unconditionally excludes
        ``_MUTATION_CREDENTIAL_ENV_VARS`` regardless of what else is
        allowlisted, passes ``_ENV_PASSTHROUGH_ALLOWLIST`` unchanged, and
        passes through caller-supplied run-scoped variables (prefixed
        ``AGENT_RETROSPECTIVE_``, e.g. the run's ``run_id``/``base_sha``) --
        never the full ambient ``os.environ``."""
        sanitized: dict[str, str] = {}
        for key, value in env.items():
            if key in _MUTATION_CREDENTIAL_ENV_VARS:
                continue
            if key in _ENV_PASSTHROUGH_ALLOWLIST or key.startswith(_RUN_SCOPED_ENV_PREFIX):
                sanitized[key] = value
        return sanitized


# ---------------------------------------------------------------------------
# run-scoped temp artifact directory (AC18)
# ---------------------------------------------------------------------------


class RunInterrupted(BaseException):
    """Raised synchronously from within ``run_scoped_temp_dir``'s managed
    block when SIGINT/SIGTERM is delivered to the process (subclasses
    ``BaseException``, matching Python's own ``KeyboardInterrupt``
    convention, so a bare ``except Exception`` elsewhere never accidentally
    swallows it before cleanup runs)."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"run_interrupted:signal={signum}")
        self.signum = signum


@contextlib.contextmanager
def run_scoped_temp_dir(run_id: str, *, base_dir: Path | None = None):
    """Create a run-scoped private temp artifact directory (mode ``0700``)
    and guarantee its removal on every exit path: normal completion,
    exception, ``SIGINT``, and ``SIGTERM`` (AC18). Signal handlers installed
    here are process-global for the duration of the ``with`` block and are
    always restored in ``finally``, regardless of how the block exits.
    Cleanup failure (``shutil.rmtree`` raising) is surfaced -- not silently
    swallowed -- so callers observe a leaked private temp directory rather
    than believing cleanup silently succeeded (Issue #2237 fix_delta gate
    #12: ``test_temp_scope_is_on_production_path_and_cleanup_failure_surfaces``)."""
    base = base_dir if base_dir is not None else Path(tempfile.gettempdir())
    path = base / f"agent-retrospective-run-{run_id}"
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(path, 0o700)  # explicit: `mkdir(mode=...)` is masked by umask

    def _signal_handler(signum: int, _frame: Any) -> None:
        raise RunInterrupted(signum)

    previous_handlers: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, _signal_handler)

    try:
        yield path
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        shutil.rmtree(path)


# ---------------------------------------------------------------------------
# stable executable entrypoint (P0-2): the root Skill invokes this via Bash
# (never the interactive Agent tool). This module then owns the whole
# collector-closure -> observer-manifest -> fan-in -> evaluator ->
# finalize call graph, itself invoking observers/evaluator only via the
# headless CLI subprocess adapter above.
# ---------------------------------------------------------------------------


def build_repository_collector(repo_root: Path) -> Callable[[str], Any]:
    """Bind ``collect_repository_source``'s ``repo_root`` keyword into a
    ``Callable[[base_sha], CollectorResult]`` closure -- the shape
    ``prepare()``'s ``collectors`` sequence requires (Issue #2237 P0-2: each
    Child 3 collector has a different, role-specific required-argument
    shape; only ``collect_repository_source`` takes solely ``base_sha`` as
    its positional parameter, so every other collector MUST be bound into a
    closure like this one before being handed to ``prepare()``)."""
    collect_mod = _collect_snapshot_module()

    def _collect(base_sha: str):
        return collect_mod.collect_repository_source(base_sha, repo_root=repo_root)

    return _collect


#: fixed, non-identity fields used by ``_default_observer_prompt`` below
#: (Issue #2345 fix_delta, OWNER review
#: https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
#: P1 item 1). The *identity* fields (``run_id``/``base_sha``/
#: ``source_set_digest``) are never fixed placeholders -- ``run_cli()``
#: calls ``_default_observer_prompt`` only AFTER its own internal
#: ``prepare()`` step has produced the real ``ctx``/``plan`` for this run,
#: and threads those real values in (see ``run_cli``'s prompt-building
#: step below). An earlier version of this function embedded a fixed
#: placeholder ``run_id`` that could never equal the real one, which made
#: every invocation using it construct-to-fail at
#: ``run_observer_wave()``'s ``bundle.run_id != ctx.run_id`` check instead
#: of exercising the real, functional production call graph end-to-end --
#: that design is no longer used (see the OWNER review URL above).
_DEFAULT_PROMPT_EVIDENCE_REF = "default-prompt-evidence-ref"

#: evidence_ref literal used when binding a caller-supplied (non-empty)
#: task prompt via ``bind_observer_prompt`` below (Issue #2350) -- distinct
#: from ``_DEFAULT_PROMPT_EVIDENCE_REF`` purely so the two prompt shapes
#: remain distinguishable in transcripts; ``run_observer_wave`` never
#: validates ``evidence_ref`` against either literal (only ``run_id`` /
#: ``base_sha`` / ``source_set_digest`` / ``observer_id`` are identity
#: fields checked there).
_CALLER_SUPPLIED_PROMPT_EVIDENCE_REF = "caller-supplied-prompt-evidence-ref"


def bind_observer_prompt(
    task_prompt: str | None,
    *,
    observer_id: str,
    run_id: str,
    base_sha: str,
    source_set_digest: str,
) -> str:
    """Issue #2350: the single identity-binding helper BOTH the
    default-prompt path (``prompts=None``, ``--prompts-file`` omitted) and
    the caller-supplied-prompt path (``--prompts-file`` present, non-empty
    per-observer task text) are threaded through in ``run_cli()``, so
    neither path can construct an observer invocation whose response is
    structurally unable to satisfy ``run_observer_wave()``'s
    ``bundle.run_id != ctx.run_id`` / ``source_set_digest`` / ``base_sha``
    identity checks.

    ``run_cli()`` calls this ONLY after its own internal ``prepare()`` step
    has produced this run's REAL ``ctx.run_id`` / ``ctx.base_sha`` /
    ``plan.source_set_digest`` -- never a fixed placeholder (mirrors the
    ``_default_observer_prompt`` design Issue #2345 fix_delta established,
    OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341).
    Identity remains single-sourced from ``run_cli()``'s own
    ``uuid.uuid4()``-generated ``run_id`` and ``prepare()``'s ``ctx`` /
    ``plan`` output -- this function never lets a caller pre-supply or
    override any of the four identity values (``run_id`` / ``base_sha`` /
    ``source_set_digest`` / ``observer_id``); doing so would let a caller
    pre-generate a ``run_id`` and undermine its role-scoped nonce
    properties (``DelegatedAgentPermissionPolicy`` / the run-scoped temp
    directory / observer identity all key off it) -- see this Issue's Stop
    Conditions.

    Background (Issue #2350): prior to this fix, a caller-supplied,
    substantive investigative prompt passed via ``--prompts-file`` was
    forwarded to the observer CLI verbatim
    (``build_observer_requests()``'s ``prompts[spec.observer_id]``) with NO
    identity-binding instructions at all. An observer receiving such a
    prompt has no way to know this run's real ``run_id`` / ``base_sha`` /
    ``source_set_digest`` and therefore cannot legitimately echo them back
    -- ``run_observer_wave()`` then fail-closed-rejected the mismatched
    response with ``observer_run_id_mismatch`` (or the
    ``source_set_digest`` / ``base_sha`` siblings) on every non-empty
    caller-supplied prompt, a structural gap independently confirmed
    during Issue #2239 and documented as a "known,
    independently-confirmed production architecture gap" in
    ``verify_agent_retrospective_live_smoke.py``'s docstring at the time
    (``run_retrospective.py`` was outside that Issue's Allowed Paths).

    Fix: this helper always appends the SAME real-identity-binding
    boilerplate this module's default prompt has used since Issue #2345 --
    the run's real ``run_id`` / ``base_sha`` / ``source_set_digest`` /
    ``observer_id`` values, with an explicit instruction to echo them
    verbatim in the ``OBSERVER_RESULT_V1`` JSON response -- around whatever
    task text (if any) is supplied. This never asks the caller to
    pre-generate or discover these identity values itself (structurally
    impossible for ``source_set_digest`` before ``prepare()`` runs
    regardless); the SSOT for identity stays exactly where it already was.

    ``task_prompt`` is ``None`` (or empty/whitespace-only) for the
    default-prompt path -- ``_default_observer_prompt`` is now a thin
    wrapper around this function passing ``task_prompt=None``. A non-empty
    ``task_prompt`` is the caller's own investigative instruction text
    (``--prompts-file``'s per-observer value); this function never itself
    decides whether an empty caller-supplied prompt is acceptable -- that
    fail-closed decision remains Issue #2345 P2's ``invalid_observer_prompts``
    check (``_reject_missing_or_empty_prompts``), applied by
    ``run_cli()``/``build_observer_requests()`` before this helper ever
    runs on a caller-supplied prompt.

    PR #2358 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255,
    P1 items 1-2):

    (1) the REAL identity block (``AUTHORITATIVE_RUN_CONTEXT``) is now
    placed BEFORE the caller-supplied task text (``CALLER_TASK_DATA``), not
    after it. A caller-supplied investigative prompt (retrospective session
    evidence) may itself legitimately contain identifier-looking data --
    e.g. a PRIOR run's own ``{"run_id": ..., "base_sha": ...,
    "source_set_digest": ...}`` tuple, quoted verbatim as historical
    evidence -- and the previous (task-text-first) ordering meant a naive
    first-match identity extraction (exactly what a real observer LLM
    reading top-to-bottom, or a hermetic test fake-runner, would plausibly
    do) could pick up that stale embedded tuple instead of THIS run's real
    identity. Emitting the caller's task text as a nested JSON string value
    (``json.dumps({"task": ...})``) additionally means any quote characters
    inside caller-supplied text are JSON-escaped there, so they never
    surface as bare ``"run_id": "..."``-shaped key/value pairs an
    unescaped-quote-based extraction (real or hermetic) would match at all.

    (2) the caller-supplied-prompt branch no longer shows a
    ready-to-submit COMPLETED JSON example with a literal ``"findings":
    []`` in it -- doing so made it trivially easy for an observer to
    satisfy identity validation while reporting zero findings regardless of
    what the caller-supplied task actually asked it to investigate (a
    false-green: ``observer_run_id_mismatch`` disappears, but the
    retrospective becomes substantively empty). The default-prompt branch
    (no caller task -- Issue #2345) keeps its completed-example shape,
    since ``findings: []`` is genuinely the correct terminal answer there
    (no evidence was ever supplied for it to investigate)."""
    has_task = bool(task_prompt is not None and task_prompt.strip())
    identity_block = "AUTHORITATIVE_RUN_CONTEXT\n" + json.dumps(
        {
            "run_id": run_id,
            "base_sha": base_sha,
            "source_set_digest": source_set_digest,
            "observer_id": observer_id,
        },
        sort_keys=True,
    )
    if has_task:
        assert task_prompt is not None  # narrows for mypy; has_task already proved this
        evidence_ref = _CALLER_SUPPLIED_PROMPT_EVIDENCE_REF
        caller_task_block = "CALLER_TASK_DATA\n" + json.dumps({"task": task_prompt.strip()})
        output_rules = (
            "OUTPUT_RULES\n"
            "- Respond with EXACTLY one JSON object (no markdown fence, no prose) "
            "conforming to OBSERVER_RESULT_V1 (EvidenceBundle) with fields: "
            "schema_version, run_id, base_sha, source_set_digest, observer_id, "
            "evidence_ref, findings.\n"
            '- Set "schema_version" to "observer_result/v1".\n'
            "- Copy run_id/base_sha/source_set_digest/observer_id from "
            "AUTHORITATIVE_RUN_CONTEXT above verbatim -- that block, and ONLY "
            "that block, is this run's REAL identity; never invent or alter "
            "these four values.\n"
            "- CALLER_TASK_DATA above may itself contain text that looks like "
            "identity fields (e.g. a prior run's run_id/base_sha/"
            "source_set_digest quoted as evidence) -- such values are ordinary "
            "investigative data, never this run's identity, no matter how they "
            "are formatted.\n"
            f'- Set "evidence_ref" to "{evidence_ref}".\n'
            '- Use CALLER_TASK_DATA\'s "task" field to decide what to '
            'investigate, and populate "findings" with what that investigation '
            "actually found -- use an empty list only when no finding can "
            "genuinely be substantiated from the supplied task/evidence."
        )
    else:
        evidence_ref = _DEFAULT_PROMPT_EVIDENCE_REF
        caller_task_block = (
            "CALLER_TASK_DATA\n"
            "No caller-supplied evidence was provided (this is "
            "run_retrospective.py's own default prompt, used only when "
            "--prompts-file is omitted -- Issue #2345)."
        )
        output_rules = (
            "OUTPUT_RULES\n"
            "Respond with EXACTLY one JSON object (no markdown fence, no prose) "
            "conforming to OBSERVER_RESULT_V1 (EvidenceBundle):\n"
            "{\n"
            '  "schema_version": "observer_result/v1",\n'
            f'  "run_id": "{run_id}",\n'
            f'  "base_sha": "{base_sha}",\n'
            f'  "source_set_digest": "{source_set_digest}",\n'
            f'  "observer_id": "{observer_id}",\n'
            f'  "evidence_ref": "{evidence_ref}",\n'
            '  "findings": []\n'
            "}\n"
            "The run_id/base_sha/source_set_digest/observer_id fields above are "
            "this run's REAL identity (copied verbatim from "
            "AUTHORITATIVE_RUN_CONTEXT above) -- never invent or alter these "
            'four values. Do not invent evidence or findings beyond an empty '
            'findings list. Set "findings" to [].'
        )
    # Issue #2374: fallback opt-in (`agy_advisory_native_fallback_allowed`)
    # and `authoritative_base_sha` are wired ONLY into the substantive
    # caller-supplied-task path (`has_task`) for `codebase-investigator`
    # specifically (AC1) -- never into `retrospective-runtime-observer`'s or
    # `web-researcher`'s prompts, and never into the default/no-task path
    # (AC7). `.claude/agents/codebase-investigator.md`'s own input contract
    # already documents `agy_advisory_native_fallback_allowed`; the caller
    # (this module) is the one that must actually pass it -- prior to this
    # Issue, no wiring path existed at all (see this Issue's "Current
    # Validated Scope"). This does NOT ask the model to override its own
    # native output contract via task-prompt instructions (Issue #2374
    # Outcome: "task prompt への追記だけでは...決定論的に解消できない") --
    # `apply_codebase_investigator_role_adapter` (Python-side) is what
    # deterministically resolves the native/observer contract selection,
    # not this prompt text.
    fallback_policy_block = ""
    if has_task and observer_id == "codebase-investigator":
        fallback_policy_block = "\n\nAGY_ADVISORY_NATIVE_FALLBACK_POLICY\n" + json.dumps(
            {"agy_advisory_native_fallback_allowed": True, "authoritative_base_sha": base_sha},
            sort_keys=True,
        )
    return (
        f"observer_id={observer_id}.\n\n{identity_block}\n\n{caller_task_block}\n\n"
        f"{output_rules}{fallback_policy_block}"
    )


def _default_observer_prompt(observer_id: str, *, run_id: str, base_sha: str, source_set_digest: str) -> str:
    """Issue #2345 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
    P1 items 1-2): the genuinely non-empty, REAL-identity default prompt
    used for ``observer_id`` when ``main()``'s ``--prompts-file`` is not
    supplied. Issue #2350: now a thin wrapper around ``bind_observer_prompt``
    (``task_prompt=None``) -- the SAME identity-binding helper the
    caller-supplied-prompt path in ``run_cli()`` also threads through, so
    both prompt-construction paths can never diverge in how they embed
    ``run_id`` / ``base_sha`` / ``source_set_digest`` / ``observer_id``."""
    return bind_observer_prompt(
        None,
        observer_id=observer_id,
        run_id=run_id,
        base_sha=base_sha,
        source_set_digest=source_set_digest,
    )


def _reject_missing_or_empty_prompts(prompts: dict[str, str]) -> None:
    """Issue #2345 fix_delta P2 item 3 (OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341):
    every ``observer_id`` in ``EXPECTED_OBSERVER_MANIFEST`` MUST have a
    non-empty (post-``strip()``) prompt in ``prompts`` -- a missing key or
    an empty/whitespace-only string is rejected fail-closed with a typed
    ``WireContractError`` (``reason_code="invalid_observer_prompts"``)
    here, locally, before any ``claude`` CLI subprocess is ever invoked or
    (Issue #2350) any identity is bound onto the prompt text. Shared by
    ``build_observer_requests`` (direct callers) and ``run_cli``'s
    caller-supplied-prompt branch (applied to the RAW, pre-
    ``bind_observer_prompt`` task text -- Issue #2350 never lets
    identity-binding paper over a genuinely empty caller-supplied prompt,
    since ``bind_observer_prompt`` always returns non-empty text
    regardless of its ``task_prompt`` argument).

    PR #2358 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255,
    P2): validates ``isinstance(value, str)`` BEFORE calling ``.strip()`` on
    it. The previous ``str(prompts.get(observer_id, "")).strip()`` coerced
    ANY value to its ``str()`` representation FIRST -- so a non-string
    ``--prompts-file`` value (e.g. JSON ``null`` decoded to Python
    ``None``) became the string ``"None"``, which is non-empty and
    therefore spuriously PASSED this check, only to later crash with an
    untyped ``AttributeError`` inside ``bind_observer_prompt()``'s own
    ``task_prompt.strip()`` call (``main()``'s exception handler does not
    catch ``AttributeError``, so this escaped as a raw traceback instead of
    the typed ``invalid_observer_prompts`` failure this function exists to
    produce)."""
    missing_or_empty = [
        spec.observer_id
        for spec in EXPECTED_OBSERVER_MANIFEST
        if not isinstance(prompts.get(spec.observer_id), str) or not prompts[spec.observer_id].strip()
    ]
    if missing_or_empty:
        raise WireContractError(
            f"invalid_observer_prompts:missing_or_empty={sorted(missing_or_empty)}",
            reason_code="invalid_observer_prompts",
        )


def build_observer_requests(
    *,
    schema_dir: Path,
    cwd: str,
    prompts: dict[str, str],
    timeout_sec: int = 300,
    caller_supplied_task_path: bool = False,
) -> list[AgentInvocationRequest]:
    """Build the exact 3-observer ``AgentInvocationRequest`` list matching
    ``EXPECTED_OBSERVER_MANIFEST`` (Issue #2237 P0-2/P0-6). ``prompts`` maps
    each ``observer_id`` to the prompt text the caller (the root Skill via
    ``main``'s ``--prompts-file``, or ``run_cli``'s own
    ``bind_observer_prompt``-bound prompts -- Issue #2345/#2350) has
    already assembled -- this function never resolves session/evidence
    content itself (that remains the root Skill's trigger-time
    responsibility), and never itself performs identity-binding (that is
    ``bind_observer_prompt``'s sole responsibility, applied by callers
    before this function ever sees the prompt text).

    Issue #2345 fix_delta P2 item 3: every ``observer_id`` in
    ``EXPECTED_OBSERVER_MANIFEST`` MUST have a non-empty (post-``strip()``)
    prompt in ``prompts`` -- see ``_reject_missing_or_empty_prompts``.

    ``caller_supplied_task_path`` (Issue #2374, default ``False``): ``True``
    only when ``run_cli()``'s caller supplied a ``--prompts-file`` (the
    substantive caller-supplied-task path, as opposed to the default/no-task
    path). When ``True``, ONLY the ``codebase-investigator`` request gets
    ``role_adapter=_ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1`` (AC1) --
    ``retrospective-runtime-observer`` and ``web-researcher`` never get a
    ``role_adapter`` regardless of this flag. The default ``False`` keeps
    every pre-#2374 direct caller of this function (which never passes this
    new keyword-only argument) producing the exact same 3 requests as
    before -- ``role_adapter=None`` on all of them."""
    _reject_missing_or_empty_prompts(prompts)
    return [
        AgentInvocationRequest(
            agent_name=spec.observer_id,
            prompt=prompts[spec.observer_id],
            json_schema_path=str(schema_dir / "observer_result_v1.schema.json"),
            cwd=cwd,
            timeout_sec=timeout_sec,
            role_adapter=(
                _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
                if caller_supplied_task_path and spec.observer_id == "codebase-investigator"
                else None
            ),
        )
        for spec in EXPECTED_OBSERVER_MANIFEST
    ]


def run_cli(
    *,
    repo_root: Path,
    repository_id: str,
    target_issue: int,
    request_id: str,
    idempotency_key: str,
    schema_dir: Path,
    prompts: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    git_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], datetime] = _utcnow,
    run_id: str | None = None,
    temp_base_dir: Path | None = None,
    previous_state_provider: "PreviousStateProviderProtocol | None" = None,
    previous_state_scope: str = DEFAULT_PREVIOUS_STATE_SCOPE,
) -> PublishRequest:
    """The single production call graph (Issue #2237 P0-2): manual-trigger
    preflight -> run-scoped temp dir -> collector closures -> ``prepare`` ->
    exact-manifest observer wave (via the real headless CLI subprocess
    adapter, permission-policy-wrapped) -> fan-in with role-authority tagging
    -> ``prepare-evaluator`` -> evaluator invocation -> delta computation
    (``PreviousStateProvider`` -> ``compute_delta()``, Issue #2237 fix_delta
    iteration-4 Warning 1) -> ``finalize``. Returns the proposal-only
    ``PublishRequest`` (raises a typed exception on any phase failure --
    ``main()`` converts that into a typed-failure stdout payload and
    non-zero exit code). ``previous_state_provider`` defaults to an empty
    ``FixturePreviousStateProvider`` -- a real, persistence-backed provider
    (#2238) can be injected here without any other change to this call
    graph.

    ``prompts`` (Issue #2345 fix_delta, OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
    P1 item 1; Issue #2350): ``None`` (the default, matching ``main()``
    when ``--prompts-file`` is omitted) means "build the default observer
    prompts AFTER this call graph's own ``prepare()`` step below has
    produced the REAL ``ctx.run_id``/``ctx.base_sha``/
    ``plan.source_set_digest`` for this run" -- never a fixed placeholder
    identity. A caller-supplied dict (from ``--prompts-file``, or a direct
    test/Skill caller) has its per-observer task text validated non-empty
    (every manifest ``observer_id`` must map to a non-empty prompt --
    ``_reject_missing_or_empty_prompts``) and then bound to this SAME real
    identity via ``bind_observer_prompt`` (Issue #2350) -- it is never
    forwarded to the observer CLI as raw, unbound task text, which
    previously left every non-empty caller-supplied prompt structurally
    unable to satisfy ``run_observer_wave()``'s identity checks."""
    manual_trigger_preflight(repo_root=repo_root)
    resolved_run_id = run_id or str(uuid.uuid4())
    policy = DelegatedAgentPermissionPolicy(run_id=resolved_run_id)

    def _base_sha_resolver() -> str:
        completed = git_runner(
            ["git", "rev-parse", "main"], cwd=str(repo_root), capture_output=True, text=True, timeout=30
        )
        if completed.returncode != 0:
            raise ValueError(f"base_sha_resolution_failed:{completed.stderr}")
        return completed.stdout.strip()

    with run_scoped_temp_dir(resolved_run_id, base_dir=temp_base_dir):
        collectors = [build_repository_collector(repo_root)]
        ctx, plan, results = prepare(
            base_sha_resolver=_base_sha_resolver, collectors=collectors, clock=clock, run_id=resolved_run_id
        )
        # Issue #2350: BOTH the caller-supplied-prompt path (`prompts`
        # not None, from `--prompts-file`) and the default-prompt path
        # (`prompts is None`) are threaded through the SAME identity-
        # binding helper (`bind_observer_prompt`), using the REAL
        # `ctx.run_id` / `ctx.base_sha` / `plan.source_set_digest` this
        # call graph's own `prepare()` step (above) just produced -- never
        # a fixed placeholder. For the caller-supplied path, the RAW
        # (pre-binding) prompt text is validated non-empty first via
        # `_reject_missing_or_empty_prompts` (Issue #2345 P2 item 3);
        # `bind_observer_prompt` always returns non-empty text regardless
        # of its `task_prompt` argument, so this raw-text check MUST run
        # before binding or it would never fire on a genuinely empty
        # caller-supplied prompt.
        if prompts is not None:
            _reject_missing_or_empty_prompts(prompts)
            resolved_prompts = {
                spec.observer_id: bind_observer_prompt(
                    prompts[spec.observer_id],
                    observer_id=spec.observer_id,
                    run_id=ctx.run_id,
                    base_sha=ctx.base_sha,
                    source_set_digest=plan.source_set_digest,
                )
                for spec in EXPECTED_OBSERVER_MANIFEST
            }
        else:
            # PR #2358 fix_delta P3 (OWNER review
            # https://github.com/squne121/loop-protocol/pull/2358#issuecomment-5437414255):
            # route through `_default_observer_prompt` (rather than calling
            # `bind_observer_prompt(None, ...)` directly, duplicating what
            # that thin wrapper already does) so the abstraction is
            # actually exercised by the one production call site that
            # needs it, instead of being a dead compatibility wrapper.
            resolved_prompts = {
                spec.observer_id: _default_observer_prompt(
                    spec.observer_id,
                    run_id=ctx.run_id,
                    base_sha=ctx.base_sha,
                    source_set_digest=plan.source_set_digest,
                )
                for spec in EXPECTED_OBSERVER_MANIFEST
            }
        # Issue #2374 AC1: `caller_supplied_task_path=(prompts is not None)`
        # -- the substantive caller-supplied-task path (`--prompts-file`)
        # is the ONLY path that gets `role_adapter` wired onto the
        # `codebase-investigator` request; `prompts is None` (default/
        # no-task path) always produces `role_adapter=None` on every
        # request (AC7).
        observer_requests = build_observer_requests(
            schema_dir=schema_dir,
            cwd=str(repo_root),
            prompts=resolved_prompts,
            caller_supplied_task_path=(prompts is not None),
        )

        def _invoke(request: AgentInvocationRequest) -> AgentInvocationResult:
            run_scoped_env = {
                **request.env,
                f"{_RUN_SCOPED_ENV_PREFIX}RUN_ID": ctx.run_id,
                f"{_RUN_SCOPED_ENV_PREFIX}BASE_SHA": ctx.base_sha,
            }
            return invoke_agent_with_role_adapter(
                dataclasses.replace(request, env=run_scoped_env), ctx=ctx, plan=plan, runner=runner, policy=policy
            )

        bundles = run_observer_wave(
            ctx, plan, invoke=_invoke, observer_requests=observer_requests, expected_manifest=EXPECTED_OBSERVER_MANIFEST
        )
        source_digest_registry = build_source_digest_registry(results)
        finding_sets = build_finding_sets(ctx, plan, bundles, source_digest_registry=source_digest_registry)
        evaluator_request = prepare_evaluator_request(ctx, plan, finding_sets)

        evaluator_agent_request = AgentInvocationRequest(
            agent_name="retrospective-evaluator",
            prompt=evaluator_request.to_wire(),
            json_schema_path=str(schema_dir / "evaluation_result_v1.schema.json"),
            cwd=str(repo_root),
            env={f"{_RUN_SCOPED_ENV_PREFIX}RUN_ID": ctx.run_id, f"{_RUN_SCOPED_ENV_PREFIX}BASE_SHA": ctx.base_sha},
        )

        def _invoke_evaluator(_request: EvaluatorRequest) -> AgentInvocationResult:
            return invoke_agent(evaluator_agent_request, runner=runner, policy=policy)

        resolved_provider = (
            previous_state_provider
            if previous_state_provider is not None
            else FixturePreviousStateProvider(fixtures={})
        )
        # Issue #2362 Scope Reframe: fetched BEFORE run_evaluation() now --
        # see execute_run()'s matching comment for why.
        previous_state = resolved_provider.get(
            repository_id=repository_id,
            scope=previous_state_scope,
            finding_identity_algorithm=_default_finding_identity_algorithm(),
        )
        evaluation = run_evaluation(
            ctx,
            evaluator_request,
            invoke_evaluator=_invoke_evaluator,
            repository_id=repository_id,
            previous_state=previous_state,
            clock=clock,
        )
        delta_results = compute_delta(previous_state, evaluation.candidate_records)
        publish_request = finalize(
            ctx,
            plan,
            evaluation,
            repository_id=repository_id,
            target_issue=target_issue,
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_previous_digest=previous_state.read_version,
            delta_results=delta_results,
            source_observations=[r.observation for r in results],
        )
    return publish_request


def main(argv: Sequence[str] | None = None) -> int:
    """Stable executable entrypoint (Issue #2237 P0-2). The root Skill (see
    ``SKILL.md``'s Procedure) invokes this via a single Bash call; this
    module owns everything downstream. Prints the ``PublishRequest``
    envelope (success) or a typed ``{"status": "failed", "reason_code":
    ..., "reason": ...}`` payload (failure) to stdout; exit code is ``0`` on
    success and ``1`` on any typed phase failure."""
    parser = argparse.ArgumentParser(
        prog="run_retrospective.py",
        description=(
            "agent-retrospective stable executable entrypoint (Issue #2237). "
            "Invoked by the root Skill via Bash -- never via the interactive Agent tool."
        ),
    )
    parser.add_argument("--repo-root", default=str(_REPO_ROOT))
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--target-issue", type=int, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--schema-dir", default=str(_SCRIPTS_DIR / "schemas"))
    parser.add_argument("--prompts-file", default=None, help="JSON file: {observer_id: prompt_text}")
    parser.add_argument(
        "--state-backend",
        choices=STATE_BACKEND_CHOICES,
        default="issue-comments",
        help=(
            "PreviousStateProvider backend (Issue #2238 AC1, P1-3 fix_delta). "
            "'issue-comments' (default, production) reads real prior run "
            "publication comments via persist_retrospective_run.py's "
            "IssueCommentPreviousStateProvider -- this never silently falls "
            "back to 'fixture' if gh auth is unavailable, it fails closed "
            "with a typed gh_auth_unavailable error instead. Tests that "
            "actually want the empty FixturePreviousStateProvider must pass "
            "'--state-backend fixture' explicitly."
        ),
    )
    args = parser.parse_args(argv)

    # Issue #2345 fix_delta (OWNER review
    # https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
    # P1 item 1): when `--prompts-file` is omitted, `prompts` is left as
    # `None` -- `run_cli()` builds the default prompts itself, AFTER its
    # own internal `prepare()` step has produced this run's REAL
    # `ctx.run_id`/`ctx.base_sha`/`plan.source_set_digest` (see
    # `_default_observer_prompt`'s docstring). `main()` cannot build those
    # defaults here because `run_id`/`base_sha` do not exist yet at this
    # point in the call graph.
    prompts: dict[str, str] | None = None
    if args.prompts_file:
        prompts = json.loads(Path(args.prompts_file).read_text(encoding="utf-8"))

    try:
        previous_state_provider = resolve_previous_state_provider(
            state_backend=args.state_backend,
            repository_id=args.repository_id,
            target_issue=args.target_issue,
        )
        publish_request = run_cli(
            repo_root=Path(args.repo_root),
            repository_id=args.repository_id,
            target_issue=args.target_issue,
            request_id=args.request_id,
            idempotency_key=args.idempotency_key,
            schema_dir=Path(args.schema_dir),
            prompts=prompts,
            previous_state_provider=previous_state_provider,
        )
    except (ObserverWaveFailed, EvaluatorInvocationFailed, WireContractError, ValueError, GhAuthUnavailable) as exc:
        # Issue #2341 AC1: additive diagnosability -- exit_code (when the
        # underlying failure traces back to a real subprocess Agent
        # invocation, e.g. ObserverWaveFailed/EvaluatorInvocationFailed) is
        # now included alongside reason_code. Exceptions that never carry an
        # exit_code (WireContractError, ValueError, GhAuthUnavailable) fall
        # back to None via getattr's default, unchanged from prior behavior.
        reason_code = getattr(exc, "reason_code", type(exc).__name__)
        exit_code = getattr(exc, "exit_code", None)
        print(
            json.dumps(
                {"status": "failed", "reason_code": reason_code, "exit_code": exit_code, "reason": str(exc)},
                sort_keys=True,
            )
        )
        return 1

    print(publish_request.to_wire())
    return 0


if __name__ == "__main__":
    sys.exit(main())
