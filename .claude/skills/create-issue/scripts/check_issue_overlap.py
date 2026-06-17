#!/usr/bin/env python3
"""
check_issue_overlap.py

Issue 起票前の duplicate / overlap preflight helper。

title keyword search だけに依存せず、title / goal / Allowed Paths / labels /
parent issue refs を使って既存 OPEN Issue / 兄弟 child との overlap を機械判定する。

Closed verdict enum（VERDICTS）:
- duplicate:                既存 OPEN Issue とほぼ同一スコープ（Allowed Paths が同一集合）
- overlap_requires_comment: Allowed Paths が部分的に重なる OPEN Issue が存在（coordination コメントが必要）
- safe_new_issue:           overlap なし（新規起票で安全）
- ambiguous_requires_human: 判定不能（title は重複だが Allowed Paths が disjoint 等）→ 人間判断

GitHub full-text search の false positive は、候補 Issue body を read-back して
実際の Allowed Paths overlap / title 類似を確認することで除外する
（search hit という事実だけでは overlap と判定しない）。

delivery-rollup parent の child 起票では、まだ存在しない child 同士の
Allowed Paths overlap も検査する（classify_child_overlap）。

本 helper は決定論的 pure function を中核とし、GitHub 取得は CLI 層の薄い
adapter に閉じる。tests は offline（--candidates-file / --children-file）で
すべての verdict 経路を再現できる。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


# ============================================================
# Verdict enum（closed set）
# ============================================================

DUPLICATE = "duplicate"
OVERLAP_REQUIRES_COMMENT = "overlap_requires_comment"
SAFE_NEW_ISSUE = "safe_new_issue"
AMBIGUOUS_REQUIRES_HUMAN = "ambiguous_requires_human"

VERDICTS: frozenset = frozenset(
    {
        DUPLICATE,
        OVERLAP_REQUIRES_COMMENT,
        SAFE_NEW_ISSUE,
        AMBIGUOUS_REQUIRES_HUMAN,
    }
)

# title 類似のしきい値（Jaccard）
_TITLE_DUP_THRESHOLD = 0.8       # ほぼ同一タイトル
_TITLE_RELATED_THRESHOLD = 0.5   # 関連していると見なす下限


# ============================================================
# Allowed Paths の正規化と overlap 判定
# ============================================================

_BULLET_RE = re.compile(r"^\s*[-+*]\s+")
_CODE_RE = re.compile(r"^`([^`]+)`\s*(?:[（(].*)?$")
_PAREN_ANNOTATION_RE = re.compile(r"\s*[（(][^）)]*[）)]\s*$")


def normalize_path(entry: str) -> str:
    """Allowed Paths の 1 エントリを bare path に正規化する。

    - bullet marker（-, +, *）を除去
    - backtick 包み（`path`）と末尾の全角/半角括弧注釈を除去
    - 先頭の ``./`` と末尾スラッシュを除去
    """
    s = entry.strip()
    s = _BULLET_RE.sub("", s).strip()
    m = _CODE_RE.match(s)
    if m:
        s = m.group(1).strip()
    else:
        s = _PAREN_ANNOTATION_RE.sub("", s).strip()
    # 残った backtick / 末尾注釈を保守的に除去
    s = s.strip("`").strip()
    s = _PAREN_ANNOTATION_RE.sub("", s).strip()
    if s.startswith("./"):
        s = s[2:]
    s = s.rstrip("/")
    return s


def normalize_paths(paths: Iterable[str]) -> Tuple[str, ...]:
    out: List[str] = []
    seen = set()
    for p in paths:
        n = normalize_path(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


def _segments(path: str) -> List[str]:
    return [seg for seg in path.split("/") if seg]


def paths_conflict(a: str, b: str) -> bool:
    """2 つの正規化済みパスが同一スコープを指すか判定する。

    完全一致、または一方が他方のディレクトリ接頭辞（segment prefix）である場合に
    True を返す（例: ``tests/create-issue`` と ``tests/create-issue/test_x.py``）。
    """
    a = normalize_path(a)
    b = normalize_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    sa, sb = _segments(a), _segments(b)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return longer[: len(shorter)] == shorter


def allowed_paths_overlap(
    a_paths: Iterable[str], b_paths: Iterable[str]
) -> Tuple[str, ...]:
    """両 Allowed Paths 集合の overlap を表す正規化パスを返す（昇順・重複なし）。"""
    a_norm = normalize_paths(a_paths)
    b_norm = normalize_paths(b_paths)
    hits = set()
    for a in a_norm:
        for b in b_norm:
            if paths_conflict(a, b):
                # より具体的（segment 数が多い）方を overlap 表現として残す
                hits.add(a if len(_segments(a)) >= len(_segments(b)) else b)
    return tuple(sorted(hits))


def same_path_set(a_paths: Iterable[str], b_paths: Iterable[str]) -> bool:
    a_norm = set(normalize_paths(a_paths))
    b_norm = set(normalize_paths(b_paths))
    return bool(a_norm) and a_norm == b_norm


# ============================================================
# title 類似（Jaccard token similarity）
# ============================================================

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[぀-ヿ一-鿿]")
# create-issue の title prefix（実装: / implement: 等）はノイズなので除外
_TITLE_PREFIX_RE = re.compile(r"^\s*(実装|implement|impl|docs?|fix|chore|test)\s*[:：]\s*", re.IGNORECASE)


def _title_tokens(title: str) -> frozenset:
    body = _TITLE_PREFIX_RE.sub("", title or "")
    return frozenset(t.lower() for t in _TOKEN_RE.findall(body))


def title_similarity(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


# ============================================================
# 入力モデル
# ============================================================

@dataclass(frozen=True)
class IssueScope:
    """Issue / child spec の overlap 判定に必要な最小スコープ。"""

    title: str
    allowed_paths: Tuple[str, ...] = ()
    number: Optional[int] = None
    goal: str = ""
    labels: Tuple[str, ...] = ()
    parent_refs: Tuple[str, ...] = ()
    body: Optional[str] = None
    state: str = "OPEN"
    # full-text search 由来の候補は read-back 検証が必要であることを示す
    search_hit: bool = False

    def effective_allowed_paths(self) -> Tuple[str, ...]:
        """明示 allowed_paths があればそれを、無ければ body から read-back する。"""
        if self.allowed_paths:
            return normalize_paths(self.allowed_paths)
        if self.body:
            return normalize_paths(extract_allowed_paths(self.body))
        return ()

    @staticmethod
    def from_dict(d: dict) -> "IssueScope":
        return IssueScope(
            title=str(d.get("title", "")),
            allowed_paths=tuple(d.get("allowed_paths", []) or []),
            number=d.get("number"),
            goal=str(d.get("goal", "") or ""),
            labels=tuple(d.get("labels", []) or []),
            parent_refs=tuple(str(x) for x in (d.get("parent_refs", []) or [])),
            body=d.get("body"),
            state=str(d.get("state", "OPEN") or "OPEN"),
            search_hit=bool(d.get("search_hit", False)),
        )


def extract_allowed_paths(body: str) -> List[str]:
    """Issue body の ``## Allowed Paths`` セクションを read-back する。

    GitHub full-text search が body のどこか（例: 引用や本文中の言及）に
    キーワードを見つけても、実際の Allowed Paths セクションに当該パスが
    無ければ overlap とは見なさない。この read-back が false positive 除外の核。
    """
    if not body:
        return []
    match = re.search(
        r"^##\s+Allowed Paths\s*$(.+?)(?=^##|\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return []
    section = match.group(1)
    paths: List[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        path = normalize_path(stripped)
        if path:
            paths.append(path)
    return paths


# ============================================================
# 判定結果モデル
# ============================================================

@dataclass(frozen=True)
class OverlapResult:
    verdict: str
    reason: str = ""
    matched_issues: Tuple[int, ...] = ()
    overlapping_paths: Tuple[str, ...] = ()
    excluded_false_positives: Tuple[int, ...] = ()

    def to_dict(self) -> dict:
        assert self.verdict in VERDICTS, f"invalid verdict: {self.verdict}"
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "matched_issues": list(self.matched_issues),
            "overlapping_paths": list(self.overlapping_paths),
            "excluded_false_positives": list(self.excluded_false_positives),
        }


@dataclass(frozen=True)
class ChildOverlapPair:
    a_index: int
    b_index: int
    a_title: str
    b_title: str
    overlapping_paths: Tuple[str, ...]


@dataclass(frozen=True)
class ChildOverlapResult:
    verdict: str
    reason: str = ""
    overlapping_pairs: Tuple[ChildOverlapPair, ...] = ()

    def to_dict(self) -> dict:
        assert self.verdict in VERDICTS, f"invalid verdict: {self.verdict}"
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "overlapping_pairs": [
                {
                    "a_index": p.a_index,
                    "b_index": p.b_index,
                    "a_title": p.a_title,
                    "b_title": p.b_title,
                    "overlapping_paths": list(p.overlapping_paths),
                }
                for p in self.overlapping_pairs
            ],
        }


# ============================================================
# 判定ロジック（pure function）
# ============================================================

def classify_overlap(
    current: IssueScope, candidates: Sequence[IssueScope]
) -> OverlapResult:
    """現在の起票候補と既存 Issue 群の overlap を closed enum で判定する。

    precedence: duplicate > overlap_requires_comment > ambiguous_requires_human
    > safe_new_issue。
    """
    cur_paths = current.effective_allowed_paths()

    duplicate_hits: List[int] = []
    overlap_hits: List[int] = []
    overlap_paths: set = set()
    title_dup_disjoint: List[int] = []
    excluded: List[int] = []

    for cand in candidates:
        # OPEN 以外（CLOSED / MERGED 等）は overlap 対象外
        if str(cand.state).upper() != "OPEN":
            continue

        cand_paths = cand.effective_allowed_paths()
        path_overlap = allowed_paths_overlap(cur_paths, cand_paths)
        tsim = title_similarity(current.title, cand.title)

        # --- false positive 除外（read-back） ---
        # full-text search hit だが、Allowed Paths が実際には重ならず、
        # title 類似も低い候補は false positive として除外する。
        if cand.search_hit and not path_overlap and tsim < _TITLE_RELATED_THRESHOLD:
            if cand.number is not None:
                excluded.append(cand.number)
            continue

        if path_overlap and cur_paths and same_path_set(cur_paths, cand_paths):
            if cand.number is not None:
                duplicate_hits.append(cand.number)
            overlap_paths.update(path_overlap)
        elif path_overlap:
            if cand.number is not None:
                overlap_hits.append(cand.number)
            overlap_paths.update(path_overlap)
        elif tsim >= _TITLE_DUP_THRESHOLD:
            # title はほぼ同一だが Allowed Paths が disjoint → 判定不能
            if cand.number is not None:
                title_dup_disjoint.append(cand.number)

    if duplicate_hits:
        return OverlapResult(
            verdict=DUPLICATE,
            reason="既存 OPEN Issue と Allowed Paths が同一集合",
            matched_issues=tuple(sorted(duplicate_hits)),
            overlapping_paths=tuple(sorted(overlap_paths)),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if overlap_hits:
        return OverlapResult(
            verdict=OVERLAP_REQUIRES_COMMENT,
            reason="既存 OPEN Issue と Allowed Paths が部分的に重複",
            matched_issues=tuple(sorted(overlap_hits)),
            overlapping_paths=tuple(sorted(overlap_paths)),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    if title_dup_disjoint:
        return OverlapResult(
            verdict=AMBIGUOUS_REQUIRES_HUMAN,
            reason="title が既存 Issue とほぼ同一だが Allowed Paths が disjoint",
            matched_issues=tuple(sorted(title_dup_disjoint)),
            excluded_false_positives=tuple(sorted(excluded)),
        )
    return OverlapResult(
        verdict=SAFE_NEW_ISSUE,
        reason="overlap なし（新規起票で安全）",
        excluded_false_positives=tuple(sorted(excluded)),
    )


def classify_child_overlap(children: Sequence[IssueScope]) -> ChildOverlapResult:
    """delivery-rollup parent の兄弟 child 同士の Allowed Paths overlap を検査する。"""
    pairs: List[ChildOverlapPair] = []
    has_identical = False
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            a, b = children[i], children[j]
            a_paths = a.effective_allowed_paths()
            b_paths = b.effective_allowed_paths()
            overlap = allowed_paths_overlap(a_paths, b_paths)
            if overlap:
                if same_path_set(a_paths, b_paths):
                    has_identical = True
                pairs.append(
                    ChildOverlapPair(
                        a_index=i,
                        b_index=j,
                        a_title=a.title,
                        b_title=b.title,
                        overlapping_paths=overlap,
                    )
                )

    if not pairs:
        return ChildOverlapResult(
            verdict=SAFE_NEW_ISSUE,
            reason="child 同士の Allowed Paths overlap なし",
        )
    verdict = DUPLICATE if has_identical else OVERLAP_REQUIRES_COMMENT
    return ChildOverlapResult(
        verdict=verdict,
        reason="child 同士で Allowed Paths が重複",
        overlapping_pairs=tuple(pairs),
    )


# ============================================================
# GitHub adapter（CLI 層、非 dry-run 時のみ使用）
# ============================================================

def _gh_search_candidates(repo: str, query: str, limit: int = 30) -> List[IssueScope]:
    """full-text search で候補 Issue を取得し body を read-back する。

    network 依存のため tests からは呼ばれない（offline は --candidates-file）。
    """
    try:
        proc = subprocess.run(
            [
                "gh", "search", "issues", query,
                "--repo", repo, "--state", "open",
                "--limit", str(limit), "--json", "number",
            ],
            check=True, capture_output=True, text=True,
        )
        numbers = [item["number"] for item in json.loads(proc.stdout or "[]")]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return []

    candidates: List[IssueScope] = []
    for num in numbers:
        try:
            view = subprocess.run(
                [
                    "gh", "issue", "view", str(num), "--repo", repo,
                    "--json", "number,title,body,labels,state",
                ],
                check=True, capture_output=True, text=True,
            )
            data = json.loads(view.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        candidates.append(
            IssueScope(
                title=data.get("title", ""),
                number=data.get("number"),
                body=data.get("body"),
                labels=tuple(l.get("name", "") for l in data.get("labels", []) or []),
                state=data.get("state", "OPEN"),
                search_hit=True,
            )
        )
    return candidates


# ============================================================
# CLI
# ============================================================

def _read_paths_file(path: str) -> List[str]:
    out: List[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _load_scope_list(path: str) -> List[IssueScope]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("candidates") or data.get("children") or []
    return [IssueScope.from_dict(d) for d in data]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_issue_overlap.py",
        description="Issue 起票前の duplicate / overlap preflight",
    )
    p.add_argument("--repo", help="owner/name（非 dry-run の full-text search に使用）")
    p.add_argument("--title", required=True, help="起票予定 Issue の title")
    p.add_argument("--goal", default="", help="goal_ref / Desired Destination")
    p.add_argument(
        "--allowed-paths-file",
        help="起票予定 Issue の Allowed Paths（1 行 1 パス）",
    )
    p.add_argument("--label", action="append", default=[], help="label（複数可）")
    p.add_argument(
        "--parent-ref", action="append", default=[], help="parent issue ref（複数可）"
    )
    p.add_argument(
        "--candidates-file",
        help="既存 Issue 候補の JSON（offline 判定 / read-back 用）",
    )
    p.add_argument(
        "--children-file",
        help="兄弟 child spec の JSON（delivery-rollup child overlap 判定用）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="GitHub へアクセスしない（候補は --candidates-file のみ）",
    )
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    allowed_paths: List[str] = []
    if args.allowed_paths_file:
        allowed_paths = _read_paths_file(args.allowed_paths_file)

    # --- child overlap モード ---
    if args.children_file:
        children = _load_scope_list(args.children_file)
        child_result = classify_child_overlap(children)
        out = {"mode": "child_overlap", **child_result.to_dict()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    current = IssueScope(
        title=args.title,
        allowed_paths=tuple(allowed_paths),
        goal=args.goal,
        labels=tuple(args.label),
        parent_refs=tuple(args.parent_ref),
    )

    candidates: List[IssueScope] = []
    if args.candidates_file:
        candidates = _load_scope_list(args.candidates_file)
    elif not args.dry_run:
        if not args.repo:
            print(
                json.dumps(
                    {"error": "--repo is required for online search"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        query = " ".join(_title_tokens(args.title)) or args.title
        candidates = _gh_search_candidates(args.repo, query)

    result = classify_overlap(current, candidates)
    out = {"mode": "issue_overlap", **result.to_dict()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
