#!/usr/bin/env python3
"""scripts/agent-guards/tests/test_capability_artifact_sanitization.py

Issue #2340 AC5: actor capability artifact / worklog must never persist
credential/token/GH_CONFIG_DIR content or raw auth stderr -- only
reason_code, command class, and a bounded sanitized diagnostic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

_GUARDS_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _GUARDS_DIR.parent.parent
_CLAUDE_GPT_DIR = _REPO_ROOT / "scripts" / "claude-gpt"

for _p in (_GUARDS_DIR, _CLAUDE_GPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import controlled_skill_mutation_exec as _exec  # noqa: E402
import workflow_capability_preflight as wcp  # noqa: E402

_FAKE_PAT = "ghp_" + ("A" * 36)
_FAKE_BEARER = "Bearer " + ("b" * 40)


def test_classify_gh_error_redacts_github_pat_shaped_substring():
    stderr = f"error talking to github: token {_FAKE_PAT} rejected"
    classified = _exec._classify_gh_error("gh_api_patch_failed", stderr)
    assert _FAKE_PAT not in classified
    assert "[REDACTED]" in classified


def test_classify_gh_error_redacts_bearer_header():
    stderr = f"request failed; header Authorization: {_FAKE_BEARER}"
    classified = _exec._classify_gh_error("gh_api_patch_failed", stderr)
    assert _FAKE_BEARER not in classified
    assert "[REDACTED]" in classified


def test_classify_gh_error_known_http_status_never_includes_raw_stderr():
    """403/404/410/422/429/503 branches return a fixed canonical string --
    the raw stderr (which could carry request/response body content) must
    never appear in the returned reason code."""
    stderr_with_secret = f"HTTP 403: token={_FAKE_PAT}"
    classified = _exec._classify_gh_error("gh_api_patch_failed", stderr_with_secret)
    assert classified == "gh_api_patch_failed_permission_denied_http_403"
    assert _FAKE_PAT not in classified


def test_classify_gh_error_unknown_pattern_is_bounded():
    huge_stderr = "x" * 5000
    classified = _exec._classify_gh_error("gh_api_patch_failed", huge_stderr)
    assert len(classified) < 300


# =============================================================================
# actor_capabilities (Issue #2340 AC2) must never carry secret-like content,
# even when the environment/subprocess surface leaks one.
# =============================================================================


def test_actor_capabilities_result_json_never_contains_ambient_secrets(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "definitely-a-secret-value-12345")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/attacker/.config/gh-fake")
    monkeypatch.setattr(wcp, "_github_auth_ok", lambda: True)
    monkeypatch.setattr(wcp, "_github_repo_read_ok", lambda repo: True)
    monkeypatch.setattr(
        wcp.trusted_uv_mod,
        "check_trusted_uv",
        lambda project_root: {
            "status": wcp.trusted_uv_mod.STATUS_OK, "reason": "resolved", "resolved_path": "/fake/uv",
        },
    )
    monkeypatch.setattr(wcp, "_run_env_only_preflight", lambda: {})

    def _fake_run(argv, **kwargs):
        # The probe's own env kwarg must not leak into stdout/stderr either.
        return __import__("subprocess").CompletedProcess(
            argv, 1, stdout="", stderr="fatal: authentication failed for definitely-a-secret-value-12345"
        )

    monkeypatch.setattr(wcp.subprocess, "run", _fake_run)

    result = wcp.assess(
        project_root=str(_REPO_ROOT),
        profile="issue-to-impl",
        repo="squne121/loop-protocol",
        spark_mode=None,
        spark_fallback=None,
        planned_operations=[],
    )

    serialized = json.dumps(result)
    assert "definitely-a-secret-value-12345" not in serialized
    assert "/home/attacker/.config/gh-fake" not in serialized
    for entry in result["actor_capabilities"].values():
        assert set(entry.keys()) == {"status", "reason_code", "fallback_route", "probe_execution_class"}
        # reason_code is a bounded, closed-vocabulary-shaped string, never a
        # raw stderr passthrough.
        if entry["reason_code"] is not None:
            assert len(entry["reason_code"]) < 100


def test_patch_issue_body_error_path_never_leaks_stderr_into_return_value(monkeypatch):
    """The `issue_body.update` failure path (`_run_issue_body_update`'s
    `_fail(patch_err, ...)`) stores whatever `_patch_issue_body` returns as
    its error string -- confirm that string is the sanitized classification,
    never the raw `gh` stderr, even when stderr contains secret-shaped
    content."""

    def _fake_run(argv, **kwargs):
        return __import__("subprocess").CompletedProcess(
            argv, 1, stdout="", stderr=f"error: bad credentials ({_FAKE_PAT})"
        )

    with patch.object(_exec.subprocess, "run", side_effect=_fake_run):
        err = _exec._patch_issue_body(1, "squne121/loop-protocol", "new body", "gh")

    assert _FAKE_PAT not in err
    assert "[REDACTED]" in err
