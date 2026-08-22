#!/usr/bin/env python3
"""run_retrospective.py -- agent-retrospective deterministic phase engine (Issue #2237).

Implements the four deterministic phases owned by ``run_retrospective.py``
(``prepare`` / ``validate-observers`` / ``prepare-evaluator`` / ``finalize``)
plus the supporting building blocks fixed by Issue #2237's Outcome section:

  - the ephemeral wire contract (``SourcePlan`` / ``EvidenceBundle`` /
    ``FindingSet`` / ``EvaluatorRequest`` / ``Evaluation`` / ``PublishRequest``)
    as strict dataclasses with a round-trippable JSON serializer/deserializer
  - a production Agent invocation adapter (headless CLI subprocess:
    ``claude -p --output-format json --json-schema``)
  - a ``PreviousStateProvider`` read-only port (fixture/in-memory
    implementation; the persistence-backed production provider is #2238)
  - the ``PUBLISH_REQUEST_V1`` producer schema (proposal-only, forbidden
    fields rejected structurally)
  - a delegated-Agent permission policy / tool callback
  - a run-scoped temp artifact directory with mode ``0700`` and cleanup on
    every exit path (success / exception / SIGINT / SIGTERM)

Orchestration owner is the root Skill (``SKILL.md``'s procedure, run by the
main conversation) -- this module never calls Claude Code's ``Agent`` tool
itself; it only provides the deterministic phase functions the root Skill
composes around each ``Agent`` tool call it makes directly. See the Issue
#2237 body "Orchestration owner" section and
``.claude/skills/agent-retrospective/references/`` for the full rationale.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
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


def compute_source_set_digest(source_observations: list[dict[str, Any]]) -> str:
    """Reuse Child 2's canonical ``source_set_digest`` computation (Issue
    #2235/#2236). Loaded lazily so importing this module never requires the
    sibling scripts to be importable unless a caller actually needs the
    digest (e.g. pure wire-contract unit tests)."""
    return _validate_retrospective_schema_module().compute_source_set_digest(source_observations)


# ---------------------------------------------------------------------------
# ephemeral wire contract (P0-2): strict dataclass serializer/deserializer
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

#: forbidden fields on PUBLISH_REQUEST_V1 (Issue #2237 P0-4). These are never
#: declared as PublishRequest dataclass fields, so the generic strict
#: deserializer already rejects them as "unknown_field" -- listed here only
#: so tests/documentation can assert the forbidden set explicitly.
PUBLISH_REQUEST_FORBIDDEN_FIELDS = frozenset(
    {"authorized", "authorized_by_human", "authorization_token", "mutation_capability"}
)


class WireContractError(ValueError):
    """Raised for any ephemeral wire contract violation: JSON decode
    failure, missing field, unknown field, field-type mismatch, oversize
    payload, or schema_version mismatch (Issue #2237 P0-2)."""

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


@dataclass
class _WireEnvelope:
    """Mixin base for every ephemeral wire contract dataclass. Subclasses
    MUST be ``@dataclass``. ``to_wire``/``from_wire`` implement the strict
    round-trippable serializer/deserializer required by AC7: unknown fields,
    missing fields, field-type mismatches, oversize payloads, and malformed
    JSON are all rejected fail-closed."""

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

        try:
            instance = cls(**payload)
        except TypeError as exc:  # pragma: no cover - defensive, shape already checked above
            raise WireContractError(f"construction_failed:{exc}", reason_code="construction_failed") from exc
        instance._post_validate()
        return instance

    def _post_validate(self) -> None:
        """Hook for subclass-specific structural checks beyond generic field
        shape (e.g. ``schema_version`` must equal the canonical wire id).
        Cross-envelope checks such as ``run_id`` agreement are the caller's
        responsibility (see ``validate_run_id_agreement``), not this hook's,
        because a single envelope cannot know about its peers."""
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
    attempt to smuggle a ``private_evidence`` (or similarly-shaped) key is
    rejected as ``unknown_field`` by ``from_wire`` (AC10)."""

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
    only by the ``finalize`` phase."""

    schema_version: str = WIRE_SCHEMA_EVALUATION
    run_id: str = ""
    base_sha: str = ""
    source_set_digest: str = ""
    candidate_records: list[dict[str, Any]] = field(default_factory=list)
    evidence_ref: str = ""

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_EVALUATION)


@dataclass
class PublishRequest(_WireEnvelope):
    """``PUBLISH_REQUEST_V1``: proposal-only envelope (Issue #2237 P0-4).
    Contains no mutation authority / human-approval trust root field --
    ``authorized``/``authorized_by_human``/``authorization_token``/
    ``mutation_capability`` are not declared fields, so any input containing
    one of them is rejected as ``unknown_field`` by ``from_wire`` (AC16).
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

    def _post_validate(self) -> None:
        _require_schema_version(self, WIRE_SCHEMA_PUBLISH_REQUEST)
        if self.authorization_required is not True:
            raise WireContractError("authorization_required_must_be_true", reason_code="invalid_value")


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


# ---------------------------------------------------------------------------
# production Agent invocation adapter (AC13)
# ---------------------------------------------------------------------------


@dataclass
class AgentInvocationRequest:
    """Typed request for a single headless CLI subprocess Agent invocation."""

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


def _stdout_excerpt(text: str | None) -> str | None:
    if not text:
        return None
    return text[:_MAX_STDOUT_EXCERPT]


def build_agent_invocation_argv(request: AgentInvocationRequest) -> list[str]:
    """Construct the ``claude -p --output-format json --json-schema <schema>``
    argv for ``request``. Subscription login is assumed (no ``--bare``)."""
    return [
        "claude",
        "-p",
        request.prompt,
        "--output-format",
        "json",
        "--json-schema",
        request.json_schema_path,
    ]


def invoke_agent(
    request: AgentInvocationRequest,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> AgentInvocationResult:
    """Production Agent invocation adapter. ``runner`` is dependency-injected
    (defaults to ``subprocess.run``) so tests exercise this exact function
    via a subprocess mock harness without starting a real process (AC13)."""
    argv = build_agent_invocation_argv(request)
    try:
        completed = runner(
            argv,
            cwd=request.cwd,
            env={**os.environ, **request.env},
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

    return AgentInvocationResult(
        status="ok",
        structured_output=payload,
        raw_stdout_excerpt=None,
        exit_code=completed.returncode,
        reason_code=None,
    )


# ---------------------------------------------------------------------------
# validate-observers phase (fan-out + fail-closed fan-in)
# ---------------------------------------------------------------------------


class ObserverWaveFailed(Exception):
    """Raised when any observer invocation in the wave fails (non-``ok``
    status, schema repair exhaustion, or run/digest mismatch). Per
    ``partial_agent_output: reject``, the caller MUST NOT invoke the
    evaluator once this is raised -- see ``run_evaluation``'s precondition
    and ``execute_run``'s ordering."""


def run_observer_wave(
    ctx: RunContext,
    plan: SourcePlan,
    *,
    invoke: Callable[[AgentInvocationRequest], AgentInvocationResult],
    observer_requests: Sequence[AgentInvocationRequest],
    repair: Callable[[str, WireContractError], str] | None = None,
) -> list[EvidenceBundle]:
    """``validate-observers`` phase (fan-out half): invoke every observer in
    ``observer_requests`` and strictly validate its serialized output into an
    ``EvidenceBundle``. All observers must succeed -- the first failure
    aborts the wave (fail-closed; ``observer_parallelism: 3`` is an execution
    budget for the caller's actual concurrency, not modeled by this
    sequential reference implementation)."""
    bundles: list[EvidenceBundle] = []
    for request in observer_requests:
        result = invoke(request)
        if result.status != "ok":
            raise ObserverWaveFailed(f"observer_failed:{request.agent_name}:{result.status}")
        raw_text = json.dumps(result.structured_output, sort_keys=True, separators=(",", ":"))
        bundle = parse_agent_output_with_repair(raw_text, EvidenceBundle, repair=repair)
        if bundle.run_id != ctx.run_id or bundle.source_set_digest != plan.source_set_digest:
            raise ObserverWaveFailed(f"observer_envelope_mismatch:{request.agent_name}")
        bundles.append(bundle)
    return bundles


def build_finding_sets(ctx: RunContext, plan: SourcePlan, bundles: Sequence[EvidenceBundle]) -> list[FindingSet]:
    """Fan-in half of ``validate-observers``: project each validated
    ``EvidenceBundle`` into a schema-controlled ``FindingSet`` (AC10 -- only
    this projection, never the bundle's ``evidence_ref``/raw channel, ever
    reaches the evaluator)."""
    return [
        FindingSet(
            run_id=ctx.run_id,
            base_sha=ctx.base_sha,
            source_set_digest=plan.source_set_digest,
            observer_id=bundle.observer_id,
            findings=bundle.findings,
        )
        for bundle in bundles
    ]


# ---------------------------------------------------------------------------
# prepare-evaluator + evaluation (AC9: strict ordering)
# ---------------------------------------------------------------------------


class EvaluatorInvocationFailed(Exception):
    """Raised when the evaluator invocation itself fails or returns an
    envelope that fails cross-validation against the run."""


def prepare_evaluator_request(
    ctx: RunContext, plan: SourcePlan, finding_sets: Sequence[FindingSet]
) -> EvaluatorRequest:
    """``prepare-evaluator`` phase: build the single ``EvaluatorRequest`` fed
    to the fresh-context evaluator invocation. This function's mere existence
    as a caller-invoked step (never auto-chained from ``run_observer_wave``)
    is what lets ``execute_run`` prove observer-wave completion precedes
    evaluator invocation (AC9)."""
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
    validate its output. The caller (``execute_run``) is responsible for only
    calling this after ``run_observer_wave`` has succeeded for every observer
    in the wave (AC9)."""
    result = invoke_evaluator(evaluator_request)
    if result.status != "ok":
        raise EvaluatorInvocationFailed(f"evaluator_failed:{result.status}")
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
) -> PublishRequest:
    """``finalize`` phase: produce the proposal-only ``PublishRequest``. This
    function performs no I/O, no GitHub/Issue mutation, and no filesystem
    write -- it only returns a value (AC11)."""
    projection = {
        "run_id": ctx.run_id,
        "base_sha": ctx.base_sha,
        "candidate_records": evaluation.candidate_records,
    }
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return PublishRequest(
        request_id=request_id,
        repository_id=repository_id,
        target_issue=target_issue,
        run_identity={"run_id": ctx.run_id, "base_sha": ctx.base_sha, "source_set_digest": plan.source_set_digest},
        candidate_records=evaluation.candidate_records,
        expected_previous_digest=expected_previous_digest,
        idempotency_key=idempotency_key,
        public_projection_digest=digest,
        authorization_required=True,
    )


# ---------------------------------------------------------------------------
# whole-run orchestration helper (composes the four phases; still performs
# no Agent tool call itself -- `invoke`/`invoke_evaluator` are injected by
# the root Skill)
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
) -> PublishRequest:
    """Reference composition of ``prepare`` -> ``validate-observers`` ->
    ``prepare-evaluator`` -> evaluator invocation -> ``finalize``. Raises
    ``ObserverWaveFailed``/``EvaluatorInvocationFailed``/``WireContractError``
    fail-closed on any phase failure; never invokes the evaluator unless
    every observer succeeded (AC9)."""
    ctx, plan, _results = prepare(
        base_sha_resolver=base_sha_resolver, collectors=collectors, clock=clock, run_id=run_id
    )
    bundles = run_observer_wave(ctx, plan, invoke=invoke, observer_requests=observer_requests)
    finding_sets = build_finding_sets(ctx, plan, bundles)
    evaluator_request = prepare_evaluator_request(ctx, plan, finding_sets)
    evaluation = run_evaluation(ctx, evaluator_request, invoke_evaluator=invoke_evaluator)
    return finalize(
        ctx,
        plan,
        evaluation,
        repository_id=repository_id,
        target_issue=target_issue,
        request_id=request_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# PreviousStateProvider (P0-3): read-only port, fixture/in-memory only
# ---------------------------------------------------------------------------

PREVIOUS_STATE_STATUSES = frozenset({"available", "no_history", "legacy_unavailable", "partial", "stale"})
DELTA_STATUSES = frozenset({"new", "resolved", "recurrent", "regressed", "unchanged"})

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass
class PreviousStateResult:
    """``PREVIOUS_RETROSPECTIVE_STATE_V1``: the read-only port's output
    shape. ``status`` is one of ``PREVIOUS_STATE_STATUSES``."""

    status: str
    previous_run_ref: str | None
    candidates: list[dict[str, Any]]
    read_version: str | None

    def __post_init__(self) -> None:
        if self.status not in PREVIOUS_STATE_STATUSES:
            raise ValueError(f"invalid PreviousStateResult.status: {self.status!r}")


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


def compute_delta(previous: PreviousStateResult, current_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify each candidate in ``current_candidates`` (each must carry a
    ``finding_identity`` and may carry ``candidate_status``/``severity``)
    against ``previous`` into one of ``DELTA_STATUSES``."""
    if previous.status in ("no_history", "legacy_unavailable"):
        return [{"finding_identity": c.get("finding_identity"), "delta_status": "new"} for c in current_candidates]

    prev_by_identity = {c.get("finding_identity"): c for c in previous.candidates}
    current_identities: set[Any] = set()
    results: list[dict[str, Any]] = []

    for candidate in current_candidates:
        identity = candidate.get("finding_identity")
        current_identities.add(identity)
        prev = prev_by_identity.get(identity)
        if prev is None:
            delta_status = "new"
        elif prev.get("candidate_status") == "resolved" and candidate.get("candidate_status") != "resolved":
            delta_status = "recurrent"
        else:
            prev_rank = _SEVERITY_RANK.get(prev.get("severity"), -1)
            cur_rank = _SEVERITY_RANK.get(candidate.get("severity"), -1)
            delta_status = "regressed" if cur_rank > prev_rank >= 0 else "unchanged"
        results.append({"finding_identity": identity, "delta_status": delta_status})

    for identity, prev in prev_by_identity.items():
        if identity not in current_identities and prev.get("candidate_status") != "resolved":
            results.append({"finding_identity": identity, "delta_status": "resolved"})

    return results


# ---------------------------------------------------------------------------
# delegated-Agent permission policy / tool callback (P0-5)
# ---------------------------------------------------------------------------

_DENIED_BASH_SUBSTRINGS = (
    "git commit",
    "git push",
    "gh issue",
    "gh pr",
    "gh api",
    "gh comment",
    "gh release",
)
_DENIED_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Skill"})


class PermissionDenied(Exception):
    def __init__(self, message: str, *, command: str) -> None:
        super().__init__(message)
        self.command = command


class DelegatedAgentPermissionPolicy:
    """Permission policy / tool callback enforced by the root Skill around
    every delegated observer/evaluator Agent invocation (Issue #2237 P0-5
    capability matrix "本番制約" column). Denies ``git commit``/``git push``,
    ``gh issue``/``gh pr``/comment/api mutation, filesystem write, any
    non-allowlisted Bash command, and resuming a session belonging to a
    different run."""

    def __init__(self, *, run_id: str, allowed_bash_commands: frozenset[str] = frozenset()) -> None:
        self.run_id = run_id
        self.allowed_bash_commands = allowed_bash_commands

    def check_bash(self, command: str) -> None:
        normalized = " ".join(command.split())
        for pattern in _DENIED_BASH_SUBSTRINGS:
            if pattern in normalized:
                raise PermissionDenied(f"denied_bash_pattern:{pattern}", command=command)
        if self.allowed_bash_commands and normalized not in self.allowed_bash_commands:
            raise PermissionDenied("bash_not_allowlisted", command=command)

    def check_filesystem_write(self, path: str) -> None:
        raise PermissionDenied(f"filesystem_write_denied:{path}", command=f"write:{path}")

    def check_tool(self, tool_name: str) -> None:
        if tool_name in _DENIED_TOOLS:
            raise PermissionDenied(f"tool_denied:{tool_name}", command=tool_name)

    def check_resume(self, session_run_id: str) -> None:
        if session_run_id != self.run_id:
            raise PermissionDenied(f"cross_run_resume_denied:{session_run_id}", command=f"resume:{session_run_id}")


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
    always restored in ``finally``, regardless of how the block exits."""
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
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - not a CLI entrypoint by itself
    print(
        "run_retrospective.py provides deterministic phase functions for the "
        "agent-retrospective SKILL.md procedure; it is not invoked directly.",
        file=sys.stderr,
    )
    sys.exit(2)
