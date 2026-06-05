#!/usr/bin/env python3
"""
Tests for VC format contract and unsafe command policy (#514).

AC1: $(...), backtick, ${...} → blocked / unsupported_shell_syntax (run_command NOT called)
AC2: shell/network/mutation/git-mutation/gh-mutation/pnpm-mutation → blocked / unsafe_command or command_not_allowed
AC3: allowlist-outside commands → blocked / command_not_allowed (default closed)
AC4: errors[] items have {kind, rule, message, minimal_context, fix_hint}
AC5: SKILL.md category table mentions unsupported_shell_syntax
AC6: all tests pass (this file)
AC7: SKILL.md mentions fenced bash as canonical VC format
"""

import json
import subprocess
import sys
import tempfile
import os
from pathlib import Path

import pytest

# Path to the script under test
SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "baseline_vc_preflight.py"
)

# Add script directory to path for direct imports
sys.path.insert(0, str(SCRIPT_PATH.parent))


def run_preflight(body_content: str, issue_num: int = 999) -> dict:
    """Run preflight on a string of body content via a temp file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body_content)
        fixture_file = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--body-file", fixture_file, "--issue", str(issue_num)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.stdout, f"No output from preflight: stderr={result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(fixture_file)


# ---------------------------------------------------------------------------
# AC1: Command substitution must be blocked (not executed)
# ---------------------------------------------------------------------------

def test_command_substitution_is_blocked_without_execution():
    """AC1: $(...)  → run_command NOT called; classification=blocked, category=unsupported_shell_syntax"""
    body = """## Verification Commands

```bash
# AC1
$ test "$(wc -l < /etc/passwd)" -ge 1
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "unsupported_shell_syntax", f"Expected unsupported_shell_syntax, got {r['category']}"
    assert r["decision"] == "blocked", f"Expected decision=blocked, got {r['decision']}"
    # exit_code must be None (not executed)
    assert r["exit_code"] is None, f"Expected exit_code=None (not executed), got {r['exit_code']}"


def test_backtick_is_blocked_without_execution():
    """AC1: backtick substitution → blocked / unsupported_shell_syntax, not executed"""
    body = """## Verification Commands

```bash
# AC1
$ echo `date`
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "unsupported_shell_syntax"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None, "Backtick command must not be executed"


def test_parameter_expansion_is_blocked_without_execution():
    """AC1: ${...} → blocked / unsupported_shell_syntax, not executed"""
    body = """## Verification Commands

```bash
# AC1
$ echo ${HOME}
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "unsupported_shell_syntax"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None, "${...} command must not be executed"


# ---------------------------------------------------------------------------
# AC2: Shell invocations, network, mutation, git/gh/pnpm mutations → blocked
# ---------------------------------------------------------------------------

def test_shell_invocation_is_blocked():
    """AC2: bash, sh, zsh → blocked / unsafe_command"""
    for shell in ["bash", "sh", "zsh"]:
        body = f"""## Verification Commands

```bash
# AC1
$ {shell} -c "echo hello"
```
"""
        data = run_preflight(body)
        results = data["results"]
        assert len(results) == 1, f"Expected 1 result for {shell}"
        r = results[0]
        assert r["classification"] == "blocked", f"{shell}: expected blocked, got {r['classification']}"
        assert r["category"] == "unsafe_command", f"{shell}: expected unsafe_command, got {r['category']}"
        assert r["decision"] == "blocked"
        assert r["exit_code"] is None, f"{shell}: must not be executed"


def test_python_dash_c_is_blocked():
    """AC2: python -c and python3 -c → blocked / unsafe_command"""
    for cmd in ["python -c 'print(1)'", "python3 -c 'print(1)'"]:
        body = f"""## Verification Commands

```bash
# AC1
$ {cmd}
```
"""
        data = run_preflight(body)
        results = data["results"]
        assert len(results) == 1, f"Expected 1 result for '{cmd}'"
        r = results[0]
        assert r["classification"] == "blocked", f"'{cmd}': expected blocked, got {r['classification']}"
        assert r["category"] == "unsafe_command", f"'{cmd}': expected unsafe_command, got {r['category']}"
        assert r["decision"] == "blocked"
        assert r["exit_code"] is None, f"'{cmd}': must not be executed"


def test_network_commands_are_blocked():
    """AC2: curl, wget → blocked / unsafe_command"""
    for cmd in ["curl https://example.com", "wget https://example.com"]:
        body = f"""## Verification Commands

```bash
# AC1
$ {cmd}
```
"""
        data = run_preflight(body)
        results = data["results"]
        assert len(results) == 1, f"Expected 1 result for '{cmd}'"
        r = results[0]
        assert r["classification"] == "blocked", f"'{cmd}': expected blocked"
        assert r["category"] == "unsafe_command", f"'{cmd}': expected unsafe_command"
        assert r["decision"] == "blocked"
        assert r["exit_code"] is None


def test_git_mutation_is_blocked():
    """AC2: git push, git commit → blocked (command_not_allowed via exact allowlist)"""
    for git_cmd in ["git push origin main", "git commit -m 'test'"]:
        body = f"""## Verification Commands

```bash
# AC1
$ {git_cmd}
```
"""
        data = run_preflight(body)
        results = data["results"]
        assert len(results) == 1, f"Expected 1 result for '{git_cmd}'"
        r = results[0]
        assert r["classification"] == "blocked", f"'{git_cmd}': expected blocked"
        # B3: git uses exact read-only allowlist; mutations return command_not_allowed
        assert r["category"] in ("unsafe_command", "command_not_allowed"), f"'{git_cmd}': expected unsafe_command or command_not_allowed, got {r['category']}"
        assert r["decision"] == "blocked"
        assert r["exit_code"] is None, f"'{git_cmd}': must not be executed"


def test_filesystem_mutation_is_blocked():
    """AC2: rm, mv, cp, chmod → blocked / unsafe_command"""
    for cmd in ["rm -rf /tmp/test", "mv a.txt b.txt", "cp a.txt b.txt", "chmod 755 file.sh"]:
        body = f"""## Verification Commands

```bash
# AC1
$ {cmd}
```
"""
        data = run_preflight(body)
        results = data["results"]
        assert len(results) == 1, f"Expected 1 result for '{cmd}'"
        r = results[0]
        assert r["classification"] == "blocked", f"'{cmd}': expected blocked"
        assert r["category"] == "unsafe_command", f"'{cmd}': expected unsafe_command"
        assert r["decision"] == "blocked"
        assert r["exit_code"] is None


def test_gh_api_is_blocked():
    """AC2: gh api → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ gh api repos/owner/repo/issues
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_gh_issue_edit_is_blocked():
    """AC2: gh issue edit → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ gh issue edit 123 --title "new title"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_pnpm_add_is_blocked():
    """AC2: pnpm add → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ pnpm add lodash
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_npm_install_is_blocked():
    """AC2: npm install → blocked / unsafe_command (npm not in allowlist)"""
    body = """## Verification Commands

```bash
# AC1
$ npm install
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    # npm is denied via _DENIED_COMMANDS (unsafe_command) or command_not_allowed
    assert r["category"] in ("unsafe_command", "command_not_allowed"), f"Got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


# ---------------------------------------------------------------------------
# AC3: Unknown commands are blocked by default (allowlist-closed)
# ---------------------------------------------------------------------------

def test_unknown_command_is_blocked_by_default():
    """AC3: command not in allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ some_unknown_command --arg value
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


# ---------------------------------------------------------------------------
# AC4: errors[] items have structured schema {kind, rule, message, minimal_context, fix_hint}
# ---------------------------------------------------------------------------

def test_extraction_errors_are_structured():
    """AC4: errors[] items have {kind, rule, message, minimal_context, fix_hint}"""
    # Trigger extraction error by providing body without VC section
    body = """## Outcome

No VC section here.

## Acceptance Criteria

- [ ] AC1: something
"""
    data = run_preflight(body)
    assert data["status"] == "blocked"
    assert len(data["errors"]) > 0, "Expected errors array to be non-empty"

    for err in data["errors"]:
        assert isinstance(err, dict), f"Error should be dict, got {type(err)}: {err}"
        assert "kind" in err, f"Error missing 'kind' field: {err}"
        assert "rule" in err, f"Error missing 'rule' field: {err}"
        assert "message" in err, f"Error missing 'message' field: {err}"
        assert "minimal_context" in err, f"Error missing 'minimal_context' field: {err}"
        assert "fix_hint" in err, f"Error missing 'fix_hint' field: {err}"
        # Check types
        assert isinstance(err["kind"], str), f"'kind' should be string: {err}"
        assert isinstance(err["rule"], str), f"'rule' should be string: {err}"
        assert isinstance(err["message"], str), f"'message' should be string: {err}"
        assert isinstance(err["minimal_context"], str), f"'minimal_context' should be string: {err}"
        assert isinstance(err["fix_hint"], str), f"'fix_hint' should be string: {err}"


def test_error_schema_rejects_string_errors():
    """AC4: errors[] must NOT contain plain strings; must be structured objects"""
    # Trigger retrieval error
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--body-file", "/nonexistent_file_xyz.md", "--issue", "999"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    assert len(data["errors"]) > 0

    for err in data["errors"]:
        assert not isinstance(err, str), (
            f"errors[] must not contain plain strings; got string: '{err}'. "
            "All error items must be {{kind, rule, message, minimal_context, fix_hint}} objects."
        )
        assert isinstance(err, dict), f"Error must be a dict object, got {type(err)}"


def test_no_commands_error_is_structured():
    """AC4: no-commands-extracted error also uses structured schema"""
    body = """## Verification Commands

No commands here, just text.
"""
    data = run_preflight(body)
    assert data["status"] == "blocked"
    assert len(data["errors"]) > 0
    err = data["errors"][0]
    assert isinstance(err, dict)
    assert "kind" in err
    assert "rule" in err
    assert "message" in err
    assert "minimal_context" in err
    assert "fix_hint" in err


# ---------------------------------------------------------------------------
# AC5: SKILL.md category table mentions unsupported_shell_syntax
# ---------------------------------------------------------------------------

def test_skill_category_table_mentions_unsupported_shell_syntax():
    """AC5: SKILL.md category table contains unsupported_shell_syntax"""
    skill_md = Path(__file__).parent.parent / "SKILL.md"
    assert skill_md.exists(), f"SKILL.md not found at {skill_md}"
    content = skill_md.read_text(encoding="utf-8")
    assert "unsupported_shell_syntax" in content, (
        "SKILL.md category table must mention 'unsupported_shell_syntax'; "
        f"not found in {skill_md}"
    )


def test_script_category_enum_includes_unsupported_shell_syntax():
    """AC5: baseline_vc_preflight.py returns unsupported_shell_syntax category"""
    body = """## Verification Commands

```bash
# AC1
$ echo $(date)
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    assert results[0]["category"] == "unsupported_shell_syntax"


# ---------------------------------------------------------------------------
# AC7: SKILL.md mentions fenced bash block as canonical VC format
# ---------------------------------------------------------------------------

def test_skill_md_mentions_canonical_fenced_bash():
    """AC7: SKILL.md states that fenced bash block is canonical VC format"""
    skill_md = Path(__file__).parent.parent / "SKILL.md"
    assert skill_md.exists(), f"SKILL.md not found at {skill_md}"
    content = skill_md.read_text(encoding="utf-8")

    # Check for canonical format description
    # Accepts either English or Japanese phrasing
    canonical_markers = [
        "canonical VC format",
        "canonical",
        "fenced bash",
    ]
    found = sum(1 for marker in canonical_markers if marker.lower() in content.lower())
    assert found >= 2, (
        f"SKILL.md must mention fenced bash block as canonical VC format. "
        f"Found {found}/{len(canonical_markers)} markers in {skill_md}. "
        "Expected at least 2 of: " + str(canonical_markers)
    )


# ---------------------------------------------------------------------------
# Additional regression tests: allowed commands should still work
# ---------------------------------------------------------------------------

def test_rg_command_is_allowed():
    """Regression: rg (ripgrep) is in allowlist and proceeds to execution"""
    body = """## Verification Commands

```bash
# AC1
$ rg "nonexistent_pattern_xyz12345" .
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    # rg with no match = expected_fail / go (not blocked by command policy)
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"rg should be allowed, but got category={r['category']}"
    )


def test_test_command_is_allowed():
    """Regression: test -f is in allowlist"""
    body = """## Verification Commands

```bash
# AC1
$ test -f /nonexistent_file_xyz
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"test should be allowed, but got category={r['category']}"
    )


def test_uv_run_pytest_is_allowed():
    """Regression: uv run pytest is in allowlist"""
    body = """## Verification Commands

```bash
# AC1
$ uv run pytest .claude/skills/issue-contract-review/tests/ -v -k "nonexistent_test_xyz"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"uv run pytest should be allowed, but got category={r['category']}"
    )


def test_pnpm_typecheck_is_allowed():
    """Regression: pnpm typecheck is in allowlist"""
    body = """## Verification Commands

```bash
# AC1
$ pnpm typecheck
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"pnpm typecheck should be allowed, but got category={r['category']}"
    )


def test_grep_command_is_allowed():
    """Regression: grep is in allowlist"""
    body = """## Verification Commands

```bash
# AC1
$ grep "nonexistent_string_xyz" .
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["category"] not in ("unsafe_command", "command_not_allowed", "unsupported_shell_syntax"), (
        f"grep should be allowed, but got category={r['category']}"
    )


# ---------------------------------------------------------------------------
# Unit tests for classify_static_command internals
# ---------------------------------------------------------------------------

def test_classify_static_command_blocks_command_substitution():
    """classify_static_command returns unsupported_shell_syntax for $(...)"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("echo $(date)", Path("."))
    assert result is not None
    classification, category, decision, fix_hint, scope_class = result
    assert classification == "blocked"
    assert category == "unsupported_shell_syntax"
    assert decision == "blocked"
    assert fix_hint is not None


def test_classify_static_command_blocks_backtick():
    """classify_static_command returns unsupported_shell_syntax for backtick"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("echo `date`", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "unsupported_shell_syntax"
    assert decision == "blocked"


def test_classify_static_command_blocks_parameter_expansion():
    """classify_static_command returns unsupported_shell_syntax for ${...}"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("echo ${HOME}", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "unsupported_shell_syntax"
    assert decision == "blocked"


def test_classify_static_command_blocks_bash():
    """classify_static_command returns unsafe_command for bash"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("bash -c 'echo hello'", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "unsafe_command"
    assert decision == "blocked"


def test_classify_static_command_blocks_python3_dash_c():
    """classify_static_command returns unsafe_command for python3 -c"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("python3 -c 'print(1)'", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "unsafe_command"
    assert decision == "blocked"


def test_classify_static_command_blocks_curl():
    """classify_static_command returns unsafe_command for curl"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("curl https://example.com", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "unsafe_command"
    assert decision == "blocked"


def test_classify_static_command_blocks_git_push():
    """classify_static_command returns command_not_allowed for git push (B3: exact allowlist)"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("git push origin main", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    # B3: git uses exact read-only allowlist; non-allowed subcommands return command_not_allowed
    assert category in ("unsafe_command", "command_not_allowed")
    assert decision == "blocked"


def test_classify_static_command_blocks_git_commit():
    """classify_static_command returns command_not_allowed for git commit (B3: exact allowlist)"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("git commit -m 'test'", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    # B3: git uses exact read-only allowlist; non-allowed subcommands return command_not_allowed
    assert category in ("unsafe_command", "command_not_allowed")
    assert decision == "blocked"


def test_classify_static_command_blocks_gh_api():
    """classify_static_command returns command_not_allowed for gh api"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("gh api repos/owner/repo", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "command_not_allowed"
    assert decision == "blocked"


def test_classify_static_command_blocks_gh_issue_edit():
    """classify_static_command returns command_not_allowed for gh issue edit"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("gh issue edit 123 --title x", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "command_not_allowed"
    assert decision == "blocked"


def test_classify_static_command_blocks_pnpm_add():
    """classify_static_command returns command_not_allowed for pnpm add"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("pnpm add lodash", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "command_not_allowed"
    assert decision == "blocked"


def test_classify_static_command_blocks_npm_install():
    """classify_static_command returns blocked for npm install (denied)"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("npm install", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category in ("unsafe_command", "command_not_allowed")
    assert decision == "blocked"


def test_classify_static_command_allows_rg():
    """classify_static_command returns None (proceed) for rg with a specific path"""
    from baseline_vc_preflight import classify_static_command
    # Use a specific (non-broad) path to avoid broad_search_path_unbounded (AC2: Issue #648)
    result = classify_static_command(
        "rg 'pattern' .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py",
        Path("."),
    )
    assert result is None, f"rg should be allowed, but got: {result}"


def test_classify_static_command_allows_test():
    """classify_static_command returns None (proceed) for test -f"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("test -f /nonexistent", Path("."))
    assert result is None, f"test should be allowed, but got: {result}"


def test_classify_static_command_allows_python3_m_pytest():
    """classify_static_command returns None (proceed) for python3 -m pytest"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("python3 -m pytest tests/", Path("."))
    assert result is None, f"python3 -m pytest should be allowed, but got: {result}"


def test_classify_static_command_allows_python3_m_py_compile():
    """classify_static_command returns None (proceed) for python3 -m py_compile"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("python3 -m py_compile src/main.py", Path("."))
    assert result is None, f"python3 -m py_compile should be allowed, but got: {result}"


def test_classify_static_command_allows_uv_run_pytest():
    """classify_static_command returns None (proceed) for uv run pytest"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("uv run pytest tests/", Path("."))
    assert result is None, f"uv run pytest should be allowed, but got: {result}"


def test_classify_static_command_blocks_unknown():
    """classify_static_command returns command_not_allowed for unknown commands"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("some_obscure_binary --flag", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "command_not_allowed"
    assert decision == "blocked"


def test_classify_static_command_blocks_uv_run_unknown():
    """classify_static_command returns command_not_allowed for uv run <unknown>"""
    from baseline_vc_preflight import classify_static_command
    result = classify_static_command("uv run some_tool", Path("."))
    assert result is not None
    _, category, decision, _, _ = result
    assert category == "command_not_allowed"
    assert decision == "blocked"


# ---------------------------------------------------------------------------
# B1: pnpm exact subcommand allowlist tests
# ---------------------------------------------------------------------------

def test_pnpm_exec_is_blocked():
    """B1: pnpm exec is not in the exact pnpm allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ pnpm exec sh -c "echo hello"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "command_not_allowed", f"Expected command_not_allowed, got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_pnpm_dlx_is_blocked():
    """B1: pnpm dlx is not in the exact pnpm allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ pnpm dlx create-react-app my-app
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_pnpm_run_is_blocked():
    """B1: pnpm run is not in the exact pnpm allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ pnpm run my-script
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "command_not_allowed"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


# ---------------------------------------------------------------------------
# B2: mkdir blocked tests
# ---------------------------------------------------------------------------

def test_mkdir_is_blocked():
    """B2: mkdir is not in the allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ mkdir -p artifacts
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "command_not_allowed", f"Expected command_not_allowed, got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


# ---------------------------------------------------------------------------
# B3: git/gh exact allowlist tests
# ---------------------------------------------------------------------------

def test_git_worktree_is_blocked():
    """B3: git worktree is not in the git read-only allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ git worktree add /tmp/wt main
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "command_not_allowed", f"Expected command_not_allowed, got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_git_config_env_is_blocked():
    """B3: git -c (config override) is not in the git read-only allowlist → blocked"""
    body = """## Verification Commands

```bash
# AC1
$ git -c alias.x='!sh -c echo hi' x
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_gh_alias_is_blocked():
    """B3: gh alias is not in the gh read-only allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ gh alias set myalias issue list
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "command_not_allowed", f"Expected command_not_allowed, got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_gh_extension_is_blocked():
    """B3: gh extension is not in the gh read-only allowlist → blocked / command_not_allowed"""
    body = """## Verification Commands

```bash
# AC1
$ gh extension install some/extension
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "command_not_allowed", f"Expected command_not_allowed, got {r['category']}"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


# ---------------------------------------------------------------------------
# B4: unlabeled fence tests
# ---------------------------------------------------------------------------

def test_unlabeled_fence_is_blocked_or_ignored():
    """B4: unlabeled fenced blocks (```) are not extracted as VC commands"""
    # Unlabeled fence should NOT be extracted as VC; result is blocked with extraction error
    body = """## Verification Commands

```
# AC1
$ rg "pattern" src/
```
"""
    data = run_preflight(body)
    # Either: no results and status=blocked (unlabeled fence ignored)
    # Or: results contains a block that has unsupported_vc_format category
    # Either way, the command must NOT have been extracted and run successfully
    assert data["status"] == "blocked", (
        f"Expected status=blocked for unlabeled fence, got {data['status']}"
    )
    # The unlabeled fence content should NOT appear in results
    results = data["results"]
    for r in results:
        assert r["category"] != "expected_baseline_fail" or r["decision"] != "go", (
            "Unlabeled fence command must not be classified as normal expected_fail/go"
        )


def test_unlabeled_fence_error_is_structured():
    """B4: when only unlabeled fences are present, error is structured with unsupported_vc_format"""
    body = """## Verification Commands

```
$ rg "pattern" src/
```
"""
    data = run_preflight(body)
    assert data["status"] == "blocked"
    assert len(data["errors"]) > 0
    err = data["errors"][0]
    assert isinstance(err, dict), f"Error must be dict, got {type(err)}"
    assert "kind" in err, f"Error missing 'kind': {err}"
    # kind should be unsupported_vc_format or extraction_error
    assert err["kind"] in ("unsupported_vc_format", "extraction_error"), (
        f"Expected unsupported_vc_format or extraction_error, got {err['kind']}"
    )


# ---------------------------------------------------------------------------
# B5: pytest exit 5 (no tests collected) → vc_no_tests_collected / blocked
# ---------------------------------------------------------------------------

def test_pytest_no_tests_collected_exit5_is_blocked():
    """B5: pytest exit 5 (no tests collected) → blocked / vc_no_tests_collected"""
    from baseline_vc_preflight import classify_result
    # Simulate pytest exit 5 with no tests collected output
    stdout = "collected 0 items\n\n========== no tests ran ==========\n"
    stderr = ""
    classification, category, decision, fix_hint, scope_class = classify_result(
        5, stdout, stderr, "uv run pytest .claude/skills/some-nonexistent-tests/ -v"
    )
    assert classification == "blocked", f"Expected blocked, got {classification}"
    assert category == "vc_no_tests_collected", f"Expected vc_no_tests_collected, got {category}"
    assert decision == "blocked", f"Expected decision=blocked, got {decision}"


def test_pytest_no_tests_exit5_in_preflight():
    """B5: pytest exit 5 in end-to-end preflight → blocked / vc_no_tests_collected"""
    from baseline_vc_preflight import classify_result
    # exit 5 = no tests collected regardless of output content
    classification, category, decision, fix_hint, scope_class = classify_result(
        5, "", "ERROR: no tests ran", "pytest .claude/skills/nonexistent/"
    )
    assert classification == "blocked"
    assert category == "vc_no_tests_collected"
    assert decision == "blocked"


# ---------------------------------------------------------------------------
# trivially_pass: ssot-discovery script --keywords + --paths detection (#201)
# ---------------------------------------------------------------------------

def test_match_ssot_with_keywords_and_paths_is_trivially_pass_blocked():
    """AC1(#201): bash match-ssot.sh --keywords ... --paths ... → blocked / trivially_pass

    Regression test for the original failing case from Issue #201 background section.
    """
    body = """## Verification Commands

```bash
# AC1
$ bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "milestone,github-milestone" --paths "docs/dev/milestone-ops.md"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked", f"Expected blocked, got {r['classification']}"
    assert r["category"] == "trivially_pass", f"Expected trivially_pass, got {r['category']}"
    assert r["decision"] == "blocked", f"Expected decision=blocked, got {r['decision']}"
    # Must not be executed (static classification before denied-command check)
    assert r["exit_code"] is None, f"Expected exit_code=None (not executed), got {r['exit_code']}"
    assert r["confidence"] == "high", f"Expected confidence=high, got {r['confidence']}"


def test_match_ssot_direct_invocation_is_trivially_pass_blocked():
    """AC1(#201): direct execution of match-ssot.sh → blocked / trivially_pass"""
    body = """## Verification Commands

```bash
# AC1
$ .claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "foo" --paths "docs/dev/bar.md"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "trivially_pass"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_match_ssot_py_via_python3_is_trivially_pass_blocked():
    """AC1(#201): python3 .../match_ssot.py --keywords ... --paths ... → blocked / trivially_pass"""
    body = """## Verification Commands

```bash
# AC1
$ python3 .claude/skills/ssot-discovery/scripts/match_ssot.py --keywords "foo,bar" --paths "docs/dev/baz.md"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    assert r["classification"] == "blocked"
    assert r["category"] == "trivially_pass"
    assert r["decision"] == "blocked"
    assert r["exit_code"] is None


def test_match_ssot_keywords_only_is_not_trivially_pass():
    """AC1(#201): match-ssot.sh --keywords only (no --paths) → NOT trivially_pass"""
    body = """## Verification Commands

```bash
# AC1
$ bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "milestone,github-milestone"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    # --keywords alone does not create a trivially-pass structure
    assert r["category"] != "trivially_pass", (
        f"Expected NOT trivially_pass for --keywords only, got {r['category']}"
    )


def test_match_ssot_paths_only_is_not_trivially_pass():
    """AC1(#201): match-ssot.sh --paths only (no --keywords) → NOT trivially_pass"""
    body = """## Verification Commands

```bash
# AC1
$ bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --paths "docs/dev/milestone-ops.md"
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    # --paths alone does not create the trivially-pass structure
    assert r["category"] != "trivially_pass", (
        f"Expected NOT trivially_pass for --paths only, got {r['category']}"
    )


def test_rg_without_paths_is_not_trivially_pass():
    """AC1(#201): rg without discovery script → NOT trivially_pass"""
    body = """## Verification Commands

```bash
# AC1
$ rg -n "some_new_function" .claude/skills/issue-contract-review/scripts/baseline_vc_preflight.py
```
"""
    data = run_preflight(body)
    results = data["results"]
    assert len(results) == 1
    r = results[0]
    # Normal rg search is NOT trivially_pass regardless of path arguments
    assert r["category"] != "trivially_pass", f"Expected not trivially_pass, got {r['category']}"


def test_rg_literal_paths_pattern_is_not_trivially_pass():
    """AC1(#201): rg searching for '--paths' as a literal string must NOT be flagged as trivially_pass.

    False positive guard: 'rg -e "--paths" file' and 'rg -- "--paths" file' are
    legitimate VC searches for the literal text '--paths' and must not be blocked
    as trivially_pass (rg has no --paths option; this is a search pattern argument).
    """
    from baseline_vc_preflight import _is_trivially_pass_command
    # Searching for the text "--paths" with -e flag
    assert _is_trivially_pass_command('rg -n -e "--paths" docs/dev/dor.md') is False
    # Using -- to separate pattern from file arguments
    assert _is_trivially_pass_command('rg -n -- "--paths" docs/dev/dor.md') is False
    # Plain grep searching for --paths text
    assert _is_trivially_pass_command('grep -n "--paths" docs/dev/dor.md') is False


def test_is_trivially_pass_unit_direct():
    """Unit tests for _is_trivially_pass_command directly."""
    from baseline_vc_preflight import _is_trivially_pass_command

    # Trivially pass patterns: discovery script + --keywords + --paths
    assert _is_trivially_pass_command(
        "bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords foo --paths bar.md"
    ) is True
    assert _is_trivially_pass_command(
        ".claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords=foo --paths=bar.md"
    ) is True
    assert _is_trivially_pass_command(
        "python3 .claude/skills/ssot-discovery/scripts/match_ssot.py --keywords foo --paths bar"
    ) is True

    # NOT trivially pass: discovery script with only one of the flags
    assert _is_trivially_pass_command(
        "bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords foo"
    ) is False
    assert _is_trivially_pass_command(
        "bash .claude/skills/ssot-discovery/scripts/match-ssot.sh --paths bar.md"
    ) is False

    # NOT trivially pass: rg/grep are not discovery scripts
    assert _is_trivially_pass_command("rg -n 'pattern' src/") is False
    assert _is_trivially_pass_command("rg 'pattern' --paths=some/file.py") is False
    assert _is_trivially_pass_command("grep -n 'pattern' --paths some/file.py") is False

    # NOT trivially pass: unrelated commands
    assert _is_trivially_pass_command("pnpm lint") is False
    assert _is_trivially_pass_command("test -f somefile") is False
