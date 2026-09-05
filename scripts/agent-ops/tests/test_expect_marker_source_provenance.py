"""Issue #2498 (AC3/AC4/AC7): ``--expect-marker-source main|subagent`` and
``--expect-skill-command`` -- an additive main/subagent evidence-provenance
CLI contract for the SubAgent causal-evidence default gate (Issue #2183
fix-delta), plus its native ``UserPromptExpansion.command_name`` evidence
channel.

AC3/AC4 are hermetic: a fake ``claude`` executable (a small Python script,
not bash -- chosen so the fixture stream-json event JSON never has to
survive bash quoting) emits synthetic stream-json event text; no live
Claude Code process is spawned. Module-load / fixture-repo conventions
mirror ``test_run_worktree_agent_runtime_smoke.py``.

AC7 is a genuine runtime-verification test (``decision: immediate`` per this
Issue's own ``## Runtime Verification Applicability``): it spawns a REAL
``claude`` process against a synthetic, hermetic fixture project containing
a project-level Skill literally named ``review-issue`` (a SAFE stand-in --
never the real ``.claude/skills/review-issue/SKILL.md``, whose Procedure
would attempt real ``gh``/GitHub network calls this Issue's own
``network_required: false`` contract forbids) whose entire body is an
instruction to reply with one fixed marker string and call no tools. This
proves the plumbing (native ``UserPromptExpansion`` evidence,
``--expect-marker-source main`` opt-out, non-fallback PASS, no SubAgent
spawn) end-to-end without depending on any real GitHub network access or
the real review-issue Skill's own (out-of-scope for this Issue) behavior.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env)


def _build_repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)

    worktree = repo / ".claude" / "worktrees" / "issue-0000-fixture"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git("branch", "worktree-fixture", cwd=repo)
    _git("worktree", "add", str(worktree), "worktree-fixture", cwd=repo)
    return repo, worktree


def _prompt_file(tmp_path: Path, text: str) -> Path:
    prompt = tmp_path / "prompt.md"
    prompt.write_text(text, encoding="utf-8")
    return prompt


def _run(
    repo: Path,
    worktree: Path,
    *args: str,
    fake_bin_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if fake_bin_dir is not None:
        env["PATH"] = f"{fake_bin_dir}:{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), "--worktree", str(worktree), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _write_fake_claude(path: Path, stdout_lines: list[str]) -> None:
    """A fake ``claude`` executable written as a Python script (never bash)
    so arbitrarily-shaped fixture JSON (quotes, braces, embedded newlines)
    never has to survive bash string-quoting. Ignores argv entirely (this
    runner's own preflight only checks the binary exists and is
    executable; capability classification reads stdout/stderr, not
    argv-echoing), reads (and discards) stdin exactly like the real
    ``claude -p`` invocation's own stdin-prompt contract, then prints the
    given lines verbatim."""
    script_lines = ["#!/usr/bin/env python3", "import sys", "sys.stdin.read()"]
    for line in stdout_lines:
        script_lines.append(f"print({line!r})")
    script_lines.append("sys.exit(0)")
    path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _line(payload: dict) -> str:
    return json.dumps(payload)


def _init_event() -> str:
    return _line({"type": "system", "subtype": "init"})


def _result_event(marker: str | None = None) -> str:
    payload = {"type": "result", "subtype": "success"}
    if marker is not None:
        payload["result"] = marker
    return _line(payload)


def _assistant_text_event(text: str) -> str:
    return _line({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def _user_prompt_expansion_event(command_name: str) -> str:
    """Mirrors a live Claude Code 2.1.261 ``UserPromptExpansion`` hook
    ``command:"cat"`` echo -- confirmed by manually registering the hook
    against a real ``claude -p`` invocation and inspecting its stream-json
    output during this Issue's own investigation."""
    embedded = json.dumps(
        {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_name": command_name,
            "command_args": "",
            "command_source": "projectSettings",
            "prompt": f"/{command_name}",
        }
    )
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "UserPromptExpansion",
            "hook_name": "UserPromptExpansion",
            "stdout": embedded,
            "output": embedded,
        }
    )


def _user_prompt_expansion_event_with_prompt_text(command_name: str, prompt_text: str) -> str:
    """Same native ``UserPromptExpansion`` hook echo shape as
    ``_user_prompt_expansion_event``, but with a CALLER-supplied
    ``prompt``/``command_args`` text -- used by the PR #2500 fix_delta
    P1-1 regression test to embed a marker string ONLY in the hook's own
    echoed input, never in any assistant/result event."""
    embedded = json.dumps(
        {
            "hook_event_name": "UserPromptExpansion",
            "expansion_type": "slash_command",
            "command_name": command_name,
            "command_args": prompt_text,
            "command_source": "projectSettings",
            "prompt": prompt_text,
        }
    )
    return _line(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_event": "UserPromptExpansion",
            "hook_name": "UserPromptExpansion",
            "stdout": embedded,
            "output": embedded,
        }
    )


def _write_fake_claude_with_invocation_marker(
    path: Path, invocation_marker: Path, stdout_lines: list[str]
) -> None:
    """Same shape as ``_write_fake_claude``, but the script also touches
    ``invocation_marker`` as its very first action -- used by the PR #2500
    fix_delta P2-3 regression test to prove the runtime subprocess was
    NEVER launched at all (as opposed to launched-and-then-erroring)."""
    script_lines = [
        "#!/usr/bin/env python3",
        "import sys",
        f"open({str(invocation_marker)!r}, 'w').write('invoked')",
        "sys.stdin.read()",
    ]
    for line in stdout_lines:
        script_lines.append(f"print({line!r})")
    script_lines.append("sys.exit(0)")
    path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


MARKER = "MARKER_ISSUE_2498"


# ---------------------------------------------------------------------------
# AC3: --expect-marker-source main opts OUT of the --expect-marker default
# SubAgent causal-evidence gate; --expect-marker-source subagent (including
# the omitted default) is byte-identical to the pre-#2498 behavior.
# ---------------------------------------------------------------------------


def test_expect_marker_source_main_opts_out_of_causal_evidence(tmp_path):
    """GIVEN a fixture with NO SubagentStart/SubagentStop hook evidence at
    all (causal_evidence_source stays no_evidence/marker_only_insufficient)
    but WITH a UserPromptExpansion command_name match, WHEN run with
    --expect-marker-source main --expect-skill-command <name>, THEN the
    run PASSes despite the causal-evidence gap (Issue #2498 AC3) -- and the
    exact SAME fixture, run with --expect-marker-source subagent (the
    default), still FAILs exactly like every pre-#2498 caller (AC5:
    no regression)."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_claude(
        fake_bin / "claude",
        [
            _init_event(),
            _user_prompt_expansion_event("review-issue"),
            _assistant_text_event(MARKER),
            _result_event(MARKER),
        ],
    )
    prompt = _prompt_file(tmp_path, "/review-issue")

    # main source: opts OUT of the causal-evidence gate -> PASS.
    evidence_json_main = tmp_path / "evidence-main.json"
    result_main = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out-main"),
        "--evidence-json", str(evidence_json_main),
        "--expect-marker", MARKER,
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
        fake_bin_dir=fake_bin,
    )
    assert result_main.returncode == 0, result_main.stderr
    evidence_main = json.loads(evidence_json_main.read_text(encoding="utf-8"))
    assert evidence_main["expect_marker_source"] == "main"
    assert evidence_main["subagent_causal_evidence"]["causal_evidence_source"] != "hook_id_correlated"
    assert evidence_main["expect_skill_command_observed"] is True

    # subagent source (explicit) and the omitted default: still FAIL, exactly
    # like every pre-#2498 --expect-marker caller (Issue #2183 fix-delta).
    for extra_args in (["--expect-marker-source", "subagent"], []):
        evidence_json_sub = tmp_path / f"evidence-sub-{len(extra_args)}.json"
        result_sub = _run(
            repo, worktree,
            "--runtime", "claude", "--mode", "structured",
            "--prompt-file", str(prompt), "--output-dir", str(tmp_path / f"out-sub-{len(extra_args)}"),
            "--evidence-json", str(evidence_json_sub),
            "--expect-marker", MARKER,
            *extra_args,
            fake_bin_dir=fake_bin,
        )
        assert result_sub.returncode == 1, result_sub.stderr
        evidence_sub = json.loads(evidence_json_sub.read_text(encoding="utf-8"))
        assert evidence_sub["expect_marker_source"] == "subagent"
        assert any("subagent causal evidence insufficient" in e for e in evidence_sub["errors"])


def test_expect_marker_source_main_rejects_marker_satisfied_only_by_hook_echo(tmp_path):
    """PR #2500 fix_delta P1-1 (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
    a marker string present ONLY in the ``UserPromptExpansion`` hook's own
    echoed ``prompt``/``command_args`` text (i.e. text the CALLER wrote
    into the prompt, never something the model itself produced) must NOT
    satisfy ``--expect-marker-source main`` -- the marker must be reported
    missing and the run must FAIL, even though the same marker string
    trivially appears in the raw combined stdout+stderr blob (proving the
    fix actually restricts the search text, not merely that the marker is
    absent everywhere)."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    leaked_marker = "LEAKED_VIA_HOOK_ECHO_ONLY"
    _write_fake_claude(
        fake_bin / "claude",
        [
            _init_event(),
            _user_prompt_expansion_event_with_prompt_text(
                "review-issue", f"/review-issue please print {leaked_marker} when done"
            ),
            _assistant_text_event("some unrelated model output"),
            _result_event("some unrelated model output"),
        ],
    )
    prompt = _prompt_file(tmp_path, "/review-issue")
    evidence_json = tmp_path / "evidence.json"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--evidence-json", str(evidence_json),
        "--expect-marker", leaked_marker,
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stderr
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["expected_markers_missing"] == [leaked_marker]


def test_ordered_marker_match_does_not_double_count_terminal_result_replay(tmp_path):
    """PR #2500 fix_delta P1-2 (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
    a reverse-order single answer ("STEP_B STEP_A") that appears both as
    the assistant's own final text AND (identically) as the terminal
    ``result`` event's replay of that same text must NOT be accepted as
    forward-order evidence for ``--expect-ordered-marker STEP_A
    --expect-ordered-marker STEP_B`` -- a naive raw-stdout cursor scan
    could otherwise find STEP_A in the assistant event's own occurrence
    and STEP_B only in the result event's later, duplicate replay of the
    SAME content, incorrectly reporting forward order for content that
    was actually reversed within one single answer."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    reversed_answer = "STEP_B STEP_A"
    _write_fake_claude(
        fake_bin / "claude",
        [
            _init_event(),
            _assistant_text_event(reversed_answer),
            _result_event(reversed_answer),
        ],
    )
    prompt = _prompt_file(tmp_path, "do the steps")
    evidence_json = tmp_path / "evidence.json"
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--evidence-json", str(evidence_json),
        "--expect-ordered-marker", "STEP_A",
        "--expect-ordered-marker", "STEP_B",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stderr
    evidence = json.loads(evidence_json.read_text(encoding="utf-8"))
    assert evidence["ordered_evidence_match"]["verified"] is False
    assert evidence["ordered_evidence_match"]["missing_markers"] == ["STEP_B"]


def test_invalid_output_schema_path_fails_before_runtime_launch(tmp_path):
    """PR #2500 fix_delta P2-3 (OWNER REQUEST_CHANGES
    https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
    a structurally invalid JSON Schema file passed via
    ``--output-schema-path`` must be rejected as a controlled, explained
    usage error BEFORE the (possibly expensive) claude runtime subprocess
    is ever launched -- never an unhandled
    ``jsonschema.exceptions.SchemaError`` traceback raised mid-run after
    runtime cost has already been paid. The fake ``claude`` executable
    touches an invocation marker file as its very first action, so this
    test can assert the runtime was never launched at all (not merely
    that it failed for some other reason)."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation_marker = tmp_path / "claude_was_invoked.marker"
    _write_fake_claude_with_invocation_marker(
        fake_bin / "claude",
        invocation_marker,
        [_init_event(), _result_event(json.dumps({"anything": "here"}))],
    )
    schema_path = tmp_path / "invalid_schema.json"
    schema_path.write_text(json.dumps({"type": "strnig"}), encoding="utf-8")
    prompt = _prompt_file(tmp_path, "hello")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--output-schema-path", str(schema_path),
        fake_bin_dir=fake_bin,
    )
    assert result.returncode not in (0, 77), result.stdout
    assert "Traceback" not in result.stderr
    assert not invocation_marker.exists(), (
        "runtime must not be launched when --output-schema-path is structurally invalid"
    )


def test_expect_marker_source_main_without_expect_skill_command_is_usage_error(tmp_path):
    """--expect-marker-source main alone (no --expect-skill-command) must
    be rejected as a usage error BEFORE any process is spawned -- it must
    never silently degrade into an unconditional causal-evidence opt-out
    (Issue #2498 AC4, Step 2.5 semantic design review, severity: high)."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    prompt = _prompt_file(tmp_path, "/review-issue")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--expect-marker-source", "main",
    )
    assert result.returncode not in (0, 77), result.stdout
    assert "--expect-skill-command" in result.stderr


def test_expect_skill_command_requires_structured_mode(tmp_path):
    repo, worktree = _build_repo_with_worktree(tmp_path)
    prompt = _prompt_file(tmp_path, "/review-issue")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "interactive",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
    )
    assert result.returncode not in (0, 77)
    assert "--expect-skill-command" in result.stderr


# ---------------------------------------------------------------------------
# AC4: --expect-skill-command matches native UserPromptExpansion.command_name
# evidence (never the undocumented command_source value domain).
# ---------------------------------------------------------------------------


def test_expect_skill_command_native_user_prompt_expansion_evidence(tmp_path):
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_claude(
        fake_bin / "claude",
        [
            _init_event(),
            _user_prompt_expansion_event("review-issue"),
            _result_event("no side effects"),
        ],
    )
    prompt = _prompt_file(tmp_path, "/review-issue")

    # Matching command name -> PASS.
    evidence_json_match = tmp_path / "evidence-match.json"
    result_match = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out-match"),
        "--evidence-json", str(evidence_json_match),
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
        fake_bin_dir=fake_bin,
    )
    assert result_match.returncode == 0, result_match.stderr
    evidence_match = json.loads(evidence_json_match.read_text(encoding="utf-8"))
    assert evidence_match["user_prompt_expansion_command_names"] == ["review-issue"]
    assert evidence_match["expect_skill_command_observed"] is True

    # Mismatching command name -> FAIL (never PASS on presence alone).
    evidence_json_mismatch = tmp_path / "evidence-mismatch.json"
    result_mismatch = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out-mismatch"),
        "--evidence-json", str(evidence_json_mismatch),
        "--expect-marker-source", "main",
        "--expect-skill-command", "some-other-skill",
        fake_bin_dir=fake_bin,
    )
    assert result_mismatch.returncode == 1, result_mismatch.stderr
    evidence_mismatch = json.loads(evidence_json_mismatch.read_text(encoding="utf-8"))
    assert evidence_mismatch["expect_skill_command_observed"] is False


def test_expect_skill_command_absent_evidence_fails(tmp_path):
    """No UserPromptExpansion hook event observed at all (e.g. a runtime
    that never wired the hook) must FAIL, never PASS on the absence of
    contrary evidence."""
    repo, worktree = _build_repo_with_worktree(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_claude(fake_bin / "claude", [_init_event(), _result_event("no evidence at all")])
    prompt = _prompt_file(tmp_path, "/review-issue")
    result = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out"),
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
        fake_bin_dir=fake_bin,
    )
    assert result.returncode == 1, result.stderr


# ---------------------------------------------------------------------------
# AC7 (runtime-verification: true): a REAL claude process, a SAFE synthetic
# stand-in Skill literally named "review-issue" (never the real
# .claude/skills/review-issue/SKILL.md -- see module docstring).
# ---------------------------------------------------------------------------

_STAND_IN_REVIEW_ISSUE_SKILL_MD = (
    """---
name: review-issue
description: Issue #2498 AC7 hermetic stand-in for direct-Skill-invocation """
    """runtime verification. Never the real review-issue Skill. Trigger word: review-issue.
---

# Review Issue (AC7 stand-in)

Reply with exactly this text and nothing else: REVIEW_ISSUE_STAND_IN_OK

Do not call any tools. Do not run any commands. Do not access the network.
"""
)


def test_live_skill_invocation_main_source_non_fallback_pass(tmp_path):
    """Issue #2498 AC7: real ``claude`` process, direct ``/review-issue``
    Skill invocation (a safe, hermetic stand-in Skill -- see module
    docstring), no SubAgent delegation. Verifies both:

    1. ``--expect-marker-source main --expect-skill-command review-issue``
       gets a non-fallback PASS with no SubAgent spawn observed.
    2. The SAME fixture, run with the existing default
       (``--expect-marker-source`` omitted) plus ``--expect-marker``,
       still correctly FAILs the causal-evidence gate (no regression in
       causal-evidence strength for the pre-existing default path,
       Runtime Verification Applicability skip_conditions/fallback_policy
       per this Issue's own contract)."""
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not available in this environment")

    repo, worktree = _build_repo_with_worktree(tmp_path)
    skill_dir = worktree / ".claude" / "skills" / "review-issue"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_STAND_IN_REVIEW_ISSUE_SKILL_MD, encoding="utf-8")
    prompt = _prompt_file(tmp_path, "/review-issue")

    # PR #2500 fix_delta P2-1 (OWNER REQUEST_CHANGES
    # https://github.com/squne121/loop-protocol/pull/2500#issuecomment-5549720805):
    # this main-path invocation MUST also pass --expect-marker so the
    # --expect-marker-source main gate is actually exercised end-to-end
    # (previously this run only checked expect_skill_command_observed and
    # no-child-spawn, never the marker-based non-fallback PASS path at
    # all).
    evidence_json_main = tmp_path / "evidence-main.json"
    result_main = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out-main"),
        "--evidence-json", str(evidence_json_main),
        "--timeout-seconds", "90",
        "--max-turns", "1",
        "--expect-marker", "REVIEW_ISSUE_STAND_IN_OK",
        "--expect-marker-source", "main",
        "--expect-skill-command", "review-issue",
    )
    if result_main.returncode == 77:
        pytest.skip(
            "runtime smoke SKIP (capability/auth/herdr unavailable in this "
            f"environment): {result_main.stderr}"
        )
    assert result_main.returncode == 0, result_main.stderr
    evidence_main = json.loads(evidence_json_main.read_text(encoding="utf-8"))
    assert evidence_main["capability_decision"] != "capability_skip"
    # The marker was actually observed via the main-output channel (P1-1),
    # and the run is a non-fallback PASS WITHOUT requiring SubAgent
    # causal evidence (--expect-marker-source main's whole point).
    assert evidence_main["expected_markers_missing"] == []
    assert evidence_main["expect_skill_command_observed"] is True
    assert "review-issue" in evidence_main["user_prompt_expansion_command_names"]
    assert evidence_main["child_spawn_observed"] is False
    assert evidence_main["spawn_events"] == []

    # Same execution set, existing default causal-evidence path: no
    # SubAgent was ever spawned in this run either, so the pre-existing
    # --expect-marker default gate must still correctly FAIL here -- proof
    # this stand-in fixture is not accidentally satisfying the gate some
    # other way, and that the default path's causal-evidence strength is
    # unchanged by this Issue's additive flags.
    evidence_json_regression = tmp_path / "evidence-regression.json"
    result_regression = _run(
        repo, worktree,
        "--runtime", "claude", "--mode", "structured",
        "--prompt-file", str(prompt), "--output-dir", str(tmp_path / "out-regression"),
        "--evidence-json", str(evidence_json_regression),
        "--timeout-seconds", "90",
        "--max-turns", "1",
        "--expect-marker", "REVIEW_ISSUE_STAND_IN_OK",
    )
    assert result_regression.returncode == 1, result_regression.stderr
    evidence_regression = json.loads(evidence_json_regression.read_text(encoding="utf-8"))
    assert evidence_regression["expect_marker_source"] == "subagent"
    # PR #2500 fix_delta P2-1: the failure reason must SPECIFICALLY be
    # causal-evidence insufficiency, not the marker being missing --
    # mirroring the same failure-reason-checking rigor the fake-executable
    # tests above apply (e.g. "subagent causal evidence insufficient" in
    # evidence["errors"]). The marker itself WAS observed (this same
    # model, same reply, same combined-stdout+stderr search text as the
    # pre-existing default path uses), so the ONLY remaining reason this
    # run FAILs is the missing SubAgent causal evidence.
    assert evidence_regression["expected_markers_missing"] == []
    assert (
        evidence_regression["subagent_causal_evidence"]["causal_evidence_source"]
        != "hook_id_correlated"
    )
    assert any(
        "subagent causal evidence insufficient" in e for e in evidence_regression["errors"]
    )
