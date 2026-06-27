"""tests/agent_ops/test_git_ref_probe.py — contract tests for git_ref_probe.py (Issue #1197).

Covers:
- AC2: GIT_REF_PROBE_RESULT_V1 JSON contract (json_contract)
- AC4: stderr ≤ 5 lines, no raw commands/secrets (stderr)
- AC5: fixtures for local exists/missing, remote-tracking exists/missing,
       configured upstream missing/gone, invalid branch name (fixtures)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "agent-ops" / "git_ref_probe.py"

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


def _run_probe(repo: Path, branch: str, extra_args: list[str] | None = None) -> dict:
    cmd = [sys.executable, str(SCRIPT), "--branch", branch, "--json"]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    return json.loads(result.stdout), result


# ─── AC2: JSON contract ────────────────────────────────────────────────────────


class TestJsonContract:
    def test_json_contract_schema_key_present(self, temp_repo: Path) -> None:
        """AC2: schema field must be GIT_REF_PROBE_RESULT_V1."""
        data, _ = _run_probe(temp_repo, "main")
        assert data["schema"] == "GIT_REF_PROBE_RESULT_V1"

    def test_json_contract_required_keys_present(self, temp_repo: Path) -> None:
        """AC2: all required keys must be present."""
        data, _ = _run_probe(temp_repo, "main")
        assert "branch" in data
        assert "local" in data
        assert "remote" in data
        assert "upstream" in data
        assert "errors" in data

    def test_json_contract_local_keys_present(self, temp_repo: Path) -> None:
        """AC2: local sub-object must have exists, ref, oid."""
        data, _ = _run_probe(temp_repo, "main")
        local = data["local"]
        assert "exists" in local
        assert "ref" in local
        assert "oid" in local

    def test_json_contract_remote_keys_present(self, temp_repo: Path) -> None:
        """AC2: remote sub-object must have mode, ref, exists, oid."""
        data, _ = _run_probe(temp_repo, "main")
        remote = data["remote"]
        assert "mode" in remote
        assert "ref" in remote
        assert "exists" in remote
        assert "oid" in remote

    def test_json_contract_remote_mode_is_fixed_value(self, temp_repo: Path) -> None:
        """AC2: remote.mode must be 'origin_branch' or 'configured_upstream'."""
        data, _ = _run_probe(temp_repo, "main")
        assert data["remote"]["mode"] in ("origin_branch", "configured_upstream")

    def test_json_contract_upstream_keys_present(self, temp_repo: Path) -> None:
        """AC2: upstream sub-object must have configured and track."""
        data, _ = _run_probe(temp_repo, "main")
        upstream = data["upstream"]
        assert "configured" in upstream
        assert "track" in upstream

    def test_json_contract_stdout_is_single_json_object(self, temp_repo: Path) -> None:
        """AC2/AC4: stdout must be exactly one JSON object."""
        cmd = [sys.executable, str(SCRIPT), "--branch", "main", "--json"]
        result = subprocess.run(cmd, cwd=str(temp_repo), capture_output=True, text=True)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert isinstance(obj, dict)

    def test_json_contract_errors_is_list(self, temp_repo: Path) -> None:
        """AC2: errors must be a list."""
        data, _ = _run_probe(temp_repo, "main")
        assert isinstance(data["errors"], list)

    def test_json_contract_branch_field_echoed(self, temp_repo: Path) -> None:
        """AC2: branch field must echo the requested branch."""
        data, _ = _run_probe(temp_repo, "main")
        assert data["branch"] == "main"


# ─── AC4: stderr constraints ──────────────────────────────────────────────────


class TestStderr:
    def test_stderr_max_5_lines_on_missing_branch(self, temp_repo: Path) -> None:
        """AC4: stderr must be ≤ 5 lines even for missing branch."""
        cmd = [sys.executable, str(SCRIPT), "--branch", "nonexistent-branch-xyz", "--json"]
        result = subprocess.run(cmd, cwd=str(temp_repo), capture_output=True, text=True)
        stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
        assert len(stderr_lines) <= 5

    def test_no_raw_absolute_path_in_stderr(self, temp_repo: Path) -> None:
        """AC4: stderr must not contain raw absolute paths."""
        cmd = [sys.executable, str(SCRIPT), "--branch", "nonexistent-xyz", "--json"]
        result = subprocess.run(cmd, cwd=str(temp_repo), capture_output=True, text=True)
        # Absolute paths would look like /home/... or /usr/...
        import re

        for line in result.stderr.splitlines():
            assert not re.search(r"(?<!\w)/[a-zA-Z]{2,}/[^\s]+", line), f"Raw absolute path found in stderr: {line!r}"

    def test_exit_code_zero_on_success(self, temp_repo: Path) -> None:
        """AC4: exit code must be 0 on successful probe."""
        _, result = _run_probe(temp_repo, "main")
        assert result.returncode == 0


# ─── AC5: fixtures ────────────────────────────────────────────────────────────


class TestFixtures:
    def test_local_branch_exists(self, temp_repo: Path) -> None:
        """AC5: local.exists=True for an existing local branch."""
        data, _ = _run_probe(temp_repo, "main")
        assert data["local"]["exists"] is True
        assert data["local"]["ref"] == "refs/heads/main"
        assert data["local"]["oid"] is not None
        assert len(data["local"]["oid"]) >= 7

    def test_local_branch_missing(self, temp_repo: Path) -> None:
        """AC5: local.exists=False for a non-existent branch."""
        data, _ = _run_probe(temp_repo, "no-such-branch-xyz")
        assert data["local"]["exists"] is False
        assert data["local"]["ref"] is None
        assert data["local"]["oid"] is None

    def test_remote_tracking_missing(self, temp_repo: Path) -> None:
        """AC5: remote.exists=False when no remote-tracking ref exists."""
        data, _ = _run_probe(temp_repo, "main")
        # No git fetch done, so origin/main remote-tracking ref doesn't exist
        assert data["remote"]["exists"] is False
        assert data["remote"]["oid"] is None

    def test_remote_tracking_exists(self, temp_repo: Path, tmp_path: Path) -> None:
        """AC5: remote.exists=True when a remote-tracking ref has been fetched."""
        # Clone from the repo to get a real remote-tracking ref
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", str(temp_repo), str(clone)],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        data, _ = _run_probe(clone, "main")
        # After clone, origin/main exists
        assert data["remote"]["exists"] is True
        assert data["remote"]["oid"] is not None

    def test_configured_upstream_missing(self, temp_repo: Path) -> None:
        """AC5: upstream.configured=False when no upstream is configured."""
        data, _ = _run_probe(temp_repo, "main")
        # No upstream configured for main in bare repo
        assert isinstance(data["upstream"]["configured"], bool)
        # In the temp_repo, no upstream is set → configured=False
        assert data["upstream"]["configured"] is False

    def test_configured_upstream_exists(self, temp_repo: Path, tmp_path: Path) -> None:
        """AC5: upstream.configured=True when upstream tracking is set."""
        clone = tmp_path / "clone_upstream"
        subprocess.run(
            ["git", "clone", str(temp_repo), str(clone)],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        data, _ = _run_probe(clone, "main")
        # After clone, main tracks origin/main
        assert data["upstream"]["configured"] is True

    def test_invalid_branch_name(self, temp_repo: Path) -> None:
        """B3/AC5: invalid branch name (contains ..) → validation error, local.exists=False."""
        data, result = _run_probe(temp_repo, "invalid..branch")
        assert isinstance(data, dict)
        assert data.get("schema") == "GIT_REF_PROBE_RESULT_V1"
        assert data["local"]["exists"] is False
        assert any("invalid branch name" in e for e in data.get("errors", []))

    def test_branch_with_whitespace_in_name_handled(self, temp_repo: Path) -> None:
        """B3/AC5: branch name with special chars → validation error, no shell injection."""
        data, result = _run_probe(temp_repo, "branch with spaces")
        assert isinstance(data, dict)
        assert data["local"]["exists"] is False
        assert any("invalid branch name" in e for e in data.get("errors", []))

    def test_invalid_branch_main_tilde(self, temp_repo: Path) -> None:
        """B3: ancestor expression (main~0) is not a valid branch name."""
        data, result = _run_probe(temp_repo, "main~0")
        assert isinstance(data, dict)
        assert data["local"]["exists"] is False
        assert any("invalid branch name" in e for e in data.get("errors", []))

    def test_invalid_branch_caret_object(self, temp_repo: Path) -> None:
        """B3: object expression (main^{commit}) is not a valid branch name."""
        data, result = _run_probe(temp_repo, "main^{commit}")
        assert isinstance(data, dict)
        assert data["local"]["exists"] is False
        assert any("invalid branch name" in e for e in data.get("errors", []))

    def test_remote_mode_configured_upstream_after_clone(self, temp_repo: Path, tmp_path: Path) -> None:
        """H1: remote.mode is 'configured_upstream' when remote matches git upstream tracking."""
        clone = tmp_path / "clone_h1"
        import subprocess

        subprocess.run(
            ["git", "clone", str(temp_repo), str(clone)],
            capture_output=True,
            text=True,
            env=_GIT_ENV,
        )
        data, _ = _run_probe(clone, "main")
        # After clone, main tracks origin/main → mode must be "configured_upstream"
        assert data["remote"]["mode"] == "configured_upstream"

    def test_remote_mode_origin_branch_without_tracking(self, temp_repo: Path) -> None:
        """H1: remote.mode is 'origin_branch' when no upstream tracking is configured."""
        data, _ = _run_probe(temp_repo, "main")
        # No upstream configured → falls back to "origin_branch"
        assert data["remote"]["mode"] == "origin_branch"
