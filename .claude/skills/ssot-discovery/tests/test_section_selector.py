import hashlib
import subprocess
import sys
from pathlib import Path


SKILLS_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILLS_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from match_ssot import parse_markdown_sections, select_section_matches  # noqa: E402


def test_given_fenced_atx_and_setext_when_parsed_then_only_real_headings_are_selected():
    text = "# Top\n\n```markdown\n## Ignored\n```\n\nReal Setext\n----------\nbody\n\n## Child\nchild\n\n# Next\n"
    sections = parse_markdown_sections(text)
    assert [(section["heading"], section["heading_level"]) for section in sections] == [
        ("Top", 1), ("Real Setext", 2), ("Child", 2), ("Next", 1),
    ]
    assert sections[1]["end_line_exclusive"] == sections[2]["start_line"]


def test_given_fence_marker_and_length_mismatch_when_parsed_then_fence_stays_open():
    text = (
        "# Top\n\n````markdown\n## Ignored\n```\n~~~\n## Also ignored\n\n````\n\n"
        "~~~\n## Tilde ignored\n```\n## Still ignored\n~~~\n\n## Real\n"
    )
    sections = parse_markdown_sections(text)
    assert [section["heading"] for section in sections] == ["Top", "Real"]


def test_given_atx_closing_sequence_when_parsed_then_only_whitespace_prefixed_hashes_removed():
    sections = parse_markdown_sections("# C#\n# Title #\n")
    assert [section["heading"] for section in sections] == ["C#", "Title"]


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
    fields = (
        "source_commit", "blob_sha", "content_sha256", "heading_level",
        "permalink", "selector_version", "selection_reason_code", "char_count",
    )
    for field in fields:
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


def test_given_dirty_document_when_selected_then_evidence_uses_head_blob(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/selector.git"],
        cwd=tmp_path,
        check=True,
    )
    document = tmp_path / "document.md"
    head_text = "# Stable\nhead body\n"
    document.write_text(head_text, encoding="utf-8")
    subprocess.run(["git", "add", "document.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True, capture_output=True)
    document.write_text("# Stable\ndirty body\n", encoding="utf-8")

    matches, outcomes = select_section_matches(
        tmp_path, [{"path": "document.md"}], ["Stable"], 4_000
    )

    assert outcomes == [{"path": "document.md", "heading": "Stable", "reason_code": "selected"}]
    expected_hash = hashlib.sha256(head_text.encode()).hexdigest()
    assert matches[0]["content_sha256"] == f"sha256:{expected_hash}"
    assert matches[0]["blob_sha"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD:document.md"], cwd=tmp_path, text=True
    ).strip()
    assert matches[0]["permalink"].endswith("/document.md#L1-L2")


def test_given_uncommitted_document_when_selected_then_no_worktree_fallback_is_used(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.md").write_text("# Tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "uncommitted.md").write_text("# Uncommitted\n", encoding="utf-8")

    matches, outcomes = select_section_matches(
        tmp_path, [{"path": "uncommitted.md"}], ["Uncommitted"], 4_000
    )

    assert matches == []
    assert outcomes == [{"path": "uncommitted.md", "reason_code": "source_blob_unavailable"}]
