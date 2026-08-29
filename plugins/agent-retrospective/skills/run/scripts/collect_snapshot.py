#!/usr/bin/env python3
"""collect_snapshot.py -- plugin-local repository source collector (Issue
#2240, agent-retrospective plugin distribution).

Deliberately trimmed port of the host repository's own project Skill
implementation of this same collector (that sibling module is unmodified --
Out of Scope). This plugin's ``run_retrospective.py``
call graph only ever wires ``collect_repository_source`` into ``prepare()``
(the Claude Code / Claude-GPT / GitHub / Web adapters, and Latitude runtime
evidence collection, are project-Skill-only concerns this plugin does not
port -- Issue #2240 explicitly excludes AGY/Latitude/Claude-GPT
``transport_log.py`` dependencies from the plugin's runtime closure).

Design invariant preserved from the project Skill: the repository adapter
never re-resolves ``main`` (or any other ref) internally -- ``base_sha`` (a
full 40-char commit SHA) is the single anchor, and the only Git invocation
this function makes is ``git ls-tree -r -z --full-tree <base_sha>``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

SOURCE_STATUSES = frozenset({"complete", "partial", "unavailable", "blocked"})

_SAFE_DIAGNOSTIC_TEXT_RE = re.compile(r"^[A-Za-z0-9 ,.:;_/=\-]{0,200}$")
_ABS_PATH_RE = re.compile(r"(?:^|[^A-Za-z0-9_])(/(?:home|Users|root|tmp|mnt|var|workspace)/\S*|[A-Za-z]:\\\S*)")
_BEARER_RE = re.compile(
    r"(?:authorization\s*:\s*)?\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}"
    r"|\bghp_[A-Za-z0-9]{20,}\b"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    re.IGNORECASE,
)
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|credential|api[_-]?key|cookie)", re.IGNORECASE
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(records: Any) -> str:
    canonical = json.dumps(records, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scrub(value: Any) -> Any:
    """Recursively drop secret-shaped keys and redact absolute local paths /
    Authorization-header-shaped string values (mirrors the project Skill's
    AC9 scrubbing)."""
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
    if not value:
        return None
    if not _SAFE_DIAGNOSTIC_TEXT_RE.match(value):
        return None
    if _ABS_PATH_RE.search(value) or _BEARER_RE.search(value):
        return None
    return value


@dataclass
class CollectorResult:
    """Dual-channel result returned by ``collect_repository_source``.
    ``observation`` is public-safe; ``private_evidence`` is
    invocation-private."""

    observation: dict[str, Any]
    private_evidence: dict[str, Any] = field(default_factory=dict)


def _finalize(observation: dict[str, Any], private_evidence: dict[str, Any]) -> CollectorResult:
    return CollectorResult(observation=_scrub(observation), private_evidence=_scrub(private_evidence))


def _build_observation(*, source_type: str, source_id: str, source_status: str, pagination_completeness: str) -> dict[str, Any]:
    assert source_status in SOURCE_STATUSES, source_status
    return {
        "source_type": source_type,
        "source_id": source_id,
        "source_status": source_status,
        "pagination_completeness": pagination_completeness,
    }


class _AdapterOperationalError(Exception):
    def __init__(self, message: str, *, source_status: str, reason_code: str, exception_class: str | None = None) -> None:
        super().__init__(message)
        assert source_status in ("unavailable", "blocked"), source_status
        self.source_status = source_status
        self.reason_code = reason_code
        self.exception_class = exception_class


def _operational_result(*, source_type: str, source_id: str, exc: _AdapterOperationalError) -> CollectorResult:
    observation = _build_observation(
        source_type=source_type, source_id=source_id, source_status=exc.source_status, pagination_completeness="unknown"
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
                "safe_detail": _safe_diagnostic_text(str(exc)),
            },
        },
    )


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
    caller-supplied ``base_sha`` (immutable snapshot). Never re-resolves
    ``main`` (or any other ref) internally."""
    if not isinstance(base_sha, str) or not _FULL_SHA_RE.match(base_sha):
        raise ValueError(f"base_sha must be a full 40-char hex commit SHA, got {base_sha!r}")

    try:
        completed = git_runner(["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "--full-tree", base_sha])
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "source_not_present"
        return _operational_result(
            source_type="repository",
            source_id=source_id,
            exc=_AdapterOperationalError(
                str(exc), source_status="unavailable", reason_code=reason, exception_class=type(exc).__name__
            ),
        )

    if completed.returncode != 0:
        observation = _build_observation(
            source_type="repository", source_id=source_id, source_status="unavailable", pagination_completeness="unknown"
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
