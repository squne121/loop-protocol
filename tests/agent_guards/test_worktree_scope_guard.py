#!/usr/bin/env python3
"""test_worktree_scope_guard.py — contract tests for worktree_scope_guard (Issue #960).

Covers AC1..AC15 of WORKTREE_SCOPE_RESOLUTION_V1 / MUTATING_BASH_CLASSIFIER_V1.

Path anchoring: this test resolves the guard via __file__ (worktree-local), NOT via
`git rev-parse --show-toplevel`. Inside a linked git worktree, `git rev-parse
--show-toplevel` returns THAT worktree's own toplevel -- NOT the main repository
root (an earlier revision of this docstring incorrectly claimed the opposite; see
`test_git_rev_parse_show_toplevel_returns_linked_worktree_root` below for a
regression test of this exact distinction). Relying on `--show-toplevel` here
would silently anchor the test at whichever worktree happens to be the cwd
instead of the fixed worktree-local `scripts/agent-guards/worktree_scope_guard.py`.
(Mirrors test_secret_boundary_contract.py.)

Test harness: each test builds an isolated temporary git repo + a real
`issue-<n>-<slug>` worktree, points CLAUDE_PROJECT_DIR at the repo root and
LOOP_ISSUE_NUMBER at the active issue, then invokes the guard wrapper via subprocess
with a PreToolUse-shaped stdin payload.
"""

import json
import os
import re
import subprocess
import sys
import importlib.util as _ilu
from pathlib import Path

import pytest

from worktree_scope_guard_testkit import (
    GUARD_PY,
    GUARD_SH,
    REPO_ROOT,
    SETTINGS_JSON,
    _bash_payload,
    _git,
    _make_repo_with_worktree,
    _run_guard,
    _write_text,
)

# =============================================================================
# Harness
#
# Issue #1657 AC8: the shared harness helpers (_git, _make_repo_with_worktree,
# _run_guard, _write_text, _bash_payload, REPO_ROOT/GUARD_SH/GUARD_PY/
# SETTINGS_JSON) live in worktree_scope_guard_testkit.py (imported above) and
# are NOT redefined here. Sibling test modules import them from the same
# testkit module explicitly instead of doing a bare `from
# test_worktree_scope_guard import ...` test-to-test import.
# =============================================================================


def test_git_rev_parse_show_toplevel_returns_linked_worktree_root(tmp_path: Path) -> None:
    """AC7 (Issue #1657): `git rev-parse --show-toplevel`, executed with cwd
    inside a LINKED git worktree, returns THAT worktree's own toplevel path --
    it does NOT return the main repository root. This is the corrected claim;
    an earlier revision of this module's docstring asserted the opposite,
    which is why `worktree_scope_guard.py` deliberately anchors project_root
    resolution on `__file__` / `CLAUDE_PROJECT_DIR` instead of
    `git rev-parse --show-toplevel` (see the module docstring above)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942", slug="x")
    main_root = repo["root"]
    worktree = repo["worktree"]

    main_toplevel = _git("rev-parse", "--show-toplevel", cwd=main_root).stdout.strip()
    worktree_toplevel = _git("rev-parse", "--show-toplevel", cwd=worktree).stdout.strip()

    assert os.path.realpath(main_toplevel) == os.path.realpath(str(main_root))
    assert os.path.realpath(worktree_toplevel) == os.path.realpath(str(worktree))
    # The key regression assertion: the worktree's own show-toplevel result is
    # NOT the main repo root -- contradicting the earlier incorrect docstring
    # claim that worktree isolation makes --show-toplevel "return the main
    # repo root".
    assert os.path.realpath(worktree_toplevel) != os.path.realpath(str(main_root))


def _install_skill_runtime_exec_fixture(repo_root: Path, catalog_mode: str = "active") -> None:
    """Install a minimal registry/preflight fixture for direct executor tests."""
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        f"""from __future__ import annotations

import os


class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    issue = os.environ.get("LOOP_ISSUE_NUMBER", "").strip()
    mode = {catalog_mode!r}
    if not issue or mode == "missing":
        return []
    worktree = os.path.realpath(
        os.path.join(project_root, ".claude", "worktrees", f"issue-{{issue}}-x")
    )
    return [{{"issue_number": issue, "worktree_realpath": worktree, "worktree": worktree}}]


def select_issue_worktree(catalog, issue_number, root_realpath):
    issue = str(issue_number)
    for entry in catalog or []:
        if str(entry.get("issue_number")) == issue:
            return entry
    return None
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

import os

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv",
            "run",
            "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number",
            "{issue_number}",
            "--repo",
            "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    }
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered: list[str] = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SKILL_RUNTIME_TEST_OUTSIDE_WRITE") == "ignored":
        ignored_dir = Path(".cache")
        ignored_dir.mkdir(parents=True, exist_ok=True)
        (ignored_dir / "outside.txt").write_text("persisted")
    if os.environ.get("SKILL_RUNTIME_TEST_OUTSIDE_WRITE") == "transient":
        transient_dir = Path(".cache")
        transient_dir.mkdir(parents=True, exist_ok=True)
        transient_path = transient_dir / "transient.txt"
        transient_path.write_text("temp")
        transient_path.unlink()
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


# =============================================================================
# AC1 — block Write/Edit outside worktree when active worktree exists
# =============================================================================


def test_block_outside_worktree_write_to_repo_root(tmp_path):
    """AC1: cwd=/repo, Write to .claude/skills/** is blocked when an active
    issue worktree exists."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["root"] / ".claude" / "skills" / "x" / "SKILL.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_block_outside_worktree_edit_main_file(tmp_path):
    """AC1: Edit to a file under repo root (outside worktree) is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["root"] / "README.md"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


# =============================================================================
# AC2 — allow Write/Edit inside the expected worktree
# =============================================================================


def test_allow_inside_worktree_write(tmp_path):
    """AC2: cwd inside the issue worktree, Write inside it is allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["worktree"] / "src" / "file.ts"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


def test_allow_inside_worktree_multiedit(tmp_path):
    """AC2: MultiEdit inside the expected worktree is allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["worktree"] / "a.txt"
    payload = {
        "tool_name": "MultiEdit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


# =============================================================================
# AC3 / AC8 — block mutating Bash outside worktree
# =============================================================================


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m wip",
        "git add .",
        "git push origin HEAD",
        "git checkout main",
        "git switch main",
        "git reset --hard",
        "git rebase main",
        "git merge feature",
        "git stash",
        "git worktree add foo",
        "git tag v1",
        "gh pr create --fill",
        "gh pr edit 1 --add-label x",
    ],
)
def test_block_mutating_bash_from_repo_root(tmp_path, command):
    """AC3/AC8: mutating Bash run from repo root (outside worktree) is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{command!r} should block; stderr={r.stderr}"


@pytest.mark.parametrize(
    "command",
    [
        "echo hi > out.txt",
        "echo hi >> out.txt",
        "sed -i 's/a/b/' file.txt",
        "cat foo | tee out.txt",
        "npm install",
        "pnpm add left-pad",
        "yarn remove x",
        "bun add y",
    ],
)
def test_block_mutating_bash_classifier_minimal_set(tmp_path, command):
    """AC8: shell file-write + package-manager mutation classified as mutating
    and blocked outside worktree."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{command!r} should block; stderr={r.stderr}"


# =============================================================================
# AC4 — allow read-only Bash
# =============================================================================


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff HEAD~1",
        "git log --oneline",
        "git show HEAD",
        "git rev-parse HEAD",
        "git worktree list",
        "git stash list",
        "git stash show",
        "gh pr view 1",
        "gh issue view 1",
        "gh api repos/o/r/pulls",
        "gh api -X GET repos/o/r",
    ],
)
def test_allow_read_only_bash(tmp_path, command):
    """AC4: read-only Bash is allowed even from repo root."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, f"{command!r} should allow; stderr={r.stderr}"


def test_allow_read_only_bash_when_worktree_unresolved(tmp_path):
    """AC4: read-only Bash is allowed even when no active issue worktree resolves."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": str(repo["root"]),
    }
    # No LOOP_ISSUE_NUMBER, cwd basename not issue-* → unresolved → read-only allow.
    r = _run_guard(payload, repo["root"], issue=None)
    assert r.returncode == 0, r.stderr


# =============================================================================
# AC5 / AC13 — bounded failure message, no leak
# =============================================================================


def test_message_bounded_max_lines(tmp_path):
    """AC5: block stderr is <= 20 lines and shows only expected worktree + cwd."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["root"] / "README.md"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) <= 20, f"stderr too long: {lines}"
    assert any("expected_worktree" in ln for ln in lines)
    assert any("actual_cwd" in ln for ln in lines)


def test_message_bounded_shows_relative_worktree(tmp_path):
    """AC5: expected worktree is shown project-relative, not a full path dump."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["root"] / "README.md"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2
    assert ".claude/worktrees/issue-942-x" in r.stderr


def test_stderr_bounded_no_leak(tmp_path):
    """AC13: block stderr must not contain tool command, tool input path,
    worktree list, or env values."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    secret_pathseg = "TOPSECRETSEG_xyz987"
    target = repo["root"] / secret_pathseg / "f.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942", extra_env={"LEAKY_ENV_VAR": "LEAKVAL_abc"})
    assert r.returncode == 2
    # tool input path must not leak
    assert secret_pathseg not in r.stderr, r.stderr
    # env values must not leak
    assert "LEAKVAL_abc" not in r.stderr, r.stderr
    # worktree list internals (HEAD sha / branch refs) must not leak
    assert "refs/heads" not in r.stderr, r.stderr


def test_stderr_bounded_no_leak_for_bash(tmp_path):
    """AC13: block stderr for mutating Bash must not echo the command."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m SECRETMSG_qqq"},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2
    assert "SECRETMSG_qqq" not in r.stderr, r.stderr


# =============================================================================
# AC6 — guard ordering (secret guard precedes worktree guard in settings)
# =============================================================================


def test_guard_ordering_secret_precedes_worktree(tmp_path):
    """AC6（#1690 で一時変更）: worktree_scope_guard は #1690 の方針決定までの間
    settings.json の PreToolUse から一時的に外されている。secret_boundary_guard
    の配線は維持されていることのみ検証する。#1690 の結論で worktree_scope_guard
    が復元された場合、本テストの ordering assertion も復元すること。"""
    settings = json.loads(SETTINGS_JSON.read_text())
    pre = settings["hooks"]["PreToolUse"]

    def _index_of(name):
        for i, entry in enumerate(pre):
            for h in entry.get("hooks", []):
                if name in h.get("command", ""):
                    return i
        return -1

    secret_idx = _index_of("secret_boundary_guard")
    worktree_idx = _index_of("worktree_scope_guard")
    assert secret_idx != -1, "secret_boundary_guard missing from PreToolUse"
    assert worktree_idx == -1, (
        "worktree_scope_guard は #1690 の方針決定までの間 PreToolUse から外れている想定"
    )
    assert GUARD_PY.exists(), "worktree_scope_guard.py 本体は削除されていない想定"


def test_guard_ordering_worktree_matcher_shape(tmp_path):
    """AC6（#1690 で一時変更）: worktree_scope_guard は現在 settings.json に
    配線されていないため matcher shape は検証しない。secret guard に
    MultiEdit が含まれること（#970）のみ検証する。#1690 の結論で
    worktree_scope_guard が復元された場合、matcher shape assertion も復元すること。"""
    settings = json.loads(SETTINGS_JSON.read_text())
    pre = settings["hooks"]["PreToolUse"]

    worktree_matcher = None
    secret_matcher = None
    for entry in pre:
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if "worktree_scope_guard" in cmd:
                worktree_matcher = entry.get("matcher", "")
            if "secret_boundary_guard" in cmd:
                secret_matcher = entry.get("matcher", "")

    assert worktree_matcher is None, (
        "worktree_scope_guard は #1690 の方針決定までの間 PreToolUse から外れている想定"
    )
    # #970 にて secret_boundary_guard にも MultiEdit を追加済み。
    assert secret_matcher is not None
    assert "MultiEdit" in secret_matcher, "secret_boundary_guard matcher must include MultiEdit (#970)"


# =============================================================================
# AC8 — classifier minimal set (read-only allowlist vs mutating)
# =============================================================================


def test_classifier_minimal_set_readonly_vs_mutating(tmp_path):
    """AC8: importable classifier returns read_only / mutating for the minimal set."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wsg", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.classify_bash("git status") == "read_only"
    assert mod.classify_bash("git commit -m x") == "mutating"
    assert mod.classify_bash("git push") == "mutating"
    assert mod.classify_bash("git worktree list") == "read_only"
    assert mod.classify_bash("git worktree add foo") == "mutating"
    assert mod.classify_bash("git stash list") == "read_only"
    assert mod.classify_bash("git stash") == "mutating"
    assert mod.classify_bash("gh pr view 1") == "read_only"
    assert mod.classify_bash("gh pr merge 1") == "mutating"
    assert mod.classify_bash("gh issue view 1") == "read_only"
    assert mod.classify_bash("gh issue comment 1 -b x") == "mutating"
    assert mod.classify_bash("npm install") == "mutating"
    assert mod.classify_bash("pnpm add x") == "mutating"
    assert mod.classify_bash("echo x > f") == "mutating"
    assert mod.classify_bash("sed -i s/a/b/ f") == "mutating"


# =============================================================================
# AC9 — wrapper / explicit-target detection
# =============================================================================


@pytest.mark.parametrize(
    "command",
    [
        "git -C {root} commit -m oops",
        "cd {root} && git add .",
        "command git -C {root} push",
        "env FOO=bar git -C {root} commit -m x",
    ],
)
def test_block_wrapper_explicit_target_outside(tmp_path, command):
    """AC9: even with cwd inside the worktree, an explicit target dir outside it
    (via git -C / cd && / command git / env ... git) is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),  # cwd is INSIDE the worktree
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{cmd!r} should block; stderr={r.stderr}"


def test_allow_wrapper_explicit_target_inside(tmp_path):
    """AC9: git -C pointing inside the worktree (from repo-root cwd) is allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"git -C {repo['worktree']} status"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    # read-only git status is allowed regardless.
    assert r.returncode == 0, r.stderr


# =============================================================================
# AC10 — gh api write methods / field flags + gh mutations
# =============================================================================


@pytest.mark.parametrize(
    "command",
    [
        "gh api -X PATCH repos/o/r/issues/1",
        "gh api --method POST repos/o/r/issues",
        "gh api -f title=x repos/o/r/issues",
        "gh api -F body=@b.md repos/o/r/issues",
        "gh api --field a=b repos/o/r",
        "gh api --input payload.json repos/o/r",
        "gh issue comment 1 -b hi",
        "gh pr merge 1",
        "gh pr review 1 --approve",
        "gh pr comment 1 -b hi",
    ],
)
def test_block_gh_api_and_mutations(tmp_path, command):
    """AC10: gh api writes and gh pr/issue mutations are blocked outside worktree."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{command!r} should block; stderr={r.stderr}"


def test_allow_gh_api_explicit_get(tmp_path):
    """AC10: gh api -X GET (no field flags) is read-only and allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api -X GET repos/o/r/pulls"},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


# =============================================================================
# AC11 — path containment via realpath/commonpath
# =============================================================================


def test_path_containment_realpath_dotdot_traversal(tmp_path):
    """AC11: a target using ../ to escape the worktree is blocked."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # target lexically inside but escapes via ..
    target = str(repo["worktree"]) + "/../../../README.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": target},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_path_containment_realpath_symlink_outside(tmp_path):
    """AC11: a symlink inside the worktree pointing outside is blocked
    (commonpath on realpath, not startswith)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    outside_dir = repo["root"] / ".claude" / "skills"
    outside_dir.mkdir(parents=True, exist_ok=True)
    link = repo["worktree"] / "out"
    os.symlink(str(outside_dir), str(link))
    target = str(link / "SKILL.md")  # realpath escapes worktree
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": target},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_path_containment_realpath_prefix_sibling_not_confused(tmp_path):
    """AC11: a sibling dir sharing a string prefix with the worktree must not be
    treated as inside (startswith would wrongly allow)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # sibling: issue-942-x-evil (string prefix of worktree path component differs)
    sibling = repo["root"] / ".claude" / "worktrees" / "issue-942-x-evil"
    sibling.mkdir(parents=True, exist_ok=True)
    target = str(sibling / "f.md")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": target},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_path_containment_realpath_relative_inside_allowed(tmp_path):
    """AC11: relative target resolved against an inside cwd stays inside → allow."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": "src/new.ts"},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


# =============================================================================
# AC12 — worktree ambiguity decision (0 / multiple / mismatch)
# =============================================================================


def test_worktree_ambiguity_decision_zero_match_mutation_blocks(tmp_path):
    """AC12: active issue resolved but NO matching worktree → mutation blocks."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # active issue 777 has no worktree
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m x"},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="777")
    assert r.returncode == 2, r.stderr


def test_worktree_ambiguity_decision_zero_match_readonly_allows(tmp_path):
    """AC12: 0 matching worktree but read-only command → allow."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="777")
    assert r.returncode == 0, r.stderr


def test_worktree_ambiguity_decision_multiple_match_blocks(tmp_path):
    """AC12: multiple worktrees matching the issue → mutation fail-closed block."""
    repo = _make_repo_with_worktree(tmp_path, issue="942", extra_worktrees=[("942", "dup")])
    target = repo["root"] / "README.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


# =============================================================================
# AC14 — porcelain -z NUL record parser
# =============================================================================


def test_porcelain_z_parser_handles_nul_records(tmp_path):
    """AC14: parser splits NUL-separated records and extracts worktree + branch."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wsg", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fixture = (
        "worktree /repo\0HEAD abc123\0branch refs/heads/main\0\0"
        "worktree /repo/.claude/worktrees/issue-942-x\0HEAD def456\0"
        "branch refs/heads/issue-942-x\0\0"
        "worktree /repo/detached-wt\0HEAD 000111\0detached\0\0"
    )
    parsed = mod.parse_worktree_porcelain_z(fixture)
    assert len(parsed) == 3, parsed
    assert parsed[0]["worktree"] == "/repo"
    assert parsed[0]["branch"] == "refs/heads/main"
    assert parsed[1]["worktree"] == "/repo/.claude/worktrees/issue-942-x"
    assert parsed[1]["branch"] == "refs/heads/issue-942-x"
    assert parsed[2]["worktree"] == "/repo/detached-wt"
    assert parsed[2].get("detached") is True
    assert "branch" not in parsed[2]


def test_porcelain_z_parser_handles_path_with_newline(tmp_path):
    """AC14: -z form tolerates a worktree path that contains a newline."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wsg", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fixture = "worktree /repo/odd\nname\0HEAD aaa\0branch refs/heads/main\0\0"
    parsed = mod.parse_worktree_porcelain_z(fixture)
    assert len(parsed) == 1
    assert parsed[0]["worktree"] == "/repo/odd\nname"


# =============================================================================
# AC15 — fail-closed: malformed payload / git missing / unparseable mutation
# =============================================================================


def test_fail_closed_malformed_or_missing_payload(tmp_path):
    """AC15: malformed (non-JSON) stdin for a matched tool → block exit 2."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    env["LOOP_ISSUE_NUMBER"] = "942"
    r = subprocess.run(
        ["bash", str(GUARD_SH)],
        input="this is not json",
        text=True,
        capture_output=True,
        env=env,
    )
    assert r.returncode == 2, r.stderr


def test_fail_closed_malformed_or_missing_tool_name(tmp_path):
    """AC15: payload missing tool_name → block exit 2."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {"tool_input": {"command": "git commit"}, "cwd": str(repo["root"])}
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_fail_closed_malformed_or_missing_unparseable_mutation(tmp_path):
    """AC15: an unknown/unparseable command that may mutate, from outside the
    worktree, is blocked (fail-closed)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "some_unknown_tool --do-things 'unbalanced"},
        "cwd": str(repo["root"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_fail_closed_git_binary_unavailable_for_mutation(tmp_path):
    """AC15: git binary unavailable while an active issue is resolved and a
    mutation is attempted → fail-closed block."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # PATH stripped of git so shutil.which('git') returns None inside the guard.
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    env["LOOP_ISSUE_NUMBER"] = "942"
    # provide a python3-only PATH dir
    py_dir = tmp_path / "pybin"
    py_dir.mkdir()
    import shutil as _sh

    real_py = _sh.which("python3")
    real_bash = _sh.which("bash")
    os.symlink(real_py, str(py_dir / "python3"))
    os.symlink(real_bash, str(py_dir / "bash"))
    env["PATH"] = str(py_dir)
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m x"},
        "cwd": str(repo["root"]),
    }
    r = subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )
    assert r.returncode == 2, r.stderr


# =============================================================================
# settings wiring sanity (supports AC7)
# =============================================================================


def test_settings_json_is_valid_and_wires_worktree_guard(tmp_path):
    """settings.json parses（#1690 で一時変更: worktree_scope_guard は方針決定
    までの間 PreToolUse から外れている想定。settings.json 自体の valid性のみ
    検証する）。#1690 の結論で復元された場合、wiring assertion を復元すること。"""
    settings = json.loads(SETTINGS_JSON.read_text())
    pre = settings["hooks"]["PreToolUse"]
    found = any("worktree_scope_guard" in h.get("command", "") for entry in pre for h in entry.get("hooks", []))
    assert not found, "worktree_scope_guard は #1690 の方針決定までの間 PreToolUse から外れている想定"


# =============================================================================
# Regression: fail-open closure (Issue #960 re-review) — B1 / B2 / B3 / Major
# =============================================================================

# ── B1 [AC12] Write/Edit/MultiEdit zero-match worktree → fail-closed block ────


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_b1_write_zero_match_external_blocks(tmp_path, tool):
    """B1/AC12: issue resolved (LOOP_ISSUE_NUMBER) but NO matching worktree →
    Write/Edit/MultiEdit to an external path is fail-closed blocked (symmetric
    with the mutating-Bash zero-match block)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    target = repo["root"] / "README.md"  # external, but worktree for 942 exists
    payload = {
        "tool_name": tool,
        "tool_input": {"file_path": str(target)},
        "cwd": str(repo["root"]),
    }
    # active issue 777 has no matching worktree → zero-match → block.
    r = _run_guard(payload, repo["root"], issue="777")
    assert r.returncode == 2, f"{tool} zero-match should block; stderr={r.stderr}"


# ── B2 [AC8] shell file-write external target from inside worktree → block ────


@pytest.mark.parametrize(
    "command_tmpl",
    [
        "echo x > {root}/evil.txt",
        "echo x >> {root}/evil.txt",
        "cat foo | tee {root}/evil.txt",
        "sed -i 's/a/b/' {root}/README.md",
        "perl -i -pe 's/a/b/' {root}/README.md",
        "python3 -c \"open('{root}/evil.txt','w').write('x')\"",
        "node -e \"require('fs').writeFileSync('{root}/evil.txt','x')\"",
        "ruby -e \"File.write('{root}/evil.txt','x')\"",
    ],
)
def test_b2_shell_write_external_target_from_worktree_blocks(tmp_path, command_tmpl):
    """B2/AC8/Outcome: cwd inside the worktree, but the file-write destination is an
    external absolute path → block (write-target extracted + containment-checked)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),  # cwd INSIDE the worktree
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{cmd!r} should block; stderr={r.stderr}"


def test_b2_shell_write_internal_target_allowed(tmp_path):
    """B2 control: file-write to a path inside the worktree is still allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"echo x > {repo['worktree']}/inside.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


def test_b2_shell_write_unextractable_target_fail_closed(tmp_path):
    """B2: a file-write mutation whose destination cannot be extracted is
    fail-closed (block) while a worktree exists."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # `>` redirection with the destination produced by command substitution —
    # the literal path is not statically extractable.
    cmd = "echo x > $(some_helper --print-path)"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


# ── B3 [AC9] bash -c / sh -c / env ... bash -lc wrapper unwrap → block ────────


@pytest.mark.parametrize(
    "command_tmpl",
    [
        "bash -lc 'cd {root} && git add .'",
        "sh -c 'echo x > {root}/y'",
        "env FOO=bar bash -lc 'git -C {root} commit -m x'",
        "bash -c 'cd {root} && git commit -m x'",
        "zsh -c 'echo x > {root}/z'",
    ],
)
def test_b3_shell_wrapper_external_target_blocks(tmp_path, command_tmpl):
    """B3/AC9: cwd inside the worktree, but a `bash/sh/zsh -c|-lc` wrapper script
    targets an external dir (cd / git -C / redirection) → unwrap and block."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),  # cwd INSIDE the worktree
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{cmd!r} should block; stderr={r.stderr}"


def test_b3_shell_wrapper_internal_target_allowed(tmp_path):
    """B3 control: a bash -lc wrapper whose cd/git -C stays inside the worktree
    is allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"bash -lc 'cd {repo['worktree']} && git add .'"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


# ── Major [AC15] unknown/mutating command with external absolute-path arg ─────


@pytest.mark.parametrize(
    "command_tmpl",
    [
        "someformatter --output {root}/out.txt",
        "somegen --output={root}/out.txt",
        "somelint --fix {root}/x.py",
        "somefmt -o {root}/out.txt",
        "rewrite --in-place {root}/x.txt",
    ],
)
def test_major_unknown_external_abs_arg_blocks(tmp_path, command_tmpl):
    """Major/AC15: an unknown command run from inside the worktree carrying a
    WRITE-OPTION absolute-path value pointing outside the worktree (formatter-write
    risk) is fail-closed blocked. A bare positional external abs path is a read
    source and is NOT blocked here (see over-block regression tests below)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),  # cwd INSIDE the worktree
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{cmd!r} should block; stderr={r.stderr}"


def test_major_unknown_internal_abs_arg_allowed(tmp_path):
    """Major control: unknown command with an absolute-path arg INSIDE the
    worktree (cwd inside) is allowed (mutation contained)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"someformatter --output {repo['worktree']}/out.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


def test_major_readonly_allowlist_not_overblocked(tmp_path):
    """Major control: read-only allowlist commands with no external abs path stay
    allowed (no over-block from the stricter unknown-inside path)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    for cmd in ("git status", "git diff HEAD~1", "gh pr view 1", "gh api -X GET repos/o/r"):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(repo["worktree"]),
        }
        r = _run_guard(payload, repo["root"], issue="942")
        assert r.returncode == 0, f"{cmd!r} should allow; stderr={r.stderr}"


# =============================================================================
# Over-block regression [AC4/AC8/AC15]: read-only commands reading files OUTSIDE
# the worktree must be ALLOWED. main pollution prevention targets mutation only;
# reading an external file does not pollute main. The fix narrows the Major
# unknown-external-abs-path block to write-option destinations only and adds a
# general read-only program allowlist.
# =============================================================================


@pytest.mark.parametrize(
    "command_tmpl",
    [
        # bare read-only programs reading an external absolute path → allow
        "cat {root}/README.md",
        "grep x {root}/README.md",
        "ls {root}",
        "head {root}/README.md",
        "tail {root}/README.md",
        "wc -l {root}/README.md",
        "find {root} -name x",
        "stat {root}/README.md",
        "realpath {root}/README.md",
        # pipeline of read-only programs reading external → allow
        "cat {root}/README.md | grep x",
        "grep x {root}/README.md | wc -l",
        # sed/perl WITHOUT in-place are read-only even reading external → allow
        "sed -n '1p' {root}/README.md",
        # read-only git against external dir → allow
        "git -C {root} status",
    ],
)
def test_readonly_external_abs_path_not_overblocked(tmp_path, command_tmpl):
    """AC4 over-block regression: read-only ALLOWLIST commands reading OUTSIDE the
    worktree from a cwd inside the worktree are allowed — reads do not pollute
    main. A bare external abs path to an UNKNOWN (non-allowlist) program is instead
    fail-closed blocked (cp/mv/dd write positionally and are indistinguishable from
    a read at parse time); see test_block_mutating_bash_positional_writer_outside."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),  # cwd INSIDE the worktree
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, f"{cmd!r} should allow; stderr={r.stderr}"


@pytest.mark.parametrize(
    "command_tmpl",
    [
        # redirection to an external path from an otherwise read-only program → block
        "cat {root}/README.md > {root}/evil.txt",
        "echo x > {root}/evil.txt",
        "echo x >> {root}/evil.txt",
        "grep x in.txt > {root}/evil.txt",
        # tee to an external path → block
        "cat foo | tee {root}/evil.txt",
        # sed/perl in-place on an external file → block
        "sed -i 's/a/b/' {root}/README.md",
        "perl -i -pe 's/a/b/' {root}/README.md",
        # write-option destination to an external path → block
        "someformatter --output {root}/out.txt",
        "somefmt -o {root}/out.txt",
    ],
)
def test_readonly_program_external_redirection_still_blocks(tmp_path, command_tmpl):
    """AC8 maintain: redirection / tee / in-place / write-option destinations to an
    EXTERNAL absolute path are still blocked even when the leading program is
    read-only. Redirection write-target checks take priority over the program
    allowlist."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, f"{cmd!r} should block; stderr={r.stderr}"


def test_readonly_program_internal_redirection_allowed(tmp_path):
    """Control: a read-only program redirecting to a path INSIDE the worktree is
    allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"cat {repo['root']}/README.md > {repo['worktree']}/copy.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


def test_readonly_relative_internal_write_and_build_allowed(tmp_path):
    """Control: relative write inside the worktree and `npm run build` are allowed
    (no over-block regression for ordinary in-worktree work)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    for cmd in ("echo x > inside.txt", "npm run build"):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(repo["worktree"]),
        }
        r = _run_guard(payload, repo["root"], issue="942")
        assert r.returncode == 0, f"{cmd!r} should allow; stderr={r.stderr}"


@pytest.mark.parametrize(
    "command_tmpl",
    [
        "cp a.txt {root}/copied.txt",
        "mv a.txt {root}/moved.txt",
        "dd if=a.txt of={root}/dd.out",
        "install a.txt {root}/inst.txt",
        "cp -r . {root}/dircopy",
        # unknown program with a bare external abs path: cannot prove read vs write
        # at parse time → fail-closed (would otherwise re-open cp/mv/dd positional writes)
        "weirdtool {root}/target.bin",
    ],
)
def test_block_mutating_bash_positional_writer_outside(tmp_path, command_tmpl):
    """AC8/AC15: an unknown positional-writer (cp/mv/dd/install) whose external
    absolute destination is a bare positional (or of=) arg must be fail-closed
    blocked from inside the worktree. Read-only commands reading an external abs
    path (cat/grep/ls) are still allowed via the read-only allowlist; only
    non-read-only (unknown) programs are blocked on any external abs path arg."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = command_tmpl.format(root=str(repo["root"]))
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 2, r.stderr


def test_allow_read_only_external_read_with_relative_inside_write(tmp_path):
    """Control: `cat ROOT/f > inside` reads an external abs path but writes the
    redirection target INSIDE the worktree → allowed (not over-blocked)."""
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    cmd = f"cat {repo['root']}/README.md > inside.txt"
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")
    assert r.returncode == 0, r.stderr


def test_given_verified_no_issue_when_claude_write_runs_then_deny_while_codex_compatibility_is_separate(tmp_path):
    """Issue #1670 compatibility decision: Claude Write/Edit stay deny-on-no-
    Issue, while Codex canonical apply_patch's established no-Issue allow is
    covered by its dedicated adapter contract test."""
    repo = _make_repo_with_worktree(tmp_path, issue="1670", slug="policy")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo["root"] / "outside.txt")},
        "cwd": str(repo["root"]),
    }
    result = _run_guard(payload, repo["root"], issue=None)
    assert result.returncode == 2, result.stderr


def test_given_empty_write_target_when_active_worktree_then_fail_closed(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1670", slug="policy")
    payload = {"tool_name": "Write", "tool_input": {}, "cwd": str(repo["worktree"])}
    result = _run_guard(payload, repo["root"], issue="1670")
    assert result.returncode == 2, result.stderr


# =============================================================================
# Issue #1050: WORKTREE_SCOPE_DECISION_V2 / cleanup classification tests
# =============================================================================

# --- AC2: git show HEAD:path inside worktree → allow ---


def test_allow_git_show_inside_worktree(tmp_path):
    """AC2: git show HEAD:path inside active worktree is read_only → allow."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git show HEAD:README.md"},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="1050")
    assert r.returncode == 0, f"git show should allow; stderr={r.stderr}"


# --- AC3: unrelated mutation inside worktree → deny (command_class: mutation) ---


def test_deny_unrelated_mutation_via_build_decision(tmp_path):
    """AC3: build_decision returns command_class==mutation and deny for mutation outside worktree."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("worktree_scope_guard", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    # Write to a path OUTSIDE the worktree → should deny
    target = str(repo["root"] / "outside.txt")
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": target},
        "cwd": str(repo["worktree"]),
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
    }
    import os

    old_env = os.environ.copy()
    os.environ["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    os.environ["LOOP_ISSUE_NUMBER"] = "1050"
    try:
        d = mod.build_decision(payload)
        assert d["schema"] == "WORKTREE_SCOPE_DECISION_V2"
        assert "mutation" in d["command_class"]
        assert d["decision"] == "deny"
    finally:
        os.environ.clear()
        os.environ.update(old_env)


# --- AC4a: git worktree remove without contract → deny ---


def test_cleanup_worktree_remove_no_contract(tmp_path):
    """AC4a: git worktree remove without cleanup contract is denied."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"should deny without contract; stderr={r.stderr}"


# --- AC4b: git worktree remove with valid contract + exact path → allow ---


def test_cleanup_worktree_remove_with_contract(tmp_path):
    """AC4b: git worktree remove with valid contract and exact path → allow."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": "issue-1050-scope-guard",
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 0, f"should allow with valid contract; stderr={r.stderr}"


# --- AC4c: git branch -d with valid contract → allow ---


def test_cleanup_branch_delete_with_contract(tmp_path):
    """AC4c: git branch -d <branch> with valid contract → allow."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    branch = "issue-1050-scope-guard"
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": branch,
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git branch -d {branch}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 0, f"should allow branch -d with valid contract; stderr={r.stderr}"


# --- AC4c: git branch -D (force delete) → deny at guard decision level ---


def test_cleanup_branch_force_delete_denied(tmp_path):
    """AC4c: git branch -D with contract present must deny at guard decision level.

    The guard must deny -D even when cwd is inside the active worktree.
    Classifier returns mutating (not cleanup); _is_force_branch_delete catches it.
    """
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    branch = "issue-1050-scope-guard"
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": branch,
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git branch -D {branch}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    # Must deny via guard decision (AC4c: -D denied regardless of cwd)
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"git branch -D must deny; returncode={r.returncode} stderr={r.stderr}"


def test_cleanup_branch_delete_force_long_form_denied(tmp_path):
    """AC4c: git branch --delete --force <branch> must deny at guard decision level."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    branch = "issue-1050-scope-guard"
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": branch,
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git branch --delete --force {branch}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"branch --delete --force must deny; stderr={r.stderr}"


# --- AC4d: path traversal in worktree remove → deny ---


def test_cleanup_worktree_remove_path_traversal_denied(tmp_path):
    """AC4d: path traversal in git worktree remove → deny."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": "issue-1050-scope-guard",
        "require_clean": True,
    }
    # Provide a different path (sibling directory)
    sibling = str(tmp_path / "other-dir")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {sibling}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"sibling path should deny; stderr={r.stderr}"


# --- AC5: compound shell command → command_class: unknown ---


def test_unknown_compound_shell_command(tmp_path):
    """AC5: cd /repo && git status is compound → classify_bash returns unknown."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("worktree_scope_guard", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cmd = "cd /repo && git status"
    klass = mod.classify_bash(cmd)
    assert klass == "unknown", f"compound shell command must be unknown; got {klass!r}"


# --- AC6: deny stderr bounded, no raw data, exit 2 ---


def test_deny_stderr_exit2_bounded(tmp_path):
    """AC6: cleanup deny → stderr ≤10 lines, no raw path/branch, exit 2."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    # No contract → should deny
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, "cleanup deny must exit 2"
    stderr_lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(stderr_lines) <= 10, f"stderr must be ≤10 lines; got {len(stderr_lines)}"
    # No raw path/branch/command in stderr
    assert wt_path not in r.stderr, "stderr must not contain raw worktree path"
    assert "git worktree remove" not in r.stderr, "stderr must not contain raw command"


# --- AC9: build_decision importable pure function ---


def test_build_decision_importable(tmp_path):
    """AC9: build_decision is importable from worktree_scope_guard and returns V2 dict."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("worktree_scope_guard", str(GUARD_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "build_decision"), "build_decision must be importable"
    assert callable(mod.build_decision)

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    import os

    old_env = os.environ.copy()
    os.environ["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    os.environ["LOOP_ISSUE_NUMBER"] = "1050"
    try:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "git show HEAD:README.md"},
            "cwd": str(repo["worktree"]),
        }
        d = mod.build_decision(payload)
        assert d["schema"] == "WORKTREE_SCOPE_DECISION_V2"
        assert d["command_class"] == "read_only"
        assert d["decision"] == "allow"
        assert "cwd_class" in d
        assert "reason" in d
    finally:
        os.environ.clear()
        os.environ.update(old_env)


# --- AC4b/AC10: require_clean enforcement ---


def test_cleanup_require_clean_false_contract_invalid(tmp_path):
    """AC4b/AC10: contract with require_clean=false is invalid → deny."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": "issue-1050-scope-guard",
        "require_clean": False,  # must be exactly True
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    # require_clean: False makes the contract invalid → no contract → deny
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"require_clean:false must deny; stderr={r.stderr}"


def test_cleanup_worktree_remove_dirty_unstaged_denied(tmp_path):
    """AC4b/AC10: git worktree remove denied when worktree has unstaged modification."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = repo["worktree"]
    # Create a dirty file in the worktree
    (wt_path / "dirty.txt").write_text("unstaged\n")
    # Stage then unstage to create a tracked unstaged modification
    _git("add", "dirty.txt", cwd=wt_path)
    _git("commit", "-m", "add dirty", cwd=wt_path)
    (wt_path / "dirty.txt").write_text("modified\n")
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": str(wt_path),
        "branch_name": "issue-1050-scope-guard",
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(wt_path),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"dirty worktree must deny; stderr={r.stderr}"


def test_cleanup_worktree_remove_dirty_untracked_denied(tmp_path):
    """AC4b/AC10: git worktree remove denied when worktree has untracked file."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = repo["worktree"]
    # Create an untracked file
    (wt_path / "untracked.txt").write_text("untracked\n")
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": str(wt_path),
        "branch_name": "issue-1050-scope-guard",
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {wt_path}"},
        "cwd": str(wt_path),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"untracked file must deny; stderr={r.stderr}"


def test_cleanup_worktree_remove_outside_worktrees_dir_denied(tmp_path):
    """AC4d (Blocker 3): worktree_path outside .claude/worktrees/ is denied."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    # Use a path that's NOT under .claude/worktrees/ (project root itself)
    bad_path = str(repo["root"])
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": bad_path,
        "branch_name": "main",
        "require_clean": True,
    }
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git worktree remove {bad_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    # bad_path is the project root, not under .claude/worktrees/
    # → invalid contract (not absolute check for worktree_path outside worktrees dir)
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"path outside .claude/worktrees/ must deny; stderr={r.stderr}"


def test_cleanup_git_dash_c_worktree_remove_denied(tmp_path):
    """High: git -C <path> worktree remove is denied (grammar not allowed in cleanup)."""
    import json

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="scope-guard")
    wt_path = str(repo["worktree"])
    project_root = str(repo["root"])
    contract = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_path,
        "branch_name": "issue-1050-scope-guard",
        "require_clean": True,
    }
    # git -C <root> worktree remove <path>: classify=cleanup but decider denies
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"git -C {project_root} worktree remove {wt_path}"},
        "cwd": str(repo["worktree"]),
    }
    env = {
        "CLAUDE_PROJECT_DIR": project_root,
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(contract),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, f"git -C worktree remove must deny; stderr={r.stderr}"


# =============================================================================
# Issue #1137: real-hook-chain integration — agent-ops allow + one-shot V3
# =============================================================================

import sys as _sys  # noqa: E402

_AGENT_OPS = REPO_ROOT / "scripts" / "agent-ops"
if str(_AGENT_OPS) not in _sys.path:
    _sys.path.insert(0, str(_AGENT_OPS))

import cleanup_contract_v3 as _cc3  # noqa: E402


def _write_v3(
    root, wt_real, branch, operation, *, expired=False, bad_hash=False, bad_op=False, corrupt=False, nonce="0" * 32
):
    """Write a V3 contract directly for precise test control."""
    import time as _t

    now = int(_t.time())
    if expired:
        issued = now - 1000
        expires = now - 700  # ttl=300 (in-bounds) but already past
    else:
        issued = now
        expires = now + 300

    def _iso(ts):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    op = operation
    chash = _cc3.canonical_command_hash(
        _cc3.expected_argv(operation, wt_real, branch), operation, os.path.realpath(str(root)), nonce
    )
    if bad_hash:
        chash = "0" * 64
    if bad_op:
        op = _cc3.OP_BRANCH_DELETE if operation == _cc3.OP_WORKTREE_REMOVE else _cc3.OP_WORKTREE_REMOVE
    contract = {
        "schema": _cc3.SCHEMA_V3,
        "pr_number": 1,
        "linked_issue_number": 1137,
        "worktree_path": wt_real,
        "branch_name": branch,
        "require_clean": True,
        "operation": op,
        "command_hash": chash,
        "nonce": nonce,
        "issued_at": _iso(issued),
        "expires_at": _iso(expires),
    }
    target = root / "artifacts" / "agent-ops" / "cleanup_contract.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        target.write_text("{ not json")
    else:
        target.write_text(json.dumps(contract))
    os.chmod(target, 0o600)  # reader requires mode 0600 (materialize writes 0600)
    return contract


# --- AC3: exact agent-ops tool allowed from main root with active issue ---


@pytest.mark.parametrize(
    ("script", "extra_args"),
    [
        # cleanup_exec.py and materialize require --pr-number/--worktree-path/--branch-name
        # (B2: required flags are now enforced at guard level, not just argparse)
        (
            "scripts/agent-ops/cleanup_exec.py",
            "--json --pr-number 1 --worktree-path /tmp/wt --branch-name test-branch",
        ),
        ("scripts/agent-ops/guard_preflight.py", "--json"),
        (
            "scripts/agent-ops/materialize_cleanup_contract.py",
            "--json --pr-number 1 --worktree-path /tmp/wt --branch-name test-branch",
        ),
    ],
)
def test_agent_ops_tool_allowed_real_hook(tmp_path, script, extra_args):
    """AC3 (real hook): uv run python3 <agent-ops tool> from main root + active issue → allow."""
    repo = _make_repo_with_worktree(tmp_path, issue="1137", slug="x")
    cmd = f"uv run python3 {script} {extra_args}"
    payload = _bash_payload(cmd, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1137"}
    r = _run_guard(payload, repo["root"], issue="1137", extra_env=env)
    assert r.returncode == 0, f"agent-ops tool must allow; stderr={r.stderr}"


@pytest.mark.parametrize(
    "command",
    [
        'python3 -c "import os"',
        "uv run python3 scripts/agent-ops/cleanup_exec.py --json; rm -rf /",
        "uv run python3 scripts/other/evil.py",
        "uv run python3 scripts/agent-ops/not_allowed.py",
    ],
)
def test_agent_ops_tool_rejected_forms_real_hook(tmp_path, command):
    """AC3: python -c / shell-chain / non-allowed scripts are NOT agent-ops-allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="1137", slug="x")
    payload = _bash_payload(command, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1137"}
    r = _run_guard(payload, repo["root"], issue="1137", extra_env=env)
    assert r.returncode == 2, f"unsafe form must not be agent-ops-allowed; stderr={r.stderr}"


def test_agent_ops_tool_rejected_from_non_root(tmp_path):
    """AC3: agent-ops tool from a non-root cwd is not allowed by the exact class."""
    repo = _make_repo_with_worktree(tmp_path, issue="1137", slug="x")
    payload = _bash_payload("uv run python3 scripts/agent-ops/cleanup_exec.py --json", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1137"}
    # cwd=worktree, not main root → not agent-ops-allowed; classified unknown → allowed
    # only if inside worktree. Here cwd IS the worktree so unknown-inside is allowed,
    # so assert it is NOT allowed via the agent_ops path by checking decision reason.
    r = _run_guard(payload, repo["root"], issue="1137", extra_env=env)
    # inside the worktree an unknown cmd is allowed; the point is the agent_ops exact
    # allow must require main root, verified via build_decision below.
    mod = _load_guard_module()
    old = os.environ.copy()
    os.environ["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    os.environ["LOOP_ISSUE_NUMBER"] = "1137"
    try:
        d = mod.build_decision(payload)
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert d["reason"] != "agent_ops_tool_allowed"
    assert r.returncode in (0, 2)


def _load_guard_module():
    spec = _ilu.spec_from_file_location("worktree_scope_guard", str(GUARD_PY))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_skill_runtime_executor_allows_preflight_run(tmp_path):
    """Issue #1154 AC9: exact preflight executor command is allowed from root."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    payload = _bash_payload(
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        str(repo["root"]),
    )
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1154"}
    mod = _load_guard_module()
    old = os.environ.copy()
    os.environ["CLAUDE_PROJECT_DIR"] = str(repo["root"])
    os.environ["LOOP_ISSUE_NUMBER"] = "1154"
    try:
        decision = mod.build_decision(payload)
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert decision["decision"] == "allow", decision
    r = _run_guard(payload, repo["root"], issue="1154", extra_env=env)
    assert r.returncode == 0, r.stderr


def test_skill_runtime_executor_runs_preflight_directly(tmp_path):
    """Issue #1154: direct executor path accepts legal placeholders and runs preflight."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _install_skill_runtime_exec_fixture(repo["root"])
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1154",
    }
    result = subprocess.run(
        [
            "uv",
            "run",
            "python3",
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1154",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo["root"]),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    artifact = repo["root"] / ".claude" / "artifacts" / "issue-refinement-loop" / "1154" / "preflight.json"
    assert artifact.exists(), "expected preflight artifact to be created"
    payload = json.loads(artifact.read_text())
    assert payload == {"issue_number": "1154", "repo": "squne121/loop-protocol"}


def test_skill_runtime_executor_allows_without_catalog_entry_for_preflight_run(tmp_path):
    """Issue #1228: preflight.run is allowed even when no linked worktree is active."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _git("worktree", "remove", "--force", str(repo["worktree"]), cwd=repo["root"])
    _install_skill_runtime_exec_fixture(repo["root"], catalog_mode="missing")
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
    }
    payload = _bash_payload(
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        str(repo["root"]),
    )
    guard_result = _run_guard(payload, repo["root"], issue=None, extra_env=env)
    assert guard_result.returncode == 0, guard_result.stderr
    direct = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1154",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo["root"]),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert direct.returncode == 0, direct.stderr


@pytest.mark.parametrize(
    "command",
    [
        "python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        "uv run python3 /tmp/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        "uv run python3 .claude/skills/issue-refinement-loop/scripts/not_registered.py",
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol --unknown-flag x",
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        "bash -lc 'uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol'",
        "FOO=bar uv run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
        "uv --with pytest run python3 scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run --issue-number 1154 --repo squne121/loop-protocol",
    ],
)
def test_skill_runtime_executor_negative(tmp_path, command):
    """Issue #1154 AC8/AC10: malformed executor routes are fail-closed."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    payload = _bash_payload(command, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1154"}
    r = _run_guard(payload, repo["root"], issue="1154", extra_env=env)
    assert r.returncode == 2, r.stderr


def test_skill_runtime_executor_blocks_ignored_outside_write(tmp_path):
    """Issue #1154: ignored writes outside the allowed artifact root are fail-closed."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _install_skill_runtime_exec_fixture(repo["root"])
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1154",
        "SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "ignored",
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1154",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo["root"]),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "unauthorized write path" in result.stderr


def test_skill_runtime_executor_blocks_transient_outside_write(tmp_path):
    """Issue #1154: transient outside writes are detected via filesystem snapshot drift."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _install_skill_runtime_exec_fixture(repo["root"])
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1154",
        "SKILL_RUNTIME_TEST_OUTSIDE_WRITE": "transient",
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1154",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo["root"]),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 2
    assert "unauthorized write path" in result.stderr


def test_skill_runtime_executor_ignores_path_poisoning(tmp_path):
    """Issue #1154: executor resolves trusted uv/python instead of PATH-prepended shims."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _install_skill_runtime_exec_fixture(repo["root"])
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    marker = tmp_path / "poisoned.txt"
    (shim_dir / "uv").write_text(f"#!/bin/sh\necho poisoned > {marker}\nexit 99\n")
    (shim_dir / "uv").chmod(0o755)
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1154",
        "PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1154",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo["root"]),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


# --- AC4/AC5/AC6: V3 one-shot decisions via real hook ---


def test_v3_valid_worktree_remove_allow_real_hook(tmp_path):
    """AC4/AC5: valid V3 contract → exact worktree remove allowed (real hook)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 0, f"valid V3 must allow; stderr={r.stderr}"


def test_v3_expired_denied_real_hook(tmp_path):
    """AC5: expired V3 contract → cleanup_contract_expired (real hook deny)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE, expired=True)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2
    assert "cleanup_contract_expired" in r.stderr


def test_v3_command_hash_mismatch_denied_real_hook(tmp_path):
    """AC5: tampered command_hash → cleanup_command_hash_mismatch (real hook deny)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE, bad_hash=True)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2
    assert "cleanup_command_hash_mismatch" in r.stderr


def test_v3_operation_mismatch_denied_real_hook(tmp_path):
    """AC5: operation mismatch → cleanup_operation_mismatch (real hook deny)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    # contract issued for branch_delete, but command is worktree remove
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_BRANCH_DELETE)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2
    assert "cleanup_operation_mismatch" in r.stderr


def test_v3_present_but_invalid_no_v2_downgrade_real_hook(tmp_path):
    """AC4/Blocker2: corrupt V3 + valid V2 env contract → deny (no downgrade)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE, corrupt=True)
    v2 = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_real,
        "branch_name": "issue-1050-g",
        "require_clean": True,
    }
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {
        "CLAUDE_PROJECT_DIR": str(repo["root"]),
        "LOOP_ISSUE_NUMBER": "1050",
        "CLAUDE_WORKTREE_CLEANUP_CONTRACT": json.dumps(v2),
    }
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2, "present-but-invalid V3 must not downgrade to V2"
    assert "cleanup_contract_present_but_invalid" in r.stderr


def test_v3_one_shot_consume_real_hook(tmp_path):
    """AC6: a valid V3 contract is consumed after allow; replay is denied (real hook)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    first = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert first.returncode == 0, f"first must allow; {first.stderr}"
    # contract consumed → replay denied
    contract_path = repo["root"] / "artifacts" / "agent-ops" / "cleanup_contract.json"
    assert not contract_path.exists(), "contract must be consumed (one-shot)"
    second = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert second.returncode == 2, "replay must be denied"


def test_v3_root_drift_active_worktree_denied_real_hook(tmp_path):
    """AC13: cleanup from a drifted root with active worktree → shared reason (real hook)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    _git("switch", "-c", "issue-1137-drift", cwd=repo["root"])
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    r = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert r.returncode == 2
    assert "root_drift_active_worktree_mismatch" in r.stderr
    assert len(r.stderr.strip().splitlines()) <= 10
    assert wt_real not in r.stderr


def test_v3_shared_reason_codes_vocabulary():
    """AC15: cleanup deny reasons are drawn from SHARED_CLEANUP_REASON_CODES."""
    for code in (
        "cleanup_contract_present_but_invalid",
        "cleanup_contract_expired",
        "cleanup_command_hash_mismatch",
        "cleanup_operation_mismatch",
        "root_drift_active_worktree_mismatch",
        "cleanup_contract_consumed",
    ):
        assert code in _cc3.SHARED_CLEANUP_REASON_CODES


# =============================================================================
# Issue #1137: agent-ops module behavioral tests (materialize / preflight / exec)
# =============================================================================

import guard_preflight as _gp  # noqa: E402
import materialize_cleanup_contract as _mat  # noqa: E402
import cleanup_exec as _ce  # noqa: E402


def _seed_repo_wt(tmp_path, issue="1050", slug="g"):
    repo = _make_repo_with_worktree(tmp_path, issue=issue, slug=slug)
    return repo


def test_materialize_durable_0600_round_trip(tmp_path):
    """AC10: materialize writes a 0600 file the reader accepts and validates."""
    repo = _seed_repo_wt(tmp_path)
    wt_real = os.path.realpath(str(repo["worktree"]))
    r = _mat.materialize(
        pr_number=1,
        linked_issue_number=1050,
        worktree_path=wt_real,
        branch_name="issue-1050-g",
        operation=_cc3.OP_WORKTREE_REMOVE,
        project_root=str(repo["root"]),
        verify=False,
    )
    assert r["status"] == "ok", r
    target = repo["root"] / "artifacts" / "agent-ops" / "cleanup_contract.json"
    assert (target.stat().st_mode & 0o777) == 0o600
    state, contract, reason = _cc3.load_contract_state(str(repo["root"]))
    assert state == _cc3.STATE_VALID_V3, (state, reason)


def test_materialize_symlink_fail_closed(tmp_path):
    """AC10: a symlinked artifacts/ safe-root is rejected (no write through symlink)."""
    repo = _seed_repo_wt(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo["root"] / "artifacts").symlink_to(elsewhere, target_is_directory=True)
    wt_real = os.path.realpath(str(repo["worktree"]))
    r = _mat.materialize(
        pr_number=1,
        linked_issue_number=1050,
        worktree_path=wt_real,
        branch_name="issue-1050-g",
        project_root=str(repo["root"]),
        verify=False,
    )
    assert r["status"] == "error"
    assert "write_failed" in r["error"]


def test_materialize_ttl_bounds(tmp_path):
    """AC12: TTL must be positive and within the max bound."""
    repo = _seed_repo_wt(tmp_path)
    wt_real = os.path.realpath(str(repo["worktree"]))
    r = _mat.materialize(
        pr_number=1,
        linked_issue_number=1050,
        worktree_path=wt_real,
        branch_name="issue-1050-g",
        project_root=str(repo["root"]),
        verify=False,
        ttl_seconds=_cc3.MAX_TTL_SECONDS + 1,
    )
    assert r["status"] == "error"
    assert r["error"] == "ttl_out_of_bounds"


def test_preflight_matrix(tmp_path, monkeypatch):
    """AC7: preflight uses the real catalog (env-only does not fake matches)."""
    repo = _seed_repo_wt(tmp_path)
    # env set but issue 999999 has no catalog entry -> not matches
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", "999999")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo["root"]))
    pf = _gp.build_preflight(project_root=str(repo["root"]), cwd=str(repo["root"]))
    assert pf["active_worktree_state"] != "matches"
    # real worktree for issue 1050 + root main -> matches/ok
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1050")
    pf2 = _gp.build_preflight(project_root=str(repo["root"]), cwd=str(repo["root"]))
    assert pf2["active_worktree_state"] == "matches"
    assert pf2["status"] == "ok"
    assert pf2["resolved_worktree"]["worktree_realpath"] == os.path.realpath(str(repo["worktree"]))


def test_preflight_drift_human_required(tmp_path, monkeypatch):
    """AC7/AC13: drifted root + active worktree -> human_required, no mutation."""
    repo = _seed_repo_wt(tmp_path)
    _git("switch", "-q", "-c", "issue-1137-drift", cwd=repo["root"])
    monkeypatch.setenv("LOOP_ISSUE_NUMBER", "1050")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo["root"]))
    pf = _gp.build_preflight(project_root=str(repo["root"]), cwd=str(repo["root"]))
    assert pf["status"] == "human_required"
    assert "root_drift_active_worktree_mismatch" in pf["blocked_reason_codes"]
    # no mutation
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=str(repo["root"]), capture_output=True, text=True
    ).stdout.strip()
    assert branch == "issue-1137-drift"


def test_cleanup_exec_refuses_non_default_root(tmp_path):
    """AC1: cleanup_exec refuses when the root checkout is not on the default branch."""
    repo = _seed_repo_wt(tmp_path)
    _git("switch", "-q", "-c", "issue-1137-drift", cwd=repo["root"])
    req = {
        "pr_number": 1,
        "linked_issue_number": 1050,
        "worktree_path": os.path.realpath(str(repo["worktree"])),
        "branch_name": "issue-1050-g",
    }
    res = _ce.run(req, project_root=str(repo["root"]))
    assert res["status"] == "refused"
    assert res["reason_code"] == _ce.ROOT_NOT_DEFAULT
    assert res["verified"]["root_default"] is False


def test_cleanup_exec_refuses_worktree_not_in_catalog(tmp_path):
    """AC1: cleanup_exec refuses when the worktree is not in the real catalog."""
    repo = _seed_repo_wt(tmp_path)
    bogus = str(repo["root"] / ".claude" / "worktrees" / "issue-1050-nonexistent")
    req = {"pr_number": 1, "linked_issue_number": 1050, "worktree_path": bogus, "branch_name": "issue-1050-g"}
    res = _ce.run(req, project_root=str(repo["root"]))
    assert res["status"] == "refused"
    assert res["reason_code"] == "branch_checked_out_in_worktree"


def test_branch_ref_format_validation():
    """AC12: branch validation rejects invalid Git ref grammar."""
    assert _cc3.is_valid_branch_ref("issue-1050-ok")
    assert not _cc3.is_valid_branch_ref("bad~branch")
    assert not _cc3.is_valid_branch_ref("bad branch")
    assert not _cc3.is_valid_branch_ref("")


def test_expiry_requires_timezone():
    """AC12: naive (timezone-less) timestamps are rejected."""
    assert _cc3.parse_iso8601_tz("2026-01-01T00:00:00") is None
    assert _cc3.parse_iso8601_tz("2026-01-01T00:00:00Z") is not None


# =============================================================================
# PR #1143 OWNER review — adversarial regression tests for the 10 blockers
# =============================================================================


@pytest.mark.parametrize(
    "command",
    [
        # Blocker 1: newline-separated second command must NOT ride along on the
        # agent-ops exact allow (a bare `;`-free `\n git worktree remove ...`).
        "uv run python3 scripts/agent-ops/guard_preflight.py --json\n"
        "git worktree remove .claude/worktrees/issue-1137-x",
        "uv run python3 scripts/agent-ops/guard_preflight.py --json\rgit branch -d issue-1137-x",
        # Blocker 1 / Blocker 5: --project-root / --no-verify escape-hatch flags are
        # not part of the exact command class.
        "uv run python3 scripts/agent-ops/cleanup_exec.py --json --project-root /etc",
        "uv run python3 scripts/agent-ops/materialize_cleanup_contract.py --no-verify --pr-number 1",
        # Blocker 1: duplicate / unknown flags rejected.
        "uv run python3 scripts/agent-ops/cleanup_exec.py --json --json",
    ],
)
def test_agent_ops_newline_and_extra_flag_rejected_real_hook(tmp_path, command):
    """Blocker 1: newline injection / escape-hatch / duplicate flags are not agent-ops-allowed."""
    repo = _make_repo_with_worktree(tmp_path, issue="1137", slug="x")
    payload = _bash_payload(command, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1137"}
    r = _run_guard(payload, repo["root"], issue="1137", extra_env=env)
    assert r.returncode == 2, f"injection/extra-flag must not be agent-ops-allowed; stderr={r.stderr}"


def _claim_worker(root):
    """Top-level worker for the concurrent-claim test (must be picklable)."""
    import cleanup_contract_v3 as cc

    return cc.claim_contract(root)


def test_claim_contract_concurrent_single_winner(tmp_path):
    """Blocker 2: under concurrent claims exactly ONE process wins the one-shot contract."""
    import multiprocessing as mp

    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    root = str(repo["root"])
    ctx = mp.get_context("fork")
    with ctx.Pool(8) as pool:
        results = pool.map(_claim_worker, [root] * 8)
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"exactly one concurrent claim must win, got {winners}"


def test_tombstone_forbids_v2_downgrade_after_consume(tmp_path):
    """Blocker 3: after a V3 consume, a durable tombstone forbids legacy V2 fallback."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    payload = _bash_payload(f"git worktree remove {wt_real}", str(repo["worktree"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1050"}
    first = _run_guard(payload, repo["root"], issue="1050", extra_env=env)
    assert first.returncode == 0, f"first must allow; {first.stderr}"
    tomb = repo["root"] / "artifacts" / "agent-ops" / "cleanup_contract.tombstone.json"
    assert tomb.exists(), "consume must leave a durable tombstone"
    # A leftover V2 env contract must NOT re-authorize a second cleanup operation.
    v2 = {
        "schema": "POST_MERGE_CLEANUP_REQUEST_V2",
        "worktree_path": wt_real,
        "branch_name": "issue-1050-g",
        "require_clean": True,
    }
    env2 = dict(env)
    env2["CLAUDE_WORKTREE_CLEANUP_CONTRACT"] = json.dumps(v2)
    second = _run_guard(payload, repo["root"], issue="1050", extra_env=env2)
    assert second.returncode == 2, "V2 downgrade after a V3 consume must be denied"
    assert "cleanup_v2_downgrade_denied" in second.stderr


def test_present_contract_io_uncapable_denied(tmp_path, monkeypatch):
    """Blocker 9: a present V3 contract on an IO-incapable platform is denied (no ABSENT fallthrough)."""
    repo = _make_repo_with_worktree(tmp_path, issue="1050", slug="g")
    wt_real = os.path.realpath(str(repo["worktree"]))
    _write_v3(repo["root"], wt_real, "issue-1050-g", _cc3.OP_WORKTREE_REMOVE)
    monkeypatch.setattr(_cc3, "IO_CAPABLE", False)
    state, _contract, reason = _cc3.load_contract_state(str(repo["root"]))
    assert state == _cc3.STATE_PRESENT_BUT_INVALID
    assert reason == _cc3.CLEANUP_IO_UNSUPPORTED_PLATFORM


def test_cleanup_exec_no_project_root_or_no_verify_cli_flags():
    """Blocker 1/5: the public CLIs expose neither --project-root nor --no-verify."""
    with pytest.raises(SystemExit):
        _ce.main(["--pr-number", "1", "--worktree-path", "/x", "--branch-name", "b", "--project-root", "/y"])
    with pytest.raises(SystemExit):
        _mat.main(["--pr-number", "1", "--worktree-path", "/x", "--branch-name", "b", "--no-verify"])


def test_cleanup_exec_rejects_cross_repo_pr(tmp_path, monkeypatch):
    """Blocker 5: a fork / cross-repo PR must not authorize deleting our local worktree."""
    repo = _seed_repo_wt(tmp_path)
    wt_real = os.path.realpath(str(repo["worktree"]))
    monkeypatch.setattr(_ce, "_repo_slug", lambda root, dl: "squne121/loop-protocol")
    monkeypatch.setattr(_ce, "_local_branch_tip", lambda root, br, dl: "deadbeef")
    monkeypatch.setattr(
        _ce,
        "_pr_state",
        lambda pr, root, slug, dl: {
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefName": "issue-1050-g",
            "headRefOid": "deadbeef",
            "baseRefName": "main",
            "isCrossRepository": True,
            "headRepositoryOwner": {"login": "attacker"},
            "closingIssuesReferences": [{"number": 1050}],
        },
    )
    req = {"pr_number": 1, "linked_issue_number": 1050, "worktree_path": wt_real, "branch_name": "issue-1050-g"}
    res = _ce.run(req, project_root=str(repo["root"]))
    assert res["status"] == "refused"
    assert res["reason_code"] == _ce.HEAD_REPO_MISMATCH


def test_cleanup_exec_rejects_head_oid_mismatch(tmp_path, monkeypatch):
    """Blocker 5: a same-named branch at a different commit must not authorize deletion."""
    repo = _seed_repo_wt(tmp_path)
    wt_real = os.path.realpath(str(repo["worktree"]))
    monkeypatch.setattr(_ce, "_repo_slug", lambda root, dl: "squne121/loop-protocol")
    monkeypatch.setattr(_ce, "_local_branch_tip", lambda root, br, dl: "aaaaaaa")
    monkeypatch.setattr(
        _ce,
        "_pr_state",
        lambda pr, root, slug, dl: {
            "state": "MERGED",
            "mergedAt": "2026-01-01T00:00:00Z",
            "headRefName": "issue-1050-g",
            "headRefOid": "bbbbbbb",
            "baseRefName": "main",
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "squne121"},
            "closingIssuesReferences": [{"number": 1050}],
        },
    )
    req = {"pr_number": 1, "linked_issue_number": 1050, "worktree_path": wt_real, "branch_name": "issue-1050-g"}
    res = _ce.run(req, project_root=str(repo["root"]))
    assert res["status"] == "refused"
    assert res["reason_code"] == _ce.HEAD_OID_MISMATCH


def test_perform_preserves_partial_actions_on_branch_delete_fail(tmp_path):
    """Blocker 6: worktree removed but branch -d fails → actions_taken keeps the partial success."""
    from worktree_catalog import Deadline

    repo = _seed_repo_wt(tmp_path)
    wt_real = os.path.realpath(str(repo["worktree"]))
    # An unmerged commit on the branch makes `git branch -d` refuse after worktree removal.
    _git("commit", "--allow-empty", "-q", "-m", "extra", cwd=repo["worktree"])
    actions, err = _ce._perform("issue-1050-g", wt_real, str(repo["root"]), Deadline(60.0))
    assert actions == [_cc3.OP_WORKTREE_REMOVE], actions
    assert err is not None and "branch_delete_failed" in err


# =============================================================================
# Issue #1166: controlled skill mutation policy tests (AC2, AC5, AC9)
# Real hook path via subprocess (PreToolUse stdin JSON).
# =============================================================================


def test_publish_termination_direct_denied_real_hook(tmp_path):
    """AC2/AC9: direct publish_termination_report.py invocation is denied by real hook.

    python3 .claude/skills/.../publish_termination_report.py must not be allowed
    as a direct command from the main root when an issue worktree is active.
    The deny is due to unknown-class cwd-outside-worktree fail-closed block.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="1166", slug="hooks-pub")
    cmd = (
        "python3 .claude/skills/issue-refinement-loop/scripts/publish_termination_report.py"
        " --issue-number 1166 --repo squne121/loop-protocol"
    )
    payload = _bash_payload(cmd, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1166"}
    r = _run_guard(payload, repo["root"], issue="1166", extra_env=env)
    assert r.returncode == 2, f"direct publish_termination_report.py must be denied; stderr={r.stderr}"


def test_publish_termination_executor_allowed_real_hook(tmp_path):
    """AC5/AC9: controlled_skill_mutation_exec.py with valid argv is allowed by real hook.

    The executor command passes the shared policy check and is allowed even when
    an issue worktree is active and the command is run from the main root.
    """
    repo = _make_repo_with_worktree(tmp_path, issue="1166", slug="hooks-pub")
    # Create the executor script in the tmp repo (so realpath resolves correctly)
    executor_dir = repo["root"] / "scripts" / "agent-guards"
    executor_dir.mkdir(parents=True, exist_ok=True)
    executor = executor_dir / "controlled_skill_mutation_exec.py"
    executor.write_text("# stub\n")
    # Create a plausible input-file in artifacts subtree
    artifact_dir = repo["root"] / "artifacts" / "1166"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    input_file = artifact_dir / "termination_report_input.json"
    input_file.write_text("{}\n")

    cmd = (
        "uv run python3 scripts/agent-guards/controlled_skill_mutation_exec.py"
        " --command-id termination_report.publish"
        " --issue-number 1166"
        " --input-file artifacts/1166/termination_report_input.json"
        " --repo squne121/loop-protocol"
    )
    payload = _bash_payload(cmd, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1166"}
    r = _run_guard(payload, repo["root"], issue="1166", extra_env=env)
    assert r.returncode == 0, f"controlled_skill_mutation_exec.py with valid argv must be allowed; stderr={r.stderr}"


# =============================================================================
# Issue #1197: probe scripts exact allow in worktree_scope_guard
# =============================================================================


class TestProbeScriptsExactAllow:
    """Issue #1197: probe scripts must be allowed by worktree_scope_guard."""

    def test_git_ref_probe_in_allowed_scripts(self) -> None:
        """git_ref_probe.py must be in _AGENT_OPS_ALLOWED_SCRIPTS."""
        import importlib.util

        guard_py = GUARD_PY
        spec = importlib.util.spec_from_file_location("wsg_probe", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        import sys

        old = sys.path[:]
        _add_guard_paths(mod, guard_py)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old
        assert "scripts/agent-ops/git_ref_probe.py" in mod._AGENT_OPS_ALLOWED_SCRIPTS

    def test_git_worktree_probe_in_allowed_scripts(self) -> None:
        """git_worktree_probe.py must be in _AGENT_OPS_ALLOWED_SCRIPTS."""
        import importlib.util

        guard_py = GUARD_PY
        spec = importlib.util.spec_from_file_location("wsg_probe2", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        import sys

        old = sys.path[:]
        _add_guard_paths(mod, guard_py)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old
        assert "scripts/agent-ops/git_worktree_probe.py" in mod._AGENT_OPS_ALLOWED_SCRIPTS

    def test_ref_probe_argv_valid_passes_spec(self) -> None:
        """Valid argv for git_ref_probe.py passes _validate_agent_ops_argv."""
        import importlib.util

        guard_py = GUARD_PY
        spec = importlib.util.spec_from_file_location("wsg_probe3", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        import sys

        old = sys.path[:]
        _add_guard_paths(mod, guard_py)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old
        args = ["--branch", "main", "--json"]
        assert mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args)

    def test_worktree_probe_argv_valid_passes_spec(self) -> None:
        """Valid argv for git_worktree_probe.py passes _validate_agent_ops_argv."""
        import importlib.util

        guard_py = GUARD_PY
        spec = importlib.util.spec_from_file_location("wsg_probe4", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        import sys

        old = sys.path[:]
        _add_guard_paths(mod, guard_py)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old
        args = ["--json"]
        assert mod._validate_agent_ops_argv("scripts/agent-ops/git_worktree_probe.py", args)

    def test_ref_probe_unknown_flag_fails_spec(self) -> None:
        """Unknown flag in git_ref_probe.py argv fails _validate_agent_ops_argv."""
        import importlib.util

        guard_py = GUARD_PY
        spec = importlib.util.spec_from_file_location("wsg_probe5", str(guard_py))
        mod = importlib.util.module_from_spec(spec)
        import sys

        old = sys.path[:]
        _add_guard_paths(mod, guard_py)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = old
        args = ["--branch", "main", "--unknown-flag"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args)
        # B2: missing required --branch must be denied (required check in guard, not just argparse)
        args_no_branch = ["--json"]
        assert not mod._validate_agent_ops_argv("scripts/agent-ops/git_ref_probe.py", args_no_branch)


def _add_guard_paths(mod, guard_py):
    """Add required paths to sys.path for loading worktree_scope_guard."""
    import sys

    repo = guard_py.parent.parent.parent
    for sub in ("scripts/agent-guards", "scripts/agent-ops"):
        p = str(repo / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def test_publish_lane_push_allowed_when_remote_and_reviewed_heads_match(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # Issue #1408 iteration-2: remote_readback_source is restricted to
    # `ls_remote`, which performs a live `git ls-remote` against `origin`
    # instead of trusting a self-declared `refs/remotes/origin/*` ref. Point
    # `origin` at a real local bare repo so the live readback succeeds.
    remote_bare = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(remote_bare), cwd=tmp_path)
    _git("remote", "set-url", "origin", str(remote_bare), cwd=repo["worktree"])
    head = _git("rev-parse", "HEAD", cwd=repo["worktree"]).stdout.strip()
    _git("push", "-q", "origin", "HEAD:refs/heads/issue-942-x", cwd=repo["worktree"])
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rtk git " + "push origin HEAD:refs/heads/issue-942-x"},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(
        payload,
        repo["root"],
        issue="942",
        extra_env={
            "LOOP_PUBLISH_EXPECTED_REMOTE_HEAD": head,
            "LOOP_PUBLISH_CURRENT_REMOTE_HEAD": head,
            "LOOP_PUBLISH_DECLARED_PUBLISH_HEAD": head,
            "LOOP_PUBLISH_VERIFIED_HEAD": head,
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS": "ok",
            "LOOP_PUBLISH_REMOTE_READBACK_SOURCE": "ls_remote",
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER": "942",
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA": head,
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA": head,
            "LOOP_CANONICAL_REPO_URL_PATTERN": "^" + re.escape(str(remote_bare)) + "$",
        },
    )
    assert r.returncode == 0, r.stderr


def test_publish_lane_push_denies_missing_strict_context(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rtk git " + "push origin HEAD:refs/heads/issue-942-x"},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(payload, repo["root"], issue="942")

    assert r.returncode == 2, r.stderr
    assert "PUBLISH_SAFETY_STOP_REPORT_V1:" in r.stderr
    assert "publish_guard_context_missing" in r.stderr
    assert "decision_inputs_complete: false" in r.stderr


def test_publish_lane_push_emits_safety_stop_report_for_fast_forward_remote_head(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="942")
    # Issue #1408 iteration-2: remote_readback_source is restricted to
    # `ls_remote`, which performs a live `git ls-remote` against `origin`
    # instead of trusting a self-declared `refs/remotes/origin/*` ref. Point
    # `origin` at a real local bare repo so the live readback observes the
    # fast-forwarded remote_head pushed below (simulating another agent
    # publishing to the same scope ahead of this push attempt).
    remote_bare = tmp_path / "origin.git"
    _git("init", "--bare", "-q", str(remote_bare), cwd=tmp_path)
    _git("remote", "set-url", "origin", str(remote_bare), cwd=repo["worktree"])
    expected_head = _git("rev-parse", "HEAD", cwd=repo["worktree"]).stdout.strip()
    _git("push", "-q", "origin", "HEAD:refs/heads/issue-942-x", cwd=repo["worktree"])
    main = repo["root"]
    (main / "README.md").write_text("next\n")
    _git("add", "README.md", cwd=main)
    _git("commit", "-q", "-m", "next", cwd=main)
    remote_head = _git("rev-parse", "HEAD", cwd=main).stdout.strip()
    _git("push", "-q", "origin", "HEAD:refs/heads/issue-942-x", cwd=main)
    _git("reset", "--hard", expected_head, cwd=repo["worktree"])
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rtk git " + "push origin HEAD:refs/heads/issue-942-x"},
        "cwd": str(repo["worktree"]),
    }
    r = _run_guard(
        payload,
        repo["root"],
        issue="942",
        extra_env={
            "LOOP_PUBLISH_EXPECTED_REMOTE_HEAD": expected_head,
            "LOOP_PUBLISH_CURRENT_REMOTE_HEAD": remote_head,
            "LOOP_PUBLISH_DECLARED_PUBLISH_HEAD": expected_head,
            "LOOP_PUBLISH_VERIFIED_HEAD": expected_head,
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_STATUS": "ok",
            "LOOP_PUBLISH_REMOTE_READBACK_SOURCE": "ls_remote",
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_ISSUE_NUMBER": "942",
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_BASE_SHA": expected_head,
            "LOOP_PUBLISH_ALLOWED_PATHS_GATE_HEAD_SHA": expected_head,
            "LOOP_PR_NUMBER": "1410",
            "LOOP_CANONICAL_REPO_URL_PATTERN": "^" + re.escape(str(remote_bare)) + "$",
        },
    )
    assert r.returncode == 2, r.stderr
    assert "PUBLISH_SAFETY_STOP_REPORT_V1:" in r.stderr
    assert "remote_fast_forward_by_same_scope" in r.stderr
    assert 'pr_number: "1410"' in r.stderr
    assert "declared_publish_head" in r.stderr
    assert 'remote_readback_source: "ls_remote"' in r.stderr
    assert "decision_inputs_complete: true" in r.stderr
    assert expected_head in r.stderr


# =============================================================================
# Issue #1498: preflight.run.with_anchor sibling exact profile
# =============================================================================
# Real PreToolUse hook path (stdin JSON via worktree_scope_guard.sh, real git
# worktree) for the Positive/Negative Test Matrix's allow (#2) and deny
# (#3-#22) cases that are within worktree_scope_guard's own responsibility
# (exact-command classification + root-no-worktree eligibility). URL-shape
# and parser-level rejections (already covered exhaustively by
# scripts/agent-guards/tests/test_skill_runtime_command_policy_anchor.py) are
# re-verified here end-to-end through the real hook to rule out split-brain
# (Matrix #23) between the parser and this guard's allow/deny decision.

_ANCHOR_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1154#issuecomment-1"


def _anchor_command(
    issue_number: str = "1154",
    repo: str = "squne121/loop-protocol",
    url: str = _ANCHOR_VALID_URL,
) -> str:
    return (
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.with_anchor "
        f"--issue-number {issue_number} --repo {repo} --anchor-comment-url {url}"
    )


def test_worktree_scope_guard_allows_anchor_profile_matrix_2(tmp_path):
    """Matrix #2: correct single anchor + preflight.run.with_anchor → allow."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    payload = _bash_payload(_anchor_command(), str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1154"}
    r = _run_guard(payload, repo["root"], issue="1154", extra_env=env)
    assert r.returncode == 0, r.stderr


def test_worktree_scope_guard_allows_anchor_profile_without_active_worktree(tmp_path):
    """preflight.run.with_anchor is root-no-worktree eligible, mirroring preflight.run."""
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    _git("worktree", "remove", "--force", str(repo["worktree"]), cwd=repo["root"])
    payload = _bash_payload(_anchor_command(), str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"])}
    r = _run_guard(payload, repo["root"], issue=None, extra_env=env)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("name", "command"),
    [
        # Matrix #3: anchor missing on preflight.run.with_anchor is not a
        # constructible command for this exact-command_id (the flag itself
        # is absent), covered instead by feeding preflight.run.with_anchor
        # with a trailing missing value (#17) and by omitting the flag pair.
        (
            "missing_anchor_flag",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol",
        ),
        # Matrix #4: anchor added to preflight.run (production, unmodified).
        (
            "anchor_on_preflight_run",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL,
        ),
        # Matrix #5: two distinct --anchor-comment-url flags.
        (
            "duplicate_distinct_anchor_flags",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL
            + " --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/1154#issuecomment-2",
        ),
        # Matrix #7: different repository in the URL.
        (
            "different_repo_in_url",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/other/repo/issues/1154#issuecomment-1",
        ),
        # Matrix #8: different issue number in the URL.
        (
            "different_issue_in_url",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/999#issuecomment-1",
        ),
        # Matrix #9: pull request review comment URL.
        (
            "pull_request_review_comment_url",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/pull/1154/files#r1",
        ),
        # Matrix #10: discussion_r fragment form.
        (
            "discussion_r_fragment",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/1154#discussion_r1",
        ),
        # Matrix #11: query string present.
        (
            "query_string",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/1154?tab=1#issuecomment-1",
        ),
        # Matrix #12: trailing slash suffix.
        (
            "trailing_slash",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/1154#issuecomment-1/",
        ),
        # Matrix #13: userinfo present.
        (
            "userinfo",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://user@github.com/squne121/loop-protocol/issues/1154#issuecomment-1",
        ),
        # Matrix #14: percent-encoding disguise.
        (
            "percent_encoded",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            "https://github.com/squne121/loop-protocol/issues/1154%23issuecomment-1",
        ),
        # Matrix #15: --anchor-comment-url=URL (= form).
        (
            "eq_form",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url=" + _ANCHOR_VALID_URL,
        ),
        # Matrix #16: option abbreviation.
        (
            "abbreviated_flag",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-u " + _ANCHOR_VALID_URL,
        ),
        # Matrix #17: flag present but no value.
        (
            "flag_no_value",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url",
        ),
        # Matrix #18: unknown extra flag.
        (
            "unknown_extra_flag",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL
            + " --extra x",
        ),
        # Matrix #19: duplicate flag (identical URL twice).
        (
            "duplicate_identical_anchor_flags",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url " + _ANCHOR_VALID_URL
            + " --anchor-comment-url " + _ANCHOR_VALID_URL,
        ),
        # Matrix #20: flag order changed.
        (
            "flag_order_changed",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--anchor-comment-url " + _ANCHOR_VALID_URL
            + " --command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol",
        ),
        # Matrix #21: shell metacharacter in value.
        (
            "shell_metachar",
            "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
            "--command-id preflight.run.with_anchor --issue-number 1154 "
            "--repo squne121/loop-protocol --anchor-comment-url "
            + _ANCHOR_VALID_URL + ";rm -rf /",
        ),
    ],
)
def test_worktree_scope_guard_denies_anchor_profile_negative_matrix(tmp_path, name, command):
    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    payload = _bash_payload(command, str(repo["root"]))
    env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1154"}
    r = _run_guard(payload, repo["root"], issue="1154", extra_env=env)
    assert r.returncode == 2, f"{name}: expected deny, got allow: {r.stderr}"


def test_worktree_scope_guard_anchor_matrix_no_split_brain_with_policy_parser(tmp_path):
    """Matrix #23: worktree_scope_guard's allow/deny must agree with the
    underlying parser (scripts/agent-guards/skill_runtime_command_policy.py)
    for the same command set — no split-brain allowlist."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))
    from skill_runtime_command_policy import (  # noqa: E402
        parse_exact_skill_runtime_anchor_command,
    )

    repo = _make_repo_with_worktree(tmp_path, issue="1154", slug="x")
    commands = [
        _anchor_command(),
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.with_anchor --issue-number 1154 "
        "--repo squne121/loop-protocol --anchor-comment-url=" + _ANCHOR_VALID_URL,
    ]
    for command in commands:
        payload = _bash_payload(command, str(repo["root"]))
        env = {"CLAUDE_PROJECT_DIR": str(repo["root"]), "LOOP_ISSUE_NUMBER": "1154"}
        r = _run_guard(payload, repo["root"], issue="1154", extra_env=env)
        guard_allows = r.returncode == 0
        # The command under test targets skill_runtime_exec.py's own argv
        # contract (12 tokens: uv run python3 <script> --command-id ...),
        # which mirrors the parser's own exact-match contract byte for byte.
        parser_allows = (
            parse_exact_skill_runtime_anchor_command(command, str(repo["root"])) is not None
        )
        assert guard_allows == parser_allows, (
            f"split-brain: guard={guard_allows} parser={parser_allows} for {command!r}"
        )


# Issue 1609 fix_delta P0 Blocker regression: the merge lane authorization
# (active Issue resolved, matching worktree count, cwd binding) must run
# BEFORE any merge transaction executes -- every unauthorized shape below
# must leave HEAD completely untouched.
def test_merge_ff_only_authorizes_before_transaction(tmp_path):
    repo = _make_repo_with_worktree(tmp_path, issue="1609", slug="mergeauth", extra_worktrees=[("1609", "other")])
    worktree = repo["worktrees"]["1609"]
    _git("checkout", "-q", "-b", "worktree-issue-1609-mergeauth", cwd=worktree)
    head_before = _git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    fake_sha = "a" * 40
    command = "rtk git merge --ff-only " + fake_sha
    payload = _bash_payload(command, str(worktree))

    # (a) no active Issue context at all.
    r = _run_guard(payload, repo["root"], issue=None)
    assert r.returncode != 0
    assert _git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == head_before

    # (b) active Issue set but zero / ambiguous matching worktree for that
    # number -- "1609" now resolves to TWO worktrees (mergeauth + other).
    r = _run_guard(payload, repo["root"], issue="1609")
    assert r.returncode != 0
    assert _git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == head_before

    # (c) active Issue set to a number with zero matching worktree.
    r = _run_guard(payload, repo["root"], issue="424242")
    assert r.returncode != 0
    assert _git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == head_before

    # (d) cwd outside the expected worktree (the repo root itself).
    root_head_before = _git("rev-parse", "HEAD", cwd=repo["root"]).stdout.strip()
    payload_root = _bash_payload(command, str(repo["root"]))
    r = _run_guard(payload_root, repo["root"], issue="1609")
    assert r.returncode != 0
    assert _git("rev-parse", "HEAD", cwd=repo["root"]).stdout.strip() == root_head_before
    assert _git("rev-parse", "HEAD", cwd=worktree).stdout.strip() == head_before
