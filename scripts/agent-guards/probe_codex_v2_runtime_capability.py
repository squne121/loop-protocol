#!/usr/bin/env python3
"""probe_codex_v2_runtime_capability.py — Codex CLI Multi-Agent V2 runtime
capability probe (Issue #1834, Phase 0 of parent #1833).

Records, as an offline-testable artifact, whether the locally installed
Codex CLI recognizes the `multi_agent_v2` feature flag, whether an isolated
Codex CLI instance actually *accepts* a structured `[features.multi_agent_v2]`
config block (as opposed to merely parsing the surrounding TOML), and which
CLI-observable V2 capability surfaces (spawn_agent V2 form, `agent_type` /
`task_name` / `fork_turns` parameters, nested delegation) are discoverable
via `codex --help` text.

This script is READ-ONLY with respect to the repository's Codex runtime
state: it never writes to `.codex/hooks.json` or `.codex/config.toml`, and
it never enables the `multi_agent_v2` feature in the *ambient* environment.
The config-loader acceptance/rejection probe (`probe_config_loader`) spawns
Codex with an isolated, empty `CODEX_HOME` (created under a private
`tempfile.TemporaryDirectory`, discarded on exit) so that the ephemeral
`-c features.multi_agent_v2....=...` overrides used for probing are never
persisted to this repository's `.codex/config.toml` or to the invoking
user's real `~/.codex` state.

Privacy contract: this artifact must never contain absolute filesystem
paths, the invoking user's home directory, or the invoking user's username.
`resolve_codex_executable()` intentionally returns only a basename,
best-effort distribution/target-triple classification, and a content
digest of the resolved binary — never `which`/`resolve()` path strings.
Every subprocess raw_output field that is retained is passed through
`_sanitize_text()` before being stored. `find_privacy_violations()` is run
by `main()` before the artifact is written, and the write is refused
(non-zero exit) if a violation is found.

Every probe function accepts an injectable `runner` (defaults to
`subprocess.run`) so that unit tests can simulate all known failure modes
(binary not found, symlink/shim executable, empty/non-semver/timeout
version output, feature not recognized, feature recognized-but-disabled,
config loader rejection, malformed subprocess output) without requiring a
real Codex CLI installation.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]

SCHEMA = "CODEX_MULTI_AGENT_V2_RUNTIME_CAPABILITY_V1"

_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]

# CLI-observable V2 capability tokens. These are searched for in
# `codex --help` / `codex exec --help` text only (a *static* capability
# scan). None of these are exposed as top-level CLI flags today (they are
# internal agent tool-call schema, not CLI surface) — a `recognized: false`
# result for all of them is expected and is itself the evidence artifact
# for #1834's "CLI 機能認識調査".
_CLI_FEATURE_TOKENS: Dict[str, List[str]] = {
    "spawn_agent_v2_form": ["spawn_agent"],
    "agent_type_param": ["agent_type", "agent-type"],
    "task_name_param": ["task_name", "task-name"],
    "fork_turns_param": ["fork_turns", "fork-turns"],
    "nested_delegation": ["nested delegation", "nested_delegation", "nested-delegation"],
}

# Ephemeral -c overrides for the isolated config-loader acceptance /
# rejection probe (Issue #1834 review finding #3). Only ever passed as
# `-c` CLI args to a *child* `codex features list` invocation run against
# an isolated, throwaway `CODEX_HOME` — nothing here is persisted to disk.
_CONFIG_LOADER_PROBE_CASES: Dict[str, List[str]] = {
    "positive": [
        "-c",
        "features.multi_agent_v2.enabled=true",
        "-c",
        "features.multi_agent_v2.max_concurrent_threads_per_session=2",
    ],
    "unknown_key_rejected": [
        "-c",
        "features.multi_agent_v2.enabled=true",
        "-c",
        "features.multi_agent_v2.unknown_bogus_key=1",
    ],
    "wrong_type_rejected": [
        "-c",
        'features.multi_agent_v2.enabled="not_a_bool"',
    ],
    "zero_concurrency_rejected": [
        "-c",
        "features.multi_agent_v2.enabled=true",
        "-c",
        "features.multi_agent_v2.max_concurrent_threads_per_session=0",
    ],
}

# Expected acceptance outcome per case: True means "Codex should accept
# this config and exit 0", False means "Codex should reject this config
# and exit non-zero".
_CONFIG_LOADER_PROBE_EXPECTED_ACCEPTED: Dict[str, bool] = {
    "positive": True,
    "unknown_key_rejected": False,
    "wrong_type_rejected": False,
    "zero_concurrency_rejected": False,
}


def _current_username() -> Optional[str]:
    try:
        user = getpass.getuser()
        if user:
            return user
    except Exception:  # pragma: no cover - platform dependent
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or None


def _sanitize_text(text: Optional[str]) -> Optional[str]:
    """Redact absolute filesystem paths / usernames from subprocess text
    that is retained in the artifact (Issue #1834 review finding #1)."""
    if text is None:
        return None
    sanitized = text
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - platform dependent
        home = ""
    if home:
        sanitized = sanitized.replace(home, "<redacted-home>")
    user = _current_username()
    if user and len(user) >= 3:
        sanitized = re.sub(re.escape(user), "<redacted-user>", sanitized)
    sanitized = re.sub(r"/home/[^/\s]+", "/home/<redacted-user>", sanitized)
    sanitized = re.sub(r"/Users/[^/\s]+", "/Users/<redacted-user>", sanitized)
    sanitized = re.sub(r"[A-Za-z]:\\[^\s]*", "<redacted-windows-path>", sanitized)
    return sanitized


def find_privacy_violations(data: Any) -> List[str]:
    """Recursively scan an artifact-shaped structure for forbidden
    privacy-leaking substrings (absolute home paths, current username,
    Windows drive-letter absolute paths). Returns a list of
    `<json-path>: <reason>` violation descriptions; empty list means
    clean."""
    try:
        home = str(Path.home())
    except Exception:  # pragma: no cover - platform dependent
        home = ""
    user = _current_username()
    forbidden: List[str] = [p for p in (home,) if p]

    violations: List[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                _walk(value, f"{path}[{idx}]")
        elif isinstance(node, str):
            for pattern in forbidden:
                if pattern and pattern in node:
                    violations.append(f"{path}: contains forbidden home path substring")
            if user and len(user) >= 3 and user in node:
                violations.append(f"{path}: contains current username substring")
            if re.search(r"/home/[^/\s]+", node):
                violations.append(f"{path}: contains /home/<user> absolute path")
            if re.search(r"/Users/[^/\s]+", node):
                violations.append(f"{path}: contains /Users/<user> absolute path")
            if re.search(r"[A-Za-z]:\\[^\s]*", node):
                violations.append(f"{path}: contains Windows drive-letter absolute path")

    _walk(data, "$")
    return violations


def _run(
    cmd: List[str],
    *,
    timeout: float,
    runner: Runner,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Normalize a subprocess invocation into a fail-closed result dict.

    Never raises: FileNotFoundError -> binary_not_found, TimeoutExpired ->
    timeout, everything else is surfaced as returncode/stdout/stderr.
    """
    kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout}
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    try:
        completed = runner(cmd, **kwargs)
    except FileNotFoundError:
        return {"outcome": "binary_not_found", "returncode": None, "stdout": "", "stderr": ""}
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "returncode": None, "stdout": "", "stderr": ""}
    except OSError:
        return {"outcome": "binary_not_found", "returncode": None, "stdout": "", "stderr": ""}
    return {
        "outcome": "completed",
        "returncode": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }


_STANDALONE_RELEASE_RE = re.compile(r"packages/([^/]+)/releases/([^/]+)/bin/")
_RELEASE_VERSION_TRIPLE_RE = re.compile(r"^\d+\.\d+\.\d+-(.+)$")


def resolve_codex_executable(codex_bin: str) -> Dict[str, Any]:
    """Resolve `codex_bin` on PATH and record symlink/shim state and a
    best-effort distribution classification.

    Privacy contract (Issue #1834 review finding #1): this function never
    returns `which_path` / `resolved_path` (absolute filesystem paths,
    which embed the invoking user's home directory). It returns only
    `executable_basename`, `distribution_kind`, `target_triple`, and a
    content digest (`binary_sha256`) — none of which reveal the local
    filesystem layout or username.

    A shim (thin wrapper script, e.g. an rtk/asdf/nvm-style shim) is
    detected heuristically: the resolved file is small text rather than
    an ELF/Mach-O/PE binary. Detection failures are non-fatal — they are
    recorded as `shim_detection == "inconclusive"` rather than blocking
    the probe.
    """
    import shutil

    which_path = shutil.which(codex_bin)
    if which_path is None:
        return {
            "executable_basename": None,
            "distribution_kind": None,
            "target_triple": None,
            "binary_sha256": None,
            "is_symlink": None,
            "shim_detection": "binary_not_found",
        }
    which_path_obj = Path(which_path)
    is_symlink = which_path_obj.is_symlink()
    try:
        resolved_path = which_path_obj.resolve()
    except OSError:
        return {
            "executable_basename": which_path_obj.name,
            "distribution_kind": None,
            "target_triple": None,
            "binary_sha256": None,
            "is_symlink": is_symlink,
            "shim_detection": "inconclusive",
        }

    shim_detection = "no_shim_detected"
    binary_sha256: Optional[str] = None
    try:
        raw = resolved_path.read_bytes()
        header = raw[:4]
        if header not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"MZ\x90\x00"):
            shim_detection = "possible_shim_non_binary_header"
        binary_sha256 = hashlib.sha256(raw).hexdigest()
    except OSError:
        shim_detection = "inconclusive"

    resolved_str = str(resolved_path)
    distribution_kind: Optional[str] = None
    target_triple: Optional[str] = None
    match = _STANDALONE_RELEASE_RE.search(resolved_str)
    if match:
        distribution_kind = match.group(1)
        version_triple = match.group(2)
        triple_match = _RELEASE_VERSION_TRIPLE_RE.match(version_triple)
        target_triple = triple_match.group(1) if triple_match else None
    else:
        distribution_kind = "unknown"

    return {
        "executable_basename": resolved_path.name,
        "distribution_kind": distribution_kind,
        "target_triple": target_triple,
        "binary_sha256": binary_sha256,
        "is_symlink": is_symlink,
        "shim_detection": shim_detection,
    }


def probe_codex_version(
    codex_bin: str = "codex",
    *,
    timeout: float = 10.0,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Probe `codex --version`. Returns a dict with `status` in:
    ok | binary_not_found | timeout | empty_output | non_semver_output.
    """
    executable_info = resolve_codex_executable(codex_bin)
    result = _run([codex_bin, "--version"], timeout=timeout, runner=runner)

    if result["outcome"] == "binary_not_found":
        return {
            "status": "binary_not_found",
            "version": None,
            "raw_output": None,
            "executable": executable_info,
        }
    if result["outcome"] == "timeout":
        return {
            "status": "timeout",
            "version": None,
            "raw_output": None,
            "executable": executable_info,
        }

    raw_output = (result["stdout"] or "").strip()
    if result["returncode"] != 0 and not raw_output:
        raw_output = (result["stderr"] or "").strip()
    raw_output = _sanitize_text(raw_output)

    if not raw_output:
        return {
            "status": "empty_output",
            "version": None,
            "raw_output": raw_output,
            "executable": executable_info,
        }

    match = _SEMVER_RE.search(raw_output)
    if match is None:
        return {
            "status": "non_semver_output",
            "version": None,
            "raw_output": raw_output,
            "executable": executable_info,
        }

    return {
        "status": "ok",
        "version": match.group(0),
        "raw_output": raw_output,
        "executable": executable_info,
    }


def _parse_features_list_table(stdout: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """Parse `codex features list` plain-text table output into
    {name: {"stage": str, "enabled": bool}}. Returns None if the output
    does not look like the expected whitespace-delimited table at all
    (malformed/unparseable subprocess output)."""
    rows: Dict[str, Dict[str, Any]] = {}
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    matched_any = False
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        enabled_token = parts[-1]
        stage = " ".join(parts[1:-1])
        if enabled_token not in ("true", "false"):
            continue
        rows[name] = {"stage": stage, "enabled": enabled_token == "true"}
        matched_any = True
    if not matched_any:
        return None
    return rows


def probe_feature_flag(
    feature_name: str,
    codex_bin: str = "codex",
    *,
    repo_root: Optional[Path] = None,
    timeout: float = 10.0,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Probe `codex features list` (read-only, ambient config) for
    `feature_name`.

    `cwd=repo_root` is passed explicitly (Issue #1834 review finding #3)
    so this probe is not dependent on the invoking process's ambient
    working directory. A repo-local `default_permissions` override is
    passed as an ephemeral `-c` arg (never persisted) so that a
    project `.codex/config.toml` with multiple named `[permissions.*]`
    profiles but no top-level `default_permissions` key does not cause
    an unrelated config-validation error to be misclassified as a
    `multi_agent_v2` recognition failure.

    Returns a dict with `status` in: ok_enabled | ok_recognized_but_disabled
    | not_recognized | binary_not_found | timeout | malformed_output |
    config_loader_rejection.
    """
    cmd = [
        codex_bin,
        "-c",
        'default_permissions="loop-protocol-readonly"',
        "features",
        "list",
    ]
    result = _run(cmd, timeout=timeout, runner=runner, cwd=str(repo_root) if repo_root else None)

    if result["outcome"] == "binary_not_found":
        return {"status": "binary_not_found", "stage": None, "enabled": None, "raw_output": None}
    if result["outcome"] == "timeout":
        return {"status": "timeout", "stage": None, "enabled": None, "raw_output": None}

    stdout = result["stdout"] or ""
    stderr = result["stderr"] or ""

    if result["returncode"] != 0:
        # Nonzero exit with config-parse-flavored stderr is classified as a
        # config loader rejection (e.g. `.codex/config.toml` contains a
        # value the CLI's config loader refuses to load).
        lowered = stderr.lower()
        if "config" in lowered and ("parse" in lowered or "invalid" in lowered or "toml" in lowered):
            return {
                "status": "config_loader_rejection",
                "stage": None,
                "enabled": None,
                "raw_output": _sanitize_text(stderr.strip() or stdout.strip()),
            }
        return {
            "status": "malformed_output",
            "stage": None,
            "enabled": None,
            "raw_output": _sanitize_text(stderr.strip() or stdout.strip()),
        }

    rows = _parse_features_list_table(stdout)
    if rows is None:
        return {
            "status": "malformed_output",
            "stage": None,
            "enabled": None,
            "raw_output": _sanitize_text(stdout.strip()),
        }

    row = rows.get(feature_name)
    if row is None:
        return {"status": "not_recognized", "stage": None, "enabled": None, "raw_output": None}

    if row["enabled"]:
        return {"status": "ok_enabled", "stage": row["stage"], "enabled": True, "raw_output": None}
    return {"status": "ok_recognized_but_disabled", "stage": row["stage"], "enabled": False, "raw_output": None}


def check_config_toml_features_schema(config_path: Path) -> Dict[str, Any]:
    """Read-only syntactic check of the `[features]` table in
    `.codex/config.toml` (or an equivalent fixture path). Never mutates the
    file. Returns `parse_status` in: ok_present | ok_absent |
    toml_parse_error | file_not_found.

    `multi_agent_v2_form` distinguishes the legacy boolean declaration
    (`multi_agent_v2 = true`) from the structured table form
    (`[features.multi_agent_v2]` with `enabled`/`max_concurrent_threads_per_session`
    etc, the form Issue #1835 introduces) so downstream consumers do not
    misread a structured-table declaration as "absent" (Issue #1834
    review finding #3).
    """
    if not config_path.is_file():
        return {
            "parse_status": "file_not_found",
            "features_table_present": False,
            "multi_agent_v2_declared": None,
            "multi_agent_v2_form": None,
        }

    try:
        raw = config_path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return {
            "parse_status": "toml_parse_error",
            "features_table_present": False,
            "multi_agent_v2_declared": None,
            "multi_agent_v2_form": None,
            "error": str(exc),
        }

    features = data.get("features")
    if features is None:
        return {
            "parse_status": "ok_absent",
            "features_table_present": False,
            "multi_agent_v2_declared": None,
            "multi_agent_v2_form": "absent",
        }
    if not isinstance(features, dict):
        return {
            "parse_status": "toml_parse_error",
            "features_table_present": True,
            "multi_agent_v2_declared": None,
            "multi_agent_v2_form": None,
            "error": "'features' key is not a table",
        }

    declared_raw = features.get("multi_agent_v2")
    if isinstance(declared_raw, bool):
        form = "bool_form"
        declared: Optional[bool] = declared_raw
    elif isinstance(declared_raw, dict):
        form = "table_form"
        declared = None
    elif declared_raw is None:
        form = "absent"
        declared = None
    else:
        form = "unknown_type"
        declared = None

    return {
        "parse_status": "ok_present",
        "features_table_present": True,
        "multi_agent_v2_declared": declared,
        "multi_agent_v2_form": form,
    }


def scan_cli_help_text_for_tokens(
    codex_bin: str = "codex",
    *,
    timeout: float = 10.0,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Static capability scan of `codex --help` and `codex exec --help`
    text for CLI-observable V2 capability tokens. This does NOT confirm
    actual agent-runtime tool schema availability -- only whether the
    tokens are documented as CLI surface."""
    combined_text = ""
    sources_ok = 0
    for cmd in ([codex_bin, "--help"], [codex_bin, "exec", "--help"]):
        result = _run(cmd, timeout=timeout, runner=runner)
        if result["outcome"] == "completed":
            combined_text += "\n" + (result["stdout"] or "") + "\n" + (result["stderr"] or "")
            sources_ok += 1

    if sources_ok == 0:
        return {"probed": False, "reason": "help_text_unavailable", "tokens": {}}

    lowered = combined_text.lower()
    tokens: Dict[str, bool] = {}
    for capability, aliases in _CLI_FEATURE_TOKENS.items():
        tokens[capability] = any(alias.lower() in lowered for alias in aliases)

    return {
        "probed": True,
        "reason": None,
        "method": "cli_help_text_scan",
        "note": (
            "Static substring scan of `codex --help` / `codex exec --help` output. "
            "spawn_agent / agent_type / task_name / fork_turns are internal agent "
            "tool-call schema, not top-level CLI flags, so `false` here is expected "
            "and does not by itself indicate the capability is unavailable to the "
            "running agent."
        ),
        "tokens": tokens,
    }


def probe_config_loader(
    codex_bin: str = "codex",
    repo_root: Optional[Path] = None,
    *,
    timeout: float = 20.0,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Isolated `CODEX_HOME` acceptance/rejection probe of the structured
    `[features.multi_agent_v2]` config block (Issue #1834 review finding
    #3): `v2_config_schema_loadable` in the prior implementation only
    checked that the surrounding TOML parsed and that `codex features
    list` recognized the flag name in the *ambient* environment — it never
    verified that Codex itself accepts the exact structured config block
    Issue #1835 introduces, nor did it reject invalid variants of that
    block.

    A fresh, empty `CODEX_HOME` is created under a private
    `tempfile.TemporaryDirectory` (discarded on exit) so these ephemeral
    `-c` overrides never touch the invoking user's real `~/.codex` state
    or this repository's committed `.codex/config.toml`. `cwd=repo_root`
    is passed explicitly on every case invocation.

    Cases probed:
      - positive: enabled=true, max_concurrent_threads_per_session=2
        (expected: accepted, exit 0)
      - unknown_key_rejected: an undeclared config key (expected: rejected)
      - wrong_type_rejected: `enabled` given a string instead of a bool
        (expected: rejected)
      - zero_concurrency_rejected: max_concurrent_threads_per_session=0
        (expected: rejected, below the documented minimum of 1)

    Returns `status` in: ok | unexpected_result | binary_not_found | timeout.
    `status: ok` means the positive case was accepted AND all three
    negative cases were rejected — the only condition under which
    `v2_config_schema_loadable` may be set to `True`.
    """
    cwd = str(repo_root) if repo_root else None
    case_results: Dict[str, Dict[str, Any]] = {}

    with tempfile.TemporaryDirectory(prefix="codex-config-loader-probe-") as td:
        codex_home = Path(td) / "codex_home"
        codex_home.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)

        for case_name, extra_args in _CONFIG_LOADER_PROBE_CASES.items():
            cmd = [codex_bin, *extra_args, "features", "list"]
            result = _run(cmd, timeout=timeout, runner=runner, cwd=cwd, env=env)
            if result["outcome"] == "binary_not_found":
                case_results[case_name] = {"outcome": "binary_not_found", "exit_code": None, "accepted": None}
                continue
            if result["outcome"] == "timeout":
                case_results[case_name] = {"outcome": "timeout", "exit_code": None, "accepted": None}
                continue
            exit_code = result["returncode"]
            case_results[case_name] = {
                "outcome": "completed",
                "exit_code": exit_code,
                "accepted": exit_code == 0,
            }

    any_binary_not_found = any(c["outcome"] == "binary_not_found" for c in case_results.values())
    any_timeout = any(c["outcome"] == "timeout" for c in case_results.values())

    if any_binary_not_found:
        status = "binary_not_found"
    elif any_timeout:
        status = "timeout"
    else:
        all_as_expected = all(
            case_results[name]["accepted"] == expected
            for name, expected in _CONFIG_LOADER_PROBE_EXPECTED_ACCEPTED.items()
        )
        status = "ok" if all_as_expected else "unexpected_result"

    return {
        "status": status,
        "positive_exit_code": case_results.get("positive", {}).get("exit_code"),
        "positive_accepted": case_results.get("positive", {}).get("accepted"),
        "unknown_key_rejected": case_results.get("unknown_key_rejected", {}).get("accepted") is False,
        "wrong_type_rejected": case_results.get("wrong_type_rejected", {}).get("accepted") is False,
        "zero_concurrency_rejected": case_results.get("zero_concurrency_rejected", {}).get("accepted") is False,
        "cases": case_results,
    }


def build_runtime_exec_probe() -> Dict[str, Any]:
    """Live `codex exec --json` V2 tool-surface canary (Issue #1834 review
    finding #4).

    This repository's controlled-execution constraints (read-only
    sandbox, no network, no `.codex/hooks.json` mutation, no new session
    state committed) mean a safe non-interactive canary cannot be executed
    from this probe script without introducing exactly the kind of
    uncontrolled runtime side effects Issue #1834's Stop Condition #3/#4
    call out (`runtime確認にwrite sandboxまたはnetworkが必要になる`,
    `.codex/hooks.json`を変更しないとlive payloadを取得できない`).

    Per the explicit carve-out in this Issue's follow-up instructions, the
    canary is intentionally **not run** in this revision. `status:
    not_run` is the accurate, non-misleading representation: config
    recognition (`v2_config_schema_loadable` / `config_loader_probe`) is
    proven by this artifact; actual runtime tool-surface availability
    (`spawn_agent` V2 form, `agent_type`/`task_name`/`fork_turns`, nested
    delegation) is NOT proven and must not be inferred from this artifact.
    A Herdr-driven interactive TUI canary was considered and is also
    skipped (documented as not implemented, per the Issue's explicit
    time-boxing note) — an independent live-canary Issue should perform
    both before any consumer treats V2 spawn as runtime-available.
    """
    return {
        "status": "not_run",
        "reason": (
            "Live spawn_agent V2 canary requires either a write-capable "
            "sandbox/network or a .codex/hooks.json change to observe hook "
            "input at the moment of tool invocation; both are out of scope "
            "for this read-only probe per Issue #1834's Stop Condition. "
            "config-recognition status (v2_config_schema_loadable) is proven "
            "independently of this field and must not be conflated with it."
        ),
        "herdr_tui_canary": "not_implemented_out_of_scope",
    }


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_head_sha(repo_root: Path, *, runner: Runner = subprocess.run) -> Optional[str]:
    result = _run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], timeout=10.0, runner=runner)
    if result["outcome"] != "completed" or result["returncode"] != 0:
        return None
    sha = (result["stdout"] or "").strip()
    return sha or None


def build_hook_wiring(hooks_json_path: Path) -> Dict[str, Any]:
    """Structured, event-keyed description of `.codex/hooks.json` wiring
    (Issue #1834 review finding #5). Replaces the prior single-enum
    `recorder_generation` classifier, which could only ever report one of
    `SessionEnd` / `SubagentStop` being wired and therefore could not
    represent the current reality of *both* being wired simultaneously.

    Returns `status` in: ok | hooks_json_missing | hooks_json_unparseable |
    unknown_structure. When `status == "ok"`, `SessionEnd` and
    `SubagentStop` are each `{present, recorder_command, timeout_seconds}`,
    and `unexpected_events` lists any top-level hook event names other
    than the two known passive-recorder events (so any accidental/future
    wiring of other events is visible in the artifact rather than silently
    dropped).
    """
    empty = {"present": False, "recorder_command": False, "timeout_seconds": None}

    if not hooks_json_path.is_file():
        return {"status": "hooks_json_missing", "SessionEnd": empty, "SubagentStop": empty, "unexpected_events": []}
    try:
        data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "status": "hooks_json_unparseable",
            "SessionEnd": empty,
            "SubagentStop": empty,
            "unexpected_events": [],
        }

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {
            "status": "unknown_structure",
            "SessionEnd": empty,
            "SubagentStop": empty,
            "unexpected_events": [],
        }

    def _describe(entries: Any) -> Dict[str, Any]:
        if not isinstance(entries, list) or not entries:
            return dict(empty)
        recorder_command = False
        timeout_seconds = None
        for entry in entries:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command", "")
                if "session-recording-composite" in command:
                    recorder_command = True
                    timeout_seconds = hook.get("timeout", timeout_seconds)
        return {"present": True, "recorder_command": recorder_command, "timeout_seconds": timeout_seconds}

    known_events = {"SessionEnd", "SubagentStop"}
    unexpected_events = sorted(set(hooks.keys()) - known_events)

    return {
        "status": "ok",
        "SessionEnd": _describe(hooks.get("SessionEnd")),
        "SubagentStop": _describe(hooks.get("SubagentStop")),
        "unexpected_events": unexpected_events,
    }


def build_provenance(
    repo_root: Path,
    hooks_json_path: Path,
    adapter_path: Path,
    config_path: Path,
    script_path: Path,
    *,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    return {
        "repo_head_sha": _git_head_sha(repo_root, runner=runner),
        "input_digest_set": {
            "config_toml_sha256": _sha256_file(config_path),
            "hooks_json_sha256": _sha256_file(hooks_json_path),
            "adapter_sha256": _sha256_file(adapter_path),
            "probe_script_sha256": _sha256_file(script_path),
        },
        "hook_wiring": build_hook_wiring(hooks_json_path),
    }


def build_artifact(
    *,
    repo_root: Path,
    codex_bin: str = "codex",
    config_path: Optional[Path] = None,
    hooks_json_path: Optional[Path] = None,
    adapter_path: Optional[Path] = None,
    script_path: Optional[Path] = None,
    runner: Runner = subprocess.run,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    config_path = config_path or (repo_root / ".codex" / "config.toml")
    hooks_json_path = hooks_json_path or (repo_root / ".codex" / "hooks.json")
    adapter_path = adapter_path or (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs")
    script_path = script_path or Path(__file__).resolve()

    version_probe = probe_codex_version(codex_bin, runner=runner)
    feature_probe = probe_feature_flag(
        "multi_agent_v2", codex_bin, repo_root=repo_root, runner=runner
    )
    config_schema = check_config_toml_features_schema(config_path)
    help_scan = scan_cli_help_text_for_tokens(codex_bin, runner=runner)
    config_loader_probe = probe_config_loader(codex_bin, repo_root, runner=runner)
    runtime_exec_probe = build_runtime_exec_probe()

    v2_config_schema_loadable = (
        config_schema["parse_status"] in ("ok_present", "ok_absent") and config_loader_probe["status"] == "ok"
    )

    mandatory_checks = {
        "codex_cli_version": version_probe["status"] == "ok",
        "config_toml_parse": config_schema["parse_status"] in ("ok_present", "ok_absent"),
        "config_loader_probe": config_loader_probe["status"] == "ok",
    }
    mandatory_failed = [name for name, ok in mandatory_checks.items() if not ok]

    if mandatory_failed:
        overall_status = "fail"
    elif runtime_exec_probe["status"] != "pass" or not help_scan.get("probed"):
        overall_status = "partial"
    else:
        overall_status = "pass"

    if generated_at is None:
        from datetime import datetime, timezone

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "generated_by": "probe_codex_v2_runtime_capability",
        "overall_status": overall_status,
        "mandatory_probe_failures": mandatory_failed,
        "codex_cli_version": version_probe,
        "v2_config_schema_loadable": v2_config_schema_loadable,
        "v2_config_schema_check": {
            "config_toml_parse": config_schema,
            "cli_feature_flag_probe": feature_probe,
            "config_loader_probe": config_loader_probe,
        },
        "cli_feature_recognition": help_scan,
        "runtime_exec_probe": runtime_exec_probe,
        "provenance": build_provenance(
            repo_root, hooks_json_path, adapter_path, config_path, script_path, runner=runner
        ),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=None, help="Repo root (default: git rev-parse --show-toplevel)"
    )
    parser.add_argument("--codex-bin", type=str, default="codex")
    parser.add_argument("--output", type=Path, default=None, help="Artifact output path")
    parser.add_argument("--config-path", type=Path, default=None)
    parser.add_argument("--hooks-json-path", type=Path, default=None)
    parser.add_argument("--adapter-path", type=Path, default=None)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit 0 even if a mandatory probe (config parse, config loader "
        "acceptance/rejection, binary version) failed. Without this flag, a "
        "mandatory probe failure is a non-zero exit (Issue #1834 review "
        "finding #6).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    repo_root = args.repo_root
    if repo_root is None:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print("ERROR: could not resolve repo root via git rev-parse", file=sys.stderr)
            return 1
        repo_root = Path(result.stdout.strip())

    output_path = args.output or (repo_root / "artifacts" / "codex-multi-agent-v2" / "runtime-capability.json")

    artifact = build_artifact(
        repo_root=repo_root,
        codex_bin=args.codex_bin,
        config_path=args.config_path,
        hooks_json_path=args.hooks_json_path,
        adapter_path=args.adapter_path,
    )

    violations = find_privacy_violations(artifact)
    if violations:
        print("ERROR: refusing to write artifact — privacy violations detected:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 3

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, output_path)
    print(f"wrote {output_path} (overall_status={artifact['overall_status']})")

    if artifact["overall_status"] == "fail" and not args.allow_partial:
        print(
            f"ERROR: mandatory probe(s) failed: {artifact['mandatory_probe_failures']} "
            "(pass --allow-partial to accept a partial/failed artifact anyway)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
