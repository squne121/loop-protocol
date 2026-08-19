#!/usr/bin/env python3
"""
Unit / CLI integration tests for `vc_duplicate_diagnostic_report/v1`
(Issue #2232): duplicate non-pure VC command diagnostics in
`baseline_vc_preflight.py`, independent of the canonical execution plan
(`canonical_vc_plan/v2`).
"""

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent.parent / "baseline_vc_preflight.py"
sys.path.insert(0, str(_SCRIPT_PATH.parent))

import baseline_vc_preflight as bvp  # noqa: E402


def _vc_body(section: str) -> str:
    return f"## Verification Commands\n\n{section}\n"


def _run_cli(body_file: Path, cwd: Path, extra_args=None) -> tuple[dict, int]:
    args = [
        sys.executable,
        str(_SCRIPT_PATH),
        "--body-file",
        str(body_file),
        "--issue",
        "999",
        "--cwd",
        str(cwd),
    ]
    if extra_args:
        args.extend(extra_args)
    result = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    return json.loads(result.stdout), result.returncode


# ---------------------------------------------------------------------------
# AC2: classification_convergence
# ---------------------------------------------------------------------------


def test_classification_convergence_directory_rg_within_allowed_path_is_pure(tmp_path):
    """GIVEN rg targeting a directory within Allowed Paths WHEN classified with
    real cwd/allowed_paths THEN it is pure in both the canonical plan and the
    diagnostic report (positive control)."""
    target_dir = tmp_path / "fixture_dir"
    target_dir.mkdir()
    (target_dir / "a.txt").write_text("pattern\n", encoding="utf-8")
    command = "rg -q pattern fixture_dir"

    assert bvp._is_parallel_eligible_command(command, str(tmp_path), ["fixture_dir"]) is True

    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        f"$ {command}\n"
        "# AC2\n"
        f"$ {command}\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body, cwd=str(tmp_path), allowed_paths=["fixture_dir"])
    assert all(occ["is_pure"] for occ in plan["command_occurrences"])

    report = bvp.compute_duplicate_diagnostic_report(
        body, plan["plan_digest"], str(tmp_path), ["fixture_dir"]
    )
    assert report["items"] == []


def test_classification_convergence_directory_rg_without_allowed_paths_is_non_pure(tmp_path):
    """GIVEN rg targeting a directory but with NO Allowed Paths recorded
    (empty allowed_paths) THEN the directory-recursion containment check
    cannot be satisfied, so the command is non-pure and duplicate
    occurrences ARE flagged (negative control; same directory target as the
    positive control above, but classified with a different, real
    classification_context)."""
    target_dir = tmp_path / "fixture_dir"
    target_dir.mkdir()
    (target_dir / "a.txt").write_text("pattern\n", encoding="utf-8")
    command = "rg -q pattern fixture_dir"

    assert bvp._is_parallel_eligible_command(command, str(tmp_path), []) is False

    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        f"$ {command}\n"
        "# AC2\n"
        f"$ {command}\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body, cwd=str(tmp_path), allowed_paths=[])
    assert all(not occ["is_pure"] for occ in plan["command_occurrences"])

    report = bvp.compute_duplicate_diagnostic_report(
        body, plan["plan_digest"], str(tmp_path), []
    )
    assert len(report["items"]) == 1
    assert report["items"][0]["occurrence_count"] == 2


def test_classification_convergence_planner_and_executor_use_same_cwd_allowed_paths(tmp_path):
    """AC2: `compute_canonical_vc_plan()`'s planner-path `is_pure` classification
    converges with the executor-path `_is_parallel_eligible_command()` call
    when both are given the SAME cwd/allowed_paths."""
    target_dir = tmp_path / "fixture_dir"
    target_dir.mkdir()
    command = "rg -q nomatch fixture_dir"

    body = _vc_body(f"```bash\n$ {command}\n```\n")
    plan = bvp.compute_canonical_vc_plan(body, cwd=str(tmp_path), allowed_paths=["fixture_dir"])
    planner_is_pure = plan["command_occurrences"][0]["is_pure"]

    executor_is_pure = bvp._is_parallel_eligible_command(command, str(tmp_path), ["fixture_dir"])
    assert planner_is_pure == executor_is_pure is True


# ---------------------------------------------------------------------------
# AC3: report_status
# ---------------------------------------------------------------------------


def test_report_status_complete_when_body_and_plan_parsed(tmp_path):
    body = _vc_body("```bash\n# AC1\n$ pnpm lint\n```\n")
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert report["status"] == "complete"
    assert report["schema"] == "vc_duplicate_diagnostic_report/v1"


def test_report_status_not_computed_sentinel():
    report = bvp.not_computed_diagnostic_report()
    assert report["status"] == "not_computed"
    assert report["items"] == []
    assert report["schema"] == "vc_duplicate_diagnostic_report/v1"


def test_report_status_not_computed_distinguishes_from_empty_complete():
    """AC3: `not_computed` and `complete` + empty `items` are distinct states."""
    body = _vc_body("```bash\n# AC1\n$ pnpm lint\n```\n")
    plan = bvp.compute_canonical_vc_plan(body)
    complete_report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    not_computed_report = bvp.not_computed_diagnostic_report()
    assert complete_report["status"] == "complete"
    assert complete_report["items"] == []
    assert not_computed_report["status"] == "not_computed"
    assert complete_report != not_computed_report


# ---------------------------------------------------------------------------
# AC4: diagnostic_schema
# ---------------------------------------------------------------------------


def test_diagnostic_schema_item_has_required_fields():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert len(report["items"]) == 1
    item = report["items"][0]
    required_keys = {"rule_id", "level", "message", "command_identity_hash", "occurrence_count", "occurrences"}
    assert required_keys <= set(item.keys())
    assert item["occurrence_count"] == 2
    assert len(item["occurrences"]) == 2
    for occ in item["occurrences"]:
        assert {"ordinal", "ac_label", "block_index", "line_in_block"} <= set(occ.keys())
    assert item["occurrences"][0]["ac_label"] == "AC1"
    assert item["occurrences"][1]["ac_label"] == "AC2"
    assert item["occurrences"][0]["ordinal"] < item["occurrences"][1]["ordinal"]


def test_diagnostic_schema_command_identity_hash_is_sha256_of_command_text():
    import hashlib

    body = _vc_body("```bash\n# AC1\n$ pnpm lint\n# AC2\n$ pnpm lint\n```\n")
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    expected_hash = hashlib.sha256("pnpm lint".encode("utf-8")).hexdigest()
    assert report["items"][0]["command_identity_hash"] == expected_hash


# ---------------------------------------------------------------------------
# AC5: determinism
# ---------------------------------------------------------------------------


def test_determinism_repeated_calls_are_byte_identical():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "$ pnpm build\n"
        "# AC3\n"
        "$ pnpm lint\n"
        "# AC4\n"
        "$ pnpm build\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report_a = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    report_b = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert json.dumps(report_a, sort_keys=True) == json.dumps(report_b, sort_keys=True)


def test_determinism_ordered_by_first_occurrence_ordinal_then_hash():
    """AC5: items ordered by first occurrence ordinal; `pnpm build` appears
    first (ordinal 1) and `pnpm lint` second (ordinal 2)."""
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ pnpm build\n"
        "# AC2\n"
        "$ pnpm lint\n"
        "# AC3\n"
        "$ pnpm build\n"
        "# AC4\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert len(report["items"]) == 2
    assert report["items"][0]["occurrences"][0]["ac_label"] == "AC1"  # pnpm build first
    assert report["items"][1]["occurrences"][0]["ac_label"] == "AC2"  # pnpm lint second


# ---------------------------------------------------------------------------
# AC6: scope_semantics
# ---------------------------------------------------------------------------


def test_scope_semantics_pr_review_only_marker_is_included():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "# preflight-scope: pr_review_only\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "# preflight-scope: pr_review_only\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert len(report["items"]) == 1


def test_scope_semantics_runtime_only_marker_is_included():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "# preflight-scope: runtime_only\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "# preflight-scope: runtime_only\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert len(report["items"]) == 1


def test_scope_semantics_baseline_expect_deferred_is_excluded():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "# baseline-expect: deferred\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "# baseline-expect: deferred\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert report["items"] == []


def test_scope_semantics_static_blocked_command_is_excluded():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ echo $(date)\n"
        "# AC2\n"
        "$ echo $(date)\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert report["items"] == []


def test_scope_semantics_state_changing_command_interposed_still_included():
    """A different non-pure (state-changing) command interposed between two
    occurrences of the SAME non-pure command does not suppress the
    diagnostic -- this report is a body-text-level signal, independent of
    the canonical plan's state_epoch barrier bookkeeping."""
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ pnpm lint\n"
        "# AC2\n"
        "$ pnpm build\n"
        "# AC3\n"
        "$ pnpm lint\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    lint_items = [i for i in report["items"] if i["occurrence_count"] == 2]
    assert len(lint_items) == 1


def test_scope_semantics_pure_command_duplicate_is_never_flagged():
    body = _vc_body(
        "```bash\n"
        "# AC1\n"
        "$ test -f /etc/passwd\n"
        "# AC2\n"
        "$ test -f /etc/passwd\n"
        "```\n"
    )
    plan = bvp.compute_canonical_vc_plan(body)
    report = bvp.compute_duplicate_diagnostic_report(body, plan["plan_digest"], ".", [])
    assert report["items"] == []


# ---------------------------------------------------------------------------
# AC7 compatibility sanity check: diagnostic_report never influences the
# canonical plan's own fields (independent computation).
# ---------------------------------------------------------------------------


def test_diagnostic_report_does_not_influence_canonical_plan_fields():
    body = _vc_body("```bash\n# AC1\n$ pnpm lint\n# AC2\n$ pnpm lint\n```\n")
    plan_before = bvp.compute_canonical_vc_plan(body)
    bvp.compute_duplicate_diagnostic_report(body, plan_before["plan_digest"], ".", [])
    plan_after = bvp.compute_canonical_vc_plan(body)
    assert plan_before == plan_after


# ---------------------------------------------------------------------------
# AC8: cli_integration
# ---------------------------------------------------------------------------


def test_cli_integration_success_no_duplicates(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(_vc_body("```bash\n# AC1\n$ test -f /etc/passwd\n```\n"), encoding="utf-8")
    payload, _rc = _run_cli(body_file, tmp_path)
    assert payload["diagnostic_report"]["status"] == "complete"
    assert payload["diagnostic_report"]["items"] == []


def test_cli_integration_success_with_duplicates(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(
        _vc_body("```bash\n# AC1\n$ pnpm lint\n# AC2\n$ pnpm lint\n```\n"), encoding="utf-8"
    )
    payload, _rc = _run_cli(body_file, tmp_path)
    assert payload["diagnostic_report"]["status"] == "complete"
    assert len(payload["diagnostic_report"]["items"]) == 1
    assert payload["diagnostic_report"]["items"][0]["occurrence_count"] == 2


def test_cli_integration_source_retrieval_failure(tmp_path):
    payload, rc = _run_cli(tmp_path / "does-not-exist.md", tmp_path)
    assert rc == 2
    assert payload["status"] == "blocked"
    assert payload["diagnostic_report"]["status"] == "not_computed"


def test_cli_integration_current_head_validation_failure(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(_vc_body("```bash\n# AC1\n$ pnpm lint\n```\n"), encoding="utf-8")
    payload, rc = _run_cli(
        body_file,
        tmp_path,
        extra_args=["--evidence-mode", "current-head", "--reviewed-head-sha", "not-a-real-sha"],
    )
    assert rc != 0
    assert payload["status"] == "blocked"
    assert payload["diagnostic_report"]["status"] == "not_computed"


def test_cli_integration_parse_failure_no_verification_commands_section(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text("## Something Else\n\nno VC section here\n", encoding="utf-8")
    payload, rc = _run_cli(body_file, tmp_path)
    assert rc == 2
    assert payload["status"] == "blocked"
    assert payload["diagnostic_report"]["status"] == "not_computed"


def test_cli_integration_parse_failure_no_commands_extracted(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(_vc_body("```bash\n# only a comment, no command\n```\n"), encoding="utf-8")
    payload, rc = _run_cli(body_file, tmp_path)
    assert rc == 2
    assert payload["status"] == "blocked"
    assert payload["diagnostic_report"]["status"] == "not_computed"


def test_cli_integration_policy_rejection_plan_digest_mismatch(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(_vc_body("```bash\n# AC1\n$ pnpm lint\n```\n"), encoding="utf-8")
    payload, rc = _run_cli(
        body_file,
        tmp_path,
        extra_args=["--expected-plan-digest", "sha256:" + "0" * 64],
    )
    assert rc != 0
    assert payload["status"] == "blocked"
    assert payload["diagnostic_report"]["status"] == "not_computed"
