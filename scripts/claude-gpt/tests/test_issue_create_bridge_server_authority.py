"""AC4: repository / credential source / helper path / gh executable path are
fixed on the server side and can never be redirected by the child request."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import issue_create_bridge_server as bridge_server  # noqa: E402

_FAKE_GH = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
_REPO_ROOT = Path(__file__).resolve().parents[3]

_MINIMAL_VALID_BODY = """## Acceptance Criteria

- [ ] AC1: Basic test

## Verification Commands

```bash
test -n "ok"  # AC1
```

## Allowed Paths

- src/**
"""


def _request(claimed_repo: str, request_id: str) -> dict:
    return {
        "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
        "request_id": request_id,
        "run_nonce": "nonce",
        "claimed_repo": claimed_repo,
        "title": "実装: authority test",
        "body": _MINIMAL_VALID_BODY,
        "labels": [],
        "issue_kind": "",
        "label_profile": "standard",
        "parent_issue_number": None,
        "dependency_issue_numbers": [],
        "blocking_issue_numbers": [],
    }


def test_claimed_repo_mismatch_does_not_redirect_the_authoritative_repo(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    request = _request(claimed_repo="some-attacker/other-repo", request_id="a" * 32)
    result = bridge_server.execute_trusted_transaction(
        request=request,
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
    )
    assert result["status"] == "success"
    # The fake gh binary is invoked with --repo squne121/loop-protocol regardless
    # of the child's claimed_repo -- verified by the resulting issue_url pointing
    # at the server-fixed repository, never the attacker-claimed one.
    assert result["issue_url"].startswith("https://github.com/squne121/loop-protocol/issues/")


def test_claimed_repo_mismatch_is_recorded_for_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    socket_path = str(tmp_path / "bridge.sock")
    server = bridge_server.IssueCreateBridgeServer(
        socket_path,
        run_nonce="nonce",
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
        ledger_path=str(tmp_path / "ledger.jsonl"),
    )
    try:
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        import json
        import socket as socket_mod

        sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect(socket_path)
        request = _request(claimed_repo="some-attacker/other-repo", request_id="b" * 32)
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        sock.shutdown(socket_mod.SHUT_WR)
        sock.recv(65536)
        sock.close()
    finally:
        server.shutdown()
        server.server_close()

    assert any(entry["event"] == "claimed_repo_mismatch_ignored" for entry in server.audit_log)


def test_helper_path_and_python_bin_are_server_fixed(tmp_path: Path, monkeypatch) -> None:
    # A malicious/compromised request cannot smuggle an alternate helper path or
    # interpreter -- these are constructor/CLI arguments of the server process,
    # never fields of ISOLATION_ISSUE_CREATE_REQUEST_V1 (see REQUEST_KEYS).
    assert "helper_path" not in bridge_server.REQUEST_KEYS
    assert "python_bin" not in bridge_server.REQUEST_KEYS
    assert "gh_bin" not in bridge_server.REQUEST_KEYS
    assert "repo_root" not in bridge_server.REQUEST_KEYS
