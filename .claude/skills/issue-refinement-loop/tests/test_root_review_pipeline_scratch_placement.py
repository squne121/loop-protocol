"""Issue #3039: root review producer scratch placement vs. the canonical executor.

`run_root_review_pipeline.py produce` used to create its per-invocation scratch
(pinned body + history snapshot) under `<repo>/tmp/root_review_pipeline_*`.
The canonical executor (`scripts/agent-guards/skill_runtime_exec.py`,
`_dispatch_child_and_check_postconditions()`) walks the repo before and after a
child runs, so a peer `produce` appearing or disappearing in that window made a
legitimate `repair_action.apply` child fail with `unauthorized_write_path`
(Issue #3035 investigation). The fix moves the scratch to the system temporary
directory (outside the repo).

AC1: `produce` scratch is never created under the repo root's `tmp/`.
AC2: with the post-fix placement, an independent peer write does not make a
     legitimate child fail (outer exit 0).
AC3: negative controls -- a child writing the same `tmp/root_review_pipeline_*`
     path, and a peer writing it under the pre-fix placement, are still
     rejected with outer exit 2 / `unauthorized_write_path`.

The executor fixtures are hermetic: an isolated `git init` repo under
`tmp_path`, the production executor, real child / peer subprocesses, and a
barrier directory OUTSIDE the fixture repo (file-based, no sleep-based
synchronisation).
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = ROOT / ".claude" / "skills" / "issue-refinement-loop" / "scripts"
PIPELINE_SCRIPT = SCRIPTS_DIR / "run_root_review_pipeline.py"
EXECUTOR_SCRIPT = ROOT / "scripts" / "agent-guards" / "skill_runtime_exec.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_REPO = "squne121/loop-protocol"
_ISSUE_NUMBER = 3039
_COMMAND_ID = "repair_action.apply"
# Pre-fix scratch layout, relative to a repo root (used by negative controls).
_PRE_FIX_SCRATCH_REL = Path("tmp") / "root_review_pipeline_x" / "root_review_pipeline_body_y.md"
_BARRIER_TIMEOUT_SECONDS = 30.0


def _load_module(unique_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(unique_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


_PIPELINE = _load_module("run_root_review_pipeline_scratch_placement", PIPELINE_SCRIPT)

_BODY = """## Machine-Readable Contract

```yaml
contract_schema_version: v1
issue_kind: research
parent_issue: none
goal_ref: "Issue #3039 scratch placement fixture"
change_kind: research
```

## Outcome

Fixture body for the scratch placement regression test (Issue #3039).

## Acceptance Criteria

- [ ] AC1: fixture body is well-formed enough for the real checker chain.

## Verification Commands

```bash
# AC1
# baseline-expect: pass
$ true
```

## Allowed Paths

- fixture/scratch_placement.md
"""


# ---------------------------------------------------------------------------
# AC1: produce scratch placement
# ---------------------------------------------------------------------------


def test_given_produce_when_run_then_scratch_is_not_created_under_repo_tmp(tmp_path: Path, monkeypatch):
    repo_root = tmp_path / "fake_repo_root"
    repo_root.mkdir()
    monkeypatch.setattr(_PIPELINE, "_REPO_ROOT", repo_root)

    body_sha256 = _PIPELINE.sha256_of(_BODY)
    monkeypatch.setattr(
        _PIPELINE,
        "fetch_and_pin_live_body",
        lambda issue_number, repo, timeout_seconds=15: (_BODY, body_sha256, None),
    )

    observed_scratch_dirs: list[str] = []
    real_write_pinned_body = _PIPELINE.write_pinned_body_tempfile

    def _spy(body: str, *, dir: str | None = None) -> str:
        assert dir is not None, "produce must pass an explicit per-invocation scratch dir"
        observed_scratch_dirs.append(dir)
        return real_write_pinned_body(body, dir=dir)

    monkeypatch.setattr(_PIPELINE, "write_pinned_body_tempfile", _spy)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _PIPELINE._cmd_produce(argparse.Namespace(issue_number=_ISSUE_NUMBER, repo=_REPO))
    out = json.loads(buf.getvalue())
    assert rc == 0 and out["status"] == "ok", out

    assert len(observed_scratch_dirs) == 1, observed_scratch_dirs
    scratch = Path(observed_scratch_dirs[0]).resolve()
    for forbidden_root in (repo_root.resolve(), (repo_root / "tmp").resolve(), (ROOT / "tmp").resolve()):
        assert not scratch.is_relative_to(forbidden_root), (scratch, forbidden_root)
    assert scratch.name.startswith("root_review_pipeline_")
    assert scratch.parent == Path(tempfile.gettempdir()).resolve()
    # The per-invocation scratch is cleaned up on exit and never created a repo `tmp/`.
    assert not scratch.exists()
    assert not (repo_root / "tmp").exists()


# ---------------------------------------------------------------------------
# AC2 / AC3: hermetic executor fixture
# ---------------------------------------------------------------------------

# Child: signals "started" (the executor's before-snapshot is already captured),
# optionally waits for the peer to finish, optionally writes `target` itself.
_CHILD_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path

    barrier, wait_for_peer, target = Path(sys.argv[1]), sys.argv[2] == "1", sys.argv[3]
    (barrier / "child_started").write_text("1")
    if wait_for_peer:
        deadline = time.monotonic() + float(sys.argv[4])
        while not (barrier / "peer_done").exists():
            if time.monotonic() > deadline:
                sys.exit(97)
            time.sleep(0.005)
    if target:
        t = Path(target)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("child wrote this\\n")
    print("CHILD_SEMANTIC_RESULT: ok")
    """
)

# Peer: an independent process that, once the child has started, creates and
# keeps a scratch file at `target` (or, if `target` is empty, a fresh dir in the
# system temp directory reproducing the post-fix placement), then signals done.
_PEER_SCRIPT = textwrap.dedent(
    """
    import sys, tempfile, time
    from pathlib import Path

    barrier, target, timeout = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
    deadline = time.monotonic() + timeout
    while not (barrier / "child_started").exists():
        if time.monotonic() > deadline:
            sys.exit(98)
        time.sleep(0.005)
    if target:
        t = Path(target)
        t.parent.mkdir(parents=True, exist_ok=True)
    else:
        d = Path(tempfile.mkdtemp(prefix="root_review_pipeline_"))
        t = d / "root_review_pipeline_body_peer.md"
        (barrier / "peer_scratch_dir").write_text(str(d))
    t.write_text("peer body\\n")
    (barrier / "peer_done").write_text("1")
    """
)


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


@pytest.fixture
def executor_module(monkeypatch):
    monkeypatch.syspath_prepend(str(EXECUTOR_SCRIPT.parent))
    return _load_module("skill_runtime_exec_scratch_placement_3039", EXECUTOR_SCRIPT)


@pytest.fixture
def fixture_env(tmp_path: Path):
    repo = tmp_path / "fixture_repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / ".gitignore").write_text("/tmp/\n")
    (repo / "README.md").write_text("fixture\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    # Barrier + helper scripts live OUTSIDE the fixture repo so they never
    # appear in the executor's snapshot.
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "child.py").write_text(_CHILD_SCRIPT)
    (helpers / "peer.py").write_text(_PEER_SCRIPT)
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    return repo, helpers, barrier


def _run_dispatch(executor, repo: Path, helpers: Path, barrier: Path, *, child_target: str, peer_target: str | None):
    """Run the production executor dispatch; `peer_target=None` means no peer,
    `""` means a peer using the post-fix (system temp) placement."""
    peer = None
    if peer_target is not None:
        peer = subprocess.Popen(
            [sys.executable, str(helpers / "peer.py"), str(barrier), peer_target, str(_BARRIER_TIMEOUT_SECONDS)]
        )
    child_argv = [
        sys.executable,
        str(helpers / "child.py"),
        str(barrier),
        "1" if peer is not None else "0",
        child_target,
        str(_BARRIER_TIMEOUT_SECONDS),
    ]
    try:
        rc = executor._dispatch_child_and_check_postconditions(
            dispatch_root=str(repo),
            issue_number=_ISSUE_NUMBER,
            command_id=_COMMAND_ID,
            child_argv=child_argv,
            env=dict(os.environ),
            timeout_seconds=60,
            binary_output=False,
        )
    finally:
        if peer is not None:
            assert peer.wait(timeout=_BARRIER_TIMEOUT_SECONDS) == 0, "peer subprocess did not complete"
    return rc


def test_given_clean_fixture_when_child_runs_alone_then_outer_exit_0(executor_module, fixture_env, capfd):
    repo, helpers, barrier = fixture_env
    rc = _run_dispatch(executor_module, repo, helpers, barrier, child_target="", peer_target=None)
    assert rc == 0, capfd.readouterr().err


def test_given_post_fix_placement_peer_when_child_runs_then_outer_exit_0(executor_module, fixture_env, capfd):
    """AC2: a peer scratch in the system temp dir (outside the repo) is invisible
    to the executor's repo snapshot even while it exists across the child run."""
    repo, helpers, barrier = fixture_env
    rc = None
    try:
        rc = _run_dispatch(executor_module, repo, helpers, barrier, child_target="", peer_target="")
        captured = capfd.readouterr()
        assert rc == 0, captured.err
        assert "unauthorized_write_path" not in captured.err
        peer_dir = Path((barrier / "peer_scratch_dir").read_text())
        assert peer_dir.is_dir() and (peer_dir / "root_review_pipeline_body_peer.md").is_file()
        assert not peer_dir.resolve().is_relative_to(repo.resolve())
    finally:
        peer_dir_file = barrier / "peer_scratch_dir"
        if peer_dir_file.exists():
            shutil.rmtree(peer_dir_file.read_text(), ignore_errors=True)
    # The executor may pre-create an empty `tmp/` node; no scratch may be inside it.
    assert not list(repo.glob("tmp/root_review_pipeline_*"))


def test_given_child_writes_scratch_path_when_run_then_outer_exit_2_unauthorized_write_path(
    executor_module, fixture_env, capfd
):
    """AC3 negative control: the child itself writing the pre-fix scratch path is rejected."""
    repo, helpers, barrier = fixture_env
    rc = _run_dispatch(
        executor_module, repo, helpers, barrier, child_target=str(repo / _PRE_FIX_SCRATCH_REL), peer_target=None
    )
    captured = capfd.readouterr()
    assert rc == 2, captured.err
    assert "unauthorized_write_path" in captured.err


def test_given_pre_fix_placement_peer_when_child_runs_then_outer_exit_2_unauthorized_write_path(
    executor_module, fixture_env, capfd
):
    """AC3 failure-class reproduction: a peer holding the PRE-FIX scratch path
    under the repo `tmp/` during the child run makes a legitimate child fail."""
    repo, helpers, barrier = fixture_env
    rc = _run_dispatch(
        executor_module, repo, helpers, barrier, child_target="", peer_target=str(repo / _PRE_FIX_SCRATCH_REL)
    )
    captured = capfd.readouterr()
    assert rc == 2, captured.err
    assert "unauthorized_write_path" in captured.err
    assert (repo / _PRE_FIX_SCRATCH_REL).is_file()
