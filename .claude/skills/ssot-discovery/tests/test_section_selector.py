import sys
from pathlib import Path


SKILLS_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from match_ssot import parse_markdown_sections, select_section_matches


def test_given_fenced_atx_and_setext_when_parsed_then_only_real_headings_are_selected():
    text = "# Top\n\n```markdown\n## Ignored\n```\n\nReal Setext\n----------\nbody\n\n## Child\nchild\n\n# Next\n"
    sections = parse_markdown_sections(text)
    assert [(section["heading"], section["heading_level"]) for section in sections] == [
        ("Top", 1), ("Real Setext", 2), ("Child", 2), ("Next", 1),
    ]
    assert sections[1]["end_line_exclusive"] == sections[2]["start_line"]


def test_given_workflow_when_scope_collision_selected_then_bounded_provenance_is_returned():
    repo_root = Path(__file__).resolve().parents[4]
    matches, outcomes = select_section_matches(
        repo_root,
        [{"path": "docs/dev/workflow.md"}],
        ["Scope Collision"],
        4_000,
    )
    assert outcomes
    assert len(matches) == 1
    match = matches[0]
    assert match["heading"].startswith("Scope Collision Preflight")
    assert match["start_line"] < match["end_line_exclusive"]
    for field in ("source_commit", "blob_sha", "content_sha256", "heading_level", "permalink", "selector_version", "selection_reason_code", "char_count"):
        assert match[field]


def test_given_budget_exceeded_when_selected_then_no_full_document_fallback_is_returned():
    repo_root = Path(__file__).resolve().parents[4]
    matches, outcomes = select_section_matches(
        repo_root,
        [{"path": "docs/dev/workflow.md"}],
        ["Scope Collision"],
        1,
    )
    assert matches == []
    assert any(outcome["reason_code"] == "section_budget_exceeded" for outcome in outcomes)
