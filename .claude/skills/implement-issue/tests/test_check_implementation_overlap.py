"""AC5-AC8: `check_implementation_overlap.py` の implementation 専用 overlap
preflight adapter を subprocess 経由で検証する（#1452、PR #1455 レビュー修正版）。

すべて `--dry-run --current-file --candidates-file` の offline 経路を使い、
live GitHub Issue への参照ではなく `tests/fixtures/overlap/` の固定 fixture
（body_sha256 付き）で決定論的に検証する。

exit code 契約（Major 2）: 分類に成功した場合は route を問わず exit 0。
`runtime_error` のみ非 0（exit 1）。route の正本は常に JSON 出力の
`route` フィールドである。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

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

ROUTES = {
    "proceed",
    "proceed_with_collision_evidence",
    "wait_for_predecessor",
    "human_review_required",
    "duplicate",
    "runtime_error",
}

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1


_SPEC = importlib.util.spec_from_file_location("overlap_checker", HELPER)
assert _SPEC and _SPEC.loader
checker_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = checker_module
_SPEC.loader.exec_module(checker_module)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DEFAULT_REPO = "squne121/loop-protocol"


def _run_cli(
    issue_number: int,
    current_file: Path,
    candidates_file: Path,
    *extra: str,
) -> Tuple[int, Dict[str, Any]]:
    # AC1/AC10 (#1462): dry-run も --repo が必須になったため、既存テストヘルパー
    # に既定の --repo を追加する（後方互換維持のための Scope Delta 内変更）。
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
            "--repo",
            DEFAULT_REPO,
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    payload = json.loads(proc.stdout)
    return proc.returncode, payload


def _number(fixture_name: str) -> int:
    return json.loads((FIXTURES_DIR / fixture_name).read_text(encoding="utf-8"))["number"]


def test_helper_and_fixtures_exist() -> None:
    assert HELPER.is_file(), f"missing helper: {HELPER}"
    assert FIXTURES_DIR.is_dir(), f"missing fixtures dir: {FIXTURES_DIR}"
    for name in (
        "current_1451_analog.json",
        "candidates_path_only_false_positive.json",
        "candidates_self_only.json",
        "current_with_open_dependency.json",
        "candidates_duplicate.json",
        "current_with_closed_dependency.json",
        "candidates_closed_predecessor.json",
        "candidates_pathset_disjoint_contract.json",
        "current_edit_target.json",
        "candidates_paraphrase_shared_edit_target.json",
        "current_with_native_blocked_by.json",
        "current_with_native_blocked_by_closed.json",
        "current_schema_collision.json",
        "candidates_partial_paths_shared_schema.json",
        "candidates_missing_body.json",
        "candidates_missing_number.json",
        "candidates_missing_allowed_paths.json",
    ):
        assert (FIXTURES_DIR / name).is_file(), f"missing fixture: {name}"


def _assert_fixture_body_sha256(fixture_path: Path) -> None:
    """全 fixture の body_sha256 が実体の body と一致することを検証する
    （AC5: live Issue 参照ではなく body_sha256 付き固定 fixture であることの保証）。
    """
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    records = data if isinstance(data, list) else [data]
    for record in records:
        if "body_sha256" not in record:
            continue  # Major 5 の欠損フィールドテスト用 fixture は意図的に省略
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
    assert exit_code == EXIT_OK

    # #9451（current 自身）は自己除外されているため candidates に現れない
    candidate_numbers = {c["issue_number"] for c in payload["candidates"]}
    assert current_number not in candidate_numbers


def test_self_exclusion_removes_current_issue_from_candidates() -> None:
    """AC6: `--issue-number` は必須であり、対象 Issue 自身は候補から除外される。
    candidates_self_only.json は current と同一 Issue 番号のみを含む候補集合であり、
    自己除外後は候補が 0 件になるため `proceed`（C0）に route する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    self_only_file = FIXTURES_DIR / "candidates_self_only.json"
    self_only_candidates = json.loads(self_only_file.read_text(encoding="utf-8"))
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    assert self_only_candidates[0]["number"] == current_number, (
        "fixture precondition: candidates_self_only.json must reference the same "
        "issue number as current_1451_analog.json to exercise self-exclusion"
    )

    exit_code, payload = _run_cli(current_number, current_file, self_only_file)

    assert payload["candidates"] == []
    assert payload["route"] == "proceed"
    assert exit_code == EXIT_OK


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


def test_given_exact_same_allowed_paths_but_disjoint_contract_then_route_is_not_duplicate() -> None:
    """Blocker 3: Allowed Paths が同一集合というだけで duplicate と即断しない。
    candidates_pathset_disjoint_contract.json は current_1451_analog.json と
    同一の Allowed Paths を持つが Outcome/In Scope/goal_ref が disjoint であり、
    readback で確認できないため `proceed_with_collision_evidence`（C1 相当）に
    route する（`duplicate` ではない）。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_pathset_disjoint_contract.json"
    )
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert exit_code == EXIT_OK
    for cand in payload["candidates"]:
        assert cand["policy_class"] != "duplicate", payload


def test_given_confirmed_duplicate_body_then_route_is_duplicate() -> None:
    """candidates_duplicate.json は current_1451_analog.json とほぼ同一の
    Outcome/In Scope を持つため、readback 確認を経て `duplicate` に route する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_duplicate.json"
    )
    assert payload["route"] == "duplicate", payload
    assert exit_code == EXIT_OK


def test_given_paraphrased_outcome_with_shared_edit_target_then_not_auto_proceeded() -> None:
    """Blocker 1: 自然言語類似度（Outcome の token Jaccard）だけを唯一の根拠に
    しない。candidates_paraphrase_shared_edit_target.json は current との
    Outcome token overlap が低い（言い換え）が、In Scope の edit target
    （`docs/dev/agent-runtime-ops.md`）を共有しており、構造シグナルが
    collision を検出するため `proceed`/`proceed_with_collision_evidence`
    へ自動で route してはならない。
    """
    current_file = FIXTURES_DIR / "current_edit_target.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_paraphrase_shared_edit_target.json",
    )
    assert payload["route"] not in {"proceed", "proceed_with_collision_evidence"}, payload
    assert exit_code == EXIT_OK
    cand = payload["candidates"][0]
    assert cand["text_similarity"] < 0.5, "fixture precondition: text similarity must be low (paraphrase)"
    assert cand["structural_signals"]["shared_edit_targets"], (
        "shared edit target must be detected structurally, independent of low text similarity"
    )


def test_given_partial_path_overlap_with_shared_output_schema_then_human_review_required() -> None:
    """構造シグナル（shared output schema 名）による collision 検出。
    Allowed Paths は部分重複のみ（同一集合ではない）だが、両 Issue の Outcome/
    In Scope が同じ output schema 名（`FOO_BAR_RESULT_V2`）を参照しており、
    `human_review_required` に route する。
    """
    current_file = FIXTURES_DIR / "current_schema_collision.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_partial_paths_shared_schema.json",
    )
    assert payload["route"] == "human_review_required", payload
    assert exit_code == EXIT_OK
    cand = payload["candidates"][0]
    assert "FOO_BAR_RESULT_V2" in cand["structural_signals"]["shared_output_schema"]


def test_given_blocked_by_open_predecessor_then_route_is_wait_for_predecessor() -> None:
    """`blocked_by` (legacy `Depends on #N`) を参照し、predecessor が OPEN の
    ままである場合は `wait_for_predecessor`（C2b）に route する。
    """
    current_file = FIXTURES_DIR / "current_with_open_dependency.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert payload["route"] == "wait_for_predecessor", payload
    assert exit_code == EXIT_OK
    assert payload["dependency_resolution"]["blocking_predecessor"]["issue_number"] == 9449


def test_given_blocked_by_closed_predecessor_then_route_is_not_blocked() -> None:
    """`blocked_by` の predecessor が CLOSED の場合は C2a として扱われ、
    readback で disjoint と確認できれば `proceed_with_collision_evidence` に
    route する（`wait_for_predecessor` ではない）。
    """
    current_file = FIXTURES_DIR / "current_with_closed_dependency.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_closed_predecessor.json"
    )
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert exit_code == EXIT_OK
    assert payload["dependency_resolution"]["closed_predecessors"] == [9449]
    assert payload["candidates"][0]["policy_class"] == "C2a"
    # #1621 P2 Major (PR #1637 レビュー): predecessor (dependency_c2a origin)
    # の dependency_relation / provenance も保存される。current の
    # Machine-Readable Contract の `blocked_by: ["#9449"]` が根拠。
    assert payload["candidates"][0]["dependency_relation"] == "predecessor", payload
    assert payload["candidates"][0]["dependency_provenance"] == [
        {"source": "current_contract_blocked_by", "repository": DEFAULT_REPO, "issue_number": 9449}
    ], payload


def test_given_native_github_dependency_blocked_by_open_then_route_is_wait_for_predecessor() -> None:
    """GitHub native dependency（`blockedBy` フィールド）を legacy `Depends on #N`
    が無くても解決し、predecessor が OPEN なら C2b に route する。
    """
    current_file = FIXTURES_DIR / "current_with_native_blocked_by.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert payload["route"] == "wait_for_predecessor", payload
    assert exit_code == EXIT_OK


def test_given_native_github_dependency_blocked_by_closed_then_route_is_c2a() -> None:
    """GitHub native dependency の predecessor が CLOSED の場合は C2a として
    readback に回す。
    """
    current_file = FIXTURES_DIR / "current_with_native_blocked_by_closed.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_closed_predecessor.json"
    )
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert exit_code == EXIT_OK
    assert payload["candidates"][0]["policy_class"] == "C2a"


def test_given_candidate_missing_body_then_route_is_human_review_required() -> None:
    """Major 5: number/body/updatedAt/Allowed Paths のいずれかが欠けた candidate は
    false positive として黙って除外されず `human_review_required` に倒す。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_missing_body.json"
    )
    assert payload["route"] == "human_review_required", payload
    assert exit_code == EXIT_OK
    assert "missing_body" in payload["validation_errors"]["9454"]


def test_given_candidate_missing_number_then_route_is_human_review_required() -> None:
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_missing_number.json"
    )
    assert payload["route"] == "human_review_required", payload
    assert exit_code == EXIT_OK
    assert "missing_or_invalid_number" in payload["validation_errors"]["-1"]


def test_given_ignored_missing_allowed_paths_candidate_then_excludes_it_from_classifier_and_returns_proceed() -> None:
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_missing_allowed_paths.json"
    )
    assert payload["route"] == "proceed", payload
    assert exit_code == EXIT_OK
    assert "9456" not in payload["validation_errors"]
    assert payload["candidates"] == []
    ignored = payload["ignored_candidates"]
    assert len(ignored) == 1
    assert ignored[0]["issue_number"] == 9456
    assert ignored[0]["reason"] == "ignored_missing_allowed_paths"
    assert ignored[0]["url"] == "https://github.com/squne121/loop-protocol/issues/9456"
    assert ignored[0]["updated_at"] == "2026-07-11T09:00:00Z"
    assert ignored[0]["body_sha256"].startswith("sha256:")
    assert ignored[0]["decision_sha256"].startswith("sha256:")


def test_given_missing_allowed_paths_and_comparable_candidate_then_preserves_comparable_collision_route() -> None:
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    missing_path_candidates = json.loads(
        (FIXTURES_DIR / "candidates_missing_allowed_paths.json").read_text(encoding="utf-8")
    )
    comparable_candidates = json.loads(
        (FIXTURES_DIR / "candidates_path_only_false_positive.json").read_text(encoding="utf-8")
    )
    combined_file = current_file.parent / "_missing_allowed_paths_with_comparable.json"
    combined_file.write_text(
        json.dumps(missing_path_candidates + comparable_candidates, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        exit_code, payload = _run_cli(current_number, current_file, combined_file)
    finally:
        combined_file.unlink(missing_ok=True)

    assert exit_code == EXIT_OK
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert {candidate["issue_number"] for candidate in payload["candidates"]} == {9449, 9450}
    assert payload["ignored_candidates"][0]["issue_number"] == 9456
    assert payload["ignored_candidates"][0]["decision_sha256"].startswith("sha256:")


def _write_overlap_inputs(tmp_path: Path, current: dict, candidates: list[dict]) -> tuple[Path, Path]:
    current_file = tmp_path / "current.json"
    candidates_file = tmp_path / "candidates.json"
    current_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    candidates_file.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return current_file, candidates_file


def _legacy_candidate(number: int, *, state: str = "OPEN") -> dict:
    return {
        "number": number,
        "title": "legacy scope",
        "body": "## Outcome\n\nlegacy\n\n## In Scope\n\n- legacy\n",
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-13T00:00:00Z",
        "url": f"https://github.com/squne121/loop-protocol/issues/{number}",
        "state": state,
    }


@pytest.mark.parametrize(
    "section_body",
    [
        "",
        "# path is intentionally absent\n",
        "-\n",
    ],
    ids=["empty", "comment_only", "malformed_entry"],
)
def test_allowed_paths_heading_empty_or_comment_only_is_fail_closed(
    tmp_path: Path, section_body: str
) -> None:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    candidate = _legacy_candidate(9550)
    candidate["body"] += f"\n## Allowed Paths\n\n{section_body}"
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["validation_errors"]["9550"] == ["invalid_allowed_paths"]
    assert payload["ignored_candidates"] == []


def test_open_legacy_predecessor_without_scope_is_unresolved_fail_closed(tmp_path: Path) -> None:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    current["body"] += "\nDepends on #9551\n"
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [_legacy_candidate(9551)])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["dependency_resolution"]["unresolved_refs"] == ["9551"]


def test_open_native_predecessor_without_scope_is_unresolved_fail_closed(tmp_path: Path) -> None:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    current["blockedBy"] = [{"repository": DEFAULT_REPO, "number": 9552, "state": "OPEN"}]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [_legacy_candidate(9552)])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["dependency_resolution"]["unresolved_refs"] == ["9552"]


def test_closed_predecessor_without_scope_is_not_an_open_scope_blocker(tmp_path: Path) -> None:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    current["body"] += "\nDepends on #9553\n"
    current_file, candidates_file = _write_overlap_inputs(
        tmp_path, current, [_legacy_candidate(9553, state="CLOSED")]
    )

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "proceed", payload
    assert payload["dependency_resolution"]["unresolved_refs"] == []


def test_cross_repository_native_predecessor_is_unresolved_fail_closed(tmp_path: Path) -> None:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    current["blockedBy"] = [{"repository": "other/repository", "number": 9554, "state": "OPEN"}]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["dependency_resolution"]["unresolved_refs"] == ["other/repository#9554"]


def test_saturation_boundary_limit_minus_one_limit_and_limit_plus_one() -> None:
    """収集上限の limit-1 / limit / limit+1 境界（saturation 検出）。
    `candidates_path_only_false_positive.json` は自己除外前 3 件（#9449/#9450/
    #9451）、自己除外後 2 件（#9449/#9450）。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    raw_count = len(json.loads(candidates_file.read_text(encoding="utf-8")))

    # limit-1: saturated=true（取得件数が limit を上回る）
    _, payload = _run_cli(current_number, current_file, candidates_file, "--limit", str(raw_count - 1))
    assert payload["source"]["saturated"] is True, payload
    assert payload["route"] == "human_review_required", payload  # 全件性を証明できない -> fail-closed

    # limit == raw_count: saturated=true（>= 境界）
    _, payload = _run_cli(current_number, current_file, candidates_file, "--limit", str(raw_count))
    assert payload["source"]["saturated"] is True, payload

    # limit+1: saturated=false
    _, payload = _run_cli(current_number, current_file, candidates_file, "--limit", str(raw_count + 1))
    assert payload["source"]["saturated"] is False, payload
    assert payload["route"] == "proceed_with_collision_evidence", payload


def test_exit_code_contract_covers_all_known_routes_including_wait_and_human_review() -> None:
    """AC7 改訂: すべての closed route（`wait_for_predecessor` /
    `human_review_required` を含む）で分類成功時は exit 0 を返す（Major 2）。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]

    # proceed (C0): no candidates at all
    empty_file = current_file.parent / "_empty_candidates.json"
    empty_file.write_text("[]", encoding="utf-8")
    try:
        exit_code, payload = _run_cli(current_number, current_file, empty_file)
        assert payload["route"] == "proceed"
        assert exit_code == EXIT_OK
    finally:
        empty_file.unlink(missing_ok=True)

    # proceed_with_collision_evidence
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert payload["route"] == "proceed_with_collision_evidence"
    assert exit_code == EXIT_OK

    # duplicate
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_duplicate.json"
    )
    assert payload["route"] == "duplicate"
    assert exit_code == EXIT_OK

    # wait_for_predecessor (C2b)
    dep_current = FIXTURES_DIR / "current_with_open_dependency.json"
    dep_number = json.loads(dep_current.read_text(encoding="utf-8"))["number"]
    exit_code, payload = _run_cli(
        dep_number, dep_current, FIXTURES_DIR / "candidates_path_only_false_positive.json"
    )
    assert payload["route"] == "wait_for_predecessor"
    assert exit_code == EXIT_OK

    # human_review_required
    exit_code, payload = _run_cli(
        current_number, current_file, FIXTURES_DIR / "candidates_missing_body.json"
    )
    assert payload["route"] == "human_review_required"
    assert exit_code == EXIT_OK

    # runtime_error: invalid JSON candidates file
    bad_file = current_file.parent / "_bad_candidates.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    try:
        exit_code, payload = _run_cli(current_number, current_file, bad_file)
        assert payload["route"] == "runtime_error"
        assert exit_code == EXIT_RUNTIME_ERROR
    finally:
        bad_file.unlink(missing_ok=True)


def test_unknown_route_value_never_escapes_closed_set() -> None:
    """AC7: route は必ず ROUTES の closed set に含まれる。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    _, payload = _run_cli(
        current_number,
        current_file,
        FIXTURES_DIR / "candidates_self_only.json",
    )
    assert payload["route"] in ROUTES


def test_continue_routes_survive_bash_set_dash_e() -> None:
    """Major 2: `set -e` 環境でも継続 route（`proceed_with_collision_evidence` /
    `wait_for_predecessor` / `human_review_required` / `duplicate`）が exit 0 で
    正しく次のコマンドへ進むことを、実際の bash `set -e` 経由で検証する。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"

    script = (
        "set -e\n"
        f"{sys.executable} {HELPER} --issue-number {current_number} --dry-run "
        f"--current-file {current_file} --candidates-file {candidates_file} "
        f"--repo {DEFAULT_REPO} > /dev/null\n"
        "echo CONTINUED_AFTER_SET_E\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "CONTINUED_AFTER_SET_E" in proc.stdout


def test_evidence_schema_contains_implement_scope_collision_preflight_v1_fields() -> None:
    """AC8: `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence の必須フィールドを検証する
    （Major 3/4 の per-candidate policy_class/reasons、decision_inputs_sha256 を含む）。
    """
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
    assert isinstance(source["limit"], int)
    assert source["limit"] == 100
    assert "collected_at" in source

    assert isinstance(payload["candidates"], list)
    for cand in payload["candidates"]:
        assert "issue_number" in cand
        assert "updated_at" in cand
        assert "overlapping_paths" in cand
        assert "heading_overlap" in cand
        assert "change_kind_equal" in cand
        assert "machine_readable_keys_intersection" in cand
        assert "policy_class" in cand
        assert "reasons" in cand and isinstance(cand["reasons"], list)
        assert "non_conflict_reason" in cand

    assert "dependency_resolution" in payload
    assert isinstance(payload["ignored_candidates"], list)
    assert "validation_errors" in payload
    assert payload["route"] in ROUTES
    assert payload["decision_inputs_sha256"].startswith("sha256:")
    assert payload["evidence_sha256"].startswith("sha256:")


def test_decision_inputs_sha256_is_time_independent() -> None:
    """Major 4 ケース 1: 同じ入力で collected_at のみ異なる 2 回の実行間で
    `decision_inputs_sha256` は同じ値になる（`evidence_sha256` は時刻を含むため
    異なりうる）。
    """
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"

    _, payload_a = _run_cli(current_number, current_file, candidates_file)
    _, payload_b = _run_cli(current_number, current_file, candidates_file)

    assert payload_a["decision_inputs_sha256"] == payload_b["decision_inputs_sha256"]


def test_decision_inputs_sha256_changes_when_candidate_body_drifts() -> None:
    """Major 4 ケース 2: candidate body を 1 文字変更すると digest が変わる。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"

    _, payload_before = _run_cli(current_number, current_file, candidates_file)

    original = json.loads(candidates_file.read_text(encoding="utf-8"))
    drifted = json.loads(json.dumps(original))
    drifted[0]["body"] = drifted[0]["body"] + " "  # 1 文字分の drift
    drifted_file = candidates_file.parent / "_drifted_candidates.json"
    drifted_file.write_text(json.dumps(drifted, ensure_ascii=False), encoding="utf-8")
    try:
        _, payload_after = _run_cli(current_number, current_file, drifted_file)
    finally:
        drifted_file.unlink(missing_ok=True)

    assert payload_before["decision_inputs_sha256"] != payload_after["decision_inputs_sha256"]


def test_decision_inputs_sha256_is_order_independent_after_canonical_sort() -> None:
    """Major 4 ケース 3: candidate の列挙順序を変えても canonical sort 後は
    同じ digest になる。"""
    current_file = FIXTURES_DIR / "current_1451_analog.json"
    current_number = json.loads(current_file.read_text(encoding="utf-8"))["number"]
    candidates_file = FIXTURES_DIR / "candidates_path_only_false_positive.json"

    original = json.loads(candidates_file.read_text(encoding="utf-8"))
    reordered = list(reversed(original))
    reordered_file = candidates_file.parent / "_reordered_candidates.json"
    reordered_file.write_text(json.dumps(reordered, ensure_ascii=False), encoding="utf-8")
    try:
        _, payload_original = _run_cli(current_number, current_file, candidates_file)
        _, payload_reordered = _run_cli(current_number, current_file, reordered_file)
    finally:
        reordered_file.unlink(missing_ok=True)

    assert payload_original["decision_inputs_sha256"] == payload_reordered["decision_inputs_sha256"]


def _human_c1_comment(
    *,
    current: dict,
    candidate: dict,
    author_association: str = "OWNER",
    decision: str = "C1/non-conflict",
    comment_id: int = 9000000001,
) -> dict:
    """#1613 の固定 comment schema を作るテスト用 helper。"""
    url = f"https://github.com/squne121/loop-protocol/issues/{current['number']}#issuecomment-{comment_id}"
    body = "\n".join(
        [
            "```yaml",
            "HUMAN_C1_DECISION_V1:",
            f"  candidate_issue_number: {candidate['number']}",
            f"  decision: {decision}",
            f"  current_body_sha256: sha256:{_sha256(current['body'])}",
            f"  candidate_body_sha256: sha256:{_sha256(candidate['body'])}",
            "```",
        ]
    )
    return {
        "body": body,
        "comment_id": comment_id,
        "comment_url": url,
        "author_login": "squne121",
        "author_association": author_association,
        "created_at": "2026-07-18T12:02:00Z",
        "updated_at": "2026-07-18T12:02:00Z",
    }


def _collision_candidate(number: int = 9771) -> dict:
    return {
        "number": number,
        "title": "same schema collision",
        "body": "\n".join(
            [
                "## Machine-Readable Contract",
                "",
                "```yaml",
                "contract_schema_version: v1",
                "issue_kind: implementation",
                "parent_issue: \"none\"",
                "goal_ref: \"different goal\"",
                "change_kind: code",
                "```",
                "",
                "## Outcome",
                "",
                "candidate has a structurally colliding result.",
                "",
                "## In Scope",
                "",
                "- `docs/dev/agent-runtime-ops.md`",
                "",
                "## Allowed Paths",
                "",
                "- `docs/dev/agent-runtime-ops.md`",
                "",
                "## Acceptance Criteria",
                "",
                "- [ ] AC1: candidate contract",
                "",
                "## Delivery Rule",
                "",
                "- 1 Issue = 1 PR",
            ]
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-18T12:00:00Z",
        "url": f"https://github.com/squne121/loop-protocol/issues/{number}",
        "state": "OPEN",
    }


def _current_with_shared_target(tmp_path: Path) -> dict:
    current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    current["body"] = current["body"].replace(
        "## Allowed Paths", "## In Scope\n\n- `shared/target.py`\n\n## Allowed Paths"
    )
    current["body"] = current["body"].replace(
        "## Acceptance Criteria", "- `shared/target.py`\n\n## Acceptance Criteria"
    )
    current["updatedAt"] = "2026-07-18T12:01:00Z"
    return current


@pytest.mark.parametrize("author_association", ["OWNER", "COLLABORATOR"])
def test_human_c1_decision_accepts_exact_owner_or_collaborator(
    tmp_path: Path, author_association: str
) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate, author_association=author_association)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert not payload["human_c1_decisions"]["rejected"], payload["human_c1_decisions"]
    assert payload["route"] == "proceed_with_collision_evidence", payload
    decision = payload["candidates"][0]["human_c1_decision"]
    assert decision["verified"] is True
    assert decision["author_association"] == author_association
    assert decision["comment_url"] == comment["comment_url"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda current, candidate, comment: comment.update(
            {"body": comment["body"].replace("C1/non-conflict", "C2a")}
        ),
        lambda current, candidate, comment: comment.update(
            {"body": comment["body"].replace(str(candidate["number"]), "9998", 1)}
        ),
        lambda current, candidate, comment: comment.update(
            {"body": comment["body"].replace("sha256:", "sha256:bad", 1)}
        ),
    ],
    ids=["decision", "candidate", "sha"],
)
def test_human_c1_decision_rejects(tmp_path: Path, mutation: Any) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    mutation(current, candidate, comment)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["human_c1_decisions"]["rejected"]


def test_human_c1_stale_attempt_is_audit_only_after_later_exact_same_candidate_decision(
    tmp_path: Path,
) -> None:
    """AC1: 同一 candidate の古い hash mismatch は、後続の exact decision が
    あれば audit evidence に残すだけで route を恒久停止させない。"""
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    stale = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000001)
    current["body"] += "\n<!-- current body changed after stale decision -->\n"
    exact = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    stale["updated_at"] = "2026-07-18T12:01:00Z"
    exact["updated_at"] = "2026-07-18T12:02:00Z"
    current["comments"] = [stale, exact]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "proceed_with_collision_evidence", payload
    decisions = payload["human_c1_decisions"]
    assert decisions["rejected"] == []
    assert [item["reason"] for item in decisions["ignored_non_routing"]] == [
        "current_body_sha256_mismatch"
    ]
    assert decisions["ignored_non_routing"][0]["parsed_candidate_issue_number"] == candidate["number"]
    assert decisions["accepted"][0]["comment_id"] == exact["comment_id"]


def test_human_c1_accepted_other_candidate_does_not_clear_unresolved_candidate(tmp_path: Path) -> None:
    """AC2: candidate A の accepted decision は candidate B の collision を
    解消しないため、route は fail-closed のままである。"""
    current = _current_with_shared_target(tmp_path)
    accepted_candidate = _collision_candidate(9771)
    unresolved_candidate = _collision_candidate(9772)
    unresolved_candidate["body"] = unresolved_candidate["body"].replace(
        "## Outcome\n\ncandidate has a structurally colliding result.\n\n", ""
    )
    current["comments"] = [
        _human_c1_comment(current=current, candidate=accepted_candidate, comment_id=9000000001)
    ]
    current_file, candidates_file = _write_overlap_inputs(
        tmp_path, current, [accepted_candidate, unresolved_candidate]
    )

    exit_code, payload = _run_cli(current["number"], current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "human_review_required", payload
    assert payload["human_c1_decisions"]["rejected"] == []
    accepted_evidence = next(
        item for item in payload["candidates"] if item["issue_number"] == accepted_candidate["number"]
    )
    unresolved_evidence = next(
        item for item in payload["candidates"] if item["issue_number"] == unresolved_candidate["number"]
    )
    assert accepted_evidence["human_c1_decision"]["candidate_issue_number"] == str(
        accepted_candidate["number"]
    )
    assert unresolved_evidence["readback_complete"] is False
    assert "human_c1_decision" not in unresolved_evidence


def test_human_c1_decision_does_not_override_duplicate_c2b_or_saturation(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, saturated = _run_cli(current["number"], current_file, candidates_file, "--limit", "1")
    assert saturated["route"] == "human_review_required", saturated

    duplicate_current = json.loads((FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    duplicate_candidate = json.loads(
        (FIXTURES_DIR / "candidates_duplicate.json").read_text(encoding="utf-8")
    )[0]
    duplicate_current["comments"] = [_human_c1_comment(current=duplicate_current, candidate=duplicate_candidate)]
    duplicate_current_file, duplicate_candidates_file = _write_overlap_inputs(
        tmp_path, duplicate_current, [duplicate_candidate]
    )
    _, duplicate = _run_cli(duplicate_current["number"], duplicate_current_file, duplicate_candidates_file)
    assert duplicate["route"] == "duplicate", duplicate
    assert duplicate["human_c1_decisions"]["rejected"] == []
    assert (
        duplicate["human_c1_decisions"]["ignored_non_routing"][0]["reason"]
        == "no_current_c1_candidate"
    )

    c2b_current = json.loads((FIXTURES_DIR / "current_with_open_dependency.json").read_text(encoding="utf-8"))
    c2b_candidates = json.loads(
        (FIXTURES_DIR / "candidates_path_only_false_positive.json").read_text(encoding="utf-8")
    )
    c2b_current["comments"] = [_human_c1_comment(current=c2b_current, candidate=c2b_candidates[0])]
    c2b_current_file, c2b_candidates_file = _write_overlap_inputs(tmp_path, c2b_current, c2b_candidates)
    _, c2b = _run_cli(c2b_current["number"], c2b_current_file, c2b_candidates_file)
    assert c2b["route"] == "wait_for_predecessor", c2b


def test_human_c1_decision_evidence_is_deterministic_and_sha_bound(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, first = _run_cli(current["number"], current_file, candidates_file)
    _, second = _run_cli(current["number"], current_file, candidates_file)

    assert first["decision_inputs_sha256"] == second["decision_inputs_sha256"]
    assert first["human_c1_decisions"]["accepted"] == second["human_c1_decisions"]["accepted"]
    accepted = first["human_c1_decisions"]["accepted"][0]
    assert accepted["current_body_sha256"] == f"sha256:{_sha256(current['body'])}"
    assert accepted["candidate_body_sha256"] == f"sha256:{_sha256(candidate['body'])}"


def test_human_c1_decision_lifecycle_uses_rest_metadata_not_self_declared_url(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    accepted = payload["human_c1_decisions"]["accepted"][0]
    assert "comment_url" not in comment["body"]
    assert accepted["comment_id"] == comment["comment_id"]
    assert accepted["comment_url"] == comment["comment_url"]
    assert accepted["author_login"] == "squne121"


def test_human_c1_decision_trust_ignores_untrusted_before_schema_parse(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate, author_association="MEMBER")
    comment["body"] = "HUMAN_C1_DECISION_V1\nnot even yaml"
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    decisions = payload["human_c1_decisions"]
    assert decisions["accepted"] == []
    assert decisions["rejected"] == []
    assert decisions["ignored_untrusted"][0]["reason"] == "untrusted_author_association"


def test_human_c1_decision_trusted_malformed_is_non_routing_without_c1_candidate(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    comment = _human_c1_comment(current=current, candidate=_collision_candidate())
    comment["body"] = "HUMAN_C1_DECISION_V1\nnot even yaml"
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    decisions = payload["human_c1_decisions"]
    assert payload["route"] == "proceed"
    assert decisions["accepted"] == []
    assert decisions["rejected"] == []
    assert decisions["ignored_non_routing"][0]["reason"] == "no_current_c1_candidate"


def test_human_c1_decision_untrusted_mimic_is_non_routing_without_c1_candidate(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    comment = _human_c1_comment(
        current=current,
        candidate=_collision_candidate(),
        author_association="MEMBER",
    )
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    decisions = payload["human_c1_decisions"]
    assert payload["route"] == "proceed"
    assert decisions["accepted"] == []
    assert decisions["rejected"] == []
    assert decisions["ignored_untrusted"] == []
    assert decisions["ignored_non_routing"][0]["reason"] == "no_current_c1_candidate"


def test_human_c1_decision_trust_rejects_malformed_trusted_with_full_audit(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    comment["body"] = "HUMAN_C1_DECISION_V1\nnot even yaml"
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    rejected = payload["human_c1_decisions"]["rejected"][0]
    assert payload["route"] == "human_review_required"
    assert set(rejected) == {
        "comment_id",
        "comment_url",
        "comment_body_sha256",
        "comment_updated_at",
        "author_login",
        "author_association",
        "parsed_candidate_issue_number",
        "reason",
    }


def test_human_c1_decision_final_readback_detects_fingerprint_drift(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    comment = _human_c1_comment(current=current, candidate=candidate)
    decisions = checker_module._validate_human_c1_decisions(
        comments=[comment],
        repository=DEFAULT_REPO,
        current_number=current["number"],
        current_body=current["body"],
        candidates_evidence=[
            {
                "issue_number": candidate["number"],
                "policy_class": "C1",
                "body_sha256": f"sha256:{_sha256(candidate['body'])}",
            }
        ],
    )

    final_current = dict(current)
    final_current["body"] += " drift"
    assert checker_module._apply_final_readback_drift(
        decisions=decisions,
        initial_comments=[comment],
        final_current=final_current,
        final_candidates={candidate["number"]: candidate},
        final_comments=[comment],
    )
    assert decisions["accepted"][0]["final_readback_verified"] is False
    assert decisions["rejected"][0]["reason"] == "final_readback_fingerprint_drift"


# ------------------------------------------------------------
# #1621 AC3/AC4/AC5/AC10: successor index injection (adapter, --dry-run
# path) fixes the adapter's blanket origin/verdict -> policy_class mapping
# so a successor candidate is evidenced as C2a (not C1), distinguished
# per-candidate from a normal C1 candidate in the same overlap_partial set,
# and PR #1615's human C1 decision override cannot clear it.
# ------------------------------------------------------------


def _body_with_paths(*, parent_issue: str, goal_ref: str, outcome: str, paths: list[str]) -> str:
    path_lines = "\n".join(f"- {p}" for p in paths)
    return "\n".join(
        [
            "## Machine-Readable Contract",
            "",
            "```yaml",
            "contract_schema_version: v1",
            "issue_kind: implementation",
            f'parent_issue: "{parent_issue}"',
            f'goal_ref: "{goal_ref}"',
            "change_kind: code",
            "```",
            "",
            "## Outcome",
            "",
            outcome,
            "",
            "## In Scope",
            "",
            path_lines,
            "",
            "## Allowed Paths",
            "",
            path_lines,
            "",
        ]
    )


def test_given_shared_parent_native_blocking_successor_then_route_is_c2a_not_parent_collision(tmp_path: Path) -> None:
    """#1621 AC3/AC4: shared parent_refs を持つ candidate は旧実装では第一段階で
    parent_child_collision(AMBIGUOUS_REQUIRES_HUMAN) になり human_review_required
    に停止していた。current の native blocking から構築した successor index が
    最初の classify_overlap() 呼び出し前に candidate の depends_on へ current
    番号を注入することで、successor_dependency_ordering(C2a) と判定され
    proceed_with_collision_evidence に route する。
    """
    current_number = 9710
    candidate_number = 9711
    current = {
        "number": current_number,
        "title": "実装: current side",
        "body": _body_with_paths(
            parent_issue="#9690",
            goal_ref="current goal alpha",
            outcome="current outcome about alpha beta gamma.",
            paths=["docs/dev/successor_shared_dry_run.md"],
        ),
        "updatedAt": "2026-07-19T00:00:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{current_number}",
        "blocking": [{"repository": DEFAULT_REPO, "number": candidate_number, "state": "OPEN"}],
    }
    candidate = {
        "number": candidate_number,
        "title": "実装: candidate side",
        "body": _body_with_paths(
            parent_issue="#9690",
            goal_ref="candidate goal beta",
            outcome="candidate outcome about delta epsilon zeta.",
            paths=["docs/dev/successor_shared_dry_run.md"],
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:05:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{candidate_number}",
        "state": "OPEN",
        # 注意: blockedBy は意図的に存在しない
    }
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    exit_code, payload = _run_cli(current_number, current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert len(payload["candidates"]) == 1
    cand_evidence = payload["candidates"][0]
    assert cand_evidence["issue_number"] == candidate_number
    assert cand_evidence["policy_class"] == "C2a", payload
    assert "successor_dependency_ordering" in cand_evidence["reasons"], payload
    # #1621 P2 Major (PR #1637 レビュー): dependency_relation / provenance
    # が candidate evidence に保存され、current の native blocking が根拠
    # であることが監査可能。
    assert cand_evidence["dependency_relation"] == "successor", payload
    assert cand_evidence["dependency_provenance"] == [
        {"source": "current_native_blocking", "repository": DEFAULT_REPO, "issue_number": current_number}
    ], payload


def test_given_mixed_normal_c1_and_successor_candidates_then_policy_class_distinguished_per_candidate(
    tmp_path: Path,
) -> None:
    """#1621 AC5: 同一 overlap_partial 集合に通常 C1 candidate（successor でも
    predecessor でもない）と successor candidate が混在する場合、
    candidates_evidence 内で両者の policy_class が candidate ごとに区別される。
    """
    current_number = 9720
    successor_number = 9721
    normal_number = 9722
    shared_path = "docs/dev/mixed_c1_c2a.md"
    current = {
        "number": current_number,
        "title": "実装: current mixed side",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="current goal mixed",
            outcome="current outcome about alpha beta gamma delta.",
            paths=[shared_path, "docs/dev/mixed_current_only.md"],
        ),
        "updatedAt": "2026-07-19T00:00:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{current_number}",
        "blocking": [{"repository": DEFAULT_REPO, "number": successor_number, "state": "OPEN"}],
    }
    successor_candidate = {
        "number": successor_number,
        "title": "実装: successor candidate",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="successor goal",
            outcome="successor outcome about epsilon zeta eta theta.",
            paths=[shared_path],
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:05:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{successor_number}",
        "state": "OPEN",
    }
    normal_candidate = {
        "number": normal_number,
        "title": "実装: normal C1 candidate",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="normal goal",
            outcome="normal outcome about iota kappa lambda mu.",
            paths=[shared_path],
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:06:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{normal_number}",
        "state": "OPEN",
    }
    current_file, candidates_file = _write_overlap_inputs(
        tmp_path, current, [successor_candidate, normal_candidate]
    )

    exit_code, payload = _run_cli(current_number, current_file, candidates_file)

    assert exit_code == EXIT_OK
    assert payload["route"] == "proceed_with_collision_evidence", payload
    by_number = {c["issue_number"]: c for c in payload["candidates"]}
    assert set(by_number) == {successor_number, normal_number}
    assert by_number[successor_number]["policy_class"] == "C2a", payload
    assert "successor_dependency_ordering" in by_number[successor_number]["reasons"], payload
    assert by_number[normal_number]["policy_class"] == "C1", payload
    assert "successor_dependency_ordering" not in by_number[normal_number]["reasons"], payload
    # #1621 P2 Major (PR #1637 レビュー): dependency_relation は candidate
    # ごとに区別され、normal C1 candidate は "none" のまま provenance も
    # 空である。
    assert by_number[successor_number]["dependency_relation"] == "successor", payload
    assert by_number[successor_number]["dependency_provenance"] == [
        {"source": "current_native_blocking", "repository": DEFAULT_REPO, "issue_number": current_number}
    ], payload
    assert by_number[normal_number]["dependency_relation"] == "none", payload
    assert by_number[normal_number]["dependency_provenance"] == [], payload


def test_given_human_c1_decision_targets_successor_c2a_candidate_when_c1_candidate_also_present_then_rejected(
    tmp_path: Path,
) -> None:
    """#1621 AC10: 同一 evidence 集合に通常 C1 candidate と successor C2a
    candidate が混在する場合でも、human C1 decision override は C2a
    candidate を解除できない（policy_class == "C1" のみを対象とする既存
    validator が candidate_not_current_c1_overlap で拒否する）。
    """
    current_number = 9750
    successor_number = 9751
    normal_number = 9752
    shared_path = "docs/dev/ac10_mixed.md"
    current = {
        "number": current_number,
        "title": "実装: current AC10 mixed side",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="current goal ac10 mixed",
            outcome="current outcome about alpha beta gamma ac10 mixed.",
            paths=[shared_path, "docs/dev/ac10_current_only.md"],
        ),
        "updatedAt": "2026-07-19T00:00:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{current_number}",
        "blocking": [{"repository": DEFAULT_REPO, "number": successor_number, "state": "OPEN"}],
    }
    successor_candidate = {
        "number": successor_number,
        "title": "実装: successor AC10 candidate",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="successor goal ac10",
            outcome="successor outcome about epsilon zeta eta theta ac10.",
            paths=[shared_path],
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:05:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{successor_number}",
        "state": "OPEN",
    }
    normal_candidate = {
        "number": normal_number,
        "title": "実装: normal AC10 candidate",
        "body": _body_with_paths(
            parent_issue="none",
            goal_ref="normal goal ac10",
            outcome="normal outcome about iota kappa lambda mu ac10.",
            paths=[shared_path],
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:06:00Z",
        "url": f"https://github.com/{DEFAULT_REPO}/issues/{normal_number}",
        "state": "OPEN",
    }
    comment = _human_c1_comment(current=current, candidate=successor_candidate)
    current["comments"] = [comment]
    current_file, candidates_file = _write_overlap_inputs(
        tmp_path, current, [successor_candidate, normal_candidate]
    )

    exit_code, payload = _run_cli(current_number, current_file, candidates_file)

    assert exit_code == EXIT_OK
    by_number = {c["issue_number"]: c for c in payload["candidates"]}
    assert by_number[successor_number]["policy_class"] == "C2a", payload
    assert by_number[normal_number]["policy_class"] == "C1", payload
    decisions = payload["human_c1_decisions"]
    assert decisions["accepted"] == []
    rejected_reasons = {item["reason"] for item in decisions["rejected"]}
    assert "candidate_not_current_c1_overlap" in rejected_reasons, payload
def _accepted_decisions(current: dict, candidate: dict, comments: list[dict]) -> dict:
    return checker_module._validate_human_c1_decisions(
        comments=comments,
        repository=DEFAULT_REPO,
        current_number=current["number"],
        current_body=current["body"],
        candidates_evidence=[
            {
                "issue_number": candidate["number"],
                "policy_class": "C1",
                "body_sha256": f"sha256:{_sha256(candidate['body'])}",
            }
        ],
    )


def test_human_c1_final_readback_new_trusted_malformed_comment_requires_human_review(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    accepted = _human_c1_comment(current=current, candidate=candidate)
    initial_comments = [accepted]
    decisions = _accepted_decisions(current, candidate, initial_comments)
    malformed = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    malformed["body"] = "HUMAN_C1_DECISION_V1\nnot even yaml"

    assert checker_module._apply_final_readback_drift(
        decisions=decisions,
        initial_comments=initial_comments,
        final_current=current,
        final_candidates={candidate["number"]: candidate},
        final_comments=[accepted, malformed],
    )
    assert any(
        item["reason"] == "final_readback_trusted_comment_set_drift"
        for item in decisions["rejected"]
    )


def test_human_c1_final_readback_edited_ignored_non_routing_comment_requires_human_review(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    stale = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000001)
    current["body"] += "\n<!-- current body changed after stale decision -->\n"
    accepted = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    stale["updated_at"] = "2026-07-18T12:01:00Z"
    accepted["updated_at"] = "2026-07-18T12:02:00Z"
    initial_comments = [stale, accepted]
    decisions = _accepted_decisions(current, candidate, initial_comments)
    assert decisions["ignored_non_routing"]
    edited_stale = dict(stale)
    edited_stale["body"] += "\n<!-- edited after initial readback -->"
    edited_stale["updated_at"] = "2026-07-18T12:03:00Z"

    assert checker_module._apply_final_readback_drift(
        decisions=decisions,
        initial_comments=initial_comments,
        final_current=current,
        final_candidates={candidate["number"]: candidate},
        final_comments=[edited_stale, accepted],
    )
    assert any(
        item["reason"] == "final_readback_trusted_comment_set_drift"
        for item in decisions["rejected"]
    )


def test_human_c1_duplicate_candidate_lines_are_not_superseded_by_later_candidate_a(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate(9771)
    malformed = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000001)
    malformed["body"] = malformed["body"].replace(
        "  candidate_issue_number: 9771\n",
        "  candidate_issue_number: 9771\n  candidate_issue_number: 9772\n",
    )
    malformed["updated_at"] = "2026-07-18T12:01:00Z"
    accepted = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    accepted["updated_at"] = "2026-07-18T12:02:00Z"
    current["comments"] = [malformed, accepted]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    rejection = payload["human_c1_decisions"]["rejected"][0]
    assert payload["route"] == "human_review_required"
    assert rejection["reason"] == "schema_field_duplicate"
    assert rejection["parsed_candidate_issue_number"] is None


def test_human_c1_outside_candidate_does_not_bind_malformed_block_to_candidate_a(
    tmp_path: Path,
) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate_a = _collision_candidate(9771)
    malformed = _human_c1_comment(current=current, candidate=candidate_a, comment_id=9000000001)
    malformed["body"] = "\n".join(
        [
            "candidate_issue_number: 9771",
            "```yaml",
            "HUMAN_C1_DECISION_V1:",
            "  candidate_issue_number: 9772",
            "  decision: C1/non-conflict",
            f"  current_body_sha256: sha256:{_sha256(current['body'])}",
            f"  candidate_body_sha256: sha256:{_sha256(candidate_a['body'])}",
            "  unexpected_field: fail-closed",
            "```",
        ]
    )
    malformed["updated_at"] = "2026-07-18T12:01:00Z"
    accepted = _human_c1_comment(current=current, candidate=candidate_a, comment_id=9000000002)
    accepted["updated_at"] = "2026-07-18T12:02:00Z"
    current["comments"] = [malformed, accepted]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate_a])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    rejection = payload["human_c1_decisions"]["rejected"][0]
    assert payload["route"] == "human_review_required"
    assert rejection["reason"] == "schema_fields_missing_or_unknown"
    assert rejection["parsed_candidate_issue_number"] == 9772


def test_human_c1_stale_comment_edited_after_acceptance_is_not_superseded(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    stale = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000001)
    current["body"] += "\n<!-- current body changed after stale decision -->\n"
    accepted = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    stale["updated_at"] = "2026-07-18T12:03:00Z"
    accepted["updated_at"] = "2026-07-18T12:02:00Z"
    current["comments"] = [stale, accepted]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    assert payload["route"] == "human_review_required"
    assert payload["human_c1_decisions"]["rejected"][0]["reason"] == "current_body_sha256_mismatch"


def test_human_c1_non_supersedable_rejection_reason_remains_routing(tmp_path: Path) -> None:
    current = _current_with_shared_target(tmp_path)
    candidate = _collision_candidate()
    rejected = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000001)
    rejected["body"] = rejected["body"].replace("C1/non-conflict", "C2a")
    rejected["updated_at"] = "2026-07-18T12:01:00Z"
    accepted = _human_c1_comment(current=current, candidate=candidate, comment_id=9000000002)
    accepted["updated_at"] = "2026-07-18T12:02:00Z"
    current["comments"] = [rejected, accepted]
    current_file, candidates_file = _write_overlap_inputs(tmp_path, current, [candidate])

    _, payload = _run_cli(current["number"], current_file, candidates_file)

    assert payload["route"] == "human_review_required"
    assert payload["human_c1_decisions"]["rejected"][0]["reason"] == "decision_not_c1_non_conflict"
