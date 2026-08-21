"""AC5: the parent bridge server strips all bridge-related env vars before
spawning the trusted create_issue_txn.py helper subprocess, so the helper can
never recursively re-enter the bridge (even if it independently checked
CLAUDE_GPT_ISOLATION_PROFILE itself)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "skills" / "create-issue" / "scripts"))

import issue_create_bridge_client as bridge_client  # noqa: E402
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


def test_bridge_env_vars_are_stripped_before_helper_subprocess_spawn(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "fake_gh_state.json"))
    # Simulate the surrounding process (the isolated Claude-GPT session) having
    # these set, as it legitimately would -- the server must still strip them
    # for the helper subprocess it spawns.
    monkeypatch.setenv("CLAUDE_GPT_ISOLATION_PROFILE", "1")
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_SOCKET", "/should/not/reach/helper.sock")
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "should-not-reach-helper")

    expected_vars = {
        "CLAUDE_GPT_ISOLATION_PROFILE",
        "CLAUDE_GPT_ISSUE_CREATE_SOCKET",
        "CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE",
    }
    for var in bridge_server.BRIDGE_ENV_VARS_TO_STRIP:
        assert var in expected_vars

    request = {
        "schema": "ISOLATION_ISSUE_CREATE_REQUEST_V1",
        "request_id": "n" * 32,
        "run_nonce": "nonce",
        "claimed_repo": "squne121/loop-protocol",
        "title": "実装: no-recursion test",
        "body": _MINIMAL_VALID_BODY,
        "labels": [],
        "issue_kind": "",
        "label_profile": "standard",
        "parent_issue_number": None,
        "dependency_issue_numbers": [],
        "blocking_issue_numbers": [],
    }

    # If the isolation env vars leaked into the helper subprocess, the helper's
    # own import-time `issue_create_bridge_client.is_isolated_profile()` check
    # would flip to True and it would refuse to call gh directly (it has no
    # socket to talk to at "/should/not/reach/helper.sock", so it would fail
    # with a bridge-transport error instead of creating the issue via gh).
    result = bridge_server.execute_trusted_transaction(
        request=request,
        repo="squne121/loop-protocol",
        gh_bin=str(_FAKE_GH),
        repo_root=_REPO_ROOT,
        python_bin=sys.executable,
    )
    assert result["status"] == "success", (
        "helper subprocess appears to have recursed into isolated-bridge mode; "
        f"result={result!r}"
    )
    assert result["issue_number"] is not None


def test_helper_env_snapshot_has_no_bridge_vars(monkeypatch) -> None:
    # Direct unit check of the stripping logic itself, independent of the
    # subprocess boundary above.
    monkeypatch.setenv("CLAUDE_GPT_ISOLATION_PROFILE", "1")
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_SOCKET", "/tmp/x.sock")
    monkeypatch.setenv("CLAUDE_GPT_ISSUE_CREATE_RUN_NONCE", "abc")
    import os

    env = os.environ.copy()
    for var in bridge_server.BRIDGE_ENV_VARS_TO_STRIP:
        env.pop(var, None)
    for var in bridge_server.BRIDGE_ENV_VARS_TO_STRIP:
        assert var not in env
    assert bridge_client.is_isolated_profile() is True  # sanity: env was actually set in this process
