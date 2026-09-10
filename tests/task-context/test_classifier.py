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
