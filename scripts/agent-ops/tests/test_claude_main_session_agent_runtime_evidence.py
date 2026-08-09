"""Issue #2046: fake-runtime deterministic CI test for the MAIN-session
agent identity / definition binding / Skill evidence separation / canonical
Read receipt / mutation boundary / settings provenance evidence added to
``run_worktree_agent_runtime_smoke.py``.

This is a dedicated new test file (not appended to the pre-existing general
smoke suite), per Issue #1960's Current Validated Scope / Issue #1285 /
PR #1305 VC contract convention (also followed by
``test_run_worktree_agent_runtime_smoke_runtime_evidence.py`` and
``test_claude_spawn_identity_evidence.py``).

Two tiers:

1. Function-level tests against synthetic ``stream-json`` event text
   (fake hook events / fake tool_use / tool_result payloads), exercising
   every evidence-building function directly and deterministically -- no
   subprocess, no network, no real Claude Code CLI.
2. A smaller set of end-to-end tests that drive the real ``main()`` entry
   point against a fake ``claude`` binary (a bash script writing the exact
   ``stream-json`` shape a real invocation would produce), proving the new
   fields are actually wired into ``schema_summary``/``summary.md`` and not
   merely reachable in isolation.

Negative cases required by Issue #2046 AC6: SessionStart identity match /
exact Read receipts / missing event / identity mismatch / wrong path /
failed Read result / marker-only false positive / mutation event / settings
digest mismatch (distinctness) / stale head / output-dir collision /
postcondition change.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2046"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, SCRIPT)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = smoke
_spec.loader.exec_module(smoke)


SESSION_ID = "6c1d9b2a-4e3f-4a10-9b2e-1234567890ab"
ISSUE_CREATOR = "issue-creator"
ISSUE_EDITOR = "issue-editor"
CANONICAL_CREATOR_PATH = ".claude/skills/create-issue/SKILL.md"
CANONICAL_EDITOR_PATH = ".claude/skills/edit-issue/SKILL.md"


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _system_init() -> str:
    return _line({"type": "system", "subtype": "init", "session_id": SESSION_ID})


def _session_start_hook(agent_type: str) -> str:
    """Fake ``SessionStart`` hook event carrying the official hook stdin
    payload shape (mirrors ``_official_hook_payload`` in
    ``test_claude_spawn_identity_evidence.py``, scoped to SessionStart)."""
    official_payload = json.dumps(
        {
            "session_id": SESSION_ID,
            "hook_event_name": "SessionStart",
            "agent_type": agent_type,
        }
    )
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "SessionStart",
            "hook_name": "SessionStart",
            "session_id": SESSION_ID,
            "stdout": official_payload,
            "output": official_payload,
            "exit_code": 0,
        }
    )


def _read_tool_use(tool_use_id: str, file_path: str) -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {"type": "tool_use", "id": tool_use_id, "name": "Read", "input": {"file_path": file_path}}
                ]
            },
        }
    )


def _read_tool_result(tool_use_id: str, *, is_error: bool = False) -> str:
    return _line(
        {
            "type": "user",
            "session_id": SESSION_ID,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": "ok"}
                ]
            },
        }
    )


def _bash_tool_use(command: str = "echo hi") -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_bash1", "name": "Bash", "input": {"command": command}}]
            },
        }
    )


def _assistant_text(text: str) -> str:
    return _line(
        {
            "type": "assistant",
            "session_id": SESSION_ID,
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def _result_event() -> str:
    return _line({"type": "result", "subtype": "success", "session_id": SESSION_ID})


# ---------------------------------------------------------------------------
# Tier 1: function-level tests against synthetic stream-json event text
# ---------------------------------------------------------------------------


class TestMainAgentIdentity:
    def test_session_start_identity_match(self) -> None:
        """AC1 positive: requested == observed -> matched True, status observed."""
        stdout = "\n".join([_system_init(), _session_start_hook(ISSUE_CREATOR), _result_event()])
        identity = smoke.build_main_agent_identity(ISSUE_CREATOR, stdout)
        assert identity["requested"] == {"agent_name": ISSUE_CREATOR, "source": "runner_argv"}
        assert identity["observed"]["agent_type"] == ISSUE_CREATOR
        assert identity["observed"]["status"] == smoke.EVIDENCE_STATUS_OBSERVED
        assert identity["matched"] is True
        assert identity["status"] == smoke.EVIDENCE_STATUS_OBSERVED

    def test_missing_session_start_event(self) -> None:
        """AC1 negative: no SessionStart hook at all -> unavailable, never matched."""
        stdout = "\n".join([_system_init(), _result_event()])
        identity = smoke.build_main_agent_identity(ISSUE_CREATOR, stdout)
        assert identity["observed"]["agent_type"] is None
        assert identity["observed"]["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert identity["matched"] is False
        assert identity["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE

    def test_identity_mismatch(self) -> None:
        """AC1 negative: observed agent_type differs from requested -> matched False,
        but the observation itself is still honestly recorded as observed."""
        stdout = "\n".join([_system_init(), _session_start_hook(ISSUE_EDITOR), _result_event()])
        identity = smoke.build_main_agent_identity(ISSUE_CREATOR, stdout)
        assert identity["observed"]["agent_type"] == ISSUE_EDITOR
        assert identity["observed"]["status"] == smoke.EVIDENCE_STATUS_OBSERVED
        assert identity["matched"] is False

    def test_not_requested_stays_unavailable(self) -> None:
        """No --claude-agent-name at all -> not a FAIL condition, honestly unavailable."""
        identity = smoke.build_main_agent_identity(None, "irrelevant")
        assert identity["requested"]["agent_name"] is None
        assert identity["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE

    def test_model_self_report_text_never_fills_observed(self) -> None:
        """A model merely SAYING its own agent type in prose must never be
        treated as observed identity evidence -- only the SessionStart hook
        channel counts."""
        stdout = "\n".join(
            [_system_init(), _assistant_text(f"I am running as {ISSUE_CREATOR}."), _result_event()]
        )
        identity = smoke.build_main_agent_identity(ISSUE_CREATOR, stdout)
        assert identity["observed"]["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert identity["matched"] is False


class TestAgentDefinitionBinding:
    def test_no_agent_requested(self) -> None:
        definition, payload, name = smoke.resolve_agent_definition("/nonexistent", None, False)
        assert definition["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert payload is None and name is None

    def test_project_discovery_status_unavailable_but_source_recorded(self, tmp_path) -> None:
        """AC2: project-discovery lane records the intended source but its
        status stays unavailable (no channel confirms the effective source
        actually loaded)."""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            f"---\nname: {ISSUE_CREATOR}\ndescription: test\nskills:\n  - create-issue\n---\nbody\n",
            encoding="utf-8",
        )
        definition, payload, name = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, False)
        assert definition["binding_mode"] == "project_discovery"
        assert definition["intended_repo_path"] == f".claude/agents/{ISSUE_CREATOR}.md"
        assert definition["intended_sha256"] is not None
        assert definition["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert payload is None and name is None

    def test_hermetic_binding_produces_declared_digests(self, tmp_path) -> None:
        """AC2: hermetic lane records both the source file digest and the
        generated session-local --agents payload digest, status declared."""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            f"---\nname: {ISSUE_CREATOR}\ndescription: test\nskills:\n  - create-issue\n---\nbody text\n",
            encoding="utf-8",
        )
        definition, payload, name = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, True)
        assert definition["binding_mode"] == "hermetic"
        assert definition["status"] == smoke.EVIDENCE_STATUS_DECLARED
        assert definition["intended_sha256"]
        assert definition["hermetic_payload_sha256"]
        assert payload is not None and name is not None
        assert name in payload
        assert payload[name]["tools"] == ["Read"]

    def test_settings_digest_mismatch_across_different_sources(self, tmp_path) -> None:
        """Distinctness check (AC6 'settings digest mismatch'): two
        differing candidate Agent definitions must produce two different
        hermetic payload digests -- the digest is not a constant."""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            "---\nname: issue-creator\ndescription: v1\n---\nbody v1\n", encoding="utf-8"
        )
        definition_v1, _payload_v1, _name_v1 = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            "---\nname: issue-creator\ndescription: v2 (changed)\n---\nbody v2\n", encoding="utf-8"
        )
        definition_v2, _payload_v2, _name_v2 = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, True)
        assert definition_v1["intended_sha256"] != definition_v2["intended_sha256"]
        assert definition_v1["hermetic_payload_sha256"] != definition_v2["hermetic_payload_sha256"]

    def test_hermetic_digest_deterministic_for_same_source(self, tmp_path) -> None:
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            "---\nname: issue-creator\ndescription: stable\n---\nbody\n", encoding="utf-8"
        )
        definition_a, _p, _n = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, True)
        definition_b, _p2, _n2 = smoke.resolve_agent_definition(str(tmp_path), ISSUE_CREATOR, True)
        assert definition_a["hermetic_payload_sha256"] == definition_b["hermetic_payload_sha256"]


class TestCanonicalReadReceipt:
    def test_exact_read_receipt_observed(self, tmp_path) -> None:
        """AC4 positive: normalized path match + matching tool_use_id +
        success result -> observed."""
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
                _read_tool_result("toolu_read1", is_error=False),
                _result_event(),
            ]
        )
        receipt = smoke.extract_claude_canonical_read_receipt(stdout, str(tmp_path), CANONICAL_CREATOR_PATH)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_OBSERVED
        assert receipt["tool_name"] == "Read"
        assert receipt["tool_use_id"] == "toolu_read1"
        assert receipt["read_result_status"] == "success"
        assert receipt["expected_sha256"] is not None

    def test_wrong_path_stays_unavailable(self, tmp_path) -> None:
        """AC4 negative: a Read of a different file must not satisfy the receipt."""
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", ".claude/skills/create-issue/scripts/create_issue_txn.py"),
                _read_tool_result("toolu_read1", is_error=False),
                _result_event(),
            ]
        )
        receipt = smoke.extract_claude_canonical_read_receipt(stdout, str(tmp_path), CANONICAL_CREATOR_PATH)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert receipt["observed_repo_relative_path"] is None

    def test_failed_read_result_stays_unavailable(self, tmp_path) -> None:
        """AC4 negative: a failed tool_result must not be treated as evidence."""
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
                _read_tool_result("toolu_read1", is_error=True),
                _result_event(),
            ]
        )
        receipt = smoke.extract_claude_canonical_read_receipt(stdout, str(tmp_path), CANONICAL_CREATOR_PATH)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert receipt["read_result_status"] == "error"

    def test_marker_only_false_positive_rejected(self, tmp_path) -> None:
        """AC4/AC6 negative: prose that merely NAMES the canonical path (no
        real Read tool_use/tool_result pair) must not fabricate observed
        evidence."""
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _assistant_text(f"I have read {CANONICAL_CREATOR_PATH} successfully."),
                _result_event(),
            ]
        )
        receipt = smoke.extract_claude_canonical_read_receipt(stdout, str(tmp_path), CANONICAL_CREATOR_PATH)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert receipt["tool_use_id"] is None

    def test_unmatched_tool_use_id_rejected(self, tmp_path) -> None:
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
                _read_tool_result("toolu_DIFFERENT", is_error=False),
                _result_event(),
            ]
        )
        receipt = smoke.extract_claude_canonical_read_receipt(stdout, str(tmp_path), CANONICAL_CREATOR_PATH)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE

    def test_no_expected_path_for_unknown_persona(self, tmp_path) -> None:
        receipt = smoke.extract_claude_canonical_read_receipt("irrelevant", str(tmp_path), None)
        assert receipt["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert receipt["expected_repo_relative_path"] is None


class TestSkillEvidenceSeparation:
    def test_declaration_preload_canonical_read_are_separate(self, tmp_path) -> None:
        """AC3: three sub-objects, never conflated -- preload is never
        promoted to observed just because declaration/canonical_read are."""
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
            "---\nname: issue-creator\ndescription: d\nskills:\n  - create-issue\n---\nbody\n",
            encoding="utf-8",
        )
        skills_dir = tmp_path / ".claude" / "skills" / "create-issue"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
                _read_tool_result("toolu_read1", is_error=False),
                _result_event(),
            ]
        )
        evidence = smoke.build_skill_evidence(ISSUE_CREATOR, str(tmp_path), stdout)
        assert evidence["declaration"]["status"] == smoke.EVIDENCE_STATUS_DECLARED
        assert evidence["declaration"]["skills"] == ["create-issue"]
        assert evidence["preload"]["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert evidence["canonical_read"]["status"] == smoke.EVIDENCE_STATUS_OBSERVED

    def test_preload_never_reports_observed(self, tmp_path) -> None:
        """No fixture can make preload.status == observed -- there is no
        native preload-confirmation channel; this pins that invariant."""
        evidence = smoke.build_skill_evidence(ISSUE_CREATOR, str(tmp_path), "any stdout")
        assert evidence["preload"]["status"] != smoke.EVIDENCE_STATUS_OBSERVED


class TestMutationBoundary:
    def test_non_hermetic_stays_unavailable(self) -> None:
        boundary = smoke.build_mutation_boundary(False, None, None, "irrelevant")
        assert boundary["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE
        assert boundary["mutation_capable_tool_event_count"] is None

    def test_mutation_event_detected(self) -> None:
        """AC5/AC6 negative: a Bash tool_use during the hermetic no-mutation
        lane is recorded as a real event, never silently absorbed."""
        stdout = "\n".join([_system_init(), _bash_tool_use(), _result_event()])
        events = smoke.count_mutation_capable_tool_events(stdout)
        assert events == [{"tool": "Bash"}]
        boundary = smoke.build_mutation_boundary(True, "deadbeef", ["claude", "-p"], stdout)
        assert boundary["status"] == smoke.EVIDENCE_STATUS_OBSERVED
        assert boundary["mutation_capable_tool_event_count"] == 1
        assert boundary["settings_digest_sha256"] == "deadbeef"

    def test_no_mutation_event_when_only_read_used(self) -> None:
        stdout = "\n".join(
            [
                _system_init(),
                _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
                _read_tool_result("toolu_read1"),
                _result_event(),
            ]
        )
        events = smoke.count_mutation_capable_tool_events(stdout)
        assert events == []
        boundary = smoke.build_mutation_boundary(True, "abc123", ["claude"], stdout)
        assert boundary["mutation_capable_tool_event_count"] == 0

    def test_effective_argv_redacted(self) -> None:
        boundary = smoke.build_mutation_boundary(
            True, "digest", ["claude", "--agents", "/home/someuser/tmp/agents.json"], "any"
        )
        assert all("/home/" not in str(a) for a in boundary["effective_argv"])


class TestSettingsProvenance:
    def test_hermetic_declared(self) -> None:
        prov = smoke.build_settings_provenance("/irrelevant", True, "digest123")
        assert prov == {"source": "session_local_generated", "digest_sha256": "digest123", "status": "declared"}

    def test_project_default_missing_file_unavailable(self, tmp_path) -> None:
        prov = smoke.build_settings_provenance(str(tmp_path), False, None)
        assert prov["source"] == "project_default"
        assert prov["status"] == smoke.EVIDENCE_STATUS_UNAVAILABLE

    def test_project_default_present_file_declared(self, tmp_path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir(parents=True)
        (settings_dir / "settings.json").write_text("{}", encoding="utf-8")
        prov = smoke.build_settings_provenance(str(tmp_path), False, None)
        assert prov["status"] == smoke.EVIDENCE_STATUS_DECLARED
        assert prov["digest_sha256"] is not None


# ---------------------------------------------------------------------------
# Tier 2: end-to-end fake-binary invocations of main()
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


@pytest.fixture()
def candidate_worktree_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """AC8: a hermetic, disposable repo + linked worktree fixture (never a
    fixed reference to any single historical Issue's worktree, e.g. the
    superseded ``.claude/worktrees/issue-1734-...``). Seeds the exact agent
    definition / canonical Skill files the new evidence functions read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)

    agents_dir = repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / f"{ISSUE_CREATOR}.md").write_text(
        "---\nname: issue-creator\ndescription: fixture\nskills:\n  - create-issue\n---\nfixture body\n",
        encoding="utf-8",
    )
    skills_dir = repo / ".claude" / "skills" / "create-issue"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text("fixture skill body\n", encoding="utf-8")

    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


def _write_fake_exe(path: Path, script_body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _prompt_file(tmp_path: Path, text: str = "hello from test\n") -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run(
    repo: Path,
    worktree: Path,
    *args: str,
    fake_bin_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_end_to_end_identity_match_and_canonical_read_observed(candidate_worktree_fixture, tmp_path) -> None:
    """Full main() wiring: --claude-agent-name issue-creator drives a fake
    claude binary emitting a matching SessionStart hook plus a real Read of
    the canonical create-issue SKILL.md -- both must land in summary.md."""
    repo, worktree = candidate_worktree_fixture
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = "\n".join(
        [
            _system_init(),
            _session_start_hook(ISSUE_CREATOR),
            _read_tool_use("toolu_read1", CANONICAL_CREATOR_PATH),
            _read_tool_result("toolu_read1", is_error=False),
            _result_event(),
        ]
    )
    _write_fake_exe(
        fake_bin / "claude",
        f"""
if [ "$1" = "--version" ]; then echo "9.9.9"; exit 0; fi
cat > /dev/null
cat <<'EVENTS'
{events}
EVENTS
exit 0
""",
    )
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", ISSUE_CREATOR, "--claude-agent-name", ISSUE_CREATOR,
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 0, result.stderr
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "'matched': True" in summary or '"matched": True' in summary or "matched': True" in summary
    assert "'status': 'observed'" in summary or "status\": \"observed\"" in summary


def test_end_to_end_hermetic_mutation_event_fails_closed(candidate_worktree_fixture, tmp_path) -> None:
    """AC5/AC6 end-to-end: a hermetic no-mutation lane run whose fake binary
    emits a Bash tool_use must FAIL (exit 1), not silently pass."""
    repo, worktree = candidate_worktree_fixture
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = "\n".join([_system_init(), _bash_tool_use("rm -rf /tmp/whatever"), _result_event()])
    _write_fake_exe(
        fake_bin / "claude",
        f"""
if [ "$1" = "--version" ]; then echo "9.9.9"; exit 0; fi
cat > /dev/null
cat <<'EVENTS'
{events}
EVENTS
exit 0
""",
    )
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", ISSUE_CREATOR, "--claude-agent-name", ISSUE_CREATOR,
        "--hermetic-agent-definition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "'tool': 'Bash'" in summary or '"tool": "Bash"' in summary


def test_end_to_end_output_dir_collision(candidate_worktree_fixture, tmp_path) -> None:
    """AC6 negative case 'output-dir collision': exclusive-create is
    enforced even for the new-evidence-carrying invocation shape."""
    repo, worktree = candidate_worktree_fixture
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_exe(fake_bin / "claude", "exit 1\n")
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", ISSUE_CREATOR, "--claude-agent-name", ISSUE_CREATOR,
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    assert "exclusive create required" in result.stderr


def test_end_to_end_postcondition_change_detected(candidate_worktree_fixture, tmp_path) -> None:
    """AC6 negative case 'postcondition change': an untracked write left in
    the worktree by the fake binary is still caught even when hermetic
    evidence collection is also active."""
    repo, worktree = candidate_worktree_fixture
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = "\n".join([_system_init(), _result_event()])
    _write_fake_exe(
        fake_bin / "claude",
        f"""
if [ "$1" = "--version" ]; then echo "9.9.9"; exit 0; fi
cat > /dev/null
echo "unexpected" > "$PWD/unexpected-mutation.txt"
cat <<'EVENTS'
{events}
EVENTS
exit 0
""",
    )
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", ISSUE_CREATOR, "--claude-agent-name", ISSUE_CREATOR,
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "unexpected-mutation.txt" in summary
    (worktree / "unexpected-mutation.txt").unlink(missing_ok=True)


def test_end_to_end_stale_head_detected(candidate_worktree_fixture, tmp_path) -> None:
    """AC6 negative case 'stale head': a fake binary that commits during
    the run (moving HEAD) is caught by the existing postcondition/HEAD-
    fingerprint guard, exercised here alongside the new evidence fields."""
    repo, worktree = candidate_worktree_fixture
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    events = "\n".join([_system_init(), _result_event()])
    _write_fake_exe(
        fake_bin / "claude",
        f"""
if [ "$1" = "--version" ]; then echo "9.9.9"; exit 0; fi
cat > /dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
echo "drift" > "$PWD/drift.txt"
git -C "$PWD" add drift.txt
git -C "$PWD" commit -m "stale head drift" --quiet
cat <<'EVENTS'
{events}
EVENTS
exit 0
""",
    )
    prompt = _prompt_file(tmp_path)
    out_dir = tmp_path / "out"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(out_dir),
        "--agent-type", ISSUE_CREATOR, "--claude-agent-name", ISSUE_CREATOR,
        "--require-clean-postcondition",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1
    summary = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "HEAD moved" in summary
