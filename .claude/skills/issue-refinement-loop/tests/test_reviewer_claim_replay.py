from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "reviewer_claim_replay.py"
sys.path.insert(0, str(SCRIPTS_DIR))

from reviewer_claim_replay import SCHEMA, analyze  # noqa: E402


READINESS_LP001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP001",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 1,
            "line_end": 1,
        }
    ],
}

READINESS_LP010 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP010",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 5,
            "line_end": 5,
        }
    ],
}

READINESS_LP005 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "LP005",
            "source_check": "validate_issue_body",
            "category": "body_lint",
            "line_start": 3,
            "line_end": 3,
        }
    ],
}

READINESS_VCS001 = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [
        {
            "rule_id": "VCS001",
            "source_check": "contract_readiness_check",
            "category": "compound_command_disallowed",
            "line_start": 10,
            "line_end": 10,
        }
    ],
}

READINESS_CLEAN = {
    "schema": "ISSUE_CONTRACT_READINESS_RESULT_V1",
    "body_sha256": "sha256:body-a",
    "errors": [],
}

COMPACT_C4 = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "blocking_issues": [{"code": "C4", "message": "missing $ prefix"}],
    "structured_blockers": [],
}

COMPACT_MISSING_SECTION = {
    "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
    "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
    "blocking_issues": [{"code": "missing_section", "message": "missing section"}],
    "structured_blockers": [],
}


def test_c4_with_lp001_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["schema"] == SCHEMA
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_c4_with_lp010_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_missing_section_with_real_lp001_is_backed():
    result, _ = analyze(
        review_result=COMPACT_MISSING_SECTION,
        readiness_result=READINESS_LP001,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    blocker = result["blockers"][0]
    assert blocker["normalized_kind"] == "missing_section"
    assert blocker["evidence"][0]["rule_id"] == "LP001"


def test_lp010_requires_exact_lp010_match():
    review = {
        "schema": "ISSUE_REVIEW_RESULT_COMPACT_V1",
        "issue_url": "https://github.com/squne121/loop-protocol/issues/1021",
        "blocking_issues": [{"code": "LP010", "message": "ac/vc mismatch"}],
        "structured_blockers": [],
    }
    result, _ = analyze(
        review_result=review,
        readiness_result=READINESS_LP010,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["blockers"][0]["evidence"][0]["rule_id"] == "LP010"


def test_missing_section_with_lp005_only_is_unbacked():
    result, _ = analyze(
        review_result=COMPACT_MISSING_SECTION,
        readiness_result=READINESS_LP005,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state={},
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert result["should_consume_iteration"] is False


def test_second_unbacked_same_body_becomes_false_positive():
    previous = {
        "schema": "REVIEWER_CLAIM_REPLAY_STATE_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:body-a",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 1,
        "last_review_artifact": "/tmp/prior.json",
    }
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=previous,
    )
    assert result["verdict"] == "reviewer_false_positive_suspected"
    assert result["routing"] == "human_escalation"
    assert next_state["consecutive_unbacked_count"] == 2


def test_body_hash_change_resets_consecutive_count():
    previous = {
        "schema": "REVIEWER_CLAIM_REPLAY_STATE_V1",
        "issue_url": COMPACT_C4["issue_url"],
        "body_sha256": "sha256:old",
        "reviewer_blocker_code": "C4",
        "normalized_kind": "vc_command_format",
        "consecutive_unbacked_count": 3,
        "last_review_artifact": "/tmp/prior.json",
    }
    result, next_state = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=None,
        previous_state=previous,
    )
    assert result["verdict"] == "reviewer_claim_unbacked_by_deterministic_checker"
    assert next_state["consecutive_unbacked_count"] == 1


def test_vc_preflight_category_backs_c4():
    preflight = {
        "schema": "baseline_vc_preflight/v1",
        "results": [{"category": "compound_command_disallowed", "line_start": 10, "line_end": 10}],
    }
    result, _ = analyze(
        review_result=COMPACT_C4,
        readiness_result=READINESS_CLEAN,
        vc_syntax_result=None,
        vc_preflight_result=preflight,
        previous_state={},
    )
    assert result["verdict"] == "deterministic_fail_confirmed"
    assert result["blockers"][0]["evidence"][0]["source_check"] == "baseline_vc_preflight"


def test_cli_bad_optional_json_fails_closed(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    vc_syntax_path = tmp_path / "vc_syntax.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")
    vc_syntax_path.write_text("{bad", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--vc-syntax-result-file",
            str(vc_syntax_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["verdict"] == "input_or_runtime_error"


def test_cli_writes_and_reuses_state_file(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    state_path = tmp_path / "state.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_CLEAN), encoding="utf-8")

    first = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--state-file",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    second = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
            "--state-file",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(second.stdout)["verdict"] == "reviewer_false_positive_suspected"
    assert json.loads(state_path.read_text(encoding="utf-8"))["consecutive_unbacked_count"] == 2


def test_cli_stdout_is_compact_json(tmp_path: Path):
    review_path = tmp_path / "review.json"
    readiness_path = tmp_path / "readiness.json"
    review_path.write_text(json.dumps(COMPACT_C4), encoding="utf-8")
    readiness_path.write_text(json.dumps(READINESS_VCS001), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--review-result-file",
            str(review_path),
            "--readiness-result-file",
            str(readiness_path),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "\n" not in proc.stdout.strip()
    assert len(proc.stdout.encode("utf-8")) <= 2048
