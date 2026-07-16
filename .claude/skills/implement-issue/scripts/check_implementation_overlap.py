#!/usr/bin/env python3
"""
check_implementation_overlap.py

`implement-issue` 専用の contract-aware overlap preflight adapter（#1452、
PR #1455 レビュー修正版）。

`check_issue_overlap.py`（`.claude/skills/create-issue/scripts/`）の pure
classifier（`classify_overlap` / `IssueScope` / `SourceStatus` / path
normalization）を正本として再利用し、それ自体の scoring / schema ロジックは
変更しない（#1452 の Out of Scope）。

本 adapter が持つ責務（implementation 専用の候補収集レイヤー + collision
判定の強化）:

- `--issue-number` を必須にし、対象 Issue 自身を候補から自己除外する。
- `phase/implementation` ラベルが付いた OPEN Issue を列挙する
  （`gh issue list` オンライン経路、または offline fixture 経路）。
- Machine-Readable Contract の `blocked_by` / `depends_on` / `supersedes`
  （YAML list）、legacy `Depends on #N` 記法、GitHub native dependency
  （`blockedBy` / `blocking`）を統合的に解析し、predecessor の実 state
  （OPEN/CLOSED）に基づいて C2a / C2b を分岐する。predecessor が候補プール
  に存在しない場合は個別 readback（オンライン時）または fail-closed（offline
  時に未解決）で扱う。
- 全候補の本文から Allowed Paths をローカルで抽出する。
- 明示的な取得上限と saturation 検出を持ち、全件性を証明できない場合は
  fail-closed にする（`check_issue_overlap.py` の `SourceStatus` にマップ）。
- candidate readback（``## Outcome`` / ``## In Scope`` / ``## Out of Scope`` /
  ``## Delivery Rule``）に加えて、AC ID・output schema 名・
  Machine-Readable Contract の key/value・edit target（In Scope 内の
  inline-code パス）・goal_ref・supersession を構造的シグナルとして評価する
  （**collision 判定の唯一根拠を自然言語類似度にしない**、Blocker 1）。
- `Allowed Paths` が同一集合であることは duplicate の十分条件にしない
  （Blocker 3）。`same_path_set` に基づく `duplicate` verdict は
  readback + 構造シグナルによる確認を経て初めて `duplicate` route を確定する。
  確認できない場合は C1 と同様に扱う。
- `IMPLEMENT_SCOPE_COLLISION_PREFLIGHT_V1` evidence を構造化出力する（AC8、
  Major 3/4 修正版: candidate ごとの `policy_class` / `reasons` /
  `machine_readable_keys_intersection` / `change_kind_equal`、
  `decision_inputs_sha256`（時刻非依存）と `evidence_sha256`（時刻含む）の
  分離）。
- 全 candidate（number/body/updatedAt/Allowed Paths/dependency contract の
  schema）を検証し、一件でも欠ければ `human_review_required` に倒す
  （Major 5）。

## exit code 契約（AC7 改訂、Major 2）

分類処理が成功した場合（`route` が closed set のいずれかに決定できた場合）は
**すべての route で exit 0** を返す。route の正本は常に JSON 出力の
`route` フィールドである（`$?` を継続判定に使わない）。GitHub 取得失敗 /
JSON・schema 破損時のみ非 0（exit 1、`runtime_error`）を返す。

| route                            | exit |
|-----------------------------------|------|
| proceed（C0）                      | 0    |
| proceed_with_collision_evidence   | 0    |
| wait_for_predecessor（C2b）        | 0    |
| human_review_required             | 0    |
| duplicate                         | 0    |
| runtime_error                     | 1    |
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
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
    PathScopeKind,
    SourceStatus,
    allowed_paths_overlap,
    classify_overlap,
    classify_path_scope_kind,
    extract_allowed_path_entries,
    extract_allowed_paths,
    normalize_path,
    paths_conflict,
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

# Major 2: 分類が成功した場合は route を問わず exit 0。runtime_error だけ exit 1。
# JSON 出力の `route` フィールドが canonical であり、呼び出し側は $? を
# continue/stop の分岐条件に使ってはならない。
EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1

_HEADING_OVERLAP_THRESHOLD = 0.5

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[぀-ヿ一-鿿]")
_AC_ID_RE = re.compile(r"\bAC(\d+)\b")
_SCHEMA_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_V\d+\b")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LOW_SPECIFICITY_PATHS = frozenset({"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"})

# workflow.md の "Depends on #N" line-anchored dependency 表記（legacy）
_DEPENDS_ON_RE = re.compile(r"[Dd]epends\s+on\s+#(\d+)")
_PARENT_ISSUE_RE = re.compile(r"^parent_issue:\s*\"?#?(\d+)\"?", re.MULTILINE)

_DEPENDENCY_CONTRACT_KEYS = ("blocked_by", "depends_on", "supersedes")
_ALLOWED_PATHS_HEADING_RE = re.compile(r"^##\s+Allowed Paths\s*$", re.MULTILINE)


class OverlapRuntimeError(RuntimeError):
    """GitHub 取得失敗 / JSON 解析失敗 / schema 違反を表す fail-closed error。"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


_GITHUB_API_VERSION = "2026-03-10"
_GITHUB_API_ACCEPT_HEADER = "Accept: application/vnd.github+json"


def _canonicalize_repo_static(repo: Optional[str]) -> str:
    """`--repo owner/name` を固定の正規化規則（owner/name の小文字化）へ変換
    する（AC2/AC5、ネットワークアクセスなし）。`owner/name` 形式でない、または
    いずれかの segment が空の場合は fail-closed で `OverlapRuntimeError` を
    投げる。
    """
    raw = (repo or "").strip()
    if "/" not in raw:
        raise OverlapRuntimeError(f"invalid --repo value (expected owner/name): {repo!r}")
    owner, _, name = raw.partition("/")
    owner = owner.strip()
    name = name.strip()
    if not owner or not name or "/" in name:
        raise OverlapRuntimeError(f"invalid --repo value (expected owner/name): {repo!r}")
    return f"{owner.lower()}/{name.lower()}"


def _fetch_repository_full_name(repo: str) -> Optional[str]:
    """`GET /repos/{owner}/{repo}` から現在の canonical `full_name` を取得する
    （Major 2: repository rename / transfer 後の redirect を追跡する）。取得
    失敗・型不正時は None を返し、呼び出し側は静的正規化へ fallback する
    （必要十分なレベルの canonicalization。numeric repository ID の追跡までは
    本 Issue のスコープに含めない）。
    """
    try:
        data = _run_gh_json(
            [
                "gh",
                "api",
                f"repos/{repo}",
                "-H",
                _GITHUB_API_ACCEPT_HEADER,
                "-H",
                f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
            ]
        )
    except OverlapRuntimeError:
        return None
    if not isinstance(data, dict):
        return None
    full_name = data.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    return full_name


def _canonicalize_repo(repo: Optional[str], *, online: bool = False) -> str:
    """`--repo owner/name` を canonical value へ変換する（AC2/AC5、Major 2）。

    `online=True`（online fetch 経路）の場合は `GET /repos/{owner}/{repo}` の
    `full_name` を正本にし、rename / transfer 後の redirect を追跡する。API
    取得に失敗した場合、または `online=False`（dry-run 経路、ネットワーク
    アクセスを避ける）の場合は固定の正規化規則（小文字化）へ fallback する。
    """
    static = _canonicalize_repo_static(repo)
    if not online:
        return static
    fetched = _fetch_repository_full_name(static)
    if fetched:
        return _canonicalize_repo_static(fetched)
    return static


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


def _ref_digits(ref: Any) -> Optional[str]:
    s = str(ref).strip()
    m = re.search(r"(\d+)", s)
    return m.group(1) if m else None


def _extract_contract_block(body: str) -> str:
    match = re.search(
        r"^##\s+Machine-Readable Contract\s*$.*?```(?:yaml)?\s*(.+?)```",
        body or "",
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else ""


def _contract_schema_keys(body: str) -> Dict[str, str]:
    """`## Machine-Readable Contract` の fenced yaml から単純 key: value を読む。"""
    block = _extract_contract_block(body)
    if not block:
        return {}
    out: Dict[str, str] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"')
        if not value:
            # block-list 形式（次行以降の `- item`）は別関数で扱う。
            continue
        out[key] = value
    return out


def _parse_yaml_scalar_or_inline_list(raw_value: str) -> Tuple[str, ...]:
    raw_value = raw_value.strip()
    if not raw_value:
        return ()
    if raw_value.startswith("[") and raw_value.endswith("]"):
        inner = raw_value[1:-1]
        items = [x.strip().strip('"').strip("'") for x in inner.split(",")]
        return tuple(x for x in items if x)
    if raw_value.lower() in {"none", "null", "[]", '"none"'}:
        return ()
    return (raw_value.strip('"').strip("'"),)


def _parse_contract_dependency_lists(body: str) -> Dict[str, Tuple[str, ...]]:
    """`blocked_by` / `depends_on` / `supersedes` を inline-list / block-list
    どちらの YAML 表記でも解析する（Blocker 2）。値が壊れていても例外を投げず
    空 tuple を返す（fail-closed は呼び出し側の schema validation が担う）。
    """
    result: Dict[str, Tuple[str, ...]] = {k: () for k in _DEPENDENCY_CONTRACT_KEYS}
    block = _extract_contract_block(body)
    if not block:
        return result
    lines = block.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        matched_key = None
        for key in _DEPENDENCY_CONTRACT_KEYS:
            prefix = f"{key}:"
            if stripped.startswith(prefix):
                matched_key = key
                rest = stripped[len(prefix) :].strip()
                break
        if matched_key is None:
            i += 1
            continue
        if rest:
            result[matched_key] = _parse_yaml_scalar_or_inline_list(rest)
            i += 1
            continue
        # block-list 形式: 次行以降の `- item` を集める
        items: List[str] = []
        j = i + 1
        while j < n:
            item_stripped = lines[j].strip()
            if item_stripped.startswith("- "):
                items.append(item_stripped[2:].strip().strip('"').strip("'"))
                j += 1
                continue
            break
        result[matched_key] = tuple(items)
        i = j
    return result


def _extract_native_dependency_numbers(
    raw: Dict[str, Any], key: str, *, current_repo: Optional[str] = None
) -> Tuple[str, ...]:
    """GitHub native issue dependency（`blockedBy` / `blocking`）を解析する。

    typed record（`{"repository": "...", "number": N, "state": "..."}`、REST
    issue-dependencies endpoint 由来、Blocker 4/5）と、legacy fixture の簡易形
    （`[N, ...]` / `[{"number": N}, ...]`、repository 情報なし）の両方を許容
    する。typed record が `repository` を持ち、かつ `current_repo` が指定され
    ている場合は同一 repository 制約を検証する。不一致は ``repo#number`` の
    namespaced ref として残し、ローカル Issue 番号と誤結合せず unresolved
    dependency として fail-closed にする。
    """
    val = raw.get(key)
    if not isinstance(val, list):
        return ()
    out: List[str] = []
    for item in val:
        if isinstance(item, dict) and "number" in item:
            repo_field = item.get("repository")
            if repo_field is not None and current_repo is not None:
                if str(repo_field).lower() != current_repo.lower():
                    digits = _ref_digits(item["number"])
                    if digits:
                        out.append(f"{str(repo_field).lower()}#{digits}")
                    continue
            digits = _ref_digits(item["number"])
        elif isinstance(item, (int, str)):
            digits = _ref_digits(item)
        else:
            digits = None
        if digits:
            out.append(digits)
    return tuple(out)


def _merge_dependency_refs(
    body: str, raw: Dict[str, Any], key: str, *, current_repo: Optional[str] = None
) -> Tuple[str, ...]:
    """legacy `Depends on #N` + contract YAML + native dependency を統合する。

    `key == "blocked_by"` の場合のみ、Machine-Readable Contract の
    `blocked_by:` に加えて legacy `Depends on #N` 記法と GitHub native
    `blockedBy` を統合する。それ以外の key（`depends_on` / `supersedes`）は
    contract の YAML 値のみを正本とする。`current_repo` は native dependency
    typed record の同一 repository 制約検証に使う（Blocker 4/5）。
    """
    contract = _parse_contract_dependency_lists(body)
    seen: set = set()
    merged: List[str] = []
    for ref in contract.get(key, ()):
        digits = _ref_digits(ref)
        if digits and digits not in seen:
            seen.add(digits)
            merged.append(digits)
    if key == "blocked_by":
        for digits in _DEPENDS_ON_RE.findall(body or ""):
            if digits not in seen:
                seen.add(digits)
                merged.append(digits)
        for digits in _extract_native_dependency_numbers(raw, "blockedBy", current_repo=current_repo):
            if digits not in seen:
                seen.add(digits)
                merged.append(digits)
    return tuple(merged)


def _extract_depends_on(body: str) -> Tuple[str, ...]:
    """body 中の line-anchored ``Depends on #N`` を抽出する（workflow.md 準拠、legacy）。"""
    return tuple(sorted(set(_DEPENDS_ON_RE.findall(body or ""))))


def _extract_parent_ref(body: str) -> Tuple[str, ...]:
    """``## Machine-Readable Contract`` の ``parent_issue: "#N"`` を抽出する。"""
    match = _PARENT_ISSUE_RE.search(body or "")
    return (match.group(1),) if match else ()


def _extract_ac_ids(body: str) -> Tuple[str, ...]:
    section = _extract_section(body, "Acceptance Criteria") or (body or "")
    return tuple(sorted({f"AC{n}" for n in _AC_ID_RE.findall(section)}))


def _extract_schema_names(body: str) -> Tuple[str, ...]:
    return tuple(sorted(set(_SCHEMA_NAME_RE.findall(body or ""))))


def _extract_edit_targets(body: str) -> Tuple[str, ...]:
    scope = _extract_section(body, "In Scope")
    outcome = _extract_section(body, "Outcome")
    text = f"{scope}\n{outcome}"
    targets = {m for m in _INLINE_CODE_RE.findall(text) if "/" in m or "." in m}
    return tuple(sorted(targets))


def _is_low_specificity_edit_target(target: str) -> bool:
    return target.split("#", 1)[0] in _LOW_SPECIFICITY_PATHS and "#" not in target


@dataclass(frozen=True)
class SignalDecision:
    """closed rule matrix の判定結果。

    weak reason と collision verdict を別々に導くと監査 evidence が矛盾する。
    この値だけを _readback_candidate() の判定・reason に用いる。
    """

    weak_path_signals: Tuple[str, ...]
    strong_path_signals: Tuple[str, ...]
    independent_structural_signals: Tuple[str, ...]
    text_signal: bool
    has_collision: bool
    reasons: Tuple[str, ...]


# boilerplate Machine-Readable Contract キー。ほぼ全 Issue で値が一致するため
# collision シグナルとしては使わない（false positive 防止）。参考情報として
# `machine_readable_keys_intersection` には含めるが `has_structural_collision`
# の根拠にはしない。
_GENERIC_CONTRACT_KEYS = frozenset({"contract_schema_version", "issue_kind", "parent_issue", "change_kind"})


def _contract_key_value_intersection(a: Dict[str, str], b: Dict[str, str]) -> Tuple[str, ...]:
    shared = sorted(k for k in (set(a) & set(b)) if a[k] == b[k])
    return tuple(shared)


def _meaningful_contract_key_intersection(a: Dict[str, str], b: Dict[str, str]) -> Tuple[str, ...]:
    """`_contract_key_value_intersection` から boilerplate key を除いたもの。
    collision 判定の根拠として使うのはこちら。"""
    return tuple(k for k in _contract_key_value_intersection(a, b) if k not in _GENERIC_CONTRACT_KEYS)


def _structural_collision_signals(
    current_number: Optional[int],
    current_body: str,
    current_contract: Dict[str, str],
    cand_number: Optional[int],
    cand_body: str,
    cand_contract: Dict[str, str],
) -> Dict[str, Any]:
    """collision 判定の構造的シグナル（Blocker 1: 自然言語類似度を唯一根拠にしない）。"""
    shared_ac = tuple(sorted(set(_extract_ac_ids(current_body)) & set(_extract_ac_ids(cand_body))))
    shared_schema = tuple(sorted(set(_extract_schema_names(current_body)) & set(_extract_schema_names(cand_body))))
    shared_targets = tuple(sorted(set(_extract_edit_targets(current_body)) & set(_extract_edit_targets(cand_body))))
    shared_low_specificity_targets = tuple(
        target for target in shared_targets if _is_low_specificity_edit_target(target)
    )
    shared_specific_targets = tuple(target for target in shared_targets if target not in shared_low_specificity_targets)
    mrc_intersection = _contract_key_value_intersection(current_contract, cand_contract)
    meaningful_mrc_intersection = _meaningful_contract_key_intersection(current_contract, cand_contract)
    shared_goal_ref = bool(
        current_contract.get("goal_ref") and current_contract.get("goal_ref") == cand_contract.get("goal_ref")
    )
    edit_intent_match = bool(
        current_contract.get("edit_intent") and current_contract.get("edit_intent") == cand_contract.get("edit_intent")
    )

    cur_deps = _parse_contract_dependency_lists(current_body)
    cand_deps = _parse_contract_dependency_lists(cand_body)
    cur_key = _ref_digits(current_number) if current_number is not None else None
    cand_key = _ref_digits(cand_number) if cand_number is not None else None
    explicit_supersession = bool(
        (cand_key and cand_key in cur_deps.get("supersedes", ()))
        or (cur_key and cur_key in cand_deps.get("supersedes", ()))
    )

    # ordinal AC ID は無関係な implementation issue にも反復するため evidence
    # 専用の weak signal とし、strong collision の根拠には使わない。
    has_signal = bool(
        shared_schema or shared_specific_targets or shared_goal_ref or edit_intent_match or meaningful_mrc_intersection
    )

    return {
        "shared_ac_ids": list(shared_ac),
        "shared_output_schema": list(shared_schema),
        "shared_edit_targets": list(shared_targets),
        "shared_low_specificity_edit_targets": list(shared_low_specificity_targets),
        "shared_specific_edit_targets": list(shared_specific_targets),
        "machine_readable_keys_intersection": list(mrc_intersection),
        "shared_goal_ref": shared_goal_ref,
        "edit_intent_match": edit_intent_match,
        "explicit_supersession": explicit_supersession,
        "has_structural_collision": has_signal and not explicit_supersession,
    }


def _path_signal_reasons(current_body: str, cand_body: str) -> Tuple[str, ...]:
    """raw path kind から closed matrix 用の weak path reason を返す。

    ``*_only`` は overlap の一部に weak pair があるという意味ではなく、全ての
    observed path overlap がその weak class に属する場合だけ出力する。
    """
    overlap_classes: List[str] = []
    for current_entry in extract_allowed_path_entries(current_body):
        for candidate_entry in extract_allowed_path_entries(cand_body):
            current_path = normalize_path(current_entry)
            candidate_path = normalize_path(candidate_entry)
            if not paths_conflict(current_entry, candidate_entry):
                continue
            current_kind = classify_path_scope_kind(current_entry)
            candidate_kind = classify_path_scope_kind(candidate_entry)
            if current_path == candidate_path and current_path in _LOW_SPECIFICITY_PATHS:
                overlap_classes.append("low_specificity")
            elif current_path != candidate_path and (
                current_kind in {PathScopeKind.DIRECTORY, PathScopeKind.RECURSIVE_GLOB}
                or candidate_kind in {PathScopeKind.DIRECTORY, PathScopeKind.RECURSIVE_GLOB}
            ):
                overlap_classes.append("broad_prefix")
            else:
                overlap_classes.append("specific")
    if overlap_classes and set(overlap_classes) == {"low_specificity"}:
        return ("low_specificity_path_only",)
    if overlap_classes and set(overlap_classes) == {"broad_prefix"}:
        return ("broad_prefix_only",)
    return ()


def _signal_decision(
    structural: Dict[str, Any], *, text_signal: bool, weak_path_reasons: Tuple[str, ...]
) -> SignalDecision:
    """weak / strong reason と collision を一つの closed rule matrix で確定する。"""
    independent: List[str] = []
    if structural.get("shared_output_schema"):
        independent.append("shared_output_schema")
    if structural.get("shared_specific_edit_targets"):
        independent.append("shared_specific_edit_target")
    if structural.get("shared_goal_ref"):
        independent.append("shared_goal_ref")
    if structural.get("edit_intent_match"):
        independent.append("shared_edit_intent")
    meaningful_keys = [
        key for key in structural.get("machine_readable_keys_intersection", []) if key not in _GENERIC_CONTRACT_KEYS
    ]
    if meaningful_keys:
        independent.append("shared_machine_readable_key")

    explicit_supersession = bool(structural.get("explicit_supersession"))
    has_collision = bool(independent or text_signal) and not explicit_supersession
    reasons: List[str] = []
    if has_collision and independent:
        reasons.append("independent_strong_signal_detected")
    elif not has_collision:
        reasons.extend(weak_path_reasons)
        if structural.get("shared_ac_ids"):
            reasons.append("ordinal_ac_id_only")
    return SignalDecision(
        weak_path_signals=weak_path_reasons,
        strong_path_signals=tuple(independent),
        independent_structural_signals=tuple(independent),
        text_signal=text_signal,
        has_collision=has_collision,
        reasons=tuple(reasons),
    )


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
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels,updatedAt,url",
        ]
    )
    if not isinstance(data, dict) or "body" not in data:
        raise OverlapRuntimeError("current issue readback missing body")
    return data


def fetch_implementation_candidates(repo: str, limit: int) -> Tuple[List[Dict[str, Any]], bool]:
    """OPEN かつ ``phase/implementation`` ラベルを持つ Issue を列挙する。

    ``limit`` は stored/fresh evidence の収集境界であり、上限到達は
    fail-closed に扱う。全ページ取得と collection metadata の producer /
    consumer 契約は #1493 の責務であり、本 Issue では変更しない。
    """
    data = _run_gh_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            "phase/implementation",
            "--state",
            "open",
            "--json",
            "number,title,body,labels,updatedAt,url",
            "--limit",
            str(limit),
        ]
    )
    if not isinstance(data, list):
        raise OverlapRuntimeError("gh issue list did not return a JSON array")
    return data, len(data) >= limit


def fetch_predecessor_issue(repo: str, number: int) -> Optional[Dict[str, Any]]:
    """明示 dependency 参照先の predecessor を個別 readback する（Blocker 2）。

    OPEN 一覧に含まれない（= CLOSED 等の）predecessor の実 state を得るために
    使う。取得失敗は None を返し呼び出し側で `dependency_unresolved` として
    fail-closed に扱う（例外で preflight 全体を落とさない）。
    """
    try:
        data = _run_gh_json(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "number,title,body,labels,updatedAt,url,state",
            ]
        )
    except OverlapRuntimeError:
        return None
    if not isinstance(data, dict) or "body" not in data:
        return None
    return data


_NATIVE_DEPENDENCY_DIRECTIONS: Dict[str, str] = {
    "blocked_by": "blocked_by",
    "blocking": "blocking",
}
_NATIVE_DEPENDENCY_PAGE_SIZE = 100
_NATIVE_DEPENDENCY_MAX_PAGES = 1000  # safety cap; fail-closed if exceeded


_REPOSITORY_URL_RE = re.compile(r"^https://api\.github\.com/repos/([^/]+)/([^/]+)$")
_NATIVE_DEPENDENCY_VALID_STATES = frozenset({"open", "closed"})


def _repository_from_repository_url(value: Any) -> Optional[str]:
    """公式 REST issue-dependencies endpoint が返す `repository_url`
    （例: ``https://api.github.com/repos/octocat/Hello-World``）から
    `owner/name` を抽出する（Blocker 1）。GitHub の公式レスポンスに
    `repository: {"full_name": ...}` という nested object は存在しない
    （https://docs.github.com/en/rest/issues/issue-dependencies）。URL path
    が厳密に `/repos/{owner}/{repo}` 形式でない場合は None を返す。
    """
    if not isinstance(value, str):
        return None
    match = _REPOSITORY_URL_RE.match(value.strip())
    if not match:
        return None
    owner, name = match.group(1), match.group(2)
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def _normalize_native_dependency_record(item: Any, *, direction: str) -> Dict[str, Any]:
    """REST issue-dependencies endpoint の 1 レコードを
    `{repository, number, state}` typed record へ正規化する（Blocker 1、
    Major 3）。

    公式契約どおり repository identity は `repository_url` から抽出する
    （nested `repository.full_name` は公式レスポンスに存在しない架空の形の
    ため受理しない）。`number` は `type(number) is int`（`bool` を除外し
    正の整数のみ）、`state` は `open` / `closed`（大小文字非依存）のいずれか
    のみを許容し、それ以外は「依存なし」として黙って握りつぶさず fail-closed
    （`OverlapRuntimeError`）にする（Outcome 要件）。
    """
    if not isinstance(item, dict):
        raise OverlapRuntimeError(f"native dependency ({direction}) record is not an object: {item!r}")
    number = item.get("number")
    state = item.get("state")
    repository = _repository_from_repository_url(item.get("repository_url"))
    number_valid = type(number) is int and number > 0
    state_valid = isinstance(state, str) and state.strip().lower() in _NATIVE_DEPENDENCY_VALID_STATES
    if not number_valid or not repository or not state_valid:
        raise OverlapRuntimeError(
            f"native dependency ({direction}) record missing/invalid number/repository_url/state: {item!r}"
        )
    return {
        "repository": _canonicalize_repo_static(repository),
        "number": number,
        "state": state.strip().upper(),
    }


def _fetch_native_dependency_page(repo: str, issue_number: int, direction: str, page: int) -> List[Any]:
    endpoint = f"repos/{repo}/issues/{issue_number}/dependencies/{direction}"
    data = _run_gh_json(
        [
            "gh",
            "api",
            endpoint,
            "-X",
            "GET",
            "-H",
            _GITHUB_API_ACCEPT_HEADER,
            "-H",
            f"X-GitHub-Api-Version: {_GITHUB_API_VERSION}",
            "-f",
            f"per_page={_NATIVE_DEPENDENCY_PAGE_SIZE}",
            "-f",
            f"page={page}",
        ]
    )
    if not isinstance(data, list):
        raise OverlapRuntimeError(f"native dependency endpoint returned non-array: {endpoint} page={page}")
    return data


def fetch_native_dependencies(repo: str, issue_number: int, direction: str) -> Tuple[Dict[str, Any], ...]:
    """GitHub native issue dependency（既定: REST issue-dependencies endpoint）を
    全ページ取得する（Blocker 5、pagination boundary の完全性証明）。

    `direction` は `"blocked_by"` または `"blocking"`。API 失敗・ページ欠落・
    レスポンス形状不正は「依存なし」として扱わず fail-closed で例外を投げる。
    """
    if direction not in _NATIVE_DEPENDENCY_DIRECTIONS:
        raise OverlapRuntimeError(f"unknown native dependency direction: {direction}")
    records: List[Dict[str, Any]] = []
    page = 1
    while True:
        raw_page = _fetch_native_dependency_page(repo, issue_number, direction, page)
        records.extend(_normalize_native_dependency_record(item, direction=direction) for item in raw_page)
        if len(raw_page) < _NATIVE_DEPENDENCY_PAGE_SIZE:
            break
        page += 1
        if page > _NATIVE_DEPENDENCY_MAX_PAGES:
            raise OverlapRuntimeError(f"native dependency pagination exceeded safety cap for {direction}")
    return tuple(records)


def fetch_all_native_dependencies(repo: str, issue_number: int) -> Dict[str, Tuple[Dict[str, Any], ...]]:
    """`blockedBy` / `blocking` の両方向を取得する。混同せず別々のキーで返す。"""
    return {
        "blockedBy": fetch_native_dependencies(repo, issue_number, "blocked_by"),
        "blocking": fetch_native_dependencies(repo, issue_number, "blocking"),
    }


def _classifier_allowed_paths(entries: Sequence[str]) -> Tuple[str, ...]:
    """pure classifier に渡す path を one-level glob semantics へ写像する。

    IssueScope は normalized path だけを保持するため、``tests/*`` が通常の
    directory prefix に畳まれるのを防ぐ。evidence には原文 path を保ち、ここは
    classifier 内部用の決定論的 sentinel に限定する。
    """
    out: List[str] = []
    for entry in entries:
        raw = entry.strip()
        if raw.endswith("/*") and not raw.endswith("/**"):
            out.append(f"{normalize_path(raw)}/__single_level_glob__")
        else:
            out.append(raw)
    return tuple(out)


def _issue_scope_from_raw(raw: Dict[str, Any], *, current_repo: Optional[str] = None) -> IssueScope:
    body = str(raw.get("body") or "")
    return IssueScope(
        title=str(raw.get("title", "")),
        number=raw.get("number"),
        url=str(raw.get("url", "") or ""),
        allowed_paths=_classifier_allowed_paths(extract_allowed_path_entries(body)),
        body=raw.get("body"),
        labels=tuple(
            (lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)) for lbl in (raw.get("labels", []) or [])
        ),
        depends_on=_merge_dependency_refs(body, raw, "blocked_by", current_repo=current_repo),
        parent_refs=_extract_parent_ref(body),
        # B1 fix (Blocker 2): state は raw の実際の値を使う。offline fixture は
        # "state" を明示できる（既定 "OPEN" は後方互換）。online 経路は
        # `gh issue list --state open` 由来（既定 OPEN）または個別 readback
        # （`fetch_predecessor_issue` 由来、実 state）のいずれか。
        state=str(raw.get("state", "OPEN") or "OPEN"),
        search_hit=True,
    )


# ============================================================
# candidate schema validation（Major 5）
# ============================================================


def _validate_candidate_schema(raw: Dict[str, Any]) -> List[str]:
    """1 candidate の raw JSON を schema validate する。エラーメッセージの
    リストを返す（空なら valid）。number/body/updatedAt/Allowed Paths/
    dependency contract のいずれかが欠けている、または壊れている候補は
    false positive として黙って除外せず `human_review_required` に倒す。
    """
    errors: List[str] = []
    number = raw.get("number")
    if not isinstance(number, int):
        errors.append("missing_or_invalid_number")
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        errors.append("missing_body")
        body = ""
    updated_at = raw.get("updatedAt")
    if not updated_at:
        errors.append("missing_updated_at")
    if body:
        # 見出しが存在しない legacy Issue だけを classifier 入力から除外できる。
        # 見出しがあるのに空/コメントだけ/正規化不能なら、scope が不明なため
        # fail-closed にする。
        has_allowed_paths_heading = _ALLOWED_PATHS_HEADING_RE.search(body) is not None
        paths = extract_allowed_paths(body)
        if not has_allowed_paths_heading:
            errors.append("missing_allowed_paths")
        elif not paths or any(path in {"-", "*", "+"} for path in paths):
            errors.append("invalid_allowed_paths")
        # Machine-Readable Contract が存在する場合は壊れていないことだけ確認
        # （存在しない contract は許容 — legacy issue 互換）。
        try:
            _parse_contract_dependency_lists(body)
            _contract_schema_keys(body)
        except Exception:  # pragma: no cover - defensive fail-closed
            errors.append("malformed_dependency_contract")
    return errors


# ============================================================
# candidate readback（AC4 + Blocker 1/3: structural signal を主軸にする）
# ============================================================


def _readback_candidate(
    current_number: Optional[int],
    current_body: str,
    current_contract: Dict[str, str],
    cand_number: Optional[int],
    cand_body: str,
    cand_contract: Dict[str, str],
) -> Dict[str, Any]:
    """candidate の readback を行い collision 判定に必要な evidence を返す。

    `heading_overlap`（= collision と扱うか）は構造シグナル（AC/schema/
    edit-target/goal_ref/MRC key 一致）を主軸とし、自然言語類似度
    （Outcome の token Jaccard）は **唯一の根拠にしない**補助 signal として
    のみ使う（Blocker 1）。構造シグナルが「衝突なし」でも、自然言語類似度が
    高い場合は保守的に collision とみなす（fail-closed 側に倒す）。
    """
    cand_outcome = _extract_section(cand_body, "Outcome")
    cand_in_scope = _extract_section(cand_body, "In Scope")
    if not cand_outcome or not cand_in_scope:
        return {
            "readback_complete": False,
            "heading_overlap": False,
            "text_similarity": 0.0,
            "structural_signals": {},
            "non_conflict_reason": None,
        }

    cur_outcome = _extract_section(current_body, "Outcome")
    text_similarity = _jaccard(cur_outcome, cand_outcome)
    text_signal = text_similarity >= _HEADING_OVERLAP_THRESHOLD

    structural = _structural_collision_signals(
        current_number,
        current_body,
        current_contract,
        cand_number,
        cand_body,
        cand_contract,
    )
    weak_path_reasons = _path_signal_reasons(current_body, cand_body)
    decision = _signal_decision(
        structural,
        text_signal=text_signal,
        weak_path_reasons=weak_path_reasons,
    )
    collision = decision.has_collision
    structural["has_structural_collision"] = bool(
        decision.independent_structural_signals and not structural["explicit_supersession"]
    )
    structural["signal_decision"] = {
        "weak_path_signals": list(decision.weak_path_signals),
        "strong_path_signals": list(decision.strong_path_signals),
        "independent_structural_signals": list(decision.independent_structural_signals),
        "text_signal": decision.text_signal,
        "has_collision": decision.has_collision,
        "reasons": list(decision.reasons),
    }
    non_conflict_reason = None
    if not collision:
        non_conflict_reason = (
            "candidate は current と構造的シグナル（AC/output schema/"
            "Machine-Readable Contract key/edit target/goal_ref）が一致せず、"
            f"Outcome の token overlap も {text_similarity:.2f} と低い。"
            "Allowed Paths の一致のみで、Outcome/In Scope は disjoint。"
        )
    elif structural["explicit_supersession"]:
        non_conflict_reason = (
            "candidate は current との supersedes/superseded-by 関係が明示されており、意図的な直列化と判定する。"
        )
        collision = False

    return {
        "readback_complete": True,
        "heading_overlap": collision,
        "text_similarity": text_similarity,
        "structural_signals": structural,
        "weak_signal_reasons": list(decision.reasons),
        "non_conflict_reason": non_conflict_reason,
    }


# ============================================================
# dependency 解決（Blocker 2: blocked_by/depends_on/native dependency + 実 state）
# ============================================================


def _resolve_dependency(
    current_number: int,
    current_paths: Sequence[str],
    blocked_by_refs: Sequence[str],
    scope_pool: Dict[str, IssueScope],
) -> Dict[str, Any]:
    """current の blocked_by refs を解決し、C2b（open predecessor）優先で判定する。

    Returns dict with:
      - blocking: Optional[dict] — open predecessor で Allowed Paths が重複する
        最初の 1 件（wait_for_predecessor route の根拠）。None なら該当なし。
      - closed_predecessors: 候補プールに存在し CLOSED かつ Allowed Paths が
        重複する predecessor の issue_number リスト（C2a track、readback 要）。
      - unresolved_refs: 候補プールに存在しない predecessor 参照
        （fail-closed の根拠）。
    """
    blocking: Optional[Dict[str, Any]] = None
    closed_predecessors: List[int] = []
    unresolved_refs: List[str] = []

    for ref in blocked_by_refs:
        scope = scope_pool.get(ref)
        if scope is None:
            unresolved_refs.append(ref)
            continue
        cand_paths = scope.effective_allowed_paths()
        if str(scope.state).upper() == "OPEN" and not cand_paths:
            unresolved_refs.append(ref)
            continue
        if not allowed_paths_overlap(current_paths, cand_paths):
            continue
        if str(scope.state).upper() == "OPEN":
            if blocking is None:
                blocking = {"issue_number": scope.number, "state": "OPEN"}
        else:
            if scope.number is not None:
                closed_predecessors.append(scope.number)

    return {
        "blocking": blocking,
        "closed_predecessors": closed_predecessors,
        "unresolved_refs": unresolved_refs,
    }


# ============================================================
# evidence 構築（AC8, Major 3/4 修正版）
# ============================================================


def build_evidence(
    *,
    repository: str,
    current_number: int,
    current_body: str,
    current_updated_at: Optional[str],
    current_paths: Sequence[str],
    source_complete: bool,
    source_saturated: bool,
    source_limit: int,
    collected_at: str,
    candidates_evidence: List[Dict[str, Any]],
    ignored_candidates: List[Dict[str, Any]],
    dependency_resolution: Dict[str, Any],
    validation_errors: Dict[int, List[str]],
    route: str,
) -> Dict[str, Any]:
    # 候補は issue_number で canonical sort（順序非依存性、Major 4）
    ordered = sorted(
        candidates_evidence,
        key=lambda c: (c["issue_number"] is None, c["issue_number"] or 0),
    )
    ordered_ignored = sorted(ignored_candidates, key=lambda c: c["issue_number"])

    decision_payload: Dict[str, Any] = {
        "schema": SCHEMA,
        "repository": repository,
        "current_issue": {
            "number": current_number,
            "body_sha256": f"sha256:{_sha256(current_body)}",
            "allowed_paths": sorted(current_paths),
        },
        "source": {
            "complete": source_complete,
            "saturated": source_saturated,
            "limit": source_limit,
        },
        "candidates": ordered,
        "ignored_candidates": ordered_ignored,
        "dependency_resolution": dependency_resolution,
        "validation_errors": {str(k): v for k, v in sorted(validation_errors.items())},
        "route": route,
    }
    decision_inputs_sha256 = f"sha256:{_sha256(_canonical_json(decision_payload))}"

    body: Dict[str, Any] = {
        "schema": SCHEMA,
        "repository": repository,
        "current_issue": {
            "number": current_number,
            "updated_at": current_updated_at,
            "body_sha256": f"sha256:{_sha256(current_body)}",
            "allowed_paths": list(current_paths),
        },
        "source": {
            "complete": source_complete,
            "saturated": source_saturated,
            "limit": source_limit,
            "collected_at": collected_at,
        },
        "candidates": ordered,
        "ignored_candidates": ordered_ignored,
        "dependency_resolution": dependency_resolution,
        "validation_errors": {str(k): v for k, v in sorted(validation_errors.items())},
        "route": route,
        "decision_inputs_sha256": decision_inputs_sha256,
    }
    canonical = _canonical_json(body)
    body["evidence_sha256"] = f"sha256:{_sha256(canonical)}"
    return body


# ============================================================
# route 判定
# ============================================================


def _classify(
    *,
    args: argparse.Namespace,
    current_raw: Dict[str, Any],
    candidates_raw: List[Dict[str, Any]],
    saturated: bool,
    repo: Optional[str],
    repository: str,
) -> Tuple[str, Dict[str, Any]]:
    current_body = str(current_raw.get("body") or "")
    current_paths = extract_allowed_path_entries(current_body)
    current_contract = _contract_schema_keys(current_body)

    # --- 全 candidate の schema validation（Major 5） ---
    # Allowed Paths 未記載だけの legacy candidate は、collision classifier の
    # 比較対象にはできない。その候補は validation error にせず、evidence に
    # provenance を残して classifier 入力から除外する。number/body/updatedAt/
    # dependency contract など他の schema error が併存する場合は従来どおり
    # fail-closed とする。
    validation_errors: Dict[int, List[str]] = {}
    ignored_candidates: List[Dict[str, Any]] = []
    comparable_raw: List[Dict[str, Any]] = []
    for raw in candidates_raw:
        number = raw.get("number")
        errors = _validate_candidate_schema(raw)
        if errors == ["missing_allowed_paths"] and isinstance(number, int):
            body = raw.get("body")
            audit_payload = {
                "issue_number": number,
                "reason": "ignored_missing_allowed_paths",
                "url": raw.get("url") if isinstance(raw.get("url"), str) else None,
                "updated_at": raw.get("updatedAt"),
                "body_sha256": (f"sha256:{_sha256(body)}" if isinstance(body, str) and body else None),
            }
            ignored_candidates.append(
                {
                    **audit_payload,
                    "decision_sha256": f"sha256:{_sha256(_canonical_json(audit_payload))}",
                }
            )
            continue
        if errors:
            key = number if isinstance(number, int) else -1
            validation_errors[key] = errors
        comparable_raw.append(raw)

    candidate_bodies: Dict[str, str] = {}
    candidate_updated_at: Dict[str, Optional[str]] = {}
    candidate_contracts: Dict[str, Dict[str, str]] = {}
    scope_pool: Dict[str, IssueScope] = {}
    candidate_scopes: List[IssueScope] = []
    for raw in candidates_raw:
        scope = _issue_scope_from_raw(raw, current_repo=repository)
        if scope.number is not None:
            key = str(scope.number)
            body_text = str(raw.get("body") or "")
            candidate_bodies[key] = body_text
            candidate_updated_at[key] = raw.get("updatedAt")
            candidate_contracts[key] = _contract_schema_keys(body_text)
            scope_pool[key] = scope
    for raw in comparable_raw:
        candidate_scopes.append(_issue_scope_from_raw(raw, current_repo=repository))

    current = IssueScope(
        title=str(current_raw.get("title", "")),
        number=args.issue_number,
        allowed_paths=_classifier_allowed_paths(current_paths),
        body=current_body,
        depends_on=_merge_dependency_refs(current_body, current_raw, "blocked_by", current_repo=repository),
        parent_refs=_extract_parent_ref(current_body),
    )

    # --- dependency 解決（Blocker 2） ---
    dep_res = _resolve_dependency(args.issue_number, current_paths, current.depends_on, scope_pool)
    # Major 1（PR #1474 レビュー）: 取得済みの typed record（`{repository,
    # number, state}`）を issue number へ潰す前に auditability 用として保持
    # する。`native_blocking`（current が後続を止めている側）はこれまで取得
    # されるが判定に一切使われず捨てられていた（dead data）ため、evidence に
    # 残して可視化する（判定ロジックへの新規組み込みは Out of Scope）。
    native_blocked_by_raw = current_raw.get("blockedBy")
    native_blocking_raw = current_raw.get("blocking")
    dependency_resolution = {
        "blocked_by_refs": list(current.depends_on),
        "blocking_predecessor": dep_res["blocking"],
        "closed_predecessors": dep_res["closed_predecessors"],
        "unresolved_refs": dep_res["unresolved_refs"],
        "native_blocked_by": native_blocked_by_raw if isinstance(native_blocked_by_raw, list) else [],
        "native_blocking": native_blocking_raw if isinstance(native_blocking_raw, list) else [],
    }

    source_complete = not saturated
    source_status = SourceStatus(
        issue_search=SOURCE_SATURATED if saturated else SOURCE_OK,
        issue_readback=SOURCE_OK,
        child_plan="absent",
    )

    result = classify_overlap(current, candidate_scopes, source_status)

    # readback-required candidate 番号の union を組み立てる:
    #   1. classify_overlap が OVERLAP_REQUIRES_COMMENT / DUPLICATE と判定した候補
    #   2. dependency 解決で見つかった CLOSED predecessor（C2a track、Blocker 2）
    readback_targets: Dict[int, str] = {}  # issue_number -> "classify" | "dependency_c2a"
    verdict_candidates: List[Any] = (
        list(result.candidates) if result.verdict in {OVERLAP_REQUIRES_COMMENT, DUPLICATE} else []
    )
    for cand in verdict_candidates:
        if cand.issue_number is not None:
            readback_targets[cand.issue_number] = "classify"
    for num in dep_res["closed_predecessors"]:
        readback_targets.setdefault(num, "dependency_c2a")

    candidates_evidence: List[Dict[str, Any]] = []
    any_incomplete = False
    any_collision = False
    duplicate_confirmed = False
    duplicate_present = bool(verdict_candidates) and result.verdict == DUPLICATE

    overlapping_by_number: Dict[int, Tuple[str, ...]] = {
        c.issue_number: c.overlapping_paths for c in result.candidates if c.issue_number is not None
    }

    for num, origin in readback_targets.items():
        key = str(num)
        cand_body = candidate_bodies.get(key, "")
        cand_contract = candidate_contracts.get(key, {})
        rb = _readback_candidate(
            args.issue_number,
            current_body,
            current_contract,
            num,
            cand_body,
            cand_contract,
        )
        if not rb["readback_complete"]:
            any_incomplete = True
        elif rb["heading_overlap"]:
            any_collision = True

        policy_class = (
            "C2a"
            if origin == "dependency_c2a"
            else ("duplicate_candidate" if origin == "classify" and result.verdict == DUPLICATE else "C1")
        )
        reasons: List[str] = []
        if origin == "dependency_c2a":
            reasons.append("closed_predecessor_via_blocked_by")
        if rb["readback_complete"]:
            if rb["heading_overlap"]:
                reasons.append("structural_or_textual_collision_detected")
            else:
                reasons.append("readback_confirmed_disjoint")
        else:
            reasons.append("readback_incomplete_missing_outcome_or_in_scope")

        structural = rb.get("structural_signals") or {}
        contract_current = current_contract
        contract_cand = cand_contract
        change_kind_equal = bool(
            contract_current.get("change_kind")
            and contract_current.get("change_kind") == contract_cand.get("change_kind")
        )

        candidates_evidence.append(
            {
                "issue_number": num,
                "updated_at": candidate_updated_at.get(key),
                "body_sha256": f"sha256:{_sha256(cand_body)}" if cand_body else None,
                "overlapping_paths": list(overlapping_by_number.get(num, ())),
                "heading_overlap": rb["heading_overlap"],
                "readback_complete": rb["readback_complete"],
                "text_similarity": round(rb["text_similarity"], 4),
                "change_kind_equal": change_kind_equal,
                "machine_readable_keys_intersection": list(structural.get("machine_readable_keys_intersection", [])),
                "structural_signals": structural,
                "policy_class": policy_class,
                "reasons": reasons + list(rb.get("weak_signal_reasons") or []),
                "non_conflict_reason": rb["non_conflict_reason"],
            }
        )

        if origin == "classify" and result.verdict == DUPLICATE and rb["readback_complete"] and rb["heading_overlap"]:
            duplicate_confirmed = True

    # --- route 決定 ---
    route: str
    verdict = result.verdict

    if verdict not in {SAFE_NEW_ISSUE, OVERLAP_REQUIRES_COMMENT, AMBIGUOUS_REQUIRES_HUMAN, DUPLICATE}:
        return ROUTE_RUNTIME_ERROR, {
            "current_body": current_body,
            "current_paths": current_paths,
            "source_complete": source_complete,
            "saturated": saturated,
            "candidates_evidence": candidates_evidence,
            "ignored_candidates": ignored_candidates,
            "dependency_resolution": dependency_resolution,
            "validation_errors": validation_errors,
        }

    if dep_res["blocking"] is not None:
        route = ROUTE_WAIT_FOR_PREDECESSOR
    elif validation_errors:
        route = ROUTE_HUMAN_REVIEW_REQUIRED
    elif dep_res["unresolved_refs"]:
        route = ROUTE_HUMAN_REVIEW_REQUIRED
    elif verdict == AMBIGUOUS_REQUIRES_HUMAN:
        route = ROUTE_HUMAN_REVIEW_REQUIRED
    elif duplicate_present:
        # Blocker 3: same_path_set 由来の duplicate は readback 確認を経て
        # 初めて確定する。確認できなければ C1 相当として扱う。
        if duplicate_confirmed:
            route = ROUTE_DUPLICATE
        elif any_incomplete:
            route = ROUTE_HUMAN_REVIEW_REQUIRED
        elif any_collision:
            route = ROUTE_HUMAN_REVIEW_REQUIRED
        else:
            route = ROUTE_PROCEED_WITH_EVIDENCE
    elif readback_targets:
        if any_incomplete:
            route = ROUTE_HUMAN_REVIEW_REQUIRED
        elif any_collision:
            route = ROUTE_HUMAN_REVIEW_REQUIRED
        else:
            route = ROUTE_PROCEED_WITH_EVIDENCE
    elif verdict == SAFE_NEW_ISSUE:
        route = ROUTE_PROCEED
    else:
        # 契約上ここに来ないはずの組み合わせは runtime_error に倒す
        route = ROUTE_RUNTIME_ERROR

    return route, {
        "current_body": current_body,
        "current_paths": current_paths,
        "source_complete": source_complete,
        "saturated": saturated,
        "candidates_evidence": candidates_evidence,
        "ignored_candidates": ignored_candidates,
        "dependency_resolution": dependency_resolution,
        "validation_errors": validation_errors,
    }


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
            "implement-issue 専用の contract-aware overlap preflight （check_issue_overlap.py の classifier を再利用）"
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
        help="offline 用: 候補 Issue raw JSON 配列（number,title,body,labels,updatedAt,url,state）",
    )
    return p


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # --- Blocker 4: online / dry-run 両方の呼び出し経路で --repo を必須化
        #     する（AC1）。欠落時は runtime_error に倒す（evidence を出力しない）。
        if args.dry_run:
            if not args.current_file or not args.candidates_file:
                raise OverlapRuntimeError("--dry-run には --current-file と --candidates-file が必須")
            if not args.repo:
                raise OverlapRuntimeError("--repo is required for dry-run")
            canonical_repo = _canonicalize_repo(args.repo, online=False)
            current_raw = _load_json_file(args.current_file)
            candidates_raw = _load_json_file(args.candidates_file)
            saturated = len(candidates_raw) >= args.limit
            repo = args.repo
        else:
            if not args.repo:
                raise OverlapRuntimeError("--repo is required for online fetch")
            canonical_repo = _canonicalize_repo(args.repo, online=True)
            current_raw = fetch_current_issue(args.repo, args.issue_number)
            candidates_raw, saturated = fetch_implementation_candidates(args.repo, args.limit)
            repo = args.repo

            # --- Blocker 5: native issue dependency（blockedBy/blocking）を
            #     REST issue-dependencies endpoint で取得し、current issue の
            #     raw dict に typed record として付与する（gh issue view/list
            #     --json には存在しないフィールド名を使わない、AC6）。
            native_deps = fetch_all_native_dependencies(canonical_repo, args.issue_number)
            current_raw["blockedBy"] = [dict(rec) for rec in native_deps["blockedBy"]]
            current_raw["blocking"] = [dict(rec) for rec in native_deps["blocking"]]

        if not isinstance(current_raw, dict) or "body" not in current_raw:
            raise OverlapRuntimeError("current issue JSON missing required 'body' field")
        if not isinstance(candidates_raw, list):
            raise OverlapRuntimeError("candidates JSON must be an array")

        # --- 自己除外（AC6） ---
        candidates_raw = [c for c in candidates_raw if int(c.get("number", -1)) != args.issue_number]

        # --- Blocker 2: current が参照する predecessor が候補プールに
        #     存在しない場合、オンライン経路では個別 readback する。
        if not args.dry_run and repo:
            current_body_preview = str(current_raw.get("body") or "")
            refs = _merge_dependency_refs(current_body_preview, current_raw, "blocked_by", current_repo=canonical_repo)
            known_numbers = {str(c.get("number")) for c in candidates_raw}
            for ref in refs:
                if ref in known_numbers:
                    continue
                if not ref.isdigit():
                    # cross-repo native dependency は number-only readback に
                    # 接続できない。_resolve_dependency で unresolved として
                    # evidence 化し、route を fail-closed にする。
                    continue
                fetched = fetch_predecessor_issue(repo, int(ref))
                if fetched is not None:
                    candidates_raw.append(fetched)
                    known_numbers.add(ref)

        route, ctx = _classify(
            args=args,
            current_raw=current_raw,
            candidates_raw=candidates_raw,
            saturated=saturated,
            repo=repo,
            repository=canonical_repo,
        )

        # --- Blocker 2 (PR #1474 レビュー): candidate 側の native dependency
        #     も取得する。全 candidate への無条件 API 呼び出しは rate limit
        #     を悪化させるため、readback が必要と判定された候補（overlap
        #     candidate + 明示 dependency 参照先の predecessor）だけに限定する
        #     二段階取得にする。取得後、candidate の `blockedBy`/`blocking`
        #     typed record を反映した上で再分類し、current/candidate 双方向の
        #     predecessor・successor 関係を classifier に渡す。
        if not args.dry_run and repo:
            readback_numbers = sorted(
                {c["issue_number"] for c in ctx["candidates_evidence"] if isinstance(c.get("issue_number"), int)}
            )
            if readback_numbers:
                raw_by_number = {c.get("number"): c for c in candidates_raw if isinstance(c.get("number"), int)}
                fetched_numbers: List[int] = []
                for num in readback_numbers:
                    target_raw = raw_by_number.get(num)
                    if target_raw is None:
                        continue
                    cand_native_deps = fetch_all_native_dependencies(canonical_repo, num)
                    target_raw["blockedBy"] = [dict(rec) for rec in cand_native_deps["blockedBy"]]
                    target_raw["blocking"] = [dict(rec) for rec in cand_native_deps["blocking"]]
                    fetched_numbers.append(num)
                if fetched_numbers:
                    route, ctx = _classify(
                        args=args,
                        current_raw=current_raw,
                        candidates_raw=candidates_raw,
                        saturated=saturated,
                        repo=repo,
                        repository=canonical_repo,
                    )
                    ctx["dependency_resolution"]["native_dependency_candidates_fetched"] = fetched_numbers

        if route not in ROUTES:
            route = ROUTE_RUNTIME_ERROR

        evidence = build_evidence(
            repository=canonical_repo,
            current_number=args.issue_number,
            current_body=ctx["current_body"],
            current_updated_at=current_raw.get("updatedAt"),
            current_paths=ctx["current_paths"],
            source_complete=ctx["source_complete"],
            source_saturated=ctx["saturated"],
            source_limit=args.limit,
            collected_at=collected_at,
            candidates_evidence=ctx["candidates_evidence"],
            ignored_candidates=ctx["ignored_candidates"],
            dependency_resolution=ctx["dependency_resolution"],
            validation_errors=ctx["validation_errors"],
            route=route,
        )
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        return EXIT_RUNTIME_ERROR if route == ROUTE_RUNTIME_ERROR else EXIT_OK
    except OverlapRuntimeError as exc:
        error_body = {
            "schema": SCHEMA,
            "route": ROUTE_RUNTIME_ERROR,
            "error": str(exc),
        }
        print(json.dumps(error_body, ensure_ascii=False, indent=2))
        return EXIT_RUNTIME_ERROR
    except (ValueError, TypeError, KeyError, AssertionError) as exc:
        # 内部契約違反（schema / 型不整合）も fail-closed で runtime_error に倒す
        error_body = {
            "schema": SCHEMA,
            "route": ROUTE_RUNTIME_ERROR,
            "error": f"unexpected internal error: {exc!r}",
        }
        print(json.dumps(error_body, ensure_ascii=False, indent=2))
        return EXIT_RUNTIME_ERROR


def main() -> None:  # pragma: no cover - thin entrypoint
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
