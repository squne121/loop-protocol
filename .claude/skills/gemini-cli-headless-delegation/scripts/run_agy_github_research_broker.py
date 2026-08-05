#!/usr/bin/env python3
"""Token-owning broker for the AGY `github_research` read-only route (Issue #1920).

This module is the *only* component that ever sees `GH_TOKEN`. The AGY
process itself never receives the token: `run_agy_github_research_e2e.py`
invokes AGY with the `github_research` tool_profile, whose permission policy
(`agy_permission_policy.PROFILE_ALLOWED_TOOLS["github_research"]`) is empty,
so AGY has no native tool-call surface at all and can only respond with plain
text. This broker parses that text (in the e2e orchestrator) for a single
next-command directive, validates it against a semantic allowlist *before*
any subprocess is spawned, and -- only if allowed -- executes exactly one
`gh` invocation with `shell=False`, a repository/host binding forced via
`--repo`, an isolated `GH_CONFIG_DIR`, bounded timeout/output, and
redaction-before-truncate.

Out of scope (Issue #1920 Out of Scope): any mutating `gh` subcommand,
`gh auth`, `gh alias`, `gh extension`, arbitrary shell, `gh api graphql`, and
non-GET `gh api`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_COMMAND_RESULT = "agy_github_research_broker_command_result/v1"

DEFAULT_HOST = "github.com"
DEFAULT_REPO = "squne121/loop-protocol"

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
DEFAULT_STDOUT_CAP_BYTES = 65536
DEFAULT_STDERR_CAP_BYTES = 16384

# Semantic allowlist: (subcommand, sub-subcommand-or-None). `gh api` is
# handled separately below because its safety depends on the HTTP verb and
# endpoint shape, not just the argv prefix.
_ALLOWED_ARGV_PREFIXES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("issue", "view"),
        ("issue", "list"),
        ("pr", "view"),
        ("pr", "list"),
        ("pr", "diff"),
        ("pr", "checks"),
        ("repo", "view"),
        ("search", "issues"),
        ("search", "prs"),
        ("search", "repos"),
        ("release", "list"),
        ("release", "view"),
    }
)

# Explicitly denied top-level/second-level tokens -- mutation, credential
# surface, auth/alias/extension management. Checked *before* the allowlist so
# a denial reason is always specific.
_DENIED_ARGV_PREFIXES: frozenset[tuple[str, ...]] = frozenset(
    {
        ("issue", "create"),
        ("issue", "edit"),
        ("issue", "close"),
        ("issue", "reopen"),
        ("issue", "comment"),
        ("issue", "delete"),
        ("issue", "pin"),
        ("issue", "transfer"),
        ("issue", "lock"),
        ("pr", "create"),
        ("pr", "edit"),
        ("pr", "close"),
        ("pr", "reopen"),
        ("pr", "merge"),
        ("pr", "comment"),
        ("pr", "review"),
        ("pr", "ready"),
        ("pr", "lock"),
        ("repo", "create"),
        ("repo", "edit"),
        ("repo", "delete"),
        ("repo", "clone"),
        ("repo", "fork"),
        ("auth",),
        ("alias",),
        ("extension",),
        ("secret",),
        ("variable",),
        ("workflow", "run"),
        ("workflow", "disable"),
        ("workflow", "enable"),
        ("gist",),
        ("ssh-key",),
        ("config",),
        ("release", "create"),
        ("release", "delete"),
        ("release", "edit"),
        ("release", "upload"),
    }
)

# Tokens that always signal an attempted shell escape / compound command,
# regardless of position in argv. Checked against every element.
_COMPOUND_SHELL_TOKEN_RE = re.compile(r"[;&|`]|\$\(|\r|\n")

# Credential-display probes: any argv element that looks like an attempt to
# read/echo the token itself, or to invoke a shell builtin/env dump.
_CREDENTIAL_DISPLAY_PATTERNS: tuple[str, ...] = (
    "GH_TOKEN",
    "$GH_TOKEN",
    "env",
    "printenv",
    "auth token",
    "auth status",
)

# Token-shaped substrings redacted from output *before* truncation and
# *before* digesting, regardless of whether the real token is present
# verbatim (defense in depth against partial-token leakage from error text).
_TOKEN_SHAPE_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|Bearer\s+[A-Za-z0-9._-]{10,}",
)

_REDACTED_MARKER = "[REDACTED]"


class BrokerDenied(Exception):
    """Raised for a pre-execution deny; never spawns a subprocess."""


@dataclass(frozen=True)
class ValidationResult:
    allowed: bool
    reason: str
    probe_class: str | None = None


def _has_compound_shell_token(argv: list[str]) -> bool:
    return any(_COMPOUND_SHELL_TOKEN_RE.search(token) for token in argv)


def _looks_like_credential_display(argv: list[str]) -> bool:
    joined = " ".join(argv)
    lowered = joined.lower()
    return any(pattern.lower() in lowered for pattern in _CREDENTIAL_DISPLAY_PATTERNS)


def _has_cross_repository_or_host_override(argv: list[str]) -> bool:
    """Detect an attempt to point `-R`/`--repo` or a hostname at a different target."""
    for index, token in enumerate(argv):
        if token in ("-R", "--repo") and index + 1 < len(argv):
            value = argv[index + 1]
            if value != DEFAULT_REPO:
                return True
        if token in ("--hostname",) and index + 1 < len(argv):
            if argv[index + 1] != DEFAULT_HOST:
                return True
        if re.fullmatch(r"[a-z0-9.-]+\.(com|org|io|net|dev)", token, re.IGNORECASE) and "github.com" not in token:
            # Any other bare hostname-shaped token is treated as a possible
            # alternate-host attempt; conservative fail-closed.
            return True
    return False


def _validate_gh_api_argv(rest: list[str]) -> ValidationResult:
    """`gh api <endpoint>`: GET-only, no graphql, no method override."""
    if not rest:
        return ValidationResult(False, "gh_api_missing_endpoint")
    if rest[0] == "graphql":
        return ValidationResult(False, "gh_api_graphql_denied", probe_class="mutation")
    for token in rest:
        if token in ("-X", "--method"):
            return ValidationResult(False, "gh_api_method_override_denied", probe_class="mutation")
        if token.startswith("-X=") or token.startswith("--method="):
            return ValidationResult(False, "gh_api_method_override_denied", probe_class="mutation")
        if token in ("-f", "-F", "--input", "--raw-field"):
            return ValidationResult(False, "gh_api_write_field_denied", probe_class="mutation")
    endpoint = rest[0]
    if endpoint.startswith("-"):
        return ValidationResult(False, "gh_api_missing_endpoint")
    return ValidationResult(True, "gh_api_get_allowed")


def validate_gh_argv(argv: list[str]) -> ValidationResult:
    """Pre-execution validator for a single `gh` invocation (no leading `gh`).

    Fail-closed: any argv shape not explicitly recognized as read-only and
    repository-bound is denied. Never spawns a subprocess.
    """
    if not argv or not all(isinstance(token, str) for token in argv):
        return ValidationResult(False, "empty_or_malformed_argv")
    if _has_compound_shell_token(argv):
        return ValidationResult(False, "compound_shell_token_denied", probe_class="compound_shell")
    if _looks_like_credential_display(argv):
        return ValidationResult(False, "credential_display_denied", probe_class="credential_display")
    if _has_cross_repository_or_host_override(argv):
        return ValidationResult(False, "cross_repository_or_host_denied", probe_class="cross_repository")

    if argv[0] == "api":
        return _validate_gh_api_argv(argv[1:])

    prefix2 = tuple(argv[:2])
    prefix1 = tuple(argv[:1])
    if prefix2 in _DENIED_ARGV_PREFIXES or prefix1 in _DENIED_ARGV_PREFIXES:
        return ValidationResult(False, "denied_subcommand", probe_class="mutation")
    if prefix2 in _ALLOWED_ARGV_PREFIXES:
        return ValidationResult(True, "allowed_subcommand")
    return ValidationResult(False, "not_in_allowed_subcommand_list")


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


def _force_repo_binding(argv: list[str], *, host: str, repo: str) -> list[str]:
    """Force-inject `--repo host/repo` for subcommands that accept the flag.

    `gh issue` / `gh pr` accept `--repo` explicitly. `gh repo view` /
    `gh release` do not accept `--repo` (it takes a positional
    `[<repository>]` argument or relies on the ambient `GH_REPO`/`GH_HOST`
    env, both of which `execute_gh_command()` always sets); injecting
    `--repo` there would be rejected as `unknown flag`. `gh search` /
    `gh api` also do not take `--repo`. For all of these, repository/host
    binding is still enforced structurally: `validate_gh_argv()` denies any
    cross-repository/alternate-host token, and `GH_REPO`/`GH_HOST` env are
    always set by `execute_gh_command()`.
    """
    if argv and argv[0] in ("issue", "pr"):
        return [*argv, "--repo", f"{host}/{repo}"]
    return list(argv)


def execute_gh_command(
    argv: list[str],
    *,
    gh_token: str,
    host: str = DEFAULT_HOST,
    repo: str = DEFAULT_REPO,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    stdout_cap_bytes: int = DEFAULT_STDOUT_CAP_BYTES,
    stderr_cap_bytes: int = DEFAULT_STDERR_CAP_BYTES,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Validate then execute exactly one `gh` invocation. Raises BrokerDenied on deny.

    Never returns raw, unredacted output; never includes `gh_token` in the
    returned record.
    """
    validation = validate_gh_argv(argv)
    if not validation.allowed:
        raise BrokerDenied(validation.reason)

    bound_argv = _force_repo_binding(argv, host=host, repo=repo)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agy-github-research-broker-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        gh_config_dir = _isolated_gh_config_dir(tmp_root)
        env = _minimal_broker_env(gh_token=gh_token, host=host, repo=repo, gh_config_dir=gh_config_dir)
        command = ["gh", *bound_argv]
        output_limit_exceeded = False
        try:
            completed = subprocess.run(
                command,
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                shell=False,
                check=False,
                start_new_session=True,
            )
            exit_code: int | None = completed.returncode
            stdout_raw = completed.stdout or ""
            stderr_raw = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout_raw = (
                exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            )
            stderr_raw = (
                exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            )
        duration_ms = int((time.monotonic() - start) * 1000)

    if len(stdout_raw.encode("utf-8", errors="replace")) > stdout_cap_bytes:
        output_limit_exceeded = True
    if len(stderr_raw.encode("utf-8", errors="replace")) > stderr_cap_bytes:
        output_limit_exceeded = True

    redacted_stdout, stdout_truncated = _bounded_redacted(stdout_raw, stdout_cap_bytes)
    redacted_stderr, stderr_truncated = _bounded_redacted(stderr_raw, stderr_cap_bytes)
    truncated = stdout_truncated or stderr_truncated or output_limit_exceeded
    combined_digest = _digest(redacted_stdout + "\n" + redacted_stderr)

    return {
        "schema": SCHEMA_COMMAND_RESULT,
        "argv": bound_argv,
        "exit_code": exit_code,
        "timed_out": exit_code is None,
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

    validate_parser = sub.add_parser("validate", help="Pre-execution validate a gh argv (no execution).")
    validate_parser.add_argument("gh_argv", nargs="*", help="gh subcommand argv, e.g. issue view 1920")

    execute_parser = sub.add_parser("execute", help="Validate then execute one gh invocation.")
    execute_parser.add_argument("gh_argv", nargs="*", help="gh subcommand argv, e.g. issue view 1920")
    execute_parser.add_argument(
        "--gh-token-env", default="GH_TOKEN", help="Env var holding the token (default GH_TOKEN)."
    )
    execute_parser.add_argument("--host", default=DEFAULT_HOST)
    execute_parser.add_argument("--repo", default=DEFAULT_REPO)
    execute_parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_COMMAND_TIMEOUT_SECONDS)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _cli_argv()
    args = parser.parse_args(argv)

    if args.mode == "validate":
        result = validate_gh_argv(list(args.gh_argv))
        print(
            json.dumps(
                {"allowed": result.allowed, "reason": result.reason, "probe_class": result.probe_class},
                sort_keys=True,
            )
        )
        return 0 if result.allowed else 2

    if args.mode == "execute":
        gh_token = os.environ.get(args.gh_token_env)
        if not gh_token:
            print(json.dumps({"ok": False, "reason": "gh_token_env_missing"}, sort_keys=True))
            return 5
        try:
            record = execute_gh_command(
                list(args.gh_argv),
                gh_token=gh_token,
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
