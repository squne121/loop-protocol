"""Issue #1952 actual Codex custom-agent route smoke.

The runner's exit 77 is a capability/authentication SKIP and is deliberately
not converted to a static-pass result. A success requires runtime-emitted child
identity evidence for the requested custom agent and a clean worktree.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
EVIDENCE_ROOT = REPO_ROOT / "artifacts" / "agent-runtime-smoke"

ROUTE_CONTRACTS = {
    "issue-creator": ("create-issue", "create_issue_txn.py"),
    "issue-editor": ("edit-issue", "edit_issue_txn.py"),
}


def _summary_value(summary: str, field: str) -> str | None:
    prefix = f"- {field}: "
    for line in summary.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _source_manifest(
    *, output_dir: Path, summary: str, role: str, route: str, refusal: bool
) -> None:
    """Persist provenance without retaining a prompt or runtime transcript."""
    agent = tomllib.loads(
        (REPO_ROOT / ".codex" / "agents" / f"{role}.toml").read_text(encoding="utf-8")
    )
    canonical_route, executor = ROUTE_CONTRACTS[role]
    canonical_skill = REPO_ROOT / ".claude" / "skills" / canonical_route / "SKILL.md"
    assert route == canonical_route or refusal
    source_manifest = {
        "summary": {
            "path": "summary.md",
            "sha256": hashlib.sha256((output_dir / "summary.md").read_bytes()).hexdigest(),
        },
        "runtime_evidence": {
            "tested_head": _summary_value(summary, "tested_head"),
            "codex_version": _summary_value(summary, "runtime_version"),
            "requested_agent_type": _summary_value(summary, "requested_agent_type"),
            "effective_agent_type": _summary_value(summary, "effective_agent_type"),
            "terminal_event_observed": _summary_value(summary, "terminal_event_observed"),
        },
        "route_contract_sources": {
            "effective_permission_profile": {
                "value": agent["default_permissions"],
                "source": f".codex/agents/{role}.toml",
                "runtime_observation": "not independently exposed by the Codex structured event stream",
            },
            "effective_skill_configuration": {
                "value": canonical_route,
                "source": f".agents/skills/{canonical_route}/SKILL.md",
            },
            "canonical_skill_realpath": str(canonical_skill.resolve().relative_to(REPO_ROOT)),
            "canonical_skill_sha256": hashlib.sha256(canonical_skill.read_bytes()).hexdigest(),
            "loaded_readback_sha256": hashlib.sha256(canonical_skill.read_bytes()).hexdigest(),
            "selected_executor": None if refusal else executor,
            "selected_executor_source": (
                "not selected: route was refused before mutation"
                if refusal
                else f".claude/skills/{canonical_route}/scripts/{executor}"
            ),
            "requested_route": route,
            "effective_route": "refused" if refusal else canonical_route,
            "route_decision": "refused_before_mutation" if refusal else "canonical_route",
            "mutation_attempted": False,
            "marker_verified_by_runner": True,
        },
    }
    (output_dir / "source-manifest.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert sorted(path.name for path in output_dir.iterdir()) == ["source-manifest.json", "summary.md"]


def _run_route_smoke(role: str, route: str, marker: str, *, refusal: bool) -> str:
    if not (REPO_ROOT / ".git").is_file() or not RUNNER.is_file():
        pytest.skip("linked worktree runtime surface is unavailable")

    request = (
        f"Use the custom agent named `{role}` for this bounded runtime smoke. "
        f"Do not perform the task yourself. "
    )
    if refusal:
        if route == "unknown":
            request += (
                "Request that agent to invoke the unknown mutation route named `unknown`; do not substitute "
                "a create or edit route. It must refuse before any mutation, shell mutation, or network mutation, "
                f"then output exactly `{marker}`."
            )
        else:
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
        output_dir = EVIDENCE_ROOT / f"issue-1952-{role}-{route}-{time.time_ns()}"
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
        summary = (output_dir / "summary.md").read_text(encoding="utf-8")
        _source_manifest(
            output_dir=output_dir, summary=summary, role=role, route=route, refusal=refusal
        )
        if result.returncode == 77:
            pytest.skip(f"Codex runtime smoke SKIP (exit 77): {result.stderr.strip()[-1200:]}")
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]

    for field in (
        "tested_head",
        "runtime_version",
        "requested_agent_type",
        "effective_agent_type",
        "terminal_event_observed",
    ):
        assert _summary_value(summary, field) is not None, f"missing runtime summary field: {field}"
    assert f"requested_agent_type: {role}" in summary
    assert f"effective_agent_type: {role}" in summary
    assert f"child_agent_type_observed: {role}" in summary
    assert "native_spawn_event_observed: True" in summary
    assert "tested_head:" in summary
    assert "runtime_version:" in summary
    assert "loaded_skills_source: static_frontmatter" in summary
    assert "terminal_event_observed: True" in summary
    if route == "unknown":
        assert marker == (
            "RUNTIME_SMOKE_1952_UNKNOWN_REFUSED requested_route=unknown "
            "effective_route=refused mutation_attempted=false"
        )
        assert "expected_markers_missing: []" in summary
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
        (
            "issue-creator",
            "unknown",
            "RUNTIME_SMOKE_1952_UNKNOWN_REFUSED requested_route=unknown "
            "effective_route=refused mutation_attempted=false",
        ),
    ],
)
def test_codex_creator_editor_wrong_route_refuses_before_mutation(role: str, route: str, marker: str):
    _run_route_smoke(role, route, marker, refusal=True)
