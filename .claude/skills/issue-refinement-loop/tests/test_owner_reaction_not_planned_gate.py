"""
test_owner_reaction_not_planned_gate.py

Issue #2689: wires `owner_reaction_decision.py`'s (Issue #1975)
`OWNER_REACTION_DECISION_RESULT_V1.selected` output into
`run_refinement_preflight.py`'s pre-existing heavy mutation gate
(`_classify_heavy_mutation_gate()` / `_is_approved_close_not_planned_decision()`,
Issue #1891 / PR #2478), limited to the ONLY category that gate already gives
exact meaning to: `close_not_planned` bound to `mutation_category ==
"not_planned"`.

AC coverage:
  AC1: `_is_approved_owner_reaction_not_planned_decision()` exists as an
       INDEPENDENT predicate (rg-verified by the Issue's own Verification
       Command; this module additionally exercises its behavior).
  AC2: same `close_not_planned` operation, different target -- rejected
       (identity-binding contract mirrors
       `test_preview_binding_primary_identity` in
       `test_owner_reaction_decision.py`).
  AC3: `unresolved` / `stale` / `environment_error` statuses, and every
       heavy mutation category other than `not_planned`, stay fail-closed.
  AC4: the heavy mutation gate uses ONLY the same-invocation fresh
       subprocess result -- never a cached/prior one.
  AC5: the new predicate never converts `selected` into
       `approved_by_trusted_anchor`, and never calls/mutates the
       pre-existing `_is_approved_close_not_planned_decision()`.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import run_refinement_preflight as preflight  # noqa: E402

REPO = "squne121/loop-protocol"
TARGET_ISSUE = 2689


def _selected_decision(*, target: str, operation: str = "close_not_planned") -> dict:
    return {
        "schema": "OWNER_REACTION_DECISION_RESULT_V1",
        "status": "selected",
        "reason_code": None,
        "selected_option_id": "close_target",
        "selected_option_metadata": {"operation": operation, "target": target},
    }


def _non_selected_decision(status: str, reason_code: str = "some_reason") -> dict:
    return {
        "schema": "OWNER_REACTION_DECISION_RESULT_V1",
        "status": status,
        "reason_code": reason_code,
        "selected_option_id": None,
        "selected_option_metadata": None,
    }


# ---------------------------------------------------------------------------
# AC1: independent predicate, exact status/operation/target binding
# ---------------------------------------------------------------------------


def test_predicate_true_only_for_selected_close_not_planned_exact_target():
    decision = _selected_decision(target=f"#{TARGET_ISSUE}")
    assert preflight._is_approved_owner_reaction_not_planned_decision(
        decision, target_issue_number=TARGET_ISSUE
    )


def test_predicate_false_when_operation_is_not_close_not_planned():
    decision = _selected_decision(target=f"#{TARGET_ISSUE}", operation="close")
    assert not preflight._is_approved_owner_reaction_not_planned_decision(
        decision, target_issue_number=TARGET_ISSUE
    )


def test_predicate_false_for_none_decision():
    assert not preflight._is_approved_owner_reaction_not_planned_decision(
        None, target_issue_number=TARGET_ISSUE
    )


def test_predicate_false_for_missing_target_issue_number():
    decision = _selected_decision(target=f"#{TARGET_ISSUE}")
    assert not preflight._is_approved_owner_reaction_not_planned_decision(
        decision, target_issue_number=None
    )


# ---------------------------------------------------------------------------
# AC2: same category, different target -- rejected (identity-binding)
# ---------------------------------------------------------------------------


def test_same_category_different_target_rejected():
    # Mirrors test_preview_binding_primary_identity's own fixture shape:
    # two options, SAME operation ("close_not_planned"), DIFFERENT targets.
    decision_for_other_issue = _selected_decision(target="#100")

    assert not preflight._is_approved_owner_reaction_not_planned_decision(
        decision_for_other_issue, target_issue_number=TARGET_ISSUE
    )

    gate = preflight._classify_heavy_mutation_gate(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_decision=decision_for_other_issue,
        target_issue_number=TARGET_ISSUE,
    )
    assert gate["status"] == "blocked"
    assert gate["fail_closed"] is True

    # The SAME decision correctly approves when bound to ITS OWN target.
    assert preflight._is_approved_owner_reaction_not_planned_decision(
        decision_for_other_issue, target_issue_number=100
    )


# ---------------------------------------------------------------------------
# AC3: unresolved / stale / environment_error / unsupported category ->
# fail-closed
# ---------------------------------------------------------------------------


def test_unresolved_stale_environment_error_and_unsupported_category_fail_closed():
    for status in ("unresolved", "stale", "environment_error"):
        decision = _non_selected_decision(status)
        assert not preflight._is_approved_owner_reaction_not_planned_decision(
            decision, target_issue_number=TARGET_ISSUE
        )
        gate = preflight._classify_heavy_mutation_gate(
            mutation_category="not_planned",
            scope_delta_decision=None,
            owner_reaction_decision=decision,
            target_issue_number=TARGET_ISSUE,
        )
        assert gate["status"] == "blocked", status
        assert gate["fail_closed"] is True, status

    # A `selected` + `close_not_planned` + exact-target decision must still
    # fail-closed for every OTHER heavy mutation category -- #2689 wires
    # ONLY `not_planned`.
    approved_decision = _selected_decision(target=f"#{TARGET_ISSUE}")
    for other_category in (
        "close",
        "replacement_issue_creation",
        "dependency_removal",
        "parent_child_change",
    ):
        gate = preflight._classify_heavy_mutation_gate(
            mutation_category=other_category,
            scope_delta_decision=None,
            owner_reaction_decision=approved_decision,
            target_issue_number=TARGET_ISSUE,
        )
        assert gate["status"] == "blocked", other_category
        assert gate["fail_closed"] is True, other_category


# ---------------------------------------------------------------------------
# AC4: fresh subprocess -- gate uses ONLY the same-invocation result
# ---------------------------------------------------------------------------


class _FakeRunnerResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def test_gate_uses_fresh_invocation_result_only():
    import json as _json

    call_log: list[list[str]] = []
    responses = [
        _json.dumps(_non_selected_decision("unresolved")),
        _json.dumps(_selected_decision(target=f"#{TARGET_ISSUE}")),
    ]

    def fake_runner(argv, **kwargs):
        call_log.append(list(argv))
        return _FakeRunnerResult(responses[len(call_log) - 1])

    owner_reaction_context = {
        "owner_user_id": 999,
        "preview_binding_file": "preview_binding.json",
    }

    gate_1 = preflight._classify_heavy_mutation_gate_with_fresh_owner_reaction(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_context=owner_reaction_context,
        repo=REPO,
        issue_number=TARGET_ISSUE,
        subprocess_runner=fake_runner,
    )
    assert len(call_log) == 1
    assert gate_1["status"] == "blocked"

    gate_2 = preflight._classify_heavy_mutation_gate_with_fresh_owner_reaction(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_context=owner_reaction_context,
        repo=REPO,
        issue_number=TARGET_ISSUE,
        subprocess_runner=fake_runner,
    )
    # A SECOND fresh subprocess call was made -- never a cached/reused
    # result from the first invocation.
    assert len(call_log) == 2
    assert gate_2["status"] == "allowed"
    assert gate_2["reason"] == "owner_reaction_not_planned_decision_present"

    # Same canonical argv shape on every fresh call (repo / issue_number /
    # owner_user_id / preview_binding_file), never diverging between calls.
    for argv in call_log:
        assert "--repo" in argv and REPO in argv
        assert "--issue-number" in argv and str(TARGET_ISSUE) in argv
        assert "--owner-user-id" in argv and "999" in argv
        assert "--preview-binding-file" in argv and "preview_binding.json" in argv


def test_no_subprocess_issued_when_owner_reaction_context_absent():
    call_log: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        call_log.append(list(argv))
        return _FakeRunnerResult("{}")

    gate = preflight._classify_heavy_mutation_gate_with_fresh_owner_reaction(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_context=None,
        repo=REPO,
        issue_number=TARGET_ISSUE,
        subprocess_runner=fake_runner,
    )
    assert call_log == []
    assert gate["status"] == "blocked"
    assert gate["fail_closed"] is True


def test_no_subprocess_issued_for_non_not_planned_category():
    call_log: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        call_log.append(list(argv))
        return _FakeRunnerResult("{}")

    gate = preflight._classify_heavy_mutation_gate_with_fresh_owner_reaction(
        mutation_category="close",
        scope_delta_decision=None,
        owner_reaction_context={
            "owner_user_id": 999,
            "preview_binding_file": "preview_binding.json",
        },
        repo=REPO,
        issue_number=TARGET_ISSUE,
        subprocess_runner=fake_runner,
    )
    assert call_log == []
    assert gate["status"] == "blocked"
    assert gate["fail_closed"] is True


def test_transport_failure_is_fail_closed_none():
    def raising_runner(argv, **kwargs):
        raise OSError("boom")

    gate = preflight._classify_heavy_mutation_gate_with_fresh_owner_reaction(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_context={
            "owner_user_id": 999,
            "preview_binding_file": "preview_binding.json",
        },
        repo=REPO,
        issue_number=TARGET_ISSUE,
        subprocess_runner=raising_runner,
    )
    assert gate["status"] == "blocked"
    assert gate["fail_closed"] is True


# ---------------------------------------------------------------------------
# AC5: never converts `selected` into `approved_by_trusted_anchor`, never
# calls/mutates the pre-existing trusted-anchor predicate.
# ---------------------------------------------------------------------------


def test_new_predicate_does_not_convert_to_approved_by_trusted_anchor():
    decision = _selected_decision(target=f"#{TARGET_ISSUE}")
    original_decision = dict(decision)

    result = preflight._is_approved_owner_reaction_not_planned_decision(
        decision, target_issue_number=TARGET_ISSUE
    )
    assert result is True

    # The predicate never mutates its input.
    assert decision == original_decision
    assert decision["status"] == "selected"
    assert decision.get("status") != "approved_by_trusted_anchor"

    # The pre-existing trusted-anchor predicate must independently reject
    # this owner-reaction-shaped decision (it lacks
    # authorized_mutation_category / anchor_author_association /
    # implementation_go entirely) -- proving the two predicates are not
    # aliases of one another.
    assert not preflight._is_approved_close_not_planned_decision(decision)

    # Static source-level check (#2689 AC5 "コードレベルで示されている"):
    # the new predicate's own EXECUTABLE body (docstring excluded -- the
    # docstring intentionally documents the relationship in prose) never
    # calls the trusted-anchor predicate and never references the
    # "approved_by_trusted_anchor" literal.
    tree = ast.parse(
        inspect.getsource(preflight._is_approved_owner_reaction_not_planned_decision)
    )
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)
    body_without_docstring = func_node.body[1:] if ast.get_docstring(func_node) else func_node.body
    body_source = "\n".join(ast.unparse(stmt) for stmt in body_without_docstring)
    assert "_is_approved_close_not_planned_decision" not in body_source
    assert "approved_by_trusted_anchor" not in body_source


def test_existing_trusted_anchor_predicate_unchanged():
    # #2689 Out of Scope: `_is_approved_close_not_planned_decision()` itself
    # must not be touched by this Issue.
    decision = {
        "status": "approved_by_trusted_anchor",
        "decision": "close_not_planned",
        "authorized_mutation_category": "not_planned",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
    }
    assert preflight._is_approved_close_not_planned_decision(decision)
