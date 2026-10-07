"""Issue #2971: body-only repair lane の production 判断関数・production CLI・契約文書・統合 regression。

全ての test は production 関数（``body_only_repair_plan``）を直接呼ぶか、production CLI
（``plan`` / ``record`` / ``guard`` / ``ci-freshness``）および実 ``adjudicate_vc_result.py`` CLI
（``step4-adjudicate --reuse-stored`` / ``step5-terminal-gate``）を subprocess で呼ぶ。skip / xfail は使わない。
test 名の接頭辞 ``test_ac1_`` ... ``test_ac7_`` / ``test_ac10_`` / ``test_ac11_`` は対応 AC を示し、
meta test（``test_ac*_meta_*``）が接頭辞ごとに 1 件以上存在することを ``ast`` で検査する。
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

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
PENDING_BLOCKER = "PR 本文に「未実施」が残っている"
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
    assert set(mod.GRAMMAR_SOURCES) == set(mod.BLOCKER_KINDS) == set(mod.BLOCKER_GRAMMARS)
    for kind, source in mod.GRAMMAR_SOURCES.items():
        assert mod.BLOCKER_GRAMMARS[kind].pattern == source
        assert ".*" not in source and ".+" not in source, f"{kind} grammar must not contain an open wildcard"


def test_ac1_module_pure_layer_has_no_io_network_time_or_environment_access() -> None:
    """pure 層（``# --- CLI layer`` より前の全 def）は I/O・時刻・乱数・環境変数を参照しない。"""
    source = MODULE_PATH.read_text(encoding="utf-8")
    sentinel_line = next(
        number for number, line in enumerate(source.splitlines(), start=1) if line.startswith("# --- CLI layer")
    )
    tree = ast.parse(source)
    forbidden = {
        "open",
        "print",
        "input",
        "subprocess",
        "os",
        "sys",
        "tempfile",
        "Path",
        "shutil",
        "time",
        "random",
        "yaml",
        "importlib",
        "environ",
        "getenv",
        "urllib",
        "socket",
    }
    pure_defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.lineno < sentinel_line]
    assert {node.name for node in pure_defs} >= {
        "decide_body_only_repair",
        "check_body_freshness",
        "verify_body_readback",
        "canonicalize_body",
        "body_sha256",
        "apply_record",
        "evaluate_ci_freshness",
        "bind_test_count_row",
        "build_evidence_refs",
        "guard_decision",
    }
    for node in pure_defs:
        used = {
            child.id if isinstance(child, ast.Name) else child.attr
            for child in ast.walk(node)
            if isinstance(child, (ast.Name, ast.Attribute))
        }
        assert not (used & forbidden), f"{node.name} touches {sorted(used & forbidden)}"
    # datetime は parse_timestamp / evaluate 内の値型としてだけ使い、now() 等の時刻取得は持たない。
    assert "now(" not in source and "utcnow(" not in source and "time.time(" not in source


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


def test_ac2_bare_code_and_test_words_do_not_make_a_blocker_body_only_either() -> None:
    # grammar は blocker 全文を消費する。状況説明の前置きが付いた blocker は eligibility の根拠にならない。
    blocker = "コード / テスト は PASS。PR 本文に Runtime Verification Evidence が無い"

    _assert_ineligible(_decide([blocker]), "blocker_unclassifiable")


def test_ac2_blocker_naming_two_kinds_in_one_sentence_is_unclassifiable() -> None:
    blocker = "PR 本文に Runtime Verification Evidence が無く、件数も 39 件のまま（head では 47 件）"

    _assert_ineligible(_decide([blocker]), "blocker_unclassifiable")


def test_ac2_overlapping_grammars_are_reported_as_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    # 実 grammar は構造上排他的だが、複数 grammar に一致した場合の fail-closed 経路は固定しておく。
    monkeypatch.setitem(
        mod.BLOCKER_GRAMMARS, "pending_wording", re.compile(mod.GRAMMAR_SOURCES["runtime_evidence_section_missing"])
    )

    _assert_ineligible(_decide([RUNTIME_BLOCKER]), "blocker_ambiguous_kind")


def test_ac2_pending_blocker_naming_two_words_is_unclassifiable() -> None:
    blocker = "PR 本文に「未実施 / 実施する予定」が残っている"

    _assert_ineligible(_decide([blocker]), "blocker_unclassifiable")


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
    result = _decide([RUNTIME_BLOCKER, "PR 本文に Runtime Verification Evidence が欠落している"])

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
    assert completed.stdout.splitlines() == [
        "BODY_ONLY_LANE_INELIGIBLE:blocker_unclassifiable",
        "BODY_ONLY_LANE_INELIGIBLE:blocker_deny_list_hit",
    ]


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


# --- AC2 (whole-blocker closed grammar) -------------------------------------------

SUBSTANTIVE_FAILURE_CLASSES = [
    # (id, blocker): 2 回目レビューの 3 failure class（blocker 全文を grammar が消費できない）。
    ("stale_count_plus_unknown", "PR 本文の evaluator 件数が 39 件のまま（head では 47 件）。API の返却値も直すこと"),
    ("runtime_evidence_plus_unknown", "Runtime Verification Evidence が無い。判定ロジックも直すこと"),
    (
        "runtime_evidence_with_prefix_plus_unknown",
        "PR 本文に Runtime Verification Evidence が無い。判定ロジックも直すこと",
    ),
    (
        "body_only_phrase_plus_unknown_clause",
        "PR 本文に「未実施」が残っている。あわせて retry 回数の既定値も見直すこと",
    ),
    ("prefix_text_before_grammar", "全体的に良いが、PR 本文に Runtime Verification Evidence が無い"),
    ("suffix_text_after_grammar", "PR 本文に Runtime Verification Evidence が無い ので追記すること"),
]


@pytest.mark.parametrize(
    "blocker", [b for _, b in SUBSTANTIVE_FAILURE_CLASSES], ids=[i for i, _ in SUBSTANTIVE_FAILURE_CLASSES]
)
def test_ac2_residual_substantive_clause_makes_the_blocker_unclassifiable(blocker: str) -> None:
    result = _decide([blocker])

    _assert_ineligible(result, "blocker_unclassifiable")


def test_ac2_residual_substantive_clause_poisons_the_incident_fixture() -> None:
    for _, blocker in SUBSTANTIVE_FAILURE_CLASSES:
        fixture = _incident()
        fixture["reviewer_result"]["blockers"].append(blocker)

        _expect_ineligible(fixture, "blocker_unclassifiable")


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文に Runtime Verification Evidence が無い",
        "PR 本文に Runtime Verification Evidence（AC8, tested_head aa9e247e）が無い",
        "PR 本文にRuntime Verification Evidence（AC8, tested_head aa9e247e）が無い",
        "PR body には Runtime Verification Evidence section が欠落している。",
        "PR 本文に Runtime Verification Evidence セクションが missing",
        "PR 本文に Runtime Verification Evidence（AC1, AC2, head 1234567）はない.",
        "PR 本文に Runtime Verification Evidence 未記載",
        "  PR 本文に Runtime Verification Evidence が欠落  ",
    ],
)
def test_ac2_runtime_grammar_accepts_the_closed_phrasings(blocker: str) -> None:
    kind, _info = mod._classify_blocker(blocker)

    assert kind == "runtime_evidence_section_missing"


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文に Runtime Verification Evidence（AC1,AC2）が無い",  # 区切りは `, ` のみ
        "PR 本文に Runtime Verification Evidence（AC）が無い",
        "PR 本文に Runtime Verification Evidence（head 123456）が無い",  # 7 桁未満
        "PR 本文に Runtime Verification Evidence（head ABCDEF1）が無い",  # 大文字 hex
        "PR 本文に Runtime Verification Evidence (AC8) が無い",  # 半角括弧
        "PR 本文に Runtime Verification Evidence が無い\n追記すること",
        "PR 本文で Runtime Verification Evidence が無い",
        "PR 本文に runtime verification evidence が無い",
        "PR 本文に Runtime Verification Evidence が不足している",
        "PR 本文に Runtime Verification Evidence が無いため追記",
    ],
)
def test_ac2_runtime_grammar_rejects_anything_outside_the_closed_set(blocker: str) -> None:
    kind, info = mod._classify_blocker(blocker)

    assert kind is None
    assert info["reason"] == "blocker_unclassifiable"


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文の evaluator 件数が 39 件のまま（head では 47 件）",
        "PR body の evaluator 件数が 39件のまま（current head では 47件）",
        "PR 本文の evaluator 件数が 39 件のまま（current head では 47 件）。",
        "PR 本文の test_foo.py 件数が 3 件のまま（head では 4 件）",
        "PR 本文のevaluator 件数が 39 件のまま（head では 47 件）",
    ],
)
def test_ac2_stale_count_grammar_accepts_the_closed_phrasings(blocker: str) -> None:
    kind, info = mod._classify_blocker(blocker)

    assert kind == "stale_count", info


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文の evaluator 件数が 39 件のまま（head では 39 件）",  # 旧 == 新
        "PR 本文の evaluator 件数が 39 件のまま（head では 47 件",
        "PR 本文の 評価器 件数が 39 件のまま（head では 47 件）",  # SUBJECT は ASCII のみ
        "PR 本文の evaluator 件数が 39 個のまま（head では 47 個）",
        "PR 本文の evaluator 件数が 39 件のまま（HEAD では 47 件）",
    ],
)
def test_ac2_stale_count_grammar_rejects_anything_outside_the_closed_set(blocker: str) -> None:
    kind, info = mod._classify_blocker(blocker)

    assert kind is None
    assert info["reason"] == "blocker_unclassifiable"


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文に「未実施」が残っている",
        "PR 本文に未実施が残っている",
        "PR 本文の「pending」のまま",
        "PR 本文に 「実施する予定」 が残存している",
        "PR body の実施する予定のまま。",
    ],
)
def test_ac2_pending_grammar_accepts_the_closed_phrasings(blocker: str) -> None:
    kind, info = mod._classify_blocker(blocker)

    assert kind == "pending_wording", info
    assert len(info["pending_words"]) == 1


@pytest.mark.parametrize(
    "blocker",
    [
        "PR 本文の AC8 が 未実施 のまま",  # 旧 grammar の語順（AC8 が）は closed set に無い
        "PR 本文に「未完了」が残っている",
        "PR 本文に「未実施」が残っているので直すこと",
        "「未実施」が残っている",
    ],
)
def test_ac2_pending_grammar_rejects_anything_outside_the_closed_set(blocker: str) -> None:
    kind, _info = mod._classify_blocker(blocker)

    assert kind is None


def test_ac2_stale_count_requires_the_old_literal_on_a_line_that_names_the_subject() -> None:
    body = "- evaluator の hermetic test（39 件）が PASS\n- 別集計: 39 件 を処理\n"

    plan = _decide([STALE_BLOCKER], body=body)["body_plan"]

    assert plan["completed_body_text"] == body.replace(
        "evaluator の hermetic test（39 件）", "evaluator の hermetic test（47 件）"
    )


def test_ac2_stale_count_in_a_line_without_the_subject_token_is_not_a_target() -> None:
    body = "- 件数は 39 件 でした\n- evaluator の説明のみ\n"

    _assert_ineligible(_decide([STALE_BLOCKER], body=body), "replacement_target_count_invalid")


def test_ac2_stale_count_two_subject_lines_with_the_old_literal_is_ineligible() -> None:
    body = "- evaluator の件数 39 件\n- evaluator の別表 39 件\n"

    _assert_ineligible(_decide([STALE_BLOCKER], body=body), "replacement_target_count_invalid")


def test_ac2_stale_count_subject_tokens_must_all_appear_on_the_target_line() -> None:
    # SUBJECT `evaluator helper`（token {evaluator, helper}）は、`helper` を含まない行を対象にしない。
    blocker = "PR 本文の evaluator helper 件数が 39 件のまま（head では 47 件）"

    _assert_ineligible(_decide([blocker], body="- evaluator の件数 39 件\n"), "replacement_target_count_invalid")
    plan = _decide([blocker], body="- evaluator_helper の件数 39 件\n")["body_plan"]
    assert plan["completed_body_text"] == "- evaluator_helper の件数 47 件\n"


NEGATIVE_MUTATION_CORPUS = [blocker for _, blocker in SUBSTANTIVE_FAILURE_CLASSES]


def test_ac2_mutation_partial_match_grammar_would_make_the_negative_corpus_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """grammar を ``fullmatch`` から partial match（``search``）へ緩めると、negative corpus は全て
    eligible に化ける。つまり上の negative test は grammar の全文消費に依存しており、false-green ではない。"""
    for blocker in NEGATIVE_MUTATION_CORPUS:
        assert _decide([blocker])["eligible"] is False

    monkeypatch.setattr(mod, "_consume", lambda pattern, text: pattern.search(text))

    escaped = [blocker for blocker in NEGATIVE_MUTATION_CORPUS if _decide([blocker])["eligible"]]
    # PREFIX（`PR 本文` / `PR body`）を欠く blocker は partial match でも grammar に入れないので漏れない。
    assert escaped == [blocker for blocker in NEGATIVE_MUTATION_CORPUS if blocker.count("PR 本文")]
    assert len(escaped) >= 5


def test_ac2_mutation_prefix_only_match_still_leaks_suffix_clauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_consume", lambda pattern, text: pattern.match(text))

    leaked = [
        blocker
        for _, blocker in SUBSTANTIVE_FAILURE_CLASSES
        if blocker.startswith("PR 本文") and _decide([blocker])["eligible"]
    ]
    assert leaked, "prefix match must leak at least the suffix-clause cases"


def test_ac2_grammar_is_the_only_eligibility_basis_not_a_deny_list() -> None:
    # deny 語を一切含まない substantive 句でも grammar が消費できなければ ineligible。
    blocker = "PR 本文に Runtime Verification Evidence が無い。判定ロジックも直すこと"
    assert not mod._deny_hit(blocker)

    result = _decide([blocker])

    _assert_ineligible(result, "blocker_unclassifiable")
    assert "blocker_deny_list_hit" not in result["reason_codes"]


def test_ac2_grammar_non_matching_blocker_with_a_deny_word_reports_both_reasons() -> None:
    result = _decide(["PR 本文に Runtime Verification Evidence が無い。branch も直すこと"])

    assert result["reason_codes"][:2] == ["blocker_unclassifiable", "blocker_deny_list_hit"]


# --- AC6 (wrapper-only mutation / no edit of protected scripts) ---------------------

PROTECTED_SCRIPTS = (
    ".claude/skills/open-pr/scripts/update_pr.py",
    ".claude/skills/impl-review-loop/scripts/route_loop_verdict_v2.py",
    ".claude/skills/impl-review-loop/scripts/adjudicate_vc_result.py",
    ".claude/skills/impl-review-loop/scripts/wait_ci_checks.py",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)


def test_ac6_protected_scripts_are_not_modified_relative_to_the_base() -> None:
    base = next(
        (ref for ref in ("origin/main", "main") if _git("rev-parse", "--verify", "--quiet", ref).returncode == 0),
        "HEAD",
    )

    completed = _git("diff", "--exit-code", base, "--", *PROTECTED_SCRIPTS)

    assert completed.returncode == 0, completed.stdout


def test_ac6_production_module_never_edits_a_pr_body_directly() -> None:
    """PR body mutation は ``update_pr.py`` wrapper 経由のみ。production CLI は ``gh pr edit`` / REST PATCH の
    argv を一切構築せず、gh は ``pr view`` / ``issue view``（read-only）だけを呼ぶ。"""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    assert "edit" not in strings  # gh pr edit の argv を組み立てない
    assert not any("--method" in value or re.search(r"\bPATCH\b", value) for value in strings)
    assert "view" in strings


def test_ac6_cli_calls_only_read_only_gh_subcommands(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan()
    assert rc == 0, payload
    rc, _payload = env.guard()
    assert rc == 0

    calls = env.gh_calls()
    assert calls, "fake gh was never invoked"
    assert all(call[:2] in (["pr", "view"], ["issue", "view"]) for call in calls), calls


# --- AC10 (production CLI integration, subprocess) --------------------------------------


def _run_cli(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), *argv], capture_output=True, text=True, check=False, env=env
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (
        f"stdout must be exactly one JSON: rc={completed.returncode} out={completed.stdout!r} err={completed.stderr}"
    )
    return completed.returncode, json.loads(lines[0]), completed.stderr


FAKE_GH_SOURCE = """#!__PYTHON__
import json, os, sys

state = json.load(open(os.environ["FAKE_GH_STATE"], encoding="utf-8"))
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    updated = state.get("updated_at", "2026-10-07T00:00:00Z")
    print(json.dumps({"headRefOid": state["head"], "body": state["body"], "updatedAt": updated}))
elif args[:2] == ["issue", "view"]:
    print(json.dumps({"body": state.get("issue_body", "")}))
else:
    sys.stderr.write("unexpected gh invocation: " + " ".join(args))
    sys.exit(1)
"""

RUNTIME_SUMMARY = (
    "### AC8: 実 Skill を dry-run で起動し、判定結果と順序を確認する\n\n"
    "- Result: PASS（最終 acceptance は commit 済み HEAD 上の 1 試行）\n"
    "- artifact: artifacts/runtime-smoke/summary.md\n"
)
EVALUATOR_COMMAND = "uv run --locked pytest .claude/skills/x/tests/test_foo_reachability_evaluator.py -q"
EVALUATOR_SUBJECT = ".claude/skills/x/tests/test_foo_reachability_evaluator.py"
WHOLE_SUITE_COMMAND = "uv run --locked pytest .claude/skills/x/tests/ -q"


def _git_run(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _ci_check(
    name: str, workflow: str, *, started: str, completed: str = "2026-10-07T00:10:00Z", bucket: str = "pass"
) -> dict[str, Any]:
    return {
        "name": name,
        "bucket": bucket,
        "state": "SUCCESS" if bucket == "pass" else "FAILURE",
        "workflow": workflow,
        "link": f"https://example.invalid/{name}",
        "startedAt": started,
        "completedAt": completed,
    }


def _ci_wait_line(
    head: str, checks: list[dict[str, Any]], *, status: str = "passed", current_head: str | None = None
) -> str:
    payload = {
        "schema": "CI_WAIT_RESULT_V1",
        "status": status,
        "repo": "o/r",
        "pr_number": 7,
        "head_sha": head,
        "current_head_sha": current_head or head,
        "required_only": True,
        "checks": checks,
        "elapsed_seconds": 1,
        "interval_seconds": 15,
        "timeout_seconds": 1800,
        "error_code": None,
        "message": None,
    }
    return "noise before\nCI_WAIT_RESULT_V1_JSON=" + json.dumps(payload, ensure_ascii=True) + "\n"


class PlanEnv:
    """tmp の git worktree・fake gh・実 ``step4-adjudicate`` で persist した LOOP_STATE を持つ plan 用環境。"""

    def __init__(self, tmp_path: Path, *, live_body: str | None = None) -> None:
        self.root = tmp_path
        self.repo = tmp_path / "wt"
        self.repo.mkdir()
        _git_run(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        _git_run(self.repo, "add", "-A")
        _git_run(self.repo, "commit", "-q", "-m", "base")
        _git_run(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        _git_run(self.repo, "add", "-A")
        _git_run(self.repo, "commit", "-q", "-m", "feature")
        self.head = _git_run(self.repo, "rev-parse", "HEAD")

        self.fixture = _incident()
        self.live_body = live_body if live_body is not None else self.fixture["live_pr_body"]
        self.blockers = list(self.fixture["reviewer_result"]["blockers"])

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH_SOURCE.replace("__PYTHON__", sys.executable), encoding="utf-8")
        gh.chmod(0o755)
        self.gh_state = tmp_path / "gh_state.json"
        self.gh_log = tmp_path / "gh_log.jsonl"
        self.set_live(self.head, self.live_body)

        self.ws = harness.Workspace(tmp_path / "ws")
        self.ws.dir.mkdir()
        self.verdict = self.verdict_report(self.head)
        rc, payload = self.ws.adjudicate(head=self.head, verdict=self.verdict)
        assert rc == 0, payload
        self.hashes = list(harness._hashes(False))
        self.hashes_file = self.write("expected_hashes.json", self.hashes)
        self.verdict_file = self.write("verdict.json", self.verdict)
        self.reviewer_file = self.write("reviewer.json", self.reviewer())
        self.wait_ci_file = self.write(
            "wait_ci.txt", _ci_wait_line(self.head, [_ci_check("test", "ci", started="2026-10-07T00:01:00Z")])
        )
        self.summary_file = self.write("summary.md", RUNTIME_SUMMARY)
        self.body_out = tmp_path / "completed_body.md"

    # -- builders ------------------------------------------------------------

    def write(self, name: str, value: Any) -> str:
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        return str(path)

    def verdict_report(
        self,
        head: str,
        *,
        result: str = "PASS",
        evaluator_passed: int = 47,
        extra_evaluator_row: bool = False,
        evaluator_status: str = "pass",
        notes_only: bool = False,
    ) -> dict[str, Any]:
        report = harness._test_verdict(head, harness.BODY_A, False, result=result)
        rows = report["runtime_ac_results"]
        rows[0]["command"] = EVALUATOR_COMMAND
        rows[0]["status"] = evaluator_status
        if notes_only:
            rows[0]["notes"] = f"{evaluator_passed} passed"
        else:
            rows[0]["test_count"] = {"subject": EVALUATOR_SUBJECT, "passed": evaluator_passed}
        rows[1]["command"] = WHOLE_SUITE_COMMAND
        rows[1]["test_count"] = {"subject": ".claude/skills/x/tests/", "passed": 47}
        if extra_evaluator_row:
            rows[1]["command"] = EVALUATOR_COMMAND + " --maxfail=1"
            rows[1]["test_count"] = {"subject": EVALUATOR_SUBJECT, "passed": 47}
        return report

    def reviewer(self, *, blockers: list[str] | None = None, head: str | None = None) -> dict[str, Any]:
        return {
            "verdict": "REQUEST_CHANGES",
            "reviewed_head_sha": head or self.head,
            "blockers": blockers if blockers is not None else self.blockers,
            "warnings": [],
        }

    def set_live(self, head: str, body: str, updated_at: str = "2026-10-07T00:00:00Z") -> None:
        self.gh_state.write_text(
            json.dumps(
                {
                    "head": head,
                    "body": body,
                    "updated_at": updated_at,
                    "issue_body": "## 概要\n日本語の Issue 本文です。\n",
                }
            ),
            encoding="utf-8",
        )

    def gh_calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def environment(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_STATE": str(self.gh_state),
            "FAKE_GH_LOG": str(self.gh_log),
        }

    # -- production CLI wrappers -----------------------------------------------

    def plan(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        values: dict[str, Any] = {
            "--repo": "o/r",
            "--pr-number": "7",
            "--issue-number": "2963",  # 本文が参照する linked Issue（validator の LP057 と整合）
            "--worktree": str(self.repo),
            "--reviewer-result-file": self.reviewer_file,
            "--test-verdict-file": self.verdict_file,
            "--wait-ci-output": self.wait_ci_file,
            "--runtime-summary-file": self.summary_file,
            "--loop-state-file": str(self.ws.loop_state),
            "--expected-contract-body-sha256": harness.BODY_A,
            "--expected-command-hashes-file": self.hashes_file,
            "--body-out": str(self.body_out),
        }
        values.update(overrides)
        argv = ["plan"]
        for flag, value in values.items():
            if value is not None:
                argv += [flag, str(value)]
        rc, payload, _err = _run_cli(argv, env=self.environment())
        return rc, payload

    def record(self, state: str) -> tuple[int, dict[str, Any]]:
        rc, payload, _err = _run_cli(
            ["record", "--loop-state-file", str(self.ws.loop_state), "--state", state], env=self.environment()
        )
        return rc, payload

    def guard(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        plan_rc, plan = self.plan_cached()
        values: dict[str, Any] = {
            "--repo": "o/r",
            "--pr-number": "7",
            "--expected-head-sha": plan["expected_head_sha"],
            "--expected-live-body-sha256": plan["expected_live_body_sha256"],
            "--body-file": plan["body_file_path"],
            "--body-file-sha256": plan["body_file_sha256"],
        }
        values.update(overrides)
        argv = ["guard"]
        for flag, value in values.items():
            argv += [flag, str(value)]
        rc, payload, _err = _run_cli(argv, env=self.environment())
        return rc, payload

    def plan_cached(self) -> tuple[int, dict[str, Any]]:
        if not hasattr(self, "_plan"):
            self._plan = self.plan()
        return self._plan


def test_ac10_plan_materializes_current_head_evidence_and_writes_the_completed_body(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan()

    assert rc == 0, payload
    assert payload["eligible"] is True and payload["reason_codes"] == []
    assert payload["expected_head_sha"] == env.head
    assert payload["expected_live_body_sha256"] == mod.body_sha256(env.live_body)
    body_text = Path(payload["body_file_path"]).read_text(encoding="utf-8")
    assert Path(payload["body_file_path"]) == env.body_out.resolve()
    assert payload["body_file_sha256"] == mod.body_sha256(body_text)
    assert "## Runtime Verification Evidence\n" + RUNTIME_SUMMARY.strip("\n") in body_text
    assert "39 件" not in body_text and "47 件" in body_text
    # 同一 fixture を production 関数に直接通した結果（refs を手で渡した場合）と completed body が一致する。
    expected = mod.decide_body_only_repair(
        env.reviewer(),
        env.head,
        True,
        True,
        env.live_body,
        [
            {"kind": "runtime_evidence", "value": RUNTIME_SUMMARY.strip("\n"), "source": "s", "head_sha": env.head},
            {"kind": "test_count", "value": "47", "source": "s", "head_sha": env.head},
        ],
        0,
    )
    assert expected["eligible"] is True
    assert body_text == expected["body_plan"]["completed_body_text"]
    # 全 edge が read-only の gh 呼出し（pr view / issue view）で完結している。
    assert all(call[:2] in (["pr", "view"], ["issue", "view"]) for call in env.gh_calls())


def test_ac10_plan_exit_codes_and_single_json_for_ineligible_and_runtime_errors(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan(
        **{"--reviewer-result-file": env.write("rv.json", env.reviewer(blockers=["API の返却値も直すこと"]))}
    )
    assert rc == 1 and payload["eligible"] is False
    assert "blocker_unclassifiable" in payload["reason_codes"]
    assert payload["body_file_path"] is None and not env.body_out.exists()

    rc, payload = env.plan(**{"--reviewer-result-file": str(tmp_path / "missing.json")})
    assert rc == 2 and "error" in payload


def test_ac10_plan_head_mismatch_between_reviewer_and_live_is_ineligible(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    env.set_live("f" * 40, env.live_body)

    rc, payload = env.plan()

    assert rc == 1
    assert "reviewed_head_sha_mismatch" in payload["reason_codes"]
    assert not env.body_out.exists()


def test_ac10_plan_artifact_head_mismatch_and_non_pass_test_verdict_are_ineligible(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan(**{"--test-verdict-file": env.write("v_head.json", env.verdict_report("e" * 40))})
    assert rc == 1 and "test_verdict_head_mismatch" in payload["reason_codes"]

    rc, payload = env.plan(
        **{"--test-verdict-file": env.write("v_fail.json", env.verdict_report(env.head, result="FAIL"))}
    )
    assert rc == 1 and "test_verdict_not_pass" in payload["reason_codes"]
    assert not env.body_out.exists()


def test_ac10_plan_ci_not_passed_or_other_head_is_ineligible(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    checks = [_ci_check("test", "ci", started="2026-10-07T00:01:00Z")]

    rc, payload = env.plan(
        **{"--wait-ci-output": env.write("pending.txt", _ci_wait_line(env.head, checks, status="pending_timeout"))}
    )
    assert rc == 1 and "required_ci_invalid" in payload["reason_codes"]

    rc, payload = env.plan(**{"--wait-ci-output": env.write("other.txt", _ci_wait_line("c" * 40, checks))})
    assert rc == 1 and "required_ci_invalid" in payload["reason_codes"]

    rc, payload = env.plan(
        **{"--wait-ci-output": env.write("two.txt", _ci_wait_line(env.head, checks) + _ci_wait_line(env.head, checks))}
    )
    assert rc == 1 and "required_ci_invalid" in payload["reason_codes"]

    rc, payload = env.plan(**{"--wait-ci-output": env.write("garbage.txt", "no result line\n")})
    assert rc == 1 and "required_ci_invalid" in payload["reason_codes"]


def test_ac10_plan_vc_binding_is_re_derived_by_the_existing_step4_gate(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan(**{"--expected-contract-body-sha256": harness.BODY_B})
    assert rc == 1 and "vc_current_head_invalid" in payload["reason_codes"]

    rc, payload = env.plan(**{"--expected-command-hashes-file": env.write("other_hashes.json", ["sha256:" + "9" * 64])})
    assert rc == 1 and "vc_current_head_invalid" in payload["reason_codes"]

    rc, payload = env.plan(**{"--loop-state-file": env.write("empty_state.json", {})})
    assert rc == 1 and "vc_current_head_invalid" in payload["reason_codes"]
    assert not env.body_out.exists()


def test_ac10_plan_runtime_summary_without_provenance_does_not_become_evidence(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    # mtime が commit 時刻より前の artifact は来歴 pre-filter で落とす。
    os.utime(env.summary_file, (1, 1))

    rc, payload = env.plan()

    assert rc == 1
    assert "runtime_summary_provenance_unverified" in payload["reason_codes"]
    assert "evidence_ref_missing" in payload["reason_codes"]


def test_ac10_plan_missing_runtime_summary_is_ineligible_for_a_runtime_blocker(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan(**{"--runtime-summary-file": None})

    assert rc == 1 and "evidence_ref_missing" in payload["reason_codes"]


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"evaluator_passed": 46}, "evidence_ref_value_mismatch"),
        ({"extra_evaluator_row": True}, "test_count_subject_ambiguous"),
        ({"evaluator_status": "fail"}, "test_count_subject_unbound"),
        ({"notes_only": True}, "test_count_subject_unbound"),
    ],
    ids=["value_mismatch", "two_rows_match", "status_not_pass", "count_only_in_notes"],
)
def test_ac10_plan_test_count_ref_is_not_created_without_exact_row_binding(
    tmp_path: Path, kwargs: dict[str, Any], expected_reason: str
) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.plan(**{"--test-verdict-file": env.write("v.json", env.verdict_report(env.head, **kwargs))})

    assert rc == 1, payload
    assert expected_reason in payload["reason_codes"]
    assert not env.body_out.exists()


def test_ac10_plan_does_not_bind_a_suite_wide_count_that_happens_to_equal_the_new_value(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    report = env.verdict_report(env.head)
    del report["runtime_ac_results"][0]["test_count"]  # evaluator 行から件数を外す（suite 全体の 47 は残る）

    rc, payload = env.plan(**{"--test-verdict-file": env.write("v.json", report)})

    assert rc == 1 and "test_count_subject_unbound" in payload["reason_codes"]


def test_ac10_plan_test_count_row_with_command_hash_outside_the_adjudicated_binding_is_not_used(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    report = env.verdict_report(env.head)
    report["runtime_ac_results"][0]["command_hash"] = "sha256:" + "7" * 64

    rc, payload = env.plan(**{"--test-verdict-file": env.write("v.json", report)})

    assert rc == 1 and "test_count_subject_unbound" in payload["reason_codes"]


def test_ac10_plan_runs_the_update_pr_validators_and_a_validator_failure_is_ineligible(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    # 日本語を含まない prose block は update_pr.py と同一の Japanese content validator で fail する。
    Path(env.summary_file).write_text("### AC8\n\nResult: PASS\nall checks are green on this head\n", encoding="utf-8")

    rc, payload = env.plan()

    assert rc == 1, payload
    assert payload["reason_codes"] == ["completed_body_validator_failed"]
    assert "validate_japanese_content" in payload["validator_failures"]
    assert not env.body_out.exists()


def test_ac10_plan_validator_inputs_are_identical_to_a_real_update_pr_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = PlanEnv(tmp_path)
    upd = mod._load_update_pr()
    calls: list[tuple[str, Any]] = []

    def recording_validator(
        body_text: str, changed_paths: Any, linked_issue: Any, linked_issue_body: Any = None
    ) -> dict[str, Any]:
        calls.append(("pr_body", (body_text, changed_paths, linked_issue, linked_issue_body)))
        return {"status": "pass", "errors": []}

    monkeypatch.setattr(upd, "_run_pr_body_validator", recording_validator)
    monkeypatch.setattr(
        upd,
        "_run_japanese_content_validator",
        lambda body_text, threshold=0.1: calls.append(("ja", body_text)) or {"status": "pass"},
    )
    monkeypatch.setattr(upd, "get_linked_issue_body", lambda repo, issue_number: f"ISSUE#{issue_number}@{repo}")
    monkeypatch.setattr(upd, "update_pr", lambda repo, pr_number, body_text: True)
    body_file = tmp_path / "b.md"
    body_file.write_text("## 概要\n本文です。\n", encoding="utf-8")

    monkeypatch.chdir(env.repo)
    assert upd.main(["--pr-number", "7", "--body-file", str(body_file), "--repo", "o/r", "--linked-issue", "2971"]) == 0
    real_calls, calls[:] = list(calls), []

    monkeypatch.chdir(tmp_path)
    failures = mod.run_completed_body_validators(
        body_file.read_text(encoding="utf-8"),
        repo="o/r",
        issue_number=2971,
        worktree=str(env.repo),
        update_pr_module=upd,
    )

    assert failures == []
    assert calls == real_calls
    assert calls[0][1][1] == ["feature.txt"] and calls[0][1][3] == "ISSUE#2971@o/r"
    assert Path.cwd() == tmp_path  # worktree への chdir は復元される


def test_ac10_plan_validator_failure_is_reported_per_validator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    upd = mod._load_update_pr()
    monkeypatch.setattr(upd, "resolve_changed_paths", lambda provided=None: ["x.py"])
    monkeypatch.setattr(upd, "get_linked_issue_body", lambda repo, issue_number: None)
    monkeypatch.setattr(
        upd, "_run_pr_body_validator", lambda body, paths, issue, linked=None: {"status": "fail", "errors": []}
    )
    monkeypatch.setattr(upd, "_run_japanese_content_validator", lambda body, threshold=0.1: {"status": "internal"})

    failures = mod.run_completed_body_validators(
        "x", repo="o/r", issue_number=1, worktree=str(tmp_path), update_pr_module=upd
    )

    assert failures == ["validate_pr_body", "validate_japanese_content"]


def test_ac10_guard_proceeds_only_when_head_live_body_and_body_file_all_match(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.guard()

    assert rc == 0, payload
    assert payload["result"] == "proceed" and payload["reason_code"] is None
    assert payload["live_head_sha"] == env.head
    # guard は live {head, body} を CLI 自身が取得している（worker が gh を即興しない）。
    assert ["pr", "view", "7", "--repo", "o/r", "--json", "headRefOid,body,updatedAt"] in env.gh_calls()


def test_ac10_guard_head_mismatch_returns_non_zero_with_expected_head_sha_mismatch(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    env.plan_cached()
    env.set_live("d" * 40, env.live_body)

    rc, payload = env.guard()

    assert rc == 1
    assert payload["result"] == "ineligible_head_changed" and payload["reason_code"] == "expected_head_sha_mismatch"


def test_ac10_guard_live_body_hash_mismatch_returns_non_zero(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    env.plan_cached()
    env.set_live(env.head, env.live_body + "\n他の actor が追記した行\n")

    rc, payload = env.guard()

    assert rc == 1
    assert payload["result"] == "stale_body_rebuild_required" and payload["reason_code"] == "live_body_hash_mismatch"


def test_ac10_guard_body_file_hash_mismatch_returns_non_zero(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    plan_rc, plan = env.plan_cached()
    assert plan_rc == 0
    Path(plan["body_file_path"]).write_text("改変された completed body\n", encoding="utf-8")

    rc, payload = env.guard()

    assert rc == 1
    assert payload["result"] == "body_file_hash_mismatch" and payload["reason_code"] == "live_body_hash_mismatch"


def test_ac10_guard_stale_expected_live_body_sha256_is_rejected_and_gh_failure_is_a_runtime_error(
    tmp_path: Path,
) -> None:
    env = PlanEnv(tmp_path)

    rc, payload = env.guard(**{"--expected-live-body-sha256": "0" * 64})
    assert rc == 1 and payload["reason_code"] == "live_body_hash_mismatch"

    rc, payload = env.guard(**{"--body-file": str(tmp_path / "missing.md")})
    assert rc == 2 and "error" in payload

    gh = env.bin / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    rc, payload = env.guard()
    assert rc == 2 and "error" in payload


def test_ac10_guard_decision_function_is_pure_and_reuses_check_body_freshness() -> None:
    body_file = "completed\n"
    base = (HEAD, mod.body_sha256(BODY), body_file, mod.body_sha256(body_file))

    ok = mod.guard_decision(HEAD, BODY, *base)
    assert ok == {"result": "proceed", "reason_code": None}
    assert mod.guard_decision(HEAD, BODY.replace("\n", "\r\n") + "\n", *base)["result"] == "proceed"
    assert mod.guard_decision(OTHER_HEAD, BODY, *base)["reason_code"] == "expected_head_sha_mismatch"
    assert mod.guard_decision(HEAD, BODY + "x", *base)["reason_code"] == "live_body_hash_mismatch"
    assert mod.guard_decision(HEAD, BODY, HEAD, base[1], body_file, "0" * 64)["result"] == "body_file_hash_mismatch"
    assert mod.guard_decision(HEAD, None, *base)["result"] == "stale_body_rebuild_required"


def test_ac10_fetch_live_pr_accepts_an_injected_runner_and_never_calls_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, json.dumps({"headRefOid": HEAD, "body": BODY, "updatedAt": "2026-10-07T00:00:00Z"}), ""

    monkeypatch.setattr(mod, "run_gh", lambda argv: (_ for _ in ()).throw(AssertionError("real gh must not be called")))

    live = mod.fetch_live_pr("o/r", 7, runner)

    assert live == {"head": HEAD, "body": BODY, "updated_at": "2026-10-07T00:00:00Z"}
    assert calls == [["pr", "view", "7", "--repo", "o/r", "--json", "headRefOid,body,updatedAt"]]
    for bad in ((1, "", "boom"), (0, "not json", ""), (0, json.dumps({"headRefOid": "", "body": "x"}), "")):
        with pytest.raises(mod.CliError):
            mod.fetch_live_pr("o/r", 7, lambda argv, bad=bad: bad)


# -- record (lane consumption writer) --


def test_ac10_record_writes_two_stage_entries_atomically_and_preserves_existing_keys(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    before = env.ws.state()
    before_keys = set(before)

    rc, payload = env.record("dispatched")
    assert rc == 0 and payload["lane_entries"] == {"consumed": 1, "no_mutation": 0, "dispatched": 1}
    state = env.ws.state()
    assert state["blockers_history"] == [{"lane": "body_only_repair", "outcome": "dispatched"}]
    assert before_keys <= set(state)
    assert state["vc_adjudication"] == before["vc_adjudication"] and state["dispatch"] == before["dispatch"]
    assert [
        path.name for path in env.ws.dir.iterdir() if path.name.startswith(".") and path.name.endswith(".tmp")
    ] == []

    rc, payload = env.record("no_mutation")
    assert rc == 0 and payload["lane_entries"] == {"consumed": 0, "no_mutation": 1, "dispatched": 0}
    assert env.ws.state()["blockers_history"] == [{"lane": "body_only_repair", "outcome": "no_mutation"}]


def test_ac10_record_preserves_unrelated_history_entries_and_closed_entry_fields(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    state = env.ws.state()
    state["blockers_history"] = ["古い blocker 文面", {"iteration": 1, "blockers": ["x"]}]
    env.ws.write_state(state)

    assert env.record("dispatched")[0] == 0
    history = env.ws.state()["blockers_history"]

    assert history[:2] == ["古い blocker 文面", {"iteration": 1, "blockers": ["x"]}]
    assert history[2] == {"lane": "body_only_repair", "outcome": "dispatched"}
    assert set(history[2]) == {"lane", "outcome"}


def test_ac10_record_enforces_the_one_dispatch_and_one_reevaluation_budget(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)

    assert env.record("no_mutation")[0] == 1  # dispatched entry が無い
    assert env.record("dispatched")[0] == 0
    rc, payload = env.record("dispatched")  # 消費済み（dispatched のまま）の lane は 2 回目を起動できない
    assert rc == 1 and payload["reason_code"] == "lane_already_consumed"
    assert env.record("no_mutation")[0] == 0
    assert env.record("dispatched")[0] == 0  # no_mutation が 1 件の間だけ 1 回の再 dispatch が許される
    assert env.record("no_mutation")[0] == 0
    rc, payload = env.record("dispatched")  # no_mutation が 2 件 → lane 終了
    assert rc == 1 and payload["reason_code"] == "lane_ended"
    history = env.ws.state()["blockers_history"]
    assert history == [{"lane": "body_only_repair", "outcome": "no_mutation"}] * 2


def test_ac10_record_rejects_a_malformed_loop_state_without_writing(tmp_path: Path) -> None:
    env = PlanEnv(tmp_path)
    state = env.ws.state()
    state["blockers_history"] = "not-a-list"
    env.ws.write_state(state)
    before = env.ws.loop_state.read_text(encoding="utf-8")

    rc, payload = env.record("dispatched")

    assert rc == 1 and payload["reason_code"] == "blockers_history_not_list"
    assert env.ws.loop_state.read_text(encoding="utf-8") == before


def test_ac10_record_is_the_only_writer_and_state_reload_restores_the_count(tmp_path: Path) -> None:
    """``--loop-state-file`` を disk から再読込（compaction / resume を模す）しても件数が復元され、
    2 回目の lane は起動せず、no_mutation がちょうど 1 件の間だけ再評価される。"""
    env = PlanEnv(tmp_path)
    assert env.plan()[0] == 0

    assert env.record("dispatched")[0] == 0
    rc, payload = env.plan()  # dispatched のまま残った entry は消費済み
    assert rc == 1 and "body_only_repair_already_used" in payload["reason_codes"]

    assert env.record("no_mutation")[0] == 0
    rc, payload = env.plan()  # no_mutation が 1 件 → 1 回だけ再評価できる
    assert rc == 0 and payload["eligible"] is True

    assert env.record("dispatched")[0] == 0 and env.record("no_mutation")[0] == 0
    rc, payload = env.plan()  # no_mutation が 2 件 → lane 終了
    assert rc == 1 and "body_only_lane_ended" in payload["reason_codes"]

    state = env.ws.state()
    assert [entry["outcome"] for entry in state["blockers_history"]] == ["no_mutation", "no_mutation"]


def test_ac10_apply_record_is_pure_and_does_not_mutate_its_input() -> None:
    state = {"blockers_history": [], "iteration": 3}
    snapshot = copy.deepcopy(state)

    new_state, error = mod.apply_record(state, "dispatched")

    assert error is None and state == snapshot
    assert new_state == {"blockers_history": [{"lane": "body_only_repair", "outcome": "dispatched"}], "iteration": 3}
    assert mod.apply_record({}, "dispatched")[0] == {
        "blockers_history": [{"lane": "body_only_repair", "outcome": "dispatched"}]
    }
    assert mod.apply_record({}, "bogus") == (None, "invalid_state")
    assert mod.apply_record([], "dispatched") == (None, "loop_state_not_object")


# -- ci-freshness --

WORKFLOW_DIR = ROOT / ".github" / "workflows"
WATERMARK = "2026-10-07T01:00:00Z"


def _ci_freshness(
    tmp_path: Path,
    checks: list[dict[str, Any]],
    *,
    status: str = "passed",
    head: str = HEAD,
    workflow_dir: Path = WORKFLOW_DIR,
    watermark: str = WATERMARK,
    extra: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    wait_file = tmp_path / "wait.txt"
    wait_file.write_text(_ci_wait_line(head, checks, status=status), encoding="utf-8")
    rc, payload, _err = _run_cli(
        [
            "ci-freshness",
            "--wait-ci-output",
            str(wait_file),
            "--body-edit-updated-at",
            watermark,
            "--workflow-dir",
            str(workflow_dir),
            *(extra or []),
        ]
    )
    return rc, payload


def test_ac10_ci_freshness_real_workflows_edited_and_non_edited_classification(tmp_path: Path) -> None:
    ci_workflow = mod.workflow_trigger_info(yaml.safe_load((WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")), "x")
    manifest_workflow = mod.workflow_trigger_info(
        yaml.safe_load((WORKFLOW_DIR / "session-manifest.yml").read_text(encoding="utf-8")), "x"
    )
    assert ci_workflow[0] == "ci" and "edited" in ci_workflow[1]
    assert manifest_workflow[0] == "agent-session-manifest" and "edited" not in manifest_workflow[1]

    checks = [
        _ci_check("typecheck", "ci", started="2026-10-07T01:00:30Z"),
        _ci_check("python-test", "ci", started="2026-10-07T01:05:00Z"),
        # edited を含まない workflow は旧 run の pass でも body に依存しないため head_bound として受理する。
        _ci_check("validate-generated-artifact", "agent-session-manifest", started="2026-10-07T00:00:00Z"),
    ]

    rc, payload = _ci_freshness(tmp_path, checks)

    assert rc == 0, payload
    assert payload["fresh"] is True and payload["reason_codes"] == []
    assert {check["name"]: check["classification"] for check in payload["checks"]} == {
        "typecheck": "fresh",
        "python-test": "fresh",
        "validate-generated-artifact": "head_bound",
    }


def test_ac10_ci_freshness_pre_edit_pass_is_stale_pre_edit_not_fresh(tmp_path: Path) -> None:
    checks = [_ci_check("test", "ci", started="2026-10-07T00:59:59Z")]

    rc, payload = _ci_freshness(tmp_path, checks)

    assert rc == 1 and payload["fresh"] is False
    assert payload["checks"] == [{"name": "test", "classification": "stale_pre_edit"}]
    assert payload["reason_codes"] == ["stale_pre_edit"] and payload["retryable"] is True


def test_ac10_ci_freshness_same_second_as_the_watermark_is_accepted(tmp_path: Path) -> None:
    checks = [_ci_check("test", "ci", started=WATERMARK)]

    rc, payload = _ci_freshness(tmp_path, checks)

    assert rc == 0 and payload["checks"][0]["classification"] == "fresh"


def test_ac10_ci_freshness_unknown_workflow_fails_closed(tmp_path: Path) -> None:
    for workflow in ("no-such-workflow", "", None):
        checks = [_ci_check("mystery", workflow, started="2026-10-07T02:00:00Z")]  # type: ignore[arg-type]

        rc, payload = _ci_freshness(tmp_path, checks)

        assert rc == 1, workflow
        assert payload["checks"][0]["classification"] == "unknown_workflow"
        assert payload["retryable"] is False


def test_ac10_ci_freshness_one_stale_check_blocks_even_when_the_others_are_fresh(tmp_path: Path) -> None:
    checks = [
        _ci_check("lint", "ci", started="2026-10-07T02:00:00Z"),
        _ci_check("build", "ci", started="2026-10-07T00:00:00Z"),
    ]

    rc, payload = _ci_freshness(tmp_path, checks)

    assert rc == 1 and payload["reason_codes"] == ["stale_pre_edit"]


def test_ac10_ci_freshness_not_passed_wait_status_or_failed_bucket_never_fresh(tmp_path: Path) -> None:
    rc, payload = _ci_freshness(tmp_path, [_ci_check("test", "ci", started=WATERMARK)], status="pending_timeout")
    assert rc == 1 and "ci_not_passed" in payload["reason_codes"]

    rc, payload = _ci_freshness(tmp_path, [_ci_check("test", "ci", started=WATERMARK, bucket="fail")], status="failed")
    assert rc == 1 and "not_passed" in payload["reason_codes"]

    rc, payload = _ci_freshness(
        tmp_path, [_ci_check("test", "ci", started=WATERMARK, completed="0001-01-01T00:00:00Z")]
    )
    assert rc == 1 and payload["checks"][0]["classification"] == "not_completed"

    rc, payload = _ci_freshness(tmp_path, [_ci_check("test", "ci", started="garbage")])
    assert rc == 1 and payload["checks"][0]["classification"] == "invalid_timestamp"


def test_ac10_ci_freshness_head_binding_is_enforced(tmp_path: Path) -> None:
    checks = [_ci_check("test", "ci", started=WATERMARK)]
    wait_file = tmp_path / "wait.txt"
    wait_file.write_text(_ci_wait_line(HEAD, checks, current_head=OTHER_HEAD), encoding="utf-8")

    rc, payload, _err = _run_cli(
        [
            "ci-freshness",
            "--wait-ci-output",
            str(wait_file),
            "--body-edit-updated-at",
            WATERMARK,
            "--workflow-dir",
            str(WORKFLOW_DIR),
        ]
    )
    assert rc == 1 and "ci_head_mismatch" in payload["reason_codes"]

    rc, payload = _ci_freshness(tmp_path, checks, extra=["--expected-head-sha", OTHER_HEAD])
    assert rc == 1 and "ci_head_mismatch" in payload["reason_codes"]


def test_ac10_ci_freshness_runtime_errors_exit_two(tmp_path: Path) -> None:
    wait_file = tmp_path / "wait.txt"
    wait_file.write_text(_ci_wait_line(HEAD, [_ci_check("test", "ci", started=WATERMARK)]), encoding="utf-8")

    rc, payload, _ = _run_cli(
        [
            "ci-freshness",
            "--wait-ci-output",
            str(wait_file),
            "--body-edit-updated-at",
            "not-a-time",
            "--workflow-dir",
            str(WORKFLOW_DIR),
        ]
    )
    assert rc == 2 and "error" in payload
    rc, payload, _ = _run_cli(
        [
            "ci-freshness",
            "--wait-ci-output",
            str(wait_file),
            "--body-edit-updated-at",
            WATERMARK,
            "--workflow-dir",
            str(tmp_path / "nope"),
        ]
    )
    assert rc == 2 and "error" in payload
    rc, payload, _ = _run_cli(
        [
            "ci-freshness",
            "--wait-ci-output",
            str(tmp_path / "nope.txt"),
            "--body-edit-updated-at",
            WATERMARK,
            "--workflow-dir",
            str(WORKFLOW_DIR),
        ]
    )
    assert rc == 2 and "error" in payload


def test_ac10_ci_freshness_workflow_without_pull_request_trigger_or_duplicate_name_is_unknown(tmp_path: Path) -> None:
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir()
    (wf_dir / "push_only.yml").write_text(
        "name: push-only\non:\n  push:\n    branches: [main]\njobs: {}\n", encoding="utf-8"
    )
    (wf_dir / "dup_a.yml").write_text(
        "name: dup\non:\n  pull_request:\n    types: [edited]\njobs: {}\n", encoding="utf-8"
    )
    (wf_dir / "dup_b.yml").write_text("name: dup\non:\n  pull_request:\njobs: {}\n", encoding="utf-8")
    (wf_dir / "broken.yml").write_text("name: [unclosed\n", encoding="utf-8")
    for workflow in ("push-only", "dup"):
        rc, payload = _ci_freshness(
            tmp_path, [_ci_check("x", workflow, started="2026-10-07T02:00:00Z")], workflow_dir=wf_dir
        )

        assert rc == 1 and payload["checks"][0]["classification"] == "unknown_workflow", workflow


def test_ac10_workflow_trigger_info_handles_the_pyyaml_boolean_on_key_and_defaults() -> None:
    document = yaml.safe_load("name: wf\non:\n  pull_request:\n    types: [opened, edited]\n")
    assert True in document and "on" not in document  # PyYAML は `on` を True として読む
    assert mod.workflow_trigger_info(document, "fb") == ("wf", frozenset({"opened", "edited"}))

    quoted = yaml.safe_load('name: wf\n"on":\n  pull_request:\n    types: [edited]\n')
    assert "on" in quoted
    assert mod.workflow_trigger_info(quoted, "fb")[1] == frozenset({"edited"})

    defaults = {
        "on: pull_request": "name: a\non: pull_request\n",
        "on: [push, pull_request]": "name: a\non: [push, pull_request]\n",
        "pull_request: null": "name: a\non:\n  pull_request:\n",
        "pull_request: no types": "name: a\non:\n  pull_request:\n    branches: [main]\n",
    }
    for label, text in defaults.items():
        assert mod.workflow_trigger_info(yaml.safe_load(text), "fb") == ("a", mod.DEFAULT_PULL_REQUEST_TYPES), label
    assert mod.workflow_trigger_info(yaml.safe_load("on:\n  push:\n"), "fallback.yml") == ("fallback.yml", None)
    assert mod.workflow_trigger_info(yaml.safe_load("name: s\non:\n  pull_request:\n    types: edited\n"), "fb")[
        1
    ] == frozenset({"edited"})
    assert mod.workflow_trigger_info(None, "fb") == ("fb", None)


def test_ac10_parse_timestamp_handles_z_offsets_and_rejects_the_zero_time() -> None:
    assert mod.parse_timestamp("2026-10-07T01:00:00Z") == mod.parse_timestamp("2026-10-07T10:00:00+09:00")
    assert mod.parse_timestamp("2026-10-07T01:00:00") == mod.parse_timestamp("2026-10-07T01:00:00Z")
    for bad in ("0001-01-01T00:00:00Z", "", None, "yesterday", 5):
        assert mod.parse_timestamp(bad) is None


# -- dispatch of the CLI entry --


def test_ac10_dry_run_and_production_entrypoints_are_separate(tmp_path: Path) -> None:
    rc, payload, _err = _run_cli(
        ["record", "--loop-state-file", str(tmp_path / "missing.json"), "--state", "dispatched"]
    )
    assert rc == 2 and "error" in payload

    completed = subprocess.run([sys.executable, str(MODULE_PATH), "plan"], capture_output=True, text=True, check=False)
    assert completed.returncode == 2 and not completed.stdout.strip()
    completed = subprocess.run([sys.executable, str(MODULE_PATH)], capture_output=True, text=True, check=False)
    assert completed.returncode == 2 and "--dry-run-fixture" in completed.stderr

    # dry-run は gh を一切実行しない: gh が PATH に無くても、fake gh が記録を残さなくても marker を出力する。
    env = PlanEnv(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--dry-run-fixture", str(INCIDENT_FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
        env=env.environment(),
    )
    assert completed.returncode == 0 and completed.stdout.splitlines()[0] == "BODY_ONLY_LANE_ELIGIBLE"
    assert env.gh_calls() == []


def test_ac10_pure_helpers_for_ci_wait_output_and_loop_state_counts() -> None:
    line = _ci_wait_line(HEAD, [_ci_check("t", "ci", started=WATERMARK)])
    payload, error = mod.parse_ci_wait_output(line)
    assert error is None and mod.required_ci_valid_for_head(payload, HEAD) is True
    assert mod.required_ci_valid_for_head(payload, OTHER_HEAD) is False
    assert mod.required_ci_valid_for_head({**payload, "status": "failed"}, HEAD) is False
    assert mod.required_ci_valid_for_head({**payload, "checks": []}, HEAD) is False
    assert mod.required_ci_valid_for_head(None, HEAD) is False
    assert mod.parse_ci_wait_output("")[1] == "ci_wait_result_line_count_invalid"
    assert mod.parse_ci_wait_output("CI_WAIT_RESULT_V1_JSON=nope")[1] == "ci_wait_result_not_json"
    assert mod.parse_ci_wait_output('CI_WAIT_RESULT_V1_JSON={"schema":"x"}')[1] == "ci_wait_result_schema_invalid"

    counts, error = mod.lane_history_counts(
        {
            "blockers_history": [
                "x",
                {"lane": "body_only_repair", "outcome": "dispatched"},
                {"lane": "body_only_repair", "outcome": "no_mutation"},
                {"lane": "other"},
            ]
        }
    )
    assert error is None and counts == {"consumed": 1, "no_mutation": 1, "dispatched": 1}
    assert mod.lane_history_counts({}) == ({"consumed": 0, "no_mutation": 0, "dispatched": 0}, None)
    assert mod.lane_history_counts({"blockers_history": 3})[1] == "blockers_history_not_list"


# -- test_count subject binding (pure) --


def _row(
    command: str,
    subject: str,
    passed: Any = 47,
    *,
    command_hash: str = "sha256:h1",
    status: str = "pass",
    exit_code: Any = 0,
) -> dict[str, Any]:
    return {
        "ac": "AC1",
        "command": command,
        "command_hash": command_hash,
        "exit_code": exit_code,
        "status": status,
        "test_count": {"subject": subject, "passed": passed},
    }


def test_ac10_subject_tokens_split_on_non_alphanumerics_and_underscore() -> None:
    assert mod.subject_tokens("test_Foo_reachability_evaluator.py") == {
        "test",
        "foo",
        "reachability",
        "evaluator",
        "py",
    }
    assert mod.subject_tokens("evaluator") <= mod.subject_tokens(".claude/x/tests/test_foo_reachability_evaluator.py")
    assert not mod.subject_tokens("evaluator") <= mod.subject_tokens("evaluatorhelper")
    assert mod.subject_tokens("evaluator_helper") == {"evaluator", "helper"}
    assert mod.subject_tokens("___ ... ") == frozenset()


def test_ac10_bind_test_count_row_requires_the_subject_in_both_command_and_subject_and_exactly_one_row() -> None:
    hashes = ["sha256:h1"]
    evaluator = _row("pytest tests/test_x_evaluator.py -q", "tests/test_x_evaluator.py")
    helper = _row("pytest tests/test_x_evaluator_helper.py -q", "tests/test_x_evaluator_helper.py")
    suite = _row("pytest tests/ -q", "tests/")

    row, error = mod.bind_test_count_row("evaluator", [suite, evaluator], hashes)
    assert (row, error) == (evaluator, None)
    # `evaluator` は `evaluator_helper` にも token として含まれるため 2 行一致 → ambiguous。
    assert mod.bind_test_count_row("evaluator", [evaluator, helper], hashes)[1] == "test_count_subject_ambiguous"
    assert mod.bind_test_count_row("evaluator helper", [evaluator, helper], hashes)[0] == helper
    assert mod.bind_test_count_row("evaluator", [suite], hashes) == (None, "test_count_subject_unbound")
    # command には含むが test_count.subject に含まない行 / その逆は束縛しない。
    assert mod.bind_test_count_row("evaluator", [_row("pytest tests/test_x_evaluator.py", "tests/")], hashes)[0] is None
    assert mod.bind_test_count_row("evaluator", [_row("pytest tests/", "tests/test_x_evaluator.py")], hashes)[0] is None


@pytest.mark.parametrize(
    "row",
    [
        _row("pytest t_evaluator.py", "t_evaluator.py", status="fail"),
        _row("pytest t_evaluator.py", "t_evaluator.py", exit_code=1),
        _row("pytest t_evaluator.py", "t_evaluator.py", exit_code=False),
        _row("pytest t_evaluator.py", "t_evaluator.py", command_hash="sha256:other"),
        _row("pytest t_evaluator.py", "t_evaluator.py", passed=-1),
        _row("pytest t_evaluator.py", "t_evaluator.py", passed="47"),
        _row("pytest t_evaluator.py", "t_evaluator.py", passed=True),
        {
            "command": "pytest t_evaluator.py",
            "command_hash": "sha256:h1",
            "status": "pass",
            "exit_code": 0,
            "notes": "47 passed",
        },
        "not-a-row",
    ],
    ids=[
        "status_fail",
        "exit_code_1",
        "exit_code_bool",
        "hash_outside_binding",
        "negative",
        "string_count",
        "bool_count",
        "notes_only",
        "non_dict",
    ],
)
def test_ac10_bind_test_count_row_ignores_rows_that_do_not_meet_every_condition(row: Any) -> None:
    assert mod.bind_test_count_row("evaluator", [row], ["sha256:h1"])[0] is None


def test_ac10_build_evidence_refs_materializes_only_current_head_bound_refs() -> None:
    report = {
        "TEST_VERDICT": {
            "schema": "TEST_VERDICT_MACHINE/v2",
            "head_sha": HEAD,
            "reviewed_head_sha": HEAD,
            "result": "PASS",
            "runtime_ac_results": [_row("pytest t_evaluator.py", "t_evaluator.py")],
        }
    }
    refs, extra = mod.build_evidence_refs(
        live_head_sha=HEAD,
        blockers=[STALE_BLOCKER, PENDING_BLOCKER],
        test_verdict_report=report,
        test_verdict_source="v.json",
        expected_command_hashes=["sha256:h1"],
        runtime_summary_text=None,
        runtime_summary_source=None,
        runtime_summary_provenance_ok=False,
    )
    assert extra == []
    assert {ref["kind"] for ref in refs} == {"test_count", "completed_status"}
    assert all(ref["head_sha"] == HEAD and ref["source"] == "v.json" for ref in refs)
    status_ref = next(ref for ref in refs if ref["kind"] == "completed_status")
    assert status_ref["value"] == f"実施済み（TEST_VERDICT_MACHINE/v2 result: PASS, head {HEAD[:7]}）"
    assert not any(word in status_ref["value"] for word in mod.PENDING_WORDS)

    refs, extra = mod.build_evidence_refs(
        live_head_sha=OTHER_HEAD,
        blockers=[STALE_BLOCKER],
        test_verdict_report=report,
        test_verdict_source="v.json",
        expected_command_hashes=["sha256:h1"],
        runtime_summary_text="x\n",
        runtime_summary_source="s.md",
        runtime_summary_provenance_ok=True,
    )
    assert extra == ["test_verdict_head_mismatch"]
    assert [ref["kind"] for ref in refs] == ["runtime_evidence"]
    assert mod.build_evidence_refs(
        live_head_sha=HEAD,
        blockers=[],
        test_verdict_report="garbage",
        test_verdict_source="v.json",
        expected_command_hashes=[],
        runtime_summary_text=None,
        runtime_summary_source=None,
        runtime_summary_provenance_ok=False,
    ) == ([], ["test_verdict_malformed"])


# --- AC11 (test_count carrier is ignored by the existing adapter) -------------------------


def test_ac11_existing_adapter_accepts_a_report_that_carries_the_optional_test_count_field() -> None:
    adjudicator = harness.mod
    plain = harness._test_verdict(harness.HEAD_A, harness.BODY_A, False)
    carrying = copy.deepcopy(plain)
    for row in carrying["runtime_ac_results"]:
        row["test_count"] = {"subject": "tests/test_x.py", "passed": 12}

    converted_plain, errors_plain = adjudicator.adapt_test_verdict_to_current_vc_result(plain)
    converted_carrying, errors_carrying = adjudicator.adapt_test_verdict_to_current_vc_result(carrying)

    assert converted_plain is not None and converted_carrying is not None
    assert errors_plain == errors_carrying == []
    assert converted_carrying == converted_plain  # 行の既知 field の判定が不変（未知 field は無視）


def test_ac11_real_step4_adjudicate_cli_accepts_the_test_count_carrier_and_dispatches(tmp_path: Path) -> None:
    ws = harness.Workspace(tmp_path)
    verdict = harness._test_verdict(harness.HEAD_A, harness.BODY_A, False)
    for row in verdict["runtime_ac_results"]:
        row["test_count"] = {"subject": "tests/test_x.py", "passed": 12}

    rc, payload = ws.adjudicate(verdict=verdict)

    assert rc == 0, payload
    assert payload["invoke_pr_reviewer"] is True and payload["seq"] == 1


def test_ac11_plan_reads_the_raw_report_row_because_the_adapter_discards_test_count() -> None:
    adjudicator = harness.mod
    report = harness._test_verdict(harness.HEAD_A, harness.BODY_A, False)
    report["runtime_ac_results"][0]["test_count"] = {"subject": "tests/test_x_evaluator.py", "passed": 12}
    report["runtime_ac_results"][0]["command"] = "pytest tests/test_x_evaluator.py"

    converted, _errors = adjudicator.adapt_test_verdict_to_current_vc_result(report)
    assert "test_count" not in json.dumps(converted)

    row, error = mod.bind_test_count_row("evaluator", report["runtime_ac_results"], [harness.H_AC1])
    assert error is None and row["test_count"]["passed"] == 12


# --- meta: every AC prefix has at least one test (no vacuous AC) ---------------------------

REQUIRED_AC_PREFIXES = (
    "test_ac1_",
    "test_ac2_",
    "test_ac3_",
    "test_ac4_",
    "test_ac5_",
    "test_ac6_",
    "test_ac7_",
    "test_ac10_",
    "test_ac11_",
)


def test_ac7_meta_every_required_ac_prefix_has_at_least_one_test() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]

    missing = [prefix for prefix in REQUIRED_AC_PREFIXES if not any(name.startswith(prefix) for name in names)]

    assert not missing, f"AC prefixes without a test: {missing}"
    # 全ての test は接頭辞で対応 AC を示す（接頭辞の無い test を置かない）。
    unprefixed = [name for name in names if not any(name.startswith(prefix) for prefix in REQUIRED_AC_PREFIXES)]
    assert not unprefixed, unprefixed
