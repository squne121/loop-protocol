"""Deterministic UserPromptSubmit primary-target classifier (Issue #2564
In Scope). No DB, no subprocess -- pure text-processing unit tests."""

from __future__ import annotations

import re

import classifier
import pytest


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


def test_japanese_marker_after_comma_demotes_only_marker_reference():
    # (a) `、` is not a clause delimiter, but the marker binds only its own segment.
    comma = classifier.classify("Issue #2827 を対象にレビューして、関連資料: #2826", current_repo=REPO)
    assert comma.kind == classifier.KIND_INFERRED
    assert comma.target.ref_number == 2827
    assert comma.targets == (comma.target,)
    # ASCII comma behaves the same way.
    ascii_comma = classifier.classify("Issue #2827 を対象にレビューして, 関連資料: #2826", current_repo=REPO)
    assert ascii_comma.kind == classifier.KIND_INFERRED and ascii_comma.target.ref_number == 2827
    # (b) clause delimiter `。` (existing behavior).
    period = classifier.classify("Issue #2827 を対象にレビューして。関連資料: #2826", current_repo=REPO)
    assert period.kind == classifier.KIND_INFERRED and period.target.ref_number == 2827
    # (c) marker-first reference stays REFERENCE_ONLY (#2826 is the only reference).
    marker_first = classifier.classify("参考: #2826。現在の作業を続けて", current_repo=REPO)
    assert marker_first.kind == classifier.KIND_REFERENCE_ONLY
    assert marker_first.target.ref_number == 2826
    # Issue #2850 (OWNER decision, Option 1): marker-first bounded reference list. Every
    # reference-led segment right after the marker is a list element, so BOTH references are
    # recognized and neither is a primary candidate (REFERENCE_ONLY).
    marker_before = classifier.classify("関連資料: #2826、Issue #2827 を対象にレビューして", current_repo=REPO)
    assert marker_before.kind == classifier.KIND_REFERENCE_ONLY
    assert sorted(t.ref_number for t in marker_before.targets) == [2826, 2827]
    # (e) two actual primaries remain AMBIGUOUS; the marker reference (#12) is not among them.
    ambiguous = classifier.classify(
        "Issue #10 と Issue #11 を対象にレビューして、関連資料: #12", current_repo=REPO
    )
    assert ambiguous.kind == classifier.KIND_AMBIGUOUS
    assert sorted(t.ref_number for t in ambiguous.targets) == [10, 11]
    # A marker and its reference in the same segment still demote (no over-split).
    same_segment = classifier.classify("Issue #3 を参考に実装して、テストも足して", current_repo=REPO)
    assert same_segment.kind == classifier.KIND_REFERENCE_ONLY
    # The generic clause semantics are unchanged: `、` does not end a clause.
    assert classifier._clause_spans("A、B。C") == [(0, 4), (4, 5)]


def test_japanese_marker_after_comma_keeps_active_projection_primary():
    # (a) ACTIVE projection keeps #2827 eligible across `、` (target phrase pairing is clause-generic).
    projection = _projection("Issue #2827 を対象にレビューして、関連資料: #2826")
    assert projection["active_rebind_primary_eligible"] is True
    assert projection["active_rebind_target_ref_number"] == 2827
    # Target phrase pairing stays clause-generic: `、` must not split the clause
    # (a `、` clause delimiter would drop the target phrase from #2827's clause).
    comma_phrase = _projection("Issue #2827 の不具合を、対象にレビューして、関連資料: #2826")
    assert comma_phrase["active_rebind_primary_eligible"] is True
    assert comma_phrase["active_rebind_target_ref_number"] == 2827
    # (d) a reference demoted by the marker stays ineligible.
    assert not _eligible("Issue #3 を参考に実装して")
    assert not _eligible("参考: #2826。現在の作業を続けて")


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


# ---------------------------------------------------------------------------
# Issue #2850 (absorbs #2855): ASCII number boundary, `Issue#12` closed-prefix
# adjacency, and the marker-bound reference list. The two fixes interact (fixing
# only the boundary turns `参考: #10、#11を実装して` into a wrong primary #11), so
# the combinations are pinned together. Pure text tests, no DB / subprocess.
# ---------------------------------------------------------------------------

_SHAPE_KEYS = (
    "active_rebind_primary_eligible",
    "active_rebind_target_repo",
    "active_rebind_target_ref_kind",
    "active_rebind_target_ref_number",
    "active_rebind_ref_form",
)


def _shape(prompt: str) -> tuple:
    """(kind, targets as (repo, ref_kind, ref_number), ACTIVE projection) of ``prompt``."""
    result = classifier.classify(prompt, current_repo=REPO)
    targets = tuple((t.repo, t.ref_kind, t.ref_number) for t in result.targets)
    projection = classifier.active_rebind_projection(prompt, result, current_repo=REPO)
    return result.kind, targets, tuple(projection.get(key) for key in _SHAPE_KEYS)


_CJK_ADJACENT_TO_SPACED = [
    # (no-space form, spaced form)
    ("Issue #12を対象にレビューして", "Issue #12 を対象にレビューして"),
    ("#12を実装して", "#12 を実装して"),
    ("PR #34をレビューして", "PR #34 をレビューして"),
    ("owner/repo#12を対象に作業開始", "owner/repo#12 を対象に作業開始"),
]


@pytest.mark.parametrize(("adjacent", "spaced"), _CJK_ADJACENT_TO_SPACED)
def test_given_cjk_char_after_reference_when_classified_then_same_as_spaced_form(adjacent, spaced):
    # classify() and active_rebind_projection() both agree with the spaced form.
    adjacent_shape = _shape(adjacent)
    assert adjacent_shape[0] != classifier.KIND_NONE, adjacent
    assert adjacent_shape == _shape(spaced), adjacent
    # Concrete anchors so a symmetric regression (both forms NONE) cannot pass.
    assert adjacent_shape[1], adjacent
    # A full URL already worked (separate regex) and stays EXPLICIT.
    url = classifier.classify("https://github.com/owner/repo/issues/12を対象にレビューして", current_repo=REPO)
    assert url.kind == classifier.KIND_EXPLICIT
    assert (url.target.repo, url.target.ref_kind, url.target.ref_number) == ("owner/repo", "issue", 12)


@pytest.mark.parametrize(
    "prompt",
    [
        "#12abc",
        "Issue #12abc を実装して",
        "abc#12",
        "xIssue#12",
        "xPR#34 をレビューして",
        "Issue #12_x を実装して",
        "`Issue #12を実装して`",
        "```\nIssue #12を実装して\n```",
        "> Issue #12を実装して",
    ],
)
def test_given_adjacent_alnum_or_unchanged_semantics_when_classified_then_boundaries_kept(prompt):
    # ASCII word characters after the digits / before the prefix, and code / quote
    # text, are still not references.
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_NONE, prompt
    assert result.target is None, prompt
    assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt
    # Genuinely separate primaries stay AMBIGUOUS exactly like the spaced form.
    for adjacent, spaced in (
        ("Issue #12とIssue #34を対象にレビューして", "Issue #12 と Issue #34 を対象にレビューして"),
        ("#12を、#34を実装して", "#12 を、#34 を実装して"),
    ):
        assert classifier.classify(adjacent, current_repo=REPO).kind == classifier.KIND_AMBIGUOUS, adjacent
        assert classifier.classify(spaced, current_repo=REPO).kind == classifier.KIND_AMBIGUOUS, spaced
        assert not _eligible(adjacent), adjacent


@pytest.mark.parametrize(
    ("prompt", "ref_kind"),
    [
        ("Issue#12", "issue"),
        ("PR#34", "pr"),
        ("イシュー#12", "issue"),
        ("プルリク#34", "pr"),
        ("プルリクエスト#34", "pr"),
        ("pull request#34", "pr"),
        ("Issue#12を対象にレビューして", "issue"),
        ("PR#34をレビューして", "pr"),
    ],
)
def test_given_closed_prefix_without_space_before_hash_when_classified_then_recognized(prompt, ref_kind):
    # Closed-prefix policy (OWNER approved): only issue / pr / pull request and the
    # Japanese counterparts may touch the `#`; the prefix is not glued to a word.
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_INFERRED, prompt
    assert result.target.ref_kind == ref_kind, prompt
    assert result.target.repo == REPO
    assert result.target.ref_number in (12, 34)
    assert result.target.explicit_repo is False
    assert _projection(prompt)["active_rebind_ref_form"] == "prefixed", prompt
    # `needs_current_repo_resolution` uses the same recognition (REPO is substituted by the caller).
    assert classifier.needs_current_repo_resolution(prompt), prompt
    # Words outside the closed set are not prefixes.
    for outside in ("bug#12", "ticket#12", "Issues#12", "xIssue#12", "abc#12", "my_pr#34"):
        assert classifier.classify(outside, current_repo=REPO).kind == classifier.KIND_NONE, outside


@pytest.mark.parametrize(
    ("prompt", "repo", "ref_number"),
    [
        ("owner/repo#12", "owner/repo", 12),
        ("owner/my-issue#12", "owner/my-issue", 12),
        ("owner/my-pr#34", "owner/my-pr", 34),
        ("owner/fix.issue#12", "owner/fix.issue", 12),
        ("PR owner/my-pr#34 をレビューして", "owner/my-pr", 34),
    ],
)
def test_given_prefix_word_inside_owner_repo_ref_when_classified_then_single_explicit_reference(
    prompt, repo, ref_number
):
    # A prefix word inside an already recognised `owner/repo#N` span is part of the
    # repo name, not a second current-repo reference (no AMBIGUOUS / no resolution).
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_EXPLICIT, prompt
    assert [(t.repo, t.ref_number) for t in result.targets] == [(repo, ref_number)], prompt
    assert _eligible(prompt), prompt
    assert not classifier.needs_current_repo_resolution(prompt), prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "Issue #１２abc を実装して",
        "Issue #12３abc を実装して",
        "#１２abc",
        "owner/repo#１２abc",
        "Issue#１２abc",
        "İssue#12 を実装して",
        "ıssue#12 を実装して",
    ],
)
def test_given_unicode_digit_suffix_or_non_ascii_prefix_when_classified_then_not_a_reference(prompt):
    # The trailing boundary must not backtrack into a Unicode digit run (`#１２abc`
    # -> `#１`), and the English prefix words are ASCII-only (no `İssue` -> PR).
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_NONE, prompt
    assert result.targets == (), prompt
    assert not classifier.needs_current_repo_resolution(prompt), prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "参考: #10、#11を実装して",
        "参考: #10, #11を実装して",
        "参考: #10、#11 を実装して",
        "参考: #10、Issue #11 を実装して",
        "関連資料: #10、#11",
    ],
)
def test_given_marker_followed_by_reference_list_when_classified_then_reference_only_not_eligible(prompt):
    result = classifier.classify(prompt, current_repo=REPO)
    # Every reference is recognized (a NONE result that merely misses #11 would not pass) ...
    assert result.kind == classifier.KIND_REFERENCE_ONLY, prompt
    assert sorted(t.ref_number for t in result.targets) == [10, 11], prompt
    # ... and none of them is an ACTIVE rebind primary.
    assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt


@pytest.mark.parametrize(
    ("prompt", "primary"),
    [
        ("Issue #2827を対象にレビューして、関連資料: #2826、#2829", 2827),
        ("Issue #2827 を対象にレビューして、関連資料: #2826、#2829", 2827),
        ("Issue #12を対象にレビューして、関連資料: #10、#11", 12),
    ],
)
def test_given_primary_then_marker_reference_list_when_classified_then_primary_kept(prompt, primary):
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_INFERRED, prompt
    assert (result.target.repo, result.target.ref_kind, result.target.ref_number) == (REPO, "issue", primary)
    assert result.targets == (result.target,), prompt
    projection = _projection(prompt)
    assert projection["active_rebind_primary_eligible"] is True, prompt
    assert projection["active_rebind_target_repo"] == REPO
    assert projection["active_rebind_target_ref_kind"] == "issue"
    assert projection["active_rebind_target_ref_number"] == primary


@pytest.mark.parametrize(
    "prompt",
    [
        "参考: #10、#11。Issue #12を実装して",
        "参考: #10、#11\nIssue #12を実装して",
        "参考: #10、#11. Issue #12を実装して",
        "参考: #10、#11？Issue #12を実装して",
        # A segment whose first token is not a reference ends the list.
        "参考: #10、それとは別に Issue #12を実装して",
    ],
)
def test_given_marker_list_then_sentence_boundary_when_classified_then_next_primary_kept(prompt):
    result = classifier.classify(prompt, current_repo=REPO)
    assert result.kind == classifier.KIND_INFERRED, prompt
    assert result.target.ref_number == 12, prompt
    assert result.targets == (result.target,), prompt
    projection = _projection(prompt)
    assert projection["active_rebind_primary_eligible"] is True, prompt
    assert projection["active_rebind_target_ref_number"] == 12
    # The marker list references (#10 / #11) never become the primary.
    assert all(t.ref_number not in (10, 11) for t in result.targets), prompt


def test_given_pre_fix_behavior_restored_when_classified_then_regression_cases_fail(monkeypatch):
    """Negative control (AC7): restoring the pre-fix module-level regexes / helper
    makes representative AC1 / AC4 / AC5 cases fail. No production switch exists;
    the pre-fix behavior is injected by monkeypatching the module attributes."""

    def satisfies_ac1() -> bool:
        return _shape("Issue #12を対象にレビューして") == _shape("Issue #12 を対象にレビューして") and (
            classifier.classify("#12を実装して", current_repo=REPO).kind == classifier.KIND_INFERRED
        )

    def satisfies_ac4() -> bool:
        result = classifier.classify("参考: #10、#11を実装して", current_repo=REPO)
        return (
            result.kind == classifier.KIND_REFERENCE_ONLY
            and sorted(t.ref_number for t in result.targets) == [10, 11]
            and not _eligible("参考: #10、#11を実装して")
        )

    def satisfies_ac4_spaced() -> bool:
        # Boundary already fine in the spaced form: only the marker list rule matters here.
        return not _eligible("参考: #10、#11 を実装して")

    def satisfies_ac5() -> bool:
        result = classifier.classify("Issue #2827を対象にレビューして、関連資料: #2826、#2829", current_repo=REPO)
        return result.kind == classifier.KIND_INFERRED and result.target.ref_number == 2827

    def satisfies_ac5_spaced() -> bool:
        result = classifier.classify("Issue #2827 を対象にレビューして、関連資料: #2826、#2829", current_repo=REPO)
        return result.kind == classifier.KIND_INFERRED and result.target.ref_number == 2827

    def satisfies_ac3() -> bool:
        return classifier.classify("Issue#12", current_repo=REPO).kind == classifier.KIND_INFERRED

    checks = {
        "ac1": satisfies_ac1,
        "ac3": satisfies_ac3,
        "ac4": satisfies_ac4,
        "ac4_spaced": satisfies_ac4_spaced,
        "ac5": satisfies_ac5,
        "ac5_spaced": satisfies_ac5_spaced,
    }
    # Post-fix: every representative case holds.
    assert {name: check() for name, check in checks.items()} == dict.fromkeys(checks, True)

    # Pre-fix number boundary (Unicode `\b`), no `Issue#12` adjacency recognition.
    monkeypatch.setattr(classifier, "_OWNER_REPO_HASH_RE", re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b"))
    monkeypatch.setattr(classifier, "_BARE_HASH_RE", re.compile(r"(?<![\w/])#(\d+)\b"))
    monkeypatch.setattr(classifier, "_ADJACENT_PREFIX_HASH_RE", re.compile(r"(?!x)x"))
    # Pre-fix marker binding: only the marker's own `、` / `,` segment is demoted.
    post_fix_primary_occurrences = classifier._primary_occurrences

    def pre_fix_primary_occurrences(authority_text, occurrences):
        marker_segments = []
        for clause in classifier._clause_spans(authority_text):
            if not any(m in authority_text[clause[0] : clause[1]] for m in classifier._JA_REFERENCE_ONLY_MARKERS):
                continue
            for segment in classifier._marker_segment_spans(authority_text, clause):
                if any(m in authority_text[segment[0] : segment[1]] for m in classifier._JA_REFERENCE_ONLY_MARKERS):
                    marker_segments.append(segment)
        if not marker_segments:
            return list(occurrences)
        return [o for o in occurrences if not any(s <= o.start < e for s, e in marker_segments)]

    monkeypatch.setattr(classifier, "_primary_occurrences", pre_fix_primary_occurrences)
    assert classifier._primary_occurrences is not post_fix_primary_occurrences

    # Each representative case fails once the pre-fix behavior is restored.
    assert {name: check() for name, check in checks.items()} == dict.fromkeys(checks, False)


# ---------------------------------------------------------------------------
# Issue #2864: (W1) marker 列挙判定から二乗経路 (a) / (b) を除去、(W2) marker より
# 後ろに参照を持つ segment からのみ後続 segment へ降格を伝播、(W3) path / URL 内の
# `#N` を現在 repo の参照にしない。既存の #2850 test の assertion は変更しない。
# ---------------------------------------------------------------------------


class _CountingOccurrence:
    """``_Occurrence`` の代替。``start`` の参照回数だけを数える（計数 test 専用）。

    実装が occurrence の開始位置を何回読むかは、除去対象の二乗経路 (a)
    （segment ごとに全 occurrence を走査）と (b)（occurrence ごとに全 marker
    segment を走査）で ``start`` の読み出しが入力サイズの二乗に増えることを使って
    検出する。実時間ではなく読み出し回数なので環境に依存せず決定論的である。"""

    def __init__(self, occurrence, counter):
        self.target = occurrence.target
        self.end = occurrence.end
        self.form = occurrence.form
        self._start = occurrence.start
        self._counter = counter

    @property
    def start(self):
        self._counter[0] += 1
        return self._start


def _start_reads(prompt: str) -> int:
    """``_primary_occurrences`` が ``prompt`` の occurrence の ``start`` を読んだ回数。"""
    authority_text = classifier._strip_authority_exclusions(prompt)
    occurrences = classifier._find_occurrences(authority_text, REPO)
    assert occurrences, prompt
    counter = [0]
    counted = [_CountingOccurrence(o, counter) for o in occurrences]
    counter[0] = 0  # 構築時の読み出しは数えない
    classifier._primary_occurrences(authority_text, counted)
    return counter[0]


_PATHOLOGICAL_MARKER_LISTS = (
    # (b) だけを通る: 各 segment 自身が marker を含むので (a) の経路には入らない。
    ("参考#1、", lambda n: "参考#1、" * n),
    # (a) と (b) の両方を通る: W2 修正後も marker segment 内の `#1` が marker より後ろにあるため、
    # 列挙が後続 segment へ伝播して (a) の segment 判定に入る。
    ("参考: #1、#1...", lambda n: "参考: #1" + "、#1" * n),
)


@pytest.mark.parametrize(("label", "build"), _PATHOLOGICAL_MARKER_LISTS, ids=[x[0] for x in _PATHOLOGICAL_MARKER_LISTS])
def test_given_pathological_marker_list_when_classified_then_not_quadratic(label, build):
    # 入力サイズを 2 倍にしたとき ``start`` 参照回数が 3 倍以下であること（二乗なら約 4 倍）。
    # 実装は ``bisect`` ベースで O(n log n)（線形ではない）。参照回数は occurrence あたり定数回なので
    # 比は約 2 になる。
    sizes = (1000, 2000, 4000)
    reads = [_start_reads(build(n)) for n in sizes]
    # 非 vacuous: 少なくとも occurrence ごとに 1 回は読まれており、サイズに応じて増える。
    assert reads[0] >= sizes[0], (label, reads)
    for smaller, larger in zip(reads, reads[1:], strict=False):
        assert larger <= 3 * smaller, (label, reads)
    # 入力を実際に分類しても結果は崩れていない（全 reference が marker に束縛された REFERENCE_ONLY）。
    result = classifier.classify(build(50), current_repo=REPO)
    assert result.kind == classifier.KIND_REFERENCE_ONLY, label
    assert not _eligible(build(50)), label


def _reference_primary_occurrences(authority_text, occurrences):
    """W2 / W1 の新意味論を素朴（全 occurrence 走査・二乗）に書いた reference implementation。

    旧実装のコピーではなく、規則を文字通りに実装している:
      * clause を `、` / `,` で segment に分ける
      * marker（参考 / 関連資料）を含む segment 内の参照は marker の前後を問わず降格する
      * 降格が後続 segment へ伝播するのは、marker segment 内に marker より後ろから始まる
        参照がある場合だけ。伝播は「先頭 token が参照（任意で Issue / PR 等の prefix 語付き）」の
        segment で続き、それ以外の segment で止まる
      * occurrence の返却順は変えない
    """
    markers = ("参考", "関連資料")
    lead_word = re.compile(r"(?:(?:issue|pr|pull request|イシュー|プルリクエスト|プルリク)\s*)?", re.IGNORECASE)
    demoted: set[int] = set()
    for clause_start, clause_end in classifier._clause_spans(authority_text):
        segments = []
        segment_start = clause_start
        for index in range(clause_start, clause_end):
            if authority_text[index] in "、,":
                segments.append((segment_start, index + 1))
                segment_start = index + 1
        if segment_start < clause_end:
            segments.append((segment_start, clause_end))
        in_list = False
        for seg_start, seg_end in segments:
            seg = authority_text[seg_start:seg_end]
            marker_ends = [seg_start + seg.find(m) + len(m) for m in markers if m in seg]
            inside = [i for i, o in enumerate(occurrences) if seg_start <= o.start < seg_end]
            if marker_ends:
                demoted.update(inside)
                marker_end = min(marker_ends)
                in_list = any(occurrences[i].start >= marker_end for i in inside)
                continue
            first = seg_start + (len(seg) - len(seg.lstrip()))
            leads_with_reference = any(
                first <= occurrences[i].start and lead_word.fullmatch(authority_text[first : occurrences[i].start])
                for i in inside
            )
            if in_list and leads_with_reference:
                demoted.update(inside)
            else:
                in_list = False
    return [o for i, o in enumerate(occurrences) if i not in demoted]


def _reference_bare_hash_starts(authority_text):
    """W3 の「path-embedded な `#N` は current repo の bare 参照にしない」規則を素朴に書いた reference。

    production の ``_path_token_spans`` / ``bisect`` は使わず、1 文字ずつ次の規則を文字通り判定する:
      * token は空白・日本語/ASCII の文末記号・括弧で切れる
      * 認識済みの ASCII ``owner/repo#N`` と GitHub URL の範囲は path context を作らず、
        その範囲の終端で path context は終わる
      * `#N` の前に、同じ token 内かつ「直前の認識済み範囲の終端より後」に `/` があれば path-embedded
    返すのは、bare 参照として残る `#N` の開始位置（直前が ASCII word 文字 / `/`、直後が ASCII word 文字は対象外）。
    """
    delimiters = set(" \t\r\n\f\v\u3000、。，．！？,;；「」『』（）()")
    recognised = [
        m.span()
        for m in re.finditer(
            r"(?<![A-Za-z0-9_])(?=[A-Za-z0-9_])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+(?![\dA-Za-z_])", authority_text
        )
    ] + [
        m.span()
        for m in re.finditer(r"https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+", authority_text, re.I)
    ]
    survivors = []
    for m in re.finditer(r"(?<![A-Za-z0-9_/])#\d+(?![\dA-Za-z_])", authority_text):
        hash_at = m.start()
        # 直前の認識済み範囲の終端（path context の起点の下限）。
        floor = max([end for _, end in recognised if end <= hash_at], default=0)
        index = hash_at - 1
        path_embedded = False
        while index >= floor and authority_text[index] not in delimiters:
            if authority_text[index] == "/" and not any(a <= index < b for a, b in recognised):
                path_embedded = True
            index -= 1
        if not path_embedded:
            survivors.append(hash_at)
    return survivors


_MARKER_LIST_CORPUS = [
    # #2850 AC4: marker 直後の reference list
    "参考: #10、#11を実装して",
    "参考: #10, #11を実装して",
    "参考: #10、#11 を実装して",
    "参考: #10、Issue #11 を実装して",
    "関連資料: #10、#11",
    # #2850 AC5: marker 前の primary 維持
    "Issue #2827を対象にレビューして、関連資料: #2826、#2829",
    "Issue #2827 を対象にレビューして、関連資料: #2826、#2829",
    "Issue #12を対象にレビューして、関連資料: #10、#11",
    # #2850 AC6: sentence boundary / list 終端
    "参考: #10、#11。Issue #12を実装して",
    "参考: #10、#11\nIssue #12を実装して",
    "参考: #10、#11. Issue #12を実装して",
    "参考: #10、#11？Issue #12を実装して",
    "参考: #10、それとは別に Issue #12を実装して",
    "関連資料: #2826、Issue #2827 を対象にレビューして",
    # #2827: marker 自身の segment
    "Issue #3 を参考に実装して",
    "参考#1、#2",
    # marker 複数 / 別 clause の marker
    "参考: #1、#2。関連資料: #3、#4",
    "参考に #1、参考: #2、#3を実装して",
    "参考 #1、関連資料 #2、#3、それとは別に #4",
    # marker 無し / occurrence 無し / marker のみ
    "#12を実装して",
    "Issue #1 と Issue #2 を実装して",
    "参考にして実装して",
    "",
    # occurrence が開始位置昇順でない入力（owner/repo は bare より先に追加される）
    "参考: #10、owner/repo#11、#12",
    "参考: owner/repo#11、#12、https://github.com/owner/repo/issues/13",
    "#5、owner/repo#6 を参考に、#7を実装して",
    # W2 の 2 入力
    "参考に、#13を実装して",
    "Issue #5を参考に、#13を実装して",
    # W3 の 4 入力
    "foo/あ#12",
    "src/ファイル#12を実装して",
    "https://x.com/あ#12",
    "https://x.com/あ#12を実装して",
    "owner/repo#12",
    # W3 fix_delta 1: 認識済み owner/repo#N / GitHub URL の後ろに日本語等で続く `#N` は path 扱いにしない
    "owner/repo#12と#13を実装して",
    "https://github.com/o/r/issues/5と#13を実装して",
    "owner/repo#12→#13を実装して",
    "owner/repo#12:#13",
    "owner/repo#12を参考に#13を実装して",
    "#13とowner/repo#12を実装して",
    "src/あ#12とowner/repo#5",
]


def _run_pipeline(prompt: str):
    result = classifier.classify(prompt, current_repo=REPO)
    projection = classifier.active_rebind_projection(prompt, result, current_repo=REPO)
    return result, projection


def test_given_marker_list_inputs_when_classified_then_same_as_reference_implementation(monkeypatch):
    production = classifier._primary_occurrences
    actual = {prompt: _run_pipeline(prompt) for prompt in _MARKER_LIST_CORPUS}

    # occurrence の返却順を含め、``_primary_occurrences`` 自体が reference implementation と一致する。
    for prompt in _MARKER_LIST_CORPUS:
        authority_text = classifier._strip_authority_exclusions(prompt)
        occurrences = classifier._find_occurrences(authority_text, REPO)
        assert production(authority_text, occurrences) == _reference_primary_occurrences(authority_text, occurrences), (
            prompt
        )

    # 分類結果（kind / target / targets の順序 / projection）も、``_primary_occurrences`` だけを
    # reference implementation に差し替えた同一 pipeline の結果と一致する。
    monkeypatch.setattr(classifier, "_primary_occurrences", _reference_primary_occurrences)
    assert classifier._primary_occurrences is not production
    for prompt in _MARKER_LIST_CORPUS:
        assert _run_pipeline(prompt) == actual[prompt], prompt
    monkeypatch.undo()

    # W3: path-embedded 判定も reference implementation（素朴な 1 文字走査）と一致する。bare 形の occurrence の
    # 開始位置集合（`#` から始まる bare / Issue 等の prefix 語付き形）が、reference の残す `#N` と一致する。
    for prompt in _MARKER_LIST_CORPUS:
        authority_text = classifier._strip_authority_exclusions(prompt)
        production_bare = sorted(
            o.start
            for o in classifier._find_occurrences(authority_text, REPO)
            if o.form != classifier.REF_FORM_EXPLICIT and authority_text[o.start] == "#"
        )
        assert production_bare == _reference_bare_hash_starts(authority_text), prompt

    # 非 vacuous: corpus は REFERENCE_ONLY / INFERRED / EXPLICIT / AMBIGUOUS / NONE を全て含み、
    # 開始位置が昇順でない occurrence 列を実際に含む。
    assert {result.kind for result, _ in actual.values()} >= {
        classifier.KIND_REFERENCE_ONLY,
        classifier.KIND_INFERRED,
        classifier.KIND_EXPLICIT,
        classifier.KIND_AMBIGUOUS,
        classifier.KIND_NONE,
    }
    unordered = classifier._find_occurrences("参考: #10、owner/repo#11、#12", REPO)
    assert [o.start for o in unordered] != sorted(o.start for o in unordered)
    # 順序は変更されない: target / targets は occurrence の追加順（owner/repo が bare より先）。
    result, _ = actual["参考: #10、owner/repo#11、#12"]
    assert [t.ref_number for t in result.targets] == [11, 10, 12]
    # W2 / W3 の新期待値が reference implementation 側でも成立している（旧実装との差分を拾う入力）。
    assert actual["参考に、#13を実装して"][0].kind == classifier.KIND_INFERRED
    assert actual["Issue #5を参考に、#13を実装して"][0].kind == classifier.KIND_INFERRED
    for prompt in ("foo/あ#12", "src/ファイル#12を実装して", "https://x.com/あ#12", "https://x.com/あ#12を実装して"):
        assert actual[prompt][0].kind == classifier.KIND_NONE, prompt


_W2_NEXT_PRIMARY_KEPT = [
    "参考に、#13を実装して",
    "Issue #5を参考に、#13を実装して",
    "参考に、Issue #13を実装して",
    "参考にしつつ、PR #13 を対象にレビューして",
]
_W2_MARKER_LIST_STILL_REFERENCE_ONLY = [
    "参考: #10、#11を実装して",
    "参考#1、#2",
    "関連資料: #2826、Issue #2827 を対象にレビューして",
    # marker 自身の segment 内の参照は marker の前後を問わず降格される（伝播開始条件とは別）。
    "Issue #3 を参考に実装して",
    "参考 Issue #3 を実装して",
]


def test_given_marker_without_reference_after_it_when_classified_then_next_primary_kept():
    for prompt in _W2_NEXT_PRIMARY_KEPT:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == classifier.KIND_INFERRED, prompt
        # marker の前の参照（#5）は参考先として降格され、依頼対象 #13 だけが primary。
        assert [t.ref_number for t in result.targets] == [13], prompt
        assert result.target == result.targets[0] and result.target.repo == REPO, prompt
        projection = _projection(prompt)
        assert projection["active_rebind_primary_eligible"] is True, prompt
        assert projection["active_rebind_target_repo"] == REPO, prompt
        assert projection["active_rebind_target_ref_number"] == 13, prompt
    # 伝播開始条件を狭めても、marker 直後から参照が並ぶ入力と marker 自身の segment 内の参照は降格のまま。
    for prompt in _W2_MARKER_LIST_STILL_REFERENCE_ONLY:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == classifier.KIND_REFERENCE_ONLY, prompt
        assert result.targets, prompt
        assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt


_W3_NOT_CURRENT_REPO_PRIMARY = [
    "foo/あ#12",
    "src/ファイル#12を実装して",
    "https://x.com/あ#12",
    "https://x.com/あ#12を実装して",
    "src/あIssue#12を実装して",
]
# W3 fix_delta 1: 認識済み owner/repo#N / GitHub URL の後ろに日本語等で続く `#N` は path 扱いにしない。
# main の fail-safe（AMBIGUOUS / REFERENCE_ONLY、ACTIVE rebind 不可）より悪化しないこと。
_W3_GLUED_AFTER_RECOGNISED_REF = [
    ("owner/repo#12と#13を実装して", classifier.KIND_AMBIGUOUS, ["owner/repo#12", f"{REPO}#13"]),
    ("https://github.com/o/r/issues/5と#13を実装して", classifier.KIND_AMBIGUOUS, ["o/r#5", f"{REPO}#13"]),
    ("owner/repo#12→#13を実装して", classifier.KIND_AMBIGUOUS, ["owner/repo#12", f"{REPO}#13"]),
    ("owner/repo#12:#13", classifier.KIND_AMBIGUOUS, ["owner/repo#12", f"{REPO}#13"]),
    ("owner/repo#12を参考に#13を実装して", classifier.KIND_REFERENCE_ONLY, ["owner/repo#12", f"{REPO}#13"]),
    # 対称形（#N が先）も main / head とも AMBIGUOUS のまま
    ("#13とowner/repo#12を実装して", classifier.KIND_AMBIGUOUS, ["owner/repo#12", f"{REPO}#13"]),
]
_W3_POSITIVE_CONTROLS = [
    # path / URL 判定が通常の参照を巻き込んでいない
    ("#12を実装して", classifier.KIND_INFERRED, REPO, 12),
    ("Issue #12を実装して", classifier.KIND_INFERRED, REPO, 12),
    ("owner/repo#12", classifier.KIND_EXPLICIT, "owner/repo", 12),
    ("other/repo#12を実装して", classifier.KIND_EXPLICIT, "other/repo", 12),
    # 句読点・空白は path token を切る: path の後ろの別 token にある `#13` は参照のまま
    ("src/foo.pyを直して、#13を実装して", classifier.KIND_INFERRED, REPO, 13),
    ("src/foo.py を直して #13を実装して", classifier.KIND_INFERRED, REPO, 13),
    # owner/repo の文字集合は ASCII。直前の日本語は owner に取り込まれない
    ("リポジトリother/repo#12を実装して", classifier.KIND_EXPLICIT, "other/repo", 12),
    # GitHub URL は URL 形として EXPLICIT のまま
    ("https://github.com/other/repo/issues/12を実装して", classifier.KIND_EXPLICIT, "other/repo", 12),
]


def test_given_url_or_path_embedded_hash_when_classified_then_not_current_repo_primary():
    for prompt in _W3_NOT_CURRENT_REPO_PRIMARY:
        # 現在 repo の Issue #12 を primary にせず、AMBIGUOUS にもせず、ACTIVE rebind 候補にもしない。
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == classifier.KIND_NONE, prompt
        assert result.target is None and result.targets == (), prompt
        assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt
        assert classifier.needs_current_repo_resolution(prompt) is False, prompt
        assert classifier._find_occurrences(classifier._strip_authority_exclusions(prompt), REPO) == [], prompt
    # positive control: 上の判定が全入力を NONE にする vacuous な実装でないこと。
    for prompt, kind, repo, ref_number in _W3_POSITIVE_CONTROLS:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == kind, prompt
        assert (result.target.repo, result.target.ref_number) == (repo, ref_number), prompt
        assert result.targets == (result.target,), prompt
        assert _eligible(prompt) is True, prompt
    # W3 fix_delta 1: 認識済み参照の後ろに続く `#N` を path 扱いして main より悪化させない（AC5 の VC に含める）。
    test_given_hash_glued_after_recognised_reference_when_classified_then_not_worse_than_fail_safe()


def test_given_hash_glued_after_recognised_reference_when_classified_then_not_worse_than_fail_safe():
    for prompt, kind, expected_targets in _W3_GLUED_AFTER_RECOGNISED_REF:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == kind, prompt
        # #13（current repo）が path 扱いで消えていない。
        found = {f"{t.repo}#{t.ref_number}" for t in result.targets}
        assert set(expected_targets) <= found, prompt
        assert f"{REPO}#13" in found, prompt
        # 単一 EXPLICIT target にならず、ACTIVE rebind 候補にもならない（wrong-target の防止）。
        assert result.kind != classifier.KIND_EXPLICIT, prompt
        assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt
        # `#13` は current repo を必要とするので、解決要否も同じ規則に従う。
        assert classifier.needs_current_repo_resolution(prompt) is True, prompt


# ---------------------------------------------------------------------------
# Issue #2864 (W3 fix_delta 2): ASCII `!` `?` `[` `]` は通常文の token 境界であり、
# path 抑制が文境界 / 括弧を越えて正当な current-repo `#N` を落とさない。外部 URL の
# query / fragment（`https://x.com/あ?あ#12`）は引き続き URL 内の `#N` として NONE。
# 期待値は production の delimiter 表を参照せず、リテラルで直接固定する。
# ---------------------------------------------------------------------------

# 現在 repo の `#13` 単独の通常文。ASCII `!` `?` `[` `]` の直後 / 直前でも INFERRED になる。
_W3_FIX2_SINGLE_CURRENT_REPO_13 = [
    "src/ファイルの不具合です!#13を実装して",
    "src/ファイルの不具合です?#13を実装して",
    "[src/ファイル]の修正は#13を実装して",
    "[src/foo.py]の修正は#13を実装して",
]

# 明示 owner/repo#12 と、ASCII 境界の後ろの current-repo #13 が共存する入力。
_W3_FIX2_MULTI_PROMPTS = [
    "owner/repo#12をレビュー。[src/ファイル]の修正は#13を実装して",
    "owner/repo#12をレビュー。src/ファイルの不具合です!#13を実装して",
]

# 外部 URL の query / fragment と path 内の `#N` は current repo の参照ではない。
_W3_FIX2_URL_OR_PATH_NONE = [
    "https://x.com/あ?あ#12を実装して",
    "https://x.com/a?b=1#12を実装して",
    "src/ファイル#12を実装して",
]


def test_given_ascii_sentence_or_bracket_boundary_before_hash_when_classified_then_current_repo_inferred():
    for prompt in _W3_FIX2_SINGLE_CURRENT_REPO_13:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == classifier.KIND_INFERRED, prompt
        assert result.target == classifier.Target(REPO, "issue", 13), prompt
        assert [(t.repo, t.ref_kind, t.ref_number) for t in result.targets] == [(REPO, "issue", 13)], prompt
        projection = _projection(prompt)
        assert projection["active_rebind_primary_eligible"] is True, prompt
        assert projection["active_rebind_target_repo"] == REPO, prompt
        assert projection["active_rebind_target_ref_number"] == 13, prompt
        # `#13` は current repo の解決を必要とする（pre-W3 の bare `#N` と同じ）。
        assert classifier.needs_current_repo_resolution(prompt) is True, prompt


def test_given_explicit_ref_then_ascii_boundary_before_current_repo_hash_when_classified_then_ambiguous():
    current_repo = "squne121/loop-protocol"
    for prompt in _W3_FIX2_MULTI_PROMPTS:
        result = classifier.classify(prompt, current_repo=current_repo)
        assert result.kind == classifier.KIND_AMBIGUOUS, prompt
        assert {(t.repo, t.ref_number) for t in result.targets} == {
            ("owner/repo", 12),
            ("squne121/loop-protocol", 13),
        }, prompt
        # 単一 EXPLICIT (owner/repo#12) に潰れて wrong-target rebind しない。
        projection = classifier.active_rebind_projection(prompt, result, current_repo=current_repo)
        assert projection == {"active_rebind_primary_eligible": False}, prompt
        assert classifier.needs_current_repo_resolution(prompt) is True, prompt


def test_given_url_query_or_path_embedded_hash_when_classified_then_still_none():
    for prompt in _W3_FIX2_URL_OR_PATH_NONE:
        result = classifier.classify(prompt, current_repo=REPO)
        assert result.kind == classifier.KIND_NONE, prompt
        assert result.target is None and result.targets == (), prompt
        assert _projection(prompt) == {"active_rebind_primary_eligible": False}, prompt
        assert classifier.needs_current_repo_resolution(prompt) is False, prompt


def test_given_fix_delta_1_and_w2_inputs_when_classified_after_fix_delta_2_then_unchanged():
    expected_kinds = {
        "owner/repo#12と#13を実装して": classifier.KIND_AMBIGUOUS,
        "https://github.com/o/r/issues/5と#13を実装して": classifier.KIND_AMBIGUOUS,
        "owner/repo#12→#13を実装して": classifier.KIND_AMBIGUOUS,
        "owner/repo#12:#13": classifier.KIND_AMBIGUOUS,
        "owner/repo#12を参考に#13を実装して": classifier.KIND_REFERENCE_ONLY,
        "参考に、#13を実装して": classifier.KIND_INFERRED,
        "Issue #5を参考に、#13を実装して": classifier.KIND_INFERRED,
        "参考: #10、#11を実装して": classifier.KIND_REFERENCE_ONLY,
        "参考#1、#2": classifier.KIND_REFERENCE_ONLY,
        "関連資料: #2826、Issue #2827 を対象にレビューして": classifier.KIND_REFERENCE_ONLY,
    }
    current_repo = "squne121/loop-protocol"
    for prompt, kind in expected_kinds.items():
        result = classifier.classify(prompt, current_repo=current_repo)
        assert result.kind == kind, prompt
        # current repo の `#N` を含むので、解決要否は常に True。
        assert classifier.needs_current_repo_resolution(prompt) is True, prompt


# ---------------------------------------------------------------------------
# Issue #2871: active_rebind_projection() clause processing must not be
# quadratic. Deterministic work counting only -- no wall-clock thresholds and
# no call-count-only criteria (an unfixed projection calls each predicate once
# per occurrence, so only the *characters passed* / *substring volume* /
# *position-lookup work* reveal the quadratic path).
#
# Scope note: only the projection's clause processing is measured. The scanner
# front-end (``_find_occurrences`` / owner-repo prefix slicing) is stubbed with
# precomputed occurrences on purpose; its residual cost belongs to #2875.
# ---------------------------------------------------------------------------

_PERF_REPO = "o/r"
_PERF_SMALL_N = 200
_PERF_LARGE_N = 400
_PERF_MAX_RATIO = 3


class _CountingStr(str):
    """``str`` whose slices are counted, so the substring volume *requested* by
    the projection is visible regardless of CPython's full-length-slice
    special case. Integer indexing (used by ``_clause_spans``) is not a clause
    substring and is not counted."""

    counters: dict[str, int]

    def __getitem__(self, key):
        if isinstance(key, slice):
            self.counters["substring_chars"] += len(range(*key.indices(len(self))))
        return str.__getitem__(self, key)


class _CountingSpans(list):
    """``list`` whose element accesses (iteration + indexing) are counted.
    This seam counts the same thing for a linear search and for a ``bisect``
    based lookup that is built on the spans."""

    counters: dict[str, int]

    def __iter__(self):
        for item in list.__iter__(self):
            self.counters["lookup_work"] += 1
            yield item

    def __getitem__(self, key):
        self.counters["lookup_work"] += 1
        return list.__getitem__(self, key)


def _measure_projection_work(monkeypatch, prompt: str) -> dict[str, int]:
    """Run ``active_rebind_projection`` once with instrumentation and return
    the work counters. ``classify`` and the scanner run un-instrumented; the
    projection's occurrence loop is isolated with precomputed occurrences."""
    classification = classifier.classify(prompt, current_repo=_PERF_REPO)
    # The corpus must reach the projection occurrence loop (not return early).
    assert classification.kind in (classifier.KIND_EXPLICIT, classifier.KIND_INFERRED), prompt[:40]
    assert classification.target is not None and classification.target.repo

    real_strip = classifier._strip_authority_exclusions
    real_spans = classifier._clause_spans
    real_target = classifier._clause_has_target_phrase
    real_creation = classifier._clause_has_creation_or_reply_phrase

    authority_text = real_strip(prompt)
    occurrences = classifier._find_occurrences(authority_text, _PERF_REPO)
    primary_all = classifier._primary_occurrences(authority_text, occurrences)
    target_key = classifier._target_key(classification.target)
    assert [o for o in primary_all if classifier._target_key(o.target) == target_key], prompt[:40]
    unstubbed = classifier.active_rebind_projection(prompt, classification, current_repo=_PERF_REPO)

    counters = {
        "target_chars": 0,
        "target_calls": 0,
        "creation_chars": 0,
        "creation_calls": 0,
        "substring_chars": 0,
        "lookup_work": 0,
    }
    _CountingStr.counters = counters
    _CountingSpans.counters = counters

    def counting_strip(text):
        return _CountingStr(real_strip(text))

    def counting_spans(text):
        return _CountingSpans(real_spans(text))

    def counting_target(clause):
        counters["target_calls"] += 1
        counters["target_chars"] += len(clause)
        return real_target(clause)

    def counting_creation(clause):
        counters["creation_calls"] += 1
        counters["creation_chars"] += len(clause)
        return real_creation(clause)

    with monkeypatch.context() as patch:
        patch.setattr(classifier, "_strip_authority_exclusions", counting_strip)
        patch.setattr(classifier, "_find_occurrences", lambda *_a, **_k: occurrences)
        patch.setattr(classifier, "_primary_occurrences", lambda *_a, **_k: primary_all)
        patch.setattr(classifier, "_clause_spans", counting_spans)
        patch.setattr(classifier, "_clause_has_target_phrase", counting_target)
        patch.setattr(classifier, "_clause_has_creation_or_reply_phrase", counting_creation)
        measured = classifier.active_rebind_projection(prompt, classification, current_repo=_PERF_REPO)

    assert measured == unstubbed, prompt[:40]
    return counters


_PERF_CORPORA = [
    ("reference_marker_commas", lambda n: "参考: " + "、#1" * n, False),
    ("owner_repo_commas", lambda n: "o/r#1、" * n, False),
    ("owner_repo_commas_then_creation", lambda n: "o/r#1、" * n + "を対象に follow-up を起票して", True),
    ("preceding_clause_then_commas", lambda n: "説明。" + "o/r#1、" * n, False),
    ("many_short_clauses", lambda n: "Issue #1。" * n, False),
]


@pytest.mark.parametrize(
    ("build_prompt", "expects_creation"),
    [(build, creation) for _name, build, creation in _PERF_CORPORA],
    ids=[name for name, _build, _creation in _PERF_CORPORA],
)
def test_given_pathological_clause_when_active_rebind_projection_then_not_quadratic(
    monkeypatch, build_prompt, expects_creation
):
    small = _measure_projection_work(monkeypatch, build_prompt(_PERF_SMALL_N))
    large = _measure_projection_work(monkeypatch, build_prompt(_PERF_LARGE_N))

    # A count of 0 would make the ratio vacuous: every measurement that this
    # corpus is supposed to exercise must be non-zero.
    for key in ("target_calls", "target_chars", "substring_chars", "lookup_work"):
        assert small[key] > 0 and large[key] > 0, (key, small, large)
    if expects_creation:
        # The creation/reply predicate is only reached when the target phrase
        # matched; this corpus must make it run at least once.
        assert small["creation_calls"] >= 1 and large["creation_calls"] >= 1, (small, large)
        assert small["creation_chars"] > 0 and large["creation_chars"] > 0, (small, large)

    measured_keys = ["target_chars", "substring_chars", "lookup_work"]
    if expects_creation:
        measured_keys.append("creation_chars")
    for key in measured_keys:
        ratio = large[key] / small[key]
        assert ratio <= _PERF_MAX_RATIO, (
            f"{key} grew {ratio:.2f}x (> {_PERF_MAX_RATIO}x) when the input doubled "
            f"(n={_PERF_SMALL_N}->{_PERF_LARGE_N}): small={small} large={large}"
        )


def _reference_active_rebind_projection(prompt, classification, *, current_repo=None):
    """Verbatim copy of the pre-#2871 (naive) ``active_rebind_projection`` --
    the behavioural reference for the equivalence test below. It must not be
    edited to follow future changes of the production function."""
    ineligible = {"active_rebind_primary_eligible": False}
    if (
        classification.kind not in (classifier.KIND_EXPLICIT, classifier.KIND_INFERRED)
        or classification.target is None
    ):
        return ineligible
    target = classification.target
    if not target.repo:
        return ineligible

    prompt = prompt or ""
    authority_text = classifier._strip_authority_exclusions(prompt)
    occurrences = classifier._find_occurrences(authority_text, current_repo)
    primary = [
        o
        for o in classifier._primary_occurrences(authority_text, occurrences)
        if classifier._target_key(o.target) == classifier._target_key(target)
    ]
    if not primary:
        return ineligible

    ref_form = None
    if classifier._is_whole_prompt_single_explicit_reference(prompt) and len(occurrences) == 1:
        if primary[0].form in (classifier.REF_FORM_EXPLICIT, classifier.REF_FORM_PREFIXED):
            ref_form = primary[0].form
    if ref_form is None:
        spans = classifier._clause_spans(authority_text)
        for occurrence in primary:
            start, end = spans[classifier._clause_index(spans, occurrence.start)]
            clause = authority_text[start:end]
            if classifier._clause_has_target_phrase(clause) and not classifier._clause_has_creation_or_reply_phrase(
                clause
            ):
                ref_form = occurrence.form
                break
    if ref_form is None:
        return ineligible

    return {
        "active_rebind_primary_eligible": True,
        "active_rebind_target_repo": target.repo,
        "active_rebind_target_ref_kind": target.ref_kind,
        "active_rebind_target_ref_number": target.ref_number,
        "active_rebind_ref_form": ref_form,
    }


_PROJECTION_EQUIVALENCE_CORPUS = [
    # single occurrence
    "#1を実装して",
    "#1 を対象にレビューして",
    # multiple occurrences in the same clause
    "#1、#1、#1を実装して",
    "o/r#1、o/r#1を対象に作業して",
    # clause splits (target phrase in a different clause than the reference)
    "#1です。を実装して",
    "説明。#1を実装して。別件。",
    "別件です。\n#1を修正して",
    # no target phrase
    "参考: #1",
    "#1 についてどう思う?",
    "Issue #1。",
    # creation/reply phrase present
    "#1を実装して、follow-up を起票して",
    "#1に返信して",
    "o/r#1、o/r#1を対象に follow-up を起票して",
    "work on #1 and create an issue",
    # whole-prompt single explicit reference
    "o/r#1",
    "o/r#1。",
    "Issue #1",
    "https://github.com/o/r/issues/1",
    "#1",
    # empty / blank prompt
    "",
    "   ",
    # primary order is not position order (mixed forms)
    "Issue#1を実装して。#1を実装して",
    "#1を実装して。Issue#1を実装して",
    # an earlier clause is ineligible (creation/reply) but a later one is eligible
    "#1を実装して、follow-up を起票して。#1を実装して",
    "Issue #1を実装して、に返信して。#1を対象に作業して",
    "o/r#1を対象に起票して。説明。o/r#1を実装して",
    # trailing text with no delimiter / English clause
    "please implement #1. Then reply to it.",
    "please reply to #1. Then implement #1.",
]


@pytest.mark.parametrize("prompt", _PROJECTION_EQUIVALENCE_CORPUS)
def test_given_projection_corpus_when_evaluated_then_same_as_reference_implementation(prompt):
    classification = classifier.classify(prompt, current_repo=_PERF_REPO)
    expected = _reference_active_rebind_projection(prompt, classification, current_repo=_PERF_REPO)
    actual = classifier.active_rebind_projection(prompt, classification, current_repo=_PERF_REPO)
    assert actual == expected, prompt


def test_given_projection_equivalence_corpus_when_inspected_then_covers_both_outcomes_and_bare_form():
    # Guard against a vacuous corpus: it must contain eligible and ineligible
    # cases, and pin the primary-order-dependent ``bare`` form.
    outcomes = set()
    for prompt in _PROJECTION_EQUIVALENCE_CORPUS:
        classification = classifier.classify(prompt, current_repo=_PERF_REPO)
        outcomes.add(
            classifier.active_rebind_projection(prompt, classification, current_repo=_PERF_REPO)[
                "active_rebind_primary_eligible"
            ]
        )
    assert outcomes == {True, False}

    mixed = "Issue#1を実装して。#1を実装して"
    classification = classifier.classify(mixed, current_repo=_PERF_REPO)
    projection = classifier.active_rebind_projection(mixed, classification, current_repo=_PERF_REPO)
    assert projection["active_rebind_primary_eligible"] is True
    assert projection["active_rebind_ref_form"] == "bare"
    assert projection == _reference_active_rebind_projection(mixed, classification, current_repo=_PERF_REPO)

    # Earlier clause ineligible by creation/reply, later clause eligible: the
    # projection must still become eligible (a "first occurrence wins" shortcut
    # would wrongly stay ineligible).
    later = "#1を実装して、follow-up を起票して。#1を実装して"
    classification = classifier.classify(later, current_repo=_PERF_REPO)
    projection = classifier.active_rebind_projection(later, classification, current_repo=_PERF_REPO)
    assert projection["active_rebind_primary_eligible"] is True


def test_given_spans_when_clause_index_from_ends_then_same_as_linear_clause_index():
    for text in ["", "abc", "一。二。三", "一。二。三。", "a.\nb? c!", "。。。", "\n"]:
        spans = classifier._clause_spans(text)
        ends = [end for _, end in spans]
        for position in range(-2, len(text) + 3):
            assert classifier._clause_index_from_ends(ends, position) == classifier._clause_index(
                spans, position
            ), (text, position)
