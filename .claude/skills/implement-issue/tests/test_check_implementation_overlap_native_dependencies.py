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
    # 公式 REST issue-dependencies endpoint の実レスポンス形。repository
    # identity は nested `repository: {"full_name": ...}` object ではなく
    # `repository_url`（例: "https://api.github.com/repos/{owner}/{repo}"）
    # として返る（https://docs.github.com/en/rest/issues/issue-dependencies、
    # PR #1474 レビュー Blocker 1 で判明）。squne121/loop-protocol に対する
    # 実際の `gh api repos/squne121/loop-protocol/issues/1470/dependencies/blocking`
    # 応答で repository_url 形であることを実証確認済み。
    return {
        "number": number,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repository}",
    }


def _dep_item_legacy_fictional_nested_repository(
    *, number: int, state: str, repository: str = REPO
) -> Dict[str, Any]:
    # Blocker 1 回帰用: 初稿実装が誤って前提としていた、公式 API には存在
    # しない架空の nested `repository: {"full_name": ...}` 形。この形は
    # `repository_url` を持たないため、typed record として受理されず
    # fail-closed になるべきである（下記の regression test 参照）。
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


def test_given_cross_repository_dependency_when_direction_and_repository_resolved_then_unresolved_fail_closed() -> None:
    """repository 不一致の OPEN native dependency は同一 repository の Issue
    番号へ誤結合せず、namespaced ref のまま unresolved として fail-closed にする。
    """
    raw = {"blockedBy": [_typed_record(number=9449, state="OPEN", repository="other-owner/other-repo")]}
    refs = _merge_dependency_refs("", raw, "blocked_by", current_repo=REPO)
    assert refs == ("other-owner/other-repo#9449",)

    # 同一 repository に同じ番号が存在しても cross-repository predecessor と
    # 誤結合しない。scope を比較できない OPEN predecessor は human review を
    # 要する unresolved dependency として evidence 化される。
    scope_pool = {"9449": IssueScope(title="local", number=9449, state="OPEN", allowed_paths=("a.py",))}
    result = _resolve_dependency(1462, ("a.py",), refs, scope_pool)
    assert result["blocking"] is None
    assert result["closed_predecessors"] == []
    assert result["unresolved_refs"] == ["other-owner/other-repo#9449"]


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
# Blocker 1 (PR #1474 レビュー): 公式 REST 応答 schema（repository_url）を
# 正として解析し、実装に都合のよい架空の nested repository object は
# 拒否することを検証する regression test。
# ------------------------------------------------------------


def test_given_official_repository_url_shape_when_normalized_then_typed_record_extracted() -> None:
    """公式ドキュメント（https://docs.github.com/en/rest/issues/issue-dependencies）
    どおりの `repository_url` 形（nested `repository.full_name` ではない）を
    typed record へ正規化できる。
    """
    item = {
        "number": 1458,
        "state": "open",
        "repository_url": "https://api.github.com/repos/squne121/loop-protocol",
    }
    record = _normalize_native_dependency_record(item, direction="blocked_by")
    assert record == {"repository": REPO, "number": 1458, "state": "OPEN"}


def test_given_fictional_nested_repository_object_when_normalized_then_fail_closed() -> None:
    """初稿実装が誤って前提としていた、公式 API に存在しない架空の nested
    `repository: {"full_name": ...}` 形（`repository_url` を持たない）は
    「依存なし」として黙って握りつぶされず fail-closed になる。
    """
    item = _dep_item_legacy_fictional_nested_repository(number=1458, state="open")
    with pytest.raises(OverlapRuntimeError):
        _normalize_native_dependency_record(item, direction="blocked_by")


def test_given_non_empty_official_shape_fixture_when_fetched_then_pagination_and_schema_both_verified(
    monkeypatch,
) -> None:
    """Blocker 1: 空配列 smoke と非空 schema smoke を別テストにする（レビュー
    必須修正）。公式 shape の非空レスポンスに対し pagination ループと schema
    解析の両方が実際に動作することを検証する。
    """
    fake = _FakeGhApiRunner(
        {"blocked_by": {1: [_dep_item(number=1458, state="open")]}}
    )
    monkeypatch.setattr(module.subprocess, "run", fake)

    records = fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert records == ({"repository": REPO, "number": 1458, "state": "OPEN"},)


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://api.github.com/repos/only-owner",
        "https://example.com/repos/squne121/loop-protocol",
        "not-a-url",
        None,
        123,
    ],
)
def test_given_malformed_repository_url_when_normalized_then_fail_closed(malformed_url) -> None:
    item = {"number": 1458, "state": "open", "repository_url": malformed_url}
    with pytest.raises(OverlapRuntimeError):
        _normalize_native_dependency_record(item, direction="blocked_by")


# ------------------------------------------------------------
# Major 3 (PR #1474 レビュー): state enum 検証 + type(number) is int
# ------------------------------------------------------------


@pytest.mark.parametrize("bad_state", ["UNKNOWN", "MERGED", 123, None, ""])
def test_given_malformed_state_when_normalized_then_fail_closed(bad_state) -> None:
    """`state` が `open`/`closed`（大小文字非依存）以外の場合は malformed
    values を CLOSED として扱わず fail-closed にする。
    """
    item = {
        "number": 1458,
        "state": bad_state,
        "repository_url": "https://api.github.com/repos/squne121/loop-protocol",
    }
    with pytest.raises(OverlapRuntimeError):
        _normalize_native_dependency_record(item, direction="blocked_by")


@pytest.mark.parametrize("bad_number", [True, False, "1458", 1.5, -1, 0, None])
def test_given_invalid_number_type_when_normalized_then_fail_closed(bad_number) -> None:
    """`isinstance(number, int)` だけでは Python の `True`/`False` も
    整数として通るため、`type(number) is int` による厳密な型検証を行う。
    """
    item = {
        "number": bad_number,
        "state": "open",
        "repository_url": "https://api.github.com/repos/squne121/loop-protocol",
    }
    with pytest.raises(OverlapRuntimeError):
        _normalize_native_dependency_record(item, direction="blocked_by")


# ------------------------------------------------------------
# Major 4 (PR #1474 レビュー): Accept / X-GitHub-Api-Version header 固定
# ------------------------------------------------------------


def test_given_native_dependency_page_fetch_when_gh_api_invoked_then_version_headers_pinned(
    monkeypatch,
) -> None:
    fake = _FakeGhApiRunner({"blocked_by": {1: []}})
    monkeypatch.setattr(module.subprocess, "run", fake)

    fetch_native_dependencies(REPO, 1462, "blocked_by")

    assert fake.calls, "expected at least one gh api call"
    call_args = fake.calls[0]
    assert "-H" in call_args
    assert "Accept: application/vnd.github+json" in call_args
    assert any(a.startswith("X-GitHub-Api-Version:") for a in call_args)


# ------------------------------------------------------------
# Blocker 2 (PR #1474 レビュー): candidate 側の native dependency 取得
# ------------------------------------------------------------


def test_given_candidate_native_blocked_by_when_scope_built_then_successor_relation_visible() -> None:
    """candidate が current に native dependency で blocked by されている
    場合（successor 関係）、candidate の raw dict に `blockedBy` typed record
    が付与されていれば `_issue_scope_from_raw` が `depends_on` に current の
    番号を含める（`_dependency_relation` が successor と判定できる入力に
    なる）。これは run() の Blocker 2 修正（overlap 候補への native
    dependency 取得）が実際に candidate raw へ書き込む形と同一である。
    """
    cand_raw = {
        "number": 2001,
        "title": "candidate",
        "body": "## Allowed Paths\n\n- a.py\n",
        "labels": [],
        "updatedAt": "2026-07-01T00:00:00Z",
        "url": "https://github.com/squne121/loop-protocol/issues/2001",
        "state": "OPEN",
        "blockedBy": [_typed_record(number=1462, state="OPEN")],
        "blocking": [],
    }
    scope = module._issue_scope_from_raw(cand_raw, current_repo=REPO)
    assert "1462" in scope.depends_on



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



# ------------------------------------------------------------
# Blocker 2 (PR #1474 レビュー): run() online 経路が readback 対象 candidate
# にも native dependency を取得することを end-to-end で検証する（二段階
# 取得: 全 candidate 無条件ではなく readback 対象のみ）。
# ------------------------------------------------------------


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "overlap"

def _fake_source_metadata(count: int, *, saturated: bool = False) -> dict:
    return {
        "collection_mode": "exhaustive_cursor_pagination",
        "page_size": 100,
        "page_count": 1,
        "fetched_count": count,
        "has_next_page": saturated,
        "complete": not saturated,
        "saturated": saturated,
    }



def test_given_online_run_when_readback_candidate_exists_then_native_dependencies_fetched_for_it_only(
    monkeypatch, capsys
) -> None:
    """online C1: current の `blocking` は後続 Issue であり、predecessor
    ではない。`current_1451_analog` と path-only false-positive candidates を
    使い、#9449 の candidate native fetch を含む二段階 readback 後も
    `proceed_with_collision_evidence` を維持する。
    """
    current_raw = json.loads((_FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))
    candidates_raw = json.loads(
        (_FIXTURES_DIR / "candidates_path_only_false_positive.json").read_text(encoding="utf-8")
    )

    fetch_calls: List[int] = []

    def fake_fetch_current_issue(repo, issue_number):
        assert repo == REPO
        return dict(current_raw)

    def fake_fetch_implementation_candidates(repo, limit):
        return list(candidates_raw), _fake_source_metadata(len(candidates_raw))

    def fake_fetch_all_native_dependencies(repo, issue_number):
        fetch_calls.append(issue_number)
        if issue_number == 9451:
            return {
                "blockedBy": (),
                "blocking": ({"repository": REPO, "number": 9600, "state": "OPEN"},),
            }
        return {"blockedBy": (), "blocking": ()}

    def fail_fetch_predecessor_issue(repo, issue_number):
        pytest.fail(
            "blocking-only native dependency must not fall through to predecessor readback: "
            f"{repo}#{issue_number}"
        )

    monkeypatch.setattr(module, "fetch_current_issue", fake_fetch_current_issue)
    monkeypatch.setattr(module, "fetch_issue_comments", lambda repo, issue_number: [])
    monkeypatch.setattr(module, "fetch_implementation_candidates", fake_fetch_implementation_candidates)
    monkeypatch.setattr(module, "fetch_all_native_dependencies", fake_fetch_all_native_dependencies)
    monkeypatch.setattr(module, "fetch_predecessor_issue", fail_fetch_predecessor_issue)

    exit_code = module.run(["--issue-number", "9451", "--repo", REPO])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert exit_code == 0, payload
    assert payload["route"] == "proceed_with_collision_evidence", payload
    # current issue（9451）は `blockedBy=[]` / `blocking=[#9600 OPEN]` として
    # 取得されるが、後者は predecessor readback に流れてはならない。
    assert fetch_calls[0] == 9451
    assert payload["dependency_resolution"]["blocked_by_refs"] == []
    assert payload["dependency_resolution"]["blocking_predecessor"] is None
    assert payload["dependency_resolution"]["native_blocking"] == [
        {"repository": REPO, "number": 9600, "state": "OPEN"}
    ]
    # readback 対象になった candidate（false positive fixture では 9449 は
    # 自己除外されず、Allowed Paths が重複するが Outcome/In Scope が disjoint
    # な候補として readback される）にも native dependency 取得が行われる。
    readback_numbers = {c["issue_number"] for c in payload["candidates"]}
    assert readback_numbers, "expected at least one readback candidate"
    assert readback_numbers.issubset(set(fetch_calls[1:]))
    assert 9449 in readback_numbers
    assert 9449 in fetch_calls[1:]
    assert "native_dependency_candidates_fetched" in payload["dependency_resolution"]
    assert set(payload["dependency_resolution"]["native_dependency_candidates_fetched"]) == readback_numbers


def test_given_online_run_when_no_readback_candidate_then_native_dependencies_fetched_only_for_current(
    monkeypatch, capsys
) -> None:
    current_raw = json.loads((_FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))

    fetch_calls: List[int] = []

    def fake_fetch_current_issue(repo, issue_number):
        return dict(current_raw)

    def fake_fetch_implementation_candidates(repo, limit):
        return [], _fake_source_metadata(0)

    def fake_fetch_all_native_dependencies(repo, issue_number):
        fetch_calls.append(issue_number)
        return {"blockedBy": (), "blocking": ()}

    monkeypatch.setattr(module, "fetch_current_issue", fake_fetch_current_issue)
    monkeypatch.setattr(module, "fetch_issue_comments", lambda repo, issue_number: [])
    monkeypatch.setattr(module, "fetch_implementation_candidates", fake_fetch_implementation_candidates)
    monkeypatch.setattr(module, "fetch_all_native_dependencies", fake_fetch_all_native_dependencies)

    exit_code = module.run(["--issue-number", "9451", "--repo", REPO])
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert exit_code == 0, payload
    assert payload["route"] == "proceed", payload
    assert fetch_calls == [9451]
    assert "native_dependency_candidates_fetched" not in payload["dependency_resolution"]


def test_given_online_run_when_current_only_blocks_open_dependent_then_route_does_not_wait(
    monkeypatch, capsys
) -> None:
    """`blocking` は current が止めている後続 Issue であり、current の
    predecessor ではない。online 経路でも C2b / human review に混入させない。
    """
    current_raw = json.loads((_FIXTURES_DIR / "current_1451_analog.json").read_text(encoding="utf-8"))

    def fake_fetch_current_issue(repo, issue_number):
        assert repo == REPO
        assert issue_number == 9451
        return dict(current_raw)

    def fake_fetch_implementation_candidates(repo, limit):
        return [], _fake_source_metadata(0)

    def fake_fetch_all_native_dependencies(repo, issue_number):
        assert repo == REPO
        assert issue_number == 9451
        return {
            "blockedBy": (),
            "blocking": ({"repository": REPO, "number": 9600, "state": "OPEN"},),
        }

    monkeypatch.setattr(module, "fetch_current_issue", fake_fetch_current_issue)
    monkeypatch.setattr(module, "fetch_issue_comments", lambda repo, issue_number: [])
    monkeypatch.setattr(module, "fetch_implementation_candidates", fake_fetch_implementation_candidates)
    monkeypatch.setattr(module, "fetch_all_native_dependencies", fake_fetch_all_native_dependencies)

    exit_code = module.run(["--issue-number", "9451", "--repo", REPO])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["route"] == "proceed", payload
    assert payload["dependency_resolution"]["blocked_by_refs"] == []
    assert payload["dependency_resolution"]["blocking_predecessor"] is None
    assert payload["dependency_resolution"]["native_blocking"] == [
        {"repository": REPO, "number": 9600, "state": "OPEN"}
    ]


# ------------------------------------------------------------
# #1621 AC1: successor index construction from current's own native
# `blocking` (dead data until now), injected before the FIRST
# classify_overlap() call -- no per-candidate API calls required.
# ------------------------------------------------------------


def test_current_native_successor_index_matches_same_repository_only() -> None:
    """#1621 AC7 (PR #1637 レビュー P2 Conditional): index は
    `(repository, issue_number)` タプルの frozenset として返る（number だけ
    へ潰さない）。
    """
    current_raw = {
        "blocking": [
            _typed_record(number=2001, state="OPEN"),
            _typed_record(number=2002, state="OPEN", repository="other-owner/other-repo"),
            {"number": 2003, "state": "OPEN"},  # missing repository -> ignored
            "not-a-dict",
            {"number": True, "state": "OPEN", "repository": REPO},  # bool number -> ignored
        ],
    }
    index = module._current_native_successor_index(current_raw, REPO)
    assert index == frozenset({(REPO, 2001)})


def test_current_native_successor_index_empty_when_blocking_missing_or_not_list() -> None:
    assert module._current_native_successor_index({}, REPO) == frozenset()
    assert module._current_native_successor_index({"blocking": "not-a-list"}, REPO) == frozenset()
    assert module._current_native_successor_index({"blocking": None}, REPO) == frozenset()


def test_current_native_successor_index_tuple_does_not_match_different_repository_same_number() -> None:
    """#1621 AC7 (PR #1637 レビュー P2 Conditional): 返り値がタプルであることの
    直接的な回帰確認。同一 issue number でも repository が異なれば
    membership check は False になる（number だけの frozenset に潰していた
    場合はこの区別ができない）。
    """
    current_raw = {"blocking": [_typed_record(number=5000, state="OPEN", repository=REPO)]}
    index = module._current_native_successor_index(current_raw, REPO)
    assert (REPO, 5000) in index
    assert ("other-owner/other-repo", 5000) not in index


def test_given_online_run_when_current_native_blocking_shared_parent_candidate_then_successor_c2a_without_extra_calls(
    monkeypatch, capsys
) -> None:
    """#1621 AC1/AC3/AC4（PR #1637 レビュー P1 Blocker 修正版）: current の
    native blocking のみから successor index を構築し、最初の
    classify_overlap() 呼び出し前に candidate の depends_on へ current 番号を
    注入する。candidate 自身は blockedBy を持たず、shared parent_refs だけを
    持つ（旧実装では parent_child_collision により human_review_required に
    停止していたケース）。fix 後は proceed_with_collision_evidence / C2a に
    なる。

    P1 Blocker: successor 関係が current の native blocking から確定済みの
    candidate に対しては、第二段階の readback（`fetch_all_native_dependencies`
    経由の候補側 blockedBy/blocking hydration）を一切実行しない
    （「candidate 単位の追加 API 呼び出しゼロ」という Issue #1621 の中核
    契約）。下位の dependency endpoint（`blocked_by`/`blocking` それぞれ）
    単位で呼び出し回数を固定し、current 自身の blocked_by 1 回・blocking 1
    回のみで、candidate 側は 0 回であることを検証する。
    """
    current_number = 9700
    candidate_number = 9701

    def _body(*, parent_issue: str, goal_ref: str, outcome: str) -> str:
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
                "- docs/dev/successor_shared.md",
                "",
                "## Allowed Paths",
                "",
                "- docs/dev/successor_shared.md",
                "",
            ]
        )

    current_raw = {
        "number": current_number,
        "title": "実装: current side",
        "body": _body(
            parent_issue="#9690", goal_ref="current goal alpha", outcome="current outcome about alpha beta gamma."
        ),
        "updatedAt": "2026-07-19T00:00:00Z",
        "url": f"https://github.com/{REPO}/issues/{current_number}",
    }
    candidate_raw = {
        "number": candidate_number,
        "title": "実装: candidate side",
        "body": _body(
            parent_issue="#9690",
            goal_ref="candidate goal beta",
            outcome="candidate outcome about delta epsilon zeta.",
        ),
        "labels": [{"name": "phase/implementation"}],
        "updatedAt": "2026-07-19T00:05:00Z",
        "url": f"https://github.com/{REPO}/issues/{candidate_number}",
        "state": "OPEN",
        # 注意: blockedBy は意図的に存在しない（AC1/AC2 の検証対象）
    }

    fetch_native_dependency_calls: List[Any] = []

    def fake_fetch_current_issue(repo, issue_number):
        assert repo == REPO
        return dict(current_raw)

    def fake_fetch_implementation_candidates(repo, limit):
        return [dict(candidate_raw)], {
            "collection_mode": "exhaustive_cursor_pagination",
            "page_size": 100,
            "page_count": 1,
            "fetched_count": 1,
            "has_next_page": False,
            "complete": True,
            "saturated": False,
        }

    def fake_fetch_native_dependencies(repo, issue_number, direction):
        # P1 Blocker: 下位の dependency endpoint（blocked_by / blocking）
        # 単位で呼び出しを記録する。`fetch_all_native_dependencies` はこの
        # 関数を経由するため、モックしない実物の `fetch_all_native_dependencies`
        # がここへ到達する呼び出し回数を直接検証できる。
        assert repo == REPO
        fetch_native_dependency_calls.append((issue_number, direction))
        if issue_number == current_number and direction == "blocking":
            return ({"repository": REPO, "number": candidate_number, "state": "OPEN"},)
        return ()

    def fail_fetch_predecessor_issue(repo, issue_number):
        pytest.fail(f"unexpected predecessor readback: {repo}#{issue_number}")

    monkeypatch.setattr(module, "fetch_current_issue", fake_fetch_current_issue)
    monkeypatch.setattr(module, "fetch_issue_comments", lambda repo, issue_number: [])
    monkeypatch.setattr(module, "fetch_implementation_candidates", fake_fetch_implementation_candidates)
    monkeypatch.setattr(module, "fetch_native_dependencies", fake_fetch_native_dependencies)
    monkeypatch.setattr(module, "fetch_predecessor_issue", fail_fetch_predecessor_issue)

    exit_code = module.run(["--issue-number", str(current_number), "--repo", REPO])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["route"] == "proceed_with_collision_evidence", payload
    assert len(payload["candidates"]) == 1
    cand_evidence = payload["candidates"][0]
    assert cand_evidence["issue_number"] == candidate_number
    assert cand_evidence["policy_class"] == "C2a", payload
    assert "successor_dependency_ordering" in cand_evidence["reasons"], payload

    # P2 Major: candidate evidence に dependency_relation / dependency_provenance
    # が保存され、current の native blocking から証明された successor で
    # あることが監査可能になっている。
    assert cand_evidence["dependency_relation"] == "successor", payload
    assert cand_evidence["dependency_provenance"] == [
        {"source": "current_native_blocking", "repository": REPO, "issue_number": current_number}
    ], payload

    # P1 Blocker: candidate 側への native dependency hydration（第二段階の
    # readback）は一切発生しない。
    assert "native_dependency_candidates_fetched" not in payload["dependency_resolution"], payload
    assert fetch_native_dependency_calls.count((current_number, "blocked_by")) == 1
    assert fetch_native_dependency_calls.count((current_number, "blocking")) == 1
    assert fetch_native_dependency_calls.count((candidate_number, "blocked_by")) == 0
    assert fetch_native_dependency_calls.count((candidate_number, "blocking")) == 0
    assert fetch_native_dependency_calls == [
        (current_number, "blocked_by"),
        (current_number, "blocking"),
    ], fetch_native_dependency_calls
