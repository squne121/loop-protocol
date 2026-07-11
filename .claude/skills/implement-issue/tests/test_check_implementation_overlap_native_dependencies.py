"""AC6-AC11 (#1462): GitHub native issue dependency（`blockedBy` / `blocking`）
取得を検証する。

`check_implementation_overlap.py` を直接 import し、REST issue-dependencies
endpoint（既定経路）を `subprocess.run` mock で fixture 化して、pagination の
完全性・typed record（`{repository, number, state}`）・`blockedBy`/`blocking`
の方向区別・repository 不一致時の fail-closed 挙動を検証する。

AC11 のみ、実際の `squne121/loop-protocol` repository に対する read-only
smoke test（`gh auth status` が失敗する環境では SKIP: `pytest.exit(...,
returncode=77)`）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "check_implementation_overlap.py"
)

_spec = importlib.util.spec_from_file_location("check_implementation_overlap_native_deps", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

fetch_native_dependencies = module.fetch_native_dependencies
fetch_all_native_dependencies = module.fetch_all_native_dependencies
_normalize_native_dependency_record = module._normalize_native_dependency_record
_extract_native_dependency_numbers = module._extract_native_dependency_numbers
_merge_dependency_refs = module._merge_dependency_refs
_resolve_dependency = module._resolve_dependency
OverlapRuntimeError = module.OverlapRuntimeError
IssueScope = module.IssueScope

REPO = "squne121/loop-protocol"


def _dep_item(*, number: int, state: str, repository: str = REPO) -> Dict[str, Any]:
    # REST issue-dependencies endpoint の raw 応答形（nested repository object）。
    return {"number": number, "state": state, "repository": {"full_name": repository}}


def _typed_record(*, number: int, state: str, repository: str = REPO) -> Dict[str, Any]:
    # _normalize_native_dependency_record 適用後の flat typed record 形。
    # run() の online 経路はこの形で current_raw["blockedBy"]/["blocking"] に
    # 格納する（_merge_dependency_refs / _extract_native_dependency_numbers が
    # 直接消費する形）。
    return {"repository": repository, "number": number, "state": state}


class _FakeGhApiRunner:
    """`gh api repos/{repo}/issues/{n}/dependencies/{direction}` の paged 応答を
    シミュレートする。`pages` は `direction` ごとの page 番号 -> item list。
    """

    def __init__(self, pages: Dict[str, Dict[int, List[Dict[str, Any]]]]):
        self.pages = pages
        self.calls: List[List[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        assert args[0] == "gh" and args[1] == "api"
        endpoint = args[2]
        direction = endpoint.rsplit("/", 1)[-1]
        page = 1
        for i, tok in enumerate(args):
            if tok == "-f" and i + 1 < len(args) and str(args[i + 1]).startswith("page="):
                page = int(str(args[i + 1]).split("=", 1)[1])
        items = self.pages.get(direction, {}).get(page, [])
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(items), stderr=""
        )


# ------------------------------------------------------------
# AC7: typed record（{repository, number, state}）が非空リストとして返る
# ------------------------------------------------------------


def test_given_rest_fixture_when_fetch_native_dependencies_then_typed_record_returned(monkeypatch) -> None:
    fake = _FakeGhApiRunner(
        {
            "blocked_by": {1: [_dep_item(number=9449, state="OPEN")]},
        }
    )
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert records == ({"repository": REPO, "number": 9449, "state": "OPEN"},)


def test_given_both_directions_when_fetch_all_native_dependencies_then_typed_record_returned(monkeypatch) -> None:
    fake = _FakeGhApiRunner(
        {
            "blocked_by": {1: [_dep_item(number=9449, state="CLOSED")]},
            "blocking": {1: [_dep_item(number=9500, state="OPEN")]},
        }
    )
    monkeypatch.setattr(module.subprocess, "run", fake)

    result = fetch_all_native_dependencies(REPO, 1462)

    assert result["blockedBy"] == ({"repository": REPO, "number": 9449, "state": "CLOSED"},)
    assert result["blocking"] == ({"repository": REPO, "number": 9500, "state": "OPEN"},)


# ------------------------------------------------------------
# AC8: pagination boundary の完全性（50/51 件、100/101 件）
# ------------------------------------------------------------


def test_given_51_items_when_page_size_50_then_pagination_boundary_returns_all_51(monkeypatch) -> None:
    monkeypatch.setattr(module, "_NATIVE_DEPENDENCY_PAGE_SIZE", 50)
    page1 = [_dep_item(number=n, state="OPEN") for n in range(1, 51)]  # 50 items
    page2 = [_dep_item(number=51, state="OPEN")]  # 1 item -> pagination continues then stops
    fake = _FakeGhApiRunner({"blocked_by": {1: page1, 2: page2}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert len(records) == 51
    assert {r["number"] for r in records} == set(range(1, 52))
    # page 1 と page 2 の両方が要求された（全ページ取得の完全性）
    assert any("page=1" in c for c in fake.calls)
    assert any("page=2" in c for c in fake.calls)


def test_given_50_items_when_page_size_50_then_pagination_boundary_stops_at_single_page(monkeypatch) -> None:
    monkeypatch.setattr(module, "_NATIVE_DEPENDENCY_PAGE_SIZE", 50)
    page1 = [_dep_item(number=n, state="OPEN") for n in range(1, 51)]  # exactly 50 items
    fake = _FakeGhApiRunner({"blocked_by": {1: page1, 2: []}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert len(records) == 50
    # 50 == page size なので page 2 も問い合わせられる（境界: len(page) < page_size で停止）
    assert any("page=2" in c for c in fake.calls)


def test_given_101_items_when_default_page_size_100_then_pagination_boundary_returns_all_101(monkeypatch) -> None:
    page1 = [_dep_item(number=n, state="OPEN") for n in range(1, 101)]  # 100 items
    page2 = [_dep_item(number=101, state="OPEN")]  # 1 item
    fake = _FakeGhApiRunner({"blocked_by": {1: page1, 2: page2}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert len(records) == 101
    assert {r["number"] for r in records} == set(range(1, 102))


def test_given_100_items_when_default_page_size_100_then_pagination_boundary_single_page_and_stop(monkeypatch) -> None:
    page1 = [_dep_item(number=n, state="OPEN") for n in range(1, 101)]  # exactly 100 items
    fake = _FakeGhApiRunner({"blocked_by": {1: page1, 2: []}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert len(records) == 100
    assert any("page=2" in c for c in fake.calls)


# ------------------------------------------------------------
# AC9: blockedBy/blocking 方向・repository 不一致・duplicate・欠損フィールド
# ------------------------------------------------------------


def test_given_blocked_by_open_predecessor_when_direction_repo_resolved_then_wait_for_signal() -> None:
    """`blockedBy` の OPEN predecessor は current の停止理由になる。"""
    raw = {"blockedBy": [_typed_record(number=9449, state="OPEN")], "blocking": []}
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ("9449",)

    scope_pool = {"9449": IssueScope(title="p", number=9449, state="OPEN", allowed_paths=("a.py",))}
    result = _resolve_dependency(1462, ("a.py",), refs, scope_pool)
    assert result["blocking"] == {"issue_number": 9449, "state": "OPEN"}


def test_given_blocked_by_closed_predecessor_when_direction_and_repository_resolved_then_c2a_signal() -> None:
    """`blockedBy` の CLOSED predecessor は current の停止理由にならず C2a track に回る。"""
    raw = {"blockedBy": [_typed_record(number=9449, state="CLOSED")], "blocking": []}
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)

    scope_pool = {"9449": IssueScope(title="p", number=9449, state="CLOSED", allowed_paths=("a.py",))}
    result = _resolve_dependency(1462, ("a.py",), refs, scope_pool)
    assert result["blocking"] is None
    assert result["closed_predecessors"] == [9449]


def test_given_blocking_only_when_direction_and_repository_resolved_then_not_treated_as_current_stop_reason() -> None:
    """`blocking`（current が後続を止めているだけ）は current 自身の
    `blocked_by` refs には混入しない（方向を混同しない）。
    """
    raw = {"blockedBy": [], "blocking": [_typed_record(number=9600, state="OPEN")]}
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ()


def test_given_both_directions_present_when_direction_and_repository_resolved_then_only_blocked_by_feeds_refs() -> None:
    """`blockedBy` と `blocking` の両方が存在するケースでも、current の
    停止理由に使われるのは `blockedBy` のみ。
    """
    raw = {
        "blockedBy": [_typed_record(number=9449, state="OPEN")],
        "blocking": [_typed_record(number=9600, state="OPEN")],
    }
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ("9449",)


def test_given_cross_repository_dependency_when_direction_and_repository_resolved_then_excluded() -> None:
    """repository 不一致の native dependency は同一 repository 制約により
    除外される（別 repo の issue number を誤って解決しない）。
    """
    raw = {"blockedBy": [_typed_record(number=9449, state="OPEN", repository="other-owner/other-repo")]}
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ()


def test_given_duplicate_dependency_entries_when_direction_and_repository_resolved_then_deduplicated() -> None:
    """同一 number が重複して返っても dedup される。"""
    raw = {
        "blockedBy": [
            _typed_record(number=9449, state="OPEN"),
            _typed_record(number=9449, state="OPEN"),
        ]
    }
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ("9449",)


@pytest.mark.parametrize(
    "broken_item",
    [
        {"state": "OPEN", "repository": {"full_name": REPO}},  # missing number
        {"number": 9449, "repository": {"full_name": REPO}},  # missing state
        {"number": 9449, "state": "OPEN"},  # missing repository
    ],
)
def test_given_response_missing_required_field_when_direction_and_repository_resolved_then_fail_closed(
    broken_item, monkeypatch
) -> None:
    """AC9: API レスポンスに number/repository/state が欠けるケースは
    「依存なし」として黙って握りつぶさず fail-closed（`OverlapRuntimeError`）
    にする。
    """
    fake = _FakeGhApiRunner({"blocked_by": {1: [broken_item]}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    with pytest.raises(OverlapRuntimeError):
        fetch_native_dependencies(REPO, 1462, "blocked_by")


def test_given_non_array_response_when_fetch_page_then_fail_closed(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    with pytest.raises(OverlapRuntimeError):
        fetch_native_dependencies(REPO, 1462, "blocked_by")


# ------------------------------------------------------------
# AC6: 既知失敗パターン（gh issue view/list --json の未対応フィールド）
#      を使用していないことの静的確認
# ------------------------------------------------------------


def test_given_fetch_functions_when_source_inspected_then_no_unsupported_json_fields_used() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert '"number,title,body,labels,updatedAt,url,blockedBy,blocking"' not in source
    assert '"number,title,body,labels,updatedAt,url"' in source  # fetch_current_issue の既存 field list


# ------------------------------------------------------------
# AC11: live read-only smoke test（実 repository、`gh auth status` 依存）
# ------------------------------------------------------------


def test_live_smoke_read_only_native_dependency_fetch() -> None:
    """AC11: 実際の `squne121/loop-protocol` repository に対して native
    dependency 取得の read-only smoke 検証を行う。`gh auth status` が失敗する
    環境では SKIP（exit 77）とし、fixture ベースの AC7 検証で代替する
    （fallback の成功を本 AC の PASS に変換しない）。
    """
    auth_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    if auth_check.returncode != 0:
        # pytest.exit() は xdist worker process ごと落とし controller 側で
        # "INTERNALERROR: assert not crashitem" を引き起こす
        # (CI run 29172464382, job 86595777094 で確認済み)。
        # xdist worker 内では pytest.skip() を使う。
        pytest.skip(
            "SKIP: gh auth status unavailable; AC11 live smoke test skipped "
            "(fixture-based AC7 coverage still applies)"
        )

    result = fetch_all_native_dependencies(REPO, 1462)

    assert isinstance(result["blockedBy"], tuple)
    assert isinstance(result["blocking"], tuple)
    for direction_records in result.values():
        for record in direction_records:
            assert set(record) == {"repository", "number", "state"}
            assert record["repository"] == REPO

    artifacts_dir = Path(__file__).resolve().parents[4] / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifacts_dir / "ac11_native_dependency_live_smoke.log"
    log_path.write_text(
        "AC11 live smoke test (read-only)\n"
        f"repository={REPO} issue=1462\n"
        f"blockedBy_count={len(result['blockedBy'])}\n"
        f"blocking_count={len(result['blocking'])}\n"
        "pagination_completeness=verified_via_fetch_native_dependencies_full_page_loop\n",
        encoding="utf-8",
    )
    assert log_path.is_file()
