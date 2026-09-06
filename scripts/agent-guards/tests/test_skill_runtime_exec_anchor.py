"""
test_skill_runtime_exec_anchor.py

Real subprocess chain tests for `preflight.run.with_anchor` in
skill_runtime_exec.py (Issue #1498).

Covers AC4 (executor reaches real subprocess for Matrix #2, Matrix #4 exits 2)
and AC9 (real executor chain positive + negative smoke).
"""

from __future__ import annotations

import base64
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    # Issue #2199: mirrors this real repo's own `.gitignore` entries for
    # `.claude/worktrees/` (the fixed dedicated-worktree path, #2197 --
    # `capture_primary_checkout_invariant_snapshot()`'s `git status` must
    # NOT see it as untracked drift under this fixture's own
    # `project_root`) and the bare `artifacts/` pattern (the real repo's own
    # pattern, which also matches `.claude/artifacts/**` at any depth --
    # tests that dispatch the executor more than once against the SAME
    # dedicated worktree need the artifact files the first dispatch leaves
    # behind there to stay git-ignored, or the fixed worktree's own
    # `git status` would report it dirty and reuse would fail-closed on the
    # second dispatch).
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.claude/worktrees/\nartifacts/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _init_control_plane_origin(repo_root: Path, origin: Path) -> None:
    """A real, local, deterministic ``file://`` bare remote (Issue #2199)
    that Issue #2199's dedicated-worktree lifecycle (#2196/#2197/#2198) can
    bind against with no real GitHub network access
    (``network_required: false``) -- the SAME bare + symbolic-HEAD pattern
    ``tests/agent_ops/test_control_plane_worktree_bootstrap.py``'s own
    ``_init_remote_fixture()`` already uses for #2197. Pushes ``repo_root``'s
    current ``HEAD`` (the already-committed fixture, see
    ``_install_skill_runtime_exec_fixture`` below) as this bare remote's
    ``main``."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "push", "-q", str(origin), "HEAD:refs/heads/main"],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        env=env,
    )


def _execution_root(repo_root: Path) -> Path:
    """The one fixed dedicated worktree path Issue #2199's wired `main()`
    dispatches the 4 production preflight profiles' child process under
    (mirrors `worktree_bootstrap_exec.fixed_control_plane_worktree_path()`)
    -- this fixture's own artifact-existence assertions must look here, not
    under `repo_root`, once the child's cwd is the dedicated worktree."""
    return repo_root / ".claude" / "worktrees" / "control-plane-preflight"


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/worktree_bootstrap_command_policy.py",
        "scripts/agent-ops/worktree_bootstrap_exec.py",
        "scripts/agent-ops/worktree_catalog.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    # Issue #2199: `preflight.run`/`.with_anchor`/`.with_human_context`/
    # `.with_agent_report` are now production preflight profiles `main()`
    # dispatches under a dedicated worktree bound to a real remote's
    # `accepted_oid` (#2197). Source-patch the copied `skill_runtime_exec.py`'s
    # hardcoded canonical remote constant to this fixture's own local,
    # deterministic bare remote (computed here, before that remote is
    # actually created by `_init_control_plane_origin` below, so this patch
    # can be committed together with every other fixture file) -- never the
    # real `https://github.com/...` production remote (`network_required:
    # false` / `auth_required: false` per this Issue's Runtime Verification
    # Applicability).
    control_plane_origin_path = repo_root.parent / "control-plane-origin.git"
    control_plane_remote_url = control_plane_origin_path.as_uri()
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_remote_line = 'CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{TRUSTED_REPO_SLUG}.git"'
    assert default_remote_line in executor_source
    fixture_remote_line = f"CONTROL_PLANE_CANONICAL_REMOTE_URL = {control_plane_remote_url!r}"
    executor_path.write_text(executor_source.replace(default_remote_line, fixture_remote_line), encoding="utf-8")

    pin = _pinned_uv_version(source_root)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "skill-runtime-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        # Issue #2311 AC1 fixture parity: bare `preflight.run` first-hops
        # into `workflow_start_entry.py` (a minimal fixture-local forwarder
        # to `run_refinement_preflight.py` below -- see that file) instead
        # of `run_refinement_preflight.py` directly. Sibling anchor-comment
        # entries (`.with_anchor` / `.with_human_context` /
        # `.with_agent_report`) below are unaffected and keep
        # `run_refinement_preflight.py` as their first hop.
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "workflow_start_entry.py",
        """from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    _inner = Path(__file__).resolve().parent / "run_refinement_preflight.py"
    _proc = subprocess.run([sys.executable, str(_inner), *sys.argv[1:]])
    raise SystemExit(_proc.returncode)
""",
    )
    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    },
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
        },
    },
    "preflight.run.with_human_context": {
        "id": "preflight.run.with_human_context",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--investigation-evidence-transport-path", "{investigation_evidence_transport_path}",
            "--investigation-evidence-primary-root", "{investigation_evidence_primary_root}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
            "investigation_evidence_transport_path": {
                "type": "path", "required": False, "optional_flag_pair": True,
            },
            # Issue #2199 OWNER feedback P1-3: kept in parity with the real
            # `command_registry.py` entry so this fixture's registry still
            # matches `skill_runtime_command_policy.py`'s own
            # `_EXPECTED_ARGV_BY_COMMAND`/`_EXPECTED_PLACEHOLDERS_BY_COMMAND`
            # (the REAL, copied policy module `validate_registry_entry()`
            # checks this fixture registry against).
            "investigation_evidence_primary_root": {
                "type": "path", "required": False, "optional_flag_pair": True,
            },
        },
    },
    "preflight.run.with_agent_report": {
        "id": "preflight.run.with_agent_report",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}", "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--agent-report-comment-url", "{anchor_comment_url}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {
                "type": "github_issue_comment_url", "required": True,
            },
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    # #2086 P0 fix_delta (Blocker 1/2): mirror production render_command()'s
    # optional_flag_pair mechanism -- an optional placeholder token whose
    # value was not supplied (and the flag literal token immediately before
    # it) is dropped entirely, instead of KeyError-ing.
    placeholders = REGISTRY[command_id].get("placeholders", {})
    argv = REGISTRY[command_id]["argv"]
    rendered: list[str] = []
    idx = 0
    while idx < len(argv):
        token = argv[idx]
        if token.startswith("{") and token.endswith("}"):
            name = token[1:-1]
            spec = placeholders.get(name, {})
            if name not in values and spec.get("optional_flag_pair"):
                if rendered and idx > 0 and not (argv[idx - 1].startswith("{")):
                    rendered.pop()
                idx += 1
                continue
            rendered.append(str(values[name]))
        else:
            rendered.append(token)
        idx += 1
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

parser = argparse.ArgumentParser()
parser.add_argument("--issue-number", required=True)
parser.add_argument("--repo", required=True)
parser.add_argument("--anchor-comment-url")
parser.add_argument("--human-context-comment-url", dest="human_context_comment_urls", action="append", default=[])
parser.add_argument("--agent-report-comment-url", dest="agent_report_comment_urls", action="append", default=[])
parser.add_argument("--consume-contract-patch-plan", action="store_true")
parser.add_argument("--investigation-evidence-transport-path", default=None)
args = parser.parse_args()
artifact = Path(".claude/artifacts/issue-refinement-loop") / args.issue_number
artifact.mkdir(parents=True, exist_ok=True)
payload = {
    "issue_number": args.issue_number,
    "repo": args.repo,
    "anchor_comment_url": args.anchor_comment_url,
    "human_context_comment_urls": args.human_context_comment_urls,
    "agent_report_comment_urls": args.agent_report_comment_urls,
}
(artifact / "preflight.json").write_text(json.dumps(payload))
print(json.dumps({"ok": True, **payload}))
""",
    )

    # Issue #2199: the dedicated worktree's child process
    # (`workflow_start_entry.py` -> `run_refinement_preflight.py`) actually
    # runs FROM the dedicated worktree checked out at the remote's
    # `accepted_oid` -- everything the child needs must be part of a REAL
    # commit this fixture pushes to its own local bare origin below, not
    # merely present, uncommitted, in the outer working tree.
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install skill runtime fixture", cwd=repo_root)
    _init_control_plane_origin(repo_root, repo_root.parent / "control-plane-origin.git")


def _copy_tree(source: Path, destination: Path) -> None:
    """Copy a production skill subtree into an isolated process fixture."""
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))


def _write_controlled_gh(trusted_bin: Path) -> None:
    """Install an actual no-network ``gh`` executable for the fixture.

    It records only safe operation labels and opaque config-path identity. The
    copied production controlled executor reaches it as an OS child process.
    """
    trusted_bin.mkdir(parents=True)
    gh_path = trusted_bin / "gh"
    gh_path.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ARTIFACT = Path.cwd() / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
STATE_PATH = ARTIFACT / "fake_remote_issue.json"
EXPECTED_CONFIG_DIR = os.environ.get("SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR")
if not EXPECTED_CONFIG_DIR or os.environ.get("GH_CONFIG_DIR") != EXPECTED_CONFIG_DIR:
    print("missing_or_wrong_config_path", file=sys.stderr)
    raise SystemExit(66)
if any(
    name in os.environ
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
):
    print("unexpected_credential_environment", file=sys.stderr)
    raise SystemExit(65)

argv = sys.argv[1:]
read_argv = [
    "api",
    "--hostname",
    "github.com",
    "repos/squne121/loop-protocol/issues/1498",
    "--jq",
    '{title, body, updatedAt: .updated_at, isPullRequest: has("pull_request")}',
]
patch_prefix = [
    "api",
    "--hostname",
    "github.com",
    "--method",
    "PATCH",
    "repos/squne121/loop-protocol/issues/1498",
    "--input",
]
if argv == read_argv:
    operation = "issue_content_read"
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    response = {
        "title": state["title"],
        "body": state["body"],
        "updatedAt": state["updatedAt"],
        "isPullRequest": False,
    }
elif tuple(argv) in {
    (
        "issue",
        "view",
        "1498",
        "--repo",
        "squne121/loop-protocol",
        "--json",
        "title,body,updatedAt",
    ),
    (
        "issue",
        "view",
        "1498",
        "--repo",
        "squne121/loop-protocol",
        "--json",
        "number,title,body,labels,url,updatedAt",
    ),
}:
    operation = "issue_view"
    response = json.loads(STATE_PATH.read_text(encoding="utf-8"))
elif argv == [
    "api",
    "repos/squne121/loop-protocol/issues/1498/comments?per_page=100",
    "--paginate",
    "--slurp",
]:
    operation = "issue_comments_read"
    response = [[json.loads((ARTIFACT / "fake_anchor.json").read_text(encoding="utf-8"))]]
elif argv == ["api", "repos/squne121/loop-protocol/issues/comments/1"]:
    operation = "issue_comment_read"
    response = json.loads((ARTIFACT / "fake_anchor.json").read_text(encoding="utf-8"))
elif len(argv) == len(patch_prefix) + 1 and argv[:-1] == patch_prefix:
    operation = "issue_content_patch"
    # Issue #2393 P1-B regression harness only: deterministically hang the
    # PATCH call itself (before the fixture state is ever mutated) so a
    # real, single-process-group child under `_run_child_with_supervision()`
    # genuinely times out DURING the mutation attempt, before readback --
    # never a fixed `sleep` racing the test, since the outer executor's own
    # timeout (shrunk by the test below) is what actually kills this sleep.
    _hang_seconds = os.environ.get("SKILL_RUNTIME_TEST_HANG_DURING_PATCH_SECONDS")
    if _hang_seconds:
        time.sleep(float(_hang_seconds))
    patch = json.loads(Path(argv[-1]).read_text(encoding="utf-8"))
    if set(patch) != {"title", "body"} or not all(isinstance(patch[key], str) for key in patch):
        print("invalid_issue_content_patch", file=sys.stderr)
        raise SystemExit(64)
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state["title"] = patch["title"]
    state["body"] = patch["body"]
    state["updatedAt"] = "2026-08-01T00:00:01Z"
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    response = {"ok": True}
    # Issue #2393 P1-B regression harness only: deterministically inject a
    # stray write OUTSIDE every allowed root, AFTER the real mutation above
    # already landed -- proving the outer unauthorized-write check can fire
    # on a span where the inner transaction genuinely already achieved a
    # result, distinct from the inner transaction itself ever writing there.
    _stray_write_rel = os.environ.get("SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH")
    if _stray_write_rel:
        _stray_path = Path.cwd() / _stray_write_rel
        _stray_path.parent.mkdir(parents=True, exist_ok=True)
        _stray_path.write_text("stray-unauthorized-write\\n", encoding="utf-8")
else:
    print("unexpected_fake_gh_argv", file=sys.stderr)
    raise SystemExit(64)

with (ARTIFACT / "fake_gh_operations.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(operation) + "\\n")
with (ARTIFACT / "fake_gh_config_states.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps("expected_path") + "\\n")
print(json.dumps(response))
""",
        encoding="utf-8",
    )
    gh_path.chmod(0o755)


def _materialize_dedicated_worktree(repo_root: Path, control_plane_remote_url: str, monkeypatch) -> Path:
    """Pre-materialize the SAME fixed dedicated worktree the real subprocess
    dispatch below will reuse (Issue #2393).

    `validate_detached_worktree_path()` fail-closed-rejects creating a
    worktree at a path that already exists, so fixture data cannot be
    pre-seeded there until the worktree itself exists -- and the worktree
    cannot exist before ITS OWN first bootstrap. This performs that SAME
    `control_plane_dedicated_execution_session()` bootstrap the real
    subprocess dispatch will later reuse (crash/rerun "same accepted OID ->
    reuse as-is", `worktree_bootstrap_exec.recover_or_create_fixed_control_plane_worktree()`'s
    own documented contract), using the SOURCE tree's own module directly
    (never a reimplementation), monkeypatched to the SAME local bare origin
    the fixture's own COPIED `skill_runtime_exec.py` was patched to use.
    """
    agent_ops_dir = REPO_ROOT / "scripts" / "agent-ops"
    if str(agent_ops_dir) not in sys.path:
        sys.path.insert(0, str(agent_ops_dir))
    import worktree_bootstrap_exec as source_bootstrap_mod  # noqa: E402

    monkeypatch.setattr(source_bootstrap_mod, "CONTROL_PLANE_CANONICAL_REMOTE_URL", control_plane_remote_url)
    with source_bootstrap_mod.control_plane_dedicated_execution_session(str(repo_root)) as session:
        return Path(str(session["execution_root"]))


def _install_real_contract_update_fixture(repo_root: Path, trusted_gh_bin: Path) -> str:
    """Install the production wrapper and its direct consumers.

    Only GitHub and the controlled mutation executor are replaced.  The
    registry, policy, privileged executor, production preflight wrapper,
    planner, candidate readiness, review, and edit transaction helper all run
    as their production files in the temporary repository.

    Issue #2393: `contract_update.run.with_anchor`/`.with_human_context` are
    now in `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`, so this fixture must
    ALSO provide the same dedicated-worktree bootstrap infrastructure
    `_install_skill_runtime_exec_fixture()` already provides for the 4
    preflight profiles (`worktree_bootstrap_exec.py`, the REAL
    `worktree_catalog.py` -- not a `list_worktrees() -> []` stub -- and a
    real local `file://` bare remote), and a real, committed, pushed
    repository state for the dedicated worktree to actually check out from
    (never the real GitHub remote; `network_required: false` is preserved).
    Returns the local bare origin's `file://` URL so callers can patch
    `worktree_bootstrap_exec.CONTROL_PLANE_CANONICAL_REMOTE_URL` for
    in-process pre-materialization (see `_materialize_dedicated_worktree()`).
    """
    source_root = REPO_ROOT
    for skill in (
        "issue-refinement-loop",
        "edit-issue",
        "issue-contract-review",
        "review-issue",
    ):
        _copy_tree(
            source_root / ".claude" / "skills" / skill,
            repo_root / ".claude" / "skills" / skill,
        )
    _copy_tree(
        source_root / ".claude" / "skills" / "create-issue" / "scripts",
        repo_root / ".claude" / "skills" / "create-issue" / "scripts",
    )
    _copy_tree(source_root / ".github" / "ISSUE_TEMPLATE", repo_root / ".github" / "ISSUE_TEMPLATE")
    _copy_tree(source_root / "docs" / "dev", repo_root / "docs" / "dev")
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/controlled_skill_mutation_exec.py",
        "scripts/agent-guards/controlled_skill_mutation_policy.py",
        "scripts/agent-guards/worktree_bootstrap_command_policy.py",
        "scripts/agent-ops/worktree_bootstrap_exec.py",
        "scripts/agent-ops/worktree_catalog.py",
    ):
        src = source_root / rel
        _write_text(repo_root / rel, src.read_text())

    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_safe_path_return = (
        "    return _dedupe_path_entries([*_trusted_toolchain_dirs(\"uv\"), *_SYSTEM_STANDARD_PATH_DIRS])\n"
    )
    fixture_safe_path_return = (
        f"    return _dedupe_path_entries([{str(trusted_gh_bin)!r}, *_trusted_toolchain_dirs(\"uv\"), "
        "*_SYSTEM_STANDARD_PATH_DIRS])\n"
    )
    assert default_safe_path_return in executor_source
    executor_source = executor_source.replace(default_safe_path_return, fixture_safe_path_return)

    # Issue #2393: mirrors `_install_skill_runtime_exec_fixture()`'s own
    # `CONTROL_PLANE_CANONICAL_REMOTE_URL` patch -- the dedicated worktree
    # bootstrap this fixture now exercises must bind against a local,
    # deterministic bare remote, never the real `https://github.com/...`
    # production remote.
    control_plane_origin_path = repo_root.parent / "control-plane-origin.git"
    control_plane_remote_url = control_plane_origin_path.as_uri()
    default_remote_line = 'CONTROL_PLANE_CANONICAL_REMOTE_URL = f"https://github.com/{TRUSTED_REPO_SLUG}.git"'
    assert default_remote_line in executor_source
    fixture_remote_line = f"CONTROL_PLANE_CANONICAL_REMOTE_URL = {control_plane_remote_url!r}"
    executor_source = executor_source.replace(default_remote_line, fixture_remote_line)
    _write_text(executor_path, executor_source)

    controlled_executor_path = repo_root / "scripts" / "agent-guards" / "controlled_skill_mutation_exec.py"
    controlled_executor_source = controlled_executor_path.read_text(encoding="utf-8")
    default_gh_trusted_paths = '_GH_TRUSTED_PATHS = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'
    fixture_gh_trusted_paths = (
        f'_GH_TRUSTED_PATHS = {str(trusted_gh_bin)!r} + '
        '":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'
    )
    assert default_gh_trusted_paths in controlled_executor_source
    _write_text(
        controlled_executor_path,
        controlled_executor_source.replace(default_gh_trusted_paths, fixture_gh_trusted_paths),
    )
    _write_controlled_gh(trusted_gh_bin)

    for rel in ("pyproject.toml", "uv.lock"):
        _write_text(repo_root / rel, (source_root / rel).read_text())
    # Materialize the project environment before the executor snapshots the
    # fixture repository; the runtime command itself must not create an
    # unrelated `.venv/` write.
    subprocess.run(["uv", "sync", "--locked"], cwd=str(repo_root), check=True, capture_output=True, text=True)

    # Fixture-only isolated-home read boundary. The production wrapper selects
    # this credentialless adapter before its authenticated transaction phase;
    # it reads only local fake state and never opens GH_CONFIG_DIR.
    _write_text(
        repo_root / "scripts" / "agent-guards" / "github_credentialless_read.py",
        """from __future__ import annotations

import json
from pathlib import Path


class CredentiallessGitHubReadTransport:
    def _artifact(self) -> Path:
        return Path.cwd() / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"

    def read_issue(self, repo: str, issue_number: int) -> dict:
        return json.loads((self._artifact() / "fake_remote_issue.json").read_text(encoding="utf-8"))

    def list_issue_comments(self, repo: str, issue_number: int) -> list[dict]:
        return [json.loads((self._artifact() / "fake_anchor.json").read_text(encoding="utf-8"))]

    def read_issue_comment(self, repo: str, comment_id: int) -> dict:
        return json.loads((self._artifact() / "fake_anchor.json").read_text(encoding="utf-8"))
""",
    )

    # The transaction helper remains production code.  This fake is its
    # external controlled-executor boundary and mutates only the fixture's
    # artifact-root backed fake remote state.
    _write_text(
        repo_root / "scripts" / "agent-guards" / "fixture_unused_controlled_skill_mutation_exec.py",
        """from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-id", required=True)
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
    artifact = Path(".claude/artifacts/issue-refinement-loop") / args.issue_number
    state_path = artifact / "fake_remote_issue.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["title"] = payload["new_title"]
    state["body"] = payload["new_body"]
    state["updatedAt"] = "2026-08-01T00:00:01Z"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    (artifact / "controlled_transaction_request.json").write_text(
        json.dumps({"command_id": args.command_id, "payload": payload}), encoding="utf-8"
    )
    print(json.dumps({"new_body_sha256": payload["new_body_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    _write_text(
        next((repo_root / ".venv").glob("lib/python*/site-packages")) / "fixture_unused_subprocess_helpers.py",
        """# Fixture-only external GitHub boundary for subprocess tests.
import json
import os
from pathlib import Path
import subprocess
import sys

_real_run = subprocess.run
_real_popen_init = subprocess.Popen.__init__


def _fake_gh(args, *positional, **kwargs):
    if not isinstance(args, (list, tuple)) or not args:
        return _real_run(args, *positional, **kwargs)
    executable = Path(str(args[0])).name
    if executable == "uv":
        child_env = dict(kwargs.get("env") or os.environ)
        child_env["PYTHONPATH"] = str(Path(__file__).parent)
        kwargs["env"] = child_env
        return _real_run(args, *positional, **kwargs)
    if executable != "gh":
        return _real_run(args, *positional, **kwargs)
    artifact = Path.cwd() / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    state = artifact / "fake_remote_issue.json"
    anchor = artifact / "fake_anchor.json"
    calls = artifact / "fake_gh_calls.jsonl"
    config_states = artifact / "fake_gh_config_states.jsonl"
    argv = [str(value) for value in args[1:]]
    expected_config_dir = os.environ.get("SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR")
    config_state = (
        "expected_path"
        if expected_config_dir and os.environ.get("GH_CONFIG_DIR") == expected_config_dir
        else "missing_or_wrong_path"
    )
    if expected_config_dir and any(
        name in os.environ
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    ):
        return subprocess.CompletedProcess(args, 2, "", "unexpected credential environment")
    calls.parent.mkdir(parents=True, exist_ok=True)
    with calls.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(argv) + "\\n")
    with config_states.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(config_state) + "\\n")
    issue_read_argvs = {
        (
            "issue", "view", "1498", "--repo", "squne121/loop-protocol",
            "--json", "title,body,updatedAt",
        ),
        (
            "issue", "view", "1498", "--repo", "squne121/loop-protocol",
            "--json", "number,title,body,labels,url,updatedAt",
        ),
    }
    comments_read_argv = (
        "api", "repos/squne121/loop-protocol/issues/1498/comments?per_page=100",
        "--paginate", "--slurp",
    )
    anchor_read_argv = (
        "api", "repos/squne121/loop-protocol/issues/comments/1",
    )
    if tuple(argv) in issue_read_argvs:
        payload = json.loads(state.read_text(encoding="utf-8"))
    elif tuple(argv) == comments_read_argv:
        payload = [[json.loads(anchor.read_text(encoding="utf-8"))]]
    elif tuple(argv) == anchor_read_argv:
        payload = json.loads(anchor.read_text(encoding="utf-8"))
    else:
        return subprocess.CompletedProcess(args, 2, "", "unexpected fake gh argv")
    return subprocess.CompletedProcess(args, 0, json.dumps(payload) + "\\n", "")


def _fake_popen_init(self, args, *positional, **kwargs):
    # Issue #2075: skill_runtime_exec.py's outer-child supervisor launches
    # its child via subprocess.Popen (not subprocess.run). Mirror the same
    # PYTHONPATH propagation _fake_gh() applies to `uv` subprocess.run calls
    # so a nested `uv run python3 run_refinement_preflight.py` launched via
    # Popen still inherits this fixture's sitecustomize.py boundary.
    if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == "uv":
        child_env = dict(kwargs.get("env") or os.environ)
        child_env["PYTHONPATH"] = str(Path(__file__).parent)
        kwargs["env"] = child_env
    return _real_popen_init(self, args, *positional, **kwargs)

subprocess.run = _fake_gh
subprocess.Popen.__init__ = _fake_popen_init
""",
    )

    # Issue #2393: `.venv/` (created by `uv sync --locked` above) must never
    # be committed -- it is large, non-reproducible, and irrelevant to the
    # dedicated worktree anyway (a MANAGED uv project's dedicated dispatch
    # relocates `UV_PROJECT_ENVIRONMENT` outside `execution_root` entirely,
    # see `dedicated_execution_venv_dir()`). `_make_repo()`'s own
    # `.gitignore` already covers `.claude/worktrees/`/`artifacts/`/`tmp/`.
    gitignore_path = repo_root / ".gitignore"
    gitignore_path.write_text(gitignore_path.read_text(encoding="utf-8") + ".venv/\n", encoding="utf-8")

    # A real, committed, pushed HEAD is required for the dedicated worktree
    # bootstrap this fixture now exercises (Issue #2393) to check out a real
    # `accepted_oid` from -- unlike the non-dedicated path, which just reads
    # whatever is on disk at `repo_root` regardless of commit status.
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install real contract update fixture", cwd=repo_root)
    _init_control_plane_origin(repo_root, control_plane_origin_path)
    return control_plane_remote_url

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
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
    },
    "preflight.run.with_anchor": {
        "id": "preflight.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
    "contract_update.run.with_anchor": {
        "id": "contract_update.run.with_anchor",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
            "--issue-number", "{issue_number}",
            "--repo", "{repo}",
            "--anchor-comment-url", "{anchor_comment_url}",
            "--human-context-comment-url", "{anchor_comment_url}",
            "--consume-contract-patch-plan",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime_contract_update_anchor",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [
            ".claude/artifacts/issue-refinement-loop/{active_issue}/",
            "artifacts/{active_issue}/issue-metadata/",
        ],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
            "anchor_comment_url": {"type": "github_issue_comment_url", "required": True},
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        elif token == "{anchor_comment_url}":
            rendered.append(str(values["anchor_comment_url"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / "scripts" / "agent-guards" / "fake_issue_edit_txn.py",
        """from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    request = json.loads(Path(sys.argv[1]).read_text())
    artifact_dir = Path(request["artifact_dir"])
    (artifact_dir / "transaction_invoked.json").write_text(json.dumps(request))
    metadata_dir = Path("artifacts") / request["issue_number"] / "issue-metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "transaction.json").write_text(json.dumps(request))
    (artifact_dir / "final_body.txt").write_text(request["candidate_body"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--anchor-comment-url", required=False, default=None)
    parser.add_argument("--human-context-comment-url", required=False, default=None)
    parser.add_argument("--consume-contract-patch-plan", action="store_true")
    args = parser.parse_args()
    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "issue_number": args.issue_number,
        "repo": args.repo,
        "anchor_comment_url": args.anchor_comment_url,
    }
    if args.consume_contract_patch_plan:
        final_body = artifact_dir / "final_body.txt"
        if final_body.exists():
            handoff = {
                "status": "no_change", "writes": 0, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        else:
            candidate_body = "## Outcome\\ntrusted desired state\\n"
            request = artifact_dir / "issue_edit_txn_input.json"
            request.write_text(json.dumps({
                "schema": "ISSUE_EDIT_TXN_INPUT_V1", "artifact_dir": str(artifact_dir),
                "issue_number": args.issue_number,
                "candidate_body": candidate_body,
            }))
            subprocess.run(
                [sys.executable, "scripts/agent-guards/fake_issue_edit_txn.py", str(request)],
                check=True, shell=False,
            )
            assert final_body.read_text() == candidate_body
            (artifact_dir / "fresh_check_input.json").write_text(json.dumps({"body": candidate_body}))
            handoff = {
                "status": "applied", "writes": 1, "iterations": 0,
                "final_readback": "verified", "fresh_preflight": "pass",
                "fresh_review": "approve", "fresh_readiness": "go",
            }
        payload["contract_update"] = handoff
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


_VALID_URL = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"


def _run_executor(
    repo: Path,
    command_id: str = "preflight.run.with_anchor",
    issue_number: str = "1498",
    repo_slug: str = "squne121/loop-protocol",
    anchor_comment_url: "str | None" = _VALID_URL,
    extra_args: "list[str] | None" = None,
    extra_env: "dict[str, str] | None" = None,
    use_fixture_runtime: bool = False,
) -> subprocess.CompletedProcess[str]:
    # Default fixture invocations model an ordinary account HOME. Individual
    # isolated-HOME tests override this with their own temporary directory.
    env = {
        **os.environ,
        "HOME": pwd.getpwuid(os.getuid()).pw_dir,
        "CLAUDE_PROJECT_DIR": str(repo),
    }
    if extra_env:
        env.update(extra_env)
    runtime = ["uv", "run", "--locked", "python3"] if use_fixture_runtime else [sys.executable]
    argv = [
        *runtime,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        command_id,
        "--issue-number",
        issue_number,
        "--repo",
        repo_slug,
    ]
    if anchor_comment_url is not None:
        argv += ["--anchor-comment-url", anchor_comment_url]
    if extra_args:
        argv += extra_args
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


# ---------------------------------------------------------------------------
# AC4 / AC9: Matrix #2 reaches the real subprocess chain.
# ---------------------------------------------------------------------------


def test_executor_reaches_subprocess_and_rejects_anchor_on_preflight_run(tmp_path: Path) -> None:
    """AC4: Matrix #2 (valid anchor) reaches real subprocess execution;
    Matrix #4 (anchor on preflight.run) exits 2 without running a subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    # Matrix #2: positive — real subprocess chain executes end to end.
    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr
    # Issue #2199: `preflight.run.with_anchor` is one of the 4 production
    # dedicated-worktree profiles -- the child actually runs under
    # `_execution_root(repo)`, never `repo` itself.
    artifact = _execution_root(repo) / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    assert artifact.exists(), "expected preflight artifact to be created by the real subprocess"
    assert not (repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json").exists()
    payload = json.loads(artifact.read_text())
    assert payload == {
        "issue_number": "1498",
        "repo": "squne121/loop-protocol",
        "anchor_comment_url": _VALID_URL,
        "human_context_comment_urls": [],
        "agent_report_comment_urls": [],
    }
    assert json.loads(positive.stdout)["anchor_comment_url"] == _VALID_URL

    # Matrix #4: negative — anchor flag rejected for preflight.run before any
    # subprocess is spawned.
    negative = _run_executor(repo, command_id="preflight.run", issue_number="1499")
    assert negative.returncode == 2, negative.stderr
    assert "anchor-comment-url" in negative.stderr
    no_artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1499" / "preflight.json"
    assert not no_artifact.exists()


def test_executor_real_subprocess_smoke_positive_and_negative(tmp_path: Path) -> None:
    """AC9: real executor chain positive and negative smoke via subprocess."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    positive = _run_executor(repo)
    assert positive.returncode == 0, positive.stderr

    # Negative: preflight.run.fixture must reject --anchor-comment-url too.
    fixture_negative = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run.fixture",
            "--issue-number",
            "1498",
            "--repo",
            "squne121/loop-protocol",
            "--fixture",
            "tmp/fixture.json",
            "--anchor-comment-url",
            _VALID_URL,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )
    assert fixture_negative.returncode == 2, fixture_negative.stderr

    # Negative: missing anchor-comment-url for preflight.run.with_anchor.
    missing_anchor = _run_executor(repo, anchor_comment_url=None)
    assert missing_anchor.returncode == 2, missing_anchor.stderr
    assert "required" in missing_anchor.stderr


def test_executor_rejects_context_mismatched_anchor_url(tmp_path: Path) -> None:
    """Matrix #22: URL owner/repo/issue must bind to the CLI arguments."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    mismatched = _run_executor(
        repo,
        anchor_comment_url="https://github.com/other/repo/issues/1498#issuecomment-1",
    )
    assert mismatched.returncode == 2, mismatched.stderr


def test_executor_preflight_run_unaffected_without_anchor(tmp_path: Path) -> None:
    """AC1: preflight.run (no anchor) continues to work exactly as before."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run", anchor_comment_url=None)
    assert result.returncode == 0, result.stderr
    artifact = _execution_root(repo) / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    payload = json.loads(artifact.read_text())
    assert payload["anchor_comment_url"] is None


def test_contract_update_phase_reaches_fake_transaction_and_fresh_handoff(tmp_path: Path, monkeypatch) -> None:
    """#1877 AC3/AC6/AC10: production process path and fake GitHub boundary.

    This runs the real registry, policy, privileged executor,
    ``run_refinement_preflight.py``, planner, candidate readiness, review,
    and ``edit_issue_txn.py`` in a temporary git repository. It uses an
    isolated HOME and test-owned config-only `GH_CONFIG_DIR`; the fake GitHub
    boundary records only exact argv plus an opaque path-identity match.
    Only GitHub CLI I/O is faked by a fixture-owned executable. The real
    controlled mutation executor runs unchanged apart from a fixture-local
    trusted-path extension; no fixture replaces the phase wrapper or changes
    the production executor's trust policy.

    Issue #2393: both `contract_update.run.*` command_ids now dispatch
    their child under the SAME dedicated worktree the 4 preflight profiles
    already use, so this test's artifact/workspace paths live under
    `execution_root`, not `repo`, once the worktree is pre-materialized
    (`_materialize_dedicated_worktree()`).
    """
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)
    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    # Issue #2393 P1-A: deliberately do NOT pre-create `tmp/` here. A
    # genuinely cold dedicated worktree (as `_materialize_dedicated_worktree`
    # above just produced) has no `tmp/` directory at all until the
    # dispatched child's own inner edit-issue transaction creates it -- the
    # SAME scenario the production `_dispatch_child_and_check_postconditions`
    # fix now handles by pre-creating `tmp/` itself, before its own
    # before-snapshot, rather than relying on this test to paper over the
    # gap. See `test_contract_update_cold_dedicated_worktree_tmp_not_
    # misdetected_as_unauthorized_write` below for the dedicated regression
    # coverage; this test's OWN cold-start dispatch below already exercises
    # the identical code path.
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    first = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    # The controlled write reached final readback, but the post-update
    # contract review detects the deliberately incomplete new AC.  That is a
    # terminal fail-closed result, never a successful implementation route.
    assert first.returncode == 2, first.stderr
    # Issue #2393 P1-A regression: this dispatch's `execution_root` was a
    # genuinely COLD dedicated worktree (no `tmp/` pre-seed above) the very
    # first time it ran a contract_update dispatch, and the transaction
    # above genuinely wrote+deleted files under a freshly-created `tmp/`.
    # The outer supervision layer must never misreport that as an
    # unauthorized write, and the exit code 2 above must be the INNER
    # child's own "needs_fix" mapping (relayed via the ordinary success
    # path), never the outer `SKILL_RUNTIME_FAIL` failure line.
    assert "SKILL_RUNTIME_FAIL" not in first.stderr, first.stderr
    assert "unauthorized_write_path" not in first.stderr
    assert first.stdout.strip(), "expected the inner transaction result to be relayed to the caller"
    # The production controlled executor's authenticated effect is verified
    # below through the fixture-local remote state and ordered fake-gh child
    # operations, rather than through a fixture-specific executor receipt.
    assert "AC2: trusted fixture directive" in json.loads(
        (artifact_dir / "fake_remote_issue.json").read_text()
    )["body"]
    operations = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_operations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    config_states = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_config_states.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert operations == [
        "issue_view",
        "issue_content_read",
        "issue_content_patch",
        "issue_content_read",
        "issue_view",
    ]
    assert config_states == ["expected_path"] * len(operations)
    result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert result["contract_update"] == {
        "status": "failed",
        "disposition": "patch",
        "writes": 1,
        "iterations": 0,
        "final_readback": "verified",
        "fresh_preflight": "pass",
        # The synthetic directive intentionally introduces a new AC without
        # its matching verification-command marker.  The failed review must
        # block the phase rather than remain telemetry on a successful exit.
        "fresh_review": "needs_fix",
        "fresh_readiness": "go",
    }
    provenance = json.loads((artifact_dir / "refinement_preflight_provenance_v1.json").read_text())
    assert provenance["runtime_evidence"]["source"]["source_kind"] == "issue_comment"

    replay = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    assert replay.returncode == 2, replay.stderr
    replay_result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert replay_result["contract_update"]["status"] == "failed"
    assert replay_result["contract_update"]["writes"] == 0
    generic_replay = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    assert generic_replay.returncode == 2, generic_replay.stderr
    generic_replay_result = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert generic_replay_result["contract_update"]["status"] == "failed"
    assert generic_replay_result["contract_update"]["writes"] == 0
    assert not (execution_root / "artifacts" / "1498" / "issue-metadata").exists() or len(
        list((execution_root / "artifacts" / "1498" / "issue-metadata").rglob("*.input.json"))
    ) == 1


def test_contract_update_outer_artifact_projection_failure_preserves_inner_transaction_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Issue #2526 AC1-AC5: real-dispatcher regression test for the THIRD
    (previously untested) outer-failure emission site in
    `_dispatch_child_and_check_postconditions()` -- the artifact-projection
    failure branch, alongside the already-covered timeout / unauthorized-
    write siblings.

    Reuses the SAME fake GitHub executable + isolated Git fixture
    (`_install_real_contract_update_fixture()` / the "trusted fixture
    directive" anchor + pre-body pair) that
    `test_contract_update_phase_reaches_fake_transaction_and_fresh_handoff`
    above already establishes, so the inner GitHub mutation, final readback,
    and inner `refinement_preflight_result_v1.json` persistence all complete
    for real FIRST -- the identical `disposition: "patch"` / `writes: 1` /
    `final_readback: "verified"` / `fresh_review: "needs_fix"` outcome as
    that sibling test's own "first" dispatch. ONLY AFTER that inner
    transaction has fully succeeded does this test corrupt what the
    caller-visible stdout says the FIRST (sorted) `ARTIFACT:` entry's path
    is, to a fixture-available path OUTSIDE the artifact root
    (``README.md``) -- never an actual write to that path, and never routed
    through the unauthorized-write check.

    The corruption patches exactly one isolated function,
    `_render_artifact_projection_lines()` (the final stdout-rendering step),
    in a COPY of `run_refinement_preflight.py` committed into this test's
    own isolated, local fixture repository -- never the real repo file, and
    never `_write_artifacts()`'s own on-disk writes/consistency checks
    above it in the same module. This is the same kind of fixture-only,
    non-behavioral source patch this test module already applies to
    `skill_runtime_exec.py` (its `CONTROL_PLANE_CANONICAL_REMOTE_URL` /
    safe-PATH patches in `_install_skill_runtime_exec_fixture()` /
    `_install_real_contract_update_fixture()` above) and to
    `controlled_skill_mutation_exec.py` (its `_GH_TRUSTED_PATHS` patch
    above) -- so the persisted result artifact this test reads back below
    still carries the real, correct `contract_update` fields.
    """
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)

    # Corrupt ONLY the final stdout artifact-projection rendering step of the
    # copied `run_refinement_preflight.py` -- the real GitHub mutation,
    # final readback, and `refinement_preflight_result_v1.json` persistence
    # (all performed by `_write_artifacts()`, a wholly separate function)
    # are completely unaffected by this patch.
    preflight_script_path = (
        repo / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py"
    )
    preflight_source = preflight_script_path.read_text(encoding="utf-8")
    default_render_fn = (
        'def _render_artifact_projection_lines(artifacts: dict[str, str]) -> list[str]:\n'
        '    lines: list[str] = ["ARTIFACT:"]\n'
        '    for key, value in sorted(artifacts.items()):\n'
        '        lines.append(f"  {key}: {value}")\n'
        '    return lines\n'
    )
    assert default_render_fn in preflight_source
    corrupted_render_fn = (
        'def _render_artifact_projection_lines(artifacts: dict[str, str]) -> list[str]:\n'
        '    lines: list[str] = ["ARTIFACT:"]\n'
        '    for index, (key, value) in enumerate(sorted(artifacts.items())):\n'
        '        # Issue #2526 regression fixture only: corrupt ONLY the\n'
        '        # first (sorted) rendered entry to a fixture-available\n'
        '        # path OUTSIDE the artifact root -- never written to.\n'
        '        if index == 0:\n'
        '            value = os.path.join(os.getcwd(), "README.md")\n'
        '        lines.append(f"  {key}: {value}")\n'
        '    return lines\n'
    )
    preflight_script_path.write_text(
        preflight_source.replace(default_render_fn, corrupted_render_fn), encoding="utf-8"
    )
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "install artifact-projection corruption fixture (Issue #2526)", cwd=repo)
    _init_control_plane_origin(repo, repo.parent / "control-plane-origin.git")

    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result_proc = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )

    # AC3: the outer dispatch failed on the artifact-projection branch (not
    # the inner child's own exit-code relay) -- AND-combined, never OR.
    assert result_proc.returncode == 2, result_proc.stderr
    assert result_proc.stdout == "", result_proc.stdout
    assert "SKILL_RUNTIME_FAIL" in result_proc.stderr, result_proc.stderr
    assert "reason_code=stale_worktree_runtime_state" in result_proc.stderr, result_proc.stderr
    assert (
        "recovery=do_not_publish_artifact_projection_outside_issue_artifact_root" in result_proc.stderr
    ), result_proc.stderr
    assert "stale_path=README.md" in result_proc.stderr, result_proc.stderr

    # AC4: BOTH `inner_transaction_result_ref` and
    # `inner_transaction_disposition=patch` must be recoverable from the
    # SAME `SKILL_RUNTIME_FAIL` stderr line (OR is forbidden).
    # `inner_transaction_state=unknown` is a hard FAILURE for this scenario
    # -- mutation, readback, and result persistence all genuinely succeeded
    # before the artifact-projection corruption was ever detected.
    assert "inner_transaction_state=unknown" not in result_proc.stderr, result_proc.stderr
    ref_match = re.search(r"inner_transaction_result_ref=(\S+)", result_proc.stderr)
    disposition_match = re.search(r"inner_transaction_disposition=(\S+)", result_proc.stderr)
    assert ref_match is not None, result_proc.stderr
    assert disposition_match is not None, result_proc.stderr
    assert disposition_match.group(1) == "patch", result_proc.stderr

    inner_result_path = Path(ref_match.group(1))
    expected_inner_result_path = (
        execution_root
        / ".claude"
        / "artifacts"
        / "issue-refinement-loop"
        / "1498"
        / "refinement_preflight_result_v1.json"
    )
    assert inner_result_path == expected_inner_result_path, (inner_result_path, expected_inner_result_path)
    inner_result = json.loads(inner_result_path.read_text(encoding="utf-8"))
    assert inner_result["contract_update"]["status"] == "failed"
    assert inner_result["contract_update"]["disposition"] == "patch"
    assert inner_result["contract_update"]["writes"] == 1
    assert inner_result["contract_update"]["final_readback"] == "verified"
    assert inner_result["contract_update"]["fresh_review"] == "needs_fix"

    # AC5: exactly one mutation/write call reached the fake GitHub
    # executable -- no blind resend after the outer artifact-projection
    # failure.
    operations = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_operations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert operations.count("issue_content_patch") == 1, operations


def test_contract_update_phase_cannot_be_reached_through_preflight_command(tmp_path: Path) -> None:
    """The read-only preflight command never receives the consumer flag."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    result = _run_executor(repo, command_id="preflight.run.with_anchor")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "contract_update" not in payload
    artifact_dir = _execution_root(repo) / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    assert not (artifact_dir / "transaction_invoked.json").exists()


def test_anchor_profiles_materialize_only_the_explicit_origin_lane(tmp_path: Path) -> None:
    """P0: executor preserves the profile-selected lane into the child argv."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)

    generic = _run_executor(repo, command_id="preflight.run.with_anchor")
    assert generic.returncode == 0, generic.stderr
    # Issue #2199: this test reuses the SAME dedicated worktree across 3
    # dispatches on the same repo (all 4 production profiles bind the SAME
    # fixed `execution_root`) -- `artifacts/` is git-ignored (see
    # `_make_repo` above) so the previous dispatch's leftover artifact file
    # never makes the fixed worktree's own `git status` dirty for reuse.
    artifact = _execution_root(repo) / ".claude" / "artifacts" / "issue-refinement-loop" / "1498" / "preflight.json"
    generic_payload = json.loads(artifact.read_text())
    assert generic_payload["human_context_comment_urls"] == []
    assert generic_payload["agent_report_comment_urls"] == []

    human = _run_executor(repo, command_id="preflight.run.with_human_context")
    assert human.returncode == 0, human.stderr
    human_payload = json.loads(artifact.read_text())
    assert human_payload["human_context_comment_urls"] == [_VALID_URL]
    assert human_payload["agent_report_comment_urls"] == []

    agent = _run_executor(repo, command_id="preflight.run.with_agent_report")
    assert agent.returncode == 0, agent.stderr
    agent_payload = json.loads(artifact.read_text())
    assert agent_payload["human_context_comment_urls"] == []
    assert agent_payload["agent_report_comment_urls"] == [_VALID_URL]


# ---------------------------------------------------------------------------
# PR #2057 OWNER REQUEST_CHANGES (P0-3): true process-boundary E2E for the
# #2048 approved-scope-reframe / empty-operations `full_rewrite_required`
# disposition, reusing `_install_real_contract_update_fixture()` (the SAME
# harness `test_contract_update_phase_reaches_fake_transaction_and_fresh_
# handoff` above already established for PR #1884/#1877). This exercises:
#
#   registry (command_registry.REGISTRY["contract_update.run.with_anchor"])
#   -> skill_runtime_command_policy.py (privileged command parsing)
#   -> skill_runtime_exec.py (real subprocess boundary)
#   -> run_refinement_preflight.py (REAL production wrapper, not a fixture
#      stub -- run_preflight() -> consume_trusted_anchor_contract_patch_plan()
#      -> scope_signal_delta.run_trusted_anchor_iteration_zero() ->
#      decide_rewrite_route.classify_scope_reframe_disposition())
#   -> fake `gh` (subprocess.run monkeypatch; only GitHub I/O is faked)
#   -> refinement_preflight_result_v1.json artifact + NEXT_ACTION stdout
#
# unlike the in-process dynamic-import tests in
# test_preflight_run_with_anchor.py, this crosses a real subprocess boundary
# (skill_runtime_exec.py spawns `uv run --locked python3 run_refinement_
# preflight.py` as a genuinely separate process).
# ---------------------------------------------------------------------------


def test_contract_update_phase_full_rewrite_required_reaches_next_action_via_real_subprocess(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """AC1/AC6/P0-3: an approved trusted-anchor ANCHOR_SCOPE_REFRAME_V1 scope
    reframe (not yet reflected in the Issue's Allowed Paths section) reaches
    `contract_update.status == "handoff_required"` /
    `disposition == "full_rewrite_required"` through the REAL registry ->
    policy -> privileged executor subprocess -> production
    `run_refinement_preflight.py` -> production `scope_signal_delta.py`
    classifier chain, with ZERO writes and ZERO mutation-executor
    invocations (full-rewrite handoff, not a mutation attempt).

    Issue #2393 AC5: this legitimate no-write `handoff_required` /
    `full_rewrite_required` disposition must reach the caller UNCHANGED now
    that `contract_update.run.with_anchor` dispatches through the dedicated
    control-plane runtime -- the outer dedicated-runtime supervision layer
    (`_dispatch_child_and_check_postconditions`'s before/after checks) never
    overwrites an already-achieved inner-transaction result; it only ever
    prints a DIFFERENT failure classification to stderr and returns a
    different process exit code, never touching the artifact this test reads
    back below.
    """
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)
    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    # Issue #2393 P1-A: no `tmp/` pre-seed -- this dispatch's own cold start
    # (a freshly-materialized dedicated worktree with no `tmp/` directory at
    # all yet) is the exact scenario the production fix now handles.
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }

    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor_body = (
        "```yaml\n"
        "schema_version: ANCHOR_SCOPE_REFRAME_V1\n"
        "target:\n  repo: squne121/loop-protocol\n  issue_number: 1498\n"
        "decision: approve_scope_delta\n"
        'allowed_path_deltas: ["docs/product/features/scope-only-reframe.md"]\n'
        'rationale: "process-boundary E2E fixture (PR #2057 OWNER review P0-3)"\n'
        'required_rerun: ["contract_review"]\n'
        "```\n"
    )
    anchor = {
        "id": 1,
        "body": anchor_body,
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-09T00:00:00Z",
        "updated_at": "2026-08-09T00:00:00Z",
    }
    pre_body = (
        "## Machine-Readable Contract\n\n"
        "```yaml\ncontract_schema_version: v1\nissue_kind: implementation\n"
        "parent_issue: none\ngoal_ref: test\nchange_kind: workflow\n```\n\n"
        "## Parent Issue\n\nnone\n\n## Parent Goal Ref\n\ntest\n\n"
        "## Current Validated Scope\n\n- test\n\n## Remaining Parent Gaps\n\nnone\n\n"
        "## Outcome\n\ntest\n\n## In Scope\n\n- test\n\n## Out of Scope\n\n- none\n\n"
        "## Acceptance Criteria\n\n- [ ] AC1: test\n\n"
        "## Verification Commands\n\n```bash\n$ true\n```\n\n"
        "## Allowed Paths\n\n- docs/product/features/existing.md\n\n"
        "## Stop Conditions\n\n- none\n\n## Required Skills\n\n- none\n"
    )
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-09T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )

    assert "NEXT_ACTION: issue_editor_required" in result.stdout, result.stdout + result.stderr
    payload = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert payload["next_action"] == "issue_editor_required"
    assert payload["contract_update"]["status"] == "handoff_required"
    assert payload["contract_update"]["disposition"] == "full_rewrite_required"
    assert payload["contract_update"]["writes"] == 0
    assert payload["contract_update"]["final_readback"] == "not_applicable"
    # `full_rewrite_required` resolves before the production controlled
    # mutation executor. Its real marker must therefore be absent, and the
    # fixture-owned gh child may not receive a PATCH argv.
    production_marker_path = (
        execution_root
        / "artifacts"
        / "1498"
        / "issue-metadata"
        / "issue_content.update"
        / "issue_content_update.marker.json"
    )
    operations_path = artifact_dir / "fake_gh_operations.jsonl"
    operations = (
        [json.loads(line) for line in operations_path.read_text(encoding="utf-8").splitlines()]
        if operations_path.exists()
        else []
    )
    assert not production_marker_path.exists()
    assert "issue_content_patch" not in operations

    # Replaying the identical transition through a second real subprocess
    # invocation is still side-effect free and deterministic (regression #2
    # / #8 "side-effect 0" + "restart replay").
    replay = _run_executor(
        repo,
        command_id="contract_update.run.with_anchor",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    assert "NEXT_ACTION: issue_editor_required" in replay.stdout, replay.stdout + replay.stderr
    replay_payload = json.loads((artifact_dir / "refinement_preflight_result_v1.json").read_text())
    assert replay_payload["contract_update"]["status"] == "handoff_required"
    assert replay_payload["contract_update"]["writes"] == 0
    assert not production_marker_path.exists()
    replay_operations = (
        [json.loads(line) for line in operations_path.read_text(encoding="utf-8").splitlines()]
        if operations_path.exists()
        else []
    )
    assert "issue_content_patch" not in replay_operations


# ---------------------------------------------------------------------------
# Issue #2393 AC4: a relative GH_CONFIG_DIR resolves to the SAME directory
# after the dedicated-runtime migration as it did before it (both relative
# and absolute inputs are covered).
# ---------------------------------------------------------------------------


def test_contract_update_relative_gh_config_dir_resolves_against_invocation_cwd(tmp_path: Path, monkeypatch) -> None:
    """Issue #2393 AC4: a RELATIVE `GH_CONFIG_DIR` value is anchored to the
    invocation cwd (`project_root` -- the SAME directory `_run_executor()`
    passes as both `cwd` and `CLAUDE_PROJECT_DIR`) BEFORE the child's own
    cwd switches to the dedicated worktree, so it resolves to the SAME
    directory it would have resolved to under the pre-#2393
    canonical-main-root-only execution. The ABSOLUTE case is already
    covered by `test_contract_update_phase_reaches_fake_transaction_and_fresh_handoff`
    above (its `gh_config_dir` fixture value is an absolute `tmp_path` child
    and is asserted to reach the fake `gh` unchanged); this reuses that SAME
    real-`gh`-reaching `contract_update.run.with_human_context` trusted-anchor
    mutation fixture data, only swapping `GH_CONFIG_DIR` for a RELATIVE
    value. Both `contract_update.run.*` profiles share the SAME
    `GH_CONFIG_DIR` resolution code path in `skill_runtime_exec.py` (no
    command-id branching there), so this single profile's coverage proves
    the shared logic for both -- `contract_update.run.with_anchor` (no
    human-context binding) never reaches an authenticated write on a first
    attempt at all (a separate, pre-existing trust-lane restriction, not
    something this Issue changes), so it is not a suitable fixture for
    proving a real-`gh`-reaching mutation."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)
    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    # Issue #2393 P1-A: no `tmp/` pre-seed -- this dispatch's own cold start
    # is the exact scenario the production fix now handles.
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    # RELATIVE to `repo` (the invocation cwd) -- never resolved against
    # `execution_root` (the child's own cwd once dispatched).
    relative_gh_config_dir = "rel-gh-config"
    (repo / relative_gh_config_dir).mkdir()
    expected_absolute_gh_config_dir = str((repo / relative_gh_config_dir).resolve())
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": relative_gh_config_dir,
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": expected_absolute_gh_config_dir,
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )

    assert result.returncode == 2, result.stderr
    assert "AC2: trusted fixture directive" in json.loads(
        (artifact_dir / "fake_remote_issue.json").read_text()
    )["body"]
    # The fake `gh` (`_write_controlled_gh`) rejects any call whose
    # `GH_CONFIG_DIR` does not match `SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR`
    # (exit 66, `missing_or_wrong_config_path`) -- reaching real `gh` reads
    # AND the successful mutation above proves the RELATIVE value was
    # correctly resolved to `expected_absolute_gh_config_dir` before the
    # child's cwd became `execution_root`.
    operations = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_operations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert operations, result.stdout + result.stderr
    config_states = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_config_states.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert config_states == ["expected_path"] * len(operations)


# ---------------------------------------------------------------------------
# Issue #2393 P1-A: a cold dedicated worktree's `tmp/` directory must never
# be misdetected as an unauthorized write, on EITHER a first-ever dispatch
# against a fixed worktree path, or a dispatch immediately after that fixed
# path was torn down and recreated at a new `accepted_oid` (the real
# `recover_or_create_fixed_control_plane_worktree()` "different OID, clean
# tree -> remove + recreate" transition). `test_contract_update_phase_
# reaches_fake_transaction_and_fresh_handoff` above already covers the
# first case (no `tmp/` pre-seed there); this covers the second.
# ---------------------------------------------------------------------------


def test_contract_update_cold_dedicated_worktree_tmp_not_misdetected_after_worktree_recreation(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #2393 P1-A regression (case 2, OWNER review recovery scenario
    "dedicated worktree が再作成された後の初回起動"): re-running a genuine
    contract-update mutation against a dedicated worktree that was just
    removed and recreated at a NEW accepted OID must not regress into the
    cold-start `unauthorized_write_path` misdetection either. "Cold start" is
    a structural property of a freshly (re)created dedicated worktree having
    no `tmp/` directory yet -- not a one-time condition that only holds the
    very first time a test process ever dispatches against the fixed path.
    """
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)
    origin_path = repo.parent / "control-plane-origin.git"

    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }

    def _populate_fixture_state() -> Path:
        execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
        artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "fake_remote_issue.json").write_text(
            json.dumps(
                {
                    "number": 1498,
                    "title": "fixture",
                    "body": pre_body,
                    "labels": [],
                    "url": "x",
                    "updatedAt": "2026-08-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")
        return execution_root

    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
    }

    execution_root_first = _populate_fixture_state()
    first = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    assert first.returncode == 2, first.stderr
    assert "SKILL_RUNTIME_FAIL" not in first.stderr, first.stderr
    assert "unauthorized_write_path" not in first.stderr

    # Force `accepted_oid` to advance: a new commit pushed onto the SAME bare
    # origin's `main` -- so the next `_materialize_dedicated_worktree()` call
    # below takes the REAL "different OID, clean tree -> remove + recreate"
    # transition (`recover_or_create_fixed_control_plane_worktree`), never a
    # bespoke test-only teardown of the worktree.
    (repo / "advance.txt").write_text("advance\n")
    _git("add", "advance.txt", cwd=repo)
    _git("commit", "-q", "-m", "advance accepted_oid", cwd=repo)
    _git("push", "-q", str(origin_path), "HEAD:refs/heads/main", cwd=repo)

    execution_root_second = _populate_fixture_state()
    assert execution_root_second == execution_root_first
    assert not (execution_root_second / "tmp").exists(), (
        "expected the recreated dedicated worktree to be genuinely cold (no tmp/) again"
    )

    second = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )
    assert second.returncode == 2, second.stderr
    assert "SKILL_RUNTIME_FAIL" not in second.stderr, second.stderr
    assert "unauthorized_write_path" not in second.stderr
    operations = [
        json.loads(line)
        for line in (
            execution_root_second
            / ".claude"
            / "artifacts"
            / "issue-refinement-loop"
            / "1498"
            / "fake_gh_operations.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert operations.count("issue_content_patch") == 1


# ---------------------------------------------------------------------------
# Issue #2393 AC5: an outer dedicated-runtime supervision failure
# (timeout / unauthorized-write / artifact-projection / primary-checkout
# drift) must never overwrite or discard an already-achieved
# inner-transaction result -- these `_emit_*_failure()` helpers only ever
# print a stderr classification line and return a process exit code; they
# never touch the filesystem. This is a direct, subprocess-free unit proof
# of that invariant against THIS repository's own (dedicated-set-widened)
# `skill_runtime_exec.py`, complementing the real-subprocess
# `test_contract_update_phase_full_rewrite_required_reaches_next_action_via_real_subprocess`
# above (which proves the SAME invariant end-to-end for a real
# `handoff_required`/`full_rewrite_required`/`writes: 0` disposition).
# ---------------------------------------------------------------------------

_AGENT_GUARDS_DIR_FOR_UNIT_TEST = REPO_ROOT / "scripts" / "agent-guards"
if str(_AGENT_GUARDS_DIR_FOR_UNIT_TEST) not in sys.path:
    sys.path.insert(0, str(_AGENT_GUARDS_DIR_FOR_UNIT_TEST))
import skill_runtime_exec as _exec_mod_for_unit_test  # noqa: E402


@pytest.mark.parametrize(
    "emit_call",
    [
        lambda: _exec_mod_for_unit_test._emit_timeout_failure(1498, 120),
        lambda: _exec_mod_for_unit_test._emit_primary_checkout_drift_failure(1498),
        lambda: _exec_mod_for_unit_test._emit_unauthorized_write_failure(1498, "some/unexpected/path"),
        lambda: _exec_mod_for_unit_test._emit_artifact_projection_failure(1498, ["some/stale/path"]),
    ],
    ids=["timeout", "primary_checkout_drift", "unauthorized_write", "artifact_projection"],
)
def test_outer_failure_emitters_never_touch_an_already_achieved_inner_result(
    tmp_path: Path, capsys, emit_call
) -> None:
    """Issue #2393 AC5: simulates an already-achieved inner-transaction
    artifact (a real `contract_update` disposition -- `disposition: patch` +
    `final_readback: verified` + `fresh_review: needs_fix`, mirroring the
    real fixture above) sitting on disk, then calls each outer-supervision
    failure emitter and proves: (1) the artifact's bytes are byte-for-byte
    unchanged, (2) the emitter's own return value is the fail-closed exit
    code `2`, and (3) nothing is written to stdout (only stderr) -- an
    outer failure is reported as a DIFFERENT signal, never as a silent
    overwrite of the inner disposition."""
    artifact_path = tmp_path / "refinement_preflight_result_v1.json"
    inner_result = {
        "contract_update": {
            "status": "failed",
            "disposition": "patch",
            "writes": 1,
            "iterations": 0,
            "final_readback": "verified",
            "fresh_preflight": "pass",
            "fresh_review": "needs_fix",
            "fresh_readiness": "go",
        }
    }
    before_bytes = json.dumps(inner_result).encode("utf-8")
    artifact_path.write_bytes(before_bytes)

    exit_code = emit_call()

    assert exit_code == 2
    assert artifact_path.read_bytes() == before_bytes
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SKILL_RUNTIME_FAIL:" in captured.err


# ---------------------------------------------------------------------------
# Issue #2393 P1-B: the emitter-only proof above shows `_emit_*_failure()`
# never CORRUPTS an unrelated file it is handed directly. It does not prove
# that a caller of the REAL dispatcher (`_dispatch_child_and_check_
# postconditions()`) can actually RECOVER an already-achieved inner
# transaction result after a genuine outer-layer failure on a real
# subprocess span. These two tests drive the actual dispatcher end to end,
# reusing the SAME real contract-update fixture (fake `gh` + isolated Git +
# dedicated worktree) as the tests above.
# ---------------------------------------------------------------------------


def test_contract_update_outer_unauthorized_write_failure_preserves_inner_transaction_result(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #2393 P1-B (timeout/unauthorized-write/artifact-projection
    sub-classification: unauthorized-write): forces a REAL outer-layer
    `unauthorized_write_path` failure to occur AFTER the inner GitHub
    mutation (PATCH) and readback have already genuinely succeeded (via the
    fake `gh` executable injecting a stray write outside every allowed root,
    immediately after applying the real patch), and proves the caller can
    still recover the achieved inner disposition."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)
    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
        # Deterministically injects a stray write OUTSIDE every allowed root
        # immediately after the fake `gh`'s real PATCH lands (see
        # `_write_controlled_gh` above) -- a controlled, real synchronization
        # primitive already wired through `_sanitize_env()`'s
        # `SKILL_RUNTIME_TEST_` passthrough, not a fixed `sleep`/race.
        "SKILL_RUNTIME_TEST_INJECT_STRAY_WRITE_AFTER_PATCH": "unexpected_stray_file.txt",
    }
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )

    # The OUTER dispatch fails closed -- this is a DIFFERENT signal from the
    # inner child's own exit code, never a silent pass-through.
    assert "SKILL_RUNTIME_FAIL:" in result.stderr, result.stdout + result.stderr
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unexpected_stray_file.txt" in result.stderr
    # The un-verified inner NEXT_ACTION/stdout is never surfaced as a
    # success by the outer layer -- an outer failure returns BEFORE ever
    # relaying the child's own stdout.
    assert result.stdout == ""

    # The inner transaction's result is NOT lost: the caller can recover it
    # via the `inner_transaction_result_ref=`/`inner_transaction_disposition=`
    # fields in stderr, AND by directly reading the still-present provenance
    # artifact under the allowed artifact root.
    assert "inner_transaction_disposition=patch" in result.stderr, result.stderr
    ref_match = re.search(r"inner_transaction_result_ref=(\S+)", result.stderr)
    assert ref_match is not None, result.stderr
    result_artifact_path = Path(ref_match.group(1))
    assert result_artifact_path.is_file()
    on_disk_result = json.loads(result_artifact_path.read_text(encoding="utf-8"))
    assert on_disk_result["contract_update"]["disposition"] == "patch"
    assert on_disk_result["contract_update"]["writes"] == 1
    assert on_disk_result["contract_update"]["final_readback"] == "verified"

    # Exactly one PATCH reached the fake GitHub executable -- the outer
    # failure must never trigger (nor be caused by) a second mutation
    # attempt against the SAME dispatch.
    operations = [
        json.loads(line)
        for line in (artifact_dir / "fake_gh_operations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert operations.count("issue_content_patch") == 1


def test_contract_update_outer_timeout_during_patch_reports_unknown_inner_state_not_false_unmutated(
    tmp_path: Path, monkeypatch
) -> None:
    """Issue #2393 P1-B (timeout/unauthorized-write/artifact-projection
    sub-classification: timeout): forces a REAL outer-layer timeout DURING
    the inner GitHub mutation attempt (PATCH), before readback -- mutation
    success genuinely cannot be confirmed. Asserts the outer failure reports
    the explicit `inner_transaction_state=unknown` marker, never a false
    "unmutated"/"no-op" claim, and that no PATCH is observed to have
    completed (and no second PATCH is ever attempted by this dispatch)."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    control_plane_remote_url = _install_real_contract_update_fixture(repo, trusted_gh_bin)

    # Shrink ONLY `contract_update.run.with_human_context`'s registry
    # `timeout_seconds` (120 -> 2) so the outer supervisor's real execution
    # deadline elapses in a couple of seconds instead of two minutes, without
    # touching any other profile's timeout.
    registry_path = repo / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py"
    registry_source = registry_path.read_text(encoding="utf-8")
    human_context_marker = '"id": "contract_update.run.with_human_context",'
    marker_index = registry_source.index(human_context_marker)
    timeout_marker = '"timeout_seconds": 120,'
    timeout_index = registry_source.index(timeout_marker, marker_index)
    registry_tail = registry_source[timeout_index + len(timeout_marker) :]
    registry_source = registry_source[:timeout_index] + '"timeout_seconds": 2,' + registry_tail
    registry_path.write_text(registry_source, encoding="utf-8")

    execution_root = _materialize_dedicated_worktree(repo, control_plane_remote_url, monkeypatch)
    artifact_dir = execution_root / ".claude" / "artifacts" / "issue-refinement-loop" / "1498"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_artifact_path = artifact_dir / "refinement_preflight_result_v1.json"
    assert not result_artifact_path.exists()
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "test-owned-gh-config"
    gh_config_dir.mkdir()
    config_only_env = {
        "HOME": str(isolated_home),
        "GH_CONFIG_DIR": str(gh_config_dir),
        "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "GH_ENTERPRISE_TOKEN": "",
        "GITHUB_ENTERPRISE_TOKEN": "",
        # Deterministically hangs the fake `gh`'s PATCH call itself for
        # longer than the shrunk 2-second registry timeout above -- the
        # outer supervisor's own timeout (not a test-side sleep/race) is
        # what actually bounds this test's wall-clock time.
        "SKILL_RUNTIME_TEST_HANG_DURING_PATCH_SECONDS": "30",
    }
    immutable = json.loads(
        (
            REPO_ROOT
            / ".claude/skills/issue-refinement-loop/tests/fixtures/issue_1835_trusted_anchor_iteration_zero.json"
        ).read_text(encoding="utf-8")
    )
    pre_body = base64.b64decode(immutable["expected_post_body_base64"]).decode("utf-8")
    anchor_url = "https://github.com/squne121/loop-protocol/issues/1498#issuecomment-1"
    anchor = {
        "id": 1,
        "body": "## Revised AC\n- AC2: trusted fixture directive\n",
        "html_url": anchor_url,
        "url": "https://api.github.com/repos/squne121/loop-protocol/issues/comments/1",
        "issue_url": "https://api.github.com/repos/squne121/loop-protocol/issues/1498",
        "author_association": "OWNER",
        "user": {"login": "owner", "type": "User"},
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    (artifact_dir / "fake_remote_issue.json").write_text(
        json.dumps(
            {
                "number": 1498,
                "title": "fixture",
                "body": pre_body,
                "labels": [],
                "url": "x",
                "updatedAt": "2026-08-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (artifact_dir / "fake_anchor.json").write_text(json.dumps(anchor), encoding="utf-8")

    result = _run_executor(
        repo,
        command_id="contract_update.run.with_human_context",
        anchor_comment_url=anchor_url,
        use_fixture_runtime=True,
        extra_env=config_only_env,
    )

    assert "SKILL_RUNTIME_FAIL:" in result.stderr, result.stdout + result.stderr
    assert "reason_code=child_process_timeout" in result.stderr
    # Mutation success genuinely cannot be confirmed here -- the explicit
    # `unknown` marker is required, never a false "unmutated"/"no-op" claim
    # and never a fabricated disposition.
    assert "inner_transaction_state=unknown" in result.stderr, result.stderr
    assert "inner_transaction_disposition=" not in result.stderr
    assert result.stdout == ""
    # No result artifact was ever written (the child was killed mid-PATCH,
    # long before `_write_artifacts()`/`write_provenance_artifact()` run).
    assert not result_artifact_path.exists()
    operations_path = artifact_dir / "fake_gh_operations.jsonl"
    operations = (
        [json.loads(line) for line in operations_path.read_text(encoding="utf-8").splitlines()]
        if operations_path.exists()
        else []
    )
    # The PATCH attempt itself never completed (killed mid-sleep, before its
    # own operation-label append) -- and no second PATCH was ever attempted.
    assert operations.count("issue_content_patch") == 0
