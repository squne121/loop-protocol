"""tests/test_loop_handoff_schema_vocabulary.py

Issue #2740 AC3: `references/termination-policy.md` の
`## LOOP_HANDOFF_RESULT_V1 — Terminal Contract` セクション全体（`## Root-Owned
Synchronous Entry Transition` 節の冒頭例示値を含む -- 両 H2 セクションは
`## Dependency Materialization Gate` の直前まで連続している）を対象に、
`approved` 終了時の新規発行値が `status: refinement_approved` /
`routing_action: none` へ更新され、旧語彙 `impl_ready` / `run_impl_review_loop`
がすべて legacy reader compatibility 専用として明示的に demarcate されている
ことをセクション抽出 + pytest でピン留めする（grep VC ではない）。

既存の readiness-invariant 前提条件（`contract_review.gate_result ==
fresh_go` / hygiene auto-fix evidence 完備 / `blockers` 空）は
`refinement_approved` 発行条件としてそのまま維持されることも確認する。
"""

from __future__ import annotations

import pathlib
import re

_POLICY_PATH = (
    pathlib.Path(__file__).parent.parent / "references" / "termination-policy.md"
)

_SECTION_START = "## LOOP_HANDOFF_RESULT_V1 — Terminal Contract"
_SECTION_END = "## Dependency Materialization Gate"
# The literal heading line (not a prose cross-reference to it elsewhere in
# the section -- those exist too, e.g. "`## Root-Owned Synchronous Entry
# Transition` 節参照", and must not be matched by `_find_heading_index`).
_NESTED_ROOT_OWNED_HEADING = (
    "## Root-Owned Synchronous Entry Transition（root が単独で所有する同期的な実装着手への遷移経路、#2272 正本）"
)


def _load_policy() -> str:
    assert _POLICY_PATH.exists(), f"file not found: {_POLICY_PATH}"
    return _POLICY_PATH.read_text(encoding="utf-8")


def _load_section() -> str:
    """Extract the LOOP_HANDOFF_RESULT_V1 Terminal Contract section, which
    runs contiguously through the nested `## Root-Owned Synchronous Entry
    Transition` heading up to (but excluding) `## Dependency Materialization
    Gate` -- matching the scope AC3 explicitly names."""
    content = _load_policy()
    start = content.index(_SECTION_START)
    end = content.index(_SECTION_END, start)
    section = content[start:end]
    # Sanity: the nested Root-Owned heading really is inside this extracted
    # span (confirms the section boundary assumption used by AC3).
    assert _NESTED_ROOT_OWNED_HEADING in section
    return section


# ---------------------------------------------------------------------------
# New producer vocabulary is present.
# ---------------------------------------------------------------------------


def test_new_producer_status_value_present():
    section = _load_section()
    assert "status: refinement_approved" in section, (
        "the LOOP_HANDOFF_RESULT_V1 section must declare `status: "
        "refinement_approved` as the new producer value (#2740)"
    )


def test_new_producer_routing_action_value_present():
    section = _load_section()
    assert "routing_action: none" in section, (
        "the LOOP_HANDOFF_RESULT_V1 section must declare `routing_action: "
        "none` as the new producer value (#2740)"
    )


def test_refinement_approved_definition_heading_present():
    section = _load_section()
    assert "### `refinement_approved` 定義" in section, (
        "the former `### `impl_ready` 定義` heading must be renamed to "
        "`### `refinement_approved` 定義` (#2740)"
    )
    assert "### `impl_ready` 定義" not in section, (
        "the old unqualified `### `impl_ready` 定義` heading must not remain"
    )


def test_six_condition_checklist_self_reference_updated():
    section = _load_section()
    assert "routing_action == none" in section, (
        "condition 6 of the invariant checklist must self-reference the new "
        "producer value `routing_action == none`, not the legacy "
        "`run_impl_review_loop` value"
    )


def test_routing_rules_table_uses_new_vocabulary():
    section = _load_section()
    match = re.search(
        r"\|\s*全 invariant 満足.*\|\s*`refinement_approved`.*\|\s*`none`.*\|",
        section,
    )
    assert match, (
        "the Routing Rules table's full-invariant-satisfied row must declare "
        "`refinement_approved` / `none` as the status / routing_action pair"
    )


# ---------------------------------------------------------------------------
# Every bare mention of the legacy vocabulary is explicitly demarcated.
# ---------------------------------------------------------------------------


def test_every_impl_ready_mention_is_legacy_demarcated():
    section = _load_section()
    offending = []
    for lineno, line in enumerate(section.splitlines(), start=1):
        if "impl_ready" in line and "legacy" not in line:
            offending.append((lineno, line))
    assert offending == [], (
        "every remaining `impl_ready` mention inside the LOOP_HANDOFF_RESULT_V1 "
        f"section must be demarcated as legacy reader-only on the same line: {offending}"
    )


def test_every_run_impl_review_loop_mention_is_legacy_demarcated():
    section = _load_section()
    offending = []
    for lineno, line in enumerate(section.splitlines(), start=1):
        if "run_impl_review_loop" in line and "legacy" not in line:
            offending.append((lineno, line))
    assert offending == [], (
        "every remaining `run_impl_review_loop` mention inside the "
        "LOOP_HANDOFF_RESULT_V1 section must be demarcated as legacy "
        f"reader-only on the same line: {offending}"
    )


def test_legacy_vocabulary_still_present_for_backward_compat_readers():
    """The legacy aliases must still be mentioned somewhere (not silently
    deleted) so that readers of pre-#2740 GitHub comments can still resolve
    the old values -- only the *producer* contract changes."""
    section = _load_section()
    assert "impl_ready" in section
    assert "run_impl_review_loop" in section


# ---------------------------------------------------------------------------
# Root-Owned Synchronous Entry Transition opening example value.
# ---------------------------------------------------------------------------


def test_root_owned_section_opening_example_uses_new_vocabulary():
    section = _load_section()
    root_owned_idx = section.index(_NESTED_ROOT_OWNED_HEADING)
    opening = section[root_owned_idx : root_owned_idx + 600]
    assert "LOOP_HANDOFF_RESULT_V1.status: refinement_approved" in opening, (
        "the Root-Owned Synchronous Entry Transition section's opening "
        "example value must cite `LOOP_HANDOFF_RESULT_V1.status: "
        "refinement_approved`, not the legacy `impl_ready` value alone"
    )


def test_root_owned_section_no_longer_claims_impl_ready_is_the_sole_authority():
    """Pre-#2740, this sentence argued approved/`impl_ready` was 'not the
    *only* authority' (implying some other path also sufficed). Post-#2740,
    an `approved` termination confers no dispatch authority at all -- so the
    sentence must not use '唯一の' (the only) qualifier, which would still
    imply approved termination is *an* authority among several."""
    section = _load_section()
    root_owned_idx = section.index(_NESTED_ROOT_OWNED_HEADING)
    opening = section[root_owned_idx : root_owned_idx + 600]
    assert "唯一の authority ではない" not in opening
    assert "authority ではない" in opening


# ---------------------------------------------------------------------------
# Internal non-contradiction: readiness-invariant preconditions preserved.
# ---------------------------------------------------------------------------


def test_readiness_invariant_preconditions_preserved():
    section = _load_section()
    assert "contract_review.gate_result == fresh_go" in section
    assert "blockers" in section
    assert "evidence" in section


def test_hygiene_delegation_contract_uses_new_vocabulary():
    section = _load_section()
    assert "`refinement_approved`（legacy: `impl_ready`）に貢献する" in section, (
        "the Hygiene Delegation Contract's contribution sentence must cite "
        "`refinement_approved` as the primary value with `impl_ready` "
        "demarcated as its legacy alias"
    )


# ---------------------------------------------------------------------------
# Task Context workflow-signal distinction (In Scope bullet, #2740).
# ---------------------------------------------------------------------------


def test_task_context_signal_kind_distinction_documented():
    section = _load_section()
    assert "task_context_workflow_signals.py" in section, (
        "the section must reference "
        "scripts/task-context/task_context_workflow_signals.py to "
        "distinguish its `signal_kind: \"refinement_approved\"` from this "
        "file's `LOOP_HANDOFF_RESULT_V1.status: refinement_approved` (#2565 "
        "vs. #2740 -- same string, different schema/concept)"
    )
    assert "別概念" in section or "異なる概念" in section
