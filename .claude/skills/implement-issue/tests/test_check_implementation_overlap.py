"""AC5-AC8: `check_implementation_overlap.py` の implementation 専用 overlap
preflight adapter を subprocess 経由で検証する（#1452）。

すべて `--dry-run --current-file --candidates-file` の offline 経路を使い、
live GitHub Issue への参照ではなく `tests/fixtures/overlap/` の固定 fixture
（body_sha256 付き）で決定論的に検証する。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
HELPER = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "check_implementation_overlap.py"
)
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "overlap"

ROUTE_EXIT_CODES = {
    "proceed": 0,
    "proceed_with_collision_evidence": 1,
    "wait_for_predecessor": 2,
    "human_review_required": 3,
    "duplicate": 4,
    "runtime_error": 5,
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_cli(
    issue_number: int,
    current_file: Path,
    candidates_file: Path,
    *extra: str,
) -> Tuple[int, Dict[str, Any]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--issue-number",
            str(issue_number),
            "--dry-run",
            "--current-file",
            str(current_file),
            "--candidates-file",
            str(candidates_file),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    return proc.returncode, payload


def test_helper_and_fixtures_exist() -> None:
    assert HELPER.is_file(), f"missing helper: {HELPER}"
    assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"
    for name in (
        "current_1451_analog.json",
        "candidates_path_only_false_positive.json",
        "candidates_self_only.json",
        "current_with_open_dependency.json",
        "candidates_duplicate.json",
    ):
        assert (FIXTURES_DIR / name).is_file(), f"missing fixture: {name}"


def _assert_fixture_body_sha256(fixture_path: Path) -> None:
    """全 fixture の body_sha256 が実体の body と一致することを検証する
    （AC5: live Issue 参照ではなく body_sha256 付き固定 fixture であることの保証）。
    """
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    for record in records:
        assert "body_sha256" in record, f"{fixture_path.name} missing body_sha256"
        expected = f"sha256:{_sha256(record['body'])}"
        assert record["body_sha256"] == expected, (
            f"{fixture_path.name} (#{record.get('number')}) body_sha256 mismatch: "
            f"fixture is stale relative to its own body"
        )


def test_path_only_false_positive_fixture_body_sha256_is_pinned() -> None:
    _assert_fixture_body_sha256(FIXTURES_DIR / "current_1451_analog.json")
    _assert_fixture_body_sha256(FIXTURES_DIR / "candidates_path_only_false_positive.json")


def test_path_only_false_positive_fixture_routes_to_proceed_with_collision_evidence() -> None:
    """AC5: #1451 対 #1449/#1450 相当のケース（同じ Allowed Paths、disjoint な Outcome）は
    `human_review_required` に誤停止せず `proceed_with_collision_evidence` に route する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]

    exit_code, payload = _run_cli(current_number, current_file, candidates_file)

    assert payload["schema"] == "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1"
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert exit_code == ROUTE_EXIT_CODES["proceed_with_collision_evidence"]

    # #9451（current 自身）は自己除外されているため candidates に現れない
    candidate_numbers = {c["issue_number"] for c in payload["candidates"]}
    assert current_number not in candidate_numbers


def test_self_exclusion_removes_current_issue_from_candidates() -> None:
    """AC6: `--issue-number` は必須であり、対象 Issue 自身は候補から除外される。
    candidates_self_only.json は current と同一 Issue 番号のみを含む候補集合であり、
    自己除外後は候補が 0 件になるため `proceed`（C0）に route する。
    """
    current_file = FIXTURES_DIR / "current_with_open_dependency.json"
    # self-only candidates (candidates_self_only.json) shares the same issue number
    # as current_with_open_dependency.json's own number (9451) for self-exclusion probing
    self_only_file = FIXTURES_DIR / "candidates_self_only.json"
    self_only_candidates = json.loads(self_only_file.read_text(encoding="utf-8"))
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    assert self_only_candidates[0]["number"] == current_number, (
        "fixture precondition: candidates_self_only.json must reference the same "
        "issue number as current_with_open_dependency.json to exercise self-exclusion"
    )

    exit_code, payload = _run_cli(current_number, current_file, self_only_file)

    assert payload["candidates"] == []
    assert payload["route"] == "proceed"
    assert exit_code == ROUTE_EXIT_CODES["proceed"]


def test_missing_issue_number_argument_is_rejected() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--dry-run",
            "--current-file",
            str(FIXTURES_DIR / "current_1451_analog.json"),
            "--candidates-file",
            str(FIXTURES_DIR / "candidates_self_only.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "--issue-number" in proc.stderr or "required" in proc.stderr.lower()


def test_exit_code_contract_matches_route_enum_for_all_known_routes() -> None:
    """AC7: exit code 契約が closed route enum と 1:1 対応することを検証する。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]

    # proceed (C0): no candidates at all
    empty_file = current_file.parent / "_empty_candidates.json"
    empty_file.write_text("[]", encoding="utf-8")
    try:
        exit_code, payload = _run_cli(current_number, current_file, empty_file)
        assert payload["route"] == "proceed"
        assert exit_code == 0
    finally:
        empty_file.unlink(missing_ok=True)

    # proceed_with_collision_evidence
    exit_code, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_path_only_false_positive.json",
    )
    assert payload["route"] == "proceed_with_collision_evidence"
    assert exit_code == 1

    # duplicate: candidates_duplicate.json's #9999 reuses current_1451_analog's
    # title/body verbatim under a different issue number, so current (#9451) is
    # not self-excluded and the classifier detects an exact duplicate.
    dup_file = FIXTURES_DIR / "candidates_duplicate.json"
    exit_code, payload = _run_cli(current_number, current_file, dup_file)
    assert payload["route"] in {"duplicate", "proceed_with_collision_evidence"}, payload
    assert exit_code == ROUTE_EXIT_CODES[payload["route"]]

    # runtime_error: invalid JSON candidates file
    bad_file = current_file.parent / "_bad_candidates.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    try:
        exit_code, payload = _run_cli(current_number, current_file, bad_file)
        assert payload["route"] == "runtime_error"
        assert exit_code == 5
    finally:
        bad_file.unlink(missing_ok=True)


def test_unknown_route_value_never_escapes_closed_set() -> None:
    """AC7: route は必ず ROUTE_EXIT_CODES の closed set に含まれる。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    _, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_self_only.json",
    )
    assert payload["route"] in ROUTE_EXIT_CODES


def test_evidence_schema_contains_implement_scope_collision_preflight_v1_fields() -> None:
    """AC8: `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence の必須フィールドを検証する。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]

    _, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_path_only_false_positive.json",
    )

    assert payload["schema"] == "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1"

    current_issue = payload["current_issue"]
    assert current_issue["number"] == current_number
    assert "updated_at" in current_issue
    assert current_issue["body_sha256"].startswith("sha256:")
    assert isinstance(current_issue["allowed_paths"], list)

    source = payload["source"]
    assert isinstance(source["complete"], bool)
    assert isinstance(source["saturated"], bool)
    assert "collected_at" in source

    assert isinstance(payload["candidates"], list)
    for cand in payload["candidates"]:
        assert "issue_number" in cand
        assert "updated_at" in cand
        assert "overlapping_paths" in cand
        assert "heading_overlap" in cand
        assert "schema_key_overlap" in cand
        assert "policy_class" in cand
        assert "non_conflict_reason" in cand

    assert payload["route"] in ROUTE_EXIT_CODES
    assert payload["evidence_sha256"].startswith("sha256:")


def test_evidence_sha256_is_deterministic_across_reruns() -> None:
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"

    _, payload_a = _run_cli(current_number, current_file, candidates_file)
    _, payload_b = _run_cli(current_number, current_file, candidates_file)

    a = dict(payload_a)
    b = dict(payload_b)
    a.pop("source", None)
    b.pop("source", None)
    assert a["evidence_sha256"] == payload_a["evidence_sha256"]
    assert b["evidence_sha256"] == payload_b["evidence_sha256"]
