"""test_run_graphify_cli_advisory.py — deterministic tests for the Graphify CLI
advisory wrapper (Issue #2009, AC10; hardened per PR #2010 OWNER REQUEST_CHANGES).

All tests use an injectable fake runner and a temporary git worktree. None
depend on PyPI, network access, or a real ``graphify``/``uvx`` executable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPT_DIR))

import run_graphify_cli_advisory as rgca  # noqa: E402

_VERSION_STDOUT = "graphify 0.9.34"


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


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _resolved(repo: Path) -> Path:
    return repo.resolve(strict=True)


def _write_marker(
    repo: Path,
    head_sha: str,
    target_path: str = ".",
    version: str = _VERSION_STDOUT,
) -> Path:
    output_dir = repo / "tmp" / "graphify" / head_sha
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "provenance.json").write_text(
        json.dumps(
            {
                "repo_root": str(_resolved(repo)),
                "head_sha": head_sha,
                "target_path": target_path,
                "graphify_version": version,
            }
        ),
        encoding="utf-8",
    )
    return output_dir


def _write_graph(repo: Path, head_sha: str) -> Path:
    graph_dir = repo / "tmp" / "graphify" / head_sha / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph_file = graph_dir / "graph.json"
    graph_file.write_text("{}", encoding="utf-8")
    return graph_file


def _fake_runner(
    argv_recorder: list,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    version_stdout: str = _VERSION_STDOUT,
    version_returncode: int = 0,
):
    """Fake runner that answers the version preflight call automatically and
    the actual action call with the configured returncode/stdout/stderr.
    Every wrapper action (including "version" itself) issues at least one
    ``--version`` preflight call before doing anything else.
    """

    def runner(argv, env, timeout_sec, cwd=None):
        argv_recorder.append(list(argv))
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, returncode=version_returncode, stdout=version_stdout, stderr="")
        return subprocess.CompletedProcess(argv, returncode=returncode, stdout=stdout, stderr=stderr)

    return runner


def _timeout_runner():
    def runner(argv, env, timeout_sec, cwd=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_sec)

    return runner


def _launch_failure_runner():
    def runner(argv, env, timeout_sec, cwd=None):
        raise OSError("no such file or directory")

    return runner


def _runtime_error_runner():
    def runner(argv, env, timeout_sec, cwd=None):
        raise RuntimeError("unexpected upstream failure")

    return runner


# ---------------------------------------------------------------------------
# 1. exact package/version
# ---------------------------------------------------------------------------


def test_uses_exact_pinned_package_spec(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
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
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "ok"
    assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 3. dirty worktree falls back to existing route (skip)
# ---------------------------------------------------------------------------


def test_dirty_worktree_falls_back(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=head_sha)
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "unavailable"
    assert result.reason == "dirty_worktree"
    assert result.fallback_to_existing_route is True
    assert recorded == []  # no subprocess launched at all


# ---------------------------------------------------------------------------
# 4. output path confined to tmp/graphify/<head-sha>/
# ---------------------------------------------------------------------------


def test_output_path_confined_to_tmp_graphify_head_sha(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "ok"
    expected_dir = _resolved(repo) / "tmp" / "graphify" / head_sha
    assert result.output_dir == str(expected_dir)
    real_argv = recorded[-1]
    assert "--out" in real_argv
    out_idx = real_argv.index("--out")
    assert real_argv[out_idx + 1] == str(expected_dir)
    # No other location (e.g. repo-root graphify-out/) was created.
    assert not (repo / "graphify-out").exists()


def test_extract_argv_places_target_path_immediately_after_extract(tmp_path: Path):
    """AC/P1-1 regression: Graphify 0.9.34's parser treats sys.argv[2] (the
    token right after the subcommand) as the positional path; if it starts
    with ``-`` the path is treated as unspecified and never recovered later.
    """
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "ok"
    real_argv = recorded[-1]
    resolved_target = str(_resolved(repo))
    assert real_argv[real_argv.index("extract") + 1] == resolved_target
    assert real_argv.index(resolved_target) < real_argv.index("--code-only")


def test_invalid_head_sha_format_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="version", repo_root=repo, head_sha="../../etc/passwd"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "unavailable"
    assert result.reason == "head_sha_mismatch"
    assert recorded == []


def test_head_sha_not_matching_actual_head_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    fake_but_well_formed_sha = "0" * 40
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=fake_but_well_formed_sha)
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "unavailable"
    assert result.reason == "head_sha_mismatch"
    assert recorded == []


# ---------------------------------------------------------------------------
# repo_root identity verification (P1-2)
# ---------------------------------------------------------------------------


def test_repo_root_not_matching_git_toplevel_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    non_toplevel = repo / "sub"
    non_toplevel.mkdir()
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=non_toplevel, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "unavailable"
    assert result.reason == "repo_root_invalid"
    assert recorded == []


def test_repo_root_that_does_not_exist_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    missing = tmp_path / "does-not-exist"
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=missing, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "unavailable"
    assert result.reason == "repo_root_invalid"
    assert recorded == []


# ---------------------------------------------------------------------------
# target_path containment (P1-2)
# ---------------------------------------------------------------------------


def test_target_path_outside_repo_root_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="extract", repo_root=repo, head_sha=_head_sha(repo), target_path=str(outside)
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "unavailable"
    assert result.reason == "target_path_outside_repo_root"
    assert recorded == []


# ---------------------------------------------------------------------------
# symlinked output path component (P1-2)
# ---------------------------------------------------------------------------


def test_symlinked_output_path_component_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    outside_target = tmp_path / "outside-target"
    outside_target.mkdir()
    (repo / "tmp").mkdir()
    (repo / "tmp" / "graphify").symlink_to(outside_target, target_is_directory=True)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "unavailable"
    assert result.reason == "output_path_symlink"
    assert recorded == []
    assert not (outside_target / head_sha).exists()


# ---------------------------------------------------------------------------
# pre-existing regular file at the output dir location (P1-3)
# ---------------------------------------------------------------------------


def test_output_dir_pre_existing_regular_file_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    (repo / "tmp").mkdir()
    (repo / "tmp" / "graphify").mkdir()
    # A regular file sits where the wrapper needs a directory.
    (repo / "tmp" / "graphify" / head_sha).write_text("not a directory\n", encoding="utf-8")
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "unavailable"
    assert result.reason == "output_dir_unavailable"
    assert recorded == []


# ---------------------------------------------------------------------------
# 5. query output bounded by --budget
# ---------------------------------------------------------------------------


def test_query_always_passes_explicit_budget(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?", budget=500
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "ok"
    real_argv = recorded[-1]
    assert "--budget" in real_argv
    budget_idx = real_argv.index("--budget")
    assert real_argv[budget_idx + 1] == "500"


def test_query_budget_non_numeric_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?", budget="not-a-number"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "unavailable"
    assert result.reason == "invalid_budget"
    assert recorded == []


def test_query_budget_non_positive_is_rejected(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?", budget=0
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "unavailable"
    assert result.reason == "invalid_budget"
    assert recorded == []


def test_query_budget_above_max_is_clamped(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?", budget=999_999
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "ok"
    real_argv = recorded[-1]
    budget_idx = real_argv.index("--budget")
    assert real_argv[budget_idx + 1] == str(rgca._MAX_QUERY_BUDGET)


# ---------------------------------------------------------------------------
# 6. non-zero exit is advisory unavailable
# ---------------------------------------------------------------------------


def test_non_zero_exit_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="explain", repo_root=repo, head_sha=head_sha, node="foo")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, returncode=1, stderr="boom"))
    assert result.status == "unavailable"
    assert result.reason == "non_zero_exit"
    assert result.fallback_to_existing_route is True
    assert result.stderr_excerpt == "boom"


def test_version_preflight_mismatch_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=head_sha)
    result = rgca.run_graphify_advisory(
        req, runner=_fake_runner(recorded, version_stdout="graphify 0.9.99")
    )
    assert result.status == "unavailable"
    assert result.reason == "version_mismatch"
    assert result.version == "graphify 0.9.99"


# ---------------------------------------------------------------------------
# 7. timeout is advisory unavailable
# ---------------------------------------------------------------------------


def test_timeout_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_timeout_runner())
    assert result.status == "unavailable"
    assert result.reason == "timeout"


# ---------------------------------------------------------------------------
# 8. missing graph is advisory unavailable (query/path/explain)
# ---------------------------------------------------------------------------


def test_missing_graph_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=_head_sha(repo), question="anything?"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE x"))
    assert result.status == "unavailable"
    assert result.reason == "missing_graph"
    assert recorded == []  # no subprocess launched — graph absence checked first


def test_stale_provenance_marker_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    # Marker exists but references a different repo root than the current one.
    output_dir = repo / "tmp" / "graphify" / head_sha
    (output_dir / "provenance.json").write_text(
        json.dumps(
            {
                "repo_root": "/completely/different/repo",
                "head_sha": head_sha,
                "target_path": ".",
                "graphify_version": _VERSION_STDOUT,
            }
        ),
        encoding="utf-8",
    )
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="anything?"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE x"))
    assert result.status == "unavailable"
    assert result.reason == "stale_provenance"
    assert recorded == []


def test_extract_writes_provenance_marker_consumable_by_query(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    recorded: list = []
    extract_req = rgca.GraphifyAdvisoryRequest(
        action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo)
    )
    extract_result = rgca.run_graphify_advisory(extract_req, runner=_fake_runner(recorded, stdout="ok"))
    assert extract_result.status == "ok"

    marker_path = repo / "tmp" / "graphify" / head_sha / "provenance.json"
    assert marker_path.exists()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["repo_root"] == str(_resolved(repo))
    assert marker["head_sha"] == head_sha

    # A subsequent query against the same graph now succeeds because the
    # marker matches (missing_graph would fire without the marker).
    _write_graph(repo, head_sha)
    query_recorded: list = []
    query_req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?"
    )
    query_result = rgca.run_graphify_advisory(query_req, runner=_fake_runner(query_recorded, stdout="NODE foo"))
    assert query_result.status == "ok"


def test_launch_failure_is_advisory_unavailable(tmp_path: Path):
    repo = _init_repo(tmp_path)
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_launch_failure_runner())
    assert result.status == "unavailable"
    assert result.reason == "launch_failed"


def test_unexpected_runner_exception_is_advisory_internal_error(tmp_path: Path):
    """An injectable runner raising something other than OSError/TimeoutExpired
    (e.g. RuntimeError) must never propagate out of run_graphify_advisory
    (P1-3): the top-level fallback boundary converts it to unavailable.
    """
    repo = _init_repo(tmp_path)
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_runtime_error_runner())
    assert result.status == "unavailable"
    assert result.reason == "internal_error"


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
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
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
    head_sha = _head_sha(repo)  # HEAD moved after the lock commit

    before_lock = (repo / "uv.lock").read_text(encoding="utf-8")
    before_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout

    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
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
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    payload = result.to_dict()
    assert "schema" not in payload
    assert "schema_version" not in payload
    # No top-level key resembling a new repo-wide Graphify schema name.
    assert not any(k.upper().startswith("GRAPHIFY_") and k.upper().endswith("_V1") for k in payload)


# ---------------------------------------------------------------------------
# version readback is included for every action, not only action="version"
# ---------------------------------------------------------------------------


def test_version_is_populated_for_extract_action(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="extract", repo_root=repo, head_sha=head_sha, target_path=str(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="ok"))
    assert result.status == "ok"
    assert result.version == _VERSION_STDOUT


def test_version_is_populated_for_query_action(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="query", repo_root=repo, head_sha=head_sha, question="who calls foo?"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="NODE foo"))
    assert result.status == "ok"
    assert result.version == _VERSION_STDOUT


def test_version_is_populated_for_path_action(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(
        action="path", repo_root=repo, head_sha=head_sha, node_a="A", node_b="B"
    )
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="A -> B"))
    assert result.status == "ok"
    assert result.version == _VERSION_STDOUT


def test_version_is_populated_for_explain_action(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_sha = _head_sha(repo)
    _write_graph(repo, head_sha)
    _write_marker(repo, head_sha)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="explain", repo_root=repo, head_sha=head_sha, node="foo")
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded, stdout="explain foo"))
    assert result.status == "ok"
    assert result.version == _VERSION_STDOUT


def test_version_is_populated_for_version_action(tmp_path: Path):
    repo = _init_repo(tmp_path)
    recorded: list = []
    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=_fake_runner(recorded))
    assert result.status == "ok"
    assert result.version == _VERSION_STDOUT


# ---------------------------------------------------------------------------
# Query log disable env is always forced on, even against adversarial overrides
# ---------------------------------------------------------------------------


def test_query_log_disable_env_is_always_forced(tmp_path: Path):
    repo = _init_repo(tmp_path)
    seen_env: dict = {}

    def runner(argv, env, timeout_sec, cwd=None):
        seen_env.update(env)
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_VERSION_STDOUT, stderr="")

    req = rgca.GraphifyAdvisoryRequest(
        action="version",
        repo_root=repo,
        head_sha=_head_sha(repo),
        env_overrides={"GRAPHIFY_QUERY_LOG_ENABLE": "1"},
    )
    result = rgca.run_graphify_advisory(req, runner=runner)
    assert result.status == "ok"
    assert seen_env.get("GRAPHIFY_QUERY_LOG_DISABLE") == "1"


def test_query_log_disable_env_overrides_cannot_defeat_disable(tmp_path: Path):
    """An adversarial env_overrides trying every enable/disable-override
    combination must never leave query logging enabled (P1-5)."""
    repo = _init_repo(tmp_path)
    seen_env: dict = {}

    def runner(argv, env, timeout_sec, cwd=None):
        seen_env.update(env)
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_VERSION_STDOUT, stderr="")

    req = rgca.GraphifyAdvisoryRequest(
        action="version",
        repo_root=repo,
        head_sha=_head_sha(repo),
        env_overrides={
            "GRAPHIFY_QUERY_LOG_DISABLE": "0",
            "GRAPHIFY_QUERY_LOG_ENABLE": "1",
            "GRAPHIFY_QUERY_LOG": "/tmp/should-not-be-used.log",
            "GRAPHIFY_QUERY_LOG_RESPONSES": "1",
        },
    )
    result = rgca.run_graphify_advisory(req, runner=runner)
    assert result.status == "ok"
    assert seen_env.get("GRAPHIFY_QUERY_LOG_DISABLE") == "1"
    assert "GRAPHIFY_QUERY_LOG_ENABLE" not in seen_env
    assert "GRAPHIFY_QUERY_LOG" not in seen_env
    assert "GRAPHIFY_QUERY_LOG_RESPONSES" not in seen_env


# ---------------------------------------------------------------------------
# subprocess cwd is pinned to repo_root
# ---------------------------------------------------------------------------


def test_subprocess_cwd_is_pinned_to_repo_root(tmp_path: Path):
    repo = _init_repo(tmp_path)
    seen_cwd: list = []

    def runner(argv, env, timeout_sec, cwd=None):
        seen_cwd.append(cwd)
        return subprocess.CompletedProcess(argv, returncode=0, stdout=_VERSION_STDOUT, stderr="")

    req = rgca.GraphifyAdvisoryRequest(action="version", repo_root=repo, head_sha=_head_sha(repo))
    result = rgca.run_graphify_advisory(req, runner=runner)
    assert result.status == "ok"
    assert seen_cwd == [_resolved(repo)]


# ---------------------------------------------------------------------------
# CLI entrypoint smoke (uses injected fake runner via monkeypatch)
# ---------------------------------------------------------------------------


def test_cli_main_writes_json_to_stdout(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)

    def fake_run_graphify_advisory(request, runner=rgca._default_runner):
        return rgca.GraphifyAdvisoryResult(status="ok", action="version", version=_VERSION_STDOUT)

    monkeypatch.setattr(rgca, "run_graphify_advisory", fake_run_graphify_advisory)

    exit_code = rgca.main(
        [
            "--action",
            "version",
            "--repo-root",
            str(repo),
            "--head-sha",
            _head_sha(repo),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["version"] == _VERSION_STDOUT


def test_cli_main_never_raises_on_unexpected_dispatch_failure(tmp_path: Path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)

    def fake_run_graphify_advisory(request, runner=rgca._default_runner):
        raise RuntimeError("simulated unexpected dispatch failure")

    monkeypatch.setattr(rgca, "run_graphify_advisory", fake_run_graphify_advisory)

    exit_code = rgca.main(
        [
            "--action",
            "version",
            "--repo-root",
            str(repo),
            "--head-sha",
            _head_sha(repo),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "internal_error"


def test_cli_no_longer_exposes_output_file_or_graph_path_flags(tmp_path: Path):
    """P1-2 (item 4) removes the caller-supplied graph path override, and
    P1-2 (item 7) removes --output-file entirely (stdout-only)."""
    parser_help = subprocess.run(
        [sys.executable, str(_SCRIPT_DIR / "run_graphify_cli_advisory.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--output-file" not in parser_help
    assert "--graph-path" not in parser_help
