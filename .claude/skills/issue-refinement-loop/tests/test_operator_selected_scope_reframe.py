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


def _e2e_2086_run_preflight(*, known_context: dict, run_id: str, tmp_path):
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
        )
        prov_path = _E2E_2086_ARTIFACT_DIR / "refinement_preflight_provenance_v1.json"
        assert prov_path.exists(), "provenance artifact must be written by the real run_preflight() success path"
        provenance = json.loads(prov_path.read_text(encoding="utf-8"))
    finally:
        if _E2E_2086_ARTIFACT_DIR.exists():
            shutil.rmtree(_E2E_2086_ARTIFACT_DIR)
    return result, exit_code, provenance


def test_investigation_derived_path_literals_reach_contract_update_via_real_preflight_entrypoint_ac3(tmp_path):
    """AC3 (runtime-verification): the caller-supplied
    `investigation_derived_path_literals` must actually flow end-to-end
    through the real `preflight.run.with_human_context` production chain --
    `run_preflight()` -> real `plan_refinement_loop.py` subprocess ->
    `classify_scope_delta_authority()` -- and be visible in the persisted
    `refinement_preflight_provenance_v1.json` artifact's
    `runtime_evidence.route.action`. Prior to the #2086 AC3 P0 fix_delta,
    `plan_refinement_loop.py`'s two `classify_scope_delta_authority()` call
    sites never forwarded this field, so this always resolved to
    `human_escalation` regardless of what a caller supplied."""
    _result, _exit_code, provenance = _e2e_2086_run_preflight(
        known_context={
            "human_context_comment_urls": [URL],
            "agent_report_comment_urls": [],
            "investigation_derived_path_literals": [
                "docs/dev/workflow.md",
                ".claude/skills/impl-review-loop/SKILL.md",
                ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
                ".claude/skills/implement-issue/SKILL.md",
            ],
        },
        run_id="with_literals",
        tmp_path=tmp_path,
    )
    route = provenance["runtime_evidence"]["route"]
    assert route["action"] == "contract_update_required", provenance
    assert route["implementation_allowed"] is False, provenance


def test_investigation_derived_path_literals_absent_still_escalates_via_real_preflight_entrypoint_ac3(tmp_path):
    """Negative control for the AC3 wiring test above: the SAME real
    `run_preflight()` entrypoint, with an identical directive but no
    `investigation_derived_path_literals`, must still resolve to
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


def test_untrusted_author_association_never_gets_operator_relaxation_ac5():
    evidence = _evidence(_WORKFLOW_WIDE_FREEFORM_BODY, payload=_payload(association="CONTRIBUTOR"))
    assert evidence["source_kind"] == "issue_comment"

    result = _classify(evidence)
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "untrusted_author_association"
