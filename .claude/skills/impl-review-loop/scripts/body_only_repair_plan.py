"""Body-only repair lane の単一 deterministic authority（Issue #2971）。

impl-review-loop が current PR HEAD の verification / runtime evidence /
required CI を完了した後、pr-reviewer の blockers が「PR 本文に既に存在する
evidence の同期」だけに限られる場合に、iteration budget を消費せず body-only
repair lane へ進めるかを決める pure な判断関数群と、それを実 artifact・gh に
接続する production CLI を提供する。

構成:

- 上半分（``# --- CLI layer`` より前）は pure 層。I/O・GitHub 呼出し・時刻依存・
  乱数・環境変数参照を持たない（test が AST で固定する）。
- 下半分（``# --- CLI layer`` 以降）だけが file / subprocess / gh の I/O を持つ。
  ``--dry-run-fixture`` は fixture file を読んで marker 行を stdout へ書くだけで、
  ``gh`` / ``update_pr.py`` / ``step4-adjudicate`` / ``step5-terminal-gate`` を一切
  実行しない（production subcommand とは分離している）。

公開 pure 関数:

- ``decide_body_only_repair``: eligible / ineligible と ``reason_codes[]``、
  eligible の場合のみ ``body_plan`` を返す。
- ``check_body_freshness``: mutation 直前の live ``{head, body}`` が plan に束縛した
  値と一致する場合のみ ``proceed``。
- ``verify_body_readback``: 書込み後の HEAD 不変・body 一致を確認する。
- ``canonicalize_body`` / ``body_sha256``: 上記 3 関数が共有する単一の
  canonicalization（CRLF -> LF、末尾改行除去、UTF-8、SHA-256）。

closed な body-only blocker kind は ``BLOCKER_KINDS`` の 3 つだけで、blocker 1 件
ごとに「blocker 全文を消費する anchored grammar（``re.fullmatch``）」「live PR body
に対する検証」「current-head の evidence ref」の 3 条件を全て満たした場合だけ分類
する。deny list の単語を増やして未知句を防ぐ方式は採らない（grammar が全文を消費
できない blocker は ``blocker_unclassifiable`` で fail-closed）。

production subcommand（JSON-in / JSON-out。exit 0 = 成功・eligible、1 = 否定結果、
2 = runtime error）:

- ``plan``: evidence refs を current-head 束縛で materialize し、eligible の場合だけ
  completed body を ``--body-out`` へ書き出す。
- ``record``: ``--loop-state-file`` の ``blockers_history[]`` へ lane 消費を原子的に
  記録する唯一の writer。
- ``guard``: mutation 直前に live ``{head, body}`` を自力で取得して照合する。
- ``ci-freshness``: body 編集後の required CI を workflow の ``edited`` trigger 有無
  で fresh / head_bound / stale_pre_edit / unknown_workflow に分類する。
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
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

# blocker 文面の whole-blocker closed grammar（``re.fullmatch``。部分一致は使わない）。
# 定義中の token 境界は「0 個以上の ASCII / 全角スペース」の明示 class（incident blocker 原文の
# ようにスペースの無い連結も許す）。ASCII 語どうしの境界だけは 1 個以上を要求する。
PENDING_WORDS = ("未実施", "実施する予定", "pending")
_SP = r"[ \u3000]*"
_SP1 = r"[ \u3000]+"
_PREFIX = r"(?:PR" + _SP + r"本文|PR" + _SP1 + r"body)"
_ID = r"(?:AC[0-9]+|(?:tested_)?head" + _SP1 + r"[0-9a-f]{7,40})"
_ID_PAREN = r"（" + _ID + r"(?:, " + _ID + r")*）"
_SUBJECT = r"[A-Za-z0-9_.\-][A-Za-z0-9_.\- ]{0,39}"
_PENDING_ALT = "|".join(re.escape(word) for word in PENDING_WORDS)
GRAMMAR_SOURCES = {
    "runtime_evidence_section_missing": (
        _PREFIX
        + _SP
        + r"(?:には|に)"
        + _SP
        + r"Runtime Verification Evidence"
        + r"(?:"
        + _SP1
        + r"section|"
        + _SP
        + r"セクション)?"
        + _SP
        + r"(?:"
        + _ID_PAREN
        + r")?"
        + _SP
        + r"(?:が|は)?"
        + _SP
        + r"(?:無い|ない|欠落している|欠落|missing|未記載)"
    ),
    "stale_count": (
        _PREFIX
        + _SP
        + r"の"
        + _SP
        + r"(?P<subject>"
        + _SUBJECT
        + r")"
        + _SP
        + r"件数が"
        + _SP
        + r"(?P<old>[0-9]+)(?P<sep>"
        + _SP
        + r")件のまま（(?:current"
        + _SP1
        + r")?head"
        + _SP
        + r"では"
        + _SP
        + r"(?P<new>[0-9]+)"
        + _SP
        + r"件）"
    ),
    "pending_wording": (
        _PREFIX
        + _SP
        + r"(?:に|の)"
        + _SP
        + r"「?(?P<word>"
        + _PENDING_ALT
        + r")」?"
        + _SP
        + r"(?:のまま|が残っている|が残存している)"
    ),
}
BLOCKER_GRAMMARS = {kind: re.compile(source) for kind, source in GRAMMAR_SOURCES.items()}
TOKEN_SPLIT_PATTERN = re.compile(r"[^A-Za-z0-9]+|_")

# deny list: eligibility の根拠ではなく、grammar に一致しなかった blocker の診断（code / test /
# Issue contract / branch 変更を示す語を含むか）にだけ使う。`コード` / `テスト` 単独の語は状況説明
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


def subject_tokens(text: str) -> frozenset[str]:
    """``[^A-Za-z0-9]+`` と ``_`` で分割した小文字 token の集合（空 token は捨てる）。

    ``\\b`` の単語境界は ``_`` を単語文字として扱い snake_case の path に効かないため
    使わない。``evaluator`` は ``..._reachability_evaluator.py`` の token 集合に含まれ、
    ``evaluator_helper`` の token 集合（``evaluator`` / ``helper``）とは区別される。
    """
    return frozenset(token for token in TOKEN_SPLIT_PATTERN.split(text.lower()) if token)


def _normalize_blocker(blocker: str) -> str:
    """前後空白除去、行末の ``。`` / ``.`` 除去（1 個）。"""
    text = blocker.strip()
    if text.endswith(("。", ".")):
        text = text[:-1].rstrip()
    return text


def _consume(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    """blocker 全文を grammar で完全に消費できた場合だけ match を返す（``fullmatch``）。

    mutation test がこの境界を ``search`` へ差し替えて、negative corpus が grammar の
    全文消費に依存していること（partial match へ緩めると fail すること）を確認する。
    """
    return pattern.fullmatch(text)


def _classify_blocker(blocker: Any) -> tuple[str | None, dict[str, Any]]:
    """blocker 全文の closed grammar 完全一致だけで kind を判定する。

    返り値は ``(kind | None, info)``。kind が None の場合は ``info["reason"]`` に主たる
    reason code、``info["reasons"]`` に全 reason code を入れる。deny list は eligibility の
    根拠ではなく、grammar に一致しなかった blocker の診断（``blocker_deny_list_hit``）だけに使う。
    """
    if not isinstance(blocker, str) or not blocker.strip():
        return None, {"reason": "blocker_malformed", "reasons": ["blocker_malformed"]}
    text = _normalize_blocker(blocker)

    matched: list[tuple[str, re.Match[str]]] = []
    for kind in BLOCKER_KINDS:
        match = _consume(BLOCKER_GRAMMARS[kind], text)
        if match is not None:
            matched.append((kind, match))

    if not matched:
        reasons = ["blocker_unclassifiable"]
        if _deny_hit(blocker):
            reasons.append("blocker_deny_list_hit")
        return None, {"reason": reasons[0], "reasons": reasons}
    if len(matched) > 1:
        return None, {"reason": "blocker_ambiguous_kind", "reasons": ["blocker_ambiguous_kind"]}

    kind, match = matched[0]
    info: dict[str, Any] = {"kind": kind}
    if kind == KIND_STALE_COUNT:
        subject = match.group("subject").strip()
        old, new = match.group("old"), match.group("new")
        if old == new or not subject_tokens(subject):
            return None, {"reason": "blocker_unclassifiable", "reasons": ["blocker_unclassifiable"]}
        info.update(
            {
                "subject": subject,
                "old_count": old,
                "new_count": new,
                "old_sep": match.group("sep"),
            }
        )
    elif kind == KIND_PENDING_WORDING:
        info["pending_words"] = [match.group("word")]
    return kind, info


def _literal_pattern(literal: str, is_count: bool) -> re.Pattern[str]:
    """count literal は先行数字を持たない（`139 件` を `39 件` と誤認しない）。"""
    escaped = re.escape(literal)
    return re.compile((r"(?<!\d)" + escaped) if is_count else escaped)


def _count_literal(body: str, literal: str, is_count: bool, scope_tokens: frozenset[str] | None = None) -> int:
    """body 中の literal の出現数。``scope_tokens`` を渡すと、その token を全て含む行に限る。"""
    pattern = _literal_pattern(literal, is_count)
    if scope_tokens is None:
        return len(pattern.findall(body))
    return sum(len(pattern.findall(line)) for line in body.split("\n") if scope_tokens <= subject_tokens(line))


def _replace_once(body: str, old: str, new: str, is_count: bool, scope_tokens: frozenset[str] | None = None) -> str:
    pattern = _literal_pattern(old, is_count)
    if scope_tokens is None:
        return pattern.sub(lambda _match: new, body, count=1)
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if scope_tokens <= subject_tokens(line) and pattern.search(line):
            lines[index] = pattern.sub(lambda _match: new, line, count=1)
            break
    return "\n".join(lines)


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

    edits: list[tuple[str, str, bool, frozenset[str] | None]] = []  # (old, new, is_count, scope)
    append_sections: list[str] = []
    used_refs: list[dict] = []
    kinds: list[str] = []
    runtime_blocker_seen = False

    for blocker in blockers:
        kind, info = _classify_blocker(blocker)
        if kind is None:
            for code in info["reasons"]:
                _add(reasons, code)
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
            scope = subject_tokens(info["subject"])
            if body_ok and _count_literal(live_body, old_literal, True, scope) != 1:
                _add(reasons, "replacement_target_count_invalid")
                continue
            ref = _select_ref(kind, valid_refs, stale_refs, reasons, required_value=new)
            if ref is None:
                continue
            edits.append((old_literal, new_literal, True, scope))
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
            edits.append((literal, replacement, False, None))
            used_refs.append(ref)
            kinds.append(kind)

    if reasons:
        return {"eligible": False, "reason_codes": reasons, "body_plan": None}

    # --- deterministic mechanical completed body ---
    completed = live_body
    for old, new, is_count, scope in edits:
        if _count_literal(completed, old, is_count, scope) != 1:
            _add(reasons, "replacement_target_count_invalid")
            return {"eligible": False, "reason_codes": reasons, "body_plan": None}
        completed = _replace_once(completed, old, new, is_count, scope)
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


# --- pure helpers for the production CLI (no I/O) -----------------------------

LANE_NAME = "body_only_repair"
OUTCOME_DISPATCHED = "dispatched"
OUTCOME_NO_MUTATION = "no_mutation"
RECORD_STATES = (OUTCOME_DISPATCHED, OUTCOME_NO_MUTATION)
# no_mutation がちょうど 1 件の間だけ 1 回の再評価が許され、2 件になった時点で lane は終了する。
MAX_NO_MUTATION_ENTRIES = 1

CI_WAIT_PREFIX = "CI_WAIT_RESULT_V1_JSON="
CI_WAIT_SCHEMA = "CI_WAIT_RESULT_V1"
TEST_VERDICT_SCHEMA = "TEST_VERDICT_MACHINE/v2"
COMPLETED_STATUS_TEMPLATE = "実施済み（TEST_VERDICT_MACHINE/v2 result: PASS, head {head7}）"

CI_FRESH = "fresh"
CI_HEAD_BOUND = "head_bound"
CI_STALE_PRE_EDIT = "stale_pre_edit"
CI_UNKNOWN_WORKFLOW = "unknown_workflow"
CI_NOT_PASSED = "not_passed"
CI_NOT_COMPLETED = "not_completed"
CI_INVALID_TIMESTAMP = "invalid_timestamp"
CI_ACCEPTED = (CI_FRESH, CI_HEAD_BOUND)
DEFAULT_PULL_REQUEST_TYPES = frozenset({"opened", "synchronize", "reopened"})
EDITED_TYPE = "edited"


def lane_history_counts(loop_state: Any) -> tuple[dict[str, int] | None, str | None]:
    """``blockers_history[]`` の ``lane: body_only_repair`` entry 件数を数える。

    返り値は ``({"consumed": int, "no_mutation": int, "dispatched": int}, None)``、
    ``blockers_history`` が list でなければ ``(None, reason)``。``consumed`` は
    ``outcome != no_mutation`` の件数（crash / resume で ``dispatched`` のまま残った entry を含む）。
    """
    if not isinstance(loop_state, dict):
        return None, "loop_state_not_object"
    history = loop_state.get("blockers_history", [])
    if history is None:
        history = []
    if not isinstance(history, list):
        return None, "blockers_history_not_list"
    consumed = no_mutation = dispatched = 0
    for entry in history:
        if not isinstance(entry, dict) or entry.get("lane") != LANE_NAME:
            continue
        if entry.get("outcome") == OUTCOME_NO_MUTATION:
            no_mutation += 1
        else:
            consumed += 1
            if entry.get("outcome") == OUTCOME_DISPATCHED:
                dispatched += 1
    return {"consumed": consumed, "no_mutation": no_mutation, "dispatched": dispatched}, None


def apply_record(loop_state: Any, state: str) -> tuple[dict[str, Any] | None, str | None]:
    """``record`` の状態遷移。``(new_loop_state, None)`` または ``(None, reason)``。

    ``dispatched``: ``{lane, outcome: dispatched}`` を 1 件追記する（lane 消費済みまたは
    no_mutation が 2 件に達した後は拒否）。``no_mutation``: 直近の ``dispatched`` entry を
    ``no_mutation`` へ更新する。entry の field は closed set ``{lane, outcome}``。既存 key は保存する。
    """
    if state not in RECORD_STATES:
        return None, "invalid_state"
    counts, error = lane_history_counts(loop_state)
    if counts is None:
        return None, error
    new_state = copy.deepcopy(loop_state)
    history = new_state.get("blockers_history")
    if not isinstance(history, list):
        history = []
        new_state["blockers_history"] = history
    if state == OUTCOME_DISPATCHED:
        if counts["consumed"] >= 1:
            return None, "lane_already_consumed"
        if counts["no_mutation"] > MAX_NO_MUTATION_ENTRIES:
            return None, "lane_ended"
        history.append({"lane": LANE_NAME, "outcome": OUTCOME_DISPATCHED})
        return new_state, None
    for index in range(len(history) - 1, -1, -1):
        entry = history[index]
        if isinstance(entry, dict) and entry.get("lane") == LANE_NAME and entry.get("outcome") == OUTCOME_DISPATCHED:
            history[index] = {"lane": LANE_NAME, "outcome": OUTCOME_NO_MUTATION}
            return new_state, None
    return None, "no_dispatched_entry"


def parse_ci_wait_output(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """``wait_ci_checks.py`` 出力から ``CI_WAIT_RESULT_V1_JSON=`` 行を 1 行だけ取り出して parse する。"""
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith(CI_WAIT_PREFIX)]
    if len(lines) != 1:
        return None, "ci_wait_result_line_count_invalid"
    try:
        payload = json.loads(lines[0][len(CI_WAIT_PREFIX) :])
    except ValueError:
        return None, "ci_wait_result_not_json"
    if not isinstance(payload, dict) or payload.get("schema") != CI_WAIT_SCHEMA:
        return None, "ci_wait_result_schema_invalid"
    return payload, None


def required_ci_valid_for_head(payload: Any, live_head_sha: Any) -> bool:
    """``status: passed`` かつ ``head_sha`` / ``current_head_sha`` が live head と一致する場合だけ True。"""
    if not isinstance(payload, dict) or not _is_nonempty_str(live_head_sha):
        return False
    checks = payload.get("checks")
    return (
        payload.get("status") == "passed"
        and payload.get("head_sha") == live_head_sha
        and payload.get("current_head_sha") == live_head_sha
        and isinstance(checks, list)
        and bool(checks)
    )


def unwrap_test_verdict(report: Any) -> dict[str, Any] | None:
    """``TEST_VERDICT_MACHINE/v2`` の本体 object を返す（``{"TEST_VERDICT": {...}}`` 包みも許す）。"""
    if not isinstance(report, dict):
        return None
    payload = report["TEST_VERDICT"] if isinstance(report.get("TEST_VERDICT"), dict) else report
    return payload if payload.get("schema") == TEST_VERDICT_SCHEMA else None


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def bind_test_count_row(
    subject: str, runtime_ac_results: Any, expected_command_hashes: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """blocker の SUBJECT に束縛できる ``test_count`` 行を exactly-1 行で選ぶ。

    対象行は ``status: pass`` かつ ``exit_code: 0``、``command_hash`` が adjudicate 済み binding の
    集合に含まれ、``test_count: {subject, passed}`` の ``passed`` が非負整数の行。SUBJECT の token
    全てが行の ``command`` の token 集合と ``test_count.subject`` の token 集合の **両方**に含まれる行が
    ちょうど 1 行の場合だけ返す。0 行は ``test_count_subject_unbound``、2 行以上は
    ``test_count_subject_ambiguous``。``notes`` は一切読まない。
    """
    tokens = subject_tokens(subject)
    if not tokens or not isinstance(runtime_ac_results, list) or not isinstance(expected_command_hashes, list):
        return None, "test_count_subject_unbound"
    expected = {hash_ for hash_ in expected_command_hashes if isinstance(hash_, str)}
    bound: list[dict[str, Any]] = []
    for row in runtime_ac_results:
        if not isinstance(row, dict):
            continue
        if row.get("status") != "pass" or not _plain_int(row.get("exit_code")) or row.get("exit_code") != 0:
            continue
        if row.get("command_hash") not in expected:
            continue
        count = row.get("test_count")
        if not isinstance(count, dict) or not isinstance(count.get("subject"), str):
            continue
        passed = count.get("passed")
        if not _plain_int(passed) or passed < 0:
            continue
        command = row.get("command")
        if not isinstance(command, str):
            continue
        if tokens <= subject_tokens(command) and tokens <= subject_tokens(count["subject"]):
            bound.append(row)
    if not bound:
        return None, "test_count_subject_unbound"
    if len(bound) > 1:
        return None, "test_count_subject_ambiguous"
    return bound[0], None


def stale_count_requirements(blockers: Any) -> list[tuple[str, str]]:
    """grammar に一致した stale_count blocker の ``(subject, new_count)`` を順に返す。"""
    found: list[tuple[str, str]] = []
    if not isinstance(blockers, list):
        return found
    for blocker in blockers:
        kind, info = _classify_blocker(blocker)
        if kind == KIND_STALE_COUNT:
            found.append((info["subject"], info["new_count"]))
    return found


def build_evidence_refs(
    *,
    live_head_sha: str,
    blockers: Any,
    test_verdict_report: Any,
    test_verdict_source: str,
    expected_command_hashes: Any,
    runtime_summary_text: str | None,
    runtime_summary_source: str | None,
    runtime_summary_provenance_ok: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """current-head 束縛の evidence refs と、ineligible を強制する追加 reason code を返す。

    ``TEST_VERDICT`` の head 不一致・``result != PASS`` は blocker の kind に関わらず ineligible。
    ``test_count`` ref は raw ``runtime_ac_results[]`` 行（adapter を通さない）から subject 束縛で
    導出し、行の ``passed`` が blocker の新値と一致しなければ ``evidence_ref_value_mismatch``。
    """
    refs: list[dict[str, Any]] = []
    extra: list[str] = []
    verdict = unwrap_test_verdict(test_verdict_report)
    if verdict is None:
        extra.append("test_verdict_malformed")
    else:
        head_ok = (
            verdict.get("head_sha") == live_head_sha
            and (verdict.get("reviewed_head_sha") or verdict.get("head_sha")) == live_head_sha
        )
        if not head_ok:
            extra.append("test_verdict_head_mismatch")
        elif verdict.get("result") != "PASS":
            extra.append("test_verdict_not_pass")
        else:
            refs.append(
                {
                    "kind": EVIDENCE_KIND_COMPLETED_STATUS,
                    "value": COMPLETED_STATUS_TEMPLATE.format(head7=live_head_sha[:7]),
                    "source": test_verdict_source,
                    "head_sha": live_head_sha,
                }
            )
            for subject, new_count in stale_count_requirements(blockers):
                row, error = bind_test_count_row(subject, verdict.get("runtime_ac_results"), expected_command_hashes)
                if row is None:
                    extra.append(error or "test_count_subject_unbound")
                    continue
                passed = row["test_count"]["passed"]
                if str(passed) != new_count:
                    extra.append("evidence_ref_value_mismatch")
                    continue
                refs.append(
                    {
                        "kind": EVIDENCE_KIND_TEST_COUNT,
                        "value": str(passed),
                        "source": test_verdict_source,
                        "head_sha": live_head_sha,
                    }
                )
    if runtime_summary_text is not None:
        if not runtime_summary_provenance_ok:
            extra.append("runtime_summary_provenance_unverified")
        elif runtime_summary_text.strip():
            refs.append(
                {
                    "kind": EVIDENCE_KIND_RUNTIME,
                    "value": runtime_summary_text.strip("\n"),
                    "source": runtime_summary_source or "",
                    "head_sha": live_head_sha,
                }
            )
    return refs, extra


def workflow_trigger_info(document: Any, fallback_name: str) -> tuple[str, frozenset[str] | None]:
    """workflow YAML（parse 済み）の ``(name, pull_request types | None)``。

    PyYAML は ``on`` キーを真偽値 ``True`` として読むため ``True`` / ``"on"`` の両方を扱う。
    ``pull_request`` trigger を持たない場合は types を None とする。``types`` 未指定は既定の
    opened / synchronize / reopened。
    """
    if not isinstance(document, dict):
        return fallback_name, None
    name = document.get("name")
    name = name if isinstance(name, str) and name.strip() else fallback_name
    trigger = document["on"] if "on" in document else document.get(True)
    if isinstance(trigger, str):
        return name, DEFAULT_PULL_REQUEST_TYPES if trigger == "pull_request" else None
    if isinstance(trigger, list):
        return name, DEFAULT_PULL_REQUEST_TYPES if "pull_request" in trigger else None
    if not isinstance(trigger, dict) or "pull_request" not in trigger:
        return name, None
    config = trigger["pull_request"]
    types = config.get("types") if isinstance(config, dict) else None
    if types is None:
        return name, DEFAULT_PULL_REQUEST_TYPES
    if isinstance(types, str):
        return name, frozenset({types})
    if isinstance(types, list):
        return name, frozenset(str(item) for item in types)
    return name, None


def parse_timestamp(value: Any) -> datetime.datetime | None:
    """ISO 8601（``Z`` 可）を UTC aware の datetime にする。解釈不能・ゼロ時刻は None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    if parsed.year <= 1:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def classify_ci_check(
    check: Any, watermark: datetime.datetime, workflows: dict[str, list[frozenset[str] | None]]
) -> str:
    """required check 1 件を fresh / head_bound / stale_pre_edit / unknown_workflow 等へ分類する。

    ``edited`` を含む workflow は ``startedAt >= watermark``（秒精度のため ``>=``）・completed・
    bucket ``pass`` の run だけを post-edit evidence とし、``startedAt < watermark`` の pass は
    ``stale_pre_edit``。``edited`` を含まない workflow（body に依存しない）は head 束縛のみで
    ``head_bound``。workflow を特定できない check は ``unknown_workflow``（fail-closed）。
    """
    if not isinstance(check, dict):
        return CI_UNKNOWN_WORKFLOW
    if check.get("bucket") != "pass":
        return CI_NOT_PASSED
    workflow = check.get("workflow")
    candidates = workflows.get(workflow) if isinstance(workflow, str) else None
    if not candidates or len(candidates) != 1 or candidates[0] is None:
        return CI_UNKNOWN_WORKFLOW
    completed = parse_timestamp(check.get("completedAt"))
    if completed is None:
        return CI_NOT_COMPLETED
    if EDITED_TYPE not in candidates[0]:
        return CI_HEAD_BOUND
    started = parse_timestamp(check.get("startedAt"))
    if started is None:
        return CI_INVALID_TIMESTAMP
    if started < watermark:
        return CI_STALE_PRE_EDIT
    return CI_FRESH


def evaluate_ci_freshness(
    wait_payload: Any,
    watermark: datetime.datetime,
    workflows: dict[str, list[frozenset[str] | None]],
    expected_head_sha: str | None = None,
) -> dict[str, Any]:
    """``wait_ci_checks.py`` の出力 payload を post-edit freshness で判定する。"""
    reasons: list[str] = []
    checks_out: list[dict[str, Any]] = []
    if not isinstance(wait_payload, dict):
        return {"fresh": False, "retryable": False, "reason_codes": ["ci_wait_result_malformed"], "checks": []}
    head = wait_payload.get("head_sha")
    if not _is_nonempty_str(head) or wait_payload.get("current_head_sha") != head:
        reasons.append("ci_head_mismatch")
    if expected_head_sha is not None and head != expected_head_sha:
        _add(reasons, "ci_head_mismatch")
    if wait_payload.get("status") != "passed":
        reasons.append("ci_not_passed")
    checks = wait_payload.get("checks")
    if not isinstance(checks, list) or not checks:
        reasons.append("no_required_checks")
        checks = []
    for check in checks:
        classification = classify_ci_check(check, watermark, workflows)
        name = check.get("name") if isinstance(check, dict) else None
        checks_out.append({"name": name, "classification": classification})
        if classification not in CI_ACCEPTED:
            _add(reasons, classification)
    fatal = [code for code in reasons if code not in (CI_STALE_PRE_EDIT, CI_NOT_COMPLETED, "ci_not_passed")]
    return {
        "fresh": not reasons,
        "retryable": bool(reasons) and not fatal,
        "reason_codes": reasons,
        "checks": checks_out,
    }


def guard_decision(
    live_head_sha: Any,
    live_pr_body: Any,
    expected_head_sha: str,
    expected_live_body_sha256: str,
    body_file_text: str,
    body_file_sha256: str,
) -> dict[str, Any]:
    """``guard`` の判定。``check_body_freshness`` を再利用し、body file の hash も照合する。"""
    plan_like = {
        "reviewed_head_sha": expected_head_sha,
        "review_body_sha256": expected_live_body_sha256,
        "completed_body_text": body_file_text,
    }
    freshness = check_body_freshness(plan_like, live_head_sha, live_pr_body)
    result: dict[str, Any] = {"result": freshness, "reason_code": None}
    if freshness == FRESHNESS_HEAD_CHANGED:
        result["reason_code"] = "expected_head_sha_mismatch"
    elif freshness == FRESHNESS_STALE_BODY:
        result["reason_code"] = "live_body_hash_mismatch"
    elif freshness == FRESHNESS_PROCEED and body_sha256(body_file_text) != body_file_sha256:
        result["result"] = "body_file_hash_mismatch"
        result["reason_code"] = "live_body_hash_mismatch"
    return result


# --- CLI layer (all file / subprocess / gh I/O lives below this line) ----------

SCRIPT_DIR = Path(__file__).resolve().parent
ADJUDICATE_SCRIPT = SCRIPT_DIR / "adjudicate_vc_result.py"
UPDATE_PR_SCRIPT = SCRIPT_DIR.parent.parent / "open-pr" / "scripts" / "update_pr.py"
SUBCOMMANDS = ("plan", "record", "guard", "ci-freshness")
EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_RUNTIME = 2
GH_TIMEOUT_SECONDS = 60


class CliError(Exception):
    """runtime error（exit 2）。メッセージは stdout の JSON ``error`` に入る。"""


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _read_text(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"{what} を読めません: {type(exc).__name__}: {path}") from exc


def _read_json(path: str, what: str) -> Any:
    try:
        return json.loads(_read_text(path, what))
    except ValueError as exc:
        raise CliError(f"{what} は JSON ではありません: {path}") from exc


def run_gh(argv: list[str]) -> tuple[int, str, str]:
    """``gh`` を実行する（既定の runner。test は fake runner または PATH 上の fake gh を使う）。"""
    try:
        result = subprocess.run(
            ["gh", *argv],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
            timeout=GH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return 127, "", "gh not found"
    except subprocess.TimeoutExpired:
        return 124, "", "gh timeout"
    return result.returncode, result.stdout, result.stderr


def fetch_live_pr(repo: str, pr_number: int, runner: Any = None) -> dict[str, Any]:
    """live ``{head, body, updated_at}`` を ``gh pr view`` で 1 回で取得する（runner は注入可能）。"""
    runner = runner or run_gh
    rc, stdout, stderr = runner(["pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid,body,updatedAt"])
    if rc != 0:
        raise CliError(f"gh pr view に失敗しました (rc={rc}): {stderr.strip()[:200]}")
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise CliError("gh pr view の出力が JSON ではありません") from exc
    head = data.get("headRefOid") if isinstance(data, dict) else None
    body = data.get("body") if isinstance(data, dict) else None
    if not _is_nonempty_str(head) or not isinstance(body, str):
        raise CliError("gh pr view の出力に headRefOid / body がありません")
    return {"head": head, "body": body, "updated_at": data.get("updatedAt")}


def write_text_atomically(path: str, text: str) -> None:
    """同一 directory の temp file + ``os.replace`` による原子的書込み（元 file の mode を保つ）。"""
    target = Path(path)
    directory = target.parent if str(target.parent) else Path(".")
    mode = None
    try:
        mode = target.stat().st_mode & 0o7777
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_update_pr() -> Any:
    """既存 ``update_pr.py`` を一意名で load する（変更しない。validator を同一入力で再利用する）。"""
    name = "update_pr_loaded_by_body_only_repair_plan"
    spec = importlib.util.spec_from_file_location(name, UPDATE_PR_SCRIPT)
    if spec is None or spec.loader is None:
        raise CliError(f"update_pr.py を load できません: {UPDATE_PR_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _Cwd:
    def __init__(self, path: str) -> None:
        self.path = path
        self.previous = ""

    def __enter__(self) -> "_Cwd":
        self.previous = os.getcwd()
        try:
            os.chdir(self.path)
        except OSError as exc:
            raise CliError(f"--worktree に移動できません: {self.path}") from exc
        return self

    def __exit__(self, *_exc: Any) -> None:
        os.chdir(self.previous)


def run_completed_body_validators(
    body_text: str, *, repo: str, issue_number: int, worktree: str, update_pr_module: Any = None
) -> list[str]:
    """completed body を実 ``update_pr.py`` と同一の入力・同一の validator 呼出しで read-only に検証する。

    ``--issue-number`` を ``--linked-issue``、linked Issue body を ``gh issue view``、changed paths を
    implementation worktree での ``git diff main...HEAD`` 解決（``update_pr.resolve_changed_paths``）で
    取得する。``gh pr edit`` は実行しない。失敗した validator 名の list（空なら通過）を返す。
    """
    upd = update_pr_module or _load_update_pr()
    failures: list[str] = []
    with _Cwd(worktree):
        changed_paths = upd.resolve_changed_paths(None)
        linked_issue_body = upd.get_linked_issue_body(repo, issue_number) if issue_number else None
        result = upd._call_pr_body_validator(
            upd._run_pr_body_validator, body_text, changed_paths, issue_number, linked_issue_body
        )
        if result.get("status") != "pass":
            failures.append("validate_pr_body")
        japanese = upd._run_japanese_content_validator(body_text)
        if japanese.get("status") != "pass":
            failures.append("validate_japanese_content")
    return failures


def _git_output(worktree: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(["git", "-C", worktree, *args], capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def runtime_summary_provenance_ok(worktree: str, live_head_sha: str, summary_path: str) -> bool:
    """best-effort の来歴確認: worktree HEAD が live head と一致し、artifact の mtime が当該 HEAD の
    commit 時刻以降であること。artifact の真正性は証明しない（residual risk）。"""
    if _git_output(worktree, ["rev-parse", "HEAD"]) != live_head_sha:
        return False
    commit_time = _git_output(worktree, ["log", "-1", "--format=%ct", "HEAD"])
    try:
        return os.stat(summary_path).st_mtime >= int(commit_time or "")
    except (OSError, ValueError):
        return False


def evaluate_vc_current_head(
    loop_state_file: str, live_head_sha: str, contract_body_sha256: str, command_hashes_file: str
) -> bool:
    """既存の read-only ``adjudicate_vc_result.py step4-gate`` で current-head VC を再検証する。"""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(ADJUDICATE_SCRIPT),
                "step4-gate",
                "--loop-state-file",
                loop_state_file,
                "--expected-head-sha",
                live_head_sha,
                "--expected-contract-body-sha256",
                contract_body_sha256,
                "--expected-command-hashes-file",
                command_hashes_file,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError(f"step4-gate を実行できません: {type(exc).__name__}") from exc
    if completed.returncode == EXIT_RUNTIME:
        raise CliError("step4-gate が malformed input を報告しました（exit 2）")
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise CliError("step4-gate の出力が JSON ではありません") from exc
    return completed.returncode == 0 and isinstance(payload, dict) and payload.get("invoke_pr_reviewer") is True


def _plan_output(
    *,
    eligible: bool,
    reasons: list[str],
    live_head: str | None,
    live_body: str | None,
    body_path: str | None = None,
    body_text: str | None = None,
) -> dict[str, Any]:
    return {
        "eligible": eligible,
        "reason_codes": reasons,
        "expected_head_sha": live_head,
        "expected_live_body_sha256": body_sha256(live_body) if isinstance(live_body, str) else None,
        "body_file_path": body_path,
        "body_file_sha256": body_sha256(body_text) if isinstance(body_text, str) else None,
    }


def cmd_plan(args: argparse.Namespace, gh_runner: Any = None) -> int:
    reviewer = _read_json(args.reviewer_result_file, "pr-reviewer result file")
    if isinstance(reviewer, dict) and isinstance(reviewer.get("reviewer_verdict"), dict):
        reviewer = reviewer["reviewer_verdict"]
    loop_state = _read_json(args.loop_state_file, "--loop-state-file")
    test_verdict_report = _read_json(args.test_verdict_file, "--test-verdict-file")
    command_hashes = _read_json(args.expected_command_hashes_file, "--expected-command-hashes-file")
    ci_payload, ci_error = parse_ci_wait_output(_read_text(args.wait_ci_output, "--wait-ci-output"))
    summary_text = (
        _read_text(args.runtime_summary_file, "--runtime-summary-file") if args.runtime_summary_file else None
    )

    live = fetch_live_pr(args.repo, args.pr_number, gh_runner)
    live_head, live_body = live["head"], live["body"]

    counts, state_error = lane_history_counts(loop_state)
    if counts is None:
        raise CliError(f"--loop-state-file: {state_error}")

    vc_valid = evaluate_vc_current_head(
        args.loop_state_file, live_head, args.expected_contract_body_sha256, args.expected_command_hashes_file
    )
    ci_valid = ci_error is None and required_ci_valid_for_head(ci_payload, live_head)

    provenance_ok = (
        runtime_summary_provenance_ok(args.worktree, live_head, args.runtime_summary_file)
        if summary_text is not None
        else False
    )
    blockers = reviewer.get("blockers") if isinstance(reviewer, dict) else None
    refs, extra_reasons = build_evidence_refs(
        live_head_sha=live_head,
        blockers=blockers,
        test_verdict_report=test_verdict_report,
        test_verdict_source=args.test_verdict_file,
        expected_command_hashes=command_hashes,
        runtime_summary_text=summary_text,
        runtime_summary_source=args.runtime_summary_file,
        runtime_summary_provenance_ok=provenance_ok,
    )
    decision = decide_body_only_repair(reviewer, live_head, vc_valid, ci_valid, live_body, refs, counts["consumed"])
    reasons = list(decision["reason_codes"])
    for code in extra_reasons:
        _add(reasons, code)
    if counts["no_mutation"] > MAX_NO_MUTATION_ENTRIES:
        _add(reasons, "body_only_lane_ended")
    if reasons or not decision["eligible"]:
        _emit(_plan_output(eligible=False, reasons=reasons, live_head=live_head, live_body=live_body))
        return EXIT_NEGATIVE

    completed_body = decision["body_plan"]["completed_body_text"]
    failures = run_completed_body_validators(
        completed_body, repo=args.repo, issue_number=args.issue_number, worktree=args.worktree
    )
    if failures:
        out = _plan_output(
            eligible=False, reasons=["completed_body_validator_failed"], live_head=live_head, live_body=live_body
        )
        out["validator_failures"] = failures
        _emit(out)
        return EXIT_NEGATIVE
    try:
        write_text_atomically(args.body_out, completed_body)
    except OSError as exc:
        raise CliError(f"--body-out に書き込めません: {type(exc).__name__}") from exc
    _emit(
        _plan_output(
            eligible=True,
            reasons=[],
            live_head=live_head,
            live_body=live_body,
            body_path=str(Path(args.body_out).resolve()),
            body_text=completed_body,
        )
    )
    return EXIT_OK


def cmd_record(args: argparse.Namespace) -> int:
    loop_state = _read_json(args.loop_state_file, "--loop-state-file")
    new_state, error = apply_record(loop_state, args.state)
    if new_state is None:
        _emit({"recorded": False, "state": args.state, "reason_code": error})
        return EXIT_NEGATIVE
    serialized = json.dumps(new_state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        write_text_atomically(args.loop_state_file, serialized)
    except OSError as exc:
        raise CliError(f"--loop-state-file に書き込めません: {type(exc).__name__}") from exc
    counts, _error = lane_history_counts(new_state)
    _emit({"recorded": True, "state": args.state, "reason_code": None, "lane_entries": counts})
    return EXIT_OK


def cmd_guard(args: argparse.Namespace, gh_runner: Any = None) -> int:
    body_file_text = _read_text(args.body_file, "--body-file")
    live = fetch_live_pr(args.repo, args.pr_number, gh_runner)
    decision = guard_decision(
        live["head"],
        live["body"],
        args.expected_head_sha,
        args.expected_live_body_sha256,
        body_file_text,
        args.body_file_sha256,
    )
    decision["live_head_sha"] = live["head"]
    decision["live_body_sha256"] = body_sha256(live["body"])
    _emit(decision)
    return EXIT_OK if decision["result"] == FRESHNESS_PROCEED else EXIT_NEGATIVE


def load_workflow_index(workflow_dir: str) -> dict[str, list[frozenset[str] | None]]:
    """``.github/workflows/*.yml`` の ``name`` -> ``on.pull_request.types`` の index を作る。"""
    import yaml  # CLI 層でだけ使う

    directory = Path(workflow_dir)
    if not directory.is_dir():
        raise CliError(f"--workflow-dir が directory ではありません: {workflow_dir}")
    index: dict[str, list[frozenset[str] | None]] = {}
    for path in sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")]):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        name, types = workflow_trigger_info(document, f".github/workflows/{path.name}")
        index.setdefault(name, []).append(types)
    return index


def cmd_ci_freshness(args: argparse.Namespace) -> int:
    watermark = parse_timestamp(args.body_edit_updated_at)
    if watermark is None:
        raise CliError("--body-edit-updated-at を ISO 8601 として解釈できません")
    payload, error = parse_ci_wait_output(_read_text(args.wait_ci_output, "--wait-ci-output"))
    if payload is None:
        _emit({"fresh": False, "retryable": False, "reason_codes": [error], "checks": []})
        return EXIT_NEGATIVE
    result = evaluate_ci_freshness(payload, watermark, load_workflow_index(args.workflow_dir), args.expected_head_sha)
    _emit(result)
    return EXIT_OK if result["fresh"] else EXIT_NEGATIVE


def build_production_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="body-only repair lane production CLI（JSON-in / JSON-out）")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    plan = sub.add_parser("plan", help="evidence refs を materialize し eligible なら completed body を書き出す")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--pr-number", required=True, type=int)
    plan.add_argument("--issue-number", required=True, type=int, help="update_pr.py の --linked-issue と同一")
    plan.add_argument("--worktree", required=True, help="PR の implementation worktree（changed paths 解決の cwd）")
    plan.add_argument("--reviewer-result-file", required=True)
    plan.add_argument("--test-verdict-file", required=True)
    plan.add_argument("--wait-ci-output", required=True)
    plan.add_argument("--runtime-summary-file")
    plan.add_argument("--loop-state-file", required=True)
    plan.add_argument("--expected-contract-body-sha256", required=True)
    plan.add_argument("--expected-command-hashes-file", required=True)
    plan.add_argument("--body-out", required=True)

    record = sub.add_parser("record", help="blockers_history[] へ lane 消費を原子的に記録する")
    record.add_argument("--loop-state-file", required=True)
    record.add_argument("--state", required=True, choices=RECORD_STATES)

    guard = sub.add_parser("guard", help="mutation 直前に live {head, body} を取得して照合する")
    guard.add_argument("--repo", required=True)
    guard.add_argument("--pr-number", required=True, type=int)
    guard.add_argument("--expected-head-sha", required=True)
    guard.add_argument("--expected-live-body-sha256", required=True)
    guard.add_argument("--body-file", required=True)
    guard.add_argument("--body-file-sha256", required=True)

    fresh = sub.add_parser("ci-freshness", help="body 編集後の required CI の freshness を判定する")
    fresh.add_argument("--wait-ci-output", required=True)
    fresh.add_argument("--body-edit-updated-at", required=True)
    fresh.add_argument("--workflow-dir", required=True)
    fresh.add_argument("--expected-head-sha")
    return parser


def run_production(argv: list[str], gh_runner: Any = None) -> int:
    args = build_production_parser().parse_args(argv)
    try:
        if args.subcommand == "plan":
            return cmd_plan(args, gh_runner)
        if args.subcommand == "record":
            return cmd_record(args)
        if args.subcommand == "guard":
            return cmd_guard(args, gh_runner)
        return cmd_ci_freshness(args)
    except CliError as exc:
        _emit({"error": str(exc)})
        return EXIT_RUNTIME


def run_dry_run(argv: list[str]) -> int:
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in SUBCOMMANDS:
        return run_production(args)
    return run_dry_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
