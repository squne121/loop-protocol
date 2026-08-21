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

PR #2269 human REQUEST_CHANGES follow-up regression matrix (2026-08-20):
  - DNS-rebinding/TOCTOU: `default_https_fetch` never re-resolves the target
    hostname, and `https_proxy`/`http_proxy` env vars are never consulted.
  - `transport_log.py`'s real dynamic import succeeds on Python 3.12
    (dataclass string-annotation resolution via `sys.modules`).
  - A resolver programmer bug (`KeyError`) propagates rather than being
    misclassified as `dns_resolution_failed`.
  - GitHub 403/429 classification distinguishes `rate_limited` from
    `permission_denied` using headers + error body.
  - `/tmp`/`/mnt`-style paths and embedded `Authorization` headers are
    scrubbed even when not at the start of a string.
  - Valid + malformed runtime evidence together downgrade to `partial`
    rather than `complete`.
"""

from __future__ import annotations

import json
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

    def fake_fetch_non_https(url, connect_ip):  # pragma: no cover - must never be called
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
# AC4 / PR #2269 P1: GitHub 403/429 rate-limit vs. permission-denied matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected_reason"),
    [
        (403, {}, {"message": "Must have admin rights."}, "permission_denied"),
        (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"}, None, "rate_limited"),
        (403, {"Retry-After": "30"}, None, "rate_limited"),
        (403, {}, {"message": "API rate limit exceeded for xxx."}, "rate_limited"),
        (403, {}, {"message": "You have triggered an abuse detection mechanism."}, "rate_limited"),
        (429, {}, None, "rate_limited"),
    ],
)
def test_given_github_403_429_status_when_collected_then_rate_limited_vs_permission_denied_classified(
    status, headers, body, expected_reason
):
    def fake_fetch_page(url):
        return cs.GithubPageResponse(status=status, body=body, headers=headers)

    result = cs.collect_github_source(["https://api.github.com/x"], fetch_page=fake_fetch_page, clock=_fixed_clock)

    provenance_pages = result.private_evidence["provenance"]["pages"]
    assert provenance_pages[0]["reason_code"] == expected_reason


def test_given_github_default_fetch_page_when_gh_api_include_output_parsed_then_status_headers_body_extracted():
    raw_stdout = (
        "HTTP/2.0 200 OK\r\n"
        "content-type: application/json; charset=utf-8\r\n"
        'link: <https://api.github.com/repos/o/r/issues?page=2>; rel="next"\r\n'
        'etag: "abc123"\r\n'
        "\r\n"
        '[{"id": 1, "title": "x"}]'
    )

    def fake_gh_runner(args):
        assert args[0:3] == ["gh", "api", "--include"]
        assert "-H" in args
        version_idx = args.index("-H") + 1
        assert args[version_idx] == f"X-GitHub-Api-Version: {cs._GITHUB_API_VERSION}"  # noqa: SLF001
        return subprocess.CompletedProcess(args, 0, stdout=raw_stdout, stderr="")

    response = cs.default_github_fetch_page("https://api.github.com/repos/o/r/issues", gh_runner=fake_gh_runner)

    assert response.status == 200
    assert response.body == [{"id": 1, "title": "x"}]
    assert response.headers["etag"] == '"abc123"'
    assert 'rel="next"' in response.headers["link"]


def test_given_github_default_fetch_page_when_gh_transport_fails_then_operational_error_raised():
    def failing_gh_runner(args):
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    with pytest.raises(cs._AdapterOperationalError) as excinfo:  # noqa: SLF001
        cs.default_github_fetch_page("https://api.github.com/x", gh_runner=failing_gh_runner)

    assert excinfo.value.reason_code == "timeout"


# ---------------------------------------------------------------------------
# AC5: Web adapter SSRF boundary
# ---------------------------------------------------------------------------


def _boom_fetch(url, connect_ip):  # pragma: no cover - must never be invoked for a boundary violation
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

    def fake_fetch_404(url, connect_ip):
        return cs.WebFetchResult(status=404, content=b"not found", final_url=url)

    unavailable = cs.collect_web_source(
        "https://example.com", fetch=fake_fetch_404, resolver=lambda h: ["93.184.216.34"], clock=_fixed_clock
    )

    assert blocked.observation["source_status"] == "blocked"
    assert unavailable.observation["source_status"] == "unavailable"
    assert blocked.observation["source_status"] != unavailable.observation["source_status"]


def test_given_valid_https_response_when_collected_then_web_ssrf_boundary_allows_and_does_not_block_other_adapters():
    def fake_fetch(url, connect_ip):
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
    def fake_fetch(url, connect_ip):
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
    def fake_fetch(url, connect_ip):
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


def test_given_redirect_to_valid_public_target_when_collected_then_web_ssrf_boundary_follows_and_fetches_each_hop():
    calls = []

    def fake_fetch(url, connect_ip):
        calls.append((url, connect_ip))
        if url == "https://example.com/start":
            return cs.WebFetchResult(status=0, content=b"", final_url="https://example.com/final")
        return cs.WebFetchResult(status=200, content=b"final content", final_url=url)

    def resolver(hostname):
        return ["93.184.216.34"]

    result = cs.collect_web_source("https://example.com/start", fetch=fake_fetch, resolver=resolver, clock=_fixed_clock)

    assert result.observation["source_status"] == "complete"
    assert len(calls) == 2
    assert calls[0][0] == "https://example.com/start"
    assert calls[1][0] == "https://example.com/final"


# ---------------------------------------------------------------------------
# AC5 / PR #2269 P0: DNS-rebinding / TOCTOU regression matrix
# ---------------------------------------------------------------------------


def test_given_dns_rebinding_resolver_when_collected_then_web_ssrf_boundary_resolves_once_and_pins_connect_ip():
    """1st resolver call returns a public IP (passes validation); a
    hypothetical 2nd call would return a private/loopback IP. Asserts the
    resolver is invoked exactly once and `fetch` receives the *first*
    (validated, public) IP as `connect_ip` -- i.e. no second resolution ever
    happens for this fetch attempt."""
    ip_sequence = iter(["93.184.216.34", "127.0.0.1"])
    call_log: list[str] = []

    def rebinding_resolver(hostname):
        call_log.append(hostname)
        return [next(ip_sequence)]

    captured: dict[str, str] = {}

    def fake_fetch(url, connect_ip):
        captured["connect_ip"] = connect_ip
        return cs.WebFetchResult(status=200, content=b"ok", final_url=url)

    result = cs.collect_web_source(
        "https://example.com/page", fetch=fake_fetch, resolver=rebinding_resolver, clock=_fixed_clock
    )

    assert result.observation["source_status"] == "complete"
    assert captured["connect_ip"] == "93.184.216.34"
    assert len(call_log) == 1


def test_given_resolver_programmer_bug_when_collected_then_web_ssrf_boundary_propagates_not_dns_resolution_failed():
    def broken_resolver(hostname):
        raise KeyError("unexpected_missing_field")

    with pytest.raises(KeyError):
        cs.collect_web_source(
            "https://example.com/page", fetch=_boom_fetch, resolver=broken_resolver, clock=_fixed_clock
        )


class _FakeTlsSocket:
    def __init__(self, response_bytes: bytes) -> None:
        self._buf = response_bytes
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def close(self) -> None:
        pass


_FAKE_HTTP_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"


def test_given_dns_rebinding_fixture_when_default_https_fetch_pins_connect_ip_then_second_resolution_never_occurs(
    monkeypatch,
):
    """Poison-tests `socket.getaddrinfo` (the real, OS-level resolver) to
    prove `default_https_fetch` never triggers a second hostname resolution
    -- it connects straight to the already-validated `connect_ip`."""

    def poison_getaddrinfo(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("default_https_fetch must never re-resolve the hostname")

    monkeypatch.setattr(cs.socket, "getaddrinfo", poison_getaddrinfo)

    captured: dict[str, object] = {}

    def fake_socket_opener(hostname, connect_ip, port, timeout):
        captured["hostname"] = hostname
        captured["connect_ip"] = connect_ip
        captured["port"] = port
        return _FakeTlsSocket(_FAKE_HTTP_RESPONSE)

    result = cs.default_https_fetch("https://example.com/page", "93.184.216.34", socket_opener=fake_socket_opener)

    assert captured["connect_ip"] == "93.184.216.34"
    assert captured["hostname"] == "example.com"
    assert result.status == 200
    assert result.content == b"hello"
    assert result.final_url == "https://example.com/page"


def test_given_https_proxy_env_set_when_default_https_fetch_then_proxy_env_is_not_consulted(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://attacker.internal:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.internal:8080")
    monkeypatch.setenv("http_proxy", "http://attacker.internal:8080")

    captured: dict[str, object] = {}

    def fake_socket_opener(hostname, connect_ip, port, timeout):
        captured["connect_ip"] = connect_ip
        return _FakeTlsSocket(_FAKE_HTTP_RESPONSE)

    result = cs.default_https_fetch("https://example.com/page", "93.184.216.34", socket_opener=fake_socket_opener)

    assert captured["connect_ip"] == "93.184.216.34"
    assert result.status == 200


def test_given_redirect_response_when_default_https_fetch_parses_then_final_url_is_location_header():
    redirect_response = b"HTTP/1.1 302 Found\r\nLocation: https://example.com/other\r\nContent-Length: 0\r\n\r\n"

    def fake_socket_opener(hostname, connect_ip, port, timeout):
        return _FakeTlsSocket(redirect_response)

    result = cs.default_https_fetch("https://example.com/page", "93.184.216.34", socket_opener=fake_socket_opener)

    assert result.status == 0
    assert result.final_url == "https://example.com/other"


# ---------------------------------------------------------------------------
# AC6: typed operational failure vs. programmer bug
# ---------------------------------------------------------------------------


def test_given_typed_timeout_when_collected_then_fail_independent_typed_vs_programmer_error_absorbed():
    def timeout_fetch(url, connect_ip):
        raise TimeoutError("boom")

    def default_https_fetch_wrapping(url, connect_ip):
        try:
            timeout_fetch(url, connect_ip)
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


def test_given_real_transport_log_fixture_when_collected_then_claude_gpt_transport_verdict_loads_via_dynamic_import(
    tmp_path,
):
    """Exercises the *real* `scripts/claude-gpt/transport_log.py` dynamic
    import end-to-end (PR #2269 P1 fix): on Python 3.12, loading a module
    that uses `from __future__ import annotations` + `@dataclass` without
    first registering it in `sys.modules` raises `AttributeError` the first
    time the dataclass's string annotations are resolved. This fixture
    proves the real module loads and `evaluate_transport_log` runs to
    completion."""
    sink = tmp_path / "hook_sink.jsonl"
    _write_hook_sink(
        sink,
        [
            {"run_nonce": "n4", "event": "UserPromptSubmit", "session_id": "s4", "ts": "2026-08-20T00:00:00Z"},
            {"run_nonce": "n4", "event": "Stop", "session_id": "s4", "ts": "2026-08-20T00:00:05Z"},
        ],
    )

    transport_log = tmp_path / "proxy.log"
    transport_events = [
        {
            "fields": {"method": "POST", "path": "/v1/messages", "query": "", "reqId": "r1"},
            "level": "info",
            "msg": "request",
            "service": "proxy",
            "t": "2026-08-20T00:00:00Z",
        },
        {
            "fields": {"reqId": "r1", "transport": "http", "model": "x"},
            "level": "info",
            "msg": "codex_upstream_request_started",
            "service": "proxy",
            "t": "2026-08-20T00:00:01Z",
        },
        {
            "fields": {"reqId": "r1", "status": 200, "model": "x", "provider": "x"},
            "level": "info",
            "msg": "request_completed",
            "service": "proxy",
            "t": "2026-08-20T00:00:02Z",
        },
    ]
    transport_log.write_text("\n".join(json.dumps(e) for e in transport_events) + "\n", encoding="utf-8")

    result = cs.collect_claude_gpt_source(sink, run_nonce="n4", transport_log_path=transport_log, clock=_fixed_clock)

    verdict = result.private_evidence["diagnostics"]["transport_verdict"]
    assert verdict["schema"] == "CLAUDE_GPT_TRANSPORT_VERDICT_V1"
    assert verdict["ok"] is True
    assert verdict["transport"]["http_count"] == 1


# ---------------------------------------------------------------------------
# PR #2269 hardening: valid + malformed evidence asymmetry
# ---------------------------------------------------------------------------


def test_given_malformed_and_valid_claude_code_lines_when_collected_then_source_status_partial(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text(
        '{"type": "user", "role": "user", "timestamp": "2026-08-20T00:00:00Z", "sessionId": "s1", "uuid": "u1"}\n'
        "{not valid json\n",
        encoding="utf-8",
    )

    result = cs.collect_claude_code_source([session], clock=_fixed_clock)

    assert result.observation["source_status"] == "partial"
    assert result.observation["partial_reason"] == "malformed_response"
    assert result.private_evidence["diagnostics"]["malformed_line_count"] == 1
    assert len(result.private_evidence["normalized_records"]) == 1


def test_given_malformed_and_valid_claude_gpt_hook_lines_when_collected_then_source_status_partial(tmp_path):
    sink = tmp_path / "hook_sink.jsonl"
    sink.write_text(
        json.dumps({"run_nonce": "n5", "event": "UserPromptSubmit", "session_id": "s5", "ts": "2026-08-20T00:00:00Z"})
        + "\n"
        + json.dumps({"run_nonce": "n5", "event": "Stop", "session_id": "s5", "ts": "2026-08-20T00:00:05Z"})
        + "\n"
        + "{not valid json\n",
        encoding="utf-8",
    )

    result = cs.collect_claude_gpt_source(sink, run_nonce="n5", clock=_fixed_clock)

    assert result.observation["source_status"] == "partial"
    assert result.observation["partial_reason"] == "malformed_response"
    assert result.private_evidence["diagnostics"]["malformed_line_count"] == 1


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


@pytest.mark.parametrize(
    "dirty_value",
    [
        "/tmp/agent-run-2236/session.jsonl",
        "/mnt/data/secret/session.jsonl",
        "/var/lib/claude/secret.jsonl",
        "/workspace/private/session.jsonl",
        "Permission denied: '/home/user/secret'",
        "traceback referenced /tmp/scratch/leaked.txt during read",
    ],
)
def test_given_embedded_or_non_home_absolute_path_when_scrubbed_then_private_evidence_no_secret_leak(dirty_value):
    clean = cs._scrub({"diagnostic": dirty_value})  # noqa: SLF001 - internal contract under test

    assert clean == {"diagnostic": "[redacted-local-path]"}


@pytest.mark.parametrize(
    "dirty_value",
    [
        "Authorization: Bearer ghp_ABCDEFGHIJ1234567890",
        "failed request with header Authorization: Bearer super-secret-abcdef",
        "token ghp_ABCDEFGHIJ1234567890ABCDEFGHIJ",
    ],
)
def test_given_embedded_credential_shaped_value_when_scrubbed_then_private_evidence_no_secret_leak(dirty_value):
    clean = cs._scrub({"diagnostic": dirty_value})  # noqa: SLF001 - internal contract under test

    assert clean == {"diagnostic": "[redacted-credential]"}


def test_given_exception_message_with_path_when_operational_result_built_then_safe_detail_dropped():
    exc = cs._AdapterOperationalError(  # noqa: SLF001 - internal contract under test
        "read failed at /home/squne/.claude/projects/x/session.jsonl",
        source_status="unavailable",
        reason_code="source_not_present",
        exception_class="OSError",
        errno=13,
    )
    result = cs._operational_result(  # noqa: SLF001 - internal contract under test
        source_type="runtime", source_id="claude_code", fetch_started_at=None, clock=_fixed_clock, exc=exc
    )

    diagnostics = result.private_evidence["diagnostics"]
    assert diagnostics["reason_code"] == "source_not_present"
    assert diagnostics["exception_class"] == "OSError"
    assert diagnostics["errno"] == 13
    assert diagnostics["safe_detail"] is None
    assert "/home/squne" not in str(diagnostics)
