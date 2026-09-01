"""Tests for the secret-free fixed-probe Claude runtime diagnostic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "scripts" / "agent-ops" / "claude_runtime_diag.py"
GUARD = REPO_ROOT / ".claude" / "hooks" / "secret_boundary_guard.sh"
PROBES = (
    "claude-gpt-root-state",
    "claude-gpt-home-class",
    "claude-gpt-root-relation",
)


@pytest.mark.parametrize(
    ("fixture_env", "expected"),
    [
        (
            {"CLAUDE_GPT_HOME": "/synthetic/root", "HOME": "/synthetic/root/claude-home"},
            ("runtime_root=present", "home_class=isolated", "root_relation=other"),
        ),
        (
            {"CLAUDE_GPT_HOME": "/synthetic/home/.claude-gpt", "HOME": "/synthetic/home"},
            ("runtime_root=present", "home_class=nested", "root_relation=nested"),
        ),
        (
            {"CLAUDE_GPT_HOME": "/synthetic/same", "HOME": "/synthetic/same"},
            ("runtime_root=present", "home_class=unexpected", "root_relation=same"),
        ),
        (
            {"HOME": "/synthetic/home"},
            ("runtime_root=absent", "home_class=unexpected", "root_relation=other"),
        ),
        (
            {"CLAUDE_GPT_HOME": "", "HOME": "/synthetic/home"},
            ("runtime_root=absent", "home_class=unexpected", "root_relation=other"),
        ),
        (
            {"CLAUDE_GPT_HOME": "relative-root", "HOME": "/synthetic/home"},
            ("runtime_root=present", "home_class=unexpected", "root_relation=other"),
        ),
        (
            {"CLAUDE_GPT_HOME": "/synthetic/root-a", "HOME": "/synthetic/root-b"},
            ("runtime_root=present", "home_class=unexpected", "root_relation=other"),
        ),
    ],
    ids=("isolated", "nested", "same", "absent", "empty", "relative", "unrelated"),
)
def test_subprocess_fixed_probe_ids(fixture_env: dict[str, str], expected: tuple[str, str, str]) -> None:
    """GIVEN synthetic-only environments, WHEN each fixed probe runs, THEN it emits its enum."""
    for probe, expected_line in zip(PROBES, expected, strict=True):
        completed = subprocess.run(
            [sys.executable, str(HELPER), probe],
            cwd=REPO_ROOT,
            env=fixture_env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0
        assert completed.stdout == f"{expected_line}\n"
        assert completed.stderr == ""


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("unknown-probe",),
        ("claude-gpt-root-state", "extra"),
        ("--probe", "claude-gpt-root-state"),
    ],
)
def test_invalid_arguments_are_fixed_and_nonzero(arguments: tuple[str, ...]) -> None:
    """GIVEN an invalid argv shape, WHEN invoked, THEN no argument is reflected."""
    completed = subprocess.run(
        [sys.executable, str(HELPER), *arguments],
        cwd=REPO_ROOT,
        env={"CLAUDE_GPT_HOME": "/synthetic/sentinel-root", "HOME": "/synthetic/sentinel-home"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == "error=invalid_arguments\n"
    assert completed.stderr == ""


def test_arbitrary_arguments_have_no_generic_interface() -> None:
    """GIVEN arbitrary path-like input, WHEN invoked, THEN it is rejected as an invalid probe."""
    arbitrary_input = "/synthetic/arbitrary/path"
    completed = subprocess.run(
        [sys.executable, str(HELPER), arbitrary_input],
        cwd=REPO_ROOT,
        env={"CLAUDE_GPT_HOME": "/synthetic/root", "HOME": "/synthetic/home"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == "error=invalid_arguments\n"
    assert arbitrary_input not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "fixture_env",
    [
        {"CLAUDE_GPT_HOME": "/synthetic/SENTINEL-root", "HOME": "/synthetic/SENTINEL-home"},
        {"CLAUDE_GPT_HOME": "relative-SENTINEL-root", "HOME": "/synthetic/SENTINEL-home"},
        {"HOME": "/synthetic/SENTINEL-home"},
    ],
)
def test_no_raw_egress(fixture_env: dict[str, str]) -> None:
    """GIVEN sentinel inputs, WHEN every path runs, THEN no raw value escapes."""
    for probe in (*PROBES, "SENTINEL-invalid"):
        completed = subprocess.run(
            [sys.executable, str(HELPER), probe],
            cwd=REPO_ROOT,
            env=fixture_env,
            capture_output=True,
            text=True,
            check=False,
        )

        assert "SENTINEL" not in completed.stdout
        assert "SENTINEL" not in completed.stderr


def test_evidence_hygiene_is_fixed_enum_only() -> None:
    """GIVEN a valid probe, WHEN its output is recorded, THEN it contains only the fixed enum."""
    completed = subprocess.run(
        [sys.executable, str(HELPER), "claude-gpt-home-class"],
        cwd=REPO_ROOT,
        env={"CLAUDE_GPT_HOME": "/synthetic/root", "HOME": "/synthetic/root/claude-home"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "home_class=isolated\n"
    assert completed.stderr == ""


def test_lexical_source_does_not_introduce_path_resolution_or_file_io() -> None:
    """GIVEN the helper source, THEN classification stays lexical and environment-only."""
    source = HELPER.read_text(encoding="utf-8")
    forbidden = ("realpath", "Path.resolve", "open(", "read_text", "expanduser", "normpath")

    assert all(marker not in source for marker in forbidden)
    assert source.count("os.environ.get(") == 2


@pytest.mark.parametrize(
    ("command", "expected_returncode"),
    [
        ("python3 scripts/agent-ops/claude_runtime_diag.py claude-gpt-root-state", 0),
        ("python3 -c 'import os; print(os.environ)'", 2),
        ("env", 2),
        ("cat /home/synthetic/.netrc", 2),
    ],
    ids=("literal-helper-allowed", "inline-python-blocked", "env-dump-blocked", "sensitive-path-blocked"),
)
def test_guard(command: str, expected_returncode: int) -> None:
    """GIVEN synthetic PreToolUse input, THEN only the literal helper command is allowed."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    completed = subprocess.run(
        ["bash", str(GUARD)],
        cwd=REPO_ROOT,
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert completed.returncode == expected_returncode
    if expected_returncode == 0:
        assert completed.stderr == ""
    else:
        assert "[secret_boundary_guard] blocked:" in completed.stderr
