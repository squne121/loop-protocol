"""
owner_reaction_dispatch_fixture.py

Shared real-subprocess fixture-repo builder for Issue #2688's
`owner_reaction.decide` / `owner_reaction.decide.fixture`
`skill_runtime_exec.py` generic-dispatch wiring test suite
(`test_owner_reaction_decide_dispatch.py` /
`test_owner_reaction_decide_flag_validation.py` /
`test_owner_reaction_decide_failure_boundary.py`).

Not a test module itself (no `test_*` functions) -- `conftest.py` in this
same directory inserts this directory onto `sys.path`, so each of the three
test files above imports this module directly by name.

Mirrors the existing real-dispatch fixture convention already used by
`.claude/skills/issue-refinement-loop/tests/test_command_registry.py`'s
`_crpd_install_dispatch_fixture` / `scripts/agent-guards/tests/
test_issue_metadata_write_capability.py`'s `_install_fixture`: verbatim
copies of the REAL `skill_runtime_exec.py` / `skill_runtime_command_policy.py`
/ `command_registry.py` / `owner_reaction_decision.py` into an isolated
scratch git repository -- never a synthetic re-implementation of any of the
four. `command_registry.py`'s `owner_reaction.decide` /
`owner_reaction.decide.fixture` entries and `owner_reaction_decision.py`
itself are Issue #2688's read-only design references -- this fixture copies
them unmodified, it never patches their content.

The one real GitHub network boundary this fixture fakes is the `gh` binary
itself (for the PRODUCTION `owner_reaction.decide` profile only -- the
`owner_reaction.decide.fixture` sibling bypasses `gh` entirely via its own
`--gh-fixture-file`). The fake `gh` script and the real `--gh-fixture-file`
consume the IDENTICAL fixture-state JSON shape (`make_fixture_gh_runner`'s
own documented shape in `owner_reaction_decision.py`), so a single scenario
builder (`write_gh_state`) drives both profiles with the same underlying
GitHub data -- deliberately, so a test can assert both profiles reach the
same selection outcome from the same input (AC2).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

TRUSTED_REPO_SLUG = "squne121/loop-protocol"

_FAKE_GH_SOURCE = '''#!/usr/bin/env python3
"""Fake `gh` binary for Issue #2688's owner_reaction.decide real-subprocess
dispatch tests (production profile only). Reads a fixture-state JSON file
(the SAME shape as owner_reaction_decision.py's own --gh-fixture-file) named
by SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE, classifies the invoked
`gh api ...` argv the SAME way owner_reaction_decision.py's own
_classify_gh_call() does, and answers with the matching canned
returncode/stdout/stderr. Never touches the real network.
"""
import json
import os
import re
import sys


def _classify(argv, anchor_comment_id):
    endpoint = next((tok for tok in argv if tok.startswith("repos/")), "")
    if "/reactions" in endpoint:
        return "reactions"
    m = re.search(r"/issues/comments/(\\d+)(\\?.*)?$", endpoint)
    if m:
        if anchor_comment_id is not None and int(m.group(1)) == anchor_comment_id:
            return "anchor"
        return "comment"
    if re.search(r"/issues/\\d+(\\?.*)?$", endpoint):
        return "issue"
    if re.fullmatch(r"repos/[^/]+/[^/]+", endpoint):
        return "repo"
    return "unknown"


def main():
    # PR #2694 review fix_delta (P0-1) regression coverage: when set, record
    # the GH_CONFIG_DIR value this fake `gh` invocation actually observed
    # (empty string if absent) so a test can assert the real
    # skill_runtime_exec.py -> _sanitize_env() child dispatch forwarded a
    # caller-supplied GH_CONFIG_DIR through to a genuine `gh` subprocess.
    # Never touched by any scenario that does not set this env var.
    observed_gh_config_dir_path = os.environ.get(
        "SKILL_RUNTIME_TEST_OBSERVED_GH_CONFIG_DIR_FILE"
    )
    if observed_gh_config_dir_path:
        with open(observed_gh_config_dir_path, "w", encoding="utf-8") as fh:
            fh.write(os.environ.get("GH_CONFIG_DIR", ""))
    state_path = os.environ.get("SKILL_RUNTIME_TEST_OWNER_REACTION_GH_STATE_FILE")
    if not state_path:
        sys.stderr.write("fake_gh: no_state_file\\n")
        return 1
    with open(state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    argv = sys.argv[1:]
    kind = _classify(argv, state.get("anchor_comment_id"))
    entry = state.get(kind)
    if not isinstance(entry, dict):
        sys.stderr.write(f"fake_gh: no_fixture_for_kind:{kind}\\n")
        return 1
    sys.stdout.write(str(entry.get("stdout", "")))
    stderr_text = entry.get("stderr", "")
    if stderr_text:
        sys.stderr.write(str(stderr_text))
    return int(entry.get("returncode", 0))


if __name__ == "__main__":
    raise SystemExit(main())
'''


def git(*args: str, cwd: Path) -> None:
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


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("remote", "add", "origin", f"https://github.com/{TRUSTED_REPO_SLUG}.git", cwd=repo)
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.venv/\n")
    (repo / "README.md").write_text("seed\n")
    git("add", "README.md", ".gitignore", cwd=repo)
    git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


def install_fixture(repo_root: Path, trusted_gh_bin: Path) -> None:
    """Install the REAL (unmodified) privileged executor, policy module,
    command_registry.py, and owner_reaction_decision.py -- the exact four
    files Issue #2688's AC3 full-chain requires. Only
    `scripts/agent-ops/worktree_catalog.py` remains a minimal local stub (it
    depends on live worktree enumeration that is out of this Issue's scope,
    mirroring the SAME stubbing convention every other real-dispatch fixture
    in this test suite already uses)."""
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/owner_reaction_decision.py",
    ):
        write_text(repo_root / rel, (REPO_ROOT / rel).read_text(encoding="utf-8"))

    write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        '''from __future__ import annotations


class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    # PR #2694 review fix_delta (P1-2): owner_reaction.decide /
    # owner_reaction.decide.fixture are now root-no-worktree eligible (see
    # `command_allows_root_no_worktree()` in skill_runtime_command_policy.py),
    # so most test scenarios never need this stub to resolve a real entry at
    # all. It still returns one unconditionally so any scenario that DOES
    # set LOOP_ISSUE_NUMBER (mirroring an active worktree being present)
    # keeps behaving exactly as before.
    return {"issue_number": issue_number, "path": root_realpath}
''',
    )

    # PATCH: `skill_runtime_exec.py`'s `_safe_path_entries()` -- trusts the
    # fixture-owned fake `gh` directory (every child dispatch's own env PATH
    # is sanitized to exactly this list, so the caller's ambient PATH is
    # never inherited). Mirrors PATCH 1 in
    # `scripts/agent-guards/tests/test_issue_metadata_write_capability.py`.
    executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
    executor_source = executor_path.read_text(encoding="utf-8")
    default_safe_path_return = (
        '    return _dedupe_path_entries([*_trusted_toolchain_dirs("uv"), *_SYSTEM_STANDARD_PATH_DIRS])\n'
    )
    fixture_safe_path_return = (
        f"    return _dedupe_path_entries([{str(trusted_gh_bin)!r}, *_trusted_toolchain_dirs(\"uv\"), "
        "*_SYSTEM_STANDARD_PATH_DIRS])\n"
    )
    assert default_safe_path_return in executor_source, "safe_path_return literal not found"
    write_text(executor_path, executor_source.replace(default_safe_path_return, fixture_safe_path_return))

    trusted_gh_bin.mkdir(parents=True, exist_ok=True)
    gh_script = trusted_gh_bin / "gh"
    gh_script.write_text(_FAKE_GH_SOURCE, encoding="utf-8")
    gh_script.chmod(0o755)

    for rel in ("pyproject.toml", "uv.lock"):
        write_text(repo_root / rel, (REPO_ROOT / rel).read_text(encoding="utf-8"))
    assert _pinned_uv_version(repo_root) == _pinned_uv_version(REPO_ROOT)
    # Fully materialize the project environment BEFORE any dispatch --
    # `owner_reaction.decide`/`.fixture` are not in
    # `PRODUCTION_DEDICATED_WORKTREE_COMMAND_IDS`, so (unlike the 4
    # preflight profiles) they get no automatic pre-dispatch `.venv`
    # warm-up.
    subprocess.run(["uv", "sync", "--locked"], cwd=str(repo_root), check=True, capture_output=True, text=True)

    git("add", "-A", cwd=repo_root)
    git("commit", "-q", "-m", "install owner_reaction dispatch fixture", cwd=repo_root)


def run_executor(
    repo: Path,
    argv: "list[str]",
    *,
    extra_env: "dict | None" = None,
    timeout: float = 60,
) -> "subprocess.CompletedProcess[str]":
    full_argv = [sys.executable, "scripts/agent-guards/skill_runtime_exec.py", *argv]
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        full_argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def write_preview_binding(
    path: Path,
    *,
    comment_id: int,
    comment_body: str,
    anchor_comment_id: int,
    anchor_body: str,
    issue_body: str,
    reaction_option_map: "dict[str, str]",
    options: "dict[str, dict]",
) -> None:
    binding = {
        "comment_id": comment_id,
        "comment_body_hash": sha256_hex(comment_body),
        "issue_snapshot_hash": sha256_hex(issue_body),
        "anchor_comment_id": anchor_comment_id,
        "anchor_comment_body_hash": sha256_hex(anchor_body),
        "reaction_option_map": reaction_option_map,
        "options": options,
    }
    write_text(path, json.dumps(binding))


def write_gh_state(
    path: Path,
    *,
    repo: str,
    issue_number: int,
    owner_user_id: int,
    comment_id: int,
    comment_body: str,
    anchor_comment_id: int,
    anchor_body: str,
    issue_body: str,
    reactions: "list[dict]",
) -> None:
    """Builds the fixture-state JSON both the real fake `gh` binary
    (production profile) and `--gh-fixture-file` (fixture profile) consume,
    IDENTICAL to `owner_reaction_decision.make_fixture_gh_runner`'s own
    documented shape."""
    issue_url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    state = {
        "repo": {
            "returncode": 0,
            "stdout": json.dumps({"owner": {"id": owner_user_id}}),
            "stderr": "",
        },
        "comment": {
            "returncode": 0,
            "stdout": json.dumps({"body": comment_body, "issue_url": issue_url}),
            "stderr": "",
        },
        "anchor": {
            "returncode": 0,
            "stdout": json.dumps({"body": anchor_body, "issue_url": issue_url}),
            "stderr": "",
        },
        "issue": {
            "returncode": 0,
            "stdout": json.dumps({"body": issue_body}),
            "stderr": "",
        },
        "reactions": {
            "returncode": 0,
            "stdout": json.dumps([reactions]),
            "stderr": "",
        },
        "anchor_comment_id": anchor_comment_id,
    }
    write_text(path, json.dumps(state))
