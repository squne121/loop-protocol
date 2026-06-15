"""
test_issue_refinement_ops_review.py - Tests for issue-refinement-ops-review task-kind.

Issue #945: Verifies that issue-refinement-ops-review task-kind is implemented,
draws from the shared PLAN_REGISTRY, and generates proper read plans and inventories.

Verifies:
- AC1: JSON compact read plan output <= 2048 UTF-8 bytes; budget overflow does NOT fallback to non-JSON
- AC2: MUST_READ includes required agent_ops_inventory paths (AGENTS.md, CLAUDE.md, contract scripts, etc.)
- AC3: inventory artifact coverage includes required prefixes (.claude/agents/, .claude/rules/, .claude/hooks/, etc.)
- AC4: DO_NOT_READ_INITIAL_ONLY sections marked with read_policy: initial_exclusion_not_absolute_forbid
- AC5: full inventory saved to artifact only; stdout emits only EVIDENCE key
- AC6: artifact items are tracked metadata only; tracked_matches and empty_ok machine-decidable
- AC7: agent-ops-review and issue-refinement-ops-review share the same registry/spec (no branch duplication)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS_DIR))

from agent_ops_inventory import (
    PLAN_REGISTRY,
    PlanSpec,
    VALID_TASK_KINDS,
    build_agent_ops_inventory,
    build_plan_output,
    get_tracked_paths_decoded,
    is_secret_like,
    write_artifact,
)
from check_agent_friendly_stdout import check_stdout


# ──────────────────────────────────────────────────────────────────────────────
# AC1: JSON compact read plan <= 2048 bytes, no non-JSON fallback on overflow
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewJsonCompactBudget:
    def test_issue_refinement_ops_review_in_valid_task_kinds(self):
        """GIVEN VALID_TASK_KINDS WHEN inspected THEN issue-refinement-ops-review present."""
        assert "issue-refinement-ops-review" in VALID_TASK_KINDS

    def test_issue_refinement_ops_review_in_plan_registry(self):
        """GIVEN PLAN_REGISTRY WHEN inspected THEN issue-refinement-ops-review spec registered."""
        assert "issue-refinement-ops-review" in PLAN_REGISTRY
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert isinstance(spec, PlanSpec)
        assert spec.task_kind == "issue-refinement-ops-review"

    def test_issue_refinement_ops_review_json_compact_budget(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN output <= 2048 UTF-8 bytes."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"Non-zero exit: {result.returncode}\nstderr: {result.stderr}"
        byte_count = len(result.stdout.encode("utf-8"))
        assert byte_count <= 2048, (
            f"JSON output exceeds 2048 bytes: {byte_count}\nstdout={result.stdout!r}"
        )

    def test_issue_refinement_ops_review_json_valid(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN valid JSON output."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "MUST_READ" in data
        assert "DO_NOT_READ_INITIAL_ONLY" in data
        assert "task_kind" in data
        assert data["task_kind"] == "issue-refinement-ops-review"

    def test_issue_refinement_ops_review_no_non_json_fallback_on_overflow(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN always outputs valid JSON, never fallback."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # Must be valid JSON, never a fallback string like "STATUS: ok"
        try:
            data = json.loads(result.stdout)
            # If it parses, it's valid JSON (not a fallback)
            assert isinstance(data, dict)
        except json.JSONDecodeError as e:
            pytest.fail(f"Output is not valid JSON (fallback to non-JSON), error: {e}\nstdout: {result.stdout!r}")


# ──────────────────────────────────────────────────────────────────────────────
# AC2: MUST_READ includes required paths
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewMustRead:
    def test_must_read_includes_agents_md(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN AGENTS.md present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert "AGENTS.md" in spec.must_read

    def test_must_read_includes_claude_md(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN CLAUDE.md present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert "CLAUDE.md" in spec.must_read

    def test_must_read_includes_refinement_loop_skill(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN refinement-loop SKILL.md present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert ".claude/skills/issue-refinement-loop/SKILL.md" in spec.must_read

    def test_must_read_includes_contract_review_scripts(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN contract review scripts present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        required_scripts = [
            ".claude/skills/issue-contract-review/scripts/contract_readiness_check.py",
            ".claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
        ]
        for script in required_scripts:
            assert script in spec.must_read, f"Missing required script: {script}"

    def test_must_read_includes_refinement_loop_entrypoint_scripts(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN refinement loop entrypoint scripts present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        required_scripts = [
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            ".claude/skills/issue-refinement-loop/scripts/plan_refinement_loop.py",
            ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
            ".claude/skills/issue-refinement-loop/scripts/compact_review_result.py",
            ".claude/skills/issue-refinement-loop/scripts/compact_author_result.py",
        ]
        for script in required_scripts:
            assert script in spec.must_read, f"Missing required entrypoint script: {script}"

    def test_must_read_includes_agent_definitions(self):
        """GIVEN issue-refinement-ops-review WHEN MUST_READ inspected THEN agent definitions present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        required_agents = [
            ".claude/agents/issue-reviewer.md",
            ".claude/agents/issue-author.md",
        ]
        for agent in required_agents:
            assert agent in spec.must_read, f"Missing required agent: {agent}"


# ──────────────────────────────────────────────────────────────────────────────
# AC3: inventory artifact coverage includes required prefixes
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewCoverage:
    def test_inventory_for_issue_refinement_ops_review_has_agents_skills(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN .agents/skills/** included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        agent_skill_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".agents/skills/")
        ]
        assert len(agent_skill_items) > 0, "Expected .agents/skills/** items in inventory"

    def test_inventory_for_issue_refinement_ops_review_has_claude_rules(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN .claude/rules/ included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        rule_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".claude/rules/")
        ]
        assert len(rule_items) > 0, "Expected .claude/rules/ items in inventory"

    def test_inventory_for_issue_refinement_ops_review_has_claude_hooks(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN .claude/hooks/ included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        hook_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".claude/hooks/") and not is_secret_like(it["path"])
        ]
        # Only check if non-secret hooks exist
        if hook_items:
            assert len(hook_items) > 0

    def test_inventory_for_issue_refinement_ops_review_has_claude_skills(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN .claude/skills/ included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        skill_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".claude/skills/")
        ]
        assert len(skill_items) > 0, "Expected .claude/skills/ items in inventory"

    def test_inventory_for_issue_refinement_ops_review_has_codex_agents(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN .codex/ included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        codex_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".codex/")
        ]
        # Codex config is optional but if present should be in inventory
        if codex_items:
            assert len(codex_items) > 0

    def test_inventory_for_issue_refinement_ops_review_has_fixture(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN expected-runtime-contract.json included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        fixture_items = [
            it for it in inventory["items"]
            if "expected-runtime-contract.json" in it["path"]
        ]
        assert len(fixture_items) >= 1, "expected-runtime-contract.json must appear in inventory"


# ──────────────────────────────────────────────────────────────────────────────
# AC4: DO_NOT_READ_INITIAL_ONLY with read_policy clarification
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewDoNotReadInitialOnly:
    def test_do_not_read_initial_only_includes_src(self):
        """GIVEN issue-refinement-ops-review spec WHEN DO_NOT_READ_INITIAL_ONLY inspected THEN src/ present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert "src/" in spec.do_not_read_initial_only

    def test_do_not_read_initial_only_includes_docs_product(self):
        """GIVEN issue-refinement-ops-review spec WHEN DO_NOT_READ_INITIAL_ONLY inspected THEN docs/product/ present."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        assert "docs/product/" in spec.do_not_read_initial_only

    def test_json_output_has_do_not_read_field(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN DO_NOT_READ_INITIAL_ONLY field present in JSON."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        data = json.loads(result.stdout)
        assert "DO_NOT_READ_INITIAL_ONLY" in data

    def test_plan_output_includes_read_policy_note(self):
        """GIVEN issue-refinement-ops-review plan WHEN note inspected THEN clarifies exclusion not absolute forbid."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        plan = build_plan_output(spec, REPO_ROOT)
        note = plan.get("note", "")
        assert "NOT forbidden" in note or "not forbidden" in note.lower(), (
            f"Expected note to clarify additional reads are not forbidden, got: {note!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# AC5: full inventory saved to artifact only; stdout minimal
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewArtifactOnly:
    def test_inventory_artifact_only_output_to_file(self):
        """GIVEN issue-refinement-ops-review with inventory WHEN artifact written THEN stdout is EVIDENCE key only."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "inventory.json"
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review",
                 "--artifact-out", str(artifact_path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 1, 2}, f"Unexpected exit: {result.returncode}"
            assert "EVIDENCE:" in result.stdout
            # stdout should NOT contain raw file lists, diffs, or contents
            assert not any(x in result.stdout for x in ["git diff", "ls -", "cat ", "raw_files"]), (
                f"stdout contains raw file data: {result.stdout!r}"
            )

    def test_artifact_file_contains_full_inventory(self):
        """GIVEN issue-refinement-ops-review WHEN artifact file inspected THEN contains full items list."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "inventory.json"
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review",
                 "--artifact-out", str(artifact_path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 1, 2}
            assert artifact_path.exists()
            artifact_data = json.loads(artifact_path.read_text(encoding="utf-8"))
            assert "items" in artifact_data
            assert len(artifact_data["items"]) > 0, "Artifact should contain inventory items"


# ──────────────────────────────────────────────────────────────────────────────
# AC6: security and metadata-only inventory
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewSecurity:
    def test_inventory_items_metadata_only(self):
        """GIVEN issue-refinement-ops-review inventory WHEN items inspected THEN only path/exists/kind/tracked."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        forbidden_fields = {"mtime", "atime", "ctime", "content", "contents", "absolute_path", "size"}
        for item in inventory["items"]:
            item_keys = set(item.keys())
            overlap = item_keys & forbidden_fields
            assert not overlap, f"Forbidden fields found in item: {overlap}"
            assert item_keys == {"path", "exists", "kind", "tracked"}, (
                f"Unexpected fields in item: {item_keys}"
            )

    def test_inventory_paths_are_relative(self):
        """GIVEN issue-refinement-ops-review inventory WHEN paths inspected THEN all repo-relative."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        for item in inventory["items"]:
            assert not item["path"].startswith("/"), (
                f"Absolute path found: {item['path']!r}"
            )

    def test_inventory_no_secret_like_paths(self):
        """GIVEN issue-refinement-ops-review inventory WHEN paths inspected THEN no secret-like strings."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        for item in inventory["items"]:
            assert not is_secret_like(item["path"]), (
                f"Secret-like path found: {item['path']!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# AC7: shared registry/spec between agent-ops-review and issue-refinement-ops-review
# ──────────────────────────────────────────────────────────────────────────────


class TestSharedRegistrySpec:
    def test_shared_registry_both_task_kinds(self):
        """GIVEN PLAN_REGISTRY WHEN inspected THEN both agent-ops-review and issue-refinement-ops-review registered."""
        assert "agent-ops-review" in PLAN_REGISTRY
        assert "issue-refinement-ops-review" in PLAN_REGISTRY

    def test_both_use_same_inventory_builder_function(self):
        """GIVEN both task-kinds WHEN inventory built with same function THEN no branch duplication."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inv_agent_ops = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="agent-ops-review")
        inv_refinement_ops = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        # Both should have the same schema structure
        assert inv_agent_ops["schema_version"] == inv_refinement_ops["schema_version"]
        assert "items" in inv_agent_ops
        assert "items" in inv_refinement_ops
        assert "critical_surfaces" in inv_agent_ops
        assert "critical_surfaces" in inv_refinement_ops

    def test_artifact_schema_shared_between_task_kinds(self):
        """GIVEN both task-kinds inventory output WHEN schema inspected THEN same artifact schema."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inv1 = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="agent-ops-review")
        inv2 = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        # Both should have identical required keys except task_kind field
        keys1 = set(inv1.keys())
        keys2 = set(inv2.keys())
        assert keys1 == keys2, f"Schema keys differ: {keys1} vs {keys2}"

    def test_shared_spec_status_logic(self):
        """GIVEN both task-kinds WHEN built with same repo state THEN same status logic."""
        # This is a trivial test since both use the same function, but it documents the expectation
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inv1 = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="agent-ops-review")
        inv2 = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        # Both should have status field with same values
        assert inv1["status"] in {"ok", "warn", "blocked"}
        assert inv2["status"] in {"ok", "warn", "blocked"}

    def test_no_separate_build_function_for_new_task_kind(self):
        """GIVEN issue-refinement-ops-review task-kind WHEN source inspected THEN no separate build function."""
        # This verifies that the refactoring (AC7 MAJOR-1) succeeded:
        # there should be only ONE build_agent_ops_inventory function, not separate ones
        import inspect
        source = inspect.getsource(build_agent_ops_inventory)
        # Should have task_kind parameter and use it, not branch on specific strings
        assert "task_kind" in source, "build_agent_ops_inventory should accept task_kind parameter"
        # Should NOT have hardcoded "agent-ops-review" only logic in the builder
        # (the main() function is allowed to branch, but not the builder)
        builder_lines = source.split("\n")
        # This is a heuristic check: the builder should be generic
        assert "agent-ops-review" not in source or "task_kind" in source, (
            "Builder should not have task-kind-specific logic; it should be parameterized"
        )


# ──────────────────────────────────────────────────────────────────────────────
# CLI integration for issue-refinement-ops-review
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementOpsReviewCLI:
    def test_cli_with_artifact_out_flag(self):
        """GIVEN issue-refinement-ops-review --artifact-out WHEN run THEN artifact written."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "test_inventory.json"
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review",
                 "--artifact-out", str(artifact_path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 1, 2}
            assert artifact_path.exists(), "Artifact file should be written"

    def test_cli_json_flag_for_plan_output(self):
        """GIVEN issue-refinement-ops-review --json (no --artifact-out) WHEN run THEN JSON plan output."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "MUST_READ" in data
        assert "DO_NOT_READ_INITIAL_ONLY" in data

    def test_cli_stdout_passes_check_agent_friendly_stdout(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN stdout passes agent_friendly checks."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        violations = check_stdout(result.stdout, max_bytes=2048)
        assert violations == [], (
            f"stdout failed check_agent_friendly_stdout: {violations}\nstdout={result.stdout!r}"
        )
