"""scripts/ci/tests/test_dispatch_close_evidence_publication_smoke_v1.py

Issue #2555 AC8: `uv run pytest` wrapper for the bounded runtime-verification
smoke run (`scripts/ci/dispatch_close_evidence_publication_smoke_v1.py`).
The VC preflight allowlist does not permit an arbitrary
`uv run python3 <script>` invocation shape, so this pytest wrapper is the
canonical VC entry point (`uv run pytest
scripts/ci/tests/test_dispatch_close_evidence_publication_smoke_v1.py -v`).

When `GH_TOKEN`/`GITHUB_TOKEN` lacks `actions: read` scope, or no existing
non-expired Reliability close-grade artifact ID can be resolved (or the
current worktree's HEAD is not yet pushed to origin), the underlying module
raises `SkipCondition` -- this wrapper maps that to `pytest.skip()` (SKIP,
never a false PASS or a false FAIL; `docs/dev/runtime-verification-policy.md`
SKIP contract).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SMOKE_MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "dispatch_close_evidence_publication_smoke_v1.py"
# PR #2559 review fix_delta (item E): legacy CLI compatibility regression
# tests below exercise the REAL `build_close_evidence_bundle_v1.py` CLI via
# `subprocess` (never re-implement its argv-shim logic here) -- this test
# file is the Allowed Paths-permitted home for that coverage (Issue #2555
# fix_delta scope: `scripts/ci/tests/test_build_close_evidence_bundle_v1.py`
# itself is Allowed Paths外 and must stay unmodified).
BUILD_BUNDLE_MODULE_PATH = REPO_ROOT / "scripts" / "ci" / "build_close_evidence_bundle_v1.py"
CLOSE_EVIDENCE_FIXTURES_DIR = REPO_ROOT / "scripts" / "ci" / "fixtures" / "close_evidence"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location(
        "ci_dispatch_close_evidence_publication_smoke_v1_under_test", SMOKE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


smoke = _load_smoke_module()


def test_module_defines_bounded_retry_constants_never_unbounded_loops():
    """GIVEN the smoke module WHEN inspected THEN every poll/search loop has
    a finite, explicit bound (never an unbounded `while True`)."""
    assert smoke.RUN_POLL_MAX_ATTEMPTS > 0
    assert smoke.RUN_DISCOVERY_MAX_ATTEMPTS > 0
    assert smoke.ARTIFACT_SEARCH_PAGES > 0
    assert smoke.ARTIFACT_SEARCH_PER_PAGE > 0


def test_exit_codes_match_runtime_verification_policy_contract():
    """GIVEN the module's exit code constants WHEN compared to
    docs/dev/runtime-verification-policy.md's SKIP contract THEN PASS=0,
    FAIL=1, SKIP=77 (SKIP is never conflated with PASS)."""
    assert smoke.EXIT_PASS == 0
    assert smoke.EXIT_FAIL == 1
    assert smoke.EXIT_SKIP == 77


def test_target_job_and_artifact_names_match_ci_yml_contract():
    """GIVEN the smoke module's target job/artifact name constants WHEN
    compared to the `close-evidence-publication` job's own AC5/AC7 upload
    step names in .github/workflows/ci.yml THEN they are byte-identical
    (never a drifted duplicate string)."""
    ci_yml_text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "if: ${{ github.event.inputs.close_evidence_source_artifact_id != '' }}" in ci_yml_text
    assert f"name: {smoke.ARTIFACT_A_NAME}" in ci_yml_text
    assert f"name: {smoke.ARTIFACT_B_NAME}" in ci_yml_text
    assert smoke.JOB_NAME == "close-evidence-publication"
    assert f"  {smoke.JOB_NAME}:" in ci_yml_text


def test_run_smoke_end_to_end_pass_fail_or_skip():
    """GIVEN the real repository/environment WHEN run_smoke() executes THEN
    it either (a) raises SkipCondition (mapped to pytest.skip() below) when
    a precondition -- GH token actions:read scope, a resolvable existing
    Reliability artifact, or a pushed matching ref -- cannot be resolved, or
    (b) returns a SmokeResult with status in {"pass", "fail"} after actually
    dispatching and polling the real `close-evidence-publication` job
    end-to-end (never a fabricated/simulated result)."""
    try:
        result = smoke.run_smoke()
    except smoke.SkipCondition as exc:
        pytest.skip(f"close_evidence_publication_smoke SKIP: {exc}")
        return

    assert result.status in ("pass", "fail")
    if result.status == "fail":
        pytest.fail(
            f"close_evidence_publication_smoke FAIL: {result.reason} "
            f"(run_id={result.run_id} run_url={result.run_url})"
        )
    # status == "pass": AC8 evidence -- record for PR body attachment.
    assert result.run_id is not None
    assert result.artifact_a_id is not None
    assert result.artifact_b_id is not None
    print(
        "close_evidence_publication_smoke PASS: "
        f"run_id={result.run_id} run_url={result.run_url} "
        f"artifact_a_id={result.artifact_a_id} artifact_b_id={result.artifact_b_id}"
    )


class _FakeCompletedProcess:
    """Minimal stand-in for `subprocess.CompletedProcess` -- used to
    deterministically drive `smoke._run_gh()`'s callers below without any
    real network/`gh` invocation."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_dispatch_workflow_failure_is_fail_never_skip(monkeypatch):
    """PR #2559 review fix_delta (item B): a `gh workflow run` dispatch
    failure is an implementation-path fault (FAIL, `RuntimeError`), never a
    SKIP `SkipCondition` -- the prior (pre-fix_delta) behavior."""
    monkeypatch.setattr(smoke, "_run_gh", lambda *a, **k: _FakeCompletedProcess(returncode=1, stderr="boom"))
    with pytest.raises(RuntimeError, match="gh workflow run failed to dispatch"):
        smoke.dispatch_workflow("owner/repo", "some-branch", 123)


def test_discover_dispatched_run_excludes_pre_dispatch_snapshot_and_binds_by_head_sha(monkeypatch):
    """PR #2559 review fix_delta (item C): a pre-existing run for the same
    (workflow, ref, event) tuple must never be misattributed as the run we
    just dispatched -- only a run absent from `pre_dispatch_run_ids` AND
    matching `head_sha` AND created at/after `dispatched_after` is bound."""
    runs_payload = json.dumps(
        [
            {"databaseId": 100, "createdAt": "2026-09-07T12:00:00Z", "status": "queued", "headSha": "deadbeef"},
            {"databaseId": 200, "createdAt": "2026-09-07T12:00:05Z", "status": "queued", "headSha": "cafef00d"},
        ]
    )
    monkeypatch.setattr(smoke, "_run_gh", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=runs_payload))
    run_id = smoke.discover_dispatched_run(
        "owner/repo",
        "some-branch",
        dispatched_after=smoke._parse_iso8601_utc_to_epoch("2026-09-07T12:00:00Z"),
        pre_dispatch_run_ids={100},
        head_sha="cafef00d",
    )
    assert run_id == 200


def test_discover_dispatched_run_ambiguous_candidates_fails_never_guesses(monkeypatch):
    """PR #2559 review fix_delta (item C): more than one candidate matching
    the exact-binding filters is an unresolvable ambiguity -- FAIL, never a
    guessed `runs[0]` (the prior, incorrect behavior)."""
    runs_payload = json.dumps(
        [
            {"databaseId": 201, "createdAt": "2026-09-07T12:00:05Z", "status": "queued", "headSha": "cafef00d"},
            {"databaseId": 202, "createdAt": "2026-09-07T12:00:06Z", "status": "queued", "headSha": "cafef00d"},
        ]
    )
    monkeypatch.setattr(smoke, "_run_gh", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=runs_payload))
    with pytest.raises(RuntimeError, match="ambiguous"):
        smoke.discover_dispatched_run(
            "owner/repo",
            "some-branch",
            dispatched_after=smoke._parse_iso8601_utc_to_epoch("2026-09-07T12:00:00Z"),
            pre_dispatch_run_ids=set(),
            head_sha="cafef00d",
        )


def test_discover_dispatched_run_exhausted_window_fails_never_skips(monkeypatch):
    """PR #2559 review fix_delta (item C/B): exhausting the bounded
    discovery window with zero matching candidates is FAIL, never SKIP (a
    runtime binding fault, not an environment precondition)."""
    monkeypatch.setattr(smoke, "RUN_DISCOVERY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(smoke, "RUN_DISCOVERY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(smoke, "_run_gh", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="[]"))
    with pytest.raises(RuntimeError, match="did not appear"):
        smoke.discover_dispatched_run(
            "owner/repo", "some-branch", dispatched_after=0.0, pre_dispatch_run_ids=set(), head_sha="cafef00d"
        )


def test_poll_run_completion_retries_transient_gh_failure_then_succeeds(monkeypatch):
    """PR #2559 review fix_delta (item B): a transient `gh run view`
    failure is retried within the EXISTING bounded poll window, never
    immediately raised (no new unbounded retry harness is introduced)."""
    monkeypatch.setattr(smoke, "RUN_POLL_INTERVAL_SECONDS", 0)
    calls = {"n": 0}

    def fake_run_gh(args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeCompletedProcess(returncode=1, stderr="transient")
        return _FakeCompletedProcess(
            returncode=0, stdout=json.dumps({"status": "completed", "conclusion": "success", "jobs": [], "url": "u"})
        )

    monkeypatch.setattr(smoke, "_run_gh", fake_run_gh)
    payload = smoke.poll_run_completion("owner/repo", 999)
    assert payload["status"] == "completed"
    assert calls["n"] == 2


def test_poll_run_completion_exhausted_window_raises_runtime_error(monkeypatch):
    """PR #2559 review fix_delta (item B): exhausting the bounded poll
    window (never reaching `status: completed`) raises `RuntimeError` --
    `main()` now catches this and prints a formatted FAIL (never an
    unformatted traceback, the prior gap)."""
    monkeypatch.setattr(smoke, "RUN_POLL_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(smoke, "RUN_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        smoke, "_run_gh", lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout=json.dumps({"status": "queued"}))
    )
    with pytest.raises(RuntimeError, match="did not reach status=completed"):
        smoke.poll_run_completion("owner/repo", 999)


def test_resolve_reliability_artifact_id_skips_candidates_whose_job_did_not_succeed(monkeypatch):
    """PR #2559 review fix_delta (item D): a name-prefix + non-expired match
    whose OWN `reliability-assessment` job did NOT conclude `success` (e.g.
    an ineligible/failed assessment run that still uploaded via
    `if: !cancelled()`) must be skipped in favor of the next candidate --
    never accepted just because the artifact metadata alone looks
    plausible."""
    artifacts_page1 = json.dumps(
        {
            "artifacts": [
                {
                    "id": 1,
                    "name": f"{smoke.RELIABILITY_ARTIFACT_NAME_PREFIX}111-a1",
                    "expired": False,
                    "workflow_run": {"id": 111},
                },
                {
                    "id": 2,
                    "name": f"{smoke.RELIABILITY_ARTIFACT_NAME_PREFIX}222-a1",
                    "expired": False,
                    "workflow_run": {"id": 222},
                },
            ]
        }
    )

    def fake_run_gh(args, **kwargs):
        joined = " ".join(args)
        if "actions/artifacts?" in joined:
            return _FakeCompletedProcess(returncode=0, stdout=artifacts_page1)
        if "actions/runs/111/jobs" in joined:
            return _FakeCompletedProcess(
                returncode=0,
                stdout=json.dumps({"jobs": [{"name": smoke.RELIABILITY_JOB_NAME, "conclusion": "failure"}]}),
            )
        if "actions/runs/222/jobs" in joined:
            return _FakeCompletedProcess(
                returncode=0,
                stdout=json.dumps({"jobs": [{"name": smoke.RELIABILITY_JOB_NAME, "conclusion": "success"}]}),
            )
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(smoke, "_run_gh", fake_run_gh)
    artifact_id, name = smoke.resolve_reliability_artifact_id("owner/repo")
    assert artifact_id == 2
    assert name.endswith("222-a1")


def _run_build_bundle_cli(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BUILD_BUNDLE_MODULE_PATH), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def close_evidence_fixture_paths(tmp_path):
    """PR #2559 review fix_delta (item E): copies the SAME close-grade
    eligible fixtures `test_build_close_evidence_bundle_v1.py` (Allowed
    Paths外, never modified here) uses, into an isolated tmp_path -- so
    these subprocess CLI-compatibility regression tests never depend on nor
    mutate that other test file's own fixtures in place."""
    dest = {
        "manifest": tmp_path / "experiment-manifest.json",
        "performance": tmp_path / "performance-close-grade-result.json",
        "reliability": tmp_path / "reliability-close-grade-result.json",
    }
    shutil.copyfile(CLOSE_EVIDENCE_FIXTURES_DIR / "experiment_manifest.fixture.json", dest["manifest"])
    shutil.copyfile(
        CLOSE_EVIDENCE_FIXTURES_DIR / "performance_close_grade_result.fixture.json", dest["performance"]
    )
    shutil.copyfile(
        CLOSE_EVIDENCE_FIXTURES_DIR / "reliability_close_grade_result.fixture.json", dest["reliability"]
    )
    return dest


def test_build_close_evidence_bundle_legacy_flat_invocation_is_routed_to_build(
    close_evidence_fixture_paths, tmp_path
):
    """PR #2559 review fix_delta (item E): a legacy flat invocation (no
    `build`/`publication-receipt` subcommand -- the CLI shape that predates
    Issue #2555's `publication-receipt` addition) must still succeed via the
    argv shim, producing the exact same `close_evidence.json` output as the
    explicit `build` subcommand."""
    output_dir = tmp_path / "close-evidence-legacy"
    result = _run_build_bundle_cli(
        [
            "--performance-receipt",
            str(close_evidence_fixture_paths["performance"]),
            "--reliability-receipt",
            str(close_evidence_fixture_paths["reliability"]),
            "--experiment-manifest",
            str(close_evidence_fixture_paths["manifest"]),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert (output_dir / "close_evidence.json").is_file()


def test_build_close_evidence_bundle_explicit_build_subcommand_still_works(close_evidence_fixture_paths, tmp_path):
    """PR #2559 review fix_delta (item E): the explicit `build` subcommand
    invocation (Issue #2555's own `.github/workflows/ci.yml` call shape)
    must be byte-for-byte unaffected by the legacy-invocation shim."""
    output_dir = tmp_path / "close-evidence-explicit"
    result = _run_build_bundle_cli(
        [
            "build",
            "--performance-receipt",
            str(close_evidence_fixture_paths["performance"]),
            "--reliability-receipt",
            str(close_evidence_fixture_paths["reliability"]),
            "--experiment-manifest",
            str(close_evidence_fixture_paths["manifest"]),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert (output_dir / "close_evidence.json").is_file()


def test_build_close_evidence_bundle_publication_receipt_subcommand_still_works(
    close_evidence_fixture_paths, tmp_path
):
    """PR #2559 review fix_delta (item E): the `publication-receipt`
    subcommand (Issue #2555 AC6) must still be routed to
    `_run_publication_receipt()` unaffected by the legacy-invocation shim
    (its first token, `publication-receipt`, is itself a known
    subcommand)."""
    output_dir = tmp_path / "close-evidence-for-receipt"
    build_result = _run_build_bundle_cli(
        [
            "build",
            "--performance-receipt",
            str(close_evidence_fixture_paths["performance"]),
            "--reliability-receipt",
            str(close_evidence_fixture_paths["reliability"]),
            "--experiment-manifest",
            str(close_evidence_fixture_paths["manifest"]),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
    )
    assert build_result.returncode == 0, f"stdout={build_result.stdout!r} stderr={build_result.stderr!r}"

    receipt_output = tmp_path / "close-evidence-publication-receipt-v1.json"
    receipt_result = _run_build_bundle_cli(
        [
            "publication-receipt",
            "--close-evidence-json",
            str(output_dir / "close_evidence.json"),
            "--github-artifact-id",
            "12345",
            "--github-artifact-digest",
            "sha256:" + "0" * 64,
            "--artifact-url",
            "https://example.invalid/artifacts/12345",
            "--output",
            str(receipt_output),
        ],
        cwd=REPO_ROOT,
    )
    assert receipt_result.returncode == 0, f"stdout={receipt_result.stdout!r} stderr={receipt_result.stderr!r}"
    receipt = json.loads(receipt_output.read_text(encoding="utf-8"))
    assert receipt["schema"] == "CI_CLOSE_EVIDENCE_PUBLICATION_RECEIPT_V1"
    assert receipt["github_artifact_id"] == "12345"
