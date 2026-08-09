"""Issue #2015 AC11 (OWNER Scope Reframe 2026-08-09): hermetic regression
coverage for ``run_agent_provider_route_smoke.py``'s bounded artifact
materialization poll, route-level shared absolute deadline (initial attempt
+ retry), ``--require-route-pass``, and secondary-failure preservation.

Every test here uses a fake/injectable clock (``time.monotonic`` /
``time.sleep`` monkeypatched to a deterministic counter, never a real
wall-clock sleep) and a fake ``subprocess.run`` that never spawns a live
Claude Code / Codex CLI process -- these tests validate the harness's own
control-flow logic only.
"""
from __future__ import annotations

import importlib.util
import subprocess
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCER_PATH = REPO_ROOT / "scripts" / "agent-ops" / "run_agent_provider_route_smoke.py"

_REAL_SUBPROCESS_RUN = subprocess.run


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def producer() -> types.ModuleType:
    # Fresh module per test (not module-scoped): several tests monkeypatch
    # module-level functions/attributes (subprocess.run, time.monotonic).
    return _load_module(PRODUCER_PATH, "test_route_smoke_deadline_producer")


class _FakeClock:
    """Deterministic, monotonically-increasing fake clock. Each
    ``monotonic()`` call advances by ``step`` seconds -- no real time passes,
    keeping bounded-poll tests fast while still exercising the real
    ``while time.monotonic() < deadline`` loop structure."""

    def __init__(self, start: float = 1000.0, step: float = 0.6) -> None:
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def sleep(self, _seconds: float) -> None:
        # No real sleep -- the fake monotonic() advance above is what
        # actually moves the bounded-poll loop toward its deadline.
        return None


def _argv_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _fake_harness_run(*, harness_summary: dict, evidence_files: dict[str, str] | None = None, returncode: int = 0):
    """Builds a fake ``subprocess.run`` replacement that writes
    ``summary.md`` (in the harness's own ``- key: value`` format) to the
    ``--output-dir`` the producer passed, and optionally pre-seeds evidence
    files (e.g. ``delegation_result.json``) into the sibling ``-evidence``
    directory the producer itself creates before invoking the harness.

    Only intercepts the actual runtime-smoke-harness invocation -- every
    other ``subprocess.run`` call the producer module makes (``git
    rev-parse``, ``<bin> --version``) is passed straight through to the
    real ``subprocess.run`` unmodified, since monkeypatching
    ``producer.subprocess.run`` replaces it for the WHOLE module, not just
    the one call site under test.
    """

    def _run(argv, **kwargs):  # noqa: ANN001
        if not (len(argv) > 1 and str(argv[1]).endswith("run_worktree_agent_runtime_smoke.py")):
            return _REAL_SUBPROCESS_RUN(argv, **kwargs)
        output_dir = Path(_argv_value(argv, "--output-dir"))
        output_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Runtime Smoke Summary", ""]
        for key, value in harness_summary.items():
            lines.append(f"- {key}: {value}")
        (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if evidence_files:
            evidence_dir = Path(str(output_dir) + "-evidence")
            evidence_dir.mkdir(parents=True, exist_ok=True)
            for name, content in evidence_files.items():
                (evidence_dir / name).write_text(content, encoding="utf-8")
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")

    return _run


_PASS_REQUEST_JSON = (
    '{"provider": "agy", "tool_profile": "local_asset_research", "prompt": "probe"}'
)
_PASS_RESULT_JSON = '{"ok": true, "provider": "agy"}'


class TestArtifactMaterializationBoundedPoll:
    def test_artifact_appears_after_a_short_delay_is_tolerated(self, producer, tmp_path, monkeypatch):
        """Child completed (per harness evidence); delegation_result.json is
        NOT present on the first check but appears after a couple of fake
        poll ticks -- the bounded poll must tolerate this lag rather than
        immediately classifying it as un-materialized."""
        clock = _FakeClock()
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        calls = {"n": 0}
        real_is_file = Path.is_file

        def _delayed_is_file(self):  # noqa: ANN001
            if self.name == "delegation_result.json":
                calls["n"] += 1
                if calls["n"] < 3:
                    return False
                self.write_text(_PASS_RESULT_JSON, encoding="utf-8")
                return True
            return real_is_file(self)

        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        monkeypatch.setattr(Path, "is_file", _delayed_is_file)
        route = producer.REQUIRED_ROUTES[0]
        monkeypatch.setattr(
            producer.subprocess,
            "run",
            _fake_harness_run(
                harness_summary={
                    "native_spawn_event_observed": True,
                    "parent_session_id": "parent-1",
                    "child_session_id": "child-1",
                    "child_spawn_observed": True,
                    "child_completion_observed": True,
                    "child_completion_source": "hook_subagent_stop",
                },
                evidence_files={"delegation_request.json": _PASS_REQUEST_JSON},
            ),
        )
        artifact, diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=60,
        )
        assert diagnostics["evidence_materialized_elapsed_sec"] > 0.0
        assert not diagnostics["secondary_failures"]

    def test_artifact_never_materializes_is_classified_not_silently_discarded(
        self, producer, tmp_path, monkeypatch
    ):
        """AC11: completion observed but the artifact never shows up even
        after the bounded poll -- must be classified as
        ``child_completed_but_artifact_not_materialized``, never silently
        treated as "no evidence"/PASS."""
        clock = _FakeClock(step=1.0)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer.REQUIRED_ROUTES[0]
        monkeypatch.setattr(
            producer.subprocess,
            "run",
            _fake_harness_run(
                harness_summary={
                    "native_spawn_event_observed": True,
                    "parent_session_id": "parent-1",
                    "child_session_id": "child-1",
                    "child_spawn_observed": True,
                    "child_completion_observed": True,
                    "child_completion_source": "hook_subagent_stop",
                },
                evidence_files={"delegation_request.json": _PASS_REQUEST_JSON},
            ),
        )
        artifact, diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=5,
        )
        assert artifact["status"] == "fail"
        kinds = [f["kind"] for f in diagnostics["secondary_failures"]]
        assert "child_completed_but_artifact_not_materialized" in kinds


class TestRouteLevelSharedDeadline:
    def test_retry_gets_a_shrunk_remaining_budget_not_a_fresh_full_timeout(
        self, producer, tmp_path, monkeypatch
    ):
        """Issue #2015 AC11/P1: the retry attempt must NOT receive a fresh,
        independent ``timeout_seconds`` budget -- it must receive the
        REMAINING budget of the single route-level absolute deadline shared
        with the initial attempt (proven here by the retry's own harness
        ``--timeout-seconds`` staying strictly below the full
        ``timeout_seconds`` requested, even though a fresh/independent
        budget would equal it exactly).

        Issue #2015 P1 fix (control-plane live re-run, 2026-08-09): a
        SEPARATE invariant -- that the initial attempt's own deadline is
        capped below the full route budget so a retry always gets a
        guaranteed minimum slice -- is covered by
        ``test_initial_attempt_is_capped_below_the_full_route_budget``
        below. The two attempts' relative sizes are NOT compared here: an
        initial attempt that fails fast (as in this fixture) naturally
        leaves the retry MORE of the shared budget than the initial
        attempt's own (deliberately capped) allotment -- that is correct,
        intended behavior of the fix, not a regression."""
        clock = _FakeClock(step=25.0)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        seen_timeouts: list[str] = []
        real_fake = _fake_harness_run(
            harness_summary={
                "native_spawn_event_observed": False,
                "parent_session_id": "parent-1",
                "child_session_id": "",
            },
        )

        def _run(argv, **kwargs):  # noqa: ANN001
            if len(argv) > 1 and str(argv[1]).endswith("run_worktree_agent_runtime_smoke.py"):
                seen_timeouts.append(_argv_value(argv, "--timeout-seconds"))
            return real_fake(argv, **kwargs)

        monkeypatch.setattr(producer.subprocess, "run", _run)
        producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=120,
        )
        assert len(seen_timeouts) == 2, "expected exactly one bounded retry"
        first, second = int(seen_timeouts[0]), int(seen_timeouts[1])
        assert second < 120, (
            "retry must receive a SHRUNK remaining budget from the shared "
            "route deadline, never a fresh full timeout_seconds"
        )
        assert first < 120 and second != first

    def test_initial_attempt_is_capped_below_the_full_route_budget(
        self, producer, tmp_path, monkeypatch
    ):
        """Issue #2015 P1 fix: the initial attempt must never be handed the
        entire route-level budget -- it must be capped to at most
        ``INITIAL_ATTEMPT_MAX_BUDGET_FRACTION`` of ``timeout_seconds``, so a
        retry-eligible failure always leaves the retry a real, non-negligible
        chance (never a near-zero remaining budget)."""
        clock = _FakeClock(step=0.1)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer.REQUIRED_ROUTES[0]
        seen_timeouts: list[str] = []
        real_fake = _fake_harness_run(
            harness_summary={
                "native_spawn_event_observed": True,
                "parent_session_id": "parent-1",
                "child_session_id": "child-1",
            },
            evidence_files={
                "delegation_request.json": _PASS_REQUEST_JSON,
                "delegation_result.json": _PASS_RESULT_JSON,
            },
        )

        def _run(argv, **kwargs):  # noqa: ANN001
            if len(argv) > 1 and str(argv[1]).endswith("run_worktree_agent_runtime_smoke.py"):
                seen_timeouts.append(_argv_value(argv, "--timeout-seconds"))
            return real_fake(argv, **kwargs)

        monkeypatch.setattr(producer.subprocess, "run", _run)
        artifact, _diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=100,
        )
        assert artifact["status"] == "pass"
        assert len(seen_timeouts) == 1, "a genuine pass must never trigger a retry"
        first = int(seen_timeouts[0])
        assert first <= int(100 * producer.INITIAL_ATTEMPT_MAX_BUDGET_FRACTION) + 1, (
            "initial attempt must be capped below the full route budget "
            "to guarantee the retry a real remaining slice"
        )

    def test_attempts_ledger_records_both_attempts(self, producer, tmp_path, monkeypatch):
        clock = _FakeClock(step=1.0)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer._find_route("codex_cli", "codebase-investigator", "local_asset_research")
        monkeypatch.setattr(
            producer.subprocess,
            "run",
            _fake_harness_run(
                harness_summary={
                    "native_spawn_event_observed": False,
                    "parent_session_id": "parent-1",
                    "child_session_id": "",
                },
            ),
        )
        _artifact, diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=60,
        )
        assert len(diagnostics["attempts"]) == 2
        assert diagnostics["route_deadline_sec"] == 60


class TestSecondaryFailurePreservation:
    def test_nonzero_harness_exit_with_spawn_evidence_is_preserved_not_discarded(
        self, producer, tmp_path, monkeypatch
    ):
        """AC11: 'harness exits nonzero but spawn was genuinely observed
        (must preserve that evidence)'."""
        clock = _FakeClock(step=0.1)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer.REQUIRED_ROUTES[0]
        monkeypatch.setattr(
            producer.subprocess,
            "run",
            _fake_harness_run(
                harness_summary={
                    "native_spawn_event_observed": True,
                    "parent_session_id": "parent-1",
                    "child_session_id": "child-1",
                    "child_spawn_observed": True,
                },
                returncode=1,
            ),
        )
        artifact, diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=60,
        )
        assert artifact["status"] == "fail"
        # Spawn evidence must still be visible in the artifact itself.
        assert artifact["spawn"]["native_spawn_event_observed"] is True
        kinds = [f["kind"] for f in diagnostics["secondary_failures"]]
        assert "nonzero_harness_exit_with_spawn_evidence" in kinds

    def test_producer_exit_zero_but_route_status_fail_is_not_reported_as_pass(
        self, producer, tmp_path, monkeypatch
    ):
        clock = _FakeClock(step=0.1)
        monkeypatch.setattr(producer.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(producer.time, "sleep", clock.sleep)
        route = producer.REQUIRED_ROUTES[0]
        monkeypatch.setattr(
            producer.subprocess,
            "run",
            _fake_harness_run(
                harness_summary={
                    "native_spawn_event_observed": False,
                    "parent_session_id": "",
                    "child_session_id": "",
                },
                returncode=0,
            ),
        )
        artifact, _diagnostics = producer.run_route_live(
            route, repo_root=REPO_ROOT, worktree=REPO_ROOT,
            output_root=tmp_path, timeout_seconds=60,
        )
        assert artifact["status"] == "fail"
        assert artifact["failure_class"] == "spawn_not_observed"


class TestRequireRoutePassFlag:
    def test_require_route_pass_exits_nonzero_when_dry_run_produces_skip(
        self, producer, tmp_path
    ):
        rc = producer.main([
            "--runtime", "codex_cli", "--agent", "web-researcher", "--profile", "grounded_research",
            "--dry-run", "--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT),
            "--require-route-pass",
        ])
        assert rc == 1

    def test_without_require_route_pass_dry_run_still_exits_zero(self, producer, tmp_path):
        rc = producer.main([
            "--runtime", "codex_cli", "--agent", "web-researcher", "--profile", "grounded_research",
            "--dry-run", "--output-dir", str(tmp_path), "--repo-root", str(REPO_ROOT),
        ])
        assert rc == 0
