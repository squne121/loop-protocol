"""Issue #2744 AC1: deterministic canonical-carrier regression test.

``task_context_runtime_smoke_verifier.py::orchestrate_runtime_smoke()`` is
the ONE canonical Task Context runtime-smoke orchestration entrypoint
(Issue #2568 / PR #2708 fix_delta item 2). Its whole isolation guarantee
rests on always invoking the child generic runner
(``scripts/agent-ops/run_worktree_agent_runtime_smoke.py``) with BOTH
``--task-context-scope runtime_smoke`` and a run-scoped, absolute
``--task-context-state-root`` -- never omitting either flag, and never
reusing the canonical DB's own on-disk path for that state root. Before
this Issue, no regression test asserted this argv-level contract directly;
a future edit could silently drop one of the two flags (reintroducing the
exact unscoped-write accident Issue #2569/#2570 AC8 observed) without any
test failing.

This test monkeypatches ``invoke_generic_runner`` (the ONLY seam through
which ``orchestrate_runtime_smoke()`` launches the generic runner) to
capture the argv it was actually called with, WITHOUT ever spawning a real
subprocess or requiring a real ``claude`` CLI -- fully deterministic and
hermetic. It reuses this directory's shared ``migration_runner``/``db``
imports (via ``conftest.py``'s bare-module ``sys.path`` treatment) rather
than duplicating them.

Live-runtime verification of the SAME entrypoint (that these argv flags
actually keep synthetic/runtime-hook writes out of a REAL shadow canonical
DB) is Issue #2744 AC2, covered separately by
``test_runtime_smoke_verifier_live_shadow_isolation.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import task_context_config as config  # noqa: E402
import task_context_db as db  # noqa: E402
import task_context_migration_runner as migration_runner  # noqa: E402
import task_context_runtime_smoke_verifier as verifier  # noqa: E402
import task_context_service as service  # noqa: E402

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parents[1]
_RUNNER_PATH = _REPO_ROOT / "scripts" / "agent-ops" / "run_worktree_agent_runtime_smoke.py"

# PR #2745 OWNER review (F2, https://github.com/squne121/loop-protocol/pull/2745#issuecomment-5816561294):
# a fresh, uniquely-named module load of the generic runner (same
# bare-module-name-collision-avoidance pattern already used by
# ``test_runtime_smoke_verifier_live_shadow_isolation.py``), reused ONLY to
# call its own real ``build_parser()`` -- never to re-derive or duplicate its
# argv-parsing semantics. This lets the adversarial test below assert on the
# EFFECTIVE value the generic runner itself would parse (its own last-flag-
# wins ``argparse`` behavior), not merely on argv's first textual occurrence
# of a flag name.
_MODULE_NAME = "run_worktree_agent_runtime_smoke_issue_2744_canonical_carrier_regression"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _RUNNER_PATH)
assert _spec is not None and _spec.loader is not None
_runner_module = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = _runner_module
_spec.loader.exec_module(_runner_module)


def _open_canonical_conn(tmp_path: Path):
    db_file = tmp_path / "canonical" / "task-context.sqlite3"
    conn = db.connect(db_file)
    migration_runner.migrate(conn)
    return conn, db_file


def _seed_canonical_task_activity(conn) -> tuple[str, str]:
    """A pre-existing canonical Task/Activity pair, created BEFORE
    ``orchestrate_runtime_smoke()`` takes its own before/after canonical
    snapshot -- exactly the "caller's own current parent Task/Activity"
    ``roll_up_runtime_smoke_execution_run()`` documents. Because this pair
    is created before the run starts, it is present identically in both the
    before and after snapshots and never itself trips the
    tasks/activities byte-identical check."""
    task = service.create_task(conn, title="issue-2744 canonical carrier regression parent task")
    activity = service.transition_activity(conn, task["id"], kind="verification")
    return task["id"], activity["id"]


def _fake_invoke_generic_runner_factory(captured_argv: list[list[str]]):
    def _fake(argv: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess:
        captured_argv.append(list(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    return _fake


def test_given_orchestrate_runtime_smoke_when_run_then_generic_runner_argv_always_carries_scope_and_isolated_state_root(
    tmp_path, monkeypatch
):
    """GIVEN orchestrate_runtime_smoke() drives one runtime-smoke run against
    a real (tmp_path-local) canonical DB, with the generic-runner launch
    itself faked out via monkeypatch
    WHEN it invokes the child generic runner
    THEN the captured argv contains BOTH --task-context-scope=runtime_smoke
    AND an absolute, run-scoped --task-context-state-root that is NOT the
    canonical DB's own path."""
    canonical_conn, canonical_db_file = _open_canonical_conn(tmp_path)
    try:
        canonical_task_id, canonical_activity_id = _seed_canonical_task_activity(canonical_conn)

        captured_argv: list[list[str]] = []
        monkeypatch.setattr(
            verifier, "invoke_generic_runner", _fake_invoke_generic_runner_factory(captured_argv)
        )

        base_dir = tmp_path / "runtime-smoke-base"
        base_dir.mkdir()
        run_id = "det-run-canonical-carrier-1"
        expected_state_root = verifier.build_isolated_state_root(base_dir, run_id=run_id)

        result = verifier.orchestrate_runtime_smoke(
            canonical_conn,
            worktree=str(tmp_path),
            base_dir=base_dir,
            prompt_file=str(tmp_path / "prompt.txt"),
            output_dir=str(tmp_path / "output"),
            run_id=run_id,
            timeout_seconds=5.0,
            canonical_task_id=canonical_task_id,
            canonical_activity_id=canonical_activity_id,
        )

        assert len(captured_argv) == 1, (
            "orchestrate_runtime_smoke() must invoke the generic runner exactly once per run"
        )
        argv = captured_argv[0]

        assert "--task-context-scope" in argv, f"--task-context-scope missing from argv: {argv}"
        scope_idx = argv.index("--task-context-scope")
        assert argv[scope_idx + 1] == config.RUNTIME_SMOKE_SCOPE_VALUE == "runtime_smoke"

        assert "--task-context-state-root" in argv, f"--task-context-state-root missing from argv: {argv}"
        state_root_idx = argv.index("--task-context-state-root")
        carried_state_root = argv[state_root_idx + 1]
        assert Path(carried_state_root).is_absolute(), (
            f"--task-context-state-root must be absolute, got {carried_state_root!r}"
        )
        assert carried_state_root == str(expected_state_root)

        # The entire isolation property Issue #2744 protects: the carried
        # state root must never be (or contain) the canonical DB's own path.
        assert carried_state_root != str(canonical_db_file)
        assert carried_state_root != str(canonical_db_file.parent)
        assert not str(canonical_db_file).startswith(carried_state_root)

        # Using a pre-existing canonical Task/Activity for the roll-up
        # (rather than the run's own isolated seed ids) must keep the
        # canonical tasks/activities/tab_bindings/task_ref_claims tables
        # byte-identical and accept exactly the one expected execution_runs
        # addition.
        assert result.canonical_delta.status == "pass", result.canonical_delta.to_dict()
    finally:
        canonical_conn.close()


def test_given_two_separate_runs_when_orchestrate_runtime_smoke_invoked_then_each_run_gets_a_distinct_isolated_state_root(
    tmp_path, monkeypatch
):
    """GIVEN two separate orchestrate_runtime_smoke() calls sharing the same
    base_dir but distinct run_id values
    WHEN each drives its own runtime-smoke run
    THEN each call's captured generic-runner argv carries a DIFFERENT
    --task-context-state-root (never a fixed/shared path reused across
    runs) -- the "run-scoped" half of the Issue #2744 AC1 carrier contract."""
    canonical_conn, _canonical_db_file = _open_canonical_conn(tmp_path)
    try:
        canonical_task_id, canonical_activity_id = _seed_canonical_task_activity(canonical_conn)

        captured_argv: list[list[str]] = []
        monkeypatch.setattr(
            verifier, "invoke_generic_runner", _fake_invoke_generic_runner_factory(captured_argv)
        )

        base_dir = tmp_path / "runtime-smoke-base"
        base_dir.mkdir()

        for run_id in ("det-run-a", "det-run-b"):
            verifier.orchestrate_runtime_smoke(
                canonical_conn,
                worktree=str(tmp_path),
                base_dir=base_dir,
                prompt_file=str(tmp_path / "prompt.txt"),
                output_dir=str(tmp_path / f"output-{run_id}"),
                run_id=run_id,
                timeout_seconds=5.0,
                canonical_task_id=canonical_task_id,
                canonical_activity_id=canonical_activity_id,
            )

        assert len(captured_argv) == 2
        state_roots = []
        for argv in captured_argv:
            assert "--task-context-scope" in argv
            assert argv[argv.index("--task-context-scope") + 1] == config.RUNTIME_SMOKE_SCOPE_VALUE
            assert "--task-context-state-root" in argv
            state_roots.append(argv[argv.index("--task-context-state-root") + 1])

        assert state_roots[0] != state_roots[1], (
            f"expected distinct run-scoped isolated state roots, got the same path twice: {state_roots}"
        )
    finally:
        canonical_conn.close()


def test_given_caller_supplied_extra_argv_when_orchestrate_runtime_smoke_invoked_then_canonical_carrier_flags_still_present(
    tmp_path, monkeypatch
):
    """GIVEN a caller passes ``runner_argv_extra`` (additive caller-owned
    argv, e.g. ``--max-turns``)
    WHEN orchestrate_runtime_smoke() builds the child argv
    THEN the fixed --task-context-scope/--task-context-state-root carrier
    flags are still present and unmodified -- caller-supplied extra argv
    must never crowd out or shadow the canonical carrier contract."""
    canonical_conn, _canonical_db_file = _open_canonical_conn(tmp_path)
    try:
        canonical_task_id, canonical_activity_id = _seed_canonical_task_activity(canonical_conn)

        captured_argv: list[list[str]] = []
        monkeypatch.setattr(
            verifier, "invoke_generic_runner", _fake_invoke_generic_runner_factory(captured_argv)
        )

        base_dir = tmp_path / "runtime-smoke-base"
        base_dir.mkdir()
        run_id = "det-run-extra-argv"

        verifier.orchestrate_runtime_smoke(
            canonical_conn,
            worktree=str(tmp_path),
            base_dir=base_dir,
            prompt_file=str(tmp_path / "prompt.txt"),
            output_dir=str(tmp_path / "output"),
            run_id=run_id,
            timeout_seconds=5.0,
            runner_argv_extra=["--max-turns", "3"],
            canonical_task_id=canonical_task_id,
            canonical_activity_id=canonical_activity_id,
        )

        assert len(captured_argv) == 1
        argv = captured_argv[0]
        assert "--max-turns" in argv
        assert argv[argv.index("--max-turns") + 1] == "3"
        assert "--task-context-scope" in argv
        assert argv[argv.index("--task-context-scope") + 1] == config.RUNTIME_SMOKE_SCOPE_VALUE
        assert "--task-context-state-root" in argv
        assert Path(argv[argv.index("--task-context-state-root") + 1]).is_absolute()
    finally:
        canonical_conn.close()


def test_given_adversarial_extra_argv_reuses_reserved_flag_names_when_orchestrate_runtime_smoke_invoked_then_generic_runner_effective_parsed_value_still_canonical(
    tmp_path, monkeypatch
):
    """PR #2745 OWNER review (F2, https://github.com/squne121/loop-protocol/pull/2745#issuecomment-5816561294):
    GIVEN a caller passes ``runner_argv_extra`` that adversarially reuses the
    SAME reserved option names this module owns (``--task-context-scope``
    with a DIFFERENT value, ``--task-context-state-root`` with a DIFFERENT
    path) rather than merely an unrelated extra flag
    WHEN orchestrate_runtime_smoke() builds the child argv AND the generic
    runner's own real ``build_parser()`` (unmodified, imported as-is -- never
    re-derived) parses that exact captured argv
    THEN the EFFECTIVE parsed values (``Namespace.task_context_scope`` /
    ``Namespace.task_context_state_root`` -- i.e. what the child generic
    runner itself would actually act on, not just argv's first textual
    occurrence of either flag name) are still the canonical
    ``runtime_smoke`` scope and the canonical run-scoped isolated state root
    -- never the caller-supplied adversarial values. A prior version of this
    regression suite only checked ``argv.index(...)``'s FIRST occurrence,
    which cannot detect a later, effective-value-shadowing duplicate."""
    canonical_conn, _canonical_db_file = _open_canonical_conn(tmp_path)
    try:
        canonical_task_id, canonical_activity_id = _seed_canonical_task_activity(canonical_conn)

        captured_argv: list[list[str]] = []
        monkeypatch.setattr(
            verifier, "invoke_generic_runner", _fake_invoke_generic_runner_factory(captured_argv)
        )

        base_dir = tmp_path / "runtime-smoke-base"
        base_dir.mkdir()
        run_id = "det-run-adversarial-extra-argv"
        expected_state_root = verifier.build_isolated_state_root(base_dir, run_id=run_id)

        adversarial_state_root = str(tmp_path / "adversarial-caller-supplied-state-root")
        verifier.orchestrate_runtime_smoke(
            canonical_conn,
            worktree=str(tmp_path),
            base_dir=base_dir,
            prompt_file=str(tmp_path / "prompt.txt"),
            output_dir=str(tmp_path / "output"),
            run_id=run_id,
            timeout_seconds=5.0,
            runner_argv_extra=[
                "--max-turns", "3",
                "--task-context-scope", "adversarial-not-runtime-smoke",
                "--task-context-state-root", adversarial_state_root,
            ],
            canonical_task_id=canonical_task_id,
            canonical_activity_id=canonical_activity_id,
        )

        assert len(captured_argv) == 1
        argv = captured_argv[0]

        # Both the caller-supplied adversarial pair AND the canonical
        # reserved pair are present SOMEWHERE in argv (this module never
        # drops or rejects runner_argv_extra) -- the property under test is
        # which one the generic runner's OWN parser resolves to.
        assert argv.count("--task-context-scope") == 2, (
            f"expected the adversarial AND canonical --task-context-scope occurrences both present, got: {argv}"
        )
        assert argv.count("--task-context-state-root") == 2, (
            f"expected the adversarial AND canonical --task-context-state-root occurrences both present, got: {argv}"
        )

        parser = _runner_module.build_parser()
        parsed, _unrecognized = parser.parse_known_args(argv)
        assert parsed.task_context_scope == config.RUNTIME_SMOKE_SCOPE_VALUE, (
            "the generic runner's OWN parser must resolve --task-context-scope to the canonical value "
            f"even though runner_argv_extra supplied an adversarial duplicate first; got {parsed.task_context_scope!r}"
        )
        assert parsed.task_context_state_root == str(expected_state_root), (
            "the generic runner's OWN parser must resolve --task-context-state-root to the canonical "
            "run-scoped isolated path even though runner_argv_extra supplied an adversarial duplicate "
            f"first; got {parsed.task_context_state_root!r}"
        )
        assert parsed.task_context_state_root != adversarial_state_root
    finally:
        canonical_conn.close()
