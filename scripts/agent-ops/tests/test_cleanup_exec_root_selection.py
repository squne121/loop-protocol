"""scripts/agent-ops/tests/test_cleanup_exec_root_selection.py

Issue #2979: ``cleanup_exec`` / ``materialize_cleanup_contract`` select the SAME canonical
primary root as ``guard_preflight`` when a post-merge cleanup is started from inside an
issue worktree, and the documented ``post-merge-cleanup-executor`` 8 steps are reachable
from such a session.

Every behavioural test drives the PUBLIC CLIs (``cleanup_exec.py``,
``materialize_cleanup_contract.py``, ``guard_preflight.py``) through a real ``subprocess``
against a real-Git fixture (primary root + linked issue worktree + local bare origin). Only
``gh`` is a fake on ``PATH``; ``git`` is real (a ``git`` shim is used solely as an explicit
fault-injection negative control). The AC8 command sequence is PARSED from
``.claude/skills/post-merge-cleanup-executor/SKILL.md`` -- never hand-copied. Deletion only
ever happens inside ``tmp_path`` fixtures.

Fixture helpers are shared with ``tests/agent_guards/test_guard_preflight_cli_root_selection.py``
and loaded under a UNIQUE module name (a bare import would collide in a shared pytest session).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HELPERS_PATH = _REPO_ROOT / "tests" / "agent_guards" / "test_guard_preflight_cli_root_selection.py"


def _load_helpers():
    import importlib.util

    name = "issue_2979_root_selection_helpers"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_HELPERS_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


H = _load_helpers()
ISSUE = H.ISSUE
PR_NUMBER = H.PR_NUMBER

CONTEXT_PREFIX_LINE = 'cd "<canonical root>" || exit 1'


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def _cleanup_script(root: Path) -> Path:
    return root / "scripts" / "agent-ops" / "cleanup_exec.py"


def _materialize_script(root: Path) -> Path:
    return root / "scripts" / "agent-ops" / "materialize_cleanup_contract.py"


def _cleanup_args(world, *, worktree: Path | None = None, branch: str | None = None) -> list[str]:
    return [
        "--pr-number", str(PR_NUMBER),
        "--linked-issue-number", str(ISSUE),
        "--worktree-path", str(worktree or world.worktree),
        "--branch-name", branch or world.branch,
        "--json",
    ]


def _run_json(script: Path, args: list[str], *, cwd: Path, env: dict) -> tuple[int, dict]:
    done = H.run_script(script, args, cwd=cwd, env=env)
    assert done.stdout.strip(), f"no stdout: rc={done.returncode} stderr={done.stderr!r}"
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def _branch_exists(world, branch: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(world.primary), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
    ).returncode == 0


def _documented_cleanup_exec_argv() -> list[str]:
    """argv mechanically extracted from the production ``cleanup_exec.py`` block of the executor Skill."""
    step3 = H.skill_section("3. worktree / branch を整理")
    block = H.block_containing(step3, "scripts/agent-ops/cleanup_exec.py")
    command = block.replace("\\\n", " ")
    command = command.replace("[--non-closing-authority-file <non_closing_authority_file>]", "")
    for placeholder, value in {
        "<pr>": str(PR_NUMBER),
        "<issue>": str(ISSUE),
        "<絶対 worktree path>": "@WORKTREE@",
        "<branch>": "@BRANCH@",
    }.items():
        command = command.replace(placeholder, value)
    tokens = shlex.split(command)
    assert tokens[:5] == ["uv", "run", "--locked", "python3", "scripts/agent-ops/cleanup_exec.py"], tokens[:5]
    return tokens[4:]  # relative script path + flags; run with the current interpreter


# ======================================================================================
# AC5
# ======================================================================================
def test_ac5_cleanup_exec_cli_from_issue_worktree_session_uses_the_canonical_root(tmp_path):
    world = H.build_world(tmp_path)
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    # preflight (same session) says the canonical root is fine ...
    _grc, pre, _ = H.run_guard(H.guard_script(world.worktree), cwd=world.worktree, env=env)
    assert (pre["status"], pre["root_branch_state"]) == ("ok", "default")
    # ... and the executor must agree instead of refusing with root_not_default_branch
    rc, out = _run_json(_cleanup_script(world.worktree), _cleanup_args(world), cwd=world.worktree, env=env)
    assert out["reason_code"] != "root_not_default_branch", out
    assert out["status"] == "ok" and rc == 0, out
    assert out["verified"]["root_default"] is True
    assert out["actions_taken"] == ["worktree_remove", "branch_delete"]
    assert not world.worktree.exists()
    assert not _branch_exists(world, world.branch)
    assert H.git("branch", "--show-current", cwd=world.primary) == "main"


def test_ac5_authorization_gates_after_root_selection_are_not_diverging_from_preflight(tmp_path):
    world = H.build_world(tmp_path)
    world.set_pr_state("OPEN")
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    _grc, pre, _ = H.run_guard(H.guard_script(world.worktree), cwd=world.worktree, env=env)
    assert pre["status"] == "ok"
    rc, out = _run_json(_cleanup_script(world.worktree), _cleanup_args(world), cwd=world.worktree, env=env)
    # refused for the PR state only -- never for the root
    assert (out["status"], out["reason_code"]) == ("refused", "pr_not_merged"), out
    assert out["verified"]["root_default"] is True and out["verified"]["worktree_in_catalog"] is True
    assert rc == 1 and world.worktree.exists()


# ======================================================================================
# AC6 -- cleanup authorization policy is not relaxed (public CLI pins; regression files stay green)
# ======================================================================================
def test_ac6_authorization_checks_still_refuse_through_the_public_cli(tmp_path):
    world = H.build_world(tmp_path)
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    script = _cleanup_script(world.worktree)

    # dirty target worktree is refused
    (world.worktree / "scratch.txt").write_text("dirty\n", encoding="utf-8")
    rc, out = _run_json(script, _cleanup_args(world), cwd=world.worktree, env=env)
    assert (out["status"], out["reason_code"]) == ("refused", "worktree_dirty"), out
    (world.worktree / "scratch.txt").unlink()

    # wrong branch for the worktree is refused
    _rc, out = _run_json(script, _cleanup_args(world, branch="issue-0-other"), cwd=world.worktree, env=env)
    assert (out["status"], out["reason_code"]) == ("refused", "worktree_branch_mismatch"), out

    # a genuinely drifted canonical root is still refused (policy unchanged)
    H.git("switch", "-q", "-c", "drifted-root-branch", cwd=world.primary)
    _rc, out = _run_json(script, _cleanup_args(world), cwd=world.worktree, env=env)
    assert (out["status"], out["reason_code"]) == ("refused", "root_not_default_branch"), out
    assert out["verified"]["root_default"] is False
    assert world.worktree.exists() and _branch_exists(world, world.branch)


# ======================================================================================
# AC7 -- the documented invocation, executed from the issue worktree (no project_root injection)
# ======================================================================================
def test_ac7_documented_cleanup_exec_command_runs_from_issue_worktree_session(tmp_path):
    world = H.build_world(tmp_path)
    tokens = _documented_cleanup_exec_argv()
    tokens = [str(world.worktree) if t == "@WORKTREE@" else world.branch if t == "@BRANCH@" else t for t in tokens]
    assert "--project-root" not in tokens
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    # relative script path -> the issue worktree's own tracked copy, exactly like the production block
    done = subprocess.run([sys.executable, *tokens], cwd=str(world.worktree), env=env, capture_output=True, text=True)
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["status"] == "ok" and done.returncode == 0, (out, done.stderr)
    assert out["actions_taken"] == ["worktree_remove", "branch_delete"]
    assert not world.worktree.exists()


# ======================================================================================
# AC8 -- full workflow reachability (command sequence parsed from SKILL.md)
# ======================================================================================
def _segments(world, steps: list[tuple[str, str, dict]], *, prefix: str) -> str:
    """One bash script: each step is a subshell preceded by the documented cwd-fixing line.

    The OUTER shell keeps the issue worktree as its cwd (a persistent shell started there);
    once ``cleanup_exec`` removes that worktree the outer cwd is an invalid, deleted path.
    """
    parts = [f'cd "{world.worktree}"']
    for name, block, values in steps:
        body = H.render(block, values)
        parts.append(f'echo "@@BEGIN {name}"')
        parts.append(f"(\n{prefix}\n{body}\n)")
        parts.append(f'echo "@@RC {name} $?"')
    return "\n".join(parts) + "\n"


def _split(stdout: str) -> tuple[dict[str, str], dict[str, int]]:
    text: dict[str, str] = {}
    rcs: dict[str, int] = {}
    current = None
    for line in stdout.splitlines():
        m = re.match(r"^@@BEGIN (\S+)$", line)
        if m:
            current = m.group(1)
            text[current] = ""
            continue
        m = re.match(r"^@@RC (\S+) (\d+)$", line)
        if m:
            rcs[m.group(1)] = int(m.group(2))
            current = None
            continue
        if current is not None:
            text[current] += line + "\n"
    return text, rcs


def _prefix_rendered(world) -> str:
    return H.render(H.cwd_prefix_block(), {"<canonical root>": str(world.primary)}).strip()


def _step_blocks() -> dict[str, str]:
    step1 = H.block_containing(H.skill_section("1. 未コミット変更"), "classify-git-state.py --format yaml")
    step2 = H.block_containing(H.skill_section("2. main を"), "guard_preflight.py --json")
    step3 = H.skill_section("3. worktree / branch を整理")
    return {
        "step1": step1,
        "step2": step2,
        "step3_guard": H.block_containing(step3, "guard_preflight.py --json"),
        "step3_exec": H.block_containing(step3, "scripts/agent-ops/cleanup_exec.py"),
        "step4": H.block_containing(H.skill_section("4. parent issue"), "/parent"),
        "step5": H.block_containing(H.skill_section("5. Superseded PR"), "closedByPullRequestsReferences"),
        "step7_list": H.block_containing(H.skill_section("7. Stash"), "git stash list"),
    }


def _values(world) -> dict[str, str]:
    return {
        "<canonical root>": str(world.primary),
        "<pr>": str(PR_NUMBER),
        "<issue>": str(ISSUE),
        "<絶対 worktree path>": str(world.worktree),
        "<branch>": world.branch,
        "[--non-closing-authority-file <non_closing_authority_file>]": "",
    }


def _advance_origin(world) -> str:
    """Push a new commit to the local bare origin from a second clone (primary must fast-forward)."""
    other = world.tmp / "other-clone"
    H.git("clone", "-q", str(world.origin), str(other), cwd=world.tmp)
    (other / "upstream.txt").write_text("upstream\n", encoding="utf-8")
    H.git("add", "upstream.txt", cwd=other)
    H.git("commit", "-q", "-m", "upstream change", cwd=other)
    H.git("push", "-q", "origin", "main", cwd=other)
    return H.git("rev-parse", "HEAD", cwd=other)


def test_ac8_canonical_root_comes_from_git_worktree_probe_entries_first(tmp_path):
    world = H.build_world(tmp_path)
    ctx = H.skill_section("実行コンテキスト")
    probe = H.block_containing(ctx, "git_worktree_probe.py --json")
    assert H.bash_blocks(ctx)[1].strip() == CONTEXT_PREFIX_LINE
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    done = H.run_bash(H.render(probe), cwd=world.worktree, env=env)
    payload = json.loads(done.stdout)
    assert done.returncode == 0 and payload["entries"][0]["worktree_realpath"] == str(world.primary)
    assert str(world.worktree) in [e["worktree_realpath"] for e in payload["entries"][1:]]
    assert "entries[0].worktree_realpath" in ctx


def test_ac8_documented_eight_step_sequence_is_reachable_from_an_issue_worktree_session(tmp_path):
    world = H.build_world(tmp_path)
    upstream_tip = _advance_origin(world)
    (world.primary / "NOTES.txt").write_text("root-owned staged change\n", encoding="utf-8")
    H.git("add", "NOTES.txt", cwd=world.primary)  # the canonical root's OWN staged change
    before_status = H.git("status", "--porcelain", cwd=world.primary)
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    env["linked_issue"] = str(ISSUE)

    blocks = _step_blocks()
    order = ["step1", "step2", "step3_guard", "step3_exec", "step4", "step5", "step7_list"]
    script = _segments(world, [(n, blocks[n], _values(world)) for n in order], prefix=_prefix_rendered(world))
    done = H.run_bash(script, cwd=world.worktree, env=env)
    text, rcs = _split(done.stdout)

    assert [n for n in order if rcs.get(n) != 0] == [], (rcs, done.stderr[-800:])
    assert "branches" in text["step1"]  # classify-git-state output (YAML)
    # Step 2: synced the canonical root only (fast-forward to origin); no stash is ever created
    assert H.git("rev-parse", "HEAD", cwd=world.primary) == upstream_tip
    assert "stash@{" not in text["step7_list"]
    # Step 3: preflight ok from the canonical root, then cleanup_exec removed the SESSION's worktree
    assert json.loads(text["step3_guard"].strip().splitlines()[-1])["root_branch_state"] == "default"
    cleanup = json.loads(text["step3_exec"].strip().splitlines()[-1])
    assert cleanup["status"] == "ok" and cleanup["actions_taken"] == ["worktree_remove", "branch_delete"], cleanup
    assert not world.worktree.exists()
    assert not _branch_exists(world, world.branch)
    # the canonical root's own staged change survived exactly (still STAGED, not unstaged)
    assert H.git("status", "--porcelain", cwd=world.primary) == before_status == "A  NOTES.txt"
    assert H.git("diff", "--cached", "--name-only", cwd=world.primary) == "NOTES.txt"
    assert H.git("stash", "list", cwd=world.primary) == ""
    assert H.git("branch", "--show-current", cwd=world.primary) == "main"


def test_ac8_deleted_session_cwd_negative_control_and_canonical_root_positive(tmp_path):
    world = H.build_world(tmp_path)
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    blocks = _step_blocks()
    values = _values(world)
    prefix = _prefix_rendered(world)
    exec_body = H.render(blocks["step3_exec"], values)
    step1_body = H.render(blocks["step1"], values)
    script = "\n".join([
        f'cd "{world.worktree}"',
        f"(\n{prefix}\n{exec_body}\n)",          # Step 3: removes the session's own worktree
        'echo "@@EXEC_RC $?"',
        'if [ -d "$PWD" ]; then echo "@@CWD still-valid"; else echo "@@CWD deleted"; fi',
        step1_body,                                # negative control: same command, deleted cwd, no prefix
        'echo "@@NEG_RC $?"',
        f"(\n{prefix}\n{step1_body}\n)",         # positive: documented prefix -> canonical root cwd
        'echo "@@POS_RC $?"',
    ]) + "\n"
    done = H.run_bash(script, cwd=world.worktree, env=env)
    out = done.stdout
    assert "@@EXEC_RC 0" in out and not world.worktree.exists()
    assert "@@CWD deleted" in out
    neg = int(re.search(r"@@NEG_RC (\d+)", out).group(1))
    pos = int(re.search(r"@@POS_RC (\d+)", out).group(1))
    assert neg != 0, "the relative Step command must fail from the deleted cwd"
    assert pos == 0, "the same command must succeed with the canonical root as cwd"
    assert "branches" in out.split("@@NEG_RC")[1]  # the positive run produced the classification output


def test_ac8_main_sync_is_gated_on_default_root_and_never_runs_in_a_linked_worktree(tmp_path):
    world = H.build_world(tmp_path)
    block = _step_blocks()["step2"]
    assert "git checkout" not in block  # no checkout at all (drifted primaries are not switched)
    assert block.index("guard_preflight.py --json") < block.index("git pull --ff-only origin main")
    assert "root_branch_state" in block

    # the OLD Step 2 (checkout main inside the issue worktree) is unreachable: the primary owns main
    old = subprocess.run(["git", "checkout", "main"], cwd=str(world.worktree), capture_output=True, text=True)
    assert old.returncode == 128 and "main" in old.stderr and "already" in old.stderr, old

    # drift: no sync, human_required, drifted primary left untouched
    upstream_tip = _advance_origin(world)
    H.git("switch", "-q", "-c", "drifted-root-branch", cwd=world.primary)
    before = H.git("rev-parse", "HEAD", cwd=world.primary)
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    prefix = _prefix_rendered(world)
    script = f"(\n{prefix}\n{H.render(block, _values(world))}\n)\necho \"@@RC $?\"\n"
    done = H.run_bash(script, cwd=world.worktree, env=env)
    assert "@@RC 2" in done.stdout, (done.stdout, done.stderr)
    assert "[STOP]" in done.stderr
    assert H.git("branch", "--show-current", cwd=world.primary) == "drifted-root-branch"
    assert H.git("rev-parse", "HEAD", cwd=world.primary) == before != upstream_tip  # no pull happened
    assert H.git("stash", "list", cwd=world.primary) == ""
    # the preflight of the same session reports the drift as human_required
    _rc, pre, _ = H.run_guard(H.guard_script(world.worktree), cwd=world.worktree, env=env)
    assert pre["status"] == "human_required" and "root_drift_active_worktree_mismatch" in pre["blocked_reason_codes"]


def test_ac8_step2_never_stashes_and_never_carries_worktree_changes(tmp_path):
    world = H.build_world(tmp_path)
    (world.worktree / "wt_only.txt").write_text("issue worktree work\n", encoding="utf-8")
    H.git("add", "wt_only.txt", cwd=world.worktree)  # staged in the ISSUE worktree
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    prefix = _prefix_rendered(world)
    block = H.render(_step_blocks()["step2"], _values(world))
    done = H.run_bash(f"(\n{prefix}\n{block}\n)\necho \"@@RC $?\"\n", cwd=world.worktree, env=env)
    assert "@@RC 0" in done.stdout, (done.stdout, done.stderr)
    assert "wt_only.txt" not in H.git("status", "--porcelain", cwd=world.primary)
    assert H.git("stash", "list", cwd=world.primary) == ""  # nothing of the worktree was stashed
    assert H.git("diff", "--cached", "--name-only", cwd=world.worktree) == "wt_only.txt"  # worktree untouched
    assert H.git("branch", "--show-current", cwd=world.worktree) == world.branch


def test_ac8_steps_6_and_8_have_no_relative_references_that_depend_on_the_deleted_cwd():
    rel_ref = re.compile(r"(?<![\w/.\-])(?:\./|\.\./|scripts/|\.claude/|docs/|artifacts/|tmp/|schemas/)[\w./#\-]*")
    for prefix in ("6. ", "6a. ", "8. "):
        section = H.skill_section(prefix)
        prose = re.sub(r"```.*?```", "", section, flags=re.DOTALL)
        assert rel_ref.findall(prose) == [], (prefix, rel_ref.findall(prose))
        for block in H.bash_blocks(section):
            if rel_ref.search(block):
                # a command that uses repo-relative paths must start from the documented cwd-fixing line
                assert block.strip().splitlines()[0] == CONTEXT_PREFIX_LINE, (prefix, block)
    assert H.bash_blocks(H.skill_section("6a. ")), "step 6a keeps its documented command"


def test_ac8_cleanup_exec_command_block_stays_a_single_relative_path_block():
    text = H.skill_text()
    blocks = [b for b in H.bash_blocks(text) if "scripts/agent-ops/cleanup_exec.py" in b]
    assert len(blocks) == 1
    assert blocks[0].lstrip().startswith("uv run --locked python3 scripts/agent-ops/cleanup_exec.py")
    context_blocks = H.bash_blocks(H.skill_section("実行コンテキスト"))
    assert context_blocks and all("scripts/agent-ops/cleanup_exec.py" not in b for b in context_blocks)
    assert "scripts/agent-ops/cleanup_exec.py" not in H.skill_section("2. main を")


# --------------------------------------------------------------------------------------
# Finding 1/4 (PR #2990 owner review): Step 2 must preserve the canonical root's index,
# working tree, untracked files and any pre-existing stash exactly.
# --------------------------------------------------------------------------------------
def _root_snapshot(world) -> dict:
    def g(*a):
        return H.git(*a, cwd=world.primary)

    return {
        "porcelain": g("status", "--porcelain"),
        "cached": g("diff", "--cached"),
        "worktree": g("diff"),
        "stash": g("stash", "list"),
        "head": g("rev-parse", "HEAD"),
    }


def _run_step2(world) -> subprocess.CompletedProcess:
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    block = H.render(_step_blocks()["step2"], _values(world))
    script = f"(\n{_prefix_rendered(world)}\n{block}\n)\necho \"@@RC $?\"\n"
    return H.run_bash(script, cwd=world.worktree, env=env)


def _tracked_file(world) -> Path:
    return world.primary / H.git("ls-files", cwd=world.primary).splitlines()[0]


def _dirty_root(world, kind: str) -> None:
    tracked = _tracked_file(world)
    base = tracked.read_text(encoding="utf-8")
    if kind in ("staged", "mixed"):
        tracked.write_text("STAGED\n" + base, encoding="utf-8")
        H.git("add", str(tracked.relative_to(world.primary)), cwd=world.primary)
    if kind in ("unstaged", "mixed"):
        tracked.write_text(tracked.read_text(encoding="utf-8") + "UNSTAGED\n", encoding="utf-8")
    (world.primary / "untracked_note.txt").write_text("untracked\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["staged", "unstaged", "mixed", "untracked"])
def test_finding1_step2_preserves_index_worktree_untracked_exactly(tmp_path, kind):
    world = H.build_world(tmp_path)
    _advance_origin(world)  # independent remote update (touches only upstream.txt)
    _dirty_root(world, kind)
    before = _root_snapshot(world)
    done = _run_step2(world)
    after = _root_snapshot(world)
    assert "@@RC 0" in done.stdout, (done.stdout, done.stderr)
    assert after["head"] != before["head"]  # fast-forwarded
    for key in ("porcelain", "cached", "worktree", "stash"):
        assert after[key] == before[key], (kind, key, before[key], after[key])
    if kind == "mixed":  # the same file is both staged AND unstaged -- must stay MM
        assert any(line.startswith("MM ") for line in after["porcelain"].splitlines())


def test_finding1_step2_leaves_preexisting_stash_untouched(tmp_path):
    world = H.build_world(tmp_path)
    (world.primary / "other.txt").write_text("someone else's work\n", encoding="utf-8")
    H.git("add", "other.txt", cwd=world.primary)
    H.git("stash", "push", "-m", "foreign-stash", cwd=world.primary)
    _advance_origin(world)
    before = _root_snapshot(world)
    done = _run_step2(world)
    after = _root_snapshot(world)
    assert "@@RC 0" in done.stdout, (done.stdout, done.stderr)
    assert after["stash"] == before["stash"] and "foreign-stash" in after["stash"]
    assert after["porcelain"] == before["porcelain"] == ""


def test_finding1_step2_conflicting_pull_stops_and_leaves_every_change_in_place(tmp_path):
    world = H.build_world(tmp_path)
    other = world.tmp / "other-clone"
    H.git("clone", "-q", str(world.origin), str(other), cwd=world.tmp)
    tracked = H.git("ls-files", cwd=other).splitlines()[0]
    (other / tracked).write_text("upstream rewrite\n", encoding="utf-8")
    H.git("commit", "-q", "-am", "conflicting upstream", cwd=other)
    H.git("push", "-q", "origin", "main", cwd=other)
    (world.primary / tracked).write_text("local edit that collides\n", encoding="utf-8")
    H.git("add", tracked, cwd=world.primary)
    before = _root_snapshot(world)
    done = _run_step2(world)
    assert "@@RC 3" in done.stdout and "[STOP]" in done.stderr, (done.stdout, done.stderr)
    assert _root_snapshot(world) == before  # nothing dropped, nothing popped, nothing merged


def test_finding1_skill_has_no_stash_mutation_command():
    for block in H.bash_blocks(H.skill_text()):
        for line in block.splitlines():
            assert not re.match(r"\s*git stash(\s+(pop|apply|drop|push|save))?\s*$", line), line


# --------------------------------------------------------------------------------------
# Finding 3: the Step 3 guard is bound to the cleanup TARGET issue even though cwd is the
# canonical root (which carries no Issue identity of its own).
# --------------------------------------------------------------------------------------
def test_finding3_step3_guard_binds_target_issue_from_canonical_root_cwd(tmp_path):
    world = H.build_world(tmp_path)
    block = _step_blocks()["step3_guard"]
    assert "LOOP_ISSUE_NUMBER=<issue>" in block
    env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    env.pop("LOOP_ISSUE_NUMBER", None)
    script = f"(\n{_prefix_rendered(world)}\n{H.render(block, _values(world))}\n)\n"
    done = H.run_bash(script, cwd=world.worktree, env=env)
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["status"] == "ok" and out["active_worktree_state"] == "matches", out
    assert out["resolved_worktree"]["worktree_realpath"] == str(world.worktree)
    # negative control: without the binding, the canonical-root cwd identifies no Issue
    unbound = H.render(block.replace("LOOP_ISSUE_NUMBER=<issue> ", ""), _values(world))
    done2 = H.run_bash(f"(\n{_prefix_rendered(world)}\n{unbound}\n)\n", cwd=world.worktree, env=env)
    out2 = json.loads(done2.stdout.strip().splitlines()[-1])
    assert out2["resolved_worktree"]["worktree_realpath"] != str(world.worktree), out2


# --------------------------------------------------------------------------------------
# Finding 5: the production probe command runs UNMODIFIED (real ``uv run --locked``).
# --------------------------------------------------------------------------------------
def test_finding5_production_probe_command_runs_unmodified_with_real_uv():
    if shutil.which("uv") is None:
        pytest.skip("uv not installed in this environment")
    probe = H.block_containing(H.skill_section("実行コンテキスト"), "git_worktree_probe.py --json")
    assert probe.strip().startswith("uv run --locked python3 scripts/agent-ops/git_worktree_probe.py")
    done = subprocess.run(["bash", "-c", probe], cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stderr[-500:]
    entries = json.loads(done.stdout)["entries"]
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            cwd=str(_REPO_ROOT), capture_output=True, text=True).stdout.strip()
    assert Path(entries[0]["worktree_realpath"]).resolve() == Path(common).parent.resolve()


# ======================================================================================
# AC10 (executor / materialize lanes)
# ======================================================================================
def _failure_cases(world, tmp_path):
    foreign = H.make_foreign_repo(tmp_path)
    (tmp_path / "bare-world").mkdir()
    bare_linked = H.make_bare_primary_world(tmp_path / "bare-world")
    (tmp_path / "plain").mkdir()
    return [
        ("worktree_catalog_unavailable", world.worktree, dict(shim="catalog_fail")),
        ("git_common_dir_unavailable", world.worktree, dict(shim="common_dir_fail")),
        ("repository_identity_mismatch", world.worktree, dict(shim="foreign_primary", foreign=foreign)),
        ("primary_root_unresolved", bare_linked, {}),
        ("primary_root_unresolved", tmp_path / "plain", {}),
    ]


def test_ac10_executor_and_materialize_return_structured_failure_for_each_root_resolution_failure(tmp_path):
    world = H.build_world(tmp_path)
    artifact = world.primary / "artifacts" / "agent-ops" / "cleanup_contract.json"
    for code, project_dir, kw in _failure_cases(world, tmp_path):
        env = H.session_env(world, project_dir=project_dir, cwd=world.worktree, **kw)
        # cleanup_exec (public CLI): refusal JSON, non-success, nothing deleted
        rc, out = _run_json(_cleanup_script(world.worktree), _cleanup_args(world), cwd=world.worktree, env=env)
        assert (out["status"], out["reason_code"]) == ("refused", code), (code, out)
        assert rc == 1 and out["actions_taken"] == []
        assert world.worktree.exists() and _branch_exists(world, world.branch)
        # materialize_cleanup_contract (public CLI, default verify path; the script itself is unmodified)
        rc, mat = _run_json(
            _materialize_script(world.worktree),
            ["--pr-number", str(PR_NUMBER), "--linked-issue-number", str(ISSUE),
             "--worktree-path", str(world.worktree), "--branch-name", world.branch, "--json"],
            cwd=world.worktree, env=env,
        )
        assert mat == {"status": "refused", "reason_code": code}, (code, mat)
        assert rc == 1 and not artifact.exists()
        # discard lane entry points (run_discard_check / run_discard_consume)
        base = ["--pr-number", str(PR_NUMBER), "--worktree-path", str(world.worktree),
                "--branch-name", world.branch, "--operation", "local_only_discard", "--json"]
        rc, chk = _run_json(_materialize_script(world.worktree), [*base, "--check"], cwd=world.worktree, env=env)
        assert (chk["status"], chk["reason_code"]) == ("refused", code), (code, chk)
        assert rc == 1
        rc, cons = _run_json(
            _materialize_script(world.worktree),
            [*base, "--consume", "--contract-id", "a" * 32, "--expected-contract-sha256", "b" * 64],
            cwd=world.worktree, env=env,
        )
        assert (cons["status"], cons["reason_code"]) == ("refused", code), (code, cons)
        assert rc == 1


def test_ac10_resolve_project_root_is_best_effort_and_never_raises(tmp_path):
    world = H.build_world(tmp_path)
    code = (
        "import sys; sys.path.insert(0, %r); import cleanup_exec; print(cleanup_exec.resolve_project_root())"
        % str(H.AGENT_OPS)
    )
    for project_dir, kw in [
        (world.worktree, {}),
        (tmp_path / "does-not-exist", {}),
        (world.worktree, dict(shim="catalog_fail")),
    ]:
        env = H.session_env(world, project_dir=project_dir, cwd=world.worktree, **kw)
        done = subprocess.run([sys.executable, "-c", code], cwd=str(world.worktree), env=env,
                              capture_output=True, text=True)
        assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr
        assert done.stdout.strip()
    ok_env = H.session_env(world, project_dir=world.worktree, cwd=world.worktree)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=str(world.worktree), env=ok_env, capture_output=True, text=True
    )
    assert done.stdout.strip() == str(world.primary)  # normalised to the canonical root, not the worktree


# ======================================================================================
# AC11 (executor / materialize lanes)
# ======================================================================================
def _mode_envs(world):
    return {
        "a_primary": (H.session_env(world, project_dir=world.primary, cwd=world.worktree), world.primary),
        "b_worktree": (H.session_env(world, project_dir=world.worktree, cwd=world.worktree), world.worktree),
        "c_script_location": (H.session_env(world, project_dir=None, cwd=world.worktree), world.worktree),
    }


def test_ac11_cleanup_exec_evaluates_the_same_canonical_root_for_primary_worktree_and_script_location(tmp_path):
    world = H.build_world(tmp_path)
    world.set_pr_state("OPEN")  # non-destructive: refusal JSON exposes which root/catalog was evaluated
    views = {}
    for name, (env, script_root) in _mode_envs(world).items():
        assert ("CLAUDE_PROJECT_DIR" in env) == (name != "c_script_location")
        rc, out = _run_json(_cleanup_script(script_root), _cleanup_args(world), cwd=world.worktree, env=env)
        v = out["verified"]
        views[name] = (out["status"], out["reason_code"], v["root_default"], v["worktree_in_catalog"],
                       v["branch_match"], v["worktree_clean"])
    assert views["a_primary"] == views["b_worktree"] == views["c_script_location"], views
    assert views["a_primary"] == ("refused", "pr_not_merged", True, True, True, True)


def test_ac11_materialize_writes_its_contract_under_the_same_canonical_root_in_every_mode(tmp_path):
    world = H.build_world(tmp_path)
    artifact = world.primary / "artifacts" / "agent-ops" / "cleanup_contract.json"
    for name, (env, script_root) in _mode_envs(world).items():
        if artifact.exists():
            artifact.unlink()
        rc, out = _run_json(
            _materialize_script(script_root),
            ["--pr-number", str(PR_NUMBER), "--linked-issue-number", str(ISSUE),
             "--worktree-path", str(world.worktree), "--branch-name", world.branch, "--json"],
            cwd=world.worktree, env=env,
        )
        assert out["status"] == "ok" and rc == 0, (name, out)
        assert artifact.is_file(), name
        assert not (world.worktree / "artifacts").exists(), name  # never the issue worktree as root
    # guard_preflight in the same session lists the contract that materialize wrote (same root)
    env, script_root = _mode_envs(world)["b_worktree"]
    _rc, pre, _ = H.run_guard(H.guard_script(script_root), cwd=world.worktree, env=env)
    assert pre["cleanup_contract_state"] == "valid_v3", pre


# ======================================================================================
# AC12 -- collect-only evidence per AC
# ======================================================================================
def test_ac12_collect_only_reports_at_least_one_test_per_acceptance_criterion():
    files = [str(_HELPERS_PATH), str(Path(__file__).resolve())]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=240,
    )
    assert done.returncode == 0, done.stdout[-600:] + done.stderr[-600:]
    ids = [line for line in done.stdout.splitlines() if "::test_" in line]
    counts = {n: len([i for i in ids if re.search(rf"::test_ac{n}_", i)]) for n in range(1, 13)}
    assert all(c >= 1 for c in counts.values()), counts
    assert counts[12] >= 1
