"""AC3: launcher owns start/endpoint-publish/stop/cleanup lifecycle of the parent
bridge server. This test exercises the server process directly (spawned exactly
as launch.sh would invoke it) rather than launch.sh itself, since launch.sh's
own end-to-end lifecycle is covered by the runtime system test (AC10)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_SERVER_PATH = _SCRIPT_DIR / "issue_create_bridge_server.py"
_FAKE_GH = _SCRIPT_DIR / "tests" / "fixtures" / "fake_gh.py"


def _wait_for_socket(path: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise TimeoutError(f"socket {path} did not appear within {timeout}s")


def test_server_publishes_socket_and_exits_cleanly_on_terminate(tmp_path: Path) -> None:
    socket_path = str(tmp_path / "bridge.sock")
    ledger_path = str(tmp_path / "ledger.jsonl")
    env = os.environ.copy()
    env["FAKE_GH_STATE"] = str(tmp_path / "fake_gh_state.json")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(_SERVER_PATH),
            "--socket-path",
            socket_path,
            "--run-nonce",
            "lifecycle-nonce",
            "--repo",
            "squne121/loop-protocol",
            "--gh",
            str(_FAKE_GH),
            "--ledger-path",
            ledger_path,
        ],
        env=env,
    )
    try:
        _wait_for_socket(socket_path)
        assert os.path.exists(socket_path)

        # A minimal handshake to confirm the server is actually accepting connections.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(socket_path)
        request = {
            "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
            "request_id": uuid.uuid4().hex,
            "run_nonce": "lifecycle-nonce",
            "claimed_repo": "squne121/loop-protocol",
            "title": "実装: lifecycle test",
            "body": (
                "## Acceptance Criteria\n\n- [ ] AC1: x\n\n"
                "## Verification Commands\n\n```bash\ntest -n \"ok\"  # AC1\n```\n\n"
                "## Allowed Paths\n\n- src/**\n"
            ),
            "labels": [],
            "issue_kind": "",
            "label_profile": "standard",
            "parent_issue_number": None,
            "dependency_issue_numbers": [],
            "blocking_issue_numbers": [],
        }
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        response = sock.recv(65536).decode("utf-8")
        sock.close()
        payload = json.loads(response)
        assert payload["schema"] == "ISOLATION_ISSUE_CREATE_RESULT_V1"
        assert payload["status"] == "success"
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # AC3: the launcher-facing lifecycle contract requires the socket file to be
    # removed on clean shutdown (best-effort cleanup, verified independently here).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and os.path.exists(socket_path):
        time.sleep(0.05)
    assert not os.path.exists(socket_path), "bridge server did not clean up its socket file on terminate"
