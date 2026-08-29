#!/usr/bin/env python3
"""test_verify_pr_reviewer_permission_boundary.py -- Issue #1881 AC6/AC7.

AC6: runtime evidence stays allowlist-only (no raw transcript/prompt/HOME/
     credential leakage), and unavailable capability always yields SKIP
     (exit 77), never PASS.
AC7: the script declares a bounded claim scope (repo_local distribution,
     no new schema/digest/receipt/publisher/state store, no gh api/GraphQL/
     HTTP client/plugin/server-side-authorization claims).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "agent-ops" / "verify_pr_reviewer_permission_boundary.py"

spec = importlib.util.spec_from_file_location("verify_pr_reviewer_permission_boundary", MODULE_PATH)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)  # type: ignore[attr-defined]


# ─── AC7: bounded_claim_scope_declared ──────────────────────────────────────


class TestBoundedClaimScopeDeclared:
    def test_bounded_claim_scope_declared(self) -> None:
        scope = verifier.BOUNDED_CLAIM_SCOPE
        assert scope["distribution_scope"] == "repo_local"
        for flag in (
            "new_schema",
            "new_digest",
            "new_receipt",
            "new_publisher",
            "new_state_store",
            "arbitrary_subprocess_claim",
            "gh_api_or_graphql_used",
            "http_client_used",
            "server_side_authorization_claim",
            "credential_scope_claim",
            "plugin_distribution",
        ):
            assert scope[flag] is False, f"{flag} must be declared False"


# ─── AC6: no_authority_artifacts_or_sensitive_output ────────────────────────


class TestNoAuthorityArtifactsOrSensitiveOutput:
    def test_no_authority_artifacts_or_sensitive_output(self, tmp_path: Path) -> None:
        artifacts_dir = tmp_path / "artifacts"
        log_path = verifier.write_artifact_log(
            artifacts_dir=artifacts_dir,
            ac="AC4",
            result="SKIP",
            exit_code=77,
            reason="concurrent_claude_processes_detected",
            input_summary="case=positive_reference_read",
            output_summary="{}",
        )
        text = log_path.read_text(encoding="utf-8")

        # Only the allowlisted log sections are present.
        assert "=== Runtime Verification Log ===" in text
        assert "--- Verdict ---" in text

        # No raw transcript/prompt/session/HOME/credential leakage.
        forbidden_substrings = [
            str(Path.home()),
            "session_id",
            "transcript_path",
            "ANTHROPIC_API_KEY",
            "credentials",
        ]
        for forbidden in forbidden_substrings:
            assert forbidden not in text, f"forbidden substring leaked into artifact log: {forbidden!r}"

        # write_artifact_log() only ever emits the allowlisted field names as
        # scalar values -- it cannot embed a session-manifest, schema, or
        # receipt-shaped structure.
        assert verifier.ALLOWLISTED_ARTIFACT_FIELDS == {
            "ac",
            "timestamp",
            "environment",
            "input_summary",
            "output_summary",
            "result",
            "exit_code",
            "reason",
        }

    def test_unavailable_is_skip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            verifier,
            "preflight_capability",
            lambda: (False, "concurrent_claude_processes_detected", {"other_live_claude_pid_count": 3}),
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        exit_code = verifier.main(
            [
                "--runtime",
                "claude",
                "--mode",
                "structured",
                "--claude-agent-name",
                "pr-reviewer",
                "--case",
                "positive_reference_read",
                "--expect-marker",
                "reviewer-reference-read-ok",
                "--require-clean-postcondition",
                "--revoke-worktree-trust-after",
                "--worktree",
                str(worktree),
            ]
        )
        assert exit_code == verifier.EXIT_SKIP
        assert exit_code != verifier.EXIT_OK

        artifact_files = list((worktree / "artifacts").glob("runtime-verification-AC4-*.log"))
        assert len(artifact_files) == 1
        assert "Result: SKIP" in artifact_files[0].read_text(encoding="utf-8")

    def test_unavailable_never_registers_trust(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKIP-before-registration means ~/.claude.json is never touched."""
        register_calls: list[tuple] = []
        monkeypatch.setattr(
            verifier,
            "preflight_capability",
            lambda: (False, "claude_binary_not_found", {}),
        )
        monkeypatch.setattr(
            verifier,
            "register_worktree_trust",
            lambda *a, **kw: register_calls.append((a, kw)) or (False, None),
        )
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        exit_code = verifier.main(
            [
                "--runtime",
                "claude",
                "--mode",
                "structured",
                "--claude-agent-name",
                "pr-reviewer",
                "--case",
                "positive_reference_read",
                "--expect-marker",
                "reviewer-reference-read-ok",
                "--worktree",
                str(worktree),
            ]
        )
        assert exit_code == verifier.EXIT_SKIP
        assert register_calls == []


# ─── Functional round-trip: trust registration/cleanup (fixture, not real HOME) ──


class TestWorktreeTrustRoundTrip:
    def test_register_new_entry_then_revoke_removes_it(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        claude_json.write_text(json.dumps({"projects": {}}), encoding="utf-8")

        worktree_abs = "/some/worktree/path"
        existed_before, original_entry = verifier.register_worktree_trust(claude_json, worktree_abs)
        assert existed_before is False
        assert original_entry is None

        data = json.loads(claude_json.read_text(encoding="utf-8"))
        assert data["projects"][worktree_abs]["hasTrustDialogAccepted"] is True

        verifier.revoke_worktree_trust(claude_json, worktree_abs, existed_before, original_entry)
        data_after = json.loads(claude_json.read_text(encoding="utf-8"))
        assert worktree_abs not in data_after["projects"]

    def test_register_existing_entry_then_revoke_restores_exact_prior_state(
        self, tmp_path: Path
    ) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        prior_entry = {
            "allowedTools": [],
            "hasTrustDialogAccepted": False,
            "lastCost": 4.2,
        }
        claude_json.write_text(
            json.dumps({"projects": {worktree_abs: prior_entry}}), encoding="utf-8"
        )

        existed_before, original_entry = verifier.register_worktree_trust(claude_json, worktree_abs)
        assert existed_before is True
        assert original_entry == prior_entry

        data = json.loads(claude_json.read_text(encoding="utf-8"))
        assert data["projects"][worktree_abs]["hasTrustDialogAccepted"] is True
        assert data["projects"][worktree_abs]["lastCost"] == 4.2

        verifier.revoke_worktree_trust(claude_json, worktree_abs, existed_before, original_entry)
        data_after = json.loads(claude_json.read_text(encoding="utf-8"))
        assert data_after["projects"][worktree_abs] == prior_entry


# ─── Concurrent-process safety check ────────────────────────────────────────


class TestOtherLiveClaudeProcesses:
    def test_excludes_self_and_ancestors(self) -> None:
        self_and_ancestors = verifier._self_and_ancestor_pids()
        assert isinstance(self_and_ancestors, set)
        assert __import__("os").getpid() in self_and_ancestors

    def test_returns_list_of_ints(self) -> None:
        result = verifier.other_live_claude_processes()
        assert isinstance(result, list)
        for pid in result:
            assert isinstance(pid, int)
