"""AC1/AC2/AC5 (#1493): GraphQL cursor pagination による overlap preflight
候補収集の全件性を検証する。

`check_implementation_overlap.py` を直接 import し、`_run_gh_json`（`gh api
graphql` 呼び出しの唯一の subprocess 境界）を monkeypatch でページ単位の
fixture に差し替えて、99件/100件境界/157件複数ページ/途中ページ失敗/
cursor 不整合/safety cap 到達/limit 超過境界/malformed response の
各シナリオを検証する（AC1/AC2、PR #1626 review fix_delta）。

AC5 のみ、実際の `squne121/loop-protocol` repository に対する read-only
smoke test。`gh auth status` が失敗する環境では SKIP（`pytest.skip`、CI
xdist worker crash 回避のため `pytest.exit` は使わない — 既存
`test_check_implementation_overlap_native_dependencies.py` の AC11 smoke
test と同じ理由）とし、fixture ベースの上記ユニットテストで代替する
（fallback 経由の成功を本 AC の PASS に変換しない）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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

_spec = importlib.util.spec_from_file_location("check_implementation_overlap_pagination", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = module
_spec.loader.exec_module(module)

fetch_implementation_candidates = module.fetch_implementation_candidates
OverlapRuntimeError = module.OverlapRuntimeError

REPO = "squne121/loop-protocol"


def _nodes(start: int, count: int) -> List[Dict[str, Any]]:
    return [
        {
            "number": n,
            "title": f"issue {n}",
            "body": "## Outcome\nx\n\n## In Scope\nx\n",
            "updatedAt": "2026-01-01T00:00:00Z",
            "url": f"https://github.com/{REPO}/issues/{n}",
        }
        for n in range(start, start + count)
    ]


def _graphql_payload(nodes: List[Dict[str, Any]], *, has_next_page: bool, end_cursor: Optional[str]) -> Dict[str, Any]:
    return {
        "data": {
            "repository": {
                "issues": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
                }
            }
        }
    }


def _install_pages(monkeypatch: pytest.MonkeyPatch, pages: List[Dict[str, Any]]) -> List[Any]:
    calls: List[Any] = []

    def fake_run_gh_json(args: Any) -> Any:
        calls.append(args)
        idx = len(calls) - 1
        if idx >= len(pages):
            raise AssertionError(f"unexpected extra gh call at page index {idx}: {args!r}")
        page = pages[idx]
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)
    return calls


def test_pagination_completes_below_page_size_with_has_next_page_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN 99 件（page size 未満）で hasNextPage=false
    WHEN fetch_implementation_candidates() を実行する
    THEN complete=True / saturated=False / fetched_count=99 / page_count=1。
    """
    pages = [_graphql_payload(_nodes(1, 99), has_next_page=False, end_cursor=None)]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 1000)

    assert len(candidates) == 99
    assert meta["complete"] is True
    assert meta["saturated"] is False
    assert meta["fetched_count"] == 99
    assert meta["page_count"] == 1
    assert meta["has_next_page"] is False
    assert meta["collection_mode"] == "exhaustive_cursor_pagination"


def test_pagination_completes_at_exactly_page_size_with_has_next_page_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN ちょうど 100 件（page size と同数）で hasNextPage=false
    WHEN fetch_implementation_candidates() を実行する
    THEN complete=True / saturated=False（#1493 の中核修正: 固定件数への
    到達だけを理由に saturated としない）。
    """
    pages = [_graphql_payload(_nodes(1, 100), has_next_page=False, end_cursor=None)]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 1000)

    assert len(candidates) == 100
    assert meta["complete"] is True
    assert meta["saturated"] is False
    assert meta["page_count"] == 1


def test_pagination_continues_past_page_size_when_has_next_page_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIVEN 1 ページ目がちょうど 100 件で hasNextPage=true
    WHEN fetch_implementation_candidates() を実行する
    THEN 2 ページ目を取得しに行く（100 件到達だけで停止しない）。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 5), has_next_page=False, end_cursor=None),
    ]
    calls = _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 1000)

    assert len(calls) == 2
    assert len(candidates) == 105
    assert meta["complete"] is True
    assert meta["saturated"] is False
    assert meta["page_count"] == 2


def test_pagination_collects_157_candidates_across_two_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1: 157 件を 2 ページ（100 + 57）で収集し、complete=True / saturated=False
    / fetched_count=157 を記録する。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 57), has_next_page=False, end_cursor=None),
    ]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 1000)

    assert len(candidates) == 157
    assert meta["fetched_count"] == 157
    assert meta["complete"] is True
    assert meta["saturated"] is False
    assert meta["page_count"] == 2


def test_pagination_second_page_api_failure_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: 2 ページ目の API 失敗は、完全な候補集合として扱わず例外で
    fail-closed にする。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        module.OverlapRuntimeError("gh command failed: gh api graphql (page 2)"),
    ]
    _install_pages(monkeypatch, pages)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 1000)


def test_pagination_cursor_pageinfo_inconsistency_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: hasNextPage=true なのに endCursor が欠落している場合、全件性を
    証明できないため fail-closed にする。
    """
    pages = [_graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor=None)]
    _install_pages(monkeypatch, pages)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 1000)


def test_pagination_safety_cap_reached_marks_saturated_not_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: 明示的な safety cap（`--limit`）到達時は、完全な候補集合として
    扱わず complete=False / saturated=True を返す（例外にはしない — 呼び出し
    側の route 判定へ fail-closed に委譲する）。PR #1626 review fix_delta の
    P1 Blocker 修正版: `fetched_count` は `--limit` を超えてはならない
    （旧テストは limit=150 で 200 件取得を許容する誤った挙動を固定していた）。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 50), has_next_page=True, end_cursor="cursor-2"),
    ]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 150)

    assert len(candidates) == 150
    assert meta["fetched_count"] == 150
    assert meta["complete"] is False
    assert meta["saturated"] is True
    assert meta["page_count"] == 2


def test_pagination_boundary_limit_150_total_149_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta 固定境界: limit=150 / total=149 → complete。"""
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 49), has_next_page=False, end_cursor=None),
    ]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 150)

    assert len(candidates) == 149
    assert meta["complete"] is True
    assert meta["saturated"] is False


def test_pagination_boundary_limit_150_total_150_has_next_page_false_is_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #1626 review fix_delta 固定境界:
    limit=150 / total=150 / hasNextPage=false → complete。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 50), has_next_page=False, end_cursor=None),
    ]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 150)

    assert len(candidates) == 150
    assert meta["complete"] is True
    assert meta["saturated"] is False


def test_pagination_boundary_limit_150_total_151_is_saturated_not_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #1626 review fix_delta 固定境界:
    limit=150 / total=151 → fetched_count=150 かつ saturated。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 50), has_next_page=True, end_cursor="cursor-2"),
    ]
    _install_pages(monkeypatch, pages)

    candidates, meta = fetch_implementation_candidates(REPO, 150)

    assert len(candidates) == 150
    assert meta["fetched_count"] == 150
    assert meta["complete"] is False
    assert meta["saturated"] is True


def test_pagination_api_returns_more_nodes_than_requested_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR #1626 review fix_delta: API が要求した page_size より多い node を
    返した場合は全件性を証明できないため runtime error にする。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        # 2 ページ目は remaining=50 のはずだが 100 件返す（違反）。
        _graphql_payload(_nodes(101, 100), has_next_page=False, end_cursor=None),
    ]
    _install_pages(monkeypatch, pages)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_pagination_top_level_graphql_errors_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: トップレベル GraphQL `errors` が非空なら
    不正・部分的な応答として fail-closed にする。
    """

    def fake_run_gh_json(args: Any) -> Any:
        return {"data": None, "errors": [{"message": "something went wrong"}]}

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_pagination_has_next_page_non_bool_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: hasNextPage が文字列等の非 bool の場合は
    安全に拒否する。
    """

    def fake_run_gh_json(args: Any) -> Any:
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": _nodes(1, 5),
                        "pageInfo": {"hasNextPage": "true", "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_pagination_has_next_page_missing_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: hasNextPage が欠落している場合は安全に
    拒否する。
    """

    def fake_run_gh_json(args: Any) -> Any:
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": _nodes(1, 5),
                        "pageInfo": {"endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_pagination_repeated_end_cursor_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: endCursor が直前ページと同一のまま
    hasNextPage=true の場合、無限ループ防止のため fail-closed にする。
    """
    pages = [
        _graphql_payload(_nodes(1, 100), has_next_page=True, end_cursor="cursor-1"),
        _graphql_payload(_nodes(101, 10), has_next_page=True, end_cursor="cursor-1"),
    ]
    _install_pages(monkeypatch, pages)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 1000)


def test_pagination_node_missing_required_key_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: node が object でも必須 key（例: url）を
    欠く場合は fail-closed にする。
    """
    bad_node = {"number": 1, "title": "x", "body": "y", "updatedAt": "2026-01-01T00:00:00Z"}

    def fake_run_gh_json(args: Any) -> Any:
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": [bad_node],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_pagination_node_not_object_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta: node が object でない場合は fail-closed
    にする。
    """

    def fake_run_gh_json(args: Any) -> Any:
        return {
            "data": {
                "repository": {
                    "issues": {
                        "nodes": ["not-a-dict"],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    with pytest.raises(OverlapRuntimeError):
        fetch_implementation_candidates(REPO, 150)


def test_cli_limit_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta P2 Blocker: `--limit 0` は argparse で拒否する。"""
    parser = module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--issue-number", "1", "--limit", "0"])


def test_cli_limit_rejects_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #1626 review fix_delta P2 Blocker: `--limit -5` は argparse で拒否する。"""
    parser = module.build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--issue-number", "1", "--limit", "-5"])


def test_cli_limit_accepts_positive_value() -> None:
    """PR #1626 review fix_delta P2 Blocker: 正の `--limit` は従来どおり通す。"""
    parser = module.build_arg_parser()
    parsed = parser.parse_args(["--issue-number", "1", "--limit", "42"])
    assert parsed.limit == 42


def test_run_cli_online_evidence_includes_collection_contract_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1/AC3: CLI online 経路（`run()`）が返す evidence の `source` に
    collection contract の additive フィールドが含まれる。
    """
    current_raw = {
        "number": 42,
        "title": "current",
        "body": "## Outcome\nx\n\n## In Scope\nx\n\n## Allowed Paths\n- foo.py\n",
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": f"https://github.com/{REPO}/issues/42",
    }

    def fake_fetch_current_issue(repo: str, issue_number: int) -> Dict[str, Any]:
        return dict(current_raw)

    def fake_fetch_all_native_dependencies(repo: str, issue_number: int) -> Dict[str, Any]:
        return {"blockedBy": (), "blocking": ()}

    pages = [_graphql_payload(_nodes(1, 5), has_next_page=False, end_cursor=None)]
    _install_pages(monkeypatch, pages)
    monkeypatch.setattr(module, "fetch_current_issue", fake_fetch_current_issue)
    monkeypatch.setattr(module, "fetch_all_native_dependencies", fake_fetch_all_native_dependencies)

    exit_code = module.run(["--issue-number", "42", "--repo", REPO])

    assert exit_code in (module.EXIT_OK, module.EXIT_RUNTIME_ERROR)


_RUNTIME_SMOKE_SCRIPT_PATH = (
    REPO_ROOT
    / ".claude"
    / "skills"
    / "implement-issue"
    / "scripts"
    / "verify_overlap_pagination_runtime.py"
)


def _load_runtime_smoke_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "verify_overlap_pagination_runtime", _RUNTIME_SMOKE_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    smoke_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = smoke_module
    spec.loader.exec_module(smoke_module)
    return smoke_module


def test_runtime_smoke_script_exit_77_when_gh_auth_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC5 / PR #1626 review fix_delta P1 Blocker: live smoke は独立
    スクリプト（`verify_overlap_pagination_runtime.py`）に分離し、実行環境
    不可時は `SKIP:` 出力 + exit 77 を返す（`pytest.skip()` は使わない —
    SKIP をテストスイート全体 green のまま通してしまうため）。ここでは
    その内部ロジックのみを unit test する（実 `gh` を呼ばない）。
    """
    smoke = _load_runtime_smoke_module()
    monkeypatch.setattr(smoke, "ARTIFACT_PATH", tmp_path / "smoke.json")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=1),
    )

    exit_code = smoke.main()

    assert exit_code == 77
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    assert payload["skip_reason"] == "gh auth status unavailable"


def test_runtime_smoke_script_exit_0_when_boundaries_satisfied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #1626 review fix_delta: boundary 条件を満たせば exit 0 で
    artifact に PASS 相当の内容を記録する。
    """
    smoke = _load_runtime_smoke_module()
    monkeypatch.setattr(smoke, "ARTIFACT_PATH", tmp_path / "smoke.json")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )
    monkeypatch.setattr(
        smoke,
        "fetch_implementation_candidates",
        lambda repo, limit: (
            [{} for _ in range(157)],
            {
                "fetched_count": 157,
                "page_count": 2,
                "has_next_page": False,
                "complete": True,
                "saturated": False,
            },
        ),
    )

    exit_code = smoke.main()

    assert exit_code == 0
    payload = json.loads((tmp_path / "smoke.json").read_text(encoding="utf-8"))
    assert payload["fetched_count"] == 157
    assert payload["complete"] is True
    assert payload["saturated"] is False


def test_runtime_smoke_script_exit_1_when_boundaries_not_satisfied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #1626 review fix_delta: fallback / 部分取得を PASS に変換しない
    （boundary 条件を満たさなければ exit 1）。
    """
    smoke = _load_runtime_smoke_module()
    monkeypatch.setattr(smoke, "ARTIFACT_PATH", tmp_path / "smoke.json")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )
    monkeypatch.setattr(
        smoke,
        "fetch_implementation_candidates",
        lambda repo, limit: (
            [{} for _ in range(50)],
            {
                "fetched_count": 50,
                "page_count": 1,
                "has_next_page": False,
                "complete": True,
                "saturated": False,
            },
        ),
    )

    exit_code = smoke.main()

    assert exit_code == 1


def test_runtime_smoke_script_exit_1_when_overlap_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PR #1626 review fix_delta: 収集中の `OverlapRuntimeError` を fallback
    経由の成功に変換せず exit 1 とする。
    """
    smoke = _load_runtime_smoke_module()
    monkeypatch.setattr(smoke, "ARTIFACT_PATH", tmp_path / "smoke.json")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(args=[], returncode=0),
    )

    def _raise(repo: str, limit: int) -> Any:
        raise smoke.OverlapRuntimeError("boom")

    monkeypatch.setattr(smoke, "fetch_implementation_candidates", _raise)

    exit_code = smoke.main()

    assert exit_code == 1
