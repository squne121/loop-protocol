"""
test_skill_runtime_policy_anchor.py

#2086 AC2/AC6/AC10 regression coverage.

AC10: `decide.run` was declared in `command_registry.py` (render_command /
argv / placeholders) but had no matching `eligible_command_ids` entry in
`skill_runtime_command_policy.py`, so it could never actually be dispatched
through the privileged `skill_runtime_exec.py` executor -- a
registry/policy-declaration-only false-green. This file exercises the real
exact-match parser (`parse_exact_skill_runtime_decide_command` /
`is_exact_skill_runtime_decide_executor_command`) both as pure unit tests
(malformed-shape matrix) and via a real subprocess chain through
`skill_runtime_exec.py` (mirroring the `_install_skill_runtime_exec_fixture`
convention already used by `test_skill_runtime_exec_anchor.py`).

AC2/AC6: the same trusted-repo / default-branch / canonical-root fail-closed
boundary that gates every other exact command class also gates `decide.run`
-- an operator-selected lane cannot bypass it, and unknown/mismatched
context is rejected before any subprocess is spawned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import skill_runtime_command_policy as policy  # noqa: E402


# ---------------------------------------------------------------------------
# Pure unit tests: parse_exact_skill_runtime_decide_command malformed-shape
# matrix (AC10). None of these should ever require a subprocess.
# ---------------------------------------------------------------------------


def _valid_decide_command() -> str:
    return (
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id decide.run "
        "--issue-number 2086 "
        "--repo squne121/loop-protocol "
        "--loop-state-file .claude/artifacts/issue-refinement-loop/2086/loop_state.json "
        "--review-result-verdict needs-fix "
        "--max-iterations 3"
    )


def test_decide_run_is_eligible_in_the_command_policy() -> None:
    """AC10: decide.run must actually be present in eligible_command_ids
    with the dedicated execution class -- the false-green this Issue fixes
    was command_registry.py declaring decide.run while this policy dict
    stayed silent about it."""
    entry = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"].get("decide.run")
    assert entry is not None
    assert entry["execution_class"] == policy.SKILL_RUNTIME_EXECUTION_CLASS_DECIDE
    assert "decide.run" in policy.ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS


def test_parse_exact_skill_runtime_decide_command_valid_shape(tmp_path: Path) -> None:
    parsed = policy.parse_exact_skill_runtime_decide_command(
        _valid_decide_command(), project_root=str(REPO_ROOT)
    )
    assert parsed is not None
    assert parsed.command_id == "decide.run"
    assert parsed.loop_state_file == (
        ".claude/artifacts/issue-refinement-loop/2086/loop_state.json"
    )
    assert parsed.verdict == "needs-fix"
    assert parsed.max_iterations == "3"


def test_parse_exact_skill_runtime_decide_command_rejects_wrong_token_count() -> None:
    truncated = _valid_decide_command().rsplit(" ", 2)[0]
    assert (
        policy.parse_exact_skill_runtime_decide_command(truncated, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_flag_reordering() -> None:
    swapped = _valid_decide_command().replace(
        "--review-result-verdict needs-fix --max-iterations 3",
        "--max-iterations 3 --review-result-verdict needs-fix",
    )
    assert (
        policy.parse_exact_skill_runtime_decide_command(swapped, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_equals_form() -> None:
    equals_form = _valid_decide_command().replace(
        "--review-result-verdict needs-fix", "--review-result-verdict=needs-fix"
    )
    assert (
        policy.parse_exact_skill_runtime_decide_command(equals_form, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_path_traversal() -> None:
    traversal = _valid_decide_command().replace(
        ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
        "../../../etc/passwd",
    )
    assert (
        policy.parse_exact_skill_runtime_decide_command(traversal, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_absolute_loop_state_file() -> None:
    absolute = _valid_decide_command().replace(
        ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
        "/etc/passwd",
    )
    assert (
        policy.parse_exact_skill_runtime_decide_command(absolute, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_unknown_verdict() -> None:
    bad_verdict = _valid_decide_command().replace("needs-fix", "delete_everything")
    assert (
        policy.parse_exact_skill_runtime_decide_command(bad_verdict, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_non_digit_max_iterations() -> None:
    bad_iterations = _valid_decide_command().replace("--max-iterations 3", "--max-iterations nan")
    assert (
        policy.parse_exact_skill_runtime_decide_command(
            bad_iterations, project_root=str(REPO_ROOT)
        )
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_shell_metacharacters() -> None:
    injected = _valid_decide_command() + "; rm -rf /"
    assert (
        policy.parse_exact_skill_runtime_decide_command(injected, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_wrong_command_id() -> None:
    wrong_id = _valid_decide_command().replace("decide.run", "preflight.run")
    assert (
        policy.parse_exact_skill_runtime_decide_command(wrong_id, project_root=str(REPO_ROOT))
        is None
    )


def test_parse_exact_skill_runtime_decide_command_rejects_untrusted_repo() -> None:
    untrusted = _valid_decide_command().replace(
        "squne121/loop-protocol", "attacker/loop-protocol"
    )
    assert (
        policy.parse_exact_skill_runtime_decide_command(untrusted, project_root=str(REPO_ROOT))
        is None
    )


# ---------------------------------------------------------------------------
# Real subprocess chain (AC10): decide.run reaches decide_next_loop_action.py
# through the unmodified production skill_runtime_exec.py /
# skill_runtime_command_policy.py dispatch path.
# ---------------------------------------------------------------------------


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


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


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


def _install_decide_run_fixture(repo_root: Path) -> None:
    """Install the REAL (unmodified) privileged executor, policy module,
    `command_registry.py`, and `decide_next_loop_action.py` from the current
    repo -- #2086 AC10 P0 fix: this fixture previously stubbed out both
    `command_registry.py` (with hand-added `execution_class` etc. metadata
    the real registry entry was missing) AND `decide_next_loop_action.py`
    itself (replaced by a fake script that only echoed
    `STATUS: reached_real_subprocess`), which meant this test could never
    detect a `validate_registry_entry()` rejection of the real `decide.run`
    entry or any real behavior of the real router. Only
    `scripts/agent-ops/worktree_catalog.py` remains a minimal local stub
    (it depends on live worktree enumeration that is out of scope for this
    exact-command-dispatch contract test).
    """
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
    ):
        _write_text(repo_root / rel, (REPO_ROOT / rel).read_text())

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
    loop_state_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / "2086"
    loop_state_dir.mkdir(parents=True, exist_ok=True)
    (loop_state_dir / "loop_state.json").write_text(
        json.dumps({"iteration": 0, "max_iterations": 3})
    )
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install decide.run fixture", cwd=repo_root)


def _run_decide_executor(
    repo: Path,
    *,
    command_id: str = "decide.run",
    issue_number: str = "2086",
    repo_slug: str = "squne121/loop-protocol",
    loop_state_file: "str | None" = (
        ".claude/artifacts/issue-refinement-loop/2086/loop_state.json"
    ),
    verdict: "str | None" = "needs-fix",
    max_iterations: "str | None" = "3",
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id",
        command_id,
        "--issue-number",
        issue_number,
        "--repo",
        repo_slug,
    ]
    if loop_state_file is not None:
        argv += ["--loop-state-file", loop_state_file]
    if verdict is not None:
        argv += ["--review-result-verdict", verdict]
    if max_iterations is not None:
        argv += ["--max-iterations", max_iterations]
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )


def test_decide_run_reaches_real_subprocess(tmp_path: Path) -> None:
    """AC10: decide.run must reach the REAL `decide_next_loop_action.py`
    subprocess (unmodified, copied verbatim from the current repo) through
    the unmodified production executor/policy dispatch path AND the REAL
    `command_registry.py` `decide.run` entry -- not a stub replacement of
    either. If the real registry entry is missing
    `execution_class`/`required_cwd`/`required_branch`/`allowed_write_roots`
    (the #2086 AC10 P0 false-green this test previously hid), this test
    fails at `validate_registry_entry()` before any subprocess is spawned.

    With loop_state {iteration: 0, max_iterations: 3} and
    review-result-verdict=needs-fix, the real router's bounded #1873 verdict
    routing (decide_next_action Priority 5) resolves to
    `STATUS: pass` / `NEXT_ACTION: continue_to_step_4`, exit code 0 —
    asserted against the router's real `STATUS:`/`NEXT_ACTION:` stdout
    contract (`_format_output`), not a fabricated JSON payload."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = _run_decide_executor(repo)
    assert result.returncode == 0, result.stderr
    stdout_lines = result.stdout.strip().splitlines()
    assert "STATUS: pass" in stdout_lines, result.stdout
    assert "NEXT_ACTION: continue_to_step_4" in stdout_lines, result.stdout
    assert not any(line.startswith("BLOCKERS:") for line in stdout_lines), result.stdout


def test_decide_run_rejects_missing_loop_state_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = _run_decide_executor(repo, loop_state_file=None)
    assert result.returncode == 2, result.stderr
    assert "loop-state-file" in result.stderr


def test_decide_run_rejects_missing_verdict(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = _run_decide_executor(repo, verdict=None)
    assert result.returncode == 2, result.stderr
    assert "review-result-verdict" in result.stderr


def test_decide_run_rejects_path_traversal_before_subprocess(tmp_path: Path) -> None:
    """A traversal attempt in --loop-state-file must never reach the
    subprocess -- the executor rejects it at the exact-match parser stage."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = _run_decide_executor(repo, loop_state_file="../../../etc/passwd")
    assert result.returncode == 2, result.stderr
    assert "rejected" in result.stderr


def test_fixture_flag_is_rejected_for_decide_run(tmp_path: Path) -> None:
    """`--fixture` is preflight.run.fixture-only; decide.run must reject it
    before any subprocess is spawned (lane isolation, AC6-adjacent)."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "decide.run",
            "--issue-number",
            "2086",
            "--repo",
            "squne121/loop-protocol",
            "--loop-state-file",
            ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
            "--review-result-verdict",
            "needs-fix",
            "--fixture",
            "tmp/fixture.json",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )
    assert result.returncode == 2, result.stderr


def test_decide_run_only_flags_rejected_for_preflight_run(tmp_path: Path) -> None:
    """`--loop-state-file`/`--review-result-verdict`/`--max-iterations` are
    decide.run-only; a plain command_id must reject them before dispatch
    (lane isolation, AC6-adjacent). Uses the REAL `command_registry.py`
    (installed by `_install_decide_run_fixture`), which already declares
    `preflight.run` -- no hand-injected registry mutation needed."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id",
            "preflight.run",
            "--issue-number",
            "2086",
            "--repo",
            "squne121/loop-protocol",
            "--loop-state-file",
            ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
            "--review-result-verdict",
            "needs-fix",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "only allowed for decide.run" in result.stderr


# ---------------------------------------------------------------------------
# #2086 AC9/AC10 (iteration 2): authority_transport.produce/consume real
# subprocess dispatch. Companion to `_install_decide_run_fixture` above --
# these two commands dispatch to `run_refinement_preflight.py`, not
# `decide_next_loop_action.py`, so this installs that script plus the real
# `.claude/skills/issue-refinement-loop/schemas/` directory it loads
# (`generate_authority_transport_manifest`/`consume_authority_transport`
# both fail-closed on a missing schema file, so a fixture without it could
# never demonstrate a genuine `status: ok` real-subprocess round trip).
# ---------------------------------------------------------------------------


def _install_authority_transport_fixture(repo_root: Path) -> None:
    import shutil

    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
    ):
        _write_text(repo_root / rel, (REPO_ROOT / rel).read_text())

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    # #2086 AC9/AC10: authority_transport.produce/consume are NOT
    # root-no-worktree eligible (unlike decide.run above), so the executor's
    # active-issue-worktree check requires a non-None entry here for the
    # fixture's own LOOP_ISSUE_NUMBER=2086 to resolve.
    return {"issue_number": issue_number, "path": root_realpath}
""",
    )
    schemas_src = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    schemas_dst = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    schemas_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(schemas_src, schemas_dst)

    worktree_dir = repo_root / ".claude" / "worktrees" / "issue-2086-authority-transport-fixture"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install authority_transport fixture", cwd=repo_root)


def _run_authority_transport_executor(repo: Path, extra_argv: list[str]) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        *extra_argv,
    ]
    return subprocess.run(
        argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(repo),
            "LOOP_ISSUE_NUMBER": "2086",
        },
        check=False,
    )


def test_authority_transport_produce_reaches_real_subprocess(tmp_path: Path) -> None:
    """AC9/AC10: `authority_transport.produce` must reach the REAL
    `run_refinement_preflight.py --produce-authority-transport` subprocess
    (unmodified, copied verbatim from the current repo) through the real
    executor/policy dispatch path AND the real `command_registry.py`
    `authority_transport.produce` entry. Before this Issue's wiring, this
    command_id was rejected unconditionally with
    'exact command class rejected' before any subprocess was spawned
    (`TestAuthorityTransportPrivilegedExecutorRealSubprocessDispatch` in
    `.claude/skills/issue-refinement-loop/tests/test_command_registry.py`,
    outside this Issue's Allowed Paths, pinned that prior rejection)."""
    repo = _make_repo(tmp_path)
    _install_authority_transport_fixture(repo)

    evidence_fixture = repo / "evidence.json"
    evidence_fixture.write_text(json.dumps({"source_kind": "generated_by_agent"}))

    result = _run_authority_transport_executor(
        repo,
        [
            "--command-id", "authority_transport.produce",
            "--issue-number", "2086",
            "--repo", "squne121/loop-protocol",
            "--invocation-id", "test-invocation-1",
            "--git-head-sha", "0123456789abcdef0123456789abcdef01234567",
            "--evidence-fixture-path", "evidence.json",
        ],
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok", payload
    assert payload["manifest"]["invocation_id"] == "test-invocation-1"


def test_authority_transport_consume_reaches_real_subprocess(tmp_path: Path) -> None:
    """AC9/AC10: `authority_transport.consume` must reach the REAL
    `run_refinement_preflight.py --consume-authority-transport` subprocess.
    A nonexistent router receipt path is intentionally supplied -- the real
    script's own fail-closed `missing_file` handling (not this Issue's
    concern) proves real dispatch occurred, distinguishing this from the
    privileged executor's own pre-dispatch 'exact command class rejected'
    rejection."""
    repo = _make_repo(tmp_path)
    _install_authority_transport_fixture(repo)

    result = _run_authority_transport_executor(
        repo,
        [
            "--command-id", "authority_transport.consume",
            "--issue-number", "2086",
            "--repo", "squne121/loop-protocol",
            "--invocation-id", "test-invocation-1",
            "--git-head-sha", "0123456789abcdef0123456789abcdef01234567",
            "--router-receipt-path",
            ".claude/artifacts/issue-refinement-loop/2086/authority-transport/"
            "test-invocation-1/nonexistent_receipt.json",
        ],
    )
    assert "exact command class rejected" not in result.stderr, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment_failure", payload
    assert payload["reason_code"] == "missing_file", payload
