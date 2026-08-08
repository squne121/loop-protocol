#!/usr/bin/env python3
"""Token-owning broker for the AGY `github_research` read-only route (Issue #1920).

This module is the *only* component that ever sees `GH_TOKEN`. The AGY
process itself never receives the token: `run_agy_github_research_e2e.py`
invokes AGY with the `github_research` tool_profile, whose permission policy
(`agy_permission_policy.PROFILE_ALLOWED_TOOLS["github_research"]`) is empty,
so AGY has no native tool-call surface at all and can only respond with plain
text. This broker parses that text (in the e2e orchestrator) for a single
next-*operation* directive expressed as a broker-owned operation id (never a
raw `gh` argv or a raw `gh api` endpoint), validates it against a fixed
operation table *before* any subprocess is spawned, and -- only if allowed --
builds the full `gh` argv itself (repository/host binding, `--limit`,
pagination, and endpoint shape are never influenced by AGY) and executes
exactly one `gh` invocation with `shell=False`, an isolated `GH_CONFIG_DIR`,
`stdin=DEVNULL`, a streaming byte cap enforced *while reading* (not after
buffering the full output), a process-group kill on timeout/overflow, and
redaction-before-truncate.

Out of scope (Issue #1920 Out of Scope): any mutating `gh` subcommand,
`gh auth`, `gh alias`, `gh extension`, arbitrary shell, `gh api graphql`,
non-GET `gh api`, and (OWNER PR #2000 REQUEST_CHANGES Blocker 2/3) any raw
`gh api <endpoint>` passthrough or unbound `gh search repos` / cross-repo
`gh repo view` / `gh release`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_COMMAND_RESULT = "agy_github_research_broker_command_result/v1"

DEFAULT_HOST = "github.com"
DEFAULT_REPO = "squne121/loop-protocol"

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
DEFAULT_STDOUT_CAP_BYTES = 65536
DEFAULT_STDERR_CAP_BYTES = 16384
MAX_RECORDS_PER_COMMAND = 100
DEFAULT_LIMIT = 30

# Credential bootstrap (Issue #2012): resolving a stored `gh` credential via
# `gh auth token --hostname <host>` when no GH_TOKEN/GITHUB_TOKEN env var is
# already present in this broker (sub)process's own startup environment.
# This is a *fail-closed* classification -- every branch below returns a
# structured reason string and never the token value itself.
GH_AUTH_TOKEN_BOOTSTRAP_TIMEOUT_SECONDS = 15.0

REASON_CREDENTIAL_BOOTSTRAP_GH_CLI_UNAVAILABLE = "credential_bootstrap_gh_cli_unavailable"
REASON_CREDENTIAL_BOOTSTRAP_TIMEOUT = "credential_bootstrap_timeout"
REASON_CREDENTIAL_BOOTSTRAP_NONZERO_EXIT = "credential_bootstrap_nonzero_exit"
REASON_CREDENTIAL_BOOTSTRAP_EMPTY_TOKEN = "credential_bootstrap_empty_token"
REASON_CREDENTIAL_BOOTSTRAP_MALFORMED_OUTPUT = "credential_bootstrap_malformed_output"

_READ_CHUNK_BYTES = 65536
_GRACE_KILL_SECONDS = 3.0

# Tokens that always signal an attempted shell escape / compound command,
# regardless of position. Checked against every operation name and every
# stringified param value.
_COMPOUND_SHELL_TOKEN_RE = re.compile(r"[;&|`]|\$\(|\r|\n")

# Credential-display probes: an operation name or param value that looks like
# an attempt to read/echo the token itself, or invoke a shell builtin/env dump.
_CREDENTIAL_DISPLAY_PATTERNS: tuple[str, ...] = (
    "GH_TOKEN",
    "$GH_TOKEN",
    "env",
    "printenv",
    "auth_token",
    "auth token",
    "auth_status",
    "auth status",
    "token",
)

# Operation-name substrings that signal an attempted mutation even though the
# operation id itself is not in the fixed table (used only to classify a
# denial's probe_class; the denial itself is decided by table membership).
_MUTATION_OPERATION_HINTS: tuple[str, ...] = (
    "close",
    "edit",
    "create",
    "delete",
    "merge",
    "comment",
    "review",
    "reopen",
    "lock",
    "transfer",
    "pin",
    "set",
    "login",
    "install",
)

# Token-shaped substrings redacted from output *before* truncation and
# *before* digesting, regardless of whether the real token is present
# verbatim (defense in depth against partial-token leakage from error text).
_TOKEN_SHAPE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._-]{10,}",
)

_REDACTED_MARKER = "[REDACTED]"

# Safe-string param shape: printable, no leading `-` (flag-injection guard),
# no shell metacharacters (checked separately/first for probe_class fidelity).
_SAFE_STRING_RE = re.compile(r"^[A-Za-z0-9 ._:/#@+]{1,200}$")


class BrokerDenied(Exception):
    """Raised for a pre-execution deny; never spawns a subprocess."""


class BrokerInvariantError(Exception):
    """Raised when a runtime invariant (numeric contract, kill guarantee) fails."""


@dataclass(frozen=True)
class ValidationResult:
    allowed: bool
    reason: str
    probe_class: str | None = None


def _validate_positive_int(value: Any) -> tuple[bool, str]:
    if isinstance(value, bool) or not isinstance(value, int):
        return False, "param_must_be_int"
    if value <= 0 or value > 999_999_999:
        return False, "param_out_of_range"
    return True, "ok"


def _validate_limit(value: Any) -> tuple[bool, str]:
    ok, reason = _validate_positive_int(value)
    if not ok:
        return ok, reason
    if value > MAX_RECORDS_PER_COMMAND:
        return False, "limit_exceeds_max_records_per_command"
    return True, "ok"


def _validate_state(value: Any) -> tuple[bool, str]:
    if value not in ("open", "closed", "all"):
        return False, "invalid_state_value"
    return True, "ok"


def _validate_safe_string(value: Any) -> tuple[bool, str]:
    if not isinstance(value, str) or not value:
        return False, "param_must_be_nonempty_string"
    if value.startswith("-"):
        return False, "param_looks_like_flag_injection"
    if not _SAFE_STRING_RE.match(value):
        return False, "param_contains_disallowed_characters"
    return True, "ok"


def _fmt_limit(params: Mapping[str, Any]) -> str:
    return str(params.get("limit", DEFAULT_LIMIT))


def _build_get_issue(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["issue", "view", str(params["number"]), "--repo", f"{host}/{repo}"]


def _build_list_issues(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return [
        "issue",
        "list",
        "--repo",
        f"{host}/{repo}",
        "--state",
        str(params.get("state", "open")),
        "--limit",
        _fmt_limit(params),
    ]


def _build_get_pr(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["pr", "view", str(params["number"]), "--repo", f"{host}/{repo}"]


def _build_list_prs(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return [
        "pr",
        "list",
        "--repo",
        f"{host}/{repo}",
        "--state",
        str(params.get("state", "open")),
        "--limit",
        _fmt_limit(params),
    ]


def _build_get_pr_diff(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["pr", "diff", str(params["number"]), "--repo", f"{host}/{repo}"]


def _build_get_pr_checks(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["pr", "checks", str(params["number"]), "--repo", f"{host}/{repo}"]


def _build_get_repo(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["repo", "view", f"{host}/{repo}"]


def _build_search_issues(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["search", "issues", "--repo", f"{host}/{repo}", "--limit", _fmt_limit(params), str(params["query"])]


def _build_search_prs(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["search", "prs", "--repo", f"{host}/{repo}", "--limit", _fmt_limit(params), str(params["query"])]


def _build_list_releases(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["release", "list", "--repo", f"{host}/{repo}", "--limit", _fmt_limit(params)]


def _build_get_release(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["release", "view", str(params["tag"]), "--repo", f"{host}/{repo}"]


def _build_get_issue_comments(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    # Fixed endpoint template -- no AGY-controlled endpoint/method/field/
    # header/input/pagination flag is ever accepted (Issue #1920 PR #2000
    # REQUEST_CHANGES Blocker 2).
    return ["api", f"repos/{repo}/issues/{params['number']}/comments"]


def _build_get_pr_review_comments(params: Mapping[str, Any], host: str, repo: str) -> list[str]:
    return ["api", f"repos/{repo}/pulls/{params['number']}/comments"]


@dataclass(frozen=True)
class OperationSpec:
    required_params: frozenset[str]
    optional_params: frozenset[str]
    validators: dict[str, Callable[[Any], tuple[bool, str]]] = field(default_factory=dict)
    builder: Callable[[Mapping[str, Any], str, str], list[str]] = field(default=None)  # type: ignore[assignment]


_OPERATIONS: dict[str, OperationSpec] = {
    "get_issue": OperationSpec(
        frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_issue
    ),
    "list_issues": OperationSpec(
        frozenset(),
        frozenset({"state", "limit"}),
        {"state": _validate_state, "limit": _validate_limit},
        _build_list_issues,
    ),
    "get_pr": OperationSpec(frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_pr),
    "list_prs": OperationSpec(
        frozenset(),
        frozenset({"state", "limit"}),
        {"state": _validate_state, "limit": _validate_limit},
        _build_list_prs,
    ),
    "get_pr_diff": OperationSpec(
        frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_pr_diff
    ),
    "get_pr_checks": OperationSpec(
        frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_pr_checks
    ),
    "get_repo": OperationSpec(frozenset(), frozenset(), {}, _build_get_repo),
    "search_issues": OperationSpec(
        frozenset({"query"}),
        frozenset({"limit"}),
        {"query": _validate_safe_string, "limit": _validate_limit},
        _build_search_issues,
    ),
    "search_prs": OperationSpec(
        frozenset({"query"}),
        frozenset({"limit"}),
        {"query": _validate_safe_string, "limit": _validate_limit},
        _build_search_prs,
    ),
    "list_releases": OperationSpec(frozenset(), frozenset({"limit"}), {"limit": _validate_limit}, _build_list_releases),
    "get_release": OperationSpec(frozenset({"tag"}), frozenset(), {"tag": _validate_safe_string}, _build_get_release),
    "get_issue_comments": OperationSpec(
        frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_issue_comments
    ),
    "get_pr_review_comments": OperationSpec(
        frozenset({"number"}), frozenset(), {"number": _validate_positive_int}, _build_get_pr_review_comments
    ),
}

ALLOWED_OPERATIONS: frozenset[str] = frozenset(_OPERATIONS)


def _stringify_params(params: Mapping[str, Any]) -> str:
    try:
        return " ".join(f"{k}={v}" for k, v in params.items())
    except Exception:  # noqa: BLE001 - defensive; malformed params must never crash validation
        return str(params)


def _looks_like_credential_display(operation: str, params: Mapping[str, Any]) -> bool:
    joined = f"{operation} {_stringify_params(params)}".lower()
    return any(pattern.lower() in joined for pattern in _CREDENTIAL_DISPLAY_PATTERNS)


def _looks_like_mutation_operation(operation: str) -> bool:
    lowered = operation.lower()
    return any(hint in lowered for hint in _MUTATION_OPERATION_HINTS)


def validate_operation(operation: Any, params: Any) -> ValidationResult:
    """Pre-execution validator for a single broker operation id + params.

    Fail-closed: any operation not explicitly in `_OPERATIONS`, or any param
    shape not explicitly validated for that operation, is denied. Never
    spawns a subprocess. Repository/host binding is never a caller-supplied
    param -- `repo`/`owner`/`host`/`hostname` in `params` are always denied
    before the fixed operation table is even consulted (Issue #1920 PR #2000
    REQUEST_CHANGES Blocker 3).
    """
    if not isinstance(operation, str) or not operation:
        return ValidationResult(False, "empty_or_malformed_operation")
    if not isinstance(params, dict):
        return ValidationResult(False, "params_must_be_object")
    if not all(isinstance(key, str) for key in params):
        return ValidationResult(False, "params_keys_must_be_strings")

    if _COMPOUND_SHELL_TOKEN_RE.search(operation) or any(
        isinstance(v, str) and _COMPOUND_SHELL_TOKEN_RE.search(v) for v in params.values()
    ):
        return ValidationResult(False, "compound_shell_token_denied", probe_class="compound_shell")
    if _looks_like_credential_display(operation, params):
        return ValidationResult(False, "credential_display_denied", probe_class="credential_display")
    if "host" in params or "hostname" in params:
        return ValidationResult(False, "alternate_host_denied", probe_class="alternate_host")
    if "repo" in params or "owner" in params or "repository" in params:
        return ValidationResult(False, "cross_repository_denied", probe_class="cross_repository")

    spec = _OPERATIONS.get(operation)
    if spec is None:
        if _looks_like_mutation_operation(operation):
            return ValidationResult(False, "mutation_operation_denied", probe_class="mutation")
        return ValidationResult(False, "unknown_operation_denied")

    allowed_keys = spec.required_params | spec.optional_params
    unexpected = set(params) - allowed_keys
    if unexpected:
        return ValidationResult(False, f"unexpected_param:{sorted(unexpected)[0]}")
    missing = spec.required_params - set(params)
    if missing:
        return ValidationResult(False, f"missing_param:{sorted(missing)[0]}")
    for key, value in params.items():
        validator = spec.validators.get(key)
        if validator is None:
            return ValidationResult(False, f"no_validator_for_param:{key}")
        ok, reason = validator(value)
        if not ok:
            return ValidationResult(False, reason)
    return ValidationResult(True, "allowed_operation")


def build_argv(
    operation: str, params: Mapping[str, Any], *, host: str = DEFAULT_HOST, repo: str = DEFAULT_REPO
) -> list[str]:
    spec = _OPERATIONS[operation]
    return spec.builder(params, host, repo)


def _isolated_gh_config_dir(tmp_root: Path) -> Path:
    """Return a fresh, empty private GH_CONFIG_DIR under *tmp_root*.

    Empty on every call: existing host auth/alias/extension/pager config in
    the real `$GH_CONFIG_DIR` (or default `~/.config/gh`) must never be
    reachable from a broker-executed command.
    """
    config_dir = tmp_root / "gh-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        config_dir.chmod(0o700)
    except OSError:
        pass
    return config_dir


def _looks_like_malformed_token_output(raw: str) -> bool:
    """A genuine `gh auth token` success prints exactly one bare token line.

    Anything else (empty, multi-line, embedded whitespace/control chars) is
    treated as malformed rather than guessed-at or partially used.
    """
    if not raw:
        return True
    if "\n" in raw or "\r" in raw:
        return True
    if any(ch.isspace() for ch in raw):
        return True
    return False


def bootstrap_gh_token(
    *,
    host: str = DEFAULT_HOST,
    gh_bin: str = "gh",
    timeout_seconds: float = GH_AUTH_TOKEN_BOOTSTRAP_TIMEOUT_SECONDS,
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a stored `gh` credential for *host* via `gh auth token`.

    Runs entirely inside this broker process/subprocess. Uses the *ambient*
    `gh` credential store (this call's own inherited environment, e.g. the
    real `GH_CONFIG_DIR` / `~/.config/gh` populated by a prior
    `gh auth login`) because that is where the stored credential actually
    lives -- this is deliberately never the isolated, fresh `GH_CONFIG_DIR`
    used afterwards to execute the research `gh` command itself (Issue
    #2012 In Scope: isolation invariant preserved after bootstrap).

    Returns `(token, failure_reason)`; exactly one of the two is not `None`.
    The resolved token (if any) is returned only as an in-memory value --
    never printed, logged, or included in any diagnostic/error output, even
    on failure. `--hostname` is always passed explicitly (never omitted, and
    never taken from an AGY- or caller-controlled param) so credential
    resolution is deterministically pinned to *host*.
    """
    resolved_bin = shutil.which(gh_bin) or gh_bin
    argv = [resolved_bin, "auth", "token", "--hostname", host]
    try:
        completed = subprocess.run(
            argv,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, REASON_CREDENTIAL_BOOTSTRAP_GH_CLI_UNAVAILABLE
    except subprocess.TimeoutExpired:
        return None, REASON_CREDENTIAL_BOOTSTRAP_TIMEOUT

    if completed.returncode != 0:
        return None, REASON_CREDENTIAL_BOOTSTRAP_NONZERO_EXIT

    raw = (completed.stdout or "").strip("\n")
    if not raw or not raw.strip():
        return None, REASON_CREDENTIAL_BOOTSTRAP_EMPTY_TOKEN
    if _looks_like_malformed_token_output(raw):
        return None, REASON_CREDENTIAL_BOOTSTRAP_MALFORMED_OUTPUT
    return raw, None


def resolve_gh_token(
    *,
    gh_token_env: str = "GH_TOKEN",
    host: str = DEFAULT_HOST,
    gh_bin: str = "gh",
) -> tuple[str | None, str | None, bool]:
    """Resolve the token this broker (sub)process will use for one operation.

    Priority: an explicit `GH_TOKEN`/`GITHUB_TOKEN`-style env var already
    present in *this process's own* startup environment wins (no bootstrap
    performed). Otherwise, `bootstrap_gh_token()` resolves a stored
    credential internally. Returns `(token, failure_reason, bootstrap_used)`.
    """
    env_token = os.environ.get(gh_token_env)
    if env_token:
        return env_token, None, False
    token, failure_reason = bootstrap_gh_token(host=host, gh_bin=gh_bin)
    return token, failure_reason, True


def _redact(text: str) -> tuple[str, bool]:
    redacted_text, count = _TOKEN_SHAPE_RE.subn(_REDACTED_MARKER, text)
    return redacted_text, count > 0


def _bounded_redacted(text: str, cap_bytes: int) -> tuple[str, bool]:
    """Redact before truncating, per Issue #1920's numeric contract."""
    redacted_text, _ = _redact(text)
    encoded = redacted_text.encode("utf-8", errors="replace")
    if len(encoded) <= cap_bytes:
        return redacted_text, False
    truncated = encoded[:cap_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _minimal_broker_env(*, gh_token: str, host: str, repo: str, gh_config_dir: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    path_value = os.environ.get("PATH")
    if path_value:
        env["PATH"] = path_value
    env["GH_TOKEN"] = gh_token
    env["GH_HOST"] = host
    env["GH_REPO"] = repo
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_CONFIG_DIR"] = str(gh_config_dir)
    env["NO_COLOR"] = "1"
    return env


def _kill_process_group(pid: int, *, force: bool = False) -> None:
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError:  # pragma: no cover - defensive
        return


def _run_streaming_bounded(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: str | None,
    timeout_seconds: float,
    stdout_cap_bytes: int,
    stderr_cap_bytes: int,
) -> tuple[bytes, bytes, int | None, bool, bool]:
    """Run *command* with a streaming byte cap enforced while reading.

    Returns (stdout_bytes, stderr_bytes, exit_code, timed_out,
    output_limit_exceeded). On timeout or byte-cap overflow the whole
    process group is killed (SIGTERM then SIGKILL) -- output is never fully
    buffered into memory before truncation (Issue #1920 PR #2000
    REQUEST_CHANGES Blocker 5).
    """
    proc = subprocess.Popen(
        command,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_len = 0
    stderr_len = 0
    output_limit_exceeded = False
    timed_out = False

    sel = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    open_streams = 2
    deadline = time.monotonic() + max(timeout_seconds, 0.0)

    try:
        while open_streams > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = sel.select(timeout=min(remaining, 0.5))
            for key, _mask in events:
                data = key.fileobj.read(_READ_CHUNK_BYTES)  # type: ignore[union-attr]
                if not data:
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                if key.data == "stdout":
                    if stdout_len < stdout_cap_bytes:
                        stdout_chunks.append(data[: stdout_cap_bytes - stdout_len])
                    stdout_len += len(data)
                    if stdout_len > stdout_cap_bytes:
                        output_limit_exceeded = True
                else:
                    if stderr_len < stderr_cap_bytes:
                        stderr_chunks.append(data[: stderr_cap_bytes - stderr_len])
                    stderr_len += len(data)
                    if stderr_len > stderr_cap_bytes:
                        output_limit_exceeded = True
            if output_limit_exceeded:
                break
    finally:
        sel.close()

    if timed_out or output_limit_exceeded:
        _kill_process_group(proc.pid)
        try:
            proc.wait(timeout=_GRACE_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid, force=True)
            try:
                proc.wait(timeout=_GRACE_KILL_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass

    exit_code: int | None
    try:
        exit_code = proc.wait(timeout=_GRACE_KILL_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        exit_code = None

    if timed_out:
        exit_code = None

    # Drain any final buffered bytes the child wrote between the last select
    # loop iteration and process exit (best-effort, still capped).
    try:
        if proc.stdout is not None:
            tail = proc.stdout.read()
            if tail:
                if stdout_len < stdout_cap_bytes:
                    stdout_chunks.append(tail[: stdout_cap_bytes - stdout_len])
                stdout_len += len(tail)
                if stdout_len > stdout_cap_bytes:
                    output_limit_exceeded = True
    except (ValueError, OSError):  # pragma: no cover - pipe already closed
        pass
    try:
        if proc.stderr is not None:
            tail = proc.stderr.read()
            if tail:
                if stderr_len < stderr_cap_bytes:
                    stderr_chunks.append(tail[: stderr_cap_bytes - stderr_len])
                stderr_len += len(tail)
                if stderr_len > stderr_cap_bytes:
                    output_limit_exceeded = True
    except (ValueError, OSError):  # pragma: no cover - pipe already closed
        pass

    return b"".join(stdout_chunks), b"".join(stderr_chunks), exit_code, timed_out, output_limit_exceeded


def execute_operation(
    operation: str,
    params: Mapping[str, Any],
    *,
    gh_token: str,
    gh_bin: str = "gh",
    host: str = DEFAULT_HOST,
    repo: str = DEFAULT_REPO,
    timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
    stderr_cap_bytes: int = DEFAULT_STDERR_CAP_BYTES,
    cwd: Path | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Validate then execute exactly one broker operation. Raises BrokerDenied on deny.

    Never returns raw, unredacted output; never includes `gh_token` in the
    returned record. `deadline` (a `time.monotonic()` absolute value) bounds
    the *route's* remaining time budget; the command's own timeout is
    `min(timeout_seconds, deadline - now)` when `deadline` is given (Issue
    #1920 PR #2000 REQUEST_CHANGES Blocker 5).
    """
    validation = validate_operation(operation, params)
    if not validation.allowed:
        raise BrokerDenied(validation.reason)

    argv = build_argv(operation, params, host=host, repo=repo)
    effective_timeout = float(timeout_seconds)
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerDenied("route_deadline_exceeded")
        effective_timeout = min(effective_timeout, remaining)

    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agy-github-research-broker-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        gh_config_dir = _isolated_gh_config_dir(tmp_root)
        env = _minimal_broker_env(gh_token=gh_token, host=host, repo=repo, gh_config_dir=gh_config_dir)
        command = [gh_bin, *argv]
        stdout_bytes, stderr_bytes, exit_code, timed_out, output_limit_exceeded = _run_streaming_bounded(
            command,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            timeout_seconds=effective_timeout,
            stdout_cap_bytes=stdout_cap_bytes,
            stderr_cap_bytes=stderr_cap_bytes,
        )
    duration_ms = int((time.monotonic() - start) * 1000)

    stdout_raw = stdout_bytes.decode("utf-8", errors="replace")
    stderr_raw = stderr_bytes.decode("utf-8", errors="replace")

    redacted_stdout, stdout_truncated = _bounded_redacted(stdout_raw, stdout_cap_bytes)
    redacted_stderr, stderr_truncated = _bounded_redacted(stderr_raw, stderr_cap_bytes)
    truncated = stdout_truncated or stderr_truncated or output_limit_exceeded
    combined_digest = _digest(redacted_stdout + "\n" + redacted_stderr)

    return {
        "schema": SCHEMA_COMMAND_RESULT,
        "operation": operation,
        "argv": argv,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "output_limit_exceeded": output_limit_exceeded,
        "redacted_stdout_sample": redacted_stdout,
        "redacted_stderr_sample": redacted_stderr,
        "redacted_output_digest": combined_digest,
    }


def _cli_argv() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    validate_parser = sub.add_parser("validate", help="Pre-execution validate an operation (no execution).")
    validate_parser.add_argument("operation", help="Broker operation id, e.g. get_issue")
    validate_parser.add_argument("--params-json", default="{}", help="JSON object of operation params.")

    execute_parser = sub.add_parser("execute", help="Validate then execute one broker operation.")
    execute_parser.add_argument("operation", help="Broker operation id, e.g. get_issue")
    execute_parser.add_argument("--params-json", default="{}", help="JSON object of operation params.")
    execute_parser.add_argument(
        "--gh-token-env",
        default="GH_TOKEN",
        help="Env var holding the token, checked first (default GH_TOKEN). If unset, this "
        "process bootstraps a stored `gh` credential internally via `gh auth token`.",
    )
    execute_parser.add_argument("--host", default=DEFAULT_HOST)
    execute_parser.add_argument("--repo", default=DEFAULT_REPO)
    execute_parser.add_argument("--gh-bin", default="gh", help="`gh` executable to use (default gh).")
    execute_parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_COMMAND_TIMEOUT_SECONDS)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _cli_argv()
    args = parser.parse_args(argv)

    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError:
        print(json.dumps({"ok": False, "reason": "params_json_malformed"}, sort_keys=True))
        return 2

    if args.mode == "validate":
        result = validate_operation(args.operation, params)
        print(
            json.dumps(
                {"allowed": result.allowed, "reason": result.reason, "probe_class": result.probe_class},
                sort_keys=True,
            )
        )
        return 0 if result.allowed else 2

    if args.mode == "execute":
        gh_token, bootstrap_failure_reason, bootstrap_used = resolve_gh_token(
            gh_token_env=args.gh_token_env, host=args.host, gh_bin=args.gh_bin
        )
        if not gh_token:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": bootstrap_failure_reason or "gh_token_env_missing",
                        "bootstrap_used": bootstrap_used,
                    },
                    sort_keys=True,
                )
            )
            return 5
        try:
            record = execute_operation(
                args.operation,
                params,
                gh_token=gh_token,
                gh_bin=args.gh_bin,
                host=args.host,
                repo=args.repo,
                timeout_seconds=args.timeout_seconds,
            )
        except BrokerDenied as exc:
            print(json.dumps({"ok": False, "reason": str(exc)}, sort_keys=True))
            return 2
        print(json.dumps(record, sort_keys=True))
        if record.get("timed_out"):
            return 5
        if record.get("output_limit_exceeded"):
            return 4
        if record.get("exit_code") not in (0, None):
            return 3
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
