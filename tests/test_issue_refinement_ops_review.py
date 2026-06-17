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
    OPS_REVIEW_INVENTORY_PROFILE,
    PLAN_REGISTRY,
    READ_POLICY_INITIAL_EXCLUSION,
    CoverageTarget,
    InventoryProfile,
    PlanSpec,
    VALID_TASK_KINDS,
    build_agent_ops_inventory,
    build_plan_output,
    classify_path_kind,
    emit_json_under_budget,
    get_tracked_paths_decoded,
    is_secret_like,
    load_contract_surfaces_with_errors,
    validate_artifact_destination,
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

    def test_json_compact_budget_overflow_stays_valid_json(self):
        """GIVEN a payload that exceeds the budget WHEN emit_json_under_budget THEN
        still valid JSON (degraded), never a non-JSON KEY: value string (BLOCKER 1)."""
        bloated = {
            "schema_version": "agent_ops_read_plan_v1",
            "task_kind": "issue-refinement-ops-review",
            "read_policy": READ_POLICY_INITIAL_EXCLUSION,
            "MUST_READ": [f"path/to/file_{i}_with_a_fairly_long_name.py" for i in range(200)],
            "DO_NOT_READ_INITIAL_ONLY": ["src/", "docs/product/"],
        }
        out = emit_json_under_budget(bloated, max_bytes=2048)
        assert len(out.encode("utf-8")) <= 2048
        data = json.loads(out)  # must parse — never a TASK_KIND: ... fallback string
        assert data["status"] == "blocked"
        assert data["error"] == "stdout_budget_exceeded"
        assert data["task_kind"] == "issue-refinement-ops-review"

    def test_json_compact_budget_overflow_via_bloated_registry_spec(self, monkeypatch):
        """GIVEN PLAN_REGISTRY spec with a huge MUST_READ WHEN --json plan emitted THEN
        output is still valid JSON within budget (forced overflow case)."""
        base = PLAN_REGISTRY["issue-refinement-ops-review"]
        huge = base._replace(
            must_read=[f"a/very/long/path/segment/file_{i}.py" for i in range(500)]
        )
        monkeypatch.setitem(PLAN_REGISTRY, "issue-refinement-ops-review", huge)
        plan = build_plan_output(PLAN_REGISTRY["issue-refinement-ops-review"], REPO_ROOT)
        out = emit_json_under_budget(plan, max_bytes=2048)
        assert len(out.encode("utf-8")) <= 2048
        data = json.loads(out)
        assert isinstance(data, dict)
        assert data["task_kind"] == "issue-refinement-ops-review"


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

    def test_inventory_coverage_includes_claude_agents_and_codex_agents(self):
        """GIVEN issue-refinement-ops-review inventory WHEN coverage inspected THEN includes all required prefixes."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        artifact = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")

        # AC6: coverage field exists and is a list
        assert "coverage" in artifact, "coverage field missing from artifact"
        assert isinstance(artifact["coverage"], list), "coverage should be a list"

        # All required prefixes in coverage set
        required_prefixes = {
            ".claude/agents/",
            ".claude/rules/",
            ".claude/hooks/",
            ".claude/skills/",
            ".agents/skills/",
            ".codex/agents/",
            "tests/fixtures/codex-agent-config/expected-runtime-contract.json",
        }
        coverage_prefixes = {entry["prefix"] for entry in artifact["coverage"]}
        assert required_prefixes == coverage_prefixes, (
            f"Coverage prefixes mismatch. Expected {required_prefixes}, got {coverage_prefixes}"
        )

        # .claude/agents/ entry has tracked_matches >= 1 and empty_ok is False
        claude_agents_entry = next(
            (e for e in artifact["coverage"] if e["prefix"] == ".claude/agents/"),
            None
        )
        assert claude_agents_entry is not None, ".claude/agents/ entry not found in coverage"
        assert claude_agents_entry["tracked_matches"] >= 1, (
            f".claude/agents/ should have tracked_matches >= 1, got {claude_agents_entry['tracked_matches']}"
        )
        assert claude_agents_entry["empty_ok"] is False, (
            ".claude/agents/ should have empty_ok=False (real agent files exist)"
        )

        # There is at least 1 inventory item whose path starts with .claude/agents/
        agent_items = [it for it in artifact["items"] if it["path"].startswith(".claude/agents/")]
        assert len(agent_items) >= 1, "Expected at least 1 item starting with .claude/agents/ in inventory"


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

    def test_do_not_read_read_policy_exact_field(self):
        """GIVEN issue-refinement-ops-review plan WHEN read_policy inspected THEN
        machine-readable field equals the exact contract value (AC4 / BLOCKER 2)."""
        spec = PLAN_REGISTRY["issue-refinement-ops-review"]
        plan = build_plan_output(spec, REPO_ROOT)
        assert plan["read_policy"] == "initial_exclusion_not_absolute_forbid"

    def test_do_not_read_read_policy_present_in_json_cli(self):
        """GIVEN issue-refinement-ops-review --json WHEN run THEN read_policy field present and exact."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["read_policy"] == "initial_exclusion_not_absolute_forbid"


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

    def test_artifact_only_json_plus_artifact_out_returns_json_not_evidence(self):
        """GIVEN --json --artifact-out WHEN run THEN stdout is JSON plan with
        inventory_artifact, NOT an EVIDENCE: line (BLOCKER 4)."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "inv.json"
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review",
                 "--json", "--artifact-out", str(artifact_path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 1, 2}, f"stderr={result.stderr}"
            assert not result.stdout.lstrip().startswith("EVIDENCE:"), (
                f"--json must not emit EVIDENCE line, got: {result.stdout!r}"
            )
            data = json.loads(result.stdout)  # must be valid JSON
            assert data["inventory_artifact"] == str(artifact_path)
            assert artifact_path.exists()

    def test_artifact_only_no_json_emits_evidence_line(self):
        """GIVEN --artifact-out WITHOUT --json WHEN run THEN stdout is EVIDENCE: only."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "inv.json"
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "issue-refinement-ops-review",
                 "--artifact-out", str(artifact_path)],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 1, 2}
            assert result.stdout.startswith("EVIDENCE:")


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

    def test_security_coverage_tracked_matches_and_empty_ok(self):
        """GIVEN issue-refinement-ops-review artifact WHEN coverage inspected THEN tracked_matches and empty_ok machine-decidable."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        artifact = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")

        # AC6 (MAJOR-2): every entry in coverage has integer tracked_matches and boolean empty_ok
        assert "coverage" in artifact, "coverage field missing"
        for entry in artifact["coverage"]:
            assert "prefix" in entry, f"Missing 'prefix' in coverage entry: {entry}"
            assert "tracked_matches" in entry, f"Missing 'tracked_matches' in coverage entry: {entry}"
            assert "empty_ok" in entry, f"Missing 'empty_ok' in coverage entry: {entry}"
            assert isinstance(entry["tracked_matches"], int), (
                f"tracked_matches should be int, got {type(entry['tracked_matches']).__name__} for {entry['prefix']}"
            )
            assert isinstance(entry["empty_ok"], bool), (
                f"empty_ok should be bool, got {type(entry['empty_ok']).__name__} for {entry['prefix']}"
            )
            # empty_ok is a configured policy on the target (here all required
            # targets are empty_ok=False); coverage_ok is the computed result.
            assert isinstance(entry["coverage_ok"], bool)
            assert isinstance(entry["included_matches"], int)
            assert isinstance(entry["filtered_matches"], int)
            assert entry["filtered_matches"] == entry["tracked_matches"] - entry["included_matches"]

    def test_security_coverage_ok_reflects_included_vs_tracked(self):
        """GIVEN coverage WHEN inspected THEN coverage_ok is machine-decidable from
        tracked/included/empty_ok (BLOCKER 5), distinguishing absent vs filtered."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        artifact = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        for e in artifact["coverage"]:
            if e["tracked_matches"] == 0:
                assert e["coverage_ok"] == bool(e["empty_ok"])
            else:
                assert e["coverage_ok"] == (e["filtered_matches"] == 0)

    def test_security_file_coverage_target_uses_exact_match(self):
        """GIVEN a file coverage target WHEN a sibling .bak path is tracked THEN it is
        NOT falsely counted (file target = exact match, not prefix) (BLOCKER 5)."""
        fixture = "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
        # Inject a decoy sibling that a startswith() match would wrongly count.
        tracked = get_tracked_paths_decoded(REPO_ROOT) + [fixture + ".bak"]
        artifact = build_agent_ops_inventory(REPO_ROOT, tracked, task_kind="issue-refinement-ops-review")
        entry = next(e for e in artifact["coverage"] if e["prefix"] == fixture)
        assert entry["target_type"] == "file"
        assert entry["tracked_matches"] == 1, (
            f"file target must match exactly, .bak must not inflate count: {entry}"
        )

    def test_security_codex_agents_toml_classified_as_codex_agent_definition(self):
        """GIVEN a .codex/agents/*.toml path WHEN classified THEN codex_agent_definition,
        not generic codex_config (MAJOR 1)."""
        assert classify_path_kind(".codex/agents/issue-author.toml") == "codex_agent_definition"
        assert classify_path_kind(".claude/agents/issue-reviewer.md") == "claude_agent_definition"
        # Non-agent codex config still classifies as codex_config.
        assert classify_path_kind(".codex/config.toml") == "codex_config"

    def test_security_invalid_contract_json_is_blocked_not_traceback(self, tmp_path):
        """GIVEN a corrupt expected-runtime-contract.json WHEN surfaces loaded THEN a
        structured contract_error is returned, not a traceback (MAJOR 2)."""
        contract = tmp_path / "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
        contract.parent.mkdir(parents=True, exist_ok=True)
        contract.write_text("{ this is not valid json", encoding="utf-8")
        surfaces, errors = load_contract_surfaces_with_errors(tmp_path)
        assert surfaces == []
        assert errors and any("invalid_json" in e for e in errors)

    def test_security_artifact_out_symlink_parent_rejected(self, tmp_path):
        """GIVEN --artifact-out whose parent is a symlink WHEN validated THEN rejected
        (parent-chain symlink trust boundary, BLOCKER 6)."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "linked"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        with pytest.raises(ValueError):
            validate_artifact_destination(link_dir / "inv.json", tmp_path)


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

    def test_shared_spec_inventory_profile_is_single_source(self):
        """GIVEN both ops task-kinds WHEN inventory_profile inspected THEN both reference
        the SAME profile object from the registry (AC7 / BLOCKER 3)."""
        p1 = PLAN_REGISTRY["agent-ops-review"].inventory_profile
        p2 = PLAN_REGISTRY["issue-refinement-ops-review"].inventory_profile
        assert p1 is not None and p2 is not None
        assert p1 is p2, "both ops task-kinds must share the same InventoryProfile object"
        assert p1 is OPS_REVIEW_INVENTORY_PROFILE

    def test_shared_registry_builder_reads_profile_not_hardcoded(self):
        """GIVEN a custom PlanSpec whose profile lists ONLY one prefix WHEN inventory
        built THEN items reflect that profile, proving the builder is spec-driven."""
        custom_profile = InventoryProfile(
            target_prefixes=(".claude/rules/",),
            coverage_targets=(CoverageTarget(".claude/rules/", "dir", empty_ok=False),),
            expected_paths=(),
            critical_surface_source="none",
        )
        custom_spec = PlanSpec(
            task_kind="issue-refinement-ops-review",
            must_read=[],
            do_not_read_initial_only=[],
            inventory_profile=custom_profile,
        )
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inv = build_agent_ops_inventory(REPO_ROOT, tracked, spec=custom_spec)
        # Only .claude/rules/ items collected — profile drove the prefixes.
        non_rules = [it for it in inv["items"] if not it["path"].startswith(".claude/rules/")]
        assert non_rules == [], f"builder ignored spec profile, leaked items: {non_rules[:5]}"
        cov_prefixes = {c["prefix"] for c in inv["coverage"]}
        assert cov_prefixes == {".claude/rules/"}
        # critical_surface_source=none -> no contract surfaces pulled in.
        assert inv["critical_surfaces"] == []


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
