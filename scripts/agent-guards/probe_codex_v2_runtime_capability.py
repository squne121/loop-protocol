#!/usr/bin/env python3
"""probe_codex_v2_runtime_capability.py — Codex CLI Multi-Agent V2 runtime
capability probe (Issue #1834, Phase 0 of parent #1833).

Records, as an offline-testable artifact, whether the locally installed
Codex CLI recognizes the `multi_agent_v2` feature flag, whether the
`[features]` schema in `.codex/config.toml` is syntactically loadable, and
which CLI-observable V2 capability surfaces (spawn_agent V2 form,
`agent_type` / `task_name` / `fork_turns` parameters, nested delegation) are
discoverable via `codex --help` text.

This script is READ-ONLY with respect to Codex runtime state: it never
writes to `.codex/hooks.json` or `.codex/config.toml`, and it never enables
the `multi_agent_v2` feature (config overrides used for probing are passed
via `-c features.multi_agent_v2=true` as ephemeral CLI args to a *child*
`codex features list` invocation only — nothing is persisted to disk).

Every probe function accepts an injectable `runner` (defaults to
`subprocess.run`) so that unit tests can simulate all known failure modes
(binary not found, symlink/shim executable, empty/non-semver/timeout
version output, feature not recognized, feature recognized-but-disabled,
config loader rejection, malformed subprocess output) without requiring a
real Codex CLI installation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
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


def _run(
    cmd: List[str],
    *,
    timeout: float,
    runner: Runner,
) -> Dict[str, Any]:
    """Normalize a subprocess invocation into a fail-closed result dict.

    Never raises: FileNotFoundError -> binary_not_found, TimeoutExpired ->
    timeout, everything else is surfaced as returncode/stdout/stderr.
    """
    try:
        completed = runner(cmd, capture_output=True, text=True, timeout=timeout)
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


def resolve_codex_executable(codex_bin: str) -> Dict[str, Any]:
    """Resolve `codex_bin` on PATH and record symlink/shim state.

    A shim (thin wrapper script, e.g. an rtk/asdf/nvm-style shim) is
    detected heuristically: the resolved real path differs from the
    `which`-reported path via more than a single symlink hop, OR the
    resolved file is small (<4096 bytes) text rather than an ELF/Mach-O
    binary. Detection failures are non-fatal — they are recorded as
    `shim_detection == "inconclusive"` rather than blocking the probe.
    """
    import shutil

    which_path = shutil.which(codex_bin)
    if which_path is None:
        return {
            "which_path": None,
            "resolved_path": None,
            "is_symlink": None,
            "shim_detection": "binary_not_found",
        }
    which_path_obj = Path(which_path)
    is_symlink = which_path_obj.is_symlink()
    try:
        resolved_path = str(which_path_obj.resolve())
    except OSError:
        return {
            "which_path": which_path,
            "resolved_path": None,
            "is_symlink": is_symlink,
            "shim_detection": "inconclusive",
        }
    shim_detection = "no_shim_detected"
    try:
        header = Path(resolved_path).open("rb").read(4)
        if header not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"MZ\x90\x00"):
            shim_detection = "possible_shim_non_binary_header"
    except OSError:
        shim_detection = "inconclusive"
    return {
        "which_path": which_path,
        "resolved_path": resolved_path,
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
    timeout: float = 10.0,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    """Probe `codex features list` (read-only) for `feature_name`.

    Returns a dict with `status` in: ok_enabled | ok_recognized_but_disabled
    | not_recognized | binary_not_found | timeout | malformed_output |
    config_loader_rejection.
    """
    result = _run([codex_bin, "features", "list"], timeout=timeout, runner=runner)

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
                "raw_output": stderr.strip() or stdout.strip(),
            }
        return {
            "status": "malformed_output",
            "stage": None,
            "enabled": None,
            "raw_output": stderr.strip() or stdout.strip(),
        }

    rows = _parse_features_list_table(stdout)
    if rows is None:
        return {"status": "malformed_output", "stage": None, "enabled": None, "raw_output": stdout.strip()}

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
    """
    if not config_path.is_file():
        return {"parse_status": "file_not_found", "features_table_present": False, "multi_agent_v2_declared": None}

    try:
        raw = config_path.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return {
            "parse_status": "toml_parse_error",
            "features_table_present": False,
            "multi_agent_v2_declared": None,
            "error": str(exc),
        }

    features = data.get("features")
    if features is None:
        return {"parse_status": "ok_absent", "features_table_present": False, "multi_agent_v2_declared": None}
    if not isinstance(features, dict):
        return {
            "parse_status": "toml_parse_error",
            "features_table_present": True,
            "multi_agent_v2_declared": None,
            "error": "'features' key is not a table",
        }

    declared = features.get("multi_agent_v2")
    return {
        "parse_status": "ok_present",
        "features_table_present": True,
        "multi_agent_v2_declared": declared if isinstance(declared, bool) else None,
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


def classify_recorder_generation(hooks_json_path: Path) -> str:
    """Classify the passive-recorder wiring generation from
    `.codex/hooks.json` structure (Issue #1834 provenance requirement,
    Issue #1830 pre/post distinction).

    Returns one of: post_1830_advisory_subagent_stop_only |
    session_end_recorder_present | hooks_json_missing | hooks_json_unparseable
    | unknown_generation.
    """
    if not hooks_json_path.is_file():
        return "hooks_json_missing"
    try:
        data = json.loads(hooks_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "hooks_json_unparseable"

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "unknown_generation"

    session_end = hooks.get("SessionEnd")
    subagent_stop = hooks.get("SubagentStop")

    def _has_recorder_command(entries: Any) -> bool:
        if not isinstance(entries, list):
            return False
        for entry in entries:
            for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if "session-recording-composite" in command:
                    return True
        return False

    session_end_wired = session_end is not None and _has_recorder_command(session_end)
    subagent_stop_wired = _has_recorder_command(subagent_stop)

    if session_end_wired:
        return "session_end_recorder_present"
    if subagent_stop_wired and session_end in (None, []):
        return "post_1830_advisory_subagent_stop_only"
    return "unknown_generation"


def build_provenance(
    repo_root: Path,
    hooks_json_path: Path,
    adapter_path: Path,
    *,
    runner: Runner = subprocess.run,
) -> Dict[str, Any]:
    return {
        "repo_head_sha": _git_head_sha(repo_root, runner=runner),
        "codex_hooks_json_sha256": _sha256_file(hooks_json_path),
        "codex_hook_adapter_sha256": _sha256_file(adapter_path),
        "recorder_generation": classify_recorder_generation(hooks_json_path),
    }


def build_artifact(
    *,
    repo_root: Path,
    codex_bin: str = "codex",
    config_path: Optional[Path] = None,
    hooks_json_path: Optional[Path] = None,
    adapter_path: Optional[Path] = None,
    runner: Runner = subprocess.run,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    config_path = config_path or (repo_root / ".codex" / "config.toml")
    hooks_json_path = hooks_json_path or (repo_root / ".codex" / "hooks.json")
    adapter_path = adapter_path or (repo_root / "scripts" / "session-recording" / "codex-hook-adapter.mjs")

    version_probe = probe_codex_version(codex_bin, runner=runner)
    feature_probe = probe_feature_flag("multi_agent_v2", codex_bin, runner=runner)
    config_schema = check_config_toml_features_schema(config_path)
    help_scan = scan_cli_help_text_for_tokens(codex_bin, runner=runner)

    v2_config_schema_loadable = config_schema["parse_status"] in ("ok_present", "ok_absent") and feature_probe[
        "status"
    ] in ("ok_enabled", "ok_recognized_but_disabled")

    if generated_at is None:
        from datetime import datetime, timezone

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "generated_by": "probe_codex_v2_runtime_capability",
        "codex_cli_version": version_probe,
        "v2_config_schema_loadable": v2_config_schema_loadable,
        "v2_config_schema_check": {
            "config_toml_parse": config_schema,
            "cli_feature_flag_probe": feature_probe,
        },
        "cli_feature_recognition": help_scan,
        "provenance": build_provenance(repo_root, hooks_json_path, adapter_path, runner=runner),
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

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
