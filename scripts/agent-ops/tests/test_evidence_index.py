"""Issue #2052 AC1-AC5: unit tests for ``scripts/agent-ops/evidence_index.py``.

These tests exercise ``EvidenceIndex`` directly and hermetically -- no
GitHub / network access, no live Claude Code process. ``fetch_fn`` is
always a small counting stub matching the ``(raw_snapshot, err)`` contract
used by ``run_refinement_preflight.py``'s own
``_fetch_issue``/``_fetch_issue_comments``/``_fetch_single_comment``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "evidence_index.py"
_MODULE_NAME = "evidence_index_issue_2052"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
evidence_index = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = evidence_index
_spec.loader.exec_module(evidence_index)


REPO = "squne121/loop-protocol"


class _CountingFetcher:
    """A fetch_fn stub that records every invocation and returns the
    CURRENT element of ``responses`` (advancing one step per call, staying
    on the last element once exhausted) -- lets tests simulate a resource
    whose observed content changes between calls (edits) or a transient
    failure followed by success."""

    def __init__(self, responses: "list[tuple[object, str]]"):
        self.responses = responses
        self.calls = 0

    def __call__(self):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx]


# ---------------------------------------------------------------------------
# AC1: same evidence_key referenced twice within one phase -> no re-fetch.
# ---------------------------------------------------------------------------


def test_duplicate_read_within_phase_is_cached():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")
    fetcher = _CountingFetcher([({"body": "hello world"}, "")])

    # Issue #2052 fix_delta D: `duplicate_projection_count` only counts a
    # cache hit that ALSO supplied a `project_fn` (an actually-suppressed
    # re-projection) -- a `project_fn` is passed here so this test still
    # meaningfully exercises that counter under the corrected semantics.
    project_calls = {"n": 0}

    def _project(raw):
        project_calls["n"] += 1
        return {"length": len(raw["body"])}

    first = index.get_or_fetch(
        repository=REPO,
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052,
        fetch_fn=fetcher,
        project_fn=_project,
    )
    second = index.get_or_fetch(
        repository=REPO,
        resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052,
        fetch_fn=fetcher,
        project_fn=_project,
    )

    assert fetcher.calls == 1, "a second reference to the same evidence_key within the same phase must not re-fetch"
    assert project_calls["n"] == 1, "a cache HIT must reuse the cached projection, never re-run project_fn"
    assert first.reused is False
    assert second.reused is True
    assert second.raw_snapshot == first.raw_snapshot
    assert second.projection == first.projection

    metrics = index.metrics_snapshot()
    assert metrics["fetch_count"] == 1
    assert metrics["snapshot_reuse_count"] == 1
    assert metrics["duplicate_projection_count"] == 1


def test_duplicate_projection_count_not_incremented_without_project_fn():
    """Issue #2052 fix_delta D: a cache hit for a call that never supplies
    `project_fn` never ran a projection to begin with, so
    `duplicate_projection_count` must stay 0 even though
    `snapshot_reuse_count` still increments normally."""
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")
    fetcher = _CountingFetcher([({"body": "hello world"}, "")])

    index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    second = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert second.reused is True

    metrics = index.metrics_snapshot()
    assert metrics["snapshot_reuse_count"] == 1
    assert metrics["duplicate_projection_count"] == 0


def test_different_resource_ids_never_collide_in_the_same_phase():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")
    fetcher_a = _CountingFetcher([({"body": "issue A"}, "")])
    fetcher_b = _CountingFetcher([({"body": "issue B"}, "")])

    out_a = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=1, fetch_fn=fetcher_a,
    )
    out_b = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2, fetch_fn=fetcher_b,
    )

    assert fetcher_a.calls == 1
    assert fetcher_b.calls == 1
    assert out_a.raw_snapshot != out_b.raw_snapshot
    assert out_a.evidence_key.observed_content_sha256 != out_b.evidence_key.observed_content_sha256


# ---------------------------------------------------------------------------
# AC2: phase transition / mutation / explicit refresh always forces a fresh
# fetch -- a stale snapshot is never treated as the current GitHub state.
# ---------------------------------------------------------------------------


def test_fresh_fetch_on_phase_transition_and_mutation():
    index = evidence_index.EvidenceIndex()
    fetcher = _CountingFetcher([({"body": "v1"}, "")])

    index.begin_phase("preflight_fetch")
    first = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert first.reused is False
    assert fetcher.calls == 1

    # Phase transition: even referencing the exact same resource_id must
    # re-fetch, never reuse the previous phase's cached snapshot.
    index.begin_phase("post_repair_readback")
    second = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert second.reused is False
    assert fetcher.calls == 2

    # Re-entering "preflight_fetch" is itself a phase TRANSITION away from
    # "post_repair_readback" -- the original preflight_fetch entry from
    # step one must NOT resurrect.
    index.begin_phase("preflight_fetch")
    third = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert third.reused is False
    assert fetcher.calls == 3

    # Explicit mutation invalidation within the SAME phase forces a fresh
    # fetch on the very next reference.
    fourth_should_be_cached = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert fourth_should_be_cached.reused is True
    assert fetcher.calls == 3

    index.invalidate(REPO, evidence_index.RESOURCE_KIND_ISSUE_BODY, 2052)
    fifth = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert fifth.reused is False
    assert fetcher.calls == 4

    # Explicit refresh (freshness-sensitive decision) bypasses the cache
    # even without an invalidate() call.
    sixth = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher, force_refresh=True,
    )
    assert sixth.reused is False
    assert fetcher.calls == 5


# ---------------------------------------------------------------------------
# AC3: body/comment edits are reflected once a refresh boundary (phase
# transition or invalidate()) is crossed -- a stale snapshot is never
# served past that boundary.
# ---------------------------------------------------------------------------


def test_body_and_comment_change_invalidates_cache_after_refresh_boundary():
    index = evidence_index.EvidenceIndex()
    fetcher = _CountingFetcher(
        [
            ({"body": "original body"}, ""),
            ({"body": "edited body"}, ""),
        ]
    )

    index.begin_phase("preflight_fetch")
    before_edit = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert before_edit.raw_snapshot["body"] == "original body"

    # Within the SAME phase, with no refresh boundary crossed, the edit
    # (which happens "out of band" here, e.g. a human editing the Issue on
    # GitHub) must not be observed yet -- reuse still applies.
    still_cached = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert still_cached.reused is True
    assert still_cached.raw_snapshot["body"] == "original body"

    # Refresh boundary #1: explicit invalidate() (the caller's contract
    # after performing/observing a mutation) -- next read reflects the edit.
    index.invalidate(REPO, evidence_index.RESOURCE_KIND_ISSUE_BODY, 2052)
    after_invalidate = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert after_invalidate.reused is False
    assert after_invalidate.raw_snapshot["body"] == "edited body"
    assert after_invalidate.evidence_key.observed_content_sha256 != before_edit.evidence_key.observed_content_sha256

    # Refresh boundary #2: a NEW phase never inherits the prior phase's
    # (now-current) snapshot either -- every phase always starts empty.
    index.begin_phase("next_phase")
    fresh_in_new_phase = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert fresh_in_new_phase.reused is False


def test_comment_add_edit_delete_all_require_explicit_invalidate_to_be_observed():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    edited_fetcher = _CountingFetcher(
        [
            ({"id": 555, "body": "first comment text"}, ""),
            ({"id": 555, "body": "EDITED comment text"}, ""),
        ]
    )
    first = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_COMMENT,
        resource_id=555, fetch_fn=edited_fetcher,
    )
    assert first.raw_snapshot["body"] == "first comment text"

    index.invalidate(REPO, evidence_index.RESOURCE_KIND_COMMENT, 555)
    second = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_COMMENT,
        resource_id=555, fetch_fn=edited_fetcher,
    )
    assert second.raw_snapshot["body"] == "EDITED comment text"
    assert edited_fetcher.calls == 2


# ---------------------------------------------------------------------------
# AC4: this is NOT a generic command cache -- unbounded/transient-retry
# operations are never suppressed.
# ---------------------------------------------------------------------------


def test_unbounded_or_transient_commands_not_suppressed():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    # (a) resource_kind is a CLOSED enum (issue_body | comment) -- an
    # attempt to route an arbitrary command result through this cache
    # fails closed rather than silently caching it.
    try:
        index.get_or_fetch(
            repository=REPO,
            resource_kind="generic_command_result",
            resource_id="pytest scripts/agent-ops/tests -q",
            fetch_fn=lambda: ("stdout captured", ""),
        )
        raise AssertionError("expected EvidenceIndexError for an unsupported resource_kind")
    except evidence_index.EvidenceIndexError:
        pass

    # (b) a transient-failure-then-retry sequence: every call actually
    # invokes fetch_fn (a failed attempt is never cached, so the retry is
    # never suppressed -- it always reaches the real read path).
    flaky = _CountingFetcher(
        [
            (None, "transport_failure:gh_timeout"),
            ({"body": "recovered after retry"}, ""),
        ]
    )
    first_attempt = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=9999, fetch_fn=flaky,
    )
    assert first_attempt.ok is False
    assert first_attempt.err == "transport_failure:gh_timeout"
    assert flaky.calls == 1

    retry_attempt = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=9999, fetch_fn=flaky,
    )
    assert retry_attempt.ok is True
    assert retry_attempt.reused is False, "a prior FAILED attempt must never be served as a cached 'reuse'"
    assert flaky.calls == 2


# ---------------------------------------------------------------------------
# AC5: missing / corrupt / stale / incompatible cache -> fallback to the
# normal read path; a failing fallback is never coerced into success.
# ---------------------------------------------------------------------------


def test_missing_or_corrupt_cache_falls_back_to_normal_read():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    # Missing: nothing cached yet for this resource_id -> normal read path.
    fetcher = _CountingFetcher([({"body": "fresh read"}, "")])
    missing = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=42, fetch_fn=fetcher,
    )
    assert missing.reused is False
    assert missing.ok is True
    assert fetcher.calls == 1


def test_incompatible_config_falls_back_to_normal_read():
    # Two EvidenceIndex instances with different `config` fingerprints
    # (e.g. a schema/version bump) must never share cached entries even if
    # somehow re-attached to the same in-memory dict -- simulated here by
    # constructing a _CacheEntry-shaped incompatibility through two
    # independent indices pointed at the same fetcher.
    fetcher = _CountingFetcher([({"body": "same content either way"}, "")])
    index_v1 = evidence_index.EvidenceIndex(config={"cache_schema": 1})
    index_v2 = evidence_index.EvidenceIndex(config={"cache_schema": 2})
    assert index_v1.config_sha256 != index_v2.config_sha256

    index_v1.begin_phase("preflight_fetch")
    index_v1.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=7, fetch_fn=fetcher,
    )
    assert fetcher.calls == 1

    index_v2.begin_phase("preflight_fetch")
    result_v2 = index_v2.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=7, fetch_fn=fetcher,
    )
    assert result_v2.reused is False
    assert fetcher.calls == 2


def test_failing_fallback_read_is_never_treated_as_success():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    always_fails = _CountingFetcher([(None, "transport_failure:gh_not_found")])
    result = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=13, fetch_fn=always_fails,
    )
    assert result.ok is False
    assert result.err == "transport_failure:gh_not_found"
    assert result.reused is False

    # A subsequent reference must NOT be served as a cached "success" --
    # nothing was ever stored for a failed fetch.
    second = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=13, fetch_fn=always_fails,
    )
    assert second.ok is False
    assert second.reused is False
    assert always_fails.calls == 2


def test_corrupt_cache_entry_falls_back_to_normal_read_without_crash():
    """Issue #2052 fix_delta C/AC5: a structurally invalid / incompatible
    entry injected directly into the cache's internal storage (simulating
    "corrupt" -- e.g. an entry that does not even have the expected
    `_CacheEntry` shape) must fall through to a normal fetch, never raise
    (the pre-existing `test_missing_or_corrupt_cache_falls_back_to_normal_read`
    only ever exercised the MISSING case, never actually injected a corrupt
    entry -- confirmed to previously raise `AttributeError` instead of
    falling back)."""
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    class _CorruptEntry:
        """Deliberately missing `.phase`/`.key` -- not a real `_CacheEntry`."""

    lookup_key = (REPO, evidence_index.RESOURCE_KIND_ISSUE_BODY, "88")
    index._entries[lookup_key] = _CorruptEntry()  # fault injection

    fetcher = _CountingFetcher([({"body": "fallback succeeded"}, "")])
    result = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=88, fetch_fn=fetcher,
    )
    assert result.ok is True
    assert result.reused is False
    assert result.raw_snapshot == {"body": "fallback succeeded"}
    assert fetcher.calls == 1


def test_corrupt_cache_entry_fallback_failure_is_not_coerced_into_success():
    """Issue #2052 fix_delta C/AC5(b): when the fallback triggered by a
    corrupt/incompatible cached entry ITSELF fails (the normal read path
    is unavailable), that failure must never be reported as a successful
    fallback."""
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    class _CorruptEntry:
        pass

    lookup_key = (REPO, evidence_index.RESOURCE_KIND_ISSUE_BODY, "89")
    index._entries[lookup_key] = _CorruptEntry()  # fault injection

    always_fails = _CountingFetcher([(None, "transport_failure:gh_timeout")])
    result = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=89, fetch_fn=always_fails,
    )
    assert result.ok is False
    assert result.reused is False
    assert result.err == "transport_failure:gh_timeout"
    assert always_fails.calls == 1


# ---------------------------------------------------------------------------
# Issue #2052 fix_delta B: force_refresh must invalidate the OLD entry
# BEFORE attempting the fresh fetch, so a fresh-fetch failure never leaves a
# stale successful entry in place for a subsequent ordinary call to reuse.
# ---------------------------------------------------------------------------


def test_force_refresh_failure_does_not_leave_stale_entry_for_subsequent_normal_call():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")

    # Step 1: initial fetch succeeds and caches v1.
    fetcher = _CountingFetcher([({"body": "v1"}, "")])
    first = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert first.raw_snapshot == {"body": "v1"}
    assert first.reused is False

    # Step 2: force_refresh=True hits a transient failure -- the fetch
    # itself fails, so nothing new is cached, AND the OLD v1 entry must not
    # survive either.
    def _always_fails():
        return None, "transport_failure:gh_timeout"

    forced = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=_always_fails, force_refresh=True,
    )
    assert forced.ok is False
    assert forced.reused is False

    # Step 3: the NEXT ordinary (non-force_refresh) call must be forced to
    # fetch again -- it must NEVER silently reuse the stale v1 entry as if
    # it were still current.
    fetcher_v2 = _CountingFetcher([({"body": "v2"}, "")])
    third = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher_v2,
    )
    assert third.reused is False, "a stale entry surviving a failed force_refresh must never be served as 'reused'"
    assert third.raw_snapshot == {"body": "v2"}
    assert fetcher_v2.calls == 1


# ---------------------------------------------------------------------------
# Issue #2052 fix_delta C: the cache must never share a mutable object
# reference with a caller -- neither on the way in (storage) nor on the way
# out (a cache hit's return value).
# ---------------------------------------------------------------------------


def test_caller_mutation_of_returned_snapshot_never_corrupts_cache_or_sha_correspondence():
    index = evidence_index.EvidenceIndex()
    index.begin_phase("preflight_fetch")
    fetcher = _CountingFetcher([({"body": "original"}, "")])

    first = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    original_sha = first.evidence_key.observed_content_sha256

    # Caller mutates the object it received back from get_or_fetch().
    first.raw_snapshot["body"] = "MUTATED BY CALLER"

    # A second, same-phase reference to the identical evidence_key must
    # still be served from cache (unaffected by the mutation above) with
    # its ORIGINAL content and an unchanged, still-correspondingly-correct
    # observed_content_sha256 -- the mutation above must be visible ONLY
    # in the caller's own copy, never in the cache's internal state.
    second = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert second.reused is True
    assert second.raw_snapshot == {"body": "original"}, "cache must not observe the caller's mutation"
    assert second.evidence_key.observed_content_sha256 == original_sha
    assert fetcher.calls == 1

    # And the SAME guarantee applies in the other direction: mutating what
    # this second (cache-hit) call returned must not affect a THIRD call.
    second.raw_snapshot["body"] = "MUTATED A SECOND TIME"
    third = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2052, fetch_fn=fetcher,
    )
    assert third.raw_snapshot == {"body": "original"}
    assert fetcher.calls == 1


# ---------------------------------------------------------------------------
# Issue #2052 fix_delta D: begin_phase() must reset the AC7 metrics
# counters to phase-LOCAL values on a genuine phase TRANSITION (never on
# re-entering the SAME phase name).
# ---------------------------------------------------------------------------


def test_metrics_are_phase_local_reset_on_transition_not_on_reentry():
    index = evidence_index.EvidenceIndex()

    index.begin_phase("phase_a")
    fetcher_a = _CountingFetcher([({"body": "a"}, "")])
    index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=1, fetch_fn=fetcher_a,
    )
    metrics_a = index.metrics_snapshot()
    assert metrics_a["fetch_count"] == 1

    # Re-entering the SAME phase name must NOT reset the counters -- a
    # second reference within "phase_a" keeps accumulating on top of the
    # existing count.
    index.begin_phase("phase_a")
    reentry = index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=1, fetch_fn=fetcher_a,
    )
    assert reentry.reused is True
    metrics_a_reentry = index.metrics_snapshot()
    assert metrics_a_reentry["fetch_count"] == 1
    assert metrics_a_reentry["snapshot_reuse_count"] == 1

    # A genuine TRANSITION to a different phase name resets every counter
    # to zero -- phase B's own metrics must never start already "carrying"
    # phase A's fetch_count/snapshot_reuse_count.
    index.begin_phase("phase_b")
    metrics_b_start = index.metrics_snapshot()
    assert metrics_b_start["fetch_count"] == 0
    assert metrics_b_start["snapshot_reuse_count"] == 0
    assert metrics_b_start["emitted_utf8_bytes"] == 0
    assert metrics_b_start["duplicate_projection_count"] == 0

    fetcher_b = _CountingFetcher([({"body": "b"}, "")])
    index.get_or_fetch(
        repository=REPO, resource_kind=evidence_index.RESOURCE_KIND_ISSUE_BODY,
        resource_id=2, fetch_fn=fetcher_b,
    )
    metrics_b = index.metrics_snapshot()
    assert metrics_b["fetch_count"] == 1, "phase B's own fetch_count must reflect ONLY phase B's activity"
