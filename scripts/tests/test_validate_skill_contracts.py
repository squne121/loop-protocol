"""Tests for scripts/validate_skill_contracts.py (Issue #2030)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate_skill_contracts.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_skill_contracts as vsc  # noqa: E402


def test_script_exists():
    assert SCRIPT.is_file()


def test_current_tracked_skill_md_has_no_invalid_frontmatter():
    report = vsc.audit(REPO_ROOT)
    assert report["tracked_skill_md_count"] > 0
    assert report["invalid_frontmatter_count"] == 0, report["files"]


def test_missing_frontmatter_field_detected(tmp_path):
    skill_dir = tmp_path / ".claude" / "skills" / "fixture-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fixture-skill\n---\n\n# Fixture\n", encoding="utf-8"
    )
    frontmatter, error = vsc.parse_frontmatter(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    )
    assert error is None
    assert frontmatter is not None
    assert not frontmatter.get("description")


def test_no_frontmatter_block_detected():
    frontmatter, error = vsc.parse_frontmatter("# No frontmatter here\n")
    assert frontmatter is None
    assert error == "no_frontmatter_block"


def test_invalid_yaml_frontmatter_detected():
    text = "---\nname: [unterminated\n---\n\nbody\n"
    frontmatter, error = vsc.parse_frontmatter(text)
    assert frontmatter is None
    assert error is not None
    assert "yaml_parse_error" in error


def test_broken_reference_detected(tmp_path):
    (tmp_path / "scripts").mkdir()
    body = (
        "---\nname: x\ndescription: y\n---\n\n"
        "See `scripts/does_not_exist.py` for details.\n"
    )
    refs = vsc.extract_references(body)
    assert "scripts/does_not_exist.py" in refs
    assert not (tmp_path / "scripts/does_not_exist.py").exists()


def test_existing_reference_not_flagged_broken(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "real.py").write_text("# real\n", encoding="utf-8")
    body = "See `scripts/real.py` for details.\n"
    refs = vsc.extract_references(body)
    assert refs == ["scripts/real.py"]
    assert (tmp_path / "scripts/real.py").exists()


def test_cli_json_output_runs_successfully():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "tracked_skill_md_count" in result.stdout
    assert "broken_reference_count" in result.stdout
