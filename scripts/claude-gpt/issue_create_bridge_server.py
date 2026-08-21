#!/usr/bin/env python3
"""Launcher-owned parent bridge server for isolated Claude-GPT issue.create requests.

Issue #2259. This process is started/stopped/owned exclusively by
``scripts/claude-gpt/launch.sh`` while still running under the launcher's own
trusted, non-isolated environment (i.e. before the launcher switches the Claude
child process to the credential-isolated HOME/GH_CONFIG_DIR/XDG_* directories).

Trust boundary contract:
  - repository, credential source, helper path, and ``gh`` executable path are
    fixed here (server-side) and are never taken from the child request (AC4).
  - schema validation of incoming requests is implemented independently in this
    file (not imported from the child-side
    ``.claude/skills/create-issue/scripts/issue_create_bridge_client.py``); the
    parent must never trust or execute child-controlled code for its own
    validation (see design note in that file).
  - the trusted ``create_issue_txn.py`` transaction helper is invoked as a
    *subprocess*, using the parent's own os.environ, with every bridge-related
    env var explicitly stripped before spawn so that a (hypothetical) helper
    that itself checked for isolation env vars could never recurse back into
    the bridge (AC5).
  - idempotency: request_id replay is served from a JSONL ledger so that
    client-side timeout/response loss after a successful remote create does not
    produce a duplicate GitHub issue (AC6).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REQUEST_SCHEMA_NAME = "ISOLATION_ISSUE_CREATE_REQUEST_V1"
RESULT_SCHEMA_NAME = "ISOLATION_ISSUE_CREATE_RESULT_V1"

REQUEST_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "run_nonce",
        "claimed_repo",
        "title",
        "body",
        "labels",
        "issue_kind",
        "label_profile",
        "parent_issue_number",
        "dependency_issue_numbers",
        "blocking_issue_numbers",
    }
)

RESULT_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "status",
        "issue_number",
        "issue_url",
        "node_id",
        "body_sha256",
        "completed_steps",
        "failure_stage",
        "failure_message",
        "reconciled",
    }
)

LABEL_PROFILES = frozenset({"standard", "triage_only"})

_MAX_TITLE_LEN = 256
_MAX_BODY_LEN = 60_000
_MAX_LABELS = 20
_MAX_LABEL_LEN = 50
_MAX_ISSUE_KIND_LEN = 50
_MAX_CLAIMED_REPO_LEN = 200
_MAX_DEPENDENCY_ITEMS = 50
_MAX_RUN_NONCE_LEN = 128
_MAX_REQUEST_BYTES = 1_048_576  # 1 MiB bounded read

# Bridge-related env vars that must never reach the trusted helper subprocess
# (AC5 -- no-recursion guarantee: even if the helper somehow re-checked these,
# it would see them absent).
BRIDGE_ENV_VARS_TO_STRIP = (
    "CLAUDE_GPT_ISOLATION_PROFILE",
    "CLAUDE_GPT_ISSUE_CREATE_SOCKET",
    "CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE",
)


class BridgeServerSchemaError(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise BridgeServerSchemaError(f"duplicate JSON key: {key!r}")
        seen.add(key)
        result[key] = value
    return result


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_int_or_none(value: Any) -> bool:
    return value is None or _is_strict_int(value)


def decode_and_validate_request(raw: str) -> dict[str, Any]:
    """Independently decode+validate an incoming request against the closed schema.

    Raises BridgeServerSchemaError on any violation (duplicate key, unknown key,
    missing key, bounds violation, bool-as-int, etc). Never partially trusts a
    malformed payload.
    """
    obj = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    if not isinstance(obj, dict):
        raise BridgeServerSchemaError("top-level payload must be a JSON object")

    keys = set(obj.keys())
    unknown = keys - REQUEST_KEYS
    if unknown:
        raise BridgeServerSchemaError(f"unknown request keys: {sorted(unknown)}")
    missing = REQUEST_KEYS - keys
    if missing:
        raise BridgeServerSchemaError(f"missing request keys: {sorted(missing)}")
    if obj["schema"] != REQUEST_SCHEMA_NAME:
        raise BridgeServerSchemaError(f"schema mismatch: {obj['schema']!r}")
    if not isinstance(obj["request_id"], str) or not obj["request_id"]:
        raise BridgeServerSchemaError("request_id must be a non-empty string")
    if not isinstance(obj["run_nonce"], str) or not (1 <= len(obj["run_nonce"]) <= _MAX_RUN_NONCE_LEN):
        raise BridgeServerSchemaError("run_nonce out of bounds")
    if not isinstance(obj["claimed_repo"], str) or not (1 <= len(obj["claimed_repo"]) <= _MAX_CLAIMED_REPO_LEN):
        raise BridgeServerSchemaError("claimed_repo out of bounds")
    if not isinstance(obj["title"], str) or not (1 <= len(obj["title"]) <= _MAX_TITLE_LEN):
        raise BridgeServerSchemaError("title out of bounds")
    if not isinstance(obj["body"], str) or len(obj["body"]) > _MAX_BODY_LEN:
        raise BridgeServerSchemaError("body out of bounds")
    labels = obj["labels"]
    if not isinstance(labels, list) or len(labels) > _MAX_LABELS:
        raise BridgeServerSchemaError("labels out of bounds")
    for label in labels:
        if not isinstance(label, str) or not (1 <= len(label) <= _MAX_LABEL_LEN):
            raise BridgeServerSchemaError(f"invalid label: {label!r}")
    if not isinstance(obj["issue_kind"], str) or len(obj["issue_kind"]) > _MAX_ISSUE_KIND_LEN:
        raise BridgeServerSchemaError("issue_kind out of bounds")
    if obj["label_profile"] not in LABEL_PROFILES:
        raise BridgeServerSchemaError(f"invalid label_profile: {obj['label_profile']!r}")
    if not _is_strict_int_or_none(obj["parent_issue_number"]):
        raise BridgeServerSchemaError("parent_issue_number must be int or null")
    if isinstance(obj["parent_issue_number"], int) and obj["parent_issue_number"] < 1:
        raise BridgeServerSchemaError("parent_issue_number must be >= 1")
    for field_name in ("dependency_issue_numbers", "blocking_issue_numbers"):
        values = obj[field_name]
        if not isinstance(values, list) or len(values) > _MAX_DEPENDENCY_ITEMS:
            raise BridgeServerSchemaError(f"{field_name} out of bounds")
        for item in values:
            if not _is_strict_int(item) or item < 1:
                raise BridgeServerSchemaError(f"invalid {field_name} entry: {item!r}")
    return obj


def encode_result(result: dict[str, Any]) -> str:
    keys = set(result.keys())
    if keys != RESULT_KEYS:
        raise BridgeServerSchemaError(f"result key set mismatch: {sorted(keys)}")
    return json.dumps(result, ensure_ascii=True, sort_keys=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RequestLedger:
    """JSONL-backed idempotency ledger. One process, guarded by a threading lock
    plus an advisory flock on the ledger file for cross-process durability."""

    def __init__(self, ledger_path: str) -> None:
        self._path = Path(ledger_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = entry.get("request_id")
                result = entry.get("result")
                if isinstance(request_id, str) and isinstance(result, dict):
                    self._cache[request_id] = result

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._cache.get(request_id)

    def put(self, request_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._cache[request_id] = result
            with self._path.open("a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(json.dumps({"request_id": request_id, "result": result}, ensure_ascii=True) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _resolve_create_issue_txn_path(repo_root: Path) -> Path:
    return repo_root / ".claude" / "skills" / "create-issue" / "scripts" / "create_issue_txn.py"


def _issue_graphql_node_id(repo: str, issue_number: int, gh_bin: str) -> str | None:
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){issue(number:$number){id}}}"
    )
    try:
        proc = subprocess.run(
            [
                gh_bin,
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"number={issue_number}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
        return payload["data"]["repository"]["issue"]["id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def execute_trusted_transaction(
    *,
    request: dict[str, Any],
    repo: str,
    gh_bin: str,
    repo_root: Path,
    python_bin: str,
) -> dict[str, Any]:
    """Spawn create_issue_txn.py as a subprocess with the parent's trusted,
    non-isolated environment (bridge env vars explicitly stripped), and return a
    RESULT_V1-shaped dict."""
    helper_path = _resolve_create_issue_txn_path(repo_root)
    body_text = request["body"]

    env = os.environ.copy()
    for var in BRIDGE_ENV_VARS_TO_STRIP:
        env.pop(var, None)

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as body_fh:
        body_fh.write(body_text)
        body_file_path = body_fh.name

    args = [
        python_bin,
        str(helper_path),
        "--repo",
        repo,
        "--title",
        request["title"],
        "--body-file",
        body_file_path,
        "--issue-kind",
        request["issue_kind"],
        "--label-profile",
        request["label_profile"],
        "--gh",
        gh_bin,
    ]
    for label in request["labels"]:
        args += ["--label", label]
    if request["parent_issue_number"] is not None:
        args += ["--parent-issue", str(request["parent_issue_number"])]
    for dep in request["dependency_issue_numbers"]:
        args += ["--blocked-by", str(dep)]
    for blk in request["blocking_issue_numbers"]:
        args += ["--blocking", str(blk)]

    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failure",
            "issue_number": None,
            "issue_url": None,
            "node_id": None,
            "body_sha256": None,
            "completed_steps": [],
            "failure_stage": "helper-timeout",
            "failure_message": "create_issue_txn.py subprocess timed out",
            "reconciled": False,
        }
    finally:
        try:
            os.unlink(body_file_path)
        except OSError:
            pass

    txn_result: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            txn_result = json.loads(line)
        except json.JSONDecodeError:
            continue
    if not txn_result:
        return {
            "status": "failure",
            "issue_number": None,
            "issue_url": None,
            "node_id": None,
            "body_sha256": None,
            "completed_steps": [],
            "failure_stage": "helper-output-parse",
            "failure_message": f"no JSON result line from helper (exit={proc.returncode})",
            "reconciled": False,
        }

    status = txn_result.get("status", "failure")
    issue_number = txn_result.get("issue_number")
    issue_url = txn_result.get("issue_url")
    node_id = None
    body_sha256 = None
    if isinstance(issue_number, int):
        # Authoritative post-create readback (AC7): confirm node_id via a fresh
        # GraphQL GET, independent from whatever the helper itself reported.
        node_id = _issue_graphql_node_id(repo, issue_number, gh_bin)
        body_sha256 = _sha256_hex(body_text)

    if status == "success":
        mapped_status = "success"
    elif status == "partial_failure":
        mapped_status = "partial_failure"
    else:
        mapped_status = "failure"
    return {
        "status": mapped_status,
        "issue_number": issue_number if isinstance(issue_number, int) else None,
        "issue_url": issue_url if isinstance(issue_url, str) else None,
        "node_id": node_id,
        "body_sha256": body_sha256,
        "completed_steps": [s for s in txn_result.get("completed_steps", []) if isinstance(s, str)],
        "failure_stage": txn_result.get("failure_stage"),
        "failure_message": txn_result.get("failure_message"),
        "reconciled": False,
    }


class BridgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:  # noqa: D102
        server: "IssueCreateBridgeServer" = self.server  # type: ignore[assignment]
        raw_bytes = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        if len(raw_bytes) > _MAX_REQUEST_BYTES:
            self._respond_failure(request_id="", stage="request-too-large", message="request exceeded size limit")
            return
        raw = raw_bytes.decode("utf-8", errors="replace").strip()
        if not raw:
            return
        try:
            request = decode_and_validate_request(raw)
        except (BridgeServerSchemaError, json.JSONDecodeError) as exc:
            self._respond_failure(request_id="", stage="schema-validation", message=str(exc))
            return

        request_id = request["request_id"]

        if request["run_nonce"] != server.run_nonce:
            self._respond_failure(request_id=request_id, stage="run-nonce-mismatch", message="run_nonce mismatch")
            return

        existing = server.ledger.get(request_id)
        if existing is not None:
            reconciled_result = dict(existing)
            reconciled_result["reconciled"] = True
            self._write(encode_result(reconciled_result))
            return

        if request["claimed_repo"] != server.repo:
            server.audit_log.append(
                {
                    "request_id": request_id,
                    "event": "claimed_repo_mismatch_ignored",
                    "claimed_repo": request["claimed_repo"],
                    "authoritative_repo": server.repo,
                }
            )

        result = execute_trusted_transaction(
            request=request,
            repo=server.repo,
            gh_bin=server.gh_bin,
            repo_root=server.repo_root,
            python_bin=server.python_bin,
        )
        result_payload = {
            "schema": RESULT_SCHEMA_NAME,
            "request_id": request_id,
            "status": result["status"],
            "issue_number": result["issue_number"],
            "issue_url": result["issue_url"],
            "node_id": result["node_id"],
            "body_sha256": result["body_sha256"],
            "completed_steps": result["completed_steps"],
            "failure_stage": result["failure_stage"],
            "failure_message": result["failure_message"],
            "reconciled": False,
        }
        server.ledger.put(request_id, result_payload)
        self._write(encode_result(result_payload))

    def _respond_failure(self, *, request_id: str, stage: str, message: str) -> None:
        payload = {
            "schema": RESULT_SCHEMA_NAME,
            "request_id": request_id,
            "status": "failure",
            "issue_number": None,
            "issue_url": None,
            "node_id": None,
            "body_sha256": None,
            "completed_steps": [],
            "failure_stage": stage,
            "failure_message": message,
            "reconciled": False,
        }
        self._write(json.dumps(payload, ensure_ascii=True, sort_keys=True))

    def _write(self, text: str) -> None:
        try:
            self.wfile.write((text + "\n").encode("utf-8"))
        except OSError:
            pass


class IssueCreateBridgeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        socket_path: str,
        *,
        run_nonce: str,
        repo: str,
        gh_bin: str,
        repo_root: Path,
        python_bin: str,
        ledger_path: str,
    ) -> None:
        self.run_nonce = run_nonce
        self.repo = repo
        self.gh_bin = gh_bin
        self.repo_root = repo_root
        self.python_bin = python_bin
        self.ledger = RequestLedger(ledger_path)
        self.audit_log: list[dict[str, Any]] = []
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        super().__init__(socket_path, BridgeRequestHandler)
        os.chmod(socket_path, 0o600)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isolated issue.create parent bridge server")
    parser.add_argument("--socket-path", required=True)
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--gh", dest="gh_bin", default="gh")
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--idle-timeout-seconds", type=float, default=0.0, help="0 disables idle auto-exit")
    parser.add_argument("--ready-fd", type=int, default=-1, help="write 'READY\\n' to this fd once listening")
    return parser.parse_args(argv)


def _raise_system_exit(_signum: int, _frame: Any) -> None:
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Respond to SIGTERM (the launcher'''s normal stop signal, per AC3 lifecycle
    # ownership) the same way as SIGINT/KeyboardInterrupt: unwind out of
    # serve_forever() so the socket-file cleanup below always runs.
    signal.signal(signal.SIGTERM, _raise_system_exit)
    server = IssueCreateBridgeServer(
        args.socket_path,
        run_nonce=args.run_nonce,
        repo=args.repo,
        gh_bin=args.gh_bin,
        repo_root=Path(args.repo_root),
        python_bin=args.python_bin,
        ledger_path=args.ledger_path,
    )

    if args.ready_fd >= 0:
        try:
            with os.fdopen(args.ready_fd, "w") as ready_fh:
                ready_fh.write("READY\n")
        except OSError:
            pass

    if args.idle_timeout_seconds > 0:
        server.timeout = args.idle_timeout_seconds

        def _serve_with_idle_timeout() -> None:
            last_activity = time.monotonic()
            while True:
                server.handle_request()
                if time.monotonic() - last_activity > args.idle_timeout_seconds:
                    break

        try:
            _serve_with_idle_timeout()
        except KeyboardInterrupt:
            pass
    else:
        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
    server.server_close()
    if os.path.exists(args.socket_path):
        try:
            os.unlink(args.socket_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
