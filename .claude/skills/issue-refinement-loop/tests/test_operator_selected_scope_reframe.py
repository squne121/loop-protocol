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


def test_untrusted_author_association_never_gets_operator_relaxation_ac5():
    evidence = _evidence(_WORKFLOW_WIDE_FREEFORM_BODY, payload=_payload(association="CONTRIBUTOR"))
    assert evidence["source_kind"] == "issue_comment"

    result = _classify(evidence)
    assert result["authority_category"] == "ai_inferred"
    assert result["route"]["action"] == "human_escalation"
    assert result["route"]["reason_code"] == "untrusted_author_association"
