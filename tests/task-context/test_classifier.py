"""Deterministic UserPromptSubmit primary-target classifier (Issue #2564
In Scope). No DB, no subprocess -- pure text-processing unit tests."""

from __future__ import annotations

import classifier


def test_given_plain_prompt_with_no_ref_when_classified_then_none():
    result = classifier.classify("please fix the failing test", current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE
    assert result.target is None


def test_given_bare_hash_ref_when_classified_then_inferred_against_current_repo():
    result = classifier.classify("implement issue #123 please", current_repo="owner/repo")
    assert result.kind == classifier.KIND_INFERRED
    assert result.target.repo == "owner/repo"
    assert result.target.ref_kind == "issue"
    assert result.target.ref_number == 123


def test_given_owner_repo_hash_ref_when_classified_then_explicit():
    result = classifier.classify("go work on other/repo#456", current_repo="owner/repo")
    assert result.kind == classifier.KIND_EXPLICIT
    assert result.target.repo == "other/repo"
    assert result.target.ref_number == 456


def test_given_full_github_issue_url_when_classified_then_explicit_issue():
    result = classifier.classify(
        "https://github.com/owner/repo/issues/789 needs work", current_repo=None
    )
    assert result.kind == classifier.KIND_EXPLICIT
    assert result.target.repo == "owner/repo"
    assert result.target.ref_kind == "issue"
    assert result.target.ref_number == 789


def test_given_full_github_pull_url_when_classified_then_explicit_pr():
    result = classifier.classify("see https://github.com/owner/repo/pull/42", current_repo=None)
    assert result.kind == classifier.KIND_EXPLICIT
    assert result.target.ref_kind == "pr"
    assert result.target.ref_number == 42


def test_given_pr_prefixed_bare_hash_when_classified_then_ref_kind_pr():
    result = classifier.classify("please review PR #55", current_repo="owner/repo")
    assert result.kind == classifier.KIND_INFERRED
    assert result.target.ref_kind == "pr"
    assert result.target.ref_number == 55


def test_given_ref_inside_fenced_code_block_when_classified_then_excluded():
    prompt = "run this:\n```\nfix #999\n```\nno other instructions"
    result = classifier.classify(prompt, current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE


def test_given_ref_inside_inline_code_when_classified_then_excluded():
    result = classifier.classify("the variable `#123` is just a comment marker", current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE


def test_given_ref_inside_blockquote_when_classified_then_excluded():
    prompt = "> old context mentioned #111\nplease just run the tests"
    result = classifier.classify(prompt, current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE


def test_given_ref_inside_double_quotes_when_classified_then_excluded():
    prompt = 'the commit message says "closes #222" as an example, just continue'
    result = classifier.classify(prompt, current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE


def test_given_reference_only_marker_when_classified_then_reference_only():
    result = classifier.classify("this is related to #321, keep working on the current task", current_repo="owner/repo")
    assert result.kind == classifier.KIND_REFERENCE_ONLY
    assert result.target.ref_number == 321


def test_given_multiple_distinct_targets_when_classified_then_ambiguous():
    result = classifier.classify("compare #10 and #20 before deciding", current_repo="owner/repo")
    assert result.kind == classifier.KIND_AMBIGUOUS
    assert len(result.targets) == 2


def test_given_repeated_same_target_when_classified_then_single_target_not_ambiguous():
    result = classifier.classify("issue #10, yes #10 exactly, work on #10", current_repo="owner/repo")
    assert result.kind == classifier.KIND_INFERRED
    assert result.target.ref_number == 10


def test_given_slash_task_prefix_when_classified_then_slash_task_kind_precedes_everything():
    result = classifier.classify("/task #55", current_repo="owner/repo")
    assert result.kind == classifier.KIND_SLASH_TASK
    assert result.slash_task_raw_target == "#55"


def test_given_slash_task_with_leading_whitespace_when_classified_then_still_detected():
    result = classifier.classify("   /task other/repo#7", current_repo="owner/repo")
    assert result.kind == classifier.KIND_SLASH_TASK
    assert result.slash_task_raw_target == "other/repo#7"


def test_given_slash_task_no_target_when_classified_then_raw_target_is_none():
    result = classifier.classify("/task", current_repo="owner/repo")
    assert result.kind == classifier.KIND_SLASH_TASK
    assert result.slash_task_raw_target is None


def test_given_slash_task_ad_hoc_label_when_classified_then_slash_task_kind():
    result = classifier.classify("/task cleanup pass", current_repo="owner/repo")
    assert result.kind == classifier.KIND_SLASH_TASK
    assert result.slash_task_raw_target == "cleanup pass"


def test_given_slash_task_when_it_also_contains_a_ref_then_precedence_is_slash_task_not_explicit():
    """AC12: precedence is `/task` special-case > normal classifier, always."""
    result = classifier.classify("/task switch to other/repo#99 now", current_repo="owner/repo")
    assert result.kind == classifier.KIND_SLASH_TASK


def test_given_none_prompt_when_classified_then_none_kind_no_crash():
    result = classifier.classify(None, current_repo="owner/repo")
    assert result.kind == classifier.KIND_NONE


# ---------------------------------------------------------------------------
# parse_slash_task_target
# ---------------------------------------------------------------------------


def test_given_slash_task_target_bare_hash_when_parsed_then_target_resolved():
    target, ad_hoc = classifier.parse_slash_task_target("#42", current_repo="owner/repo")
    assert target.repo == "owner/repo"
    assert target.ref_kind == "issue"
    assert target.ref_number == 42
    assert ad_hoc is None


def test_given_slash_task_target_owner_repo_hash_when_parsed_then_explicit_repo():
    target, ad_hoc = classifier.parse_slash_task_target("other/repo#7", current_repo="owner/repo")
    assert target.repo == "other/repo"
    assert target.ref_number == 7
    assert ad_hoc is None


def test_given_slash_task_target_url_when_parsed_then_explicit_pr():
    target, ad_hoc = classifier.parse_slash_task_target(
        "https://github.com/owner/repo/pull/9", current_repo=None
    )
    assert target.ref_kind == "pr"
    assert target.ref_number == 9
    assert ad_hoc is None


def test_given_slash_task_target_kind_word_when_parsed_then_ref_kind_set():
    target, ad_hoc = classifier.parse_slash_task_target("pr 3", current_repo="owner/repo")
    assert target.ref_kind == "pr"
    assert target.ref_number == 3


def test_given_slash_task_target_free_text_when_parsed_then_ad_hoc_title():
    target, ad_hoc = classifier.parse_slash_task_target("cleanup pass", current_repo="owner/repo")
    assert target is None
    assert ad_hoc == "cleanup pass"


def test_given_slash_task_target_empty_when_parsed_then_both_none():
    target, ad_hoc = classifier.parse_slash_task_target("", current_repo="owner/repo")
    assert target is None
    assert ad_hoc is None


# ---------------------------------------------------------------------------
# Issue #2827: Japanese reference-only markers (`参考` / `関連資料`, clause
# bound) and the ACTIVE-only rebind projection. Pure text tests.
# ---------------------------------------------------------------------------

REPO = "owner/repo"


def _projection(prompt: str) -> dict:
    return classifier.active_rebind_projection(
        prompt, classifier.classify(prompt, current_repo=REPO), current_repo=REPO
    )


def _eligible(prompt: str) -> bool:
    return _projection(prompt)["active_rebind_primary_eligible"]


def test_japanese_reference_marker_is_reference_only():
    result = classifier.classify("参考: #2826。現在の作業を続けて", current_repo=REPO)
    assert result.kind == classifier.KIND_REFERENCE_ONLY
    assert result.target.ref_number == 2826
    # `関連資料` is the other (and only other) Japanese marker.
    only_related = classifier.classify("関連資料: Issue #7 を参照", current_repo=REPO)
    assert only_related.kind == classifier.KIND_REFERENCE_ONLY
    # `参照` / `比較` are NOT markers: they appear in ordinary technical nouns.
    for prompt in ("Issue #2827 の参照カウント不具合を修正して", "Issue #2827 の比較ロジックを修正して"):
        assert classifier.classify(prompt, current_repo=REPO).kind == classifier.KIND_INFERRED
    # English markers keep their whole-prompt substring behavior.
    assert classifier.classify("see also #5 please", current_repo=REPO).kind == classifier.KIND_REFERENCE_ONLY


def test_japanese_marker_demotes_only_same_clause_reference_and_keeps_other_clause_primary():
    prompt = "Issue #2827 を対象にレビューして。関連資料: #2826"
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_INFERRED
    assert result.target.ref_number == 2827
    projection = _projection(prompt)
    assert projection["active_rebind_primary_eligible"] is True
    assert projection["active_rebind_target_ref_number"] == 2827

    # A marker in a clause WITHOUT the reference does not demote a reference elsewhere.
    other_clause = classifier.classify("参考までに背景を共有します。Issue #9 を実装して", current_repo=REPO)
    assert other_clause.kind == classifier.KIND_INFERRED and other_clause.target.ref_number == 9

    # Two primaries stay ambiguous when no marker shares a clause with either.
    assert classifier.classify("Issue #1 と Issue #2 を実装して", current_repo=REPO).kind == classifier.KIND_AMBIGUOUS
    # Marker and target phrase in the same clause: the marker (demotion) wins.
    assert classifier.classify("Issue #3 を参考に実装して", current_repo=REPO).kind == classifier.KIND_REFERENCE_ONLY


def test_creation_or_reply_object_reference_is_not_primary_for_active_rebind():
    excluded = [
        "Issue #2830 を対象に follow-up を起票して",
        "Issue #2830 に返信して、対象を確認",
        "Issue #2830 へ返信を対象にレビュー",
        "Issue #2830 にコメントして対象を確認",
        "Issue #2830 にコメントするために対象を確認",
        "Issue #2830 にコメントを投稿して対象を確認",
        "Issue #2830 の対象コメントを投稿",
        "review and reply to Issue #2830",
        "please comment on Issue #2830 and review it",
        "create an issue to fix Issue #2830",
        "file an issue and review Issue #2830",
        "review then post a comment on Issue #2830",
    ]
    for prompt in excluded:
        assert not _eligible(prompt), prompt
    # Bare creation/reply NOUNS alone are not exclusion words.
    kept = [
        "PR #2834 のレビューコメントを修正して",
        "Issue #2842 のコメント投稿処理を実装して",
        "fix the post-merge check in Issue #2830",
        "implement the follow-up handling for Issue #4",
        "Issue #5 の reply 処理を修正して",
    ]
    for prompt in kept:
        assert _eligible(prompt), prompt


def test_target_phrase_with_creation_word_in_same_clause_is_excluded_only_by_exclusion_rule(monkeypatch):
    prompt = "Issue #2830 を対象に follow-up を起票して"
    result = classifier.classify(prompt, current_repo=REPO)
    # Every OTHER rule passes: single legacy primary, closed-set prefixed form, target phrase present.
    assert result.kind == classifier.KIND_INFERRED and result.target.ref_number == 2830
    assert classifier._clause_has_target_phrase(prompt)
    assert classifier._find_occurrences(prompt, REPO)[0].form == classifier.REF_FORM_PREFIXED
    assert _eligible(prompt) is False

    # Non-vacuous: removing ONLY the exclusion vocabulary flips the outcome to eligible.
    monkeypatch.setattr(classifier, "_JA_CREATION_REPLY_PHRASES", ())
    monkeypatch.setattr(classifier, "_EN_CREATION_REPLY_RE", classifier.re.compile(r"(?!x)x"))
    assert _eligible(prompt) is True


def test_reference_and_target_phrase_in_different_clauses_never_pair():
    # The target phrase is in a different clause from the only reference.
    for prompt in (
        "Issue #2830。対象を確認して",
        "Issue #2830 はこれです。実装してください",
        "レビューして\nIssue #2830",
        "please review this.\nIssue #2830",
    ):
        assert not _eligible(prompt), prompt
    # `.` inside a URL / number is not a clause break; sentence-final `. ` is.
    assert _eligible("review https://github.com/owner/repo/issues/12")
    assert not _eligible("review the plan. https://github.com/owner/repo/issues/12 is later")
    # Two primaries in different clauses are ambiguous (never a silent rebind).
    ambiguous = "Issue #2827 をレビューして。Issue #2830 に follow-up を起票して"
    assert classifier.classify(ambiguous, current_repo=REPO).kind == classifier.KIND_AMBIGUOUS
    assert not _eligible(ambiguous)


def test_english_target_phrase_requires_word_boundary():
    for prompt in (
        "preview Issue #2830",
        "Issue #2830 reviewer notes",
        "prefix Issue #2830",
        "Issue #2830 fixed already",
        "Issue #2830 implementation",
    ):
        assert not _eligible(prompt), prompt
    for prompt in (
        "Review Issue #2830",
        "WORK ON Issue #2830",
        "please Implement Issue #2830",
        "refine Issue #2830 today",
        "Switch to Issue #2830",
        "fix Issue #2830",
    ):
        assert _eligible(prompt), prompt


def test_active_projection_reference_forms_and_whole_prompt_rule():
    assert _projection("owner/other#5 を実装して")["active_rebind_ref_form"] == "explicit"
    assert _projection("https://github.com/owner/repo/issues/8 を実装して")["active_rebind_ref_form"] == "explicit"
    assert _projection("Issue #6 を実装して")["active_rebind_ref_form"] == "prefixed"
    assert _projection("プルリクエスト #6 を実装して")["active_rebind_ref_form"] == "prefixed"
    assert _projection("イシュー #6 を実装して")["active_rebind_ref_form"] == "prefixed"
    assert _projection("#6 を実装して")["active_rebind_ref_form"] == "bare"
    # Japanese PR prefixes resolve to ref_kind pr.
    assert _projection("プルリク #12 をレビューして")["active_rebind_target_ref_kind"] == "pr"
    # The entire prompt being one explicit reference is enough; a bare `#N` alone is not.
    assert _eligible("Issue #6") and _eligible("Issue #6。") and _eligible("https://github.com/owner/repo/pull/9")
    assert not _eligible("#6") and not _eligible("Issue #6 とその周辺")
    # NONE / REFERENCE_ONLY / AMBIGUOUS / SLASH_TASK never reach eligibility.
    for prompt in ("続けて", "see also #6", "/task #6", "Issue #1 と Issue #2 を実装して"):
        assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt
    # Code / quoted / blockquote text is not authority.
    assert not _eligible("`Issue #6 を実装して`")
    assert not _eligible("> Issue #6 を実装して")
