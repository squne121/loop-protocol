"""
test_skill_runtime_exec_session_manifest.py

Issue #1546: Codex Stop/SubagentStop session manifest peer-write vs.
skill_runtime_exec.py runtime guard interaction. Covers AC3/AC4/AC5/AC10:

- AC3: an independent peer writer publishing the canonical *external*
  session manifest (outside the repository tree entirely) is never reported
  as an unauthorized_write_path false positive by the executor's generic
  repo-status diff -- no session-manifest-specific allowlist/typed policy is
  needed or added for this, because the write is structurally invisible to
  a repo-tree `git status` diff.
- AC4 (regression gate): verified by the Issue's separate
  `git diff --exit-code -- scripts/agent-guards/skill_runtime_exec.py`
  Verification Command, not by this file.
- AC5: an independent peer's repo-local `tmp/session-manifests/**` write
  (plain create, and a non-regular/sibling variant) fails closed via the
  existing generic postcondition -- the target object is never actually
  written to a *tracked* state (it is created, then this test removes it as
  part of proving the write was observed as unauthorized, matching the other
  runtime-exec negative tests in this directory).
- AC10: this whole module, run together, is the AC10 runtime evidence VC.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as sre  # noqa: E402

ADAPTER = REPO_ROOT / "scripts" / "session-recording" / "codex-hook-adapter.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codex"
_ISSUE_NUMBER = "999999"


def _run_stop_hook(env: dict) -> subprocess.CompletedProcess[str]:
    payload = json.loads((FIXTURES / "positive_fixture.json").read_text())
    return subprocess.run(
        ["node", str(ADAPTER), "--event", "Stop"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )


def test_external_session_manifest_peer_write_does_not_stop_preflight(tmp_path: Path) -> None:
    """AC3: a real Stop-event session manifest peer write to the canonical
    external per-user state root (isolated under a tmp XDG_STATE_HOME for
    this test) is invisible to the executor's repo-tree diff -- it never
    produces an unauthorized_write_path false positive."""
    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    env = os.environ.copy()
    env.pop("CODEX_HOOK_MANIFEST_ROOT", None)
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    # Issue #1546 CI fix_delta (PR #1586): the python-test CI job runs this
    # test without a pnpm install step, so scripts/generate-session-manifest.mjs
    # --validate (which loads ajv/ajv-formats) is unavailable there for a
    # reason unrelated to this AC. AC3 verifies that the external manifest
    # write is invisible to the executor's repo-tree diff, not the
    # producer's own JSON schema validation, so requesting --no-validate via
    # this test-only override does not weaken what AC3 proves.
    env["CODEX_SESSION_RECORDING_SKIP_VALIDATE"] = "1"

    result = _run_stop_hook(env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"continue": True}
    assert result.stderr == ""

    manifests = list(
        (tmp_path / "state").glob("loop-protocol/session-manifests/v1/*/codex/stop/*.json")
    )
    assert len(manifests) == 1, manifests

    unauthorized = sre._find_unauthorized_repo_changes(
        str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
    )
    assert unauthorized is None


def test_repo_local_session_manifest_write_is_rejected(tmp_path: Path) -> None:
    """AC5: an independent peer's plain repo-local
    tmp/session-manifests/codex/** create fails closed via the existing
    generic postcondition (no session-manifest-specific allowlist)."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    target_dir.mkdir(parents=True, exist_ok=True)
    dir_preexisted = True
    if not any(target_dir.parent.iterdir()) and not target_dir.exists():
        dir_preexisted = False

    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    rogue_file = target_dir / "rogue-peer-write.json"
    try:
        rogue_file.write_text(json.dumps({"rogue": True}))

        unauthorized = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized is not None, "expected the repo-local peer write to fail closed"
    finally:
        rogue_file.unlink(missing_ok=True)
        if not dir_preexisted:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)


def test_repo_local_session_manifest_nonregular_and_sibling_writes_are_rejected(tmp_path: Path) -> None:
    """AC5: non-regular (symlink) and sibling repo-local
    tmp/session-manifests/codex/** writes also fail closed via the existing
    generic postcondition."""
    target_dir = REPO_ROOT / "tmp" / "session-manifests" / "codex" / "stop"
    dir_preexisted_before_test = (REPO_ROOT / "tmp" / "session-manifests").exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    before_snapshot = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
    before_status = sre._git_status_paths(str(REPO_ROOT))

    symlink_path = target_dir / "rogue-symlink.json"
    sibling_path = target_dir.parent / "rogue-sibling.json"
    try:
        symlink_path.symlink_to(REPO_ROOT / "README.md")

        unauthorized_symlink = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot, before_status
        )
        assert unauthorized_symlink is not None, "expected symlink substitution to fail closed"
        symlink_path.unlink(missing_ok=True)

        before_snapshot_2 = sre._snapshot_repo_paths(str(REPO_ROOT), _ISSUE_NUMBER)
        before_status_2 = sre._git_status_paths(str(REPO_ROOT))
        sibling_path.write_text(json.dumps({"sibling": True}))

        unauthorized_sibling = sre._find_unauthorized_repo_changes(
            str(REPO_ROOT), _ISSUE_NUMBER, before_snapshot_2, before_status_2
        )
        assert unauthorized_sibling is not None, "expected sibling write to fail closed"
    finally:
        symlink_path.unlink(missing_ok=True)
        sibling_path.unlink(missing_ok=True)
        if not dir_preexisted_before_test:
            shutil.rmtree(REPO_ROOT / "tmp" / "session-manifests", ignore_errors=True)
