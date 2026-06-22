#!/usr/bin/env python3
"""
pr_body_japanese_repair_plan.py

PR body の日本語修復プランを生成するスクリプト。

日本語判定の SSOT:
  - validate_japanese_content.py の validate_text() / split_markdown_blocks()
  - prose_boundary_policy.py の iter_markdown_blocks() / lookup_heading_policy()

Exit codes:
  0  = pass       (全 prose block が日本語比率を満たす)
  10 = repairable (deterministic に意味保存できる修復のみで pass 可能)
  20 = human_review_required (任意英語の意味変換が必要 = 人間判断)
  30 = invalid_body (PR body が不正 / parse 不能)
  40 = gh_error   (gh CLI からの PR body 取得に失敗)

Stdout: PR_BODY_JAPANESE_REPAIR_PLAN_V1 の compact JSON のみ
Stderr: 最小限のエラーメッセージのみ（raw PR body / CI log は出さない）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SSOT インポート: validate_japanese_content.py / prose_boundary_policy.py
# ---------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_SCRIPTS_DIR = Path(__file__).resolve().parent
_CREATE_ISSUE_SCRIPTS = _SCRIPTS_DIR.parent.parent / "create-issue" / "scripts"

# prose_boundary_policy は validate_japanese_content が依存するため先にロード
_pbp = _load_module("prose_boundary_policy", _CREATE_ISSUE_SCRIPTS / "prose_boundary_policy.py")

# sys.modules に登録してから vjc をロードすることで import prose_boundary_policy が成功する
_vjc = _load_module("validate_japanese_content", _CREATE_ISSUE_SCRIPTS / "validate_japanese_content.py")

# SSOT 関数参照
validate_text = _vjc.validate_text
iter_markdown_blocks = _pbp.iter_markdown_blocks
lookup_heading_policy = _pbp.lookup_heading_policy

# ---------------------------------------------------------------------------
# GitHub closing keyword の全 variant（AC6）
# ---------------------------------------------------------------------------

# GitHub が認識するすべての closing keyword variant
# 参照: https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
_CLOSING_KEYWORDS = (
    r"close|closes|closed|"
    r"fix|fixes|fixed|"
    r"resolve|resolves|resolved"
)

# closing keyword パターン:
#   - keyword (colon variant もサポート: "Closes: #N")
#   - 同一リポジトリ: #N
#   - クロスリポジトリ: owner/repo#N
_CLOSING_KEYWORD_RE = re.compile(
    r"(?i)\b(?:" + _CLOSING_KEYWORDS + r"):?\s+"
    r"(?:"
    r"[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+"  # cross-repo: owner/repo#N
    r"|#\d+"                                  # same-repo: #N
    r")"
    r"(?:\s*,\s*(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+|#\d+))*",  # 複数 issue
    re.IGNORECASE,
)

# Refs #N パターン（closing キーワードではないが保護トークン）
_REFS_RE = re.compile(
    r"(?i)\bRefs?\s+(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+|#\d+)"
    r"(?:\s*,\s*(?:[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+#\d+|#\d+))*"
)

# HTML comment パターン
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# ---------------------------------------------------------------------------
# 保護トークン抽出
# ---------------------------------------------------------------------------

def extract_preserved_tokens(body: str) -> list[str]:
    """
    PR body から保護すべきトークンを抽出する。

    保護対象:
    - GitHub closing keyword 全 variant (close/closes/closed/fix/fixes/fixed/resolve/resolves/resolved)
    - colon variant (Closes: #N)
    - cross-repo reference (owner/repo#N)
    - 複数 issue 列挙 (Closes #1, #2)
    - Refs #N
    - HTML comment (<!-- ... -->)
    """
    tokens = []

    for m in _CLOSING_KEYWORD_RE.finditer(body):
        tok = m.group(0).strip()
        if tok and tok not in tokens:
            tokens.append(tok)

    for m in _REFS_RE.finditer(body):
        tok = m.group(0).strip()
        if tok and tok not in tokens:
            tokens.append(tok)

    for m in _HTML_COMMENT_RE.finditer(body):
        tok = m.group(0).strip()
        if tok and tok not in tokens:
            tokens.append(tok)

    return tokens


# ---------------------------------------------------------------------------
# 保護ブロック判定
# ---------------------------------------------------------------------------

def _is_protected_block(block: dict) -> bool:
    """
    ブロックが保護対象（書き換え禁止）かどうかを判定する。

    保護対象:
    - code_fence
    - table
    - machine_yaml / shell_command / grep_pattern / url_or_identifier_only
    - canonical heading / bilingual heading（heading_policy SSOT）
    """
    btype = block.get("type", "prose")

    # code_fence, table, machine_yaml 等は保護
    if btype in ("code_fence", "table", "machine_yaml", "shell_command",
                 "grep_pattern", "url_or_identifier_only"):
        return True

    # prose ブロックでも ATX 見出し・HTML comment・closing keyword は保護
    if btype == "prose":
        text = block.get("text", "")
        raw_text = block.get("raw_text", text)
        # 任意の ATX 見出し行は保護（canonical か否かを問わず）
        parsed = _pbp.parse_atx_heading_line(raw_text.rstrip("\r\n"))
        if parsed is not None:
            return True
        # HTML comment を含むブロックは保護
        if _HTML_COMMENT_RE.search(text):
            return True
        # closing keyword / Refs を含むブロックは保護（意味変換不可）
        if _CLOSING_KEYWORD_RE.search(text) or _REFS_RE.search(text):
            return True

    return False


# ---------------------------------------------------------------------------
# repairable 判定
# ---------------------------------------------------------------------------

# repairable な template / boilerplate パターン
# これらは deterministic に日本語を補足できる既知パターン
_REPAIRABLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 例: "Summary" → "## 概要 (Summary)" のような見出し直後の英語説明が短い場合
    # ここでは「短い英語 boilerplate」を判定する近似ルールを採用
    # 実際の書き換えは safe_rewrite_plan に記録するのみ（LLM 依存の翻訳はしない）
]

# "repairable" とみなす最大文字数（英語のみの短い boilerplate）
_REPAIRABLE_MAX_EFFECTIVE_CHARS = 60

# boilerplate / template 特徴: 単純な英語フレーズ（句点・複数文なし）
_SIMPLE_ENGLISH_RE = re.compile(
    r"^[A-Za-z0-9 _\-.,:()\[\]#/\\\"\'`%@!?&+*=~^<>;{}|\n\r\t]*$"
)


def _is_repairable_block(block: dict, threshold: float = 0.1) -> bool:
    """
    ブロックが repairable（deterministic な意味保存修復が可能）かどうかを判定する。

    repairable 条件:
    1. 保護ブロックでない（prose ブロック）
    2. 日本語比率が threshold 未満（SSOT の failed_blocks から来るため既知）
    3. テキストが短い（_REPAIRABLE_MAX_EFFECTIVE_CHARS 以下）かつ ASCII のみ
       → 既知 template / boilerplate として機械的な日本語補足が可能

    任意英語 prose（長文・複数文・句読点あり）は human_review_required に routing する。
    """
    if _is_protected_block(block):
        return False

    btype = block.get("type", "prose")
    if btype != "prose":
        return False

    text = block.get("text", "")

    # SSOT データから effective_chars を取得（未提供の場合は validate_text で計算）
    effective_chars = block.get("effective_chars")
    if effective_chars is None:
        result = validate_text(text, threshold=threshold)
        if result.passed:
            return False
        effective_chars = result.total_chars

    # 有効文字数が少なく ASCII のみ → boilerplate
    if effective_chars == 0:
        return True  # empty prose = repairable (no-op)
    if effective_chars <= _REPAIRABLE_MAX_EFFECTIVE_CHARS and _SIMPLE_ENGLISH_RE.match(text.strip()):
        return True

    return False


# ---------------------------------------------------------------------------
# safe_rewrite_plan 生成
# ---------------------------------------------------------------------------

def _build_safe_rewrite_plan(
    failed_blocks: list[dict],
    threshold: float,
    include_preview: bool = False,
) -> list[dict]:
    """
    repairable な failed block の safe_rewrite_plan を構築する。

    safe_rewrite_plan の各エントリ:
    - block_index: 0-indexed の block 番号
    - original_text: 元テキスト（--include-preview 指定時のみ先頭 100 文字。デフォルト: None）
    - action: "append_japanese_note" (唯一の deterministic action)
    - note: append すべき日本語注記（空文字列 = 呼び出し側が決定）
    """
    plan = []
    for fb in failed_blocks:
        if _is_repairable_block(fb, threshold=threshold):
            plan.append({
                "block_index": fb.get("_block_index", -1),
                "original_text": fb.get("text", "")[:100] if include_preview else None,
                "action": "append_japanese_note",
                "note": "",  # 機械的な確定翻訳は不可; 呼び出し側が補足する
            })
    return plan


# ---------------------------------------------------------------------------
# body_file_out 生成（repairable case）
# ---------------------------------------------------------------------------

def _generate_body_file_out(body: str, failed_blocks: list[dict], threshold: float) -> str | None:
    """
    repairable case で body_file_out を生成する。

    現時点では repairable case であっても「意味保存できる確定テキスト」が
    機械的に生成できないため、body_file_out は None を返す。

    将来的に deterministic template 補完が実装された場合のみ non-None を返すようにする。

    NOTE: この関数は「repairable = true」であっても body_file_out を生成しない。
    safe_rewrite_plan に action: "append_japanese_note" を記録し、
    呼び出し側（main agent / human）が注記テキストを確定させた後に
    update_pr.py 経由で適用することを想定している。
    """
    return None


# ---------------------------------------------------------------------------
# PR body の取得（--pr モード）
# ---------------------------------------------------------------------------

def _fetch_pr_body(pr_number: int, repo: str | None) -> tuple[str | None, str | None]:
    """
    gh CLI を使って PR body を取得する。

    Returns:
        (body_text, error_message)
    """
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "body", "--jq", ".body"]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, "gh CLI timeout"
    except OSError as e:
        return None, f"gh CLI spawn error: {e}"

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return None, f"gh pr view failed (exit {result.returncode}): {stderr}"

    return result.stdout.strip(), None


# ---------------------------------------------------------------------------
# メインロジック
# ---------------------------------------------------------------------------

def analyze_pr_body(
    body: str,
    threshold: float = 0.1,
    include_preview: bool = False,
) -> dict[str, Any]:
    """
    PR body を分析して PR_BODY_JAPANESE_REPAIR_PLAN_V1 を返す。

    Args:
        body: PR body テキスト
        threshold: 日本語比率の閾値（デフォルト: 0.1）
        include_preview: text_preview / original_text に実テキストを含めるか（デフォルト: False）

    Returns:
        PR_BODY_JAPANESE_REPAIR_PLAN_V1 dict
    """
    # body が空またはほぼ空の場合
    if not body or not body.strip():
        return {
            "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
            "status": "invalid_body",
            "threshold": threshold,
            "failed_blocks": [],
            "safe_rewrite_plan": [],
            "body_file_out": None,
            "preserved_tokens": [],
            "next_action": "human_review_required",
        }

    # 保護トークンを抽出（AC6）
    preserved_tokens = extract_preserved_tokens(body)

    # SSOT: validate_text を全 body に対して一度だけ呼び出す（AC2 SSOT 再利用）
    full_result = validate_text(body, threshold=threshold)

    # 全ブロック pass の場合は早期リターン
    if full_result.passed:
        return {
            "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
            "status": "pass",
            "threshold": threshold,
            "failed_blocks": [],
            "safe_rewrite_plan": [],
            "body_file_out": None,
            "preserved_tokens": preserved_tokens,
            "next_action": "none",
        }

    # SSOT の failed_blocks を出発点とし、追加の保護フィルタを適用する。
    # validate_text は code fence / table / canonical heading を除外済み。
    # ここでは ATX 見出し・HTML comment・closing keyword の追加保護を行う。
    failed_block_infos = []
    for i, sfb in enumerate(full_result.failed_blocks):
        original = sfb.get("original", "")
        synthetic_block = {"type": "prose", "text": original, "raw_text": original}
        if _is_protected_block(synthetic_block):
            continue
        failed_block_infos.append({
            "_block_index": i,
            "text": original,
            "type": "prose",
            "ratio": sfb.get("ratio", 0.0),
            "effective_chars": sfb.get("effective_chars", 0),
            "japanese_chars": sfb.get("japanese_chars", 0),
            "passed": False,
        })

    # 全 failed block が保護対象だった場合は pass
    if not failed_block_infos:
        return {
            "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
            "status": "pass",
            "threshold": threshold,
            "failed_blocks": [],
            "safe_rewrite_plan": [],
            "body_file_out": None,
            "preserved_tokens": preserved_tokens,
            "next_action": "none",
        }

    # repairable チェック
    all_repairable = all(
        _is_repairable_block(fb, threshold=threshold)
        for fb in failed_block_infos
    )

    safe_rewrite_plan = _build_safe_rewrite_plan(failed_block_infos, threshold, include_preview)

    # body_file_out 生成（repairable case のみ、現時点は None）
    body_file_out = None
    if all_repairable:
        body_file_out = _generate_body_file_out(body, failed_block_infos, threshold)

    # failed_blocks の出力用整形
    # text_preview は --include-preview 指定時のみ実テキストを含める（デフォルト: None）
    output_failed_blocks = [
        {
            "block_index": fb.get("_block_index", -1),
            "text_preview": fb.get("text", "")[:100] if include_preview else None,
            "ratio": round(fb.get("ratio", 0.0), 4),
            "effective_chars": fb.get("effective_chars", 0),
            "japanese_chars": fb.get("japanese_chars", 0),
        }
        for fb in failed_block_infos
    ]

    # status: repairable は body_file_out が non-None の場合のみ（Blocker 1）
    if all_repairable and body_file_out is not None:
        status = "repairable"
        next_action = "apply_safe_rewrite_plan"
    else:
        status = "human_review_required"
        next_action = "human_review_required"

    return {
        "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
        "status": status,
        "threshold": threshold,
        "failed_blocks": output_failed_blocks,
        "safe_rewrite_plan": safe_rewrite_plan,
        "body_file_out": body_file_out,
        "preserved_tokens": preserved_tokens,
        "next_action": next_action,
    }


def _determine_exit_code(plan: dict) -> int:
    """PR_BODY_JAPANESE_REPAIR_PLAN_V1 の status から exit code を返す。"""
    status = plan.get("status", "")
    if status == "pass":
        return 0
    elif status == "repairable":
        return 10
    elif status == "human_review_required":
        return 20
    elif status == "invalid_body":
        return 30
    else:
        return 20  # デフォルト: human_review_required


# ---------------------------------------------------------------------------
# CLI エントリポイント
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PR body の日本語修復プランを生成する"
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--pr",
        type=int,
        metavar="PR_NUMBER",
        help="PR 番号（gh CLI 経由で body を取得）",
    )
    input_group.add_argument(
        "--body-file",
        type=Path,
        help="PR body ファイルパス（直接指定）",
    )

    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="owner/repo（--pr モード時。省略時は git remote から取得）",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.1,
        help="日本語比率の閾値（デフォルト: 0.1）",
    )
    parser.add_argument(
        "--include-preview",
        action="store_true",
        default=False,
        help="text_preview / original_text に実テキストを含める（デフォルト: 非表示）",
    )

    args = parser.parse_args(argv)

    # PR body の取得
    if args.pr is not None:
        body, error = _fetch_pr_body(args.pr, args.repo)
        if error:
            plan = {
                "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
                "status": "gh_error",
                "threshold": args.threshold,
                "failed_blocks": [],
                "safe_rewrite_plan": [],
                "body_file_out": None,
                "preserved_tokens": [],
                "next_action": "human_review_required",
                "error": error,
            }
            # stdout に compact JSON のみ（AC8）
            print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
            return 40
        if body is None:
            body = ""
    else:
        # --body-file モード
        body_path = args.body_file
        if not body_path.exists():
            plan = {
                "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
                "status": "invalid_body",
                "threshold": args.threshold,
                "failed_blocks": [],
                "safe_rewrite_plan": [],
                "body_file_out": None,
                "preserved_tokens": [],
                "next_action": "human_review_required",
                "error": f"body-file が存在しません: {body_path}",
            }
            print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
            return 30
        try:
            body = body_path.read_text(encoding="utf-8")
        except OSError as e:
            plan = {
                "schema": "PR_BODY_JAPANESE_REPAIR_PLAN_V1",
                "status": "invalid_body",
                "threshold": args.threshold,
                "failed_blocks": [],
                "safe_rewrite_plan": [],
                "body_file_out": None,
                "preserved_tokens": [],
                "next_action": "human_review_required",
                "error": str(e),
            }
            print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
            return 30

    # 分析実行
    plan = analyze_pr_body(body, threshold=args.threshold, include_preview=args.include_preview)

    # stdout に compact JSON のみ（AC8）
    print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))

    return _determine_exit_code(plan)


if __name__ == "__main__":
    sys.exit(main())
