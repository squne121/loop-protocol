"""
Tests for issue_contract_hygiene_autofix.py

Tests cover:
  - C4: $ prefix added to command lines in fenced bash blocks within Verification Commands
  - C9: ## Runtime Verification Applicability section inserted when missing + non-runtime paths
  - body_sha256 guard: exit 1 when body unchanged
  - exit 2 cases: runtime paths with missing C9
  - Combined C4+C9 repair
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "issue_contract_hygiene_autofix.py"
)


def run_autofix(body: str, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """Run autofix script with given body as stdin. Returns (exit_code, stdout, stderr)."""
    args = [sys.executable, str(SCRIPT)] + (extra_args or [])
    result = subprocess.run(
        args,
        input=body,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_autofix_file(body: str, tmp_path: Path) -> tuple[int, str, str]:
    """Run autofix script with --body-file and --out-file."""
    in_file = tmp_path / "body_in.md"
    out_file = tmp_path / "body_out.md"
    in_file.write_text(body, encoding="utf-8")
    args = [
        sys.executable,
        str(SCRIPT),
        "--body-file",
        str(in_file),
        "--out-file",
        str(out_file),
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    if out_file.exists():
        return result.returncode, out_file.read_text(encoding="utf-8"), result.stderr
    return result.returncode, "", result.stderr


# ---------------------------------------------------------------------------
# C4 Tests
# ---------------------------------------------------------------------------

C4_BODY_NEEDS_REPAIR = textwrap.dedent("""\
    ## Verification Commands

    ```bash
    # AC1
    test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
    ```

    ## Allowed Paths
    - .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py

    ## Runtime Verification Applicability
    ```yaml
    decision: not_applicable
    reason: "test"
    ```

    ## Delivery Rule
    1 Issue = 1 PR
""")

C4_BODY_ALREADY_FIXED = textwrap.dedent("""\
    ## Verification Commands

    ```bash
    # AC1
    $ test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
    ```

    ## Allowed Paths
    - .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py

    ## Runtime Verification Applicability
    ```yaml
    decision: not_applicable
    reason: "test"
    ```

    ## Delivery Rule
    1 Issue = 1 PR
""")


def test_c4_adds_dollar_prefix_to_command_line():
    """GIVEN a fenced bash VC block with a command missing $ / WHEN autofix runs / THEN $ prefix is added."""
    code, out, err = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert code == 0, f"Expected exit 0 (repaired), got {code}. stderr={err}"
    assert "$ test -f" in out


def test_c4_does_not_add_prefix_to_comment_line():
    """GIVEN a # comment line in bash block / WHEN autofix runs / THEN comment line is unchanged."""
    code, out, err = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert "# AC1" in out
    assert "$ # AC1" not in out


def test_c4_skips_already_prefixed_line():
    """GIVEN a line already prefixed with $ / WHEN autofix runs / THEN no double-prefix."""
    code, out, err = run_autofix(C4_BODY_ALREADY_FIXED)
    # Body already fixed → no C4 repair → exit 1 (no_change)
    assert code == 1, f"Expected exit 1 (no_change), got {code}. stderr={err}"
    assert "$ $ " not in out


def test_c4_skips_shell_variable_assignment():
    """GIVEN a shell variable assignment line in bash block / WHEN autofix runs / THEN no $ prefix."""
    body = textwrap.dedent("""\
        ## Verification Commands

        ```bash
        # AC1
        ISSUE_NUMBER=573
        test -f some_file
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md

        ## Runtime Verification Applicability
        ```yaml
        decision: not_applicable
        reason: "test"
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    assert code == 0
    assert "$ ISSUE_NUMBER" not in out
    assert "ISSUE_NUMBER=573" in out
    assert "$ test -f" in out


def test_c4_only_in_vc_section():
    """GIVEN bash blocks outside ## Verification Commands / WHEN autofix runs / THEN those are not modified."""
    body = textwrap.dedent("""\
        ## Background

        ```bash
        some_command_outside_vc
        ```

        ## Verification Commands

        ```bash
        # AC1
        test -f some_file
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md

        ## Runtime Verification Applicability
        ```yaml
        decision: not_applicable
        reason: "test"
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    assert code == 0
    # Outside VC section: not modified
    assert "$ some_command_outside_vc" not in out
    # Inside VC section: modified
    assert "$ test -f some_file" in out


# ---------------------------------------------------------------------------
# C9 Tests
# ---------------------------------------------------------------------------

C9_BODY_MISSING_RVA = textwrap.dedent("""\
    ## Verification Commands

    ```bash
    # AC1
    $ test -f .claude/agents/issue-author.md
    ```

    ## Allowed Paths
    - .claude/agents/issue-author.md
    - .claude/skills/edit-issue/SKILL.md

    ## Delivery Rule
    1 Issue = 1 PR
""")

C9_BODY_WITH_RUNTIME_PATHS = textwrap.dedent("""\
    ## Verification Commands

    ```bash
    # AC1
    $ test -f src/main.ts
    ```

    ## Allowed Paths
    - src/main.ts
    - .claude/agents/issue-author.md

    ## Delivery Rule
    1 Issue = 1 PR
""")


def test_c9_inserts_rva_section_for_non_runtime_paths():
    """GIVEN missing RVA section with non-runtime Allowed Paths / WHEN autofix runs / THEN RVA section inserted."""
    code, out, err = run_autofix(C9_BODY_MISSING_RVA)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" in out
    assert "decision: not_applicable" in out


def test_c9_inserted_before_delivery_rule():
    """GIVEN missing RVA / WHEN autofix runs / THEN RVA section appears before ## Delivery Rule."""
    code, out, err = run_autofix(C9_BODY_MISSING_RVA)
    assert code == 0
    rva_pos = out.find("## Runtime Verification Applicability")
    delivery_pos = out.find("## Delivery Rule")
    assert rva_pos != -1
    assert delivery_pos != -1
    assert rva_pos < delivery_pos


def test_c9_skips_runtime_paths_returns_exit1():
    """GIVEN missing RVA section with runtime path (src/) / WHEN autofix runs / THEN exit 1 (no repair)."""
    code, out, err = run_autofix(C9_BODY_WITH_RUNTIME_PATHS)
    # Should not insert RVA, and since no C4 repair either, exit 1
    assert code == 1, f"Expected exit 1, got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_no_duplicate_if_already_present():
    """GIVEN existing RVA section / WHEN autofix runs / THEN no duplicate section added."""
    body = textwrap.dedent("""\
        ## Verification Commands

        ```bash
        # AC1
        $ test -f .claude/agents/issue-author.md
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md

        ## Runtime Verification Applicability
        ```yaml
        decision: not_applicable
        reason: "already present"
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    # No changes needed → exit 1
    assert code == 1
    assert out.count("## Runtime Verification Applicability") == 0  # stdout empty on exit 1


# ---------------------------------------------------------------------------
# Combined C4 + C9 repair
# ---------------------------------------------------------------------------

def test_combined_c4_and_c9_repair():
    """GIVEN both C4 (missing $) and C9 (missing RVA) issues / WHEN autofix runs / THEN both repaired."""
    body = textwrap.dedent("""\
        ## Verification Commands

        ```bash
        # AC1
        test -f .claude/agents/issue-author.md
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md
        - .claude/skills/edit-issue/SKILL.md

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    assert code == 0
    assert "$ test -f" in out
    assert "## Runtime Verification Applicability" in out
    assert "decision: not_applicable" in out


# ---------------------------------------------------------------------------
# sha256 guard (no_change)
# ---------------------------------------------------------------------------

def test_no_change_returns_exit1():
    """GIVEN body with no issues / WHEN autofix runs / THEN exit 1 (no_change)."""
    code, out, err = run_autofix(C4_BODY_ALREADY_FIXED)
    assert code == 1
    assert "no_change" in err


def test_sha256_guard_idempotent(tmp_path: Path):
    """GIVEN repaired body / WHEN autofix runs again / THEN exit 1 (already repaired = no_change)."""
    # First run: should repair
    code1, out1, _ = run_autofix(C4_BODY_NEEDS_REPAIR)
    assert code1 == 0

    # Second run on repaired body: should be no_change
    code2, out2, err2 = run_autofix(out1)
    assert code2 == 1, f"Expected exit 1 (no_change on second run), got {code2}. stderr={err2}"


# ---------------------------------------------------------------------------
# --body-file / --out-file interface
# ---------------------------------------------------------------------------

def test_body_file_and_out_file_interface(tmp_path: Path):
    """GIVEN --body-file and --out-file args / WHEN autofix runs / THEN output written to out-file."""
    code, out, err = run_autofix_file(C4_BODY_NEEDS_REPAIR, tmp_path)
    assert code == 0
    assert "$ test -f" in out


# ---------------------------------------------------------------------------
# Continuation line handling
# ---------------------------------------------------------------------------

def test_c4_skips_continuation_lines():
    """GIVEN a multi-line command with backslash continuation / WHEN autofix runs / THEN continuation lines not prefixed."""
    body = textwrap.dedent("""\
        ## Verification Commands

        ```bash
        # AC1
        some_command \\
          --arg1 \\
          --arg2
        ```

        ## Allowed Paths
        - .claude/agents/issue-author.md

        ## Runtime Verification Applicability
        ```yaml
        decision: not_applicable
        reason: "test"
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    assert code == 0
    assert "$ some_command \\" in out
    # Continuation lines should not be prefixed
    assert "$   --arg1" not in out
    assert "$   --arg2" not in out
