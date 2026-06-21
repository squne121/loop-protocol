#!/usr/bin/env python3
"""test_worktree_scope_guard.py — contract tests for worktree_scope_guard (Issue #960).

Covers AC1..AC15 of WORKTREE_SCOPE_RESOLUTION_V1 / MUTATING_BASH_CLASSIFIER_V1.

Path anchoring: this test resolves the guard via __file__ (worktree-local), NOT via
`git rev-parse --show-toplevel`, because worktree isolation makes the latter return
the main repo root. (Mirrors test_secret_boundary_contract.py.)

Test harness: each test builds an isolated temporary git repo + a real
`issue-<n>-<slug>` worktree, points CLAUDE_PROJECT_DIR at the repo root and
LOOP_ISSUE_NUMBER at the active issue, then invokes the guard wrapper via subprocess
with a PreToolUse-shaped stdin payload.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

# Anchor on __file__ for worktree isolation.
_THIS_FILE = Path(__file__).resolve()
REPO_ROOT = _THIS_FILE.parent.parent.parent.parent  # worktree root
GUARD_SH = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.sh"
GUARD_PY = REPO_ROOT / ".claude" / "hooks" / "worktree_scope_guard.py"
SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"


# =============================================================================
# Harness
# =============================================================================

def _git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _make_repo_with_worktree(tmp_path: Path, issue: str = "942",
                             slug: str = "x", extra_worktrees=None) -> dict:
    """Create a git repo + a real issue worktree. Returns dict with paths."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=main)
    _git("commit", "-q", "-m", "seed", cwd=main)

    worktrees = {}
    wt_path = main / ".claude" / "worktrees" / f"issue-{issue}-{slug}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    branch = f"issue-{issue}-{slug}"
    _git("worktree", "add", "-q", "-b", branch, str(wt_path), "main", cwd=main)
    worktrees[issue] = wt_path

    for extra in (extra_worktrees or []):
        ei, es = extra
        ewt = main / ".claude" / "worktrees" / f"issue-{ei}-{es}"
        eb = f"issue-{ei}-{es}"
        _git("worktree", "add", "-q", "-b", eb, str(ewt), "main", cwd=main)
        worktrees[ei] = ewt

    return {"root": main, "worktree": wt_path, "worktrees": worktrees}


def _run_guard(payload: dict, project_root: Path, issue: str | None = None,
               extra_env: dict | None = None):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    if issue is not None:
        env["LOOP_ISSUE_NUMBER"] = str(issue)
    else:
        env.pop("LOOP_ISSUE_NUMBER", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(GUARD_SH)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
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

@pytest.mark.parametrize("command", [
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
])
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


@pytest.mark.parametrize("command", [
    "echo hi > out.txt",
    "echo hi >> out.txt",
    "sed -i 's/a/b/' file.txt",
    "cat foo | tee out.txt",
    "npm install",
    "pnpm add left-pad",
    "yarn remove x",
    "bun add y",
])
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

@pytest.mark.parametrize("command", [
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
])
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
    r = _run_guard(payload, repo["root"], issue="942",
                   extra_env={"LEAKY_ENV_VAR": "LEAKVAL_abc"})
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
    """AC6: in settings.json PreToolUse, the shared-tool secret_boundary_guard
    entry appears before the worktree_scope_guard entry."""
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
    assert worktree_idx != -1, "worktree_scope_guard missing from PreToolUse"
    assert secret_idx < worktree_idx, (
        f"secret guard (idx {secret_idx}) must precede worktree guard (idx {worktree_idx})"
    )


def test_guard_ordering_worktree_matcher_shape(tmp_path):
    """AC6: worktree_scope_guard matcher includes Bash|Write|Edit|MultiEdit and
    secret guard also includes MultiEdit (#970 で追加済み)."""
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

    assert worktree_matcher is not None
    for tool in ("Bash", "Write", "Edit", "MultiEdit"):
        assert tool in worktree_matcher, f"{tool} missing from worktree matcher"
    # #970 にて secret_boundary_guard にも MultiEdit を追加済み。
    assert secret_matcher is not None
    assert "MultiEdit" in secret_matcher, (
        "secret_boundary_guard matcher must include MultiEdit (#970)"
    )


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

@pytest.mark.parametrize("command", [
    "git -C {root} commit -m oops",
    "cd {root} && git add .",
    "command git -C {root} push",
    "env FOO=bar git -C {root} commit -m x",
])
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

@pytest.mark.parametrize("command", [
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
])
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
    repo = _make_repo_with_worktree(tmp_path, issue="942",
                                    extra_worktrees=[("942", "dup")])
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
    """settings.json parses and references worktree_scope_guard in PreToolUse."""
    settings = json.loads(SETTINGS_JSON.read_text())
    pre = settings["hooks"]["PreToolUse"]
    found = any(
        "worktree_scope_guard" in h.get("command", "")
        for entry in pre for h in entry.get("hooks", [])
    )
    assert found, "worktree_scope_guard not wired in PreToolUse"


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

@pytest.mark.parametrize("command_tmpl", [
    "echo x > {root}/evil.txt",
    "echo x >> {root}/evil.txt",
    "cat foo | tee {root}/evil.txt",
    "sed -i 's/a/b/' {root}/README.md",
    "perl -i -pe 's/a/b/' {root}/README.md",
    "python3 -c \"open('{root}/evil.txt','w').write('x')\"",
    "node -e \"require('fs').writeFileSync('{root}/evil.txt','x')\"",
    "ruby -e \"File.write('{root}/evil.txt','x')\"",
])
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

@pytest.mark.parametrize("command_tmpl", [
    "bash -lc 'cd {root} && git add .'",
    "sh -c 'echo x > {root}/y'",
    "env FOO=bar bash -lc 'git -C {root} commit -m x'",
    "bash -c 'cd {root} && git commit -m x'",
    "zsh -c 'echo x > {root}/z'",
])
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

@pytest.mark.parametrize("command_tmpl", [
    "someformatter --output {root}/out.txt",
    "somegen --output={root}/out.txt",
    "somelint --fix {root}/x.py",
    "somefmt -o {root}/out.txt",
    "rewrite --in-place {root}/x.txt",
])
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
    for cmd in ("git status", "git diff HEAD~1", "gh pr view 1",
                "gh api -X GET repos/o/r"):
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

@pytest.mark.parametrize("command_tmpl", [
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
])
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


@pytest.mark.parametrize("command_tmpl", [
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
])
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


@pytest.mark.parametrize("command_tmpl", [
    "cp a.txt {root}/copied.txt",
    "mv a.txt {root}/moved.txt",
    "dd if=a.txt of={root}/dd.out",
    "install a.txt {root}/inst.txt",
    "cp -r . {root}/dircopy",
    # unknown program with a bare external abs path: cannot prove read vs write
    # at parse time → fail-closed (would otherwise re-open cp/mv/dd positional writes)
    "weirdtool {root}/target.bin",
])
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
    stderr_lines = [l for l in r.stderr.splitlines() if l.strip()]
    assert len(stderr_lines) <= 10, f"stderr must be ≤10 lines; got {len(stderr_lines)}"
    # No raw path/branch/command in stderr
    assert wt_path not in r.stderr, "stderr must not contain raw worktree path"
    assert f"git worktree remove" not in r.stderr, "stderr must not contain raw command"


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
