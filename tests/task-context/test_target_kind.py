"""Issue #2566 -- pure `target_kind` resolution + guard decision core
(`task_context_target_kind.py`). GIVEN/WHEN/THEN, no DB, no I/O."""

from __future__ import annotations

import task_context_target_kind as target_kind


# ---------------------------------------------------------------------------
# classify_send_message_target (AC2, AC4)
# ---------------------------------------------------------------------------


def test_given_empty_to_when_classifying_send_message_target_then_unaddressed_broadcast():
    kind = target_kind.classify_send_message_target(
        to=None,
        is_in_session_subagent=False,
        peer_session_found=False,
        peer_task_id=None,
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_UNADDRESSED_BROADCAST


def test_given_star_to_when_classifying_send_message_target_then_unaddressed_broadcast():
    kind = target_kind.classify_send_message_target(
        to="*",
        is_in_session_subagent=False,
        peer_session_found=False,
        peer_task_id=None,
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_UNADDRESSED_BROADCAST


def test_given_open_subagent_match_when_classifying_send_message_target_then_in_session_subagent():
    kind = target_kind.classify_send_message_target(
        to="agent-123",
        is_in_session_subagent=True,
        peer_session_found=False,
        peer_task_id=None,
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_IN_SESSION_SUBAGENT


def test_given_unresolvable_peer_when_classifying_send_message_target_then_unknown_independent_session():
    kind = target_kind.classify_send_message_target(
        to="some-peer",
        is_in_session_subagent=False,
        peer_session_found=False,
        peer_task_id=None,
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_UNKNOWN_SESSION


def test_given_same_task_peer_when_classifying_send_message_target_then_same_task_independent_session():
    kind = target_kind.classify_send_message_target(
        to="peer-session",
        is_in_session_subagent=False,
        peer_session_found=True,
        peer_task_id="task_a",
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_SAME_TASK_SESSION


def test_given_both_taskless_peer_when_classifying_send_message_target_then_same_task_independent_session():
    kind = target_kind.classify_send_message_target(
        to="peer-session",
        is_in_session_subagent=False,
        peer_session_found=True,
        peer_task_id=None,
        caller_task_id=None,
    )
    assert kind == target_kind.TARGET_KIND_SAME_TASK_SESSION


def test_given_cross_task_peer_when_classifying_send_message_target_then_known_cross_task_independent_session():
    kind = target_kind.classify_send_message_target(
        to="peer-session",
        is_in_session_subagent=False,
        peer_session_found=True,
        peer_task_id="task_b",
        caller_task_id="task_a",
    )
    assert kind == target_kind.TARGET_KIND_CROSS_TASK_SESSION


# ---------------------------------------------------------------------------
# classify_herdr_target (AC5)
# ---------------------------------------------------------------------------


def test_given_machine_scoped_when_classifying_herdr_target_then_unknown_independent_session():
    kind = target_kind.classify_herdr_target(
        machine_scoped=True, locator_resolved=True, peer_task_id="task_a", caller_task_id="task_a"
    )
    assert kind == target_kind.TARGET_KIND_UNKNOWN_SESSION


def test_given_unresolved_locator_when_classifying_herdr_target_then_unknown_independent_session():
    kind = target_kind.classify_herdr_target(
        machine_scoped=False, locator_resolved=False, peer_task_id=None, caller_task_id="task_a"
    )
    assert kind == target_kind.TARGET_KIND_UNKNOWN_SESSION


def test_given_same_task_locator_when_classifying_herdr_target_then_same_task_independent_session():
    kind = target_kind.classify_herdr_target(
        machine_scoped=False, locator_resolved=True, peer_task_id="task_a", caller_task_id="task_a"
    )
    assert kind == target_kind.TARGET_KIND_SAME_TASK_SESSION


def test_given_cross_task_locator_when_classifying_herdr_target_then_known_cross_task_independent_session():
    kind = target_kind.classify_herdr_target(
        machine_scoped=False, locator_resolved=True, peer_task_id="task_b", caller_task_id="task_a"
    )
    assert kind == target_kind.TARGET_KIND_CROSS_TASK_SESSION


# ---------------------------------------------------------------------------
# decision_for_target_kind
# ---------------------------------------------------------------------------


def test_given_pass_target_kinds_when_resolving_decision_then_pass():
    for kind in (
        target_kind.TARGET_KIND_IN_SESSION_SUBAGENT,
        target_kind.TARGET_KIND_SAME_TASK_SESSION,
        target_kind.TARGET_KIND_UNADDRESSED_BROADCAST,
    ):
        decision, reason_code = target_kind.decision_for_target_kind(kind)
        assert decision == target_kind.DECISION_PASS
        assert reason_code == f"target_kind_{kind}"


def test_given_ask_target_kinds_when_resolving_decision_then_ask():
    for kind in (target_kind.TARGET_KIND_CROSS_TASK_SESSION, target_kind.TARGET_KIND_UNKNOWN_SESSION):
        decision, reason_code = target_kind.decision_for_target_kind(kind)
        assert decision == target_kind.DECISION_ASK
        assert reason_code == f"target_kind_{kind}"
