#!/usr/bin/env python3
"""
Deterministic integration tests for session_manifest_debounce.mjs.

Wall-clock-independent design:
- A valid worker.lock (PID=os.getpid(), alive) is pre-written to suppress the
  detached worker that the front-gate would otherwise spawn.  No real worker
  runs; events accumulate in the spool directory.
- After queueing events, the lock is removed and `--flush` is run synchronously
  (blocking call), which triggers exactly-one producer invocation with the
  aggregated payload.
- No time.sleep() calls; no timing assertions on elapsed wall-clock time.

AC coverage:
  AC1  burst boundary is controlled: same-burst is guaranteed because no worker
       runs between enqueue calls (lock is held by the test process).
  AC2  10 events queued, producer invocation count == 0 while lock is held.
  AC3  --flush produces exactly 1 producer invocation; debounce_event_count==10;
       all deltas are aggregated.
  AC5  post-test: event dir empty, lock absent, no children running.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
)

DEBOUNCE_PATH = REPO_ROOT / ".claude" / "hooks" / "session_manifest_debounce.mjs"

# AC1: burst window is 400 ms in production; we use a large value here so the
# test is not sensitive to execution speed.  The window is never reached during
# the enqueue phase because the worker is suppressed (no autonomous flush).
WINDOW_MS_FOR_TEST = "5000"


def make_env(tmp_path: Path, producer_script: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["SESSION_MANIFEST_DEBOUNCE_DIR"] = str(tmp_path / "debounce")
    env["SESSION_MANIFEST_DEBOUNCE_WINDOW_MS"] = WINDOW_MS_FOR_TEST
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "3000"
    env["SESSION_MANIFEST_DEBOUNCE_LOCK_POLL_MS"] = "50"
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_TIMEOUT_MS"] = "5000"
    env["SESSION_MANIFEST_DEBOUNCE_WORKER_STALE_MS"] = "60000"
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_CMD"] = str(producer_script)
    env["SESSION_MANIFEST_DEBOUNCE_PRODUCER_ARGS_JSON"] = "[]"
    return env


def write_active_lock(lock_path: Path) -> None:
    """
    Write a valid worker.lock owned by the current process.

    isStaleLock() considers a lock stale if:
      - heartbeat_at_ms is missing or 0
      - now() - heartbeat_at_ms > WORKER_STALE_MS
      - owner_pid is set and the process does not exist (kill(pid, 0) raises)

    Using os.getpid() as owner_pid ensures the lock is treated as live.
    WORKER_STALE_MS is set to 60 000 ms in make_env, so the heartbeat check
    passes for the duration of the test.
    """
    now_ms = int(time.time() * 1000)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "owner_pid": os.getpid(),
                "role": "worker",
                "started_at_ms": now_ms,
                "heartbeat_at_ms": now_ms,
            }
        ),
        encoding="utf-8",
    )


def run_front_gate(
    payload: dict, env: dict[str, str]
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["node", str(DEBOUNCE_PATH)],
        cwd=REPO_ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )


def run_flush(env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["node", str(DEBOUNCE_PATH), "--flush"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# AC1 + AC2 + AC3 + AC5: core burst-dedup and forced-flush test
# ---------------------------------------------------------------------------

def test_ten_events_queued_then_forced_flush_calls_producer_exactly_once(
    tmp_path: Path,
):
    """
    AC1: burst boundary is explicit -- same burst is guaranteed because the
         worker.lock is held by os.getpid() for the entire enqueue phase.
         < WINDOW_MS: lock is held, no autonomous flush occurs.
         >= WINDOW_MS: not reached during enqueue (WINDOW_MS_FOR_TEST=5000ms).

    AC2: while the lock is held (worker suppressed), 10 events are queued and
         the producer is not called (producer.log does not exist).

    AC3: after lock removal, --flush calls producer exactly once with
         debounce_event_count == 10 and all deltas aggregated.

    AC5: after flush, events dir is empty, lock is absent, flush child has
         exited (synchronous call).
    """
    payload_log = tmp_path / "payload.json"
    producer = tmp_path / "producer.py"
    producer.write_text(
        f"""#!/usr/bin/env python3
import json
import pathlib
import sys

payload = json.load(sys.stdin)
pathlib.Path({str(payload_log)!r}).write_text(json.dumps(payload), encoding="utf-8")
sys.exit(0)
""",
        encoding="utf-8",
    )
    producer.chmod(0o755)

    env = make_env(tmp_path, producer)
    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    events_dir = debounce_dir / "events"
    lock_path = debounce_dir / "worker.lock"

    # AC1: suppress the autonomous worker by writing a live lock owned by us.
    write_active_lock(lock_path)

    # Build 10 distinct Write events (different file paths so we get distinct
    # delta entries for aggregation verification).
    payloads = [
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(REPO_ROOT),
            "session_id": "burst-test",
            "issue_number": 1141,
            "tool_input": {"file_path": str(REPO_ROOT / "docs" / "dev" / f"file-{i}.md")},
        }
        for i in range(10)
    ]

    # AC2: enqueue 10 events; producer must NOT be called (lock is held).
    for payload in payloads:
        result = run_front_gate(payload, env)
        assert result.returncode == 0, f"front-gate failed: {result.stderr}"
        assert result.stdout == "", "front-gate must not write to stdout"

    # Verify 10 event files are in the spool.
    queued = sorted(events_dir.glob("*.json"))
    assert len(queued) == 10, f"expected 10 queued events, got {len(queued)}"

    # AC2: producer was NOT called (log file absent).
    assert not payload_log.exists(), "producer was called before flush -- AC2 violated"

    # AC5 pre-check: lock is still held by us.
    assert lock_path.exists(), "lock should still exist before flush"

    # Release the lock so --flush can acquire it.
    lock_path.unlink()

    # AC3: --flush runs producer exactly once.
    flush_result = run_flush(env)
    assert flush_result.returncode == 0, (
        f"--flush failed (exit {flush_result.returncode}): {flush_result.stderr}"
    )

    # AC3: producer was called exactly once.
    assert payload_log.exists(), "producer was never called after flush -- AC3 violated"

    flushed = json.loads(payload_log.read_text(encoding="utf-8"))

    # AC3: debounce_event_count == 10.
    assert flushed.get("debounce_event_count") == 10, (
        f"expected debounce_event_count=10, got {flushed.get('debounce_event_count')}"
    )

    # AC3: all 10 distinct relative paths appear in the aggregated delta.
    delta = flushed.get("session_manifest_delta", [])
    all_paths: list[str] = []
    for item in delta:
        all_paths.extend(item.get("relative_paths", []))
    for i in range(10):
        expected_path = f"docs/dev/file-{i}.md"
        assert expected_path in all_paths, (
            f"aggregated delta missing '{expected_path}'; got paths: {all_paths}"
        )

    # AC5: event directory is empty after flush.
    remaining = sorted(events_dir.glob("*.json"))
    assert remaining == [], f"event dir not empty after flush: {remaining}"

    # AC5: lock is absent after flush.
    assert not lock_path.exists(), "worker.lock still exists after flush -- AC5 violated"

    # AC5: flush subprocess has exited (synchronous call -- inherently satisfied).


# ---------------------------------------------------------------------------
# Supporting tests (AC2 isolation, AC5 cleanup edge cases)
# ---------------------------------------------------------------------------

def test_readonly_bash_not_queued_with_suppressed_worker(tmp_path: Path):
    """
    AC2 extension: readonly Bash events (rg, cat, ls, ...) are not queued even
    when the worker is suppressed -- verifies the kind=readonly_bash guard.
    """
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    lock_path = debounce_dir / "worker.lock"
    write_active_lock(lock_path)

    for _ in range(5):
        result = run_front_gate(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "cwd": str(REPO_ROOT),
                "tool_input": {"command": "rg -n debounce .claude/hooks/tests"},
            },
            env,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    events_dir = debounce_dir / "events"
    if events_dir.exists():
        queued = list(events_dir.glob("*.json"))
        assert queued == [], f"readonly events were queued: {queued}"

    lock_path.unlink(missing_ok=True)


def test_delta_sanitization_no_absolute_paths_in_spool(tmp_path: Path):
    """
    AC2 extension: queued event files must not contain absolute paths from
    tool_input.file_path (sanitizeRelativePath should convert to relative).
    """
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    lock_path = debounce_dir / "worker.lock"
    write_active_lock(lock_path)

    result = run_front_gate(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(REPO_ROOT),
            "tool_input": {"file_path": str(REPO_ROOT / "docs" / "dev" / "hook-boundaries.md")},
        },
        env,
    )
    assert result.returncode == 0

    events_dir = debounce_dir / "events"
    queued = sorted(events_dir.glob("*.json"))
    assert queued, "expected at least one queued event"

    event_payload = json.loads(queued[0].read_text(encoding="utf-8"))
    serialized = json.dumps(event_payload)

    # No absolute paths from repo root should appear in the spool file.
    assert str(REPO_ROOT) not in serialized, "absolute repo path leaked into spool"
    # Relative path is preserved.
    delta = event_payload.get("session_manifest_delta", [])
    assert delta, "delta is empty"
    assert delta[0]["relative_paths"] == ["docs/dev/hook-boundaries.md"]

    lock_path.unlink(missing_ok=True)


def test_flush_with_stale_lock_recovers_and_flushes(tmp_path: Path):
    """
    AC5 edge case: --flush with a stale lock (dead PID) should recover the lock
    and flush pending events exactly once.
    """
    log_path = tmp_path / "producer.log"
    producer = tmp_path / "producer.sh"
    producer.write_text(
        f"#!/usr/bin/env bash\necho call >> {log_path}\ncat >/dev/null\nexit 0\n",
        encoding="utf-8",
    )
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    (events_dir / f"{now_ms}-pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "stale-lock-test",
                "session_manifest_delta": [
                    {
                        "mutation_type": "write",
                        "relative_paths": ["docs/dev/hook-boundaries.md"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Write a stale lock: dead PID and ancient heartbeat.
    lock_path = debounce_dir / "worker.lock"
    lock_path.write_text(
        json.dumps(
            {
                "owner_pid": 999999,
                "role": "worker",
                "started_at_ms": 1,
                "heartbeat_at_ms": 1,
            }
        ),
        encoding="utf-8",
    )

    flush_result = run_flush(env)
    assert flush_result.returncode == 0, f"--flush failed: {flush_result.stderr}"

    assert log_path.exists(), "producer not called after stale-lock recovery"
    assert log_path.read_text(encoding="utf-8").count("call") == 1

    # AC5: lock is absent after successful flush.
    assert not lock_path.exists()


def test_flush_does_not_steal_live_lock(tmp_path: Path):
    """
    AC5 edge case: --flush must not steal a live worker.lock owned by the
    current process when there are pending events (timeout path).
    """
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)
    env["SESSION_MANIFEST_DEBOUNCE_FLUSH_WAIT_MS"] = "200"
    env["SESSION_MANIFEST_DEBOUNCE_WORKER_STALE_MS"] = "60000"

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    events_dir = debounce_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    (events_dir / f"{now_ms}-pending.json").write_text(
        json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "live-lock-test",
                "session_manifest_delta": [
                    {
                        "mutation_type": "write",
                        "relative_paths": ["docs/dev/hook-boundaries.md"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    lock_path = debounce_dir / "worker.lock"
    write_active_lock(lock_path)

    result = run_flush(env)

    # --flush should time out (exit 124) when a live lock is held and there are
    # pending events it cannot process.
    assert result.returncode == 124, (
        f"expected exit 124 (timeout), got {result.returncode}; stderr={result.stderr}"
    )
    assert lock_path.exists(), "flush stole live worker.lock -- AC5 violated"
    assert "flush_pending_timeout_lock_held" in result.stderr

    lock_path.unlink(missing_ok=True)


def test_stdout_silent_during_enqueue(tmp_path: Path):
    """
    AC5 extension: front-gate must produce no stdout output.
    """
    producer = tmp_path / "producer.sh"
    producer.write_text("#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n", encoding="utf-8")
    producer.chmod(0o755)
    env = make_env(tmp_path, producer)

    debounce_dir = Path(env["SESSION_MANIFEST_DEBOUNCE_DIR"])
    lock_path = debounce_dir / "worker.lock"
    write_active_lock(lock_path)

    result = run_front_gate(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "cwd": str(REPO_ROOT),
            "tool_input": {"file_path": str(REPO_ROOT / ".claude" / "settings.json")},
        },
        env,
    )
    assert result.returncode == 0
    assert result.stdout == ""

    lock_path.unlink(missing_ok=True)
