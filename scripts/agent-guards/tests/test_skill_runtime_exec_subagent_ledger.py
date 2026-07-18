"""
test_skill_runtime_exec_subagent_ledger.py

Typed SubAgent-launch-ledger transition policy tests for skill_runtime_exec.py
(Issue #1502). Covers AC1-AC6:

- AC1: cold/warm real `check-codex-agents.mjs --hook-subagent-start` peer
  writes to the ledger final file never produce an unauthorized_write_path
  false positive, and the native writer build artifact never lands in a
  repo-local `tmp/subagent-launch-ledger-writer*` path.
- AC2: the stable-exact ledger policy allows only `absent -> regular` and
  `regular -> regular`; delete/symlink/directory/FIFO/socket/device
  transitions fail closed.
- AC3: transient `.lock` / `.tmp` protocol entries are allowed only within a
  bounded quiescence window; residue that outlives the window fails closed.
- AC4: siblings of the three exact ledger paths under `artifacts/codex/`
  remain subject to the ordinary fail-closed diff (no directory-wide
  exclusion).
- AC5: this whole file, run together, exercises cold/warm hook integration,
  ignored-ancestor folding compatibility, non-regular substitution, and the
  pre-existing race-tolerant roots / allowed-artifact-root regression.
- AC6: the SSOT documents the four typed categories and the stdlib mode
  guarantee limit, referencing #1363 as the strict-attribution handoff.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARDS_DIR = Path(__file__).resolve().parent.parent
if str(_GUARDS_DIR) not in sys.path:
    sys.path.insert(0, str(_GUARDS_DIR))

import skill_runtime_exec as sre  # noqa: E402

WRITER_SOURCE = REPO_ROOT / "scripts" / "subagent-launch-ledger-writer.c"
HOOK_SOURCE = REPO_ROOT / "scripts" / "check-codex-agents.mjs"

_LEDGER_REL = "artifacts/codex/subagent-launch-ledger.json"
_LEDGER_LOCK_REL = f"{_LEDGER_REL}.lock"
_LEDGER_TMP_REL = f"{_LEDGER_REL}.tmp"


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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/squne121/loop-protocol.git", cwd=repo)
    # Issue #1502 REQUEST_CHANGES (Blocker 5): mirror the real repo's
    # `.gitignore` `artifacts/` ignore rule so fixture-repo tests exercise
    # the same Git-status ignored-directory-folding behavior
    # (`_expand_new_status_paths` / `_strict_ancestor_of_race_tolerant_root`)
    # as the real repo, instead of only the un-ignored case.
    (repo / ".gitignore").write_text("__pycache__/\ntmp/\nartifacts/\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _install_hook_fixture(repo: Path) -> None:
    """Copy the real hook + real writer source into the fixture repo, with a
    per-test nonce appended to the writer source so its content hash (and
    therefore the content-addressed build cache key) is guaranteed unique,
    forcing a genuinely cold build on first invocation."""
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    nonce = f"\n/* test-nonce: {uuid.uuid4().hex} */\n"
    (repo / "scripts" / "subagent-launch-ledger-writer.c").write_text(
        WRITER_SOURCE.read_text(encoding="utf-8") + nonce, encoding="utf-8"
    )
    # The hook script itself is invoked from its real on-disk location (not
    # copied into the fixture), so its `sourceRepoRoot`-derived fixture
    # fallback (e.g. the runtime-contract fixture path) still resolves
    # against the real repo tree, matching tests/test_subagent_launch_ledger_writer.py's
    # existing pattern for --hook-subagent-start. Only `repoRoot` (via
    # REPO_ROOT_OVERRIDE) points at this fixture.
    agents_dir = repo / ".codex" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "spark-skim.toml").write_text(
        'model = "gpt-5.3-codex-spark"\n'
        'model_reasoning_effort = "medium"\n'
        'default_permissions = "loop-protocol-readonly"\n',
        encoding="utf-8",
    )


def _ledger_before_state(repo: Path) -> tuple[dict[str, str], bytes | None, dict[str, str]]:
    """Snapshot the typed ledger before-state (exact-path kinds, raw
    before-content bytes when the stable ledger is currently `"regular"`, and
    ancestor directory-node kinds), matching what `main()` captures before
    the child command runs (Issue #1502 REQUEST_CHANGES Blocker 3 / 5)."""
    kinds = sre._ledger_exact_kinds(str(repo))
    ancestor_kinds = sre._ledger_ancestor_kinds(str(repo))
    before_bytes = (
        sre._read_bytes_or_none(repo / _LEDGER_REL)
        if kinds.get(_LEDGER_REL) == "regular"
        else None
    )
    return kinds, before_bytes, ancestor_kinds


def _run_hook(repo: Path, *, session_id: str, turn_id: str, agent_id: str) -> subprocess.CompletedProcess[str]:
    payload = {
        "agent_type": "spark-skim",
        "model": "gpt-5.3-codex-spark",
        "session_id": session_id,
        "turn_id": turn_id,
        "agent_id": agent_id,
    }
    return subprocess.run(
        ["node", str(HOOK_SOURCE), "--hook-subagent-start"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "REPO_ROOT_OVERRIDE": str(repo),
            "CODEX_AGENT_EVIDENCE_RUN_ID": "run",
            "CODEX_AGENT_EVIDENCE_HEAD_SHA": "a" * 40,
        },
    )


def test_hook_cold_and_warm_ledger_peer_write_is_quiescent(tmp_path: Path) -> None:
    """GIVEN the real check-codex-agents.mjs --hook-subagent-start hook
    WHEN it is invoked cold (no cached writer binary) and then warm (cached
    writer binary reused) against the same fixture repo
    THEN neither invocation creates/updates a repo-local
    tmp/subagent-launch-ledger-writer* build artifact, and the executor's
    typed ledger policy never reports the peer ledger create/update as an
    unauthorized_write_path false positive."""
    repo = _make_repo(tmp_path)
    _install_hook_fixture(repo)

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds, ledger_before_content, ledger_ancestor_before_kinds = _ledger_before_state(repo)
    assert ledger_before_kinds[_LEDGER_REL] == "absent"

    cold = _run_hook(repo, session_id="s1", turn_id="t1", agent_id="a1")
    assert cold.returncode == 0, cold.stderr

    # AC1: no repo-local build artifact, cold or warm.
    repo_tmp_dir = repo / "tmp"
    if repo_tmp_dir.exists():
        assert not any("subagent-launch-ledger-writer" in p.name for p in repo_tmp_dir.iterdir())

    stale = sre._wait_for_ledger_transient_quiescence(str(repo))
    assert stale == []

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        ledger_before_content,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized is None

    ledger_after_cold_kinds = sre._ledger_exact_kinds(str(repo))
    assert ledger_after_cold_kinds[_LEDGER_REL] == "regular"

    # Warm: same source content (same fixture files, unchanged), so the
    # cached binary is reused without a rebuild.
    before_snapshot_2 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_2 = sre._git_status_paths(str(repo))
    ledger_before_kinds_2, ledger_before_content_2, ledger_ancestor_before_kinds_2 = _ledger_before_state(repo)

    warm = _run_hook(repo, session_id="s2", turn_id="t2", agent_id="a2")
    assert warm.returncode == 0, warm.stderr

    if repo_tmp_dir.exists():
        assert not any("subagent-launch-ledger-writer" in p.name for p in repo_tmp_dir.iterdir())

    stale_2 = sre._wait_for_ledger_transient_quiescence(str(repo))
    assert stale_2 == []

    unauthorized_2 = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot_2,
        before_status_2,
        ledger_before_kinds_2,
        ledger_before_content_2,
        ledger_ancestor_before_kinds_2,
    )
    assert unauthorized_2 is None

    ledger = json.loads((repo / _LEDGER_REL).read_text())
    assert [entry["observed_dispatch"]["agent_id"] for entry in ledger["launches"]] == ["a1", "a2"]


def test_ledger_exact_policy_rejects_nonregular_and_delete_transitions(tmp_path: Path) -> None:
    """GIVEN the stable-exact ledger final file
    WHEN it transitions to delete, symlink, directory, FIFO, socket, or
    device (from absent or from regular)
    THEN the typed policy fails closed on that exact path, while
    absent -> regular and regular -> regular remain authorized."""
    assert sre._is_allowed_stable_ledger_transition("absent", "regular") is True
    assert sre._is_allowed_stable_ledger_transition("regular", "regular") is True
    assert sre._is_allowed_stable_ledger_transition("regular", "absent") is False
    assert sre._is_allowed_stable_ledger_transition("absent", "symlink") is False
    assert sre._is_allowed_stable_ledger_transition("absent", "dir") is False
    assert sre._is_allowed_stable_ledger_transition("absent", "fifo") is False
    assert sre._is_allowed_stable_ledger_transition("absent", "socket") is False
    assert sre._is_allowed_stable_ledger_transition("absent", "device") is False
    assert sre._is_allowed_stable_ledger_transition("regular", "symlink") is False
    assert sre._is_allowed_stable_ledger_transition("regular", "dir") is False

    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger_path = ledger_dir / "subagent-launch-ledger.json"
    valid_ledger = {
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "coverage_scope": {
            "subagent_start_event_recorded": True,
            "supported_pretooluse_paths": ["Bash", "apply_patch", "Edit", "Write"],
            "unsupported_paths_fail_closed": True,
            "scope_note": "supported PreToolUse paths only",
        },
        "launches": [],
        "root_thread_actions": [],
    }
    ledger_path.write_text(json.dumps(valid_ledger), encoding="utf-8")

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds, ledger_before_content, ledger_ancestor_before_kinds = _ledger_before_state(repo)
    assert ledger_before_kinds[_LEDGER_REL] == "regular"

    # regular -> regular (a valid append of a new launch entry) is authorized.
    valid_ledger_appended = json.loads(json.dumps(valid_ledger))
    valid_ledger_appended["launches"].append({"agent_name": "spark-skim"})
    ledger_path.write_text(json.dumps(valid_ledger_appended), encoding="utf-8")
    assert (
        sre._find_unauthorized_repo_changes(
            str(repo),
            "9999",
            before_snapshot,
            before_status,
            ledger_before_kinds,
            ledger_before_content,
            ledger_ancestor_before_kinds,
        )
        is None
    )

    # regular -> delete is rejected.
    before_snapshot_2 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_2 = sre._git_status_paths(str(repo))
    ledger_before_kinds_2, ledger_before_content_2, ledger_ancestor_before_kinds_2 = _ledger_before_state(repo)
    ledger_path.unlink()
    assert (
        sre._find_unauthorized_repo_changes(
            str(repo),
            "9999",
            before_snapshot_2,
            before_status_2,
            ledger_before_kinds_2,
            ledger_before_content_2,
            ledger_ancestor_before_kinds_2,
        )
        == _LEDGER_REL
    )

    # regular -> symlink is rejected.
    outside = repo.parent / "outside-target.json"
    outside.write_text("{}", encoding="utf-8")
    before_snapshot_3 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_3 = sre._git_status_paths(str(repo))
    ledger_before_kinds_3 = sre._ledger_exact_kinds(str(repo))
    assert ledger_before_kinds_3[_LEDGER_REL] == "absent"
    ledger_path.symlink_to(outside)
    assert (
        sre._find_unauthorized_repo_changes(
            str(repo), "9999", before_snapshot_3, before_status_3, ledger_before_kinds_3
        )
        == _LEDGER_REL
    )

    # absent -> directory is rejected.
    ledger_path.unlink()
    before_snapshot_4 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_4 = sre._git_status_paths(str(repo))
    ledger_before_kinds_4 = sre._ledger_exact_kinds(str(repo))
    ledger_path.mkdir()
    assert (
        sre._find_unauthorized_repo_changes(
            str(repo), "9999", before_snapshot_4, before_status_4, ledger_before_kinds_4
        )
        == _LEDGER_REL
    )

    # absent -> FIFO is rejected.
    ledger_path.rmdir()
    before_snapshot_5 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_5 = sre._git_status_paths(str(repo))
    ledger_before_kinds_5 = sre._ledger_exact_kinds(str(repo))
    os.mkfifo(ledger_path)
    assert (
        sre._find_unauthorized_repo_changes(
            str(repo), "9999", before_snapshot_5, before_status_5, ledger_before_kinds_5
        )
        == _LEDGER_REL
    )


def test_ledger_transient_residue_times_out_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """GIVEN the writer's transient .lock/.tmp protocol entries
    WHEN one vanishes within the bounded quiescence window and the other
    outlives it
    THEN the quiescent one is not reported as residue, while the surviving
    one is reported as stale residue after the window elapses."""
    monkeypatch.setattr(sre, "_LEDGER_TRANSIENT_QUIESCENCE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sre, "_LEDGER_TRANSIENT_QUIESCENCE_POLL_INTERVAL_SECONDS", 0.02)

    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    lock_path = ledger_dir / "subagent-launch-ledger.json.lock"
    tmp_path_entry = ledger_dir / "subagent-launch-ledger.json.tmp"
    lock_path.write_text("lock", encoding="utf-8")
    tmp_path_entry.write_text("tmp", encoding="utf-8")

    def _remove_lock_after_delay() -> None:
        time.sleep(0.05)
        lock_path.unlink()

    thread = threading.Thread(target=_remove_lock_after_delay)
    thread.start()
    try:
        stale = sre._wait_for_ledger_transient_quiescence(str(repo))
    finally:
        thread.join(timeout=5)

    assert stale == [_LEDGER_TMP_REL]
    assert not lock_path.exists()
    assert tmp_path_entry.exists()


def test_ledger_siblings_remain_fail_closed_for_observable_changes(tmp_path: Path) -> None:
    """GIVEN a sibling file under artifacts/codex/ that is not one of the
    three typed exact ledger paths
    WHEN it is newly created alongside the stable ledger file
    THEN the typed ledger policy does not exempt it -- the ordinary
    repo-wide diff still fails closed on it (no directory-wide exclusion of
    artifacts/codex/)."""
    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "subagent-launch-ledger.json").write_text(
        '{"ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1"}', encoding="utf-8"
    )

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds, ledger_before_bytes, ledger_ancestor_before_kinds = _ledger_before_state(repo)

    sibling = ledger_dir / "unexpected-sibling.json"
    sibling.write_text("{}", encoding="utf-8")

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        ledger_before_bytes,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized == "artifacts/codex/unexpected-sibling.json"


def _valid_ledger_doc(launches: list | None = None, root_thread_actions: list | None = None) -> dict:
    return {
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "coverage_scope": {
            "subagent_start_event_recorded": True,
            "supported_pretooluse_paths": ["Bash", "apply_patch", "Edit", "Write"],
            "unsupported_paths_fail_closed": True,
            "scope_note": "supported PreToolUse paths only",
        },
        "launches": launches if launches is not None else [],
        "root_thread_actions": root_thread_actions if root_thread_actions is not None else [],
    }


def test_nonregular_to_regular_transition_fails_closed(tmp_path: Path) -> None:
    """GIVEN the stable ledger path's before-kind was symlink, dir, fifo,
    socket, or device (never absent or regular)
    WHEN the after-kind is regular
    THEN `_is_allowed_stable_ledger_transition` rejects the transition
    (Issue #1502 REQUEST_CHANGES Blocker 2: the previous implementation
    returned True for any `after_kind == "regular"` regardless of
    `before_kind`)."""
    for before_kind in ("symlink", "dir", "fifo", "socket", "device"):
        assert sre._is_allowed_stable_ledger_transition(before_kind, "regular") is False

    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger_path = ledger_dir / "subagent-launch-ledger.json"
    outside = repo.parent / "outside-target-2.json"
    outside.write_text("{}", encoding="utf-8")
    ledger_path.symlink_to(outside)

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds = sre._ledger_exact_kinds(str(repo))
    ledger_ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))
    assert ledger_before_kinds[_LEDGER_REL] == "symlink"

    ledger_path.unlink()
    ledger_path.write_text(json.dumps(_valid_ledger_doc()), encoding="utf-8")

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        None,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized == _LEDGER_REL


def test_regular_to_malformed_regular_fails_closed(tmp_path: Path) -> None:
    """GIVEN a valid `SUBAGENT_LAUNCH_LEDGER_V1` ledger
    WHEN it is replaced by malformed content (not valid JSON, or valid JSON
    that is not a valid ledger document)
    THEN the typed policy fails closed on the exact ledger path even though
    the coarse `regular -> regular` type transition is authorized (Issue
    #1502 REQUEST_CHANGES Blocker 3)."""
    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger_path = ledger_dir / "subagent-launch-ledger.json"
    ledger_path.write_text(json.dumps(_valid_ledger_doc(launches=[{"agent_name": "a"}])), encoding="utf-8")

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds, ledger_before_bytes, ledger_ancestor_before_kinds = _ledger_before_state(repo)
    assert ledger_before_kinds[_LEDGER_REL] == "regular"

    ledger_path.write_text("not-json-at-all", encoding="utf-8")
    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo), "9999", before_snapshot, before_status, ledger_before_kinds, ledger_before_bytes,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized == _LEDGER_REL

    # Deleting an existing launch entry (instead of only appending) is also
    # rejected even though the replacement is itself a structurally valid
    # ledger document.
    before_snapshot_2 = sre._snapshot_repo_paths(str(repo), "9999")
    before_status_2 = sre._git_status_paths(str(repo))
    ledger_before_kinds_2, ledger_before_bytes_2, ledger_ancestor_before_kinds_2 = _ledger_before_state(repo)
    ledger_path.write_text(json.dumps(_valid_ledger_doc(launches=[])), encoding="utf-8")
    unauthorized_2 = sre._find_unauthorized_repo_changes(
        str(repo), "9999", before_snapshot_2, before_status_2, ledger_before_kinds_2, ledger_before_bytes_2,
        ledger_ancestor_before_kinds_2,
    )
    assert unauthorized_2 == _LEDGER_REL


def test_parent_symlink_to_real_directory_transition_fails_closed(tmp_path: Path) -> None:
    """GIVEN the `artifacts` ancestor directory-node was a symlink before the
    child command ran
    WHEN it is replaced by a real (non-symlink) directory during the child's
    run
    THEN `_is_allowed_ancestor_transition` rejects the transition, and
    `_safe_ledger_ancestor_dir_rels` does not include `artifacts` in the safe
    set even though the after-state is a real, non-symlink directory (Issue
    #1502 REQUEST_CHANGES Blocker 5: postcondition-only ancestor inspection
    would otherwise silently exempt this parent-substitution attack)."""
    assert sre._is_allowed_ancestor_transition("symlink", "dir") is False
    assert sre._is_allowed_ancestor_transition("regular", "dir") is False
    assert sre._is_allowed_ancestor_transition("fifo", "dir") is False
    assert sre._is_allowed_ancestor_transition("absent", "dir") is True
    assert sre._is_allowed_ancestor_transition("dir", "dir") is True

    repo = _make_repo(tmp_path)
    outside_target = repo.parent / "outside-artifacts-dir"
    outside_target.mkdir()
    artifacts_link = repo / "artifacts"
    artifacts_link.symlink_to(outside_target)

    ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))
    assert ancestor_before_kinds["artifacts"] == "symlink"

    # Simulate the child replacing the symlinked parent with a real
    # directory (parent substitution).
    artifacts_link.unlink()
    (repo / "artifacts" / "codex").mkdir(parents=True)

    safe_rels = sre._safe_ledger_ancestor_dir_rels(str(repo), ancestor_before_kinds)
    assert "artifacts" not in safe_rels
    assert "artifacts/codex" not in safe_rels


def test_parent_file_to_real_directory_transition_fails_closed(tmp_path: Path) -> None:
    """GIVEN the `artifacts` ancestor directory-node was a plain regular file
    before the child command ran
    WHEN it is replaced by a real directory during the child's run
    THEN `_safe_ledger_ancestor_dir_rels` does not include `artifacts` in the
    safe set -- parent substitution via a plain-file predecessor is also
    detected (Issue #1502 REQUEST_CHANGES Blocker 5)."""
    repo = _make_repo(tmp_path)
    artifacts_path = repo / "artifacts"
    artifacts_path.write_text("not a directory", encoding="utf-8")

    ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))
    assert ancestor_before_kinds["artifacts"] == "regular"

    artifacts_path.unlink()
    (repo / "artifacts" / "codex").mkdir(parents=True)

    safe_rels = sre._safe_ledger_ancestor_dir_rels(str(repo), ancestor_before_kinds)
    assert "artifacts" not in safe_rels
    assert "artifacts/codex" not in safe_rels


def test_ledger_create_plus_existing_sibling_update_fails_closed(tmp_path: Path) -> None:
    """GIVEN an existing sibling file under `artifacts/codex/`
    WHEN the stable ledger transitions `absent -> regular` (a legitimate
    first-ever write) in the *same* invocation as the sibling's own content
    being modified
    THEN the sibling's update is still detected (Issue #1502
    REQUEST_CHANGES Blocker 4: the previous symmetric-difference-first
    computation would skip the metadata-changed-for-existing-paths pass
    whenever the create/delete set was non-empty, silently dropping the
    sibling update)."""
    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    sibling = ledger_dir / "existing-sibling.json"
    sibling.write_text("original", encoding="utf-8")

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds = sre._ledger_exact_kinds(str(repo))
    ledger_ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))
    assert ledger_before_kinds[_LEDGER_REL] == "absent"

    # Mixed delta: a brand-new ledger create (authorized) *and* an existing
    # sibling's content mutation (not authorized) in the same snapshot pair.
    time.sleep(0.01)
    (ledger_dir / "subagent-launch-ledger.json").write_text(
        json.dumps(_valid_ledger_doc()), encoding="utf-8"
    )
    sibling.write_text("mutated-without-authorization", encoding="utf-8")

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        None,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized == "artifacts/codex/existing-sibling.json"


def test_sibling_create_delete_residue_fails_closed(tmp_path: Path) -> None:
    """GIVEN a legitimate `absent -> regular` ledger creation
    WHEN an unrelated sibling file is simultaneously created *and* another
    pre-existing sibling is deleted in the same invocation
    THEN both the create-residue and the delete are detected via the
    symmetric-difference component of the unioned delta set (Issue #1502
    REQUEST_CHANGES Blocker 4)."""
    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    doomed_sibling = ledger_dir / "doomed-sibling.json"
    doomed_sibling.write_text("will-be-deleted", encoding="utf-8")

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds = sre._ledger_exact_kinds(str(repo))
    ledger_ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))

    (ledger_dir / "subagent-launch-ledger.json").write_text(
        json.dumps(_valid_ledger_doc()), encoding="utf-8"
    )
    doomed_sibling.unlink()
    (ledger_dir / "new-sibling.json").write_text("new", encoding="utf-8")

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        None,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized is not None
    assert unauthorized in {
        "artifacts/codex/doomed-sibling.json",
        "artifacts/codex/new-sibling.json",
    }


def test_transient_appearing_after_first_empty_poll_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """GIVEN the writer's transient `.lock` / `.tmp` entries are absent on
    the very first poll
    WHEN a still-finishing peer writer creates one of them shortly after that
    first empty observation (before the confirmation re-poll)
    THEN the confirmation re-poll observes it and the quiescence wait keeps
    polling (and ultimately fails closed if it never quiesces within the
    bounded window) instead of returning success on the strength of the
    single early empty poll alone (Issue #1502 REQUEST_CHANGES Blocker 6:
    TOCTOU between the first poll and the caller's subsequent snapshot
    capture)."""
    monkeypatch.setattr(sre, "_LEDGER_TRANSIENT_QUIESCENCE_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr(sre, "_LEDGER_TRANSIENT_QUIESCENCE_POLL_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(sre, "_LEDGER_TRANSIENT_QUIESCENCE_CONFIRM_INTERVAL_SECONDS", 0.05)

    repo = _make_repo(tmp_path)
    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    tmp_path_entry = ledger_dir / "subagent-launch-ledger.json.tmp"

    def _create_late_tmp_after_delay() -> None:
        # Fires after the very first (empty) poll but before the confirm
        # re-poll would otherwise trust it, and is never removed -- so the
        # wait must fail closed once the deadline elapses.
        time.sleep(0.01)
        tmp_path_entry.write_text("late-arriving-residue", encoding="utf-8")

    thread = threading.Thread(target=_create_late_tmp_after_delay)
    thread.start()
    try:
        stale = sre._wait_for_ledger_transient_quiescence(str(repo))
    finally:
        thread.join(timeout=5)

    assert stale == [_LEDGER_TMP_REL]
    assert tmp_path_entry.exists()


def test_real_repo_artifacts_ignore_folding(tmp_path: Path) -> None:
    """GIVEN the fixture repo's `.gitignore` mirrors the real repo's
    `artifacts/` ignore rule
    WHEN the stable ledger's ancestor directories (`artifacts`,
    `artifacts/codex`) are created for the first time by a peer write
    THEN Git's ignored-directory folding (`!! artifacts/`) is still expanded
    and excluded correctly, matching the real repo's `.gitignore` shape
    (Issue #1502 REQUEST_CHANGES Blocker 5)."""
    repo = _make_repo(tmp_path)
    gitignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert "artifacts/" in gitignore_text.splitlines()

    before_snapshot = sre._snapshot_repo_paths(str(repo), "9999")
    before_status = sre._git_status_paths(str(repo))
    ledger_before_kinds = sre._ledger_exact_kinds(str(repo))
    ledger_ancestor_before_kinds = sre._ledger_ancestor_kinds(str(repo))
    assert ledger_before_kinds[_LEDGER_REL] == "absent"

    ledger_dir = repo / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "subagent-launch-ledger.json").write_text(
        json.dumps(_valid_ledger_doc()), encoding="utf-8"
    )

    unauthorized = sre._find_unauthorized_repo_changes(
        str(repo),
        "9999",
        before_snapshot,
        before_status,
        ledger_before_kinds,
        None,
        ledger_ancestor_before_kinds,
    )
    assert unauthorized is None


def test_parallel_agent_runtime_safety_documents_typed_ledger_policy() -> None:
    """GIVEN docs/dev/agent-skill-boundaries.md
    WHEN the Parallel Agent Runtime Safety section is read
    THEN it documents the four typed categories (directory roots, stable
    exact peer file, transient protocol entries, build/runtime executable
    artifact), the owner/canonicality of each, the allowed stable-ledger
    state transitions, the stdlib snapshot mode guarantee limit, and the
    #1363 strict-attribution handoff."""
    text = (REPO_ROOT / "docs" / "dev" / "agent-skill-boundaries.md").read_text(encoding="utf-8")
    assert "型付き ledger 遷移ポリシー" in text
    assert "stable exact peer file" in text
    assert "transient protocol entries" in text
    assert "build/runtime executable artifact" in text
    assert "_LEDGER_STABLE_EXACT_REL" in text
    assert "_LEDGER_TRANSIENT_EXACT_RELS" in text
    assert "absent -> regular" in text
    assert "regular -> regular" in text
    assert "ledger_transient_residue_timeout" in text
    assert "byte-preserving update" in text
    assert "#1363" in text
    assert "subagent-launch-ledger-writer.c" in text
    assert "hostile process" in text
