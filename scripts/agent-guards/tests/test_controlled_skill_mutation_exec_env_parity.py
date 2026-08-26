#!/usr/bin/env python3
"""Issue #2340 AC1: GitHub read/write subprocess env-sanitization parity.

Regression coverage for the P0 credential-parity bug: `_patch_issue_body()`
(the body-only PATCH used by `issue_body.update`) previously inherited the
caller's ambient environment while its sibling read helpers
(`_fetch_issue_content()` / `_fetch_issue_body_and_updated_at()`) and the
combined-write helper `_patch_issue_content()` explicitly passed
`env=_build_metadata_sanitized_env()`. A read and a write in the same
logical transaction could therefore execute under different GitHub
credential/host contexts (e.g. an inherited `GH_TOKEN` / `GH_HOST` /
`GH_CONFIG_DIR` override reaching only the write).

This file also covers the same-transaction issue comment publish route
(`_post_gh_comment`) and the cross-file env-sanitize-key parity between this
module and `.claude/skills/edit-issue/scripts/edit_issue_txn.py`
(In Scope item 3: pre-read `gh` calls in that module must use the same
sanitized env policy as the controlled executor they gate).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import controlled_skill_mutation_exec as _exec  # noqa: E402
from controlled_skill_mutation_policy import ENV_SANITIZE_KEYS  # noqa: E402

_EDIT_ISSUE_SCRIPTS_DIR = (
    Path(__file__).resolve().parents[3] / ".claude" / "skills" / "edit-issue" / "scripts"
)


def _fake_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    from subprocess import CompletedProcess

    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _GivenAmbientCredentialEnv:
    """GIVEN an ambient environment carrying GH_TOKEN / GH_HOST / GH_CONFIG_DIR
    overrides that must never reach a controlled `gh` subprocess call."""

    @staticmethod
    def apply(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "ambient-leaked-token")
        monkeypatch.setenv("GITHUB_TOKEN", "ambient-leaked-token-2")
        monkeypatch.setenv("GH_HOST", "evil.example.com")
        monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/attacker-controlled-gh-config")
        monkeypatch.setenv("GH_DEBUG", "1")


# =============================================================================
# WHEN each read/write GitHub subprocess helper runs, THEN it must invoke
# subprocess.run() with an explicit sanitized env= (Issue #2340 AC1).
# =============================================================================


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: _exec._fetch_issue_content(1, "squne121/loop-protocol", "gh"),
            id="fetch_issue_content",
        ),
        pytest.param(
            lambda: _exec._fetch_issue_body_and_updated_at(1, "squne121/loop-protocol", "gh"),
            id="fetch_issue_body_and_updated_at",
        ),
        pytest.param(
            lambda: _exec._patch_issue_body(1, "squne121/loop-protocol", "new body", "gh"),
            id="patch_issue_body",
        ),
        pytest.param(
            lambda: _exec._patch_issue_content(1, "squne121/loop-protocol", "title", "body", "gh"),
            id="patch_issue_content",
        ),
        pytest.param(
            lambda: _exec._post_gh_comment(1, "squne121/loop-protocol", "comment body", "gh"),
            id="post_gh_comment",
        ),
    ],
)
def test_gh_subprocess_helper_receives_explicit_sanitized_env(monkeypatch, call):
    """GIVEN an ambient env carrying credential/host overrides, WHEN a GitHub
    read or write helper in scope for #2340 AC1 runs, THEN subprocess.run is
    invoked with an explicit env= kwarg (not the ambient default) whose
    sanitize-sensitive keys are stripped."""
    _GivenAmbientCredentialEnv.apply(monkeypatch)

    with patch.object(_exec.subprocess, "run", return_value=_fake_completed(0, stdout="{}")) as mock_run:
        call()

    assert mock_run.call_count == 1
    _args, kwargs = mock_run.call_args
    assert "env" in kwargs, "subprocess.run must receive an explicit env= kwarg"
    env = kwargs["env"]
    assert env is not None, "env= must not be None (None means ambient-inherited)"
    for key in ENV_SANITIZE_KEYS:
        assert key not in env, f"{key} must be stripped from the sanitized subprocess env"
    # The ambient overrides set above must not have survived sanitization.
    assert env.get("GH_TOKEN") != "ambient-leaked-token"
    assert env.get("GH_HOST") != "evil.example.com"


def test_patch_issue_body_regression_env_was_previously_missing(monkeypatch):
    """Regression guard for the exact P0 bug: `_patch_issue_body` must not
    silently regress back to inheriting the ambient environment (which would
    make this call's credential context diverge from
    `_fetch_issue_body_and_updated_at`, its paired read in
    `_run_issue_body_update`)."""
    _GivenAmbientCredentialEnv.apply(monkeypatch)

    with patch.object(_exec.subprocess, "run", return_value=_fake_completed(0)) as mock_run:
        _exec._patch_issue_body(1, "squne121/loop-protocol", "new body", "gh")

    _args, kwargs = mock_run.call_args
    assert kwargs.get("env") == _exec._build_metadata_sanitized_env()


def test_read_and_write_use_identical_sanitized_env_policy(monkeypatch):
    """WHEN the read helper and the write helper are both invoked in the same
    process, THEN they build byte-identical sanitized env dicts (same policy,
    not merely both non-ambient)."""
    _GivenAmbientCredentialEnv.apply(monkeypatch)

    read_envs = []
    write_envs = []

    def _capture(kind, *args, **kwargs):
        (read_envs if kind == "read" else write_envs).append(kwargs.get("env"))
        return _fake_completed(0, stdout="{}")

    with patch.object(
        _exec.subprocess, "run", side_effect=lambda *a, **k: _capture("read", *a, **k)
    ):
        _exec._fetch_issue_body_and_updated_at(1, "squne121/loop-protocol", "gh")

    with patch.object(
        _exec.subprocess, "run", side_effect=lambda *a, **k: _capture("write", *a, **k)
    ):
        _exec._patch_issue_body(1, "squne121/loop-protocol", "new body", "gh")

    assert read_envs and write_envs
    assert read_envs[0] == write_envs[0]


# =============================================================================
# Cross-file parity: .claude/skills/edit-issue/scripts/edit_issue_txn.py must
# strip the same ambient-env keys before its direct `gh` pre-read calls
# (Issue #2340 In Scope item 3).
# =============================================================================


def test_edit_issue_txn_sanitize_key_list_matches_controlled_executor():
    if str(_EDIT_ISSUE_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_EDIT_ISSUE_SCRIPTS_DIR))
    import edit_issue_txn as _txn

    assert set(_txn._GH_ENV_SANITIZE_KEYS) == set(ENV_SANITIZE_KEYS)


def test_workflow_capability_preflight_sanitize_key_list_matches_controlled_executor():
    claude_gpt_dir = Path(__file__).resolve().parents[3] / "scripts" / "claude-gpt"
    if str(claude_gpt_dir) not in sys.path:
        sys.path.insert(0, str(claude_gpt_dir))
    import workflow_capability_preflight as _wcp

    assert set(_wcp._ENV_SANITIZE_KEYS) == set(ENV_SANITIZE_KEYS)


def test_edit_issue_txn_fetch_issue_uses_sanitized_env(monkeypatch):
    if str(_EDIT_ISSUE_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_EDIT_ISSUE_SCRIPTS_DIR))
    import edit_issue_txn as _txn

    _GivenAmbientCredentialEnv.apply(monkeypatch)

    captured = {}

    def _fake_run(args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _fake_completed(0, stdout='{"title": "t", "body": "b", "updatedAt": "2026-01-01T00:00:00Z"}')

    monkeypatch.setattr(_txn.subprocess, "run", _fake_run)
    _txn._fetch_issue(1, "squne121/loop-protocol")

    assert captured["env"] is not None
    for key in ENV_SANITIZE_KEYS:
        if key == "GH_HOST":
            # edit_issue_txn._sanitized_gh_env() strips the ambient GH_HOST
            # override and then explicitly pins the trusted host (gh issue
            # view has no --hostname flag, so pinning must go through the
            # env var instead).
            assert captured["env"]["GH_HOST"] == "github.com"
            continue
        assert key not in captured["env"]
