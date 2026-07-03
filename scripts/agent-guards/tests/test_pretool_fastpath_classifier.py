#!/usr/bin/env python3
"""
Tests for pretool_fastpath_classifier.py (Issue #1289).

Covers AC1-AC8. See the Issue #1289 contract for full AC text and Verification
Commands (each test below is invoked individually via `pytest -k <name>`).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
import time
from pathlib import Path


_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import pretool_fastpath_classifier as fp  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _publish_cmd(
    command_id: str = "termination_report.publish",
    issue_number: str = "1166",
    repo: str = "squne121/loop-protocol",
    input_file: str | None = None,
) -> str:
    if input_file is None:
        input_file = f"artifacts/{issue_number}/termination_report_input.json"
    return (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
        f" --command-id {command_id}"
        f" --issue-number {issue_number}"
        f" --input-file {input_file}"
        f" --repo {repo}"
    )


# =============================================================================
# AC1: readonly_display bounded fast-path summary
# =============================================================================


class TestAC1ReadonlyDisplay:
    def test_ac1_readonly_display_bounded_summary(self):
        for cmd in ("gh issue view 1289", "git status", "rg foo bar"):
            result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_READONLY_DISPLAY, (cmd, result)
            assert result.display_summary is not None
            # Bounded: never echoes the raw command body beyond a short head.
            assert len(result.display_summary) <= 64
            assert result.display_summary.startswith("readonly_display")

        # Must-not-fastpath (adversarial pairing): a mutating command must
        # never be classified readonly_display.
        mutating = fp.classify("git commit -m x", str(REPO_ROOT), str(REPO_ROOT))
        assert mutating.classification != fp.CLASS_READONLY_DISPLAY


# =============================================================================
# AC2: mutating / unknown -> mutation_or_unknown, existing chain unchanged
# =============================================================================


class TestAC2MutationOrUnknown:
    def test_ac2_mutation_or_unknown_existing_chain_unchanged(self):
        must_be_mutation_or_unknown = [
            "git commit -am x",
            "git push origin main",
            "rm -rf /tmp/x",
            "gh issue edit 1 --title x",
            "gh pr merge 1",
            "some-totally-unknown-binary --flag",
        ]
        for cmd in must_be_mutation_or_unknown:
            result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_MUTATION_OR_UNKNOWN, (cmd, result)

        # local_main_branch_guard's actual block/allow decision for a
        # representative blocking command must be unaffected by the presence
        # of the fastpath classifier (existing chain unchanged).
        from local_main_branch_guard import evaluate

        with subprocess.Popen(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            text=True,
        ) as proc:
            root, _ = proc.communicate()
        cwd = root.strip() or str(REPO_ROOT)
        result = evaluate("git commit -am 'should not fastpath'", cwd)
        assert result["status"] in ("allow", "block")
        assert result["reason_code"] != "readonly_command"


# =============================================================================
# AC3: exact_controlled_executor_authorized records registry/policy hash,
# no raw command body / secret-like value in transcript output. Shape-only
# (unauthorized) invocations fall through to mutation_or_unknown.
# =============================================================================


class TestAC3ExactExecutorHash:
    def test_ac3_exact_executor_hash_no_raw_body(self):
        cmd = _publish_cmd()
        result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
        assert result.classification == fp.CLASS_EXACT_AUTHORIZED
        assert result.command_id == "termination_report.publish"
        assert result.policy_hash is not None
        assert len(result.policy_hash) == 16

        telemetry = result.to_telemetry_dict()
        # The raw cmd string (containing the input-file path) must never
        # appear verbatim in the telemetry payload.
        serialized = repr(telemetry)
        assert cmd not in serialized
        assert "artifacts/1166/termination_report_input.json" not in serialized
        assert "policy_hash" in telemetry
        assert "command_id" in telemetry

        # Secret-like value smuggled via an otherwise-valid-shaped flag value
        # must not leak into telemetry even if the command is misclassified.
        secret_cmd = _publish_cmd(
            input_file="artifacts/1166/ghp_1234567890ABCDEFsecretvalue.json"
        )
        secret_result = fp.classify(secret_cmd, str(REPO_ROOT), str(REPO_ROOT))
        secret_serialized = repr(secret_result.to_telemetry_dict())
        assert "ghp_1234567890ABCDEFsecretvalue" not in secret_serialized

        # exact_controlled_executor_shape without authorization (unknown
        # command_id) must fold into mutation_or_unknown, not leak as a
        # separate externally-visible classification.
        shape_only_cmd = _publish_cmd(command_id="not_a_real_command_id")
        shape_result = fp.classify(shape_only_cmd, str(REPO_ROOT), str(REPO_ROOT))
        assert shape_result.classification == fp.CLASS_MUTATION_OR_UNKNOWN

        # Wrong repo binding: shape matches, authorization must fail closed.
        wrong_repo_cmd = _publish_cmd(repo="evil/not-the-repo")
        wrong_repo_result = fp.classify(wrong_repo_cmd, str(REPO_ROOT), str(REPO_ROOT))
        assert wrong_repo_result.classification == fp.CLASS_MUTATION_OR_UNKNOWN

        # Namespace mismatch: input-file does not belong to this issue number.
        namespace_mismatch_cmd = _publish_cmd(
            issue_number="1166", input_file="artifacts/9999/termination_report_input.json"
        )
        namespace_result = fp.classify(namespace_mismatch_cmd, str(REPO_ROOT), str(REPO_ROOT))
        assert namespace_result.classification == fp.CLASS_MUTATION_OR_UNKNOWN


# =============================================================================
# AC4: hook boundary manifest (including .codex/hooks.json) documents the
# fast-path classification / fail policy / stdout-stderr contract.
# =============================================================================


class TestAC4HookBoundaryManifest:
    def test_ac4_hook_boundary_manifest_fastpath_contract(self):
        docs_path = REPO_ROOT / "docs" / "dev" / "hook-boundaries.md"
        docs_text = docs_path.read_text(encoding="utf-8")
        assert "pretool_fastpath_classifier" in docs_text
        assert "readonly_display" in docs_text
        assert "exact_controlled_executor_authorized" in docs_text
        assert "mutation_or_unknown" in docs_text

        codex_hooks_path = REPO_ROOT / ".codex" / "hooks.json"
        codex_text = codex_hooks_path.read_text(encoding="utf-8")
        assert "pretool_fastpath_classifier" in codex_text
        assert "readonly_display" in codex_text


# =============================================================================
# AC5: classifier execution time budget for read-only commands.
# =============================================================================


class TestAC5Budget:
    BUDGET_SECONDS = 0.25

    def test_ac5_readonly_classification_within_budget(self):
        cmd = "git status"
        # Warm up (import caches / module-level regex compilation already
        # happened at import time, but the first call may still be slower).
        fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
        start = time.monotonic()
        for _ in range(20):
            result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_READONLY_DISPLAY
        elapsed = time.monotonic() - start
        per_call = elapsed / 20
        assert per_call < self.BUDGET_SECONDS, (
            f"per-call classification time {per_call:.4f}s exceeded budget "
            f"{self.BUDGET_SECONDS}s"
        )


# =============================================================================
# AC6: classifier is not registered as an independent PreToolUse hook; hook
# topology (settings.json / .codex/hooks.json) is unchanged.
# =============================================================================


class TestAC6NoNewHookTopology:
    def test_ac6_no_new_hook_topology(self):
        import json

        settings_path = REPO_ROOT / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        pretool_entries = settings.get("hooks", {}).get("PreToolUse", [])
        for entry in pretool_entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                assert "pretool_fastpath_classifier" not in command, (
                    "classifier must not be registered as its own PreToolUse hook entry"
                )

        codex_hooks_path = REPO_ROOT / ".codex" / "hooks.json"
        codex = json.loads(codex_hooks_path.read_text(encoding="utf-8"))
        codex_pretool_entries = codex.get("hooks", {}).get("PreToolUse", [])
        for entry in codex_pretool_entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                assert "pretool_fastpath_classifier" not in command

        # The module itself must not define a __main__ hook entry point that
        # reads stdin PreToolUse payloads (i.e. it has no `main()` CLI hook
        # runner exposed at module scope like the real hooks do).
        assert not hasattr(fp, "run_hook")
        assert not hasattr(fp, "main")


# =============================================================================
# AC7: readonly_display intersection semantics; gh api token-level check;
# gh issue/pr --web/-w exclusion.
# =============================================================================


class TestAC7ReadonlyIntersectionAndGhApiTokenCheck:
    def test_ac7_readonly_intersection_and_gh_api_token_check(self):
        # gh api default GET, exact allowlisted comment endpoint -> readonly.
        readonly_gh_api = fp.classify(
            "gh api repos/squne121/loop-protocol/issues/comments/1",
            str(REPO_ROOT),
            str(REPO_ROOT),
        )
        assert readonly_gh_api.classification == fp.CLASS_READONLY_DISPLAY

        # gh api with a POST-izing flag -> mutation_or_unknown.
        for mutating_cmd in (
            "gh api --method POST repos/squne121/loop-protocol/issues/1/comments",
            "gh api -X POST repos/squne121/loop-protocol/issues/1/comments",
            "gh api -f body=x repos/squne121/loop-protocol/issues/1/comments",
            "gh api -F body=x repos/squne121/loop-protocol/issues/1/comments",
        ):
            result = fp.classify(mutating_cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_MUTATION_OR_UNKNOWN, mutating_cmd

        # gh issue/pr --web / -w must be excluded from readonly_display even
        # though the base subcommand (view) would otherwise be read-only.
        for web_cmd in (
            "gh issue view 1289 --web",
            "gh issue view 1289 -w",
            "gh pr view 1 --web",
        ):
            result = fp.classify(web_cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_MUTATION_OR_UNKNOWN, web_cmd

        # Adversarial: cases where one guard would call it read-only but the
        # other would not must fall to mutation_or_unknown (intersection, not
        # union). git worktree remove is "cleanup" per worktree_scope_guard
        # (not read_only) even though it superficially resembles a read op.
        cleanup_like = fp.classify(
            "git worktree remove some-path", str(REPO_ROOT), str(REPO_ROOT)
        )
        assert cleanup_like.classification == fp.CLASS_MUTATION_OR_UNKNOWN

        # gh issue edit / gh pr merge must never be readonly_display.
        for mutation_cmd in ("gh issue edit 1 --title x", "gh pr merge 1"):
            result = fp.classify(mutation_cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_MUTATION_OR_UNKNOWN, mutation_cmd


# =============================================================================
# AC8: local_main_branch_guard's controlled skill mutation executor allow
# returns the independent reason_code `controlled_skill_mutation_executor`.
# =============================================================================


class TestAC8ControlledSkillMutationExecutorReasonCode:
    def test_ac8_controlled_skill_mutation_executor_reason_code(self, tmp_path):
        from local_main_branch_guard import (
            REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR,
            REASON_DETERMINISTIC_CHECKER,
        )

        # Build a minimal local git repo whose root is treated as local-root
        # context, with the canonical executor script present.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "a"], cwd=repo, check=True)
        (repo / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        executor_dir = repo / "scripts" / "agent-guards"
        executor_dir.mkdir(parents=True, exist_ok=True)
        (executor_dir / "controlled_skill_mutation_exec.py").write_text("# stub\n")

        cmd = _publish_cmd()
        # local_main_branch_guard.evaluate is only exercised at local-root
        # (default-branch) context. We assert the reason_code directly on
        # the shared policy layer, mirroring the guard's Step 13.6 call.
        from controlled_skill_mutation_policy import is_controlled_skill_mutation_exec_command

        assert is_controlled_skill_mutation_exec_command(cmd, str(repo))

        # AC8: the reason_code constant must be independent of
        # REASON_DETERMINISTIC_CHECKER (distinct string values).
        assert REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR != REASON_DETERMINISTIC_CHECKER
        assert REASON_CONTROLLED_SKILL_MUTATION_EXECUTOR == "controlled_skill_mutation_executor"


# =============================================================================
# Blocker 1 (PR #1299 review fix_delta): import-order independence.
#
# local_main_branch_guard.py imports pretool_fastpath_classifier.py, which in
# turn imports is_readonly_command / _parse_gh_api_command back from
# local_main_branch_guard.py. If local_main_branch_guard is imported FIRST in
# a fresh process (before anything imports the classifier module directly),
# a naive top-level `import pretool_fastpath_classifier` inside
# local_main_branch_guard.py would previously latch _FASTPATH_AVAILABLE=False
# forever, silently disabling evaluate()["fastpath"] enrichment. This test
# must run in a fresh subprocess (not this pytest process, which has already
# imported pretool_fastpath_classifier directly above) to exercise the real
# import-order hazard.
# =============================================================================


class TestBlocker1ImportOrderIndependence:
    def test_fastpath_available_when_local_main_branch_guard_imported_first(self):
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(_GUARDS_DIR)!r})

            # Import local_main_branch_guard FIRST, before anything imports
            # pretool_fastpath_classifier directly — this is the ordering
            # that previously triggered the circular-import hazard.
            import local_main_branch_guard as lmb

            result = lmb.evaluate("git status", {str(REPO_ROOT)!r})
            fastpath = result.get("fastpath")
            assert fastpath is not None, (
                "fastpath enrichment must not be permanently disabled by "
                "import order"
            )
            assert fastpath["classification"] == "readonly_display", fastpath
            print("OK")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "OK" in proc.stdout


# =============================================================================
# Blocker 2 (PR #1299 review fix_delta): --input-file path traversal must
# fold into mutation_or_unknown, never exact_controlled_executor_authorized.
# =============================================================================


class TestBlocker2InputFilePathTraversal:
    def test_path_traversal_and_malformed_input_file_rejected(self):
        malicious_input_files = [
            # Traversal that escapes the namespace while still matching a
            # naive str.startswith(expected_prefix) check.
            "artifacts/1166/../../evil.json",
            "artifacts/1166/../1166/../../etc/passwd",
            "artifacts/1166/subdir/../../../evil.json",
            # Absolute path.
            "/etc/passwd",
            # Backslash (Windows-style separator smuggling).
            "artifacts\\1166\\evil.json",
            # NUL byte.
            "artifacts/1166/evil.json\x00.txt",
            # Leading ./ relative marker.
            "./artifacts/1166/termination_report_input.json",
            # Bare "." / ".." components.
            "artifacts/1166/./termination_report_input.json",
            "artifacts/1166/..",
            # Stdin marker.
            "-",
            # Nested subdirectory beneath the namespace prefix (not a bare
            # leaf filename).
            "artifacts/1166/nested/dir/termination_report_input.json",
        ]
        for input_file in malicious_input_files:
            cmd = _publish_cmd(input_file=input_file)
            result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_MUTATION_OR_UNKNOWN, (
                input_file,
                result,
            )

        # Sanity: a legitimate, single-segment leaf filename under the
        # namespace prefix must still classify as authorized.
        legit = fp.classify(_publish_cmd(), str(REPO_ROOT), str(REPO_ROOT))
        assert legit.classification == fp.CLASS_EXACT_AUTHORIZED


# =============================================================================
# Blocker 3 (PR #1299 review fix_delta): readonly_display summary must never
# leak search queries / paths / secret-like values — only fixed labels.
# =============================================================================


class TestBlocker3SummaryNoSecretLeak:
    def test_search_and_display_summaries_use_fixed_labels_only(self):
        secret_token = "ghp_1234567890ABCDEFsecretvalue"
        cases = [
            (f"rg {secret_token} .", "readonly_display:search"),
            ("grep SECRET_TOKEN .env.example", "readonly_display:search"),
            ("cat some/secret/path.txt", "readonly_display:file-display"),
            ("head -n 5 some/secret/path.txt", "readonly_display:file-display"),
            ("git status", "readonly_display:git:status"),
            ("git diff", "readonly_display:git:diff"),
            ("gh issue view 1289", "readonly_display:gh:issue:view"),
        ]
        for cmd, expected_summary in cases:
            result = fp.classify(cmd, str(REPO_ROOT), str(REPO_ROOT))
            assert result.classification == fp.CLASS_READONLY_DISPLAY, (cmd, result)
            assert result.display_summary == expected_summary, (cmd, result)
            serialized = repr(result.to_telemetry_dict())
            assert secret_token not in serialized
            assert "SECRET_TOKEN" not in serialized
            assert ".env.example" not in serialized
            assert "some/secret/path.txt" not in serialized


# =============================================================================
# Major (PR #1299 review fix_delta): .codex/hooks.json PreToolUse topology
# must be verified against a fixed expected count, not just absence of the
# classifier module name (which cannot detect other hook additions/removals).
# =============================================================================


def _load_check_hook_boundaries():
    checker_path = REPO_ROOT / "scripts" / "check_hook_boundaries.py"
    spec = importlib.util.spec_from_file_location(
        "check_hook_boundaries_for_fastpath_test", checker_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class TestMajorCodexHooksTopologyCheck:
    def test_current_topology_matches_frozen_expectation(self):
        checker = _load_check_hook_boundaries()
        errors = checker.check_codex_hooks_pretool_topology()
        assert errors == []

    def test_topology_drift_is_detected_and_fails_closed(self):
        checker = _load_check_hook_boundaries()
        actual = checker.load_codex_hooks_topology()
        assert actual, "expected at least one PreToolUse matcher in .codex/hooks.json"

        # Mutate a copy: bump one matcher's count by one (simulating an
        # undetected added hook) and verify the check fails closed.
        drifted_expected = dict(actual)
        first_matcher = next(iter(drifted_expected))
        drifted_expected[first_matcher] += 1

        errors = checker.check_codex_hooks_pretool_topology(expected=drifted_expected)
        assert errors, "topology drift must be reported, not silently accepted"
        assert any("pretool_topology" in err for err in errors)

        # Also verify a removed-matcher case fails closed.
        removed_expected = dict(actual)
        del removed_expected[first_matcher]
        errors2 = checker.check_codex_hooks_pretool_topology(expected=removed_expected)
        assert errors2
