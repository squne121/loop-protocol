#!/usr/bin/env python3
"""test_verify_pr_reviewer_permission_boundary.py -- Issue #1881 AC6/AC7.

AC6: runtime evidence stays allowlist-only (no raw transcript/prompt/HOME/
     credential leakage), and unavailable capability always yields SKIP
     (exit 77), never PASS.
AC7: the script declares a bounded claim scope (repo_local distribution,
     no new schema/digest/receipt/publisher/state store, no gh api/GraphQL/
     HTTP client/plugin/server-side-authorization claims).

PR #2385 review fix_delta:
- P1-2: the `git_worktree` canary command is a real member of the guard's
  deny-scoped worktree subcommand family but does not mutate real state
  (`--dry-run`), and is deliberately not `git worktree list` (read-only,
  never denied post-P1-2).
- P1-3: `classify_positive_case()` requires the runner's own structured
  `expected_markers_missing == []` evidence field, not a marker-substring
  search against captured stdout.

Issue #1881 contract refinement (this iteration): workspace-trust
registration/revocation (`register_worktree_trust` / `revoke_worktree_trust`)
was removed entirely and replaced with a read-only prerequisite check
(`is_worktree_trusted`). This script must never write to `~/.claude.json`
(or any fixture standing in for it), regardless of trust state. All tests
below use temp fixture files -- never the real ambient `~/.claude.json`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "agent-ops" / "verify_pr_reviewer_permission_boundary.py"

spec = importlib.util.spec_from_file_location("verify_pr_reviewer_permission_boundary", MODULE_PATH)
assert spec is not None and spec.loader is not None
verifier = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = verifier
spec.loader.exec_module(verifier)  # type: ignore[attr-defined]


def _write_fixture(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ─── PR #2385 fix_delta: translate_agent_definition_to_agents_json ──────────
# Issue #25816 (https://github.com/anthropics/claude-code/issues/25816)
# workaround: mechanical frontmatter+body -> --agents JSON translation.
# Uses a small synthetic fixture -- never the real pr-reviewer.md content
# (that is exercised only by the live runtime probe path).


_SYNTHETIC_AGENT_MD = """---
name: synthetic-agent
description: A synthetic test agent.
tools:
  - Bash
  - Read
disallowedTools:
  - Edit
model: sonnet
effort: high
permissionMode: dontAsk
skills:
  - some-skill
hooks: {PreToolUse: [{matcher: "Bash", hooks: [
  {type: command, if: "Bash(git commit *)", command: "${CLAUDE_PROJECT_DIR}/fake_guard.py",
   args: ["deny"], timeout: 5}
]}]}
---

You are a synthetic test agent.

## Section

Body text here.
"""

_SYNTHETIC_AGENT_MD_MINIMAL = """---
name: minimal-agent
description: Minimal synthetic agent with no optional fields.
---

Minimal body.
"""


class TestTranslateAgentDefinitionToAgentsJson:
    def test_expected_json_shape_and_passthrough_fields(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "synthetic-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "synthetic-agent")

        assert set(result.keys()) == {"synthetic-agent"}
        payload = result["synthetic-agent"]
        assert payload["description"] == "A synthetic test agent."
        assert payload["prompt"] == (
            "You are a synthetic test agent.\n\n## Section\n\nBody text here."
        )
        assert payload["tools"] == ["Bash", "Read"]
        assert payload["disallowedTools"] == ["Edit"]
        assert payload["model"] == "sonnet"
        assert payload["permissionMode"] == "dontAsk"

    def test_hooks_passed_through_verbatim(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "synthetic-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "synthetic-agent")
        payload = result["synthetic-agent"]

        assert payload["hooks"] == {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "if": "Bash(git commit *)",
                            "command": "${CLAUDE_PROJECT_DIR}/fake_guard.py",
                            "args": ["deny"],
                            "timeout": 5,
                        }
                    ],
                }
            ]
        }

    def test_absent_fields_are_excluded_not_null(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "minimal-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD_MINIMAL, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "minimal-agent")
        payload = result["minimal-agent"]

        assert payload == {
            "description": "Minimal synthetic agent with no optional fields.",
            "prompt": "Minimal body.",
        }
        for absent_field in ("tools", "disallowedTools", "model", "permissionMode", "hooks"):
            assert absent_field not in payload

    def test_no_invented_fields_like_name_or_skills(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "synthetic-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "synthetic-agent")
        payload = result["synthetic-agent"]

        assert set(payload.keys()) == {
            "description",
            "prompt",
            "tools",
            "disallowedTools",
            "model",
            "permissionMode",
            "hooks",
        }

    def test_missing_frontmatter_delimiters_raises_value_error(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "broken-agent.md"
        agent_md.write_text("no frontmatter here at all\n", encoding="utf-8")

        with pytest.raises(ValueError):
            verifier.translate_agent_definition_to_agents_json(agent_md, "broken-agent")

    def test_missing_description_raises_value_error(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "no-description-agent.md"
        agent_md.write_text("---\nname: no-description-agent\n---\n\nBody.\n", encoding="utf-8")

        with pytest.raises(ValueError):
            verifier.translate_agent_definition_to_agents_json(agent_md, "no-description-agent")


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


# ─── PR #2385 review fix_delta P1-2: git_worktree canary command ───────────


class TestGitWorktreeCanaryCommand:
    def test_canary_is_a_mutation_family_subcommand_not_list(self) -> None:
        command = verifier.CASE_COMMANDS["git_worktree"]
        assert command != "git worktree list", (
            "git worktree list is read-only and never denied post-P1-2; it "
            "is no longer a valid confirmed-deny canary signal"
        )
        assert command.split()[:2] == ["git", "worktree"]
        subcommand = command.split()[2]
        assert subcommand in {"add", "remove", "move", "prune", "repair", "lock", "unlock"}

    def test_canary_command_is_dry_run_safe(self) -> None:
        assert "--dry-run" in verifier.CASE_COMMANDS["git_worktree"]


# ─── PR #2385 review fix_delta P1-3: classify_positive_case structured match ──


class TestClassifyPositiveCaseStructuredMatch:
    def _base_result(self, **overrides: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "process_error": None,
            "exit_code": verifier.EXIT_OK,
            "marker_observed": True,
            "evidence": {"expected_markers_missing": []},
        }
        result.update(overrides)
        return result

    def test_pass_requires_empty_expected_markers_missing(self) -> None:
        result = self._base_result()
        assert verifier.classify_positive_case(result) == "pass"

    def test_marker_observed_true_alone_is_not_sufficient(self) -> None:
        """P1-3: a truthy `marker_observed` (stdout substring match) without
        the runner's own structured `expected_markers_missing == []`
        evidence must NOT be classified as pass."""
        result = self._base_result(evidence={"expected_markers_missing": ["reviewer-reference-read-ok"]})
        assert verifier.classify_positive_case(result) != "pass"

    def test_missing_evidence_field_is_inconclusive_not_pass(self) -> None:
        result = self._base_result(evidence={})
        assert verifier.classify_positive_case(result) == "inconclusive"

    def test_non_list_expected_markers_missing_is_inconclusive_not_pass(self) -> None:
        result = self._base_result(evidence={"expected_markers_missing": None})
        assert verifier.classify_positive_case(result) == "inconclusive"

    def test_exit_fail_is_fail_regardless_of_evidence(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_FAIL)
        assert verifier.classify_positive_case(result) == "fail"

    def test_exit_skip_is_inconclusive(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_SKIP)
        assert verifier.classify_positive_case(result) == "inconclusive"


# ─── AC6: no_authority_artifacts_or_sensitive_output ────────────────────────


class TestNoAuthorityArtifactsOrSensitiveOutput:
    def test_no_authority_artifacts_or_sensitive_output(self, tmp_path: Path) -> None:
        artifacts_dir = tmp_path / "artifacts"
        log_path = verifier.write_artifact_log(
            artifacts_dir=artifacts_dir,
            ac="AC4",
            result="SKIP",
            exit_code=77,
            reason="worktree_trust_prerequisite_missing",
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
            lambda *a, **kw: (False, "worktree_trust_prerequisite_missing", {"worktree_trusted": False}),
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


# ─── register/revoke removal (this iteration's fix_delta) ──────────────────


class TestTrustMutationLogicFullyRemoved:
    def test_register_worktree_trust_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "register_worktree_trust")

    def test_revoke_worktree_trust_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "revoke_worktree_trust")


# ─── is_worktree_trusted(): read-only prerequisite check ───────────────────


class TestIsWorktreeTrusted:
    def test_trusted_exact_match_returns_true(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        _write_fixture(
            claude_json,
            {"projects": {worktree_abs: {"hasTrustDialogAccepted": True}}},
        )
        assert verifier.is_worktree_trusted(claude_json, worktree_abs) is True

    def test_untrusted_flag_false_returns_false(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        _write_fixture(
            claude_json,
            {"projects": {worktree_abs: {"hasTrustDialogAccepted": False}}},
        )
        assert verifier.is_worktree_trusted(claude_json, worktree_abs) is False

    def test_missing_entry_returns_false(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        _write_fixture(claude_json, {"projects": {}})
        assert verifier.is_worktree_trusted(claude_json, "/some/worktree/path") is False

    def test_missing_projects_key_fails_closed(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        _write_fixture(claude_json, {})
        assert verifier.is_worktree_trusted(claude_json, "/some/worktree/path") is False

    def test_non_bool_trust_flag_fails_closed(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        for bogus_value in ("true", 1, ["yes"], None):
            _write_fixture(
                claude_json,
                {"projects": {worktree_abs: {"hasTrustDialogAccepted": bogus_value}}},
            )
            assert verifier.is_worktree_trusted(claude_json, worktree_abs) is False, bogus_value

    def test_non_dict_project_entry_fails_closed(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        _write_fixture(claude_json, {"projects": {worktree_abs: "not-a-dict"}})
        assert verifier.is_worktree_trusted(claude_json, worktree_abs) is False

    def test_malformed_json_fails_closed_not_crash(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        claude_json.write_text("{not valid json", encoding="utf-8")
        assert verifier.is_worktree_trusted(claude_json, "/some/worktree/path") is False

    def test_missing_file_fails_closed_not_crash(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "does-not-exist.json"
        assert verifier.is_worktree_trusted(claude_json, "/some/worktree/path") is False

    def test_unrelated_project_entry_is_not_authority(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        our_worktree = "/some/worktree/path"
        other_worktree = "/some/other/worktree/path"
        _write_fixture(
            claude_json,
            {
                "projects": {
                    other_worktree: {"hasTrustDialogAccepted": True},
                }
            },
        )
        assert verifier.is_worktree_trusted(claude_json, our_worktree) is False

    def test_never_writes_fixture_file(self, tmp_path: Path) -> None:
        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        _write_fixture(
            claude_json,
            {"projects": {worktree_abs: {"hasTrustDialogAccepted": True}}},
        )
        before_mtime = claude_json.stat().st_mtime_ns
        before_content = claude_json.read_bytes()

        for _ in range(3):
            verifier.is_worktree_trusted(claude_json, worktree_abs)

        assert claude_json.stat().st_mtime_ns == before_mtime
        assert claude_json.read_bytes() == before_content


# ─── preflight_capability(): trust becomes the gate, not process count ─────


class TestPreflightCapability:
    def _stub_claude_and_gh_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        class _FakeCompleted:
            returncode = 0

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeCompleted())

    def test_trusted_worktree_with_concurrent_claude_processes_still_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_claude_and_gh_ok(monkeypatch)
        # Simulate concurrent `claude` processes: this must NOT gate SKIP
        # anymore now that no write to ~/.claude.json ever happens.
        monkeypatch.setattr(verifier, "other_live_claude_processes", lambda *a, **kw: [111, 222, 333])

        claude_json = tmp_path / "claude.json"
        worktree_abs = "/some/worktree/path"
        _write_fixture(
            claude_json,
            {"projects": {worktree_abs: {"hasTrustDialogAccepted": True}}},
        )

        available, reason, detail = verifier.preflight_capability(
            worktree_abs, claude_json_path=claude_json
        )
        assert available is True
        assert reason == ""
        assert detail["worktree_trusted"] is True

    def test_untrusted_worktree_returns_worktree_trust_prerequisite_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_claude_and_gh_ok(monkeypatch)
        claude_json = tmp_path / "claude.json"
        _write_fixture(claude_json, {"projects": {}})

        available, reason, detail = verifier.preflight_capability(
            "/some/worktree/path", claude_json_path=claude_json
        )
        assert available is False
        assert reason == "worktree_trust_prerequisite_missing"
        assert detail["worktree_trusted"] is False

    def test_malformed_trust_state_returns_prerequisite_missing_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_claude_and_gh_ok(monkeypatch)
        claude_json = tmp_path / "claude.json"
        claude_json.write_text("{not valid json", encoding="utf-8")

        available, reason, detail = verifier.preflight_capability(
            "/some/worktree/path", claude_json_path=claude_json
        )
        assert available is False
        assert reason == "worktree_trust_prerequisite_missing"

    def test_missing_claude_binary_short_circuits_before_trust_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: None)
        claude_json = tmp_path / "claude.json"
        _write_fixture(claude_json, {"projects": {}})

        available, reason, _detail = verifier.preflight_capability(
            "/some/worktree/path", claude_json_path=claude_json
        )
        assert available is False
        assert reason == "claude_binary_not_found"


# ─── Full main() round-trip: never writes ~/.claude.json fixture ───────────


class TestMainNeverWritesClaudeJsonFixture:
    def _run_main_with_trusted_fixture(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        trusted: bool,
        extra_args: list[str] | None = None,
    ) -> tuple[int, Path]:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        claude_json = tmp_path / "claude.json"
        if trusted:
            _write_fixture(
                claude_json,
                {"projects": {str(worktree): {"hasTrustDialogAccepted": True}}},
            )
        else:
            _write_fixture(claude_json, {"projects": {}})

        monkeypatch.setattr(verifier, "_claude_json_path", lambda: claude_json)
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        class _FakeCompleted:
            returncode = 0

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeCompleted())

        # Avoid spawning the real smoke runner: simulate a confirmed-deny
        # canary result directly.
        def _fake_run_runtime_case(**kwargs: Any) -> dict[str, Any]:
            return {
                "case": kwargs["case_name"],
                "exit_code": verifier.EXIT_OK,
                "process_error": None,
                "marker_observed": True,
                "evidence": {"expected_markers_missing": []},
            }

        monkeypatch.setattr(verifier, "run_runtime_case", _fake_run_runtime_case)

        args = [
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
        if extra_args:
            args.extend(extra_args)

        before_mtime = claude_json.stat().st_mtime_ns
        before_content = claude_json.read_bytes()
        exit_code = verifier.main(args)
        assert claude_json.stat().st_mtime_ns == before_mtime
        assert claude_json.read_bytes() == before_content
        return exit_code, claude_json

    def test_trusted_path_proceeds_and_never_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code, _claude_json = self._run_main_with_trusted_fixture(
            tmp_path, monkeypatch, trusted=True
        )
        assert exit_code == verifier.EXIT_OK

    def test_untrusted_path_skips_and_never_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code, _claude_json = self._run_main_with_trusted_fixture(
            tmp_path, monkeypatch, trusted=False
        )
        assert exit_code == verifier.EXIT_SKIP

    def test_revoke_flag_causes_no_mutation_attempt_and_no_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code, _claude_json = self._run_main_with_trusted_fixture(
            tmp_path,
            monkeypatch,
            trusted=True,
            extra_args=["--revoke-worktree-trust-after"],
        )
        assert exit_code == verifier.EXIT_OK

    def test_revoke_flag_with_untrusted_worktree_still_skips_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code, _claude_json = self._run_main_with_trusted_fixture(
            tmp_path,
            monkeypatch,
            trusted=False,
            extra_args=["--revoke-worktree-trust-after"],
        )
        assert exit_code == verifier.EXIT_SKIP

    def test_no_write_text_call_targets_claude_json_fixture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert no code path opens the claude.json fixture in write mode,
        by wrapping Path.write_text and recording any target path equal to
        the fixture."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        claude_json = tmp_path / "claude.json"
        _write_fixture(
            claude_json,
            {"projects": {str(worktree): {"hasTrustDialogAccepted": True}}},
        )
        monkeypatch.setattr(verifier, "_claude_json_path", lambda: claude_json)
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        class _FakeCompleted:
            returncode = 0

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeCompleted())

        def _fake_run_runtime_case(**kwargs: Any) -> dict[str, Any]:
            return {
                "case": kwargs["case_name"],
                "exit_code": verifier.EXIT_OK,
                "process_error": None,
                "marker_observed": True,
                "evidence": {"expected_markers_missing": []},
            }

        monkeypatch.setattr(verifier, "run_runtime_case", _fake_run_runtime_case)

        original_write_text = Path.write_text
        write_targets: list[Path] = []

        def _tracking_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            write_targets.append(self)
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _tracking_write_text)

        verifier.main(
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
                "--revoke-worktree-trust-after",
            ]
        )

        assert claude_json.resolve() not in {p.resolve() for p in write_targets}


# ─── Sensitive fixture content must never leak into stdout/artifact log ───


class TestClaudeJsonContentNeverLeaks:
    def test_claude_json_shaped_content_not_in_stdout_or_artifact_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        claude_json = tmp_path / "claude.json"
        sentinel = "SUPER_SECRET_MCP_TOKEN_VALUE_DO_NOT_LEAK"
        _write_fixture(
            claude_json,
            {
                "projects": {
                    str(worktree): {
                        "hasTrustDialogAccepted": True,
                        "mcpServers": {"token": sentinel},
                    }
                },
                "oauthAccount": {"secret": sentinel},
            },
        )
        monkeypatch.setattr(verifier, "_claude_json_path", lambda: claude_json)
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        class _FakeCompleted:
            returncode = 0

        monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **kw: _FakeCompleted())

        def _fake_run_runtime_case(**kwargs: Any) -> dict[str, Any]:
            return {
                "case": kwargs["case_name"],
                "exit_code": verifier.EXIT_OK,
                "process_error": None,
                "marker_observed": True,
                "evidence": {"expected_markers_missing": []},
            }

        monkeypatch.setattr(verifier, "run_runtime_case", _fake_run_runtime_case)

        verifier.main(
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

        captured = capsys.readouterr()
        assert sentinel not in captured.out
        assert sentinel not in captured.err

        artifact_files = list((worktree / "artifacts").glob("runtime-verification-*.log"))
        assert len(artifact_files) == 1
        assert sentinel not in artifact_files[0].read_text(encoding="utf-8")


# ─── Concurrent-process diagnostic (retained, non-gating) ──────────────────


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
