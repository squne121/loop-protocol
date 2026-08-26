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
    returns a partially-valid result -- once the retry budget is spent."""
    attempt = 0
    text = raw_text
    last_error: WireContractError | None = None
    while True:
        try:
            return envelope_cls.from_wire(text)
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


def _structured_output_from_result_compat(
    payload: dict[str, Any], *, json_schema_path: str
) -> dict[str, Any] | None:
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
    `ok`."""
    result_text = payload.get("result")
    if not isinstance(result_text, str) or not result_text.strip():
        return None
    try:
        schema = json.loads(Path(json_schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    fenced_candidates = _iter_fenced_json_candidates(result_text)
    candidate_texts = fenced_candidates if fenced_candidates else [result_text.strip()]

    schema_valid_candidates: list[dict[str, Any]] = []
    for candidate_text in candidate_texts:
        try:
            candidate = json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(candidate, dict):
            continue
        try:
            jsonschema.validate(candidate, schema)
        except (jsonschema.exceptions.ValidationError, jsonschema.exceptions.SchemaError):
            continue
        schema_valid_candidates.append(candidate)

    if len(schema_valid_candidates) != 1:
        return None
    return schema_valid_candidates[0]


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
    if isinstance(raw_structured_output, dict):
        structured_output = raw_structured_output
    elif raw_structured_output is _MISSING or raw_structured_output is None:
        structured_output = _structured_output_from_result_compat(
            payload, json_schema_path=request.json_schema_path
        )
    else:
        structured_output = None
    if not isinstance(structured_output, dict):
        return AgentInvocationResult(
            status="malformed_output",
            structured_output=None,
            raw_stdout_excerpt=_stdout_excerpt(completed.stdout),
            exit_code=completed.returncode,
            reason_code="missing_structured_output",
        )

    return AgentInvocationResult(
        status="ok",
        structured_output=structured_output,
        raw_stdout_excerpt=None,
        exit_code=completed.returncode,
        reason_code=None,
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


def run_evaluation(
    ctx: RunContext,
    evaluator_request: EvaluatorRequest,
    *,
    invoke_evaluator: Callable[[EvaluatorRequest], AgentInvocationResult],
    repair: Callable[[str, WireContractError], str] | None = None,
) -> Evaluation:
    """Invoke the evaluator exactly once with ``evaluator_request`` (built
    only from validated ``FindingSet`` projections -- see
    ``build_finding_sets``/``prepare_evaluator_request``) and strictly
    validate its output. The caller (``execute_run``/``run_cli``) is
    responsible for only calling this after ``run_observer_wave`` has
    succeeded for every observer in the wave (AC9)."""
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
    evaluation = parse_agent_output_with_repair(raw_text, Evaluation, repair=repair)
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
    evaluation = run_evaluation(ctx, evaluator_request, invoke_evaluator=invoke_evaluator)
    resolved_provider = (
        previous_state_provider if previous_state_provider is not None else FixturePreviousStateProvider(fixtures={})
    )
    previous_state = resolved_provider.get(
        repository_id=repository_id,
        scope=previous_state_scope,
        finding_identity_algorithm=_default_finding_identity_algorithm(),
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


def _default_observer_prompt(observer_id: str, *, run_id: str, base_sha: str, source_set_digest: str) -> str:
    """Issue #2345 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
    P1 items 1-2): the genuinely non-empty, REAL-identity default prompt
    used for ``observer_id`` when ``main()``'s ``--prompts-file`` is not
    supplied. ``run_cli()`` passes this function the SAME ``ctx.run_id`` /
    ``ctx.base_sha`` / ``plan.source_set_digest`` its internal ``prepare()``
    step produced for this run (never a fixed placeholder) -- see
    ``run_cli``'s prompt-building step, executed after ``prepare()``
    returns.

    Background: prior to Issue #2345, the ``--prompts-file``-omitted
    default was an empty string per observer (``prompts.get(observer_id,
    "") -> ""``). Against the real ``claude`` CLI (observed on Claude Code
    2.1.245), ``claude -p`` rejects an empty prompt argument before any
    observer output is produced at all -- ``Error: Input must be provided
    either through stdin or as a prompt argument when using --print``, exit
    code 1. This is an empty-prompt invocation contract mismatch between
    this module's (former) default and the real CLI's documented ``-p``
    contract (a caller-side default that never supplied a prompt at all),
    not a Claude Code CLI-side regression -- rejecting an explicitly empty
    prompt is a reasonable caller-contract enforcement on the CLI's part
    (see Issue #2345, https://github.com/squne121/loop-protocol/issues/2345).

    This default asks ``observer_id`` to emit a schema-conformant
    ``OBSERVER_RESULT_V1`` (``EvidenceBundle``) JSON envelope that echoes
    the run's REAL identity fields verbatim, with an empty ``findings``
    list (no caller-supplied evidence is provided along this
    ``--prompts-file``-omitted path). A real, successful invocation
    therefore satisfies ``run_observer_wave()``'s ``bundle.run_id !=
    ctx.run_id`` / ``source_set_digest`` / ``base_sha`` checks and lets the
    production call graph continue past the observer wave into the
    evaluator, delta, and ``finalize`` phases -- the genuine end-to-end
    completion this default is now designed to reach, rather than a
    construct-to-fail identity mismatch."""
    return (
        f"observer_id={observer_id}. No caller-supplied evidence was "
        "provided (this is run_retrospective.py's own default prompt, used "
        "only when --prompts-file is omitted -- Issue #2345). Respond with "
        "EXACTLY one JSON object (no markdown fence, no prose) conforming "
        "to OBSERVER_RESULT_V1 (EvidenceBundle):\n"
        "{\n"
        '  "schema_version": "observer_result/v1",\n'
        f'  "run_id": "{run_id}",\n'
        f'  "base_sha": "{base_sha}",\n'
        f'  "source_set_digest": "{source_set_digest}",\n'
        f'  "observer_id": "{observer_id}",\n'
        f'  "evidence_ref": "{_DEFAULT_PROMPT_EVIDENCE_REF}",\n'
        '  "findings": []\n'
        "}\n"
        "Echo the run_id/base_sha/source_set_digest/observer_id/"
        "evidence_ref fields above verbatim; do not invent evidence or "
        "findings beyond an empty findings list."
    )


def build_observer_requests(
    *, schema_dir: Path, cwd: str, prompts: dict[str, str], timeout_sec: int = 300
) -> list[AgentInvocationRequest]:
    """Build the exact 3-observer ``AgentInvocationRequest`` list matching
    ``EXPECTED_OBSERVER_MANIFEST`` (Issue #2237 P0-2/P0-6). ``prompts`` maps
    each ``observer_id`` to the prompt text the caller (the root Skill via
    ``main``'s ``--prompts-file``, or ``run_cli``'s own
    ``_default_observer_prompt`` fallback -- Issue #2345) has already
    assembled -- this function never resolves session/evidence content
    itself (that remains the root Skill's trigger-time responsibility).

    Issue #2345 fix_delta (OWNER review
    https://github.com/squne121/loop-protocol/pull/2347#issuecomment-5417901341,
    P2 item 3): every ``observer_id`` in ``EXPECTED_OBSERVER_MANIFEST``
    MUST have a non-empty (post-``strip()``) prompt in ``prompts`` -- a
    missing key or an empty/whitespace-only string is rejected fail-closed
    with a typed ``WireContractError`` (``reason_code=
    "invalid_observer_prompts"``) here, locally, before any ``claude`` CLI
    subprocess is ever invoked. This replaces the previous silent
    ``prompts.get(spec.observer_id, "")`` fallback, which let an
    incomplete/partial caller-supplied ``prompts`` dict silently reproduce
    the original empty-prompt-reaches-the-CLI bug this Issue fixes."""
    missing_or_empty = [
        spec.observer_id
        for spec in EXPECTED_OBSERVER_MANIFEST
        if not str(prompts.get(spec.observer_id, "")).strip()
    ]
    if missing_or_empty:
        raise WireContractError(
            f"invalid_observer_prompts:missing_or_empty={sorted(missing_or_empty)}",
            reason_code="invalid_observer_prompts",
        )
    return [
        AgentInvocationRequest(
            agent_name=spec.observer_id,
            prompt=prompts[spec.observer_id],
            json_schema_path=str(schema_dir / "observer_result_v1.schema.json"),
            cwd=cwd,
            timeout_sec=timeout_sec,
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
    P1 item 1): ``None`` (the default, matching ``main()`` when
    ``--prompts-file`` is omitted) means "build the default observer
    prompts AFTER this call graph's own ``prepare()`` step below has
    produced the REAL ``ctx.run_id``/``ctx.base_sha``/
    ``plan.source_set_digest`` for this run" -- never a fixed placeholder
    identity. A caller-supplied dict (from ``--prompts-file``, or a direct
    test/Skill caller) is used as-is and validated by
    ``build_observer_requests`` (every manifest ``observer_id`` must map to
    a non-empty prompt)."""
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
        resolved_prompts = (
            prompts
            if prompts is not None
            else {
                spec.observer_id: _default_observer_prompt(
                    spec.observer_id,
                    run_id=ctx.run_id,
                    base_sha=ctx.base_sha,
                    source_set_digest=plan.source_set_digest,
                )
                for spec in EXPECTED_OBSERVER_MANIFEST
            }
        )
        observer_requests = build_observer_requests(
            schema_dir=schema_dir, cwd=str(repo_root), prompts=resolved_prompts
        )

        def _invoke(request: AgentInvocationRequest) -> AgentInvocationResult:
            run_scoped_env = {
                **request.env,
                f"{_RUN_SCOPED_ENV_PREFIX}RUN_ID": ctx.run_id,
                f"{_RUN_SCOPED_ENV_PREFIX}BASE_SHA": ctx.base_sha,
            }
            return invoke_agent(dataclasses.replace(request, env=run_scoped_env), runner=runner, policy=policy)

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

        evaluation = run_evaluation(ctx, evaluator_request, invoke_evaluator=_invoke_evaluator)
        resolved_provider = (
            previous_state_provider
            if previous_state_provider is not None
            else FixturePreviousStateProvider(fixtures={})
        )
        previous_state = resolved_provider.get(
            repository_id=repository_id,
            scope=previous_state_scope,
            finding_identity_algorithm=_default_finding_identity_algorithm(),
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
