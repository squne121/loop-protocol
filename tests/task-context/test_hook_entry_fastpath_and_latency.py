"""Issue #2564 In Scope -- "UserPromptSubmit Task Context adapter の timeout
は既定 30 秒を使わず最大 1 秒の bounded hot-path budget とし、既存 SQLite
busy budget 200ms との整合を保つ" (PR #2615 fix_delta 6).

These are deterministic, non-timing-based contract tests (never flaky by
construction, matching this suite's existing `test_synchronous_mode_
correctness.py` convention) -- they assert the *timeout value actually
passed* to the CLI subprocess call and the *conditional skip* of the
`git remote get-url origin` probe, not a wall-clock measurement."""

from __future__ import annotations

import pytest

import hook_entry


def test_given_hot_path_timeout_constant_when_read_then_bounded_at_one_second():
    assert hook_entry.HOT_PATH_TIMEOUT_SECONDS == 1.0
    # In Scope: "既存 SQLite busy budget 200ms との整合" -- the hot-path
    # budget must comfortably exceed the DB busy-retry budget it wraps.
    assert hook_entry.HOT_PATH_TIMEOUT_SECONDS < hook_entry.DEFAULT_TIMEOUT_SECONDS


def test_given_user_prompt_submit_when_dispatched_then_ctl_call_uses_hot_path_timeout(monkeypatch):
    captured = {}

    def fake_call_hook(event, payload, *, timeout):
        captured["event"] = event
        captured["timeout"] = timeout
        return {"status": "ok", "data": {"decision": "pass", "reason_code": "reference_only_or_none"}}

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", fake_call_hook)
    monkeypatch.setattr(hook_entry.sys, "stdin", _FakeStdin('{"session_id": "s1", "prompt": "hello world"}'))

    exit_code = hook_entry.main(["hook_entry.py", "UserPromptSubmit"])

    assert exit_code == 0
    assert captured["timeout"] == hook_entry.HOT_PATH_TIMEOUT_SECONDS


def test_given_session_start_when_dispatched_then_ctl_call_uses_default_timeout(monkeypatch):
    captured = {}

    def fake_call_hook(event, payload, *, timeout):
        captured["timeout"] = timeout
        return {"status": "ok", "data": {"decision": "pass"}}

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", fake_call_hook)
    monkeypatch.setattr(hook_entry.sys, "stdin", _FakeStdin('{"session_id": "s1", "source": "startup"}'))
    monkeypatch.setattr(hook_entry.ctl_client, "call_query_current_by_session", lambda *a, **k: None)

    exit_code = hook_entry.main(["hook_entry.py", "SessionStart"])

    assert exit_code == 0
    assert captured["timeout"] == hook_entry.DEFAULT_TIMEOUT_SECONDS


def test_given_prompt_without_bare_hash_when_classifying_then_git_remote_probe_skipped(monkeypatch):
    """fix_delta 6: a prompt with no pattern that needs `current_repo`
    resolution (full URL / owner/repo#N / no reference at all) must never
    spawn the `git remote get-url origin` subprocess."""

    def _fail_if_called(cwd):
        raise AssertionError("git remote probe must not run for this prompt")

    monkeypatch.setattr(hook_entry, "_current_repo", _fail_if_called)

    payload: dict = {}
    hook_entry._apply_user_prompt_submit_fields(payload, {"prompt": "hello world", "cwd": "/tmp"})
    assert payload["classification_kind"] == "NONE"

    payload = {}
    hook_entry._apply_user_prompt_submit_fields(
        payload, {"prompt": "see https://github.com/owner/repo/issues/9", "cwd": "/tmp"}
    )
    assert payload.get("target_repo") == "owner/repo"

    payload = {}
    hook_entry._apply_user_prompt_submit_fields(payload, {"prompt": "work on owner/repo#9", "cwd": "/tmp"})
    assert payload.get("target_repo") == "owner/repo"


def test_given_bare_hash_prompt_when_classifying_then_git_remote_probe_invoked(monkeypatch):
    """The converse of the previous test -- a bare `#N` shorthand genuinely
    needs `current_repo` resolution, so the probe must still run."""
    calls = []

    def _record(cwd):
        calls.append(cwd)
        return "owner/current-repo"

    monkeypatch.setattr(hook_entry, "_current_repo", _record)

    payload: dict = {}
    hook_entry._apply_user_prompt_submit_fields(payload, {"prompt": "please fix #42", "cwd": "/tmp"})

    assert calls == ["/tmp"]
    assert payload.get("target_repo") == "owner/current-repo"


# ---------------------------------------------------------------------------
# OWNER PR review P2 fix_delta (PR #2632): `/task` (UserPromptExpansion)
# counterpart of fix_delta 6 above -- only pay for the `git remote get-url
# origin` probe when the already-`/task`-prefix-stripped raw target actually
# needs `current_repo` resolution, and share the single
# HOT_PATH_TIMEOUT_SECONDS budget between that probe and the subsequent
# `ctl_client.call_hook` CLI call instead of each having an independent
# timeout.
# ---------------------------------------------------------------------------


def test_given_fully_qualified_github_url_target_when_task_expanded_then_git_remote_probe_skipped(monkeypatch):
    def _fail_if_called(cwd, *, timeout=None):
        raise AssertionError("git remote probe must not run for a fully-qualified target")

    monkeypatch.setattr(hook_entry, "_current_repo", _fail_if_called)

    payload: dict = {}
    remaining = hook_entry._apply_user_prompt_expansion_fields(
        payload,
        {"command_name": "task", "command_args": "https://github.com/owner/repo/issues/9", "cwd": "/tmp"},
        hook_entry.HOT_PATH_TIMEOUT_SECONDS,
    )
    assert payload.get("slash_task_target_repo") == "owner/repo"
    assert remaining == hook_entry.HOT_PATH_TIMEOUT_SECONDS


def test_given_owner_repo_hash_target_when_task_expanded_then_git_remote_probe_skipped(monkeypatch):
    def _fail_if_called(cwd, *, timeout=None):
        raise AssertionError("git remote probe must not run for an explicit owner/repo#N target")

    monkeypatch.setattr(hook_entry, "_current_repo", _fail_if_called)

    payload: dict = {}
    remaining = hook_entry._apply_user_prompt_expansion_fields(
        payload,
        {"command_name": "task", "command_args": "owner/repo#9", "cwd": "/tmp"},
        hook_entry.HOT_PATH_TIMEOUT_SECONDS,
    )
    assert payload.get("slash_task_target_repo") == "owner/repo"
    assert remaining == hook_entry.HOT_PATH_TIMEOUT_SECONDS


def test_given_ad_hoc_title_target_when_task_expanded_then_git_remote_probe_skipped(monkeypatch):
    def _fail_if_called(cwd, *, timeout=None):
        raise AssertionError("git remote probe must not run for an ad-hoc title target")

    monkeypatch.setattr(hook_entry, "_current_repo", _fail_if_called)

    payload: dict = {}
    remaining = hook_entry._apply_user_prompt_expansion_fields(
        payload,
        {"command_name": "task", "command_args": "write the changelog", "cwd": "/tmp"},
        hook_entry.HOT_PATH_TIMEOUT_SECONDS,
    )
    assert payload.get("slash_task_ad_hoc_title") == "write the changelog"
    assert remaining == hook_entry.HOT_PATH_TIMEOUT_SECONDS


@pytest.mark.parametrize("raw_target", ["#42", "42", "issue 42", "pr #42"])
def test_given_bare_or_kind_word_target_when_task_expanded_then_git_remote_probe_invoked(monkeypatch, raw_target):
    """The converse of the skip tests above -- a bare number / kind-word
    shorthand genuinely needs `current_repo` resolution, so the probe must
    still run."""
    calls = []

    def _record(cwd, *, timeout=None):
        calls.append((cwd, timeout))
        return "owner/current-repo"

    monkeypatch.setattr(hook_entry, "_current_repo", _record)

    payload: dict = {}
    hook_entry._apply_user_prompt_expansion_fields(
        payload,
        {"command_name": "task", "command_args": raw_target, "cwd": "/tmp"},
        hook_entry.HOT_PATH_TIMEOUT_SECONDS,
    )
    assert len(calls) == 1
    assert calls[0][0] == "/tmp"
    assert payload.get("slash_task_target_repo") == "owner/current-repo"


def test_given_measured_git_probe_elapsed_time_when_task_expanded_then_remaining_budget_deducted(monkeypatch):
    """Deterministic (no real sleeping/wall-clock dependency): fake
    `time.monotonic()` to report a controlled 0.3s elapsed for the git
    probe, and assert the returned remaining budget reflects
    `HOT_PATH_TIMEOUT_SECONDS - 0.3`, floored at 0."""
    monkeypatch.setattr(hook_entry, "_current_repo", lambda cwd, *, timeout=None: "owner/current-repo")
    fake_times = iter([100.0, 100.3])
    monkeypatch.setattr(hook_entry.time, "monotonic", lambda: next(fake_times))

    payload: dict = {}
    remaining = hook_entry._apply_user_prompt_expansion_fields(
        payload, {"command_name": "task", "command_args": "#42", "cwd": "/tmp"}, hook_entry.HOT_PATH_TIMEOUT_SECONDS
    )
    assert remaining == pytest.approx(hook_entry.HOT_PATH_TIMEOUT_SECONDS - 0.3)


def test_given_measured_git_probe_elapsed_time_when_dispatched_then_ctl_call_receives_reduced_timeout(monkeypatch):
    """End-to-end via `main()`: the remaining budget after the (mocked,
    deterministic) git probe cost is what actually gets passed as
    `timeout=` into `ctl_client.call_hook(...)`, not the full
    `HOT_PATH_TIMEOUT_SECONDS`."""
    captured = {}

    def fake_call_hook(event, payload, *, timeout):
        captured["timeout"] = timeout
        return {
            "status": "ok",
            "data": {"decision": "pass", "reason_code": "slash_task_rebind", "task_id": "t1", "activity_id": "a1"},
        }

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", fake_call_hook)
    monkeypatch.setattr(hook_entry, "_current_repo", lambda cwd, *, timeout=None: "owner/current-repo")
    fake_times = iter([100.0, 100.4])
    monkeypatch.setattr(hook_entry.time, "monotonic", lambda: next(fake_times))
    monkeypatch.setattr(
        hook_entry.sys,
        "stdin",
        _FakeStdin('{"session_id": "s1", "command_name": "task", "command_args": "#42", "cwd": "/tmp"}'),
    )

    exit_code = hook_entry.main(["hook_entry.py", "UserPromptExpansion"])

    assert exit_code == 0
    assert captured["timeout"] == pytest.approx(hook_entry.HOT_PATH_TIMEOUT_SECONDS - 0.4)


def test_given_no_git_probe_needed_when_dispatched_then_ctl_call_uses_full_hot_path_timeout(monkeypatch):
    """The converse: when the git probe is skipped entirely (fully-qualified
    target), the full unreduced `HOT_PATH_TIMEOUT_SECONDS` budget is passed
    on to `ctl_client.call_hook(...)`."""
    captured = {}

    def fake_call_hook(event, payload, *, timeout):
        captured["timeout"] = timeout
        return {
            "status": "ok",
            "data": {"decision": "pass", "reason_code": "slash_task_rebind", "task_id": "t1", "activity_id": "a1"},
        }

    def _fail_if_called(cwd, *, timeout=None):
        raise AssertionError("git remote probe must not run for a fully-qualified target")

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", fake_call_hook)
    monkeypatch.setattr(hook_entry, "_current_repo", _fail_if_called)
    monkeypatch.setattr(
        hook_entry.sys,
        "stdin",
        _FakeStdin('{"session_id": "s1", "command_name": "task", "command_args": "owner/repo#9", "cwd": "/tmp"}'),
    )

    exit_code = hook_entry.main(["hook_entry.py", "UserPromptExpansion"])

    assert exit_code == 0
    assert captured["timeout"] == hook_entry.HOT_PATH_TIMEOUT_SECONDS


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
