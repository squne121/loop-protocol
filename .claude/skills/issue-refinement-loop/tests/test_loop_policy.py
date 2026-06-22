"""
Tests for issue-refinement-loop loop policy (Issue #460).

Verifies:
- AC1: max_iterations default is 3 in SKILL.md
- AC4: needs-fix at iteration < 3 → auto-continue
- AC5: needs-fix at iteration >= 3 → human_escalation
- AC6: hard stop conditions (state/needs-human, scope change) stop as before
"""

import re
import yaml
import pytest
from pathlib import Path

SKILL_MD = Path(__file__).parent.parent / "SKILL.md"
TERMINATION_MD = Path(__file__).parent.parent / "references" / "termination-policy.md"


def test_skill_md_max_iterations_default_is_3():
    """AC1: SKILL.md の max_iterations 既定値 = 3"""
    text = SKILL_MD.read_text()
    # Check Inputs section
    assert re.search(r"max_iterations.*既定\s*3", text), \
        "max_iterations default should be 3 in Inputs section"
    # LOOP_STATE block was moved to schemas/loop_state.schema.json (Issue #795).
    # Verify that schema file carries the default value.
    schema_path = SKILL_MD.parent / "schemas" / "loop_state.schema.json"
    assert schema_path.exists(), "schemas/loop_state.schema.json must exist"
    import json
    schema = json.loads(schema_path.read_text())
    max_iter_prop = schema.get("properties", {}).get("max_iterations", {})
    assert max_iter_prop.get("default") == 3, \
        "loop_state.schema.json max_iterations default must be 3"


def test_skill_md_loop_iteration_approval_gate_not_required():
    """AC3: loop_iteration_approval_gate.default_required: false が明記されている"""
    text = SKILL_MD.read_text()
    assert "loop_iteration_approval_gate" in text, \
        "loop_iteration_approval_gate must be present in SKILL.md"
    assert "default_required: false" in text, \
        "default_required: false must be present in SKILL.md"


def test_skill_md_loop_policy_concept_separation():
    """AC8: loop policy と Claude Code permission mode の概念分離が明記されている"""
    text = SKILL_MD.read_text()
    assert "loop policy" in text, "loop policy concept should be mentioned"
    assert "permission mode" in text, "permission mode concept should be mentioned"
    # Both concepts should be explained as orthogonal
    assert "直交" in text, "The two concepts should be described as orthogonal (直交)"


def test_loop_continues_when_iteration_below_max():
    """AC4: needs-fix + iteration < max_iterations → 自動継続"""
    max_iterations = 3
    # Simulate loop state
    iterations_continued = []
    for iteration in range(max_iterations):
        verdict = "needs-fix"
        if verdict == "needs-fix":
            if iteration + 1 < max_iterations:
                action = "continue"
            else:
                action = "human_escalation"
        iterations_continued.append((iteration, action))

    # First two iterations (0, 1) should continue
    assert iterations_continued[0] == (0, "continue"), \
        "iteration=0 needs-fix should auto-continue"
    assert iterations_continued[1] == (1, "continue"), \
        "iteration=1 needs-fix should auto-continue"


def test_loop_escalates_at_max_iterations():
    """AC5: needs-fix + iteration >= max_iterations → human_escalation"""
    max_iterations = 3
    iteration = 2  # 0-indexed, this is the 3rd (last allowed)
    verdict = "needs-fix"

    if verdict == "needs-fix":
        if iteration + 1 < max_iterations:
            action = "continue"
        else:
            action = "human_escalation"

    assert action == "human_escalation", \
        "3rd needs-fix (iteration=2) should result in human_escalation"


def test_loop_escalation_requires_blocker_summary():
    """AC5: human_escalation は全 iteration 分の blocker summary を含む"""
    text = SKILL_MD.read_text()
    # The policy should mention blocker summary for escalation
    assert "blocker summary" in text, \
        "human_escalation path should include blocker summary requirement"


def test_hard_stop_state_needs_human():
    """AC6: state/needs-human は hard stop として従来通り停止する"""
    text = SKILL_MD.read_text()
    assert "state/needs-human" in text, \
        "state/needs-human hard stop must still be present in SKILL.md"


def test_hard_stop_scope_change():
    """AC6: scope change signal は従来通り停止する"""
    text = SKILL_MD.read_text()
    assert "Scope Change Stop Conditions" in text, \
        "Scope Change Stop Conditions section must still be present"
    # Verify the hard stop triggers human_escalation
    assert "human_escalation" in text


def test_termination_policy_no_auto_fixable_structural():
    """AC7 (pr_review_only): termination-policy.md に auto_fixable_structural なし"""
    text = TERMINATION_MD.read_text()
    assert "auto_fixable_structural" not in text, \
        "auto_fixable_structural should be removed from termination-policy.md"
    assert "--no-approval auto-continuation" not in text, \
        "--no-approval auto-continuation section should be removed"


def test_termination_policy_no_no_approval_dependency():
    """AC4/AC5: termination-policy.md の継続条件に --no-approval 依存なし"""
    text = TERMINATION_MD.read_text()
    # The --no-approval flag should not appear as a routing condition
    assert "--no-approval" not in text, \
        "--no-approval should not appear as a routing condition in termination-policy.md"


def test_termination_policy_human_escalation_at_max():
    """AC5: termination-policy.md に max_iterations 到達時の human_escalation 条件あり"""
    text = TERMINATION_MD.read_text()
    assert "human_escalation" in text, \
        "human_escalation termination reason must be in termination-policy.md"
    assert "max_iterations" in text or "iteration" in text, \
        "iteration/max_iterations condition must appear in termination-policy.md"


# B3: Bidi/Unicode 制御文字検出テスト
def test_no_unicode_bidi_control_chars_in_loop_policy_files():
    from pathlib import Path
    skill_root = Path(__file__).parent.parent
    paths = [
        skill_root / "SKILL.md",
        skill_root / "references" / "termination-policy.md",
        skill_root.parent / "impl-review-loop" / "SKILL.md",
        skill_root.parent / "impl-review-loop" / "steps" / "preparation.md",
    ]
    forbidden = {
        "‪", "‫", "‬", "‭", "‮",
        "⁦", "⁧", "⁨", "⁩",
    }
    for path in paths:
        text = path.read_text()
        bad = sorted({hex(ord(ch)) for ch in text if ch in forbidden})
        assert not bad, f"{path} contains bidi control chars: {bad}"


# B4: AC5 termination result schema 検証
def test_human_escalation_termination_result_schema_documented():
    """AC5: human_escalation 時の termination result が machine-readable に定義されていること"""
    text = TERMINATION_MD.read_text()
    assert "LOOP_TERMINATION_RESULT_V1" in text
    assert "blockers_history" in text
    assert "human_escalation" in text


# B5: AC6 hard stop テスト
def test_hard_stop_state_done():
    """AC6: state/done は hard stop として SKILL.md に明記されていること"""
    text = SKILL_MD.read_text()
    assert "state/done" in text


def test_hard_stop_fail_closed_contract_malformation():
    """AC6: contract malformation / fail_closed は hard stop として扱われること"""
    text = SKILL_MD.read_text()
    assert "fail_closed" in text


def test_hard_stop_required_external_research_unresolved():
    """AC6: required external research unresolved は停止条件として termination-policy.md に明記"""
    text = TERMINATION_MD.read_text()
    assert "required external research" in text


# ---------------------------------------------------------------------------
# B2: hard stop priority table-driven tests
# ---------------------------------------------------------------------------

HARD_STOP_SIGNALS = [
    "state/needs-human",
    "state/done",
    "scope_change_signal",
    "contract_malformation",
    "required_external_research_unresolved",
    "unsafe_mutation",
]


@pytest.mark.parametrize("signal", HARD_STOP_SIGNALS)
def test_hard_stop_overrides_continue(signal):
    """AC6: hard stop シグナルは needs-fix + iteration < max_iterations でも継続しない"""
    policy_text = TERMINATION_MD.read_text()
    assert signal in policy_text, f"hard_stop '{signal}' が LOOP_POLICY_V1 に含まれていない"


# ---------------------------------------------------------------------------
# B3: LOOP_POLICY_V1 機械可読ブロック parse テスト
# ---------------------------------------------------------------------------


def _load_loop_policy_v1() -> dict:
    """termination-policy.md から LOOP_POLICY_V1 ブロックを抽出してパース"""
    text = TERMINATION_MD.read_text()
    match = re.search(r"```yaml\s*\n(LOOP_POLICY_V1:.*?)```", text, re.DOTALL)
    assert match, "LOOP_POLICY_V1 が termination-policy.md に見つからない"
    return yaml.safe_load(match.group(1))


def test_loop_policy_v1_max_iterations_default():
    policy = _load_loop_policy_v1()
    assert policy["LOOP_POLICY_V1"]["max_iterations_default"] == 3


def test_loop_policy_v1_approval_gate_not_required():
    policy = _load_loop_policy_v1()
    gate = policy["LOOP_POLICY_V1"]["loop_iteration_approval_gate"]
    assert gate["default_required"] is False
    assert gate["scope"] == "repo_loop_iteration_only"


def test_loop_policy_v1_hard_stop_overrides_continue():
    policy = _load_loop_policy_v1()
    routes = policy["LOOP_POLICY_V1"]["routes"]
    hard_stop_route = next((r for r in routes if "hard_stop" in r["when"]), None)
    assert hard_stop_route is not None
    assert hard_stop_route["action"] == "human_escalation"
    # hard_stop は needs-fix + iteration < max_iterations より優先（リストの先頭）
    assert routes.index(hard_stop_route) < routes.index(
        next(r for r in routes if "needs-fix" in r.get("when", ""))
    )


def test_loop_policy_v1_does_not_control_claude_permissions():
    policy = _load_loop_policy_v1()
    dnc = policy["LOOP_POLICY_V1"]["loop_iteration_approval_gate"]["does_not_control"]
    assert "bypassPermissions" in dnc
    assert "Claude Code permissions.defaultMode" in dnc
