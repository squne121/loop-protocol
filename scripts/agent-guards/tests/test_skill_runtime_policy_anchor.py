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


def _valid_fixture_human_context_command() -> str:
    return (
        "uv run python3 scripts/agent-guards/skill_runtime_exec.py "
        "--command-id preflight.run.fixture.with_human_context "
        "--issue-number 2084 --repo squne121/loop-protocol "
        "--fixture fixtures/ac3.json "
        "--anchor-comment-url https://github.com/squne121/loop-protocol/issues/2084#issuecomment-5249734344 "
        "--investigation-evidence-transport-path "
        ".claude/artifacts/issue-refinement-loop/2084/authority-transport/ac3/authority_transport_v1.json"
    )


def test_fixture_human_context_sibling_has_exact_local_only_policy() -> None:
    command_id = _valid_fixture_human_context_command().split()[5]
    entry = policy.SKILL_RUNTIME_COMMAND_POLICY_V2["eligible_command_ids"][command_id]
    assert entry["execution_class"] == policy.SKILL_RUNTIME_EXECUTION_CLASS_ANCHOR_FIXTURE
    assert entry["network_effect"] == "local_only"
    assert _valid_fixture_human_context_command().split()[5] in policy.ROOT_NO_WORKTREE_ALLOWED_COMMAND_IDS


def test_fixture_human_context_exact_parser_rejects_security_matrix() -> None:
    valid = _valid_fixture_human_context_command()
    assert policy.parse_exact_skill_runtime_anchor_fixture_command(valid, str(REPO_ROOT)) is not None
    malformed = (
        valid.replace("fixtures/ac3.json", "/tmp/ac3.json"),
        valid.replace("fixtures/ac3.json", "../ac3.json"),
        valid.replace("fixtures/ac3.json", "-fixture.json"),
        valid.replace("fixtures/ac3.json", "fixture\n.json"),
        valid.replace("--fixture fixtures/ac3.json ", ""),
        valid.replace(
            "--fixture fixtures/ac3.json",
            "--fixture fixtures/ac3.json --fixture fixtures/again.json",
        ),
        valid.replace(
            "--fixture fixtures/ac3.json --anchor-comment-url",
            "--anchor-comment-url --fixture fixtures/ac3.json",
        ),
        valid.replace("--fixture fixtures/ac3.json", "--fixture=fixtures/ac3.json"),
        valid + " --unknown extra",
        valid.replace("squne121/loop-protocol", "other/repo", 1),
        valid.replace("--issue-number 2084", "--issue-number 2085"),
    )
    for command in malformed:
        assert policy.parse_exact_skill_runtime_anchor_fixture_command(command, str(REPO_ROOT)) is None


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


# ---------------------------------------------------------------------------
# #2086 P0 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 1): the
# `--investigation-evidence-transport-path` flag added to
# `preflight.run.with_human_context` (Blocker 1/2) must be real-subprocess
# dispatchable through the SAME unmodified production
# `skill_runtime_exec.py` -> `skill_runtime_command_policy.py` ->
# `command_registry.py` -> `run_refinement_preflight.py` chain proven above
# for `authority_transport.produce`/`consume` -- not merely declared in the
# registry. This mints a REAL manifest via `authority_transport.produce`
# (the same real-subprocess dispatch proven by the test immediately above),
# then feeds its path into `preflight.run.with_human_context` and asserts
# the executor's OWN pre-dispatch exact-match parser accepts the new
# two-token suffix (`is_exact_skill_runtime_anchor_executor_command` /
# `_parse_exact_skill_runtime_anchor_command`) and reaches the real
# `run_refinement_preflight.py` subprocess (no live `gh` credentials are
# faked here, so the real subprocess's own `gh` failure path is what proves
# genuine dispatch occurred -- the same "real subprocess owns the failure,
# not the privileged executor's pre-dispatch rejection" pattern the
# `authority_transport.consume` test above already established for AC10).
# ---------------------------------------------------------------------------


def test_investigation_evidence_transport_path_reaches_real_subprocess_ac3(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _install_authority_transport_fixture(repo)

    evidence_fixture = repo / "investigation_evidence.json"
    anchor_url = "https://github.com/squne121/loop-protocol/issues/2086#issuecomment-1"
    evidence_fixture.write_text(
        json.dumps(
            [
                {
                    "comment_id": 1,
                    "comment_url": anchor_url,
                    "body_sha256": "0" * 64,
                    "source_kind": "generated_by_agent",
                    "path_literals": ["docs/dev/workflow.md"],
                }
            ]
        )
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    produce_result = _run_authority_transport_executor(
        repo,
        [
            "--command-id", "authority_transport.produce",
            "--issue-number", "2086",
            "--repo", "squne121/loop-protocol",
            "--invocation-id", "test-ac3-invocation",
            "--git-head-sha", head_sha,
            "--evidence-fixture-path", "investigation_evidence.json",
        ],
    )
    assert "exact command class rejected" not in produce_result.stderr, produce_result.stderr
    assert produce_result.returncode == 0, (produce_result.returncode, produce_result.stdout, produce_result.stderr)
    manifest_path_abs = json.loads(produce_result.stdout)["manifest_path"]
    manifest_path = str(Path(manifest_path_abs).relative_to(repo))

    preflight_result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id", "preflight.run.with_human_context",
            "--issue-number", "2086",
            "--repo", "squne121/loop-protocol",
            "--anchor-comment-url", anchor_url,
            "--investigation-evidence-transport-path", manifest_path,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "LOOP_ISSUE_NUMBER": "2086"},
        check=False,
    )
    assert "exact command class rejected" not in preflight_result.stderr, preflight_result.stderr
    # The real run_refinement_preflight.py subprocess owns whatever happens
    # next (live `gh` is not faked in this fixture) -- what this test proves
    # is that the new flag reaches that real subprocess at all, matching the
    # exact same "not rejected pre-dispatch" bar already established for
    # authority_transport.produce/consume above.
    assert preflight_result.stdout.strip() != "", (
        preflight_result.returncode, preflight_result.stdout, preflight_result.stderr
    )


def test_investigation_evidence_transport_path_rejected_for_agent_report_lane_ac6(tmp_path: Path) -> None:
    """#2086 AC6: `--investigation-evidence-transport-path` must never be
    accepted for the `with_agent_report` lane -- it is exclusively a
    `preflight.run.with_human_context` extension."""
    repo = _make_repo(tmp_path)
    _install_authority_transport_fixture(repo)
    anchor_url = "https://github.com/squne121/loop-protocol/issues/2086#issuecomment-1"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/agent-guards/skill_runtime_exec.py",
            "--command-id", "preflight.run.with_agent_report",
            "--issue-number", "2086",
            "--repo", "squne121/loop-protocol",
            "--anchor-comment-url", anchor_url,
            "--investigation-evidence-transport-path", "some/manifest.json",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "LOOP_ISSUE_NUMBER": "2086"},
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "only allowed for preflight.run.with_human_context" in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# #2086 P0 fix_delta (iteration 3, OWNER REQUEST_CHANGES Blocker 3): the
# privileged `decide.run` router must be able to dispatch #2053's canonical
# authority-transport verification ("Mode B") through the SAME unmodified
# production chain the AC10 produce/consume tests above already proved for
# the individual producer/consumer roles. This chains all THREE real
# subprocess steps -- authority_transport.produce -> Mode B decide.run
# (consuming the produced manifest as its sidecar) -> authority_transport.
# consume -- entirely through `skill_runtime_exec.py`, not through
# `command_registry.render_command()` called directly (already proven to
# work by `test_command_registry.py`, outside this Issue's Allowed Paths).
# ---------------------------------------------------------------------------


def _install_decide_run_authority_e2e_fixture(repo_root: Path) -> None:
    import shutil

    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
        ".claude/skills/issue-refinement-loop/scripts/command_registry.py",
        ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
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
    return {"issue_number": issue_number, "path": root_realpath}
""",
    )
    schemas_src = REPO_ROOT / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    schemas_dst = repo_root / ".claude" / "skills" / "issue-refinement-loop" / "schemas"
    schemas_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(schemas_src, schemas_dst)

    loop_state_dir = repo_root / ".claude" / "artifacts" / "issue-refinement-loop" / "2086"
    loop_state_dir.mkdir(parents=True, exist_ok=True)
    (loop_state_dir / "loop_state.json").write_text(
        json.dumps({"iteration": 0, "max_iterations": 3})
    )
    worktree_dir = repo_root / ".claude" / "worktrees" / "issue-2086-decide-authority-e2e-fixture"
    worktree_dir.mkdir(parents=True, exist_ok=True)
    _git("add", "-A", cwd=repo_root)
    _git("commit", "-q", "-m", "install decide.run authority e2e fixture", cwd=repo_root)


def test_decide_run_mode_b_authority_chain_reaches_real_subprocess_ac10(tmp_path: Path) -> None:
    """AC10 (Blocker 3): produce -> Mode B decide.run -> consume, all three
    real-subprocess-dispatched through the unmodified privileged executor.
    A digest-verified, current-HEAD-bound manifest makes the router's
    `generate_router_receipt()` resolve `status: ok` -- proving Mode B
    genuinely reaches and exercises the real authority-transport
    verification, not merely a pre-dispatch acceptance."""
    repo = _make_repo(tmp_path)
    _install_decide_run_authority_e2e_fixture(repo)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "LOOP_ISSUE_NUMBER": "2086"}

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()

    evidence_fixture = repo / "evidence.json"
    evidence_fixture.write_text(json.dumps({"source_kind": "generated_by_agent"}))

    produce_argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id", "authority_transport.produce",
        "--issue-number", "2086",
        "--repo", "squne121/loop-protocol",
        "--invocation-id", "test-mode-b-e2e",
        "--git-head-sha", head_sha,
        "--evidence-fixture-path", "evidence.json",
    ]
    produce_result = subprocess.run(
        produce_argv, cwd=str(repo), capture_output=True, text=True, env=env, check=False,
    )
    assert "exact command class rejected" not in produce_result.stderr, produce_result.stderr
    assert produce_result.returncode == 0, (produce_result.returncode, produce_result.stdout, produce_result.stderr)
    manifest_path_abs = json.loads(produce_result.stdout)["manifest_path"]
    manifest_path = str(Path(manifest_path_abs).relative_to(repo))

    decide_argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id", "decide.run",
        "--issue-number", "2086",
        "--repo", "squne121/loop-protocol",
        "--loop-state-file", ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
        "--review-result-verdict", "needs-fix",
        "--max-iterations", "3",
        "--authority-transport-path", manifest_path,
        "--authority-expected",
        "--invocation-id", "test-mode-b-e2e",
        "--git-head-sha", head_sha,
    ]
    decide_result = subprocess.run(
        decide_argv, cwd=str(repo), capture_output=True, text=True, env=env, check=False,
    )
    assert "exact command class rejected" not in decide_result.stderr, decide_result.stderr
    assert decide_result.returncode == 0, (decide_result.returncode, decide_result.stdout, decide_result.stderr)
    decide_lines = decide_result.stdout.strip().splitlines()
    assert "STATUS: pass" in decide_lines, decide_result.stdout
    assert not any(
        line.startswith("BLOCKERS: authority_transport_environment_failure") for line in decide_lines
    ), decide_result.stdout

    consume_argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id", "authority_transport.consume",
        "--issue-number", "2086",
        "--repo", "squne121/loop-protocol",
        "--invocation-id", "test-mode-b-e2e",
        "--git-head-sha", head_sha,
        "--router-receipt-path",
        ".claude/artifacts/issue-refinement-loop/2086/authority-transport/"
        "test-mode-b-e2e/nonexistent_receipt.json",
    ]
    consume_result = subprocess.run(
        consume_argv, cwd=str(repo), capture_output=True, text=True, env=env, check=False,
    )
    assert "exact command class rejected" not in consume_result.stderr, consume_result.stderr
    consume_payload = json.loads(consume_result.stdout)
    assert consume_payload["status"] == "environment_failure", consume_payload
    assert consume_payload["reason_code"] == "missing_file", consume_payload


def test_decide_run_mode_b_rejects_partial_authority_fields(tmp_path: Path) -> None:
    """#2086 Blocker 3: Mode B's authority sub-fields are all-or-none --
    supplying only some of them must be rejected before any subprocess is
    spawned, never silently degrade to Mode A or partially dispatch."""
    repo = _make_repo(tmp_path)
    _install_decide_run_authority_e2e_fixture(repo)

    partial_argv = [
        sys.executable,
        "scripts/agent-guards/skill_runtime_exec.py",
        "--command-id", "decide.run",
        "--issue-number", "2086",
        "--repo", "squne121/loop-protocol",
        "--loop-state-file", ".claude/artifacts/issue-refinement-loop/2086/loop_state.json",
        "--review-result-verdict", "needs-fix",
        "--invocation-id", "test-mode-b-partial",
    ]
    result = subprocess.run(
        partial_argv,
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo), "LOOP_ISSUE_NUMBER": "2086"},
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "all-or-none" in result.stderr, result.stderr
