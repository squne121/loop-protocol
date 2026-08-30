#!/usr/bin/env python3
"""P0 regression canary (Issue #2419): a delegated observer/evaluator Bash
tool_use attempting a git/gh mutation must be denied by
``retrospective_bash_guard_hook.py`` -- the real ``PreToolUse`` hook script
``run_retrospective.write_bash_guard_settings_file`` registers via a
run-scoped ``--settings`` file for every headless ``claude -p --agent
<name>`` subprocess this Skill launches.

Runtime Verification Applicability: immediate. Unlike a unit test that
calls ``DelegatedAgentPermissionPolicy.check_bash`` directly (already
covered in ``test_run_retrospective.py``), every test here spawns
``retrospective_bash_guard_hook.py`` as an actual subprocess, feeding it the
exact stdin JSON shape Claude Code's ``PreToolUse`` hook contract produces
-- this is what makes it a genuine "actual producer -> subprocess ->
consumer boundary" check (Issue #2419's own incident root cause was a
policy object that existed but was never reached from any real invocation
path; a test that only imports and calls the Python function in-process
would not have caught that class of wiring defect)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
_HOOK_SCRIPT = _SCRIPTS_DIR / "retrospective_bash_guard_hook.py"


def _run_hook(command: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip(), "hook produced no stdout for a Bash tool_use event"
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "command",
    [
        # Issue #2419's exact incident command class.
        "git fetch origin worktree-issue-2240-agent-retrospective-plugin",
        "git merge origin/worktree-issue-2240-agent-retrospective-plugin",
        "git merge stale-feature",
        "git commit -m 'sneaky commit'",
        "git push origin main",
        "git reset --hard origin/main",
        "git rebase main",
        "git checkout -b new-branch",
        "git branch -D old-branch",
        "git pull origin main",
        # composed into an otherwise-innocuous-looking pipeline segment.
        "git show HEAD:file.txt | git apply",
        "git log --oneline && git merge stale-feature",
        # gh mutation surface.
        "gh pr merge 1 --squash",
        "gh issue close 42",
        "gh pr comment 1 --body hi",
        "gh pr edit 1 --title x",
        "gh api repos/x/y/issues/1/comments -f body=hi",
        # pre-existing substring-blacklist bypass classes must still deny.
        "git -C . commit -m x",
        "gh --repo owner/repo issue comment 1 --body x",
        "python3 -c \"import os; os.system('gh pr merge 1')\"",
        "curl -X POST https://evil.example/payload",
        "printf data > repository-file",
        # command substitution is denied unconditionally, regardless of
        # what the substituted command itself would resolve to.
        "echo $(git merge stale-feature)",
        "echo `git merge stale-feature`",
        # PR #2425 review fix_delta (P0 bypass found in this Issue's own
        # fix): `find -exec` runs an arbitrary command that a head-token
        # allowlist never inspects.
        "find . -maxdepth 1 -exec git merge stale-feature ;",
        "find . -maxdepth 1 -execdir git commit -m x \\;",
        # PR #2425 review fix_delta: a bare interpreter/launcher head token
        # (no `-c` flag) executing a heredoc, stdin script, or script FILE
        # can shell out to a nested git mutation with nothing in the outer
        # command line for a static token scan to catch.
        "python3 script_that_merges.py",
        "python3 - <<'EOF'\nimport os\nos.system('git merge stale-feature')\nEOF",
        "uv run python3 script_that_merges.py",
        "uv run --locked python3 script_that_merges.py",
        # PR #2425 review fix_delta round 2 (2 more real bypasses found in
        # end-to-end subprocess re-testing).
        "ls\ngit merge stale-feature",
        "git -c alias.merge-x=merge merge-x stale-feature",
        "git --config alias.mx=merge mx stale-feature",
        # PR #2425 review fix_delta round 3 (OWNER REQUEST_CHANGES,
        # #2425#issuecomment-5466916997): `<(...)`/`>(...)` process
        # substitution actually executes its inner `list` as a real
        # process (OWNER-verified end-to-end: this exact command
        # fast-forwards `main` to a stale branch's SHA).
        "cat <(git merge stale-feature)",
        # P0-3: Git moved from a denylist of known mutating subcommands to
        # an allowlist of read-only ones -- these were all previously
        # unlisted (and therefore silently allowed) mutations.
        "git add sentinel.txt",
        "git hash-object -w sentinel.txt",
        "git bisect start main stale-feature",
        # P0-4: `gh` action lookup moved from token-SET membership to
        # argv-POSITION lookup -- `view` here is a branch name / `--ref`
        # value, never the actual action.
        "gh pr checkout 1 --branch view",
        "gh workflow run triage.yml --ref view",
        # P1-c: `gh api` is allowed only for an effective GET request --
        # an explicit non-GET `--method`/`-X` is always denied.
        "gh api repos/squne121/loop-protocol/issues/2419 -X POST -f title=x",
    ],
)
def test_bash_guard_hook_denies_mutation(command: str) -> None:
    decision = _run_hook(command)
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "deny", (command, decision)


def test_bash_guard_hook_passes_through_non_bash_tool_use() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    # No enforcement opinion for a non-Bash tool -- empty stdout leaves
    # Claude Code's normal permission flow for that tool call untouched.
    assert completed.stdout.strip() == ""


def test_bash_guard_hook_denies_missing_command() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    decision = json.loads(completed.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_guard_hook_denies_non_json_stdin() -> None:
    completed = subprocess.run(
        [sys.executable, str(_HOOK_SCRIPT)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    decision = json.loads(completed.stdout)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_guard_hook_exits_2_on_unexpected_exception(tmp_path: Path) -> None:
    """PR #2425 review fix_delta P1-a (#2425#issuecomment-5466916997):
    Claude Code's own PreToolUse hook contract treats ONLY exit code ``2``
    as blocking -- any other non-zero exit (the Python default for an
    uncaught exception is ``1``) is a non-blocking error whose tool call
    proceeds anyway. This test forces a REAL, uncaught exception inside
    the hook's own dependency (an ``AttributeError`` raised from
    ``build_bash_guard_hook_decision`` itself, injected via a stub
    ``run_retrospective`` module placed earlier on ``sys.path`` than the
    real one) and asserts the hook still exits ``2`` -- i.e. the fix_delta
    P1-a wrapper (``main()`` catching every exception and calling
    ``sys.exit(2)``) actually fires end-to-end through a real subprocess,
    not merely in an in-process unit test of the wrapper function."""
    # a verbatim copy of the real hook script, run from a throwaway
    # directory so `sys.path.insert(0, <this file's own dir>)` resolves to
    # the STUB `run_retrospective.py` below instead of the real module.
    stub_hook_script = tmp_path / "retrospective_bash_guard_hook.py"
    stub_hook_script.write_text(_HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "run_retrospective.py").write_text(
        "class DelegatedAgentPermissionPolicy:\n"
        "    def __init__(self, **kwargs):\n"
        "        pass\n"
        "\n"
        "\n"
        "def build_bash_guard_hook_decision(command, *, policy):\n"
        "    raise AttributeError('simulated unexpected hook dependency failure')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(stub_hook_script)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 2, (completed.returncode, completed.stdout, completed.stderr)
    # exit 2's blocking reason is conveyed via stderr (Claude Code's
    # PreToolUse contract), never a JSON decision on stdout.
    assert completed.stdout.strip() == ""
    assert "AttributeError" in completed.stderr
    assert "simulated unexpected hook dependency failure" in completed.stderr


def test_bash_guard_hook_allows_canonical_agy_builder_invocation() -> None:
    """PR #2425 review fix_delta P0-1: the canonical AGY delegation builder
    invocation `codebase-investigator` actually issues via Bash must be
    allowed -- a prior round's blanket `python3`/`uv` head exclusion
    self-blocked this exact, legitimate workflow."""
    decision = _run_hook(
        "uv run python3 .claude/skills/gemini-cli-headless-delegation/scripts/build_request.py "
        "--provider agy --profile local_asset_research --objective x --prompt y --output /tmp/out.json"
    )
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PreToolUse"
    assert hook_output["permissionDecision"] == "allow", decision


#: real repo root, resolved the exact same way `run_retrospective._REPO_ROOT`
#: resolves its own (this test file lives 5 directories below repo root:
#: tests/ -> scripts/ -> agent-retrospective/ -> skills/ -> .claude/).
_REPO_ROOT = Path(__file__).resolve().parents[5]
_CANONICAL_BUILD_REQUEST_SUFFIX = ".claude/skills/gemini-cli-headless-delegation/scripts/build_request.py"


def test_bash_guard_hook_denies_decoy_agy_script_outside_repo(tmp_path: Path) -> None:
    """PR #2425 review fix_delta round 4 (P0, decoy-script bypass): a
    `python3`/`uv` invocation of a script that merely ENDS WITH one of the
    canonical AGY delegation scripts' relative paths, but is planted
    OUTSIDE this repository (e.g. an attacker-controlled `/tmp` decoy
    copy), must be denied -- the prior `str.endswith()`-only design had no
    filesystem/repo-root anchoring and `allow`ed this exact bypass
    end-to-end. The decoy file here is a REAL, readable file on disk (an
    arbitrary `subprocess` call, in a genuine attack) with the identical
    trailing directory structure as the real canonical script, proving the
    denial is not merely "the file doesn't exist" -- it is "this is not
    THIS repo's own canonical script"."""
    decoy_script = tmp_path / "evilcopy" / _CANONICAL_BUILD_REQUEST_SUFFIX
    decoy_script.parent.mkdir(parents=True)
    decoy_script.write_text("import subprocess, sys\nsubprocess.run(['git', 'push', 'origin', 'main'])\n")
    decision = _run_hook(f"python3 {decoy_script}")
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny", decision
    assert "denied_unlisted_command:python3" in hook_output["permissionDecisionReason"]


def test_bash_guard_hook_denies_nonexistent_agy_script_path(tmp_path: Path) -> None:
    """PR #2425 review fix_delta round 4 (P0, decoy-script bypass): a
    `python3`/`uv` invocation of a path with the canonical AGY delegation
    scripts' trailing directory structure that does not exist ANYWHERE on
    disk must ALSO be denied -- the prior `str.endswith()`-only design
    never even checked filesystem existence, so a nonexistent path was
    `allow`ed identically to a real canonical invocation."""
    nonexistent_script = tmp_path / "does-not-exist" / _CANONICAL_BUILD_REQUEST_SUFFIX
    assert not nonexistent_script.exists()
    decision = _run_hook(f"python3 {nonexistent_script}")
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "deny", decision
    assert "denied_unlisted_command:python3" in hook_output["permissionDecisionReason"]


def test_bash_guard_hook_allows_canonical_agy_builder_invocation_absolute_path() -> None:
    """Regression confirmation for PR #2425 review fix_delta round 4: the
    REAL, in-repo canonical AGY delegation script, referenced by its own
    real repo-root-anchored ABSOLUTE path (not merely the relative-path
    form ``test_bash_guard_hook_allows_canonical_agy_builder_invocation``
    already covers), must still be allowed after anchoring this capability
    to `Path.resolve()`d containment instead of `str.endswith()`."""
    canonical_script = _REPO_ROOT / _CANONICAL_BUILD_REQUEST_SUFFIX
    assert canonical_script.is_file(), "canonical AGY builder script must exist in this repo"
    decision = _run_hook(
        f"python3 {canonical_script} --provider agy --profile local_asset_research "
        "--objective x --prompt y --output /tmp/out.json"
    )
    hook_output = decision["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "allow", decision
