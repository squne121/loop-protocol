#!/usr/bin/env python3

from pathlib import Path


SKILL_MD = Path(__file__).parent.parent / "SKILL.md"
REFERENCES_DIR = Path(__file__).parent.parent / "references"


def load_skill_md() -> str:
    assert SKILL_MD.exists(), f"SKILL.md not found: {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_skill_md_has_thin_entrypoint_sentinel():
    skill_md = load_skill_md()
    assert "ISSUE_REFINEMENT_LOOP_THIN_ENTRYPOINT_V1" in skill_md
    assert "planner_ssot: REFINEMENT_LOOP_PLAN_V1" in skill_md
    assert "no_prose_rejudgment: true" in skill_md


def test_skill_md_is_under_500_lines():
    line_count = len(SKILL_MD.read_text(encoding="utf-8").splitlines())
    assert line_count <= 500, f"SKILL.md should be <= 500 lines, got {line_count}"


def test_references_index_has_required_columns():
    index_md = REFERENCES_DIR / "index.md"
    assert index_md.exists(), "references/index.md should exist"
    text = index_md.read_text(encoding="utf-8")
    assert "| topic | file | loaded_when | owner | moved_from | must_not |" in text


def test_skill_md_links_required_reference_topics():
    skill_md = load_skill_md()
    required_refs = [
        "references/anchor-comment-handling.md",
        "references/web-research-routing.md",
        "references/follow-up-materialization.md",
        "references/termination-policy.md",
        "references/ac-vc-reflection.md",
        "references/scope-signal-guard.md",
    ]
    missing = [ref for ref in required_refs if ref not in skill_md]
    assert not missing, f"SKILL.md is missing required reference links: {missing}"


def test_forbidden_planner_rejudgment_fragments_are_absent():
    skill_md = load_skill_md()
    forbidden_fragments = [
        "investigation_policy.codebase_required = true",
        "investigation_policy.codebase_required = false",
        "web_research_policy.required = true",
        "web_research_policy.required = false",
        "true_if_any",
        "false_only_if",
        "policy_derivation:",
    ]
    found = [fragment for fragment in forbidden_fragments if fragment in skill_md]
    assert not found, f"Found forbidden planner re-judgment fragments: {found}"


def test_required_reference_files_exist():
    required_files = [
        "anchor-comment-handling.md",
        "scope-signal-guard.md",
        "ac-vc-reflection.md",
        "follow-up-materialization.md",
        "web-research-routing.md",
        "termination-policy.md",
    ]
    missing = [name for name in required_files if not (REFERENCES_DIR / name).exists()]
    assert not missing, f"Missing required reference files: {missing}"
