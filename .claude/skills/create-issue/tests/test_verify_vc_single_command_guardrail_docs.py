"""
Tests for verify_vc_single_command_guardrail_docs.py

AC6: checker が違反時は非 0、成功時は 0 を返すことを検証する。
"""

import subprocess
import sys
import textwrap
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "verify_vc_single_command_guardrail_docs.py"


def _run_checker(
    tmp_path: Path,
    body_authoring_content: str,
    skill_md_content: str = ""
) -> subprocess.CompletedProcess:
    """Run the checker with synthetic test files."""
    repo_root = tmp_path

    # Create required directory structure
    ba_path = repo_root / ".claude/skills/create-issue/references/body-authoring.md"
    skill_path = repo_root / ".claude/skills/create-issue/SKILL.md"
    ba_path.parent.mkdir(parents=True, exist_ok=True)
    ba_path.write_text(body_authoring_content, encoding="utf-8")
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(skill_md_content, encoding="utf-8")

    return subprocess.run(
        [sys.executable, str(SCRIPT), "--strict", "--repo-root", str(repo_root)],
        capture_output=True,
        text=True,
    )


class TestCleanContent:
    """GIVEN clean content with no compound shell — WHEN checker runs — THEN exit 0."""

    def test_no_bash_blocks_passes(self, tmp_path):
        content = "# Doc\n\nSome text without code blocks.\n"
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_single_rg_command_passes(self, tmp_path):
        content = textwrap.dedent("""\
            ## VC examples

            ```bash
            rg -n "pattern" file.md
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0

    def test_test_f_single_command_passes(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            test -f <file>
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0

    def test_comment_line_with_angle_bracket_passes(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            # VC セクション内の # AC<n> コメント件数を数える
            rg -c "# AC[0-9]" issue_body.md
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0

    def test_operators_in_quoted_string_passes(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            rg -n "&&|\\|\\|" file.md
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0

    def test_plain_block_not_checked(self, tmp_path):
        """Plain ``` blocks (no language tag) are not checked."""
        content = textwrap.dedent("""\
            ```
            grep -q pattern file && echo PASS || echo FAIL
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0

    def test_sh_block_is_checked(self, tmp_path):
        """```sh blocks ARE checked."""
        content = textwrap.dedent("""\
            ```sh
            cmd1 && cmd2
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1

    def test_uv_run_single_command_passes(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            uv run python3 .claude/skills/create-issue/scripts/verify_vc_single_command_guardrail_docs.py --strict
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 0


class TestAndOperatorViolation:
    """GIVEN && in bash block — WHEN checker runs — THEN exit 1."""

    def test_and_and_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            test -f file && echo PASS
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1
        assert "&&" in result.stdout

    def test_classic_pass_fail_pattern_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            grep -q pattern file && echo "PASS" || echo "FAIL"
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1


class TestOrOperatorViolation:
    """GIVEN || in bash block — WHEN checker runs — THEN exit 1."""

    def test_or_or_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            rg -c "# AC[0-9]" issue_body.md || echo 0
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1
        assert "||" in result.stdout


class TestPipeOperatorViolation:
    """GIVEN | pipe in bash block — WHEN checker runs — THEN exit 1."""

    def test_pipe_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            rg -nA 20 "^## Heading" file.md | rg "pattern"
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1
        assert "|" in result.stdout


class TestSemicolonViolation:
    """GIVEN ; in bash block — WHEN checker runs — THEN exit 1."""

    def test_semicolon_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            cmd1; cmd2
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1
        assert ";" in result.stdout


class TestBackgroundOperatorViolation:
    """GIVEN & (single) in bash block — WHEN checker runs — THEN exit 1."""

    def test_background_fails(self, tmp_path):
        content = textwrap.dedent("""\
            ```bash
            long_running_cmd &
            ```
        """)
        result = _run_checker(tmp_path, content)
        assert result.returncode == 1
        assert "&" in result.stdout


class TestFileMissing:
    """GIVEN missing target file — WHEN checker runs — THEN still exits 0 (warn only)."""

    def test_missing_file_warns_but_passes(self, tmp_path):
        # Create only body-authoring.md, not SKILL.md
        repo_root = tmp_path
        ba_path = repo_root / ".claude/skills/create-issue/references/body-authoring.md"
        ba_path.parent.mkdir(parents=True, exist_ok=True)
        ba_path.write_text("# clean\n", encoding="utf-8")
        # skill path does NOT exist
        _skill_path = repo_root / ".claude/skills/create-issue/SKILL.md"
        # do not create it

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--strict", "--repo-root", str(repo_root)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "WARN" in result.stderr


class TestRealFiles:
    """GIVEN actual repo files — WHEN checker runs — THEN exit 0 (no violations)."""

    def test_real_repo_files_pass(self):
        """Integration test: verify actual files in repo pass the checker."""
        # Find repo root by locating CLAUDE.md
        repo_root = Path(__file__).parent
        while repo_root != repo_root.parent:
            if (repo_root / "CLAUDE.md").exists():
                break
            repo_root = repo_root.parent

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--strict", "--repo-root", str(repo_root)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "Real repo files have compound shell violations:\n" + result.stdout
        )
