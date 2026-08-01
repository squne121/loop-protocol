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
"""

from __future__ import annotations

import json
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
