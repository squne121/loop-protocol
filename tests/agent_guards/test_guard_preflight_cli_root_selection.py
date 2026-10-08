"""tests/agent_guards/test_guard_preflight_cli_root_selection.py

Issue #2979: ``guard_preflight`` / ``cleanup_exec`` must select the CANONICAL primary
repository root from Git identity instead of trusting the session's current worktree
(``CLAUDE_PROJECT_DIR`` / script location may both point at an issue worktree).

Every behavioural test drives the PUBLIC CLI (``guard_preflight.py --json``) through a
real ``subprocess`` against a real-Git fixture (primary root + linked issue worktree +
local bare origin). ``project_root`` injection is never the only proof. Only ``gh`` is a
fake on ``PATH``; ``git`` is real. A ``git`` shim exists ONLY as an explicit
fault-injection negative control (catalog failure / common-dir failure / foreign primary).

This module also hosts the fixture helpers that
``scripts/agent-ops/tests/test_cleanup_exec_root_selection.py`` loads under a unique
module name (no ``sys.modules`` collision with a bare ``import``).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_OPS = REPO_ROOT / "scripts" / "agent-ops"
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "post-merge-cleanup-executor" / "SKILL.md"
THIS_FILE = Path(__file__).resolve()
CLEANUP_TEST_FILE = REPO_ROOT / "scripts" / "agent-ops" / "tests" / "test_cleanup_exec_root_selection.py"

ISSUE = 2979
SLUG = "root-selection"
PR_NUMBER = 3001
REPO_SLUG = "squne121/loop-protocol"

# The script set the fixture repository tracks, so a linked worktree contains its own copy
# (AC11 (c): CLAUDE_PROJECT_DIR unset + script located inside the issue worktree).
SCRIPT_FILES = (
    "scripts/agent-ops/guard_preflight.py",
    "scripts/agent-ops/cleanup_exec.py",
    "scripts/agent-ops/worktree_catalog.py",
    "scripts/agent-ops/cleanup_contract_v3.py",
    "scripts/agent-ops/materialize_cleanup_contract.py",
    "scripts/agent-ops/git_worktree_probe.py",
    ".claude/skills/create-issue/scripts/mrc_contract_parser.py",
    ".claude/skills/post-merge-cleanup/scripts/classify-git-state.py",
)


def load_module(unique_name: str, path: Path):
    """Load ``path`` under a UNIQUE module name (bare imports collide in a shared pytest session)."""
    spec = importlib.util.spec_from_file_location(unique_name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


wc = load_module("worktree_catalog_issue_2979_under_test", AGENT_OPS / "worktree_catalog.py")


# --------------------------------------------------------------------------------------
# real-Git fixture
# --------------------------------------------------------------------------------------
def _base_env() -> dict:
    env = dict(os.environ)
    for key in ("CLAUDE_PROJECT_DIR", "LOOP_ISSUE_NUMBER", "LOOP_DEFAULT_BRANCH", "CLAUDE_WORKTREE_CLEANUP_CONTRACT"):
        env.pop(key, None)
    env.update(
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def git(*args: str, cwd: Path, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env or _base_env())
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args} failed in {cwd}: {result.stderr}")
    return result.stdout.strip()


FAKE_GH = """#!{python}
import json
import os
import sys

with open(os.environ["FAKE_GH_DATA"], "r", encoding="utf-8") as fh:
    data = json.load(fh)
argv = sys.argv[1:]
if argv[:2] == ["repo", "view"]:
    print(data["repo"])
elif argv[:2] == ["pr", "view"]:
    print(json.dumps(data["pr"]))
elif argv[:2] == ["issue", "view"]:
    print(json.dumps(data.get("issue", {{}})))
elif argv[:1] == ["api"]:
    print(data.get("api", "1"))
else:
    sys.stderr.write("fake gh: unsupported invocation\\n")
    raise SystemExit(1)
"""

GIT_SHIM = """#!{python}
import os
import subprocess
import sys

REAL = os.environ["SHIM_REAL_GIT"]
MODE = os.environ.get("SHIM_MODE", "")
args = sys.argv[1:]
if MODE == "catalog_fail" and "worktree" in args and "list" in args:
    sys.stderr.write("git shim: injected worktree list failure\\n")
    raise SystemExit(128)
if MODE == "common_dir_fail" and "--git-common-dir" in args:
    sys.stderr.write("git shim: injected git-common-dir failure\\n")
    raise SystemExit(128)
if MODE == "foreign_primary" and "worktree" in args and "list" in args:
    proc = subprocess.run([REAL, *args], capture_output=True)
    fields = proc.stdout.split(b"\\0")
    if fields and fields[0].startswith(b"worktree "):
        fields[0] = b"worktree " + os.environ["SHIM_FOREIGN"].encode()
    sys.stdout.buffer.write(b"\\0".join(fields))
    raise SystemExit(proc.returncode)
os.execv(REAL, [REAL, *args])
"""


@dataclass
class World:
    tmp: Path
    origin: Path
    primary: Path
    worktree: Path
    branch: str
    tip: str
    bin_dir: Path
    gh_data: Path
    env: dict = field(default_factory=dict)

    def pr_json(self, *, state: str = "MERGED") -> dict:
        return {
            "state": state,
            "mergedAt": "2026-01-01T00:00:00Z" if state == "MERGED" else None,
            "headRefName": self.branch,
            "headRefOid": self.tip,
            "baseRefName": "main",
            "isCrossRepository": False,
            "headRepositoryOwner": {"login": "squne121"},
            "closingIssuesReferences": [{"number": ISSUE}],
            "mergeCommit": {"oid": self.tip} if state == "MERGED" else None,
            "body": "Closes #%d\n" % ISSUE,
        }

    def set_pr_state(self, state: str) -> None:
        data = json.loads(self.gh_data.read_text(encoding="utf-8"))
        data["pr"] = self.pr_json(state=state)
        self.gh_data.write_text(json.dumps(data), encoding="utf-8")


def build_world(tmp_path: Path, *, issue: int = ISSUE, slug: str = SLUG) -> World:
    """Primary root on ``main`` (clean) + linked issue worktree + local bare origin."""
    env = _base_env()
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=origin)

    primary = tmp_path / "primary"
    primary.mkdir()
    git("init", "-q", "-b", "main", cwd=primary)
    (primary / ".gitignore").write_text(".claude/worktrees/\nartifacts/\n__pycache__/\n", encoding="utf-8")
    (primary / "README.md").write_text("seed\n", encoding="utf-8")
    for rel in SCRIPT_FILES:
        dest = primary / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    git("add", "-A", cwd=primary)
    git("commit", "-q", "-m", "seed", cwd=primary)
    git("remote", "add", "origin", str(origin), cwd=primary)
    git("push", "-q", "-u", "origin", "main", cwd=primary)

    branch = f"worktree-issue-{issue}-{slug}"
    git("checkout", "-q", "-b", branch, cwd=primary)
    (primary / "feature.txt").write_text("feature\n", encoding="utf-8")
    git("add", "feature.txt", cwd=primary)
    git("commit", "-q", "-m", "feature", cwd=primary)
    tip = git("rev-parse", "HEAD", cwd=primary)
    git("checkout", "-q", "main", cwd=primary)
    git("merge", "-q", "--ff-only", branch, cwd=primary)
    git("push", "-q", "origin", "main", cwd=primary)

    worktree = primary / ".claude" / "worktrees" / f"issue-{issue}-{slug}"
    git("worktree", "add", "-q", str(worktree), branch, cwd=primary)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH.format(python=sys.executable), encoding="utf-8")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
    gh_data = tmp_path / "gh_data.json"

    world = World(tmp_path, origin, primary.resolve(), worktree.resolve(), branch, tip, bin_dir, gh_data, env)
    gh_data.write_text(json.dumps({"repo": REPO_SLUG, "pr": world.pr_json()}), encoding="utf-8")
    return world


def session_env(
    world: World,
    *,
    project_dir: Path | str | None,
    cwd: Path,
    issue: str | None = None,
    shim: str | None = None,
    foreign: Path | None = None,
) -> dict:
    """Environment of a Claude Code session: ``CLAUDE_PROJECT_DIR`` fixed at session start."""
    env = _base_env()
    env["PWD"] = str(cwd)
    path_parts = [str(world.bin_dir), env.get("PATH", "")]
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    if issue is not None:
        env["LOOP_ISSUE_NUMBER"] = issue
    env["FAKE_GH_DATA"] = str(world.gh_data)
    if shim:
        shim_dir = world.tmp / "gitshim"
        shim_dir.mkdir(exist_ok=True)
        shim_path = shim_dir / "git"
        shim_path.write_text(GIT_SHIM.format(python=sys.executable), encoding="utf-8")
        shim_path.chmod(shim_path.stat().st_mode | stat.S_IXUSR)
        env["SHIM_REAL_GIT"] = shutil.which("git") or "git"
        env["SHIM_MODE"] = shim
        if foreign is not None:
            env["SHIM_FOREIGN"] = str(foreign)
        path_parts.insert(0, str(shim_dir))
    env["PATH"] = os.pathsep.join(path_parts)
    return env


def make_foreign_repo(tmp_path: Path) -> Path:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git("init", "-q", "-b", "main", cwd=foreign)
    (foreign / "x.txt").write_text("x\n", encoding="utf-8")
    git("add", "x.txt", cwd=foreign)
    git("commit", "-q", "-m", "foreign", cwd=foreign)
    return foreign.resolve()


def make_bare_primary_world(tmp_path: Path) -> Path:
    """A bare repository whose only work tree is a linked worktree (primary has NO work tree)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a\n", encoding="utf-8")
    git("add", "a.txt", cwd=seed)
    git("commit", "-q", "-m", "seed", cwd=seed)
    bare = tmp_path / "bare.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    linked = tmp_path / "bare-linked"
    git("worktree", "add", "-q", "-b", f"worktree-issue-{ISSUE}-bare", str(linked), cwd=bare)
    return linked.resolve()


def run_script(script: Path, args: list[str], *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120
    )


def run_guard(world_or_script: Path, *, cwd: Path, env: dict, extra: list[str] | None = None):
    """Run the PUBLIC ``guard_preflight.py --json`` CLI; return ``(returncode, parsed JSON, completed)``."""
    done = run_script(world_or_script, ["--json", *(extra or [])], cwd=cwd, env=env)
    assert done.stdout.strip(), f"no stdout: rc={done.returncode} stderr={done.stderr!r}"
    return done.returncode, json.loads(done.stdout), done


def guard_script(root: Path) -> Path:
    return root / "scripts" / "agent-ops" / "guard_preflight.py"


# --------------------------------------------------------------------------------------
# SKILL.md parsing (the test and the documented procedure share ONE command sequence)
# --------------------------------------------------------------------------------------
def skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def skill_section(prefix: str) -> str:
    """Text of the ``### <prefix>...`` section up to the next ``##`` / ``###`` heading."""
    lines = skill_text().splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("### " + prefix):
            inside = True
            out.append(line)
            continue
        if inside and (line.startswith("### ") or line.startswith("## ")):
            break
        if inside:
            out.append(line)
    assert out, f"SKILL.md section not found: {prefix!r}"
    return "\n".join(out)


def bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)


def block_containing(text: str, needle: str) -> str:
    matches = [b for b in bash_blocks(text) if needle in b]
    assert len(matches) == 1, (needle, len(matches))
    return matches[0]


def cwd_prefix_block() -> str:
    """The documented cwd-fixing line (execution context section)."""
    return block_containing(skill_section("実行コンテキスト"), 'cd "<canonical root>"')


def render(block: str, values: dict[str, str] | None = None) -> str:
    """Substitute documented placeholders; run ``uv run`` forms with the current interpreter."""
    text = block.replace("\\\n", "\\\n")
    py = shlex.quote(sys.executable)
    text = text.replace("uv run --locked python3", py).replace("uv run python3", py)
    for placeholder, value in (values or {}).items():
        text = text.replace(placeholder, value)
    return text


def run_bash(script: str, *, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=240)


# ======================================================================================
# AC1
# ======================================================================================
def test_ac1_cli_from_issue_worktree_with_project_dir_at_worktree_resolves_canonical_root(tmp_path):
    world = build_world(tmp_path)
    env = session_env(world, project_dir=world.worktree, cwd=world.worktree)
    rc, out, done = run_guard(guard_script(world.worktree), cwd=world.worktree, env=env)
    assert out["root_branch_state"] == "default", out
    assert out["active_worktree_state"] == "matches", out
    assert out["status"] == "ok", out
    assert out["blocked_reason_codes"] == []
    assert rc == 0, done.stderr
    resolved = out["resolved_worktree"]
    assert resolved["issue_number"] == ISSUE
    assert resolved["worktree_realpath"] == str(world.worktree)
    assert resolved["cwd_classification"] == "inside_worktree"


# ======================================================================================
# AC2
# ======================================================================================
@pytest.mark.parametrize("drift", ["other_branch", "detached"])
def test_ac2_canonical_root_drift_stays_fail_closed_and_is_never_judged_by_the_issue_worktree(tmp_path, drift):
    world = build_world(tmp_path)
    if drift == "other_branch":
        git("switch", "-q", "-c", "drifted-root-branch", cwd=world.primary)
    else:
        git("checkout", "-q", "--detach", cwd=world.primary)
    env = session_env(world, project_dir=world.worktree, cwd=world.worktree)
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=world.worktree, env=env)
    assert out["status"] == "human_required", out
    assert rc == 2
    assert "root_drift_active_worktree_mismatch" in out["blocked_reason_codes"], out
    assert out["root_branch_state"] in ("drifted", "detached_or_unknown")
    assert out["status"] != "ok"
    # The issue worktree was NOT mistaken for the root: its own catalog entry is still found
    # (a worktree wrongly treated as the root is excluded from the issue lookup).
    assert out["resolved_worktree"]["worktree_realpath"] == str(world.worktree), out
    # fail-closed: no mutation of the drifted primary.
    expected = "drifted-root-branch" if drift == "other_branch" else ""
    assert git("branch", "--show-current", cwd=world.primary) == expected


# ======================================================================================
# AC3
# ======================================================================================
def _calls_recorder(monkeypatch):
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _spy(cmd, *a, **kw):
        calls.append([str(c) for c in cmd])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(wc.subprocess, "run", _spy)
    return calls


def test_ac3_resolver_returns_same_primary_from_linked_worktree_and_primary(tmp_path):
    world = build_world(tmp_path)
    from_linked = wc.resolve_canonical_root(str(world.worktree))
    from_primary = wc.resolve_canonical_root(str(world.primary))
    assert from_linked.ok and from_primary.ok, (from_linked, from_primary)
    assert from_linked.primary_root == from_primary.primary_root == str(world.primary)
    nested = world.worktree / "src"
    nested.mkdir()
    assert wc.resolve_canonical_root(str(nested)).primary_root == str(world.primary)


def test_ac3_identity_runs_independent_git_common_dir_in_candidate_and_primary(tmp_path, monkeypatch):
    world = build_world(tmp_path)
    calls = _calls_recorder(monkeypatch)
    res = wc.resolve_canonical_root(str(world.worktree))
    assert res.ok, res
    common_dir_calls = [c for c in calls if "rev-parse" in c and "--git-common-dir" in c]
    scoped = {c[c.index("-C") + 1] for c in common_dir_calls if "-C" in c}
    # one subprocess per side, each run INSIDE its own work tree
    assert str(world.worktree) in scoped and str(world.primary) in scoped, common_dir_calls


def test_ac3_tampering_catalog_git_common_dir_field_does_not_change_the_result(tmp_path):
    world = build_world(tmp_path)
    honest = wc.list_worktrees(str(world.primary))
    assert honest is not None
    tampered = [dict(e, git_common_dir="/definitely/not/the/common/dir") for e in honest]
    # field-vs-field comparison would FAIL here; the independent subprocess comparison still passes
    assert wc.resolve_canonical_root(str(world.worktree), catalog=tampered).ok
    # and a "consistent" lie in the field cannot rescue a foreign primary either
    foreign = make_foreign_repo(tmp_path)
    lying_head = dict(honest[0], worktree_realpath=str(foreign), git_common_dir=honest[0]["git_common_dir"])
    foreign_first = [lying_head] + honest[1:]
    res = wc.resolve_canonical_root(str(world.worktree), catalog=foreign_first)
    assert not res.ok and res.reason_code == "repository_identity_mismatch", res


def test_ac3_foreign_common_dir_injected_through_list_worktrees_patch_is_a_mismatch(tmp_path, monkeypatch):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    honest = wc.list_worktrees(str(world.primary))
    swapped = [dict(honest[0], worktree_realpath=str(foreign))] + honest[1:]
    monkeypatch.setattr(wc, "list_worktrees", lambda *a, **k: swapped)
    res = wc.resolve_canonical_root(str(world.worktree))
    assert not res.ok
    assert res.reason_code == "repository_identity_mismatch"
    assert res.primary_root is None  # never falls back to the candidate


def test_ac3_list_worktrees_signature_and_return_are_unchanged(tmp_path):
    import inspect

    params = list(inspect.signature(wc.list_worktrees).parameters)
    assert params == ["project_root", "deadline"]
    world = build_world(tmp_path)
    catalog = wc.list_worktrees(str(world.worktree))
    assert catalog is not None and catalog[0]["worktree_realpath"] == str(world.primary)
    assert all(e["git_common_dir"] == catalog[0]["git_common_dir"] for e in catalog)  # copied field (unchanged)
    assert wc.list_worktrees(str(tmp_path / "does-not-exist")) is None  # None on failure (unchanged)


def test_ac3_cli_foreign_primary_injection_is_repository_identity_mismatch(tmp_path):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    env = session_env(world, project_dir=world.worktree, cwd=world.worktree, shim="foreign_primary", foreign=foreign)
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=world.worktree, env=env)
    assert out["status"] != "ok" and rc != 0, out
    assert out["blocked_reason_codes"] == ["repository_identity_mismatch"], out


# ======================================================================================
# AC4
# ======================================================================================
@pytest.mark.parametrize("project_dir_kind", ["primary", "worktree"])
def test_ac4_catalog_failure_never_becomes_empty_catalog_or_status_ok(tmp_path, project_dir_kind):
    world = build_world(tmp_path)
    project_dir = world.primary if project_dir_kind == "primary" else world.worktree
    env = session_env(world, project_dir=project_dir, cwd=world.worktree, shim="catalog_fail")
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=world.worktree, env=env)
    assert out["status"] != "ok", out  # old behaviour: None -> [] -> status ok when the root is default
    assert rc != 0
    assert out["blocked_reason_codes"] == ["worktree_catalog_unavailable"], out
    assert out["root_branch_state"] != "default"
    assert out["resolved_worktree"]["worktree_realpath"] is None  # no issue-worktree fallback


def test_ac4_unresolvable_primary_is_structured_failure_without_worktree_fallback(tmp_path):
    (tmp_path / "bare-world").mkdir()
    (tmp_path / "w").mkdir()
    linked = make_bare_primary_world(tmp_path / "bare-world")
    base = build_world(tmp_path / "w")  # only to obtain a gh/bin environment
    env = session_env(base, project_dir=linked, cwd=linked)
    rc, out, _done = run_guard(guard_script(base.primary), cwd=linked, env=env)
    assert out["status"] != "ok" and rc != 0, out
    assert out["blocked_reason_codes"] == ["primary_root_unresolved"], out
    assert out["root_branch_state"] != "default"


# ======================================================================================
# AC6 -- existing #1137 semantics are preserved (the regression files stay green; these pin the CLI)
# ======================================================================================
def test_ac6_root_cwd_with_explicit_issue_number_behaviour_is_unchanged(tmp_path):
    world = build_world(tmp_path)
    env = session_env(world, project_dir=world.primary, cwd=world.primary, issue=str(ISSUE))
    rc, out, _done = run_guard(guard_script(world.primary), cwd=world.primary, env=env)
    assert (out["status"], out["root_branch_state"], out["active_worktree_state"]) == ("ok", "default", "matches")
    assert out["resolved_worktree"]["cwd_classification"] == "outside_worktree"
    assert rc == 0
    # env-only issue number without a catalog entry is still not "matches"
    env2 = session_env(world, project_dir=world.primary, cwd=world.primary, issue="999999")
    _rc2, out2, _ = run_guard(guard_script(world.primary), cwd=world.primary, env=env2)
    assert out2["active_worktree_state"] != "matches"


def test_ac6_cwd_in_a_different_repository_than_project_dir_is_still_accepted(tmp_path):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    env = session_env(world, project_dir=world.primary, cwd=foreign, issue=str(ISSUE))
    rc, out, _done = run_guard(guard_script(world.primary), cwd=foreign, env=env)
    assert out["root_branch_state"] == "default" and out["active_worktree_state"] == "matches", out
    assert out["resolved_worktree"]["cwd_classification"] == "unknown"
    assert rc == 0


def test_ac6_root_drift_without_active_issue_remains_blocked_root_branch_drift(tmp_path):
    world = build_world(tmp_path)
    git("switch", "-q", "-c", "plain-drift", cwd=world.primary)
    env = session_env(world, project_dir=world.primary, cwd=world.primary)
    rc, out, _done = run_guard(guard_script(world.primary), cwd=world.primary, env=env)
    assert out["status"] == "blocked" and out["blocked_reason_codes"] == ["root_branch_drift"], out
    assert rc == 1


# ======================================================================================
# AC7 -- the documented invocation is exercised, not a project_root injection
# ======================================================================================
def test_ac7_documented_guard_preflight_invocation_from_issue_worktree_session(tmp_path):
    world = build_world(tmp_path)
    ctx = skill_section("3. worktree / branch を整理")
    block = block_containing(ctx, "scripts/agent-ops/guard_preflight.py --json")
    script = render(block, {"<issue>": str(ISSUE)})
    assert "guard_preflight.py" in script and "--project-root" not in script  # no root injection
    env = session_env(world, project_dir=world.worktree, cwd=world.worktree)
    done = run_bash(script, cwd=world.worktree, env=env)  # relative path -> the worktree's tracked copy
    out = json.loads(done.stdout)
    assert (out["status"], out["root_branch_state"], out["active_worktree_state"]) == ("ok", "default", "matches"), out
    assert done.returncode == 0


# ======================================================================================
# AC9
# ======================================================================================
def test_ac9_nested_cwd_identifies_active_issue_from_catalog_containment(tmp_path):
    world = build_world(tmp_path)
    nested = world.worktree / "src" / "deep"
    nested.mkdir(parents=True)
    env = session_env(world, project_dir=world.worktree, cwd=nested)  # LOOP_ISSUE_NUMBER unset
    assert "LOOP_ISSUE_NUMBER" not in env
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=nested, env=env)
    assert out["resolved_worktree"]["issue_number"] == ISSUE, out
    assert out["active_worktree_state"] == "matches" and out["status"] == "ok", out
    assert out["resolved_worktree"]["cwd_classification"] == "inside_worktree"
    assert rc == 0


def test_ac9_catalog_identity_wins_over_a_misleading_cwd_basename(tmp_path):
    world = build_world(tmp_path)
    decoy = world.worktree / "issue-9999-decoy"
    decoy.mkdir()
    env = session_env(world, project_dir=world.worktree, cwd=decoy)
    _rc, out, _done = run_guard(guard_script(world.worktree), cwd=decoy, env=env)
    assert out["resolved_worktree"]["issue_number"] == ISSUE, out


def test_ac9_nested_cwd_with_true_root_drift_stays_human_required(tmp_path):
    world = build_world(tmp_path)
    git("switch", "-q", "-c", "drifted-root-branch", cwd=world.primary)
    nested = world.worktree / "src"
    nested.mkdir()
    env = session_env(world, project_dir=world.worktree, cwd=nested)
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=nested, env=env)
    assert out["status"] == "human_required" and rc == 2, out
    assert out["blocked_reason_codes"] == ["root_drift_active_worktree_mismatch"], out


def test_ac9_loop_issue_number_conflicting_with_catalog_identity_is_non_success(tmp_path):
    world = build_world(tmp_path)
    nested = world.worktree / "src"
    nested.mkdir()
    env = session_env(world, project_dir=world.worktree, cwd=nested, issue="1234")
    rc, out, _done = run_guard(guard_script(world.worktree), cwd=nested, env=env)
    assert out["status"] != "ok" and rc != 0, out
    assert "active_issue_catalog_conflict" in out["blocked_reason_codes"], out
    assert out["active_worktree_state"] == "mismatch"
    # a matching explicit number is not a conflict
    env_ok = session_env(world, project_dir=world.worktree, cwd=nested, issue=str(ISSUE))
    _rc, out_ok, _ = run_guard(guard_script(world.worktree), cwd=nested, env=env_ok)
    assert out_ok["status"] == "ok" and out_ok["blocked_reason_codes"] == []


def test_ac9_containment_prefers_linked_entries_then_the_deepest_and_primary_last():
    primary = {"worktree_realpath": "/r", "branch_ref": "refs/heads/main"}
    outer = {"worktree_realpath": "/r/.claude/worktrees/issue-1-a", "branch_ref": "refs/heads/worktree-issue-1-a"}
    inner = {"worktree_realpath": "/r/.claude/worktrees/issue-1-a/.claude/worktrees/issue-2-b",
             "branch_ref": "refs/heads/worktree-issue-2-b"}
    catalog = [primary, outer, inner]
    assert wc.find_containing_entry(catalog, "/r/.claude/worktrees/issue-1-a/src") is outer
    assert wc.find_containing_entry(catalog, "/r/.claude/worktrees/issue-1-a/.claude/worktrees/issue-2-b/x") is inner
    assert wc.find_containing_entry(catalog, "/r/docs") is primary
    assert wc.find_containing_entry(catalog, "/elsewhere") is None
    # a sibling whose NAME merely starts with the worktree path is not contained
    assert wc.find_containing_entry(catalog, "/r/.claude/worktrees/issue-1-abc") is primary
    assert wc.issue_number_from_entry(outer) == "1" and wc.issue_number_from_entry(primary) is None


# ======================================================================================
# AC10 (guard lane; the executor / materialize lanes live in the cleanup test file)
# ======================================================================================
def _assert_guard_failure(world, env, cwd, code):
    rc, out, _done = run_guard(guard_script(world.primary), cwd=cwd, env=env)
    assert out["schema"] == "AGENT_GUARD_PREFLIGHT_V1"
    assert out["status"] != "ok" and rc != 0, out
    assert out["blocked_reason_codes"] == [code], out
    assert out["root_branch_state"] != "default"


def test_ac10_guard_reports_each_root_resolution_failure_with_its_fixed_reason_code(tmp_path):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    cases = [
        ("catalog_fail", None, "worktree_catalog_unavailable"),
        ("common_dir_fail", None, "git_common_dir_unavailable"),
        ("foreign_primary", foreign, "repository_identity_mismatch"),
    ]
    for shim, frn, code in cases:
        env = session_env(world, project_dir=world.worktree, cwd=world.worktree, shim=shim, foreign=frn)
        _assert_guard_failure(world, env, world.worktree, code)


def test_ac10_guard_reports_primary_root_unresolved_for_bare_primary_and_for_non_git_candidates(tmp_path):
    world = build_world(tmp_path)
    (tmp_path / "bare-world").mkdir()
    linked = make_bare_primary_world(tmp_path / "bare-world")
    env = session_env(world, project_dir=linked, cwd=linked)
    _assert_guard_failure(world, env, linked, "primary_root_unresolved")
    not_git = tmp_path / "not-a-repo"
    not_git.mkdir()
    env2 = session_env(world, project_dir=not_git, cwd=world.worktree)
    _assert_guard_failure(world, env2, world.worktree, "primary_root_unresolved")


# ======================================================================================
# AC11
# ======================================================================================
def _guard_view(out: dict) -> tuple:
    r = out["resolved_worktree"]
    return (out["status"], out["root_branch_state"], out["active_worktree_state"],
            r["worktree_realpath"], r["issue_number"], tuple(out["blocked_reason_codes"]))


def test_ac11_guard_evaluates_the_same_canonical_root_for_primary_worktree_and_script_location(tmp_path):
    world = build_world(tmp_path)
    views = {}
    # (a) CLAUDE_PROJECT_DIR -> primary root
    env_a = session_env(world, project_dir=world.primary, cwd=world.worktree)
    views["a"] = _guard_view(run_guard(guard_script(world.primary), cwd=world.worktree, env=env_a)[1])
    # (b) CLAUDE_PROJECT_DIR -> issue worktree
    env_b = session_env(world, project_dir=world.worktree, cwd=world.worktree)
    views["b"] = _guard_view(run_guard(guard_script(world.worktree), cwd=world.worktree, env=env_b)[1])
    # (c) CLAUDE_PROJECT_DIR unset, the script lives inside the issue worktree (tracked copy)
    env_c = session_env(world, project_dir=None, cwd=world.worktree)
    assert "CLAUDE_PROJECT_DIR" not in env_c
    views["c"] = _guard_view(run_guard(guard_script(world.worktree), cwd=world.worktree, env=env_c)[1])
    assert views["a"] == views["b"] == views["c"], views
    assert views["a"][:3] == ("ok", "default", "matches")
    assert views["a"][3] == str(world.worktree)


def test_ac11_candidate_priority_is_project_root_then_env_then_script_with_no_silent_fallthrough(tmp_path):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    not_git = tmp_path / "plain"
    not_git.mkdir()
    script = guard_script(world.worktree)
    # --project-root outranks CLAUDE_PROJECT_DIR (env points at a non-git directory)
    env = session_env(world, project_dir=not_git, cwd=world.worktree)
    _rc, out, _ = run_guard(script, cwd=world.worktree, env=env, extra=["--project-root", str(world.worktree)])
    assert out["status"] == "ok" and out["root_branch_state"] == "default", out
    # an unusable --project-root is rejected; it does not fall through to the valid env root
    env2 = session_env(world, project_dir=world.primary, cwd=world.worktree)
    rc2, out2, _ = run_guard(script, cwd=world.worktree, env=env2, extra=["--project-root", str(not_git)])
    assert out2["blocked_reason_codes"] == ["primary_root_unresolved"] and rc2 != 0, out2
    # CLAUDE_PROJECT_DIR outranks the script location
    env3 = session_env(world, project_dir=foreign, cwd=world.worktree)
    _rc3, out3, _ = run_guard(script, cwd=world.worktree, env=env3)
    assert out3["resolved_worktree"]["worktree_realpath"] is None, out3  # evaluated the foreign repo, not ours
    # cwd's repository is not required to equal the candidate's repository
    assert out3["blocked_reason_codes"] != ["repository_identity_mismatch"]


def test_ac11_project_root_flag_still_verifies_identity(tmp_path):
    world = build_world(tmp_path)
    foreign = make_foreign_repo(tmp_path)
    env = session_env(world, project_dir=world.primary, cwd=world.worktree, shim="foreign_primary", foreign=foreign)
    rc, out, _ = run_guard(guard_script(world.primary), cwd=world.worktree, env=env,
                           extra=["--project-root", str(world.worktree)])
    assert out["blocked_reason_codes"] == ["repository_identity_mismatch"] and rc != 0, out


# ======================================================================================
# AC12 -- evidence conventions
# ======================================================================================
def _test_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]


def test_ac12_every_acceptance_criterion_has_at_least_one_dedicated_test():
    names = _test_names(THIS_FILE) + _test_names(CLEANUP_TEST_FILE)
    missing = [n for n in range(1, 13) if not any(name.startswith(f"test_ac{n}_") for name in names)]
    assert missing == [], f"ACs without a dedicated test_ac<N>_ test: {missing}"
    # exit 5 (no tests collected) / exit 4 must never stand in for evidence: each -k selector is non-empty
    for n in range(1, 13):
        assert len([x for x in names if x.startswith(f"test_ac{n}_")]) >= 1
