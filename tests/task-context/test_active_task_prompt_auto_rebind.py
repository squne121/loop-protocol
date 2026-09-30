"""Issue #2827: ordinary user prompt -> ACTIVE Task auto-rebind.

Every test drives the REAL adapter (``hook_entry._apply_user_prompt_submit_fields``
builds the payload from a Claude-Code-shaped ``UserPromptSubmit`` stdin dict,
including the ACTIVE-only projection and ``input_provenance``) into the REAL
core (``task_context_hook_flows.on_user_prompt_submit``) against an isolated
tmp DB. No fixture injects an Activity to make a downstream signal green.
"""

from __future__ import annotations

import json
import threading

import pytest

import hook_entry
import task_context_errors as errors
import task_context_hook_flows as hook_flows
import task_context_service as service
import task_context_db as db

REPO = "owner/repo"


@pytest.fixture(autouse=True)
def _fixed_current_repo(monkeypatch):
    """Bare ``#N`` shorthand resolves against a fixed repo (no git call)."""
    monkeypatch.setattr(hook_entry, "_current_repo", lambda *_a, **_k: REPO)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hook_input(prompt, *, prompt_id="prompt-1", **overrides):
    data = {"hook_event_name": "UserPromptSubmit", "prompt_id": prompt_id, "prompt": prompt, "cwd": "/unused"}
    data.update(overrides)
    return data


def _adapter_payload(prompt, *, session, tab, **hook_overrides):
    """Real-adapter payload: exactly what ``hook_entry.main`` sends to core."""
    payload = {"herdr_tab_id": tab, "claude_session_id": session}
    hook_entry._apply_user_prompt_submit_fields(payload, _hook_input(prompt, **hook_overrides))
    return payload


def _prompt(conn, prompt, *, session="s1", tab="tab-1", **hook_overrides):
    payload = _adapter_payload(prompt, session=session, tab=tab, **hook_overrides)
    return hook_flows.on_user_prompt_submit(conn, payload)


def _start(conn, tab="tab-1", session="s1"):
    return hook_flows.on_session_start(conn, {"source": "startup", "herdr_tab_id": tab, "claude_session_id": session})[
        "binding_id"
    ]


def _current(conn, binding_id):
    return service.get_current_task_activity_for_binding(conn, binding_id)


_IDENTITY_TABLES = ("tasks", "activities", "task_ref_claims", "execution_runs", "tab_bindings")


def _identity_snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        for table in _IDENTITY_TABLES
    }


def _task_count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _bound_to_issue(conn, number, *, tab="tab-1", session="s1"):
    """Start a session and autobind it to Issue ``number`` (Task A)."""
    binding_id = _start(conn, tab, session)
    result = _prompt(conn, f"Issue #{number} を対象に作業開始", session=session, tab=tab)
    assert result["reason_code"] == "autobind", result
    return binding_id, result["task_id"]


def _existing_task_with_claim(conn, ref_kind, number, *, activity_kind=None):
    task = service.create_task(conn, title=f"existing-{ref_kind}-{number}")
    service.claim_task_ref(conn, task["id"], REPO, ref_kind, number)
    activity = service.transition_activity(conn, task["id"], activity_kind) if activity_kind else None
    return task["id"], (activity["id"] if activity else None)


# ---------------------------------------------------------------------------
# AC1 / AC10: normal flow needs zero /task
# ---------------------------------------------------------------------------


def test_active_task_rebinds_to_single_primary_target_without_slash_task(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)

    result = _prompt(conn, "Issue #20 を対象にレビューして")

    assert result["decision"] == "pass"
    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    task_b = result["task_id"]
    assert task_b != task_a
    assert service.find_live_claim(conn, REPO, "issue", 20)["task_id"] == task_b
    current_task, current_activity, current_run = _current(conn, binding_id)
    assert current_task == task_b
    assert current_activity == result["activity_id"]
    assert service.get_execution_run(conn, current_run)["task_id"] == task_b
    # Old Task A is neither deleted nor merged, and keeps its claim.
    assert service.find_live_claim(conn, REPO, "issue", 10)["task_id"] == task_a
    assert service.get_task(conn, task_a)["id"] == task_a


def test_rebind_failure_mid_transaction_leaves_no_partial_state(conn, monkeypatch):
    binding_id, task_a = _bound_to_issue(conn, 10)
    before = _identity_snapshot(conn)
    outbox_before = [tuple(r) for r in conn.execute("SELECT * FROM projection_outbox ORDER BY rowid")]

    def _boom(*_a, **_k):
        raise errors.ValidationError("injected failure after attach/event")

    monkeypatch.setattr(service, "_bump_projection_tx", _boom)
    with pytest.raises(errors.ValidationError):
        _prompt(conn, "Issue #20 を対象にレビューして")

    assert _identity_snapshot(conn) == before
    assert [tuple(r) for r in conn.execute("SELECT * FROM projection_outbox ORDER BY rowid")] == outbox_before
    assert _current(conn, binding_id)[0] == task_a
    assert service.find_live_claim(conn, REPO, "issue", 20) is None


def test_normal_active_switch_requires_zero_slash_task_invocations(conn, monkeypatch):
    slash_calls = []
    original = hook_flows.on_user_prompt_expansion
    monkeypatch.setattr(
        hook_flows,
        "on_user_prompt_expansion",
        lambda *a, **k: slash_calls.append(1) or original(*a, **k),
    )
    binding_id, _ = _bound_to_issue(conn, 10)

    result = _prompt(conn, "Issue #20 を実装して")

    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    assert _current(conn, binding_id)[0] == service.find_live_claim(conn, REPO, "issue", 20)["task_id"]
    assert slash_calls == [], "the normal ACTIVE A -> B switch must invoke /task zero times"


# ---------------------------------------------------------------------------
# AC3 / AC15: no silent rebind for reference / ambiguous / none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Issue #20 と Issue #21 を対象にレビューして",  # truly conflicting primaries
        "参考: #20。現在の作業を続けて",  # Japanese reference-only marker
        "see also #20 and keep going",  # English reference-only marker
        "続けてください",  # no target at all
    ],
)
def test_ambiguous_reference_and_multi_primary_never_auto_rebind(conn, prompt):
    binding_id, task_a = _bound_to_issue(conn, 10)
    before = _identity_snapshot(conn)

    result = _prompt(conn, prompt)

    assert result["decision"] == "pass"
    assert result.get("reason_code") != "user_prompt_primary_target_rebind"
    assert _identity_snapshot(conn) == before
    assert _current(conn, binding_id)[0] == task_a


def test_reference_only_target_never_rebinds_active_task(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)
    before = _identity_snapshot(conn)
    prompts = [
        "参考: #2826。現在の作業を続けて",
        "関連資料: Issue #2826 を実装した経緯",
        "`Issue #2826 を対象に` はコード例です",
        "> Issue #2826 を対象にレビューして\n続けてください",
        'メッセージ例 "Issue #2826 を対象に作業開始" を参考にして',
        "Issue #2826 に似た件です。related to #2826, keep going",
    ]
    for prompt in prompts:
        result = _prompt(conn, prompt)
        assert result["decision"] == "pass", prompt
        assert result.get("reason_code") != "user_prompt_primary_target_rebind", prompt
    assert _identity_snapshot(conn) == before
    assert _current(conn, binding_id)[0] == task_a
    assert service.find_live_claim(conn, REPO, "issue", 2826) is None


# ---------------------------------------------------------------------------
# AC7: bounded, same-transaction identity metadata
# ---------------------------------------------------------------------------


def test_auto_rebind_event_records_bounded_pre_post_identity_in_same_transaction(conn, monkeypatch):
    binding_id, task_a = _bound_to_issue(conn, 10)
    prompt = "Issue #20 を対象にレビューして SECRET-RAW-PROMPT-MARKER"

    result = _prompt(conn, prompt)

    rows = [
        r
        for r in conn.execute("SELECT * FROM events WHERE event_type = 'hook:UserPromptSubmit'")
        if json.loads(r["metadata_json"]).get("reason_code") == "user_prompt_primary_target_rebind"
    ]
    assert len(rows) == 1
    event = rows[0]
    metadata = json.loads(event["metadata_json"])
    assert metadata["source_task_id"] == task_a
    assert metadata["destination_task_id"] == result["task_id"]
    assert event["task_id"] == result["task_id"]
    assert event["binding_id"] == binding_id
    assert metadata["repo"] == REPO and metadata["ref_kind"] == "issue" and metadata["ref_number"] == 20
    # No raw prompt / transcript content in the journal.
    assert "SECRET-RAW-PROMPT-MARKER" not in event["metadata_json"]
    assert prompt not in json.dumps(dict(event))

    # Same transaction: if the event append fails, NO rebind survives.
    before = _identity_snapshot(conn)
    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def _boom(*_a, **_k):
        raise errors.ValidationError("injected event failure")

    monkeypatch.setattr(service, "_append_event_tx", _boom)
    with pytest.raises(errors.ValidationError):
        _prompt(conn, "Issue #30 を対象にレビューして")
    assert _identity_snapshot(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before


# ---------------------------------------------------------------------------
# AC12: initial Activity three-way rule
# ---------------------------------------------------------------------------


def test_initial_activity_is_refine_and_live_activity_is_preserved(conn):
    binding_id, _ = _bound_to_issue(conn, 10)

    # (i) a Task created in the same transaction starts with `refine`.
    created = _prompt(conn, "Issue #20 を対象にレビューして")
    assert created["task_created"] is True
    assert service.get_activity(conn, created["activity_id"])["kind"] == "refine"

    # (ii) an existing Task with a live implementation Activity keeps it.
    task_c, implementation_id = _existing_task_with_claim(conn, "issue", 30, activity_kind="implementation")
    preserved = _prompt(conn, "Issue #30 を対象にレビューして")
    assert preserved["reason_code"] == "user_prompt_primary_target_rebind"
    assert preserved["task_id"] == task_c
    assert preserved["task_created"] is False
    assert preserved["activity_id"] == implementation_id
    assert service.get_activity(conn, implementation_id)["kind"] == "implementation"
    assert service.get_activity(conn, implementation_id)["status"] == "ACTIVE"
    assert _current(conn, binding_id)[0] == task_c


def test_existing_task_with_only_done_activities_is_not_restarted_as_refine(conn):
    binding_id, _ = _bound_to_issue(conn, 10)
    task_d, done_id = _existing_task_with_claim(conn, "issue", 40, activity_kind="implementation")
    with db.write_transaction(conn):
        conn.execute("UPDATE activities SET status = 'DONE', ended_at = 'x' WHERE id = ?", (done_id,))

    result = _prompt(conn, "Issue #40 を対象にレビューして")

    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    assert result["task_id"] == task_d
    new_kind = service.get_activity(conn, result["activity_id"])["kind"]
    assert new_kind == "native_operator", "existing selector default is kept; never re-started as refine"
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM activities WHERE task_id = ?", (task_d,))]
    assert "refine" not in kinds


def test_slash_task_activity_stays_native_operator_and_only_ordinary_rebind_opts_in(conn):
    binding_id, _ = _bound_to_issue(conn, 10)

    # `/task <ref>` (UserPromptExpansion) keeps `native_operator`.
    slash = hook_flows.on_user_prompt_expansion(
        conn,
        {
            "herdr_tab_id": "tab-1",
            "claude_session_id": "s1",
            "command_name": "task",
            "slash_task_target_repo": REPO,
            "slash_task_target_ref_kind": "issue",
            "slash_task_target_ref_number": 50,
        },
    )
    assert slash["reason_code"] == "slash_task_rebind"
    assert service.get_activity(conn, slash["activity_id"])["kind"] == "native_operator"

    # A direct binder call without the opt-in argument is unchanged too.
    plain = service.bind_target_to_binding(
        conn,
        binding_id=binding_id,
        execution_run_id=_current(conn, binding_id)[2],
        repo=REPO,
        ref_kind="issue",
        ref_number=51,
        reason_code="terminal_advance_or_rebind",
    )
    assert service.get_activity(conn, plain["activity_id"])["kind"] == "native_operator"
    assert "task_created" not in plain

    # Only the opt-in argument makes a newly created Task start as `refine`.
    opted = service.bind_target_to_binding(
        conn,
        binding_id=binding_id,
        execution_run_id=_current(conn, binding_id)[2],
        repo=REPO,
        ref_kind="issue",
        ref_number=52,
        reason_code="user_prompt_primary_target_rebind",
        activity_kind_for_new_task="refine",
    )
    assert service.get_activity(conn, opted["activity_id"])["kind"] == "refine"


def test_terminal_rebind_keeps_native_operator_activity(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)
    with db.write_transaction(conn):
        conn.execute("UPDATE activities SET status = 'DONE', ended_at = 'x' WHERE task_id = ?", (task_a,))

    result = _prompt(conn, "Issue #20 を対象にレビューして")

    assert result["reason_code"] == "terminal_advance_or_rebind"
    assert service.get_activity(conn, _current(conn, binding_id)[1])["kind"] == "native_operator"


# ---------------------------------------------------------------------------
# AC14: Issue / PR boundary and reference forms through the real adapter path
# ---------------------------------------------------------------------------


def test_issue_and_claimed_pr_switch_but_unclaimed_pr_stays_local_only(conn):
    binding_id, _ = _bound_to_issue(conn, 10)

    issue = _prompt(conn, "Issue #20 を対象にレビューして")
    assert issue["reason_code"] == "user_prompt_primary_target_rebind"

    task_pr, _ = _existing_task_with_claim(conn, "pr", 60, activity_kind="implementation")
    claimed = _prompt(conn, "PR #60 を対象にレビューして")
    assert claimed["reason_code"] == "user_prompt_primary_target_rebind"
    assert claimed["task_id"] == task_pr
    assert _current(conn, binding_id)[0] == task_pr

    before = _identity_snapshot(conn)
    unclaimed = _prompt(conn, "PR #61 を対象にレビューして")
    assert unclaimed == {"decision": "pass", "reason_code": "unclaimed_pr_local_only"}
    assert _identity_snapshot(conn) == before
    assert service.find_live_claim(conn, REPO, "pr", 61) is None


def test_unclaimed_pr_written_as_bare_hash_or_japanese_prefix_never_rebinds_or_creates_task(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)
    before = _identity_snapshot(conn)
    tasks_before = _task_count(conn)

    outcomes = {
        "#61 を実装して": None,  # bare `#N`, no claim -> no Task, no rebind
        "プルリクエスト #62 を対象にレビューして": "unclaimed_pr_local_only",
        "プルリク #63 をレビューして": "unclaimed_pr_local_only",
    }
    for prompt, expected in outcomes.items():
        result = _prompt(conn, prompt)
        assert result["decision"] == "pass", prompt
        assert result.get("reason_code") != "user_prompt_primary_target_rebind", prompt
        if expected:
            assert result["reason_code"] == expected, prompt

    assert _identity_snapshot(conn) == before
    assert _task_count(conn) == tasks_before
    assert _current(conn, binding_id)[0] == task_a
    for number in (61, 62, 63):
        assert service.find_live_claim(conn, REPO, "issue", number) is None
        assert service.find_live_claim(conn, REPO, "pr", number) is None


def test_bare_hash_rebinds_only_when_live_claim_resolves_through_real_adapter_path(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)

    before = _identity_snapshot(conn)
    unresolved = _prompt(conn, "#70 を実装して")
    assert unresolved["reason_code"] == "active_rebind_bare_ref_unclaimed"
    assert unresolved["advisory"] is True
    assert _identity_snapshot(conn) == before
    assert service.find_live_claim(conn, REPO, "issue", 70) is None

    task_e, _ = _existing_task_with_claim(conn, "issue", 70, activity_kind="implementation")
    resolved = _prompt(conn, "#70 を実装して")
    assert resolved["reason_code"] == "user_prompt_primary_target_rebind"
    assert resolved["task_id"] == task_e
    assert _current(conn, binding_id)[0] == task_e


def test_prefixed_issue_ref_without_claim_creates_task_and_rebinds_via_real_adapter_path(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)

    result = _prompt(conn, "Issue #80 を対象にレビューして")

    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    assert result["task_created"] is True
    assert result["task_id"] != task_a
    assert service.find_live_claim(conn, REPO, "issue", 80)["task_id"] == result["task_id"]
    assert service.get_activity(conn, result["activity_id"])["kind"] == "refine"
    assert _current(conn, binding_id)[0] == result["task_id"]


def test_lowercase_issue_prefix_without_claim_creates_task_and_rebinds_via_real_adapter_path(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)

    result = _prompt(conn, "issue #81 を対象にレビューして")

    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    assert result["task_created"] is True
    assert service.find_live_claim(conn, REPO, "issue", 81)["task_id"] == result["task_id"]
    assert _current(conn, binding_id)[0] == result["task_id"]


# ---------------------------------------------------------------------------
# AC15: Japanese marker / non-marker words / creation-reply nouns end to end
# ---------------------------------------------------------------------------


def _unbound_autobind_number(conn, prompt, number):
    """UNBOUND session: the legacy autobind path resolves this prompt."""
    _start(conn, f"tab-u-{number}", f"s-u-{number}")
    result = _prompt(conn, prompt, session=f"s-u-{number}", tab=f"tab-u-{number}")
    assert result["reason_code"] == "autobind", (prompt, result)
    assert service.find_live_claim(conn, REPO, "issue", number)["task_id"] == result["task_id"]


def test_japanese_reference_word_in_other_clause_keeps_primary_in_unbound_and_active(conn):
    prompt = "参考までに背景を共有します。Issue #2827 を対象にレビューして"
    _unbound_autobind_number(conn, prompt, 2827)

    binding_id, _ = _bound_to_issue(conn, 10)
    result = _prompt(conn, prompt.replace("2827", "2828"))
    assert result["reason_code"] == "user_prompt_primary_target_rebind"
    assert _current(conn, binding_id)[0] == service.find_live_claim(conn, REPO, "issue", 2828)["task_id"]


def test_japanese_non_marker_words_reference_count_and_compare_logic_keep_primary_in_unbound_and_active(conn):
    numbers = iter(range(3000, 3100))
    templates = [
        "Issue #{n} を実装して。設計は docs を参照",
        "Issue #{n} の参照カウント不具合を修正して",
        "Issue #{n} の比較ロジックを修正して",
    ]
    for template in templates:
        n = next(numbers)
        _unbound_autobind_number(conn, template.format(n=n), n)

    binding_id, _ = _bound_to_issue(conn, 10, tab="tab-a", session="s-a")
    for template in templates:
        n = next(numbers)
        result = _prompt(conn, template.format(n=n), session="s-a", tab="tab-a")
        assert result["reason_code"] == "user_prompt_primary_target_rebind", template
        assert _current(conn, binding_id)[0] == service.find_live_claim(conn, REPO, "issue", n)["task_id"]


def test_creation_reply_nouns_alone_do_not_exclude_work_prompts_in_active_rebind(conn):
    binding_id, _ = _bound_to_issue(conn, 10)
    task_pr, _ = _existing_task_with_claim(conn, "pr", 2834, activity_kind="implementation")

    for prompt, kind, number in (
        ("PR #2834 のレビューコメントを修正して", "pr", 2834),
        ("Issue #2842 のコメント投稿処理を実装して", "issue", 2842),
        ("fix the post-merge check in Issue #2830", "issue", 2830),
    ):
        result = _prompt(conn, prompt)
        assert result["reason_code"] == "user_prompt_primary_target_rebind", prompt
        assert _current(conn, binding_id)[0] == service.find_live_claim(conn, REPO, kind, number)["task_id"]


# ---------------------------------------------------------------------------
# AC16: fail-closed provenance on all four identity mutation entries
# ---------------------------------------------------------------------------

_BAD_PROVENANCE = {
    "internal_or_unknown": {"input_provenance": "internal_or_unknown"},
    "key_missing": {},
    "unknown_value": {"input_provenance": "verified_human"},
    "wrong_type": {"input_provenance": 1},
    "none_value": {"input_provenance": None},
}


def _entry_setup(conn, entry):
    """Prepare one of the four mutation entries; return (binding_id, prompt)."""
    if entry == "unbound_autobind":
        return _start(conn), "Issue #90 を対象にレビューして"
    if entry == "provisional_absorb":
        binding_id = _start(conn)
        hook_flows.on_user_prompt_expansion(
            conn,
            {
                "herdr_tab_id": "tab-1",
                "claude_session_id": "s1",
                "command_name": "task",
                "slash_task_ad_hoc_title": "ad-hoc work",
            },
        )
        return binding_id, "Issue #90 を対象にレビューして"
    if entry == "terminal_rebind":
        binding_id, task_a = _bound_to_issue(conn, 10)
        with db.write_transaction(conn):
            conn.execute("UPDATE activities SET status = 'DONE', ended_at = 'x' WHERE task_id = ?", (task_a,))
        return binding_id, "Issue #90 を対象にレビューして"
    assert entry == "active_rebind"
    binding_id, _ = _bound_to_issue(conn, 10)
    return binding_id, "Issue #90 を対象にレビューして"


_ENTRIES = ("unbound_autobind", "provisional_absorb", "terminal_rebind", "active_rebind")


@pytest.mark.parametrize("entry", _ENTRIES)
@pytest.mark.parametrize("variant", sorted(_BAD_PROVENANCE))
def test_internal_or_unknown_provenance_blocks_every_identity_mutation_entry(conn, entry, variant):
    _, prompt = _entry_setup(conn, entry)
    payload = _adapter_payload(prompt, session="s1", tab="tab-1")
    assert payload["input_provenance"] == "user_prompt_observed"
    payload.pop("input_provenance")
    payload.update(_BAD_PROVENANCE[variant])
    before = _identity_snapshot(conn)

    result = hook_flows.on_user_prompt_submit(conn, payload)

    assert result["decision"] == "pass"
    assert result["reason_code"] == "internal_or_unknown_provenance_no_mutation"
    assert _identity_snapshot(conn) == before, f"{entry}/{variant} must not mutate Task identity"


@pytest.mark.parametrize("entry", _ENTRIES)
def test_observed_provenance_mutates_every_identity_mutation_entry(conn, entry):
    """Positive control for the parametrized fail-closed test above: the same
    setup with an observed user prompt DOES mutate, so the negative cases are
    not vacuous."""
    _, prompt = _entry_setup(conn, entry)
    before = _identity_snapshot(conn)

    result = _prompt(conn, prompt)

    assert result["reason_code"] in {
        "autobind",
        "provisional_absorb",
        "terminal_advance_or_rebind",
        "user_prompt_primary_target_rebind",
    }
    assert _identity_snapshot(conn) != before


# ---------------------------------------------------------------------------
# AC17 (offline part): a skipped required leaf never passes
# ---------------------------------------------------------------------------


def _load_adapter():
    import importlib.util
    import pathlib

    path = pathlib.Path(hook_flows.__file__).resolve().parent / "verify_active_task_prompt_auto_rebind.py"
    spec = importlib.util.spec_from_file_location("verify_active_task_prompt_auto_rebind_under_test", path)
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _bound_leaf(status="pass"):
    return {
        "status": status,
        "actor": "claude -p (real runtime)",
        "session_id": "sess-1",
        "run_id": "run-1",
        "before": {"task": "A"},
        "after": {"task": "B"},
        "result": "ok",
    }


def test_skipped_required_leaf_never_passes_even_when_aggregate_passes(tmp_path):
    adapter = _load_adapter()
    leaves = {name: _bound_leaf() for name in adapter.LEAF_NAMES}
    leaves["workflow-signal-applied"] = {"status": "skipped", "reason": "no evidence"}
    evidence = {"aggregate": {"status": "pass"}, "leaves": leaves}

    full = adapter.evaluate_evidence(evidence, leaf=None)
    assert full["status"] != "pass" and full["exit_code"] != 0
    assert any("workflow-signal-applied" in v for v in full["violations"])

    # `--leaf X` inspects only X: a skipped X fails; the other leaves are not evaluated.
    single = adapter.evaluate_evidence(evidence, leaf="workflow-signal-applied")
    assert single["exit_code"] != 0
    other = adapter.evaluate_evidence(evidence, leaf="ordinary-prompt-rebind")
    assert other["exit_code"] == 0 and other["status"] == "pass"

    # An absent leaf is as bad as a skipped one.
    absent = dict(leaves)
    absent.pop("slash-task-override")
    assert adapter.evaluate_evidence({"aggregate": {"status": "pass"}, "leaves": absent}, leaf=None)["exit_code"] != 0
    assert (
        adapter.evaluate_evidence({"aggregate": {"status": "pass"}, "leaves": absent}, leaf="slash-task-override")[
            "exit_code"
        ]
        != 0
    )

    # A 'pass' leaf that is not bound to actor/session/run/before/after/result is not a pass.
    unbound = dict(leaves)
    unbound["workflow-signal-applied"] = {"status": "pass"}
    assert adapter.evaluate_evidence({"aggregate": {"status": "pass"}, "leaves": unbound}, leaf=None)["exit_code"] != 0

    # All four bound leaves pass; the aggregate is irrelevant either way.
    good = {name: _bound_leaf() for name in adapter.LEAF_NAMES}
    assert adapter.evaluate_evidence({"aggregate": {"status": "fail"}, "leaves": good}, leaf=None)["exit_code"] == 0


# ---------------------------------------------------------------------------
# AC18: atomicity / idempotency / fault injection
# ---------------------------------------------------------------------------


def test_resend_same_target_creates_no_duplicate_task_and_projection_failure_does_not_block(conn, monkeypatch, capsys):
    binding_id, _ = _bound_to_issue(conn, 10)
    first = _prompt(conn, "Issue #20 を対象にレビューして")
    tasks_after_first = _task_count(conn)

    # Re-send: the claim already resolves to the current Task -> same_target.
    again = _prompt(conn, "Issue #20 を対象にレビューして", prompt_id="prompt-2")
    assert again == {"decision": "pass", "reason_code": "same_target"}
    assert _task_count(conn) == tasks_after_first
    assert _current(conn, binding_id)[0] == first["task_id"]

    # A Herdr projection failure (the detached flush cannot even start) and a
    # lost response after commit never block the prompt and never claim "not applied".
    import io
    import sys

    def _raise_oserror(*_a, **_k):
        raise OSError("herdr unavailable")

    monkeypatch.setattr(hook_entry.subprocess, "Popen", _raise_oserror)
    committed_response = {
        "status": "ok",
        "data": {"decision": "pass", "reason_code": "user_prompt_primary_target_rebind", "projection_key": "k"},
    }
    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", lambda *_a, **_k: committed_response)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_hook_input("Issue #20 を対象に"))))
    assert hook_entry.main(["hook_entry.py", "UserPromptSubmit"]) == 0

    monkeypatch.setattr(hook_entry.ctl_client, "call_hook", lambda *_a, **_k: None)  # response lost
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_hook_input("Issue #20 を対象に"))))
    assert hook_entry.main(["hook_entry.py", "UserPromptSubmit"]) == 0
    stderr = capsys.readouterr().err
    assert "NOT applied" not in stderr and "未適用" not in stderr


def test_sibling_hook_deny_after_commit_keeps_mutation_and_assumes_no_rollback(conn, monkeypatch):
    binding_id, task_a = _bound_to_issue(conn, 10)
    import inspect

    class _SiblingDeny(Exception):
        pass

    real = hook_flows.on_user_prompt_submit

    def _commit_then_sibling_denies(c, payload):
        result = real(c, payload)
        raise _SiblingDeny(result)  # a sibling hook denies the prompt AFTER our commit

    payload = _adapter_payload("Issue #20 を対象にレビューして", session="s1", tab="tab-1")
    with pytest.raises(_SiblingDeny):
        _commit_then_sibling_denies(conn, payload)

    task_b = service.find_live_claim(conn, REPO, "issue", 20)["task_id"]
    assert _current(conn, binding_id)[0] == task_b, "the committed rebind must survive a sibling deny"
    # Nothing in the adapter/core tries to compensate: no rollback/undo hook exists.
    for module in (hook_flows, hook_entry):
        source = inspect.getsource(module).lower()
        assert "undo_rebind" not in source and "rollback_rebind" not in source
    # The prompt re-processed later is simply the same target (no duplicate).
    assert _prompt(conn, "Issue #20 を対象にレビューして", prompt_id="prompt-2")["reason_code"] == "same_target"
    assert service.get_task(conn, task_a)["id"] == task_a


# ---------------------------------------------------------------------------
# AC19: ACTIVE-only projection isolation
# ---------------------------------------------------------------------------


def test_active_rebind_projection_is_isolated_from_legacy_classification_fields(conn):
    # UNBOUND autobind keeps using the legacy fields ONLY: a payload with no
    # ACTIVE projection at all still autobinds.
    _start(conn, "tab-u", "s-u")
    legacy = {
        "herdr_tab_id": "tab-u",
        "claude_session_id": "s-u",
        "input_provenance": "user_prompt_observed",
        "classification_kind": "EXPLICIT",
        "target_repo": REPO,
        "target_ref_kind": "issue",
        "target_ref_number": 500,
    }
    assert hook_flows.on_user_prompt_submit(conn, legacy)["reason_code"] == "autobind"

    # An ACTIVE branch with a legacy-only payload (no projection) is advisory-only.
    binding_id, task_a = _bound_to_issue(conn, 10)
    before = _identity_snapshot(conn)
    legacy_active = dict(legacy, claude_session_id="s1", herdr_tab_id="tab-1", target_ref_number=501)
    result = hook_flows.on_user_prompt_submit(conn, legacy_active)
    assert result["reason_code"] == "different_primary_target_active"
    assert _identity_snapshot(conn) == before

    # eligible=False projection never rebinds even with strong-looking legacy fields.
    legacy_active["active_rebind_primary_eligible"] = False
    assert hook_flows.on_user_prompt_submit(conn, legacy_active)["reason_code"] == "different_primary_target_active"
    assert _identity_snapshot(conn) == before

    # Terminal/absorb entries ignore the ACTIVE projection entirely.
    with db.write_transaction(conn):
        conn.execute("UPDATE activities SET status = 'DONE', ended_at = 'x' WHERE task_id = ?", (task_a,))
    terminal = dict(legacy_active, active_rebind_primary_eligible=False)
    assert hook_flows.on_user_prompt_submit(conn, terminal)["reason_code"] == "terminal_advance_or_rebind"


def test_active_rebind_projection_mismatch_with_legacy_target_yields_advisory_without_mutation(conn):
    binding_id, task_a = _bound_to_issue(conn, 10)
    good = _adapter_payload("Issue #20 を対象にレビューして", session="s1", tab="tab-1")
    assert good["active_rebind_primary_eligible"] is True
    before = _identity_snapshot(conn)

    mutations = {
        "number differs": {"target_ref_number": 21},
        "repo differs": {"target_repo": "other/repo"},
        "kind differs": {"target_ref_kind": "pr"},
        "legacy kind is not EXPLICIT/INFERRED": {"classification_kind": "REFERENCE_ONLY"},
        "projection ref_form invalid": {"active_rebind_ref_form": "weird"},
        "projection number wrong type": {"active_rebind_target_ref_number": "20"},
        "projection repo missing": {"active_rebind_target_repo": None},
    }
    for label, patch in mutations.items():
        payload = dict(good, **patch)
        result = hook_flows.on_user_prompt_submit(conn, payload)
        if label == "legacy kind is not EXPLICIT/INFERRED":
            # REFERENCE_ONLY never reaches the ACTIVE branch at all.
            assert result["reason_code"] == "reference_only_or_none", label
        elif label in ("number differs", "repo differs", "kind differs"):
            # PR/unclaimed shortcuts may trigger first for a changed kind; the
            # contract is only "no mutation".
            assert result["reason_code"] in ("active_rebind_projection_mismatch", "unclaimed_pr_local_only"), label
        else:
            assert result["reason_code"] == "active_rebind_projection_mismatch", label
        assert result["decision"] == "pass"
        assert _identity_snapshot(conn) == before, label
    assert _current(conn, binding_id)[0] == task_a


# ---------------------------------------------------------------------------
# AC20: do not share a Task held by another live managed Binding
# ---------------------------------------------------------------------------


def _two_sessions(conn):
    """s2 (tab-2) works on Task B (Issue #20); s1 (tab-1) works on Task A (Issue #10)."""
    _start(conn, "tab-2", "s2")
    task_b = _prompt(conn, "Issue #20 を対象に作業開始", session="s2", tab="tab-2")["task_id"]
    binding_a, task_a = _bound_to_issue(conn, 10, tab="tab-1", session="s1")
    return binding_a, task_a, task_b


def test_target_task_held_by_other_binding_open_managed_run_blocks_auto_rebind(conn):
    binding_a, task_a, task_b = _two_sessions(conn)
    before = _identity_snapshot(conn)

    result = _prompt(conn, "Issue #20 を対象にレビューして", session="s1", tab="tab-1")

    assert result["decision"] == "pass"
    assert result["reason_code"] == "blocked_by_other_live_binding"
    assert result["advisory"] is True
    assert _identity_snapshot(conn) == before, "current Binding must be kept, nothing mutated"
    assert _current(conn, binding_a)[0] == task_a


def test_target_task_with_only_own_binding_open_run_still_rebinds(conn):
    # Service level: the check excludes the caller's own Binding, ended runs and non-managed runs.
    binding_a, task_a = _bound_to_issue(conn, 10)
    task_b, activity_b = _existing_task_with_claim(conn, "issue", 20, activity_kind="implementation")

    # A DIFFERENT binding's ENDED managed run and a non-managed (subagent) open run do not block.
    other = service.create_binding(conn)
    ended = service.start_execution_run(
        conn, run_kind="native_operator", task_id=task_b, activity_id=activity_b, binding_id=other["id"]
    )
    service.end_execution_run(conn, ended["id"])
    service.start_execution_run(conn, run_kind="subagent", task_id=task_b, activity_id=activity_b, binding_id=None)
    # ... and an open managed run owned by THIS binding is never a blocker.
    own_run = _current(conn, binding_a)[2]
    service.attach_execution_run(conn, own_run, task_id=task_b, activity_id=activity_b)

    result = service.bind_target_to_binding(
        conn,
        binding_id=binding_a,
        execution_run_id=own_run,
        repo=REPO,
        ref_kind="issue",
        ref_number=20,
        reason_code="user_prompt_primary_target_rebind",
        refuse_when_other_binding_holds_task=True,
    )
    assert "blocked_by_other_live_binding" not in result
    assert result["task_id"] == task_b


def test_concurrent_bindings_racing_for_same_target_task_only_one_succeeds(conn, state_root):
    # Two independent Bindings, both ACTIVE on their own Tasks, both prompted
    # to take the same existing Task C at the same moment.
    _start(conn, "tab-1", "s1")
    _prompt(conn, "Issue #10 を対象に作業開始", session="s1", tab="tab-1")
    _start(conn, "tab-2", "s2")
    _prompt(conn, "Issue #11 を対象に作業開始", session="s2", tab="tab-2")
    task_c, _ = _existing_task_with_claim(conn, "issue", 30, activity_kind="implementation")
    payloads = {
        session: _adapter_payload("Issue #30 を対象にレビューして", session=session, tab=tab)
        for session, tab in (("s1", "tab-1"), ("s2", "tab-2"))
    }

    import task_context_config as config

    barrier = threading.Barrier(2)
    results: dict[str, dict] = {}
    failures: list[BaseException] = []

    def _worker(session):
        connection = db.connect(config.db_path())
        try:
            barrier.wait(timeout=10)
            for _attempt in range(50):
                try:
                    results[session] = hook_flows.on_user_prompt_submit(connection, payloads[session])
                    return
                except errors.TemporarilyUnavailableError:
                    continue
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_worker, args=(s,)) for s in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not failures, failures

    reasons = sorted(r["reason_code"] for r in results.values())
    assert reasons == ["blocked_by_other_live_binding", "user_prompt_primary_target_rebind"]
    holders = conn.execute(
        "SELECT DISTINCT binding_id FROM execution_runs WHERE task_id = ? AND ended_at IS NULL "
        "AND run_kind IN ('native_operator','claude_gpt')",
        (task_c,),
    ).fetchall()
    assert len(holders) == 1
