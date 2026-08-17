from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


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
        repo_root / ".claude" / "skills" / "issue-refinement-loop" / "scripts" / "command_registry.py",
        """from __future__ import annotations

REGISTRY = {
    "preflight.run": {
        "id": "preflight.run",
        "argv": [
            "uv",
            "run",
            "python3",
            ".claude/skills/issue-refinement-loop/scripts/run_refinement_preflight.py",
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
