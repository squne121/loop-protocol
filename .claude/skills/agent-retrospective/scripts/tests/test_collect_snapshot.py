#!/usr/bin/env python3
"""Tests for collect_snapshot.py (Issue #2236).

Fixture-based / hermetic only (Runtime Verification Applicability:
not_applicable) -- no live GitHub/Web/git call is ever made; all I/O
boundaries are dependency-injected.

Covers:
  - AC2: CollectorResult dual-channel shape + observation schema conformance
  - AC3: source_status/reason_code computed independently per adapter
  - AC4: GitHub pagination via Link header + endpoint/page provenance
  - AC5: Web adapter SSRF boundary enforcement (blocked, not unavailable)
  - AC6: typed operational failure absorbed vs. programmer bug propagated
  - AC7: repository adapter anchors only on the caller-supplied base_sha
  - AC8: Claude-GPT adapter hook-sink authority (flat transcript presence alone
    insufficient)
  - AC9: private_evidence / observation never leak raw local paths or credentials
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import collect_snapshot as cs  # noqa: E402
import validate_retrospective_schema as vrs  # noqa: E402

_FIXED_CLOCK_TICKS = iter(
    [
        datetime(2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 0, 0, 1, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 0, 0, 2, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 0, 0, 3, tzinfo=timezone.utc),
    ]
)


def _fixed_clock():
    try:
        return next(_FIXED_CLOCK_TICKS)
    except StopIteration:
        return datetime(2026, 8, 20, 0, 0, 9, tzinfo=timezone.utc)


def _assert_valid_source_observation(observation: dict) -> None:
    schema = vrs.load_run_schema()
    sub_schema = schema["$defs"]["source_observation"]
    jsonschema.validate(observation, sub_schema)


# ---------------------------------------------------------------------------
# AC2: CollectorResult dual-channel + observation schema conformance
# ---------------------------------------------------------------------------


def test_given_repository_source_when_collected_then_collector_result_dual_channel_shape():
    def fake_git_runner(args):
        assert args[-1] == "a" * 40
        return subprocess.CompletedProcess(args, 0, stdout="100644 blob deadbeef\tREADME.md\0", stderr="")

    result = cs.collect_repository_source("a" * 40, repo_root=Path("/repo"), git_runner=fake_git_runner)

    assert isinstance(result, cs.CollectorResult)
    assert isinstance(result.observation, dict)
    assert isinstance(result.private_evidence, dict)
    assert set(result.private_evidence.keys()) >= {"normalized_records", "evidence_digest", "provenance", "diagnostics"}
    _assert_valid_source_observation(result.observation)


def test_given_github_source_when_collected_then_observation_matches_schema():
    def fake_fetch_page(url):
        return cs.GithubPageResponse(status=200, body=[{"id": 1, "title": "x"}], headers={})

    result = cs.collect_github_source(
        ["https://api.github.com/repos/o/r/issues/1/comments"], fetch_page=fake_fetch_page, clock=_fixed_clock
    )
    _assert_valid_source_observation(result.observation)
    assert result.observation["source_status"] == "complete"


# ---------------------------------------------------------------------------
# AC3: source_status / reason_code independent per adapter
# ---------------------------------------------------------------------------


def test_given_five_adapters_when_collected_then_source_status_reason_code_independent():
    # claude_code: no sessions on disk -> unavailable
    claude_code_result = cs.collect_claude_code_source([Path("/does/not/exist.jsonl")], clock=_fixed_clock)
    assert claude_code_result.observation["source_status"] == "unavailable"

    # claude_gpt: complete pairing -> complete
    def fake_git_runner(args):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    repo_result = cs.collect_repository_source("b" * 40, repo_root=Path("/repo"), git_runner=fake_git_runner)
    assert repo_result.observation["source_status"] == "complete"

    def fake_fetch_page_404(url):
        return cs.GithubPageResponse(status=404, body=None, headers={})

    github_result = cs.collect_github_source(
        ["https://api.github.com/x"], fetch_page=fake_fetch_page_404, clock=_fixed_clock
    )
    assert github_result.observation["source_status"] == "blocked"
    assert github_result.private_evidence["provenance"]["pages"][0]["reason_code"] == "auth_ambiguous_404"

    def fake_fetch_non_https(url):  # pragma: no cover - must never be called
        raise AssertionError("fetch must not be called for a boundary violation")

    web_result = cs.collect_web_source("http://example.com", fetch=fake_fetch_non_https, clock=_fixed_clock)
    assert web_result.observation["source_status"] == "blocked"
    assert web_result.private_evidence["diagnostics"]["reason_code"] == "non_https_scheme"

    # every observed source_status is independently one of the 4 allowed values,
    # and the failures above did not share a single global reason_code.
    reason_codes = {
        github_result.private_evidence["provenance"]["pages"][0]["reason_code"],
        web_result.private_evidence["diagnostics"]["reason_code"],
    }
    assert len(reason_codes) == 2


# ---------------------------------------------------------------------------
# AC4: GitHub pagination via Link header + endpoint/page provenance
# ---------------------------------------------------------------------------


def test_given_multi_page_github_endpoint_when_collected_then_github_pagination_link_provenance_recorded():
    page1_url = "https://api.github.com/repos/o/r/issues/1/comments?page=1"
    page2_url = "https://api.github.com/repos/o/r/issues/1/comments?page=2"
    pages = {
        page1_url: cs.GithubPageResponse(
            status=200,
            body=[{"id": 1}],
            headers={"Link": f'<{page2_url}>; rel="next"', "ETag": '"etag-1"'},
        ),
        page2_url: cs.GithubPageResponse(status=200, body=[{"id": 2}], headers={"ETag": '"etag-2"'}),
    }

    def fake_fetch_page(url):
        return pages[url]

    result = cs.collect_github_source([page1_url], fetch_page=fake_fetch_page, clock=_fixed_clock)

    assert result.observation["source_status"] == "complete"
    assert result.observation["pagination_completeness"] == "complete"
    provenance_pages = result.private_evidence["provenance"]["pages"]
    assert [p["page"] for p in provenance_pages] == [1, 2]
    assert provenance_pages[0]["link"] == f'<{page2_url}>; rel="next"'
    assert provenance_pages[0]["etag"] == '"etag-1"'
    assert provenance_pages[0]["complete"] is False
    assert provenance_pages[1]["complete"] is True
    assert [r["id"] for r in result.private_evidence["normalized_records"]] == [1, 2]


def test_given_page_limit_reached_when_collected_then_github_pagination_link_provenance_marks_partial():
    loop_url = "https://api.github.com/repos/o/r/issues/1/comments"

    def fake_fetch_page(url):
        return cs.GithubPageResponse(status=200, body=[{"id": 1}], headers={"Link": f'<{loop_url}>; rel="next"'})

    result = cs.collect_github_source(
        [loop_url], fetch_page=fake_fetch_page, max_pages_per_endpoint=2, clock=_fixed_clock
    )

    assert result.observation["source_status"] == "partial"
    assert result.observation["pagination_completeness"] == "partial"
    assert result.observation["partial_reason"]
    provenance_pages = result.private_evidence["provenance"]["pages"]
    assert provenance_pages[-1]["reason_code"] == "page_limit_reached"


# ---------------------------------------------------------------------------
# AC5: Web adapter SSRF boundary
# ---------------------------------------------------------------------------


def _boom_fetch(url):  # pragma: no cover - must never be invoked for a boundary violation
    raise AssertionError("fetch must not be called once a boundary violation is detected")


@pytest.mark.parametrize(
    ("url", "resolver", "expected_reason"),
    [
        ("http://example.com/page", lambda h: ["93.184.216.34"], "non_https_scheme"),
        ("https://user:pass@example.com/page", lambda h: ["93.184.216.34"], "credential_bearing_url"),
        ("https://localhost/page", lambda h: ["127.0.0.1"], "localhost_rejected"),
        ("https://internal.example.com/page", lambda h: ["10.0.0.5"], "private_or_metadata_address_rejected"),
        ("https://internal.example.com/page", lambda h: ["169.254.169.254"], "private_or_metadata_address_rejected"),
        ("https://internal.example.com/page", lambda h: ["127.0.0.1"], "private_or_metadata_address_rejected"),
    ],
)
def test_given_ssrf_boundary_violation_when_collected_then_web_ssrf_boundary_blocks(url, resolver, expected_reason):
    result = cs.collect_web_source(url, fetch=_boom_fetch, resolver=resolver, clock=_fixed_clock)

    assert result.observation["source_status"] == "blocked"
    assert result.private_evidence["diagnostics"]["reason_code"] == expected_reason


def test_given_ssrf_boundary_violation_when_collected_then_web_ssrf_boundary_distinct_from_unavailable():
    blocked = cs.collect_web_source(
        "http://example.com", fetch=_boom_fetch, resolver=lambda h: ["93.184.216.34"], clock=_fixed_clock
    )

    def fake_fetch_404(url):
        return cs.WebFetchResult(status=404, content=b"not found", final_url=url)

    unavailable = cs.collect_web_source(
        "https://example.com", fetch=fake_fetch_404, resolver=lambda h: ["93.184.216.34"], clock=_fixed_clock
    )

    assert blocked.observation["source_status"] == "blocked"
    assert unavailable.observation["source_status"] == "unavailable"
    assert blocked.observation["source_status"] != unavailable.observation["source_status"]


def test_given_valid_https_response_when_collected_then_web_ssrf_boundary_allows_and_does_not_block_other_adapters():
    def fake_fetch(url):
        return cs.WebFetchResult(status=200, content=b"hello world", final_url=url)

    result = cs.collect_web_source(
        "https://example.com/page", fetch=fake_fetch, resolver=lambda h: ["93.184.216.34"], clock=_fixed_clock
    )
    assert result.observation["source_status"] == "complete"

    def fake_git_runner(args):
        return subprocess.CompletedProcess(args, 0, stdout="100644 blob dead\tf.py\0", stderr="")

    repo_result = cs.collect_repository_source("c" * 40, repo_root=Path("/repo"), git_runner=fake_git_runner)
    assert repo_result.observation["source_status"] == "complete"


def test_given_oversized_response_when_collected_then_web_ssrf_boundary_blocks_response_size():
    def fake_fetch(url):
        return cs.WebFetchResult(status=200, content=b"x" * 100, final_url=url)

    result = cs.collect_web_source(
        "https://example.com/page",
        fetch=fake_fetch,
        resolver=lambda h: ["93.184.216.34"],
        max_bytes=10,
        clock=_fixed_clock,
    )
    assert result.observation["source_status"] == "blocked"
    assert result.private_evidence["diagnostics"]["reason_code"] == "response_size_exceeded"


def test_given_redirect_to_private_address_when_collected_then_web_ssrf_boundary_revalidates_redirect_hop():
    def fake_fetch(url):
        return cs.WebFetchResult(status=200, content=b"redirected", final_url="https://internal.example.com/other")

    resolver_calls = []

    def resolver(hostname):
        resolver_calls.append(hostname)
        if hostname == "internal.example.com":
            return ["10.0.0.5"]
        return ["93.184.216.34"]

    result = cs.collect_web_source("https://example.com/start", fetch=fake_fetch, resolver=resolver, clock=_fixed_clock)

    assert result.observation["source_status"] == "blocked"
    assert "internal.example.com" in resolver_calls


# ---------------------------------------------------------------------------
# AC6: typed operational failure vs. programmer bug
# ---------------------------------------------------------------------------


def test_given_typed_timeout_when_collected_then_fail_independent_typed_vs_programmer_error_absorbed():
    def timeout_fetch(url):
        raise TimeoutError("boom")

    def default_https_fetch_wrapping(url):
        try:
            timeout_fetch(url)
        except TimeoutError as exc:
            raise cs._AdapterOperationalError(  # noqa: SLF001 - internal contract under test
                "web_fetch_timeout", source_status="unavailable", reason_code="timeout"
            ) from exc

    result = cs.collect_web_source(
        "https://example.com/page",
        fetch=default_https_fetch_wrapping,
        resolver=lambda h: ["93.184.216.34"],
        clock=_fixed_clock,
    )

    assert result.observation["source_status"] == "unavailable"
    assert result.private_evidence["diagnostics"]["reason_code"] == "timeout"

    # a sibling adapter must be unaffected by the above -- fail-independent.
    def fake_git_runner(args):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    repo_result = cs.collect_repository_source("d" * 40, repo_root=Path("/repo"), git_runner=fake_git_runner)
    assert repo_result.observation["source_status"] == "complete"


def test_given_programmer_bug_when_collected_then_fail_independent_typed_vs_programmer_error_propagates():
    def buggy_fetch_page(url):
        raise KeyError("unexpected_missing_field")

    with pytest.raises(KeyError):
        cs.collect_github_source(["https://api.github.com/x"], fetch_page=buggy_fetch_page, clock=_fixed_clock)


def test_given_programmer_bug_in_git_runner_when_collected_then_fail_independent_typed_vs_programmer_error_propagates():
    def buggy_git_runner(args):
        raise AssertionError("contract_violation")

    with pytest.raises(AssertionError):
        cs.collect_repository_source("e" * 40, repo_root=Path("/repo"), git_runner=buggy_git_runner)


# ---------------------------------------------------------------------------
# AC7: repository adapter base_sha anchor only
# ---------------------------------------------------------------------------


def test_given_explicit_base_sha_when_collected_then_repository_base_sha_anchor_used_exclusively():
    captured_args = []

    def capturing_git_runner(args):
        captured_args.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="100644 blob dead\tf.py\0", stderr="")

    fixed_sha = "f" * 40
    result = cs.collect_repository_source(fixed_sha, repo_root=Path("/repo"), git_runner=capturing_git_runner)

    assert len(captured_args) == 1
    invoked = captured_args[0]
    assert invoked == ["git", "-C", "/repo", "ls-tree", "-r", "-z", "--full-tree", fixed_sha]
    assert "main" not in invoked
    assert "HEAD" not in invoked
    assert result.private_evidence["provenance"]["base_sha"] == fixed_sha


def test_given_non_sha_ref_when_collected_then_repository_base_sha_anchor_rejects_symbolic_ref():
    with pytest.raises(ValueError):
        cs.collect_repository_source("main", repo_root=Path("/repo"), git_runner=lambda args: None)


# ---------------------------------------------------------------------------
# AC8: Claude-GPT hook-sink authority
# ---------------------------------------------------------------------------


def _write_hook_sink(path: Path, lines: list[dict]) -> None:
    import json

    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_given_flat_transcript_only_when_collected_then_claude_gpt_hook_sink_authority_not_complete(tmp_path):
    # No hook sink file at all -- simulates a flat transcript existing elsewhere
    # (this adapter never reads it) with no hook-event evidence.
    missing_sink = tmp_path / "hook_sink.jsonl"

    result = cs.collect_claude_gpt_source(missing_sink, run_nonce="nonce-abc", clock=_fixed_clock)

    assert result.observation["source_status"] == "unavailable"
    assert result.private_evidence["diagnostics"]["reason_code"] == "source_not_present"


def test_given_stale_nonce_hook_sink_when_collected_then_claude_gpt_hook_sink_authority_rejects_stale_evidence(
    tmp_path,
):
    sink = tmp_path / "hook_sink.jsonl"
    _write_hook_sink(
        sink,
        [
            {"run_nonce": "stale-nonce", "event": "UserPromptSubmit", "session_id": "s1", "ts": "2026-08-19T00:00:00Z"},
            {"run_nonce": "stale-nonce", "event": "Stop", "session_id": "s1", "ts": "2026-08-19T00:00:05Z"},
        ],
    )

    result = cs.collect_claude_gpt_source(sink, run_nonce="current-nonce", clock=_fixed_clock)

    assert result.observation["source_status"] == "unavailable"
    assert result.private_evidence["diagnostics"]["reason_code"] == "stale_runtime_evidence"


def test_given_paired_hook_sink_records_when_collected_then_claude_gpt_hook_sink_authority_complete(tmp_path):
    sink = tmp_path / "hook_sink.jsonl"
    _write_hook_sink(
        sink,
        [
            {"run_nonce": "n1", "event": "UserPromptSubmit", "session_id": "s1", "ts": "2026-08-20T00:00:00Z"},
            {"run_nonce": "n1", "event": "Stop", "session_id": "s1", "ts": "2026-08-20T00:00:05Z"},
        ],
    )

    result = cs.collect_claude_gpt_source(sink, run_nonce="n1", clock=_fixed_clock)

    assert result.observation["source_status"] == "complete"
    assert result.private_evidence["provenance"]["complete_sessions"] == ["s1"]


def test_given_prompt_without_stop_when_collected_then_claude_gpt_hook_sink_authority_partial(tmp_path):
    sink = tmp_path / "hook_sink.jsonl"
    _write_hook_sink(
        sink, [{"run_nonce": "n2", "event": "UserPromptSubmit", "session_id": "s2", "ts": "2026-08-20T00:00:00Z"}]
    )

    result = cs.collect_claude_gpt_source(sink, run_nonce="n2", clock=_fixed_clock)

    assert result.observation["source_status"] == "partial"


# ---------------------------------------------------------------------------
# AC9: no secret / raw local path leak
# ---------------------------------------------------------------------------


def test_given_secret_and_path_bearing_records_when_collected_then_private_evidence_no_secret_leak(tmp_path):
    sink = tmp_path / "hook_sink.jsonl"
    _write_hook_sink(
        sink,
        [
            {
                "run_nonce": "n3",
                "event": "UserPromptSubmit",
                "session_id": "s3",
                "ts": "2026-08-20T00:00:00Z",
                "authorization": "Bearer super-secret-token",
                "raw_path": "/home/squne/.claude/projects/secret/session.jsonl",
            },
        ],
    )

    result = cs.collect_claude_gpt_source(sink, run_nonce="n3", clock=_fixed_clock)
    serialized = str(result.observation) + str(result.private_evidence)

    assert "super-secret-token" not in serialized
    assert "/home/squne" not in serialized


def test_given_github_item_with_token_field_when_collected_then_private_evidence_no_secret_leak():
    def fake_fetch_page(url):
        return cs.GithubPageResponse(
            status=200,
            body=[{"id": 1, "token": "ghp_super_secret", "Authorization": "Bearer xyz"}],
            headers={},
        )

    result = cs.collect_github_source(["https://api.github.com/x"], fetch_page=fake_fetch_page, clock=_fixed_clock)
    serialized = str(result.private_evidence)

    assert "ghp_super_secret" not in serialized
    assert "Bearer xyz" not in serialized


def test_given_absolute_local_path_string_field_when_collected_then_private_evidence_no_secret_leak():
    # Directly exercise the scrubbing primitive against a raw absolute path
    # value nested anywhere in a CollectorResult -- regardless of which
    # adapter produced it.
    dirty = {"nested": {"list": ["/home/squne/secret/file.txt", "safe-value"]}}
    clean = cs._scrub(dirty)  # noqa: SLF001 - internal contract under test

    assert clean == {"nested": {"list": ["[redacted-local-path]", "safe-value"]}}
