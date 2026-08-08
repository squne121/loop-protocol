"""Issue #1952 actual Codex custom-agent route smoke.

The runner's exit 77 is a capability/authentication SKIP and is deliberately
not converted to a static-pass result. A success requires runtime-emitted child
identity evidence for the requested custom agent and a clean worktree.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _run_route_smoke(role: str, route: str, marker: str, *, refusal: bool) -> str:
    if not (REPO_ROOT / ".git").exists() or not RUNNER.is_file():
        pytest.skip("linked worktree runtime surface is unavailable")

    request = (
        f"Use the custom agent named `{role}` for this bounded runtime smoke. "
        f"Do not perform the task yourself. "
    )
    if refusal:
        wrong_intent = "edit an existing GitHub Issue" if role == "issue-creator" else "create a new GitHub Issue"
        request += (
            f"Request that agent to {wrong_intent}. It must refuse before any mutation, shell mutation, "
            f"or network mutation, then output exactly `{marker}`."
        )
    else:
        request += (
            f"Request that agent to use only its canonical `{route}` Skill, read its SKILL.md, "
            f"perform no mutation, then output exactly `{marker}`."
        )

    with tempfile.TemporaryDirectory(prefix="runtime-smoke-1952-") as tmp:
        tmp_path = Path(tmp)
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text(request + "\n", encoding="utf-8")
        output_dir = tmp_path / f"output-{role}-{int(time.time() * 1000)}"
        result = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--runtime", "codex",
                "--mode", "structured",
                "--worktree", str(REPO_ROOT),
                "--prompt-file", str(prompt_file),
                "--output-dir", str(output_dir),
                "--timeout-seconds", "180",
                "--agent-type", role,
                "--expect-marker", marker,
                "--require-clean-postcondition",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if result.returncode == 77:
            pytest.skip(f"Codex runtime smoke SKIP (exit 77): {result.stderr.strip()[-1200:]}")
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
        summary = (output_dir / "summary.md").read_text(encoding="utf-8")

    assert f"requested_agent_type: {role}" in summary
    assert f"effective_agent_type: {role}" in summary
    assert f"child_agent_type_observed: {role}" in summary
    assert "native_spawn_event_observed: True" in summary
    assert "tested_head:" in summary
    assert "runtime_version:" in summary
    assert "loaded_skills_source: static_frontmatter" in summary
    assert "terminal" not in summary.lower() or "exit_code: 0" in summary
    return summary


@pytest.mark.parametrize(
    ("role", "route", "marker"),
    [
        ("issue-creator", "create-issue", "RUNTIME_SMOKE_1952_CREATOR_CREATE_OK"),
        ("issue-editor", "edit-issue", "RUNTIME_SMOKE_1952_EDITOR_EDIT_OK"),
    ],
)
def test_codex_creator_editor_runtime_evidence(role: str, route: str, marker: str):
    _run_route_smoke(role, route, marker, refusal=False)


@pytest.mark.parametrize(
    ("role", "route", "marker"),
    [
        ("issue-creator", "edit-issue", "RUNTIME_SMOKE_1952_CREATOR_EDIT_REFUSED"),
        ("issue-editor", "create-issue", "RUNTIME_SMOKE_1952_EDITOR_CREATE_REFUSED"),
        ("issue-creator", "unknown", "RUNTIME_SMOKE_1952_UNKNOWN_REFUSED"),
    ],
)
def test_codex_creator_editor_wrong_route_refuses_before_mutation(role: str, route: str, marker: str):
    _run_route_smoke(role, route, marker, refusal=True)
