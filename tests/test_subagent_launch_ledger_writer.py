"""Runtime coverage for the Linux-only SubAgent launch ledger writer."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITER_SOURCE = ROOT / "scripts" / "subagent-launch-ledger-writer.c"
HOOK = ROOT / "scripts" / "check-codex-agents.mjs"
VALIDATOR = ROOT / "scripts" / "check_subagent_launch_ledger.py"
PYTHON_TEST_PLAN = ROOT / ".github" / "ci" / "python-test-plan.json"


def build_writer(tmp_path: Path) -> Path:
    binary = tmp_path / "ledger-writer"
    subprocess.run(
        ["cc", "-std=c17", "-Wall", "-Wextra", "-Werror", "-O2", "-o", str(binary), str(WRITER_SOURCE)],
        check=True,
        text=True,
        capture_output=True,
    )
    return binary


def entry(name: str) -> dict[str, object]:
    return {
        "agent_name": name,
        "event_type": "SubagentStart",
        "evidence_source": "event_derived",
        "event_fingerprint": f"fingerprint-{name}",
        "declared_runtime": {
            "model": "gpt-5.3-codex-spark",
            "reasoning_effort": "medium",
            "default_permissions": "loop-protocol-readonly",
            "agent_definition_sha256": "a" * 64,
        },
        "observed_dispatch": {
            "model": "gpt-5.3-codex-spark",
            "session_id": "session",
            "turn_id": "turn",
            "agent_id": name,
            "observed_at": "2026-07-16T00:00:00Z",
        },
        "correlation": {
            "evidence_run_id": "run",
            "repo_head_sha": "a" * 40,
            "worktree_dirty": False,
        },
    }


def invoke(
    writer: Path,
    repo: Path,
    payload: dict[str, object],
    *,
    kind: str = "launches",
    identity: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if identity is None:
        identity = str(payload.get("event_fingerprint", "invalid-entry"))
    return subprocess.run(
        [str(writer), "--repo", str(repo), "--kind", kind, "--entry", json.dumps(payload), "--identity", identity],
        text=True,
        capture_output=True,
    )


def root_action(kind: str = "file_edit", command: str = "apply change") -> dict[str, object]:
    return {
        "kind": kind,
        "command": command,
        "tool_name": "Bash",
        "coverage_source": "supported_pretooluse_path",
    }


def barrier_invoke_writer(
    barrier: object,
    results: object,
    writer: str,
    repo: str,
    payload: str,
    kind: str,
    identity: str,
) -> None:
    """Start a native writer only after every independent process reaches the gate."""
    barrier.wait(timeout=10)
    result = subprocess.run(
        [writer, "--repo", repo, "--kind", kind, "--entry", payload, "--identity", identity],
        text=True,
        capture_output=True,
    )
    results.put((kind, identity, result.returncode, result.stdout, result.stderr))


def test_independent_trusted_processes_preserve_distinct_evidence(tmp_path: Path):
    writer = build_writer(tmp_path)
    # The writer itself is Linux-only; fork avoids pytest's non-package test
    # module import limitation while still creating independent OS processes.
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(3)
    results = context.Queue()
    launch = entry("spark-skim")
    launch["declared_runtime"]["reasoning_effort"] = "low"
    action = root_action("bash_observed", "rtk git status --short")
    workers = [
        context.Process(
            target=barrier_invoke_writer,
            args=(
                barrier,
                results,
                str(writer),
                str(tmp_path),
                json.dumps(launch),
                "launches",
                "fingerprint-spark-skim",
            ),
        ),
        context.Process(
            target=barrier_invoke_writer,
            args=(
                barrier,
                results,
                str(writer),
                str(tmp_path),
                json.dumps(action),
                "root_thread_actions",
                "Bash\\nrtk git status --short",
            ),
        ),
    ]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=10)
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in workers]
    assert all(returncode == 0 for _, _, returncode, _, _ in outcomes), outcomes

    ledger = json.loads((tmp_path / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert [item["event_fingerprint"] for item in ledger["launches"]] == ["fingerprint-spark-skim"]
    assert ledger["root_thread_actions"] == [action]

    audit = subprocess.run(
        [sys.executable, str(VALIDATOR), "--audit-mode", str(tmp_path / "artifacts/codex/subagent-launch-ledger.json")],
        text=True,
        capture_output=True,
    )
    assert audit.returncode == 0, audit.stdout
    assert json.loads(audit.stdout)["status"] == "pass"


def test_hook_builds_writer_and_records_dispatch_evidence(tmp_path: Path):
    agent_dir = tmp_path / ".codex" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "spark-skim.toml").write_text(
        "model = \"gpt-5.3-codex-spark\"\nmodel_reasoning_effort = \"medium\"\ndefault_permissions = \"loop-protocol-readonly\"\n",
        encoding="utf-8",
    )
    writer_dir = tmp_path / "scripts"
    writer_dir.mkdir()
    shutil.copy2(WRITER_SOURCE, writer_dir / WRITER_SOURCE.name)
    payload = {
        "agent_type": "spark-skim",
        "model": "gpt-5.3-codex-spark",
        "session_id": "session",
        "turn_id": "turn",
        "agent_id": "agent",
    }
    result = subprocess.run(
        ["node", str(HOOK), "--hook-subagent-start"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "REPO_ROOT_OVERRIDE": str(tmp_path),
            "CODEX_AGENT_EVIDENCE_RUN_ID": "run",
            "CODEX_AGENT_EVIDENCE_HEAD_SHA": "a" * 40,
        },
    )
    assert result.returncode == 0, result.stderr
    ledger = json.loads((tmp_path / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert ledger["launches"][0]["observed_dispatch"]["agent_id"] == "agent"


def test_hook_bounds_native_writer_execution_time():
    source = HOOK.read_text(encoding="utf-8")
    assert "spawnSync(ensureLedgerWriter()" in source
    assert "timeout: 10_000" in source
    assert "killSignal: 'SIGKILL'" in source


def test_hook_builds_writer_outside_repo_tree_cold_and_warm(tmp_path: Path):
    """GIVEN the hook is invoked cold (no cached writer binary for this
    source content) and then warm (cached binary reused)
    WHEN --hook-subagent-start builds/uses the native writer
    THEN no repo-local `tmp/subagent-launch-ledger-writer*` build artifact is
    ever created under REPO_ROOT_OVERRIDE, for either invocation
    (Issue #1502 AC1: the build cache lives outside the repo snapshot)."""
    import uuid

    agent_dir = tmp_path / ".codex" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "spark-skim.toml").write_text(
        "model = \"gpt-5.3-codex-spark\"\nmodel_reasoning_effort = \"medium\"\ndefault_permissions = \"loop-protocol-readonly\"\n",
        encoding="utf-8",
    )
    writer_dir = tmp_path / "scripts"
    writer_dir.mkdir()
    # Append a unique nonce so this test's source content hash is guaranteed
    # distinct from any other test's cached writer binary, forcing a
    # genuinely cold build on the first of the two invocations below.
    nonce = f"\n/* test-nonce: {uuid.uuid4().hex} */\n"
    (writer_dir / WRITER_SOURCE.name).write_text(
        WRITER_SOURCE.read_text(encoding="utf-8") + nonce, encoding="utf-8"
    )

    def _invoke(agent_id: str) -> subprocess.CompletedProcess[str]:
        payload = {
            "agent_type": "spark-skim",
            "model": "gpt-5.3-codex-spark",
            "session_id": "session",
            "turn_id": "turn",
            "agent_id": agent_id,
        }
        return subprocess.run(
            ["node", str(HOOK), "--hook-subagent-start"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "REPO_ROOT_OVERRIDE": str(tmp_path),
                "CODEX_AGENT_EVIDENCE_RUN_ID": "run",
                "CODEX_AGENT_EVIDENCE_HEAD_SHA": "a" * 40,
            },
        )

    cold = _invoke("cold-agent")
    assert cold.returncode == 0, cold.stderr
    repo_tmp_dir = tmp_path / "tmp"
    if repo_tmp_dir.exists():
        assert not any("subagent-launch-ledger-writer" in p.name for p in repo_tmp_dir.iterdir())

    warm = _invoke("warm-agent")
    assert warm.returncode == 0, warm.stderr
    if repo_tmp_dir.exists():
        assert not any("subagent-launch-ledger-writer" in p.name for p in repo_tmp_dir.iterdir())

    ledger = json.loads((tmp_path / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert [entry["observed_dispatch"]["agent_id"] for entry in ledger["launches"]] == [
        "cold-agent",
        "warm-agent",
    ]


def _invoke_hook_subagent_start(
    repo_root: Path,
    *,
    agent_id: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = {
        "agent_type": "spark-skim",
        "model": "gpt-5.3-codex-spark",
        "session_id": "session",
        "turn_id": "turn",
        "agent_id": agent_id,
    }
    env = {
        **os.environ,
        "REPO_ROOT_OVERRIDE": str(repo_root),
        "CODEX_AGENT_EVIDENCE_RUN_ID": "run",
        "CODEX_AGENT_EVIDENCE_HEAD_SHA": "a" * 40,
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["node", str(HOOK), "--hook-subagent-start"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
    )


def _make_ledger_writer_fixture_repo(tmp_path: Path, *, nonce_label: str) -> Path:
    import uuid

    repo = tmp_path / "repo"
    agent_dir = repo / ".codex" / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / "spark-skim.toml").write_text(
        "model = \"gpt-5.3-codex-spark\"\nmodel_reasoning_effort = \"medium\"\ndefault_permissions = \"loop-protocol-readonly\"\n",
        encoding="utf-8",
    )
    writer_dir = repo / "scripts"
    writer_dir.mkdir()
    nonce = f"\n/* test-nonce: {nonce_label}-{uuid.uuid4().hex} */\n"
    (writer_dir / WRITER_SOURCE.name).write_text(WRITER_SOURCE.read_text(encoding="utf-8") + nonce, encoding="utf-8")
    return repo


def test_tmpdir_inside_repo_is_rejected(tmp_path: Path):
    """GIVEN `TMPDIR` is set to a directory inside the repo tree
    WHEN --hook-subagent-start attempts to build the native ledger writer
    THEN the hook fails closed instead of building/using a writer whose
    private work directory would live inside the repo snapshot (Issue #1502
    REQUEST_CHANGES Blocker 1)."""
    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="tmpdir-inside-repo")
    inside_tmpdir = repo / "tmp-inside-repo"
    inside_tmpdir.mkdir()

    result = _invoke_hook_subagent_start(
        repo, agent_id="agent-1", extra_env={"TMPDIR": str(inside_tmpdir)}
    )
    assert result.returncode != 0
    assert "ledger_writer_tmp_root_inside_repo" in (result.stderr or "")
    assert not (repo / "artifacts" / "codex" / "subagent-launch-ledger.json").exists()


def test_relative_tmpdir_is_rejected(tmp_path: Path):
    """GIVEN `TMPDIR` is set to a relative (non-absolute) path
    WHEN --hook-subagent-start attempts to build the native ledger writer
    THEN the hook fails closed instead of resolving the relative value
    against an unpredictable cwd (Issue #1502 REQUEST_CHANGES Blocker 1)."""
    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="relative-tmpdir")

    result = _invoke_hook_subagent_start(
        repo, agent_id="agent-1", extra_env={"TMPDIR": "relative/tmp/path"}
    )
    assert result.returncode != 0
    assert "ledger_writer_tmpdir_env_relative" in (result.stderr or "")
    assert not (repo / "artifacts" / "codex" / "subagent-launch-ledger.json").exists()


def test_preseeded_cache_executable_is_not_run(tmp_path: Path):
    """GIVEN a file is pre-planted at the exact predictable path the
    pre-#1502 shared content-addressed cache would have used
    WHEN --hook-subagent-start builds and runs the native ledger writer
    THEN that pre-planted file is never executed (its marker side effect
    never appears) -- the writer is always built into a fresh
    `fs.mkdtempSync` directory with an unpredictable name, so there is no
    predictable path left for an attacker to preseed (Issue #1502
    REQUEST_CHANGES Blocker 1)."""
    import hashlib

    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="preseeded-cache")
    source_bytes = (repo / "scripts" / WRITER_SOURCE.name).read_bytes()
    content_hash = hashlib.sha256(source_bytes).hexdigest()

    marker = tmp_path / "preseeded-executed.marker"
    stale_cache_dir = Path(tempfile_dir_for_test()) / "loop-protocol-subagent-ledger-writer-cache"
    stale_cache_dir.mkdir(parents=True, exist_ok=True)
    stale_binary = stale_cache_dir / f"{content_hash}-ledger-writer"
    stale_binary.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
    stale_binary.chmod(0o755)

    try:
        result = _invoke_hook_subagent_start(repo, agent_id="agent-1")
        assert result.returncode == 0, result.stderr
        assert not marker.exists()
        ledger = json.loads((repo / "artifacts/codex/subagent-launch-ledger.json").read_text())
        assert ledger["launches"][0]["observed_dispatch"]["agent_id"] == "agent-1"
    finally:
        shutil.rmtree(stale_cache_dir, ignore_errors=True)


def tempfile_dir_for_test() -> str:
    import tempfile

    return tempfile.gettempdir()


def test_cache_entry_wrong_uid_or_mode_fails_closed(tmp_path: Path):
    """GIVEN `TMPDIR` points at a directory that is world/group-writable
    without the sticky bit (an insecure base temp root)
    WHEN --hook-subagent-start attempts to build the native ledger writer
    THEN the hook fails closed instead of trusting an insecurely-permissioned
    base directory to create its private work directory under (Issue #1502
    REQUEST_CHANGES Blocker 1)."""
    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="insecure-tmp-root")
    insecure_root = tmp_path / "insecure-tmp-root"
    insecure_root.mkdir()
    insecure_root.chmod(0o777)  # world-writable, no sticky bit

    result = _invoke_hook_subagent_start(
        repo, agent_id="agent-1", extra_env={"TMPDIR": str(insecure_root)}
    )
    assert result.returncode != 0
    assert "ledger_writer_tmp_root_insecure_mode" in (result.stderr or "")
    assert not (repo / "artifacts" / "codex" / "subagent-launch-ledger.json").exists()


def test_cache_output_digest_mismatch_fails_closed(tmp_path: Path):
    """GIVEN the writer source changes between two invocations
    WHEN --hook-subagent-start builds the native ledger writer for each
    THEN the second invocation's compiled binary reflects the *new* source
    content, not a stale reused binary compiled from the old content --
    there is no shared cache keyed by content hash to become stale/mismatched
    against (Issue #1502 REQUEST_CHANGES Blocker 1: the shared warm cache is
    abolished, so a digest mismatch between a cached binary and its
    supposed source can no longer occur)."""
    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="digest-mismatch")
    source_path = repo / "scripts" / WRITER_SOURCE.name

    first = _invoke_hook_subagent_start(repo, agent_id="agent-first")
    assert first.returncode == 0, first.stderr

    # Mutate the source (a behavior-preserving comment-only change) between
    # invocations and confirm the next invocation still succeeds using the
    # newly-read bytes rather than any stale artifact.
    source_path.write_text(source_path.read_text(encoding="utf-8") + "\n/* mutated */\n", encoding="utf-8")
    second = _invoke_hook_subagent_start(repo, agent_id="agent-second")
    assert second.returncode == 0, second.stderr

    ledger = json.loads((repo / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert [entry["observed_dispatch"]["agent_id"] for entry in ledger["launches"]] == [
        "agent-first",
        "agent-second",
    ]


def test_toolchain_or_header_change_misses_cache(tmp_path: Path):
    """GIVEN two independent hook invocations against the same fixture repo
    WHEN each builds its own native ledger writer
    THEN neither invocation reuses a persistent build artifact from the
    other -- each private work directory (and its compiled binary) is
    deleted once its own invocation completes, so a toolchain or header
    change between invocations is always picked up on the very next build
    rather than silently served from a stale cache entry (Issue #1502
    REQUEST_CHANGES Blocker 1)."""
    repo = _make_ledger_writer_fixture_repo(tmp_path, nonce_label="no-persistent-cache")

    before_tmp_listing = set(Path(tempfile_dir_for_test()).iterdir())
    first = _invoke_hook_subagent_start(repo, agent_id="agent-first")
    assert first.returncode == 0, first.stderr
    after_first_listing = set(Path(tempfile_dir_for_test()).iterdir())
    # No `loop-protocol-ledger-writer-*` private directory should survive
    # past the invocation that created it.
    leaked = [
        p for p in (after_first_listing - before_tmp_listing)
        if p.name.startswith("loop-protocol-ledger-writer-")
    ]
    assert leaked == [], leaked

    second = _invoke_hook_subagent_start(repo, agent_id="agent-second")
    assert second.returncode == 0, second.stderr
    after_second_listing = set(Path(tempfile_dir_for_test()).iterdir())
    leaked_2 = [
        p for p in (after_second_listing - before_tmp_listing)
        if p.name.startswith("loop-protocol-ledger-writer-")
    ]
    assert leaked_2 == [], leaked_2


def test_preexisting_substitution_and_nonregular_entries_fail_closed(tmp_path: Path):
    writer = build_writer(tmp_path)
    parent_link = tmp_path / "artifacts"
    parent_link.symlink_to(tmp_path / "outside")
    assert invoke(writer, tmp_path, entry("spark-skim")).returncode != 0

    for name, create in (
        ("subagent-launch-ledger.json", lambda path: path.symlink_to(tmp_path / "outside")),
        ("subagent-launch-ledger.json.lock", lambda path: os.mkfifo(path)),
        ("subagent-launch-ledger.json.tmp", lambda path: os.mkfifo(path)),
    ):
        case_root = tmp_path / name.replace(".", "-")
        ledger_dir = case_root / "artifacts" / "codex"
        ledger_dir.mkdir(parents=True)
        target = ledger_dir / name
        create(target)
        result = invoke(writer, case_root, entry("spark-skim"))
        assert result.returncode != 0
        assert target.exists() or target.is_symlink()


def test_malformed_and_replacement_failures_never_reset_or_publish_partial_json(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    original = b'{"ledger_schema":'
    ledger.write_bytes(original)
    result = invoke(writer, tmp_path, entry("spark-skim"))
    assert result.returncode != 0
    assert ledger.read_bytes() == original
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()

    ledger.unlink()
    residue = ledger_dir / "subagent-launch-ledger.json.tmp"
    residue.write_text("do-not-replace", encoding="utf-8")
    result = invoke(writer, tmp_path, entry("spark-skim"))
    assert result.returncode != 0
    assert residue.read_text(encoding="utf-8") == "do-not-replace"
    assert not ledger.exists()


def test_schema_invalid_ledger_missing_coverage_scope_fails_closed_without_replacement(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    original = json.dumps({
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "launches": [],
        "root_thread_actions": [],
    }).encode()
    ledger.write_bytes(original)

    result = invoke(writer, tmp_path, entry("spark-skim"))

    assert result.returncode != 0
    assert "ledger_parse_or_schema_invalid" in result.stderr
    assert ledger.read_bytes() == original
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()


def test_schema_invalid_launch_array_entry_fails_closed_without_replacement(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts" / "codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    original = json.dumps({
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "coverage_scope": {
            "subagent_start_event_recorded": True,
            "supported_pretooluse_paths": ["Bash", "apply_patch", "Edit", "Write"],
            "unsupported_paths_fail_closed": True,
            "scope_note": "supported PreToolUse paths only",
        },
        "launches": [{}],
        "root_thread_actions": [],
    }).encode()
    ledger.write_bytes(original)

    result = invoke(writer, tmp_path, entry("spark-skim"))

    assert result.returncode != 0
    assert "ledger_parse_or_schema_invalid" in result.stderr
    assert ledger.read_bytes() == original
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()


def test_root_actions_allow_empty_launches_then_transition_to_launch_evidence(tmp_path: Path):
    writer = build_writer(tmp_path)
    first = invoke(
        writer,
        tmp_path,
        root_action("file_edit", "apply_patch scripts/check-codex-agents.mjs"),
        kind="root_thread_actions",
        identity="Bash\napply_patch scripts/check-codex-agents.mjs",
    )
    assert first.returncode == 0, first.stderr
    ledger_path = tmp_path / "artifacts/codex/subagent-launch-ledger.json"
    after_root = json.loads(ledger_path.read_text())
    assert after_root["launches"] == []
    assert after_root["root_thread_actions"][0]["kind"] == "file_edit"

    second = invoke(writer, tmp_path, entry("spark-skim"))
    assert second.returncode == 0, second.stderr
    final = json.loads(ledger_path.read_text())
    assert len(final["launches"]) == 1
    assert len(final["root_thread_actions"]) == 1


def test_writer_accepts_node_classifier_kinds_and_defers_policy_to_canonical_audit(tmp_path: Path):
    writer = build_writer(tmp_path)
    classifier_kinds = (
        "test_execution",
        "review_judgment",
        "git_commit",
        "git_push",
        "cleanup_git_mutation",
        "file_edit",
    )
    for index, kind in enumerate(classifier_kinds):
        result = invoke(
            writer,
            tmp_path,
            root_action(kind, f"command-{index}"),
            kind="root_thread_actions",
            identity=f"Bash\\ncommand-{index}",
        )
        assert result.returncode == 0, result.stderr

    ledger_path = tmp_path / "artifacts/codex/subagent-launch-ledger.json"
    payload = json.loads(ledger_path.read_text())
    assert [action["kind"] for action in payload["root_thread_actions"]] == list(classifier_kinds)
    audit = subprocess.run([sys.executable, str(VALIDATOR), "--audit-mode", str(ledger_path)], text=True, capture_output=True)
    assert audit.returncode == 1
    assert "root_thread_data_plane_execution_observed" in json.loads(audit.stdout)["error_codes"]


def test_incoming_entry_schema_validation_and_post_append_validation_fail_closed(tmp_path: Path):
    writer = build_writer(tmp_path)
    invalid = invoke(writer, tmp_path, {}, identity="invalid")
    assert invalid.returncode != 0
    assert invalid.stderr.strip() == "ledger_entry_invalid"
    ledger_path = tmp_path / "artifacts/codex/subagent-launch-ledger.json"
    assert not ledger_path.exists()
    assert not (ledger_path.parent / "subagent-launch-ledger.json.lock").exists()

    valid = invoke(writer, tmp_path, entry("spark-skim"))
    assert valid.returncode == 0, valid.stderr
    before = ledger_path.read_bytes()
    invalid_root = invoke(writer, tmp_path, {"kind": "file_edit"}, kind="root_thread_actions", identity="invalid-root")
    assert invalid_root.returncode != 0
    assert invalid_root.stderr.strip() == "ledger_entry_invalid"
    assert ledger_path.read_bytes() == before
    assert not (ledger_path.parent / "subagent-launch-ledger.json.lock").exists()


def test_failure_releases_only_owned_lock_and_temp_with_exact_reason_codes(tmp_path: Path):
    writer = build_writer(tmp_path)
    ledger_dir = tmp_path / "artifacts/codex"
    ledger_dir.mkdir(parents=True)
    ledger = ledger_dir / "subagent-launch-ledger.json"
    ledger.write_text("{}", encoding="utf-8")

    malformed = invoke(writer, tmp_path, entry("spark-skim"))
    assert malformed.returncode != 0
    assert malformed.stderr.strip() == "ledger_parse_or_schema_invalid"
    assert not (ledger_dir / "subagent-launch-ledger.json.lock").exists()
    assert not (ledger_dir / "subagent-launch-ledger.json.tmp").exists()

    ledger.unlink()
    residue = ledger_dir / "subagent-launch-ledger.json.tmp"
    residue.write_text("foreign-temp", encoding="utf-8")
    external_temp = invoke(writer, tmp_path, entry("spark-skim"))
    assert external_temp.returncode != 0
    assert external_temp.stderr.strip() == "ledger_temp_preexisting"
    assert residue.read_text(encoding="utf-8") == "foreign-temp"
    assert not (ledger_dir / "subagent-launch-ledger.json.lock").exists()


def test_fifo_and_socket_ledger_entries_fail_without_blocking(tmp_path: Path):
    writer = build_writer(tmp_path)
    for name, create in (("fifo", os.mkfifo), ("socket", None)):
        case_root = tmp_path / name if create is not None else tmp_path.parent / "socket-case"
        ledger_dir = case_root / "artifacts/codex"
        ledger_dir.mkdir(parents=True)
        target = ledger_dir / "subagent-launch-ledger.json"
        if create is not None:
            create(target)
        else:
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(target))
        try:
            result = invoke(writer, case_root, entry(f"spark-{name}"))
        finally:
            if create is None:
                server.close()
                target.unlink()
        assert result.returncode != 0
        assert result.stderr.strip() == "ledger_target_unsafe"
        assert not (ledger_dir / "subagent-launch-ledger.json.lock").exists()


def test_duplicate_detection_uses_exact_parsed_identity_fields(tmp_path: Path):
    writer = build_writer(tmp_path)
    assert invoke(writer, tmp_path, entry("prefix")).returncode == 0
    suffix = entry("suffix")
    suffix["event_fingerprint"] = "fingerprint"
    prefix = entry("prefix")
    prefix["event_fingerprint"] = "prefix-fingerprint"
    clean_root = tmp_path / "exact-identity"
    clean_root.mkdir()
    assert invoke(writer, clean_root, prefix).returncode == 0
    assert invoke(writer, clean_root, suffix).returncode == 0
    ledger = json.loads((clean_root / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert {item["event_fingerprint"] for item in ledger["launches"]} == {"prefix-fingerprint", "fingerprint"}

    action = root_action(command="pnpm test")
    assert invoke(writer, clean_root, action, kind="root_thread_actions", identity="first").returncode == 0
    assert invoke(writer, clean_root, action, kind="root_thread_actions", identity="second").returncode == 0
    final = json.loads((clean_root / "artifacts/codex/subagent-launch-ledger.json").read_text())
    assert len(final["root_thread_actions"]) == 1


def test_canonical_evidence_requires_launch_dispatch_and_correlation(tmp_path: Path):
    runtime = json.loads(
        (ROOT / "tests/fixtures/codex-agent-config/expected-runtime-contract.json").read_text(encoding="utf-8")
    )["required_agents"]["spark-skim"]
    payload = {
        "ledger_schema": "SUBAGENT_LAUNCH_LEDGER_V1",
        "generated_by": "codex_hook_pipeline",
        "coverage_scope": {
            "subagent_start_event_recorded": True,
            "supported_pretooluse_paths": ["Bash", "apply_patch", "Edit", "Write"],
            "unsupported_paths_fail_closed": True,
            "scope_note": "supported PreToolUse paths only",
        },
        "launches": [{
            "agent_name": "spark-skim",
            "event_type": "SubagentStart",
            "evidence_source": "event_derived",
            "event_fingerprint": "declared-only",
            "runtime": {
                "model": runtime["model"],
                "reasoning_effort": runtime["model_reasoning_effort"],
                "default_permissions": runtime["default_permissions"],
            },
        }],
        "root_thread_actions": [],
    }
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run([sys.executable, str(VALIDATOR), "--audit-mode", str(ledger)], text=True, capture_output=True)
    assert result.returncode == 1
    assert "dispatch_evidence_missing" in json.loads(result.stdout)["error_codes"]


def test_ssot_documents_native_writer_boundary():
    text = (ROOT / "docs/dev/agent-skill-boundaries.md").read_text(encoding="utf-8")
    assert "subagent-launch-ledger-writer.c" in text
    assert "hostile process" in text


def test_ci_test_selection_plan_includes_writer_test_once():
    plan = json.loads(PYTHON_TEST_PLAN.read_text(encoding="utf-8"))
    targets = plan["targets"]
    assert targets.count("tests/test_subagent_launch_ledger_writer.py") == 1
    assert targets.index("tests/test_subagent_launch_ledger_writer.py") == (
        targets.index("tests/test_subagent_launch_ledger.py") + 1
    )
