"""
test_owner_reaction_decision.py

Mocked-fixture regression tests for `owner_reaction_decision.py` (Issue
#1975, parent #1950).

AC coverage:
  AC1: preview binding / primary identity
  AC2: stable user id principal resolver
  AC3: pagination full-retrieval correctness (4 fail-closed sub-cases)
  AC4: selection semantics disambiguation
  AC5: untrusted / unmapped reaction exclusion
  AC6: drift / stale detection
  AC7: registry-based canonical invocation (production-shaped, real CLI
       subprocess, GitHub network boundary faked only) -- runtime
       verification AC, see `docs/dev/runtime-verification-policy.md`.

PR #2683 fix_delta coverage (OWNER adversarial review comment
#5748651887, applied against Issue #1975's fixed Allowed Paths):
  P1-1: anchor comment binding / drift readback
  P1-2: TOCTOU -- final readback happens AFTER reaction retrieval
  P1-3: target/anchor comment must belong to the given Issue (issue_url)
  P2-1: repository owner principal resolved from `GET repos/{repo}`,
        never trusted from the caller-supplied `--owner-user-id` alone
  P2-2: AC7 production entry (not just `.fixture` sibling) end-to-end
  P2-3: outer/inner timeout budget consistency (see `command_registry.py`
        and `owner_reaction_decision.DEFAULT_GH_TIMEOUT`)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import owner_reaction_decision as m  # noqa: E402
import command_registry as reg  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[4]


# ---------------------------------------------------------------------------
# Shared fixture-building helpers
# ---------------------------------------------------------------------------


def _reaction(*, reaction_id: int, content: str, user_id: int, login: str = "someone") -> dict:
    return {"id": reaction_id, "content": content, "user": {"id": user_id, "login": login}}


_DEFAULT_REPO = "squne121/loop-protocol"
_DEFAULT_ANCHOR_COMMENT_ID = 556


def _binding(
    *,
    comment_body: str = "preview body",
    issue_body: str = "issue body",
    anchor_body: str = "anchor preview body",
    reaction_option_map=None,
    options=None,
) -> dict:
    reaction_option_map = reaction_option_map if reaction_option_map is not None else {
        "+1": "option_a",
        "eyes": "option_b",
    }
    options = options if options is not None else {
        "option_a": {"operation": "close_not_planned", "target": "#1950"},
        "option_b": {"operation": "parent_child_change", "target": "#1951"},
    }
    return {
        "comment_id": 555,
        "comment_body_hash": m.sha256_hex(comment_body),
        "issue_snapshot_hash": m.sha256_hex(issue_body),
        # PR #2683 fix_delta P1-1: anchor comment binding/readback.
        "anchor_comment_id": _DEFAULT_ANCHOR_COMMENT_ID,
        "anchor_comment_body_hash": m.sha256_hex(anchor_body),
        "reaction_option_map": reaction_option_map,
        "options": options,
    }


def _fake_runner(
    *,
    comment_body="preview body",
    issue_body="issue body",
    anchor_body="anchor preview body",
    repo_owner_id=999,
    repo=_DEFAULT_REPO,
    issue_number=1975,
    anchor_comment_id=_DEFAULT_ANCHOR_COMMENT_ID,
    comment_issue_number=None,
    anchor_issue_number=None,
    reactions_pages=None,
    reactions_result: "m.GhInvocationResult | None" = None,
):
    """Build a gh_runner covering: repo owner resolution (P2-1), the
    target/anchor comment + Issue readbacks (drift + issue_url binding
    checks, P1-1/P1-2/P1-3) trivially succeeding at the given bodies, and
    the reactions fetch controlled either by `reactions_pages` (a
    list-of-lists to JSON-encode as a normal success) or an explicit
    `reactions_result` override (used by the AC3 fail-closed sub-cases)."""
    comment_issue_number = comment_issue_number if comment_issue_number is not None else issue_number
    anchor_issue_number = anchor_issue_number if anchor_issue_number is not None else issue_number

    def _runner(argv, *, timeout):  # noqa: ARG001
        kind = m._classify_gh_call(argv, anchor_comment_id=anchor_comment_id)
        if kind == "repo":
            return m.GhInvocationResult(0, json.dumps({"owner": {"id": repo_owner_id}}), "")
        if kind == "comment":
            return m.GhInvocationResult(
                0,
                json.dumps(
                    {
                        "body": comment_body,
                        "issue_url": f"https://api.github.com/repos/{repo}/issues/{comment_issue_number}",
                    }
                ),
                "",
            )
        if kind == "anchor":
            return m.GhInvocationResult(
                0,
                json.dumps(
                    {
                        "body": anchor_body,
                        "issue_url": f"https://api.github.com/repos/{repo}/issues/{anchor_issue_number}",
                    }
                ),
                "",
            )
        if kind == "issue":
            return m.GhInvocationResult(0, json.dumps({"body": issue_body}), "")
        if kind == "reactions":
            if reactions_result is not None:
                return reactions_result
            return m.GhInvocationResult(0, json.dumps(reactions_pages or [[]]), "")
        raise AssertionError(f"unexpected gh call kind: {kind} argv={argv}")

    return _runner


# ---------------------------------------------------------------------------
# AC1: preview binding / primary identity
# ---------------------------------------------------------------------------


def test_preview_binding_primary_identity():
    binding = _binding(
        reaction_option_map={"+1": "close_target_a", "eyes": "close_target_b"},
        options={
            "close_target_a": {"operation": "close_not_planned", "target": "#100"},
            "close_target_b": {"operation": "close_not_planned", "target": "#200"},
        },
    )
    runner = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]])

    result = m.decide(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=999,
        preview_binding=binding,
        gh_runner=runner,
    )

    # Primary identity: selected_option_id + preview identity (comment id /
    # comment body hash), NOT the shared operation.
    assert result["status"] == "selected"
    assert result["selected_option_id"] == "close_target_a"
    assert result["preview_identity"] == {"comment_id": 555, "comment_body_hash": binding["comment_body_hash"]}
    # Same mutation category ("close_not_planned"), different target -- the
    # two options must never collapse into one.
    assert result["selected_option_metadata"]["target"] == "#100"
    assert result["selected_option_metadata"]["operation"] == "close_not_planned"

    # Reacting with the OTHER option (different target, same category)
    # resolves to the OTHER option_id -- proving they are not treated as
    # identical operations.
    runner_b = _fake_runner(reactions_pages=[[_reaction(reaction_id=2, content="eyes", user_id=999)]])
    result_b = m.decide(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=999,
        preview_binding=binding,
        gh_runner=runner_b,
    )
    assert result_b["selected_option_id"] == "close_target_b"
    assert result_b["selected_option_metadata"]["target"] == "#200"


def test_preview_binding_invalid_rejects_reserved_reject_all_key():
    binding = _binding(reaction_option_map={"-1": "should_not_be_mappable"})
    with pytest.raises(m.PreviewBindingInvalid):
        m.validate_preview_binding(binding)


def test_preview_binding_invalid_dangling_option_id():
    binding = _binding(
        reaction_option_map={"+1": "option_missing"},
        options={"option_a": {"operation": "close_not_planned", "target": "#1"}},
    )
    with pytest.raises(m.PreviewBindingInvalid):
        m.validate_preview_binding(binding)


# ---------------------------------------------------------------------------
# AC2: stable user id principal resolver
# ---------------------------------------------------------------------------


def test_stable_user_id_principal_resolution():
    binding = _binding()

    # Owner's login changed between preview time and readback time, but the
    # stable id is unchanged -- still counted as the owner.
    runner = _fake_runner(
        repo_owner_id=42,
        reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=42, login="new-login-name")]],
    )
    result = m.decide(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=42,
        preview_binding=binding,
        gh_runner=runner,
    )
    assert result["status"] == "selected"
    assert result["selected_option_id"] == "option_a"

    # A different stable user id, even with the SAME login string the real
    # owner used to have, must never count as an owner reaction.
    runner_untrusted = _fake_runner(
        repo_owner_id=42,
        reactions_pages=[[_reaction(reaction_id=2, content="+1", user_id=99999, login="new-login-name")]],
    )
    result_untrusted = m.decide(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=42,
        preview_binding=binding,
        gh_runner=runner_untrusted,
    )
    assert result_untrusted["status"] == "unresolved"
    assert result_untrusted["reason_code"] == "unanswered"
    assert result_untrusted["selected_option_id"] is None


# ---------------------------------------------------------------------------
# AC3: pagination full-retrieval correctness (4 fail-closed sub-cases)
#
# A single module-level test function (the exact VC node id the Issue
# specifies) exercises all 4 sub-cases -- each sub-case is still its own
# independent decide() call + assertion block, just not a separate pytest
# node id.
# ---------------------------------------------------------------------------


def test_pagination_partial_failure_fail_closed():
    binding = _binding()

    # (a) page 1 has owner +1, a later page has owner eyes, but the overall
    # `gh api --paginate` subprocess call fails (nonzero exit) -- even
    # though the partial stdout WOULD parse. Must fail-closed, never return
    # a selection.
    partial_stdout = json.dumps([[_reaction(reaction_id=1, content="+1", user_id=999)]])
    runner_a = _fake_runner(
        reactions_result=m.GhInvocationResult(returncode=1, stdout=partial_stdout, stderr="later page fetch failed")
    )
    result_a = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_a,
    )
    assert result_a["status"] == "environment_error"
    assert result_a["reason_code"] == "reactions_fetch_failed:subprocess_nonzero_exit"
    assert result_a["selected_option_id"] is None

    # (b) parseable partial JSON present on stdout, but the subprocess
    # itself exited non-zero -- must fail-closed regardless of stdout
    # content (this module never even attempts json.loads in this branch).
    runner_b = _fake_runner(
        reactions_result=m.GhInvocationResult(
            returncode=2, stdout='[[{"id": 1, "content": "+1", "user": {"id": 999}}]]', stderr="boom"
        )
    )
    result_b = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_b,
    )
    assert result_b["status"] == "environment_error"
    assert result_b["reason_code"] == "reactions_fetch_failed:subprocess_nonzero_exit"
    assert result_b["selected_option_id"] is None

    # (c) subprocess exits 0, JSON parses, but one page's shape is not a
    # JSON array (e.g. a bare object instead of a list of records) --
    # all-pages-fetched is NOT sufficient for a selection.
    malformed_pages = [[_reaction(reaction_id=1, content="+1", user_id=999)], {"not": "a list"}]
    runner_c = _fake_runner(reactions_result=m.GhInvocationResult(0, json.dumps(malformed_pages), ""))
    result_c = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_c,
    )
    assert result_c["status"] == "environment_error"
    assert result_c["reason_code"] == "reaction_page_shape_invalid"
    assert result_c["selected_option_id"] is None

    # (d) every page has valid shape, but a reaction record is missing a
    # required field (here: `user.id`) -- reaction record integrity
    # validation failure.
    malformed_record = {"id": 1, "content": "+1", "user": {"login": "owner-no-id"}}
    runner_d = _fake_runner(reactions_pages=[[malformed_record]])
    result_d = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_d,
    )
    assert result_d["status"] == "environment_error"
    assert result_d["reason_code"] == "reaction_record_integrity_failure"
    assert result_d["selected_option_id"] is None


# ---------------------------------------------------------------------------
# AC4: selection semantics disambiguation (single module-level VC node id,
# 5 independent sub-cases).
# ---------------------------------------------------------------------------


def test_selection_semantics_disambiguation():
    binding = _binding()

    # unanswered: owner has zero valid reactions.
    runner_unanswered = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="heart", user_id=1)]])
    result_unanswered = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_unanswered,
    )
    assert result_unanswered["status"] == "unresolved"
    assert result_unanswered["reason_code"] == "unanswered"
    assert result_unanswered["selected_option_id"] is None

    # -1 only: reject-all, reproposal needed.
    runner_reject_all = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="-1", user_id=999)]])
    result_reject_all = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_reject_all,
    )
    assert result_reject_all["status"] == "unresolved"
    assert result_reject_all["reason_code"] == "reject_all"
    assert result_reject_all["selected_option_id"] is None

    # exactly one mapped reaction type: selection established.
    runner_selected = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]])
    result_selected = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_selected,
    )
    assert result_selected["status"] == "selected"
    assert result_selected["selected_option_id"] == "option_a"

    # multiple valid mapped reaction types: conflict, not adopted.
    runner_conflict = _fake_runner(
        reactions_pages=[[
            _reaction(reaction_id=1, content="+1", user_id=999),
            _reaction(reaction_id=2, content="eyes", user_id=999),
        ]]
    )
    result_conflict = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_conflict,
    )
    assert result_conflict["status"] == "unresolved"
    assert result_conflict["reason_code"] == "conflict"
    assert result_conflict["selected_option_id"] is None

    # unmapped-only: never silently converted into a selection.
    runner_unmapped = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="rocket", user_id=999)]])
    result_unmapped = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner_unmapped,
    )
    assert result_unmapped["status"] == "unresolved"
    assert result_unmapped["reason_code"] == "no_selection"
    assert result_unmapped["selected_option_id"] is None


# ---------------------------------------------------------------------------
# AC5: untrusted / unmapped reaction exclusion
# ---------------------------------------------------------------------------


def test_untrusted_and_unmapped_reaction_excluded():
    binding = _binding()
    # A large volume of non-owner reactions, ALL mapped content types, plus
    # some unmapped reactions from the owner -- none of it should ever
    # produce a selection.
    non_owner_reactions = [
        _reaction(reaction_id=i, content=content, user_id=1000 + i, login=f"user{i}")
        for i, content in enumerate(["+1", "eyes", "+1", "eyes", "+1"] * 20)
    ]
    owner_unmapped = [_reaction(reaction_id=9999, content="laugh", user_id=999)]
    runner = _fake_runner(reactions_pages=[non_owner_reactions + owner_unmapped])

    result = m.decide(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=999,
        preview_binding=binding,
        gh_runner=runner,
    )
    assert result["status"] == "unresolved"
    assert result["reason_code"] == "no_selection"
    assert result["selected_option_id"] is None
    assert result["fetched_reaction_count"] == len(non_owner_reactions) + 1


# ---------------------------------------------------------------------------
# AC6: drift / stale detection (single module-level VC node id, 3
# independent sub-cases).
# ---------------------------------------------------------------------------


def test_drift_detection_returns_stale():
    # Comment body drifted: current body differs from the preview
    # binding's comment_body_hash.
    binding_comment = _binding(comment_body="original preview text")
    runner_comment = _fake_runner(comment_body="EDITED preview text", issue_body="issue body")
    result_comment = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding_comment, gh_runner=runner_comment,
    )
    assert result_comment["status"] == "stale"
    assert result_comment["reason_code"] == "preview_drifted"
    assert result_comment["drift"] == {
        "comment_body_drifted": True,
        "issue_body_drifted": False,
        "anchor_body_drifted": False,
    }
    assert result_comment["selected_option_id"] is None

    # Issue snapshot drifted: current issue body differs from
    # issue_snapshot_hash.
    binding_issue = _binding(issue_body="original issue snapshot")
    runner_issue = _fake_runner(comment_body="preview body", issue_body="EDITED issue snapshot")
    result_issue = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding_issue, gh_runner=runner_issue,
    )
    assert result_issue["status"] == "stale"
    assert result_issue["drift"] == {
        "comment_body_drifted": False,
        "issue_body_drifted": True,
        "anchor_body_drifted": False,
    }
    assert result_issue["selected_option_id"] is None

    # PR #2683 fix_delta P1-1: anchor comment body drifted -- the anchor
    # comment carries the options the owner is reacting to, so an edit to
    # it must invalidate the selection just like the target comment/Issue.
    binding_anchor = _binding(anchor_body="original anchor text")
    runner_anchor = _fake_runner(anchor_body="EDITED anchor text")
    result_anchor = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding_anchor, gh_runner=runner_anchor,
    )
    assert result_anchor["status"] == "stale"
    assert result_anchor["reason_code"] == "preview_drifted"
    assert result_anchor["drift"] == {
        "comment_body_drifted": False,
        "issue_body_drifted": False,
        "anchor_body_drifted": True,
    }
    assert result_anchor["selected_option_id"] is None

    # No drift: proceeds through to a real selection.
    binding_ok = _binding()
    runner_ok = _fake_runner(reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]])
    result_ok = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding_ok, gh_runner=runner_ok,
    )
    assert result_ok["status"] == "selected"
    assert result_ok["drift"] == {
        "comment_body_drifted": False,
        "issue_body_drifted": False,
        "anchor_body_drifted": False,
    }


# ---------------------------------------------------------------------------
# PR #2683 fix_delta P1-2: TOCTOU -- the readback that determines staleness
# must happen AFTER reactions are fully fetched, not only before.
# ---------------------------------------------------------------------------


def test_toctou_final_readback_happens_after_reactions_fetch():
    """A comment edit that races with (or immediately follows) reaction
    pagination must still be caught: the drift readback must be performed
    strictly AFTER the reactions fetch completes, not only before it."""
    binding = _binding()
    state = {"reactions_fetched": False}

    def runner(argv, *, timeout):  # noqa: ARG001
        kind = m._classify_gh_call(argv, anchor_comment_id=binding["anchor_comment_id"])
        if kind == "repo":
            return m.GhInvocationResult(0, json.dumps({"owner": {"id": 999}}), "")
        if kind == "reactions":
            state["reactions_fetched"] = True
            return m.GhInvocationResult(
                0, json.dumps([[_reaction(reaction_id=1, content="+1", user_id=999)]]), ""
            )
        if kind == "comment":
            # The body only "changes" (drifts) once reactions have already
            # been fetched -- simulates an edit racing with pagination.
            body = "EDITED during reaction pagination" if state["reactions_fetched"] else "preview body"
            return m.GhInvocationResult(
                0,
                json.dumps(
                    {"body": body, "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1975"}
                ),
                "",
            )
        if kind == "anchor":
            return m.GhInvocationResult(
                0,
                json.dumps(
                    {
                        "body": "anchor preview body",
                        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1975",
                    }
                ),
                "",
            )
        if kind == "issue":
            return m.GhInvocationResult(0, json.dumps({"body": "issue body"}), "")
        raise AssertionError(f"unexpected gh call kind: {kind} argv={argv}")

    result = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner,
    )
    # If the drift readback happened BEFORE the reactions fetch (the old,
    # vulnerable ordering), `state["reactions_fetched"]` would still be
    # False at readback time, the body would read as unchanged, and this
    # would incorrectly resolve to "selected" instead of "stale".
    assert state["reactions_fetched"] is True
    assert result["status"] == "stale"
    assert result["reason_code"] == "preview_drifted"
    assert result["drift"]["comment_body_drifted"] is True
    assert result["selected_option_id"] is None


# ---------------------------------------------------------------------------
# PR #2683 fix_delta P1-3: target/anchor comment must belong to the given
# Issue (issue_url binding check).
# ---------------------------------------------------------------------------


def test_comment_issue_url_mismatch_is_rejected():
    """A `comment_id` that resolves to a comment on a DIFFERENT Issue must
    never be silently accepted as the target comment."""
    binding = _binding()
    runner = _fake_runner(
        reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]],
        comment_issue_number=9999,
    )
    result = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner,
    )
    assert result["status"] == "environment_error"
    assert result["reason_code"] == "comment_issue_url_mismatch"
    assert result["selected_option_id"] is None


def test_anchor_comment_issue_url_mismatch_is_rejected():
    """The anchor comment must also belong to the given Issue -- the same
    identity-binding risk applies to it as to the target comment."""
    binding = _binding()
    runner = _fake_runner(
        reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]],
        anchor_issue_number=9999,
    )
    result = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner,
    )
    assert result["status"] == "environment_error"
    assert result["reason_code"] == "anchor_comment_issue_url_mismatch"
    assert result["selected_option_id"] is None


# ---------------------------------------------------------------------------
# PR #2683 fix_delta P2-1: repository owner principal resolved from
# `GET repos/{repo}`, never trusted from `--owner-user-id` alone.
# ---------------------------------------------------------------------------


def test_owner_user_id_mismatch_is_rejected():
    """A caller-supplied `--owner-user-id` that does not match the
    repository's own authoritative owner id must never be silently
    trusted."""
    binding = _binding()
    runner = _fake_runner(
        repo_owner_id=111,
        reactions_pages=[[_reaction(reaction_id=1, content="+1", user_id=999)]],
    )
    result = m.decide(
        repo="squne121/loop-protocol", issue_number=1975, owner_user_id=999,
        preview_binding=binding, gh_runner=runner,
    )
    assert result["status"] == "environment_error"
    assert result["reason_code"] == "owner_user_id_mismatch"
    assert result["selected_option_id"] is None


# ---------------------------------------------------------------------------
# AC7: registry-based canonical invocation (production-shaped, runtime
# verification). GitHub network boundary faked via `--gh-fixture-file`
# only; registry rendering + real CLI subprocess launch + result read are
# all real.
# ---------------------------------------------------------------------------


def _write_evidence_log(*, verdict: str, exit_code: int, reason: str, argv: list, extra: dict) -> Path:
    artifact_dir = _REPO_ROOT / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = artifact_dir / f"runtime-verification-AC7-{timestamp}.log"
    lines = [
        "=== Runtime Verification Log ===",
        "AC: AC7 (Issue #1975) - owner_reaction.decide registry-based canonical invocation",
        f"Timestamp: {timestamp}",
        f"Environment: python={sys.version.split()[0]} platform={sys.platform}",
        "",
        "--- Input ---",
        f"argv: {argv!r}",
        "",
        "--- Output ---",
        json.dumps(extra, ensure_ascii=False, indent=2)[:20000],
        "",
        "--- Verdict ---",
        f"Result: {verdict}",
        f"Exit Code: {exit_code}",
        f"Reason: {reason}",
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _ac7_fixture_payload(
    *,
    repo: str,
    issue_number: int,
    owner_user_id: int,
    comment_body: str,
    issue_body: str,
    anchor_body: str,
) -> tuple[dict, dict]:
    """Build a matched (preview_binding, gh_fixture) pair covering the full
    P1-1/P1-2/P1-3/P2-1 request sequence (repo owner / reactions / comment
    readback / anchor readback / issue readback), reusable by both the
    `.fixture` sibling test and the production-entry test (P2-2)."""
    preview_binding = _binding(comment_body=comment_body, issue_body=issue_body, anchor_body=anchor_body)
    anchor_comment_id = preview_binding["anchor_comment_id"]
    issue_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    gh_fixture = {
        "repo": {"returncode": 0, "stdout": json.dumps({"owner": {"id": owner_user_id}}), "stderr": ""},
        "comment": {
            "returncode": 0,
            "stdout": json.dumps({"body": comment_body, "issue_url": issue_url}),
            "stderr": "",
        },
        "anchor": {
            "returncode": 0,
            "stdout": json.dumps({"body": anchor_body, "issue_url": issue_url}),
            "stderr": "",
        },
        "issue": {"returncode": 0, "stdout": json.dumps({"body": issue_body}), "stderr": ""},
        "reactions": {
            "returncode": 0,
            "stdout": json.dumps([[_reaction(reaction_id=1, content="+1", user_id=owner_user_id)]]),
            "stderr": "",
        },
        "anchor_comment_id": anchor_comment_id,
    }
    return preview_binding, gh_fixture


def _assert_ac7_payload_ok(payload: dict, *, ac_label: str) -> None:
    # Runtime-verification-policy fallback rule: any `_*_fallback: true`
    # field anywhere in the result must be treated as FAIL, never PASS.
    fallback_hit = any(
        key.startswith("_") and key.endswith("_fallback") and value is True
        for key, value in payload.items()
        if isinstance(key, str)
    )
    assert not fallback_hit, f"[{ac_label}] fallback field detected in result, must not PASS: {payload!r}"
    assert payload.get("schema") == "OWNER_REACTION_DECISION_RESULT_V1"
    assert payload.get("status") == "selected"
    assert payload.get("selected_option_id") == "option_a"


def test_canonical_command_registry_entry_end_to_end():
    uv_path = shutil.which("uv")
    if uv_path is None:
        print("SKIP: uv CLI unavailable in PATH -- cannot launch the real owner_reaction_decision.py subprocess")
        pytest.exit("SKIP: uv not found", returncode=77)

    fixture_dir = _REPO_ROOT / "artifacts" / "test_owner_reaction_decision_ac7"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    preview_binding, gh_fixture = _ac7_fixture_payload(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=4242,
        comment_body="AC7 canonical preview comment body",
        issue_body="AC7 canonical issue snapshot body",
        anchor_body="AC7 canonical anchor comment body",
    )

    preview_binding_file = fixture_dir / "preview_binding.json"
    preview_binding_file.write_text(json.dumps(preview_binding), encoding="utf-8")

    gh_fixture_file = fixture_dir / "gh_fixture.json"
    gh_fixture_file.write_text(json.dumps(gh_fixture), encoding="utf-8")

    # Registry entry rendering -- do NOT hand-build argv bypassing
    # render_command().
    argv = reg.render_command(
        "owner_reaction.decide.fixture",
        {
            "repo": "squne121/loop-protocol",
            "issue_number": 1975,
            "owner_user_id": 4242,
            "preview_binding_file": str(preview_binding_file.relative_to(_REPO_ROOT)),
            "gh_fixture_file": str(gh_fixture_file.relative_to(_REPO_ROOT)),
        },
    )
    assert argv[:3] == ["uv", "run", "python3"]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"SKIP: real CLI subprocess launch failed for environment reasons: {exc}")
        pytest.exit(f"SKIP: subprocess launch failed: {exc}", returncode=77)

    if proc.returncode != 0:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=proc.returncode,
            reason="owner_reaction_decision.py exited non-zero",
            argv=argv,
            extra={"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
        )
        pytest.fail(f"owner_reaction_decision.py exited {proc.returncode}: stderr={proc.stderr[-1000:]!r}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=proc.returncode,
            reason="stdout was not valid JSON",
            argv=argv,
            extra={"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
        )
        pytest.fail(f"owner_reaction_decision.py stdout was not valid JSON: {proc.stdout[-500:]!r}")

    verdict_ok = True
    try:
        _assert_ac7_payload_ok(payload, ac_label="AC7.fixture")
    except AssertionError:
        verdict_ok = False
        raise
    finally:
        _write_evidence_log(
            verdict="PASS" if verdict_ok else "FAIL",
            exit_code=proc.returncode,
            reason=(
                "registry render_command() -> real subprocess launch -> root read "
                "of structured OWNER_REACTION_DECISION_RESULT_V1 succeeded"
                if verdict_ok
                else f"unexpected payload: {payload!r}"
            ),
            argv=argv,
            extra=payload,
        )


# ---------------------------------------------------------------------------
# PR #2683 fix_delta P2-2: the PREVIOUS AC7 test only ever drove the
# `.fixture` sibling entry (which has a `--gh-fixture-file` escape hatch
# baked into its own argv). This test drives the PRODUCTION
# `owner_reaction.decide` entry itself (no `--gh-fixture-file` flag exists
# on that entry) by putting a fake `gh` executable first on PATH -- the
# ONLY boundary faked is the GitHub network call; registry rendering,
# real CLI subprocess launch, and root's read of the structured result are
# all real, exactly like the `.fixture` sibling test above.
# `skill_runtime_exec.py` generic dispatch wiring is still never exercised
# (this test calls `render_command()` + `subprocess.run()` directly, same
# as the `.fixture` test).
# ---------------------------------------------------------------------------


_FAKE_GH_SCRIPT_TEMPLATE = """#!/usr/bin/env python3
import json
import re
import sys

FIXTURE = json.loads(open({fixture_path!r}, "r", encoding="utf-8").read())


def _classify(argv):
    endpoint = next((tok for tok in argv if tok.startswith("repos/")), "")
    if "/reactions" in endpoint:
        return "reactions"
    match = re.search(r"/issues/comments/(\\d+)(\\?.*)?$", endpoint)
    if match:
        anchor_id = FIXTURE.get("anchor_comment_id")
        if anchor_id is not None and int(match.group(1)) == anchor_id:
            return "anchor"
        return "comment"
    if re.search(r"/issues/\\d+(\\?.*)?$", endpoint):
        return "issue"
    if re.fullmatch(r"repos/[^/]+/[^/]+", endpoint):
        return "repo"
    return "unknown"


kind = _classify(sys.argv[1:])
entry = FIXTURE.get(kind)
if not isinstance(entry, dict):
    sys.stderr.write("no_fixture_for_kind:" + kind + "\\n")
    sys.exit(1)
sys.stdout.write(str(entry.get("stdout", "")))
sys.stderr.write(str(entry.get("stderr", "")))
sys.exit(int(entry.get("returncode", 0)))
"""


def test_canonical_command_registry_production_entry_end_to_end():
    uv_path = shutil.which("uv")
    if uv_path is None:
        print("SKIP: uv CLI unavailable in PATH -- cannot launch the real owner_reaction_decision.py subprocess")
        pytest.exit("SKIP: uv not found", returncode=77)

    fixture_dir = _REPO_ROOT / "artifacts" / "test_owner_reaction_decision_ac7_production"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    preview_binding, gh_fixture = _ac7_fixture_payload(
        repo="squne121/loop-protocol",
        issue_number=1975,
        owner_user_id=4343,
        comment_body="AC7 production-entry preview comment body",
        issue_body="AC7 production-entry issue snapshot body",
        anchor_body="AC7 production-entry anchor comment body",
    )

    preview_binding_file = fixture_dir / "preview_binding.json"
    preview_binding_file.write_text(json.dumps(preview_binding), encoding="utf-8")

    gh_fixture_file = fixture_dir / "gh_fixture.json"
    gh_fixture_file.write_text(json.dumps(gh_fixture), encoding="utf-8")

    # Fake `gh` executable: the ONLY GitHub network boundary faked. It
    # reads the SAME gh_fixture.json this test wrote (embedded at write
    # time so the script is self-contained -- no wrapper process needed),
    # and its own argv (`gh api -X GET <endpoint> ...`) is classified with
    # the same endpoint-shape logic `owner_reaction_decision._classify_gh_call`
    # uses internally, independently reimplemented here so that this test
    # does not silently pass by construction if the module's own
    # classifier were ever broken. `gh` must be the literal executable
    # name found on PATH (the registry argv invokes literal `"gh"`).
    fake_gh_dir = fixture_dir / "fake_bin"
    fake_gh_dir.mkdir(parents=True, exist_ok=True)
    fake_gh_path = fake_gh_dir / "gh"
    fake_gh_path.write_text(
        _FAKE_GH_SCRIPT_TEMPLATE.format(fixture_path=str(gh_fixture_file)), encoding="utf-8"
    )
    fake_gh_path.chmod(0o755)

    # Registry entry rendering against the PRODUCTION entry -- no
    # `--gh-fixture-file` placeholder exists on this entry's argv.
    argv = reg.render_command(
        "owner_reaction.decide",
        {
            "repo": "squne121/loop-protocol",
            "issue_number": 1975,
            "owner_user_id": 4343,
            "preview_binding_file": str(preview_binding_file.relative_to(_REPO_ROOT)),
        },
    )
    assert argv[:3] == ["uv", "run", "python3"]
    assert "--gh-fixture-file" not in argv

    env = dict(os.environ)
    env["PATH"] = f"{fake_gh_dir}{os.pathsep}{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            argv,
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"SKIP: real CLI subprocess launch failed for environment reasons: {exc}")
        pytest.exit(f"SKIP: subprocess launch failed: {exc}", returncode=77)

    if proc.returncode != 0:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=proc.returncode,
            reason="owner_reaction_decision.py (production entry) exited non-zero",
            argv=argv,
            extra={"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
        )
        pytest.fail(f"owner_reaction_decision.py exited {proc.returncode}: stderr={proc.stderr[-1000:]!r}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _write_evidence_log(
            verdict="FAIL",
            exit_code=proc.returncode,
            reason="stdout was not valid JSON",
            argv=argv,
            extra={"stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]},
        )
        pytest.fail(f"owner_reaction_decision.py stdout was not valid JSON: {proc.stdout[-500:]!r}")

    verdict_ok = True
    try:
        _assert_ac7_payload_ok(payload, ac_label="AC7.production")
    except AssertionError:
        verdict_ok = False
        raise
    finally:
        _write_evidence_log(
            verdict="PASS" if verdict_ok else "FAIL",
            exit_code=proc.returncode,
            reason=(
                "PRODUCTION owner_reaction.decide registry render_command() -> "
                "real subprocess launch (fake `gh` on PATH only) -> root read "
                "of structured OWNER_REACTION_DECISION_RESULT_V1 succeeded"
                if verdict_ok
                else f"unexpected payload: {payload!r}"
            ),
            argv=argv,
            extra=payload,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
