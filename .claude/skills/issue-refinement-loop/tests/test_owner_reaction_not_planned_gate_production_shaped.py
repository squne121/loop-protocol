"""
test_owner_reaction_not_planned_gate_production_shaped.py

Issue #2689 AC6 (runtime-verification): production-shaped test proving a
single producer -> consumer path:

  producer: a REAL `scripts/agent-guards/skill_runtime_exec.py` subprocess
            dispatches the (already production-wired, Issue #2688 / PR
            #2694) `owner_reaction.decide.fixture` command_id through the
            REAL `command_registry.py` entry into the REAL
            `owner_reaction_decision.py` CLI (network-boundary-only faked,
            via the SAME fake-`gh` fixture #2694 established --
            `owner_reaction_dispatch_fixture.py`, reused verbatim here, not
            reimplemented).

  consumer: `run_refinement_preflight.py`'s own
            `_classify_heavy_mutation_gate()` /
            `_is_approved_owner_reaction_not_planned_decision()` (#2689),
            fed with the REAL subprocess's own stdout payload (never a
            hand-crafted dict) -- this is the "producer output reaches the
            heavy mutation gate" path AC6 requires.

Per the Issue's `fallback_policy`: a hand-crafted `known_context`-only unit
test would NOT satisfy AC6 on its own -- this module's PASS depends on the
REAL `owner_reaction.decide.fixture` subprocess dispatch above.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))

from owner_reaction_dispatch_fixture import (  # noqa: E402
    TRUSTED_REPO_SLUG,
    install_fixture,
    make_repo,
    run_executor,
    write_gh_state,
    write_preview_binding,
)

_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import run_refinement_preflight as preflight  # noqa: E402

ISSUE_NUMBER = 2689
OWNER_USER_ID = 5150
COMMENT_ID = 9001
ANCHOR_COMMENT_ID = 9002
COMMENT_BODY = "owner reacts to the not_planned option here\n"
ANCHOR_BODY = "not_planned option anchor body\n"
ISSUE_BODY = "issue body snapshot for #2689\n"
REACTION_OPTION_MAP = {"+1": "close_not_planned_option"}
OPTIONS = {
    "close_not_planned_option": {
        "operation": "close_not_planned",
        "target": f"#{ISSUE_NUMBER}",
    }
}
REACTIONS = [{"id": 1, "content": "+1", "user": {"id": OWNER_USER_ID, "login": "owner"}}]


def _artifact_dir(repo: Path) -> Path:
    return repo / ".claude" / "artifacts" / "issue-refinement-loop" / str(ISSUE_NUMBER)


def test_selected_not_planned_reaches_gate_via_real_subprocess(tmp_path):
    repo = make_repo(tmp_path)
    install_fixture(repo, tmp_path / "trusted-gh-bin")

    binding_path = _artifact_dir(repo) / "preview_binding.json"
    write_preview_binding(
        binding_path,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reaction_option_map=REACTION_OPTION_MAP,
        options=OPTIONS,
    )
    preview_binding_rel = str(binding_path.relative_to(repo))

    gh_fixture_path = _artifact_dir(repo) / "gh_fixture.json"
    write_gh_state(
        gh_fixture_path,
        repo=TRUSTED_REPO_SLUG,
        issue_number=ISSUE_NUMBER,
        owner_user_id=OWNER_USER_ID,
        comment_id=COMMENT_ID,
        comment_body=COMMENT_BODY,
        anchor_comment_id=ANCHOR_COMMENT_ID,
        anchor_body=ANCHOR_BODY,
        issue_body=ISSUE_BODY,
        reactions=REACTIONS,
    )
    gh_fixture_rel = str(gh_fixture_path.relative_to(repo))

    # --- producer: REAL skill_runtime_exec.py subprocess (Issue #2688 /
    # PR #2694 production wiring), dispatching the REAL owner_reaction_decision.py
    # CLI through the REAL command_registry.py entry.
    result = run_executor(
        repo,
        [
            "--command-id", "owner_reaction.decide.fixture",
            "--issue-number", str(ISSUE_NUMBER),
            "--repo", TRUSTED_REPO_SLUG,
            "--owner-user-id", str(OWNER_USER_ID),
            "--preview-binding-file", preview_binding_rel,
            "--gh-fixture-file", gh_fixture_rel,
        ],
        extra_env={"LOOP_ISSUE_NUMBER": str(ISSUE_NUMBER)},
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)

    producer_payload = json.loads(result.stdout)
    assert producer_payload["schema"] == "OWNER_REACTION_DECISION_RESULT_V1", producer_payload
    assert producer_payload["status"] == "selected", producer_payload
    assert producer_payload["selected_option_metadata"] == OPTIONS["close_not_planned_option"]

    # --- consumer: run_refinement_preflight.py's own predicate/gate,
    # fed with the REAL producer payload above (never a hand-crafted dict).
    approved = preflight._is_approved_owner_reaction_not_planned_decision(
        producer_payload, target_issue_number=ISSUE_NUMBER
    )
    assert approved is True

    gate = preflight._classify_heavy_mutation_gate(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_decision=producer_payload,
        target_issue_number=ISSUE_NUMBER,
    )
    assert gate["status"] == "allowed", gate
    assert gate["fail_closed"] is False
    assert gate["reason"] == "owner_reaction_not_planned_decision_present"

    # A different target Issue must NOT reach "allowed" from the SAME
    # producer payload (#2689 AC2 identity binding, re-verified here against
    # the real producer output rather than a hand-crafted dict).
    other_gate = preflight._classify_heavy_mutation_gate(
        mutation_category="not_planned",
        scope_delta_decision=None,
        owner_reaction_decision=producer_payload,
        target_issue_number=ISSUE_NUMBER + 1,
    )
    assert other_gate["status"] == "blocked"
    assert other_gate["fail_closed"] is True

    # The pre-existing trusted-anchor gate never fires here (#2689 AC5:
    # this producer payload never satisfies
    # _is_approved_close_not_planned_decision()).
    assert not preflight._is_approved_close_not_planned_decision(producer_payload)
