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
import copy
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
from pathlib import Path, PurePosixPath
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


def _load_module_from_path(module_name: str, module_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _load_sibling_module(module_name: str, filename: str):
    return _load_module_from_path(module_name, _SCRIPTS_DIR / filename)


def _validate_repo_evidence_ref_module():
    """Reuse ``gemini-cli-headless-delegation``'s already-tested
    ``REPO_EVIDENCE_REF_V1`` byte-level validator (``git show <commit_sha>:
    <path>`` + ``excerpt_sha256`` recomputation, permalink/verification_status
    cross-checks) instead of reimplementing base_sha-bound evidence
    verification (Issue #2374, OWNER review
    #2387#issuecomment-5459502795 P0-4: ``capability-matrix.md`` requires
    materializing the input via ``git show <base_sha>:<path>`` rather than
    treating the current worktree as authority; read-only git commands only,
    no sandbox/AST parser/receipt system). Loaded lazily via absolute path --
    a different skill's script, outside this Issue's Allowed Paths, never
    modified, only imported read-only."""
    module_path = (
        _REPO_ROOT
        / ".claude"
        / "skills"
        / "gemini-cli-headless-delegation"
        / "scripts"
        / "validate_repo_evidence_ref.py"
    )
    return _load_module_from_path("agent_retrospective_validate_repo_evidence_ref", module_path)


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


def _private_audit_resolver_module():
    """Lazily load Issue #2376 (#1939 Workstream 5)'s private-local-audit
    resolver sibling script (``private_audit_resolver.py``, this Issue's own
    Allowed Paths). Loaded lazily like every other sibling module above, so
    importing ``run_retrospective`` never requires it unless a caller
    actually registers a private audit ref (``register_private_audit_ref``
    below)."""
    return _load_sibling_module("agent_retrospective_private_audit_resolver", "private_audit_resolver.py")


def register_private_audit_ref(
    evidence_ref: dict[str, Any],
    run_identity: dict[str, Any],
    *,
    private_content: Any,
    audit_root: Path | None = None,
) -> dict[str, Any] | None:
    """Issue #2376 (#1939 Workstream 5) generation-time sidecar PRODUCER hook
    (mandatory per the OWNER contract-repair anchor comment,
    issuecomment-5551373964 blocker 1: a resolver with no producer can only
    ever return ``unavailable``, since a public ``evidence_ref`` alone
    cannot be used to locate its own private local audit evidence).

    Registers a private-audit sidecar mapping for ONE already-generated,
    already public-safe ``evidence_ref`` ONLY when ``private_content`` --
    the ACTUAL real evidence data this run already collected and used to
    compute that same ``evidence_ref``'s ``projection_digest`` (e.g. one
    ``_observer_source_type_index()`` entry) -- is truthy, i.e. a local
    private source already exists this run. Never fabricates a private
    source: a falsy ``private_content`` is a no-op (``None`` returned, no
    manifest written) -- see ``private_audit_resolver.
    register_private_audit_ref()`` for the actual atomic local-only storage
    write this delegates to.

    Best-effort / fail-open by design (mirrors the existing Latitude
    evidence binding convention in ``execute_run()`` immediately below,
    which never blocks or fails the retrospective run on
    unavailability/failure): any exception raised while resolving/writing
    local storage is swallowed and ``None`` is returned, so a local-audit
    storage problem never turns into a retrospective run failure."""
    if not private_content:
        return None
    try:
        resolver = _private_audit_resolver_module()
        root = audit_root if audit_root is not None else resolver.default_audit_root(_REPO_ROOT)
        return resolver.register_private_audit_ref(
            evidence_ref=evidence_ref,
            run_identity=run_identity,
            private_content=private_content,
            audit_root=root,
        )
    except Exception:
        return None


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

#: environment variable names never forwarded to a delegated Agent's
#: subprocess, regardless of what else is present (Issue #2445 AC1:
#: replaces the prior allowlist-only design). These carry mutation
#: authority (git/gh credentials, cloud credentials, SSH agent socket) that
#: has no legitimate use inside a read-only observer/evaluator invocation
#: (Issue #2237 P0-5). Everything else -- including provider transport env
#: vars such as ``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN``/
#: ``ANTHROPIC_MODEL``/``CLAUDE_CONFIG_DIR`` -- is inherited from the parent
#: environment by default (Issue #2445; mirrors the sibling
#: ``plugins/agent-retrospective/skills/run/scripts/run_retrospective.py``
#: implementation's denylist semantics, which motivated this replacement).
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

# ---------------------------------------------------------------------------
# codebase-investigator role adapter (Issue #2374; schema-selection/
# verification redesign per OWNER review
# #2387#issuecomment-5459502795 P0-1/P0-3/P0-4)
# ---------------------------------------------------------------------------

#: ``AgentInvocationRequest.role_adapter`` value that opts a codebase-
#: investigator invocation into the native ``CODEBASE_INVESTIGATION_RESULT_V1``
#: contract: ``build_agent_invocation_argv`` passes THIS shape's schema (not
#: ``observer_result_v1.schema.json``) to ``--json-schema``, and role-adapted
#: conversion into ``EvidenceBundle``/``OBSERVER_RESULT_V1`` happens
#: afterwards (see ``apply_codebase_investigator_role_adapter``). Only
#: ``build_observer_requests()``'s substantive-caller-supplied-task
#: codebase-investigator branch ever sets this -- the CLI-level schema
#: passed for a given request is now a direct, deterministic function of
#: this flag rather than the pre-fix_delta dual observer/native structural
#: probe (OWNER review P0-1: "二方式を混在させず、role_adapter の有無で
#: 分岐する")."""
_ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1 = "codebase_investigator_observer_v1"

#: On-disk filename (under ``schemas/``, alongside ``observer_result_v1.
#: schema.json``) of the native ``CODEBASE_INVESTIGATION_RESULT_V1`` wire
#: schema (Issue #2374 Allowed Paths note: a native JSON Schema file is
#: added as a Scope Delta specifically because this design -- OWNER review
#: P0-1 -- requires passing REAL schema *content* to the ``claude`` CLI's
#: ``--json-schema`` flag, not just a Python-side structural check).
_CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME = "codebase_investigation_result_v1.schema.json"

_CODEBASE_INVESTIGATION_RESULT_SCHEMA_PATH = _SCRIPTS_DIR / "schemas" / _CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME


def _codebase_investigation_result_schema() -> dict[str, Any]:
    """Load the native ``CODEBASE_INVESTIGATION_RESULT_V1`` JSON Schema
    (``schema_version`` const ``1`` -- distinct from, and never confusable
    with, ``observer_result_v1.schema.json``'s ``schema_version`` const
    string ``"observer_result/v1"``). Used both as the ``--json-schema`` CLI
    argument content (``build_observer_requests``) and for real
    ``jsonschema.validate`` re-verification of the native result
    (``adapt_native_codebase_investigation_result`` -- OWNER review P0-3:
    replaces the prior structural 8-key-and-``schema_version`` recognizer,
    which could not reject a wrong-typed ``investigation_route``/
    ``impact_scope``, an incomplete ``REPO_EVIDENCE_REF_V1``, a malformed
    ``source_evidence_result``, or a ``status: ok`` / non-null
    ``failure_reason`` contradiction)."""
    return json.loads(_CODEBASE_INVESTIGATION_RESULT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _stdout_excerpt(text: str | None) -> str | None:
    if not text:
        return None
    return text[:_MAX_STDOUT_EXCERPT]


def _default_sanitized_env(env: dict[str, str]) -> dict[str, str]:
    """Issue #2445 AC1: inherit the parent environment by default, stripping
    only ``_MUTATION_CREDENTIAL_ENV_VARS`` (see that constant's docstring).
    Used as the fallback when no ``DelegatedAgentPermissionPolicy`` is
    supplied (defensive fallback only -- production callers always supply a
    policy; see ``run_cli``)."""
    return {k: v for k, v in env.items() if k not in _MUTATION_CREDENTIAL_ENV_VARS}


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

    OWNER review #2387#issuecomment-5459502795 (P0-1): this function no
    longer mixes two schemas together (the pre-fix_delta behavior probed
    every observer-schema-INVALID candidate against a separate, structural
    native recognizer). ``request.json_schema_path`` is now ITSELF a
    deterministic function of ``role_adapter``
    (``build_observer_requests``/``build_agent_invocation_argv``): a
    ``role_adapter is None`` request always receives
    ``observer_result_v1.schema.json``, and a
    ``role_adapter == _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1``
    request always receives ``codebase_investigation_result_v1.schema.json``
    (the native contract's OWN schema). This function therefore validates
    every candidate against EXACTLY ONE schema -- whichever one this
    specific invocation was actually given -- and never structurally guesses
    which of two shapes a candidate might be. ``matched_kind`` is
    ``"native"`` when ``role_adapter`` is the codebase-investigator marker
    (the payload is then the NOT-YET-CONVERTED native
    ``CODEBASE_INVESTIGATION_RESULT_V1`` dict; conversion into
    ``EvidenceBundle``/``OBSERVER_RESULT_V1`` remains
    ``apply_codebase_investigator_role_adapter``'s responsibility, not this
    function's), else ``"observer"`` -- exactly mirroring the pre-#2374
    behavior for every other caller (``role_adapter is None``)."""
    result_text = payload.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        return _RecoveredStructuredOutput(None, None)
    try:
        schema = json.loads(Path(json_schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _RecoveredStructuredOutput(None, None)

    fenced_candidates = _iter_fenced_json_candidates(result_text)
    candidate_texts = fenced_candidates if fenced_candidates else [result_text.strip()]

    is_native = role_adapter == _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1
    schema_valid: list[dict[str, Any]] = []
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
        except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError):
            continue
        schema_valid.append(candidate)

    diagnostics_kwargs = {
        "result_fence_count": len(fenced_candidates),
        "json_candidate_count": json_candidate_count,
        "observer_schema_valid_candidate_count": 0 if is_native else len(schema_valid),
        "native_schema_valid_candidate_count": len(schema_valid) if is_native else 0,
        "observed_top_level_keys": observed_top_level_keys,
    }

    if len(schema_valid) == 1:
        return _RecoveredStructuredOutput(schema_valid[0], "native" if is_native else "observer", **diagnostics_kwargs)
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
        # Issue #2419: registers `retrospective_bash_guard_hook.py` as a
        # real `PreToolUse` hook for this subprocess (`--disallowedTools`
        # alone never covered Bash -- it only ever named
        # Write/Edit/MultiEdit/NotebookEdit/Agent/Skill).
        if policy.settings_path:
            argv += ["--settings", policy.settings_path]
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
        # OWNER review #2387#issuecomment-5459502795 (P0-1): when this
        # invocation's role_adapter is the codebase-investigator marker,
        # `--json-schema` was ALREADY the native schema (see
        # `build_observer_requests`), so a directly-populated
        # `structured_output` dict here is a native-contract candidate, not
        # an observer-contract one -- it must still be routed through
        # `apply_codebase_investigator_role_adapter`'s conversion, exactly
        # like a result-text-recovered native match. Prior to this fix, this
        # branch never set the `native_role_adapter_candidate` marker, so a
        # `claude` CLI response that populated `structured_output` directly
        # (rather than needing `_structured_output_from_result_compat`
        # recovery) silently skipped role-adapter conversion and returned
        # the raw native dict as if it were already an EvidenceBundle.
        if request.role_adapter == _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1:
            matched_kind = "native"
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


def _is_repo_relative_path(path: Any) -> bool:
    """OWNER review #2387#issuecomment-5459502795 (P0-4 step 1): reject any
    ``REPO_EVIDENCE_REF_V1.path`` that is not a plain, repo-relative POSIX
    path -- an absolute path (``/etc/passwd``), a home-relative path
    (``~/...``), or a path containing a ``..`` traversal segment is never
    passed to ``git show``/``git cat-file``."""
    if not isinstance(path, str) or not path:
        return False
    if path.startswith("/") or path.startswith("~"):
        return False
    if ".." in PurePosixPath(path).parts:
        return False
    return True


def _verify_repo_evidence_ref_bytes(
    ref: dict[str, Any],
    *,
    repo_root: Path,
    blob_bytes_getter: Callable[[str, str], bytes] | None = None,
) -> None:
    """Independently re-verify ONE ``REPO_EVIDENCE_REF_V1``'s excerpt bytes
    against the actual git blob at its own ``commit_sha`` (OWNER review
    #2387#issuecomment-5459502795 P0-4): the Agent's self-reported
    ``commit_sha``/``excerpt_sha256`` pairing is never trusted at face
    value. ``capability-matrix.md`` requires materializing the input via
    ``git show <base_sha>:<path>`` rather than treating the current worktree
    as authority -- a caller that reports an unbound worktree byte's hash
    alongside a correct-looking ``commit_sha`` string must be rejected, not
    merely string-compared.

    Delegates the actual byte-level verification (``git show`` + recomputed
    ``excerpt_sha256``, line-range/EOF handling, permalink/verification_status
    cross-checks) to the existing, already-tested
    ``validate_repo_evidence_ref`` (``gemini-cli-headless-delegation`` skill,
    Issue #248/#1920) instead of reimplementing it -- read-only git commands
    only, no sandbox/Bash AST parser/receipt system (OWNER review: "sandbox/
    Bash AST parser/receipt systemは不要、read-only Gitコマンドのみで良い").
    Raises ``NativeResultAdaptationFailed`` (never returns a soft/advisory
    result) unless that validator's own verdict is ``status == "verified"``
    -- an ``"inconclusive"`` verdict (e.g. the path does not exist at
    ``commit_sha``, or the recomputed hash does not match) is fail-closed
    here exactly like a ``"rejected"`` one; this adapter's stricter
    native-fallback acceptance bar never treats "inconclusive" as
    good-enough. ``blob_bytes_getter`` is dependency-injected ONLY by this
    module's own hermetic tests (mirrors ``validate_repo_evidence_ref``'s
    own unit-test seam) -- every production call site leaves it ``None``,
    which routes to a real ``git show`` subprocess."""
    path = ref.get("path")
    if not _is_repo_relative_path(path):
        raise NativeResultAdaptationFailed("native_result_evidence_path_not_repo_relative")
    validator = _validate_repo_evidence_ref_module()
    result = validator.validate_repo_evidence_ref(ref, repo_root=repo_root, blob_bytes_getter=blob_bytes_getter)
    if result.get("status") != "verified":
        raise NativeResultAdaptationFailed("native_result_evidence_bytes_unverified")


def adapt_native_codebase_investigation_result(
    native_result: dict[str, Any],
    *,
    run_id: str,
    base_sha: str,
    source_set_digest: str,
    observer_id: str,
    repo_root: Path,
    blob_bytes_getter: Callable[[str, str], bytes] | None = None,
) -> dict[str, Any]:
    """Role adapter (Issue #2374, redesigned per OWNER review
    #2387#issuecomment-5459502795): converts a recognized native
    ``CODEBASE_INVESTIGATION_RESULT_V1`` dict (produced during an AGY
    advisory native fallback -- ``.claude/agents/codebase-investigator.md``)
    into an ``EvidenceBundle``-conformant (``observer_result/v1``) dict.
    Raises ``NativeResultAdaptationFailed`` -- never silently downgrades to
    an empty-``findings`` success -- when:

    - ``native_result`` fails real ``jsonschema`` validation against
      ``codebase_investigation_result_v1.schema.json`` (P0-3: rejects a
      malformed ``investigation_route``/``impact_scope`` type, an
      incomplete ``REPO_EVIDENCE_REF_V1``, a malformed
      ``source_evidence_result``, or a ``status: ok`` / non-null
      ``failure_reason`` contradiction -- all of which the prior structural
      8-key recognizer let through silently)
    - ``status`` is not ``"ok"`` (``"failed"``/``"inconclusive"`` -- AC4)
    - ``evidence_refs`` is missing/empty, is not a list of objects, or any
      entry's ``commit_sha`` does not equal this run's authoritative
      ``base_sha`` (AC5 -- ``REPO_EVIDENCE_REF_V1.commit_sha != ctx.base_sha``)
    - any ``evidence_refs`` entry's bytes cannot be independently re-verified
      against the actual git blob at ``base_sha`` (P0-4 --
      ``_verify_repo_evidence_ref_bytes``): an unbound/stale/untracked path,
      or a hash that does not match the real blob, is rejected even when the
      self-reported ``commit_sha`` string equals ``base_sha``
    - ``discovery_summary`` is missing/empty (nothing to report as a finding)

    The returned dict's key set is EXACTLY ``EvidenceBundle``'s 7 declared
    fields (``_parse_wire_payload`` rejects unknown/missing fields) -- no
    extra/renamed keys, and no ``SMUGGLED_AUTHORITY_KEYS`` collision (the
    nested ``evidence_refs`` carries only ``REPO_EVIDENCE_REF_V1`` public
    fields, never raw stdout/credentials/absolute paths)."""
    try:
        jsonschema.validate(native_result, _codebase_investigation_result_schema())
    except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError):
        raise NativeResultAdaptationFailed("native_result_schema_invalid") from None

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
        _verify_repo_evidence_ref_bytes(ref, repo_root=repo_root, blob_bytes_getter=blob_bytes_getter)

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
        # P1-6 (OWNER review): the observed native-fallback signal is kept
        # structured -- never collapsed into free-form `discovery_summary`
        # prose only -- so a downstream consumer can key off it without
        # re-parsing text. `fallback_used` is always True here (this branch
        # only ever runs for a role-adapted native-fallback candidate);
        # `observed_failure_class` is best-effort extracted from
        # `discovery_summary`'s own `failure_class: <token>` mention (the
        # native output contract requires this SubAgent to state it there --
        # see codebase-investigator.md's "AGY advisory native fallback"
        # section) and is `None` when no such token is present.
        "fallback_used": True,
        "observed_failure_class": _extract_observed_failure_class(discovery_summary),
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


_FAILURE_CLASS_RE = re.compile(r"failure_class:\s*([A-Za-z0-9_]+)")


def _extract_observed_failure_class(discovery_summary: str) -> str | None:
    """Best-effort, non-fabricating extraction of the ``failure_class:
    <token>`` mention ``.claude/agents/codebase-investigator.md``'s native
    fallback contract requires ``discovery_summary`` to state (Issue #2374
    OWNER review P1-6). Returns ``None`` (never a guessed value) when no
    such token is present."""
    match = _FAILURE_CLASS_RE.search(discovery_summary)
    return match.group(1) if match else None


def apply_codebase_investigator_role_adapter(
    result: AgentInvocationResult,
    *,
    ctx: "RunContext",
    plan: "SourcePlan",
    observer_id: str,
    repo_root: Path,
) -> AgentInvocationResult:
    """Issue #2374 role adapter entry point: a pure, additive wrapper around
    an already-produced ``AgentInvocationResult``. When
    ``result.native_role_adapter_candidate`` is ``False`` (every request
    except a substantive-task codebase-investigator invocation that actually
    hit the native-recognition path), ``result`` is returned COMPLETELY
    UNCHANGED. Only when the marker is set does this function attempt
    ``adapt_native_codebase_investigation_result`` -- success replaces
    ``structured_output`` with the converted ``EvidenceBundle`` dict
    (``status="ok"``); failure (schema/AC4/AC5/P0-4 typed rejection) is
    surfaced as ``status="malformed_output"`` with a
    ``native_fallback_adaptation_failed:``-prefixed ``reason_code`` (never
    silently promoted to an empty-findings success)."""
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
            repo_root=repo_root,
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
    step for the codebase-investigator role-adapter path. ``repo_root`` for
    the P0-4 independent byte verification is derived from ``request.cwd``
    (already, always, the run's real repo_root -- ``build_observer_requests``/
    ``run_cli`` set it) rather than adding a new keyword-only parameter to
    this function's own signature."""
    result = invoke_agent(request, runner=runner, policy=policy)
    if request.role_adapter != _ROLE_ADAPTER_CODEBASE_INVESTIGATOR_OBSERVER_V1:
        return result
    return apply_codebase_investigator_role_adapter(
        result, ctx=ctx, plan=plan, observer_id=request.agent_name, repo_root=Path(request.cwd)
    )


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


def _real_evidence_projection_digest(real_findings: list[dict[str, Any]]) -> str:
    """The exact ``sha256`` digest formula both ``_enrich_evidence_ref``
    (below) and the private-audit producer hook
    (``_register_private_audit_refs_from_evidence``, Issue #2376 fix_delta
    blocker 3) use to recompute an evidence_ref's ``projection_digest`` from
    the ACTUAL real ``finding_sets`` content for one ``source_id`` -- a
    JCS-canonicalized hash of the real, already-redacted observer findings.
    Factored out so both call sites can never drift apart (a digest
    mismatch between them would otherwise silently defeat blocker 3's
    Latitude/observer-evidence disambiguation, see that function's
    docstring)."""
    projection = json.dumps(real_findings, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(projection.encode("utf-8")).hexdigest()


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
    digest = _real_evidence_projection_digest(real_findings)
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


def _register_private_audit_refs_from_evidence(
    candidate_records: Sequence[dict[str, Any]],
    *,
    finding_sets: Sequence[dict[str, Any]],
    run_identity: dict[str, Any],
) -> None:
    """Shared private-audit producer-hook wiring (Issue #2376 fix_delta,
    OWNER review issuecomment-5552512140 blockers 1/2/3), factored out of
    ``execute_run()`` so ``run_cli()`` -- the production CLI entrypoint --
    can call the SAME wiring instead of never reaching this hook at all
    (blocker 1: previously only ``execute_run()``'s own inline loop invoked
    ``register_private_audit_ref()``; ``run_cli()``'s own separate
    ``prepare`` -> ``validate-observers`` -> ``prepare-evaluator`` ->
    evaluator -> ``finalize`` composition never did).

    ``finding_sets`` MUST be ``evaluator_request.finding_sets`` -- the
    ALREADY dict-shaped list ``prepare_evaluator_request()`` builds via
    ``[dataclasses.asdict(fs) for fs in finding_sets]`` -- and never the raw
    ``list[FindingSet]`` dataclass objects ``build_finding_sets()`` returns
    (blocker 2: ``_observer_source_type_index()``'s
    ``isinstance(finding_set, dict)`` guard silently drops every entry of a
    ``list[FindingSet]``, leaving the resulting index permanently empty and
    this hook permanently a no-op). This is the exact same
    ``evaluator_request.finding_sets`` value ``run_evaluation()`` already
    threads into ``_enrich_evaluation_payload()`` to recompute each
    candidate's own ``evidence_refs[].projection_digest`` in the first
    place, so reusing it here (rather than recomputing a fresh index from
    ``build_finding_sets()``'s output) is also the one real data path the
    evaluator itself actually saw this run -- never a separately
    reconstructed/mocked view of it (this Issue's regression tests must
    exercise this exact conversion, not monkeypatch it).

    Only the LATEST (``evaluations[-1]``) evaluation entry of each candidate
    is considered -- never carried-over history from a previous run, which
    this run's producer hook has no business re-registering.

    For each of that latest evaluation's ``evidence_refs[]`` entries, a
    private-audit mapping is registered ONLY when the ref's own
    ``projection_digest`` matches the digest INDEPENDENTLY recomputed here
    (via ``_real_evidence_projection_digest``, the exact same formula
    ``_enrich_evidence_ref`` used to compute it) from
    ``real_evidence_index``'s real observer content for that ref's
    ``source_id`` (blocker 3). This is what distinguishes a ref this run's
    OWN observer-projection enrichment step actually produced from real
    observer evidence, from a ref a LATER step
    (``bind_latitude_evidence_to_candidates``) appended under the SAME
    ``source_id`` (``"runtime"``) from an entirely unrelated evidence
    source: a Latitude-bound ``runtime_receipt`` ref's ``projection_digest``
    is ``latitude_evidence["evidence_identity"]``, computed by a different
    collector entirely, so it will not match the observer-projection digest
    recomputed here even when ``real_evidence_index.get("runtime")`` is
    non-empty (the runtime OBSERVER did report real findings this run --
    those real findings are simply not what backs the Latitude ref). Such a
    ref is correctly left unregistered here -- ``private_audit_resolver.
    resolve()`` then reports ``unavailable`` for it, never silently
    backfilled with an unrelated observer's real evidence
    (``object_digest == projection_digest`` is deliberately NOT enforced
    generically inside the resolver itself -- a public projection and its
    private original may legitimately differ; this digest-equality check is
    this producer's own correspondence verification, not a generic resolver
    invariant).

    Best-effort / fail-open by construction: delegates every actual write to
    ``register_private_audit_ref()`` (module-level, above), which already
    swallows every exception from the underlying local storage write -- this
    function itself performs no I/O of its own beyond that delegation."""
    real_evidence_index = _observer_source_type_index(finding_sets)
    for candidate in candidate_records:
        finding_contract = candidate.get("finding_contract") if isinstance(candidate, dict) else None
        evaluations = finding_contract.get("evaluations") if isinstance(finding_contract, dict) else None
        if not evaluations:
            continue
        latest_evaluation = evaluations[-1]
        for ref in latest_evaluation.get("evidence_refs") or []:
            if not isinstance(ref, dict):
                continue
            real_findings = real_evidence_index.get(ref.get("source_id"))
            if not real_findings:
                continue
            if ref.get("projection_digest") != _real_evidence_projection_digest(real_findings):
                # Blocker 3: this ref's digest was not produced from THIS
                # source_id's real observer content this run (e.g. a
                # Latitude-bound `runtime_receipt` ref sharing the same
                # `source_id` as a real runtime-observer finding set) --
                # never backfill it with unrelated real evidence.
                continue
            register_private_audit_ref(ref, run_identity, private_content=real_findings)


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

    # Issue #2375 PR #2392 fix_delta: wires `collect_latitude_runtime_evidence_once()`/
    # `bind_latitude_evidence_to_candidates()` into the actual `execute_run()` call graph --
    # previously both were defined but had zero call sites outside their own definitions/tests
    # (PR #2392 human review blocker). Placed after `compute_delta()` (mirroring how delta
    # computation itself is wired in immediately before `finalize()`) so binding sees the final
    # `evaluation.candidate_records` the same way `finalize()` will persist them. Collection
    # Budget: exactly one `collect_latitude_runtime_evidence_once()` call per run, unconditionally
    # (the child collector itself declines to launch the CLI -- `session_id_unresolved`/
    # `project_slug_unresolved` -- when no target session_id/project slug is resolvable, so this
    # is never a second CLI launch). Failure/unavailability never blocks or fails the
    # retrospective (fail-open): `bind_latitude_evidence_to_candidates()` leaves every candidate
    # byte-for-byte unchanged whenever `latitude_evidence["availability"] != "available"`.
    latitude_session_id = _resolve_latitude_target_session_id(results)
    latitude_evidence = collect_latitude_runtime_evidence_once(session_id=latitude_session_id)
    bound_candidate_records = bind_latitude_evidence_to_candidates(
        evaluation.candidate_records, latitude_evidence
    )
    evaluation = dataclasses.replace(evaluation, candidate_records=bound_candidate_records)

    # Issue #2376 (#1939 Workstream 5): generation-time private-audit
    # producer hook. Additive-only, mirroring the fail-open Latitude
    # binding immediately above -- never mutates `evaluation`/
    # `bound_candidate_records`/the eventual `PublishRequest` (this hook's
    # only effect is a local-only sidecar filesystem write, and even that is
    # entirely delegated to -- and fail-open within --
    # `_register_private_audit_refs_from_evidence()`/
    # `register_private_audit_ref()`). Issue #2376 fix_delta (OWNER review
    # issuecomment-5552512140 blocker 1/2): the actual wiring now lives in
    # the shared `_register_private_audit_refs_from_evidence()` helper --
    # also called from `run_cli()` below -- fed `evaluator_request.
    # finding_sets` (the real, ALREADY dict-shaped evidence the evaluator
    # itself was given this run), never a freshly-recomputed
    # `list[FindingSet]` view of `finding_sets` (which silently produced an
    # always-empty index -- blocker 2).
    digest_run_identity = {
        "run_id": ctx.run_id,
        "base_sha": ctx.base_sha,
        "source_set_digest": plan.source_set_digest,
    }
    _register_private_audit_refs_from_evidence(
        bound_candidate_records,
        finding_sets=evaluator_request.finding_sets,
        run_identity=digest_run_identity,
    )

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

# ---------------------------------------------------------------------------
# Issue #2419 P0 incident fix: a delegated `codebase-investigator` observer
# ran `git fetch` + `git merge` via Bash and fast-forwarded the canonical
# local `main` ref to an unrelated stale branch. Root cause (see PR #2419):
# `DelegatedAgentPermissionPolicy.check_bash` was never called from any real
# invocation path (dead code), AND its exact-allowlist-only design could not
# have distinguished a legitimate read-only investigation pipeline (`git
# show <sha>:<path> | sha256sum`, needed to independently verify
# `REPO_EVIDENCE_REF_V1.excerpt_sha256`) from a mutation even if it HAD been
# wired -- its single-command, no-pipeline-splitting scan treats every `|`
# as an unconditional deny. The constants below back a NEW, explicitly
# opt-in ("read_only_investigation_enabled") gate that is pipeline-aware
# (each `|`-delimited segment is validated independently; `;`/`&`/`&&`/`||`
# and a literal newline are unconditional denies, never segment delimiters
# -- see `_tokenize_read_only_investigation_pipeline`) and denies by git/gh
# SUBCOMMAND+ACTION *position* (not token-SET membership) rather than by
# exact full-command match, so a real, unbounded set of read-only `git
# show`/`gh pr view` invocations can be allowed while every mutating verb
# stays denied. This is ADDITIVE: the default
# (`read_only_investigation_enabled=False`) keeps `check_bash`'s
# pre-existing deny-all-by-default / exact-allowlist-plus-tokenized-denylist
# behavior byte-for-byte (Issue #2237 P0-5's fail-open fix is not touched).
#
# PR #2425 review fix_delta round 3 (OWNER REQUEST_CHANGES,
# #2425#issuecomment-5466916997) rebuilt this profile around 3 explicit
# capabilities instead of an ever-growing set of individually-patched
# bypasses:
#   1. canonical AGY capability (`_AGY_CANONICAL_SCRIPT_SUFFIXES`) --
#      `codebase-investigator`'s real `build_request.py` /
#      `run_gemini_headless.py` invocation is now the ONLY allowed
#      `python3`/`uv` use, restoring the workflow the round-2 fix had
#      self-blocked (a bare `python3`/`uv` head was excluded entirely).
#   2. native Git read-only capability (`_ALLOWED_GIT_READ_ONLY_SUBCOMMANDS`)
#      -- an ALLOWLIST of read-only subcommands (position-based: the first
#      non-global-flag token after `git`), replacing the previous denylist
#      of *known* mutating subcommands, which silently allowed every
#      unlisted mutation (`git add`, `git hash-object -w`, `git bisect
#      start`, ...).
#   3. native GitHub capability (`_ALLOWED_GH_GROUP_ACTION_PAIRS` +
#      `_check_gh_api_segment`) -- exact (group, action) PAIRS at their real
#      argv POSITIONS (not token-SET membership, which let `gh pr checkout 1
#      --branch view` and `gh workflow run x.yml --ref view` through because
#      `view`/`pr`/`workflow` were merely *present* somewhere in the
#      command), plus a `gh api` GET-only rule (Issue #2419 contract
#      requires `gh api GET` for `github_research`).
# ---------------------------------------------------------------------------

#: git GLOBAL flags that take a following value token (consumed as a pair)
#: when locating the actual subcommand position -- e.g. `git -C <path>
#: show ...` must resolve to subcommand `show`, not treat `<path>` as the
#: subcommand. `-c`/`--config` are deliberately absent: those are denied
#: unconditionally (git config/alias indirection) before subcommand lookup
#: ever runs, regardless of position.
_GIT_GLOBAL_FLAGS_WITH_VALUE = frozenset({"-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix"})
#: git GLOBAL flags that take no value.
_GIT_GLOBAL_FLAGS_NO_VALUE = frozenset(
    {"--no-pager", "--paginate", "-p", "--bare", "--literal-pathspecs", "--no-replace-objects", "--no-optional-locks"}
)
#: git subcommands with a genuine read-only investigation surface -- an
#: ALLOWLIST (PR #2425 review fix_delta P0-3): every unlisted subcommand
#: (`add`, `hash-object -w`, `bisect`, `commit`, `push`, `merge`, ...) is
#: denied by construction, instead of relying on an ever-incomplete
#: denylist of *known* mutating subcommands.
_ALLOWED_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"show", "log", "diff", "blame", "rev-parse", "status", "cat-file", "ls-tree", "grep", "merge-base"}
)
#: `gh` GLOBAL flags that take a following value token.
_GH_GLOBAL_FLAGS_WITH_VALUE = frozenset({"--repo", "-R", "--hostname"})
#: exact (group, action) pairs allowed under the read-only investigation
#: profile, matched at their real argv POSITION (PR #2425 review fix_delta
#: P0-4) -- a flag value or branch/ref name that happens to equal an action
#: token (`gh pr checkout 1 --branch view`, `gh workflow run x.yml --ref
#: view`) can no longer be mistaken for the actual action the way a
#: token-SET-membership check could.
_ALLOWED_GH_GROUP_ACTION_PAIRS = frozenset(
    {
        ("pr", "view"), ("pr", "diff"), ("pr", "checks"), ("pr", "status"),
        ("issue", "view"), ("issue", "list"),
        ("repo", "view"),
        ("run", "view"), ("run", "list"),
        ("workflow", "view"), ("workflow", "list"),
    }
)
#: `gh api` method-override flags (an explicit non-GET method is always
#: denied; PR #2425 review fix_delta P1-c).
_GH_API_METHOD_FLAGS = frozenset({"--method", "-x"})
#: `gh api` flags that implicitly switch the request to `POST` per GitHub
#: CLI's own documented behavior, UNLESS an explicit `--method GET` is also
#: present.
_GH_API_DATA_FLAGS = frozenset({"-f", "-F", "--raw-field", "--input"})
#: canonical AGY delegation scripts (Issue #2419 PR #2425 review fix_delta
#: P0-1), relative to ``_REPO_ROOT``, that this profile's `python3`/`uv`
#: capability allows invoking.
#:
#: PR #2425 review fix_delta round 4 (P0, decoy-script bypass): a prior
#: design matched these by raw ``str.endswith()`` trailing-path membership
#: against the UNRESOLVED token text -- that check is trivially spoofed by
#: ANY path merely ending with the same trailing directory structure
#: (verified end-to-end: ``python3
#: /tmp/evilcopy/.claude/skills/gemini-cli-headless-delegation/scripts/
#: build_request.py`` was `allow`ed), and never even checked that the
#: script existed on disk (a NONEXISTENT path with the same trailing
#: structure was also `allow`ed). ``_AGY_CANONICAL_SCRIPT_ABSOLUTE_PATHS``
#: below instead holds the real, ``Path.resolve()``d (symlink- and
#: ``..``-collapsing) absolute path of each canonical script anchored to
#: this repo's OWN root (``_REPO_ROOT``, derived from this very module's
#: ``__file__`` the same way the hook script resolves its own location --
#: see ``retrospective_bash_guard_hook.py``'s
#: ``sys.path.insert(0, str(Path(__file__).resolve().parent))``), and
#: ``_is_agy_canonical_script_token`` requires an EXACT membership match
#: against a token's OWN resolved absolute path -- never a substring/suffix
#: match against unresolved text.
_AGY_CANONICAL_SCRIPT_SUFFIXES = (
    ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py",
    ".claude/skills/gemini-cli-headless-delegation/scripts/run_gemini_headless.py",
)
#: real, resolved, repo-root-anchored absolute paths backing
#: ``_is_agy_canonical_script_token``'s exact-match containment check.
_AGY_CANONICAL_SCRIPT_ABSOLUTE_PATHS = frozenset(
    (_REPO_ROOT / suffix).resolve() for suffix in _AGY_CANONICAL_SCRIPT_SUFFIXES
)
#: head commands that MAY be a canonical AGY delegation invocation -- gated
#: further by `_AGY_CANONICAL_SCRIPT_SUFFIXES` membership, never trusted
#: unconditionally (a bare `python3`/`uv` invocation of anything else is
#: still an unconditional deny -- see `_check_read_only_investigation_command`).
_AGY_CANONICAL_INVOCATION_HEAD_COMMANDS = frozenset({"python3", "uv"})
#: standalone head commands (outside `git`/`gh`/the canonical AGY
#: invocation above) allowed under the read-only investigation profile --
#: ONLY hashing/inspection coreutils that cannot themselves execute
#: arbitrary further commands. `find` (its `-exec`/`-execdir`/`-ok`/`-okdir`
#: actions run an arbitrary command, unrelated to its head token) is
#: deliberately EXCLUDED for that reason.
_READ_ONLY_INVESTIGATION_HEAD_COMMANDS = frozenset({"sha256sum", "sha1sum", "wc", "head", "tail", "cat", "ls"})
#: pipeline operator tokens this profile ever tokenizes as significant
#: (see `_tokenize_read_only_investigation_pipeline`). Only `|` composes a
#: pipeline; every other operator token that appears is an unconditional
#: deny of the WHOLE command (never a segment delimiter).
_PIPELINE_TOKENIZER_PUNCTUATION_CHARS = "|;&\n\r"
_DENIED_PIPELINE_OPERATOR_TOKENS = frozenset({";", "&", "&&", "||", "\n", "\r"})

_DENIED_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Skill"})

#: caller-supplied run-scoped variables (never credentials) built by this
#: module elsewhere (e.g. ``AGENT_RETROSPECTIVE_RUN_ID``). Issue #2445 AC1:
#: no longer consulted by ``sanitize_subprocess_env``/``_default_sanitized_env``
#: (those are denylist-based now, see ``_MUTATION_CREDENTIAL_ENV_VARS``) --
#: retained only for the ``env=`` dict-building call sites below that still
#: reference this prefix constant.
_RUN_SCOPED_ENV_PREFIX = "AGENT_RETROSPECTIVE_"


class PermissionDenied(Exception):
    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


def _tokenize_read_only_investigation_pipeline(command: str) -> list[list[str]]:
    """Quote-aware tokenizer for the read-only investigation Bash profile
    (Issue #2419 PR #2425 review fix_delta P1-b). Uses ``shlex``'s
    ``punctuation_chars`` mode so a ``|``/``;``/``&``/newline INSIDE a
    quoted string (``git grep 'foo|bar'``, ``gh pr view 1 --jq '.title |
    length'``) is never mistaken for a command separator -- unlike the
    previous ``re.split(r"\\|\\||&&|[|;\\r\\n]", command)``, which split on
    any raw occurrence of those characters regardless of quoting.

    Returns one token list PER simple command in a single-pipe (``|``-only)
    pipeline. Any OTHER operator token (``;``, a bare ``&``, ``&&``,
    ``||``, a literal newline/carriage-return) appearing ANYWHERE in the
    command -- even what otherwise looks like a single simple command -- is
    an unconditional deny of the WHOLE command: this profile only ever
    composes read-only pipelines with ``|``, and treating those operators as
    segment delimiters (the previous ``re.split`` behavior) is what let a
    mutating segment ride along next to a read-only-looking one."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PIPELINE_TOKENIZER_PUNCTUATION_CHARS)
    lexer.whitespace = " \t"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise PermissionDenied(f"bash_unparsable:{exc}", command=command) from exc
    if not tokens:
        raise PermissionDenied("bash_empty", command=command)
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token == "|":
            if not current:
                raise PermissionDenied("bash_empty_segment", command=command)
            segments.append(current)
            current = []
            continue
        if token in _DENIED_PIPELINE_OPERATOR_TOKENS:
            raise PermissionDenied(f"denied_bash_operator:{token!r}", command=command)
        current.append(token)
    if not current:
        raise PermissionDenied("bash_empty_segment", command=command)
    segments.append(current)
    return segments


def _find_git_subcommand(tokens: list[str]) -> str | None:
    """PR #2425 review fix_delta P0-3: locate the real git subcommand
    POSITION by walking past recognized GLOBAL flags following git's own
    argv grammar (a value-taking flag like ``-C <path>`` consumes its
    value as a separate token; a ``--flag=value`` long option consumes only
    itself). An unrecognized leading ``-``-prefixed flag fails CLOSED
    (returns ``None``, which the caller then denies) rather than guessing
    whether it takes a value -- consistent with this profile's fail-closed
    philosophy everywhere else."""
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok in _GIT_GLOBAL_FLAGS_WITH_VALUE:
            idx += 2
            continue
        if tok in _GIT_GLOBAL_FLAGS_NO_VALUE:
            idx += 1
            continue
        if tok.startswith("--") and "=" in tok:
            idx += 1
            continue
        if tok.startswith("-"):
            return None
        return tok
    return None


def _find_gh_group_action(tokens: list[str]) -> tuple[str | None, str | None]:
    """PR #2425 review fix_delta P0-4: locate the real ``gh`` (group,
    action) POSITIONS by walking past recognized GLOBAL flags
    (``--repo``/``-R``/``--hostname``), the same fail-closed-on-unrecognized
    -flag approach as ``_find_git_subcommand``."""
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok in _GH_GLOBAL_FLAGS_WITH_VALUE:
            idx += 2
            continue
        if tok.startswith("--") and "=" in tok:
            idx += 1
            continue
        if tok.startswith("-"):
            return None, None
        break
    if idx >= n:
        return None, None
    group = tokens[idx]
    idx += 1
    while idx < n:
        tok = tokens[idx]
        if tok in _GH_GLOBAL_FLAGS_WITH_VALUE:
            idx += 2
            continue
        if tok.startswith("--") and "=" in tok:
            idx += 1
            continue
        if tok.startswith("-"):
            return group, None
        break
    if idx >= n:
        return group, None
    return group, tokens[idx]


def _is_agy_canonical_script_token(tok: str) -> bool:
    """PR #2425 review fix_delta round 4 (P0, decoy-script bypass): returns
    ``True`` only when ``tok`` is a real filesystem path that
    ``Path.resolve()``s (following any symlink and collapsing any ``..``
    segment -- this covers the "symlink-based impersonation" concern too,
    since a symlink at/under a decoy path resolving to a canonical script
    is not itself the canonical script's OWN absolute path, and a symlink
    planted AT the canonical path pointing elsewhere resolves away from
    ``_AGY_CANONICAL_SCRIPT_ABSOLUTE_PATHS``) to EXACTLY one of the two
    canonical AGY delegation scripts' real, ``_REPO_ROOT``-anchored absolute
    paths.

    ``Path.resolve()`` never requires the path to exist (Python's default
    ``strict=False``) -- a decoy path is denied not because a stat() call
    fails, but because its resolved value can never equal a DIFFERENT,
    real script's own resolved absolute path. This is what makes the
    check fail closed for BOTH a decoy copy at an attacker-chosen prefix
    (e.g. ``/tmp/evilcopy/.claude/skills/gemini-cli-headless-delegation/
    scripts/build_request.py``) and a wholly nonexistent path with the
    same trailing directory structure -- the prior ``str.endswith()``
    design allowed both."""
    try:
        resolved = Path(tok).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved in _AGY_CANONICAL_SCRIPT_ABSOLUTE_PATHS


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

    def __init__(
        self,
        *,
        run_id: str,
        allowed_bash_commands: frozenset[str] = frozenset(),
        read_only_investigation_enabled: bool = False,
        settings_path: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.allowed_bash_commands = allowed_bash_commands
        #: Issue #2419: explicit opt-in only. Default ``False`` preserves
        #: `check_bash`'s pre-existing deny-all-by-default behavior
        #: byte-for-byte for every caller that does not pass this.
        self.read_only_investigation_enabled = read_only_investigation_enabled
        #: Issue #2419: path to a run-scoped ``--settings`` JSON file (see
        #: ``write_bash_guard_settings_file``) registering
        #: ``retrospective_bash_guard_hook.py`` as a ``PreToolUse`` hook for
        #: the real ``claude`` CLI subprocess. ``None`` (the default) adds no
        #: ``--settings`` argv element -- existing callers that never set
        #: this are unaffected.
        self.settings_path = settings_path

    def check_bash(self, command: str) -> None:
        """Fail-closed Bash gate. Two modes, selected by
        ``self.read_only_investigation_enabled``:

        - ``False`` (the default): pre-existing Issue #2237 P0-5 semantics,
          unchanged. A command must be present verbatim in
          ``self.allowed_bash_commands`` AND pass
          ``_check_single_command_tokenized_denylist`` -- an empty allowlist
          denies all Bash.
        - ``True`` (Issue #2419, opt-in): an exact ``allowed_bash_commands``
          match still short-circuits (via the same tokenized denylist scan,
          for backward compatibility with literal fixture allowlisting).
          Otherwise, falls through to
          ``_check_read_only_investigation_command``, which is
          pipeline-aware and denies by git/gh subcommand+action rather than
          by exact string match -- this is what actually allows the
          `codebase-investigator` observer's legitimate `git show <sha>:
          <path> | sha256sum` evidence-verification pipeline while still
          denying `git merge`/`git commit`/`git push`/... (the Issue #2419
          incident's root command class) inside that same pipeline."""
        normalized = " ".join(command.split())
        if normalized in self.allowed_bash_commands:
            self._check_single_command_tokenized_denylist(command)
            return
        if not self.read_only_investigation_enabled:
            raise PermissionDenied("bash_not_allowlisted", command=command)
        self._check_read_only_investigation_command(command)

    def _check_single_command_tokenized_denylist(self, command: str) -> None:
        """Pre-existing (Issue #2237 P0-5) single-command tokenized denylist
        scan -- unchanged. Only reached for a command that already matched
        ``self.allowed_bash_commands`` verbatim; closes the substring-
        blacklist bypasses identified in OWNER review
        #2237#issuecomment-5378291560 (``git -C . commit -m x``,
        ``gh --repo owner/repo issue comment 1 --body x``,
        ``python -c '...'``, ``curl -X POST ...``,
        ``printf data > repository-file``)."""
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

    def _check_read_only_investigation_command(self, command: str) -> None:
        """Issue #2419 / PR #2425 review fix_delta round 3
        (#2425#issuecomment-5466916997): pipeline-aware read-only
        investigation gate built around 3 explicit capabilities (canonical
        AGY invocation, native Git read-only subcommands, native GitHub
        group+action pairs) instead of an ever-growing set of individually
        patched bypasses -- see the module-level comment above
        ``_ALLOWED_GIT_READ_ONLY_SUBCOMMANDS`` for the full rationale.

        Process substitution (``<(...)``/``>(...)``) and command
        substitution (`` ` `` / ``$(``) are denied unconditionally on the
        RAW command string, before any tokenization: bash actually executes
        the substituted/substitution-fed command as its own process (P0-2 --
        OWNER-verified end-to-end that ``cat <(git merge stale-feature)``
        fast-forwards ``main`` exactly like Issue #2419's original
        incident), and no segment-aware parser can safely predict what
        either one conjures at runtime.

        The remaining command is tokenized quote-aware
        (``_tokenize_read_only_investigation_pipeline``) into one or more
        ``|``-joined segments; every other operator (``;``/``&``/``&&``/
        ``||``/a literal newline) is an unconditional deny of the WHOLE
        command, never a segment delimiter (P1-b -- a raw-string
        ``re.split`` previously split on those same characters even INSIDE
        a quoted argument, e.g. ``git grep 'foo|bar'`` or ``gh pr view 2425
        --jq '.title | length'``, denying legitimate read-only
        investigation).

        Each segment is allowed only if its head command is:

        - ``git`` with an ALLOWLISTED read-only subcommand at its real argv
          POSITION (P0-3 -- replaces a denylist of known mutating
          subcommands, which silently allowed every unlisted mutation like
          ``git add``/``git hash-object -w``/``git bisect``);
        - ``gh`` with an ALLOWLISTED (group, action) PAIR at its real argv
          POSITION, or ``gh api`` with an effective GET method (P0-4/P1-c --
          replaces token-SET membership, which let a flag VALUE or branch
          name equal to an action token bypass the check, e.g. ``gh pr
          checkout 1 --branch view``);
        - the canonical AGY delegation builder/wrapper invocation (P0-1 --
          the ONLY ``python3``/``uv`` use this profile allows; restores the
          workflow a prior round's blanket `python3`/`uv` exclusion had
          self-blocked); or
        - one of the standalone read-only coreutils in
          ``_READ_ONLY_INVESTIGATION_HEAD_COMMANDS``.

        Any inline git config override (``-c``/``--config``) is denied
        outright for ``git`` segments regardless of position -- it is never
        needed for read-only investigation and is a documented alias/
        indirection vector (verified end-to-end: ``git -c
        alias.x=merge x <branch>`` performs a real fast-forward merge)."""
        if any(marker in command for marker in ("`", "$(", "<(", ">(")):
            raise PermissionDenied("denied_bash_metacharacter:command_or_process_substitution", command=command)
        segments = _tokenize_read_only_investigation_pipeline(command)
        for original_tokens in segments:
            lowered = [tok.lower() for tok in original_tokens]
            token_set = set(lowered)
            if token_set & _DENIED_BASH_METACHAR_TOKENS:
                raise PermissionDenied("denied_bash_metacharacter", command=command)
            head = Path(lowered[0]).name
            if head in _DENIED_BASH_STANDALONE_COMMANDS:
                raise PermissionDenied(f"denied_bash_standalone:{head}", command=command)
            if (token_set & _DENIED_INLINE_EXEC_INTERPRETERS) and (token_set & _DENIED_INLINE_EXEC_FLAGS):
                raise PermissionDenied("denied_bash_pattern:inline_exec", command=command)
            if head == "git":
                if "-c" in original_tokens or "--config" in lowered:
                    raise PermissionDenied("denied_git_inline_config_override", command=command)
                # PR #2425 review fix_delta P0-3: POSITION-based subcommand
                # lookup against an ALLOWLIST (not a token-SET denylist
                # intersection) -- `_find_git_subcommand` walks past global
                # flags following real git argv grammar (`-C <path>` etc.
                # consume their value; an unrecognized leading flag fails
                # closed to `None` rather than guessing its arity).
                subcommand = _find_git_subcommand(original_tokens)
                if subcommand is None or subcommand.lower() not in _ALLOWED_GIT_READ_ONLY_SUBCOMMANDS:
                    raise PermissionDenied(f"denied_git_subcommand_not_allowlisted:{subcommand}", command=command)
                continue
            if head == "gh":
                # PR #2425 review fix_delta P0-4/P1-c: POSITION-based
                # (group, action) lookup against an exact-pair ALLOWLIST,
                # with `gh api` handled by its own GET-only rule.
                group, action = _find_gh_group_action(original_tokens)
                if group is not None and group.lower() == "api":
                    self._check_gh_api_segment(original_tokens, lowered, command)
                    continue
                pair = (group.lower(), action.lower()) if group and action else None
                if pair is None or pair not in _ALLOWED_GH_GROUP_ACTION_PAIRS:
                    raise PermissionDenied(
                        f"denied_gh_group_action_not_allowlisted:{group}:{action}", command=command
                    )
                continue
            if head in _AGY_CANONICAL_INVOCATION_HEAD_COMMANDS:
                # PR #2425 review fix_delta P0-1 / round 4 (P0,
                # decoy-script bypass): the ONLY allowed `python3`/`uv` use
                # is an exact canonical AGY builder/wrapper invocation --
                # everything else (heredoc, `-c`, arbitrary script FILE
                # argument, or a decoy/nonexistent path merely ending with
                # the same trailing directory structure -- see
                # `_is_agy_canonical_script_token`) stays denied.
                if any(_is_agy_canonical_script_token(tok) for tok in original_tokens):
                    continue
                raise PermissionDenied(f"denied_unlisted_command:{head}", command=command)
            if head in _READ_ONLY_INVESTIGATION_HEAD_COMMANDS:
                continue
            raise PermissionDenied(f"denied_unlisted_command:{head}", command=command)

    def _check_gh_api_segment(self, original_tokens: list[str], lowered: list[str], command: str) -> None:
        """PR #2425 review fix_delta P1-c: ``gh api`` is allowed only for an
        effective ``GET`` request -- GitHub CLI's own documented behavior is
        that the default method is ``GET`` but ``-f``/``-F``/``--raw-field``/
        ``--input`` implicitly switch it to ``POST``. Denies any explicit
        non-``GET`` ``--method``/``-X``, and denies any data-carrying flag
        UNLESS an explicit ``--method GET`` is also present."""
        explicit_method: str | None = None
        has_data_flag = False
        idx = 0
        n = len(original_tokens)
        while idx < n:
            tok = original_tokens[idx]
            low = lowered[idx]
            if low in _GH_API_METHOD_FLAGS:
                explicit_method = original_tokens[idx + 1] if idx + 1 < n else ""
                idx += 2
                continue
            if low.startswith("--method="):
                explicit_method = tok.split("=", 1)[1]
                idx += 1
                continue
            if low in _GH_API_DATA_FLAGS:
                has_data_flag = True
                idx += 1
                continue
            idx += 1
        explicit_method_upper = explicit_method.upper() if explicit_method is not None else None
        if explicit_method_upper is not None and explicit_method_upper != "GET":
            raise PermissionDenied(f"denied_gh_api_method:{explicit_method_upper}", command=command)
        if has_data_flag and explicit_method_upper != "GET":
            raise PermissionDenied("denied_gh_api_data_flag_without_explicit_get", command=command)

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
        subprocess (Issue #2237 P0-5, replaced by Issue #2445 AC1 with
        denylist-based semantics): inherits ``env`` (built by ``invoke_agent``
        as ``{**os.environ, **request.env}``) unconditionally excluding only
        ``_MUTATION_CREDENTIAL_ENV_VARS``. This restores continuity for
        provider transport / model-selection / other non-mutation Claude
        runtime env vars (``ANTHROPIC_BASE_URL``, ``ANTHROPIC_AUTH_TOKEN``,
        ``ANTHROPIC_MODEL``, ``ANTHROPIC_DEFAULT_*_MODEL``,
        ``CLAUDE_CONFIG_DIR``, ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``, etc.) that
        the prior allowlist-only design silently dropped (Issue #2436
        Background) -- no new claude-gpt-specific opt-in flag is introduced;
        this mirrors the sibling
        ``plugins/agent-retrospective/skills/run/scripts/run_retrospective.py``
        implementation's ``sanitize_subprocess_env``/``_default_sanitized_env``
        pair exactly."""
        return _default_sanitized_env(env)


# ---------------------------------------------------------------------------
# Issue #2419: real PreToolUse hook wiring for `DelegatedAgentPermissionPolicy
# .check_bash`. `check_bash` was previously defined but never called from any
# production invocation path -- the actual production call site is
# `retrospective_bash_guard_hook.py` (a standalone script the real `claude`
# CLI subprocess launches per-Bash-tool-use, per Claude Code's own
# `PreToolUse` hook contract), which imports this module and calls
# `build_bash_guard_hook_decision`. Agent frontmatter's own `hooks:` field
# does NOT fire in a headless `-p` session (it requires a workspace-trust
# dialog acceptance no `-p` session ever presents); a `--settings`-file hook
# does fire in `-p` (Issue #2419 SubAgent C research, Claude Code hooks
# reference), which is why `write_bash_guard_settings_file` below produces a
# `--settings` file rather than relying on the agent definition's `hooks:`.
# ---------------------------------------------------------------------------


def build_bash_guard_hook_decision(command: str, *, policy: DelegatedAgentPermissionPolicy) -> dict[str, Any]:
    """Real production call site for ``policy.check_bash`` (Issue #2419).
    Used by ``retrospective_bash_guard_hook.py``. Returns a Claude Code
    ``PreToolUse`` hook JSON response -- never raises."""
    try:
        policy.check_bash(command)
    except PermissionDenied as exc:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"agent-retrospective Bash guard: {exc}",
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "agent-retrospective Bash guard: command passed read-only investigation policy",
        }
    }


def write_bash_guard_settings_file(tmp_dir: Path) -> Path:
    """Write a run-scoped ephemeral Claude Code ``--settings`` file (Issue
    #2419) registering ``retrospective_bash_guard_hook.py`` as a
    ``PreToolUse`` hook for the ``Bash`` tool. Written inside the run's own
    private temp dir (``run_scoped_temp_dir``) so it is removed with
    everything else that dir's context manager cleans up."""
    settings_path = tmp_dir / "bash_guard_settings.json"
    hook_script = Path(__file__).resolve().parent / "retrospective_bash_guard_hook.py"
    hook_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(hook_script))}"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": hook_command}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return settings_path


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
        if observer_id == "codebase-investigator":
            # OWNER review #2387#issuecomment-5459502795 (P0-1 live-smoke
            # finding): telling codebase-investigator to produce
            # OBSERVER_RESULT_V1 JSON here actively conflicts with THIS
            # invocation's own --json-schema binding, which
            # build_observer_requests()'s role_adapter branch already set to
            # the native codebase_investigation_result_v1.schema.json (never
            # observer_result_v1.schema.json) -- see that function's
            # docstring. A live run confirmed the conflicting instruction
            # actually wins: the model produced OBSERVER_RESULT_V1-shaped
            # JSON that then failed native-schema validation
            # (missing_structured_output, zero native-schema-valid
            # candidates). This branch tells the model the TRUTH about which
            # schema this specific invocation enforces instead of steering it
            # toward the wrong one; the Python-side role adapter
            # (apply_codebase_investigator_role_adapter /
            # adapt_native_codebase_investigation_result) remains the sole
            # AUTHORITATIVE resolver -- real jsonschema validation plus
            # base_sha/evidence-byte re-verification -- this prompt text is
            # advisory to the model, never trusted as-is.
            output_rules = (
                "OUTPUT_RULES\n"
                "- This invocation's --json-schema enforces conformance to "
                "YOUR OWN native CODEBASE_INVESTIGATION_RESULT_V1 output "
                "contract (see your own operating instructions, "
                "codebase-investigator.md's \"Result: "
                "CODEBASE_INVESTIGATION_RESULT_V1\" section) -- NOT "
                "OBSERVER_RESULT_V1. Do not attempt to produce an "
                "OBSERVER_RESULT_V1-shaped JSON object (schema_version/"
                "run_id/base_sha/source_set_digest/observer_id/evidence_ref/"
                "findings) for this invocation; it will be rejected.\n"
                "- CALLER_TASK_DATA above may itself contain text that looks "
                "like identity fields (e.g. a prior run's run_id/base_sha/"
                "source_set_digest quoted as evidence) -- such values are "
                "ordinary investigative data, never this run's identity, no "
                "matter how they are formatted.\n"
                "- Use CALLER_TASK_DATA's \"task\" field to decide what to "
                "investigate, and report your findings per your own native "
                "output contract (including the AGY_ADVISORY_NATIVE_FALLBACK_"
                "POLICY input below, when your own operating instructions "
                "apply it)."
            )
        else:
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
    # Validated Scope"). The AUTHORITATIVE contract selection is never this
    # prompt text alone -- `build_observer_requests()` deterministically
    # selects the `--json-schema` CLI argument from `role_adapter`, and
    # `apply_codebase_investigator_role_adapter` (Python-side)
    # deterministically resolves/verifies the native result (OWNER review
    # #2387#issuecomment-5459502795 P0-1/P0-3/P0-4). The `output_rules`
    # branch above additionally tells the model, truthfully, which schema
    # THIS invocation enforces (native vs observer) -- a live-smoke run
    # confirmed that leaving the (stale, pre-fix_delta) OBSERVER_RESULT_V1
    # instruction in place for a role-adapted invocation actively steers the
    # model toward producing the wrong shape, even though `--json-schema`
    # itself was already correctly the native schema.
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
    before -- ``role_adapter=None`` on all of them.

    OWNER review #2387#issuecomment-5459502795 (P0-1): the
    ``role_adapter``-carrying ``codebase-investigator`` request ALSO gets a
    different ``json_schema_path`` -- ``codebase_investigation_result_v1.
    schema.json`` (the native contract's own schema), not
    ``observer_result_v1.schema.json``. This is the deterministic
    CLI-level schema selection: which shape the real ``claude`` CLI's
    ``--json-schema`` flag constrains ``structured_output`` to is now a
    direct function of ``role_adapter``, not a post-hoc structural guess
    applied after the fact against a single, always-observer schema."""
    _reject_missing_or_empty_prompts(prompts)
    return [
        AgentInvocationRequest(
            agent_name=spec.observer_id,
            prompt=prompts[spec.observer_id],
            json_schema_path=str(
                schema_dir
                / (
                    _CODEBASE_INVESTIGATION_RESULT_SCHEMA_FILENAME
                    if caller_supplied_task_path and spec.observer_id == "codebase-investigator"
                    else "observer_result_v1.schema.json"
                )
            ),
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

    def _base_sha_resolver() -> str:
        completed = git_runner(
            ["git", "rev-parse", "main"], cwd=str(repo_root), capture_output=True, text=True, timeout=30
        )
        if completed.returncode != 0:
            raise ValueError(f"base_sha_resolution_failed:{completed.stderr}")
        return completed.stdout.strip()

    with run_scoped_temp_dir(resolved_run_id, base_dir=temp_base_dir) as run_tmp_dir:
        # Issue #2419: `--settings` file registering the real PreToolUse
        # Bash guard hook, and `read_only_investigation_enabled=True` so
        # `policy.check_bash` (now a real production call site via
        # `retrospective_bash_guard_hook.py`) allows the read-only git/gh
        # investigation surface `codebase-investigator`'s AGY advisory
        # native fallback genuinely needs, while still denying every
        # mutating verb (the Issue #2419 incident's root command class).
        bash_guard_settings_path = write_bash_guard_settings_file(run_tmp_dir)
        policy = DelegatedAgentPermissionPolicy(
            run_id=resolved_run_id,
            read_only_investigation_enabled=True,
            settings_path=str(bash_guard_settings_path),
        )
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

        # Issue #2376 fix_delta (OWNER review issuecomment-5552512140
        # blocker 1): the private-audit producer hook was previously wired
        # ONLY into `execute_run()`'s own call graph -- this production CLI
        # entrypoint (`run_cli()` -> `main()`, the one the root Skill's
        # Procedure actually invokes via Bash) never reached it. Reuses the
        # SAME shared `_register_private_audit_refs_from_evidence()` helper
        # `execute_run()` calls, fed `evaluator_request.finding_sets` (the
        # real dict-shaped evidence this run's evaluator was actually given
        # -- see that helper's docstring for why NOT `finding_sets` itself).
        digest_run_identity = {
            "run_id": ctx.run_id,
            "base_sha": ctx.base_sha,
            "source_set_digest": plan.source_set_digest,
        }
        _register_private_audit_refs_from_evidence(
            evaluation.candidate_records,
            finding_sets=evaluator_request.finding_sets,
            run_identity=digest_run_identity,
        )

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



# ---------------------------------------------------------------------------
# Latitude runtime evidence deterministic enrichment (Issue #2375)
# ---------------------------------------------------------------------------
#
# Binds a single validated `latitude_runtime_evidence/v1` instance (Child collector:
# `collect_snapshot.collect_latitude_runtime_evidence`) onto `agent_improvement_candidate/v1`
# candidates' `finding_contract.evaluations[].evidence_refs[]` -- WITHOUT touching
# `agent_improvement_candidate_v1.schema.json` (outside this Issue's Allowed Paths): the existing
# schema's `runtime_receipt`/`runtime` `ref_type`/`source_id` pair already accepts exactly this
# shape (`resource_identity` has no extra pattern constraint for `runtime_receipt`, and
# `projection_digest` only requires the `sha256:<64 hex>` shape that
# `compute_latitude_evidence_identity` already produces), so no schema change is required.
#
# Binding Rules (Issue #2375) enforced here:
#   - only `availability == "available"` evidence is ever bound (`None`/`unavailable`/`error`
#     evidence leaves every candidate unchanged -- Latitude being absent/unavailable never halts
#     or alters the retrospective by itself);
#   - only `finding_contract.claim_class == "runtime_behavior"` candidates are eligible (Latitude
#     runtime evidence substantiates runtime-behavior claims, not code-content/review/mergeability
#     claims);
#   - binding NEVER changes `observed`/`presence_delta`/`evaluation_status`/`source_coverage`/
#     `claim_class`/any signal field -- it only appends one `evidence_refs[]` entry to the current
#     (last) evaluation, so evidence presence/absence can never by itself upgrade a finding to a
#     positive claim/status/confidence (AC3);
#   - an unknown `schema_version`, a schema/format violation, or an identity mismatch on the
#     supplied evidence all fail closed (`RetrospectiveSchemaError`, propagated from
#     `validate_retrospective_schema.validate_latitude_runtime_evidence`) rather than being
#     silently treated as absent evidence;
#   - the same `evidence_identity` is never bound twice onto the same evaluation within one call
#     (Collection Budget: at most 1 evidence per retrospective run) -- fail closed
#     (`RetrospectiveSchemaError`) rather than silently de-duplicating.

LATITUDE_RUNTIME_EVIDENCE_REF_TYPE = "runtime_receipt"
LATITUDE_RUNTIME_EVIDENCE_SOURCE_ID = "runtime"
LATITUDE_ELIGIBLE_CLAIM_CLASS = "runtime_behavior"


def collect_latitude_runtime_evidence_once(**collector_kwargs: Any) -> dict[str, Any]:
    """Thin delegating wrapper around Child 3's
    ``collect_snapshot.collect_latitude_runtime_evidence`` (Issue #2375 AC2: at most one Latitude
    CLI launch per retrospective run). Callers MUST call this at most once per run and reuse the
    single returned evidence dict for every ``bind_latitude_evidence_to_candidates`` call in that
    run -- this wrapper has no internal retry/loop, so a caller that (incorrectly) calls it more
    than once per run would itself violate the Collection Budget, not this function.

    ``**collector_kwargs`` (``which``/``runner``/``clock``/``identity_computer``/``session_id``/
    ``project_slug``/``project_slug_resolver``/``limit``) are forwarded verbatim to
    ``collect_latitude_runtime_evidence`` -- this lets tests inject a fake CLI runner through THIS
    wrapper directly, rather than needing to monkeypatch the ``_collect_snapshot_module()``-loaded
    module object (which, like every ``_load_module_from_path`` sibling load in this file, is
    re-exec'd fresh on every call and therefore not a stable monkeypatch target across two separate
    calls). PR #2392 fix_delta: ``session_id`` is the caller-resolved target Claude Code
    ``session_id`` (see ``_resolve_latitude_target_session_id``) -- when omitted/``None``, the
    child collector itself returns ``unavailable``/``session_id_unresolved`` without launching the
    CLI (never a query without a session filter)."""
    return _collect_snapshot_module().collect_latitude_runtime_evidence(**collector_kwargs)


def _resolve_latitude_target_session_id(
    results: Sequence[Any], *, source_id: str = "claude_gpt"
) -> str | None:
    """Resolves the target Claude Code ``session_id`` for Latitude trace correlation from the
    EXISTING hook-sink-derived ``complete_sessions`` provenance the ``claude_gpt`` collector
    (``collect_snapshot.collect_claude_gpt_source``) already produces (Issue #2375 PR #2392
    fix_delta -- Session Correlation, not "latest trace"). This function does NOT invent a new
    session-recording mechanism; it reuses
    ``CollectorResult.private_evidence["provenance"]["complete_sessions"]`` -- a sorted list of
    ``session_id`` strings this run's nonce observed as both ``UserPromptSubmit`` and ``Stop``
    (see ``collect_snapshot.collect_claude_gpt_source``'s ``complete_sessions`` computation).

    ``results`` is the list ``prepare()`` returns (each a Child 3 ``CollectorResult``) -- the SAME
    list ``execute_run()`` already threads through to ``build_source_digest_registry``/
    ``finalize(source_observations=...)``, so this reuses data already computed for this run
    rather than re-collecting anything.

    Deterministic tie-break when more than one session completed within the same retrospective
    run: the (already sorted) first entry is used, so ``collect_latitude_runtime_evidence_once()``
    is still called with exactly one session_id candidate and the Collection Budget's "at most 1
    CLI launch per run" is honored.

    Returns ``None`` (session unresolved) when no ``claude_gpt`` collector result is present in
    ``results`` or it reports no complete sessions -- callers MUST treat this as "skip Latitude
    collection entirely for this run", never as "query without a session filter"."""
    for result in results:
        observation = getattr(result, "observation", {}) or {}
        if observation.get("source_id") != source_id:
            continue
        private_evidence = getattr(result, "private_evidence", {}) or {}
        provenance = private_evidence.get("provenance", {}) or {}
        complete_sessions = provenance.get("complete_sessions") or []
        if complete_sessions:
            return complete_sessions[0]
    return None


def bind_latitude_evidence_to_candidates(
    candidates: Sequence[dict[str, Any]],
    latitude_evidence: dict[str, Any] | None,
    *,
    validator_module: Any | None = None,
) -> list[dict[str, Any]]:
    """Deterministically bind ``latitude_evidence``'s allowlisted metrics + opaque
    ``evidence_ref``/``evidence_identity`` onto every ``runtime_behavior``-claim-class candidate's
    current (last) ``finding_contract`` evaluation, per the Binding Rules in the module docstring
    above.

    Pure / non-mutating: returns a deep copy of ``candidates`` in every case; neither ``candidates``
    nor ``latitude_evidence`` (nor any of their nested dicts) is ever mutated in place. When
    ``latitude_evidence`` is ``None`` or its ``availability`` is not ``"available"``, the deep copy
    is returned completely unchanged (Latitude absence/unavailability never alters or blocks the
    retrospective).

    Raises ``RetrospectiveSchemaError`` (imported from ``validate_retrospective_schema.py``, fail
    closed, never silently swallowed) when:

    - ``latitude_evidence["schema_version"]`` is not exactly ``"latitude_runtime_evidence/v1"``
      (unknown schema version);
    - ``latitude_evidence`` fails schema validation or its declared ``evidence_ref``/
      ``evidence_identity`` do not match the values recomputed from its own
      ``collector_version``/``metrics``/``collected_at`` (identity mismatch) -- see
      ``validate_retrospective_schema.validate_latitude_runtime_evidence``;
    - the same ``evidence_identity`` would be bound twice onto the same evaluation's
      ``evidence_refs[]`` (duplicate evidence within a single retrospective run).
    """
    result = copy.deepcopy(list(candidates))
    if latitude_evidence is None:
        return result

    # ``validator_module`` is injectable (defaults to a fresh
    # ``_validate_retrospective_schema_module()`` load) SOLELY because
    # ``_load_module_from_path`` (see module docstring's sibling-loading section) re-execs the
    # target file on every call, producing a structurally-identical but IDENTITY-DISTINCT
    # ``RetrospectiveSchemaError`` class each time -- a caller (e.g. a test) that wants
    # ``pytest.raises(validator_mod.RetrospectiveSchemaError)`` to actually match must supply the
    # exact module instance it will assert against, rather than relying on two independent fresh
    # loads coincidentally producing "the same" exception type (they never do; see
    # ``test_latitude_evidence_binding_duplicate_within_run_fails_closed`` and neighbors in
    # ``test_run_retrospective.py`` for the concrete failure mode this avoids).
    validator_mod = validator_module if validator_module is not None else _validate_retrospective_schema_module()
    schema_version = latitude_evidence.get("schema_version")
    if schema_version != "latitude_runtime_evidence/v1":
        raise validator_mod.RetrospectiveSchemaError(
            "bind_latitude_evidence_to_candidates: unknown latitude_evidence schema_version="
            f"{schema_version!r}; expected 'latitude_runtime_evidence/v1'."
        )
    validator_mod.validate_latitude_runtime_evidence(latitude_evidence)

    if latitude_evidence["availability"] != "available":
        return result

    evidence_ref_entry = {
        "ref_type": LATITUDE_RUNTIME_EVIDENCE_REF_TYPE,
        "source_id": LATITUDE_RUNTIME_EVIDENCE_SOURCE_ID,
        "resource_identity": latitude_evidence["evidence_ref"],
        "projection_digest": latitude_evidence["evidence_identity"],
    }

    for candidate in result:
        finding_contract = candidate.get("finding_contract")
        if not finding_contract or finding_contract.get("claim_class") != LATITUDE_ELIGIBLE_CLAIM_CLASS:
            continue
        evaluations = finding_contract.get("evaluations") or []
        if not evaluations:
            continue
        current_evaluation = evaluations[-1]
        existing_digests = {
            ref.get("projection_digest")
            for ref in current_evaluation.get("evidence_refs", [])
            if ref.get("ref_type") == LATITUDE_RUNTIME_EVIDENCE_REF_TYPE
        }
        if evidence_ref_entry["projection_digest"] in existing_digests:
            raise validator_mod.RetrospectiveSchemaError(
                "bind_latitude_evidence_to_candidates: duplicate latitude evidence_identity="
                f"{evidence_ref_entry['projection_digest']!r} already bound to this evaluation "
                "within the same retrospective run (Collection Budget: at most 1 evidence per run)."
            )
        current_evaluation.setdefault("evidence_refs", []).append(evidence_ref_entry)

    return result


if __name__ == "__main__":
    sys.exit(main())
