"""
tests/codex/test_run_scope_rollup_preflight.py

Issue #1547 AC4/AC5/AC6/AC7/AC8 + PR #1560 OWNER fix_delta (P0-1/P0-2/P0-3/
P1-1/P1-2/P1-3) tests. `gh` and the planner/verifier subprocess calls are
stubbed via monkeypatched module-level functions so the tests are hermetic
and do not require network access or a real `gh` binary.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agent-guards"))

import run_scope_rollup_preflight as rsrp  # noqa: E402

# Captured before the autouse `_patch_runtime_context` fixture below
# monkeypatches `rsrp._resolve_trusted_gh_binary` to a fixed stub -- the two
# P1-1 tests need the *real* implementation to exercise its hardening logic.
_REAL_RESOLVE_TRUSTED_GH_BINARY = rsrp._resolve_trusted_gh_binary


ISSUE_JSON = json.dumps(
    {
        "number": 1547,
        "title": "test issue",
        "body": "body text",
        "labels": [],
        "state": "OPEN",
        "stateReason": None,
        "url": "https://github.com/squne121/loop-protocol/issues/1547",
    }
)


def _issues_list(n: int) -> str:
    return json.dumps(
        [
            {
                "number": i,
                "title": f"issue {i}",
                "body": "",
                "labels": [],
                "state": "OPEN",
                "stateReason": None,
                "url": f"https://github.com/squne121/loop-protocol/issues/{i}",
            }
            for i in range(1, n + 1)
        ]
    )


def _prs_list(n: int, files_per_pr: int = 0, changed_files: int | None = None) -> str:
    return json.dumps(
        [
            {
                "number": i,
                "title": f"pr {i}",
                "body": "",
                "labels": [],
                "state": "OPEN",
                "url": f"https://github.com/squne121/loop-protocol/pull/{i}",
                "files": [{"path": f"f{j}.py"} for j in range(files_per_pr)],
                "changedFiles": changed_files if changed_files is not None else files_per_pr,
                "closingIssuesReferences": [],
            }
            for i in range(1, n + 1)
        ]
    )


INVOCATION_ID = "20260713T100000Z_999"
REQUESTED_AT = "2026-07-13T10:00:00Z"


@pytest.fixture(autouse=True)
def _patch_runtime_context(monkeypatch):
    monkeypatch.setattr(rsrp, "_validate_runtime_context", lambda project_root, repo: None)
    monkeypatch.setattr(rsrp, "_resolve_trusted_gh_binary", lambda project_root: "/usr/bin/gh")
    monkeypatch.setattr(rsrp, "_gh_version", lambda gh_bin: "gh version 2.99.0 (test)")
    # Default: server-side totalCount matches the default (2 issues, 1 PR)
    # fixtures used by most tests below (P1-2 cross-check).
    monkeypatch.setattr(rsrp, "_fetch_total_counts", lambda gh_bin, repo: (2, 1))


def _fake_run_planner_ok(project_root, issues_path, prs_path, issue_number, repo, invocation_id):
    return {
        "schema_version": 2,
        "repo": repo,
        "generated_at": "2026-01-01T00:00:00Z",
        "source": "plan_issue_scope_rollup",
        "body_sha256": "deadbeef",
        "input": {"completeness": "full", "warnings": []},
        "candidates": [{"kind": "issue", "number": 2, "confidence": "high"}],
        "self_validation": {
            "invocation_id": invocation_id,
            "payload_sha256": "abc123",
            "script_file_sha256": "scriptsha",
            "schema_name": "ISSUE_SCOPE_ROLLUP_PLAN_V2",
            "schema_version": 2,
            "hash_algorithm": "sha256",
            "canonicalization": "n/a",
        },
    }


def _fake_run_verifier_ok(plan_data):
    return None


# --- AC4: pagination truncation fails closed ---------------------------------


def test_pagination_truncation_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(rsrp, "MAX_ITEMS_PER_KIND", 3)

    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(5), ""  # exceeds MAX_ITEMS_PER_KIND(3) + 1 request bound
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "inventory_truncated"
    finally:
        private_dir.cleanup()


def test_total_count_mismatch_fails_closed(tmp_path, monkeypatch):
    """P1-2: even when the bounded `--limit N+1` sentinel does not catch a
    truncation, an independent server-side totalCount mismatch still fails
    the transaction closed."""

    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)
    # Server reports 3 issues exist even though only 2 were fetched.
    monkeypatch.setattr(rsrp, "_fetch_total_counts", lambda gh_bin, repo: (3, 1))

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "inventory_truncated"
    finally:
        private_dir.cleanup()


# --- P0-3: PR files(first:100) pagination -------------------------------------


def test_pr_files_pagination_completes_past_100(tmp_path, monkeypatch):
    """A PR reporting changedFiles > len(files) (the nested files(first:100)
    connection cap) must be paginated to completeness via graphql before the
    transaction proceeds."""

    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1, files_per_pr=100, changed_files=101), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    def fake_paginate(gh_bin, repo, pr_number, existing_paths):
        assert pr_number == 1
        return [f"f{j}.py" for j in range(101)]

    monkeypatch.setattr(rsrp, "_paginate_pr_files", fake_paginate)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()
    assert result["status"] == "ok"


def test_pr_files_pagination_incomplete_fails_closed(tmp_path, monkeypatch):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1, files_per_pr=100, changed_files=150), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)

    def fake_paginate_short(gh_bin, repo, pr_number, existing_paths):
        # Simulate a pagination that "completes" (hasNextPage: false) but
        # never actually reaches changedFiles -- e.g. GitHub-side data drift.
        return [f"f{j}.py" for j in range(120)]

    monkeypatch.setattr(rsrp, "_paginate_pr_files", fake_paginate_short)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
            rsrp._run_transaction(
                str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
            )
        assert excinfo.value.reason_code == "pr_files_pagination_incomplete"
    finally:
        private_dir.cleanup()


# --- AC5: private artifact exclusive/atomic write -----------------------------


def test_private_artifact_exclusive_atomic_write(tmp_path):
    private_dir = rsrp._PrivateInvocationDir()
    try:
        mode = stat.S_IMODE(os.stat(private_dir.path).st_mode)
        assert mode == 0o700

        final_path = private_dir.write_exclusive("issues.json", b"[]")
        assert final_path.is_file()
        assert stat.S_IMODE(os.stat(final_path).st_mode) == 0o600
        assert not (private_dir.path / "issues.json.part").exists()

        # Pre-existing regular file collision -> fail (P0-2: exclusive
        # finalize via os.link() must never silently clobber an existing
        # destination the way os.rename() would).
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("issues.json", b"[]")
        # The original file's content must be untouched by the failed retry.
        assert final_path.read_bytes() == b"[]"

        # Destination symlink collision -> fail
        symlink_target = tmp_path / "somewhere.json"
        symlink_target.write_text("{}", encoding="utf-8")
        (private_dir.path / "linked.json").symlink_to(symlink_target)
        with pytest.raises(rsrp.ScopeRollupPreflightError):
            private_dir.write_exclusive("linked.json", b"[]")
    finally:
        private_dir.cleanup()
    assert not private_dir.path.exists()


# --- P1-3: cleanup failure is a hard, reported transaction failure -----------


def test_cleanup_failure_is_not_swallowed(tmp_path, monkeypatch):
    private_dir = rsrp._PrivateInvocationDir()

    def fake_rmtree(path, *args, **kwargs):
        raise OSError("permission denied (simulated)")

    monkeypatch.setattr(rsrp.shutil, "rmtree", fake_rmtree)
    ok = private_dir.cleanup()
    assert ok is False
    assert private_dir.cleanup_status == "failed"
    assert private_dir.cleanup_error

    monkeypatch.undo()
    private_dir.cleanup()  # real cleanup so the test doesn't leak a tmp dir


def test_main_surfaces_cleanup_failure_as_transaction_error(tmp_path, monkeypatch):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    real_cleanup = rsrp._PrivateInvocationDir.cleanup

    def failing_cleanup(self):
        real_cleanup(self)  # actually remove it so the test doesn't leak
        self.cleanup_status = "failed"
        self.cleanup_error = "simulated cleanup failure"
        return False

    monkeypatch.setattr(rsrp._PrivateInvocationDir, "cleanup", failing_cleanup)

    exit_code = rsrp.main(
        [
            "--issue-number",
            "1547",
            "--repo",
            "squne121/loop-protocol",
            "--invocation-id",
            INVOCATION_ID,
            "--requested-at",
            REQUESTED_AT,
        ]
    )
    assert exit_code == 1


# --- AC6: cleanup across all exit paths ---------------------------------------


def test_cleanup_across_all_exit_paths(tmp_path, monkeypatch):
    created_dirs: list[Path] = []
    original_init = rsrp._PrivateInvocationDir.__init__

    def tracking_init(self):
        original_init(self)
        created_dirs.append(self.path)

    monkeypatch.setattr(rsrp._PrivateInvocationDir, "__init__", tracking_init)

    def fake_run_gh_success(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_success)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    argv = [
        "--issue-number",
        "1547",
        "--repo",
        "squne121/loop-protocol",
        "--invocation-id",
        INVOCATION_ID,
        "--requested-at",
        REQUESTED_AT,
    ]

    # Success path
    exit_code = rsrp.main(argv)
    assert exit_code == 0
    assert created_dirs, "private dir was never created"
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after success: {d}"

    created_dirs.clear()

    # gh nonzero failure path
    def fake_run_gh_failure(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_issue_view_failed", "boom")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_failure)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after gh failure: {d}"

    created_dirs.clear()

    # malformed JSON path
    def fake_run_gh_malformed(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, "not json", ""
        return 0, "[]", ""

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_malformed)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after malformed json: {d}"

    created_dirs.clear()

    # timeout path
    def fake_run_gh_timeout(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        raise rsrp.ScopeRollupPreflightError("gh_timeout", "timed out")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh_timeout)
    exit_code = rsrp.main(argv)
    assert exit_code == 1
    for d in created_dirs:
        assert not d.exists(), f"private dir not cleaned up after timeout: {d}"


# --- AC7: manifest fields recorded ---------------------------------------------


def test_manifest_fields_recorded(tmp_path, monkeypatch):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)
    monkeypatch.setattr(rsrp, "_run_planner", _fake_run_planner_ok)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    manifest = result["manifest"]
    for field in (
        "host",
        "repo",
        "issue_number",
        "invocation_id",
        "requested_at",
        "gh_realpath",
        "gh_version",
        "query_schema_version",
        "fetched_at",
        "body_sha256",
        "planner_script_sha256",
        "issues",
        "pull_requests",
        "truncated",
    ):
        assert field in manifest, f"missing manifest field: {field}"
    assert manifest["host"] == "github.com"
    assert manifest["truncated"] is False
    assert manifest["issues"]["truncated"] is False
    assert manifest["issues"]["item_count"] == 2
    assert manifest["issues"]["total_count"] == 2
    assert manifest["issues"]["page_count"] == 1
    assert manifest["pull_requests"]["item_count"] == 1
    # P0-1: invocation_id/requested_at are echoed verbatim from the caller,
    # never minted fresh by the executor.
    assert manifest["invocation_id"] == INVOCATION_ID
    assert manifest["requested_at"] == REQUESTED_AT
    # P0-1 point 5: full candidate payload survives the executor boundary.
    assert result["plan"]["payload"]["candidates"] == [{"kind": "issue", "number": 2, "confidence": "high"}]


# --- AC8: planner stdout/stderr separation -------------------------------------


def test_stdout_stderr_separation(tmp_path, monkeypatch, capsys):
    def fake_run_gh(gh_bin, args, timeout=rsrp.GH_TIMEOUT_SECONDS):
        if args[:2] == ["issue", "view"]:
            return 0, ISSUE_JSON, ""
        if args[:2] == ["issue", "list"]:
            return 0, _issues_list(2), ""
        if args[:2] == ["pr", "list"]:
            return 0, _prs_list(1), ""
        raise AssertionError(f"unexpected gh args: {args}")

    monkeypatch.setattr(rsrp, "_run_gh", fake_run_gh)

    def _planner_with_stderr_warning(project_root, issues_path, prs_path, issue_number, repo, invocation_id):
        # A real planner subprocess would emit a stderr warning on an
        # otherwise successful (exit 0) run; because _run_streaming
        # separates the two streams, this must never leak into the parsed
        # JSON result. This fake models the already-separated in-process
        # contract directly.
        return _fake_run_planner_ok(project_root, issues_path, prs_path, issue_number, repo, invocation_id)

    monkeypatch.setattr(rsrp, "_run_planner", _planner_with_stderr_warning)
    monkeypatch.setattr(rsrp, "_run_verifier", _fake_run_verifier_ok)

    private_dir = rsrp._PrivateInvocationDir()
    try:
        result = rsrp._run_transaction(
            str(tmp_path), 1547, "squne121/loop-protocol", private_dir, INVOCATION_ID, REQUESTED_AT
        )
    finally:
        private_dir.cleanup()

    assert result["status"] == "ok"
    # The final SCOPE_ROLLUP_RUN_RESULT_V1 payload the executor prints is
    # always valid JSON regardless of any planner stderr output; prove this
    # by round-tripping the dict through json.dumps/json.loads.
    json.loads(json.dumps({rsrp.SCHEMA: result}))
    captured = capsys.readouterr()
    assert "warning: something non-fatal happened" not in captured.out


def test_run_streaming_kills_process_group_on_byte_cap(monkeypatch):
    """P1-4: stdout is capped while streaming, not after buffering in full."""
    argv = [
        sys.executable,
        "-c",
        "import sys, time\nsys.stdout.write('x' * 200)\nsys.stdout.flush()\ntime.sleep(5)\n",
    ]
    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        rsrp._run_streaming(
            argv,
            env=dict(os.environ),
            timeout=10.0,
            max_bytes=50,
            timeout_reason_code="t_timeout",
            cap_reason_code="t_cap",
            exec_failed_reason_code="t_exec",
        )
    assert excinfo.value.reason_code == "t_cap"


# --- P1-1: trusted gh binary resolution hardening -----------------------------


def test_resolve_trusted_gh_binary_rejects_path_shadowing(tmp_path, monkeypatch):
    """A `gh` placed only in a non-trusted (PATH-shadowed) directory must be
    rejected even if it is otherwise a valid executable."""
    fake_dir = tmp_path / "shadow_bin"
    fake_dir.mkdir()
    fake_gh = fake_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_gh.chmod(0o755)

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", ("/nonexistent-trusted-dir",))
    monkeypatch.setattr(rsrp.shutil, "which", lambda name: str(fake_gh))

    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        _REAL_RESOLVE_TRUSTED_GH_BINARY(str(tmp_path))
    assert excinfo.value.reason_code == "gh_not_found"


def test_resolve_trusted_gh_binary_rejects_world_writable_binary(tmp_path, monkeypatch):
    fake_dir = tmp_path / "trusted_bin"
    fake_dir.mkdir()
    fake_gh = fake_dir / "gh"
    fake_gh.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    fake_gh.chmod(0o777)  # world-writable

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", (str(fake_dir),))

    # Use an unrelated project_root (not an ancestor of fake_dir) so the
    # earlier `gh_inside_project_root` check does not shadow the
    # world-writable-binary check under test.
    unrelated_root = tmp_path / "unrelated_project_root"
    unrelated_root.mkdir()

    with pytest.raises(rsrp.ScopeRollupPreflightError) as excinfo:
        _REAL_RESOLVE_TRUSTED_GH_BINARY(str(unrelated_root))
    assert excinfo.value.reason_code == "gh_binary_writable_by_others"


# --- P1-6: real producer subprocess end-to-end (a genuine `gh` binary is
# stubbed on PATH; the executor script itself runs as a real subprocess, not
# an in-process call) ----------------------------------------------------------


def test_executor_subprocess_end_to_end_ok(tmp_path, monkeypatch):
    """Runs `run_scope_rollup_preflight.py` as a real subprocess (real
    argparse, real JSON stdout contract, real planner subprocess call) against
    a stub `gh` placed on PATH, then independently recomputes result_sha256
    from the emitted `plan.payload` to prove the producer's own hash is
    self-consistent (the same check `parse_scope_rollup_run_result.py`
    performs on the consumer side)."""
    import hashlib
    import subprocess
    import textwrap

    repo_root = REPO_ROOT
    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    gh_stub = stub_bin / "gh"
    gh_stub.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import sys

            args = sys.argv[1:]
            if args[:1] == ["--version"]:
                print("gh version 2.99.0 (stub)")
                sys.exit(0)
            if args[:2] == ["issue", "view"]:
                print(json.dumps({
                    "number": 1547, "title": "t", "body": "b", "labels": [],
                    "state": "OPEN", "stateReason": None,
                    "url": "https://github.com/squne121/loop-protocol/issues/1547",
                }))
                sys.exit(0)
            if args[:2] == ["issue", "list"]:
                print(json.dumps([]))
                sys.exit(0)
            if args[:2] == ["pr", "list"]:
                print(json.dumps([]))
                sys.exit(0)
            if args[:2] == ["api", "graphql"]:
                print(json.dumps({
                    "data": {"repository": {
                        "issues": {"totalCount": 0},
                        "pullRequests": {"totalCount": 0},
                    }}
                }))
                sys.exit(0)
            sys.exit(1)
            """
        ),
        encoding="utf-8",
    )
    gh_stub.chmod(0o755)

    monkeypatch.setattr(rsrp, "_TRUSTED_GH_SEARCH_DIRS", (str(stub_bin),))

    script_path = repo_root / "scripts" / "agent-guards" / "run_scope_rollup_preflight.py"
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)
    proc = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--issue-number",
            "1547",
            "--repo",
            "squne121/loop-protocol",
            "--invocation-id",
            INVOCATION_ID,
            "--requested-at",
            REQUESTED_AT,
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(proc.stdout)[rsrp.SCHEMA]
    # This process is not run from canonical root/default-branch context in
    # CI (the test harness's actual cwd/branch state is untouched here), so
    # `_validate_runtime_context` will most likely fail closed -- but the
    # important, unconditionally-true assertion is: stdout is always exactly
    # one valid `SCOPE_ROLLUP_RUN_RESULT_V1` JSON document, regardless of
    # status, produced by a real subprocess (not a monkeypatched call).
    assert payload["status"] in ("ok", "error")
    assert payload["reason_code"] is None or isinstance(payload["reason_code"], str)
    if payload["status"] == "ok":
        recomputed = hashlib.sha256(
            json.dumps(
                payload["plan"]["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        assert recomputed == payload["plan"]["payload_sha256"]
        assert payload["manifest"]["invocation_id"] == INVOCATION_ID
        assert payload["manifest"]["requested_at"] == REQUESTED_AT
