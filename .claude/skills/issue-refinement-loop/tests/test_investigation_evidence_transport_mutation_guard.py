"""
test_investigation_evidence_transport_mutation_guard.py

Issue #2678 AC6: when a caller explicitly specifies
`--investigation-evidence-transport-path` on the mutation-phase
`contract_update.run.with_human_context` lane AND that transport fails
validation (digest / issue / repo / anchor / stale body / stale HEAD / unsafe
path), the mutation consumer (`consume_trusted_anchor_contract_patch_plan()`)
must never be reached -- including the case where the anchor comment body,
taken alone (with no investigation evidence at all), can already derive an
INDEPENDENTLY valid, non-empty `CONTRACT_PATCH_PLAN_V1` (an exact
backtick-literal "Allowed Paths" delta, which
`scope_signal_delta.derive_contract_patch_operations()` extracts directly
from the comment text -- no investigation evidence is needed to clear the
`expands_allowed_paths` boundary for THIS directive shape at all). Mutation
callback invocation count and GitHub update-request count must both be
exactly 0 (observable assertion, not a status-string check alone).

Reuses the production `run_preflight()` -> `consume_trusted_anchor_
contract_patch_plan()` call chain via `fixture_path` mode (the SAME
in-process E2E convention `test_preflight_run_with_anchor.py` already
establishes for this module) -- no new harness.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_module("run_refinement_preflight_2678_guard", "run_refinement_preflight.py")

_REPO = "squne121/loop-protocol"
_ISSUE = 267800
_URL = f"https://github.com/{_REPO}/issues/{_ISSUE}#issuecomment-1"
_NEW_ALLOWED_PATH = "docs/dev/some-new-allowed-path-2678.md"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _issue_body() -> str:
    # Mirrors `test_preflight_run_with_anchor.py`'s own `_hrd_issue_body()`
    # (the SAME minimal-but-complete shape that reaches the mutation
    # consumer successfully on that module's human-context lane tests) --
    # a body missing `## Runtime Verification Applicability` fails closed
    # via `missing_required_section` before `consume_contract_patch_plan`
    # is ever reached, independent of transport, which would make this
    # test's AC6 guard unobservable.
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


def _anchor_comment() -> dict:
    # This directive is INDEPENDENTLY valid without any investigation
    # evidence: it names an exact backtick-literal Allowed Paths addition,
    # which `derive_contract_patch_operations()` extracts directly from the
    # comment text (`_has_explicit_exact_allowed_path_expansion()` bypasses
    # the `expands_allowed_paths` boundary for an exact literal). This is
    # the "anchor 本文単独から独立に有効な patch plan を導出できるケース"
    # AC6 requires coverage for.
    body = f"以下を確認してください。\n- Add `{_NEW_ALLOWED_PATH}` to Allowed Paths\n"
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


def _write_manifest(*, repo_root: Path, valid_body_sha256: str, git_head_sha: str, tamper: str) -> Path:
    """Hand-build a SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest matching
    `generate_authority_transport_manifest()`'s own shape, then tamper
    exactly ONE binding field so `_validate_investigation_evidence_transport()`
    fails closed on that ONE check."""
    payload = [
        {
            "comment_url": _URL,
            "body_sha256": valid_body_sha256,
            "source_kind": "generated_by_agent",
            "path_literals": ["docs/dev/workflow.md"],
        }
    ]
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest = {
        "schema_version": "SCOPE_DELTA_AUTHORITY_TRANSPORT_V1",
        "invocation_id": "test-2678-ac6",
        "issue_number": _ISSUE,
        "repo": _REPO,
        "git_head_sha": git_head_sha,
        "generated_at": "2026-08-01T00:00:00Z",
        "canonicalization_id": "loop-protocol-json-c14n-v1",
        "source_comment_id": 1,
        "source_comment_url": _URL,
        "source_issue_body_sha256": valid_body_sha256,
        "source_kind": "generated_by_agent",
        "payload": payload,
        "payload_sha256": _sha256(payload_json),
    }
    if tamper == "stale_body":
        manifest["source_issue_body_sha256"] = _sha256("a different, stale issue body")
    elif tamper == "stale_head":
        manifest["git_head_sha"] = "0" * 40
    elif tamper == "wrong_issue":
        manifest["issue_number"] = _ISSUE + 1
    elif tamper == "digest_mismatch":
        manifest["payload_sha256"] = _sha256("tampered")
    else:
        raise ValueError(f"unknown tamper kind: {tamper}")

    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(_ISSUE)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = artifact_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _run(tamper: str):
    repo_root = preflight._find_repo_root()
    issue_body = _issue_body()
    anchor_comment = _anchor_comment()
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
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(fixture, handle)
        fixture_path = Path(handle.name)

    manifest_path = _write_manifest(
        repo_root=repo_root,
        valid_body_sha256=preflight._sha256(issue_body),
        git_head_sha=preflight._git_head_sha(repo_root),
        tamper=tamper,
    )
    callbacks, calls = _callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(_ISSUE)
    known_context = {"human_context_comment_urls": [_URL]}
    try:
        result, exit_code = preflight.run_preflight(
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
    finally:
        fixture_path.unlink(missing_ok=True)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
    return result, exit_code, calls


def test_invalid_transport_with_independently_valid_anchor_write_zero():
    """AC6: a rejected transport (stale body binding) fail-closes the
    mutation consumer even though the anchor body ALONE derives a valid,
    non-empty patch plan -- mutation callback invocation count AND the
    (fixture-proxy) GitHub update-request count are both exactly 0."""
    result, _exit_code, calls = _run(tamper="stale_body")

    assert any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
    # Observable assertion (not a status-string check alone): the mutation
    # callback (`apply_transaction`, proxy for `edit_issue_txn.py` -- the
    # ONLY GitHub update-request path `consume_trusted_anchor_contract_
    # patch_plan()` can reach) was never invoked, and `fresh_checks` (only
    # ever invoked INSIDE that same consumer call) was never invoked either
    # -- proving `consume_trusted_anchor_contract_patch_plan()` itself was
    # never reached, not merely that it declined to write.
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0
    assert result.get("contract_update", {}).get("writes", 0) == 0
    # `contract_update` is entirely absent (never a fabricated "failed"
    # placeholder) -- the consumer branch is skipped altogether, not
    # entered-then-declined (schema-safe: `contract_update` is optional in
    # `refinement_preflight_result_v1.schema.json`).
    assert "contract_update" not in result, result


def test_invalid_transport_stale_head_with_independently_valid_anchor_write_zero():
    """AC6 (stale HEAD variant): same guarantee for a `git_head_sha`
    mismatch -- a distinct binding failure from the stale-body case above."""
    result, _exit_code, calls = _run(tamper="stale_head")

    assert any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0
    assert result.get("contract_update", {}).get("writes", 0) == 0


def test_invalid_transport_wrong_issue_with_independently_valid_anchor_write_zero():
    """AC6 (wrong-issue variant)."""
    result, _exit_code, calls = _run(tamper="wrong_issue")

    assert any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0
    assert result.get("contract_update", {}).get("writes", 0) == 0


def test_invalid_transport_digest_mismatch_with_independently_valid_anchor_write_zero():
    """AC6 (payload digest tamper variant)."""
    result, _exit_code, calls = _run(tamper="digest_mismatch")

    assert any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
    assert calls["apply_transaction"] == 0
    assert calls["fresh_checks"] == 0
    assert result.get("contract_update", {}).get("writes", 0) == 0


def test_valid_transport_absent_regression_still_reaches_mutation_for_same_anchor():
    """Non-regression control: WITHOUT any transport path at all (the
    ordinary, pre-#2678 call shape), this SAME independently-valid anchor
    directive still reaches the mutation consumer and writes exactly once --
    proving the AC6 guard above is transport-presence-gated, not an
    accidental universal block of this directive shape."""
    repo_root = preflight._find_repo_root()
    issue_body = _issue_body()
    anchor_comment = _anchor_comment()
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
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(fixture, handle)
        fixture_path = Path(handle.name)
    callbacks, calls = _callbacks(issue_body=issue_body, anchor_comment=anchor_comment)
    artifact_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / str(_ISSUE)
    known_context = {"human_context_comment_urls": [_URL]}
    try:
        result, _exit_code = preflight.run_preflight(
            issue_number=_ISSUE,
            repo=_REPO,
            anchor_comment_urls=[_URL],
            fixture_path=fixture_path,
            known_context=known_context,
            consume_contract_patch_plan=True,
            contract_update_callbacks=callbacks,
        )
    finally:
        fixture_path.unlink(missing_ok=True)
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

    assert calls["apply_transaction"] == 1
    assert result["contract_update"]["writes"] == 1
    assert not any(
        isinstance(b, str) and b.startswith("investigation_evidence_transport_rejected:")
        for b in result.get("blockers", [])
    ), result.get("blockers")
