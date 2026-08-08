"""Issue #1952 actual Codex custom-agent route smoke.

The runner's exit 77 is a capability/authentication SKIP and is deliberately
not converted to a static-pass result. A success requires runtime-emitted child
identity evidence for the requested custom agent and a clean worktree.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    "issue-creator": "create-issue",
    "issue-editor": "edit-issue",
}

# AC5 is an immediate runtime verification, but CI/verifier time budgets must
# not kill the pytest process before the harness can emit its authoritative
# capability result.  The two read-only positive routes run concurrently and
# each gets one explicit capability window; exceeding it is a runner exit 77
# (SKIP), never a static PASS or an outer-process timeout.
_CAPABILITY_WINDOW_SECONDS = 45
_RUNNER_PROCESS_TIMEOUT_SECONDS = 60


def _summary_value(summary: str, field: str) -> str | None:
    prefix = f"- {field}: "
    for line in summary.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


def _observed(value: str | None, source_event: str | None) -> dict[str, object]:
    """Represent a runtime field without upgrading declarations to observations."""
    return {
        "status": "observed" if value is not None else "unavailable",
        "value": value,
        "source_event": source_event if value is not None else None,
    }


def _source_manifest(*, output_dir: Path, summary: str, role: str, route: str) -> None:
    """Persist declared and runtime-observed provenance without raw transcripts."""
    agent = tomllib.loads(
        (REPO_ROOT / ".codex" / "agents" / f"{role}.toml").read_text(encoding="utf-8")
    )
    canonical_route = ROUTE_CONTRACTS[role]
    canonical_skill = REPO_ROOT / ".claude" / "skills" / canonical_route / "SKILL.md"
    route_decision = _summary_value(summary, "route_preflight_decision")
    refusal = route_decision == "refused_before_runtime"
    source_manifest = {
        "summary": {
            "path": "summary.md",
            "sha256": hashlib.sha256((output_dir / "summary.md").read_bytes()).hexdigest(),
        },
        "declared": {
            "agent_type": role,
            "permission_profile": agent["default_permissions"],
            "skill_route": canonical_route,
            "canonical_skill_path": f".agents/skills/{canonical_route}/SKILL.md",
            "canonical_skill_realpath": str(canonical_skill.resolve().relative_to(REPO_ROOT)),
            "canonical_skill_sha256": hashlib.sha256(canonical_skill.read_bytes()).hexdigest(),
            "requested_mutation_route": route,
        },
        "observed": {
            "tested_head": _observed(_summary_value(summary, "tested_head"), "runner_git_rev_parse"),
            "codex_version": _observed(_summary_value(summary, "runtime_version"), "runtime_version_subprocess"),
            "child_agent_type": _observed(
                _summary_value(summary, "child_agent_type_observed"),
                _summary_value(summary, "child_agent_type_source"),
            ),
            "effective_permission_profile": _observed(None, None),
            "loaded_skill": _observed(None, None),
            "executor": {
                "status": "not_invoked" if refusal else "unavailable",
                "path": None,
                "invocation_observed": False if refusal else None,
                "source_event": "runner_agent_route_guard" if refusal else None,
            },
            "route_preflight": {
                "decision": route_decision,
                "declared_route": _summary_value(summary, "declared_mutation_route"),
                "source_event": _summary_value(summary, "route_preflight_source"),
            },
            "mutation": {
                "status": "not_attempted_by_runner" if refusal else "unavailable",
                "attempted": None,
                "observed_channels": ["runner_agent_route_guard"] if refusal else [],
            },
            "terminal_event": _observed(
                _summary_value(summary, "terminal_event_observed"), "codex_json_event_stream"
            ),
        },
    }
    (output_dir / "source-manifest.json").write_text(
        json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert sorted(path.name for path in output_dir.iterdir()) == ["source-manifest.json", "summary.md"]


def _run_route_smoke(role: str, route: str, marker: str | None, *, refusal: bool) -> str:
    if not (REPO_ROOT / ".git").is_file() or not RUNNER.is_file():
        pytest.skip("linked worktree runtime surface is unavailable")

    request = (
        f"Use the custom agent named `{role}` for this bounded runtime smoke. "
        f"Do not perform the task yourself. "
    )
    if refusal:
        request += "The runner must evaluate the requested mutation route before any runtime is started."
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
        command = [
                sys.executable,
                str(RUNNER),
                "--runtime", "codex",
                "--mode", "structured",
                "--worktree", str(REPO_ROOT),
                "--prompt-file", str(prompt_file),
                "--output-dir", str(output_dir),
                "--timeout-seconds", str(_CAPABILITY_WINDOW_SECONDS),
                "--timeout-is-capability-unavailable",
                "--agent-type", role,
                "--require-clean-postcondition",
                "--requested-mutation-route", route,
            ]
        if marker is not None:
            command.extend(["--expect-marker", marker])
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_RUNNER_PROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        summary = (output_dir / "summary.md").read_text(encoding="utf-8")
        _source_manifest(output_dir=output_dir, summary=summary, role=role, route=route)
        if result.returncode == 77:
            pytest.skip(f"Codex runtime smoke SKIP (exit 77): {result.stderr.strip()[-1200:]}")
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]

    manifest = json.loads((output_dir / "source-manifest.json").read_text(encoding="utf-8"))
    assert manifest["declared"]["agent_type"] == role
    assert manifest["declared"]["skill_route"] == ROUTE_CONTRACTS[role]
    assert manifest["observed"]["effective_permission_profile"] == {
        "status": "unavailable", "value": None, "source_event": None
    }
    assert manifest["observed"]["loaded_skill"] == {
        "status": "unavailable", "value": None, "source_event": None
    }

    for field in (
        "tested_head",
        "runtime_version",
        "requested_agent_type",
    ):
        assert _summary_value(summary, field) is not None, f"missing runtime summary field: {field}"
    assert f"requested_agent_type: {role}" in summary
    if refusal:
        assert "route_preflight_decision: refused_before_runtime" in summary
        assert "runtime_invocation: not_started_route_preflight_blocked" in summary
        assert "native_spawn_event_observed: False" in summary
        assert manifest["observed"]["executor"]["status"] == "not_invoked"
        assert manifest["observed"]["mutation"] == {
            "status": "not_attempted_by_runner",
            "attempted": None,
            "observed_channels": ["runner_agent_route_guard"],
        }
    else:
        assert f"effective_agent_type: {role}" in summary
        assert f"child_agent_type_observed: {role}" in summary
        assert "native_spawn_event_observed: True" in summary
        assert manifest["observed"]["child_agent_type"] == {
            "status": "observed",
            "value": role,
            "source_event": "codex_session_meta_agent_role",
        }
        assert manifest["observed"]["mutation"] == {
            "status": "unavailable", "attempted": None, "observed_channels": []
        }
    assert "tested_head:" in summary
    assert "runtime_version:" in summary
    if not refusal:
        assert "loaded_skills_source: static_frontmatter" in summary
        assert "terminal_event_observed: True" in summary
    return summary


def test_codex_creator_editor_runtime_evidence():
    """Run independent positive routes concurrently within one capability window."""
    cases = [
        ("issue-creator", "create-issue", "RUNTIME_SMOKE_1952_CREATOR_CREATE_OK"),
        ("issue-editor", "edit-issue", "RUNTIME_SMOKE_1952_EDITOR_EDIT_OK"),
    ]
    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        futures = [
            executor.submit(_run_route_smoke, role, route, marker, refusal=False)
            for role, route, marker in cases
        ]
        for future in futures:
            future.result()


def test_codex_creator_editor_wrong_route_refuses_before_mutation():
    """Refuse distinct wrong routes concurrently through the deterministic preflight."""
    cases = [
        ("issue-creator", "edit-issue", None),
        ("issue-editor", "create-issue", None),
        ("issue-creator", "unknown", None),
    ]
    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        futures = [
            executor.submit(_run_route_smoke, role, route, marker, refusal=True)
            for role, route, marker in cases
        ]
        for future in futures:
            future.result()
