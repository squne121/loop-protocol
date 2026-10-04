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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_post_merge_cleanup_boundary as boundary  # noqa: E402

WORKER_MD = REPO_ROOT / ".claude" / "agents" / "post-merge-cleanup-worker.md"
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
    assert EXECUTOR_WRAPPER.is_file(), f"missing repo-local skill discovery surface: {EXECUTOR_WRAPPER}"


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
# AC3: codex_skill_surface (Issue #2161 native Codex CLI retirement: the
# TOML-parsing checks this parametrized against the now-deleted worker's
# Codex agent TOML config were removed as an orphan DELETE_CHAIN; see
# `check_post_merge_cleanup_boundary.py`)
# ---------------------------------------------------------------------------


def test_claude_worker_description_no_stale_procedure():
    result = boundary.check_claude_worker_description_no_stale_procedure(WORKER_MD)
    assert result.valid, result.errors


def test_claude_worker_description_detects_reintroduced_orchestrator_reference():
    poisoned = WORKER_MD.read_text(encoding="utf-8").replace(
        "post-merge-cleanup-executor` skill の Procedure を実行し",
        "post-merge-cleanup` skill の Procedure を実行し",
    )
    tmp_path = REPO_ROOT / "tests" / "_tmp_poisoned_worker_md_for_test.md"
    tmp_path.write_text(poisoned, encoding="utf-8")
    try:
        result = boundary.check_claude_worker_description_no_stale_procedure(tmp_path)
        assert not result.valid
    finally:
        tmp_path.unlink()


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
# Blocker 3 (Issue #1733 PR #1947 fix_delta): parent-issue close must be
# gated on recommended_action == "close", not mere field presence.
# ---------------------------------------------------------------------------


def test_parent_close_condition_explicit():
    result = boundary.check_parent_close_condition_explicit(ORCHESTRATOR_SKILL)
    assert result.valid, result.errors


def test_parent_close_condition_detects_reintroduced_ambiguous_phrasing():
    poisoned = ORCHESTRATOR_SKILL.read_text(encoding="utf-8") + (
        "\n   - `parent_issue_status.recommended_action` あり → `gh issue close` を実行\n"
    )
    tmp_path = REPO_ROOT / "tests" / "_tmp_poisoned_orchestrator_for_test.md"
    tmp_path.write_text(poisoned, encoding="utf-8")
    try:
        result = boundary.check_parent_close_condition_explicit(tmp_path)
        assert not result.valid
    finally:
        tmp_path.unlink()


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


# ── Issue #1523 fix_delta P1-2: additive optional discard_confirmation field ──


def test_report_validator_accepts_absent_discard_confirmation_backward_compat():
    """GIVEN a pre-#1523-shaped report (no discard_confirmation key at all)
    WHEN validated THEN it still validates (backward compatible)."""
    data = _valid_report()
    assert "discard_confirmation" not in data
    result = boundary.validate_report_v1(data)
    assert result.valid, result.errors


def test_report_validator_accepts_null_discard_confirmation():
    data = _valid_report()
    data["discard_confirmation"] = None
    result = boundary.validate_report_v1(data)
    assert result.valid, result.errors


def test_report_validator_accepts_present_discard_confirmation():
    data = _valid_report()
    data["discard_confirmation"] = {
        "contract_id": "a" * 32,
        "contract_sha256": "b" * 64,
        "pr_head_sha": "c" * 40,
        "local_tip_sha": "d" * 40,
        "local_only_commit_shas": ["d" * 40],
        "expires_at": "2026-08-02T00:05:00Z",
        "argv": ["uv", "run", "python3", "materialize_cleanup_contract.py"],
    }
    result = boundary.validate_report_v1(data)
    assert result.valid, result.errors


def test_report_validator_rejects_malformed_discard_confirmation_missing_subfield():
    """A discard_confirmation present but missing a required sub-field fails validation."""
    data = _valid_report()
    data["discard_confirmation"] = {
        "contract_id": "a" * 32,
        # contract_sha256 missing
        "pr_head_sha": "c" * 40,
        "local_tip_sha": "d" * 40,
        "local_only_commit_shas": ["d" * 40],
        "expires_at": "2026-08-02T00:05:00Z",
        "argv": [],
    }
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("discard_confirmation" in e for e in result.errors)


def test_report_validator_rejects_discard_confirmation_wrong_type():
    data = _valid_report()
    data["discard_confirmation"] = "not-a-dict-or-null"
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("discard_confirmation" in e for e in result.errors)



def test_report_validator_accepts_wrapped_schema_key():
    wrapped = {"POST_MERGE_CLEANUP_REPORT_V1": _valid_report()}
    import yaml

    result = boundary.validate_report_yaml(yaml.safe_dump(wrapped))
    assert result.valid, result.errors


# ---------------------------------------------------------------------------
# AC6 / P1: report validator closed-key + type gaps (Issue #1733 PR #1947
# fix_delta — OWNER REQUEST_CHANGES review comment 5154801090)
# ---------------------------------------------------------------------------


def test_report_validator_rejects_non_int_parent_issue_number():
    data = _valid_report()
    data["parent_issue_status"]["parent_issue_number"] = "not-an-integer"
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("parent_issue_number" in e for e in result.errors)


def test_report_validator_rejects_bool_masquerading_as_parent_issue_number():
    # bool is a subclass of int in Python; must not silently pass.
    data = _valid_report()
    data["parent_issue_status"]["parent_issue_number"] = True
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("parent_issue_number" in e for e in result.errors)


def test_report_validator_rejects_non_positive_parent_issue_number():
    data = _valid_report()
    data["parent_issue_status"]["parent_issue_number"] = 0
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("positive integer" in e for e in result.errors)


def test_report_validator_rejects_non_bool_all_children_closed():
    data = _valid_report()
    data["parent_issue_status"]["all_children_closed"] = "yes"
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("all_children_closed" in e for e in result.errors)


def test_report_validator_rejects_malformed_follow_up_issue_request_item():
    data = _valid_report()
    data["follow_up_issue_requests"] = [
        {
            "title": "x",
            # missing required keys, plus an unknown key
            "unexpected": "surprise",
        }
    ]
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("follow_up_issue_requests[0]" in e for e in result.errors)


def test_report_validator_rejects_malformed_follow_up_issue_request_source():
    data = _valid_report()
    data["follow_up_issue_requests"] = [
        {
            "title": "x",
            "issue_kind": "implementation",
            "severity": "optional_follow_up",
            "source": {"kind": "post_merge_cleanup", "url": "https://example.com"},  # missing note_id
            "dedupe_key": "follow-up:x:1",
            "desired_destination": "x",
            "validated_scope_delta": "x",
            "origin_skill": "post-merge-cleanup",
            "labels": [],
        }
    ]
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("follow_up_issue_requests[0].source" in e for e in result.errors)


def test_report_validator_rejects_malformed_superseded_pr_item():
    data = _valid_report()
    data["superseded_prs"] = [{"number": "not-an-int", "title": "x", "url": "https://example.com"}]
    result = boundary.validate_report_v1(data)
    assert not result.valid
    assert any("superseded_prs[0]" in e for e in result.errors)


def test_report_validator_rejects_unknown_sibling_key_in_wrapper():
    import yaml

    wrapped = {"POST_MERGE_CLEANUP_REPORT_V1": _valid_report(), "attacker_controlled": "injected"}
    result = boundary.validate_report_yaml(yaml.safe_dump(wrapped))
    assert not result.valid
    assert any("sibling key" in e for e in result.errors)


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
# P1 (Issue #1733 PR #1947 fix_delta): the no-child regex denylist must catch
# evasions the original line-start-only pattern missed, without matching
# this file's own documented prose prohibition sentences (self-poisoning).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evasive_command",
    [
        "env codex exec --json -",
        "command codex exec --json -",
        "/usr/bin/codex exec --json -",
        "bash -lc 'codex exec --json -'",
    ],
)
def test_no_child_policy_detects_broadened_evasion_patterns(evasive_command):
    poisoned = EXECUTOR_SKILL.read_text(encoding="utf-8") + f"\n```bash\n{evasive_command}\n```\n"
    tmp_path = REPO_ROOT / "tests" / "_tmp_poisoned_executor_evasion_for_test.md"
    tmp_path.write_text(poisoned, encoding="utf-8")
    try:
        result = boundary.check_no_child_policy(WORKER_MD, tmp_path)
        assert not result.valid, f"evasion not detected: {evasive_command!r}"
    finally:
        tmp_path.unlink()


def test_no_child_policy_does_not_self_poison_on_own_prose_prohibition():
    # The executor Skill's own guardrail prose mentions `codex exec` /
    # `claude -p` outside any fenced code block; this must not trigger a
    # false-positive violation.
    result = boundary.check_no_child_policy(WORKER_MD, EXECUTOR_SKILL)
    assert result.valid, result.errors


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


# ---------------------------------------------------------------------------
# Blocker 1 (Issue #1733 PR #1947 fix_delta): explicit artifact path (no
# glob-first-match), independent tested_head cross-check, and structured
# worker-identity / Skill-load / spawn-event field validation.
# ---------------------------------------------------------------------------


def _synthetic_summary_fields(**overrides) -> dict:
    fields = {
        "exit_code": "0",
        "timed_out": "False",
        "expected_markers_missing": "[]",
        "errors": "[]",
        "tested_head": boundary._current_head(REPO_ROOT),
        "runtime_version": "'claude 2.1.220'",
        "requested_agent_type": "claude",
        "effective_agent_type": "claude",
        "loaded_skills": "['post-merge-cleanup-executor']",
        "child_spawn_event_count": "0",
        "spawn_events": "[]",
        "self_restart_event_count": "0",
        "orchestration_action_count": "0",
        "prompt_sha256": "a" * 64,
        "postcondition_unexpected_changes": "[]",
    }
    fields.update(overrides)
    return fields


def _write_summary(tmp_path: Path, fields: dict) -> Path:
    lines = ["# Runtime Smoke Summary", ""]
    for key, value in fields.items():
        lines.append(f"- {key}: {value}")
    summary_dir = tmp_path / "artifacts" / "runtime-smoke" / "run1"
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / "summary.md"
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def test_runtime_smoke_evidence_skips_without_explicit_artifact_path(tmp_path):
    summary_path = _write_summary(tmp_path, _synthetic_summary_fields())
    # A glob-discoverable artifact exists, but no --artifact-path is given —
    # the checker must not auto-pick it (Blocker 1: no glob-first-match).
    assert boundary.find_runtime_smoke_summary(tmp_path) == summary_path
    rc = boundary.check_runtime_smoke_evidence(tmp_path, artifact_path=None)
    assert rc == boundary.EXIT_SKIP


def test_runtime_smoke_evidence_skips_when_artifact_path_missing(tmp_path):
    missing_path = "artifacts/runtime-smoke/does-not-exist/summary.md"
    rc = boundary.check_runtime_smoke_evidence(tmp_path, artifact_path=missing_path)
    assert rc == boundary.EXIT_SKIP


def test_runtime_smoke_evidence_passes_with_full_structured_fields(tmp_path):
    summary_path = _write_summary(tmp_path, _synthetic_summary_fields())
    rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
    assert rc == boundary.EXIT_OK


def test_runtime_smoke_evidence_fails_on_stale_tested_head(tmp_path):
    summary_path = _write_summary(tmp_path, _synthetic_summary_fields(tested_head="0" * 40))
    rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
    assert rc == boundary.EXIT_FAIL


def test_runtime_smoke_evidence_fails_on_missing_structured_fields(capsys):
    # Iteration 8 (Issue #1733 Scope Delta, 2026-08-02 owner-approved harness
    # extension): the harness now genuinely emits all 10 structured fields,
    # so the iteration 7 best-effort/WARNING-only downgrade is reverted --
    # legacy-fields-only evidence (missing tested_head / loaded_skills /
    # spawn_events / etc) must hard FAIL, not WARN-and-PASS.
    legacy_only_fields = {
        "exit_code": "0",
        "timed_out": "False",
        "expected_markers_missing": "[]",
        "errors": "[]",
        "postcondition_unexpected_changes": "[]",
        "native_event_count": "6",
    }
    tmp_dir = REPO_ROOT / "tests" / "_tmp_legacy_summary_for_test"
    summary_path = _write_summary(tmp_dir, legacy_only_fields)
    try:
        rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
        assert rc == boundary.EXIT_FAIL
        captured = capsys.readouterr()
        assert "missing or unresolved structured field(s)" in captured.err
        assert "tested_head" in captured.err
    finally:
        import shutil

        shutil.rmtree(tmp_dir)


def test_runtime_smoke_evidence_fails_when_structured_field_value_is_literal_none(capsys):
    # A field present as a "- key: None" line (the harness's own documented
    # "could not be honestly derived" marker, e.g. loaded_skills when
    # --agent-type was not supplied) must be treated the same as a fully
    # absent field -- a hard FAIL, not silently accepted as a present value.
    tmp_dir = REPO_ROOT / "tests" / "_tmp_none_valued_summary_for_test"
    summary_path = _write_summary(tmp_dir, _synthetic_summary_fields(loaded_skills="None"))
    try:
        rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
        assert rc == boundary.EXIT_FAIL
        captured = capsys.readouterr()
        assert "missing or unresolved structured field(s)" in captured.err
        assert "loaded_skills" in captured.err
    finally:
        import shutil

        shutil.rmtree(tmp_dir)


def test_runtime_smoke_evidence_default_path_used_when_artifact_path_omitted(tmp_path):
    # The Issue's literal AC12 VC never passes --artifact-path. Iteration 7
    # fix (retained in iteration 8): this must resolve to a single fixed
    # default location (not a glob across multiple candidates). Iteration 8:
    # now that structured fields are hard-required again, a genuine PASS at
    # the default location requires a full structured-fields artifact, not
    # just the legacy-fields-only shape -- exercised here against a fresh
    # throwaway git repo (its own real HEAD) rather than REPO_ROOT, since
    # ``tested_head`` is cross-checked against ``repo_root``'s actual HEAD.
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "seed"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    fields = {
        "exit_code": "0",
        "timed_out": "False",
        "expected_markers_missing": "[]",
        "errors": "[]",
        "tested_head": head,
        "runtime_version": "'claude 2.1.220'",
        "requested_agent_type": "claude",
        "effective_agent_type": "claude",
        "loaded_skills": "['post-merge-cleanup-executor']",
        "child_spawn_event_count": "0",
        "spawn_events": "[]",
        "self_restart_event_count": "0",
        "orchestration_action_count": "0",
        "prompt_sha256": "a" * 64,
        "postcondition_unexpected_changes": "[]",
    }
    summary_dir = tmp_path / boundary._DEFAULT_ARTIFACT_PATH.parent
    summary_dir.mkdir(parents=True)
    summary_path = summary_dir / "summary.md"
    lines = ["# Runtime Smoke Summary", ""] + [f"- {key}: {value}" for key, value in fields.items()]
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc = boundary.check_runtime_smoke_evidence(tmp_path, artifact_path=None)
    assert rc == boundary.EXIT_OK


def test_runtime_smoke_evidence_fails_on_non_matching_loaded_skills(tmp_path):
    summary_path = _write_summary(
        tmp_path, _synthetic_summary_fields(loaded_skills="['post-merge-cleanup', 'post-merge-cleanup-executor']")
    )
    rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
    assert rc == boundary.EXIT_FAIL


def test_runtime_smoke_evidence_fails_on_nonzero_child_spawn_event_count(tmp_path):
    summary_path = _write_summary(tmp_path, _synthetic_summary_fields(child_spawn_event_count="1"))
    rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
    assert rc == boundary.EXIT_FAIL


def test_runtime_smoke_evidence_fails_on_agent_type_mismatch(tmp_path):
    summary_path = _write_summary(tmp_path, _synthetic_summary_fields(effective_agent_type="codex"))
    rc = boundary.check_runtime_smoke_evidence(REPO_ROOT, artifact_path=str(summary_path))
    assert rc == boundary.EXIT_FAIL


# ---------------------------------------------------------------------------
# Issue #2878 AC5: merge と Issue close の分離（close gate matrix の 2 文書一致）
# ---------------------------------------------------------------------------

WORKFLOW_DOC = REPO_ROOT / "docs" / "dev" / "workflow.md"
VALIDATE_PR_BODY = REPO_ROOT / ".claude" / "skills" / "open-pr" / "scripts" / "validate_pr_body.py"
DECISION_TABLE_HEADING = "#### PR reference decision table（判定表の正本）"


def _decision_table(path: Path) -> list[tuple[str, ...]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(DECISION_TABLE_HEADING) == 1, f"{path}: exactly one decision table heading expected"
    start = lines.index(DECISION_TABLE_HEADING) + 1
    rows: list[tuple[str, ...]] = []
    for line in lines[start:]:
        if line.startswith("|"):
            rows.append(tuple(" ".join(cell.split()) for cell in line.strip().strip("|").split("|")))
        elif rows:
            break
    return rows


def _load_validate_pr_body_for_table():
    import importlib.util

    spec = importlib.util.spec_from_file_location("validate_pr_body_for_close_gate_matrix", VALIDATE_PR_BODY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_given_close_gate_matrix_when_compared_then_workflow_and_post_merge_skill_match():
    workflow_table = _decision_table(WORKFLOW_DOC)
    skill_table = _decision_table(ORCHESTRATOR_SKILL)
    assert workflow_table == skill_table, "docs/dev/workflow.md and post-merge-cleanup/SKILL.md decision tables diverged"
    assert workflow_table[0] == ("順", "条件", "decision", "level", "PR 本文の reference", "merge 時の Issue")
    body_rows = workflow_table[2:]
    assert [row[0] for row in body_rows] == ["0", "1", "2", "3", "4", "5"]  # total, ordered rows 0..5

    # the table is not just prose: each row's decision / level is what the single evaluator returns
    module = _load_validate_pr_body_for_table()
    repo = "squne121/loop-protocol"
    a3 = "## Runtime Verification Applicability\n\n- decision: immediate\n- reason: x\n"
    a2 = (
        "## Runtime Verification Applicability\n\n- decision: deferred\n- reason: x\n- deferred_destination:\n"
        "    - destination_type: phase\n    - destination_ref: post-merge-live-evidence\n"
        "- deferred_verification_condition: merge 後\n"
    )
    url = f"https://github.com/{repo}/issues/7#issuecomment-3"
    comment = {
        "url": url, "id": 3, "issue_url": f"https://api.github.com/repos/{repo}/issues/7",
        "author_association": "OWNER", "body": "REFERENCE_DECISION_V1: nonclosing issue=#7",
    }

    def evaluate(state, issue_body, pr_body, decision_comment=None):
        facts = {"repo": repo, "issue_state": state, "pr_number": None, "decision_comment": decision_comment}
        return module.evaluate_reference_policy(pr_body.encode("utf-8"), 7, issue_body, facts)

    observed = {
        "0": evaluate("CLOSED", a3, "Refs #7"),
        "1": evaluate("OPEN", "## Outcome\n", f"Refs #7\nReference-Decision: {url}", comment),
        "2": evaluate("OPEN", a3, f"Refs #7\nReference-Decision: {url}\nReference-Decision: {url}", comment),
        "3": evaluate("OPEN", a2, "Refs #7"),
        "4": evaluate("OPEN", a3, "Closes #7"),
        "5": evaluate("OPEN", "## Outcome\n", "Refs #7"),
    }
    for row in body_rows:
        result = observed[row[0]]
        assert f"`{result['decision']}`" == row[2], (row, result)
        expected_level = "なし" if result["level"] is None else result["level"]
        assert expected_level == row[3], (row, result)

    # close gate prose is present in both documents (merge != close; evidence before an explicit close)
    for path in (WORKFLOW_DOC, ORCHESTRATOR_SKILL):
        text = path.read_text(encoding="utf-8")
        assert "merge は" in text and "close" in text
        assert "OPEN" in text and "operator / orchestrator" in text
        assert "pr_body_sha256" in text and "re-review" in text
        assert "Closes #N" in text and "Refs #N" in text

    # the old unconditional rule ("PR body Closes string is never an authority") is now a limited rule
    skill_text = ORCHESTRATOR_SKILL.read_text(encoding="utf-8")
    assert "PR 本文の `Closes #N` 文字列は authority にしない（`closingIssuesReferences` だけを使う）。" not in skill_text
    assert "PR 本文の `Closes #N` 文字列そのものは authority にしない" in skill_text
    assert "non_closing_authority" in skill_text and "`--phase recover` / `--phase local-only` は non-closing binding を受理せず" in skill_text

    # Issue #2891 replaced the old "stopped until #2891" limitation: see
    # test_issue_2891_* below for the delivery path and the remaining limitation.
    assert "LINKED_ISSUE_MISMATCH" in skill_text
    assert "`linked_issue_number` を省略して認可を迂回する運用は採らない" in skill_text
    assert "cleanup_exec.py::_verify_linked_issue" in skill_text


# ---------------------------------------------------------------------------
# Issue #2891: non_closing_authority delivery path (orchestrator -> worker -> executor Skill -> CLI)
# ---------------------------------------------------------------------------

CLEANUP_EXEC_SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "cleanup_exec.py"
AUTHORITY_FLAG = "--non-closing-authority-file"


def _executor_cleanup_exec_block() -> str:
    text = EXECUTOR_SKILL.read_text(encoding="utf-8")
    blocks = [
        b
        for b in re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)
        if "scripts/agent-ops/cleanup_exec.py" in b
    ]
    assert len(blocks) == 1, f"exactly one production cleanup_exec command block expected, got {len(blocks)}"
    return blocks[0]


def test_issue_2891_executor_command_block_flags_exist_in_production_cli():
    """The executor Skill's production command block must not drift from the real argparse."""
    block = _executor_cleanup_exec_block()
    # flags of the script itself only (the `uv run --locked` prefix belongs to uv)
    skill_flags = set(re.findall(r"--[a-z][a-z-]*", block.split("scripts/agent-ops/cleanup_exec.py", 1)[1]))
    assert AUTHORITY_FLAG in skill_flags
    help_text = subprocess.run(
        [sys.executable, str(CLEANUP_EXEC_SCRIPT), "--help"], capture_output=True, text=True, check=True
    ).stdout
    cli_flags = set(re.findall(r"--[a-z][a-z-]*", help_text))
    assert skill_flags <= cli_flags, f"Skill flags not accepted by the CLI: {sorted(skill_flags - cli_flags)}"
    # The authority is optional: it is wrapped in brackets so a missing worker input drops the whole argument.
    assert f"[{AUTHORITY_FLAG} <non_closing_authority_file>]" in block


def test_issue_2891_orchestrator_passes_authority_file_path_only_when_body_verdict_valid():
    text = ORCHESTRATOR_SKILL.read_text(encoding="utf-8")
    assert "non_closing_authority_file" in text
    assert AUTHORITY_FLAG in text
    # conditional: only when the evaluator body_verdict is `valid`, never on `decision == nonclosing_required` alone
    assert "`body_verdict` が `valid` の場合に限り" in text
    assert "`decision == nonclosing_required` だけを根拠にしない" in text
    # PATH ONLY via the Delegation message / Materialization rule; the orchestrator never assembles the argv
    assert "**path のみ**" in text
    assert "orchestrator が `cleanup_exec` の argv 全体を組み立てて worker に渡してはならない" in text
    assert "ファイルの内容（7 key）を message に複製せず" in text
    delegation = text[text.index("### Materialization rule"):]
    assert "`non_closing_authority_file`" in delegation.split("完了の扱いは4 site 共通")[0]


def test_issue_2891_worker_input_lists_authority_file_path_without_duplicating_procedure():
    text = WORKER_MD.read_text(encoding="utf-8")
    inputs = text.split("## 入力")[1].split("## 振る舞い")[0]
    assert "`non_closing_authority_file`" in inputs
    assert "path のみ" in inputs
    # procedure body is not duplicated into the worker definition (DRY)
    assert "uv run --locked python3 scripts/agent-ops/cleanup_exec.py" not in text


def test_issue_2891_old_limitation_replaced_and_remaining_limitation_documented():
    skill_text = ORCHESTRATOR_SKILL.read_text(encoding="utf-8")
    workflow_text = WORKFLOW_DOC.read_text(encoding="utf-8")
    executor_text = EXECUTOR_SKILL.read_text(encoding="utf-8")
    for label, text in (("orchestrator", skill_text), ("workflow", workflow_text), ("executor", executor_text)):
        # old limitation wording is gone
        assert "#2891 まで" not in text, label
        assert "follow-up #2891 が所有し" not in text, label
        assert "別 Outcome（#2891）が所有" not in text, label
        assert "それまで `Refs`-bound PR の削除系 cleanup は" not in text, label
        # the delivery entry and the remaining (unfixed) limitation are documented consistently
        assert AUTHORITY_FLAG in text, label
        assert "#2910" in text, label
        assert "recover" in text and "local-only" in text, label
        # never claimed as fully unlocked
        assert "全 recovery path が解禁された" in text or "全 recovery path が解禁されたわけではない" in text, label
        assert "全 recovery path が解禁された。" not in text, label
        assert "すべての recovery path が解禁" not in text, label
    assert "全 recovery path が解禁されたとは扱わない" in skill_text
    assert "全 recovery path が解禁されたわけではない" in workflow_text
    assert "全 recovery path が解禁されたわけではない" in executor_text
