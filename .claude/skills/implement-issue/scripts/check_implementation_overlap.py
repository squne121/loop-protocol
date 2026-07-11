#!/usr/bin/env python3
"""
check_implementation_overlap.py

`implement-issue` 専用の contract-aware overlap preflight adapter（#1452）。

`check_issue_overlap.py`（`.claude/skills/create-issue/scripts/`）の pure
classifier（`classify_overlap` / `IssueScope` / `SourceStatus` / path
normalization）を正本として再利用し、それ自体の scoring / schema ロジックは
変更しない（#1452 の Out of Scope）。

本 adapter が新規に持つ責務は「implementation 専用の候補収集レイヤー」のみ:

- `--issue-number` を必須にし、対象 Issue 自身を候補から自己除外する。
- `phase/implementation` ラベルが付いた OPEN Issue を列挙する
  （`gh issue list` オンライン経路、または offline fixture 経路）。
- 全候補の本文から Allowed Paths をローカルで抽出する。
- 明示的な取得上限と saturation 検出を持ち、全件性を証明できない場合は
  fail-closed にする（`check_issue_overlap.py` の `SourceStatus` にマップ）。
- candidate readback（``## Outcome`` / ``## In Scope`` / ``## Out of Scope`` /
  ``## Delivery Rule``）を経ないと `overlap_requires_comment`（C1 / C2a）を
  「継続可能」と確定しない（AC4）。
- `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence を構造化出力する（AC8）。
- exit code は下記の closed route enum にマップする（AC7）。

## exit code 契約（AC7, closed set）

| route                            | exit |
|-----------------------------------|------|
| proceed（C0）                      | 0    |
| proceed_with_collision_evidence   | 1    |
| wait_for_predecessor（C2b）        | 2    |
| human_review_required             | 3    |
| duplicate                         | 4    |
| runtime_error                     | 5    |

unknown な classify_overlap 出力（verdict / policy_class が既知集合外）は
`runtime_error`（内部契約違反の兆候）に、readback 不完全や source degraded は
`human_review_required` に fail-closed で倒す。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_THIS_FILE = Path(__file__).resolve()
_IMPLEMENT_ISSUE_SCRIPTS_DIR = _THIS_FILE.parent
_SKILLS_DIR = _IMPLEMENT_ISSUE_SCRIPTS_DIR.parent.parent
_CREATE_ISSUE_SCRIPTS_DIR = _SKILLS_DIR / "create-issue" / "scripts"
if str(_CREATE_ISSUE_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CREATE_ISSUE_SCRIPTS_DIR))

from check_issue_overlap import (  # noqa: E402
    AMBIGUOUS_REQUIRES_HUMAN,
    DUPLICATE,
    OVERLAP_REQUIRES_COMMENT,
    SAFE_NEW_ISSUE,
    SOURCE_OK,
    SOURCE_SATURATED,
    IssueScope,
    OverlapResult,
    SourceStatus,
    classify_overlap,
    extract_allowed_paths,
)

SCHEMA = "IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1"

DEFAULT_CANDIDATE_LIMIT = 100

# ------------------------------------------------------------
# route enum（closed set, AC7 の正本）
# ------------------------------------------------------------

ROUTE_PROCEED = "proceed"
ROUTE_PROCEED_WITH_EVIDENCE = "proceed_with_collision_evidence"
ROUTE_WAIT_FOR_PREDECESSOR = "wait_for_predecessor"
ROUTE_HUMAN_REVIEW_REQUIRED = "human_review_required"
ROUTE_DUPLICATE = "duplicate"
ROUTE_RUNTIME_ERROR = "runtime_error"

ROUTES: frozenset = frozenset(
    {
        ROUTE_PROCEED,
        ROUTE_PROCEED_WITH_EVIDENCE,
        ROUTE_WAIT_FOR_PREDECESSOR,
        ROUTE_HUMAN_REVIEW_REQUIRED,
        ROUTE_DUPLICATE,
        ROUTE_RUNTIME_ERROR,
    }
)

ROUTE_EXIT_CODES: Dict[str, int] = {
    ROUTE_PROCEED: 0,
    ROUTE_PROCEED_WITH_EVIDENCE: 1,
    ROUTE_WAIT_FOR_PREDECESSOR: 2,
    ROUTE_HUMAN_REVIEW_REQUIRED: 3,
    ROUTE_DUPLICATE: 4,
    ROUTE_RUNTIME_ERROR: 5,
}

_READBACK_HEADINGS = ("Outcome", "In Scope", "Out of Scope", "Delivery Rule")
_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[぀-ヿ一-鿿]")
_HEADING_OVERLAP_THRESHOLD = 0.5

# workflow.md の "Depends on #N" line-anchored dependency 表記（C2a/C2b 判定用）
_DEPENDS_ON_RE = re.compile(r"[Dd]epends\s+on\s+#(\d+)")
_PARENT_ISSUE_RE = re.compile(r"^parent_issue:\s*\"?#?(\d+)\"?", re.MULTILINE)


class OverlapRuntimeError(RuntimeError):
    """GitHub 取得失敗 / JSON 解析失敗 / schema 違反を表す fail-closed error。"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _extract_section(body: str, heading: str) -> str:
    if not body:
        return ""
    pattern = rf"^##\s+{re.escape(heading)}\s*$(.+?)(?=^##|\Z)"
    match = re.search(pattern, body, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def _tokens(text: str) -> set:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _contract_schema_keys(body: str) -> Dict[str, str]:
    """`## Machine-Readable Contract` の fenced yaml から単純 key: value を読む。"""
    match = re.search(
        r"^##\s+Machine-Readable Contract\s*$.*?```(?:yaml)?\s*(.+?)```",
        body or "",
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return {}
    out: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip('"')
    return out


# ============================================================
# 候補収集レイヤー（implementation 専用、self-exclusion + saturation guard）
# ============================================================


def _run_gh_json(args: Sequence[str]) -> Any:
    try:
        proc = subprocess.run(
            list(args),
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OverlapRuntimeError(f"gh command failed: {' '.join(args)}: {exc}") from exc
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise OverlapRuntimeError(f"gh command returned invalid JSON: {' '.join(args)}") from exc


def fetch_current_issue(repo: str, issue_number: int) -> Dict[str, Any]:
    data = _run_gh_json(
        [
            "gh", "issue", "view", str(issue_number),
            "--repo", repo,
            "--json", "number,title,body,labels,updatedAt,url",
        ]
    )
    if not isinstance(data, dict) or "body" not in data:
        raise OverlapRuntimeError("current issue readback missing body")
    return data


def fetch_implementation_candidates(
    repo: str, limit: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """OPEN かつ ``phase/implementation`` ラベルを持つ Issue を列挙する。

    Returns (candidates_raw, saturated)。``saturated`` は取得件数が limit に
    到達し全件性を証明できないことを表す（fail-closed の入力）。
    """
    data = _run_gh_json(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--label", "phase/implementation",
            "--state", "open",
            "--json", "number,title,body,labels,updatedAt,url",
            "--limit", str(limit),
        ]
    )
    if not isinstance(data, list):
        raise OverlapRuntimeError("gh issue list did not return a JSON array")
    saturated = len(data) >= limit
    return data, saturated


def _extract_depends_on(body: str) -> Tuple[str, ...]:
    """body 中の line-anchored ``Depends on #N`` を抽出する（workflow.md 準拠）。"""
    return tuple(sorted(set(_DEPENDS_ON_RE.findall(body or ""))))


def _extract_parent_ref(body: str) -> Tuple[str, ...]:
    """``## Machine-Readable Contract`` の ``parent_issue: "#N"`` を抽出する。"""
    match = _PARENT_ISSUE_RE.search(body or "")
    return (match.group(1),) if match else ()


def _issue_scope_from_raw(raw: Dict[str, Any]) -> IssueScope:
    body = str(raw.get("body") or "")
    return IssueScope(
        title=str(raw.get("title", "")),
        number=raw.get("number"),
        url=str(raw.get("url", "") or ""),
        body=raw.get("body"),
        labels=tuple(
            (lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
            for lbl in (raw.get("labels", []) or [])
        ),
        depends_on=_extract_depends_on(body),
        parent_refs=_extract_parent_ref(body),
        state="OPEN",
        search_hit=True,
    )


# ============================================================
# candidate readback（AC4: Outcome/In Scope/Out of Scope/Delivery Rule）
# ============================================================


def _readback_candidate(
    current_body: str, cand_body: str
) -> Tuple[bool, bool, Optional[str]]:
    """candidate の readback を行い (readback_complete, heading_overlap, non_conflict_reason) を返す。

    readback_complete=False は ``## Outcome`` / ``## In Scope`` のいずれかが
    候補本文から取得できなかったことを表す（fail-closed の入力）。
    """
    cand_outcome = _extract_section(cand_body, "Outcome")
    cand_in_scope = _extract_section(cand_body, "In Scope")
    if not cand_outcome or not cand_in_scope:
        return False, False, None

    cur_outcome = _extract_section(current_body, "Outcome")
    overlap_ratio = _jaccard(cur_outcome, cand_outcome)
    heading_overlap = overlap_ratio >= _HEADING_OVERLAP_THRESHOLD
    non_conflict_reason = None
    if not heading_overlap:
        non_conflict_reason = (
            "candidate Outcome は current Outcome と意味的に不一致"
            f"（token overlap={overlap_ratio:.2f} < {_HEADING_OVERLAP_THRESHOLD}）。"
            "Allowed Paths の一致のみで、Outcome/In Scope は disjoint。"
        )
    return True, heading_overlap, non_conflict_reason


# ============================================================
# route 判定（classify_overlap 結果 -> AC7 route enum）
# ============================================================


def _route_for_result(
    result: OverlapResult,
    *,
    current_body: str,
    candidate_bodies: Dict[int, str],
) -> Tuple[str, Dict[int, Dict[str, Any]]]:
    """OverlapResult を route + per-candidate readback evidence へマップする。"""
    per_candidate: Dict[int, Dict[str, Any]] = {}

    verdict = result.verdict
    policy_class = result.policy_class

    if verdict not in {SAFE_NEW_ISSUE, OVERLAP_REQUIRES_COMMENT, AMBIGUOUS_REQUIRES_HUMAN, DUPLICATE}:
        return ROUTE_RUNTIME_ERROR, per_candidate

    if verdict == DUPLICATE:
        return ROUTE_DUPLICATE, per_candidate

    if verdict == SAFE_NEW_ISSUE:
        return ROUTE_PROCEED, per_candidate

    if verdict == AMBIGUOUS_REQUIRES_HUMAN:
        if policy_class == "C2b":
            return ROUTE_WAIT_FOR_PREDECESSOR, per_candidate
        # C3 / unknown(source degraded) はいずれも human_review_required
        return ROUTE_HUMAN_REVIEW_REQUIRED, per_candidate

    # verdict == OVERLAP_REQUIRES_COMMENT (C1 or C2a)
    if policy_class not in {"C1", "C2a"}:
        # 契約上ここに来ないはずの policy_class は runtime_error に倒す
        return ROUTE_RUNTIME_ERROR, per_candidate

    all_readback_complete = True
    any_real_overlap = False
    for cand in result.candidates:
        if cand.issue_number is None:
            continue
        cand_body = candidate_bodies.get(cand.issue_number, "")
        complete, heading_overlap, non_conflict_reason = _readback_candidate(
            current_body, cand_body
        )
        per_candidate[cand.issue_number] = {
            "heading_overlap": heading_overlap,
            "non_conflict_reason": non_conflict_reason,
            "readback_complete": complete,
        }
        if not complete:
            all_readback_complete = False
        if heading_overlap:
            any_real_overlap = True

    if not all_readback_complete:
        # AC4: readback 前に統合PRを提案しない -> fail-closed
        return ROUTE_HUMAN_REVIEW_REQUIRED, per_candidate
    if any_real_overlap:
        # 見た目は path-only overlap だが Outcome まで意味的に重なる -> 人間判断
        return ROUTE_HUMAN_REVIEW_REQUIRED, per_candidate
    return ROUTE_PROCEED_WITH_EVIDENCE, per_candidate


# ============================================================
# evidence 構築（AC8: IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1）
# ============================================================


def build_evidence(
    *,
    current_number: int,
    current_body: str,
    current_updated_at: Optional[str],
    current_paths: Sequence[str],
    source_complete: bool,
    source_saturated: bool,
    collected_at: str,
    result: OverlapResult,
    route: str,
    candidate_meta: Dict[int, Dict[str, Any]],
    candidate_updated_at: Dict[int, Optional[str]],
    candidate_bodies: Dict[int, str],
) -> Dict[str, Any]:
    current_contract = _contract_schema_keys(current_body)

    candidates_out: List[Dict[str, Any]] = []
    for cand in result.candidates:
        num = cand.issue_number
        meta = candidate_meta.get(num, {}) if num is not None else {}
        cand_body = candidate_bodies.get(num, "") if num is not None else ""
        cand_contract = _contract_schema_keys(cand_body)
        schema_key_overlap = bool(
            current_contract.get("change_kind")
            and current_contract.get("change_kind") == cand_contract.get("change_kind")
        )
        candidates_out.append(
            {
                "issue_number": num,
                "updated_at": candidate_updated_at.get(num) if num is not None else None,
                "body_sha256": f"sha256:{_sha256(cand_body)}" if cand_body else None,
                "overlapping_paths": list(cand.overlapping_paths),
                "heading_overlap": meta.get("heading_overlap", False),
                "schema_key_overlap": schema_key_overlap,
                "policy_class": result.policy_class,
                "non_conflict_reason": meta.get("non_conflict_reason"),
            }
        )

    body: Dict[str, Any] = {
        "schema": SCHEMA,
        "current_issue": {
            "number": current_number,
            "updated_at": current_updated_at,
            "body_sha256": f"sha256:{_sha256(current_body)}",
            "allowed_paths": list(current_paths),
        },
        "source": {
            "complete": source_complete,
            "saturated": source_saturated,
            "collected_at": collected_at,
        },
        "candidates": candidates_out,
        "route": route,
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    body["evidence_sha256"] = f"sha256:{_sha256(canonical)}"
    return body


# ============================================================
# CLI
# ============================================================


def _load_json_file(path: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise OverlapRuntimeError(f"failed to read JSON file: {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OverlapRuntimeError(f"invalid JSON in file: {path}: {exc}") from exc


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="check_implementation_overlap.py",
        description=(
            "implement-issue 専用の contract-aware overlap preflight "
            "（check_issue_overlap.py の classifier を再利用）"
        ),
    )
    p.add_argument("--issue-number", required=True, type=int, help="対象 Issue 番号（自己除外に使用）")
    p.add_argument("--repo", help="owner/name（オンライン取得時に必須）")
    p.add_argument("--limit", type=int, default=DEFAULT_CANDIDATE_LIMIT, help="候補取得上限")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="GitHub へアクセスしない（--current-file / --candidates-file 必須）",
    )
    p.add_argument("--current-file", help="offline 用: 対象 Issue の JSON（number,title,body,updatedAt）")
    p.add_argument(
        "--candidates-file",
        help="offline 用: 候補 Issue raw JSON 配列（number,title,body,labels,updatedAt,url）",
    )
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        if args.dry_run:
            if not args.current_file or not args.candidates_file:
                raise OverlapRuntimeError("--dry-run には --current-file と --candidates-file が必須")
            current_raw = _load_json_file(args.current_file)
            candidates_raw = _load_json_file(args.candidates_file)
            saturated = len(candidates_raw) >= args.limit
        else:
            if not args.repo:
                raise OverlapRuntimeError("--repo is required for online fetch")
            current_raw = fetch_current_issue(args.repo, args.issue_number)
            candidates_raw, saturated = fetch_implementation_candidates(args.repo, args.limit)

        if not isinstance(current_raw, dict) or "body" not in current_raw:
            raise OverlapRuntimeError("current issue JSON missing required 'body' field")
        if not isinstance(candidates_raw, list):
            raise OverlapRuntimeError("candidates JSON must be an array")

        # --- 自己除外（AC6） ---
        candidates_raw = [
            c for c in candidates_raw if int(c.get("number", -1)) != args.issue_number
        ]

        current_body = str(current_raw.get("body") or "")
        current_paths = extract_allowed_paths(current_body)
        current = IssueScope(
            title=str(current_raw.get("title", "")),
            number=args.issue_number,
            allowed_paths=tuple(current_paths),
            body=current_body,
            depends_on=_extract_depends_on(current_body),
            parent_refs=_extract_parent_ref(current_body),
        )

        candidate_bodies: Dict[int, str] = {}
        candidate_updated_at: Dict[int, Optional[str]] = {}
        candidates: List[IssueScope] = []
        for raw in candidates_raw:
            scope = _issue_scope_from_raw(raw)
            candidates.append(scope)
            if scope.number is not None:
                candidate_bodies[scope.number] = str(raw.get("body") or "")
                candidate_updated_at[scope.number] = raw.get("updatedAt")

        source_complete = not saturated
        source_status = SourceStatus(
            issue_search=SOURCE_SATURATED if saturated else SOURCE_OK,
            issue_readback=SOURCE_OK,
            child_plan="absent",
        )

        result = classify_overlap(current, candidates, source_status)
        route, candidate_meta = _route_for_result(
            result, current_body=current_body, candidate_bodies=candidate_bodies
        )
        if route not in ROUTES:
            route = ROUTE_RUNTIME_ERROR

        evidence = build_evidence(
            current_number=args.issue_number,
            current_body=current_body,
            current_updated_at=current_raw.get("updatedAt"),
            current_paths=current_paths,
            source_complete=source_complete,
            source_saturated=saturated,
            collected_at=collected_at,
            result=result,
            route=route,
            candidate_meta=candidate_meta,
            candidate_updated_at=candidate_updated_at,
            candidate_bodies=candidate_bodies,
        )
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return ROUTE_EXIT_CODES[route]
    except OverlapRuntimeError as exc:
        error_body = {
            "schema": SCHEMA,
            "route": ROUTE_RUNTIME_ERROR,
            "error": str(exc),
        }
        print(json.dumps(error_body, ensure_ascii=False, indent=2))
        return ROUTE_EXIT_CODES[ROUTE_RUNTIME_ERROR]
    except (ValueError, TypeError, KeyError, AssertionError) as exc:
        # 内部契約違反（schema / 型不整合）も fail-closed で runtime_error に倒す
        error_body = {
            "schema": SCHEMA,
            "route": ROUTE_RUNTIME_ERROR,
            "error": f"unexpected internal error: {exc!r}",
        }
        print(json.dumps(error_body, ensure_ascii=False, indent=2))
        return ROUTE_EXIT_CODES[ROUTE_RUNTIME_ERROR]


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
