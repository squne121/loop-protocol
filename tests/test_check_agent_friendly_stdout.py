"""
test_check_agent_friendly_stdout.py - Tests for check_agent_friendly_stdout.py (AC4).

Verifies:
- 2049+ UTF-8 bytes → fail
- raw diff (diff --git / @@ hunk) → fail
- raw log (Traceback / npm ERR!) → fail
- ANSI escape sequences → fail
- Japanese text with <= 2048 UTF-8 bytes → pass
- Clean compact output → pass
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "agent-stdout"

sys.path.insert(0, str(SCRIPTS_DIR))

from check_agent_friendly_stdout import check_stdout


# ---------------------------------------------------------------------------
# check_stdout function unit tests
# ---------------------------------------------------------------------------


class TestCheckStdoutByteLimit:
    def test_exactly_2048_bytes_passes(self):
        """GIVEN exactly 2048 ASCII bytes WHEN check_stdout THEN no byte violation."""
        text = "x" * 2048
        violations = check_stdout(text, max_bytes=2048)
        byte_violations = [v for v in violations if "BYTE_LIMIT" in v]
        assert byte_violations == []

    def test_2049_bytes_fails(self):
        """GIVEN 2049 ASCII bytes WHEN check_stdout THEN byte limit violation."""
        text = "x" * 2049
        violations = check_stdout(text, max_bytes=2048)
        assert any("BYTE_LIMIT_EXCEEDED" in v for v in violations)

    def test_japanese_text_under_2048_bytes_passes(self):
        """GIVEN Japanese text < 2048 UTF-8 bytes WHEN check_stdout THEN no byte violation."""
        # Japanese text: each char is 3 bytes in UTF-8
        # 100 chars * 3 bytes = 300 bytes, well under 2048
        text = "STATUS: ok\nVERDICT: approve\nSUMMARY: 日本語テキスト（完了）\nNEXT_ACTION: proceed\n"
        byte_count = len(text.encode("utf-8"))
        assert byte_count < 2048, f"fixture too large: {byte_count}"
        violations = check_stdout(text, max_bytes=2048)
        byte_violations = [v for v in violations if "BYTE_LIMIT" in v]
        assert byte_violations == []

    def test_japanese_text_over_2048_bytes_fails(self):
        """GIVEN Japanese text > 2048 UTF-8 bytes WHEN check_stdout THEN byte limit violation."""
        # Each Japanese char is 3 UTF-8 bytes, so 700 chars = 2100 bytes
        text = "あ" * 700
        byte_count = len(text.encode("utf-8"))
        assert byte_count > 2048
        violations = check_stdout(text, max_bytes=2048)
        assert any("BYTE_LIMIT_EXCEEDED" in v for v in violations)


class TestCheckStdoutRawDiff:
    def test_diff_git_header_fails(self):
        """GIVEN text with 'diff --git' WHEN check_stdout THEN raw diff violation."""
        text = "STATUS: ok\ndiff --git a/foo.py b/foo.py\n"
        violations = check_stdout(text)
        assert any("RAW_DIFF" in v for v in violations)

    def test_hunk_header_fails(self):
        """GIVEN text with '@@ -1' WHEN check_stdout THEN raw diff violation."""
        text = "STATUS: ok\n@@ -1,3 +1,4 @@\n"
        violations = check_stdout(text)
        assert any("RAW_DIFF" in v for v in violations)

    def test_clean_text_no_diff_violation(self):
        """GIVEN clean text without diff markers WHEN check_stdout THEN no diff violation."""
        text = "STATUS: ok\nVERDICT: approve\n"
        violations = check_stdout(text)
        diff_violations = [v for v in violations if "RAW_DIFF" in v]
        assert diff_violations == []


class TestCheckStdoutRawLog:
    def test_traceback_fails(self):
        """GIVEN text with 'Traceback (most recent call last)' WHEN check_stdout THEN raw log violation."""
        text = "STATUS: failed\nTraceback (most recent call last):\n  File 'foo.py', line 1\n"
        violations = check_stdout(text)
        assert any("RAW_LOG" in v for v in violations)

    def test_npm_err_fails(self):
        """GIVEN text with 'npm ERR!' WHEN check_stdout THEN raw log violation."""
        text = "STATUS: failed\nnpm ERR! code ENOENT\n"
        violations = check_stdout(text)
        assert any("RAW_LOG" in v for v in violations)

    def test_clean_text_no_log_violation(self):
        """GIVEN clean text without log markers WHEN check_stdout THEN no log violation."""
        text = "STATUS: ok\nSUMMARY: contract ready\n"
        violations = check_stdout(text)
        log_violations = [v for v in violations if "RAW_LOG" in v]
        assert log_violations == []


class TestCheckStdoutAnsiEscape:
    def test_ansi_color_code_fails(self):
        """GIVEN text with ANSI color escape WHEN check_stdout THEN ANSI violation."""
        text = "STATUS: ok\n\x1b[32mVERDICT: approve\x1b[0m\n"
        violations = check_stdout(text)
        assert any("ANSI_ESCAPE" in v for v in violations)

    def test_clean_text_no_ansi_violation(self):
        """GIVEN clean text without ANSI WHEN check_stdout THEN no ANSI violation."""
        text = "STATUS: ok\nVERDICT: approve\n"
        violations = check_stdout(text)
        ansi_violations = [v for v in violations if "ANSI_ESCAPE" in v]
        assert ansi_violations == []


class TestCheckStdoutOverall:
    def test_fully_compliant_output_passes(self):
        """GIVEN fully compliant compact output WHEN check_stdout THEN empty violations."""
        text = (
            "STATUS: ok\n"
            "VERDICT: approve\n"
            "SUMMARY: contract ready\n"
            "BLOCKERS: 0\n"
            "NEXT_ACTION: proceed\n"
            "ARTIFACT: compact_review_result_v1=.claude/artifacts/issue-refinement-loop/42/result.json\n"
        )
        violations = check_stdout(text, max_bytes=2048)
        assert violations == []

    def test_multiple_violations_reported(self):
        """GIVEN text with multiple violations WHEN check_stdout THEN all reported."""
        # Over byte limit + raw diff
        text = "x" * 2049 + "\ndiff --git a/foo b/foo\n"
        violations = check_stdout(text, max_bytes=2048)
        assert any("BYTE_LIMIT" in v for v in violations)
        assert any("RAW_DIFF" in v for v in violations)


# ---------------------------------------------------------------------------
# Fixture file tests (integration with fixture files)
# ---------------------------------------------------------------------------


class TestCheckStdoutFixtures:
    def test_compliant_japanese_fixture_passes(self):
        """GIVEN compliant_japanese.txt fixture WHEN check_stdout THEN passes."""
        fixture = FIXTURES_DIR / "compliant_japanese.txt"
        text = fixture.read_text(encoding="utf-8")
        violations = check_stdout(text, max_bytes=2048)
        assert violations == [], f"Expected no violations, got: {violations}"

    def test_too_long_fixture_fails(self):
        """GIVEN too_long.txt fixture (2049 bytes) WHEN check_stdout THEN byte limit fail."""
        fixture = FIXTURES_DIR / "too_long.txt"
        text = fixture.read_text(encoding="utf-8")
        violations = check_stdout(text, max_bytes=2048)
        assert any("BYTE_LIMIT_EXCEEDED" in v for v in violations)

    def test_raw_diff_fixture_fails(self):
        """GIVEN raw_diff.txt fixture WHEN check_stdout THEN raw diff fail."""
        fixture = FIXTURES_DIR / "raw_diff.txt"
        text = fixture.read_text(encoding="utf-8")
        violations = check_stdout(text, max_bytes=2048)
        assert any("RAW_DIFF" in v for v in violations)

    def test_raw_log_fixture_fails(self):
        """GIVEN raw_log.txt fixture WHEN check_stdout THEN raw log fail."""
        fixture = FIXTURES_DIR / "raw_log.txt"
        text = fixture.read_text(encoding="utf-8")
        violations = check_stdout(text, max_bytes=2048)
        assert any("RAW_LOG" in v for v in violations)

    def test_ansi_escape_fixture_fails(self):
        """GIVEN ansi_escape.txt fixture WHEN check_stdout THEN ANSI escape fail."""
        fixture = FIXTURES_DIR / "ansi_escape.txt"
        text = fixture.read_text(encoding="utf-8")
        violations = check_stdout(text, max_bytes=2048)
        assert any("ANSI_ESCAPE" in v for v in violations)


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCheckStdoutCLI:
    def test_cli_compliant_exits_0(self):
        """GIVEN compliant fixture WHEN CLI invoked THEN exit 0 and PASS printed."""
        script = SCRIPTS_DIR / "check_agent_friendly_stdout.py"
        fixture = FIXTURES_DIR / "compliant_japanese.txt"
        result = subprocess.run(
            [sys.executable, str(script), str(fixture)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "PASS" in result.stdout

    def test_cli_raw_diff_exits_1(self):
        """GIVEN raw_diff fixture WHEN CLI invoked THEN exit 1 and FAIL printed."""
        script = SCRIPTS_DIR / "check_agent_friendly_stdout.py"
        fixture = FIXTURES_DIR / "raw_diff.txt"
        result = subprocess.run(
            [sys.executable, str(script), str(fixture)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "FAIL:" in result.stdout

    def test_cli_too_long_exits_1(self):
        """GIVEN too_long fixture WHEN CLI invoked THEN exit 1."""
        script = SCRIPTS_DIR / "check_agent_friendly_stdout.py"
        fixture = FIXTURES_DIR / "too_long.txt"
        result = subprocess.run(
            [sys.executable, str(script), str(fixture)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1

    def test_cli_missing_file_exits_2(self):
        """GIVEN nonexistent file WHEN CLI invoked THEN exit 2."""
        script = SCRIPTS_DIR / "check_agent_friendly_stdout.py"
        result = subprocess.run(
            [sys.executable, str(script), "/tmp/nonexistent_fixture_file_xyz.txt"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2

    def test_cli_max_bytes_option(self):
        """GIVEN --max-bytes 100 and long file WHEN CLI invoked THEN exit 1."""
        script = SCRIPTS_DIR / "check_agent_friendly_stdout.py"
        fixture = FIXTURES_DIR / "compliant_japanese.txt"
        result = subprocess.run(
            [sys.executable, str(script), str(fixture), "--max-bytes", "10"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "BYTE_LIMIT_EXCEEDED" in result.stdout
