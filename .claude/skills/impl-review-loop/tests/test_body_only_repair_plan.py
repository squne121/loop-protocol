"""Issue #2971: body-only repair lane の production 判断関数・契約文書・統合 regression。

全ての test は production 関数（``body_only_repair_plan``）を直接呼ぶか、実
``adjudicate_vc_result.py`` CLI（``step4-adjudicate --reuse-stored`` /
``step5-terminal-gate``）を subprocess で呼ぶ。skip / xfail は使わない。
test 名の接頭辞 ``test_ac1_`` / ``test_ac2_`` / ``test_ac3_`` / ``test_ac4_`` /
``test_ac5_`` / ``test_ac7_`` は Issue の Verification Commands の ``-k`` 選択と
一致させている。
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL_DIR = ROOT / ".claude" / "skills" / "impl-review-loop"
MODULE_PATH = SKILL_DIR / "scripts" / "body_only_repair_plan.py"
SKILL_MD = SKILL_DIR / "SKILL.md"
STEP5_MD = SKILL_DIR / "steps" / "step-5-feedback-and-termination.md"
FIXTURE_DIR = SKILL_DIR / "tests" / "fixtures"
INCIDENT_FIXTURE = FIXTURE_DIR / "body_only_lane_incident_2963.json"
SMOKE_PROMPT = FIXTURE_DIR / "body_only_lane_runtime_smoke_prompt.md"
HARNESS_PATH = SKILL_DIR / "tests" / "test_independent_vc_terminal_gate_regression.py"


def _load(name: str, path: Path) -> Any:
    """bare module 名の衝突を避け、一意名で登録して load する。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("body_only_repair_plan_issue_2971_under_test", MODULE_PATH)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40

BODY = (
    "## Summary\n変更の要約です。\n\n"
    "## 検証\n- evaluator の hermetic test（39 件）が PASS\n- AC8 の live smoke は未実施です。\n"
)
RUNTIME_BLOCKER = "PR 本文に Runtime Verification Evidence（AC8）が無い"
STALE_BLOCKER = "PR 本文の evaluator 件数が 39 件のまま（head では 47 件）"
PENDING_BLOCKER = "PR 本文の AC8 が 未実施 のまま"
RUNTIME_VALUE = "### AC8\n- Result: PASS（tested_head aaaaaaaa）\n- artifact: artifacts/runtime-verification.json"
COMPLETED_STATUS = "PASS（commit 済み HEAD で実施済み）"


def _refs(head: str = HEAD) -> list[dict[str, Any]]:
    return [
        {"kind": "runtime_evidence", "value": RUNTIME_VALUE, "source": "artifacts/rv.json", "head_sha": head},
        {"kind": "test_count", "value": "47", "source": "artifacts/test-verdict.json", "head_sha": head},
        {"kind": "completed_status", "value": COMPLETED_STATUS, "source": "artifacts/rv.json", "head_sha": head},
    ]


def _reviewer(blockers: list[Any], head: str = HEAD, verdict: str = "REQUEST_CHANGES") -> dict[str, Any]:
    return {"verdict": verdict, "reviewed_head_sha": head, "blockers": blockers, "warnings": []}


def _decide(
    blockers: list[Any] | None = None,
    *,
    reviewer: dict[str, Any] | None = None,
    live_head: str = HEAD,
    vc: Any = True,
    ci: Any = True,
    body: Any = BODY,
    refs: Any = None,
    prior: Any = 0,
) -> dict[str, Any]:
    return mod.decide_body_only_repair(
        reviewer if reviewer is not None else _reviewer(blockers if blockers is not None else [RUNTIME_BLOCKER]),
        live_head,
        vc,
        ci,
        body,
        _refs() if refs is None else refs,
        prior,
    )


def _incident() -> dict[str, Any]:
    return json.loads(INCIDENT_FIXTURE.read_text(encoding="utf-8"))


def _decide_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    return mod.decide_from_fixture(fixture)


def _assert_ineligible(result: dict[str, Any], reason: str) -> None:
    assert result["eligible"] is False, result
    assert result["body_plan"] is None
    assert reason in result["reason_codes"], result


# --- AC1 ----------------------------------------------------------------------


def test_ac1_all_three_kinds_eligible_with_mechanical_completed_body() -> None:
    result = _decide([RUNTIME_BLOCKER, STALE_BLOCKER, PENDING_BLOCKER])

    assert result["eligible"] is True, result
    assert result["reason_codes"] == []
    plan = result["body_plan"]
    assert plan["reviewed_head_sha"] == HEAD
    assert plan["review_body_sha256"] == mod.body_sha256(BODY)
    assert sorted(plan["blocker_kinds"]) == sorted(mod.BLOCKER_KINDS)
    assert len(plan["evidence_refs"]) == 3
    expected = (
        BODY.replace("39 件", "47 件").replace("未実施", COMPLETED_STATUS).rstrip("\n")
        + "\n\n## Runtime Verification Evidence\n"
        + RUNTIME_VALUE
        + "\n"
    )
    assert plan["completed_body_text"] == expected


@pytest.mark.parametrize(
    ("blocker", "kind"),
    [
        (RUNTIME_BLOCKER, "runtime_evidence_section_missing"),
        (STALE_BLOCKER, "stale_count"),
        (PENDING_BLOCKER, "pending_wording"),
    ],
)
def test_ac1_each_closed_kind_alone_is_eligible(blocker: str, kind: str) -> None:
    result = _decide([blocker])

    assert result["eligible"] is True, result
    assert result["body_plan"]["blocker_kinds"] == [kind]
    assert result["body_plan"]["completed_body_text"] != BODY


def test_ac1_runtime_section_is_appended_verbatim_to_body_end() -> None:
    plan = _decide([RUNTIME_BLOCKER])["body_plan"]

    assert plan["completed_body_text"].startswith(BODY.rstrip("\n"))
    assert plan["completed_body_text"].endswith("## Runtime Verification Evidence\n" + RUNTIME_VALUE + "\n")


def test_ac1_stale_count_replaces_exactly_one_literal_and_nothing_else() -> None:
    plan = _decide([STALE_BLOCKER])["body_plan"]

    assert plan["completed_body_text"] == BODY.replace("39 件", "47 件")


def test_ac1_iteration_budget_is_not_an_input_and_never_blocks_eligibility() -> None:
    params = list(inspect.signature(mod.decide_body_only_repair).parameters)
    assert params == [
        "reviewer_result",
        "live_head_sha",
        "vc_current_head_valid",
        "required_ci_valid",
        "live_pr_body",
        "evidence_refs",
        "prior_body_only_repairs",
    ]
    fixture = _incident()
    assert fixture["iteration"] == fixture["max_iterations"]
    assert _decide_fixture(fixture)["eligible"] is True


def test_ac1_closed_pattern_constants_are_fixed() -> None:
    assert mod.BLOCKER_KINDS == ("runtime_evidence_section_missing", "stale_count", "pending_wording")
    assert mod.EVIDENCE_KINDS == ("runtime_evidence", "test_count", "completed_status")
    assert mod.EVIDENCE_REF_KEYS == ("kind", "value", "source", "head_sha")
    assert mod.PENDING_WORDS == ("未実施", "実施する予定", "pending")
    assert mod.MISSING_WORDS == ("無い", "欠落", "missing", "未記載")
    assert mod.DENY_WORDS == (
        "Allowed Paths",
        "Issue 本文",
        "Issue contract",
        "branch",
        "rebase",
        "conflict",
        "テスト追加",
        "実装修正",
    )
    assert mod.RUNTIME_EVIDENCE_HEADING == "## Runtime Verification Evidence"


def test_ac1_module_is_pure_no_io_network_time_or_subprocess_imports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "hashlib", "json", "re", "sys", "typing"}, imported


def test_ac1_decision_is_deterministic_and_does_not_mutate_inputs() -> None:
    reviewer = _reviewer([RUNTIME_BLOCKER, STALE_BLOCKER])
    refs = _refs()
    snapshot = copy.deepcopy((reviewer, refs, BODY))

    first = _decide(reviewer=reviewer, refs=refs)
    second = _decide(reviewer=reviewer, refs=refs)

    assert first == second
    assert (reviewer, refs, BODY) == snapshot


# --- AC2 (negative / false-green) ----------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        "scripts/foo.py の実装にバグがある",
        "src/app.ts を修正すること",
        "tools/run.sh の exit code を直す",
        "util.js の分岐が誤り",
    ],
)
def test_ac2_path_like_code_token_makes_whole_result_ineligible(extra: str) -> None:
    result = _decide([RUNTIME_BLOCKER, extra])

    _assert_ineligible(result, "blocker_deny_list_hit")


@pytest.mark.parametrize("word", mod.DENY_WORDS)
def test_ac2_deny_word_mixed_with_body_only_word_is_ineligible(word: str) -> None:
    mixed = f"PR 本文に Runtime Verification Evidence が無い。あわせて {word} も対応すること"

    result = _decide([mixed])

    _assert_ineligible(result, "blocker_deny_list_hit")


def test_ac2_deny_list_takes_precedence_over_kind_detection() -> None:
    blocker = "PR 本文の evaluator 件数が 39 件のまま（head では 47 件）。helper.py も 47 件に揃えること"

    result = _decide([blocker])

    _assert_ineligible(result, "blocker_deny_list_hit")
    assert "blocker_ambiguous_kind" not in result["reason_codes"]


def test_ac2_bare_code_and_test_words_are_not_deny_tokens() -> None:
    blocker = "コード / テスト は PASS。PR 本文に Runtime Verification Evidence が無い"

    assert _decide([blocker])["eligible"] is True


def test_ac2_ambiguous_blocker_matching_two_kinds_is_ineligible() -> None:
    blocker = "PR 本文に Runtime Verification Evidence が無く、件数も 39 件のまま（head では 47 件）"

    _assert_ineligible(_decide([blocker]), "blocker_ambiguous_kind")


def test_ac2_pending_blocker_naming_two_words_is_ambiguous() -> None:
    blocker = "本文が「未実施 / 実施する予定」のまま"

    _assert_ineligible(_decide([blocker]), "blocker_ambiguous_kind")


@pytest.mark.parametrize("blocker", ["もっと分かりやすくしてほしい", "AC8 の説明が弱い", "", "   "])
def test_ac2_unclassifiable_blocker_is_ineligible(blocker: str) -> None:
    result = _decide([blocker])

    assert result["eligible"] is False
    assert result["reason_codes"] and result["reason_codes"][0] in {"blocker_unclassifiable", "blocker_malformed"}


@pytest.mark.parametrize("blocker", [None, 5, {"text": RUNTIME_BLOCKER}, ["x"]])
def test_ac2_non_string_blocker_is_ineligible(blocker: Any) -> None:
    _assert_ineligible(_decide([blocker]), "blocker_malformed")


def test_ac2_one_unclassifiable_blocker_poisons_otherwise_eligible_set() -> None:
    result = _decide([RUNTIME_BLOCKER, STALE_BLOCKER, "別の懸念がある"])

    _assert_ineligible(result, "blocker_unclassifiable")


def test_ac2_missing_evidence_ref_for_kind_is_ineligible() -> None:
    refs = [ref for ref in _refs() if ref["kind"] != "runtime_evidence"]

    _assert_ineligible(_decide([RUNTIME_BLOCKER], refs=refs), "evidence_ref_missing")


def test_ac2_empty_or_non_list_evidence_refs_is_ineligible() -> None:
    _assert_ineligible(_decide([RUNTIME_BLOCKER], refs=[]), "evidence_ref_missing")
    _assert_ineligible(_decide([RUNTIME_BLOCKER], refs="not-a-list"), "evidence_ref_missing")
    _assert_ineligible(_decide([RUNTIME_BLOCKER], refs={"kind": "x"}), "evidence_ref_missing")


@pytest.mark.parametrize("stale_head", [OTHER_HEAD, "", "aaaaaaaa"])
def test_ac2_evidence_ref_bound_to_another_head_is_invalid(stale_head: str) -> None:
    refs = _refs(head=stale_head)

    result = _decide([RUNTIME_BLOCKER, STALE_BLOCKER, PENDING_BLOCKER], refs=refs)

    assert result["eligible"] is False
    assert any(code in result["reason_codes"] for code in ("evidence_ref_stale_head", "evidence_ref_missing"))


def test_ac2_evidence_ref_head_must_match_both_live_and_reviewed_head() -> None:
    # reviewed == ref head だが live head と不一致の場合は reviewed_head_sha_mismatch で ineligible。
    result = _decide([RUNTIME_BLOCKER], reviewer=_reviewer([RUNTIME_BLOCKER], head=OTHER_HEAD), refs=_refs(OTHER_HEAD))

    _assert_ineligible(result, "reviewed_head_sha_mismatch")


@pytest.mark.parametrize("drop", ["kind", "value", "source", "head_sha"])
def test_ac2_evidence_ref_missing_a_required_key_is_ignored(drop: str) -> None:
    refs = _refs()
    for ref in refs:
        ref.pop(drop)

    assert _decide([RUNTIME_BLOCKER], refs=refs)["eligible"] is False


def test_ac2_evidence_ref_with_empty_value_or_source_is_ignored() -> None:
    refs = _refs()
    refs[0]["value"] = "  "
    assert _decide([RUNTIME_BLOCKER], refs=refs)["eligible"] is False
    refs = _refs()
    refs[0]["source"] = ""
    assert _decide([RUNTIME_BLOCKER], refs=refs)["eligible"] is False


def test_ac2_test_count_ref_value_must_equal_new_count() -> None:
    refs = _refs()
    refs[1]["value"] = "46"

    _assert_ineligible(_decide([STALE_BLOCKER], refs=refs), "evidence_ref_value_mismatch")


def test_ac2_conflicting_refs_of_same_kind_are_ambiguous() -> None:
    refs = _refs() + [{"kind": "completed_status", "value": "完了", "source": "artifacts/x.json", "head_sha": HEAD}]

    _assert_ineligible(_decide([PENDING_BLOCKER], refs=refs), "evidence_ref_ambiguous")


@pytest.mark.parametrize("body", ["## 検証\n- 件数の記載なし。未実施です。\n", "39 件 と 39 件 を含む。\n39 件\n"])
def test_ac2_stale_count_target_count_not_exactly_one_is_ineligible(body: str) -> None:
    _assert_ineligible(_decide([STALE_BLOCKER], body=body), "replacement_target_count_invalid")


def test_ac2_stale_count_does_not_match_inside_a_longer_number() -> None:
    body = "検証は 139 件 でした。\n"

    _assert_ineligible(_decide([STALE_BLOCKER], body=body), "replacement_target_count_invalid")


@pytest.mark.parametrize("body", ["本文に該当語は無い。\n", "未実施と未実施\n"])
def test_ac2_pending_target_count_not_exactly_one_is_ineligible(body: str) -> None:
    _assert_ineligible(_decide([PENDING_BLOCKER], body=body), "replacement_target_count_invalid")


def test_ac2_replacement_value_that_still_contains_pending_wording_is_ineligible() -> None:
    refs = _refs()
    refs[2]["value"] = "まだ未実施"

    _assert_ineligible(_decide([PENDING_BLOCKER], refs=refs), "evidence_ref_value_invalid")


def test_ac2_runtime_section_that_claims_pending_is_not_accepted_as_evidence() -> None:
    refs = _refs()
    refs[0]["value"] = "AC8 は実施する予定です"

    _assert_ineligible(_decide([RUNTIME_BLOCKER], refs=refs), "evidence_ref_value_invalid")


def test_ac2_runtime_section_already_present_means_blocker_is_not_body_only() -> None:
    body = BODY + "\n## Runtime Verification Evidence\n既に記載済み\n"

    _assert_ineligible(_decide([RUNTIME_BLOCKER], body=body), "live_body_runtime_evidence_section_present")


def test_ac2_two_runtime_blockers_do_not_append_the_section_twice() -> None:
    result = _decide([RUNTIME_BLOCKER, "Runtime Verification Evidence が欠落している"])

    _assert_ineligible(result, "duplicate_runtime_evidence_blocker")


def test_ac2_reviewed_head_differs_from_live_head_is_ineligible() -> None:
    result = _decide([RUNTIME_BLOCKER], live_head=OTHER_HEAD)

    _assert_ineligible(result, "reviewed_head_sha_mismatch")


@pytest.mark.parametrize("value", [False, None, "true", 1, 0])
def test_ac2_vc_and_ci_must_be_exactly_true(value: Any) -> None:
    _assert_ineligible(_decide([RUNTIME_BLOCKER], vc=value), "vc_current_head_invalid")
    _assert_ineligible(_decide([RUNTIME_BLOCKER], ci=value), "required_ci_invalid")


@pytest.mark.parametrize("prior", [1, 2, 7])
def test_ac2_second_lane_use_is_ineligible(prior: int) -> None:
    _assert_ineligible(_decide([RUNTIME_BLOCKER], prior=prior), "body_only_repair_already_used")


@pytest.mark.parametrize("prior", [-1, True, False, "0", None, 0.0])
def test_ac2_invalid_prior_count_is_ineligible(prior: Any) -> None:
    _assert_ineligible(_decide([RUNTIME_BLOCKER], prior=prior), "prior_body_only_repairs_invalid")


@pytest.mark.parametrize("verdict", ["APPROVE", "HUMAN_REVIEW_REQUIRED", "", None, "request_changes"])
def test_ac2_verdict_other_than_request_changes_is_ineligible(verdict: Any) -> None:
    reviewer = _reviewer([RUNTIME_BLOCKER], verdict=verdict)

    _assert_ineligible(_decide(reviewer=reviewer), "verdict_not_request_changes")


@pytest.mark.parametrize("blockers", [[], None, "PR 本文に Runtime Verification Evidence が無い"])
def test_ac2_empty_or_malformed_blockers_are_ineligible(blockers: Any) -> None:
    reviewer = {"verdict": "REQUEST_CHANGES", "reviewed_head_sha": HEAD, "blockers": blockers}

    _assert_ineligible(_decide(reviewer=reviewer), "blockers_empty_or_malformed")


@pytest.mark.parametrize(
    "reviewer",
    [None, "REQUEST_CHANGES", [], {"verdict": "REQUEST_CHANGES"}, {"verdict": "REQUEST_CHANGES", "blockers": []}],
)
def test_ac2_malformed_reviewer_result_is_ineligible_without_raising(reviewer: Any) -> None:
    result = mod.decide_body_only_repair(reviewer, HEAD, True, True, BODY, _refs(), 0)

    assert result["eligible"] is False
    assert result["reason_codes"]


def test_ac2_non_string_live_body_and_missing_head_are_ineligible() -> None:
    _assert_ineligible(_decide([RUNTIME_BLOCKER], body=None), "live_pr_body_missing")
    _assert_ineligible(_decide([RUNTIME_BLOCKER], live_head=""), "live_head_sha_missing")


def test_ac2_ineligible_dry_run_markers_never_include_lane_order_markers() -> None:
    lines = mod.dry_run_marker_lines(_decide([RUNTIME_BLOCKER], prior=1))

    assert lines == ["BODY_ONLY_LANE_INELIGIBLE:body_only_repair_already_used"]
    assert not set(lines) & set(mod.LANE_ORDER_MARKERS)


# --- AC3 (freshness / readback) -------------------------------------------------


def _plan(blockers: list[str] | None = None) -> dict[str, Any]:
    result = _decide(blockers or [RUNTIME_BLOCKER, STALE_BLOCKER])
    assert result["eligible"] is True, result
    return result["body_plan"]


def test_ac3_canonicalization_is_crlf_and_trailing_newline_insensitive_only() -> None:
    assert mod.canonicalize_body("a\r\nb\r\n\r\n") == "a\nb"
    assert mod.body_sha256("a\r\nb\n") == mod.body_sha256("a\nb\n\n\n") == mod.body_sha256("a\nb")
    assert mod.body_sha256("a\nb ") != mod.body_sha256("a\nb")
    assert mod.body_sha256("a\n\nb") != mod.body_sha256("a\nb")
    assert mod.body_sha256("日本語") == __import__("hashlib").sha256("日本語".encode("utf-8")).hexdigest()


def test_ac3_freshness_proceeds_only_when_head_and_body_match_the_plan() -> None:
    plan = _plan()

    assert mod.check_body_freshness(plan, HEAD, BODY) == "proceed"
    assert mod.check_body_freshness(plan, HEAD, BODY.replace("\n", "\r\n")) == "proceed"
    assert mod.check_body_freshness(plan, HEAD, BODY + "\n\n") == "proceed"


def test_ac3_body_hash_change_returns_stale_body_rebuild_required() -> None:
    plan = _plan()

    assert mod.check_body_freshness(plan, HEAD, BODY + "\n並行編集された行\n") == "stale_body_rebuild_required"
    assert mod.check_body_freshness(plan, HEAD, BODY.replace("PASS", "PASS ")) == "stale_body_rebuild_required"
    assert mod.check_body_freshness(plan, HEAD, "") == "stale_body_rebuild_required"
    assert mod.check_body_freshness(plan, HEAD, None) == "stale_body_rebuild_required"


def test_ac3_head_change_returns_ineligible_head_changed_and_takes_priority() -> None:
    plan = _plan()

    assert mod.check_body_freshness(plan, OTHER_HEAD, BODY) == "ineligible_head_changed"
    assert mod.check_body_freshness(plan, OTHER_HEAD, BODY + "x") == "ineligible_head_changed"
    assert mod.check_body_freshness(plan, None, BODY) == "ineligible_head_changed"


@pytest.mark.parametrize(
    "plan", [None, {}, "plan", {"reviewed_head_sha": HEAD}, {"reviewed_head_sha": HEAD, "review_body_sha256": "x"}]
)
def test_ac3_malformed_plan_is_fail_closed_for_freshness_and_readback(plan: Any) -> None:
    assert mod.check_body_freshness(plan, HEAD, BODY) == "malformed_body_plan"
    assert mod.verify_body_readback(plan, HEAD, BODY) == "malformed_body_plan"


def test_ac3_readback_ok_only_when_head_unchanged_and_body_equals_completed() -> None:
    plan = _plan()
    completed = plan["completed_body_text"]

    assert mod.verify_body_readback(plan, HEAD, completed) == "ok"


def test_ac3_readback_has_no_spurious_mismatch_for_line_ending_only_differences() -> None:
    completed = _plan()["completed_body_text"]

    assert mod.verify_body_readback(_plan(), HEAD, completed.replace("\n", "\r\n")) == "ok"
    assert mod.verify_body_readback(_plan(), HEAD, completed.rstrip("\n")) == "ok"
    assert mod.verify_body_readback(_plan(), HEAD, completed + "\n\n") == "ok"


def test_ac3_readback_detects_real_differences_and_head_movement() -> None:
    plan = _plan()
    completed = plan["completed_body_text"]

    assert mod.verify_body_readback(plan, HEAD, BODY) == "readback_body_mismatch"
    assert mod.verify_body_readback(plan, HEAD, completed.replace("47 件", "46 件")) == "readback_body_mismatch"
    assert mod.verify_body_readback(plan, HEAD, completed + "追記") == "readback_body_mismatch"
    assert mod.verify_body_readback(plan, HEAD, None) == "readback_body_mismatch"
    assert mod.verify_body_readback(plan, OTHER_HEAD, completed) == "head_changed_after_write"


def test_ac3_plan_review_hash_uses_the_same_canonicalization_as_freshness() -> None:
    plan_lf = _decide([RUNTIME_BLOCKER], body=BODY)["body_plan"]
    plan_crlf = _decide([RUNTIME_BLOCKER], body=BODY.replace("\n", "\r\n"))["body_plan"]

    assert plan_lf["review_body_sha256"] == plan_crlf["review_body_sha256"] == mod.body_sha256(BODY)
    assert plan_lf["completed_body_text"] == plan_crlf["completed_body_text"]


# --- AC4 (documents, per file) --------------------------------------------------

COMMON_TOKENS = [
    "decide_body_only_repair",
    "reuse-stored",
    "dispatch_seq",
    "blockers_history",
    "conflict_hard_stop",
    "already_satisfied",
    "route_to_update_branch",
    "CONFLICTING",
    "DIRTY",
    "BEHIND",
    "lane: body_only_repair",
    "check_body_freshness",
    "verify_body_readback",
    "update_pr_body_hygiene",
    "update_pr.py",
    "expected_head_sha",
    "step5-terminal-gate",
    "step4-adjudicate",
    "terminal gate bypass",
    "carry-forward",
    "max_iterations",
    "prior_body_only_repairs",
    "LOOP_STATE",
]
LANE_ORDER_TOKENS = [
    "check_body_freshness",
    "update_pr_body_hygiene",
    "verify_body_readback",
    "step4-adjudicate --reuse-stored",
    "pr-reviewer",
    "step5-terminal-gate",
]
DOC_FILES = [pytest.param(SKILL_MD, id="SKILL.md"), pytest.param(STEP5_MD, id="step-5-feedback-and-termination.md")]


def _lane_section(text: str) -> str:
    """見出し level を保ったまま、次の同 level 以上の見出しまでを返す。"""
    match = re.search(r"^(#{2,3}) body-only lane（`iteration[^\n]*\n", text, re.M)
    assert match, "body-only lane section not found"
    level = len(match.group(1))
    rest = text[match.end() :]
    end = re.search(r"^#{2,%d} " % level, rest, re.M)
    return match.group(0) + (rest[: end.start()] if end else rest)


def _order_part(section: str) -> str:
    start = section.index("実行順序")
    end = section.index("guard の所在")
    return section[start:end]


@pytest.mark.parametrize("path", DOC_FILES)
def test_ac4_each_file_independently_documents_the_lane_tokens(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    missing = [token for token in COMMON_TOKENS if token not in text]
    assert not missing, f"{path.name} is missing {missing}"


@pytest.mark.parametrize("path", DOC_FILES)
def test_ac4_each_file_lane_section_has_the_same_semantics(path: Path) -> None:
    section = _lane_section(path.read_text(encoding="utf-8"))

    for token in COMMON_TOKENS:
        if token in ("LOOP_STATE", "step4-adjudicate"):
            continue
        assert token in section, f"{path.name} lane section lacks {token}"
    order = _order_part(section)
    positions = [order.index(token) for token in LANE_ORDER_TOKENS]
    assert positions == sorted(positions), (
        f"{path.name} lane order is not freshness -> worker -> readback -> reuse-stored -> reviewer -> gate"
    )
    assert re.search(r"iteration.{0,4}≥.{0,4}max_iterations", section)
    assert "最大 1 回" in section
    assert "verification は再実行しない" in section
    assert "キー集合" in section or "key 集合" in section


@pytest.mark.parametrize("path", DOC_FILES)
def test_ac4_each_file_states_worker_does_not_enforce_expected_head_sha(path: Path) -> None:
    section = _lane_section(path.read_text(encoding="utf-8"))

    assert re.search(r"worker は[^\n]*`expected_head_sha` を強制しない", section)
    assert "check_body_freshness" in section and "verify_body_readback" in section


@pytest.mark.parametrize("path", DOC_FILES)
def test_ac4_each_file_fail_close_row_names_the_body_only_exception(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    rows = [
        line
        for line in text.splitlines()
        if line.startswith("|") and re.search(r"max_iterations", line) and "fail" in line
    ]

    assert rows, f"{path.name} has no max_iterations fail-close row"
    assert all("decide_body_only_repair" in row or "body-only lane" in row for row in rows), rows


def test_ac4_skill_md_has_the_dry_run_section_with_exact_invocation() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"^## body-only lane dry-run[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    assert match, "body-only lane dry-run section not found"
    section = match.group(0)

    assert "body-only-lane-dry-run" in section
    assert (
        "uv run --locked python3 .claude/skills/impl-review-loop/scripts/body_only_repair_plan.py"
        " --dry-run-fixture <fixture>" in section
    )
    assert "verbatim" in section
    assert "preparation" in section and "worktree" in section


def test_ac4_dry_run_cli_path_and_markers_exist_only_in_the_skill_md_dry_run_section() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    section = re.search(r"^## body-only lane dry-run[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    assert section
    outside = text.replace(section.group(0), "")

    assert "--dry-run-fixture" in section.group(0)
    assert "--dry-run-fixture" not in outside
    assert "body_only_repair_plan.py" not in outside
    assert "BODY_ONLY_LANE_" not in text


def test_ac4_runtime_smoke_prompt_contains_only_the_invocation_intent() -> None:
    prompt = SMOKE_PROMPT.read_text(encoding="utf-8")
    first_line = prompt.splitlines()[0]
    fixture_arg = first_line.split(" ", 2)[2]

    assert first_line.startswith("/impl-review-loop body-only-lane-dry-run ")
    assert (ROOT / fixture_arg).is_file()
    assert fixture_arg.endswith("body_only_lane_incident_2963.json")
    for forbidden in ("BODY_ONLY_LANE_", "body_only_repair_plan", "--dry-run-fixture", "uv run", "python3", "```"):
        assert forbidden not in prompt, forbidden


def test_ac4_loop_state_key_set_is_unchanged() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    block = re.search(r"```yaml\nLOOP_STATE:\n(.*?)```", text, re.S)
    assert block
    keys = re.findall(r"^  ([a-z_]+):", block.group(1), re.M)

    assert keys == [
        "issue_number",
        "contract_snapshot_url",
        "contract_snapshot_source",
        "iteration",
        "max_iterations",
        "worktree",
        "branch",
        "last_step",
        "last_loop_verdict",
        "blockers_history",
        "external_research_skip_basis",
        "termination_reason",
        "product_spec_preflight",
        "contract_materialization",
        "vc_adjudication",
    ]


def test_ac4_step5_no_longer_delegates_body_only_decision_to_free_text() -> None:
    text = STEP5_MD.read_text(encoding="utf-8")

    assert "機械的に修正可能と判断した場合のみ" not in text
    assert "decide_body_only_repair" in text


# --- AC5 (real step4-adjudicate --reuse-stored / step5-terminal-gate) -------------

harness = _load("body_only_lane_ref_harness_issue_2971", HARNESS_PATH)


def _lane_decision(prior: int = 0) -> dict[str, Any]:
    fixture = _incident()
    fixture["prior_body_only_repairs"] = prior
    return _decide_fixture(fixture)


def _save_dispatch_seq(ws: Any, seq: int) -> Path:
    path = ws.dir / "dispatch_seq"
    path.write_text(str(seq), encoding="utf-8")
    return path


def _terminal(ws: Any, seq_file: Path, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    return ws.terminal_gate(dispatch_seq=int(seq_file.read_text(encoding="utf-8")), **kwargs)


def test_ac5_lane_reuse_stored_then_fresh_approve_passes_with_saved_dispatch_seq(tmp_path: Path) -> None:
    ws = harness._dispatched(tmp_path)  # 初回 reviewer dispatch（seq=1）→ REQUEST_CHANGES だった想定
    assert _lane_decision()["eligible"] is True

    rc, payload = ws.reuse_stored()
    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True
    assert payload["seq"] == 2
    seq_file = _save_dispatch_seq(ws, payload["seq"])  # reviewer 起動前に保存

    rc, gate = _terminal(ws, seq_file)
    assert rc == 0, gate
    assert gate["route"] == "approved"


def test_ac5_stale_dispatch_seq_from_before_the_lane_does_not_pass(tmp_path: Path) -> None:
    ws = harness._dispatched(tmp_path)
    rc, payload = ws.reuse_stored()
    assert rc == 0 and payload["seq"] == 2

    rc, gate = ws.terminal_gate(dispatch_seq=1)  # lane 前の reviewer result の carry-forward

    assert rc == 1
    assert gate["route"] == "continue_loop"
    assert gate["reason_code"] == "dispatch_seq_mismatch"
    assert gate["rerun_required"]["pr_review"] is True


def test_ac5_lane_without_reuse_stored_dispatch_cannot_reuse_the_old_seq(tmp_path: Path) -> None:
    ws = harness._dispatched(tmp_path)

    rc, gate = ws.terminal_gate(dispatch_seq=2)  # reuse-stored を実行せず seq を自作

    assert rc == 1
    assert gate["reason_code"] == "dispatch_seq_mismatch"


@pytest.mark.parametrize(
    "verdict",
    [
        {
            "verdict": "REQUEST_CHANGES",
            "reviewed_head_sha": harness.HEAD_A,
            "blockers": ["別の blocker"],
            "warnings": [],
        },
        {"verdict": "HUMAN_REVIEW_REQUIRED", "reviewed_head_sha": harness.HEAD_A, "blockers": [], "warnings": []},
        {"verdict": "APPROVE", "reviewed_head_sha": harness.HEAD_A, "blockers": ["残存 blocker"], "warnings": []},
        {"verdict": "APPROVE", "reviewed_head_sha": harness.HEAD_B, "blockers": [], "warnings": []},
    ],
    ids=["request_changes", "human_review_required", "approve_with_blockers", "reviewer_head_mismatch"],
)
def test_ac5_non_approve_or_inconsistent_fresh_reviewer_result_does_not_pass(
    tmp_path: Path, verdict: dict[str, Any]
) -> None:
    ws = harness._dispatched(tmp_path)
    rc, payload = ws.reuse_stored()
    assert rc == 0
    seq_file = _save_dispatch_seq(ws, payload["seq"])

    rc, gate = _terminal(ws, seq_file, verdict=verdict)

    assert rc == 1, gate
    assert gate["route"] != "approved"


@pytest.mark.parametrize("status", ["DIRTY", "BEHIND", "BLOCKED", "UNKNOWN"])
def test_ac5_ineligible_live_mergeability_does_not_pass_even_after_the_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    ws = harness._dispatched(tmp_path)
    rc, payload = ws.reuse_stored()
    assert rc == 0
    seq_file = _save_dispatch_seq(ws, payload["seq"])
    monkeypatch.setattr(
        harness,
        "_live_mergeability",
        lambda head: {
            "head_sha": head,
            "mergeable": "CONFLICTING" if status == "DIRTY" else "MERGEABLE",
            "merge_state_status": status,
        },
    )

    rc, gate = _terminal(ws, seq_file)

    assert rc == 1, gate
    assert gate["route"] != "approved"


def test_ac5_fresh_reviewer_requesting_code_change_returns_to_normal_routing_and_lane_is_consumed(
    tmp_path: Path,
) -> None:
    ws = harness._dispatched(tmp_path)
    rc, payload = ws.reuse_stored()
    assert rc == 0
    seq_file = _save_dispatch_seq(ws, payload["seq"])
    code_change = {
        "verdict": "REQUEST_CHANGES",
        "reviewed_head_sha": harness.HEAD_A,
        "blockers": ["scripts/foo.py の分岐に不具合がある"],
        "warnings": [],
    }

    rc, gate = _terminal(ws, seq_file, verdict=code_change)

    assert rc == 1 and gate["route"] == "continue_loop"
    # lane 消費後（blockers_history に lane: body_only_repair を 1 件追記済み）は、body-only でも
    # code blocker でも 2 回目の lane は選ばれず、通常 routing（iteration 消費 / max_iterations fail-close）へ戻る。
    assert _lane_decision(prior=1)["eligible"] is False
    reviewer = _reviewer(code_change["blockers"], head=harness.HEAD_A)
    assert (
        mod.decide_body_only_repair(reviewer, harness.HEAD_A, True, True, BODY, _refs(harness.HEAD_A), 0)["eligible"]
        is False
    )


def test_ac5_second_lane_use_on_the_same_pr_is_ineligible() -> None:
    first = _lane_decision(prior=0)
    second = _lane_decision(prior=1)

    assert first["eligible"] is True
    assert second["eligible"] is False
    assert second["reason_codes"] == ["body_only_repair_already_used"]


def test_ac5_binding_change_after_the_lane_write_falls_back_to_normal_adjudication(tmp_path: Path) -> None:
    ws = harness._dispatched(tmp_path)

    rc, payload = ws.reuse_stored(head=harness.HEAD_B)  # head が変化 → --reuse-stored は使えない

    assert rc != 0, payload
    assert "invoke_pr_reviewer" not in payload or payload["invoke_pr_reviewer"] is not True
    rc, gate = ws.terminal_gate(dispatch_seq=1, head=harness.HEAD_B)
    assert rc == 1 and gate["route"] != "approved"


# --- AC7 (#2963 incident fixture / negatives) -------------------------------------

INCIDENT_BLOCKERS = [
    "PR 本文に Runtime Verification Evidence（AC8, tested_head aa9e247e）が無い",
    "PR 本文の evaluator 件数が 39 件のまま（head では 47 件）",
]


def test_ac7_incident_fixture_blockers_are_the_verbatim_reviewer_comment_blockers() -> None:
    fixture = _incident()

    assert fixture["reviewer_result"]["blockers"] == INCIDENT_BLOCKERS
    assert (
        fixture["_source_reviewer_comment"]
        == "https://github.com/squne121/loop-protocol/pull/2967#issuecomment-6018363721"
    )
    assert not any(
        mod._deny_hit(blocker) for blocker in INCIDENT_BLOCKERS
    )  # deny list を fixture に合わせて調整していない


def test_ac7_incident_live_body_is_the_pre_repair_body() -> None:
    body = _incident()["live_pr_body"]

    assert "## Runtime Verification Evidence" not in body
    assert body.count("39 件") == 1
    assert body.count("未実施") >= 1


def test_ac7_incident_fixture_selects_the_lane_and_runs_to_readback_ok() -> None:
    fixture = _incident()
    assert fixture["iteration"] == fixture["max_iterations"]
    legacy_route_before = "max_iterations" if fixture["iteration"] >= fixture["max_iterations"] else "continue_loop"
    assert legacy_route_before == "max_iterations"  # before: lane が無ければ fail-close

    decision = _decide_fixture(fixture)

    assert decision["eligible"] is True, decision
    plan = decision["body_plan"]
    assert mod.check_body_freshness(plan, fixture["live_head_sha"], fixture["live_pr_body"]) == "proceed"
    completed = plan["completed_body_text"]
    assert "## Runtime Verification Evidence" in completed
    assert "39 件" not in completed
    assert "47 件" in completed
    assert completed.startswith(fixture["live_pr_body"].replace("39 件", "47 件").rstrip("\n"))
    assert mod.verify_body_readback(plan, fixture["live_head_sha"], completed) == "ok"


def _expect_ineligible(fixture: dict[str, Any], reason: str) -> None:
    _assert_ineligible(_decide_fixture(fixture), reason)


def test_ac7_negative_concurrent_body_edit_is_stale_body_rebuild_required() -> None:
    fixture = _incident()
    plan = _decide_fixture(fixture)["body_plan"]
    edited = fixture["live_pr_body"] + "\n人間が並行して追記した行\n"

    assert mod.check_body_freshness(plan, fixture["live_head_sha"], edited) == "stale_body_rebuild_required"
    assert mod.check_body_freshness(plan, "c" * 40, fixture["live_pr_body"]) == "ineligible_head_changed"
    assert mod.verify_body_readback(plan, fixture["live_head_sha"], fixture["live_pr_body"]) == "readback_body_mismatch"


@pytest.mark.parametrize(
    "extra_blocker",
    [
        "scripts/helper.py の判定ロジックを修正すること",
        "PR 本文に Runtime Verification Evidence が無い。あわせて Allowed Paths を見直すこと",
        "PR 本文の evaluator 件数が 39 件のまま。branch を rebase すること",
        "PR 本文をもう少し整えてほしい",
    ],
    ids=["code_blocker", "deny_and_body_only_mixed", "count_with_branch_rebase", "vague_wording"],
)
def test_ac7_negative_extra_non_body_only_blocker_makes_the_incident_ineligible(extra_blocker: str) -> None:
    fixture = _incident()
    fixture["reviewer_result"]["blockers"].append(extra_blocker)

    result = _decide_fixture(fixture)

    assert result["eligible"] is False, result
    assert result["body_plan"] is None


def test_ac7_negative_missing_or_old_head_evidence_refs() -> None:
    fixture = _incident()
    fixture["evidence_refs"] = []
    _expect_ineligible(fixture, "evidence_ref_missing")

    fixture = _incident()
    for ref in fixture["evidence_refs"]:
        ref["head_sha"] = "9" * 40
    _expect_ineligible(fixture, "evidence_ref_stale_head")

    fixture = _incident()
    fixture["evidence_refs"] = [ref for ref in fixture["evidence_refs"] if ref["kind"] != "test_count"]
    _expect_ineligible(fixture, "evidence_ref_missing")


def test_ac7_negative_multiple_replacement_targets() -> None:
    fixture = _incident()
    fixture["live_pr_body"] += "\n追記: evaluator は 39 件 です。\n"

    _expect_ineligible(fixture, "replacement_target_count_invalid")


def test_ac7_negative_runtime_section_present_in_live_body_or_wrong_head_state() -> None:
    fixture = _incident()
    fixture["live_pr_body"] += "\n## Runtime Verification Evidence\n既存\n"
    _expect_ineligible(fixture, "live_body_runtime_evidence_section_present")

    fixture = _incident()
    fixture["live_head_sha"] = "d" * 40
    _expect_ineligible(fixture, "reviewed_head_sha_mismatch")

    fixture = _incident()
    fixture["vc_current_head_valid"] = False
    _expect_ineligible(fixture, "vc_current_head_invalid")

    fixture = _incident()
    fixture["prior_body_only_repairs"] = 1
    _expect_ineligible(fixture, "body_only_repair_already_used")


def test_ac7_dry_run_cli_markers_come_from_the_production_decision_on_the_same_fixture(tmp_path: Path) -> None:
    fixture = _incident()
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--dry-run-fixture", str(INCIDENT_FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": ""},  # gh / git が解決できない環境でも通る（外部 command を実行しない）
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "BODY_ONLY_LANE_ELIGIBLE",
        "BODY_ONLY_LANE_STEP1_NOT_DISPATCHED",
        "BODY_ONLY_LANE_REPAIR_VIA_UPDATE_PR",
        "BODY_ONLY_LANE_STEP4_REUSE_STORED",
        "BODY_ONLY_LANE_STEP5_TERMINAL_GATE",
    ]
    assert completed.stdout.splitlines() == mod.dry_run_marker_lines(_decide_fixture(fixture))

    negative = _incident()
    negative["reviewer_result"]["blockers"].append("scripts/helper.py を修正すること")
    negative_path = tmp_path / "negative.json"
    negative_path.write_text(json.dumps(negative, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--dry-run-fixture", str(negative_path)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": ""},
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["BODY_ONLY_LANE_INELIGIBLE:blocker_deny_list_hit"]


def test_ac7_dry_run_cli_rejects_unreadable_or_non_object_fixture(tmp_path: Path) -> None:
    missing = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--dry-run-fixture", str(tmp_path / "nope.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    not_object = tmp_path / "list.json"
    not_object.write_text("[]", encoding="utf-8")
    bad = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--dry-run-fixture", str(not_object)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert missing.returncode == 2 and not missing.stdout
    assert bad.returncode == 2 and not bad.stdout
