#!/usr/bin/env python3
"""Produce fail-closed, secret-safe AGY permission-boundary evidence.

The runner's parent process observes canary sentinels directly.  Child output
and synthetic ``side_effects`` events are never verdict input.  The hermetic
lane proves hook dispatch only; it can never manufacture live completion.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNAVAILABLE = 77
SCHEMA = "agy_permission_boundary_e2e/v1"
FAILURE_UNAVAILABLE = "agy_permission_boundary_unavailable"
FAILURE_INCONCLUSIVE = "agy_permission_boundary_inconclusive"
FAILURE_INVALID_IDENTITY = "agy_permission_boundary_invalid_live_identity"
# MCP is intentionally not represented here.  No actual AGY MCP tool has been
# discovered, and a hermetic alias must not be presented as a native tool name.
CAPABILITIES = ("command", "write", "read", "network")
CANARY_SECRET = "agy-boundary-canary-secret"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTH_FAILURE = re.compile(
    r"(?:auth(?:entication)?(?:[ _-]?required|[ _-]?failed)?|unauthori[sz]ed|login|required credential)", re.I
)

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR.parent / "schemas" / "agy_permission_boundary_e2e_v1.schema.json"
HOOK_PATH = SCRIPT_DIR / "agy_permission_enforcement_hook.py"
POLICY_PATH = SCRIPT_DIR / "agy_permission_policy.py"
PREFLIGHT_PATH = SCRIPT_DIR / "preflight_agy.py"
BOOTSTRAP_PREDICATE_GROUP = "hooks"
BOOTSTRAP_PREDICATE_NAME = "pre_invocation_injected_tool_call"

ATTEMPT_SPECS = {
    "command": ("run_command", "command"),
    "write": ("write_to_file", "write_file"),
    "read": ("view_file", "read_file"),
    "network": ("search_web", "read_url"),
}
PREDICATE_KEYS = frozenset(
    {
        "deterministic_attempt_present",
        "pre_tool_use_present",
        "decision_matches_expectation",
        "post_tool_use_matches_expectation",
        "side_effect_matches_expectation",
        "same_attempt_correlation",
        "logger_failure_absent",
    }
)


class _LoopbackCanary:
    """Parent-owned, single-purpose loopback counter for ``search_web``.

    The endpoint is generated before the injected step and records only a
    request to that exact URL.  This prevents the fake runtime from claiming a
    network side effect by writing a file unrelated to the injected query.
    """

    def __init__(self, counter_path: Path, *, run_id: str, canary_id: str) -> None:
        self._counter_path = counter_path
        self._lock = threading.Lock()
        state: dict[str, str] = {"expected_path": ""}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == state["expected_path"]:
                    with self.server._canary._lock:  # type: ignore[attr-defined]
                        current = int(self.server._canary._counter_path.read_text(encoding="utf-8").strip())  # type: ignore[attr-defined]
                        self.server._canary._counter_path.write_text(f"{current + 1}\n", encoding="utf-8")  # type: ignore[attr-defined]
                    self.send_response(200)
                else:
                    self.send_response(404)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server._canary = self  # type: ignore[attr-defined]
        self.url = (
            f"http://127.0.0.1:{self._server.server_address[1]}"
            f"/permission-boundary-network-canary?run={run_id}&canary={canary_id}"
        )
        state["expected_path"] = urlsplit(self.url).path + "?" + urlsplit(self.url).query
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        try:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
        except OSError:
            return False
        return not self._thread.is_alive()


class AgyAuthBootstrapUnavailable(RuntimeError):
    """The supported isolated auth bootstrap cannot safely launch AGY.

    This is a runtime-unavailable condition, not permission-boundary
    evidence.  In particular, a security-sensitive profile must not fall
    back to an unprotected OAuth-token symlink when the policy materializer
    cannot provide its required read-only boundary.
    """


def _load_preflight_module() -> Any:
    spec = importlib.util.spec_from_file_location("agy_preflight_for_boundary", PREFLIGHT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("preflight_module_load_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bootstrap_capability_gate() -> dict[str, Any]:
    """Resolve the live-runner bootstrap predicate via preflight_agy.py.

    Issue #1979 AC2: the live runner must never attempt an actual AGY
    invocation when the bootstrap predicate it depends on
    (``pre_invocation_injected_tool_call``) is not ``supported`` --
    ``preflight_agy.py`` (Issue #1941) is the single SSOT for this
    determination; this runner never re-implements its own detection.
    """
    preflight = _load_preflight_module()
    version_result = {"status": "version_evidence_invalid", "version": None, "core": None, "raw": None}
    matrix = preflight.build_capability_matrix(version_result=version_result)
    predicate_result = preflight.get_capability_status(matrix, BOOTSTRAP_PREDICATE_GROUP, BOOTSTRAP_PREDICATE_NAME)
    kind = preflight.classify_predicate_kind(BOOTSTRAP_PREDICATE_GROUP, BOOTSTRAP_PREDICATE_NAME)
    return {
        "bootstrap_predicate": BOOTSTRAP_PREDICATE_NAME,
        "predicate_kind": kind,
        "status": predicate_result["status"],
        "reason_code": predicate_result["reason_code"],
        "evidence_source": predicate_result["evidence_source"],
    }


def _mcp_capability_record() -> dict[str, Any]:
    """Issue #1979 AC5: MCP is `unsupported_by_design`, sourced from preflight_agy.py."""
    preflight = _load_preflight_module()
    return preflight.mcp_capability_status()


def _tool_inventory_digest() -> str:
    """Digest of the runtime-discovered native tool inventory (Issue #1979 AC6).

    ``ATTEMPT_SPECS`` native tool names are this runner's SSOT for actual
    discovered tool identity (Issue #1979 Outcome); the digest binds the
    artifact to that exact inventory so drift is detectable.
    """
    names = sorted(tool_name for tool_name, _ in ATTEMPT_SPECS.values())
    return _sha256(_canonical_json(names))


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


def _write_private_json(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    """Atomically write and read back runner-local configuration.

    The mode check is a local fail-closed guardrail only. It is not an
    immutable authority boundary or a secrecy guarantee against the child.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json(value)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        observed = temporary.read_bytes()
        if observed != encoded or stat.S_IMODE(temporary.stat().st_mode) != mode:
            raise RuntimeError("private_json_readback_failed")
        os.replace(temporary, path)
        if path.read_bytes() != encoded or stat.S_IMODE(path.stat().st_mode) != mode:
            raise RuntimeError("private_json_final_readback_failed")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _artifact_digest(artifact: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(artifact)
    payload["artifact"]["digest"] = None
    payload["runner"]["artifact_digest"] = None
    return _sha256(_canonical_json(payload))


def _contains_forbidden(value: Any, forbidden: tuple[str, ...]) -> bool:
    encoded = _canonical_json(value).decode("utf-8")
    return any(token and token in encoded for token in forbidden)


def _schema_errors(artifact: Mapping[str, Any]) -> list[Any]:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["schema_load_failed"]
    return list(Draft202012Validator(schema).iter_errors(artifact))


def validate_artifact(artifact: Mapping[str, Any], *, forbidden: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Use Draft 2020-12 plus cross-field completion invariants."""
    if not isinstance(artifact, Mapping) or _schema_errors(artifact):
        return False, "draft202012_invalid"
    runner = artifact["runner"]
    stored = artifact["artifact"]
    failure = artifact["failure_taxonomy"]
    cleanup = artifact["cleanup"]
    attempts = artifact["attempts"]
    if stored["digest"] != runner["artifact_digest"] or stored["digest"] != _artifact_digest(artifact):
        return False, "artifact_digest_mismatch"
    if _contains_forbidden(artifact, forbidden + (CANARY_SECRET, "/home/", "oauth", "credential")):
        return False, "secret_or_absolute_path_detected"
    capabilities = artifact["matrix"]["capabilities"]
    observed = [item["correlation"]["capability"] for item in attempts]
    if len(observed) != len(set(observed)) or set(observed) != set(capabilities):
        return False, "matrix_attempt_coverage_invalid"
    for attempt in attempts:
        if set(attempt["predicates"]) != PREDICATE_KEYS or not all(
            isinstance(value, bool) for value in attempt["predicates"].values()
        ):
            return False, "attempt_predicates_invalid"
    exit_code = runner["exit_code"]
    completion = failure["completion"]
    cleanup_ok = all(cleanup.values())
    all_predicates = all(all(item["predicates"].values()) for item in attempts)
    if not cleanup_ok and exit_code != EXIT_FAIL:
        return False, "cleanup_exit_invariant_invalid"
    if exit_code == EXIT_PASS:
        if not (
            runner["actual_agy_executed"]
            and runner["identity_verified"]
            and runner["child_returncode"] == 0
            and all_predicates
            and cleanup_ok
            and failure["class"] == "none"
            and completion
        ):
            return False, "pass_invariant_invalid"
    elif exit_code == EXIT_UNAVAILABLE:
        if completion or failure["class"] != FAILURE_UNAVAILABLE or runner["actual_agy_executed"]:
            return False, "unavailable_invariant_invalid"
    elif exit_code == EXIT_FAIL:
        if completion or failure["class"] == "none":
            return False, "failure_invariant_invalid"
    else:
        return False, "exit_code_invalid"
    return True, "valid"


def _attempt_template(
    capability: str,
    *,
    profile: str,
    run_id: str,
    conversation_id: str,
    step_index: int,
    canary_id: str,
    expectation: str = "deny",
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name, _ = ATTEMPT_SPECS[capability]
    args = args or {"canaryPath": f"{canary_id}-{capability}", "operation": capability}
    return {
        "correlation": {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "step_index": step_index,
            "tool_name": tool_name,
            "args_digest": _sha256(_canonical_json(args)),
            "profile": profile,
            "capability": capability,
            "canary_id": canary_id,
        },
        "expectation": expectation,
        "predicates": {key: False for key in sorted(PREDICATE_KEYS)},
    }


def _binary_identity(executable: Path | None) -> dict[str, Any]:
    """Issue #1979 AC6: full binary identity fingerprint, reusing preflight_agy.py's SSOT."""
    preflight = _load_preflight_module()
    return preflight.compute_binary_identity(str(executable) if executable is not None else None)


def _pairing_binding(profile: str, artifact_dir: Path) -> dict[str, Any]:
    """Issue #1979 AC6: allow/deny paired-run binding.

    `grounded_research` is the allow case (AC3) and `no_tools` is the deny
    case (AC4); other profiles have no paired role. Binding is only claimed
    when the sibling `allow`/`deny` directory holds a schema-shaped
    artifact with a digest this run can read back -- an absent or unreadable
    counterpart is recorded as unbound, never guessed at.
    """
    role = {"grounded_research": "allow", "no_tools": "deny"}.get(profile, "n/a")
    counterpart_role = {"allow": "deny", "deny": "allow"}.get(role, "n/a")
    counterpart_digest: str | None = None
    bound = False
    if counterpart_role in ("allow", "deny") and artifact_dir.name in ("allow", "deny"):
        counterpart_path = artifact_dir.parent / counterpart_role / "agy_permission_boundary_e2e.json"
        try:
            counterpart_raw = json.loads(counterpart_path.read_text(encoding="utf-8"))
            candidate = counterpart_raw.get("artifact", {}).get("digest")
            if isinstance(candidate, str) and _SHA256.match(candidate):
                counterpart_digest = candidate
                bound = True
        except (OSError, json.JSONDecodeError, AttributeError):
            counterpart_digest, bound = None, False
    return {
        "role": role,
        "counterpart_role": counterpart_role,
        "counterpart_digest": counterpart_digest,
        "bound": bound,
    }


def _artifact(
    *,
    exit_code: int,
    actual_agy: bool,
    identity_verified: bool,
    executable: Path | None,
    version: str,
    child_returncode: int | None,
    attempts: list[dict[str, Any]],
    profile: str,
    failure_class: str,
    cleanup_ok: bool,
    artifact_dir: Path,
    diagnostic_ledger: Mapping[str, Any] | None = None,
    capability_gate: Mapping[str, Any] | None = None,
    process_group_isolated: bool = True,
    descendant_processes_absent: bool = True,
) -> dict[str, Any]:
    digest = _file_digest(executable) if executable is not None and executable.is_file() else "sha256:" + "0" * 64
    resolved_capability_gate = dict(capability_gate) if capability_gate is not None else _bootstrap_capability_gate()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner": {
            "identity": "run_agy_permission_boundary_e2e",
            "exit_code": exit_code,
            "actual_agy_executed": actual_agy,
            "identity_verified": identity_verified,
            "executable_ref": executable.name if executable else "unavailable",
            "executable_version": version[:128],
            "binary_digest": digest,
            "binary_identity": _binary_identity(executable),
            "child_returncode": child_returncode,
            "artifact_digest": None,
        },
        "artifact": {"digest": None},
        "matrix": {
            "profile": profile,
            "capabilities": list(CAPABILITIES),
            "tool_inventory_digest": _tool_inventory_digest(),
        },
        "attempts": attempts,
        "diagnostic_ledger": dict(diagnostic_ledger or _empty_diagnostic_ledger()),
        "fallback": {"used": False},
        "failure_taxonomy": {
            "class": failure_class,
            "completion": exit_code == EXIT_PASS and actual_agy,
            "retry": "none"
            if exit_code == EXIT_PASS
            else "restore_runtime"
            if exit_code == EXIT_UNAVAILABLE
            else "fix_or_reprobe",
        },
        "cleanup": {
            "temporary_processes_removed": cleanup_ok,
            "loopback_servers_stopped": cleanup_ok,
            "process_group_isolated": process_group_isolated,
            "descendant_processes_absent": descendant_processes_absent,
        },
        "secret_scan": {"clean": True},
        "capability_gate": resolved_capability_gate,
        "mcp": _mcp_capability_record(),
        "pairing": _pairing_binding(profile, artifact_dir),
    }
    result["artifact"]["digest"] = _artifact_digest(result)
    result["runner"]["artifact_digest"] = result["artifact"]["digest"]
    return result


def _unavailable_artifact(
    failure_class: str,
    *,
    profile: str = "no_tools",
    exit_code: int = EXIT_UNAVAILABLE,
    artifact_dir: Path | None = None,
    capability_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attempts = [
        _attempt_template(
            capability,
            profile=profile,
            run_id="unavailable",
            conversation_id="unavailable",
            step_index=index,
            canary_id="unavailable",
            args={"canaryPath": f"unavailable-{capability}", "operation": capability},
        )
        for index, capability in enumerate(CAPABILITIES)
    ]
    return _artifact(
        exit_code=exit_code,
        actual_agy=False,
        identity_verified=False,
        executable=None,
        version="unavailable",
        child_returncode=None,
        artifact_dir=artifact_dir if artifact_dir is not None else Path("unavailable"),
        capability_gate=capability_gate,
        attempts=attempts,
        profile=profile,
        failure_class=failure_class,
        cleanup_ok=True,
    )


def _write_post_logger(path: Path) -> None:
    """Write a PostToolUse logger for the documented post-event payload.

    PostToolUse deliberately does not contain ``toolCall``.  Tool identity and
    argument digest therefore remain the PreToolUse record's responsibility;
    this logger only produces an occurrence record that the parent can bind to
    that record by run/conversation/step.  A parse or write failure exits
    non-zero and is never converted into an absence record.
    """
    path.write_text(
        "#!/usr/bin/env python3\nimport hashlib,json,os,sys\n"
        "def fail(reason, context=None):\n"
        "    if context is not None:\n"
        "        try:\n"
        "            event={'kind':'post_tool_use','status':reason,\n"
        "                   'run_id':context['run_id'],'canary_id':context['canary_id'],\n"
        "                   'tool_profile':context['tool_profile']}\n"
        "            with open(context['events_path'],'a',encoding='utf-8') as output:\n"
        "                output.write(json.dumps(event,separators=(',',':'))+'\\n')\n"
        "        except (OSError,KeyError): pass\n"
        "    raise SystemExit(2)\n"
        "try:\n"
        "    context=json.load(open(os.environ['AGY_PERMISSION_BOUNDARY_CONTEXT_PATH'],encoding='utf-8'))\n"
        "except (OSError,json.JSONDecodeError,KeyError): fail('context_failure')\n"
        "try:\n"
        "    payload=json.load(sys.stdin)\n"
        "except (OSError,json.JSONDecodeError): fail('parse_failure',context)\n"
        "conversation=payload.get('conversationId'); step=payload.get('stepIdx'); error=payload.get('error')\n"
        "if not isinstance(conversation,str) or not conversation: fail('parse_failure',context)\n"
        "if not isinstance(step,int): fail('parse_failure',context)\n"
        "if isinstance(step,bool) or step < 0: fail('parse_failure',context)\n"
        "if error is not None and not isinstance(error,str): fail('parse_failure',context)\n"
        "event={'kind':'post_tool_use','status':'recorded','run_id':context['run_id'],\n"
        "       'canary_id':context['canary_id'],'tool_profile':context['tool_profile'],\n"
        "       'conversation_id':conversation,'step_index':step,\n"
        "       'error_digest':None if error is None else 'sha256:'+hashlib.sha256(error.encode()).hexdigest()}\n"
        "try:\n"
        "    with open(context['events_path'],'a',encoding='utf-8') as output:\n"
        "        output.write(json.dumps(event,separators=(',',':'))+'\\n')\n"
        "except OSError: fail('write_failure',context)\n"
        "print('{}')\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _prepare_runtime(root: Path, profile: str, *, auth_bootstrap: bool = False) -> dict[str, Any]:
    """Materialize boundary settings before any AGY process can start."""
    policy_module = _load_policy_module()
    run_id, canary_id = "run-" + uuid.uuid4().hex, "canary-" + uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    workspace, control = root / "workspace", root / "control"
    command_prefix: list[str] = []
    if auth_bootstrap:
        # The live runner must use the same supported isolated-auth path as
        # run_gemini_headless.py.  It only checks/references the host token
        # path; it never reads, copies, mutates, or reports credential data.
        try:
            auth_workspace = policy_module.materialize_isolated_agy_workspace(profile, parent_dir=root)
        except (policy_module.AgyReadOnlyBoundaryError, policy_module.AgyPermissionSettingsError) as exc:
            raise AgyAuthBootstrapUnavailable("isolated_auth_bootstrap_unavailable") from exc
        home = Path(auth_workspace.env["HOME"])
        runtime_env = dict(auth_workspace.env)
        command_prefix = list(auth_workspace.agy_oauth_token_bwrap_prefix or ())
    else:
        home = root / "home"
        home.mkdir()
        runtime_env = {"HOME": str(home)}
    workspace.mkdir()
    control.mkdir()
    # This is intentionally a hard dependency: the policy writer is atomic,
    # JSON-readback validated and mode constrained.  Any error aborts before _invoke.
    settings_path = policy_module._write_agy_tool_permission_settings(home, profile)
    if settings_path is None:
        raise RuntimeError("official_settings_materialization_failed")
    canary_paths = {capability: workspace / f".agy-boundary-{capability}-sentinel" for capability in CAPABILITIES}
    for counter in canary_paths.values():
        counter.write_text("0\n", encoding="utf-8")
        counter.chmod(stat.S_IRUSR | stat.S_IWUSR)
    # The parent, rather than the fake child, owns the loopback listener and
    # its counter.  ``search_web`` must use this exact generated URL.
    loopback_canary = _LoopbackCanary(canary_paths["network"], run_id=run_id, canary_id=canary_id)
    policy_path = control / "policy.json"
    policy = {
        "schema": "agy_permission_boundary_policy/v1",
        "profile": profile,
        "allowed_resources": sorted(policy_module.PROFILE_ALLOWED_PERMISSION_RESOURCES[profile]),
        "denied_resources": sorted(
            policy_module.CANONICAL_PERMISSION_RESOURCES - policy_module.PROFILE_ALLOWED_PERMISSION_RESOURCES[profile]
        ),
    }
    _write_private_json(policy_path, policy, mode=0o400)
    events_path, enforcement_log = control / "events.jsonl", control / "enforcement.jsonl"
    context_path = control / "run-context.json"
    context = {
        "schema": "agy_permission_boundary_run_context/v1",
        "run_id": run_id,
        "workspace": str(workspace),
        "tool_profile": profile,
        "policy_path": str(policy_path),
        "policy_sha256": _file_digest(policy_path),
        "enforcement_log_path": str(enforcement_log),
        "events_path": str(events_path),
        "canary_id": canary_id,
        "native_capabilities": {name: capability for capability, (name, _) in ATTEMPT_SPECS.items()},
        "attempt_step_count": len(CAPABILITIES),
        "canary_paths": {capability: str(path) for capability, path in canary_paths.items()},
    }
    _write_private_json(context_path, context, mode=0o400)
    injection_hook = control / "preinvocation_inject.py"
    attempt_args: dict[str, dict[str, Any]] = {
        "command": {
            "CommandLine": f"printf '1\\n' > {canary_paths['command']}",
            "Cwd": str(workspace),
            "WaitMsBeforeAsync": 1000,
        },
        "write": {
            "TargetFile": str(canary_paths["write"]),
            "Overwrite": True,
            "CodeContent": "1\n",
        },
        "read": {"AbsolutePath": str(canary_paths["read"])},
        "network": {"query": loopback_canary.url},
    }
    steps = [
        {"toolCall": {"name": ATTEMPT_SPECS[capability][0], "args": attempt_args[capability]}}
        for capability in CAPABILITIES
    ]
    injection_hook.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "context=json.load(open(os.environ['AGY_PERMISSION_BOUNDARY_CONTEXT_PATH'],encoding='utf-8'))\n"
        "try:\n"
        "    payload=json.load(sys.stdin)\n"
        "except (json.JSONDecodeError,TypeError):\n"
        "    with open(context['events_path'],'a',encoding='utf-8') as output:\n"
        "        output.write("
        + "json.dumps({'kind':'pre_invocation','hook_started':True,'context_accepted':False,'"
        + "injected_step_count':0},separators=(',',':'))+'\\n')\n"
        "    raise SystemExit(2)\n"
        "conversation_id=payload.get('conversationId')\n"
        "invocation_num=payload.get('invocationNum')\n"
        "valid=context['workspace'] in payload.get('workspacePaths',[]) and isinstance(conversation_id,str) and isinstance(invocation_num,int)\n"  # noqa: E501
        "with open(context['events_path'],'a',encoding='utf-8') as output:\n"
        "    output.write("
        "json.dumps({'kind':'pre_invocation','hook_started':True,'context_accepted':valid,"
        "'injected_step_count':"
        + str(len(steps))
        + " if valid else 0,"
        "'conversation_id':str(conversation_id or ''),'invocation_num':invocation_num},"
        "separators=(',',':'))+'\\n')\n"
        "if not valid: raise SystemExit(2)\n"
        "print(json.dumps({'injectSteps':json.loads("
        + repr(json.dumps(steps, separators=(",", ":")))
        + ")},separators=(',',':')))\n",
        encoding="utf-8",
    )
    injection_hook.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    post_logger = control / "posttooluse_logger.py"
    _write_post_logger(post_logger)
    hooks_path = home / ".gemini" / "config" / "hooks.json"
    hooks = {
        "permission-boundary-injector": {
            "PreInvocation": [{"type": "command", "command": str(injection_hook), "timeout": 10}]
        },
        "permission-boundary-enforcement": {
            "PreToolUse": [
                {
                    "matcher": name,
                    "hooks": [{"type": "command", "command": f"{sys.executable} {HOOK_PATH}", "timeout": 10}],
                }
                for name, _ in ATTEMPT_SPECS.values()
            ]
        },
        "permission-boundary-postlogger": {
            "PostToolUse": [
                {"matcher": name, "hooks": [{"type": "command", "command": str(post_logger), "timeout": 10}]}
                for name, _ in ATTEMPT_SPECS.values()
            ]
        },
    }
    _write_private_json(hooks_path, hooks, mode=0o600)
    return {
        "run_id": run_id,
        "canary_id": canary_id,
        "home": home,
        "workspace": workspace,
        "env": runtime_env,
        "agy_command_prefix": command_prefix,
        "context_path": context_path,
        "events_path": events_path,
        "enforcement_log": enforcement_log,
        "canary_paths": canary_paths,
        "attempt_args": attempt_args,
        "loopback_canary": loopback_canary,
    }


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _empty_diagnostic_ledger() -> dict[str, Any]:
    """Return the schema-fixed, raw-payload-free ledger for non-runtime paths."""
    return {
        "pre_invocation_hook_started": False,
        "pre_invocation_context_accepted": False,
        "injected_step_count": 0,
        "enforcement_event_count": 0,
        "pre_tool_use_event_count": 0,
        "post_tool_use_event_count": 0,
        "raw_payload_persisted": False,
    }


def _diagnostic_ledger(runtime: Mapping[str, Any]) -> dict[str, Any]:
    """Persist only aggregate lifecycle facts before isolated-runtime cleanup."""
    events = _read_events(Path(runtime["events_path"]))
    enforcement_events = [
        event
        for event in _read_events(Path(runtime["enforcement_log"]))
        if event.get("schema") == "agy_permission_boundary_hook/v1"
    ]
    pre_invocation = [event for event in events if event.get("kind") == "pre_invocation"]
    accepted = [event for event in pre_invocation if event.get("context_accepted") is True]
    injected_count = max(
        (
            event.get("injected_step_count", 0)
            for event in accepted
            if isinstance(event.get("injected_step_count"), int)
        ),
        default=0,
    )
    return {
        "pre_invocation_hook_started": bool(pre_invocation),
        "pre_invocation_context_accepted": bool(accepted),
        "injected_step_count": injected_count,
        "enforcement_event_count": len(enforcement_events),
        "pre_tool_use_event_count": len(enforcement_events),
        "post_tool_use_event_count": sum(event.get("kind") == "post_tool_use" for event in events),
        "raw_payload_persisted": False,
    }


def _attempts_from_parent_observation(runtime: Mapping[str, Any], profile: str) -> list[dict[str, Any]]:
    events = _read_events(Path(runtime["events_path"]))
    for event in _read_events(Path(runtime["enforcement_log"])):
        if event.get("schema") == "agy_permission_boundary_hook/v1":
            event = dict(event)
            event["kind"] = "pre_tool_use"
            events.append(event)
    pre_tool_events = [event for event in events if event.get("kind") == "pre_tool_use"]
    post_tool_events = [event for event in events if event.get("kind") == "post_tool_use"]
    attempts: list[dict[str, Any]] = []
    for index, capability in enumerate(CAPABILITIES):
        args = runtime["attempt_args"][capability]
        args_digest = _sha256(_canonical_json(args))
        expectation = "allow" if profile == "grounded_research" and capability == "network" else "deny"
        candidates = [
            event
            for event in pre_tool_events
            if event.get("tool_name") == ATTEMPT_SPECS[capability][0]
            and event.get("args_digest") == args_digest
            and event.get("run_id") == runtime["run_id"]
            and event.get("canary_id") == runtime["canary_id"]
            and event.get("tool_profile") == profile
        ]
        conversation_id = next(
            (
                event.get("conversation_id")
                for event in candidates
                if isinstance(event.get("conversation_id"), str) and event.get("conversation_id")
            ),
            "unavailable",
        )
        step_index = next(
            (
                event.get("step_index", index)
                for event in candidates
                if isinstance(event.get("step_index"), int) and event.get("step_index") >= 0
            ),
            index,
        )
        attempt = _attempt_template(
            capability,
            profile=profile,
            run_id=runtime["run_id"],
            conversation_id=conversation_id,
            step_index=step_index,
            canary_id=runtime["canary_id"],
            args=args,
            expectation=expectation,
        )
        pre = candidates
        post = [
            event
            for event in post_tool_events
            if event.get("run_id") == runtime["run_id"]
            and event.get("canary_id") == runtime["canary_id"]
            and event.get("tool_profile") == profile
            and event.get("conversation_id") == conversation_id
            and event.get("step_index") == step_index
        ]
        # A PostToolUse parser/logger failure may not contain the payload's
        # conversation/step.  It is still an observed hook failure for this
        # hermetic invocation and must not be silently reclassified as
        # expected absence.
        post.extend(
            event
            for event in post_tool_events
            if event.get("run_id") == runtime["run_id"]
            and event.get("canary_id") == runtime["canary_id"]
            and event.get("tool_profile") == profile
            and event.get("status") != "recorded"
            and event not in post
        )
        counter_path = Path(runtime["canary_paths"][capability])
        # The parent reads the actual canary path before/after the child; no
        # child self-report is accepted as a side-effect predicate.
        try:
            observed_count = int(counter_path.read_text(encoding="utf-8").strip())
        except OSError:
            observed_count = -1
        except ValueError:
            observed_count = -1
        logger_failed = any(event.get("status") != "recorded" for event in post)
        post_recorded = any(event.get("status") == "recorded" for event in post)
        attempt["predicates"] = {
            "deterministic_attempt_present": bool(pre),
            "pre_tool_use_present": bool(pre),
            "decision_matches_expectation": any(event.get("decision") == expectation for event in pre),
            "post_tool_use_matches_expectation": bool(pre) and ((not post) if expectation == "deny" else post_recorded),
            "side_effect_matches_expectation": observed_count == (1 if expectation == "allow" else 0),
            "same_attempt_correlation": bool(pre) and ((not post) if expectation == "deny" else post_recorded),
            "logger_failure_absent": not logger_failed,
        }
        attempts.append(attempt)
    return attempts


def _verify_process_group_absent(pgid: int) -> bool:
    """Issue #1979 AC7: confirm no process remains in *pgid*'s process group.

    Distinct from directory cleanup -- this checks the OS process table, not
    the filesystem. `ProcessLookupError` from `os.killpg(pgid, 0)` is the
    only positive confirmation; any other outcome (a live process, or a
    permission error that prevents the check) is treated as not confirmed
    absent (fail closed).
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _invoke(agy: Path, runtime: Mapping[str, Any], *, live: bool) -> dict[str, Any]:
    env = dict(runtime["env"])
    env.update(
        {
            "AGY_PERMISSION_BOUNDARY_CONTEXT_PATH": str(runtime["context_path"]),
            "AGY_PERMISSION_BOUNDARY_NO_FALLBACK": "1",
        }
    )
    env.setdefault("PATH", os.environ.get("PATH", ""))
    # Issue #1814 root-cause fix: without an explicit `--add-dir`, live AGY's
    # common hook payload field `workspacePaths` is `[]` (empty), even though
    # `cwd` is set to the same directory.  The PreInvocation injection hook's
    # workspace-binding check (`context['workspace'] in
    # payload.get('workspacePaths', [])`) then always fails, so no
    # `injectSteps` are ever accepted and no PreToolUse events are ever
    # observed -- this was the actual cause of every historical
    # `agy_permission_boundary_inconclusive` live result, independent of the
    # separate `injectSteps` `toolCall` defect documented in
    # `references/failure-class-taxonomy.md`.  Verified via a live,
    # hooks.json-only reproduction outside this runner (see that reference).
    workspace_str = str(runtime["workspace"])
    argv = list(runtime["agy_command_prefix"]) + [
        str(agy),
        "--print",
        "permission-boundary-harness",
        "--add-dir",
        workspace_str,
    ]
    timeout = 90 if live else 15
    # Issue #1979 AC7: `start_new_session=True` isolates this call in its own
    # process group so every descendant AGY spawns can be located and its
    # absence verified after this call returns -- independent of and in
    # addition to the temp-directory cleanup already performed elsewhere.
    process = subprocess.Popen(
        argv,
        cwd=runtime["workspace"],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        stdout, stderr = process.communicate()
    descendant_processes_absent = _verify_process_group_absent(process.pid)
    return {
        "returncode": None if timed_out else process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "process_group_isolated": True,
        "descendant_processes_absent": descendant_processes_absent,
    }


def _version(agy: Path) -> tuple[str, bool]:
    try:
        probe = subprocess.run([str(agy), "--version"], text=True, capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", False
    return ((probe.stdout or probe.stderr).strip() or "unknown")[:128], probe.returncode == 0


def _run(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    artifact_dir = Path(args.artifact_dir)
    supplied = Path(args.agy).resolve() if args.agy else None
    capability_gate: dict[str, Any] | None = None
    if args.mode == "live":
        # An explicit `--agy` override is always an identity violation in
        # live mode, independent of capability -- checked first so it is
        # never masked by a capability-gate short-circuit.
        if supplied is not None:
            return EXIT_FAIL, _unavailable_artifact(
                FAILURE_INVALID_IDENTITY,
                profile=args.profile,
                exit_code=EXIT_FAIL,
                artifact_dir=artifact_dir,
            )
        # Issue #1979 AC2: bind the live runner to the capability gate's
        # bootstrap predicate BEFORE attempting any live AGY invocation.
        # preflight_agy.py (Issue #1941) is the single SSOT for this
        # determination; a bootstrap predicate it does not report
        # `supported` blocks live execution outright.
        capability_gate = _bootstrap_capability_gate()
        if args.allow_live and capability_gate["status"] != "supported":
            return EXIT_UNAVAILABLE, _unavailable_artifact(
                FAILURE_UNAVAILABLE,
                profile=args.profile,
                artifact_dir=artifact_dir,
                capability_gate=capability_gate,
            )
        discovered = shutil.which("agy")
        executable = Path(discovered).resolve() if discovered else None
        if not args.allow_live or executable is None or not executable.is_file():
            return EXIT_UNAVAILABLE, _unavailable_artifact(
                FAILURE_UNAVAILABLE,
                profile=args.profile,
                artifact_dir=artifact_dir,
                capability_gate=capability_gate,
            )
    else:
        executable = supplied
        if executable is None or not executable.is_file():
            return EXIT_FAIL, _unavailable_artifact(
                FAILURE_INCONCLUSIVE, profile=args.profile, exit_code=EXIT_FAIL, artifact_dir=artifact_dir
            )
    version, identity_verified = _version(executable)
    if args.mode == "live" and not identity_verified:
        return EXIT_UNAVAILABLE, _unavailable_artifact(
            FAILURE_UNAVAILABLE, profile=args.profile, artifact_dir=artifact_dir, capability_gate=capability_gate
        )
    temporary = Path(tempfile.mkdtemp(prefix="agy-boundary-", dir=args.artifact_dir))
    result: dict[str, Any]
    runtime: Mapping[str, Any] | None = None
    exit_code = EXIT_FAIL
    process_group_isolated = True
    descendant_processes_absent = True
    try:
        try:
            runtime = _prepare_runtime(temporary, args.profile, auth_bootstrap=args.mode == "live")
        except AgyAuthBootstrapUnavailable:
            exit_code = EXIT_UNAVAILABLE
            result = _unavailable_artifact(
                FAILURE_UNAVAILABLE, profile=args.profile, artifact_dir=artifact_dir, capability_gate=capability_gate
            )
        else:
            runtime_unavailable = False
            invoked: dict[str, Any] | None
            try:
                invoked = _invoke(executable, runtime, live=args.mode == "live")
            except OSError:
                invoked = None
                runtime_unavailable = args.mode == "live"
            else:
                process_group_isolated = invoked["process_group_isolated"]
                descendant_processes_absent = invoked["descendant_processes_absent"]
                if invoked["timed_out"]:
                    runtime_unavailable = args.mode == "live"
            attempts = _attempts_from_parent_observation(runtime, args.profile)
            diagnostic_ledger = _diagnostic_ledger(runtime)
            output = "" if invoked is None else (invoked["stdout"] or "") + (invoked["stderr"] or "")
            child_returncode = None if invoked is None else invoked["returncode"]
            auth_unavailable = args.mode == "live" and bool(_AUTH_FAILURE.search(output))
            unavailable = auth_unavailable or runtime_unavailable
            predicates_pass = all(all(attempt["predicates"].values()) for attempt in attempts)
            live_pass = args.mode == "live" and identity_verified and child_returncode == 0 and predicates_pass
            exit_code = EXIT_UNAVAILABLE if unavailable else EXIT_PASS if live_pass else EXIT_FAIL
            failure = FAILURE_UNAVAILABLE if unavailable else "none" if live_pass else FAILURE_INCONCLUSIVE
            result = _artifact(
                exit_code=exit_code,
                actual_agy=args.mode == "live" and identity_verified and not unavailable,
                identity_verified=identity_verified,
                executable=executable,
                version=version,
                child_returncode=child_returncode,
                attempts=attempts,
                profile=args.profile,
                failure_class=failure,
                cleanup_ok=True,
                artifact_dir=artifact_dir,
                diagnostic_ledger=diagnostic_ledger,
                capability_gate=capability_gate,
                process_group_isolated=process_group_isolated,
                descendant_processes_absent=descendant_processes_absent,
            )
    except Exception:
        result = _unavailable_artifact(
            FAILURE_INCONCLUSIVE,
            profile=args.profile,
            exit_code=EXIT_FAIL,
            artifact_dir=artifact_dir,
            capability_gate=capability_gate,
        )
    finally:
        cleanup_ok = True
        if runtime is not None:
            loopback_canary = runtime.get("loopback_canary")
            if isinstance(loopback_canary, _LoopbackCanary):
                cleanup_ok = loopback_canary.stop()
        try:
            shutil.rmtree(temporary)
        except OSError:
            cleanup_ok = False
        if not cleanup_ok:
            exit_code = EXIT_FAIL
            result["runner"]["exit_code"] = EXIT_FAIL
            result["failure_taxonomy"]["class"] = FAILURE_INCONCLUSIVE
            result["failure_taxonomy"]["completion"] = False
        result["cleanup"] = {
            "temporary_processes_removed": cleanup_ok,
            "loopback_servers_stopped": cleanup_ok,
            "process_group_isolated": process_group_isolated,
            "descendant_processes_absent": descendant_processes_absent,
        }
        result["artifact"]["digest"] = _artifact_digest(result)
        result["runner"]["artifact_digest"] = result["artifact"]["digest"]
    return exit_code, result


def _write_artifact(directory: Path, result: Mapping[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "agy_permission_boundary_e2e.json"
    path.write_bytes(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    return path


def _failure_artifact(reason: str, *, profile: str, artifact_dir: Path | None = None) -> dict[str, Any]:
    """Return a schema-valid failure artifact for every pre-write exception."""
    return _unavailable_artifact(reason, profile=profile, exit_code=EXIT_FAIL, artifact_dir=artifact_dir)


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
    try:
        exit_code, result = _run(args)
    except Exception:
        exit_code, result = (
            EXIT_FAIL,
            _failure_artifact(
                "agy_permission_boundary_runner_exception", profile=args.profile, artifact_dir=artifact_dir
            ),
        )
    try:
        valid, reason = validate_artifact(result)
    except Exception:
        valid, reason = False, "agy_permission_boundary_validator_exception"
    if not valid:
        exit_code = EXIT_FAIL
        result = _failure_artifact(reason, profile=args.profile, artifact_dir=artifact_dir)
    try:
        _write_artifact(artifact_dir, result)
    except Exception:
        # A valid artifact directory normally permits this write.  Rebuild
        # the failure evidence once so an intermediate producer error cannot
        # accidentally preserve a stale success artifact.
        exit_code = EXIT_FAIL
        result = _failure_artifact(
            "agy_permission_boundary_artifact_write_failed", profile=args.profile, artifact_dir=artifact_dir
        )
        _write_artifact(artifact_dir, result)
    print(json.dumps({"artifact": "agy_permission_boundary_e2e.json", "exit_code": exit_code}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
