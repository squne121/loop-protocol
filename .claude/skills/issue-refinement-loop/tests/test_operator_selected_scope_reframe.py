"""
test_operator_selected_scope_reframe.py

#2086 regression coverage: AC1, AC3, AC4, AC7, AC8, AC11.

Fixes the workflow defect surfaced by Issue #2084 comment #5249734344:
an operator-selected human-context anchor (`preflight.run.with_human_context`)
whose freeform prose explicitly signals architecture/workflow-level scope
expansion was being routed to `human_judgment_required` solely because it
lacked a hand-written `ANCHOR_SCOPE_REFRAME_V1` payload or one of the fixed
`_DIRECTIVE_SECTION_MARKERS` section headings.

This file never re-implements the classification logic under test -- it only
exercises the production `scope_signal_delta.classify_scope_delta_authority`
/ `classify_directive_confidence` / `run_refinement_preflight.
_build_scope_delta_authority_evidence` functions, mirroring the existing
`test_issue_1952_trusted_directive_regression.py` fixture-loading
convention.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
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


preflight = _load_module("run_refinement_preflight_2086_regression", "run_refinement_preflight.py")

REPO = "squne121/loop-protocol"
ISSUE = 2084
URL = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-5249734344"
ISSUE_URL = f"https://github.com/{REPO}/issues/{ISSUE}"


def _payload(*, comment_id: int = 5249734344, association: str = "OWNER") -> dict:
    return {
        "id": comment_id,
        "author_association": association,
        "user": {"login": "squne121", "type": "User"},
    }


def _evidence(
    body: str,
    *,
    url: str = URL,
    payload: "dict | None" = None,
    human_context_comment_urls: "list[str] | None" = None,
    agent_report_comment_urls: "list[str] | None" = None,
) -> dict:
    if human_context_comment_urls is None and agent_report_comment_urls is None:
        human_context_comment_urls = [url]
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=dict(payload or _payload()),
        comment_body=body,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=url,
        captured_at="2026-08-11T00:00:00Z",
        human_context_comment_urls=human_context_comment_urls,
        agent_report_comment_urls=agent_report_comment_urls,
    )
    assert evidence is not None
    return evidence


def _classify(evidence, **kwargs) -> dict:
    return sda.classify_scope_delta_authority(
        evidence,
        triggered=True,
        target_issue_number=ISSUE,
        expected_repo=REPO,
        base_issue_body_sha256="sha256:issue-2084-body",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC1/AC3: freeform prose without any known section-heading marker, on the
# operator-selected human-context lane, must not require
# ANCHOR_SCOPE_REFRAME_V1 (or any known marker) to become explicit.
# ---------------------------------------------------------------------------

_WORKFLOW_WIDE_FREEFORM_BODY = "\n".join(
    [
        "この不具合は issue-refinement-loop だけでなく impl-review-loop や "
        "build_intake_capsule、implement-issue にも共通する構造的な問題です。",
        "- impl-review-loop の該当ロジックも同様に直してください。",
        "- build_intake_capsule と implement-issue の関連処理も合わせて修正してください。",
        "- 今後は同じ前提が workflow 全体で成り立つようにしてください。",
    ]
)


def test_classify_directive_confidence_does_not_require_known_marker_on_operator_lane():
    """AC1: no known `_DIRECTIVE_SECTION_MARKERS` heading, operator lane +
    bullet directives -> explicit (not inferred/ambiguous)."""
    markers = sda.extract_directive_markers(_WORKFLOW_WIDE_FREEFORM_BODY)
    assert markers == [], "fixture must not accidentally contain a known marker"

    assert (
        sda.classify_directive_confidence(_WORKFLOW_WIDE_FREEFORM_BODY, markers)
        == sda.DIRECTIVE_CONFIDENCE_INFERRED
    ), "default (non-operator) classification is unchanged"

    assert (
        sda.classify_directive_confidence(
            _WORKFLOW_WIDE_FREEFORM_BODY, markers, operator_asserted_human_context=True
        )
        == sda.DIRECTIVE_CONFIDENCE_EXPLICIT
    )


# #2086 AC1 P1 fix_delta: a bullet list that is NOT an actual
# scope-expansion directive (observation notes / failure-log lines) must
# not be classified as `explicit` on the operator-selected human-context
# lane just because it has bullets.
_NON_DIRECTIVE_BULLET_BODY = "\n".join(
    [
        "調査中に issue-refinement-loop で以下の状況を確認しました。",
        "- 現在のログには AssertionError が記録されている。",
        "- 直近の実行時刻は 2026-08-10 だった。",
        "- 関連する PR 番号は #2084 だった。",
    ]
)


def test_non_directive_bullet_list_is_not_explicit_on_operator_lane_ac1():
    """AC1 P1 fix_delta: origin-lane assertion (operator-selected
    human-context) must not be conflated with semantic-directive detection.
    An observation/failure-log bullet list has no known section marker AND
    no imperative directive-request verb in any bullet -- it must stay
    `inferred`, not `explicit`, even on the trusted human-context lane."""
    markers = sda.extract_directive_markers(_NON_DIRECTIVE_BULLET_BODY)
    assert markers == [], "fixture must not accidentally contain a known marker"
    assert sda._BULLET_LINE_RE.search(_NON_DIRECTIVE_BULLET_BODY), "fixture must contain bullets"
    assert sda._has_semantic_directive_bullet(_NON_DIRECTIVE_BULLET_BODY) is False

    assert (
        sda.classify_directive_confidence(
            _NON_DIRECTIVE_BULLET_BODY, markers, operator_asserted_human_context=True
        )
        == sda.DIRECTIVE_CONFIDENCE_INFERRED
    )


def test_freeform_workflow_wide_directive_routes_contract_update_without_anchor_payload():
    """AC1/AC3/AC8: the #2084 comment #5249734344 failure profile -- freeform
    operator directive, no structured ANCHOR_SCOPE_REFRAME_V1 payload, scope
    expansion naming multiple skills -- must reach `contract_update_required`,
    not `human_judgment_required` / `no_anchor_scope_reframe_v1_payload`."""
    evidence = _evidence(_WORKFLOW_WIDE_FREEFORM_BODY)
    assert evidence["source_kind"] == "issue_comment"
    assert evidence["directive_markers"] == []
    assert evidence["confidence"] == "explicit"

    result = _classify(evidence)
    assert result["authority_category"] == "human_review_directive"
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["reason_code"] == "explicit_human_contract_directive"
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"
    # AC11: contract rewrite completing never implies implementation go.
    assert result["route"]["implementation_allowed"] is False


def test_freeform_directive_without_exact_backtick_path_yields_empty_operations_ac7():
    """AC7: without a known marker, `derive_contract_patch_operations`
    returns an empty operations list -- the SKILL.md-documented
    `full_rewrite_required` -> `issue_editor_required` lane is what a caller
    must reach next, not a silent no-op or a scope-expansion-only
    termination report."""
    evidence = _evidence(_WORKFLOW_WIDE_FREEFORM_BODY)
    assert sda.derive_contract_patch_operations([evidence]) == []


# ---------------------------------------------------------------------------
# AC3/AC4: a directive that additionally names an explicit (but vague, no
# exact literal) Allowed Paths expansion still fail-closes without
# investigation-derived literals (preserves the existing #1952 lock), and
# only a *trusted operator-lane* investigation-derived literal can clear it.
# ---------------------------------------------------------------------------

_VAGUE_ALLOWED_PATHS_BODY = "\n".join(
    [
        "この issue-refinement-loop の欠陥は他の workflow skill にも共通するため、",
        "allowed paths を必要に応じて拡張してください。",
        "- impl-review-loop も合わせて直してください。",
        "- build_intake_capsule と implement-issue の関連処理も修正してください。",
    ]
)


def test_vague_allowed_paths_directive_without_investigation_literals_still_escalates():
    """The pre-existing #1952 fail-closed behavior for a directive that only
    says "expand Allowed Paths as needed" (no exact literal, no
    investigation-derived path) must not regress."""
    evidence = _evidence(_VAGUE_ALLOWED_PATHS_BODY)
    assert evidence["boundary_flags"] == ["expands_allowed_paths"]

    result = _classify(evidence)
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


def test_investigation_derived_path_literals_clear_the_boundary_for_operator_lane_ac3_ac4():
    """AC3/AC4: read-only agent investigation supplies the exact repository
    paths the freeform semantic directive named (no hand-written
    ANCHOR_SCOPE_REFRAME_V1, no exact backtick literal in the comment
    itself). Only the operator-selected human-context lane may use this --
    the Allowed Paths / architecture-layer expansion itself is not treated
    as a Stop Condition for a trusted directive."""
    evidence = _evidence(_VAGUE_ALLOWED_PATHS_BODY)

    result = _classify(
        evidence,
        investigation_derived_path_literals=[
            "docs/dev/workflow.md",
            ".claude/skills/impl-review-loop/SKILL.md",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            ".claude/skills/implement-issue/SKILL.md",
        ],
    )
    assert result["route"]["action"] == "contract_update_required"
    assert result["route"]["reason_code"] == "explicit_human_contract_directive"
    assert result["route"]["next_step"] == "rerun_refinement_after_contract_update"
    assert result["route"]["implementation_allowed"] is False


def test_investigation_derived_path_literals_do_not_clear_destructive_boundary_ac5():
    """AC5: destructive/permission/external-service boundaries are not
    relaxed by investigation-derived literals -- only `expands_allowed_paths`
    is."""
    body = "\n".join(
        [
            "この破壊的な force push 作業は allowed paths を必要に応じて拡張してください。",
            "- impl-review-loop も合わせて直してください。",
        ]
    )
    evidence = _evidence(body)
    assert evidence["boundary_flags"] == sorted(
        {"destructive_or_non_idempotent_operation", "expands_allowed_paths"}
    ) or set(evidence["boundary_flags"]) == {
        "destructive_or_non_idempotent_operation",
        "expands_allowed_paths",
    }

    result = _classify(
        evidence,
        investigation_derived_path_literals=[".claude/skills/impl-review-loop/SKILL.md"],
    )
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "destructive_or_non_idempotent_operation"


def test_investigation_derived_path_literals_mixed_valid_and_unsafe_stays_fail_closed_ac4():
    """#2086 AC4 P0 fix_delta: a MIXED list containing both a safe literal
    and an unsafe/traversal token must NOT clear the boundary -- one safe
    entry must never launder the whole list. Regression for the `any()`
    authority-laundering risk in `_has_investigation_derived_allowed_path_
    literals()` (previously only unsafe-only lists were tested)."""
    evidence = _evidence(_VAGUE_ALLOWED_PATHS_BODY)
    assert sda._has_investigation_derived_allowed_path_literals(
        ["docs/dev/workflow.md", "../../escape.py"]
    ) is False
    assert sda._has_investigation_derived_allowed_path_literals(
        ["/etc/passwd", ".claude/skills/impl-review-loop/SKILL.md"]
    ) is False

    result = _classify(
        evidence,
        investigation_derived_path_literals=["docs/dev/workflow.md", "../../escape.py"],
    )
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


def test_investigation_derived_path_literals_reject_unsafe_tokens():
    """An unsafe/absolute/traversal literal supplied via
    investigation_derived_path_literals must not clear the boundary either
    -- the same safety validation applies as for comment-extracted
    literals."""
    evidence = _evidence(_VAGUE_ALLOWED_PATHS_BODY)
    result = _classify(
        evidence,
        investigation_derived_path_literals=["/etc/passwd", "../../escape.py"],
    )
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "expands_allowed_paths"


# ---------------------------------------------------------------------------
# AC6: the same freeform directive on the `with_agent_report` / unlabeled
# lane never gets this relaxation, with or without investigation-derived
# literals.
# ---------------------------------------------------------------------------


def test_agent_report_lane_never_gets_operator_relaxation_ac6():
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=_payload(),
        comment_body=_WORKFLOW_WIDE_FREEFORM_BODY,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
        captured_at="2026-08-11T00:00:00Z",
        human_context_comment_urls=[],
        agent_report_comment_urls=[URL],
    )
    assert evidence["source_kind"] == "generated_by_agent"
    assert evidence["confidence"] != "explicit"

    result = _classify(
        evidence,
        investigation_derived_path_literals=[".claude/skills/impl-review-loop/SKILL.md"],
    )
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "ai_inferred_scope_delta"


def test_unlabeled_lane_never_gets_operator_relaxation_ac6():
    evidence = preflight._build_scope_delta_authority_evidence(
        comment_payload=_payload(),
        comment_body=_WORKFLOW_WIDE_FREEFORM_BODY,
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
        captured_at="2026-08-11T00:00:00Z",
    )
    assert evidence["source_kind"] == "generated_by_agent"

    result = _classify(
        evidence,
        investigation_derived_path_literals=[".claude/skills/impl-review-loop/SKILL.md"],
    )
    assert result["route"]["action"] == "human_escalation"


# ---------------------------------------------------------------------------
# #2086 AC3 P0 fix_delta: `investigation_derived_path_literals` must be
# reachable from the REAL canonical `preflight.run.with_human_context`
# entrypoint (`run_refinement_preflight.run_preflight()`, which the registry
# command dispatches to via `skill_runtime_exec.py`), not merely from a
# direct hand-injected call to `classify_scope_delta_authority()`. This
# exercises the full production chain: `run_preflight()` ->
# `_build_scope_delta_authority_evidence()` -> real
# `plan_refinement_loop.py` subprocess (`_invoke_planner()`, the same
# `subprocess.run([sys.executable, PLANNER_SCRIPT], ...)` call production
# uses) -> `classify_scope_delta_authority()` -> the persisted
# `refinement_preflight_provenance_v1.json` artifact's
# `runtime_evidence.route.action`.
# ---------------------------------------------------------------------------

_E2E_2086_ARTIFACT_DIR = SKILL_ROOT.parent.parent / "artifacts" / "issue-refinement-loop" / str(ISSUE)

_E2E_2086_ISSUE_BODY = (
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
    "## Allowed Paths\n\n- docs/dev/workflow.md\n\n"
    "## Stop Conditions\n\n- none\n\n"
    "## Required Skills\n\n- none\n"
)



def _e2e_2086_anchor_comment(body: str) -> dict:
    return {
        "id": 5249734344,
        "body": body,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/{ISSUE}",
        "created_at": "2026-08-11T00:00:00Z",
        "updated_at": "2026-08-11T00:00:00Z",
        "html_url": URL,
        "url": f"https://api.github.com/repos/{REPO}/issues/comments/5249734344",
        "user": {"login": "squne121", "type": "User"},
        "author_association": "OWNER",
    }


def _e2e_2086_run_preflight(
    *,
    known_context: dict,
    run_id: str,
    tmp_path,
    investigation_evidence_transport_path=None,
):
    fixture = {
        "schema_version": "refinement_preflight_input/v1",
        "issue_number": ISSUE,
        "repo": REPO,
        "now": "2026-08-11T00:00:00Z",
        "issue": {"number": ISSUE, "title": "test", "body": _E2E_2086_ISSUE_BODY, "labels": []},
        "comments": [],
        "anchor_comment_urls": [URL],
        "anchor_comments": [_e2e_2086_anchor_comment(_VAGUE_ALLOWED_PATHS_BODY)],
    }
    fixture_path = tmp_path / f"preflight_2086_{run_id}.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    try:
        result, exit_code = preflight.run_preflight(
            issue_number=ISSUE,
            repo=REPO,
            anchor_comment_urls=[URL],
            fixture_path=fixture_path,
            known_context=known_context,
            investigation_evidence_transport_path=investigation_evidence_transport_path,
        )
        prov_path = _E2E_2086_ARTIFACT_DIR / "refinement_preflight_provenance_v1.json"
        assert prov_path.exists(), "provenance artifact must be written by the real run_preflight() success path"
        provenance = json.loads(prov_path.read_text(encoding="utf-8"))
    finally:
        if _E2E_2086_ARTIFACT_DIR.exists():
            shutil.rmtree(_E2E_2086_ARTIFACT_DIR)
    return result, exit_code, provenance


# ---------------------------------------------------------------------------
# #2086 P0 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 1/Blocker 2):
# the PRIOR version of this test hand-injected
# `known_context["investigation_derived_path_literals"]` directly into a
# Python API call to `run_preflight()` -- there was no CLI flag, no
# producer, and no registry field carrying this value through the REAL
# `skill_runtime_exec.py` -> registry-rendered argv -> subprocess chain, so
# the positive route only ever proved the hand-injected shortcut worked, not
# production wiring. This rewrite mints a REAL, digest-bound
# SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest via the actual #2053 producer
# (`preflight.generate_authority_transport_manifest`, the identical function
# `--produce-authority-transport` / the `authority_transport.produce`
# command_id dispatch to -- see
# `scripts/agent-guards/tests/test_skill_runtime_policy_anchor.py::
# test_authority_transport_produce_reaches_real_subprocess` for the
# companion real-subprocess-dispatch proof of that producer, and
# `test_investigation_evidence_transport_path_reaches_real_subprocess_ac3`
# in the same file for the real-subprocess proof of the NEW
# `preflight.run.with_human_context --investigation-evidence-transport-path`
# flag added by this fix_delta), then drives `run_preflight()`'s new
# `investigation_evidence_transport_path` parameter -- the SAME parameter
# `main()`'s new `--investigation-evidence-transport-path` CLI flag
# populates -- through `_validate_investigation_evidence_transport()`
# (Blocker 2's typed, cryptographically bound evidence loader) exactly as
# production does. `known_context` here NEVER carries
# `investigation_derived_path_literals` directly.
# ---------------------------------------------------------------------------


def _mint_investigation_evidence_transport_manifest(*, invocation_id: str, path_literals: list) -> Path:
    repo_root = preflight._find_repo_root()
    git_head_sha = preflight._git_head_sha(repo_root)
    payload = [
        {
            "comment_id": 5249734344,
            "comment_url": URL,
            "body_sha256": preflight._sha256(_E2E_2086_ISSUE_BODY),
            "source_kind": "generated_by_agent",
            "path_literals": path_literals,
        }
    ]
    result, error = preflight.generate_authority_transport_manifest(
        evidence=payload,
        issue_number=ISSUE,
        repo=REPO,
        invocation_id=invocation_id,
        git_head_sha=git_head_sha,
        repo_root=repo_root,
    )
    assert result is not None, error
    return Path(result["manifest_path"])


def _cleanup_investigation_evidence_transport_manifest(invocation_id: str) -> None:
    repo_root = preflight._find_repo_root()
    manifest_dir = preflight._authority_transport_dir(repo_root, ISSUE, invocation_id)
    if manifest_dir.exists():
        shutil.rmtree(manifest_dir)


def test_investigation_derived_path_literals_reach_contract_update_via_real_preflight_entrypoint_ac3(tmp_path):
    """AC3 (runtime-verification): a REAL, digest-bound
    SCOPE_DELTA_AUTHORITY_TRANSPORT_V1 manifest (minted by the actual #2053
    producer, never hand-injected known_context) must flow end-to-end
    through `run_preflight()`'s `investigation_evidence_transport_path`
    parameter -> `_validate_investigation_evidence_transport()` -> real
    `plan_refinement_loop.py` subprocess -> `classify_scope_delta_authority()`
    -- and be visible in the persisted `refinement_preflight_provenance_v1.json`
    artifact's `runtime_evidence.route.action`."""
    invocation_id = "test-ac3-fixdelta-positive"
    try:
        manifest_path = _mint_investigation_evidence_transport_manifest(
            invocation_id=invocation_id,
            path_literals=[
                "docs/dev/workflow.md",
                ".claude/skills/impl-review-loop/SKILL.md",
                ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
                ".claude/skills/implement-issue/SKILL.md",
            ],
        )
        _result, _exit_code, provenance = _e2e_2086_run_preflight(
            known_context={
                "human_context_comment_urls": [URL],
                "agent_report_comment_urls": [],
            },
            run_id="with_bound_transport",
            tmp_path=tmp_path,
            investigation_evidence_transport_path=manifest_path,
        )
    finally:
        _cleanup_investigation_evidence_transport_manifest(invocation_id)
    route = provenance["runtime_evidence"]["route"]
    assert route["action"] == "contract_update_required", provenance
    assert route["implementation_allowed"] is False, provenance


def test_investigation_derived_path_literals_absent_still_escalates_via_real_preflight_entrypoint_ac3(tmp_path):
    """Negative control for the AC3 wiring test above: the SAME real
    `run_preflight()` entrypoint, with an identical directive but no
    `investigation_evidence_transport_path`, must still resolve to
    `human_escalation` -- proving the positive test above is exercising
    real wiring, not a boundary that always clears."""
    _result, _exit_code, provenance = _e2e_2086_run_preflight(
        known_context={
            "human_context_comment_urls": [URL],
            "agent_report_comment_urls": [],
        },
        run_id="without_literals",
        tmp_path=tmp_path,
    )
    route = provenance["runtime_evidence"]["route"]
    assert route["action"] == "human_escalation", provenance


def test_investigation_evidence_transport_wrong_anchor_binding_fails_closed_ac4(tmp_path):
    """#2086 Blocker 2: a manifest minted for a DIFFERENT anchor comment URL
    must never clear the boundary for this invocation -- the binding check
    (`source_comment_url == anchor_url`) must reject it, not silently trust
    a caller-supplied `list[str]`-shaped payload just because its outer
    schema/digest is internally self-consistent."""
    invocation_id = "test-ac4-fixdelta-wrong-anchor"
    repo_root = preflight._find_repo_root()
    git_head_sha = preflight._git_head_sha(repo_root)
    other_url = f"https://github.com/{REPO}/issues/{ISSUE}#issuecomment-9999999999"
    payload = [
        {
            "comment_id": 9999999999,
            "comment_url": other_url,
            "body_sha256": preflight._sha256(_E2E_2086_ISSUE_BODY),
            "source_kind": "generated_by_agent",
            "path_literals": ["docs/dev/workflow.md"],
        }
    ]
    try:
        result, error = preflight.generate_authority_transport_manifest(
            evidence=payload,
            issue_number=ISSUE,
            repo=REPO,
            invocation_id=invocation_id,
            git_head_sha=git_head_sha,
            repo_root=repo_root,
        )
        assert result is not None, error
        manifest_path = Path(result["manifest_path"])
        _result, _exit_code, provenance = _e2e_2086_run_preflight(
            known_context={
                "human_context_comment_urls": [URL],
                "agent_report_comment_urls": [],
            },
            run_id="wrong_anchor_binding",
            tmp_path=tmp_path,
            investigation_evidence_transport_path=manifest_path,
        )
    finally:
        _cleanup_investigation_evidence_transport_manifest(invocation_id)
    route = provenance["runtime_evidence"]["route"]
    assert route["action"] == "human_escalation", provenance


def test_investigation_evidence_transport_tampered_digest_fails_closed_ac4(tmp_path):
    """#2086 Blocker 2: a manifest whose `payload_sha256` no longer matches
    its (tampered-after-mint) `payload` must never clear the boundary --
    content-digest binding, not just outer schema validity."""
    invocation_id = "test-ac4-fixdelta-tampered-digest"
    try:
        manifest_path = _mint_investigation_evidence_transport_manifest(
            invocation_id=invocation_id,
            path_literals=["docs/dev/workflow.md"],
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["payload"][0]["path_literals"].append(".claude/skills/implement-issue/SKILL.md")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        _result, _exit_code, provenance = _e2e_2086_run_preflight(
            known_context={
                "human_context_comment_urls": [URL],
                "agent_report_comment_urls": [],
            },
            run_id="tampered_digest",
            tmp_path=tmp_path,
            investigation_evidence_transport_path=manifest_path,
        )
    finally:
        _cleanup_investigation_evidence_transport_manifest(invocation_id)
    route = provenance["runtime_evidence"]["route"]
    assert route["action"] == "human_escalation", provenance


def test_untrusted_author_association_never_gets_operator_relaxation_ac5():
    evidence = _evidence(_WORKFLOW_WIDE_FREEFORM_BODY, payload=_payload(association="CONTRIBUTOR"))
    assert evidence["source_kind"] == "issue_comment"

    result = _classify(evidence)
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "untrusted_author_association"


# ---------------------------------------------------------------------------
# #2086 P1 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 4):
# `_SEMANTIC_DIRECTIVE_VERB_RE` / `_has_semantic_directive_bullet` is a
# lexical match and must not promote past-tense status statements or
# negated imperatives to `explicit` scope-expansion directives, even on the
# trusted operator-selected human-context lane.
# ---------------------------------------------------------------------------

_PAST_TENSE_STATUS_BODY = "\n".join(
    [
        "現在の issue-refinement-loop の状況を共有します。",
        "- この不具合は昨日すでに修正済みです。",
        "- ログを確認したところ、関連する処理は先日対応済みでした。",
    ]
)

_NEGATED_IMPERATIVE_BODY = "\n".join(
    [
        "issue-refinement-loop の scope について方針を共有します。",
        "- We should not widen the file scope for this Issue.",
        "- Do not fix the unrelated logging module in this PR.",
    ]
)

_GENUINE_POSITIVE_IMPERATIVE_BODY = "\n".join(
    [
        "issue-refinement-loop に既知の不具合があります。",
        "- Please fix the anchor classification bug in scope_signal_delta.py.",
        "- 関連する build_intake_capsule のロジックも修正してください。",
    ]
)


def test_past_tense_status_bullet_is_not_explicit_directive_ac1_blocker4():
    """A past-tense/perfect-status bullet ("already fixed yesterday") must
    NOT be promoted to `explicit` just because it sits on the trusted
    operator-selected human-context lane -- it is a status report, not an
    instruction."""
    assert sda._BULLET_LINE_RE.search(_PAST_TENSE_STATUS_BODY)
    assert sda._has_semantic_directive_bullet(_PAST_TENSE_STATUS_BODY) is False
    markers = sda.extract_directive_markers(_PAST_TENSE_STATUS_BODY)
    assert markers == []
    assert (
        sda.classify_directive_confidence(
            _PAST_TENSE_STATUS_BODY, markers, operator_asserted_human_context=True
        )
        == sda.DIRECTIVE_CONFIDENCE_INFERRED
    )


def test_negated_imperative_bullet_is_not_explicit_directive_ac1_blocker4():
    """A negated imperative ("should not expand", "do not fix") must NOT be
    promoted to `explicit` -- it explicitly instructs the OPPOSITE of a
    scope-expansion directive."""
    assert sda._BULLET_LINE_RE.search(_NEGATED_IMPERATIVE_BODY)
    assert sda._has_semantic_directive_bullet(_NEGATED_IMPERATIVE_BODY) is False
    markers = sda.extract_directive_markers(_NEGATED_IMPERATIVE_BODY)
    assert markers == []
    assert (
        sda.classify_directive_confidence(
            _NEGATED_IMPERATIVE_BODY, markers, operator_asserted_human_context=True
        )
        == sda.DIRECTIVE_CONFIDENCE_INFERRED
    )


def test_genuine_positive_imperative_bullet_still_explicit_ac1_blocker4():
    """Regression control: the narrowing in Blocker 4 must not break the
    existing genuine positive-imperative case (English "Please fix" +
    Japanese "してください")."""
    assert sda._has_semantic_directive_bullet(_GENUINE_POSITIVE_IMPERATIVE_BODY) is True
    markers = sda.extract_directive_markers(_GENUINE_POSITIVE_IMPERATIVE_BODY)
    assert markers == []
    assert (
        sda.classify_directive_confidence(
            _GENUINE_POSITIVE_IMPERATIVE_BODY, markers, operator_asserted_human_context=True
        )
        == sda.DIRECTIVE_CONFIDENCE_EXPLICIT
    )


# ---------------------------------------------------------------------------
# #2156 AC7: `_project_scope_delta_decision_to_approval()` (plan_refinement_loop.py)
# must not drop trusted-author anchor comment evidence for the genuine-absence
# (`status: not_applicable`) case.
# ---------------------------------------------------------------------------

import importlib.util as _importlib_util  # noqa: E402

_PLAN_SCRIPTS_DIR = SKILL_ROOT / "scripts"


def _load_plan_refinement_loop_module():
    if "scope_signal_delta" not in sys.modules:
        _spec_sd = _importlib_util.spec_from_file_location(
            "scope_signal_delta", _PLAN_SCRIPTS_DIR / "scope_signal_delta.py"
        )
        assert _spec_sd is not None and _spec_sd.loader is not None
        _module_sd = _importlib_util.module_from_spec(_spec_sd)
        sys.modules["scope_signal_delta"] = _module_sd
        _spec_sd.loader.exec_module(_module_sd)

    _spec = _importlib_util.spec_from_file_location(
        "plan_refinement_loop_2156", _PLAN_SCRIPTS_DIR / "plan_refinement_loop.py"
    )
    assert _spec is not None and _spec.loader is not None
    _module = _importlib_util.module_from_spec(_spec)
    sys.modules["plan_refinement_loop_2156"] = _module
    _spec.loader.exec_module(_module)
    return _module


def test_not_applicable_genuine_absence_preserves_anchor_evidence():
    """AC7: when `scope_delta_decision.status == "not_applicable"` (the
    genuine-absence case, #2156 AC2), `_project_scope_delta_decision_to_approval()`
    must still populate the trusted author's anchor comment evidence fields
    (`comment_url` / `body_sha256` / `author_association` / `required_rerun`)
    from `scope_delta_decision` rather than leaving them at the
    `_base_approval_result()` defaults. The final `approval["status"]` stays
    `missing_marker` (unchanged from the pre-#2156 `fail_closed` +
    `no_anchor_scope_reframe_v1_payload` projection)."""
    planner = _load_plan_refinement_loop_module()

    scope_delta_decision = preflight._classify_anchor_scope_reframe(
        comment_payload=_payload(association="OWNER"),
        anchor_body="Just a plain review comment without any reframe marker.",
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
    )
    assert scope_delta_decision["status"] == "not_applicable"
    assert scope_delta_decision["reason"] == "no_anchor_scope_reframe_v1_payload"

    known_context = {"scope_delta_decision": scope_delta_decision}
    approval = planner._project_scope_delta_decision_to_approval(known_context)

    assert approval["status"] == "missing_marker"
    assert approval["present"] is True
    assert approval["comment_url"] == scope_delta_decision["anchor_comment_url"]
    assert approval["body_sha256"] == scope_delta_decision["anchor_comment_hash"]
    assert approval["author_association"] == scope_delta_decision["anchor_author_association"]
    assert approval["comment_url"], "comment_url evidence must not be dropped"
    assert approval["body_sha256"], "body_sha256 evidence must not be dropped"
    assert approval["author_association"] == "OWNER"


# ---------------------------------------------------------------------------
# PR #2171 fix_delta (P1-4, OWNER adversarial review):
# `_project_scope_delta_decision_to_approval()`'s `status == "not_applicable"`
# handling must stay scoped to the intended combination (`reason ==
# no_anchor_scope_reframe_v1_payload` + trusted-author anchor evidence), and
# must not change the meaning of other `not_applicable` producers (bare
# `{"status": "not_applicable"}`, or an unrelated reason).
# ---------------------------------------------------------------------------


def test_bare_not_applicable_without_reason_stays_missing():
    """A bare `{"status": "not_applicable"}` (no `reason`, no anchor comment
    evidence at all) must project to the untouched `_base_approval_result()`
    baseline (`status: missing`, `present: False`) -- never
    `invalid_scope_delta_approval` (which would mischaracterize "no info
    available" as "a reframe was attempted but rejected")."""
    planner = _load_plan_refinement_loop_module()

    known_context = {"scope_delta_decision": {"status": "not_applicable"}}
    approval = planner._project_scope_delta_decision_to_approval(known_context)

    assert approval["status"] == "missing"
    assert approval["present"] is False
    assert approval["comment_url"] is None
    assert approval["body_sha256"] is None
    assert approval["author_association"] is None


def test_not_applicable_with_unrelated_reason_stays_missing():
    """A `status: not_applicable` decision carrying a reason OTHER than
    `no_anchor_scope_reframe_v1_payload` must also stay at the untouched
    `missing` baseline -- P1-4 scopes the evidence-populating branch to the
    ONE intended reason, not to `status == not_applicable` in general."""
    planner = _load_plan_refinement_loop_module()

    known_context = {
        "scope_delta_decision": {
            "status": "not_applicable",
            "reason": "some_future_unrelated_producer_reason",
            "anchor_comment_url": URL,
            "anchor_comment_hash": "sha256:should-not-be-projected",
            "anchor_author_association": "OWNER",
        }
    }
    approval = planner._project_scope_delta_decision_to_approval(known_context)

    assert approval["status"] == "missing"
    assert approval["present"] is False
    assert approval["comment_url"] is None
    assert approval["body_sha256"] is None
    assert approval["author_association"] is None


def test_intended_reason_missing_url_hash_author_association_still_projects_missing_marker():
    """The intended combination (`not_applicable` +
    `no_anchor_scope_reframe_v1_payload`) with some evidence fields absent
    (e.g. `anchor_comment_url` not set) must still reach the `missing_marker`
    lane -- the scoping fix (P1-4) only restricts WHICH `not_applicable`
    producers reach evidence population, not the intended lane's own
    tolerance for partially-missing fields."""
    planner = _load_plan_refinement_loop_module()

    known_context = {
        "scope_delta_decision": {
            "status": "not_applicable",
            "reason": "no_anchor_scope_reframe_v1_payload",
        }
    }
    approval = planner._project_scope_delta_decision_to_approval(known_context)

    assert approval["status"] == "missing_marker"
    assert approval["present"] is True
    assert approval["comment_url"] is None
    assert approval["body_sha256"] is None
    assert approval["author_association"] is None


def test_intended_reason_with_full_trusted_evidence_projects_missing_marker_with_evidence():
    """The intended combination with COMPLETE trusted-author anchor evidence
    (the real `_classify_anchor_scope_reframe()` shape) is the pre-existing
    #2156 AC7 behavior, re-asserted here as an explicit fourth regression
    case alongside the three narrower ones above."""
    planner = _load_plan_refinement_loop_module()

    scope_delta_decision = preflight._classify_anchor_scope_reframe(
        comment_payload=_payload(association="OWNER"),
        anchor_body="Just a plain review comment without any reframe marker.",
        repo=REPO,
        issue_number=ISSUE,
        anchor_url=URL,
    )
    known_context = {"scope_delta_decision": scope_delta_decision}
    approval = planner._project_scope_delta_decision_to_approval(known_context)

    assert approval["status"] == "missing_marker"
    assert approval["present"] is True
    assert approval["comment_url"] == scope_delta_decision["anchor_comment_url"]
    assert approval["body_sha256"] == scope_delta_decision["anchor_comment_hash"]
    assert approval["author_association"] == "OWNER"

