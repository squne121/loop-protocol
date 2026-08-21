"""AC8: a sentinel credential-like value injected into the isolated child's
environment must never appear in the bridge request, the bridge response, or
this test's own captured stdout/stderr from the client<->server round trip.

The sentinel is treated as an opaque string (no fixed-length regex assumption).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills" / "create-issue" / "scripts"))

import issue_create_bridge_client as bridge_client  # noqa: E402
import issue_create_bridge_server as bridge_server  # noqa: E402

_FAKE_GH = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
_REPO_ROOT = Path(__file__).resolve().parents[3]

_SENTINEL = f"SENTINEL-CRED-{uuid.uuid4().hex}-DO-NOT-LEAK"

_MINIMAL_VALID_BODY = """## Acceptance Criteria

- [ ] AC1: Basic test

## Verification Commands

```bash
test -n "ok"  # AC1
```

## Allowed Paths

- src/**
"""


def test_sentinel_credential_never_appears_in_request_or_response(tmp_path: Path, monkeypatch) -> None:
    # Simulate a host credential-like value present in the child's ambient
    # environment (e.g. an accidentally-inherited variable) -- the isolated
    # child code path must never read or forward it into the bridge request.
    monkeypatch.setenv("SIMULATED_HOST_GH_TOKEN", _SENTINEL)
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "sentinel-nonce")

    request = bridge_client.build_request(
        claimed_repo="squne121/loop-protocol",
        title="実装: sentinel test",
        body=_MINIMAL_VALID_BODY,
    )
    request_json = json.dumps(request.to_json_dict())
    assert _SENTINEL not in request_json

    socket_path = str(tmp_path / "bridge.sock")
    server = bridge_server.IssueCreateBridgeServer(
        socket_path,
        run_nonce="sentinel-nonce",
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
        ledger_path=str(tmp_path / "ledger.jsonl"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CLAUDE_GPT_ISOLATION_PROFILE", "1")
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_SOCKET", socket_path)

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            result = bridge_client.send_issue_create_request(request)
    finally:
        server.shutdown()
        server.server_close()

    result_json = json.dumps(result)
    assert _SENTINEL not in result_json
    assert _SENTINEL not in captured_stdout.getvalue()
    assert _SENTINEL not in captured_stderr.getvalue()
    assert result["status"] == "success"


def test_sentinel_credential_not_present_in_server_audit_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SIMULATED_HOST_GH_TOKEN", _SENTINEL)
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))

    server = bridge_server.IssueCreateBridgeServer(
        str(tmp_path / "bridge2.sock"),
        run_nonce="sentinel-nonce-2",
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
        ledger_path=str(tmp_path / "ledger2.jsonl"),
    )
    try:
        request = {
            "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
            "request_id": "e" * 32,
            "run_nonce": "sentinel-nonce-2",
            "claimed_repo": "squne121/loop-protocol",
            "title": "実装: sentinel audit test",
            "body": _MINIMAL_VALID_BODY,
            "labels": [],
            "issue_kind": "",
            "label_profile": "standard",
            "parent_issue_number": None,
            "dependency_issue_numbers": [],
            "blocking_issue_numbers": [],
        }
        result = bridge_server.execute_trusted_transaction(
            request=request,
            repo=server.repo,
            gh_bin=server.gh_bin,
            repo_root=server.repo_root,
            python_bin=server.python_bin,
        )
        assert result["status"] == "success"
        assert _SENTINEL not in json.dumps(server.audit_log)
    finally:
        server.server_close()
