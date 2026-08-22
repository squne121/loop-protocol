"""scripts/claude-gpt/tests/test_live_issue_create_canary.py

Issue #2306 AC5: deterministic fault-injection test suite for
`scripts/claude-gpt/live_issue_create_canary.sh`'s exact-identity
verification (AC3) and cleanup/close-state-readback (AC4) logic.

This suite never launches a real `claude-gpt` session and never touches
real GitHub: it sources `live_issue_create_canary.sh` (guarded by
`LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE=1` so the top-level `main "$@"`
invocation at the bottom of the file does not run) and calls its
`verify_identity` / `fallback_search` / `close_and_verify` /
`resolve_target_issue_number` / `cleanup_handler` shell functions directly
against the deterministic fake `gh` provider
(`scripts/claude-gpt/tests/fixtures/fake_gh.py`), following the same
"source the real production shell functions and invoke them via `bash -c`"
pattern already established by
`scripts/claude-gpt/tests/test_smoke_harness_canary_fixture.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts" / "claude-gpt"
LIVE_CANARY_SH = SCRIPT_DIR / "live_issue_create_canary.sh"
FAKE_GH_FIXTURE = SCRIPT_DIR / "tests" / "fixtures" / "fake_gh.py"

BASH_BIN = shutil.which("bash") or "/bin/bash"

REPO = "squne121/loop-protocol"
TITLE = "claude-gpt live_issue_create_canary disposable probe (fixture-title)"
BODY = "## Acceptance Criteria\n\n- [ ] AC1: disposable probe\n"
MARKER = "fixture-title"


def _q(value: str) -> str:
    """Minimal POSIX single-quote shell-escape for test-controlled literals."""
    return "'" + str(value).replace("'", "'\\''") + "'"


@pytest.fixture()
def fake_gh_setup(tmp_path: Path):
    """Sets up a fake-`gh`-shadowed PATH + FAKE_GH_STATE for one test."""
    fake_bin_dir = tmp_path / "fake-bin"
    fake_bin_dir.mkdir()
    wrapper = fake_bin_dir / "gh"
    wrapper.write_text(f'#!/bin/sh\nexec python3 "{FAKE_GH_FIXTURE}" "$@"\n', encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    state_path = tmp_path / "fake_gh_state.json"

    base_process_env = os.environ.copy()
    base_process_env["PATH"] = f"{fake_bin_dir}:{base_process_env['PATH']}"
    base_process_env["FAKE_GH_STATE"] = str(state_path)
    base_process_env["LIVE_ISSUE_CREATE_CANARY_TEST_SOURCE"] = "1"
    base_process_env.pop("FAKE_GH_CLOSE_SHOULD_FAIL", None)
    base_process_env.pop("FAKE_GH_CLOSE_NOOP", None)
    return {"process_env": base_process_env, "state_path": state_path}


def _seed_state(state_path: Path, issues: dict, next_number: int = 9010) -> None:
    state_path.write_text(
        json.dumps({"next_number": next_number, "issues": issues, "calls": []}),
        encoding="utf-8",
    )


def _issue(title: str, body: str, repo: str = REPO, state: str = "open", url: str | None = None) -> dict:
    return {"title": title, "body": body, "repo": repo, "state": state, "url": url or "https://example.invalid"}


def _run(fake_gh_setup: dict, script: str, *, fault_env: dict | None = None, timeout: float = 20):
    process_env = dict(fake_gh_setup["process_env"])
    if fault_env:
        process_env.update(fault_env)
    full_script = f". {_q(str(LIVE_CANARY_SH))}\n{script}\n"
    return subprocess.run(
        [BASH_BIN, "-c", full_script],
        capture_output=True,
        text=True,
        env=process_env,
        timeout=timeout,
    )


# --- verify_identity ---------------------------------------------------


def test_verify_identity_exact_match(fake_gh_setup):
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(
        fake_gh_setup,
        f"verify_identity 9010 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "match"


def test_verify_identity_wrong_body_is_mismatch(fake_gh_setup):
    """AC5 'wrong body': the numbered Issue exists with the right title but
    a different body -- must NOT be treated as identity-confirmed."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, "an unrelated body")})
    result = _run(
        fake_gh_setup,
        f"verify_identity 9010 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "identity_mismatch"


def test_verify_identity_number_not_found(fake_gh_setup):
    """AC5 'parsed number 無し' precursor: the numbered Issue simply does not
    exist in the fake provider state."""
    _seed_state(fake_gh_setup["state_path"], {})
    result = _run(
        fake_gh_setup,
        f"verify_identity 9999 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "not_found"


def test_verify_identity_wrong_unrelated_existing_issue(fake_gh_setup):
    """AC5 '誤った（既存無関係 Issue の）parsed number': the numbered Issue
    exists but is a completely unrelated Issue (different title/body)."""
    _seed_state(
        fake_gh_setup["state_path"],
        {"2299": _issue("Issue #2299: some unrelated title", "some unrelated body")},
    )
    result = _run(
        fake_gh_setup,
        f"verify_identity 2299 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "identity_mismatch"


def test_verify_identity_wrong_repo(fake_gh_setup):
    """AC5 'wrong repo': the numbered Issue exists but under a different
    repo than expected."""
    _seed_state(
        fake_gh_setup["state_path"],
        {"9010": _issue(TITLE, BODY, repo="someone-else/other-repo")},
    )
    result = _run(
        fake_gh_setup,
        f"verify_identity 9010 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "not_found"


def test_verify_identity_already_closed_still_matches_on_title_body(fake_gh_setup):
    """AC5 '対象が既に closed': identity is about title/body equality, not
    state -- an already-closed matching Issue is still an identity match
    (close_and_verify / resolve flow decides what to do with an
    already-closed target, not verify_identity)."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY, state="closed")})
    result = _run(
        fake_gh_setup,
        f"verify_identity 9010 {_q(REPO)} {_q(TITLE)} {_q(BODY)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "match"
    assert payload["state"] == "closed"


# --- fallback_search -----------------------------------------------------


def test_fallback_search_single_exact_match(fake_gh_setup):
    """AC5 'fallback 候補1件': exactly one exact-title candidate -> match."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(
        fake_gh_setup,
        f"fallback_search {_q(REPO)} {_q(TITLE)} {_q(MARKER)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "match"
    assert payload["candidate_count"] == 1
    assert payload["number"] == 9010


def test_fallback_search_zero_candidates(fake_gh_setup):
    """AC5 'fallback 候補0件': no matching title -> none, nothing to act on."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue("a totally different title", BODY)})
    result = _run(
        fake_gh_setup,
        f"fallback_search {_q(REPO)} {_q(TITLE)} {_q(MARKER)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "none"
    assert payload["candidate_count"] == 0


def test_fallback_search_ambiguous_multiple_candidates(fake_gh_setup):
    """AC5 'fallback 候補2件以上' + 'duplicate fake issues': two Issues share
    the exact same title -> ambiguous, must not auto-select one."""
    _seed_state(
        fake_gh_setup["state_path"],
        {
            "9010": _issue(TITLE, BODY),
            "9011": _issue(TITLE, BODY),
        },
    )
    result = _run(
        fake_gh_setup,
        f"fallback_search {_q(REPO)} {_q(TITLE)} {_q(MARKER)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "ambiguous"
    assert payload["candidate_count"] == 2


def test_fallback_search_wrong_repo_excluded(fake_gh_setup):
    _seed_state(
        fake_gh_setup["state_path"],
        {"9010": _issue(TITLE, BODY, repo="someone-else/other-repo")},
    )
    result = _run(
        fake_gh_setup,
        f"fallback_search {_q(REPO)} {_q(TITLE)} {_q(MARKER)}",
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "none"


# --- close_and_verify ------------------------------------------------------


def test_close_and_verify_success(fake_gh_setup):
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(
        fake_gh_setup,
        f"close_and_verify 9010 {_q(REPO)} {_q('cleanup comment')}",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["close_ok"] is True
    assert payload["state"] == "closed"


def test_close_and_verify_close_command_failure(fake_gh_setup):
    """AC5 'close command 失敗'."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(
        fake_gh_setup,
        f"close_and_verify 9010 {_q(REPO)} {_q('cleanup comment')}",
        fault_env={"FAKE_GH_CLOSE_SHOULD_FAIL": "1"},
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["close_ok"] is False


def test_close_and_verify_state_mismatch_after_apparent_success(fake_gh_setup):
    """AC5 'close 後 state 不一致': `gh issue close` reports success (exit 0)
    but the post-close readback shows the state was NOT actually mutated to
    closed."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(
        fake_gh_setup,
        f"close_and_verify 9010 {_q(REPO)} {_q('cleanup comment')}",
        fault_env={"FAKE_GH_CLOSE_NOOP": "1"},
    )
    payload = json.loads(result.stdout)
    assert payload["close_ok"] is True
    assert payload["state"] == "open"  # NOT closed -- this is the fault


# --- resolve_target_issue_number: end-to-end identity+fallback flow --------


def _resolve_script(canary_issue_number: str) -> str:
    return f'''
CANARY_ISSUE_NUMBER={_q(canary_issue_number)}
if resolve_target_issue_number {_q(REPO)} {_q(TITLE)} {_q(BODY)} {_q(MARKER)}; then
  RC=0
else
  RC=1
fi
python3 -c "import json; print(json.dumps({{'rc': $RC, 'identity_status': '$IDENTITY_STATUS', 'fallback_status': '$FALLBACK_STATUS', 'fallback_candidate_count': '$FALLBACK_CANDIDATE_COUNT', 'resolved_issue_number': '$RESOLVED_ISSUE_NUMBER'}}))"
'''


def test_resolve_correct_parsed_number(fake_gh_setup):
    """AC5 '正しい parsed number': the parsed number is exactly the
    disposable probe Issue -- identity match, no fallback needed."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(fake_gh_setup, _resolve_script("9010"))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 0
    assert payload["identity_status"] == "match"
    assert payload["resolved_issue_number"] == "9010"


def test_resolve_missing_parsed_number_falls_back(fake_gh_setup):
    """AC5 'parsed number 無し': empty CANARY_ISSUE_NUMBER -> straight to
    fallback search, which finds the single real candidate."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    result = _run(fake_gh_setup, _resolve_script(""))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 0
    assert payload["identity_status"] == "no_number_parsed"
    assert payload["fallback_status"] == "match"
    assert payload["resolved_issue_number"] == "9010"


def test_resolve_wrong_parsed_number_falls_back_to_correct_issue(fake_gh_setup):
    """AC5 '誤った（既存無関係 Issue の）parsed number': parsed number points
    at an unrelated existing Issue; identity check must reject it, then
    fallback must find the real probe Issue by title/marker instead."""
    _seed_state(
        fake_gh_setup["state_path"],
        {
            "2299": _issue("Issue #2299: unrelated", "unrelated body"),
            "9010": _issue(TITLE, BODY),
        },
    )
    result = _run(fake_gh_setup, _resolve_script("2299"))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 0
    assert payload["identity_status"] == "identity_mismatch"
    assert payload["fallback_status"] == "match"
    assert payload["resolved_issue_number"] == "9010"
    # the unrelated Issue must never be selected as the close target
    assert payload["resolved_issue_number"] != "2299"


def test_resolve_fallback_zero_candidates_fails_closed(fake_gh_setup):
    _seed_state(fake_gh_setup["state_path"], {})
    result = _run(fake_gh_setup, _resolve_script(""))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 1
    assert payload["fallback_status"] == "none"
    assert payload["resolved_issue_number"] == "null"


def test_resolve_fallback_multiple_candidates_fails_closed(fake_gh_setup):
    """AC5 'fallback 候補2件以上' + 'duplicate fake issues' via the full
    resolve flow: ambiguity must never auto-select a close target."""
    _seed_state(
        fake_gh_setup["state_path"],
        {
            "9010": _issue(TITLE, BODY),
            "9011": _issue(TITLE, BODY),
        },
    )
    result = _run(fake_gh_setup, _resolve_script(""))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 1
    assert payload["fallback_status"] == "ambiguous"
    assert payload["resolved_issue_number"] == "null"


def test_resolve_wrong_state_identity_match_but_prior_unexpected_state(fake_gh_setup):
    """AC5 'wrong state': the disposable probe Issue is found by number with
    matching title/body, but its state is unexpectedly already 'closed'
    (e.g. a race/duplicate close elsewhere) -- resolve should still report
    the match (state handling is a downstream cleanup concern, see
    test_already_closed_target_still_confirms_close)."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY, state="closed")})
    result = _run(fake_gh_setup, _resolve_script("9010"))
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["rc"] == 0
    assert payload["identity_status"] == "match"
    assert payload["resolved_issue_number"] == "9010"


def test_already_closed_target_still_confirms_close(fake_gh_setup):
    """AC5 '対象が既に closed': resolve + close_and_verify end-to-end on a
    target that starts out already closed must still report a successful,
    state-confirmed close (idempotent close semantics)."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY, state="closed")})
    script = _resolve_script("9010") + "\nclose_and_verify 9010 " + _q(REPO) + " " + _q("cleanup") + "\n"
    result = _run(fake_gh_setup, script)
    lines = [ln for ln in result.stdout.strip().splitlines() if ln]
    close_payload = json.loads(lines[-1])
    assert close_payload["close_ok"] is True
    assert close_payload["state"] == "closed"


# --- structural: normalized calls[] trace -----------------------------------


def test_calls_trace_records_operations(fake_gh_setup):
    """AC2: the fake provider's calls[] trace records the operations issued
    by verify_identity + close_and_verify (operation/repo/subcommand only)."""
    _seed_state(fake_gh_setup["state_path"], {"9010": _issue(TITLE, BODY)})
    _run(fake_gh_setup, f"verify_identity 9010 {_q(REPO)} {_q(TITLE)} {_q(BODY)}")
    _run(fake_gh_setup, f"close_and_verify 9010 {_q(REPO)} {_q('cleanup comment')}")
    state = json.loads(fake_gh_setup["state_path"].read_text(encoding="utf-8"))
    operations = [c["operation"] for c in state["calls"]]
    assert "issue_view" in operations
    assert "issue_close" in operations
