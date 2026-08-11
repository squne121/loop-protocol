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
    """Install the real (unmodified) privileged executor + policy module,
    plus a minimal stub `command_registry.py` / `decide_next_loop_action.py`
    pair -- the same "only the domain script is stubbed" convention already
    used by `_install_skill_runtime_exec_fixture` in
    `test_skill_runtime_exec_anchor.py` for `preflight.run.with_anchor`.
    """
    for rel in (
        "scripts/agent-guards/skill_runtime_exec.py",
        "scripts/agent-guards/skill_runtime_command_policy.py",
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
    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "decide.run": {
        "id": "decide.run",
        "argv": [
            "uv", "run", "python3",
            ".claude/skills/issue-refinement-loop/scripts/decide_next_loop_action.py",
            "--loop-state-file", "{loop_state_file}",
            "--review-result-verdict", "{verdict}",
            "--max-iterations", "{max_iterations}",
        ],
        "shell": False, "cwd_policy": "repo_root", "execution_class": "exact_skill_runtime_decide",
        "required_cwd": "canonical_main_root", "required_branch": "default_branch",
        "allowed_write_roots": [],
        "network_effect": "local_only",
        "placeholders": {
            "loop_state_file": {"type": "repo_relative_file", "required": True},
            "verdict": {"type": "verdict", "required": True},
            "max_iterations": {"type": "positive_int", "required": False},
        },
    },
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    return [str(values[token[1:-1]]) if token.startswith("{") else token for token in REGISTRY[command_id]["argv"]]
""",
    )
    _write_text(
        repo_root
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "decide_next_loop_action.py",
        """from __future__ import annotations
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--loop-state-file", required=True)
parser.add_argument("--review-result-verdict", required=True)
parser.add_argument("--max-iterations", type=int, required=True)
args = parser.parse_args()
state = json.loads(Path(args.loop_state_file).read_text())
print(json.dumps({
    "STATUS": "reached_real_subprocess",
    "loop_state_file": args.loop_state_file,
    "verdict": args.review_result_verdict,
    "max_iterations": args.max_iterations,
    "iteration": state.get("iteration"),
}))
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
    """AC10: decide.run must reach the real `decide_next_loop_action.py`
    subprocess through the unmodified production executor/policy dispatch
    path, not just be declared in the registry."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)

    result = _run_decide_executor(repo)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["STATUS"] == "reached_real_subprocess"
    assert payload["verdict"] == "needs-fix"
    assert payload["max_iterations"] == 3
    assert payload["iteration"] == 0


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
    (lane isolation, AC6-adjacent)."""
    repo = _make_repo(tmp_path)
    _install_decide_run_fixture(repo)
    (
        repo
        / ".claude"
        / "skills"
        / "issue-refinement-loop"
        / "scripts"
        / "command_registry.py"
    ).write_text(
        (
            repo
            / ".claude"
            / "skills"
            / "issue-refinement-loop"
            / "scripts"
            / "command_registry.py"
        )
        .read_text()
        .replace(
            'REGISTRY = {\n    "decide.run"',
            (
                'REGISTRY = {\n'
                '    "preflight.run": {\n'
                '        "id": "preflight.run",\n'
                '        "argv": [\n'
                '            "uv", "run", "python3",\n'
                '            ".claude/skills/issue-refinement-loop/scripts/'
                'run_refinement_preflight.py",\n'
                '            "--issue-number", "{issue_number}", "--repo", "{repo}",\n'
                '        ],\n'
                '        "shell": False, "cwd_policy": "repo_root",\n'
                '        "execution_class": "exact_skill_runtime",\n'
                '        "required_cwd": "canonical_main_root",\n'
                '        "required_branch": "default_branch",\n'
                '        "allowed_write_roots": '
                '[".claude/artifacts/issue-refinement-loop/{active_issue}/"],\n'
                '        "network_effect": "github_read_only",\n'
                '        "placeholders": {\n'
                '            "issue_number": {"type": "positive_int", "required": True},\n'
                '            "repo": {"type": "owner_repo", "required": True},\n'
                '        },\n'
                '    },\n'
                '    "decide.run"'
            ),
        )
    )

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
