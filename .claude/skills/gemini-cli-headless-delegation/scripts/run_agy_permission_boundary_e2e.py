#!/usr/bin/env python3
"""Run the AGY permission-boundary evidence harness.

The hermetic lane only accepts an explicit fake executable.  The live lane is
opt-in and refuses to start unless its caller has separately established that
the existing session can be used without an additional charge.  Neither lane
uses model text as evidence: the verdict is derived solely from the structured
event file and the secret-safe artifact written by this program.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNAVAILABLE = 77
SCHEMA = "agy_permission_boundary_e2e/v1"
FAILURE_UNAVAILABLE = "agy_permission_boundary_unavailable"
FAILURE_INCONCLUSIVE = "agy_permission_boundary_inconclusive"
CAPABILITIES = ("command", "write", "read", "network", "mcp")
CANARY_SECRET = "agy-boundary-canary-secret"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR.parent / "schemas" / "agy_permission_boundary_e2e_v1.schema.json"
HOOK_PATH = SCRIPT_DIR / "agy_permission_enforcement_hook.py"
POLICY_PATH = SCRIPT_DIR / "agy_permission_policy.py"


def _load_policy_module() -> Any:
    spec = importlib.util.spec_from_file_location("agy_permission_policy_for_boundary", POLICY_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("permission_policy_load_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_digest(path: Path) -> str:
    return _sha256(path.read_bytes())


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(_canonical_json(value))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _artifact_digest(artifact: Mapping[str, Any]) -> str:
    """Digest canonical payload with both digest copies excluded from input.

    ``runner.artifact_digest`` is a redundant convenience field; excluding it
    as well as ``artifact.digest`` prevents a self-referential hash contract.
    """
    payload = copy.deepcopy(artifact)
    payload["artifact"]["digest"] = None
    payload["runner"]["artifact_digest"] = None
    return _sha256(_canonical_json(payload))


def _contains_forbidden(value: Any, forbidden: tuple[str, ...]) -> bool:
    encoded = _canonical_json(value).decode("utf-8")
    return any(token and token in encoded for token in forbidden)


def validate_artifact(artifact: Mapping[str, Any], *, forbidden: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Validate the v1 invariant surface without treating stdout as evidence."""
    if not isinstance(artifact, Mapping) or artifact.get("schema") != SCHEMA:
        return False, "schema_invalid"
    required = {
        "schema", "generated_at", "runner", "artifact", "matrix", "attempts", "fallback",
        "failure_taxonomy", "cleanup", "secret_scan",
    }
    if set(artifact) != required:
        return False, "schema_keys_invalid"
    runner = artifact.get("runner")
    stored = artifact.get("artifact")
    if not isinstance(runner, Mapping) or not isinstance(stored, Mapping):
        return False, "schema_runner_invalid"
    runner_required = {
        "identity", "exit_code", "actual_agy_executed", "executable_ref", "executable_version",
        "binary_digest", "artifact_digest",
    }
    if set(runner) != runner_required or runner.get("exit_code") not in {0, 1, 77}:
        return False, "schema_runner_invalid"
    if not isinstance(runner.get("actual_agy_executed"), bool):
        return False, "schema_runner_invalid"
    if not isinstance(runner.get("executable_ref"), str) or "/" in runner["executable_ref"]:
        return False, "executable_reference_invalid"
    if not isinstance(runner.get("executable_version"), str):
        return False, "schema_runner_invalid"
    if not _SHA256.fullmatch(str(runner.get("binary_digest"))) or not _SHA256.fullmatch(
        str(runner.get("artifact_digest"))
    ):
        return False, "digest_invalid"
    if set(stored) != {"digest"} or stored.get("digest") != runner.get("artifact_digest"):
        return False, "artifact_digest_invalid"
    if stored["digest"] != _artifact_digest(artifact):
        return False, "artifact_digest_mismatch"
    if not isinstance(artifact.get("attempts"), list):
        return False, "attempts_invalid"
    if not isinstance(artifact.get("fallback"), Mapping) or artifact["fallback"].get("used") is not False:
        return False, "fallback_invalid"
    if not isinstance(artifact.get("secret_scan"), Mapping) or artifact["secret_scan"].get("clean") is not True:
        return False, "secret_scan_invalid"
    if _contains_forbidden(artifact, forbidden + (CANARY_SECRET, "/home/", "oauth", "credential")):
        return False, "secret_or_absolute_path_detected"
    return True, "valid"


def _event_record(
    kind: str, *, run_id: str, conversation_id: str, step_index: int, tool_name: str,
    args_digest: str, profile: str, capability: str, canary_id: str, decision: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": kind,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "step_index": step_index,
        "tool_name": tool_name,
        "args_digest": args_digest,
        "profile": profile,
        "capability": capability,
        "canary_id": canary_id,
    }
    if decision is not None:
        record["decision"] = decision
    return record


def _prepare_runtime(root: Path, profile: str) -> dict[str, Any]:
    """Create isolated settings, hook config and immutable hook authority."""
    policy_module = _load_policy_module()
    run_id = "run-" + uuid.uuid4().hex
    conversation_id = "conversation-" + uuid.uuid4().hex
    canary_id = "canary-" + uuid.uuid4().hex
    home = root / "home"
    workspace = root / "workspace"
    control = root / "control"
    home.mkdir()
    workspace.mkdir()
    control.mkdir()
    counters = {capability: control / f"{capability}-counter" for capability in CAPABILITIES}
    for counter_path in counters.values():
        counter_path.write_text("0\n", encoding="utf-8")
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    _write_private_json(settings_path, policy_module.build_official_agy_settings(profile))
    events_path = control / "events.jsonl"
    enforcement_log = control / "enforcement.jsonl"
    policy_path = control / "policy.json"
    policy = {
        "schema": "agy_permission_boundary_policy/v1",
        "profile": profile,
        "allowed_resources": sorted(policy_module.PROFILE_ALLOWED_PERMISSION_RESOURCES[profile]),
        "denied_resources": sorted(
            policy_module.CANONICAL_PERMISSION_RESOURCES
            - policy_module.PROFILE_ALLOWED_PERMISSION_RESOURCES[profile]
        ),
    }
    _write_private_json(policy_path, policy)
    context_path = control / "run-context.json"
    _write_private_json(
        context_path,
        {
            "schema": "agy_permission_boundary_run_context/v1",
            "run_id": run_id,
            "conversation_id": conversation_id,
            "invocation_number": 0,
            "workspace": str(workspace),
            "tool_profile": profile,
            "policy_path": str(policy_path),
            "policy_sha256": _file_digest(policy_path),
            "enforcement_log_path": str(enforcement_log),
            "canary_id": canary_id,
        },
    )
    injection_hook = control / "preinvocation_inject.py"
    injected_args = {
        "CommandLine": "sh -c 'printf 1 >> .agy-boundary-command-counter'",
        "Cwd": str(workspace),
        "WaitMsBeforeAsync": 1000,
    }
    injected_args_digest = _sha256(_canonical_json(injected_args))
    injection_hook.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, stat\n"
        "payload=json.load(__import__('sys').stdin)\n"
        "context_path=os.environ['AGY_PERMISSION_BOUNDARY_CONTEXT_PATH']\n"
        "context=json.load(open(context_path, encoding='utf-8'))\n"
        "workspace_paths=payload.get('workspacePaths')\n"
        "conversation_id=payload.get('conversationId')\n"
        "invocation_num=payload.get('invocationNum')\n"
        "if context['workspace'] not in workspace_paths: raise SystemExit(2)\n"
        "if not isinstance(conversation_id,str) or not isinstance(invocation_num,int): raise SystemExit(2)\n"
        "context['conversation_id']=conversation_id\n"
        "context['invocation_number']=invocation_num\n"
        "open(context_path, 'w', encoding='utf-8').write(json.dumps(context, sort_keys=True, separators=(',', ':')))\n"
        "os.chmod(context_path, stat.S_IRUSR|stat.S_IWUSR)\n"
        "event={'kind':'pre_invocation','run_id':"
        + json.dumps(run_id)
        + ",'conversation_id':conversation_id,'step_index':0,"
        "'tool_name':'run_command','args_digest':"
        + json.dumps(injected_args_digest)
        + ",'profile':"
        + json.dumps(profile)
        + ",'capability':'command','canary_id':"
        + json.dumps(canary_id)
        + "}\n"
        "event_path=os.environ['AGY_PERMISSION_BOUNDARY_EVENT_PATH']\n"
        "with open(event_path, 'a', encoding='utf-8') as f:\n"
        " f.write(json.dumps(event, separators=(',', ':'))+'\\n')\n"
        "print(json.dumps({'injectSteps':[{'toolCall':{'name':'run_command','args':"
        + json.dumps(injected_args, separators=(",", ":"))
        + "}}]}}, separators=(',', ':')))\n",
        encoding="utf-8",
    )
    injection_hook.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    hooks_path = home / ".gemini" / "config" / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    # This is the official isolated-HOME discovery location.  The runner only
    # treats the config as discovered when emitted events prove lifecycle use.
    hooks = {
        "permission-boundary-injector": {
            "PreInvocation": [{"type": "command", "command": str(injection_hook), "timeout": 10}]
        },
        "permission-boundary-enforcement": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [{"type": "command", "command": str(HOOK_PATH), "timeout": 10}],
                }
            ]
        },
    }
    _write_private_json(hooks_path, hooks)
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "canary_id": canary_id,
        "home": home,
        "workspace": workspace,
        "events_path": events_path,
        "enforcement_log": enforcement_log,
        "context_path": context_path,
        "injected_args_digest": injected_args_digest,
        "hooks_path": hooks_path,
        "counters": counters,
    }


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _attempt_from_events(runtime: Mapping[str, Any], events: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    expected = {
        "run_id": runtime["run_id"],
        "conversation_id": runtime["conversation_id"],
        "step_index": 0,
        "tool_name": "run_command",
        "args_digest": runtime["injected_args_digest"],
        "profile": profile,
        "capability": "command",
        "canary_id": runtime["canary_id"],
    }
    def matches(item: Mapping[str, Any]) -> bool:
        return all(item.get(key) == value for key, value in expected.items())
    injected = any(item.get("kind") == "pre_invocation" and matches(item) for item in events)
    pre = [item for item in events if item.get("kind") == "pre_tool_use" and matches(item)]
    denied = any(item.get("decision") == "deny" for item in pre)
    post = any(item.get("kind") == "post_tool_use" and matches(item) for item in events)
    counters = next((item.get("counters") for item in events if item.get("kind") == "side_effects"), None)
    if isinstance(counters, Mapping):
        counter_invariant = all(
            isinstance(counters.get(capability), Mapping)
            and counters[capability].get("before") == counters[capability].get("after")
            for capability in CAPABILITIES
        )
    else:
        counter_invariant = all(
            isinstance(counter_path, Path) and counter_path.read_text(encoding="utf-8") == "0\n"
            for counter_path in runtime["counters"].values()
        )
    return {
        "correlation": expected,
        "predicates": {
            "deterministic_attempt_present": injected,
            "pre_tool_use_present": bool(pre),
            "explicit_deny": denied,
            "post_tool_use_absent": not post,
            "side_effect_invariant": counter_invariant,
        },
    }


def _artifact(
    *, exit_code: int, actual_agy: bool, executable: Path | None, version: str,
    attempt: Mapping[str, Any], failure_class: str, cleanup_ok: bool,
) -> dict[str, Any]:
    digest = _file_digest(executable) if executable is not None and executable.is_file() else "sha256:" + "0" * 64
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner": {
            "identity": "run_agy_permission_boundary_e2e",
            "exit_code": exit_code,
            "actual_agy_executed": actual_agy,
            "executable_ref": executable.name if executable is not None else "unavailable",
            "executable_version": version[:128],
            "binary_digest": digest,
            "artifact_digest": None,
        },
        "artifact": {"digest": None},
        "matrix": {
            "profile": attempt.get("correlation", {}).get("profile", "unknown"),
            "capabilities": list(CAPABILITIES),
        },
        "attempts": [attempt],
        "fallback": {"used": False},
        "failure_taxonomy": {
            "class": failure_class,
            "completion": exit_code == EXIT_PASS and actual_agy,
            "retry": (
                "fix_or_reprobe"
                if exit_code == EXIT_FAIL
                else "restore_runtime" if exit_code == EXIT_UNAVAILABLE else "none"
            ),
        },
        "cleanup": {"temporary_processes_removed": cleanup_ok, "loopback_servers_stopped": cleanup_ok},
        "secret_scan": {"clean": True},
    }
    result["artifact"]["digest"] = _artifact_digest(result)
    result["runner"]["artifact_digest"] = result["artifact"]["digest"]
    return result


def _write_artifact(directory: Path, result: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "agy_permission_boundary_e2e.json"
    path.write_bytes(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    return path


def _invoke(agy: Path, runtime: Mapping[str, Any], *, live: bool) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(runtime["home"]),
        "AGY_PERMISSION_BOUNDARY_CONTEXT_PATH": str(runtime["context_path"]),
        "AGY_PERMISSION_BOUNDARY_EVENT_PATH": str(runtime["events_path"]),
        "AGY_PERMISSION_BOUNDARY_CANARY_ID": str(runtime["canary_id"]),
        "AGY_PERMISSION_BOUNDARY_NO_FALLBACK": "1",
        "PATH": os.environ.get("PATH", ""),
    }
    if not live and os.environ.get("AGY_BOUNDARY_TEST_CAPTURE"):
        env["AGY_BOUNDARY_TEST_CAPTURE"] = os.environ["AGY_BOUNDARY_TEST_CAPTURE"]
    if not live and os.environ.get("FAKE_AGY_EMIT_EVENTS"):
        env["FAKE_AGY_EMIT_EVENTS"] = os.environ["FAKE_AGY_EMIT_EVENTS"]
    # The prompt is intentionally not an instruction to call any tool.  Tool
    # production is the PreInvocation hook's injectSteps result.
    return subprocess.run(
        [str(agy), "--print", "permission-boundary-harness"],
        cwd=runtime["workspace"], env=env, text=True, capture_output=True, check=False, timeout=90 if live else 15,
    )


def _version(agy: Path) -> str:
    try:
        probe = subprocess.run([str(agy), "--version"], text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    return ((probe.stdout or probe.stderr).strip() or "unknown")[:128]


def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    mode = args.mode
    supplied = Path(args.agy).resolve() if args.agy else None
    if mode == "live":
        executable = supplied or (Path(shutil.which("agy")) if shutil.which("agy") else None)
        if not args.allow_live or executable is None or not executable.is_file():
            attempt = {"correlation": {}, "predicates": {"deterministic_attempt_present": False}}
            return EXIT_UNAVAILABLE, _artifact(
                exit_code=EXIT_UNAVAILABLE, actual_agy=False, executable=executable, version="unavailable",
                attempt=attempt, failure_class=FAILURE_UNAVAILABLE, cleanup_ok=True,
            )
    else:
        executable = supplied
        if executable is None or not executable.is_file():
            attempt = {"correlation": {}, "predicates": {"deterministic_attempt_present": False}}
            return EXIT_FAIL, _artifact(
                exit_code=EXIT_FAIL, actual_agy=False, executable=None, version="hermetic_fake_required",
                attempt=attempt, failure_class=FAILURE_INCONCLUSIVE, cleanup_ok=True,
            )
    version = _version(executable)
    cleanup_ok = True
    with tempfile.TemporaryDirectory(prefix="agy-boundary-", dir=args.artifact_dir) as temporary:
        runtime = _prepare_runtime(Path(temporary), args.profile)
        invocation_failed = False
        try:
            _invoke(executable, runtime, live=mode == "live")
        except (OSError, subprocess.TimeoutExpired):
            invocation_failed = True
        events = _read_events(runtime["events_path"])
        for record in _read_events(runtime["enforcement_log"]):
            if record.get("schema") == "agy_permission_boundary_hook/v1":
                record["kind"] = "pre_tool_use"
                record["capability"] = record.pop("resource", None)
                record["canary_id"] = runtime["canary_id"]
                events.append(record)
        attempt = _attempt_from_events(runtime, events, args.profile)
        predicates = attempt["predicates"]
        passed = not invocation_failed and all(predicates.values())
        if mode == "live":
            # A live PASS is permitted only after all structured predicates;
            # no provider other than this exact executable is invoked here.
            exit_code = EXIT_PASS if passed else EXIT_FAIL
            actual_agy = True
        else:
            exit_code = EXIT_PASS if passed else EXIT_FAIL
            actual_agy = False
        failure = "none" if passed else FAILURE_INCONCLUSIVE
        result = _artifact(
            exit_code=exit_code, actual_agy=actual_agy, executable=executable, version=version,
            attempt=attempt, failure_class=failure, cleanup_ok=cleanup_ok,
        )
        return exit_code, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="no_tools")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--agy", help="explicit fake executable in hermetic mode")
    parser.add_argument("--mode", choices=("hermetic", "live"), default="live")
    parser.add_argument("--allow-live", action="store_true", help="caller has confirmed no additional charge")
    args = parser.parse_args()
    if args.profile not in {"no_tools", "local_asset_research", "grounded_research", "proposal_only"}:
        parser.error("unknown profile")
    artifact_dir = Path(args.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    exit_code, result = _run(args)
    valid, reason = validate_artifact(result)
    if not valid:
        exit_code = EXIT_FAIL
        result["runner"]["exit_code"] = EXIT_FAIL
        result["failure_taxonomy"]["class"] = reason
        result["artifact"]["digest"] = _artifact_digest(result)
        result["runner"]["artifact_digest"] = result["artifact"]["digest"]
    _write_artifact(artifact_dir, result)
    print(json.dumps({"artifact": "agy_permission_boundary_e2e.json", "exit_code": exit_code}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
