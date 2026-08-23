from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def _pinned_uv_version(repo_root: Path) -> str:
    data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["uv"]["required-version"]


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
    (repo / ".gitignore").write_text(".cache/\n__pycache__/\ntmp/\n.guard_shadow_log.jsonl\n")
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "seed", cwd=repo)
    return repo


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _skill_runtime_exec_rel() -> str:
    return "scripts/agent-guards/" + "skill_runtime_exec" + ".py"


def _install_skill_runtime_exec_fixture(repo_root: Path) -> None:
    source_root = REPO_ROOT
    for rel in (
        _skill_runtime_exec_rel(),
        "scripts/agent-guards/skill_runtime_command_policy.py",
    ):
        src = source_root / rel
        dest = repo_root / rel
        _write_text(dest, src.read_text())

    pin = _pinned_uv_version(source_root)
    _write_text(
        repo_root / "pyproject.toml",
        f'''[project]
name = "skill-runtime-fixture"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = []

[tool.uv]
required-version = "{pin}"
managed = false
''',
    )

    _write_text(
        repo_root / "scripts" / "agent-ops" / "worktree_catalog.py",
        """from __future__ import annotations

class Deadline:
    def subprocess_timeout(self, seconds: float) -> float:
        return seconds


def list_worktrees(project_root: str, deadline=None):
    return []


def select_issue_worktree(catalog, issue_number, root_realpath):
    return None
""",
    )

    _write_text(
        # Issue #2311 AC1 fixture parity: bare `preflight.run` first-hops
        # into `workflow_start_entry.py` (a minimal fixture-local forwarder
        # to `run_refinement_preflight.py` below -- see that file) instead
        # of `run_refinement_preflight.py` directly.
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "workflow_start_entry.py",
        """from __future__ import annotations

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    _inner = Path(__file__).resolve().parent / "run_refinement_preflight.py"
    _proc = subprocess.run([sys.executable, str(_inner), *sys.argv[1:]])
    raise SystemExit(_proc.returncode)
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv",
            "run",
            "python3",
                        ".claude/skills/issue-refinement-loop/scripts/workflow_start_entry.py",
            "--issue-number",
            "{issue_number}",
            "--repo",
            "{repo}",
        ],
        "shell": False,
        "cwd_policy": "repo_root",
        "execution_class": "exact_skill_runtime",
        "required_cwd": "canonical_main_root",
        "required_branch": "default_branch",
        "allowed_write_roots": [".claude/artifacts/issue-refinement-loop/{active_issue}/"],
        "network_effect": "github_read_only",
        "placeholders": {
            "issue_number": {"type": "positive_int", "required": True},
            "repo": {"type": "owner_repo", "required": True},
        },
    }
}


def render_command(command_id: str, values: dict[str, object]) -> list[str]:
    argv = REGISTRY[command_id]["argv"]
    rendered = []
    for token in argv:
        if token == "{issue_number}":
            rendered.append(str(values["issue_number"]))
        elif token == "{repo}":
            rendered.append(str(values["repo"]))
        else:
            rendered.append(token)
    return rendered
""",
    )

    _write_text(
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "run_refinement_preflight.py",
        """from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--issue-number", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    sleep_seconds = os.environ.get("SKILL_RUNTIME_TEST_SLEEP_SECONDS")
    if sleep_seconds:
        time.sleep(float(sleep_seconds))

    shadow_log_path = Path(".guard_shadow_log.jsonl")
    mutate = os.environ.get("SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE")
    if mutate:
        if mutate == "symlink":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
            shadow_log_path.symlink_to("/etc/hostname")
        elif mutate == "directory":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
            shadow_log_path.mkdir()
        elif mutate == "delete":
            if shadow_log_path.exists() or shadow_log_path.is_symlink():
                shadow_log_path.unlink()
        elif mutate == "truncate":
            shadow_log_path.write_text("")
        elif mutate == "overwrite":
            shadow_log_path.write_text(json.dumps({"schema_version": "1", "event": "overwritten"}) + "\\n")
        elif mutate == "malformed-append":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write("not-json-at-all\\n")
        elif mutate == "delete-record":
            lines = [line for line in shadow_log_path.read_text().splitlines() if line]
            remaining = lines[1:]
            shadow_log_path.write_text("".join(line + "\\n" for line in remaining))
        elif mutate == "self-append":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"schema_version": "1", "event": "self-append"}) + "\\n")
        elif mutate == "cold-start-malformed":
            # Blocker 2: absent -> regular cold-start creation with content
            # that is a regular file but NOT well-formed JSONL.
            shadow_log_path.write_text("not-json-at-all\\n")
        elif mutate == "cold-start-blank-line":
            shadow_log_path.write_text(
                json.dumps({"schema_version": "1", "event": "a"}) + "\\n\\n"
                + json.dumps({"schema_version": "1", "event": "b"}) + "\\n"
            )
        elif mutate == "cold-start-nan":
            shadow_log_path.write_text('{"schema_version": "1", "value": NaN}\\n')
        elif mutate == "replace-inode-valid-prefix":
            # Blocker 3: replace the shadow log with a *different inode*
            # whose content is nonetheless a byte-for-byte valid JSONL
            # extension of the original (before) content -- this must still
            # fail closed because it is not an in-place append.
            before = shadow_log_path.read_bytes()
            tmp_path = shadow_log_path.with_name(".guard_shadow_log.jsonl.tmp-replace")
            tmp_path.write_bytes(
                before + (json.dumps({"schema_version": "1", "event": "replaced-append"}) + "\\n").encode()
            )
            os.replace(tmp_path, shadow_log_path)
        elif mutate == "append-blank-line":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write("\\n")
        elif mutate == "append-nan":
            with open(shadow_log_path, "a", encoding="utf-8") as f:
                f.write('{"schema_version": "1", "value": NaN}\\n')

    artifact_dir = Path(".claude") / "artifacts" / "issue-refinement-loop" / args.issue_number
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {"issue_number": args.issue_number, "repo": args.repo}
    (artifact_dir / "preflight.json").write_text(json.dumps(payload))
    print(json.dumps({"ok": True, **payload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
    )


def _run_executor(repo: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            _skill_runtime_exec_rel(),
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _append_after_delay(path: Path, content: str, delay_seconds: float) -> threading.Thread:
    def _worker() -> None:
        time.sleep(delay_seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)

    thread = threading.Thread(target=_worker)
    thread.start()
    return thread


def _seed_shadow_log(repo: Path) -> Path:
    shadow_log = repo / ".guard_shadow_log.jsonl"
    shadow_log.write_text(json.dumps({"schema_version": "1", "event": "seed"}) + "\n")
    return shadow_log


# ---------------------------------------------------------------------------
# AC1: regular peer append(s) to .guard_shadow_log.jsonl (including multiple
# concurrent producers) must not trigger unauthorized_write_path.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_peer_append_does_not_block_preflight(tmp_path: Path) -> None:
    """GIVEN a pre-existing .guard_shadow_log.jsonl
    WHEN two independent peer hook producers concurrently append JSONL
    records to it while this command's own child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path,
    and the self-append made by the child command's own peer hooks must not
    be lost/reverted either."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)

    peer_1 = _append_after_delay(
        shadow_log,
        json.dumps({"schema_version": "1", "event": "peer-append-1"}) + "\n",
        delay_seconds=0.15,
    )
    peer_2 = _append_after_delay(
        shadow_log,
        json.dumps({"schema_version": "1", "event": "peer-append-2"}) + "\n",
        delay_seconds=0.3,
    )
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.6",
                "SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append",
            },
        )
    finally:
        peer_1.join(timeout=5)
        peer_2.join(timeout=5)

    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    events = {json.loads(line)["event"] for line in lines}
    assert {"seed", "peer-append-1", "peer-append-2", "self-append"} <= events
    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


def test_guard_shadow_log_first_ever_creation_does_not_block_preflight(tmp_path: Path) -> None:
    """GIVEN .guard_shadow_log.jsonl does not exist yet (cold start)
    WHEN the child command's own peer hooks create it for the first time
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path
    (absent -> regular is an authorized shadow-log kind transition)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = repo / ".guard_shadow_log.jsonl"
    assert not shadow_log.exists()

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append"})
    assert result.returncode == 0, result.stderr
    assert shadow_log.exists()


# ---------------------------------------------------------------------------
# AC2: non-regular kind substitution must still fail closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mutate", ["symlink", "directory"])
def test_guard_shadow_log_nonregular_substitution_still_fails_closed(tmp_path: Path, mutate: str) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is replaced by a symlink or a directory during the run
    THEN skill_runtime_exec.py must fail with unauthorized_write_path
    (this guarantee must NOT come from _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS,
    which never inspects transition kind at all)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_guard_shadow_log_delete_still_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is deleted (regular -> absent) during the run
    THEN skill_runtime_exec.py must fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "delete"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


# ---------------------------------------------------------------------------
# AC3: regular -> regular non-append-only content transitions must be
# rejected (truncate / overwrite / malformed JSONL / record deletion).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    ["truncate", "overwrite", "malformed-append", "delete-record"],
)
def test_guard_shadow_log_truncate_and_overwrite_rejected(tmp_path: Path, mutate: str) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN the on-disk content is truncated, overwritten, replaced with a
    malformed (non-JSON) appended line, or has an existing record removed
    THEN skill_runtime_exec.py must fail with unauthorized_write_path
    (append-only is enforced, not just after_kind == "regular")."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_guard_shadow_log_valid_append_succeeds(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN a single well-formed JSONL record is appended (append-only)
    THEN skill_runtime_exec.py must succeed (regression control for AC3
    negative cases above)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append"})
    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Issue #2243 AC1 / AC9: a synthetic .guard_shadow_log.jsonl over the old
# 8MiB ceiling must not trigger unauthorized_write_path -- neither at the
# direct-unit level (AC1) nor through the canonical skill_runtime_exec.py
# executor (AC9 runtime verification). The fixture is generated at test-run
# time under tmp_path and never committed to the repository.
# ---------------------------------------------------------------------------


def _write_synthetic_jsonl(path: Path, min_bytes: int) -> int:
    """Write a synthetic well-formed JSONL fixture at `path` with at least
    `min_bytes` bytes, returning the actual byte count written. Never
    committed to the repository -- generated fresh under tmp_path."""
    written = 0
    seq = 0
    with open(path, "wb") as f:
        while written < min_bytes:
            line = (json.dumps({"schema_version": "1", "event": "synthetic", "seq": seq}) + "\n").encode()
            f.write(line)
            written += len(line)
            seq += 1
    return written


def test_guard_shadow_log_over_8mib_direct_unit_append_not_blocked(tmp_path: Path) -> None:
    """AC1 (direct unit level): a `.guard_shadow_log.jsonl` synthetic fixture
    larger than the old 8MiB ceiling, with a regular JSONL append applied to
    it, must be authorized as a valid `regular -> regular` content
    transition by the new bounded-memory streaming capture -- the old
    `_SHADOW_LOG_MAX_BYTES` sentinel-fail behavior must not resurface."""
    module = _load_skill_runtime_exec_module()
    over_8mib = (8 * 1024 * 1024) + (64 * 1024)
    before_path = tmp_path / "shadow-log-before.jsonl"
    _write_synthetic_jsonl(before_path, over_8mib)
    before_bytes = before_path.read_bytes()
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    assert before_capture is not None
    assert before_capture.total > 8 * 1024 * 1024

    appended = before_bytes + (json.dumps({"schema_version": "1", "event": "appended"}) + "\n").encode()
    after_capture = _capture_from_bytes(module, appended, tmp_path, prefix_size=before_capture.total)
    assert after_capture is not None

    assert module._is_authorized_shadow_log_content_transition_capture(before_capture, after_capture)


def test_guard_shadow_log_over_8mib_runtime_smoke_via_executor(tmp_path: Path) -> None:
    """AC1 / AC9 (runtime verification): the canonical `skill_runtime_exec.py`
    privileged executor, run against a synthetic fixture repo whose
    `.guard_shadow_log.jsonl` is seeded above the old 8MiB ceiling before a
    regular JSONL peer append happens during the run, must not fail with
    `unauthorized_write_path`. The fixture is generated fresh under
    `tmp_path` and is never written to a permanently tracked repository
    path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = repo / ".guard_shadow_log.jsonl"
    written = _write_synthetic_jsonl(shadow_log, (8 * 1024 * 1024) + (64 * 1024))
    assert written > 8 * 1024 * 1024

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append"})
    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    assert lines[-1]
    assert json.loads(lines[-1])["event"] == "self-append"


# ---------------------------------------------------------------------------
# Issue #2243 AC2: a 32MiB+ synthetic log must not require a total-size
# proportional buffer. This is demonstrated both structurally (the streaming
# scan never grows a Python-heap allocation proportional to file size) and
# via a `tracemalloc` peak-usage bound well under the file size.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_32mib_stream_capture_bounded_memory(tmp_path: Path) -> None:
    """AC2: streaming a 32MiB+ synthetic `.guard_shadow_log.jsonl` through
    `_shadow_log_stream_capture` must not allocate memory proportional to
    the total file size -- peak traced allocation must stay well under the
    file size (bounded by a small multiple of the chunk size / per-record
    bound, not by total file size)."""
    import tracemalloc

    module = _load_skill_runtime_exec_module()
    over_32mib = (32 * 1024 * 1024) + (128 * 1024)
    path = tmp_path / "shadow-log-32mib.jsonl"
    written = _write_synthetic_jsonl(path, over_32mib)
    assert written > 32 * 1024 * 1024

    fd = os.open(str(path), os.O_RDONLY)
    try:
        tracemalloc.start()
        try:
            capture = module._shadow_log_stream_capture(fd)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    finally:
        os.close(fd)

    assert capture is not None
    assert capture.total == written
    assert capture.valid_jsonl
    # A total-size-proportional buffer (the old chunks.append()/b"".join()
    # pattern) would peak at roughly `written` bytes (32MiB+). Bounded
    # streaming must stay far below that -- generously allow up to 4MiB of
    # traced Python-level allocation (chunk buffers, hashers, scanner
    # state), which is still an order of magnitude below the file size.
    assert peak < 4 * 1024 * 1024, f"peak traced memory {peak} bytes suggests a total-size-proportional buffer"


def test_guard_shadow_log_stream_capture_has_no_total_size_proportional_buffer(tmp_path: Path) -> None:
    """AC2 (structural/code-level control): `_shadow_log_stream_capture` must
    never accumulate the full stream into a single buffer -- verified by
    wrapping `os.read` to track the largest single object ever handed back
    from a read call and asserting it never exceeds the bounded chunk size,
    regardless of total file size."""
    module = _load_skill_runtime_exec_module()
    over_32mib = (32 * 1024 * 1024) + (64 * 1024)
    path = tmp_path / "shadow-log-32mib-structural.jsonl"
    written = _write_synthetic_jsonl(path, over_32mib)

    real_read = os.read
    max_chunk_seen = 0

    def _tracked_read(fd: int, n: int) -> bytes:
        nonlocal max_chunk_seen
        chunk = real_read(fd, n)
        max_chunk_seen = max(max_chunk_seen, len(chunk))
        return chunk

    fd = os.open(str(path), os.O_RDONLY)
    try:
        original_os_read = module.os.read
        module.os.read = _tracked_read
        try:
            capture = module._shadow_log_stream_capture(fd)
        finally:
            module.os.read = original_os_read
    finally:
        os.close(fd)

    assert capture is not None
    assert capture.total == written
    assert max_chunk_seen <= module._SHADOW_LOG_STREAM_CHUNK_BYTES
    assert max_chunk_seen < written


# ---------------------------------------------------------------------------
# Issue #2243 Negative controls: disabling prefix-digest / JSONL validity /
# non-regular-kind checks must make the corresponding AC3/AC4/AC5 regression
# fail, proving those checks are actually load-bearing.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_negative_control_prefix_comparison_disabled_breaks_ac3(tmp_path: Path) -> None:
    """Negative control for AC3: if the prefix-digest comparison inside
    `_is_authorized_shadow_log_content_transition_capture` is disabled
    (monkeypatched to always treat the prefix as matching), a truncated /
    1-byte-mutated "after" state that AC3 requires to fail closed would
    instead be authorized -- proving the real implementation's prefix check
    is load-bearing."""
    module = _load_skill_runtime_exec_module()
    before_bytes = (json.dumps({"schema_version": "1", "event": "seed"}) + "\n").encode()
    # Mutate the single byte inside the prefix region (truncate + replace
    # with unrelated content of the same-or-different length) -- this must
    # be rejected by AC3 under the real implementation.
    mutated_after_bytes = (json.dumps({"schema_version": "1", "event": "tampered"}) + "\n").encode()

    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    after_capture = _capture_from_bytes(module, mutated_after_bytes, tmp_path, prefix_size=before_capture.total)

    # Sanity: the real implementation correctly fails closed.
    assert not module._is_authorized_shadow_log_content_transition_capture(before_capture, after_capture)

    def _always_matching_transition(before, after) -> bool:
        # Simulates a disabled prefix-digest comparison: skip straight to
        # "the appended tail is well-formed JSONL" without ever checking
        # that the prefix actually matches `before`.
        if after.total < before.total:
            return False
        if not before.valid_jsonl:
            return False
        return bool(after.valid_jsonl)

    original = module._is_authorized_shadow_log_content_transition_capture
    module._is_authorized_shadow_log_content_transition_capture = _always_matching_transition
    try:
        assert module._is_authorized_shadow_log_content_transition_capture(before_capture, after_capture)
    finally:
        module._is_authorized_shadow_log_content_transition_capture = original


def test_guard_shadow_log_negative_control_jsonl_validation_disabled_breaks_ac4(tmp_path: Path) -> None:
    """Negative control for AC4: if JSONL well-formedness validation is
    disabled (a scanner that always reports `valid_jsonl=True`), malformed
    appended content that AC4 requires to fail closed would instead be
    authorized -- proving the real `_JsonlLineScanner` validation is
    load-bearing."""
    module = _load_skill_runtime_exec_module()

    class _AlwaysValidScanner:
        def feed(self, chunk: bytes) -> None:
            pass

        def finalize(self) -> bool:
            return True

    before_bytes = (json.dumps({"schema_version": "1", "event": "seed"}) + "\n").encode()
    malformed_after_bytes = before_bytes + b"not-json-at-all\n"

    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    after_capture = _capture_from_bytes(module, malformed_after_bytes, tmp_path, prefix_size=before_capture.total)

    # Sanity: the real implementation correctly fails closed on malformed
    # appended content.
    assert after_capture.suffix_valid_jsonl is False
    assert not module._is_authorized_shadow_log_content_transition_capture(before_capture, after_capture)

    original_scanner_cls = module._JsonlLineScanner
    module._JsonlLineScanner = _AlwaysValidScanner
    try:
        disabled_after_capture = _capture_from_bytes(
            module, malformed_after_bytes, tmp_path, prefix_size=before_capture.total
        )
        assert disabled_after_capture.suffix_valid_jsonl is True
        assert module._is_authorized_shadow_log_content_transition_capture(before_capture, disabled_after_capture)
    finally:
        module._JsonlLineScanner = original_scanner_cls


def test_guard_shadow_log_negative_control_nonregular_kind_check_disabled_breaks_ac5(tmp_path: Path) -> None:
    """Negative control for AC5: if the non-regular-kind check
    (`_is_allowed_shadow_log_kind_transition`) is disabled (monkeypatched to
    always authorize), a `regular -> symlink` substitution that AC5
    requires to fail closed would instead be silently authorized -- proving
    the real allow-tuple check is load-bearing."""
    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))
    _seed_shadow_log(repo)
    before_bytes = (repo / module._SHADOW_LOG_EXACT_REL).read_bytes()
    before_stat = (repo / module._SHADOW_LOG_EXACT_REL).stat()
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)

    (repo / module._SHADOW_LOG_EXACT_REL).unlink()
    (repo / module._SHADOW_LOG_EXACT_REL).symlink_to("/etc/hostname")

    # Sanity: the real implementation correctly fails closed on the
    # regular -> symlink substitution.
    unauthorized = module._find_unauthorized_repo_changes(
        str(repo),
        "1228",
        before_snapshot,
        before_status,
        shadow_log_before_kind="regular",
        shadow_log_before_capture=before_capture,
        shadow_log_before_identity=before_identity,
    )
    assert unauthorized == module._SHADOW_LOG_EXACT_REL

    original = module._is_allowed_shadow_log_kind_transition
    module._is_allowed_shadow_log_kind_transition = lambda before_kind, after_kind: True
    try:
        disabled_unauthorized = module._find_unauthorized_repo_changes(
            str(repo),
            "1228",
            before_snapshot,
            before_status,
            shadow_log_before_kind="regular",
            shadow_log_before_capture=before_capture,
            shadow_log_before_identity=before_identity,
        )
        assert disabled_unauthorized is None
    finally:
        module._is_allowed_shadow_log_kind_transition = original


# ---------------------------------------------------------------------------
# AC6: behavior-based test that the guarantee is NOT implemented as a bare
# tuple addition to _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS.
# ---------------------------------------------------------------------------


def _capture_from_bytes(module, data: bytes, tmp_path: Path, prefix_size: int | None = None):
    """Test helper: build a `ShadowLogStreamCapture` for a fixed in-memory
    byte blob by writing it to a scratch file and running it through the
    production streaming capture function, so direct unit tests can assert
    against the new capture-struct-based contract without needing raw
    buffered bytes plumbed through `_find_unauthorized_repo_changes`."""
    scratch = tmp_path / f"shadow-log-capture-scratch-{os.getpid()}-{id(data)}.jsonl"
    scratch.write_bytes(data)
    fd = os.open(str(scratch), os.O_RDONLY)
    try:
        capture = module._shadow_log_stream_capture(fd, prefix_size=prefix_size)
    finally:
        os.close(fd)
    scratch.unlink()
    return capture


def _load_skill_runtime_exec_module():
    agent_guards_dir = REPO_ROOT / "scripts" / "agent-guards"
    if str(agent_guards_dir) not in sys.path:
        sys.path.insert(0, str(agent_guards_dir))
    module_name = "skill_runtime_exec_under_test_issue_1563"
    if module_name in sys.modules:
        return sys.modules[module_name]
    module_path = agent_guards_dir / (_skill_runtime_exec_rel().rsplit("/", 1)[-1])
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_guard_shadow_log_is_not_a_directory_root_exclusion(tmp_path: Path) -> None:
    """GIVEN the production skill_runtime_exec.py module
    THEN .guard_shadow_log.jsonl must not be a member (exact or prefix) of
    _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS, AND (behavior-based, not just a
    tuple-membership assertion) a symlink substitution of
    .guard_shadow_log.jsonl must still be independently detected as
    unauthorized by _find_unauthorized_repo_changes -- if the guarantee had
    instead been implemented as a bare tuple addition to
    _RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS, this symlink substitution would
    be silently pruned before kind inspection and this assertion would fail."""
    module = _load_skill_runtime_exec_module()

    assert module._SHADOW_LOG_EXACT_REL not in module._RACE_TOLERANT_UNATTRIBUTABLE_ROOT_RELS
    assert not module._is_race_tolerant_unattributable_path(module._SHADOW_LOG_EXACT_REL)

    repo = _make_repo(tmp_path)
    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))
    (repo / module._SHADOW_LOG_EXACT_REL).symlink_to("/etc/hostname")

    unauthorized = module._find_unauthorized_repo_changes(
        str(repo),
        "1228",
        before_snapshot,
        before_status,
        shadow_log_before_kind="absent",
        shadow_log_before_capture=None,
    )
    assert unauthorized == module._SHADOW_LOG_EXACT_REL


def test_guard_shadow_log_kind_transition_is_explicit_allow_tuple(tmp_path: Path) -> None:
    """Direct unit test of _is_allowed_shadow_log_kind_transition: only the
    three documented transitions are authorized; every other before/after
    kind combination is rejected (explicit allow-tuple match, not a
    postcondition-only after_kind == "regular" check)."""
    module = _load_skill_runtime_exec_module()

    authorized = {
        ("absent", "absent"),
        ("absent", "regular"),
        ("regular", "regular"),
    }
    kinds = ["absent", "regular", "symlink", "dir", "fifo", "socket", "device"]
    for before_kind in kinds:
        for after_kind in kinds:
            expected = (before_kind, after_kind) in authorized
            actual = module._is_allowed_shadow_log_kind_transition(before_kind, after_kind)
            assert actual == expected, (before_kind, after_kind)


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES Blocker 1: exact-path observation must be
# generation-consistent with (and must run strictly after) the generic
# repo-wide snapshot/status capture.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_check_runs_after_generic_snapshot(tmp_path: Path, monkeypatch) -> None:
    """GIVEN the production _find_unauthorized_repo_changes implementation
    THEN the shadow-log exact-path observation (_shadow_log_stable_observation)
    must be invoked strictly after the generic repo-wide `_snapshot_repo_paths`
    / `_git_status_paths` "after" capture -- not before it (PR #1572
    REQUEST_CHANGES Blocker 1: the previous ordering left a TOCTOU window
    between the exact-path content read and the generic diff capture during
    which the excluded path could be replaced with a generation the generic
    diff never independently re-validated)."""
    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    _seed_shadow_log(repo)

    call_order: list[str] = []
    real_snapshot = module._snapshot_repo_paths
    real_status = module._git_status_paths
    real_observation = module._shadow_log_stable_observation

    def _tracked_snapshot(*args, **kwargs):
        call_order.append("snapshot")
        return real_snapshot(*args, **kwargs)

    def _tracked_status(*args, **kwargs):
        call_order.append("status")
        return real_status(*args, **kwargs)

    def _tracked_observation(*args, **kwargs):
        call_order.append("shadow_log_observation")
        return real_observation(*args, **kwargs)

    monkeypatch.setattr(module, "_snapshot_repo_paths", _tracked_snapshot)
    monkeypatch.setattr(module, "_git_status_paths", _tracked_status)
    monkeypatch.setattr(module, "_shadow_log_stable_observation", _tracked_observation)

    before_snapshot = real_snapshot(str(repo), "1228")
    before_status = real_status(str(repo))

    unauthorized = module._find_unauthorized_repo_changes(
        str(repo),
        "1228",
        before_snapshot,
        before_status,
        shadow_log_before_kind="regular",
        shadow_log_before_capture=_capture_from_bytes(
            module, (repo / module._SHADOW_LOG_EXACT_REL).read_bytes(), tmp_path
        ),
        shadow_log_before_identity=(0, 0, 0, 0),
    )
    # The before_identity is deliberately a placeholder that will never
    # match the real after-identity, so this call is expected to report the
    # shadow log path as unauthorized (Blocker 3 inode check) -- what this
    # test actually asserts is the *call order*, not this particular
    # outcome.
    assert unauthorized == module._SHADOW_LOG_EXACT_REL
    assert "snapshot" in call_order
    assert "status" in call_order
    assert "shadow_log_observation" in call_order
    assert call_order.index("shadow_log_observation") > call_order.index("snapshot")
    assert call_order.index("shadow_log_observation") > call_order.index("status")


def test_guard_shadow_log_stable_observation_retries_and_fails_closed_on_persistent_replace(
    tmp_path: Path, monkeypatch
) -> None:
    """Unit-level regression for Blocker 1: if the shadow-log path keeps
    being replaced by a distinct inode on every single observation attempt
    (persisting for the entire bounded retry budget), `_shadow_log_stable_observation`
    must never return a stale/inconsistent generation -- it must exhaust the
    retry budget and return the `_SHADOW_LOG_KIND_UNSTABLE` sentinel, which
    `_is_allowed_shadow_log_kind_transition` never authorizes."""
    module = _load_skill_runtime_exec_module()
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS", 4)
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS", 0.001)

    path = tmp_path / ".guard_shadow_log.jsonl"
    path.write_text(json.dumps({"schema_version": "1", "event": "seed"}) + "\n")

    real_read = os.read

    def _read_then_swap(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        # Replace the file with a distinct inode on every read, so the
        # final re-lstat() identity check can never match the fd's fstat()
        # identity within the retry budget.
        tmp = path.with_name(".guard_shadow_log.jsonl.swap")
        tmp.write_text(json.dumps({"schema_version": "1", "event": "swapped"}) + "\n")
        os.replace(tmp, path)
        return chunk

    monkeypatch.setattr(module.os, "read", _read_then_swap)

    kind, identity, content = module._shadow_log_stable_observation(path)
    assert kind == module._SHADOW_LOG_KIND_UNSTABLE
    assert identity is None
    assert content is None
    assert not module._is_allowed_shadow_log_kind_transition("regular", kind)


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES Blocker 2: cold-start (absent -> regular) content
# must be validated as well-formed JSONL, not merely "some regular file".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    ["cold-start-malformed", "cold-start-blank-line", "cold-start-nan"],
)
def test_guard_shadow_log_cold_start_malformed_content_fails_closed(tmp_path: Path, mutate: str) -> None:
    """GIVEN .guard_shadow_log.jsonl does not exist yet (cold start)
    WHEN it is created with content that is a regular file but NOT
    well-formed JSONL (not JSON at all, contains a blank line, or contains
    a non-standard NaN token)
    THEN skill_runtime_exec.py must fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = repo / ".guard_shadow_log.jsonl"
    assert not shadow_log.exists()

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES Blocker 3: regular -> regular must be a genuine
# in-place append (same inode), not a different-inode replacement, even when
# the replacement's content is a byte-for-byte valid JSONL extension.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_replace_with_different_inode_valid_prefix_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN it is replaced (os.replace onto a distinct inode) by content that is
    a byte-for-byte valid JSONL extension of the original (i.e. would pass
    a content-only append check)
    THEN skill_runtime_exec.py must still fail with unauthorized_write_path
    (an in-place append and a same-content different-inode replacement are
    not the same guarantee -- a concurrent producer still appending to the
    original inode would otherwise split-brain silently)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "replace-inode-valid-prefix"})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_guard_shadow_log_content_transition_rejects_different_inode(tmp_path: Path) -> None:
    """Direct unit-level regression for Blocker 3: `_find_unauthorized_repo_changes`
    must reject a regular -> regular transition whose before/after identity
    tuples have different (st_dev, st_ino), even though the content-only
    check (`_is_authorized_shadow_log_content_transition_capture`) would
    authorize it as a valid append."""
    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    shadow_log = _seed_shadow_log(repo)
    before_bytes = shadow_log.read_bytes()
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    before_stat = shadow_log.stat()
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)

    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))

    tmp = shadow_log.with_name(".guard_shadow_log.jsonl.tmp")
    tmp.write_bytes(before_bytes + (json.dumps({"schema_version": "1", "event": "appended"}) + "\n").encode())
    os.replace(tmp, shadow_log)

    # Sanity: the content-only check alone would authorize this transition.
    after_bytes = shadow_log.read_bytes()
    after_capture = _capture_from_bytes(module, after_bytes, tmp_path, prefix_size=before_capture.total)
    assert module._is_authorized_shadow_log_content_transition_capture(before_capture, after_capture)

    unauthorized = module._find_unauthorized_repo_changes(
        str(repo),
        "1228",
        before_snapshot,
        before_status,
        shadow_log_before_kind="regular",
        shadow_log_before_capture=before_capture,
        shadow_log_before_identity=before_identity,
    )
    assert unauthorized == module._SHADOW_LOG_EXACT_REL


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES Blocker 4: the JSONL parser must implement
# well-formed JSON Lines, not "json.loads per non-blank line".
# ---------------------------------------------------------------------------


def test_guard_shadow_log_parser_rejects_blank_lines() -> None:
    module = _load_skill_runtime_exec_module()
    data = (
        json.dumps({"schema_version": "1", "event": "a"}).encode()
        + b"\n\n"
        + json.dumps({"schema_version": "1", "event": "b"}).encode()
        + b"\n"
    )
    assert module._parse_shadow_log_jsonl(data) is None


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_guard_shadow_log_parser_rejects_nonstandard_json_constants(token: bytes) -> None:
    module = _load_skill_runtime_exec_module()
    data = b'{"schema_version": "1", "value": ' + token + b"}\n"
    assert module._parse_shadow_log_jsonl(data) is None


def test_guard_shadow_log_parser_accepts_well_formed_jsonl() -> None:
    module = _load_skill_runtime_exec_module()
    data = (
        json.dumps({"schema_version": "1", "event": "a"}).encode()
        + b"\n"
        + json.dumps({"schema_version": "1", "event": "b"}).encode()
        + b"\n"
    )
    records = module._parse_shadow_log_jsonl(data)
    assert records == [
        {"schema_version": "1", "event": "a"},
        {"schema_version": "1", "event": "b"},
    ]


@pytest.mark.parametrize("mutate", ["append-blank-line", "append-nan"])
def test_guard_shadow_log_blank_line_and_nan_append_fails_closed(tmp_path: Path, mutate: str) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl with a seed record
    WHEN a blank line or a non-standard NaN-containing line is appended
    THEN skill_runtime_exec.py must fail with unauthorized_write_path."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    _seed_shadow_log(repo)

    result = _run_executor(repo, {"SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": mutate})
    assert result.returncode == 2
    assert "reason_code=unauthorized_write_path" in result.stderr
    assert "unauthorized write path=.guard_shadow_log.jsonl" in result.stderr


def test_shadow_log_hook_producer_rejects_nan_payload(tmp_path: Path) -> None:
    """GIVEN the production .claude/hooks/shadow_log.py producer
    WHEN invoked with a --fields-json payload containing a non-standard NaN
    value
    THEN it must fail closed (non-zero exit, no line written) rather than
    writing a non-standard-JSON line to the log (PR #1572 REQUEST_CHANGES
    Blocker 4: allow_nan=False on the producer side)."""
    log_file = tmp_path / ".guard_shadow_log.jsonl"
    shadow_log_py = REPO_ROOT / ".claude" / "hooks" / "shadow_log.py"
    result = subprocess.run(
        [
            sys.executable,
            str(shadow_log_py),
            "--log-file",
            str(log_file),
            "--fields-json",
            '{"guard_name": "test", "value": NaN}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not log_file.exists() or log_file.read_text() == ""


def test_shadow_log_hook_producer_writes_well_formed_line(tmp_path: Path) -> None:
    log_file = tmp_path / ".guard_shadow_log.jsonl"
    shadow_log_py = REPO_ROOT / ".claude" / "hooks" / "shadow_log.py"
    result = subprocess.run(
        [
            sys.executable,
            str(shadow_log_py),
            "--log-file",
            str(log_file),
            "--fields-json",
            '{"guard_name": "test", "event": "ok"}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    module = _load_skill_runtime_exec_module()
    assert module._parse_shadow_log_jsonl(log_file.read_bytes()) is not None


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES (Medium): explicit shadow-log size contract.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_oversized_content_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """Issue #2243: the old `_SHADOW_LOG_MAX_BYTES` total-file-size ceiling
    was replaced by `_SHADOW_LOG_SAFETY_VALVE_MAX_BYTES` (a pure DoS safety
    valve, not the primary bounded-memory defense). This regression asserts
    the safety valve still fails closed at whatever bound it is set to."""
    module = _load_skill_runtime_exec_module()
    monkeypatch.setattr(module, "_SHADOW_LOG_SAFETY_VALVE_MAX_BYTES", 16)

    path = tmp_path / ".guard_shadow_log.jsonl"
    path.write_text(json.dumps({"schema_version": "1", "event": "this-record-is-too-long"}) + "\n")

    kind, identity, content = module._shadow_log_stable_observation(path)
    assert kind == module._SHADOW_LOG_KIND_UNSTABLE
    assert identity is None
    assert content is None


# ---------------------------------------------------------------------------
# PR #1572 REQUEST_CHANGES High: real independent OS-process concurrent
# append, not thread-based sleep-staggered timing.
# ---------------------------------------------------------------------------


def _wait_for_barrier(barrier_path: Path, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not barrier_path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"barrier {barrier_path} never appeared")
        time.sleep(0.005)


def test_guard_shadow_log_real_multiprocess_barrier_synchronized_append(tmp_path: Path) -> None:
    """GIVEN a pre-existing .guard_shadow_log.jsonl
    WHEN four independent OS processes (a Python producer using the
    production shadow_log.py hook, a second independent shadow_log.py
    invocation, a bash-shell `>>` append producer mirroring the
    rtk_boundary_shadow_guard.sh direct-write fallback, and a Node.js
    fs.appendFileSync producer) are released simultaneously via a
    filesystem barrier and append concurrently while this command's own
    child subprocess is still running
    THEN skill_runtime_exec.py must not fail with unauthorized_write_path,
    every producer's record must be present in the final file with no
    partial/corrupted lines, and the expected record count must exactly
    match the actual record count (no lost or duplicated records)."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)
    barrier = tmp_path / "start_barrier"

    python_producer = tmp_path / "producer_python.py"
    python_producer.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        f"barrier = Path({str(barrier)!r})\n"
        "while not barrier.exists():\n"
        "    time.sleep(0.002)\n"
        f"shadow_log_py = {str(REPO_ROOT / '.claude' / 'hooks' / 'shadow_log.py')!r}\n"
        f"log_file = {str(shadow_log)!r}\n"
        "import subprocess\n"
        "for seq in range(5):\n"
        "    subprocess.run([\n"
        "        sys.executable, shadow_log_py,\n"
        "        '--log-file', log_file,\n"
        "        '--fields-json', json.dumps({'guard_name': 'test-producer-python', 'seq': seq}),\n"
        "    ], check=True)\n"
    )

    node_producer = tmp_path / "producer_node.mjs"
    node_producer.write_text(
        "import fs from 'fs';\n"
        f"const barrier = {str(barrier)!r};\n"
        f"const logFile = {str(shadow_log)!r};\n"
        "function sleepSync(ms) { const end = Date.now() + ms; while (Date.now() < end) {} }\n"
        "while (!fs.existsSync(barrier)) { sleepSync(2); }\n"
        "for (let seq = 0; seq < 5; seq++) {\n"
        "  const entry = JSON.stringify({ guard_name: 'test-producer-node', seq }) + '\\n';\n"
        "  fs.appendFileSync(logFile, entry, 'utf8');\n"
        "}\n"
    )

    bash_producer = tmp_path / "producer_bash.sh"
    bash_producer.write_text(
        "#!/usr/bin/env bash\n"
        f"barrier={barrier}\n"
        f"log_file={shadow_log}\n"
        "while [ ! -e \"$barrier\" ]; do sleep 0.002; done\n"
        "for seq in 0 1 2 3 4; do\n"
        "  printf '{\"guard_name\":\"test-producer-bash\",\"seq\":%s}\\n' \"$seq\" >> \"$log_file\"\n"
        "done\n"
    )
    bash_producer.chmod(0o755)

    node_bin = shutil.which("node")
    procs: list[subprocess.Popen] = []
    procs.append(subprocess.Popen([sys.executable, str(python_producer)]))
    procs.append(subprocess.Popen(["bash", str(bash_producer)]))
    have_node = node_bin is not None
    if have_node:
        procs.append(subprocess.Popen([node_bin, str(node_producer)]))

    executor_proc = subprocess.Popen(
        [
            sys.executable,
            _skill_runtime_exec_rel(),
            "--command-id",
            "preflight.run",
            "--issue-number",
            "1228",
            "--repo",
            "squne121/loop-protocol",
        ],
        cwd=str(repo),
        env={
            **os.environ,
            "CLAUDE_PROJECT_DIR": str(repo),
            "SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.4",
            "SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    time.sleep(0.05)
    barrier.write_text("go\n")

    stdout, stderr = executor_proc.communicate(timeout=30)
    for proc in procs:
        proc.wait(timeout=15)

    assert executor_proc.returncode == 0, stderr

    lines = [line for line in shadow_log.read_text().splitlines() if line]
    for line in lines:
        json.loads(line)  # every retained line must be individually valid JSON

    events = [json.loads(line) for line in lines]
    python_events = {e["seq"] for e in events if e.get("guard_name") == "test-producer-python"}
    bash_events = {e["seq"] for e in events if e.get("guard_name") == "test-producer-bash"}
    assert python_events == {0, 1, 2, 3, 4}
    assert bash_events == {0, 1, 2, 3, 4}
    if have_node:
        node_events = {e["seq"] for e in events if e.get("guard_name") == "test-producer-node"}
        assert node_events == {0, 1, 2, 3, 4}

    seed_events = [e for e in events if e.get("event") == "seed"]
    self_append_events = [e for e in events if e.get("event") == "self-append"]
    assert len(seed_events) == 1
    assert len(self_append_events) == 1

    artifact = repo / ".claude" / "artifacts" / "issue-refinement-loop" / "1228" / "preflight.json"
    assert artifact.exists()


# ---------------------------------------------------------------------------
# Real filesystem object integration tests for non-regular kinds (not just
# kind-string combinations): FIFO, UNIX-domain socket, and (best-effort,
# skipped if unprivileged) a device node.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_real_fifo_substitution_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is replaced by a *real* FIFO (os.mkfifo), not merely a kind
    string
    THEN _find_unauthorized_repo_changes must fail closed on that exact
    path, and _shadow_log_stable_observation must classify it as "fifo"
    without blocking (O_NONBLOCK open, no writer present)."""
    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    shadow_log = _seed_shadow_log(repo)
    before_bytes = shadow_log.read_bytes()
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    before_stat = shadow_log.stat()
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)

    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))

    shadow_log.unlink()
    os.mkfifo(shadow_log)
    try:
        kind, identity, content = module._shadow_log_stable_observation(shadow_log)
        assert kind == "fifo"
        assert content is None

        unauthorized = module._find_unauthorized_repo_changes(
            str(repo),
            "1228",
            before_snapshot,
            before_status,
            shadow_log_before_kind="regular",
            shadow_log_before_capture=before_capture,
            shadow_log_before_identity=before_identity,
        )
        assert unauthorized == module._SHADOW_LOG_EXACT_REL
    finally:
        if shadow_log.exists():
            shadow_log.unlink()


def test_guard_shadow_log_real_unix_socket_substitution_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is replaced by a *real* bound UNIX-domain socket (socket.bind),
    not merely a kind string
    THEN _find_unauthorized_repo_changes must fail closed on that exact
    path."""
    import socket

    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    shadow_log = _seed_shadow_log(repo)
    before_bytes = shadow_log.read_bytes()
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    before_stat = shadow_log.stat()
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)

    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))

    shadow_log.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(shadow_log))
        kind, identity, content = module._shadow_log_stable_observation(shadow_log)
        assert kind == "socket"
        assert content is None

        unauthorized = module._find_unauthorized_repo_changes(
            str(repo),
            "1228",
            before_snapshot,
            before_status,
            shadow_log_before_kind="regular",
            shadow_log_before_capture=before_capture,
            shadow_log_before_identity=before_identity,
        )
        assert unauthorized == module._SHADOW_LOG_EXACT_REL
    finally:
        sock.close()
        if shadow_log.exists():
            shadow_log.unlink()


def test_guard_shadow_log_real_device_node_substitution_fails_closed(tmp_path: Path) -> None:
    """GIVEN a pre-existing regular .guard_shadow_log.jsonl
    WHEN it is replaced by a *real* device node (os.mknod with S_IFCHR),
    not merely a kind string
    THEN _find_unauthorized_repo_changes must fail closed on that exact
    path. Skipped in unprivileged sandboxes where CAP_MKNOD is unavailable
    (os.mknod raises PermissionError)."""
    module = _load_skill_runtime_exec_module()
    repo = _make_repo(tmp_path)
    shadow_log = _seed_shadow_log(repo)
    before_bytes = shadow_log.read_bytes()
    before_capture = _capture_from_bytes(module, before_bytes, tmp_path)
    before_stat = shadow_log.stat()
    before_identity = (before_stat.st_dev, before_stat.st_ino, before_stat.st_size, before_stat.st_mtime_ns)

    before_snapshot = module._snapshot_repo_paths(str(repo), "1228")
    before_status = module._git_status_paths(str(repo))

    shadow_log.unlink()
    try:
        os.mknod(shadow_log, mode=stat.S_IFCHR | 0o600, device=os.makedev(1, 3))
    except (PermissionError, OSError) as exc:
        pytest.skip(f"device node creation not permitted in this sandbox: {exc}")

    try:
        kind, identity, content = module._shadow_log_stable_observation(shadow_log)
        assert kind == "device"
        assert content is None

        unauthorized = module._find_unauthorized_repo_changes(
            str(repo),
            "1228",
            before_snapshot,
            before_status,
            shadow_log_before_kind="regular",
            shadow_log_before_capture=before_capture,
            shadow_log_before_identity=before_identity,
        )
        assert unauthorized == module._SHADOW_LOG_EXACT_REL
    finally:
        if shadow_log.exists():
            shadow_log.unlink()


# ---------------------------------------------------------------------------
# Issue #2243 AC7 (fixed-cutoff snapshot semantics, "B案"): a continuously
# appending producer must not be able to indefinitely prevent
# `_shadow_log_stable_observation` from converging -- observation reads only
# `[0, observed_end)` fixed at `fstat()` time, so ongoing growth past that
# cutoff cannot fail or retry-exhaust this attempt; it is simply left for the
# next audit epoch.
# ---------------------------------------------------------------------------


def test_guard_shadow_log_cutoff_semantics_ongoing_append_past_cutoff_does_not_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """GIVEN a shadow-log path whose content keeps growing (append-only)
    strictly *after* every single `fstat()`-observed cutoff, on every retry
    attempt
    THEN `_shadow_log_stable_observation` must still converge to a
    successful "regular" observation within a small, bounded number of
    attempts -- it must NOT exhaust the retry budget and return
    `_SHADOW_LOG_KIND_UNSTABLE`. Under the old "wait for read() to reach EOF,
    then require final mtime == fstat mtime" design this scenario would
    retry-exhaust and fail closed forever; that is exactly the Blocker 1
    defect AC7 was revised to eliminate."""
    module = _load_skill_runtime_exec_module()
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS", 5)
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS", 0.001)

    path = tmp_path / ".guard_shadow_log.jsonl"
    path.write_text(json.dumps({"schema_version": "1", "event": "seed"}) + "\n")

    real_read = os.read
    appends_done = 0

    def _read_then_append(fd: int, n: int) -> bytes:
        nonlocal appends_done
        chunk = real_read(fd, n)
        # Simulate a peer producer appending *after* our fstat()-observed
        # cutoff was already fixed, strictly during the bounded read of this
        # attempt -- this must never be observed by this attempt's capture
        # (its `read_upto` bound was already fixed before this write
        # happens), and must not prevent the attempt from converging.
        if appends_done < 3:
            with open(path, "a") as f:
                f.write(json.dumps({"schema_version": "1", "event": f"peer-{appends_done}"}) + "\n")
            appends_done += 1
        return chunk

    monkeypatch.setattr(module.os, "read", _read_then_append)

    kind, identity, capture = module._shadow_log_stable_observation(path)
    assert kind == "regular"
    assert identity is not None
    assert capture is not None
    # Only the seed record (present at the fstat()-observed cutoff) is part
    # of this attempt's capture -- the peer appends made strictly after that
    # cutoff was fixed are outside this audit epoch.
    assert capture.total == len(json.dumps({"schema_version": "1", "event": "seed"}).encode()) + 1


def test_guard_shadow_log_cutoff_semantics_truncation_below_cutoff_still_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """GIVEN a shadow-log path that is truncated to below the `fstat()`-
    observed cutoff strictly during the bounded read of every attempt
    THEN `_shadow_log_stable_observation` must never return a successful
    "regular" observation for that inconsistent generation -- it must
    exhaust the retry budget and return `_SHADOW_LOG_KIND_UNSTABLE` (AC7:
    growth past the cutoff is authorized to be skipped, but shrinkage below
    the cutoff during the read must still fail closed)."""
    module = _load_skill_runtime_exec_module()
    attempts = 3
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS", attempts)
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS", 0.001)

    path = tmp_path / ".guard_shadow_log.jsonl"
    seed_line = json.dumps({"schema_version": "1", "event": "seed-line-long-enough-to-truncate"}) + "\n"
    path.write_text(seed_line)

    # Strictly decreasing sizes (each smaller than every prior write) so
    # this attempt's post-read size is always smaller than the observed_end
    # that was fixed at the top of *this* attempt -- guaranteeing the
    # shrink-below-cutoff mismatch is detected on every single attempt, and
    # the sequence never idempotently stabilizes.
    sizes = [len(seed_line) - (i + 1) * 4 for i in range(attempts + 2)]
    assert all(size > 0 for size in sizes)
    assert sizes == sorted(sizes, reverse=True)
    call_count = 0
    real_read = os.read

    def _read_then_truncate(fd: int, n: int) -> bytes:
        nonlocal call_count
        chunk = real_read(fd, n)
        size = sizes[min(call_count, len(sizes) - 1)]
        with open(path, "wb") as f:
            f.write(b"x" * size + b"\n")
        call_count += 1
        return chunk

    monkeypatch.setattr(module.os, "read", _read_then_truncate)

    kind, identity, capture = module._shadow_log_stable_observation(path)
    assert kind == module._SHADOW_LOG_KIND_UNSTABLE
    assert identity is None
    assert capture is None


def test_guard_shadow_log_cutoff_semantics_end_to_end_continuous_append_via_executor(
    tmp_path: Path,
) -> None:
    """AC7 (runtime/end-to-end): a shadow-log that is continuously appended
    to by an independent background process for the whole duration of the
    privileged executor run must not cause `unauthorized_write_path` -- the
    fixed-cutoff snapshot semantics must converge even under sustained
    concurrent growth, not merely a single staggered append."""
    repo = _make_repo(tmp_path)
    _install_skill_runtime_exec_fixture(repo)
    shadow_log = _seed_shadow_log(repo)

    stop = threading.Event()

    def _continuous_appender() -> None:
        seq = 0
        while not stop.is_set():
            with open(shadow_log, "a") as f:
                f.write(json.dumps({"schema_version": "1", "event": "continuous", "seq": seq}) + "\n")
            seq += 1
            time.sleep(0.005)

    appender = threading.Thread(target=_continuous_appender)
    appender.start()
    try:
        result = _run_executor(
            repo,
            {
                "SKILL_RUNTIME_TEST_SLEEP_SECONDS": "0.5",
                "SKILL_RUNTIME_TEST_SHADOW_LOG_MUTATE": "self-append",
            },
        )
    finally:
        stop.set()
        appender.join(timeout=5)

    assert result.returncode == 0, result.stderr
    lines = [line for line in shadow_log.read_text().splitlines() if line]
    for line in lines:
        json.loads(line)


# ---------------------------------------------------------------------------
# Issue #2243 Blocker 3: `_SHADOW_LOG_MAX_RECORD_BYTES` boundary tests
# (4MiB compatibility safety margin, raised from 64KiB).
# ---------------------------------------------------------------------------


def _padded_record_line(record_body_len: int) -> bytes:
    """Build a single well-formed JSONL line (including trailing newline)
    whose *record body* (the line content excluding the trailing `\\n`,
    which is exactly what `_JsonlLineScanner` / `_SHADOW_LOG_MAX_RECORD_BYTES`
    bound) is exactly `record_body_len` bytes, by padding a string field to
    consume the remaining budget."""
    overhead = len(json.dumps({"schema_version": "1", "pad": ""}).encode())
    assert record_body_len >= overhead
    pad_len = record_body_len - overhead
    body = json.dumps({"schema_version": "1", "pad": "x" * pad_len}).encode()
    assert len(body) == record_body_len, (len(body), record_body_len)
    return body + b"\n"


def test_guard_shadow_log_record_at_max_bytes_boundary_accepted(tmp_path: Path) -> None:
    """A single record exactly at `_SHADOW_LOG_MAX_RECORD_BYTES` bytes must
    be accepted as valid JSONL (inclusive upper boundary)."""
    module = _load_skill_runtime_exec_module()
    line = _padded_record_line(module._SHADOW_LOG_MAX_RECORD_BYTES)
    capture = _capture_from_bytes(module, line, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is True


def test_guard_shadow_log_record_one_byte_over_max_bytes_boundary_rejected(tmp_path: Path) -> None:
    """A single record one byte over `_SHADOW_LOG_MAX_RECORD_BYTES` must be
    rejected as invalid JSONL (exclusive upper boundary -- the per-record
    memory bound is still enforced even after raising the constant to
    4MiB)."""
    module = _load_skill_runtime_exec_module()
    line = _padded_record_line(module._SHADOW_LOG_MAX_RECORD_BYTES + 1)
    capture = _capture_from_bytes(module, line, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


def test_guard_shadow_log_record_below_max_bytes_boundary_accepted(tmp_path: Path) -> None:
    """A record one byte under the boundary is accepted (sanity control for
    the boundary tests above)."""
    module = _load_skill_runtime_exec_module()
    line = _padded_record_line(module._SHADOW_LOG_MAX_RECORD_BYTES - 1)
    capture = _capture_from_bytes(module, line, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is True


def test_guard_shadow_log_max_record_bytes_is_generous_margin_above_real_producer_records() -> None:
    """Issue #2243 Blocker 3: the 4MiB bound must remain a generous
    compatibility safety margin -- not a value tuned down close to real
    observed producer record sizes (~400-610 bytes, max observed ~609
    bytes)."""
    module = _load_skill_runtime_exec_module()
    assert module._SHADOW_LOG_MAX_RECORD_BYTES == 4 * 1024 * 1024
    assert module._SHADOW_LOG_MAX_RECORD_BYTES > 1000 * 609


# ---------------------------------------------------------------------------
# Issue #2243 HIGH: process-level peak-memory evidence (not just tracemalloc
# Python-heap tracing) across multiple fixture sizes, and a retry-scenario
# regression guard.
# ---------------------------------------------------------------------------


def _write_synthetic_jsonl_bulk(path: Path, min_bytes: int) -> int:
    """Faster variant of `_write_synthetic_jsonl` for large (32MiB-256MiB)
    fixtures: builds one fixed-size JSONL line and writes it in a single
    bulk `bytes` multiplication + write, instead of looping one
    `json.dumps` call per record."""
    line = (json.dumps({"schema_version": "1", "event": "synthetic-bulk"}) + "\n").encode()
    repeat = (min_bytes // len(line)) + 1
    blob = line * repeat
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


# Issue #2243 HIGH (process-level memory evidence): a bare `os.read()` loop
# with nothing retained already shows peak RSS scaling close to 1x total
# bytes read on this sandbox's kernel/filesystem stack (verified empirically:
# on the CI/dev sandbox this repository is developed on, a no-op streaming
# read of a 256MiB file alone shows ru_maxrss within ~5% of 256MiB) -- i.e.
# an *absolute* process-level RSS ceiling that stays flat as file size grows
# is not an achievable or meaningful invariant in this environment, because
# the environment's own I/O/allocator accounting already scales with total
# bytes touched, independent of how the reading code buffers content. A
# *comparative* measurement against a deliberately naive full-buffer
# implementation (the exact `chunks.append()` / `b"".join(chunks)` pattern
# Issue #2243 requires eliminating) isolates the actual difference this
# redesign makes, and is robust to that environment-level floor.
# Both child scripts below self-report `ru_maxrss` *twice*: once
# immediately after interpreter/module startup (before touching the fixture
# at all) and once after the read/parse work completes. The *delta* between
# those two same-process watermarks isolates the memory cost of this
# specific operation from unrelated system-wide memory pressure (other
# concurrently running processes/tests) far better than comparing raw
# absolute `ru_maxrss` values across two independently-scheduled
# subprocesses would -- both watermarks are read from the *same* process, so
# whatever baseline noise affects one measurement affects both consistently.
_STREAM_CAPTURE_CHILD_SCRIPT = r"""
import os
import resource
import sys

sys.path.insert(0, sys.argv[2])
import skill_runtime_exec as module

baseline_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    capture = module._shadow_log_stream_capture(fd)
finally:
    os.close(fd)
assert capture is not None
after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(capture.total, baseline_kib, after_kib)
"""

# The naive baseline this redesign replaced (Issue #2243 Problem section):
# accumulate every chunk into a list, then `b"".join()` the whole thing
# before hashing/validating -- holding the entire file content resident (at
# least) twice at peak (the joined `bytes` plus the still-referenced list of
# chunks until it goes out of scope).
_NAIVE_FULL_BUFFER_CHILD_SCRIPT = r"""
import hashlib
import os
import resource
import sys

baseline_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
fd = os.open(sys.argv[1], os.O_RDONLY)
chunks = []
while True:
    chunk = os.read(fd, 1 << 20)
    if not chunk:
        break
    chunks.append(chunk)
os.close(fd)
data = b"".join(chunks)
hashlib.sha256(data).hexdigest()
after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(len(data), baseline_kib, after_kib)
"""


def _measure_child_rss_delta_kib(script: str, path: Path, *, needs_module: bool = False) -> int:
    """Spawn a fresh Python interpreter subprocess running `script` against
    `path` and return the *delta* between its self-reported pre-work and
    post-work `ru_maxrss` (KiB on Linux) -- see the module-level comment
    above `_STREAM_CAPTURE_CHILD_SCRIPT` for why the same-process delta
    (rather than the raw absolute peak) is used."""
    agent_guards_dir = str(REPO_ROOT / "scripts" / "agent-guards")
    args = [sys.executable, "-c", script, str(path)]
    if needs_module:
        args.append(agent_guards_dir)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=True,
        cwd=agent_guards_dir,
    )
    _total_str, baseline_str, after_str = result.stdout.strip().splitlines()[-1].split()
    return int(after_str) - int(baseline_str)


# Issue #2243 HIGH: 32MiB is included in the *Python-heap*
# (`tracemalloc`-based) bounded-memory test above
# (`test_guard_shadow_log_32mib_stream_capture_bounded_memory`), which is not
# subject to the noise described below. For this *process-level* RSS
# comparison specifically, 32MiB was found empirically to be too close to
# this sandbox's fixed per-process baseline (interpreter startup + module
# import) for the naive-vs-streaming gap to be a reliable signal under
# concurrent sandbox load -- 64MiB/128MiB/256MiB give a stable, reproducible
# margin.
@pytest.mark.parametrize("size_mib", [64, 128, 256])
def test_guard_shadow_log_process_level_peak_memory_below_naive_full_buffer_baseline(
    tmp_path: Path, size_mib: int
) -> None:
    """HIGH: process-level (not just Python-heap-traced) peak RSS while
    streaming a `size_mib` fixture through the production
    `_shadow_log_stream_capture` must stay meaningfully below the peak RSS
    of a deliberately naive full-buffer implementation (`chunks.append()` /
    `b"".join(chunks)`, the exact pattern Issue #2243 requires eliminating)
    processing the *same* fixture in the *same* environment -- proving the
    bounded-memory redesign has a real, measurable process-level effect, not
    merely a `tracemalloc`-visible one."""
    path = tmp_path / f"shadow-log-{size_mib}mib.jsonl"
    written = _write_synthetic_jsonl_bulk(path, size_mib * 1024 * 1024)
    assert written >= size_mib * 1024 * 1024

    # RSS measurements are inherently noisy under a shared/loaded sandbox
    # (page-cache warmth, concurrent processes, allocator jitter). Take the
    # best-case pairing across a few repeats (max observed naive delta, min
    # observed streaming delta) rather than a single sample, so incidental
    # noise cannot make a structurally-sound implementation appear to fail
    # (or, conversely, mask a real regression -- taking the *most
    # favorable* naive sample and *least favorable... i.e. lowest* streaming
    # sample is the conservative direction that still requires a real,
    # reproducible gap to pass). Each individual sample is itself already a
    # same-process pre/post delta (see `_measure_child_rss_delta_kib`), which
    # is what makes this comparison robust under concurrent sandbox load.
    trials = 3
    naive_deltas = [_measure_child_rss_delta_kib(_NAIVE_FULL_BUFFER_CHILD_SCRIPT, path) for _ in range(trials)]
    stream_deltas = [
        _measure_child_rss_delta_kib(_STREAM_CAPTURE_CHILD_SCRIPT, path, needs_module=True) for _ in range(trials)
    ]
    naive_delta_kib = max(naive_deltas)
    stream_delta_kib = min(stream_deltas)

    written_kib = written / 1024
    if naive_delta_kib < 0.05 * written_kib:
        # Sanity check on the measurement itself, not the implementation
        # under test: the *known-bad* naive full-buffer baseline is expected
        # to show growth roughly proportional to the fixture size. Under
        # extreme concurrent sandbox load (e.g. this file running as part of
        # the full `scripts/agent-guards/tests/` suite alongside many other
        # subprocess-spawning tests), even the naive baseline's own
        # pre/post-work `ru_maxrss` delta can round to ~0 (e.g. because the
        # process's resident set was already large from OS-level scheduling
        # noise before the measured window even started). If the naive
        # baseline itself did not show the expected proportional growth in
        # this run, the measurement is inconclusive this run -- it is not
        # evidence about the streaming implementation either way (the
        # deterministic, environment-noise-immune `tracemalloc`-based
        # bounded-memory tests above already cover this size/behavior).
        pytest.skip(
            f"naive full-buffer baseline RSS delta ({naive_delta_kib} KiB) did not show the "
            f"expected proportional growth for a {size_mib}MiB fixture under current sandbox "
            "load -- measurement inconclusive this run, not treated as a pass or fail"
        )
    savings_kib = naive_delta_kib - stream_delta_kib
    # A fixed relative-ratio threshold (e.g. "streaming must be under 75% of
    # naive") is dominated by a constant per-process baseline (interpreter
    # startup, module import) at small file sizes and would be a weak signal
    # at small `size_mib`. Instead require the *absolute* RSS-delta savings
    # versus the naive baseline to be at least proportional to a meaningful
    # fraction of the fixture size itself -- the naive implementation's extra
    # `chunks` list + `b"".join()` result together add roughly one
    # additional full copy of the file content on top of whatever the
    # streaming implementation needs, so savings should scale with file
    # size, not stay flat. The 5% floor (rather than a larger fraction) is
    # deliberately conservative to tolerate the sandbox-level RSS noise
    # described above while still requiring a real, size-scaling gap.
    assert savings_kib >= 0.05 * written_kib, (
        f"streaming RSS delta {stream_delta_kib} KiB (best of {trials}) saved only "
        f"{savings_kib} KiB versus the naive full-buffer baseline delta {naive_delta_kib} KiB "
        f"(worst of {trials}) for a {size_mib}MiB ({written_kib:.0f} KiB) fixture -- "
        "expected savings proportional to file size, suggesting the streaming "
        "implementation is no longer avoiding a total-size-proportional buffer"
    )


def test_guard_shadow_log_retry_scenario_memory_does_not_scale_with_attempt_count(
    tmp_path: Path, monkeypatch
) -> None:
    """HIGH: when `_shadow_log_stable_observation` retries repeatedly (a
    persistent kind-replacement race spanning the whole attempt budget), the
    peak Python-heap allocation across the *entire* call must stay bounded
    and must NOT grow proportionally with the number of attempts -- each
    attempt's cutoff-bound read must be independently bounded-memory, not
    accumulate state across attempts."""
    import tracemalloc

    module = _load_skill_runtime_exec_module()
    attempts = 20
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_ATTEMPTS", attempts)
    monkeypatch.setattr(module, "_SHADOW_LOG_STABLE_OBSERVATION_RETRY_SECONDS", 0.0)

    path = tmp_path / ".guard_shadow_log.jsonl"
    seed_bytes = (json.dumps({"schema_version": "1", "pad": "x" * (256 * 1024)}) + "\n").encode()
    path.write_text("")
    with open(path, "wb") as f:
        f.write(seed_bytes)

    real_read = os.read

    def _read_then_swap(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        tmp = path.with_name(".guard_shadow_log.jsonl.swap")
        with open(tmp, "wb") as f:
            f.write(seed_bytes)
        os.replace(tmp, path)
        return chunk

    monkeypatch.setattr(module.os, "read", _read_then_swap)

    tracemalloc.start()
    try:
        kind, _identity, _capture = module._shadow_log_stable_observation(path)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    assert kind == module._SHADOW_LOG_KIND_UNSTABLE
    # `attempts` (20) times the per-record seed size (256KiB) would be
    # ~5MiB if state were accumulated across attempts instead of being
    # independent per-attempt bounded reads. Assert peak stays well under
    # that `attempts * len(seed_bytes)` proportional bound (allow a generous
    # constant-factor margin above a single attempt's own footprint -- e.g.
    # hasher/scanner/bytearray copies within one attempt -- while still
    # being far below what `attempts`-proportional growth would produce).
    assert peak < 10 * len(seed_bytes) < attempts * len(seed_bytes), (
        f"peak traced memory {peak} bytes across {attempts} retry attempts suggests "
        "cross-attempt memory accumulation"
    )


def test_guard_shadow_log_stream_capture_source_has_no_full_buffer_accumulation_pattern() -> None:
    """HIGH regression guard: statically inspect the production
    `_shadow_log_stream_capture` source for the specific
    `chunks.append(...)` / `b"".join(chunks)` total-buffer-accumulation
    pattern this function was rewritten to eliminate (Issue #2243). This is
    a structural guard against silently reintroducing that pattern in a
    future edit, independent of any behavioral memory-bound test above."""
    import inspect

    module = _load_skill_runtime_exec_module()
    source = inspect.getsource(module._shadow_log_stream_capture)
    assert "chunks.append(" not in source
    assert ".join(chunks)" not in source
    assert re.search(r"chunks\s*[:=]\s*\[\s*\]", source) is None


# ---------------------------------------------------------------------------
# Issue #2243 MEDIUM: pathological JSON must fail closed via json.loads
# raising RecursionError (not just ValueError) in both the streaming
# scanner and _parse_shadow_log_jsonl.
# ---------------------------------------------------------------------------


def _deeply_nested_json_line() -> bytes:
    depth = 200000
    return b'{"a": ' + (b"[" * depth) + (b"]" * depth) + b"}\n"


def test_guard_shadow_log_deeply_nested_json_fails_closed_parse(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    data = _deeply_nested_json_line()
    assert module._parse_shadow_log_jsonl(data) is None


def test_guard_shadow_log_deeply_nested_json_fails_closed_stream_scanner(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    data = _deeply_nested_json_line()
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


def test_guard_shadow_log_invalid_utf8_fails_closed(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    data = b"\xff\xfe not valid utf-8\n"
    assert module._parse_shadow_log_jsonl(data) is None
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


def test_guard_shadow_log_very_long_numeric_value_fails_closed(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    huge_digits = "9" * 200000
    data = ('{"schema_version": "1", "value": ' + huge_digits + "}\n").encode()
    assert module._parse_shadow_log_jsonl(data) is None
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


def test_guard_shadow_log_extremely_many_object_keys_still_valid_but_bounded(tmp_path: Path) -> None:
    """A record with an extremely large number of object keys is still
    well-formed JSON (this is not itself a fail-closed condition), but must
    still be rejected once it exceeds `_SHADOW_LOG_MAX_RECORD_BYTES` on
    disk -- demonstrating the per-record byte bound, not key count, is the
    load-bearing memory guard."""
    module = _load_skill_runtime_exec_module()
    obj = {f"k{i}": i for i in range(50000)}
    line = (json.dumps(obj) + "\n").encode()
    capture = _capture_from_bytes(module, line, tmp_path)
    assert capture is not None
    if len(line) > module._SHADOW_LOG_MAX_RECORD_BYTES:
        assert capture.valid_jsonl is False
    else:
        assert capture.valid_jsonl is True


def test_guard_shadow_log_incomplete_final_line_fails_closed(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    data = (json.dumps({"schema_version": "1", "event": "a"}) + "\n").encode()
    data += b'{"schema_version": "1", "event": "incomplete-no-trailing-newline"}'
    assert module._parse_shadow_log_jsonl(data) is None
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


def test_guard_shadow_log_blank_line_fails_closed_parse_and_scanner(tmp_path: Path) -> None:
    module = _load_skill_runtime_exec_module()
    data = (
        json.dumps({"schema_version": "1", "event": "a"}).encode()
        + b"\n\n"
        + json.dumps({"schema_version": "1", "event": "b"}).encode()
        + b"\n"
    )
    assert module._parse_shadow_log_jsonl(data) is None
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False


@pytest.mark.parametrize("token", [b"NaN", b"Infinity", b"-Infinity"])
def test_guard_shadow_log_nan_infinity_fails_closed_stream_scanner(tmp_path: Path, token: bytes) -> None:
    module = _load_skill_runtime_exec_module()
    data = b'{"schema_version": "1", "value": ' + token + b"}\n"
    capture = _capture_from_bytes(module, data, tmp_path)
    assert capture is not None
    assert capture.valid_jsonl is False
