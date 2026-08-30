"""
.claude/skills/issue-refinement-loop/scripts/tests/test_workflow_start_entry_canonical_executor.py

Real end-to-end subprocess test (Issue #2311 fix_delta / PR #2320 review):
proves that the canonical `preflight.run` executor
(`scripts/agent-guards/skill_runtime_exec.py --command-id preflight.run`)
actually carries an invocation-scoped `LOOP_SPARK_MODE` /
`LOOP_SPARK_FALLBACK` / `LOOP_PLANNED_OPERATIONS_JSON` capability request
through `_sanitize_env()` to the REAL `workflow_start_entry.py`, which in
turn calls the REAL `root_entry_router.capability_preflight_result()`,
which in turn invokes the REAL
`scripts/claude-gpt/workflow_capability_preflight.py` producer as an
actual child subprocess -- and that an `unsupported_operation` /
`required`+`forbidden`-Spark capability request is blocked BEFORE
`run_refinement_preflight.py` (the inner preflight) is ever started.

This is deliberately NOT a fake-transport test: `wse.run()` is never called
directly and `capability_preflight_result_fn` is never monkeypatched. Every
process boundary this Issue's production `preflight.run` first hop
actually crosses is exercised for real:

    skill_runtime_exec.py (real)
      -> _sanitize_env() (real, PR #2320 review P0-1 fix under test)
      -> workflow_start_entry.py (real)
        -> root_entry_router.capability_preflight_result() (real)
          -> workflow_capability_preflight.py (real, subprocess)

Only `run_refinement_preflight.py` (the inner preflight this Issue's gate
exists to protect) and `scripts/agent-ops/worktree_catalog.py` (an
unrelated worktree-listing helper) are fixture stubs -- the former to
detect (via a marker file) whether it was ever started at all, matching
this repo's existing `_install_skill_runtime_exec_fixture` pattern in
`scripts/agent-guards/tests/test_skill_runtime_preflight_no_worktree.py`;
the latter because it is unrelated to capability-preflight gating and
would otherwise require a live GitHub API surface this test does not need.

No live GitHub API call and no live ChatGPT/ Spark proxy auth is required:
`workflow_capability_preflight.py`'s own `gh auth status` /
`gh repo view` / `sh preflight.sh --env-only` calls either fail or (for the
missing `preflight.sh` in this fixture) raise `OSError`, which the
producer's own `_run_env_only_preflight()` already catches and normalizes
to `{}` -- exactly as it does for any real environment lacking a
`claude-code-proxy` binary/auth. This test's assertions do not depend on
which of `uv`/`spark`/`github` ends up being the actual blocking reason in
this sandbox; they instead assert on the SPECIFIC `unsupported_operation`
and (when the sandbox's Spark proxy is absent, which it always is here)
`spark:unavailable` reason strings that only appear if the declared
capability request genuinely reached the producer -- proving the P0-1 env
carry-through fix, not just "some checks failed for some other reason".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]


def _parse_compact_stdout(stdout: str) -> dict:
    """Minimal reader for the compact `STATUS:`/`NEXT_ACTION:`/`BLOCKERS:`
    stdout line grammar (Issue #2323) -- the SAME grammar
    `run_refinement_preflight.py::_build_compact_stdout()` renders. Only
    extracts the handful of fields this test file's real-subprocess
    assertions need; not a general-purpose grammar parser."""
    status = None
    next_action = None
    blockers: list[str] = []
    lines = stdout.splitlines()
    in_blockers = False
    for line in lines:
        if line.startswith("STATUS: "):
            status = line[len("STATUS: "):]
            in_blockers = False
        elif line.startswith("NEXT_ACTION: "):
            next_action = line[len("NEXT_ACTION: "):]
            in_blockers = False
        elif line == "BLOCKERS:":
            in_blockers = True
        elif in_blockers and line.startswith("  - "):
            blockers.append(line[len("  - "):])
        elif in_blockers and not line.startswith("  "):
            in_blockers = False
    return {"status": status, "next_action": next_action, "blockers": blockers}


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
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_from_source(repo_root: Path, rel: str) -> None:
    src = REPO_ROOT / rel
    dest = repo_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _install_real_capability_preflight_fixture(repo_root: Path, *, trusted_gh_bin: Path | None = None) -> None:
    """Install the REAL production first-hop chain
    (`skill_runtime_exec.py` -> `workflow_start_entry.py` ->
    `root_entry_router.capability_preflight_result()` ->
    `workflow_capability_preflight.py`), copied verbatim from this
    checkout, into a fresh temp repo. Only `run_refinement_preflight.py`
    and `scripts/agent-ops/worktree_catalog.py` are fixture stubs (see
    module docstring)."""
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        "scripts/agent-guards/trusted_runtime_capabilities.py",
        "scripts/claude-gpt/workflow_capability_preflight.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
        ".claude/skills/issue-refinement-loop/scripts/root_entry_router.py",
    ):
        _write_from_source(repo_root, rel)

    if trusted_gh_bin is not None:
        # The real executor intentionally discards caller PATH and constructs
        # a fixed trusted PATH.  Keep that production selection behavior in
        # this actual-process fixture, but prepend one fixture-owned fixed
        # directory so its controlled `gh` is selected instead of any host
        # GitHub CLI.  Ambient PATH is never used.
        executor_path = repo_root / "scripts" / "agent-guards" / "skill_runtime_exec.py"
        source = executor_path.read_text(encoding="utf-8")
        default_system_path = '''_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
'''
        fixture_system_path = f'''_SYSTEM_STANDARD_PATH_DIRS: tuple[str, ...] = (
    {str(trusted_gh_bin)!r},
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)
'''
        assert default_system_path in source
        executor_path.write_text(source.replace(default_system_path, fixture_system_path), encoding="utf-8")

    pin = _pinned_uv_version(REPO_ROOT)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "capability-preflight-e2e-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    return None
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import os
from pathlib import Path


def _build_compact_stdout(result: dict) -> str:
    # Issue #2323: minimal fixture stand-in for the real
    # `run_refinement_preflight.py::_build_compact_stdout()` -- imported
    # by `workflow_start_entry.py` at module load time (its blocked path
    # renders the same compact grammar this real function renders for
    # ready/degraded). Only the subset of fields this fixture's own
    # blocked-path assertions need is implemented.
    lines = [
        f"STATUS: {result['status']}",
        f"NEXT_ACTION: {result['next_action']}",
    ]
    blockers = result.get("blockers", [])
    if blockers:
        lines.append("BLOCKERS:")
        for b in blockers:
            lines.append(f"  - {b}")
    return "\\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    parser.parse_args()
    marker_path = os.environ.get("SKILL_RUNTIME_TEST_INNER_MARKER_PATH")
    if marker_path:
        marker = Path(marker_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("inner-preflight-ran")
    probe_log_path = os.environ.get("SKILL_RUNTIME_TEST_GH_PROBE_LOG")
    if probe_log_path:
        with Path(probe_log_path).open("a", encoding="utf-8") as probe_log:
            probe_log.write("inner-preflight-start\\n")
    print('{"schema": "refinement_preflight_result/v1", "status": "ready"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


def _run_executor(
    repo: Path,
    inner_marker_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "CLAUDE_PROJECT_DIR": str(repo),
        "SKILL_RUNTIME_TEST_INNER_MARKER_PATH": str(inner_marker_path),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _write_controlled_gh(trusted_bin: Path) -> None:
    """Install a no-network GitHub CLI stand-in for the fixed trusted PATH.

    It reads only GH_CONFIG_DIR presence plus equality with a test-owned
    expected path, then records an opaque match status and command order.  It
    never opens configuration files or observes token/HOME values.
    """
    trusted_bin.mkdir(parents=True)
    gh_path = trusted_bin / "gh"
    gh_path.write_text(
        """#!/bin/sh
set -eu
if [ -n "${GH_CONFIG_DIR:-}" ] && [ "${GH_CONFIG_DIR}" = "${SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR:-}" ]; then
    gh_config_state=expected_path
else
    gh_config_state=missing_or_wrong_path
fi
case "$*" in
    "auth status --active --hostname github.com") probe=gh-auth ;;
    "repo view github.com/squne121/loop-protocol --json name") probe=gh-repo-view ;;
    "api --hostname github.com repos/squne121/loop-protocol --jq {name}") probe=controlled-gh-api ;;
    *)
        printf '%s\n' "unexpected-gh-argv" >> "${SKILL_RUNTIME_TEST_GH_PROBE_LOG}"
        exit 64
        ;;
esac
printf '%s\\n' "${probe}:gh_config=${gh_config_state}" >> "${SKILL_RUNTIME_TEST_GH_PROBE_LOG}"
[ "${gh_config_state}" = "expected_path" ]
""",
        encoding="utf-8",
    )
    gh_path.chmod(0o755)


def test_canonical_executor_preserves_gh_config_dir_to_root_github_probes_before_inner_preflight(
    tmp_path: Path,
) -> None:
    """An isolated-HOME process crosses the real executor -> workflow-start
    -> root router -> capability producer path.  The controlled fixed-PATH
    `gh` sees only a non-secret GH_CONFIG_DIR path-identity match before the
    inner preflight starts; no GitHub network/auth/mutation is possible."""
    repo = _make_repo(tmp_path)
    trusted_gh_bin = tmp_path / "trusted-gh-bin"
    _write_controlled_gh(trusted_gh_bin)
    _install_real_capability_preflight_fixture(repo, trusted_gh_bin=trusted_gh_bin)
    isolated_home = tmp_path / "isolated-home"
    isolated_home.mkdir()
    gh_config_dir = tmp_path / "launcher-gh-config"
    gh_config_dir.mkdir()
    inner_marker = tmp_path / "inner-ran.marker"
    probe_log = tmp_path / "probe-order.log"

    result = _run_executor(
        repo,
        inner_marker,
        extra_env={
            "HOME": str(isolated_home),
            "GH_CONFIG_DIR": str(gh_config_dir),
            "SKILL_RUNTIME_TEST_EXPECTED_GH_CONFIG_DIR": str(gh_config_dir),
            "SKILL_RUNTIME_TEST_GH_PROBE_LOG": str(probe_log),
            "LOOP_PLANNED_OPERATIONS_JSON": json.dumps(
                [
                    {
                        "phase": "workflow_start",
                        "actor_role": "issue-refinement-loop",
                        "operation": "issue_comment",
                        "requires_mutation": True,
                    }
                ]
            ),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert inner_marker.exists()
    assert probe_log.read_text(encoding="utf-8").splitlines() == [
        "gh-auth:gh_config=expected_path",
        "gh-repo-view:gh_config=expected_path",
        "controlled-gh-api:gh_config=expected_path",
        "inner-preflight-start",
    ]


def test_canonical_executor_blocks_unsupported_operation_and_required_forbidden_spark_before_inner_starts(
    tmp_path: Path,
) -> None:
    """Real subprocess boundary proof for verification requirements 1, 3,
    and 4 of PR #2320 review's minimum 6 cases: the canonical executor
    carries the invocation-scoped `LOOP_*` capability request through to
    the real producer (proven by the SPECIFIC `unsupported_operation` and
    `spark:unavailable` reason strings only the real producer, having
    actually received this exact request, would emit), and
    `run_refinement_preflight.py` is never started."""
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    inner_marker = tmp_path / "inner-ran.marker"

    result = _run_executor(
        repo,
        inner_marker,
        extra_env={
            "LOOP_SPARK_MODE": "required",
            "LOOP_SPARK_FALLBACK": "forbidden",
            "LOOP_PLANNED_OPERATIONS_JSON": json.dumps(
                [
                    {
                        "phase": "workflow_start",
                        "actor_role": "issue-refinement-loop",
                        "operation": "definitely_unsupported_operation_xyz",
                        "requires_mutation": True,
                    }
                ]
            ),
        },
    )

    assert result.returncode != 0, result.stdout + result.stderr
    # Issue #2323: the blocked path now renders the SAME compact stdout
    # line grammar (`STATUS:`/`NEXT_ACTION:`/`BLOCKERS:`) that
    # `run_refinement_preflight.py` already uses for ready/degraded --
    # instead of the previous raw `{"schema": "WORKFLOW_START_ENTRY_
    # RESULT_V1", ...}` JSON dict -- so this is parsed with the compact
    # line-grammar helper, not `json.loads`.
    parsed = _parse_compact_stdout(result.stdout)
    assert parsed["status"] == "blocked"
    # A non-trivial `BLOCKERS:` list (beyond the single fixed
    # `caller_capability_request_missing_or_malformed` marker the
    # companion negative test below asserts) proves the producer
    # subprocess was actually invoked and returned structured reasons (as
    # opposed to the zero-blockers `environment_failure` shape a
    # missing/malformed request would produce).
    assert any(
        "definitely_unsupported_operation_xyz" in blocker and "operation_route_unavailable" in blocker
        for blocker in parsed["blockers"]
    ), parsed["blockers"]
    assert any(blocker.startswith("spark:unavailable") for blocker in parsed["blockers"]), parsed["blockers"]
    assert not inner_marker.exists(), "run_refinement_preflight.py must never have started"


def test_canonical_executor_fails_closed_with_no_declared_capability_request(tmp_path: Path) -> None:
    """Companion negative case: with NONE of the three `LOOP_*` env vars
    set (the real bare-`preflight.run` registry argv has no CLI flag for
    them at all), `workflow_start_entry.py` must fail closed as
    `environment_failure` BEFORE ever invoking the producer subprocess --
    unlike the `checks`-populated blocked result above, `checks` here must
    be empty, proving the producer was never reached (requirement 2 of PR
    #2320 review's minimum 6 verification cases)."""
    repo = _make_repo(tmp_path)
    _install_real_capability_preflight_fixture(repo)
    inner_marker = tmp_path / "inner-ran.marker"

    result = _run_executor(repo, inner_marker, extra_env=None)

    assert result.returncode != 0, result.stdout + result.stderr
    # Issue #2323: same compact line-grammar shape as the positive case
    # above -- `BLOCKERS:` carries exactly the single typed
    # `caller_capability_request_missing_or_malformed` marker (no
    # producer-specific reasons), proving the producer was never reached.
    parsed = _parse_compact_stdout(result.stdout)
    assert parsed["status"] == "blocked"
    assert parsed["blockers"] == ["caller_capability_request_missing_or_malformed"]
    assert not inner_marker.exists()
