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
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
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

    def __init__(self, message: str, *, source_status: str, reason_code: str) -> None:
        super().__init__(message)
        assert source_status in ("unavailable", "blocked"), source_status
        self.source_status = source_status
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# secret / local-path scrubbing (AC9) -- applied to every CollectorResult
# ---------------------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|credential|api[_-]?key|cookie)", re.IGNORECASE
)
_ABS_PATH_RE = re.compile(r"^(/home/|/Users/|/root/|[A-Za-z]:\\)")
_BEARER_RE = re.compile(r"^(bearer|basic|token)\s+\S+", re.IGNORECASE)


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
        if _ABS_PATH_RE.match(value):
            return "[redacted-local-path]"
        if _BEARER_RE.match(value):
            return "[redacted-credential]"
        return value
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
            "diagnostics": {"reason_code": exc.reason_code, "message": str(exc)},
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
    status = "complete" if normalized else "unavailable"
    observation = _build_observation(
        source_type="runtime",
        source_id=source_id,
        source_status=status,
        pagination_completeness="complete" if normalized else "unknown",
        fetch_started_at=fetch_started_at,
        fetch_completed_at=fetch_completed_at,
    )
    return _finalize(
        observation,
        {
            "normalized_records": normalized,
            "evidence_digest": _digest(normalized),
            "provenance": {"session_count": len(session_paths), "sessions_read": sessions_read},
            "diagnostics": {
                "malformed_line_count": malformed_line_count,
                "reason_code": None if normalized else "source_not_present",
            },
        },
    )


# ---------------------------------------------------------------------------
# Claude-GPT adapter
# ---------------------------------------------------------------------------

_HOOK_SINK_ALLOWED_KEYS = frozenset({"run_nonce", "event", "session_id", "agent_id", "ts", "prompt_digest"})
_HOOK_SINK_ALLOWED_EVENTS = frozenset(
    {"UserPromptSubmit", "Stop", "StopFailure", "SubagentStart", "SubagentStop"}
)


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
    logic rather than re-implementing a raw log grep)."""
    import importlib.util

    module_path = _REPO_ROOT / "scripts" / "claude-gpt" / "transport_log.py"
    spec = importlib.util.spec_from_file_location("agent_retrospective_transport_log", module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise _AdapterOperationalError(
            "transport_log_module_unavailable", source_status="unavailable", reason_code="source_not_present"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
            exc=_AdapterOperationalError(str(exc), source_status="unavailable", reason_code=reason),
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
                "diagnostics": {"stderr": (completed.stderr or "")[:500], "reason_code": "source_not_present"},
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

            reason_code: str | None = None
            if status_code in (403, 429):
                reason_code = "rate_limited"
            elif status_code == 404:
                reason_code = "auth_ambiguous_404"
            elif status_code == 401:
                reason_code = "permission_denied"
            elif status_code != 200:
                reason_code = "malformed_response"

            if reason_code is not None:
                any_partial = True
                provenance_pages.append(
                    {
                        "endpoint": endpoint,
                        "page": page_index,
                        "status": status_code,
                        "link": link,
                        "etag": etag,
                        "complete": False,
                        "reason_code": reason_code,
                    }
                )
                diagnostics["errors"].append(f"{reason_code}:{endpoint}:page={page_index}")
                break

            body = response.body
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
        partial_reason = ",".join(
            sorted({p["reason_code"] for p in provenance_pages if p.get("reason_code")})
        ) or "partial"

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
    import socket

    return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def _check_web_boundary(url: str, *, resolver: Callable[[str], list[str]]) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return "malformed_url"
    if parsed.scheme != "https":
        return "non_https_scheme"
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return "credential_bearing_url"
    hostname = parsed.hostname
    if not hostname:
        return "missing_hostname"
    if hostname.lower() == "localhost":
        return "localhost_rejected"
    try:
        addresses = resolver(hostname)
    except Exception:
        return "dns_resolution_failed"
    if not addresses:
        return "dns_resolution_failed"
    for raw_addr in addresses:
        try:
            ip = ipaddress.ip_address(raw_addr)
        except ValueError:
            return "dns_resolution_failed"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            or str(ip) in _METADATA_ADDRESSES
        ):
            return "private_or_metadata_address_rejected"
    return None


def collect_web_source(
    url: str,
    *,
    fetch: Callable[[str], WebFetchResult],
    resolver: Callable[[str], list[str]] = _default_resolve,
    max_bytes: int = 2_000_000,
    source_id: str = "web",
    clock: Callable[[], datetime] = _utcnow,
) -> CollectorResult:
    """Collect the primary-source Web page at `url`, subject to the SSRF
    defense boundary fixed by Issue #2236 ("Web adapter のネットワーク安全境界"):
    https-only, no credential-bearing URL, no localhost/private/link-local/
    multicast/metadata-address target (post-DNS-resolution), redirects
    re-validated against the same boundary rather than followed blindly, a
    response-size cap, and bytes/text-only handling (no HTML/JS execution).
    Boundary violations return ``source_status: blocked`` -- distinct from a
    plain transient ``unavailable`` failure (AC5).
    """
    fetch_started_at = _iso(clock())
    violation = _check_web_boundary(url, resolver=resolver)
    if violation:
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
                "provenance": {"url": url},
                "diagnostics": {"reason_code": violation},
            },
        )

    try:
        response = fetch(url)
    except _AdapterOperationalError as exc:
        return _operational_result(
            source_type="web", source_id=source_id, fetch_started_at=fetch_started_at, clock=clock, exc=exc
        )

    fetch_completed_at = _iso(clock())

    if response.final_url != url:
        redirect_violation = _check_web_boundary(response.final_url, resolver=resolver)
        if redirect_violation:
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
                    "provenance": {"url": url, "final_url": response.final_url},
                    "diagnostics": {"reason_code": f"redirect_{redirect_violation}"},
                },
            )

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
                "provenance": {"url": url},
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
                "provenance": {"url": url, "status": response.status},
                "diagnostics": {"reason_code": "malformed_response"},
            },
        )

    text = response.content.decode("utf-8", errors="replace")
    normalized = [{"url": url, "status": response.status, "text_excerpt": text[:2000]}]
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
            "provenance": {"url": url, "status": response.status},
            "diagnostics": {},
        },
    )


def default_https_fetch(url: str, *, timeout: float = 10.0, max_bytes: int = 2_000_000) -> WebFetchResult:
    """Production `fetch` implementation for `collect_web_source`: a single
    GET with no automatic redirect following (redirects are surfaced as
    `final_url` != `url` via a manual, bounded, re-validated hop so
    `collect_web_source` can re-apply the SSRF boundary at each hop rather
    than trusting `urllib`'s default silent-follow behavior), a hard byte
    cap enforced while reading, and bytes-only handling (never executes
    HTML/JS)."""
    import urllib.error
    import urllib.request

    class _RedirectSignal(Exception):
        def __init__(self, target: str) -> None:
            super().__init__(target)
            self.target = target

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
            raise _RedirectSignal(newurl)

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", "loop-protocol-agent-retrospective-web-adapter (Issue-2236)")

    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            content = response.read(max_bytes + 1)
            status = response.status
            final_url = response.geturl()
    except _RedirectSignal as exc:
        return WebFetchResult(status=0, content=b"", final_url=exc.target)
    except TimeoutError as exc:
        raise _AdapterOperationalError("web_fetch_timeout", source_status="unavailable", reason_code="timeout") from exc
    except urllib.error.URLError as exc:
        raise _AdapterOperationalError(
            f"web_fetch_transport_error:{type(exc.reason).__name__}",
            source_status="unavailable",
            reason_code="source_not_present",
        ) from exc

    return WebFetchResult(status=status, content=content, final_url=final_url)


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
