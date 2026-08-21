#!/usr/bin/env python3
"""collect_snapshot.py -- agent-retrospective source adapters (Issue #2236).

Implements 5 independent collector functions -- Claude Code / Claude-GPT /
repository / GitHub / Web -- each returning a `CollectorResult` (dual-channel:
a schema-controlled `observation` matching `agent_retrospective_run/v1`'s
`source_observation`, plus an invocation-private `private_evidence` channel
that is never persisted by this Issue's scope; that is Child 4's job).

Design invariants fixed by Issue #2236 (see the Issue body "固定する設計判断"
section for the full rationale):

- ``source_status`` (`complete|partial|unavailable|blocked`) and a
  source-specific ``reason_code`` are computed independently per adapter
  (AC3). Only *typed operational* failures (timeout, connection error, HTTP
  error response, etc.) are caught and converted into `unavailable`/`blocked`
  results; programmer bugs / contract violations (``KeyError``, schema
  mismatch, assertion failures, ...) are never caught here and propagate to
  the caller as run-level fatal errors (AC6). This module achieves that by
  only ever catching a narrow, explicit set of exception types at each I/O
  boundary (see ``_AdapterOperationalError`` below) -- it never uses a bare
  ``except Exception`` around adapter bodies.
- The repository adapter never re-resolves ``main``; it only ever operates
  on the ``base_sha`` explicitly supplied by the caller (AC7).
- The Claude-GPT adapter treats the run-nonce-keyed hook-event JSONL sink
  (established by PR #2222) as the sole completeness authority. The mere
  presence of a flat main transcript file is never sufficient for
  ``complete`` (AC8).
- ``private_evidence`` (and ``observation``) never contain a raw absolute
  local filesystem path or a credential/Authorization-header-shaped value --
  enforced centrally by ``_scrub`` (AC9), applied to every ``CollectorResult``
  this module constructs.

PR #2269 human REQUEST_CHANGES follow-up fixes (2026-08-20, reviewed at head
9f2f599ef71d8ac4a46d7d89c994980810bcce5a):

- Web adapter: eliminated a DNS-rebinding/TOCTOU gap where the boundary
  check's validated IP was discarded and the production fetch re-resolved
  the (attacker-influenced) hostname a second time. Resolution now happens
  exactly once per fetch attempt (`_resolve_pinned_ip`), and the validated
  IP is threaded through to `fetch`/`default_https_fetch`, which connects
  directly to that IP while presenting the original hostname for TLS SNI /
  the HTTP `Host` header. `default_https_fetch` no longer uses
  `urllib.request`'s opener chain at all, so `http_proxy`/`https_proxy`
  environment variables are never consulted.
- Claude-GPT adapter: `_load_transport_log_module()` now registers the
  dynamically-loaded module in `sys.modules` *before* `exec_module()`
  (standard recipe), which is required on Python 3.12 because
  `transport_log.py` uses `from __future__ import annotations` +
  `@dataclass`, and 3.12's dataclass machinery resolves string annotations
  via `sys.modules[cls.__module__].__dict__`.
- Web adapter: `_resolve_pinned_ip` only catches `socket.gaierror`/`OSError`
  around the injected `resolver` call, so a resolver programmer bug
  (`KeyError`, `AssertionError`, ...) is never misclassified as
  `dns_resolution_failed`/`blocked` -- it propagates as a run-level fatal
  error, matching AC6's general invariant.
- GitHub adapter: 403/429 responses are classified using status code +
  `Retry-After`/`X-RateLimit-Remaining`/`X-RateLimit-Reset` headers + the
  error body, distinguishing `rate_limited` from `permission_denied` instead
  of collapsing every 403/429 into `rate_limited`.
- GitHub adapter: added `default_github_fetch_page`, a production
  `fetch_page` implementation backed by `gh api --include` (pinning the
  GitHub REST API version via `X-GitHub-Api-Version`).
- AC9 scrubbing hardened: absolute-local-path detection now also covers
  `/tmp/`, `/mnt/`, `/var/`, `/workspace/` and searches for embedded
  occurrences (not just a string prefix); `diagnostics` dicts built from
  caught exceptions no longer hold `str(exc)`/raw `stderr` verbatim -- only
  `reason_code`, `exception_class`, `errno`, and other bounded structured
  metadata are kept unconditionally, and any other free-form text is passed
  through a fail-closed allowlist validator (`_safe_diagnostic_text`) before
  being retained at all.
- Claude Code / Claude-GPT adapters: a source with malformed JSONL lines
  alongside valid evidence no longer reports `complete` -- it now reports
  `partial` (`malformed_response`), so the presence of corrupt evidence is
  never silently absorbed into a clean-looking result.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# shared primitives
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: minimum reason_code vocabulary required by the Issue body -- adapters may
#: also use additional, more specific codes not in this set.
REQUIRED_REASON_CODES = frozenset(
    {
        "rate_limited",
        "timeout",
        "auth_ambiguous_404",
        "permission_denied",
        "malformed_response",
        "page_limit_reached",
        "stale_runtime_evidence",
        "source_not_present",
    }
)

SOURCE_STATUSES = frozenset({"complete", "partial", "unavailable", "blocked"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(records: Any) -> str:
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class CollectorResult:
    """Dual-channel result returned by every collector function.

    ``observation`` is public-safe and schema-controlled (must validate
    against `agent_retrospective_run/v1`'s ``source_observation`` def, modulo
    the caller wrapping it into a full run instance -- this module does not
    itself add ``run_identity``). ``private_evidence`` is invocation-private,
    not persisted by this Issue's scope, and consumed only by Child 4's
    evaluator.
    """

    observation: dict[str, Any]
    private_evidence: dict[str, Any] = field(default_factory=dict)


class _AdapterOperationalError(Exception):
    """Internal marker for a *typed operational* failure inside an adapter.

    Adapters raise this only from a narrow ``except`` clause around a
    specific, known-transient exception type (timeout, connection error, OS
    error, non-2xx HTTP, ...) and then catch *only* this type one frame up to
    build an ``unavailable``/``blocked`` ``CollectorResult``. Any other
    exception (``KeyError``, ``AssertionError``, schema mismatch, ...) is
    never wrapped here and propagates untouched (AC6).
    """

    def __init__(
        self,
        message: str,
        *,
        source_status: str,
        reason_code: str,
        exception_class: str | None = None,
        errno: int | None = None,
    ) -> None:
        super().__init__(message)
        assert source_status in ("unavailable", "blocked"), source_status
        self.source_status = source_status
        self.reason_code = reason_code
        self.exception_class = exception_class
        self.errno = errno


# ---------------------------------------------------------------------------
# secret / local-path scrubbing (AC9) -- applied to every CollectorResult
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|credential|api[_-]?key|cookie)", re.IGNORECASE
)
#: matches an absolute local filesystem path either at the start of a string
#: or embedded after a non-identifier character (e.g. inside a longer
#: diagnostic message such as "Permission denied: '/home/user/secret'").
_ABS_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_])(/(?:home|Users|root|tmp|mnt|var|workspace)/\S*|[A-Za-z]:\\\S*)")
#: matches a Bearer/Basic/token-style credential value, or a recognizable
#: GitHub token shape, anywhere in a string (not just at the start).
_BEARER_RE = re.compile(
    r"(?:authorization\s*:\s*)?\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}"
    r"|\bghp_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    re.IGNORECASE,
)
#: fail-closed allowlist for free-form diagnostic text (see
#: `_safe_diagnostic_text`): narrowly-charactered and bounded in length.
_SAFE_DIAGNOSTIC_TEXT_RE = re.compile(r"^[A-Za-z0-9 ,.:;_/=\-]{0,200}$")


def _scrub(value: Any) -> Any:
    """Recursively drop secret-shaped keys and redact absolute local paths /
    Authorization-header-shaped string values (AC9)."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _SECRET_KEY_RE.search(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v) for v in value)
    if isinstance(value, str):
        if _ABS_PATH_RE.search(value):
            return "[redacted-local-path]"
        if _BEARER_RE.search(value):
            return "[redacted-credential]"
        return value
    return value


def _safe_diagnostic_text(value: str | None) -> str | None:
    """Fail-closed allowlist validator for free-form diagnostic text (AC9 /
    PR #2269 P1 fix). `private_evidence.diagnostics` must never hold a raw
    exception message / stderr blob verbatim -- only bounded structured
    metadata (`reason_code`, `exception_class`, `errno`, ...) is kept
    unconditionally. Any other free-form text is dropped (returns `None`)
    unless it is short, narrowly-charactered, and contains no path-like or
    credential-like substring; nothing is ever partially leaked."""
    if not value:
        return None
    if not _SAFE_DIAGNOSTIC_TEXT_RE.match(value):
        return None
    if _ABS_PATH_RE.search(value) or _BEARER_RE.search(value):
        return None
    return value


def _finalize(observation: dict[str, Any], private_evidence: dict[str, Any]) -> CollectorResult:
    return CollectorResult(observation=_scrub(observation), private_evidence=_scrub(private_evidence))


def _build_observation(
    *,
    source_type: str,
    source_id: str,
    source_status: str,
    pagination_completeness: str,
    fetch_started_at: str | None = None,
    fetch_completed_at: str | None = None,
    partial_reason: str | None = None,
    endpoint: str | None = None,
    etag: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    assert source_status in SOURCE_STATUSES, source_status
    observation: dict[str, Any] = {
        "source_type": source_type,
        "source_id": source_id,
        "source_status": source_status,
        "pagination_completeness": pagination_completeness,
    }
    if source_type != "repository":
        observation["fetch_started_at"] = fetch_started_at
        observation["fetch_completed_at"] = fetch_completed_at
    if pagination_completeness == "partial":
        observation["partial_reason"] = partial_reason or "unspecified"
    if endpoint is not None:
        observation["endpoint"] = endpoint
    if etag is not None:
        observation["etag"] = etag
    if cursor is not None:
        observation["cursor"] = cursor
    return observation


def _operational_result(
    *,
    source_type: str,
    source_id: str,
    fetch_started_at: str | None,
    clock: Callable[[], datetime],
    exc: _AdapterOperationalError,
) -> CollectorResult:
    fetch_completed_at = _iso(clock())
    observation = _build_observation(
        source_type=source_type,
        source_id=source_id,
        source_status=exc.source_status,
        pagination_completeness="unknown",
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
    )
    return _finalize(
        observation,
        {
            "normalized_records": [],
            "evidence_digest": _digest([]),
            "provenance": {},
            "diagnostics": {
                "reason_code": exc.reason_code,
                "exception_class": exc.exception_class or type(exc).__name__,
                "errno": exc.errno,
                "safe_detail": _safe_diagnostic_text(str(exc)),
            },
        },
    )


# ---------------------------------------------------------------------------
# Claude Code adapter
# ---------------------------------------------------------------------------

_CLAUDE_CODE_ALLOWED_KEYS = frozenset({"type", "role", "timestamp", "sessionId", "uuid"})


def _redact_claude_code_record(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k in _CLAUDE_CODE_ALLOWED_KEYS}


def collect_claude_code_source(
    session_paths: Sequence[Path],
    *,
    source_id: str = "claude_code",
    clock: Callable[[], datetime] = _utcnow,
) -> CollectorResult:
    """Collect Claude Code session evidence from `session_paths` (JSONL
    transcript files) deterministically.

    ``session_paths`` is caller-supplied (the orchestration layer resolves
    which sessions are in scope for a run); this adapter never globs
    ``~/.claude/projects`` itself, keeping the collection deterministic and
    hermetically testable. Raw absolute file paths are never embedded in the
    returned records (AC9) -- only per-session record counts are kept as
    provenance.

    A source with malformed JSONL lines *alongside* valid evidence is never
    reported as `complete` -- the presence of corrupt lines forces `partial`
    (`malformed_response`) even though usable records exist (PR #2269
    hardening: a prior asymmetry allowed malformed evidence to be silently
    absorbed whenever at least one valid record was present).
    """
    fetch_started_at = _iso(clock())
    normalized: list[dict[str, Any]] = []
    malformed_line_count = 0
    sessions_read = 0
    try:
        for path in session_paths:
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _AdapterOperationalError(
                    f"claude_code_read_failed:{type(exc).__name__}",
                    source_status="unavailable",
                    reason_code="source_not_present",
                    exception_class=type(exc).__name__,
                    errno=getattr(exc, "errno", None),
                ) from exc
            sessions_read += 1
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    malformed_line_count += 1
                    continue
                if isinstance(record, dict):
                    normalized.append(_redact_claude_code_record(record))
    except _AdapterOperationalError as exc:
        return _operational_result(
            source_type="runtime", source_id=source_id, fetch_started_at=fetch_started_at, clock=clock, exc=exc
        )

    fetch_completed_at = _iso(clock())
    if not normalized:
        status, pagination, reason = "unavailable", "unknown", None
    elif malformed_line_count > 0:
        status, pagination, reason = "partial", "partial", "malformed_response"
    else:
        status, pagination, reason = "complete", "complete", None
    observation = _build_observation(
        source_type="runtime",
        source_id=source_id,
        source_status=status,
        pagination_completeness=pagination,
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
        partial_reason=reason if pagination == "partial" else None,
    )
    return _finalize(
        observation,
        {
            "normalized_records": normalized,
            "evidence_digest": _digest(normalized),
            "provenance": {"session_count": len(session_paths), "sessions_read": sessions_read},
            "diagnostics": {
                "malformed_line_count": malformed_line_count,
                "reason_code": reason if reason else (None if normalized else "source_not_present"),
            },
        },
    )


# ---------------------------------------------------------------------------
# Claude-GPT adapter
# ---------------------------------------------------------------------------

_HOOK_SINK_ALLOWED_KEYS = frozenset({"run_nonce", "event", "session_id", "agent_id", "ts", "prompt_digest"})
_HOOK_SINK_ALLOWED_EVENTS = frozenset({"UserPromptSubmit", "Stop", "StopFailure", "SubagentStart", "SubagentStop"})


def _parse_hook_sink(hook_sink_path: Path) -> tuple[list[dict[str, Any]], int]:
    try:
        text = hook_sink_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    except OSError as exc:
        raise _AdapterOperationalError(
            f"hook_sink_read_failed:{type(exc).__name__}",
            source_status="unavailable",
            reason_code="source_not_present",
            exception_class=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        ) from exc

    records: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(raw, dict) or raw.get("event") not in _HOOK_SINK_ALLOWED_EVENTS:
            malformed += 1
            continue
        records.append({k: v for k, v in raw.items() if k in _HOOK_SINK_ALLOWED_KEYS})
    return records, malformed


def _load_transport_log_module():
    """Load `scripts/claude-gpt/transport_log.py` by path (Issue #2236: the
    Claude-GPT adapter reuses this module's ``reqId`` correlation/diagnostics
    logic rather than re-implementing a raw log grep).

    PR #2269 P1 fix: the loaded module is registered in `sys.modules`
    *before* `exec_module()` is called (the standard
    `importlib.util.module_from_spec` recipe). `transport_log.py` uses
    ``from __future__ import annotations`` together with ``@dataclass``, and
    on Python 3.12 the dataclass machinery resolves string annotations via
    ``sys.modules[cls.__module__].__dict__`` -- without the `sys.modules`
    registration this raises `AttributeError` on `None.__dict__` the first
    time `TransportVerdict` is defined. On any failure the partially-loaded
    module is removed from `sys.modules` again so a later retry does not see
    a broken half-initialized module.
    """
    import importlib.util

    module_path = _REPO_ROOT / "scripts" / "claude-gpt" / "transport_log.py"
    spec = importlib.util.spec_from_file_location("agent_retrospective_transport_log", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise _AdapterOperationalError(
            "transport_log_module_unavailable", source_status="unavailable", reason_code="source_not_present"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def collect_claude_gpt_source(
    hook_sink_path: Path,
    *,
    run_nonce: str,
    transport_log_path: Path | None = None,
    source_id: str = "claude_gpt",
    clock: Callable[[], datetime] = _utcnow,
) -> CollectorResult:
    """Collect Claude-GPT session evidence.

    Authority is the run-nonce-keyed hook-event JSONL sink established by
    PR #2222 (``UserPromptSubmit``/``Stop``/``StopFailure``/``SubagentStart``/
    ``SubagentStop`` records correlated by ``session_id``). The mere presence
    of a flat main transcript file is never treated as sufficient evidence of
    ``complete`` (AC8) -- this adapter never reads a flat transcript at all.
    ``transport_log_path``, when supplied, is diagnosed via
    ``transport_log.evaluate_transport_log`` (reused, not re-implemented).

    A source with malformed hook-sink lines alongside otherwise-complete
    session pairing no longer reports `complete` -- see the module docstring
    (PR #2269 hardening).
    """
    fetch_started_at = _iso(clock())
    try:
        records, malformed_line_count = _parse_hook_sink(hook_sink_path)
    except _AdapterOperationalError as exc:
        return _operational_result(
            source_type="runtime", source_id=source_id, fetch_started_at=fetch_started_at, clock=clock, exc=exc
        )

    nonce_matched = [r for r in records if r.get("run_nonce") == run_nonce]
    prompt_session_ids = {r.get("session_id") for r in nonce_matched if r.get("event") == "UserPromptSubmit"}
    stop_session_ids = {r.get("session_id") for r in nonce_matched if r.get("event") == "Stop"}
    stop_failure_events = [r for r in nonce_matched if r.get("event") == "StopFailure"]
    complete_sessions = sorted(s for s in (prompt_session_ids & stop_session_ids) if s is not None)

    diagnostics: dict[str, Any] = {
        "malformed_line_count": malformed_line_count,
        "nonce_matched_count": len(nonce_matched),
        "complete_session_count": len(complete_sessions),
        "stop_failure_count": len(stop_failure_events),
    }
    if transport_log_path is not None:
        transport_module = _load_transport_log_module()
        verdict = transport_module.evaluate_transport_log(str(transport_log_path))
        diagnostics["transport_verdict"] = verdict.to_dict()

    if not records:
        status, pagination, reason = "unavailable", "unknown", "source_not_present"
    elif not nonce_matched:
        # Records exist but none match this run's nonce -- e.g. only a stale
        # flat-transcript-style artifact from a previous run is present.
        # AC8: presence alone is never sufficient for "complete".
        status, pagination, reason = "unavailable", "unknown", "stale_runtime_evidence"
    elif complete_sessions:
        if malformed_line_count > 0:
            status, pagination, reason = "partial", "partial", "malformed_response"
        else:
            status, pagination, reason = "complete", "complete", None
    else:
        status, pagination, reason = "partial", "partial", "session_incomplete"

    fetch_completed_at = _iso(clock())
    observation = _build_observation(
        source_type="runtime",
        source_id=source_id,
        source_status=status,
        pagination_completeness=pagination,
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
        partial_reason=reason if pagination == "partial" else None,
    )
    diagnostics["reason_code"] = reason
    return _finalize(
        observation,
        {
            "normalized_records": nonce_matched,
            "evidence_digest": _digest(nonce_matched),
            "provenance": {"complete_sessions": complete_sessions},
            "diagnostics": diagnostics,
        },
    )


# ---------------------------------------------------------------------------
# repository adapter
# ---------------------------------------------------------------------------

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _default_git_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=60)  # noqa: S603


def _parse_ls_tree_entry(entry: str) -> dict[str, Any]:
    # `git ls-tree` output (non -z-terminated per record): "<mode> <type> <sha>\t<path>"
    meta, _, path = entry.partition("\t")
    parts = meta.split(" ")
    mode = parts[0] if len(parts) > 0 else ""
    obj_type = parts[1] if len(parts) > 1 else ""
    obj_sha = parts[2] if len(parts) > 2 else ""
    return {"mode": mode, "type": obj_type, "sha": obj_sha, "path": path}


def collect_repository_source(
    base_sha: str,
    *,
    repo_root: Path,
    source_id: str = "repository",
    git_runner: Callable[[list[str]], subprocess.CompletedProcess] = _default_git_runner,
) -> CollectorResult:
    """Collect a deterministic repository asset inventory anchored at the
    caller-supplied ``base_sha`` (immutable snapshot).

    This adapter never re-resolves ``main`` (or any other ref) internally
    (AC7) -- ``base_sha`` (a full 40-char commit SHA) is the single anchor,
    and the only Git invocation this function ever makes is
    ``git ls-tree -r -z --full-tree <base_sha>``.
    """
    if not isinstance(base_sha, str) or not _FULL_SHA_RE.match(base_sha):
        raise ValueError(f"base_sha must be a full 40-char hex commit SHA, got {base_sha!r}")

    try:
        completed = git_runner(["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "--full-tree", base_sha])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "source_not_present"
        return _operational_result(
            source_type="repository",
            source_id=source_id,
            fetch_started_at=None,
            clock=_utcnow,
            exc=_AdapterOperationalError(
                str(exc),
                source_status="unavailable",
                reason_code=reason,
                exception_class=type(exc).__name__,
                errno=getattr(exc, "errno", None),
            ),
        )

    if completed.returncode != 0:
        observation = _build_observation(
            source_type="repository",
            source_id=source_id,
            source_status="unavailable",
            pagination_completeness="unknown",
        )
        return _finalize(
            observation,
            {
                "normalized_records": [],
                "evidence_digest": _digest([]),
                "provenance": {"base_sha": base_sha, "returncode": completed.returncode},
                "diagnostics": {
                    "reason_code": "source_not_present",
                    "stderr_excerpt": _safe_diagnostic_text((completed.stderr or "")[:200]),
                },
            },
        )

    stdout = completed.stdout or ""
    entries = [e for e in stdout.split("\0") if e]
    normalized = [_parse_ls_tree_entry(e) for e in entries]
    observation = _build_observation(
        source_type="repository", source_id=source_id, source_status="complete", pagination_completeness="complete"
    )
    return _finalize(
        observation,
        {
            "normalized_records": normalized,
            "evidence_digest": _digest(normalized),
            "provenance": {
                "base_sha": base_sha,
                "command": "git ls-tree -r -z --full-tree <base_sha>",
                "entry_count": len(normalized),
            },
            "diagnostics": {},
        },
    )


# ---------------------------------------------------------------------------
# GitHub adapter
# ---------------------------------------------------------------------------

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')

_GITHUB_BLOCKED_REASON_CODES = frozenset({"auth_ambiguous_404", "permission_denied", "rate_limited"})

_GITHUB_SECRET_ITEM_KEYS = frozenset({"token", "authorization"})

_GITHUB_API_VERSION = "2022-11-28"


@dataclass
class GithubPageResponse:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)


def _parse_link_next(link_header: str | None) -> str | None:
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else None


def _redact_github_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    return {k: v for k, v in item.items() if k.lower() not in _GITHUB_SECRET_ITEM_KEYS}


def _header_lookup(headers: dict[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _classify_github_status(status_code: int, headers: dict[str, str], body: Any) -> str | None:
    """Classify a non-2xx GitHub response into a `reason_code`.

    PR #2269 P1 fix: 403 is not inherently a rate-limit signal (it is also
    returned for plain permission-denied). This now distinguishes
    `rate_limited` from `permission_denied` using the HTTP status code, the
    `Retry-After` / `X-RateLimit-Remaining` headers, and the error body's
    `message` field, rather than collapsing every 403/429 into
    `rate_limited`.
    """
    if status_code == 200:
        return None
    if status_code == 404:
        return "auth_ambiguous_404"
    if status_code == 401:
        return "permission_denied"
    if status_code in (403, 429):
        retry_after = _header_lookup(headers, "Retry-After")
        remaining = _header_lookup(headers, "X-RateLimit-Remaining")
        message = ""
        if isinstance(body, dict):
            message = str(body.get("message") or "")
        message_lower = message.lower()
        rate_limited_signal = (
            status_code == 429
            or retry_after is not None
            or (remaining is not None and str(remaining).strip() == "0")
            or "rate limit" in message_lower
            or "abuse detection" in message_lower
            or "secondary rate limit" in message_lower
        )
        return "rate_limited" if rate_limited_signal else "permission_denied"
    return "malformed_response"


def collect_github_source(
    endpoints: Sequence[str],
    *,
    fetch_page: Callable[[str], GithubPageResponse],
    source_id: str = "github",
    max_pages_per_endpoint: int = 25,
    clock: Callable[[], datetime] = _utcnow,
) -> CollectorResult:
    """Collect GitHub Issue/PR/comment/review/check data across `endpoints`,
    following ``Link: rel="next"`` pagination (never a hand-rolled
    ``page += 1``), recording endpoint/page-level provenance
    (status/Link/etag/completeness) in ``private_evidence`` (AC4).
    """
    fetch_started_at = _iso(clock())
    provenance_pages: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {"errors": []}
    any_success = False
    any_partial = False

    for endpoint in endpoints:
        url: str | None = endpoint
        page_index = 0
        while url:
            page_index += 1
            if page_index > max_pages_per_endpoint:
                any_partial = True
                provenance_pages.append(
                    {
                        "endpoint": endpoint,
                        "page": page_index,
                        "status": None,
                        "link": None,
                        "etag": None,
                        "complete": False,
                        "reason_code": "page_limit_reached",
                    }
                )
                diagnostics["errors"].append(f"page_limit_reached:{endpoint}")
                break

            try:
                response = fetch_page(url)
            except _AdapterOperationalError as exc:
                any_partial = True
                provenance_pages.append(
                    {
                        "endpoint": endpoint,
                        "page": page_index,
                        "status": None,
                        "link": None,
                        "etag": None,
                        "complete": False,
                        "reason_code": exc.reason_code,
                    }
                )
                diagnostics["errors"].append(f"{exc.reason_code}:{endpoint}")
                break

            headers = response.headers or {}
            link = headers.get("Link") or headers.get("link")
            etag = headers.get("ETag") or headers.get("etag")
            status_code = response.status
            body = response.body

            reason_code = _classify_github_status(status_code, headers, body)

            if reason_code is not None:
                any_partial = True
                page_provenance: dict[str, Any] = {
                    "endpoint": endpoint,
                    "page": page_index,
                    "status": status_code,
                    "link": link,
                    "etag": etag,
                    "complete": False,
                    "reason_code": reason_code,
                }
                if reason_code == "rate_limited":
                    page_provenance["rate_limit"] = {
                        "retry_after": _header_lookup(headers, "Retry-After"),
                        "remaining": _header_lookup(headers, "X-RateLimit-Remaining"),
                        "reset": _header_lookup(headers, "X-RateLimit-Reset"),
                    }
                provenance_pages.append(page_provenance)
                diagnostics["errors"].append(f"{reason_code}:{endpoint}:page={page_index}")
                break

            if isinstance(body, list):
                normalized.extend(_redact_github_item(item) for item in body)
            else:
                normalized.append(_redact_github_item(body))
            any_success = True

            next_url = _parse_link_next(link)
            provenance_pages.append(
                {
                    "endpoint": endpoint,
                    "page": page_index,
                    "status": status_code,
                    "link": link,
                    "etag": etag,
                    "complete": next_url is None,
                    "reason_code": None,
                }
            )
            url = next_url

    fetch_completed_at = _iso(clock())
    pagination_completeness = "partial" if any_partial else "complete"
    if any_success:
        source_status = "partial" if any_partial else "complete"
    else:
        failure_reasons = {p["reason_code"] for p in provenance_pages if p.get("reason_code")}
        is_blocked = bool(failure_reasons) and failure_reasons <= _GITHUB_BLOCKED_REASON_CODES
        source_status = "blocked" if is_blocked else "unavailable"

    partial_reason = None
    if pagination_completeness == "partial":
        partial_reason = (
            ",".join(sorted({p["reason_code"] for p in provenance_pages if p.get("reason_code")})) or "partial"
        )

    observation = _build_observation(
        source_type="github",
        source_id=source_id,
        source_status=source_status,
        pagination_completeness=pagination_completeness,
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
        partial_reason=partial_reason,
    )
    return _finalize(
        observation,
        {
            "normalized_records": normalized,
            "evidence_digest": _digest(normalized),
            "provenance": {"pages": provenance_pages},
            "diagnostics": diagnostics,
        },
    )


def _default_gh_runner(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=30)  # noqa: S603


def _parse_gh_api_include_output(raw: str) -> tuple[int | None, dict[str, str], str]:
    """Parse `gh api --include`'s stdout: an HTTP status line, a header
    block, a blank line, then the (possibly-JSON) response body."""
    lines = raw.splitlines()
    if not lines or not lines[0].startswith("HTTP/"):
        return None, {}, ""
    status_line = lines[0]
    parts = status_line.split(" ")
    try:
        status = int(parts[1])
    except (IndexError, ValueError):
        return None, {}, ""
    headers: dict[str, str] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip():
        header_line = lines[idx]
        if ":" in header_line:
            key, _, value = header_line.partition(":")
            headers[key.strip()] = value.strip()
        idx += 1
    body_text = "\n".join(lines[idx + 1 :])
    return status, headers, body_text


def default_github_fetch_page(
    url: str,
    *,
    gh_runner: Callable[[list[str]], subprocess.CompletedProcess] = _default_gh_runner,
) -> GithubPageResponse:
    """Production `fetch_page` implementation for `collect_github_source`
    (Issue #2236 PR #2269 P1 fix: no production transport previously
    existed, only the injectable hermetic-test seam).

    Invokes ``gh api --include <url>``: ``--include`` makes `gh` print the
    raw HTTP status line and response headers (so `Link`/`ETag`/rate-limit
    provenance can be captured) before the JSON body. The GitHub REST API
    version is pinned explicitly via the `X-GitHub-Api-Version` header so
    the endpoint response shape does not silently drift out from under this
    adapter as the API evolves.
    """
    try:
        completed = gh_runner(
            [
                "gh",
                "api",
                "--include",
                "-H",
                f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
                url,
            ]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "source_not_present"
        raise _AdapterOperationalError(
            f"github_fetch_failed:{type(exc).__name__}",
            source_status="unavailable",
            reason_code=reason,
            exception_class=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        ) from exc

    status, headers, body_text = _parse_gh_api_include_output(completed.stdout or "")
    if status is None:
        raise _AdapterOperationalError(
            f"github_fetch_transport_error:rc={completed.returncode}",
            source_status="unavailable",
            reason_code="source_not_present",
        )
    try:
        body = json.loads(body_text) if body_text.strip() else None
    except json.JSONDecodeError:
        body = None
    return GithubPageResponse(status=status, body=body, headers=headers)


# ---------------------------------------------------------------------------
# Web adapter (SSRF-defended primary-source fetch)
# ---------------------------------------------------------------------------

_METADATA_ADDRESSES = frozenset({"169.254.169.254"})


@dataclass
class WebFetchResult:
    status: int
    content: bytes
    final_url: str
    headers: dict[str, str] = field(default_factory=dict)


def _default_resolve(hostname: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def _resolve_pinned_ip(hostname: str, *, resolver: Callable[[str], list[str]]) -> tuple[str | None, str | None]:
    """Resolve ``hostname`` exactly once and validate every returned address
    against the SSRF boundary (private/loopback/link-local/multicast/
    reserved/unspecified/metadata). Returns ``(connect_ip, violation)``: on
    success ``violation`` is `None` and ``connect_ip`` is the first
    validated address; on failure ``connect_ip`` is `None` and ``violation``
    is a `reason_code` string.

    This is the *sole* DNS resolution point used for a given `fetch`
    attempt -- `_check_web_boundary` (the pre-flight gate) and
    `collect_web_source` (which threads the returned `connect_ip` straight
    into `fetch`) both go through this single function, so no second,
    TOCTOU-vulnerable resolution of the (potentially attacker-influenced)
    hostname ever happens between validation and connect (Issue #2236
    PR #2269 P0 fix).

    Only `socket.gaierror`/`OSError` (typed, operational DNS failures) are
    caught around the injected `resolver` call -- a resolver programmer bug
    (`KeyError`, `AssertionError`, ...) is never misclassified as
    `dns_resolution_failed`; it propagates untouched (PR #2269 P1 fix,
    consistent with AC6's general typed-vs-programmer-error invariant).
    """
    try:
        addresses = resolver(hostname)
    except (socket.gaierror, OSError):
        return None, "dns_resolution_failed"
    if not addresses:
        return None, "dns_resolution_failed"
    validated: list[str] = []
    for raw_addr in addresses:
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            return None, "dns_resolution_failed"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or str(ip) in _METADATA_ADDRESSES
        ):
            return None, "private_or_metadata_address_rejected"
        validated.append(str(ip))
    return validated[0], None


def _check_web_boundary(url: str, *, resolver: Callable[[str], list[str]]) -> tuple[str | None, str | None]:
    """Validate `url` against the Web adapter SSRF boundary.

    Returns ``(connect_ip, violation)``: on success `violation` is `None`
    and `connect_ip` is the single validated address `collect_web_source`
    should hand to `fetch` (see `_resolve_pinned_ip`); on failure
    `connect_ip` is `None` and `violation` is a `reason_code` string.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None, "malformed_url"
    if parsed.scheme != "https":
        return None, "non_https_scheme"
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return None, "credential_bearing_url"
    hostname = parsed.hostname
    if not hostname:
        return None, "missing_hostname"
    if hostname.lower() == "localhost":
        return None, "localhost_rejected"
    return _resolve_pinned_ip(hostname, resolver=resolver)


def collect_web_source(
    url: str,
    *,
    fetch: Callable[[str, str], WebFetchResult],
    resolver: Callable[[str], list[str]] = _default_resolve,
    max_bytes: int = 2_000_000,
    max_redirects: int = 5,
    source_id: str = "web",
    clock: Callable[[], datetime] = _utcnow,
) -> CollectorResult:
    """Collect the primary-source Web page at `url`, subject to the SSRF
    defense boundary fixed by Issue #2236 ("Web adapter のネットワーク安全境界"):
    https-only, no credential-bearing URL, no localhost/private/link-local/
    multicast/metadata-address target (post-DNS-resolution), redirects
    re-validated against the same boundary at every hop (rather than
    followed blindly), a response-size cap, and bytes/text-only handling (no
    HTML/JS execution). Boundary violations return ``source_status:
    blocked`` -- distinct from a plain transient ``unavailable`` failure
    (AC5).

    ``fetch`` receives ``(current_url, connect_ip)`` -- ``connect_ip`` is the
    single already-validated address for ``current_url``'s hostname (see
    `_check_web_boundary`/`_resolve_pinned_ip`); this is threaded through so
    a production `fetch` (e.g. `default_https_fetch`) can connect directly
    to it instead of re-resolving the hostname a second time (PR #2269 P0
    fix). If `fetch` reports a different `final_url` (i.e. an HTTP redirect
    occurred), this function re-applies the full boundary check -- including
    a fresh single DNS resolution for the *new* hostname -- and calls
    `fetch` again for that hop, up to `max_redirects` hops.
    """
    fetch_started_at = _iso(clock())
    current_url = url
    redirect_hop = 0
    response: WebFetchResult | None = None

    while True:
        connect_ip, violation = _check_web_boundary(current_url, resolver=resolver)
        if violation:
            reason_code = violation if redirect_hop == 0 else f"redirect_{violation}"
            fetch_completed_at = _iso(clock())
            observation = _build_observation(
                source_type="web",
                source_id=source_id,
                source_status="blocked",
                pagination_completeness="unknown",
                fetch_started_at=fetch_started_at,
                fetch_completed_at=fetch_completed_at,
            )
            return _finalize(
                observation,
                {
                    "normalized_records": [],
                    "evidence_digest": _digest([]),
                    "provenance": {"url": url, "current_url": current_url, "redirect_hop": redirect_hop},
                    "diagnostics": {"reason_code": reason_code},
                },
            )

        try:
            response = fetch(current_url, connect_ip)
        except _AdapterOperationalError as exc:
            return _operational_result(
                source_type="web", source_id=source_id, fetch_started_at=fetch_started_at, clock=clock, exc=exc
            )

        if response.final_url != current_url:
            redirect_hop += 1
            if redirect_hop > max_redirects:
                fetch_completed_at = _iso(clock())
                observation = _build_observation(
                    source_type="web",
                    source_id=source_id,
                    source_status="blocked",
                    pagination_completeness="unknown",
                    fetch_started_at=fetch_started_at,
                    fetch_completed_at=fetch_completed_at,
                )
                return _finalize(
                    observation,
                    {
                        "normalized_records": [],
                        "evidence_digest": _digest([]),
                        "provenance": {
                            "url": url,
                            "current_url": current_url,
                            "attempted_redirect": response.final_url,
                        },
                        "diagnostics": {"reason_code": "redirect_limit_exceeded"},
                    },
                )
            current_url = response.final_url
            continue

        break

    assert response is not None  # the loop always assigns `response` before `break`
    fetch_completed_at = _iso(clock())

    if len(response.content) > max_bytes:
        observation = _build_observation(
            source_type="web",
            source_id=source_id,
            source_status="blocked",
            pagination_completeness="unknown",
            fetch_started_at=fetch_started_at,
            fetch_completed_at=fetch_completed_at,
        )
        return _finalize(
            observation,
            {
                "normalized_records": [],
                "evidence_digest": _digest([]),
                "provenance": {"url": url, "current_url": current_url},
                "diagnostics": {"reason_code": "response_size_exceeded"},
            },
        )

    if response.status != 200:
        observation = _build_observation(
            source_type="web",
            source_id=source_id,
            source_status="unavailable",
            pagination_completeness="unknown",
            fetch_started_at=fetch_started_at,
            fetch_completed_at=fetch_completed_at,
        )
        return _finalize(
            observation,
            {
                "normalized_records": [],
                "evidence_digest": _digest([]),
                "provenance": {"url": url, "current_url": current_url, "status": response.status},
                "diagnostics": {"reason_code": "malformed_response"},
            },
        )

    text = response.content.decode("utf-8", errors="replace")
    normalized = [{"url": current_url, "status": response.status, "text_excerpt": text[:2000]}]
    observation = _build_observation(
        source_type="web",
        source_id=source_id,
        source_status="complete",
        pagination_completeness="complete",
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
    )
    return _finalize(
        observation,
        {
            "normalized_records": normalized,
            "evidence_digest": _digest(normalized),
            "provenance": {"url": url, "current_url": current_url, "status": response.status},
            "diagnostics": {},
        },
    )


def _parse_raw_http_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    """Parse a raw HTTP/1.1 response (status line + headers + body). Bodies
    are sliced to `Content-Length` when present; a response with neither
    `Content-Length` nor `Transfer-Encoding: chunked` is read to EOF by the
    caller (this adapter always sends `Connection: close`), so the
    remaining bytes are the full body either way."""
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    lines = header_blob.split(b"\r\n")
    status_line = lines[0].decode("iso-8859-1", errors="replace")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            key, _, value = line.partition(b":")
            headers[key.decode("iso-8859-1", errors="replace").strip()] = value.decode(
                "iso-8859-1", errors="replace"
            ).strip()
    content_length = headers.get("Content-Length") or headers.get("content-length")
    if content_length is not None:
        try:
            body = body[: int(content_length)]
        except ValueError:
            pass
    return status, headers, body


def _open_pinned_tls_socket(hostname: str, connect_ip: str, port: int, timeout: float) -> ssl.SSLSocket:
    """Open a TCP socket connected directly to the already-DNS-validated
    ``connect_ip`` (never re-resolving ``hostname``) and TLS-wrap it with
    ``hostname`` as the SNI / certificate-verification target.

    This is the single production connection primitive `default_https_fetch`
    uses. Because the socket connects to a numeric IP rather than
    re-resolving ``hostname``, no second (TOCTOU-vulnerable) DNS lookup for
    the untrusted target hostname ever happens; and because this bypasses
    `urllib.request`'s opener chain entirely, no `http_proxy`/`https_proxy`
    environment variable is ever consulted either (Issue #2236 PR #2269 P0
    fix).
    """
    raw_sock = socket.create_connection((connect_ip, port), timeout=timeout)
    context = ssl.create_default_context()
    return context.wrap_socket(raw_sock, server_hostname=hostname)


def default_https_fetch(
    url: str,
    connect_ip: str,
    *,
    timeout: float = 10.0,
    max_bytes: int = 2_000_000,
    socket_opener: Callable[[str, str, int, float], Any] = _open_pinned_tls_socket,
) -> WebFetchResult:
    """Production `fetch` implementation for `collect_web_source`.

    ``connect_ip`` is the caller-supplied, single-resolution, validated IP
    address for `url`'s hostname (see `_resolve_pinned_ip`/
    `_check_web_boundary`). Connecting directly to this IP -- rather than
    handing the hostname to a general-purpose HTTP client that would
    re-resolve it -- closes the DNS-rebinding/TOCTOU window between
    validation and connect that the pre-fix implementation had (it discarded
    the validated IP and let `urllib.request` re-resolve the original
    hostname via the opener chain, which also silently honored
    `http_proxy`/`https_proxy` environment variables).

    Manual redirects (HTTP 3xx `Location`) are surfaced to the caller as a
    `final_url` != `url` result rather than followed automatically, so
    `collect_web_source` can re-apply the full SSRF boundary (including a
    fresh, single DNS resolution) to each hop.
    """
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname
    if not hostname:
        raise _AdapterOperationalError(
            "web_fetch_missing_hostname", source_status="blocked", reason_code="missing_hostname"
        )
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    try:
        tls_sock = socket_opener(hostname, connect_ip, port, timeout)
    except TimeoutError as exc:
        raise _AdapterOperationalError(
            "web_fetch_timeout",
            source_status="unavailable",
            reason_code="timeout",
            exception_class=type(exc).__name__,
        ) from exc
    except (OSError, ssl.SSLError) as exc:
        raise _AdapterOperationalError(
            f"web_fetch_transport_error:{type(exc).__name__}",
            source_status="unavailable",
            reason_code="source_not_present",
            exception_class=type(exc).__name__,
            errno=getattr(exc, "errno", None),
        ) from exc

    try:
        request_text = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostname}\r\n"
            "User-Agent: loop-protocol-agent-retrospective-web-adapter (Issue-2236)\r\n"
            "Accept-Encoding: identity\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            tls_sock.sendall(request_text.encode("ascii"))
            raw_response = b""
            while True:
                chunk = tls_sock.recv(65536)
                if not chunk:
                    break
                raw_response += chunk
                if len(raw_response) > max_bytes + 65536:
                    break
        except TimeoutError as exc:
            raise _AdapterOperationalError(
                "web_fetch_timeout",
                source_status="unavailable",
                reason_code="timeout",
                exception_class=type(exc).__name__,
            ) from exc
        except OSError as exc:
            raise _AdapterOperationalError(
                f"web_fetch_transport_error:{type(exc).__name__}",
                source_status="unavailable",
                reason_code="source_not_present",
                exception_class=type(exc).__name__,
                errno=getattr(exc, "errno", None),
            ) from exc
    finally:
        try:
            tls_sock.close()
        except OSError:
            pass

    status, headers, body = _parse_raw_http_response(raw_response)
    if status == 0:
        raise _AdapterOperationalError(
            "web_fetch_malformed_status_line", source_status="unavailable", reason_code="malformed_response"
        )

    if 300 <= status < 400:
        location = headers.get("Location") or headers.get("location")
        if not location:
            raise _AdapterOperationalError(
                "web_fetch_redirect_missing_location",
                source_status="unavailable",
                reason_code="malformed_response",
            )
        final_url = urllib.parse.urljoin(url, location)
        return WebFetchResult(status=0, content=b"", final_url=final_url, headers=headers)

    return WebFetchResult(status=status, content=body[: max_bytes + 1], final_url=url, headers=headers)


# ---------------------------------------------------------------------------
# orchestration (fail-independent multi-source collection)
# ---------------------------------------------------------------------------


def collect_all_sources(collectors: Sequence[Callable[[], CollectorResult]]) -> list[CollectorResult]:
    """Run each zero-arg collector callable in sequence and return the list
    of `CollectorResult`s.

    Each `collect_*_source` function already absorbs its own typed
    operational failures internally (AC6), so this orchestrator does not
    (and must not) wrap the calls in a blanket ``except Exception`` -- a
    programmer bug/contract violation raised by one collector propagates to
    the caller as a run-level fatal error rather than being silently
    swallowed here.
    """
    return [collector() for collector in collectors]
