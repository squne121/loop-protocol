"""
test_investigation_evidence_transport_boundary_flags.py

Issue #2678 AC7: wiring `investigation_evidence_transport_path` /
`investigation_evidence_primary_root` into the mutation-phase
`contract_update.run.with_human_context` lane must not relax
`destructive_or_non_idempotent_operation` / `changes_permission_boundary` /
`changes_external_service_boundary` / `requires_issue_split` -- only
`expands_allowed_paths` is ever cleared by a validated, bound
investigation-derived path-literal set
(`scope_signal_delta._has_investigation_derived_allowed_path_literals`).

Reuses the production `scope_signal_delta.classify_scope_delta_authority()`
classifier directly (the SAME function `test_operator_selected_scope_
reframe.py::test_investigation_derived_path_literals_do_not_clear_
destructive_boundary_ac5` already exercises for the READ-ONLY
`preflight.run.with_human_context` lane) and additionally proves the SAME
non-relaxation end-to-end through the real `run_preflight()` ->
`consume_trusted_anchor_contract_patch_plan()` call chain for the
MUTATION-phase lane this Issue actually wires -- a VALID (non-rejected)
transport still yields zero writes / zero mutation-callback invocations for
a destructive-boundary directive, no new scope classifier or schema.

#2678 P1-1/P1-2 fix_delta (PR #2684 review):
  - The end-to-end test below `monkeypatch`es `preflight._find_repo_root`
    to a throwaway, per-test git checkout under pytest's own `tmp_path`
    instead of writing/`shutil.rmtree()`-ing the real repo checkout's
    `.claude/artifacts/issue-refinement-loop/<N>/` (mirrors the same fix in
    `test_investigation_evidence_transport_mutation_guard.py` -- see that
    module's own docstring for the full rationale).
  - `test_split_permission_and_external_service_boundary_flags_not_relaxed`
    (renamed from `test_permission_and_external_service_boundary_flags_
    not_relaxed`) now ALSO covers `requires_issue_split` -- AC7 names 4
    non-relaxed boundaries but the original suite only exercised 3 of them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

sda = importlib.import_module("scope_signal_delta")


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_module("run_refinement_preflight_2678_boundary", "run_refinement_preflight.py")
import command_registry as registry  # noqa: E402

_REPO = "squne121/loop-protocol"
_ISSUE = 267801
_URL = f"https://github.com/{_REPO}/issues/{_ISSUE}#issuecomment-1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _make_isolated_repo_root(tmp_path: Path) -> Path:
    """A throwaway, per-test git checkout under pytest's own `tmp_path` --
    see `test_investigation_evidence_transport_mutation_guard.py`'s own
    `_make_isolated_repo_root()` docstring for the full rationale."""
    root = tmp_path / "isolated-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True, env=_GIT_ENV)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(root), check=True, capture_output=True, env=_GIT_ENV)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(root), check=True, capture_output=True, env=_GIT_ENV)
    return root


# ---------------------------------------------------------------------------
# Static registry/policy non-relaxation: the new placeholder pair is a
# closed, exact two-item set -- never a generic arbitrary-flag passthrough.
# ---------------------------------------------------------------------------


def test_contract_update_with_human_context_registry_entry_adds_only_the_two_known_placeholders():
    entry = registry.REGISTRY["contract_update.run.with_human_context"]
    new_placeholders = {"investigation_evidence_transport_path", "investigation_evidence_primary_root"}
    assert new_placeholders <= set(entry["placeholders"])
    # No OTHER new placeholder was introduced alongside these two.
    unexpected = set(entry["placeholders"]) - {
        "issue_number",
        "repo",
        "anchor_comment_url",
        *new_placeholders,
    }
    assert unexpected == set(), unexpected
    # The mutation/permission-scope-relevant registry declarations this
    # Issue's Out of Scope forbids relaxing are unchanged.
    assert entry["mutation"] is True
    assert entry["main_control_plane_only"] is True
    assert entry["allowed_write_roots"] == [
        ".claude/artifacts/issue-refinement-loop/{active_issue}/",
        "artifacts/{active_issue}/issue-metadata/",
    ]


# ---------------------------------------------------------------------------
# Classifier-level non-relaxation (mirrors test_operator_selected_scope_
# reframe.py's own AC5 coverage for the read-only lane; reused here to prove
# the SAME production function, unmodified by this Issue).
# ---------------------------------------------------------------------------


def test_destructive_and_permission_boundary_flags_not_relaxed():
    """AC7: a directive carrying BOTH `expands_allowed_paths` and
    `destructive_or_non_idempotent_operation` stays `human_escalation` on
    the DESTRUCTIVE reason code -- investigation-derived literals clear
    `expands_allowed_paths` only, never the destructive boundary."""
    body = "\n".join(
        [
            "この破壊的な force push 作業は allowed paths を必要に応じて拡張してください。",
            "- impl-review-loop も合わせて直してください。",
        ]
    )
    payload = {"id": 1, "author_association": "OWNER", "user": {"login": "owner", "type": "User"}}
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=payload,
        comment_body=body,
        repo=_REPO,
        issue_number=_ISSUE,
        anchor_url=_URL,
        captured_at="2026-08-01T00:00:00Z",
        human_context_comment_urls=[_URL],
        agent_report_comment_urls=None,
    )
    assert set(evidence["boundary_flags"]) == {
        "expands_allowed_paths",
        "destructive_or_non_idempotent_operation",
    }

    result = sda.classify_scope_delta_authority(
        evidence,
        triggered=True,
        target_issue_number=_ISSUE,
        expected_repo=_REPO,
        base_issue_body_sha256="sha256:x",
        investigation_derived_path_literals=[".claude/skills/impl-review-loop/SKILL.md"],
    )
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "destructive_or_non_idempotent_operation"


def test_split_permission_and_external_service_boundary_flags_not_relaxed():
    """AC7 (sibling boundaries): `requires_issue_split`,
    `changes_permission_boundary`, and `changes_external_service_boundary`
    are equally never cleared by investigation-derived literals -- the
    positive `requires_issue_split` case (#2678 P1-2 fix_delta) was
    previously missing even though this module's own docstring already
    claimed coverage for all 4 non-relaxed boundaries."""
    split_body = "\n".join(
        [
            "複数の Issue に split into separate issues する必要があるほどスコープが大きいので、"
            "allowed paths を必要に応じて拡張してください。",
            "- impl-review-loop も合わせて直してください。",
        ]
    )
    permission_body = "\n".join(
        [
            "sudo access が必要になるため allowed paths を必要に応じて拡張してください。",
            "- impl-review-loop も合わせて直してください。",
        ]
    )
    external_body = "\n".join(
        [
            "external API 呼び出しが必要になるため allowed paths を必要に応じて拡張してください。",
            "- impl-review-loop も合わせて直してください。",
        ]
    )
    payload = {"id": 1, "author_association": "OWNER", "user": {"login": "owner", "type": "User"}}
    for body, expected_reason in (
        (split_body, "requires_issue_split"),
        (permission_body, "changes_permission_boundary"),
        (external_body, "changes_external_service_boundary"),
    ):
        evidence = preflight._build_scope_delta_authority_evidence(
            comment_payload=payload,
            comment_body=body,
            repo=_REPO,
            issue_number=_ISSUE,
            anchor_url=_URL,
            captured_at="2026-08-01T00:00:00Z",
            human_context_comment_urls=[_URL],
            agent_report_comment_urls=None,
        )
        assert expected_reason in evidence["boundary_flags"], (expected_reason, evidence["boundary_flags"])
        result = sda.classify_scope_delta_authority(
            evidence,
            triggered=True,
            target_issue_number=_ISSUE,
            expected_repo=_REPO,
            base_issue_body_sha256="sha256:x",
            investigation_derived_path_literals=[".claude/skills/impl-review-loop/SKILL.md"],
        )
        assert result["route"]["action"] == "human_escalation"
        assert result["route"]["reason_code"] == expected_reason


# ---------------------------------------------------------------------------
# End-to-end (mutation-phase lane, real `run_preflight()` call chain): a
# VALID (non-rejected) transport still yields zero writes / zero mutation-
# callback invocations for a destructive-boundary directive.
# ---------------------------------------------------------------------------


def _issue_body() -> str:
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
        "## Allowed Paths\n\n- docs/dev/existing.md\n\n"
        "## Stop Conditions\n\n- none\n\n"
        "## Required Skills\n\n- none\n\n"
        "## Runtime Verification Applicability\n\n"
        "- decision: not_applicable\n"
        "- reason: static verification only for this fixture\n"
    )


def _destructive_anchor_comment() -> dict:
    body = (
        "この破壊的な force push 作業は allowed paths を必要に応じて拡張してください。\n"
        "- impl-review-loop も合わせて直してください。\n"
    )
    return {
        "id": 1,
        "body": body,
        "issue_url": f"https://api.github.com/repos/{_REPO}/issues/{_ISSUE}",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
        "html_url": _URL,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "user": {"login": "owner", "type": "User"},
        "author_association": "OWNER",
    }


def _callbacks(*, issue_body: str, anchor_comment: dict):
    calls = {"apply_transaction": 0, "fresh_checks": 0}
    state = {"body": issue_body}

    def fetch_current():
        return (
            {"body": state["body"], "updatedAt": "2026-08-01T00:00:00Z"},
            dict(anchor_comment, html_url=_URL),
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


def test_valid_transport_still_fails_closed_for_destructive_directive_end_to_end(tmp_path, monkeypatch):
    """AC7 end-to-end: a VALID (non-rejected) transport manifest reaches
    `known_context["investigation_derived_path_literals"]`, but the
    destructive-boundary directive's route stays `human_escalation` --
    zero mutation-callback invocations through the real `run_preflight()`
    call chain, not merely at the classifier-unit level above.

    #2678 P1-1 fix_delta: `_find_repo_root()` is monkeypatched to a
    throwaway, per-test git checkout under `tmp_path` instead of the real
    repo checkout -- see this module's own docstring / `test_investigation_
    evidence_transport_mutation_guard.py`'s `_make_isolated_repo_root()`
    docstring for the full rationale."""
    repo_root = _make_isolated_repo_root(tmp_path)
    monkeypatch.setattr(preflight, "_find_repo_root", lambda: repo_root)

    issue_body = _issue_body()
    anchor_comment = _destructive_anchor_comment()
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": _ISSUE,
        "repo": _REPO,
        "now": "2026-08-01T00:00:00Z",
        "issue": {"number": _ISSUE, "title": "test", "body": issue_body, "labels": []},
        "comments": [],
        "anchor_comment_urls": [_URL],
        "anchor_comments": [anchor_comment],
    }
    fixture_path = tmp_path / "preflight_fixture_destructive.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

    body_sha256 = preflight._sha256(issue_body)
    git_head_sha = preflight._git_head_sha(repo_root)
    payload = [
        {
            "comment_url": _URL,
            "body_sha256": body_sha256,
            "source_kind": "generated_by_agent",
            "path_literals": [".claude/skills/impl-review-loop/SKILL.md"],
        }
    ]
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": "test-2678-ac7",
        "issue_number": _ISSUE,
        "repo": _REPO,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-01T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": _URL,
        "source_issue_body_sha256": body_sha256,
        "source_kind": "generated_by_agent",
        "payload": payload,
        "payload_sha256": _sha256(payload_json),
    }
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(_ISSUE)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    callbacks, calls = _callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    known_context = {"human_context_comment_urls": [_URL]}
    result, _exit_code = preflight.run_preflight(
        issue_number=_ISSUE,
        repo=_REPO,
        anchor_comment_urls=[_URL],
        fixture_path=fixture_path,
        known_context=known_context,
        consume_contract_patch_plan=True,
        contract_update_callbacks=callbacks,
        investigation_evidence_transport_path=manifest_path,
        investigation_evidence_primary_root=repo_root,
    )

    # The transport itself validated fine (no rejection blocker) -- the
    # destructive boundary, not a transport failure, is what fail-closes
    # this route.
    assert not any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0
    assert result.get("contract_update", {}).get("writes", 0) == 0
