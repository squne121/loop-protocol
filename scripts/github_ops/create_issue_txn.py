#!/usr/bin/env python3
"""create_issue_txn.py

`create-issue` skill から呼ばれる Issue 起票ラッパー。
gh CLI を経由して GitHub に Issue を作成し、結果を ISSUE_AUTHOR_COVERAGE_V1 形式で stdout に返す。

特徴:
- body は file 経由 (`gh issue create --body-file`) でシェル展開バグを避ける
- template と必須セクション一致を起票前に検証 (fail-closed)
- idempotency: 同一タイトルが既に存在する場合はエラー終了 (status: failed)
- LOOP_PROTOCOL は pnpm + TypeScript プロジェクトだが、本スクリプトは
  外部依存ゼロの標準ライブラリ Python 3.10+ で実装している。

Usage:
    python3 scripts/github_ops/create_issue_txn.py \\
        --repo squne121/loop-protocol \\
        --template implementation \\
        --title "実装: 自機の境界クランプを追加する" \\
        --body-file tmp/issue-draft.md \\
        [--labels label1,label2] \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Issue Forms で定義された必須セクション（テンプレートと一致させる）
REQUIRED_SECTIONS: dict[str, list[str]] = {
    "implementation": ["背景", "目的", "受け入れ条件", "非ゴール", "テスト観点", "変更許可領域"],
    "research": ["背景", "調査目的", "調査範囲", "アウトプット形式"],
    "human-confirm": ["背景", "判断が必要な論点", "選択肢", "影響範囲"],
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create a GitHub Issue with contract validation")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--template", required=True, choices=list(REQUIRED_SECTIONS.keys()))
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True, type=Path)
    p.add_argument("--labels", default="", help="comma-separated labels")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def emit(payload: dict) -> None:
    payload.setdefault("generated_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    payload.setdefault("generated_by", "scripts/github_ops/create_issue_txn.py")
    print("ISSUE_AUTHOR_COVERAGE_V1:")
    for k, v in payload.items():
        if isinstance(v, list):
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")


def check_sections(body: str, template: str) -> tuple[list[str], list[str]]:
    """Issue 本文中の Markdown 見出しから必須セクションの存在を検証。

    Returns: (present, missing)
    """
    required = REQUIRED_SECTIONS[template]
    # 「### 背景」「## 背景」両方許容
    heading_pattern = re.compile(r"^#{2,3}\s+(\S.*?)\s*$", re.MULTILINE)
    found = {m.group(1).strip() for m in heading_pattern.finditer(body)}
    present = [s for s in required if s in found]
    missing = [s for s in required if s not in found]
    return present, missing


def title_already_exists(repo: str, title: str) -> bool:
    """同一タイトルの open Issue が既にあるか確認 (idempotency)。"""
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open", "--search", title, "--json", "title"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.SubprocessError as e:
        print(f"WARNING: gh issue list failed: {e}", file=sys.stderr)
        return False
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return any(item.get("title") == title for item in items)


def title_prefix_matches_template(title: str, template: str) -> bool:
    """title prefix と template の整合チェック。"""
    if template == "implementation":
        return title.startswith("実装:") or title.startswith("implement:")
    if template == "research":
        return title.startswith("調査:") or title.startswith("research:")
    if template == "human-confirm":
        return title.startswith("人間判断:") or title.startswith("human-confirm:")
    return True


def main() -> int:
    args = parse_args()

    if not args.body_file.exists():
        emit({"status": "failed", "errors": [f"body-file not found: {args.body_file}"]})
        return 2
    body = args.body_file.read_text(encoding="utf-8")

    if not title_prefix_matches_template(args.title, args.template):
        emit({
            "status": "failed",
            "errors": [
                f"title prefix が template ({args.template}) と一致しません。"
                f"title: {args.title!r}"
            ],
        })
        return 2

    present, missing = check_sections(body, args.template)
    if missing:
        emit({
            "status": "failed",
            "template": args.template,
            "required_sections_present": present,
            "required_sections_missing": missing,
            "errors": [f"必須セクションが不足: {missing}"],
        })
        return 2

    if title_already_exists(args.repo, args.title):
        emit({
            "status": "failed",
            "title": args.title,
            "errors": ["同一タイトルの open Issue が既に存在します (idempotency)"],
        })
        return 2

    if args.dry_run:
        emit({
            "status": "ok",
            "title": args.title,
            "template": args.template,
            "required_sections_present": present,
            "required_sections_missing": [],
            "url": None,
            "warnings": ["dry-run mode: gh issue create は実行していません"],
        })
        return 0

    cmd = ["gh", "issue", "create", "--repo", args.repo,
           "--title", args.title, "--body-file", str(args.body_file)]
    if args.labels:
        for label in args.labels.split(","):
            label = label.strip()
            if label:
                cmd.extend(["--label", label])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    except subprocess.CalledProcessError as e:
        emit({
            "status": "failed",
            "errors": [f"gh issue create failed: {e.stderr.strip()}"],
        })
        return 2
    url = result.stdout.strip()
    issue_number = url.rsplit("/", 1)[-1] if url else None

    emit({
        "status": "ok",
        "issue_number": int(issue_number) if issue_number and issue_number.isdigit() else None,
        "title": args.title,
        "template": args.template,
        "required_sections_present": present,
        "required_sections_missing": [],
        "url": url,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
