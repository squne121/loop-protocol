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

Owns:

- ``build_run_envelope()`` / ``compute_publication_digest()``: derive the
  persisted envelope and its ``sha256-jcs-v1`` canonical digest from a
  ``PUBLISH_REQUEST_V1``-shaped dict (AC3). ``publication_digest`` is a
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
- ``evaluate_idempotency()`` / ``compute_idempotency_key()``: duplicate
  suppression keyed on ``(repository_id, base_sha, source_set_digest,
  scope)`` (AC5, ADR 0007 Decision 5). The publisher recomputes the key
  itself -- a caller-supplied ``idempotency_key`` on the input
  ``PUBLISH_REQUEST_V1`` is never trusted as-is.
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
- ``publish_run()``: the top-level orchestration composing all of the above,
  including AC11's ``published_index_stale`` non-rollback behavior for a
  failed derived-index update after a successful primary record.
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
#: schema_version string: the strict ``agent_retrospective_run/v1`` schema
#: (``schemas/agent_retrospective_run_v1.schema.json``) requires a full
#: ``source_observations[]`` acquisition-window record per source, which
#: ``PUBLISH_REQUEST_V1`` (this module's only input) does not carry --
#: enriching ``PUBLISH_REQUEST_V1``/``run_identity`` with that data is
#: Child 2's follow-up (ADR 0007 "Remaining Parent Gaps"), out of this
#: Issue's Allowed Paths. This envelope's ``run.source_observations`` is
#: therefore a minimal, schema-*shaped* single-entry repository-source
#: projection, not a claim of full agent_retrospective_run/v1 conformance.
RUN_PUBLICATION_SCHEMA = "agent_retrospective_run_publication/v1"

RUNTIME_VERSION = "agent-retrospective-persist/v1"

#: matches run_retrospective.DEFAULT_PREVIOUS_STATE_SCOPE's value. Kept as an
#: independent literal (rather than importing run_retrospective eagerly) so
#: this module stays importable standalone; the two are cross-checked by
#: test_persist_retrospective_run.py.
DEFAULT_SCOPE = "repository"

#: sha256-jcs-v1 digest namespace prefix, matching validate_retrospective_schema's
#: FINDING_IDENTITY_V1 convention (`"sha256:" + hexdigest`).
_DIGEST_ALGORITHM = "sha256-jcs-v1"

#: GitHub Issue comment body soft cap this module enforces before POST (well
#: under GitHub's actual ~65536-byte hard limit) -- AC12's size precheck.
MAX_COMMENT_BODY_BYTES = 60_000

#: best-effort staleness threshold for IssueCommentPreviousStateProvider
#: (AC9): a previously-persisted run older than this is classified "stale"
#: rather than "available", even though its source coverage was complete at
#: publish time -- the read itself is what's stale, not the source coverage.
STALE_AFTER_SECONDS = 7 * 24 * 3600


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
# canonical JSON / digest helpers (sha256-jcs-v1)
# ---------------------------------------------------------------------------


def _jcs_canonicalize(value: Any) -> Any:
    """Delegates to validate_retrospective_schema.py's RFC 8785 JCS
    canonicalizer (reused rather than reimplemented -- single source of
    truth for the ``sha256-jcs-v1`` algorithm across Child 2 and this
    module)."""
    return _validate_schema_module()._jcs_canonicalize(value)  # noqa: SLF001 -- intentional cross-module reuse


def _digest_of(payload: dict[str, Any]) -> str:
    """``sha256:`` + SHA256 hex digest of the RFC 8785 JCS canonicalization of
    ``payload`` -- the ``sha256-jcs-v1`` algorithm this module uses for both
    ``publication_digest`` (AC3) and the recomputed idempotency key (AC5)."""
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


# ---------------------------------------------------------------------------
# run-publication envelope: build / render / extract
# ---------------------------------------------------------------------------


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
    generated_at: str,
    scope: str = DEFAULT_SCOPE,
    pagination_completeness: str = "complete",
    runtime_version: str = RUNTIME_VERSION,
) -> dict[str, Any]:
    """AC3/AC8: build the persisted run-publication envelope from a
    ``PUBLISH_REQUEST_V1``-shaped ``run_identity``/``candidate_records``/
    ``delta_results`` triple, computing both the recomputed idempotency key
    and the ``publication_digest``. ``candidate_records``/``delta_results``
    are carried through verbatim (AC8's full-record round-trip -- this
    function never projects down to a private ``candidate_status``-only
    dialect)."""
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
                "generated_at": generated_at,
                "runtime_version": runtime_version,
            },
            "source_observations": [
                {
                    "source_type": "repository",
                    "source_id": "repository",
                    "source_status": "complete",
                    "pagination_completeness": pagination_completeness,
                }
            ],
        },
        "candidate_records": candidate_records,
        "delta_results": delta_results,
    }
    envelope["publication_digest"] = compute_publication_digest(envelope)
    return envelope


_MARKER_LINE_RE = re.compile(r"^<!--\s*agent_retrospective_run:v1\b")
_FENCED_JSON_RE = re.compile(r"```json\r?\n(.*?)\r?\n```", re.DOTALL)


def render_comment_body(envelope: dict[str, Any]) -> str:
    """Render ``envelope`` as the tool-managed append-only Issue comment body
    (AC3's public-safe Issue comment). First line is a machine-parseable
    ownership marker; the payload itself is a fenced ```json block so
    ``extract_envelope_from_body`` can round-trip it exactly."""
    marker = (
        f'<!-- agent_retrospective_run:v1 repository_id={envelope["repository_id"]} '
        f'idempotency_key={envelope["idempotency_key"]} -->'
    )
    fenced = "```json\n" + json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n```"
    return f"{marker}\n\n{fenced}\n"


def extract_envelope_from_body(body: str | None) -> dict[str, Any] | None:
    """Inverse of ``render_comment_body``: returns ``None`` (not an
    exception) for any comment that is not one of this module's own
    run-publication comments -- callers treat that as "ignore this
    comment", not as an error."""
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


def _iter_run_records(
    transport: "IssueCommentTransportProtocol", repo: str, issue_number: int
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    records = []
    for comment in transport.list_comments(repo=repo, issue_number=issue_number):
        envelope = extract_envelope_from_body(comment.get("body"))
        if envelope is not None:
            records.append((comment, envelope))
    return records


def find_by_idempotency_key(
    transport: "IssueCommentTransportProtocol", repo: str, issue_number: int, idempotency_key: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number)
        if item[1].get("idempotency_key") == idempotency_key
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0].get("id", 0))
    return matches[-1]


def find_by_request_id(
    transport: "IssueCommentTransportProtocol", repo: str, issue_number: int, request_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number)
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
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    matches = [
        item
        for item in _iter_run_records(transport, repo, issue_number)
        if item[1].get("repository_id") == repository_id and item[1].get("scope") == scope
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0].get("id", 0))
    return matches[-1]


def find_siblings_by_parent_digest(
    transport: "IssueCommentTransportProtocol", repo: str, issue_number: int, parent_record_digest: str | None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        item
        for item in _iter_run_records(transport, repo, issue_number)
        if item[1].get("parent_record_digest") == parent_record_digest
    ]


# ---------------------------------------------------------------------------
# idempotency guard (AC5) -- duplicate suppression, distinct from AC6
# ---------------------------------------------------------------------------

IDEMPOTENCY_DECISIONS = frozenset({"publish", "no_op", "conflict"})


def evaluate_idempotency(
    transport: "IssueCommentTransportProtocol",
    repo: str,
    issue_number: int,
    *,
    idempotency_key: str,
    publication_digest: str,
) -> tuple[str, tuple[dict[str, Any], dict[str, Any]] | None]:
    """AC5: three-way idempotency decision. ``idempotency_key`` MUST already
    be the publisher-recomputed value (``compute_idempotency_key()``), never
    a caller-supplied one taken on faith."""
    existing = find_by_idempotency_key(transport, repo, issue_number, idempotency_key)
    if existing is None:
        return "publish", None
    _, envelope = existing
    if envelope.get("publication_digest") == publication_digest:
        return "no_op", existing
    return "conflict", existing


# ---------------------------------------------------------------------------
# optimistic concurrency guard (AC6) -- best-effort stale-write detection,
# distinct mechanism from AC5's idempotency guard (ADR 0007 Decision 5)
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
) -> str | None:
    """AC6 step (a): best-effort pre-POST head re-check. GitHub's Issue
    comment API has no atomic compare-and-swap, so this narrows but cannot
    eliminate the race window -- it is explicitly NOT an absolute guarantee
    (ADR 0007 Decision 5). Returns the current head ``publication_digest``
    (``None`` if no prior record exists) so the caller can bind it as the
    new record's ``parent_record_digest`` (step (b))."""
    latest = find_latest_run_record(transport, repo, issue_number, repository_id=repository_id, scope=scope)
    head_digest = latest[1]["publication_digest"] if latest is not None else None
    if expected_previous_digest is not None and expected_previous_digest != head_digest:
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
) -> bool:
    """AC6 steps (c)/(d): post-POST full rescan. If more than one comment
    shares the same ``parent_record_digest`` (two runs both read the same
    prior head and both appended), a conflict is detected -- callers MUST
    then report ``stale`` on the next ``PreviousStateProvider.get()``,
    never silently resolve it themselves (this function only detects; it
    never repairs)."""
    siblings = find_siblings_by_parent_digest(transport, repo, issue_number, parent_record_digest)
    other_ids = {comment.get("id") for comment, _ in siblings if comment.get("id") != own_comment_id}
    return len(other_ids) >= 1


# ---------------------------------------------------------------------------
# post-write readback (AC7)
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
) -> None:
    """AC7: GET the just-created comment by its ``comment_id`` (never trust
    the POST response body alone), extract the fenced JSON envelope,
    recompute its canonical (``sha256-jcs-v1``) digest, and cross-check both
    against the envelope's own stored ``publication_digest`` and against the
    digest computed before POST. Never compares raw Markdown bytes."""
    fetched = transport.get_comment(repo=repo, comment_id=comment_id)
    envelope = extract_envelope_from_body(fetched.get("body"))
    if envelope is None:
        raise ReadbackVerificationFailed(
            "readback comment body did not contain a parseable run-publication envelope",
            reason_code="readback_unparsable",
        )
    preimage = {k: v for k, v in envelope.items() if k != "publication_digest"}
    recomputed = compute_publication_digest(preimage)
    if recomputed != envelope.get("publication_digest"):
        raise ReadbackVerificationFailed(
            "readback envelope's own publication_digest does not match its recomputed canonical digest",
            reason_code="readback_self_digest_mismatch",
        )
    if recomputed != expected_publication_digest:
        raise ReadbackVerificationFailed(
            "readback recomputed digest does not match the digest computed before POST",
            reason_code="readback_expected_digest_mismatch",
        )


# ---------------------------------------------------------------------------
# human authorization gate (AC4) -- fail-closed
# ---------------------------------------------------------------------------

HUMAN_AUTHORIZATION_RECEIPT_SCHEMA = "human_authorization_receipt/v1"
HUMAN_AUTHORIZATION_RECEIPT_REQUIRED_FIELDS = frozenset(
    {"request_id", "publication_digest", "repository_id", "target_issue", "operation", "approved_at", "expires_at"}
)


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
    pattern this Issue's contract forbids."""
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
        expires_at = _parse_iso8601(receipt["expires_at"])
        if ctx.clock() >= expires_at:
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
    """AC12: runs immediately before every POST.

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
    -- ``publish_run()`` never calls ``transport.create_comment`` after this
    raises."""
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
# ambiguous POST failure recovery (AC10)
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
    max_retries: int = 2,
) -> tuple[dict[str, Any], bool]:
    """AC10: on ``AmbiguousTransportError`` (e.g. a POST timeout), never
    blindly retry. First search by ``request_id`` (falling back to the
    idempotency key) for a comment the ambiguous POST may already have
    created server-side:

    - found with the same ``publication_digest`` -> recovered (return it,
      ``recovered=True``, no second POST issued)
    - found with a different ``publication_digest`` -> ``PublicationConflict``
    - not found -> bounded retry (``max_retries``), then re-raise

    Returns ``(comment, recovered)``."""
    try:
        return transport.create_comment(repo=repo, issue_number=issue_number, body=body), False
    except AmbiguousTransportError:
        pass

    existing = find_by_request_id(transport, repo, issue_number, request_id)
    if existing is None:
        existing = find_by_idempotency_key(transport, repo, issue_number, idempotency_key)
    if existing is not None:
        comment, envelope = existing
        if envelope.get("publication_digest") == publication_digest:
            return comment, True
        raise PublicationConflict(
            "recovered comment (matched by request_id/idempotency_key) has a different publication_digest "
            "than the pending write",
            reason_code="ambiguous_post_recovered_conflict",
        )

    last_exc: Exception | None = None
    for _ in range(max_retries):
        try:
            return transport.create_comment(repo=repo, issue_number=issue_number, body=body), False
        except AmbiguousTransportError as exc:
            last_exc = exc
            continue
    raise PublicationConflict(
        f"ambiguous POST failure not recoverable after {max_retries} bounded retries: {last_exc}",
        reason_code="ambiguous_post_retry_exhausted",
    )


# ---------------------------------------------------------------------------
# IssueCommentPreviousStateProvider (AC1, AC9) -- the real,
# persistence-backed PreviousStateProviderProtocol implementation
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
    """AC1/AC9: persistence-backed ``PreviousStateProviderProtocol``
    implementation. Reads the latest run-publication comment on
    ``target_issue`` matching ``(repository_id, scope)`` and classifies it
    into one of the 5 ``PREVIOUS_STATE_STATUSES`` from the *actual shape of
    the data read* -- never from a caller-supplied fixture:

    - no matching envelope found at all, but a legacy (unparseable)
      retrospective-shaped comment exists -> ``legacy_unavailable``
    - no matching envelope found and nothing legacy either -> ``no_history``
    - matching envelope found, but its own ``source_observations`` recorded
      ``pagination_completeness: partial`` at publish time -> ``partial``
    - matching envelope found and not partial, but older than
      ``STALE_AFTER_SECONDS`` -> ``stale``
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
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repo
        self._target_issue = target_issue
        self._transport = transport
        self._clock = clock

    def get(self, *, repository_id: str, scope: str, finding_identity_algorithm: str) -> Any:
        del finding_identity_algorithm  # part of the port signature; stored records are trusted as-is
        rr_mod = _run_retrospective_module()

        matching = [
            item
            for item in _iter_run_records(self._transport, self._repo, self._target_issue)
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

        matching.sort(key=lambda item: item[0].get("id", 0))
        comment, envelope = matching[-1]
        candidates = envelope.get("candidate_records", [])
        read_version = envelope.get("publication_digest")
        previous_run_ref = comment.get("html_url")

        source_observations = envelope.get("run", {}).get("source_observations", [])
        if any(obs.get("pagination_completeness") == "partial" for obs in source_observations):
            return rr_mod.PreviousStateResult(
                status="partial",
                previous_run_ref=previous_run_ref,
                candidates=candidates,
                read_version=read_version,
            )

        generated_at = envelope.get("run", {}).get("run_identity", {}).get("generated_at")
        if generated_at:
            try:
                generated_dt = _parse_iso8601(generated_at)
            except ValueError:
                generated_dt = None
            if generated_dt is not None:
                age_seconds = (self._clock() - generated_dt).total_seconds()
                if age_seconds > STALE_AFTER_SECONDS:
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
# top-level orchestration
# ---------------------------------------------------------------------------


@dataclass
class PublicationResult:
    status: str  # published | no_op | recovered | conflict | published_index_stale
    reason_code: str | None
    comment_url: str | None
    comment_id: int | None
    publication_digest: str | None
    idempotency_key: str | None
    conflict_detected: bool = False
    errors: list[str] = field(default_factory=list)


def publish_run(
    *,
    publish_request: dict[str, Any],
    repo: str,
    transport: "IssueCommentTransportProtocol",
    auth_ctx: AuthorizationContext,
    generated_at: str | None = None,
    scope: str = DEFAULT_SCOPE,
    index_updater: Callable[[], None] | None = None,
) -> PublicationResult:
    """Top-level Issue #2238 orchestration consuming a
    ``PUBLISH_REQUEST_V1``-shaped dict (``run_identity``/
    ``candidate_records``/``delta_results``/``repository_id``/
    ``target_issue``/``request_id``/``expected_previous_digest``):

    1. best-effort optimistic-concurrency precheck (AC6) -> ``parent_record_digest``
    2. build envelope + ``publication_digest`` (AC3/AC8)
    3. public-safety validator (AC12) -- before idempotency/authorization,
       so an unsafe payload never reaches a human for approval either
    4. idempotency guard (AC5) -- ``no_op``/``conflict`` never reach POST
    5. human authorization gate (AC4) -- only for the ``publish`` decision
    6. POST with ambiguous-failure recovery (AC10)
    7. post-write readback (AC7) and sibling-conflict rescan (AC6 step d)
    8. optional derived-index update; failure -> ``published_index_stale``,
       never a rollback of the primary record (AC11)"""
    run_identity = publish_request["run_identity"]
    repository_id = publish_request["repository_id"]
    target_issue = publish_request["target_issue"]
    request_id = publish_request["request_id"]
    generated_at_value = generated_at or _iso(_utcnow())

    parent_record_digest = check_optimistic_concurrency_precondition(
        transport,
        repo,
        target_issue,
        repository_id=repository_id,
        scope=scope,
        expected_previous_digest=publish_request.get("expected_previous_digest"),
    )

    envelope = build_run_envelope(
        repository_id=repository_id,
        target_issue=target_issue,
        request_id=request_id,
        run_identity=run_identity,
        candidate_records=publish_request.get("candidate_records", []),
        delta_results=publish_request.get("delta_results", []),
        expected_previous_digest=publish_request.get("expected_previous_digest"),
        parent_record_digest=parent_record_digest,
        generated_at=generated_at_value,
        scope=scope,
    )
    publication_digest = envelope["publication_digest"]
    idempotency_key = envelope["idempotency_key"]

    run_public_safety_validator(envelope)

    decision, existing = evaluate_idempotency(
        transport, repo, target_issue, idempotency_key=idempotency_key, publication_digest=publication_digest
    )
    if decision == "no_op":
        comment, _ = existing  # type: ignore[misc]
        return PublicationResult(
            status="no_op",
            reason_code=None,
            comment_url=comment.get("html_url"),
            comment_id=comment.get("id"),
            publication_digest=publication_digest,
            idempotency_key=idempotency_key,
        )
    if decision == "conflict":
        return PublicationResult(
            status="conflict",
            reason_code="idempotency_key_digest_conflict",
            comment_url=None,
            comment_id=None,
            publication_digest=publication_digest,
            idempotency_key=idempotency_key,
        )

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
    )

    errors: list[str] = []
    try:
        verify_readback_digest(
            transport, repo, comment_id=comment["id"], expected_publication_digest=publication_digest
        )
    except ReadbackVerificationFailed as exc:
        errors.append(str(exc))

    conflict_detected = detect_post_write_sibling_conflict(
        transport, repo, target_issue, parent_record_digest=parent_record_digest, own_comment_id=comment.get("id")
    )

    result = PublicationResult(
        status="recovered" if recovered else "published",
        reason_code=None,
        comment_url=comment.get("html_url"),
        comment_id=comment.get("id"),
        publication_digest=publication_digest,
        idempotency_key=idempotency_key,
        conflict_detected=conflict_detected,
        errors=errors,
    )

    if index_updater is not None:
        try:
            index_updater()
        except Exception as exc:  # noqa: BLE001 -- AC11: any index-update failure is non-fatal to the primary record
            return dataclasses.replace(result, status="published_index_stale", errors=[*result.errors, str(exc)])

    return result


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _prompt_tty_confirmation(prompt: str) -> bool:  # pragma: no cover - interactive path only
    answer = input(f"{prompt}\nConfirm publish? [y/N]: ")
    return answer.strip().lower() in ("y", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="persist_retrospective_run.py",
        description=(
            "Issue #2238: persist a run_retrospective.py PUBLISH_REQUEST_V1 as a "
            "tool-managed append-only agent-retrospective run Issue comment."
        ),
    )
    parser.add_argument("--publish-request-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--authorization-receipt-file", default=None)
    parser.add_argument("--confirm-tty", action="store_true")
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    args = parser.parse_args(argv)

    publish_request = json.loads(Path(args.publish_request_file).read_text(encoding="utf-8"))
    transport = GhCliIssueCommentTransport()
    auth_ctx = AuthorizationContext(
        receipt_path=Path(args.authorization_receipt_file) if args.authorization_receipt_file else None,
        tty_confirm=_prompt_tty_confirmation if args.confirm_tty else None,
    )

    try:
        result = publish_run(
            publish_request=publish_request,
            repo=args.repo,
            transport=transport,
            auth_ctx=auth_ctx,
            scope=args.scope,
        )
    except (AuthorizationDenied, PublicSafetyViolation, PublicationConflict, StaleWriteDetected) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason_code": getattr(exc, "reason_code", "error"),
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(dataclasses.asdict(result), sort_keys=True))
    return 0 if result.status in ("published", "no_op", "recovered", "published_index_stale") else 1


if __name__ == "__main__":
    sys.exit(main())
