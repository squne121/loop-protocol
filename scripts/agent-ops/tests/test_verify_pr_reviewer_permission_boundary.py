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

PR #2385 fix_delta (this iteration -- contract refinement, body_sha256
sha256:105def95ae4294b2cbdbb24dd0a75128d5443ec5b22ef858b469dd70acb1b3c8):
- The workspace-trust prerequisite (`is_worktree_trusted`,
  `_claude_json_path`, and the `~/.claude.json`-reading branch of
  `preflight_capability`) has been removed entirely -- this script never
  reads or writes `~/.claude.json` (or any fixture standing in for it) in
  any code path anymore.
- `gh auth status` is no longer part of `preflight_capability` either --
  the only remaining genuine capability prerequisite is the `claude`
  binary itself.
- `translate_agent_definition_to_agents_json` now passes through the FULL
  officially-documented `--agents` JSON field set (including `skills` and
  `effort`, which a prior iteration silently dropped), still excluding
  only `name` (the JSON object's own key) and `description`/`prompt`
  (handled as their own explicit top-level fields).
- `classify_positive_case` / `classify_deny_case` no longer depend on
  `--expect-marker`/`expected_markers_missing` (which required the
  now-removed, structurally-unsatisfiable-for-a-main-session-persona
  SubagentStart/SubagentStop causal-evidence gate). They now read
  `main_agent_identity` / `skill_evidence.canonical_read` /
  `mutation_boundary` directly, honestly returning `'inconclusive'`
  whenever a field they depend on reports `status: "unavailable"`.
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

# Exercises the remaining officially-documented fields not covered by
# _SYNTHETIC_AGENT_MD above (maxTurns, mcpServers, memory, background,
# isolation, color, initialPrompt, experimental) -- Issue #1881 PR #2385
# fix_delta: a prior iteration silently dropped every field below.
_SYNTHETIC_AGENT_MD_EXTENDED_FIELDS = """---
name: extended-agent
description: A synthetic agent exercising the remaining official fields.
maxTurns: 12
mcpServers:
  some-server:
    command: some-command
memory: project
background: true
isolation: worktree
color: blue
initialPrompt: Say hello.
experimental:
  someFlag: true
---

Extended body.
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

    def test_skills_and_effort_passed_through(self, tmp_path: Path) -> None:
        """PR #2385 fix_delta: `skills`/`effort` were silently dropped by a
        prior iteration's hardcoded 5-field passthrough list, even though
        the candidate `.claude/agents/pr-reviewer.md` uses both."""
        agent_md = tmp_path / "synthetic-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "synthetic-agent")
        payload = result["synthetic-agent"]

        assert payload["skills"] == ["some-skill"]
        assert payload["effort"] == "high"

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

    def test_extended_official_fields_passed_through_verbatim(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "extended-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD_EXTENDED_FIELDS, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "extended-agent")
        payload = result["extended-agent"]

        assert payload["maxTurns"] == 12
        assert payload["mcpServers"] == {"some-server": {"command": "some-command"}}
        assert payload["memory"] == "project"
        assert payload["background"] is True
        assert payload["isolation"] == "worktree"
        assert payload["color"] == "blue"
        assert payload["initialPrompt"] == "Say hello."
        assert payload["experimental"] == {"someFlag": True}

    def test_absent_fields_are_excluded_not_null(self, tmp_path: Path) -> None:
        agent_md = tmp_path / "minimal-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD_MINIMAL, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "minimal-agent")
        payload = result["minimal-agent"]

        assert payload == {
            "description": "Minimal synthetic agent with no optional fields.",
            "prompt": "Minimal body.",
        }
        for absent_field in verifier._AGENTS_JSON_PASSTHROUGH_FRONTMATTER_FIELDS:
            assert absent_field not in payload

    def test_name_field_excluded_but_skills_and_effort_included(self, tmp_path: Path) -> None:
        """PR #2385 fix_delta: `name` must never appear in the payload (it
        becomes the JSON object's own key), while `skills`/`effort` --
        previously invented-as-excluded by a prior iteration's incorrect
        comment -- ARE officially-supported fields and must be included."""
        agent_md = tmp_path / "synthetic-agent.md"
        agent_md.write_text(_SYNTHETIC_AGENT_MD, encoding="utf-8")

        result = verifier.translate_agent_definition_to_agents_json(agent_md, "synthetic-agent")
        payload = result["synthetic-agent"]

        assert "name" not in payload
        assert set(payload.keys()) == {
            "description",
            "prompt",
            "tools",
            "disallowedTools",
            "model",
            "permissionMode",
            "hooks",
            "skills",
            "effort",
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


# ─── PR #2385 fix_delta: classify_positive_case evidence-field validation ──


class TestClassifyPositiveCase:
    def _base_evidence(self, **overrides: Any) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "main_agent_identity": {
                "requested": {"agent_name": "pr-reviewer", "source": "runner_argv"},
                "observed": {"agent_type": "pr-reviewer", "source": "hook_payload", "status": "observed"},
                "matched": True,
                "status": "observed",
            },
            "skill_evidence": {
                "canonical_read": {
                    "expected_repo_relative_path": verifier.CANONICAL_REFERENCE_RELATIVE_PATH,
                    "observed_repo_relative_path": verifier.CANONICAL_REFERENCE_RELATIVE_PATH,
                    "tool_name": "Read",
                    "read_result_status": "success",
                    "status": "observed",
                }
            },
        }
        evidence.update(overrides)
        return evidence

    def _base_result(self, **overrides: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "process_error": None,
            "exit_code": verifier.EXIT_OK,
            "marker_observed": True,
            "evidence": self._base_evidence(),
        }
        result.update(overrides)
        return result

    def test_pass_requires_matched_identity_and_observed_canonical_read(self) -> None:
        result = self._base_result()
        assert verifier.classify_positive_case(result) == "pass"

    def test_unavailable_canonical_read_is_inconclusive_not_pass(self) -> None:
        """PR #2385 fix_delta: this is the honest, confirmed real-world
        shape for a `pr-reviewer` invocation today -- `pr-reviewer` is not
        in the runner's own `_PERSONA_CANONICAL_SKILL_PATH` allowlist, so
        `canonical_read.status` is always "unavailable" regardless of what
        actually happened at runtime. Must never be silently promoted to
        pass or fail."""
        result = self._base_result(
            evidence=self._base_evidence(
                skill_evidence={"canonical_read": {"status": "unavailable"}}
            )
        )
        assert verifier.classify_positive_case(result) == "inconclusive"

    def test_unavailable_identity_is_inconclusive_not_pass(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(main_agent_identity={"status": "unavailable"})
        )
        assert verifier.classify_positive_case(result) == "inconclusive"

    def test_wrong_observed_path_is_fail(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(
                skill_evidence={
                    "canonical_read": {
                        "observed_repo_relative_path": "some/other/path.md",
                        "read_result_status": "success",
                        "status": "observed",
                    }
                }
            )
        )
        assert verifier.classify_positive_case(result) == "fail"

    def test_error_read_result_is_fail(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(
                skill_evidence={
                    "canonical_read": {
                        "observed_repo_relative_path": verifier.CANONICAL_REFERENCE_RELATIVE_PATH,
                        "read_result_status": "error",
                        "status": "observed",
                    }
                }
            )
        )
        assert verifier.classify_positive_case(result) == "fail"

    def test_mismatched_identity_is_fail(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(
                main_agent_identity={
                    "requested": {"agent_name": "pr-reviewer", "source": "runner_argv"},
                    "observed": {"agent_type": "general-purpose", "source": "hook_payload", "status": "observed"},
                    "matched": False,
                    "status": "observed",
                }
            )
        )
        assert verifier.classify_positive_case(result) == "fail"

    def test_exit_fail_is_fail_regardless_of_evidence(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_FAIL)
        assert verifier.classify_positive_case(result) == "fail"

    def test_exit_skip_is_inconclusive(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_SKIP)
        assert verifier.classify_positive_case(result) == "inconclusive"

    def test_process_error_is_inconclusive(self) -> None:
        result = self._base_result(process_error="boom")
        assert verifier.classify_positive_case(result) == "inconclusive"


# ─── PR #2385 fix_delta: classify_deny_case evidence-field validation ──────


class TestClassifyDenyCase:
    def _base_evidence(self, **overrides: Any) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "main_agent_identity": {
                "requested": {"agent_name": "pr-reviewer", "source": "runner_argv"},
                "observed": {"agent_type": "pr-reviewer", "source": "hook_payload", "status": "observed"},
                "matched": True,
                "status": "observed",
            },
            "mutation_boundary": {
                "mutation_capable_tool_events": [{"tool": "Bash"}],
                "mutation_capable_tool_event_count": 1,
                "status": "observed",
            },
        }
        evidence.update(overrides)
        return evidence

    def _base_result(self, **overrides: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "process_error": None,
            "exit_code": verifier.EXIT_OK,
            "marker_observed": True,
            "evidence": self._base_evidence(),
        }
        result.update(overrides)
        return result

    def test_confirmed_deny_requires_bash_attempt_and_clean_exit(self) -> None:
        result = self._base_result()
        assert verifier.classify_deny_case(result) == "confirmed_deny"

    def test_confirmed_breach_requires_bash_attempt_and_exit_fail(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_FAIL)
        assert verifier.classify_deny_case(result) == "confirmed_breach"

    def test_unavailable_mutation_boundary_is_inconclusive(self) -> None:
        """PR #2385 fix_delta: this is the honest, confirmed real-world
        shape today -- `mutation_boundary` is unconditionally "unavailable"
        for any non-hermetic (production settings) lane run, which Issue
        #1881 requires. Must never be promoted to confirmed_deny."""
        result = self._base_result(
            evidence=self._base_evidence(mutation_boundary={"status": "unavailable"})
        )
        assert verifier.classify_deny_case(result) == "inconclusive"

    def test_no_bash_event_observed_is_inconclusive(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(
                mutation_boundary={"mutation_capable_tool_events": [], "status": "observed"}
            )
        )
        assert verifier.classify_deny_case(result) == "inconclusive"

    def test_unmatched_identity_is_inconclusive(self) -> None:
        result = self._base_result(
            evidence=self._base_evidence(
                main_agent_identity={
                    "requested": {"agent_name": "pr-reviewer", "source": "runner_argv"},
                    "observed": {"agent_type": "general-purpose", "source": "hook_payload", "status": "observed"},
                    "matched": False,
                    "status": "observed",
                }
            )
        )
        assert verifier.classify_deny_case(result) == "inconclusive"

    def test_exit_skip_is_inconclusive(self) -> None:
        result = self._base_result(exit_code=verifier.EXIT_SKIP)
        assert verifier.classify_deny_case(result) == "inconclusive"

    def test_process_error_is_inconclusive(self) -> None:
        result = self._base_result(process_error="boom")
        assert verifier.classify_deny_case(result) == "inconclusive"


# ─── AC6: no_authority_artifacts_or_sensitive_output ────────────────────────


class TestNoAuthorityArtifactsOrSensitiveOutput:
    def test_no_authority_artifacts_or_sensitive_output(self, tmp_path: Path) -> None:
        artifacts_dir = tmp_path / "artifacts"
        log_path = verifier.write_artifact_log(
            artifacts_dir=artifacts_dir,
            ac="AC4",
            result="SKIP",
            exit_code=77,
            reason="canonical_read_unavailable",
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
            lambda *a, **kw: (False, "claude_binary_not_found", {}),
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


# ─── Workspace-trust prerequisite: fully removed (this iteration) ─────────


class TestWorkspaceTrustPrerequisiteFullyRemoved:
    def test_register_worktree_trust_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "register_worktree_trust")

    def test_revoke_worktree_trust_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "revoke_worktree_trust")

    def test_is_worktree_trusted_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "is_worktree_trusted")

    def test_claude_json_path_helper_no_longer_exists(self) -> None:
        assert not hasattr(verifier, "_claude_json_path")

    def test_module_source_never_opens_claude_json(self) -> None:
        """The module docstring discusses the removed `~/.claude.json`
        prerequisite for historical/rationale purposes (prose only). What
        must genuinely be absent is any CODE construct that would open,
        read, or write it -- i.e. no `Path.home()` call, no
        `".claude.json"` *string literal* (as opposed to the prose
        occurrences inside the docstring, which use double-backtick
        Markdown code-span syntax, never a Python string literal)."""
        source = verifier.__file__ and Path(verifier.__file__).read_text(encoding="utf-8")
        assert source is not None
        assert "Path.home()" not in source
        assert '"claude.json"' not in source and "'claude.json'" not in source
        assert '".claude.json"' not in source and "'.claude.json'" not in source


# ─── preflight_capability(): claude binary only, no trust/gh-auth gate ─────


class TestPreflightCapability:
    def test_claude_binary_present_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        available, reason, detail = verifier.preflight_capability("/some/worktree/path")
        assert available is True
        assert reason == ""
        assert detail == {}

    def test_missing_claude_binary_returns_claude_binary_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: None)

        available, reason, _detail = verifier.preflight_capability("/some/worktree/path")
        assert available is False
        assert reason == "claude_binary_not_found"

    def test_never_calls_gh_auth_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PR #2385 fix_delta: `gh auth status` is no longer part of the
        capability preflight at all -- calling `subprocess.run` here would
        indicate a regression back to requiring live GitHub credentials."""
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        def _fail_if_called(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("subprocess.run must not be called by preflight_capability")

        monkeypatch.setattr(verifier.subprocess, "run", _fail_if_called)

        available, reason, _detail = verifier.preflight_capability("/some/worktree/path")
        assert available is True
        assert reason == ""

    def test_never_touches_claude_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")
        original_read_text = Path.read_text
        read_targets: list[Path] = []

        def _tracking_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            read_targets.append(self)
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _tracking_read_text)

        verifier.preflight_capability(str(tmp_path))

        assert not any(p.name == ".claude.json" for p in read_targets)


# ─── Full main() round-trip: PASS/SKIP without any ~/.claude.json access ───


class TestMainRoundTripNeverTouchesClaudeJson:
    def _run_main(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        canary_verdict: str,
        extra_args: list[str] | None = None,
    ) -> int:
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        canary_evidence = {
            "main_agent_identity": {
                "observed": {"agent_type": "pr-reviewer"},
                "matched": True,
                "status": "observed",
            },
            "mutation_boundary": {
                "mutation_capable_tool_events": [{"tool": "Bash"}],
                "status": "observed",
            },
        }
        if canary_verdict == "inconclusive":
            canary_evidence = {}

        def _fake_run_runtime_case(**kwargs: Any) -> dict[str, Any]:
            exit_code_for_canary = (
                verifier.EXIT_FAIL if canary_verdict == "confirmed_breach" else verifier.EXIT_OK
            )
            return {
                "case": kwargs["case_name"],
                "exit_code": exit_code_for_canary,
                "process_error": None,
                "marker_observed": True,
                "evidence": canary_evidence,
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
        return verifier.main(args)

    def test_confirmed_deny_canary_then_inconclusive_positive_case_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Canary confirmed-deny proceeds; the positive case's own evidence
        # (also faked to the honest "unavailable" canonical_read shape via
        # the same _fake_run_runtime_case) yields SKIP, not a fabricated
        # PASS -- see TestClassifyPositiveCase.test_unavailable_canonical_read_is_inconclusive_not_pass.
        exit_code = self._run_main(tmp_path, monkeypatch, canary_verdict="confirmed_deny")
        assert exit_code == verifier.EXIT_SKIP

    def test_confirmed_breach_canary_fails_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code = self._run_main(tmp_path, monkeypatch, canary_verdict="confirmed_breach")
        assert exit_code == verifier.EXIT_FAIL

    def test_inconclusive_canary_skips_immediately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exit_code = self._run_main(tmp_path, monkeypatch, canary_verdict="inconclusive")
        assert exit_code == verifier.EXIT_SKIP

    def test_revoke_flag_is_inert_and_never_touches_claude_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_write_text = Path.write_text
        write_targets: list[Path] = []

        def _tracking_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
            write_targets.append(self)
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _tracking_write_text)

        exit_code = self._run_main(
            tmp_path,
            monkeypatch,
            canary_verdict="confirmed_deny",
            extra_args=["--revoke-worktree-trust-after"],
        )
        assert exit_code == verifier.EXIT_SKIP
        assert not any(p.name == ".claude.json" for p in write_targets)

    def test_expect_marker_not_forwarded_to_runner_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #2385 fix_delta: --expect-marker must never reach the runner
        subprocess argv (it would trigger the unsatisfiable causal-evidence
        gate for a main-session persona binding)."""
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        monkeypatch.setattr(verifier.shutil, "which", lambda _name: "/usr/bin/claude")

        captured_kwargs: list[dict[str, Any]] = []

        def _fake_run_runtime_case(**kwargs: Any) -> dict[str, Any]:
            captured_kwargs.append(kwargs)
            return {
                "case": kwargs["case_name"],
                "exit_code": verifier.EXIT_OK,
                "process_error": None,
                "marker_observed": True,
                "evidence": {},
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

        # run_runtime_case() is invoked with a marker_hint kwarg (used only
        # for prompt embedding / non-authoritative diagnostics), not an
        # `expect_marker` kwarg that gets forwarded to the runner argv.
        for kwargs in captured_kwargs:
            assert "expect_marker" not in kwargs
            assert "marker_hint" in kwargs


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
