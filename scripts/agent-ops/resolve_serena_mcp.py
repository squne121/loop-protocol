#!/usr/bin/env python3
"""resolve_serena_mcp.py -- single Serena MCP resolver/launcher (Issue #2015
AC13, OWNER Scope Reframe 2026-08-09).

Resolution priority (first match wins):

1. Explicit override: ``SERENA_BIN`` (a direct executable path) or
   ``SERENA_MCP_COMMAND`` (a JSON array argv override, e.g.
   ``'["serena", "start-mcp-server", "--project-from-cwd"]'``).
2. An already-installed ``serena`` found on ``PATH``.
3. A user-local MANAGED install this resolver itself performs/maintains via
   ``uv tool install`` (persistent across invocations -- see
   ``_user_local_install_marker``).
4. The exact-pinned ``uvx`` fallback, using the pinned ref already recorded
   in ``references/serena-tool-manifest.json`` (never a moving-target
   ``uvx --from git+... serena`` without a pin).

This script sits in FRONT of an MCP JSON-RPC stdio channel when launched as
the actual MCP server command: stdout is reserved entirely for the resolved
subprocess's own stdio once exec'd. All resolver diagnostics go to stderr
only, and are bounded/redacted (never a raw absolute home-directory path or
long token, mirroring ``_redact`` in
``run_worktree_agent_runtime_smoke.py``).

No machine-specific absolute path is ever committed to the repository by
this script -- ``--report`` prints resolution metadata to stdout as JSON for
CALLER inspection (never written back into a tracked file).

Live installation verification (``uv tool install`` / ``command -v serena``
actually succeeding end-to-end against the real network) is environment-
dependent: this script implements the resolution/fallback LOGIC correctly
and is exercised by hermetic tests (mocked ``shutil.which`` / subprocess),
but genuine live install/fetch verification in a network-restricted sandbox
is deferred to control-plane's live validation pass (see PR #2044 root-cause
report).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "gemini-cli-headless-delegation"
    / "references"
    / "serena-tool-manifest.json"
)

SERENA_BIN_ENV = "SERENA_BIN"
SERENA_MCP_COMMAND_ENV = "SERENA_MCP_COMMAND"
DEFAULT_TOOL_TIMEOUT_SEC = 45

RESOLUTION_SOURCE_ENV_BIN = "env_serena_bin"
RESOLUTION_SOURCE_ENV_COMMAND = "env_serena_mcp_command"
RESOLUTION_SOURCE_PATH = "path_installed"
RESOLUTION_SOURCE_USER_LOCAL = "user_local_managed_install"
RESOLUTION_SOURCE_UVX_PINNED = "uvx_pinned_fallback"

# Absolute path / long-base64-token redaction (mirrors
# run_worktree_agent_runtime_smoke.py's ``_SECRET_LIKE_RE``) -- resolver
# stderr diagnostics never leak a raw home-directory path or long token.
_SECRET_LIKE_RE = re.compile(
    r"(/(?:home|root|Users)/[^\s\"']+)|"
    r"([A-Za-z0-9+/]{40,}=*)"
)


def _redact(text: str) -> str:
    return _SECRET_LIKE_RE.sub("<redacted>", text)


def _eprint(message: str) -> None:
    print(_redact(message), file=sys.stderr)


def load_manifest(manifest_path: Path = MANIFEST_PATH) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _resolved_executable_version(executable: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=15, check=False,
        )
    except OSError:
        return None
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0].strip() if text else None


def _user_local_install_marker() -> Path:
    """The marker this resolver uses to recognize a PERSISTENT user-local
    install it (or a prior invocation) already performed, distinct from an
    ambient PATH install a human/other tool may have set up (source #2:
    ``RESOLUTION_SOURCE_PATH`` already covers that case before this is ever
    consulted)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "loop-protocol" / "serena-mcp" / "resolved-bin"


def _resolve_path_installed() -> dict | None:
    found = shutil.which("serena")
    if not found:
        return None
    return {
        "source": RESOLUTION_SOURCE_PATH,
        "argv": [found, "start-mcp-server", "--project-from-cwd"],
        "executable": found,
    }


def _resolve_user_local_managed() -> dict | None:
    marker = _user_local_install_marker()
    if not marker.is_file():
        return None
    executable = marker.read_text(encoding="utf-8").strip()
    if not executable or not Path(executable).is_file():
        return None
    return {
        "source": RESOLUTION_SOURCE_USER_LOCAL,
        "argv": [executable, "start-mcp-server", "--project-from-cwd"],
        "executable": executable,
    }


def _resolve_uvx_pinned(manifest: dict) -> dict:
    mcp_command = manifest.get("mcp_command")
    if not isinstance(mcp_command, list) or not mcp_command:
        raise ValueError("serena-tool-manifest.json is missing a well-formed mcp_command")
    argv = [str(a) for a in mcp_command]
    pinned_ref = manifest.get("pinned_ref")
    if not isinstance(pinned_ref, str) or not pinned_ref:
        raise ValueError("serena-tool-manifest.json is missing pinned_ref")
    if pinned_ref not in " ".join(argv):
        raise ValueError(
            "serena-tool-manifest.json mcp_command does not embed the manifest's own "
            "pinned_ref -- refusing an unpinned/drifted uvx fallback"
        )
    return {
        "source": RESOLUTION_SOURCE_UVX_PINNED,
        "argv": argv,
        "executable": argv[0],
        "pinned_ref": pinned_ref,
    }


def resolve(
    *,
    manifest_path: Path = MANIFEST_PATH,
    tool_timeout_sec: int = DEFAULT_TOOL_TIMEOUT_SEC,
    env: dict[str, str] | None = None,
) -> dict:
    """Resolve the Serena MCP launch command, in priority order. Returns a
    machine-readable resolution record (never printed to stdout when this
    module is used as an actual MCP launcher -- see ``main`` --
    ``--report`` mode prints it explicitly for caller inspection only)."""
    env = os.environ if env is None else env
    manifest = load_manifest(manifest_path)

    resolution: dict | None = None
    fallback_used = False
    for resolver in (
        lambda: _resolve_env_override_with(env),
        _resolve_path_installed,
        _resolve_user_local_managed,
    ):
        candidate = resolver()
        if candidate is not None:
            resolution = candidate
            break
    if resolution is None:
        resolution = _resolve_uvx_pinned(manifest)
        fallback_used = True

    argv = list(resolution["argv"])
    if "--tool-timeout" not in argv:
        argv = [*argv, "--tool-timeout", str(int(tool_timeout_sec))]
    if "--project-from-cwd" not in argv:
        argv = [*argv, "--project-from-cwd"]

    uv_cache_dir = env.get("UV_CACHE_DIR")
    executable = resolution["executable"]
    version = (
        _resolved_executable_version(executable)
        if Path(executable).exists() or shutil.which(executable)
        else None
    )
    sha256 = _sha256_file(Path(executable)) if Path(executable).is_file() else None

    return {
        "schema": "resolve_serena_mcp_v1",
        "resolution_source": resolution["source"],
        "executable": executable,
        "version": version,
        "sha256": sha256,
        "pinned_ref": resolution.get("pinned_ref"),
        "effective_argv": argv,
        "uv_cache_dir": uv_cache_dir,
        "fallback_used": fallback_used,
        # This resolver itself never performs a network fetch as part of
        # RESOLUTION (only the resolved ``uvx``/``uv tool install`` COMMAND,
        # once actually exec'd by the caller, may reach the network) -- so
        # "was a network fetch observed" is honestly unobservable from
        # resolution alone and is always False here, never guessed.
        "network_fetch_observed": False,
    }


def _resolve_env_override_with(env: dict[str, str]) -> dict | None:
    explicit_bin = env.get(SERENA_BIN_ENV)
    if explicit_bin:
        return {
            "source": RESOLUTION_SOURCE_ENV_BIN,
            "argv": [explicit_bin, "start-mcp-server", "--project-from-cwd"],
            "executable": explicit_bin,
        }
    explicit_command = env.get(SERENA_MCP_COMMAND_ENV)
    if explicit_command:
        try:
            argv = json.loads(explicit_command)
        except (json.JSONDecodeError, ValueError):
            _eprint(f"{SERENA_MCP_COMMAND_ENV} is not valid JSON array text; ignoring override")
            return None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            _eprint(f"{SERENA_MCP_COMMAND_ENV} must be a non-empty JSON array of strings; ignoring")
            return None
        return {
            "source": RESOLUTION_SOURCE_ENV_COMMAND,
            "argv": [str(a) for a in argv],
            "executable": argv[0],
        }
    return None


def install_user_local(manifest_path: Path = MANIFEST_PATH) -> dict:
    """Best-effort ``uv tool install`` of the pinned Serena ref into a
    user-local managed location, then records the resulting executable path
    in ``_user_local_install_marker()`` for future resolutions to prefer
    over the uvx fallback (priority 3).

    Network-dependent: in a network-restricted sandbox this legitimately
    fails, in which case the resolver simply continues to fall back to the
    exact-pinned uvx invocation (priority 4) on every subsequent call --
    never a silent partial/corrupt install."""
    manifest = load_manifest(manifest_path)
    pinned_ref = manifest["pinned_ref"]
    spec = f"git+https://github.com/oraios/serena@{pinned_ref}"
    try:
        result = subprocess.run(
            ["uv", "tool", "install", "--from", spec, "serena"],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except OSError as exc:
        return {"ok": False, "error": _redact(str(exc))}
    if result.returncode != 0:
        return {"ok": False, "error": _redact(result.stderr[-2000:])}
    executable = shutil.which("serena")
    if not executable:
        return {"ok": False, "error": "uv tool install reported success but serena not found on PATH"}
    marker = _user_local_install_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(executable, encoding="utf-8")
    return {"ok": True, "executable": executable, "pinned_ref": pinned_ref}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", action="store_true",
        help="print the resolution record as JSON to stdout and exit (never used as the actual MCP launcher)",
    )
    parser.add_argument(
        "--install-user-local", action="store_true",
        help="attempt a persistent user-local install of the pinned Serena ref (network-dependent)",
    )
    parser.add_argument("--tool-timeout", type=int, default=DEFAULT_TOOL_TIMEOUT_SEC)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.install_user_local:
        result = install_user_local()
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1

    resolution = resolve(tool_timeout_sec=args.tool_timeout)

    if args.report:
        print(json.dumps(resolution, indent=2, ensure_ascii=False))
        return 0

    _eprint(
        f"resolve_serena_mcp: source={resolution['resolution_source']} "
        f"fallback_used={resolution['fallback_used']}"
    )
    # As the actual MCP launcher: replace this process with the resolved
    # command so stdout becomes the pure MCP JSON-RPC stdio channel (no
    # resolver output interleaved on stdout).
    try:
        os.execvp(resolution["effective_argv"][0], resolution["effective_argv"])
    except OSError as exc:
        _eprint(f"resolve_serena_mcp: failed to exec resolved command: {exc}")
        return 1
    return 0  # unreachable after a successful execvp


if __name__ == "__main__":
    sys.exit(main())
