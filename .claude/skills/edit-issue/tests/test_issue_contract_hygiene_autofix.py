"""
Tests for issue_contract_hygiene_autofix.py

Tests cover:
  - C4: $ prefix added to command lines in fenced bash blocks within Verification Commands
  - C9: ## Runtime Verification Applicability section inserted when missing + non-runtime paths
  - body_sha256 guard: exit 1 when body unchanged
  - exit 2 cases: runtime paths / unknown paths / missing Allowed Paths with missing C9
  - exit 2 case: non-C4/C9 blockers detected by check_issue_contract.py
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

# ---------------------------------------------------------------------------
# Minimal complete contract body helpers
# ---------------------------------------------------------------------------
# To pass check_issue_contract.py's non-C4/C9 blocker gate, fixture bodies must
# include the minimum required sections (Outcome, Acceptance Criteria, Stop Conditions,
# Verification Commands, Allowed Paths) with at least one $ command in VC.
#
# _MINIMAL_CONTRACT_SUFFIX is appended to test bodies that need to satisfy the
# structural requirements without overriding what we're testing.

_MINIMAL_SUFFIX = textwrap.dedent("""\
    ## Outcome
    Test outcome.

    ## Acceptance Criteria
    - [ ] AC1: file exists

    ## Stop Conditions
    - エラー時は停止する。
""")


def _wrap_body(vc_block: str, allowed_paths: str, rva_block: str = "", suffix: str = _MINIMAL_SUFFIX) -> str:
    """Assemble a minimal valid contract body."""
    parts = [suffix]
    parts.append(f"## Verification Commands\n\n{vc_block}\n")
    parts.append(f"## Allowed Paths\n{allowed_paths}\n")
    if rva_block:
        parts.append(rva_block)
    parts.append("## Delivery Rule\n1 Issue = 1 PR\n")
    return "\n".join(parts)


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
# Shared fixture bodies (complete contract structure)
# ---------------------------------------------------------------------------

_RVA_BLOCK = textwrap.dedent("""\
    ## Runtime Verification Applicability
    ```yaml
    decision: not_applicable
    reason: "test"
    ```
""")

# C4 tests: body already has RVA so C9 doesn't trigger
C4_BODY_NEEDS_REPAIR = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
        ```"""),
    allowed_paths="- .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py\n",
    rva_block=_RVA_BLOCK,
)

C4_BODY_ALREADY_FIXED = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py
        ```"""),
    allowed_paths="- .claude/skills/edit-issue/scripts/issue_contract_hygiene_autofix.py\n",
    rva_block=_RVA_BLOCK,
)

# C9 tests: body has $ prefix already (no C4 issue), but missing RVA
C9_BODY_MISSING_RVA = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f .claude/agents/issue-author.md
        ```"""),
    allowed_paths="- .claude/agents/issue-author.md\n- .claude/skills/edit-issue/SKILL.md\n",
    rva_block="",
)

C9_BODY_WITH_RUNTIME_PATHS = _wrap_body(
    vc_block=textwrap.dedent("""\
        ```bash
        # AC1
        $ test -f src/main.ts
        ```"""),
    allowed_paths="- src/main.ts\n- .claude/agents/issue-author.md\n",
    rva_block="",
)


# ---------------------------------------------------------------------------
# C4 Tests
# ---------------------------------------------------------------------------

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
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            ISSUE_NUMBER=573
            test -f some_file
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=_RVA_BLOCK,
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ ISSUE_NUMBER" not in out
    assert "ISSUE_NUMBER=573" in out
    assert "$ test -f" in out


def test_c4_only_in_vc_section():
    """GIVEN bash blocks outside ## Verification Commands / WHEN autofix runs / THEN those are not modified."""
    # Construct body with a Background section containing a bash block
    body = textwrap.dedent("""\
        ## Outcome
        Test outcome.

        ## Acceptance Criteria
        - [ ] AC1: file exists

        ## Stop Conditions
        - エラー時は停止する。

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
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    # Outside VC section: not modified
    assert "$ some_command_outside_vc" not in out
    # Inside VC section: modified
    assert "$ test -f some_file" in out


# ---------------------------------------------------------------------------
# C9 Tests
# ---------------------------------------------------------------------------

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


def test_c9_skips_runtime_paths_returns_exit2():
    """GIVEN missing RVA section with runtime path (src/) / WHEN autofix runs / THEN exit 2 (not_autofixable)."""
    code, out, err = run_autofix(C9_BODY_WITH_RUNTIME_PATHS)
    # Runtime paths → not safe to autofix → exit 2
    assert code == 2, f"Expected exit 2 (not_autofixable), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_unknown_path_returns_exit2():
    """GIVEN missing RVA section with .github/workflows path (not in whitelist) / WHEN autofix runs / THEN exit 2."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            $ test -f .github/workflows/ci.yml
            ```"""),
        allowed_paths="- .github/workflows/ci.yml\n",
        rva_block="",
    )
    code, out, err = run_autofix(body)
    # .github/workflows is not in the non-runtime whitelist → exit 2
    assert code == 2, f"Expected exit 2 (unknown path), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_missing_allowed_paths_returns_exit2():
    """GIVEN missing RVA section and missing ## Allowed Paths section / WHEN autofix runs / THEN exit 2."""
    body = textwrap.dedent("""\
        ## Outcome
        Test outcome.

        ## Acceptance Criteria
        - [ ] AC1: file exists

        ## Stop Conditions
        - エラー時は停止する。

        ## Verification Commands

        ```bash
        # AC1
        $ test -f .claude/agents/issue-author.md
        ```

        ## Delivery Rule
        1 Issue = 1 PR
    """)
    code, out, err = run_autofix(body)
    # No Allowed Paths section → cannot safely classify → exit 2
    assert code == 2, f"Expected exit 2 (missing Allowed Paths), got {code}. stderr={err}"
    assert "## Runtime Verification Applicability" not in out


def test_c9_no_duplicate_if_already_present():
    """GIVEN existing RVA section / WHEN autofix runs / THEN no duplicate section added."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            $ test -f .claude/agents/issue-author.md
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=textwrap.dedent("""\
            ## Runtime Verification Applicability
            ```yaml
            decision: not_applicable
            reason: "already present"
            ```
        """),
    )
    code, out, err = run_autofix(body)
    # No changes needed → exit 1
    assert code == 1, f"Expected exit 1 (no_change), got {code}. stderr={err}"
    assert out.count("## Runtime Verification Applicability") == 0  # stdout empty on exit 1


# ---------------------------------------------------------------------------
# Combined C4 + C9 repair
# ---------------------------------------------------------------------------

def test_combined_c4_and_c9_repair():
    """GIVEN both C4 (missing $) and C9 (missing RVA) issues / WHEN autofix runs / THEN both repaired."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            test -f .claude/agents/issue-author.md
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n- .claude/skills/edit-issue/SKILL.md\n",
        rva_block="",
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ test -f" in out
    assert "## Runtime Verification Applicability" in out
    assert "decision: not_applicable" in out


# ---------------------------------------------------------------------------
# sha256 guard (no_change)
# ---------------------------------------------------------------------------

def test_no_change_returns_exit1():
    """GIVEN body with no issues / WHEN autofix runs / THEN exit 1 (no_change)."""
    code, out, err = run_autofix(C4_BODY_ALREADY_FIXED)
    assert code == 1, f"Expected exit 1, got {code}. stderr={err}"
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
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ test -f" in out


# ---------------------------------------------------------------------------
# Continuation line handling
# ---------------------------------------------------------------------------

def test_c4_skips_continuation_lines():
    """GIVEN a multi-line command with backslash continuation / WHEN autofix runs / THEN continuation lines not prefixed."""
    body = _wrap_body(
        vc_block=textwrap.dedent("""\
            ```bash
            # AC1
            some_command \\
              --arg1 \\
              --arg2
            ```"""),
        allowed_paths="- .claude/agents/issue-author.md\n",
        rva_block=_RVA_BLOCK,
    )
    code, out, err = run_autofix(body)
    assert code == 0, f"Expected exit 0, got {code}. stderr={err}"
    assert "$ some_command \\" in out
    # Continuation lines should not be prefixed
    assert "$   --arg1" not in out
    assert "$   --arg2" not in out
