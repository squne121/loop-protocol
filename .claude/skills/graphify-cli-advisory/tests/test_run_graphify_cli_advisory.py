"""test_run_graphify_cli_advisory.py — deterministic tests for the Graphify CLI
advisory wrapper (Issue #2009, AC10).

All tests use an injectable fake runner and a temporary git worktree. None
depend on PyPI, network access, or a real ``graphify``/``uvx`` executable.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

import run_graphify_cli_advisory as rgca  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / ".gitignore").write_text("/tmp/\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _fake_runner(argv_recorder: list, returncode: int = 0, stdout: str = "", stderr: str = ""):
    def runner(argv, env, timeout_sec):
        argv_recorder.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode=returncode, stdout=stdout, stderr=stderr)

    return runner


def _timeout_runner():
    def runner(argv, env, timeout_sec):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_sec)

    return runner


def _launch_failure_runner():
    def runner(argv, env, timeout_sec):
        raise OSError("no such file or directory")

    return runner


# ---------------------------------------------------------------------------
# 1. exact package/version
# ---------------------------------------------------------------------------


def test_uses_exact_pinned_package_spec(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="graphify 0.9.34"))
    assert result.status == "ok"
    assert "--from" in recorded[0]
    idx = recorded[0].index("--from")
    assert recorded[0][idx + 1] == "graphifyy==0.9.34"


# ---------------------------------------------------------------------------
# 2. clean worktree only executes
# ---------------------------------------------------------------------------


def test_clean_worktree_executes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="graphify 0.9.34"))
    assert result.status == "ok"
    assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 3. dirty worktree falls back to existing route (skip)
# ---------------------------------------------------------------------------


def test_dirty_worktree_falls_back(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="graphify 0.9.34"))
    assert result.status == "unavailable"
    assert result.reason == "dirty_worktree"
    assert result.fallback_to_existing_route is True
    assert recorded == []  # no subprocess launched at all


# ---------------------------------------------------------------------------
# 4. output path confined to tmp/graphify/<head-sha>/
# ---------------------------------------------------------------------------


def test_output_path_confined_to_tmp_graphify_head_sha(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha="deadbeef", target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "ok"
    expected_dir = repo / "tmp" / "graphify" / "deadbeef"
    assert result.output_dir == str(expected_dir)
    assert "--out" in recorded[0]
    out_idx = recorded[0].index("--out")
    assert recorded[0][out_idx + 1] == str(expected_dir)
    # No other location (e.g. repo-root graphify-out/) was created.
    assert not (repo / "graphify-out").exists()


def test_output_path_sanitizes_head_sha(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="extract",
        repo_root=repo,
        head_sha="../../etc/passwd",
        target_path=str(repo),
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "ok"
    assert result.output_dir is not None
    assert ".." not in result.output_dir
    assert str(repo / "tmp" / "graphify") in result.output_dir


# ---------------------------------------------------------------------------
# 5. query output bounded by --budget
# ---------------------------------------------------------------------------


def test_query_always_passes_explicit_budget(tmp_path: Path):
    repo = _init_repo(tmp_path)
    graph_dir = repo / "tmp" / "graphify" / "abc123" / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph_file = graph_dir / "graph.json"
    graph_file.write_text("{}", encoding="utf-8")

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha="abc123", question="who calls foo?", budget=500
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "ok"
    assert "--budget" in recorded[0]
    budget_idx = recorded[0].index("--budget")
    assert recorded[0][budget_idx + 1] == "500"


# ---------------------------------------------------------------------------
# 6. non-zero exit is advisory unavailable
# ---------------------------------------------------------------------------


def test_non_zero_exit_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, returncode=1, stderr="boom"))
    assert result.status == "unavailable"
    assert result.reason == "non_zero_exit"
    assert result.fallback_to_existing_route is True


# ---------------------------------------------------------------------------
# 7. timeout is advisory unavailable
# ---------------------------------------------------------------------------


def test_timeout_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_timeout_runner())
    assert result.status == "unavailable"
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# 8. missing graph is advisory unavailable (query/path/explain)
# ---------------------------------------------------------------------------


def test_missing_graph_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="query", repo_root=repo, head_sha="abc123", question="anything?")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE x"))
    assert result.status == "unavailable"
    assert result.reason == "missing_graph"
    assert recorded == []  # no subprocess launched — graph absence checked first


def test_launch_failure_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_launch_failure_runner())
    assert result.status == "unavailable"
    assert result.reason == "launch_failed"


# ---------------------------------------------------------------------------
# 9. forbidden subcommands never launched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_action",
    ["install", "uninstall", "hook", "watch", "prs", "mcp", "serve", "clone"],
)
def test_forbidden_subcommands_never_launched(tmp_path: Path, forbidden_action: str):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action=forbidden_action, repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "unavailable"
    assert result.reason == "disallowed_subcommand"
    assert recorded == []


def test_allowlist_only_contains_permitted_actions():
    assert set(rgca._ALLOWED_SUBCOMMANDS) == {"extract", "query", "path", "explain", "version"}
    for forbidden in rgca._FORBIDDEN_TOKENS:
        assert forbidden not in rgca._ALLOWED_SUBCOMMANDS


# ---------------------------------------------------------------------------
# 10. stdout alone never produces a source-verified finding
# ---------------------------------------------------------------------------


def test_result_has_no_finding_or_verdict_fields(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="graphify 0.9.34"))
    payload = result.to_dict()
    forbidden_keys = {"finding", "verdict", "blocker", "allowed_paths", "review_verdict"}
    assert forbidden_keys.isdisjoint(payload.keys())
    assert result.fallback_to_existing_route is True


# ---------------------------------------------------------------------------
# 11. project .venv / uv.lock / tracked files untouched
# ---------------------------------------------------------------------------


def test_does_not_touch_project_venv_or_lock_or_tracked_files(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "uv.lock").write_text("locked\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "lock"], cwd=repo, check=True)

    before_lock = (repo / "uv.lock").read_text(encoding="utf-8")
    before_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha="abc123", target_path=str(repo))
    rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))

    after_lock = (repo / "uv.lock").read_text(encoding="utf-8")
    after_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert before_lock == after_lock
    assert before_status == after_status
    assert not (repo / ".venv" / "graphify-marker").exists()


# ---------------------------------------------------------------------------
# 12. existing CODEBASE_INVESTIGATION_RESULT_V1 shape is not overridden
# ---------------------------------------------------------------------------


def test_result_dict_is_plain_advisory_payload_not_a_new_schema(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha="abc123")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="graphify 0.9.34"))
    payload = result.to_dict()
    assert "schema" not in payload
    assert "schema_version" not in payload
    # No top-level key resembling a new repo-wide Graphify schema name.
    assert not any(k.upper().startswith("GRAPHIFY_") and k.upper().endswith("_V1") for k in payload)


# ---------------------------------------------------------------------------
# Query log disable env is always forced on
# ---------------------------------------------------------------------------


def test_query_log_disable_env_is_always_forced(tmp_path: Path):
    repo = _init_repo(tmp_path)
    seen_env: dict = {}

    def runner(argv, env, timeout_sec):
        seen_env.update(env)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="graphify 0.9.34", stderr="")

    req = rgca.GraphifyAdvisoryRequest(
        action="version",
        repo_root=repo,
        head_sha="abc123",
        env_overrides={"GRAPHIFY_QUERY_LOG_ENABLE": "1"},
    )
    result = rgca.run_graphify_advisory(req, runner=runner)
    assert result.status == "ok"
    assert seen_env.get("GRAPHIFY_QUERY_LOG_DISABLE") == "1"


# ---------------------------------------------------------------------------
# CLI entrypoint smoke (uses injected fake runner via monkeypatch)
# ---------------------------------------------------------------------------


def test_cli_main_writes_json_output_file(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    out_file = tmp_path / "result.json"

    def fake_run_graphify_advisory(request, runner=rgca._default_runner):
        return rgca.GraphifyAdvisoryResult(status="ok", action="version", version="graphify 0.9.34")

    monkeypatch.setattr(rgca, "run_graphify_advisory", fake_run_graphify_advisory)

    exit_code = rgca.main(
        [
            "--action",
            "version",
            "--repo-root",
            str(repo),
            "--head-sha",
            "abc123",
            "--output-file",
            str(out_file),
        ]
    )
    assert exit_code == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["status"] == "ok"
    assert payload["version"] == "graphify 0.9.34"
