#!/usr/bin/env python3
"""Tests for scripts/agent-guards/shell_command_analysis.py (Issue #1428).

Fixture naming convention (AC12): each parametrized test id is prefixed
with the expected classification bucket — `data_only`, `executed`, or
`indeterminate` — so the expected outcome is discoverable from the fixture
name / parameter id alone.
"""

from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from shell_command_analysis import (  # noqa: E402
    COMMAND_KIND_GIT_PUSH,
    COMMAND_KIND_RTK_GIT_PUSH,
    SCHEMA,
    STATUS_INDETERMINATE,
    STATUS_OK,
    analyze_shell_command,
)


def _push_commands(result: dict) -> list[dict]:
    return [c for c in result["commands"] if c["command_kind"] in (COMMAND_KIND_GIT_PUSH, COMMAND_KIND_RTK_GIT_PUSH)]


# ---------------------------------------------------------------------------
# AC7 / In Scope 2: schema shape
# ---------------------------------------------------------------------------


def test_schema_shape_ok_result():
    """GIVEN a plain git push command WHEN analyzed THEN the result matches
    the SHELL_COMMAND_ANALYSIS_V1 schema shape."""
    result = analyze_shell_command("git push origin main")
    assert result["schema"] == SCHEMA
    assert result["status"] == STATUS_OK
    assert isinstance(result["commands"], list)
    assert result["reason_code"] == "parsed"
    fact = result["commands"][0]
    assert set(fact.keys()) == {
        "command_kind",
        "executable_literalness",
        "subcommand_literalness",
        "remote_class",
        "refspec_class",
        "dangerous_flags",
        "execution_context",
        "source_span",
    }
    assert set(fact["source_span"].keys()) == {"start", "end"}


def test_schema_never_includes_raw_argv():
    """GIVEN a command with sensitive-looking argument text WHEN analyzed
    THEN the structured output never contains the raw command text or argv
    strings (only bounded enums / integers)."""
    result = analyze_shell_command("git push origin secret-branch-name-xyz")
    serialized = str(result)
    assert "secret-branch-name-xyz" not in serialized


# ---------------------------------------------------------------------------
# data_only fixtures (AC1 / AC2 / AC12) — must NOT be classified as git_push
# ---------------------------------------------------------------------------

DATA_ONLY_CASES = [
    (
        "data_only_match_ssot_keywords",
        '.claude/skills/ssot-discovery/scripts/match-ssot.sh --keywords "issue-refinement remote_write git push"',
    ),
    ("data_only_rg_search", 'rg -n "git push" docs/ .claude/'),
    ("data_only_grep_search", "grep -R 'git push origin main' docs/"),
    ("data_only_printf_literal", "printf '%s\\n' 'git push origin main'"),
    ("data_only_python_option_value", 'python3 tool.py --message "git push origin main"'),
    ("data_only_git_log_grep", 'git log --grep="git push"'),
    (
        "data_only_quoted_keyword_description",
        "some-command --keyword='git push' --description=\"do not execute git push\"",
    ),
    ("data_only_quoted_heredoc_delimiter", "cat <<'EOF'\ngit push origin main\nEOF\n"),
    ("data_only_double_quoted_heredoc_delimiter", 'cat <<"EOF"\ngit push origin main\nEOF\n'),
]


@pytest.mark.parametrize("command", [c for _id, c in DATA_ONLY_CASES], ids=[i for i, _c in DATA_ONLY_CASES])
def test_data_only_commands_not_classified_as_push(command: str):
    """GIVEN a command containing 'git push' only as non-executable data
    WHEN analyzed THEN no git_push / rtk_git_push command is reported."""
    result = analyze_shell_command(command)
    assert _push_commands(result) == []


# ---------------------------------------------------------------------------
# executed fixtures (AC3 / AC4 / AC5 / AC6) — MUST be classified as git_push
# ---------------------------------------------------------------------------

EXECUTED_CASES = [
    ("executed_top_level_push", "git push origin main", "top_level"),
    ("executed_dash_c_push", "git -C . push origin main", "top_level"),
    ("executed_env_prefix_push", "env FOO=bar git push origin main", "top_level"),
    ("executed_bare_assignment_prefix_push", "FOO=bar git push origin main", "top_level"),
    ("executed_command_wrapper_push", "command git push origin main", "top_level"),
    ("executed_and_list_push", "echo ok && git push origin main", "list"),
    ("executed_semicolon_list_push", "echo ok; git push origin main", "list"),
    ("executed_or_list_push", "false || git push origin main", "list"),
    ("executed_pipeline_push", "git status | git push origin main", "pipeline"),
    ("executed_dollar_paren_substitution_push", 'echo "$(git push origin main)"', "command_substitution"),
    ("executed_backtick_substitution_push", "echo `git push origin main`", "command_substitution"),
    ("executed_bash_dash_c_push", "bash -c 'git push origin main'", "execution_carrier"),
    ("executed_sh_dash_c_push", 'sh -c "git push origin main"', "execution_carrier"),
    (
        "executed_unquoted_heredoc_substitution_push",
        "cat <<EOF\n$(git push origin main)\nEOF\n",
        "command_substitution",
    ),
    ("executed_here_string_substitution_push", 'cat <<< "$(git push origin main)"', "command_substitution"),
]


@pytest.mark.parametrize(
    "command,expected_context",
    [(c, ctx) for _id, c, ctx in EXECUTED_CASES],
    ids=[i for i, _c, _ctx in EXECUTED_CASES],
)
def test_executed_commands_classified_as_push(command: str, expected_context: str):
    """GIVEN a command that would actually execute `git push` WHEN analyzed
    THEN a git_push command fact is reported with the expected execution
    context."""
    result = analyze_shell_command(command)
    push_commands = _push_commands(result)
    assert push_commands, f"expected a git_push fact for: {command!r}, got {result}"
    assert push_commands[0]["execution_context"] == expected_context
    assert push_commands[0]["command_kind"] == COMMAND_KIND_GIT_PUSH


def test_executed_rtk_git_push():
    """GIVEN `rtk git push ...` WHEN analyzed THEN command_kind is
    rtk_git_push (distinct from plain git_push)."""
    result = analyze_shell_command("rtk git push origin HEAD:refs/heads/feature-x")
    push_commands = _push_commands(result)
    assert len(push_commands) == 1
    assert push_commands[0]["command_kind"] == COMMAND_KIND_RTK_GIT_PUSH
    assert push_commands[0]["remote_class"] == "origin"
    assert push_commands[0]["refspec_class"] == "head_to_literal_branch"


def test_executed_dangerous_flags_detected():
    """GIVEN `git push --force origin main` WHEN analyzed THEN the
    dangerous_flags list includes 'force'."""
    result = analyze_shell_command("git push --force origin main")
    push_commands = _push_commands(result)
    assert push_commands[0]["dangerous_flags"] == ["force"]


def test_executed_status_is_ok_not_indeterminate():
    """GIVEN a fully-literal executed git push WHEN analyzed THEN status is
    'ok' (not indeterminate) — literal detection must not over-trigger
    fail-closed."""
    result = analyze_shell_command("git push origin main")
    assert result["status"] == STATUS_OK


# ---------------------------------------------------------------------------
# indeterminate fixtures (AC8 / AC12) — dynamic command words / unsupported
# constructs must fail-closed, never fail-open
# ---------------------------------------------------------------------------


def test_indeterminate_dynamic_executable_word():
    """GIVEN a dynamic executable word followed by a literal 'push' token
    WHEN analyzed THEN status is indeterminate with dynamic_command_word."""
    result = analyze_shell_command('cmd=git\n"$cmd" push origin main')
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "dynamic_command_word"


def test_indeterminate_dynamic_subcommand_word():
    """GIVEN `git p$(printf ush) origin main` WHEN analyzed THEN status is
    indeterminate (subcommand word is not a static literal)."""
    result = analyze_shell_command("git p$(printf ush) origin main")
    assert result["status"] == STATUS_INDETERMINATE


def test_indeterminate_dynamic_variable_executable():
    """GIVEN `"$command" push origin main` WHEN analyzed THEN status is
    indeterminate with dynamic_command_word."""
    result = analyze_shell_command('"$command" push origin main')
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "dynamic_command_word"


def test_indeterminate_unclosed_quote():
    """GIVEN an unclosed quote WHEN analyzed THEN status is indeterminate
    with malformed_shell (never fail-open)."""
    result = analyze_shell_command("git push origin 'main")
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "malformed_shell"


def test_indeterminate_malformed_command_substitution():
    """GIVEN an unclosed $(...) WHEN analyzed THEN status is indeterminate
    with malformed_shell."""
    result = analyze_shell_command("echo $(git push origin main")
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "malformed_shell"


def test_indeterminate_unsupported_execution_carrier_find_exec():
    """GIVEN `find ... -exec git push ...` WHEN analyzed THEN status is
    indeterminate with unsupported_construct (find -exec is not a
    recursively-supported execution carrier)."""
    result = analyze_shell_command("find . -maxdepth 0 -exec git push origin main ;")
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "unsupported_construct"


def test_indeterminate_unsupported_execution_carrier_xargs():
    """GIVEN `xargs git push < push-args.txt` WHEN analyzed THEN status is
    indeterminate with unsupported_construct."""
    result = analyze_shell_command("xargs git push < push-args.txt")
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "unsupported_construct"


@pytest.mark.parametrize("carrier", ["sudo", "timeout", "nice", "nohup"])
def test_indeterminate_unsupported_execution_carrier_misc(carrier: str):
    """GIVEN an unsupported execution carrier prefix WHEN analyzed THEN
    status is indeterminate with unsupported_construct."""
    result = analyze_shell_command(f"{carrier} git push origin main")
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "unsupported_construct"


def test_indeterminate_unresolved_source_carrier():
    """GIVEN `source generated-script.sh` WHEN analyzed THEN status is
    indeterminate (file content is not inline and cannot be statically
    resolved)."""
    result = analyze_shell_command("source generated-script.sh")
    assert result["status"] == STATUS_INDETERMINATE


def test_indeterminate_bash_stdin_script():
    """GIVEN `printf '...' | bash` (bash reading a script from stdin) WHEN
    analyzed THEN status is indeterminate (script content not inline)."""
    result = analyze_shell_command("printf 'git push origin main\\n' | bash")
    assert result["status"] == STATUS_INDETERMINATE


def test_indeterminate_analysis_timeout_on_oversized_input():
    """GIVEN an oversized command string WHEN analyzed THEN status is
    indeterminate with analysis_timeout (bounded resource guard)."""
    huge = "echo " + ("a" * 30000)
    result = analyze_shell_command(huge)
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "analysis_timeout"


def test_indeterminate_malformed_payload_none_command():
    """GIVEN a None command WHEN analyzed THEN status is indeterminate
    (fail-closed for malformed input, mirrors the Node adapter's
    malformed_payload handling)."""
    result = analyze_shell_command(None)  # type: ignore[arg-type]
    assert result["status"] == STATUS_INDETERMINATE
    assert result["reason_code"] == "malformed_shell"


# ---------------------------------------------------------------------------
# In Scope 5 / git_mutation_command_policy.py split-brain regression
# ---------------------------------------------------------------------------


def test_analyzer_does_not_change_git_mutation_command_policy_api():
    """GIVEN scripts/agent-guards/git_mutation_command_policy.py WHEN
    imported alongside this analyzer THEN classify_rtk_git_mutation keeps
    its existing signature (external API unchanged, Issue #1428 In Scope
    5 / Out of Scope)."""
    sys.path.insert(0, os.path.join(HERE, ".."))
    import git_mutation_command_policy as gmcp
    import inspect

    sig = inspect.signature(gmcp.classify_rtk_git_mutation)
    assert list(sig.parameters) == ["command", "cwd", "require_active_branch_push"]


def test_analyzer_and_git_mutation_policy_agree_on_rtk_git_push_recognition():
    """GIVEN an `rtk git push origin HEAD:refs/heads/<branch>` command WHEN
    both this analyzer and git_mutation_command_policy tokenize it
    independently THEN both recognize it as an rtk git push command (no
    split-brain divergence on the basic recognition question)."""
    sys.path.insert(0, os.path.join(HERE, ".."))
    import git_mutation_command_policy as gmcp

    command = "rtk git push origin HEAD:refs/heads/feature-x"
    analyzer_result = analyze_shell_command(command)
    assert _push_commands(analyzer_result)[0]["command_kind"] == COMMAND_KIND_RTK_GIT_PUSH

    policy_result = gmcp.classify_rtk_git_mutation(
        command,
        cwd=os.getcwd(),
        require_active_branch_push=False,
    )
    assert policy_result is not None
    assert policy_result.command_class == gmcp.COMMAND_CLASS_RTK_GIT_PUSH
