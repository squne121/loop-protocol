"""AC6: request replay (idempotency) / reconcile behavior.

Runs the real IssueCreateBridgeServer over a Unix domain socket in a
background thread and drives it with issue_create_bridge_client, against a
deterministic fake `gh` binary.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts" / "claude-gpt"))

import issue_create_bridge_client as bridge_client  # noqa: E402
import issue_create_bridge_server as bridge_server  # noqa: E402

_FAKE_GH = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]

_MINIMAL_VALID_BODY = """## Acceptance Criteria

- [ ] AC1: Basic test

## Verification Commands

```bash
test -n "ok"  # AC1
```

## Allowed Paths

- src/**
"""


@pytest.fixture
def running_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    socket_path = str(tmp_path / "bridge.sock")
    server = bridge_server.IssueCreateBridgeServer(
        socket_path,
        run_nonce="test-nonce",
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
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "test-nonce")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _send(title: str, request_id_seed: str | None = None) -> dict:
    request = bridge_client.build_request(
        claimed_repo="squne121/loop-protocol",
        title=title,
        body=_MINIMAL_VALID_BODY,
    )
    if request_id_seed is not None:
        request = bridge_client.IssueCreateRequest(
            request_id=request_id_seed,
            run_nonce=request.run_nonce,
            claimed_repo=request.claimed_repo,
            title=request.title,
            body=request.body,
        )
    return bridge_client.send_issue_create_request(request)


def test_replaying_the_same_request_id_does_not_duplicate_create(running_server) -> None:
    request_id = "z" * 32
    first = _send("実装: reconcile test A", request_id_seed=request_id)
    assert first["status"] == "success"
    assert first["reconciled"] is False

    replay = _send("実装: reconcile test A", request_id_seed=request_id)
    assert replay["status"] == "success"
    assert replay["issue_number"] == first["issue_number"]
    assert replay["reconciled"] is True


def test_distinct_requests_create_distinct_issues(running_server) -> None:
    first = _send("実装: reconcile test B1")
    second = _send("実装: reconcile test B2")
    assert first["issue_number"] != second["issue_number"]


def test_run_nonce_mismatch_is_rejected_without_mutation(running_server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "wrong-nonce")
    result = _send("実装: reconcile test C")
    assert result["status"] == "failure"
    assert result["issue_number"] is None
