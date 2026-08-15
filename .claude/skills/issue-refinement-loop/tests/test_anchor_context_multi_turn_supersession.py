"""
test_anchor_context_multi_turn_supersession.py

Issue #1891: anchor_context.py multi-turn segmentation / candidate extraction
and its wiring into run_refinement_preflight.py.

AC1: segment splits `# you asked` / `# chatgpt response` markers (with
     case/whitespace normalization) into speaker-labeled segments.
AC2: unmarked spans are `speaker: unknown` (never auto-promoted to owner).
AC3: candidates() returns multiple unclassified candidates, never a single
     final_candidate winner.
AC4: _build_scope_delta_authority_evidence()'s caller routes scope_delta_decision
     to a human-judgment (fail_closed) status when anchor_context.py finds
     multiple candidates spread across a genuine multi-turn transcript.
AC5: source_ranges_covered is computed via interval union, not a naive sum
     (duplicated/overlapping ranges must not produce a false "true").
AC6: heavy mutation categories are fail-closed without owner-sourced
     evidence; non-heavy categories keep warning-only continuation.
AC7: this file itself, with >=4 fixture kinds (869-line real-shaped fixture,
     addition-only, partial-retraction, unmarked-region).
AC8: anchor_context.py has no GitHub API client of its own -- verified via a
     production subprocess E2E invocation.

Iteration 2 (PR #1923 OWNER REQUEST_CHANGES, CORRECTED_REVIEW): the private
helpers above only ever mutated `known_context`, which the planner never
reads, so the multi-turn route / heavy mutation gate never actually stopped
the loop. `test_run_preflight_subprocess_*` below invoke the real
`run_refinement_preflight.py` entrypoint as a subprocess (not the private
function) and assert the wrapper's own `status` / exit code, proving the
fail-closed wiring reaches `_apply_exit_code_mapping()`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURES_DIR = SKILL_ROOT / "tests" / "fixtures"

sys.path.insert(0, str(SCRIPTS_DIR))

import anchor_context as ac  # noqa: E402
import run_refinement_preflight as preflight  # noqa: E402

_apply_multi_turn_candidate_route = preflight._apply_multi_turn_candidate_route
_classify_heavy_mutation_gate = preflight._classify_heavy_mutation_gate
_classify_anchor_scope_reframe = preflight._classify_anchor_scope_reframe

import importlib.util as _importlib_util  # noqa: E402


def _load_plan_refinement_loop_module():
    """PR #2171 fix_delta (P1-3, OWNER adversarial review): load
    `plan_refinement_loop.py`'s `_project_scope_delta_decision_to_approval()`
    the same way `test_operator_selected_scope_reframe.py` does, so the
    producer -> consumer -> projection chain can be exercised end to end in
    this file too."""
    if "scope_signal_delta" not in sys.modules:
        _spec_sd = _importlib_util.spec_from_file_location(
            "scope_signal_delta", SCRIPTS_DIR / "scope_signal_delta.py"
        )
        assert _spec_sd is not None and _spec_sd.loader is not None
        _module_sd = _importlib_util.module_from_spec(_spec_sd)
        sys.modules["scope_signal_delta"] = _module_sd
        _spec_sd.loader.exec_module(_module_sd)

    _spec = _importlib_util.spec_from_file_location(
        "plan_refinement_loop_2156_multi_turn", SCRIPTS_DIR / "plan_refinement_loop.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = _importlib_util.module_from_spec(_spec)
    sys.modules["plan_refinement_loop_2156_multi_turn"] = _module
    _spec.loader.exec_module(_module)
    return _module

_REPO_2156 = "squne121/loop-protocol"
_ISSUE_2156 = 2156
_URL_2156 = f"https://github.com/{_REPO_2156}/issues/{_ISSUE_2156}#issuecomment-999001"


def _payload_2156(association: str) -> dict:
    return {"author_association": association}


# ---------------------------------------------------------------------------
# Fixture 1: 869-line real-shaped fixture (Allowed Paths file)
# ---------------------------------------------------------------------------

FIXTURE_869_LINES = (FIXTURES_DIR / "anchor_multi_turn_869_lines.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture 2: addition-only -- later turn adds to, but never retracts, earlier
# directives.
# ---------------------------------------------------------------------------

FIXTURE_ADDITION_ONLY = """\
# You Asked

Please implement retry logic for the job runner.

- Add exponential backoff
- Cap retries at 5

# ChatGPT Response

Looks reasonable. A couple of notes.

- Please add jitter to the backoff
- Please log every retry attempt

# You Asked

Good point -- in addition to those, please also add a metric counter for
retries so we can alert on retry storms.

- Add a `job_retry_total` metric counter
"""


# ---------------------------------------------------------------------------
# Fixture 3: partial retraction -- one earlier point is retracted, others are
# explicitly kept.
# ---------------------------------------------------------------------------

FIXTURE_PARTIAL_RETRACTION = """\
# You Asked

Plan for the retry logic:

- Add exponential backoff
- Reuse the existing common/retry.py helper
- Cap retries at 5

# ChatGPT Response

REQUEST_CHANGES.

- Please also add an idempotency key
- Please document the dead-letter path

# You Asked

Keep the backoff and the retry cap as-is. Drop the "reuse common/retry.py"
requirement -- write a new helper instead, since the old one is being
deprecated separately.

- Keep: exponential backoff
- Keep: cap retries at 5
- Drop: reuse common/retry.py
"""


# ---------------------------------------------------------------------------
# Fixture 4: unmarked region -- a comment with no conversation markers at all
# (plain single-turn review comment).
# ---------------------------------------------------------------------------

FIXTURE_UNMARKED_REGION = """\
This is a plain review comment with no exported-conversation markers.

- Please rename `foo` to `bar`
- Please add a test for the empty-input case

Thanks!
"""


def _write_snapshot_artifact(tmp_path: Path, body: str) -> Path:
    """Build a raw_issue_snapshot.json-shaped artifact (the only supported
    anchor_context.py input shape, AC8)."""
    snapshot = {
        "schema_version": "raw_issue_snapshot/v1",
        "issue_number": 1891,
        "repo": "squne121/loop-protocol",
        "anchor_comment": {
            "snapshot": body,
            "api_url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
            "captured_at": "2026-08-01T00:00:00Z",
        },
    }
    snapshot_path = tmp_path / "raw_issue_snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    return snapshot_path


# ---------------------------------------------------------------------------
# AC1
# ---------------------------------------------------------------------------


def test_segment_splits_you_asked_chatgpt_response_markers():
    body = (
        "# You Asked\n\nplease review\n\n"
        "# ChatGPT Response\n\nrequest changes\n\n"
        "#   you   asked  \n\nfollow up\n\n"
        "# CHATGPT RESPONSE\n\nack\n"
    )
    result = ac.segment_body(body)
    assert result["schema_version"] == "ANCHOR_CONTEXT_SEGMENTS_V1"

    markers = [seg["marker"] for seg in result["segments"]]
    speakers = [seg["speaker"] for seg in result["segments"]]
    assert markers == ["you_asked", "chatgpt_response", "you_asked", "chatgpt_response"]
    assert speakers == ["owner", "quoted_assistant", "owner", "quoted_assistant"]

    for seg in result["segments"]:
        assert isinstance(seg["start_line"], int)
        assert isinstance(seg["end_line"], int)
        assert seg["end_line"] >= seg["start_line"]


def test_segment_case_and_whitespace_normalization_variants():
    body = "#you asked\ntext a\n# You Asked:\ntext b\n#  CHATGPT RESPONSE  \ntext c\n"
    result = ac.segment_body(body)
    markers = [seg["marker"] for seg in result["segments"]]
    assert markers == ["you_asked", "you_asked", "chatgpt_response"]


# ---------------------------------------------------------------------------
# AC2
# ---------------------------------------------------------------------------


def test_segment_unmarked_span_is_unknown_speaker():
    result = ac.segment_body(FIXTURE_UNMARKED_REGION)
    assert len(result["segments"]) == 1
    assert result["segments"][0]["speaker"] == ac.SPEAKER_UNKNOWN
    assert result["segments"][0]["marker"] is None
    # never auto-promoted to owner
    assert result["segments"][0]["speaker"] != ac.SPEAKER_OWNER


def test_segment_unmarked_leading_span_before_first_marker_is_unknown():
    body = "some preamble with no marker\nmore preamble\n# You Asked\nreal turn\n"
    result = ac.segment_body(body)
    assert result["segments"][0]["speaker"] == ac.SPEAKER_UNKNOWN
    assert result["segments"][0]["marker"] is None
    assert result["segments"][1]["speaker"] == ac.SPEAKER_OWNER


# ---------------------------------------------------------------------------
# AC3
# ---------------------------------------------------------------------------


def test_candidates_returns_multiple_unclassified_no_final_candidate():
    result = ac.extract_candidates(FIXTURE_869_LINES)
    assert result["schema_version"] == "ANCHOR_CONTEXT_CANDIDATES_V1"
    assert result["final_candidate"] is None
    assert len(result["candidates"]) > 1
    for cand in result["candidates"]:
        assert cand["relation"] == "unclassified"
        assert "source_span" in cand
        assert cand["source_span"]["start_line"] <= cand["source_span"]["end_line"]
        assert cand["speaker"] in (ac.SPEAKER_OWNER, ac.SPEAKER_QUOTED_ASSISTANT, ac.SPEAKER_UNKNOWN)


def test_candidates_addition_only_case_no_final_candidate():
    result = ac.extract_candidates(FIXTURE_ADDITION_ONLY)
    assert len(result["candidates"]) > 1
    assert result["final_candidate"] is None
    assert all(c["relation"] == "unclassified" for c in result["candidates"])


def test_candidates_partial_retraction_case_no_final_candidate():
    result = ac.extract_candidates(FIXTURE_PARTIAL_RETRACTION)
    assert len(result["candidates"]) > 1
    assert result["final_candidate"] is None
    assert all(c["relation"] == "unclassified" for c in result["candidates"])
    texts = [c["text"] for c in result["candidates"]]
    assert any("Drop" in t for t in texts)
    assert any("Keep" in t for t in texts)


# ---------------------------------------------------------------------------
# AC4
# ---------------------------------------------------------------------------


def test_multiple_candidates_route_to_human_judgment():
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "approved_by_trusted_anchor",
        "implementation_go": True,
        "reason": "trusted_anchor_scope_reframe",
    }
    routed = _apply_multi_turn_candidate_route(baseline_decision, segments_result, candidates_result)

    assert routed["status"] == "fail_closed"
    assert routed["implementation_go"] is False
    assert routed["anchor_context_candidate_count"] == len(candidates_result["candidates"])
    assert routed["anchor_context_marked_segment_count"] >= 2


_FULL_INTEGRITY_PREDICATES = {
    "source_fetch_complete": True,
    "source_hash_verified": True,
    "source_ranges_covered": True,
}


def test_trusted_owner_multi_turn_routes_to_advisory_chronology_metadata():
    """#1950 AC1: trusted OWNER multi-turn anchors are not a hard stop. The
    last OWNER-speaker segment's index/source span is recorded as
    `latest_owner_turn` chronology metadata only -- it must never be
    promoted to `technical_recommendation` or `mutation_authorization`
    precedence, and multi-turn ambiguity alone still does not grant
    implementation_go.

    PR #1973 (OWNER REQUEST_CHANGES, P0-1): the advisory downgrade is now
    gated on full retrieval-integrity confirmation (source fetch complete,
    hash verified, source ranges covered) in addition to trusted-OWNER
    authorship and the "no structured payload" reason."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "no_anchor_scope_reframe_v1_payload",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }
    routed = _apply_multi_turn_candidate_route(
        baseline_decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )

    assert routed["status"] == "warn"
    assert routed["reason"] == "multi_turn_anchor_context_trusted_owner_advisory"
    assert routed["implementation_go"] is False
    assert routed["anchor_context_candidate_count"] == len(candidates_result["candidates"])
    assert routed["anchor_context_marked_segment_count"] >= 2

    owner_segments = [
        seg for seg in segments_result["segments"] if seg.get("speaker") == ac.SPEAKER_OWNER
    ]
    assert owner_segments, "fixture must contain at least one owner segment"
    last_owner_segment = owner_segments[-1]

    latest_owner_turn = routed["latest_owner_turn"]
    assert latest_owner_turn["segment_index"] == last_owner_segment["index"]
    assert latest_owner_turn["source_range"]["start_line"] == last_owner_segment["start_line"]
    assert latest_owner_turn["source_range"]["end_line"] == last_owner_segment["end_line"]
    # chronology metadata must never claim precedence or authorization.
    assert "technical_recommendation" not in latest_owner_turn
    assert "mutation_authorization" not in latest_owner_turn


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P0-1): the advisory `warn` route must
# never silently overwrite a valid `fail_closed` decision whose reason is a
# distinct integrity problem, nor a valid `approved_by_trusted_anchor`
# decision -- and `known_context.anchor_reframe` must always reflect the
# FINAL routed status, never the pre-route status.
# ---------------------------------------------------------------------------


def test_owner_multi_turn_schema_invalid_not_downgraded_to_warn():
    """OWNER + multi-turn + `schema_invalid` reason must stay `fail_closed`
    unchanged -- never downgraded to `warn`."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "schema_invalid: ['some error']",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }
    routed = _apply_multi_turn_candidate_route(
        baseline_decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )

    assert routed == baseline_decision
    assert routed["status"] == "fail_closed"
    assert "schema_invalid" in routed["reason"]


def test_owner_multi_turn_wrong_repo_not_downgraded_to_warn():
    """OWNER + multi-turn + `wrong_repo` reason must stay `fail_closed`
    unchanged."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "wrong_repo: expected 'a/b', got 'c/d'",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }
    routed = _apply_multi_turn_candidate_route(
        baseline_decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )

    assert routed == baseline_decision
    assert routed["status"] == "fail_closed"


def test_owner_multi_turn_wrong_issue_number_not_downgraded_to_warn():
    """OWNER + multi-turn + `wrong_issue_number` reason must stay
    `fail_closed` unchanged."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "wrong_issue_number: expected 1, got 2",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }
    routed = _apply_multi_turn_candidate_route(
        baseline_decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )

    assert routed == baseline_decision
    assert routed["status"] == "fail_closed"


def test_owner_multi_turn_approved_by_trusted_anchor_status_preserved():
    """OWNER + multi-turn + a valid structured ANCHOR_SCOPE_REFRAME_V1
    payload (`approved_by_trusted_anchor`) must STAY `approved_by_trusted_anchor`
    -- not downgraded to `warn` -- while still recording `latest_owner_turn`
    chronology metadata."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "approved_by_trusted_anchor",
        "implementation_go": False,
        "reason": "trusted_anchor_scope_reframe",
        "anchor_author_association": "OWNER",
        "allowed_path_deltas": [],
        "required_rerun": [],
    }
    routed = _apply_multi_turn_candidate_route(
        baseline_decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )

    assert routed["status"] == "approved_by_trusted_anchor"
    assert routed["reason"] == "trusted_anchor_scope_reframe"
    assert "latest_owner_turn" in routed

    owner_segments = [
        seg for seg in segments_result["segments"] if seg.get("speaker") == ac.SPEAKER_OWNER
    ]
    last_owner_segment = owner_segments[-1]
    assert routed["latest_owner_turn"]["segment_index"] == last_owner_segment["index"]


def test_owner_multi_turn_advisory_not_applied_when_integrity_incomplete():
    """OWNER + multi-turn + `no_anchor_scope_reframe_v1_payload`, but with an
    incomplete/unconfirmed retrieval integrity predicate (e.g.
    `source_ranges_covered: False`), must NOT get the advisory downgrade --
    the original fail_closed decision is returned unchanged."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "no_anchor_scope_reframe_v1_payload",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }

    for missing_key in ("source_fetch_complete", "source_hash_verified", "source_ranges_covered"):
        predicates = dict(_FULL_INTEGRITY_PREDICATES)
        predicates[missing_key] = False
        routed = _apply_multi_turn_candidate_route(
            baseline_decision,
            segments_result,
            candidates_result,
            integrity_predicates=predicates,
        )
        assert routed == baseline_decision, (missing_key, routed)
        assert routed["status"] == "fail_closed"

    # Also: integrity_predicates omitted entirely (None) must not apply the
    # advisory downgrade either (fail-closed default).
    routed_no_predicates = _apply_multi_turn_candidate_route(
        baseline_decision, segments_result, candidates_result
    )
    assert routed_no_predicates == baseline_decision


def test_non_owner_multi_turn_route_is_unaffected_by_owner_advisory_path():
    """#1950 AC2 regression: a multi-turn anchor whose author is trusted but
    NOT the strict `OWNER` association (e.g. `MEMBER`/`COLLABORATOR`) must
    still hit the pre-existing hard fail_closed route, unchanged by the
    #1950 AC1 advisory carve-out."""
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    baseline_decision = {
        "status": "fail_closed",
        "reason": "no_anchor_scope_reframe_v1_payload",
        "implementation_go": False,
        "anchor_author_association": "MEMBER",
    }
    routed = _apply_multi_turn_candidate_route(baseline_decision, segments_result, candidates_result)

    assert routed["status"] == "fail_closed"
    assert routed["reason"] == "multi_turn_anchor_context_requires_human_judgment"
    assert "latest_owner_turn" not in routed


def test_anchor_comment_handling_reference_documents_owner_reaction_procedure():
    """#1950 AC3/AC4: the material-conflict owner-reaction procedure (up to
    3 options, reaction mapping, drift readback, untrusted-reaction
    re-evaluation) is defined in the anchor reference doc."""
    reference_doc = (
        SKILL_ROOT / "references" / "anchor-comment-handling.md"
    ).read_text(encoding="utf-8")
    assert "owner reaction" in reference_doc
    assert "untrusted reaction" in reference_doc


def test_single_turn_comment_does_not_trigger_multi_turn_route():
    # A single-turn comment (no markers) must not be routed to fail_closed by
    # this specific mechanism, even if it has several bullet candidates --
    # this guard is scoped to genuine multi-turn transcripts only.
    segments_result = ac.segment_body(FIXTURE_UNMARKED_REGION)
    candidates_result = ac.extract_candidates(FIXTURE_UNMARKED_REGION)
    baseline_decision = {"status": "fail_closed", "reason": "untrusted_author_association"}
    routed = _apply_multi_turn_candidate_route(baseline_decision, segments_result, candidates_result)
    assert routed == baseline_decision


def test_multi_turn_route_missing_inputs_is_noop():
    baseline_decision = {"status": "approved_by_trusted_anchor"}
    routed = _apply_multi_turn_candidate_route(baseline_decision, None, None)
    assert routed == baseline_decision


# ---------------------------------------------------------------------------
# #2156 AC5/AC6/AC8: producer (`_classify_anchor_scope_reframe`) -> consumer
# (`_apply_multi_turn_candidate_route`) integration -- exercises the REAL
# classifier return value (not a hand-built fixture dict) end to end.
# ---------------------------------------------------------------------------


def test_producer_consumer_chain_classify_to_route_to_approval():
    """AC8: `_classify_anchor_scope_reframe()`'s actual return value, fed
    through `_apply_multi_turn_candidate_route()`, covers the four AC5/AC6/AC3
    scenarios:

    1. trusted OWNER, single-turn, genuine absence
    2. trusted OWNER, multi-turn, genuine absence, integrity confirmed
       (advisory upgrade)
    3. trusted OWNER, multi-turn, genuine absence, integrity unconfirmed
       (stays blocking)
    4. trusted OWNER, present-but-wrong-schema-version fence (stays
       fail_closed, never not_applicable)
    """
    # --- Scenario 1: single-turn genuine absence ---
    single_turn_decision = _classify_anchor_scope_reframe(
        comment_payload=_payload_2156("OWNER"),
        anchor_body=FIXTURE_UNMARKED_REGION,
        repo=_REPO_2156,
        issue_number=_ISSUE_2156,
        anchor_url=_URL_2156,
    )
    assert single_turn_decision["status"] == "not_applicable"
    assert single_turn_decision["reason"] == "no_anchor_scope_reframe_v1_payload"

    single_turn_segments = ac.segment_body(FIXTURE_UNMARKED_REGION)
    single_turn_candidates = ac.extract_candidates(FIXTURE_UNMARKED_REGION)
    single_turn_routed = _apply_multi_turn_candidate_route(
        single_turn_decision,
        single_turn_segments,
        single_turn_candidates,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )
    # Single-turn: the multi-turn route is a no-op (fewer than 2 marked
    # segments), so the not_applicable classification passes through
    # unchanged.
    assert single_turn_routed == single_turn_decision
    assert single_turn_routed["status"] == "not_applicable"

    # PR #2171 fix_delta (P1-3, OWNER adversarial review): actually reach
    # `_project_scope_delta_decision_to_approval()` for this scenario, not
    # just the classify/route steps.
    planner = _load_plan_refinement_loop_module()
    single_turn_approval = planner._project_scope_delta_decision_to_approval(
        {"scope_delta_decision": single_turn_routed}
    )
    assert single_turn_approval["status"] == "missing_marker"
    assert single_turn_approval["present"] is True
    assert single_turn_approval["comment_url"] == single_turn_routed["anchor_comment_url"]
    assert single_turn_approval["body_sha256"] == single_turn_routed["anchor_comment_hash"]
    assert single_turn_approval["author_association"] == "OWNER"
    assert single_turn_approval["valid"] is False

    # --- Scenario 2: multi-turn genuine absence, integrity confirmed ---
    multi_turn_decision = _classify_anchor_scope_reframe(
        comment_payload=_payload_2156("OWNER"),
        anchor_body=FIXTURE_869_LINES,
        repo=_REPO_2156,
        issue_number=_ISSUE_2156,
        anchor_url=_URL_2156,
    )
    assert multi_turn_decision["status"] == "not_applicable"
    assert multi_turn_decision["reason"] == "no_anchor_scope_reframe_v1_payload"

    multi_turn_segments = ac.segment_body(FIXTURE_869_LINES)
    multi_turn_candidates = ac.extract_candidates(FIXTURE_869_LINES)

    confirmed_routed = _apply_multi_turn_candidate_route(
        multi_turn_decision,
        multi_turn_segments,
        multi_turn_candidates,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )
    assert confirmed_routed["status"] == "warn"
    assert confirmed_routed["reason"] == "multi_turn_anchor_context_trusted_owner_advisory"
    assert confirmed_routed["implementation_go"] is False

    # PR #2171 fix_delta (P1-3): the `warn` advisory route is not one of
    # `_project_scope_delta_decision_to_approval()`'s two positive lanes
    # (`approved_by_trusted_anchor` / `no_anchor_scope_reframe_v1_payload`),
    # so it falls through to the invalid lane -- confirm the full chain
    # reaches that projection without raising and without silently
    # fabricating an `approved` status.
    confirmed_approval = planner._project_scope_delta_decision_to_approval(
        {"scope_delta_decision": confirmed_routed}
    )
    assert confirmed_approval["status"] == "invalid_scope_delta_approval"
    assert confirmed_approval["valid"] is False

    # --- Scenario 3: multi-turn genuine absence, integrity unconfirmed ---
    unconfirmed_predicates = dict(_FULL_INTEGRITY_PREDICATES)
    unconfirmed_predicates["source_ranges_covered"] = False
    unconfirmed_routed = _apply_multi_turn_candidate_route(
        multi_turn_decision,
        multi_turn_segments,
        multi_turn_candidates,
        integrity_predicates=unconfirmed_predicates,
    )
    assert unconfirmed_routed["status"] == "fail_closed"
    assert unconfirmed_routed["status"] != "not_applicable"
    assert unconfirmed_routed["implementation_go"] is False
    assert (
        unconfirmed_routed["reason"]
        == "multi_turn_anchor_context_retrieval_integrity_unconfirmed"
    )

    # PR #2171 fix_delta (P1-3): the integrity-unconfirmed forced-blocking
    # route also reaches `_project_scope_delta_decision_to_approval()` and
    # is correctly classified as the invalid lane (never `approved`).
    unconfirmed_approval = planner._project_scope_delta_decision_to_approval(
        {"scope_delta_decision": unconfirmed_routed}
    )
    assert unconfirmed_approval["status"] == "invalid_scope_delta_approval"
    assert unconfirmed_approval["valid"] is False

    # --- Scenario 4: present-but-wrong-schema-version fence stays fail_closed ---
    wrong_schema_body = "```yaml\nschema_version: WRONG_SCHEMA_V1\n```\n"
    wrong_schema_decision = _classify_anchor_scope_reframe(
        comment_payload=_payload_2156("OWNER"),
        anchor_body=wrong_schema_body,
        repo=_REPO_2156,
        issue_number=_ISSUE_2156,
        anchor_url=_URL_2156,
    )
    assert wrong_schema_decision["status"] == "fail_closed"
    assert wrong_schema_decision["status"] != "not_applicable"
    assert wrong_schema_decision["reason"].startswith("schema_invalid:")

    wrong_schema_segments = ac.segment_body(wrong_schema_body)
    wrong_schema_candidates = ac.extract_candidates(wrong_schema_body)
    wrong_schema_routed = _apply_multi_turn_candidate_route(
        wrong_schema_decision,
        wrong_schema_segments,
        wrong_schema_candidates,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )
    # Not a multi-turn transcript (single segment), so passes through
    # unchanged, still fail_closed / schema_invalid.
    assert wrong_schema_routed == wrong_schema_decision
    assert wrong_schema_routed["status"] == "fail_closed"

    # PR #2171 fix_delta (P1-3): the present-but-wrong-schema-version case
    # also reaches the projection step and lands in the invalid lane, never
    # `missing_marker` (which is reserved for genuine absence).
    wrong_schema_approval = planner._project_scope_delta_decision_to_approval(
        {"scope_delta_decision": wrong_schema_routed}
    )
    assert wrong_schema_approval["status"] == "invalid_scope_delta_approval"
    assert wrong_schema_approval["valid"] is False


def test_genuine_absence_integrity_confirmed_advisory_upgrade_via_classifier():
    """AC5: the classifier's real not_applicable return value, routed through
    a genuine multi-turn transcript with full retrieval integrity confirmed,
    is upgraded to the trusted-owner advisory `warn` status."""
    decision = _classify_anchor_scope_reframe(
        comment_payload=_payload_2156("OWNER"),
        anchor_body=FIXTURE_869_LINES,
        repo=_REPO_2156,
        issue_number=_ISSUE_2156,
        anchor_url=_URL_2156,
    )
    assert decision["status"] == "not_applicable"

    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)
    routed = _apply_multi_turn_candidate_route(
        decision,
        segments_result,
        candidates_result,
        integrity_predicates=_FULL_INTEGRITY_PREDICATES,
    )
    assert routed["status"] == "warn"
    assert routed["reason"] == "multi_turn_anchor_context_trusted_owner_advisory"
    assert routed["implementation_go"] is False
    assert "latest_owner_turn" in routed


def test_genuine_absence_integrity_unconfirmed_stays_blocking_via_classifier():
    """AC6: the classifier's real not_applicable return value, routed through
    a genuine multi-turn transcript with retrieval integrity NOT confirmed,
    must stay blocking (`fail_closed`, `implementation_go: false`) rather
    than silently remaining `not_applicable` (non-blocking)."""
    decision = _classify_anchor_scope_reframe(
        comment_payload=_payload_2156("OWNER"),
        anchor_body=FIXTURE_869_LINES,
        repo=_REPO_2156,
        issue_number=_ISSUE_2156,
        anchor_url=_URL_2156,
    )
    assert decision["status"] == "not_applicable"

    segments_result = ac.segment_body(FIXTURE_869_LINES)
    candidates_result = ac.extract_candidates(FIXTURE_869_LINES)

    for missing_key in ("source_fetch_complete", "source_hash_verified", "source_ranges_covered"):
        predicates = dict(_FULL_INTEGRITY_PREDICATES)
        predicates[missing_key] = False
        routed = _apply_multi_turn_candidate_route(
            decision,
            segments_result,
            candidates_result,
            integrity_predicates=predicates,
        )
        assert routed["status"] == "fail_closed", (missing_key, routed)
        assert routed["status"] != "not_applicable"
        assert routed["implementation_go"] is False

    routed_no_predicates = _apply_multi_turn_candidate_route(
        decision, segments_result, candidates_result
    )
    assert routed_no_predicates["status"] == "fail_closed"


# ---------------------------------------------------------------------------
# AC5
# ---------------------------------------------------------------------------


def test_source_ranges_covered_interval_union_no_overcount():
    segments_result = ac.segment_body(FIXTURE_869_LINES)
    covered = ac.compute_source_ranges_covered(
        segments_result["segments"], segments_result["line_count"]
    )
    assert covered is True

    # Regression: two overlapping/duplicated ranges covering only half the
    # document must NOT be reported as covered, even though a naive sum of
    # segment lengths could coincidentally equal line_count.
    line_count = 10
    overlapping_segments = [
        {"start_line": 1, "end_line": 5},
        {"start_line": 3, "end_line": 5},  # overlaps [1,5], adds nothing new
        {"start_line": 1, "end_line": 3},  # duplicate overlap again
    ]
    # naive sum of lengths = 5 + 3 + 3 = 11 >= line_count, but real union is
    # only [1,5] -- must not be considered "covered".
    assert ac.compute_source_ranges_covered(overlapping_segments, line_count) is False

    # A genuinely contiguous, non-overlapping set of segments that spans the
    # whole range must be reported as covered.
    contiguous_segments = [
        {"start_line": 1, "end_line": 4},
        {"start_line": 5, "end_line": 10},
    ]
    assert ac.compute_source_ranges_covered(contiguous_segments, line_count) is True

    # A gap must not be considered covered.
    gapped_segments = [
        {"start_line": 1, "end_line": 4},
        {"start_line": 6, "end_line": 10},
    ]
    assert ac.compute_source_ranges_covered(gapped_segments, line_count) is False


# ---------------------------------------------------------------------------
# AC6
# ---------------------------------------------------------------------------


def test_heavy_mutation_fail_closed_without_owner_evidence():
    for category in sorted(preflight.HEAVY_MUTATION_CATEGORIES):
        gate = _classify_heavy_mutation_gate(
            mutation_category=category,
            scope_delta_decision={"status": "fail_closed"},
        )
        assert gate["status"] == "blocked"
        assert gate["fail_closed"] is True
        assert gate["is_heavy_mutation"] is True

    # Regression: with explicit owner-sourced evidence the same category is
    # allowed to proceed.
    gate_with_owner = _classify_heavy_mutation_gate(
        mutation_category="close",
        scope_delta_decision={
            "status": "approved_by_trusted_anchor",
            "anchor_author_association": "OWNER",
        },
    )
    assert gate_with_owner["status"] == "allowed"
    assert gate_with_owner["fail_closed"] is False

    # Regression: non-heavy mutation categories keep the pre-existing
    # warning-only continuation and are never blocked here.
    gate_non_heavy = _classify_heavy_mutation_gate(
        mutation_category="body_improvement",
        scope_delta_decision={"status": "fail_closed"},
    )
    assert gate_non_heavy["status"] == "warn"
    assert gate_non_heavy["fail_closed"] is False
    assert gate_non_heavy["is_heavy_mutation"] is False


# ---------------------------------------------------------------------------
# AC8
# ---------------------------------------------------------------------------


def test_anchor_context_subprocess_e2e_no_own_github_api_call():
    script_source = (SCRIPTS_DIR / "anchor_context.py").read_text(encoding="utf-8")

    # Static guard: no GitHub API client dependency at all.
    for forbidden in ("requests", "urllib.request", "httpx", "gh api", "api.github.com"):
        assert forbidden not in script_source, forbidden

    with tempfile.TemporaryDirectory() as tmp_dir:
        snapshot_path = _write_snapshot_artifact(Path(tmp_dir), FIXTURE_869_LINES)

        for subcommand in ("segment", "candidates"):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "anchor_context.py"),
                    subcommand,
                    "--snapshot-file",
                    str(snapshot_path),
                ],
                capture_output=True,
                text=True,
                shell=False,
                timeout=30,
            )
            assert completed.returncode == 0, completed.stderr
            payload = json.loads(completed.stdout)
            assert "error" not in payload

    # A snapshot artifact missing anchor_comment.snapshot must fail-closed
    # (exit 2), never silently fetch anything on its own.
    with tempfile.TemporaryDirectory() as tmp_dir:
        empty_snapshot_path = Path(tmp_dir) / "raw_issue_snapshot.json"
        empty_snapshot_path.write_text(json.dumps({"schema_version": "raw_issue_snapshot/v1"}))
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "anchor_context.py"),
                "segment",
                "--snapshot-file",
                str(empty_snapshot_path),
            ],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
        )
        assert completed.returncode == 2


# ---------------------------------------------------------------------------
# Iteration 2 (PR #1923 CORRECTED_REVIEW): fail_closed / heavy_mutation_gate
# actually reach `blockers` -> `_apply_exit_code_mapping()` via a real
# subprocess invocation of run_refinement_preflight.py's own CLI entrypoint
# (main() -> run_preflight()), not merely the private helper functions.
# ---------------------------------------------------------------------------

TARGET_SCRIPT = SCRIPTS_DIR / "run_refinement_preflight.py"

_VALID_CONTRACT_BODY = """\
## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: implementation
parent_issue: "#1"
```

## Parent Issue

#1

## Parent Goal Ref

- Goal: Test goal

## Current Validated Scope

- scripts/example.py

## Remaining Parent Gaps

- [ ] Nothing remaining

## Runtime Verification Applicability

decision: not_applicable
reason: 静的検証のみで完結するため

## Outcome

Add `scripts/example.py`.

## In Scope

- scripts/example.py

## Out of Scope

- Unrelated changes

## Acceptance Criteria

- [ ] AC1: Script exists.

## Verification Commands

```bash
uv run python3 scripts/example.py
```

## Allowed Paths

- scripts/example.py

## Stop Conditions

- Allowed Paths 外の変更が必要な場合

## Required Skills

なし
"""


def _repo_root_for_test() -> Path:
    """Mirrors the wrapper's own `_find_repo_root()` so the test can locate
    (and clean up) the artifact directory the real subprocess run writes
    to."""
    current = TARGET_SCRIPT.resolve().parent
    for _ in range(10):
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    raise AssertionError("could not locate repo root from TARGET_SCRIPT")


def _run_subprocess_fixture(
    tmp_path: Path,
    *,
    issue_number: int,
    repo: str,
    fixture: dict,
    anchor_comment_url: "str | None" = None,
) -> tuple[dict, int]:
    """Invoke the real `run_refinement_preflight.py` CLI (not the private
    Python function) against `fixture`, and return
    (refinement_preflight_result_v1.json contents, subprocess exit code)."""
    fixture_path = tmp_path / f"fixture_{issue_number}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    repo_root = _repo_root_for_test()
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)

    argv = [
        sys.executable,
        str(TARGET_SCRIPT),
        "--issue-number",
        str(issue_number),
        "--repo",
        repo,
        "--fixture",
        str(fixture_path),
    ]
    if anchor_comment_url:
        argv.extend(["--anchor-comment-url", anchor_comment_url])

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result_path = artifact_dir / "refinement_preflight_result_v1.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)

    return result, completed.returncode


def _run_subprocess_fixture_with_planner_input(
    tmp_path: Path,
    *,
    issue_number: int,
    repo: str,
    fixture: dict,
    anchor_comment_url: "str | None" = None,
) -> tuple[dict, dict, int]:
    """Like `_run_subprocess_fixture()`, but also returns the
    `planner_input.json` artifact contents (which carries
    `known_context.anchor_reframe` / `known_context.scope_delta_decision`)."""
    fixture_path = tmp_path / f"fixture_{issue_number}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    repo_root = _repo_root_for_test()
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)

    argv = [
        sys.executable,
        str(TARGET_SCRIPT),
        "--issue-number",
        str(issue_number),
        "--repo",
        repo,
        "--fixture",
        str(fixture_path),
    ]
    if anchor_comment_url:
        argv.extend(["--anchor-comment-url", anchor_comment_url])

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result_path = artifact_dir / "refinement_preflight_result_v1.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        planner_input_path = artifact_dir / "planner_input.json"
        planner_input = (
            json.loads(planner_input_path.read_text(encoding="utf-8")) if planner_input_path.exists() else {}
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)

    return result, planner_input, completed.returncode


_SUBPROC_ISSUE_MULTI_TURN = 99931891
_SUBPROC_ISSUE_HEAVY_MUTATION_BLOCKED = 99941891
_SUBPROC_ISSUE_HEAVY_MUTATION_OWNER_APPROVED = 99951891
_SUBPROC_ISSUE_NON_HEAVY_MUTATION = 99961891
_SUBPROC_REPO = "testowner/testrepo"
_SUBPROC_COMMENT_ID = 88891891
_SUBPROC_ANCHOR_URL = (
    f"https://github.com/{_SUBPROC_REPO}/issues/{_SUBPROC_ISSUE_MULTI_TURN}"
    f"#issuecomment-{_SUBPROC_COMMENT_ID}"
)


def _base_fixture(issue_number: int, *, known_context: "dict | None" = None) -> dict:
    return {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": issue_number,
        "repo": _SUBPROC_REPO,
        "now": "2026-01-01T00:00:00+00:00",
        "issue": {
            "number": issue_number,
            "title": "Subprocess fail-closed wiring fixture (#1891 iteration 2)",
            "body": _VALID_CONTRACT_BODY,
            "labels": [],
        },
        "comments": [],
        "anchor_comment_urls": [],
        "known_context": known_context,
    }


def test_run_preflight_subprocess_multi_turn_ambiguity_non_owner_blocks(tmp_path):
    """#1950 AC2: multiple marker-delimited segments + multiple candidates,
    authored by a non-OWNER (trusted-but-not-OWNER `MEMBER`), must still
    make the real subprocess entrypoint report `status: blocked` / exit code
    EXIT_BLOCKED -- the #1950 AC1 advisory route is scoped strictly to
    `anchor_author_association == "OWNER"`, matching the pre-existing heavy
    mutation gate's OWNER-only check."""
    fixture = _base_fixture(_SUBPROC_ISSUE_MULTI_TURN)
    fixture["anchor_comment_urls"] = [_SUBPROC_ANCHOR_URL]
    fixture["anchor_comments"] = [
        {
            "id": _SUBPROC_COMMENT_ID,
            "body": FIXTURE_869_LINES,
            "issue_url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/{_SUBPROC_ISSUE_MULTI_TURN}",
            "author_association": "MEMBER",
            "user": {"login": "reviewer", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": _SUBPROC_ANCHOR_URL,
            "url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/comments/{_SUBPROC_COMMENT_ID}",
        }
    ]

    result, exit_code = _run_subprocess_fixture(
        tmp_path,
        issue_number=_SUBPROC_ISSUE_MULTI_TURN,
        repo=_SUBPROC_REPO,
        fixture=fixture,
        anchor_comment_url=_SUBPROC_ANCHOR_URL,
    )

    assert exit_code == preflight.EXIT_BLOCKED, (result, exit_code)
    assert result.get("status") == "blocked", result
    assert preflight.BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED in result.get("blockers", []), result


def test_run_preflight_subprocess_multi_turn_ambiguity_trusted_owner_advisory_not_blocked(tmp_path):
    """#1950 AC1: the identical multi-turn transcript, authored by a trusted
    OWNER, must NOT hard-block the real subprocess entrypoint. multi-turn
    ambiguity alone is chronology metadata (`latest_owner_turn`), not a
    precedence or mutation-authorization signal, so it routes to an advisory
    `warn` / exit code EXIT_WARN instead of `blocked`."""
    fixture = _base_fixture(_SUBPROC_ISSUE_MULTI_TURN)
    fixture["anchor_comment_urls"] = [_SUBPROC_ANCHOR_URL]
    fixture["anchor_comments"] = [
        {
            "id": _SUBPROC_COMMENT_ID,
            "body": FIXTURE_869_LINES,
            "issue_url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/{_SUBPROC_ISSUE_MULTI_TURN}",
            "author_association": "OWNER",
            "user": {"login": "reviewer", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": _SUBPROC_ANCHOR_URL,
            "url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/comments/{_SUBPROC_COMMENT_ID}",
        }
    ]

    result, exit_code = _run_subprocess_fixture(
        tmp_path,
        issue_number=_SUBPROC_ISSUE_MULTI_TURN,
        repo=_SUBPROC_REPO,
        fixture=fixture,
        anchor_comment_url=_SUBPROC_ANCHOR_URL,
    )

    assert exit_code != preflight.EXIT_BLOCKED, (result, exit_code)
    assert result.get("status") != "blocked", result
    assert preflight.BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED not in result.get("blockers", []), result


def test_run_preflight_subprocess_heavy_mutation_without_owner_blocks(tmp_path):
    """A heavy mutation category (`close`) without owner-sourced evidence must
    make the real subprocess entrypoint report `status: blocked` / exit code
    EXIT_BLOCKED."""
    fixture = _base_fixture(
        _SUBPROC_ISSUE_HEAVY_MUTATION_BLOCKED,
        known_context={"mutation_category": "close"},
    )

    result, exit_code = _run_subprocess_fixture(
        tmp_path,
        issue_number=_SUBPROC_ISSUE_HEAVY_MUTATION_BLOCKED,
        repo=_SUBPROC_REPO,
        fixture=fixture,
    )

    assert exit_code == preflight.EXIT_BLOCKED, (result, exit_code)
    assert result.get("status") == "blocked", result
    assert preflight.BLOCKER_HEAVY_MUTATION_FAIL_CLOSED in result.get("blockers", []), result


def test_run_preflight_subprocess_heavy_mutation_with_owner_approval_not_blocked_by_gate(tmp_path):
    """Regression: an explicit owner-sourced decision must NOT be blocked by
    the heavy mutation gate (existing normal-path behavior is preserved)."""
    fixture = _base_fixture(
        _SUBPROC_ISSUE_HEAVY_MUTATION_OWNER_APPROVED,
        known_context={
            "mutation_category": "close",
            "scope_delta_decision": {
                "status": "approved_by_trusted_anchor",
                "anchor_author_association": "OWNER",
            },
        },
    )

    result, exit_code = _run_subprocess_fixture(
        tmp_path,
        issue_number=_SUBPROC_ISSUE_HEAVY_MUTATION_OWNER_APPROVED,
        repo=_SUBPROC_REPO,
        fixture=fixture,
    )

    assert preflight.BLOCKER_HEAVY_MUTATION_FAIL_CLOSED not in result.get("blockers", []), result
    assert preflight.BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED not in result.get("blockers", []), result
    # -- end of test_run_preflight_subprocess_heavy_mutation_with_owner_approval_not_blocked_by_gate --


def test_run_preflight_subprocess_non_heavy_mutation_not_blocked_by_gate(tmp_path):
    """Regression: an ordinary body-improvement mutation_category must NOT be
    blocked by the heavy mutation gate."""
    fixture = _base_fixture(
        _SUBPROC_ISSUE_NON_HEAVY_MUTATION,
        known_context={"mutation_category": "body_improvement"},
    )

    result, exit_code = _run_subprocess_fixture(
        tmp_path,
        issue_number=_SUBPROC_ISSUE_NON_HEAVY_MUTATION,
        repo=_SUBPROC_REPO,
        fixture=fixture,
    )

    assert preflight.BLOCKER_HEAVY_MUTATION_FAIL_CLOSED not in result.get("blockers", []), result
    assert preflight.BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED not in result.get("blockers", []), result


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P0-1 / P1-5): `known_context.anchor_reframe`
# must reflect the FINAL routed `scope_delta_decision.status` (post-route),
# never the pre-route status -- and the CI-visible wrapper `status`/exit code
# must surface the multi-turn trusted-owner advisory `warn` route.
# ---------------------------------------------------------------------------

_SUBPROC_ISSUE_ANCHOR_REFRAME_ADVISORY = 99971891
_SUBPROC_ISSUE_ANCHOR_REFRAME_APPROVED = 99981891
# The ANCHOR_SCOPE_REFRAME_V1 schema hardcodes `target.repo` to this repo's
# real slug (`squne121/loop-protocol`), so the "approved_by_trusted_anchor
# survives multi-turn routing" case below must use the real repo slug, not
# the generic `_SUBPROC_REPO` fixture placeholder used elsewhere in this file.
_REAL_REPO_SLUG = "squne121/loop-protocol"


def test_run_preflight_subprocess_anchor_reframe_reflects_post_route_warn_status(tmp_path):
    """#1950 P0-1 fix_delta (test 7, advisory branch): when the multi-turn
    route downgrades an OWNER `fail_closed` / `no_anchor_scope_reframe_v1_payload`
    decision to `warn`, `known_context.anchor_reframe` in the planner_input.json
    artifact must be `False` (since `warn != approved_by_trusted_anchor`) --
    never left stale from a pre-route computation."""
    issue_number = _SUBPROC_ISSUE_ANCHOR_REFRAME_ADVISORY
    comment_id = 88891892
    anchor_url = (
        f"https://github.com/{_SUBPROC_REPO}/issues/{issue_number}#issuecomment-{comment_id}"
    )
    fixture = _base_fixture(issue_number)
    fixture["issue"]["number"] = issue_number
    fixture["anchor_comment_urls"] = [anchor_url]
    fixture["anchor_comments"] = [
        {
            "id": comment_id,
            "body": FIXTURE_869_LINES,
            "issue_url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/{issue_number}",
            "author_association": "OWNER",
            "user": {"login": "owner", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": anchor_url,
            "url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/comments/{comment_id}",
        }
    ]

    result, planner_input, exit_code = _run_subprocess_fixture_with_planner_input(
        tmp_path,
        issue_number=issue_number,
        repo=_SUBPROC_REPO,
        fixture=fixture,
        anchor_comment_url=anchor_url,
    )

    known_context = planner_input.get("known_context", {})
    scope_delta_decision = known_context.get("scope_delta_decision", {})
    assert scope_delta_decision.get("status") == "warn", (planner_input, result)
    assert scope_delta_decision.get("reason") == "multi_turn_anchor_context_trusted_owner_advisory"
    assert known_context.get("anchor_reframe") is False, planner_input

    # Fix 2 (P1-5): the wrapper-level status/exit code must also surface the
    # advisory route as `warn`/EXIT_WARN.
    assert result.get("status") == "warn", result
    assert exit_code == preflight.EXIT_WARN, (result, exit_code)


def test_run_preflight_subprocess_anchor_reframe_reflects_post_route_approved_status(tmp_path):
    """#1950 P0-1 fix_delta (test 7, approved branch): a genuine multi-turn
    transcript authored by OWNER, carrying a VALID structured
    ANCHOR_SCOPE_REFRAME_V1 payload, keeps `scope_delta_decision.status ==
    "approved_by_trusted_anchor"` through the multi-turn route, so
    `known_context.anchor_reframe` (computed post-route) must be `True`."""
    issue_number = _SUBPROC_ISSUE_ANCHOR_REFRAME_APPROVED
    comment_id = 88891893
    anchor_url = (
        f"https://github.com/{_REAL_REPO_SLUG}/issues/{issue_number}#issuecomment-{comment_id}"
    )
    reframe_body = FIXTURE_ADDITION_ONLY + (
        "\n\n# You Asked\n\nFormal decision below.\n\n"
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n"
        f"  repo: {_REAL_REPO_SLUG}\n"
        f"  issue_number: {issue_number}\n"
        "decision: approve_scope_delta\n"
        "allowed_path_deltas:\n"
        "  - scripts/example.py\n"
        "rationale: Formal owner approval of the scope delta.\n"
        "required_rerun:\n"
        "  - refinement_preflight\n"
        "```\n"
    )

    fixture = _base_fixture(issue_number)
    fixture["repo"] = _REAL_REPO_SLUG
    fixture["issue"]["number"] = issue_number
    fixture["anchor_comment_urls"] = [anchor_url]
    fixture["anchor_comments"] = [
        {
            "id": comment_id,
            "body": reframe_body,
            "issue_url": f"https://api.github.com/repos/{_REAL_REPO_SLUG}/issues/{issue_number}",
            "author_association": "OWNER",
            "user": {"login": "owner", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": anchor_url,
            "url": f"https://api.github.com/repos/{_REAL_REPO_SLUG}/issues/comments/{comment_id}",
        }
    ]

    result, planner_input, exit_code = _run_subprocess_fixture_with_planner_input(
        tmp_path,
        issue_number=issue_number,
        repo=_REAL_REPO_SLUG,
        fixture=fixture,
        anchor_comment_url=anchor_url,
    )

    known_context = planner_input.get("known_context", {})
    scope_delta_decision = known_context.get("scope_delta_decision", {})
    assert scope_delta_decision.get("status") == "approved_by_trusted_anchor", (planner_input, result)
    assert known_context.get("anchor_reframe") is True, planner_input
    assert "latest_owner_turn" in scope_delta_decision, scope_delta_decision


# ---------------------------------------------------------------------------
# PR #1973 (OWNER REQUEST_CHANGES, P1-5), test 8: `_apply_exit_code_mapping()`
# direct-call assertion for the advisory-warn scenario.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PR #2171 fix_delta (P0-1, OWNER adversarial review): the
# integrity-unconfirmed forced-blocking route must actually reach the
# wrapper's `status`/exit code AND the freeform
# SCOPE_DELTA_AUTHORITY_EVIDENCE_V1 built before that route ran must not
# survive into the final known_context once the decision is confirmed
# blocking.
# ---------------------------------------------------------------------------

_SUBPROC_ISSUE_MULTI_TURN_INTEGRITY_UNCONFIRMED = 99991891


def test_run_preflight_direct_multi_turn_integrity_unconfirmed_blocks_and_drops_evidence(
    tmp_path, monkeypatch
):
    """PR #2171 fix_delta (P0-1, OWNER adversarial review, item 4): a
    genuine multi-turn OWNER anchor with genuine absence
    (`no_anchor_scope_reframe_v1_payload`) but UNCONFIRMED multi-turn
    retrieval integrity must make the real `run_preflight()` entrypoint
    report `status: blocked` / `EXIT_BLOCKED` with
    `BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED` present -- and the freeform
    authority evidence eagerly built earlier in the same call (before the
    multi-turn route could downgrade the decision to blocking) must not
    remain in the final known_context / planner_input.json artifact.

    Forcing an unconfirmed integrity predicate through a real end-to-end
    body requires monkeypatching `compute_source_ranges_covered` --
    `anchor_context.segment_body()`'s own segments always partition
    `[1, line_count]` contiguously for a real fetched body (#1891 AC5), so
    `source_ranges_covered` is always True through the genuine producer
    chain otherwise.
    """
    issue_number = _SUBPROC_ISSUE_MULTI_TURN_INTEGRITY_UNCONFIRMED
    comment_id = 88891894
    anchor_url = (
        f"https://github.com/{_SUBPROC_REPO}/issues/{issue_number}#issuecomment-{comment_id}"
    )
    fixture = _base_fixture(issue_number)
    fixture["issue"]["number"] = issue_number
    fixture["anchor_comment_urls"] = [anchor_url]
    fixture["anchor_comments"] = [
        {
            "id": comment_id,
            "body": FIXTURE_869_LINES,
            "issue_url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/{issue_number}",
            "author_association": "OWNER",
            "user": {"login": "owner", "type": "User"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "html_url": anchor_url,
            "url": f"https://api.github.com/repos/{_SUBPROC_REPO}/issues/comments/{comment_id}",
        }
    ]

    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    repo_root = _repo_root_for_test()
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(issue_number)

    monkeypatch.setattr(
        preflight.anchor_context,
        "compute_source_ranges_covered",
        lambda *args, **kwargs: False,
    )

    try:
        result, exit_code = preflight.run_preflight(
            issue_number=issue_number,
            repo=_SUBPROC_REPO,
            anchor_comment_urls=[anchor_url],
            fixture_path=fixture_path,
        )
        planner_input_path = artifact_dir / "planner_input.json"
        planner_input = (
            json.loads(planner_input_path.read_text(encoding="utf-8"))
            if planner_input_path.exists()
            else {}
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)

    assert result.get("status") == "blocked", result
    assert exit_code == preflight.EXIT_BLOCKED, (result, exit_code)
    assert preflight.BLOCKER_ANCHOR_MULTI_TURN_FAIL_CLOSED in result.get("blockers", []), result

    known_context = planner_input.get("known_context", {})
    scope_delta_decision = known_context.get("scope_delta_decision", {})
    assert scope_delta_decision.get("status") == "fail_closed", scope_delta_decision
    assert (
        scope_delta_decision.get("reason")
        == "multi_turn_anchor_context_retrieval_integrity_unconfirmed"
    ), scope_delta_decision
    assert "scope_delta_authority_evidence" not in known_context, known_context


def test_apply_exit_code_mapping_warn_scope_delta_decision_produces_exit_warn():
    """A `scope_delta_decision.status == "warn"` (set by
    `_apply_multi_turn_candidate_route()`) must make
    `_apply_exit_code_mapping()` return `("warn", EXIT_WARN)`, even when
    `plan` has no unknown-confidence decisions (the pre-existing warn
    condition)."""
    plan_no_unknown_confidence = {
        "decisions": {
            "some_policy": {"confidence": "high"},
        }
    }
    scope_delta_decision_warn = {
        "status": "warn",
        "reason": "multi_turn_anchor_context_trusted_owner_advisory",
    }

    status, exit_code = preflight._apply_exit_code_mapping(
        planner_exit_code=0,
        planner_fail_closed=False,
        blockers=[],
        plan=plan_no_unknown_confidence,
        scope_delta_decision=scope_delta_decision_warn,
    )

    assert status == "warn"
    assert exit_code == preflight.EXIT_WARN

    # Regression: scope_delta_decision absent / not warn must not force warn.
    status_pass, exit_code_pass = preflight._apply_exit_code_mapping(
        planner_exit_code=0,
        planner_fail_closed=False,
        blockers=[],
        plan=plan_no_unknown_confidence,
        scope_delta_decision=None,
    )
    assert status_pass == "pass"
    assert exit_code_pass == preflight.EXIT_PASS

    status_pass2, exit_code_pass2 = preflight._apply_exit_code_mapping(
        planner_exit_code=0,
        planner_fail_closed=False,
        blockers=[],
        plan=plan_no_unknown_confidence,
        scope_delta_decision={"status": "approved_by_trusted_anchor"},
    )
    assert status_pass2 == "pass"
    assert exit_code_pass2 == preflight.EXIT_PASS
