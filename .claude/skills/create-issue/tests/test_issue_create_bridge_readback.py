"""AC7: authoritative post-create readback (issue number / URL / node_id / body SHA).

Exercises the parent bridge server's execute_trusted_transaction() end to end
against a deterministic fake `gh` binary (no network, no real GitHub calls).
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts" / "claude-gpt"))

import issue_create_bridge_server as bridge_server  # noqa: E402

_FAKE_GH = Path(__file__).resolve().parent / "fixtures" / "fake_gh.py"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _fake_gh_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))


_MINIMAL_VALID_BODY = """## Acceptance Criteria

- [ ] AC1: Basic test

## Verification Commands

```bash
test -n "ok"  # AC1
```

## Allowed Paths

- src/**
"""


def _base_request(request_id: str = "r" * 32) -> dict:
    return {
        "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
        "request_id": request_id,
        "run_nonce": "nonce",
        "claimed_repo": "squne121/loop-protocol",
        "title": "実装: readback test issue",
        "body": _MINIMAL_VALID_BODY,
        "labels": [],
        "issue_kind": "",
        "label_profile": "standard",
        "parent_issue_number": None,
        "dependency_issue_numbers": [],
        "blocking_issue_numbers": [],
    }


def test_readback_returns_issue_number_url_node_id_and_body_sha(tmp_path: Path, monkeypatch) -> None:
    _fake_gh_env(tmp_path, monkeypatch)
    request = _base_request()
    result = bridge_server.execute_trusted_transaction(
        request=request,
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
    )
    assert result["status"] == "success"
    assert isinstance(result["issue_number"], int)
    assert result["issue_url"] == f"https://github.com/squne121/loop-protocol/issues/{result['issue_number']}"
    assert result["node_id"] == f"NODEID_{result['issue_number']}"
    assert result["body_sha256"] == hashlib.sha256(request["body"].encode("utf-8")).hexdigest()


def test_readback_gh_bin_is_split_correctly(tmp_path: Path, monkeypatch) -> None:
    # gh_bin (a single executable path with its own shebang) must be forwarded to
    # the helper subprocess's own --gh flag intact.
    _fake_gh_env(tmp_path, monkeypatch)
    request = _base_request(request_id="s" * 32)
    result = bridge_server.execute_trusted_transaction(
        request=request,
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
    )
    assert result["completed_steps"], "expected at least one completed step from the helper transaction"
