"""Tests for scripts/ci/verify_ci_check_conclusions.py (Issue #1760 AC9 / #1824 P1-3 / #2631).

Runtime evidence check: real job conclusions for the SAME head SHA AND the
SAME workflow run/attempt (actionlint / python-test-core / python-test /
node-backed-hook-tests) -- never a plain string search, and never a
(name, head_sha)-only grouping that can mix evidence from an unrelated rerun
of the same commit.

Issue #2161 (native Codex CLI retirement): the former codex-execpolicy check
name and its AC6 sentinel artifact verification were removed with the job;
this suite was rewritten accordingly.

Issue #2631: this script (and this test suite) no longer builds its own
commit-scoped GitHub CheckRuns payload or a (name, head_sha)-details_url
binding test double. Every test here builds a REAL, validated
``ci_job_snapshot_v1`` through the actual shared helper
(``scripts/ci/ci_job_snapshot.py``) -- never a mirror/fake reimplementation
of its identity/provenance semantics (the exact gap the Issue calls out for
the former ``_fake_filter_check_runs_by_workflow_run()`` test double).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "scripts" / "ci" / "verify_ci_check_conclusions.py"
_CI_JOB_SNAPSHOT_PATH = _REPO_ROOT / "scripts" / "ci" / "ci_job_snapshot.py"

EXPECTED_SHA = "a" * 40
OTHER_SHA = "b" * 40
RUN_ID = 123
OTHER_RUN_ID = 999
RUN_ATTEMPT = 1
REPOSITORY = "owner/repo"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module("verify_ci_check_conclusions", _MODULE_PATH)


@pytest.fixture(scope="module")
def cjs():
    """The SAME real shared job-snapshot helper the production script
    imports -- tests build REAL validated snapshots through it."""
    return _load_module("ci_job_snapshot", _CI_JOB_SNAPSHOT_PATH)


ALL_REQUIRED = ["actionlint", "python-test-core", "python-test", "node-backed-hook-tests"]


def _job_row(
    name: str,
    *,
    job_id: int,
    run_id: int = RUN_ID,
    head_sha: str = EXPECTED_SHA,
    run_attempt: int | None = RUN_ATTEMPT,
    status: str | None = "completed",
    conclusion: str | None = "success",
) -> dict:
    return {
        "id": job_id,
        "name": name,
        "run_id": run_id,
        "head_sha": head_sha,
        "run_attempt": run_attempt,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "check_run_url": f"https://api.github.com/repos/{REPOSITORY}/check-runs/{job_id}",
    }


def _make_snapshot(
    cjs,
    names: list[str],
    *,
    head_sha: str = EXPECTED_SHA,
    status: str = "completed",
    conclusion: str = "success",
    run_id: int = RUN_ID,
    run_attempt: int = RUN_ATTEMPT,
    repository: str = REPOSITORY,
) -> dict:
    """Build a REAL validated ci_job_snapshot_v1 dict via the actual shared
    helper (never a hand-rolled dict bypassing its own validation)."""
    jobs = [
        _job_row(
            name,
            job_id=idx + 1,
            run_id=run_id,
            head_sha=head_sha,
            run_attempt=run_attempt,
            status=status,
            conclusion=conclusion,
        )
        for idx, name in enumerate(names)
    ]
    identity_payload = {
        "id": run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "run_started_at": "2026-01-01T00:00:00Z",
        "repository": {"full_name": repository},
    }
    return cjs.build_snapshot(
        pages=[{"total_count": len(jobs), "jobs": jobs}],
        identity_payload=identity_payload,
        expected_repository=repository,
        expected_run_id=run_id,
        expected_head_sha=head_sha,
    )


def _make_multi_page_snapshot(
    cjs,
    names: list[str],
    *,
    split_at: int,
    head_sha: str = EXPECTED_SHA,
    run_id: int = RUN_ID,
    run_attempt: int = RUN_ATTEMPT,
    repository: str = REPOSITORY,
    conclusions: dict[str, str] | None = None,
) -> dict:
    """Build a REAL validated snapshot from a genuinely MULTI-PAGE ``gh api
    --paginate --slurp`` page array (page 1 = ``names[:split_at]``, page 2 =
    ``names[split_at:]``). Every other fixture in this suite uses a single
    one-page ``pages=[{...}]`` list, which never exercises
    ``ci_job_snapshot._validate_pages``'s actual multi-page flatten /
    cross-page ``total_count`` consistency logic (AC1) -- this helper does."""
    conclusions = conclusions or {}
    jobs = [
        _job_row(
            name,
            job_id=idx + 1,
            run_id=run_id,
            head_sha=head_sha,
            run_attempt=run_attempt,
            conclusion=conclusions.get(name, "success"),
        )
        for idx, name in enumerate(names)
    ]
    total = len(jobs)
    identity_payload = {
        "id": run_id,
        "run_attempt": run_attempt,
        "head_sha": head_sha,
        "run_started_at": "2026-01-01T00:00:00Z",
        "repository": {"full_name": repository},
    }
    return cjs.build_snapshot(
        pages=[
            {"total_count": total, "jobs": jobs[:split_at]},
            {"total_count": total, "jobs": jobs[split_at:]},
        ],
        identity_payload=identity_payload,
        expected_repository=repository,
        expected_run_id=run_id,
        expected_head_sha=head_sha,
    )


def _verify(mod, snapshot: dict, **kwargs):
    kwargs.setdefault("expected_repository", REPOSITORY)
    kwargs.setdefault("expected_head_sha", EXPECTED_SHA)
    kwargs.setdefault("workflow_run_id", RUN_ID)
    kwargs.setdefault("workflow_run_attempt", RUN_ATTEMPT)
    kwargs.setdefault("bench_mode", False)
    return mod.verify(snapshot=snapshot, **kwargs)


class TestPositive:
    def test_all_green_same_head_same_run_is_ok(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED)
        report = _verify(mod, snapshot)
        assert report["ok"] is True, report["violations"]

    def test_bench_mode_no_check_name_is_currently_skippable(self, mod, cjs):
        """Issue #2161: codex-execpolicy (the sole BENCH_MODE_SKIPPABLE check
        name) was removed, so bench_mode=True no longer changes the outcome
        -- every required check must still be present and successful."""
        snapshot = _make_snapshot(cjs, ALL_REQUIRED)
        report = _verify(mod, snapshot, bench_mode=True)
        assert report["ok"] is True, report["violations"]
        assert mod.BENCH_MODE_SKIPPABLE == set()


class TestMissingCheck:
    def test_missing_required_check_is_rejected(self, mod, cjs):
        names = [n for n in ALL_REQUIRED if n != "node-backed-hook-tests"]
        snapshot = _make_snapshot(cjs, names)
        report = _verify(mod, snapshot)
        assert report["ok"] is False
        assert any("no job named 'node-backed-hook-tests'" in v for v in report["violations"])
        assert report["checks"]["node-backed-hook-tests"] == {"found": False}


class TestIdentityMismatch:
    """Issue #2631 AC2: this consumer independently re-checks the snapshot's
    OWN baked-in identity against its trusted inputs -- a mismatch here is a
    fail-closed identity violation, never degraded to per-check
    not-found (the per-check loop does not even run)."""

    def test_snapshot_head_sha_mismatch_is_rejected(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED, head_sha=OTHER_SHA)
        report = _verify(mod, snapshot, expected_head_sha=EXPECTED_SHA)
        assert report["ok"] is False
        assert report["checks"] == {}
        assert any("expected_head_sha" in v for v in report["violations"])

    def test_snapshot_bound_to_a_different_workflow_run_is_rejected(self, mod, cjs):
        """P1-3 (Issue #2631 restatement): a snapshot legitimately built for
        a DIFFERENT run (e.g. a rerun) must never be accepted as evidence
        for THIS run, even though every job row inside it is internally
        self-consistent."""
        snapshot = _make_snapshot(cjs, ALL_REQUIRED, run_id=OTHER_RUN_ID)
        report = _verify(mod, snapshot, workflow_run_id=RUN_ID)
        assert report["ok"] is False
        assert report["checks"] == {}
        assert any("workflow_run_id" in v for v in report["violations"])

    def test_snapshot_bound_to_a_different_repository_is_rejected(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED, repository="owner/other-repo")
        report = _verify(mod, snapshot, expected_repository=REPOSITORY)
        assert report["ok"] is False
        assert report["checks"] == {}
        assert any("expected_repository" in v for v in report["violations"])


class TestMixedProvenanceStructurallyPrevented:
    """Issue #2631: because acquisition is now attempt-scoped (the Jobs
    endpoint is itself scoped to one run_id/attempt_number), a single
    response can no longer mix rows from an unrelated rerun of the same
    commit the way the former commit-scoped CheckRuns endpoint could -- this
    is enforced at snapshot CONSTRUCTION time, not at verify() time."""

    def test_snapshot_construction_rejects_a_foreign_run_job_row(self, cjs):
        identity_payload = {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "head_sha": EXPECTED_SHA,
            "run_started_at": "2026-01-01T00:00:00Z",
            "repository": {"full_name": REPOSITORY},
        }
        foreign_job = _job_row("python-test-core", job_id=1, run_id=OTHER_RUN_ID)
        with pytest.raises(cjs.SnapshotError, match="job_row_run_id_mismatch"):
            cjs.build_snapshot(
                pages=[{"total_count": 1, "jobs": [foreign_job]}],
                identity_payload=identity_payload,
                expected_repository=REPOSITORY,
                expected_run_id=RUN_ID,
                expected_head_sha=EXPECTED_SHA,
            )


class TestMultiPageAcquisition:
    """PR #2669 review fix_delta iteration 1: exercise
    ``ci_job_snapshot._validate_pages``'s actual multi-page combination /
    consistency logic (AC1) and the acquisition-sequence-once invariant
    (AC3) with a genuine multi-page ``pages`` list, not the single-page
    shortcut every other fixture in this suite uses."""

    def test_required_job_present_only_on_later_page_is_not_dropped(self, mod, cjs):
        # node-backed-hook-tests is ALL_REQUIRED[-1]; split_at=2 puts it on
        # page 2 only (page 1 = ["actionlint", "python-test-core"]).
        snapshot = _make_multi_page_snapshot(cjs, ALL_REQUIRED, split_at=2)
        assert len(snapshot["jobs"]) == len(ALL_REQUIRED)
        assert {j["name"] for j in snapshot["jobs"]} == set(ALL_REQUIRED)

        report = _verify(mod, snapshot)
        assert report["ok"] is True, report["violations"]
        for name in ALL_REQUIRED:
            assert report["checks"][name]["found"] is True

    def test_failing_job_on_later_page_is_not_a_partial_success(self, mod, cjs):
        # The failing job (node-backed-hook-tests) lives ONLY on page 2;
        # this must fail-closed, never be silently dropped/ignored as if
        # only page 1's jobs mattered.
        snapshot = _make_multi_page_snapshot(
            cjs,
            ALL_REQUIRED,
            split_at=2,
            conclusions={"node-backed-hook-tests": "failure"},
        )
        assert len(snapshot["jobs"]) == len(ALL_REQUIRED)

        report = _verify(mod, snapshot)
        assert report["ok"] is False
        assert any(
            "node-backed-hook-tests" in v and "failure" in v for v in report["violations"]
        )

    def test_page_total_count_mismatch_across_pages_is_rejected(self, cjs):
        identity_payload = {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "head_sha": EXPECTED_SHA,
            "run_started_at": "2026-01-01T00:00:00Z",
            "repository": {"full_name": REPOSITORY},
        }
        jobs = [_job_row(n, job_id=idx + 1) for idx, n in enumerate(ALL_REQUIRED)]
        with pytest.raises(cjs.SnapshotError, match="job_pages_total_count_mismatch_across_pages"):
            cjs.build_snapshot(
                pages=[
                    {"total_count": len(jobs), "jobs": jobs[:2]},
                    {"total_count": len(jobs) + 1, "jobs": jobs[2:]},
                ],
                identity_payload=identity_payload,
                expected_repository=REPOSITORY,
                expected_run_id=RUN_ID,
                expected_head_sha=EXPECTED_SHA,
            )

    def test_partial_multi_page_acquisition_is_rejected(self, cjs):
        """A page array that stops short of ``total_count`` (e.g. an
        interrupted paginated fetch) is NEVER treated as success evidence,
        even though every individual page it DOES contain is well-formed."""
        identity_payload = {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "head_sha": EXPECTED_SHA,
            "run_started_at": "2026-01-01T00:00:00Z",
            "repository": {"full_name": REPOSITORY},
        }
        jobs = [_job_row(n, job_id=idx + 1) for idx, n in enumerate(ALL_REQUIRED)]
        with pytest.raises(cjs.SnapshotError, match="job_pages_incomplete_partial_acquisition"):
            cjs.build_snapshot(
                # declares 4 total but only ships page 1's 2 rows.
                pages=[{"total_count": len(jobs), "jobs": jobs[:2]}],
                identity_payload=identity_payload,
                expected_repository=REPOSITORY,
                expected_run_id=RUN_ID,
                expected_head_sha=EXPECTED_SHA,
            )


class TestDuplicateJobName:
    def test_duplicate_job_name_is_deterministically_rejected(self, mod, cjs):
        """AC8: never disambiguated by e.g. picking the highest id."""
        jobs = [
            _job_row("python-test-core", job_id=1),
            _job_row("python-test-core", job_id=2),
            *[
                _job_row(n, job_id=100 + i)
                for i, n in enumerate(ALL_REQUIRED)
                if n != "python-test-core"
            ],
        ]
        identity_payload = {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "head_sha": EXPECTED_SHA,
            "run_started_at": "2026-01-01T00:00:00Z",
            "repository": {"full_name": REPOSITORY},
        }
        snapshot = cjs.build_snapshot(
            pages=[{"total_count": len(jobs), "jobs": jobs}],
            identity_payload=identity_payload,
            expected_repository=REPOSITORY,
            expected_run_id=RUN_ID,
            expected_head_sha=EXPECTED_SHA,
        )
        report = _verify(mod, snapshot)
        assert report["ok"] is False
        assert report["checks"]["python-test-core"] == {"found": True, "duplicate": True}
        assert any("AC8" in v and "python-test-core" in v for v in report["violations"])


class TestFailedConclusion:
    def test_failed_conclusion_is_rejected(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED)
        for job in snapshot["jobs"]:
            if job["name"] == "python-test-core":
                job["conclusion"] = "failure"
        report = _verify(mod, snapshot)
        assert report["ok"] is False
        assert any("python-test-core" in v and "failure" in v for v in report["violations"])

    def test_skipped_conclusion_is_rejected(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED)
        for job in snapshot["jobs"]:
            if job["name"] == "node-backed-hook-tests":
                job["conclusion"] = "skipped"
        report = _verify(mod, snapshot)
        assert report["ok"] is False
        assert any("node-backed-hook-tests" in v and "skipped" in v for v in report["violations"])

    def test_pending_status_is_rejected(self, mod, cjs):
        snapshot = _make_snapshot(cjs, ALL_REQUIRED, status="in_progress", conclusion=None)
        report = _verify(mod, snapshot)
        assert report["ok"] is False


class TestCheckRunUrlBinding:
    """AC6/AC7: strict-parse check_run_url and confirm it names the SAME
    CheckRun as this job row's own id (0 dereference calls needed in the
    ordinary case)."""

    def test_matching_check_run_url_requires_no_dereference(self, cjs):
        job = _job_row("python-test-core", job_id=42)
        result = cjs.verify_check_run_binding(job, expected_owner_repo=REPOSITORY)
        assert result == {"check_run_id": 42, "dereferenced": False}

    def test_malformed_check_run_url_is_rejected(self, cjs):
        job = _job_row("python-test-core", job_id=42)
        job["check_run_url"] = "https://evil.example.com/repos/owner/repo/check-runs/42"
        with pytest.raises(cjs.SnapshotError, match="check_run_url_wrong_host_or_repo"):
            cjs.verify_check_run_binding(job, expected_owner_repo=REPOSITORY)

    def test_mismatched_id_without_dereference_fn_is_rejected(self, cjs):
        job = _job_row("python-test-core", job_id=42)
        job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/99"
        with pytest.raises(cjs.SnapshotError, match="check_run_url_id_mismatch_undereferenced"):
            cjs.verify_check_run_binding(job, expected_owner_repo=REPOSITORY)

    def test_mismatched_id_is_resolved_by_exactly_one_dereference(self, cjs):
        """PR #2669 review fix_delta Blocker 2: the job's own ``id`` (42) and
        the URL-derived CheckRun id (99) are NOT required to be numerically
        equal -- the Jobs API documents them as separate fields. A single
        dereference call confirming the response's own ``id`` matches the
        URL-derived id is sufficient; job-id agreement is never required."""
        job = _job_row("python-test-core", job_id=42)
        job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/99"
        calls: list[int] = []

        def dereference_fn(check_run_id: int) -> dict:
            calls.append(check_run_id)
            return {"id": 99}

        result = cjs.verify_check_run_binding(
            job, expected_owner_repo=REPOSITORY, dereference_fn=dereference_fn
        )
        assert result == {"check_run_id": 99, "dereferenced": True}
        assert calls == [99]

    def test_dereference_response_id_disagreeing_with_url_is_rejected(self, cjs):
        """The dereferenced response's own ``id`` must match the URL-derived
        id -- a response for a DIFFERENT CheckRun is never accepted even if
        the dereference call itself succeeded."""
        job = _job_row("python-test-core", job_id=42)
        job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/99"

        def dereference_fn(check_run_id: int) -> dict:
            return {"id": 12345}

        with pytest.raises(cjs.SnapshotError, match="check_run_dereference_id_mismatch"):
            cjs.verify_check_run_binding(
                job, expected_owner_repo=REPOSITORY, dereference_fn=dereference_fn
            )

    def test_leading_zero_check_run_id_is_rejected_as_noncanonical(self, cjs):
        job = _job_row("python-test-core", job_id=42)
        job["check_run_url"] = f"https://api.github.com/repos/{REPOSITORY}/check-runs/042"
        with pytest.raises(cjs.SnapshotError, match="check_run_url_noncanonical"):
            cjs.verify_check_run_binding(job, expected_owner_repo=REPOSITORY)


class TestCli:
    def _write_snapshot(self, cjs, tmp_path: Path, names: list[str], **kwargs) -> Path:
        snapshot = _make_snapshot(cjs, names, **kwargs)
        out = tmp_path / "ci_job_snapshot.json"
        cjs.atomic_write_snapshot(snapshot, out)
        return out

    def test_cli_operational_error_on_missing_file(self, tmp_path):
        import subprocess

        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(tmp_path / "missing.json"),
                "--repository",
                REPOSITORY,
                "--expected-head-sha",
                EXPECTED_SHA,
                "--workflow-run-id",
                str(RUN_ID),
                "--bench-mode",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 3
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False

    def test_cli_requires_workflow_run_id(self, cjs, tmp_path):
        import subprocess

        snapshot_path = self._write_snapshot(cjs, tmp_path, ALL_REQUIRED)
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(snapshot_path),
                "--repository",
                REPOSITORY,
                "--expected-head-sha",
                EXPECTED_SHA,
                "--bench-mode",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "--workflow-run-id" in proc.stderr

    def test_cli_requires_repository(self, cjs, tmp_path):
        import subprocess

        snapshot_path = self._write_snapshot(cjs, tmp_path, ALL_REQUIRED)
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(snapshot_path),
                "--expected-head-sha",
                EXPECTED_SHA,
                "--workflow-run-id",
                str(RUN_ID),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        assert "--repository" in proc.stderr

    def test_cli_exit_0_on_success(self, cjs, tmp_path):
        import subprocess

        snapshot_path = self._write_snapshot(cjs, tmp_path, ALL_REQUIRED)
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(snapshot_path),
                "--repository",
                REPOSITORY,
                "--expected-head-sha",
                EXPECTED_SHA,
                "--workflow-run-id",
                str(RUN_ID),
                "--workflow-run-attempt",
                str(RUN_ATTEMPT),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def test_cli_exit_2_on_violation(self, cjs, tmp_path):
        import subprocess

        snapshot_path = self._write_snapshot(cjs, tmp_path, [])
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(snapshot_path),
                "--repository",
                REPOSITORY,
                "--expected-head-sha",
                EXPECTED_SHA,
                "--workflow-run-id",
                str(RUN_ID),
                "--bench-mode",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2

    def test_cli_exit_3_on_malformed_snapshot_schema(self, tmp_path):
        import subprocess

        source = tmp_path / "ci_job_snapshot.json"
        source.write_text(json.dumps({"schema": "not_ci_job_snapshot_v1"}))
        proc = subprocess.run(
            [
                "python3",
                str(_MODULE_PATH),
                "--job-snapshot-json",
                str(source),
                "--repository",
                REPOSITORY,
                "--expected-head-sha",
                EXPECTED_SHA,
                "--workflow-run-id",
                str(RUN_ID),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 3
        payload = json.loads(proc.stdout)
        assert payload["ok"] is False


class TestGateReadyLatency:
    """PR #2669 review fix_delta Blocker 3 (Issue #2631 AC4):
    ``ci_job_snapshot.compute_gate_ready_latency_artifact`` must NEVER
    raise, NEVER record a negative ``gate_ready_latency_ms``, and always
    record a diagnostic-only reason when the measurement itself cannot be
    computed -- an auxiliary measurement must never escalate to a
    required-job failure."""

    def _snapshot(self, cjs, *, run_started_at="2026-01-01T00:00:00Z", jobs=None):
        return {
            "schema": "ci_job_snapshot_v1",
            "schema_version": 1,
            "expected_repository": REPOSITORY,
            "workflow_run_id": RUN_ID,
            "workflow_run_attempt": RUN_ATTEMPT,
            "expected_head_sha": EXPECTED_SHA,
            "run_started_at": run_started_at,
            "jobs": jobs if jobs is not None else [],
        }

    def test_normal_completion_yields_positive_latency(self, cjs):
        snapshot = self._snapshot(
            cjs,
            jobs=[
                _job_row(
                    "e2e",
                    job_id=1,
                    status="completed",
                    conclusion="success",
                )
            ],
        )
        # _job_row's completed_at is "2026-01-01T00:05:00Z" (5 minutes after
        # this snapshot's run_started_at).
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert artifact["gate_ready_latency_ms"] == 5 * 60 * 1000
        assert "gate_ready_latency_omitted_reason" not in artifact
        assert artifact["gate_ready_at"] == "2026-01-01T00:05:00Z"

    def test_negative_delta_is_omitted_not_recorded(self, cjs):
        """completed_at BEFORE run_started_at (e.g. a stale snapshot) must
        never produce a negative gate_ready_latency_ms."""
        job = _job_row("e2e", job_id=1, status="completed")
        job["completed_at"] = "1999-01-01T00:00:00Z"
        snapshot = self._snapshot(cjs, jobs=[job])
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert (
            artifact["gate_ready_latency_omitted_reason"]
            == "negative_latency_completed_before_run_started"
        )

    def test_malformed_run_started_at_never_raises(self, cjs):
        snapshot = self._snapshot(cjs, run_started_at="not-a-timestamp")
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"] == "run_started_at_parse_failed"

    def test_malformed_completed_at_never_raises(self, cjs):
        job = _job_row("e2e", job_id=1, status="completed")
        job["completed_at"] = "not-a-timestamp"
        snapshot = self._snapshot(cjs, jobs=[job])
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"] == "completed_at_parse_failed"

    def test_job_not_uniquely_resolvable_is_omitted_not_raised(self, cjs):
        snapshot = self._snapshot(cjs, jobs=[])
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"].startswith(
            "job_not_uniquely_resolvable:"
        )

    def test_duplicate_job_name_is_omitted_not_raised(self, cjs):
        snapshot = self._snapshot(
            cjs,
            jobs=[
                _job_row("e2e", job_id=1, status="completed"),
                _job_row("e2e", job_id=2, status="completed"),
            ],
        )
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"].startswith(
            "job_not_uniquely_resolvable:"
        )

    def test_job_not_completed_is_omitted_not_raised(self, cjs):
        job = _job_row("e2e", job_id=1, status="in_progress", conclusion=None)
        job["completed_at"] = None
        snapshot = self._snapshot(cjs, jobs=[job])
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"] == "job_not_completed"

    def test_missing_completed_at_is_omitted_not_raised(self, cjs):
        job = _job_row("e2e", job_id=1, status="completed")
        job["completed_at"] = None
        snapshot = self._snapshot(cjs, jobs=[job])
        artifact = cjs.compute_gate_ready_latency_artifact(
            snapshot,
            job_name="e2e",
            run_id="123",
            run_attempt="1",
            head_sha=EXPECTED_SHA,
            merge_sha=EXPECTED_SHA,
        )
        assert "gate_ready_latency_ms" not in artifact
        assert artifact["gate_ready_latency_omitted_reason"] == "job_missing_completed_at"
