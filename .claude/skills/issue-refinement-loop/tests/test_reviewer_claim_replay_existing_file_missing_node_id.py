"""Issue #1549 production-chain coverage for an existing pytest file/node miss."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / ".claude/skills/issue-refinement-loop/scripts"
CONTRACT_SCRIPTS = REPO_ROOT / ".claude/skills/issue-contract-review/scripts"
BASELINE = CONTRACT_SCRIPTS / "baseline_vc_preflight.py"
READINESS = CONTRACT_SCRIPTS / "contract_readiness_check.py"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(CONTRACT_SCRIPTS))

from parent_replay_binding import build_parent_replay_binding, validate_binding_artifact  # noqa: E402
from reviewer_claim_replay import (  # noqa: E402
    REVIEWER_CHECKER_TAXONOMY_V1,
    analyze,
    dump_taxonomy,
    normalize_taxonomy_key,
    resolve_readiness_error_to_taxonomy,
    taxonomy_invariant_violations,
)

BODY = (
    "## Acceptance Criteria\n"
    "- [ ] AC1: classifier proof\n\n"
    "## Verification Commands\n"
    "```bash\n"
    "# AC1\n"
    "$ uv run --locked pytest .claude/skills/issue-refinement-loop/tests/"
    "test_reviewer_claim_replay.py::test_missing_1549_node -q\n"
    "```\n"
)
BODY_SHA = "sha256:" + hashlib.sha256(BODY.encode()).hexdigest()
CODE = "VCP_EXISTING_FILE_MISSIN"
CATEGORY = "existing_file_missing_node_id_noncanonical"


def _run_json(command: list[str], *, cwd: Path = REPO_ROOT) -> tuple[dict, int]:
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)
    assert proc.stdout, proc.stderr
    return json.loads(proc.stdout), proc.returncode


def _real_preflight(tmp_path: Path) -> dict:
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    result, returncode = _run_json(
        [sys.executable, str(BASELINE), "--body-file", str(body_file), "--cwd", str(REPO_ROOT)]
    )
    assert returncode == 1
    return result


def _real_readiness(tmp_path: Path, preflight: dict) -> dict:
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    result, returncode = _run_json(
        [
            sys.executable,
            str(READINESS),
            "--body-file",
            str(body_file),
            "--mode",
            "execute",
        ]
    )
    assert returncode == 1
    return result


def _review(body_sha: str = BODY_SHA, code: str = CODE) -> dict:
    return {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1549",
        "body_sha256": body_sha,
        "structured_blockers": [{"reviewer_blocker_code": code, "message": "pytest node missing"}],
    }


def _claim(body_sha: str = BODY_SHA, code: str = CODE) -> dict:
    return {
        "schema": "REVIEWER_BLOCKER_CLAIM_V1",
        "body_sha256": body_sha,
        "blockers": [{"reviewer_blocker_code": code, "message": None, "line_start": 1, "line_end": 1}],
    }


def _error() -> dict:
    return {
        "rule_id": CODE,
        "source_check": "baseline_vc_preflight",
        "category": CATEGORY,
        "source_payload": {
            "classification": "blocked",
            "category": CATEGORY,
            "decision": "blocked",
            "scope_class": "baseline_fail_expected",
        },
    }


def _readiness(errors: list[dict], body_sha: str = BODY_SHA) -> dict:
    return {"schema": "ISSUE_CONTRACT_READINESS_RESULT_V1", "body_sha256": body_sha, "errors": errors}


def test_taxonomy_ssot_entry():
    entries = {entry["entry_id"]: entry for entry in REVIEWER_CHECKER_TAXONOMY_V1}
    entry = entries[CATEGORY]
    assert entry["reviewer_codes"] == ["vcp_existing_file_missin", CODE, CATEGORY]
    assert entry["readiness_rule_ids"] == [CODE]
    assert entry["readiness_categories"] == [CATEGORY]
    assert entry["readiness_category_source_check"] == "baseline_vc_preflight"
    assert entry["producer_shape_policy"] == "blocked_baseline_v1"
    assert entry["entry_id"] == CATEGORY == entry["domain_keys"][0]
    assert taxonomy_invariant_violations() == []


def test_canonical_normalization():
    for value in ("vcp_existing_file_missin", CODE, "Vcp_Existing_File_Missin", CATEGORY):
        assert normalize_taxonomy_key(value) == CATEGORY
    for prose in ("pytest node missing", "existing file missing node", "prefix VCP_EXISTING_FILE_MISSIN suffix"):
        assert normalize_taxonomy_key(prose) is None


def test_real_baseline_classifier_existing_file_missing_node_id(tmp_path: Path):
    preflight = _real_preflight(tmp_path)
    item = preflight["results"][0]
    assert (item["classification"], item["category"], item["decision"], item["scope_class"]) == (
        "blocked", CATEGORY, "blocked", "baseline_fail_expected"
    )
    assert item["exit_code"] == 4
    assert "no match in any of" in "\n".join(item["stderr_head"]).lower()


def test_readiness_producer_preserves_source_and_sha(tmp_path: Path):
    preflight = _real_preflight(tmp_path)
    readiness = _real_readiness(tmp_path, preflight)
    error = next(error for error in readiness["errors"] if error["category"] == CATEGORY)
    assert readiness["body_sha256"] == BODY_SHA
    assert (error["rule_id"], error["category"], error["source_check"]) == (
        CODE,
        CATEGORY,
        "baseline_vc_preflight",
    )
    assert error["source_payload"]["classification"] == "blocked"
    assert resolve_readiness_error_to_taxonomy(error) == {
        "status": "resolved",
        "entry_id": CATEGORY,
        "deterministic_domain_key": CATEGORY,
    }


def test_full_cli_chain_parent_replay(tmp_path: Path):
    preflight = _real_preflight(tmp_path)
    readiness = _real_readiness(tmp_path, preflight)
    artifact = build_parent_replay_binding(
        reviewer_blocker_claim=_claim(),
        readiness_result=readiness,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
        current_body_bytes=BODY.encode(),
        issue_url="https://github.com/squne121/loop-protocol/issues/1549",
        repository_full_name="squne121/loop-protocol", issue_number=1549,
        refinement_session_id="issue-1549-test", iteration_id="1",
    )
    validate_binding_artifact(artifact)
    result = artifact["replay_result"]
    assert (result["verdict_detail_v1"], result["routing"], result["should_consume_iteration"]) == (
        "deterministic_fail_confirmed", "proceed_to_rewrite", True
    )
    assert result["blockers"][0]["deterministic_backed"] is True
    assert result["blockers"][0]["checker_gap"] is False


def test_strict_negative_matrix_fail_closed():
    cases = [
        {"rule_id": "OTHER"},
        {"category": "other"},
        {"source_check": "other"},
        {
            "source_payload": {
                "classification": "expected_fail",
                "category": CATEGORY,
                "decision": "blocked",
                "scope_class": "baseline_fail_expected",
            }
        },
        {
            "source_payload": {
                "classification": "blocked",
                "category": CATEGORY,
                "decision": "go",
                "scope_class": "baseline_fail_expected",
            }
        },
        {
            "source_payload": {
                "classification": "blocked",
                "category": CATEGORY,
                "decision": "blocked",
                "scope_class": "wrong",
            }
        },
    ]
    for override in cases:
        err = _error()
        err.update(override)
        result, _ = analyze(
            review_result=_review(),
            readiness_result=_readiness([err]),
            vc_syntax_result=None,
            vc_preflight_result=None,
            previous_state={},
        )
        assert result["should_consume_iteration"] is False
        assert result["blockers"][0]["deterministic_backed"] is False
    conflict = _error()
    conflict["category"] = "broad_search_path_unbounded"
    assert resolve_readiness_error_to_taxonomy(conflict)["reason_code"] == "readiness_taxonomy_conflict"


def test_fail_closed_state_contract():
    result, state = analyze(
        review_result=_review(body_sha="sha256:stale"), readiness_result=_readiness([], BODY_SHA),
        vc_syntax_result=None, vc_preflight_result=None,
        previous_state={"consecutive_unbacked_count": 7},
    )
    assert (result["verdict_detail_v1"], result["routing"], result["should_consume_iteration"]) == (
        "input_or_runtime_error", "human_judgment_required", False
    )
    assert state["consecutive_unbacked_count"] == 7


def test_natural_language_never_normalizes():
    result, _ = analyze(
        review_result=_review(code="existing file has a missing pytest node id"),
        readiness_result=_readiness([_error()]),
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["blockers"][0]["normalized_kind"] == "unknown_blocker_type"
    assert result["blockers"][0]["checker_gap"] is True


def test_multiple_blockers_and_taxonomy_dump():
    result, state = analyze(
        review_result={**_review(), "structured_blockers": [
            {"reviewer_blocker_code": "unknown prose", "message": None},
            {"reviewer_blocker_code": CODE, "message": None},
        ]},
        readiness_result=_readiness([_error()]),
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["routing"] == "proceed_to_rewrite"
    assert state["consecutive_unbacked_count"] == 0
    assert dump_taxonomy()["entries"] == REVIEWER_CHECKER_TAXONOMY_V1


def test_downstream_ownership_contract():
    entry = next(
        entry for entry in REVIEWER_CHECKER_TAXONOMY_V1 if entry["entry_id"] == CATEGORY
    )
    assert entry["entry_id"] == CATEGORY
    assert entry["domain_keys"] == [CATEGORY]
    assert not any(key.startswith("VCS") or key == "RVA001" for key in entry["readiness_rule_ids"])
