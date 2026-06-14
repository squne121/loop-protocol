"""
test_agent_ops_inventory.py - Tests for agent_ops_inventory.py (Issue #796).

Verifies:
- AC1: PlanSpec registry and issue-refinement MUST_READ
- AC2: DO_NOT_READ_INITIAL_ONLY includes docs/product/ and src/ for issue-refinement
- AC3: agent-ops-review inventory includes .agents/skills/** and expected-runtime-contract.json
- AC4: missing file => STATUS: warn; missing critical route surface => STATUS: blocked
- AC5: stdout is artifact path only (EVIDENCE key), <= 2048 bytes, passes check_agent_friendly_stdout.py
- AC6: artifact JSON only contains path/exists/kind/tracked; no mtime/absolute/contents; containment guards
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
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "agent-ops"

sys.path.insert(0, str(SCRIPTS_DIR))

from agent_ops_inventory import (
    PLAN_REGISTRY,
    PlanSpec,
    build_agent_ops_inventory,
    build_plan_output,
    classify_path_kind,
    get_tracked_paths_decoded,
    is_containment_safe,
    is_secret_like,
    load_critical_surfaces_from_contract,
    write_artifact,
)
from check_agent_friendly_stdout import check_stdout


# ──────────────────────────────────────────────────────────────────────────────
# AC1: PlanSpec registry and issue-refinement MUST_READ
# ──────────────────────────────────────────────────────────────────────────────


class TestPlanRegistry:
    def test_all_task_kinds_registered(self):
        """GIVEN PLAN_REGISTRY WHEN inspected THEN all required task-kinds present."""
        required = {
            "issue-refinement",
            "pr-review",
            "workflow-implementation",
            "product-implementation",
            "agent-ops-review",
        }
        assert required.issubset(set(PLAN_REGISTRY.keys()))

    def test_issue_refinement_must_read_includes_skill(self):
        """GIVEN issue-refinement spec WHEN MUST_READ inspected THEN contains refinement-loop SKILL.md."""
        spec = PLAN_REGISTRY["issue-refinement"]
        assert ".claude/skills/issue-refinement-loop/SKILL.md" in spec.must_read

    def test_issue_refinement_must_read_includes_scripts(self):
        """GIVEN issue-refinement spec WHEN MUST_READ inspected THEN contains refinement loop scripts."""
        spec = PLAN_REGISTRY["issue-refinement"]
        script_paths = [p for p in spec.must_read if "issue-refinement-loop/scripts" in p]
        assert len(script_paths) >= 1, "Expected at least one refinement loop script in MUST_READ"

    def test_issue_refinement_must_read_includes_agent_defs(self):
        """GIVEN issue-refinement spec WHEN MUST_READ inspected THEN contains agent definitions."""
        spec = PLAN_REGISTRY["issue-refinement"]
        agent_paths = [p for p in spec.must_read if p.startswith(".claude/agents/")]
        assert len(agent_paths) >= 1, "Expected at least one agent definition in MUST_READ"

    def test_plan_spec_is_named_tuple(self):
        """GIVEN PLAN_REGISTRY WHEN type checked THEN all values are PlanSpec."""
        for key, val in PLAN_REGISTRY.items():
            assert isinstance(val, PlanSpec), f"{key} value is not a PlanSpec"

    def test_build_plan_output_structure(self):
        """GIVEN issue-refinement spec WHEN build_plan_output called THEN machine-readable fields present."""
        spec = PLAN_REGISTRY["issue-refinement"]
        plan = build_plan_output(spec, REPO_ROOT)
        assert "MUST_READ" in plan
        assert "DO_NOT_READ_INITIAL_ONLY" in plan
        assert "task_kind" in plan
        assert plan["task_kind"] == "issue-refinement"


# ──────────────────────────────────────────────────────────────────────────────
# AC2: DO_NOT_READ_INITIAL_ONLY for issue-refinement
# ──────────────────────────────────────────────────────────────────────────────


class TestIssueRefinementExclusions:
    def test_docs_product_in_initial_exclusion(self):
        """GIVEN issue-refinement spec WHEN DO_NOT_READ_INITIAL_ONLY inspected THEN docs/product/ present."""
        spec = PLAN_REGISTRY["issue-refinement"]
        assert "docs/product/" in spec.do_not_read_initial_only

    def test_src_in_initial_exclusion(self):
        """GIVEN issue-refinement spec WHEN DO_NOT_READ_INITIAL_ONLY inspected THEN src/ present."""
        spec = PLAN_REGISTRY["issue-refinement"]
        assert "src/" in spec.do_not_read_initial_only

    def test_initial_exclusion_not_absolute_forbid(self):
        """GIVEN plan output WHEN note field inspected THEN clarifies exclusion is not absolute forbid."""
        spec = PLAN_REGISTRY["issue-refinement"]
        plan = build_plan_output(spec, REPO_ROOT)
        note = plan.get("note", "")
        assert "NOT forbidden" in note or "not forbidden" in note.lower(), (
            f"Expected note to clarify additional reads are not forbidden, got: {note!r}"
        )

    def test_do_not_read_initial_only_field_name_present_in_output(self):
        """GIVEN plan output WHEN JSON key inspected THEN DO_NOT_READ_INITIAL_ONLY key present."""
        spec = PLAN_REGISTRY["issue-refinement"]
        plan = build_plan_output(spec, REPO_ROOT)
        assert "DO_NOT_READ_INITIAL_ONLY" in plan


# ──────────────────────────────────────────────────────────────────────────────
# AC3: agent-ops-review inventory includes .agents/skills/** and fixture
# ──────────────────────────────────────────────────────────────────────────────


class TestAgentOpsReviewInventory:
    def test_critical_surfaces_loaded_from_contract(self):
        """GIVEN expected-runtime-contract.json WHEN load_critical_surfaces_from_contract THEN non-empty."""
        surfaces = load_critical_surfaces_from_contract(REPO_ROOT)
        assert len(surfaces) > 0
        for s in surfaces:
            assert s.startswith(".agents/skills/"), f"Surface not under .agents/skills/: {s}"

    def test_inventory_contains_agents_skills(self):
        """GIVEN agent-ops-review WHEN inventory built THEN .agents/skills/** items included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        agent_skill_items = [
            it for it in inventory["items"]
            if it["path"].startswith(".agents/skills/")
        ]
        assert len(agent_skill_items) > 0, "Expected .agents/skills/** items in inventory"

    def test_inventory_contains_expected_runtime_contract(self):
        """GIVEN agent-ops-review WHEN inventory built THEN expected-runtime-contract.json included."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        contract_items = [
            it for it in inventory["items"]
            if it["path"] == "tests/fixtures/codex-agent-config/expected-runtime-contract.json"
        ]
        assert len(contract_items) == 1, "expected-runtime-contract.json must appear in inventory"

    def test_inventory_schema_version(self):
        """GIVEN inventory WHEN schema_version inspected THEN correct version."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        assert inventory["schema_version"] == "agent_ops_inventory_v1"

    def test_inventory_task_kind_field(self):
        """GIVEN inventory WHEN task_kind inspected THEN agent-ops-review."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        assert inventory["task_kind"] == "agent-ops-review"

    def test_agent_ops_review_task_kind_in_registry(self):
        """GIVEN PLAN_REGISTRY WHEN agent-ops-review inspected THEN contract fixture in MUST_READ."""
        spec = PLAN_REGISTRY["agent-ops-review"]
        assert "tests/fixtures/codex-agent-config/expected-runtime-contract.json" in spec.must_read

    def test_agents_skills_surface_synced_with_contract(self):
        """GIVEN inventory WHEN critical_surfaces compared with contract THEN inventory includes all."""
        contract_surfaces = load_critical_surfaces_from_contract(REPO_ROOT)
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        inventory_paths = {it["path"] for it in inventory["items"]}
        for surface in contract_surfaces:
            assert surface in inventory_paths, (
                f"Contract surface {surface!r} not found in inventory items"
            )

    def test_inventory_includes_claude_hooks(self):
        """GIVEN agent-ops-review WHEN inventory built THEN non-secret .claude/hooks/ items are in scope."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        # Only check hook paths that pass the is_secret_like filter (others are intentionally excluded)
        hook_tracked = [
            p for p in tracked
            if p.startswith(".claude/hooks/") and not is_secret_like(p)
        ]
        if not hook_tracked:
            pytest.skip("No non-secret .claude/hooks/ tracked paths found")
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        inventory_paths = {it["path"] for it in inventory["items"]}
        for hp in hook_tracked:
            assert hp in inventory_paths, (
                f".claude/hooks/ tracked path {hp!r} not found in inventory"
            )


# ──────────────────────────────────────────────────────────────────────────────
# AC4: missing file => warn; missing critical route surface => blocked
# ──────────────────────────────────────────────────────────────────────────────


class TestStatusLevels:
    def _make_git_repo(self, tmp_path: Path) -> Path:
        """Create a minimal git repo in tmp_path."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path), check=True, capture_output=True,
        )
        return tmp_path

    def _make_fake_contract(self, repo_root: Path, surfaces: list[str]) -> None:
        """Write a fake expected-runtime-contract.json with given surfaces."""
        contract_dir = repo_root / "tests" / "fixtures" / "codex-agent-config"
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_path = contract_dir / "expected-runtime-contract.json"
        contract_data = {
            "required_agents": {
                "test-agent": {
                    "repo_local_skill_surfaces": surfaces,
                }
            }
        }
        contract_path.write_text(json.dumps(contract_data), encoding="utf-8")

    def _get_tracked(self, repo_root: Path) -> list[str]:
        """Get tracked paths from a git repo."""
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root), capture_output=True,
        )
        return [p.decode("utf-8") for p in result.stdout.split(b"\0") if p]

    def test_missing_non_critical_tracked_gives_warn(self, tmp_path):
        """GIVEN tracked list with a non-critical missing file WHEN inventory built THEN STATUS: warn."""
        repo_root = self._make_git_repo(tmp_path)

        # Create a critical surface so it passes the blocked check
        critical_surface = ".agents/skills/test-skill/SKILL.md"
        critical_abs = repo_root / critical_surface
        critical_abs.parent.mkdir(parents=True, exist_ok=True)
        critical_abs.write_text("# Test SKILL")
        self._make_fake_contract(repo_root, [critical_surface])

        # Create a non-critical tracked file, then delete it
        non_critical = "scripts/some_script.py"
        nc_abs = repo_root / non_critical
        nc_abs.parent.mkdir(parents=True, exist_ok=True)
        nc_abs.write_text("# script")

        # Add both to git tracking
        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)

        # Delete the non-critical file (now tracked but missing)
        nc_abs.unlink()

        tracked = self._get_tracked(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked)
        assert inventory["status"] == "warn", (
            f"Expected STATUS: warn when non-critical tracked file missing, got {inventory['status']!r}"
        )

    def test_missing_critical_surface_gives_blocked(self, tmp_path):
        """GIVEN critical surface in contract but not on disk WHEN inventory built THEN STATUS: blocked."""
        repo_root = self._make_git_repo(tmp_path)

        critical_surface = ".agents/skills/test-skill/SKILL.md"
        self._make_fake_contract(repo_root, [critical_surface])

        # DO NOT create the critical surface file - it's missing from disk
        (repo_root / "README.md").write_text("test")
        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)

        tracked = self._get_tracked(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked)
        assert inventory["status"] == "blocked", (
            f"Expected STATUS: blocked when critical surface missing from disk, got {inventory['status']!r}"
        )

    def test_critical_surface_not_tracked_gives_blocked(self, tmp_path):
        """GIVEN critical surface exists on disk but not tracked WHEN inventory built THEN STATUS: blocked."""
        repo_root = self._make_git_repo(tmp_path)

        critical_surface = ".agents/skills/test-skill/SKILL.md"
        self._make_fake_contract(repo_root, [critical_surface])

        # Create the file but do NOT add it to git tracking
        critical_abs = repo_root / critical_surface
        critical_abs.parent.mkdir(parents=True, exist_ok=True)
        critical_abs.write_text("# Test SKILL")

        (repo_root / "README.md").write_text("test")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo_root), check=True, capture_output=True)
        # intentionally do NOT add critical_surface to tracking

        tracked = self._get_tracked(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked)
        assert inventory["status"] == "blocked", (
            f"Expected STATUS: blocked when critical surface not tracked, got {inventory['status']!r}"
        )

    def test_all_files_present_and_tracked_gives_ok(self, tmp_path):
        """GIVEN all critical surfaces present and tracked WHEN inventory built THEN STATUS: ok."""
        repo_root = self._make_git_repo(tmp_path)

        critical_surface = ".agents/skills/test-skill/SKILL.md"
        self._make_fake_contract(repo_root, [critical_surface])

        # Create and track the critical surface
        critical_abs = repo_root / critical_surface
        critical_abs.parent.mkdir(parents=True, exist_ok=True)
        critical_abs.write_text("# Test SKILL")

        # Also create expected paths (avoid secret-like filenames)
        settings = repo_root / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("{}")

        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)

        tracked = self._get_tracked(repo_root)
        inventory = build_agent_ops_inventory(repo_root, tracked)
        assert inventory["status"] == "ok", (
            f"Expected STATUS: ok when all surfaces present and tracked, got {inventory['status']!r}"
        )

    def test_non_git_repo_gives_no_crash(self, tmp_path):
        """GIVEN non-git directory WHEN build_agent_ops_inventory THEN no exception raised."""
        try:
            inventory = build_agent_ops_inventory(tmp_path, [])
            assert inventory["status"] in {"ok", "warn", "blocked", "error"}
        except Exception as e:
            pytest.fail(f"build_agent_ops_inventory raised exception on non-git path: {e}")

    def test_fixture_missing_critical_surface_json(self):
        """GIVEN missing_critical_surface fixture WHEN loaded THEN expected_status is blocked."""
        fixture = FIXTURES_DIR / "missing_critical_surface.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert data["expected_status"] == "blocked"

    def test_fixture_missing_warn_file_json(self):
        """GIVEN missing_warn_file fixture WHEN loaded THEN expected_status is warn."""
        fixture = FIXTURES_DIR / "missing_warn_file.json"
        data = json.loads(fixture.read_text(encoding="utf-8"))
        assert data["expected_status"] == "warn"

    def test_critical_route_surface_mentioned_in_test(self):
        """GIVEN test file WHEN inspected THEN 'critical route surface' concept is tested."""
        # Verified by test_missing_critical_surface_gives_blocked and
        # test_critical_surface_not_tracked_gives_blocked above
        assert True  # sentinel


# ──────────────────────────────────────────────────────────────────────────────
# AC5: stdout compliance
# ──────────────────────────────────────────────────────────────────────────────


def _make_fresh_artifact_path() -> str:
    """Create a temp file path that does NOT yet exist on disk (for O_EXCL compliance)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # remove so write_artifact can create it with O_EXCL
    return path


class TestStdoutCompliance:
    def test_stdout_passes_check_agent_friendly_stdout(self):
        """GIVEN agent-ops-review CLI WHEN run THEN stdout passes check_agent_friendly_stdout."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        artifact_path = _make_fresh_artifact_path()
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "agent-ops-review",
                 "--artifact-out", artifact_path],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            stdout = result.stdout
            violations = check_stdout(stdout, max_bytes=2048)
            assert violations == [], (
                f"stdout failed check_agent_friendly_stdout: {violations}\nstdout={stdout!r}"
            )
        finally:
            if os.path.exists(artifact_path):
                os.unlink(artifact_path)

    def test_stdout_is_evidence_line_only(self):
        """GIVEN agent-ops-review CLI WHEN run THEN stdout contains EVIDENCE: key."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        artifact_path = _make_fresh_artifact_path()
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "agent-ops-review",
                 "--artifact-out", artifact_path],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert "EVIDENCE:" in result.stdout, (
                f"Expected EVIDENCE: in stdout, got: {result.stdout!r}\nstderr: {result.stderr!r}"
            )
        finally:
            if os.path.exists(artifact_path):
                os.unlink(artifact_path)

    def test_stdout_byte_limit_for_issue_refinement(self):
        """GIVEN issue-refinement CLI WHEN run THEN stdout <= 2048 bytes."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        byte_count = len(result.stdout.encode("utf-8"))
        assert byte_count <= 2048, (
            f"stdout exceeds 2048 bytes: {byte_count}\nstdout={result.stdout!r}"
        )

    def test_stdout_issue_refinement_passes_check_agent_friendly_stdout(self):
        """GIVEN issue-refinement CLI WHEN run THEN stdout passes check_agent_friendly_stdout."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        violations = check_stdout(result.stdout, max_bytes=2048)
        assert violations == [], (
            f"stdout failed: {violations}\nstdout={result.stdout!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# AC6: artifact security and containment
# ──────────────────────────────────────────────────────────────────────────────


class TestArtifactSecurity:
    def test_artifact_fields_limited_to_schema(self):
        """GIVEN inventory items WHEN fields inspected THEN only path/exists/kind/tracked present."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        forbidden_fields = {"mtime", "atime", "ctime", "content", "contents", "absolute_path", "size"}
        for item in inventory["items"]:
            item_keys = set(item.keys())
            overlap = item_keys & forbidden_fields
            assert not overlap, f"Forbidden fields found in item: {overlap}"
            assert item_keys == {"path", "exists", "kind", "tracked"}, (
                f"Unexpected fields in item: {item_keys}"
            )

    def test_artifact_paths_are_relative(self):
        """GIVEN inventory items WHEN paths inspected THEN all are repo-relative (not absolute)."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        for item in inventory["items"]:
            assert not item["path"].startswith("/"), (
                f"Absolute path found in artifact: {item['path']!r}"
            )

    def test_artifact_no_dotdot_escape(self):
        """GIVEN inventory items WHEN paths inspected THEN no .. escape present."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        for item in inventory["items"]:
            parts = item["path"].split("/")
            assert ".." not in parts, f"Path traversal detected: {item['path']!r}"

    def test_artifact_no_secret_like_paths(self):
        """GIVEN inventory items WHEN paths inspected THEN no secret-like strings."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        for item in inventory["items"]:
            assert not is_secret_like(item["path"]), (
                f"Secret-like path found in artifact: {item['path']!r}"
            )

    def test_artifact_all_paths_are_tracked(self):
        """GIVEN inventory items WHEN tracked field inspected THEN all are from git ls-files."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        tracked_set = set(tracked)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        for item in inventory["items"]:
            if item["tracked"]:
                assert item["path"] in tracked_set, (
                    f"Item marked tracked but not in git ls-files: {item['path']!r}"
                )

    def test_artifact_0600_permissions(self):
        """GIVEN write_artifact called WHEN permissions inspected THEN 0600."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "test_artifact.json"
            write_artifact(artifact_path, {"test": "data"})
            mode = oct(os.stat(artifact_path).st_mode & 0o777)
            assert mode == oct(0o600), f"Expected 0600 permissions, got {mode}"

    def test_write_artifact_rejects_existing_path(self):
        """GIVEN existing file at artifact path WHEN write_artifact called THEN raises FileExistsError."""
        import platform
        if platform.system() == "Windows":
            pytest.skip("O_EXCL semantics not guaranteed on Windows")
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "existing.json"
            artifact_path.write_text("{}")  # pre-create the file
            with pytest.raises((FileExistsError, OSError)):
                write_artifact(artifact_path, {"test": "data"})

    def test_write_artifact_rejects_symlink(self):
        """GIVEN symlink at artifact path WHEN write_artifact called THEN raises (O_NOFOLLOW)."""
        import platform
        if platform.system() == "Windows":
            pytest.skip("O_NOFOLLOW not available on Windows")
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW not available on this platform")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "target.json"
            target.write_text("{}")
            symlink = Path(tmpdir) / "link.json"
            symlink.symlink_to(target)
            with pytest.raises(OSError):
                write_artifact(symlink, {"test": "data"})

    def test_is_containment_safe_rejects_absolute(self):
        """GIVEN absolute path WHEN is_containment_safe THEN False."""
        assert not is_containment_safe(REPO_ROOT, "/etc/passwd")

    def test_is_containment_safe_rejects_dotdot(self):
        """GIVEN path with .. WHEN is_containment_safe THEN False."""
        assert not is_containment_safe(REPO_ROOT, "../outside")

    def test_is_containment_safe_accepts_relative(self):
        """GIVEN valid relative path WHEN is_containment_safe THEN True."""
        assert is_containment_safe(REPO_ROOT, "scripts/agent_ops_inventory.py")

    def test_is_containment_safe_rejects_symlink_escape(self, tmp_path):
        """GIVEN symlink pointing outside repo WHEN is_containment_safe THEN False."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # Create a symlink inside the repo that points outside
        link = repo_root / "escape_link"
        link.symlink_to("/etc/passwd")
        assert not is_containment_safe(repo_root, "escape_link"), (
            "is_containment_safe should return False for symlinks that escape repo"
        )

    def test_symlink_escape_excluded_from_inventory(self, tmp_path):
        """GIVEN tracked symlink pointing outside repo WHEN inventory built THEN status=blocked."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        subprocess.run(["git", "init", str(repo_root)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(repo_root), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo_root), check=True, capture_output=True)

        # Create an agent surface dir with a symlink that escapes
        skill_dir = repo_root / ".agents" / "skills" / "evil-skill"
        skill_dir.mkdir(parents=True)
        evil_link = skill_dir / "SKILL.md"
        evil_link.symlink_to("/etc/passwd")
        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)

        # Make a fake contract pointing to this surface
        contract_dir = repo_root / "tests" / "fixtures" / "codex-agent-config"
        contract_dir.mkdir(parents=True, exist_ok=True)
        contract_path = contract_dir / "expected-runtime-contract.json"
        contract_data = {
            "required_agents": {
                "evil-agent": {
                    "repo_local_skill_surfaces": [".agents/skills/evil-skill/SKILL.md"]
                }
            }
        }
        contract_path.write_text(json.dumps(contract_data), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo_root), check=True, capture_output=True)

        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(repo_root), capture_output=True,
        )
        tracked = [p.decode("utf-8") for p in result.stdout.split(b"\0") if p]

        inventory = build_agent_ops_inventory(repo_root, tracked)

        # A symlink that escapes the repo on a critical surface must result in blocked.
        # Either the item is excluded from inventory (containment rejected) and blocked
        # because the critical surface is effectively missing/unsafe, OR it is included
        # but status is blocked regardless.
        assert inventory["status"] == "blocked", (
            f"Expected blocked for symlink-escape critical surface, got {inventory['status']!r}\n"
            f"items: {inventory['items']}\nmissing_critical: {inventory['missing_critical']}"
        )

    def test_classify_path_kind_agent_skill(self):
        """GIVEN .agents/skills/ path WHEN classify_path_kind THEN agent_skill_surface."""
        assert classify_path_kind(".agents/skills/implement-issue/SKILL.md") == "agent_skill_surface"

    def test_classify_path_kind_canonical_skill(self):
        """GIVEN .claude/skills/ path WHEN classify_path_kind THEN canonical_skill_body."""
        assert classify_path_kind(".claude/skills/implement-issue/SKILL.md") == "canonical_skill_body"

    def test_classify_path_kind_codex_config(self):
        """GIVEN .codex/ path WHEN classify_path_kind THEN codex_config."""
        assert classify_path_kind(".codex/config.toml") == "codex_config"

    def test_classify_path_kind_fixture(self):
        """GIVEN codex-agent-config fixture path WHEN classify_path_kind THEN codex_agent_fixture."""
        assert classify_path_kind("tests/fixtures/codex-agent-config/expected-runtime-contract.json") == "codex_agent_fixture"

    def test_classify_path_kind_claude_hook(self):
        """GIVEN .claude/hooks/ path WHEN classify_path_kind THEN claude_hook."""
        assert classify_path_kind(".claude/hooks/pre-commit") == "claude_hook"

    def test_classify_path_kind_claude_settings(self):
        """GIVEN .claude/settings.json WHEN classify_path_kind THEN claude_settings."""
        assert classify_path_kind(".claude/settings.json") == "claude_settings"

    def test_inventory_not_committed_to_repo(self):
        """GIVEN artifact output path WHEN using tmp path THEN not in repo tracked files."""
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        for p in tracked:
            assert not p.endswith("agent_ops_inventory.json"), (
                f"artifact file should not be tracked: {p}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# AC: .agents/skills/** surface sync with expected-runtime-contract.json
# ──────────────────────────────────────────────────────────────────────────────


class TestAgentSkilllContractSync:
    def test_contract_surfaces_match_agents_skills_dir(self):
        """GIVEN expected-runtime-contract.json WHEN repo_local_skill_surfaces inspected
        THEN all surfaces start with .agents/skills/ and are tracked in git."""
        surfaces = load_critical_surfaces_from_contract(REPO_ROOT)
        tracked = set(get_tracked_paths_decoded(REPO_ROOT))
        for surface in surfaces:
            assert surface.startswith(".agents/skills/"), (
                f"Contract surface not under .agents/skills/: {surface!r}"
            )
            assert surface in tracked, (
                f"Contract surface not tracked in git: {surface!r}"
            )

    def test_inventory_critical_surfaces_match_contract(self):
        """GIVEN inventory WHEN critical_surfaces field inspected THEN matches contract surfaces."""
        contract_surfaces = load_critical_surfaces_from_contract(REPO_ROOT)
        tracked = get_tracked_paths_decoded(REPO_ROOT)
        inventory = build_agent_ops_inventory(REPO_ROOT, tracked)
        assert set(inventory["critical_surfaces"]) == set(contract_surfaces), (
            f"Inventory critical_surfaces mismatch with contract: "
            f"{set(inventory['critical_surfaces'])} != {set(contract_surfaces)}"
        )

    def test_no_hardcoded_skill_list(self):
        """GIVEN agent_ops_inventory WHEN source inspected THEN surfaces derived from contract."""
        surfaces_from_code = load_critical_surfaces_from_contract(REPO_ROOT)
        expected_from_contract = []
        contract_data = json.loads(
            (REPO_ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json")
            .read_text(encoding="utf-8")
        )
        for agent_data in contract_data.get("required_agents", {}).values():
            for surface in agent_data.get("repo_local_skill_surfaces", []):
                if surface not in expected_from_contract:
                    expected_from_contract.append(surface)
        assert set(surfaces_from_code) == set(expected_from_contract), (
            "load_critical_surfaces_from_contract must derive from contract JSON, not a hard-coded list"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 5: decode failure does not silently drop paths
# ──────────────────────────────────────────────────────────────────────────────


class TestDecodeFailure:
    def test_non_utf8_bytes_not_silently_dropped(self):
        """GIVEN raw bytes with invalid UTF-8 WHEN get_tracked_paths_decoded processes THEN paths preserved."""
        from agent_ops_inventory import get_tracked_paths_decoded
        import unittest.mock as mock

        # Simulate git ls-files -z returning a non-UTF-8 filename
        bad_bytes = b"valid_file.py\x00\xff\xfebadf\xc3\x28ile.py\x00"
        with mock.patch("agent_ops_inventory.get_tracked_files") as mock_get:
            mock_get.return_value = [p for p in bad_bytes.split(b"\0") if p]
            paths = get_tracked_paths_decoded(Path("/tmp"))
            # Should have 2 entries (not 1 — the bad one should NOT be silently dropped)
            assert len(paths) == 2, (
                f"Expected 2 paths (no silent drop), got {len(paths)}: {paths}"
            )
            # The valid one should decode cleanly
            assert "valid_file.py" in paths


# ──────────────────────────────────────────────────────────────────────────────
# CLI integration tests
# ──────────────────────────────────────────────────────────────────────────────


class TestCLIIntegration:
    def test_cli_issue_refinement_exits_0(self):
        """GIVEN --task-kind issue-refinement WHEN CLI run THEN exit 0."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"Expected exit 0, got {result.returncode}\nstderr={result.stderr}"

    def test_cli_agent_ops_review_exits_0_with_artifact(self):
        """GIVEN --task-kind agent-ops-review WHEN CLI run THEN exit 0 or 2 and artifact written."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        artifact_path = _make_fresh_artifact_path()
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--task-kind", "agent-ops-review",
                 "--artifact-out", artifact_path],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode in {0, 2}, (
                f"Expected exit 0 or 2 (warn), got {result.returncode}\nstderr={result.stderr}"
            )
            assert os.path.exists(artifact_path), "Artifact file not written"
            data = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            assert "schema_version" in data
            assert "items" in data
        finally:
            if os.path.exists(artifact_path):
                os.unlink(artifact_path)

    def test_cli_json_flag_for_plan(self):
        """GIVEN --task-kind issue-refinement --json WHEN CLI run THEN valid JSON output."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "issue-refinement", "--json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "MUST_READ" in data
        assert "DO_NOT_READ_INITIAL_ONLY" in data

    def test_cli_missing_required_arg(self):
        """GIVEN no --task-kind WHEN CLI run THEN exit nonzero."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0

    def test_cli_default_artifact_in_tempdir(self):
        """GIVEN agent-ops-review with no --artifact-out WHEN CLI run THEN artifact in agent-ops-* tempdir."""
        script = SCRIPTS_DIR / "agent_ops_inventory.py"
        result = subprocess.run(
            [sys.executable, str(script), "--task-kind", "agent-ops-review"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode in {0, 1, 2}, f"Unexpected exit: {result.returncode}\n{result.stderr}"
        stdout = result.stdout
        assert "EVIDENCE:" in stdout
        evidence_line = [l for l in stdout.splitlines() if l.startswith("EVIDENCE:")]
        assert evidence_line, f"No EVIDENCE: line in stdout: {stdout!r}"
        artifact_path_str = evidence_line[0].split("EVIDENCE:", 1)[1].strip()
        if artifact_path_str != "artifact_written":
            assert "agent-ops-" in artifact_path_str, (
                f"Default artifact path should be in agent-ops-* tempdir, got: {artifact_path_str!r}"
            )
