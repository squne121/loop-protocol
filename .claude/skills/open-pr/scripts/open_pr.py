#!/usr/bin/env python3
"""open_pr.py — open-pr skill の Python wrapper.

LOOP_PROTOCOL の PR 起票を決定論的に行う。skill (SKILL.md) の手順を実装する:
- publish ゲート (人間承認)
- PR 本文 Template Guard (必須セクション存在確認)
- Linked Issue 状態確認 + Closes / Refs 自動 downgrade
- Idempotency チェック (既存 PR 検出)
- gh pr create 実行
- KEY=VALUE stdout contract

外部依存ゼロ (stdlib のみ)。`uv run python3` 経由で実行。

Usage:
    uv run python3 .claude/skills/open-pr/scripts/open_pr.py \\
        --pr-title "feat(systems): MovementSystem に境界クランプを追加" \\
        --linked-issue 42 \\
        --publish yes \\
        --pr-body-file /tmp/pr-body.md \\
        [--draft true] [--branch <name>] [--dry-run] [--repo <owner>/<repo>]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# LOOP_PROTOCOL implement-issue が生成する PR 本文の最小必須セクション
REQUIRED_PR_SECTIONS = [
    "Summary",
    "受け入れ条件の達成状況",
    "検証コマンド結果",
    "Allowed Paths 遵守",
]

# safety-sensitive PR の判定パターン（changed path の部分一致）
SAFETY_SENSITIVE_PATH_PATTERNS = [
    "transport",
    "permission",
    "sandbox",
    "auth",
    "mcp",
    ".claude/skills/",
    ".github/workflows/",
]

# Safety Claim Matrix の必須列ヘッダー
SAFETY_CLAIM_MATRIX_REQUIRED_COLS = [
    "Claim",
    "Implemented?",
    "Not controlled",
    "Evidence",
    "Follow-up",
]

# エラーコード（skill SKILL.md と一致させる）
E_APPROVAL_MISSING = "E_APPROVAL_MISSING"
E_PR_TEMPLATE_GUARD = "E_PR_TEMPLATE_GUARD"
E_SAFETY_CLAIM_MATRIX_MISSING = "E_SAFETY_CLAIM_MATRIX_MISSING"
E_LINKED_ISSUE_STATE_UNKNOWN = "E_LINKED_ISSUE_STATE_UNKNOWN"
E_GH_FAILURE = "E_GH_FAILURE"


def is_safety_sensitive(changed_paths: list[str]) -> bool:
    """changed paths のいずれかが safety-sensitive パターンに部分一致するか確認。"""
    for path in changed_paths:
        for pattern in SAFETY_SENSITIVE_PATH_PATTERNS:
            if pattern in path:
                return True
    return False


def check_safety_claim_matrix(body: str) -> list[str]:
    """Safety Claim Matrix セクションと必須列の存在を確認。問題リストを返す。

    Returns:
        空リスト: 問題なし
        非空リスト: 問題の説明一覧
    """
    issues: list[str] = []

    # Safety Claim Matrix セクションの存在確認
    if not re.search(r"^##\s+Safety Claim Matrix\b", body, re.MULTILINE):
        issues.append("## Safety Claim Matrix セクションが存在しません")
        return issues  # セクションがなければ列チェックは不要

    # 必須列ヘッダーの存在確認
    for col in SAFETY_CLAIM_MATRIX_REQUIRED_COLS:
        if col not in body:
            issues.append(f"Safety Claim Matrix に必須列 '{col}' が見つかりません")

    # Not controlled が非空の場合、Follow-up に Issue 番号（#N）が必要
    # テーブル行から Not controlled と Follow-up 列を抽出して確認
    # テーブル行のパターン: | ... | ... | <not_controlled> | ... | <follow-up> |
    # ヘッダー行とセパレータ行をスキップし、データ行のみ確認
    table_section = re.search(
        r"\|.*Claim.*\|.*Implemented.*\|.*Not controlled.*\|.*Evidence.*\|.*Follow-up.*\|(.*?)(?=\n##|\Z)",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    if table_section:
        table_text = table_section.group(0)
        # ヘッダー行とセパレータ行（---|---）以外のデータ行を解析
        for line in table_text.splitlines():
            if not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5:
                continue
            # セパレータ行をスキップ
            if re.match(r"^[-\s]+$", cells[0]):
                continue
            # ヘッダー行をスキップ
            if cells[0].lower() in ("claim",):
                continue
            not_controlled = cells[2] if len(cells) > 2 else ""
            follow_up = cells[4] if len(cells) > 4 else ""
            # Not controlled が空でなく（N/A や空文字でない）、Follow-up に #N がない場合
            if not_controlled and not_controlled.lower() not in ("", "n/a", "-"):
                if not re.search(r"#\d+", follow_up):
                    issues.append(
                        f"Not controlled が非空の行（'{not_controlled[:30]}...' 等）に "
                        f"Follow-up の open Issue 番号（#N 形式）が必要です"
                    )
                    break  # 1 件報告すれば十分

    return issues


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Open PR (LOOP_PROTOCOL open-pr skill wrapper)")
    p.add_argument("--pr-title", required=True)
    p.add_argument("--linked-issue", required=True, type=int)
    p.add_argument("--publish", required=True, help="`yes` で人間承認確認")
    p.add_argument("--pr-body-file", required=True, type=Path)
    p.add_argument("--draft", default="true", help="`true` (default) で Draft PR")
    p.add_argument("--branch", help="head branch 名 (省略時は現在の HEAD)")
    p.add_argument("--repo", help="owner/repo (省略時は git remote から取得)")
    p.add_argument("--dry-run", action="store_true", help="gh pr create を実行しない")
    p.add_argument(
        "--changed-paths",
        nargs="*",
        default=[],
        help="変更ファイルパスのリスト（safety-sensitive 判定に使用）。省略時は Safety Claim Matrix Guard をスキップ",
    )
    return p.parse_args()


def emit_kv(key: str, value: object) -> None:
    """KEY=VALUE 形式で stdout に 1 行出力。改行は \\n にエスケープ。"""
    s = str(value).replace("\n", "\\n").replace("\r", "\\r")
    print(f"{key}={s}")


def emit_error(code: str, detail: str = "") -> None:
    emit_kv("ERROR", code)
    if detail:
        emit_kv("ERROR_DETAIL", detail)


def run_gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60)


def resolve_repo() -> str:
    try:
        r = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    url = r.stdout.strip()
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", url)
    return m.group(1) if m else ""


def resolve_branch() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except subprocess.SubprocessError:
        return ""
    return r.stdout.strip()


def check_pr_template(body: str) -> list[str]:
    """必須セクションが本文に含まれるか確認。欠落セクション名のリストを返す。"""
    missing = []
    for sec in REQUIRED_PR_SECTIONS:
        # `## <セクション名>` で検索（前後空白を許容）
        if not re.search(r"^##\s+" + re.escape(sec) + r"\b", body, re.MULTILINE):
            missing.append(sec)
    return missing


def get_linked_issue_state(repo: str, issue_number: int) -> str | None:
    try:
        r = run_gh("issue", "view", str(issue_number), "--repo", repo, "--json", "state")
        return json.loads(r.stdout).get("state")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None


def find_existing_pr(repo: str, branch: str) -> dict | None:
    try:
        r = run_gh("pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                   "--json", "number,url")
        items = json.loads(r.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return items[0] if items else None


def apply_linked_issue_reference(body: str, issue_number: int, link_kind: str) -> str:
    """PR 本文の末尾近くに Closes / Refs 参照を確実に含める。

    既に同じ番号の Closes / Refs / Fixes / Resolves があれば link_kind に合わせて書き換える。
    なければ末尾に `<link_kind> #<issue_number>` を追記する。
    """
    pattern = re.compile(rf"(Closes|Refs|Fixes|Resolves)\s+#{issue_number}\b", re.IGNORECASE)
    if pattern.search(body):
        return pattern.sub(f"{link_kind} #{issue_number}", body, count=1)
    sep = "\n\n" if not body.endswith("\n") else "\n"
    return body + sep + f"{link_kind} #{issue_number}\n"


def create_pr(repo: str, title: str, body: str, branch: str, draft: bool) -> str:
    args = [
        "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--head", branch,
        "--base", "main",
    ]
    if draft:
        args.append("--draft")
    r = run_gh(*args)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def main() -> int:
    args = parse_args()

    # 1. Publish ゲート
    if args.publish.strip().lower() != "yes":
        emit_error(E_APPROVAL_MISSING, "publish: yes が指定されていません")
        return 2

    # 2. PR 本文 Template Guard
    if not args.pr_body_file.exists():
        emit_error(E_PR_TEMPLATE_GUARD, f"pr-body-file が存在しません: {args.pr_body_file}")
        return 2
    body = args.pr_body_file.read_text(encoding="utf-8")
    missing = check_pr_template(body)
    if missing:
        emit_error(E_PR_TEMPLATE_GUARD, f"欠落セクション: {missing}")
        emit_kv("MISSING_SECTIONS", ",".join(missing))
        return 2

    # 2.5. Safety Claim Matrix Guard（changed_paths が指定されている場合のみ）
    if args.changed_paths and is_safety_sensitive(args.changed_paths):
        scm_issues = check_safety_claim_matrix(body)
        if scm_issues:
            emit_error(E_SAFETY_CLAIM_MATRIX_MISSING, "; ".join(scm_issues))
            emit_kv("SAFETY_CLAIM_MATRIX_ISSUES", " | ".join(scm_issues))
            return 2

    # repo / branch を解決
    repo = args.repo or resolve_repo()
    if not repo:
        emit_error(E_GH_FAILURE, "git remote から owner/repo を取得できませんでした")
        return 2
    branch = args.branch or resolve_branch()
    if not branch:
        emit_error(E_GH_FAILURE, "現在のブランチ名を取得できませんでした")
        return 2

    # 3. Linked Issue 状態確認 + Closes / Refs 判定
    state = get_linked_issue_state(repo, args.linked_issue)
    if state is None:
        emit_error(E_LINKED_ISSUE_STATE_UNKNOWN,
                   f"linked issue #{args.linked_issue} の state を取得できませんでした")
        return 2
    link_kind = "Closes" if state == "OPEN" else "Refs"
    body = apply_linked_issue_reference(body, args.linked_issue, link_kind)
    if state != "OPEN":
        print(f"[WARN] linked issue #{args.linked_issue} は {state}。Closes → Refs に downgrade します",
              file=sys.stderr)

    # 4. Idempotency チェック
    existing = find_existing_pr(repo, branch)
    if existing:
        emit_kv("EXISTING", "true")
        emit_kv("PR_URL", existing["url"])
        emit_kv("PR_NUMBER", existing["number"])
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        return 0

    draft = str(args.draft).strip().lower() == "true"

    # 5. PR 作成 (dry-run なら preview のみ)
    if args.dry_run:
        emit_kv("DRY_RUN", "true")
        emit_kv("PR_TITLE_PREVIEW", args.pr_title)
        first_lines = "\\n".join(body.splitlines()[:5])
        emit_kv("PR_BODY_PREVIEW_FIRST_LINES", first_lines)
        emit_kv("LINKED_ISSUE", args.linked_issue)
        emit_kv("LINK_KIND", link_kind)
        emit_kv("DRAFT", str(draft).lower())
        return 0

    try:
        pr_url = create_pr(repo, args.pr_title, body, branch, draft)
    except subprocess.CalledProcessError as e:
        emit_error(E_GH_FAILURE, f"gh pr create 失敗: exit {e.returncode}")
        if e.stderr:
            emit_kv("COMMAND_STDERR", e.stderr.strip()[:500])
        return 2

    if not pr_url:
        emit_error(E_GH_FAILURE, "gh pr create が URL を返しませんでした")
        return 2

    # PR 番号を URL から抽出
    m = re.search(r"/pull/(\d+)", pr_url)
    pr_number = m.group(1) if m else ""

    # 6. Output
    emit_kv("PR_URL", pr_url)
    emit_kv("PR_NUMBER", pr_number)
    emit_kv("LINKED_ISSUE", args.linked_issue)
    emit_kv("LINK_KIND", link_kind)
    emit_kv("EXISTING", "false")
    emit_kv("DRY_RUN", "false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
