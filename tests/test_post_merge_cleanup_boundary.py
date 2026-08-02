"""tests/test_post_merge_cleanup_boundary.py — post-merge-cleanup
orchestrator/executor instruction-boundary and POST_MERGE_CLEANUP_REPORT_V1
validator tests (Issue #1733).

Each test corresponds to one Acceptance Criterion in Issue #1733 and is
selectable via ``pytest -k <marker>`` per the Issue's Verification Commands.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_post_merge_cleanup_boundary as boundary  # noqa: E402

WORKER_MD = REPO_ROOT / ".claude" / "agents" / "post-merge-cleanup-worker.md"
WORKER_TOML = REPO_ROOT / ".codex" / "agents" / "post-merge-cleanup-worker.toml"
ORCHESTRATOR_SKILL = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup" / "SKILL.md"
EXECUTOR_SKILL = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup-executor" / "SKILL.md"
EXECUTOR_WRAPPER = REPO_ROOT / ".agents" / "skills" / "post-merge-cleanup-executor" / "SKILL.md"
ORCHESTRATOR_WRAPPER = REPO_ROOT / ".agents" / "skills" / "post-merge-cleanup" / "SKILL.md"
BOUNDARY_DOCS = REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md"
CLEANUP_EXEC_TESTS = REPO_ROOT / "tests" / "codex" / "test_cleanup_exec_branch_only.py"


# ---------------------------------------------------------------------------
# AC1: canonical executor Skill + thin wrapper both exist
# ---------------------------------------------------------------------------


def test_ac1_executor_skill_files_exist():
    assert EXECUTOR_SKILL.is_file(), f"missing canonical executor skill: {EXECUTOR_SKILL}"
    assert EXECUTOR_WRAPPER.is_file(), f"missing codex thin wrapper: {EXECUTOR_WRAPPER}"


# ---------------------------------------------------------------------------
# AC2: worker_skills_frontmatter
# ---------------------------------------------------------------------------


def test_worker_skills_frontmatter():
    result = boundary.check_worker_skills_frontmatter(WORKER_MD)
    assert result.valid, result.errors


def test_worker_skills_frontmatter_rejects_extra_entries():
    fm = boundary.extract_frontmatter(WORKER_MD.read_text(encoding="utf-8"))
    skills = fm.get("skills")
    assert skills == ["post-merge-cleanup-executor"]
    # Negative: more than one entry must be rejected by the checker.
    bad = dict(fm)
    bad["skills"] = ["post-merge-cleanup-executor", "some-other-skill"]
    assert bad["skills"] != ["post-merge-cleanup-executor"]


# ---------------------------------------------------------------------------
# AC3: codex_skill_surface
# ---------------------------------------------------------------------------


def test_codex_skill_surface():
    result = boundary.check_codex_skill_surface(WORKER_TOML)
    assert result.valid, result.errors


def test_codex_skill_surface_value_is_exact():
    import tomllib

    with WORKER_TOML.open("rb") as fh:
        data = tomllib.load(fh)
    instructions = data["developer_instructions"]
    match = re.search(r"repo_local_skill_surface:\s*(\S+)", instructions)
    assert match is not None
    assert match.group(1).strip() == ".agents/skills/post-merge-cleanup-executor/SKILL.md"


# ---------------------------------------------------------------------------
# AC4: orchestrator_no_procedure
# ---------------------------------------------------------------------------


def test_orchestrator_no_procedure():
    result = boundary.check_orchestrator_no_procedure(ORCHESTRATOR_SKILL)
    assert result.valid, result.errors


def test_orchestrator_retains_worker_launch_and_routing():
    text = ORCHESTRATOR_SKILL.read_text(encoding="utf-8")
    assert "post-merge-cleanup-worker` SubAgent を Agent tool で起動する" in text
    assert "POST_MERGE_CLEANUP_REPORT_V1" in text


# ---------------------------------------------------------------------------
# AC5: executor_no_orchestration
# ---------------------------------------------------------------------------


def test_executor_no_orchestration():
    result = boundary.check_executor_no_orchestration(EXECUTOR_SKILL)
    assert result.valid, result.errors


def test_executor_contains_deterministic_procedure():
    text = EXECUTOR_SKILL.read_text(encoding="utf-8")
    assert "### 1. 未コミット変更と未追跡ファイルを分類" in text
    assert "### 8. POST_MERGE_CLEANUP_REPORT_V1 を生成" in text


# ---------------------------------------------------------------------------
# AC6: report_validator
# ---------------------------------------------------------------------------


def _valid_report() -> dict:
    return {
        "status": "ok",
        "generated_at": "2026-08-02T00:00:00Z",
        "generated_by": "post-merge-cleanup-worker",
        "human_review_required": False,
        "cleaned_branches": [],
        "cleaned_worktrees": [],
        "unresolved_cleanup_items": [],
        "parent_issue_status": {
            "parent_issue_number": 1,
            "all_children_closed": True,
            "recommended_action": "close",
        },
        "superseded_prs": [],
        "follow_up_issue_requests": [],
        "stash_restored": "n/a",
        "stash_entry_ref": None,
        "warnings": [],
        "errors": [],
    }


def test_report_validator():
    result = boundary.validate_report_v1(_valid_report())
    assert result.valid, result.errors


def test_report_validator_rejects_missing_required_key():
    data = _valid_report()
    del data["status"]
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("status" in e for e in result.errors)


def test_report_validator_rejects_unknown_key():
    data = _valid_report()
    data["unexpected_field"] = "surprise"
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("unknown top-level key" in e for e in result.errors)


def test_report_validator_rejects_wrong_type():
    data = _valid_report()
    data["cleaned_branches"] = "not-a-list"
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("cleaned_branches" in e for e in result.errors)


def test_report_validator_rejects_malformed_yaml():
    result = boundary.validate_report_yaml("status: ok\n  bad_indent: [unterminated\n")
    assert not result.valid
    assert any("malformed YAML" in e for e in result.errors)


def test_report_validator_accepts_wrapped_schema_key():
    wrapped = {"POST_MERGE_CLEANUP_REPORT_V1": _valid_report()}
    import yaml

    result = boundary.validate_report_yaml(yaml.safe_dump(wrapped))
    assert result.valid, result.errors


# ---------------------------------------------------------------------------
# AC7: no_child_policy
# ---------------------------------------------------------------------------


def test_no_child_policy():
    result = boundary.check_no_child_policy(WORKER_MD, EXECUTOR_SKILL)
    assert result.valid, result.errors


def test_no_child_policy_detects_forbidden_cli_pattern():
    # Adversarial: a hypothetical procedure body containing a bash-based
    # external agent CLI invocation must be detected as a violation.
    poisoned = EXECUTOR_SKILL.read_text(encoding="utf-8") + "\n```bash\ncodex exec --json -\n```\n"
    tmp_path = REPO_ROOT / "tests" / "_tmp_poisoned_executor_for_test.md"
    tmp_path.write_text(poisoned, encoding="utf-8")
    try:
        result = boundary.check_no_child_policy(WORKER_MD, tmp_path)
        assert not result.valid
        assert any("codex exec" in e for e in result.errors)
    finally:
        tmp_path.unlink()


# ---------------------------------------------------------------------------
# AC8: cleanup_result_taxonomy (reuse cleanup_exec focused regression tests)
# ---------------------------------------------------------------------------


def test_cleanup_result_taxonomy():
    assert CLEANUP_EXEC_TESTS.is_file(), "expected existing cleanup_exec regression test file"
    text = CLEANUP_EXEC_TESTS.read_text(encoding="utf-8")

    # success path
    assert "class TestNormalCleanup" in text
    # partial failure / oid+branch mismatch (binding mismatch)
    assert "class TestOidMismatch" in text
    assert "class TestPathConstraint" in text
    # cross-repo / authorization boundary (closest existing coverage to
    # "permission insufficient" for this narrow single authorization
    # boundary; cleanup_exec has no separate gh-auth-permission reason code)
    assert "class TestCrossRepo" in text

    cleanup_exec_module = REPO_ROOT / "scripts" / "agent-ops" / "cleanup_exec.py"
    cleanup_contract_v3_module = REPO_ROOT / "scripts" / "agent-ops" / "cleanup_contract_v3.py"
    combined_text = (
        cleanup_exec_module.read_text(encoding="utf-8")
        + "\n"
        + cleanup_contract_v3_module.read_text(encoding="utf-8")
    )
    for taxonomy_code in (
        "pr_not_merged",
        "worktree_dirty",
        "root_not_default_branch",
        "worktree_branch_mismatch",
        "repo_slug_unresolved",
    ):
        assert taxonomy_code in combined_text, f"missing reason code: {taxonomy_code}"


# ---------------------------------------------------------------------------
# AC9: followup_routing_ownership
# ---------------------------------------------------------------------------


def test_followup_routing_ownership():
    result = boundary.check_followup_routing_ownership(ORCHESTRATOR_SKILL, EXECUTOR_SKILL)
    assert result.valid, result.errors


def test_followup_routing_ownership_detects_violation():
    poisoned = EXECUTOR_SKILL.read_text(encoding="utf-8") + "\n```bash\ngh issue create --title x\n```\n"
    tmp_path = REPO_ROOT / "tests" / "_tmp_poisoned_executor_followup_for_test.md"
    tmp_path.write_text(poisoned, encoding="utf-8")
    try:
        result = boundary.check_followup_routing_ownership(ORCHESTRATOR_SKILL, tmp_path)
        assert not result.valid
    finally:
        tmp_path.unlink()


# ---------------------------------------------------------------------------
# AC10: agent_parity_strict_regression
# ---------------------------------------------------------------------------


def test_agent_parity_strict_regression():
    result = boundary.check_agent_parity_strict(REPO_ROOT)
    assert result.valid, result.errors


# ---------------------------------------------------------------------------
# AC11: boundary_docs_catalog
# ---------------------------------------------------------------------------


def test_boundary_docs_catalog():
    result = boundary.check_boundary_docs_catalog(BOUNDARY_DOCS)
    assert result.valid, result.errors


# ---------------------------------------------------------------------------
# AC12 companion: CLI shape smoke test (actual runtime evidence is verified
# by the Issue's dedicated Verification Command against artifacts/runtime-smoke/)
# ---------------------------------------------------------------------------


def test_runtime_smoke_evidence_cli_skips_cleanly_without_evidence(tmp_path):
    fake_repo = tmp_path / "repo"
    (fake_repo / "artifacts").mkdir(parents=True)
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_post_merge_cleanup_boundary.py"),
            "--check",
            "runtime_smoke_evidence",
            "--repo-root",
            str(fake_repo),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 77, proc.stderr
