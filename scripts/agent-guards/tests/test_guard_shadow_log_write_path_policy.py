from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


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
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.guard_shadow_log.jsonl\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _skill_runtime_exec_rel() -> str:
    return "scripts/agent-guards/" + "skill_runtime_exec" + ".py"


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        _skill_runtime_exec_rel(),
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

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
    rendered = []
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
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    sleep_seconds = os.environ.get("SKILL_RUNTIME_TEST_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))

    shadow_log_path = Path(".guard_shadow_log.jsonl")
    mutate = os.environ.get("SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE")
    if mutate:
        if mutate == "symlink":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
            shadow_log_path.symlink_to("/etc/hostname")
        elif mutate == "directory":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
            shadow_log_path.mkdir()
        elif mutate == "delete":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
        elif mutate == "truncate":
            shadow_log_path.write_text("")
        elif mutate == "overwrite":
            shadow_log_path.write_text(json.dumps({"schema_version": "1", "event": "overwritten"}) + "\\n")
        elif mutate == "malformed-append":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write("not-json-at-all\\n")
        elif mutate == "delete-record":
            lines = [line for line in shadow_log_path.read_text().splitlines() if line]
            remaining = lines[1:]
            shadow_log_path.write_text("".join(line + "\\n" for line in remaining))
        elif mutate == "self-append":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"schema_version": "1", "event": "self-append"}) + "\\n")

    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


def _run_executor(repo: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            _skill_runtime_exec_rel(),
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


def _append_after_delay(path: Path, content: str, delay_seconds: float) -> threading.Thread:
    def _worker() -> None:
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def _seed_shadow_log(repo: Path) -> Path:
    shadow_log = repo / ".guard_shadow_log.jsonl"
    shadow_log.write_text(json.dumps({"schema_version": "1", "event": "seed"}) + "\n")
    return shadow_log


# ---------------------------------------------------------------------------
# AC1: regular peer append(s) to .guard_shadow_log.jsonl (including multiple
# concurrent producers) must not trigger unauthorized_write_path.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_peer_append_does_not_block_preflight(tmp_path: Path) -> None:
    """GIVEN a pre-existing .guard_shadow_log.jsonl
    WHEN two independent peer hook producers concurrently append JSONL
    records to it while this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path,
    and the self-append made by the child command's own peer hooks must not
    be lost/reverted either."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)

    peer_1 = _append_after_delay(
        shadow_log,
        json.dumps({"schema_version": "1", "event": "peer-append-1"}) + "\n",
        delay_seconds=0.15,
    )
    peer_2 = _append_after_delay(
        shadow_log,
        json.dumps({"schema_version": "1", "event": "peer-append-2"}) + "\n",
        delay_seconds=0.3,
    )
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6",
                "SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append",
            },
        )
    finally:
        peer_1.join(timeout=5)
        peer_2.join(timeout=5)

    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    events = {json.loads(line)["event"] for line in lines}
    assert {"seed", "peer-append-1", "peer-append-2", "self-append"} <= events
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_guard_shadow_log_first_ever_creation_does_not_block_preflight(tmp_path: Path) -> None:
    """GIVEN .guard_shadow_log.jsonl does not exist yet (cold start)
    WHEN the child command's own peer hooks create it for the first time
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (absent -> regular is an authorized shadow-log kind transition)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = repo / ".guard_shadow_log.jsonl"
    assert not shadow_log.exists()

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append"})
    assert result.returncode == 0, result.stderr
    assert shadow_log.exists()


# ---------------------------------------------------------------------------
# AC2: non-regular kind substitution must still fail closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mutate", ["symlink", "directory"])
def test_guard_shadow_log_nonregular_substitution_still_fails_closed(tmp_path: Path, mutate: str) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is replaced by a symlink or a directory during the run
    THEN skill_runtime_exec.py must fail with unauthorized_write_path
    (this guarantee must NOT come from _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS,
    which never inspects transition kind at all)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_guard_shadow_log_delete_still_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is deleted (regular -> absent) during the run
    THEN skill_runtime_exec.py must fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "delete"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


# ---------------------------------------------------------------------------
# AC3: regular -> regular non-append-only content transitions must be
# rejected (truncate / overwrite / malformed JSONL / record deletion).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    ["truncate", "overwrite", "malformed-append", "delete-record"],
)
def test_guard_shadow_log_truncate_and_overwrite_rejected(tmp_path: Path, mutate: str) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN the on-disk content is truncated, overwritten, replaced with a
    malformed (non-JSON) appended line, or has an existing record removed
    THEN skill_runtime_exec.py must fail with unauthorized_write_path
    (append-only is enforced, not just after_kind == "regular")."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_guard_shadow_log_valid_append_succeeds(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN a single well-formed JSONL record is appended (append-only)
    THEN skill_runtime_exec.py must succeed (regression control for AC3
    negative cases above)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append"})
    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# AC6: behavior-based test that the guarantee is NOT implemented as a bare
# tuple addition to _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS.
# ---------------------------------------------------------------------------


def _load_skill_runtime_exec_module():
    agent_guards_dir = REPO_ROOT / "scripts" / "agent-guards"
    if str(agent_guards_dir) not in sys.path:
        sys.path.insert(0, str(agent_guards_dir))
    module_name = "skill_runtime_exec_under_test_issue_1563"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = agent_guards_dir / (_skill_runtime_exec_rel().rsplit("/", 1)[-1])
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_guard_shadow_log_is_not_a_directory_root_exclusion(tmp_path: Path) -> None:
    """GIVEN the production skill_runtime_exec.py module
    THEN .guard_shadow_log.jsonl must not be a member (exact or prefix) of
    _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS, AND (behavior-based, not just a
    tuple-membership assertion) a symlink substitution of
    .guard_shadow_log.jsonl must still be independently detected as
    unauthorized by _find_unauthorized_repo_changes -- if the guarantee had
    instead been implemented as a bare tuple addition to
    _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS, this symlink substitution would
    be silently pruned before kind inspection and this assertion would fail."""
    module = _load_skill_runtime_exec_module()

    assert module._SHADOW_LOG_EXACT_REL not in module._RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS
    assert not module._is_race_tolerant_unattributable_path(module._SHADOW_LOG_EXACT_REL)

    repo = _make_repo(tmp_path)
    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))
    (repo / module._SHADOW_LOG_EXACT_REL).symlink_to("/etc/hostname")

    unauthorized = module._find_unauthorized_repo_changes(
        str(repo),
        "1228",
        before_snapshot,
        before_status,
        shadow_log_before_kind="absent",
        shadow_log_before_bytes=None,
    )
    assert unauthorized == module._SHADOW_LOG_EXACT_REL


def test_guard_shadow_log_kind_transition_is_explicit_allow_tuple(tmp_path: Path) -> None:
    """Direct unit test of _is_allowed_shadow_log_kind_transition: only the
    three documented transitions are authorized; every other before/after
    kind combination is rejected (explicit allow-tuple match, not a
    postcondition-only after_kind == "regular" check)."""
    module = _load_skill_runtime_exec_module()

    authorized = {
        ("absent", "absent"),
        ("absent", "regular"),
        ("regular", "regular"),
    }
    kinds = ["absent", "regular", "symlink", "dir", "fifo", "socket", "device"]
    for before_kind in kinds:
        for after_kind in kinds:
            expected = (before_kind, after_kind) in authorized
            actual = module._is_allowed_shadow_log_kind_transition(before_kind, after_kind)
            assert actual == expected, (before_kind, after_kind)
