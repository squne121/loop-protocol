"""tests/agent_ops/test_git_worktree_probe.py — contract tests for git_worktree_probe.py (Issue #1197).

Covers:
- AC3: GIT_WORKTREE_PROBE_RESULT_V1 JSON contract (json_contract)
- AC4: stderr ≤ 5 lines, stdout JSON-only (stderr)
- AC5: normal catalog, detached HEAD worktree, path with whitespace (fixtures)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "git_worktree_probe.py"

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        env=_GIT_ENV,
    )


@pytest.fixture()
def temp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://example.com/repo.git", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    return repo


def _run_probe(repo: Path) -> tuple[dict, subprocess.CompletedProcess[str]]:
    cmd = [sys.executable, str(SCRIPT), "--json"]
    result = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    data = json.loads(result.stdout)
    return data, result


# ─── AC3: JSON contract ────────────────────────────────────────────────────────

class TestJsonContract:
    def test_json_contract_schema_key_present(self, temp_repo: Path) -> None:
        """AC3: schema field must be GIT_WORKTREE_PROBE_RESULT_V1."""
        data, _ = _run_probe(temp_repo)
        assert data["schema"] == "GIT_WORKTREE_PROBE_RESULT_V1"

    def test_json_contract_entries_key_present(self, temp_repo: Path) -> None:
        """AC3: entries key must be present as a list."""
        data, _ = _run_probe(temp_repo)
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_json_contract_errors_key_present(self, temp_repo: Path) -> None:
        """AC3: errors key must be present as a list."""
        data, _ = _run_probe(temp_repo)
        assert "errors" in data
        assert isinstance(data["errors"], list)

    def test_json_contract_entry_schema_matches_catalog_entry_v1(self, temp_repo: Path) -> None:
        """AC3: each entry must be compatible with WORKTREE_CATALOG_ENTRY_V1."""
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        assert len(entries) >= 1  # at least the main worktree
        for entry in entries:
            assert "worktree_realpath" in entry
            assert "branch_ref" in entry
            assert "detached" in entry
            assert "exists_on_disk" in entry

    def test_json_contract_entry_has_head_field(self, temp_repo: Path) -> None:
        """AC3: entries should expose head (OID) field."""
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        assert len(entries) >= 1
        for entry in entries:
            # head may be None for detached, but key must exist
            assert "head" in entry

    def test_json_contract_entry_exists_on_disk_field(self, temp_repo: Path) -> None:
        """AC3: exists_on_disk must reflect whether the path is a directory."""
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        for entry in entries:
            wt_path = entry["worktree_realpath"]
            expected = os.path.isdir(wt_path)
            assert entry["exists_on_disk"] == expected, (
                f"exists_on_disk mismatch for {wt_path}"
            )

    def test_json_contract_stdout_is_single_json_object(self, temp_repo: Path) -> None:
        """AC3/AC4: stdout must be exactly one JSON object."""
        cmd = [sys.executable, str(SCRIPT), "--json"]
        result = subprocess.run(cmd, cwd=str(temp_repo), capture_output=True, text=True)
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert isinstance(obj, dict)


# ─── AC4: stderr constraints ──────────────────────────────────────────────────

class TestStderr:
    def test_stderr_max_5_lines_normal(self, temp_repo: Path) -> None:
        """AC4: stderr must be ≤ 5 lines for a normal repo."""
        _, result = _run_probe(temp_repo)
        stderr_lines = [l for l in result.stderr.splitlines() if l.strip()]
        assert len(stderr_lines) <= 5

    def test_exit_code_zero_on_success(self, temp_repo: Path) -> None:
        """AC4: exit code must be 0 on successful probe."""
        _, result = _run_probe(temp_repo)
        assert result.returncode == 0


# ─── AC5: fixtures ────────────────────────────────────────────────────────────

class TestFixtures:
    def test_normal_catalog_has_primary_worktree(self, temp_repo: Path) -> None:
        """AC5: normal catalog must include the primary worktree."""
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        assert len(entries) >= 1
        # Primary worktree should point to temp_repo
        real_temp = os.path.realpath(str(temp_repo))
        found = any(
            os.path.realpath(e["worktree_realpath"]) == real_temp
            for e in entries
        )
        assert found, f"Primary worktree {real_temp!r} not found in entries"

    def test_linked_worktree_appears_in_catalog(self, temp_repo: Path, tmp_path: Path) -> None:
        """AC5: a linked worktree added via git worktree add appears in the catalog."""
        linked = tmp_path / "linked"
        _git("worktree", "add", "-b", "feature-x", str(linked), "main", cwd=temp_repo)
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        real_linked = os.path.realpath(str(linked))
        found = any(
            os.path.realpath(e["worktree_realpath"]) == real_linked
            for e in entries
        )
        assert found, f"Linked worktree {real_linked!r} not found in catalog"

    def test_detached_worktree_detached_flag(self, temp_repo: Path, tmp_path: Path) -> None:
        """AC5: a detached worktree has detached=True."""
        # Get the HEAD commit sha
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(temp_repo), capture_output=True, text=True, env=_GIT_ENV,
        ).stdout.strip()
        linked = tmp_path / "detached_wt"
        _git("worktree", "add", "--detach", str(linked), head, cwd=temp_repo)
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        real_linked = os.path.realpath(str(linked))
        match = next(
            (e for e in entries if os.path.realpath(e["worktree_realpath"]) == real_linked),
            None,
        )
        assert match is not None, "Detached worktree not found in catalog"
        assert match["detached"] is True

    def test_path_with_spaces_in_worktree(self, temp_repo: Path, tmp_path: Path) -> None:
        """AC5: worktree at a path with spaces is handled correctly."""
        spaced = tmp_path / "path with spaces"
        spaced.mkdir()
        _git("worktree", "add", "-b", "feature-spaces", str(spaced), "main", cwd=temp_repo)
        data, _ = _run_probe(temp_repo)
        entries = data["entries"]
        real_spaced = os.path.realpath(str(spaced))
        found = any(
            os.path.realpath(e["worktree_realpath"]) == real_spaced
            for e in entries
        )
        assert found, f"Worktree with spaces in path not found in catalog"
