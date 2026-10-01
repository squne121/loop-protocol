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
