#!/usr/bin/env python3
"""verify_overlap_pagination_runtime.py

AC5（#1493、PR #1626 review fix_delta）専用の read-only live runtime smoke。

`docs/dev/runtime-verification-policy.md` の `decision: immediate` 要件に
従い、実行環境不可時（`gh auth status` 失敗）は `SKIP:` を stderr へ出力し
exit 77 を返す（SKIP は PASS ではない）。`pytest.skip()` はテストスイート
全体を green のまま通してしまうため、本 script は pytest から独立して実行
される想定であり、内部ロジックのみ unit test で検証する
（`test_check_implementation_overlap_pagination.py` 参照）。

fixture / stub / cached evidence による代替成功は PASS として扱わない
（`fetch_implementation_candidates()` の例外は fallback せず exit 1 で
fail-closed にする）。

Usage:
    uv run python3 .claude/skills/implement-issue/scripts/verify_overlap_pagination_runtime.py

Exit codes:
    0  PASS  — fetched_count > 100 かつ complete=true かつ saturated=false
               かつ has_next_page=false
    1  FAIL  — 収集に成功したが boundary 条件を満たさない、または収集自体が
               失敗した（OverlapRuntimeError）
    77 SKIP  — `gh auth status` が失敗した（実行環境不可）
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_FILE = Path(__file__).resolve()
_SCRIPTS_DIR = _THIS_FILE.parent
_REPO_ROOT = _SCRIPTS_DIR.parents[3]

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from check_implementation_overlap import (  # noqa: E402
    OverlapRuntimeError,
    fetch_implementation_candidates,
)

REPO = "squne121/loop-protocol"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_SKIP = 77

ARTIFACT_PATH = _REPO_ROOT / "artifacts" / "issue-1493-overlap-pagination-smoke.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_artifact(payload: dict) -> None:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    timestamp = _now()

    auth_check = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    if auth_check.returncode != 0:
        _write_artifact(
            {
                "timestamp": timestamp,
                "repo": REPO,
                "skip_reason": "gh auth status unavailable",
            }
        )
        print("SKIP: gh auth status unavailable; AC5 live smoke test skipped", file=sys.stderr)
        return EXIT_SKIP

    try:
        candidates, meta = fetch_implementation_candidates(REPO, 5000)
    except OverlapRuntimeError as exc:
        _write_artifact(
            {
                "timestamp": timestamp,
                "repo": REPO,
                "skip_reason": None,
                "error": str(exc),
            }
        )
        print(f"FAIL: fetch_implementation_candidates raised OverlapRuntimeError: {exc}", file=sys.stderr)
        return EXIT_FAIL

    payload = {
        "timestamp": timestamp,
        "repo": REPO,
        "fetched_count": meta["fetched_count"],
        "page_count": meta["page_count"],
        "has_next_page": meta["has_next_page"],
        "complete": meta["complete"],
        "saturated": meta["saturated"],
        "route": "not_evaluated_by_this_smoke_test",
        "skip_reason": None,
    }
    _write_artifact(payload)

    ok = (
        meta["fetched_count"] > 100
        and meta["complete"] is True
        and meta["saturated"] is False
        and meta["has_next_page"] is False
    )
    if not ok:
        print(f"FAIL: AC5 boundary assertions failed: {meta!r}", file=sys.stderr)
        return EXIT_FAIL

    print(
        f"PASS: fetched_count={meta['fetched_count']} complete={meta['complete']} "
        f"saturated={meta['saturated']}"
    )
    return EXIT_PASS


if __name__ == "__main__":
    raise SystemExit(main())
