"""
test_preflight_run_with_anchor.py

Tests for the `preflight.run.with_anchor` sibling exact profile added to
command_registry.py (Issue #1498).

Covers AC1, AC2, and Positive/Negative Test Matrix items #1, #9-#14.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import command_registry as reg  # noqa: E402


# ---------------------------------------------------------------------------
# AC1: preflight.run.with_anchor is a sibling exact profile; preflight.run
# itself is byte-for-byte unmodified.
# ---------------------------------------------------------------------------

# Snapshot of the exact `preflight.run` entry as it existed prior to Issue
# #1498. If this entry ever changes, this test must fail loudly (AC1) rather
# than silently pass, since the Issue's core invariant is that `preflight.run`
# is untouched by the sibling-profile addition.
_EXPECTED_PREFLIGHT_RUN_ENTRY = {
    "id": "preflight.run",
    "argv": [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
    ],
    "shell": False,
    "cwd_policy": "repo_root",
    "execution_class": "exact_skill_runtime",
    "required_cwd": "canonical_main_root",
    "required_branch": "default_branch",
    "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
    "network_effect": "github_read_only",
    "stdin_contract": "none",
    "stdout_contract": "refinement_preflight_result/v1",
    "timeout_seconds": 120,
    "mutation": False,
    "placeholders": {
        "issue_number": {"type": "positive_int", "required": True},
        "repo": {"type": "owner_repo", "required": True},
    },
}


def test_registry_sibling_profile_preserves_preflight_run():
    """AC1: `preflight.run.with_anchor` exists as a sibling entry and
    `preflight.run` itself is unchanged (argv/placeholders/execution_class)."""
    assert "preflight.run.with_anchor" in reg.REGISTRY
    assert reg.REGISTRY["preflight.run"] == _EXPECTED_PREFLIGHT_RUN_ENTRY

    anchor_entry = reg.REGISTRY["preflight.run.with_anchor"]
    assert anchor_entry["execution_class"] == "exact_skill_runtime_anchor"
    assert anchor_entry["required_cwd"] == "canonical_main_root"
    assert anchor_entry["required_branch"] == "default_branch"
    assert anchor_entry["network_effect"] == "github_read_only"
    assert anchor_entry["mutation"] is False
    assert anchor_entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/"
    ]
    assert anchor_entry["argv"] == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "{issue_number}",
        "--repo", "{repo}",
        "--anchor-comment-url", "{anchor_comment_url}",
    ]
    assert anchor_entry["placeholders"]["anchor_comment_url"] == {
        "type": "github_issue_comment_url",
        "required": True,
    }


def test_registry_contract_update_phase_is_explicit_and_preflight_remains_read_only():
    """#1877 AC3: the mutation consumer is a distinct registry command."""
    entry = reg.REGISTRY["contract_update.run.with_human_context"]
    assert reg.REGISTRY["preflight.run.with_anchor"]["mutation"] is False
    assert "--consume-contract-patch-plan" not in reg.REGISTRY["preflight.run.with_anchor"]["argv"]
    assert entry["mutation"] is True
    assert entry["main_control_plane_only"] is True
    assert entry["execution_class"] == "exact_skill_runtime_contract_update_anchor"
    assert entry["required_cwd"] == "canonical_main_root"
    assert entry["required_branch"] == "default_branch"
    assert entry["network_effect"] == "github_read_only"
    assert entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        "artifacts/{active_issue}/issue-metadata/",
    ]
    assert entry["argv"][-1] == "--consume-contract-patch-plan"

    url = "https://github.com/squne121/loop-protocol/issues/1877#issuecomment-5143816923"
    assert reg.render_command(
        "contract_update.run.with_human_context",
        {"issue_number": 1877, "repo": "squne121/loop-protocol", "anchor_comment_url": url},
    ) == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "1877", "--repo", "squne121/loop-protocol",
        "--anchor-comment-url", url, "--human-context-comment-url", url,
        "--consume-contract-patch-plan",
    ]


def test_registry_sibling_profile_renders_argv():
    """render_command() produces the expected 10-token argv for
    preflight.run.with_anchor."""
    url = "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"
    argv = reg.render_command(
        "preflight.run.with_anchor",
        {"issue_number": 1492, "repo": "squne121/loop-protocol", "anchor_comment_url": url},
    )
    assert argv == [
        "uv", "run", "python3",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
        "--issue-number", "1492",
        "--repo", "squne121/loop-protocol",
        "--anchor-comment-url", url,
    ]


def test_registry_explicit_origin_profiles_do_not_infer_human_provenance():
    """P0: caller-selected lane, not anchor presence, determines origin."""
    url = "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"
    values = {"issue_number": 1492, "repo": "squne121/loop-protocol", "anchor_comment_url": url}

    generic = reg.render_command("preflight.run.with_anchor", values)
    human = reg.render_command("preflight.run.with_human_context", values)
    agent = reg.render_command("preflight.run.with_agent_report", values)

    assert "--human-context-comment-url" not in generic
    assert human[-2:] == ["--human-context-comment-url", url]
    assert agent[-2:] == ["--agent-report-comment-url", url]
    assert "contract_update.run.with_agent_report" not in reg.REGISTRY


def test_registry_sibling_profile_missing_anchor_raises():
    """render_command() fails closed when the required anchor_comment_url
    placeholder is missing."""
    with pytest.raises(ValueError):
        reg.render_command(
            "preflight.run.with_anchor",
            {"issue_number": 1492, "repo": "squne121/loop-protocol"},
        )


# ---------------------------------------------------------------------------
# AC2: github_issue_comment_url placeholder type — Positive/Negative Test
# Matrix #9-#14.
# ---------------------------------------------------------------------------

_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"

_NEGATIVE_URLS = {
    # Matrix #9: pull request review comment URL, not an issue comment URL.
    "pull_request_review_comment": (
        "https://github.com/squne121/loop-protocol/pull/1492/files#r1234567"
    ),
    # Matrix #10: discussion_r fragment form (PR review comment fragment).
    "discussion_r_fragment": (
        "https://github.com/squne121/loop-protocol/issues/1492#discussion_r1234567"
    ),
    # Matrix #11: query string present.
    "query_string": (
        "https://github.com/squne121/loop-protocol/issues/1492?tab=timeline"
        "#issuecomment-4959671503"
    ),
    # Matrix #12: extra fragment / suffix / trailing slash.
    "trailing_slash": (
        "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503/"
    ),
    "extra_suffix": (
        "https://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503-extra"
    ),
    # Matrix #13: userinfo, port, non-GitHub host, HTTP scheme.
    "userinfo": (
        "https://user:pass@github.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "port": (
        "https://github.com:8443/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "non_github_host": (
        "https://evil.example.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "subdomain_host": (
        "https://gist.github.com/squne121/loop-protocol/issues/1492"
        "#issuecomment-4959671503"
    ),
    "http_scheme": (
        "http://github.com/squne121/loop-protocol/issues/1492#issuecomment-4959671503"
    ),
    # Matrix #14: percent-encoding disguise of canonical shape.
    "percent_encoded_hash": (
        "https://github.com/squne121/loop-protocol/issues/1492%23issuecomment-4959671503"
    ),
    "percent_encoded_dotdot": (
        "https://github.com/squne121/loop-protocol/%2e%2e/issues/1492"
        "#issuecomment-4959671503"
    ),
    # Not a URL at all / empty
    "empty": "",
    "not_a_url": "not-a-url",
}


class TestGithubIssueCommentUrlPlaceholderType:
    def test_valid_url_accepted(self):
        argv = reg.render_command(
            "preflight.run.with_anchor",
            {
                "issue_number": 1492,
                "repo": "squne121/loop-protocol",
                "anchor_comment_url": _VALID_URL,
            },
        )
        assert _VALID_URL in argv

    @pytest.mark.parametrize("name", sorted(_NEGATIVE_URLS.keys()))
    def test_negative_matrix_rejected(self, name: str):
        url = _NEGATIVE_URLS[name]
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/loop-protocol",
                    "anchor_comment_url": url,
                },
            )


def test_github_issue_comment_url_type_rejects_negative_matrix():
    """AC2 entrypoint test referenced by the Issue's Verification Commands."""
    for url in _NEGATIVE_URLS.values():
        with pytest.raises(ValueError):
            reg.render_command(
                "preflight.run.with_anchor",
                {
                    "issue_number": 1492,
                    "repo": "squne121/loop-protocol",
                    "anchor_comment_url": url,
                },
            )


def test_registry_export_is_json_serializable_with_anchor_entry():
    """export_registry() (used by --list) does not choke on the new entry."""
    import json

    data = reg.export_registry()
    assert "preflight.run.with_anchor" in data["commands"]
    json.dumps(data)  # must not raise


def test_registry_entry_is_a_deep_copy_safe_snapshot():
    """Sanity: mutating a returned export dict must not corrupt REGISTRY."""
    data = reg.export_registry()
    mutated = copy.deepcopy(data)
    mutated["commands"]["preflight.run.with_anchor"]["argv"] = ["tampered"]
    assert reg.REGISTRY["preflight.run.with_anchor"]["argv"] != ["tampered"]



# ---------------------------------------------------------------------------
# #2048 Scope Delta: production-path E2E for the approved-scope-reframe /
# empty-operations[] router.
#
# Unlike test_scope_only_reframe_route.py (which drives
# decide_scope_reframe_contract_route() / run_trusted_anchor_iteration_zero()
# directly), these tests exercise the ACTUAL canonical production entry
# points:
#   - the positive/replay cases below call run_preflight() itself (the same
#     function `contract_update.run.with_human_context` invokes via
#     `--consume-contract-patch-plan`), through fixture mode with injected
#     GitHub-mutation callbacks (no live gh calls, no real Issue write).
#   - the negative regression cases call
#     consume_trusted_anchor_contract_patch_plan() directly (the exact
#     production consumer function -- not a copy/reimplementation) with
#     crafted known_context/patch_plan/anchor inputs, since exercising each
#     failure mode through the full planner subprocess boundary would
#     require a distinct planner-triggering fixture per case without adding
#     assurance beyond what the consumer-boundary call already proves.
#
# This directly targets the #2048 PR review blocker (iteration 1/2):
# decide_scope_reframe_contract_route() was reachable only from unit tests,
# never from the real `consume_trusted_anchor_contract_patch_plan()` /
# `run_preflight()` call chain, because `known_context["scope_delta_decision"]`
# was never threaded into `run_trusted_anchor_iteration_zero()`.
# ---------------------------------------------------------------------------

_E2E_SKILL_ROOT = Path(__file__).resolve().parent.parent
_E2E_SCRIPTS_DIR = _E2E_SKILL_ROOT / "scripts"


def _e2e_load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _E2E_SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_e2e_preflight = _e2e_load_module("run_refinement_preflight_2048_e2e", "run_refinement_preflight.py")

_E2E_REPO = "squne121/loop-protocol"
_E2E_ISSUE = 2048
_E2E_URL = f"https://github.com/{_E2E_REPO}/issues/{_E2E_ISSUE}#issuecomment-2048099"
_E2E_DELTA = "docs/product/features/scope-only-reframe.md"


def _e2e_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _e2e_anchor_body(delta: str = _E2E_DELTA) -> str:
    return (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        f"target:\n  repo: {_E2E_REPO}\n  issue_number: {_E2E_ISSUE}\n"
        "decision: approve_scope_delta\n"
        f'allowed_path_deltas: ["{delta}"]\n'
        'rationale: "approved scope reframe for #2048 E2E fixture"\n'
        'required_rerun: ["contract_review"]\n'
        "```\n"
    )


def _e2e_issue_body(*, allowed_paths_includes_delta: bool) -> str:
    allowed_paths_lines = ["- docs/product/features/existing.md"]
    if allowed_paths_includes_delta:
        allowed_paths_lines.append(f"- {_E2E_DELTA}")
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\n"
        "contract_schema_version: v1\n"
        "issue_kind: implementation\n"
        "parent_issue: none\n"
        "goal_ref: test\n"
        "change_kind: workflow\n"
        "```\n\n"
        "## Parent Issue\n\nnone\n\n"
        "## Parent Goal Ref\n\ntest\n\n"
        "## Current Validated Scope\n\n- test\n\n"
        "## Remaining Parent Gaps\n\nnone\n\n"
        "## Outcome\n\ntest\n\n"
        "## In Scope\n\n- test\n\n"
        "## Out of Scope\n\n- none\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: test\n\n"
        "## Verification Commands\n\n```bash\n$ true\n```\n\n"
        "## Allowed Paths\n\n" + "\n".join(allowed_paths_lines) + "\n\n"
        "## Stop Conditions\n\n- none\n\n"
        "## Required Skills\n\n- none\n"
    )


def _e2e_anchor_comment(*, delta: str = _E2E_DELTA, comment_id: int = 2048099) -> dict:
    return {
        "id": comment_id,
        "body": _e2e_anchor_body(delta),
        "issue_url": f"https://api.github.com/repos/{_E2E_REPO}/issues/{_E2E_ISSUE}",
        "created_at": "2026-08-09T00:00:00Z",
        "updated_at": "2026-08-09T00:00:00Z",
        "html_url": _E2E_URL,
        "url": f"https://api.github.com/repos/{_E2E_REPO}/issues/comments/{comment_id}",
        "user": {"login": "squne121", "type": "User"},
        "author_association": "OWNER",
    }


def _e2e_callbacks(*, issue_body: str, anchor_comment: dict):
    calls = {"apply_transaction": 0, "fresh_checks": 0}
    state = {"body": issue_body}

    def fetch_current():
        return (
            {"body": state["body"], "updatedAt": "2026-08-09T00:00:00Z"},
            dict(anchor_comment, html_url=_E2E_URL),
        )

    def candidate_readiness(_body):
        return {
            "status": "go",
            "body_sha256": "sha256:candidate",
            "source_checks": [],
            "errors": [],
            "readiness_result_ref": "fixture",
        }

    def apply_transaction(current_issue, candidate_body, readiness):
        calls["apply_transaction"] += 1
        state["body"] = candidate_body
        return {"status": "applied"}

    def fresh_checks(_current_issue):
        calls["fresh_checks"] += 1
        return {
            "preflight": "unavailable",
            "review": "unavailable",
            "readiness": "unavailable",
            "allowed_paths": "unavailable",
            "permission_profile": "unavailable",
            "runtime_evidence": "unavailable",
        }

    return {
        "fetch_current": fetch_current,
        "candidate_readiness": candidate_readiness,
        "apply_transaction": apply_transaction,
        "fresh_checks": fresh_checks,
    }, calls


def _e2e_run_preflight(tmp_path, *, allowed_paths_includes_delta: bool, run_id: str):
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=allowed_paths_includes_delta)
    anchor_comment = _e2e_anchor_comment()
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": _E2E_ISSUE,
        "repo": _E2E_REPO,
        "now": "2026-08-09T00:00:00Z",
        "issue": {"number": _E2E_ISSUE, "title": "test", "body": issue_body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [_E2E_URL],
        "anchor_comments": [anchor_comment],
    }
    fixture_path = tmp_path / f"preflight_{run_id}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    callbacks, calls = _e2e_callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    artifact_dir = (
        _E2E_SKILL_ROOT.parent.parent / "artifacts" / "issue-refinement-loop" / str(_E2E_ISSUE)
    )
    try:
        result, exit_code = _e2e_preflight.run_preflight(
            issue_number=_E2E_ISSUE,
            repo=_E2E_REPO,
            anchor_comment_urls=[_E2E_URL],
            fixture_path=fixture_path,
            consume_contract_patch_plan=True,
            contract_update_callbacks=callbacks,
        )
    finally:
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
    return result, exit_code, calls


def test_e2e_full_rewrite_required_reaches_next_action_via_canonical_consumer(tmp_path, capsys):
    """AC1/AC6 positive fixture: an approved trusted-anchor scope reframe
    (non-empty allowed_path_deltas) not yet reflected in the current Issue
    body, with no section-bound operations[] derivable, reaches
    NEXT_ACTION: issue_editor_required on stdout AND result["next_action"]
    from the REAL run_preflight() -> consume_trusted_anchor_contract_patch_plan()
    call chain -- not a unit-level shortcut."""
    result, exit_code, calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="positive"
    )
    captured = capsys.readouterr()

    assert "NEXT_ACTION: issue_editor_required" in captured.out
    assert result["next_action"] == "issue_editor_required"
    # AC2: no contract-update mutation was ever attempted.
    assert calls["apply_transaction"] == 0
    assert result["contract_update"]["writes"] == 0
    # AC3: no separate scope-reframe comment publisher exists in this call
    # chain at all -- there is nothing to assert a count of 0 against beyond
    # the absence of any comment-posting callback, which this fixture never
    # supplies.
    assert result["contract_update"]["status"] != "no_change"
    # AC4: the existing _bounded_contract_update_handoff() post-update
    # 6-gate (preflight/review/readiness/allowed_paths/permission_profile/
    # runtime_evidence) is still exercised for this disposition -- fresh_checks
    # is invoked exactly once, the same reused gate #1877 already established,
    # not a duplicate routing plane.
    assert calls["fresh_checks"] == 1


def test_e2e_full_rewrite_required_replay_is_deterministic_and_side_effect_free(tmp_path):
    """AC2/AC3/regression #7: replaying the identical disposition (same
    anchor, same unreflected body) through run_preflight() twice never
    attempts a mutation and always reaches the same route."""
    first_result, _, first_calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="replay_a"
    )
    second_result, _, second_calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=False, run_id="replay_b"
    )

    assert first_result["next_action"] == "issue_editor_required"
    assert second_result["next_action"] == "issue_editor_required"
    assert first_calls["apply_transaction"] == 0
    assert second_calls["apply_transaction"] == 0


def test_e2e_deltas_already_reflected_is_proven_no_change_not_issue_editor_required(tmp_path):
    """Regression #3: when the approved allowed_path_deltas are ALREADY
    present in the current Issue body's Allowed Paths section, this is
    `proven_no_change` -- never promoted to issue_editor_required."""
    result, _exit_code, calls = _e2e_run_preflight(
        tmp_path, allowed_paths_includes_delta=True, run_id="already_reflected"
    )

    assert result["next_action"] != "issue_editor_required"
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# Negative regressions #1-#6: direct calls to the production consumer
# function consume_trusted_anchor_contract_patch_plan() (not a
# reimplementation) with crafted known_context / patch_plan / anchor
# identity inputs.
# ---------------------------------------------------------------------------


def _e2e_consumer_kwargs(*, patch_plan: dict, known_context: "dict | None", issue_body: str):
    anchor_comment = _e2e_anchor_comment()
    anchor_body = anchor_comment["body"]
    callbacks, calls = _e2e_callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    return (
        dict(
            repo=_E2E_REPO,
            issue_number=_E2E_ISSUE,
            issue={"body": issue_body, "updatedAt": "2026-08-09T00:00:00Z"},
            anchor_url=_E2E_URL,
            anchor_payload=anchor_comment,
            anchor_body=anchor_body,
            contract_patch_plan=patch_plan,
            callbacks=callbacks,
            known_context=known_context,
        ),
        calls,
        anchor_body,
    )


def _e2e_approved_scope_delta_decision(*, anchor_body: str, delta: str = _E2E_DELTA) -> dict:
    return {
        "status": "approved_by_trusted_anchor",
        "implementation_go": False,
        "anchor_author_association": "OWNER",
        "anchor_comment_url": _E2E_URL,
        "anchor_comment_hash": _e2e_sha256(anchor_body),
        "allowed_path_deltas": [delta],
        "required_rerun": ["contract_review"],
    }


def test_negative_1_non_empty_operations_preserves_ordinary_contract_update_behavior():
    """Regression #1: non-empty operations[] still take the ordinary
    section-bound apply_transaction path, unaffected by the #2048 wiring."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    operations = [
        {
            "section": "Allowed Paths",
            "op": "append",
            "text": f"- `{_E2E_DELTA}`",
            "rationale": "test",
            "source_evidence_index": 0,
        }
    ]
    patch_plan = {
        "schema_version": "CONTRACT_PATCH_PLAN_V1",
        "target_issue_number": _E2E_ISSUE,
        "base_issue_body_sha256": _e2e_sha256(issue_body),
        "source_evidence": [],
        "operations": operations,
        "forbidden": ["direct_github_write", "implementation_phase_transition"],
        "required_next_step": "rerun_refinement_after_contract_update",
    }
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "applied"
    assert calls["apply_transaction"] == 1
    assert "rewrite_route" not in result


def test_negative_2_unapproved_scope_reframe_does_not_escalate():
    """Regression #2: scope_delta_decision.status != approved_by_trusted_anchor
    -- empty operations[] stays an ordinary no_change, never
    issue_editor_required."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "status": "fail_closed",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_3_already_reflected_deltas_are_proven_no_change():
    """Regression #3 (consumer-boundary level): allowed_path_deltas already
    present in the current body -- proven_no_change, not escalated."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=True)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_4_malformed_operations_type_fails_closed_without_coercion():
    """Regression #4: operations is not a list -- fails closed immediately,
    never coerced to []; run_trusted_anchor_iteration_zero is never reached."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": "not-a-list"}
    known_context = {
        "scope_delta_decision": _e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"])
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "blocked"
    assert result["writes"] == 0
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0


def test_negative_5_malformed_allowed_path_deltas_fails_closed():
    """Regression #5: an explicit EMPTY allowed_path_deltas list is not a
    scope-reframe signal at all (consistent with
    `classify_scope_reframe_disposition()`'s own
    `bool(normalized_deltas)` gate) -- fails closed to the ordinary
    no_change path, never escalated to issue_editor_required, and never
    silently promoted to `full_rewrite_required`. See
    `test_negative_5b_non_list_allowed_path_deltas_is_invalid` for the
    genuinely malformed (non-list) case, which IS `invalid`."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "allowed_path_deltas": [],
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "no_change"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_5b_non_list_allowed_path_deltas_is_invalid():
    """Regression #5 (PR #2057 OWNER review P0-1): a NON-LIST
    allowed_path_deltas (genuinely malformed, distinct from an explicit
    empty list) is a DISTINCT `invalid` disposition/status -- never
    silently collapsed into the same `no_change` observable as a
    genuinely satisfied `proven_no_change`."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "allowed_path_deltas": "not-a-list",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_6_anchor_identity_binding_drift_fails_closed():
    """Regression #6 (revised, PR #2057 OWNER review P0-1):
    scope_delta_decision.anchor_comment_url does not match THIS consumer
    call's anchor_url (a stale/different decision) -- a distinct `invalid`
    outcome, never silently the same as an ordinary no-op."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "anchor_comment_url": _E2E_URL + "-different",
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


def test_negative_6b_anchor_body_hash_drift_fails_closed():
    """Regression #6 (body hash variant, revised): scope_delta_decision.
    anchor_comment_hash does not match sha256(this call's anchor_body) --
    a distinct `invalid` outcome (TOCTOU-adjacent: the anchor was edited
    after the decision was computed)."""
    issue_body = _e2e_issue_body(allowed_paths_includes_delta=False)
    anchor_comment = _e2e_anchor_comment()
    patch_plan = {"operations": []}
    known_context = {
        "scope_delta_decision": {
            **_e2e_approved_scope_delta_decision(anchor_body=anchor_comment["body"]),
            "anchor_comment_hash": "0" * 64,
        }
    }
    kwargs, calls, _ = _e2e_consumer_kwargs(
        patch_plan=patch_plan, known_context=known_context, issue_body=issue_body
    )
    result = _e2e_preflight.consume_trusted_anchor_contract_patch_plan(**kwargs)

    assert result["status"] == "invalid"
    assert result["disposition"]["disposition"] == "invalid"
    assert "rewrite_route" not in result
    assert calls["apply_transaction"] == 0


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (P1-5): tri-state Allowed Paths reflected
# check -- false-positive resistance regression coverage for
# `_check_scope_reframe_deltas_reflected()` (formerly `_scope_reframe_
# deltas_already_reflected()`, which had a `normalized_delta in
# current_body` whole-body substring fallback that this function removes).
# ---------------------------------------------------------------------------


def _reflected_issue_body(allowed_paths_lines: list[str], *, extra_sections: str = "") -> str:
    return (
        "## Machine-Readable Contract\n\n"
        "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n"
        "parent_issue: none\ngoal_ref: test\nchange_kind: workflow\n```\n\n"
        "## Outcome\n\n" + extra_sections + "\n\n"
        "## Allowed Paths\n\n" + "\n".join(allowed_paths_lines) + "\n\n"
        "## Stop Conditions\n\n- none\n"
    )


class TestScopeReframeDeltasReflectedFalsePositiveResistance:
    def test_delta_only_in_outcome_prose_is_absent_not_present(self):
        """A delta literal appearing only in `## Outcome` prose (not the
        canonical `## Allowed Paths` section) must NOT be treated as
        reflected."""
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md"],
            extra_sections="この Issue は docs/product/features/scope-only-reframe.md を扱う。",
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "absent"

    def test_delta_only_in_fenced_code_is_absent_not_present(self):
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md"],
            extra_sections="```text\ndocs/product/features/scope-only-reframe.md\n```",
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "absent"

    def test_delta_as_prefix_of_a_longer_unrelated_path_is_absent(self):
        """`docs/foo` must not be matched by `docs/foobar` appearing in the
        Allowed Paths section."""
        body = _reflected_issue_body(["- docs/foobar.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=["docs/foo.md"]
        )
        assert status == "absent"

    def test_delta_exactly_present_in_allowed_paths_section_is_present(self):
        body = _reflected_issue_body(
            ["- docs/product/features/existing.md", "- docs/product/features/scope-only-reframe.md"]
        )
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body,
            allowed_path_deltas=["docs/product/features/scope-only-reframe.md"],
        )
        assert status == "present"

    def test_whitespace_only_delta_entry_is_absent_never_present(self):
        """A whitespace-only delta normalizes to an empty string; it must
        never match (the pre-fix bug: an empty string is a substring of
        every body, so this always "matched")."""
        body = _reflected_issue_body(["- docs/product/features/existing.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=["   "]
        )
        assert status == "absent"

    def test_empty_deltas_list_is_invalid_or_unavailable(self):
        body = _reflected_issue_body(["- docs/product/features/existing.md"])
        status = _e2e_preflight._check_scope_reframe_deltas_reflected(
            current_body=body, allowed_path_deltas=[]
        )
        assert status == "invalid_or_unavailable"
