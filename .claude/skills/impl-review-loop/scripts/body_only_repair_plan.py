#!/usr/bin/env python3
"""Body-only repair lane の単一 deterministic authority（Issue #2971）。

impl-review-loop が current PR HEAD の verification / runtime evidence /
required CI を完了した後、pr-reviewer の blockers が「PR 本文に既に存在する
evidence の同期」だけに限られる場合に、iteration budget を消費せず body-only
repair lane へ進めるかを決める pure な判断関数群を提供する。

pure module の不変条件: I/O・GitHub 呼出し・時刻依存・乱数・環境変数参照を
持たない（CLI の ``--dry-run-fixture`` だけが fixture file を読み stdout へ
marker 行を書く。dry-run は ``gh`` / ``update_pr.py`` / ``step4-adjudicate`` /
``step5-terminal-gate`` を一切実行しない）。

公開関数:

- ``decide_body_only_repair``: eligible / ineligible と ``reason_codes[]``、
  eligible の場合のみ ``body_plan`` を返す。
- ``check_body_freshness``: mutation 直前の live ``{head, body}`` が plan に束縛した
  値と一致する場合のみ ``proceed``。
- ``verify_body_readback``: 書込み後の HEAD 不変・body 一致を確認する。
- ``canonicalize_body`` / ``body_sha256``: 上記 3 関数が共有する単一の
  canonicalization（CRLF -> LF、末尾改行除去、UTF-8、SHA-256）。

closed な body-only blocker kind は ``BLOCKER_KINDS`` の 3 つだけで、blocker 1 件
ごとに「文面 pattern」「live PR body に対する検証」「current-head の evidence
ref」の 3 条件を全て満たした場合だけ分類する。deny list（code / test /
Issue contract / branch 変更を示す token）は kind 検出より常に優先する。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from typing import Any

# --- closed sets / constants -------------------------------------------------

VERDICT_REQUEST_CHANGES = "REQUEST_CHANGES"

KIND_RUNTIME_EVIDENCE_MISSING = "runtime_evidence_section_missing"
KIND_STALE_COUNT = "stale_count"
KIND_PENDING_WORDING = "pending_wording"
BLOCKER_KINDS = (
    KIND_RUNTIME_EVIDENCE_MISSING,
    KIND_STALE_COUNT,
    KIND_PENDING_WORDING,
)

EVIDENCE_KIND_RUNTIME = "runtime_evidence"
EVIDENCE_KIND_TEST_COUNT = "test_count"
EVIDENCE_KIND_COMPLETED_STATUS = "completed_status"
EVIDENCE_KINDS = (
    EVIDENCE_KIND_RUNTIME,
    EVIDENCE_KIND_TEST_COUNT,
    EVIDENCE_KIND_COMPLETED_STATUS,
)
EVIDENCE_KIND_FOR_BLOCKER = {
    KIND_RUNTIME_EVIDENCE_MISSING: EVIDENCE_KIND_RUNTIME,
    KIND_STALE_COUNT: EVIDENCE_KIND_TEST_COUNT,
    KIND_PENDING_WORDING: EVIDENCE_KIND_COMPLETED_STATUS,
}
EVIDENCE_REF_KEYS = ("kind", "value", "source", "head_sha")

RUNTIME_EVIDENCE_HEADING = "## Runtime Verification Evidence"
RUNTIME_EVIDENCE_NAME = "Runtime Verification Evidence"

# blocker 文面の検出 pattern 定数（test で固定する）。
MISSING_WORDS = ("無い", "欠落", "missing", "未記載")
PENDING_WORDS = ("未実施", "実施する予定", "pending")
COUNT_PATTERN = re.compile(r"(?<!\d)(\d+)(\s*)件")

# deny list: kind 検出より常に優先する。`コード` / `テスト` 単独の語は状況説明
# （「code/tests は PASS」等）にも現れるため含めない。
DENY_PATH_PATTERN = re.compile(r"\S+\.(?:py|ts|js|sh)(?![A-Za-z0-9_])")
DENY_WORDS = (
    "Allowed Paths",
    "Issue 本文",
    "Issue contract",
    "branch",
    "rebase",
    "conflict",
    "テスト追加",
    "実装修正",
)
DENY_WORD_PATTERNS = tuple(re.compile(re.escape(word), re.IGNORECASE) for word in DENY_WORDS)

# freshness / readback の結果定数。
FRESHNESS_PROCEED = "proceed"
FRESHNESS_HEAD_CHANGED = "ineligible_head_changed"
FRESHNESS_STALE_BODY = "stale_body_rebuild_required"
FRESHNESS_MALFORMED_PLAN = "malformed_body_plan"
READBACK_OK = "ok"
READBACK_HEAD_CHANGED = "head_changed_after_write"
READBACK_BODY_MISMATCH = "readback_body_mismatch"
READBACK_MALFORMED_PLAN = "malformed_body_plan"

# lane 順序定数（dry-run CLI が eligible のときだけ出力する）。
MARKER_ELIGIBLE = "BODY_ONLY_LANE_ELIGIBLE"
MARKER_INELIGIBLE_PREFIX = "BODY_ONLY_LANE_INELIGIBLE:"
LANE_ORDER_MARKERS = (
    "BODY_ONLY_LANE_STEP1_NOT_DISPATCHED",
    "BODY_ONLY_LANE_REPAIR_VIA_UPDATE_PR",
    "BODY_ONLY_LANE_STEP4_REUSE_STORED",
    "BODY_ONLY_LANE_STEP5_TERMINAL_GATE",
)


# --- canonicalization (shared by all three public functions) -----------------


def canonicalize_body(text: str) -> str:
    """CRLF -> LF、末尾改行除去。review hash / freshness / readback で共有する。"""
    return text.replace("\r\n", "\n").rstrip("\n")


def body_sha256(text: str) -> str:
    """canonicalize 後の UTF-8 bytes の SHA-256 (hex)。"""
    return hashlib.sha256(canonicalize_body(text).encode("utf-8")).hexdigest()


# --- helpers -----------------------------------------------------------------


def _add(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _deny_hit(blocker: str) -> bool:
    if DENY_PATH_PATTERN.search(blocker):
        return True
    return any(pattern.search(blocker) for pattern in DENY_WORD_PATTERNS)


def _pending_words_in(text: str) -> list[str]:
    return [word for word in PENDING_WORDS if word in text]


def _valid_refs(evidence_refs: Any, live_head_sha: str, reviewed_head_sha: str) -> tuple[list[dict], list[dict]]:
    """(valid refs, shape-valid but head-mismatched refs) を返す。"""
    valid: list[dict] = []
    stale: list[dict] = []
    if not isinstance(evidence_refs, list):
        return valid, stale
    for ref in evidence_refs:
        if not isinstance(ref, dict):
            continue
        if any(key not in ref for key in EVIDENCE_REF_KEYS):
            continue
        if ref["kind"] not in EVIDENCE_KINDS:
            continue
        value = ref["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        if not str(value).strip():
            continue
        if not _is_nonempty_str(ref["source"]):
            continue
        if not _is_nonempty_str(ref["head_sha"]):
            continue
        if ref["head_sha"] == live_head_sha and ref["head_sha"] == reviewed_head_sha:
            valid.append(ref)
        else:
            stale.append(ref)
    return valid, stale


def _select_ref(
    blocker_kind: str,
    valid: list[dict],
    stale: list[dict],
    reasons: list[str],
    *,
    required_value: str | None = None,
) -> dict | None:
    evidence_kind = EVIDENCE_KIND_FOR_BLOCKER[blocker_kind]
    candidates = [ref for ref in valid if ref["kind"] == evidence_kind]
    if not candidates:
        if any(ref["kind"] == evidence_kind for ref in stale):
            _add(reasons, "evidence_ref_stale_head")
        else:
            _add(reasons, "evidence_ref_missing")
        return None
    if required_value is not None:
        matching = [ref for ref in candidates if str(ref["value"]).strip() == required_value]
        if not matching:
            _add(reasons, "evidence_ref_value_mismatch")
            return None
        candidates = matching
    distinct_values = {str(ref["value"]) for ref in candidates}
    if len(distinct_values) > 1:
        _add(reasons, "evidence_ref_ambiguous")
        return None
    return candidates[0]


def _classify_blocker(blocker: Any) -> tuple[str | None, dict[str, Any]]:
    """文面 pattern だけで kind を判定する。(kind | None, 抽出情報)。

    kind が None の場合は info["reason"] に reason code を入れる。deny list は
    kind 検出より優先する。
    """
    if not isinstance(blocker, str) or not blocker.strip():
        return None, {"reason": "blocker_malformed"}
    if _deny_hit(blocker):
        return None, {"reason": "blocker_deny_list_hit"}

    matched: list[str] = []
    info: dict[str, Any] = {}

    if RUNTIME_EVIDENCE_NAME in blocker:
        # 名称への言及だけで runtime kind の候補にする（欠落語の活用形違いで他 kind との曖昧さを見逃さない）。
        matched.append(KIND_RUNTIME_EVIDENCE_MISSING)
        info["has_missing_word"] = any(word in blocker.lower() for word in MISSING_WORDS)

    counts = COUNT_PATTERN.findall(blocker)
    if len(counts) == 2 and counts[0][0] != counts[1][0]:
        matched.append(KIND_STALE_COUNT)
        info["old_count"] = counts[0][0]
        info["new_count"] = counts[1][0]
        info["old_sep"] = counts[0][1]

    pending = _pending_words_in(blocker)
    if pending:
        matched.append(KIND_PENDING_WORDING)
        info["pending_words"] = pending

    if not matched:
        return None, {"reason": "blocker_unclassifiable"}
    if len(matched) > 1:
        return None, {"reason": "blocker_ambiguous_kind"}
    if matched[0] == KIND_RUNTIME_EVIDENCE_MISSING and not info["has_missing_word"]:
        return None, {"reason": "blocker_unclassifiable"}
    info["kind"] = matched[0]
    return matched[0], info


def _literal_pattern(literal: str, is_count: bool) -> re.Pattern[str]:
    """count literal は先行数字を持たない（`139 件` を `39 件` と誤認しない）。"""
    escaped = re.escape(literal)
    return re.compile((r"(?<!\d)" + escaped) if is_count else escaped)


def _count_literal(body: str, literal: str, is_count: bool) -> int:
    return len(_literal_pattern(literal, is_count).findall(body))


def _replace_once(body: str, old: str, new: str, is_count: bool) -> str:
    return _literal_pattern(old, is_count).sub(lambda _match: new, body, count=1)


# --- decide_body_only_repair --------------------------------------------------


def decide_body_only_repair(
    reviewer_result: Any,
    live_head_sha: Any,
    vc_current_head_valid: Any,
    required_ci_valid: Any,
    live_pr_body: Any,
    evidence_refs: Any,
    prior_body_only_repairs: Any,
) -> dict[str, Any]:
    """body-only repair lane の eligibility と body plan を決定論的に返す。

    返り値: ``{"eligible": bool, "reason_codes": [...], "body_plan": dict | None}``。
    eligible の場合のみ ``body_plan`` を持ち、``reason_codes`` は空である。
    fail-closed: 判定できない入力は全て ineligible。``iteration >= max_iterations``
    は本関数の入力ではなく、eligibility を妨げない。
    """
    reasons: list[str] = []

    # --- gate inputs ---
    if not isinstance(reviewer_result, dict):
        _add(reasons, "reviewer_result_malformed")
        reviewer_result = {}
    if reviewer_result.get("verdict") != VERDICT_REQUEST_CHANGES:
        _add(reasons, "verdict_not_request_changes")

    live_head_ok = _is_nonempty_str(live_head_sha)
    if not live_head_ok:
        _add(reasons, "live_head_sha_missing")
    reviewed_head_sha = reviewer_result.get("reviewed_head_sha")
    if not _is_nonempty_str(reviewed_head_sha):
        _add(reasons, "reviewed_head_sha_missing")
    elif live_head_ok and reviewed_head_sha != live_head_sha:
        _add(reasons, "reviewed_head_sha_mismatch")

    if vc_current_head_valid is not True:
        _add(reasons, "vc_current_head_invalid")
    if required_ci_valid is not True:
        _add(reasons, "required_ci_invalid")

    prior_ok = isinstance(prior_body_only_repairs, int) and not isinstance(prior_body_only_repairs, bool)
    if not prior_ok or prior_body_only_repairs < 0:
        _add(reasons, "prior_body_only_repairs_invalid")
    elif prior_body_only_repairs >= 1:
        _add(reasons, "body_only_repair_already_used")

    body_ok = isinstance(live_pr_body, str)
    if not body_ok:
        _add(reasons, "live_pr_body_missing")

    blockers = reviewer_result.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        _add(reasons, "blockers_empty_or_malformed")
        blockers = []

    # --- blocker classification (always evaluated, diagnostics only when gated) ---
    safe_live_head = live_head_sha if live_head_ok else ""
    safe_reviewed = reviewed_head_sha if _is_nonempty_str(reviewed_head_sha) else ""
    valid_refs, stale_refs = _valid_refs(evidence_refs, safe_live_head, safe_reviewed)
    live_body = live_pr_body.replace("\r\n", "\n") if body_ok else ""

    edits: list[tuple[str, str, bool]] = []  # (old, new, is_count) replacements
    append_sections: list[str] = []
    used_refs: list[dict] = []
    kinds: list[str] = []
    runtime_blocker_seen = False

    for blocker in blockers:
        kind, info = _classify_blocker(blocker)
        if kind is None:
            _add(reasons, info["reason"])
            continue

        if kind == KIND_RUNTIME_EVIDENCE_MISSING:
            if runtime_blocker_seen:
                _add(reasons, "duplicate_runtime_evidence_blocker")
                continue
            runtime_blocker_seen = True
            if body_ok and re.search(r"(?m)^" + re.escape(RUNTIME_EVIDENCE_HEADING) + r"\s*$", live_body):
                _add(reasons, "live_body_runtime_evidence_section_present")
                continue
            ref = _select_ref(kind, valid_refs, stale_refs, reasons)
            if ref is None:
                continue
            value = str(ref["value"])
            if _pending_words_in(value):
                _add(reasons, "evidence_ref_value_invalid")
                continue
            append_sections.append(value)
            used_refs.append(ref)
            kinds.append(kind)

        elif kind == KIND_STALE_COUNT:
            old, new, sep = info["old_count"], info["new_count"], info["old_sep"]
            old_literal = f"{old}{sep}件"
            new_literal = f"{new}{sep}件"
            if body_ok and _count_literal(live_body, old_literal, True) != 1:
                _add(reasons, "replacement_target_count_invalid")
                continue
            ref = _select_ref(kind, valid_refs, stale_refs, reasons, required_value=new)
            if ref is None:
                continue
            edits.append((old_literal, new_literal, True))
            used_refs.append(ref)
            kinds.append(kind)

        else:  # KIND_PENDING_WORDING
            words = info["pending_words"]
            if len(words) != 1:
                _add(reasons, "blocker_ambiguous_kind")
                continue
            literal = words[0]
            if body_ok and _count_literal(live_body, literal, False) != 1:
                _add(reasons, "replacement_target_count_invalid")
                continue
            ref = _select_ref(kind, valid_refs, stale_refs, reasons)
            if ref is None:
                continue
            replacement = str(ref["value"])
            if _pending_words_in(replacement):
                _add(reasons, "evidence_ref_value_invalid")
                continue
            edits.append((literal, replacement, False))
            used_refs.append(ref)
            kinds.append(kind)

    if reasons:
        return {"eligible": False, "reason_codes": reasons, "body_plan": None}

    # --- deterministic mechanical completed body ---
    completed = live_body
    for old, new, is_count in edits:
        if _count_literal(completed, old, is_count) != 1:
            _add(reasons, "replacement_target_count_invalid")
            return {"eligible": False, "reason_codes": reasons, "body_plan": None}
        completed = _replace_once(completed, old, new, is_count)
    for section in append_sections:
        completed = canonicalize_body(completed) + "\n\n" + RUNTIME_EVIDENCE_HEADING + "\n" + section.strip("\n")
    completed = canonicalize_body(completed) + "\n"

    body_plan = {
        "review_body_sha256": body_sha256(live_pr_body),
        "reviewed_head_sha": reviewed_head_sha,
        "completed_body_text": completed,
        "evidence_refs": used_refs,
        "blocker_kinds": kinds,
    }
    return {"eligible": True, "reason_codes": [], "body_plan": body_plan}


# --- check_body_freshness / verify_body_readback ------------------------------


def _plan_fields(body_plan: Any) -> tuple[str, str, str] | None:
    if not isinstance(body_plan, dict):
        return None
    head = body_plan.get("reviewed_head_sha")
    review_sha = body_plan.get("review_body_sha256")
    completed = body_plan.get("completed_body_text")
    if not (_is_nonempty_str(head) and _is_nonempty_str(review_sha) and isinstance(completed, str)):
        return None
    return head, review_sha, completed


def check_body_freshness(body_plan: Any, live_head_sha: Any, live_pr_body: Any) -> str:
    """mutation 直前の live ``{head, body}`` が plan に束縛した値と一致するか。

    ``proceed`` / ``ineligible_head_changed`` / ``stale_body_rebuild_required`` /
    ``malformed_body_plan``。stale full-body overwrite を避けるため、body hash
    不一致は ``stale_body_rebuild_required``（current body から plan を再構築する）。
    """
    fields = _plan_fields(body_plan)
    if fields is None:
        return FRESHNESS_MALFORMED_PLAN
    reviewed_head, review_sha, _completed = fields
    if live_head_sha != reviewed_head:
        return FRESHNESS_HEAD_CHANGED
    if not isinstance(live_pr_body, str) or body_sha256(live_pr_body) != review_sha:
        return FRESHNESS_STALE_BODY
    return FRESHNESS_PROCEED


def verify_body_readback(body_plan: Any, post_head_sha: Any, post_pr_body: Any) -> str:
    """書込み後の HEAD 不変かつ readback body が completed body と一致するか。"""
    fields = _plan_fields(body_plan)
    if fields is None:
        return READBACK_MALFORMED_PLAN
    reviewed_head, _review_sha, completed = fields
    if post_head_sha != reviewed_head:
        return READBACK_HEAD_CHANGED
    if not isinstance(post_pr_body, str) or canonicalize_body(post_pr_body) != canonicalize_body(completed):
        return READBACK_BODY_MISMATCH
    return READBACK_OK


# --- dry-run CLI --------------------------------------------------------------


def dry_run_marker_lines(decision: dict[str, Any]) -> list[str]:
    """``decide_body_only_repair`` の実出力から marker 行を導出する。"""
    if decision.get("eligible") is True:
        return [MARKER_ELIGIBLE, *LANE_ORDER_MARKERS]
    reasons = decision.get("reason_codes") or ["unknown"]
    return [f"{MARKER_INELIGIBLE_PREFIX}{reason}" for reason in reasons]


def decide_from_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return decide_body_only_repair(
        fixture.get("reviewer_result"),
        fixture.get("live_head_sha"),
        fixture.get("vc_current_head_valid"),
        fixture.get("required_ci_valid"),
        fixture.get("live_pr_body"),
        fixture.get("evidence_refs"),
        fixture.get("prior_body_only_repairs"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="body-only repair lane dry-run（判断関数の出力から marker を導出）")
    parser.add_argument("--dry-run-fixture", required=True, help="fixture JSON のパス")
    args = parser.parse_args(argv)
    try:
        with open(args.dry_run_fixture, encoding="utf-8") as handle:
            fixture = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"fixture を読めません: {exc}", file=sys.stderr)
        return 2
    if not isinstance(fixture, dict):
        print("fixture は JSON object である必要があります", file=sys.stderr)
        return 2
    for line in dry_run_marker_lines(decide_from_fixture(fixture)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
