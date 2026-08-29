"""Timeout taxonomy compatibility for capability preflight."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_GUARDS_DIR = _REPO_ROOT / "scripts" / "agent-guards"
_UPDATE_BRANCH_SCRIPTS = _REPO_ROOT / ".claude" / "skills" / "implement-issue" / "scripts"

for _path in (_SCRIPTS_DIR, _GUARDS_DIR, _UPDATE_BRANCH_SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import update_branch  # noqa: E402
import workflow_capability_preflight as wcp  # noqa: E402


def _timeout_expired():
    return subprocess.TimeoutExpired(cmd=["gh"], timeout=15)


def test_controlled_github_read_capability_fails_closed_on_timeout(monkeypatch):
    monkeypatch.setattr(wcp.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(_timeout_expired()))
    result = wcp._controlled_github_read_capability("squne121/loop-protocol")
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "controlled_github_unavailable"


def test_deadline_probe_timeout_is_typed_not_uncaught(monkeypatch):
    monkeypatch.setattr(wcp.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(_timeout_expired()))
    deadline_ns = wcp.time.monotonic_ns() + 1_000_000_000
    assert wcp._github_auth_probe(deadline_ns).kind == wcp.PROBE_TIMEOUT
    assert wcp._github_repo_read_probe("squne121/loop-protocol", deadline_ns).kind == wcp.PROBE_TIMEOUT


def test_assess_preserves_fixed_decision_enum_and_timeout_reason(monkeypatch):
    monkeypatch.setattr(wcp.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(_timeout_expired()))
    monkeypatch.setattr(
        wcp.trusted_uv_mod,
        "check_trusted_uv",
        lambda _project_root: {
            "status": wcp.trusted_uv_mod.STATUS_OK,
            "reason": "resolved",
            "resolved_path": "/fake/uv",
        },
    )

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo="squne121/loop-protocol",
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )

    assert result["decision"] == wcp.DECISION_BLOCKED
    assert "preflight_probe_timeout:github_auth" in result["reasons"]
    assert "preflight_probe_timeout:controlled_github_read" in result["reasons"]


def test_update_branch_transport_error_reason_code_unrenamed():
    assert update_branch.REASON_TRANSPORT_ERROR == "transport_error"


# Issue #2401 AC6: the filename-based blacklist test that used to live here
# (asserting `shared_deadline.py` / `nested_timeout_manager.py` /
# `timeout_ownership.py` do not exist under this package or the guards
# package) is intentionally removed. Absence of a competing
# timeout-ownership module is confirmed once during PR review instead of
# enforced by a permanent filename blacklist test.
