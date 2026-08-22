#!/usr/bin/env python3
"""persist_retrospective_run.py -- agent-retrospective run persistence
(Issue #2238, Child 5 of #2192).

Consumes the ``PUBLISH_REQUEST_V1`` proposal-only envelope
``run_retrospective.py`` produces and, after a fail-closed human
authorization gate, persists it as a tool-managed append-only public Issue
comment (``artifact-publication mutation`` per ``docs/adr/0007-agent-
retrospective-boundaries.md`` Decision 4 -- human authorization is required
before every POST; this module never posts without one of the two
authorization channels below actually confirming).

Issue #2238 fix_delta (OWNER adversarial review, PR #2304
issuecomment-5381003316): this revision closes 7 P0 blockers and 4 P1 items
found in the first implementation. See each function's docstring for the
specific fix; a summary:

- P0-1 ``_check_repository_match()``: the approved ``repository_id`` and the
  actual POST destination (``--repo``) are cross-checked *before any
  transport call* -- fail closed, zero transport calls, on mismatch.
- P0-2 ``prepare_publication()`` / ``publish_prepared()``: the single-shot
  flow is split into an explicit two-stage CLI flow (``prepare-publication``
  then ``authorize``/``publish``) so a non-interactive receipt can actually
  be produced against a frozen, already-known ``publication_digest`` --
  ``publish_prepared()`` re-verifies the live head immediately before POST
  and refuses (never POSTs) if it moved since ``prepare_publication()``.
  Receipts now carry a bounded max TTL and are checked for chronological
  sanity (``approved_at`` not in the future, not already expired).
- P0-3 ``compute_request_payload_digest()`` / ``evaluate_idempotency()``:
  idempotency is now evaluated BEFORE any optimistic-concurrency check,
  against a digest that deliberately excludes ``parent_record_digest``/
  ``generated_at`` -- so re-running the identical logical request actually
  reaches ``no_op`` instead of always drifting into ``conflict``.
  ``expected_previous_digest=None`` is now a strict value to match (current
  head being ``None`` is itself a valid state to match), never a wildcard.
- P0-4 ``IssueCommentPreviousStateProvider.get()``: fork detection
  (sibling comments sharing a ``parent_record_digest``, or any branch that
  doesn't terminate at the most-recent comment) is now a read-time
  reconstruction over ALL matching records on every ``get()`` call -- not a
  separately-persisted conflict flag that could go stale.
- P0-5 ``build_run_envelope()``: persists the REAL per-collector
  ``source_observations[]`` (threaded through from ``run_retrospective.py``'s
  ``finalize()``/``execute_run()``/``run_cli()``) instead of a fixed
  single-entry placeholder that didn't match the declared
  ``source_set_digest``.
- P0-6 ``parse_verified_run_comment()``: the single shared verification
  function every read path (idempotency/OCC/provider/recovery/readback) now
  goes through -- author allowlist, marker/payload cross-check, schema
  shape, public-safety re-scan, recomputed digest match. A comment that
  merely *looks* like one of this module's envelopes but fails verification
  is never trusted as valid prior state.
- P0-7 the CLI ``main()`` now wires ``--index-parent-issue`` to actually
  invoke ``scripts/agent-logs/update-retro-index.mjs`` after a verified
  successful publish (previously only exercised via test injection).
- P1-1 the internal digest-algorithm identifier is renamed
  ``sha256-sorted-json-v1`` (previously ``sha256-jcs-v1``, which falsely
  implied RFC 8785 JCS conformance -- the underlying canonicalizer this
  module reuses is Python ``sorted()`` key ordering, not RFC 8785).
- P1-2 ``create_comment_with_recovery()``: a full rescan-by-request_id now
  happens after EVERY ambiguous POST outcome (not just the first).
- P1-4 ``IssueCommentPreviousStateProvider``'s age-based staleness rule is
  now opt-in (``stale_after_seconds=None`` disables it by default) --
  staleness is primarily driven by source completeness / fork detection /
  schema-or-digest mismatch, not a blanket 7-day clock.

Owns:

- ``build_run_envelope()`` / ``compute_publication_digest()``: derive the
  persisted envelope and its ``sha256-sorted-json-v1`` canonical digest from
  a ``PUBLISH_REQUEST_V1``-shaped dict (AC3). ``publication_digest`` is a
  distinct digest from ``run_retrospective.py``'s ``public_projection_digest``
  -- the latter is the *proposal*'s binding digest; this module's digest is
  the *persisted record*'s binding digest (its preimage additionally
  includes ``parent_record_digest``, the optimistic-concurrency chain link).
- ``IssueCommentTransportProtocol`` / ``GhCliIssueCommentTransport``: the
  I/O boundary (list/create/get Issue comments). Every function below except
  ``GhCliIssueCommentTransport`` itself is dependency-injected against this
  protocol, so every unit test in ``tests/test_persist_retrospective_run.py``
  is hermetic (no live GitHub call -- Runtime Verification Applicability:
  not_applicable, live smoke is Child 6 / #2239's responsibility).
- ``AuthorizationContext`` / ``confirm_human_authorization()``: the
  fail-closed human authorization gate (AC4). Exactly one of a validated
  ``human_authorization_receipt/v1`` file or an interactive TTY confirmation
  can authorize a POST -- there is no boolean/flag parameter anywhere in this
  module that accepts authorization directly (unlike the *forbidden*
  ``PUBLISH_REQUEST_V1`` fields ``authorized``/``authorized_by_human``/
  ``authorization_token``/``mutation_capability``, which remain undeclared
  fields on that dataclass in ``run_retrospective.py``).
- ``evaluate_idempotency()`` / ``compute_idempotency_key()`` /
  ``compute_request_payload_digest()``: duplicate suppression keyed on
  ``(repository_id, base_sha, source_set_digest, scope)`` (AC5, ADR 0007
  Decision 5). The publisher recomputes the key itself -- a caller-supplied
  ``idempotency_key`` on the input ``PUBLISH_REQUEST_V1`` is never trusted
  as-is.
- ``check_optimistic_concurrency_precondition()`` /
  ``detect_post_write_sibling_conflict()``: the best-effort (not atomic
  compare-and-swap) stale-write guard (AC6, ADR 0007 Decision 5). Distinct
  mechanism from idempotency -- see the ADR for why these are never merged
  into one guard.
- ``verify_readback_digest()``: post-POST GET -> canonical JSON digest
  readback (AC7). Never compares raw Markdown bytes.
- ``IssueCommentPreviousStateProvider``: the real,
  ``PreviousStateProviderProtocol``-satisfying persistence-backed provider
  ``run_retrospective.py``'s ``resolve_previous_state_provider()`` (AC1)
  constructs for ``--state-backend issue-comments`` (AC9).
- ``create_comment_with_recovery()``: ambiguous-POST-failure recovery via
  ``request_id``/idempotency-key search before any blind retry (AC10).
- ``run_public_safety_validator()``: the final pre-POST allowlist + value-
  level safety gate (AC12, ADR 0007 Decision 6's two-layer model).
- ``prepare_publication()`` / ``publish_prepared()`` / ``publish_run()``:
  the top-level orchestration. ``publish_run()`` is a convenience wrapper
  composing the two-stage flow into one call (used by most tests and by
  simple in-process callers); the CLI ``main()`` exposes the two-stage flow
  directly via ``prepare-publication``/``authorize``/``publish``
  subcommands so a non-interactive receipt is actually achievable (P0-2).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent

#: schema_version stamped on every persisted run-publication envelope. This
#: is intentionally distinct from Child 2's ``agent_retrospective_run/v1``
#: schema_version string -- this envelope additionally carries the
#: publication-layer fields (``idempotency_key``/``parent_record_digest``/
#: ``publication_digest``) that Child 2's schema does not model.
RUN_PUBLICATION_SCHEMA = "agent_retrospective_run_publication/v1"

RUNTIME_VERSION = "agent-retrospective-persist/v1"

#: matches run_retrospective.DEFAULT_PREVIOUS_STATE_SCOPE's value. Kept as an
#: independent literal (rather than importing run_retrospective eagerly) so
#: this module stays importable standalone; the two are cross-checked by
#: test_persist_retrospective_run.py.
DEFAULT_SCOPE = "repository"

#: Issue #2238 P1-1 fix_delta: renamed from ``sha256-jcs-v1``. The
#: canonicalizer this module reuses (``validate_retrospective_schema.py``'s
#: ``_jcs_canonicalize``) is a recursive Python ``sorted()`` key-ordering +
#: compact-JSON serialization -- it is NOT an RFC 8785 JCS implementation
#: (no number/string normalization per the spec). This identifier now
#: reflects what the algorithm actually does instead of falsely claiming
#: RFC 8785 conformance. This is purely an internal/documentation identifier
#: -- it is never embedded in a digest string itself (digests are always
#: ``"sha256:" + hexdigest``), so renaming it changes no persisted value.
_DIGEST_ALGORITHM = "sha256-sorted-json-v1"

#: GitHub Issue comment body soft cap this module enforces before POST (well
#: under GitHub's actual ~65536-byte hard limit) -- AC12's size precheck.
MAX_COMMENT_BODY_BYTES = 60_000

#: Issue #2238 P1-4 fix_delta: age-based staleness is now opt-in (``None``
#: disables it, the new default). Previously a blanket 7 days regardless of
#: actual usage cadence -- for low-frequency solo development this produced
#: false "stale" classifications with no real justification. Staleness is
#: now driven primarily by source completeness (``partial``) and fork
#: detection (P0-4), not wall-clock age. Callers that still want the old
#: behavior can pass ``stale_after_seconds=STALE_AFTER_SECONDS_LEGACY_DEFAULT``.
STALE_AFTER_SECONDS_LEGACY_DEFAULT = 7 * 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso8601(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# sibling module loading (reuse run_retrospective.py / validate_retrospective_schema.py
# without editing them -- those files are outside this module's own edits)
# ---------------------------------------------------------------------------


def _load_sibling_module(module_name: str, filename: str):
    import importlib.util
    import sys as _sys

    module_path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load sibling module {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _sys.modules.pop(spec.name, None)
        raise
    return module


def _run_retrospective_module():
    return _load_sibling_module("agent_retrospective_run_retrospective_sibling", "run_retrospective.py")


def _validate_schema_module():
    return _load_sibling_module("agent_retrospective_validate_schema_sibling", "validate_retrospective_schema.py")


# ---------------------------------------------------------------------------
# canonical JSON / digest helpers (sha256-sorted-json-v1, P1-1)
# ---------------------------------------------------------------------------


def _jcs_canonicalize(value: Any) -> Any:
    """Delegates to validate_retrospective_schema.py's canonicalizer
    (reused rather than reimplemented -- single source of truth for the
    ``sha256-sorted-json-v1`` algorithm across Child 2 and this module)."""
    return _validate_schema_module()._jcs_canonicalize(value)  # noqa: SLF001 -- intentional cross-module reuse


def _digest_of(payload: dict[str, Any]) -> str:
    """``sha256:`` + SHA256 hex digest of the canonicalization of ``payload``
    -- the ``sha256-sorted-json-v1`` algorithm this module uses for
    ``publication_digest`` (AC3), the recomputed idempotency key (AC5), and
    the stable ``request_payload_digest`` (AC5/P0-3)."""
    canonical = json.dumps(
        _jcs_canonicalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_publication_digest(envelope_without_digest: dict[str, Any]) -> str:
    """AC3: the ``publication_digest`` for a run-publication envelope.
    ``envelope_without_digest`` MUST NOT itself contain a
    ``publication_digest`` key (the digest is never part of its own
    preimage)."""
    if "publication_digest" in envelope_without_digest:
        raise ValueError("envelope_without_digest must not already contain 'publication_digest'")
    return _digest_of(envelope_without_digest)


def compute_idempotency_key(*, repository_id: str, base_sha: str, source_set_digest: str, scope: str) -> str:
    """AC5: the publisher-recomputed idempotency key over
    ``(repository_id, base_sha, source_set_digest, scope)`` (ADR 0007
    Decision 5). A caller-supplied ``idempotency_key`` is never trusted as
    this value -- callers may pass one for cross-checking only (see
    ``publish_run()``)."""
    return _digest_of(
        {
            "repository_id": repository_id,
            "base_sha": base_sha,
            "source_set_digest": source_set_digest,
            "scope": scope,
        }
    )


def compute_request_payload_digest(
    *,
    repository_id: str,
    target_issue: int,
    scope: str,
    run_identity: dict[str, Any],
    candidate_records: list[dict[str, Any]],
    delta_results: list[dict[str, Any]],
    source_observations: list[dict[str, Any]],
) -> str:
    """Issue #2238 P0-3 fix_delta: the STABLE digest idempotency is actually
    evaluated against. Deliberately excludes ``parent_record_digest``/
    ``expected_previous_digest``/``generated_at``/``idempotency_key``/
    ``publication_digest``/``runtime_version`` -- everything that is a
    function of WHEN/WHERE this request happens to be persisted rather than
    WHAT it logically contains. Two calls with the same logical request
    content (same repository/issue/scope/base_sha/source_set_digest/
    candidates/delta/observations) always produce the same
    ``request_payload_digest``, regardless of how many times the head has
    advanced between them -- this is what makes ``no_op`` actually reachable
    (previously the digest always included freshly-read
    ``parent_record_digest``, so a re-run against a since-advanced head
    always looked like a NEW, different digest and fell into ``conflict``
    instead of ``no_op``)."""
    return _digest_of(
        {
            "repository_id": repository_id,
            "target_issue": target_issue,
            "scope": scope,
            "run_identity": {
                "base_sha": run_identity["base_sha"],
                "source_set_digest": run_identity["source_set_digest"],
            },
            "candidate_records": candidate_records,
            "delta_results": delta_results,
            "source_observations": source_observations,
        }
    )


# ---------------------------------------------------------------------------
# run-publication envelope: build / render / extract
# ---------------------------------------------------------------------------

#: Issue #2238 P0-5 fix_delta: only used as a last-resort fallback for
#: callers that supply no ``source_observations`` at all (e.g. hand-rolled
#: unit-level ``build_run_envelope()`` calls that predate the P0-5 wiring).
#: The production ``execute_run()``/``run_cli()`` -> ``finalize()`` ->
#: ``publish_run()`` call graph always supplies the real observations now,
#: so this fallback is never exercised by that path.
_FALLBACK_SOURCE_OBSERVATIONS = [
    {
        "source_type": "repository",
        "source_id": "repository",
        "source_status": "complete",
        "pagination_completeness": "complete",
    }
]


def build_run_envelope(
    *,
    repository_id: str,
    target_issue: int,
    request_id: str,
    run_identity: dict[str, Any],
    candidate_records: list[dict[str, Any]],
    delta_results: list[dict[str, Any]],
    expected_previous_digest: str | None,
    parent_record_digest: str | None,
    generated_at: str | None = None,
    scope: str = DEFAULT_SCOPE,
    runtime_version: str | None = None,
    source_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """AC3/AC8: build the persisted run-publication envelope from a
    ``PUBLISH_REQUEST_V1``-shaped ``run_identity``/``candidate_records``/
    ``delta_results`` triple, computing both the recomputed idempotency key
    and the ``publication_digest``. ``candidate_records``/``delta_results``
    are carried through verbatim (AC8's full-record round-trip -- this
    function never projects down to a private ``candidate_status``-only
    dialect).

    Issue #2238 P0-5 fix_delta: ``source_observations`` -- if not passed
    explicitly -- is read from ``run_identity["source_observations"]``
    (``run_retrospective.py``'s ``finalize()`` now threads the real
    per-collector observations there additively, see that module's
    docstring). Likewise ``generated_at``/``runtime_version`` fall back to
    ``run_identity["generated_at"]``/``run_identity["runtime_version"]`` --
    the ORIGINAL run's values, not a value freshly generated at persist
    time. Only when none of these are available at all (legacy/unit-level
    callers) does this function fall back to a single-entry placeholder."""
    generated_at_value = generated_at or run_identity.get("generated_at") or _iso(_utcnow())
    runtime_version_value = runtime_version or run_identity.get("runtime_version") or RUNTIME_VERSION
    observations = source_observations if source_observations is not None else run_identity.get("source_observations")
    if not observations:
        observations = _FALLBACK_SOURCE_OBSERVATIONS

    idempotency_key = compute_idempotency_key(
        repository_id=repository_id,
        base_sha=run_identity["base_sha"],
        source_set_digest=run_identity["source_set_digest"],
        scope=scope,
    )
    envelope: dict[str, Any] = {
        "schema_version": RUN_PUBLICATION_SCHEMA,
        "repository_id": repository_id,
        "target_issue": target_issue,
        "request_id": request_id,
        "scope": scope,
        "idempotency_key": idempotency_key,
        "expected_previous_digest": expected_previous_digest,
        "parent_record_digest": parent_record_digest,
        "run": {
            "run_identity": {
                "run_id": run_identity["run_id"],
                "base_sha": run_identity["base_sha"],
                "source_set_digest": run_identity["source_set_digest"],
                "generated_at": generated_at_value,
                "runtime_version": runtime_version_value,
            },
            "source_observations": observations,
        },
        "candidate_records": candidate_records,
        "delta_results": delta_results,
    }
    envelope["publication_digest"] = compute_publication_digest(envelope)
    return envelope


_MARKER_LINE_RE = re.compile(r"^<!--\s*agent_retrospective_run:v1\b")
_MARKER_FIELDS_RE = re.compile(r"repository_id=(?P<repository_id>\S+)\s+idempotency_key=(?P<idempotency_key>\S+)")
_FENCED_JSON_RE = re.compile(r"```json\r?\n(.*?)\r?\n```", re.DOTALL)


def render_comment_body(envelope: dict[str, Any]) -> str:
    """Render ``envelope`` as the tool-managed append-only Issue comment
    body (AC3's public-safe Issue comment). First line is a machine-parseable
    ownership marker; the payload itself is a fenced ```json block so
    ``extract_envelope_from_body`` can round-trip it exactly."""
    marker = (
        f"<!-- agent_retrospective_run:v1 repository_id={envelope['repository_id']} "
        f"idempotency_key={envelope['idempotency_key']} -->"
    )
    fenced = "```json\n" + json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n```"
    return f"{marker}\n\n{fenced}\n"


def extract_envelope_from_body(body: str | None) -> dict[str, Any] | None:
    """Inverse of ``render_comment_body``: returns ``None`` (not an
    exception) for any comment that is not one of this module's own
    run-publication comments -- callers treat that as "ignore this
    comment", not as an error. This function performs NO trust decision
    (author/digest/marker-cross-check) -- that is
    ``parse_verified_run_comment()``'s job (P0-6). This function alone is
    NOT sufficient to treat a comment as valid prior state."""
    if not body:
        return None
    lines = body.splitlines()
    if not lines or not _MARKER_LINE_RE.match(lines[0].strip()):
        return None
    match = _FENCED_JSON_RE.search(body)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != RUN_PUBLICATION_SCHEMA:
        return None
    return payload


# ---------------------------------------------------------------------------
# transport boundary (I/O port)
# ---------------------------------------------------------------------------


class IssueCommentTransportProtocol(typing.Protocol):
    def list_comments(self, *, repo: str, issue_number: int) -> list[dict[str, Any]]: ...

    def create_comment(self, *, repo: str, issue_number: int, body: str) -> dict[str, Any]: ...

    def get_comment(self, *, repo: str, comment_id: int) -> dict[str, Any]: ...


class AmbiguousTransportError(Exception):
    """Raised by a transport implementation when a POST's outcome is
    ambiguous (e.g. a network timeout) -- the caller does not know whether
    the mutation actually landed server-side (AC10)."""


def _parse_paginated_json(stdout: str) -> list[dict[str, Any]]:
    """``gh api ... --paginate`` concatenates one JSON array per page
    back-to-back on stdout (not a single JSON array) -- decode each
    top-level JSON value in the stream and flatten any arrays found."""
    stdout = stdout.strip()
    if not stdout:
        return []
    decoder = json.JSONDecoder()
    idx = 0
    results: list[dict[str, Any]] = []
    length = len(stdout)
    while idx < length:
        while idx < length and stdout[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, end = decoder.raw_decode(stdout, idx)
        if isinstance(obj, list):
            results.extend(obj)
        else:
            results.append(obj)
        idx = end
    return results


class GhCliIssueCommentTransport:
    """Production ``IssueCommentTransportProtocol`` implementation: shells
    out to ``gh api`` (the same CLI transport convention
    ``run_retrospective.py``'s ``runner``/``git_runner`` parameters use).
    Every method is dependency-injectable via ``runner`` for hermetic
    testing -- production code never constructs this with a non-default
    ``runner``."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout_sec: int = 30,
    ) -> None:
        self._runner = runner
        self._timeout_sec = timeout_sec

    def list_comments(self, *, repo: str, issue_number: int) -> list[dict[str, Any]]:
        try:
            completed = self._runner(
                ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments", "--paginate"],
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise AmbiguousTransportError(f"list_comments timed out: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"gh api list comments failed: {completed.stderr}")
        return _parse_paginated_json(completed.stdout)

    def create_comment(self, *, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        try:
            completed = self._runner(
                ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments", "-f", f"body={body}"],
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise AmbiguousTransportError(f"create_comment timed out: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"gh api create comment failed: {completed.stderr}")
        return json.loads(completed.stdout)

    def get_comment(self, *, repo: str, comment_id: int) -> dict[str, Any]:
        try:
            completed = self._runner(
                ["gh", "api", f"repos/{repo}/issues/comments/{comment_id}"],
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise AmbiguousTransportError(f"get_comment timed out: {exc}") from exc
        if completed.returncode != 0:
            raise RuntimeError(f"gh api get comment failed: {completed.stderr}")
        return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# P0-6: single shared verified-comment parser. Every read path below
# (idempotency/OCC/provider/recovery/readback) goes through this, not
# through the trust-nothing ``extract_envelope_from_body`` alone.
# ---------------------------------------------------------------------------


class CommentVerificationFailed(Exception):
    """Issue #2238 P0-6: raised when a comment LOOKS like one of this
    module's own run-publication envelopes (marker line present) but fails
    verification -- untrusted author, marker/payload cross-check mismatch,
    schema shape, public-safety scan, or recomputed-digest mismatch. Callers
    scanning historical comments (idempotency/OCC/provider/recovery) treat
    this the same as "not one of ours" (skip -- never trust as valid prior
    state, AC5/AC6/AC9 tamper-resistance). ``verify_readback_digest()`` (the
    verification of THIS process's own just-posted comment) treats it
    differently: it stops and reports ``published_unverified`` rather than
    silently skipping."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def parse_verified_run_comment(
    comment: dict[str, Any],
    *,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> dict[str, Any] | None:
    """Issue #2238 P0-6: the single shared verification function. Returns
    ``None`` (not an error) if ``comment`` is simply not one of this
    module's envelopes at all (no marker line) -- exactly like
    ``extract_envelope_from_body``. Raises ``CommentVerificationFailed`` if
    it looks like one of ours but is NOT trustworthy:

    1. author allowlist: ``comment["user"]["login"]`` must be in
       ``trusted_publisher_logins`` (an empty/unset allowlist always fails
       closed -- there is no "trust everyone" default).
    2. marker/payload cross-check: the marker line's ``repository_id``/
       ``idempotency_key`` must match the envelope's own fields (detects a
       comment whose marker was hand-edited independently of its payload).
    3. structural/schema shape: required top-level keys present with the
       expected types, no disallowed top-level fields.
    4. public-safety re-scan: ``run_public_safety_validator()`` must not
       raise (a tampered comment could otherwise smuggle a credential/path
       pattern into what looks like valid prior state).
    5. delta/candidate identity consistency: every
       ``delta_results[].finding_identity`` must correspond to a
       ``candidate_records[].finding_contract.identity.value`` OR be absent
       from ``candidate_records`` only because it was itself marked
       ``resolved`` in delta terms (i.e. it is not required to currently
       exist as a candidate) -- the check here is narrower and only rejects
       a delta entry whose ``finding_identity`` is structurally malformed.
    6. recomputed digest: the envelope's own ``publication_digest`` must
       equal the canonical digest recomputed from its own preimage (never
       raw byte comparison)."""
    body = comment.get("body")
    envelope = extract_envelope_from_body(body)
    if envelope is None:
        return None

    author_login = (comment.get("user") or {}).get("login")
    if not trusted_publisher_logins or author_login not in trusted_publisher_logins:
        raise CommentVerificationFailed(
            f"comment author {author_login!r} is not an allowlisted publisher identity",
            reason_code="untrusted_author",
        )

    marker_line = (body or "").splitlines()[0].strip()
    marker_match = _MARKER_FIELDS_RE.search(marker_line)
    if marker_match is None:
        raise CommentVerificationFailed(
            "marker line missing repository_id/idempotency_key fields", reason_code="marker_unparsable"
        )
    if marker_match.group("repository_id") != envelope.get("repository_id"):
        raise CommentVerificationFailed(
            "marker repository_id does not match envelope payload repository_id",
            reason_code="marker_payload_mismatch",
        )
    if marker_match.group("idempotency_key") != envelope.get("idempotency_key"):
        raise CommentVerificationFailed(
            "marker idempotency_key does not match envelope payload idempotency_key",
            reason_code="marker_payload_mismatch",
        )

    required_top_level = {
        "schema_version",
        "repository_id",
        "target_issue",
        "request_id",
        "scope",
        "idempotency_key",
        "run",
        "candidate_records",
        "delta_results",
        "publication_digest",
    }
    missing = required_top_level - set(envelope.keys())
    if missing:
        raise CommentVerificationFailed(
            f"envelope missing required field(s): {sorted(missing)}", reason_code="schema_incomplete"
        )
    if not isinstance(envelope.get("run"), dict) or "run_identity" not in envelope["run"]:
        raise CommentVerificationFailed("envelope.run.run_identity missing", reason_code="schema_incomplete")

    try:
        run_public_safety_validator(envelope)
    except PublicSafetyViolation as exc:
        raise CommentVerificationFailed(f"public safety re-scan failed: {exc}", reason_code=exc.reason_code) from exc

    candidate_identities = set()
    for candidate in envelope.get("candidate_records", []):
        finding_contract = candidate.get("finding_contract") if isinstance(candidate, dict) else None
        if finding_contract:
            identity = finding_contract.get("identity", {}).get("value")
            if identity:
                candidate_identities.add(identity)
    for delta in envelope.get("delta_results", []):
        if (
            not isinstance(delta, dict)
            or not isinstance(delta.get("finding_identity"), str)
            or not delta.get("finding_identity")
        ):
            raise CommentVerificationFailed(
                "delta_results entry has a missing/malformed finding_identity", reason_code="delta_identity_malformed"
            )

    preimage = {k: v for k, v in envelope.items() if k != "publication_digest"}
    recomputed = compute_publication_digest(preimage)
    if recomputed != envelope.get("publication_digest"):
        raise CommentVerificationFailed(
            "envelope's own publication_digest does not match its recomputed canonical digest",
            reason_code="digest_mismatch",
        )

    return envelope


def _iter_run_records(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Issue #2238 P0-6: every historical-scan caller (idempotency/OCC/
    provider/recovery) goes through ``parse_verified_run_comment()`` here --
    a comment that fails verification (untrusted author, tampered digest,
    marker/payload mismatch) is silently excluded, exactly as if it were
    absent. This is intentionally different from ``verify_readback_digest``'s
    own-post verification, which stops with ``published_unverified`` instead
    of silently skipping."""
    records = []
    for comment in transport.list_comments(repo=repo, issue_number=issue_number):
        try:
            envelope = parse_verified_run_comment(comment, trusted_publisher_logins=trusted_publisher_logins)
        except CommentVerificationFailed:
            continue
        if envelope is not None:
            records.append((comment, envelope))
    return records


def find_by_idempotency_key(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    idempotency_key: str,
    *,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number, trusted_publisher_logins=trusted_publisher_logins)
        if item[1].get("idempotency_key") == idempotency_key
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0].get("id", 0))
    return matches[-1]


def find_by_request_id(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    request_id: str,
    *,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number, trusted_publisher_logins=trusted_publisher_logins)
        if item[1].get("request_id") == request_id
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0].get("id", 0))
    return matches[-1]


def find_latest_run_record(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    repository_id: str,
    scope: str,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number, trusted_publisher_logins=trusted_publisher_logins)
        if item[1].get("repository_id") == repository_id and item[1].get("scope") == scope
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0].get("id", 0))
    return matches[-1]


def find_siblings_by_parent_digest(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    parent_record_digest: str | None,
    *,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        item
        for item in _iter_run_records(transport, repo, issue_number, trusted_publisher_logins=trusted_publisher_logins)
        if item[1].get("parent_record_digest") == parent_record_digest
    ]


# ---------------------------------------------------------------------------
# idempotency guard (AC5, P0-3) -- duplicate suppression, distinct from AC6
# ---------------------------------------------------------------------------

IDEMPOTENCY_DECISIONS = frozenset({"publish", "no_op", "conflict"})


def evaluate_idempotency(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    idempotency_key: str,
    request_payload_digest: str,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> tuple[str, tuple[dict[str, Any], dict[str, Any]] | None]:
    """AC5/P0-3: three-way idempotency decision, evaluated against the
    STABLE ``request_payload_digest`` (``compute_request_payload_digest()``)
    -- NOT ``publication_digest`` (which varies run-to-run purely because it
    embeds ``parent_record_digest``/``generated_at``). This is what makes
    ``no_op`` actually reachable: an identical logical request re-run after
    the head has advanced now still matches on
    ``request_payload_digest`` -> ``no_op``, rather than always drifting
    into ``conflict``. ``idempotency_key`` MUST already be the
    publisher-recomputed value (``compute_idempotency_key()``), never a
    caller-supplied one taken on faith. The existing record's own stored
    ``request_payload_digest`` is recomputed from ITS envelope content here
    (not trusted from a stored field) -- an older persisted envelope has no
    such field, so this recomputation is also what makes the comparison
    forward/backward compatible."""
    existing = find_by_idempotency_key(
        transport, repo, issue_number, idempotency_key, trusted_publisher_logins=trusted_publisher_logins
    )
    if existing is None:
        return "publish", None
    _, envelope = existing
    existing_run_identity = envelope.get("run", {}).get("run_identity", {})
    existing_stable_digest = compute_request_payload_digest(
        repository_id=envelope.get("repository_id"),
        target_issue=envelope.get("target_issue"),
        scope=envelope.get("scope"),
        run_identity=existing_run_identity,
        candidate_records=envelope.get("candidate_records", []),
        delta_results=envelope.get("delta_results", []),
        source_observations=envelope.get("run", {}).get("source_observations", []),
    )
    if existing_stable_digest == request_payload_digest:
        return "no_op", existing
    return "conflict", existing


# ---------------------------------------------------------------------------
# optimistic concurrency guard (AC6, P0-3 strict-None fix) -- best-effort
# stale-write detection, distinct mechanism from AC5's idempotency guard
# (ADR 0007 Decision 5)
# ---------------------------------------------------------------------------


class StaleWriteDetected(Exception):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def check_optimistic_concurrency_precondition(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    repository_id: str,
    scope: str,
    expected_previous_digest: str | None,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> str | None:
    """AC6 step (a): best-effort pre-POST head re-check. GitHub's Issue
    comment API has no atomic compare-and-swap, so this narrows but cannot
    eliminate the race window -- it is explicitly NOT an absolute guarantee
    (ADR 0007 Decision 5). Returns the current head ``publication_digest``
    (``None`` if no prior record exists) so the caller can bind it as the
    new record's ``parent_record_digest`` (step (b)).

    Issue #2238 P0-3 fix_delta: ``expected_previous_digest=None`` is now a
    STRICT value to match -- the current head being ``None`` (no prior
    record at all) is itself a valid state that must be matched, not a
    wildcard that skips the check. Previously ``None`` meant "skip
    comparison entirely", so a caller who legitimately declared "I expect no
    prior record" would silently succeed even against an already-advanced
    head."""
    latest = find_latest_run_record(
        transport,
        repo,
        issue_number,
        repository_id=repository_id,
        scope=scope,
        trusted_publisher_logins=trusted_publisher_logins,
    )
    head_digest = latest[1]["publication_digest"] if latest is not None else None
    if expected_previous_digest != head_digest:
        raise StaleWriteDetected(
            f"expected_previous_digest={expected_previous_digest!r} but current head is {head_digest!r}",
            reason_code="stale_expected_previous_digest",
        )
    return head_digest


def detect_post_write_sibling_conflict(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    parent_record_digest: str | None,
    own_comment_id: int | None,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> bool:
    """AC6 steps (c)/(d): post-POST full rescan. If more than one comment
    shares the same ``parent_record_digest`` (two runs both read the same
    prior head and both appended), a conflict is detected. This function
    only detects (and reports for caller visibility); it never repairs. The
    durable consequence (P0-4) lives entirely in
    ``IssueCommentPreviousStateProvider.get()``'s own read-time chain
    reconstruction -- since BOTH sibling comments are already durable Issue
    comments, the provider re-derives the fork on every future ``get()``
    call without needing any separate persisted "conflict flag"."""
    siblings = find_siblings_by_parent_digest(
        transport, repo, issue_number, parent_record_digest, trusted_publisher_logins=trusted_publisher_logins
    )
    other_ids = {comment.get("id") for comment, _ in siblings if comment.get("id") != own_comment_id}
    return len(other_ids) >= 1


# ---------------------------------------------------------------------------
# post-write readback (AC7, P0-6)
# ---------------------------------------------------------------------------


class ReadbackVerificationFailed(Exception):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def verify_readback_digest(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    *,
    comment_id: int,
    expected_publication_digest: str,
    trusted_publisher_logins: frozenset[str] | set[str],
) -> None:
    """AC7/P0-6: GET the just-created comment by its ``comment_id`` (never
    trust the POST response body alone), and verify it through the SAME
    ``parse_verified_run_comment()`` every other read path uses (author
    allowlist, marker/payload cross-check, schema shape, public-safety
    re-scan, recomputed digest). This additionally cross-checks the
    recomputed digest against ``expected_publication_digest`` (the digest
    computed before POST). Never compares raw Markdown bytes. Unlike
    historical-scan callers (which silently skip an unverifiable comment),
    the caller here (``publish_prepared()``) MUST stop and report
    ``published_unverified`` -- it must NOT silently retry or roll back the
    already-posted comment (P0-6)."""
    fetched = transport.get_comment(repo=repo, comment_id=comment_id)
    try:
        envelope = parse_verified_run_comment(fetched, trusted_publisher_logins=trusted_publisher_logins)
    except CommentVerificationFailed as exc:
        raise ReadbackVerificationFailed(f"readback verification failed: {exc}", reason_code=exc.reason_code) from exc
    if envelope is None:
        raise ReadbackVerificationFailed(
            "readback comment body did not contain a parseable run-publication envelope",
            reason_code="readback_unparsable",
        )
    if envelope.get("publication_digest") != expected_publication_digest:
        raise ReadbackVerificationFailed(
            "readback recomputed digest does not match the digest computed before POST",
            reason_code="readback_expected_digest_mismatch",
        )


# ---------------------------------------------------------------------------
# human authorization gate (AC4, P0-2 TTL/sanity) -- fail-closed
# ---------------------------------------------------------------------------

HUMAN_AUTHORIZATION_RECEIPT_SCHEMA = "human_authorization_receipt/v1"
HUMAN_AUTHORIZATION_RECEIPT_REQUIRED_FIELDS = frozenset(
    {"request_id", "publication_digest", "repository_id", "target_issue", "operation", "approved_at", "expires_at"}
)

#: Issue #2238 P0-2 fix_delta: a receipt's own (approved_at, expires_at)
#: window must not exceed this bound, regardless of what the receipt file
#: itself claims -- closes the gap where a receipt could otherwise declare
#: an arbitrarily long-lived approval window.
MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS = 10 * 60


class AuthorizationDenied(Exception):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass
class AuthorizationContext:
    """The only two authorization channels ``confirm_human_authorization``
    accepts (Issue #2238 Outcome #7). There is no boolean/flag field on this
    dataclass that means "authorized" -- a bare ``--authorized-by-human``
    style flag structurally cannot exist here."""

    receipt_path: Path | None = None
    tty_confirm: Callable[[str], bool] | None = None
    is_tty: Callable[[], bool] = field(default=lambda: sys.stdin.isatty())
    clock: Callable[[], datetime] = field(default=_utcnow)


def _load_authorization_receipt(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthorizationDenied(f"receipt file unreadable: {exc}", reason_code="receipt_unreadable") from exc
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthorizationDenied(f"receipt file is not valid JSON: {exc}", reason_code="receipt_malformed") from exc
    if not isinstance(receipt, dict) or receipt.get("schema_version") != HUMAN_AUTHORIZATION_RECEIPT_SCHEMA:
        raise AuthorizationDenied("receipt missing/incorrect schema_version", reason_code="receipt_schema_mismatch")
    missing = HUMAN_AUTHORIZATION_RECEIPT_REQUIRED_FIELDS - set(receipt.keys())
    if missing:
        raise AuthorizationDenied(
            f"receipt missing required field(s): {sorted(missing)}", reason_code="receipt_incomplete"
        )
    return receipt


def confirm_human_authorization(
    ctx: AuthorizationContext,
    *,
    publication_digest: str,
    repository_id: str,
    target_issue: int,
    request_id: str,
    operation: str = "publish_retrospective_run",
) -> None:
    """AC4: fail-closed. Raises ``AuthorizationDenied`` unless exactly one of
    the two channels actually confirms:

    (a) ``ctx.receipt_path`` points at a well-formed, matching, unexpired
        ``human_authorization_receipt/v1`` file, or
    (b) ``ctx.tty_confirm`` is set AND ``ctx.is_tty()`` is true AND the
        callback itself returns ``True`` for an explicit confirmation
        prompt naming the destination/digest.

    Neither channel present (the default, non-interactive, no-receipt case
    -- e.g. a bare CLI invocation with no additional flag) always denies.
    There is deliberately no parameter here that means "trust me, a human
    already approved this" -- that is exactly the ``--authorized-by-human``
    pattern this Issue's contract forbids.

    Issue #2238 P0-2 fix_delta: receipt validation now additionally checks
    (1) ``approved_at`` is not in the future (2) the receipt's own
    ``(approved_at, expires_at)`` window does not exceed
    ``MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS``, closing the gap where a
    receipt could otherwise declare an unbounded approval window."""
    if ctx.receipt_path is not None:
        receipt = _load_authorization_receipt(ctx.receipt_path)
        if receipt["publication_digest"] != publication_digest:
            raise AuthorizationDenied("receipt publication_digest mismatch", reason_code="receipt_digest_mismatch")
        if receipt["repository_id"] != repository_id:
            raise AuthorizationDenied("receipt repository_id mismatch", reason_code="receipt_repository_mismatch")
        if receipt["target_issue"] != target_issue:
            raise AuthorizationDenied("receipt target_issue mismatch", reason_code="receipt_target_issue_mismatch")
        if receipt["operation"] != operation:
            raise AuthorizationDenied("receipt operation mismatch", reason_code="receipt_operation_mismatch")
        if receipt["request_id"] != request_id:
            raise AuthorizationDenied("receipt request_id mismatch", reason_code="receipt_request_id_mismatch")

        approved_at = _parse_iso8601(receipt["approved_at"])
        expires_at = _parse_iso8601(receipt["expires_at"])
        now = ctx.clock()
        if approved_at > now:
            raise AuthorizationDenied("receipt approved_at is in the future", reason_code="receipt_approved_at_future")
        if (expires_at - approved_at).total_seconds() > MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS:
            raise AuthorizationDenied(
                f"receipt TTL exceeds the maximum allowed {MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS}s",
                reason_code="receipt_ttl_exceeded",
            )
        if now >= expires_at:
            raise AuthorizationDenied("receipt has expired", reason_code="receipt_expired")
        return

    if ctx.tty_confirm is not None and ctx.is_tty():
        prompt = (
            f"Publish agent-retrospective run to {repository_id}#{target_issue}? "
            f"publication_digest={publication_digest}"
        )
        if ctx.tty_confirm(prompt):
            return
        raise AuthorizationDenied("TTY confirmation declined", reason_code="tty_declined")

    raise AuthorizationDenied(
        "no human authorization channel confirmed (neither a valid human_authorization_receipt/v1 "
        "file nor an interactive TTY confirmation was provided) -- a bare flag is never accepted as authorization",
        reason_code="authorization_missing",
    )


def issue_authorization_receipt(
    *,
    publication_digest: str,
    repository_id: str,
    target_issue: int,
    request_id: str,
    operation: str = "publish_retrospective_run",
    ttl_seconds: int = MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS,
    clock: Callable[[], datetime] = _utcnow,
) -> dict[str, Any]:
    """Issue #2238 P0-2 fix_delta: builds a ``human_authorization_receipt/v1``
    dict bound to an ALREADY-KNOWN (frozen by ``prepare_publication()``)
    ``publication_digest`` -- this is what makes the non-interactive receipt
    flow actually achievable: the CLI ``authorize`` subcommand calls this
    against the digest ``prepare-publication`` already computed and wrote to
    disk, not a digest that can only be known once a live POST is already in
    flight. ``ttl_seconds`` MUST NOT exceed
    ``MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS`` (``confirm_human_authorization``
    would reject it anyway; this function fails closed earlier)."""
    if ttl_seconds > MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS or ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be in (0, {MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS}], got {ttl_seconds}")
    now = clock()
    expires_at = now.timestamp() + ttl_seconds
    return {
        "schema_version": HUMAN_AUTHORIZATION_RECEIPT_SCHEMA,
        "request_id": request_id,
        "publication_digest": publication_digest,
        "repository_id": repository_id,
        "target_issue": target_issue,
        "operation": operation,
        "approved_at": _iso(now),
        "expires_at": _iso(datetime.fromtimestamp(expires_at, tz=timezone.utc)),
    }


# ---------------------------------------------------------------------------
# public-safety validator (AC12) -- allowlist model, two layers (ADR 0007
# Decision 6): field allowlist + value-level pattern rejection
# ---------------------------------------------------------------------------

PUBLIC_SAFETY_ALLOWED_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "repository_id",
        "target_issue",
        "request_id",
        "scope",
        "idempotency_key",
        "expected_previous_digest",
        "parent_record_digest",
        "run",
        "candidate_records",
        "delta_results",
        "publication_digest",
    }
)

_ABSOLUTE_PATH_RE = re.compile(r"(?:^|[\s\"'=(:])(/home/[^\s\"')]+|/Users/[^\s\"')]+|/root/[^\s\"')]+)")
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bghs_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}\b"),
)


class PublicSafetyViolation(ValueError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _scan_value_patterns(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, sub_value in value.items():
            _scan_value_patterns(sub_value, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, sub_value in enumerate(value):
            _scan_value_patterns(sub_value, f"{path}[{index}]")
        return
    if isinstance(value, str):
        if _ABSOLUTE_PATH_RE.search(value):
            raise PublicSafetyViolation(
                f"absolute local path pattern detected at {path}", reason_code="absolute_path_detected"
            )
        for pattern in _TOKEN_PATTERNS:
            if pattern.search(value):
                raise PublicSafetyViolation(
                    f"credential/token pattern detected at {path}", reason_code="token_pattern_detected"
                )


def run_public_safety_validator(envelope: dict[str, Any]) -> None:
    """AC12: runs immediately before every POST (and, per P0-6, again on
    every readback verification of a comment before it is trusted as prior
    state).

    Layer 1 (field allowlist): top-level keys are restricted to
    ``PUBLIC_SAFETY_ALLOWED_TOP_LEVEL_FIELDS``, and every nesting depth is
    scanned for ``run_retrospective.py``'s ``SMUGGLED_AUTHORITY_KEYS`` (raw
    transcript/credential-bearing field names) via that module's own
    ``_scan_for_smuggled_keys`` -- reused, not reimplemented, so both
    modules reject the same key set.

    Layer 2 (value-level pattern rejection): every string value anywhere in
    the envelope is scanned for absolute local path and credential/token
    patterns.

    A size precheck on the rendered comment body closes out the checks.
    Raises ``PublicSafetyViolation`` (never auto-redacts) on any violation
    -- ``publish_prepared()`` never calls ``transport.create_comment`` after
    this raises."""
    extra = set(envelope.keys()) - PUBLIC_SAFETY_ALLOWED_TOP_LEVEL_FIELDS
    if extra:
        raise PublicSafetyViolation(
            f"disallowed top-level field(s): {sorted(extra)}", reason_code="field_not_allowlisted"
        )

    rr_mod = _run_retrospective_module()
    try:
        rr_mod._scan_for_smuggled_keys(envelope)  # noqa: SLF001 -- intentional cross-module reuse
    except rr_mod.WireContractError as exc:
        raise PublicSafetyViolation(str(exc), reason_code="smuggled_authority_field") from exc

    _scan_value_patterns(envelope)

    body = render_comment_body(envelope)
    if len(body.encode("utf-8")) > MAX_COMMENT_BODY_BYTES:
        raise PublicSafetyViolation(
            f"comment body ({len(body.encode('utf-8'))} bytes) exceeds the {MAX_COMMENT_BODY_BYTES}-byte size guard",
            reason_code="oversize",
        )


# ---------------------------------------------------------------------------
# ambiguous POST failure recovery (AC10, P1-2 rescan-after-every-attempt)
# ---------------------------------------------------------------------------


class PublicationConflict(Exception):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def create_comment_with_recovery(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    body: str,
    request_id: str,
    idempotency_key: str,
    publication_digest: str,
    trusted_publisher_logins: frozenset[str] | set[str],
    max_retries: int = 2,
) -> tuple[dict[str, Any], bool]:
    """AC10/P1-2: on ``AmbiguousTransportError`` (e.g. a POST timeout), never
    blindly retry. After EVERY ambiguous outcome (not just the first, P1-2
    fix_delta), search by ``request_id`` (falling back to the idempotency
    key) for a comment the ambiguous POST may already have created
    server-side:

    - found with the same ``publication_digest`` -> recovered (return it,
      ``recovered=True``, no further POST issued)
    - found with a different ``publication_digest`` -> ``PublicationConflict``
    - not found -> bounded retry (``max_retries`` total attempts beyond the
      first), rescanning again after each subsequent ambiguous outcome, then
      re-raise once the retry budget is spent.

    Returns ``(comment, recovered)``."""
    attempts = 0
    last_exc: Exception | None = None
    while True:
        try:
            return transport.create_comment(repo=repo, issue_number=issue_number, body=body), False
        except AmbiguousTransportError as exc:
            attempts += 1
            last_exc = exc

        existing = find_by_request_id(
            transport, repo, issue_number, request_id, trusted_publisher_logins=trusted_publisher_logins
        )
        if existing is None:
            existing = find_by_idempotency_key(
                transport, repo, issue_number, idempotency_key, trusted_publisher_logins=trusted_publisher_logins
            )
        if existing is not None:
            comment, envelope = existing
            if envelope.get("publication_digest") == publication_digest:
                return comment, True
            raise PublicationConflict(
                "recovered comment (matched by request_id/idempotency_key) has a different publication_digest "
                "than the pending write",
                reason_code="ambiguous_post_recovered_conflict",
            )

        if attempts > max_retries:
            raise PublicationConflict(
                f"ambiguous POST failure not recoverable after {attempts} attempts: {last_exc}",
                reason_code="ambiguous_post_retry_exhausted",
            )


# ---------------------------------------------------------------------------
# IssueCommentPreviousStateProvider (AC1, AC9, P0-4 fork detection,
# P1-4 opt-in staleness) -- the real, persistence-backed
# PreviousStateProviderProtocol implementation
# ---------------------------------------------------------------------------


def _looks_like_legacy_retrospective_comment(comment: dict[str, Any]) -> bool:
    """Heuristic for "this looks like a pre-#2238 retrospective-adjacent
    comment that this module cannot parse as one of its own envelopes" --
    used only to distinguish ``no_history`` (nothing at all found) from
    ``legacy_unavailable`` (something was found but is not a persistable
    canonical record) in ``IssueCommentPreviousStateProvider.get()``."""
    body = comment.get("body") or ""
    if not body:
        return False
    first_line = body.splitlines()[0].strip()
    return "agent_retrospective_run" in first_line and not _MARKER_LINE_RE.match(first_line)


class IssueCommentPreviousStateProvider:
    """AC1/AC9/P0-4/P1-4: persistence-backed ``PreviousStateProviderProtocol``
    implementation. Reads run-publication comments on ``target_issue``
    matching ``(repository_id, scope)`` and classifies the result into one
    of the 5 ``PREVIOUS_STATE_STATUSES`` from the *actual shape of the data
    read* -- never from a caller-supplied fixture:

    - no matching envelope found at all, but a legacy (unparseable)
      retrospective-shaped comment exists -> ``legacy_unavailable``
    - no matching envelope found and nothing legacy either -> ``no_history``
    - Issue #2238 P0-4 fix_delta: read-time fork detection over the
      ``(repository_id, scope, parent_record_digest)``-keyed chain of ALL
      matching verified records -- if any ``parent_record_digest`` has 2+
      children, or the chain's single leaf does not equal the most-recent
      (highest comment id) record, the read is classified ``stale``. This
      is recomputed fresh on every ``get()`` call, not derived from a
      separately-persisted conflict flag (so it can never go stale itself).
    - matching envelope found, but its own ``source_observations`` recorded
      ``pagination_completeness: partial`` at publish time -> ``partial``
    - Issue #2238 P1-4 fix_delta: matching envelope found, not partial, not
      forked, but older than ``stale_after_seconds`` (constructor
      parameter, default ``None`` == disabled) -> ``stale``. Previously a
      blanket 7-day rule with no real justification for low-frequency solo
      development; now opt-in.
    - otherwise -> ``available``

    ``read_version`` is the matched envelope's ``publication_digest`` --
    this is the value ``run_retrospective.py``'s ``execute_run()``/
    ``run_cli()`` now propagate into the next run's
    ``PUBLISH_REQUEST_V1.expected_previous_digest`` (AC2)."""

    def __init__(
        self,
        *,
        repo: str,
        target_issue: int,
        transport: "IssueCommentTransportProtocol",
        trusted_publisher_logins: frozenset[str] | set[str] = frozenset(),
        clock: Callable[[], datetime] = _utcnow,
        stale_after_seconds: int | None = None,
    ) -> None:
        self._repo = repo
        self._target_issue = target_issue
        self._transport = transport
        self._trusted_publisher_logins = frozenset(trusted_publisher_logins)
        self._clock = clock
        self._stale_after_seconds = stale_after_seconds

    def get(self, *, repository_id: str, scope: str, finding_identity_algorithm: str) -> Any:
        del finding_identity_algorithm  # part of the port signature; stored records are trusted as-is
        rr_mod = _run_retrospective_module()

        all_records = _iter_run_records(
            self._transport, self._repo, self._target_issue, trusted_publisher_logins=self._trusted_publisher_logins
        )
        matching = [
            item
            for item in all_records
            if item[1].get("repository_id") == repository_id and item[1].get("scope") == scope
        ]
        if not matching:
            legacy_present = any(
                _looks_like_legacy_retrospective_comment(comment)
                for comment in self._transport.list_comments(repo=self._repo, issue_number=self._target_issue)
            )
            if legacy_present:
                return rr_mod.PreviousStateResult(
                    status="legacy_unavailable", previous_run_ref=None, candidates=[], read_version=None
                )
            return rr_mod.PreviousStateResult(
                status="no_history", previous_run_ref=None, candidates=[], read_version=None
            )

        matching_sorted = sorted(matching, key=lambda item: item[0].get("id", 0))
        comment, envelope = matching_sorted[-1]
        candidates = envelope.get("candidate_records", [])
        read_version = envelope.get("publication_digest")
        previous_run_ref = comment.get("html_url")

        # Issue #2238 P0-4 fix_delta: read-time fork/chain reconstruction
        # over ALL matching records, keyed on parent_record_digest.
        children_by_parent: dict[str | None, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for item in matching_sorted:
            parent_digest = item[1].get("parent_record_digest")
            children_by_parent.setdefault(parent_digest, []).append(item)
        forked = any(len(children) >= 2 for children in children_by_parent.values())
        parent_digests_referenced = set(children_by_parent.keys())
        leaves = [
            item for item in matching_sorted if item[1].get("publication_digest") not in parent_digests_referenced
        ]
        chain_diverged = (
            forked or len(leaves) != 1 or leaves[0][1].get("publication_digest") != envelope.get("publication_digest")
        )
        if chain_diverged:
            return rr_mod.PreviousStateResult(
                status="stale", previous_run_ref=previous_run_ref, candidates=candidates, read_version=read_version
            )

        source_observations = envelope.get("run", {}).get("source_observations", [])
        if any(obs.get("pagination_completeness") == "partial" for obs in source_observations):
            return rr_mod.PreviousStateResult(
                status="partial",
                previous_run_ref=previous_run_ref,
                candidates=candidates,
                read_version=read_version,
            )

        if self._stale_after_seconds is not None:
            generated_at = envelope.get("run", {}).get("run_identity", {}).get("generated_at")
            if generated_at:
                try:
                    generated_dt = _parse_iso8601(generated_at)
                except ValueError:
                    generated_dt = None
                if generated_dt is not None:
                    age_seconds = (self._clock() - generated_dt).total_seconds()
                    if age_seconds > self._stale_after_seconds:
                        return rr_mod.PreviousStateResult(
                            status="stale",
                            previous_run_ref=previous_run_ref,
                            candidates=candidates,
                            read_version=read_version,
                        )

        return rr_mod.PreviousStateResult(
            status="available",
            previous_run_ref=previous_run_ref,
            candidates=candidates,
            read_version=read_version,
        )


# ---------------------------------------------------------------------------
# P0-1: repository_id <-> --repo cross-check (fail closed, zero transport
# calls on mismatch)
# ---------------------------------------------------------------------------


class RepositoryMismatch(Exception):
    """Issue #2238 P0-1: raised when the approved ``publish_request
    ["repository_id"]`` does not exactly match the actual POST destination
    (``repo``, the ``--repo``/``GhCliIssueCommentTransport`` target). Raised
    BEFORE any transport call is made -- this is deliberately the very first
    thing ``prepare_publication()``/``publish_run()`` do."""

    reason_code = "repository_mismatch"


def _check_repository_match(*, declared_repository_id: str, repo: str) -> None:
    if declared_repository_id != repo:
        raise RepositoryMismatch(
            f"publish_request.repository_id={declared_repository_id!r} does not match "
            f"the POST destination --repo={repo!r} -- refusing before any transport call"
        )


#: Issue #2238 P0-6: env var an operator can set to override the default
#: trusted-publisher allowlist resolution (comma-separated GitHub logins).
#: Kept as a tiny, explicit, auditable knob rather than an implicit "trust
#: the currently `gh auth`-logged-in user" default that could silently
#: widen/narrow across environments.
_TRUSTED_PUBLISHER_LOGINS_ENV = "AGENT_RETROSPECTIVE_TRUSTED_PUBLISHER_LOGINS"


def resolve_trusted_publisher_logins(
    *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
) -> frozenset[str]:
    """Issue #2238 P0-6: resolves the allowlist of GitHub logins this module
    trusts as a valid publisher identity for durable-state comments.
    Resolution order: (1) ``AGENT_RETROSPECTIVE_TRUSTED_PUBLISHER_LOGINS``
    env var (comma-separated) if set, else (2) the currently ``gh
    auth``-authenticated login (``gh api user --jq .login``) as the sole
    trusted identity. Returns an empty ``frozenset`` (fail closed -- nothing
    is trusted) if neither resolves."""
    import os

    env_value = os.environ.get(_TRUSTED_PUBLISHER_LOGINS_ENV)
    if env_value:
        return frozenset(login.strip() for login in env_value.split(",") if login.strip())
    try:
        completed = runner(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    if completed.returncode != 0:
        return frozenset()
    login = completed.stdout.strip()
    return frozenset({login}) if login else frozenset()


# ---------------------------------------------------------------------------
# top-level orchestration (P0-2 two-stage flow)
# ---------------------------------------------------------------------------


@dataclass
class PublicationResult:
    status: str  # published | no_op | recovered | conflict | published_unverified | published_index_stale
    reason_code: str | None
    comment_url: str | None
    comment_id: int | None
    publication_digest: str | None
    idempotency_key: str | None
    conflict_detected: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass
class PreparedPublication:
    """Issue #2238 P0-2 fix_delta: the frozen output of
    ``prepare_publication()``. ``status`` is ``"publish"`` (needs
    authorization + POST via ``publish_prepared()``), or one of the terminal
    idempotency outcomes (``"no_op"``/``"conflict"``) that were already
    resolved during ``prepare_publication()`` itself -- in either terminal
    case ``envelope`` is ``None`` and no authorization/POST is ever
    required. ``parent_record_digest_at_prepare`` is the live head digest
    read at prepare time -- ``publish_prepared()`` re-reads the live head
    immediately before POST and refuses if it has since diverged from this
    snapshot (never POSTs a stale envelope)."""

    status: str  # publish | no_op | conflict
    envelope: dict[str, Any] | None
    parent_record_digest_at_prepare: str | None
    request_payload_digest: str
    idempotency_key: str
    repo: str
    prepared_at: str
    existing: tuple[dict[str, Any], dict[str, Any]] | None = None

    def to_file_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "agent_retrospective_prepared_publication/v1",
            "status": self.status,
            "envelope": self.envelope,
            "parent_record_digest_at_prepare": self.parent_record_digest_at_prepare,
            "request_payload_digest": self.request_payload_digest,
            "idempotency_key": self.idempotency_key,
            "repo": self.repo,
            "prepared_at": self.prepared_at,
        }

    @classmethod
    def from_file_dict(cls, payload: dict[str, Any]) -> "PreparedPublication":
        return cls(
            status=payload["status"],
            envelope=payload.get("envelope"),
            parent_record_digest_at_prepare=payload.get("parent_record_digest_at_prepare"),
            request_payload_digest=payload["request_payload_digest"],
            idempotency_key=payload["idempotency_key"],
            repo=payload["repo"],
            prepared_at=payload["prepared_at"],
        )


def _terminal_result_from_existing(
    status: str, existing: tuple[dict[str, Any], dict[str, Any]] | None, *, idempotency_key: str
) -> PublicationResult:
    if existing is not None:
        comment, envelope = existing
        return PublicationResult(
            status=status,
            reason_code=None if status == "no_op" else "idempotency_key_digest_conflict",
            comment_url=comment.get("html_url"),
            comment_id=comment.get("id"),
            publication_digest=envelope.get("publication_digest"),
            idempotency_key=idempotency_key,
        )
    return PublicationResult(
        status=status,
        reason_code=None if status == "no_op" else "idempotency_key_digest_conflict",
        comment_url=None,
        comment_id=None,
        publication_digest=None,
        idempotency_key=idempotency_key,
    )


def prepare_publication(
    *,
    publish_request: dict[str, Any],
    repo: str,
    transport: "IssueCommentTransportProtocol",
    trusted_publisher_logins: frozenset[str] | set[str],
    scope: str = DEFAULT_SCOPE,
    generated_at: str | None = None,
) -> PreparedPublication:
    """Issue #2238 P0-2/P0-1/P0-3 fix_delta: stage 1 of the two-stage flow.

    1. P0-1: cross-check ``publish_request["repository_id"]`` against
       ``repo`` -- ZERO transport calls if they mismatch.
    2. P0-3: evaluate idempotency FIRST, against the stable
       ``request_payload_digest`` (no OCC/head read needed for this step at
       all -- ``compute_idempotency_key``/``compute_request_payload_digest``
       are pure functions of the request content). If the outcome is
       already ``no_op``/``conflict``, return that terminal
       ``PreparedPublication`` immediately -- no envelope is built, no
       authorization is ever required for either terminal outcome.
    3. Only for the ``publish`` outcome: run the OCC precheck (one
       transport read) to obtain ``parent_record_digest``, build the full
       envelope (P0-5's real ``source_observations``), and run the
       public-safety validator early (so an unsafe payload never reaches a
       human for approval).

    The returned ``PreparedPublication`` is what the CLI ``prepare-
    publication`` subcommand freezes to a file -- its ``publication_digest``
    is now fully and deterministically known, which is what makes the
    non-interactive ``authorize`` step (P0-2) actually achievable."""
    declared_repository_id = publish_request["repository_id"]
    _check_repository_match(declared_repository_id=declared_repository_id, repo=repo)

    run_identity = publish_request["run_identity"]
    target_issue = publish_request["target_issue"]
    request_id = publish_request["request_id"]
    source_observations = publish_request.get("source_observations") or run_identity.get("source_observations") or []
    candidate_records = publish_request.get("candidate_records", [])
    delta_results = publish_request.get("delta_results", [])

    idempotency_key = compute_idempotency_key(
        repository_id=declared_repository_id,
        base_sha=run_identity["base_sha"],
        source_set_digest=run_identity["source_set_digest"],
        scope=scope,
    )
    request_payload_digest = compute_request_payload_digest(
        repository_id=declared_repository_id,
        target_issue=target_issue,
        scope=scope,
        run_identity=run_identity,
        candidate_records=candidate_records,
        delta_results=delta_results,
        source_observations=source_observations,
    )

    decision, existing = evaluate_idempotency(
        transport,
        repo,
        target_issue,
        idempotency_key=idempotency_key,
        request_payload_digest=request_payload_digest,
        trusted_publisher_logins=trusted_publisher_logins,
    )
    prepared_at = _iso(_utcnow())
    if decision in ("no_op", "conflict"):
        return PreparedPublication(
            status=decision,
            envelope=None,
            parent_record_digest_at_prepare=None,
            request_payload_digest=request_payload_digest,
            idempotency_key=idempotency_key,
            repo=repo,
            prepared_at=prepared_at,
            existing=existing,
        )

    parent_record_digest = check_optimistic_concurrency_precondition(
        transport,
        repo,
        target_issue,
        repository_id=declared_repository_id,
        scope=scope,
        expected_previous_digest=publish_request.get("expected_previous_digest"),
        trusted_publisher_logins=trusted_publisher_logins,
    )

    generated_at_value = generated_at or publish_request.get("generated_at") or run_identity.get("generated_at")
    envelope = build_run_envelope(
        repository_id=declared_repository_id,
        target_issue=target_issue,
        request_id=request_id,
        run_identity=run_identity,
        candidate_records=candidate_records,
        delta_results=delta_results,
        expected_previous_digest=publish_request.get("expected_previous_digest"),
        parent_record_digest=parent_record_digest,
        generated_at=generated_at_value,
        scope=scope,
        runtime_version=publish_request.get("runtime_version") or run_identity.get("runtime_version"),
        source_observations=source_observations,
    )
    run_public_safety_validator(envelope)

    return PreparedPublication(
        status="publish",
        envelope=envelope,
        parent_record_digest_at_prepare=parent_record_digest,
        request_payload_digest=request_payload_digest,
        idempotency_key=idempotency_key,
        repo=repo,
        prepared_at=prepared_at,
    )


class PreparedEnvelopeStale(Exception):
    """Issue #2238 P0-2 fix_delta: raised by ``publish_prepared()`` when the
    live head has changed since ``prepare_publication()`` froze
    ``parent_record_digest_at_prepare``. The caller MUST re-run
    ``prepare_publication()`` (a fresh envelope bound to the new head) --
    this exception is never silently retried with the stale envelope."""

    reason_code = "prepared_envelope_stale"


def publish_prepared(
    prepared: PreparedPublication,
    *,
    repo: str,
    transport: "IssueCommentTransportProtocol",
    auth_ctx: AuthorizationContext,
    trusted_publisher_logins: frozenset[str] | set[str],
    index_updater: Callable[..., None] | None = None,
) -> PublicationResult:
    """Issue #2238 P0-2 fix_delta: stage 2 of the two-stage flow --
    authorization + POST + readback + index update, driven entirely by an
    already-``prepare_publication()``-frozen envelope (never rebuilds one).

    1. P0-2: re-verify the live head against
       ``prepared.parent_record_digest_at_prepare`` -- refuses (raises
       ``StaleWriteDetected``, via the same
       ``check_optimistic_concurrency_precondition`` P0-3 strict-None logic)
       if it has moved since ``prepare_publication()``.
    2. Defensive re-check of idempotency (a race between prepare and
       publish could itself have landed a matching record) -- if so, return
       the terminal outcome without POSTing again.
    3. AC4 human authorization gate.
    4. AC10/P1-2 POST with ambiguous-failure recovery.
    5. P0-6 readback verification -- on failure, STOP with
       ``published_unverified`` and do NOT invoke ``index_updater`` (P0-7).
    6. AC6 sibling-conflict rescan (informational; P0-4's actual durable
       consequence lives in the provider).
    7. P0-7: optional index update, called with the VERIFIED
       ``publication_digest`` -- failure here is reported as
       ``published_index_stale``, never a rollback of the primary record
       (AC11)."""
    if prepared.status != "publish":
        raise ValueError(f"publish_prepared() requires status='publish', got {prepared.status!r}")
    envelope = prepared.envelope
    assert envelope is not None  # narrows type for the checks below

    _check_repository_match(declared_repository_id=envelope["repository_id"], repo=repo)

    repository_id = envelope["repository_id"]
    target_issue = envelope["target_issue"]
    scope = envelope["scope"]
    publication_digest = envelope["publication_digest"]
    idempotency_key = envelope["idempotency_key"]
    request_id = envelope["request_id"]

    # P0-2: refuse to POST if the live head has moved since prepare-time.
    check_optimistic_concurrency_precondition(
        transport,
        repo,
        target_issue,
        repository_id=repository_id,
        scope=scope,
        expected_previous_digest=prepared.parent_record_digest_at_prepare,
        trusted_publisher_logins=trusted_publisher_logins,
    )

    decision, existing = evaluate_idempotency(
        transport,
        repo,
        target_issue,
        idempotency_key=idempotency_key,
        request_payload_digest=prepared.request_payload_digest,
        trusted_publisher_logins=trusted_publisher_logins,
    )
    if decision in ("no_op", "conflict"):
        return _terminal_result_from_existing(decision, existing, idempotency_key=idempotency_key)

    confirm_human_authorization(
        auth_ctx,
        publication_digest=publication_digest,
        repository_id=repository_id,
        target_issue=target_issue,
        request_id=request_id,
    )

    body = render_comment_body(envelope)
    comment, recovered = create_comment_with_recovery(
        transport,
        repo,
        target_issue,
        body=body,
        request_id=request_id,
        idempotency_key=idempotency_key,
        publication_digest=publication_digest,
        trusted_publisher_logins=trusted_publisher_logins,
    )

    try:
        verify_readback_digest(
            transport,
            repo,
            comment_id=comment["id"],
            expected_publication_digest=publication_digest,
            trusted_publisher_logins=trusted_publisher_logins,
        )
    except ReadbackVerificationFailed as exc:
        # P0-6: stop here. Do NOT delete/rollback the already-posted
        # comment, do NOT silently retry, and do NOT invoke index_updater.
        return PublicationResult(
            status="published_unverified",
            reason_code=exc.reason_code,
            comment_url=comment.get("html_url"),
            comment_id=comment.get("id"),
            publication_digest=publication_digest,
            idempotency_key=idempotency_key,
            errors=[str(exc)],
        )

    conflict_detected = detect_post_write_sibling_conflict(
        transport,
        repo,
        target_issue,
        parent_record_digest=prepared.parent_record_digest_at_prepare,
        own_comment_id=comment.get("id"),
        trusted_publisher_logins=trusted_publisher_logins,
    )

    result = PublicationResult(
        status="recovered" if recovered else "published",
        reason_code=None,
        comment_url=comment.get("html_url"),
        comment_id=comment.get("id"),
        publication_digest=publication_digest,
        idempotency_key=idempotency_key,
        conflict_detected=conflict_detected,
        errors=[],
    )

    if index_updater is not None:
        try:
            index_updater(publication_digest=publication_digest)
        except Exception as exc:  # noqa: BLE001 -- AC11/P0-7: index-update failure is non-fatal to the primary record
            return dataclasses.replace(result, status="published_index_stale", errors=[*result.errors, str(exc)])

    return result


def publish_run(
    *,
    publish_request: dict[str, Any],
    repo: str,
    transport: "IssueCommentTransportProtocol",
    auth_ctx: AuthorizationContext,
    generated_at: str | None = None,
    scope: str = DEFAULT_SCOPE,
    index_updater: Callable[..., None] | None = None,
    trusted_publisher_logins: frozenset[str] | set[str] = frozenset(),
) -> PublicationResult:
    """Convenience single-call wrapper composing ``prepare_publication()``
    then (if not already terminal) ``publish_prepared()`` -- used by most
    tests and by any in-process caller that doesn't need the CLI's explicit
    two-stage split. The CLI's ``prepare-publication``/``authorize``/
    ``publish`` subcommands call the two stage functions directly (P0-2)."""
    prepared = prepare_publication(
        publish_request=publish_request,
        repo=repo,
        transport=transport,
        trusted_publisher_logins=trusted_publisher_logins,
        scope=scope,
        generated_at=generated_at,
    )
    if prepared.status in ("no_op", "conflict"):
        return _terminal_result_from_existing(
            prepared.status, prepared.existing, idempotency_key=prepared.idempotency_key
        )
    return publish_prepared(
        prepared,
        repo=repo,
        transport=transport,
        auth_ctx=auth_ctx,
        trusted_publisher_logins=trusted_publisher_logins,
        index_updater=index_updater,
    )


# ---------------------------------------------------------------------------
# CLI entrypoint (P0-2 two-stage subcommands + P0-7 index wiring)
# ---------------------------------------------------------------------------


def _prompt_tty_confirmation(prompt: str) -> bool:  # pragma: no cover - interactive path only
    answer = input(f"{prompt}\nConfirm publish? [y/N]: ")
    return answer.strip().lower() in ("y", "yes")


def _print_failure(reason_code: str, reason: str) -> None:
    print(json.dumps({"status": "failed", "reason_code": reason_code, "reason": reason}, sort_keys=True))


def _build_index_updater(*, repo: str, index_parent_issue: int) -> Callable[..., None]:
    """Issue #2238 P0-7 fix_delta: production ``index_updater`` -- shells
    out to ``scripts/agent-logs/update-retro-index.mjs`` (the existing
    updater, previously only exercised via test injection). The index
    entry's ``run_digest`` is derived by ``retro-index-builder.mjs`` from
    the comment's OWN ``publication_digest`` field directly (see that
    file's ``extractRetrospectiveRunPayload``/``normalizeRetrospectiveRunComment``
    fix, also part of this fix_delta) -- never a separately-computed digest
    of pretty-printed JSON."""
    repo_root = _SCRIPTS_DIR.parents[3]
    updater_script = repo_root / "scripts" / "agent-logs" / "update-retro-index.mjs"

    def _run(*, publication_digest: str | None = None) -> None:
        del publication_digest  # the index rebuild itself re-derives run_digest from each comment
        completed = subprocess.run(
            [
                "node",
                str(updater_script),
                "--repo",
                repo,
                "--parent-issue",
                str(index_parent_issue),
                "--dry-run",
                "false",
                "--confirm-live",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"update-retro-index.mjs failed (exit {completed.returncode}): {completed.stderr}")

    return _run


def _cmd_prepare_publication(args: argparse.Namespace) -> int:
    publish_request = json.loads(Path(args.publish_request_file).read_text(encoding="utf-8"))
    transport = GhCliIssueCommentTransport()
    trusted = resolve_trusted_publisher_logins()
    try:
        prepared = prepare_publication(
            publish_request=publish_request,
            repo=args.repo,
            transport=transport,
            trusted_publisher_logins=trusted,
            scope=args.scope,
        )
    except (RepositoryMismatch, StaleWriteDetected, PublicSafetyViolation) as exc:
        _print_failure(getattr(exc, "reason_code", "error"), str(exc))
        return 1
    Path(args.output_file).write_text(json.dumps(prepared.to_file_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": prepared.status, "output_file": args.output_file}, sort_keys=True))
    return 0


def _cmd_authorize(args: argparse.Namespace) -> int:
    prepared = PreparedPublication.from_file_dict(json.loads(Path(args.prepared_file).read_text(encoding="utf-8")))
    if prepared.status != "publish":
        _print_failure("nothing_to_authorize", f"prepared publication status is {prepared.status!r}, not 'publish'")
        return 1
    envelope = prepared.envelope
    assert envelope is not None
    if args.confirm_tty:
        if not sys.stdin.isatty():
            _print_failure("authorization_missing", "--confirm-tty requested but stdin is not a TTY")
            return 1
        print(json.dumps(envelope, indent=2, sort_keys=True))
        answer = input(
            f"Above is the frozen publication envelope (publication_digest={envelope['publication_digest']}). "
            "Confirm publish? [y/N]: "
        )
        if answer.strip().lower() not in ("y", "yes"):
            _print_failure("tty_declined", "TTY confirmation declined")
            return 1
    receipt = issue_authorization_receipt(
        publication_digest=envelope["publication_digest"],
        repository_id=envelope["repository_id"],
        target_issue=envelope["target_issue"],
        request_id=envelope["request_id"],
        ttl_seconds=args.ttl_seconds,
    )
    Path(args.receipt_output_file).write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": "authorized", "receipt_output_file": args.receipt_output_file}, sort_keys=True))
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    prepared = PreparedPublication.from_file_dict(json.loads(Path(args.prepared_file).read_text(encoding="utf-8")))
    transport = GhCliIssueCommentTransport()
    trusted = resolve_trusted_publisher_logins()
    auth_ctx = AuthorizationContext(receipt_path=Path(args.receipt_file) if args.receipt_file else None)
    index_updater = (
        _build_index_updater(repo=args.repo, index_parent_issue=args.index_parent_issue)
        if args.index_parent_issue
        else None
    )
    if prepared.status in ("no_op", "conflict"):
        result = _terminal_result_from_existing(
            prepared.status, prepared.existing, idempotency_key=prepared.idempotency_key
        )
    else:
        try:
            result = publish_prepared(
                prepared,
                repo=args.repo,
                transport=transport,
                auth_ctx=auth_ctx,
                trusted_publisher_logins=trusted,
                index_updater=index_updater,
            )
        except (AuthorizationDenied, PublicSafetyViolation, PublicationConflict, StaleWriteDetected) as exc:
            reason_code = getattr(exc, "reason_code", "error")
            if isinstance(exc, StaleWriteDetected):
                _print_failure(
                    PreparedEnvelopeStale.reason_code,
                    f"live head changed since prepare-publication -- re-run prepare-publication: {exc}",
                )
            else:
                _print_failure(reason_code, str(exc))
            return 1

    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status in ("published", "no_op", "recovered", "published_index_stale") else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="persist_retrospective_run.py",
        description=(
            "Issue #2238: persist a run_retrospective.py PUBLISH_REQUEST_V1 as a "
            "tool-managed append-only agent-retrospective run Issue comment. "
            "P0-2 fix_delta: split into an explicit two-stage "
            "prepare-publication / authorize / publish flow."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    prepare_parser = subparsers.add_parser(
        "prepare-publication", help="Stage 1: freeze the envelope + publication_digest to a file."
    )
    prepare_parser.add_argument("--publish-request-file", required=True)
    prepare_parser.add_argument("--repo", required=True)
    prepare_parser.add_argument("--scope", default=DEFAULT_SCOPE)
    prepare_parser.add_argument("--output-file", required=True)
    prepare_parser.set_defaults(func=_cmd_prepare_publication)

    authorize_parser = subparsers.add_parser(
        "authorize", help="Stage 2a: issue a human_authorization_receipt/v1 bound to a frozen prepared file."
    )
    authorize_parser.add_argument("--prepared-file", required=True)
    authorize_parser.add_argument("--receipt-output-file", required=True)
    authorize_parser.add_argument("--confirm-tty", action="store_true")
    authorize_parser.add_argument("--ttl-seconds", type=int, default=MAX_AUTHORIZATION_RECEIPT_TTL_SECONDS)
    authorize_parser.set_defaults(func=_cmd_authorize)

    publish_parser = subparsers.add_parser(
        "publish", help="Stage 2b: re-verify the live head, then POST using a frozen prepared file + receipt."
    )
    publish_parser.add_argument("--prepared-file", required=True)
    publish_parser.add_argument("--repo", required=True)
    publish_parser.add_argument("--receipt-file", default=None)
    publish_parser.add_argument("--index-parent-issue", type=int, default=None)
    publish_parser.set_defaults(func=_cmd_publish)

    # Legacy single-shot invocation (no subcommand): preserved for scripts
    # that don't need the two-stage split -- equivalent to publish_run().
    parser.add_argument("--publish-request-file", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--authorization-receipt-file", default=None)
    parser.add_argument("--confirm-tty", action="store_true")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--index-parent-issue", type=int, default=None)
    args = parser.parse_args(argv)

    if getattr(args, "command", None):
        return args.func(args)

    if not args.publish_request_file or not args.repo:
        parser.error(
            "either a subcommand (prepare-publication/authorize/publish), or "
            "--publish-request-file/--repo for the legacy single-shot invocation, is required"
        )

    publish_request = json.loads(Path(args.publish_request_file).read_text(encoding="utf-8"))
    transport = GhCliIssueCommentTransport()
    trusted = resolve_trusted_publisher_logins()
    auth_ctx = AuthorizationContext(
        receipt_path=Path(args.authorization_receipt_file) if args.authorization_receipt_file else None,
        tty_confirm=_prompt_tty_confirmation if args.confirm_tty else None,
    )
    index_updater = (
        _build_index_updater(repo=args.repo, index_parent_issue=args.index_parent_issue)
        if args.index_parent_issue
        else None
    )

    try:
        result = publish_run(
            publish_request=publish_request,
            repo=args.repo,
            transport=transport,
            auth_ctx=auth_ctx,
            scope=args.scope,
            trusted_publisher_logins=trusted,
            index_updater=index_updater,
        )
    except (
        AuthorizationDenied,
        PublicSafetyViolation,
        PublicationConflict,
        StaleWriteDetected,
        RepositoryMismatch,
    ) as exc:
        _print_failure(getattr(exc, "reason_code", "error"), str(exc))
        return 1

    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status in ("published", "no_op", "recovered", "published_index_stale") else 1


if __name__ == "__main__":
    sys.exit(main())
